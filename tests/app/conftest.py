"""Fixture dùng chung cho test tầng app (đặc tả PR-3 mục 4.1).

`tests/net/conftest.py` không dùng chung được (`--import-mode=importlib`), nên các phần cần dùng
được chép lại ở đây. Module `teleport.app.*` chỉ được import **bên trong** fixture: nếu một module
chưa có, chỉ các test dùng tới nó hỏng (ví dụ `test_settings.py` vẫn chạy khi `node.py` chưa có).

Hàm phụ trợ (không phải fixture) lấy qua fixture `app_helpers`, vì test không import được conftest.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import gc
import inspect
import secrets
import socket
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
from cryptography.x509.oid import NameOID

from teleport.core.framing import HEADER, MAX_PAIRING_MESSAGE_SIZE, FramingError, encode_message
from teleport.core.messages import PROTOCOL_VERSION
from teleport.core.pairing_code import NONCE_SIZE, make_commitment, pairing_code
from teleport.lock import NodeLock
from teleport.net import tls
from teleport.net.stream import read_message
from teleport.trust.identity import Identity, load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore

LOCAL = "127.0.0.1"
# Các cách một kết nối bị đóng mà phía đọc có thể thấy (Windows hay báo reset thay cho EOF).
CLOSED_ERRORS: tuple[type[BaseException], ...] = (ConnectionError, ssl.SSLError)

# Hằng thời gian sau khi patch (mục 4.1); test đọc từ đây để tính biên.
FAST = SimpleNamespace(
    MESSAGE_TIMEOUT=1.0,
    CONFIRM_TIMEOUT=2.0,
    CONFIRM_GRACE=0.5,
    BACKOFF_BASE=0.5,
    BACKOFF_MAX=2.0,
    OPENING_TIMEOUT=2.0,
    CLOSE_TIMEOUT=0.5,
    DRAIN_TIMEOUT=1.0,
    SEND_TIMEOUT=1.0,
    BUSY_REPLY_TIMEOUT=0.5,
    ABORT_ON_CANCEL_TIMEOUT=0.5,
    CLOSE_SESSIONS_TIMEOUT=1.0,
    LOCK_TIMEOUT=0.5,
    SLACK=2.5,  # biên trên nới cho CI: thời gian chờ + 2,5 s
)


# --- Hàm phụ trợ ---------------------------------------------------------------------------------


async def wait_until(predicate: Callable[[], bool], timeout: float = 2.0, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.01)


async def expect_closed(reader: asyncio.StreamReader, within: float) -> tuple[float, bytes]:
    """Đọc tới khi thấy kết nối đóng (EOF, reset, lỗi SSL) trong `within` giây; trả (thời gian, byte)."""
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


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((LOCAL, 0))
        return s.getsockname()[1]
    finally:
        s.close()


@contextlib.contextmanager
def busy_port():
    """Một socket đang listen trên 127.0.0.1; trả cổng của nó."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((LOCAL, 0))
        s.listen(1)
        yield s.getsockname()[1]
    finally:
        s.close()


def hello_dict(identity_or_id: Identity | str, name: str = "Raw", **overrides: object) -> dict:
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


def frame(body: bytes) -> bytes:
    return HEADER.pack(len(body)) + body


