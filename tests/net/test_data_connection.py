"""Kết nối dữ liệu trên socket thật 127.0.0.1 (spec P2-3 mục 3.1–3.6; AC-1…AC-3, AC-5…AC-9, AC-11…AC-16,
AC-18 và các ca biên mục 5.3 cần socket).

Viết từ spec và `docs/protocol.md` (§2.1–2.3, §4.1–4.2, §7), không từ code. Mọi hằng thời gian được vá qua
module `teleport.net.connection` **trước** khi tạo `Listener` hay gọi `open_*`, vì `ssl_shutdown_timeout`
được đặt lúc mở kết nối (spec mục 2, “Seam cho test”). Phép thử “máy kia không bao giờ đọc” dùng
`ssl.SSLSocket` chặn trong thread: peer asyncio tự trả `close_notify` (spec mục 7, bẫy 6).

Test kiểm tham số trước I/O (`no_io`) không mở socket nên không mang marker `integration`.
"""

import asyncio
import contextlib
import datetime
import hashlib
import inspect
import logging
import random
import socket
import ssl
import struct
import time
import tracemalloc
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from support import wait_until

from teleport.core.blocks import BLOCK_SIZE, BlockError, BlockHeader
from teleport.core.framing import HEADER, decode_body, encode_message
from teleport.core.messages import (
    PROTOCOL_VERSION,
    ConnectionKind,
    ControlOpen,
    DataOpen,
    Hello,
    PairRequest,
    Ping,
    to_dict,
)
from teleport.net import connection
from teleport.net.connection import (
    ConnectionLostError,
    IdentityMismatch,
    Rejected,
    Unreachable,
    open_control,
)
from teleport.trust.identity import CERT_FILE, KEY_FILE, Identity, load_or_create_identity

