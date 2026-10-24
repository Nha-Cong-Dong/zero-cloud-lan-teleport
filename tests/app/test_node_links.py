"""Test `Node` với mDNS (bản giả `FakeMdns`) và tự giữ kết nối điều khiển (đặc tả PR-4 mục 3.2–3.9, 4.5).

- NF1–NF9: Node + `FakeMdns`, không socket mDNS (vẫn có Listener thật trên 127.0.0.1 → integration).
- NL1–NL12: hai/ba Node thật trên 127.0.0.1; `FakeMdns` báo địa chỉ `127.0.0.1` và cổng thật.
- SC1–SC5: `scan_devices` với `FakeMdns`, không socket TCP.

Viết theo đặc tả, không theo code. Hằng thời gian đã rút ngắn bằng `fast` (RETRY_BASE 0,2 s,
RETRY_MAX 1 s, EVENT_RETRY_GAP 0,2 s, REPLACE_AFTER 2 s, NETWORK_CHECK_INTERVAL 0,2 s).
NL13 nằm ở `test_pairing.py`; NL14 là các test PR-3 trong `test_node.py` chạy không sửa.
"""

import asyncio
import contextlib
import itertools
import logging
import math
import time

import pytest

from teleport.app import node as node_mod
from teleport.app.devices import DeviceView
from teleport.app.node import (
    ConfigDirError,
    ConnectFailed,
    DeviceConnected,
    DeviceDisconnected,
    DeviceOffline,
    DeviceOnline,
    DiscoveryUnavailable,
    MdnsOptions,
    Node,
    ScanResult,
    default_device_name,
    scan_devices,
)
from teleport.app.pairing import PairingOutcome
from teleport.app.settings import Settings
from teleport.core import links
from teleport.core.discovery import DeviceAdded, DeviceRemoved, DeviceUpdated
from teleport.core.links import ConnectResult
from teleport.net.discovery import DiscoveryError
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore, TrustStoreError

