"""Chẩn đoán kết nối ở đầu luồng chat của máy đã ghép đôi (spec F3b; mvp F2, mục 8, N12).

Khi máy đang mở trong `ui.chat.ChatView` đã ghép đôi mà ngoại tuyến hay không kết nối được, thanh
`DiagnoseBar` (objectName `chatDiagnoseBar`, giữa `header` và `threads`, đặt bằng
`ChatView.set_device_banner`) nói đúng nguyên nhân và cho sửa ngay: [Kết nối bằng IP…] mở
`ConnectAddressDialog`, [Thử lại] gọi `Node.retry_connect`. Thanh này chỉ nói về máy đang mở; thanh
thông báo chung `ChatView.info_bar` giữ vai cũ (hai thanh có thể cùng nói “bị chặn”, vai khác nhau).

**Chọn nguyên nhân** (`diagnose`, hàm thuần): từ `DeviceView` của máy đang mở, lỗi kết nối gần nhất
`(ConnectResult, from_hint)` và `discovery_active`, xét lần lượt: chưa ghép đôi hay đã kết nối → NONE;
bị chặn (REJECTED, VERSION_MISMATCH; IDENTITY_MISMATCH không `from_hint`) → BLOCKED; IDENTITY_MISMATCH
`from_hint` (địa chỉ vừa nhập là của máy khác) → WRONG_ADDRESS (không [Thử lại]: tracker vẫn giữ
địa chỉ sai); trong mạng mà UNREACHABLE → ISOLATED; trong mạng → NONE; mDNS không chạy → MDNS_DOWN;
có địa chỉ đã lưu → RECONNECTING; còn lại NOT_SEEN. BLOCKED, WRONG_ADDRESS, ISOLATED hiện ngay
(`urgent`); ba nguyên nhân ngoại tuyến chỉ hiện sau `SHOW_DELAY_MS` kể từ lúc máy **chuyển** sang
ngoại tuyến (không chớp lúc mở app hay mDNS khởi động lại).

**Nguồn tín hiệu** (`DiagnoseController`, mọi thứ trên luồng Qt): model thiết bị dùng chung
(`radar.DeviceListModel`; mọi signal của model → `_reevaluate`: cập nhật mốc ngoại tuyến, bật/tắt
poll, vẽ lại); `ConnectFailed` theo luật spec 2.3 (`from_hint` + UNREACHABLE bị bỏ: chỉ nói về địa
chỉ vừa nhập, hộp đã báo, và có thể tới muộn; bỏ thì không xóa lỗi đang nhớ); `DeviceConnected` xóa
lỗi, kết quả [Thử lại] và ✕ của máy đó. mDNS không có `NodeEvent`, nên poll `node.discovery_active`
bằng `host.call` mỗi `DISCOVERY_POLL_MS` khi máy đang mở ngoại tuyến (nhịp bỏ qua khi `ChatView`
không hiện; `Show` của `ChatView`, `on_started` và đổi máy đọc ngay), một `call` một lúc; lỗi giữ
giá trị cũ (DEBUG, tên loại lỗi); `None` (chưa đọc) coi như đang chạy. Máy thôi ghép đôi hay rời
model → quên mọi trạng thái của nó.

**[Thử lại]** → `host.submit(node.retry_connect(id))`; bấm thì chụp lỗi đang nhớ (`_before_retry`) rồi
xóa nó; trong lúc chờ nguyên nhân tính theo `ConnectFailed` mới nếu có, không thì theo lỗi đã chụp
(spec H17), thanh hiện INFO “Đang kết nối tới X…”, hai nút ẩn, vẫn có ✕. Kết quả: OK → bỏ kết quả
cũ (`DeviceConnected` tới trước); UNREACHABLE → “Vẫn chưa kết nối được…” + thân của nguyên nhân (core
chỉ báo UNREACHABLE sau vài lần lỗi liền và `retry_now` đặt lại bộ đếm, nên chưa có sự kiện mới mà
máy vẫn trong mạng thì tự ghi lỗi ISOLATED); IDENTITY_MISMATCH (không bị chặn) → “Vẫn chưa…” +
`saved_changed_body`; REJECTED / VERSION_MISMATCH → `ConnectFailed` đi kèm đưa về BLOCKED; None (không
có đích để thử) → câu của nguyên nhân, ẩn [Thử lại] tới khi nguyên nhân đổi; KeyError → quên máy;
RuntimeError / CancelledError im lặng; lỗi khác WARNING (tên loại lỗi). Kết quả cũ (`_tried`) bỏ khi
nguyên nhân hay loại lỗi đổi, kết nối được, bấm lại, bấm ✕, `attach`.

**Hộp “Kết nối bằng IP…”** (`ConnectAddressDialog`, objectName `connectAddressDialog`): cha là cửa sổ
chính, `WindowModal`, `open()` (không `exec()`), tự xóa khi đóng; một hộp một lúc, bấm lại chỉ đưa
lên trước; hộp đang mở cho máy đang xem thì thanh ẩn [Thử lại]. Ô `connectAddressEdit` điền sẵn địa
chỉ đã lưu (`format_address`, chọn hết), không có thì trống. Bấm “Kết nối” (mặc định, Enter):
`app.devices.parse_manual_address(text, cổng mặc định)` với cổng mặc định là cổng đã lưu, rồi cổng
mDNS, cuối cùng `DEFAULT_PORT`; sai → câu mvp mục 8 ở nhãn `connectAddressError` (Alert), không gọi
mạng; đúng → `host.submit(node.connect_address(id, ip, port))`, ô và nút tắt, “Đang kết nối tới
ip:cổng…”. Hộp chỉ lấy kết quả từ giá trị trả về, không bao giờ nghe `ConnectFailed`: OK → đóng (F3a
tự lưu địa chỉ); UNREACHABLE, IDENTITY_MISMATCH, REJECTED, VERSION_MISMATCH, lỗi khác → câu ở nhãn lỗi
và bật lại ô; KeyError → đóng. Hủy/Esc (kể cả lúc chờ) → `job.cancel()` rồi đóng. `attach`,
`close_all` → hủy việc, đóng hộp.

**Vẽ**: khóa hiển thị `(device_id, cause, result, overlay, name, last_address)`; chỉ gọi
`show_message` khi khóa đổi (không lặp Alert sau mỗi nhịp poll). ✕ nhớ khóa đang hiện; khóa đổi thì
hiện lại, kết nối được thì quên. Chữ truyền dạng hàm (`functools.partial`) để tự dịch lại khi đổi
ngôn ngữ. Tên máy kia qua `texts.peer_label`, mọi nhãn là chữ thuần.

**Trợ năng** (N12): [Kết nối bằng IP…] là `action_button`, [Thử lại] là `secondary_button`; tên trợ
năng là chữ nút, mô tả là tiêu đề thanh. Chuỗi Tab (`focus_chain`): tiêu đề, thân, hai nút, ✕; nút
ẩn tự rời chuỗi. Nút đang giữ tiêu điểm bị ẩn thì tiêu điểm về tiêu đề thanh. Màu theo `InfoLevel`,
nguyên nhân luôn nói bằng chữ.

Chạm `Node` chỉ qua `host.call` / `host.submit`; mọi `on_done` kiểm host còn là host hiện tại và
chưa `close_all`. Câu chữ ở ngữ cảnh dịch `"Diagnose"`, gọi lúc dùng.

Seam cho test (biến cấp module, đọc lúc dùng): `SHOW_DELAY_MS`, `DISCOVERY_POLL_MS`, `_monotonic`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAccessible, QAccessibleEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from teleport.app.devices import DeviceView, parse_manual_address
from teleport.app.node import ConnectFailed, DeviceConnected, NodeEvent
from teleport.core.links import ConnectResult
from teleport.net.connection import DEFAULT_PORT
from teleport.ui import pairing, radar, texts, theme
from teleport.ui.widgets import InfoBar, InfoLevel

if TYPE_CHECKING:
    from teleport.app.node import Node
    from teleport.ui.bridge import NodeHost, SubmitJob
    from teleport.ui.chat import ChatView
    from teleport.ui.radar import DeviceListModel

log = logging.getLogger(__name__)

SHOW_DELAY_MS = 4000  # > mvp N1 3 s: không chớp lúc mở app hay lúc mDNS khởi động lại
DISCOVERY_POLL_MS = 5000  # bằng NETWORK_CHECK_INTERVAL của node
# Seam: đồng hồ monotonic (giây) cho mốc ngoại tuyến; đọc lúc gọi.
_monotonic: Callable[[], float] = time.monotonic

_ADDRESS_MAX_LENGTH = 64  # như ô “Nhập IP…” của trang Tìm máy mới
_ADDRESS_PLACEHOLDER = "192.168.1.5"  # ví dụ địa chỉ, không dịch


# ---- Câu chữ -------------------------------------------------------------------------------------


def _peer(view: DeviceView) -> str:
    return texts.peer_label(view.name, view.device_id)


def not_seen_title(name: str) -> str:
    """Tiêu đề NOT_SEEN, RECONNECTING. `name` đã qua `peer_label`."""
    return texts.fill(QCoreApplication.translate("Diagnose", "Không thấy {name} trong mạng"), name=name)


def not_seen_body() -> str:
    return QCoreApplication.translate(
        "Diagnose",
        "Hãy mở Teleport trên máy đó và kiểm tra hai máy dùng chung một mạng. Nếu mạng chặn việc tìm "
        "thiết bị, hãy kết nối bằng địa chỉ IP của máy đó.",
    )


def reconnecting_body(address: str) -> str:
    """Thân RECONNECTING; `address` theo `format_address`."""
    return texts.fill(
        QCoreApplication.translate(
            "Diagnose",
            "Teleport vẫn đang tự thử kết nối lại tới địa chỉ lần trước ({address}). Nếu máy đó đã đổi "
            "mạng hay đổi địa chỉ, hãy kết nối bằng địa chỉ IP mới.",
        ),
        address=address,
    )


def mdns_down_title() -> str:
    return QCoreApplication.translate("Diagnose", "Máy này chưa tìm được thiết bị nào trong mạng")


def mdns_down_body(name: str) -> str:
    return texts.fill(
        QCoreApplication.translate(
            "Diagnose",
            "Tính năng tìm thiết bị của Teleport trên máy này đang không chạy (có thể mạng vừa đổi, hay "
            "VPN hoặc phần mềm bảo mật đang chặn). Teleport sẽ tự bật lại. Trong lúc chờ, hãy kết nối "
            "tới {name} bằng địa chỉ IP.",
        ),
        name=name,
    )


def wrong_address_title(name: str) -> str:
    return texts.fill(
        QCoreApplication.translate("Diagnose", "Địa chỉ IP vừa nhập là của một máy khác, không phải {name}"),
        name=name,
    )


def wrong_address_body(name: str) -> str:
    return texts.fill(
        QCoreApplication.translate(
            "Diagnose",
            "Hãy xem lại địa chỉ ở dòng “Địa chỉ” trong cửa sổ Teleport trên {name}, rồi kết nối lại.",
        ),
        name=name,
    )


def connecting_title(name: str) -> str:
    """Thanh trong lúc chờ [Thử lại]."""
    return texts.fill(QCoreApplication.translate("Diagnose", "Đang kết nối tới {name}…"), name=name)


def still_failed_title(name: str) -> str:
    """Thanh sau [Thử lại] không kết nối được."""
    return texts.fill(QCoreApplication.translate("Diagnose", "Vẫn chưa kết nối được với {name}"), name=name)


def saved_changed_body(address: str, name: str) -> str:
    """Thân sau [Thử lại] trả IDENTITY_MISMATCH: địa chỉ đã lưu giờ là của máy khác."""
    return texts.fill(
        QCoreApplication.translate(
            "Diagnose",
            "Địa chỉ lần trước ({address}) giờ là của một máy khác. Hãy kết nối bằng địa chỉ IP mới "
            "của {name}.",
        ),
        address=address,
        name=name,
    )


def connect_by_ip() -> str:
    """Nút mở hộp “Kết nối bằng IP”."""
    return QCoreApplication.translate("Diagnose", "Kết nối bằng IP…")


def dialog_title() -> str:
    return QCoreApplication.translate("Diagnose", "Kết nối bằng IP")


def dialog_label(name: str) -> str:
    return texts.fill(QCoreApplication.translate("Diagnose", "Địa chỉ IP của {name}"), name=name)


def dialog_help() -> str:
    return QCoreApplication.translate(
        "Diagnose",
        "Xem ở dòng “Địa chỉ” trong cửa sổ Teleport trên máy đó, ví dụ 192.168.1.5 hoặc 192.168.1.5:47474.",
    )


def dialog_connect() -> str:
    return QCoreApplication.translate("Diagnose", "Kết nối")


def dialog_cancel() -> str:
    return QCoreApplication.translate("Diagnose", "Hủy")


def dialog_connecting(address: str) -> str:
    """Trạng thái hộp khi đang thử `address` (“ip:cổng”)."""
    return texts.fill(QCoreApplication.translate("Diagnose", "Đang kết nối tới {address}…"), address=address)


def not_this_device_text(address: str, name: str) -> str:
    """Hộp: máy ở `address` có danh tính khác `name` (IDENTITY_MISMATCH của `connect_address`)."""
    return texts.fill(
        QCoreApplication.translate(
            "Diagnose",
            "Máy ở {address} không phải {name}. Có thể đó là một máy khác đang chạy Teleport; hãy kiểm "
            "tra lại địa chỉ.",
        ),
        address=address,
        name=name,
    )


def connect_error_text() -> str:
    """Hộp: lỗi không mong đợi (node đang đóng, lỗi lạ)."""
    return QCoreApplication.translate(
        "Diagnose", "Không kết nối được vì một lỗi không mong đợi. Hãy thử lại."
    )


def format_address(ip: str, port: int) -> str:
    """`ip` khi `port` là `DEFAULT_PORT`, không thì `ip:port`."""
    return ip if port == DEFAULT_PORT else f"{ip}:{port}"


# ---- Nguyên nhân ---------------------------------------------------------------------------------


class Cause(StrEnum):
    NONE = "none"
    BLOCKED = "blocked"
    WRONG_ADDRESS = "wrong_address"
    ISOLATED = "isolated"
    MDNS_DOWN = "mdns_down"
    RECONNECTING = "reconnecting"
    NOT_SEEN = "not_seen"


class Overlay(StrEnum):
    """Trạng thái của [Thử lại] đè lên câu của nguyên nhân."""

    NONE = "none"
    BUSY = "busy"  # đang chờ `retry_connect`
    TRIED_UNREACHABLE = "tried_unreachable"
    TRIED_IDENTITY = "tried_identity"
    NO_TARGET = "no_target"  # `retry_connect` trả None: không có đích để thử


@dataclass(frozen=True, slots=True)
class Diagnosis:
    cause: Cause
    result: ConnectResult | None  # BLOCKED: loại lỗi; còn lại None
    level: InfoLevel
    urgent: bool  # True: hiện ngay; False: chờ SHOW_DELAY_MS kể từ lúc ngoại tuyến
    can_retry: bool
    can_connect_ip: bool


_NONE = Diagnosis(Cause.NONE, None, InfoLevel.INFO, False, False, False)
_BLOCKING = (ConnectResult.REJECTED, ConnectResult.IDENTITY_MISMATCH, ConnectResult.VERSION_MISMATCH)
_HINT_BLOCKING = (ConnectResult.REJECTED, ConnectResult.VERSION_MISMATCH)


def diagnose(
    view: DeviceView | None,
    failure: tuple[ConnectResult, bool] | None,
    discovery_active: bool | None,
) -> Diagnosis:
    """Nguyên nhân để hiện cho `view` (xem docstring module; bảng ở spec F3b mục 2.4)."""
    if view is None or not view.paired or view.connected:
        return _NONE
    if failure is not None:
        result, from_hint = failure
        if (not from_hint and result in _BLOCKING) or (from_hint and result in _HINT_BLOCKING):
            return Diagnosis(Cause.BLOCKED, result, InfoLevel.ERROR, True, True, True)
        if from_hint and result is ConnectResult.IDENTITY_MISMATCH:
            return Diagnosis(Cause.WRONG_ADDRESS, None, InfoLevel.WARNING, True, False, True)
        if view.online and not from_hint and result is ConnectResult.UNREACHABLE:
            return Diagnosis(Cause.ISOLATED, None, InfoLevel.WARNING, True, True, True)
    if view.online:
        return _NONE
    if discovery_active is False:
        return Diagnosis(Cause.MDNS_DOWN, None, InfoLevel.WARNING, False, view.last_address is not None, True)
    if view.last_address is not None:
        return Diagnosis(Cause.RECONNECTING, None, InfoLevel.INFO, False, True, True)
    return Diagnosis(Cause.NOT_SEEN, None, InfoLevel.INFO, False, False, True)


def _cause_texts(diagnosis: Diagnosis, view: DeviceView) -> tuple[texts.Text, texts.Text]:
    """(tiêu đề, thân) của nguyên nhân, dạng hàm; thân "" khi nguyên nhân chỉ có tiêu đề."""
    name = _peer(view)
    cause = diagnosis.cause
    if cause is Cause.BLOCKED and diagnosis.result is not None:
        return functools.partial(radar.connect_failed_text, view, diagnosis.result), ""
    if cause is Cause.WRONG_ADDRESS:
        return functools.partial(wrong_address_title, name), functools.partial(wrong_address_body, name)
    if cause is Cause.ISOLATED:
        return functools.partial(radar.connect_failed_text, view, ConnectResult.UNREACHABLE), ""
    if cause is Cause.MDNS_DOWN:
        return mdns_down_title, functools.partial(mdns_down_body, name)
    if cause is Cause.RECONNECTING and view.last_address is not None:
        address = format_address(*view.last_address)
        return functools.partial(not_seen_title, name), functools.partial(reconnecting_body, address)
    return functools.partial(not_seen_title, name), not_seen_body


# ---- Thanh ---------------------------------------------------------------------------------------


class DiagnoseBar(InfoBar):
    """`InfoBar` thêm nút thứ hai `secondary_button` (cùng hàng với `action_button`)."""

    secondary_triggered = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatDiagnoseBar")
        self._secondary: texts.Text | None = None
        self.action_button.setObjectName("chatDiagnoseConnect")
        self.secondary_button = QPushButton(self)
        self.secondary_button.setObjectName("chatDiagnoseRetry")
        self.secondary_button.clicked.connect(self.secondary_triggered)
        self.secondary_button.setVisible(False)
        grid = self.layout()
        if isinstance(grid, QGridLayout):
            grid.removeWidget(self.action_button)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(theme.SPACING[1])
            row.addWidget(self.action_button)
            row.addWidget(self.secondary_button)
            row.addStretch(1)
            grid.addLayout(row, 1, 1)
        for first, second in itertools.pairwise(self.focus_chain()):
            QWidget.setTabOrder(first, second)

    def focus_chain(self) -> list[QWidget]:
        """Các ô nhận Tab của thanh theo thứ tự: tiêu đề, thân, hai nút, ✕."""
        return [
            self.title_label,
            self.text_label,
            self.action_button,
            self.secondary_button,
            self.close_button,
        ]

    def show_message(
        self,
        level: InfoLevel,
        title: texts.Text,
        text: texts.Text = "",
        *,
        action: texts.Text | None = None,
        secondary: texts.Text | None = None,
        closable: bool = True,
    ) -> None:
        """Như `InfoBar.show_message`, thêm `secondary` là chữ nút thứ hai (None: không có nút)."""
        super().show_message(level, title, text, action=action, closable=closable)
        self._secondary = secondary
        self.secondary_button.setVisible(secondary is not None)
        self._apply_buttons()

    def _apply_buttons(self) -> None:
        secondary = self._secondary
        self.secondary_button.setText(texts.resolve(secondary) if secondary is not None else "")
        for button in (self.action_button, self.secondary_button):
            button.setAccessibleName(button.text())
            button.setAccessibleDescription(self.title)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange and not self.isHidden():
            self._apply_buttons()


# ---- Hộp “Kết nối bằng IP” -----------------------------------------------------------------------


def _plain_label(object_name: str, parent: QWidget) -> QLabel:
    label = QLabel(parent)
    label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class ConnectAddressDialog(QDialog):
    """Hộp nhập địa chỉ IP cho một máy đã ghép đôi; chỉ là phần nhìn, việc mạng do controller làm."""

    submitted = Signal(str)  # chữ trong ô, khi bấm “Kết nối” (không lúc đang chờ)

    def __init__(self, view: DeviceView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("connectAddressDialog")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._view = view
        self._busy_address: str | None = None
        self._error: texts.Text | None = None

        self.label = _plain_label("connectAddressLabel", self)
        self.edit = QLineEdit(self)
        self.edit.setObjectName("connectAddressEdit")
        self.edit.setMaxLength(_ADDRESS_MAX_LENGTH)
        self.edit.setInputMethodHints(Qt.InputMethodHint.ImhPreferNumbers)
        self.edit.setPlaceholderText(_ADDRESS_PLACEHOLDER)
        self.label.setBuddy(self.edit)
        self.help_label = _plain_label("connectAddressHelp", self)
        self.help_label.setProperty(theme.TONE_PROPERTY, "muted")
        # Một dòng trạng thái cho cả “Đang kết nối tới …” (màu dịu) và câu lỗi (màu nguy hiểm, Alert).
        self.error_label = _plain_label("connectAddressError", self)
        self.error_label.setVisible(False)
        self.cancel_button = QPushButton(self)
        self.cancel_button.setObjectName("connectAddressCancel")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        self.connect_button = QPushButton(self)
        self.connect_button.setObjectName("connectAddressConnect")
        self.connect_button.setProperty(theme.PRIMARY_PROPERTY, True)
        self.connect_button.setDefault(True)
        self.connect_button.clicked.connect(self._on_connect)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.connect_button)
        layout = QVBoxLayout(self)
        layout.setSpacing(theme.SPACING[1])
        layout.addWidget(self.label)
        layout.addWidget(self.edit)
        layout.addWidget(self.help_label)
        layout.addWidget(self.error_label)
        layout.addSpacing(theme.SPACING[2])
        layout.addLayout(buttons)
        for first, second in ((self.edit, self.cancel_button), (self.cancel_button, self.connect_button)):
            QWidget.setTabOrder(first, second)
        self.setMinimumWidth(theme.DIALOG_MIN_WIDTH)

        if view.last_address is not None:
            self.edit.setText(format_address(*view.last_address))
            self.edit.selectAll()
        self.edit.textChanged.connect(self._on_text_changed)
        self.edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.retranslate()
        self._update_enabled()

    @property
    def device_id(self) -> str:
        return self._view.device_id

    @property
    def view(self) -> DeviceView:
        """`DeviceView` lúc mở hộp (tên cho câu chữ)."""
        return self._view

    @property
    def busy(self) -> bool:
        return self._busy_address is not None

    def error_text(self) -> str:
        """Câu lỗi đang hiện ("" khi không có)."""
        return self.error_label.text() if self._error is not None else ""

    def set_busy(self, address: str | None) -> None:
        """Đang thử `address` (“ip:cổng”): ô và nút “Kết nối” tắt, dòng trạng thái “Đang kết nối tới
        …”; None → bật lại, ẩn dòng trạng thái."""
        self._busy_address = address
        self._error = None
        if address is not None:
            self.cancel_button.setFocus(Qt.FocusReason.OtherFocusReason)
        self._update_enabled()
        self._refresh_status()
        if address is None:
            self.edit.setFocus(Qt.FocusReason.OtherFocusReason)
            self.edit.selectAll()

    def show_error(self, text: texts.Text) -> None:
        """Hiện câu lỗi (hàm để dịch lại khi đổi ngôn ngữ) và bật lại ô."""
        self._busy_address = None
        self._error = text
        self._update_enabled()
        self._refresh_status()
        self.edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.edit.selectAll()
        with contextlib.suppress(Exception):  # cố gắng tốt nhất, như `InfoBar`
            QAccessible.updateAccessibility(QAccessibleEvent(self.error_label, QAccessible.Event.Alert))

    def _refresh_status(self) -> None:
        """Chữ, màu và trợ năng của dòng trạng thái theo lỗi / việc đang chờ."""
        label = self.error_label
        if self._error is not None:
            text, tone = texts.resolve(self._error), "danger"
        elif self._busy_address is not None:
            text, tone = dialog_connecting(self._busy_address), "muted"
        else:
            text, tone = "", "muted"
        label.setText(text)
        label.setAccessibleName(text)
        label.setVisible(bool(text))
        if label.property(theme.TONE_PROPERTY) != tone:
            label.setProperty(theme.TONE_PROPERTY, tone)
            label.style().unpolish(label)
            label.style().polish(label)
        self.edit.setAccessibleDescription(
            texts.resolve(self._error) if self._error is not None else dialog_help()
        )
        self._fit_height()

    def _fit_height(self) -> None:
        """Nới hộp cho đủ chữ xuống dòng ở bề rộng hiện tại (Qt không tự nới cửa sổ theo heightForWidth
        của nhãn); không thu nhỏ hộp người dùng đã kéo to."""
        layout = self.layout()
        if layout is None or not self.isVisible():
            return
        layout.activate()
        width = self.width()
        needed = layout.heightForWidth(width) if layout.hasHeightForWidth() else layout.sizeHint().height()
        if needed > self.height():
            self.resize(width, needed)

    def retranslate(self) -> None:
        name = _peer(self._view)
        self.setWindowTitle(dialog_title())
        self.setAccessibleName(dialog_title())
        self.label.setText(dialog_label(name))
        self.edit.setAccessibleName(dialog_label(name))
        self.help_label.setText(dialog_help())
        self.connect_button.setText(dialog_connect())
        self.cancel_button.setText(dialog_cancel())
        self._refresh_status()

    def _update_enabled(self) -> None:
        busy = self.busy
        self.edit.setEnabled(not busy)
        self.connect_button.setEnabled(not busy and bool(self.edit.text().strip()))

    def _on_text_changed(self, _text: str) -> None:
        if self._error is not None:
            self._error = None
            self._refresh_status()
        self._update_enabled()

    def _on_connect(self) -> None:
        if self.busy or not self.edit.text().strip():
            return
        self.submitted.emit(self.edit.text())

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()


# ---- Điều phối -----------------------------------------------------------------------------------


def _read_discovery(node: Node) -> bool:
    return node.discovery_active


def _retry(node: Node, device_id: str) -> Awaitable[ConnectResult | None]:
    return node.retry_connect(device_id)


def _connect_address(node: Node, device_id: str, ip: str, port: int) -> Awaitable[ConnectResult]:
    return node.connect_address(device_id, ip, port)


@dataclass(frozen=True, slots=True)
class _Tried:
    overlay: Overlay
    cause: Cause
    result: ConnectResult | None


_Key = tuple[str, Cause, ConnectResult | None, Overlay, str, tuple[str, int] | None]


class DiagnoseController(QObject):
    """Thanh chẩn đoán và hộp “Kết nối bằng IP” của `ChatView` (xem docstring module)."""

    def __init__(
        self,
        view: ChatView,
        devices: DeviceListModel,
        window: QWidget,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._view = view
        self._devices = devices
        self._window = window
        self._host: NodeHost | None = None
        self._started = False
        self._closing = False
        self._device_id: str | None = None
        self._failures: dict[str, tuple[ConnectResult, bool]] = {}
        self._offline_since: dict[str, float] = {}
        self._busy: dict[str, SubmitJob] = {}
        self._tried: dict[str, _Tried] = {}
        # Lỗi đã nhớ lúc bấm [Thử lại] (H17): vẽ BUSY trong lúc chờ khi chưa có `ConnectFailed` mới.
        self._before_retry: dict[str, tuple[ConnectResult, bool] | None] = {}
        self._dismissed: dict[str, _Key] = {}
        self._discovery: bool | None = None
        self._poll_token = object()  # đổi ở `attach` / `close_all`: kết quả poll cũ bị bỏ
        self._polling = False  # đang có một `call` đọc mDNS
        self._shown_key: _Key | None = None
        self._dialog: ConnectAddressDialog | None = None
        self._dialog_job: SubmitJob | None = None

        self.bar = DiagnoseBar(view)
        self.bar.action_triggered.connect(self._open_dialog)
        self.bar.secondary_triggered.connect(self._retry_clicked)
        self.bar.dismissed.connect(self._on_dismissed)
        view.set_device_banner(self.bar, self.bar.focus_chain())

        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self._render)
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._on_poll_tick)

        for signal in (
            devices.modelReset,
            devices.dataChanged,
            devices.rowsInserted,
            devices.rowsRemoved,
            devices.rowsMoved,
            devices.layoutChanged,
        ):
            signal.connect(self._reevaluate)
        view.installEventFilter(self)

    # ---- API ------------------------------------------------------------------------------------

    @property
    def dialog(self) -> ConnectAddressDialog | None:
        """Hộp “Kết nối bằng IP” đang mở, hay None."""
        return self._dialog

    def attach(self, host: NodeHost | None) -> None:
        """Host mới (None: không có): hủy việc đang chạy, đóng hộp, xóa mọi trạng thái, ẩn thanh."""
        self._drop_all()
        self._host = host
        self._started = False
        self._render()

    def on_started(self, host: NodeHost) -> None:
        """Node của `host` đã chạy: mọi máy đang ngoại tuyến tính mốc lại từ lúc này, đọc mDNS ngay."""
        if host is not self._host or self._closing:
            return
        self._started = True
        self._offline_since.clear()
        self._track_devices()
        self._update_poll(read_now=True)
        self._render()

    def on_node_event(self, event: NodeEvent) -> None:
        """Sự kiện của host hiện tại (model thiết bị đã cập nhật trước)."""
        view = getattr(event, "device", None)
        if not isinstance(view, DeviceView):
            return
        device_id = view.device_id
        if isinstance(event, ConnectFailed):
            if event.result is ConnectResult.OK:
                return
            if event.from_hint and event.result is ConnectResult.UNREACHABLE:
                return  # chỉ nói về địa chỉ vừa nhập; hộp đã báo (spec 2.3)
            self._failures[device_id] = (event.result, event.from_hint)
        elif isinstance(event, DeviceConnected):
            self._forget_errors(device_id)
        self._render()

    def close_all(self) -> None:
        """App đang thoát: hủy việc, đóng hộp, dừng poll, ẩn thanh; kết quả về sau bị bỏ."""
        self._closing = True
        self._drop_all()
        self._render()

    def set_device(self, device_id: str | None) -> None:
        """Máy đang mở trong `ChatView` (None: không máy nào)."""
        if device_id == self._device_id:
            return
        self._device_id = device_id
        self._update_poll(read_now=True)
        self._render()

    # ---- Trạng thái -----------------------------------------------------------------------------

    def _drop_all(self) -> None:
        for job in self._busy.values():
            job.cancel()
        self._busy.clear()
        self._close_dialog()
        self._failures.clear()
        self._offline_since.clear()
        self._tried.clear()
        self._before_retry.clear()
        self._dismissed.clear()
        self._discovery = None
        self._poll_token = object()
        self._polling = False
        self._poll_timer.stop()
        self._show_timer.stop()

    def _forget_errors(self, device_id: str) -> None:
        self._failures.pop(device_id, None)
        self._tried.pop(device_id, None)
        self._before_retry.pop(device_id, None)
        self._dismissed.pop(device_id, None)

    def _forget_device(self, device_id: str) -> None:
        self._forget_errors(device_id)
        self._offline_since.pop(device_id, None)

    def _track_devices(self) -> None:
        """Mốc ngoại tuyến (chỉ đặt lúc **chuyển** sang ngoại tuyến); quên máy thôi ghép đôi / rời model."""
        now = _monotonic()
        paired: set[str] = set()
        for view in self._devices.devices():
            if not view.paired:
                continue
            paired.add(view.device_id)
            if not view.online and not view.connected:
                self._offline_since.setdefault(view.device_id, now)
            else:
                self._offline_since.pop(view.device_id, None)
        known = (
            set(self._failures)
            | set(self._tried)
            | set(self._before_retry)
            | set(self._dismissed)
            | set(self._offline_since)
        )
        for device_id in known - paired:
            self._forget_device(device_id)

    def _reevaluate(self, *_args: object) -> None:
        self._track_devices()
        self._update_poll()
        self._render()

    # ---- Poll mDNS ------------------------------------------------------------------------------

    def _should_poll(self) -> bool:
        if not self._started or self._closing or self._host is None or self._device_id is None:
            return False
        view = self._devices.device(self._device_id)
        return view is not None and view.paired and not view.online and not view.connected

    def _update_poll(self, *, read_now: bool = False) -> None:
        if not self._should_poll():
            self._poll_timer.stop()
            return
        if not self._poll_timer.isActive():
            self._poll_timer.start(DISCOVERY_POLL_MS)
            read_now = True
        if read_now:
            self._poll()

    def _on_poll_tick(self) -> None:
        if not self._should_poll():
            self._poll_timer.stop()
            return
        self._poll()

    def _poll(self) -> None:
        host = self._host
        if self._polling or host is None or not self._should_poll() or not self._view.isVisible():
            return
        self._polling = True
        host.call(_read_discovery, functools.partial(self._on_poll_done, host, self._poll_token))

    def _on_poll_done(self, host: NodeHost, token: object, result: object) -> None:
        if host is not self._host or token is not self._poll_token or self._closing:
            return
        self._polling = False
        if isinstance(result, bool):
            if result != self._discovery:
                self._discovery = result
                self._render()
        elif isinstance(result, BaseException):
            log.debug("không đọc được trạng thái mDNS: %s", type(result).__name__)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._view and event.type() == QEvent.Type.Show:
            self._poll()
        return False

    # ---- [Thử lại] ------------------------------------------------------------------------------

    def _retry_clicked(self) -> None:
        host, device_id = self._host, self._device_id
        if host is None or device_id is None or not self._started or self._closing:
            return
        if device_id in self._busy:
            return
        # H6/H17: `ConnectFailed` của lần này đặt lại nếu có; trong lúc chờ vẽ theo lỗi chụp lúc bấm.
        self._before_retry[device_id] = self._failures.pop(device_id, None)
        self._tried.pop(device_id, None)
        cell: list[SubmitJob] = []
        job = host.submit(
            functools.partial(_retry, device_id=device_id),
            functools.partial(self._on_retry_done, host, device_id, cell),
        )
        cell.append(job)
        self._busy[device_id] = job
        self._render()

    def _on_retry_done(self, host: NodeHost, device_id: str, cell: list[SubmitJob], result: object) -> None:
        if host is not self._host or self._closing:
            return
        if not cell or self._busy.get(device_id) is not cell[0]:
            return
        del self._busy[device_id]
        before = self._before_retry.pop(device_id, None)
        if isinstance(result, BaseException) and not isinstance(result, KeyError) and before is not None:
            # Lần thử không ra kết quả nào: chưa có gì cho thấy nguyên nhân cũ đã hết.
            self._failures.setdefault(device_id, before)
        if isinstance(result, asyncio.CancelledError | RuntimeError):
            pass
        elif isinstance(result, KeyError):
            self._forget_device(device_id)
        elif isinstance(result, BaseException):
            log.warning("thử kết nối lại lỗi: %s", type(result).__name__)
        elif result is None:
            self._set_tried(device_id, Overlay.NO_TARGET)
        elif result is ConnectResult.UNREACHABLE:
            # H17: core chỉ báo UNREACHABLE sau vài lần lỗi liền, mà `retry_now` đặt lại bộ đếm; chưa có
            # sự kiện mới mà máy vẫn trong mạng thì đúng là ISOLATED.
            view = self._devices.device(device_id)
            if device_id not in self._failures and view is not None and view.online:
                self._failures[device_id] = (ConnectResult.UNREACHABLE, False)
            self._set_tried(device_id, Overlay.TRIED_UNREACHABLE)
        elif result is ConnectResult.IDENTITY_MISMATCH:
            if self._diagnosis(device_id).cause is not Cause.BLOCKED:
                self._set_tried(device_id, Overlay.TRIED_IDENTITY)
        else:  # OK, REJECTED, VERSION_MISMATCH: sự kiện đi kèm đã nói đủ
            self._tried.pop(device_id, None)
        self._render()

    def _set_tried(self, device_id: str, overlay: Overlay) -> None:
        diagnosis = self._diagnosis(device_id)
        if diagnosis.cause is Cause.NONE:
            self._tried.pop(device_id, None)
            return
        self._tried[device_id] = _Tried(overlay, diagnosis.cause, diagnosis.result)

    # ---- Hộp “Kết nối bằng IP” ------------------------------------------------------------------

    def _open_dialog(self) -> None:
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        device_id = self._device_id
        view = None if device_id is None else self._devices.device(device_id)
        if view is None or self._host is None or self._closing:
            return
        dialog = ConnectAddressDialog(view, self._window)
        dialog.submitted.connect(functools.partial(self._on_dialog_submitted, dialog))
        dialog.finished.connect(functools.partial(self._on_dialog_finished, dialog))
        self._dialog = dialog
        self._render()
        dialog.open()

    def _on_dialog_submitted(self, dialog: ConnectAddressDialog, text: str) -> None:
        host = self._host
        if dialog is not self._dialog or self._dialog_job is not None:
            return
        if host is None or self._closing:
            dialog.show_error(connect_error_text)
            return
        view = self._devices.device(dialog.device_id) or dialog.view
        if view.last_address is not None:
            default_port = view.last_address[1]
        else:
            default_port = view.port if view.port is not None else DEFAULT_PORT
        try:
            ip, port = parse_manual_address(text, default_port)
        except ValueError:
            dialog.show_error(functools.partial(radar.invalid_address_text, text))
            return
        address = f"{ip}:{port}"
        cell: list[SubmitJob] = []
        job = host.submit(
            functools.partial(_connect_address, device_id=dialog.device_id, ip=ip, port=port),
            functools.partial(self._on_dialog_done, host, dialog, cell, address, text),
        )
        cell.append(job)
        self._dialog_job = job
        dialog.set_busy(address)

    def _on_dialog_done(
        self,
        host: NodeHost,
        dialog: ConnectAddressDialog,
        cell: list[SubmitJob],
        address: str,
        text: str,
        result: object,
    ) -> None:
        if host is not self._host or self._closing or dialog is not self._dialog:
            return
        if not cell or self._dialog_job is not cell[0]:
            return
        self._dialog_job = None
        name = _peer(dialog.view)
        if isinstance(result, asyncio.CancelledError):
            dialog.set_busy(None)
        elif result is ConnectResult.OK:
            dialog.accept()
        elif result is ConnectResult.UNREACHABLE:
            dialog.show_error(functools.partial(pairing.unreachable_text, address))
        elif result is ConnectResult.IDENTITY_MISMATCH:
            dialog.show_error(functools.partial(not_this_device_text, address, name))
        elif result in (ConnectResult.REJECTED, ConnectResult.VERSION_MISMATCH):
            assert isinstance(result, ConnectResult)
            dialog.show_error(functools.partial(radar.connect_failed_text, dialog.view, result))
        elif isinstance(result, ValueError):
            dialog.show_error(functools.partial(radar.invalid_address_text, text))
        elif isinstance(result, KeyError):
            dialog.reject()
        else:
            if not isinstance(result, RuntimeError):
                log.warning("kết nối bằng IP lỗi: %s", type(result).__name__)
            dialog.show_error(connect_error_text)

    def _on_dialog_finished(self, dialog: ConnectAddressDialog, _result: int) -> None:
        if dialog is not self._dialog:
            return
        job, self._dialog_job = self._dialog_job, None
        self._dialog = None
        if job is not None:
            job.cancel()
        self._render()

    def _close_dialog(self) -> None:
        dialog, self._dialog = self._dialog, None
        job, self._dialog_job = self._dialog_job, None
        if job is not None:
            job.cancel()
        if dialog is not None:
            dialog.reject()

    # ---- Vẽ -------------------------------------------------------------------------------------

    def _failure(self, device_id: str) -> tuple[ConnectResult, bool] | None:
        """Lỗi để chẩn đoán: lỗi đang nhớ; đang [Thử lại] mà chưa có sự kiện mới thì lỗi lúc bấm (H17)."""
        failure = self._failures.get(device_id)
        if failure is None and device_id in self._busy:
            return self._before_retry.get(device_id)
        return failure

    def _diagnosis(self, device_id: str) -> Diagnosis:
        return diagnose(self._devices.device(device_id), self._failure(device_id), self._discovery)

    def _overlay(self, device_id: str, diagnosis: Diagnosis) -> Overlay:
        if device_id in self._busy:
            return Overlay.BUSY
        tried = self._tried.get(device_id)
        if tried is None:
            return Overlay.NONE
        if (tried.cause, tried.result) != (diagnosis.cause, diagnosis.result):
            del self._tried[device_id]  # nguyên nhân hay loại lỗi đã đổi
            return Overlay.NONE
        return tried.overlay

    def _state(self) -> tuple[DeviceView, Diagnosis, Overlay, _Key] | None:
        """(view, nguyên nhân, overlay, khóa) của máy đang mở; None khi không có gì để nói."""
        device_id = self._device_id
        if not self._started or self._closing or self._host is None or device_id is None:
            return None
        view = self._devices.device(device_id)
        diagnosis = self._diagnosis(device_id)
        if view is None or diagnosis.cause is Cause.NONE:
            self._tried.pop(device_id, None)
            self._before_retry.pop(device_id, None)
            return None
        overlay = self._overlay(device_id, diagnosis)
        key: _Key = (device_id, diagnosis.cause, diagnosis.result, overlay, view.name, view.last_address)
        return view, diagnosis, overlay, key

    def _hide(self) -> None:
        self._shown_key = None
        self.bar.clear()

    def _render(self) -> None:
        self._show_timer.stop()
        state = self._state()
        if state is None:
            self._hide()
            return
        view, diagnosis, overlay, key = state
        device_id = view.device_id
        if not diagnosis.urgent and overlay is Overlay.NONE:
            since = self._offline_since.get(device_id)
            elapsed_ms = 0.0 if since is None else (_monotonic() - since) * 1000
            remaining = SHOW_DELAY_MS - elapsed_ms
            if remaining > 0:
                self._hide()
                self._show_timer.start(max(1, math.ceil(remaining)))
                return
        dismissed = self._dismissed.get(device_id)
        if dismissed is not None:
            if dismissed == key:
                self._hide()
                return
            del self._dismissed[device_id]
        focus = QApplication.focusWidget()
        if key != self._shown_key or self.bar.isHidden():
            self._show(view, diagnosis, overlay)
            self._shown_key = key
        dialog_open = self._dialog is not None and self._dialog.device_id == device_id
        retry_shown = diagnosis.can_retry and overlay not in (Overlay.BUSY, Overlay.NO_TARGET)
        self.bar.secondary_button.setVisible(retry_shown and not dialog_open)
        buttons = (self.bar.action_button, self.bar.secondary_button)
        if focus is not None and focus in buttons and focus.isHidden():
            # Nút đang giữ tiêu điểm vừa ẩn (bấm [Thử lại]): tiêu điểm về tiêu đề thanh, không rơi mất.
            self.bar.title_label.setFocus(Qt.FocusReason.OtherFocusReason)

    def _show(self, view: DeviceView, diagnosis: Diagnosis, overlay: Overlay) -> None:
        name = _peer(view)
        title, body = _cause_texts(diagnosis, view)
        level = diagnosis.level
        if overlay is Overlay.BUSY:
            title, body, level = functools.partial(connecting_title, name), "", InfoLevel.INFO
        elif overlay is Overlay.TRIED_UNREACHABLE:
            body = body or title
            title = functools.partial(still_failed_title, name)
        elif overlay is Overlay.TRIED_IDENTITY:
            title = functools.partial(still_failed_title, name)
            if view.last_address is not None:
                body = functools.partial(saved_changed_body, format_address(*view.last_address), name)
            else:
                body = not_seen_body
        elif overlay is Overlay.NO_TARGET and diagnosis.cause is Cause.RECONNECTING:
            body = not_seen_body  # H4: không có đích nào đang được thử
        busy = overlay is Overlay.BUSY
        action = connect_by_ip if diagnosis.can_connect_ip and not busy else None
        retry = diagnosis.can_retry and overlay not in (Overlay.BUSY, Overlay.NO_TARGET)
        self.bar.show_message(level, title, body, action=action, secondary=texts.retry if retry else None)

    def _on_dismissed(self) -> None:
        device_id = self._device_id
        if device_id is None:
            return
        self._tried.pop(device_id, None)
        state = self._state()
        self._shown_key = None
        if state is not None:
            self._dismissed[device_id] = state[3]
        self._render()