pytestmark = [
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
integration = pytest.mark.integration

log = logging.getLogger(__name__)

LOCAL = "127.0.0.1"
MiB = 1 << 20
TID = "0123456789abcdef" * 2
OPENING = DataOpen(transfer_id=TID, file_index=0, resume_seq=1)
DATA_OPEN = to_dict(OPENING)
HDR = struct.Struct("!BII32s")  # protocol §4.2
SIZE4 = 3 * BLOCK_SIZE + 123  # 4 khối, khối cuối ngắn (AC-7)


TEST_TIMEOUT = 180.0  # chặn trên cho mọi test (AC-16 tự giới hạn 120 s); quá thì là treo, không chờ mãi


@pytest.fixture(autouse=True)
async def _watchdog(request):
    """Hủy task của test sau `TEST_TIMEOUT`: cài sai thì test đỏ thay vì treo cả bộ."""
    if not inspect.iscoroutinefunction(request.function):
        yield
        return
    loop = asyncio.get_running_loop()
    before = asyncio.all_tasks()

    def fire() -> None:
        for task in asyncio.all_tasks(loop):
            if task not in before and not task.done():
                task.cancel(f"test quá {TEST_TIMEOUT} s (treo?)")

    handle = loop.call_later(TEST_TIMEOUT, fire)
    yield
    handle.cancel()


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


def data_open_dict(**overrides) -> dict:
    return {**DATA_OPEN, **overrides}


def header_bytes(index: int, length: int, sha: bytes, type_: int = 0x02) -> bytes:
    return HDR.pack(type_, index, length, sha)


def make_blocks(size: int, indices, seed: int = 0) -> list[tuple[int, bytes, bytes]]:
    """[(index, sha256, payload)] với payload ngẫu nhiên đúng độ dài khối của file `size` byte."""
    rng = random.Random(seed)
    out = []
    for i in indices:
        payload = rng.randbytes(min(BLOCK_SIZE, size - i * BLOCK_SIZE))
        out.append((i, hashlib.sha256(payload).digest(), payload))
    return out


async def read_all_blocks(conn, size: int, *, idle: float = 10.0) -> list[tuple[int, bytes, bytes]]:
    got = []
    while (item := await conn.read_block(size=size, idle_timeout=idle)) is not None:
        hdr, payload = item
        assert hdr.length == len(payload)
        got.append((hdr.index, hdr.sha256, bytes(payload)))
    return got


async def cancel_wait(*tasks) -> None:
    for t in tasks:
        if t is not None:
            t.cancel()
    for t in tasks:
        if t is not None:
            with contextlib.suppress(BaseException):
                await t


# `Connection` không có API công khai cho `StreamWriter`/`StreamReader` của nó, mà bộ đệm socket nhỏ
# (spec mục 7, bẫy 5), AC-9 (h) và AC-14 (b) cần tới transport và `at_eof()`. Tên `_writer`/`_reader` khớp
# tham số `writer=`/`reader=` của seam không socket (spec mục 2). Mọi truy cập thuộc tính riêng đi qua hai
# hàm dưới đây: src đổi tên thì test đỏ với thông báo rõ và chỉ phải sửa ở đây.


def writer_of(conn) -> asyncio.StreamWriter:
    writer = getattr(conn, "_writer", None)
    assert writer is not None, "Connection không còn `_writer` (seam test riêng): sửa writer_of()"
    return writer


def reader_of(conn) -> asyncio.StreamReader:
    reader = getattr(conn, "_reader", None)
    assert reader is not None, "Connection không còn `_reader` (seam test riêng): sửa reader_of()"
    return reader


def set_small_buffer(conn, option: int, size: int = 4096) -> None:
    """Bộ đệm socket nhỏ, chỉ trên socket của test (spec mục 7, bẫy 5)."""
    writer_of(conn).get_extra_info("socket").setsockopt(socket.SOL_SOCKET, option, size)


def capture(body):
    """`on_connection` chạy `body(conn)`; (trạng thái, giá trị hay ngoại lệ, conn) vào hàng đợi trả về.

    `CancelledError` được ghi rồi raise lại; ngoại lệ khác chỉ được ghi (handler trả về bình thường).
    """
    results: asyncio.Queue = asyncio.Queue()

    async def on_connection(conn) -> None:
        try:
            value = await body(conn)
        except BaseException as e:
            results.put_nowait(("error", e, conn))
            if isinstance(e, asyncio.CancelledError):
                raise
            return
        results.put_nowait(("ok", value, conn))

    return on_connection, results


async def read_outcome(conn, *, idle: float = 5.0, within: float = 3.0):
    """Lần đọc kế tiếp phía này: None, hay lớp lỗi (ConnectionLostError / IdleTimeout / BlockError)."""
    try:
        item = await asyncio.wait_for(conn.read_block(size=10 * BLOCK_SIZE, idle_timeout=idle), within)
    except connection.IdleTimeout:
        return connection.IdleTimeout
    except ConnectionLostError:
        return ConnectionLostError
    except BlockError:
        return BlockError
    return item


@pytest.fixture(scope="module")
def ids(tmp_path_factory):
    """Danh tính dùng chung cho cả module: server A, B tin cậy, X lạ, Anon."""
    base = tmp_path_factory.mktemp("data-ids")
    return SimpleNamespace(
        server=load_or_create_identity(base / "server"),
        b=load_or_create_identity(base / "b"),
        x=load_or_create_identity(base / "stranger"),
        anon=load_or_create_identity(base / "anon"),
    )


@pytest.fixture
def slow_opening(fast, monkeypatch):
    """OPENING_TIMEOUT = 5 s (đóng nhanh là bị từ chối, không phải hết giờ); CLOSE/DRAIN = 0,5 s."""
    monkeypatch.setattr(fast, "OPENING_TIMEOUT", 5.0)
    return fast


@pytest.fixture
def no_io(monkeypatch):
    """Mở socket trong test kiểm tham số là lỗi: tham số sai phải bị chặn trước mọi I/O."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("không được mở socket khi tham số sai")

    monkeypatch.setattr(asyncio, "open_connection", forbidden)
    monkeypatch.setattr(asyncio, "start_server", forbidden)


def count_open_connection(monkeypatch) -> list[tuple]:
    """Đếm số lần `asyncio.open_connection` được gọi (spec: `open_data` mở TCP qua tên toàn cục này)."""
    real = asyncio.open_connection
    calls: list[tuple] = []

    async def counting(*args, **kwargs):
        calls.append(args[:2] or (kwargs.get("host"), kwargs.get("port")))
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", counting)
    return calls


async def b_data(h, ids, opening: DataOpen = OPENING, *, identity=None, expected: str | None = None):
    identity = identity if identity is not None else ids.b
    return await connection.open_data(
        LOCAL, h.port, identity, hello_of(identity), expected or h.identity.cert_pem(), opening
    )


async def data_pair(h, ids, opening: DataOpen = OPENING):
    """(client, server) của một kết nối dữ liệu qua `open_data` tới harness mặc định (giữ kết nối)."""
    client = await b_data(h, ids, opening)
    try:
        server = await h.next()
    except BaseException:
        client.abort()
        raise
    return client, server


async def raw_opening(raw_client, h, identity, hello: dict, opening: dict | None, extra: bytes = b""):
    """Client thô: TLS (chứng chỉ `identity` hay ẩn danh), đọc hello server, gửi hello, `opening`, `extra`."""
    peer = await raw_client(h.port, identity)
    await peer.read_hello()
    peer.send_dict(hello)
    if opening is not None:
        peer.send_dict(opening)
    if extra:
        peer.send_bytes(extra)
    await peer.drain()
    return peer


async def nothing_received(peer, duration: float) -> bool:
    """True nếu trong `duration` giây máy kia không gửi byte nào và cũng không đóng kết nối."""
    try:
        await asyncio.wait_for(peer.reader.read(1), duration)
    except TimeoutError:
        return True
    return False


# --- Client ssl đồng bộ: máy kia không bao giờ đọc ----------------------------------------------


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


def cert_client_context(identity) -> ssl.SSLContext:
    """Client gửi chứng chỉ của `identity` mà không kiểm server (chỉ để thử phía server)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(identity.cert_path, identity.key_path)
    return ctx


def sync_open_data(
    port: int, identity, *, rcvbuf: int | None = None, opening: dict | None = None, extra: bytes = b""
) -> ssl.SSLSocket:
    """Mở kết nối dữ liệu bằng socket chặn: đọc hello server, gửi hello + data_open (+ `extra`)."""
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if rcvbuf is not None:
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        raw.settimeout(5)
        raw.connect((LOCAL, port))
        sock = cert_client_context(identity).wrap_socket(raw)
    except BaseException:
        raw.close()
        raise
    try:
        hello = sync_read_frame(sock)
        assert hello is not None and hello["type"] == "hello"
        sock.sendall(encode_message(wire_hello(identity)) + encode_message(opening or DATA_OPEN) + extra)
    except BaseException:
        sock.close()
        raise
    return sock


def peer_sees_close(sock: ssl.SSLSocket, timeout: float) -> str:
    """'eof' / 'error' nếu máy kia đã đóng trong `timeout` giây; 'open' nếu chưa; 'data' nếu có byte."""
    sock.settimeout(timeout)
    try:
        data = sock.recv(1)
    except TimeoutError:
        return "open"
    except (OSError, ssl.SSLError):
        return "error"
    return "eof" if data == b"" else "data"


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((LOCAL, 0))
        return s.getsockname()[1]
    finally:
        s.close()


# =================================================================================================
# AC-1: Listener giao DATA
# =================================================================================================


@pytest.mark.slow
@integration
async def test_listener_delivers_trusted_data_open(slow_opening, listener_factory, raw_client, ids):
    h = await listener_factory(ids.server, trusted=[ids.b])
    opening = DataOpen(transfer_id="fedcba9876543210" * 2, file_index=3, resume_seq=7)
    # Byte ngay sau data_open (cùng một lần ghi) phải tới được kết nối, không bị Listener đọc mất.
    peer = await raw_opening(raw_client, h, ids.b, wire_hello(ids.b), to_dict(opening), extra=b"tiep")
    conn = await h.next()
    assert conn.kind is ConnectionKind.DATA
    assert conn.is_server is True
    assert conn.opening == opening
    assert conn.peer.fingerprint == ids.b.fingerprint
    assert conn.peer.cert.device_id == ids.b.device_id
    assert conn.peer.address == LOCAL
    assert conn.peer.hello == hello_of(ids.b)
    assert conn.closed is False
    await h.wait_idle()
    await asyncio.sleep(0.1)
    assert h.calls == 1
    assert h.listener.opening_count == 0
    assert h.listener.connection_count == 1
    assert await conn.read_raw(4, at_boundary=True, idle_timeout=2) == bytearray(b"tiep")
    assert await nothing_received(peer, 0.3), "server không được gửi gì sau data_open (protocol §2.1, §4.1)"


# =================================================================================================
# AC-2: từ chối ở tầng mạng (OPENING_TIMEOUT = 5: đóng < 1 s là từ chối ngay)
# =================================================================================================


async def assert_rejected(h, peer, within: float = 3.0) -> float:
    """Server đóng kết nối, không gửi gì ngoài hello (đã đọc trước), không gọi on_connection."""
    elapsed, data = await peer.expect_closed(within)
    assert data == b"", f"server gửi thêm {len(data)} byte trước khi đóng (protocol §2.1)"
    await h.wait_idle()
    assert h.calls == 0
    assert h.listener.opening_count == 0
    assert h.listener.connection_count == 0
    return elapsed


def _sc(identity_attr: str | None, hello_attr: str, opening: dict):
    async def scenario(env):
        identity = None if identity_attr is None else getattr(env.ids, identity_attr)
        hello = wire_hello(getattr(env.ids, hello_attr))
        return await raw_opening(env.raw_client, env.h, identity, hello, opening)

    return scenario


_NO_SEQ = {k: v for k, v in DATA_OPEN.items() if k != "resume_seq"}
REJECTIONS = {
    "a-anonymous": _sc(None, "anon", DATA_OPEN),
    "c-hello-device_id-not-CN": _sc("b", "x", DATA_OPEN),
    "d-transfer_id-uppercase": _sc("b", "b", data_open_dict(transfer_id=TID.upper())),
    "d-transfer_id-short": _sc("b", "b", data_open_dict(transfer_id="ab" * 15)),
    "d-file_index-negative": _sc("b", "b", data_open_dict(file_index=-1)),
    "d-file_index-bool": _sc("b", "b", data_open_dict(file_index=True)),
    "d-resume_seq-zero": _sc("b", "b", data_open_dict(resume_seq=0)),
    "d-resume_seq-missing": _sc("b", "b", _NO_SEQ),
}


@integration
@pytest.mark.parametrize("case", list(REJECTIONS))
async def test_bad_data_open_is_rejected_immediately(slow_opening, listener_factory, raw_client, ids, case):
    h = await listener_factory(ids.server, trusted=[ids.b])
    env = SimpleNamespace(h=h, ids=ids, raw_client=raw_client)
    peer = await REJECTIONS[case](env)
    elapsed = await assert_rejected(h, peer)
    assert elapsed < 1.0, f"đóng sau {elapsed:.2f} s: có vẻ chờ hết giờ thay vì từ chối ngay"


@integration
async def test_unknown_client_cert_cannot_open_data(slow_opening, listener_factory, ids):
    """AC-2 (b): chứng chỉ lạ → client thấy Rejected (hoặc bắt tay hỏng: Unreachable), không được giao."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    start = time.monotonic()
    with pytest.raises((Rejected, Unreachable)):
        await connection.open_data(LOCAL, h.port, ids.x, hello_of(ids.x), ids.server.cert_pem(), OPENING)
    assert time.monotonic() - start < 1.0
    await h.wait_idle()
    assert h.calls == 0
    assert h.listener.connection_count == 0


@integration
async def test_is_trusted_raising_rejects_data_open(slow_opening, listener_factory, raw_client, ids):
    """AC-2 (e)."""

    def broken(fp):
        raise RuntimeError("kho tin cậy hỏng")

    h = await listener_factory(ids.server, trusted=[ids.b], is_trusted=broken)
    peer = await raw_opening(raw_client, h, ids.b, wire_hello(ids.b), DATA_OPEN)
    assert await assert_rejected(h, peer) < 1.0


@integration
async def test_unpair_after_handshake_is_caught_at_data_open(slow_opening, listener_factory, raw_client, ids):
    """AC-2 (f), protocol §7: bắt tay bằng context cũ → hủy ghép đôi → rồi mới gửi data_open → bị đóng."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer = await raw_opening(raw_client, h, ids.b, wire_hello(ids.b), None)
    await asyncio.sleep(0.1)  # server đã đọc hello và đang chờ message mở đầu
    h.listener.update_trusted([])
    h.trusted_fps.clear()
    peer.send_dict(DATA_OPEN)
    await peer.drain()
    assert await assert_rejected(h, peer) < 1.0


@integration
async def test_is_trusted_turning_false_before_data_open_rejects_it(
    slow_opening, listener_factory, raw_client, ids
):
    """Kiểm lại `is_trusted` lúc nhận `data_open` (spec mục 3.1 bước 3), kể cả khi context TLS chưa đổi."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    peer = await raw_opening(raw_client, h, ids.b, wire_hello(ids.b), None)
    await asyncio.sleep(0.1)
    h.trusted_fps.clear()
    peer.send_dict(DATA_OPEN)
    await peer.drain()
    await assert_rejected(h, peer)


# =================================================================================================
# AC-3: không có await giữa lần kiểm is_trusted cuối và handler
# =================================================================================================


@integration
async def test_no_suspension_between_trust_check_and_data_handler(
    slow_opening, listener_factory, raw_client, ids
):
    loop = asyncio.get_running_loop()
    flags: list[dict] = []
    seen: list = []

    def is_trusted(fp):
        flag = {"ran": False}
        flags.append(flag)
        loop.call_soon(flag.__setitem__, "ran", True)
        return fp == ids.b.fingerprint

    async def on_connection(conn):
        seen.append((conn.kind, len(flags), flags[-1]["ran"]))

    h = await listener_factory(
        ids.server, trusted=[ids.b], is_trusted=is_trusted, on_connection=on_connection
    )
    await raw_opening(raw_client, h, ids.b, wire_hello(ids.b), DATA_OPEN)
    await wait_until(lambda: seen, 2.0, "on_connection được gọi")
    [(kind, calls, ran)] = seen
    assert kind is ConnectionKind.DATA
    assert calls >= 1, "is_trusted chưa từng được gọi trước on_connection"
    assert ran is False, "có await thật giữa lần kiểm is_trusted cuối và on_connection"


# =================================================================================================
# AC-5: open_data thành công
# =================================================================================================


@integration
async def test_open_data_end_to_end(slow_opening, monkeypatch, listener_factory, ids):
    h = await listener_factory(ids.server, trusted=[ids.b])
    calls = count_open_connection(monkeypatch)
    opening = DataOpen(transfer_id="00ff" * 8, file_index=12, resume_seq=99)
    loop = asyncio.get_running_loop()
    before = loop.time()
    client = await connection.open_data(LOCAL, h.port, ids.b, hello_of(ids.b), ids.server.cert_pem(), opening)
    after = loop.time()
    try:
        assert client.kind is ConnectionKind.DATA
        assert client.is_server is False
        assert client.opening is None
        assert client.closed is False
        assert client.peer.address == LOCAL
        assert client.peer.port == h.port
        assert client.peer.fingerprint == ids.server.fingerprint
        assert client.peer.cert.device_id == ids.server.device_id
        assert client.peer.hello == h.hello
        assert before <= client.opened_at <= after
        server = await h.next()
        assert server.kind is ConnectionKind.DATA
        assert server.opening == opening
        assert server.peer.fingerprint == ids.b.fingerprint
        await asyncio.sleep(0.2)
        assert h.calls == 1, "server phải nhận đúng một kết nối"
        assert len(calls) == 1, f"open_data mở {len(calls)} kết nối TCP, cần đúng 1"
    finally:
        client.abort()


@integration
async def test_bytes_written_right_after_open_data_arrive(slow_opening, listener_factory, ids):
    """Không có xác nhận sau data_open: bên gửi ghi ngay được, không mất byte nào (spec mục 3.2 bước 4)."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    client = await b_data(h, ids)
    try:
        await client.write_raw(b"ngay-sau-data_open")
        server = await h.next()
        assert await server.read_raw(18, at_boundary=True, idle_timeout=2) == bytearray(b"ngay-sau-data_open")
    finally:
        client.abort()


# =================================================================================================
# AC-6: open_data lỗi — kiểm tham số trước mọi I/O (không socket)
# =================================================================================================


async def call_open_data(ids, address=LOCAL, port=47474, **overrides):
    return await connection.open_data(
        address,
        port,
        overrides.get("identity", ids.b),
        overrides.get("hello", hello_of(ids.b)),
        overrides.get("expected", ids.server.cert_pem()),
        overrides.get("opening", OPENING),
    )


BAD_ADDRESSES = [
    [LOCAL],
    (LOCAL,),
    "::1",
    "0.0.0.0",
    "224.0.0.1",
    "224.0.0.251",
    "255.255.255.255",
    "127.000.0.1",
    "01.2.3.4",
    "1.2.3",
    "1.2.3.256",
    " 127.0.0.1",
    "localhost",
    "",
    None,
    127,
    b"127.0.0.1",
]


@pytest.mark.parametrize("address", BAD_ADDRESSES, ids=repr)
async def test_open_data_rejects_bad_address_before_io(no_io, ids, address):
    with pytest.raises(ValueError):
        await call_open_data(ids, address=address)


@pytest.mark.parametrize("port", [0, 65536, -1, True, False, "47474", 47474.0, None], ids=repr)
async def test_open_data_rejects_bad_port_before_io(no_io, ids, port):
    with pytest.raises(ValueError):
        await call_open_data(ids, port=port)


@pytest.mark.parametrize(
    "hello",
    ["protocol-2", "other-identity", "dict"],
)
async def test_open_data_rejects_bad_hello_before_io(no_io, ids, hello):
    value = {
        "protocol-2": hello_of(ids.b, protocol=2),
        "other-identity": hello_of(ids.x),
        "dict": wire_hello(ids.b),
    }[hello]
    with pytest.raises(ValueError):
        await call_open_data(ids, hello=value)


def _ca_cert_pem() -> str:
    """Chứng chỉ P-256 tự ký đúng mọi điều kiện của Teleport trừ BasicConstraints CA=true."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "teleport-" + "ab" * 16)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC))
        .not_valid_after(datetime.datetime(9999, 12, 31, tzinfo=datetime.UTC))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


BAD_EXPECTED_PEMS = {
    "fingerprint-instead-of-pem": "ab" * 32,
    "empty": "",
    "ascii-text": "not a PEM",
    "non-ascii": "không phải PEM",
    "broken-base64": "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
    "ca-cert": _ca_cert_pem(),
    "none": None,
    "bytes": b"-----BEGIN CERTIFICATE-----",
}


@pytest.mark.parametrize("expected", list(BAD_EXPECTED_PEMS.values()), ids=list(BAD_EXPECTED_PEMS))
async def test_open_data_rejects_bad_expected_cert_pem_before_io(no_io, ids, expected):
    with pytest.raises(ValueError):
        await call_open_data(ids, expected=expected)


@pytest.mark.parametrize(
    "opening",
    [ControlOpen(), DATA_OPEN, None, "data_open", Ping(t=1)],
    ids=["ControlOpen", "dict", "None", "str", "Ping"],
)
async def test_open_data_rejects_opening_that_is_not_data_open(no_io, ids, opening):
    with pytest.raises(ValueError):
        await call_open_data(ids, opening=opening)


async def test_open_data_rejects_pair_request_as_opening(no_io, ids):
    with pytest.raises(ValueError):
        await call_open_data(ids, opening=PairRequest(cert=ids.b.cert_pem()))


def test_open_data_signature():
    import inspect

    params = list(inspect.signature(connection.open_data).parameters)
    assert params == ["address", "port", "identity", "hello", "expected_cert_pem", "opening"]


# =================================================================================================
# AC-6: open_data lỗi trên socket thật
# =================================================================================================


async def _record(server, reader, writer):
    await server.record_until_closed(reader, timeout=5)


async def _hold(server, reader, writer):
    await server.record_until_closed(reader, timeout=30)


def forge_same_name_identity(real, directory):
    """Danh tính giả mạo `real`: cùng subject, issuer, serial, hạn và extension, khóa khác."""
    directory.mkdir(parents=True, exist_ok=True)
    real_cert = x509.load_pem_x509_certificate(real.cert_path.read_bytes())
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (
        x509.CertificateBuilder()
        .subject_name(real_cert.subject)
        .issuer_name(real_cert.issuer)
        .public_key(key.public_key())
        .serial_number(real_cert.serial_number)
        .not_valid_before(real_cert.not_valid_before_utc)
        .not_valid_after(real_cert.not_valid_after_utc)
    )
    for ext in real_cert.extensions:
        builder = builder.add_extension(ext.value, ext.critical)
    cert = builder.sign(key, hashes.SHA256())
    cert_path, key_path = directory / CERT_FILE, directory / KEY_FILE
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    return Identity(real.device_id, cert_path, key_path, hashlib.sha256(der).hexdigest())


@integration
async def test_open_data_to_wrong_server_identity_fails_in_handshake_and_sends_nothing(
    slow_opening, fake_server, ids
):
    """Server mang danh tính khác (lại tin chứng chỉ client) → IdentityMismatch ngay trong bắt tay; server
    không nhận được chứng chỉ client, `hello` hay `data_open`."""
    server = await fake_server(ids.server, _record, trusted_certs=[ids.b.cert_pem()])
    start = time.monotonic()
    with pytest.raises(IdentityMismatch) as exc:
        await connection.open_data(LOCAL, server.port, ids.b, hello_of(ids.b), ids.x.cert_pem(), OPENING)
    assert time.monotonic() - start < 2.0
    assert exc.value.actual_fingerprint is None
    assert exc.value.address == LOCAL
    await asyncio.sleep(0.2)
    assert server.connections == 0, "server giả đã bắt tay xong (đã nhận chứng chỉ client)"
    assert bytes(server.received) == b""


@integration
async def test_open_data_impostor_with_same_cn_and_serial_is_identity_mismatch(
    slow_opening, fake_server, ids, tmp_path
):
    fake = forge_same_name_identity(ids.server, tmp_path / "fake")
    assert fake.device_id == ids.server.device_id and fake.fingerprint != ids.server.fingerprint
    server = await fake_server(fake, _record, trusted_certs=[ids.b.cert_pem()])
    with pytest.raises(IdentityMismatch):
        await connection.open_data(LOCAL, server.port, ids.b, hello_of(ids.b), ids.server.cert_pem(), OPENING)
    await asyncio.sleep(0.2)
    assert server.connections == 0
    assert bytes(server.received) == b""


@integration
async def test_open_data_to_silent_tls_server_is_unreachable_after_opening_timeout(fast, fake_server, ids):
    """Server bắt tay xong nhưng không gửi `hello` → hết hạn OPENING_TIMEOUT (0,5 s) → Unreachable."""
    server = await fake_server(ids.server, _hold, trusted_certs=[ids.b.cert_pem()])
    start = time.monotonic()
    with pytest.raises(Unreachable) as exc:
        await connection.open_data(LOCAL, server.port, ids.b, hello_of(ids.b), ids.server.cert_pem(), OPENING)
    elapsed = time.monotonic() - start
    assert 0.4 <= elapsed <= 0.5 + 2.5, elapsed
    assert exc.value.address == LOCAL
    assert "data_open" not in bytes(server.received).decode("latin-1")


def _close_now(how: str):
    async def script(server, reader, writer):
        if how == "abort":
            writer.transport.abort()
        else:
            writer.close()
        await asyncio.sleep(0)

    return script


@pytest.mark.slow
@integration
@pytest.mark.parametrize("case", ["closed-port", "no-handshake", "close-after-handshake", "wrong-identity"])
async def test_open_data_makes_exactly_one_attempt(slow_opening, monkeypatch, fake_server, ids, case):
    """Một lần thử duy nhất (protocol §4.1): không thử song song, không thử lại, lỗi ném thẳng."""
    expected_pem = ids.server.cert_pem()
    if case == "closed-port":
        port, error = free_port(), Unreachable
        monkeypatch.setattr(connection, "OPENING_TIMEOUT", 2.0)
    elif case == "no-handshake":
        port, error = (await fake_server(None, _hold, use_tls=False)).port, Unreachable
        monkeypatch.setattr(connection, "OPENING_TIMEOUT", 0.5)
    elif case == "close-after-handshake":
        server = await fake_server(ids.server, _close_now("graceful"), trusted_certs=[ids.b.cert_pem()])
        port, error = server.port, Rejected
    else:
        server = await fake_server(ids.server, _record, trusted_certs=[ids.b.cert_pem()])
        port, error, expected_pem = server.port, IdentityMismatch, ids.x.cert_pem()
    calls = count_open_connection(monkeypatch)
    with pytest.raises(error):
        await connection.open_data(LOCAL, port, ids.b, hello_of(ids.b), expected_pem, OPENING)
    await asyncio.sleep(0.3)
    assert len(calls) == 1, f"{len(calls)} lần mở TCP: open_data chỉ được thử một lần"


@integration
async def test_cancelling_open_data_leaves_nothing_behind(fast, monkeypatch, fake_server, ids):
    """Hủy giữa chừng → không sót task hay socket (kiểm bởi no_leaks); server câm thấy client đóng."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 10.0)
    server = await fake_server(None, _hold, use_tls=False)
    task = asyncio.create_task(
        connection.open_data(LOCAL, server.port, ids.b, hello_of(ids.b), ids.server.cert_pem(), OPENING)
    )
    await wait_until(lambda: server.connections == 1, 2.0, "client đã nối TCP tới server câm")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_until(lambda: not server._tasks, 2.0, "socket phía client đã đóng")


@integration
async def test_cancelling_open_data_after_hello_leaves_nothing_behind(
    slow_opening, monkeypatch, fake_server, ids
):
    """Hủy khi đã có `hello` của server (đang gửi data_open hay vừa xong) cũng không để lại gì."""

    async def hello_then_hold(server, reader, writer):
        writer.write(encode_message(wire_hello(ids.server, "Server")))
        await writer.drain()
        await server.record_until_closed(reader, timeout=10)

    server = await fake_server(ids.server, hello_then_hold, trusted_certs=[ids.b.cert_pem()])
    gate = asyncio.Event()
    real_write = asyncio.StreamWriter.write

    def write(writer, data):
        if b'"data_open"' in bytes(data):
            gate.set()
        return real_write(writer, data)

    monkeypatch.setattr(asyncio.StreamWriter, "write", write)
    task = asyncio.create_task(
        connection.open_data(LOCAL, server.port, ids.b, hello_of(ids.b), ids.server.cert_pem(), OPENING)
    )
    await asyncio.wait_for(gate.wait(), 3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        result = await task
        result.abort()  # đã xong trước khi bị hủy: vẫn phải dọn
    await wait_until(lambda: not server._tasks, 3.0, "socket phía client đã đóng")


def _types_in(data: bytes) -> list[str]:
    out: list[str] = []
    while len(data) >= HEADER.size:
        (n,) = HEADER.unpack(data[: HEADER.size])
        if len(data) < HEADER.size + n:
            break
        out.append(decode_body(data[HEADER.size : HEADER.size + n]).get("type"))
        data = data[HEADER.size + n :]
    return out


@integration
async def test_error_sending_data_open_is_unreachable(slow_opening, monkeypatch, fake_server, ids):
    """Gửi data_open lỗi → abort rồi Unreachable (spec mục 3.2, bảng)."""

    async def hello_then_record(server, reader, writer):
        writer.write(encode_message(wire_hello(server.identity, "Server")))
        await writer.drain()
        await server.record_until_closed(reader)

    server = await fake_server(ids.server, hello_then_record, trusted_certs=[ids.b.cert_pem()])
    real_write = asyncio.StreamWriter.write
    real_drain = asyncio.StreamWriter.drain
    failing: set[int] = set()

    def write(writer, data):
        if b'"data_open"' in bytes(data):
            failing.add(id(writer))  # giả lập: socket hỏng đúng lúc gửi data_open
            return None
        return real_write(writer, data)

    async def drain(writer):
        if id(writer) in failing:
            raise ConnectionResetError("giả lập lỗi gửi data_open")
        await real_drain(writer)

    monkeypatch.setattr(asyncio.StreamWriter, "write", write)
    monkeypatch.setattr(asyncio.StreamWriter, "drain", drain)
    with pytest.raises(Unreachable) as exc:
        await connection.open_data(LOCAL, server.port, ids.b, hello_of(ids.b), ids.server.cert_pem(), OPENING)
    assert exc.value.address == LOCAL
    assert failing, "phép thử vô nghĩa: data_open chưa từng được gửi"
    await wait_until(lambda: not server._tasks, 3.0, "server giả thấy kết nối bị đóng (abort)")
    assert "data_open" not in _types_in(bytes(server.received))


# =================================================================================================
# AC-7: hai chiều
# =================================================================================================


@integration
@pytest.mark.parametrize("order", [[0, 1, 2, 3], [3, 0, 2]], ids=["0123", "302"])
async def test_client_writes_blocks_and_server_reads_until_none(
    slow_opening, monkeypatch, listener_factory, ids, order
):
    """AC-7 (a), (c): client ghi ngay sau open_data rồi close(); handler đọc tới None."""
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 10.0)
    blocks = make_blocks(SIZE4, order, seed=7)
    on_connection, results = capture(lambda conn: read_all_blocks(conn, SIZE4))
    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    client = await b_data(h, ids)
    try:
        for i, sha, payload in blocks:
            await client.write_block(i, sha, payload)
        await asyncio.wait_for(client.close(), 15)
        status, value, _ = await asyncio.wait_for(results.get(), 15)
    finally:
        client.abort()
    assert status == "ok", value
    assert value == blocks


@integration
async def test_server_writes_blocks_and_client_reads_until_none(
    slow_opening, monkeypatch, listener_factory, ids
):
    """AC-7 (b)."""
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 10.0)
    blocks = make_blocks(SIZE4, [0, 1, 2, 3], seed=8)

    async def body(conn):
        for i, sha, payload in blocks:
            await conn.write_block(i, sha, payload)
        await conn.close()
        return conn.closed

    on_connection, results = capture(body)
    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    client = await b_data(h, ids)
    try:
        got = await asyncio.wait_for(read_all_blocks(client, SIZE4), 15)
        status, value, _ = await asyncio.wait_for(results.get(), 15)
    finally:
        client.abort()  # bên nhận file nhận None → abort ở phía client (spec mục 3.6)
    assert got == blocks
    assert status == "ok", value


