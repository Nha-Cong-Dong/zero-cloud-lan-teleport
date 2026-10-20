"""Giao diện dòng lệnh: `teleport <lệnh>`.

Mỗi lệnh là một hàm `cmd_<tên>(args) -> int` (mã thoát). Lệnh mới được thêm
theo từng giai đoạn: `list`, `listen`, `pair`, `unpair`, `send`, `receive`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from teleport import __version__
from teleport.paths import ENV_HOME, config_dir
from teleport.trust.identity import IdentityError, format_fingerprint, load_or_create_identity


def cmd_identity(args: argparse.Namespace) -> int:
    directory = args.config_dir or config_dir()
    identity = load_or_create_identity(directory)
    print(f"Mã thiết bị:   {identity.device_id}")
    print(f"Pin ngắn:      {identity.short_fingerprint}")
    print(f"Fingerprint:   {format_fingerprint(identity.fingerprint)}")
    print(f"Thư mục:       {directory}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="teleport",
        description="Chuyển file và clipboard trong mạng LAN, không qua cloud.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config-dir",
        type=Path,
        help=f"thư mục cấu hình (mặc định theo hệ điều hành, hoặc biến môi trường {ENV_HOME})",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<lệnh>")
    sub.add_parser("identity", help="hiện mã thiết bị và fingerprint của máy này").set_defaults(
        handler=cmd_identity
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Ép UTF-8 để tiếng Việt hiển thị đúng cả khi đầu ra bị chuyển hướng (pipe).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except IdentityError as e:
        # Lỗi người dùng tự xử lý được: in thông báo, không in traceback.
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1
