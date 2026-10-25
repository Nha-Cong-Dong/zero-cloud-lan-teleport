"""Test tích hợp `Listener` + `core.admission` (`docs/protocol.md` mục 2.3, gạch “Nhận kết nối mới”).

Viết từ đặc tả, không từ code: từ chối = `transport.abort()` trước `start_tls` (client không nhận
byte nào); bucket theo IP và bucket tổng; IP có kết nối điều khiển (vào, hay ra qua
`note_outgoing_control`) được ưu tiên, kể cả khi pool chung đã đóng; `update_trusted` bỏ thiết bị thì
IP về pool chung (kể cả khi dựng context lỗi); hoàn token cho bắt tay tin cậy; chỗ được trả trên mọi
đường ra. IP ưu tiên dùng kết nối không kết thúc bằng bắt tay tin cậy quá ngân sách lỗi thì bị hạ ưu tiên;
`trusted()` chỉ khi `is_trusted(fp)` còn True.

Gần như mọi test chạy trên 127.0.0.1 (macOS không bind được 127.0.0.2), nên “IP lạ” và “IP ưu tiên” là
cùng một IP ở hai thời điểm; riêng kịch bản hai IP (`priority_starve`) dùng 127.0.0.2 và tự bỏ qua
khi không bind được. Test giới hạn tốc độ gắn `real_admission_limits` (không nới) và tự đặt lại hằng
nào cần trên `teleport.core.admission`.
"""

import asyncio
import contextlib
import socket
import ssl
import time
from types import SimpleNamespace

import pytest

from teleport.core import admission
from teleport.core.admission import Refusal
from teleport.core.messages import PROTOCOL_VERSION, Hello, Ping
from teleport.net import connection, tls
from teleport.net.connection import Rejected, Unreachable, open_control, open_pairing
from teleport.trust.identity import load_or_create_identity

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
real_limits = pytest.mark.real_admission_limits

LOCAL = "127.0.0.1"


def hello_of(identity, name: str = "B") -> Hello:
    return Hello(
        protocol=PROTOCOL_VERSION, device_id=identity.device_id, name=name, app_version="0.0.0", os="windows"
    )


@pytest.fixture(scope="module")
def ids(tmp_path_factory):
    base = tmp_path_factory.mktemp("adm-ids")
    return SimpleNamespace(
        server=load_or_create_identity(base / "server"),
        b=load_or_create_identity(base / "b"),
        x=load_or_create_identity(base / "stranger"),
        anon=load_or_create_identity(base / "anon"),
    )


