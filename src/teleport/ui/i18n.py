"""Đa ngôn ngữ của giao diện (mvp F11): chuỗi nguồn tiếng Việt, bản dịch `.qm` đóng gói kèm app.

Bản dịch nằm ở `teleport/ui/i18n/teleport_<mã>.qm`, đọc qua `importlib.resources` (chạy được cả
khi cài bằng wheel lẫn bản build). Không có file cho ngôn ngữ đó, hay ngôn ngữ là tiếng Việt (chuỗi
nguồn), thì dùng chuỗi nguồn, chỉ ghi log DEBUG. Đổi ngôn ngữ thì gỡ translator cũ rồi cài cái mới:
Qt gửi `LanguageChange` tới mọi widget, widget gọi `retranslate()` trong `changeEvent`.

Mã ngôn ngữ chỉ nhận dạng `xx` hoặc `xx_YY` (chữ ASCII), để không ghép được đường dẫn lạ.

Trang Cài đặt (spec D2 mục 1.4): `available_languages()` = chuỗi nguồn + mọi file
`i18n/teleport_<mã>.qm` có thật (đọc thư mục lúc gọi, lỗi đọc thì chỉ chuỗi nguồn);
`language_name(code)` = tên tự gọi của ngôn ngữ (không dịch: người không đọc được ngôn ngữ đang hiện
vẫn tìm được ngôn ngữ của mình), mã lạ thì hiện chính mã; `apply_preference(code | None)` áp lựa
chọn đã lưu (None: theo hệ điều hành; mã sai dạng: chuỗi nguồn, DEBUG).
"""

from __future__ import annotations

import logging
import re
from importlib import resources

from PySide6.QtCore import QCoreApplication, QLocale, QTranslator

SOURCE_LANGUAGE = "vi"
_QM_RE = re.compile(r"teleport_([a-z]{2,3}(?:_[A-Z]{2})?)\.qm", re.ASCII)
# Tên tự gọi (endonym) của các ngôn ngữ app có thể có; không qua `tr()`.
_ENDONYMS = {"vi": "Tiếng Việt", "en": "English"}
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
    apply_preference(language)


def apply_preference(code: str | None) -> None:
    """Áp ngôn ngữ người dùng chọn: None theo hệ điều hành; mã sai dạng → chuỗi nguồn (DEBUG)."""
    chosen = code or system_language()
    if not isinstance(chosen, str) or not _CODE_RE.fullmatch(chosen):  # ví dụ locale `C`
        log.debug("ngôn ngữ %r không dùng được, dùng chuỗi nguồn", chosen)
        chosen = SOURCE_LANGUAGE
    set_language(chosen)


def available_languages() -> tuple[str, ...]:
    """Mã các ngôn ngữ có thể chọn: `SOURCE_LANGUAGE` và mọi `teleport_<mã>.qm` có thật, sắp xếp."""
    codes = {SOURCE_LANGUAGE}
    try:
        folder = resources.files("teleport.ui").joinpath("i18n")
        if folder.is_dir():
            for entry in folder.iterdir():
                match = _QM_RE.fullmatch(entry.name)
                if match is not None and entry.is_file():
                    codes.add(match.group(1))
    except (FileNotFoundError, NotADirectoryError, OSError) as e:
        log.debug("không đọc được thư mục bản dịch (%s)", type(e).__name__)
        return (SOURCE_LANGUAGE,)
    return tuple(sorted(codes))


def language_name(code: str) -> str:
    """Tên tự gọi của ngôn ngữ `code` (`vi` → “Tiếng Việt”, `en` → “English”); mã lạ → chính mã."""
    name = _ENDONYMS.get(code)
    if name is None and "_" in code:
        name = _ENDONYMS.get(code.split("_", 1)[0])
        if name is not None:
            return f"{name} ({code.split('_', 1)[1]})"
    return name if name is not None else code


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


def loadable(code: str) -> bool:
    """`teleport_<code>.qm` có thật và `QTranslator` nạp được (cho `--selftest`); không cài gì."""
    resource = resources.files("teleport.ui").joinpath("i18n", f"teleport_{code}.qm")
    try:
        data = resource.read_bytes()
    except OSError:
        return False
    return QTranslator().load(data)


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
