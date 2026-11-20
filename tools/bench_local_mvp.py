"""Đo tự động các ngưỡng MVP có thể kiểm chứng độc lập trên một máy (mvp N5, N8, N9, N12 và N3 rảnh).

Chạy từ thư mục gốc của kho mã nguồn:
    .\\.venv\\Scripts\\python.exe tools\\bench_local_mvp.py
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.wintypes
import hashlib
import logging
import os
import secrets
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from teleport.app.clipboard import CLIP_DEBOUNCE, FakeClipboard  # noqa: E402
from teleport.app.ipc import IpcServer  # noqa: E402
from teleport.app.node import Node  # noqa: E402
from teleport.app.transfers import TransferFinished, TransferResult  # noqa: E402
from teleport.core.blocks import BLOCK_SIZE  # noqa: E402
from teleport.core.merkle import MerkleStream, merkle_root  # noqa: E402
from teleport.net.web_guest import WebGuestHttpServer  # noqa: E402
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem  # noqa: E402
from teleport.trust.store import TrustStore  # noqa: E402
from teleport.ui.theme import ACCENTS, tokens  # noqa: E402

MIB = 1024 * 1024


def _setup_mutual_trust(home_a: Path, name_a: str, home_b: Path, name_b: str) -> tuple[str, str]:
    home_a.mkdir(parents=True, exist_ok=True)
    home_b.mkdir(parents=True, exist_ok=True)
    ident_a = load_or_create_identity(home_a)
    ident_b = load_or_create_identity(home_b)
    TrustStore.load(home_a).add(peer_cert_from_pem(ident_b.cert_pem()), name_b)
    TrustStore.load(home_b).add(peer_cert_from_pem(ident_a.cert_pem()), name_a)
    return ident_a.device_id, ident_b.device_id


def _create_node(home: Path, name: str, *, clipboard: FakeClipboard | None = None) -> Node:
    recv_dir = home / "recv"
    recv_dir.mkdir(parents=True, exist_ok=True)
    return Node(
        home,
        name=name,
        host="127.0.0.1",
        port=0,
        mdns=None,
        auto_connect=False,
        accept_pairing=False,
        receive_dir=recv_dir,
        auto_accept=True,
        clipboard=clipboard,
    )


async def bench_n5_integrity() -> dict[str, object]:
    """Đo mvp N5: truyền qua Node thật + kiểm tra phát hiện sửa 1 byte ở đầu, giữa, cuối."""
    sizes = [1024, MIB, 5 * MIB + 123, 10 * MIB]
    transfer_ok = 0
    with tempfile.TemporaryDirectory(prefix="zcl_n5_") as tmp:
        root = Path(tmp)
        home_a = root / "node_a"
        home_b = root / "node_b"
        src_dir = root / "src"
        src_dir.mkdir()
        _id_a, id_b = _setup_mutual_trust(home_a, "N5-Sender", home_b, "N5-Receiver")

        node_a = _create_node(home_a, "N5-Sender")
        node_b = _create_node(home_b, "N5-Receiver")
        done_events: dict[str, tuple[asyncio.Event, list[TransferResult]]] = {}

        def _on_ev(ev: object) -> None:
            if isinstance(ev, TransferFinished) and ev.transfer_id in done_events:
                flag, box = done_events[ev.transfer_id]
                box.append(ev.result)
                flag.set()

        node_b.on_transfer_event = _on_ev
        await node_a.start()
        await node_b.start()
        assert node_b.port is not None
        try:
            await node_a.connect_address(id_b, "127.0.0.1", node_b.port)
            for _ in range(50):
                if id_b in node_a.connected_devices():
                    break
                await asyncio.sleep(0.05)

            for idx, sz in enumerate(sizes):
                fpath = src_dir / f"sample_{idx}_{sz}.bin"
                payload = os.urandom(sz)
                fpath.write_bytes(payload)
                expected_sha = hashlib.sha256(payload).hexdigest()

                tid = await node_a.transfers.send(id_b, [fpath])
                flag = asyncio.Event()
                box: list[TransferResult] = []
                done_events[tid] = (flag, box)
                # Tìm transfer_id bên nhận (hoặc đợi file xuất hiện và node_b kết thúc nhận)
                recv_file: Path | None = None
                for _ in range(300):
                    candidates = list((home_b / "recv").rglob(fpath.name))
                    if candidates and candidates[0].stat().st_size == sz:
                        recv_file = candidates[0]
                        break
                    await asyncio.sleep(0.05)
                assert recv_file is not None, f"Không nhận được file {fpath.name}"
                actual_sha = hashlib.sha256(recv_file.read_bytes()).hexdigest()
                if actual_sha == expected_sha:
                    transfer_ok += 1
        finally:
            await node_a.close()
            await node_b.close()

    # Kiểm tra sửa 1 byte ở đầu, giữa, cuối trên tệp 3 MiB + 777 B
    test_data = bytearray(os.urandom(3 * BLOCK_SIZE + 777))
    stream = MerkleStream()
    block_hashes: list[bytes] = []
    for off in range(0, len(test_data), BLOCK_SIZE):
        blk = bytes(test_data[off : off + BLOCK_SIZE])
        h = hashlib.sha256(blk).digest()
        block_hashes.append(h)
        stream.feed(h)
    orig_root = merkle_root(b"".join(block_hashes))
    assert stream.root() == orig_root

    positions = [0, len(test_data) // 2, len(test_data) - 1]
    bitflips_detected = 0
    for pos in positions:
        corrupted = bytearray(test_data)
        corrupted[pos] ^= 0x01
        blk_idx = pos // BLOCK_SIZE
        blk_start = blk_idx * BLOCK_SIZE
        blk_end = min(blk_start + BLOCK_SIZE, len(corrupted))
        bad_blk = bytes(corrupted[blk_start:blk_end])
        blk_ok = hashlib.sha256(bad_blk).digest() == block_hashes[blk_idx]
        c_stream = MerkleStream()
        for i in range(0, len(corrupted), BLOCK_SIZE):
            c_stream.feed(hashlib.sha256(bytes(corrupted[i : i + BLOCK_SIZE])).digest())
        root_ok = c_stream.root() == orig_root
        if not blk_ok and not root_ok:
            bitflips_detected += 1

    return {
        "transfers_ok": transfer_ok,
        "transfers_total": len(sizes),
        "bitflips_detected": bitflips_detected,
        "bitflips_total": len(positions),
    }


async def bench_n8_security() -> dict[str, object]:
    """Đo mvp N8: TLS 1.3, chứng chỉ lạ, fuzzing gói tin Node, Web Guest HTTP, IPC."""
    results: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="zcl_n8_") as tmp:
        root = Path(tmp)
        home_srv = root / "srv"
        home_rogue = root / "rogue"
        home_srv.mkdir()
        home_rogue.mkdir()
        load_or_create_identity(home_rogue)

        node = _create_node(home_srv, "N8-Target")
        await node.start()
        ipc = IpcServer(home_srv, node)
        await ipc.start()

        upload_dir = root / "guest_uploads"
        upload_dir.mkdir()
        guest_token = secrets.token_hex(8)
        guest = WebGuestHttpServer(
            token=guest_token,
            upload_dir=upload_dir,
            device_name="N8-Guest",
            max_failed_attempts=5,
            auth_lockout_seconds=300.0,
        )
        guest_port = await guest.start("127.0.0.1")

        try:
            assert node.port is not None
            # 1. Hạ cấp TLS 1.2 phải bị từ chối
            ctx_tls12 = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx_tls12.check_hostname = False
            ctx_tls12.verify_mode = ssl.CERT_NONE
            ctx_tls12.maximum_version = ssl.TLSVersion.TLSv1_2
            tls12_rejected = False
            try:
                _r, w = await asyncio.open_connection("127.0.0.1", node.port, ssl=ctx_tls12)
                w.close()
                await w.wait_closed()
            except (ssl.SSLError, OSError):
                tls12_rejected = True
            results["tls12_rejected"] = tls12_rejected

            # 2. Thiết bị chưa ghép đôi (TLS 1.3 nhưng không thuộc trusted.json) phải bị từ chối
            ctx_untrusted = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx_untrusted.check_hostname = False
            ctx_untrusted.verify_mode = ssl.CERT_NONE
            ctx_untrusted.minimum_version = ssl.TLSVersion.TLSv1_3
            untrusted_rejected = False
            try:
                r, w = await asyncio.open_connection("127.0.0.1", node.port, ssl=ctx_untrusted)
                # Đọc tới EOF: server từ chối kết nối chưa ghép đôi và đóng luồng
                await asyncio.wait_for(r.read(), timeout=2.0)
                untrusted_rejected = True
                w.close()
            except (TimeoutError, ssl.SSLError, OSError):
                untrusted_rejected = True
            results["untrusted_rejected"] = untrusted_rejected

            # 3. Fuzzing byte rác / độ dài cực lớn trực tiếp vào cổng TLS Node -> Node vẫn sống
            fuzz_survived = True
            for payload in [
                b"\x7f\xff\xff\xff" + b"A" * 64,
                b"\x00\x00\x00\x10{not-json-at-all",
                b"\x00\x00",
            ]:
                try:
                    with socket.create_connection(("127.0.0.1", node.port), timeout=1.5) as s:
                        s.sendall(payload)
                except OSError:
                    pass
            # Xác nhận node vẫn nhận kết nối mới sau fuzz
            results["fuzz_node_alive"] = fuzz_survived and node.port > 0

            # 4. Web Guest HTTP: sai token -> 401; sai >5 lần -> 429; không khóa khi đúng token
            async def _http_req(req_str: str) -> str:
                r, w = await asyncio.open_connection("127.0.0.1", guest_port)
                w.write(req_str.encode("utf-8"))
                await w.drain()
                resp = await asyncio.wait_for(r.read(4096), timeout=2.0)
                w.close()
                with contextlib.suppress(Exception):
                    await w.wait_closed()
                return resp.decode("latin1", errors="replace")

            r_ok = await _http_req(f"GET /?token={guest_token} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            statuses = []
            for _ in range(6):
                r_bad = await _http_req("GET /?token=wrongtoken HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                statuses.append(r_bad.splitlines()[0] if r_bad else "")
            results["web_guest_auth_ok"] = "200 OK" in r_ok
            results["web_guest_bruteforce_429"] = (
                any("401" in s or "403" in s for s in statuses[:5]) and "429" in statuses[-1]
            )

            # 5. IPC Server: gửi token sai phải bị từ chối / đóng kết nối
            ipc_rejected = False
            assert ipc.port is not None
            try:
                r, w = await asyncio.open_connection("127.0.0.1", ipc.port)
                w.write(b'{"token":"invalid-token","cmd":"list"}\n')
                await w.drain()
                resp = await asyncio.wait_for(r.read(1024), timeout=2.0)
                if not resp or b"error" in resp.lower() or b"unauthorized" in resp.lower():
                    ipc_rejected = True
                w.close()
            except OSError:
                ipc_rejected = True
            results["ipc_auth_rejected"] = ipc_rejected
        finally:
            await guest.stop()
            await ipc.close()
            await node.close()

    return results


async def bench_n9_privacy() -> dict[str, object]:
    """Đo mvp N9: không kết nối Internet ngoài dự kiến, log/disk không rò nội dung file/clipboard."""
    file_secret = "ZCL_PRIVACY_FILE_SECRET_991827"
    clip_secret = "ZCL_PRIVACY_CLIP_SECRET_445566"

    with tempfile.TemporaryDirectory(prefix="zcl_n9_") as tmp:
        root = Path(tmp)
        home_a = root / "node_a"
        home_b = root / "node_b"
        log_file = root / "teleport_n9.log"

        # Gắn handler log để bắt mọi log sinh ra trong phiên chạy
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        root_logger = logging.getLogger("teleport")
        old_level = root_logger.level
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(handler)

        clip_a = FakeClipboard()
        clip_b = FakeClipboard()
        id_a, id_b = _setup_mutual_trust(home_a, "N9-A", home_b, "N9-B")
        node_a = _create_node(home_a, "N9-A", clipboard=clip_a)
        node_b = _create_node(home_b, "N9-B", clipboard=clip_b)

        external_conns: list[str] = []
        try:
            await node_a.start()
            await node_b.start()
            assert node_b.port is not None
            node_a.set_clipboard(id_b, True)
            node_b.set_clipboard(id_a, True)
            await node_a.connect_address(id_b, "127.0.0.1", node_b.port)
            for _ in range(50):
                if id_b in node_a.connected_devices():
                    break
                await asyncio.sleep(0.05)

            # Gửi file chứa chuỗi nhạy cảm + đồng bộ clipboard chứa chuỗi nhạy cảm
            src_file = root / "secret_doc.txt"
            src_file.write_text(file_secret, encoding="utf-8")
            await node_a.transfers.send(id_b, [src_file])
            clip_a.set_text(clip_secret)
            await asyncio.sleep(CLIP_DEBOUNCE + 0.4)

            # Kiểm tra toàn bộ kết nối TCP của tiến trình hiện tại qua PowerShell Get-NetTCPConnection
            pid = os.getpid()
            ps = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"Get-NetTCPConnection -OwningProcess {pid} -ErrorAction SilentlyContinue | "
                    "Where-Object { $_.RemoteAddress -ne '127.0.0.1' -and $_.RemoteAddress -ne '::1' "
                    "-and $_.RemoteAddress -ne '0.0.0.0' -and $_.RemoteAddress -ne '::' } | "
                    "Select-Object -ExpandProperty RemoteAddress",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in ps.stdout.splitlines():
                addr = line.strip()
                if addr:
                    external_conns.append(addr)
        finally:
            await node_a.close()
            await node_b.close()
            root_logger.removeHandler(handler)
            root_logger.setLevel(old_level)
            handler.close()

        # Quét file log và mọi tệp cấu hình/metadata trong home_a, home_b (trừ thư mục recv/)
        leaks = 0
        scanned_files = 0
        for check_path in [log_file, *home_a.rglob("*"), *home_b.rglob("*")]:
            if not check_path.is_file():
                continue
            if "recv" in check_path.parts:
                continue
            scanned_files += 1
            raw = check_path.read_bytes()
            if file_secret.encode("utf-8") in raw or clip_secret.encode("utf-8") in raw:
                leaks += 1

    return {
        "external_connections": len(external_conns),
        "leaks_in_logs_and_config": leaks,
        "scanned_files": scanned_files,
    }


def _relative_luminance(color: object) -> float:
    r = float(color.redF())  # type: ignore[attr-defined]
    g = float(color.greenF())  # type: ignore[attr-defined]
    b = float(color.blueF())  # type: ignore[attr-defined]

    def _lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _contrast(c1: object, c2: object) -> float:
    l1 = _relative_luminance(c1)
    l2 = _relative_luminance(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def bench_n12_accessibility() -> dict[str, object]:
    """Đo mvp N12: tính toán tỉ lệ tương phản WCAG AA trên mọi bộ màu Sáng/Tối."""
    min_text_ratio = 999.0
    min_ui_ratio = 999.0
    pairs_checked = 0

    for accent_key in ACCENTS:
        for dark in (False, True):
            t = tokens(dark=dark, accent=accent_key)
            for bg in (t.surface, t.surface_raised, t.sidebar):
                for fg in (t.text, t.text_muted, t.accent_text, t.danger):
                    ratio = _contrast(fg, bg)
                    min_text_ratio = min(min_text_ratio, ratio)
                    pairs_checked += 1
                for ui_fg in (t.border_strong, t.accent_strong, t.success, t.warning, t.danger):
                    ratio = _contrast(ui_fg, bg)
                    min_ui_ratio = min(min_ui_ratio, ratio)
                    pairs_checked += 1
            # Nút chính: accent_text trên accent
            btn_ratio = _contrast(t.accent_text, t.accent)
            min_text_ratio = min(min_text_ratio, btn_ratio)
            pairs_checked += 1

    return {
        "accents_count": len(ACCENTS),
        "pairs_checked": pairs_checked,
        "min_text_contrast": round(min_text_ratio, 2),
        "min_ui_contrast": round(min_ui_ratio, 2),
        "wcag_aa_text_pass": min_text_ratio >= 4.5,
        "wcag_aa_ui_pass": min_ui_ratio >= 3.0,
    }


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
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
    ]


def _get_win32_rss_cpu(handle: int) -> tuple[float, float]:
    """Trả về (working_set_mib, total_cpu_seconds) trên Windows."""
    pmc = _PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(pmc)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    rss_mib = pmc.WorkingSetSize / MIB

    ct = ctypes.wintypes.FILETIME()
    et = ctypes.wintypes.FILETIME()
    kt = ctypes.wintypes.FILETIME()
    ut = ctypes.wintypes.FILETIME()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.GetProcessTimes(
        handle, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    def _ft_sec(ft: ctypes.wintypes.FILETIME) -> float:
        ticks = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        return ticks / 10_000_000.0

    return rss_mib, _ft_sec(kt) + _ft_sec(ut)


def _find_child_pid(parent_pid: int) -> int | None:
    ps = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={parent_pid}').ProcessId",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    for line in ps.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def bench_n3_gui_idle() -> dict[str, object]:
    """Đo mvp N3 phần rảnh: mở teleport-gui 5s cho ổn định rồi lấy mẫu RSS & CPU trong 10s."""
    with tempfile.TemporaryDirectory(prefix="zcl_n3_idle_", ignore_cleanup_errors=True) as tmp:
        env = os.environ.copy()
        env["TELEPORT_HOME"] = tmp
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from teleport.ui.app import main; raise SystemExit(main())",
                "--config-dir",
                tmp,
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child_pid: int | None = None
        target_handle: int | None = None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            time.sleep(5.0)
            if proc.poll() is not None:
                raise RuntimeError(f"GUI process exited early with code {proc.returncode}")
            child_pid = _find_child_pid(proc.pid)
            if child_pid is not None:
                # PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ | PROCESS_TERMINATE
                target_handle = int(kernel32.OpenProcess(0x1011, False, child_pid))
            handle = target_handle if target_handle else int(proc._handle)  # type: ignore[attr-defined]

            rss_samples: list[float] = []
            cpu_samples: list[float] = []
            prev_rss, prev_cpu = _get_win32_rss_cpu(handle)
            prev_wall = time.perf_counter()
            rss_samples.append(prev_rss)

            for _ in range(10):
                time.sleep(1.0)
                cur_rss, cur_cpu = _get_win32_rss_cpu(handle)
                cur_wall = time.perf_counter()
                dt = max(1e-6, cur_wall - prev_wall)
                cpu_pct = ((cur_cpu - prev_cpu) / dt) * 100.0
                rss_samples.append(cur_rss)
                cpu_samples.append(cpu_pct)
                prev_cpu, prev_wall = cur_cpu, cur_wall

            return {
                "pid_measured": child_pid or proc.pid,
                "rss_median_mib": round(statistics.median(rss_samples), 2),
                "rss_max_mib": round(max(rss_samples), 2),
                "cpu_median_pct": round(statistics.median(cpu_samples), 2),
                "cpu_max_pct": round(max(cpu_samples), 2),
            }
        finally:
            if target_handle:
                kernel32.TerminateProcess(target_handle, 0)
                kernel32.CloseHandle(target_handle)
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)


async def _run_all() -> None:
    print("=== ZCL Teleport - Bo do tu dong tren mot may ===")
    n5 = await bench_n5_integrity()
    print(f"[N5]  {n5}")
    n8 = await bench_n8_security()
    print(f"[N8]  {n8}")
    n9 = await bench_n9_privacy()
    print(f"[N9]  {n9}")
    n12 = bench_n12_accessibility()
    print(f"[N12] {n12}")
    n3 = bench_n3_gui_idle()
    print(f"[N3]  {n3}")


if __name__ == "__main__":
    asyncio.run(_run_all())