def fake_cert_with_cn(device_id: str, *, ca: bool = False) -> str:
    """Chứng chỉ tự ký P-256 mới, CN `teleport-<device_id>`: danh tính khác, cùng device_id (P12).

    `ca=True` thêm BasicConstraints CA=true (P21: phải bị từ chối).
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "teleport-" + device_id)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC))
        .not_valid_after(datetime.datetime(9999, 12, 31, tzinfo=datetime.UTC))
    )
    if ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    cert = builder.sign(key, hashes.SHA256())
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def ensure_identity(directory: Path) -> Identity:
    return load_or_create_identity(directory)


def seed_trust(directory: Path, pem: str, name: str) -> None:
    """Thêm một mục vào `trusted.json` của `directory` khi node chưa chạy (tạo danh tính trước)."""
    ensure_identity(directory)
    TrustStore.load(directory).add(peer_cert_from_pem(pem), name)


def lock_is_free(directory: Path) -> bool:
    lock = NodeLock(directory)
    try:
        lock.acquire()
    except Exception:
        return False
    lock.release()
    return True


class RawPeer:
    """Client TLS (ẩn danh hoặc có chứng chỉ) hay TCP trần, tự soạn từng byte gửi đi."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.server_hello: dict | None = None

    @classmethod
    async def connect(
        cls, port: int, identity: Identity | None = None, *, use_tls: bool = True, timeout: float = 5.0
    ) -> RawPeer:
        ctx = tls.client_context(identity) if use_tls else None
        reader, writer = await asyncio.wait_for(asyncio.open_connection(LOCAL, port, ssl=ctx), timeout)
        return cls(reader, writer)

    def send_bytes(self, data: bytes) -> None:
        with contextlib.suppress(*CLOSED_ERRORS, RuntimeError):
            self.writer.write(data)

    def send_dict(self, msg: dict) -> None:
        self.send_bytes(encode_message(msg))

    async def drain(self) -> None:
        with contextlib.suppress(*CLOSED_ERRORS, RuntimeError):
            await self.writer.drain()

    async def read(self, timeout: float = 3.0) -> dict | None:
        return await asyncio.wait_for(read_message(self.reader, MAX_PAIRING_MESSAGE_SIZE), timeout)

    async def read_hello(self, timeout: float = 3.0) -> dict:
        msg = await self.read(timeout)
        assert msg is not None and msg.get("type") == "hello", f"server không gửi hello trước: {msg!r}"
        self.server_hello = msg
        return msg

    async def read_type(self, type_name: str, timeout: float = 3.0) -> dict:
        msg = await self.read(timeout)
        assert msg is not None, f"kết nối đóng trước khi nhận {type_name}"
        assert msg.get("type") == type_name, f"cần {type_name}, nhận {msg!r}"
        return msg

    async def read_until_closed(self, timeout: float = 5.0) -> list[dict]:
        """Đọc mọi message tới khi kết nối đóng (EOF/reset/lỗi khung), tối đa `timeout`."""
        out: list[dict] = []
        try:
            async with asyncio.timeout(timeout):
                while True:
                    try:
                        msg = await read_message(self.reader, MAX_PAIRING_MESSAGE_SIZE)
                    except (*CLOSED_ERRORS, asyncio.IncompleteReadError, FramingError, ValueError):
                        break
                    if msg is None:
                        break
                    out.append(msg)
        except TimeoutError:
            raise AssertionError(f"kết nối vẫn mở sau {timeout} s; đã nhận {out!r}") from None
        return out

    async def expect_closed(self, within: float) -> tuple[float, bytes]:
        return await expect_closed(self.reader, within)

    def abort(self) -> None:
        with contextlib.suppress(Exception):
            self.writer.transport.abort()
        with contextlib.suppress(Exception):
            self.writer.close()


class PairingClient(RawPeer):
    """Client ghép đôi thô (đóng vai bên khởi xướng): tự tính cam kết và nonce."""

    identity: Identity
    nonce: bytes

    async def begin(self, identity: Identity, name: str = "Raw", *, nonce: bytes | None = None) -> None:
        """Đọc hello server, gửi hello và `pair_request` hợp lệ."""
        self.identity = identity
        self.nonce = nonce if nonce is not None else secrets.token_bytes(NONCE_SIZE)
        if self.server_hello is None:
            await self.read_hello()
        self.send_dict(hello_dict(identity, name))
        self.send_dict(
            {
                "type": "pair_request",
                "cert": identity.cert_pem(),
                "commitment": make_commitment(identity.fingerprint, self.nonce),
            }
        )
        await self.drain()

    def code_for(self, responder_fingerprint: str, challenge_nonce_hex: str) -> str:
        return pairing_code(
            self.identity.fingerprint, responder_fingerprint, self.nonce, bytes.fromhex(challenge_nonce_hex)
        )