pytestmark = [
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
integration = pytest.mark.integration

LOCAL = "127.0.0.1"
NODE_TASKS = ("teleport.node", "teleport.pairing", "teleport.connection")


async def wait_until(predicate, timeout: float = 2.0, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.01)


def task_names(prefix: str) -> list[str]:
    return [t.get_name() for t in asyncio.all_tasks() if not t.done() and t.get_name().startswith(prefix)]


def free_port() -> int:
    import socket

    s = socket.socket()
    try:
        s.bind((LOCAL, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def seed_trust(directory, identity, name: str) -> None:
    load_or_create_identity(directory)
    TrustStore.load(directory).add(peer_cert_from_pem(identity.cert_pem()), name)


def connected_count(rec, device_id: str) -> int:
    return len(rec.events_of(DeviceConnected, device_id)) - len(rec.events_of(DeviceDisconnected, device_id))


# =================================================================================================
# NF1–NF9: Node + FakeMdns
# =================================================================================================


def test_constants_match_spec():
    assert node_mod.NETWORK_CHECK_INTERVAL == 5.0
    assert node_mod.SCAN_TIMEOUT == 3.0
    assert node_mod.MAX_SCAN_TIMEOUT == 60.0


def test_mdns_options_defaults_and_validation():
    from teleport.core.discovery import PROBE_INTERVAL, RESOLVE_TIMEOUT
    from teleport.net.discovery import PROBE_TICK

    o = MdnsOptions()
    assert o.advertise is True
    assert o.interfaces is None and o.advertise_addresses is None
    assert (o.probe_interval, o.resolve_timeout, o.probe_tick) == (
        PROBE_INTERVAL,
        RESOLVE_TIMEOUT,
        PROBE_TICK,
    )
    o = MdnsOptions(interfaces=[LOCAL], advertise_addresses=["192.0.2.1"])
    assert o.interfaces == (LOCAL,) and o.advertise_addresses == ("192.0.2.1",)
    for bad in (
        {"interfaces": LOCAL},
        {"interfaces": ["localhost"]},
        {"advertise_addresses": "192.0.2.1"},
        {"advertise_addresses": ["::1"]},
        {"probe_interval": 0},
        {"resolve_timeout": -1.0},
        {"probe_tick": math.nan},
        {"probe_interval": math.inf},
        {"advertise": 1},
    ):
        with pytest.raises((ValueError, TypeError)):
            MdnsOptions(**bad)


def test_node_constructor_checks_new_arguments(tmp_path):
    with pytest.raises(TypeError):
        Node(tmp_path / "A", name="A", mdns={"advertise": False})
    with pytest.raises(ValueError):
        Node(tmp_path / "A", name="A", auto_connect=1)
    Node(tmp_path / "A", name="A", on_event=None)  # None → hàm rỗng
    assert not (tmp_path / "A").exists()


async def test_new_properties_before_start(tmp_path):
    node = Node(tmp_path / "A", name="A", host=LOCAL, port=0)
    with pytest.raises(RuntimeError):
        node.devices()
    with pytest.raises(RuntimeError):
        node.control_connection("0" * 32)
    await node.close()


@integration
async def test_nf1_start_order_and_advertise_arguments(node_factory, fake_mdns):
    fake_mdns.local_addresses = ("192.0.2.5",)
    A = await node_factory("A", mdns=MdnsOptions())
    m = fake_mdns.last
    assert len(fake_mdns.instances) == 1
    assert m.calls[:4] == ["start", "set_priority", "start_browsing", "advertise"]
    assert m.advertised[0] == {
        "device_id": A.id,
        "name": A.name,
        "os": A.hello.os,
        "port": A.port,
        "addresses": None,
    }
    opts = MdnsOptions()
    assert m.kwargs == {
        "interfaces": opts.interfaces,
        "probe_interval": opts.probe_interval,
        "resolve_timeout": opts.resolve_timeout,
        "probe_tick": opts.probe_tick,
    }
    assert m.self_device_id == A.id
    assert m.priorities[0] == frozenset()
    assert A.discovery_active is True
    assert A.discovery_error is None
    await asyncio.sleep(3 * node_mod.NETWORK_CHECK_INTERVAL)  # địa chỉ không đổi: không khởi động lại
    assert len(fake_mdns.instances) == 1
    assert not m.closing


@integration
async def test_nf1_options_are_passed_to_mdns(node_factory, fake_mdns):
    opts = MdnsOptions(
        interfaces=(LOCAL,),
        advertise_addresses=("192.0.2.7",),
        probe_interval=1.5,
        resolve_timeout=0.7,
        probe_tick=0.3,
    )
    A = await node_factory("A", mdns=opts)
    m = fake_mdns.last
    assert m.kwargs == {
        "interfaces": (LOCAL,),
        "probe_interval": 1.5,
        "resolve_timeout": 0.7,
        "probe_tick": 0.3,
    }
    assert m.advertised[0]["addresses"] == ("192.0.2.7",)
    assert m.advertised[0]["port"] == A.port


@integration
async def test_nf2_advertise_false_and_mdns_none(node_factory, fake_mdns):
    A = await node_factory("A", mdns=MdnsOptions(advertise=False))
    m = fake_mdns.last
    assert "advertise" not in m.calls
    assert m.calls[:3] == ["start", "set_priority", "start_browsing"]
    assert A.discovery_active is True
    assert task_names("teleport.node.netwatch") == []  # chỉ chạy khi có quảng bá
    B = await node_factory("B")
    assert len(fake_mdns.instances) == 1
    assert B.discovery_active is False
    assert B.discovery_error is None


@integration
async def test_nf3_failed_mdns_start_does_not_fail_node(node_factory, fake_mdns):
    fake_mdns.fail_start = OSError("giả lập không mở được socket mDNS")
    A = await node_factory("A", mdns=MdnsOptions(), accept_pairing=True, answer=True)
    first = fake_mdns.instances[0]
    assert A.discovery_active is False
    assert isinstance(A.discovery_error, OSError)
    assert first.closing, "instance lỗi ở start() phải được close()"
    B = await node_factory("B", answer=True)
    result = await asyncio.wait_for(B.pairing.pair_with([LOCAL], A.port), 15)
    assert result.outcome == PairingOutcome.PAIRED  # đường nhập IP vẫn dùng được


@integration
async def test_nf3_mdns_recovers_after_start_error(node_factory, fake_mdns):
    """Mục 3.6: mDNS đang tắt vì lỗi → lượt theo dõi mạng kế tiếp khởi động lại."""
    fake_mdns.fail_start = OSError("giả lập")
    A = await node_factory("A", mdns=MdnsOptions())
    assert A.discovery_active is False
    fake_mdns.fail_start = None
    await wait_until(lambda: A.discovery_active, 3.0, "mDNS chưa được khởi động lại")
    assert A.discovery_error is None
    assert fake_mdns.last.advertised, "instance mới phải quảng bá"
    assert all(m.closing for m in fake_mdns.instances[:-1])


def _teleport_records(caplog, since: int = 0, *, level: int | None = None, min_level: int | None = None):
    """Bản ghi log của các logger `teleport*` từ vị trí `since`, lọc theo mức (đúng / tối thiểu)."""
    return [
        r
        for r in caplog.records[since:]
        if r.name.startswith("teleport")
        and (level is None or r.levelno == level)
        and (min_level is None or r.levelno >= min_level)
    ]


def _describe(records) -> list[str]:
    return [f"{r.levelname} {r.name}: {r.getMessage()}" for r in records]


@integration
async def test_nf3_repeated_mdns_start_error_warns_once(node_factory, fake_mdns, caplog):
    """Sửa F3: mDNS không bật được → netwatch thử lại mỗi vòng nhưng chỉ WARNING lần đầu (các lần sau
    DEBUG); thông điệp lỗi đổi thì WARNING thêm một lần."""
    caplog.set_level(logging.DEBUG, logger="teleport")
    fake_mdns.fail_start = OSError("giả lập không mở được socket mDNS")
    A = await node_factory("A", mdns=MdnsOptions())
    assert A.discovery_active is False
    await wait_until(lambda: len(fake_mdns.instances) >= 4, 5, "netwatch chưa thử bật lại mDNS 3 lần")
    warnings = _teleport_records(caplog, min_level=logging.WARNING)
    assert len(warnings) == 1, _describe(warnings)
    tracebacks = [r for r in _teleport_records(caplog) if r.exc_info]
    assert tracebacks == [], "OSError của mDNS không cần traceback: " + repr(_describe(tracebacks))

    mark, n = len(caplog.records), len(fake_mdns.instances)
    fake_mdns.fail_start = OSError("giả lập lỗi khác: card mạng biến mất")
    await wait_until(lambda: len(fake_mdns.instances) >= n + 3, 5, "netwatch ngừng thử bật lại mDNS")
    changed = _teleport_records(caplog, mark, min_level=logging.WARNING)
    assert len(changed) == 1, _describe(changed)


@integration
async def test_nf3_unexpected_mdns_error_logs_traceback_once(node_factory, fake_mdns, caplog):
    """Sửa F3: lỗi lạ (không phải OSError/DiscoveryError) không in traceback mỗi vòng netwatch."""
    caplog.set_level(logging.DEBUG, logger="teleport")
    fake_mdns.fail_start = RuntimeError("lỗi lạ giả lập trong zeroconf")
    A = await node_factory("A", mdns=MdnsOptions())
    assert A.discovery_active is False
    await wait_until(lambda: len(fake_mdns.instances) >= 4, 5, "netwatch chưa thử bật lại mDNS 3 lần")
    loud = _teleport_records(caplog, min_level=logging.WARNING)
    assert len(loud) == 1, _describe(loud)
    tracebacks = [r for r in _teleport_records(caplog) if r.exc_info]
    assert len(tracebacks) <= 1, _describe(tracebacks)


@integration
async def test_nf3_mdns_recovery_logs_info_once_then_warns_again(node_factory, fake_mdns, caplog):
    """Sửa F3: bật lại được → INFO đúng một lần và đặt lại; lỗi lần sau lại được WARNING."""
    caplog.set_level(logging.DEBUG, logger="teleport")
    fake_mdns.local_addresses = ("192.0.2.5",)
    fake_mdns.fail_start = OSError("giả lập không mở được socket mDNS")
    A = await node_factory("A", mdns=MdnsOptions())
    await wait_until(lambda: len(fake_mdns.instances) >= 3, 5, "netwatch chưa thử bật lại mDNS")
    mark = len(caplog.records)
    fake_mdns.fail_start = None
    await wait_until(lambda: A.discovery_active, 3, "mDNS chưa bật lại được")
    reads = fake_mdns.addr_reads
    await wait_until(lambda: fake_mdns.addr_reads >= reads + 3, 3, "netwatch ngừng sau khi bật lại mDNS")
    infos = _teleport_records(caplog, mark, level=logging.INFO)
    assert len(infos) == 1, _describe(infos)
    assert _teleport_records(caplog, mark, min_level=logging.WARNING) == []

    mark, n = len(caplog.records), len(fake_mdns.instances)
    fake_mdns.fail_start = OSError("giả lập không mở được socket mDNS")  # cùng thông điệp như lần đầu
    fake_mdns.local_addresses = ("192.0.2.6",)  # địa chỉ đổi → khởi động lại mDNS → lỗi
    await wait_until(lambda: len(fake_mdns.instances) >= n + 3, 5, "netwatch chưa thử bật lại mDNS")
    again = _teleport_records(caplog, mark, min_level=logging.WARNING)
    assert len(again) == 1, "sau khi bật lại được, lỗi mới phải được báo lại: " + repr(_describe(again))


@integration
async def test_nf3_failed_advertise_keeps_browsing(node_factory, fake_mdns):
    fake_mdns.fail_advertise = DiscoveryError("không có địa chỉ IPv4 nào để quảng bá")
    A = await node_factory("A", mdns=MdnsOptions())
    m = fake_mdns.instances[0]
    assert "start_browsing" in m.calls
    assert not m.closing
    assert A.discovery_active is True
    assert isinstance(A.discovery_error, DiscoveryError)


@integration
async def test_nf4_priorities_follow_trusted_devices(node_factory, fake_mdns):
    A = await node_factory("A", mdns=MdnsOptions(), accept_pairing=True, answer=True)
    B = await node_factory("B", mdns=MdnsOptions(), answer=True)
    result = await asyncio.wait_for(B.pairing.pair_with([LOCAL], A.port), 15)
    assert result.outcome == PairingOutcome.PAIRED
    await wait_until(lambda: fake_mdns.of(A).priorities[-1] == frozenset({B.id}), 2, "A chưa ưu tiên B")
    await wait_until(lambda: fake_mdns.of(B).priorities[-1] == frozenset({A.id}), 2, "B chưa ưu tiên A")
    await A.unpair(B.id)
    assert fake_mdns.of(A).priorities[-1] == frozenset()


@integration
async def test_nf5_unpaired_device_online_and_offline(
    node_factory, fake_mdns, count_open_control, app_helpers
):
    counter = count_open_control()
    A = await node_factory("A", mdns=MdnsOptions())
    stranger = load_or_create_identity(A.dir.parent / "stranger")
    d = app_helpers.dev(stranger, port=47474, name="Lạ", addresses=("192.0.2.40",), os="linux")
    fake_mdns.of(A).emit(DeviceAdded(d))
    [ev] = A.rec.events_of(DeviceOnline)
    assert ev.device.device_id == stranger.device_id
    assert ev.device.paired is False and ev.device.online is True and ev.device.connected is False
    assert ev.device.name == "Lạ" and ev.device.os == "linux"
    assert ev.device.addresses == ("192.0.2.40",) and ev.device.port == 47474
    assert [v.device_id for v in A.devices()] == [stranger.device_id]
    await asyncio.sleep(0.3)
    assert counter.count() == 0, "không bao giờ nối tới thiết bị chưa ghép đôi"
    fake_mdns.of(A).emit(DeviceRemoved(d))
    [off] = A.rec.events_of(DeviceOffline)
    assert off.device.device_id == stranger.device_id
    assert off.device.online is False and off.device.addresses == () and off.device.port is None
    assert A.devices() == ()
    assert len(A.rec.events_of(DeviceOnline)) == 1


@integration
async def test_nf5_updated_event_is_silent(node_factory, fake_mdns, app_helpers):
    A = await node_factory("A", mdns=MdnsOptions())
    stranger = load_or_create_identity(A.dir.parent / "stranger")
    d1 = app_helpers.dev(stranger, port=47474, addresses=("192.0.2.40",))
    d2 = app_helpers.dev(stranger, port=47475, addresses=("192.0.2.41",))
    fake_mdns.of(A).emit(DeviceAdded(d1))
    fake_mdns.of(A).emit(DeviceUpdated(d2, d1))
    assert len(A.rec.events) == 1
    [v] = A.devices()
    assert (v.port, v.addresses) == (47475, ("192.0.2.41",))


@integration
async def test_nf6_address_change_restarts_mdns(node_factory, fake_mdns, app_helpers):
    fake_mdns.local_addresses = ("192.0.2.5",)
    A = await node_factory("A", mdns=MdnsOptions())
    first = fake_mdns.instances[0]
    stranger = load_or_create_identity(A.dir.parent / "stranger")
    d = app_helpers.dev(stranger, port=47474, addresses=("192.0.2.40",))
    first.emit(DeviceAdded(d))
    await wait_until(lambda: fake_mdns.addr_reads >= 2, 2, "node chưa đọc địa chỉ card mạng")
    t0 = time.monotonic()
    fake_mdns.local_addresses = ("192.0.2.6",)
    await wait_until(lambda: len(fake_mdns.instances) >= 2, 2, "mDNS chưa được khởi động lại")
    second = fake_mdns.instances[1]
    await wait_until(lambda: bool(second.advertised), 2, "instance mới chưa quảng bá")
    assert time.monotonic() - t0 <= 2.0
    assert first.closed
    log = fake_mdns.log
    assert log.index((0, "closed")) < log.index((1, "start")), (
        "instance cũ phải đóng xong trước khi mới start"
    )
    offline = A.rec.events_of(DeviceOffline, stranger.device_id)
    assert len(offline) == 1, "thiết bị đang thấy phải được báo rời mạng khi khởi động lại mDNS"
    assert A.devices() == ()
    n = len(A.rec.events)
    first.on_event(DeviceAdded(d))  # sự kiện muộn từ instance cũ
    assert len(A.rec.events) == n, "sự kiện từ instance cũ không được tới on_event"
    assert A.discovery_active


@integration
async def test_nf6_fixed_advertise_addresses_only_readvertises(node_factory, fake_mdns):
    """Mục 3.6: có `advertise_addresses` (test) → địa chỉ đổi chỉ quảng bá lại, không khởi động lại."""
    fake_mdns.local_addresses = ("192.0.2.5",)
    await node_factory("A", mdns=MdnsOptions(advertise_addresses=("192.0.2.9",)))
    m = fake_mdns.instances[0]
    await wait_until(lambda: fake_mdns.addr_reads >= 2, 2)
    fake_mdns.local_addresses = ("192.0.2.6",)
    await wait_until(lambda: len(m.advertised) >= 2, 2, "chưa quảng bá lại")
    assert len(fake_mdns.instances) == 1
    assert m.advertised[-1] == m.advertised[0]


@integration
async def test_nf6_address_read_error_skips_round(node_factory, fake_mdns):
    fake_mdns.local_addresses = ("192.0.2.5",)
    await node_factory("A", mdns=MdnsOptions())
    await wait_until(lambda: fake_mdns.addr_reads >= 2, 2)
    fake_mdns.local_addresses = OSError("giả lập lỗi đọc card mạng")
    reads = fake_mdns.addr_reads
    await wait_until(lambda: fake_mdns.addr_reads >= reads + 3, 3, "netwatch ngừng sau lỗi đọc")
    fake_mdns.local_addresses = ("192.0.2.5",)
    await asyncio.sleep(0.5)
    assert len(fake_mdns.instances) == 1


@integration
async def test_nf7_wake_readvertises_and_retries_now(
    node_factory, fake_mdns, count_open_control, monkeypatch, tmp_path
):
    counter = count_open_control()
    monkeypatch.setattr(links, "RETRY_BASE", 30.0)
    monkeypatch.setattr(links, "RETRY_MAX", 30.0)
    b_id = load_or_create_identity(tmp_path / "B")
    seed_trust(tmp_path / "A", b_id, "B")
    await node_factory("A", mdns=MdnsOptions())
    m = fake_mdns.instances[0]
    # Một thiết bị tin cậy ở cổng đóng: lần thử đầu thất bại → chờ 30 s.
    port = free_port()
    m.emit(DeviceAdded(make_dev(b_id, port)))
    await wait_until(lambda: counter.count(to=b_id) >= 1, 3, "chưa thử nối B")
    await asyncio.sleep(0.1)
    await wait_until(lambda: not task_names("teleport.node.link-out"), 5, "lần thử đầu chưa xong")
    assert counter.count(to=b_id) == 1
    await wait_until(lambda: fake_mdns.addr_reads >= 2, 2)
    n_adv = len(m.advertised)
    real_wall = time.time
    monkeypatch.setattr(node_mod, "_wall_clock", lambda: real_wall() + 3600)
    t0 = time.monotonic()
    await wait_until(lambda: len(m.advertised) > n_adv, 1.0, "chưa quảng bá lại sau khi thức dậy")
    assert len(fake_mdns.instances) == 1, "thức dậy chỉ quảng bá lại, không khởi động lại mDNS"
    assert m.advertised[-1] == m.advertised[0]
    await wait_until(lambda: counter.count(to=b_id) >= 2, 1.5, "thiết bị đang chờ lịch dài chưa được thử lại")
    assert counter.calls[-1].t - t0 <= 1.5


def make_dev(identity, port: int, *, name: str = "B", addresses=(LOCAL,)):
    from teleport.core.discovery import DiscoveredDevice
    from teleport.core.messages import PROTOCOL_VERSION

    return DiscoveredDevice(
        device_id=identity.device_id,
        name=name,
        os="windows",
        proto=PROTOCOL_VERSION,
        port=port,
        addresses=addresses,
    )


@integration
async def test_nf8_close_closes_mdns_and_stops_background(node_factory, fake_mdns, app_helpers):
    A = await node_factory("A", mdns=MdnsOptions())
    m = fake_mdns.last
    assert task_names("teleport.node.netwatch") and task_names("teleport.node.links-timer")
    await A.close()
    assert m.closed
    assert task_names("teleport.node") == []
    assert app_helpers.lock_is_free(A.dir)
    stranger = load_or_create_identity(A.dir.parent / "stranger")
    n = len(A.rec.events)
    if m.on_event is not None:
        m.on_event(DeviceAdded(app_helpers.dev(stranger, port=1)))  # sự kiện muộn: bị bỏ qua
    assert len(A.rec.events) == n


@integration
async def test_nf9_cancelled_close_still_closes_mdns(node_factory, fake_mdns, app_helpers):
    A = await node_factory("A", mdns=MdnsOptions())
    m = fake_mdns.last
    fake_mdns.close_delay = 2.0
    closing = asyncio.create_task(A.close(), name="test.close")
    await asyncio.sleep(0.1)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert app_helpers.lock_is_free(A.dir), "khóa phải được nhả ngay khi close() bị hủy"
    await wait_until(lambda: m.closed, 3.0, "mDNS chưa được đóng sau khi close() bị hủy")
    await wait_until(lambda: not task_names("teleport.node"), 1.0, f"còn task {task_names('teleport.node')}")


@integration
@pytest.mark.parametrize("hang", ["start", "start_browsing", "advertise"])
async def test_start_cancelled_after_mdns_created_closes_it(tmp_path, fast, fake_mdns, app_helpers, hang):
    """Mục 3.3 bước 7: `start` bị hủy sau khi đã tạo MdnsDiscovery → đóng mDNS trước khi nhả khóa."""
    fake_mdns.hang = hang
    node = Node(tmp_path / "A", name="A", host=LOCAL, port=0, mdns=MdnsOptions())
    task = asyncio.create_task(node.start(), name="test.start")
    await wait_until(lambda: bool(fake_mdns.instances) and hang in fake_mdns.instances[-1].calls, 3)
    m = fake_mdns.last
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await node.close()
    await wait_until(lambda: m.closed, 2, "mDNS chưa đóng sau khi start bị hủy")
    assert app_helpers.lock_is_free(tmp_path / "A")
    assert task_names("teleport.node") == []


async def _assert_port_not_accepting(port: int) -> None:
    """Cổng không còn nhận kết nối: TCP bị từ chối, hoặc nếu lọt vào thì bị đóng ngay (≤ 1 s).

    (Chép từ tests/app/test_node.py.) Listener còn sống thì giữ kết nối TCP trần tới hết thời gian
    mở đầu (2 s với `fast`).
    """
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(LOCAL, port), 5)
    except (OSError, TimeoutError):
        return  # bị từ chối (Windows mất ~2 s mới báo)
    try:
        async with asyncio.timeout(1.0):
            while True:
                try:
                    chunk = await reader.read(1024)
                except (ConnectionError, OSError):
                    return
                if not chunk:
                    return
    except TimeoutError:
        raise AssertionError(f"cổng {port} vẫn nhận và giữ kết nối TCP sau khi start() thất bại") from None
    finally:
        writer.transport.abort()


@integration
async def test_start_cancelled_twice_closes_port_before_releasing_lock(
    tmp_path, fast, fake_mdns, app_helpers
):
    """Sửa F1 (review PR-4): `start()` bị hủy lúc đang bật mDNS, rồi bị hủy lần nữa lúc đang dọn
    (đóng mDNS chậm). Sau khi `start()` raise: cổng không còn nhận kết nối, `node.lock` lấy lại được,
    mDNS vẫn được đóng (chạy nền) và không còn task của node sau một nhịp ngắn."""
    fake_mdns.hang = "start"  # start() của node bị hủy lần 1 ở đây
    fake_mdns.close_delay = 1.0  # close() chậm: lần hủy thứ hai rơi vào lúc đang dọn
    directory = tmp_path / "A"
    node = Node(directory, name="A", host=LOCAL, port=0, mdns=MdnsOptions())
    task = asyncio.create_task(node.start(), name="test.start")
    await wait_until(
        lambda: bool(fake_mdns.instances) and "start" in fake_mdns.last.calls, 3, "chưa bật mDNS"
    )
    m = fake_mdns.last
    port = node.port
    assert isinstance(port, int) and port > 0, "cổng phải đã mở trước khi bật mDNS"
    task.cancel()
    await wait_until(lambda: "close" in m.calls, 2, "start() bị hủy mà chưa đóng mDNS")
    assert not task.done()
    task.cancel()  # lần 2: đang chờ đóng mDNS
    with pytest.raises(asyncio.CancelledError):
        await task
    assert app_helpers.lock_is_free(directory), "node.lock phải được nhả khi start() thất bại"
    await _assert_port_not_accepting(port)
    await wait_until(lambda: m.closed, fake_mdns.close_delay + fast.SLACK, "mDNS chưa được đóng (chạy nền)")
    await wait_until(lambda: not task_names("teleport.node"), 1.0, f"còn task {task_names('teleport.node')}")
    with pytest.raises(RuntimeError):
        await node.start()  # start() đã thất bại: không gọi lại được
    await node.close()  # không làm gì, không lỗi
    assert app_helpers.lock_is_free(directory)


def _port_is_free(port: int) -> bool:
    """Bind lại được đúng cổng (không SO_REUSEADDR) ⇔ không còn socket nào nghe trên cổng đó."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((LOCAL, port))
    except OSError:
        return False
    else:
        return True
    finally:
        s.close()


_START_SWEEP_TICKS = 40  # đủ phủ toàn bộ Node.start() với FakeMdns; dừng ở nhịp đầu tiên start() đã xong


@integration
@pytest.mark.parametrize("with_mdns", [False, True], ids=["no-mdns", "mdns"])
async def test_start_cancelled_at_any_tick_releases_lock_only_after_port_closed(
    tmp_path, fast, fake_mdns, app_helpers, with_mdns
):
    """Sửa G1 (review PR-4 vòng 2): `Node.start()` bị hủy ở nhịp loop thứ k (và có khi bị hủy lần
    hai sau j nhịp nữa). Ngay lúc `start()` kết thúc (done-callback, đồng bộ): `node.lock` đã nhả
    và cổng đã đóng — không bao giờ có lúc khóa đã nhả mà cổng còn nghe (node khác lấy được khóa
    nhưng không mở được cổng). Sau đó mDNS được đóng (có thể chạy nền), không còn task của node."""
    directory = tmp_path / "A"
    app_helpers.ensure_identity(directory)
    opts = MdnsOptions(advertise=False) if with_mdns else None
    outcomes: list[str] = []
    for k in range(_START_SWEEP_TICKS):
        for j in (None, 0, 1, 3):
            port = free_port()
            node = Node(directory, name="A", host=LOCAL, port=port, mdns=opts)
            snap: dict[str, bool] = {}

            async def run_start(node=node, port=port, snap=snap) -> None:
                # Đo ngay trong bước mà start() kết thúc (done-callback thì chạy muộn một nhịp).
                try:
                    await node.start()
                finally:
                    snap["lock_free"] = app_helpers.lock_is_free(directory)
                    snap["port_free"] = _port_is_free(port)

            task = asyncio.create_task(run_start(), name="test.start")
            for _ in range(k):
                await asyncio.sleep(0)
            if task.done():
                break
            task.cancel()
            if j is not None:
                for _ in range(j):
                    await asyncio.sleep(0)
                if not task.done():
                    task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                outcomes.append("cancelled")
            else:
                outcomes.append("done-anyway")  # lần hủy rơi đúng lúc start() vừa xong: hợp lệ
                await node.close()
                assert app_helpers.lock_is_free(directory) and _port_is_free(port), f"k={k} j={j}"
                continue
            where = f"k={k} j={j}"
            if not snap:  # bị hủy trước khi kịp chạy: start() chưa hề được gọi
                assert app_helpers.lock_is_free(directory) and _port_is_free(port), where
                continue
            assert not (snap["lock_free"] and not snap["port_free"]), (
                f"{where}: node.lock đã nhả mà cổng {port} vẫn nghe"
            )
            assert snap["lock_free"], f"{where}: start() đã raise mà node.lock chưa nhả"
            assert snap["port_free"], f"{where}: start() đã raise mà cổng {port} vẫn nghe"
            await node.close()  # start() thất bại: close() không làm gì, không lỗi
            assert _port_is_free(port), where
        else:
            continue
        # start() đã xong trước khi kịp hủy: dừng node như bình thường.
        assert task.result() is None
        assert not _port_is_free(port)
        outcomes.append("done")
        await node.close()
        assert app_helpers.lock_is_free(directory) and _port_is_free(port)
        break
    assert outcomes and outcomes[-1] == "done", f"{_START_SWEEP_TICKS} nhịp chưa phủ hết start(): {outcomes}"
    assert "cancelled" in outcomes, outcomes
    await wait_until(
        lambda: all(m.closed for m in fake_mdns.instances), 2, "còn FakeMdns chưa đóng sau khi start bị hủy"
    )
    await wait_until(lambda: not task_names("teleport.node"), 1.0, f"còn task {task_names('teleport.node')}")


# =================================================================================================
# NL1–NL12: tự nối kết nối điều khiển
# =================================================================================================


async def pair_up(node_factory, trust_each_other, labels=("A", "B"), **kw):
    trust_each_other(*labels)
    nodes = [await node_factory(label, mdns=MdnsOptions(), **kw) for label in labels]
    return nodes


@integration
async def test_nl1_one_side_sees_other_and_both_connect(
    node_factory, trust_each_other, fake_mdns, app_helpers, count_open_control
):
    counter = count_open_control()
    A, B = await pair_up(node_factory, trust_each_other)
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    await wait_until(lambda: B.id in A.connected_devices(), 2, "A chưa nối tới B")
    await wait_until(lambda: A.id in B.connected_devices(), 2, "B chưa ghi nhận A")
    kinds = [type(e).__name__ for e in A.rec.events if e.device.device_id == B.id]
    assert kinds[:2] == ["DeviceOnline", "DeviceConnected"]
    [up_a] = A.rec.events_of(DeviceConnected, B.id)
    assert up_a.outgoing is True and up_a.device.connected is True and up_a.device.paired is True
    up_b = await B.rec.wait_event(DeviceConnected, A.id)
    assert up_b.outgoing is False
    assert len(B.rec.events_of(DeviceConnected, A.id)) == 1
    assert counter.count(caller=A) == 1 and counter.count(caller=B) == 0
    assert counter.calls[0].fingerprint == B.identity.fingerprint
    assert A.control_connection(B.id) is not None and A.control_connection(B.id).is_server is False
    assert B.control_connection(A.id).is_server is True
    [v] = [v for v in A.devices() if v.device_id == B.id]
    assert v.connected and v.online and v.paired


@integration
async def test_nl2_simultaneous_connects_converge_to_one(
    node_factory, trust_each_other, fake_mdns, app_helpers, count_open_control, fast
):
    counter = count_open_control()
    A, B = await pair_up(node_factory, trust_each_other)
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    fake_mdns.of(B).emit(DeviceAdded(app_helpers.dev(A)))
    await wait_until(
        lambda: A.connected_devices() == {B.id} and B.connected_devices() == {A.id}, 3, "chưa kết nối hai bên"
    )
    await asyncio.sleep(0.5)
    n_calls = counter.count()
    await asyncio.sleep(fast.REPLACE_AFTER + 2)
    assert A.connected_devices() == {B.id} and B.connected_devices() == {A.id}
    assert counter.count() == n_calls, "vẫn còn thử nối sau khi đã hội tụ"
    assert connected_count(A.rec, B.id) == 1
    assert connected_count(B.rec, A.id) == 1
    lo, hi = sorted([A, B], key=lambda n: n.id)
    assert lo.control_connection(hi.id).is_server is False, "giữ kết nối do bên có device_id nhỏ hơn mở"
    assert hi.control_connection(lo.id).is_server is True
    # Hai bên giữ cùng một kết nối: bên nhỏ là client của bên lớn.
    assert lo.control_connection(hi.id).peer.cert.device_id == hi.id


@integration
async def test_nl3_peer_closes_then_retry_schedule_and_one_unreachable(
    node_factory, trust_each_other, fake_mdns, app_helpers, count_open_control, fast
):
    counter = count_open_control()
    A, B = await pair_up(node_factory, trust_each_other)
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    await wait_until(lambda: B.id in A.connected_devices(), 2)
    base = counter.count(to=B)
    await B.close()
    t_close = time.monotonic()
    down = await A.rec.wait_event(DeviceDisconnected, B.id, timeout=1.0 + 0.5)
    assert A.rec.event_times[A.rec.events.index(down)] - t_close <= 1.0
    assert down.device.online is True  # còn thấy qua mDNS → CLI in “Đang nối lại…”
    failed = await A.rec.wait_event(ConnectFailed, B.id, timeout=20)
    assert failed.result == ConnectResult.UNREACHABLE
    attempts = counter.of(to=B)[base:]
    assert len(attempts) >= 3, "chỉ báo UNREACHABLE sau 3 lần thất bại liên tiếp"
    starts = [c.t for c in attempts]
    # Lịch đã patch: sau khi mất kết nối +0,2; sau mỗi thất bại +0,2, +0,4, +0,8, +1,0 (đo từ lúc thất bại,
    # nên khoảng giữa hai lần bắt đầu ≥ khoảng chờ).
    for i, (a, b) in enumerate(itertools.pairwise(starts)):
        expected = min(fast.RETRY_BASE * 2**i, fast.RETRY_MAX)
        assert b - a >= 0.8 * expected, f"lần thử {i + 2} sớm: {b - a:.3f} s < 80 % của {expected}"
    await asyncio.sleep(1.5)
    assert len(A.rec.events_of(ConnectFailed, B.id)) == 1, "UNREACHABLE chỉ báo một lần"
    fake_mdns.of(A).emit(DeviceRemoved(app_helpers.dev(B)))
    n = counter.count(to=B)
    await asyncio.sleep(2.0)
    assert counter.count(to=B) == n, "thiết bị đã rời mạng mà vẫn thử nối"


@integration
async def test_nl4_restarted_peer_with_new_port_connects_fast(
    node_factory, trust_each_other, fake_mdns, app_helpers, monkeypatch
):
    A, B = await pair_up(node_factory, trust_each_other)
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    await wait_until(lambda: B.id in A.connected_devices(), 2)
    old = app_helpers.dev(B)
    monkeypatch.setattr(links, "RETRY_BASE", 10.0)  # lịch dài: lần thử kế sau 10 s
    monkeypatch.setattr(links, "RETRY_MAX", 30.0)
    await B.close()
    await A.rec.wait_event(DeviceDisconnected, B.id, timeout=2)
    B2 = await node_factory("B", mdns=MdnsOptions())
    t0 = time.monotonic()
    fake_mdns.of(A).emit(DeviceUpdated(app_helpers.dev(B2), old))
    await wait_until(lambda: B2.id in A.connected_devices(), 1.0, "A chưa nối lại B trên cổng mới")
    assert time.monotonic() - t0 <= 1.0
    await wait_until(lambda: A.id in B2.connected_devices(), 1.0)


@integration
async def test_nl5_rejected_after_peer_unpairs(
    node_factory, trust_each_other, fake_mdns, app_helpers, count_open_control
):
    counter = count_open_control()
    A, B = await pair_up(node_factory, trust_each_other)
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    await wait_until(lambda: B.id in A.connected_devices(), 2)
    await B.unpair(A.id)
    await A.rec.wait_event(DeviceDisconnected, B.id, timeout=2)
    failed = await A.rec.wait_event(ConnectFailed, B.id, timeout=5)
    assert failed.result == ConnectResult.REJECTED
    await wait_until(lambda: not task_names("teleport.node.link-out"), 3)
    n = counter.count(to=B)
    await asyncio.sleep(3.0)
    assert counter.count(to=B) == n, "bị từ chối mà vẫn thử lại theo lịch"
    fake_mdns.of(A).emit(DeviceUpdated(app_helpers.dev(B), app_helpers.dev(B)))
    await wait_until(lambda: counter.count(to=B) == n + 1, 2, "sự kiện mDNS mới phải cho đúng một lần thử")
    await asyncio.sleep(1.0)
    assert counter.count(to=B) == n + 1
    assert len(A.rec.events_of(ConnectFailed, B.id)) == 1, "REJECTED chỉ báo một lần"
    assert B.id not in A.connected_devices()


@integration
async def test_nl6_spoofed_mdns_identity_mismatch(node_factory, fake_mdns, count_open_control, tmp_path):
    counter = count_open_control()
    b_id = load_or_create_identity(tmp_path / "B")
    seed_trust(tmp_path / "A", b_id, "B")
    A = await node_factory("A", mdns=MdnsOptions())
    C = await node_factory("C")
    before = (A.dir / "trusted.json").read_bytes()
    spoof = make_dev(b_id, C.port)
    m = fake_mdns.of(A)
    m.emit(DeviceAdded(spoof))
    failed = await A.rec.wait_event(ConnectFailed, b_id.device_id, timeout=5)
    assert failed.result == ConnectResult.IDENTITY_MISMATCH
    assert failed.device.name == "B" and failed.device.paired is True
    await wait_until(lambda: not task_names("teleport.node.link-out"), 3)
    n = counter.count(to=b_id)
    t_end = time.monotonic() + 1.0
    i = 0
    while time.monotonic() < t_end:
        m.emit(DeviceUpdated(make_dev(b_id, C.port, name=f"B{i % 2}"), spoof))
        i += 1
        await asyncio.sleep(0.02)
    assert counter.count(to=b_id) - n <= 2, f"giả mạo mDNS gây {counter.count(to=b_id) - n} lần thử trong 1 s"
    await asyncio.sleep(0.3)
    assert len(A.rec.events_of(ConnectFailed, b_id.device_id)) == 1
    assert (A.dir / "trusted.json").read_bytes() == before
    assert C.connected_devices() == frozenset()
    assert A.connected_devices() == frozenset()
    assert [v.name for v in A.devices() if v.device_id == b_id.device_id] == ["B"]


@integration
async def test_link_failed_is_logged_at_info_not_warning(
    node_factory, fake_mdns, count_open_control, tmp_path, caplog
):
    """Sửa F4: `LinkFailed` đã được báo người dùng qua `on_event` (CLI in câu của mvp), nên log chỉ ở
    mức INFO — `listen` không in hai dòng cho một lỗi."""
    caplog.set_level(logging.DEBUG, logger="teleport")
    count_open_control()
    b_id = load_or_create_identity(tmp_path / "B")
    seed_trust(tmp_path / "A", b_id, "B")
    A = await node_factory("A", mdns=MdnsOptions())
    C = await node_factory("C")
    fake_mdns.of(A).emit(DeviceAdded(make_dev(b_id, C.port)))  # giả mạo: cổng của C, mã của B
    failed = await A.rec.wait_event(ConnectFailed, b_id.device_id, timeout=5)
    assert failed.result == ConnectResult.IDENTITY_MISMATCH
    await wait_until(lambda: not task_names("teleport.node.link-out"), 3)
    short = b_id.device_id[:8]
    about_b = [r for r in _teleport_records(caplog) if short in r.getMessage()]
    loud = [r for r in about_b if r.levelno >= logging.WARNING]
    assert loud == [], _describe(loud)
    assert any(r.levelno == logging.INFO for r in about_b), (
        "lỗi kết nối vẫn phải được ghi (mức INFO): " + repr(_describe(about_b))
    )


@integration
async def test_nl7_unpair_outgoing_connection(
    node_factory, trust_each_other, fake_mdns, app_helpers, count_open_control
):
    counter = count_open_control()
    A, B = await pair_up(node_factory, trust_each_other)
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    await wait_until(lambda: A.id in B.connected_devices() and B.id in A.connected_devices(), 2)
    assert A.control_connection(B.id).is_server is False
    t0 = time.monotonic()
    task = asyncio.create_task(A.unpair(B.id), name="test.unpair")
    await asyncio.sleep(0)
    assert B.id not in A.connected_devices(), "unpair phải bỏ kết nối trước await đầu tiên"
    await asyncio.wait_for(task, 3)
    await wait_until(lambda: A.id not in B.connected_devices(), max(0.05, 1.0 - (time.monotonic() - t0)))
    n = counter.count(to=B)
    fake_mdns.of(A).emit(DeviceUpdated(app_helpers.dev(B), app_helpers.dev(B)))
    await asyncio.sleep(0.5)
    assert counter.count(to=B) == n, "đã hủy ghép đôi mà vẫn thử nối"
    assert A.control_connection(B.id) is None


@integration
async def test_nl8_unpair_cancels_running_attempt(node_factory, fake_mdns, fake_server, tmp_path, fast):
    """NL8: hủy ghép đôi dừng ngay việc nối tới máy đó (đo theo review PR-4: khẳng định lúc `unpair`
    trả về, còn thời gian task kết thúc hẳn có biên nới cho CI)."""
    b_id = load_or_create_identity(tmp_path / "B")
    seed_trust(tmp_path / "A", b_id, "B")
    A = await node_factory("A", mdns=MdnsOptions())

    async def silent(reader, writer):
        await asyncio.sleep(30)  # treo: không bao giờ bắt tay TLS, không gửi hello

    port = await fake_server(None, silent, use_tls=False)  # TCP nhận nhưng không bắt tay TLS
    fake_mdns.of(A).emit(DeviceAdded(make_dev(b_id, port)))
    await wait_until(lambda: bool(task_names("teleport.node.link-out")), 2, "chưa có lần thử")
    await asyncio.sleep(0.2)
    link_out = [t for t in asyncio.all_tasks() if t.get_name().startswith("teleport.node.link-out")]
    assert link_out and all(not t.done() for t in link_out), "lần thử phải đang treo ở bắt tay"
    tracker = A.node._links  # truy vấn công khai của LinkTracker; Node không có API riêng cho nó
    assert b_id.device_id in tracker.attempting()
    await A.unpair(b_id.device_id)
    # Ngay khi unpair trả về: B không còn trong lần thử đang chạy, task link-out đã bị hủy (hoặc xong).
    t0 = time.monotonic()
    assert b_id.device_id not in tracker.attempting(), "unpair phải bỏ lần thử đang chạy tới B"
    assert all(t.done() or t.cancelling() for t in link_out), "unpair phải hủy task link-out tới B"
    limit = 1.0 + fast.SLACK
    await wait_until(lambda: all(t.done() for t in link_out), limit, "task link-out chưa kết thúc")
    assert time.monotonic() - t0 <= limit
    await asyncio.sleep(0.3)
    assert A.rec.events_of(ConnectFailed) == []
    assert task_names("teleport.node.link-out") == [], "đã hủy ghép đôi mà vẫn có lần thử mới tới B"


@integration
async def test_nl9_pairing_then_auto_connect(node_factory, fake_mdns, app_helpers, count_open_control):
    count_open_control()
    A = await node_factory("A", mdns=MdnsOptions(), accept_pairing=True, answer=True)
    B = await node_factory("B", mdns=MdnsOptions(), accept_pairing=True, answer=True)
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    fake_mdns.of(B).emit(DeviceAdded(app_helpers.dev(A)))
    await asyncio.sleep(0.2)
    assert A.connected_devices() == frozenset()
    result = await asyncio.wait_for(A.pairing.pair_with([LOCAL], B.port), 15)
    assert result.outcome == PairingOutcome.PAIRED
    t0 = time.monotonic()
    await wait_until(
        lambda: A.connected_devices() == {B.id} and B.connected_devices() == {A.id},
        2,
        "chưa tự nối sau ghép đôi",
    )
    assert time.monotonic() - t0 <= 2.0
    await asyncio.sleep(0.5)
    assert connected_count(A.rec, B.id) == 1
    assert connected_count(B.rec, A.id) == 1


@integration
async def test_nl10_raising_on_event_is_logged(
    node_factory, trust_each_other, fake_mdns, app_helpers, caplog
):
    caplog.set_level(logging.DEBUG)
    trust_each_other("A", "B")
    A = await node_factory("A", mdns=MdnsOptions(), raise_in_event=True)
    B = await node_factory("B", mdns=MdnsOptions(), raise_in_event=True)
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    await wait_until(lambda: B.id in A.connected_devices() and A.id in B.connected_devices(), 2)
    assert [type(e) for e in A.rec.events][:2] == [DeviceOnline, DeviceConnected]
    assert any(r.exc_info and r.levelno >= logging.ERROR for r in caplog.records), "phải log exception"


@integration
async def test_nl11_close_while_busy_with_links(
    node_factory, trust_each_other, fake_mdns, app_helpers, fake_server, tmp_path
):
    trust_each_other("A", "B")
    trust_each_other("A", "C", name_b="C")
    h_id = load_or_create_identity(tmp_path / "H")
    TrustStore.load(tmp_path / "A").add(peer_cert_from_pem(h_id.cert_pem()), "H")
    A = await node_factory("A", mdns=MdnsOptions())
    B = await node_factory("B", mdns=MdnsOptions())
    C = await node_factory("C", mdns=MdnsOptions())

    async def silent(reader, writer):
        await asyncio.sleep(30)

    h_port = await fake_server(None, silent, use_tls=False)
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))  # kết nối ra
    fake_mdns.of(C).emit(DeviceAdded(app_helpers.dev(A)))  # kết nối vào
    await wait_until(lambda: A.connected_devices() == {B.id, C.id}, 3, "chưa đủ kết nối")
    fake_mdns.of(A).emit(DeviceAdded(make_dev(h_id, h_port, name="H")))  # lần thử treo
    await wait_until(lambda: bool(task_names("teleport.node.link-out")), 2)
    assert A.control_connection(B.id).is_server is False
    assert A.control_connection(C.id).is_server is True
    t0 = time.monotonic()
    await asyncio.wait_for(A.close(), 10)
    assert time.monotonic() - t0 <= 6.0
    assert app_helpers.lock_is_free(A.dir)
    await wait_until(lambda: A.id not in B.connected_devices(), 2, "B chưa thấy A đóng")
    await wait_until(lambda: A.id not in C.connected_devices(), 2, "C chưa thấy A đóng")
    await B.close()
    await C.close()


@integration
async def test_nl12_close_cancelled_while_pairing(node_factory, fast, app_helpers):
    """Backlog a: `close()` bị hủy khi đang ghép đôi → phiên bị hủy trước khi nhả khóa, không ghi kho."""
    A = await node_factory("A")
    B = await node_factory("B", accept_pairing=True)
    task = asyncio.create_task(A.pairing.pair_with([LOCAL], B.port), name="test.pair")
    await A.rec.wait_prompts()
    await B.rec.wait_prompts()
    A.pairing.decide(A.rec.prompts[0].session_id, True)
    closing = asyncio.create_task(A.close(), name="test.close")
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert app_helpers.lock_is_free(A.dir)
    t0 = time.monotonic()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, fast.ABORT_ON_CANCEL_TIMEOUT + 1 + 1)
    assert time.monotonic() - t0 <= fast.ABORT_ON_CANCEL_TIMEOUT + 1
    await B.rec.wait_finished(1, timeout=3)
    assert str(B.rec.finished[0].outcome) in ("peer_rejected", "connection_lost")
    assert TrustStore.load(A.dir).get(B.id) is None
    await wait_until(lambda: not task_names("teleport.pairing"), 2)


@integration
async def test_abort_all_blocks_later_save(node_factory):
    """Mục 2.5: sau `abort_all()` không bao giờ ghi trusted.json, kể cả phiên trong task đang gọi.

    `on_prompt` của A đồng ý rồi gọi `abort_all()`. Nếu `on_prompt` chạy trong task của phiên thì phiên
    đó không bị hủy, đi tiếp tới `SavePeer` và `_save` phải từ chối (SAVE_FAILED); nếu không thì phiên
    bị hủy (CANCELLED). Cả hai cách: kho của A không có B.
    """
    B = await node_factory("B", accept_pairing=True, answer=True)
    holder: dict = {}

    def answer(prompt):
        holder["node"].pairing.decide(prompt.session_id, True)
        holder["node"].pairing.abort_all()
        holder["node"].pairing.abort_all()  # gọi nhiều lần không lỗi

    A = await node_factory("A", answer=answer)
    holder["node"] = A
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(A.pairing.pair_with([LOCAL], B.port), 10)
    await A.rec.wait_finished(1, timeout=5)
    assert [str(f.outcome) for f in A.rec.finished] in (["save_failed"], ["cancelled"])
    assert A.store.get(B.id) is None
    assert TrustStore.load(A.dir).get(B.id) is None


@integration
async def test_abort_all_rejects_new_requests(node_factory):
    A = await node_factory("A", accept_pairing=True)
    B = await node_factory("B")
    A.pairing.abort_all()
    result = await asyncio.wait_for(B.pairing.pair_with([LOCAL], A.port), 10)
    assert result.outcome == PairingOutcome.PEER_BUSY
    assert A.rec.prompts == []


@integration
async def test_auto_connect_false_never_connects(
    node_factory, trust_each_other, fake_mdns, app_helpers, count_open_control
):
    counter = count_open_control()
    trust_each_other("A", "B")
    A = await node_factory("A", mdns=MdnsOptions(advertise=False), auto_connect=False)
    B = await node_factory("B")
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    await asyncio.sleep(0.5)
    assert counter.count() == 0
    assert A.connected_devices() == frozenset()
    assert [v.device_id for v in A.devices()] == [B.id]


@integration
async def test_mdns_goodbye_does_not_close_live_connection(
    node_factory, trust_each_other, fake_mdns, app_helpers, count_open_control
):
    """Bất biến mục 3.11: goodbye / Updated / địa chỉ giả không đóng kết nối đang sống."""
    counter = count_open_control()
    A, B = await pair_up(node_factory, trust_each_other)
    m = fake_mdns.of(A)
    m.emit(DeviceAdded(app_helpers.dev(B)))
    await wait_until(lambda: B.id in A.connected_devices(), 2)
    n = counter.count()
    conn = A.control_connection(B.id)
    for i in range(20):
        m.emit(DeviceRemoved(app_helpers.dev(B)))
        m.emit(DeviceAdded(app_helpers.dev(B, addresses=(f"192.0.2.{i + 1}",))))
    await asyncio.sleep(0.5)
    assert A.control_connection(B.id) is conn
    assert counter.count() == n
    assert A.rec.events_of(DeviceDisconnected) == []


# =================================================================================================
# SC1–SC5: scan_devices
# =================================================================================================


@pytest.fixture
def scan_dir(tmp_path):
    """X có danh tính; kho có B (thấy) và D (không thấy); C chưa ghép đôi (thấy)."""
    d = tmp_path / "X"
    ident = load_or_create_identity(d)
    ids = {label: load_or_create_identity(tmp_path / f"peer-{label}") for label in "BCD"}
    store = TrustStore.load(d)
    store.add(peer_cert_from_pem(ids["B"].cert_pem()), "B")
    store.add(peer_cert_from_pem(ids["D"].cert_pem()), "D")
    return d, ident, ids


async def test_sc1_scan_devices(scan_dir, fake_mdns, app_helpers):
    d, ident, ids = scan_dir
    fake_mdns.seed = {
        ids["B"].device_id: make_dev(ids["B"], 47474, name="B-txt", addresses=("192.0.2.10",)),
        ids["C"].device_id: make_dev(ids["C"], 50000, name="C", addresses=("192.0.2.20",)),
        ident.device_id: make_dev(ident, 47474, name="chính-nó", addresses=("192.0.2.1",)),
    }
    result = await scan_devices(d, timeout=0.3)
    assert isinstance(result, ScanResult)
    assert result.device_id == ident.device_id
    assert result.name == default_device_name(ident)
    assert result.port == 47474
    views = {v.device_id: v for v in result.devices}
    assert set(views) == {ids["B"].device_id, ids["C"].device_id, ids["D"].device_id}
    b, c, dd = views[ids["B"].device_id], views[ids["C"].device_id], views[ids["D"].device_id]
    assert (b.name, b.paired, b.online, b.port, b.addresses) == ("B", True, True, 47474, ("192.0.2.10",))
    assert (c.name, c.paired, c.online, c.port) == ("C", False, True, 50000)
    assert (dd.name, dd.paired, dd.online, dd.port, dd.addresses, dd.os) == ("D", True, False, None, (), None)
    assert all(v.connected is False for v in result.devices)
    assert all(isinstance(v, DeviceView) for v in result.devices)
    [m] = fake_mdns.instances
    assert "advertise" not in m.calls
    assert m.calls.index("set_priority") < m.calls.index("start_browsing")
    assert m.priorities[-1] == frozenset({ids["B"].device_id, ids["D"].device_id})
    assert m.closed
    assert not (d / "node.lock").exists(), "list không được đụng node.lock"
    assert app_helpers.lock_is_free(d)


async def test_sc1_scan_uses_saved_port(scan_dir, fake_mdns):
    d, _ident, _ids = scan_dir
    Settings(port=47480).save(d)
    result = await scan_devices(d, timeout=0.1)
    assert result.port == 47480


async def test_sc1_scan_ignores_advertise_option(scan_dir, fake_mdns):
    d, _ident, _ids = scan_dir
    await scan_devices(d, timeout=0.1, mdns=MdnsOptions(interfaces=(LOCAL,)))
    m = fake_mdns.last
    assert "advertise" not in m.calls
    assert m.kwargs["interfaces"] == (LOCAL,)


async def test_sc1_scan_creates_identity_when_missing(tmp_path, fake_mdns):
    d = tmp_path / "moi"
    result = await scan_devices(d, timeout=0.1)
    assert (d / "device.crt").exists()
    assert result.device_id == load_or_create_identity(d).device_id
    assert result.devices == ()


@pytest.mark.parametrize("timeout", [0, -1, math.nan, math.inf, 61, 60.0001, True, "3"])
async def test_sc2_bad_timeout(scan_dir, fake_mdns, timeout):
    d, _ident, _ids = scan_dir
    with pytest.raises((ValueError, TypeError)):
        await scan_devices(d, timeout=timeout)
    assert fake_mdns.instances == []


async def test_sc2_max_timeout_is_allowed_and_cancel_closes_mdns(scan_dir, fake_mdns):
    """`timeout = MAX_SCAN_TIMEOUT` hợp lệ; quét bị hủy giữa chừng vẫn đóng mDNS (mục 3.9 bước 6)."""
    d, _ident, _ids = scan_dir
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(scan_devices(d, timeout=60), 0.3)
    assert fake_mdns.last.closed


async def test_sc3_start_failure_is_discovery_unavailable(scan_dir, fake_mdns):
    d, _ident, _ids = scan_dir
    fake_mdns.fail_start = OSError("giả lập")
    with pytest.raises(DiscoveryUnavailable) as exc:
        await scan_devices(d, timeout=0.1)
    assert isinstance(exc.value.__cause__, OSError)
    assert fake_mdns.last.closing


async def test_sc4_corrupt_store_and_config_dir_file(scan_dir, fake_mdns, tmp_path):
    d, _ident, _ids = scan_dir
    (d / "trusted.json").write_bytes(b"{rac")
    with pytest.raises(TrustStoreError):
        await scan_devices(d, timeout=0.1)
    assert (d / "trusted.json").read_bytes() == b"{rac"
    f = tmp_path / "la-file"
    f.write_bytes(b"x")
    with pytest.raises(ConfigDirError):
        await scan_devices(f, timeout=0.1)
    assert f.read_bytes() == b"x"
    assert fake_mdns.instances == []


class _AsyncioSpy:
    """Thay `asyncio` trong `teleport.app.node`: ghi lại mỗi lần `sleep` (lúc gọi, đang duyệt chưa)."""

    def __init__(self, fake_mdns) -> None:
        self.fake_mdns = fake_mdns
        self.sleeps: list[tuple[float, list[str]]] = []

    def __getattr__(self, name: str):
        return getattr(asyncio, name)

    async def sleep(self, delay, *args, **kwargs):
        self.sleeps.append((delay, list(self.fake_mdns.last.calls)))
        return await asyncio.sleep(delay, *args, **kwargs)


async def test_sc5_scan_duration(scan_dir, fake_mdns, monkeypatch):
    """Quét đúng `timeout` giây: chờ đúng một lần, đúng `timeout`, giữa `start_browsing` và `close`.

    Không chặn trên bằng đồng hồ thật: khi chạy cả bộ, máy bận (GC thế hệ 2, thiếu CPU) làm phần
    dựng/dọn quanh lần chờ đó dài thêm quá 1 s. Đồng hồ thật chỉ kiểm không xong sớm và không treo.
    """
    d, _ident, _ids = scan_dir
    for timeout in (0.3, 1.0):
        spy = _AsyncioSpy(fake_mdns)
        with monkeypatch.context() as m:
            m.setattr(node_mod, "asyncio", spy)
            t0 = time.monotonic()
            await asyncio.wait_for(scan_devices(d, timeout=timeout), timeout + 30)
            took = time.monotonic() - t0
        assert spy.sleeps == [(timeout, ["start", "set_priority", "start_browsing"])], spy.sleeps
        assert fake_mdns.last.calls[-1] == "close" and fake_mdns.last.closed
        assert took >= 0.8 * timeout, took