@pytest.fixture
def slow(fast, monkeypatch):
    """OPENING_TIMEOUT 5 s: kết nối bị đóng nhanh là bị từ chối, không phải hết giờ."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    return fast


def client_hello_bytes() -> bytes:
    """ClientHello TLS 1.3 thật (qua MemoryBIO) để gửi trên TCP trần."""
    ctx = tls.client_context()
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ctx.wrap_bio(incoming, outgoing, server_side=False)
    with pytest.raises(ssl.SSLWantReadError):
        obj.do_handshake()
    return outgoing.read()


async def read_some(peer, timeout: float = 2.0) -> bytes:
    """Byte đầu tiên server gửi (b"" nếu server đóng / reset trước khi gửi gì)."""
    try:
        return await asyncio.wait_for(peer.reader.read(65536), timeout)
    except (ConnectionError, ssl.SSLError):
        return b""


async def b_control(h, ids, identity=None):
    identity = identity if identity is not None else ids.b
    return await open_control([LOCAL], h.port, identity, hello_of(identity), h.identity.cert_pem())


async def wait_until(predicate, timeout: float = 2.0, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.01)


def refusals(h) -> dict[str, int]:
    counts = dict(h.listener.refusals)
    return {str(r): counts.get(r, 0) for r in Refusal}


def no_refusals() -> dict[str, int]:
    return {str(r): 0 for r in Refusal}


async def tls_attempt(port: int, ctx: ssl.SSLContext) -> bool:
    """Một lần bắt tay + đọc `hello`. True nếu nhận được `hello` của server."""
    from teleport.net.stream import read_message

    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(LOCAL, port, ssl=ctx), 3)
    except (OSError, ssl.SSLError, TimeoutError):
        return False
    try:
        msg = await asyncio.wait_for(read_message(reader, 16 * 1024), 2)
        return isinstance(msg, dict) and msg.get("type") == "hello"
    except Exception:
        return False
    finally:
        writer.transport.abort()


# --- Bucket theo IP và bucket tổng: từ chối trước TLS -------------------------------------------


@real_limits
async def test_connection_over_per_ip_burst_is_reset_before_tls(
    slow, monkeypatch, listener_factory, raw_client, ids
):
    """Burst theo IP là 2: kết nối thứ 3 ngay sau đó bị cắt trước TLS, không nhận byte nào (kể cả
    ServerHello), trong khi kết nối được nhận vẫn trả lời ClientHello."""
    # Không nạp lại trong lúc chạy: kết quả không phụ thuộc tốc độ máy.
    monkeypatch.setattr(admission, "HANDSHAKE_RATE_PER_IP", 0.001)
    h = await listener_factory(ids.server)
    hello = client_hello_bytes()
    p1 = await raw_client(h.port, use_tls=False)
    p1.send_bytes(hello)
    await p1.drain()
    assert await read_some(p1) != b"", "kết nối được nhận phải trả ServerHello: phép thử vô nghĩa"
    p2 = await raw_client(h.port, use_tls=False)
    await wait_until(lambda: h.listener.opening_count == 2, 2.0, "2 kết nối đang mở đầu")
    p3 = await raw_client(h.port, use_tls=False)
    p3.send_bytes(hello)
    await p3.drain()
    elapsed, data = await p3.expect_closed(2.0)
    assert data == b"", f"kết nối bị từ chối vẫn nhận {len(data)} byte"
    assert elapsed < 0.5
    assert refusals(h) == {**no_refusals(), str(Refusal.RATE_PER_IP): 1}
    assert await p2.stays_open(0.05)
    assert h.calls == 0


@real_limits
async def test_total_bucket_limits_one_ip_when_per_ip_is_relaxed(
    slow, monkeypatch, listener_factory, raw_client, ids
):
    for name, value in {
        "HANDSHAKE_RATE_PER_IP": 1000.0,
        "HANDSHAKE_BURST_PER_IP": 1000,
        "MAX_OPENING_PER_IP": 100,
        "HANDSHAKE_RATE_TOTAL": 0.001,  # gần như không nạp lại: kết quả không phụ thuộc tốc độ máy
    }.items():
        monkeypatch.setattr(admission, name, value)
    h = await listener_factory(ids.server)
    peers = [await raw_client(h.port, use_tls=False) for _ in range(4)]
    fifth = await raw_client(h.port, use_tls=False)
    _, data = await fifth.expect_closed(2.0)
    assert data == b""
    assert refusals(h) == {**no_refusals(), str(Refusal.RATE_TOTAL): 1}
    for p in peers:
        assert await p.stays_open(0.05)
    assert h.listener.opening_count == 4


@real_limits
async def test_trusted_client_handshakes_are_refunded(slow, listener_factory, ids, cert_client):
    """Máy tin cậy (chứng chỉ client đã tin) bắt tay liên tiếp nhiều lần: không bị giới hạn tốc độ,
    dù IP chưa ưu tiên (chưa từng giao kết nối điều khiển)."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    ctx = cert_client(ids.b)
    results = []
    for _ in range(8):
        results.append(await tls_attempt(h.port, ctx))
        await h.wait_idle()
    assert results == [True] * 8, results
    assert refusals(h) == no_refusals()
    assert h.listener.is_priority(LOCAL) is False


@real_limits
@pytest.mark.parametrize("client", ["anonymous", "unknown-cert"])
async def test_anonymous_and_failed_handshakes_spend_the_budget(
    slow, monkeypatch, listener_factory, ids, cert_client, client
):
    # Không nạp lại trong lúc chạy: kết quả không phụ thuộc tốc độ máy.
    monkeypatch.setattr(admission, "HANDSHAKE_RATE_PER_IP", 0.001)
    h = await listener_factory(ids.server, trusted=[ids.b])
    ctx = tls.client_context() if client == "anonymous" else cert_client(ids.x)
    for _ in range(3):
        await tls_attempt(h.port, ctx)
        await h.wait_idle()
    assert refusals(h)[str(Refusal.RATE_PER_IP)] == 1, refusals(h)


@real_limits
async def test_client_of_refused_connection_sees_unreachable(slow, monkeypatch, listener_factory, ids):
    """Từ chối trước TLS là lỗi trước bắt tay → `Unreachable` (protocol 6.2: không chặn thử lại)."""
    monkeypatch.setattr(admission, "MAX_OPENING_TOTAL", 0)
    h = await listener_factory(ids.server, trusted=[ids.b])
    with pytest.raises(Unreachable):
        await b_control(h, ids)
    with pytest.raises(Unreachable):
        await open_pairing([LOCAL], h.port, hello_of(ids.anon, "Anon"))
    assert refusals(h)[str(Refusal.OPENING_TOTAL)] == 2


# --- Pool ưu tiên ----------------------------------------------------------------------------------


@real_limits
async def test_ip_with_incoming_control_is_admitted_when_general_pool_is_closed(
    slow, monkeypatch, listener_factory, raw_client, ids
):
    h = await listener_factory(ids.server, trusted=[ids.b])
    assert h.listener.is_priority(LOCAL) is False
    first = await b_control(h, ids)
    try:
        await h.next()
        assert h.listener.is_priority(LOCAL) is True
        monkeypatch.setattr(admission, "MAX_OPENING_TOTAL", 0)  # pool chung cạn hẳn
        second = await b_control(h, ids)
        await h.next()
        await second.close()
        silent = await raw_client(h.port, use_tls=False)
        assert await silent.stays_open(0.2), "IP ưu tiên phải vào được pool ưu tiên"
        assert refusals(h) == no_refusals()
    finally:
        await first.close()


