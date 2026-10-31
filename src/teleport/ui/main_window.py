"""Cửa sổ chính: trạng thái của node, màn “Bắt đầu”, khung hai cột (spec B1) và khung trang (spec A3).

Bốn trạng thái (`ViewState`); trang HOME có `home_stack` chọn giữa khối căn giữa `start_view`
(`home.StartPanel`: WELCOME, STARTING, ERROR) và khung hai cột `shell` (RUNNING). Mọi đường vào
trạng thái đi qua `retranslate()`, nơi chọn `home_stack` và ẩn/hiện từng phần.

- WELCOME: màn “Bắt đầu” khi máy chưa có danh tính (spec hackathon mục 10 A1): tên máy này sẽ dùng
  trong mạng, câu mvp F1 “Tên này hiện với mọi máy…”, nút chính “Bắt đầu” (phát `start_requested`).
  Chưa bấm thì `ui.app` không chạy node, không quảng bá gì. F1 đầy đủ để D1.
- STARTING: “Đang khởi động…” kèm thanh bận mảnh (`starting_bar`), không có nút.
- RUNNING: khung hai cột (dưới đây). Không còn dòng chữ trạng thái: tên máy, địa chỉ, huy hiệu nằm ở
  `machine_card`, thư mục nhận ở `save_bar`.
- ERROR: icon lỗi (`status_icon`, màu `danger`, trang trí), “Không khởi động được”, câu lỗi
  (`texts.start_error`) và gợi ý; “Thử lại” là nút chính.

Trong khối căn giữa: logo + “Teleport” (`title_label`, bậc chữ `theme.TextStyle.TITLE`, font điểm
nhấn nếu `ui.fonts.install()` đã nạp trước khi dựng cửa sổ), dòng trạng thái (SECTION), chi tiết, gợi
ý, nút căn phải. `title_label` nằm trong `start_view` nên ở RUNNING không hiện.

**Bố cục hai cột** (RUNNING; cây widget ở spec B1 mục 2.3):
- `body` (`runningBody`): cột trái `sidebar` (`QFrame[surface="sidebar"]`, nền `sidebar`, viền phải
  1 px `border` nằm trong bề ngang của cột) và khung phải `stream_pane` (nền `surface`).
- Cột trái từ trên xuống: `sidebar_header` (logo 32 px, “Teleport”, nút “Tìm máy mới” và “Cài đặt”),
  `sidebar_scroll` (radar `ui.radar.RadarView` trong `sidebarContent`, nền trong suốt; nội dung cao hơn
  chỗ thì cuộn) và `machine_card` (`home.MachineCard`: máy này, địa chỉ, “Chép địa chỉ”, huy hiệu
  “Sẵn sàng nhận” / “Đang truyền n lượt”). Bề ngang `SIDEBAR_WIDTH`, cửa sổ hẹp hơn `COMPACT_BELOW`
  thì `SIDEBAR_COMPACT_WIDTH` (`sidebar_compact`; áp ở `resizeEvent` và `showEvent`). Kiểu chỉ còn
  avatar là việc của B2.
- Khung phải: `empty_state` (“Ghép đôi máy đầu tiên”) khi radar chưa có máy nào đã ghép đôi **và**
  danh sách lượt rỗng (`set_transfer_summary`), không thì `transfers_area` (`ui.transfers` đặt
  `TransfersView` vào). Nút của `empty_state` chỉ có khi đã có trang DISCOVER.
- `save_bar` (`home.SaveBar`, “Lưu vào: …” + “Mở thư mục”) chạy hết chiều ngang dưới `body`; ẩn khi
  không biết thư mục nhận. Bấm “Mở thư mục” phát `open_receive_dir_requested(Path)`.
- Nút đầu cột chỉ hiện khi đã có trang tương ứng (`add_page`): không có nút bấm mà không làm gì.
- Không phần nào làm cửa sổ to hơn `MIN_SIZE`: cột trái cuộn bằng `sidebar_scroll`, khối căn giữa
  bằng `start_scroll`. Tiêu điểm đặt bằng code vào widget trong hai vùng cuộn đó (ví dụ “Nhập IP thủ
  công”) thì vùng cuộn tự cuộn tới (`QApplication.focusChanged`; không khi tiêu điểm đến từ cú bấm chuột,
  để cú bấm vào hàng bị che một phần không trượt).

Dòng địa chỉ (`address_label`, trong `machine_card`) chỉ hiện ở RUNNING khi có ít nhất một địa chỉ:
địa chỉ đầu tiên, thêm `:cổng` khi cổng khác `DEFAULT_PORT`; các địa chỉ còn lại nằm ở tooltip (và
accessibleDescription). “Chép địa chỉ” chép đúng chuỗi đó. Rời RUNNING (`show_welcome`,
`show_starting`, `show_error`) thì quên địa chỉ đã nhớ. `ui.app` đọc địa chỉ qua `NodeHost.submit`
và gọi `set_addresses`.

**Trợ năng** (mvp N12): mọi chữ là chữ thuần (`Qt.PlainText`), có tên cho trình đọc màn hình; nhãn
chọn được bằng chuột nhưng không có con trỏ chữ khi Tab tới (không `TextSelectableByKeyboard`).
Thứ tự Tab ở RUNNING: “Tìm máy mới” → “Cài đặt” → radar → tên máy này → địa chỉ → “Chép địa chỉ” →
khung phải → “Mở thư mục” (luôn cuối: widget tạo sau như thẻ lượt truyền hay trang mới rơi vào cuối
chuỗi, nên `set_transfer_summary` và `add_page` dời nút về cuối). WELCOME / ERROR: dòng trạng thái →
chi tiết → gợi ý → nút. Tiêu điểm ban đầu (`_place_focus`): nút đang hiện (“Bắt đầu” / “Thử lại”);
RUNNING thì danh sách thiết bị khi radar có thiết bị, không thì nút “Nhập IP thủ công”; STARTING thì
dòng trạng thái. Trạng thái luôn nói bằng chữ, không chỉ bằng màu. Phím tắt: F5 = Thử lại (khi nút
đang hiện), Ctrl+W = đóng cửa sổ, Ctrl+Q = Thoát.

Cỡ cửa sổ lúc mở: `default_size(vùng dùng được của màn hình chính)` = `DEFAULT_SIZE` kẹp theo màn
hình; cỡ tối thiểu `MIN_SIZE`. Không lưu cỡ giữa các lần chạy.

Đóng cửa sổ (`closeEvent`): khay dùng được (`Tray.is_available()`) thì ẩn cửa sổ và phát
`hidden_to_tray` (app vẫn chạy và nhận file); không có khay thì phát `quit_requested`. Cửa sổ
không tự thoát app: `ui.app` quyết định. Ẩn xuống khay giữ nguyên trang đang mở.

**Khung trang** (spec A3): `centralWidget()` là `PageStack` (`self.pages`, objectName `pageStack`),
một `QStackedWidget`. Mọi thứ ở trên nằm trong trang HOME (`self.home_page`, objectName `homePage`);
các thuộc tính công khai (`radar`, `transfers_area`, `body`, `status_label`…) giữ tên. Tên trang là
`Page` (HOME, DISCOVER, SETTINGS, WAIT_PAIRING); trang chỉ có khi được `add_page`.
- `PageStack.show_page(page)`: chưa có → KeyError; trùng trang hiện tại thì thôi; HOME xóa lịch sử;
  trang đã nằm trong lịch sử thì cắt lịch sử tới ngay trước nó (không vòng); còn lại đẩy (trang hiện
  tại, `window().focusWidget()`) vào lịch sử. Đổi xong đặt tiêu điểm vào trang (trang tự
  `setFocusProxy` tới control đầu tiên). `back()` lấy phần tử cuối, hiện trang đó và trả tiêu điểm về
  widget đã nhớ nếu nó còn sống, còn trong trang, hiện và bật; không thì vào trang. `reset()` về HOME,
  xóa lịch sử, không đặt tiêu điểm. `page_changed(str)` phát đúng một lần mỗi khi trang đổi thật
  (kể cả do `reset`).
- `MainWindow.open_page(page)`: trang khác HOME chỉ mở được ở RUNNING (các trang khác cần node đang
  chạy), không thì False và đứng yên. `go_back()` = `pages.back()`. `show_welcome`, `show_starting`,
  `show_error` gọi `pages.reset()` trước `retranslate`/`_place_focus`.
- Esc (`_back_action`, `WindowShortcut`, “Quay lại”) chỉ bật khi đang ở trang khác HOME. Hộp thoại
  là cửa sổ riêng nên không bị Esc của cửa sổ chính bắt. Ô nhập trên trang khác HOME phải nhận
  `ShortcutOverride` cho Esc nếu cần phím đó (như `_AddressEdit` của `ui.radar`).
- Không có hoạt ảnh chuyển trang: đổi tức thì.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from itertools import pairwise
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QKeySequence, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from teleport.core.display import display_name
from teleport.net.connection import DEFAULT_PORT
from teleport.ui import texts, theme
from teleport.ui.home import START_PANEL_MAX_WIDTH as START_PANEL_MAX_WIDTH  # hằng của bảng spec B1 mục 2.3
from teleport.ui.home import MachineCard, SaveBar, StartPanel
from teleport.ui.radar import RadarView
from teleport.ui.tray import Tray, make_icon
from teleport.ui.widgets import EmptyState, IconButton

DEFAULT_SIZE = QSize(980, 660)  # cỡ cửa sổ lúc mở khi màn hình đủ rộng
MIN_SIZE = QSize(720, 460)
SIDEBAR_WIDTH = 288  # px, cột trái (kể cả viền phải 1 px)
SIDEBAR_COMPACT_WIDTH = 248  # px, cột trái khi cửa sổ hẹp
COMPACT_BELOW = 820  # px: cửa sổ hẹp hơn thì cột trái thu gọn
_BRAND_LOGO = 32  # px
_LAYOUT_PASSES = 5  # tối đa số lượt chạy layout trước khi cuộn tới widget có tiêu điểm


def default_size(available: QSize) -> QSize:
    """Cỡ cửa sổ lúc mở: `DEFAULT_SIZE` kẹp theo vùng dùng được của màn hình (`available`)."""
    return QSize(min(DEFAULT_SIZE.width(), available.width()), min(DEFAULT_SIZE.height(), available.height()))


class Page(StrEnum):
    """Tên trang của cửa sổ chính; trang chỉ có khi được `add_page` (B5, D2, D3 thêm trang của mình)."""

    HOME = "home"
    DISCOVER = "discover"
    SETTINGS = "settings"
    WAIT_PAIRING = "wait_pairing"


class PageStack(QStackedWidget):
    """Chồng trang của cửa sổ chính, có lịch sử để quay lại (xem docstring module, “Khung trang”)."""

    page_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pageStack")
        self._pages: dict[Page, QWidget] = {}
        # (trang trước, widget có tiêu điểm lúc rời trang đó)
        self._history: list[tuple[Page, QWidget | None]] = []

    def add_page(self, page: Page, widget: QWidget) -> None:
        """Thêm trang; trùng → ValueError. Đặt objectName `<page>Page` nếu widget chưa có tên."""
        page = Page(page)
        if page in self._pages:
            raise ValueError(f"trang đã có: {page}")
        if not widget.objectName():
            widget.setObjectName(f"{page}Page")
        self._pages[page] = widget
        self.addWidget(widget)

    def has_page(self, page: Page) -> bool:
        return page in self._pages

    @property
    def current_page(self) -> Page:
        current = self.currentWidget()
        for page, widget in self._pages.items():
            if widget is current:
                return page
        return Page.HOME

    def show_page(self, page: Page) -> None:
        """Hiện trang `page` (chưa có → KeyError) và đặt tiêu điểm vào nó; ghi lịch sử để quay lại."""
        page = Page(page)
        widget = self._pages[page]
        current = self.current_page
        if page is current:
            return
        if page is Page.HOME:
            self._history.clear()
        else:
            index = next((i for i, (seen, _) in enumerate(self._history) if seen is page), None)
            if index is not None:
                del self._history[index:]  # không tạo vòng
            else:
                self._history.append((current, self.window().focusWidget()))
        self._switch(page, widget)
        widget.setFocus(Qt.FocusReason.OtherFocusReason)

    def back(self) -> bool:
        """Về trang trước trong lịch sử và trả tiêu điểm đã nhớ; lịch sử rỗng → False."""
        if not self._history:
            return False
        page, focus = self._history.pop()
        widget = self._pages[page]
        self._switch(page, widget)
        window = self.window()
        if (
            focus is not None
            and isValid(focus)
            and (focus is widget or widget.isAncestorOf(focus))
            and focus.isVisibleTo(window)
            and focus.isEnabled()
        ):
            focus.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            widget.setFocus(Qt.FocusReason.OtherFocusReason)
        return True

    def reset(self) -> None:
        """Về HOME, xóa lịch sử; không đặt tiêu điểm (bên gọi lo)."""
        self._history.clear()
        if Page.HOME in self._pages and self.current_page is not Page.HOME:
            self._switch(Page.HOME, self._pages[Page.HOME])

    def _switch(self, page: Page, widget: QWidget) -> None:
        self.setCurrentWidget(widget)
        self.page_changed.emit(str(page))


class ViewState(StrEnum):
    WELCOME = "welcome"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


class MainWindow(QMainWindow):
    """Cửa sổ chính; trạng thái đổi bằng các hàm `show_*`."""

    retry_requested = Signal()
    start_requested = Signal()
    quit_requested = Signal()
    hidden_to_tray = Signal()
    page_changed = Signal(str)  # giá trị `Page`; nối thẳng từ `PageStack.page_changed`
    open_receive_dir_requested = Signal(object)  # Path; nối thẳng từ `save_bar.open_requested`

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("mainWindow")
        self.setMinimumSize(MIN_SIZE)
        screen = QGuiApplication.primaryScreen()
        self.resize(DEFAULT_SIZE if screen is None else default_size(screen.availableGeometry().size()))
        self._state = ViewState.STARTING
        self._name: str | None = None
        self._receive_dir: Path | None = None
        self._planned_name = ""
        self._error_code = ""
        self._error_detail = ""
        self._addresses: tuple[str, ...] = ()
        self._port: int | None = None
        self._has_content = False  # danh sách lượt có thẻ hay dải thông báo (`set_transfer_summary`)
        self._active = 0
        self._has_paired = False  # radar có máy đã ghép đôi
        self._scroll_target: QWidget | None = None  # widget chờ được cuộn tới (`_on_focus_changed`)

        # Trang HOME: khối căn giữa (WELCOME / STARTING / ERROR) hay khung hai cột (RUNNING).
        self.pages = PageStack(self)
        self.home_page = QWidget(self.pages)
        self.home_page.setObjectName("homePage")
        home_layout = QVBoxLayout(self.home_page)
        home_layout.setContentsMargins(0, 0, 0, 0)
        home_layout.setSpacing(0)
        self.home_stack = QStackedWidget(self.home_page)
        self.home_stack.setObjectName("homeStack")
        home_layout.addWidget(self.home_stack)

        self.start_view = StartPanel(self.home_stack)
        self.title_label = self.start_view.title_label
        self.status_icon = self.start_view.status_icon
        self.status_label = self.start_view.status_label
        self.detail_label = self.start_view.detail_label
        self.hint_label = self.start_view.hint_label
        self.starting_bar = self.start_view.starting_bar
        self.retry_button = self.start_view.retry_button
        self.start_button = self.start_view.start_button
        self.retry_button.clicked.connect(self.retry_requested)
        self.start_button.clicked.connect(self.start_requested)

        self.shell = QWidget(self.home_stack)
        self.shell.setObjectName("runningShell")
        shell_layout = QVBoxLayout(self.shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        self.body = QWidget(self.shell)
        self.body.setObjectName("runningBody")
        body_layout = QHBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        # Cột trái: đầu cột, radar cuộn được, máy này.
        self.sidebar = QFrame(self.body)
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setProperty(theme.SURFACE_PROPERTY, "sidebar")
        self.sidebar.setFixedWidth(SIDEBAR_WIDTH)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        self.sidebar_header = QWidget(self.sidebar)
        self.sidebar_header.setObjectName("sidebarHeader")
        self.brand_logo = QLabel(self.sidebar_header)
        self.brand_logo.setObjectName("brandLogo")
        self.brand_logo.setTextFormat(Qt.TextFormat.PlainText)
        self.brand_logo.setFixedSize(_BRAND_LOGO, _BRAND_LOGO)
        self.brand_logo.setPixmap(make_icon().pixmap(_BRAND_LOGO))
        self.brand_logo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.brand_label = QLabel(self.sidebar_header)
        self.brand_label.setObjectName("brandLabel")
        self.brand_label.setTextFormat(Qt.TextFormat.PlainText)
        self.brand_label.setFont(theme.font_for(theme.TextStyle.TITLE, self.brand_label.font()))
        self.brand_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.discover_button = IconButton("search", self.tr("Tìm máy mới"), self.sidebar_header)
        self.discover_button.setObjectName("discoverButton")
        self.discover_button.clicked.connect(self._open_discover)
        self.settings_button = IconButton("settings", self.tr("Cài đặt"), self.sidebar_header)
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.clicked.connect(self._open_settings)
        header_layout = QHBoxLayout(self.sidebar_header)
        header_layout.setContentsMargins(16, 16, 12, 8)
        header_layout.setSpacing(theme.SPACING[1])
        header_layout.addWidget(self.brand_logo, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self.brand_label, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addStretch(1)
        header_buttons = QHBoxLayout()
        header_buttons.setSpacing(theme.SPACING[0])
        header_buttons.addWidget(self.discover_button)
        header_buttons.addWidget(self.settings_button)
        header_layout.addLayout(header_buttons)

        self.sidebar_scroll = QScrollArea(self.sidebar)
        self.sidebar_scroll.setObjectName("sidebarScroll")
        self.sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # mặc định StrongFocus: Tab ghé vô ích
        sidebar_content = QWidget()
        sidebar_content.setObjectName("sidebarContent")
        content_layout = QVBoxLayout(sidebar_content)
        content_layout.setContentsMargins(theme.SPACING[2], 0, theme.SPACING[2], theme.SPACING[2])
        content_layout.setSpacing(0)
        self.radar = RadarView(sidebar_content)
        content_layout.addWidget(self.radar)
        self.sidebar_scroll.setWidget(sidebar_content)
        # `setWidget` bật autofill: tắt lại để thấy nền `sidebar` của cột (B2 thay danh sách tô Base).
        viewport = self.sidebar_scroll.viewport()
        if viewport is not None:
            viewport.setAutoFillBackground(False)
        sidebar_content.setAutoFillBackground(False)
        self.radar.setAutoFillBackground(False)

        self.machine_card = MachineCard(self.sidebar)
        self.name_label = self.machine_card.name_label
        self.address_label = self.machine_card.address_label
        self.copy_button = self.machine_card.copy_button
        self.badge = self.machine_card.badge

        sidebar_layout.addWidget(self.sidebar_header)
        sidebar_layout.addWidget(self.sidebar_scroll, 1)
        sidebar_layout.addWidget(self.machine_card)

        # Khung phải: màn rỗng hay danh sách lượt truyền (`ui.transfers`).
        self.stream_pane = QStackedWidget(self.body)
        self.stream_pane.setObjectName("streamPane")
        self.empty_state = EmptyState(self.stream_pane)
        self.empty_state.setObjectName("streamEmpty")
        self.empty_state.action_triggered.connect(self._open_discover)
        self.transfers_area = QWidget(self.stream_pane)
        self.transfers_area.setObjectName("transfersArea")
        area_layout = QVBoxLayout(self.transfers_area)
        area_layout.setContentsMargins(*(theme.SPACING[3],) * 4)
        self.stream_pane.addWidget(self.empty_state)
        self.stream_pane.addWidget(self.transfers_area)

        body_layout.addWidget(self.sidebar)
        body_layout.addWidget(self.stream_pane, 1)

        self.save_bar = SaveBar(self.shell)
        self.save_bar.open_requested.connect(self.open_receive_dir_requested)
        shell_layout.addWidget(self.body, 1)
        shell_layout.addWidget(self.save_bar)

        self.home_stack.addWidget(self.start_view)
        self.home_stack.addWidget(self.shell)

        self.pages.add_page(Page.HOME, self.home_page)
        self.pages.page_changed.connect(self.page_changed)
        self.pages.page_changed.connect(self._on_page_changed)
        self.setCentralWidget(self.pages)

        # Thứ tự Tab: khối căn giữa (`StartPanel` tự nối) rồi khung hai cột; widget ẩn tự bị bỏ qua.
        empty = self.empty_state
        chain: list[QWidget] = [
            self.status_label,
            self.detail_label,
            self.hint_label,
            self.retry_button,
            self.start_button,
            self.discover_button,
            self.settings_button,
            self.radar.first_focus_widget(),
        ]
        for first, second in pairwise(chain):
            QWidget.setTabOrder(first, second)
        rest: list[QWidget] = [
            self.radar.last_focus_widget(),
            self.name_label,
            self.address_label,
            self.copy_button,
            empty.title_label,
            empty.text_label,
            empty.action_button,
            self.save_bar.open_button,  # nội dung của `transfers_area` tạo sau: `_keep_footer_last`
        ]
        for first, second in pairwise(rest):
            QWidget.setTabOrder(first, second)

        model = self.radar.model
        for signal in (model.modelReset, model.rowsInserted, model.rowsRemoved, model.dataChanged):
            signal.connect(self._update_paired)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.focusChanged.connect(self._on_focus_changed)

        self._retry_action = QAction(self)
        self._retry_action.setShortcut(QKeySequence(Qt.Key.Key_F5))
        self._retry_action.triggered.connect(self._retry_shortcut)
        self._close_action = QAction(self)
        self._close_action.setShortcut(QKeySequence("Ctrl+W"))
        self._close_action.triggered.connect(self.close)
        self._quit_action = QAction(self)
        self._quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        self._quit_action.triggered.connect(self.quit_requested)
        self._back_action = QAction(self)
        self._back_action.setShortcut(QKeySequence(Qt.Key.Key_Escape))
        self._back_action.setEnabled(False)
        self._back_action.triggered.connect(self.go_back)
        for action in (self._retry_action, self._close_action, self._quit_action, self._back_action):
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            self.addAction(action)

        self._update_page_buttons()
        self.retranslate()

    # ---- Trạng thái --------------------------------------------------------------------------

    @property
    def view_state(self) -> ViewState:
        return self._state

    @property
    def receive_dir(self) -> Path | None:
        """Thư mục nhận của lần `show_running` gần nhất."""
        return self._receive_dir

    @property
    def sidebar_compact(self) -> bool:
        """Cột trái đang thu gọn (cửa sổ hẹp hơn `COMPACT_BELOW`)."""
        return self.width() < COMPACT_BELOW

    def show_welcome(self, planned_name: str) -> None:
        """Màn “Bắt đầu”: `planned_name` là tên node sẽ dùng (chưa qua `display_name`)."""
        self._state = ViewState.WELCOME
        self._planned_name = planned_name
        self._forget_addresses()
        self.pages.reset()
        self.retranslate()
        self._place_focus()

    def show_starting(self) -> None:
        self._state = ViewState.STARTING
        self._forget_addresses()
        self.pages.reset()
        self.retranslate()
        self._place_focus()

    def show_running(self, name: str | None, receive_dir: Path | None) -> None:
        self._state = ViewState.RUNNING
        self._name, self._receive_dir = name, receive_dir
        self.retranslate()
        self._place_focus()

    def show_error(self, code: str, detail: str = "") -> None:
        """Lỗi khởi động node: `code` là `bridge.StartError`, `detail` đã làm sạch."""
        self._state = ViewState.ERROR
        self._error_code, self._error_detail = code, detail
        self._forget_addresses()
        self.pages.reset()
        self.retranslate()
        self._place_focus()

    def set_transfer_summary(self, has_content: bool, active: int) -> None:
        """Danh sách lượt có gì không (thẻ hay dải thông báo) và số lượt chưa kết thúc (≥ 0).

        Cập nhật huy hiệu của máy này, chọn `empty_state` / `transfers_area`, giữ nút “Mở thư mục”
        cuối chuỗi Tab. `active` < 0 → ValueError.
        """
        if active < 0:
            raise ValueError(f"số lượt phải ≥ 0: {active}")
        self._has_content = bool(has_content)
        self._active = active
        self.machine_card.set_activity(active)
        self._update_stream()
        self._keep_footer_last()

    # ---- Trang -------------------------------------------------------------------------------

    @property
    def current_page(self) -> Page:
        return self.pages.current_page

    def add_page(self, page: Page, widget: QWidget) -> None:
        """Thêm trang `page` (xem `PageStack.add_page`); hiện nút đầu cột và nút của màn rỗng tương ứng."""
        self.pages.add_page(page, widget)
        self._update_page_buttons()
        self._update_empty_content()
        self._keep_footer_last()

    def open_page(self, page: Page) -> bool:
        """Mở trang `page`; trang khác HOME chỉ mở được ở RUNNING (không thì False, đứng yên)."""
        page = Page(page)
        if page is not Page.HOME and self._state is not ViewState.RUNNING:
            return False
        self.pages.show_page(page)
        self._ensure_home_focus()
        return True

    def go_back(self) -> bool:
        """Về trang trước (Esc); không có trang trước thì False."""
        moved = self.pages.back()
        if moved:
            self._ensure_home_focus()
        return moved

    def _ensure_home_focus(self) -> None:
        """Về HOME mà tiêu điểm rơi vào chính `home_page` (không nhận tiêu điểm, không proxy) hay không
        vào đâu được thì đặt lại theo `_place_focus`."""
        if self.pages.current_page is not Page.HOME:
            return
        focus = self.focusWidget()
        if (
            focus is None
            or focus is self.home_page
            or not self.home_page.isAncestorOf(focus)
            or focus.focusPolicy() == Qt.FocusPolicy.NoFocus
            or not focus.isVisibleTo(self)
        ):
            self._place_focus()

    def _open_discover(self) -> None:
        self.open_page(Page.DISCOVER)

    def _open_settings(self) -> None:
        self.open_page(Page.SETTINGS)

    def _on_page_changed(self, page: str) -> None:
        self._back_action.setEnabled(page != Page.HOME)

    def _update_page_buttons(self) -> None:
        self.discover_button.setVisible(self.pages.has_page(Page.DISCOVER))
        self.settings_button.setVisible(self.pages.has_page(Page.SETTINGS))

    def set_addresses(self, addresses: Sequence[str], port: int | None) -> None:
        """Địa chỉ IPv4 của máy này và cổng đang lắng nghe (`Node.local_addresses`, `Node.port`).

        Nhớ lại để `retranslate` dựng lại chữ; chỉ hiện ở RUNNING và khi `addresses` khác rỗng.
        """
        self._addresses = tuple(addresses)
        self._port = port
        self._update_address()

    def _forget_addresses(self) -> None:
        self._addresses, self._port = (), None

    def _place_focus(self) -> None:
        """Đặt tiêu điểm bàn phím: nút đang hiện (Bắt đầu / Thử lại); RUNNING thì danh sách thiết bị
        (có thiết bị) hay nút “Nhập IP thủ công”; còn lại (STARTING) dòng trạng thái.

        Đang ở trang khác HOME thì thôi (`show_running` không đưa về HOME; trang đó tự giữ tiêu điểm)."""
        if self.pages.current_page is not Page.HOME:
            return
        for button in (self.start_button, self.retry_button):
            if button.isVisibleTo(self):
                button.setFocus(Qt.FocusReason.OtherFocusReason)
                return
        if self._state is ViewState.RUNNING:
            target: QWidget = (
                self.radar.list_view if self.radar.model.rowCount() > 0 else self.radar.address_button
            )
            target.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        self.status_label.setFocus(Qt.FocusReason.OtherFocusReason)

    def bring_to_front(self) -> None:
        """Hiện lại cửa sổ (kể cả khi đang thu nhỏ hay ẩn) và đưa lên trước."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    # ---- Chữ ---------------------------------------------------------------------------------

    def retranslate(self) -> None:
        """Đặt lại mọi chữ theo ngôn ngữ hiện tại và trạng thái hiện tại."""
        self.setWindowTitle(self.tr("Teleport"))
        self.title_label.setText(self.tr("Teleport"))
        self.title_label.setAccessibleName(self.tr("Tên ứng dụng"))
        self.brand_label.setText(self.tr("Teleport"))
        self.brand_label.setAccessibleName(self.tr("Teleport"))
        self.retry_button.setText(texts.retry())
        self.retry_button.setAccessibleName(texts.retry())
        self.retry_button.setToolTip(self.tr("Khởi động lại Teleport (F5)"))
        start = self.tr("Bắt đầu")
        self.start_button.setText(start)
        self.start_button.setAccessibleName(start)
        self.discover_button.set_label(self.tr("Tìm máy mới"))
        self.settings_button.set_label(self.tr("Cài đặt"))
        self.sidebar.setAccessibleName(self.tr("Thiết bị"))
        transfers = self.tr("Lượt truyền")
        self.stream_pane.setAccessibleName(transfers)
        self.transfers_area.setAccessibleName(transfers)
        self._retry_action.setText(texts.retry())
        self._close_action.setText(self.tr("Đóng cửa sổ"))
        self._quit_action.setText(texts.menu_quit())
        self._back_action.setText(self.tr("Quay lại"))
        self.start_view.retranslate()
        self.machine_card.retranslate()
        self.save_bar.retranslate()

        status, detail, hints = self._content()
        self.status_label.setText(status)
        self.status_label.setAccessibleName(self.tr("Trạng thái"))
        self.status_label.setAccessibleDescription(status)
        self._set_optional(self.detail_label, detail, self.tr("Chi tiết"))
        self._set_optional(self.hint_label, "\n".join(hints), self.tr("Gợi ý"))
        self._update_address()
        running = self._state is ViewState.RUNNING
        self.home_stack.setCurrentWidget(self.shell if running else self.start_view)
        self.start_view.set_error_icon(self._state is ViewState.ERROR)
        self.starting_bar.setVisible(self._state is ViewState.STARTING)
        self.retry_button.setVisible(self._state is ViewState.ERROR)
        self.start_button.setVisible(self._state is ViewState.WELCOME)
        self.machine_card.set_name(self._name if running else None)
        self.save_bar.set_receive_dir(self._receive_dir if running else None)
        self._update_empty_content()
        self._update_stream()

    def _content(self) -> tuple[str, str, list[str]]:
        """(dòng trạng thái, dòng chi tiết, các dòng gợi ý) của khối căn giữa; RUNNING thì rỗng."""
        if self._state is ViewState.STARTING:
            return self.tr("Đang khởi động…"), "", []
        if self._state is ViewState.WELCOME:
            name_line = texts.fill(
                self.tr("Tên của máy này trong mạng: {name}"), name=display_name(self._planned_name)
            )
            return (
                self.tr("Bắt đầu dùng Teleport"),
                "\n".join(
                    [
                        name_line,
                        self.tr("Tên này hiện với mọi máy trong cùng mạng, kể cả máy lạ chưa ghép đôi."),
                    ]
                ),
                [self.tr("Bấm Bắt đầu để Teleport chạy và hiện máy này với các máy khác trong mạng.")],
            )
        if self._state is ViewState.RUNNING:
            return "", "", []  # tên, địa chỉ, huy hiệu ở `machine_card`; thư mục nhận ở `save_bar`
        head, hints = texts.start_error(self._error_code, self._error_detail)
        return self.tr("Không khởi động được"), head, hints

    def _update_address(self) -> None:
        """Dựng dòng “Địa chỉ: …”, tooltip các địa chỉ khác và chuỗi để chép từ giá trị đã nhớ."""
        suffix = f":{self._port}" if self._port is not None and self._port != DEFAULT_PORT else ""
        shown = [f"{address}{suffix}" for address in self._addresses]
        text = ""
        if self._state is ViewState.RUNNING and shown:
            text = texts.fill(self.tr("Địa chỉ: {address}"), address=shown[0])
        others = ""
        if text and len(shown) > 1:
            others = texts.fill(self.tr("Các địa chỉ khác của máy này: {list}"), list=", ".join(shown[1:]))
        self.address_label.setText(text)
        self.address_label.setAccessibleName(self.tr("Địa chỉ của máy này"))
        self.address_label.setAccessibleDescription(others)
        self.address_label.setToolTip(others)
        self.address_label.setVisible(bool(text))
        self.machine_card.set_copy_value(shown[0] if text else None)

    def _update_empty_content(self) -> None:
        """Chữ và nút của màn rỗng ở khung phải; nút “Tìm máy mới” chỉ khi đã có trang DISCOVER."""
        title = self.tr("Ghép đôi máy đầu tiên")
        if self.pages.has_page(Page.DISCOVER):
            text = self.tr("Mở Teleport trên máy kia, trong cùng mạng với máy này, rồi bấm Tìm máy mới.")
            action: str | None = self.tr("Tìm máy mới")
        else:
            text = self.tr(
                "Mở Teleport trên máy kia, trong cùng mạng với máy này. Máy đó sẽ hiện ở cột bên trái; "
                "bấm vào tên máy để ghép đôi."
            )
            action = None
        empty = self.empty_state
        empty.set_content("arrow_swap", title, text, action=action)
        empty.title_label.setAccessibleName(title)
        empty.text_label.setAccessibleName(text)
        if action is not None:
            empty.action_button.setAccessibleName(action)

    def _update_paired(self) -> None:
        self._has_paired = any(view.paired for view in self.radar.model.devices())
        self._update_stream()

    def _update_stream(self) -> None:
        """Khung phải: màn rỗng khi RUNNING, chưa có máy đã ghép đôi và danh sách lượt rỗng."""
        show_empty = self._state is ViewState.RUNNING and not self._has_paired and not self._has_content
        target = self.empty_state if show_empty else self.transfers_area
        if self.stream_pane.currentWidget() is not target:
            self.stream_pane.setCurrentWidget(target)

    def _keep_footer_last(self) -> None:
        """Dời “Mở thư mục” về cuối chuỗi Tab: widget tạo sau cửa sổ (thẻ lượt truyền, trang mới) rơi
        vào cuối chuỗi, sau nút này."""
        button = self.save_bar.open_button
        last = self.previousInFocusChain()
        # `setTabOrder` bỏ qua widget NoFocus: lùi tới widget cuối cùng nhận được tiêu điểm bằng Tab.
        while last is not None and last is not self and not last.focusPolicy() & Qt.FocusPolicy.TabFocus:
            last = last.previousInFocusChain()
        if last is not None and last is not self and last is not button:
            QWidget.setTabOrder(last, button)

    @staticmethod
    def _set_optional(label: QLabel, text: str, accessible_name: str) -> None:
        label.setText(text)
        label.setAccessibleName(accessible_name)
        label.setAccessibleDescription(text)
        label.setVisible(bool(text))

    # ---- Bố cục ------------------------------------------------------------------------------

    def _apply_sidebar_width(self) -> None:
        width = SIDEBAR_COMPACT_WIDTH if self.sidebar_compact else SIDEBAR_WIDTH
        if self.sidebar.width() != width or self.sidebar.minimumWidth() != width:
            self.sidebar.setFixedWidth(width)

    def _on_focus_changed(self, _old: QWidget | None, new: QWidget | None) -> None:
        """Tiêu điểm vào một widget trong vùng cuộn (kể cả đặt bằng code) → cuộn tới nó."""
        if new is None or not isValid(self) or new.window() is not self:
            return
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            return  # bấm chuột: widget đã thấy được; cuộn lúc nhấn làm cú bấm rơi sang chỗ khác
        if self._scroll_into_view(new):
            # Widget vừa hiện (khung nhập IP) chưa có vị trí thật tới khi layout chạy: cuộn lại sau đó.
            self._scroll_target = new
            QTimer.singleShot(0, self._scroll_pending)

    def _scroll_pending(self) -> None:
        target, self._scroll_target = self._scroll_target, None
        if target is None or not isValid(self) or not isValid(target) or not target.hasFocus():
            return
        # Hàng nút đổi hướng và nhãn xuống dòng làm layout chạy vài lượt: chạy tới khi khoảng cuộn đứng yên.
        bars = (self.sidebar_scroll.verticalScrollBar(), self.start_view.start_scroll.verticalScrollBar())
        for _ in range(_LAYOUT_PASSES):
            before = [bar.maximum() for bar in bars]
            QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            if [bar.maximum() for bar in bars] == before:
                break
        self._scroll_into_view(target)

    def _scroll_into_view(self, widget: QWidget) -> bool:
        for scroll in (self.sidebar_scroll, self.start_view.start_scroll):
            inner = scroll.widget()
            if inner is not None and inner.isAncestorOf(widget):
                # Cả widget, không chỉ con trỏ chữ như `ensureWidgetVisible` (ô nhập bị che mép dưới);
                # widget cao hơn vùng thấy thì căn giữa nó.
                center = widget.mapTo(inner, widget.rect().center())
                half = scroll.viewport().height() // 2
                scroll.ensureVisible(
                    center.x(), center.y(), 0, min(widget.height() // 2 + theme.SPACING[1], half)
                )
                return True
        return False

    # ---- Sự kiện -----------------------------------------------------------------------------

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_sidebar_width()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._apply_sidebar_width()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        kind = event.type()
        if kind == QEvent.Type.LanguageChange:
            self.retranslate()
        elif kind in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self.start_view.refresh_icon()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        if Tray.is_available():
            self.hide()
            self.hidden_to_tray.emit()
        else:
            self.quit_requested.emit()

    def _retry_shortcut(self) -> None:
        if self.retry_button.isVisibleTo(self):
            self.retry_requested.emit()
