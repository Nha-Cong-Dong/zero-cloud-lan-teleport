"""Hoạt ảnh của `IconButton`, `ToggleSwitch`, `SegmentedControl` (spec-g2 mục 3.5–3.7, 4.2, test 7.2;
mục 12 H1 và H5 thắng).

Viết từ spec, không từ code. Mặc định chế độ tức thì (autouse `motion_instant`), hoạt ảnh thật xin
`real_motion`; chỉ kiểm giá trị cuối (`qtbot.waitUntil`) rồi `running_count() == 0` (mvp N3);
`motion.finish_all()` trước `grab()`; màu ở cả sáng và tối, hai màu nhấn.

- `IconButton.hover_level()/press_level()`: tức thì và thật; pixel hover ≈ `selected`, ấn ≈ `border`.
- `ToggleSwitch.knob_position()`: chữ/`isChecked()` đổi ngay, núm tới 1/0; tức thì và chưa hiện thì nhảy;
  H1: `setChecked` khi đang chặn signal vẫn nhảy thẳng; pixel bật ≈ `accent_strong`.
- `SegmentedControl.indicator_rect()`: mũi tên đổi `value()`/tiêu điểm ngay, khối tới
  `checked_button().geometry()`; H5: property `SEGMENT_SLIDING_PROPERTY` gỡ khi trượt xong, khi ẩn,
  khi bị thay; pixel chọn ≈ `accent`, tắt ≈ `selected`; luật QSS `:checked` mới.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QCursor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton, QWidget

MODES = pytest.mark.parametrize("dark", [False, True], ids=["sang", "toi"])
ACCENTS = pytest.mark.parametrize("accent", ["mint", "ocean"])
WAIT_MS = 3000
TOL = 2


# =================================================================================================
# Hàm phụ trợ (định nghĩa tại chỗ: không import conftest)
# =================================================================================================


def _close(a: QColor, b: QColor, tol: int = TOL) -> bool:
    return (
        abs(a.red() - b.red()) <= tol
        and abs(a.green() - b.green()) <= tol
        and abs(a.blue() - b.blue()) <= tol
    )


def _px(image: QImage, x: float, y: float) -> QColor:
    dpr = image.devicePixelRatio() or 1.0
    return image.pixelColor(int(x * dpr), int(y * dpr))


def _apply(qapp, ui_theme, dark: bool, accent: str = "mint"):
    from teleport.ui import theme

    theme.set_preference(qapp, theme.Mode.DARK if dark else theme.Mode.LIGHT, accent)
    ui_theme(dark)
    QApplication.processEvents()
    return theme.tokens(dark, accent)


_HOSTS: list[QWidget] = []


@pytest.fixture(autouse=True)
def _keep_hosts():
    yield
    _HOSTS.clear()


def _host(qtbot, *widgets: QWidget) -> QWidget:
    host = QWidget()
    qtbot.addWidget(host)
    _HOSTS.append(host)  # qtbot chỉ giữ tham chiếu yếu: giữ cửa sổ sống tới hết test
    row = QHBoxLayout(host)
    row.setContentsMargins(24, 24, 24, 24)
    for w in widgets:
        row.addWidget(w)
    row.addStretch(1)
    host.resize(520, 120)
    # Cửa sổ mới mở dưới con trỏ của test trước thì Qt coi nút đã được rê chuột, và QTest.mouseMove
    # trong cửa sổ đó không sinh Leave (offscreen): đưa con trỏ ra xa trước khi hiện.
    QCursor.setPos(5000, 5000)
    QApplication.processEvents()
    host.show()
    qtbot.waitExposed(host)
    QApplication.processEvents()
    return host


def _activate(qtbot, host: QWidget) -> None:
    host.activateWindow()
    host.raise_()
    qtbot.waitUntil(host.isActiveWindow, timeout=WAIT_MS)


def _hover(w: QWidget) -> None:
    QTest.mouseMove(w, w.rect().center())
    QApplication.processEvents()


def _leave(host: QWidget) -> None:
    QTest.mouseMove(host, QPoint(2, 2))
    QApplication.processEvents()


def _finish() -> None:
    from teleport.ui import motion

    motion.finish_all()
    QApplication.processEvents()


_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_RULE = re.compile(r"([^{}]*)\{([^{}]*)\}")


def _norm(selector: str) -> str:
    return re.sub(r"[\s\"']", "", selector)


def find_rule(css: str, selector: str) -> dict[str, str]:
    """Gộp khai báo của mọi luật có đúng bộ chọn `selector` (bỏ nháy, khoảng trắng khi so)."""
    want = _norm(selector)
    merged: dict[str, str] = {}
    found = False
    for m in _RULE.finditer(_COMMENT.sub("", css)):
        if any(_norm(s) == want for s in m.group(1).split(",")):
            found = True
            for part in m.group(2).split(";"):
                if ":" in part:
                    key, value = part.split(":", 1)
                    merged[key.strip().lower()] = value.strip()
    assert found, f"stylesheet không có luật cho {selector}"
    return merged


def _rule_start(css: str, selector: str) -> int:
    """Vị trí của luật đầu tiên có đúng bộ chọn `selector` (so như `find_rule`)."""
    want = _norm(selector)
    stripped = _COMMENT.sub("", css)
    for m in _RULE.finditer(stripped):
        if any(_norm(s) == want for s in m.group(1).split(",")):
            return m.start()
    raise AssertionError(f"stylesheet không có luật cho {selector}")


def _sliding(button) -> bool:
    from teleport.ui import theme

    value = button.property(theme.SEGMENT_SLIDING_PROPERTY)
    return value is True or value == "true"


# =================================================================================================
# 7.2.1 IconButton
# =================================================================================================


def _icon_button():
    from teleport.ui.widgets import IconButton

    return IconButton("settings", "Cài đặt")


def test_icon_button_instant_levels(qtbot, ui_theme):
    from teleport.ui import motion

    ui_theme(False)
    btn = _icon_button()
    host = _host(qtbot, btn)
    _leave(host)
    assert btn.hover_level() == 0 and btn.press_level() == 0
    seen: list[bool] = []
    btn.clicked.connect(lambda *_a: seen.append(True))
    _hover(btn)
    assert btn.hover_level() == 1
    QTest.mousePress(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    assert btn.press_level() == 1
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    assert btn.press_level() == 0
    assert seen == [True]
    _leave(host)
    assert btn.hover_level() == 0
    assert motion.running_count() == 0


def test_icon_button_real_motion_final_values_then_idle(qtbot, ui_theme, real_motion):
    ui_theme(False)
    btn = _icon_button()
    host = _host(qtbot, btn)
    _leave(host)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)
    _hover(btn)
    qtbot.waitUntil(lambda: btn.hover_level() == 1, timeout=WAIT_MS)
    QTest.mousePress(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    qtbot.waitUntil(lambda: btn.press_level() == 1, timeout=WAIT_MS)
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    qtbot.waitUntil(lambda: btn.press_level() == 0, timeout=WAIT_MS)
    _leave(host)
    qtbot.waitUntil(lambda: btn.hover_level() == 0, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_icon_button_hide_drops_hover_to_zero(qtbot, ui_theme, real_motion):
    ui_theme(False)
    btn = _icon_button()
    host = _host(qtbot, btn)
    _leave(host)
    _hover(btn)
    qtbot.waitUntil(lambda: btn.hover_level() == 1, timeout=WAIT_MS)
    btn.hide()
    assert btn.hover_level() == 0
    assert real_motion.running_count() == 0


def test_icon_button_os_animations_off_jumps(qtbot, ui_theme, real_motion, monkeypatch):
    monkeypatch.setattr(real_motion, "animations_enabled", lambda: False)
    ui_theme(False)
    btn = _icon_button()
    host = _host(qtbot, btn)
    _leave(host)
    _hover(btn)
    assert btn.hover_level() == 1
    assert real_motion.running_count() == 0


def test_footer_button_is_icon_button_with_levels(qtbot, ui_theme):
    """3.5: áp cho mọi `IconButton`, kể cả `home._FooterButton`."""
    from teleport.ui import home
    from teleport.ui.widgets import IconButton

    ui_theme(False)
    btn = home._FooterButton("folder", "Đổi…")
    assert isinstance(btn, IconButton)
    host = _host(qtbot, btn)
    _leave(host)
    _hover(btn)
    assert btn.hover_level() == 1


@MODES
@ACCENTS
def test_icon_button_pixels_hover_selected_pressed_border(qtbot, qapp, ui_theme, dark, accent):
    tok = _apply(qapp, ui_theme, dark, accent)
    btn = _icon_button()
    host = _host(qtbot, btn)
    _leave(host)
    # Điểm nền: giữa chiều cao, cách mép trái 4 px (trong nền bo góc, ngoài icon 20 px ở giữa).
    x, y = 4.0, btn.height() / 2

    def sample() -> QColor:
        _finish()
        return _px(host.grab(btn.geometry()).toImage(), x, y)

    rest = sample()
    assert _close(rest, tok.surface), ("nghỉ: trong suốt trên nền cửa sổ", rest.name(), tok.surface.name())
    _hover(btn)
    got = sample()
    assert _close(got, tok.selected), ("hover", got.name(), tok.selected.name())
    QTest.mousePress(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    got = sample()
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    assert _close(got, tok.border), ("ấn", got.name(), tok.border.name())


# =================================================================================================
# 7.2.2 ToggleSwitch
# =================================================================================================


def _switch():
    from teleport.ui.widgets import ToggleSwitch

    return ToggleSwitch()


def test_toggle_space_changes_state_now_and_knob_reaches_end(qtbot, ui_theme, real_motion):
    from teleport.ui.widgets import toggle_state_text

    ui_theme(False)
    sw = _switch()
    host = _host(qtbot, sw)
    _leave(host)
    _activate(qtbot, host)
    sw.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(sw.hasFocus, timeout=WAIT_MS)
    assert sw.knob_position() == 0
    QTest.keyClick(sw, Qt.Key.Key_Space)
    assert sw.isChecked() is True, "isChecked đổi ngay"
    assert sw.state_text() == toggle_state_text(True), "chữ đổi ngay"
    qtbot.waitUntil(lambda: sw.knob_position() == 1, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)
    QTest.keyClick(sw, Qt.Key.Key_Return)
    assert sw.isChecked() is False
    assert sw.state_text() == toggle_state_text(False)
    qtbot.waitUntil(lambda: sw.knob_position() == 0, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_toggle_mouse_click_reaches_end(qtbot, ui_theme, real_motion):
    ui_theme(False)
    sw = _switch()
    host = _host(qtbot, sw)
    _leave(host)
    QTest.mouseClick(sw, Qt.MouseButton.LeftButton, pos=sw.rect().center())
    assert sw.isChecked()
    qtbot.waitUntil(lambda: sw.knob_position() == 1, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_toggle_instant_jumps(qtbot, ui_theme):
    from teleport.ui import motion

    ui_theme(False)
    sw = _switch()
    _host(qtbot, sw)
    sw.setChecked(True)
    assert sw.knob_position() == 1
    sw.setChecked(False)
    assert sw.knob_position() == 0
    assert motion.running_count() == 0


def test_toggle_os_animations_off_jumps(qtbot, ui_theme, real_motion, monkeypatch):
    monkeypatch.setattr(real_motion, "animations_enabled", lambda: False)
    ui_theme(False)
    sw = _switch()
    _host(qtbot, sw)
    sw.setChecked(True)
    assert sw.knob_position() == 1
    assert real_motion.running_count() == 0


def test_toggle_not_shown_jumps(qtbot, real_motion):
    sw = _switch()
    qtbot.addWidget(sw)
    assert not sw.isVisible()
    sw.setChecked(True)
    assert sw.knob_position() == 1
    assert real_motion.running_count() == 0
    sw.setChecked(False)
    assert sw.knob_position() == 0


def test_toggle_set_checked_with_signals_blocked_jumps_while_shown(qtbot, ui_theme, real_motion):
    """H1: `setChecked` khi `blockSignals(True)` (Cài đặt nạp giá trị) vẫn đặt núm, nhảy thẳng."""
    ui_theme(False)
    sw = _switch()
    _host(qtbot, sw)
    sw.blockSignals(True)
    try:
        sw.setChecked(True)
        assert sw.knob_position() == 1, "chặn signal: núm vẫn theo isChecked, nhảy thẳng"
        assert real_motion.running_count() == 0
        sw.setChecked(False)
        assert sw.knob_position() == 0
    finally:
        sw.blockSignals(False)
    assert real_motion.running_count() == 0


def test_toggle_set_checked_programmatically_while_shown_reaches_end(qtbot, ui_theme, real_motion):
    ui_theme(False)
    sw = _switch()
    _host(qtbot, sw)
    sw.setChecked(True)
    qtbot.waitUntil(lambda: sw.knob_position() == 1, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


@MODES
@ACCENTS
def test_toggle_pixels_on_accent_strong_off_surface_raised(qtbot, qapp, ui_theme, dark, accent):
    tok = _apply(qapp, ui_theme, dark, accent)
    sw = _switch()
    host = _host(qtbot, sw)
    _leave(host)
    track = sw.track_rect()
    cy = track.center().y()
    left_cap = track.left() + track.height() / 2
    right_cap = track.right() - track.height() / 2

    _finish()
    off = sw.grab().toImage()
    got = _px(off, right_cap, cy)
    assert _close(got, tok.surface_raised), ("tắt: rãnh", got.name(), tok.surface_raised.name())

    sw.setChecked(True)
    _finish()
    on = sw.grab().toImage()
    got = _px(on, left_cap, cy)
    assert _close(got, tok.accent_strong), ("bật: rãnh", got.name(), tok.accent_strong.name())
    knob = _px(on, right_cap, cy)
    assert _close(knob, tok.surface_raised), ("bật: núm", knob.name(), tok.surface_raised.name())


# =================================================================================================
# 7.2.3 SegmentedControl
# =================================================================================================


def _segmented(parent: QWidget | None = None):
    from teleport.ui.widgets import SegmentedControl

    seg = SegmentedControl(("a", "b", "c"), parent)
    seg.set_texts(("Theo hệ điều hành", "Sáng", "Tối"))
    return seg


def _target(seg) -> QRectF:
    return QRectF(seg.checked_button().geometry())


def test_segmented_sliding_property_name():
    from teleport.ui import theme

    assert theme.SEGMENT_SLIDING_PROPERTY == "segmentSliding"


def test_segmented_arrow_changes_value_and_focus_now_and_indicator_reaches_target(
    qtbot, ui_theme, real_motion
):
    ui_theme(False)
    before = QPushButton("trước")
    seg = _segmented()
    host = _host(qtbot, before, seg)
    _leave(host)
    _activate(qtbot, host)
    buttons = seg.buttons()
    qtbot.waitUntil(lambda: seg.indicator_rect() == _target(seg), timeout=WAIT_MS)
    seen: list = []
    seg.value_selected.connect(seen.append)
    buttons[0].setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(buttons[0].hasFocus, timeout=WAIT_MS)
    QTest.keyClick(buttons[0], Qt.Key.Key_Right)
    assert seg.value() == "b", "value đổi ngay"
    assert seg.checked_button() is buttons[1]
    assert QApplication.focusWidget() is buttons[1], "tiêu điểm đổi ngay"
    assert seen == ["b"], "value_selected phát ngay"
    assert _sliding(buttons[1]), "H5: nút mới chọn mang segmentSliding trong lúc trượt"
    qtbot.waitUntil(lambda: seg.indicator_rect() == _target(seg), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)
    assert not any(_sliding(b) for b in buttons), "H5: trượt xong gỡ segmentSliding"


def test_segmented_click_slides_to_clicked_button(qtbot, ui_theme, real_motion):
    ui_theme(False)
    seg = _segmented()
    host = _host(qtbot, seg)
    _leave(host)
    buttons = seg.buttons()
    QTest.mouseClick(buttons[2], Qt.MouseButton.LeftButton, pos=buttons[2].rect().center())
    assert seg.value() == "c"
    qtbot.waitUntil(lambda: seg.indicator_rect() == _target(seg), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)
    assert not any(_sliding(b) for b in buttons)


def test_segmented_set_value_while_shown_slides(qtbot, ui_theme, real_motion):
    ui_theme(False)
    seg = _segmented()
    _host(qtbot, seg)
    seg.set_value("c")
    assert seg.value() == "c"
    qtbot.waitUntil(lambda: seg.indicator_rect() == _target(seg), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_segmented_replaced_slide_clears_old_sliding_property(qtbot, ui_theme, real_motion):
    """H5: hoạt ảnh bị thay → gỡ property ở nút cũ; cuối cùng không nút nào còn property."""
    ui_theme(False)
    seg = _segmented()
    _host(qtbot, seg)
    buttons = seg.buttons()
    seg.set_value("b")
    seg.set_value("c")
    assert not _sliding(buttons[1]), "nút bị thay không còn segmentSliding"
    qtbot.waitUntil(lambda: seg.indicator_rect() == _target(seg), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)
    assert not any(_sliding(b) for b in buttons)


def test_segmented_hide_during_slide_clears_sliding_property(qtbot, ui_theme, real_motion):
    ui_theme(False)
    seg = _segmented()
    _host(qtbot, seg)
    buttons = seg.buttons()
    seg.set_value("c")
    seg.hide()
    QApplication.processEvents()
    assert not any(_sliding(b) for b in buttons), "H5: hideEvent gỡ segmentSliding"
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_segmented_not_shown_jumps(qtbot, real_motion):
    seg = _segmented()
    qtbot.addWidget(seg)
    assert not seg.isVisible()
    seg.set_value("c")
    assert seg.indicator_rect() == _target(seg)
    assert real_motion.running_count() == 0
    assert not any(_sliding(b) for b in seg.buttons())


def test_segmented_instant_jumps(qtbot, ui_theme):
    from teleport.ui import motion

    ui_theme(False)
    seg = _segmented()
    _host(qtbot, seg)
    seg.set_value("c")
    assert seg.indicator_rect() == _target(seg)
    seg.set_value("a")
    assert seg.indicator_rect() == _target(seg)
    assert motion.running_count() == 0
    assert not any(_sliding(b) for b in seg.buttons())


def test_segmented_os_animations_off_jumps(qtbot, ui_theme, real_motion, monkeypatch):
    monkeypatch.setattr(real_motion, "animations_enabled", lambda: False)
    ui_theme(False)
    seg = _segmented()
    _host(qtbot, seg)
    seg.set_value("b")
    assert seg.indicator_rect() == _target(seg)
    assert real_motion.running_count() == 0
    assert not any(_sliding(b) for b in seg.buttons())


def test_segmented_indicator_rect_type(qtbot):
    seg = _segmented()
    qtbot.addWidget(seg)
    assert isinstance(seg.indicator_rect(), QRectF)


@MODES
@ACCENTS
def test_segmented_pixels_checked_accent_unchecked_not(qtbot, qapp, ui_theme, dark, accent):
    tok = _apply(qapp, ui_theme, dark, accent)
    seg = _segmented()
    host = _host(qtbot, seg)
    _leave(host)
    seg.set_value("b")
    _finish()
    image = seg.grab().toImage()
    buttons = seg.buttons()

    def point(button) -> tuple[float, float]:
        g = button.geometry()
        return g.left() + 4.0, g.top() + g.height() / 2

    got = _px(image, *point(buttons[1]))
    assert _close(got, tok.accent), ("chọn", got.name(), tok.accent.name())
    for other in (buttons[0], buttons[2]):
        got = _px(image, *point(other))
        assert not _close(got, tok.accent, 6), ("chưa chọn không có nền accent", got.name())

    seg.setEnabled(False)
    _finish()
    image = seg.grab().toImage()
    got = _px(image, *point(buttons[1]))
    assert _close(got, tok.selected), ("khung tắt", got.name(), tok.selected.name())


@MODES
@ACCENTS
def test_segmented_stylesheet_checked_rules(dark, accent):
    from teleport.ui import theme

    css = theme.stylesheet(dark, accent=accent)
    t = theme.tokens(dark, accent)
    for position in ("first", "middle", "last"):
        sel = f'QRadioButton[segment="{position}"]'
        checked = find_rule(css, f"{sel}:checked")
        background = checked.get("background", checked.get("background-color", ""))
        assert "transparent" in background, ("nền :checked do khung vẽ", background)
        assert t.accent.name().lower() not in background.lower()
        assert QColor(checked["color"]).name() == t.accent_text.name(), checked
        assert "transparent" in checked.get("border-color", ""), checked
        checked_disabled = find_rule(css, f"{sel}:checked:disabled")
        bg = checked_disabled.get("background", checked_disabled.get("background-color", ""))
        assert "transparent" in bg, checked_disabled
        assert "transparent" in checked_disabled.get("border-color", ""), checked_disabled

        sliding_sel = f'{sel}[{theme.SEGMENT_SLIDING_PROPERTY}="true"]:checked'
        sliding = find_rule(css, sliding_sel)
        assert QColor(sliding["color"]).name() == t.text.name(), sliding
        assert _rule_start(css, f"{sel}:checked") < _rule_start(css, sliding_sel), "H5: sau :checked"
        assert _rule_start(css, sliding_sel) < _rule_start(css, f"{sel}:focus"), "H5: trước :focus"


def test_parentless_icon_button_is_destroyed_after_del(qtbot):
    """`IconButton` không cha không bị giữ sống bởi `ButtonLevels` (rò bộ nhớ, soat-code G2)."""
    import gc

    from PySide6.QtCore import QCoreApplication, QEvent

    from teleport.ui.widgets import IconButton

    def drain() -> None:
        gc.collect()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()

    drain()
    before = sum(isinstance(w, IconButton) for w in QApplication.allWidgets())
    for _ in range(5):
        btn = _icon_button()
        btn.show()
        QApplication.processEvents()
        del btn
    drain()
    assert sum(isinstance(w, IconButton) for w in QApplication.allWidgets()) == before
