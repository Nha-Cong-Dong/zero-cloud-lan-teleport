"""Test F3a ở tầng app: nhớ địa chỉ máy đã ghép đôi (specs/spec-f3a.md mục 2.4–2.7, 6.3; mục 10 thắng).

N1–N15 chạy hai/ba `Node` thật trên 127.0.0.1 với `mdns=None` (mạng “chặn mDNS”): kết nối chỉ có được qua
`connect_hint` / `connect_address` hoặc qua địa chỉ đã lưu. Hằng thời gian rút ngắn bằng fixture `fast`
(`OPENING_TIMEOUT` 2 s…); hằng của F3a (`ADDRESS_SAVE_MIN_INTERVAL`, `SAVED_*`) vá qua module khi cần.
`tcp_proxy` cho một địa chỉ thứ hai (cổng khác) tới cùng node B và cắt kết nối bằng RST (`rst()`).

Viết theo đặc tả, không theo code. “Không ghi lại” được kiểm bằng byte và `st_mtime_ns` của `trusted.json`.
"""

import asyncio
import dataclasses
import json
import logging
import time

import pytest
from support import wait_until

from teleport.app import node as node_mod
from teleport.app.devices import DeviceView
from teleport.app.node import ConnectFailed, DeviceConnected, unpair_offline
from teleport.core import links
from teleport.core.links import ConnectResult
from teleport.core.messages import MessageError
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore

