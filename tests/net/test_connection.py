"""Test `teleport.net.connection` theo đặc tả PR-2 (mục 2.3, 3, 4.4).

Nhóm ID: U (unit), S (đường thành công), R (server từ chối), L (giới hạn), T (đổi danh sách
tin cậy), C (phía client), A (API `Connection`), K- (`ControlChannel`), LC (vòng đời `Listener`).
Mọi test mở socket mang `@pytest.mark.integration` và chỉ bind 127.0.0.1. Hằng thời gian được patch
qua module `teleport.net.connection` (fixture `fast`) trước khi tạo Listener hay gọi `open_*`.
"""

import asyncio
import contextlib
import json
import logging
import socket
import ssl
import struct
import sys
import time
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.core.framing import HEADER, MAX_PAIRING_MESSAGE_SIZE, FramingError, decode_body, encode_message
from teleport.core.messages import (
    MAX_SAFE_INTEGER,
    PROTOCOL_VERSION,
    ConnectionKind,
    ControlOpen,
    Hello,
    MessageError,
    PairAbort,
    PairCommit,
    PairNonce,
    PairRequest,
    PairReveal,
    Ping,
    Pong,
    UnknownMessageType,
    WrongConnectionKind,
    parse_message,
    to_dict,
)
from teleport.net import connection, tls
from teleport.net.connection import (
    CloseReason,
    ConnectError,
    ConnectionLostError,
    ControlChannel,
    IdentityMismatch,
    Listener,
    Rejected,
    Unreachable,
    VersionMismatch,
    most_relevant_error,
    open_control,
    open_pairing,
)
from teleport.trust.identity import load_or_create_identity

