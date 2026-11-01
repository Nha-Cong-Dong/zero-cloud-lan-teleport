"""Khởi động cùng Windows (mvp F10): giá trị `Teleport` trong khóa Run của người dùng (HKCU).

Không Qt, không node: hàm đồng bộ, gọi thẳng trên thread giao diện (HKCU nằm sẵn trong bộ nhớ).
Hợp đồng với bộ cài ở docs/architecture.md mục 4 (“Bộ cài Windows”, gạch “Tự khởi động”).

- **Lệnh** (`launch_command`, ghép bằng `subprocess.list2cmdline`): bản đóng gói
  `"<exe>" --background --config-dir "<D>"`; bản mã nguồn `"<thư mục python>\\pythonw.exe" -m
  teleport.ui --background --config-dir "<D>"` (không có `pythonw.exe` cạnh `sys.executable` thì
  không hỗ trợ). `D` luôn tuyệt đối, lấy bằng `os.path.abspath` như `ui/app.py`.
- **Một giá trị Run cho mọi thư mục cấu hình**: Windows chỉ có một giá trị `Teleport`, nên bật ở
  bản/thư mục cấu hình này thì ghi đè giá trị của bản khác.
- **“Của bản này”**: kiểu `REG_SZ` hay `REG_EXPAND_SZ` (mở biến môi trường trước), tách tham số
  như Windows (`CommandLineToArgvW` trên win32, bộ tách cùng luật ở nơi khác) rồi so **từng tham
  số** với `launch_command` sau `normpath` + `normcase` của Windows (không phân biệt hoa thường,
  `/` như `\\`). Khác → `OTHER` (bản cài khác, bản zip đã dời chỗ, thư mục cấu hình khác).
- **BLOCKED**: giá trị Run của bản này nhưng người dùng tắt Teleport trong danh sách ứng dụng
  khởi động của Windows: giá trị `Teleport` trong `StartupApproved\\Run` có byte đầu lẻ (định
  dạng không có tài liệu: chỉ đọc byte đầu, và chỉ xóa, không bao giờ tự ghi).
- `enable` ghi `REG_SZ`, rồi xóa dấu chặn ở `StartupApproved` nếu có. `disable` chỉ xóa giá trị
  Run khi là của bản này (và xóa luôn giá trị `StartupApproved`); của bản khác thì không đụng.
- Registry thắng: giao diện đối chiếu `settings.autostart` theo `state()`, không bao giờ tự ghi
  registry theo `settings.json`.

Seam cấp module, đọc lúc gọi: `registry` (`WinRegistry` trên win32, None nơi khác; test thay bằng
bản giả và không bao giờ ghi registry thật), `_frozen`, `_executable`. Lỗi registry (`OSError`)
thành `AutostartError` với `__cause__` là lỗi gốc; log chỉ ghi tên loại lỗi (mvp N9), không
đường dẫn, không dữ liệu của giá trị.
"""

from __future__ import annotations

import logging
import ntpath
import os
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Protocol

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "Teleport"

# Mã kiểu giá trị của registry (trùng `winreg.REG_*`, khai lại để chạy được ngoài Windows).
REG_SZ = 1
REG_EXPAND_SZ = 2
REG_BINARY = 3

PYTHONW = "pythonw.exe"

log = logging.getLogger(__name__)

registry: Registry | None  # seam, đọc lúc gọi; gán ở dưới theo hệ điều hành


class AutostartState(StrEnum):
    ON = "on"  # giá trị Run của bản này, Windows không chặn
    OFF = "off"  # không có giá trị Run
    BLOCKED = "blocked"  # của bản này nhưng bị tắt trong danh sách ứng dụng khởi động
    OTHER = "other"  # có giá trị Run nhưng của bản/thư mục cấu hình khác
    UNSUPPORTED = "unsupported"  # không phải Windows, hay bản mã nguồn thiếu `pythonw.exe`


class AutostartError(Exception):
    """Đọc/ghi registry lỗi; `__cause__` là `OSError` gốc."""


class AutostartUnsupported(AutostartError):
    """Máy/bản này không tự khởi động được (`supported()` False)."""


