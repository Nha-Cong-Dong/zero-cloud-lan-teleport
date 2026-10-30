"""Điểm vào `teleport-gui`: tham số, một bản mỗi thư mục cấu hình, ráp cửa sổ + khay + node.

Luồng khởi động (spec P4-3 mục 4): phân tích tham số (`--config-dir D`, `--background`,
`--selftest`); thư mục cấu hình = `--config-dir` › `TELEPORT_HOME` › `paths.config_dir()`, tuyệt
đối; dựng `QApplication` (`setQuitOnLastWindowClosed(False)`); i18n, theme; `SingleInstance.acquire`;
log ra file (bên dưới); `GuiApp` dựng cửa sổ, khay, rồi chạy node trong `bridge.NodeHost` nếu máy
đã có `device.crt`. Chưa có thì cửa sổ hiện màn “Bắt đầu” (spec hackathon mục 10 A1): chưa bấm
“Bắt đầu” thì không chạy node, không lấy `node.lock`, không quảng bá gì; bấm thì chạy node và
`Node.start` tự tạo danh tính (tên mặc định `app.node.default_device_name`). Hiện cửa sổ trừ khi
`--background` và có khay; `app.exec()`.

**Radar**: `NodeHost.node_event` → `RadarView.model.apply`; lúc `started` lấy ảnh chụp
`Node.devices()` qua `NodeHost.call` rồi `model.reset`. Sự kiện và kết quả của host cũ bị bỏ; dựng
host mới (“Thử lại”) thì `model.clear()` trước.

**Gửi/nhận file**: `ui.transfers.TransfersController` (`GuiApp.transfers`) giữ danh sách lượt, hộp
hỏi nhận file và việc gửi; `GuiApp` chỉ báo host mới (`attach`), `started`, `NodeEvent` và
`TransferUpdate` của host hiện tại cho nó.

**Ghép đôi** (`ui.pairing.PairingController`): radar `pair_requested` → `pair_device`,
`manual_address_entered` → `pair_address`, `unpair_requested` → hộp xác nhận hủy ghép đôi;
`devices_changed` (đọc lại `devices()` sau ghép đôi / hủy ghép đôi) → `model.reset`. Mỗi host mới
gọi `set_host` (đóng mọi hộp của host cũ); Thoát gọi `close_all`.

**Ghép rồi gửi**: `TransfersController.pair_needed` → `pair_device`; `PairingController.job_done`
(việc ghép chủ động kết thúc mà không PAIRED, hay `pair_device` không bắt đầu được) và `paired`
→ `TransfersController.on_pairing_finished`. Nút “Ghép đôi lại” (`repair_requested`) →
`pair_device`; không bắt đầu được vì radar không thấy máy kia (không bận, có host) thì dải thông
báo hiện `repair_offline_text` và mở khung “Nhập IP thủ công” (`open_address_entry`), không điền
sẵn IP.

**Log ra file** (spec hackathon Q7, mục 9.10): `<config_dir>/logs/teleport.log`, xoay khi đầy
`LOG_MAX_BYTES`, giữ thêm `LOG_BACKUPS` bản cũ; mức WARNING như log ra stderr. Chỉ gắn **sau**
`SingleInstance.acquire` thành công (chỉ một tiến trình ghi và xoay file), gỡ và đóng khi `main` trả
về. Theo mvp N9: chỉ ghi loại lỗi và mã kết quả, không ghi nội dung file hay clipboard; file log
dùng `errorlog.RedactingFormatter` nên traceback không kèm nội dung lỗi. Cùng lúc gắn file log,
`errorlog.install_excepthooks` cài hook ghi ERROR mọi lỗi không bắt được (thread chính, slot Qt,
thread khác), gỡ khi `main` trả về; lỗi lan ra khỏi `main` được ghi trước khi gỡ. Không tạo được
thư mục hay file log thì chỉ ghi WARNING ra stderr (nếu có), app vẫn chạy.

**Một bản mỗi thư mục cấu hình** (`SingleInstance`): bản GUI giữ `<config_dir>/gui.lock` suốt đời
tiến trình (không giữ trước `node.lock` được: trên Windows hai fd cùng tiến trình chặn nhau), rồi
mới `QLocalServer.listen(server_name(config_dir))`. Nhờ giữ `gui.lock` nên được phép
`removeServer` socket cũ (Unix). `acquire` lặp tới `wait` giây: lấy được `gui.lock` (thử trước)
thì mở server; khóa bị giữ mà nối được server thì gửi đúng `b"show\\n"` và trả None (bản kia hiện
cửa sổ); hết hạn → TimeoutError (`main` báo “đang tắt hoặc chưa phản hồi”).
Server coi dữ liệu từ tiến trình khác là không tin cậy: đọc tối đa
`IPC_MAX_BYTES` byte trong `IPC_READ_TIMEOUT_MS`, khớp đúng lệnh thì phát `show_requested`, khác
thì bỏ qua, luôn đóng kết nối; tối đa `IPC_MAX_CONNECTIONS` kết nối cùng lúc. `listen()` lỗi thì
chỉ ghi WARNING (vẫn chỉ một bản nhờ `gui.lock`).

**Thoát** (`GuiApp.request_quit`, từ “Thoát” ở khay, Ctrl+Q, hay đóng cửa sổ khi không có khay):
đóng server IPC (`stop_listening`, `gui.lock` giữ tới `shutdown`), đóng các hộp hỏi và hộp
chọn file (`TransfersController.close_all`), ẩn cửa sổ
và khay, `NodeHost.stop()` (không chặn), `stopped` → `quit()`; kèm
`QTimer` dự phòng `close_time_limit() + QUIT_GRACE` giây. Sau `exec()` (trong `finally`):
`stop()` lần nữa (không sao), đóng `SingleInstance`, `join(JOIN_TIMEOUT)`; thread node vẫn sống
thì ghi log, `logging.shutdown()`, `os._exit(1)`.

**GC chỉ trên thread Qt** (`ui.gcguard.GcGuard`): ngay sau khi có `QApplication` (trừ `--selftest`),
`main` tắt GC tự động và thu gom định kỳ trên thread Qt, để đối tượng PySide trong vòng tham chiếu
không bị hủy ở thread node hay thread đĩa; khôi phục sau `shutdown()`.

Ctrl+C ở cửa sổ dòng lệnh (khi chạy `python -m teleport.ui`): handler SIGINT hẹn luồng Thoát bằng
`QTimer.singleShot(0, …)` (không gọi thẳng giữa chừng một slot Qt); một
`QTimer` rỗng 200 ms cho Python có dịp chạy handler trong lúc Qt giữ vòng lặp.

Khi `sys.stderr is None` (exe windowed) thì không in gì và không lỗi: argparse tự bỏ qua, log
không có handler ra stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import getpass
import hashlib
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import cast

from PySide6.QtCore import QEventLoop, QObject, Qt, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from teleport import paths
from teleport.app import node as app_node
from teleport.app.devices import DeviceView
from teleport.app.node import NodeEvent
from teleport.app.transfers import TransferUpdate
from teleport.lock import NodeLock, NodeLockedError
from teleport.trust.identity import CERT_FILE, Identity
from teleport.ui import errorlog, i18n, send, texts, theme
from teleport.ui.bridge import NodeHost, NodeOptions, StartError
from teleport.ui.gcguard import GcGuard
from teleport.ui.main_window import MainWindow, ViewState
from teleport.ui.pairing import PairingController, repair_offline_text
from teleport.ui.transfers import TransfersController
from teleport.ui.tray import Tray, make_icon

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

GUI_LOCK_FILE = "gui.lock"
SHOW_COMMAND = b"show\n"
IPC_MAX_BYTES = 16
IPC_READ_TIMEOUT_MS = 1000
IPC_MAX_CONNECTIONS = 4
JOIN_TIMEOUT = 5.0  # giây chờ thread node sau `exec()`
QUIT_GRACE = 5.0  # giây cộng thêm `close_time_limit()` cho QTimer dự phòng lúc thoát
LOG_DIR = "logs"
LOG_FILE = "teleport.log"
LOG_MAX_BYTES = 1024 * 1024  # một file log tối đa 1 MiB
LOG_BACKUPS = 1  # giữ thêm một bản cũ: tối đa 2 file

_CONNECT_TIMEOUT_MS = 200
_ACK_TIMEOUT_MS = 1500  # client chờ server đọc lệnh rồi đóng kết nối
_ACQUIRE_RETRY_MS = 100
_SIGNAL_POLL_MS = 200

log = logging.getLogger(__name__)


# ---- Một bản mỗi thư mục cấu hình ----------------------------------------------------------


def server_name(config_dir: Path) -> str:
    """Tên `QLocalServer` của thư mục cấu hình: ổn định, không lộ đường dẫn, riêng mỗi người dùng.

    `"teleport-" + sha256(f"{user}\\0{norm}")[:24]`, `norm = normcase(realpath(config_dir))` (Windows:
    không phân biệt hoa thường, bỏ `\\` cuối). `getpass.getuser()` lỗi thì `user = ""`.
    """
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    norm = os.path.normcase(os.path.realpath(os.fspath(config_dir)))
    digest = hashlib.sha256(f"{user}\0{norm}".encode("utf-8", "surrogatepass")).hexdigest()
    return "teleport-" + digest[:24]


def _send_show(name: str) -> bool:
    """Nối tới bản đang chạy và gửi lệnh `show`; True nếu nối được.

    Gửi xong thì chờ server tự đóng kết nối (tối đa `_ACK_TIMEOUT_MS`) trong một vòng sự kiện con,
    không tự ngắt ngay: đóng ống trước khi server kịp đọc thì Windows bỏ mất dữ liệu. Chờ bằng vòng
    sự kiện (không `waitForDisconnected`) để server cùng tiến trình (test) vẫn chạy được.
    """
    sock = QLocalSocket()
    try:
        sock.connectToServer(name)
        if not sock.waitForConnected(_CONNECT_TIMEOUT_MS):
            return False
        loop = QEventLoop()
        sock.disconnected.connect(loop.quit)
        sock.errorOccurred.connect(loop.quit)
        QTimer.singleShot(_ACK_TIMEOUT_MS, loop.quit)
        sock.write(SHOW_COMMAND)
        sock.flush()
        if sock.state() == QLocalSocket.LocalSocketState.ConnectedState:
            loop.exec()
        return True
    finally:
        sock.abort()
        sock.deleteLater()


def _pause(milliseconds: int) -> None:
    """Chờ mà vẫn xử lý sự kiện Qt (không `time.sleep` trên thread Qt)."""
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


class SingleInstance(QObject):
    """Giữ `gui.lock` và server nhận lệnh `show` của bản GUI đang chạy cho một thư mục cấu hình."""

    show_requested = Signal()

    def __init__(self, lock: NodeLock, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = lock
        self._name = name
        self._server: QLocalServer | None = None
        self._clients: dict[QLocalSocket, bytearray] = {}

    @classmethod
    def acquire(cls, config_dir: Path, wait: float = 3.0) -> SingleInstance | None:
        """Thành bản duy nhất của `config_dir`, hoặc báo bản đang chạy hiện cửa sổ (trả None).

        Mỗi vòng thử lấy `gui.lock` **trước**: được thì `removeServer` + `listen` và trả bản này;
        chỉ khi khóa đang bị giữ mới nối tới server để gửi `show`. Làm ngược lại thì một người dùng
        khác dựng trước pipe/socket cùng tên là đủ làm GUI tưởng đã có bản khác rồi thoát (từ chối
        dịch vụ cục bộ); `gui.lock` nằm trong thư mục cấu hình của chính người dùng nên không giả được.
        Hết `wait` giây mà không được gì → TimeoutError. Không tạo/khóa được `gui.lock` → OSError.
        """
        config_dir = Path(config_dir)
        name = server_name(config_dir)
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            lock = NodeLock(config_dir, GUI_LOCK_FILE)
            try:
                lock.acquire()
            except NodeLockedError:
                pass
            else:
                instance = cls(lock, name)
                instance._listen()
                return instance
            if _send_show(name):
                return None
            if time.monotonic() >= deadline:
                raise TimeoutError("một bản Teleport khác giữ gui.lock nhưng không trả lời")
            _pause(_ACQUIRE_RETRY_MS)

    @property
    def name(self) -> str:
        return self._name

    @property
    def listening(self) -> bool:
        return self._server is not None and self._server.isListening()

    def stop_listening(self) -> None:
        """Đóng server và ngắt các kết nối, vẫn giữ `gui.lock` (đang thoát). Gọi nhiều lần không sao."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            server.deleteLater()
        for sock in list(self._clients):
            self._drop(sock)

    def close(self) -> None:
        """`stop_listening()` rồi nhả `gui.lock`. Gọi nhiều lần không sao."""
        self.stop_listening()
        self._lock.release()

    def _listen(self) -> None:
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        QLocalServer.removeServer(self._name)  # an toàn: đang giữ gui.lock
        if not server.listen(self._name):
            log.warning("không mở được kênh nhận lệnh từ bản Teleport khác: %s", server.errorString())
            server.deleteLater()
            return
        server.setMaxPendingConnections(IPC_MAX_CONNECTIONS)
        server.newConnection.connect(self._on_new_connection)
        self._server = server

    def _on_new_connection(self) -> None:
        """Nhận kết nối chờ tới khi đủ `IPC_MAX_CONNECTIONS`; còn lại chờ trong hàng của server.

        Hàng chờ của `QLocalServer` cũng giới hạn ở `IPC_MAX_CONNECTIONS` (`setMaxPendingConnections`):
        đầy thì bên nối tới không nối được và thử lại sau, nên kết nối im lặng không chiếm chỗ mãi
        (mỗi kết nối đã nhận bị ngắt sau `IPC_READ_TIMEOUT_MS`).
        """
        server = self._server
        while (
            server is not None and len(self._clients) < IPC_MAX_CONNECTIONS and server.hasPendingConnections()
        ):
            sock = server.nextPendingConnection()
            if sock is None:
                break
            sock.setReadBufferSize(IPC_MAX_BYTES + 1)
            self._clients[sock] = bytearray()
            timer = QTimer(sock)
            timer.setSingleShot(True)
            timer.timeout.connect(functools.partial(self._drop, sock))
            timer.start(IPC_READ_TIMEOUT_MS)
            sock.readyRead.connect(functools.partial(self._on_ready_read, sock, False))
            sock.disconnected.connect(functools.partial(self._on_ready_read, sock, True))
            self._on_ready_read(sock, False)

    def _on_ready_read(self, sock: QLocalSocket, final: bool) -> None:
        buffer = self._clients.get(sock)
        if buffer is None:
            return
        buffer += bytes(sock.read(IPC_MAX_BYTES + 1 - len(buffer)).data())
        if len(buffer) > IPC_MAX_BYTES:
            self._drop(sock)
            return
        if not (final or b"\n" in buffer or len(buffer) == IPC_MAX_BYTES):
            return
        matched = bytes(buffer) == SHOW_COMMAND
        self._drop(sock)
        if matched:
            self.show_requested.emit()

    def _drop(self, sock: QLocalSocket) -> None:
        if self._clients.pop(sock, None) is None:
            return
        sock.abort()
        sock.deleteLater()
        if self._server is not None:
            QTimer.singleShot(0, self._on_new_connection)  # nhận kết nối đang chờ trong hàng


