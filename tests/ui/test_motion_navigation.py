"""Chuyển động điều hướng (spec G3, đợt G chuyển động): T1–T20 của mục 5, chi tiết ở mục 10 (thắng).

Viết từ spec, không từ code. Ràng buộc kiểm (mvp N3, N12): trạng thái logic và tiêu điểm đổi **ngay**,
lớp phủ không nhận chuột/tiêu điểm, tắt hiệu ứng thì nhảy thẳng trạng thái cuối, hết hoạt ảnh thì
`motion.running_count() == 0`.

- Mặc định ở chế độ tức thì (autouse `motion_instant`); test hoạt ảnh xin `real_motion`, cửa sổ `show()`
  + `waitExposed`. Chỉ kiểm giá trị cuối sau `W`/`N` (thêm `waitUntil` cho máy chậm), trừ “có hoạt ảnh
  đang chạy” ngay sau lời gọi.
- Đếm overlay: con trực tiếp tên `motionOverlay` của đúng `pages` hay `stream_pane` (mục 10.9).
- Test hoạt ảnh đổi trang dùng một trang mẫu (WAIT_PAIRING) thay vì DISCOVER: trang Tìm máy chạy sóng
  radar (`motion.Loop`, spec G6) nên `running_count()` không về 0 trong vài giây. T1/T3 (không có hoạt
  ảnh nào chạy) vẫn dùng DISCOVER thật như spec.

Module `teleport.ui.*` chỉ import bên trong test/fixture.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QRectF, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

QWIDGETSIZE_MAX = (1 << 24) - 1
WAIT_MS = 3000
PEER_ID = "a71c04d2" + "e" * 24


# =================================================================================================
# Hàm phụ trợ
# =================================================================================================


def is_valid(obj) -> bool:
    import shiboken6

    return shiboken6.isValid(obj)


def overlays(container: QWidget) -> list[QWidget]:
    """Overlay của `Fader` là con trực tiếp của `container` (mục 10.9)."""
    found = container.findChildren(QWidget, "motionOverlay", Qt.FindChildOption.FindDirectChildrenOnly)
    return [w for w in found if is_valid(w)]


def visible_overlays(container: QWidget) -> list[QWidget]:
    return [w for w in overlays(container) if w.isVisible()]


def show_fades(window: QWidget) -> list[QObject]:
    return [o for o in window.findChildren(QObject, "motionShowFade") if is_valid(o)]


def W() -> int:
    from teleport.ui import motion

    return motion.SLOW.ms + 200


def N() -> int:
    from teleport.ui import motion

    return motion.NORMAL.ms + 200


def settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


def after(qtbot, ms: int, predicate, what: str = "") -> None:
    """Chờ `ms` rồi (cho máy chậm) chờ thêm tới khi `predicate()` đúng."""
    qtbot.wait(ms)
    qtbot.waitUntil(lambda: bool(predicate()), timeout=WAIT_MS)


def idle(qtbot, motion) -> None:
    after(qtbot, W(), lambda: motion.running_count() == 0)


def activate(qtbot, window: QWidget) -> None:
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    window.raise_()
    qtbot.waitUntil(window.isActiveWindow, timeout=WAIT_MS)
    settle()


def focus_in(root: QWidget) -> bool:
    focus = QApplication.focusWidget()
    return focus is not None and (focus is root or root.isAncestorOf(focus))


def record(signal) -> list[str]:
    seen: list[str] = []
    signal.connect(lambda *args: seen.append(str(args[0]) if args else ""))
    return seen


def sample_page(label: str = "W") -> tuple[QWidget, QPushButton]:
    page = QWidget()
    lay = QVBoxLayout(page)
    button = QPushButton(f"Nút của trang {label}")
    button.setObjectName(f"button{label}")
    button.setMinimumHeight(120)
    lay.addWidget(button)
    page.setFocusProxy(button)
    return page, button


def disable_animations(motion, monkeypatch) -> None:
    """Hệ điều hành tắt hiệu ứng (sau `real_motion`; lần vá sau thắng)."""
    monkeypatch.setattr(motion, "animations_enabled", lambda: False)


def long_token(motion):
    return motion.Token(10_000, QEasingCurve.Type.OutCubic)


def close_color(a: QColor, b: QColor, tol: int = 3) -> bool:
    return (
        abs(a.red() - b.red()) <= tol
        and abs(a.green() - b.green()) <= tol
        and abs(a.blue() - b.blue()) <= tol
    )


def dview(label: str, *, did: str | None = None, paired: bool = True):
    import hashlib

    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=did or hashlib.sha256(label.encode()).hexdigest()[:32],
        name=label,
        os="windows",
        paired=paired,
        online=True,
        connected=False,
        addresses=("192.0.2.7",),
        port=47474,
    )


# =================================================================================================
# Fixture
# =================================================================================================


@pytest.fixture(autouse=True)
def drain_animations(qtbot):
    """Sau mỗi test: dừng mọi hoạt ảnh (không callback) rồi xử lý `deleteLater` đang chờ."""
    yield
    from teleport.ui import motion as m

    m._stop_all_for_tests()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qtbot.wait(1)


@pytest.fixture
def win(qtbot, tmp_path):
    """`MainWindow` ở RUNNING, có thêm trang mẫu WAIT_PAIRING, 980x660, đang hiện và active."""
    from teleport.ui.main_window import MainWindow, Page

    w = MainWindow()
    qtbot.addWidget(w)
    page, button = sample_page("W")
    w.add_page(Page.WAIT_PAIRING, page)
    w.show_running("May-A", tmp_path / "nhan")
    w.resize(980, 660)
    activate(qtbot, w)
    start = w.conversations.add_button
    start.setFocus(Qt.FocusReason.OtherFocusReason)
    settle()
    return SimpleNamespace(window=w, page=page, button=button, pages=w.pages, start=start)


class Conv:
    """`ConversationList` đứng riêng, có `DeviceListModel` thật, đang hiện."""

    def __init__(self, qtbot) -> None:
        from teleport.ui.conversations import ConversationList
        from teleport.ui.radar import DeviceListModel

        self.host = QWidget()
        qtbot.addWidget(self.host)
        self.devices = DeviceListModel(self.host)
        self.widget = ConversationList(self.devices, self.host)
        lay = QVBoxLayout(self.host)
        lay.addWidget(self.widget)
        self.host.resize(320, 700)
        self.views = [dview(f"May-{c}") for c in "ABCD"]
        self.devices.reset(self.views)
        self.host.show()
        qtbot.waitExposed(self.host)
        settle()

    @property
    def view(self):
        return self.widget.list_view

    def device_at(self, row: int) -> str:
        device_id = self.widget.device_id_at(row)
        assert device_id is not None
        return device_id

    def cell(self, row: int) -> QRectF:
        from teleport.ui.conversations import _row_cell

        index = self.view.model().index(row, 0)
        return _row_cell(QRectF(self.view.visualRect(index)), self.widget.compact)

    def select_row(self, row: int) -> None:
        assert self.widget.select_device(self.device_at(row))

    def repaint(self) -> None:
        self.view.viewport().repaint()
        settle()


@pytest.fixture
def conv(qtbot):
    return Conv(qtbot)


@pytest.fixture
def settings_page(qtbot):
    """`SettingsPage` đứng riêng 980x660, đang hiện, đã nạp giá trị mặc định."""
    from teleport.app.autostart import AutostartState
    from teleport.ui.radar import DeviceListModel
    from teleport.ui.settings import SettingsPage, SettingsSnapshot

    model = DeviceListModel()
    page = SettingsPage(model)
    model.setParent(page)
    qtbot.addWidget(page)
    page.resize(980, 660)
    page.set_loaded(True)
    page.set_values(
        SettingsSnapshot(
            name="May-A",
            visible=True,
            rate_limit=None,
            temp_retention_days=None,
            language=None,
            autostart=False,
            devices={},
        )
    )
    page.set_autostart(AutostartState.OFF)
    page.show()
    qtbot.waitExposed(page)
    settle()
    return page


@pytest.fixture
def bar_host(qtbot):
    """InfoBar trong một cửa sổ (có cha, có layout), kèm ô nhập phía sau; cửa sổ chưa hiện."""
    from teleport.ui.widgets import InfoBar

    host = QWidget()
    qtbot.addWidget(host)
    lay = QVBoxLayout(host)
    bar = InfoBar(host)
    after_edit = QLineEdit(host)
    after_edit.setObjectName("afterBar")
    lay.addWidget(bar)
    lay.addWidget(after_edit)
    lay.addStretch(1)
    host.resize(520, 300)
    return SimpleNamespace(host=host, bar=bar, edit=after_edit)


def nav_item_rect(page, row: int) -> QRectF:
    return QRectF(page.nav.visualRect(page.nav.model().index(row, 0)))


def section_row(name: str) -> int:
    from teleport.ui.settings import Section

    return list(Section).index(Section[name])


def other_section(page) -> str:
    """Tên mục khác mục đang mở."""
    from teleport.ui.settings import Section

    current = page.nav.currentRow()
    names = [s.name for s in Section]
    return names[(current + 3) % len(names)]


def repaint_nav(page) -> None:
    page.nav.viewport().repaint()
    settle()


# =================================================================================================
# T1–T7 đổi trang (mục 2.2)
# =================================================================================================


def test_t1_instant_open_page_switches_without_overlay(win):
    from teleport.ui import motion
    from teleport.ui.main_window import Page

    w = win.window
    seen = record(w.pages.page_changed)
    assert w.open_page(Page.DISCOVER) is True
    assert w.pages.current_page is Page.DISCOVER
    assert w.pages.currentWidget() is w.discover
    assert overlays(w.pages) == []
    assert seen == ["discover"]
    assert focus_in(w.discover), "tiêu điểm vào trang mới ngay"
    settle()
    assert overlays(w.pages) == []
    assert motion.running_count() == 0


def test_t2_real_open_page_changes_now_and_overlay_removes_itself(real_motion, qtbot, win):
    from teleport.ui.main_window import Page

    w = win.window
    seen = record(w.pages.page_changed)
    assert w.open_page(Page.WAIT_PAIRING) is True
    assert w.pages.current_page is Page.WAIT_PAIRING
    assert w.pages.currentWidget() is win.page
    assert seen == ["wait_pairing"]
    assert focus_in(win.page), "tiêu điểm đổi ngay, không chờ hoạt ảnh"
    found = overlays(w.pages)
    assert len(found) == 1, "đúng một overlay ảnh trang cũ"
    overlay = found[0]
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert overlay.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert real_motion.running_count() >= 1
    after(qtbot, W(), lambda: overlays(w.pages) == [] and real_motion.running_count() == 0)
    assert seen == ["wait_pairing"], "page_changed phát đúng một lần"


def test_t3_system_disabled_animations_switch_like_instant(real_motion, monkeypatch, win):
    from teleport.ui.main_window import Page

    disable_animations(real_motion, monkeypatch)
    w = win.window
    seen = record(w.pages.page_changed)
    assert w.open_page(Page.DISCOVER) is True
    assert w.pages.currentWidget() is w.discover
    assert overlays(w.pages) == []
    assert seen == ["discover"]
    assert focus_in(w.discover)
    settle()
    assert overlays(w.pages) == []
    assert real_motion.running_count() == 0


@pytest.mark.parametrize("real", [False, True], ids=["tuc-thi", "that"])
def test_t4_go_back_returns_focus_now(request, qtbot, win, real):
    from teleport.ui import motion
    from teleport.ui.main_window import Page

    if real:
        request.getfixturevalue("real_motion")
    w = win.window
    assert w.open_page(Page.WAIT_PAIRING)
    assert focus_in(win.page)
    assert w.go_back() is True
    assert w.pages.current_page is Page.HOME
    assert QApplication.focusWidget() is win.start, "về HOME: tiêu điểm trả ngay về widget đã nhớ"
    idle(qtbot, motion)
    assert overlays(w.pages) == []


def test_t5_switches_in_same_tick_keep_at_most_one_overlay(real_motion, qtbot, win):
    from teleport.ui.main_window import Page

    w = win.window
    seen = record(w.pages.page_changed)
    w.open_page(Page.WAIT_PAIRING)
    w.open_page(Page.DISCOVER)
    w.go_back()
    assert len(visible_overlays(w.pages)) <= 1
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert len(overlays(w.pages)) <= 1
    assert seen == ["wait_pairing", "discover", "wait_pairing"]
    assert w.pages.current_page is Page.WAIT_PAIRING
    assert focus_in(win.page)
    after(qtbot, W(), lambda: overlays(w.pages) == [])
    assert w.pages.current_page is Page.WAIT_PAIRING
    assert w.pages.currentWidget() is win.page


def test_t6_overlay_takes_no_mouse_and_no_tab(real_motion, qtbot, win):
    from teleport.ui.main_window import Page

    w = win.window
    w.open_page(Page.WAIT_PAIRING)
    (overlay,) = overlays(w.pages)
    point = win.button.mapTo(w.pages, win.button.rect().center())
    assert overlay.geometry().contains(point), "tiền đề: overlay phủ lên nút của trang mới"
    hit = w.pages.childAt(point)
    assert hit is not overlay and not (hit is not None and overlay.isAncestorOf(hit))
    assert hit is win.button, "chuột tới thẳng nút của trang mới"
    visited = []
    for _ in range(25):
        current = QApplication.focusWidget() or w
        QTest.keyClick(current, Qt.Key.Key_Tab)
        visited.append(QApplication.focusWidget())
    assert overlay not in visited
    assert not any(f is not None and overlay.isAncestorOf(f) for f in visited)
    real_motion.finish_all()
    after(qtbot, 0, lambda: overlays(w.pages) == [])


def test_t7_stream_pane_switches_now_with_overlay(real_motion, qtbot, win):
    w = win.window
    w.devices.reset([])  # ảnh chụp danh sách máy đầu tiên (mục 10.7): chưa ghép đôi máy nào
    settle()
    after(qtbot, W(), lambda: real_motion.running_count() == 0)
    assert w.stream_pane.currentWidget() is w.empty_state, "tiền đề: chưa có gì → màn rỗng"
    w.set_transfer_summary(True, 0)
    assert w.stream_pane.currentWidget() is w.transfers_area, "đổi ngay"
    assert len(overlays(w.stream_pane)) == 1
    assert real_motion.running_count() >= 1
    after(qtbot, W(), lambda: overlays(w.stream_pane) == [] and real_motion.running_count() == 0)
    assert w.stream_pane.currentWidget() is w.transfers_area


def _single_overlay_shift(container: QWidget):
    found = visible_overlays(container)
    assert len(found) == 1, f"cần đúng một overlay đang hiện, thấy {len(found)}"
    return found[0]._shift


@pytest.mark.parametrize("rtl", [False, True], ids=["ltr", "rtl"])
def test_page_overlay_shift_direction_forward_and_back(real_motion, qtbot, win, rtl):
    """Mục 2.2: đi tới → ảnh trang cũ dịch sang trái (dx < 0); quay lại → sang phải; RTL đảo dấu."""
    from teleport.ui.main_window import Page

    w = win.window
    if rtl:
        w.pages.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        assert w.pages.isRightToLeft()
    sign = -1 if rtl else 1
    w.open_page(Page.WAIT_PAIRING)
    shift = _single_overlay_shift(w.pages)
    assert sign * shift.x() < 0, f"đi tới: dx={shift.x()}"
    assert shift.y() == 0
    real_motion.finish_all()
    settle()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert w.go_back() is True
    shift = _single_overlay_shift(w.pages)
    assert sign * shift.x() > 0, f"quay lại: dx={shift.x()}"
    assert shift.y() == 0
    real_motion.finish_all()


def test_stream_pane_overlay_shift_direction(real_motion, qtbot, win):
    """Mục 2.2: sang `transfers_area` → dy < 0; về `empty_state` → dy > 0."""
    w = win.window
    w.devices.reset([])
    settle()
    after(qtbot, W(), lambda: real_motion.running_count() == 0)
    assert w.stream_pane.currentWidget() is w.empty_state
    w.set_transfer_summary(True, 0)
    shift = _single_overlay_shift(w.stream_pane)
    assert shift.y() < 0 and shift.x() == 0, f"sang luồng chat: {shift}"
    real_motion.finish_all()
    settle()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    w.set_transfer_summary(False, 0)
    assert w.stream_pane.currentWidget() is w.empty_state
    shift = _single_overlay_shift(w.stream_pane)
    assert shift.y() > 0 and shift.x() == 0, f"về màn rỗng: {shift}"
    real_motion.finish_all()


def test_t7_stream_pane_before_first_device_snapshot_is_instant(real_motion, qtbot, win):
    """Mục 10.7: chưa nhận ảnh chụp danh sách máy đầu tiên thì đổi khung phải tức thì."""
    w = win.window
    target = w.empty_state if w.stream_pane.currentWidget() is w.transfers_area else w.transfers_area
    w.set_transfer_summary(target is w.transfers_area, 0)
    assert w.stream_pane.currentWidget() is target
    assert overlays(w.stream_pane) == []


# =================================================================================================
# T8–T10 thanh chọn danh sách cuộc trò chuyện (mục 2.3)
# =================================================================================================


def test_t8_instant_selection_indicator_sits_on_selected_row(conv):
    from teleport.ui import motion

    conv.select_row(2)
    conv.repaint()
    indicator = conv.view.selection_indicator
    assert isinstance(indicator, motion.SlidingRect)
    assert indicator.rect == conv.cell(2)
    assert indicator.end == conv.cell(2)
    assert motion.running_count() == 0


def test_t9_real_selection_slides_to_new_row_and_paints_selected(real_motion, qtbot, ui_theme, conv):
    tok = ui_theme(False)
    settle()
    conv.select_row(0)
    conv.repaint()
    indicator = conv.view.selection_indicator
    assert indicator.rect == conv.cell(0), "lần vẽ đầu: nhảy thẳng"
    conv.select_row(2)
    conv.repaint()
    assert real_motion.running_count() >= 1, "người dùng đổi máy: thanh chọn trượt"
    assert indicator.end == conv.cell(2)
    after(qtbot, N(), lambda: real_motion.running_count() == 0)
    conv.repaint()
    assert indicator.rect == conv.cell(2)
    real_motion.finish_all()
    cell = conv.cell(2)
    image = conv.view.viewport().grab().toImage()
    dpr = image.devicePixelRatio()
    x, y = round((cell.left() + 3) * dpr), round(cell.center().y() * dpr)
    assert close_color(image.pixelColor(x, y), tok.selected), "nền hàng chọn là màu selected"


@pytest.mark.parametrize(
    "change", ["insert_row_above", "apply_insert_above", "apply_remove_above", "compact"]
)
def test_t10_layout_change_jumps_without_animation(real_motion, qtbot, conv, change):
    """Sắp lại / thêm / bớt hàng / thu gọn: nhảy thẳng. `apply_*` đi qua `rowsInserted`/`rowsRemoved`
    (một hàng), không qua `modelReset`."""
    from teleport.app.node import DeviceOffline, DeviceOnline

    conv.select_row(2)
    conv.repaint()
    after(qtbot, N(), lambda: real_motion.running_count() == 0)
    chosen = conv.device_at(2)
    if change == "insert_row_above":
        conv.devices.reset([dview("May-0"), *conv.views])  # thêm một hàng phía trên
    elif change == "apply_insert_above":
        conv.devices.apply(DeviceOnline(device=dview("May-0")))
        assert conv.widget.model.row_of(chosen) == 3, "tiền đề: hàng chọn dời xuống một hàng"
    elif change == "apply_remove_above":
        gone = conv.views[0]
        assert conv.widget.model.row_of(gone.device_id) == 0
        conv.devices.apply(DeviceOffline(device=dview(gone.name, did=gone.device_id, paired=False)))
        assert conv.widget.model.row_of(chosen) == 1, "tiền đề: hàng chọn dời lên một hàng"
    else:
        conv.widget.set_compact(True)
    settle()
    conv.repaint()
    row = conv.widget.model.row_of(chosen)
    assert row >= 0
    assert conv.view.selection_indicator.rect == conv.cell(row), "nhảy thẳng tới hàng mới"
    assert real_motion.running_count() == 0


# =================================================================================================
# T11 vạch nhấn cột mục Cài đặt (mục 2.4)
# =================================================================================================


def test_t11_instant_show_section_moves_nav_indicator(settings_page):
    from teleport.ui import motion
    from teleport.ui.settings import Section

    page = settings_page
    name = other_section(page)
    page.show_section(Section[name])
    repaint_nav(page)
    indicator = page.nav.selection_indicator
    assert isinstance(indicator, motion.SlidingRect)
    assert indicator.rect is not None
    assert nav_item_rect(page, section_row(name)).contains(indicator.rect.center())
    assert motion.running_count() == 0


def test_t11_real_show_section_slides_nav_indicator(real_motion, qtbot, settings_page):
    from teleport.ui.settings import Section

    page = settings_page
    repaint_nav(page)
    indicator = page.nav.selection_indicator
    assert indicator.rect is not None, "tiền đề: mục đầu đã có vạch"
    name = other_section(page)
    page.show_section(Section[name])
    repaint_nav(page)
    assert real_motion.running_count() >= 1, "đổi mục khi đang hiện: vạch trượt"
    after(qtbot, N(), lambda: real_motion.running_count() == 0)
    repaint_nav(page)
    assert nav_item_rect(page, section_row(name)).contains(indicator.rect.center())
    assert page.sections.currentIndex() == section_row(name), "nội dung mục đổi tức thì (10.6)"


# =================================================================================================
# T12–T13 hộp thoại mờ dần khi hiện (mục 2.1b, 2.5)
# =================================================================================================


@pytest.fixture
def window_opacity(real_motion, monkeypatch):
    """Nền có hỗ trợ độ mờ cửa sổ (offscreen mặc định đặt ngay 1.0)."""
    monkeypatch.setattr(real_motion, "_PLATFORMS_WITHOUT_WINDOW_OPACITY", frozenset())
    return real_motion


def _dialog(qtbot) -> tuple[QDialog, QLineEdit]:
    d = QDialog()
    qtbot.addWidget(d)
    lay = QVBoxLayout(d)
    edit = QLineEdit(d)
    lay.addWidget(edit)
    d.resize(240, 120)
    return d, edit


def test_t12_fade_in_on_show_fades_and_focus_is_ready(window_opacity, qtbot):
    motion = window_opacity
    d, edit = _dialog(qtbot)
    motion.fade_in_on_show(d, long_token(motion))
    d.show()
    assert d.windowOpacity() < 1.0 or motion.running_count() >= 1, "hiện lên: đang mờ dần"
    d.activateWindow()
    edit.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(lambda: QApplication.focusWidget() is edit, timeout=WAIT_MS)
    assert motion.running_count() >= 1, "tiêu điểm có ngay, không chờ hết hoạt ảnh"
    motion.finish_all()
    assert d.windowOpacity() == pytest.approx(1.0)
    assert motion.running_count() == 0


def test_t12_fade_in_on_show_default_token_ends_at_full_opacity(window_opacity, qtbot):
    motion = window_opacity
    d, _ = _dialog(qtbot)
    motion.fade_in_on_show(d)
    d.show()
    assert d.windowOpacity() < 1.0 or motion.running_count() >= 1
    after(qtbot, W(), lambda: motion.running_count() == 0)
    assert d.windowOpacity() == pytest.approx(1.0)


def test_t12_fade_in_on_show_twice_installs_one_filter(window_opacity, qtbot):
    motion = window_opacity
    d, _ = _dialog(qtbot)
    motion.fade_in_on_show(d)
    motion.fade_in_on_show(d)
    assert len(show_fades(d)) == 1


def test_t12_fade_in_on_show_fades_again_when_shown_again(window_opacity, qtbot):
    """Giả định 10.16: hộp hiện lại (không do HĐH) thì mờ lại."""
    motion = window_opacity
    d, _ = _dialog(qtbot)
    motion.fade_in_on_show(d, long_token(motion))
    d.show()
    motion.finish_all()
    d.hide()
    settle()
    d.show()
    assert d.windowOpacity() < 1.0 or motion.running_count() >= 1
    motion.finish_all()
    assert d.windowOpacity() == pytest.approx(1.0)


def test_t12_embedded_dialog_does_not_fade(window_opacity, qtbot):
    motion = window_opacity
    host = QWidget()
    qtbot.addWidget(host)
    lay = QVBoxLayout(host)
    embedded = QDialog(host)
    embedded.setWindowFlags(Qt.WindowType.Widget)
    lay.addWidget(embedded)
    host.resize(300, 200)
    motion.fade_in_on_show(embedded)
    host.show()
    qtbot.waitExposed(host)
    embedded.show()
    settle()
    assert not embedded.isWindow()
    assert motion.running_count() == 0
    assert host.windowOpacity() == pytest.approx(1.0)


def _pairing_prompt():
    from teleport.app.pairing import PairingPrompt
    from teleport.core.pairing import Role

    return PairingPrompt(
        session_id=7,
        role=Role("responder"),
        address="192.168.1.5",
        peer_name="MacBook",
        peer_device_id=PEER_ID,
        code="905617",
        expires_at=1000.0 + 60.0,
        recent_codes=1,
        known_name=None,
    )


def test_t13_offer_box_has_show_fade(qtbot):
    from teleport.ui.offer import _OfferBox

    box = _OfferBox(lambda: None)
    qtbot.addWidget(box)
    assert len(show_fades(box)) == 1


def test_t13_pairing_dialogs_have_show_fade(qtbot):
    from teleport.ui.pairing import PairingDialog, PairingProgressDialog

    dialog = PairingDialog(_pairing_prompt(), clock=lambda: 1000.0)
    progress = PairingProgressDialog("May-B")
    qtbot.addWidget(dialog)
    qtbot.addWidget(progress)
    assert len(show_fades(dialog)) == 1
    assert len(show_fades(progress)) == 1


def test_t13_pairing_message_box_has_show_fade(qtbot):
    from teleport.ui.pairing import _new_box

    box = _new_box(None, lambda _box: None)
    qtbot.addWidget(box)
    assert len(show_fades(box)) == 1


def test_t13_clear_history_box_has_show_fade(qtbot, tmp_path, settings_page):
    from teleport.ui.settings import SettingsController

    ctrl = SettingsController(settings_page, tmp_path)
    ctrl.request_clear_history()
    box = ctrl.confirm_box
    assert box is not None
    try:
        assert len(show_fades(box)) == 1
    finally:
        box.close()


@pytest.fixture
def no_exec(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a, **k: 0)
    monkeypatch.setattr(QDialog, "exec", lambda self, *a, **k: 0)


@pytest.fixture
def fake_tray_icon(monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon

    from teleport.ui import tray

    shown: dict[int, bool] = {}
    monkeypatch.setattr(tray, "is_available", lambda: True)
    monkeypatch.setattr(QSystemTrayIcon, "show", lambda self: shown.__setitem__(id(self), True))
    monkeypatch.setattr(QSystemTrayIcon, "hide", lambda self: shown.__setitem__(id(self), False))
    monkeypatch.setattr(QSystemTrayIcon, "setVisible", lambda self, v: shown.__setitem__(id(self), bool(v)))
    monkeypatch.setattr(QSystemTrayIcon, "isVisible", lambda self: shown.get(id(self), False))
    monkeypatch.setattr(QSystemTrayIcon, "showMessage", lambda self, *a, **k: None)


def test_t13_quit_box_has_show_fade(qapp, qtbot, config_dir, fake_tray_icon, no_exec):
    import contextlib

    from teleport.ui.app import GuiApp

    controller = GuiApp(qapp, config_dir("A", identity=False))
    try:
        controller.window.show()
        controller._confirm_quit(["PC-Nha"])
        box = controller.quit_box
        assert box is not None
        assert len(show_fades(box)) == 1
    finally:
        box = controller.quit_box
        if box is not None:
            with contextlib.suppress(Exception):
                box.close()
        with contextlib.suppress(Exception):
            controller.shutdown()
        with contextlib.suppress(Exception):
            controller.window.hide()
            controller.window.deleteLater()
        qapp.processEvents()


# =================================================================================================
# T14–T18 InfoBar mở/đóng theo chiều cao (mục 2.6, 10.2–10.3, 10.13)
# =================================================================================================


def test_t14_instant_show_and_clear_change_state_now(qtbot, bar_host):
    from teleport.ui import motion
    from teleport.ui.widgets import InfoLevel

    bar = bar_host.bar
    activate(qtbot, bar_host.host)
    assert bar.is_open is False
    bar.show_message(InfoLevel.INFO, "Thông báo")
    assert bar.is_open is True
    assert bar.isVisible()
    bar.clear()
    assert bar.is_open is False
    assert bar.isHidden()
    assert motion.running_count() == 0


def test_t15_real_open_then_close_by_height(real_motion, qtbot, bar_host):
    from teleport.ui.widgets import InfoLevel

    bar = bar_host.bar
    activate(qtbot, bar_host.host)
    bar.show_message(InfoLevel.INFO, "Thông báo", "Dòng giải thích dài hơn một chút.")
    assert bar.is_open is True, "trạng thái logic đổi ngay"
    assert real_motion.running_count() >= 1, "cha đang hiện: mở có hoạt ảnh"
    after(qtbot, N(), lambda: real_motion.running_count() == 0)
    assert bar.maximumHeight() == QWIDGETSIZE_MAX
    assert bar.height() > 0
    assert bar.isVisible()
    bar.clear()
    assert bar.is_open is False, "trạng thái logic đổi ngay"
    after(qtbot, N(), lambda: bar.isHidden() and real_motion.running_count() == 0)
    assert bar.maximumHeight() == QWIDGETSIZE_MAX


@pytest.mark.parametrize("real", [False, True], ids=["tuc-thi", "that"])
def test_t16_close_button_by_space_moves_focus_out_now(request, qtbot, bar_host, real):
    from teleport.ui import motion
    from teleport.ui.widgets import InfoLevel

    if real:
        request.getfixturevalue("real_motion")
    bar = bar_host.bar
    activate(qtbot, bar_host.host)
    bar.show_message(InfoLevel.ERROR, "Lỗi")
    motion.finish_all()
    settle()
    dismissed: list[bool] = []
    bar.dismissed.connect(lambda: dismissed.append(True))
    bar.close_button.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(lambda: QApplication.focusWidget() is bar.close_button, timeout=WAIT_MS)
    QTest.keyClick(bar.close_button, Qt.Key.Key_Space)
    assert QApplication.focusWidget() is bar_host.edit, "tiêu điểm sang widget kế tiếp ngoài thanh, ngay"
    assert bar.is_open is False
    assert dismissed == [True]
    idle(qtbot, motion)
    assert bar.isHidden()
    assert dismissed == [True], "dismissed phát đúng một lần"


@pytest.mark.parametrize("real", [False, True], ids=["tuc-thi", "that"])
def test_t16_close_without_other_focusable_widget_clears_focus(request, qtbot, real):
    from teleport.ui import motion
    from teleport.ui.widgets import InfoBar, InfoLevel

    if real:
        request.getfixturevalue("real_motion")
    host = QWidget()
    qtbot.addWidget(host)
    lay = QVBoxLayout(host)
    bar = InfoBar(host)
    lay.addWidget(bar)
    lay.addStretch(1)
    host.resize(520, 200)
    activate(qtbot, host)
    bar.show_message(InfoLevel.ERROR, "Lỗi")
    motion.finish_all()
    settle()
    bar.close_button.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(lambda: QApplication.focusWidget() is bar.close_button, timeout=WAIT_MS)
    QTest.keyClick(bar.close_button, Qt.Key.Key_Space)
    assert host.focusWidget() is None
    assert not focus_in(bar)
    idle(qtbot, motion)


@pytest.mark.parametrize("real", [False, True], ids=["tuc-thi", "that"])
def test_t17_show_while_closing_ends_open(request, qtbot, bar_host, real):
    from teleport.ui import motion
    from teleport.ui.widgets import InfoLevel

    if real:
        request.getfixturevalue("real_motion")
    bar = bar_host.bar
    activate(qtbot, bar_host.host)
    bar.show_message(InfoLevel.INFO, "Một")
    idle(qtbot, motion)
    bar.clear()
    bar.show_message(InfoLevel.WARNING, "Hai")
    assert bar.is_open is True
    idle(qtbot, motion)
    assert bar.is_open is True
    assert bar.isVisible()
    assert bar.title == "Hai"
    assert bar.maximumHeight() == QWIDGETSIZE_MAX
    assert bar.height() > 0


def test_t18_hidden_parent_is_instant(real_motion, bar_host):
    from teleport.ui.widgets import InfoLevel

    bar = bar_host.bar
    assert not bar_host.host.isVisible()
    bar.show_message(InfoLevel.INFO, "Thông báo")
    assert bar.is_open is True
    assert not bar.isHidden()
    assert real_motion.running_count() == 0
    bar.clear()
    assert bar.is_open is False
    assert bar.isHidden()
    assert real_motion.running_count() == 0


def test_t18_top_level_bar_is_instant(real_motion, qtbot):
    """Mục 10.13: thanh là cửa sổ cấp cao (không cha) → đóng tức thì."""
    from teleport.ui.widgets import InfoBar, InfoLevel

    bar = InfoBar()
    qtbot.addWidget(bar)
    bar.resize(520, 120)
    bar.show_message(InfoLevel.INFO, "Thông báo")
    bar.show()
    qtbot.waitExposed(bar)
    assert bar.is_open is True
    bar.clear()
    assert bar.is_open is False
    assert bar.isHidden()
    assert real_motion.running_count() == 0


def test_t18_chat_view_reads_logic_state_right_after_hide(real_motion, qtbot):
    from teleport.ui.chat import ChatView
    from teleport.ui.radar import DeviceListModel

    host = QWidget()
    qtbot.addWidget(host)
    devices = DeviceListModel(host)
    view = ChatView(devices, host)
    lay = QVBoxLayout(host)
    lay.addWidget(view, 1)
    host.resize(720, 460)
    activate(qtbot, host)
    view.show_notice("Câu thông báo.")
    assert view.has_content() is True
    assert view.notice_text() == "Câu thông báo."
    after(qtbot, N(), lambda: real_motion.running_count() == 0)
    view.hide_notice()
    assert view.has_content() is False, "đọc trạng thái logic, không chờ thanh ẩn hẳn"
    assert view.notice_text() == ""
    after(qtbot, N(), lambda: view.info_bar.isHidden() and real_motion.running_count() == 0)


def test_t18_settings_controller_notice_none_right_after_clear(real_motion, qtbot, tmp_path, settings_page):
    from teleport.ui.settings import SettingsController
    from teleport.ui.widgets import InfoLevel

    ctrl = SettingsController(settings_page, tmp_path)
    settings_page.show_notice(InfoLevel.INFO, "Đã lưu.")
    assert ctrl.notice_text() == "Đã lưu."
    after(qtbot, N(), lambda: real_motion.running_count() == 0)
    settings_page.clear_notice()
    assert ctrl.notice_text() is None
    after(qtbot, N(), lambda: settings_page.info_bar.isHidden() and real_motion.running_count() == 0)


# =================================================================================================
# T19 hệ điều hành tắt hiệu ứng: mọi thao tác nhảy thẳng trạng thái cuối
# =================================================================================================


def test_t19_disabled_animations_every_action_is_final_at_once(
    real_motion, monkeypatch, qtbot, win, conv, settings_page
):
    from teleport.ui.main_window import Page
    from teleport.ui.settings import Section
    from teleport.ui.widgets import InfoBar, InfoLevel

    disable_animations(real_motion, monkeypatch)
    motion = real_motion
    w = win.window

    w.open_page(Page.WAIT_PAIRING)
    assert w.pages.currentWidget() is win.page
    assert overlays(w.pages) == []
    assert motion.running_count() == 0
    w.go_back()
    assert overlays(w.pages) == []
    w.devices.reset([])
    settle()
    w.set_transfer_summary(True, 0)
    assert w.stream_pane.currentWidget() is w.transfers_area
    assert overlays(w.stream_pane) == []
    assert motion.running_count() == 0

    conv.select_row(0)
    conv.repaint()
    conv.select_row(2)
    conv.repaint()
    assert conv.view.selection_indicator.rect == conv.cell(2)
    assert motion.running_count() == 0

    name = other_section(settings_page)
    settings_page.show_section(Section[name])
    repaint_nav(settings_page)
    assert nav_item_rect(settings_page, section_row(name)).contains(
        settings_page.nav.selection_indicator.rect.center()
    )
    assert motion.running_count() == 0

    host = QWidget()
    qtbot.addWidget(host)
    lay = QVBoxLayout(host)
    bar = InfoBar(host)
    lay.addWidget(bar)
    activate(qtbot, host)
    bar.show_message(InfoLevel.INFO, "Thông báo")
    assert bar.isVisible() and bar.maximumHeight() == QWIDGETSIZE_MAX
    bar.clear()
    assert bar.isHidden()
    assert motion.running_count() == 0

    monkeypatch.setattr(motion, "_PLATFORMS_WITHOUT_WINDOW_OPACITY", frozenset())
    d, _ = _dialog(qtbot)
    motion.fade_in_on_show(d)
    d.show()
    assert d.windowOpacity() == pytest.approx(1.0)
    assert motion.running_count() == 0


# =================================================================================================
# T20 không còn timer khi rảnh; finish_all giữa lúc đổi trang
# =================================================================================================


def test_t20_idle_after_sequence_of_actions(real_motion, qtbot, win):
    from teleport.ui.main_window import Page
    from teleport.ui.widgets import InfoBar, InfoLevel

    w = win.window
    w.devices.reset([])
    settle()
    w.open_page(Page.WAIT_PAIRING)
    w.go_back()
    w.set_transfer_summary(True, 0)
    w.conversations.list_view.viewport().repaint()
    host = QWidget()
    qtbot.addWidget(host)
    lay = QVBoxLayout(host)
    bar = InfoBar(host)
    lay.addWidget(bar)
    activate(qtbot, host)
    bar.show_message(InfoLevel.INFO, "Một")
    bar.clear()
    after(qtbot, W(), lambda: real_motion.running_count() == 0)
    assert overlays(w.pages) == []
    assert overlays(w.stream_pane) == []


def test_t20_finish_all_mid_page_switch_removes_overlay(real_motion, qtbot, win):
    from teleport.ui.main_window import Page

    w = win.window
    w.open_page(Page.WAIT_PAIRING)
    assert len(overlays(w.pages)) == 1
    real_motion.finish_all()
    settle()
    assert visible_overlays(w.pages) == [], "finish_all gỡ overlay ngay"
    assert real_motion.running_count() == 0
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qtbot.waitUntil(lambda: overlays(w.pages) == [], timeout=WAIT_MS)
    assert w.pages.currentWidget() is win.page