# =================================================================================================
# AC-8 (integ): dùng sai API trên kết nối thật
# =================================================================================================


@integration
async def test_send_and_receive_on_real_data_connection_are_value_error(slow_opening, listener_factory, ids):
    h = await listener_factory(ids.server, trusted=[ids.b])
    client, server = await data_pair(h, ids)
    try:
        for conn in (client, server):
            with pytest.raises(ValueError):
                await conn.send(Ping(t=1))
            with pytest.raises(ValueError):
                await conn.receive()
            assert not conn.closed
        sha = hashlib.sha256(b"abc").digest()
        await client.write_block(0, sha, b"abc")
        got = await asyncio.wait_for(server.read_block(size=3), 2)
        assert got == (BlockHeader(0, 3, sha), bytearray(b"abc")), "byte lạ đã lọt lên dây"
    finally:
        client.abort()


@integration
async def test_data_methods_on_real_control_connection_are_value_error(fast, listener_factory, ids):
    h = await listener_factory(ids.server, trusted=[ids.b])
    conn = await open_control([LOCAL], h.port, ids.b, hello_of(ids.b), ids.server.cert_pem())
    try:
        server = await h.next()
        for c in (conn, server):
            with pytest.raises(ValueError):
                await c.read_raw(1, at_boundary=True)
            with pytest.raises(ValueError):
                await c.read_block(size=1)
            with pytest.raises(ValueError):
                await c.write_raw(b"x")
            with pytest.raises(ValueError):
                await c.write_block(0, bytes(32), b"x")
            assert not c.closed
        await conn.send(Ping(t=3))
        assert await asyncio.wait_for(server.receive(), 2) == Ping(t=3)
    finally:
        await conn.close()


