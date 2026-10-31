"""Fixture dùng chung cho test tầng net (đặc tả PR-2 mục 4.1).

`teleport.net.connection` chỉ được import **bên trong** fixture: nếu module chưa có hoặc lỗi,
chỉ các test dùng tới nó hỏng, các test net khác (`test_tls`, `test_discovery`…) vẫn chạy.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import gc
import inspect
import ssl
import time
import uuid
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtensionOID, NameOID
from support import wait_until

from teleport.core.framing import MAX_MESSAGE_SIZE, encode_message
from teleport.core.messages import PROTOCOL_VERSION, Hello
from teleport.net import tls
from teleport.net.stream import read_message
from teleport.trust.identity import (
    CERT_FILE,
    KEY_FILE,
    Identity,
    fingerprint_of_der,
    load_or_create_identity,
)

# Các cách một kết nối bị đóng mà phía đọc có thể thấy. Windows hay báo ConnectionResetError
# (WinError 10054) hoặc ConnectionAbortedError (10053) thay cho EOF (đặc tả mục 3.6).
CLOSED_ERRORS: tuple[type[BaseException], ...] = (ConnectionError, ssl.SSLError)

HEX64 = "ab" * 32

# SAN chứa GeneralName mà `cryptography` không hỗ trợ (DER thô của extension). OpenSSL vẫn nạp và
# bắt tay được với chứng chỉ mang chúng, còn `cryptography` ném UnsupportedGeneralNameType (kế thừa
# thẳng từ Exception, không từ ValueError) khi đọc extension (đợt kiểm tra M1).
UNSUPPORTED_SAN: dict[str, bytes] = {
    "x400": b"\x30\x02\xa3\x00",  # SEQUENCE { x400Address [3] rỗng }
    # SEQUENCE { ediPartyName [5] { partyName "ab" } }; bản [5] rỗng thì OpenSSL không nạp làm cert server.
    "edi": bytes.fromhex("3008a506a1040c026162"),
}


def write_identity_with_san(config_dir: Path, san_der: bytes) -> Identity:
    """Danh tính trông như của Teleport (P-256, CN đúng dạng, tự ký, ECDSA-SHA256) nhưng SAN là `san_der`."""
    config_dir.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    device_id = uuid.uuid4().hex
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "teleport-" + device_id)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC))
        .not_valid_after(datetime.datetime(9999, 12, 31, 23, 59, 59, tzinfo=datetime.UTC))
        .add_extension(
            x509.UnrecognizedExtension(ExtensionOID.SUBJECT_ALTERNATIVE_NAME, san_der), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = config_dir / CERT_FILE
    key_path = config_dir / KEY_FILE
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return Identity(
        device_id=device_id,
        cert_path=cert_path,
        key_path=key_path,
        fingerprint=fingerprint_of_der(cert.public_bytes(serialization.Encoding.DER)),
    )


def connection_module():
    from teleport.net import connection

    return connection


def make_hello(identity: Identity, name: str = "A", **overrides: object) -> Hello:
    fields: dict = {
        "protocol": PROTOCOL_VERSION,
        "device_id": identity.device_id,
        "name": name,
        "app_version": "0.0.0",
        "os": "windows",
    }
    fields.update(overrides)
    return Hello(**fields)


def hello_dict(identity_or_id: Identity | str, name: str = "A", **overrides: object) -> dict:
    """`hello` dạng dict trên dây, cho client thô; `overrides` có thể làm hỏng bất kỳ trường nào."""
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


async def expect_closed(reader: asyncio.StreamReader, within: float) -> tuple[float, bytes]:
    """Đọc tới khi thấy kết nối đóng (EOF, reset, lỗi SSL) trong `within` giây.

    Trả về (thời gian chờ, các byte đọc được trước khi đóng). Hết giờ mà vẫn mở → AssertionError.
    """
    start = time.monotonic()
    data = bytearray()
    try:
        async with asyncio.timeout(within):
            while True:
                try:
                    chunk = await reader.read(65536)
                except CLOSED_ERRORS:
                    break
                if not chunk:
                    break
                data += chunk
    except TimeoutError:
        raise AssertionError(f"kết nối vẫn mở sau {within} s (đã nhận {len(data)} byte)") from None
    return time.monotonic() - start, bytes(data)


def cert_client_context(identity: Identity) -> ssl.SSLContext:
    """Client gửi chứng chỉ của `identity` nhưng KHÔNG kiểm chứng chỉ server (`CERT_NONE`).

    Chỉ để thử phía server (mTLS, giới hạn, message mở đầu sai). Sản phẩm không còn đường nào như
    vậy: chứng chỉ client chỉ đi tới server đã được kiểm bằng `tls.trusted_client_context`
    (`docs/protocol.md` mục 2.1). Chép ở `tests/app/conftest.py` và `tests/net/test_connection.py`
    (không import được conftest).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(identity.cert_path, identity.key_path)
    return ctx