pytestmark = [
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
integration = pytest.mark.integration

LOCAL = "127.0.0.1"
OK = ConnectResult.OK
UNREACHABLE = ConnectResult.UNREACHABLE
IDENTITY = ConnectResult.IDENTITY_MISMATCH
REJECTED = ConnectResult.REJECTED


# --- Hàm phụ trợ ---------------------------------------------------------------------------------


def stored_address(directory, device_id: str) -> tuple[str, int] | None:
    """`last_address` của `device_id` đọc thẳng từ `trusted.json` (độc lập với `TrustStore`)."""
    data = json.loads((directory / "trusted.json").read_text(encoding="utf-8"))
    for e in data["devices"]:
        if e["device_id"] == device_id:
            value = e.get("last_address")
            return None if value is None else (value["ip"], value["port"])
    raise AssertionError(f"trusted.json không có mục {device_id[:8]}")


def stored_ids(directory) -> set[str]:
    data = json.loads((directory / "trusted.json").read_text(encoding="utf-8"))
    return {e["device_id"] for e in data["devices"]}


def file_state(directory) -> tuple[bytes, int]:
    path = directory / "trusted.json"
    return path.read_bytes(), path.stat().st_mtime_ns


def view_of(node, device_id: str) -> DeviceView:
    return next(v for v in node.devices() if v.device_id == device_id)


async def connected(a, b, timeout: float = 5.0) -> None:
    await wait_until(lambda: b.id in a.connected_devices(), timeout, f"{a.label} chưa kết nối tới {b.label}")


async def disconnected(a, b, timeout: float = 5.0) -> None:
    await wait_until(lambda: b.id not in a.connected_devices(), timeout, f"{a.label} vẫn kết nối {b.label}")


async def saved(a, b, address: tuple[str, int], timeout: float = 5.0) -> None:
    await wait_until(
        lambda: stored_address(a.dir, b.id) == address,
        timeout,
        f"{a.label} chưa lưu địa chỉ {address} của {b.label}",
    )


async def two_nodes(node_factory, trust_each_other, **a_kwargs):
    """A và B tin nhau (ghi `trusted.json` trước khi start), cả hai `mdns=None`, chưa kết nối."""
    trust_each_other("A", "B")
    B = await node_factory("B")
    A = await node_factory("A", **a_kwargs)
    return A, B


async def restart(node_factory, A, **kwargs):
    """Đóng A rồi chạy lại một node mới trên cùng thư mục cấu hình."""
    await asyncio.wait_for(A.close(), 20)
    return await node_factory("A", **kwargs)


def spy_set_last_address(monkeypatch) -> list[tuple[float, str, str, int]]:
    """Ghi lại mọi lời gọi `TrustStore.set_last_address` (thời điểm monotonic, device_id, ip, port)."""
    calls: list[tuple[float, str, str, int]] = []
    real = TrustStore.set_last_address

    def spy(self, device_id, ip, port):
        calls.append((time.monotonic(), device_id, ip, port))
        return real(self, device_id, ip, port)

    monkeypatch.setattr(TrustStore, "set_last_address", spy)
    return calls


# --- Hằng, kiểu (không socket) ----------------------------------------------------------------------


def test_address_save_min_interval_constant():
    assert node_mod.ADDRESS_SAVE_MIN_INTERVAL == 30.0


def test_device_view_and_connect_failed_new_fields():
    view_fields = dataclasses.fields(DeviceView)
    assert view_fields[-1].name == "last_address" and view_fields[-1].default is None
    failed_fields = {f.name: f for f in dataclasses.fields(ConnectFailed)}
    assert failed_fields["from_hint"].default is False


# --- N1, N2: chỉ kết nối ra mới lưu -------------------------------------------------------------------


@integration
async def test_n1_outgoing_connection_saves_address_and_view_has_it_at_connected(
    node_factory, trust_each_other
):
    A, B = await two_nodes(node_factory, trust_each_other)
    A.connect_hint(B.id, [LOCAL], B.port)
    ev = await A.rec.wait_event(DeviceConnected, B.id)
    assert ev.outgoing is True
    assert ev.device.last_address == (LOCAL, B.port), "DeviceConnected đã thấy địa chỉ mới (ghi ngay lần đầu)"
    assert stored_address(A.dir, B.id) == (LOCAL, B.port)
    assert A.store.get(B.id).last_address == (LOCAL, B.port)
    assert view_of(A, B.id).last_address == (LOCAL, B.port)


@integration
async def test_n2_incoming_connection_does_not_save_address(node_factory, trust_each_other):
    A, B = await two_nodes(node_factory, trust_each_other)
    A.connect_hint(B.id, [LOCAL], B.port)
    ev = await B.rec.wait_event(DeviceConnected, A.id)
    assert ev.outgoing is False
    await asyncio.sleep(0.3)
    assert stored_address(B.dir, A.id) is None
    assert B.store.get(A.id).last_address is None
    assert view_of(B, A.id).last_address is None


# --- N3, N10: khởi động lại → tự nối lại, không ghi lại ----------------------------------------------


@integration
async def test_n3_restarted_node_reconnects_to_saved_address_without_hint(
    node_factory, trust_each_other, count_open_control
):
    A, B = await two_nodes(node_factory, trust_each_other)
    A.connect_hint(B.id, [LOCAL], B.port)
    await connected(A, B)
    await saved(A, B, (LOCAL, B.port))
    counter = count_open_control()
    A2 = await restart(node_factory, A)
    ev = await A2.rec.wait_event(DeviceConnected, B.id, timeout=5)
    assert ev.outgoing is True, "A tự mở kết nối ra tới địa chỉ đã lưu"
    first = counter.of(caller=A2)[0]
    assert (first.addresses, first.port) == ((LOCAL,), B.port)


@integration
async def test_n10_reconnect_to_same_address_does_not_rewrite_trusted_json(
    node_factory, trust_each_other, monkeypatch
):
    A, B = await two_nodes(node_factory, trust_each_other)
    A.connect_hint(B.id, [LOCAL], B.port)
    await connected(A, B)
    await saved(A, B, (LOCAL, B.port))
    before = file_state(A.dir)
    calls = spy_set_last_address(monkeypatch)
    A2 = await restart(node_factory, A)
    await A2.rec.wait_event(DeviceConnected, B.id, timeout=5)
    await asyncio.sleep(0.3)
    assert file_state(A2.dir) == before, "cùng địa chỉ: không ghi lại trusted.json"
    assert calls == []


# --- N4: auto_connect ----------------------------------------------------------------------------------


@pytest.mark.slow
@integration
async def test_n4_auto_connect_false_does_not_try_saved_address(
    node_factory, trust_each_other, count_open_control
):
    A, B = await two_nodes(node_factory, trust_each_other)
    A.connect_hint(B.id, [LOCAL], B.port)
    await connected(A, B)
    await saved(A, B, (LOCAL, B.port))
    counter = count_open_control()
    A2 = await restart(node_factory, A, auto_connect=False)
    await asyncio.sleep(1.0)
    assert counter.count(caller=A2) == 0
    assert B.id not in A2.connected_devices()


@integration
async def test_n4_auto_connect_false_still_learns_address(node_factory, trust_each_other):
    A, B = await two_nodes(node_factory, trust_each_other, auto_connect=False)
    A.connect_hint(B.id, [LOCAL], B.port)
    await connected(A, B)
    await saved(A, B, (LOCAL, B.port))


@pytest.mark.slow
@integration
async def test_n4_auto_connect_to_only_tries_saved_address_of_that_device(
    node_factory, trust_each_other, count_open_control
):
    trust_each_other("A", "B")
    trust_each_other("A", "C", name_b="C")
    B = await node_factory("B")
    C = await node_factory("C")
    A = await node_factory("A")
    A.connect_hint(B.id, [LOCAL], B.port)
    A.connect_hint(C.id, [LOCAL], C.port)
    await connected(A, B)
    await connected(A, C)
    await saved(A, B, (LOCAL, B.port))
    await saved(A, C, (LOCAL, C.port))
    counter = count_open_control()
    A2 = await restart(node_factory, A, auto_connect_to=B.id)
    await connected(A2, B)
    await asyncio.sleep(0.8)
    assert counter.count(to=C, caller=A2) == 0, "auto_connect_to=B: không thử địa chỉ đã lưu của C"
    assert C.id not in A2.connected_devices()


# --- N5: connect_address ---------------------------------------------------------------------------


@integration
async def test_n5_connect_address_to_peer_returns_ok_and_saves(node_factory, trust_each_other):
    A, B = await two_nodes(node_factory, trust_each_other)
    result = await asyncio.wait_for(A.connect_address(B.id, LOCAL, B.port), 10)
    assert result == OK
    assert B.id in A.connected_devices()
    assert stored_address(A.dir, B.id) == (LOCAL, B.port)


@pytest.mark.slow
@integration
async def test_n5_connect_address_to_closed_port_returns_unreachable(
    node_factory, trust_each_other, app_helpers
):
    A, B = await two_nodes(node_factory, trust_each_other)
    before = file_state(A.dir)
    result = await asyncio.wait_for(A.connect_address(B.id, LOCAL, app_helpers.free_port()), 10)
    assert result == UNREACHABLE
    assert B.id not in A.connected_devices()
    assert file_state(A.dir) == before


@integration
async def test_n5_connect_address_to_other_node_returns_identity_mismatch_and_writes_nothing(
    node_factory, trust_each_other
):
    A, B = await two_nodes(node_factory, trust_each_other)
    C = await node_factory("C")
    before = file_state(A.dir)
    result = await asyncio.wait_for(A.connect_address(B.id, LOCAL, C.port), 10)
    assert result == IDENTITY
    assert file_state(A.dir) == before, "đúng IP sai thiết bị: trusted.json không đổi"
    assert B.id not in A.connected_devices()


@integration
async def test_n5_connect_address_when_connected_returns_ok_without_network(
    node_factory, trust_each_other, count_open_control, app_helpers
):
    A, B = await two_nodes(node_factory, trust_each_other)
    A.connect_hint(B.id, [LOCAL], B.port)
    await connected(A, B)
    counter = count_open_control()
    before = file_state(A.dir)
    t0 = time.monotonic()
    result = await asyncio.wait_for(A.connect_address(B.id, LOCAL, app_helpers.free_port()), 5)
    assert result == OK
    assert time.monotonic() - t0 < 1.0, "đã kết nối: trả OK ngay"
    await asyncio.sleep(0.3)
    assert counter.count(caller=A) == 0, "đã kết nối: không mở kết nối mới"
    assert file_state(A.dir) == before, "đã kết nối: không lưu địa chỉ"


@integration
async def test_n5_connect_address_argument_errors(node_factory, trust_each_other, app_helpers):
    A, B = await two_nodes(node_factory, trust_each_other)
    stranger = app_helpers.new_device_id()

    async def check_errors() -> None:
        with pytest.raises(KeyError):
            await A.connect_address(stranger, LOCAL, B.port)
        with pytest.raises(ValueError):
            await A.connect_address(A.id, LOCAL, A.port)
        with pytest.raises(MessageError):
            await A.connect_address("abc", LOCAL, B.port)
        for ip, port in [
            ("::1", B.port),
            ("0.0.0.0", B.port),
            ("224.0.0.251", B.port),
            ("127.000.0.1", B.port),
            (" 127.0.0.1", B.port),
            (LOCAL, 0),
            (LOCAL, 65536),
            (LOCAL, True),
        ]:
            with pytest.raises(ValueError):
                await A.connect_address(B.id, ip, port)

    await check_errors()
    A.connect_hint(B.id, [LOCAL], B.port)
    await connected(A, B)
    await check_errors()  # 10.2: đã kết nối vẫn kiểm đủ tham số


@integration
async def test_n5_connect_address_before_start_raises_runtime_error(node_factory):
    A = await node_factory("A", start=False)
    with pytest.raises(RuntimeError):
        await A.connect_address("b" * 32, LOCAL, 47474)


@integration
async def test_n5_concurrent_connect_address_calls_get_same_result(
    node_factory, trust_each_other, app_helpers
):
    """B1: mỗi lời gọi một future riêng; mọi waiter của cùng thiết bị nhận cùng kết quả."""
    A, B = await two_nodes(node_factory, trust_each_other)
    r1, r2 = await asyncio.wait_for(
        asyncio.gather(A.connect_address(B.id, LOCAL, B.port), A.connect_address(B.id, LOCAL, B.port)), 10
    )
    assert (r1, r2) == (OK, OK)
    assert B.id in A.connected_devices()


@integration
async def test_n5_cancelled_connect_address_lets_attempt_finish(node_factory, trust_each_other):
    A, B = await two_nodes(node_factory, trust_each_other)
    task = asyncio.create_task(A.connect_address(B.id, LOCAL, B.port))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await connected(A, B)  # bị hủy chỉ gỡ waiter, lần thử vẫn chạy


@integration
async def test_n5_connect_address_default_port_uses_saved_port(node_factory, trust_each_other, tmp_path):
    """B8: `port=None` → cổng trong `last_address` trước tiên."""
    trust_each_other("A", "B")
    B = await node_factory("B")
    TrustStore.load(tmp_path / "A").set_last_address(B.id, LOCAL, B.port)
    A = await node_factory("A", auto_connect=False)  # không tự thử: chỉ connect_address nối
    result = await asyncio.wait_for(A.connect_address(B.id, LOCAL), 10)
    assert result == OK
    assert B.id in A.connected_devices()


# --- N6: hủy ghép đôi ---------------------------------------------------------------------------------


@pytest.mark.slow
@integration
async def test_n6_unpair_removes_address_and_stops_attempts(
    node_factory, trust_each_other, count_open_control, monkeypatch
):
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 0.3)
    A, B = await two_nodes(node_factory, trust_each_other)
    A.connect_hint(B.id, [LOCAL], B.port)
    await connected(A, B)
    await saved(A, B, (LOCAL, B.port))
    counter = count_open_control()
    await asyncio.wait_for(A.unpair(B.id), 5)
    assert A.store.get(B.id) is None
    assert B.id not in stored_ids(A.dir)
    await asyncio.sleep(1.2)
    assert counter.count(caller=A) == 0, "đã hủy ghép đôi: không còn thử tới địa chỉ cũ"


