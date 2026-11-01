"""Lựa chọn chế độ màu và màu nhấn của người dùng trong `ui/theme.py` (spec-d6 mục 2.2, 5.3; mục 12 thắng).

Viết từ spec, không từ code:

- `set_preference(app, mode, accent)`: LIGHT/DARK ép chế độ (kể cả khi hệ điều hành ngược lại) và gọi
  `styleHints().setColorScheme(...)`; SYSTEM gọi `unsetColorScheme()` và theo hệ điều hành; **áp ngay**,
  không chờ `colorSchemeChanged`. Màu nhấn lạ quy về `DEFAULT_ACCENT`.
- Handler `colorSchemeChanged` nối một lần; khi đang ép thì không dùng giá trị tín hiệu.
- `_reapply` chỉ đặt palette/stylesheet khi khác cái đang có trên `app` (mục 12 ý 1); style Fusion đặt
  một lần (ý 2); thứ tự: gán lựa chọn → `setPalette` → `setStyleSheet` (ý 3), nên widget đọc
  `tokens_for` trong lúc nhận `ApplicationPaletteChange` đã thấy màu mới.
- `apply(app, scheme)` giữ chữ ký cũ: `scheme` tường minh áp đúng chế độ đó với màu nhấn đang chọn, không
  đổi lựa chọn đã lưu.
- Ràng buộc chéo `app`–`ui` (mục 12 ý 8): mã `ACCENTS` khớp `settings.ACCENT_RE`; giá trị lưu của `Mode`
  (bỏ None) đúng bằng `settings.THEMES`.

Giả hệ điều hành bằng cách vá `QStyleHints.colorScheme` và tự phát `colorSchemeChanged` (mục 12 ý 6);
`setColorScheme`/`unsetColorScheme` được thay bằng bản ghi lại lời gọi (không gọi thật: trên một số nền
tảng chúng tự phát tín hiệu và làm lệch phép thử). Trạng thái cấp module do fixture autouse
`theme_state_reset` của `tests/ui/conftest.py` đặt lại sau mỗi test.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtGui import QColor, QPalette

Role = QPalette.ColorRole

ACCENT_NAMES_VI = {
    "mint": "Bạc hà",
    "ocean": "Đại dương",
    "violet": "Tím",
    "rose": "Hồng",
    "amber": "Hổ phách",
    "graphite": "Than chì",
}
ACCENT_NAMES_EN = {
    "Bạc hà": "Mint",
    "Đại dương": "Ocean",
    "Tím": "Violet",
    "Hồng": "Rose",
    "Hổ phách": "Amber",
    "Than chì": "Graphite",
}


# =================================================================================================
# Hàm phụ trợ, fixture
# =================================================================================================


def _hex(color: QColor) -> str:
    return color.name().upper()


def _close(a: QColor, b: QColor, tol: int) -> bool:
    return (
        abs(a.red() - b.red()) <= tol
        and abs(a.green() - b.green()) <= tol
        and abs(a.blue() - b.blue()) <= tol
    )


def _has_pixel_near(image, color: QColor, tol: int) -> bool:
    for y in range(image.height()):
        for x in range(image.width()):
            px = image.pixelColor(x, y)
            if px.alpha() >= 250 and _close(px, color, tol):
                return True
    return False


def _primary_background(css: str) -> str:
    """Mã màu nền của luật `QPushButton[primary="true"] { … }` (luật gốc, không phải :hover…)."""
    m = re.search(r'QPushButton\[primary=["\']?true["\']?\]\s*\{([^}]*)\}', css)
    assert m, "stylesheet không có luật nút chính"
    bg = re.search(r"background(?:-color)?\s*:\s*(#[0-9a-fA-F]{6})", m.group(1))
    assert bg, f"luật nút chính không đặt nền: {m.group(1)!r}"
    return bg.group(1).upper()


def _applied(qapp, dark: bool, accent: str) -> bool:
    """Palette của ứng dụng là palette (dark, accent) của theme: so Window và Highlight."""
    from teleport.ui import theme

    tok = theme.tokens(dark, accent)
    pal = qapp.palette()
    return _hex(pal.color(Role.Window)) == _hex(tok.surface) and _hex(pal.color(Role.Highlight)) == _hex(
        tok.accent_strong
    )


@pytest.fixture
def fake_os(qapp, monkeypatch):
    """Hệ điều hành giả: `state.scheme` là chế độ HĐH; `state.change(s)` đổi rồi phát `colorSchemeChanged`;
    `state.calls` ghi lại `("set", scheme)` / `("unset",)` của `QStyleHints`."""
    from PySide6.QtGui import QStyleHints

    state = SimpleNamespace(scheme=Qt.ColorScheme.Light, calls=[], emitted=[])

    monkeypatch.setattr(QStyleHints, "colorScheme", lambda self: state.scheme)
    monkeypatch.setattr(
        QStyleHints, "setColorScheme", lambda self, scheme: state.calls.append(("set", scheme))
    )
    monkeypatch.setattr(QStyleHints, "unsetColorScheme", lambda self: state.calls.append(("unset",)))

    def on_signal(value) -> None:
        state.emitted.append(value)

    hints = qapp.styleHints()
    hints.colorSchemeChanged.connect(on_signal)

    def change(scheme) -> None:
        state.scheme = scheme
        hints.colorSchemeChanged.emit(scheme)
        qapp.processEvents()

    state.change = change
    yield state
    hints.colorSchemeChanged.disconnect(on_signal)


class _Spy:
    """Đếm lời gọi `setPalette` / `setStyleSheet` / `setStyle` của `QApplication` rồi gọi bản thật."""

    def __init__(self, monkeypatch) -> None:
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QApplication

        self.palette = 0
        self.stylesheet = 0
        self.style = 0
        real_app_palette = QApplication.setPalette
        real_gui_palette = QGuiApplication.setPalette
        real_style = QApplication.setStyle
        real_css = QApplication.setStyleSheet

        def strip(args):
            return args[1:] if args and isinstance(args[0], QCoreApplication) else args

        def app_palette(*args):
            self.palette += 1
            return real_app_palette(*strip(args))

        def gui_palette(*args):
            self.palette += 1
            return real_gui_palette(*strip(args))

        def set_style(*args):
            self.style += 1
            return real_style(*strip(args))

        def set_css(app, css):
            self.stylesheet += 1
            return real_css(app, css)

        monkeypatch.setattr(QApplication, "setPalette", staticmethod(app_palette))
        monkeypatch.setattr(QGuiApplication, "setPalette", staticmethod(gui_palette))
        monkeypatch.setattr(QApplication, "setStyle", staticmethod(set_style))
        monkeypatch.setattr(QApplication, "setStyleSheet", set_css)


class _PaletteEvents(QObject):
    """Bộ lọc sự kiện: ghi `accent_strong` mà `tokens_for` trả về lúc widget nhận `ApplicationPaletteChange`
    và đếm số lần vẽ."""

    def __init__(self, widget) -> None:
        super().__init__()
        self.widget = widget
        self.seen: list[str] = []
        self.paints = 0
        widget.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        from teleport.ui import theme

        if event.type() == QEvent.Type.ApplicationPaletteChange:
            self.seen.append(_hex(theme.tokens_for(self.widget).accent_strong))
        elif event.type() == QEvent.Type.Paint:
            self.paints += 1
        return False


# =================================================================================================
# Chế độ màu
# =================================================================================================


def test_mode_values_and_setting_mapping():
    from teleport.ui import theme

    assert [m.value for m in theme.Mode] == ["system", "light", "dark"]
    assert theme.Mode.SYSTEM.setting_value is None
    assert theme.Mode.LIGHT.setting_value == "light"
    assert theme.Mode.DARK.setting_value == "dark"
    for mode in theme.Mode:
        assert theme.mode_from_setting(mode.setting_value) is mode
    for odd in ("system", "Light", "DARK", "", "auto", " dark"):
        assert theme.mode_from_setting(odd) is theme.Mode.SYSTEM, odd


def test_light_forced_even_when_os_dark(qapp, fake_os):
    from teleport.ui import theme

    fake_os.scheme = Qt.ColorScheme.Dark
    theme.set_preference(qapp, theme.Mode.LIGHT, None)
    assert theme.current_mode() is theme.Mode.LIGHT
    assert _applied(qapp, False, theme.DEFAULT_ACCENT)
    assert not theme.is_dark_palette(qapp.palette())
    assert _primary_background(qapp.styleSheet()) == _hex(theme.tokens(False, theme.DEFAULT_ACCENT).accent)


def test_dark_forced_even_when_os_light(qapp, fake_os):
    from teleport.ui import theme

    fake_os.scheme = Qt.ColorScheme.Light
    theme.set_preference(qapp, theme.Mode.DARK, None)
    assert theme.current_mode() is theme.Mode.DARK
    assert _applied(qapp, True, theme.DEFAULT_ACCENT)
    assert theme.is_dark_palette(qapp.palette())
    assert _primary_background(qapp.styleSheet()) == _hex(theme.tokens(True, theme.DEFAULT_ACCENT).accent)


@pytest.mark.parametrize("unknown_os", [Qt.ColorScheme.Unknown, Qt.ColorScheme.Light])
def test_system_follows_os_and_later_changes(qapp, fake_os, unknown_os):
    """SYSTEM: theo HĐH lúc chọn (`Unknown` → sáng) và theo mỗi lần HĐH đổi sau đó."""
    from teleport.ui import theme

    fake_os.scheme = unknown_os
    theme.set_preference(qapp, theme.Mode.SYSTEM, "ocean")
    assert _applied(qapp, False, "ocean")
    fake_os.change(Qt.ColorScheme.Dark)
    assert _applied(qapp, True, "ocean"), "HĐH đổi sang tối mà ứng dụng chưa đổi"
    fake_os.change(Qt.ColorScheme.Light)
    assert _applied(qapp, False, "ocean")
    assert theme.current_mode() is theme.Mode.SYSTEM


def test_system_after_forced_returns_to_os(qapp, fake_os):
    from teleport.ui import theme

    fake_os.scheme = Qt.ColorScheme.Dark
    theme.set_preference(qapp, theme.Mode.LIGHT, None)
    assert _applied(qapp, False, "mint")
    theme.set_preference(qapp, theme.Mode.SYSTEM, None)
    assert _applied(qapp, True, "mint"), "bỏ ép phải về ngay chế độ của HĐH (tối)"


@pytest.mark.parametrize("forced", ["LIGHT", "DARK"])
def test_forced_mode_ignores_os_change(qapp, fake_os, forced):
    """Đang ép: tín hiệu HĐH (kể cả mang giá trị ngược) không đổi chế độ, không đổi màu nhấn."""
    from teleport.ui import theme

    mode = getattr(theme.Mode, forced)
    dark = mode is theme.Mode.DARK
    fake_os.scheme = Qt.ColorScheme.Light if dark else Qt.ColorScheme.Dark
    theme.set_preference(qapp, mode, "rose")
    for scheme in (Qt.ColorScheme.Dark, Qt.ColorScheme.Light, Qt.ColorScheme.Unknown, Qt.ColorScheme.Dark):
        fake_os.change(scheme)
        assert _applied(qapp, dark, "rose"), f"ép {forced} nhưng đổi theo HĐH ({scheme.name})"
    assert theme.current_mode() is mode and theme.current_accent() == "rose"


def test_set_preference_calls_set_and_unset_color_scheme(qapp, fake_os):
    from teleport.ui import theme

    theme.set_preference(qapp, theme.Mode.LIGHT, None)
    assert fake_os.calls[-1] == ("set", Qt.ColorScheme.Light)
    theme.set_preference(qapp, theme.Mode.DARK, None)
    assert fake_os.calls[-1] == ("set", Qt.ColorScheme.Dark)
    theme.set_preference(qapp, theme.Mode.SYSTEM, None)
    assert fake_os.calls[-1] == ("unset",)
    # Đổi màu nhấn khi đang ép: vẫn đúng chế độ ép (không được gọi unset).
    theme.set_preference(qapp, theme.Mode.DARK, "amber")
    assert fake_os.calls[-1] == ("set", Qt.ColorScheme.Dark)
    assert ("unset",) not in fake_os.calls[3:]


def test_applies_without_color_scheme_signal(qapp, fake_os):
    """Áp ngay trong lời gọi: không phát/không chờ `colorSchemeChanged`, không cần vòng sự kiện."""
    from teleport.ui import theme

    fake_os.scheme = Qt.ColorScheme.Light
    theme.set_preference(qapp, theme.Mode.DARK, "violet")
    assert _applied(qapp, True, "violet")  # chưa processEvents
    theme.set_preference(qapp, theme.Mode.LIGHT, "graphite")
    assert _applied(qapp, False, "graphite")
    assert fake_os.emitted == []


# =================================================================================================
# Màu nhấn
# =================================================================================================


def test_unknown_accent_in_set_preference_becomes_default(qapp, fake_os):
    from teleport.ui import theme

    for odd in ("tuong_lai_9", "OCEAN", ""):
        theme.set_preference(qapp, theme.Mode.LIGHT, "rose")
        theme.set_preference(qapp, theme.Mode.LIGHT, odd)
        assert theme.current_accent() == theme.DEFAULT_ACCENT, odd
        assert _applied(qapp, False, theme.DEFAULT_ACCENT)
    theme.set_preference(qapp, theme.Mode.LIGHT, "rose")
    theme.set_preference(qapp, theme.Mode.LIGHT, None)
    assert theme.current_accent() == theme.DEFAULT_ACCENT


@pytest.mark.parametrize("dark", [False, True], ids=["sang", "toi"])
def test_accent_change_updates_highlight_and_primary_rule(qapp, fake_os, dark):
    from teleport.ui import theme

    mode = theme.Mode.DARK if dark else theme.Mode.LIGHT
    for accent in theme.ACCENTS:
        theme.set_preference(qapp, mode, accent)
        tok = theme.tokens(dark, accent)
        assert theme.current_accent() == accent
        assert _hex(qapp.palette().color(Role.Highlight)) == _hex(tok.accent_strong), accent
        assert _primary_background(qapp.styleSheet()) == _hex(tok.accent), accent


def test_tokens_for_uses_current_accent(qapp, fake_os):
    from PySide6.QtWidgets import QWidget

    from teleport.ui import theme

    widget = QWidget()
    try:
        theme.set_preference(qapp, theme.Mode.DARK, "violet")
        want = theme.tokens(True, "violet")
        assert _hex(theme.tokens_for(widget).accent_strong) == _hex(want.accent_strong)
        assert _hex(theme.tokens_for(widget).bubble_out) == _hex(want.bubble_out)
        # `tokens(dark)` không truyền màu nhấn = màu đang chọn (mục 12 ý 4).
        assert _hex(theme.tokens(True).accent) == _hex(want.accent)
        assert _hex(theme.tokens(False).accent) == _hex(theme.tokens(False, "violet").accent)
        theme.set_preference(qapp, theme.Mode.LIGHT, "amber")
        assert _hex(theme.tokens_for(widget).accent_strong) == _hex(
            theme.tokens(False, "amber").accent_strong
        )
    finally:
        widget.deleteLater()


def test_widgets_see_new_accent_during_palette_change(qapp, qtbot, fake_os):
    """Thứ tự áp (mục 12 ý 3): lúc widget nhận `ApplicationPaletteChange`, `tokens_for` đã trả màu mới."""
    from teleport.ui import theme
    from teleport.ui.widgets import ChatBubble, Direction

    theme.set_preference(qapp, theme.Mode.LIGHT, "mint")
    bubble = ChatBubble(Direction.OUT)
    qtbot.addWidget(bubble)
    bubble.resize(120, 60)
    bubble.show()
    qtbot.waitExposed(bubble)
    spy = _PaletteEvents(bubble)
    theme.set_preference(qapp, theme.Mode.LIGHT, "ocean")
    qapp.processEvents()
    assert spy.seen, "đổi màu nhấn không gửi ApplicationPaletteChange (Highlight phải đổi)"
    assert set(spy.seen) == {_hex(theme.tokens(False, "ocean").accent_strong)}, spy.seen


def test_custom_widgets_repaint_on_accent_change(qapp, qtbot, fake_os):
    """Widget tự vẽ (`ChatBubble` gửi đi tô `bubble_out`) tự vẽ lại khi đổi màu nhấn, không cần ai gọi
    `update()`; lần vẽ mới dùng màu mới."""
    from teleport.ui import theme
    from teleport.ui.widgets import ChatBubble, Direction

    theme.set_preference(qapp, theme.Mode.LIGHT, "mint")
    bubble = ChatBubble(Direction.OUT)
    qtbot.addWidget(bubble)
    bubble.resize(160, 80)
    bubble.show()
    qtbot.waitExposed(bubble)
    spy = _PaletteEvents(bubble)
    qapp.processEvents()
    before = spy.paints
    theme.set_preference(qapp, theme.Mode.LIGHT, "rose")
    qtbot.waitUntil(lambda: spy.paints > before, timeout=2000)
    center = bubble.grab().toImage().pixelColor(80, 40)
    want = theme.tokens(False, "rose").bubble_out
    assert _close(center, want, 2), f"{_hex(center)} ≠ {_hex(want)}"


@pytest.mark.parametrize("dark", [False, True], ids=["sang", "toi"])
def test_no_stale_accent_pixels_after_switch(qapp, qtbot, fake_os, dark):
    """Màu không đóng băng lúc dựng widget: dựng dưới `mint`, đổi sang `ocean` thì ảnh chụp không còn điểm
    ảnh nào của bạc hà (nền bong bóng gửi đi, viền ATTENTION, vòng icon + icon của `EmptyState`, nền nút
    chính), và có màu của `ocean`."""
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    from teleport.ui import theme
    from teleport.ui.widgets import BubbleEmphasis, ChatBubble, Direction, EmptyState

    mode = theme.Mode.DARK if dark else theme.Mode.LIGHT
    theme.set_preference(qapp, mode, "mint")
    root = QWidget()
    qtbot.addWidget(root)
    column = QVBoxLayout(root)
    out_bubble = ChatBubble(Direction.OUT)
    out_bubble.setMinimumSize(160, 50)
    attention = ChatBubble(Direction.IN, emphasis=BubbleEmphasis.ATTENTION)
    attention.setMinimumSize(160, 50)
    empty = EmptyState()
    for w in (out_bubble, attention, empty):
        column.addWidget(w)
    root.resize(360, 420)
    root.show()
    qtbot.waitExposed(root)
    mint = theme.tokens(dark, "mint")
    first = root.grab().toImage()
    assert _has_pixel_near(first, mint.bubble_out, 2), "đối chứng: lúc đầu phải có màu bạc hà"

    theme.set_preference(qapp, mode, "ocean")
    qapp.processEvents()
    image = root.grab().toImage()
    ocean = theme.tokens(dark, "ocean")
    stale = [
        name
        for name in ("bubble_out", "accent_strong", "accent_subtle", "accent_text")
        if _has_pixel_near(image, getattr(mint, name), 1)
    ]
    assert not stale, f"còn điểm ảnh bạc hà sau khi đổi sang ocean: {stale}"
    assert _has_pixel_near(image, ocean.bubble_out, 2)
    assert _has_pixel_near(image, ocean.accent_strong, 2)


# =================================================================================================
# Áp lại, tín hiệu, style
# =================================================================================================


def test_reapply_skips_when_unchanged(qapp, fake_os, monkeypatch):
    """Cùng (chế độ, màu nhấn) lần nữa: không đặt lại palette/stylesheet (không polish lại, không nháy)."""
    from teleport.ui import theme

    theme.set_preference(qapp, theme.Mode.DARK, "rose")
    spy = _Spy(monkeypatch)
    theme.set_preference(qapp, theme.Mode.DARK, "rose")
    fake_os.change(Qt.ColorScheme.Light)  # đang ép: tín hiệu HĐH cũng không được áp lại
    assert (spy.palette, spy.stylesheet) == (0, 0)
    theme.set_preference(qapp, theme.Mode.DARK, "amber")
    assert spy.palette >= 1 and spy.stylesheet >= 1, "đổi màu nhấn phải đặt lại palette và stylesheet"


def test_reapply_restores_after_external_palette_change(qapp, fake_os):
    """Không nhớ lần áp trước trong module (mục 12 ý 1): palette bị đặt từ ngoài (fixture khôi phục) thì lần
    `set_preference` cùng lựa chọn vẫn áp lại."""
    from teleport.ui import theme

    theme.set_preference(qapp, theme.Mode.DARK, "violet")
    qapp.setPalette(theme.palette(False, "mint"))
    qapp.setStyleSheet("")
    theme.set_preference(qapp, theme.Mode.DARK, "violet")
    assert _applied(qapp, True, "violet")
    assert _primary_background(qapp.styleSheet()) == _hex(theme.tokens(True, "violet").accent)


def test_signal_connected_once(qapp, fake_os):
    from PySide6.QtCore import SIGNAL

    from teleport.ui import theme

    hints = qapp.styleHints()
    signature = SIGNAL("colorSchemeChanged(Qt::ColorScheme)")
    theme.set_preference(qapp, theme.Mode.SYSTEM, None)
    base = hints.receivers(signature)
    for mode in (theme.Mode.LIGHT, theme.Mode.DARK, theme.Mode.SYSTEM):
        theme.set_preference(qapp, mode, "ocean")
    theme.apply(qapp)
    theme.apply(qapp, Qt.ColorScheme.Dark)
    assert hints.receivers(signature) == base, "mỗi lần áp lại nối thêm handler colorSchemeChanged"


def test_fusion_style_set_only_once(qapp, fake_os, monkeypatch):
    """Mục 12 ý 2: Fusion đặt một lần (cờ trên `app`), không phải mỗi lần áp (đặt lại style polish lại toàn
    bộ widget, gây nháy)."""
    from teleport.ui import theme

    spy = _Spy(monkeypatch)
    for mode, accent in (
        (theme.Mode.LIGHT, "mint"),
        (theme.Mode.DARK, "ocean"),
        (theme.Mode.SYSTEM, "rose"),
        (theme.Mode.DARK, "rose"),
    ):
        theme.set_preference(qapp, mode, accent)
    theme.apply(qapp)
    theme.apply(qapp, Qt.ColorScheme.Light)
    assert spy.style <= 1, f"setStyle gọi {spy.style} lần"


def test_apply_explicit_scheme_compat(qapp, fake_os):
    """`apply(app, scheme)`: áp đúng chế độ `scheme` với màu nhấn đang chọn, không đổi lựa chọn đã lưu;
    `apply(app)` áp lại theo lựa chọn."""
    from teleport.ui import theme

    theme.set_preference(qapp, theme.Mode.LIGHT, "rose")
    theme.apply(qapp, Qt.ColorScheme.Dark)
    assert _applied(qapp, True, "rose")
    assert theme.current_mode() is theme.Mode.LIGHT
    assert theme.current_accent() == "rose"
    theme.apply(qapp)
    assert _applied(qapp, False, "rose")
    # Không có lựa chọn nào (SYSTEM + mặc định): hành vi như trước D6.
    theme.set_preference(qapp, theme.Mode.SYSTEM, None)
    theme.apply(qapp, Qt.ColorScheme.Light)
    assert _applied(qapp, False, theme.DEFAULT_ACCENT)
    theme.apply(qapp, Qt.ColorScheme.Dark)
    assert _applied(qapp, True, theme.DEFAULT_ACCENT)


# =================================================================================================
# Tên màu nhấn
# =================================================================================================


def test_accent_names_vietnamese_by_default():
    from teleport.ui import theme

    assert {a: theme.accent_name(a) for a in theme.ACCENTS} == ACCENT_NAMES_VI
    assert theme.accent_name("tuong_lai_9") == ACCENT_NAMES_VI[theme.DEFAULT_ACCENT]


def test_accent_names_translated_in_theme_context(qapp):
    """Tên màu dịch lúc gọi, ngữ cảnh `Theme`, nguồn là tên tiếng Việt (mục 3, mục 12 ý 7)."""
    from PySide6.QtCore import QTranslator

    from teleport.ui import theme

    class Recorder(QTranslator):
        def __init__(self) -> None:
            super().__init__()
            self.seen: list[tuple[str, str]] = []

        def translate(self, context, source, disambiguation=None, n=-1):
            self.seen.append((context, source))
            return ACCENT_NAMES_EN.get(source, "") if context == "Theme" else ""

        def isEmpty(self) -> bool:
            return False

    translator = Recorder()
    assert QCoreApplication.installTranslator(translator)
    try:
        names = {a: theme.accent_name(a) for a in theme.ACCENTS}
        assert names == {a: ACCENT_NAMES_EN[ACCENT_NAMES_VI[a]] for a in theme.ACCENTS}
        theme_sources = {source for context, source in translator.seen if context == "Theme"}
        assert set(ACCENT_NAMES_VI.values()) <= theme_sources
    finally:
        QCoreApplication.removeTranslator(translator)
    assert theme.accent_name("ocean") == "Đại dương", "gỡ bản dịch thì tên về tiếng Việt (không đệm)"


# =================================================================================================
# Ràng buộc chéo app–ui (mục 12 ý 8)
# =================================================================================================


def test_accent_ids_match_settings_accent_re():
    from teleport.app import settings
    from teleport.ui import theme

    for accent in theme.ACCENTS:
        assert re.fullmatch(settings.ACCENT_RE, accent), accent
    assert re.fullmatch(settings.ACCENT_RE, theme.DEFAULT_ACCENT)


def test_mode_setting_values_match_settings_themes():
    from teleport.app import settings
    from teleport.ui import theme

    values = {m.setting_value for m in theme.Mode} - {None}
    assert values == set(settings.THEMES)
    assert theme.Mode.SYSTEM.setting_value is None


# =================================================================================================
# Fixture autouse (mục 12 ý 5). Hai test liền nhau: test đầu để lại trạng thái lạ, test sau kiểm nó đã
# được dọn. Test sau cũng tự kiểm `_reset_for_tests` (chạy riêng lẻ hay thứ tự khác vẫn có ý nghĩa).
# =================================================================================================


def test_autouse_reset_leaves_state_dirty_on_purpose(qapp, fake_os):
    from teleport.ui import theme

    theme.set_preference(qapp, theme.Mode.DARK, "graphite")
    assert theme.current_mode() is theme.Mode.DARK and theme.current_accent() == "graphite"


def test_autouse_reset_restores_system_and_default(qapp, request):
    from teleport.ui import theme

    assert "theme_state_reset" in request.fixturenames
    assert theme.current_mode() is theme.Mode.SYSTEM
    assert theme.current_accent() == theme.DEFAULT_ACCENT
    assert not _applied(qapp, True, "graphite"), "palette của test trước còn sót"
    theme.set_preference(qapp, theme.Mode.LIGHT, "amber")
    theme._reset_for_tests()
    assert theme.current_mode() is theme.Mode.SYSTEM
    assert theme.current_accent() == theme.DEFAULT_ACCENT
