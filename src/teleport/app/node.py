"""Node Teleport: một thư mục cấu hình, một cổng, ghép đôi, mDNS và các kết nối điều khiển.

Vòng đời và mô hình tiến trình ở docs/architecture.md mục 2. `Node` giữ `node.lock`
từ `start()` tới `close()`, nạp danh tính và kho tin cậy, mở `Listener` trên một cổng
(47474, hoặc cổng dự phòng lưu trong `settings.json`), rồi giao kết nối tới:

- ghép đôi → `PairingService.handle_incoming`;
- điều khiển → `ControlChannel` (ping/pong).

Kết nối điều khiển (cả vào lẫn ra) do `core.links.LinkTracker` quyết định: tối đa
**một** kết nối cho mỗi thiết bị (chống trùng protocol §6.1), tự mở tới thiết bị tin
cậy thấy qua mDNS và nối lại theo lịch §6.2. Node chỉ thực hiện hành động tracker trả
về. `DeviceRegistry` gộp mDNS + `trusted.json` + trạng thái kết nối thành `DeviceView`
cho giao diện, và `on_event` báo `NodeEvent` (thiết bị lên/xuống, kết nối được/mất/lỗi).

mDNS là tùy chọn (`mdns=None`: tắt) và chỉ là gợi ý địa chỉ: kết nối luôn so fingerprint
đã lưu. Lỗi mDNS không làm `start()` thất bại. Khi quảng bá, node cứ
NETWORK_CHECK_INTERVAL giây đọc địa chỉ và đồng hồ (`core.discovery.NetworkWatch`):
địa chỉ đổi → khởi động lại mDNS; máy vừa thức dậy → quảng bá lại.

`unpair` chạy đồng bộ từ lúc xóa khỏi kho tới lúc lấy kết nối của máy đó ra khỏi bảng
(không có `await` xen giữa), rồi đóng kết nối đó. Kết nối (vào hay ra) được đăng ký
trước mọi `await`. Ghi `trusted.json` / `settings.json` chạy đồng bộ trên event loop:
file nhỏ, và nhờ vậy trạng thái trong RAM đổi cùng lúc với file.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import ipaddress
import logging
import math
import platform
import re
import ssl
import time
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import teleport
from teleport.app.devices import DeviceNotFound, DeviceRegistry, DeviceView, resolve_trusted
from teleport.app.pairing import PairingFinished, PairingOutcome, PairingPrompt, PairingService
from teleport.app.settings import Settings
from teleport.core.discovery import (
    PROBE_INTERVAL,
    RESOLVE_TIMEOUT,
    DeviceAdded,
    DeviceEvent,
    DeviceRemoved,
    DeviceUpdated,
    DiscoveredDevice,
    NetworkChange,
    NetworkWatch,
)
from teleport.core.links import (
    CancelConnect,
    CloseConnection,
    Connect,
    ConnectResult,
    LinkAction,
    LinkDown,
    LinkFailed,
    LinkTracker,
    LinkUp,
    Notify,
)
from teleport.core.messages import PROTOCOL_VERSION, ConnectionKind, Hello, validate_device_name, validate_os
from teleport.lock import NodeLock
from teleport.net import connection as net_connection
from teleport.net import discovery as net_discovery
from teleport.net.connection import (
    DEFAULT_PORT,
    ConnectError,
    Connection,
    ControlChannel,
    IdentityMismatch,
    Listener,
    Rejected,
    Unreachable,
    VersionMismatch,
    open_control,
)
from teleport.net.discovery import PROBE_TICK, DiscoveryError, MdnsDiscovery
from teleport.trust.identity import Identity, load_or_create_identity
from teleport.trust.store import TrustedDevice, TrustStore

LOCK_TIMEOUT = 2.0  # chờ node.lock (khóa của tiến trình vừa chết có thể chưa được nhả, nhất là Windows)
UNPAIR_CLOSE_TIMEOUT = 1.0  # unpair chờ đóng kết nối của máy bị hủy
NETWORK_CHECK_INTERVAL = 5.0  # giây giữa hai lần đọc địa chỉ + đồng hồ
SCAN_TIMEOUT = 3.0  # mặc định của scan_devices / teleport list
MAX_SCAN_TIMEOUT = 60.0
_wall_clock = time.time  # test patch để giả máy thức dậy
_mono_clock = time.monotonic
# DEFAULT_PORT import từ teleport.net.connection; Node đọc qua biến toàn cục của module này
# (test patch `teleport.app.node.DEFAULT_PORT`). Tương tự, `MdnsDiscovery`, `open_control`
# và `net_discovery.local_ipv4_addresses` được gọi qua tên toàn cục (test thay được).

_ANY_HOST = "0.0.0.0"
_TASK_NAME = "teleport.node"
_OS_NAMES = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}
_OS_STRIP_RE = re.compile(r"[^a-z0-9_]")
_LOCAL_SUFFIX = ".local"
_MAX_INTERFACES = 16

_CONNECT_RESULTS: tuple[tuple[type[ConnectError], ConnectResult], ...] = (
    (Unreachable, ConnectResult.UNREACHABLE),
    (Rejected, ConnectResult.REJECTED),
    (IdentityMismatch, ConnectResult.IDENTITY_MISMATCH),
    (VersionMismatch, ConnectResult.VERSION_MISMATCH),
)

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


class DiscoveryUnavailable(Exception):
    """Không bật được khám phá mDNS; `__cause__` là lỗi gốc (OSError, DiscoveryError…)."""


# ---- Tùy chọn mDNS -------------------------------------------------------------------------


def _check_ipv4_list(name: str, value: object) -> tuple[str, ...] | None:
    """None, hoặc dãy (không phải chuỗi) 1–16 IPv4 dạng chấm viết chuẩn."""
    if value is None:
        return None
    if isinstance(value, str | bytes) or not isinstance(value, Iterable):
        raise ValueError(f"{name} phải là một dãy địa chỉ IPv4, không phải một chuỗi")
    items = tuple(value)
    if not 1 <= len(items) <= _MAX_INTERFACES:
        raise ValueError(f"{name} phải có 1–{_MAX_INTERFACES} địa chỉ")
    for item in items:
        if not isinstance(item, str):
            raise ValueError(f"{name}: địa chỉ phải là chuỗi IPv4 dạng chấm")
        try:
            canonical = str(ipaddress.IPv4Address(item))
        except ValueError as e:
            raise ValueError(f"{name}: địa chỉ phải là IPv4 dạng chấm: {item!r}") from e
        if canonical != item:
            raise ValueError(f"{name}: địa chỉ phải là IPv4 dạng chấm: {item!r}")
    return items


def _check_seconds(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} phải là số giây")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} phải là số hữu hạn > 0")
    return float(value)


@dataclass(frozen=True, slots=True)
class MdnsOptions:
    """Cách node dùng mDNS (protocol §8). Tham số sai → ValueError / TypeError."""

    advertise: bool = True  # quảng bá máy này (False: chỉ duyệt)
    interfaces: tuple[str, ...] | None = None  # None = mọi card; test: ("127.0.0.1",)
    advertise_addresses: tuple[str, ...] | None = None  # None = tự chọn + theo dõi đổi địa chỉ
    probe_interval: float = PROBE_INTERVAL
    resolve_timeout: float = RESOLVE_TIMEOUT
    probe_tick: float = PROBE_TICK

    def __post_init__(self) -> None:
        if type(self.advertise) is not bool:
            raise TypeError("advertise phải là bool")
        object.__setattr__(self, "interfaces", _check_ipv4_list("interfaces", self.interfaces))
        object.__setattr__(
            self, "advertise_addresses", _check_ipv4_list("advertise_addresses", self.advertise_addresses)
        )
        for name in ("probe_interval", "resolve_timeout", "probe_tick"):
            object.__setattr__(self, name, _check_seconds(name, getattr(self, name)))


# ---- Sự kiện cho giao diện -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceOnline:
    """mDNS vừa thấy một thiết bị."""

    device: DeviceView


@dataclass(frozen=True, slots=True)
class DeviceOffline:
    """Thiết bị rời mạng (mDNS); `device.online` là False, không có địa chỉ, cổng."""

    device: DeviceView


@dataclass(frozen=True, slots=True)
class DeviceConnected:
    """Đã có kết nối điều khiển tới thiết bị (đổi kết nối thì không báo lại)."""

    device: DeviceView
    outgoing: bool  # True: máy này mở


@dataclass(frozen=True, slots=True)
class DeviceDisconnected:
    """Mất kết nối điều khiển tới thiết bị."""

    device: DeviceView


@dataclass(frozen=True, slots=True)
class ConnectFailed:
    """Không kết nối được tới thiết bị đã ghép đôi (mỗi loại lỗi báo một lần, xem `core.links`)."""

    device: DeviceView
    result: ConnectResult  # không bao giờ OK


NodeEvent = DeviceOnline | DeviceOffline | DeviceConnected | DeviceDisconnected | ConnectFailed


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Kết quả `scan_devices` (`teleport list`)."""

    device_id: str  # máy này
    name: str  # default_device_name(identity)
    port: int  # Settings.load().port or DEFAULT_PORT
    devices: tuple[DeviceView, ...]  # thấy qua mDNS + thiết bị tin cậy không thấy; connected luôn False


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


