"""Node Teleport: một thư mục cấu hình, một cổng, ghép đôi, mDNS và các kết nối điều khiển.

Vòng đời và mô hình tiến trình ở docs/architecture.md mục 2. `Node` giữ `node.lock`
từ `start()` tới `close()`, nạp danh tính và kho tin cậy, mở `Listener` trên một cổng
(47474, hoặc cổng dự phòng lưu trong `settings.json`), rồi giao kết nối tới:

- ghép đôi → `PairingService.handle_incoming`;
- điều khiển → `ControlChannel` (ping/pong); message truyền file → `TransferManager`; mảnh
  `clipboard` → `ClipboardSync` (`app.clipboard`, chỉ khi có `clipboard` backend);
- dữ liệu → `TransferManager.serve_data` (`app.transfers`).

Kết nối điều khiển (cả vào lẫn ra) do `core.links.LinkTracker` quyết định: tối đa
**một** kết nối cho mỗi thiết bị (chống trùng protocol §6.1), tự mở tới thiết bị tin
cậy thấy qua mDNS (hoặc chỉ một thiết bị: `auto_connect_to`), tới địa chỉ người dùng
đưa (`connect_hint`), và nối lại theo lịch §6.2. Node chỉ thực hiện hành động tracker
trả về; đổi kết nối (`LinkSwitched`) không báo `NodeEvent` mà chuyển kênh điều khiển của
`TransferManager` sang kết nối mới. Thiết bị còn lượt truyền dở được `hold` (nối lại theo
lịch kể cả khi mDNS báo máy đi). `DeviceRegistry` gộp mDNS + `trusted.json` + trạng thái
kết nối thành `DeviceView` cho giao diện, và `on_event` báo `NodeEvent` (thiết bị
lên/xuống, kết nối được/mất/lỗi, máy đã ghép đôi đổi tên). Mỗi `LinkUp` / `LinkSwitched`,
`hello` của kết nối điều khiển đã xác thực cập nhật `os` của view (chỉ RAM) và tên trong
`trusted.json` (`_refresh_peer`, protocol §5.5; tối đa một lần ghi mỗi
`PEER_RENAME_MIN_INTERVAL` cho mỗi thiết bị, tên bị trần chặn được hẹn áp khi hết trần).

Địa chỉ đã lưu (protocol §6.2, gạch “Địa chỉ đã lưu”): mỗi kết nối điều khiển RA mở xong tới
đúng thiết bị (fingerprint khớp) cho node địa chỉ IP:cổng đã chứng minh tới được máy đó;
`_learn_address` ghi nó vào `trusted.json` (`last_address`, chỉ khi đổi, tối đa một lần mỗi
`ADDRESS_SAVE_MIN_INTERVAL` cho mỗi thiết bị, không lưu địa chỉ Internet công khai) và đưa vào
tracker (`LinkTracker.remember`). Lúc khởi động, địa chỉ đã lưu của thiết bị `_auto_target` được
đưa vào tracker, nên máy đã ghép đôi tự nối lại khi mạng chặn mDNS, kể cả khi máy này đang ẩn.
`connect_address` (“Kết nối bằng IP…”) thử một địa chỉ cho máy đã ghép đôi và trả `ConnectResult`.

mDNS là tùy chọn (`mdns=None`: tắt) và chỉ là gợi ý địa chỉ: kết nối luôn kiểm chứng
chỉ server với chứng chỉ đã lưu ngay trong bắt tay. Kết nối ra mở xong thì node báo
`Listener.note_outgoing_control`, để kết nối vào từ IP đó được ưu tiên khi cổng bị lũ
(protocol §2.3). Lỗi mDNS không làm `start()` thất bại. Khi quảng bá, node cứ
NETWORK_CHECK_INTERVAL giây đọc địa chỉ và đồng hồ (`core.discovery.NetworkWatch`):
địa chỉ đổi → khởi động lại mDNS; máy vừa thức dậy → quảng bá lại.

Truyền file (`receive_dir`, `auto_accept`, `rate_limit`, `on_transfer_event`) chạy ở
`TransferManager` (`node.transfers`); thứ tự đóng ở `close()` ở docs/architecture.md mục 5,
trần thời gian của nó là `close_time_limit()` (tính từ các hằng lúc gọi; CLI đặt hạn thoát cứng
theo hàm này). Mỗi instance mDNS chỉ được chờ đóng tối đa `MDNS_CLOSE_ALLOWANCE` giây trong
`close()`; phần còn lại chạy nền và được chờ cùng các task nền.

`find_trusted` tìm thiết bị tin cậy mà không lấy khóa, không tạo gì (`teleport send` kiểm
`--to` trước khi chạy node).

`unpair` chạy đồng bộ từ lúc xóa khỏi kho tới lúc lấy kết nối của máy đó ra khỏi bảng
và hủy mọi lượt truyền với nó (không có `await` xen giữa), rồi đóng kết nối đó. Kết nối
(vào hay ra) được đăng ký trước mọi `await`. Ghi `trusted.json` / `settings.json` chạy
đồng bộ trên event loop: file nhỏ, và nhờ vậy trạng thái trong RAM đổi cùng lúc với file.

Cài đặt (`app.settings`, đọc một lần lúc `start`): tên, thư mục nhận (`ReceiveDirChoice.SAVED`),
giới hạn tốc độ, ngưỡng dọn tạm, cài đặt theo thiết bị ("Hỏi trước khi nhận") và cài đặt của
giao diện (gồm nhớ trạng thái cửa sổ: `window`, `last_conversation`, `tray_hint_shown`); đổi lúc
chạy bằng các setter (`set_name`, `set_receive_dir`…), ghi file trước rồi mới đổi RAM;
`set_ui_preferences` không ghi khi không có gì đổi. Cài đặt theo thiết bị và `last_conversation`
chỉ có hiệu lực với thiết bị đang tin cậy; `unpair`, `unpair_offline` xóa cả hai của thiết bị đó,
ghép đôi (kể cả ghép đôi lại) xóa mục cài đặt theo thiết bị của nó.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import errno
import functools
import ipaddress
import logging
import math
import os
import platform
import re
import socket
import ssl
import time
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final, Self

import teleport
from teleport import paths
from teleport.app import clipboard as app_clipboard
from teleport.app import pairing as app_pairing
from teleport.app import storage
from teleport.app import transfers as app_transfers
from teleport.app.clipboard import ClipboardBackend, ClipboardEvent, ClipboardSync
from teleport.app.devices import DeviceNotFound, DeviceRegistry, DeviceView, resolve_trusted
from teleport.app.diskio import DiskExecutor, DiskJob, DiskTimeout
from teleport.app.pairing import PairingFinished, PairingOutcome, PairingPrompt, PairingService
from teleport.app.settings import DeviceSettings, Settings, SettingsSaveError, WindowGeometry
from teleport.app.transfers import ReceiveDirBusy, TransferManager, TransferUpdate
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
    LinkEvent,
    LinkFailed,
    LinkSwitched,
    LinkTracker,
    LinkUp,
    Notify,
    check_endpoint,
    is_saveable,
)
from teleport.core.messages import (
    PROTOCOL_VERSION,
    Clipboard,
    ConnectionKind,
    Hello,
    MessageError,
    Ping,
    Pong,
    validate_device_id,
    validate_device_name,
    validate_os,
)
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
from teleport.trust.store import TrustedDevice, TrustStore, TrustStoreError

LOCK_TIMEOUT = 2.0  # chờ node.lock (khóa của tiến trình vừa chết có thể chưa được nhả, nhất là Windows)
UNPAIR_CLOSE_TIMEOUT = 1.0  # unpair chờ đóng kết nối của máy bị hủy
NETWORK_CHECK_INTERVAL = 5.0  # giây giữa hai lần đọc địa chỉ + đồng hồ
SCAN_TIMEOUT = 3.0  # mặc định của scan_devices / teleport list
MAX_SCAN_TIMEOUT = 60.0
MDNS_CLOSE_ALLOWANCE = 2.0  # close(): chờ đóng mỗi instance mDNS (goodbye, socket) tối đa; còn lại chạy nền
PEER_RENAME_MIN_INTERVAL = 30.0  # giây tối thiểu giữa hai lần ghi tên mới của cùng một máy đã ghép đôi
ADDRESS_SAVE_MIN_INTERVAL = 30.0  # giây tối thiểu giữa hai lần ghi địa chỉ mới của cùng một máy đã ghép đôi
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
# Số instance mDNS tối đa lúc `close()`: instance đang dùng + một instance cũ đang đóng dở
# (`_restart_mdns` bị hủy giữa chừng; lỗi khi đóng làm task theo dõi mạng dừng, nên không có thêm).
_MAX_MDNS_INSTANCES = 2

_CONNECT_RESULTS: tuple[tuple[type[ConnectError], ConnectResult], ...] = (
    (Unreachable, ConnectResult.UNREACHABLE),
    (Rejected, ConnectResult.REJECTED),
    (IdentityMismatch, ConnectResult.IDENTITY_MISMATCH),
    (VersionMismatch, ConnectResult.VERSION_MISMATCH),
)

log = logging.getLogger(__name__)


class ReceiveDirChoice(Enum):
    """Giá trị đặc biệt của `Node(receive_dir=...)`."""

    # Thư mục nhận đã lưu (`settings.receive_dir`); chưa lưu thì `paths.default_receive_dir()`.
    # Không mở được thì node vẫn chạy mà không nhận file (`Node.receive_dir_error`).
    SAVED = "saved"


class _Unset(Enum):
    UNSET = "unset"


_UNSET: Final = _Unset.UNSET  # `set_ui_preferences`: trường không đổi


def device_os() -> str:
    """Tên hệ điều hành cho `hello.os`: `windows`, `macos`, `linux`, hoặc tên khác đã làm sạch."""
    system = platform.system()
    value = _OS_NAMES.get(system)
    if value is None:
        value = _OS_STRIP_RE.sub("", system.lower())[:16] or "unknown"
    return validate_os(value)


def _hostname() -> str:
    """Hostname như `platform.node()` (cùng `socket.gethostname()`, lỗi → chuỗi rỗng) nhưng không qua
    `platform.uname()`: trên Windows (Python 3.12+) lần gọi `uname()` đầu tiên truy vấn WMI qua COM,
    mất hàng chục tới hàng trăm ms, mà màn “Bắt đầu” gọi trên thread Qt. Seam cấp module cho test."""
    try:
        return socket.gethostname()
    except OSError:
        return ""


def default_device_name(identity: Identity) -> str:
    """Tên thiết bị mặc định: hostname (bỏ đuôi `.local`), không dùng được thì `Teleport-<4 ký tự mã>`."""
    raw = _hostname().strip()
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


class ReceiveDirUnavailable(Exception):
    """Không mở được thư mục nhận (`path`); `__cause__` là lỗi gốc.

    Lỗi gốc: `UnsafeReceiveDirError` (liên kết hay sai chủ trong cây `.teleport`),
    `PathTooLongError`, OSError (không tạo được, không phải thư mục…), `DiskTimeout`.
    """

    def __init__(self, path: Path) -> None:
        super().__init__("không mở được thư mục nhận")
        self.path = path


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
    # Lỗi của lần thử tới địa chỉ người dùng vừa đưa (`connect_hint` / `connect_address`). Có thể tới
    # sau khi `connect_address` đã trả UNREACHABLE vì hết giờ: bên gọi bỏ qua nếu không còn chờ.
    from_hint: bool = False


@dataclass(frozen=True, slots=True)
class DeviceRenamed:
    """Máy đã ghép đôi đổi tên: tên mới (theo `hello` đã xác thực) đã ghi vào `trusted.json`."""

    device: DeviceView  # view với tên mới
    old_name: str


NodeEvent = (
    DeviceOnline | DeviceOffline | DeviceConnected | DeviceDisconnected | ConnectFailed | DeviceRenamed
)


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Kết quả `scan_devices` (`teleport list`)."""

    device_id: str  # máy này
    name: str  # Settings.load().name or default_device_name(identity)
    port: int  # Settings.load().port or DEFAULT_PORT
    devices: tuple[DeviceView, ...]  # thấy qua mDNS + thiết bị tin cậy không thấy; connected luôn False


