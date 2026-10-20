"""Quảng bá và tìm thiết bị Teleport trong LAN bằng mDNS/DNS-SD (thư viện zeroconf).

Đặc tả ở docs/protocol.md mục 8. Module này chỉ làm I/O: đăng ký/cập nhật/gỡ dịch
vụ, chạy `AsyncServiceBrowser`, resolve các tên mà `teleport.core.discovery.DiscoveryTracker`
yêu cầu và đọc địa chỉ card mạng. Mọi quyết định (bản ghi nào hợp lệ, thiết bị nào
thêm/sửa/xóa, khi nào kiểm còn sống, địa chỉ nào quảng bá) nằm ở `core.discovery`.

Mọi callback chạy trong event loop của asyncio; `MdnsDiscovery` không an toàn đa luồng.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import socket
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Self

import ifaddr
from zeroconf import InterfaceChoice, IPVersion, NonUniqueNameException, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from teleport.core.discovery import (
    MAX_TRACKED_SERVICES,
    PROBE_INTERVAL,
    RECORD_TTL,
    RESOLVE_TIMEOUT,
    SERVICE_TYPE,
    DeviceEvent,
    DiscoveredDevice,
    DiscoveryTracker,
    InterfaceAddress,
    ResolvedService,
    TrackerOutput,
    build_txt,
    device_id_from_service_name,
    filter_addresses,
    select_local_addresses,
    service_name,
)
from teleport.core.messages import MessageError, validate_device_id, validate_device_name, validate_os

PROBE_TICK = 5.0  # giây giữa hai lần hỏi tracker dịch vụ nào đến hạn kiểm còn sống
MAX_CONCURRENT_RESOLVES = 16  # resolve chạy cùng lúc; hàng `resolve` được ưu tiên hơn hàng `probe`
# Số chỗ luôn dành cho kiểm còn sống: một loạt thiết bị mới (hoặc kẻ phá spam PTR) không
# làm bỏ đói việc kiểm thiết bị đã biết.
PROBE_RESERVED_SLOTS = 4

# Nhóm multicast mDNS IPv4: địa chỉ nguồn của tuyến tới đây là card zeroconf sẽ dùng chính.
_MDNS_GROUP = ("224.0.0.251", 5353)
# Đích bất kỳ ngoài LAN (TEST-NET-1, RFC 5737) để hỏi tuyến mặc định; không có gói nào được gửi.
_DEFAULT_ROUTE_PROBE = ("192.0.2.1", 9)

log = logging.getLogger(__name__)


class DiscoveryError(Exception):
    """Lỗi khám phá người dùng xử lý được: không có IPv4 để quảng bá, trùng tên instance."""


def _check_port(port: object) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port phải là số nguyên 1–65535")
    return port


def build_service_info(
    *, device_id: str, name: str, os: str, port: int, addresses: Sequence[str]
) -> AsyncServiceInfo:
    """`AsyncServiceInfo` để quảng bá máy này (SRV, TXT, A; mọi bản ghi TTL `RECORD_TTL`)."""
    return AsyncServiceInfo(
        SERVICE_TYPE,
        service_name(device_id),
        port=_check_port(port),
        properties=build_txt(device_id, name, os),
        server=f"teleport-{device_id}.local.",
        host_ttl=RECORD_TTL,
        other_ttl=RECORD_TTL,
        parsed_addresses=list(addresses),
    )


def _route_source(dest: tuple[str, int]) -> str | None:
    """Địa chỉ nguồn mà hệ điều hành chọn khi gửi tới `dest`; không xác định được → None.

    `connect` của socket UDP không gửi gói nào, chỉ chọn tuyến.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(dest)
            ip = sock.getsockname()[0]
    except OSError:
        return None
    return None if ip == "0.0.0.0" else ip


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.IPv4Address(ip).is_loopback
    except ValueError:
        return True  # không phải IPv4 hợp lệ: coi như không dùng được


