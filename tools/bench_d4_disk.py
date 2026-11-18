"""Đo D4 (`mvp N3` trên ổ đĩa đích chậm, nhiều file nhỏ và từ chối FAT32 >= 4 GiB).

Chạy được hoàn toàn trên một máy không cần USB thật nhờ hai chế độ:
- Giới hạn tốc độ ghi `PartFile.write_at` (`--simulate-slow-mbps`, mặc định 15 MiB/s)
- Mô phỏng độ trễ đồng bộ đĩa `storage._full_fsync` (`--simulate-fsync-ms`, mặc định 15 ms)
- Lô nhiều file nhỏ ngẫu nhiên 4–64 KiB (`--small-files`)
- Kiểm tra giả lập FAT32 tự động từ chối file >= 4 GiB (`--test-fat32`)

Ví dụ:
    python tools/bench_d4_disk.py --size 256MiB --runs 3 --small-files 100 --test-fat32
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.wintypes
import hashlib
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Hỗ trợ chạy trực tiếp khi chưa cài editable
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from teleport.app import storage  # noqa: E402
from teleport.app.node import Node  # noqa: E402
from teleport.app.transfers import TransferFinished, TransferResult, TransferUpdate  # noqa: E402
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem  # noqa: E402
from teleport.trust.store import TrustStore  # noqa: E402

try:
    import psutil  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    psutil = None


MIB = 1024 * 1024
DEFAULT_SLOW_MBPS = 15.0
DEFAULT_FSYNC_MS = 15.0
SAMPLE_INTERVAL_S = 0.02
WAIT_READY_TIMEOUT_S = 20.0
TRANSFER_TIMEOUT_S = 600.0


def parse_size(size_str: str) -> int:
    s = size_str.strip().upper()
    multipliers = {
        "GIB": 1024**3,
        "GB": 1000**3,
        "MIB": 1024**2,
        "MB": 1000**2,
        "KIB": 1024,
        "KB": 1000,
    }
    for suffix, mult in multipliers.items():
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)].strip()) * mult)
    if s.endswith("B"):
        return int(s[:-1].strip())
    return int(s)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


class _ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class ProcessMemoryProbe:
    """Đọc WorkingSetSize và PeakWorkingSetSize của tiến trình đích trên Windows/POSIX."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._handle: Any = None
        self._kernel32: Any = None
        self._get_mem_info: Any = None
        if os.name == "nt":
            k32 = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION (0x1000) | PROCESS_VM_READ (0x0010)
            handle = k32.OpenProcess(0x1000 | 0x0010, False, pid)
            if handle:
                self._handle = handle
                self._kernel32 = k32
                fn = getattr(k32, "K32GetProcessMemoryInfo", None)
                if fn is None:
                    fn = ctypes.windll.psapi.GetProcessMemoryInfo
                fn.argtypes = [
                    ctypes.wintypes.HANDLE,
                    ctypes.POINTER(_ProcessMemoryCountersEx),
                    ctypes.wintypes.DWORD,
                ]
                fn.restype = ctypes.wintypes.BOOL
                self._get_mem_info = fn

    def read(self) -> tuple[int, int] | None:
        """Trả về `(working_set_bytes, peak_working_set_bytes)` hoặc None nếu không đọc được."""
        if os.name == "nt" and self._handle and self._get_mem_info is not None:
            counters = _ProcessMemoryCountersEx()
            counters.cb = ctypes.sizeof(_ProcessMemoryCountersEx)
            if self._get_mem_info(self._handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)
        if psutil is not None:
            try:
                info = psutil.Process(self.pid).memory_info()
                peak = getattr(info, "peak_wset", info.rss)
                return int(info.rss), int(peak)
            except Exception:
                return None
        return None

    def close(self) -> None:
        if os.name == "nt" and self._handle and self._kernel32 is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


@dataclass
class RunResult:
    label: str
    run_index: int
    total_bytes: int
    elapsed_s: float
    speed_mb_s: float
    baseline_mib: float
    peak_mib: float
    delta_mib: float
    q4_minus_q1_mib: float
    samples: int
    sha_ok: bool
    temp_clean: bool


