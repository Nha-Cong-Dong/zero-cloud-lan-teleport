"""Kiểm tra tình trạng tường lửa và hồ sơ mạng của hệ điều hành (mvp mục 8).

Trên Windows, khi máy tính đang ở chế độ Mạng công cộng (Public Network) hoặc khi tường lửa
Windows chặn các kết nối đến, các máy tính khác trong mạng nội bộ sẽ không thể kết nối tới máy
này. Module này cung cấp các hàm hỗ trợ phát hiện tình huống trên để giao diện hiển thị thông báo
và hướng dẫn người dùng khắc phục.

Không dùng Qt hay event loop: hàm đồng bộ, an toàn khi gọi từ bất kỳ luồng nào.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable
from enum import StrEnum

log = logging.getLogger(__name__)

# Seam cấp module để kiểm thử tự động mà không cần chạy lệnh hệ điều hành thật.
_CommandRunner = Callable[[list[str]], str]


def _default_run_command(cmd: list[str]) -> str:
    """Chạy lệnh dòng lệnh ngầm (không hiện cửa sổ terminal trên Windows) và lấy kết quả."""
    creationflags = 0
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=2.0,
        creationflags=creationflags,
    )
    return proc.stdout or ""


_runner: _CommandRunner = _default_run_command


class FirewallIssue(StrEnum):
    """Vấn đề về tường lửa hoặc hồ sơ mạng được phát hiện."""

    NONE = "none"  # Hoạt động bình thường
    PUBLIC_NETWORK = "public_network"  # Mạng đang là Mạng công cộng (Public)
    BLOCKED = "blocked"  # Có quy tắc chặn kết nối đến của Teleport


def parse_netsh_currentprofile(text: str) -> FirewallIssue:
    """Phân tích kết quả lệnh `netsh advfirewall show currentprofile`."""
    lower = text.lower()
    if "public profile" in lower and "state" in lower:
        for line in text.splitlines():
            line_strip = line.strip().lower()
            if line_strip.startswith("state") and "on" in line_strip:
                return FirewallIssue.PUBLIC_NETWORK
    return FirewallIssue.NONE


def parse_netsh_rule(text: str) -> bool:
    """True nếu tìm thấy quy tắc chặn (Action: Block) cho kết nối đến (Inbound)."""
    lower = text.lower()
    return bool("action" in lower and "block" in lower and ("direction" in lower or "dir" in lower))


def check_firewall_status(runner: _CommandRunner | None = None) -> FirewallIssue:
    """Kiểm tra tình trạng tường lửa trên máy tính hiện tại.

    Chỉ kiểm tra trên Windows; các hệ điều hành khác trả về `NONE`.
    """
    if sys.platform != "win32" and runner is None:
        return FirewallIssue.NONE

    run = runner or _runner
    try:
        # 1. Kiểm tra hồ sơ mạng hiện tại (Public hay Private)
        profile_out = run(["netsh", "advfirewall", "show", "currentprofile"])
        issue = parse_netsh_currentprofile(profile_out)
        if issue is not FirewallIssue.NONE:
            return issue

        # 2. Kiểm tra xem có quy tắc chặn riêng cho Teleport hay không
        rule_out = run(["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "dir=in"])
        # Nếu có quy tắc Teleport và quy tắc đó chặn
        if "teleport" in rule_out.lower() and parse_netsh_rule(rule_out):
            return FirewallIssue.BLOCKED

    except Exception:
        log.debug("khong the kiem tra tuong lua he dieu hanh", exc_info=True)

    return FirewallIssue.NONE