def close_time_limit() -> float:
    """Trần thời gian (giây) của `Node.close()`, tính từ các hằng đọc lúc gọi (docs/architecture.md mục 5).

    Cộng các bước có hạn của `close()`: chờ mảnh clipboard đang gửi dở (`CLIP_CLOSE_WAIT`); chờ
    đẩy `cancel` (`CANCEL_FLUSH_TIMEOUT`); đóng ghép đôi
    (`CLOSE_SESSIONS_TIMEOUT` + `ABORT_ON_CANCEL_TIMEOUT` + 1); đóng mDNS (`MDNS_CLOSE_ALLOWANCE`
    mỗi instance, tối đa hai instance); `Listener.close` (2 × `CLOSE_TIMEOUT` + 1); chờ task nền
    (`CLOSE_TIMEOUT` + 1); chờ đĩa (`CLOSE_DISK_WAIT`); xả bia mộ và lịch sử, song song
    (`TOMBSTONE_FLUSH_TIMEOUT`).
    Thread đĩa treo không kéo dài thêm (daemon).
    """
    return (
        app_clipboard.CLIP_CLOSE_WAIT
        + app_transfers.CANCEL_FLUSH_TIMEOUT
        + app_pairing.CLOSE_SESSIONS_TIMEOUT
        + app_pairing.ABORT_ON_CANCEL_TIMEOUT
        + 1
        + _MAX_MDNS_INSTANCES * MDNS_CLOSE_ALLOWANCE
        + 2 * net_connection.CLOSE_TIMEOUT
        + 1
        + net_connection.CLOSE_TIMEOUT
        + 1
        + app_transfers.CLOSE_DISK_WAIT
        + app_transfers.TOMBSTONE_FLUSH_TIMEOUT
    )


def find_trusted(config_dir: Path, query: str) -> TrustedDevice:
    """Tìm đúng một thiết bị tin cậy theo tên hoặc tiền tố mã (`resolve_trusted`), chỉ đọc `trusted.json`.

    Không lấy `node.lock`, không tạo file hay thư mục nào. Thư mục cấu hình chưa có →
    DeviceNotFound (`known` rỗng); có mà không phải thư mục → ConfigDirError (kiểm trước khi
    đọc, để mọi hệ điều hành cùng kết quả). Lỗi khác bay ra nguyên trạng: TrustStoreError,
    DeviceNotFound (`known`: mọi thiết bị tin cậy), AmbiguousDevice.
    """
    config_dir = Path(config_dir)
    try:
        if not config_dir.exists():
            raise DeviceNotFound(query, ())
        if not config_dir.is_dir():
            code = errno.ENOTDIR
            raise ConfigDirError(config_dir, NotADirectoryError(code, os.strerror(code), str(config_dir)))
    except OSError as e:
        raise ConfigDirError(config_dir, e) from e
    store = TrustStore.load(config_dir)
    return resolve_trusted(query, store.devices())


def _mentions_device(settings: Settings, device_id: str) -> bool:
    """`settings` có gì của `device_id` phải bỏ khi hủy ghép đôi: mục `devices` hay `last_conversation`."""
    return device_id in settings.devices or settings.last_conversation == device_id


def unpair_offline(config_dir: Path, query: str, *, lock_timeout: float | None = None) -> TrustedDevice:
    """Hủy ghép đôi khi không có node nào chạy: giữ `node.lock`, xóa khỏi `trusted.json`.

    `query` là tên hoặc tiền tố mã thiết bị (`resolve_trusted`). Thư mục cấu hình chưa
    có → DeviceNotFound ngay, không tạo gì. Lỗi bay ra nguyên trạng: NodeLockedError
    (đang có node chạy), TrustStoreError, DeviceNotFound, AmbiguousDevice, OSError
    (không tạo/khóa được, ví dụ đường dẫn là một file). Xóa xong, vẫn giữ khóa, bỏ mục cài
    đặt theo thiết bị của máy đó và `last_conversation` nếu là máy đó trong `settings.json` (chỉ
    ghi khi có một trong hai); không ghi được thì chỉ WARNING (chúng không có hiệu lực vì máy
    không còn tin cậy, và bị bỏ ở lần ghi sau).
    """
    config_dir = Path(config_dir)
    if not config_dir.exists():
        raise DeviceNotFound(query, ())
    lock = NodeLock(config_dir)
    lock.acquire(timeout=LOCK_TIMEOUT if lock_timeout is None else lock_timeout)
    try:
        store = TrustStore.load(config_dir)
        device = resolve_trusted(query, store.devices())
        removed = store.remove(device.device_id)
        settings = Settings.load(config_dir)
        if _mentions_device(settings, removed.device_id):
            ids = [d.device_id for d in store.devices()]
            try:
                settings.with_device(removed.device_id, None).pruned(ids).save(config_dir)
            except (OSError, ValueError, TypeError, RecursionError) as e:
                log.warning("không xóa được cài đặt theo thiết bị trong settings.json (%s)", type(e).__name__)
        return removed
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
        name=settings.name or default_device_name(identity),
        port=settings.port or DEFAULT_PORT,
        devices=registry.views(),
    )


def _ignore(_: object) -> None:
    return None


