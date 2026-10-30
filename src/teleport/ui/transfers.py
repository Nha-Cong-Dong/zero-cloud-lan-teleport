"""Điều phối gửi/nhận file của giao diện (mvp F4, F5): nối radar, danh sách lượt, hộp hỏi, node.

`TransfersController` sống trên thread Qt, dựng trong `ui.app.GuiApp` (thuộc tính `transfers`). Nó
giữ `TransfersView` (`view`, đặt vào `MainWindow.transfers_area`) và `OfferPrompts` (`offers`), và
chỉ chạm node qua `NodeHost.call` / `submit` (không bao giờ gọi `Node` từ thread Qt).

**Host** (`attach`): mỗi `NodeHost` mới (lần đầu, “Bắt đầu”, “Thử lại”) → xóa danh sách lượt và
thông báo, đóng hộp hỏi, bỏ việc “ghép rồi gửi” đang chờ, đặt thư mục nhận. Mọi kết quả `call` /
`submit` mang theo host lúc gọi và bị bỏ nếu host đã đổi; `on_node_event` / `on_transfer` chỉ được
`ui.app` gọi với sự kiện của host hiện tại. Lúc `started` (`on_started`): một `call` đọc
`(devices(), transfers.transfers())` → tên máy, trạng thái kết nối, ảnh chụp các lượt (gộp với
`TransferResumed` tới trước, spec hackathon mục 9.7).

**Gửi** (`request_send(device_id, paths)`, từ kéo thả hay “Gửi file…”): thread Qt không `stat`
file người dùng (D7); việc kiểm file do `transfers.send` làm trên `DiskExecutor` của node.
1. Danh sách rỗng → bỏ qua; `send.selection_error` (quá `MAX_OFFER_FILES` file, trùng) → thông báo.
2. Thiết bị chưa ghép đôi → “ghép rồi gửi”: nhớ một việc chờ `(device_id, paths)` và phát
   `pair_needed(DeviceView)` (luồng ghép đôi nối vào). Chưa ai nối `pair_needed` → không giữ việc
   chờ, hiện câu mvp mục 8 “Chưa ghép đôi với *X*…”. Đã có việc chờ → câu mvp mục 8 “Đang chờ ghép
   đôi với *A* để gửi file…” (*A* là máy **đang chờ ghép**). `on_pairing_finished(peer_device_id,
   paired)` kết thúc việc chờ: khớp `peer_device_id` (không theo `session_id`, spec mục 9.2) và
   `paired` → gửi; không ghép được (`paired` False, với `peer_device_id` khớp hay None) → bỏ việc chờ
   (luồng ghép đôi tự báo lỗi). Lưới an toàn: luồng ghép đôi không báo lại thì việc chờ tự bỏ, im
   lặng, sau `PENDING_PAIR_TIMEOUT` giây (`QTimer` một lần; `attach`, `close_all` và lúc việc chờ
   kết thúc thì dừng hẹn).
3. Kết nối đang bị chặn (`ConnectFailed` gần nhất trên radar là REJECTED, IDENTITY_MISMATCH,
   VERSION_MISMATCH; spec mục 10 A2) → câu mvp mục 8, không tạo lượt. Dải thông báo kèm nút:
   REJECTED → “Ghép đôi lại” (phát `repair_requested(DeviceView)`, luồng ghép đôi nối);
   IDENTITY_MISMATCH → “Hủy ghép đôi” (phát `radar.unpair_requested(device_id)`, luồng ghép đôi hỏi
   xác nhận). Chỉ chưa kết nối thì vẫn gửi: thẻ hiện “Đang chờ kết nối tới X…”.
   Lần mở kết nối do chính lượt đó gây ra mà bị chặn (`on_node_event` nhận `ConnectFailed` chặn,
   lần đầu radar biết) → mọi **lượt mới** gửi tới máy đó bị gỡ thẻ và hủy (không treo “Đang nối
   lại…” tới hạn nối lại), rồi hiện câu và nút như trên. Lượt mới: lượt gửi mà lúc thẻ xuất hiện
   radar báo chưa kết nối, và chưa có `DeviceConnected` nào của máy đó từ đó tới giờ, tức `offer`
   chưa từng đi. Lượt đã gửi `offer` (lượt dở) giữ nguyên: bị từ chối khi còn lượt dở thì
   `core.links` vẫn hẹn thử lại (docstring `core/links.py`), lỗi tạm trông giống bị từ chối.
   Đánh đổi: lỗi tạm đó (máy kia đang thoát hay khởi động lại) cũng gỡ lượt mới, người dùng gửi lại;
   lượt bị gỡ vào lịch sử là `CANCELLED_USER` (như bấm “Hủy”).
4. `host.submit(transfers.send)`; lỗi → `send.send_error_message` (None thì im lặng). Thẻ của lượt
   tự hiện qua `TransferStatusChanged`.

“Gửi file…” (`pick_and_send`): `QFileDialog` cha là cửa sổ chính, `ExistingFiles`, mở bằng `open()`
(không `exec()`), dựng qua seam cấp module `file_dialog_factory` (đọc lúc gọi).

**Thông báo**: lỗi chọn gửi hiện ở dải thông báo của `TransfersView` (`show_notice`), không hộp
chặn. Khay: có `offer` chờ trả lời → `texts.offer_waiting()`, **chỉ khi app không được dùng**
(`app_active()` False: không cửa sổ nào của app đang active; đọc trước `offers.handle`, vì hộp hỏi
mới mở có thể giành active); nhận xong một lượt → câu mvp mục 8 “Đã nhận *n* file từ *X*.” (luôn
hiện; một file: tên file thật đã lưu, lấy từ `FileDone.path.name` cuối, có thể là `ten (1).ext`;
chưa có thì tên trong `offer`), kèm `folder` để bấm bóng thoại mở thư mục: thư mục cha của file đã
lưu cuối, không có thì thư mục nhận của host. Tên máy kia qua `texts.peer_label`, tên file qua
`texts.file_label`; bóng thoại khay là chữ thuần.

**Lượt gửi dở** (`active_sends()`): tên hiển thị các máy đang có lượt gửi chưa kết thúc, theo các
hàng của `TransfersView` (không gọi node); `ui.app` dùng để hỏi lại trước khi Thoát.

Chuỗi qua `QCoreApplication.translate("Transfers", ...)` lúc dùng.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from PySide6.QtCore import QCoreApplication, QMetaMethod, QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from teleport.app.devices import DeviceView
from teleport.app.node import (
    ConnectFailed,
    DeviceConnected,
    DeviceDisconnected,
    DeviceOffline,
    DeviceOnline,
    NodeEvent,
)
from teleport.app.transfers import (
    Direction,
    FileDone,
    OfferReceived,
    TransferFinished,
    TransferInfo,
    TransferKey,
    TransferResult,
    TransferUpdate,
)
from teleport.core import pairing
from teleport.core.links import ConnectResult
from teleport.ui import send, texts
from teleport.ui.bridge import NodeHost
from teleport.ui.main_window import MainWindow
from teleport.ui.offer import OfferPrompts
from teleport.ui.transfers_view import TransfersView
from teleport.ui.tray import Tray

# Seam: dựng hộp chọn file (test thay bằng hàm trả hộp giả). Nhận cha (cửa sổ chính).
file_dialog_factory: Callable[[QWidget | None], QFileDialog] = QFileDialog

log = logging.getLogger(__name__)

_DEVICE_EVENTS = (DeviceOnline, DeviceOffline, DeviceConnected, DeviceDisconnected, ConnectFailed)


# Lưới an toàn của “ghép rồi gửi”: việc chờ tự bỏ (im lặng) sau từng này giây nếu luồng ghép đôi
# không báo lại. Dài hơn phiên ghép đôi dài nhất ở vai khởi xướng (chờ `pair_commit` và `pair_reveal`,
# người dùng quyết định, thời gian ân hạn), cộng `_PENDING_PAIR_MARGIN` cho việc nối kết nối và hộp
# của luồng ghép đôi. Đọc lúc đặt hẹn (test chỉnh được).
_PENDING_PAIR_MARGIN = 60.0
PENDING_PAIR_TIMEOUT = (
    2 * pairing.MESSAGE_TIMEOUT + pairing.CONFIRM_TIMEOUT + pairing.CONFIRM_GRACE + _PENDING_PAIR_MARGIN
)


def default_app_active() -> bool:
    """App đang được dùng: một cửa sổ của app (cửa sổ chính hay một hộp) đang active."""
    return QApplication.activeWindow() is not None


def pair_wait_text(peer: str) -> str:
    """mvp mục 8, “Giao diện: gửi tiếp khi đang chờ ghép đôi để gửi”. `peer`: máy **đang chờ ghép**."""
    return texts.fill(
        QCoreApplication.translate(
            "Transfers", "Đang chờ ghép đôi với {name} để gửi file. Hãy chờ xong rồi gửi tiếp."
        ),
        name=peer,
    )


def received_text(info: TransferInfo, peer: str, saved_name: str | None = None) -> str:
    """Bóng thoại khay khi nhận xong một lượt (mvp mục 8). `peer` đã qua `peer_label`.

    Một file: tên file thật đã lưu (`saved_name`, từ `FileDone.path.name`; có thể đã thành
    `ten (1).ext` để không ghi đè) khi có, không thì tên trong `offer`.
    """
    t = QCoreApplication.translate
    if len(info.files) == 1 and (saved_name or info.names):
        name = saved_name or info.names[0]
        return texts.fill(t("Transfers", "Đã nhận {file} từ {name}."), file=texts.file_label(name), name=peer)
    return texts.fill(t("Transfers", "Đã nhận %n file từ {name}.", None, len(info.files)), name=peer)


def repair_action_text() -> str:
    """Nút của thông báo “*X* từ chối kết nối từ máy này” (mvp mục 8, “Máy kia từ chối máy này”)."""
    return QCoreApplication.translate("Transfers", "Ghép đôi lại")


def unpair_action_text() -> str:
    """Nút của thông báo “*X* đang dùng một danh tính khác…” (mvp mục 8, “Danh tính thiết bị thay đổi”)."""
    return QCoreApplication.translate("Transfers", "Hủy ghép đôi")


def pick_title(peer: str) -> str:
    """Tiêu đề hộp chọn file của “Gửi file…”. `peer` đã qua `peer_label`."""
    return texts.fill(QCoreApplication.translate("Transfers", "Chọn file gửi tới {name}"), name=peer)


class TransfersController(QObject):
    """Gửi/nhận file trên thread Qt: radar → node, sự kiện lượt → danh sách lượt, hộp hỏi, khay."""

    pair_needed = Signal(object)  # DeviceView: chưa ghép đôi, cần ghép trước khi gửi
    repair_requested = Signal(object)  # DeviceView: nút “Ghép đôi lại” (máy kia từ chối máy này)

    def __init__(
        self,
        window: MainWindow,
        tray: Tray,
        parent: QObject | None = None,
        *,
        app_active: Callable[[], bool] | None = None,
    ) -> None:
        """`app_active`: app đang được dùng không (bóng thoại offer chỉ hiện khi False).

        Mặc định `default_app_active` của module (đọc lúc gọi, test vá được).
        """
        super().__init__(parent)
        self._window = window
        self._tray = tray
        self._app_active = app_active if app_active is not None else lambda: default_app_active()
        self._host: NodeHost | None = None
        self._names: dict[str, str | None] = {}
        self._offer_names: dict[TransferKey, str | None] = {}
        self._saved_paths: dict[TransferKey, Path] = {}  # file thật đã lưu cuối (`FileDone.path`)
        self._pending: tuple[str, list[Path]] | None = None  # “ghép rồi gửi” đang chờ
        self._unoffered: set[TransferKey] = set()  # lượt gửi mới: `offer` chưa từng đi (bước 3)
        self._dropped: set[TransferKey] = set()  # thẻ đã gỡ, đang chờ `TransferFinished` của lần hủy
        self._pending_timer = QTimer(self)
        self._pending_timer.setSingleShot(True)
        self._pending_timer.timeout.connect(self._drop_pending)
        self._dialog: QFileDialog | None = None

        self.view = TransfersView(window.transfers_area)
        area_layout = window.transfers_area.layout()
        if area_layout is not None:
            area_layout.addWidget(self.view)
        self.offers = OfferPrompts(parent=self)

        radar = window.radar
        radar.send_files_requested.connect(self.request_send)
        radar.pick_files_requested.connect(self.pick_and_send)
        radar.drop_hint_requested.connect(self.view.show_notice)
        self.view.cancel_requested.connect(self.cancel)
        self.offers.decided.connect(self.decide)

    # ---- Vòng đời host -----------------------------------------------------------------------

    @property
    def host(self) -> NodeHost | None:
        return self._host

    @property
    def pending(self) -> tuple[str, list[Path]] | None:
        """Việc “ghép rồi gửi” đang chờ: (device_id, các đường dẫn), hay None."""
        return self._pending

    def attach(self, host: NodeHost | None) -> None:
        """Dùng `host` mới: xóa lượt và thông báo, đóng hộp hỏi, bỏ việc chờ, đặt thư mục nhận."""
        self._host = host
        self.view.clear()
        self.offers.close_all()
        self._drop_pending()
        self._names = {}
        self._offer_names = {}
        self._saved_paths = {}
        self._unoffered = set()
        self._dropped = set()
        self.view.set_device_names(self._names)
        receive_dir = None if host is None else host.options.receive_dir
        self.offers.set_receive_dir(receive_dir)
        self.view.set_receive_dir(receive_dir)

    def on_started(self, host: NodeHost) -> None:
        """Node của `host` đã chạy: đọc một ảnh chụp thiết bị và lượt truyền."""
        if host is not self._host:
            return
        host.call(
            lambda node: (node.devices(), node.transfers.transfers()),
            functools.partial(self._on_snapshot, host),
        )

    def close_all(self) -> None:
        """App đang thoát: đóng hộp hỏi và hộp chọn file, bỏ việc chờ."""
        self.offers.close_all()
        self._drop_pending()
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()

    # ---- Sự kiện của node (đã về thread Qt, của host hiện tại) --------------------------------

    def on_node_event(self, event: NodeEvent) -> None:
        if not isinstance(event, _DEVICE_EVENTS):
            return
        view = event.device
        if not isinstance(view, DeviceView):
            return
        if isinstance(event, DeviceConnected):
            self.view.set_connected(view.device_id, True)
            self._unoffered = {k for k in self._unoffered if k.device_id != view.device_id}
        elif isinstance(event, DeviceDisconnected):
            self.view.set_connected(view.device_id, False)
        if self._names.get(view.device_id) != view.name:
            self._names[view.device_id] = view.name
            self.view.set_device_names(self._names)
        if isinstance(event, ConnectFailed):
            self._on_connect_failed(view, event.result)

    def active_sends(self) -> list[str]:
        """Tên hiển thị (`texts.peer_label`) các máy đang có lượt gửi chưa kết thúc, theo thứ tự hàng.

        Mỗi máy một lần (theo `device_id`). Chỉ đọc các hàng của `TransfersView`, không gọi node.
        """
        model = self._window.radar.model
        seen: set[str] = set()
        names: list[str] = []
        for row in self.view.rows():
            key = row.key
            if key.direction is not Direction.SEND or row.finished or key.device_id in seen:
                continue
            seen.add(key.device_id)
            names.append(self._peer(key.device_id, model.device(key.device_id)))
        return names

    def on_transfer(self, update: TransferUpdate) -> None:
        key = _update_key(update)
        if key in self._dropped:
            # Lượt mới đã gỡ thẻ vì bị chặn (bước 3): bỏ mọi sự kiện, tới `TransferFinished` của lần hủy.
            if isinstance(update, TransferFinished):
                self._dropped.discard(key)
                self._offer_names.pop(key, None)
            return
        if (
            key is not None
            and key.direction is Direction.SEND
            and not isinstance(update, TransferFinished)
            and self.view.row(key) is None
        ):
            device = self._window.radar.model.device(key.device_id)
            if device is None or not device.connected:
                self._unoffered.add(key)
        # Đọc trước `offers.handle`: hộp hỏi mới mở có thể giành active.
        active = isinstance(update, OfferReceived) and self._app_active()
        self.offers.handle(update)
        self.view.handle(update)
        if isinstance(update, OfferReceived):
            self._offer_names[update.key] = update.device_name
            if not update.auto_accepted and not active:
                self._tray.show_message(texts.offer_waiting())
        elif isinstance(update, FileDone):
            if update.key.direction is Direction.RECEIVE and update.path is not None and update.path.name:
                self._saved_paths[update.key] = update.path
        elif isinstance(update, TransferFinished):
            key = update.info.key
            self._unoffered.discard(key)
            offer_name = self._offer_names.pop(key, None)
            saved_path = self._saved_paths.pop(key, None)
            if update.result is TransferResult.DONE and key.direction is Direction.RECEIVE:
                name = self._names.get(key.device_id) or offer_name
                if saved_path is not None:
                    folder: Path | None = saved_path.parent
                else:
                    folder = None if self._host is None else self._host.options.receive_dir
                self._tray.show_message(
                    received_text(
                        update.info,
                        texts.peer_label(name, key.device_id),
                        None if saved_path is None else saved_path.name,
                    ),
                    folder=folder,
                )

    # ---- Gửi ---------------------------------------------------------------------------------

    def request_send(self, device_id: str, paths: Sequence[Path]) -> None:
        """Gửi `paths` tới `device_id` (kéo thả, “Gửi file…”). Không chạm đĩa trên thread Qt."""
        paths = list(paths)
        if not paths:
            return
        error = send.selection_error(paths)
        if error is not None:
            self.view.show_notice(error)
            return
        model = self._window.radar.model
        view = model.device(device_id)
        peer = self._peer(device_id, view)
        if view is not None and not view.paired:
            if self._pending is not None:
                waiting_id = self._pending[0]
                self.view.show_notice(pair_wait_text(self._peer(waiting_id, model.device(waiting_id))))
                return
            if not view.online:
                return  # không ghép đôi được với máy không trong mạng (radar cũng không nhận thả)
            if not self.isSignalConnected(QMetaMethod.fromSignal(self.pair_needed)):
                # Chưa có luồng ghép đôi nào nối vào: không giữ việc chờ (sẽ kẹt mãi).
                self.view.show_notice(send.not_paired_message(peer))
                return
            self._pending = (device_id, paths)
            self._pending_timer.start(max(0, int(PENDING_PAIR_TIMEOUT * 1000)))
            self.pair_needed.emit(view)
            return
        failure = model.failure(device_id)
        if failure is not None:
            blocked = send.blocked_message(failure, peer)
            if blocked is not None:
                self._show_blocked(blocked, failure, device_id, view)
                return
        self._submit_send(device_id, paths, peer)

    def on_pairing_finished(self, peer_device_id: str | None, paired: bool) -> None:
        """Kết quả ghép đôi (luồng ghép đôi gọi): khớp việc “ghép rồi gửi” đang chờ theo `peer_device_id`.

        Khớp và `paired` → gửi; `paired` False mà `peer_device_id` khớp hay None (phiên dừng trước khi
        biết máy kia) → bỏ việc chờ. Ghép được với máy khác → giữ việc chờ (hẹn bỏ vẫn chạy).
        """
        pending = self._pending
        if pending is None:
            return
        device_id, paths = pending
        if paired and peer_device_id == device_id:
            self._drop_pending()
            self._submit_send(
                device_id, paths, self._peer(device_id, self._window.radar.model.device(device_id))
            )
        elif not paired and (peer_device_id is None or peer_device_id == device_id):
            self._drop_pending()

    def pick_and_send(self, device_id: str) -> None:
        """“Gửi file…”: mở hộp chọn nhiều file (không chặn), chọn xong thì `request_send`."""
        old, self._dialog = self._dialog, None
        if old is not None:
            old.close()
            old.deleteLater()
        view = self._window.radar.model.device(device_id)
        dialog = file_dialog_factory(self._window)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setWindowTitle(pick_title(self._peer(device_id, view)))
        dialog.filesSelected.connect(
            lambda files, i=device_id: self.request_send(i, [Path(f) for f in files if f])
        )
        dialog.finished.connect(lambda _result, d=dialog: self._dialog_finished(d))
        self._dialog = dialog
        dialog.open()

    # ---- Hủy, trả lời lời mời ------------------------------------------------------------------

    def cancel(self, key: TransferKey) -> None:
        host = self._host
        if host is not None:
            host.call(lambda node: node.transfers.cancel(key), functools.partial(self._log_error, "hủy lượt"))

    def decide(self, key: TransferKey, accept: bool) -> None:
        host = self._host
        if host is not None:
            host.call(
                lambda node: node.transfers.decide(key, accept),
                functools.partial(self._log_error, "trả lời lời mời nhận file"),
            )

    # ---- Bên trong ---------------------------------------------------------------------------

    def _peer(self, device_id: str, view: DeviceView | None) -> str:
        name = view.name if view is not None else self._names.get(device_id)
        return texts.peer_label(name, device_id)

    def _drop_pending(self) -> None:
        """Bỏ việc “ghép rồi gửi” đang chờ (im lặng) và hẹn tự bỏ của nó."""
        self._pending_timer.stop()
        self._pending = None

    def _show_blocked(
        self, message: str, failure: ConnectResult | str, device_id: str, view: DeviceView | None
    ) -> None:
        """Thông báo thiết bị bị chặn (mục 10 A2), kèm nút hành động của mvp mục 8 khi có."""
        value = str(failure)
        if value == ConnectResult.REJECTED.value and view is not None:
            self.view.show_notice(
                message, repair_action_text(), functools.partial(self._request_repair, device_id)
            )
        elif value == ConnectResult.IDENTITY_MISMATCH.value:
            self.view.show_notice(
                message, unpair_action_text(), functools.partial(self._request_unpair, device_id)
            )
        else:
            self.view.show_notice(message)

    def _on_connect_failed(self, view: DeviceView, result: ConnectResult) -> None:
        """`ConnectFailed` chặn khi còn lượt mới tới máy đó: gỡ thẻ, hủy lượt, hiện câu (bước 3)."""
        keys = [k for k in self._unoffered if k.device_id == view.device_id]
        if not keys:
            return
        message = send.blocked_message(result, self._peer(view.device_id, view))
        if message is None:
            return  # chưa kết nối được: lượt chờ tiếp, thẻ đã có câu chờ
        self._unoffered.difference_update(keys)
        host = self._host
        for key in keys:
            self.view.remove(key)
            if host is not None:
                self._dropped.add(key)
                self.cancel(key)
        self._show_blocked(message, result, view.device_id, view)

    def _request_repair(self, device_id: str) -> None:
        """Nút “Ghép đôi lại”: phát `repair_requested` với view mới nhất của radar (luồng ghép đôi nối)."""
        view = self._window.radar.model.device(device_id)
        if view is not None:
            self.repair_requested.emit(view)

    def _request_unpair(self, device_id: str) -> None:
        """Nút “Hủy ghép đôi”: qua `radar.unpair_requested` (luồng ghép đôi hỏi xác nhận, kèm cảnh báo)."""
        self._window.radar.unpair_requested.emit(device_id)

    def _submit_send(self, device_id: str, paths: list[Path], peer: str) -> None:
        host = self._host
        if host is None:
            return
        host.submit(
            lambda node: node.transfers.send(device_id, paths),
            functools.partial(self._on_send_done, host, peer),
        )

    def _on_send_done(self, host: NodeHost, peer: str, result: object) -> None:
        if host is not self._host or not isinstance(result, BaseException):
            return
        message = send.send_error_message(result, peer)
        if message is not None:
            self.view.show_notice(message)

    def _on_snapshot(self, host: NodeHost, result: object) -> None:
        """Ảnh chụp lúc `started`: tên máy, trạng thái kết nối, các lượt chưa kết thúc."""
        if host is not self._host:
            return
        if isinstance(result, BaseException):
            log.warning("không đọc được danh sách lượt truyền: %s", type(result).__name__)
            return
        devices, infos = cast(tuple[tuple[DeviceView, ...], tuple[TransferInfo, ...]], result)
        for device in devices:
            if isinstance(device, DeviceView):
                self._names[device.device_id] = device.name
                self.view.set_connected(device.device_id, device.connected)
        self.view.set_device_names(self._names)
        self.view.load_snapshot(infos)

    def _dialog_finished(self, dialog: QFileDialog) -> None:
        if self._dialog is dialog:
            self._dialog = None
        dialog.deleteLater()

    @staticmethod
    def _log_error(what: str, result: object) -> None:
        if isinstance(result, BaseException):
            log.warning("không %s được: %s", what, type(result).__name__)


def _update_key(update: TransferUpdate) -> TransferKey | None:
    """Khóa lượt của một `TransferUpdate` (có loại mang `key`, có loại mang `info.key`)."""
    key = getattr(update, "key", None)
    if isinstance(key, TransferKey):
        return key
    info = getattr(update, "info", None)
    return info.key if isinstance(info, TransferInfo) else None