# Kiểm rò rỉ (mục 4.1): `no_leaks` bắt task còn sót; socket bị giải phóng mà chưa đóng phát
# ResourceWarning ngay trong `__del__`, nên biến cảnh báo đó thành lỗi để test đỏ.
pytestmark = [
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

LOCAL = "127.0.0.1"
TEST_NET = "192.0.2.1"  # TEST-NET-1 (RFC 5737): không ai trả lời
HEX64 = "ab" * 32
CLOSED = (ConnectionError, ssl.SSLError)
DATA_OPEN = {"type": "data_open", "transfer_id": "0123456789abcdef" * 2, "file_index": 0, "resume_seq": 1}
PI, DA = 0.2, 0.6  # ping_interval, dead_after cho test ControlChannel (mục 4.4)


# --- Hàm phụ trợ ---------------------------------------------------------------------------------


def hello_of(identity, name: str = "B", **overrides) -> Hello:
    fields = {
        "protocol": PROTOCOL_VERSION,
        "device_id": identity.device_id,
        "name": name,
        "app_version": "0.0.0",
        "os": "windows",
    }
    fields.update(overrides)
    return Hello(**fields)


def wire_hello(identity_or_id, name: str = "B", **overrides) -> dict:
    device_id = identity_or_id if isinstance(identity_or_id, str) else identity_or_id.device_id
    d = {
        "type": "hello",
        "protocol": PROTOCOL_VERSION,
        "device_id": device_id,
        "name": name,
        "app_version": "0.0.0",
        "os": "windows",
    }
    d.update(overrides)
    return d


def pair_request(identity) -> PairRequest:
    return PairRequest(cert=identity.cert_pem())


def frame(body: bytes) -> bytes:
    return HEADER.pack(len(body)) + body


@pytest.fixture(scope="module")
def ids(tmp_path_factory):
    """Danh tính dùng chung cho cả module (chỉ đọc, nên chia sẻ được): server A, B tin cậy, X lạ."""
    base = tmp_path_factory.mktemp("conn-ids")
    return SimpleNamespace(
        server=load_or_create_identity(base / "server"),
        b=load_or_create_identity(base / "b"),
        x=load_or_create_identity(base / "stranger"),
        anon=load_or_create_identity(base / "anon"),
    )


@pytest.fixture
def slow_opening(fast, monkeypatch):
    """OPENING_TIMEOUT = 5 s: đóng nhanh thì chắc chắn là bị từ chối ngay, không phải hết giờ."""
    monkeypatch.setattr(fast, "OPENING_TIMEOUT", 5.0)
    return fast


async def b_control(h, ids, *, identity=None, expected: str | None = None, addresses=(LOCAL,)):
    identity = identity if identity is not None else ids.b
    return await open_control(
        list(addresses), h.port, identity, hello_of(identity, "B"), expected or h.identity.fingerprint
    )


async def anon_pairing(h, ids):
    return await open_pairing([LOCAL], h.port, hello_of(ids.anon, "Anon"))


async def raw_opening(raw_client, h, identity, hello: dict, opening: dict | None):
    """Client thô: TLS (có chứng chỉ `identity` hoặc ẩn danh), đọc hello server, gửi hello rồi `opening`."""
    peer = await raw_client(h.port, identity)
    await peer.read_hello()
    peer.send_dict(hello)
    if opening is not None:
        peer.send_dict(opening)
    await peer.drain()
    return peer


async def raw_control(raw_client, h, ids):
    peer = await raw_opening(raw_client, h, ids.b, wire_hello(ids.b), {"type": "control_open"})
    return peer, await h.next()


async def raw_pairing(raw_client, h, ids):
    peer = await raw_opening(
        raw_client, h, None, wire_hello(ids.anon, "Anon"), to_dict(pair_request(ids.anon))
    )
    return peer, await h.next()


async def read_messages_for(peer, duration: float) -> list[dict]:
    """Đọc mọi message tới trong `duration` giây."""
    out: list[dict] = []
    deadline = time.monotonic() + duration
    while (left := deadline - time.monotonic()) > 0:
        try:
            msg = await peer.read(timeout=left)
        except TimeoutError:
            break
        if msg is None:
            break
        out.append(msg)
    return out


async def read_until_type(peer, type_name: str, timeout: float = 2.0) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            msg = await peer.read(timeout=timeout)
            assert msg is not None, f"kết nối đóng trước khi nhận {type_name}"
            if msg.get("type") == type_name:
                return msg


async def cancel_wait(*tasks: asyncio.Task) -> None:
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(BaseException):
            await t


async def wait_until(predicate, timeout: float = 2.0, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.01)


async def receive_closed(conn, timeout: float = 2.0) -> None:
    """Phía client thấy kết nối đã bị máy kia đóng: `receive` trả None hoặc raise ConnectionLostError."""
    try:
        msg = await asyncio.wait_for(conn.receive(), timeout)
    except ConnectionLostError:
        return
    assert msg is None, f"còn nhận được message: {msg!r}"


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((LOCAL, 0))
        return s.getsockname()[1]
    finally:
        s.close()


# --- Client ssl đồng bộ (cho A5, A6, T4: máy kia không bao giờ đọc / dùng lại session) -------------


def _recv_exact(sock: ssl.SSLSocket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if not buf:
                return None
            raise ConnectionError("đóng giữa chừng")
        buf += chunk
    return bytes(buf)


def sync_read_frame(sock: ssl.SSLSocket) -> dict | None:
    header = _recv_exact(sock, HEADER.size)
    if header is None:
        return None
    (n,) = HEADER.unpack(header)
    body = _recv_exact(sock, n)
    if body is None:
        raise ConnectionError("đóng giữa chừng")
    return decode_body(body)


def sync_connect(port: int, ctx: ssl.SSLContext, *, session=None, rcvbuf: int | None = None) -> ssl.SSLSocket:
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if rcvbuf is not None:
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        raw.settimeout(5)
        raw.connect((LOCAL, port))
        return ctx.wrap_socket(raw, session=session)
    except BaseException:
        raw.close()
        raise


def sync_open_control(port: int, identity, *, rcvbuf: int | None = None) -> ssl.SSLSocket:
    sock = sync_connect(port, tls.client_context(identity), rcvbuf=rcvbuf)
    try:
        hello = sync_read_frame(sock)
        assert hello is not None and hello["type"] == "hello"
        sock.sendall(encode_message(wire_hello(identity)) + encode_message({"type": "control_open"}))
    except BaseException:
        sock.close()
        raise
    return sock


# =================================================================================================
# U: unit, không socket
# =================================================================================================


def test_constants_match_spec():
    assert connection.DEFAULT_PORT == 47474
    assert connection.OPENING_TIMEOUT == 10.0
    assert connection.MAX_OPENING_PER_IP == 4
    assert connection.MAX_OPENING_TOTAL == 64
    assert connection.MAX_ANONYMOUS_PER_IP == 1
    assert connection.MAX_ANONYMOUS_TOTAL == 8
    assert connection.DRAIN_TIMEOUT == 30.0
    assert connection.CLOSE_TIMEOUT == 5.0
    assert connection._SEND_CHUNK == 64 * 1024


def test_close_reason_values():
    assert {r.value for r in CloseReason} == {
        "peer_closed",
        "keepalive",
        "connection_lost",
        "protocol_error",
        "local",
    }


def _errors():
    return SimpleNamespace(
        u=Unreachable("u", address="10.0.0.1"),
        u2=Unreachable("u2", address="10.0.0.5"),
        r=Rejected("r", address="10.0.0.2"),
        r2=Rejected("r2", address="10.0.0.6"),
        v=VersionMismatch("v", address="10.0.0.3", peer_protocol=2),
        i=IdentityMismatch("i", address="10.0.0.4", actual_fingerprint=HEX64),
        i2=IdentityMismatch("i2", address="10.0.0.7"),
    )


def test_most_relevant_error_priority():
    e = _errors()
    assert most_relevant_error([e.u, e.v, e.r, e.i]) is e.i
    assert most_relevant_error([e.u, e.v, e.r]) is e.r
    assert most_relevant_error([e.r, e.v, e.u]) is e.r
    assert most_relevant_error([e.u, e.v]) is e.v
    assert most_relevant_error([e.v, e.u]) is e.v
    assert most_relevant_error([e.u]) is e.u


def test_most_relevant_error_same_level_takes_first():
    e = _errors()
    assert most_relevant_error([e.u, e.u2]) is e.u
    assert most_relevant_error([e.u2, e.u]) is e.u2
    assert most_relevant_error([e.r2, e.u, e.r]) is e.r2
    assert most_relevant_error([e.u, e.i2, e.r, e.i]) is e.i2


def test_most_relevant_error_accepts_any_iterable():
    e = _errors()
    assert most_relevant_error(iter([e.u, e.r])) is e.r
    assert most_relevant_error(x for x in (e.v, e.u)) is e.v


def test_most_relevant_error_of_nothing_is_value_error():
    with pytest.raises(ValueError):
        most_relevant_error([])


def test_exception_hierarchy():
    for cls in (Unreachable, Rejected, IdentityMismatch, VersionMismatch):
        assert issubclass(cls, ConnectError)
    assert not issubclass(ConnectionLostError, ConnectError)
    assert not issubclass(ConnectionLostError, OSError)
    assert issubclass(ConnectError, Exception)


def test_exception_attributes():
    assert Unreachable("x").address is None
    assert Rejected("x", address="1.2.3.4").address == "1.2.3.4"
    e = IdentityMismatch("x", address="1.2.3.4", actual_fingerprint=HEX64)
    assert (e.address, e.actual_fingerprint) == ("1.2.3.4", HEX64)
    assert IdentityMismatch("x").actual_fingerprint is None
    v = VersionMismatch("x", peer_protocol=2)
    assert (v.address, v.peer_protocol) == (None, 2)
    assert "x" in str(v)


# --- U3: kiểm tham số trước mọi I/O --------------------------------------------------------------


@pytest.fixture
def no_io(monkeypatch):
    """Mở socket trong test kiểm tham số là lỗi: tham số sai phải bị chặn trước mọi I/O."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("không được mở socket khi tham số sai")

    monkeypatch.setattr(asyncio, "open_connection", forbidden)
    monkeypatch.setattr(asyncio, "start_server", forbidden)


BAD_ADDRESSES = [
    [],
    ["::1"],
    ["localhost"],
    ["0.0.0.0"],
    ["224.0.0.251"],
    ["255.255.255.255"],
    ["01.2.3.4"],
    ["1.2.3"],
    ["1.2.3.256"],
    [" 127.0.0.1"],
    ["127.0.0.1", "::1"],
    [f"10.0.0.{i}" for i in range(1, 18)],  # 17 địa chỉ
    "127.0.0.1",  # chuỗi trần, không phải danh sách
    [127],
    [None],
]


async def _call_open(kind: str, ids, addresses, port, **overrides):
    if kind == "pairing":
        return await open_pairing(addresses, port, overrides.get("hello", hello_of(ids.anon)))
    return await open_control(
        addresses,
        port,
        overrides.get("identity", ids.b),
        overrides.get("hello", hello_of(ids.b)),
        overrides.get("expected", ids.server.fingerprint),
    )


@pytest.mark.parametrize("kind", ["pairing", "control"])
@pytest.mark.parametrize("addresses", BAD_ADDRESSES, ids=repr)
async def test_open_rejects_bad_addresses_before_io(no_io, ids, kind, addresses):
    with pytest.raises(ValueError):
        await _call_open(kind, ids, addresses, 47474)


@pytest.mark.parametrize("kind", ["pairing", "control"])
@pytest.mark.parametrize("port", [0, 65536, -1, True, False, "47474", 47474.0, None], ids=repr)
async def test_open_rejects_bad_port_before_io(no_io, ids, kind, port):
    with pytest.raises(ValueError):
        await _call_open(kind, ids, [LOCAL], port)


@pytest.mark.parametrize("kind", ["pairing", "control"])
async def test_open_rejects_hello_with_other_protocol(no_io, ids, kind):
    identity = ids.anon if kind == "pairing" else ids.b
    with pytest.raises(ValueError):
        await _call_open(kind, ids, [LOCAL], 47474, hello=hello_of(identity, protocol=2))


async def test_open_control_rejects_hello_of_other_identity(no_io, ids):
    with pytest.raises(ValueError):
        await _call_open("control", ids, [LOCAL], 47474, hello=hello_of(ids.x))


@pytest.mark.parametrize(
    "expected",
    ["AB" * 32, "ab" * 31, "ab" * 33, "zz" * 32, "", "ab:" * 31 + "ab", None, b"ab" * 32, "ａ" * 64],
    ids=repr,
)
async def test_open_control_rejects_malformed_fingerprint(no_io, ids, expected):
    with pytest.raises(ValueError):
        await _call_open("control", ids, [LOCAL], 47474, expected=expected)


async def _noop_handler(conn):
    return None


async def test_listener_rejects_hello_of_other_identity(ids):
    with pytest.raises(ValueError):
        Listener(ids.server, hello_of(ids.x), [], lambda fp: False, _noop_handler)


async def test_listener_rejects_hello_with_other_protocol(ids):
    with pytest.raises(ValueError):
        Listener(ids.server, hello_of(ids.server, protocol=2), [], lambda fp: False, _noop_handler)


# Đặc tả chỉ nói "raise" mà không nói lớp lỗi; chuỗi không phải ASCII làm `ssl` báo TypeError.
PEM_ERRORS = (ssl.SSLError, ValueError, TypeError)
BAD_PEMS = ["không phải PEM", "not a PEM", "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"]
BAD_PEM_IDS = ["non-ascii-text", "ascii-text", "broken-pem"]


@pytest.mark.parametrize("bad_pem", BAD_PEMS, ids=BAD_PEM_IDS)
async def test_listener_rejects_bad_trusted_pem_at_construction(ids, bad_pem):
    with pytest.raises(PEM_ERRORS):
        Listener(ids.server, hello_of(ids.server), [bad_pem], lambda fp: False, _noop_handler)


@pytest.mark.parametrize("host", ["::", "::1", "localhost", "", "0", "127.1", None], ids=repr)
async def test_listener_start_rejects_non_ipv4_host(no_io, ids, host):
    listener = Listener(ids.server, hello_of(ids.server), [], lambda fp: False, _noop_handler)
    try:
        with pytest.raises(ValueError):
            await listener.start(host, 0)
    finally:
        await listener.close()


@pytest.mark.parametrize("port", [-1, 65536, True, "0", 1.5], ids=repr)
async def test_listener_start_rejects_bad_port(no_io, ids, port):
    listener = Listener(ids.server, hello_of(ids.server), [], lambda fp: False, _noop_handler)
    try:
        with pytest.raises((ValueError, TypeError)):
            await listener.start(LOCAL, port)
    finally:
        await listener.close()


async def test_listener_port_is_none_before_start(ids):
    listener = Listener(ids.server, hello_of(ids.server), [], lambda fp: False, _noop_handler)
    assert listener.port is None
    assert listener.opening_count == 0
    assert listener.connection_count == 0
    await listener.close()


# =================================================================================================
# S: đường thành công
# =================================================================================================


@pytest.mark.integration
async def test_open_pairing_end_to_end(fast, listener_factory, ids):
    h = await listener_factory(ids.server)
    loop = asyncio.get_running_loop()
    before = loop.time()
    conn = await anon_pairing(h, ids)
    try:
        assert conn.kind is ConnectionKind.PAIRING
        assert conn.is_server is False
        assert conn.opening is None
        assert conn.closed is False
        assert conn.peer.address == LOCAL
        assert conn.peer.port == h.port
        assert conn.peer.cert is not None
        assert conn.peer.cert.fingerprint == ids.server.fingerprint
        assert conn.peer.fingerprint == ids.server.fingerprint
        assert conn.peer.hello == h.hello
        assert before <= conn.opened_at <= loop.time()

        request = pair_request(ids.anon)
        await conn.send(request)
        server = await h.next()
        assert server.kind is ConnectionKind.PAIRING
        assert server.is_server is True
        assert server.opening == request
        assert server.peer.cert is None
        assert server.peer.fingerprint is None
        assert server.peer.address == LOCAL
        assert server.peer.hello == hello_of(ids.anon, "Anon")
        assert before <= server.opened_at <= loop.time()
        assert h.listener.connection_count == 1
        assert h.listener.opening_count == 0

        # Luồng ghép đôi (protocol §5): commit (B→A), nonce (A→B), reveal (B→A) đi qua kết nối.
        commit = PairCommit(commitment=HEX64)
        await server.send(commit)
        assert await asyncio.wait_for(conn.receive(), 2) == commit
        nonce = PairNonce(nonce=HEX64)
        await conn.send(nonce)
        assert await asyncio.wait_for(server.receive(), 2) == nonce
        reveal = PairReveal(nonce=HEX64)
        await server.send(reveal)
        assert await asyncio.wait_for(conn.receive(), 2) == reveal
        abort = PairAbort(reason="user")
        await conn.send(abort)
        assert await asyncio.wait_for(server.receive(), 2) == abort
    finally:
        await conn.close()


@pytest.mark.integration
async def test_open_control_end_to_end(fast, listener_factory, ids):
    h = await listener_factory(ids.server, trusted=[ids.b])
    start = time.monotonic()
    conn = await b_control(h, ids)
    elapsed = time.monotonic() - start
    try:
        assert elapsed < 1.0
        assert conn.kind is ConnectionKind.CONTROL
        assert conn.is_server is False
        assert conn.opening is None
        assert conn.peer.fingerprint == ids.server.fingerprint
        assert conn.peer.hello == h.hello

        server = await h.next()
        assert server.kind is ConnectionKind.CONTROL
        assert server.is_server is True
        assert server.opening == ControlOpen()
        assert server.peer.fingerprint == ids.b.fingerprint
        assert server.peer.cert.device_id == ids.b.device_id
        assert server.peer.hello.device_id == ids.b.device_id
        assert server.peer.address == LOCAL

        await conn.send(Ping(t=42))
        assert await asyncio.wait_for(server.receive(), 2) == Ping(t=42)
        await server.send(Pong(t=42))
        assert await asyncio.wait_for(conn.receive(), 2) == Pong(t=42)
        await server.send(Ping(t=7))
        assert await asyncio.wait_for(conn.receive(), 2) == Ping(t=7)
    finally:
        await conn.close()


@pytest.mark.integration
async def test_tls12_client_is_refused(fast, listener_factory, raw_client, ids):
    h = await listener_factory(ids.server)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        peer = await raw_client(h.port, ctx=ctx)
    except CLOSED:
        pass  # bắt tay thất bại ngay: đúng
    else:
        _, data = await peer.expect_closed(3.0)
        assert data == b""
    await h.wait_idle()
    assert h.calls == 0


@pytest.mark.integration
async def test_start_returns_real_port_and_busy_port_raises(fast, listener_factory, ids):
    h = await listener_factory(ids.server, start=False)
    assert h.listener.port is None
    port = await h.listener.start(LOCAL, 0)
    assert isinstance(port, int) and port > 0
    assert h.listener.port == port

    other = await listener_factory(ids.server, start=False)
    with pytest.raises(OSError):
        await other.listener.start(LOCAL, port)


@pytest.mark.integration
async def test_start_twice_raises(fast, listener_factory, ids):
    h = await listener_factory(ids.server)
    with pytest.raises(RuntimeError):
        await h.listener.start(LOCAL, 0)


@pytest.mark.integration
async def test_many_concurrent_openings_all_succeed(fast, monkeypatch, listener_factory, ids):
    """S5 (mục 3.6): start_tls là await đầu tiên, ClientHello tới sớm không làm treo bắt tay."""
    for name in ("MAX_OPENING_PER_IP", "MAX_OPENING_TOTAL", "MAX_ANONYMOUS_PER_IP", "MAX_ANONYMOUS_TOTAL"):
        monkeypatch.setattr(connection, name, 1000)
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 10.0)
    h = await listener_factory(ids.server, trusted=[ids.b])

    async def pairing():
        conn = await anon_pairing(h, ids)
        await conn.send(pair_request(ids.anon))
        return conn

    results = await asyncio.gather(
        *(b_control(h, ids) for _ in range(20)), *(pairing() for _ in range(20)), return_exceptions=True
    )
    conns = [r for r in results if not isinstance(r, BaseException)]
    try:
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, f"{len(errors)} lần mở thất bại: {errors[:3]!r}"
        await wait_until(lambda: h.calls == 40, 5.0, "on_connection được gọi đủ 40 lần")
        await asyncio.sleep(0.1)
        assert h.calls == 40
        kinds = [h.accepted.get_nowait().kind for _ in range(40)]
        assert kinds.count(ConnectionKind.CONTROL) == 20
        assert kinds.count(ConnectionKind.PAIRING) == 20
    finally:
        await asyncio.gather(*(c.close() for c in conns))


# =================================================================================================
# R: server từ chối
# =================================================================================================


async def assert_rejected(h, peer, within: float = 2.0) -> float:
    """Server đóng kết nối, không gửi gì ngoài hello (đã đọc trước), không gọi on_connection."""
    elapsed, data = await peer.expect_closed(within)
    assert data == b"", f"server gửi thêm {len(data)} byte trước khi đóng (protocol §2.1)"
    await h.wait_idle()
    assert h.calls == 0
    assert h.listener.connection_count == 0
    return elapsed


# Mỗi kịch bản: client xấu làm đúng một việc sai, trả về RawPeer (đã đọc hello server nếu có).


async def sc_anon_control_open(env):
    return await raw_opening(env.raw_client, env.h, None, wire_hello(env.ids.anon), {"type": "control_open"})


async def sc_anon_data_open(env):
    return await raw_opening(env.raw_client, env.h, None, wire_hello(env.ids.anon), DATA_OPEN)


async def sc_trusted_pair_request(env):
    return await raw_opening(
        env.raw_client, env.h, env.ids.b, wire_hello(env.ids.b), to_dict(pair_request(env.ids.b))
    )


async def sc_trusted_data_open(env):
    return await raw_opening(env.raw_client, env.h, env.ids.b, wire_hello(env.ids.b), DATA_OPEN)


async def sc_hello_device_id_not_cn(env):
    return await raw_opening(
        env.raw_client, env.h, env.ids.b, wire_hello(env.ids.x), {"type": "control_open"}
    )


async def sc_hello_protocol_2_anon(env):
    return await raw_opening(
        env.raw_client, env.h, None, wire_hello(env.ids.anon, protocol=2), to_dict(pair_request(env.ids.anon))
    )


async def sc_hello_protocol_2_trusted(env):
    return await raw_opening(
        env.raw_client, env.h, env.ids.b, wire_hello(env.ids.b, protocol=2), {"type": "control_open"}
    )


async def sc_oversized_hello_header(env):
    peer = await env.raw_client(env.h.port, None)
    await peer.read_hello()
    peer.send_bytes(HEADER.pack(MAX_PAIRING_MESSAGE_SIZE + 1))  # chỉ header, không có thân
    await peer.drain()
    return peer


async def sc_oversized_opening_header(env):
    peer = await raw_opening(env.raw_client, env.h, env.ids.b, wire_hello(env.ids.b), None)
    peer.send_bytes(HEADER.pack(MAX_PAIRING_MESSAGE_SIZE + 1))
    await peer.drain()
    return peer


async def sc_control_open_instead_of_hello(env):
    peer = await env.raw_client(env.h.port, env.ids.b)
    await peer.read_hello()
    peer.send_dict({"type": "control_open"})
    peer.send_dict({"type": "control_open"})
    await peer.drain()
    return peer


async def sc_garbage_instead_of_hello(env):
    peer = await env.raw_client(env.h.port, None)
    await peer.read_hello()
    peer.send_bytes(frame(b"\xff\xfe{]\x00garbage"))
    await peer.drain()
    return peer


async def sc_json_array_instead_of_hello(env):
    peer = await env.raw_client(env.h.port, None)
    await peer.read_hello()
    peer.send_bytes(frame(b"[1, 2, 3]"))
    await peer.drain()
    return peer


async def sc_json_string_instead_of_hello(env):
    peer = await env.raw_client(env.h.port, None)
    await peer.read_hello()
    peer.send_bytes(frame(json.dumps("hello").encode()))
    await peer.drain()
    return peer


async def sc_second_hello_anon(env):
    return await raw_opening(env.raw_client, env.h, None, wire_hello(env.ids.anon), wire_hello(env.ids.anon))


async def sc_second_hello_trusted(env):
    return await raw_opening(env.raw_client, env.h, env.ids.b, wire_hello(env.ids.b), wire_hello(env.ids.b))


async def sc_name_with_bidi_override(env):
    return await raw_opening(
        env.raw_client,
        env.h,
        None,
        wire_hello(env.ids.anon, name="PC‮Nha"),
        to_dict(pair_request(env.ids.anon)),
    )


IMMEDIATE_REJECTIONS = {
    "R2-anon-control_open": sc_anon_control_open,
    "R3-anon-data_open": sc_anon_data_open,
    "R4-trusted-pair_request": sc_trusted_pair_request,
    "R5-trusted-data_open": sc_trusted_data_open,
    "R6-hello-device_id-not-CN": sc_hello_device_id_not_cn,
    "R7-protocol-2-anon": sc_hello_protocol_2_anon,
    "R7-protocol-2-trusted": sc_hello_protocol_2_trusted,
    "R11-hello-header-16385": sc_oversized_hello_header,
    "R11-opening-header-16385": sc_oversized_opening_header,
    "R12-control_open-first": sc_control_open_instead_of_hello,
    "R12-garbage-first": sc_garbage_instead_of_hello,
    "R12-json-array-first": sc_json_array_instead_of_hello,
    "R12-json-string-first": sc_json_string_instead_of_hello,
    "R13-second-hello-anon": sc_second_hello_anon,
    "R13-second-hello-trusted": sc_second_hello_trusted,
    "R14-name-U+202E": sc_name_with_bidi_override,
}


@pytest.mark.integration
@pytest.mark.parametrize("case", list(IMMEDIATE_REJECTIONS))
async def test_server_closes_bad_opening_immediately(slow_opening, listener_factory, raw_client, ids, case):
    """Với OPENING_TIMEOUT = 5 s, đóng trong < 1 s nghĩa là server từ chối ngay, không chờ hết giờ."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    env = SimpleNamespace(h=h, ids=ids, raw_client=raw_client)
    peer = await IMMEDIATE_REJECTIONS[case](env)
    elapsed = await assert_rejected(h, peer, within=3.0)
    assert elapsed < 1.0, f"đóng sau {elapsed:.2f} s: có vẻ chờ hết giờ thay vì từ chối ngay"


@pytest.mark.integration
async def test_unknown_client_cert_is_rejected(fast, listener_factory, ids):
    """R1: chứng chỉ lạ → bắt tay TLS 1.3 xong phía client, rồi thất bại khi đọc hello → Rejected."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    with pytest.raises(Rejected) as exc:
        await b_control(h, ids, identity=ids.x)
    assert exc.value.address == LOCAL
    await h.wait_idle()
    assert h.calls == 0


@pytest.mark.integration
async def test_silence_after_handshake_times_out(fast, listener_factory, raw_client, ids):
    """R8: im lặng sau bắt tay → đóng sau ≈ OPENING_TIMEOUT (0,5 s)."""
    h = await listener_factory(ids.server)
    peer = await raw_client(h.port, None)
    await peer.read_hello()
    await assert_rejected(h, peer, within=5.0)
    total = time.monotonic() - peer.started
    assert 0.4 <= total <= 0.5 + 2.5


@pytest.mark.integration
async def test_silent_tcp_without_tls_times_out(fast, listener_factory, raw_client, ids):
    """R9: TCP thô không gửi byte nào → đóng sau ≈ OPENING_TIMEOUT."""
    h = await listener_factory(ids.server)
    peer = await raw_client(h.port, use_tls=False)
    _, data = await peer.expect_closed(5.0)
    assert data == b""
    total = time.monotonic() - peer.started
    assert 0.4 <= total <= 0.5 + 2.5
    await h.wait_idle()
    assert h.calls == 0


@pytest.mark.integration
async def test_slowloris_deadline_is_not_extended(fast, monkeypatch, listener_factory, raw_client, ids):
    """R10: hello gửi 1 byte mỗi 100 ms → đóng ở ≈ 1 s tính từ lúc kết nối, không gia hạn."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 1.0)
    h = await listener_factory(ids.server)
    peer = await raw_client(h.port, None)
    await peer.read_hello()
    payload = encode_message(wire_hello(ids.anon)) + encode_message(to_dict(pair_request(ids.anon)))

    async def drip():
        for i in range(len(payload)):
            peer.send_bytes(payload[i : i + 1])
            await peer.drain()
            await asyncio.sleep(0.1)

    dripper = asyncio.create_task(drip())
    try:
        await peer.expect_closed(6.0)
    finally:
        await cancel_wait(dripper)
    total = time.monotonic() - peer.started
    assert 0.8 <= total <= 1.0 + 2.5
    await h.wait_idle()
    assert h.calls == 0


# --- R15: fuzz message mở đầu ------------------------------------------------------------------


KNOWN_TYPES = [
    "hello",
    "pair_request",
    "pair_commit",
    "pair_nonce",
    "pair_reveal",
    "pair_challenge",  # type của luồng cũ, nay lạ
    "pair_confirm",
    "pair_abort",
    "control_open",
    "data_open",
    "ping",
    "pong",
    "offer",
]
FUZZ_FIELDS = [
    "protocol",
    "device_id",
    "name",
    "app_version",
    "os",
    "cert",
    "commitment",
    "nonce",
    "reason",
    "t",
    "transfer_id",
    "file_index",
    "resume_seq",
    "x",
]
FAKE_CERT = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"
fuzz_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=40)
    | st.sampled_from([HEX64, FAKE_CERT, "0123456789abcdef" * 2, "user", 1]),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=5), children, max_size=3)
    ),
    max_leaves=8,
)
fuzz_dicts = st.builds(
    lambda t, extra: {"type": t, **extra},
    st.sampled_from(KNOWN_TYPES) | st.text(max_size=12),
    st.dictionaries(st.sampled_from(FUZZ_FIELDS), fuzz_values, max_size=4),
)
valid_openings = st.sampled_from(
    [
        {"type": "pair_request", "cert": FAKE_CERT},
        {"type": "pair_request", "cert": FAKE_CERT, "x": [1]},
        # `commitment` của luồng cũ giờ là trường lạ, bị bỏ qua.
        {"type": "pair_request", "cert": FAKE_CERT, "commitment": HEX64},
        {"type": "control_open"},
        {"type": "control_open", "future": True},
    ]
)