@real_limits
async def test_update_trusted_dropping_device_moves_ip_back_to_general_even_with_live_connection(
    slow, monkeypatch, listener_factory, raw_client, ids
):
    h = await listener_factory(ids.server, trusted=[ids.b])
    live = await b_control(h, ids)
    try:
        server_side = await h.next()
        assert h.listener.is_priority(LOCAL) is True
        h.listener.update_trusted([])
        h.trusted_fps.clear()
        assert h.listener.is_priority(LOCAL) is False, "hủy ghép đôi thắng kết nối đang sống"
        monkeypatch.setattr(admission, "MAX_OPENING_TOTAL", 0)
        peer = await raw_client(h.port, use_tls=False)
        _, data = await peer.expect_closed(2.0)
        assert data == b""
        assert refusals(h)[str(Refusal.OPENING_TOTAL)] == 1
        # Kết nối cũ không bị Listener đóng.
        await live.send(Ping(t=1))
        assert await asyncio.wait_for(server_side.receive(), 2) == Ping(t=1)
    finally:
        await live.close()


@real_limits
@pytest.mark.parametrize(
    "bad_pem", ["không phải PEM", "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"]
)
async def test_priority_is_dropped_even_if_building_new_context_fails(slow, listener_factory, ids, bad_pem):
    h = await listener_factory(ids.server, trusted=[ids.b])
    live = await b_control(h, ids)
    try:
        await h.next()
        assert h.listener.is_priority(LOCAL) is True
        with pytest.raises((ssl.SSLError, ValueError, TypeError)):
            h.listener.update_trusted([bad_pem])
        assert h.listener.is_priority(LOCAL) is False
    finally:
        await live.close()


@real_limits
async def test_priority_survives_close_of_incoming_control_within_ttl(
    slow, monkeypatch, listener_factory, raw_client, ids
):
    h = await listener_factory(ids.server, trusted=[ids.b])
    conn = await b_control(h, ids)
    server_side = await h.next()
    await conn.close()
    await server_side.close()
    h.release.set()  # on_connection của harness kết thúc → control_closed
    await asyncio.sleep(0.2)
    assert h.listener.is_priority(LOCAL) is True, "trong PRIORITY_TTL sau khi đóng vẫn ưu tiên"
    monkeypatch.setattr(admission, "PRIORITY_TTL", 0.3)
    await asyncio.sleep(0.5)
    assert h.listener.is_priority(LOCAL) is False, "hết TTL thì về pool chung"


@real_limits
async def test_outgoing_control_marks_ip_priority(slow, monkeypatch, listener_factory, ids, cert_client):
    """Node mở `open_control` ra thành công → báo `note_outgoing_control`: IP đó ưu tiên tới khi
    `release_outgoing_control`, rồi thêm TTL; `update_trusted` bỏ thiết bị thì mất ngay.

    Kết nối thử là bắt tay **tin cậy** (kết nối không tin cậy từ IP ưu tiên tốn ngân sách lỗi, có thể
    làm IP bị hạ)."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    handle = h.listener.note_outgoing_control(LOCAL, ids.b.fingerprint)
    assert isinstance(handle, int) and handle > 0
    assert h.listener.is_priority(LOCAL) is True
    monkeypatch.setattr(admission, "MAX_OPENING_TOTAL", 0)  # pool chung đóng hẳn
    assert await tls_attempt(h.port, cert_client(ids.b)) is True, "IP ưu tiên phải vào được pool ưu tiên"
    await h.wait_idle()
    h.listener.release_outgoing_control(handle)
    h.listener.release_outgoing_control(handle)  # gọi lại không sao
    assert h.listener.is_priority(LOCAL) is True
    h.listener.update_trusted([])
    assert h.listener.is_priority(LOCAL) is False
    assert refusals(h) == no_refusals()


@real_limits
async def test_ipv4_mapped_address_counts_as_ipv4(slow, listener_factory, ids):
    h = await listener_factory(ids.server, trusted=[ids.b])
    h.listener.note_outgoing_control("::ffff:" + LOCAL, ids.b.fingerprint)
    assert h.listener.is_priority(LOCAL) is True


# --- Ngân sách lỗi và hạ ưu tiên (`docs/protocol.md` mục 2.3) ----------------------------------------
# mTLS chứng minh thiết bị, không chứng minh IP: kẻ lạ relay byte tới máy tin cậy thật thì giành được
# ưu tiên cho IP của nó. Mỗi IP ưu tiên được tha `PRIORITY_STRIKES` lần kết thúc không tin cậy (hồi dần;
# kết nối điều khiển sống ≥ 60 s lúc đóng cộng lại 1, bắt tay tin cậy thì không); lần kế tiếp
# thì IP bị hạ về pool chung trong PRIORITY_TTL, thắng cả kết nối điều khiển đang sống lẫn kết nối mới.
# Ngân sách là để máy tin cậy nhiều địa chỉ (lần thử thua bị `_open_any` hủy giữa bắt tay) không bị hạ
# oan.

SECOND_LOCAL = "127.0.0.2"
OTHER_PRIORITY_IP = "192.0.2.10"  # TEST-NET-1: chỉ dùng làm khóa trong bảng ưu tiên


def can_bind(ip: str) -> bool:
    """macOS chỉ có 127.0.0.1 trên lo0: bind 127.0.0.2 lỗi EADDRNOTAVAIL."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((ip, 0))
        except OSError:
            return False
    return True


