"""Test `Node.retry_connect` ([Thử lại] của thanh chẩn đoán; specs/spec-f3b.md mục 2.9, 7.2, 12 H15).

R1–R10 chạy `Node` thật trên 127.0.0.1 với `mdns=None`: đích duy nhất của tracker là địa chỉ đã lưu
(`trusted.json`) hay địa chỉ người dùng đưa. Hằng thời gian rút ngắn bằng fixture `fast`
(`OPENING_TIMEOUT` 2 s); `links.SAVED_RETRY_BASE` vá qua module để lịch tự nối lại tới địa chỉ đã
lưu không chen vào (60 s) hay chạy nhanh (khi cần kiểm “lịch vẫn chạy”).

Mẫu dựng chung: chọn trước một cổng trống `P`, ghi `last_address = 127.0.0.1:P` vào `trusted.json` của A
rồi chạy A (`auto_connect=True` để tracker nạp địa chỉ đã lưu). Lần thử lúc khởi động tới `P` thất bại
(chưa ai nghe); chờ nó xong (`LinkTracker.attempting()`, truy vấn công khai), rồi mới dựng node đích trên
`P` hay để trống, và gọi `retry_connect`. Nhờ vậy kết quả trả về là của đúng lần thử do `retry_now` mở.

Viết theo đặc tả, không theo code.
"""

import asyncio
import time

import pytest
from support import wait_until

from teleport.app.node import ConnectFailed, DeviceConnected
from teleport.core import links
from teleport.core.links import ConnectResult
from teleport.core.messages import MessageError
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


def file_state(directory) -> tuple[bytes, int]:
    path = directory / "trusted.json"
    return path.read_bytes(), path.stat().st_mtime_ns


def attempting(node) -> dict[str, int]:
    return node.node._links.attempting()  # truy vấn công khai của LinkTracker; Node không có API riêng


async def connected(a, b, timeout: float = 5.0) -> None:
    await wait_until(lambda: b.id in a.connected_devices(), timeout, f"{a.label} chưa kết nối tới {b.label}")


async def idle(a, b, timeout: float = 6.0) -> None:
    """Chờ tới khi A không còn lần thử nào đang chạy tới B."""
    await wait_until(lambda: b.id not in attempting(a), timeout, f"{a.label} vẫn đang thử tới {b.label}")


async def with_saved_address(node_factory, trust_each_other, tmp_path, port: int, **a_kwargs):
    """A, B tin nhau; `trusted.json` của A lưu `127.0.0.1:port` cho B; chạy A (B chưa chạy).

    Trả `(A, b_id)`; lần thử lúc khởi động của A (nếu có) đã kết thúc khi hàm trả về.
    """
    _, id_b = trust_each_other("A", "B")
    TrustStore.load(tmp_path / "A").set_last_address(id_b.device_id, LOCAL, port)
    A = await node_factory("A", **a_kwargs)
    await wait_until(lambda: id_b.device_id not in attempting(A), 6, "lần thử lúc khởi động chưa xong")
    return A, id_b.device_id


async def two_nodes(node_factory, trust_each_other):
    """A và B tin nhau, cả hai `mdns=None`, chưa kết nối, không có địa chỉ đã lưu."""
    trust_each_other("A", "B")
    B = await node_factory("B")
    A = await node_factory("A")
    return A, B


# --- R1: đã kết nối -------------------------------------------------------------------------------


@integration
async def test_r1_connected_returns_ok_without_new_attempt(
    node_factory, trust_each_other, count_open_control
):
    A, B = await two_nodes(node_factory, trust_each_other)
    A.connect_hint(B.id, [LOCAL], B.port)
    await connected(A, B)
    counter = count_open_control()
    before = file_state(A.dir)
    t0 = time.monotonic()
    result = await asyncio.wait_for(A.retry_connect(B.id), 5)
    assert result == OK
    assert time.monotonic() - t0 < 1.0, "đã kết nối: trả OK ngay"
    await asyncio.sleep(0.3)
    assert counter.count(caller=A) == 0, "đã kết nối: không mở lần thử mới"
    assert B.id in A.connected_devices()
    assert file_state(A.dir) == before


