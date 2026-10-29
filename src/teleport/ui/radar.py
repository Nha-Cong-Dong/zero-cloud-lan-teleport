"""Radar (P4-4h, mvp F2): danh sách thiết bị trong mạng hoặc đã ghép đôi, và ô nhập IP thủ công.

**Model** (`DeviceListModel`): mỗi `device_id` một hàng, giữ `DeviceView` mới nhất, sắp theo
(NFC(tên).casefold(), device_id) như `app.devices`. Nguồn dữ liệu: ảnh chụp `Node.devices()` lúc
node `started` (`reset`), rồi từng `NodeEvent` (`apply`): `DeviceOnline`, `DeviceConnected`,
`DeviceDisconnected`, `ConnectFailed` cập nhật hay thêm hàng; `DeviceOffline` bỏ hàng của thiết bị
chưa ghép đôi, còn thiết bị đã ghép đôi thì giữ hàng (không thấy trong mạng). Host mới (“Thử lại”)
→ `clear()`.

**Lỗi kết nối gần nhất** (spec hackathon mục 9.5): model nhớ `ConnectFailed.result` gần nhất của
mỗi thiết bị, xóa khi có `DeviceConnected`; `reset` (nạp lại `devices()`) không xóa. Câu hiện trên
hàng là câu mvp mục 8 (`connect_failed_text`); câu “Thấy … nhưng không kết nối được” chỉ hiện khi
thiết bị còn trong mạng, các lỗi khác hiện tới khi kết nối lại được. Thiết bị không còn ghép đôi thì
không hiện lỗi kết nối.

**Chữ của máy khác**: tên thiết bị đi qua `texts.peer_label` (`core.display.display_name`, ký tự
điều khiển thành U+FFFD, ≤ 32 cột); hàng của `QListView` vẽ chữ thuần, không HTML. Tên hệ điều hành
chỉ hiện khi là một trong các giá trị đã biết (`windows`, `macos`, `linux`).

**Signal** (`ui.app` / `ui.transfers` nối): `pair_requested(DeviceView)` (chỉ khi thiết bị chưa
ghép đôi và đang trong mạng, spec mục 9.1); `manual_address_entered(ip, cổng)` (đã qua
`app.devices.parse_manual_address`); `send_files_requested(device_id, list[Path])` (kéo thả);
`pick_files_requested(device_id)` (menu “Gửi file…”); `drop_hint_requested(câu)` (thả vào chỗ
không có thiết bị); `unpair_requested(device_id)`.

**Kéo thả** (mvp F4): danh sách nhận kéo khi `mimeData().hasUrls()` có ít nhất một URL file cục bộ
(`ui.send.local_paths`, không chạm đĩa), tô viền hàng dưới con trỏ (màu `Highlight` của palette)
mà không đổi hàng đang chọn. Thả lên hàng → `drop_files(id, urls)` → `send_files_requested(id,
paths)`, kể cả thiết bị chưa ghép đôi đang trong mạng (`ui.transfers` lo “ghép rồi gửi”); hàng của
thiết bị chưa ghép đôi không trong mạng (`not can_receive_files`) thì `dragMoveEvent` từ chối thả
(con trỏ hiện “không được thả”), không tô. Thả vào chỗ trống (hay
vào câu “Chưa thấy thiết bị nào”) → `drop_hint_requested` với câu mvp mục 8 “Thả file lên một thiết
bị để gửi.”. URL không phải file cục bộ bị bỏ. `drop_files` là hàm công khai (seam test).

**Bàn phím và trình đọc màn hình** (mvp N12): Tab / Shift+Tab đi qua **từng** thiết bị rồi mới ra
khỏi danh sách (vào bằng Tab thì tới thiết bị đầu, bằng Shift+Tab thì tới thiết bị cuối); Enter
(hay nhấp đúp) trên thiết bị chưa ghép đôi đang trong mạng = “Ghép đôi…”; phím Menu (hay
Shift+F10, chuột phải) mở menu của thiết bị đang chọn bằng `popup()` (không `exec()`): “Gửi file…”
(thiết bị đã ghép đôi, hay chưa ghép đôi mà đang trong mạng), “Ghép đôi…”, “Hủy ghép đôi…”. Mỗi hàng có
`AccessibleTextRole` một dòng; trạng thái luôn nói bằng chữ, không chỉ bằng màu.

**Nhập IP thủ công**: nút luôn hiện (mvp F2) mở một khung ngay trong radar (không hộp thoại chặn):
ô nhập, “Ghép đôi”, “Hủy”. Sai thì hiện câu mvp mục 8 (dòng `--address` sai, bỏ chữ `--address`),
không hiện `str(e)` của `parse_manual_address`, không phát signal. Esc trong ô nhập = Hủy.
"""