# =================================================================================================
# AC-9 (integ): móc chặn giữ đoạn lại; ghi đồng thời; kiểm đóng sau close_notify
# =================================================================================================


@integration
async def test_blocked_hook_holds_back_its_chunk(slow_opening, monkeypatch, listener_factory, ids):
    """AC-9 (d): móc của đoạn 2 chờ một Event → bên kia không nhận byte nào của đoạn đó tới khi móc trả về."""
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 10.0)
    monkeypatch.setattr(connection, "_SEND_CHUNK", 1024)
    h = await listener_factory(ids.server, trusted=[ids.b])
    client, server = await data_pair(h, ids)
    received = bytearray()

    async def reader():
        while (b := await server.read_raw(1, at_boundary=True, idle_timeout=10)) is not None:
            received.extend(b)

    gate, entered, calls = asyncio.Event(), asyncio.Event(), []

    async def hook(n):
        calls.append(n)
        if len(calls) == 2:
            entered.set()
            await gate.wait()

    data = random.Random(1).randbytes(4096)
    rtask = asyncio.create_task(reader())
    wtask = asyncio.create_task(client.write_raw(data, before_chunk=hook))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await wait_until(lambda: len(received) >= 1024, 2.0, "bên kia nhận đủ đoạn đầu")
        await asyncio.sleep(0.3)
        assert len(received) == 1024, "bên kia đã nhận byte của đoạn mà móc còn đang chặn"
        gate.set()
        await asyncio.wait_for(wtask, 5)
        await wait_until(lambda: len(received) == len(data), 3.0, "bên kia nhận đủ dữ liệu")
        assert received == data
        assert calls == [1024] * 4
    finally:
        gate.set()
        await cancel_wait(wtask, rtask)
        client.abort()