# --- R2: không có đích ---------------------------------------------------------------------------


@integration
async def test_r2_no_target_returns_none_immediately(node_factory, trust_each_other, count_open_control):
    A, B = await two_nodes(node_factory, trust_each_other)  # mdns=None, không địa chỉ lưu, không hint
    counter = count_open_control()
    t0 = time.monotonic()
    result = await asyncio.wait_for(A.retry_connect(B.id), 5)
    assert result is None
    assert time.monotonic() - t0 < 1.0, "không có đích: trả None ngay, không chờ hết giờ"
    await asyncio.sleep(0.2)
    assert counter.count(caller=A) == 0
    assert A.rec.events_of(ConnectFailed) == []


# --- R3: địa chỉ đã lưu còn sống -----------------------------------------------------------------


@integration
async def test_r3_saved_address_alive_returns_ok_and_emits_device_connected(
    node_factory, trust_each_other, tmp_path, app_helpers, count_open_control, monkeypatch
):
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 60.0)  # lịch tự nối lại không chen vào
    port = app_helpers.free_port()
    A, b_id = await with_saved_address(node_factory, trust_each_other, tmp_path, port)
    B = await node_factory("B", port=port)
    assert B.id not in A.connected_devices()
    counter = count_open_control()
    t0 = time.monotonic()
    result = await asyncio.wait_for(A.retry_connect(b_id), 10)
    assert result == OK
    assert time.monotonic() - t0 < 4.0, "retry_now thử ngay, không chờ lịch SAVED_RETRY_BASE"
    assert B.id in A.connected_devices()
    ev = await A.rec.wait_event(DeviceConnected, b_id, timeout=3)
    assert ev.outgoing is True
    calls = counter.of(caller=A)
    assert len(calls) == 1 and (calls[0].addresses, calls[0].port) == ((LOCAL,), port)
    assert A.store.get(b_id).last_address == (LOCAL, port)


# --- R4: địa chỉ đã lưu không ai nghe ------------------------------------------------------------


@integration
async def test_r4_saved_address_closed_returns_unreachable_silently_and_schedule_continues(
    node_factory, trust_each_other, tmp_path, app_helpers, count_open_control, monkeypatch
):
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 1.0)
    port = app_helpers.free_port()
    A, b_id = await with_saved_address(node_factory, trust_each_other, tmp_path, port)
    before = file_state(A.dir)
    counter = count_open_control()
    result = await asyncio.wait_for(A.retry_connect(b_id), 10)
    assert result == UNREACHABLE
    assert counter.count(caller=A) >= 1, "retry_connect phải thử thật"
    assert all((c.addresses, c.port) == ((LOCAL,), port) for c in counter.of(caller=A))
    await asyncio.sleep(0.3)
    assert A.rec.events_of(ConnectFailed) == [], "lỗi tại địa chỉ đã lưu không phát ConnectFailed"
    assert file_state(A.dir) == before, "không lưu gì thêm"
    # Lịch `saved` vẫn chạy: B lên ở đúng cổng thì A tự nối, không cần thao tác nào nữa. Nếu
    # retry_connect đặt `hint` thì lỗi lần sau sẽ là `from_hint` (ConnectFailed) và lịch không chạy.
    B = await node_factory("B", port=port)
    ev = await A.rec.wait_event(DeviceConnected, b_id, timeout=10)
    assert ev.outgoing is True
    assert B.id in A.connected_devices()
    assert A.rec.events_of(ConnectFailed) == []


# --- R5: địa chỉ đã lưu là của node khác ---------------------------------------------------------


