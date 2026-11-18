"""Công cụ ký số và xác minh chữ ký số cho tệp thực thi / bộ cài Windows.

Tìm `signtool.exe` trong Windows Kits 10 trên runner hoặc máy phát triển.
Khi chạy trên CI với cờ `--if-configured`:
- Nếu có biến môi trường `WINDOWS_CERT_PFX_BASE64` (và tùy chọn
  `WINDOWS_CERT_PASSWORD`, `WINDOWS_TIMESTAMP_URL`), giải mã chứng chỉ PFX ra
  tệp tạm trong bộ nhớ tạm hệ điều hành, ký toàn bộ đường dẫn chỉ định bằng
  `signtool sign /fd SHA256 /tr <timestamp> /td SHA256`, rồi xóa ngay tệp PFX.
- Nếu chưa cấu hình chứng chỉ tự ký cục bộ (ví dụ dùng SignPath Foundation ở bước
  nộp artifact riêng sau khi CI build xong), thoát sạch với mã 0 để không chặn
  quy trình build.

Ví dụ:
    python packaging/signing/sign_windows.py sign --if-configured dist/teleport/teleport.exe
    python packaging/signing/sign_windows.py verify installer/teleport-setup-1.2.0-x64.exe
"""

from __future__ import annotations

import argparse
import base64
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_TIMESTAMP_URL = "http://timestamp.digicert.com"


def find_signtool() -> Path | None:
    """Tìm `signtool.exe` trong PATH hoặc thư mục cài đặt Windows Kits 10."""
    on_path = shutil.which("signtool")
    if on_path:
        return Path(on_path)

    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    patterns = [
        str(Path(program_files_x86) / "Windows Kits" / "10" / "bin" / "*" / "x64" / "signtool.exe"),
        str(Path(program_files_x86) / "Windows Kits" / "10" / "bin" / "x64" / "signtool.exe"),
        str(Path(program_files) / "Windows Kits" / "10" / "bin" / "*" / "x64" / "signtool.exe"),
    ]
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(Path(p) for p in glob.glob(pattern))
    if not matches:
        return None
    # Chọn phiên bản SDK mới nhất theo thứ tự từ điển
    return sorted(matches)[-1]


def expand_targets(raw_targets: list[str]) -> list[Path]:
    """Mở rộng danh sách đường dẫn hoặc mẫu glob thành danh sách tệp có thật."""
    resolved: list[Path] = []
    for raw in raw_targets:
        has_magic = any(ch in raw for ch in "*?[")
        if has_magic:
            for item in sorted(glob.glob(raw)):
                p = Path(item)
                if p.is_file():
                    resolved.append(p)
        else:
            p = Path(raw)
            if p.is_file():
                resolved.append(p)
    return resolved


def cmd_sign(targets: list[str], *, if_configured: bool, description: str) -> int:
    files = expand_targets(targets)
    if not files:
        print("Không tìm thấy tệp đích nào để ký.", file=sys.stderr)
        return 0 if if_configured else 1

    pfx_b64 = os.environ.get("WINDOWS_CERT_PFX_BASE64", "").strip()
    if not pfx_b64:
        msg = (
            "Chưa cấu hình WINDOWS_CERT_PFX_BASE64; "
            "bỏ qua bước ký cục bộ (dùng SignPath trên Release nếu có)."
        )
        if if_configured:
            print(msg)
            return 0
        print(msg, file=sys.stderr)
        return 1

    signtool = find_signtool()
    if signtool is None:
        print("Không tìm thấy signtool.exe của Windows SDK.", file=sys.stderr)
        return 1

    password = os.environ.get("WINDOWS_CERT_PASSWORD", "")
    timestamp_url = os.environ.get("WINDOWS_TIMESTAMP_URL", "").strip() or DEFAULT_TIMESTAMP_URL

    try:
        pfx_bytes = base64.b64decode(pfx_b64, validate=True)
    except Exception as exc:
        print(f"WINDOWS_CERT_PFX_BASE64 không hợp lệ: {exc}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="zcl_sign_") as tmp_dir:
        pfx_path = Path(tmp_dir) / "signing.pfx"
        pfx_path.write_bytes(pfx_bytes)
        cmd = [
            str(signtool),
            "sign",
            "/f",
            str(pfx_path),
            "/fd",
            "SHA256",
            "/tr",
            timestamp_url,
            "/td",
            "SHA256",
            "/d",
            description,
        ]
        if password:
            cmd.extend(["/p", password])
        cmd.extend(str(f) for f in files)
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            return proc.returncode

    print(f"Đã ký số {len(files)} tệp bằng signtool ({signtool}).")
    return 0


def cmd_verify(targets: list[str]) -> int:
    files = expand_targets(targets)
    if not files:
        print("Không tìm thấy tệp nào để xác minh chữ ký.", file=sys.stderr)
        return 1

    signtool = find_signtool()
    if signtool is None:
        print("Không tìm thấy signtool.exe để xác minh chữ ký.", file=sys.stderr)
        return 1

    for path in files:
        proc = subprocess.run([str(signtool), "verify", "/pa", "/v", str(path)], check=False)
        if proc.returncode != 0:
            return proc.returncode
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ký số và kiểm tra chữ ký cho tệp phát hành Windows.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sign = sub.add_parser("sign", help="Ký số tệp .exe / .dll bằng signtool.exe")
    p_sign.add_argument("targets", nargs="+", help="Đường dẫn hoặc mẫu glob tới tệp cần ký")
    p_sign.add_argument(
        "--if-configured",
        action="store_true",
        help="Thoát 0 nếu chưa thiết lập biến môi trường chứng chỉ",
    )
    p_sign.add_argument(
        "--description",
        default="ZCL Teleport",
        help="Mô tả hiển thị trong hộp thoại UAC/SmartScreen",
    )

    p_verify = sub.add_parser("verify", help="Xác minh chữ ký Authenticode bằng signtool verify /pa")
    p_verify.add_argument("targets", nargs="+", help="Đường dẫn hoặc mẫu glob tới tệp cần xác minh")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sign":
        return cmd_sign(args.targets, if_configured=args.if_configured, description=args.description)
    return cmd_verify(args.targets)


if __name__ == "__main__":
    raise SystemExit(main())