@integration
@pytest.mark.parametrize("hook_sleep", [0.0, 0.2], ids=["raise-at-once", "raise-after-sleep"])
async def test_hook_raising_connection_lost_after_header_cuts_the_connection(
    slow_opening, monkeypatch, listener_factory, ids, hook_sleep
):
    """AC-9 (e) trên socket thật; hồi quy (soat-code P2-3, mức trung bình): móc của đoạn payload đầu ném
    ConnectionLostError (lỗi của một kết nối khác mà móc chờ) khi header đã lên dây → kết nối dữ liệu này bị
    cắt (spec mục 3.3 bước 6). Lần ghi sau không được nối byte vào sau nửa khung, và bên kia không đọc lệch
    khung thành BlockError hay một khối khác.

    - `raise-after-sleep`: móc ngủ rồi mới ném, nên header chắc chắn đã tới máy kia (spec mục 1.4 ý 7: khi móc
      của đoạn payload đầu đang ngủ, header đã nằm trên dây) → bên kia **phải** thấy đứt giữa khung
      (ConnectionLostError, spec mục 3.4 bước 3), không được ra `None`.
    - `raise-at-once`: móc ném ngay sau `write(header)`, không qua vòng lặp sự kiện nào. `abort()` đóng ngay
      transport và không hứa đẩy hết byte còn trong bộ đệm (spec mục 3.6), mà `None` chỉ nghĩa là “không còn
      khung nào” (spec mục 3.4 bước 3, D25), nên bên kia được thấy `None` hoặc ConnectionLostError."""
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 5.0)
    monkeypatch.setattr(connection, "_SEND_CHUNK", 1024)
    size = BLOCK_SIZE + 4096
    (i0, sha0, p0), (i1, sha1, p1) = make_blocks(size, [0, 1], seed=9)
    h = await listener_factory(ids.server, trusted=[ids.b])
    client, server = await data_pair(h, ids)
    error = ConnectionLostError("kết nối điều khiển đã mất")

    async def hook(n):
        if hook_sleep:
            await asyncio.sleep(hook_sleep)
        raise error

    try:
        with pytest.raises(ConnectionLostError) as exc:
            await asyncio.wait_for(client.write_block(i0, sha0, p0, before_chunk=hook), 5)
        assert exc.value is error, "ngoại lệ của móc phải bay ra nguyên trạng"
        assert client.closed, "header đã lên dây mà móc ném ConnectionLostError: phải abort"
        with pytest.raises(ConnectionLostError):
            await asyncio.wait_for(client.write_block(i1, sha1, p1), 5)
        try:  # BlockError (lớp con của ValueError) hay TimeoutError bay ra thì test đỏ
            got = await asyncio.wait_for(server.read_block(size=size, idle_timeout=10), 3)
        except ConnectionLostError as e:
            assert not isinstance(e, connection.IdleTimeout), "bên kia phải thấy kết nối bị cắt ngay"
        else:
            assert got is None, f"bên kia đọc được một khung từ nửa khung: {got!r}"
            assert not hook_sleep, (
                "header đã lên dây trong lúc móc ngủ mà bên kia thấy None (ranh giới khung): "
                "header chưa được ghi trước móc của đoạn payload đầu?"
            )
    finally:
        client.abort()


@integration
async def test_concurrent_write_blocks_arrive_as_intact_frames(
    slow_opening, monkeypatch, listener_factory, ids
):
    """AC-9 (f)."""
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 10.0)
    monkeypatch.setattr(connection, "_SEND_CHUNK", 4096)
    size = BLOCK_SIZE + 10_000
    blocks = make_blocks(size, [0, 1], seed=5)
    h = await listener_factory(ids.server, trusted=[ids.b])
    client, server = await data_pair(h, ids)
    reading = asyncio.create_task(read_all_blocks(server, size))
    try:
        await asyncio.gather(*(client.write_block(i, sha, p) for i, sha, p in blocks))
        await asyncio.wait_for(client.close(), 10)
        got = await asyncio.wait_for(reading, 10)
    finally:
        await cancel_wait(reading)
        client.abort()
    assert sorted(got) == sorted(blocks)


@integration
async def test_write_after_peer_sent_close_notify_is_connection_lost(
    slow_opening, monkeypatch, listener_factory, ids
):
    """AC-9 (h): máy kia ngừng đọc, rồi đóng gọn (close_notify) trong lúc bộ đệm ghi của bên gửi còn dữ liệu
    → write_block kế tiếp (≥ 2 đoạn) ném ConnectionLostError, không trả về bình thường."""
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 5.0)
    may_close = asyncio.Event()

    async def body(conn):
        set_small_buffer(conn, socket.SO_RCVBUF)
        await may_close.wait()  # không đọc gì
        await conn.close()

    on_connection, results = capture(body)
    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    client = await b_data(h, ids)
    try:
        set_small_buffer(client, socket.SO_SNDBUF)
        transport = writer_of(client).transport
        transport.set_write_buffer_limits(high=4 * MiB)
        payload = random.Random(2).randbytes(BLOCK_SIZE)
        sha = hashlib.sha256(payload).digest()
        i = 0
        async with asyncio.timeout(20):
            while transport.get_write_buffer_size() == 0:
                assert i < 64, "đã ghi 64 MiB mà bộ đệm ghi vẫn rỗng: phép thử vô nghĩa"
                await client.write_block(i, sha, payload)
                i += 1
        may_close.set()
        await wait_until(
            lambda: transport.is_closing() or reader_of(client).at_eof(),
            3.0,
            "bên gửi chưa thấy máy kia đóng (is_closing/at_eof): phép thử vô nghĩa",
        )
        with pytest.raises(ConnectionLostError):
            await asyncio.wait_for(client.write_block(i, sha, payload), 5)
        assert client.closed
    finally:
        may_close.set()
        client.abort()
    await asyncio.wait_for(results.get(), 10)