@integration
async def test_r5_saved_address_of_other_node_returns_identity_mismatch_silently(
    node_factory, trust_each_other, tmp_path, app_helpers, count_open_control, monkeypatch
):
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 60.0)
    port = app_helpers.free_port()
    A, b_id = await with_saved_address(node_factory, trust_each_other, tmp_path, port)
    await node_factory("C", port=port)  # node lạ nghe đúng địa chỉ đã lưu của B
    B = await node_factory("B")
    before = file_state(A.dir)
    counter = count_open_control()
    result = await asyncio.wait_for(A.retry_connect(b_id), 10)
    assert result == IDENTITY
    assert counter.count(caller=A) == 1
    await asyncio.sleep(0.3)
    # Lỗi chặn được báo bằng ConnectFailed; sai danh tính tại địa chỉ đã lưu thì không báo, không chặn.
    assert A.rec.events_of(ConnectFailed) == []
    assert file_state(A.dir) == before, "đúng IP sai thiết bị: trusted.json không đổi"
    assert b_id not in A.connected_devices()
    # Địa chỉ đúng vẫn kết nối được, vẫn không có ConnectFailed nào.
    assert await asyncio.wait_for(A.connect_address(b_id, LOCAL, B.port), 10) == OK
    assert A.rec.events_of(ConnectFailed) == []


# --- R6: bị chặn sau REJECTED --------------------------------------------------------------------


@integration
async def test_r6_after_rejected_retry_unblocks_and_really_tries_again(
    node_factory, trust_each_other, count_open_control, monkeypatch
):
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 0.3)
    A, B = await two_nodes(node_factory, trust_each_other)
    assert await asyncio.wait_for(A.connect_address(B.id, LOCAL, B.port), 10) == OK  # lưu địa chỉ của B
    await asyncio.wait_for(B.unpair(A.id), 5)
    ev = await A.rec.wait_event(ConnectFailed, B.id, timeout=8)
    assert ev.result == REJECTED, "B đã hủy ghép đôi: lần nối lại tới địa chỉ đã lưu bị từ chối"
    await idle(A, B)
    counter = count_open_control()
    await asyncio.sleep(1.0)
    assert counter.count(caller=A) == 0, "bị từ chối → chặn: không tự thử lại theo lịch"
    result = await asyncio.wait_for(A.retry_connect(B.id), 10)
    assert result == REJECTED
    assert counter.count(caller=A) == 1, "retry_connect gỡ chặn và thử lại thật đúng một lần"
    assert B.id not in A.connected_devices()


# --- R7: đang có lần thử sẵn ---------------------------------------------------------------------


@integration
async def test_r7_waits_for_attempt_already_running(
    node_factory, trust_each_other, tmp_path, app_helpers, count_open_control, monkeypatch, fast
):
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 60.0)
    with app_helpers.busy_port() as port:  # TCP nhận vào hàng chờ nhưng không bắt tay TLS: lần thử treo
        _, id_b = trust_each_other("A", "B")
        TrustStore.load(tmp_path / "A").set_last_address(id_b.device_id, LOCAL, port)
        A = await node_factory("A")  # lần thử lúc khởi động tới `port` treo tới OPENING_TIMEOUT
        b_id = id_b.device_id
        await wait_until(lambda: b_id in attempting(A), 3, "lần thử lúc khởi động chưa bắt đầu")
        await asyncio.sleep(0.8)  # lần đang chạy kết thúc ~1.2 s sau lời gọi; một lần mới sẽ cần đủ 2 s
        counter = count_open_control()
        t0 = time.monotonic()
        result = await asyncio.wait_for(A.retry_connect(b_id), 10)
        elapsed = time.monotonic() - t0
    assert result == UNREACHABLE, "kết quả của lần thử đang chạy"
    assert all(c.t >= t0 + 1.0 for c in counter.of(caller=A)), "đang có lần thử: lời gọi không mở lần mới"
    assert elapsed < fast.OPENING_TIMEOUT - 0.4, f"phải trả khi lần thử sẵn có kết thúc, đo {elapsed:.2f} s"