class Registry(Protocol):
    """Giá trị trong HKCU. Mọi lỗi là `OSError`."""

    def get(self, key: str, name: str) -> tuple[object, int] | None:
        """(dữ liệu, kiểu) của giá trị `name` trong `key`; không có khóa hay giá trị → None."""
        ...

    def set(self, key: str, name: str, value: str, kind: int) -> None:
        """Ghi giá trị (tạo khóa nếu chưa có)."""
        ...

    def delete(self, key: str, name: str) -> bool:
        """Xóa giá trị; False nếu không có."""
        ...


if sys.platform == "win32":
    import ctypes
    import winreg
    from ctypes import wintypes

    class WinRegistry:
        """`Registry` thật trên `HKEY_CURRENT_USER`."""

        def get(self, key: str, name: str) -> tuple[object, int] | None:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_QUERY_VALUE) as k:
                    value, kind = winreg.QueryValueEx(k, name)
            except FileNotFoundError:
                return None
            return value, kind

        def set(self, key: str, name: str, value: str, kind: int) -> None:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, name, 0, kind, value)

        def delete(self, key: str, name: str) -> bool:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, name)
            except FileNotFoundError:
                return False
            return True

    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    _shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL

    def _split_command(text: str) -> list[str]:
        """Tách dòng lệnh đúng như Windows (`CommandLineToArgvW`); lỗi → []."""
        if not text.strip():
            return []  # chuỗi rỗng: hàm của Windows trả đường dẫn exe đang chạy
        count = ctypes.c_int(0)
        argv = _shell32.CommandLineToArgvW(text, ctypes.byref(count))
        if not argv:
            return []
        try:
            return [argv[i] for i in range(count.value)]
        finally:
            _kernel32.LocalFree(ctypes.cast(argv, wintypes.HLOCAL))

    def _expand(text: str) -> str:
        return winreg.ExpandEnvironmentStrings(text)

    registry = WinRegistry()
else:

    def _split_command(text: str) -> list[str]:
        """Tách dòng lệnh theo luật của `CommandLineToArgvW` (dùng khi test chạy ngoài Windows)."""
        return _split_windows(text)

    def _expand(text: str) -> str:
        return ntpath.expandvars(text)

    registry = None


def _frozen() -> bool:
    """Seam: đang chạy bản đóng gói (PyInstaller)."""
    return bool(getattr(sys, "frozen", False))


def _executable() -> str:
    """Seam: `sys.executable`."""
    return sys.executable


def supported() -> bool:
    """Có registry và dựng được lệnh khởi động (bản mã nguồn cần `pythonw.exe`)."""
    return registry is not None and _program() is not None


def launch_command(config_dir: Path) -> str | None:
    """Dòng lệnh ghi vào giá trị Run cho `config_dir`; None nếu bản này không dựng được lệnh."""
    args = _launch_args(config_dir)
    return None if args is None else subprocess.list2cmdline(args)


def state(config_dir: Path) -> AutostartState:
    """Trạng thái giá trị Run so với bản này và `config_dir`. Đọc registry lỗi → `AutostartError`."""
    reg = registry
    args = _launch_args(config_dir)
    if reg is None or args is None:
        return AutostartState.UNSUPPORTED
    try:
        current = reg.get(RUN_KEY, VALUE_NAME)
        if current is None:
            return AutostartState.OFF
        if not _is_ours(current, args):
            return AutostartState.OTHER
        approved = reg.get(STARTUP_APPROVED_KEY, VALUE_NAME)
    except OSError as e:
        raise _error("đọc", e) from e
    return AutostartState.BLOCKED if _disabled(approved) else AutostartState.ON


def enable(config_dir: Path) -> None:
    """Ghi giá trị Run của bản này (`REG_SZ`), gỡ dấu chặn ở `StartupApproved` nếu có.

    Không hỗ trợ → `AutostartUnsupported`; lỗi registry → `AutostartError`.
    """
    reg = registry
    command = launch_command(config_dir)
    if reg is None or command is None:
        raise AutostartUnsupported("máy này không hỗ trợ khởi động cùng Windows")
    try:
        reg.set(RUN_KEY, VALUE_NAME, command, REG_SZ)
        if _disabled(reg.get(STARTUP_APPROVED_KEY, VALUE_NAME)):
            reg.delete(STARTUP_APPROVED_KEY, VALUE_NAME)
    except OSError as e:
        raise _error("ghi", e) from e


