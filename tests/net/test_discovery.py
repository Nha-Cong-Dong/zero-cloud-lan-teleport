"""Test `teleport.net.discovery` theo kế hoạch PR 3 (mục 2.5 và mục 4).

Phần đầu không mở socket. Phần `integration` chạy mDNS thật trên 127.0.0.1: mỗi test dùng
`device_id` ngẫu nhiên và chỉ xét sự kiện về các id của chính nó, để không lẫn với tiến trình
khác cũng đang quảng bá trên loopback. Đặt `TELEPORT_SKIP_MDNS=1` để bỏ qua phần này.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import ipaddress
import logging
import math
import os
import secrets
import socket
import types
from collections.abc import Callable

import pytest
from zeroconf import DNSOutgoing, DNSPointer, IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
from zeroconf.const import _CLASS_IN, _FLAGS_AA, _FLAGS_QR_RESPONSE, _TYPE_PTR

import teleport.net.discovery as net_discovery
from teleport.core.discovery import (
    RECORD_TTL,
    SERVICE_TYPE,
    DeviceAdded,
    DeviceEvent,
    DeviceRemoved,
    DeviceUpdated,
    DiscoveredDevice,
    DiscoveryTracker,
    TrackerOutput,
    build_txt,
    service_name,
)
from teleport.core.messages import PROTOCOL_VERSION, MessageError
from teleport.net.discovery import DiscoveryError, MdnsDiscovery, build_service_info, local_ipv4_addresses

E = "\U0001f600"  # 😀, 4 byte UTF-8
LOOPBACK = ["127.0.0.1"]
WAIT = 10.0  # giây chờ tối đa một sự kiện (kế hoạch mục 4)

mdns = pytest.mark.mdns


def new_id() -> str:
    return secrets.token_hex(16)


# --- Không cần mạng ------------------------------------------------------------------------------


def test_discovery_error_is_exception():
    assert issubclass(DiscoveryError, Exception)


def test_build_service_info_fields():
    did = new_id()
    info = build_service_info(
        device_id=did, name="Máy A 😀", os="windows", port=47474, addresses=["192.0.2.10", "198.51.100.7"]
    )
    assert isinstance(info, AsyncServiceInfo)
    assert info.type == SERVICE_TYPE
    assert info.name == service_name(did) == f"{did}._teleport._tcp.local."
    assert info.server == f"teleport-{did}.local."
    assert info.port == 47474
    assert info.host_ttl == info.other_ttl == RECORD_TTL == 60
    props = {k.decode(): (v.decode() if v is not None else None) for k, v in info.properties.items()}
    assert props == build_txt(did, "Máy A 😀", "windows")
    assert sorted(info.parsed_addresses(IPVersion.V4Only)) == ["192.0.2.10", "198.51.100.7"]


def test_build_service_info_truncates_long_name():
    did = new_id()
    info = build_service_info(device_id=did, name=E * 64, os="linux", port=1, addresses=["192.0.2.10"])
    assert info.properties[b"name"] == (E * 50).encode()


GOOD = {"device_id": "0" * 32, "name": "PC", "os": "windows", "port": 47474, "addresses": ["192.0.2.10"]}


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"device_id": "0" * 31}, MessageError),
        ({"device_id": "A" * 32}, MessageError),
        ({"name": ""}, MessageError),
        ({"name": " PC"}, MessageError),
        ({"name": "x" * 65}, MessageError),
        ({"os": "Windows"}, MessageError),
        ({"os": ""}, MessageError),
        ({"port": 0}, ValueError),
        ({"port": 65536}, ValueError),
        ({"port": -1}, ValueError),
    ],
    ids=[
        "id_short",
        "id_upper",
        "name_empty",
        "name_space",
        "name_long",
        "os_upper",
        "os_empty",
        "p0",
        "p65536",
        "pneg",
    ],
)
async def test_advertise_validates_arguments_before_state(override, error):
    d = MdnsDiscovery(interfaces=LOOPBACK)  # chưa start: lỗi tham số vẫn phải báo trước
    with pytest.raises(error):
        await d.advertise(**{**GOOD, **override})


async def test_advertise_before_start_raises_runtime_error():
    d = MdnsDiscovery(interfaces=LOOPBACK)
    with pytest.raises(RuntimeError):
        await d.advertise(**GOOD)


async def test_start_browsing_before_start_raises_runtime_error():
    d = MdnsDiscovery(interfaces=LOOPBACK)
    with pytest.raises(RuntimeError):
        await d.start_browsing(self_device_id=new_id(), on_event=lambda ev: None)


# --- Tiện ích cho phần integration ---------------------------------------------------------------


async def browse(d: MdnsDiscovery, self_id: str) -> asyncio.Queue[DeviceEvent]:
    q: asyncio.Queue[DeviceEvent] = asyncio.Queue()
    await d.start_browsing(self_device_id=self_id, on_event=q.put_nowait)
    return q


def about(*ids: str, kind: type | None = None) -> Callable[[DeviceEvent], bool]:
    def pred(ev: DeviceEvent) -> bool:
        return ev.device.device_id in ids and (kind is None or isinstance(ev, kind))

    return pred


async def wait_event(q: asyncio.Queue[DeviceEvent], pred: Callable[[DeviceEvent], bool]) -> DeviceEvent:
    """Sự kiện đầu tiên thỏa `pred`; bỏ qua sự kiện của thiết bị lạ (tiến trình khác)."""
    async with asyncio.timeout(WAIT):
        while True:
            ev = await q.get()
            if pred(ev):
                return ev


async def collect(
    q: asyncio.Queue[DeviceEvent], seconds: float, pred: Callable[[DeviceEvent], bool]
) -> list[DeviceEvent]:
    """Mọi sự kiện thỏa `pred` tới trong `seconds` giây."""
    found: list[DeviceEvent] = []
    try:
        async with asyncio.timeout(seconds):
            while True:
                ev = await q.get()
                if pred(ev):
                    found.append(ev)
    except TimeoutError:
        pass
    return found


ADDRS = ["192.0.2.10", "198.51.100.7"]


async def advertise(d: MdnsDiscovery, device_id: str, name: str = "PC", **kw) -> None:
    await d.advertise(
        device_id=device_id,
        name=name,
        os=kw.get("os", "windows"),
        port=kw.get("port", 47474),
        addresses=kw.get("addresses", ADDRS),
    )


# --- (a)–(e): khám phá giữa hai MdnsDiscovery trên 127.0.0.1 -------------------------------------


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_a_advertised_device_is_discovered_with_all_fields():
    x, y = new_id(), new_id()
    async with MdnsDiscovery(interfaces=LOOPBACK) as a, MdnsDiscovery(interfaces=LOOPBACK) as b:
        q = await browse(b, y)
        await advertise(a, x, "Máy A 😀", os="windows", port=47474, addresses=ADDRS)
        ev = await wait_event(q, about(x))
        expected = DiscoveredDevice(
            device_id=x,
            name="Máy A 😀",
            os="windows",
            proto=PROTOCOL_VERSION,
            port=47474,
            addresses=("192.0.2.10", "198.51.100.7"),
        )
        assert ev == DeviceAdded(expected)
        assert b.devices[x] == expected


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_a2_device_advertised_before_browsing_is_found():
    x, y = new_id(), new_id()
    async with MdnsDiscovery(interfaces=LOOPBACK) as a, MdnsDiscovery(interfaces=LOOPBACK) as b:
        await advertise(a, x, "Có sẵn")
        q = await browse(b, y)
        ev = await wait_event(q, about(x))
        assert isinstance(ev, DeviceAdded)
        assert ev.device.name == "Có sẵn"


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_b_long_name_is_truncated_to_200_bytes():
    x, y = new_id(), new_id()
    async with MdnsDiscovery(interfaces=LOOPBACK) as a, MdnsDiscovery(interfaces=LOOPBACK) as b:
        q = await browse(b, y)
        await advertise(a, x, E * 64)
        ev = await wait_event(q, about(x))
        assert isinstance(ev, DeviceAdded)
        assert ev.device.name == E * 50


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_c_rename_emits_device_updated():
    x, y = new_id(), new_id()
    async with MdnsDiscovery(interfaces=LOOPBACK) as a, MdnsDiscovery(interfaces=LOOPBACK) as b:
        q = await browse(b, y)
        await advertise(a, x, "Tên cũ")
        await wait_event(q, about(x, kind=DeviceAdded))
        await advertise(a, x, "Tên mới")
        ev = await wait_event(q, about(x))
        assert isinstance(ev, DeviceUpdated), ev
        assert ev.previous.name == "Tên cũ"
        assert ev.device.name == "Tên mới"
        assert b.devices[x].name == "Tên mới"


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_d_stop_advertising_emits_device_removed():
    x, y = new_id(), new_id()
    async with MdnsDiscovery(interfaces=LOOPBACK) as a, MdnsDiscovery(interfaces=LOOPBACK) as b:
        q = await browse(b, y)
        await advertise(a, x, "Sắp tắt")
        await wait_event(q, about(x, kind=DeviceAdded))
        await a.stop_advertising()
        ev = await wait_event(q, about(x))
        assert isinstance(ev, DeviceRemoved), ev
        assert ev.device.name == "Sắp tắt"
        assert x not in b.devices


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_e_browser_never_reports_itself():
    x, z = new_id(), new_id()
    async with MdnsDiscovery(interfaces=LOOPBACK) as a, MdnsDiscovery(interfaces=LOOPBACK) as b:
        q = await browse(a, x)
        await advertise(a, x, "Chính A")
        await advertise(b, z, "Máy B")
        ev = await wait_event(q, about(x, z))
        assert isinstance(ev, DeviceAdded)
        assert ev.device.device_id == z
        assert await collect(q, 2.0, about(x)) == []
        assert x not in a.devices
        assert z in a.devices


# --- (f): bản ghi sai do zeroconf thô đăng ký ----------------------------------------------------


def raw_info(label: str, props: dict[bytes, bytes], tag: str) -> AsyncServiceInfo:
    return AsyncServiceInfo(
        SERVICE_TYPE,
        f"{label}.{SERVICE_TYPE}",
        port=47474,
        properties=props,
        server=f"raw-{tag}.local.",
        host_ttl=60,
        other_ttl=60,
        parsed_addresses=["192.0.2.20"],
    )


def raw_txt(device_id: str, **override: bytes) -> dict[bytes, bytes]:
    txt = {b"id": device_id.encode(), b"name": b"PC", b"os": b"windows", b"proto": b"1"}
    txt.update({k.encode(): v for k, v in override.items()})
    return txt


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_f_malformed_records_are_ignored_quietly(caplog):
    caplog.set_level(logging.DEBUG)
    y, w, other = new_id(), new_id(), new_id()
    bad_ids = [new_id() for _ in range(4)]
    tag = secrets.token_hex(4)
    infos = [
        raw_info(bad_ids[0], raw_txt(bad_ids[0], os=b"Windows"), f"os-{tag}"),
        raw_info(bad_ids[1], raw_txt(other), f"mismatch-{tag}"),  # id trong TXT khác nhãn
        raw_info(bad_ids[2], raw_txt(bad_ids[2], proto=b"01"), f"proto-{tag}"),
        raw_info(bad_ids[3], raw_txt(bad_ids[3], name=(E * 50 + "a").encode()), f"name-{tag}"),  # 201 B
        raw_info(f"notanid-{tag}", raw_txt(new_id()), f"label-{tag}"),
        raw_info(w, raw_txt(w, name="Hợp lệ".encode()), f"good-{tag}"),
    ]
    ours = (w, other, *bad_ids)
    async with MdnsDiscovery(interfaces=LOOPBACK) as b:
        q = await browse(b, y)
        raw = AsyncZeroconf(interfaces=LOOPBACK, ip_version=IPVersion.V4Only)
        try:
            async with asyncio.timeout(WAIT):
                pending = await asyncio.gather(*(raw.async_register_service(i) for i in infos))
                await asyncio.gather(*pending)
            ev = await wait_event(q, about(*ours))
            assert isinstance(ev, DeviceAdded), ev
            assert ev.device.device_id == w
            assert ev.device.name == "Hợp lệ"
            assert await collect(q, 2.0, about(*ours)) == []
            assert set(b.devices) & set(ours) == {w}
        finally:
            async with asyncio.timeout(WAIT):
                await raw.async_unregister_all_services()  # gửi goodbye trước khi đóng
                # Cho lệnh gửi cuối xong: transport datagram proactor của CPython (Windows) đóng giữa
                # lúc đang gửi thì không bao giờ đóng socket, để rò tới khi gc thu dọn.
                await asyncio.sleep(0.1)
                await raw.async_close()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], [r.getMessage() for r in errors]


# --- (g): địa chỉ máy ----------------------------------------------------------------------------


@pytest.mark.integration
def test_g_local_ipv4_addresses_are_usable():
    result = local_ipv4_addresses()
    assert isinstance(result, tuple)
    assert len(result) <= 16
    assert len(set(result)) == len(result)
    parsed = [ipaddress.IPv4Address(a) for a in result]  # chỉ IPv4, dạng chuẩn
    assert [str(p) for p in parsed] == list(result)
    for p in parsed:
        assert not (p.is_loopback or p.is_link_local or p.is_multicast or p.is_unspecified or p.is_reserved)
        assert p not in ipaddress.IPv4Network("0.0.0.0/8")
    # preferred (nếu có) đứng đầu, phần còn lại tăng dần.
    assert parsed[1:] == sorted(parsed[1:])


# --- (h): đóng ----------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_h_close_twice_and_no_event_after_close():
    x, y = new_id(), new_id()
    a = MdnsDiscovery(interfaces=LOOPBACK)
    b = MdnsDiscovery(interfaces=LOOPBACK)
    await a.start()
    try:
        await b.start()
        q = await browse(b, y)
        await advertise(a, x, "Trước khi đóng")
        await wait_event(q, about(x, kind=DeviceAdded))
        async with asyncio.timeout(WAIT):
            await b.close()
            await b.close()
        # on_event chạy đồng bộ trong event loop: mọi sự kiện giao trước khi close() trả về đã
        # nằm trong hàng đợi (có thể là của thiết bị lạ do tiến trình khác quảng bá). Bỏ chúng đi;
        # từ đây không được có thêm sự kiện nào, của bất kỳ thiết bị nào.
        while not q.empty():
            q.get_nowait()
        await advertise(a, x, "Sau khi đóng")
        await a.stop_advertising()
        await asyncio.sleep(2.0)
        assert q.empty(), [q.get_nowait() for _ in range(q.qsize())]
    finally:
        async with asyncio.timeout(WAIT):
            await b.close()
            await a.close()
            await a.close()


@pytest.mark.integration
@mdns
async def test_close_without_advertising_or_browsing():
    d = MdnsDiscovery(interfaces=LOOPBACK)
    await d.start()
    async with asyncio.timeout(WAIT):
        await d.close()
        await d.close()


# --- Trạng thái và lỗi người dùng xử lý được -----------------------------------------------------


@pytest.mark.integration
@mdns
async def test_start_twice_raises_runtime_error():
    async with MdnsDiscovery(interfaces=LOOPBACK) as d:
        with pytest.raises(RuntimeError):
            await d.start()


@pytest.mark.integration
@mdns
async def test_start_browsing_twice_raises_runtime_error():
    async with MdnsDiscovery(interfaces=LOOPBACK) as d:
        await d.start_browsing(self_device_id=new_id(), on_event=lambda ev: None)
        with pytest.raises(RuntimeError):
            await d.start_browsing(self_device_id=new_id(), on_event=lambda ev: None)


@pytest.mark.integration
@mdns
async def test_stop_advertising_without_advertising_is_noop():
    async with MdnsDiscovery(interfaces=LOOPBACK) as d:
        await d.stop_advertising()
        await d.stop_advertising()


@pytest.mark.integration
@mdns
@pytest.mark.parametrize("addresses", [[], ["127.0.0.1", "169.254.1.1", "::1"]], ids=["empty", "unusable"])
async def test_advertise_without_usable_address_raises_discovery_error(addresses):
    async with MdnsDiscovery(interfaces=LOOPBACK) as d:
        with pytest.raises(DiscoveryError):
            await d.advertise(device_id=new_id(), name="PC", os="windows", port=47474, addresses=addresses)


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_advertise_cannot_change_device_id():
    async with MdnsDiscovery(interfaces=LOOPBACK) as d:
        await advertise(d, new_id())
        with pytest.raises(ValueError):
            await advertise(d, new_id())


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_duplicate_instance_name_raises_discovery_error():
    x = new_id()
    async with MdnsDiscovery(interfaces=LOOPBACK) as a, MdnsDiscovery(interfaces=LOOPBACK) as b:
        await advertise(a, x, "Bản gốc")
        with pytest.raises(DiscoveryError):
            async with asyncio.timeout(WAIT):
                await advertise(b, x, "Bản trùng")


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_on_event_exception_is_logged_and_later_events_still_arrive(caplog):
    x, y = new_id(), new_id()
    q: asyncio.Queue[DeviceEvent] = asyncio.Queue()

    def on_event(ev: DeviceEvent) -> None:
        q.put_nowait(ev)
        raise RuntimeError("lỗi trong on_event")

    async with MdnsDiscovery(interfaces=LOOPBACK) as a, MdnsDiscovery(interfaces=LOOPBACK) as b:
        await b.start_browsing(self_device_id=y, on_event=on_event)
        await advertise(a, x, "Một")
        await wait_event(q, about(x, kind=DeviceAdded))
        await advertise(a, x, "Hai")
        ev = await wait_event(q, about(x))
        assert isinstance(ev, DeviceUpdated), ev
        assert ev.device.name == "Hai"
    assert any(r.levelno >= logging.ERROR and r.exc_info for r in caplog.records)


# --- close() khi đang có resolve chờ, và close() bị hủy giữa chừng (kế hoạch 7.5, 7.7) ----------

NET_DISCOVERY_FILE = os.path.normcase(os.path.abspath(net_discovery.__file__))


def module_tasks() -> list[asyncio.Task]:
    """Task chưa xong mà coroutine nằm trong teleport/net/discovery.py."""
    found = []
    for task in asyncio.all_tasks():
        code = getattr(task.get_coro(), "cr_code", None)
        if task.done() or code is None:
            continue
        if os.path.normcase(os.path.abspath(code.co_filename)) == NET_DISCOVERY_FILE:
            found.append(task)
    return found


def ptr_only_packet(instance: str) -> DNSOutgoing:
    """Gói trả lời chỉ có PTR: bên duyệt thấy dịch vụ nhưng resolve SRV/TXT/A không ai trả lời."""
    out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA)
    out.add_answer_at_time(DNSPointer(SERVICE_TYPE, _TYPE_PTR, _CLASS_IN, 4500, instance), 0)
    return out


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_close_while_resolve_pending_finishes_tasks_and_emits_nothing(caplog):
    ghost = new_id()  # có PTR, không có SRV/TXT/A: resolve treo tới hết resolve_timeout
    q: asyncio.Queue[DeviceEvent] = asyncio.Queue()
    b = MdnsDiscovery(interfaces=LOOPBACK, resolve_timeout=30.0)
    raw = AsyncZeroconf(interfaces=LOOPBACK, ip_version=IPVersion.V4Only)
    try:
        await b.start()
        await b.start_browsing(self_device_id=new_id(), on_event=q.put_nowait)
        raw.zeroconf.async_send(ptr_only_packet(service_name(ghost)))
        await asyncio.sleep(1.0)
        assert module_tasks(), "tiền điều kiện: phải có task resolve/worker đang chạy"
        # close() không được chờ hết resolve_timeout (30 s).
        async with asyncio.timeout(5.0):
            await b.close()
        # Sự kiện giao trước khi close() trả về (có thể của tiến trình khác trên loopback): bỏ.
        while not q.empty():
            q.get_nowait()
        await asyncio.sleep(0)  # cho task vừa hủy kịp kết thúc
        assert module_tasks() == [], "còn task của net/discovery sau close()"
        await asyncio.sleep(1.0)
        assert q.empty(), [q.get_nowait() for _ in range(q.qsize())]
    finally:
        async with asyncio.timeout(WAIT):
            await b.close()
            await raw.async_close()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], [r.getMessage() for r in errors]


def stuck_browser_cancel(monkeypatch, seconds: float) -> asyncio.Event:
    """`AsyncServiceBrowser.async_cancel` chạy thật rồi kẹt `seconds` giây; trả Event báo đã tới chỗ kẹt."""
    real_cancel = AsyncServiceBrowser.async_cancel
    reached = asyncio.Event()

    async def stuck_cancel(self: AsyncServiceBrowser) -> None:
        await real_cancel(self)
        reached.set()
        await asyncio.sleep(seconds)

    monkeypatch.setattr(AsyncServiceBrowser, "async_cancel", stuck_cancel)
    return reached


async def eventually(cond: Callable[[], bool]) -> bool:
    async with asyncio.timeout(WAIT):
        while not cond():
            await asyncio.sleep(0.05)
    return True


@pytest.mark.integration
@mdns
async def test_cancelled_close_still_closes_zeroconf(monkeypatch):
    closed: list[AsyncZeroconf] = []
    real_close = AsyncZeroconf.async_close

    async def spy_close(self: AsyncZeroconf) -> None:
        closed.append(self)
        await real_close(self)

    monkeypatch.setattr(AsyncZeroconf, "async_close", spy_close)
    reached = stuck_browser_cancel(monkeypatch, 1.0)

    d = MdnsDiscovery(interfaces=LOOPBACK)
    await d.start()
    await d.start_browsing(self_device_id=new_id(), on_event=lambda ev: None)
    task = asyncio.create_task(d.close())
    async with asyncio.timeout(WAIT):
        await reached.wait()
    task.cancel()
    async with asyncio.timeout(WAIT):
        with contextlib.suppress(asyncio.CancelledError):
            await task
    # close() bị hủy giữa chừng: socket vẫn phải được đóng (ngay, hoặc khi phần đóng chạy xong).
    assert await eventually(lambda: len(closed) == 1 and closed[0].zeroconf.done)


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_second_close_waits_for_cancelled_close_to_finish(monkeypatch):
    # close() đầu bị hủy ĐÚNG lúc async_close đang chạy (đã được shield): lần gọi sau phải chờ.
    real_close = AsyncZeroconf.async_close
    in_close = asyncio.Event()

    async def slow_close(self: AsyncZeroconf) -> None:
        in_close.set()
        await asyncio.sleep(1.0)
        await real_close(self)

    monkeypatch.setattr(AsyncZeroconf, "async_close", slow_close)
    d = MdnsDiscovery(interfaces=LOOPBACK)
    await d.start()
    zc = d._aiozc.zeroconf  # nội bộ: chỉ để quan sát socket đã đóng chưa
    await d.start_browsing(self_device_id=new_id(), on_event=lambda ev: None)
    first = asyncio.create_task(d.close())
    async with asyncio.timeout(WAIT):
        await in_close.wait()
    first.cancel()
    async with asyncio.timeout(WAIT):
        with contextlib.suppress(asyncio.CancelledError):
            await first
    async with asyncio.timeout(WAIT):
        await d.close()
    assert zc.done, "close() lần hai trả về khi zeroconf chưa đóng xong"
    async with asyncio.timeout(WAIT):
        await d.close()  # thêm lần nữa: vẫn không lỗi


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_concurrent_close_waits_until_zeroconf_closed(monkeypatch):
    # Hai close() chồng nhau (ví dụ __aexit__ và một close() khác): lần sau không được trả về
    # khi lần đầu còn đang dở (ở đây: đang dừng browser), vì lúc đó socket chưa đóng.
    reached = stuck_browser_cancel(monkeypatch, 1.0)
    d = MdnsDiscovery(interfaces=LOOPBACK)
    await d.start()
    zc = d._aiozc.zeroconf
    await d.start_browsing(self_device_id=new_id(), on_event=lambda ev: None)
    first = asyncio.create_task(d.close())
    try:
        async with asyncio.timeout(WAIT):
            await reached.wait()
        async with asyncio.timeout(WAIT):
            await d.close()
        assert zc.done, "close() thứ hai trả về khi close() thứ nhất chưa đóng xong zeroconf"
    finally:
        async with asyncio.timeout(WAIT):
            await first


# --- Lập lịch resolve trong net (review lần 2): 16 chỗ, resolve tối đa 16 - 4 --------------------


class FakeResolves:
    """Thay `_resolve` của một MdnsDiscovery: mỗi việc chờ tới khi test thả nó ra."""

    def __init__(self) -> None:
        self.running: set[str] = set()
        self.started: collections.Counter[str] = collections.Counter()
        self.order: list[str] = []  # thứ tự các việc bắt đầu chạy
        self.release_events: dict[str, asyncio.Event] = {}
        self.max_running = 0

    async def resolve(self, name: str) -> None:
        self.started[name] += 1
        self.order.append(name)
        self.running.add(name)
        self.max_running = max(self.max_running, len(self.running))
        try:
            await self.release_events.setdefault(name, asyncio.Event()).wait()
        finally:
            self.running.discard(name)

    def kinds(self) -> tuple[int, int]:
        return (
            sum(1 for n in self.running if n.startswith("r")),
            sum(1 for n in self.running if n.startswith("p")),
        )

    async def release(self, *names: str) -> None:
        for n in names:
            self.release_events.setdefault(n, asyncio.Event()).set()
        await settle()

    def some(self, prefix: str, k: int) -> list[str]:
        return sorted(n for n in self.running if n.startswith(prefix))[:k]


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.fixture
async def scheduler(monkeypatch):
    d = MdnsDiscovery(interfaces=LOOPBACK)  # không start: không mở socket
    fake = FakeResolves()
    monkeypatch.setattr(d, "_resolve", fake.resolve)
    try:
        yield d, fake
    finally:
        async with asyncio.timeout(WAIT):
            await d.close()


def names(prefix: str, n: int) -> tuple[str, ...]:
    return tuple(f"{prefix}{i:02d}.{SERVICE_TYPE}" for i in range(n))


def test_scheduler_constants():
    assert net_discovery.MAX_CONCURRENT_RESOLVES == 16
    assert net_discovery.PROBE_RESERVED_SLOTS == 4


async def test_scheduler_reserves_slots_for_probes(scheduler):
    d, fake = scheduler
    d._apply(TrackerOutput(resolve=names("r", 20), probe=names("p", 10)))
    await settle()
    assert fake.kinds() == (12, 4)


async def test_scheduler_resolve_alone_uses_at_most_12_slots(scheduler):
    d, fake = scheduler
    d._apply(TrackerOutput(resolve=names("r", 20)))
    await settle()
    assert fake.kinds() == (12, 0)


async def test_scheduler_probe_alone_uses_all_16_slots(scheduler):
    d, fake = scheduler
    d._apply(TrackerOutput(probe=names("p", 20)))
    await settle()
    assert fake.kinds() == (0, 16)


async def test_scheduler_probes_fill_free_slots_and_resolve_gets_its_quota_back(scheduler):
    d, fake = scheduler
    d._apply(TrackerOutput(probe=names("p", 20)))
    await settle()
    assert fake.kinds() == (0, 16)
    # Resolve tới khi mọi chỗ đã bị probe chiếm: được ưu tiên ở mỗi chỗ vừa trống.
    d._apply(TrackerOutput(resolve=names("r", 20)))
    await settle()
    assert fake.kinds() == (0, 16)
    await fake.release(*fake.some("p", 3))
    assert fake.kinds() == (3, 13)
    await fake.release(*fake.some("p", 13))
    assert fake.kinds() == (12, 4)  # chạm hạn mức 12: chỗ còn lại cho probe


async def test_scheduler_prefers_resolve_within_quota_while_draining(scheduler):
    d, fake = scheduler
    d._apply(TrackerOutput(resolve=names("r", 20), probe=names("p", 10)))
    await settle()
    assert fake.kinds() == (12, 4)
    # Thả 2 resolve: 2 chỗ trống quay về hàng resolve (còn 8 chờ), không cho probe.
    await fake.release(*fake.some("r", 2))
    assert fake.kinds() == (12, 4)
    # Thả 2 probe: resolve đã đủ 12 nên chỗ trống dành cho probe.
    await fake.release(*fake.some("p", 2))
    assert fake.kinds() == (12, 4)
    # Thả hết resolve đang chạy: 6 resolve còn lại chạy, chỗ dư cho probe.
    await fake.release(*fake.some("r", 12))
    # 6 resolve còn lại (dưới hạn mức), 10 chỗ dư: 4 probe đang chạy + 4 probe cuối cùng trong hàng.
    assert fake.kinds() == (6, 8)
    assert sum(1 for n in fake.started if n.startswith("p")) == 10
    # Không bao giờ quá 16 việc, và mỗi tên chỉ chạy một lần.
    assert fake.max_running <= 16
    assert set(fake.started.values()) == {1}


async def test_scheduler_runs_everything_exactly_once(scheduler):
    d, fake = scheduler
    d._apply(TrackerOutput(resolve=names("r", 20), probe=names("p", 10)))
    await settle()
    for _ in range(40):
        if not fake.running:
            break
        assert len(fake.running) <= 16
        await fake.release(*sorted(fake.running)[:3])
    assert not fake.running
    assert fake.started == collections.Counter({n: 1 for n in (*names("r", 20), *names("p", 10))})
    assert fake.max_running <= 16


async def test_scheduler_drops_duplicate_request_with_error_log(scheduler, caplog):
    d, fake = scheduler
    caplog.set_level(logging.DEBUG)
    running = names("r", 1)[0]
    d._apply(TrackerOutput(resolve=names("r", 13)))  # 12 chạy, r12 chờ trong hàng
    await settle()
    queued = names("r", 13)[12]
    assert running in fake.running and queued not in fake.running
    caplog.clear()
    d._apply(TrackerOutput(resolve=(running,)))  # trùng việc đang chạy
    d._apply(TrackerOutput(probe=(queued,)))  # trùng việc đang chờ (khác hàng)
    d._apply(TrackerOutput(resolve=(queued,)))  # trùng việc đang chờ (cùng hàng)
    await settle()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 3, [r.getMessage() for r in caplog.records]
    for _ in range(10):
        if not fake.running:
            break
        await fake.release(*fake.running)
    assert fake.started[running] == 1
    assert fake.started[queued] == 1


# --- PR-1 bổ sung mục A: resolve của thiết bị ưu tiên chạy trước hàng chờ ------------------------


@pytest.fixture
async def prio_scheduler(scheduler):
    """`scheduler` kèm một tracker thật có tập ưu tiên; trả (d, fake, tập id ưu tiên).

    Không mở socket: `_tracker` được gắn thẳng (nội bộ) vì `start_browsing` cần zeroconf.
    """
    d, fake = scheduler
    prio = [new_id() for _ in range(3)]
    d._tracker = DiscoveryTracker(new_id(), priority_ids=prio)
    d._on_event = lambda ev: None
    d.set_priority(prio)  # đi qua đường công khai khi đã có tracker
    await settle()
    assert not fake.running
    return d, fake, prio


def seen(d: MdnsDiscovery, device_id: str) -> None:
    """Giả lập zeroconf báo `Added` cho một dịch vụ (đúng đường callback của ServiceBrowser)."""
    d._on_change(
        zeroconf=None,
        service_type=SERVICE_TYPE,
        name=service_name(device_id),
        state_change=ServiceStateChange.Added,
    )


async def drain_one_by_one(fake: FakeResolves) -> None:
    """Thả từng việc cũ nhất đang chạy tới khi hết việc."""
    for _ in range(200):
        if not fake.running:
            return
        oldest = next(n for n in fake.order if n in fake.running)
        await fake.release(oldest)
    raise AssertionError("lập lịch không dừng")


async def test_scheduler_priority_device_is_next_resolve_after_regular_flood(prio_scheduler):
    d, fake, prio = prio_scheduler
    flood = [new_id() for _ in range(40)]
    for did in flood:
        seen(d, did)
    await settle()
    assert len(fake.running) == 12  # hạn mức hàng `resolve`; 28 tên thường đang chờ
    seen(d, prio[0])
    await settle()
    assert service_name(prio[0]) not in fake.running, "chưa có chỗ trống: chưa được chạy"
    # Một chỗ trống: thiết bị ưu tiên là việc kế tiếp, trước 28 tên thường đang chờ.
    await fake.release(fake.order[0])
    assert fake.order[12] == service_name(prio[0])
    assert service_name(prio[0]) in fake.running
    assert len(fake.order) == 13
    # Tên thường vẫn đi theo thứ tự FIFO, mỗi tên đúng một lần.
    await drain_one_by_one(fake)
    regular = [service_name(did) for did in flood]
    assert fake.order == [*regular[:12], service_name(prio[0]), *regular[12:]]
    assert set(fake.started.values()) == {1}


async def test_scheduler_priority_names_in_one_output_jump_queue_keeping_their_order(prio_scheduler):
    d, fake, prio = prio_scheduler
    d._apply(TrackerOutput(resolve=names("r", 12)))  # đầy hạn mức
    d._apply(TrackerOutput(resolve=names("q", 3)))  # đang chờ, tên không hợp lệ: không ưu tiên
    await settle()
    reg_a, reg_b = service_name(new_id()), service_name(new_id())
    p1, p2, p3 = (service_name(p) for p in prio)
    d._apply(TrackerOutput(resolve=(reg_a, p1, reg_b, p2, p3)))
    await settle()
    assert len(fake.order) == 12
    await drain_one_by_one(fake)
    assert fake.order[12:] == [p1, p2, p3, *names("q", 3), reg_a, reg_b]


async def test_scheduler_priority_admitted_from_overflow_by_set_priority_jumps_queue(scheduler):
    d, fake = scheduler
    d._tracker = DiscoveryTracker(new_id(), max_services=12)
    d._on_event = lambda ev: None
    flood = [new_id() for _ in range(12)]
    for did in flood:
        seen(d, did)
    await settle()
    assert len(fake.running) == 12
    waiting = new_id()
    seen(d, waiting)  # tracker đầy: vào danh sách tràn, không yêu cầu resolve
    d._apply(TrackerOutput(resolve=names("q", 5)))  # hàng chờ thường khác rỗng
    await settle()
    assert service_name(waiting) not in fake.started
    d.set_priority([waiting])  # được nhận từ danh sách tràn, resolve phải đi đầu hàng
    await settle()
    await fake.release(fake.order[0])
    assert fake.order[12] == service_name(waiting)


async def test_scheduler_name_already_queued_moves_to_front_when_promoted(scheduler):
    # Tên đã chờ trong hàng thường TỪ TRƯỚC rồi mới được nâng ưu tiên qua set_priority: phải
    # dời lên đầu hàng, trước mọi tên thường đang chờ, giữ thứ tự hàng giữa các tên ưu tiên.
    d, fake = scheduler
    d._tracker = DiscoveryTracker(new_id())
    d._on_event = lambda ev: None
    flood = [new_id() for _ in range(12)]
    for did in flood:
        seen(d, did)
    queued = [new_id() for _ in range(5)]  # a, b, c, d, e: chờ trong hàng thường
    for did in queued:
        seen(d, did)
    await settle()
    assert len(fake.running) == 12
    a, b, c, dd, e = (service_name(did) for did in queued)
    # Thứ tự tham số khác thứ tự hàng: thứ tự giữ theo hàng (b trước d).
    d.set_priority([queued[3], queued[1]])
    await settle()
    assert len(fake.order) == 12, "chưa có chỗ trống: chưa được chạy"
    await drain_one_by_one(fake)
    assert fake.order[12:] == [b, dd, a, c, e]
    assert set(fake.started.values()) == {1}


async def test_scheduler_priority_probe_stays_in_probe_queue_order(prio_scheduler):
    # Mục A chỉ đổi hàng `resolve`; hàng `probe` giữ nguyên (FIFO, 4 chỗ dành riêng).
    d, fake, prio = prio_scheduler
    d._apply(TrackerOutput(probe=names("p", 20)))  # 16 chạy, p16–p19 chờ
    await settle()
    d._apply(TrackerOutput(probe=(service_name(prio[0]),)))
    await settle()
    await fake.release(fake.order[0])
    assert fake.order[16] == names("p", 20)[16]
    await drain_one_by_one(fake)
    assert fake.order[16:] == [*names("p", 20)[16:], service_name(prio[0])]


async def test_close_never_started_twice_returns():
    d = MdnsDiscovery(interfaces=LOOPBACK)
    async with asyncio.timeout(WAIT):
        await d.close()
        await d.close()


# --- Bước 1.3 mục 2a: địa chỉ nguồn ưu tiên (socket giả, không mở socket thật) ------------------

MDNS_GROUP = ("224.0.0.251", 5353)  # chỉ để chứng minh tuyến multicast mDNS không còn được hỏi
DEFAULT_ROUTE = ("192.0.2.1", 9)  # TEST-NET-1 (RFC 5737)


class FakeRoutes:
    """Bảng tuyến giả: đích → địa chỉ nguồn (str) hoặc OSError khi connect; đích lạ → OSError."""

    def __init__(self, routes: dict[tuple[str, int], str | OSError], *, socket_error: bool = False) -> None:
        self.routes = routes
        self.socket_error = socket_error
        self.connects: list[tuple[str, int]] = []
        self.closed = 0
        self.opened = 0

    def socket_factory(self, *args: object, **kwargs: object) -> FakeUdpSocket:
        if self.socket_error:
            raise OSError("không tạo được socket")
        self.opened += 1
        return FakeUdpSocket(self)


class FakeUdpSocket:
    def __init__(self, routes: FakeRoutes) -> None:
        self._routes = routes
        self._source: str | None = None

    def connect(self, dest: tuple[str, int]) -> None:
        self._routes.connects.append(tuple(dest))
        result = self._routes.routes.get(tuple(dest), OSError("không có tuyến"))
        if isinstance(result, OSError):
            raise result
        self._source = result

    def getsockname(self) -> tuple[str, int]:
        if self._source is None:
            return ("0.0.0.0", 0)
        return (self._source, 54321)

    def send(self, *args: object, **kwargs: object) -> int:
        raise AssertionError("không được gửi gói nào khi hỏi tuyến")

    sendto = sendall = send

    def close(self) -> None:
        self._routes.closed += 1

    def __enter__(self) -> FakeUdpSocket:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> Callable[..., None]:
        return lambda *a, **k: None  # setsockopt, settimeout, setblocking…


@pytest.fixture
def fake_routes(monkeypatch):
    """Thay `socket` trong module net.discovery bằng bản sao có `socket.socket` giả."""

    def install(routes: dict[tuple[str, int], str | OSError], **kw: bool) -> FakeRoutes:
        fake = FakeRoutes(routes, **kw)
        module = types.ModuleType("socket")
        module.__dict__.update(socket.__dict__)
        module.socket = fake.socket_factory
        monkeypatch.setattr(net_discovery, "socket", module)
        return fake

    return install


def test_route_source_returns_source_without_sending(fake_routes):
    fake = fake_routes({DEFAULT_ROUTE: "192.168.1.7"})
    assert net_discovery._route_source(DEFAULT_ROUTE) == "192.168.1.7"
    assert fake.connects == [DEFAULT_ROUTE]
    assert fake.closed == fake.opened == 1, "socket phải được đóng"


@pytest.mark.parametrize("route", ["0.0.0.0", OSError("mạng không tới được")], ids=["unspecified", "oserror"])
def test_route_source_unknown_is_none(fake_routes, route):
    fake = fake_routes({DEFAULT_ROUTE: route})
    assert net_discovery._route_source(DEFAULT_ROUTE) is None
    assert fake.closed == fake.opened


def test_route_source_socket_creation_error_is_none(fake_routes):
    fake_routes({}, socket_error=True)
    assert net_discovery._route_source(DEFAULT_ROUTE) is None


def test_preferred_source_asks_only_the_route_out_of_the_lan(fake_routes):
    fake = fake_routes({MDNS_GROUP: "10.0.0.5", DEFAULT_ROUTE: "192.168.1.7"})
    assert net_discovery._preferred_source() == "192.168.1.7"
    assert fake.connects == [DEFAULT_ROUTE], "không được hỏi tuyến multicast mDNS"
    assert fake.closed == fake.opened


def test_preferred_source_ignores_multicast_route_with_lan_address(fake_routes):
    # Tuyến multicast trên Windows chỉ phản ánh InterfaceMetric, không cho biết card nối LAN.
    fake = fake_routes({MDNS_GROUP: "192.168.1.7", DEFAULT_ROUTE: "10.0.0.5"})
    assert net_discovery._preferred_source() == "10.0.0.5"
    assert MDNS_GROUP not in fake.connects


@pytest.mark.parametrize(
    "default",
    [OSError("mạng không tới được"), "0.0.0.0"],
    ids=["oserror", "unspecified"],
)
def test_preferred_source_none_without_route_out_even_if_multicast_route_is_lan(fake_routes, default):
    # LAN không gateway: không có địa chỉ ưu tiên, không quay về tuyến multicast.
    fake = fake_routes({MDNS_GROUP: "192.168.1.7", DEFAULT_ROUTE: default})
    assert net_discovery._preferred_source() is None
    assert MDNS_GROUP not in fake.connects


@pytest.mark.parametrize(
    "default",
    [OSError("y"), "127.0.0.1", "127.0.1.1", "0.0.0.0"],
    ids=["error", "loopback", "loopback_8", "unspecified"],
)
def test_preferred_source_none_when_route_out_is_unusable(fake_routes, default):
    fake_routes({DEFAULT_ROUTE: default})
    assert net_discovery._preferred_source() is None


def test_preferred_source_none_when_socket_cannot_be_created(fake_routes):
    fake_routes({}, socket_error=True)
    assert net_discovery._preferred_source() is None


def fake_adapter(name: str, nice_name: str, *ips: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        name=name, nice_name=nice_name, ips=[types.SimpleNamespace(ip=ip, nice_name=nice_name) for ip in ips]
    )


WIN_ADAPTERS = [
    fake_adapter("{GUID-ETH}", "Realtek PCIe GbE Family Controller", "10.0.0.5"),
    fake_adapter("{GUID-WIFI}", "Intel(R) Wi-Fi 6 AX201 160MHz", "192.168.1.7"),
    fake_adapter("{GUID-WSL}", "vEthernet (WSL)", "172.20.0.1"),
    fake_adapter("{GUID-LO}", "Software Loopback Interface 1", "127.0.0.1"),
]

# Hyper-V External vSwitch: card vật lý được nối vào switch ảo, máy ra ngoài LAN qua card
# "Hyper-V Virtual Ethernet Adapter #2" — trông như card ảo nhưng mang IP LAN thật.
EXTERNAL_VSWITCH_ADAPTERS = [
    fake_adapter("{GUID-WSL}", "Hyper-V Virtual Ethernet Adapter", "172.18.96.1"),
    fake_adapter("{GUID-EXT}", "Hyper-V Virtual Ethernet Adapter #2", "192.168.1.20"),
    fake_adapter("{GUID-LO}", "Software Loopback Interface 1", "127.0.0.1"),
]


def test_local_ipv4_addresses_keeps_preferred_source_on_external_vswitch(monkeypatch, fake_routes):
    # Nguồn của tuyến ra ngoài LAN nằm trên card trông như card ảo: vẫn được giữ; card WSL bị lọc.
    monkeypatch.setattr(net_discovery.ifaddr, "get_adapters", lambda *a, **k: EXTERNAL_VSWITCH_ADAPTERS)
    fake_routes({DEFAULT_ROUTE: "192.168.1.20"})
    assert local_ipv4_addresses() == ("192.168.1.20",)


def test_local_ipv4_addresses_puts_preferred_source_first(monkeypatch):
    monkeypatch.setattr(net_discovery.ifaddr, "get_adapters", lambda *a, **k: WIN_ADAPTERS)
    monkeypatch.setattr(net_discovery, "_preferred_source", lambda: "192.168.1.7")
    assert local_ipv4_addresses() == ("192.168.1.7", "10.0.0.5")


def test_local_ipv4_addresses_ignores_hyperv_multicast_route_wsl_leak(monkeypatch, fake_routes):
    # Hồi quy (lần đo N1, 2026-10-23): trên Windows mọi card có tuyến 224.0.0.0/4 cùng metric,
    # tuyến multicast đi qua vEthernet (WSL) nên 172.18.96.1 từng lọt vào danh sách quảng bá.
    adapters = [
        fake_adapter(
            "{1CC8A943-F91D-43D3-BE42-BECA33EEC5C1}", "Hyper-V Virtual Ethernet Adapter", "172.18.96.1"
        ),
        fake_adapter(
            "{4E5C1AF5-793B-461A-BD72-8D9FF0719255}", "Intel(R) Wi-Fi 6 AX201 160MHz", "192.168.1.7"
        ),
    ]
    monkeypatch.setattr(net_discovery.ifaddr, "get_adapters", lambda *a, **k: adapters)
    fake = fake_routes({MDNS_GROUP: "172.18.96.1", DEFAULT_ROUTE: "192.168.1.7"})
    assert local_ipv4_addresses() == ("192.168.1.7",)
    assert fake.connects == [DEFAULT_ROUTE], "không được hỏi tuyến multicast mDNS"


def test_local_ipv4_addresses_without_preferred_source(monkeypatch, fake_routes):
    monkeypatch.setattr(net_discovery.ifaddr, "get_adapters", lambda *a, **k: WIN_ADAPTERS)
    fake_routes({MDNS_GROUP: "192.168.1.7", DEFAULT_ROUTE: OSError("y")})
    assert local_ipv4_addresses() == ("10.0.0.5", "192.168.1.7")


# --- Bước 1.3 mục 2b: probe_tick ---------------------------------------------------------------


def test_probe_tick_default_constant():
    assert net_discovery.PROBE_TICK == 5.0


TIME_PARAMS = ["probe_interval", "resolve_timeout", "probe_tick"]


@pytest.mark.parametrize("param", TIME_PARAMS)
@pytest.mark.parametrize("good", [0.05, 1, 1e-9, 1e9], ids=repr)
def test_time_params_accept_positive_finite_number(param, good):
    MdnsDiscovery(interfaces=LOOPBACK, **{param: good})


@pytest.mark.parametrize("param", TIME_PARAMS)
@pytest.mark.parametrize("bad", [0, 0.0, -0.0, -1, -0.5, math.inf, -math.inf, math.nan], ids=repr)
def test_time_params_reject_non_positive_or_non_finite_with_value_error(param, bad):
    with pytest.raises(ValueError):
        MdnsDiscovery(interfaces=LOOPBACK, **{param: bad})


@pytest.mark.parametrize("param", TIME_PARAMS)
@pytest.mark.parametrize("bad", ["3", True, False, None, [1], b"3"], ids=repr)
def test_time_params_reject_non_number_with_type_error(param, bad):
    with pytest.raises(TypeError):
        MdnsDiscovery(interfaces=LOOPBACK, **{param: bad})


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_probe_tick_sets_how_often_tracker_is_asked_for_probes(monkeypatch):
    calls: list[int] = []
    real_due = DiscoveryTracker.due_probes

    def spy(self: DiscoveryTracker, now: float) -> TrackerOutput:
        calls.append(id(self))
        return real_due(self, now)

    monkeypatch.setattr(DiscoveryTracker, "due_probes", spy)
    async with MdnsDiscovery(interfaces=LOOPBACK, probe_tick=0.1) as fast:
        await fast.start_browsing(self_device_id=new_id(), on_event=lambda ev: None)
        async with MdnsDiscovery(interfaces=LOOPBACK) as slow:  # mặc định PROBE_TICK = 5 s
            await slow.start_browsing(self_device_id=new_id(), on_event=lambda ev: None)
            fast_id, slow_id = id(fast._tracker), id(slow._tracker)  # nội bộ: chỉ để phân biệt hai tracker
            calls.clear()
            await asyncio.sleep(1.0)
    assert calls.count(fast_id) >= 5, calls.count(fast_id)
    assert calls.count(slow_id) == 0


# --- Bước 1.3 mục 2c: MdnsDiscovery.set_priority ------------------------------------------------


def test_set_priority_before_start_accepts_valid_ids():
    d = MdnsDiscovery(interfaces=LOOPBACK)
    assert d.set_priority([new_id(), new_id()]) is None
    assert d.set_priority(i for i in [new_id()]) is None
    assert d.set_priority([]) is None


@pytest.mark.parametrize("bad", ["", "0" * 31, "A" * 32, None, 5], ids=repr)
def test_set_priority_rejects_invalid_id(bad):
    d = MdnsDiscovery(interfaces=LOOPBACK)
    with pytest.raises(MessageError):
        d.set_priority([new_id(), bad])


def test_set_priority_rejects_bare_string():
    d = MdnsDiscovery(interfaces=LOOPBACK)
    with pytest.raises(MessageError):
        d.set_priority(new_id())


async def test_set_priority_after_close_does_nothing():
    d = MdnsDiscovery(interfaces=LOOPBACK)
    async with asyncio.timeout(WAIT):
        await d.close()
    assert d.set_priority([new_id()]) is None


@pytest.mark.parametrize(
    "bad",
    [["bad"], [new_id(), "bad"], ["A" * 32], [None]],
    ids=["bad", "id-hop-le+bad", "A*32", "None"],
)
async def test_set_priority_after_close_still_rejects_invalid_id(bad):
    # Đặc tả 2c: id sai → MessageError, kể cả khi đã close (kiểm tham số trước trạng thái).
    d = MdnsDiscovery(interfaces=LOOPBACK)
    async with asyncio.timeout(WAIT):
        await d.close()
    with pytest.raises(MessageError):
        d.set_priority(bad)


@pytest.mark.integration
@mdns
async def test_set_priority_after_browsing_rejects_invalid_id():
    async with MdnsDiscovery(interfaces=LOOPBACK) as d:
        await d.start_browsing(self_device_id=new_id(), on_event=lambda ev: None)
        with pytest.raises(MessageError):
            d.set_priority(["bad"])
        assert d.set_priority([new_id()]) is None


@pytest.mark.integration
@mdns
async def test_set_priority_after_close_of_browsing_instance_emits_nothing():
    q: asyncio.Queue[DeviceEvent] = asyncio.Queue()
    d = MdnsDiscovery(interfaces=LOOPBACK)
    await d.start()
    try:
        await d.start_browsing(self_device_id=new_id(), on_event=q.put_nowait)
    finally:
        async with asyncio.timeout(WAIT):
            await d.close()
    while not q.empty():
        q.get_nowait()
    with pytest.raises(MessageError):
        d.set_priority(["bad"])
    assert d.set_priority([new_id()]) is None
    await asyncio.sleep(0.2)
    assert q.empty()


# --- Bước 1.3 mục 2d: kiểm còn sống, tràn và ưu tiên đầu-cuối trên 127.0.0.1 ---------------------


def ghost_packet(*instances: str) -> DNSOutgoing:
    """Một gói chỉ có PTR cho nhiều dịch vụ "ma": thấy được nhưng không ai trả lời SRV/TXT/A."""
    out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA)
    for instance in instances:
        out.add_answer_at_time(DNSPointer(SERVICE_TYPE, _TYPE_PTR, _CLASS_IN, 4500, instance), 0)
    return out


def dying_device_packet(device_id: str, name: str, ttl: int) -> DNSOutgoing:
    """Thông báo đầy đủ PTR/SRV/TXT/A của một máy; SRV/TXT/A có TTL ngắn, PTR TTL dài (không phụ
    thuộc việc zeroconf nâng TTL PTR). Không có dịch vụ nào được đăng ký nên không bao giờ có goodbye
    và không ai trả lời truy vấn sau đó: máy "chết" ngay sau gói này."""
    info = AsyncServiceInfo(
        SERVICE_TYPE,
        service_name(device_id),
        port=47474,
        properties=build_txt(device_id, name, "linux"),
        server=f"teleport-{device_id}.local.",
        host_ttl=ttl,
        other_ttl=ttl,
        parsed_addresses=["192.0.2.30"],
    )
    out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA)
    out.add_answer_at_time(info.dns_pointer(override_ttl=4500), 0)
    out.add_answer_at_time(info.dns_service(), 0)
    out.add_answer_at_time(info.dns_text(), 0)
    for record in info.dns_addresses():
        out.add_answer_at_time(record, 0)
    return out


async def send_raw_and_die(packet: DNSOutgoing) -> None:
    """Gửi một gói bằng zeroconf thô rồi đóng nó (không có dịch vụ đăng ký → không goodbye)."""
    raw = AsyncZeroconf(interfaces=LOOPBACK, ip_version=IPVersion.V4Only)
    try:
        async with asyncio.timeout(WAIT):
            await raw.zeroconf.async_wait_for_start()  # trước đó chưa có socket gửi: gói bị bỏ im lặng
        raw.zeroconf.async_send(packet)
        # Cho lệnh gửi xong trước khi đóng (transport datagram proactor trên Windows, xem test f).
        await asyncio.sleep(0.1)
    finally:
        async with asyncio.timeout(WAIT):
            await raw.async_close()


DEAD_TTL = 2  # giây, TTL của SRV/TXT/A trên máy sắp chết
DEAD_DEADLINE = 15.0  # giây từ lúc thấy thiết bị tới DeviceRemoved


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_device_dying_without_goodbye_is_removed_by_liveness_probe():
    y, x = new_id(), new_id()
    loop = asyncio.get_running_loop()
    async with MdnsDiscovery(
        interfaces=LOOPBACK, probe_interval=1.0, resolve_timeout=0.5, probe_tick=0.2
    ) as b:
        q = await browse(b, y)
        await send_raw_and_die(dying_device_packet(x, "Sắp mất điện", DEAD_TTL))
        ev = await wait_event(q, about(x))
        assert isinstance(ev, DeviceAdded), ev
        assert ev.device.name == "Sắp mất điện"
        added_at = loop.time()
        # Cận trên ≈ TTL + 2 × (probe_interval + probe_tick + resolve_timeout) ≈ 5.4 s; cho dư tới 15 s.
        async with asyncio.timeout(DEAD_DEADLINE):
            while True:
                ev = await q.get()
                if about(x)(ev):
                    break
        assert isinstance(ev, DeviceRemoved), ev
        assert ev.device.device_id == x
        assert x not in b.devices
        # Bị xóa vì kiểm còn sống: chỉ sau khi SRV/TXT/A trong cache hết hạn.
        assert loop.time() - added_at >= DEAD_TTL - 0.5


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_tracker_full_of_ghosts_admits_real_device_after_ghosts_are_dropped():
    y, real = new_id(), new_id()
    ghosts = [new_id(), new_id()]
    loop = asyncio.get_running_loop()
    async with (
        MdnsDiscovery(interfaces=LOOPBACK) as a,
        MdnsDiscovery(interfaces=LOOPBACK, probe_interval=2.0, resolve_timeout=2.0, probe_tick=0.2) as b,
    ):
        q: asyncio.Queue[DeviceEvent] = asyncio.Queue()
        await b.start_browsing(self_device_id=y, on_event=q.put_nowait, max_services=2)
        await send_raw_and_die(ghost_packet(*(service_name(g) for g in ghosts)))
        ghosts_at = loop.time()
        await asyncio.sleep(0.3)
        await advertise(a, real, "Máy thật")
        # Hai ma chiếm hết chỗ; mỗi ma cần ≈ 2 × resolve_timeout + probe_interval + nhịp ≈ 6.2 s mới bị bỏ.
        await asyncio.sleep(max(0.0, ghosts_at + 3.0 - loop.time()))
        assert real not in b.devices, "tiền điều kiện: máy thật phải chờ khi danh sách đầy"
        # Máy thật hiện sau khi ma bị bỏ, không phải chờ zeroconf làm mới PTR (~14 phút).
        async with asyncio.timeout(15.0):
            while True:
                ev = await q.get()
                if about(real, *ghosts)(ev):
                    break
        assert isinstance(ev, DeviceAdded), ev
        assert ev.device.device_id == real
        assert ev.device.name == "Máy thật"
        assert not set(ghosts) & set(b.devices)


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_set_priority_after_browsing_admits_device_waiting_behind_ghosts():
    y, real = new_id(), new_id()
    ghosts = [new_id(), new_id()]
    # resolve_timeout dài: ma giữ chỗ suốt test, nên chỉ ưu tiên mới giải thích được việc máy thật hiện.
    async with (
        MdnsDiscovery(interfaces=LOOPBACK) as a,
        MdnsDiscovery(interfaces=LOOPBACK, resolve_timeout=30.0, probe_tick=0.2) as b,
    ):
        q: asyncio.Queue[DeviceEvent] = asyncio.Queue()
        await b.start_browsing(self_device_id=y, on_event=q.put_nowait, max_services=2)
        await send_raw_and_die(ghost_packet(*(service_name(g) for g in ghosts)))
        await asyncio.sleep(0.3)
        await advertise(a, real, "Đã ghép đôi")
        assert await collect(q, 1.5, about(real)) == [], "tiền điều kiện: máy thật phải chờ khi danh sách đầy"
        assert real not in b.devices
        b.set_priority([real])
        ev = await wait_event(q, about(real))
        assert isinstance(ev, DeviceAdded), ev
        assert ev.device.name == "Đã ghép đôi"
        assert real in b.devices


@pytest.mark.slow
@pytest.mark.integration
@mdns
async def test_set_priority_before_start_is_applied_when_browsing_starts():
    y, real, regular = new_id(), new_id(), new_id()
    ghosts = [new_id(), new_id()]
    b = MdnsDiscovery(interfaces=LOOPBACK, resolve_timeout=30.0, probe_tick=0.2)
    b.set_priority([real])  # trước cả start(): phải được lưu lại cho tracker
    async with MdnsDiscovery(interfaces=LOOPBACK) as a, MdnsDiscovery(interfaces=LOOPBACK) as c, b:
        q: asyncio.Queue[DeviceEvent] = asyncio.Queue()
        await b.start_browsing(self_device_id=y, on_event=q.put_nowait, max_services=2)
        await send_raw_and_die(ghost_packet(*(service_name(g) for g in ghosts)))
        await asyncio.sleep(0.3)
        # Tiền điều kiện: hai ma thật sự chiếm hết chỗ (resolve_timeout dài giữ chúng suốt test),
        # nên một máy thường quảng bá đúng vẫn phải chờ. Không có bước này, ma bị lạc gói thì
        # máy ưu tiên hiện ra cũng không chứng minh được gì.
        await advertise(c, regular, "Máy thường")
        assert await collect(q, 1.5, about(regular)) == [], "tiền điều kiện: ma phải chiếm hết chỗ"
        assert regular not in b.devices
        await advertise(a, real, "Đã ghép đôi")
        ev = await wait_event(q, about(real))
        assert isinstance(ev, DeviceAdded), ev
        assert real in b.devices
        assert regular not in b.devices, "máy thường vẫn phải chờ: ưu tiên không được nhả chỗ cho nó"