async def scan_devices(
    config_dir: Path, *, timeout: float = SCAN_TIMEOUT, mdns: MdnsOptions | None = None
) -> ScanResult:
    """Duyệt mDNS trong `timeout` giây rồi trả các thiết bị thấy được (`teleport list`).

    Không giữ `node.lock`, không quảng bá máy này (bỏ qua `mdns.advertise`). Tạo danh
    tính nếu chưa có (cần `device_id` để bỏ qua chính máy này). `timeout` không phải số
    hữu hạn trong (0, MAX_SCAN_TIMEOUT] → ValueError. Lỗi: ConfigDirError (OSError của
    thư mục cấu hình), IdentityError, TrustStoreError, DiscoveryUnavailable (không bật
    được mDNS).
    """
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= MAX_SCAN_TIMEOUT
    ):
        raise ValueError(f"timeout phải là số giây trong (0, {MAX_SCAN_TIMEOUT:g}]")
    if mdns is not None and not isinstance(mdns, MdnsOptions):
        raise TypeError("mdns phải là None hoặc MdnsOptions")
    config_dir = Path(config_dir)
    try:
        identity = load_or_create_identity(config_dir)
        store = TrustStore.load(config_dir)
        settings = Settings.load(config_dir)
    except OSError as e:
        raise ConfigDirError(config_dir, e) from e
    opts = mdns or MdnsOptions(advertise=False)
    trusted = store.devices()
    m: MdnsDiscovery | None = None
    try:
        try:
            m = MdnsDiscovery(
                interfaces=opts.interfaces,
                probe_interval=opts.probe_interval,
                resolve_timeout=opts.resolve_timeout,
                probe_tick=opts.probe_tick,
            )
            await m.start()
            m.set_priority([d.device_id for d in trusted])
            await m.start_browsing(self_device_id=identity.device_id, on_event=_ignore)
        except Exception as e:
            raise DiscoveryUnavailable(f"không bật được khám phá mDNS: {e or type(e).__name__}") from e
        await asyncio.sleep(timeout)
        seen = list(m.devices.values())
    finally:
        if m is not None:
            await m.close()
    registry = DeviceRegistry(identity.device_id)
    registry.set_trusted(trusted)
    for device in seen:
        registry.seen(device)
    return ScanResult(
        device_id=identity.device_id,
        name=default_device_name(identity),
        port=settings.port or DEFAULT_PORT,
        devices=registry.views(),
    )


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
        mdns: MdnsOptions | None = None,
        auto_connect: bool = True,
        on_event: Callable[[NodeEvent], None] | None = None,
    ) -> None:
        """`name` sai → MessageError (lớp con ValueError); `port` không phải None hay 0–65535 → ValueError.

        `name=None`: hostname. `port=None`: cổng đã lưu (hoặc 47474), bận thì để hệ điều
        hành chọn và lưu lại; `port` là số: đúng cổng đó, không dự phòng, không lưu.
        `host` được kiểm lúc `start()`. `mdns=None`: không dùng mDNS; khác None mà không
        phải `MdnsOptions` → TypeError. `auto_connect`: tự mở và giữ kết nối điều khiển
        tới thiết bị tin cậy thấy qua mDNS (không phải bool → ValueError). `on_event`
        nhận `NodeEvent`, gọi đồng bộ trên loop; raise thì chỉ ghi log.
        `on_pairing_prompt=None`: node không hiện mã được nên không ghép đôi được;
        khi đó `accept_pairing=True` → ValueError.
        """
        if name is not None:
            validate_device_name(name)
        if port is not None and (type(port) is not int or not 0 <= port <= 65535):
            raise ValueError("port phải là None hoặc số nguyên 0–65535")
        if type(accept_pairing) is not bool:
            raise ValueError("accept_pairing phải là bool")
        if accept_pairing and on_pairing_prompt is None:
            raise ValueError("accept_pairing=True cần on_pairing_prompt để hiện mã ghép đôi")
        if mdns is not None and not isinstance(mdns, MdnsOptions):
            raise TypeError("mdns phải là None hoặc MdnsOptions")
        if type(auto_connect) is not bool:
            raise ValueError("auto_connect phải là bool")
        self._config_dir = Path(config_dir)
        self._name_arg = name
        self._port_arg = port
        self._host = host
        self._accept_pairing = accept_pairing
        self._on_pairing_prompt = on_pairing_prompt  # None: không ghép đôi được (PairingService)
        self._on_pairing_finished = on_pairing_finished or _ignore
        self._mdns_opts = mdns
        self._auto_connect = auto_connect
        self._on_event = on_event or _ignore

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
        self._links: LinkTracker | None = None
        self._registry: DeviceRegistry | None = None
        self._conns: dict[int, Connection] = {}  # conn_id → kết nối điều khiển đang chạy (vào lẫn ra)
        self._next_conn_id = 1
        self._attempts: dict[int, asyncio.Task] = {}  # attempt_id → task của lần thử đang chạy
        self._link_tasks: set[asyncio.Task] = set()  # task `link-out` (thử rồi chạy kết nối ra)
        self._background: set[asyncio.Task] = set()  # task đóng kết nối
        self._links_wake = asyncio.Event()  # bộ hẹn giờ của tracker tính lại `next_wakeup`
        self._timer_deadline: float | None = None  # thời điểm bộ hẹn giờ đang chờ tới
        self._timer_task: asyncio.Task | None = None
        self._netwatch_task: asyncio.Task | None = None
        self._mdns: MdnsDiscovery | None = None
        self._mdns_closing: set[MdnsDiscovery] = set()  # instance cũ đang đóng (khởi động lại)
        self._mdns_lock = asyncio.Lock()
        self._discovery_active = False
        self._discovery_error: Exception | None = None
        self._mdns_error_key: tuple[str, type, str] | None = None  # lỗi mDNS đã ghi WARNING gần nhất

    # ---- Vòng đời --------------------------------------------------------------------------

    async def start(self) -> None:
        """Lấy `node.lock`, nạp danh tính và kho tin cậy, mở cổng, bật mDNS (nếu có).

        Lỗi bay ra sau khi đã dọn (đóng cổng, đóng mDNS, nhả khóa): NodeLockedError,
        IdentityError, TrustStoreError, ConfigDirError (OSError khi lấy khóa, nạp danh
        tính hay kho tin cậy), OSError (không mở được cổng), ValueError (`host` sai). Bị
        hủy lần nữa trong lúc dọn thì cổng vẫn đóng đồng bộ trước khi nhả khóa, còn mDNS
        đóng nền sau khi đã nhả khóa. Lỗi mDNS không làm `start` thất bại (xem
        `discovery_error`). Gọi lần hai, sau khi `start` thất bại hoặc sau `close()` →
        RuntimeError.
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
                on_finished=self._pairing_finished,
                on_trust_changed=self._trust_changed,
                accepting=self._accept_pairing,
            )
            listener = Listener(identity, hello, store.cert_pems(), store.is_trusted, self._on_connection)
            self._identity, self._store, self._hello = identity, store, hello
            self._pairing, self._listener = pairing, listener
            # Tracker có trước khi mở cổng: kết nối vào có thể tới ngay.
            self._registry = DeviceRegistry(identity.device_id)
            self._links = LinkTracker(identity.device_id)
            self._registry.set_trusted(store.devices())
            self._apply(self._links.trusted_changed(self._trusted_ids(), self._now()))
            await self._listen(listener)
            if self._close_done is not None:
                raise RuntimeError("Node đã bị đóng trong lúc khởi động")
            if self._mdns_opts is not None:
                await self._start_mdns()
                if self._close_done is not None:
                    raise RuntimeError("Node đã bị đóng trong lúc khởi động")
            self._timer_task = asyncio.create_task(self._links_timer(), name=f"{_TASK_NAME}.links-timer")
            if self._mdns_opts is not None and self._mdns_opts.advertise:
                self._netwatch_task = asyncio.create_task(self._netwatch(), name=f"{_TASK_NAME}.netwatch")
        except BaseException:
            self._closing = True  # _apply không tạo task mới nữa
            try:
                await self._abort_start(listener)
            finally:
                self._identity = self._store = self._hello = None
                self._pairing = self._listener = None
                self._links = self._registry = None
                lock.release()
            raise
        self._lock = lock
        self._running = True
        log.info("node chạy trên cổng %d, mã máy %s", listener.port, identity.device_id[:8])

    async def _abort_start(self, listener: Listener | None) -> None:
        """Dọn khi `start` thất bại hoặc bị hủy: task nền, cổng, kết nối, mDNS.

        Đóng cổng trước mDNS. Bị hủy lần nữa trong lúc dọn thì làm như nhánh hủy của
        `close()`: đóng cổng ngay (đồng bộ), đóng mDNS chạy nền, rồi để lỗi bay ra — bên
        gọi chỉ nhả khóa sau khi cổng đã đóng.
        """
        tasks = [t for t in (self._timer_task, self._netwatch_task) if t is not None]
        tasks += [*self._attempts.values(), *self._link_tasks]
        for task in tasks:
            task.cancel()
        listener_closing = False
        try:
            if listener is not None:
                listener_closing = True
                await listener.close()
            for m in self._mdns_instances():
                await m.close()
                self._mdns_closing.discard(m)
                if self._mdns is m:
                    self._mdns = None
            await self._drain_background()
        except BaseException:
            for task in self._all_node_tasks():
                task.cancel()
            if listener is not None and not listener_closing:
                self._stop_listener_now(listener)
            for m in self._mdns_instances():
                # Phần đóng socket của MdnsDiscovery.close chạy tới cùng kể cả khi bị hủy.
                self._spawn(m.close(), f"{_TASK_NAME}.close-mdns")
            raise
        finally:
            self._mdns = None
            self._mdns_closing.clear()
            self._discovery_active = False

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
        """Hủy các phiên ghép đôi, gửi goodbye mDNS, đóng cổng và mọi kết nối, nhả `node.lock`.

        Gọi nhiều lần hay đồng thời đều được; trước `start` hay sau khi `start` thất bại
        thì không làm gì. Bị hủy giữa chừng thì vẫn hủy cứng mọi phiên ghép đôi (không
        ghi `trusted.json` nữa), ngừng nhận kết nối và hủy mọi kết nối trước khi nhả khóa
        (phần chờ đóng xong, kể cả mDNS, chạy nền, có giới hạn), rồi để CancelledError bay ra.
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
            await self._stop_tasks([self._timer_task, self._netwatch_task])
            await self._pairing.close()  # phiên đang chạy gửi pair_abort{user}
            for m in self._mdns_instances():
                await m.close()  # goodbye: máy khác xóa máy này ngay (mvp N1)
                self._mdns_closing.discard(m)
            self._mdns = None
            self._discovery_active = False
            for task in list(self._attempts.values()):
                task.cancel()
            listener_closing = True
            await self._listener.close()  # hủy handler còn lại, đóng kết nối vào
            for task in list(self._link_tasks):
                task.cancel()  # kết nối ra: ControlChannel đóng gọn
            await self._drain_background()
        except BaseException:
            self._pairing.abort_all()
            for task in self._all_node_tasks():
                task.cancel()  # Connection.close() bị hủy thì abort
            if not listener_closing:
                self._stop_listener_now(self._listener)
            for m in self._mdns_instances():
                # Phần đóng socket của MdnsDiscovery.close chạy tới cùng kể cả khi bị hủy.
                self._spawn(m.close(), f"{_TASK_NAME}.close-mdns")
            self._mdns = None
            self._mdns_closing.clear()
            self._discovery_active = False
            raise
        finally:
            self._running = False
            if self._lock is not None:
                self._lock.release()
            self._close_done.set()

    def _mdns_instances(self) -> list[MdnsDiscovery]:
        """Mọi instance mDNS chưa đóng xong (đang dùng và đang đóng dở)."""
        instances = list(self._mdns_closing)
        if self._mdns is not None and self._mdns not in self._mdns_closing:
            instances.append(self._mdns)
        return instances

    def _all_node_tasks(self) -> list[asyncio.Task]:
        tasks = [t for t in (self._timer_task, self._netwatch_task) if t is not None]
        return [*tasks, *self._attempts.values(), *self._link_tasks, *self._background]

    @staticmethod
    async def _stop_tasks(tasks: Iterable[asyncio.Task | None]) -> None:
        """Hủy các task và chờ chúng kết thúc."""
        running = [t for t in tasks if t is not None and not t.done()]
        for task in running:
            task.cancel()
        if running:
            await asyncio.wait(running)

    def _stop_listener_now(self, listener: Listener) -> None:
        """`close()` hay `start()` bị hủy trước khi kịp đóng cổng: đóng ngay, không chờ.

        Bước đầu của `Listener.close()` (đóng socket nghe, hủy task của mọi kết nối) không
        có `await` nào, nên chạy nó bằng task khởi động ngay (eager) là xong đồng bộ, trước
        khi nhả khóa. Phần còn lại (chờ các kết nối đóng, có giới hạn) chạy nền.
        """
        loop = asyncio.get_running_loop()
        task = asyncio.eager_task_factory(loop, listener.close(), name=f"{_TASK_NAME}.close")
        if not task.done():
            self._background.add(task)  # giữ tham chiếu tới khi xong
            task.add_done_callback(self._background.discard)

    async def _drain_background(self) -> None:
        """Chờ các task nền (đóng kết nối, kết nối ra) tối đa CLOSE_TIMEOUT + 1; còn lại thì hủy."""
        tasks = self._background | self._link_tasks
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
        """Có nhận yêu cầu ghép đôi tới không; đổi lúc chạy có hiệu lực với yêu cầu kế tiếp.

        Đặt True khi node không có `on_pairing_prompt` → ValueError.
        """
        return self.pairing.accepting

    @accept_pairing.setter
    def accept_pairing(self, value: bool) -> None:
        self.pairing.accepting = value

    @property
    def discovery_active(self) -> bool:
        """Đang duyệt mDNS."""
        self._need(self._registry)
        return self._mdns is not None and self._discovery_active

    @property
    def discovery_error(self) -> Exception | None:
        """Lỗi mDNS gần nhất (không bật được, hoặc không quảng bá được); None khi tắt hoặc đang ổn."""
        self._need(self._registry)
        return self._discovery_error

    def connected_devices(self) -> frozenset[str]:
        """`device_id` của các thiết bị đang có kết nối điều khiển."""
        return frozenset() if self._links is None else frozenset(self._links.connected())

    def devices(self) -> tuple[DeviceView, ...]:
        """Thiết bị đã ghép đôi hoặc đang thấy qua mDNS, sắp theo tên."""
        registry: DeviceRegistry = self._need(self._registry)
        return registry.views()

    def control_connection(self, device_id: str) -> Connection | None:
        """Kết nối điều khiển hiện tại tới thiết bị (chẩn đoán, test)."""
        links: LinkTracker = self._need(self._links)
        conn_id = links.connected().get(device_id)
        return None if conn_id is None else self._conns.get(conn_id)

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
        Khi trả về, máy đó không còn trong `connected_devices()`, lần thử kết nối tới nó
        đã bị hủy, và kết nối của nó đã bắt đầu đóng (chờ tối đa UNPAIR_CLOSE_TIMEOUT).
        Node chưa chạy → RuntimeError.
        """
        if not self._running:
            raise RuntimeError("node chưa chạy")
        # Đồng bộ từ đây tới lúc lấy kết nối ra khỏi bảng: không có await xen giữa.
        device = self.store.remove(device_id)
        closes = self._trust_changed()
        if closes:
            await asyncio.wait(closes, timeout=UNPAIR_CLOSE_TIMEOUT)
        log.info("đã hủy ghép đôi với %s", device.device_id[:8])
        return device

    # ---- Tin cậy, ghép đôi -----------------------------------------------------------------

    def _trusted_ids(self) -> list[str]:
        assert self._store is not None
        return [d.device_id for d in self._store.devices()]

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    def _trust_changed(self) -> list[asyncio.Task]:
        """Kho tin cậy vừa đổi (đồng bộ): cổng, registry, tracker, mDNS dùng danh sách mới.

        Trả các task đóng kết nối của thiết bị thôi tin cậy. Lỗi cập nhật cổng chỉ ghi log:
        máy đã bị xóa vẫn không vào được vì `Listener` gọi lại `is_trusted` (đọc kho
        trong RAM) mỗi khi nhận `control_open`.
        """
        if self._listener is None or self._store is None or self._links is None or self._registry is None:
            return []
        try:
            self._listener.update_trusted(self._store.cert_pems())
        except (ssl.SSLError, ValueError):
            log.exception("không cập nhật được danh sách tin cậy của cổng")
        ids = self._trusted_ids()
        self._registry.set_trusted(self._store.devices())
        closes = self._apply(self._links.trusted_changed(ids, self._now()))
        if self._mdns is not None:
            try:
                self._mdns.set_priority(ids)
            except Exception:
                log.exception("không cập nhật được danh sách ưu tiên mDNS")
        return closes

    def _pairing_finished(self, result: PairingFinished) -> None:
        """Ghép đôi xong: gỡ chặn và thử kết nối ngay (ghép đôi lại), rồi báo người dùng."""
        if (
            result.outcome is PairingOutcome.PAIRED
            and result.peer_device_id is not None
            and self._auto_connect
            and self._links is not None
            and not self._closing
        ):
            try:
                self._apply(self._links.retry_now(result.peer_device_id, self._now()))
            except Exception:
                log.exception("không thử kết nối sau khi ghép đôi")
        self._on_pairing_finished(result)

    # ---- Kết nối điều khiển ----------------------------------------------------------------

    def _new_conn_id(self) -> int:
        conn_id = self._next_conn_id
        self._next_conn_id += 1
        return conn_id

    async def _on_connection(self, conn: Connection) -> None:
        if conn.kind is ConnectionKind.PAIRING:
            await self.pairing.handle_incoming(conn)
        elif conn.kind is ConnectionKind.CONTROL:
            await self._serve_control(conn)

    async def _serve_control(self, conn: Connection) -> None:
        """Kết nối điều khiển vào: đăng ký với tracker (chống trùng), rồi chạy tới khi kết thúc."""
        # Đăng ký trước mọi await (unpair dựa vào điều này).
        if self._closing or self._links is None:
            return  # Listener sẽ đóng kết nối
        assert conn.peer.cert is not None
        device_id = conn.peer.cert.device_id
        conn_id = self._new_conn_id()
        self._conns[conn_id] = conn
        self._apply(self._links.incoming_control(device_id, conn_id, self._now()))
        if conn_id not in self._conns:
            return  # vừa bị luật chống trùng đóng; Listener đóng kết nối (idempotent)
        await self._run_control(device_id, conn_id, conn)

    async def _run_control(self, device_id: str, conn_id: int, conn: Connection) -> None:
        """Chạy `ControlChannel` tới khi kết nối kết thúc rồi báo tracker."""
        try:
            reason = await ControlChannel(conn).run(_ignore)
            log.debug("kết nối điều khiển %d với %s kết thúc: %s", conn_id, device_id[:8], reason.value)
        finally:
            self._conns.pop(conn_id, None)
            # Báo cả khi đang đóng, để `connected_devices()` rỗng sau `close()`; lúc đó
            # `_emit` không phát sự kiện và `_apply` không mở kết nối mới.
            if self._links is not None:
                self._apply(self._links.connection_lost(device_id, conn_id, self._now()))

    async def _link_out(self, action: Connect) -> None:
        """Một lần thử của tracker: mở kết nối điều khiển, báo kết quả, rồi chạy kết nối đó."""
        assert self._store is not None and self._identity is not None and self._hello is not None
        device_id, attempt_id = action.device_id, action.attempt_id
        conn: Connection | None = None
        result = ConnectResult.UNREACHABLE
        device = self._store.get(device_id)
        if device is None:
            log.error("thử kết nối tới %s nhưng thiết bị không có trong kho tin cậy", device_id[:8])
        else:
            try:
                # Gọi trực tiếp (không bọc task): không có khe hở giữa lúc trả về và lúc đăng ký.
                conn = await open_control(
                    list(action.addresses), action.port, self._identity, self._hello, device.fingerprint
                )
            except ConnectError as e:
                for cls, value in _CONNECT_RESULTS:
                    if isinstance(e, cls):
                        result = value
                        break
                log.debug("kết nối điều khiển tới %s thất bại: %s", device_id[:8], result.value)
            except Exception:
                log.exception("lỗi khi mở kết nối điều khiển tới %s", device_id[:8])
        if conn is None:
            self._attempts.pop(attempt_id, None)
            if not self._closing and self._links is not None:
                self._apply(self._links.connect_finished(device_id, attempt_id, result, None, self._now()))
            return
        # Thành công: không await cho tới khi đăng ký xong.
        try:
            self._attempts.pop(attempt_id, None)
            if self._closing or self._links is None:
                return
            conn_id = self._new_conn_id()
            self._conns[conn_id] = conn
            self._apply(
                self._links.connect_finished(device_id, attempt_id, ConnectResult.OK, conn_id, self._now())
            )
            if conn_id not in self._conns:
                return  # lần thử cũ, hoặc luật chống trùng giữ kết nối khác
            log.debug("đã mở kết nối điều khiển %d tới %s", conn_id, device_id[:8])
            await self._run_control(device_id, conn_id, conn)
        finally:
            if not conn.closed:
                await conn.close()

    def _apply(self, actions: list[LinkAction]) -> list[asyncio.Task]:
        """Thực hiện hành động của tracker (đồng bộ); trả các task đóng kết nối vừa tạo."""
        closes: list[asyncio.Task] = []
        for action in actions:
            if isinstance(action, Connect):
                if self._closing:
                    continue
                task = asyncio.create_task(
                    self._link_out(action), name=f"{_TASK_NAME}.link-out {action.device_id[:8]}"
                )
                self._attempts[action.attempt_id] = task
                self._link_tasks.add(task)
                task.add_done_callback(self._link_tasks.discard)
            elif isinstance(action, CancelConnect):
                task = self._attempts.pop(action.attempt_id, None)
                if task is not None:
                    task.cancel()
            elif isinstance(action, CloseConnection):
                conn = self._conns.pop(action.conn_id, None)
                if conn is not None:
                    closes.append(self._spawn(conn.close(), f"{_TASK_NAME}.close {action.device_id[:8]}"))
            elif isinstance(action, Notify):
                self._notify(action.event)
        self._wake_timer()
        return closes

    def _notify(self, event: LinkUp | LinkDown | LinkFailed) -> None:
        assert self._registry is not None
        registry = self._registry
        if isinstance(event, LinkUp):
            registry.set_connected(event.device_id, True)
            log.info("đã kết nối với %s", event.device_id[:8])
            view = registry.get(event.device_id)
            if view is not None:
                self._emit(DeviceConnected(view, event.outgoing))
        elif isinstance(event, LinkDown):
            registry.set_connected(event.device_id, False)
            log.info("mất kết nối với %s", event.device_id[:8])
            view = registry.get(event.device_id)
            if view is not None:
                self._emit(DeviceDisconnected(view))
        else:
            # INFO: giao diện / CLI báo người dùng qua ConnectFailed, không lặp lại trên stderr.
            log.info("không kết nối được tới %s: %s", event.device_id[:8], event.result.value)
            view = registry.get(event.device_id)
            if view is not None:
                self._emit(ConnectFailed(view, event.result))

    def _emit(self, event: NodeEvent) -> None:
        if self._closing:
            return  # node đang đóng: không báo từng kết nối mất
        try:
            self._on_event(event)
        except Exception:
            log.exception("on_event raise")

    def _wake_timer(self) -> None:
        """Báo bộ hẹn giờ khi tracker có lần thử hẹn sớm hơn lúc nó đang chờ tới."""
        if self._links is None:
            return
        wake = self._links.next_wakeup()
        if wake is not None and (self._timer_deadline is None or wake < self._timer_deadline):
            self._links_wake.set()

    async def _links_timer(self) -> None:
        """Gọi `tracker.tick` đúng các lần thử đã hẹn (`next_wakeup`)."""
        assert self._links is not None
        links = self._links
        while True:
            wake = links.next_wakeup()
            self._timer_deadline = wake
            if wake is None:
                await self._links_wake.wait()
            else:
                delay = wake - self._now()
                if delay > 0:
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(delay):
                            await self._links_wake.wait()
            self._links_wake.clear()
            self._apply(links.tick(self._now()))

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # ---- mDNS ------------------------------------------------------------------------------

    def _advertise_args(self) -> dict[str, Any]:
        assert self._identity is not None and self._hello is not None and self._mdns_opts is not None
        return {
            "device_id": self._identity.device_id,
            "name": self._hello.name,
            "os": self._hello.os,
            "port": self.port,
            "addresses": self._mdns_opts.advertise_addresses,
        }

    async def _start_mdns(self) -> None:
        """Bật mDNS (best-effort): duyệt, rồi quảng bá nếu `advertise`. Lỗi → `discovery_error`."""
        opts = self._mdns_opts
        assert opts is not None and self._identity is not None
        m: MdnsDiscovery | None = None
        try:
            m = MdnsDiscovery(
                interfaces=opts.interfaces,
                probe_interval=opts.probe_interval,
                resolve_timeout=opts.resolve_timeout,
                probe_tick=opts.probe_tick,
            )
            self._mdns = m
            await m.start()
            m.set_priority(self._trusted_ids())
            await m.start_browsing(
                self_device_id=self._identity.device_id,
                on_event=functools.partial(self._on_discovery_event, m),
            )
        except Exception as e:
            self._log_mdns_error("không bật được khám phá mDNS", e)
            self._discovery_error = e
            if m is not None:
                await m.close()
            if self._mdns is m:
                self._mdns = None
            self._discovery_active = False
            return
        self._discovery_active = True
        self._discovery_error = None
        if opts.advertise:
            await self._advertise(m)
        else:
            self._mdns_recovered()

    async def _advertise(self, m: MdnsDiscovery) -> None:
        """Quảng bá (hoặc quảng bá lại) máy này; lỗi → `discovery_error`, vẫn duyệt."""
        try:
            await m.advertise(**self._advertise_args())
        except Exception as e:
            self._log_mdns_error("không quảng bá được qua mDNS", e)
            self._discovery_error = e
            return
        self._discovery_error = None
        self._mdns_recovered()

    def _log_mdns_error(self, what: str, error: Exception) -> None:
        """Ghi lỗi mDNS: WARNING lần đầu hoặc khi lỗi đổi; lặp lại (netwatch thử mỗi vài giây) thì DEBUG."""
        key = (what, type(error), str(error))
        if key == self._mdns_error_key:
            log.debug("%s (lặp lại): %s", what, error or type(error).__name__)
            return
        self._mdns_error_key = key
        if isinstance(error, DiscoveryError | OSError):
            log.warning("%s: %s", what, error or type(error).__name__)
        else:
            log.exception("%s", what)

    def _mdns_recovered(self) -> None:
        """mDNS chạy lại bình thường sau một lỗi đã ghi: báo một lần, quên lỗi cũ."""
        if self._mdns_error_key is not None:
            self._mdns_error_key = None
            log.info("khám phá mDNS đã hoạt động trở lại")

    def _on_discovery_event(self, source: MdnsDiscovery, event: DeviceEvent) -> None:
        """Sự kiện của `MdnsDiscovery` (đồng bộ): registry, tracker, rồi `on_event`."""
        if self._closing or source is not self._mdns or self._registry is None or self._links is None:
            return  # instance cũ (sau khi khởi động lại) hoặc node đang đóng
        registry, links = self._registry, self._links
        now = self._now()
        if isinstance(event, DeviceAdded | DeviceUpdated):
            device = event.device
            registry.seen(device)
            if self._auto_connect:
                self._apply(links.device_seen(device.device_id, device.addresses, device.port, now))
            if isinstance(event, DeviceAdded):
                view = registry.get(device.device_id)
                if view is not None:
                    self._emit(DeviceOnline(view))
        elif isinstance(event, DeviceRemoved):
            device = event.device
            before = registry.get(device.device_id)
            registry.gone(device.device_id)
            if self._auto_connect:
                self._apply(links.device_gone(device.device_id, now))
            self._emit(DeviceOffline(self._offline_view(device, before)))

    def _offline_view(self, device: DiscoveredDevice, before: DeviceView | None) -> DeviceView:
        """View cuối của một thiết bị vừa rời mạng (online=False, không địa chỉ, cổng)."""
        assert self._registry is not None
        after = self._registry.get(device.device_id)
        if after is not None:
            return after
        if before is None:
            before = DeviceView(device.device_id, device.name, None, False, False, False, (), None)
        return dataclasses.replace(before, os=None, online=False, connected=False, addresses=(), port=None)

    async def _netwatch(self) -> None:
        """Cứ NETWORK_CHECK_INTERVAL giây: địa chỉ đổi → khởi động lại mDNS; thức dậy → quảng bá lại."""
        opts = self._mdns_opts
        assert opts is not None
        try:
            addresses = tuple(await asyncio.to_thread(net_discovery.local_ipv4_addresses))
        except Exception as e:
            log.debug("không đọc được địa chỉ IPv4 (%s)", type(e).__name__)
            addresses = ()
        watch = NetworkWatch(addresses, _mono_clock(), _wall_clock())
        while True:
            await asyncio.sleep(NETWORK_CHECK_INTERVAL)
            try:
                addresses = tuple(await asyncio.to_thread(net_discovery.local_ipv4_addresses))
            except Exception as e:
                log.debug("không đọc được địa chỉ IPv4 (%s), bỏ lượt", type(e).__name__)
                continue
            change = watch.sample(addresses, _mono_clock(), _wall_clock(), interval=NETWORK_CHECK_INTERVAL)
            if self._closing:
                return
            if self._mdns is None:
                log.debug("mDNS đang tắt (%s), thử bật lại", change.value)
                await self._restart_mdns()
            elif change is NetworkChange.ADDRESSES:
                if opts.advertise_addresses is None:
                    log.debug("địa chỉ của máy này đổi, khởi động lại mDNS")
                    await self._restart_mdns()
                else:
                    await self._readvertise()
            elif change is NetworkChange.WOKE:
                log.debug("đồng hồ nhảy (máy vừa thức dậy?), quảng bá lại mDNS")
                await self._readvertise()
            if change is not NetworkChange.NONE and self._auto_connect and self._links is not None:
                self._apply(self._links.network_changed(self._now()))

    async def _readvertise(self) -> None:
        async with self._mdns_lock:
            m = self._mdns
            if m is not None and not self._closing:
                await self._advertise(m)

    async def _restart_mdns(self) -> None:
        """Đóng instance mDNS cũ, báo mọi thiết bị offline, rồi bật instance mới.

        zeroconf chỉ tham gia nhóm multicast trên các địa chỉ có lúc khởi động, nên khi
        địa chỉ đổi, chỉ quảng bá lại là không đủ.
        """
        async with self._mdns_lock:
            if self._closing:
                return
            assert self._registry is not None and self._links is not None
            old, self._mdns = self._mdns, None
            self._discovery_active = False
            if old is not None:
                self._mdns_closing.add(old)
            now = self._now()
            before = {v.device_id: v for v in self._registry.views()}
            for device_id in self._registry.clear_seen():
                self._apply(self._links.device_gone(device_id, now))
                view = before.get(device_id)
                if view is not None:
                    after = self._registry.get(device_id)
                    offline = after or dataclasses.replace(
                        view, os=None, online=False, connected=False, addresses=(), port=None
                    )
                    self._emit(DeviceOffline(offline))
            if old is not None:
                await old.close()
                self._mdns_closing.discard(old)
            if not self._closing:
                await self._start_mdns()
