"""Kịch bản tự động đo đạc so sánh tốc độ giữa ZCL Teleport và chia sẻ thư mục SMB (mvp N2, phép đo D2).

Theo đặc tả tại docs/technical/benchmark.md mục 5.1:
- Chiều truyền: Máy gửi -> Máy nhận trong cùng mạng nội bộ.
- Dữ liệu: Cùng một file mẫu (1 GiB hoặc 5 GiB).
- Xen kẽ thứ tự: Teleport và SMB chạy xen kẽ, tính trung vị thời gian và tỉ số tốc độ.
- Đầu ra: Bảng markdown tổng hợp sẵn sàng cập nhật vào benchmark.md.

Cách sử dụng:
    python tools/bench_d2_smb.py --teleport-to May-Nhan \\
        --teleport-addr 192.168.1.50:47474 \\
        --smb-dest \\\\192.168.1.50\\share\\bench.bin \\
        --size 1GiB --runs 5
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Đơn vị tính
MiB = 1024 * 1024
GiB = 1024 * MiB


def parse_size(size_str: str) -> int:
    s = size_str.strip().upper()
    if s.endswith("GIB") or s.endswith("GB"):
        return int(float(s[:-3] if s.endswith("GIB") else s[:-2]) * GiB)
    if s.endswith("MIB") or s.endswith("MB"):
        return int(float(s[:-3] if s.endswith("MIB") else s[:-2]) * MiB)
    return int(s)


def generate_test_file(path: Path, size_bytes: int) -> str:
    """Tạo file kiểm thử ngẫu nhiên và trả về hash SHA-256."""
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


def run_teleport_send(
    file_path: Path,
    peer_name: str,
    peer_address: str | None,
) -> tuple[float, float, bool]:
    """Chạy teleport send và đo thời gian (giây), tốc độ (MB/s), thành công hay không."""
    cmd = [
        sys.executable,
        "-m",
        "teleport.cli",
        "send",
        str(file_path),
        "--to",
        peer_name,
        "--json",
    ]
    if peer_address:
        cmd.extend(["--address", peer_address])

    t_start = time.perf_counter()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    ok = False
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            if ev.get("type") == "result" and ev.get("status") in ("done", "verified"):
                ok = True
        except json.JSONDecodeError:
            pass

    proc.wait()
    t_end = time.perf_counter()
    duration = max(0.001, t_end - t_start)
    speed_mb_s = (file_path.stat().st_size / 1_000_000) / duration
    return duration, speed_mb_s, (ok and proc.returncode == 0)


def run_smb_copy(
    src_path: Path,
    smb_dest_path: str,
) -> tuple[float, float, bool]:
    """Chép file qua chia sẻ SMB và đo thời gian (giây), tốc độ (MB/s)."""
    t_start = time.perf_counter()
    dest_path = Path(smb_dest_path)
    ok = False

    # Thử dùng robocopy nếu trên Windows
    if sys.platform == "win32":
        dest_dir = str(dest_path.parent)
        dest_filename = dest_path.name
        cmd = [
            "robocopy",
            str(src_path.parent),
            dest_dir,
            dest_filename,
            "/R:0",
            "/W:0",
            "/NP",
            "/NJH",
            "/NJS",
            "/NFL",
            "/NDL",
        ]
        res = subprocess.run(cmd, capture_output=True)
        # robocopy returncode: 1 = sao chép thành công ít nhất 1 file
        ok = res.returncode in (0, 1)
    else:
        try:
            shutil.copyfile(src_path, dest_path)
            ok = True
        except Exception as e:
            print(f"[!] Lỗi chép SMB: {e}")
            ok = False

    t_end = time.perf_counter()
    duration = max(0.001, t_end - t_start)
    speed_mb_s = (src_path.stat().st_size / 1_000_000) / duration

    # Dọn dẹp file đích trên SMB sau khi đo
    with contextlib.suppress(Exception):
        if dest_path.exists():
            dest_path.unlink()

    return duration, speed_mb_s, ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Đo đạc so sánh hiệu năng Teleport vs SMB (D2)")
    parser.add_argument("--teleport-to", required=True, help="Tên hoặc mã máy nhận của Teleport")
    parser.add_argument(
        "--teleport-addr",
        help="Địa chỉ IP:cổng của máy nhận Teleport (ví dụ: 192.168.1.50:47474)",
    )
    parser.add_argument(
        "--smb-dest",
        required=True,
        help="Đường dẫn file đích trên chia sẻ SMB (ví dụ: \\\\192.168.1.50\\share\\bench.bin)",
    )
    parser.add_argument("--size", default="1GiB", help="Dung lượng file mẫu (mặc định: 1GiB)")
    parser.add_argument("--file", help="Đường dẫn file có sẵn (nếu không tạo file mẫu)")
    parser.add_argument("--runs", type=int, default=5, help="Số lần chạy mỗi phương thức (mặc định: 5)")
    parser.add_argument("--work-dir", default="temp_bench_d2", help="Thư mục chứa file mẫu tạm thời")
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.file:
        test_file = Path(args.file)
    else:
        size_bytes = parse_size(args.size)
        test_file = work_dir / f"bench_{args.size}.bin"
        if not test_file.exists() or test_file.stat().st_size != size_bytes:
            generate_test_file(test_file, size_bytes)

    total_runs = args.runs
    teleport_results: list[tuple[float, float]] = []
    smb_results: list[tuple[float, float]] = []

    print(f"\n[*] Bắt đầu đo đạc {total_runs} lượt xen kẽ giữa Teleport và SMB...")
    print(f"[*] File mẫu: {test_file.name} ({test_file.stat().st_size / MiB:.1f} MiB)")

    for r in range(1, total_runs + 1):
        print(f"\n--- Lượt {r}/{total_runs} ---")
        # Đổi thứ tự xen kẽ: lượt chẵn Teleport trước, lượt lẻ SMB trước
        if r % 2 == 0:
            print("  [1/2] Đang truyền qua Teleport...")
            t_dur, t_spd, t_ok = run_teleport_send(test_file, args.teleport_to, args.teleport_addr)
            print(f"        Teleport: {t_dur:.2f} s ({t_spd:.2f} MB/s) - Thành công: {t_ok}")
            if t_ok:
                teleport_results.append((t_dur, t_spd))
            time.sleep(5)

            print("  [2/2] Đang sao chép qua SMB...")
            s_dur, s_spd, s_ok = run_smb_copy(test_file, args.smb_dest)
            print(f"        SMB:      {s_dur:.2f} s ({s_spd:.2f} MB/s) - Thành công: {s_ok}")
            if s_ok:
                smb_results.append((s_dur, s_spd))
        else:
            print("  [1/2] Đang sao chép qua SMB...")
            s_dur, s_spd, s_ok = run_smb_copy(test_file, args.smb_dest)
            print(f"        SMB:      {s_dur:.2f} s ({s_spd:.2f} MB/s) - Thành công: {s_ok}")
            if s_ok:
                smb_results.append((s_dur, s_spd))
            time.sleep(5)

            print("  [2/2] Đang truyền qua Teleport...")
            t_dur, t_spd, t_ok = run_teleport_send(test_file, args.teleport_to, args.teleport_addr)
            print(f"        Teleport: {t_dur:.2f} s ({t_spd:.2f} MB/s) - Thành công: {t_ok}")
            if t_ok:
                teleport_results.append((t_dur, t_spd))

        time.sleep(5)

    print("\n" + "=" * 60)
    print("KẾT QUẢ TỔNG HỢP (D2: Teleport vs SMB)")
    print("=" * 60)

    if not teleport_results or not smb_results:
        print("[!] Không đủ dữ liệu hợp lệ để tổng hợp.")
        return

    t_speeds = [s for _, s in teleport_results]
    s_speeds = [s for _, s in smb_results]

    t_speeds.sort()
    s_speeds.sort()

    median_t = t_speeds[len(t_speeds) // 2]
    median_s = s_speeds[len(s_speeds) // 2]
    ratio = median_t / median_s if median_s > 0 else 0.0

    print(
        f"Teleport (MB/s): Trung vị = {median_t:.2f} | Nhỏ nhất = {t_speeds[0]:.2f} | "
        f"Lớn nhất = {t_speeds[-1]:.2f}"
    )
    print(
        f"SMB      (MB/s): Trung vị = {median_s:.2f} | Nhỏ nhất = {s_speeds[0]:.2f} | "
        f"Lớn nhất = {s_speeds[-1]:.2f}"
    )
    print(f"Tỉ số tốc độ (Teleport / SMB): {ratio:.2f} ({ratio * 100:.1f}%)")

    # In bảng Markdown
    print("\n### Bảng dữ liệu Markdown cập nhật benchmark.md:")
    print("| Phương thức | Số lượt hợp lệ | Tốc độ trung vị (MB/s) | Tốc độ min (MB/s) | Tốc độ max (MB/s) |")
    print("|---|:---:|:---:|:---:|:---:|")
    print(f"| Teleport | {len(teleport_results)} | {median_t:.2f} | {t_speeds[0]:.2f} | {t_speeds[-1]:.2f} |")
    print(f"| SMB | {len(smb_results)} | {median_s:.2f} | {s_speeds[0]:.2f} | {s_speeds[-1]:.2f} |")
    print(f"\n**Tỉ số tốc độ Teleport / SMB: {ratio:.2f}**")


if __name__ == "__main__":
    main()