async def bare_tcp_from(h, src: str) -> None:
    """TCP trần từ địa chỉ nguồn `src` (như `priority_starve.py`): chờ server nhận hoặc từ chối kết nối,
    rồi cắt (RST) và chờ server trả vé."""
    before = sum(h.listener.refusals.values())
    _, writer = await asyncio.wait_for(asyncio.open_connection(LOCAL, h.port, local_addr=(src, 0)), 3)
    try:
        await wait_until(
            lambda: h.listener.opening_count > 0 or sum(h.listener.refusals.values()) > before,
            2.0,
            "server chưa nhận/từ chối kết nối TCP trần",
        )
    finally:
        writer.transport.abort()
        with contextlib.suppress(Exception):
            writer.close()
    await h.wait_idle()


@real_limits
@pytest.mark.parametrize(
    "how", ["bare_tcp", "silent_timeout", "anonymous", "unknown_cert", "stale_trusted_cert"]
)
async def test_priority_ip_whose_connection_ends_untrusted_is_demoted(
    fast, monkeypatch, listener_factory, raw_client, ids, cert_client, how
):
    """IP ưu tiên (giành qua relay; ở đây cấp bằng `note_outgoing_control`) mở một kết nối không kết thúc
    bằng bắt tay tin cậy → IP bị hạ: `is_priority` False dù kết nối điều khiển cấp ưu tiên vẫn sống, kể
    cả khi báo thêm kết nối điều khiển mới; kết nối sau vào pool chung. IP ưu tiên khác không bị ảnh hưởng.

    `stale_trusted_cert`: chứng chỉ còn trong context TLS cũ nhưng `is_trusted` đã trả False (vừa hủy ghép
    đôi): Listener không được gọi `trusted()` (sửa thấp), nên vé cũng là “chưa tin cậy”.

    `PRIORITY_STRIKES = 0`: lần đầu đã hạ (ca ngân sách mặc định ở test ngay dưới)."""
    monkeypatch.setattr(admission, "PRIORITY_STRIKES", 0)
    if how != "silent_timeout":
        monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    h = await listener_factory(ids.server, trusted=[ids.b])
    h.listener.note_outgoing_control(LOCAL, ids.b.fingerprint)
    h.listener.note_outgoing_control(OTHER_PRIORITY_IP, ids.b.fingerprint)
    assert h.listener.is_priority(LOCAL) is True
    if how == "bare_tcp":
        p = await raw_client(h.port, use_tls=False)
        await wait_until(lambda: h.listener.opening_count == 1, 2.0, "server chưa nhận kết nối")
        p.abort()
    elif how == "silent_timeout":
        p = await raw_client(h.port, use_tls=False)
        await p.expect_closed(3.0)
    elif how == "anonymous":
        p = await raw_client(h.port, None)
        await p.read_hello()
        p.abort()
    elif how == "unknown_cert":
        assert await tls_attempt(h.port, cert_client(ids.x)) is False
    else:
        h.trusted_fps.clear()  # hủy ghép đôi, context TLS chưa kịp đổi
        assert await tls_attempt(h.port, cert_client(ids.b)) is True, "bắt tay phải qua context cũ"
    await h.wait_idle(timeout=3.0)
    assert h.listener.is_priority(LOCAL) is False, "IP ưu tiên dùng kết nối không tin cậy phải bị hạ"
    assert h.listener.is_priority(OTHER_PRIORITY_IP) is True, "chỉ IP vi phạm bị hạ"
    h.listener.note_outgoing_control(LOCAL, ids.b.fingerprint)
    assert h.listener.is_priority(LOCAL) is False, "kết nối điều khiển mới không gỡ được hạ ưu tiên"
    monkeypatch.setattr(admission, "MAX_OPENING_TOTAL", 0)  # pool chung đóng: IP bị hạ không vào được
    late = await raw_client(h.port, use_tls=False)
    _, data = await late.expect_closed(2.0)
    assert data == b""
    assert refusals(h)[str(Refusal.OPENING_TOTAL)] == 1