# =================================================================================================
# AC-11 (f): header rồi im lặng → IdleTimeout, bên gửi thấy đóng
# =================================================================================================


@integration
async def test_header_then_silence_is_idle_timeout_and_peer_sees_close(
    slow_opening, listener_factory, raw_client, ids
):
    async def body(conn):
        start = time.monotonic()
        try:
            await conn.read_block(size=1000, idle_timeout=0.5)
        except connection.IdleTimeout:
            return "idle", time.monotonic() - start, conn.closed
        return "no-timeout", time.monotonic() - start, conn.closed

    on_connection, results = capture(body)
    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    sha = bytes(32)
    peer = await raw_opening(
        raw_client, h, ids.b, wire_hello(ids.b), DATA_OPEN, extra=header_bytes(0, 1000, sha) + b"x" * 10
    )
    status, value, _ = await asyncio.wait_for(results.get(), 5)
    assert status == "ok", value
    kind, elapsed, closed = value
    assert kind == "idle"
    assert 0.4 <= elapsed <= 0.5 + 2.0, elapsed
    assert closed
    await peer.expect_closed(2.0)


# =================================================================================================
# AC-12: hạn drain() trên DATA, máy kia không bao giờ đọc
# =================================================================================================


@integration
async def test_write_gives_up_after_drain_timeout_when_peer_never_reads(
    fast, monkeypatch, listener_factory, ids
):
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 0.5)  # trước khi tạo Listener
    fill_limit = 64 * MiB
    payload = random.Random(3).randbytes(BLOCK_SIZE)
    sha = hashlib.sha256(payload).digest()
    progress = {"last": None, "bytes": 0}

    async def hook(n):
        progress["last"] = time.monotonic()
        progress["bytes"] += n

    async def body(conn):
        set_small_buffer(conn, socket.SO_SNDBUF)
        i = 0
        try:
            while True:
                if progress["bytes"] > fill_limit:
                    return "not-stuck", None, conn.closed
                await conn.write_block(i, sha, payload, before_chunk=hook)
                i += 1
        except ConnectionLostError:
            return "lost", time.monotonic(), conn.closed

    on_connection, results = capture(body)
    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    sock = await asyncio.to_thread(sync_open_data, h.port, ids.b, rcvbuf=4096)
    try:
        status, value, _ = await asyncio.wait_for(results.get(), 30)
    finally:
        sock.close()
    assert status == "ok", value
    kind, raised_at, closed = value
    assert kind == "lost", "đã ghi quá FILL_LIMIT mà chưa kẹt: phép thử vô nghĩa"
    assert closed
    gap = raised_at - progress["last"]
    # Kể từ đoạn cuối cùng (kẹt) tới lúc bỏ cuộc: ít nhất DRAIN_TIMEOUT (kẹt thật), nhiều nhất + 2,5 s.
    assert 0.4 <= gap <= 0.5 + 2.5, gap


# =================================================================================================
# AC-13 (integ): hủy giữa khung
# =================================================================================================


class DrainGate:
    """Giữ `StreamWriter.drain` (vá ở mức lớp) từ lần gọi thứ `block_from` khi `armed`."""

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