@integration
async def test_n6_unpair_while_connect_address_waits_raises_key_error(
    node_factory, trust_each_other, count_open_control, app_helpers
):
    A, B = await two_nodes(node_factory, trust_each_other)
    counter = count_open_control()
    with app_helpers.busy_port() as port:  # TCP nhận vào hàng chờ nhưng không bắt tay TLS: lần thử treo
        task = asyncio.create_task(A.connect_address(B.id, LOCAL, port))
        await wait_until(lambda: counter.count(caller=A) >= 1, 3, "lần thử chưa bắt đầu")
        await asyncio.wait_for(A.unpair(B.id), 5)
        with pytest.raises(KeyError):
            await asyncio.wait_for(task, 3)


@pytest.mark.slow
@integration
async def test_n6_unpair_offline_removes_saved_address(node_factory, trust_each_other, count_open_control):
    A, B = await two_nodes(node_factory, trust_each_other)
    A.connect_hint(B.id, [LOCAL], B.port)
    await connected(A, B)
    await saved(A, B, (LOCAL, B.port))
    await asyncio.wait_for(A.close(), 20)
    removed = unpair_offline(A.dir, B.id)
    assert removed.device_id == B.id
    assert B.id not in stored_ids(A.dir)
    counter = count_open_control()
    A2 = await node_factory("A")
    await asyncio.sleep(0.8)
    assert counter.count(caller=A2) == 0