from __future__ import annotations

import html
import unicodedata
from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QCoreApplication,
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QContextMenuEvent,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QFocusEvent,
    QFont,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from teleport.app.devices import DeviceView, parse_manual_address
from teleport.app.node import (
    ConnectFailed,
    DeviceConnected,
    DeviceDisconnected,
    DeviceOffline,
    DeviceOnline,
    NodeEvent,
)
from teleport.core.links import ConnectResult
from teleport.ui import send, texts

DEVICE_ROLE = Qt.ItemDataRole.UserRole + 1  # DeviceView của hàng
DEVICE_ID_ROLE = Qt.ItemDataRole.UserRole + 2  # device_id (str)
FAILURE_ROLE = Qt.ItemDataRole.UserRole + 3  # ConnectResult đang hiện trên hàng, hoặc None

_NODE_EVENTS = (DeviceOnline, DeviceOffline, DeviceConnected, DeviceDisconnected, ConnectFailed)
_OS_LABELS = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}
_Index = QModelIndex | QPersistentModelIndex


# ---- Câu chữ -------------------------------------------------------------------------------------


def no_devices_text() -> str:
    """mvp mục 8, “Không thấy thiết bị nào”."""
    return QCoreApplication.translate(
        "Radar",
        "Chưa thấy thiết bị nào. Hãy mở Teleport trên máy kia và kiểm tra hai máy dùng chung một mạng Wi-Fi.",
    )


def connect_failed_text(view: DeviceView, result: ConnectResult) -> str:
    """Câu mvp mục 8 cho một `ConnectResult` ≠ OK (OK → chuỗi rỗng). Tên qua `texts.peer_label`."""
    t = QCoreApplication.translate
    if result is ConnectResult.OK:
        return ""
    if result is ConnectResult.REJECTED:
        template = t("Radar", "{name} từ chối kết nối từ máy này. Có thể máy đó đã hủy ghép đôi.")
    elif result is ConnectResult.IDENTITY_MISMATCH:
        template = t(
            "Radar",
            "{name} đang dùng một danh tính khác với lúc ghép đôi. Có thể có người đang giả mạo máy này. "
            "Kết nối đã bị chặn.",
        )
    elif result is ConnectResult.VERSION_MISMATCH:
        template = t(
            "Radar", "{name} đang dùng phiên bản Teleport khác. Hãy cập nhật cả hai máy lên cùng phiên bản."
        )
    else:  # UNREACHABLE
        template = t(
            "Radar",
            "Thấy {name} nhưng không kết nối được. Mạng Wi-Fi này có thể đang chặn các thiết bị nói "
            "chuyện với nhau (hay gặp ở quán cà phê, khách sạn).",
        )
    return texts.fill(template, name=texts.peer_label(view.name, view.device_id))


def invalid_address_text(entered: str) -> str:
    """mvp mục 8, dòng địa chỉ sai (bản giao diện, không có chữ `--address`)."""
    # Chữ người dùng gõ: ký tự điều khiển thành U+FFFD, ≤ 32 cột như tên máy (`display_name`).
    shown = texts.peer_label(entered.strip(), "")
    return texts.fill(
        QCoreApplication.translate(
            "Radar",
            "{address} không phải địa chỉ IPv4 hợp lệ. Hãy nhập ví dụ 192.168.1.5 hoặc 192.168.1.5:47474.",
        ),
        address=shown,
    )


def drop_hint_text() -> str:
    """mvp mục 8: gợi ý khi thả file vào chỗ không có thiết bị."""
    return QCoreApplication.translate("Radar", "Thả file lên một thiết bị để gửi.")


