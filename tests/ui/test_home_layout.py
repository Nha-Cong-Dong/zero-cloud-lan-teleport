"""Khung hai cột của cửa sổ chính (spec B1, mục 6: L1–L18; mục 12 thắng khi khác).

Viết từ spec, không từ code. Mỗi test ghi mã L.. ở tên hàm. Cửa sổ dựng riêng (không `main()`), trừ L11
(`gui`, chạy node thật trên 127.0.0.1).

Hai cách hiện cửa sổ (`show_window`):
- `WA_DontShowOnScreen` + `show` + `waitExposed` cho test bố cục và màu (spec 12.14);
- cửa sổ không có cờ đó khi test cần cửa sổ active (tiêu điểm, Tab, phím tắt, `focusChanged`): trên
  nền `offscreen`, cửa sổ `WA_DontShowOnScreen` không bao giờ thành active (đã thử), nên `setFocus` và
  phím tắt `WindowShortcut` không chạy.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QPoint, QSize, Qt, QTranslator, QUrl
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication, QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

# ---- Chữ của spec (2.6, 2.7, 2.8) -----------------------------------------------------------------
THIS_MACHINE = "Máy này"
READY = "Sẵn sàng nhận"
BUSY = "Đang truyền {n} lượt"
COPY = "Chép địa chỉ"
SAVE_PREFIX = "Lưu vào: "
OPEN_DIR = "Mở thư mục"
RECEIVE_DIR = "Thư mục nhận"
DISCOVER = "Tìm máy mới"
SETTINGS = "Cài đặt"
DEVICES = "Thiết bị"
EMPTY_TITLE = "Ghép đôi máy đầu tiên"
EMPTY_TEXT_NO_DISCOVER = (
    "Mở Teleport trên máy kia, trong cùng mạng với máy này. Máy đó sẽ hiện ở cột bên trái; bấm vào tên "
    "máy để ghép đôi."
)
EMPTY_TEXT_DISCOVER = "Mở Teleport trên máy kia, trong cùng mạng với máy này, rồi bấm Tìm máy mới."
STARTING_BAR = "Đang khởi động"
TRANSFERS = "Lượt truyền"
ADDRESS_A11Y = "Địa chỉ của máy này"

# ---- Hằng của spec (2.3) --------------------------------------------------------------------------
SIDEBAR_WIDTH = 288
SIDEBAR_COMPACT_WIDTH = 248
COMPACT_BELOW = 820
START_PANEL_MAX_WIDTH = 460
MIN_W, MIN_H = 720, 460

PEER_ID = "a71c04d2" + "e" * 24


# =================================================================================================
# Dữ liệu giả
# =================================================================================================


def device_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:32]


def dview(label: str = "PC-Nha", *, paired: bool = False, online: bool = True, connected: bool = False):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id(label),
        name=label,
        os="windows" if online else None,
        paired=paired,
        online=online,
        connected=connected and paired,
        addresses=("192.0.2.7",) if online else (),
        port=47474 if online else None,
    )


def tkey(direction: str = "send", transfer_id: str = "a" * 32, dev: str = PEER_ID):
    from teleport.core.transfer import Direction, TransferKey

    return TransferKey(dev, Direction(direction), transfer_id)


def tinfo(k, status: str = "transferring"):
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import TransferInfo, TransferStatus

    return TransferInfo(
        key=k,
        status=TransferStatus(status),
        files=(FileEntry("video.mp4", 1000, 0),),
        names=("video.mp4",),
        file_index=0,
        done_bytes=0,
        total_bytes=1000,
    )


def status_changed(k, status: str = "transferring"):
    from teleport.app.transfers import TransferStatusChanged

    return TransferStatusChanged(tinfo(k, status))


def finished(k, result: str = "done"):
    from teleport.app.transfers import TransferFinished, TransferResult

    return TransferFinished(
        info=tinfo(k, "verifying"),
        result=TransferResult(result),
        by_peer=False,
        reason=None,
        file_index=None,
        reject=None,
    )


def make_page(label: str = "D") -> QWidget:
    """Trang mẫu: một nút, `setFocusProxy` tới nút (như trang thật)."""
    page = QWidget()
    button = QPushButton(f"Nút của trang {label}")
    QVBoxLayout(page).addWidget(button)
    page.setFocusProxy(button)
    return page


def shout(text: str) -> str:
    """Chữ in hoa, giữ nguyên chỗ giữ chỗ `{…}` và `%n`."""
    return "".join(
        p if re.fullmatch(r"\{\w+\}|%n", p) else p.upper() for p in re.split(r"(\{\w+\}|%n)", text)
    )


class ShoutTranslator(QTranslator):
    """Translator giả trả chữ in hoa (như `test_i18n_theme.py`, spec L14)."""

    def isEmpty(self):
        return False

    def translate(self, context, source_text, disambiguation=None, n=-1):
        return shout(source_text) if source_text else source_text


# =================================================================================================
# Fixture, hàm phụ trợ
# =================================================================================================


@pytest.fixture
def make_window(qtbot):
    def make():
        from teleport.ui.main_window import MainWindow

        w = MainWindow()
        qtbot.addWidget(w)
        return w

    return make


@pytest.fixture
def window(make_window):
    return make_window()


@pytest.fixture
def tray(monkeypatch):
    from teleport.ui.tray import Tray

    monkeypatch.setattr(Tray, "show_message", lambda self, text, folder=None: None)
    t = Tray()
    yield t
    t.dispose()


@pytest.fixture
def make_ctl(tray):
    """`make_ctl(window) -> TransfersController` (spec 12.6: không dựng `TransfersView` bằng tay)."""
    made: list = []

    def make(window):
        from teleport.ui.transfers import TransfersController

        ctl = TransfersController(window, tray, parent=None)
        made.append(ctl)
        return ctl

    yield make
    for ctl in made:
        ctl.close_all()


def show_window(window, qtbot, width: int = 980, height: int = 660, *, active: bool = False) -> None:
    """Hiện cửa sổ ở cỡ `width`x`height` (xem docstring module về `active`)."""
    if not active:
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.resize(width, height)
    window.show()
    qtbot.waitExposed(window)
    if active:
        window.activateWindow()
        window.raise_()
        qtbot.waitUntil(window.isActiveWindow, timeout=3000)
    settle()


def settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


def resize_to(window, width: int, height: int) -> None:
    window.resize(width, height)
    settle()


def layout_holding(layout, widget):
    """Layout (trong cây layout `layout`) chứa trực tiếp `widget`; không có → None."""
    if layout is None:
        return None
    if layout.indexOf(widget) >= 0:
        return layout
    for i in range(layout.count()):
        found = layout_holding(layout.itemAt(i).layout(), widget)
        if found is not None:
            return found
    return None


def visible(widget, window) -> bool:
    return widget.isVisibleTo(window)


def focusable(widget) -> bool:
    return bool(widget.focusPolicy().value & Qt.FocusPolicy.TabFocus.value)


def running(window, tmp_path: Path, name: str | None = "May-A", receive_dir: Path | None = None):
    window.show_running(name, tmp_path / "nhan" if receive_dir is None else receive_dir)
    settle()


def color_at(image, widget, window, point: QPoint) -> str:
    """Màu (dạng #rrggbb) của điểm `point` (tọa độ của `widget`) trên ảnh `window.grab()`."""
    p = widget.mapTo(window, point)
    dpr = image.devicePixelRatio()
    return QColor(image.pixel(round(p.x() * dpr), round(p.y() * dpr))).name()


def fill_l12(window, ctl, tmp_path: Path) -> None:
    """Dựng như L12: trang DISCOVER, một máy đã ghép đôi, hai thẻ và một thông báo, có địa chỉ."""
    from teleport.ui.main_window import Page

    window.add_page(Page.DISCOVER, make_page())
    running(window, tmp_path)
    window.set_addresses(["192.0.2.5"], None)
    window.radar.model.reset([dview("PC-Nha", paired=True, connected=True)])
    ctl.view.set_device_names({PEER_ID: "PC-Nha"})
    ctl.view.handle(status_changed(tkey("send", "a" * 32)))
    ctl.view.handle(status_changed(tkey("receive", "b" * 32)))
    ctl.view.show_notice("Một thông báo.", "Làm gì đó", lambda: None)
    settle()


def fill_l3_full(window, ctl, tmp_path: Path) -> None:
    """RUNNING đầy đủ của L3: 6 thiết bị, khung nhập IP mở kèm lỗi, 3 thẻ + thông báo, 3 địa chỉ,
    thư mục nhận dài 200 ký tự."""
    long_dir = Path("C:/" + "r" * 197)
    assert len(str(long_dir)) == 200
    window.show_running("May-A", long_dir)
    window.set_addresses(["192.0.2.5", "192.0.2.6", "192.0.2.7"], 50000)
    window.radar.model.reset(
        [dview(f"May-{i}", paired=i < 2, connected=i == 0) for i in range(6)],
    )
    window.radar.open_address_entry()
    window.radar.address_edit.setText("không phải IP")
    window.radar.submit_address()
    ctl.view.set_device_names({PEER_ID: "PC-Nha"})
    for c in "abc":
        ctl.view.handle(status_changed(tkey("send", c * 32)))
    ctl.view.show_notice(
        "Một thông báo khá dài để chiếm vài dòng trong khung lượt truyền.", "Làm", lambda: None
    )
    settle()


# =================================================================================================
# L1 — Cấu trúc
# =================================================================================================


def test_l1_running_shows_shell_and_hides_start_view(window, tmp_path):
    running(window, tmp_path)
    assert window.home_stack.currentWidget() is window.shell
    assert not visible(window.start_view, window)
    assert visible(window.shell, window)


def test_l1_widget_tree_matches_spec(window):
    from teleport.ui import home, widgets

    assert isinstance(window.home_stack, QStackedWidget)
    assert isinstance(window.start_view, home.StartPanel)
    assert isinstance(window.sidebar, QFrame)
    assert isinstance(window.sidebar_scroll, QScrollArea)
    assert isinstance(window.machine_card, home.MachineCard)
    assert isinstance(window.stream_pane, QStackedWidget)
    assert isinstance(window.empty_state, widgets.EmptyState)
    assert isinstance(window.save_bar, home.SaveBar)
    assert isinstance(window.discover_button, widgets.IconButton)
    assert isinstance(window.settings_button, widgets.IconButton)

    assert window.home_page.isAncestorOf(window.home_stack)
    assert window.home_stack.indexOf(window.start_view) >= 0
    assert window.home_stack.indexOf(window.shell) >= 0
    assert window.body.isAncestorOf(window.sidebar)
    assert window.body.isAncestorOf(window.stream_pane)
    for w in (window.radar, window.machine_card, window.discover_button, window.settings_button):
        assert window.sidebar.isAncestorOf(w), w.objectName()
    assert window.sidebar.isAncestorOf(window.sidebar_header)
    assert window.sidebar_header.isAncestorOf(window.discover_button)
    assert window.sidebar_header.isAncestorOf(window.brand_label)
    assert window.sidebar_header.isAncestorOf(window.brand_logo)
    assert window.sidebar_scroll.widget().isAncestorOf(window.radar)
    assert window.stream_pane.indexOf(window.empty_state) >= 0
    assert window.stream_pane.indexOf(window.transfers_area) >= 0
    assert not window.body.isAncestorOf(window.save_bar)
    assert window.shell.isAncestorOf(window.save_bar)


def test_l1_shortcut_attributes_point_into_machine_card_and_start_view(window):
    assert window.name_label is window.machine_card.name_label
    assert window.copy_button is window.machine_card.copy_button
    assert window.badge is window.machine_card.badge
    assert window.address_label is window.machine_card.address_label
    assert window.status_icon is window.start_view.status_icon
    assert window.starting_bar is window.start_view.starting_bar
    card = window.start_view.card
    for name in ("title_label", "status_label", "detail_label", "hint_label", "start_button", "retry_button"):
        assert card.isAncestorOf(getattr(window, name)), name


@pytest.mark.parametrize(
    ("attr", "object_name"),
    [
        ("home_stack", "homeStack"),
        ("start_view", "startView"),
        ("shell", "runningShell"),
        ("body", "runningBody"),
        ("sidebar", "sidebar"),
        ("sidebar_header", "sidebarHeader"),
        ("brand_logo", "brandLogo"),
        ("brand_label", "brandLabel"),
        ("sidebar_scroll", "sidebarScroll"),
        ("machine_card", "machineCard"),
        ("stream_pane", "streamPane"),
        ("empty_state", "streamEmpty"),
        ("transfers_area", "transfersArea"),
        ("save_bar", "saveBar"),
        ("address_label", "addressLabel"),
        ("name_label", "machineName"),
        ("copy_button", "copyAddressButton"),
        ("badge", "machineBadge"),
    ],
)
def test_l1_object_names(window, attr, object_name):
    assert getattr(window, attr).objectName() == object_name


def test_l1_surfaces_of_sidebar_and_start_card(window):
    assert window.sidebar.property("surface") == "sidebar"
    card = window.start_view.card
    assert isinstance(card, QFrame)
    assert card.objectName() == "startCard"
    assert card.property("surface") == "card"


def test_l1_save_bar_spans_full_width_below_body(window, qtbot, tmp_path):
    running(window, tmp_path)
    show_window(window, qtbot, 980, 660)
    assert window.save_bar.width() == window.home_page.width()
    assert window.save_bar.geometry().top() >= window.body.geometry().bottom()


def test_l1_old_public_attributes_live_in_home_page_with_same_types(window):
    from teleport.ui.radar import RadarView

    labels = ("status_label", "detail_label", "hint_label", "title_label", "address_label")
    for name in labels:
        assert isinstance(getattr(window, name), QLabel), name
    for name in ("start_button", "retry_button"):
        assert isinstance(getattr(window, name), QPushButton), name
    assert isinstance(window.radar, RadarView)
    for name in (*labels, "start_button", "retry_button", "radar", "transfers_area", "body"):
        assert window.home_page.isAncestorOf(getattr(window, name)), name


# =================================================================================================
# L2 — Bề ngang cột trái
# =================================================================================================


@pytest.mark.parametrize(
    ("width", "expected", "compact"),
    [
        (980, SIDEBAR_WIDTH, False),
        (820, SIDEBAR_WIDTH, False),
        (819, SIDEBAR_COMPACT_WIDTH, True),
        (720, SIDEBAR_COMPACT_WIDTH, True),
    ],
)
def test_l2_sidebar_width_on_show(window, qtbot, tmp_path, width, expected, compact):
    running(window, tmp_path)
    show_window(window, qtbot, width, 660)
    assert window.width() == width
    assert window.sidebar.width() == expected
    assert window.sidebar_compact is compact
    # 12.1: viền phải 1 px nằm trong bề ngang của `sidebar`.
    assert window.sidebar.width() + window.stream_pane.width() == window.body.width()


def test_l2_sidebar_width_follows_resizes(window, qtbot, tmp_path):
    running(window, tmp_path)
    show_window(window, qtbot, 980, 660)
    for width, expected, compact in (
        (720, SIDEBAR_COMPACT_WIDTH, True),
        (820, SIDEBAR_WIDTH, False),
        (819, SIDEBAR_COMPACT_WIDTH, True),
        (1100, SIDEBAR_WIDTH, False),
        (720, SIDEBAR_COMPACT_WIDTH, True),
    ):
        resize_to(window, width, 660)
        assert window.sidebar.width() == expected, width
        assert window.sidebar_compact is compact, width
        assert window.sidebar.width() + window.stream_pane.width() == window.body.width(), width


def test_l2_resize_keeps_focus_on_address_label(window, qtbot, tmp_path):
    running(window, tmp_path)
    window.set_addresses(["192.0.2.5"], None)
    show_window(window, qtbot, 980, 660, active=True)
    window.address_label.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(lambda: window.focusWidget() is window.address_label, timeout=1000)
    resize_to(window, 720, 660)
    assert window.sidebar_compact
    assert window.focusWidget() is window.address_label
    resize_to(window, 980, 660)
    assert window.focusWidget() is window.address_label


# =================================================================================================
# L3 — Không phình quá 720x460
# =================================================================================================


def _assert_fits(window) -> None:
    assert window.size() == QSize(MIN_W, MIN_H), window.size()
    hint = window.minimumSizeHint()
    assert hint.width() <= MIN_W and hint.height() <= MIN_H, f"nội dung cần tối thiểu {hint}"


@pytest.mark.parametrize("state", ["welcome", "starting", "error"])
def test_l3_start_states_do_not_grow_window(window, qtbot, state):
    {
        "welcome": lambda: window.show_welcome("Laptop-Minh"),
        "starting": window.show_starting,
        "error": lambda: window.show_error("config_dir", "C:\\" + "a" * 200 + "\nAccess denied"),
    }[state]()
    show_window(window, qtbot, MIN_W, MIN_H)
    _assert_fits(window)


def test_l3_full_running_does_not_grow_window(window, qtbot, make_ctl, tmp_path):
    ctl = make_ctl(window)
    show_window(window, qtbot, MIN_W, MIN_H)
    fill_l3_full(window, ctl, tmp_path)
    _assert_fits(window)
    assert window.radar.address_panel.isVisibleTo(window)
    assert_radar_fits_sidebar(window)


def test_l3_error_with_long_path_scrolls_start_panel_instead(window, qtbot):
    window.show_error("config_dir", "C:\\" + "a" * 200 + "\nAccess denied")
    show_window(window, qtbot, MIN_W, MIN_H)
    scroll = window.start_view.start_scroll
    assert isinstance(scroll, QScrollArea)
    assert window.size() == QSize(MIN_W, MIN_H)
    # 12.15: thanh cuộn ngang AsNeeded (không tắt): đường dẫn dài không dấu cách vẫn xem được.
    assert scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded


# =================================================================================================
# L4 — Màu (sáng, tối)
# =================================================================================================


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_l4_region_colors(make_window, qtbot, ui_theme, tmp_path, dark):
    t = ui_theme(dark)
    window = make_window()
    running(window, tmp_path, receive_dir=Path("C:/T"))  # thư mục ngắn: điểm giữa `save_bar` là nền
    window.set_addresses(["192.0.2.5"], None)
    show_window(window, qtbot, 980, 660)
    image = window.grab().toImage()

    sidebar, border, surface = t.sidebar.name(), t.border.name(), t.surface.name()
    assert color_at(image, window.sidebar, window, QPoint(200, 4)) == sidebar, "đầu cột"
    assert window.stream_pane.currentWidget() is window.empty_state
    assert color_at(image, window.stream_pane, window, QPoint(8, 8)) == surface, "khung phải"
    bar = window.save_bar
    assert color_at(image, bar, window, QPoint(bar.width() // 2, bar.height() // 2)) == sidebar, (
        "giữa save_bar"
    )
    assert color_at(image, bar, window, QPoint(bar.width() // 2, 0)) == border, "đường kẻ trên save_bar"
    edge = window.sidebar.width() - 1
    wrong = [
        y
        for y in range(window.sidebar.height())
        if color_at(image, window.sidebar, window, QPoint(edge, y)) != border
    ]
    assert not wrong, f"mép phải cột trái không phải `border` ở y = {wrong[:5]}…"
    card = window.machine_card
    assert color_at(image, card, window, QPoint(card.width() // 2, 0)) == border, "đường kẻ trên machine_card"
    # 12.2: nền trong suốt của vùng cuộn và radar: khe giữa tiêu đề và gợi ý của radar là `sidebar`.
    radar = window.radar
    gap = QPoint(radar.width() // 2, radar.heading_label.geometry().bottom() + 4)
    assert color_at(image, radar, window, gap) == sidebar, "điểm trống của radar"


# =================================================================================================
# L5 — Trạng thái
# =================================================================================================


def _apply(window, tmp_path, state: str) -> None:
    {
        "welcome": lambda: window.show_welcome("Laptop-Minh"),
        "starting": window.show_starting,
        "running": lambda: running(window, tmp_path),
        "error": lambda: window.show_error("locked", ""),
    }[state]()
    settle()


@pytest.mark.parametrize("state", ["welcome", "starting", "error"])
def test_l5_start_states_show_start_view_only(window, tmp_path, state):
    _apply(window, tmp_path, "running")
    _apply(window, tmp_path, state)
    assert window.home_stack.currentWidget() is window.start_view
    assert visible(window.start_view, window)
    assert not visible(window.shell, window)
    assert not visible(window.save_bar, window)
    assert visible(window.starting_bar, window) is (state == "starting")


@pytest.mark.parametrize(("width", "height", "check_vertical"), [(980, 660, True), (720, 460, False)])
def test_l5_start_card_is_centered_and_capped(window, qtbot, width, height, check_vertical):
    window.show_welcome("Laptop-Minh")
    show_window(window, qtbot, width, height)
    card, view = window.start_view.card, window.start_view
    available = window.start_view.start_scroll.viewport().width()
    assert card.width() == min(START_PANEL_MAX_WIDTH, available)
    center = card.mapTo(view, card.rect().center())
    assert abs(center.x() - view.rect().center().x()) <= 2
    if check_vertical:
        assert abs(center.y() - view.rect().center().y()) <= 2


def test_l5_error_state_icon_and_primary_retry(window, tmp_path):
    from teleport.ui import theme

    _apply(window, tmp_path, "error")
    assert visible(window.status_icon, window)
    assert visible(window.retry_button, window)
    assert window.retry_button.property(theme.PRIMARY_PROPERTY) is True
    assert not visible(window.start_button, window)
    assert not visible(window.starting_bar, window)


def test_l5_welcome_state_primary_start_without_icon(window, tmp_path):
    from teleport.ui import theme

    _apply(window, tmp_path, "welcome")
    assert visible(window.start_button, window)
    assert window.start_button.property(theme.PRIMARY_PROPERTY) is True
    assert not visible(window.status_icon, window)
    assert not visible(window.retry_button, window)


def test_l5_starting_state_has_bar_and_no_buttons(window, tmp_path):
    _apply(window, tmp_path, "starting")
    assert visible(window.starting_bar, window)
    assert not visible(window.status_icon, window)
    assert not visible(window.start_button, window)
    assert not visible(window.retry_button, window)


def test_l5_state_sequence_switches_stack_and_focus(window, qtbot, tmp_path):
    show_window(window, qtbot, 980, 660, active=True)
    steps = [
        ("welcome", lambda: window.start_view, lambda: window.start_button),
        ("starting", lambda: window.start_view, lambda: window.status_label),
        ("running", lambda: window.shell, lambda: window.radar.address_button),  # radar trống
        ("error", lambda: window.start_view, lambda: window.retry_button),
        ("starting", lambda: window.start_view, lambda: window.status_label),
    ]
    for state, page, focus in steps:
        _apply(window, tmp_path, state)
        assert window.home_stack.currentWidget() is page(), state
        assert window.focusWidget() is focus(), (state, window.focusWidget())
    # RUNNING khi radar có thiết bị: tiêu điểm vào danh sách.
    window.radar.model.reset([dview("PC-Nha", paired=True)])
    _apply(window, tmp_path, "running")
    assert window.home_stack.currentWidget() is window.shell
    assert window.focusWidget() is window.radar.list_view


# =================================================================================================
# L6 — Nút đầu cột
# =================================================================================================


def test_l6_header_buttons_hidden_without_pages(window, tmp_path):
    running(window, tmp_path)
    assert not visible(window.discover_button, window)
    assert not visible(window.settings_button, window)


@pytest.mark.parametrize("page_name", ["discover", "settings"])
@pytest.mark.parametrize("add_first", [True, False], ids=["add-then-run", "run-then-add"])
def test_l6_header_button_appears_with_page_and_opens_it(window, tmp_path, page_name, add_first):
    from teleport.ui.main_window import Page

    page = Page(page_name)
    button = window.discover_button if page is Page.DISCOVER else window.settings_button
    other = window.settings_button if page is Page.DISCOVER else window.discover_button
    if add_first:
        window.add_page(page, make_page())
        running(window, tmp_path)
    else:
        running(window, tmp_path)
        window.add_page(page, make_page())
    assert visible(button, window)
    assert not visible(other, window)
    button.click()
    assert window.current_page is page


def test_l6_header_buttons_names_and_tooltips(window):
    assert window.discover_button.accessibleName() == DISCOVER
    assert window.settings_button.accessibleName() == SETTINGS
    assert window.discover_button.toolTip()
    assert window.settings_button.toolTip()


# =================================================================================================
# L7 — EmptyState của khung phải
# =================================================================================================


def test_l7_empty_state_when_nothing_paired_and_no_content(window, tmp_path):
    running(window, tmp_path)
    window.set_transfer_summary(False, 0)
    assert window.stream_pane.currentWidget() is window.empty_state
    assert window.empty_state.title_label.text() == EMPTY_TITLE
    assert window.empty_state.text_label.text() == EMPTY_TEXT_NO_DISCOVER
    assert window.empty_state.action_button.isHidden()


def test_l7_paired_device_switches_to_transfers_area(window, tmp_path):
    running(window, tmp_path)
    window.set_transfer_summary(False, 0)
    window.radar.model.reset([dview(paired=True)])
    assert window.stream_pane.currentWidget() is window.transfers_area
    window.radar.model.clear()
    assert window.stream_pane.currentWidget() is window.empty_state


def test_l7_only_unpaired_devices_keep_empty_state(window, tmp_path):
    running(window, tmp_path)
    window.set_transfer_summary(False, 0)
    window.radar.model.reset([dview("May-1"), dview("May-2")])
    assert window.stream_pane.currentWidget() is window.empty_state


def test_l7_model_signals_recompute_paired_flag(window, tmp_path):
    from teleport.app.node import DeviceOffline, DeviceOnline

    running(window, tmp_path)
    window.set_transfer_summary(False, 0)
    model = window.radar.model
    model.apply(DeviceOnline(dview("May-1")))  # rowsInserted, chưa ghép đôi
    assert window.stream_pane.currentWidget() is window.empty_state
    model.apply(DeviceOnline(dview("May-1", paired=True)))  # cùng máy, nay đã ghép đôi
    assert window.stream_pane.currentWidget() is window.transfers_area
    model.reset([dview("May-3")])  # modelReset: chỉ còn máy chưa ghép đôi
    assert window.stream_pane.currentWidget() is window.empty_state
    model.apply(DeviceOffline(dview("May-3", online=False)))  # rowsRemoved
    assert model.rowCount() == 0
    assert window.stream_pane.currentWidget() is window.empty_state


def test_l7_content_without_paired_device_shows_transfers_area(window, tmp_path):
    running(window, tmp_path)
    window.set_transfer_summary(True, 0)
    assert window.stream_pane.currentWidget() is window.transfers_area
    window.set_transfer_summary(False, 0)
    assert window.stream_pane.currentWidget() is window.empty_state


def test_l7_discover_page_adds_action_button(window, tmp_path):
    from teleport.ui.main_window import Page

    running(window, tmp_path)
    window.set_transfer_summary(False, 0)
    window.add_page(Page.DISCOVER, make_page())
    state = window.empty_state
    assert not state.action_button.isHidden()
    assert state.action_button.text().replace("&", "") == DISCOVER
    assert state.text_label.text() == EMPTY_TEXT_DISCOVER
    state.action_button.click()
    assert window.current_page is Page.DISCOVER


# =================================================================================================
# L8 — Thẻ “máy này”
# =================================================================================================


def test_l8_name_and_caption(window, tmp_path):
    from teleport.ui.home import MachineCard

    running(window, tmp_path, name="May-A")
    assert isinstance(window.machine_card, MachineCard)
    assert window.name_label.text() == "May-A"
    assert window.name_label.textFormat() == Qt.TextFormat.PlainText
    assert window.name_label.accessibleName() == THIS_MACHINE
    assert window.machine_card.caption_label.text() == THIS_MACHINE


def test_l8_hostile_name_shown_through_display_name(window, tmp_path):
    running(window, tmp_path, name="<b>X</b>\x07")
    assert window.name_label.text() == "<b>X</b>\ufffd"  # 12.12
    assert window.name_label.textFormat() == Qt.TextFormat.PlainText


def test_l8_no_name_hides_name_label(window, tmp_path):
    running(window, tmp_path, name=None)
    assert not visible(window.name_label, window)


def test_l8_badge_follows_transfer_summary(window, tmp_path):
    from teleport.ui.widgets import BadgeTone

    running(window, tmp_path, name="May-A")
    window.set_transfer_summary(False, 0)
    assert (window.badge.text, window.badge.tone) == (READY, BadgeTone.SUCCESS)
    window.set_transfer_summary(True, 2)
    assert (window.badge.text, window.badge.tone) == (BUSY.format(n=2), BadgeTone.BUSY)
    desc = window.name_label.accessibleDescription()
    assert "May-A" in desc and BUSY.format(n=2) in desc
    window.set_transfer_summary(True, 1)
    assert window.badge.text == BUSY.format(n=1)
    window.set_transfer_summary(True, 0)
    assert (window.badge.text, window.badge.tone) == (READY, BadgeTone.SUCCESS)
    desc = window.name_label.accessibleDescription()
    assert "May-A" in desc and READY in desc


def test_l8_description_follows_rename(window, tmp_path):
    running(window, tmp_path, name="May-A")
    running(window, tmp_path, name="May-Moi")
    assert "May-Moi" in window.name_label.accessibleDescription()


def test_l8_negative_active_count_raises(window, tmp_path):
    running(window, tmp_path)
    with pytest.raises(ValueError):
        window.set_transfer_summary(True, -1)


# =================================================================================================
# L9 — Chép địa chỉ
# =================================================================================================


@pytest.mark.parametrize(("port", "expected"), [(50000, "192.0.2.5:50000"), ("default", "192.0.2.5")])
def test_l9_copy_button_copies_shown_address(window, tmp_path, port, expected):
    from teleport.net.connection import DEFAULT_PORT

    running(window, tmp_path)
    window.set_addresses(["192.0.2.5"], DEFAULT_PORT if port == "default" else port)
    assert visible(window.copy_button, window)
    got: list[str] = []
    window.machine_card.address_copied.connect(got.append)
    QGuiApplication.clipboard().setText("")
    window.copy_button.click()
    assert QGuiApplication.clipboard().text() == expected
    assert got == [expected]


def test_l9_copy_button_hidden_without_address_or_outside_running(window, tmp_path):
    running(window, tmp_path)
    assert not visible(window.copy_button, window)
    window.set_addresses(["192.0.2.5"], 50000)
    assert visible(window.copy_button, window)
    window.set_addresses([], 50000)
    assert not visible(window.copy_button, window)
    window.set_addresses(["192.0.2.5"], 50000)
    window.show_error("locked", "")
    assert not visible(window.copy_button, window)
    running(window, tmp_path)
    assert not visible(window.copy_button, window)  # rời RUNNING đã quên địa chỉ


def test_l9_copy_button_name_and_row(window):
    assert window.copy_button.accessibleName() == COPY
    # 12.4: nút chép nằm cùng hàng (QHBoxLayout) với `address_label`, ngay sau nhãn.
    row = layout_holding(window.machine_card.layout(), window.address_label)
    assert isinstance(row, QHBoxLayout), f"address_label phải nằm trong một hàng ngang, thấy {row!r}"
    assert row.indexOf(window.copy_button) > row.indexOf(window.address_label)
    name_column = layout_holding(window.machine_card.layout(), window.name_label)
    assert name_column is not row, "nút chép không làm cột riêng cạnh tên"


# =================================================================================================
# L10 — Chân “Lưu vào”
# =================================================================================================


def test_l10_long_receive_dir_elided_with_full_text_in_tooltip(window, qtbot):
    path = Path("C:/x/" + "d" * 200)
    window.show_running("A", path)
    show_window(window, qtbot, MIN_W, MIN_H)
    label = window.save_bar.save_label
    assert label.text().startswith(SAVE_PREFIX)
    assert "…" in label.text()
    doc = QTextDocument()
    doc.setHtml(label.toolTip())
    assert str(path) in doc.toPlainText()
    assert str(path) in label.accessibleDescription()
    assert label.accessibleName() == RECEIVE_DIR
    assert window.save_bar.receive_dir == path
    assert window.receive_dir == path


def test_l10_markup_in_path_shown_as_text(window, qtbot):
    path = Path("C:/x") / "<i>nghieng&amp;"  # không có “/” trong tên: Windows coi “/” là dấu phân cách
    window.show_running("A", path)
    show_window(window, qtbot, 980, 660)
    label = window.save_bar.save_label
    assert label.textFormat() == Qt.TextFormat.PlainText
    assert label.text() == SAVE_PREFIX + str(path)
    doc = QTextDocument()
    doc.setHtml(label.toolTip())
    assert str(path) in doc.toPlainText()


def test_l10_open_button_emits_path_once(window, tmp_path):
    path = tmp_path / "nhan"
    window.show_running("A", path)
    got: list = []
    window.open_receive_dir_requested.connect(got.append)
    button = window.save_bar.open_button
    assert button.accessibleName() == OPEN_DIR
    button.click()
    assert got == [path]
    assert isinstance(got[0], Path)


def test_l10_no_receive_dir_hides_save_bar(window, tmp_path):
    window.show_running("A", None)
    assert not visible(window.save_bar, window)
    assert window.receive_dir is None
    window.show_running("A", tmp_path)
    assert visible(window.save_bar, window)


def test_l10_widening_window_shows_more_of_path(window, qtbot):
    path = Path("C:/x/" + "d" * 200)
    window.show_running("A", path)
    show_window(window, qtbot, MIN_W, MIN_H)
    label = window.save_bar.save_label
    narrow = label.text()
    resize_to(window, 980, 660)
    wide = label.text()
    assert len(wide) > len(narrow)
    full = SAVE_PREFIX + str(path)
    if QFontMetrics(label.font()).horizontalAdvance(full) <= label.contentsRect().width():
        assert wide == full


def test_l10_short_path_not_elided_when_it_fits(window, qtbot):
    path = Path("C:/T")
    window.show_running("A", path)
    show_window(window, qtbot, 980, 660)
    assert window.save_bar.save_label.text() == SAVE_PREFIX + str(path)


# =================================================================================================
# L11 — GuiApp mở thư mục nhận (node thật)
# =================================================================================================


@pytest.mark.integration
def test_l11_gui_app_opens_receive_dir_via_open_url(
    gui, gui_wait, config_dir, monkeypatch, tmp_path, ui_helpers
):
    from teleport.ui import app as ui_app
    from teleport.ui.main_window import ViewState

    calls: list = []
    reply = {"value": True}

    def fake_open_url(url):
        calls.append(QUrl(url))
        return reply["value"]

    monkeypatch.setattr(ui_app, "open_url", fake_open_url)
    d = config_dir("A")
    receive_dir = tmp_path / "nhan-gui"  # thư mục nhận mà fixture `gui` ép cho node
    seen: dict = {}

    def script(run):
        yield gui_wait(
            lambda: (
                run.windows
                and run.hosts
                and run.hosts[-1].running
                and run.window.view_state is ViewState.RUNNING
            ),
            15,
            "node chạy, cửa sổ RUNNING",
        )
        run.window.save_bar.open_button.click()
        for _ in range(5):
            yield
        seen["first"] = list(calls)
        reply["value"] = False
        run.window.save_bar.open_button.click()  # open_url trả False: chỉ ghi log, không hộp
        for _ in range(5):
            yield
        seen["second"] = list(calls)
        seen["boxes"] = ui_helpers.visible_message_boxes()
        run.tray.quit_requested.emit()

    run = gui(["--config-dir", str(d)], script)
    assert seen["first"] == [QUrl.fromLocalFile(str(receive_dir))]
    assert len(seen["second"]) == 2
    assert seen["boxes"] == []
    assert run.exec_calls == []
    assert run.code == 0


# =================================================================================================
# L12 — Thứ tự Tab ở RUNNING
# =================================================================================================


def tab_cycle(window, start, key=Qt.Key.Key_Tab, steps: int = 120) -> tuple[list, bool]:
    """Bấm `key` từ `start` tới khi quay về `start` (tối đa `steps`).

    Trả (các widget theo thứ tự lần đầu gặp, đã quay vòng hay chưa)."""
    start.setFocus(Qt.FocusReason.OtherFocusReason)
    QApplication.processEvents()
    assert window.focusWidget() is start
    order = [start]
    for _ in range(steps):
        current = window.focusWidget()
        if key == Qt.Key.Key_Backtab:
            QTest.keyClick(current, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        else:
            QTest.keyClick(current, key)
        QApplication.processEvents()
        focus = window.focusWidget()
        if focus is start:
            return order, True
        if focus is not None and all(focus is not w for w in order):
            order.append(focus)
    return order, False


def index_of(order: list, widget) -> int:
    for i, w in enumerate(order):
        if w is widget:
            return i
    raise AssertionError(f"{widget.objectName() or type(widget).__name__} không có trong chuỗi Tab")


def check_running_order(window, order: list) -> None:
    i_discover = index_of(order, window.discover_button)
    i_ip = index_of(order, window.radar.address_button)
    i_name = index_of(order, window.name_label)
    i_addr = index_of(order, window.address_label)
    i_copy = index_of(order, window.copy_button)
    i_open = index_of(order, window.save_bar.open_button)
    assert i_discover < i_ip < i_name < i_addr < i_copy < i_open
    in_area = [i for i, w in enumerate(order) if window.transfers_area.isAncestorOf(w)]
    assert in_area, "Tab không vào được widget nào của transfers_area"
    assert all(i_copy < i < i_open for i in in_area), (i_copy, in_area, i_open)
    assert not any(window.start_view.isAncestorOf(w) for w in order), "Tab vào widget của start_view"


def test_l12_tab_order_in_running(window, qtbot, make_ctl, tmp_path):
    ctl = make_ctl(window)
    fill_l12(window, ctl, tmp_path)
    show_window(window, qtbot, 980, 660, active=True)
    assert visible(window.discover_button, window)
    forward, closed = tab_cycle(window, window.discover_button)
    assert closed, f"Tab không quay vòng về discover_button sau {len(forward)} widget"
    check_running_order(window, forward)


def test_l12_shift_tab_is_reverse_order(window, qtbot, make_ctl, tmp_path):
    ctl = make_ctl(window)
    fill_l12(window, ctl, tmp_path)
    show_window(window, qtbot, 980, 660, active=True)
    forward, closed = tab_cycle(window, window.discover_button)
    assert closed
    backward, closed = tab_cycle(window, window.discover_button, Qt.Key.Key_Backtab)
    assert closed, "Shift+Tab không quay vòng về discover_button"
    radar = window.radar

    # Danh sách thiết bị có thể giữ Tab giữa các hàng (12.6): chỉ so các widget ngoài radar, cộng nút IP.
    def key_widgets(seq):
        return [w for w in seq if not radar.isAncestorOf(w) or w is radar.address_button]

    fwd = key_widgets(forward)
    back = key_widgets(backward)
    assert [fwd[0], *reversed(fwd[1:])] == back


def test_l12_new_card_stays_before_open_button(window, qtbot, make_ctl, tmp_path):
    ctl = make_ctl(window)
    fill_l12(window, ctl, tmp_path)
    show_window(window, qtbot, 980, 660, active=True)
    new_key = tkey("send", "f" * 32)
    ctl.view.handle(status_changed(new_key))
    settle()
    forward, closed = tab_cycle(window, window.discover_button)
    assert closed
    check_running_order(window, forward)
    new_row = ctl.view.row(new_key)
    assert index_of(forward, new_row.cancel_button) < index_of(forward, window.save_bar.open_button)


@pytest.mark.parametrize("state", ["welcome", "error"])
def test_l12_tab_order_on_start_screens(window, qtbot, state):
    """Spec 2.7: WELCOME / ERROR đi `status_label` → `detail_label` → `hint_label` → nút."""
    if state == "welcome":
        window.show_welcome("Laptop-Minh")
        button = window.start_button
    else:
        window.show_error("port", "[WinError 10048]")
        button = window.retry_button
    show_window(window, qtbot, 980, 660, active=True)
    order, closed = tab_cycle(window, window.status_label)
    assert closed
    labels = (window.status_label, window.detail_label, window.hint_label)
    shown = [w for w in labels if visible(w, window)]
    indexes = [index_of(order, w) for w in (*shown, button)]
    assert indexes == sorted(indexes), [w.objectName() for w in order]
    assert not any(window.shell.isAncestorOf(w) for w in order), "Tab vào widget của khung RUNNING"


# =================================================================================================
# L13 — Tên cho trình đọc màn hình
# =================================================================================================


def unnamed_focusable(window) -> list[str]:
    return [
        f"{type(w).__name__} {w.objectName()!r}"
        for w in window.findChildren(QWidget)
        if w.isVisibleTo(window) and focusable(w) and not w.accessibleName()
    ]


def icon_buttons_without_tooltip(window) -> list[str]:
    from teleport.ui.widgets import IconButton

    return [
        b.objectName() for b in window.findChildren(IconButton) if b.isVisibleTo(window) and not b.toolTip()
    ]


@pytest.mark.parametrize("state", ["running", "welcome", "error"])
def test_l13_every_focusable_widget_has_a_name(window, qtbot, make_ctl, tmp_path, state):
    ctl = make_ctl(window)
    fill_l12(window, ctl, tmp_path)
    if state == "welcome":
        window.show_welcome("Laptop-Minh")
    elif state == "error":
        window.show_error("port", "[WinError 10048]")
    show_window(window, qtbot, 980, 660)
    assert unnamed_focusable(window) == []
    assert icon_buttons_without_tooltip(window) == []


def test_l13_names_of_new_regions(window):
    assert window.sidebar.accessibleName() == DEVICES
    assert window.stream_pane.accessibleName() == TRANSFERS
    assert window.transfers_area.accessibleName() == TRANSFERS
    assert window.brand_label.accessibleName() == "Teleport"
    assert window.brand_label.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert window.brand_logo.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert window.starting_bar.accessibleName() == STARTING_BAR
    assert window.address_label.accessibleName() == ADDRESS_A11Y
    assert window.badge.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert window.status_icon.accessibleName() == ""


# =================================================================================================
# L14 — Đổi ngôn ngữ
# =================================================================================================


def test_l14_language_change_retranslates_new_texts(window, qtbot, tmp_path):
    from teleport.ui.main_window import Page

    window.add_page(Page.DISCOVER, make_page())
    window.show_running("May-A", Path("C:/T"))
    window.set_addresses(["192.0.2.5"], None)
    window.set_transfer_summary(False, 0)
    show_window(window, qtbot, 980, 660)
    translator = ShoutTranslator()
    QCoreApplication.installTranslator(translator)
    try:
        qtbot.waitUntil(lambda: window.machine_card.caption_label.text() == shout(THIS_MACHINE), timeout=3000)
        assert window.badge.text == shout(READY)
        assert window.copy_button.accessibleName() == shout(COPY)
        assert window.save_bar.save_label.text().startswith(shout(SAVE_PREFIX))
        assert window.save_bar.open_button.accessibleName() == shout(OPEN_DIR)
        assert window.save_bar.open_button.text().replace("&", "") == shout(OPEN_DIR)
        assert window.empty_state.title_label.text() == shout(EMPTY_TITLE)
        assert window.discover_button.accessibleName() == shout(DISCOVER)
        window.set_transfer_summary(True, 2)
        assert window.badge.text == shout(BUSY).format(n=2)
    finally:
        QCoreApplication.removeTranslator(translator)
    qtbot.waitUntil(lambda: window.machine_card.caption_label.text() == THIS_MACHINE, timeout=3000)
    assert window.badge.text == BUSY.format(n=2)


# =================================================================================================
# L15 — Nhãn
# =================================================================================================


def test_l15_labels_are_plain_and_not_keyboard_selectable(window, tmp_path):
    running(window, tmp_path)
    window.set_addresses(["192.0.2.5"], None)
    bad = []
    for root in (window.machine_card, window.save_bar, window.sidebar_header, window.start_view):
        for label in root.findChildren(QLabel):
            if label.textFormat() != Qt.TextFormat.PlainText:
                bad.append(f"{label.objectName()}: textFormat {label.textFormat()}")
            if label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByKeyboard:
                bad.append(f"{label.objectName()}: TextSelectableByKeyboard")
    assert bad == []
    for label in (window.name_label, window.address_label):
        assert label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse, label.objectName()
        assert label.focusPolicy() == Qt.FocusPolicy.StrongFocus, label.objectName()


# =================================================================================================
# L16 — Vùng cuộn
# =================================================================================================


def test_l16_programmatic_focus_scrolls_sidebar(window, qtbot, tmp_path):
    running(window, tmp_path)
    window.radar.model.reset([dview(f"May-{i}", paired=i < 2) for i in range(6)])
    show_window(window, qtbot, MIN_W, MIN_H, active=True)
    window.radar.open_address_entry()
    settle()
    edit = window.radar.address_edit
    assert window.focusWidget() is edit
    viewport = window.sidebar_scroll.viewport()
    top_left = edit.mapTo(viewport, QPoint(0, 0))
    rect = edit.rect().translated(top_left)
    assert viewport.rect().contains(rect), f"ô nhập IP {rect} nằm ngoài vùng thấy {viewport.rect()}"


def assert_radar_fits_sidebar(window) -> None:
    """Spec 12.22 (thay phép kiểm cũ `radar.width() >= minimumSizeHint`, luôn đúng): không cuộn ngang
    được, và radar cần không quá bề ngang vùng cuộn trừ lề trái/phải 12 + 12."""
    settle()
    scroll = window.sidebar_scroll
    assert scroll.horizontalScrollBar().maximum() == 0, "cột trái cuộn ngang được: radar rộng quá chỗ"
    need = window.radar.minimumSizeHint().width()
    assert need <= scroll.viewport().width() - 24, (
        f"radar cần {need} px, chỗ {scroll.viewport().width() - 24}"
    )


def test_l16_address_entry_open_fits_sidebar_at_min_size(window, qtbot, tmp_path):
    """12.22 (soat-code vòng 1, cao): khung nhập IP mở ở 720x460 không làm radar rộng hơn cột trái."""
    running(window, tmp_path)
    window.radar.model.reset([dview(f"May-{i}", paired=i < 2) for i in range(6)])
    show_window(window, qtbot, MIN_W, MIN_H, active=True)
    window.radar.open_address_entry()
    settle()
    assert window.radar.address_panel.isVisibleTo(window)
    assert_radar_fits_sidebar(window)


def _partly_hidden_list(window, qtbot, tmp_path):
    """720x460, 8 máy chưa ghép đôi, khung nhập IP mở (tiêu điểm ở ô nhập, cột trái đã cuộn xuống)."""
    running(window, tmp_path)
    window.radar.model.reset([dview(f"May-{i}") for i in range(8)])
    show_window(window, qtbot, MIN_W, MIN_H, active=True)
    window.radar.open_address_entry()
    settle()
    assert window.focusWidget() is window.radar.address_edit
    viewport = window.sidebar_scroll.viewport()
    lv = window.radar.list_view
    shown = lv.rect().translated(lv.mapTo(viewport, QPoint(0, 0)))
    assert not viewport.rect().contains(shown), "danh sách phải bị che một phần (ca của test)"
    return viewport, lv


def test_l16_click_on_partly_hidden_row_is_not_lost(window, qtbot, tmp_path):
    """12.22 (soat-code vòng 1, trung bình): nhấn rồi nhả chuột tại cùng một điểm màn hình trên một
    hàng đang thấy được của danh sách bị che một phần → `row_clicked` phát đúng hàng đó. Tự cuộn tới
    widget có tiêu điểm lúc nhấn chuột sẽ dời hàng khỏi con trỏ và làm mất cú bấm."""
    viewport, lv = _partly_hidden_list(window, qtbot, tmp_path)
    model = window.radar.model
    row = next(
        (
            r
            for r in range(model.rowCount())
            if viewport.rect().contains(
                lv.viewport().mapTo(viewport, lv.visualRect(model.index(r, 0)).center())
            )
        ),
        None,
    )
    assert row is not None, "không có hàng nào đang thấy được"
    point = lv.visualRect(model.index(row, 0)).center()
    screen_point = lv.viewport().mapToGlobal(point)
    clicks: list[int] = []
    lv.row_clicked.connect(lambda index, *_: clicks.append(index.row()))
    QTest.mousePress(lv.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    settle()
    release = lv.viewport().mapFromGlobal(screen_point)  # cùng điểm màn hình, dù view có cuộn
    QTest.mouseRelease(lv.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, release)
    settle()
    assert clicks == [row]


def test_l16_keyboard_focus_on_hidden_widget_still_scrolls(window, qtbot, tmp_path):
    """12.22: bỏ tự cuộn chỉ khi tiêu điểm đến từ chuột; Shift+Tab tới danh sách đang bị che thì cột
    trái vẫn tự cuộn tới nó (L16 giữ)."""
    viewport, lv = _partly_hidden_list(window, qtbot, tmp_path)
    bar = window.sidebar_scroll.verticalScrollBar()
    before = bar.value()
    assert before > 0, "cột trái phải đang cuộn xuống (ca của test)"
    for _ in range(10):
        if window.focusWidget() is lv:
            break
        QTest.keyClick(window.focusWidget(), Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        settle()
    assert window.focusWidget() is lv, "Shift+Tab không tới được danh sách thiết bị"
    assert bar.value() < before, "cột trái không tự cuộn lên tới danh sách"
    shown = lv.rect().translated(lv.mapTo(viewport, QPoint(0, 0)))
    if shown.height() + 16 <= viewport.height():
        assert viewport.rect().contains(shown), f"danh sách {shown} chưa nằm trọn trong {viewport.rect()}"
    else:
        # Cao hơn vùng thấy: `ensureVisible` theo tâm widget thì căn giữa.
        assert viewport.rect().contains(shown.center()), f"danh sách {shown} không căn vào vùng thấy"


def test_l16_scroll_areas_take_no_focus(window):
    assert window.sidebar_scroll.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert window.start_view.start_scroll.focusPolicy() == Qt.FocusPolicy.NoFocus


# =================================================================================================
# L17 — Đổi sáng/tối
# =================================================================================================


def has_color(pixmap, color: QColor, tolerance: int = 6) -> bool:
    """Pixmap có điểm ảnh (alpha ≥ 128, đã bỏ nhân trước alpha) mang màu `color` (lệch ≤ `tolerance`).

    Icon 20 px là nét viền khử răng cưa: không có điểm ảnh nào alpha 255, và `pixel()` của ảnh nhân
    trước alpha trả màu đã tối đi, nên đổi sang ARGB32 trước (như `test_icons.py`)."""
    from PySide6.QtGui import QImage

    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    for y in range(image.height()):
        for x in range(image.width()):
            c = QColor.fromRgba(image.pixel(x, y))
            if (
                c.alpha() >= 128
                and max(
                    abs(c.red() - color.red()), abs(c.green() - color.green()), abs(c.blue() - color.blue())
                )
                <= tolerance
            ):
                return True
    return False


def test_l17_status_icon_recolors_on_theme_change(window, ui_theme):
    light = ui_theme(False)
    window.show_error("locked", "")
    settle()
    assert has_color(window.status_icon.pixmap(), light.danger), "icon lỗi không tô `danger` (sáng)"
    dark = ui_theme(True)
    settle()
    assert has_color(window.status_icon.pixmap(), dark.danger), "icon lỗi không tô lại `danger` (tối)"
    assert not has_color(window.status_icon.pixmap(), light.danger), (
        "icon lỗi còn màu `danger` của chế độ sáng"
    )


# =================================================================================================
# L18 — Lối tắt cũ
# =================================================================================================


def test_l18_f5_in_error_emits_retry(window, qtbot):
    window.show_error("locked", "")
    show_window(window, qtbot, 980, 660, active=True)
    got: list = []
    window.retry_requested.connect(lambda: got.append(True))
    QTest.keyClick(window, Qt.Key.Key_F5)
    assert got == [True]


def test_l18_ctrl_w_hides_window_to_tray(window, qtbot, tmp_path, monkeypatch):
    from teleport.ui import tray

    monkeypatch.setattr(tray, "is_available", lambda: True)
    running(window, tmp_path)
    show_window(window, qtbot, 980, 660, active=True)
    hidden: list = []
    window.hidden_to_tray.connect(lambda: hidden.append(True))
    QTest.keyClick(window, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    settle()
    assert not window.isVisible()
    assert hidden == [True]


def test_l18_escape_on_home_does_nothing(window, qtbot, tmp_path):
    from teleport.ui.main_window import Page

    running(window, tmp_path)
    show_window(window, qtbot, 980, 660, active=True)
    focus = window.focusWidget()
    changes: list = []
    window.page_changed.connect(changes.append)
    QTest.keyClick(window, Qt.Key.Key_Escape)
    settle()
    assert window.current_page is Page.HOME
    assert window.isVisible()
    assert changes == []
    assert window.focusWidget() is focus