# --- N7: trần ghi ---------------------------------------------------------------------------------------


@pytest.mark.slow
@integration
async def test_n7_address_writes_are_capped_per_device(
    node_factory, trust_each_other, tcp_proxy, monkeypatch
):
    cap = 2.0
    monkeypatch.setattr(node_mod, "ADDRESS_SAVE_MIN_INTERVAL", cap)
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 60.0)  # không để lịch tự nối lại chen vào
    A, B = await two_nodes(node_factory, trust_each_other)
    p1, p2 = tcp_proxy(B.port), tcp_proxy(B.port)
    calls = spy_set_last_address(monkeypatch)
    assert await asyncio.wait_for(A.connect_address(B.id, LOCAL, p1.port), 10) == OK
    t1 = time.monotonic()
    assert stored_address(A.dir, B.id) == (LOCAL, p1.port), "lần đầu: ghi ngay"
    p1.rst()
    await disconnected(A, B)
    assert await asyncio.wait_for(A.connect_address(B.id, LOCAL, p2.port), 10) == OK
    assert stored_address(A.dir, B.id) == (LOCAL, p1.port), "trong trần: chưa ghi địa chỉ thứ hai"
    await wait_until(
        lambda: stored_address(A.dir, B.id) == (LOCAL, p2.port), cap + 3, "địa chỉ chờ chưa được áp"
    )
    assert time.monotonic() - t1 >= cap - 0.3, "địa chỉ thứ hai ghi trước khi hết trần"
    assert [(ip, port) for _, _, ip, port in calls] == [(LOCAL, p1.port), (LOCAL, p2.port)]
    assert A.store.get(B.id).last_address == (LOCAL, p2.port)


