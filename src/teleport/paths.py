"""Thư mục lưu dữ liệu của app theo chuẩn của từng hệ điều hành.

Biến môi trường `TELEPORT_HOME` ghi đè thư mục cấu hình. Dùng nó để chạy nhiều
node trên cùng một máy khi phát triển và kiểm thử, mỗi node một danh tính riêng.

Thư mục nhận mặc định (`default_receive_dir`) là `Teleport` trong thư mục Tải về của người
dùng: Windows hỏi Known Folder `FOLDERID_Downloads` (người dùng có thể đã dời nó sang ổ
khác), Linux đọc `XDG_DOWNLOAD_DIR` trong `user-dirs.dirs`, macOS dùng `~/Downloads`; không
đọc được thì `~/Downloads`. Seam cho test (đọc lúc dùng): `_known_folder_downloads`,
`_xdg_user_dirs_path`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_HOME = "TELEPORT_HOME"

# Kho thiết bị đã ghép đôi trong thư mục cấu hình. Đặt ở đây vì cả `trust.store` (đọc/ghi)
# lẫn `trust.identity` (không tạo danh tính mới khi còn ghép đôi) đều cần tên này.
TRUSTED_FILE = "trusted.json"


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


RECEIVE_DIR_NAME = "Teleport"
_XDG_DOWNLOAD_KEY = "XDG_DOWNLOAD_DIR"
_XDG_HOME_PREFIX = "$HOME"
_MAX_USER_DIRS_BYTES = 64 * 1024  # user-dirs.dirs chỉ vài dòng; file lớn hơn thì bỏ qua
# {374DE290-123F-4565-9164-39C4925E467B}
_FOLDERID_DOWNLOADS = (0x374DE290, 0x123F, 0x4565, (0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))


def default_receive_dir() -> Path:
    """Thư mục nhận file mặc định: `<thư mục Tải về>/Teleport` (xem docstring module).

    Không tạo thư mục; lỗi khi dò thư mục Tải về thì dùng `~/Downloads`.
    """
    downloads: Path | None = None
    try:
        if sys.platform == "win32":
            downloads = _known_folder_downloads()
        elif sys.platform != "darwin":
            downloads = _xdg_download_dir(_xdg_user_dirs_path())
    except (OSError, ValueError, RuntimeError):  # ctypes, Path.home() không xác định được…
        downloads = None
    if downloads is None:
        downloads = Path.home() / "Downloads"
    return downloads / RECEIVE_DIR_NAME


def _known_folder_downloads() -> Path | None:
    """Windows: `SHGetKnownFolderPath(FOLDERID_Downloads)`; lỗi hay không phải Windows → None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class _Guid(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    d1, d2, d3, d4 = _FOLDERID_DOWNLOADS
    guid = _Guid(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))
    buffer = ctypes.c_wchar_p()
    try:
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
        get_path = shell32.SHGetKnownFolderPath
        get_path.argtypes = [
            ctypes.POINTER(_Guid),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        get_path.restype = ctypes.c_long
        result = get_path(ctypes.byref(guid), 0, None, ctypes.byref(buffer))
        try:
            if result != 0 or not buffer.value:
                return None
            return Path(buffer.value)
        finally:
            if buffer:
                ole32.CoTaskMemFree(buffer)
    except (OSError, AttributeError, ValueError):
        return None


def _xdg_user_dirs_path() -> Path:
    """Linux: `$XDG_CONFIG_HOME/user-dirs.dirs` (mặc định `~/.config/user-dirs.dirs`)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "user-dirs.dirs"


def _xdg_download_dir(path: Path) -> Path | None:
    """Đọc `XDG_DOWNLOAD_DIR="$HOME/…"` hoặc `="/…"` trong `user-dirs.dirs`; không có hay sai → None.

    Giá trị trỏ về chính thư mục nhà (xdg-user-dirs coi là “không dùng”) cũng → None.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(_MAX_USER_DIRS_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(raw) > _MAX_USER_DIRS_BYTES:
        return None
    text = raw.decode("utf-8", errors="surrogateescape")
    value: str | None = None
    for line in text.splitlines():
        key, sep, rest = line.strip().partition("=")
        if sep and key.strip() == _XDG_DOWNLOAD_KEY:
            value = rest.strip()
    if value is None or len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return None
    value = _unescape(value[1:-1])
    home = Path.home()
    if value == _XDG_HOME_PREFIX or value.startswith(_XDG_HOME_PREFIX + "/"):
        result = home / value[len(_XDG_HOME_PREFIX) :].lstrip("/")
    elif value.startswith("/"):
        result = Path(value)
    else:
        return None
    if result == home:
        return None
    return result


def _unescape(text: str) -> str:
    """Bỏ dấu gạch ngược thoát ký tự trong giá trị của `user-dirs.dirs` (ví dụ trước `"` hay `$`)."""
    out: list[str] = []
    chars = iter(text)
    for ch in chars:
        out.append(next(chars, "") if ch == "\\" else ch)
    return "".join(out)
