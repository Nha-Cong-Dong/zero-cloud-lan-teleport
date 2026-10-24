"""Test `Node` với pool ưu tiên (`docs/protocol.md` mục 2.3) và client `CERT_REQUIRED` (mục 2.1, 8).

- Luồng sản phẩm chạy **mặc định thật** của `core.admission` (`real_admission_limits`): ghép đôi →
  tự kết nối → nối lại sau khi đứt, nhiều lần, không có lần nào bị từ chối trước TLS.
- Node mở kết nối điều khiển ra → IP đích được ưu tiên ở Listener của chính node đó; hủy ghép đôi
  thì mất.
- Máy giả (danh tính khác, ở địa chỉ mDNS của máy tin cậy, lại tin chứng chỉ của A): A báo
  `IDENTITY_MISMATCH`, máy giả không nhận được kết nối nào, A không tự thử lại theo lịch.

Hằng thời gian rút ngắn bằng `fast` (tests/app/conftest.py). Listener của node đọc qua
`node._listener` (không có API công khai; chỉ để chẩn đoán).
"""

import asyncio
import time

import pytest

from teleport.app.node import ConnectFailed, MdnsOptions
from teleport.app.pairing import PairingOutcome
from teleport.core.admission import Refusal
from teleport.core.discovery import DeviceAdded, DeviceUpdated
from teleport.core.links import ConnectResult
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

LOCAL = "127.0.0.1"


async def wait_until(predicate, timeout: float = 2.0, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.01)


def listener_of(node):
    return node.node._listener


def refusals(node) -> dict[str, int]:
    counts = dict(listener_of(node).refusals)
    return {str(r): counts.get(r, 0) for r in Refusal}


def no_refusals() -> dict[str, int]:
    return {str(r): 0 for r in Refusal}


def shared_link(a, b) -> tuple[bool, int] | None:
    """Kết nối điều khiển hiện tại mà **cả hai** node cùng giữ (hai đầu của một kết nối TCP).

    Trả (A là client?, cổng nguồn của client), hoặc None khi chưa có, hoặc mỗi bên còn giữ một kết
    nối khác nhau: lúc hai bên cùng gọi nhau, luật chống trùng (`docs/protocol.md` mục 6.1) chưa
    xử lý xong ở cả hai bên.
    """
    ca, cb = a.control_connection(b.id), b.control_connection(a.id)
    if ca is None or cb is None or ca.closed or cb.closed or ca.is_server == cb.is_server:
        return None
    client, server = (cb, ca) if ca.is_server else (ca, cb)
    port = client._writer.get_extra_info("sockname")[1]
    return (client is ca, port) if server.peer.port == port else None


@pytest.mark.real_admission_limits
async def test_pairing_then_auto_connect_then_reconnect_with_real_default_limits(
    node_factory, fake_mdns, app_helpers, count_open_control
):
    """Mặc định thật: ghép đôi (kết nối ẩn danh, tiêu ngân sách) → tự nối (hoàn token) → đứt và nối
    lại 3 lần. Không có lần nào bị từ chối trước TLS ở cả hai máy."""
    count_open_control()
    A = await node_factory("A", mdns=MdnsOptions(), accept_pairing=True, answer=True)
    B = await node_factory("B", mdns=MdnsOptions(), accept_pairing=True, answer=True)
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    fake_mdns.of(B).emit(DeviceAdded(app_helpers.dev(A)))
    result = await asyncio.wait_for(A.pairing.pair_with([LOCAL], B.port), 15)
    assert result.outcome == PairingOutcome.PAIRED
    await wait_until(lambda: shared_link(A, B) is not None, 3, "chưa tự nối sau ghép đôi")
    for round_no in range(3):
        # Chờ cả hai bên giữ cùng một kết nối rồi mới cắt: nếu cắt lúc luật chống trùng còn dở,
        # có thể cắt đúng kết nối sắp bị bỏ, kết nối còn lại vẫn sống và không có lần nối lại nào.
        old = shared_link(A, B)
        side = B if round_no % 2 else A
        other = A if side is B else B
        await side.control_connection(other.id).close()
        await wait_until(
            lambda old=old: shared_link(A, B) not in (None, old),
            5,
            f"lần {round_no + 1}: chưa nối lại sau khi đứt",
        )
    assert A.rec.events_of(ConnectFailed) == []
    assert B.rec.events_of(ConnectFailed) == []
    assert refusals(A) == no_refusals(), refusals(A)
    assert refusals(B) == no_refusals(), refusals(B)
    assert listener_of(A).is_priority(LOCAL) is True
    assert listener_of(B).is_priority(LOCAL) is True


