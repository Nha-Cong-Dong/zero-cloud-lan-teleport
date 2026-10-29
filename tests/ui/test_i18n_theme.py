"""i18n (D10, mục 6.7) và theme sáng/tối (mục 6.8) của tầng `ui`.

i18n: mọi chuỗi qua `translate`/`tr` lúc dùng; widget xử lý `LanguageChange` thì dịch lại. Test cài một
translator giả trả `"[x]" + nguồn`: mọi chữ tiếng Việt trên cửa sổ và khay phải có tiền tố. Thiếu
`.qm` → chuỗi nguồn. Theme: tương phản chữ/nền ≥ 4.5 (WCAG AA, mvp N12) ở cả sáng và tối, tính độc
lập với `theme.contrast_ratio`.
"""

from __future__ import annotations

import re
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QTranslator
from PySide6.QtGui import QColor, QPalette

PREFIX = "[x]"
# Chữ cái có dấu của tiếng Việt: chuỗi nguồn (tiếng Việt) gần như luôn có ít nhất một chữ như vậy; dữ
# liệu (đường dẫn tmp, tên máy ASCII) thì không. Chuỗi nào có chữ như vậy mà thiếu tiền tố là chưa dịch.
VIETNAMESE = re.compile(
    r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]", re.IGNORECASE
)


class PrefixTranslator(QTranslator):
    def isEmpty(self):
        # Qt chỉ phát `LanguageChange` khi cài/gỡ một translator không rỗng.
        return False

    def translate(self, context, source_text, disambiguation=None, n=-1):
        if not source_text:
            return source_text
        return PREFIX + source_text


def untranslated(texts: list[str]) -> list[str]:
    return [t for t in texts if VIETNAMESE.search(t) and PREFIX not in t]


# --- i18n -------------------------------------------------------------------------------------------


def test_fake_translator_retranslates_window_and_tray(gui, gui_wait, config_dir, ui_helpers):
    d = config_dir("A", identity=False)  # màn “Bắt đầu”: không node, không mạng
    seen: dict = {}
    translator = PrefixTranslator()

    def script(run):
        yield gui_wait(lambda: run.windows and run.trays, 10, "có cửa sổ và khay")
        yield gui_wait(lambda: ui_helpers.WELCOME_HEAD in ui_helpers.all_text(run.window), 5, "màn Bắt đầu")
        seen["before"] = ui_helpers.widget_texts(run.window)
        QCoreApplication.installTranslator(translator)
        seen["direct"] = QCoreApplication.translate("test", "Thử lại")  # translator giả có hiệu lực
        # Chờ tối đa 3 s cho `LanguageChange` tới cửa sổ và khay (hết hạn thì so ở dưới, báo rõ chỗ thiếu).
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not (
            any(PREFIX in t for t in ui_helpers.widget_texts(run.window))
            and all(PREFIX in a.text() for a in run.tray.actions())
        ):
            yield
        seen["window"] = ui_helpers.widget_texts(run.window)
        seen["tray"] = [a.text() for a in run.tray.actions() if not a.isSeparator()]
        QCoreApplication.removeTranslator(translator)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(PREFIX in a.text() for a in run.tray.actions()):
            yield
        seen["back"] = [ui_helpers.plain(a.text()) for a in run.tray.actions() if not a.isSeparator()]
        run.tray.quit_requested.emit()

    try:
        gui(["--config-dir", str(d)], script)
    finally:
        QCoreApplication.removeTranslator(translator)
    assert not any(PREFIX in t for t in seen["before"]), "trước khi cài translator: chuỗi nguồn"
    assert seen["direct"] == PREFIX + "Thử lại", "translator giả không có hiệu lực (lỗi của test)"
    assert any(PREFIX in t for t in seen["window"]), seen["window"]
    assert not untranslated(seen["window"]), f"chữ chưa dịch lại trên cửa sổ: {untranslated(seen['window'])}"
    assert len(seen["tray"]) == 4 and all(PREFIX in t for t in seen["tray"]), seen["tray"]
    assert seen["back"] == list(ui_helpers.M5), "gỡ translator → về chuỗi nguồn"


def test_missing_qm_falls_back_to_source_strings(qapp, gui, gui_wait, config_dir, ui_helpers):
    from teleport.ui import i18n

    i18n.install(qapp, "zz")  # không có teleport_zz.qm
    i18n.set_language("zz")
    d = config_dir("A", identity=False)
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.windows and run.trays, 10, "có cửa sổ và khay")
        seen["tray"] = [ui_helpers.plain(a.text()) for a in run.tray.actions() if not a.isSeparator()]
        seen["text"] = ui_helpers.all_text(run.window)
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(d)], script)
    assert seen["tray"] == list(ui_helpers.M5)
    assert ui_helpers.WELCOME_HEAD in seen["text"]


