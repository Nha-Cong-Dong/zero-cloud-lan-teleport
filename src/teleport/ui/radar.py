"""Model và chữ dùng chung của các thiết bị (mvp F2): `DeviceListModel` và câu mvp mục 8.

Module chỉ còn model và chữ (spec B2 mục 2.1): danh sách cuộc trò chuyện ở cột trái
(`ui.conversations`) và trang “Tìm máy mới” (`ui.discover`) dùng chung một `DeviceListModel`
(`MainWindow.devices`). Ngữ cảnh dịch giữ là `"Radar"`.

**Model** (`DeviceListModel`): mỗi `device_id` một hàng, giữ `DeviceView` mới nhất, sắp theo
`_sort_key` = (chưa kết nối, chưa ghép đôi, NFC(tên).casefold(), device_id): máy đang kết nối lên
đầu, rồi máy đã ghép đôi, rồi theo tên. Kết nối / ghép đôi / tên đổi thì hàng dời chỗ
(`beginMoveRows`); nạp lại cả danh sách cũng sắp theo khóa đó. Nguồn dữ liệu: ảnh chụp
`Node.devices()` lúc node `started` (`reset`), rồi từng `NodeEvent` (`apply`): `DeviceOnline`,
`DeviceConnected`, `DeviceDisconnected`, `ConnectFailed` cập nhật hay thêm hàng; `DeviceOffline` bỏ
hàng của thiết bị chưa ghép đôi, còn thiết bị đã ghép đôi thì giữ hàng (không thấy trong mạng). Host
mới (“Thử lại”) → `clear()`. Role: `DEVICE_ROLE`, `DEVICE_ID_ROLE`, `FAILURE_ROLE`; chữ của hàng
(`DisplayRole`, `ToolTipRole`, `AccessibleTextRole`) là tên + “(đã ghép)/(chưa ghép)”, hệ điều hành ·
trạng thái, câu lỗi kết nối.

**Lỗi kết nối gần nhất** (spec hackathon mục 9.5): model nhớ `ConnectFailed.result` gần nhất của
mỗi thiết bị, xóa khi có `DeviceConnected`; `reset` (nạp lại `devices()`) không xóa. Câu hiện là câu
mvp mục 8 (`connect_failed_text`); `shown_failure`: câu “Thấy … nhưng không kết nối được” chỉ khi
thiết bị còn trong mạng, các lỗi khác tới khi kết nối lại được. Thiết bị không còn ghép đôi thì không
có lỗi kết nối.

**Chữ của máy khác**: tên thiết bị đi qua `texts.peer_label` (`core.display.display_name`, ký tự
điều khiển thành U+FFFD, ≤ 32 cột); chữ thuần, không HTML (tooltip đã thoát HTML). Tên hệ điều hành
chỉ hiện khi là một trong các giá trị đã biết (`windows`, `macos`, `linux`).
"""

from __future__ import annotations

import html
import unicodedata
from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import QAbstractListModel, QCoreApplication, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QStyle

from teleport.app.devices import DeviceView
from teleport.app.node import (
    ConnectFailed,
    DeviceConnected,
    DeviceDisconnected,
    DeviceOffline,
    DeviceOnline,
    NodeEvent,
)
from teleport.core.links import ConnectResult
from teleport.ui import texts

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
        "Chưa thấy thiết bị nào. Hãy mở Teleport trên máy kia, kiểm tra hai máy dùng chung một mạng "
        "Wi-Fi và tường lửa của Windows cho phép Teleport.",
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
            "chuyện với nhau (hay gặp ở quán cà phê, khách sạn). Hãy thử dùng điểm phát Wi-Fi từ điện thoại.",
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


def _sort_key(view: DeviceView) -> tuple[bool, bool, str, str]:
    """Đang kết nối lên đầu, rồi đã ghép đôi, rồi theo tên (NFC, casefold), cuối cùng mã máy."""
    return (
        not view.connected,
        not view.paired,
        unicodedata.normalize("NFC", view.name).casefold(),
        view.device_id,
    )


class DeviceListModel(QAbstractListModel):
    """Các thiết bị (trong mạng hay đã ghép đôi), mỗi `device_id` một hàng (chỉ dùng trên thread Qt)."""

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
        if role == Qt.ItemDataRole.DecorationRole:
            # Biểu tượng của style (không tải gì); đọc lúc vẽ để theo style hiện tại.
            return QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        if role == Qt.ItemDataRole.FontRole:
            # Máy đã ghép đôi: chữ nửa đậm (nhấn mạnh; chữ “(đã ghép)” vẫn nói rõ). FontRole áp cho cả
            # hàng 2–3 dòng, nên DemiBold (`bold()` vẫn True) đỡ nặng hơn Bold. None: font của view.
            if not view.paired:
                return None
            font = QFont(QApplication.font())
            font.setWeight(QFont.Weight.DemiBold)
            return font
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
        key = _sort_key(view)
        others = self._rows if row < 0 else self._rows[:row] + self._rows[row + 1 :]
        position = 0
        while position < len(others) and _sort_key(others[position]) < key:
            position += 1
        if row < 0:
            self.beginInsertRows(QModelIndex(), position, position)
            self._rows.insert(position, view)
            self.endInsertRows()
            return
        if position != row:
            # Đổi tên làm đổi thứ tự: dời hàng (không bỏ rồi chèn) để view giữ lựa chọn, hàng hiện hành.
            destination = position if position < row else position + 1
            self.beginMoveRows(QModelIndex(), row, row, QModelIndex(), destination)
            del self._rows[row]
            self._rows.insert(position, view)
            self.endMoveRows()
        self._rows[position] = view
        index = self.index(position, 0)
        self.dataChanged.emit(index, index)