@real_limits
async def test_priority_ip_is_forgiven_strikes_then_demoted(
    slow, monkeypatch, listener_factory, raw_client, ids
):
    """Ngân sách lỗi mặc định (4): 4 kết nối TCP trần từ IP ưu tiên được tha (vẫn ưu tiên), kết nối thứ 5
    làm IP bị hạ. Burst ưu tiên nới lên 8 (tốc độ nạp gần 0) để kết nối thứ 5 được nhận ngay, không phụ
    thuộc thời gian."""
    for name, value in {
        "PRIORITY_BURST_PER_IP": 8,
        "PRIORITY_BURST_TOTAL": 8,
        "PRIORITY_RATE_PER_IP": 0.001,
        "PRIORITY_RATE_TOTAL": 0.001,
    }.items():
        monkeypatch.setattr(admission, name, value)
    h = await listener_factory(ids.server, trusted=[ids.b])
    h.listener.note_outgoing_control(LOCAL, ids.b.fingerprint)
    for k in range(admission.PRIORITY_STRIKES):
        p = await raw_client(h.port, use_tls=False)
        await wait_until(lambda: h.listener.opening_count == 1, 2.0, "server chưa nhận kết nối")
        p.abort()
        await h.wait_idle()
        assert h.listener.is_priority(LOCAL) is True, f"lần lỗi thứ {k + 1} phải được tha"
    p = await raw_client(h.port, use_tls=False)
    await wait_until(lambda: h.listener.opening_count == 1, 2.0, "server chưa nhận kết nối thứ 5")
    p.abort()
    await h.wait_idle()
    assert h.listener.is_priority(LOCAL) is False
    assert refusals(h) == no_refusals()