# ---- Ráp các phần --------------------------------------------------------------------------


class GuiApp(QObject):
    """Cửa sổ, khay, gửi/nhận file (`transfers`) và node của một tiến trình `teleport-gui`.

    `make_options` (test) dựng `NodeOptions` cho mỗi lần chạy node; mặc định: thư mục nhận
    `paths.default_receive_dir()`, mọi địa chỉ, mDNS quảng bá.
    """

    def __init__(
        self,
        app: QApplication,
        config_dir: Path,
        instance: SingleInstance | None = None,
        *,
        background: bool = False,
        make_options: Callable[[Path], NodeOptions] | None = None,
    ) -> None:
        super().__init__()
        self._app = app
        self._config_dir = config_dir
        self._instance = instance
        self._background = background
        self._make_options = make_options or _default_options
        self._quitting = False
        self._told_tray = False
        self._start_confirmed = False  # người dùng đã bấm “Bắt đầu” trong phiên này
        self.host: NodeHost | None = None
        self._old_hosts: list[NodeHost] = []
        self.window = MainWindow()
        self.tray = Tray(self)
        self.transfers = TransfersController(self.window, self.tray, self)
        self.pairing = PairingController(
            self,
            notify=lambda _title, text: self.tray.show_message(text),
            window_visible=self.window.isVisible,
            message_parent=lambda: self.window,
        )
        self._quit_timer = QTimer(self)  # dự phòng: `quit()` dù node chưa báo `stopped`
        self._quit_timer.setSingleShot(True)
        self._quit_timer.timeout.connect(self._app.quit)

        self.window.retry_requested.connect(self._on_retry)
        self.window.start_requested.connect(self._on_start_requested)
        self.window.quit_requested.connect(self.request_quit)
        self.window.hidden_to_tray.connect(self._on_hidden_to_tray)
        self.tray.open_requested.connect(self.window.bring_to_front)
        self.tray.quit_requested.connect(self.request_quit)
        radar = self.window.radar
        radar.pair_requested.connect(self.pairing.pair_device)
        radar.manual_address_entered.connect(self.pairing.pair_address)
        radar.unpair_requested.connect(self._on_unpair_requested)
        self.pairing.devices_changed.connect(self._on_devices_changed)
        self.transfers.pair_needed.connect(self._on_pair_needed)
        self.transfers.repair_requested.connect(self._on_repair_requested)
        self.pairing.job_done.connect(self._on_pair_job_done)
        self.pairing.paired.connect(self._on_paired)
        if instance is not None:
            instance.show_requested.connect(self._on_show_requested)

    @property
    def quitting(self) -> bool:
        return self._quitting

    def start(self) -> None:
        """Hiện khay, chạy node (hoặc hiện màn “Bắt đầu”), hiện cửa sổ trừ khi `--background` và có khay."""
        self.tray.show()
        self.start_node()
        if (
            not self._background
            or not Tray.is_available()
            or self.window.view_state is not ViewState.STARTING
        ):
            self.window.bring_to_front()

    def start_node(self) -> None:
        """Dựng một `NodeHost` mới (lần đầu, “Bắt đầu” và “Thử lại”).

        Chưa có `device.crt` và người dùng chưa bấm “Bắt đầu” → màn “Bắt đầu”, không chạy node.
        """
        if self._quitting:
            return
        if self.host is not None:
            if self.host.running or self.window.view_state is ViewState.STARTING:
                return  # node đang chạy hay đang khởi động
            self._old_hosts.append(self.host)  # đã lỗi; thread có thể chưa xong, `shutdown` chờ
            self.host = None
        if not self._start_confirmed and not self._setup_done():
            self.window.show_welcome(planned_device_name())
            return
        self.window.radar.model.clear()
        options = self._make_options(self._config_dir)
        host = NodeHost(options, self)
        host.started.connect(functools.partial(self._on_started, host))
        host.start_failed.connect(functools.partial(self._on_start_failed, host))
        host.transfer_event.connect(functools.partial(self._on_transfer, host))
        host.node_event.connect(functools.partial(self._on_node_event, host))
        self.host = host
        self.transfers.attach(host)
        self.pairing.set_host(host)
        self.window.show_starting()
        host.start()

    def request_quit(self) -> None:
        """Luồng Thoát: dừng node không chặn, `quit()` khi node dừng xong hoặc khi hết hạn dự phòng."""
        if self._quitting:
            return
        self._quitting = True
        if self._instance is not None:
            self._instance.stop_listening()  # bản mở sau không “show” vào app đang tắt
        self.transfers.close_all()
        self.pairing.close_all()
        self.window.hide()
        self.tray.hide()
        self._quit_timer.start(int((app_node.close_time_limit() + QUIT_GRACE) * 1000))
        hosts = self._live_hosts()
        if not hosts:
            QTimer.singleShot(0, self._app.quit)
            return
        for host in hosts:
            host.stopped.connect(self._quit_when_stopped)
            host.stop()

    def shutdown(self) -> None:
        """Sau `exec()`: dừng node, đóng `SingleInstance`, chờ thread node có hạn; kẹt → `os._exit(1)`."""
        self._quit_timer.stop()
        hosts = [h for h in (self.host, *self._old_hosts) if h is not None]
        for host in hosts:
            host.stop()
        self.transfers.close_all()
        self.pairing.close_all()
        if self._instance is not None:
            self._instance.stop_listening()
        deadline = time.monotonic() + JOIN_TIMEOUT
        stuck = [h for h in hosts if not h.join(max(0.0, deadline - time.monotonic()))]
        # Nhả `gui.lock` chỉ sau khi thread node đã nhả `node.lock`: bản mở ngay sau đó không gặp M2
        # vì chính bản đang tắt này. Thread kẹt thì `os._exit` nhả cả hai khóa.
        if self._instance is not None:
            self._instance.close()
        self.tray.dispose()
        if stuck:
            log.error("thread node không dừng sau %.0f s, thoát cứng", JOIN_TIMEOUT)
            logging.shutdown()
            os._exit(EXIT_ERROR)

    # ---- Slot ----------------------------------------------------------------------------------

    def _setup_done(self) -> bool:
        try:
            return (self._config_dir / CERT_FILE).is_file()
        except OSError:
            return False

    def _live_hosts(self) -> list[NodeHost]:
        return [h for h in (self.host, *self._old_hosts) if h is not None and not h.is_stopped]

    def _quit_when_stopped(self) -> None:
        if not self._live_hosts():
            self._app.quit()

    def _on_started(self, host: NodeHost) -> None:
        if host is not self.host:
            return
        self.window.show_running(host.node_name, host.options.receive_dir)
        host.call(lambda node: node.devices(), functools.partial(self._on_devices, host))
        self.transfers.on_started(host)

    def _on_devices(self, host: NodeHost, result: tuple[DeviceView, ...] | BaseException) -> None:
        """Ảnh chụp `Node.devices()` lúc `started` (sự kiện tới trước đó đã nằm trong ảnh chụp)."""
        if host is not self.host or self._quitting:
            return
        if isinstance(result, BaseException):
            log.warning("không đọc được danh sách thiết bị: %s", type(result).__name__)
            return
        self.window.radar.model.reset(result)

    def _on_devices_changed(self, views: tuple[DeviceView, ...]) -> None:
        """Danh sách đọc lại sau ghép đôi / hủy ghép đôi (kho tin cậy đổi không phát NodeEvent)."""
        if not self._quitting:
            self.window.radar.model.reset(views)

    def _on_pair_needed(self, view: DeviceView) -> None:
        """“Ghép rồi gửi”: không bắt đầu được (offline, đang bận) thì bỏ ngay việc chờ gửi."""
        busy = self.pairing.busy  # bận thì `pair_device` tự báo câu “đang bận”
        if not self.pairing.pair_device(view):
            self.transfers.on_pairing_finished(view.device_id, False)
            if not busy:
                self.transfers.view.show_notice(
                    send.not_paired_message(texts.peer_label(view.name, view.device_id))
                )

    def _on_repair_requested(self, view: DeviceView) -> None:
        """Nút “Ghép đôi lại”: radar không thấy máy kia (mDNS bị chặn) thì mời nhập IP.

        Bận thì `pair_device` tự báo câu “đang bận”; chưa có host thì không làm gì. Không điền sẵn
        IP (địa chỉ cũ có thể đã thuộc máy khác).
        """
        busy = self.pairing.busy
        if self.pairing.pair_device(view) or busy or self.host is None or self._quitting:
            return
        self.transfers.view.show_notice(repair_offline_text(texts.peer_label(view.name, view.device_id)))
        self.window.radar.open_address_entry()

    def _on_pair_job_done(self, device_id: str | None, paired: bool) -> None:
        """Việc ghép chủ động kết thúc; PAIRED thì chờ `paired` (sau khi đã đọc lại `devices()`)."""
        if not paired:
            self.transfers.on_pairing_finished(device_id, False)

    def _on_paired(self, device_id: str) -> None:
        self.transfers.on_pairing_finished(device_id, True)

    def _on_unpair_requested(self, device_id: str) -> None:
        view = self.window.radar.model.device(device_id)
        self.pairing.request_unpair(device_id, None if view is None else view.name)

    def _on_node_event(self, host: NodeHost, event: NodeEvent) -> None:
        if host is self.host and not self._quitting:
            self.window.radar.model.apply(event)
            self.transfers.on_node_event(event)

    def _on_start_failed(self, host: NodeHost, code: str, detail: str) -> None:
        if host is not self.host:
            return
        if code != StartError.LOCKED:
            log.warning("node không khởi động được: %s", code)
        self.window.show_error(code, detail)
        if not self._quitting:
            self.window.bring_to_front()  # kể cả khi `--background`: người dùng phải biết

    def _on_transfer(self, host: NodeHost, update: TransferUpdate) -> None:
        if host is self.host and not self._quitting:
            self.transfers.on_transfer(update)

    def _on_retry(self) -> None:
        if self.window.view_state is ViewState.ERROR:
            self.start_node()

    def _on_start_requested(self) -> None:
        """Nút “Bắt đầu”: người dùng đồng ý chạy Teleport; `Node.start` tự tạo danh tính."""
        if self.window.view_state is ViewState.WELCOME:
            self._start_confirmed = True
            self.start_node()

    def _on_show_requested(self) -> None:
        """Mở Teleport lần nữa: hiện cửa sổ; đang lỗi thì thử chạy lại (màn “Bắt đầu” vẫn chờ bấm)."""
        if self._quitting:
            return
        self.window.bring_to_front()
        if self.window.view_state is ViewState.ERROR:
            self.start_node()

    def _on_hidden_to_tray(self) -> None:
        if not self._told_tray:
            self._told_tray = True
            self.tray.show_message(texts.still_running())