@integration
async def test_n7_write_failure_only_warns_without_address(
    node_factory, trust_each_other, monkeypatch, caplog
):
    """2.4 bước 5 + B6: `TrustStoreError` khi ghi địa chỉ chỉ là WARNING (có `device_id[:8]`, không có IP
    hay cổng); kết nối vẫn chạy."""
    from teleport.trust.store import TrustStoreError

    def failing(self, device_id, ip, port):
        raise TrustStoreError("giả lập không ghi được trusted.json")

    monkeypatch.setattr(TrustStore, "set_last_address", failing)
    A, B = await two_nodes(node_factory, trust_each_other)
    with caplog.at_level(logging.DEBUG, logger="teleport.app.node"):
        A.connect_hint(B.id, [LOCAL], B.port)
        await connected(A, B)
        await asyncio.sleep(0.2)
    warnings = [r for r in caplog.records if r.name == "teleport.app.node" and r.levelno >= logging.WARNING]
    assert warnings, "lỗi ghi địa chỉ phải có WARNING"
    for r in warnings:
        text = r.getMessage()
        assert LOCAL not in text and str(B.port) not in text, text
    assert any(B.id[:8] in r.getMessage() for r in warnings)
    assert all(r.levelno == logging.WARNING for r in warnings), "chỉ WARNING (2.4 bước 5), không ERROR"
    assert stored_address(A.dir, B.id) is None
    assert B.id in A.connected_devices()