def _json_body(d: dict) -> bytes:
    return json.dumps(d, ensure_ascii=False).encode("utf-8", "surrogatepass")


fuzz_bodies = (
    fuzz_dicts.map(_json_body) | valid_openings.map(_json_body) | st.binary(max_size=MAX_PAIRING_MESSAGE_SIZE)
)


def _expected_kind(body: bytes, trusted_client: bool):
    """Oracle theo đặc tả: chỉ pair_request hợp lệ (ẩn danh) hoặc control_open (tin cậy) được giao."""
    try:
        msg = parse_message(decode_body(body))
    except (FramingError, MessageError):
        return None
    if isinstance(msg, PairRequest) and not trusted_client:
        return ConnectionKind.PAIRING
    if isinstance(msg, ControlOpen) and trusted_client:
        return ConnectionKind.CONTROL
    return None


async def _fuzz_one(helpers, server_id, client_id, body: bytes, trusted_client: bool) -> None:
    before = asyncio.all_tasks()
    accepted = []

    async def on_conn(conn):
        accepted.append(conn)

    listener = Listener(
        server_id,
        helpers.make_hello(server_id, "Server"),
        [client_id.cert_pem()],
        lambda fp: fp == client_id.fingerprint,
        on_conn,
    )
    port = await listener.start(LOCAL, 0)
    peers = []
    try:
        peer = await helpers.RawPeer.connect(port, client_id if trusted_client else None)
        peers.append(peer)
        await peer.read_hello()
        peer.send_dict(helpers.hello_dict(client_id))
        peer.send_bytes(frame(body))
        await peer.drain()
        expected = _expected_kind(body, trusted_client)
        if expected is None:
            _, data = await peer.expect_closed(2.0)
            assert data == b""
            assert accepted == []
        else:
            await helpers.wait_until(lambda: len(accepted) == 1, 2.0, "kết nối hợp lệ được giao")
            assert accepted[0].kind is expected
        await helpers.wait_until(lambda: listener.opening_count == 0, 2.0, "opening_count về 0")

        # Listener vẫn sống và vẫn nhận kết nối hợp lệ.
        n = len(accepted)
        conn = await open_pairing([LOCAL], port, helpers.make_hello(client_id))
        try:
            await conn.send(pair_request(client_id))
            await helpers.wait_until(lambda: len(accepted) == n + 1, 2.0, "kết nối sau fuzz được giao")
        finally:
            await conn.close()
    finally:
        for p in peers:
            p.abort()
        await listener.close()
    await asyncio.sleep(0.05)
    leftover = [t for t in asyncio.all_tasks() if t not in before and t is not asyncio.current_task()]
    assert not leftover, f"task còn sót: {[t.get_name() for t in leftover]}"


@pytest.fixture(scope="module")
def fuzz_ids(tmp_path_factory):
    base = tmp_path_factory.mktemp("fuzz-ids")
    return load_or_create_identity(base / "server"), load_or_create_identity(base / "client")


@pytest.mark.integration
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(body=fuzz_bodies, trusted_client=st.booleans())
def test_fuzz_opening_message_never_crashes_listener(net_helpers, fuzz_ids, body, trusted_client):
    server_id, client_id = fuzz_ids
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(connection, "OPENING_TIMEOUT", 3.0)
        mp.setattr(connection, "CLOSE_TIMEOUT", 0.5)
        asyncio.run(_fuzz_one(net_helpers, server_id, client_id, body, trusted_client))


# =================================================================================================
# L: giới hạn kết nối đang mở đầu (Q2) và kết nối ẩn danh
# =================================================================================================


async def open_silent_tcp(raw_client, h, n: int):
    return [await raw_client(h.port, use_tls=False) for _ in range(n)]


@pytest.mark.integration
async def test_fifth_opening_from_same_ip_is_refused(fast, monkeypatch, listener_factory, raw_client, ids):
    """L1: 4 kết nối đang mở đầu mỗi IP; cái thứ 5 bị đóng ngay; trả chỗ khi một cái đóng."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    h = await listener_factory(ids.server)
    peers = await open_silent_tcp(raw_client, h, 4)
    await wait_until(lambda: h.listener.opening_count == 4, 2.0, "4 kết nối đang mở đầu")

    fifth = await raw_client(h.port, use_tls=False)
    elapsed, _ = await fifth.expect_closed(2.0)
    assert elapsed < 0.5
    for p in peers:
        assert await p.stays_open(0.05)

    peers[0].abort()
    await wait_until(lambda: h.listener.opening_count == 3, 2.0, "trả chỗ sau khi đóng một kết nối")
    sixth = await raw_client(h.port, use_tls=False)
    assert await sixth.stays_open(0.5)
    assert h.listener.opening_count == 4


@pytest.mark.integration
async def test_total_opening_limit(fast, monkeypatch, listener_factory, raw_client, ids):
    """L2: MAX_OPENING_PER_IP = 100, MAX_OPENING_TOTAL = 3 → kết nối thứ 4 bị đóng ngay."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    monkeypatch.setattr(connection, "MAX_OPENING_PER_IP", 100)
    monkeypatch.setattr(connection, "MAX_OPENING_TOTAL", 3)
    h = await listener_factory(ids.server)
    await open_silent_tcp(raw_client, h, 3)
    await wait_until(lambda: h.listener.opening_count == 3, 2.0, "3 kết nối đang mở đầu")
    fourth = await raw_client(h.port, use_tls=False)
    elapsed, _ = await fourth.expect_closed(2.0)
    assert elapsed < 0.5


@pytest.mark.integration
async def test_delivered_connections_do_not_count_toward_opening_limit(
    fast, monkeypatch, listener_factory, raw_client, ids
):
    """L3: 4 kết nối điều khiển đã giao + 4 kết nối thô im lặng: cả 4 kết nối thô đều được nhận."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    h = await listener_factory(ids.server, trusted=[ids.b])
    conns = [await b_control(h, ids) for _ in range(4)]
    try:
        await wait_until(lambda: h.calls == 4, 2.0, "4 kết nối điều khiển được giao")
        assert h.listener.connection_count == 4
        assert h.listener.opening_count == 0
        peers = await open_silent_tcp(raw_client, h, 4)
        for p in peers:
            assert await p.stays_open(0.1)
        await wait_until(lambda: h.listener.opening_count == 4, 2.0, "4 kết nối thô đang mở đầu")
    finally:
        await asyncio.gather(*(c.close() for c in conns))


@pytest.mark.integration
async def test_one_waiting_anonymous_connection_per_ip(fast, monkeypatch, listener_factory, raw_client, ids):
    """L4: ẩn danh chờ vào phiên 1/IP; kết nối tin cậy không bị ảnh hưởng; giao xong thì trả chỗ."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    h = await listener_factory(ids.server, trusted=[ids.b])
    anon1 = await raw_opening(raw_client, h, None, wire_hello(ids.anon), None)

    with pytest.raises(Rejected):
        await anon_pairing(h, ids)
    over = await raw_client(h.port, None)
    _, data = await over.expect_closed(2.0)
    assert data == b"", "kết nối ẩn danh vượt giới hạn không được nhận hello (mục 3.1 bước 4)"

    control = await b_control(h, ids)
    try:
        anon1.send_dict(to_dict(pair_request(ids.anon)))
        await anon1.drain()
        await wait_until(lambda: h.calls == 2, 2.0, "anon1 được giao sau khi gửi pair_request")
        second = await anon_pairing(h, ids)
        await second.close()
    finally:
        await control.close()