async def test_outgoing_control_makes_target_ip_priority_and_unpair_drops_it(
    node_factory, trust_each_other, fake_mdns, app_helpers
):
    """Chỉ A thấy B qua mDNS → A tự mở kết nối ra; Listener của A ưu tiên IP của B (không có kết nối
    vào nào ở A). Hủy ghép đôi → mất ưu tiên ngay."""
    trust_each_other("A", "B")
    A = await node_factory("A", mdns=MdnsOptions())
    B = await node_factory("B")
    assert listener_of(A).is_priority(LOCAL) is False
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    await wait_until(lambda: A.connected_devices() == {B.id} and B.connected_devices() == {A.id}, 3)
    assert A.control_connection(B.id).is_server is False
    assert listener_of(A).is_priority(LOCAL) is True
    await A.unpair(B.id)
    assert listener_of(A).is_priority(LOCAL) is False


async def test_outgoing_priority_kept_after_connection_drops(
    node_factory, trust_each_other, fake_mdns, app_helpers
):
    trust_each_other("A", "B")
    A = await node_factory("A", mdns=MdnsOptions(), auto_connect=True)
    B = await node_factory("B")
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    await wait_until(lambda: A.connected_devices() == {B.id}, 3)
    await B.close()
    await wait_until(lambda: A.connected_devices() == frozenset(), 3, "A chưa thấy B đóng")
    assert listener_of(A).is_priority(LOCAL) is True, "đóng rồi vẫn ưu tiên trong PRIORITY_TTL"


async def test_impostor_trusting_our_cert_gets_no_connection_and_no_scheduled_retry(
    node_factory, fake_mdns, count_open_control, tmp_path, app_helpers
):
    """`docs/protocol.md` mục 8: C (danh tính khác) chạy ở địa chỉ mDNS báo cho B, và C **tin**
    chứng chỉ của A (nên phía server sẽ nhận A nếu A gửi chứng chỉ). A kiểm chứng chỉ server trong
    bắt tay → lỗi `IDENTITY_MISMATCH`; C không có kết nối nào từ A; A không tự thử lại theo lịch
    (mvp mục 8)."""
    counter = count_open_control()
    b_id = load_or_create_identity(tmp_path / "B")
    a_id = app_helpers.ensure_identity(tmp_path / "A")
    app_helpers.seed_trust(tmp_path / "A", b_id.cert_pem(), "B")
    app_helpers.ensure_identity(tmp_path / "C")
    TrustStore.load(tmp_path / "C").add(peer_cert_from_pem(a_id.cert_pem()), "A")
    A = await node_factory("A", mdns=MdnsOptions())
    C = await node_factory("C")
    m = fake_mdns.of(A)
    spoof = app_helpers.dev(b_id, port=C.port)
    m.emit(DeviceAdded(spoof))
    failed = await A.rec.wait_event(ConnectFailed, b_id.device_id, timeout=5)
    assert failed.result == ConnectResult.IDENTITY_MISMATCH
    n = counter.count(to=b_id)
    await asyncio.sleep(2.5)  # > RETRY_MAX (1 s) nhiều lần: không có lần thử theo lịch
    assert counter.count(to=b_id) == n, "sau IDENTITY_MISMATCH vẫn tự thử lại theo lịch"
    assert C.connected_devices() == frozenset()
    assert A.connected_devices() == frozenset()
    assert listener_of(A).is_priority(LOCAL) is False
    # Sự kiện mDNS mới vẫn được thử (theo lịch chặn), lỗi không báo lại lần nữa.
    m.emit(DeviceUpdated(app_helpers.dev(b_id, port=C.port, name="B2"), spoof))
    await asyncio.sleep(0.5)
    assert counter.count(to=b_id) <= n + 1
    assert len(A.rec.events_of(ConnectFailed, b_id.device_id)) == 1
    assert C.connected_devices() == frozenset()


async def test_node_passes_stored_cert_pem_to_open_control(
    node_factory, trust_each_other, fake_mdns, app_helpers, count_open_control
):
    counter = count_open_control()
    _, id_b = trust_each_other("A", "B")
    A = await node_factory("A", mdns=MdnsOptions())
    B = await node_factory("B")
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    await wait_until(lambda: A.connected_devices() == {B.id}, 3)
    [call] = counter.of(to=B)[:1]
    assert call.cert_pem == id_b.cert_pem()