class Recorder:
    """Ghi lại callback của một Node: lời hỏi, kết quả, kèm thời điểm."""

    def __init__(
        self, answer=None, *, raise_in_prompt: bool = False, raise_in_finished: bool = False
    ) -> None:
        self.answer = answer
        self.raise_in_prompt = raise_in_prompt
        self.raise_in_finished = raise_in_finished
        self.node = None
        self.prompts: list = []
        self.finished: list = []
        self.prompt_times: list[float] = []  # time.monotonic()
        self.prompt_loop_times: list[float] = []  # loop.time()
        self.finished_times: list[float] = []
        self.gate_active_at_finish: list = []
        self.decide_results: list[bool] = []
        self.prompt_event = asyncio.Event()
        self.finished_event = asyncio.Event()

    def on_prompt(self, prompt) -> None:
        self.prompts.append(prompt)
        self.prompt_times.append(time.monotonic())
        self.prompt_loop_times.append(asyncio.get_running_loop().time())
        self.prompt_event.set()
        if callable(self.answer):
            self.answer(prompt)
        elif isinstance(self.answer, bool):
            self.decide_results.append(self.node.pairing.decide(prompt.session_id, self.answer))
        if self.raise_in_prompt:
            raise RuntimeError("lỗi giả lập trong on_prompt")

    def on_finished(self, result) -> None:
        self.finished.append(result)
        self.finished_times.append(time.monotonic())
        with contextlib.suppress(Exception):
            self.gate_active_at_finish.append(self.node.pairing.gate.active)
        self.finished_event.set()
        if self.raise_in_finished:
            raise RuntimeError("lỗi giả lập trong on_finished")

    async def wait_prompts(self, n: int = 1, timeout: float = 5.0) -> None:
        await wait_until(lambda: len(self.prompts) >= n, timeout, f"chưa có {n} lời hỏi")

    async def wait_finished(self, n: int = 1, timeout: float = 5.0) -> None:
        await wait_until(lambda: len(self.finished) >= n, timeout, f"chưa có {n} kết quả")


class AppNode:
    """Bọc một `Node` thật: chuyển mọi thuộc tính tới node, thêm `rec`, `dir`, `label`, `id`."""

    _OWN = frozenset({"node", "rec", "dir", "label"})

    def __init__(self, node, rec: Recorder, directory: Path, label: str) -> None:
        object.__setattr__(self, "node", node)
        object.__setattr__(self, "rec", rec)
        object.__setattr__(self, "dir", directory)
        object.__setattr__(self, "label", label)

    @property
    def id(self) -> str:
        return self.node.identity.device_id

    def __getattr__(self, name: str):
        return getattr(self.node, name)

    def __setattr__(self, name: str, value) -> None:
        if name in self._OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self.node, name, value)

    def __repr__(self) -> str:
        return f"<AppNode {self.label}>"


class ControlClient:
    """Kết nối điều khiển phía client, chạy `ControlChannel` trong một task."""

    def __init__(self, conn, task: asyncio.Task, received: list) -> None:
        self.conn = conn
        self.task = task
        self.received = received

    def __iter__(self):
        return iter((self.conn, self.task))

    def pongs(self) -> int:
        return sum(1 for m in self.received if getattr(m, "TYPE", None) == "pong")


async def find_leaks(before: set[asyncio.Task]) -> list[str]:
    problems: list[str] = []
    await asyncio.sleep(0.05)
    current = asyncio.current_task()
    leftover = [t for t in asyncio.all_tasks() if t is not current and t not in before and not t.done()]
    named = [t for t in leftover if t.get_name().startswith(("teleport.pairing", "teleport.node"))]
    problems += [f"task của app còn chạy: {t.get_name()} {t.get_coro()!r}" for t in named]
    problems += [f"task còn chạy: {t.get_name()} {t.get_coro()!r}" for t in leftover if t not in named]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        gc.collect()
    problems += [f"ResourceWarning: {w.message}" for w in caught if issubclass(w.category, ResourceWarning)]
    return problems


# --- Fixture -------------------------------------------------------------------------------------