def _gone_view(view: DeviceView) -> DeviceView:
    """View cuối của một thiết bị chưa ghép đôi vừa rời khỏi registry: bỏ mọi thứ mDNS báo."""
    return dataclasses.replace(view, os=None, online=False, connected=False, addresses=(), port=None)


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
        auto_connect_to: str | None = None,
        on_event: Callable[[NodeEvent], None] | None = None,
        receive_dir: str | os.PathLike[str] | ReceiveDirChoice | None = None,
        auto_accept: bool = False,
        rate_limit: int | None = None,
        on_transfer_event: Callable[[TransferUpdate], None] | None = None,
        save_args: bool = False,
        clipboard: ClipboardBackend | None = None,
        on_clipboard_event: Callable[[ClipboardEvent], None] | None = None,
    ) -> None:
        """`name` sai → MessageError (lớp con ValueError); `port` không phải None hay 0–65535 → ValueError.

        Cài đặt (`settings.json`) đọc một lần lúc `start()`. `name=None`: tên đã lưu, chưa có thì
        hostname; `name` khác None chỉ dùng cho lần chạy này (không lưu, trừ `save_args`).
        `port=None`: cổng đã lưu (hoặc 47474), bận thì để hệ điều
        hành chọn và lưu lại; `port` là số: đúng cổng đó, không dự phòng, không lưu.
        `host` được kiểm lúc `start()`. `mdns=None`: không dùng mDNS; khác None mà không
        phải `MdnsOptions` → TypeError. `auto_connect`: tự mở và giữ kết nối điều khiển
        tới thiết bị tin cậy thấy qua mDNS (không phải bool → ValueError).
        `auto_connect_to`: chỉ tự mở kết nối tới thiết bị có `device_id` này (registry vẫn
        nhận mọi thiết bị mDNS); sai → MessageError; khác None mà `auto_connect=False` →
        ValueError. `on_event` nhận `NodeEvent`, gọi đồng bộ trên loop; raise thì chỉ ghi log.
        `on_pairing_prompt=None`: node không hiện mã được nên không ghép đôi được;
        khi đó `accept_pairing=True` → ValueError.

        Truyền file (`app.transfers`): `receive_dir=None` → không nhận file (`offer` nhận
        `reject{not_receiving}`); đường dẫn → mở lúc `start()` (tạo nếu chưa có), không mở
        được thì `start` thất bại; `ReceiveDirChoice.SAVED` → thư mục đã lưu (chưa lưu thì
        `paths.default_receive_dir()`), không mở được thì node vẫn chạy mà không nhận file
        (`receive_dir_error`, đổi bằng `set_receive_dir`). Có
        `receive_dir` mà không tự nhận (`auto_accept=False`) và không có `on_transfer_event`
        để hỏi người dùng → ValueError. `auto_accept` không phải bool → ValueError.
        `rate_limit`: None (giới hạn đã lưu, chưa có thì không giới hạn) hoặc số nguyên ≥
        `transfers.MIN_RATE_LIMIT` byte/giây chỉ cho lần chạy này (không lưu), sai →
        ValueError. `on_transfer_event` nhận `TransferUpdate`, gọi đồng bộ trên loop; raise
        thì chỉ ghi log. `receive_dir` không phải đường dẫn dạng chuỗi, `on_transfer_event`
        không gọi được → TypeError.

        `save_args` (lần chạy đầu của giao diện): `start()` lưu `name` (nếu có) trước khi dựng
        `hello`, và lưu `receive_dir` (nếu là đường dẫn) ngay sau khi mở được, trước khi mở cổng
        và quảng bá; ghi lỗi thì `start` thất bại với `SettingsSaveError` (không quảng bá một tên
        người dùng chưa chọn). Không phải bool → ValueError.

        Đồng bộ clipboard (`app.clipboard`): `clipboard` là backend đọc/ghi clipboard thật (giao
        diện; test dùng `FakeClipboard`); None (CLI) → node không đồng bộ clipboard, mảnh
        `clipboard` tới bị bỏ qua. `on_clipboard_event` nhận `ClipboardEvent`, gọi đồng bộ trên
        loop; raise thì chỉ ghi log; không gọi được → TypeError.
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
        if auto_connect_to is not None:
            validate_device_id(auto_connect_to)
            if not auto_connect:
                raise ValueError("auto_connect_to cần auto_connect=True")
        receive_path: Path | None = None
        receive_saved = isinstance(receive_dir, ReceiveDirChoice)  # chỉ có SAVED
        if receive_dir is not None and not isinstance(receive_dir, ReceiveDirChoice):
            raw = os.fspath(receive_dir)  # không phải đường dẫn → TypeError
            if not isinstance(raw, str):
                raise TypeError("receive_dir phải là đường dẫn dạng chuỗi")
            receive_path = Path(raw)
        if type(auto_accept) is not bool:
            raise ValueError("auto_accept phải là bool")
        minimum = app_transfers.MIN_RATE_LIMIT
        if rate_limit is not None and (type(rate_limit) is not int or rate_limit < minimum):
            raise ValueError(f"rate_limit phải là None hoặc số nguyên ≥ {minimum} (byte/giây)")
        if on_transfer_event is not None and not callable(on_transfer_event):
            raise TypeError("on_transfer_event phải là hàm")
        has_dir = receive_path is not None or receive_saved
        if has_dir and not auto_accept and on_transfer_event is None:
            raise ValueError("receive_dir cần auto_accept=True hoặc on_transfer_event để hỏi người dùng")
        if type(save_args) is not bool:
            raise ValueError("save_args phải là bool")
        if on_clipboard_event is not None and not callable(on_clipboard_event):
            raise TypeError("on_clipboard_event phải là hàm")
        self._config_dir = Path(config_dir)
        self._name_arg = name
        self._port_arg = port
        self._host = host
        self._accept_pairing = accept_pairing
        self._on_pairing_prompt = on_pairing_prompt  # None: không ghép đôi được (PairingService)
        self._on_pairing_finished = on_pairing_finished or _ignore
        self._mdns_opts = mdns
        self._auto_connect = auto_connect
        self._auto_connect_to = auto_connect_to  # None: mọi thiết bị tin cậy
        self._on_event = on_event or _ignore
        self._receive_path = receive_path
        self._receive_saved = receive_saved  # `ReceiveDirChoice.SAVED`
        self._auto_accept = auto_accept
        self._rate_limit = rate_limit  # None: giới hạn đã lưu
        self._on_transfer_event = on_transfer_event
        self._save_args = save_args
        self._clipboard_backend = clipboard
        self._on_clipboard_event = on_clipboard_event or _ignore
        self._settings: Settings | None = None  # đọc lúc `start`; đổi cùng lúc với file (`_save_settings`)
        self._receive_dir_error: ReceiveDirUnavailable | None = None  # thư mục `SAVED` không mở được
        self._receive_dir_lock = asyncio.Lock()  # `set_receive_dir` chạy lần lượt
        self._visible = True  # chế độ ẩn (F10): `settings.visible`, đọc lúc `start`, đổi bằng `set_visible`

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
        self._disk: DiskExecutor | None = None  # I/O đĩa của truyền file; TransferManager đóng nó
        self._transfers: TransferManager | None = None
        self._clipboard: ClipboardSync | None = None  # chỉ khi có `clipboard` backend
        self._conns: dict[int, Connection] = {}  # conn_id → kết nối điều khiển đang chạy (vào lẫn ra)
        self._next_conn_id = 1
        self._attempts: dict[int, asyncio.Task] = {}  # attempt_id → task của lần thử đang chạy
        self._link_tasks: set[asyncio.Task] = set()  # task `link-out` (thử rồi chạy kết nối ra)
        self._background: set[asyncio.Task] = set()  # task đóng kết nối
        self._links_wake = asyncio.Event()  # bộ hẹn giờ của tracker tính lại `next_wakeup`
        self._timer_deadline: float | None = None  # thời điểm bộ hẹn giờ đang chờ tới
        self._timer_task: asyncio.Task | None = None
        self._netwatch_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None  # hẹn giờ dọn tạm (chỉ khi có thư mục nhận)
        self._mdns: MdnsDiscovery | None = None
        self._mdns_closing: set[MdnsDiscovery] = set()  # instance cũ đang đóng (khởi động lại)
        self._mdns_lock = asyncio.Lock()
        self._discovery_active = False
        self._discovery_error: Exception | None = None
        self._mdns_error_key: tuple[str, type, str] | None = None  # lỗi mDNS đã ghi WARNING gần nhất
        # Máy đã ghép đôi đổi tên (`_refresh_peer`): lần thử ghi gần nhất (giờ loop), tên chờ hết
        # trần PEER_RENAME_MIN_INTERVAL và hẹn giờ áp nó, theo device_id.
        self._renamed_at: dict[str, float] = {}
        self._pending_names: dict[str, str] = {}
        self._rename_timers: dict[str, asyncio.TimerHandle] = {}
        # Địa chỉ đã quan sát của máy đã ghép đôi (`_learn_address`): lần thử ghi gần nhất (giờ loop),
        # địa chỉ chờ hết trần ADDRESS_SAVE_MIN_INTERVAL (kèm fingerprint lúc học) và hẹn giờ áp nó.
        self._address_saved_at: dict[str, float] = {}
        self._pending_addresses: dict[str, tuple[str, int, str]] = {}
        self._address_timers: dict[str, asyncio.TimerHandle] = {}
        # `connect_address` đang chờ: device_id → [(ip, cổng, future)], mỗi lời gọi một future.
        self._address_waiters: dict[str, list[tuple[str, int, asyncio.Future[ConnectResult]]]] = {}

    # ---- Vòng đời --------------------------------------------------------------------------

    async def start(self) -> None:
        """Lấy `node.lock`, nạp danh tính, kho tin cậy và cài đặt, mở cổng, bật mDNS (nếu có).

        Cài đặt đọc một lần (`Settings.load`) trước khi dựng `hello`. Có `receive_dir` thì mở thư mục
        nhận (qua `DiskExecutor`, hạn `DISK_OP_TIMEOUT`). Tạo
        `TransferManager` và chạy `TransferManager.restore()` trước khi mở cổng: khôi phục bia mộ
        (`tombstones.json`, nạp song song với `history.json`), lượt đã xong và lượt nhận dở; `start`
        có thể chậm thêm tối đa hạn đọc bia mộ (`DISK_OP_TIMEOUT`) + hạn quét (`DISK_SYNC_TIMEOUT`)
        + hạn tổng của bước mở lại (docs/architecture.md mục 5). Sau khi mở cổng và mDNS, có
        `receive_dir` thì bật hẹn giờ dọn tạm (`cleanup_temps`, lần đầu chạy ngay, rồi mỗi
        `CLEANUP_INTERVAL`).

        Lỗi bay ra sau khi đã dọn (đóng cổng, đóng mDNS, nhả khóa): NodeLockedError,
        IdentityError (cả khi `PairingService` không đọc được khóa để ký lúc ghép đôi),
        TrustStoreError, ConfigDirError (OSError khi lấy khóa, nạp danh
        tính hay kho tin cậy), ReceiveDirUnavailable (không mở được thư mục nhận là đường dẫn
        tường minh; `SAVED` thì không lỗi: `receive_dir_error`), SettingsSaveError (`save_args`
        mà không ghi được `settings.json`), OSError
        (không mở được cổng), ValueError (`host` sai). Bị
        hủy lần nữa trong lúc dọn thì cổng vẫn đóng đồng bộ trước khi nhả khóa, còn mDNS
        đóng nền sau khi đã nhả khóa. Thất bại hay bị hủy trong lúc khôi phục: lần mở lại xong muộn
        được đóng nền, thư mục nhận đóng sau khi mọi việc đĩa dùng nó đã xong (đuôi đĩa,
        `TransferManager.abort_now`), có thể sau khi đã nhả khóa. Lỗi mDNS không làm `start` thất bại (xem
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
            self._identity, self._store = identity, store
            # Đọc cài đặt đúng một lần; từ đây `self._settings` đổi cùng lúc với file.
            self._settings = Settings.load(self._config_dir)
            self._visible = self._settings.visible
            if self._save_args and self._name_arg is not None and self._settings.name != self._name_arg:
                # Lần chạy đầu: lưu tên người dùng chọn trước khi quảng bá nó.
                self._save_settings(dataclasses.replace(self._settings, name=self._name_arg))
            name = self._name_arg or self._settings.name or default_device_name(identity)
            hello = Hello(PROTOCOL_VERSION, identity.device_id, name, teleport.__version__, device_os())
            pairing = PairingService(
                identity=identity,
                hello=hello,
                store=store,
                on_prompt=self._on_pairing_prompt,
                on_finished=self._pairing_finished,
                on_trust_changed=self._trust_changed,
                accepting=self._accept_pairing,
                before_save=self._before_pair_save,
            )
            listener = Listener(
                identity,
                hello,
                store.cert_pems(),
                store.is_trusted,
                self._on_connection,
                allow_anonymous=self._allows_anonymous,
            )
            self._hello = hello
            self._pairing, self._listener = pairing, listener
            # Tracker có trước khi mở cổng: kết nối vào có thể tới ngay.
            self._registry = DeviceRegistry(identity.device_id)
            self._links = LinkTracker(identity.device_id)
            self._registry.set_trusted(store.devices())
            self._apply(self._links.trusted_changed(self._trusted_ids(), self._now()))
            disk = self._disk = DiskExecutor()
            receive_dir = await self._start_receive_dir(disk)
            # TransferManager có trước khi mở cổng: kết nối dữ liệu vào có thể tới ngay.
            settings = self._settings
            assert settings is not None
            transfers = TransferManager(
                identity=identity,
                hello=hello,
                store=store,
                executor=disk,
                receive_dir=receive_dir,
                config_dir=self._config_dir,
                auto_accept=self._auto_accept,
                rate_limit=settings.rate_limit if self._rate_limit is None else self._rate_limit,
                on_event=self._on_transfer_event,
                new_id=self._new_conn_id,
                set_hold=self._transfer_hold,
                ask_before_receive=self._asks_before_receive,
            )
            if settings.temp_retention_days is not None:
                transfers.set_temp_retention(settings.temp_retention_days * 86400.0)
            self._transfers = transfers
            transfers.start()
            if self._clipboard_backend is not None:
                # Sau TransferManager (`paused` đọc nó), trước khi mở cổng (kết nối có thể tới ngay).
                self._clipboard = ClipboardSync(
                    backend=self._clipboard_backend,
                    enabled=self._clipboard_enabled,
                    paused=self._clipboard_paused,
                    on_event=self._on_clipboard_event,
                    control_idle=transfers.control_idle,
                )
                self._clipboard.start()
            # Khôi phục lượt nhận dở, bia mộ, lượt đã xong trước khi mở cổng (lời gọi engine đầu tiên).
            await transfers.restore()
            if self._close_done is not None:
                raise RuntimeError("Node đã bị đóng trong lúc khởi động")
            await self._listen(listener)
            if self._close_done is not None:
                raise RuntimeError("Node đã bị đóng trong lúc khởi động")
            if self._mdns_opts is not None:
                await self._start_mdns()
                if self._close_done is not None:
                    raise RuntimeError("Node đã bị đóng trong lúc khởi động")
            self._timer_task = asyncio.create_task(self._links_timer(), name=f"{_TASK_NAME}.links-timer")
            # Sau `transfers.restore()` (lời gọi engine đầu tiên) và khi cổng, mDNS đã sẵn sàng.
            self._remember_saved_addresses()
            if self._mdns_opts is not None and self._mdns_opts.advertise:
                self._netwatch_task = asyncio.create_task(self._netwatch(), name=f"{_TASK_NAME}.netwatch")
            if receive_dir is not None:
                self._cleanup_task = asyncio.create_task(
                    self._cleanup_temps(transfers), name=f"{_TASK_NAME}.cleanup-temps"
                )
        except BaseException:
            self._closing = True  # _apply không tạo task mới nữa
            self._cancel_rename_timers()
            self._cancel_address_timers()
            if self._clipboard is not None:
                self._clipboard.abort()
            try:
                await self._abort_start(listener)
            finally:
                self._identity = self._store = self._hello = None
                self._settings = None
                self._pairing = self._listener = None
                self._links = self._registry = None
                self._transfers = self._disk = None
                self._clipboard = None
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
        tasks = [t for t in (self._timer_task, self._netwatch_task, self._cleanup_task) if t is not None]
        tasks += [*self._attempts.values(), *self._link_tasks]
        for task in tasks:
            task.cancel()
        # Truyền file: chưa có lượt nào (cổng chưa nhận kết nối) — hủy task mạng, shutdown executor;
        # task đĩa (như đóng handle mở lại tới muộn của `restore`) không bị hủy, thư mục nhận đóng
        # ngay hay ở đuôi đĩa khi mọi việc dùng nó đã xong (`TransferManager.abort_now`).
        if self._transfers is not None:
            self._transfers.abort_now()
        elif self._disk is not None:
            self._disk.shutdown()
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

    async def _start_receive_dir(self, disk: DiskExecutor) -> storage.ReceiveDir | None:
        """Thư mục nhận lúc `start` (đường dẫn, `SAVED` hay không có); xem docstring constructor."""
        assert self._settings is not None
        if self._receive_path is None and not self._receive_saved:
            return None
        if self._receive_path is not None:
            receive_dir = await self._open_receive_dir(disk, self._receive_path)
        else:
            saved = self._settings.receive_dir
            path = paths.default_receive_dir() if saved is None else Path(saved)
            try:
                receive_dir = await self._open_receive_dir(disk, path)
            except ReceiveDirUnavailable as e:
                # Vẫn chạy (gửi được), không nhận; không tự chuyển sang thư mục khác.
                log.warning("thư mục nhận đã lưu không mở được: node chạy mà không nhận file")
                self._receive_dir_error = e
                return None
        if self._close_done is not None:
            receive_dir.close()
            raise RuntimeError("Node đã bị đóng trong lúc khởi động")
        root = str(receive_dir.root)
        if self._save_args and self._receive_path is not None and self._settings.receive_dir != root:
            try:
                self._save_settings(dataclasses.replace(self._settings, receive_dir=root))
            except BaseException:
                receive_dir.close()  # chưa giao cho TransferManager: không ai khác đóng nó
                raise
        return receive_dir

    async def _open_receive_dir(self, disk: DiskExecutor, path: Path) -> storage.ReceiveDir:
        """`ReceiveDir.open` qua executor, hạn `DISK_OP_TIMEOUT`; lỗi → ReceiveDirUnavailable."""
        job = disk.submit(storage.ReceiveDir.open, path)
        try:
            return await job.result(app_transfers.DISK_OP_TIMEOUT)
        except (storage.StorageError, OSError, DiskTimeout) as e:
            if isinstance(e, DiskTimeout):
                # Mở xong muộn thì đóng ngay (task nền, `_abort_start` chờ có giới hạn).
                self._spawn(self._close_late_receive_dir(job), f"{_TASK_NAME}.close-receive-dir")
            log.warning("không mở được thư mục nhận (%s)", type(e).__name__)
            raise ReceiveDirUnavailable(path) from e
        except asyncio.CancelledError:
            if not job.cancel():
                self._spawn(self._close_late_receive_dir(job), f"{_TASK_NAME}.close-receive-dir")
            raise

    @staticmethod
    async def _close_late_receive_dir(job: DiskJob[storage.ReceiveDir]) -> None:
        await job.settled()
        try:
            receive_dir = await job.result(None)
        except (Exception, asyncio.CancelledError):
            return
        receive_dir.close()

    async def _listen(self, listener: Listener) -> None:
        """Mở cổng: cổng chỉ định, hoặc cổng ưu tiên rồi dự phòng (protocol §2.1)."""
        if self._port_arg is not None:
            await listener.start(self._host, self._port_arg)
            return
        assert self._settings is not None
        preferred = self._settings.port or DEFAULT_PORT
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
            # Dựng từ `self._settings` lúc này (không phải bản chụp trước `await`).
            self._save_settings(dataclasses.replace(self._settings, port=listener.port))
        except SettingsSaveError as e:
            # ValueError/TypeError/RecursionError: trường lạ giữ lại từ file (ví dụ JSON lồng
            # quá sâu, đọc được nhưng không ghi lại được) — node vẫn chạy, chỉ không lưu cổng.
            cause = e.__cause__
            log.warning("không lưu được cổng dự phòng vào settings.json (%s)", type(cause).__name__)

    async def close(self) -> None:
        """Tạm dừng lượt đang nhận, hủy các lượt truyền khác và phiên ghép đôi, gửi goodbye mDNS,
        đóng cổng và mọi kết nối, dọn thư mục tạm, lưu bia mộ, nhả `node.lock`.

        Thứ tự (docs/architecture.md mục 5): `TransferManager.begin_close()` (trước `await` đầu
        tiên): tạm dừng lượt đang nhận (không `cancel`, thư mục tạm giữ cho lần chạy sau), hủy lượt
        gửi và `offer` đang chờ; dừng đồng bộ clipboard (`ClipboardSync.close()`, đồng bộ) và hẹn
        giờ (`core.links`, theo dõi mạng, dọn tạm); chờ mảnh clipboard đang gửi dở xong tối đa
        `CLIP_CLOSE_WAIT` (`ClipboardSync.drain`, để nó không giữ khóa gửi khi `cancel` cần); chờ
        đẩy các `cancel` ra kết nối điều khiển tối đa `CANCEL_FLUSH_TIMEOUT`, trước khi cổng cắt
        kết nối dữ liệu, rồi hủy các task gửi clipboard còn lại (`ClipboardSync.abort()`); đóng
        ghép đôi, mDNS (mỗi instance chờ tối đa `MDNS_CLOSE_ALLOWANCE`), cổng, kết nối; rồi
        `TransferManager.close()`: chờ các hàng đĩa tối đa `CLOSE_DISK_WAIT`, xả `tombstones.json`
        và `history.json` (song song) tối đa `TOMBSTONE_FLUSH_TIMEOUT`, đóng thư mục nhận (ngay,
        hay ở đuôi đĩa chạy nền khi còn thread đĩa bận: `TransferManager.close`).
        Gọi nhiều lần hay đồng thời đều được; trước `start` hay sau khi `start` thất bại thì không
        làm gì. Bị hủy giữa chừng thì vẫn hủy cứng mọi phiên ghép đôi (không ghi `trusted.json`
        nữa), mọi task
        mạng của truyền file (không chờ đĩa: task đĩa không bị hủy mà chạy nốt bước dọn nền,
        `TransferManager.abort_now`), ngừng nhận kết nối và hủy mọi kết nối trước khi nhả khóa
        (phần chờ đóng xong, kể cả mDNS, chạy nền, có giới hạn), rồi để CancelledError bay ra.
        Đuôi đĩa có thể còn chạy sau khi khóa đã nhả.
        """
        if self._close_done is not None:
            await self._close_done.wait()
            return
        self._close_done = asyncio.Event()
        if not self._running:
            self._close_done.set()
            return
        self._closing = True
        self._cancel_rename_timers()
        self._cancel_address_timers()
        self._fail_address_waiters()  # cùng khối đồng bộ với `_closing`, trước `await` đầu tiên
        assert self._pairing is not None and self._listener is not None
        listener_closing = False
        transfers = self._transfers
        try:
            if transfers is not None:
                # Đồng bộ, trước await đầu tiên: tạm dừng lượt đang nhận, ngừng nhận offer, hủy lượt khác.
                transfers.begin_close()
            if self._clipboard is not None:
                self._clipboard.close()  # đồng bộ: không đọc/ghi/gửi clip mới nữa
            await self._stop_tasks([self._timer_task, self._netwatch_task, self._cleanup_task])
            if self._clipboard is not None:
                # Mảnh clip đang gửi dở giữ khóa gửi của kết nối điều khiển: chờ nó xong trước khi
                # tính hạn đẩy `cancel` (đường chậm: một mảnh có thể lâu hơn CANCEL_FLUSH_TIMEOUT).
                await self._clipboard.drain(app_clipboard.CLIP_CLOSE_WAIT)
            if transfers is not None:
                await transfers.flush_control(app_transfers.CANCEL_FLUSH_TIMEOUT)
            if self._clipboard is not None:
                # Kết nối sắp đóng: hủy cả mảnh clip đang gửi dở (sau khi các `cancel` đã đi).
                self._clipboard.abort()
            await self._pairing.close()  # phiên đang chạy gửi pair_abort{user}
            for m in self._mdns_instances():
                await self._close_mdns(m)  # goodbye: máy khác xóa máy này ngay (mvp N1)
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
            if transfers is not None:
                # Task dữ liệu, hàng đĩa (≤ CLOSE_DISK_WAIT), bia mộ và lịch sử (song song,
                # ≤ TOMBSTONE_FLUSH_TIMEOUT), thư mục nhận.
                await transfers.close()
        except BaseException:
            self._pairing.abort_all()
            if transfers is not None:
                transfers.abort_now()
            if self._clipboard is not None:
                self._clipboard.abort()
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

    async def _close_mdns(self, m: MdnsDiscovery) -> None:
        """`m.close()` chạy như task nền, chờ tối đa `MDNS_CLOSE_ALLOWANCE`; quá hạn thì để nó chạy tiếp.

        Không hủy: zeroconf thấy task bị hủy thì dừng giữa chừng và có thể để lại socket. Phần
        còn lại được `_drain_background` chờ (có giới hạn) như mọi task nền.
        """
        task = self._spawn(m.close(), f"{_TASK_NAME}.close-mdns")
        _, pending = await asyncio.wait({task}, timeout=MDNS_CLOSE_ALLOWANCE)
        if pending:
            log.warning("đóng mDNS quá %.1f s, để chạy nền", MDNS_CLOSE_ALLOWANCE)

    def _mdns_instances(self) -> list[MdnsDiscovery]:
        """Mọi instance mDNS chưa đóng xong (đang dùng và đang đóng dở)."""
        instances = list(self._mdns_closing)
        if self._mdns is not None and self._mdns not in self._mdns_closing:
            instances.append(self._mdns)
        return instances

    def _all_node_tasks(self) -> list[asyncio.Task]:
        tasks = [t for t in (self._timer_task, self._netwatch_task, self._cleanup_task) if t is not None]
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
    def transfers(self) -> TransferManager:
        """Quản lý lượt truyền file (`app.transfers`). Chưa `start()` → RuntimeError.

        Sau `close()` vẫn trả về đối tượng cũ: mọi lời gọi không làm gì, `send` → RuntimeError.
        """
        return self._need(self._transfers)

    @property
    def clipboard(self) -> ClipboardSync | None:
        """Đồng bộ clipboard (`app.clipboard`); None khi node không có `clipboard` backend (CLI)
        hay chưa `start()`. Sau `close()` vẫn trả về đối tượng cũ: mọi lời gọi không làm gì."""
        return self._clipboard

    @property
    def port(self) -> int | None:
        """Cổng đang lắng nghe; None trước `start()`."""
        return None if self._listener is None else self._listener.port

    @property
    def accept_pairing(self) -> bool:
        """Có nhận yêu cầu ghép đôi tới không; đổi lúc chạy có hiệu lực với yêu cầu kế tiếp.

        Đây là "chế độ chờ ghép đôi" (mvp F3); node không tự đổi nó khi đổi `visible`. Đang ẩn
        mà `accept_pairing` True thì node vẫn gửi `hello` cho máy lạ (luật `_allows_anonymous`),
        nên giao diện phải giữ `accept_pairing = visible or đang mở "Chờ ghép đôi"`; CLI chỉ đặt
        True trong `teleport listen`. Đặt True khi node không có `on_pairing_prompt` → ValueError.
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

    # ---- Cài đặt ---------------------------------------------------------------------------
    # Mọi setter: chạy trên loop của node, đồng bộ (trừ `set_receive_dir`); node chưa chạy hay
    # đang đóng → RuntimeError. Ghi `settings.json` trước, đổi RAM sau; ghi lỗi → SettingsSaveError,
    # không đổi gì (docs/architecture.md mục 4, 5).

    @property
    def settings(self) -> Settings:
        """Ảnh chụp cài đặt hiện tại (bất biến). Chưa `start()` → RuntimeError."""
        settings: Settings = self._need(self._settings)
        return settings

    @property
    def visible(self) -> bool:
        """Máy chưa ghép đôi có thấy máy này không (F10, `settings.visible`). Chưa `start` → RuntimeError."""
        self._need(self._settings)
        return self._visible

    @property
    def receive_dir_error(self) -> ReceiveDirUnavailable | None:
        """Thư mục nhận đã lưu (`ReceiveDirChoice.SAVED`) không mở được lúc `start`: node chạy mà
        không nhận file. None khi không có lỗi, hay sau khi `set_receive_dir` thành công."""
        return self._receive_dir_error

    def device_settings(self, device_id: str) -> DeviceSettings:
        """Cài đặt của thiết bị; thiết bị không tin cậy (hay chưa có mục) → mặc định.

        `device_id` sai → MessageError; chưa `start()` → RuntimeError.
        """
        settings: Settings = self._need(self._settings)
        validate_device_id(device_id)
        if self._store is None or self._store.get(device_id) is None:
            return DeviceSettings()
        return settings.device(device_id)

    def _check_running(self) -> None:
        if not self._running or self._closing or self._settings is None:
            raise RuntimeError("node chưa chạy")

    def _save_settings(self, new: Settings) -> None:
        """Ghi `new` (bỏ mục của thiết bị không tin cậy) rồi mới gán `self._settings` (đồng bộ).

        Lỗi ghi (OSError, ValueError, TypeError, RecursionError) → SettingsSaveError, không đổi gì.
        Chỉ tiến trình giữ `node.lock` gọi (`start`, setter, `unpair`, ghép đôi).
        """
        assert self._store is not None
        pruned = new.pruned(self._trusted_ids())
        try:
            pruned.save(self._config_dir)
        except (OSError, ValueError, TypeError, RecursionError) as e:
            raise SettingsSaveError("không ghi được settings.json") from e
        # Đã ghi: file giờ là bản này (lần ghi sau không chép `.bak` nữa dù lần nạp đầu lỗi đọc).
        self._settings = dataclasses.replace(pruned, load_failed=False) if pruned.load_failed else pruned

    def set_name(self, name: str) -> str:
        """Đổi và lưu tên máy này; trả tên đang dùng. Sai → MessageError.

        Trùng tên đang dùng thì chỉ lưu (nếu chưa lưu). Tên mới: lưu, rồi trong cùng khối đồng bộ
        dùng `hello` mới cho ghép đôi, kết nối điều khiển và dữ liệu mở từ giờ và cổng; đang quảng
        bá thì quảng bá lại (nền). Kết nối đang mở giữ `hello` cũ (không nối lại).
        """
        self._check_running()
        assert self._settings is not None and self._hello is not None
        name = validate_device_name(name)
        if name == self._hello.name:
            if self._settings.name != name:
                self._save_settings(dataclasses.replace(self._settings, name=name))
            return name
        self._save_settings(dataclasses.replace(self._settings, name=name))
        hello = dataclasses.replace(self._hello, name=name)
        self._hello = hello
        self.pairing.set_hello(hello)
        self.transfers.set_hello(hello)
        if self._listener is not None:
            self._listener.set_hello(hello)
        if self._mdns_opts is not None and self._mdns_opts.advertise:
            self._spawn(self._readvertise(), f"{_TASK_NAME}.readvertise")
        log.info("đã đổi tên máy này")
        return name

    async def set_receive_dir(self, path: str | os.PathLike[str]) -> Path:
        """Đổi thư mục nhận (chung cho mọi thiết bị) và lưu; trả gốc thật (`ReceiveDir.root`).

        Các lời gọi chạy lần lượt. Lỗi: không phải đường dẫn dạng chuỗi → TypeError; node không
        tự nhận và không có `on_transfer_event` → ValueError; còn lượt nhận đã chấp nhận, hàng đĩa
        hay việc dọn của khôi phục (`TransferManager.receive_dir_busy`; `offer` chưa nhận không
        tính, chúng được kiểm lại chỗ trống trên thư mục mới) → `ReceiveDirBusy`; không mở được →
        `ReceiveDirUnavailable`; ghi lỗi → `SettingsSaveError`; node chưa chạy hay đóng trong lúc
        mở → RuntimeError. Lỗi thì thư mục nhận và cài đặt không đổi.

        `await` duy nhất là lúc mở thư mục (qua `DiskExecutor`); sau đó, trong một khối đồng bộ:
        kiểm lại đóng/bận, lưu (dựng từ cài đặt lúc này), thay thư mục (`replace_receive_dir`,
        thư mục cũ đóng khi không còn ai dùng), dọn tạm ngay một lần ở thư mục mới. Node chưa có
        thư mục nhận thì từ giờ nhận file (theo tạm dừng nhận). Lượt dở có sẵn trong thư mục mới
        không được khôi phục lúc chạy (chỉ ở lần `start` sau).
        """
        raw = os.fspath(path)  # không phải đường dẫn → TypeError
        if not isinstance(raw, str):
            raise TypeError("path phải là đường dẫn dạng chuỗi")
        async with self._receive_dir_lock:
            self._check_running()
            if not self._auto_accept and self._on_transfer_event is None:
                raise ValueError("nhận file cần auto_accept=True hoặc on_transfer_event để hỏi người dùng")
            transfers = self.transfers
            disk = self._disk
            assert disk is not None
            if transfers.receive_dir_busy():
                raise ReceiveDirBusy()
            try:
                rd = await self._open_receive_dir(disk, Path(raw))
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if (task is None or task.cancelling() == 0) and (self._closing or not self._running):
                    # Không ai hủy task này: việc mở còn xếp hàng bị `DiskExecutor.shutdown()` bỏ (node đóng).
                    raise RuntimeError("node đang đóng") from None
                raise
            # Từ đây tới hết: một khối đồng bộ.
            if not self._running or self._closing:
                rd.close()
                raise RuntimeError("node đang đóng")
            if transfers.receive_dir_busy():
                rd.close()
                raise ReceiveDirBusy()
            assert self._settings is not None
            root = str(rd.root)
            if transfers.receive_dir == rd.root:
                rd.close()
                if self._settings.receive_dir != root:
                    self._save_settings(dataclasses.replace(self._settings, receive_dir=root))
                return rd.root
            try:
                # ValueError nếu gốc thật không lưu được (ví dụ quá dài), SettingsSaveError nếu ghi lỗi.
                self._save_settings(dataclasses.replace(self._settings, receive_dir=root))
            except BaseException:
                rd.close()
                raise
            transfers.replace_receive_dir(rd)
            self._receive_dir_error = None
            self._restart_cleanup()
            log.info("đã đổi thư mục nhận")
            return rd.root

    def set_ask_before_receive(self, device_id: str, ask: bool) -> None:
        """Hỏi (True) hay tự nhận (False, "Luôn nhận") `offer` từ thiết bị tin cậy `device_id`; lưu.

        Áp cho lần hỏi kế tiếp; `offer` đang chờ không tự được nhận (giao diện gọi `decide`).
        `device_id` sai → MessageError; không tin cậy → KeyError; `ask` không phải bool → TypeError.
        """
        self._check_running()
        assert self._settings is not None and self._store is not None
        validate_device_id(device_id)
        if type(ask) is not bool:
            raise TypeError("ask phải là bool")
        if self._store.get(device_id) is None:
            raise KeyError(device_id)
        current = self._settings.device(device_id)
        value = dataclasses.replace(current, ask_before_receive=ask)
        self._save_settings(self._settings.with_device(device_id, value))

    def set_clipboard(self, device_id: str, enabled: bool) -> None:
        """Bật/tắt “Đồng bộ clipboard” với thiết bị tin cậy `device_id`; lưu.

        Clip chỉ đi khi cả hai máy cùng bật cho nhau (mỗi bên chỉ gửi tới và nhận từ máy mình đã
        bật). Lưu xong mới báo `ClipboardSync.set_enabled` (tắt: bỏ clip đang gửi, đang ráp và chờ
        ghi của máy đó; bật: không gửi bù). Đọc: `device_settings(device_id).clipboard`.
        `device_id` sai → MessageError; không tin cậy → KeyError; `enabled` không phải bool →
        TypeError.
        """
        self._check_running()
        assert self._settings is not None and self._store is not None
        validate_device_id(device_id)
        if type(enabled) is not bool:
            raise TypeError("enabled phải là bool")
        if self._store.get(device_id) is None:
            raise KeyError(device_id)
        current = self._settings.device(device_id)
        value = dataclasses.replace(current, clipboard=enabled)
        self._save_settings(self._settings.with_device(device_id, value))
        if self._clipboard is not None:
            self._clipboard.set_enabled(device_id, enabled)

    def set_visible(self, visible: bool) -> None:
        """Bật/tắt chế độ ẩn (F10) và lưu; không phải bool → TypeError.

        Lưu trước (`SettingsSaveError` thì không đổi gì), rồi đổi luật nhận kết nối ẩn danh ngay
        (`_allows_anonymous`), rồi gửi goodbye hoặc quảng bá lại ở nền (`_apply_visibility`).
        Khi ẩn vẫn duyệt mDNS và tự nối ra máy tin cậy. Không đổi `accept_pairing`.
        """
        self._check_running()
        assert self._settings is not None
        if type(visible) is not bool:
            raise TypeError("visible phải là bool")
        if self._settings.visible != visible:
            self._save_settings(dataclasses.replace(self._settings, visible=visible))
        if visible == self._visible:
            return
        self._visible = visible
        log.info("máy này %s", "hiện với máy chưa ghép đôi" if visible else "đang ẩn")
        if self._mdns_opts is not None and self._mdns_opts.advertise:
            self._spawn(self._apply_visibility(), f"{_TASK_NAME}.visibility")

    def _allows_anonymous(self, ip: str) -> bool:
        """`Listener`: gửi `hello` cho kết nối ẩn danh từ `ip` không (đồng bộ, protocol mục 2.3).

        Hiện, hoặc đang chờ ghép đôi (`accept_pairing`), hoặc máy này đang khởi xướng ghép đôi
        tới `ip` (yêu cầu của máy kia khi hai bên cùng chủ động).
        """
        if self._visible:
            return True
        pairing = self._pairing
        return pairing is not None and (pairing.accepting or pairing.expects_anonymous_from(ip))

    def set_rate_limit(self, value: int | None) -> None:
        """Lưu và áp giới hạn tốc độ gửi (byte/giây; None: không giới hạn). Sai → ValueError."""
        self._check_running()
        assert self._settings is not None
        minimum = app_transfers.MIN_RATE_LIMIT
        if value is not None and (type(value) is not int or value < minimum):
            raise ValueError(f"rate_limit phải là None hoặc số nguyên ≥ {minimum} (byte/giây)")
        self._save_settings(dataclasses.replace(self._settings, rate_limit=value))
        self.transfers.rate_limit = value

    def set_temp_retention_days(self, days: int | None) -> None:
        """Lưu ngưỡng dọn tạm (1–365 ngày; None: mặc định) rồi dọn ngay một lần với ngưỡng mới.

        Ngoài 1–365 → ValueError; không phải số nguyên → TypeError.
        """
        self._check_running()
        assert self._settings is not None
        self._save_settings(dataclasses.replace(self._settings, temp_retention_days=days))
        self.transfers.set_temp_retention(None if days is None else days * 86400.0)
        self._restart_cleanup()

    def set_ui_preferences(
        self,
        *,
        language: str | _Unset | None = _UNSET,
        autostart: bool | _Unset = _UNSET,
        first_run_done: bool | _Unset = _UNSET,
        radar_view: str | _Unset | None = _UNSET,
        theme: str | _Unset | None = _UNSET,
        accent: str | _Unset | None = _UNSET,
        window: WindowGeometry | _Unset | None = _UNSET,
        last_conversation: str | _Unset | None = _UNSET,
        tray_hint_shown: bool | _Unset = _UNSET,
    ) -> bool:
        """Lưu cài đặt của giao diện (chỉ lưu; app không làm gì thêm). Trường không truyền giữ nguyên.

        Trả True khi đã ghi file. Bản mới (đã bỏ mục của thiết bị không tin cậy, `Settings.pruned`)
        bằng bản đang có thì không ghi, trả False: giao diện gọi lặp lúc ẩn xuống khay hay thoát mà
        không gây ghi dồn dập. `last_conversation` của thiết bị không tin cậy bị bỏ như vậy, không
        báo lỗi. Sai theo luật của `Settings` → ValueError / TypeError; ghi lỗi → SettingsSaveError.
        """
        self._check_running()
        assert self._settings is not None
        changes: dict[str, Any] = {
            key: value
            for key, value in (
                ("language", language),
                ("autostart", autostart),
                ("first_run_done", first_run_done),
                ("radar_view", radar_view),
                ("theme", theme),
                ("accent", accent),
                ("window", window),
                ("last_conversation", last_conversation),
                ("tray_hint_shown", tray_hint_shown),
            )
            if value is not _UNSET
        }
        new = dataclasses.replace(self._settings, **changes).pruned(self._trusted_ids())
        if new == self._settings:  # so sánh bỏ qua `extra`, `load_failed`
            return False
        self._save_settings(new)
        return True

    def _asks_before_receive(self, device_id: str) -> bool:
        """`TransferManager`: hỏi trước khi nhận từ `device_id` không (đọc lúc có `offer`).

        Thiết bị không tin cậy luôn hỏi (cài đặt theo thiết bị không có hiệu lực, A5).
        """
        if self._settings is None or self._store is None or self._store.get(device_id) is None:
            return True
        return self._settings.device(device_id).ask_before_receive

    def _clipboard_enabled(self, device_id: str) -> bool:
        """`ClipboardSync`: đồng bộ clipboard với `device_id` không (đọc cài đặt và kho lúc gọi)."""
        settings, store = self._settings, self._store
        if settings is None or store is None or store.get(device_id) is None:
            return False
        return settings.device(device_id).clipboard

    def _clipboard_paused(self) -> bool:
        """`ClipboardSync`: đang tạm dừng nhận (clipboard tới bị bỏ như `offer` mới)."""
        return self._transfers is not None and self._transfers.receiving_paused

    def _before_pair_save(self, device_id: str) -> None:
        """`PairingService` ngay trước `store.add`: ghép đôi (kể cả lại) đặt lại cài đặt theo thiết bị.

        Chỉ ghi khi file có mục của thiết bị; ghi lỗi → SettingsSaveError (ghép đôi `SAVE_FAILED`).
        Tên đang chờ hết trần đổi tên bị bỏ (không đè tên vừa ghép đôi), và lần ghép đôi tính là
        một lần ghi tên: cùng khối đồng bộ với `store.add`, nên hẹn giờ không chen vào giữa. Đồng
        bộ clipboard với máy đó về tắt (ghép đôi lại khi chưa hủy: bỏ clip đang gửi, đang ráp).
        """
        self._drop_pending_name(device_id)
        self._renamed_at[device_id] = self._now()
        if self._settings is not None and device_id in self._settings.devices:
            self._save_settings(self._settings.with_device(device_id, None))
        if self._clipboard is not None:
            self._clipboard.set_enabled(device_id, False)

    def _restart_cleanup(self) -> None:
        """Hẹn giờ dọn tạm lại từ đầu (đồng bộ): dọn ngay một lần (sau khi lần trước xong), rồi theo nhịp.

        Node không có thư mục nhận thì không làm gì.
        """
        transfers = self._transfers
        if transfers is None or transfers.receive_dir is None:
            return
        old = self._cleanup_task
        if old is not None:
            old.cancel()
        self._cleanup_task = asyncio.create_task(
            self._cleanup_temps(transfers, after_previous=True), name=f"{_TASK_NAME}.cleanup-temps"
        )

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

    def connect_hint(self, device_id: str, addresses: Sequence[str], port: int) -> None:
        """Nối tới thiết bị tin cậy tại `addresses:port` (người dùng đưa, không cần mDNS).

        Tracker nhớ địa chỉ này (`LinkTracker.hint`): gỡ chặn, thử ngay nếu chưa có kết nối
        (đang thử thì thử lại tới địa chỉ này khi lần đó thất bại). Lần thử đó không kết nối
        được thì phát `ConnectFailed(UNREACHABLE, from_hint=True)` ngay, một lần cho mỗi lần
        gọi, kể cả khi đang có lịch nối lại (không chờ đủ số lần thất bại). Còn lượt truyền dở
        với thiết bị thì nối lại theo lịch (`hold`). Không có mDNS tươi và không còn lượt dở thì
        không nối lại theo lịch tới địa chỉ này: chỉ thử khi người dùng thao tác (`connect_hint`
        lần nữa), khi có lượt truyền mới, hoặc khi mạng đổi; riêng “mạng đổi” chỉ được theo dõi
        khi `auto_connect=True` và node quảng bá mDNS (`core.links`). Lần thử đó thất bại (không
        tới được, hay là máy khác) mà thiết bị có địa chỉ đã lưu khác thì tracker quên địa chỉ
        này và nối lại tới địa chỉ đã lưu; kết nối được thì địa chỉ này thành địa chỉ đã lưu
        (`_learn_address`). Chạy được cả khi `auto_connect=False`. Node chưa chạy hoặc đang đóng
        → RuntimeError; thiết bị không tin cậy → KeyError; máy này, `device_id`, địa chỉ hay cổng
        sai → ValueError (`device_id` sai → MessageError, lớp con của ValueError).
        """
        if not self._running or self._closing or self._links is None or self._store is None:
            raise RuntimeError("node chưa chạy")
        assert self._identity is not None
        validate_device_id(device_id)
        if device_id == self._identity.device_id:
            raise ValueError("không thể kết nối tới chính máy này")
        if self._store.get(device_id) is None:
            raise KeyError(device_id)
        self._apply(self._links.hint(device_id, addresses, port, self._now()))

    async def connect_address(self, device_id: str, ip: str, port: int | None = None) -> ConnectResult:
        """“Kết nối bằng IP…” cho một máy đã ghép đôi: thử `ip:port` và trả kết quả.

        `port=None`: cổng trong địa chỉ đã lưu của thiết bị, không có thì cổng mDNS đang thấy,
        cuối cùng `DEFAULT_PORT` (đọc lúc gọi). Lỗi tham số như `connect_hint`: node chưa chạy
        hay đang đóng → RuntimeError; không tin cậy → KeyError; máy này, địa chỉ, cổng sai
        (`core.links.check_endpoint`) → ValueError; `device_id` sai → MessageError.

        Đã có kết nối điều khiển → `OK` ngay, không mở kết nối, không đổi `hint`. Chưa có thì gọi
        `connect_hint(device_id, [ip], port)` rồi chờ tối đa `2 * OPENING_TIMEOUT + 1` giây (đọc
        lúc gọi): kết nối được (có thể qua đường khác, ví dụ máy kia gọi tới) → `OK`; lần thử tới
        đúng địa chỉ này thất bại → kết quả của nó (`IDENTITY_MISMATCH` khi địa chỉ là của máy
        khác: không lưu gì); hết giờ → `UNREACHABLE`. Trong lúc chờ: thiết bị thôi tin cậy →
        KeyError; `close()` → RuntimeError. Bị hủy thì chỉ thôi chờ, lần thử vẫn chạy. Kết nối
        được thì địa chỉ được lưu như mọi kết nối ra (`_learn_address`; không lưu địa chỉ
        Internet công khai).
        """
        if not self._running or self._closing or self._links is None or self._store is None:
            raise RuntimeError("node chưa chạy")
        assert self._identity is not None and self._registry is not None
        validate_device_id(device_id)
        if device_id == self._identity.device_id:
            raise ValueError("không thể kết nối tới chính máy này")
        device = self._store.get(device_id)
        if device is None:
            raise KeyError(device_id)
        if port is None:
            view = self._registry.get(device_id)
            if device.last_address is not None:
                port = device.last_address[1]
            elif view is not None and view.port is not None:
                port = view.port
            else:
                port = DEFAULT_PORT
        ip, port = check_endpoint(ip, port)
        if device_id in self._links.connected():
            return ConnectResult.OK
        future: asyncio.Future[ConnectResult] = asyncio.get_running_loop().create_future()
        waiter = (ip, port, future)
        # Đăng ký trước khi gọi `connect_hint`: kết quả chỉ tới qua `_notify`, sau lời gọi này.
        self._address_waiters.setdefault(device_id, []).append(waiter)
        try:
            self.connect_hint(device_id, [ip], port)
            try:
                async with asyncio.timeout(2 * net_connection.OPENING_TIMEOUT + 1):
                    return await future
            except TimeoutError:
                return ConnectResult.UNREACHABLE
        finally:
            self._remove_address_waiter(device_id, waiter)

    def _remove_address_waiter(
        self, device_id: str, waiter: tuple[str, int, asyncio.Future[ConnectResult]]
    ) -> None:
        waiters = self._address_waiters.get(device_id)
        if waiters is None:
            return
        with contextlib.suppress(ValueError):
            waiters.remove(waiter)
        if not waiters:
            del self._address_waiters[device_id]

    def _settle_address_waiters(self, event: LinkEvent) -> None:
        """Hoàn tất các `connect_address` đang chờ theo một sự kiện của tracker (đồng bộ).

        `LinkUp` / `LinkSwitched` → `OK` cho mọi lời gọi của thiết bị; `LinkFailed` của lần thử do
        `hint` → kết quả của nó cho lời gọi có `ip` nằm trong đích của lần thử đó và cùng cổng.
        """
        waiters = self._address_waiters.get(event.device_id)
        if not waiters:
            return
        for ip, port, future in list(waiters):
            if future.done():
                continue
            if isinstance(event, LinkUp | LinkSwitched):
                future.set_result(ConnectResult.OK)
            elif (
                isinstance(event, LinkFailed)
                and event.from_hint
                and ip in event.addresses
                and port == event.port
            ):
                future.set_result(event.result)

    def _fail_address_waiters(self, device_ids: Iterable[str] | None = None) -> None:
        """`connect_address` đang chờ: thiết bị thôi tin cậy → KeyError; node đóng (`None`) → RuntimeError."""
        ids = list(self._address_waiters) if device_ids is None else list(device_ids)
        for device_id in ids:
            for _, _, future in self._address_waiters.pop(device_id, []):
                if not future.done():
                    error = RuntimeError("node đang đóng") if device_ids is None else KeyError(device_id)
                    future.set_exception(error)

    # ---- Hủy ghép đôi ----------------------------------------------------------------------

    async def unpair(self, device_id: str, *, clear_history: bool = False) -> TrustedDevice:
        """Hủy ghép đôi: xóa khỏi kho, đổi danh sách tin cậy của cổng, đóng kết nối của máy đó.

        Không có `device_id` → KeyError; không ghi được → TrustStoreError (không đổi gì).
        Khi trả về, máy đó không còn trong `connected_devices()`, lần thử kết nối tới nó
        đã bị hủy, mọi lượt truyền với nó đã bị hủy (kết quả `UNPAIRED`; máy kia không nhận
        `cancel` vì kết nối đã mất, nó kết thúc lượt bằng `EXPIRED`), mục cài đặt theo thiết bị
        của nó và `last_conversation` nếu là nó đã bị xóa khỏi `settings.json` (ghi lỗi thì chỉ
        WARNING: chúng không có hiệu lực và bị bỏ ở lần ghi sau), và kết nối của nó đã bắt đầu
        đóng (chờ tối đa UNPAIR_CLOSE_TIMEOUT). Node chưa chạy → RuntimeError.

        `clear_history`: xóa luôn lịch sử với máy đó (mvp F5), gồm cả các mục `UNPAIRED` vừa
        sinh; mặc định giữ (mục của máy đã hủy ghép đôi còn tới khi xóa toàn bộ lịch sử).
        """
        if not self._running:
            raise RuntimeError("node chưa chạy")
        # Đồng bộ từ đây tới lúc lấy kết nối ra khỏi bảng và hủy các lượt: không có await xen giữa.
        device = self.store.remove(device_id)
        if self._settings is not None and _mentions_device(self._settings, device.device_id):
            try:
                # `_save_settings` bỏ cả `last_conversation` (máy đó đã ra khỏi kho: `pruned`).
                self._save_settings(self._settings.with_device(device.device_id, None))
            except SettingsSaveError as e:
                log.warning(
                    "không xóa được cài đặt theo thiết bị trong settings.json (%s)",
                    type(e.__cause__).__name__,
                )
        closes = self._trust_changed()
        if self._transfers is not None:
            self._transfers.forget_device(device.device_id)
            if clear_history:  # sau `forget_device`: các mục `UNPAIRED` của nó cũng bị xóa
                self._transfers.clear_history(device.device_id)
        if self._clipboard is not None:
            self._clipboard.forget_device(device.device_id)
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
        self._forget_peer_names()
        self._forget_addresses()
        self._fail_address_waiters([d for d in self._address_waiters if self._store.get(d) is None])
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
        elif conn.kind is ConnectionKind.DATA:
            # `serve_data` báo engine trước `await` đầu tiên (cùng khối đồng bộ với lần kiểm
            # `is_trusted` của Listener), rồi giữ handler tới khi kết nối dữ liệu xong.
            if self._transfers is None:
                conn.abort()
            else:
                await self._transfers.serve_data(conn)

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
            on_message = functools.partial(self._on_control_message, device_id, conn_id)
            reason = await ControlChannel(conn).run(on_message)
            log.debug("kết nối điều khiển %d với %s kết thúc: %s", conn_id, device_id[:8], reason.value)
        finally:
            self._conns.pop(conn_id, None)
            # Báo cả khi đang đóng, để `connected_devices()` rỗng sau `close()`; lúc đó
            # `_emit` không phát sự kiện và `_apply` không mở kết nối mới.
            if self._links is not None:
                self._apply(self._links.connection_lost(device_id, conn_id, self._now()))

    def _on_control_message(self, device_id: str, conn_id: int, msg: object) -> None:
        """Message trên kết nối điều khiển (đồng bộ): `ping`/`pong` bỏ qua, `clipboard` →
        `ClipboardSync` (không có thì bỏ qua), còn lại → `TransferManager`."""
        if isinstance(msg, Ping | Pong):
            return
        if isinstance(msg, Clipboard):
            if self._clipboard is not None:
                try:
                    self._clipboard.on_message(device_id, conn_id, msg)
                except Exception:
                    log.exception("lỗi khi xử lý clipboard từ %s", device_id[:8])
            return
        if self._transfers is None:
            return
        try:
            self._transfers.on_message(device_id, conn_id, msg)
        except Exception:
            log.exception("lỗi khi xử lý message điều khiển từ %s", device_id[:8])

    def _transfer_hold(self, device_id: str, held: bool) -> None:
        """`TransferManager`: thiết bị có/hết lượt truyền dở → giữ/thả địa chỉ ở tracker."""
        if self._links is not None:
            # `hold` chỉ trả `Connect` (không `Notify`), nên không sinh lời gọi lồng vào
            # TransferManager.
            self._apply(self._links.hold(device_id, held, self._now()))

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
                    list(action.addresses), action.port, self._identity, self._hello, device.cert_pem
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
        # Thành công: không await cho tới khi đăng ký xong. Chứng chỉ server đã được kiểm
        # trong bắt tay, nên IP đó được ưu tiên ở cổng của máy này (protocol §2.3).
        priority = self._note_outgoing(conn)
        try:
            self._attempts.pop(attempt_id, None)
            if self._closing or self._links is None:
                return
            # Trước `connect_finished`: `DeviceConnected` (phát trong đó) đã thấy địa chỉ mới.
            self._learn_address(device_id, conn)
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
            self._release_outgoing(priority)
            if not conn.closed:
                await conn.close()

    def _note_outgoing(self, conn: Connection) -> int:
        """Báo cổng có kết nối điều khiển ra tới `conn.peer.address`; trả handle (0: không cấp)."""
        if self._listener is None:
            return 0
        try:
            fingerprint = conn.peer.fingerprint
            if fingerprint is None:  # không xảy ra: phía client luôn có chứng chỉ server
                return 0
            return self._listener.note_outgoing_control(conn.peer.address, fingerprint)
        except Exception:
            log.exception("không ghi nhận được kết nối ra cho giới hạn kết nối vào")
            return 0

    def _release_outgoing(self, handle: int) -> None:
        if self._listener is None or not handle:
            return
        try:
            self._listener.release_outgoing_control(handle)
        except Exception:
            log.exception("không trả được ưu tiên của kết nối ra")

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
                attempt = self._attempts.pop(action.attempt_id, None)
                if attempt is not None:
                    attempt.cancel()
            elif isinstance(action, CloseConnection):
                conn = self._conns.pop(action.conn_id, None)
                if conn is not None:
                    closes.append(self._spawn(conn.close(), f"{_TASK_NAME}.close {action.device_id[:8]}"))
            elif isinstance(action, Notify):
                self._notify(action.event)
        self._wake_timer()
        return closes

    def _notify(self, event: LinkEvent) -> None:
        # `connect_address` đang chờ: trước và độc lập với registry / `_emit`.
        try:
            self._settle_address_waiters(event)
        except Exception:
            log.exception("không hoàn tất được lời gọi connect_address của %s", event.device_id[:8])
        assert self._registry is not None
        registry = self._registry
        if isinstance(event, LinkSwitched):
            # Thiết bị vẫn kết nối: không phát NodeEvent, chỉ ghi DEBUG; kênh điều khiển của
            # truyền file chuyển sang kết nối mới (engine đồng bộ lại, protocol §6.3).
            log.debug(
                "đổi kết nối điều khiển với %s: %d → %d",
                event.device_id[:8],
                event.old_conn_id,
                event.conn_id,
            )
            self._refresh_peer(event.device_id, self._conns.get(event.conn_id))
            self._transfer_control_up(event.device_id, event.conn_id, event.outgoing)
            self._clipboard_link_up(event.device_id, event.conn_id)
        elif isinstance(event, LinkUp):
            # Trước `_transfer_control_up` và `DeviceConnected`: offer và view mang tên, `os` mới.
            self._refresh_peer(event.device_id, self._conns.get(event.conn_id))
            self._transfer_control_up(event.device_id, event.conn_id, event.outgoing)
            self._clipboard_link_up(event.device_id, event.conn_id)
            registry.set_connected(event.device_id, True)
            log.info("đã kết nối với %s", event.device_id[:8])
            view = registry.get(event.device_id)
            if view is not None:
                self._emit(DeviceConnected(view, event.outgoing))
        elif isinstance(event, LinkDown):
            if self._transfers is not None:
                self._transfers.control_lost(event.device_id, event.conn_id)
            if self._clipboard is not None:
                try:
                    self._clipboard.link_down(event.device_id, event.conn_id)
                except Exception:
                    log.exception("lỗi khi báo mất kết nối cho đồng bộ clipboard")
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
                self._emit(ConnectFailed(view, event.result, event.from_hint))

    def _refresh_peer(self, device_id: str, conn: Connection | None) -> None:
        """Kết nối điều khiển hiện tại của máy đã ghép đôi vừa có hay vừa đổi (đồng bộ): nhớ
        `hello.os`, và ghi `hello.name` vào `trusted.json` nếu máy đó đã đổi tên.

        Chỉ áp khi node không đang đóng, thiết bị còn trong kho và fingerprint của `conn` khớp
        chứng chỉ đã lưu: kết nối điều khiển đã xác thực hai chiều, nên chỉ chính máy đó đổi được
        tên của nó. Không có `await` từ lúc kiểm tới lúc ghi. Đổi tên không đổi tin cậy: không gọi
        `_trust_changed` (cổng, tracker, mDNS giữ nguyên).

        Giá trị trả về của `set_peer_hello` bị bỏ qua: ở `LinkSwitched`, `os` đổi mà tên không đổi
        thì không có sự kiện nào (view mới thấy ở lần đọc sau). Chấp nhận được: `os` chỉ để hiển
        thị, và một máy đổi hệ điều hành mà vẫn giữ danh tính là chuyện hiếm.
        """
        if conn is None or self._closing or self._store is None or self._registry is None:
            return
        device = self._store.get(device_id)
        if device is None or conn.peer.fingerprint != device.fingerprint:
            return
        hello = conn.peer.hello
        try:
            self._registry.set_peer_hello(device_id, os=hello.os)
            self._rename_peer(device, hello.name)
        except Exception:  # không để lỗi ở đây cắt ngang `_apply` của tracker
            log.exception("không cập nhật được thông tin của %s", device_id[:8])

    def _rename_peer(self, device: TrustedDevice, name: str) -> None:
        """Ghi tên mới (đã xác thực) của `device`, tối đa một lần mỗi PEER_RENAME_MIN_INTERVAL.

        Bị trần chặn thì nhớ tên đó và hẹn áp khi hết trần (`_apply_pending_name`). Lỗi ghi chỉ
        WARNING: kết nối vẫn chạy, lần thử vẫn tính vào trần (máy nối lại liên tục không biến loop
        thành vòng ghi đĩa).
        """
        assert self._store is not None and self._registry is not None
        device_id = device.device_id
        if name == device.name:
            self._drop_pending_name(device_id)
            return
        now = self._now()
        interval = PEER_RENAME_MIN_INTERVAL
        last = self._renamed_at.get(device_id)
        if last is not None and now - last < interval:
            self._pending_names[device_id] = name
            if device_id not in self._rename_timers:
                self._rename_timers[device_id] = asyncio.get_running_loop().call_later(
                    last + interval - now, self._apply_pending_name, device_id
                )
            return
        self._drop_pending_name(device_id)
        self._renamed_at[device_id] = now
        try:
            self._store.rename(device_id, name)
        except (MessageError, KeyError, TrustStoreError) as e:
            log.warning("không lưu được tên mới của %s (%s)", device_id[:8], type(e).__name__)
            return
        self._registry.set_trusted(self._store.devices())
        log.info("%s đổi tên, đã cập nhật trusted.json", device_id[:8])
        view = self._registry.get(device_id)
        if view is not None:
            self._emit(DeviceRenamed(view, device.name))

    def _drop_pending_name(self, device_id: str) -> None:
        self._pending_names.pop(device_id, None)
        timer = self._rename_timers.pop(device_id, None)
        if timer is not None:
            timer.cancel()

    def _apply_pending_name(self, device_id: str) -> None:
        """Hẹn giờ của tên bị trần chặn: áp nếu kết nối điều khiển HIỆN TẠI vẫn đúng chứng chỉ đã lưu."""
        self._rename_timers.pop(device_id, None)
        name = self._pending_names.pop(device_id, None)
        if name is None or self._closing or self._store is None or self._links is None:
            return
        device = self._store.get(device_id)
        conn_id = self._links.connected().get(device_id)
        conn = None if conn_id is None else self._conns.get(conn_id)
        if device is None or conn is None or conn.peer.fingerprint != device.fingerprint:
            return
        try:
            self._rename_peer(device, name)
        except Exception:
            log.exception("không áp được tên mới của %s", device_id[:8])

    def _forget_peer_names(self) -> None:
        """Bỏ trạng thái đổi tên của thiết bị không còn tin cậy (gọi sau khi kho đổi)."""
        if self._store is None:
            return
        for device_id in list(self._renamed_at.keys() | self._pending_names.keys()):
            if self._store.get(device_id) is None:
                self._renamed_at.pop(device_id, None)
                self._drop_pending_name(device_id)

    def _cancel_rename_timers(self) -> None:
        for timer in self._rename_timers.values():
            timer.cancel()
        self._rename_timers.clear()
        self._pending_names.clear()

    # ---- Địa chỉ đã lưu (protocol §6.2, gạch “Địa chỉ đã lưu”) ---------------------------------

    def _remember_saved_addresses(self) -> None:
        """Lúc khởi động: đưa địa chỉ đã lưu (`trusted.json`) của thiết bị `_auto_target` vào tracker.

        Bỏ qua địa chỉ Internet công khai (`is_saveable`). Lỗi của một thiết bị chỉ ghi log.
        """
        if self._store is None or self._links is None:
            return
        for device in self._store.devices():
            address = device.last_address
            if address is None or not self._auto_target(device.device_id) or not is_saveable(address[0]):
                continue
            try:
                self._apply(self._links.remember(device.device_id, address[0], address[1], self._now()))
            except Exception:
                log.exception("không nạp được địa chỉ đã lưu của %s", device.device_id[:8])

    def _learn_address(self, device_id: str, conn: Connection) -> None:
        """Kết nối điều khiển RA vừa mở xong tới thiết bị tin cậy (đồng bộ, không bao giờ raise).

        Chỉ học khi node không đang đóng, thiết bị còn trong kho và fingerprint của `conn` khớp
        chứng chỉ đã lưu (kiểm ngay trước khi ghi, không có `await` xen giữa), và địa chỉ không
        phải Internet công khai. Địa chỉ là IP thắng cuộc đua mở kết nối và cổng ta đã gọi tới,
        tức cổng nghe thật (kết nối vào chỉ có cổng nguồn tạm nên không học). Thiết bị
        `_auto_target` thì đưa vào tracker (`remember`); mọi node đều ghi `trusted.json`
        (`_save_address`). IP chỉ ở log DEBUG.
        """
        try:
            if self._closing or self._store is None or self._links is None:
                return
            device = self._store.get(device_id)
            if device is None or conn.peer.fingerprint != device.fingerprint:
                return
            ip, port = conn.peer.address, conn.peer.port
            if not is_saveable(ip):
                return
            if self._auto_target(device_id):
                self._apply(self._links.remember(device_id, ip, port, self._now()))
            self._save_address(device, ip, port)
        except Exception:
            log.exception("không ghi nhận được địa chỉ của %s", device_id[:8])

    def _save_address(self, device: TrustedDevice, ip: str, port: int) -> None:
        """Ghi địa chỉ mới của `device`, tối đa một lần mỗi ADDRESS_SAVE_MIN_INTERVAL.

        Không đổi thì không ghi. Bị trần chặn thì nhớ địa chỉ (kèm fingerprint) và hẹn áp khi hết
        trần (`_apply_pending_address`). Lỗi ghi chỉ WARNING (không có IP); lần thử vẫn tính vào
        trần. Đổi địa chỉ không phát `NodeEvent`.
        """
        assert self._store is not None and self._registry is not None
        device_id = device.device_id
        if device.last_address == (ip, port):
            self._drop_pending_address(device_id)
            return
        now = self._now()
        interval = ADDRESS_SAVE_MIN_INTERVAL
        last = self._address_saved_at.get(device_id)
        if last is not None and now - last < interval:
            self._pending_addresses[device_id] = (ip, port, device.fingerprint)
            if device_id not in self._address_timers:
                self._address_timers[device_id] = asyncio.get_running_loop().call_later(
                    last + interval - now, self._apply_pending_address, device_id
                )
            return
        self._drop_pending_address(device_id)
        self._address_saved_at[device_id] = now
        try:
            self._store.set_last_address(device_id, ip, port)
        except (ValueError, KeyError, TrustStoreError) as e:
            log.warning("không lưu được địa chỉ của %s (%s)", device_id[:8], type(e).__name__)
            return
        self._registry.set_trusted(self._store.devices())
        log.debug("đã lưu địa chỉ %s:%d của %s", ip, port, device_id[:8])

    def _drop_pending_address(self, device_id: str) -> None:
        self._pending_addresses.pop(device_id, None)
        timer = self._address_timers.pop(device_id, None)
        if timer is not None:
            timer.cancel()

    def _apply_pending_address(self, device_id: str) -> None:
        """Hẹn giờ của địa chỉ bị trần chặn: áp nếu node chưa đóng và thiết bị còn fingerprint lúc học."""
        self._address_timers.pop(device_id, None)
        pending = self._pending_addresses.pop(device_id, None)
        if pending is None or self._closing or self._store is None or self._registry is None:
            return
        ip, port, fingerprint = pending
        device = self._store.get(device_id)
        if device is None or device.fingerprint != fingerprint:
            return
        try:
            self._save_address(device, ip, port)
        except Exception:
            log.exception("không áp được địa chỉ mới của %s", device_id[:8])

    def _forget_addresses(self) -> None:
        """Bỏ trạng thái ghi địa chỉ của thiết bị không còn tin cậy (gọi sau khi kho đổi)."""
        if self._store is None:
            return
        for device_id in list(self._address_saved_at.keys() | self._pending_addresses.keys()):
            if self._store.get(device_id) is None:
                self._address_saved_at.pop(device_id, None)
                self._drop_pending_address(device_id)

    def _cancel_address_timers(self) -> None:
        for timer in self._address_timers.values():
            timer.cancel()
        self._address_timers.clear()
        self._pending_addresses.clear()

    def _transfer_control_up(self, device_id: str, conn_id: int, outgoing: bool) -> None:
        """Kết nối điều khiển hiện tại của thiết bị vừa có hay vừa đổi → `TransferManager`."""
        if self._transfers is None:
            return
        conn = self._conns.get(conn_id)
        if conn is None:
            log.error("kết nối điều khiển %d với %s không có trong bảng", conn_id, device_id[:8])
            return
        self._transfers.control_up(device_id, conn_id, conn, client=outgoing)

    def _clipboard_link_up(self, device_id: str, conn_id: int) -> None:
        """Kết nối điều khiển hiện tại của thiết bị vừa có hay vừa đổi → `ClipboardSync`."""
        conn = self._conns.get(conn_id)
        if self._clipboard is None or conn is None:
            return
        try:
            self._clipboard.link_up(device_id, conn_id, conn)
        except Exception:
            log.exception("lỗi khi báo kết nối cho đồng bộ clipboard")

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

    @staticmethod
    async def _cleanup_temps(transfers: TransferManager, *, after_previous: bool = False) -> None:
        """Hẹn giờ dọn tạm: lần đầu ngay sau khi mở cổng, rồi mỗi `CLEANUP_INTERVAL` (đọc lúc dùng).

        `after_previous` (`_restart_cleanup`): chờ lần dọn trước thật sự xong rồi mới dọn lần đầu.
        """
        if after_previous:
            await transfers.cleanup_settled()
        while True:
            try:
                await transfers.cleanup_temps()
            except Exception:  # cleanup_temps không ném (trừ CancelledError): phòng thủ
                log.exception("dọn tạm lỗi")
            await asyncio.sleep(app_transfers.CLEANUP_INTERVAL)

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
        if opts.advertise and self._visible:
            await self._advertise(m)
        else:
            self._mdns_recovered()

    async def _advertise(self, m: MdnsDiscovery) -> None:
        """Quảng bá (hoặc quảng bá lại) máy này; lỗi → `discovery_error`, vẫn duyệt.

        Đang ẩn thì không làm gì: mọi đường quảng bá đi qua đây (`_start_mdns`, `_readvertise`,
        `_apply_visibility`), và trừ lúc `start` đều giữ `_mdns_lock`.
        """
        if not self._visible:
            return
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

    def _auto_target(self, device_id: str) -> bool:
        """Địa chỉ mDNS và địa chỉ đã lưu của thiết bị có được đưa vào tracker không (`auto_connect`,
        `auto_connect_to`). Địa chỉ đã lưu vẫn được học và ghi cho mọi thiết bị."""
        return self._auto_connect and self._auto_connect_to in (None, device_id)

    def _on_discovery_event(self, source: MdnsDiscovery, event: DeviceEvent) -> None:
        """Sự kiện của `MdnsDiscovery` (đồng bộ): registry, tracker, rồi `on_event`.

        Registry nhận mọi thiết bị; tracker chỉ nhận thiết bị `_auto_target`. Các
        `device_gone` của `_restart_mdns` và `network_changed` vẫn chạy cho mọi thiết bị:
        vô hại, vì tracker không có địa chỉ mDNS của thiết bị bị lọc ở đây.
        """
        if self._closing or source is not self._mdns or self._registry is None or self._links is None:
            return  # instance cũ (sau khi khởi động lại) hoặc node đang đóng
        registry, links = self._registry, self._links
        now = self._now()
        if isinstance(event, DeviceAdded | DeviceUpdated):
            device = event.device
            registry.seen(device)
            if self._auto_target(device.device_id):
                self._apply(links.device_seen(device.device_id, device.addresses, device.port, now))
            if isinstance(event, DeviceAdded):
                view = registry.get(device.device_id)
                if view is not None:
                    self._emit(DeviceOnline(view))
        elif isinstance(event, DeviceRemoved):
            device = event.device
            before = registry.get(device.device_id)
            registry.gone(device.device_id)
            if self._auto_target(device.device_id):
                self._apply(links.device_gone(device.device_id, now))
            self._emit(DeviceOffline(self._offline_view(device, before)))

    def _offline_view(self, device: DiscoveredDevice, before: DeviceView | None) -> DeviceView:
        """View cuối của một thiết bị vừa rời mạng (online=False, không địa chỉ, cổng).

        Máy đã ghép đôi vẫn có view trong registry, giữ `os` từ `hello` đã nhớ. Không còn
        view thì máy đó chưa ghép đôi: `os` của nó chỉ đến từ mDNS nên bỏ.
        """
        assert self._registry is not None
        after = self._registry.get(device.device_id)
        if after is not None:
            return after
        return _gone_view(
            before or DeviceView(device.device_id, device.name, None, False, False, False, (), None)
        )

    async def _netwatch(self) -> None:
        """Cứ NETWORK_CHECK_INTERVAL giây: địa chỉ đổi → khởi động lại mDNS; thức dậy → quảng bá lại.

        Đang hiện mà lần quảng bá trước lỗi (`discovery_error`, mDNS vẫn chạy) → thử quảng bá lại.
        """
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
            elif self._visible and self._discovery_error is not None:
                # Lần quảng bá trước lỗi (mDNS vẫn chạy): thử lại mỗi nhịp, không kẹt ở trạng thái
                # "hiện" mà không quảng bá. Lỗi lặp lại chỉ ghi DEBUG (`_log_mdns_error`).
                await self._readvertise()
            if change is not NetworkChange.NONE and self._auto_connect and self._links is not None:
                self._apply(self._links.network_changed(self._now()))

    async def _readvertise(self) -> None:
        async with self._mdns_lock:
            m = self._mdns
            if m is not None and not self._closing and self._visible:
                await self._advertise(m)

    async def _apply_visibility(self) -> None:
        """Áp `_visible` cho mDNS: ẩn → goodbye (`stop_advertising`), hiện → quảng bá lại.

        Chạy dưới `_mdns_lock`, nên quảng bá đang bay lúc gọi `set_visible(False)` kết thúc bằng
        goodbye này. Sau mỗi `await` kiểm lại `_visible`: đổi trong lúc chờ thì làm lại theo giá
        trị mới, tới khi khớp. mDNS đang tắt thì thôi (`_start_mdns` của netwatch đọc `_visible`).
        """
        async with self._mdns_lock:
            applied: bool | None = None
            while not self._closing and self._mdns is not None and applied is not self._visible:
                m = self._mdns
                applied = self._visible
                if applied:
                    await self._advertise(m)
                    continue
                try:
                    await m.stop_advertising()
                except Exception as e:
                    self._log_mdns_error("không ngừng quảng bá được qua mDNS", e)
                    self._discovery_error = e
                    continue
                self._discovery_error = None  # lỗi quảng bá cũ không còn ý nghĩa khi đã ẩn
                self._mdns_recovered()

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
                    # Máy đã ghép đôi còn view (giữ `os` từ `hello`); không còn thì chưa ghép đôi.
                    after = self._registry.get(device_id)
                    self._emit(DeviceOffline(after or _gone_view(view)))
            if old is not None:
                await old.close()
                self._mdns_closing.discard(old)
            if not self._closing:
                await self._start_mdns()
