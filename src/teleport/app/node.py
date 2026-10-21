"""Node Teleport: một thư mục cấu hình, một cổng, dịch vụ ghép đôi và các kết nối điều khiển.

Vòng đời và mô hình tiến trình ở docs/architecture.md mục 2. `Node` giữ `node.lock`
từ `start()` tới `close()`, nạp danh tính và kho tin cậy, mở `Listener` trên một cổng
(47474, hoặc cổng dự phòng lưu trong `settings.json`), rồi giao kết nối tới:

- ghép đôi → `PairingService.handle_incoming`;
- điều khiển → `ControlChannel` (ping/pong), tối đa **một** kết nối cho mỗi thiết bị:
  kết nối mới thay kết nối cũ, kết nối cũ bị đóng.

`unpair` chạy đồng bộ từ lúc xóa khỏi kho tới lúc lấy kết nối của máy đó ra khỏi bảng
(không có `await` xen giữa), rồi đóng kết nối đó. Ghi `trusted.json` / `settings.json`
chạy đồng bộ trên event loop: file nhỏ, và nhờ vậy trạng thái trong RAM đổi cùng lúc
với file.

Chưa có ở đây (PR-4): mDNS, tự mở và giữ kết nối điều khiển, chống trùng protocol §6.1.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import platform
import re
import ssl
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Self

import teleport
from teleport.app.devices import DeviceNotFound, resolve_trusted
from teleport.app.pairing import PairingFinished, PairingPrompt, PairingService
from teleport.app.settings import Settings
from teleport.core.messages import PROTOCOL_VERSION, ConnectionKind, Hello, validate_device_name, validate_os
from teleport.lock import NodeLock
from teleport.net import connection as net_connection
from teleport.net import discovery as net_discovery
from teleport.net.connection import DEFAULT_PORT, Connection, ControlChannel, Listener
from teleport.trust.identity import Identity, load_or_create_identity
from teleport.trust.store import TrustedDevice, TrustStore

LOCK_TIMEOUT = 2.0  # chờ node.lock (khóa của tiến trình vừa chết có thể chưa được nhả, nhất là Windows)
UNPAIR_CLOSE_TIMEOUT = 1.0  # unpair chờ đóng kết nối của máy bị hủy
# DEFAULT_PORT import từ teleport.net.connection; Node đọc qua biến toàn cục của module này
# (test patch `teleport.app.node.DEFAULT_PORT`).

_ANY_HOST = "0.0.0.0"
_TASK_NAME = "teleport.node"
_OS_NAMES = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}
_OS_STRIP_RE = re.compile(r"[^a-z0-9_]")
_LOCAL_SUFFIX = ".local"

log = logging.getLogger(__name__)


def device_os() -> str:
    """Tên hệ điều hành cho `hello.os`: `windows`, `macos`, `linux`, hoặc tên khác đã làm sạch."""
    system = platform.system()
    value = _OS_NAMES.get(system)
    if value is None:
        value = _OS_STRIP_RE.sub("", system.lower())[:16] or "unknown"
    return validate_os(value)


def default_device_name(identity: Identity) -> str:
    """Tên thiết bị mặc định: hostname (bỏ đuôi `.local`), không dùng được thì `Teleport-<4 ký tự mã>`."""
    raw = platform.node().strip()
    if raw[-len(_LOCAL_SUFFIX) :].lower() == _LOCAL_SUFFIX:
        raw = raw[: -len(_LOCAL_SUFFIX)]
    try:
        return validate_device_name(raw)
    except ValueError:
        return f"Teleport-{identity.device_id[:4]}"


class ConfigDirError(Exception):
    """Không dùng được thư mục cấu hình (tạo thư mục, lấy khóa, đọc/ghi file); `__cause__` là OSError gốc."""

    def __init__(self, path: Path, error: OSError) -> None:
        super().__init__(f"{path}: {error}")
        self.path = path
        self.error = error


def unpair_offline(config_dir: Path, query: str, *, lock_timeout: float | None = None) -> TrustedDevice:
    """Hủy ghép đôi khi không có node nào chạy: giữ `node.lock`, xóa khỏi `trusted.json`.

    `query` là tên hoặc tiền tố mã thiết bị (`resolve_trusted`). Thư mục cấu hình chưa
    có → DeviceNotFound ngay, không tạo gì. Lỗi bay ra nguyên trạng: NodeLockedError
    (đang có node chạy), TrustStoreError, DeviceNotFound, AmbiguousDevice, OSError
    (không tạo/khóa được, ví dụ đường dẫn là một file).
    """
    config_dir = Path(config_dir)
    if not config_dir.exists():
        raise DeviceNotFound(query, ())
    lock = NodeLock(config_dir)
    lock.acquire(timeout=LOCK_TIMEOUT if lock_timeout is None else lock_timeout)
    try:
        store = TrustStore.load(config_dir)
        device = resolve_trusted(query, store.devices())
        return store.remove(device.device_id)
    finally:
        lock.release()


def _ignore(_: object) -> None:
    return None


class Node:
    """Một node Teleport trên một thư mục cấu hình. Dùng một lần: `start()`, rồi `close()`."""

    def __init__(
        self,
        config_dir: Path,
        *,
        name: str | None = None,
        port: int | None = None,
        host: str = _ANY_HOST,
        accept_pairing: bool = False,
        on_pairing_prompt: Callable[[PairingPrompt], None] | None = None,
        on_pairing_finished: Callable[[PairingFinished], None] | None = None,
    ) -> None:
        """`name` sai → MessageError (lớp con ValueError); `port` không phải None hay 0–65535 → ValueError.

        `name=None`: hostname. `port=None`: cổng đã lưu (hoặc 47474), bận thì để hệ điều
        hành chọn và lưu lại; `port` là số: đúng cổng đó, không dự phòng, không lưu.
        `host` được kiểm lúc `start()`.
        """
        if name is not None:
            validate_device_name(name)
        if port is not None and (type(port) is not int or not 0 <= port <= 65535):
            raise ValueError("port phải là None hoặc số nguyên 0–65535")
        if type(accept_pairing) is not bool:
            raise ValueError("accept_pairing phải là bool")
        self._config_dir = Path(config_dir)
        self._name_arg = name
        self._port_arg = port
        self._host = host
        self._accept_pairing = accept_pairing
        self._on_pairing_prompt = on_pairing_prompt or _ignore
        self._on_pairing_finished = on_pairing_finished or _ignore

        self._started = False  # start() đã được gọi (thành công hay không)
        self._running = False  # start() thành công, close() chưa xong
        self._closing = False
        self._close_done: asyncio.Event | None = None
        self._lock: NodeLock | None = None
        self._identity: Identity | None = None
        self._store: TrustStore | None = None
        self._hello: Hello | None = None
        self._pairing: PairingService | None = None
        self._listener: Listener | None = None
        self._controls: dict[str, Connection] = {}  # device_id → kết nối điều khiển đang mở
        self._background: set[asyncio.Task] = set()

    # ---- Vòng đời --------------------------------------------------------------------------

    async def start(self) -> None:
        """Lấy `node.lock`, nạp danh tính và kho tin cậy, mở cổng.

        Lỗi bay ra sau khi đã dọn (đóng cổng, nhả khóa): NodeLockedError, IdentityError,
        TrustStoreError, ConfigDirError (OSError khi lấy khóa, nạp danh tính hay kho tin
        cậy), OSError (không mở được cổng), ValueError (`host` sai). Gọi lần hai, sau khi
        `start` thất bại hoặc sau `close()` → RuntimeError.
        """
        if self._started or self._close_done is not None:
            raise RuntimeError("Node chỉ start được một lần")
        self._started = True
        lock = NodeLock(self._config_dir)
        try:
            lock.acquire(timeout=LOCK_TIMEOUT)
        except OSError as e:
            raise ConfigDirError(self._config_dir, e) from e
        listener: Listener | None = None
        try:
            try:
                identity = load_or_create_identity(self._config_dir)
                store = TrustStore.load(self._config_dir)
            except OSError as e:
                raise ConfigDirError(self._config_dir, e) from e
            name = self._name_arg or default_device_name(identity)
            hello = Hello(PROTOCOL_VERSION, identity.device_id, name, teleport.__version__, device_os())
            pairing = PairingService(
                identity=identity,
                hello=hello,
                store=store,
                on_prompt=self._on_pairing_prompt,
                on_finished=self._on_pairing_finished,
                on_trust_changed=self._trust_changed,
                accepting=self._accept_pairing,
            )
            listener = Listener(identity, hello, store.cert_pems(), store.is_trusted, self._on_connection)
            self._identity, self._store, self._hello = identity, store, hello
            self._pairing, self._listener = pairing, listener
            await self._listen(listener)
            if self._close_done is not None:
                raise RuntimeError("Node đã bị đóng trong lúc khởi động")
        except BaseException:
            if listener is not None:
                await listener.close()
            self._identity = self._store = self._hello = None
            self._pairing = self._listener = None
            lock.release()
            raise
        self._lock = lock
        self._running = True
        log.info("node chạy trên cổng %d, mã máy %s", listener.port, identity.device_id[:8])

    async def _listen(self, listener: Listener) -> None:
        """Mở cổng: cổng chỉ định, hoặc cổng ưu tiên rồi dự phòng (protocol §2.1)."""
        if self._port_arg is not None:
            await listener.start(self._host, self._port_arg)
            return
        settings = Settings.load(self._config_dir)
        preferred = settings.port or DEFAULT_PORT
        try:
            await listener.start(self._host, preferred)
            return
        except OSError as e:
            # Mọi OSError: Windows báo WSAEACCES khi cổng nằm trong dải Hyper-V/WSL giữ chỗ.
            log.warning(
                "không mở được cổng %d (errno %s), để hệ điều hành chọn cổng khác", preferred, e.errno
            )
        await listener.start(self._host, 0)
        try:
            dataclasses.replace(settings, port=listener.port).save(self._config_dir)
        except (OSError, ValueError, TypeError, RecursionError) as e:
            # ValueError/TypeError/RecursionError: trường lạ giữ lại từ file (ví dụ JSON lồng
            # quá sâu, đọc được nhưng không ghi lại được) — node vẫn chạy, chỉ không lưu cổng.
            log.warning("không lưu được cổng dự phòng vào settings.json (%s)", type(e).__name__)

    async def close(self) -> None:
        """Hủy các phiên ghép đôi, đóng cổng và mọi kết nối, nhả `node.lock`.

        Gọi nhiều lần hay đồng thời đều được; trước `start` hay sau khi `start` thất bại
        thì không làm gì. Bị hủy giữa chừng thì vẫn ngừng nhận kết nối và hủy mọi kết nối
        trước khi nhả khóa (phần chờ kết nối đóng xong chạy nền, có giới hạn), rồi để
        CancelledError bay ra.
        """
        if self._close_done is not None:
            await self._close_done.wait()
            return
        self._close_done = asyncio.Event()
        if not self._running:
            self._close_done.set()
            return
        self._closing = True
        assert self._pairing is not None and self._listener is not None
        listener_closing = False
        try:
            await self._pairing.close()  # phiên đang chạy gửi pair_abort{user}
            listener_closing = True
            await self._listener.close()  # hủy handler còn lại, đóng kết nối
            await self._drain_background()
        except BaseException:
            for task in list(self._background):
                task.cancel()  # Connection.close() bị hủy thì abort
            if not listener_closing:
                self._stop_listener_now()
            raise
        finally:
            self._running = False
            if self._lock is not None:
                self._lock.release()
            self._close_done.set()

    def _stop_listener_now(self) -> None:
        """`close()` bị hủy trước khi kịp đóng cổng: đóng ngay, không chờ.

        Bước đầu của `Listener.close()` (đóng socket nghe, hủy task của mọi kết nối) không
        có `await` nào, nên chạy nó bằng task khởi động ngay (eager) là xong đồng bộ, trước
        khi nhả khóa. Phần còn lại (chờ các kết nối đóng, có giới hạn) chạy nền.
        """
        assert self._listener is not None
        loop = asyncio.get_running_loop()
        task = asyncio.eager_task_factory(loop, self._listener.close(), name=f"{_TASK_NAME}.close")
        if not task.done():
            self._background.add(task)  # giữ tham chiếu tới khi xong
            task.add_done_callback(self._background.discard)

    async def _drain_background(self) -> None:
        """Chờ các task nền (đóng kết nối cũ) tối đa CLOSE_TIMEOUT + 1; còn lại thì hủy."""
        tasks = set(self._background)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=net_connection.CLOSE_TIMEOUT + 1)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ---- Thuộc tính ------------------------------------------------------------------------

    def _need(self, value: Any) -> Any:
        if value is None:
            raise RuntimeError("node chưa chạy")
        return value

    @property
    def identity(self) -> Identity:
        return self._need(self._identity)

    @property
    def store(self) -> TrustStore:
        return self._need(self._store)

    @property
    def hello(self) -> Hello:
        return self._need(self._hello)

    @property
    def pairing(self) -> PairingService:
        return self._need(self._pairing)

    @property
    def name(self) -> str:
        return self.hello.name

    @property
    def port(self) -> int | None:
        """Cổng đang lắng nghe; None trước `start()`."""
        return None if self._listener is None else self._listener.port

    @property
    def accept_pairing(self) -> bool:
        """Có nhận yêu cầu ghép đôi tới không; đổi lúc chạy có hiệu lực với yêu cầu kế tiếp."""
        return self.pairing.accepting

    @accept_pairing.setter
    def accept_pairing(self, value: bool) -> None:
        self.pairing.accepting = value

    def connected_devices(self) -> frozenset[str]:
        """`device_id` của các thiết bị đang có kết nối điều khiển mở."""
        return frozenset(self._controls)

    async def local_addresses(self) -> tuple[str, ...]:
        """Địa chỉ IPv4 của máy này để người dùng nhập ở máy kia; lỗi → `()`."""
        if self._host != _ANY_HOST:
            return (self._host,)
        try:
            return tuple(await asyncio.to_thread(net_discovery.local_ipv4_addresses))
        except Exception as e:
            log.warning("không đọc được địa chỉ IPv4 của máy này (%s)", type(e).__name__)
            return ()

    # ---- Hủy ghép đôi ----------------------------------------------------------------------

    async def unpair(self, device_id: str) -> TrustedDevice:
        """Hủy ghép đôi: xóa khỏi kho, đổi danh sách tin cậy của cổng, đóng kết nối của máy đó.

        Không có `device_id` → KeyError; không ghi được → TrustStoreError (không đổi gì).
        Khi trả về, máy đó không còn trong `connected_devices()` và kết nối của nó đã bắt
        đầu đóng (chờ tối đa UNPAIR_CLOSE_TIMEOUT). Node chưa chạy → RuntimeError.
        """
        if not self._running:
            raise RuntimeError("node chưa chạy")
        # Đồng bộ từ đây tới lúc lấy kết nối ra khỏi bảng: không có await xen giữa.
        device = self.store.remove(device_id)
        self._trust_changed()
        conn = self._controls.pop(device.device_id, None)
        if conn is not None:
            task = self._spawn(conn.close(), f"{_TASK_NAME}.unpair-close")
            await asyncio.wait({task}, timeout=UNPAIR_CLOSE_TIMEOUT)
        log.info("đã hủy ghép đôi với %s", device.device_id[:8])
        return device

    # ---- Kết nối ---------------------------------------------------------------------------

    def _trust_changed(self) -> None:
        """Kho tin cậy vừa đổi: kết nối mới dùng danh sách mới.

        Lỗi chỉ ghi log: máy đã bị xóa vẫn không vào được vì `Listener` gọi lại
        `is_trusted` (đọc kho trong RAM) mỗi khi nhận `control_open`.
        """
        if self._listener is None or self._store is None:
            return
        try:
            self._listener.update_trusted(self._store.cert_pems())
        except (ssl.SSLError, ValueError):
            log.exception("không cập nhật được danh sách tin cậy của cổng")

    async def _on_connection(self, conn: Connection) -> None:
        if conn.kind is ConnectionKind.PAIRING:
            await self.pairing.handle_incoming(conn)
        elif conn.kind is ConnectionKind.CONTROL:
            await self._serve_control(conn)

    async def _serve_control(self, conn: Connection) -> None:
        """Chạy một kết nối điều khiển; thay kết nối cũ của cùng thiết bị."""
        # Đăng ký trước mọi await (unpair dựa vào điều này).
        if self._closing:
            return  # Listener sẽ đóng kết nối
        assert conn.peer.cert is not None
        device_id = conn.peer.cert.device_id
        old = self._controls.get(device_id)
        self._controls[device_id] = conn
        if old is not None:
            log.debug("thiết bị %s mở kết nối điều khiển mới, đóng kết nối cũ", device_id[:8])
            self._spawn(old.close(), f"{_TASK_NAME}.close-old")
        try:
            reason = await ControlChannel(conn).run(_ignore)
            log.debug("kết nối điều khiển với %s kết thúc: %s", device_id[:8], reason.value)
        finally:
            if self._controls.get(device_id) is conn:
                del self._controls[device_id]

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task