def setup_mutual_trust(recv_home: Path, send_home: Path) -> tuple[str, str]:
    """Tạo danh tính và ghép đôi sẵn giữa hai thư mục cấu hình tạm."""
    recv_home.mkdir(parents=True, exist_ok=True)
    send_home.mkdir(parents=True, exist_ok=True)
    recv_ident = load_or_create_identity(recv_home)
    send_ident = load_or_create_identity(send_home)
    recv_store = TrustStore.load(recv_home)
    recv_store.add(peer_cert_from_pem(send_ident.cert_pem()), "bench-send")
    send_store = TrustStore.load(send_home)
    send_store.add(peer_cert_from_pem(recv_ident.cert_pem()), "bench-recv")
    return recv_ident.device_id, send_ident.device_id


def check_teleport_clean(recv_dir: Path, *, timeout_s: float = 2.0) -> bool:
    """Chờ dọn dẹp và kiểm tra trong `.teleport` không còn `.part`, `.state` hay `.tmp`."""
    tp_dir = recv_dir / ".teleport"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not tp_dir.exists():
            return True
        leftover = [
            p
            for p in tp_dir.rglob("*")
            if p.is_file() and (p.name.endswith((".part", ".state", ".tmp")) or ".tmp." in p.name)
        ]
        if not leftover:
            return True
        time.sleep(0.05)
    return False


def collect_received_files(recv_dir: Path) -> dict[str, Path]:
    """Thu thập toàn bộ file đã nhận (bỏ qua cây `.teleport`), ánh xạ theo tên file."""
    found: dict[str, Path] = {}
    for p in recv_dir.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(recv_dir)
        if ".teleport" in rel.parts:
            continue
        found[p.name] = p
    return found


