"""Hộp “Gửi n file tới…” khi gửi từ File Explorer (đợt F, mục F5, mở rộng mvp F4; N9, N12).

Lệnh `send` của kênh `ui.ipc` (Gửi tới › ZCL Teleport, hay `teleport-gui --send …`) chỉ **điền sẵn**
hộp này. Teleport không bao giờ tự gửi: chỉ nút “Gửi” (bật khi đã chọn máy và đã qua hạn chờ) mới
dẫn tới `TransfersController.request_send`, qua `ui.app` (đường gửi của “Gửi file…” ở khay).

**`SendPickerController`** (`GuiApp.send_picker`, cha là `GuiApp`):
- Yêu cầu đầu thắng, không chồng hộp: đang có hộp mở hay yêu cầu chờ thì `request` bỏ yêu cầu mới
  (trả False). Có hộp thì hộp được đưa lên trước, hiện `busy_notice` ở thanh thông báo của hộp và
  tính lại hạn chờ của nút Gửi. Danh sách file của hộp đang mở không bao giờ đổi.
- Chưa sẵn sàng (màn lần chạy đầu, đang khởi động, lỗi): giữ đúng một yêu cầu (`pending`);
  `set_ready(True)` (do `ui.app` gọi sau ảnh chụp `devices()` đầu tiên của host hiện tại) mở hộp cho
  yêu cầu đó, `discard_pending()` bỏ nó không báo. `set_ready(False)` đóng hộp đang mở.
- `close_all()` đóng hộp và bỏ yêu cầu chờ; gọi lặp không sao.
- Phát `send_confirmed(device_id, tuple[Path, ...])` khi người dùng bấm Gửi (hộp đã đóng), và
  `discover_requested()` khi bấm “Tìm máy mới” ở trạng thái rỗng (hộp đã đóng).

**`SendPickerDialog`** (objectName `sendPickerDialog`, cha là cửa sổ chính, mở bằng `open()`, không
`WA_DeleteOnClose`: controller `deleteLater` khi hộp đóng). Từ trên xuống: tiêu đề `picker_title(n)`
(cũng là `windowTitle` và tên trợ năng); “File sẽ gửi” và danh sách file `sendPickerFiles` (chỉ đọc,
không chọn được; mỗi hàng tên `texts.file_label`, dòng phụ là thư mục cha qua `send.printable`,
tooltip đường dẫn đầy đủ qua `widgets.safe_tooltip`, tên trợ năng `file_row_accessible`; cao tối đa
`FILES_MAX_ROWS` hàng rồi cuộn); “Gửi tới máy” và danh sách máy `sendPickerDevices` (các máy đã ghép
đôi của `ConversationModel`, theo thứ tự của nó, cập nhật sống; tên qua `texts.peer_label`, dòng phụ
trực tuyến/ngoại tuyến bằng chữ; **không chọn sẵn**, kể cả khi chỉ có một máy; máy đang chọn biến
mất thì bỏ chọn); câu cảnh báo `warning_text`; thanh thông báo `sendPickerNotice`; nút “Hủy”
(`sendPickerCancel`) và “Gửi” (`sendPickerSend`, nút mặc định, nút chính tô màu nhấn). Không có máy
đã ghép đôi: thay danh sách bằng `empty_text` và nút “Tìm máy mới” (`sendPickerDiscover`); có máy tới
thì đổi lại ngay và đặt lại tiêu điểm.

**Nút Gửi** chỉ bật khi có máy được chọn **và** hộp đã active ít nhất `SEND_ARM_MS` ms (seam cấp
module `_arm_ms`, đọc lúc dựng hộp): hạn tính từ lần đầu hộp thực sự active (`WindowActivate`), nên
một cú bấm hay phím Enter đang dở lúc hộp bật lên không gửi nhầm. Mỗi lần hộp bị đưa lên lại vì yêu
cầu bận (`show_busy`) thì tắt Gửi và tính hạn lại; `show_busy` chạy tối đa một lần mỗi
`BUSY_REPEAT_MS` (đồng hồ: seam `_clock`, đọc lúc gọi), yêu cầu bận khác trong khoảng đó bị bỏ im
lặng, để tiến trình khác không bật hộp lên liên tục. Máy đang chọn đổi chỗ trong danh sách (máy
khác lên/xuống mạng) thì cũng tắt Gửi và tính hạn lại: hàng dưới con trỏ có thể đã là máy khác.

**Bàn phím trên danh sách máy** (`_DeviceList`): chưa có máy nào được chọn mà nhấn Up/Down/Home/End/
PgUp/PgDn hay Enter thì chỉ chọn hàng hiện hành (hàng đầu khi chưa có) và nuốt phím, không gửi (một
máy: Down chọn được máy đó). Đã chọn rồi thì phím mũi tên như thường; Enter bị Qt cho qua nên `QDialog`
bấm nút mặc định (gửi đúng một lần khi Gửi đang bật); Enter tự lặp (giữ phím) bị nuốt. Bấm đúp /
`activated` chỉ chọn (không nối `activated` vào gì). Enter trên “Hủy” là Hủy. Hủy, Esc, nút đóng:
không phát gì.

**Không I/O trước khi bấm Gửi**: hộp không `stat` file, không dùng `QFileIconProvider` hay API nào
chạm đường dẫn (UNC tới máy không tồn tại không gây truy cập mạng). File có tồn tại không do
`transfers.send` kiểm sau khi bấm Gửi, như kéo thả.

**Bàn phím, trình đọc màn hình (N12)**: tiêu điểm đầu ở danh sách máy (hay “Tìm máy mới”); Tab:
file → máy → (“Tìm máy mới”) → Hủy → Gửi. Mô tả trợ năng của hộp là câu cảnh báo, kèm câu của thanh
thông báo khi có (thanh tự gửi sự kiện Alert). Trạng thái máy luôn bằng chữ, không chỉ bằng màu.
Mọi chữ là chữ thuần (`Qt.PlainText`, `drawText`).

Câu chữ ở ngữ cảnh `"SendPicker"`, gọi lúc dùng; đổi ngôn ngữ thì dịch lại cả hộp.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeyEvent, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from teleport.ui import send, texts, theme
from teleport.ui.conversations import ConversationModel, is_online
from teleport.ui.main_window import MainWindow
from teleport.ui.widgets import InfoBar, InfoLevel, safe_tooltip

SEND_ARM_MS = 500  # ms hộp phải active trước khi nút Gửi bật
FILES_MAX_ROWS = 6  # danh sách file cao tối đa từng này hàng, quá thì cuộn
DEVICES_MIN_ROWS = 3  # danh sách máy cao ít nhất từng này hàng
DIALOG_MAX_WIDTH = 560  # px

BUSY_REPEAT_MS = 2000  # `show_busy` tối đa một lần mỗi từng này ms

# Seam (test): hạn chờ nút Gửi, ms; đọc lúc dựng hộp.
_arm_ms = SEND_ARM_MS
# Seam (test): đồng hồ đơn điệu (giây) cho giới hạn tần suất của `show_busy`; đọc lúc gọi.
_clock: Callable[[], float] = time.monotonic

_SELECT_KEYS = frozenset(
    {
        Qt.Key.Key_Up,
        Qt.Key.Key_Down,
        Qt.Key.Key_Home,
        Qt.Key.Key_End,
        Qt.Key.Key_PageUp,
        Qt.Key.Key_PageDown,
        Qt.Key.Key_Return,
        Qt.Key.Key_Enter,
    }
)
_ENTER_KEYS = frozenset({Qt.Key.Key_Return, Qt.Key.Key_Enter})

_SECONDARY_ROLE = Qt.ItemDataRole.UserRole + 1  # dòng phụ của hàng (str)
_DEVICE_ID_ROLE = Qt.ItemDataRole.UserRole + 2  # device_id của hàng máy (str)
_ELIDE_ROLE = Qt.ItemDataRole.UserRole + 3  # Qt.TextElideMode của dòng phụ
_PAD = 8  # px, lề trong của một hàng
_LINE_GAP = 2  # px giữa hai dòng của một hàng
_Index = QModelIndex | QPersistentModelIndex


# ---- Câu chữ ---------------------------------------------------------------------------------


def picker_title(count: int) -> str:
    """Tiêu đề hộp (cũng là tiêu đề cửa sổ và tên trợ năng)."""
    return QCoreApplication.translate("SendPicker", "Gửi %n file tới…", None, count)


def files_label() -> str:
    return QCoreApplication.translate("SendPicker", "File sẽ gửi")


def file_row_accessible(name: str, folder: str) -> str:
    """Tên trợ năng của một hàng file; `name` đã qua `texts.file_label`, `folder` qua `send.printable`."""
    return texts.fill(
        QCoreApplication.translate("SendPicker", "{name}, trong {folder}"), name=name, folder=folder
    )


def devices_label() -> str:
    return QCoreApplication.translate("SendPicker", "Gửi tới máy")


def warning_text() -> str:
    return QCoreApplication.translate(
        "SendPicker",
        "Teleport chỉ gửi khi bạn chọn máy và bấm Gửi. Nếu bạn không tự chọn các file này, hãy bấm Hủy.",
    )


def cancel_text() -> str:
    return QCoreApplication.translate("SendPicker", "Hủy")


def send_text() -> str:
    return QCoreApplication.translate("SendPicker", "Gửi")


def empty_text() -> str:
    return QCoreApplication.translate(
        "SendPicker", "Chưa ghép đôi với máy nào. Hãy ghép đôi một máy rồi gửi lại từ File Explorer."
    )


def discover_text() -> str:
    return QCoreApplication.translate("SendPicker", "Tìm máy mới")


def busy_notice() -> str:
    return QCoreApplication.translate(
        "SendPicker",
        "Đang mở một hộp gửi file. Hãy gửi hoặc hủy các file này trước, rồi gửi tiếp từ File Explorer.",
    )


# ---- Hàng hai dòng ---------------------------------------------------------------------------


class _TwoLineDelegate(QStyledItemDelegate):
    """Hàng hai dòng: chữ chính (DisplayRole) và dòng phụ (`_SECONDARY_ROLE`), chữ thuần, rút gọn “…”."""

    def _fonts(self, option: QStyleOptionViewItem) -> tuple[QFont, QFont]:
        base = QFont(option.font)
        return base, theme.font_for(theme.TextStyle.CAPTION, base)

    def sizeHint(self, option: QStyleOptionViewItem, index: _Index) -> QSize:
        primary, secondary = self._fonts(option)
        height = QFontMetrics(primary).height() + QFontMetrics(secondary).height() + _LINE_GAP + 2 * _PAD
        return QSize(_PAD * 2 + 120, height)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: _Index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        primary_text = opt.text
        secondary_text = index.data(_SECONDARY_ROLE)
        secondary_text = secondary_text if isinstance(secondary_text, str) else ""
        elide = index.data(_ELIDE_ROLE)
        elide_mode = elide if isinstance(elide, Qt.TextElideMode) else Qt.TextElideMode.ElideRight
        focused = bool(opt.state & QStyle.StateFlag.State_HasFocus) and (widget is None or widget.hasFocus())
        opt.text = ""
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        pal = opt.palette
        main_color = pal.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text)
        muted_color = main_color if selected else pal.color(QPalette.ColorRole.PlaceholderText)
        primary_font, secondary_font = self._fonts(opt)
        area = opt.rect.adjusted(_PAD, _PAD, -_PAD, -_PAD)
        primary_metrics = QFontMetrics(primary_font)
        secondary_metrics = QFontMetrics(secondary_font)
        top = QRect(area.left(), area.top(), area.width(), primary_metrics.height())
        bottom = QRect(area.left(), top.bottom() + 1 + _LINE_GAP, area.width(), secondary_metrics.height())
        painter.save()
        painter.setFont(primary_font)
        painter.setPen(main_color)
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        painter.drawText(
            top, flags, primary_metrics.elidedText(primary_text, Qt.TextElideMode.ElideRight, top.width())
        )
        if secondary_text:
            painter.setFont(secondary_font)
            painter.setPen(muted_color)
            painter.drawText(
                bottom, flags, secondary_metrics.elidedText(secondary_text, elide_mode, bottom.width())
            )
        if focused:
            dark = theme.is_dark_palette(pal)
            pen = QPen(QColor(theme.focus_ring_color(dark)))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.drawRoundedRect(opt.rect.adjusted(1, 1, -1, -1), 4, 4)
        painter.restore()


def _list(object_name: str, cls: type[QListWidget] = QListWidget) -> QListWidget:
    view = cls()
    view.setObjectName(object_name)
    view.setItemDelegate(_TwoLineDelegate(view))
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setTabKeyNavigation(False)
    view.setUniformItemSizes(True)
    view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return view


class _DeviceList(QListWidget):
    """Danh sách máy: phím đầu tiên khi chưa chọn máy chỉ chọn hàng hiện hành (docstring module)."""

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in _ENTER_KEYS and event.isAutoRepeat():
            event.accept()  # giữ Enter không gửi
            return
        if key in _SELECT_KEYS and not self.selectedItems() and self.count() > 0:
            item = self.currentItem() or self.item(0)
            if item is not None:
                self.setCurrentItem(item)
                item.setSelected(True)
            event.accept()
            return
        super().keyPressEvent(event)


def _row_height(view: QListWidget) -> int:
    delegate = view.itemDelegate()
    option = QStyleOptionViewItem()
    option.font = view.font()
    return delegate.sizeHint(option, QModelIndex()).height()


# ---- Hộp -------------------------------------------------------------------------------------


class SendPickerDialog(QDialog):
    """Hộp chọn máy nhận cho các file đã điền sẵn (docstring module)."""

    confirmed = Signal(str, object)  # device_id, tuple[Path, ...]; hộp đã đóng
    discover_requested = Signal()  # hộp đã đóng

    def __init__(
        self, paths: Sequence[Path], model: ConversationModel, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._paths = tuple(Path(p) for p in paths)
        self._model = model
        self._rows: tuple[tuple[str, str, bool], ...] | None = None  # (device_id, tên, trực tuyến)
        self._empty: bool | None = None
        self._resolved = False  # đã gửi / đã đóng: không phát gì nữa
        self._arm_delay = max(0, int(_arm_ms))
        self._armed = False
        self._arm_on_activate = True  # tính hạn ở lần active tới
        self._notice = False  # đang hiện `busy_notice`
        self._busy_at: float | None = None  # lần `show_busy` gần nhất (giây, `_clock`)
        self._arm_timer = QTimer(self)
        self._arm_timer.setSingleShot(True)
        self._arm_timer.timeout.connect(self._on_armed)

        self.setObjectName("sendPickerDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMinimumWidth(theme.DIALOG_MIN_WIDTH)
        self.setMaximumWidth(DIALOG_MAX_WIDTH)

        self.title_label = self._label("sendPickerTitle")
        self.title_label.setFont(theme.font_for(theme.TextStyle.SECTION, self.title_label.font()))
        self.title_label.setWordWrap(True)
        self.files_caption = self._label("sendPickerFilesLabel")
        self.files_view = _list("sendPickerFiles")
        self.files_view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.files_caption.setBuddy(self.files_view)
        self.devices_caption = self._label("sendPickerDevicesLabel")
        self.devices_view = _list("sendPickerDevices", _DeviceList)
        self.devices_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.devices_caption.setBuddy(self.devices_view)
        self.empty_label = self._label("sendPickerEmpty")
        self.empty_label.setWordWrap(True)
        self.discover_button = QPushButton()
        self.discover_button.setObjectName("sendPickerDiscover")
        self.discover_button.setAutoDefault(True)
        self.warning_label = self._label("sendPickerWarning")
        self.warning_label.setWordWrap(True)
        self.cancel_button = QPushButton()
        self.cancel_button.setObjectName("sendPickerCancel")
        # Nút đang có tiêu điểm nhận Enter (autoDefault): Enter trên “Hủy” là Hủy, không bao giờ là Gửi.
        self.cancel_button.setAutoDefault(True)
        self.send_button = QPushButton()
        self.send_button.setObjectName("sendPickerSend")
        self.send_button.setProperty(theme.PRIMARY_PROPERTY, True)
        self.send_button.setDefault(True)  # Enter trên danh sách máy: gửi khi nút đang bật
        self.send_button.setEnabled(False)
        self.notice = InfoBar()
        self.notice.setObjectName("sendPickerNotice")
        self.notice.dismissed.connect(self._on_notice_dismissed)

        empty_row = QHBoxLayout()
        empty_row.addWidget(self.discover_button, 0, Qt.AlignmentFlag.AlignLeft)
        empty_row.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.send_button)
        layout = QVBoxLayout(self)
        layout.setSpacing(theme.SPACING[1])
        layout.addWidget(self.title_label)
        layout.addSpacing(theme.SPACING[1])
        layout.addWidget(self.files_caption)
        layout.addWidget(self.files_view)
        layout.addSpacing(theme.SPACING[1])
        layout.addWidget(self.devices_caption)
        layout.addWidget(self.devices_view, 1)
        layout.addWidget(self.empty_label)
        layout.addLayout(empty_row)
        layout.addSpacing(theme.SPACING[1])
        layout.addWidget(self.warning_label)
        layout.addWidget(self.notice)
        layout.addSpacing(theme.SPACING[1])
        layout.addLayout(buttons)
        QWidget.setTabOrder(self.files_view, self.devices_view)
        QWidget.setTabOrder(self.devices_view, self.discover_button)
        QWidget.setTabOrder(self.discover_button, self.cancel_button)
        QWidget.setTabOrder(self.cancel_button, self.send_button)

        self.cancel_button.clicked.connect(self.reject)
        self.send_button.clicked.connect(self._on_send)
        self.discover_button.clicked.connect(self._on_discover)
        selection = self.devices_view.selectionModel()
        selection.selectionChanged.connect(self._refresh_send)
        for signal in (
            model.rowsInserted,
            model.rowsRemoved,
            model.modelReset,
            model.layoutChanged,
            model.dataChanged,
        ):
            signal.connect(self._sync_devices)

        self.retranslate()
        self._fit_files_height()
        self.devices_view.setMinimumHeight(
            DEVICES_MIN_ROWS * _row_height(self.devices_view) + 2 * self.devices_view.frameWidth()
        )
        (self.discover_button if self._empty else self.devices_view).setFocus()

    @staticmethod
    def _label(object_name: str) -> QLabel:
        label = QLabel()
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        return label

    # -- Trạng thái -------------------------------------------------------------------------------

    @property
    def paths(self) -> tuple[Path, ...]:
        return self._paths

    @property
    def armed(self) -> bool:
        """Đã qua hạn chờ của nút Gửi."""
        return self._armed

    def selected_device(self) -> str | None:
        """`device_id` của máy đang chọn, hay None."""
        for item in self.devices_view.selectedItems():
            value = item.data(_DEVICE_ID_ROLE)
            if isinstance(value, str):
                return value
        return None

    def device_ids(self) -> list[str]:
        """Các máy đang có trong danh sách, theo thứ tự hiện."""
        return [row[0] for row in self._rows or ()]

    def select_device(self, device_id: str) -> bool:
        """Chọn máy `device_id` (như bấm chuột); không có → False."""
        for row in range(self.devices_view.count()):
            item = self.devices_view.item(row)
            if item.data(_DEVICE_ID_ROLE) == device_id:
                self.devices_view.setCurrentItem(item)
                item.setSelected(True)
                return True
        return False

    def busy_allowed(self) -> bool:
        """`show_busy` lúc này có chạy không (đã đóng, hay chưa qua `BUSY_REPEAT_MS` → False)."""
        if self._resolved:
            return False
        last = self._busy_at
        return last is None or (_clock() - last) * 1000 >= BUSY_REPEAT_MS

    def show_busy(self) -> bool:
        """Yêu cầu gửi khác tới khi hộp đang mở: đưa hộp lên trước, câu bận, tính lại hạn chờ.

        Tối đa một lần mỗi `BUSY_REPEAT_MS`; bị bỏ (im lặng) → False."""
        if not self.busy_allowed():
            return False
        self._busy_at = _clock()
        self._notice = True
        self.notice.show_message(InfoLevel.INFO, busy_notice)
        self._update_description()
        self._rearm()
        self.raise_()
        self.activateWindow()
        return True

    # -- Qt ---------------------------------------------------------------------------------------

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowActivate and self._arm_on_activate and not self._resolved:
            self._arm_on_activate = False
            self._start_arm()
        return super().event(event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()

    def done(self, result: int) -> None:
        self._resolved = True
        self._arm_timer.stop()
        super().done(result)

    def retranslate(self) -> None:
        title = picker_title(len(self._paths))
        self.setWindowTitle(title)
        self.setAccessibleName(title)
        self.title_label.setText(title)
        caption = files_label()
        self.files_caption.setText(caption)
        self.files_view.setAccessibleName(caption)
        caption = devices_label()
        self.devices_caption.setText(caption)
        self.devices_view.setAccessibleName(caption)
        self.empty_label.setText(empty_text())
        self.discover_button.setText(discover_text())
        self.discover_button.setAccessibleName(discover_text())
        self.warning_label.setText(warning_text())
        for button, text in ((self.cancel_button, cancel_text()), (self.send_button, send_text())):
            button.setText(text)
            button.setAccessibleName(text)
        self._fill_files()
        self._rows = None
        self._sync_devices()
        self._update_description()

    # -- Bên trong -------------------------------------------------------------------------------

    def _update_description(self) -> None:
        parts = [warning_text()]
        if self._notice and self.notice.isVisibleTo(self):
            parts.append(self.notice.title)
        self.setAccessibleDescription(" ".join(parts))

    def _on_notice_dismissed(self) -> None:
        self._notice = False
        self._update_description()

    def _fill_files(self) -> None:
        view = self.files_view
        view.clear()
        for path in self._paths:
            name = texts.file_label(path.name or str(path))
            folder = send.printable(str(path.parent))
            item = QListWidgetItem(name)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            item.setData(_SECONDARY_ROLE, folder)
            item.setData(_ELIDE_ROLE, Qt.TextElideMode.ElideMiddle)
            item.setData(Qt.ItemDataRole.ToolTipRole, safe_tooltip(send.printable(str(path))))
            item.setData(Qt.ItemDataRole.AccessibleTextRole, file_row_accessible(name, folder))
            view.addItem(item)

    def _fit_files_height(self) -> None:
        view = self.files_view
        rows = max(1, min(FILES_MAX_ROWS, view.count()))
        view.setFixedHeight(rows * _row_height(view) + 2 * view.frameWidth())

    def _sync_devices(self) -> None:
        """Dựng lại danh sách máy khi tập máy, tên hay trạng thái đổi; giữ máy đang chọn nếu còn."""
        rows = tuple(
            (view.device_id, texts.peer_label(view.name, view.device_id), is_online(view))
            for view in self._model.devices()
        )
        if rows != self._rows:
            old_order = [row[0] for row in self._rows or ()]
            selected = self.selected_device()
            self._rebuild_devices(rows)
            new_order = [row[0] for row in rows]
            if (
                selected is not None
                and selected in old_order
                and selected in new_order
                and old_order.index(selected) != new_order.index(selected)
            ):
                self._rearm()  # máy đang chọn đổi chỗ: hàng dưới con trỏ có thể đã là máy khác
        self._apply_empty(not rows)
        self._refresh_send()

    def _rebuild_devices(self, rows: tuple[tuple[str, str, bool], ...]) -> None:
        view = self.devices_view
        selected = self.selected_device()
        current_item = view.currentItem()
        current = current_item.data(_DEVICE_ID_ROLE) if current_item is not None else None
        self._rows = rows
        selection = view.selectionModel()
        selection.blockSignals(True)
        try:
            view.clear()
            for device_id, name, online in rows:
                presence = texts.presence_online() if online else texts.presence_offline()
                item = QListWidgetItem(name)
                item.setData(_DEVICE_ID_ROLE, device_id)
                item.setData(_SECONDARY_ROLE, presence)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{name}. {presence}")
                item.setData(Qt.ItemDataRole.ToolTipRole, safe_tooltip(f"{name}\n{presence}"))
                view.addItem(item)
                if device_id == current:
                    view.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
                if device_id == selected:
                    item.setSelected(True)
        finally:
            selection.blockSignals(False)
        view.viewport().update()

    def _apply_empty(self, empty: bool) -> None:
        """Danh sách máy hay trạng thái rỗng; đổi khi hộp đang mở thì đặt lại tiêu điểm (D4)."""
        if empty == self._empty:
            return
        first = self._empty is None
        had_focus = self.devices_view.hasFocus() or self.discover_button.hasFocus()
        self._empty = empty
        self.devices_caption.setVisible(not empty)
        self.devices_view.setVisible(not empty)
        self.empty_label.setVisible(empty)
        self.discover_button.setVisible(empty)
        if not first and (had_focus or self.focusWidget() is None):
            (self.discover_button if empty else self.devices_view).setFocus()

    def _refresh_send(self) -> None:
        enabled = self._armed and not self._resolved and self.selected_device() is not None
        self.send_button.setEnabled(enabled)

    def _start_arm(self) -> None:
        if self._arm_delay <= 0:
            self._on_armed()
        else:
            self._arm_timer.start(self._arm_delay)

    def _rearm(self) -> None:
        self._armed = False
        self._arm_timer.stop()
        self._refresh_send()
        if self.isActiveWindow():
            self._arm_on_activate = False
            self._start_arm()
        else:
            self._arm_on_activate = True

    def _on_armed(self) -> None:
        if self._resolved:
            return
        self._armed = True
        self._refresh_send()

    def _on_send(self) -> None:
        device_id = self.selected_device()
        if self._resolved or not self._armed or device_id is None:
            return
        paths = self._paths
        self.done(QDialog.DialogCode.Accepted.value)
        self.confirmed.emit(device_id, paths)

    def _on_discover(self) -> None:
        if self._resolved:
            return
        self.done(QDialog.DialogCode.Rejected.value)
        self.discover_requested.emit()


# ---- Controller ------------------------------------------------------------------------------


class SendPickerController(QObject):
    """Một hộp “Gửi n file tới…” cùng lúc, và một yêu cầu chờ khi node chưa sẵn sàng (docstring module)."""

    send_confirmed = Signal(str, object)  # device_id, tuple[Path, ...]
    discover_requested = Signal()

    def __init__(self, window: MainWindow, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window = window
        self._ready = False
        self._dialog: SendPickerDialog | None = None
        self._pending: tuple[Path, ...] | None = None

    @property
    def dialog(self) -> SendPickerDialog | None:
        return self._dialog

    @property
    def pending(self) -> tuple[Path, ...] | None:
        return self._pending

    @property
    def ready(self) -> bool:
        return self._ready

    def request(self, paths: Sequence[Path]) -> bool:
        """Yêu cầu mở hộp cho `paths`; True khi được nhận (mở ngay hay giữ chờ), False khi bị bỏ."""
        files = tuple(Path(p) for p in paths)
        if not files:
            return False
        dialog = self._dialog
        if dialog is not None:
            if dialog.busy_allowed():  # giới hạn tần suất: không đưa cửa sổ lên liên tục
                self._window.bring_to_front()
                dialog.show_busy()
            return False
        if self._pending is not None:
            return False
        if not self._ready:
            self._pending = files
            return True
        self._open(files)
        return True

    def set_ready(self, ready: bool) -> None:
        """Node chạy và đã có ảnh chụp thiết bị (True): mở hộp cho yêu cầu chờ. False: đóng hộp."""
        self._ready = ready
        if not ready:
            self._close_dialog()
            return
        pending, self._pending = self._pending, None
        if pending is not None and self._dialog is None:
            self._open(pending)

    def discard_pending(self) -> None:
        """Bỏ yêu cầu chờ mà không báo (node không khởi động được)."""
        self._pending = None

    def close_all(self) -> None:
        """Đóng hộp và bỏ yêu cầu chờ (Thoát, `shutdown`); gọi lặp không sao."""
        self._pending = None
        self._close_dialog()

    def _open(self, paths: tuple[Path, ...]) -> None:
        window = self._window
        window.bring_to_front()
        dialog = SendPickerDialog(paths, window.conversations.model, window)
        dialog.confirmed.connect(self.send_confirmed)
        dialog.discover_requested.connect(self.discover_requested)
        dialog.finished.connect(functools.partial(self._on_finished, dialog))
        self._dialog = dialog
        dialog.open()

    def _on_finished(self, dialog: SendPickerDialog, _result: int) -> None:
        if self._dialog is dialog:
            self._dialog = None
        dialog.deleteLater()

    def _close_dialog(self) -> None:
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.reject()
            dialog.deleteLater()