@integration
async def test_cancelled_write_leaves_peer_with_truncated_frame(
    slow_opening, monkeypatch, listener_factory, ids
):
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 5.0)
    monkeypatch.setattr(connection, "_SEND_CHUNK", 1024)
    h = await listener_factory(ids.server, trusted=[ids.b])
    client, server = await data_pair(h, ids)
    payload = bytes(8192)
    reading = asyncio.create_task(server.read_block(size=8192, idle_timeout=5))
    gate = DrainGate(monkeypatch, block_from=2)
    try:
        gate.armed = True
        task = asyncio.create_task(client.write_block(0, hashlib.sha256(payload).digest(), payload))
        await asyncio.wait_for(gate.blocked.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.open()
        assert client.closed, "bị hủy giữa khung mà kết nối vẫn mở"
        with pytest.raises(ConnectionLostError) as exc:
            await asyncio.wait_for(reading, 3)
        assert not isinstance(exc.value, (BlockError, connection.IdleTimeout))
    finally:
        gate.open()
        await cancel_wait(reading)
        client.abort()


# =================================================================================================
# AC-14: abort và close
# =================================================================================================


@integration
async def test_abort_is_idempotent_wakes_pending_read_and_peer_sees_close(
    slow_opening, listener_factory, ids
):
    """AC-14 (a)."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    client, server = await data_pair(h, ids)
    pending = asyncio.create_task(client.read_raw(1, at_boundary=True, idle_timeout=10))
    await asyncio.sleep(0.05)
    start = time.monotonic()
    client.abort()
    client.abort()
    assert client.closed
    with pytest.raises(ConnectionLostError) as exc:
        await asyncio.wait_for(pending, 2)
    assert not isinstance(exc.value, connection.IdleTimeout)
    outcome = await read_outcome(server, idle=5.0, within=1.5)
    assert outcome in (None, ConnectionLostError), outcome
    assert time.monotonic() - start < 1.0


@pytest.mark.slow
@integration
@pytest.mark.parametrize("sender", ["client", "server"])
async def test_graceful_close_waits_for_slow_reader_without_cutting_the_tail(
    monkeypatch, listener_factory, ids, sender
):
    """AC-14 (b): CLOSE_TIMEOUT = 0,5, DRAIN_TIMEOUT = 5; bên đọc dừng 1,5 s; close() vẫn chờ đọc hết.

    `sender="server"` thử `ssl_shutdown_timeout` của `start_tls` (Listener); `"client"` thử của `open_data`.
    """
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    monkeypatch.setattr(connection, "CLOSE_TIMEOUT", 0.5)
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 5.0)
    size = 3 * BLOCK_SIZE
    blocks = make_blocks(size, [0, 1, 2], seed=3)
    ready = asyncio.Event()
    info: dict = {}

    async def send_role(conn):
        await ready.wait()
        set_small_buffer(conn, socket.SO_SNDBUF)
        transport = writer_of(conn).transport
        transport.set_write_buffer_limits(high=4 * MiB)
        for i, sha, payload in blocks:
            await conn.write_block(i, sha, payload)
        info["buffered"] = transport.get_write_buffer_size()
        info["close_start"] = time.monotonic()
        await conn.close()
        info["close_end"] = time.monotonic()

    async def read_role(conn):
        # 64 KiB chứ không 4 KiB: trên Linux, SO_RCVBUF tí hon cộng ACK trễ làm loopback chỉ
        # còn ~100 KB/s, nên 3 MiB không đọc kịp trong DRAIN_TIMEOUT. 64 KiB vẫn nhỏ hơn
        # nhiều so với 3 MiB, nên lúc close() bộ đệm ghi vẫn còn dữ liệu (assert `buffered`).
        set_small_buffer(conn, socket.SO_RCVBUF, 64 * 1024)
        ready.set()
        await asyncio.sleep(1.5)
        got = []
        while (item := await conn.read_block(size=size, idle_timeout=10)) is not None:
            hdr, payload = item
            got.append((hdr.index, hdr.sha256, bytes(payload)))
            info["last_block_at"] = time.monotonic()
        info["got"] = got

    on_connection, results = capture(read_role if sender == "client" else send_role)
    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    client = await b_data(h, ids)
    try:
        own = send_role if sender == "client" else read_role
        await asyncio.wait_for(own(client), 15)
        status, value, _ = await asyncio.wait_for(results.get(), 15)
    finally:
        client.abort()
    assert status == "ok", value
    assert info["buffered"] > 0, "phép thử vô nghĩa: lúc close() bộ đệm ghi đã rỗng"
    assert info["got"] == blocks, "đóng gọn đã cắt mất đuôi"
    took = info["close_end"] - info["close_start"]
    assert took > 0.5 + 0.3, f"close() trả về sau {took:.2f} s: không chờ bên đọc (dùng CLOSE_TIMEOUT?)"
    assert took <= 5.0 + 1.0
    assert info["close_end"] >= info["last_block_at"] - 0.2, "close() trả về trước khi bên đọc đọc hết"


@pytest.mark.slow
@integration
async def test_data_close_takes_drain_timeout_when_peer_never_answers(
    fast, monkeypatch, listener_factory, ids
):
    """AC-14 (c): máy kia không đọc nên không trả close_notify; close() của DATA mất ≈ DRAIN_TIMEOUT."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 1.0)  # CLOSE_TIMEOUT = 0,5 (fast)

    async def body(conn):
        start = time.monotonic()
        await conn.close()
        return time.monotonic() - start, conn.closed

    on_connection, results = capture(body)
    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    sock = await asyncio.to_thread(sync_open_data, h.port, ids.b)
    try:
        status, value, _ = await asyncio.wait_for(results.get(), 10)
    finally:
        sock.close()
    assert status == "ok", value
    took, closed = value
    assert 1.0 - 0.1 <= took <= 1.0 + 1.0, f"close() của DATA mất {took:.2f} s (cần ≈ DRAIN_TIMEOUT = 1 s)"
    assert closed


@integration
async def test_control_close_stays_bounded_by_close_timeout(fast, monkeypatch, listener_factory, ids):
    """Spec mục 3.6: `ssl_shutdown_timeout` của `start_tls` giờ là max(CLOSE, DRAIN), nhưng `close()` của
    kết nối điều khiển vẫn tự chặn ở CLOSE_TIMEOUT (máy kia không bao giờ trả close_notify)."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 5.0)  # CLOSE_TIMEOUT = 0,5 (fast)

    async def body(conn):
        start = time.monotonic()
        await conn.close()
        return conn.kind, time.monotonic() - start

    on_connection, results = capture(body)
    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    sock = await asyncio.to_thread(sync_open_data, h.port, ids.b, opening={"type": "control_open"})
    try:
        status, value, _ = await asyncio.wait_for(results.get(), 10)
    finally:
        sock.close()
    assert status == "ok", value
    kind, took = value
    assert kind is ConnectionKind.CONTROL
    assert took <= 0.5 + 1.0, f"close() của kết nối điều khiển mất {took:.2f} s > CLOSE_TIMEOUT + 1 s"


@integration
@pytest.mark.parametrize("ending", ["return", "raise"])
async def test_data_handler_ending_without_close_aborts_at_once(
    monkeypatch, listener_factory, ids, caplog, ending
):
    """AC-14 (d), (e): handler DATA trả về / raise mà chưa đóng → Listener cắt (không đóng gọn), dù
    DRAIN_TIMEOUT không vá (30 s) và máy kia không bao giờ trả close_notify."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    assert connection.DRAIN_TIMEOUT == 30.0
    caplog.set_level(logging.DEBUG)
    marker = b"BIMAT-DATA-9c1e"
    tid = "feedfacecafebeef" * 2
    seen: list = []

    async def on_connection(conn):
        await conn.read_raw(len(marker), at_boundary=True, idle_timeout=5)
        seen.append(conn)
        if ending == "raise":
            raise RuntimeError("handler hỏng")

    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    opening = data_open_dict(transfer_id=tid)
    sock = await asyncio.to_thread(sync_open_data, h.port, ids.b, opening=opening, extra=marker)
    try:
        await wait_until(
            lambda: bool(seen) and h.listener.connection_count == 0,
            1.0,
            "Listener chưa cắt kết nối dữ liệu sau khi handler kết thúc",
        )
        assert seen[0].closed
        assert await asyncio.to_thread(peer_sees_close, sock, 1.0) in ("eof", "error")
    finally:
        sock.close()
    if ending == "raise":
        assert any(r.levelno >= logging.ERROR for r in caplog.records), "handler raise phải được ghi ERROR"
    assert marker.decode() not in caplog.text
    assert tid not in caplog.text


# =================================================================================================
# AC-15: Listener.close() khi có DATA đang sống
# =================================================================================================


@integration
async def test_listener_close_aborts_live_data_connections_first(monkeypatch, listener_factory, ids, caplog):
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    monkeypatch.setattr(connection, "CLOSE_TIMEOUT", 0.5)
    assert connection.DRAIN_TIMEOUT == 30.0  # không vá
    caplog.set_level(logging.WARNING)
    outcomes: dict[int, type] = {}
    ready = {k: asyncio.Event() for k in (0, 1, 2)}
    progress = {"last": None}
    payload = bytes(BLOCK_SIZE)
    sha = hashlib.sha256(payload).digest()

    async def hook(n):
        progress["last"] = time.monotonic()

    async def on_connection(conn):
        role = conn.opening.file_index
        try:
            if role == 0:  # rảnh
                ready[0].set()
                await asyncio.Event().wait()
            elif role == 1:  # đang đọc
                ready[1].set()
                await conn.read_block(size=BLOCK_SIZE, idle_timeout=60)
            else:  # đang ghi, kẹt ở drain (máy kia không đọc)
                set_small_buffer(conn, socket.SO_SNDBUF)
                ready[2].set()
                i = 0
                while True:
                    await conn.write_block(i, sha, payload, before_chunk=hook)
                    i += 1
        except BaseException as e:
            outcomes[role] = type(e)
            if isinstance(e, asyncio.CancelledError):
                raise

    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    c0 = await b_data(h, ids, DataOpen(transfer_id=TID, file_index=0, resume_seq=1))
    c1 = await b_data(h, ids, DataOpen(transfer_id=TID, file_index=1, resume_seq=1))
    sock = await asyncio.to_thread(
        sync_open_data, h.port, ids.b, rcvbuf=4096, opening=data_open_dict(file_index=2)
    )
    try:
        for event in ready.values():
            await asyncio.wait_for(event.wait(), 3)
        await wait_until(
            lambda: progress["last"] is not None and time.monotonic() - progress["last"] > 0.5,
            20,
            "handler ghi chưa kẹt",
        )
        start = time.monotonic()
        await asyncio.wait_for(h.listener.close(), 10)
        took = time.monotonic() - start
        await wait_until(lambda: len(outcomes) == 3, 2.0, "mọi handler kết thúc")
    finally:
        c0.abort()
        c1.abort()
        sock.close()
    assert took <= 0.5 + 1.0, f"Listener.close() mất {took:.2f} s"
    stuck = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "không dừng" in r.getMessage()
    ]
    assert not stuck, stuck
    assert outcomes[0] is asyncio.CancelledError
    for role in (1, 2):
        cls = outcomes[role]
        assert cls is asyncio.CancelledError or (
            issubclass(cls, ConnectionLostError) and not issubclass(cls, connection.IdleTimeout)
        ), f"handler {role} kết thúc bằng {cls.__name__}"
    assert h.listener.connection_count == 0


# =================================================================================================
# AC-16: backpressure với bên đọc chậm
# =================================================================================================


class Progress:
    def __init__(self) -> None:
        self.sent = 0  # tổng n qua before_chunk (payload)
        self.read = 0  # tổng payload bên đọc đã nhận
        self.in_flight = False
        self.done = 0  # số lần write_block đã xong


async def send_blocks(conn, n: int, seed: int, prog: Progress) -> str:
    rng = random.Random(seed)
    total = hashlib.sha256()

    async def hook(k):
        prog.sent += k

    for i in range(n):
        payload = rng.randbytes(BLOCK_SIZE)
        total.update(payload)
        prog.in_flight = True
        await conn.write_block(i, hashlib.sha256(payload).digest(), payload, before_chunk=hook)
        prog.in_flight = False
        prog.done += 1
    await conn.close()
    return total.hexdigest()


async def read_slowly(conn, n: int, pause_at: int, prog: Progress, pause: dict) -> tuple[str, list[int]]:
    total = hashlib.sha256()
    indices: list[int] = []
    while (item := await conn.read_block(size=n * BLOCK_SIZE)) is not None:
        hdr, payload = item
        if hashlib.sha256(payload).digest() != hdr.sha256:
            raise AssertionError(f"khối {hdr.index} sai hash")
        total.update(payload)
        indices.append(hdr.index)
        prog.read += len(payload)
        if len(indices) % 8 == 0:
            await asyncio.sleep(0.02)
        if len(indices) == pause_at:
            await asyncio.sleep(1.0)
            sent0, done0 = prog.sent, prog.done
            await asyncio.sleep(0.5)  # 0,5 s cuối của lần dừng 1,5 s
            pause.update(
                delta=prog.sent - sent0,
                in_flight=prog.in_flight,
                same_call=prog.done == done0,
                backlog=prog.sent - prog.read,
            )
    return total.hexdigest(), indices


