"""Định nghĩa cây tham số dòng lệnh (`argparse`) cho `teleport`."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from teleport import __version__
from teleport.app.node import MAX_SCAN_TIMEOUT, SCAN_TIMEOUT
from teleport.cli.messages import _ANY_HOST, _valid_host
from teleport.core.messages import MAX_OFFER_FILES
from teleport.paths import ENV_HOME


def _port_type(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"cổng phải là số nguyên 0–65535: {text!r}") from None
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"cổng phải là số nguyên 0–65535: {text!r}")
    return value


def _add_node_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="in sự kiện dạng JSON Lines")
    parser.add_argument(
        "--name", help="tên thiết bị cho lần chạy này (mặc định: tên đã lưu, chưa có thì tên máy)"
    )
    parser.add_argument(
        "--stealth",
        action="store_true",
        help="chế độ ẩn danh: không phát tên thật máy tính ra toàn mạng WiFi",
    )
    # Ẩn: chỉ để test bind 127.0.0.1 và cổng 0 (tránh hộp thoại tường lửa Windows).
    parser.add_argument("--host", default=_ANY_HOST, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=_port_type, default=None, help=argparse.SUPPRESS)
    # Ẩn: chỉ duyệt/quảng bá mDNS trên các card này (test dùng 127.0.0.1).
    parser.add_argument("--mdns-interface", action="append", type=_ipv4_type, help=argparse.SUPPRESS)


def _ipv4_type(text: str) -> str:
    if not _valid_host(text):
        raise argparse.ArgumentTypeError(f"phải là địa chỉ IPv4 dạng chấm: {text!r}")
    return text


def _timeout_type(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"thời gian phải là số giây: {text!r}") from None
    if not math.isfinite(value) or not 0 < value <= MAX_SCAN_TIMEOUT:
        raise argparse.ArgumentTypeError(
            f"thời gian phải trong khoảng (0, {MAX_SCAN_TIMEOUT:g}] giây: {text!r}"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    import teleport.cli as _cli

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
        handler=_cli.cmd_identity
    )

    list_ = sub.add_parser("list", help="liệt kê các máy Teleport trong mạng")
    list_.add_argument(
        "--timeout",
        type=_timeout_type,
        default=SCAN_TIMEOUT,
        metavar="GIÂY",
        help=f"thời gian tìm (mặc định {SCAN_TIMEOUT:g} giây)",
    )
    list_.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    list_.add_argument("--mdns-interface", action="append", type=_ipv4_type, help=argparse.SUPPRESS)
    list_.set_defaults(handler=_cli.cmd_list)

    listen = sub.add_parser("listen", help="chờ ghép đôi và giữ kết nối với các máy đã ghép đôi")
    _add_node_options(listen)
    # Ẩn: địa chỉ quảng bá cố định (test), không theo dõi đổi địa chỉ.
    listen.add_argument("--advertise-address", action="append", type=_ipv4_type, help=argparse.SUPPRESS)
    listen.set_defaults(handler=_cli.cmd_listen)

    pair = sub.add_parser("pair", help="ghép đôi với máy khác theo tên, mã thiết bị hoặc địa chỉ IP")
    pair.add_argument(
        "address",
        metavar="<tên | tiền tố mã | ip[:cổng]>",
        help="máy kia như `teleport list` hiện, hoặc địa chỉ IP, ví dụ 192.168.1.5",
    )
    _add_node_options(pair)
    pair.set_defaults(handler=_cli.cmd_pair)

    send = sub.add_parser("send", help="gửi file hoặc thư mục tới một thiết bị đã ghép đôi")
    send.add_argument(
        "files",
        nargs="+",
        metavar="<đường dẫn>",
        help=f"các file hoặc thư mục cần gửi (tối đa {MAX_OFFER_FILES})",
    )
    send.add_argument(
        "--to",
        action="append",
        required=True,
        metavar="<thiết bị>",
        help="tên hoặc tiền tố mã thiết bị đã ghép đôi (có thể lặp lại hoặc cách nhau bằng dấu phẩy)",
    )
    send.add_argument(
        "--address", metavar="<ip[:cổng]>", help="địa chỉ của máy nhận, khi mạng chặn mDNS (vẫn cần --to)"
    )
    send.add_argument(
        "--limit-rate",
        metavar="<tốc độ>",
        help=(
            "giới hạn tốc độ gửi (byte/giây, hệ nhị phân), ví dụ 20M hoặc 512K; tối thiểu 64K "
            "(mặc định: theo cài đặt)"
        ),
    )
    send.add_argument(
        "--no-delta",
        action="store_true",
        help="tắt chế độ truyền thông minh phần thay đổi (gửi đầy đủ mọi khối)",
    )
    _add_node_options(send)
    send.set_defaults(handler=_cli.cmd_send)

    receive = sub.add_parser("receive", help="nhận file từ các thiết bị đã ghép đôi")
    receive.add_argument(
        "--dir",
        metavar="<thư mục>",
        help=(
            "thư mục lưu file nhận (mặc định: thư mục đã lưu trong cài đặt, chưa có thì Teleport "
            "trong thư mục Tải về)"
        ),
    )
    receive.add_argument(
        "--yes", action="store_true", help="tự nhận mọi file từ mọi thiết bị đã ghép đôi, không hỏi"
    )
    receive.add_argument(
        "--no-delta",
        action="store_true",
        help="tắt chế độ dò và tái sử dụng phần không đổi từ tệp cũ",
    )
    _add_node_options(receive)
    receive.add_argument("--advertise-address", action="append", type=_ipv4_type, help=argparse.SUPPRESS)
    receive.set_defaults(handler=_cli.cmd_receive)

    unpair = sub.add_parser("unpair", help="hủy ghép đôi với một thiết bị (khi Teleport không chạy)")
    unpair.add_argument("device", metavar="<thiết bị>", help="tên hoặc tiền tố mã thiết bị (≥ 4 ký tự)")
    unpair.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    unpair.set_defaults(handler=_cli.cmd_unpair)

    transfers = sub.add_parser("transfers", help="xem và hủy các lượt truyền file đang dở")
    transfers_sub = transfers.add_subparsers(dest="transfers_command", required=True, metavar="<thao tác>")

    transfers_list = transfers_sub.add_parser("list", help="liệt kê các lượt truyền đang dở")
    transfers_list.add_argument(
        "--dir",
        metavar="<thư mục>",
        help="thư mục nhận cần kiểm tra (khi Teleport không chạy)",
    )
    transfers_list.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    transfers_list.set_defaults(handler=_cli.cmd_transfers_list, _parser=transfers_list)

    transfers_cancel = transfers_sub.add_parser("cancel", help="hủy một hoặc toàn bộ lượt truyền đang dở")
    transfers_cancel.add_argument(
        "transfer",
        nargs="?",
        metavar="<mã lượt>",
        help="tiền tố mã lượt truyền (tối thiểu 4 ký tự hex)",
    )
    transfers_cancel.add_argument(
        "--all",
        action="store_true",
        dest="cancel_all",
        help="hủy toàn bộ lượt truyền đang dở",
    )
    transfers_cancel.add_argument(
        "--dir",
        metavar="<thư mục>",
        help="thư mục nhận cần xử lý (khi Teleport không chạy)",
    )
    transfers_cancel.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    transfers_cancel.set_defaults(handler=_cli.cmd_transfers_cancel, _parser=transfers_cancel)

    guest = sub.add_parser("guest", help="mở chế độ khách chia sẻ tệp qua Web QR với điện thoại")
    guest.add_argument(
        "files",
        nargs="*",
        metavar="<file>",
        help="các file trên máy tính muốn chia sẻ cho điện thoại",
    )
    guest.add_argument(
        "--dir",
        dest="receive_dir",
        metavar="<thư mục>",
        help="thư mục lưu tệp nhận từ điện thoại",
    )
    guest.add_argument("--ip", metavar="<ip>", help="chỉ định địa chỉ IP card mạng hiển thị trong mã QR")
    guest.add_argument("--port", type=int, default=0, help="cổng TCP (mặc định ngẫu nhiên)")
    guest.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="thời gian tự đóng khi không hoạt động (giây, mặc định 600)",
    )
    guest.add_argument(
        "--scope",
        choices=("both", "receive_only", "send_only"),
        default="both",
        help="phạm vi quyền của phiên khách (both, receive_only, send_only)",
    )
    guest.add_argument(
        "--one-shot",
        action="store_true",
        help="tự đóng phiên sau đúng một lượt tải lên hoặc tải về",
    )
    guest.add_argument("--wifi-ssid", metavar="<ssid>", help="tạo thêm mã QR Wi-Fi trước mã QR kết nối")
    guest.add_argument(
        "--wifi-password", default="", metavar="<mật khẩu>", help="mật khẩu Wi-Fi cho mã QR Wi-Fi"
    )
    guest.add_argument(
        "--wifi-security",
        choices=("WPA", "WEP", "nopass"),
        default="WPA",
        help="kiểu bảo mật Wi-Fi (WPA, WEP, nopass; mặc định WPA)",
    )
    guest.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    guest.set_defaults(handler=_cli.cmd_guest)

    receipt = sub.add_parser("receipt", help="liệt kê và xác minh biên lai truyền tệp có chữ ký")
    receipt_sub = receipt.add_subparsers(dest="receipt_command", required=True, metavar="<thao tác>")

    receipt_verify = receipt_sub.add_parser("verify", help="xác minh chữ ký và tệp của một biên lai")
    receipt_verify.add_argument("path", metavar="<file>", help="đường dẫn tới tệp .teleport-receipt.json")
    receipt_verify.add_argument("--dir", metavar="<thư mục>", help="thư mục chứa tệp để đối chiếu nội dung")
    receipt_verify.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    receipt_verify.set_defaults(handler=_cli.cmd_receipt_verify, _parser=receipt_verify)

    receipt_list = receipt_sub.add_parser("list", help="liệt kê các biên lai đã lưu")
    receipt_list.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    receipt_list.set_defaults(handler=_cli.cmd_receipt_list, _parser=receipt_list)

    routing_p = sub.add_parser("routing", help="quản lý quy tắc phân loại thư mục nhận tự động")
    routing_sub = routing_p.add_subparsers(dest="routing_command", required=True, metavar="<thao tác>")

    routing_list = routing_sub.add_parser("list", help="liệt kê quy tắc phân loại thư mục nhận")
    routing_list.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    routing_list.set_defaults(handler=_cli.cmd_routing_list)

    routing_add = routing_sub.add_parser("add", help="thêm quy tắc phân loại thư mục nhận mới")
    routing_add.add_argument("--name", required=True, metavar="<tên>", help="tên gợi nhớ cho quy tắc")
    routing_add.add_argument(
        "--dest", required=True, metavar="<thư mục>", help="thư mục đích khi khớp quy tắc"
    )
    routing_add.add_argument(
        "--from",
        dest="from_device",
        default=None,
        metavar="<thiết bị>",
        help="chỉ áp dụng khi nhận từ thiết bị này",
    )
    routing_add.add_argument(
        "--pattern",
        action="append",
        metavar="<mẫu>",
        help="mẫu tên tệp (ví dụ *.mp4, *.pdf); lặp lại hoặc cách nhau bằng dấu phẩy",
    )
    routing_add.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    routing_add.set_defaults(handler=_cli.cmd_routing_add)

    routing_remove = routing_sub.add_parser("remove", help="xóa một quy tắc phân loại theo mã")
    routing_remove.add_argument("rule_id", metavar="<mã>", help="mã hoặc tiền tố mã quy tắc")
    routing_remove.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    routing_remove.set_defaults(handler=_cli.cmd_routing_remove)

    watch_p = sub.add_parser("watch", help="quản lý cổng thư mục đẩy tự động một chiều")
    watch_sub = watch_p.add_subparsers(dest="watch_command", required=True, metavar="<thao tác>")

    watch_list = watch_sub.add_parser("list", help="liệt kê các cổng đẩy tự động")
    watch_list.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    watch_list.set_defaults(handler=_cli.cmd_watch_list)

    watch_add = watch_sub.add_parser("add", help="thêm cổng thư mục đẩy tự động mới")
    watch_add.add_argument("--name", required=True, metavar="<tên>", help="tên gợi nhớ cho cổng đẩy")
    watch_add.add_argument("--dir", required=True, metavar="<thư mục>", help="thư mục nguồn cần theo dõi")
    watch_add.add_argument("--to", required=True, metavar="<thiết bị>", help="thiết bị tin cậy đích")
    watch_add.add_argument(
        "--pattern",
        action="append",
        metavar="<mẫu>",
        help="lọc theo mẫu tên tệp (ví dụ *.mp4, *.apk); bỏ trống để gửi mọi tệp mới",
    )
    watch_add.add_argument("--recursive", action="store_true", help="theo dõi cả thư mục con bên trong")
    watch_add.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    watch_add.set_defaults(handler=_cli.cmd_watch_add)

    watch_remove = watch_sub.add_parser("remove", help="xóa một cổng đẩy tự động theo mã")
    watch_remove.add_argument("portal_id", metavar="<mã>", help="mã hoặc tiền tố mã cổng đẩy")
    watch_remove.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    watch_remove.set_defaults(handler=_cli.cmd_watch_remove)

    return parser