def _default_options(config_dir: Path) -> NodeOptions:
    return NodeOptions(config_dir=config_dir, receive_dir=paths.default_receive_dir())


class _NoIdentityYet:
    """Đứng thay `Identity` khi chưa có danh tính: mã máy chưa biết."""

    device_id = "????"


def planned_device_name() -> str:
    """Tên node sẽ dùng khi `Node.start` tạo danh tính: `app.node.default_device_name`.

    Hostname dùng được (gần như luôn vậy) thì đúng là tên đó; không thì app dùng `Teleport-<4 ký tự
    đầu mã máy>`, mà mã máy chưa có nên hiện `Teleport-????`. Không I/O đĩa hay mạng.

    Chạy trên thread Qt: `platform.node()` lần đầu có thể mất vài trăm ms (đo được 50–265 ms trên
    Windows), chỉ một lần lúc mở màn “Bắt đầu”; chấp nhận được.
    """
    return app_node.default_device_name(cast(Identity, _NoIdentityYet()))


# ---- main ----------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="teleport-gui", description="Teleport: gửi file trong mạng LAN.")
    parser.add_argument(
        "--config-dir",
        metavar="D",
        help="thư mục cấu hình (mặc định: TELEPORT_HOME hoặc thư mục của hệ điều hành)",
    )
    parser.add_argument(
        "--background", action="store_true", help="chỉ hiện ở khay hệ thống (khi khởi động cùng máy)"
    )
    parser.add_argument(
        "--selftest", action="store_true", help="dựng giao diện rồi thoát (kiểm tra bản build)"
    )
    return parser


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _setup_logging() -> None:
    if sys.stderr is not None and not logging.getLogger().handlers:
        logging.basicConfig(level=logging.WARNING, format=_LOG_FORMAT)