@real_limits
async def test_trusted_handshakes_from_priority_ip_keep_its_priority(
    slow, listener_factory, ids, cert_client
):
    """Đối chứng: vé đã `trusted()` rồi mới kết thúc (bỏ dở trước message mở đầu, hay kết nối điều khiển
    đầy đủ) không làm IP bị hạ. 6 lần > burst ưu tiên theo IP (4): còn kiểm luôn việc hoàn token."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    h.listener.note_outgoing_control(LOCAL, ids.b.fingerprint)
    for _ in range(6):
        assert await tls_attempt(h.port, cert_client(ids.b)) is True
        await h.wait_idle()
    conn = await b_control(h, ids)
    try:
        await h.next()
    finally:
        await conn.close()
    await h.wait_idle()
    assert h.listener.is_priority(LOCAL) is True
    assert refusals(h) == no_refusals()


@real_limits
async def test_handshake_with_cert_no_longer_trusted_is_not_refunded(
    slow, monkeypatch, listener_factory, ids, cert_client
):
    """Sửa thấp: `trusted()` (hoàn token) chỉ khi `is_trusted(fp)` còn True. Chứng chỉ vẫn trong context
    TLS cũ nhưng đã bị hủy ghép đôi → bắt tay qua mà vẫn tiêu ngân sách như bắt tay hỏng."""
    monkeypatch.setattr(admission, "HANDSHAKE_RATE_PER_IP", 0.001)  # không nạp lại trong lúc chạy
    h = await listener_factory(ids.server, trusted=[ids.b])
    h.trusted_fps.clear()
    results = []
    for _ in range(3):
        results.append(await tls_attempt(h.port, cert_client(ids.b)))
        await h.wait_idle()
    assert results[:2] == [True, True], "bắt tay qua context cũ (phép thử vô nghĩa nếu không)"
    assert results[2] is False
    assert refusals(h) == {**no_refusals(), str(Refusal.RATE_PER_IP): 1}
    assert h.calls == 0


@real_limits
async def test_flood_from_demoted_priority_ip_does_not_starve_trusted_device_at_other_priority_ip(
    slow, monkeypatch, listener_factory, ids
):
    """Kịch bản `priority_starve.py`: IP A = 127.0.0.2 đã giành ưu tiên (relay) lũ TCP trần; máy tin cậy
    thật ở IP ưu tiên B = 127.0.0.1 vẫn `open_control` được, xen giữa các đợt lũ.

    Ngân sách lỗi mặc định (4): A chỉ lấy được `PRIORITY_STRIKES + 1` = 5 vé ưu tiên rồi bị hạ. Tốc độ
    nạp lại gần 0 (kết quả không phụ thuộc thời gian) và burst ưu tiên 8, nên sau A còn 3 token cho B
    (B được hoàn mỗi lần). Không có hạ ưu tiên thì A vét cả 8 và B bị từ chối."""
    if not can_bind(SECOND_LOCAL):
        pytest.skip("không bind được 127.0.0.2 (macOS); ca một IP ở test_priority_ip_whose_connection_ends_…")
    for name in (
        "PRIORITY_RATE_PER_IP",
        "PRIORITY_RATE_TOTAL",
        "HANDSHAKE_RATE_PER_IP",
        "HANDSHAKE_RATE_TOTAL",
    ):
        monkeypatch.setattr(admission, name, 0.001)
    monkeypatch.setattr(admission, "PRIORITY_BURST_PER_IP", 8)
    monkeypatch.setattr(admission, "PRIORITY_BURST_TOTAL", 8)
    h = await listener_factory(ids.server, trusted=[ids.b])
    h.listener.note_outgoing_control(SECOND_LOCAL, ids.b.fingerprint)  # A: giành được qua relay
    h.listener.note_outgoing_control(LOCAL, ids.b.fingerprint)  # B: máy tin cậy thật
    assert h.listener.is_priority(SECOND_LOCAL) is True
    for _ in range(admission.PRIORITY_STRIKES + 1):
        await bare_tcp_from(h, SECOND_LOCAL)
    assert h.listener.is_priority(SECOND_LOCAL) is False, "A phải bị hạ sau PRIORITY_STRIKES + 1 lần"
    for _ in range(3):
        for _ in range(4):
            await bare_tcp_from(h, SECOND_LOCAL)
        assert h.listener.is_priority(SECOND_LOCAL) is False
        conn = await b_control(h, ids)
        try:
            await h.next()
        finally:
            await conn.close()
        await h.wait_idle()
    assert h.listener.is_priority(LOCAL) is True
    assert refusals(h)[str(Refusal.RATE_TOTAL)] == 0, refusals(h)


class DelayProxy:
    """Địa chỉ thứ hai/ba của “máy server”: nghe ở (ip, port), chuyển tiếp tới 127.0.0.1:port (nên server
    thấy nguồn là 127.0.0.1, như một máy khách có một IP nối tới máy server có nhiều IP). Chiều server →
    client trễ `delay` giây, nên lần thử qua đây luôn thua và bị `_open_any` hủy giữa bắt tay."""

    def __init__(self, ip: str, port: int, delay: float) -> None:
        self.ip, self.port, self.delay = ip, port, delay
        self.accepted = 0
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.ip, self.port)

    async def _pump(self, reader, writer, delay: float) -> None:
        with contextlib.suppress(ConnectionError, OSError):
            while data := await reader.read(65536):
                if delay:
                    await asyncio.sleep(delay)
                writer.write(data)
                await writer.drain()

    async def _handle(self, c_reader, c_writer) -> None:
        self.accepted += 1
        self._tasks.add(asyncio.current_task())
        self._writers.append(c_writer)
        try:
            s_reader, s_writer = await asyncio.open_connection(LOCAL, self.port)
        except OSError:
            c_writer.transport.abort()
            return
        self._writers.append(s_writer)
        pumps = [
            asyncio.create_task(self._pump(c_reader, s_writer, 0.0)),
            asyncio.create_task(self._pump(s_reader, c_writer, self.delay)),
        ]
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in pumps:
                t.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for w in (c_writer, s_writer):
                w.transport.abort()  # client hủy → cắt luôn phía server (RST)
            self._tasks.discard(asyncio.current_task())

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for w in self._writers:
            w.transport.abort()
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=2)
        if self._server is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), 2)


@real_limits
async def test_three_address_trusted_device_survives_three_connections_and_fourth_demotes(
    slow, monkeypatch, listener_factory, ids
):
    """Qua `open_control` thật: máy server có 3 địa chỉ (127.0.0.1 trực tiếp + 127.0.0.2/3 qua
    proxy trễ). Mỗi lần nối, lần thử trực tiếp thắng; hai lần thử kia bị `_open_any` hủy giữa bắt tay nên
    vé của chúng ở server kết thúc không tin cậy. Lần nối 1 làm 127.0.0.1 thành IP ưu tiên (lần thua của nó
    còn ở pool chung); lần 2, 3 mỗi lần tốn 2 (4 → 2 → 0), vẫn ưu tiên; lần thứ 4 liền nhau thì bị hạ
    (`trusted()` không cộng lại, kết nối trước sống < 60 s)."""
    extra = ("127.0.0.2", "127.0.0.3")
    if not all(can_bind(ip) for ip in extra):
        pytest.skip("không bind được 127.0.0.2/3 (macOS); ca đơn vị ở tests/core/test_admission_demotion.py")
    # Tốc độ nới (không phải phép thử tốc độ); ngân sách lỗi giữ mặc định thật.
    for name in (
        "HANDSHAKE_RATE_PER_IP",
        "HANDSHAKE_BURST_PER_IP",
        "HANDSHAKE_RATE_TOTAL",
        "HANDSHAKE_BURST_TOTAL",
        "PRIORITY_RATE_PER_IP",
        "PRIORITY_BURST_PER_IP",
        "PRIORITY_RATE_TOTAL",
        "PRIORITY_BURST_TOTAL",
    ):
        monkeypatch.setattr(admission, name, 1000.0)
    h = await listener_factory(ids.server, trusted=[ids.b])
    gate = h.listener._gate  # chẩn đoán: Listener không có API cho ngân sách lỗi
    loop = asyncio.get_running_loop()
    proxies = [DelayProxy(ip, h.port, delay=1.0) for ip in extra]
    expected_left = [admission.PRIORITY_STRIKES, 2.0, 0.0]
    try:
        for proxy in proxies:
            await proxy.start()
        for n in range(1, 5):
            before = [p.accepted for p in proxies]
            conn = await open_control([LOCAL, *extra], h.port, ids.b, hello_of(ids.b), h.identity.cert_pem())
            try:
                assert conn.peer.address == LOCAL, "lần thử trực tiếp phải thắng"
                await h.next()
            finally:
                await conn.close()
            await h.wait_idle(timeout=3.0)  # vé của hai lần thử thua đã kết thúc ở server
            assert [p.accepted for p in proxies] == [b + 1 for b in before], "mỗi proxy phải có một lần thử"
            if n <= 3:
                assert h.listener.is_priority(LOCAL) is True, f"lần nối thứ {n} làm IP bị hạ oan"
                left = gate.priority_strikes(LOCAL, loop.time())
                assert left == pytest.approx(expected_left[n - 1], abs=0.05), f"sau lần nối {n}: {left}"
        assert h.listener.is_priority(LOCAL) is False, "lần nối thứ 4 liền nhau phải làm IP bị hạ"
        assert refusals(h) == no_refusals()
    finally:
        for proxy in proxies:
            await proxy.close()


@real_limits
@pytest.mark.parametrize("direction", ["incoming", "outgoing"])
@pytest.mark.parametrize(("credit_after", "credited"), [(0.0, True), (1000.0, False)])
async def test_closing_control_connection_credits_a_strike_after_strike_credit_after(
    slow, monkeypatch, listener_factory, raw_client, ids, direction, credit_after, credited
):
    """Kết nối điều khiển (vào qua Listener, hay ra qua `note_outgoing_control`) đóng sau khi sống
    ≥ `STRIKE_CREDIT_AFTER` thì IP được cộng lại 1; sớm hơn thì không. Hằng đặt 0 / 1000 s để không phụ
    thuộc thời gian thực."""
    monkeypatch.setattr(admission, "STRIKE_CREDIT_AFTER", credit_after)
    h = await listener_factory(ids.server, trusted=[ids.b])
    gate = h.listener._gate  # chẩn đoán
    loop = asyncio.get_running_loop()
    h.listener.note_outgoing_control(LOCAL, ids.b.fingerprint)  # giữ mục ưu tiên sống suốt test
    if direction == "incoming":
        client = await b_control(h, ids)
        server_side = await h.next()
    else:
        handle = h.listener.note_outgoing_control(LOCAL, ids.b.fingerprint)
    for _ in range(2):  # hai lần lỗi: TCP trần
        p = await raw_client(h.port, use_tls=False)
        await wait_until(lambda: h.listener.opening_count == 1, 2.0, "server chưa nhận kết nối")
        p.abort()
        await h.wait_idle()
    assert gate.priority_strikes(LOCAL, loop.time()) == pytest.approx(2.0, abs=0.05)
    if direction == "incoming":
        await client.close()
        h.release.set()  # on_connection của harness kết thúc → control_closed
        await wait_until(lambda: server_side.closed, 3.0, "kết nối phía server chưa đóng")
    else:
        h.listener.release_outgoing_control(handle)
    expected = 3.0 if credited else 2.0
    assert gate.priority_strikes(LOCAL, loop.time()) == pytest.approx(expected, abs=0.05)
    assert h.listener.is_priority(LOCAL) is True


# --- Kết nối dữ liệu (spec P2-3 mục 3.1 bước 2 và 5, AC-4) -----------------------------------------
# DATA được hoàn token như kết nối điều khiển (protocol §2.3), nhưng không cấp và không giữ ưu tiên
# (chỉ kết nối điều khiển cấp ưu tiên), và vé đã `trusted()` không tính là một lần lỗi.

DATA_OPEN = {"type": "data_open", "transfer_id": "0123456789abcdef" * 2, "file_index": 0, "resume_seq": 1}


async def open_raw_data(h, ctx: ssl.SSLContext, identity, delivered) -> None:
    """TLS bằng `ctx`, đọc `hello`, gửi `hello` + `data_open`; chờ `delivered()` đúng rồi mới cắt."""
    from teleport.core.framing import encode_message
    from teleport.net.stream import read_message

    reader, writer = await asyncio.wait_for(asyncio.open_connection(LOCAL, h.port, ssl=ctx), 3)
    try:
        msg = await asyncio.wait_for(read_message(reader, 16 * 1024), 2)
        assert isinstance(msg, dict) and msg.get("type") == "hello"
        hello = {"type": "hello", "protocol": PROTOCOL_VERSION, "device_id": identity.device_id, "name": "B"}
        hello.update(app_version="0.0.0", os="windows")
        writer.write(encode_message(hello) + encode_message(DATA_OPEN))
        await writer.drain()
        await wait_until(delivered, 2.0, "kết nối dữ liệu chưa được giao")
    finally:
        writer.transport.abort()


@real_limits
async def test_trusted_data_connections_are_refunded(slow, monkeypatch, listener_factory, ids, cert_client):
    """AC-4 (a): 8 lần mở DATA liên tiếp bằng chứng chỉ tin cậy, tốc độ nạp gần 0: đều được giao, không
    lần nào bị từ chối (bắt tay tin cậy được hoàn token, protocol §2.3)."""
    from teleport.core.messages import ConnectionKind

    monkeypatch.setattr(admission, "HANDSHAKE_RATE_PER_IP", 0.001)
    delivered: list = []

    async def on_connection(conn):
        delivered.append(conn.kind)

    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    ctx = cert_client(ids.b)
    for k in range(8):
        await open_raw_data(h, ctx, ids.b, lambda k=k: len(delivered) == k + 1)
        await h.wait_idle()
    assert delivered == [ConnectionKind.DATA] * 8
    assert refusals(h) == no_refusals()


@real_limits
async def test_data_connection_does_not_grant_priority(slow, listener_factory, ids, cert_client):
    """AC-4 (b): DATA được giao (còn sống, rồi đóng) mà không có kết nối điều khiển → IP không ưu tiên."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    assert h.listener.is_priority(LOCAL) is False
    await open_raw_data(h, cert_client(ids.b), ids.b, lambda: h.calls == 1)
    conn = await h.next()
    assert h.listener.is_priority(LOCAL) is False, "kết nối dữ liệu đang sống không được cấp ưu tiên"
    conn.abort()
    h.release.set()
    await wait_until(lambda: h.listener.connection_count == 0, 2.0, "kết nối dữ liệu chưa kết thúc")
    await h.wait_idle()
    assert h.listener.is_priority(LOCAL) is False
    assert refusals(h) == no_refusals()