@pytest.mark.integration
async def test_anonymous_slot_is_freed_when_waiting_client_leaves(
    fast, monkeypatch, listener_factory, raw_client, ids
):
    """L4 (ca riêng): anon1 đóng trước khi được giao cũng trả chỗ."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    h = await listener_factory(ids.server)
    anon1 = await raw_opening(raw_client, h, None, wire_hello(ids.anon), None)
    with pytest.raises(Rejected):
        await anon_pairing(h, ids)
    anon1.abort()
    await h.wait_idle()
    conn = await anon_pairing(h, ids)
    await conn.close()
    assert h.calls == 0


@pytest.mark.integration
async def test_total_anonymous_limit(fast, monkeypatch, listener_factory, raw_client, ids):
    """L5: MAX_ANONYMOUS_PER_IP = 100, MAX_ANONYMOUS_TOTAL = 2 → kết nối ẩn danh thứ 3 → Rejected."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    monkeypatch.setattr(connection, "MAX_ANONYMOUS_PER_IP", 100)
    monkeypatch.setattr(connection, "MAX_ANONYMOUS_TOTAL", 2)
    h = await listener_factory(ids.server)
    await raw_opening(raw_client, h, None, wire_hello(ids.anon), None)
    await raw_opening(raw_client, h, None, wire_hello(ids.anon), None)
    with pytest.raises(Rejected):
        await anon_pairing(h, ids)


async def sc_unknown_cert(env):
    with pytest.raises(Rejected):
        await b_control(env.h, env.ids, identity=env.ids.x)


async def sc_silent_after_handshake(env):
    peer = await env.raw_client(env.h.port, None)
    await peer.read_hello()
    return peer


async def sc_silent_tcp(env):
    return await env.raw_client(env.h.port, use_tls=False)


async def sc_slowloris(env):
    peer = await env.raw_client(env.h.port, None)
    await peer.read_hello()
    peer.send_bytes(encode_message(wire_hello(env.ids.anon))[:3])
    await peer.drain()
    return peer


def assert_no_counter_entries(listener) -> None:
    """Bất biến mục 3.5 (white-box): mọi bộ đếm về 0 và dict theo IP không giữ mục đếm bằng 0."""
    assert listener._opening.per_ip == {}
    assert listener._opening.total == 0
    assert listener._anonymous.per_ip == {}
    assert listener._anonymous.total == 0


ALL_REJECTIONS = {
    "R1-unknown-cert": sc_unknown_cert,
    **IMMEDIATE_REJECTIONS,
    "R8-silent-after-handshake": sc_silent_after_handshake,
    "R9-silent-tcp": sc_silent_tcp,
    "R10-partial-hello": sc_slowloris,
}


@pytest.mark.integration
@pytest.mark.parametrize("case", list(ALL_REJECTIONS))
async def test_counters_are_released_after_every_rejection(
    fast, monkeypatch, listener_factory, raw_client, ids, case
):
    """L6: sau mỗi lý do từ chối, opening_count về 0 và Listener vẫn nhận đủ 4 kết nối mới."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 1.0)
    h = await listener_factory(ids.server, trusted=[ids.b])
    env = SimpleNamespace(h=h, ids=ids, raw_client=raw_client)
    peer = await ALL_REJECTIONS[case](env)
    if peer is not None:
        await peer.expect_closed(4.0)
        peer.abort()
    await h.wait_idle()
    assert h.calls == 0
    assert_no_counter_entries(h.listener)

    # Chỗ ẩn danh cũng đã được trả.
    conn = await anon_pairing(h, ids)
    await conn.close()
    await h.wait_idle()
    assert_no_counter_entries(h.listener)

    fresh = await open_silent_tcp(raw_client, h, 4)
    for p in fresh:
        assert await p.stays_open(0.1)
    await wait_until(lambda: h.listener.opening_count == 4, 2.0, "4 kết nối mới đang mở đầu")


# =================================================================================================
# T: đổi danh sách tin cậy
# =================================================================================================


@pytest.mark.integration
async def test_update_trusted_takes_effect_without_reopening(fast, listener_factory, ids):
    """T1."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    port = h.port
    old = await b_control(h, ids)
    try:
        old_server = await h.next()
        h.listener.update_trusted([])
        h.trusted_fps.clear()

        with pytest.raises(Rejected):
            await b_control(h, ids)
        assert h.listener.port == port

        # Kết nối cũ vẫn dùng được: Listener không tự đóng nó.
        await old.send(Ping(t=1))
        assert await asyncio.wait_for(old_server.receive(), 2) == Ping(t=1)
        await old_server.send(Pong(t=1))
        assert await asyncio.wait_for(old.receive(), 2) == Pong(t=1)
        assert h.calls == 1
    finally:
        await old.close()


@pytest.mark.integration
async def test_unpair_during_handshake_is_caught_at_control_open(
    slow_opening, listener_factory, raw_client, ids
):
    """T2: bắt tay xong bằng context cũ, rồi bị hủy ghép đôi, rồi mới gửi control_open → bị đóng."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer = await raw_opening(raw_client, h, ids.b, wire_hello(ids.b), None)
    await asyncio.sleep(0.1)  # server đã đọc hello và đang chờ message mở đầu
    h.listener.update_trusted([])
    h.trusted_fps.clear()
    peer.send_dict({"type": "control_open"})
    await peer.drain()
    await assert_rejected(h, peer)


@pytest.mark.integration
async def test_is_trusted_raising_counts_as_untrusted(slow_opening, listener_factory, raw_client, ids):
    """Mục 3.1 bước 7: `is_trusted` raise → coi như False."""

    def broken(fp):
        raise RuntimeError("kho tin cậy hỏng")

    h = await listener_factory(ids.server, trusted=[ids.b], is_trusted=broken)
    peer = await raw_opening(raw_client, h, ids.b, wire_hello(ids.b), {"type": "control_open"})
    await assert_rejected(h, peer)
    # Listener vẫn sống.
    conn = await anon_pairing(h, ids)
    await conn.close()


@pytest.mark.integration
async def test_newly_trusted_device_connects_without_restart(fast, listener_factory, ids):
    """T3."""
    h = await listener_factory(ids.server, trusted=[])
    port = h.port
    with pytest.raises(Rejected):
        await b_control(h, ids)
    h.listener.update_trusted([ids.b.cert_pem()])
    h.trusted_fps.add(ids.b.fingerprint)
    conn = await b_control(h, ids)
    try:
        assert (await h.next()).peer.fingerprint == ids.b.fingerprint
        assert h.listener.port == port
    finally:
        await conn.close()


@pytest.mark.integration
async def test_tls_session_is_never_resumed(fast, listener_factory, ids):
    """T4: không có session ticket, nên session cũ không đưa máy vừa bị hủy ghép đôi vào lại."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    port = h.port
    ctx = tls.client_context(ids.b)

    def first():
        s = sync_connect(port, ctx)
        try:
            hello = sync_read_frame(s)
            return s.session, hello
        finally:
            s.close()

    def again():
        s = sync_connect(port, ctx, session=s1)
        try:
            return s.session_reused, sync_read_frame(s)
        finally:
            s.close()

    def after_unpair():
        try:
            s = sync_connect(port, ctx, session=s1)
        except (ssl.SSLError, OSError):
            return "handshake-failed"
        try:
            reused = s.session_reused
            try:
                msg = sync_read_frame(s)
            except (ssl.SSLError, OSError):
                return "closed" if not reused else "reused-then-closed"
            if msg is None:
                return "eof" if not reused else "reused-then-eof"
            return msg
        finally:
            s.close()

    s1, hello1 = await asyncio.to_thread(first)
    assert hello1 is not None and hello1["type"] == "hello"
    reused, hello2 = await asyncio.to_thread(again)
    assert reused is False
    assert hello2 is not None and hello2["type"] == "hello"

    h.listener.update_trusted([])
    h.trusted_fps.clear()
    result = await asyncio.to_thread(after_unpair)
    assert result in ("handshake-failed", "closed", "eof"), f"máy đã hủy ghép đôi vẫn đọc được: {result!r}"
    await h.wait_idle()
    assert h.calls == 0


@pytest.mark.integration
@pytest.mark.parametrize("bad_pem", BAD_PEMS, ids=BAD_PEM_IDS)
async def test_bad_update_keeps_old_context(fast, listener_factory, ids, bad_pem):
    """T5."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    with pytest.raises(PEM_ERRORS):
        h.listener.update_trusted([bad_pem])
    conn = await b_control(h, ids)
    try:
        assert (await h.next()).peer.fingerprint == ids.b.fingerprint
    finally:
        await conn.close()


@pytest.mark.integration
async def test_update_trusted_after_close_is_noop(fast, listener_factory, ids):
    h = await listener_factory(ids.server)
    await h.listener.close()
    h.listener.update_trusted([ids.b.cert_pem()])


# =================================================================================================
# C: phía client
# =================================================================================================


async def _hold(server, reader, writer):
    """Kịch bản server giả: nhận kết nối, không gửi gì, giữ tới khi bị đóng."""
    await server.record_until_closed(reader, timeout=30)


def send_then_hold(data: bytes):
    async def script(server, reader, writer):
        writer.write(data)
        await writer.drain()
        await server.record_until_closed(reader)

    return script


async def _open_kind(kind: str, port: int, ids, expected: str | None = None):
    if kind == "pairing":
        return await open_pairing([LOCAL], port, hello_of(ids.anon))
    return await open_control([LOCAL], port, ids.b, hello_of(ids.b), expected or ids.server.fingerprint)


@pytest.mark.integration
async def test_wrong_fingerprint_sends_nothing(slow_opening, fake_server, ids):
    """C1: sai fingerprint → IdentityMismatch, và client không gửi byte nào cho máy chưa xác minh."""

    async def record(server, reader, writer):
        await server.record_until_closed(reader, timeout=5)

    server = await fake_server(ids.server, record, trusted_certs=[ids.b.cert_pem()])
    with pytest.raises(IdentityMismatch) as exc:
        await open_control([LOCAL], server.port, ids.b, hello_of(ids.b), ids.x.fingerprint)
    assert exc.value.actual_fingerprint == ids.server.fingerprint
    assert exc.value.address == LOCAL
    await wait_until(lambda: not server._tasks, 3.0, "server giả thấy client đóng")
    assert server.connections == 1, "server giả không thấy kết nối nào: phép kiểm vô nghĩa"
    assert bytes(server.received) == b""


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["pairing", "control"])
async def test_server_hello_device_id_must_match_cn(slow_opening, fake_server, ids, kind):
    """C2."""
    server = await fake_server(
        ids.server, send_then_hold(encode_message(wire_hello(ids.x))), trusted_certs=[ids.b.cert_pem()]
    )
    start = time.monotonic()
    with pytest.raises(IdentityMismatch):
        await _open_kind(kind, server.port, ids)
    assert time.monotonic() - start < 2.0


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["pairing", "control"])
async def test_newer_protocol_is_version_mismatch(slow_opening, fake_server, ids, kind):
    """C3: hello bản mới (protocol 2, có trường lạ) → VersionMismatch, không phải lỗi parse."""
    hello = wire_hello(ids.server, protocol=2, capabilities={"quic": True}, name="")
    server = await fake_server(
        ids.server, send_then_hold(encode_message(hello)), trusted_certs=[ids.b.cert_pem()]
    )
    with pytest.raises(VersionMismatch) as exc:
        await _open_kind(kind, server.port, ids)
    assert exc.value.peer_protocol == 2
    assert exc.value.address == LOCAL


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["pairing", "control"])
@pytest.mark.parametrize("how", ["graceful", "abort"])
async def test_close_after_handshake_is_rejected(slow_opening, fake_server, ids, kind, how):
    """C4: server đóng ngay sau bắt tay, không gửi hello → Rejected (không phải Unreachable)."""

    async def close_now(server, reader, writer):
        if how == "abort":
            writer.transport.abort()
        else:
            writer.close()
        await asyncio.sleep(0)

    server = await fake_server(ids.server, close_now, trusted_certs=[ids.b.cert_pem()])
    start = time.monotonic()
    with pytest.raises(Rejected) as exc:
        await _open_kind(kind, server.port, ids)
    assert exc.value.address == LOCAL
    assert time.monotonic() - start < 2.0


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["pairing", "control"])
async def test_closed_port_is_unreachable(fast, ids, kind):
    """C5a."""
    port = free_port()
    with pytest.raises(Unreachable) as exc:
        await _open_kind(kind, port, ids)
    assert exc.value.address == LOCAL


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["pairing", "control"])
async def test_server_that_never_handshakes_is_unreachable(fast, fake_server, ids, kind):
    """C5b: TCP nhận nhưng không bao giờ bắt tay TLS → Unreachable sau ≈ OPENING_TIMEOUT."""
    server = await fake_server(None, _hold, use_tls=False)
    start = time.monotonic()
    with pytest.raises(Unreachable) as exc:
        await _open_kind(kind, server.port, ids)
    elapsed = time.monotonic() - start
    assert 0.4 <= elapsed <= 0.5 + 2.5
    assert exc.value.address == LOCAL


@pytest.mark.integration
@pytest.mark.parametrize(
    "data",
    [
        HEADER.pack(MAX_PAIRING_MESSAGE_SIZE + 1) + b"x" * 64,
        b"HTTP/1.1 400 Bad Request\r\n\r\n",
        frame(b"\xff\xfe\xfd\xfc"),
        frame(b"[1, 2]"),
        encode_message({"type": "ping", "t": 1}),
        encode_message({"type": "hello"}),
    ],
    ids=["header>16KiB", "http", "bad-utf8", "json-array", "ping", "hello-missing-fields"],
)
@pytest.mark.parametrize("kind", ["pairing", "control"])
async def test_non_teleport_server_is_unreachable(slow_opening, fake_server, ids, kind, data):
    """C6: máy kia không nói giao thức Teleport → Unreachable, ngay khi đọc (không chờ hết giờ)."""
    server = await fake_server(ids.server, send_then_hold(data), trusted_certs=[ids.b.cert_pem()])
    start = time.monotonic()
    with pytest.raises(Unreachable):
        await _open_kind(kind, server.port, ids)
    assert time.monotonic() - start < 2.0


@pytest.mark.integration
async def test_dead_address_next_to_good_one_does_not_delay(monkeypatch, listener_factory, ids):
    """C7: OPENING_TIMEOUT để mặc định 10 s; địa chỉ câm không được kéo dài lần mở."""
    monkeypatch.setattr(connection, "CLOSE_TIMEOUT", 0.5)
    assert connection.OPENING_TIMEOUT == 10.0
    h = await listener_factory(ids.server, trusted=[ids.b])
    start = time.monotonic()
    conn = await b_control(h, ids, addresses=(TEST_NET, LOCAL))
    try:
        assert time.monotonic() - start < 2.0
        assert conn.peer.address == LOCAL
        await h.next()
    finally:
        await conn.close()


@pytest.mark.integration
async def test_all_addresses_fail_reports_most_relevant_error(fast, monkeypatch, listener_factory, ids):
    """C8: TEST-NET (Unreachable/treo) + 127.0.0.1 sai fingerprint → IdentityMismatch trong ≤ 2,5 s."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 1.0)
    h = await listener_factory(ids.server, trusted=[ids.b])
    start = time.monotonic()
    with pytest.raises(IdentityMismatch) as exc:
        await b_control(h, ids, expected=ids.x.fingerprint, addresses=(TEST_NET, LOCAL))
    assert time.monotonic() - start <= 2.5
    assert exc.value.address == LOCAL
    assert exc.value.actual_fingerprint == ids.server.fingerprint
    assert h.calls == 0


