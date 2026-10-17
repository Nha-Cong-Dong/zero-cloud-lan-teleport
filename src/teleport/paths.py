"""Thư mục lưu dữ liệu của app theo chuẩn của từng hệ điều hành.

Biến môi trường `TELEPORT_HOME` ghi đè thư mục cấu hình. Dùng nó để chạy nhiều
node trên cùng một máy khi phát triển và kiểm thử, mỗi node một danh tính riêng.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_HOME = "TELEPORT_HOME"


def config_dir() -> Path:
    """Nơi lưu danh tính thiết bị, danh sách thiết bị tin cậy và cài đặt."""
    if override := os.environ.get(ENV_HOME):
        return Path(override)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Teleport"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Teleport"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "teleport"


def default_download_dir() -> Path:
    """Thư mục nhận file mặc định; người dùng đổi được trong cài đặt."""
    return Path.home() / "Downloads" / "Teleport"