@real_limits
async def test_data_connection_from_priority_ip_keeps_priority_and_strikes(
    slow, listener_factory, ids, cert_client
):
    """AC-4 (c): IP đang ưu tiên (có kết nối điều khiển vào) mở rồi đóng DATA tin cậy → vẫn ưu tiên và ngân
    sách lỗi không giảm."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    control = await b_control(h, ids)
    try:
        await h.next()
        assert h.listener.is_priority(LOCAL) is True
        gate = h.listener._gate  # chẩn đoán: Listener không có API cho ngân sách lỗi
        loop = asyncio.get_running_loop()
        before = gate.priority_strikes(LOCAL, loop.time())
        await open_raw_data(h, cert_client(ids.b), ids.b, lambda: h.calls == 2)
        data = await h.next()
        assert data.kind.value == "data"
        data.abort()
        await h.wait_idle()
        after = gate.priority_strikes(LOCAL, loop.time())
        assert after >= before - 1e-9, f"ngân sách lỗi giảm từ {before} xuống {after}"
        assert h.listener.is_priority(LOCAL) is True
        assert refusals(h) == no_refusals()
    finally:
        await control.close()


# --- Chỗ được trả trên mọi đường ra --------------------------------------------------------------


async def test_slots_are_released_on_timeout(fast, listener_factory, raw_client, ids):
    h = await listener_factory(ids.server)
    silent = await raw_client(h.port, use_tls=False)
    half = await raw_client(h.port, use_tls=False)
    half.send_bytes(client_hello_bytes())  # bắt tay dở dang
    await half.drain()
    anon = await raw_client(h.port, None)
    await anon.read_hello()  # ẩn danh, đã giữ chỗ ẩn danh, không gửi gì thêm
    await asyncio.sleep(0.05)
    assert h.listener.opening_count == 3
    await h.wait_idle(timeout=4.0)
    for p in (silent, half, anon):
        await p.expect_closed(2.0)
    conn = await open_pairing([LOCAL], h.port, hello_of(ids.anon, "Anon"))  # chỗ ẩn danh đã trả
    await conn.close()


async def test_slots_are_released_on_bad_certificate(slow, listener_factory, ids, cert_client):
    h = await listener_factory(ids.server, trusted=[ids.b])
    for _ in range(3):
        assert await tls_attempt(h.port, cert_client(ids.x)) is False
    await h.wait_idle()
    with pytest.raises(Rejected):
        await b_control(h, ids, identity=ids.x)
    await h.wait_idle()
    assert h.listener.opening_count == 0


async def test_slots_are_released_when_listener_closes(slow, listener_factory, raw_client, ids):
    h = await listener_factory(ids.server)
    peers = [await raw_client(h.port, use_tls=False) for _ in range(2)]
    half = await raw_client(h.port, use_tls=False)
    half.send_bytes(client_hello_bytes())
    await half.drain()
    anon = await raw_client(h.port, None)
    await anon.read_hello()
    await asyncio.sleep(0.05)
    assert h.listener.opening_count == 4
    t0 = time.monotonic()
    await asyncio.wait_for(h.listener.close(), 10)
    assert time.monotonic() - t0 < 5.0
    assert h.listener.opening_count == 0
    for p in (*peers, half, anon):
        await p.expect_closed(2.0)


async def test_slots_are_released_when_client_cancels_mid_handshake(slow, listener_factory, raw_client, ids):
    h = await listener_factory(ids.server)
    for _ in range(3):
        p = await raw_client(h.port, use_tls=False)
        p.send_bytes(client_hello_bytes())
        await p.drain()
        await read_some(p)  # ServerHello tới: server đang chờ phần còn lại
        p.abort()
    await h.wait_idle()
    task = asyncio.create_task(open_pairing([LOCAL], h.port, hello_of(ids.anon, "Anon")))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await h.wait_idle()
    assert h.listener.opening_count == 0