@pytest.mark.integration
async def test_cancelling_open_leaves_nothing_behind(fast, monkeypatch, fake_server, ids):
    """C9: hủy task open_control giữa chừng → không còn task hay socket (kiểm bởi no_leaks)."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 10.0)
    server = await fake_server(None, _hold, use_tls=False)
    task = asyncio.create_task(
        open_control([TEST_NET, LOCAL], server.port, ids.b, hello_of(ids.b), ids.server.fingerprint)
    )
    await wait_until(lambda: server.connections == 1, 2.0, "client đã nối TCP tới server câm")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Server câm thấy kết nối của client bị đóng.
    await wait_until(lambda: not server._tasks, 2.0, "socket phía client đã đóng")


# =================================================================================================
# A: API của Connection
# =================================================================================================


@pytest.mark.integration
@pytest.mark.parametrize(
    "msg",
    [
        hello_of(SimpleNamespace(device_id="0" * 32)),
        ControlOpen(),
        PairCommit(commitment=HEX64),
        PairNonce(nonce=HEX64),
        PairReveal(nonce=HEX64),
    ],
    ids=lambda m: type(m).__name__,
)
async def test_send_rejects_type_not_allowed_on_control(fast, listener_factory, ids, msg):
    """A1 (CONTROL): ValueError, không có byte nào đi ra."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    conn = await b_control(h, ids)
    try:
        server = await h.next()
        with pytest.raises(ValueError):
            await conn.send(msg)
        with pytest.raises(ValueError):
            await server.send(msg)
        assert not conn.closed
        await conn.send(Ping(t=3))
        assert await asyncio.wait_for(server.receive(), 2) == Ping(t=3)
    finally:
        await conn.close()


@pytest.mark.integration
async def test_send_rejects_pair_request_on_control(fast, listener_factory, ids):
    """A1 (CONTROL): PairRequest."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    conn = await b_control(h, ids)
    try:
        server = await h.next()
        with pytest.raises(ValueError):
            await conn.send(pair_request(ids.b))
        await conn.send(Pong(t=9))
        assert await asyncio.wait_for(server.receive(), 2) == Pong(t=9)
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.parametrize("msg", [Ping(t=1), Pong(t=1), ControlOpen()], ids=lambda m: type(m).__name__)
async def test_send_rejects_type_not_allowed_on_pairing(fast, listener_factory, ids, msg):
    """A1 (PAIRING): ValueError; byte không đi ra, nên pair_request gửi sau vẫn là message mở đầu."""
    h = await listener_factory(ids.server)
    conn = await anon_pairing(h, ids)
    try:
        with pytest.raises(ValueError):
            await conn.send(msg)
        request = pair_request(ids.anon)
        await conn.send(request)
        server = await h.next()
        assert server.opening == request
    finally:
        await conn.close()


@pytest.mark.integration
async def test_receive_on_control_reports_wrong_kind_and_unknown_type(
    fast, listener_factory, raw_client, ids
):
    """A2 (CONTROL)."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    peer.send_dict({"type": "pair_commit", "commitment": HEX64})
    await peer.drain()
    with pytest.raises(WrongConnectionKind):
        await asyncio.wait_for(server.receive(), 2)
    peer.send_dict({"type": "pair_nonce", "nonce": HEX64})
    await peer.drain()
    with pytest.raises(WrongConnectionKind):
        await asyncio.wait_for(server.receive(), 2)
    # Type của luồng cũ không còn tồn tại: là type lạ, không phải sai loại kết nối.
    peer.send_dict({"type": "pair_challenge", "nonce": HEX64})
    await peer.drain()
    with pytest.raises(UnknownMessageType):
        await asyncio.wait_for(server.receive(), 2)
    peer.send_dict({"type": "future_thing", "x": 1})
    await peer.drain()
    with pytest.raises(UnknownMessageType):
        await asyncio.wait_for(server.receive(), 2)
    peer.send_dict({"type": "ping", "t": 5})
    await peer.drain()
    assert await asyncio.wait_for(server.receive(), 2) == Ping(t=5)


@pytest.mark.integration
async def test_receive_on_pairing_enforces_16_kib(fast, listener_factory, raw_client, ids):
    """A2 (PAIRING): header > 16 KiB → FramingError."""
    h = await listener_factory(ids.server)
    peer, server = await raw_pairing(raw_client, h, ids)
    peer.send_bytes(HEADER.pack(MAX_PAIRING_MESSAGE_SIZE + 1))
    await peer.drain()
    with pytest.raises(FramingError):
        await asyncio.wait_for(server.receive(), 2)


@pytest.mark.integration
async def test_receive_on_control_allows_more_than_16_kib(fast, listener_factory, raw_client, ids):
    """CONTROL đọc với MAX_MESSAGE_SIZE: message 20 KiB (trường lạ) vẫn nhận được."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    peer.send_dict({"type": "ping", "t": 1, "pad": "x" * 20_000})
    await peer.drain()
    assert await asyncio.wait_for(server.receive(), 2) == Ping(t=1)


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["pairing", "control"])
async def test_receive_returns_none_on_clean_eof(fast, listener_factory, raw_client, ids, kind):
    """A2: EOF gọn ở ranh giới message → None."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    if kind == "pairing":
        peer, server = await raw_pairing(raw_client, h, ids)
    else:
        peer, server = await raw_control(raw_client, h, ids)
    await peer.close_gracefully()
    assert await asyncio.wait_for(server.receive(), 2) is None


@pytest.mark.integration
async def test_concurrent_receive_is_runtime_error(fast, listener_factory, ids):
    """A3."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    conn = await b_control(h, ids)
    try:
        server = await h.next()
        first = asyncio.create_task(conn.receive())
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError):
            await conn.receive()
        await server.send(Ping(t=11))
        assert await asyncio.wait_for(first, 2) == Ping(t=11)
    finally:
        await conn.close()


@pytest.mark.integration
async def test_use_after_close_and_double_close(fast, listener_factory, ids):
    """A4."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    conn = await b_control(h, ids)
    server = await h.next()

    await conn.close()
    assert conn.closed
    with pytest.raises(ConnectionLostError):
        await conn.send(Ping(t=1))
    with pytest.raises(ConnectionLostError):
        await conn.receive()
    await conn.close()

    assert await asyncio.wait_for(server.receive(), 2) is None
    await asyncio.gather(server.close(), server.close())
    assert server.closed
    with pytest.raises(ConnectionLostError):
        await server.send(Pong(t=1))
    with pytest.raises(ConnectionLostError):
        await server.receive()


@pytest.mark.integration
async def test_close_wakes_pending_receive(fast, listener_factory, ids):
    """Một `receive` đang chờ thì bị close() → ConnectionLostError (kết nối đã đóng)."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    conn = await b_control(h, ids)
    await h.next()
    pending = asyncio.create_task(conn.receive())
    await asyncio.sleep(0.05)
    await conn.close()
    with pytest.raises(ConnectionLostError):
        await asyncio.wait_for(pending, 2)


@pytest.mark.integration
async def test_close_is_bounded_when_peer_never_answers(fast, listener_factory, ids):
    """A5: máy kia không bao giờ đọc nên không trả close_notify → close() ≤ CLOSE_TIMEOUT + 1 s."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    sock = await asyncio.to_thread(sync_open_control, h.port, ids.b)
    try:
        server = await h.next()
        start = time.monotonic()
        await server.close()
        assert time.monotonic() - start <= 0.5 + 1.0
        assert server.closed
    finally:
        sock.close()


@pytest.mark.integration
async def test_send_gives_up_when_peer_never_reads(fast, listener_factory, ids):
    """A6: DRAIN_TIMEOUT = 0,5 s; máy kia không đọc; gửi Ping liên tục → ConnectionLostError ≤ 10 s."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    sock = await asyncio.to_thread(sync_open_control, h.port, ids.b, rcvbuf=4096)
    try:
        server = await h.next()
        start = time.monotonic()
        with pytest.raises(ConnectionLostError):
            async with asyncio.timeout(15):
                while True:
                    await server.send(Ping(t=1))
        assert time.monotonic() - start <= 10.0
        assert server.closed
    finally:
        sock.close()


@pytest.mark.integration
async def test_concurrent_sends_do_not_interleave(fast, listener_factory, raw_client, ids):
    """Mục 2.3: send chạy dưới lock, nên các lần gửi đồng thời ra đúng từng khung nguyên vẹn."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    await asyncio.gather(*(server.send(Ping(t=i)) for i in range(200)))
    got = [await peer.read() for _ in range(200)]
    assert sorted(m["t"] for m in got) == list(range(200))


# =================================================================================================
# K-: ControlChannel (ping_interval = 0.2, dead_after = 0.6)
# =================================================================================================


def start_channel(conn, received: list, **kwargs):
    kwargs.setdefault("ping_interval", PI)
    kwargs.setdefault("dead_after", DA)
    channel = ControlChannel(conn, **kwargs)
    task = asyncio.create_task(channel.run(received.append))
    return channel, task


@pytest.mark.integration
async def test_channel_answers_ping_with_same_t(fast, listener_factory, raw_client, ids):
    """K-1."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    received: list = []
    channel, task = start_channel(server, received)
    try:
        assert channel.connection is server
        peer.send_dict({"type": "ping", "t": 12345})
        await peer.drain()
        pong = await read_until_type(peer, "pong")
        assert pong["t"] == 12345
        await wait_until(lambda: Ping(t=12345) in received, 1.0, "on_message nhận Ping")
    finally:
        await cancel_wait(task)


@pytest.mark.integration
async def test_channel_dies_after_silence(fast, listener_factory, raw_client, ids):
    """K-2: máy kia giữ kết nối nhưng im lặng → KEEPALIVE trong 0,48–3 s."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    start = time.monotonic()
    _, task = start_channel(server, [])
    reason = await asyncio.wait_for(task, 5)
    elapsed = time.monotonic() - start
    assert reason is CloseReason.KEEPALIVE
    assert 0.48 <= elapsed <= 3.0
    assert server.closed
    await peer.expect_closed(2.0)


