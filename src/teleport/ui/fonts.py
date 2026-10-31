"""Nạp font điểm nhấn Bricolage Grotesque (SIL OFL 1.1) đóng gói kèm app.

Font nằm ở `teleport/ui/assets/fonts/*.ttf` (bản variable, kèm `OFL.txt`), đọc qua
`importlib.resources` nên chạy được cả từ wheel lẫn bản PyInstaller; không tải gì từ mạng. Font chỉ
dùng cho tiêu đề và đề mục (`theme.font_for` với `TITLE`/`SECTION`); chữ thân luôn là font của hệ
điều hành.

`install()` nạp mọi file `.ttf` bằng `QFontDatabase.addApplicationFontFromData` (sau khi đã có
`QGuiApplication`, gọi trên thread Qt), một lần cho mỗi tiến trình: gọi lại thì trả kết quả lần đầu,
không nạp hai lần. Trả `theme.ACCENT_FAMILY` nếu tên đó có trong `applicationFontFamilies(id)` của
một file đã nạp; không lấy phần tử đầu của danh sách, vì với font variable `qwindows` trả mỗi
instance có tên một mục (19 mục). Lỗi (không đọc được file, Qt từ chối dữ liệu, không thấy tên
family) → DEBUG và trả None, không ném: `font_for` rơi về font hệ thống, giao diện vẫn chạy.

`last_error()` cho `--selftest` (`ui/app.py`) biết lỗi nào là lỗi đóng gói: `MISSING` (không có
hay không đọc được file font) và `REJECTED` (`addApplicationFontFromData` trả −1) làm selftest
thất bại; `NO_FAMILY` (Qt nạp được nhưng không báo tên `ACCENT_FAMILY`) thì không, vì vẫn có font
hệ thống để rơi về.

`missing_glyphs(text, font)` trả các ký tự của `text` mà font chính của `font` không có glyph (dùng
`QRawFont`, không tính font thay thế của hệ điều hành).

`_reset()` là seam cho test: gỡ các font đã nạp (`removeApplicationFont`) và xóa trạng thái để
`install()` nạp lại.
"""

from __future__ import annotations

import logging
from importlib import resources

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QFont, QFontDatabase, QRawFont

from teleport.ui import theme

log = logging.getLogger(__name__)

MISSING = "missing"
"""`last_error()`: không có hay không đọc được file font nào."""
REJECTED = "rejected"
"""`last_error()`: Qt từ chối dữ liệu font (`addApplicationFontFromData` trả −1)."""
NO_FAMILY = "no_family"
"""`last_error()`: đã nạp nhưng không file nào có family `theme.ACCENT_FAMILY`."""

_installed = False
_family: str | None = None
_error: str | None = None
_font_ids: list[int] = []


def _font_data() -> list[tuple[str, bytes]]:
    """(tên file, nội dung) của mọi `.ttf` trong `assets/fonts/`, xếp theo tên."""
    folder = resources.files("teleport.ui").joinpath("assets", "fonts")
    entries = sorted(
        (entry for entry in folder.iterdir() if entry.name.lower().endswith(".ttf") and entry.is_file()),
        key=lambda entry: entry.name,
    )
    return [(entry.name, entry.read_bytes()) for entry in entries]


def install() -> str | None:
    """Nạp font điểm nhấn; trả `theme.ACCENT_FAMILY` hoặc None (xem docstring module)."""
    global _installed, _family, _error
    if _installed:
        return _family
    _installed = True
    try:
        files = _font_data()
    except Exception as e:  # resources/OSError/thư mục thiếu: mọi lỗi đọc đều thành “thiếu file”
        log.debug("không đọc được font điểm nhấn: %s", type(e).__name__)
        _error = MISSING
        return None
    if not files:
        log.debug("không có file font điểm nhấn trong assets/fonts")
        _error = MISSING
        return None
    for name, data in files:
        font_id = QFontDatabase.addApplicationFontFromData(QByteArray(data))
        if font_id < 0:
            log.debug("Qt không nạp được font %s", name)
            _error = REJECTED
            continue
        _font_ids.append(font_id)
        families = QFontDatabase.applicationFontFamilies(font_id)
        if theme.ACCENT_FAMILY in families:
            _family = theme.ACCENT_FAMILY
        else:
            log.debug("font %s không có family %s (có %d family)", name, theme.ACCENT_FAMILY, len(families))
    if _family is not None:
        _error = None
    elif _error is None:
        _error = NO_FAMILY
    return _family


def last_error() -> str | None:
    """Lỗi của lần `install()` (`MISSING`, `REJECTED`, `NO_FAMILY`), None nếu ổn hoặc chưa gọi."""
    return _error


def missing_glyphs(text: str, font: QFont) -> set[str]:
    """Các ký tự của `text` mà font chính của `font` không có glyph."""
    raw = QRawFont.fromFont(font)
    if not raw.isValid():
        return set(text)
    return {ch for ch in text if not raw.supportsCharacter(ord(ch))}


def _reset() -> None:
    """Gỡ font đã nạp và xóa trạng thái để `install()` chạy lại (cho test)."""
    global _installed, _family, _error
    for font_id in _font_ids:
        QFontDatabase.removeApplicationFont(font_id)
    _font_ids.clear()
    _installed = False
    _family = None
    _error = None