class RawPeer:
    """Client (TLS hoặc TCP trần) tự soạn từng byte gửi đi, để thử phía server.

    Có `identity` thì gửi chứng chỉ đó mà không kiểm server (`cert_client_context`); không có thì
    ẩn danh (`tls.client_context()`).
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, started: float) -> None:
        self.reader = reader
        self.writer = writer
        self.started = started  # time.monotonic() lúc bắt đầu kết nối

    @classmethod
    async def connect(
        cls,
        port: int,
        identity: Identity | None = None,
        *,
        use_tls: bool = True,
        ctx: ssl.SSLContext | None = None,
        timeout: float = 5.0,
    ) -> RawPeer:
        started = time.monotonic()
        ssl_ctx = None
        if use_tls:
            if ctx is not None:
                ssl_ctx = ctx
            elif identity is not None:
                ssl_ctx = cert_client_context(identity)
            else:
                ssl_ctx = tls.client_context()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port, ssl=ssl_ctx), timeout
        )
        return cls(reader, writer, started)

    def send_bytes(self, data: bytes) -> None:
        with contextlib.suppress(*CLOSED_ERRORS, RuntimeError):
            self.writer.write(data)

    def send_dict(self, msg: dict) -> None:
        self.send_bytes(encode_message(msg))

    async def drain(self) -> None:
        with contextlib.suppress(*CLOSED_ERRORS, RuntimeError):
            await self.writer.drain()

    async def read(self, timeout: float = 2.0, max_size: int = MAX_MESSAGE_SIZE) -> dict | None:
        return await asyncio.wait_for(read_message(self.reader, max_size), timeout)

    async def read_hello(self, timeout: float = 2.0) -> dict:
        msg = await self.read(timeout)
        assert msg is not None and msg.get("type") == "hello", f"server không gửi hello trước: {msg!r}"
        return msg

    async def expect_closed(self, within: float) -> tuple[float, bytes]:
        return await expect_closed(self.reader, within)

    async def stays_open(self, duration: float) -> bool:
        """True nếu trong `duration` giây kết nối không bị đóng (dữ liệu tới thì bỏ qua)."""
        try:
            await expect_closed(self.reader, duration)
        except AssertionError:
            return True
        return False

    async def close_gracefully(self, timeout: float = 2.0) -> None:
        self.writer.close()
        try:
            async with asyncio.timeout(timeout):
                await self.writer.wait_closed()
        except (TimeoutError, *CLOSED_ERRORS):
            self.abort()

    def abort(self) -> None:
        self.writer.transport.abort()
        with contextlib.suppress(Exception):
            self.writer.close()


class ListenerHarness:
    """Một `Listener` thật trên 127.0.0.1 kèm `on_connection` mặc định của test.

    `on_connection` mặc định đưa kết nối vào `accepted` rồi chờ `release`, để kết nối sống tới
    khi test cho phép. `trusted_fps` là kho tin cậy giả mà `is_trusted` đọc, test sửa trực tiếp.
    """

    def __init__(self, identity: Identity, hello: Hello) -> None:
        self.identity = identity
        self.hello = hello
        self.listener = None
        self.port: int | None = None
        self.accepted: asyncio.Queue = asyncio.Queue()
        self.calls = 0
        self.release = asyncio.Event()
        self.trusted_fps: set[str] = set()

    def is_trusted(self, fingerprint: str) -> bool:
        return fingerprint in self.trusted_fps

    async def on_connection(self, conn) -> None:
        self.calls += 1
        self.accepted.put_nowait(conn)
        await self.release.wait()

    async def next(self, timeout: float = 2.0):
        return await asyncio.wait_for(self.accepted.get(), timeout)

    async def wait_idle(self, timeout: float = 2.0) -> None:
        """Chờ tới khi không còn kết nối nào đang mở đầu."""
        await wait_until(lambda: self.listener.opening_count == 0, timeout, "opening_count không về 0")


class FakeServer:
    """Server giả (TLS với `server_context`, hoặc TCP trần) chạy kịch bản tùy ý để thử phía client.

    `script(server, reader, writer)` chạy cho mỗi kết nối. `received` ghi mọi byte client gửi
    nếu kịch bản gọi `record_until_closed`.
    """

    def __init__(
        self,
        identity: Identity | None,
        script: Callable[[FakeServer, asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
        *,
        trusted_certs: list[str] | None = None,
        use_tls: bool = True,
    ) -> None:
        self.identity = identity
        self.script = script
        self.trusted_certs = trusted_certs or []
        self.use_tls = use_tls
        self.received = bytearray()
        self.connections = 0
        self.port: int | None = None
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> int:
        ctx = None
        if self.use_tls:
            ctx = tls.server_context(self.identity, self.trusted_certs, allow_anonymous=True)
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=ctx)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.append(writer)
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            await self.script(self, reader, writer)
        except (*CLOSED_ERRORS, asyncio.IncompleteReadError):
            pass
        finally:
            self._tasks.discard(task)

    async def record_until_closed(self, reader: asyncio.StreamReader, timeout: float = 10.0) -> None:
        with contextlib.suppress(TimeoutError, *CLOSED_ERRORS):
            async with asyncio.timeout(timeout):
                while chunk := await reader.read(65536):
                    self.received += chunk

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        for writer in self._writers:
            writer.transport.abort()
            with contextlib.suppress(Exception):
                writer.close()
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=2)
        if self._server is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), 2)


# --- Fixture -------------------------------------------------------------------------------------


@pytest.fixture
def make_identity(tmp_path):
    """`make_identity(name)`: danh tính riêng trong `tmp_path / name` (gọi lại cùng tên → cùng danh tính)."""
    cache: dict[str, Identity] = {}

    def make(name: str) -> Identity:
        if name not in cache:
            cache[name] = load_or_create_identity(tmp_path / name)
        return cache[name]

    return make


@pytest.fixture
def make_san_identity(tmp_path):
    """`make_san_identity("x400" | "edi")` → Identity có SAN chứa GeneralName lạ (M1), trong `tmp_path`."""

    def make(kind: str) -> Identity:
        return write_identity_with_san(tmp_path / f"san-{kind}", UNSUPPORTED_SAN[kind])

    return make


@pytest.fixture
def cert_client():
    """`cert_client(identity)` → context client gửi chứng chỉ mà không kiểm server (chỉ thử phía server)."""
    return cert_client_context


@pytest.fixture
def hello_for():
    """`hello_for(identity, name="A")` → `Hello` hợp lệ của danh tính đó."""
    return make_hello


@pytest.fixture
def connmod():
    """Module `teleport.net.connection` (import muộn)."""
    return connection_module()


@pytest.fixture
def fast(monkeypatch):
    """Rút ngắn thời gian chờ còn 0,5 s. Patch qua module, trước khi tạo Listener hay gọi open_*."""
    mod = connection_module()
    for name in ("OPENING_TIMEOUT", "CLOSE_TIMEOUT", "DRAIN_TIMEOUT"):
        monkeypatch.setattr(mod, name, 0.5)
    return mod


@pytest.fixture
async def listener_factory(make_identity):
    """`await listener_factory(identity=None, trusted=(), on_connection=None, ...)` → `ListenerHarness`.

    Listener nghe trên 127.0.0.1, cổng 0, và luôn được `close()` khi test kết thúc.
    `allow_anonymous` (C2, chế độ ẩn) chỉ được truyền cho `Listener` khi khác None.
    """
    harnesses: list[ListenerHarness] = []

    async def make(
        identity: Identity | None = None,
        *,
        trusted: tuple[Identity, ...] | list[Identity] = (),
        on_connection=None,
        is_trusted=None,
        name: str = "Server",
        start: bool = True,
        allow_anonymous=None,
    ) -> ListenerHarness:
        mod = connection_module()
        identity = identity if identity is not None else make_identity("server")
        h = ListenerHarness(identity, make_hello(identity, name))
        h.trusted_fps = {t.fingerprint for t in trusted}
        extra = {} if allow_anonymous is None else {"allow_anonymous": allow_anonymous}
        h.listener = mod.Listener(
            identity,
            h.hello,
            [t.cert_pem() for t in trusted],
            is_trusted if is_trusted is not None else h.is_trusted,
            on_connection if on_connection is not None else h.on_connection,
            **extra,
        )
        harnesses.append(h)
        if start:
            h.port = await h.listener.start("127.0.0.1", 0)
        return h

    yield make

    for h in harnesses:
        h.release.set()
        await asyncio.wait_for(h.listener.close(), 10)


@pytest.fixture
async def raw_client():
    """`await raw_client(port, identity=None, use_tls=True, ctx=None)` → `RawPeer`; tự abort khi test xong."""
    peers: list[RawPeer] = []

    async def connect(port: int, identity: Identity | None = None, **kwargs) -> RawPeer:
        peer = await RawPeer.connect(port, identity, **kwargs)
        peers.append(peer)
        return peer

    yield connect

    for peer in peers:
        peer.abort()
    await asyncio.sleep(0)


@pytest.fixture
async def fake_server():
    """`await fake_server(identity, script, trusted_certs=None, use_tls=True)` → `FakeServer` đã start."""
    servers: list[FakeServer] = []

    async def make(identity, script, **kwargs) -> FakeServer:
        server = FakeServer(identity, script, **kwargs)
        servers.append(server)
        await server.start()
        return server

    yield make

    for server in servers:
        await server.close()


@pytest.fixture
def expect_closed_fn():
    """`await expect_closed_fn(reader, within)` → (thời gian, byte đã đọc); còn mở → AssertionError."""
    return expect_closed


@pytest.fixture
def net_helpers():
    """Các lớp và hàm phụ trợ, cho test đồng bộ tự chạy `asyncio.run` (ví dụ fuzz R15)."""
    return SimpleNamespace(
        RawPeer=RawPeer,
        FakeServer=FakeServer,
        ListenerHarness=ListenerHarness,
        make_hello=make_hello,
        hello_dict=hello_dict,
        cert_client_context=cert_client_context,
        expect_closed=expect_closed,
        wait_until=wait_until,
        find_leaks=find_leaks,
        connection_module=connection_module,
        CLOSED_ERRORS=CLOSED_ERRORS,
    )


async def find_leaks(before: set[asyncio.Task]) -> list[str]:
    """Task và socket còn sót sau một test integration (đặc tả mục 4.1, "Kiểm rò rỉ")."""
    problems: list[str] = []
    await asyncio.sleep(0.05)
    current = asyncio.current_task()
    leftover = [t for t in asyncio.all_tasks() if t is not current and t not in before and not t.done()]
    problems += [f"task còn chạy: {t.get_name()} {t.get_coro()!r}" for t in leftover]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        gc.collect()
    problems += [f"ResourceWarning: {w.message}" for w in caught if issubclass(w.category, ResourceWarning)]
    return problems


@pytest.fixture
async def no_leaks(request):
    """Sau test async: không còn task mới nào (kể cả `teleport.connection*`), không có socket chưa đóng.

    Test đồng bộ (unit, hoặc tự chạy `asyncio.run` như fuzz R15) thì bỏ qua: chúng tự kiểm.
    """
    if not inspect.iscoroutinefunction(request.function):
        yield
        return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()  # dọn rác của các test trước, để không đổ lỗi nhầm cho test này
    before = asyncio.all_tasks()
    yield
    problems = await find_leaks(before)
    assert not problems, "rò rỉ sau test:\n" + "\n".join(problems)