@pytest.mark.slow
@integration
async def test_n7_unpair_drops_pending_address_write(node_factory, trust_each_other, tcp_proxy, monkeypatch):
    """2.4/2.7: `_trust_changed` bỏ địa chỉ đang chờ hết trần của thiết bị thôi tin cậy."""
    cap = 1.5
    monkeypatch.setattr(node_mod, "ADDRESS_SAVE_MIN_INTERVAL", cap)
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 60.0)
    A, B = await two_nodes(node_factory, trust_each_other)
    p1, p2 = tcp_proxy(B.port), tcp_proxy(B.port)
    calls = spy_set_last_address(monkeypatch)
    assert await asyncio.wait_for(A.connect_address(B.id, LOCAL, p1.port), 10) == OK
    p1.rst()
    await disconnected(A, B)
    assert await asyncio.wait_for(A.connect_address(B.id, LOCAL, p2.port), 10) == OK
    await asyncio.wait_for(A.unpair(B.id), 5)
    await asyncio.sleep(cap + 0.7)
    assert B.id not in stored_ids(A.dir), "địa chỉ chờ không được ghi lại mục đã hủy ghép đôi"
    assert len(calls) == 1, calls


@pytest.mark.slow
@integration
async def test_n_global_saved_address_is_not_tried_at_startup(
    node_factory, trust_each_other, count_open_control, tmp_path, app_helpers
):
    """10.4: địa chỉ Internet công khai trong `trusted.json` vẫn nạp nhưng không tự thử khi khởi động.

    `count_open_control(rewrite_to=LOCAL)` đổi địa chỉ thật thành 127.0.0.1, để code sai cũng không gửi gói
    nào ra Internet."""
    trust_each_other("A", "B")
    TrustStore.load(tmp_path / "A").set_last_address(
        TrustStore.load(tmp_path / "A").devices()[0].device_id, "8.8.8.8", app_helpers.free_port()
    )
    counter = count_open_control(rewrite_to=LOCAL)
    A = await node_factory("A")
    assert A.store.devices()[0].last_address is not None, "địa chỉ global hợp lệ về dạng vẫn nạp"
    await asyncio.sleep(1.0)
    assert counter.count(caller=A) == 0


# --- N8: chế độ ẩn --------------------------------------------------------------------------------------


@integration
async def test_n8_hidden_nodes_reconnect_through_saved_address_after_restart(node_factory, trust_each_other):
    A, B = await two_nodes(node_factory, trust_each_other)
    A.set_visible(False)
    B.set_visible(False)
    A.connect_hint(B.id, [LOCAL], B.port)
    await connected(A, B)
    await saved(A, B, (LOCAL, B.port))
    A2 = await restart(node_factory, A)
    assert A2.visible is False
    ev = await A2.rec.wait_event(DeviceConnected, B.id, timeout=5)
    assert ev.outgoing is True


# --- N9: địa chỉ đã lưu trỏ tới node khác ---------------------------------------------------------------


