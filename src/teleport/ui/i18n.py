"""Đa ngôn ngữ của giao diện (mvp F11): chuỗi nguồn tiếng Việt, bản dịch `.qm` đóng gói kèm app.

Bản dịch nằm ở `teleport/ui/i18n/teleport_<mã>.qm`, đọc qua `importlib.resources` (chạy được cả
khi cài bằng wheel lẫn bản build). Không có file cho ngôn ngữ đó, hay ngôn ngữ là tiếng Việt (chuỗi
nguồn), thì dùng chuỗi nguồn, chỉ ghi log DEBUG. Đổi ngôn ngữ thì gỡ translator cũ rồi cài cái mới:
Qt gửi `LanguageChange` tới mọi widget, widget gọi `retranslate()` trong `changeEvent`.

Mã ngôn ngữ chỉ nhận dạng `xx` hoặc `xx_YY` (chữ ASCII), để không ghép được đường dẫn lạ.
"""

from __future__ import annotations

import logging
import re
from importlib import resources

from PySide6.QtCore import QCoreApplication, QLocale, QTranslator

SOURCE_LANGUAGE = "vi"
_CODE_RE = re.compile(r"[a-z]{2,3}(_[A-Z]{2})?", re.ASCII)

log = logging.getLogger(__name__)

_translator: QTranslator | None = None
# Dữ liệu `.qm` của translator đang cài: `QTranslator.load(bytes)` không chép, phải giữ cho sống.
_translator_data: dict[int, bytes] = {}
_language = SOURCE_LANGUAGE


def system_language() -> str:
    """Mã ngôn ngữ của hệ điều hành, ví dụ `vi`, `en_US`."""
    return QLocale.system().name()


def install(app: QCoreApplication, language: str | None = None) -> None:
    """Cài bản dịch cho `app`; `language=None`: theo ngôn ngữ hệ điều hành."""
    del app  # translator cài vào QCoreApplication.instance(), chính là `app`
    code = language or system_language()
    if not _CODE_RE.fullmatch(code):  # ví dụ locale `C`: dùng chuỗi nguồn
        log.debug("ngôn ngữ hệ thống %r không dùng được, dùng chuỗi nguồn", code)
        code = SOURCE_LANGUAGE
    set_language(code)


def current_language() -> str:
    """Mã ngôn ngữ đang dùng (`vi` khi đang hiện chuỗi nguồn)."""
    return _language


def set_language(code: str) -> None:
    """Đổi ngôn ngữ; mã sai dạng → ValueError. Không có bản dịch thì dùng chuỗi nguồn."""
    global _translator, _language
    if not _CODE_RE.fullmatch(code):
        raise ValueError(f"mã ngôn ngữ không hợp lệ: {code!r}")
    app = QCoreApplication.instance()
    if app is None:
        raise RuntimeError("cần QCoreApplication trước khi đổi ngôn ngữ")
    if _translator is not None:
        QCoreApplication.removeTranslator(_translator)
        _translator = None
        _translator_data.clear()
    _language = SOURCE_LANGUAGE
    for candidate in _candidates(code):
        if candidate == SOURCE_LANGUAGE:
            return
        translator = _load(candidate)
        if translator is not None:
            QCoreApplication.installTranslator(translator)
            _translator, _language = translator, candidate
            return
    log.debug("không có bản dịch cho %s, dùng chuỗi nguồn", code)


def _candidates(code: str) -> list[str]:
    """`en_US` → [`en_US`, `en`]; `vi` → [`vi`]."""
    base = code.split("_", 1)[0]
    return [code] if base == code else [code, base]


def _load(code: str) -> QTranslator | None:
    resource = resources.files("teleport.ui").joinpath("i18n", f"teleport_{code}.qm")
    try:
        if not resource.is_file():
            return None
        data = resource.read_bytes()
    except OSError as e:
        log.debug("không đọc được bản dịch %s (%s)", code, type(e).__name__)
        return None
    translator = QTranslator(QCoreApplication.instance())
    if not translator.load(data):
        log.debug("bản dịch %s hỏng, bỏ qua", code)
        return None
    _translator_data.clear()
    _translator_data[id(translator)] = data
    return translator