@pytest.fixture
def fast(monkeypatch):
    """Rút ngắn hằng thời gian (mục 4.1). Patch qua module, trước khi tạo Node."""
    from teleport.core import pairing as core_pairing
    from teleport.net import connection

    for name in ("MESSAGE_TIMEOUT", "CONFIRM_TIMEOUT", "CONFIRM_GRACE", "BACKOFF_BASE", "BACKOFF_MAX"):
        monkeypatch.setattr(core_pairing, name, getattr(FAST, name))
    for name in ("OPENING_TIMEOUT", "CLOSE_TIMEOUT", "DRAIN_TIMEOUT"):
        monkeypatch.setattr(connection, name, getattr(FAST, name))
    try:
        from teleport.app import pairing as app_pairing
    except ImportError:
        app_pairing = None
    if app_pairing is not None:
        for name in (
            "SEND_TIMEOUT",
            "BUSY_REPLY_TIMEOUT",
            "ABORT_ON_CANCEL_TIMEOUT",
            "CLOSE_SESSIONS_TIMEOUT",
        ):
            monkeypatch.setattr(app_pairing, name, getattr(FAST, name))
    try:
        from teleport.app import node as app_node
    except ImportError:
        app_node = None
    if app_node is not None:
        monkeypatch.setattr(app_node, "LOCK_TIMEOUT", FAST.LOCK_TIMEOUT)
    return FAST


_LABEL = object()


@pytest.fixture
async def node_factory(fast, tmp_path):
    """`await node_factory("A", accept_pairing=False, name=None, answer=None, ...)` → `AppNode`.

    Node ở `tmp_path / label`, nghe `127.0.0.1`, cổng 0 (mặc định), luôn được `close()` khi test
    xong; sau đó kiểm `node.lock` của mọi thư mục đã chạy đều lấy lại được ngay.
    `answer=True/False` → trong `on_prompt` gọi `decide`; `answer=callable` → gọi với prompt.
    `hostname=True` → truyền `name=None` cho Node (tên lấy từ hostname).
    """
    from teleport.app.node import Node

    created: list[AppNode] = []
    started_dirs: set[Path] = set()

    async def make(
        label: str,
        *,
        accept_pairing: bool = False,
        name: str | None = None,
        answer=None,
        port: int | None = 0,
        host: str = LOCAL,
        start: bool = True,
        hostname: bool = False,
        raise_in_prompt: bool = False,
        raise_in_finished: bool = False,
    ) -> AppNode:
        directory = tmp_path / label
        rec = Recorder(answer, raise_in_prompt=raise_in_prompt, raise_in_finished=raise_in_finished)
        node = Node(
            directory,
            name=None if hostname else (name or label),
            port=port,
            host=host,
            accept_pairing=accept_pairing,
            on_pairing_prompt=rec.on_prompt,
            on_pairing_finished=rec.on_finished,
        )
        rec.node = node
        handle = AppNode(node, rec, directory, label)
        created.append(handle)
        if start:
            await node.start()
            started_dirs.add(directory)
        return handle

    yield make

    for handle in reversed(created):
        await asyncio.wait_for(handle.node.close(), 20)
    still_locked = [str(d) for d in started_dirs if not lock_is_free(d)]
    assert not still_locked, f"node.lock chưa được nhả sau close(): {still_locked}"


@pytest.fixture
def trust_each_other(tmp_path):
    """`trust_each_other(a, b, name_a="A", name_b="B")`: hai thư mục (Path hoặc nhãn) tin nhau.

    Gọi **trước** khi start node: tạo danh tính hai bên rồi ghi `trusted.json` của từng bên.
    """

    def run(a: Path | str, b: Path | str, name_a: str = "A", name_b: str = "B") -> tuple[Identity, Identity]:
        dir_a = a if isinstance(a, Path) else tmp_path / a
        dir_b = b if isinstance(b, Path) else tmp_path / b
        id_a, id_b = ensure_identity(dir_a), ensure_identity(dir_b)
        TrustStore.load(dir_a).add(peer_cert_from_pem(id_b.cert_pem()), name_b)
        TrustStore.load(dir_b).add(peer_cert_from_pem(id_a.cert_pem()), name_a)
        return id_a, id_b

    return run


@pytest.fixture
async def raw_pairing_client():
    """`await raw_pairing_client(port, read_hello=True)` → `PairingClient` ẩn danh; tự abort khi xong."""
    peers: list[RawPeer] = []

    async def connect(port: int, *, read_hello: bool = True) -> PairingClient:
        peer = await PairingClient.connect(port)
        peers.append(peer)
        if read_hello:
            await peer.read_hello()
        return peer

    yield connect

    for peer in peers:
        peer.abort()
    await asyncio.sleep(0)