def disable(config_dir: Path) -> None:
    """Xóa giá trị Run nếu là của bản này (kèm giá trị `StartupApproved`); của bản khác thì giữ.

    Không hỗ trợ → `AutostartUnsupported`; lỗi registry → `AutostartError`.
    """
    reg = registry
    args = _launch_args(config_dir)
    if reg is None or args is None:
        raise AutostartUnsupported("máy này không hỗ trợ khởi động cùng Windows")
    try:
        current = reg.get(RUN_KEY, VALUE_NAME)
        if current is None or not _is_ours(current, args):
            return
        reg.delete(RUN_KEY, VALUE_NAME)
        reg.delete(STARTUP_APPROVED_KEY, VALUE_NAME)
    except OSError as e:
        raise _error("xóa", e) from e


def _program() -> list[str] | None:
    """Phần đầu của lệnh (chương trình và tham số chạy giao diện), chưa có cờ; None nếu không dựng được."""
    executable = _executable()
    if not executable:
        return None
    if _frozen():
        return [os.path.abspath(executable)]
    pythonw = os.path.join(os.path.dirname(os.path.abspath(executable)), PYTHONW)
    if not os.path.isfile(pythonw):
        return None
    return [pythonw, "-m", "teleport.ui"]


def _launch_args(config_dir: Path) -> list[str] | None:
    program = _program()
    if program is None:
        return None
    return [*program, "--background", "--config-dir", os.path.abspath(config_dir)]


def _part_key(text: str) -> str:
    """Khóa so một tham số: `normpath` + `normcase` của Windows (cờ không đổi trừ chữ hoa/thường)."""
    return ntpath.normcase(ntpath.normpath(text))


def _is_ours(current: tuple[object, int], args: list[str]) -> bool:
    """Giá trị Run `current` có đúng là lệnh `args` của bản này không (so từng tham số)."""
    value, kind = current
    if kind not in (REG_SZ, REG_EXPAND_SZ) or not isinstance(value, str):
        return False
    text = _expand(value) if kind == REG_EXPAND_SZ else value
    parts = _split_command(text.strip())
    if len(parts) != len(args):
        return False
    return [_part_key(p) for p in parts] == [_part_key(a) for a in args]


def _disabled(approved: tuple[object, int] | None) -> bool:
    """Giá trị `StartupApproved` đánh dấu tắt (byte đầu lẻ)."""
    if approved is None:
        return False
    value = approved[0]
    return isinstance(value, bytes | bytearray) and len(value) > 0 and value[0] % 2 == 1


def _error(action: str, e: OSError) -> AutostartError:
    log.warning("không %s được registry khởi động cùng Windows: %s", action, type(e).__name__)
    return AutostartError(f"không {action} được registry khởi động cùng Windows")


def _split_windows(text: str) -> list[str]:
    """Tách dòng lệnh theo luật của `CommandLineToArgvW`.

    Tham số đầu (chương trình): trong ngoặc kép thì tới ngoặc kép kế, không thì tới khoảng trắng;
    không có thoát. Các tham số sau: `2n` gạch chéo ngược trước `"` → `n` gạch và `"` đóng/mở
    ngoặc; `2n+1` → `n` gạch và `"` thường; `""` trong ngoặc → `"` thường và đóng ngoặc; gạch không đứng trước
    `"` giữ nguyên.
    """
    args: list[str] = []
    i, n = 0, len(text)
    if n == 0:
        return args
    if text[0] == '"':
        end = text.find('"', 1)
        end = n if end < 0 else end
        args.append(text[1:end])
        i = end + 1
    else:
        while i < n and text[i] not in " \t":
            i += 1
        args.append(text[:i])
    while True:
        while i < n and text[i] in " \t":
            i += 1
        if i >= n:
            return args
        current: list[str] = []
        quoted = False
        while i < n and (quoted or text[i] not in " \t"):
            ch = text[i]
            if ch == "\\":
                start = i
                while i < n and text[i] == "\\":
                    i += 1
                slashes = i - start
                if i < n and text[i] == '"':
                    current.append("\\" * (slashes // 2))
                    if slashes % 2:
                        current.append('"')
                        i += 1
                else:
                    current.append("\\" * slashes)
                continue
            if ch == '"':
                if quoted and i + 1 < n and text[i + 1] == '"':
                    current.append('"')
                    quoted = False
                    i += 2
                    continue
                quoted = not quoted
                i += 1
                continue
            current.append(ch)
            i += 1
        args.append("".join(current))