def _attach_file_log(config_dir: Path) -> logging.Handler | None:
    """Gắn log ra `<config_dir>/logs/teleport.log` (xoay 2 file) vào logger gốc; lỗi → None.

    Chỉ gọi khi đã là bản duy nhất của `config_dir` (giữ `gui.lock`). Mở file lúc ghi dòng đầu tiên
    (`delay=True`), nên chạy bình thường không có lỗi thì không tạo file.
    """
    try:
        directory = config_dir / LOG_DIR
        directory.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            directory / LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
            delay=True,
        )
    except OSError as e:
        log.warning("không mở được file log (%s)", type(e).__name__)
        return None
    handler.setLevel(logging.WARNING)
    handler.setFormatter(errorlog.RedactingFormatter(_LOG_FORMAT))
    logging.getLogger().addHandler(handler)
    return handler


def _detach_file_log(handler: logging.Handler | None) -> None:
    if handler is not None:
        logging.getLogger().removeHandler(handler)
        handler.close()


def _application() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return cast(QApplication, existing)
    return QApplication([sys.argv[0] if sys.argv and sys.argv[0] else "teleport-gui"])


def _selftest(app: QApplication) -> int:
    """Dựng i18n, theme, cửa sổ, menu khay, danh sách lượt mà không hiện; không lấy khóa, không mở mạng."""
    try:
        i18n.install(app)
        theme.apply(app)
        window = MainWindow()
        tray = Tray()
        window.show_welcome(planned_device_name())
        window.show_starting()
        window.show_running("Teleport", Path("Teleport"))
        window.radar.model.reset(())
        for code in StartError:
            window.show_error(code.value, "")
        transfers = TransfersController(window, tray)
        transfers.attach(None)
        if len(tray.actions()) != 4 or transfers.offers.keys() or transfers.view.rows():
            return EXIT_ERROR
        app.processEvents()
        tray.dispose()
        window.deleteLater()
        app.processEvents()
        return EXIT_OK
    except Exception:
        log.exception("selftest lỗi")
        return EXIT_ERROR