def compute_q4_minus_q1_mib(samples: list[int]) -> float:
    if len(samples) < 4:
        return 0.0
    n = len(samples)
    q1 = max(samples[: max(1, n // 4)])
    q4 = max(samples[n - max(1, n // 4) :])
    return (q4 - q1) / MIB


def _parse_json_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def start_recv_worker(
    recv_home: Path,
    recv_dir: Path,
    port: int,
    slow_mbps: float,
    fsync_ms: float,
) -> tuple[subprocess.Popen[str], int]:
    """Khởi chạy tiến trình nhận qua `--_recv-worker` và chờ cổng lắng nghe."""
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--_recv-worker",
        "--",
        "--config-dir",
        str(recv_home),
        "receive",
        "--name",
        "bench-recv",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--dir",
        str(recv_dir),
        "--yes",
        "--json",
    ]
    env = os.environ.copy()
    env["TELEPORT_HOME"] = str(recv_home)
    env["PYTHONIOENCODING"] = "utf-8"
    env["TELEPORT_SIM_SLOW_MBPS"] = str(slow_mbps)
    env["TELEPORT_SIM_FSYNC_MS"] = str(fsync_ms)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=env,
    )
    assert proc.stdout is not None
    bound_port: int | None = None
    deadline = time.monotonic() + WAIT_READY_TIMEOUT_S
    seen_lines: list[str] = []
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"Tiến trình nhận thoát sớm ({proc.returncode}): {seen_lines} | {err}")
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.02)
            continue
        seen_lines.append(line.strip())
        ev = _parse_json_line(line)
        if ev and ev.get("event") == "receiving":
            bound_port = int(ev["port"])
            break
    if bound_port is None:
        proc.terminate()
        raise RuntimeError(f"Hết giờ chờ receiver sẵn sàng: {seen_lines}")

    # Xả phần stdout còn lại trên luồng nền để tránh nghẽn pipe trong lúc truyền
    def _drain() -> None:
        assert proc.stdout is not None
        for _ in proc.stdout:
            pass

    threading.Thread(target=_drain, daemon=True).start()
    return proc, bound_port


def run_single_transfer(
    label: str,
    run_idx: int,
    send_home: Path,
    recv_home: Path,
    recv_dir: Path,
    sources: list[Path],
    expected_hashes: dict[str, str],
    total_bytes: int,
    port: int,
    slow_mbps: float,
    fsync_ms: float,
) -> RunResult:
    if recv_dir.exists():
        shutil.rmtree(recv_dir)
    recv_dir.mkdir(parents=True, exist_ok=True)

    recv_proc, actual_port = start_recv_worker(recv_home, recv_dir, port, slow_mbps, fsync_ms)
    probe = ProcessMemoryProbe(recv_proc.pid)
    samples: list[int] = []
    stop_event = threading.Event()

    # Đọc baseline lúc receiver đã ổn định ở trạng thái chờ
    time.sleep(0.15)
    initial = probe.read()
    if initial is None:
        recv_proc.terminate()
        raise RuntimeError("Không đọc được bộ nhớ tiến trình nhận")
    baseline_ws, os_peak_ws = initial

    def _sample_loop() -> None:
        nonlocal os_peak_ws
        while not stop_event.is_set():
            snap = probe.read()
            if snap is not None:
                ws, pk = snap
                samples.append(ws)
                if pk > os_peak_ws:
                    os_peak_ws = pk
            stop_event.wait(SAMPLE_INTERVAL_S)

    sampler = threading.Thread(target=_sample_loop, daemon=True)
    sampler.start()

    try:
        send_cmd = [
            sys.executable,
            "-m",
            "teleport",
            "--config-dir",
            str(send_home),
            "send",
            "--name",
            "bench-send",
            *[str(p) for p in sources],
            "--to",
            "bench-recv",
            "--address",
            f"127.0.0.1:{actual_port}",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--json",
        ]
        send_env = os.environ.copy()
        send_env["TELEPORT_HOME"] = str(send_home)
        send_env["PYTHONIOENCODING"] = "utf-8"

        t0 = time.perf_counter()
        send_res = subprocess.run(
            send_cmd,
            env=send_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TRANSFER_TIMEOUT_S,
        )
        t1 = time.perf_counter()
        if send_res.returncode != 0:
            raise RuntimeError(
                f"teleport send thất bại (code={send_res.returncode}):\n"
                f"STDOUT: {send_res.stdout}\nSTDERR: {send_res.stderr}"
            )
    finally:
        stop_event.set()
        sampler.join(timeout=2.0)
        final_snap = probe.read()
        if final_snap is not None and final_snap[1] > os_peak_ws:
            os_peak_ws = final_snap[1]
        probe.close()
        recv_proc.terminate()
        try:
            recv_proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            recv_proc.kill()
            recv_proc.wait(timeout=5.0)

    elapsed = max(t1 - t0, 1e-6)
    peak_ws = max([os_peak_ws, *samples]) if samples else os_peak_ws
    received = collect_received_files(recv_dir)
    sha_ok = set(received.keys()) == set(expected_hashes.keys()) and all(
        sha256_file(received[name]) == expected_hashes[name] for name in expected_hashes
    )
    temp_clean = check_teleport_clean(recv_dir)
    return RunResult(
        label=label,
        run_index=run_idx,
        total_bytes=total_bytes,
        elapsed_s=elapsed,
        speed_mb_s=(total_bytes / 1_000_000) / elapsed,
        baseline_mib=baseline_ws / MIB,
        peak_mib=peak_ws / MIB,
        delta_mib=(peak_ws - baseline_ws) / MIB,
        q4_minus_q1_mib=compute_q4_minus_q1_mib(samples),
        samples=len(samples),
        sha_ok=sha_ok,
        temp_clean=temp_clean,
    )


def create_random_file(path: Path, size_bytes: int, rng: random.Random) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    remaining = size_bytes
    chunk_size = 1024 * 1024
    with path.open("wb") as f:
        while remaining > 0:
            n = min(remaining, chunk_size)
            data = rng.randbytes(n)
            f.write(data)
            h.update(data)
            remaining -= n
    return h.hexdigest()


def create_sparse_oversize_file(path: Path, size_bytes: int) -> bool:
    """Tạo file thưa >= 4 GiB không tốn dung lượng đĩa thật (Windows FSCTL_SET_SPARSE / POSIX truncate)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            import msvcrt

            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
            try:
                osf = msvcrt.get_osfhandle(fd)
                bytes_ret = ctypes.wintypes.DWORD(0)
                ok = ctypes.windll.kernel32.DeviceIoControl(
                    ctypes.wintypes.HANDLE(osf),
                    0x000900C4,  # FSCTL_SET_SPARSE
                    None,
                    0,
                    None,
                    0,
                    ctypes.byref(bytes_ret),
                    None,
                )
                if not ok:
                    return False
                os.lseek(fd, size_bytes - 1, os.SEEK_SET)
                os.write(fd, b"\x00")
            finally:
                os.close(fd)
            return path.stat().st_size == size_bytes
        with path.open("wb") as f:
            f.truncate(size_bytes)
        return path.stat().st_size == size_bytes
    except OSError:
        return False


async def _run_fat32_rejection_async(work_root: Path) -> dict[str, Any]:
    """Kiểm tra `volume_info` dưới `"vfat"` và Node thật tự từ chối file >= 4 GiB."""
    fat_recv_home = work_root / "fat_recv_home"
    fat_send_home = work_root / "fat_send_home"
    fat_recv_dir = work_root / "fat_recv_dir"
    fat_recv_dir.mkdir(parents=True, exist_ok=True)
    recv_id, send_id = setup_mutual_trust(fat_recv_home, fat_send_home)

    orig_fs_type = storage._fs_type_of
    orig_fat_max = storage.FAT_MAX_FILE_SIZE
    storage._fs_type_of = lambda _p: "vfat"  # type: ignore[assignment]
    try:
        vol = storage.volume_info(fat_recv_dir)
        assert vol.fs_type == "vfat", f"fs_type sai: {vol.fs_type}"
        assert vol.max_file_size == orig_fat_max == (1 << 32) - 1, vol

        oversize_path = work_root / "oversize_4gib.bin"
        used_real_4gib = create_sparse_oversize_file(oversize_path, orig_fat_max + 1)
        if not used_real_4gib:
            # Nếu hệ file không cho tạo sparse file 4 GiB, lùi về seam FAT_MAX_FILE_SIZE = 1 MiB
            storage.FAT_MAX_FILE_SIZE = MIB
            oversize_path.write_bytes(os.urandom(MIB + 1024))
        tested_size = oversize_path.stat().st_size
        active_limit = storage.FAT_MAX_FILE_SIZE

        recv_events: list[TransferUpdate] = []
        send_events: list[TransferUpdate] = []
        recv_done = asyncio.Event()
        send_done = asyncio.Event()

        def _on_recv(ev: TransferUpdate) -> None:
            recv_events.append(ev)
            if isinstance(ev, TransferFinished):
                recv_done.set()

        def _on_send(ev: TransferUpdate) -> None:
            send_events.append(ev)
            if isinstance(ev, TransferFinished):
                send_done.set()

        node_r = Node(
            fat_recv_home,
            name="bench-recv",
            host="127.0.0.1",
            port=0,
            mdns=None,
            auto_connect=False,
            receive_dir=fat_recv_dir,
            auto_accept=True,
            on_transfer_event=_on_recv,
        )
        node_s = Node(
            fat_send_home,
            name="bench-send",
            host="127.0.0.1",
            port=0,
            mdns=None,
            auto_connect=False,
            receive_dir=None,
            auto_accept=False,
            on_transfer_event=_on_send,
        )
        await node_r.start()
        await node_s.start()
        try:
            assert node_r.port is not None
            node_s.connect_hint(recv_id, ["127.0.0.1"], node_r.port)
            await node_s.transfers.send(recv_id, [oversize_path])
            await asyncio.wait_for(asyncio.gather(send_done.wait(), recv_done.wait()), timeout=15.0)
        finally:
            await node_s.close()
            await node_r.close()

        fin_s = next(e for e in send_events if isinstance(e, TransferFinished))
        fin_r = next(e for e in recv_events if isinstance(e, TransferFinished))
        ok = (
            fin_s.result is TransferResult.REJECTED_FILE_TOO_LARGE
            and fin_s.by_peer is True
            and fin_s.reject is not None
            and fin_s.reject.reason == "file_too_large"
            and fin_s.reject.max_size == active_limit
            and fin_r.result is TransferResult.REJECTED_FILE_TOO_LARGE
            and fin_r.by_peer is False
            and not collect_received_files(fat_recv_dir)
        )
        return {
            "passed": ok,
            "used_real_4gib_sparse": used_real_4gib,
            "tested_size_bytes": tested_size,
            "volume_max_file_size": vol.max_file_size,
            "send_result": fin_s.result.value,
            "recv_result": fin_r.result.value,
            "reject_max_size": fin_s.reject.max_size if fin_s.reject else None,
            "recv_id": recv_id[:8],
            "send_id": send_id[:8],
        }
    finally:
        storage._fs_type_of = orig_fs_type
        storage.FAT_MAX_FILE_SIZE = orig_fat_max


def _print_summary(label: str, rows: list[RunResult]) -> bool:
    print(f"\n=== {label} ({len(rows)} lượt) ===")
    for r in rows:
        print(
            f"  Lượt {r.run_index}: time={r.elapsed_s:.2f}s | speed={r.speed_mb_s:.2f} MB/s | "
            f"base={r.baseline_mib:.2f} MiB | peak={r.peak_mib:.2f} MiB | "
            f"delta=+{r.delta_mib:.2f} MiB | Q4-Q1={r.q4_minus_q1_mib:+.2f} MiB | "
            f"samples={r.samples} | sha={'OK' if r.sha_ok else 'FAIL'} | "
            f"clean={'OK' if r.temp_clean else 'FAIL'}"
        )
    med_delta = statistics.median(r.delta_mib for r in rows)
    max_delta = max(r.delta_mib for r in rows)
    max_q4_q1 = max(r.q4_minus_q1_mib for r in rows)
    med_speed = statistics.median(r.speed_mb_s for r in rows)
    all_ok = all(
        r.sha_ok and r.temp_clean and r.delta_mib <= 100.0 and r.q4_minus_q1_mib <= 4.0 for r in rows
    )
    print(
        f"  -> Trung vị tốc độ: {med_speed:.2f} MB/s | "
        f"Trung vị RAM tăng: +{med_delta:.2f} MiB (max +{max_delta:.2f} MiB, ngưỡng N3 <= 100 MiB) | "
        f"max(Q4-Q1): {max_q4_q1:+.2f} MiB (ngưỡng <= 4.0 MiB) | "
        f"Kết luận: {'ĐẠT' if all_ok else 'KHÔNG ĐẠT'}"
    )
    return all_ok


def _recv_worker_main(raw_argv: list[str]) -> int:
    slow_mbps = float(os.environ.get("TELEPORT_SIM_SLOW_MBPS", "0"))
    fsync_ms = float(os.environ.get("TELEPORT_SIM_FSYNC_MS", "0"))
    if slow_mbps > 0:
        orig_write_at = storage.PartFile.write_at
        bytes_per_sec = slow_mbps * MIB

        def throttled_write_at(self: Any, index: int, data: bytes | bytearray | memoryview) -> None:
            t0 = time.perf_counter()
            orig_write_at(self, index, data)
            target = len(data) / bytes_per_sec
            elapsed = time.perf_counter() - t0
            if target > elapsed:
                time.sleep(target - elapsed)

        storage.PartFile.write_at = throttled_write_at  # type: ignore[method-assign]

    if fsync_ms > 0:
        orig_full_fsync = storage._full_fsync
        delay_s = fsync_ms / 1000.0

        def slow_full_fsync(fd: int) -> None:
            orig_full_fsync(fd)
            time.sleep(delay_s)

        storage._full_fsync = slow_full_fsync

    from teleport.cli import main as cli_main

    return cli_main(raw_argv)


def main() -> int:
    if "--_recv-worker" in sys.argv[1:]:
        sep = sys.argv.index("--")
        return _recv_worker_main(sys.argv[sep + 1 :])

    parser = argparse.ArgumentParser(
        description="Đo D4: ổ đích chậm, nhiều file nhỏ và FAT32 (giả lập phần mềm)"
    )
    parser.add_argument("--target-dir", default=None, help="Thư mục nhận đích (mặc định: thư mục tạm)")
    parser.add_argument("--size", default="256MiB", help="Dung lượng file lớn kiểm thử (mặc định: 256MiB)")
    parser.add_argument("--runs", type=int, default=3, help="Số lượt kiểm thử mỗi kịch bản (mặc định: 3)")
    parser.add_argument("--port", type=int, default=0, help="Cổng lắng nghe (mặc định: 0 = tự chọn)")
    parser.add_argument(
        "--work-dir", default=None, help="Thư mục làm việc tạm thời (mặc định: %%TEMP%%/tpd4_*)"
    )
    parser.add_argument(
        "--simulate-slow-mbps",
        type=float,
        default=DEFAULT_SLOW_MBPS,
        help=f"Giới hạn tốc độ ghi PartFile.write_at theo MiB/s (0 = tắt, mặc định: {DEFAULT_SLOW_MBPS})",
    )
    parser.add_argument(
        "--simulate-fsync-ms",
        type=float,
        default=DEFAULT_FSYNC_MS,
        help=f"Độ trễ _full_fsync giả lập theo ms (0 = tắt, mặc định: {DEFAULT_FSYNC_MS})",
    )
    parser.add_argument(
        "--small-files",
        type=int,
        default=0,
        help="Số file nhỏ ngẫu nhiên 4–64 KiB gửi trong một lượt (0 = bỏ qua)",
    )
    parser.add_argument(
        "--test-fat32",
        action="store_true",
        help="Kiểm tra giả lập FAT32 từ chối file >= 4 GiB",
    )
    args = parser.parse_args()

    # Giữ đường dẫn ngắn trên Windows để không chạm TEMP_PATH_RESERVE
    temp_ctx: tempfile.TemporaryDirectory[str] | None = None
    if args.work_dir is None:
        temp_ctx = tempfile.TemporaryDirectory(prefix="tpd4_")
        work_root = Path(temp_ctx.name)
    else:
        work_root = Path(args.work_dir).resolve()
        work_root.mkdir(parents=True, exist_ok=True)

    try:
        recv_home = work_root / "rh"
        send_home = work_root / "sh"
        target_base = Path(args.target_dir).resolve() if args.target_dir else (work_root / "dst")
        target_base.mkdir(parents=True, exist_ok=True)
        recv_id, send_id = setup_mutual_trust(recv_home, send_home)

        print("==================================================")
        print("BENCHMARK D4: Ổ ĐÍCH CHẬM / NHIỀU FILE NHỎ / FAT32")
        print(f"Thiết bị tin cậy: recv={recv_id[:8]}, send={send_id[:8]}")
        print(
            f"Mô phỏng đĩa: write_cap={args.simulate_slow_mbps:.1f} MiB/s, "
            f"fsync_delay={args.simulate_fsync_ms:.1f} ms"
        )
        print(f"Cỡ file lớn: {args.size} | Số lượt: {args.runs} | Small files: {args.small_files}")
        print("==================================================")

        rng = random.Random(20261118)
        all_passed = True

        # 1. Kịch bản file lớn trên ổ chậm
        size_bytes = parse_size(args.size)
        if size_bytes > 0 and args.runs > 0:
            big_src = work_root / "src_big" / "d4_large.bin"
            big_hash = create_random_file(big_src, size_bytes, rng)
            big_rows: list[RunResult] = []
            for idx in range(1, args.runs + 1):
                row = run_single_transfer(
                    label="large_slow_disk",
                    run_idx=idx,
                    send_home=send_home,
                    recv_home=recv_home,
                    recv_dir=target_base / f"big_{idx}",
                    sources=[big_src],
                    expected_hashes={big_src.name: big_hash},
                    total_bytes=size_bytes,
                    port=args.port,
                    slow_mbps=args.simulate_slow_mbps,
                    fsync_ms=args.simulate_fsync_ms,
                )
                big_rows.append(row)
            if not _print_summary(f"1. File lớn {args.size} trên ổ chậm", big_rows):
                all_passed = False

        # 2. Kịch bản nhiều file nhỏ (4–64 KiB) trên ổ chậm
        if args.small_files > 0 and args.runs > 0:
            small_dir = work_root / "src_small"
            small_hashes: dict[str, str] = {}
            small_total = 0
            for i in range(args.small_files):
                sz = rng.randint(4 * 1024, 64 * 1024)
                fp = small_dir / f"s_{i:04d}.bin"
                small_hashes[fp.name] = create_random_file(fp, sz, rng)
                small_total += sz
            small_rows: list[RunResult] = []
            for idx in range(1, args.runs + 1):
                row = run_single_transfer(
                    label="small_files_slow_disk",
                    run_idx=idx,
                    send_home=send_home,
                    recv_home=recv_home,
                    recv_dir=target_base / f"small_{idx}",
                    sources=[small_dir],
                    expected_hashes=small_hashes,
                    total_bytes=small_total,
                    port=args.port,
                    slow_mbps=args.simulate_slow_mbps,
                    fsync_ms=args.simulate_fsync_ms,
                )
                small_rows.append(row)
            if not _print_summary(
                f"2. Lô {args.small_files} file nhỏ (tổng {small_total / MIB:.2f} MiB) trên ổ chậm",
                small_rows,
            ):
                all_passed = False

        # 3. Kiểm tra giả lập FAT32
        if args.test_fat32:
            fat_res = asyncio.run(_run_fat32_rejection_async(work_root))
            print("\n=== 3. Kiểm tra FAT32 giả lập (_fs_type_of = 'vfat') ===")
            print(
                f"  max_file_size={fat_res['volume_max_file_size']:,} B | "
                f"sparse_4GiB={fat_res['used_real_4gib_sparse']} | "
                f"file_test={fat_res['tested_size_bytes']:,} B | "
                f"send={fat_res['send_result']} | recv={fat_res['recv_result']} | "
                f"Kết luận: {'ĐẠT' if fat_res['passed'] else 'KHÔNG ĐẠT'}"
            )
            if not fat_res["passed"]:
                all_passed = False

        return 0 if all_passed else 1
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