@pytest.mark.slow
@integration
@pytest.mark.parametrize(
    ("client_sends", "n_blocks", "pause_at"),
    [(True, 256, 128), (False, 64, 32)],
    ids=["client-gui-256MiB-listener-doc-cham", "listener-gui-64MiB-client-doc-cham"],
)
async def test_backpressure_with_slow_reader(
    monkeypatch, listener_factory, ids, client_sends, n_blocks, pause_at
):
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    monkeypatch.setattr(connection, "CLOSE_TIMEOUT", 0.5)
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 10.0)  # trước khi tạo Listener / open_data
    prog = Progress()
    pause: dict = {}
    seed = 2026
    if client_sends:
        on_connection, results = capture(lambda conn: read_slowly(conn, n_blocks, pause_at, prog, pause))
    else:
        on_connection, results = capture(lambda conn: send_blocks(conn, n_blocks, seed, prog))
    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    client = await b_data(h, ids)
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        start = time.monotonic()
        async with asyncio.timeout(120):
            if client_sends:
                sent_hash = await send_blocks(client, n_blocks, seed, prog)
                status, value, _ = await results.get()
                assert status == "ok", value
                read_hash, indices = value
            else:
                read_hash, indices = await read_slowly(client, n_blocks, pause_at, prog, pause)
                status, value, _ = await results.get()
                assert status == "ok", value
                sent_hash = value
        elapsed = time.monotonic() - start
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        if not was_tracing:
            tracemalloc.stop()
        client.abort()
    log.info(
        "AC-16 %d MiB: %.1f MiB/s (gồm 1,5 s dừng), đỉnh tracemalloc %.1f MiB",
        n_blocks,
        n_blocks / elapsed,
        peak / MiB,
    )
    assert read_hash == sent_hash, "SHA-256 tổng hai bên khác nhau"
    assert indices == list(range(n_blocks)), "thiếu khối hoặc sai thứ tự"
    assert pause, "bên đọc chưa tới khối dừng dài"
    assert pause["in_flight"] and pause["same_call"], (
        "phép thử vô nghĩa: bên gửi không bị chặn trong lúc dừng"
    )
    assert pause["delta"] <= MiB, (
        f"bên gửi vẫn tiến {pause['delta']} byte khi bên đọc dừng (không có backpressure)"
    )
    assert pause["backlog"] <= 64 * MiB, f"đã ghi trừ đã đọc = {pause['backlog'] / MiB:.1f} MiB"
    assert peak <= 32 * MiB, f"đỉnh bộ nhớ Python {peak / MiB:.1f} MiB > 32 MiB"


# =================================================================================================
# AC-18: log không chứa payload, transfer_id hay PEM
# =================================================================================================


@integration
async def test_logs_never_contain_payload_transfer_id_or_pem(
    slow_opening, listener_factory, raw_client, ids, caplog
):
    caplog.set_level(logging.DEBUG)
    marker = "BIMAT-5d2c"
    mbytes = marker.encode()
    tid = "c0ffee00deadbeef" * 2
    opening = data_open_dict(transfer_id=tid)
    finished: list[int] = []

    async def on_connection(conn):
        mode = conn.opening.file_index
        if mode == 0:  # BlockError
            with contextlib.suppress(BlockError):
                await conn.read_block(size=100, idle_timeout=5)
        elif mode == 1:  # IdleTimeout
            with contextlib.suppress(connection.IdleTimeout):
                await conn.read_block(size=100, idle_timeout=0.3)
        elif mode == 2:  # abort sau khi đọc payload
            await conn.read_raw(len(mbytes), at_boundary=True, idle_timeout=5)
            conn.abort()
        else:  # handler raise sau khi đọc payload
            await conn.read_raw(len(mbytes), at_boundary=True, idle_timeout=5)
            raise RuntimeError("handler hỏng")
        finished.append(mode)

    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    # Các lần từ chối khi mở đầu, mỗi lần mang marker và transfer_id.
    rejected = [
        await raw_opening(raw_client, h, None, wire_hello(ids.anon, f"PC {marker}"), opening),
        await raw_opening(
            raw_client, h, ids.b, wire_hello(ids.b), {**opening, "file_index": -1, "x": marker}
        ),
        await raw_opening(raw_client, h, ids.b, wire_hello(ids.x, f"X {marker}"), opening),
    ]
    stale = await raw_opening(raw_client, h, ids.b, wire_hello(ids.b, f"B {marker}"), None)
    await asyncio.sleep(0.1)
    h.trusted_fps.clear()
    stale.send_dict(opening)
    await stale.drain()
    for p in [*rejected, stale]:
        await p.expect_closed(2.0)
    h.trusted_fps.add(ids.b.fingerprint)
    # Các lần lỗi sau khi đã giao.
    sha = bytes(32)
    await raw_opening(
        raw_client,
        h,
        ids.b,
        wire_hello(ids.b),
        {**opening, "file_index": 0},
        header_bytes(0, 100, sha, 0x03) + mbytes * 5,
    )
    await raw_opening(
        raw_client,
        h,
        ids.b,
        wire_hello(ids.b),
        {**opening, "file_index": 1},
        header_bytes(0, 100, sha) + mbytes,
    )
    await raw_opening(raw_client, h, ids.b, wire_hello(ids.b), {**opening, "file_index": 2}, mbytes)
    await raw_opening(raw_client, h, ids.b, wire_hello(ids.b), {**opening, "file_index": 3}, mbytes)
    await wait_until(
        lambda: sorted(finished) == [0, 1, 2] and h.listener.connection_count == 0,
        5.0,
        "mọi kết nối kết thúc",
    )
    await asyncio.sleep(0.1)
    text = caplog.text
    assert marker not in text
    assert tid not in text
    assert "BEGIN CERTIFICATE" not in text
    assert ids.b.cert_pem().splitlines()[1] not in text
    assert any(r.levelno >= logging.ERROR for r in caplog.records), "phép thử vô nghĩa: không có log ERROR"


# =================================================================================================
# Ca biên mục 5.3 cần socket
# =================================================================================================


@integration
async def test_peer_abort_at_frame_boundary_reads_as_none_or_connection_lost(
    slow_opening, monkeypatch, listener_factory, ids
):
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 10.0)
    size = 2 * BLOCK_SIZE
    blocks = make_blocks(size, [0, 1], seed=9)
    h = await listener_factory(ids.server, trusted=[ids.b])
    client, server = await data_pair(h, ids)

    async def read_two():
        return [await server.read_block(size=size, idle_timeout=5) for _ in blocks]

    reading = asyncio.create_task(read_two())  # đọc song song: có backpressure thật
    try:
        for i, sha, payload in blocks:
            await client.write_block(i, sha, payload)
        got = await asyncio.wait_for(reading, 10)
        assert got == [(BlockHeader(i, len(p), sha), p) for i, sha, p in blocks]
    finally:
        await cancel_wait(reading)
        client.abort()  # đúng ở ranh giới khung: bên đọc đã nhận trọn hai khung
    outcome = await read_outcome(server, idle=5.0, within=3.0)
    assert outcome in (None, ConnectionLostError), outcome


@pytest.mark.slow
@integration
async def test_bytes_sent_back_by_receiver_do_not_bloat_sender(
    slow_opening, monkeypatch, listener_factory, raw_client, ids
):
    """Bên nhận file gửi lũ byte ngược vào kết nối mà bên gửi file (Listener) đang ghi: `net` không đọc
    chúng (StreamReader tự chặn), bên gửi không phình bộ nhớ và vẫn ghi xong hoặc bị cắt gọn."""
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 2.0)
    n = 16
    blocks = make_blocks(n * BLOCK_SIZE, range(n), seed=11)
    start_writing = asyncio.Event()

    async def body(conn):
        await start_writing.wait()
        written = 0
        try:
            for i, sha, payload in blocks:
                await conn.write_block(i, sha, payload)
                written += 1
        except ConnectionLostError:
            return "cut", written, None
        t0 = time.monotonic()
        await conn.close()
        return "done", written, time.monotonic() - t0

    on_connection, results = capture(body)
    h = await listener_factory(ids.server, trusted=[ids.b], on_connection=on_connection)
    peer = await raw_opening(raw_client, h, ids.b, wire_hello(ids.b), DATA_OPEN)
    counters = {"junk": 0, "read": 0}

    async def flood():
        junk = b"\x99" * 65536
        with contextlib.suppress(ConnectionError, ssl.SSLError, RuntimeError):
            while True:
                peer.writer.write(junk)
                await peer.writer.drain()
                counters["junk"] += len(junk)

    async def consume():
        with contextlib.suppress(ConnectionError, ssl.SSLError):
            while chunk := await peer.reader.read(65536):
                counters["read"] += len(chunk)

    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    flooder = asyncio.create_task(flood())
    consumer = asyncio.create_task(consume())
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        await asyncio.sleep(1.0)
        j0 = counters["junk"]
        await asyncio.sleep(0.5)
        stalled = counters["junk"] - j0
        start_writing.set()
        status, value, _ = await asyncio.wait_for(results.get(), 30)
        peak = tracemalloc.get_traced_memory()[1] - base
    finally:
        start_writing.set()
        if not was_tracing:
            tracemalloc.stop()
        await cancel_wait(flooder, consumer)
    assert stalled <= 256 * 1024, (
        f"byte gửi ngược vẫn chảy ({stalled} byte/0,5 s): bên gửi file đang đọc chúng"
    )
    assert status == "ok", value
    kind, written, close_took = value
    assert kind in ("done", "cut")
    if kind == "done":
        assert written == n
        assert close_took <= 2.0 + 1.0
    assert peak <= 16 * MiB, f"đỉnh bộ nhớ Python {peak / MiB:.1f} MiB"