def test_install_without_language_does_not_raise(qapp):
    from teleport.ui import i18n

    i18n.install(qapp, None)
    assert isinstance(i18n.current_language(), str) or i18n.current_language() is None


# --- Theme ------------------------------------------------------------------------------------------


def _linear(c: float) -> float:
    c = c / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def wcag_ratio(fg: QColor, bg: QColor) -> float:
    """Tỉ lệ tương phản WCAG 2.x, tính độc lập."""

    def lum(c: QColor) -> float:
        return 0.2126 * _linear(c.red()) + 0.7152 * _linear(c.green()) + 0.0722 * _linear(c.blue())

    a, b = sorted((lum(fg), lum(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


PAIRS = [
    (QPalette.ColorRole.WindowText, QPalette.ColorRole.Window),
    (QPalette.ColorRole.Text, QPalette.ColorRole.Base),
    (QPalette.ColorRole.ButtonText, QPalette.ColorRole.Button),
    (QPalette.ColorRole.HighlightedText, QPalette.ColorRole.Highlight),
    (QPalette.ColorRole.ToolTipText, QPalette.ColorRole.ToolTipBase),
]


@pytest.fixture
def restore_palette(qapp):
    palette = qapp.palette()
    style_sheet = qapp.styleSheet()
    yield
    qapp.setPalette(palette)
    qapp.setStyleSheet(style_sheet)


@pytest.fixture
def color_scheme(qapp, monkeypatch):
    """`set(scheme)`: đổi `styleHints().colorScheme()` và phát `colorSchemeChanged`.

    Trên nền `offscreen`, `setColorScheme` có thể không có tác dụng; khi đó vá `QStyleHints.colorScheme`
    và tự phát tín hiệu, như hệ điều hành đổi chế độ."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QStyleHints

    state: dict = {"patched": False, "value": None}

    def set_scheme(scheme) -> None:
        hints = qapp.styleHints()
        if hasattr(hints, "setColorScheme"):
            hints.setColorScheme(scheme)
            qapp.processEvents()
            if hints.colorScheme() == scheme:
                return
        state["value"] = scheme
        if not state["patched"]:
            monkeypatch.setattr(QStyleHints, "colorScheme", lambda self: state["value"])
            state["patched"] = True
        hints.colorSchemeChanged.emit(scheme)
        qapp.processEvents()

    yield set_scheme
    hints = qapp.styleHints()
    if hasattr(hints, "unsetColorScheme"):
        hints.unsetColorScheme()
    elif hasattr(hints, "setColorScheme"):
        hints.setColorScheme(Qt.ColorScheme.Unknown)


def test_contrast_ratio_matches_wcag():
    from teleport.ui.theme import contrast_ratio

    black, white = QColor("#000000"), QColor("#ffffff")
    assert contrast_ratio(black, white) == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio(white, black) == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio(white, white) == pytest.approx(1.0, abs=0.001)
    grey = QColor("#767676")  # xám nhạt nhất đạt 4.5 trên nền trắng
    assert contrast_ratio(grey, white) == pytest.approx(wcag_ratio(grey, white), abs=0.01)
    assert contrast_ratio(grey, white) >= 4.5


@pytest.mark.parametrize("scheme_name", ["Light", "Dark"])
def test_theme_contrast_at_least_4_5(qapp, restore_palette, color_scheme, scheme_name):
    from PySide6.QtCore import Qt

    from teleport.ui import theme

    color_scheme(getattr(Qt.ColorScheme, scheme_name))
    theme.apply(qapp)
    palette = qapp.palette()
    low = []
    for fg_role, bg_role in PAIRS:
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            fg, bg = palette.color(group, fg_role), palette.color(group, bg_role)
            ratio = wcag_ratio(fg, bg)
            if ratio < 4.5:
                low.append(f"{fg_role.name}/{bg_role.name} ({group.name}): {ratio:.2f}")
    assert not low, f"tương phản < 4.5 ở chế độ {scheme_name}: {low}"


def test_dark_and_light_palettes_differ_and_follow_scheme_change(qapp, restore_palette, color_scheme):
    from PySide6.QtCore import Qt

    from teleport.ui import theme

    color_scheme(Qt.ColorScheme.Light)
    theme.apply(qapp)
    light_bg = qapp.palette().color(QPalette.ColorRole.Window)
    # Đổi chế độ sau `apply`: theme phải tự theo `colorSchemeChanged`, không cần gọi lại `apply`.
    color_scheme(Qt.ColorScheme.Dark)
    qapp.processEvents()
    dark_bg = qapp.palette().color(QPalette.ColorRole.Window)
    assert dark_bg != light_bg
    assert dark_bg.lightness() < light_bg.lightness()
    color_scheme(Qt.ColorScheme.Light)
    qapp.processEvents()
    assert qapp.palette().color(QPalette.ColorRole.Window).lightness() > dark_bg.lightness()