@pytest.mark.slow
@integration
async def test_n9_saved_address_of_other_node_is_silent_and_not_blocking(
    node_factory, trust_each_other, count_open_control, tmp_path
):
    trust_each_other("A", "B")
    B = await node_factory("B")
    C = await node_factory("C")
    TrustStore.load(tmp_path / "A").set_last_address(B.id, LOCAL, C.port)
    before = file_state(tmp_path / "A")
    counter = count_open_control()
    A = await node_factory("A")

    def tries_to_c() -> int:
        return sum(1 for c in counter.of(caller=A) if c.port == C.port)

    await wait_until(lambda: tries_to_c() >= 1, 3, "A chưa thử địa chỉ đã lưu")
    await asyncio.sleep(1.5)
    assert A.rec.events_of(ConnectFailed) == [], "IDENTITY_MISMATCH tại địa chỉ đã lưu: không ConnectFailed"
    assert tries_to_c() == 1, "không thử lại địa chỉ đó trong phiên"
    assert file_state(A.dir) == before, "đĩa không bị sửa khi danh tính không khớp"
    result = await asyncio.wait_for(A.connect_address(B.id, LOCAL, B.port), 10)
    assert result == OK
    assert A.rec.events_of(ConnectFailed) == []


# --- N11: ghép đôi lại cùng danh tính -----------------------------------------------------------------


@pytest.mark.slow
@integration
async def test_n11_repairing_same_identity_keeps_saved_address(node_factory, trust_each_other):
    trust_each_other("A", "B")
    B = await node_factory("B", accept_pairing=True, answer=True)
    A = await node_factory("A", answer=True)
    A.connect_hint(B.id, [LOCAL], B.port)
    await connected(A, B)
    await saved(A, B, (LOCAL, B.port))
    result = await asyncio.wait_for(A.pairing.pair_with([LOCAL], B.port), 15)
    assert result.outcome == "paired"
    assert A.store.get(B.id).last_address == (LOCAL, B.port)
    assert stored_address(A.dir, B.id) == (LOCAL, B.port)


# --- N12: last_address hỏng ---------------------------------------------------------------------------


@pytest.mark.slow
@integration
async def test_n12_corrupt_last_address_is_dropped_and_node_still_works(
    node_factory, trust_each_other, count_open_control, tmp_path, caplog
):
    trust_each_other("A", "B")
    B = await node_factory("B")
    path = tmp_path / "A" / "trusted.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    bad_ip = "127.000.000.001"  # không viết chuẩn: nếu được “sửa” thành 127.0.0.1 thì sẽ tới đúng B
    data["devices"][0]["last_address"] = {"ip": bad_ip, "port": B.port}
    path.write_text(json.dumps(data), encoding="utf-8")
    counter = count_open_control()
    with caplog.at_level(logging.WARNING, logger="teleport.trust.store"):
        A = await node_factory("A", answer=True)
    messages = [r.getMessage() for r in caplog.records if r.name == "teleport.trust.store"]
    assert any("last_address" in m for m in messages), messages
    assert not any(bad_ip in m for m in messages)
    assert A.store.get(B.id).last_address is None
    await asyncio.sleep(1.0)
    assert counter.count(caller=A) == 0, "địa chỉ hỏng không được tự thử"
    A.connect_hint(B.id, [LOCAL], B.port)
    await connected(A, B)
    await saved(A, B, (LOCAL, B.port))  # lần ghi sau làm sạch trường hỏng
    C = await node_factory("C", accept_pairing=True, answer=True)
    result = await asyncio.wait_for(A.pairing.pair_with([LOCAL], C.port), 15)
    assert result.outcome == "paired", "ghép đôi vẫn chạy"


# --- N13: close() trong lúc chờ --------------------------------------------------------------------------


@integration
async def test_n13_close_while_connect_address_waits_raises_runtime_error(
    node_factory, trust_each_other, count_open_control, app_helpers
):
    A, B = await two_nodes(node_factory, trust_each_other)
    counter = count_open_control()
    with app_helpers.busy_port() as port:
        task = asyncio.create_task(A.connect_address(B.id, LOCAL, port))
        await wait_until(lambda: counter.count(caller=A) >= 1, 3, "lần thử chưa bắt đầu")
        await asyncio.wait_for(A.close(), 20)
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(task, 3)