@pytest.fixture
async def raw_client():
    """`await raw_client(port, identity=None, use_tls=True)` → `RawPeer` (chưa đọc gì)."""
    peers: list[RawPeer] = []

    async def connect(port: int, identity: Identity | None = None, *, use_tls: bool = True) -> RawPeer:
        peer = await RawPeer.connect(port, identity, use_tls=use_tls)
        peers.append(peer)
        return peer

    yield connect

    for peer in peers:
        peer.abort()
    await asyncio.sleep(0)


@pytest.fixture
async def control_client(node_factory):
    """`await control_client(node_from, node_to, ping_interval=0.2)` → `ControlClient`.

    `open_control` từ danh tính của `node_from` tới cổng của `node_to`, rồi chạy `ControlChannel`
    phía client trong một task. Phụ thuộc `node_factory` để được dọn **trước** khi node đóng.
    """
    from teleport.net.connection import ControlChannel, open_control

    clients: list[ControlClient] = []

    async def make(
        node_from, node_to, *, ping_interval: float = 0.2, dead_after: float = 5.0
    ) -> ControlClient:
        conn = await open_control(
            [LOCAL], node_to.port, node_from.identity, node_from.hello, node_to.identity.fingerprint
        )
        received: list = []
        channel = ControlChannel(conn, ping_interval=ping_interval, dead_after=dead_after)
        task = asyncio.create_task(channel.run(received.append), name="test.control-client")
        client = ControlClient(conn, task, received)
        clients.append(client)
        return client

    yield make

    for client in clients:
        client.task.cancel()
    for client in clients:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(client.task, 5)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(client.conn.close(), 5)


@pytest.fixture
async def fake_server():
    """`await fake_server(identity, script)` → cổng của server TLS giả chạy `script(reader, writer)`."""
    servers: list[asyncio.Server] = []
    writers: list[asyncio.StreamWriter] = []
    tasks: set[asyncio.Task] = set()

    async def make(
        identity: Identity | None,
        script: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
        *,
        use_tls: bool = True,
    ) -> int:
        ctx = tls.server_context(identity, [], allow_anonymous=True) if use_tls else None

        async def handle(reader, writer):
            writers.append(writer)
            task = asyncio.current_task()
            tasks.add(task)
            try:
                await script(reader, writer)
            except (*CLOSED_ERRORS, asyncio.IncompleteReadError):
                pass
            finally:
                tasks.discard(task)

        server = await asyncio.start_server(handle, LOCAL, 0, ssl=ctx)
        servers.append(server)
        return server.sockets[0].getsockname()[1]

    yield make

    for server in servers:
        server.close()
    for task in list(tasks):
        task.cancel()
    for writer in writers:
        with contextlib.suppress(Exception):
            writer.transport.abort()
        with contextlib.suppress(Exception):
            writer.close()
    if tasks:
        await asyncio.wait(list(tasks), timeout=2)
    for server in servers:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(server.wait_closed(), 2)


@pytest.fixture
def app_helpers():
    """Hàm và lớp phụ trợ (test không import được conftest)."""
    return SimpleNamespace(
        FAST=FAST,
        LOCAL=LOCAL,
        CLOSED_ERRORS=CLOSED_ERRORS,
        wait_until=wait_until,
        expect_closed=expect_closed,
        free_port=free_port,
        busy_port=busy_port,
        hello_dict=hello_dict,
        frame=frame,
        fake_cert_with_cn=fake_cert_with_cn,
        ensure_identity=ensure_identity,
        seed_trust=seed_trust,
        lock_is_free=lock_is_free,
        find_leaks=find_leaks,
        new_device_id=lambda: uuid.uuid4().hex,
        read_message=read_message,
        encode_message=encode_message,
    )


@pytest.fixture
async def no_leaks(request):
    """Sau test async: không còn task mới nào (kể cả `teleport.pairing*`, `teleport.node*`,
    `teleport.connection*`), không có socket chưa đóng. Test đồng bộ thì bỏ qua."""
    if not inspect.iscoroutinefunction(request.function):
        yield
        return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()
    before = asyncio.all_tasks()
    yield
    problems = await find_leaks(before)
    assert not problems, "rò rỉ sau test:\n" + "\n".join(problems)