@pytest.mark.integration
async def test_two_channels_keep_each_other_alive(fast, listener_factory, ids):
    """K-3: hai ControlChannel ở hai đầu chạy 2 s, không bên nào kết thúc."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    client = await b_control(h, ids)
    server = await h.next()
    got_client: list = []
    got_server: list = []
    _, t1 = start_channel(client, got_client)
    _, t2 = start_channel(server, got_server)
    try:
        done, _ = await asyncio.wait({t1, t2}, timeout=2.0)
        assert not done, f"kênh kết thúc sớm: {[t.result() for t in done if not t.exception()]}"
        pings = sum(isinstance(m, Ping) for m in got_client + got_server)
        assert pings >= 3
        # Mỗi bên đều thấy lưu lượng keepalive của bên kia.
        assert len(got_client) >= 3
        assert len(got_server) >= 3
    finally:
        await cancel_wait(t1, t2)


@pytest.mark.integration
async def test_partial_message_counts_as_alive(fast, listener_factory, raw_client, ids):
    """K-4: một message lớn tới từng mẩu 1 KB mỗi 20 ms, lâu hơn dead_after nhiều lần → không chết.

    Độ dài lần gửi được khẳng định bằng cận dưới lý thuyết (số mẩu × khoảng nghỉ), không bằng
    đồng hồ thực đo: `asyncio.sleep` không bao giờ ngủ ít hơn khoảng đã hẹn.
    """
    piece, gap = 1024, 0.02
    data = encode_message({"type": "ping", "t": 1, "pad": "x" * 102_400})
    pieces = -(-len(data) // piece)
    assert pieces >= 100
    assert pieces * gap > 2 * DA, "phép thử quá ngắn: không vượt được 2 × dead_after"

    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    received: list = []
    _, task = start_channel(server, received)
    try:
        start = time.monotonic()
        for i in range(0, len(data), piece):
            assert not task.done(), f"kênh kết thúc sau {time.monotonic() - start:.2f} s: {task.result()!r}"
            peer.send_bytes(data[i : i + piece])
            await peer.drain()
            await asyncio.sleep(gap)
        await wait_until(lambda: Ping(t=1) in received, 0.5, "on_message nhận Ping(t=1)")
        assert not task.done()
    finally:
        await cancel_wait(task)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("peer_closed", CloseReason.PEER_CLOSED),
        ("garbage", CloseReason.PROTOCOL_ERROR),
        ("oversized", CloseReason.PROTOCOL_ERROR),
        ("pair_commit", CloseReason.PROTOCOL_ERROR),
        ("ping_missing_t", CloseReason.PROTOCOL_ERROR),
        ("local_close", CloseReason.LOCAL),
    ],
)
async def test_channel_close_reasons(fast, listener_factory, raw_client, ids, case, expected):
    """K-5."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    _, task = start_channel(server, [], dead_after=5.0)
    try:
        await asyncio.sleep(0.05)
        if case == "peer_closed":
            await peer.close_gracefully()
        elif case == "garbage":
            peer.send_bytes(frame(b"\xff\xfe\xfd\xfc\xfb"))
        elif case == "oversized":
            peer.send_bytes(HEADER.pack(0xFFFFFFFF))
        elif case == "pair_commit":
            peer.send_dict({"type": "pair_commit", "commitment": HEX64})
        elif case == "ping_missing_t":
            peer.send_dict({"type": "ping"})
        else:
            await server.close()
        await peer.drain()
        assert await asyncio.wait_for(task, 3) is expected
        assert server.closed
    finally:
        await cancel_wait(task)


@pytest.mark.integration
async def test_channel_ignores_unknown_type(fast, listener_factory, raw_client, ids):
    """K-5: `type` lạ → bỏ qua, kênh chạy tiếp và vẫn trả lời ping."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    received: list = []
    _, task = start_channel(server, received, dead_after=5.0)
    try:
        peer.send_dict({"type": "offer_v9", "files": [1, 2]})
        peer.send_dict({"type": "ping", "t": 77})
        await peer.drain()
        assert (await read_until_type(peer, "pong"))["t"] == 77
        assert not task.done()
        assert received == [Ping(t=77)]
    finally:
        await cancel_wait(task)


@pytest.mark.integration
async def test_idle_channel_sends_valid_ping(fast, listener_factory, raw_client, ids):
    """K-6: khi rảnh, máy kia nhận Ping trong ≤ ping_interval + 1 s, 0 ≤ t ≤ 2⁵³ − 1."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    _, task = start_channel(server, [])
    try:
        msg = await peer.read(timeout=PI + 1.0)
        assert msg is not None and msg["type"] == "ping"
        t = msg["t"]
        assert isinstance(t, int) and not isinstance(t, bool)
        assert 0 <= t <= MAX_SAFE_INTEGER
    finally:
        await cancel_wait(task)


@pytest.mark.integration
async def test_app_sends_suppress_pings(fast, listener_factory, raw_client, ids):
    """K-7: app gửi Pong mỗi 0,1 s qua conn.send, ping_interval = 0,3 → máy kia không nhận Ping nào."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    _, task = start_channel(server, [], ping_interval=0.3, dead_after=5.0)

    async def app_traffic():
        while True:
            await server.send(Pong(t=1))
            await asyncio.sleep(0.1)

    sender = asyncio.create_task(app_traffic())
    try:
        msgs = await read_messages_for(peer, 1.0)
        assert [m for m in msgs if m["type"] == "ping"] == []
        assert sum(m["type"] == "pong" for m in msgs) >= 5
        assert not task.done()
    finally:
        await cancel_wait(sender, task)


@pytest.mark.integration
async def test_channel_misuse(fast, listener_factory, raw_client, ids):
    """K-8: run gọi lần hai → RuntimeError; kênh trên kết nối PAIRING / tham số sai → ValueError."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    _, server = await raw_control(raw_client, h, ids)
    with pytest.raises(ValueError):
        ControlChannel(server, ping_interval=1.0, dead_after=1.0)
    with pytest.raises(ValueError):
        ControlChannel(server, ping_interval=0.0, dead_after=1.0)
    channel, task = start_channel(server, [], dead_after=5.0)
    try:
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError):
            await channel.run(lambda m: None)
        assert not task.done()
    finally:
        await cancel_wait(task)

    _, pairing = await raw_pairing(raw_client, h, ids)
    with pytest.raises(ValueError):
        ControlChannel(pairing)


@pytest.mark.integration
async def test_channel_on_closed_connection_returns_local(fast, listener_factory, raw_client, ids):
    """Mục 3.4 bước 1: kết nối đã đóng → run trả LOCAL ngay."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    _, server = await raw_control(raw_client, h, ids)
    await server.close()
    channel = ControlChannel(server, ping_interval=PI, dead_after=DA)
    assert await asyncio.wait_for(channel.run(lambda m: None), 1) is CloseReason.LOCAL


class Boom(Exception):
    pass


@pytest.mark.integration
async def test_on_message_error_closes_and_propagates(fast, listener_factory, raw_client, ids):
    """K-9."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)

    def explode(msg):
        raise Boom(msg)

    channel = ControlChannel(server, ping_interval=PI, dead_after=5.0)
    task = asyncio.create_task(channel.run(explode))
    peer.send_dict({"type": "pong", "t": 7})
    await peer.drain()
    with pytest.raises(Boom):
        await asyncio.wait_for(task, 3)
    assert server.closed
    await peer.expect_closed(2.0)


@pytest.mark.integration
async def test_cancelling_run_closes_connection(fast, listener_factory, raw_client, ids):
    """K-10: hủy run → kết nối đóng, không còn task (no_leaks kiểm)."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    _, task = start_channel(server, [], dead_after=5.0)
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server.closed
    await peer.expect_closed(2.0)


# =================================================================================================
# LC: vòng đời Listener
# =================================================================================================


@pytest.mark.integration
async def test_listener_close_cleans_up_everything(fast, monkeypatch, listener_factory, raw_client, ids):
    """LC1."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    cancelled: list = []
    delivered: list = []

    async def on_connection(conn):
        delivered.append(conn.kind)
        try:
            if conn.kind is ConnectionKind.CONTROL:
                await ControlChannel(conn).run(lambda m: None)
            else:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(conn.kind)
            raise

    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    opening = await raw_client(h.port, use_tls=False)
    pairing = await anon_pairing(h, ids)
    await pairing.send(pair_request(ids.anon))
    control = await b_control(h, ids)
    try:
        await wait_until(lambda: len(delivered) == 2, 2.0, "hai kết nối được giao")
        await wait_until(lambda: h.listener.opening_count == 1, 2.0, "một kết nối đang mở đầu")
        assert h.listener.connection_count == 2

        start = time.monotonic()
        await h.listener.close()
        assert time.monotonic() - start <= 0.5 + 1.0

        leftover = [
            t.get_name() for t in asyncio.all_tasks() if t.get_name().startswith("teleport.connection")
        ]
        assert leftover == []
        assert sorted(cancelled) == sorted([ConnectionKind.CONTROL, ConnectionKind.PAIRING])
        assert h.listener.opening_count == 0
        assert h.listener.connection_count == 0
        await opening.expect_closed(2.0)
        await receive_closed(pairing)
        await receive_closed(control)

        await h.listener.close()
        with pytest.raises(RuntimeError):
            await h.listener.start(LOCAL, 0)
    finally:
        await pairing.close()
        await control.close()


@pytest.mark.integration
async def test_concurrent_listener_close(fast, listener_factory, ids):
    """Mục 2.3: close gọi đồng thời không lỗi."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    conn = await b_control(h, ids)
    try:
        await h.next()
        await asyncio.gather(h.listener.close(), h.listener.close(), h.listener.close())
        assert h.listener.connection_count == 0
    finally:
        await conn.close()


@pytest.mark.integration
async def test_failing_handler_does_not_stop_listener(fast, listener_factory, ids):
    """LC2: on_connection raise → kết nối đó bị đóng; kết nối kế tiếp vẫn được nhận."""
    calls: list = []
    release = asyncio.Event()

    async def on_connection(conn):
        calls.append(conn)
        if len(calls) == 1:
            raise RuntimeError("handler hỏng")
        await release.wait()

    h = await listener_factory(ids.server, on_connection=on_connection)
    first = await anon_pairing(h, ids)
    try:
        await first.send(pair_request(ids.anon))
        await receive_closed(first)
        await wait_until(lambda: h.listener.connection_count == 0, 2.0, "kết nối lỗi được bỏ khỏi tập")
        assert calls[0].closed
    finally:
        await first.close()

    second = await anon_pairing(h, ids)
    try:
        await second.send(pair_request(ids.anon))
        await wait_until(lambda: len(calls) == 2, 2.0, "kết nối thứ hai được giao")
        assert not calls[1].closed
    finally:
        release.set()
        await second.close()


@pytest.mark.integration
async def test_listener_closes_connection_when_handler_returns(fast, listener_factory, ids):
    """LC3: on_connection trả về → Listener đóng kết nối, client thấy EOF."""
    handled: list = []

    async def on_connection(conn):
        handled.append(conn)

    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    conn = await b_control(h, ids)
    try:
        assert await asyncio.wait_for(conn.receive(), 3) is None
        await wait_until(lambda: h.listener.connection_count == 0, 2.0, "kết nối đã bị bỏ khỏi tập")
        assert handled[0].closed
    finally:
        await conn.close()


# =================================================================================================
# Bất biến bổ sung (mục 2.3, 3.1, 3.4, 3.5)
# =================================================================================================


@pytest.mark.integration
async def test_connection_tasks_are_named(slow_opening, listener_factory, raw_client, ids):
    """Mục 3.1 bước 0: task xử lý kết nối có tên bắt đầu bằng `teleport.connection`."""
    h = await listener_factory(ids.server)
    await raw_client(h.port, use_tls=False)
    await wait_until(lambda: h.listener.opening_count == 1, 2.0, "một kết nối đang mở đầu")
    names = [t.get_name() for t in asyncio.all_tasks()]
    assert any(n.startswith("teleport.connection") for n in names), names


@pytest.mark.integration
async def test_no_suspension_between_trust_check_and_handler(slow_opening, listener_factory, ids):
    """Mục 3.1 bước 8: giữa `is_trusted` và phần đồng bộ đầu `on_connection` không có điểm dừng.

    Một callback xếp bằng `call_soon` trong `is_trusted` chỉ chạy được khi vòng lặp được nhường;
    nếu nó đã chạy lúc handler bắt đầu thì có một `await` thật ở giữa.
    """
    loop = asyncio.get_running_loop()
    flag = {"ran": False}
    seen: list = []

    def is_trusted(fp):
        loop.call_soon(flag.__setitem__, "ran", True)
        return fp == ids.b.fingerprint

    async def on_connection(conn):
        seen.append(flag["ran"])

    h = await listener_factory(
        ids.server, trusted=[ids.b], is_trusted=is_trusted, on_connection=on_connection
    )
    conn = await b_control(h, ids)
    try:
        await wait_until(lambda: seen, 2.0, "on_connection được gọi")
        assert seen == [False]
    finally:
        await conn.close()


@pytest.mark.integration
async def test_logs_never_contain_message_content(slow_opening, listener_factory, raw_client, ids, caplog):
    """Mục 2.3 / N9: log chỉ ghi `type`, địa chỉ, lý do; không ghi nội dung message hay PEM."""
    caplog.set_level(logging.DEBUG)
    marker = "BIMAT-7f3a"
    h = await listener_factory(ids.server, trusted=[ids.b])

    # Ghép đôi hợp lệ có tên máy đánh dấu, gửi kèm chứng chỉ PEM.
    conn = await open_pairing([LOCAL], h.port, hello_of(ids.anon, f"PC {marker}"))
    await conn.send(pair_request(ids.anon))
    await h.next()
    await conn.close()
    await h.wait_idle()
    # Bị từ chối: hello bản khác có trường đánh dấu; message mở đầu sai có trường đánh dấu.
    p1 = await raw_opening(raw_client, h, None, wire_hello(ids.anon, protocol=2, note=marker), None)
    await p1.expect_closed(2.0)
    p2 = await raw_opening(raw_client, h, None, wire_hello(ids.anon, f"X {marker}"), {"type": marker})
    await p2.expect_closed(2.0)
    # Trên kênh điều khiển: type lạ mang dữ liệu đánh dấu.
    peer, server = await raw_control(raw_client, h, ids)
    _, task = start_channel(server, [], dead_after=5.0)
    try:
        peer.send_dict({"type": "future_thing", "secret": marker})
        peer.send_dict({"type": "ping", "t": 1})
        await peer.drain()
        await read_until_type(peer, "pong")
    finally:
        await cancel_wait(task)

    assert marker not in caplog.text
    assert "BEGIN CERTIFICATE" not in caplog.text
    assert ids.anon.cert_pem().splitlines()[1] not in caplog.text


@pytest.mark.integration
async def test_duplicate_addresses_are_tried_once(fast, listener_factory, ids):
    """Mục 2.3: địa chỉ trùng được bỏ, không phải lỗi."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    conn = await b_control(h, ids, addresses=(LOCAL, LOCAL, LOCAL))
    try:
        assert conn.peer.address == LOCAL
        await h.next()
        await asyncio.sleep(0.2)
        assert h.calls == 1
    finally:
        await conn.close()