def can_receive_files(view: DeviceView) -> bool:
    """Gửi file tới được (kéo thả, “Gửi file…”): đã ghép đôi, hay chưa ghép đôi mà đang trong mạng."""
    return view.paired or view.online


def _paired_tag(paired: bool) -> str:
    t = QCoreApplication.translate
    return t("Radar", "(đã ghép)") if paired else t("Radar", "(chưa ghép)")


def _status_text(view: DeviceView) -> str:
    t = QCoreApplication.translate
    if view.connected:
        return t("Radar", "đã kết nối")
    if view.online:
        return t("Radar", "đang trong mạng")
    return t("Radar", "không thấy trong mạng")


# ---- Model ---------------------------------------------------------------------------------------


def _sort_key(view: DeviceView) -> tuple[str, str]:
    return unicodedata.normalize("NFC", view.name).casefold(), view.device_id


class DeviceListModel(QAbstractListModel):
    """Các thiết bị của radar, mỗi `device_id` một hàng (chỉ dùng trên thread Qt)."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._rows: list[DeviceView] = []
        self._failures: dict[str, ConnectResult] = {}

    # -- API ------------------------------------------------------------------------------------

    def devices(self) -> tuple[DeviceView, ...]:
        """Các thiết bị theo thứ tự hàng."""
        return tuple(self._rows)

    def device(self, device_id: str) -> DeviceView | None:
        row = self.row_of(device_id)
        return None if row < 0 else self._rows[row]

    def row_of(self, device_id: str) -> int:
        """Hàng của thiết bị; -1 nếu không có."""
        for row, view in enumerate(self._rows):
            if view.device_id == device_id:
                return row
        return -1

    def failure(self, device_id: str) -> ConnectResult | None:
        """`ConnectFailed.result` gần nhất chưa bị xóa bởi `DeviceConnected` (kể cả khi đang không hiện)."""
        return self._failures.get(device_id)

    def shown_failure(self, view: DeviceView) -> ConnectResult | None:
        """Lỗi kết nối đang hiện trên hàng của `view` (xem docstring module)."""
        result = self._failures.get(view.device_id)
        if result is None or not view.paired or view.connected:
            return None
        if result is ConnectResult.UNREACHABLE and not view.online:
            return None
        return result

    def reset(self, views: Iterable[DeviceView]) -> None:
        """Thay mọi hàng bằng ảnh chụp `Node.devices()`; giữ lỗi kết nối đã nhớ (mục 9.5)."""
        unique: dict[str, DeviceView] = {}
        for view in views:
            if isinstance(view, DeviceView):
                unique[view.device_id] = view
        self.beginResetModel()
        self._rows = sorted(unique.values(), key=_sort_key)
        self.endResetModel()

    def clear(self) -> None:
        """Bỏ mọi hàng và mọi lỗi đã nhớ (host mới)."""
        self.beginResetModel()
        self._rows = []
        self._failures.clear()
        self.endResetModel()

    def apply(self, event: NodeEvent) -> None:
        """Cập nhật theo một `NodeEvent`; sự kiện lạ bị bỏ qua."""
        if not isinstance(event, _NODE_EVENTS):
            return
        view = event.device
        if not isinstance(view, DeviceView):
            return
        if isinstance(event, ConnectFailed):
            if event.result is not ConnectResult.OK:
                self._failures[view.device_id] = event.result
        elif isinstance(event, DeviceConnected):
            self._failures.pop(view.device_id, None)
        if isinstance(event, DeviceOffline) and not view.paired:
            self._remove(view.device_id)
            return
        self._upsert(view)

    def refresh(self) -> None:
        """Vẽ lại mọi hàng (đổi ngôn ngữ)."""
        if self._rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, 0))

    # -- Qt -------------------------------------------------------------------------------------

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008 - chữ ký của Qt
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        view = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return "\n".join(self._lines(view))
        if role == Qt.ItemDataRole.ToolTipRole:
            # Tooltip của Qt tự đoán rich text (AutoText): tên do máy kia đặt phải được thoát HTML.
            return "<qt>" + html.escape("\n".join(self._lines(view))).replace("\n", "<br>") + "</qt>"
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return ". ".join(self._lines(view))
        if role == DEVICE_ROLE:
            return view
        if role == DEVICE_ID_ROLE:
            return view.device_id
        if role == FAILURE_ROLE:
            return self.shown_failure(view)
        return None

    def flags(self, index: _Index) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # -- Nội bộ ---------------------------------------------------------------------------------

    def _lines(self, view: DeviceView) -> list[str]:
        """Các dòng chữ của một hàng: tên + (đã ghép/chưa ghép); hệ điều hành · trạng thái; lỗi."""
        lines = [f"{texts.peer_label(view.name, view.device_id)} {_paired_tag(view.paired)}"]
        parts = []
        os_label = _OS_LABELS.get(view.os or "")
        if os_label:
            parts.append(os_label)
        parts.append(_status_text(view))
        lines.append(" · ".join(parts))
        failure = self.shown_failure(view)
        if failure is not None:
            lines.append(connect_failed_text(view, failure))
        return lines

    def _remove(self, device_id: str) -> None:
        row = self.row_of(device_id)
        if row < 0:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._rows[row]
        self.endRemoveRows()

    def _upsert(self, view: DeviceView) -> None:
        row = self.row_of(view.device_id)
        if row >= 0 and _sort_key(self._rows[row]) == _sort_key(view):
            self._rows[row] = view
            index = self.index(row, 0)
            self.dataChanged.emit(index, index)
            return
        if row >= 0:
            self._remove(view.device_id)
        key = _sort_key(view)
        position = 0
        while position < len(self._rows) and _sort_key(self._rows[position]) < key:
            position += 1
        self.beginInsertRows(QModelIndex(), position, position)
        self._rows.insert(position, view)
        self.endInsertRows()


# ---- View ----------------------------------------------------------------------------------------


class _DeviceList(QListView):
    """`QListView` mà Tab đi qua từng hàng; menu ngữ cảnh báo qua `menu_requested`."""

    menu_requested = Signal(QModelIndex, QPoint)  # hàng (có thể không hợp lệ), vị trí toàn màn hình
    files_dropped = Signal(QModelIndex, list)  # hàng (có thể không hợp lệ), list[QUrl]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self._drop_index = QPersistentModelIndex()  # hàng đang được kéo file qua (tô viền)

    # Kéo thả tự xử lý (model không hỗ trợ mime): chỉ nhận URL file cục bộ, không chạm đĩa.

    def drop_target(self) -> str | None:
        """device_id của hàng đang được tô vì có file kéo qua; không có → None (test)."""
        if not self._drop_index.isValid():
            return None
        device_id = self._drop_index.data(DEVICE_ID_ROLE)
        return device_id if isinstance(device_id, str) else None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if _has_local_files(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if not _has_local_files(event):
            self._set_drop_index(QModelIndex())
            event.ignore()
            return
        index = self.indexAt(event.position().toPoint())
        if index.isValid():
            view = index.data(DEVICE_ROLE)
            if not isinstance(view, DeviceView) or not can_receive_files(view):
                # Thiết bị không gửi tới được (chưa ghép đôi, không trong mạng): không nhận thả ở đây.
                self._set_drop_index(QModelIndex())
                event.ignore(self.visualRect(index))
                return
        # Chỉ tô viền hàng dưới con trỏ, không đổi hàng đang chọn khi file chỉ được kéo qua.
        self._set_drop_index(index)
        event.acceptProposedAction()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_drop_index(QModelIndex())
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_drop_index(QModelIndex())
        urls = _event_urls(event)
        if not send.local_paths(urls):
            event.ignore()
            return
        index = self.indexAt(event.position().toPoint())
        event.acceptProposedAction()
        self.files_dropped.emit(QModelIndex(index), urls)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        model, drop = self.model(), self._drop_index
        if model is None or not drop.isValid():
            return
        rect = self.visualRect(model.index(drop.row(), drop.column()))
        if rect.isEmpty():
            return
        painter = QPainter(self.viewport())
        try:
            pen = QPen(self.palette().color(QPalette.ColorRole.Highlight))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(rect.adjusted(1, 1, -2, -2))
        finally:
            painter.end()

    def _set_drop_index(self, index: QModelIndex) -> None:
        if QPersistentModelIndex(index) == self._drop_index:
            return
        self._drop_index = QPersistentModelIndex(index)
        self.viewport().update()

    def focusNextPrevChild(self, next: bool) -> bool:
        model = self.model()
        current = self.currentIndex()
        # Qt gọi hàm này trên widget đang có tiêu điểm khi nhấn Tab (không dùng `hasFocus()`: cửa sổ
        # chưa được kích hoạt thì `hasFocus()` là False dù danh sách đang là widget tiêu điểm của nó).
        if self.window().focusWidget() is self and model is not None and current.isValid():
            target = current.row() + (1 if next else -1)
            if 0 <= target < model.rowCount():
                self.setCurrentIndex(model.index(target, 0))
                return True
        return super().focusNextPrevChild(next)

    def focusInEvent(self, event: QFocusEvent) -> None:
        super().focusInEvent(event)
        model = self.model()
        if model is None or model.rowCount() == 0:
            return
        if event.reason() == Qt.FocusReason.TabFocusReason:
            self.setCurrentIndex(model.index(0, 0))
        elif event.reason() == Qt.FocusReason.BacktabFocusReason:
            self.setCurrentIndex(model.index(model.rowCount() - 1, 0))
        elif not self.currentIndex().isValid():
            self.setCurrentIndex(model.index(0, 0))

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key, modifiers = event.key(), event.modifiers()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentIndex().isValid():
            self.activated.emit(self.currentIndex())
            event.accept()
            return
        # Phím Menu / Shift+F10 xử lý ngay ở đây (nhận rồi thì Qt không sinh thêm QContextMenuEvent),
        # để chạy giống nhau trên mọi hệ điều hành và cả khi phím được gửi thẳng tới widget.
        if key == Qt.Key.Key_Menu or (
            key == Qt.Key.Key_F10 and modifiers & Qt.KeyboardModifier.ShiftModifier
        ):
            self._request_keyboard_menu()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:
        if event.reason() == QContextMenuEvent.Reason.Keyboard:
            self._request_keyboard_menu()
        else:
            pos = event.pos()
            index = self.indexAt(pos)
            if index.isValid():
                self.setCurrentIndex(index)
            self.menu_requested.emit(QModelIndex(index), self.viewport().mapToGlobal(pos))
        event.accept()

    def _request_keyboard_menu(self) -> None:
        index = self.currentIndex()
        pos = self.visualRect(index).center() if index.isValid() else QPoint(0, 0)
        self.menu_requested.emit(QModelIndex(index), self.viewport().mapToGlobal(pos))


def _event_urls(event: QDragEnterEvent | QDragMoveEvent | QDropEvent) -> list[QUrl]:
    mime = event.mimeData()
    return list(mime.urls()) if mime is not None and mime.hasUrls() else []


def _has_local_files(event: QDragEnterEvent | QDragMoveEvent | QDropEvent) -> bool:
    return bool(send.local_paths(_event_urls(event)))


def _label(object_name: str, *, bold: bool = False, size_delta: int = 0) -> QLabel:
    label = QLabel()
    label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if bold or size_delta:
        font = QFont(label.font())
        font.setBold(bold)
        if size_delta:
            font.setPointSizeF(font.pointSizeF() + size_delta)
        label.setFont(font)
    return label


class RadarView(QWidget):
    """Radar: danh sách thiết bị, câu khi trống, nút và khung nhập IP thủ công."""

    pair_requested = Signal(object)  # DeviceView: chưa ghép đôi, đang trong mạng
    manual_address_entered = Signal(str, int)  # ip, cổng
    send_files_requested = Signal(str, list)  # device_id, list[Path] (kéo thả)
    pick_files_requested = Signal(str)  # device_id (menu “Gửi file…”)
    drop_hint_requested = Signal(str)  # câu gợi ý (thả vào chỗ không có thiết bị)
    unpair_requested = Signal(str)  # device_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("radar")
        self.model = DeviceListModel(self)
        self._menu: QMenu | None = None
        self._restore_id: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.heading_label = _label("radarHeading", bold=True, size_delta=1)
        self.hint_label = _label("radarHint")
        self.empty_label = _label("radarEmpty")
        self.empty_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByKeyboard | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.empty_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # trình đọc màn hình đọc được câu

        self.list_view = _DeviceList(self)
        self.list_view.setObjectName("deviceList")
        self.list_view.setModel(self.model)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_view.setWordWrap(True)
        self.list_view.setUniformItemSizes(False)
        self.list_view.setSpacing(4)
        self.list_view.setTabKeyNavigation(False)  # Tab do `_DeviceList.focusNextPrevChild` xử lý
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.list_view.activated.connect(self._on_activated)
        self.list_view.menu_requested.connect(self._on_menu_requested)
        self.list_view.files_dropped.connect(self._on_files_dropped)
        self.setAcceptDrops(True)  # thả vào câu “Chưa thấy thiết bị nào” hay chỗ trống khác → gợi ý

        self.address_button = QPushButton(self)
        self.address_button.setObjectName("addressButton")
        self.address_button.clicked.connect(self.open_address_entry)

        self.address_panel = QFrame(self)
        self.address_panel.setObjectName("addressPanel")
        self.address_panel.setFrameShape(QFrame.Shape.StyledPanel)
        panel = QVBoxLayout(self.address_panel)
        self.address_label = _label("addressLabel")
        self.address_edit = _AddressEdit(self.address_panel)
        self.address_edit.setObjectName("addressEdit")
        self.address_edit.setMaxLength(64)
        self.address_edit.setInputMethodHints(Qt.InputMethodHint.ImhPreferNumbers)
        self.address_label.setBuddy(self.address_edit)
        self.address_error = _label("addressError")
        self.address_error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.address_submit = QPushButton(self.address_panel)
        self.address_submit.setObjectName("addressSubmit")
        self.address_cancel = QPushButton(self.address_panel)
        self.address_cancel.setObjectName("addressCancel")
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.address_cancel)
        buttons.addWidget(self.address_submit)
        panel.addWidget(self.address_label)
        panel.addWidget(self.address_edit)
        panel.addWidget(self.address_error)
        panel.addLayout(buttons)
        self.address_panel.hide()
        self.address_error.hide()
        self.address_edit.textChanged.connect(self._on_address_changed)
        self.address_edit.returnPressed.connect(self.submit_address)
        self.address_edit.escape_pressed.connect(self.close_address_entry)
        self.address_submit.clicked.connect(self.submit_address)
        self.address_cancel.clicked.connect(self.close_address_entry)
        self.address_submit.setEnabled(False)

        layout.addWidget(self.heading_label)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.empty_label, 1, Qt.AlignmentFlag.AlignTop)  # chiếm chỗ của danh sách khi trống
        layout.addWidget(self.list_view, 1)
        address_row = QHBoxLayout()
        address_row.addWidget(self.address_button)
        address_row.addStretch(1)
        layout.addLayout(address_row)
        layout.addWidget(self.address_panel)

        QWidget.setTabOrder(self.empty_label, self.list_view)
        QWidget.setTabOrder(self.list_view, self.address_button)
        QWidget.setTabOrder(self.address_button, self.address_edit)
        QWidget.setTabOrder(self.address_edit, self.address_cancel)
        QWidget.setTabOrder(self.address_cancel, self.address_submit)

        self.model.modelAboutToBeReset.connect(self._remember_current)
        self.model.modelReset.connect(self._restore_current)
        self.model.modelReset.connect(self._update_empty)
        self.model.rowsInserted.connect(self._update_empty)
        self.model.rowsRemoved.connect(self._update_empty)

        self.retranslate()
        self._update_empty()

    # ---- API -------------------------------------------------------------------------------------

    def first_focus_widget(self) -> QWidget:
        """Widget đầu tiên của radar theo thứ tự Tab (để cửa sổ chính nối chuỗi Tab)."""
        return self.empty_label

    def last_focus_widget(self) -> QWidget:
        return self.address_submit

    def selected_device(self) -> DeviceView | None:
        index = self.list_view.currentIndex()
        if not index.isValid():
            return None
        view = index.data(DEVICE_ROLE)
        return view if isinstance(view, DeviceView) else None

    def select_device(self, device_id: str) -> bool:
        row = self.model.row_of(device_id)
        if row < 0:
            return False
        self.list_view.setCurrentIndex(self.model.index(row, 0))
        return True

    def context_menu_for(self, device_id: str) -> QMenu | None:
        """Menu của một thiết bị (None nếu không có thao tác nào). Menu có cha là radar; người gọi
        tự `deleteLater()` khi không dùng nữa."""
        view = self.model.device(device_id)
        if view is None:
            return None
        menu = QMenu(self)
        menu.setAccessibleName(
            texts.fill(
                QCoreApplication.translate("Radar", "Thao tác với {name}"),
                name=texts.peer_label(view.name, view.device_id),
            )
        )
        if can_receive_files(view):
            action = QAction(QCoreApplication.translate("Radar", "Gửi file…"), menu)
            action.triggered.connect(lambda _=False, i=view.device_id: self._request_pick(i))
            menu.addAction(action)
        if not view.paired and view.online:
            action = QAction(QCoreApplication.translate("Radar", "Ghép đôi…"), menu)
            action.triggered.connect(lambda _=False, i=view.device_id: self._request_pair(i))
            menu.addAction(action)
        if view.paired:
            action = QAction(QCoreApplication.translate("Radar", "Hủy ghép đôi…"), menu)
            action.triggered.connect(lambda _=False, i=view.device_id: self._request_unpair(i))
            menu.addAction(action)
        if not menu.actions():
            menu.deleteLater()
            return None
        return menu

    @property
    def last_menu(self) -> QMenu | None:
        """Menu ngữ cảnh mở gần nhất (test, trợ năng)."""
        return self._menu

    def open_address_entry(self) -> None:
        """Mở khung nhập IP và đặt tiêu điểm vào ô nhập."""
        self.address_panel.show()
        self.address_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.address_edit.selectAll()

    def close_address_entry(self) -> None:
        self.address_edit.clear()
        self._set_address_error("")
        self.address_panel.hide()
        self.address_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def submit_address(self) -> bool:
        """Đọc ô nhập; đúng → phát `manual_address_entered`, đóng khung, trả True; sai → câu lỗi, False."""
        text = self.address_edit.text()
        if not text.strip():
            return False
        try:
            ip, port = parse_manual_address(text)
        except ValueError:
            self._set_address_error(invalid_address_text(text))
            self.address_edit.setFocus(Qt.FocusReason.OtherFocusReason)
            self.address_edit.selectAll()
            return False
        self.close_address_entry()
        self.manual_address_entered.emit(ip, port)
        return True

    def drop_files(self, device_id: str | None, urls: list[QUrl]) -> bool:
        """Thả `urls` lên thiết bị `device_id` (None: chỗ trống); True nếu đã phát `send_files_requested`.

        Chỉ lấy URL file cục bộ (`send.local_paths`, không chạm đĩa); không có cái nào → bỏ qua.
        Chỗ trống hay thiết bị không còn trên radar → `drop_hint_requested`. Thiết bị chưa ghép đôi
        mà không trong mạng → bỏ qua.
        """
        paths = send.local_paths(urls)
        if not paths:
            return False
        view = None if device_id is None else self.model.device(device_id)
        if view is None:
            self.drop_hint_requested.emit(drop_hint_text())
            return False
        if not can_receive_files(view):
            return False
        self.select_device(view.device_id)
        self.send_files_requested.emit(view.device_id, paths)
        return True

    # ---- Kéo thả vào phần còn lại của radar --------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if _has_local_files(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if _has_local_files(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        urls = _event_urls(event)
        if not send.local_paths(urls):
            event.ignore()
            return
        event.acceptProposedAction()
        self.drop_files(None, urls)

    # ---- Chữ -------------------------------------------------------------------------------------

    def retranslate(self) -> None:
        t = QCoreApplication.translate
        heading = t("Radar", "Thiết bị trong mạng")
        self.heading_label.setText(heading)
        self.heading_label.setAccessibleName(heading)
        hint = t("Radar", "Kéo file thả vào một thiết bị")
        self.hint_label.setText(hint)
        self.hint_label.setAccessibleName(hint)
        self.empty_label.setText(no_devices_text())
        self.empty_label.setAccessibleName(heading)
        self.empty_label.setAccessibleDescription(no_devices_text())
        self.list_view.setAccessibleName(heading)
        self.list_view.setAccessibleDescription(
            t("Radar", "Tab hoặc phím mũi tên để chọn thiết bị, phím Menu để mở các thao tác.")
        )
        manual = t("Radar", "Nhập IP thủ công")
        self.address_button.setText(manual)
        self.address_button.setAccessibleName(manual)
        self.address_panel.setAccessibleName(manual)
        label = t("Radar", "Địa chỉ IP của máy kia (ví dụ 192.168.1.5 hoặc 192.168.1.5:47474):")
        self.address_label.setText(label)
        self.address_edit.setAccessibleName(t("Radar", "Địa chỉ IP của máy kia"))
        self.address_edit.setPlaceholderText("192.168.1.5")
        submit = t("Radar", "Ghép đôi")
        self.address_submit.setText(submit)
        self.address_submit.setAccessibleName(submit)
        cancel = t("Radar", "Hủy")
        self.address_cancel.setText(cancel)
        self.address_cancel.setAccessibleName(cancel)
        self.address_error.setAccessibleName(t("Radar", "Lỗi"))
        if self.address_error.isVisibleTo(self) and self.address_edit.text().strip():
            self._set_address_error(invalid_address_text(self.address_edit.text()))
        self.model.refresh()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()

    # ---- Nội bộ ----------------------------------------------------------------------------------

    def _update_empty(self) -> None:
        empty = self.model.rowCount() == 0
        self.empty_label.setVisible(empty)
        self.list_view.setVisible(not empty)
        selection = self.list_view.selectionModel()
        if not empty and selection is not None and not self.list_view.currentIndex().isValid():
            # Luôn có hàng hiện hành để Tab / phím Menu dùng được ngay (không chọn sẵn hàng nào).
            selection.setCurrentIndex(self.model.index(0, 0), QItemSelectionModel.SelectionFlag.NoUpdate)

    def _remember_current(self) -> None:
        view = self.selected_device()
        self._restore_id = None if view is None else view.device_id

    def _restore_current(self) -> None:
        device_id, self._restore_id = self._restore_id, None
        if device_id is not None:
            self.select_device(device_id)

    def _on_activated(self, index: QModelIndex) -> None:
        device_id = index.data(DEVICE_ID_ROLE)
        if isinstance(device_id, str):
            view = self.model.device(device_id)
            if view is not None and not view.paired:
                self._request_pair(device_id)

    def _on_menu_requested(self, index: QModelIndex, global_pos: QPoint) -> None:
        device_id = index.data(DEVICE_ID_ROLE) if index.isValid() else None
        if not isinstance(device_id, str):
            return
        menu = self.context_menu_for(device_id)
        if menu is None:
            return
        old, self._menu = self._menu, menu
        if old is not None:
            old.close()
            old.deleteLater()
        menu.popup(global_pos)

    def _request_pair(self, device_id: str) -> None:
        """Phát `pair_requested` với view mới nhất, chỉ khi chưa ghép đôi và đang trong mạng."""
        view = self.model.device(device_id)
        if view is not None and not view.paired and view.online:
            self.pair_requested.emit(view)

    def _on_files_dropped(self, index: QModelIndex, urls: list[QUrl]) -> None:
        device_id = index.data(DEVICE_ID_ROLE) if index.isValid() else None
        self.drop_files(device_id if isinstance(device_id, str) else None, urls)

    def _request_pick(self, device_id: str) -> None:
        """Phát `pick_files_requested` nếu thiết bị vẫn còn trên radar và gửi tới được."""
        view = self.model.device(device_id)
        if view is not None and can_receive_files(view):
            self.pick_files_requested.emit(device_id)

    def _request_unpair(self, device_id: str) -> None:
        view = self.model.device(device_id)
        if view is not None and view.paired:
            self.unpair_requested.emit(device_id)

    def _on_address_changed(self, text: str) -> None:
        self.address_submit.setEnabled(bool(text.strip()))
        if self.address_error.isVisibleTo(self):
            self._set_address_error("")

    def _set_address_error(self, text: str) -> None:
        self.address_error.setText(text)
        self.address_error.setAccessibleDescription(text)
        self.address_error.setVisible(bool(text))
        self.address_edit.setAccessibleDescription(text)


class _AddressEdit(QLineEdit):
    """Ô nhập IP: Esc phát `escape_pressed` (đóng khung), không lan ra cửa sổ."""

    escape_pressed = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.escape_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)