# --- N14: ConnectFailed.from_hint -------------------------------------------------------------------------


@pytest.mark.slow
@integration
async def test_n14_connect_failed_from_user_address_has_from_hint(
    node_factory, trust_each_other, app_helpers
):
    A, B = await two_nodes(node_factory, trust_each_other)
    A.connect_hint(B.id, [LOCAL], app_helpers.free_port())
    ev = await A.rec.wait_event(ConnectFailed, B.id)
    assert (ev.result, ev.from_hint) == (UNREACHABLE, True)
    result = await asyncio.wait_for(A.connect_address(B.id, LOCAL, app_helpers.free_port()), 10)
    assert result == UNREACHABLE
    ev2 = await A.rec.wait_event(ConnectFailed, B.id, n=2)
    assert (ev2.result, ev2.from_hint) == (UNREACHABLE, True)


@integration
async def test_n14_connect_failed_of_saved_address_attempt_is_not_from_hint(node_factory, tmp_path):
    """B không tin A: lần thử tới địa chỉ đã lưu bị REJECTED → `ConnectFailed(REJECTED, from_hint=False)`
    (L4 ở tầng app)."""
    B = await node_factory("B")
    load_or_create_identity(tmp_path / "A")
    store = TrustStore.load(tmp_path / "A")
    store.add(peer_cert_from_pem(B.identity.cert_pem()), "B")
    store.set_last_address(B.id, LOCAL, B.port)
    A = await node_factory("A")
    ev = await A.rec.wait_event(ConnectFailed, B.id, timeout=5)
    assert (ev.result, ev.from_hint) == (REJECTED, False)


# --- N15: connect_address hỏng không che địa chỉ đã lưu (spec 10.2) ------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("wrong", ["unreachable", "identity"])
@integration
async def test_n15_failed_connect_address_does_not_hide_saved_address(
    wrong, node_factory, trust_each_other, tcp_proxy, count_open_control, app_helpers, monkeypatch
):
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 2.0)
    A, B = await two_nodes(node_factory, trust_each_other)
    if wrong == "identity":
        C = await node_factory("C")
        bad_port, expected = C.port, IDENTITY
    else:
        bad_port, expected = app_helpers.free_port(), UNREACHABLE
    proxy = tcp_proxy(B.port)
    assert await asyncio.wait_for(A.connect_address(B.id, LOCAL, proxy.port), 10) == OK
    assert stored_address(A.dir, B.id) == (LOCAL, proxy.port)
    counter = count_open_control()
    proxy.rst()
    await disconnected(A, B)
    assert await asyncio.wait_for(A.connect_address(B.id, LOCAL, bad_port), 10) == expected
    await connected(A, B, timeout=8)  # địa chỉ đã lưu vẫn được tự thử lại
    last = counter.of(caller=A)[-1]
    assert (last.addresses, last.port) == ((LOCAL,), proxy.port)
    assert stored_address(A.dir, B.id) == (LOCAL, proxy.port)


@integration
async def test_n15_connect_address_while_connected_does_not_replace_saved_target(
    node_factory, trust_each_other, tcp_proxy, count_open_control, app_helpers, monkeypatch
):
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 0.5)
    A, B = await two_nodes(node_factory, trust_each_other)
    proxy = tcp_proxy(B.port)
    assert await asyncio.wait_for(A.connect_address(B.id, LOCAL, proxy.port), 10) == OK
    bad_port = app_helpers.free_port()
    assert await asyncio.wait_for(A.connect_address(B.id, LOCAL, bad_port), 5) == OK
    counter = count_open_control()
    proxy.rst()
    await disconnected(A, B)
    await connected(A, B, timeout=6)  # hint không bị đổi thành cổng sai: nối lại tới địa chỉ đã lưu
    assert all(c.port != bad_port for c in counter.of(caller=A)), [c.port for c in counter.of(caller=A)]
    assert A.rec.events_of(ConnectFailed) == []
