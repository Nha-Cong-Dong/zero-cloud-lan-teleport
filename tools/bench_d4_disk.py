"""Kịch bản kiểm tra ghi file lên ổ đĩa tốc độ chậm (USB / thẻ nhớ) (mvp N3, phép đo D4).

Theo đặc tả tại docs/technical/benchmark.md mục 4.8:
- Kiểm tra hệ thống file ổ đích (NTFS, exFAT hay FAT32).
  Nếu là FAT32 và cỡ file >= 4 GiB: từ chối ghi và cảnh báo rõ giới hạn hệ thống file.
- Gửi file qua Teleport cục bộ hoặc từ máy gửi vào thư mục nhận trên ổ đĩa đích.
- Giám sát mức tăng bộ nhớ RAM (RSS, Private bytes) để đảm bảo không tràn bộ nhớ đệm khi ổ đĩa ghi chậm.
- Kiểm tra mã băm SHA-256 tính toàn vẹn và dọn dẹp file tạm (.part, .state).

Cách sử dụng:
    python tools/bench_d4_disk.py --dest-dir E:\\teleport_test --size 1GiB --runs 3
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore[assignment]

MiB = 1024 * 1024
GiB = 1024 * MiB


def parse_size(size_str: str) -> int:
    s = size_str.strip().upper()
    if s.endswith("GIB") or s.endswith("GB"):
        return int(float(s[:-3] if s.endswith("GIB") else s[:-2]) * GiB)
    if s.endswith("MIB") or s.endswith("MB"):
        return int(float(s[:-3] if s.endswith("MIB") else s[:-2]) * MiB)
    return int(s)


def get_volume_fs(path: Path) -> str:
    """Xác định hệ thống file (NTFS, exFAT, FAT32...) của ổ đĩa đích."""
    if sys.platform != "win32":
        return "POSIX"
    root = path.anchor
    if not root:
        root = os.path.abspath(str(path))[:3]
    fs_buf = ctypes.create_unicode_buffer(260)
    vol_buf = ctypes.create_unicode_buffer(260)
    res = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root),
        vol_buf,
        260,
        None,
        None,
        None,
        fs_buf,
        260,
    )
    if res:
        return fs_buf.value
    return "UNKNOWN"


def generate_test_file(path: Path, size_bytes: int) -> str:
    """Tạo file kiểm thử ngẫu nhiên và tính SHA-256."""
    print(f"[*] Đang tạo file mẫu {size_bytes / MiB:.1f} MiB tại {path}...")
    chunk_size = 4 * MiB
    hasher = hashlib.sha256()
    written = 0
    with open(path, "wb") as f:
        while written < size_bytes:
            batch = min(chunk_size, size_bytes - written)
            data = os.urandom(batch)
            hasher.update(data)
            f.write(data)
            written += batch
    digest = hasher.hexdigest()
    print(f"[+] File đã tạo xong. SHA-256: {digest}")
    return digest


def hash_file(path: Path) -> str:
    """Tính mã SHA-256 của file."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4 * MiB):
            hasher.update(chunk)
    return hasher.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Đo đạc kiểm tra ghi file lên ổ đích chậm (D4)")
    parser.add_argument(
        "--dest-dir",
        required=True,
        help="Thư mục nhận trên ổ đĩa đích (ví dụ: E:\\teleport_recv)",
    )
    parser.add_argument("--size", default="1GiB", help="Dung lượng file kiểm thử (mặc định: 1GiB)")
    parser.add_argument("--runs", type=int, default=3, help="Số lượt kiểm thử (mặc định: 3)")
    parser.add_argument("--port", type=int, default=47474, help="Cổng mạng nhận (mặc định: 47474)")
    parser.add_argument("--work-dir", default="temp_bench_d4", help="Thư mục làm việc tạm thời")
    args = parser.parse_args()

    dest_dir = Path(args.dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    size_bytes = parse_size(args.size)

    # 1. Kiểm tra hệ thống file
    fs_name = get_volume_fs(dest_dir)
    free_bytes = shutil.disk_usage(dest_dir).free
    print(f"[*] Ổ đĩa đích: {dest_dir.anchor} | Hệ thống file: {fs_name} | Trống: {free_bytes / GiB:.2f} GiB")

    if fs_name.upper() == "FAT32" and size_bytes >= 4 * GiB:
        print("[!] LỖI: Hệ thống file FAT32 không hỗ trợ file >= 4 GiB!")
        print("    Vui lòng định dạng lại ổ đĩa sang exFAT hoặc NTFS theo yêu cầu benchmark D4.")
        sys.exit(1)

    if free_bytes < size_bytes * 1.5:
        print(
            f"[!] CẢNH BÁO: Dung lượng trống ({free_bytes / GiB:.2f} GiB) "
            f"không đủ chứa dữ liệu kiểm thử ({size_bytes / GiB:.2f} GiB)!"
        )
        sys.exit(1)

    # 2. Tạo file mẫu
    test_file = work_dir / f"test_disk_{args.size}.bin"
    expected_hash = generate_test_file(test_file, size_bytes)

    print(f"\n[*] Bắt đầu {args.runs} lượt truyền vào ổ đích chậm...")
    results = []

    for r in range(1, args.runs + 1):
        print(f"\n--- Lượt {r}/{args.runs} ---")
        recv_home = work_dir / f"home_recv_{r}"
        send_home = work_dir / f"home_send_{r}"
        recv_home.mkdir(parents=True, exist_ok=True)
        send_home.mkdir(parents=True, exist_ok=True)

        recv_cmd = [
            sys.executable,
            "-m",
            "teleport.cli",
            "receive",
            "--yes",
            "--dir",
            str(dest_dir),
            "--port",
            str(args.port),
            "--json",
        ]
        env_recv = os.environ.copy()
        env_recv["TELEPORT_HOME"] = str(recv_home)

        recv_proc = subprocess.Popen(
            recv_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env_recv,
        )

        time.sleep(2)  # Chờ receiver khởi động

        send_cmd = [
            sys.executable,
            "-m",
            "teleport.cli",
            "send",
            str(test_file),
            "--to",
            "local",
            "--address",
            f"127.0.0.1:{args.port}",
            "--json",
        ]
        env_send = os.environ.copy()
        env_send["TELEPORT_HOME"] = str(send_home)

        t_start = time.perf_counter()
        send_proc = subprocess.Popen(
            send_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env_send,
        )

        peak_rss_mb = 0.0
        recv_ps = psutil.Process(recv_proc.pid) if psutil else None

        while send_proc.poll() is None:
            if recv_ps:
                with contextlib.suppress(Exception):
                    mem = recv_ps.memory_info()
                    rss_mb = mem.rss / MiB
                    if rss_mb > peak_rss_mb:
                        peak_rss_mb = rss_mb
            time.sleep(0.05)

        t_end = time.perf_counter()
        duration = max(0.001, t_end - t_start)
        throughput_mb_s = (size_bytes / 1_000_000) / duration

        # Dừng receiver
        recv_proc.terminate()
        try:
            recv_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            recv_proc.kill()

        # 3. Kiểm tra tính toàn vẹn
        received_file = dest_dir / test_file.name
        if not received_file.exists():
            print(f"[!] Lượt {r}: Không tìm thấy file nhận tại {received_file}")
            continue

        print("[*] Đang kiểm tra mã SHA-256 của file nhận...")
        actual_hash = hash_file(received_file)
        hash_ok = actual_hash == expected_hash

        # 4. Kiểm tra dọn dẹp file tạm
        teleport_temp_dir = dest_dir / ".teleport"
        temp_files = []
        if teleport_temp_dir.exists():
            temp_files = [f.name for f in teleport_temp_dir.rglob("*.part")]

        print(f"    Thời gian: {duration:.2f} s | Tốc độ ghi: {throughput_mb_s:.2f} MB/s")
        print(
            f"    RAM đỉnh nhận: {peak_rss_mb:.1f} MiB | "
            f"Hash khớp: {hash_ok} | File tạm sót lại: {len(temp_files)}"
        )

        results.append(
            {
                "run": r,
                "duration": duration,
                "speed": throughput_mb_s,
                "peak_rss": peak_rss_mb,
                "hash_ok": hash_ok,
                "part_leftover": len(temp_files),
            }
        )

        # Dọn file nhận để lượt sau chạy độc lập
        with contextlib.suppress(Exception):
            received_file.unlink()

    print("\n" + "=" * 60)
    print("KẾT QUẢ ĐO Ổ ĐÍCH CHẬM (D4 / mvp N3)")
    print("=" * 60)

    print(f"Ổ đĩa: {dest_dir.anchor} ({fs_name}) | Cỡ file: {args.size}")
    for res in results:
        print(
            f"Lượt {res['run']}: {res['duration']:.2f}s | {res['speed']:.2f} MB/s | "
            f"RAM đỉnh: {res['peak_rss']:.1f} MiB | Khớp SHA-256: {res['hash_ok']} | "
            f"File rác: {res['part_leftover']}"
        )


if __name__ == "__main__":
    main()