@integration
async def test_r7_connection_through_other_path_returns_ok(
    node_factory, trust_each_other, tmp_path, app_helpers, monkeypatch
):
    """2.9: đang chờ mà kết nối được qua đường khác (máy kia gọi tới: `LinkUp`) → OK."""
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 60.0)
    with app_helpers.busy_port() as port:
        _, id_b = trust_each_other("A", "B")
        TrustStore.load(tmp_path / "A").set_last_address(id_b.device_id, LOCAL, port)
        B = await node_factory("B")
        A = await node_factory("A")
        await wait_until(lambda: B.id in attempting(A), 3, "lần thử lúc khởi động chưa bắt đầu")
        task = asyncio.create_task(A.retry_connect(B.id))
        await asyncio.sleep(0.05)
        assert not task.done()
        B.connect_hint(A.id, [LOCAL], A.port)
        result = await asyncio.wait_for(task, 10)
    assert result == OK
    assert B.id in A.connected_devices()


@integration
async def test_r7_wait_limit_is_read_at_call_time(
    node_factory, trust_each_other, tmp_path, app_helpers, monkeypatch
):
    """2.9: quá `2 * OPENING_TIMEOUT + 1` giây (đọc lúc gọi) → UNREACHABLE, kể cả khi lần thử còn treo."""
    from teleport.app import node as node_mod
    from teleport.net import connection as net_connection

    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 60.0)

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()  # lần mở kết nối không bao giờ xong (bị hủy khi node đóng)

    monkeypatch.setattr(node_mod, "open_control", hang)
    _, id_b = trust_each_other("A", "B")
    TrustStore.load(tmp_path / "A").set_last_address(id_b.device_id, LOCAL, app_helpers.free_port())
    A = await node_factory("A")
    b_id = id_b.device_id
    await wait_until(lambda: b_id in attempting(A), 3, "lần thử lúc khởi động chưa bắt đầu")
    monkeypatch.setattr(net_connection, "OPENING_TIMEOUT", 0.2)  # hạn chờ = 1.4 s
    t0 = time.monotonic()
    result = await asyncio.wait_for(A.retry_connect(b_id), 6)
    elapsed = time.monotonic() - t0
    assert result == UNREACHABLE
    assert 1.2 <= elapsed < 1.8, f"hạn chờ phải đọc lúc gọi (1.4 s), đo được {elapsed:.2f} s"
    await asyncio.wait_for(A.close(), 20)


# --- R8: thôi tin cậy, đóng, hủy khi đang chờ ----------------------------------------------------


async def _waiting_retry(node_factory, trust_each_other, tmp_path, port):
    """A có địa chỉ đã lưu của B là `port` (đang treo); trả `(A, b_id, task retry_connect đang chờ)`."""
    _, id_b = trust_each_other("A", "B")
    TrustStore.load(tmp_path / "A").set_last_address(id_b.device_id, LOCAL, port)
    A = await node_factory("A")
    b_id = id_b.device_id
    await wait_until(lambda: b_id in attempting(A), 3, "lần thử lúc khởi động chưa bắt đầu")
    task = asyncio.create_task(A.retry_connect(b_id))
    await asyncio.sleep(0.05)
    assert not task.done(), "retry_connect phải đang chờ lần thử treo"
    return A, b_id, task


@integration
async def test_r8_unpair_while_waiting_raises_key_error(
    node_factory, trust_each_other, tmp_path, app_helpers, monkeypatch
):
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 60.0)
    with app_helpers.busy_port() as port:
        A, b_id, task = await _waiting_retry(node_factory, trust_each_other, tmp_path, port)
        await asyncio.wait_for(A.unpair(b_id), 5)
        with pytest.raises(KeyError):
            await asyncio.wait_for(task, 1)


@integration
async def test_r8_close_while_waiting_raises_runtime_error(
    node_factory, trust_each_other, tmp_path, app_helpers, monkeypatch
):
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 60.0)
    with app_helpers.busy_port() as port:
        A, _, task = await _waiting_retry(node_factory, trust_each_other, tmp_path, port)
        await asyncio.wait_for(A.close(), 20)
        assert task.done(), "close() xong thì lời gọi đang chờ đã kết thúc"
        with pytest.raises(RuntimeError):
            await task
    # `no_leaks` kiểm không còn task nào sau test.