def _preferred_source() -> str | None:
    """Địa chỉ nên quảng bá đầu tiên (protocol mục 2.1); không xác định được → None.

    Thử nguồn của tuyến tới nhóm mDNS; nếu không có hoặc là loopback (Windows
    thường chọn 127.0.0.1 cho tuyến multicast) thì thử nguồn của tuyến mặc định.
    """
    for dest in (_MDNS_GROUP, _DEFAULT_ROUTE_PROBE):
        ip = _route_source(dest)
        if ip is not None and not _is_loopback(ip):
            return ip
    return None


def local_ipv4_addresses() -> tuple[str, ...]:
    """Địa chỉ IPv4 của máy này để quảng bá (chọn bởi `core.discovery.select_local_addresses`).

    Hàm chặn (đọc bảng card mạng); trong event loop hãy gọi qua `asyncio.to_thread`.
    """
    interfaces = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if isinstance(ip.ip, str):  # IPv4; IPv6 là tuple (ip, flowinfo, scope_id)
                names = tuple(
                    n for n in (adapter.name, adapter.nice_name, ip.nice_name) if isinstance(n, str)
                )
                interfaces.append(InterfaceAddress(ip.ip, names))
    return select_local_addresses(interfaces, _preferred_source())


def _check_seconds(name: str, value: object) -> float:
    """Tham số thời gian: số hữu hạn > 0 (giây)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} phải là số giây")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} phải là số hữu hạn > 0")
    return float(value)


def _check_priority_ids(device_ids: Iterable[str]) -> tuple[str, ...]:
    """Danh sách device_id ưu tiên; phần tử sai (hoặc truyền một chuỗi) → MessageError."""
    if isinstance(device_ids, str | bytes):
        raise MessageError("tập ưu tiên phải là một tập device_id, không phải một chuỗi")
    return tuple(validate_device_id(device_id) for device_id in device_ids)


class MdnsDiscovery:
    """Quảng bá máy này và theo dõi các máy Teleport khác qua mDNS.

    Dùng một lần: `start()` (hoặc `async with`) rồi `advertise` / `start_browsing`,
    cuối cùng `close()`. `on_event` được gọi đồng bộ trong event loop, đúng thứ tự
    tracker trả về; sau khi `close()` trả về thì không còn sự kiện nào.
    """

    def __init__(
        self,
        *,
        interfaces: Sequence[str] | None = None,
        probe_interval: float = PROBE_INTERVAL,
        resolve_timeout: float = RESOLVE_TIMEOUT,
        probe_tick: float = PROBE_TICK,
    ) -> None:
        """Tham số thời gian (giây) phải là số hữu hạn > 0; sai → TypeError / ValueError."""
        # None = mọi card (InterfaceChoice.All); test truyền ["127.0.0.1"].
        self._interfaces = list(interfaces) if interfaces is not None else None
        self._probe_interval = _check_seconds("probe_interval", probe_interval)
        self._resolve_timeout = _check_seconds("resolve_timeout", resolve_timeout)
        self._probe_tick = _check_seconds("probe_tick", probe_tick)
        self._priority_ids: tuple[str, ...] = ()  # thiết bị đã ghép đôi, xem `set_priority`
        self._aiozc: AsyncZeroconf | None = None
        self._started = False
        self._closed = False
        self._advertise_lock = asyncio.Lock()
        self._info: AsyncServiceInfo | None = None
        self._tracker: DiscoveryTracker | None = None
        self._on_event: Callable[[DeviceEvent], None] | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._tick_task: asyncio.Task[None] | None = None
        # Mỗi tên có tối đa một việc: hoặc đang chạy, hoặc nằm trong đúng một hàng đợi.
        self._resolving: dict[str, asyncio.Task[None]] = {}
        self._resolving_first: set[str] = set()  # tên đang chạy lấy từ hàng `resolve`
        self._close_task: asyncio.Future[None] | None = None
        self._close_done = asyncio.Event()  # đặt khi socket mDNS đã đóng xong
        self._resolve_queue: deque[str] = deque()  # resolve lần đầu / sau thay đổi
        self._probe_queue: deque[str] = deque()  # kiểm còn sống

    async def start(self) -> None:
        """Mở socket mDNS (chỉ IPv4). Gọi lần hai, hoặc sau `close()` → RuntimeError."""
        if self._started or self._closed:
            raise RuntimeError("MdnsDiscovery chỉ start được một lần")
        self._started = True
        interfaces = self._interfaces if self._interfaces is not None else InterfaceChoice.All
        self._aiozc = AsyncZeroconf(interfaces=interfaces, ip_version=IPVersion.V4Only)

    async def close(self) -> None:
        """Ngừng duyệt, gửi goodbye, đóng socket. Gọi nhiều lần không lỗi."""
        if self._closed:
            # close() trước có thể đã bị hủy giữa chừng (kể cả trước khi kịp gọi
            # async_close): chờ phần đóng socket của nó xong rồi mới trả về.
            await self._close_done.wait()
            return
        self._closed = True
        # try/finally: async_close luôn chạy (kể cả khi close bị hủy hoặc lỗi giữa chừng),
        # để không rò socket mDNS.
        try:
            self._resolve_queue.clear()
            self._probe_queue.clear()
            tasks = [*self._resolving.values()]
            if self._tick_task is not None:
                tasks.append(self._tick_task)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._aiozc is not None:
                try:
                    await self._unregister()
                except Exception:
                    log.exception("gửi goodbye mDNS thất bại")
        finally:
            try:
                if self._browser is not None:
                    await self._browser.async_cancel()
            except Exception:
                log.exception("dừng ServiceBrowser thất bại")
            finally:
                # finally lồng: kể cả khi close() bị hủy lúc đang dừng browser.
                if self._aiozc is None:
                    self._close_done.set()
                else:
                    # Chạy trong task riêng và shield: nếu close() đang bị hủy, zeroconf thấy
                    # `current_task().cancelling()` và tự dừng giữa chừng, để lại socket mở.
                    # Task chạy tới cùng kể cả khi close() bị hủy; lần gọi sau chờ `_close_done`.
                    self._close_task = asyncio.ensure_future(self._aiozc.async_close())
                    self._close_task.add_done_callback(lambda _: self._close_done.set())
                    await asyncio.shield(self._close_task)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _running_zeroconf(self) -> AsyncZeroconf:
        if self._aiozc is None or self._closed:
            raise RuntimeError("MdnsDiscovery chưa start hoặc đã close")
        return self._aiozc

    # ---- Quảng bá --------------------------------------------------------------------------

    async def advertise(
        self, *, device_id: str, name: str, os: str, port: int, addresses: Sequence[str] | None = None
    ) -> None:
        """Quảng bá (lần đầu) hoặc cập nhật tên/cổng/địa chỉ (các lần sau) của máy này.

        Tham số sai → MessageError / ValueError (kiểm trước cả trạng thái). Chưa
        start → RuntimeError. `addresses` None → tự chọn (`local_ipv4_addresses`);
        có truyền thì vẫn qua `filter_addresses`. Không còn địa chỉ nào, hoặc tên
        instance đã có máy khác dùng → DiscoveryError. Đổi `device_id` so với lần
        quảng bá đang chạy → ValueError.
        """
        validate_device_id(device_id)
        validate_device_name(name)
        validate_os(os)
        _check_port(port)
        aiozc = self._running_zeroconf()
        async with self._advertise_lock:
            if self._info is not None and self._info.name != service_name(device_id):
                raise ValueError("không được đổi device_id khi đang quảng bá")
            if addresses is None:
                selected = await asyncio.to_thread(local_ipv4_addresses)
            else:
                selected = filter_addresses(addresses)
            if not selected:
                raise DiscoveryError("không tìm thấy địa chỉ IPv4 nào để quảng bá trong mạng LAN")
            info = build_service_info(device_id=device_id, name=name, os=os, port=port, addresses=selected)
            aiozc = self._running_zeroconf()  # có thể đã close trong lúc chờ
            if self._info is None:
                try:
                    await (await aiozc.async_register_service(info))
                except NonUniqueNameException as e:
                    raise DiscoveryError("một máy khác trong mạng đang quảng bá cùng mã thiết bị này") from e
            else:
                await (await aiozc.async_update_service(info))
            self._info = info
            log.debug("quảng bá %s cổng %d, địa chỉ %s", info.name, port, ", ".join(selected))

    async def stop_advertising(self) -> None:
        """Gửi goodbye và ngừng quảng bá; chưa quảng bá thì không làm gì."""
        async with self._advertise_lock:
            await self._unregister()

    async def _unregister(self) -> None:
        info, self._info = self._info, None
        if info is not None and self._aiozc is not None:
            await (await self._aiozc.async_unregister_service(info))

    # ---- Duyệt -----------------------------------------------------------------------------

    @property
    def devices(self) -> Mapping[str, DiscoveredDevice]:
        """Thiết bị đang hiện, khóa theo device_id (rỗng khi chưa duyệt)."""
        if self._tracker is None:
            return MappingProxyType({})
        return self._tracker.devices

    async def start_browsing(
        self,
        *,
        self_device_id: str,
        on_event: Callable[[DeviceEvent], None],
        max_services: int = MAX_TRACKED_SERVICES,
    ) -> None:
        """Bắt đầu theo dõi các máy Teleport khác (bỏ qua `self_device_id`).

        Chưa start, đã close, hoặc gọi lần hai → RuntimeError.
        """
        aiozc = self._running_zeroconf()
        if self._tracker is not None:
            raise RuntimeError("start_browsing chỉ gọi được một lần")
        self._tracker = DiscoveryTracker(
            self_device_id,
            max_services=max_services,
            probe_interval=self._probe_interval,
            priority_ids=self._priority_ids,
        )
        self._on_event = on_event
        self._browser = AsyncServiceBrowser(aiozc.zeroconf, SERVICE_TYPE, handlers=[self._on_change])
        self._tick_task = asyncio.create_task(self._tick_loop())

    def set_priority(self, device_ids: Iterable[str]) -> None:
        """Đặt tập thiết bị ưu tiên (đã ghép đôi): luôn được theo dõi, kể cả khi danh sách đầy.

        Gọi được trước hoặc sau `start_browsing`; thay toàn bộ tập cũ. Id sai →
        MessageError. Sau `close()` thì không làm gì.
        """
        ids = _check_priority_ids(device_ids)
        if self._closed:
            return
        self._priority_ids = ids
        if self._tracker is not None:
            output = self._tracker.set_priority(ids)
            # Tên đã chờ trong hàng thường từ trước khi được nâng ưu tiên cũng lên đầu hàng.
            queued = list(self._resolve_queue)
            self._resolve_queue.clear()
            self._resolve_queue.extend(n for n in queued if self._is_priority_name(n))
            self._resolve_queue.extend(n for n in queued if not self._is_priority_name(n))
            self._apply(output)

    def _on_change(
        self, zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        # zeroconf gọi handler bằng tham số từ khóa đúng các tên này.
        if self._closed or self._tracker is None:
            return
        if state_change is ServiceStateChange.Removed:
            self._apply(self._tracker.service_gone(name))
        else:  # Added, Updated
            self._apply(self._tracker.service_seen(name))

    async def _tick_loop(self) -> None:
        assert self._tracker is not None
        while True:
            await asyncio.sleep(self._probe_tick)
            self._apply(self._tracker.due_probes(time.monotonic()))

    def _apply(self, output: TrackerOutput) -> None:
        """Thực hiện kết quả của tracker: ghi log, báo sự kiện, khởi động resolve."""
        for note in output.notes:
            log.debug("mDNS: %s", note)
        for event in output.events:
            if self._closed or self._on_event is None:
                break
            try:
                self._on_event(event)
            except Exception:
                log.exception("on_event raise khi xử lý %r", event)
        if self._closed:
            return
        urgent: list[str] = []  # resolve của thiết bị ưu tiên, theo thứ tự tracker trả về
        for queue, names in ((self._resolve_queue, output.resolve), (self._probe_queue, output.probe)):
            for name in names:
                if (
                    name in self._resolving
                    or name in self._probe_queue
                    or name in self._resolve_queue
                    or name in urgent
                ):
                    # Tracker hứa tối đa một yêu cầu mỗi device_id; không bao giờ chạy trùng.
                    log.error("tracker yêu cầu resolve trùng %r, bỏ qua", name)
                    continue
                if queue is self._resolve_queue and self._is_priority_name(name):
                    urgent.append(name)
                else:
                    queue.append(name)
        # Thiết bị đã ghép đôi lên đầu hàng `resolve` (giữ thứ tự giữa chúng), để lũ PTR
        # giả xếp hàng trước không làm nó chờ quá khoảng một lần hết giờ resolve.
        self._resolve_queue.extendleft(reversed(urgent))
        self._pump()

    def _is_priority_name(self, name: str) -> bool:
        """Tên dịch vụ có thuộc thiết bị ưu tiên (đã ghép đôi) không."""
        device_id = device_id_from_service_name(name)
        return device_id is not None and self._tracker is not None and self._tracker.is_priority(device_id)

    def _pump(self) -> None:
        """Khởi động resolve tới `MAX_CONCURRENT_RESOLVES` việc.

        Hàng `resolve` được lấy trước, nhưng chiếm tối đa
        `MAX_CONCURRENT_RESOLVES - PROBE_RESERVED_SLOTS` chỗ; hàng `probe` dùng được
        mọi chỗ còn trống. Thiết bị ưu tiên luôn đứng đầu hàng `resolve` (`_apply`).
        """
        first_limit = MAX_CONCURRENT_RESOLVES - PROBE_RESERVED_SLOTS
        while not self._closed and len(self._resolving) < MAX_CONCURRENT_RESOLVES:
            if self._resolve_queue and len(self._resolving_first) < first_limit:
                name = self._resolve_queue.popleft()
                self._resolving_first.add(name)
            elif self._probe_queue:
                name = self._probe_queue.popleft()
            else:
                return
            task = asyncio.create_task(self._resolve(name))
            self._resolving[name] = task
            task.add_done_callback(self._resolve_finished)

    def _forget(self, name: str) -> None:
        """Bỏ tên khỏi danh sách đang chạy."""
        self._resolving.pop(name, None)
        self._resolving_first.discard(name)

    def _resolve_finished(self, task: asyncio.Task[None]) -> None:
        for name, running in list(self._resolving.items()):
            if running is task:
                self._forget(name)
        self._pump()

    async def _resolve(self, name: str) -> None:
        assert self._aiozc is not None and self._tracker is not None
        result: ResolvedService | None = None
        try:
            info = AsyncServiceInfo(SERVICE_TYPE, name)
            if await info.async_request(self._aiozc.zeroconf, self._resolve_timeout * 1000):
                result = ResolvedService(
                    name=name,
                    txt=dict(info.properties),
                    port=info.port,
                    addresses=tuple(info.parsed_addresses(IPVersion.V4Only)),
                )
        except Exception as e:  # CancelledError là BaseException nên vẫn lan ra ngoài
            log.debug("resolve %r thất bại: %r", name, e)
        # Bỏ tên khỏi danh sách đang chạy TRƯỚC resolve_done: tracker có thể yêu cầu
        # resolve lại chính tên này ngay trong kết quả trả về.
        self._forget(name)
        if not self._closed:
            self._apply(self._tracker.resolve_done(name, result, time.monotonic()))