@pytest.mark.integration
async def test_on_message_sees_messages_in_order(fast, listener_factory, raw_client, ids):
    """Mục 3.4 bước 3: mọi message hợp lệ, gồm Pong, tới on_message đúng thứ tự nhận."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    received: list = []
    _, task = start_channel(server, received, dead_after=5.0)
    try:
        sent = [Ping(t=1), Pong(t=2), Ping(t=3), Pong(t=4), Pong(t=5)]
        for m in sent:
            peer.send_dict(to_dict(m))
        await peer.drain()
        await wait_until(lambda: len(received) >= len(sent), 2.0, "on_message nhận đủ")
        assert received == sent
    finally:
        await cancel_wait(task)


@pytest.mark.integration
async def test_ping_reply_is_sent_before_on_message(fast, listener_factory, raw_client, ids):
    """Mục 3.4 bước 3: với Ping, Pong được gửi trước rồi mới gọi on_message.

    on_message raise ngay khi nhận Ping: kênh đóng, nhưng Pong phải đã đi ra trước đó.
    """
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)

    def explode(msg):
        raise Boom(msg)

    task = asyncio.create_task(ControlChannel(server, ping_interval=5.0, dead_after=10.0).run(explode))
    peer.send_dict({"type": "ping", "t": 31})
    await peer.drain()
    with pytest.raises(Boom):
        await asyncio.wait_for(task, 3)
    _, data = await peer.expect_closed(2.0)
    msgs = []
    while data:
        (n,) = HEADER.unpack(data[:4])
        msgs.append(decode_body(data[4 : 4 + n]))
        data = data[4 + n :]
    assert {"type": "pong", "t": 31} in msgs


# =================================================================================================
# Sửa sau review vòng 1 (F1, F3, F4, F5): hủy giữa chừng, lần thử thua, lý do đóng
# =================================================================================================


def frames_in(data: bytes) -> list[dict]:
    """Các khung trọn vẹn trong `data` (phần cuối dở dang bị bỏ qua)."""
    out: list[dict] = []
    while len(data) >= HEADER.size:
        (n,) = HEADER.unpack(data[: HEADER.size])
        if len(data) < HEADER.size + n:
            break
        out.append(decode_body(data[HEADER.size : HEADER.size + n]))
        data = data[HEADER.size + n :]
    return out


def types_in(data: bytes) -> list[str]:
    return [f.get("type") for f in frames_in(bytes(data))]


async def control_pair(side: str, h, ids, raw_client):
    """Kết nối điều khiển phía `side` (client: open_control; server: Listener giao) và hàm
    `other_side_sees_close()` khẳng định máy kia thấy kết nối bị đóng."""
    if side == "server":
        peer, conn = await raw_control(raw_client, h, ids)

        async def other_side_sees_close() -> None:
            await peer.expect_closed(2.0)

        return conn, other_side_sees_close

    conn = await b_control(h, ids)
    server = await h.next()

    async def other_side_sees_close() -> None:
        await receive_closed(server, 2.0)

    return conn, other_side_sees_close


def new_live_tasks(before: set[asyncio.Task]) -> list[str]:
    current = asyncio.current_task()
    return [
        t.get_name() for t in asyncio.all_tasks() if t is not current and t not in before and not t.done()
    ]


# --- F1: run bị hủy lần hai trong lúc dọn --------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("yields", [1, 2, 3, 5])
@pytest.mark.parametrize("side", ["client", "server"])
async def test_cancelling_run_twice_still_closes_connection(
    fast, listener_factory, raw_client, ids, side, yields
):
    """F1 / mục 3.4 bước 5: hủy `run`, rồi hủy lần nữa khi nó đang dọn → kết nối vẫn đóng.

    Móc keepalive phải được gỡ, máy kia thấy EOF/reset, không task nào sống lâu hơn `run`,
    và gửi tiếp qua kết nối chỉ ra ConnectionLostError.
    """
    h = await listener_factory(ids.server, trusted=[ids.b])
    conn, other_side_sees_close = await control_pair(side, h, ids, raw_client)
    before = asyncio.all_tasks()
    _, task = start_channel(conn, [], dead_after=5.0)
    await asyncio.sleep(0.1)
    assert not task.done()

    task.cancel()
    for _ in range(yields):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)

    assert conn.closed, "run bị hủy hai lần mà kết nối chưa đóng"
    # White-box: móc keepalive đã gỡ (không còn gọi vào Keepalive của một run đã chết).
    assert conn._on_sent is None
    assert conn._on_received is None
    with pytest.raises(ConnectionLostError):
        await conn.send(Ping(t=1))
    await other_side_sees_close()
    await wait_until(lambda: not new_live_tasks(before), 0.5, "task của run đã kết thúc")


@pytest.mark.integration
async def test_cancelling_run_again_while_it_closes_aborts_at_once(fast, monkeypatch, listener_factory, ids):
    """F1: lần hủy thứ hai rơi vào lúc `run` đang đóng gọn (máy kia không bao giờ trả close_notify).

    CLOSE_TIMEOUT = 5 s, nên `run` chỉ kết thúc nhanh nếu lần hủy thứ hai abort transport ngay.
    """
    monkeypatch.setattr(connection, "CLOSE_TIMEOUT", 5.0)
    h = await listener_factory(ids.server, trusted=[ids.b])
    sock = await asyncio.to_thread(sync_open_control, h.port, ids.b)
    try:
        server = await h.next()
        raw = server._writer.get_extra_info("socket")
        _, task = start_channel(server, [], ping_interval=5.0, dead_after=10.0)
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.sleep(0.2)
        start = time.monotonic()
        if not task.done():  # đang chờ close_notify: hủy lần hai
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert time.monotonic() - start < 1.0
        assert server.closed
        assert server._on_sent is None and server._on_received is None
        await wait_until(lambda: raw.fileno() == -1, 1.0, "socket phía server đã bị abort")
    finally:
        sock.close()


# --- F3: receive bị hủy sau khi đã đọc một phần khung ---------------------------------------------


def _valid_inbound(kind: str) -> tuple[dict, object]:
    """Một message hợp lệ client gửi được tới server trên loại kết nối `kind` (dict trên dây, message)."""
    if kind == "pairing":
        return {"type": "pair_abort", "reason": "user"}, PairAbort(reason="user")
    return {"type": "ping", "t": 5}, Ping(t=5)


async def _server_conn(kind: str, h, ids, raw_client):
    if kind == "pairing":
        return await raw_pairing(raw_client, h, ids)
    return await raw_control(raw_client, h, ids)


@pytest.mark.integration
@pytest.mark.parametrize("cut", ["header-2-bytes", "header-and-5-body-bytes"])
@pytest.mark.parametrize("kind", ["pairing", "control"])
async def test_receive_cancelled_mid_frame_aborts_connection(
    fast, listener_factory, raw_client, ids, kind, cut
):
    """F3: hết giờ của bên gọi khi đã đọc một phần khung → kết nối bị abort, không đọc lệch khung."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await _server_conn(kind, h, ids, raw_client)
    wire, _ = _valid_inbound(kind)
    data = encode_message(wire)
    n = 2 if cut == "header-2-bytes" else HEADER.size + 5
    peer.send_bytes(data[:n])
    await peer.drain()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(server.receive(), 0.3)
    assert server.closed, "receive bị hủy giữa khung mà kết nối vẫn mở (lần đọc sau sẽ lệch khung)"

    # Phần còn lại tới muộn, kèm một khung trọn: không được đọc thành message nào.
    peer.send_bytes(data[n:] + data)
    await peer.drain()
    with pytest.raises(ConnectionLostError):
        await asyncio.wait_for(server.receive(), 2)
    await peer.expect_closed(2.0)


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["pairing", "control"])
async def test_receive_cancelled_before_any_byte_keeps_connection(
    fast, listener_factory, raw_client, ids, kind
):
    """F3, ca đối chứng: hết giờ khi chưa có byte nào của khung → kết nối vẫn dùng được."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await _server_conn(kind, h, ids, raw_client)
    wire, expected = _valid_inbound(kind)

    for _ in range(2):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(server.receive(), 0.2)
    assert not server.closed
    peer.send_dict(wire)
    await peer.drain()
    assert await asyncio.wait_for(server.receive(), 2) == expected
    assert not server.closed


# --- F4: send bị hủy -----------------------------------------------------------------------------


class DrainGate:
    """Giữ `StreamWriter.drain` (vá ở mức lớp) từ lần gọi thứ `block_from` khi `armed`.

    Dùng để dựng một lần gửi đang dở một cách tất định. Chỉ bật trong lúc kết nối dưới thử gửi:
    client thô cũng dùng `StreamWriter.drain`.
    """

    def __init__(self, monkeypatch, block_from: int) -> None:
        self.armed = False
        self.calls = 0
        self.block_from = block_from
        self.blocked = asyncio.Event()
        self.opened = asyncio.Event()
        original = asyncio.StreamWriter.drain
        gate = self

        async def drain(writer):
            if gate.armed:
                gate.calls += 1
                if gate.calls >= gate.block_from and not gate.opened.is_set():
                    gate.blocked.set()
                    await gate.opened.wait()
            await original(writer)

        monkeypatch.setattr(asyncio.StreamWriter, "drain", drain)

    def open(self) -> None:
        self.armed = False
        self.opened.set()


@pytest.mark.integration
async def test_send_cancelled_while_waiting_for_lock_keeps_connection(
    fast, monkeypatch, listener_factory, raw_client, ids
):
    """F4: lần gửi bị hủy khi đang chờ lock, hoặc trước khi chạy → không ghi gì, kết nối vẫn dùng được."""
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 5.0)
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    gate = DrainGate(monkeypatch, block_from=1)

    gate.armed = True
    first = asyncio.create_task(server.send(Ping(t=1)))
    await asyncio.wait_for(gate.blocked.wait(), 2)  # lần gửi đầu giữ lock, kẹt ở drain
    waiting = asyncio.create_task(server.send(Ping(t=2)))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    never_started = asyncio.create_task(server.send(Ping(t=3)))
    waiting.cancel()
    never_started.cancel()
    for t in (waiting, never_started):
        with pytest.raises(asyncio.CancelledError):
            await t

    gate.open()
    await asyncio.wait_for(first, 2)
    assert not server.closed
    await server.send(Ping(t=4))
    got = [await peer.read(), await peer.read()]
    assert got == [{"type": "ping", "t": 1}, {"type": "ping", "t": 4}]
    assert not server.closed


@pytest.mark.integration
async def test_send_cancelled_between_chunks_aborts_connection(
    fast, monkeypatch, listener_factory, raw_client, ids
):
    """F4: hủy sau khi đã ghi một phần khung → kết nối bị abort; lần gửi sau → ConnectionLostError.

    Message hợp lệ nào hiện cũng < 64 KiB, nên `_SEND_CHUNK` (đọc lúc dùng, mục 2.3) được hạ xuống
    8 byte để một `ping` thành nhiều đoạn; drain của đoạn thứ hai bị giữ rồi hủy.
    """
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 5.0)
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer, server = await raw_control(raw_client, h, ids)
    full = encode_message(to_dict(Ping(t=1)))
    assert len(full) > 2 * 8
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    gate = DrainGate(monkeypatch, block_from=2)

    gate.armed = True
    task = asyncio.create_task(server.send(Ping(t=1)))
    await asyncio.wait_for(gate.blocked.wait(), 2)  # đã ghi 2 đoạn đầu, khung còn dở
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.open()

    assert server.closed, "send bị hủy giữa khung mà kết nối vẫn mở (máy kia sẽ đọc lệch khung)"
    with pytest.raises(ConnectionLostError):
        await server.send(Ping(t=2))
    _, data = await peer.expect_closed(2.0)
    assert len(data) < len(full) and full.startswith(data), "máy kia nhận được nhiều hơn một phần khung"


# --- F5: chỉ kết nối thắng nhận control_open -------------------------------------------------------

OTHER_ADDRESS = "198.51.100.7"  # TEST-NET-2: được chuyển hướng về server giả thứ hai trên 127.0.0.1


async def _hello_then_record(server, reader, writer):
    writer.write(encode_message(wire_hello(server.identity, "Server")))
    await writer.drain()
    await server.record_until_closed(reader)


@pytest.mark.integration
async def test_losing_attempt_never_sends_control_open(slow_opening, monkeypatch, fake_server, ids):
    """F5: hai địa chỉ cùng trao xong `hello` trong một vòng lặp → chỉ kết nối thắng nhận `control_open`.

    Hai server giả cùng danh tính (đúng fingerprint). `asyncio.open_connection` được bọc để
    (1) chuyển địa chỉ thứ hai về server giả thứ hai và (2) trả về cho cả hai lần thử cùng lúc,
    khi `hello` của cả hai server đã nằm sẵn trong bộ đệm: hai lần thử xong `hello` liền nhau.
    """
    s1 = await fake_server(ids.server, _hello_then_record, trusted_certs=[ids.b.cert_pem()])
    s2 = await fake_server(ids.server, _hello_then_record, trusted_certs=[ids.b.cert_pem()])
    real_open = asyncio.open_connection
    opened: list = []
    both_ready = asyncio.Event()

    async def open_connection(host=None, port=None, **kwargs):
        if host == OTHER_ADDRESS:
            host, port = LOCAL, s2.port
        streams = await real_open(host, port, **kwargs)
        opened.append(streams)
        if len(opened) == 2:
            await asyncio.sleep(0.3)  # hello của cả hai server đã tới bộ đệm
            both_ready.set()
        else:
            await both_ready.wait()
        return streams

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    conn = await open_control([LOCAL, OTHER_ADDRESS], s1.port, ids.b, hello_of(ids.b), ids.server.fingerprint)
    try:
        assert len(opened) == 2, "phép thử vô nghĩa: không có hai lần thử song song"
        winner, loser = (s1, s2) if conn.peer.address == LOCAL else (s2, s1)
        await wait_until(lambda: not loser._tasks, 3.0, "lần thử thua bị đóng")
        await wait_until(lambda: len(types_in(winner.received)) >= 2, 2.0, "kết nối thắng nhận control_open")
        assert types_in(winner.received) == ["hello", "control_open"]
        assert "control_open" not in types_in(loser.received), "lần thử thua đã gửi control_open"
    finally:
        await conn.close()


@pytest.mark.integration
async def test_error_sending_control_open_is_unreachable(slow_opening, monkeypatch, fake_server, ids):
    """F5 (quyết định đã duyệt): OSError khi gửi `control_open` trên kết nối thắng → abort, Unreachable."""
    server = await fake_server(ids.server, _hello_then_record, trusted_certs=[ids.b.cert_pem()])
    real_write = asyncio.StreamWriter.write
    real_drain = asyncio.StreamWriter.drain
    failing: set[int] = set()

    def write(writer, data):
        if b'"control_open"' in bytes(data):
            failing.add(id(writer))  # giả lập: socket hỏng đúng lúc gửi control_open
            return None
        return real_write(writer, data)

    async def drain(writer):
        if id(writer) in failing:
            raise ConnectionResetError("giả lập lỗi gửi control_open")
        await real_drain(writer)

    monkeypatch.setattr(asyncio.StreamWriter, "write", write)
    monkeypatch.setattr(asyncio.StreamWriter, "drain", drain)
    with pytest.raises(Unreachable) as exc:
        await open_control([LOCAL], server.port, ids.b, hello_of(ids.b), ids.server.fingerprint)
    assert exc.value.address == LOCAL
    assert failing, "phép thử vô nghĩa: control_open chưa từng được gửi"
    await wait_until(lambda: not server._tasks, 3.0, "server giả thấy kết nối bị đóng")
    assert "control_open" not in types_in(server.received)


# --- CloseReason.CONNECTION_LOST -----------------------------------------------------------------


def _close_with_rst(sock: socket.socket) -> None:
    """Đóng socket bằng RST (SO_LINGER = 0) thay cho FIN: phía kia thấy lỗi socket, không phải EOF.

    Dùng socket đồng bộ: `transport.abort()` của asyncio gọi `shutdown()` trước khi đóng (Proactor),
    nên phía kia chỉ thấy EOF.
    """
    linger = struct.pack("HH", 1, 0) if sys.platform == "win32" else struct.pack("ii", 1, 0)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
    sock.close()


@pytest.mark.integration
async def test_peer_reset_during_run_is_connection_lost(fast, listener_factory, ids):
    """Mục 3.4: kết nối bị reset (lỗi socket) trong lúc `run` chạy → CONNECTION_LOST, không PEER_CLOSED."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    sock = await asyncio.to_thread(sync_open_control, h.port, ids.b)
    try:
        server = await h.next()
        _, task = start_channel(server, [], dead_after=5.0)
        await asyncio.sleep(0.1)
        assert not task.done()
        _close_with_rst(sock)
        assert await asyncio.wait_for(task, 3) is CloseReason.CONNECTION_LOST
        assert server.closed
    finally:
        sock.close()