def _error_box(head: str, hints: list[str]) -> None:
    """Hộp báo lỗi lúc khởi động (trước khi có cửa sổ chính): không cha, chữ thuần."""
    box = QMessageBox(QMessageBox.Icon.Critical, "Teleport", "\n\n".join([head, *hints]))
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.exec()


def _install_sigint(controller: GuiApp) -> Callable[[], None]:
    """Ctrl+C → luồng Thoát. Trả hàm khôi phục handler cũ."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = signal.getsignal(signal.SIGINT)

    def handler(signum: int, frame: FrameType | None) -> None:
        QTimer.singleShot(0, controller.request_quit)

    try:
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        return lambda: None
    poll = QTimer(controller)
    poll.timeout.connect(lambda: None)  # cho Python có dịp chạy handler tín hiệu
    poll.start(_SIGNAL_POLL_MS)

    def restore() -> None:
        poll.stop()
        with contextlib.suppress(ValueError, OSError, TypeError):
            signal.signal(signal.SIGINT, previous)

    return restore


def main(argv: list[str] | None = None) -> int:
    """`teleport-gui`: trả mã thoát (0 bình thường, 1 lỗi, 2 sai tham số)."""
    _setup_logging()
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parser().parse_args(raw)
    except SystemExit as e:  # argparse: sai tham số (2) hoặc --help (0)
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    config_dir = Path(os.path.abspath(args.config_dir if args.config_dir else paths.config_dir()))

    app = _application()
    app.setApplicationName("Teleport")
    app.setWindowIcon(make_icon())
    app.setQuitOnLastWindowClosed(False)
    if args.selftest:
        return _selftest(app)

    # GC vòng tham chiếu chỉ chạy trên thread Qt (`ui.gcguard`): bật trước khi có node, `SingleInstance`
    # và widget; tắt sau `controller.shutdown()` (đã `join` thread node).
    gc_guard = GcGuard()
    gc_guard.start()
    try:
        return _run(app, args, config_dir)
    finally:
        gc_guard.stop()


def _run(app: QApplication, args: argparse.Namespace, config_dir: Path) -> int:
    """Phần của `main` sau khi có `QApplication` (ngoài `--selftest`), chạy trong `GcGuard`."""
    i18n.install(app)
    theme.apply(app)
    try:
        instance = SingleInstance.acquire(config_dir)
    except TimeoutError:  # trước OSError: TimeoutError là lớp con của OSError
        log.error("một bản Teleport khác đang chạy với %s nhưng không trả lời", config_dir)
        _error_box(*texts.not_responding())
        return EXIT_ERROR
    except OSError as e:
        _error_box(*texts.config_dir_unusable_at_launch(f"{config_dir}\n{e.strerror or e}"))
        return EXIT_ERROR
    if instance is None:
        return EXIT_OK  # bản đang chạy đã hiện cửa sổ

    file_log = _attach_file_log(config_dir)  # sau `acquire`: chỉ bản duy nhất ghi và xoay file
    restore_excepthooks = errorlog.install_excepthooks()
    try:
        controller = GuiApp(app, config_dir, instance, background=args.background)
        restore_sigint = _install_sigint(controller)
        try:
            controller.start()
            app.exec()
        finally:
            restore_sigint()
            controller.shutdown()
    except Exception as e:  # ghi trước khi gỡ hook và file log; lỗi vẫn lan ra như cũ
        errorlog.log_uncaught(type(e), e, e.__traceback__)
        raise
    finally:
        restore_excepthooks()
        _detach_file_log(file_log)
    return EXIT_OK