@integration
async def test_r8_cancel_only_stops_waiting(
    node_factory, trust_each_other, tmp_path, app_helpers, count_open_control, monkeypatch
):
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 60.0)
    with app_helpers.busy_port() as port:
        A, b_id, task = await _waiting_retry(node_factory, trust_each_other, tmp_path, port)
        running = attempting(A)[b_id]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert attempting(A).get(b_id) == running, "bị hủy: lần thử vẫn chạy"
        await wait_until(lambda: b_id not in attempting(A), 8, "lần thử treo chưa kết thúc")
    # Waiter đã gỡ: node vẫn dùng được, lời gọi mới thử lại thật và nhận kết quả của nó.
    counter = count_open_control()
    assert await asyncio.wait_for(A.retry_connect(b_id), 10) == UNREACHABLE
    assert counter.count(caller=A) == 1
    assert A.rec.events_of(ConnectFailed) == []


# --- R9: lỗi tham số -----------------------------------------------------------------------------


@integration
async def test_r9_argument_errors(node_factory, trust_each_other, app_helpers):
    A, B = await two_nodes(node_factory, trust_each_other)
    with pytest.raises(KeyError):
        await A.retry_connect(app_helpers.new_device_id())
    with pytest.raises(ValueError) as info:
        await A.retry_connect(A.id)
    assert not isinstance(info.value, MessageError)
    for bad in ["abc", B.id.upper(), B.id + "0", ""]:
        with pytest.raises(MessageError):
            await A.retry_connect(bad)
    # Đã kết nối vẫn kiểm đủ tham số trước khi trả OK.
    A.connect_hint(B.id, [LOCAL], B.port)
    await connected(A, B)
    with pytest.raises(KeyError):
        await A.retry_connect(app_helpers.new_device_id())
    with pytest.raises(ValueError):
        await A.retry_connect(A.id)
    with pytest.raises(MessageError):
        await A.retry_connect("abc")


@integration
async def test_r9_before_start_raises_runtime_error(node_factory):
    A = await node_factory("A", start=False)
    with pytest.raises(RuntimeError):
        await A.retry_connect("b" * 32)


@integration
async def test_r9_after_close_raises_runtime_error(node_factory, trust_each_other):
    A, B = await two_nodes(node_factory, trust_each_other)
    await asyncio.wait_for(A.close(), 20)
    with pytest.raises(RuntimeError):
        await A.retry_connect(B.id)


# --- R10: hai lời gọi đồng thời ------------------------------------------------------------------


@integration
async def test_r10_concurrent_calls_get_same_ok(
    node_factory, trust_each_other, tmp_path, app_helpers, count_open_control, monkeypatch
):
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 60.0)
    port = app_helpers.free_port()
    A, b_id = await with_saved_address(node_factory, trust_each_other, tmp_path, port)
    await node_factory("B", port=port)
    counter = count_open_control()
    r1, r2 = await asyncio.wait_for(asyncio.gather(A.retry_connect(b_id), A.retry_connect(b_id)), 10)
    assert (r1, r2) == (OK, OK)
    assert counter.count(caller=A) == 1, "lời gọi thứ hai chờ đúng lần thử của lời gọi đầu"
    assert b_id in A.connected_devices()


@integration
async def test_r10_concurrent_calls_get_same_failure(
    node_factory, trust_each_other, tmp_path, app_helpers, count_open_control, monkeypatch
):
    monkeypatch.setattr(links, "SAVED_RETRY_BASE", 60.0)
    port = app_helpers.free_port()
    A, b_id = await with_saved_address(node_factory, trust_each_other, tmp_path, port)
    r1, r2 = await asyncio.wait_for(asyncio.gather(A.retry_connect(b_id), A.retry_connect(b_id)), 10)
    assert (r1, r2) == (UNREACHABLE, UNREACHABLE)