# --- Connection.close() bị hủy giữa chừng ----------------------------------------------------------


@pytest.mark.integration
async def test_cancelled_close_aborts_transport(fast, monkeypatch, listener_factory, ids):
    """Mục 2.3 `close()`: bị hủy khi đang chờ close_notify → abort transport rồi CancelledError bay ra.

    Máy kia không bao giờ đọc (không trả close_notify) và CLOSE_TIMEOUT = 5 s, nên close() đang
    treo ở bước chờ khi bị hủy. Sau đó socket đã đóng thật, và close() lần sau trả về ngay.
    """
    monkeypatch.setattr(connection, "CLOSE_TIMEOUT", 5.0)
    h = await listener_factory(ids.server, trusted=[ids.b])
    sock = await asyncio.to_thread(sync_open_control, h.port, ids.b)
    try:
        server = await h.next()
        raw = server._writer.get_extra_info("socket")
        task = asyncio.create_task(server.close())
        await asyncio.sleep(0.2)
        assert not task.done(), "close() phải đang chờ close_notify (máy kia không đọc)"
        start = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert time.monotonic() - start < 1.0
        assert server.closed
        await wait_until(lambda: raw.fileno() == -1, 1.0, "socket phía server đã bị abort")
        await asyncio.wait_for(server.close(), 1.0)
        with pytest.raises(ConnectionLostError):
            await server.send(Ping(t=1))
    finally:
        sock.close()


# --- Listener.start() bị hủy hoặc bị close() chen ngang ở từng nhịp (sửa G1, review PR-4 vòng 2) ----
#
# Hợp đồng kiểm ở đây:
# - `start()` raise (CancelledError khi bị hủy, RuntimeError khi close() chen ngang) thì cổng đã
#   đóng ngay lúc đó, KHÔNG cần ai gọi `close()` (bên gọi như `Node.start` nhả `node.lock` ngay sau).
# - `start()` bị hủy vẫn gọi lại được (như mọi lần `start()` thất bại, PR-2); `close()` đóng được.
# - `close()` trả về thì cổng đã đóng, kể cả khi `start()` đang chạy dở.
# Cổng “đã đóng” đo bằng cách bind lại đúng cổng đó (không SO_REUSEADDR): nhanh, không phụ thuộc
# việc Windows mất ~2 s mới báo kết nối bị từ chối.

_SWEEP_TICKS = 12  # đủ phủ toàn bộ start() (thực tế chỉ vài nhịp); nhịp cuối phải là start() đã xong


def _port_is_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((LOCAL, port))
    except OSError:
        return False
    else:
        return True
    finally:
        s.close()


@pytest.fixture
async def server_tracker(monkeypatch):
    """Ghi mọi `asyncio.Server` tạo trong test (kể cả server bị rò khi hủy) để dọn khi test xong.

    Chỉ dùng để dọn và in chẩn đoán, không để khẳng định: `loop.create_server` được bọc bằng phiên
    bản tương đương (bind với start_serving=False, ghi lại, rồi `start_serving()` nếu bên gọi muốn).
    """
    loop = asyncio.get_running_loop()
    real = loop.create_server
    servers: list[asyncio.Server] = []

    async def create_server(*args, **kwargs):
        serving = kwargs.pop("start_serving", True)
        server = await real(*args, start_serving=False, **kwargs)
        servers.append(server)
        if serving:
            await server.start_serving()
        return server

    monkeypatch.setattr(loop, "create_server", create_server)
    yield servers
    for server in servers:
        server.close()
    for server in servers:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(server.wait_closed(), 2)


def _serving(servers) -> list[str]:
    return [repr(s) for s in servers if s.is_serving()]


@pytest.mark.integration
async def test_start_cancelled_at_any_tick_leaves_no_listening_port(
    fast, listener_factory, server_tracker, ids
):
    outcomes: list[str] = []
    for k in range(_SWEEP_TICKS):
        h = await listener_factory(ids.server, start=False)
        port = free_port()
        task = asyncio.create_task(h.listener.start(LOCAL, port), name="test.listener-start")
        for _ in range(k):
            await asyncio.sleep(0)
        if task.done():
            outcomes.append("done")
            assert task.result() == port
            assert not _port_is_free(port), f"k={k}: start() xong mà cổng chưa nghe"
            await h.listener.close()
            assert _port_is_free(port), f"k={k}: close() xong mà cổng còn nghe"
            continue
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        outcomes.append("cancelled")
        assert _port_is_free(port), (
            f"k={k}: start() đã raise CancelledError mà cổng {port} vẫn bị giữ "
            f"(server rò, close() không đóng được): {_serving(server_tracker)}"
        )
        # start() bị hủy là một lần start() thất bại: gọi lại được, rồi close() đóng được.
        again = await asyncio.wait_for(h.listener.start(LOCAL, port), 2)
        assert again == port
        await h.listener.close()
        assert _port_is_free(port), f"k={k}: close() sau lần start() thứ hai mà cổng còn nghe"
    assert "cancelled" in outcomes, outcomes
    assert outcomes[-1] == "done", f"{_SWEEP_TICKS} nhịp chưa phủ hết start(): {outcomes}"
    assert _serving(server_tracker) == []


@pytest.mark.integration
async def test_close_during_start_at_any_tick_closes_port(fast, listener_factory, server_tracker, ids):
    outcomes: list[str] = []
    for k in range(_SWEEP_TICKS):
        h = await listener_factory(ids.server, start=False)
        port = free_port()
        task = asyncio.create_task(h.listener.start(LOCAL, port), name="test.listener-start")
        for _ in range(k):
            await asyncio.sleep(0)
        started = task.done()
        await asyncio.wait_for(h.listener.close(), 3)
        assert _port_is_free(port), (
            f"k={k}: close() đã trả về mà cổng {port} vẫn nghe: {_serving(server_tracker)}"
        )
        try:
            got = await asyncio.wait_for(task, 2)
        except RuntimeError:
            outcomes.append("refused")
        else:
            assert started, f"k={k}: start() trả về {got} dù close() đã chạy trước khi nó xong"
            outcomes.append("done")
        assert _port_is_free(port), f"k={k}: start() kết thúc mà cổng {port} vẫn nghe"
        with pytest.raises(RuntimeError):
            await h.listener.start(LOCAL, 0)  # sau close(): không start lại được
    assert "refused" in outcomes, outcomes
    assert outcomes[-1] == "done", f"{_SWEEP_TICKS} nhịp chưa phủ hết start(): {outcomes}"
    assert _serving(server_tracker) == []
