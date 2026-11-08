"""`ui/buttons.py`: `AnimatedButton` (spec-g2 mục 3.1–3.3, 4.1, test 7.1; mục 12 H2, H3, H6 thắng).

Viết từ spec, không từ code. Quy ước G1: mặc định chế độ tức thì (autouse `motion_instant`), hoạt ảnh
thật xin `real_motion`; chỉ kiểm giá trị cuối (`qtbot.waitUntil`), không giá trị giữa chừng;
`motion.finish_all()` trước `grab()` (trừ ca vòng tiêu điểm, cố ý chụp ngay khi hoạt ảnh hover còn chạy
để chứng minh vòng tiêu điểm tĩnh); màu so ở cả sáng và tối, với hai màu nhấn.

Nhóm theo spec 7.1:

1. Nghỉ: `sizeHint()` bằng `QPushButton` cùng chữ/property; `PRIMARY_PROPERTY`; default/autoDefault.
2. Tức thì: hover/ấn/nhả → `hover_level`, `press_level`, `content_offset`, `clicked`.
3. `real_motion`: tới giá trị cuối rồi `running_count() == 0` (N3); tắt hiệu ứng → nhảy thẳng (N12).
4. Pixel nút chính: thường/hover/ấn/tắt; đổi sang tối sau khi dựng.
5. Vòng tiêu điểm tĩnh.
6. Nút phụ: tương phản khi ấn hẳn; lúc nghỉ trùng `QPushButton` thường (kể cả có tiêu điểm, H2).
7. Đang chạy: chữ/tên/mô tả; khóa bấm; gỡ default; trả lại tất cả.
8. `setText` / `setAccessibleName` khi đang chạy.
9. Cỡ khi chạy (H6).
10. Spinner (`real_motion`): ẩn/hiện, thu nhỏ/khôi phục (H3), chạm trần, `finish_all`, dừng chạy.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QCursor, QImage, QKeySequence, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLineEdit, QPushButton, QWidget

MODES = pytest.mark.parametrize("dark", [False, True], ids=["sang", "toi"])
ACCENTS = pytest.mark.parametrize("accent", ["mint", "ocean"])
WAIT_MS = 3000
TOL = 2

REST = "Trùng khớp"
RUN = "Đang ghép đôi…"
RUN_DESC = "Đã xác nhận mã. Đang chờ máy kia xác nhận."


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
    """Áp màu nhấn `accent` rồi chế độ `dark`; trả token của đúng tổ hợp đó."""
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
    """Cửa sổ chứa `widgets` thành một hàng, lề 24 px (điểm (2, 2) luôn nằm ngoài mọi nút)."""
    host = QWidget()
    qtbot.addWidget(host)
    _HOSTS.append(host)  # qtbot chỉ giữ tham chiếu yếu: giữ cửa sổ sống tới hết test
    row = QHBoxLayout(host)
    row.setContentsMargins(24, 24, 24, 24)
    for w in widgets:
        row.addWidget(w)
    host.resize(420, 120)
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


def _button(text: str = REST, *, primary: bool = False):
    from teleport.ui.buttons import AnimatedButton

    return AnimatedButton(text, primary=primary)


def _reference(text: str = REST, *, primary: bool = False) -> QPushButton:
    from teleport.ui import theme

    ref = QPushButton(text)
    if primary:
        ref.setProperty(theme.PRIMARY_PROPERTY, True)
    return ref


def _fill_point(btn: QWidget) -> tuple[float, float]:
    """Điểm nền bên trong viền, ngoài vùng chữ: cách mép trái 5 px, giữa chiều cao."""
    return 5.0, btn.height() / 2


def _grab(btn: QWidget) -> QImage:
    from teleport.ui import motion

    motion.finish_all()
    QApplication.processEvents()
    return btn.grab().toImage()


def _clicks(btn) -> list[bool]:
    seen: list[bool] = []
    btn.clicked.connect(lambda *_a: seen.append(True))
    return seen


# =================================================================================================
# 7.1.1 Nghỉ
# =================================================================================================


def test_module_constants_from_spec():
    from teleport.ui import buttons, theme

    assert buttons._SPIN_PERIOD_MS == 1000
    assert buttons._SPINNER_MAX == 16
    assert buttons._SPINNER_PEN == 2
    assert abs(buttons._SECONDARY_HOVER_ALPHA - 0.08) < 1e-9
    assert abs(buttons._SECONDARY_PRESS_ALPHA - 0.14) < 1e-9
    assert theme.PRIMARY_BORDER_WIDTH == 2


@MODES
@pytest.mark.parametrize("primary", [True, False], ids=["chinh", "phu"])
@pytest.mark.parametrize("text", ["Trùng khớp", "OK", "Ghép đôi theo địa chỉ IP"])
def test_rest_size_hint_equals_plain_push_button(qtbot, ui_theme, dark, primary, text):
    ui_theme(dark)
    btn = _button(text, primary=primary)
    ref = _reference(text, primary=primary)
    qtbot.addWidget(btn)
    qtbot.addWidget(ref)
    btn.ensurePolished()
    ref.ensurePolished()
    assert btn.sizeHint() == ref.sizeHint(), (btn.sizeHint(), ref.sizeHint())


def test_primary_flag_and_property(qtbot, ui_theme):
    from teleport.ui import theme

    ui_theme(False)
    main = _button(primary=True)
    side = _button("Không trùng")
    qtbot.addWidget(main)
    qtbot.addWidget(side)
    assert isinstance(main, QPushButton)
    assert main.primary is True and side.primary is False
    assert bool(main.property(theme.PRIMARY_PROPERTY)) is True
    assert not side.property(theme.PRIMARY_PROPERTY), "nút phụ không mang PRIMARY_PROPERTY"
    assert main.running is False and side.running is False


@pytest.mark.parametrize("in_dialog", [False, True], ids=["dung-rieng", "trong-QDialog"])
def test_default_and_auto_default_like_push_button(qtbot, in_dialog):
    parent = QDialog() if in_dialog else None
    if parent is not None:
        qtbot.addWidget(parent)
    from teleport.ui.buttons import AnimatedButton

    btn = AnimatedButton(REST, parent, primary=True)
    ref = QPushButton(REST, parent)
    if parent is None:
        qtbot.addWidget(btn)
        qtbot.addWidget(ref)
    assert btn.isDefault() == ref.isDefault()
    assert btn.autoDefault() == ref.autoDefault()


def test_rest_levels_are_zero(qtbot):
    btn = _button()
    qtbot.addWidget(btn)
    assert btn.hover_level() == 0
    assert btn.press_level() == 0
    assert btn.content_offset() == 0
    assert btn.spinner_active() is False


# =================================================================================================
# 7.1.2 Tức thì
# =================================================================================================


@pytest.mark.parametrize("primary", [True, False], ids=["chinh", "phu"])
def test_instant_hover_press_release_levels_and_clicked(qtbot, ui_theme, primary):
    from teleport.ui import motion

    ui_theme(False)
    btn = _button(primary=primary)
    host = _host(qtbot, btn)
    _leave(host)
    seen = _clicks(btn)
    _hover(btn)
    assert btn.hover_level() == 1
    _leave(host)
    assert btn.hover_level() == 0
    _hover(btn)
    QTest.mousePress(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    assert btn.press_level() == 1
    assert btn.content_offset() == 1
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    assert btn.press_level() == 0
    assert btn.content_offset() == 0
    assert seen == [True]
    assert motion.running_count() == 0


def test_disabled_button_has_no_hover_level(qtbot, ui_theme):
    ui_theme(False)
    btn = _button(primary=True)
    host = _host(qtbot, btn)
    _leave(host)
    _hover(btn)
    assert btn.hover_level() == 1
    btn.setEnabled(False)
    QApplication.processEvents()
    assert btn.hover_level() == 0, "EnabledChange phải đồng bộ đích hover (nút tắt: h = 0)"
    btn.setEnabled(True)
    QApplication.processEvents()
    assert btn.hover_level() == 1, "bật lại khi chuột còn ở trên: h về 1"


def test_hide_drops_levels_to_zero_without_animation(qtbot, ui_theme, real_motion):
    ui_theme(False)
    btn = _button(primary=True)
    host = _host(qtbot, btn)
    _leave(host)
    _hover(btn)
    qtbot.waitUntil(lambda: btn.hover_level() == 1, timeout=WAIT_MS)
    btn.hide()
    assert btn.hover_level() == 0, "hideEvent: nhảy về 0, không hoạt ảnh"
    assert btn.press_level() == 0
    assert real_motion.running_count() == 0


# =================================================================================================
# 7.1.3 Hoạt ảnh thật và tắt hiệu ứng
# =================================================================================================


@pytest.mark.parametrize("primary", [True, False], ids=["chinh", "phu"])
def test_real_motion_reaches_final_levels_then_no_animation_runs(qtbot, ui_theme, real_motion, primary):
    ui_theme(False)
    btn = _button(primary=primary)
    host = _host(qtbot, btn)
    _leave(host)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)
    _hover(btn)
    qtbot.waitUntil(lambda: btn.hover_level() == 1, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)
    QTest.mousePress(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    assert btn.content_offset() == 1, "lún 1 px ngay, không hoạt ảnh"
    qtbot.waitUntil(lambda: btn.press_level() == 1, timeout=WAIT_MS)
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    assert btn.content_offset() == 0
    qtbot.waitUntil(lambda: btn.press_level() == 0, timeout=WAIT_MS)
    _leave(host)
    qtbot.waitUntil(lambda: btn.hover_level() == 0, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_animations_disabled_by_os_jumps_straight(qtbot, ui_theme, real_motion, monkeypatch):
    monkeypatch.setattr(real_motion, "animations_enabled", lambda: False)
    ui_theme(False)
    btn = _button(primary=True)
    host = _host(qtbot, btn)
    _leave(host)
    _hover(btn)
    assert btn.hover_level() == 1
    assert real_motion.running_count() == 0
    QTest.mousePress(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    assert btn.press_level() == 1
    assert btn.content_offset() == 1
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    assert btn.press_level() == 0
    _leave(host)
    assert btn.hover_level() == 0
    assert real_motion.running_count() == 0


# =================================================================================================
# 7.1.4 Pixel nút chính
# =================================================================================================


@MODES
@ACCENTS
def test_primary_pixels_normal_hover_pressed_disabled(qtbot, qapp, ui_theme, dark, accent):
    from teleport.ui import theme

    _apply(qapp, ui_theme, dark, accent)
    colors = theme.primary_colors(dark, accent)
    btn = _button(primary=True)
    host = _host(qtbot, btn)
    _leave(host)
    x, y = _fill_point(btn)

    got = _px(_grab(btn), x, y)
    assert _close(got, colors["normal"][1]), ("thường", got.name(), colors["normal"][1].name())

    _hover(btn)
    got = _px(_grab(btn), x, y)
    assert _close(got, colors["hover"][1]), ("hover", got.name(), colors["hover"][1].name())

    QTest.mousePress(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    got = _px(_grab(btn), x, y)
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    assert _close(got, colors["pressed"][1]), ("ấn", got.name(), colors["pressed"][1].name())

    _leave(host)
    btn.setEnabled(False)
    got = _px(_grab(btn), x, y)
    assert _close(got, colors["disabled"][1]), ("tắt", got.name(), colors["disabled"][1].name())


def test_primary_disabled_under_mouse_is_not_tinted(qtbot, ui_theme):
    from teleport.ui import theme

    ui_theme(False)
    btn = _button(primary=True)
    host = _host(qtbot, btn)
    _leave(host)
    btn.setEnabled(False)
    _hover(btn)
    got = _px(_grab(btn), *_fill_point(btn))
    want = theme.primary_colors(False)["disabled"][1]
    assert _close(got, want), (got.name(), want.name())


@ACCENTS
def test_primary_follows_dark_mode_switched_after_build(qtbot, qapp, ui_theme, accent):
    from teleport.ui import theme

    _apply(qapp, ui_theme, False, accent)
    btn = _button(primary=True)
    host = _host(qtbot, btn)
    _leave(host)
    _hover(btn)
    _grab(btn)
    _apply(qapp, ui_theme, True, accent)
    QApplication.processEvents()
    colors = theme.primary_colors(True, accent)
    got = _px(_grab(btn), *_fill_point(btn))
    assert _close(got, colors["hover"][1]), (got.name(), colors["hover"][1].name())
    _leave(host)
    got = _px(_grab(btn), *_fill_point(btn))
    assert _close(got, colors["normal"][1]), (got.name(), colors["normal"][1].name())


# =================================================================================================
# 7.1.5 Vòng tiêu điểm tĩnh
# =================================================================================================


def _border_points(btn: QWidget) -> list[tuple[float, float]]:
    """Hai điểm giữa cạnh trái/phải, trong viền 2 px của nút chính."""
    return [(1.0, btn.height() / 2), (btn.width() - 2.0, btn.height() / 2)]


@MODES
def test_focus_ring_is_static_and_immediate(qtbot, ui_theme, dark):
    from teleport.ui import theme

    ui_theme(dark)
    btn = _button(primary=True)
    other = QPushButton("Khác")
    host = _host(qtbot, other, btn)
    _leave(host)
    _activate(qtbot, host)
    btn.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(btn.hasFocus, timeout=WAIT_MS)
    ring = theme.focus_ring_color(dark)
    image = _grab(btn)
    for x, y in _border_points(btn):
        got = _px(image, x, y)
        assert _close(got, ring), ((x, y), got.name(), ring.name())


@MODES
def test_focus_ring_stays_while_hover_animation_runs(qtbot, ui_theme, real_motion, dark):
    from teleport.ui import theme

    ui_theme(dark)
    btn = _button(primary=True)
    other = QPushButton("Khác")
    host = _host(qtbot, other, btn)
    _leave(host)
    _activate(qtbot, host)
    btn.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(btn.hasFocus, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)
    _hover(btn)
    # Cố ý chụp ngay (không finish_all): vòng tiêu điểm không được đi theo hoạt ảnh hover.
    image = btn.grab().toImage()
    ring = theme.focus_ring_color(dark)
    for x, y in _border_points(btn):
        got = _px(image, x, y)
        assert _close(got, ring), ((x, y), got.name(), ring.name())
    qtbot.waitUntil(lambda: btn.hover_level() == 1, timeout=WAIT_MS)
    image = _grab(btn)
    for x, y in _border_points(btn):
        assert _close(_px(image, x, y), ring)


# =================================================================================================
# 7.1.6 Nút phụ
# =================================================================================================


@MODES
@ACCENTS
def test_secondary_text_contrast_on_fully_pressed_background(qtbot, qapp, ui_theme, dark, accent):
    from teleport.ui import theme

    _apply(qapp, ui_theme, dark, accent)
    btn = _button("Không trùng")
    host = _host(qtbot, btn)
    _leave(host)
    _hover(btn)
    QTest.mousePress(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    assert btn.press_level() == 1
    image = _grab(btn)
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    text = QApplication.palette().color(QPalette.ColorRole.ButtonText)
    for x in (4.0, btn.width() - 5.0):
        for y in (btn.height() * 0.35, btn.height() * 0.5, btn.height() * 0.65):
            bg = _px(image, x, y)
            ratio = theme.contrast_ratio(text, bg)
            assert ratio >= theme.MIN_CONTRAST, ((x, y), bg.name(), round(ratio, 2))


def test_secondary_hover_and_press_tint_toward_text(qtbot, ui_theme):
    """Nút phụ: hover/ấn phủ màu `text` mờ lên nền nghỉ, nên nền đổi (về phía màu chữ)."""
    ui_theme(False)
    btn = _button("Không trùng")
    host = _host(qtbot, btn)
    _leave(host)
    x, y = 5.0, btn.height() / 2
    rest = _px(_grab(btn), x, y)
    _hover(btn)
    hovered = _px(_grab(btn), x, y)
    QTest.mousePress(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    pressed = _px(_grab(btn), x, y)
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    # Sáng: chữ tối, nên phủ làm nền tối đi; ấn hẳn (0,14) tối hơn hover (0,08).
    assert hovered.lightness() < rest.lightness(), (rest.name(), hovered.name())
    assert pressed.lightness() < hovered.lightness(), (hovered.name(), pressed.name())


@MODES
def test_secondary_at_rest_paints_like_plain_push_button(qtbot, ui_theme, dark):
    ui_theme(dark)
    btn = _button("Không trùng")
    ref = QPushButton("Không trùng")
    host = _host(qtbot, btn, ref)
    _leave(host)
    size = btn.sizeHint()
    btn.setFixedSize(size)
    ref.setFixedSize(size)
    QApplication.processEvents()
    assert _grab(btn) == _grab(ref), "nút phụ lúc nghỉ phải vẽ y như QPushButton thường"


@MODES
def test_secondary_at_rest_with_focus_paints_like_plain_push_button(qtbot, ui_theme, dark):
    """H2: khung tiêu điểm Fusion (`PE_FrameFocusRect`) vẫn vẽ như `QPushButton`."""
    ui_theme(dark)
    btn = _button("Không trùng")
    ref = QPushButton("Không trùng")
    host = _host(qtbot, btn, ref)
    _leave(host)
    size = btn.sizeHint()
    btn.setFixedSize(size)
    ref.setFixedSize(size)
    _activate(qtbot, host)
    btn.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(btn.hasFocus, timeout=WAIT_MS)
    focused_btn = _grab(btn)
    ref.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(ref.hasFocus, timeout=WAIT_MS)
    focused_ref = _grab(ref)
    assert focused_btn == focused_ref, "nút phụ có tiêu điểm phải vẽ y như QPushButton có tiêu điểm"


# =================================================================================================
# 7.1.7 Đang chạy: chữ, tên, mô tả, khóa bấm, default
# =================================================================================================


def test_set_running_sets_text_name_and_description(qtbot):
    btn = _button(primary=True)
    qtbot.addWidget(btn)
    btn.setAccessibleName(REST)
    btn.setAccessibleDescription("mô tả nghỉ")
    btn.set_running(True, RUN, RUN_DESC)
    assert btn.running is True
    assert btn.text() == RUN
    assert btn.accessibleName() == RUN
    assert btn.accessibleDescription() == RUN_DESC
    assert btn.isEnabled(), "đang chạy không setEnabled(False)"
    btn.set_running(False)
    assert btn.running is False
    assert btn.text() == REST
    assert btn.accessibleName() == REST
    assert btn.accessibleDescription() == "mô tả nghỉ"


def test_set_running_without_description_keeps_description(qtbot):
    btn = _button(primary=True)
    qtbot.addWidget(btn)
    btn.setAccessibleDescription("mô tả nghỉ")
    btn.set_running(True, RUN)
    assert btn.accessibleDescription() == "mô tả nghỉ"
    btn.set_running(False)
    assert btn.accessibleDescription() == "mô tả nghỉ"


def test_set_running_again_only_changes_texts_and_stop_restores_rest(qtbot):
    btn = _button(primary=True)
    qtbot.addWidget(btn)
    btn.setAccessibleName(REST)
    btn.set_running(True, RUN, RUN_DESC)
    btn.set_running(True, "Pairing…", "Waiting.")
    assert btn.running is True
    assert (btn.text(), btn.accessibleName(), btn.accessibleDescription()) == (
        "Pairing…",
        "Pairing…",
        "Waiting.",
    )
    btn.set_running(False)
    assert (btn.text(), btn.accessibleName(), btn.accessibleDescription()) == (REST, REST, "")


def test_set_running_false_when_idle_does_nothing(qtbot):
    btn = _button(primary=True)
    qtbot.addWidget(btn)
    btn.setAccessibleName("tên")
    btn.setDefault(True)
    btn.set_running(False)
    assert btn.running is False
    assert btn.text() == REST and btn.accessibleName() == "tên" and btn.isDefault()


def test_running_resets_hover_and_press_levels(qtbot, ui_theme):
    ui_theme(False)
    btn = _button(primary=True)
    host = _host(qtbot, btn)
    _leave(host)
    _hover(btn)
    assert btn.hover_level() == 1
    btn.set_running(True, RUN)
    assert btn.hover_level() == 0 and btn.press_level() == 0
    _leave(host)
    _hover(btn)
    assert btn.hover_level() == 0, "đang chạy: h = 0 dù chuột ở trên"
    QTest.mousePress(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    assert btn.press_level() == 0
    assert btn.content_offset() == 0, "đang chạy không lún"
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton, pos=btn.rect().center())
    btn.set_running(False)
    assert btn.hover_level() == 1, "dừng chạy khi chuột còn ở trên: h về 1"


def test_running_button_swallows_mouse_keys_and_python_clicks(qtbot, ui_theme):
    ui_theme(False)
    btn = _button(primary=True)
    host = _host(qtbot, btn)
    _leave(host)
    _activate(qtbot, host)
    btn.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(btn.hasFocus, timeout=WAIT_MS)
    seen = _clicks(btn)
    btn.set_running(True, RUN, RUN_DESC)
    center = btn.rect().center()
    QTest.mouseClick(btn, Qt.MouseButton.LeftButton, pos=center)
    QTest.mouseDClick(btn, Qt.MouseButton.LeftButton, pos=center)
    QTest.mouseRelease(btn, Qt.MouseButton.LeftButton, pos=center)  # mouseDClick không nhả lần cuối
    for key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Select):
        QTest.keyClick(btn, key)
    btn.click()
    btn.animateClick()
    qtbot.wait(250)  # animateClick nhả sau ~100 ms
    assert seen == [], "nút đang chạy không được phát clicked"
    assert btn.isDown() is False
    assert btn.hasFocus(), "tiêu điểm ở lại trên nút đang chạy"
    assert btn.isEnabled()

    btn.set_running(False)
    QTest.mouseClick(btn, Qt.MouseButton.LeftButton, pos=center)
    assert seen == [True], "dừng chạy: bấm lại phát clicked"
    QTest.keyClick(btn, Qt.Key.Key_Space)
    assert seen == [True, True]
    btn.click()
    assert seen == [True, True, True]


@pytest.mark.parametrize("kind", ["shortcut", "mnemonic"])
def test_running_button_ignores_its_shortcut(qtbot, ui_theme, kind):
    """3.3: `event()` nuốt `Shortcut` khi đang chạy (phím tắt gán bằng `setShortcut` hay `&` trong chữ)."""
    if kind == "mnemonic" and sys.platform == "darwin":
        pytest.skip("macOS không bật phím tắt `&` của nút")
    ui_theme(False)
    other = QLineEdit()
    btn = _button("&Nhận" if kind == "mnemonic" else REST, primary=True)
    if kind == "shortcut":
        btn.setShortcut(QKeySequence("Ctrl+K"))
    host = _host(qtbot, other, btn)
    _leave(host)
    _activate(qtbot, host)
    other.setFocus()
    qtbot.waitUntil(other.hasFocus, timeout=WAIT_MS)
    seen = _clicks(btn)

    def press() -> None:
        if kind == "shortcut":
            QTest.keyClick(other, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
        else:
            QTest.keyClick(other, Qt.Key.Key_N, Qt.KeyboardModifier.AltModifier)
        qtbot.wait(250)  # phím tắt của nút đi qua animateClick: clicked phát sau ~100 ms

    press()
    assert seen == [True], "trước khi chạy: phím tắt bấm được nút"
    # `QAbstractButton.setText` thay phím tắt bằng mnemonic của chữ mới (hành vi Qt, như QPushButton
    # thường), nên ca `setShortcut` gán lại phím tắt sau mỗi lần đổi chữ.
    btn.set_running(True, RUN)
    if kind == "shortcut":
        btn.setShortcut(QKeySequence("Ctrl+K"))  # phím tắt vẫn còn trên nút đang chạy
    press()
    assert seen == [True], "đang chạy: phím tắt không bấm được nút"
    btn.set_running(False)
    if kind == "shortcut":
        btn.setShortcut(QKeySequence("Ctrl+K"))
    press()
    assert seen == [True, True], "dừng chạy: phím tắt bấm lại được"


def test_running_removes_default_so_dialog_enter_does_not_click(qtbot, ui_theme):
    from teleport.ui.buttons import AnimatedButton

    ui_theme(False)
    dialog = QDialog()
    qtbot.addWidget(dialog)
    edit = QLineEdit(dialog)
    btn = AnimatedButton(REST, dialog, primary=True)
    row = QHBoxLayout(dialog)
    row.addWidget(edit)
    row.addWidget(btn)
    btn.setAutoDefault(True)
    btn.setDefault(True)
    dialog.show()
    qtbot.waitExposed(dialog)
    _activate(qtbot, dialog)
    edit.setFocus()
    qtbot.waitUntil(edit.hasFocus, timeout=WAIT_MS)
    seen = _clicks(btn)

    QTest.keyClick(edit, Qt.Key.Key_Return)
    assert seen == [True], "trước khi chạy: Enter của QDialog bấm nút mặc định"

    btn.set_running(True, RUN)
    assert btn.isDefault() is False
    assert btn.autoDefault() is False
    QTest.keyClick(edit, Qt.Key.Key_Return)
    QTest.keyClick(edit, Qt.Key.Key_Enter)
    assert seen == [True], "đang chạy: Enter của QDialog không bấm được nút"

    btn.set_running(False)
    assert btn.isDefault() is True
    assert btn.autoDefault() is True
    QTest.keyClick(edit, Qt.Key.Key_Return)
    assert seen == [True, True], "dừng chạy: nút mặc định trở lại"


# =================================================================================================
# 7.1.8 setText / setAccessibleName khi đang chạy
# =================================================================================================


def test_set_text_while_running_goes_to_rest_copy(qtbot, ui_theme):
    ui_theme(False)
    btn = _button(primary=True)
    qtbot.addWidget(btn)
    btn.set_running(True, RUN)
    btn.setText("B")
    assert btn.text() == RUN, "chữ đang chạy giữ nguyên"
    btn.set_running(False)
    assert btn.text() == "B"
    ref = _reference("B", primary=True)
    qtbot.addWidget(ref)
    assert btn.sizeHint() == ref.sizeHint(), "H6: cỡ nghỉ tính theo chữ nghỉ mới"


def test_set_accessible_name_while_running_goes_to_rest_copy(qtbot):
    btn = _button(primary=True)
    qtbot.addWidget(btn)
    btn.setAccessibleName(REST)
    btn.set_running(True, RUN)
    btn.setAccessibleName("Tên mới")
    assert btn.accessibleName() == RUN
    btn.set_running(False)
    assert btn.accessibleName() == "Tên mới"


# =================================================================================================
# 7.1.9 Cỡ khi chạy (H6)
# =================================================================================================


@MODES
@pytest.mark.parametrize("primary", [True, False], ids=["chinh", "phu"])
def test_running_size_keeps_height_and_only_grows(qtbot, ui_theme, dark, primary):
    ui_theme(dark)
    btn = _button(primary=primary)
    qtbot.addWidget(btn)
    rest = btn.sizeHint()

    btn.set_running(True, "A")
    short = btn.sizeHint()
    assert short.height() == rest.height()
    assert short.width() >= rest.width(), "không co"

    long_text = "Đang ghép đôi với một máy có tên khá dài…"
    btn.set_running(True, long_text)
    grown = btn.sizeHint()
    assert grown.height() == rest.height()
    assert grown.width() > btn.fontMetrics().horizontalAdvance(long_text), "đủ chỗ cho chữ + spinner"
    assert grown.width() >= rest.width()

    btn.set_running(False)
    ref = _reference(REST, primary=primary)
    qtbot.addWidget(ref)
    assert btn.sizeHint() == ref.sizeHint(), "dừng chạy: cỡ về đúng QPushButton"


# =================================================================================================
# 7.1.10 Spinner (real_motion), N3
# =================================================================================================


@pytest.fixture
def spinning(qtbot, ui_theme, real_motion):
    """(host, btn): nút chính đang chạy trong một cửa sổ đang hiện, spinner đang quay."""
    ui_theme(False)
    btn = _button(primary=True)
    host = _host(qtbot, btn)
    _leave(host)
    btn.set_running(True, RUN, RUN_DESC)
    QApplication.processEvents()
    return host, btn


def test_spinner_runs_while_running(spinning, real_motion):
    btn = spinning[1]
    assert btn.spinner_active() is True
    assert real_motion.running_count() >= 1


def test_spinner_stops_when_running_ends(qtbot, spinning, real_motion):
    btn = spinning[1]
    btn.set_running(False)
    assert btn.spinner_active() is False
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_spinner_stops_when_window_hidden_and_resumes_when_shown(qtbot, spinning, real_motion):
    host, btn = spinning
    host.hide()
    qtbot.waitUntil(lambda: not btn.spinner_active(), timeout=WAIT_MS)
    assert btn.running is True
    assert btn.text() == RUN
    assert real_motion.running_count() == 0
    host.show()
    qtbot.waitExposed(host)
    qtbot.waitUntil(btn.spinner_active, timeout=WAIT_MS)
    assert btn.running is True


def test_spinner_stops_when_button_hidden_and_resumes_when_shown(qtbot, spinning, real_motion):
    btn = spinning[1]
    btn.hide()
    qtbot.waitUntil(lambda: not btn.spinner_active(), timeout=WAIT_MS)
    assert real_motion.running_count() == 0
    btn.show()
    QApplication.processEvents()
    qtbot.waitUntil(btn.spinner_active, timeout=WAIT_MS)


def test_spinner_stops_when_minimized_and_resumes_when_restored(qtbot, spinning, real_motion):
    """H3: cửa sổ thu nhỏ → dừng; hết thu nhỏ (`WindowStateChange`) → quay lại."""
    host, btn = spinning
    host.setWindowState(host.windowState() | Qt.WindowState.WindowMinimized)
    assert host.isMinimized()
    qtbot.waitUntil(lambda: not btn.spinner_active(), timeout=WAIT_MS)
    assert real_motion.running_count() == 0
    assert btn.running is True
    host.setWindowState(Qt.WindowState.WindowNoState)
    assert not host.isMinimized()
    QApplication.processEvents()
    qtbot.waitUntil(btn.spinner_active, timeout=WAIT_MS)


def test_spinner_started_while_window_hidden_spins_once_shown(qtbot, ui_theme, real_motion):
    """H3: `set_running(True)` khi cửa sổ đang ẩn → chưa quay; cửa sổ hiện → quay."""
    ui_theme(False)
    btn = _button(primary=True)
    host = _host(qtbot, btn)
    host.hide()
    btn.set_running(True, RUN)
    assert btn.spinner_active() is False
    assert btn.running is True
    host.show()
    qtbot.waitExposed(host)
    qtbot.waitUntil(btn.spinner_active, timeout=WAIT_MS)


def test_spinner_capped_stops_and_never_restarts(qtbot, ui_theme, real_motion, monkeypatch):
    monkeypatch.setattr(real_motion, "MAX_LOOP_MS", 200)
    ui_theme(False)
    btn = _button(primary=True)
    host = _host(qtbot, btn)
    _leave(host)
    btn.set_running(True, RUN, RUN_DESC)
    assert btn.spinner_active() is True
    qtbot.waitUntil(lambda: not btn.spinner_active(), timeout=WAIT_MS)
    assert real_motion.running_count() == 0
    assert btn.running is True
    assert btn.text() == RUN, "chạm trần: chữ “Đang …” vẫn còn"
    # Hiện lại (ẩn/hiện cửa sổ, thu nhỏ/khôi phục) không làm spinner quay lại sau khi chạm trần.
    host.hide()
    host.show()
    qtbot.waitExposed(host)
    host.setWindowState(host.windowState() | Qt.WindowState.WindowMinimized)
    host.setWindowState(Qt.WindowState.WindowNoState)
    qtbot.wait(150)
    assert btn.spinner_active() is False
    assert real_motion.running_count() == 0
    assert btn.text() == RUN


def test_spinner_after_finish_all_does_not_restart(qtbot, spinning, real_motion):
    host, btn = spinning
    real_motion.finish_all()
    assert btn.spinner_active() is False
    assert real_motion.running_count() == 0
    host.hide()
    host.show()
    qtbot.waitExposed(host)
    qtbot.wait(100)
    assert btn.spinner_active() is False
    assert btn.running is True


def test_spinner_cap_is_cleared_by_stopping(qtbot, ui_theme, real_motion, monkeypatch):
    """Cờ chạm trần xóa khi `set_running(False)`: lần chạy sau quay lại."""
    monkeypatch.setattr(real_motion, "MAX_LOOP_MS", 200)
    ui_theme(False)
    btn = _button(primary=True)
    host = _host(qtbot, btn)
    _leave(host)
    btn.set_running(True, RUN)
    qtbot.waitUntil(lambda: not btn.spinner_active(), timeout=WAIT_MS)
    btn.set_running(False)
    btn.set_running(True, RUN)
    assert btn.spinner_active() is True


def test_spinner_off_in_instant_mode(qtbot, ui_theme):
    from teleport.ui import motion

    ui_theme(False)
    btn = _button(primary=True)
    _host(qtbot, btn)
    btn.set_running(True, RUN)
    assert btn.running is True
    assert btn.spinner_active() is False
    assert motion.running_count() == 0


def test_spinner_off_when_os_disables_animations(qtbot, ui_theme, real_motion, monkeypatch):
    monkeypatch.setattr(real_motion, "animations_enabled", lambda: False)
    ui_theme(False)
    btn = _button(primary=True)
    _host(qtbot, btn)
    btn.set_running(True, RUN)
    assert btn.running is True
    assert btn.spinner_active() is False
    assert real_motion.running_count() == 0


def test_running_button_still_grabs_with_static_frame(qtbot, ui_theme):
    """Tức thì: khung tĩnh vẫn vẽ được (không lỗi), chữ đang chạy hiện trên nút."""
    ui_theme(False)
    btn = _button(primary=True)
    _host(qtbot, btn)
    rest = _grab(btn)
    btn.set_running(True, RUN)
    QApplication.processEvents()
    running = _grab(btn)
    assert not running.isNull()
    assert running != rest


# =================================================================================================
# Sửa sau soat-code (G2): rò bộ nhớ, nút mặc định bật lại, phím tắt
# =================================================================================================


def _drain_deletes() -> None:
    import gc

    from PySide6.QtCore import QCoreApplication, QEvent

    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QApplication.processEvents()


@pytest.mark.parametrize("running", [False, True], ids=["nghi", "da-chay"])
def test_parentless_button_is_destroyed_after_del(qtbot, running):
    """Nút không cha không bị giữ sống bởi vòng tham chiếu qua slot C++ (`ButtonLevels`)."""
    from teleport.ui.buttons import AnimatedButton

    _drain_deletes()
    before = sum(isinstance(w, AnimatedButton) for w in QApplication.allWidgets())
    for _ in range(5):
        btn = AnimatedButton("Tạm", primary=True)
        btn.show()
        QApplication.processEvents()
        if running:
            btn.set_running(True, RUN)
            btn.set_running(False)
        del btn
    _drain_deletes()
    after = sum(isinstance(w, AnimatedButton) for w in QApplication.allWidgets())
    assert after == before


def test_running_main_default_is_not_clicked_by_return_after_focus_moves(qtbot):
    """`QDialog` đặt lại `mainDef` bằng C++ khi tiêu điểm rời một nút autoDefault: Return trong ô nhập
    vẫn không được bấm nút đang chạy."""
    from teleport.ui.buttons import AnimatedButton

    dialog = QDialog()
    qtbot.addWidget(dialog)
    row = QHBoxLayout(dialog)
    edit = QLineEdit(dialog)
    other = QPushButton("Khác", dialog)
    other.setAutoDefault(True)
    runner = AnimatedButton(REST, dialog, primary=True)
    runner.setDefault(True)
    for w in (edit, other, runner):
        row.addWidget(w)
    dialog.show()
    qtbot.waitExposed(dialog)
    seen = _clicks(runner)
    runner.set_running(True, RUN)
    other.setFocus()
    QApplication.processEvents()
    edit.setFocus()
    QApplication.processEvents()
    QTest.keyClick(edit, Qt.Key.Key_Return)
    QApplication.processEvents()
    assert seen == []
    runner.set_running(False)
    assert runner.isDefault() is True, "trả lại nút mặc định khi dừng"


def test_set_default_while_running_is_kept_for_rest(qtbot):
    from teleport.ui.buttons import AnimatedButton

    dialog = QDialog()
    qtbot.addWidget(dialog)
    btn = AnimatedButton(REST, dialog)
    btn.set_running(True, RUN)
    btn.setDefault(True)
    assert btn.isDefault() is False, "đang chạy: không thành nút mặc định"
    btn.set_running(False)
    assert btn.isDefault() is True


def test_custom_shortcut_survives_running(qtbot):
    btn = _button("&Lưu")
    qtbot.addWidget(btn)
    btn.setShortcut(QKeySequence("Ctrl+K"))
    btn.set_running(True, RUN)
    btn.set_running(True, "Đang lưu…")
    btn.set_running(False)
    assert btn.shortcut() == QKeySequence("Ctrl+K")
    assert btn.text() == "&Lưu"
