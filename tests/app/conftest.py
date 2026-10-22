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
    # PR-4 (đặc tả PR-4 mục 4.1): teleport.core.links và teleport.app.node
    RETRY_BASE=0.2,
    RETRY_MAX=1.0,
    EVENT_RETRY_GAP=0.2,
    REPLACE_AFTER=2.0,
    NETWORK_CHECK_INTERVAL=0.2,
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
    """Ghi lại callback của một Node: lời hỏi, kết quả, sự kiện `NodeEvent`, kèm thời điểm."""

    def __init__(
        self,
        answer=None,
        *,
        raise_in_prompt: bool = False,
        raise_in_finished: bool = False,
        raise_in_event: bool = False,
    ) -> None:
        self.answer = answer
        self.raise_in_prompt = raise_in_prompt
        self.raise_in_finished = raise_in_finished
        self.raise_in_event = raise_in_event
        self.events: list = []  # NodeEvent theo thứ tự nhận (PR-4)
        self.event_times: list[float] = []  # time.monotonic()
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

    def on_event(self, event) -> None:
        self.events.append(event)
        self.event_times.append(time.monotonic())
        if self.raise_in_event:
            raise RuntimeError("lỗi giả lập trong on_event")

    def events_of(self, kind: type, device_id: str | None = None) -> list:
        return [
            e
            for e in self.events
            if isinstance(e, kind) and (device_id is None or e.device.device_id == device_id)
        ]

    async def wait_event(
        self, kind: type, device_id: str | None = None, n: int = 1, timeout: float = 5.0
    ) -> object:
        name = getattr(kind, "__name__", str(kind))
        await wait_until(
            lambda: len(self.events_of(kind, device_id)) >= n, timeout, f"chưa có {n} sự kiện {name}"
        )
        return self.events_of(kind, device_id)[n - 1]

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


class FakeMdnsControl:
    """Bộ điều khiển `FakeMdns` (đặc tả PR-4 mục 4.1).

    - `instances`: mọi `FakeMdns` theo thứ tự tạo; `last`: cái mới nhất.
    - `fail_start` / `fail_advertise`: ngoại lệ ném ra ở `start()` / `advertise()` (giữ tới khi gán None).
    - `close_delay`: `close()` chờ chừng ấy giây (không che chắn khỏi hủy, như yêu cầu nghiêm nhất).
    - `seed`: thiết bị “đã thấy sẵn”, được báo `DeviceAdded` ngay sau `start_browsing` (vòng lặp kế).
    - `local_addresses`: giá trị `teleport.net.discovery.local_ipv4_addresses()` trả về; `addr_reads`
      đếm số lần đọc. Gán một exception để giả lỗi đọc card mạng.
    - `log`: (số thứ tự instance, tên phương thức) theo thứ tự toàn cục (NF6).
    """

    def __init__(self) -> None:
        self.instances: list[FakeMdns] = []
        self.fail_start: BaseException | None = None
        self.fail_advertise: BaseException | None = None
        self.close_delay: float = 0.0
        self.seed: dict = {}
        self.local_addresses: tuple[str, ...] | BaseException = ("192.0.2.5",)
        self.addr_reads = 0
        self.log: list[tuple[int, str]] = []
        self.hang: str | None = None  # tên phương thức async treo mãi (tới khi bị hủy)

    @property
    def last(self) -> FakeMdns:
        assert self.instances, "chưa có FakeMdns nào được tạo"
        return self.instances[-1]

    def of(self, node) -> FakeMdns:
        """Instance đang sống mới nhất mà `node` đã `start_browsing` (theo `self_device_id`)."""
        device_id = node.identity.device_id
        mine = [m for m in self.instances if m.self_device_id == device_id and not m.closing]
        assert mine, f"node {device_id[:8]} không có FakeMdns đang duyệt"
        return mine[-1]

    def read_addresses(self) -> tuple[str, ...]:
        self.addr_reads += 1
        value = self.local_addresses
        if isinstance(value, BaseException):
            raise value
        return tuple(value)


class FakeMdns:
    """Bản giả `teleport.net.discovery.MdnsDiscovery`: không socket, sự kiện do test phát (`emit`)."""

    def __init__(self, ctl: FakeMdnsControl, **kwargs) -> None:
        self.ctl = ctl
        self.kwargs = kwargs
        self.index = len(ctl.instances)
        self.calls: list[str] = []
        self.advertised: list[dict] = []
        self.priorities: list[frozenset[str]] = []
        self.started = False
        self.closing = False  # đã gọi close(): không phát sự kiện nữa
        self.closed = False  # close() đã chạy xong
        self.self_device_id: str | None = None
        self.on_event = None
        self.max_services: int | None = None
        self._devices: dict = {}
        ctl.instances.append(self)

    def _call(self, name: str) -> None:
        self.calls.append(name)
        self.ctl.log.append((self.index, name))

    async def _maybe_hang(self, name: str) -> None:
        if self.ctl.hang == name:
            await asyncio.Event().wait()

    async def start(self) -> None:
        self._call("start")
        await self._maybe_hang("start")
        if self.ctl.fail_start is not None:
            raise self.ctl.fail_start
        self.started = True

    async def close(self) -> None:
        self._call("close")
        self.closing = True
        if self.closed:
            return
        if self.ctl.close_delay:
            await asyncio.sleep(self.ctl.close_delay)
        self.closed = True
        self.ctl.log.append((self.index, "closed"))

    async def advertise(self, **kw) -> None:
        self._call("advertise")
        await self._maybe_hang("advertise")
        if self.ctl.fail_advertise is not None:
            raise self.ctl.fail_advertise
        self.advertised.append(dict(kw))

    async def stop_advertising(self) -> None:
        self._call("stop_advertising")

    async def start_browsing(self, *, self_device_id: str, on_event, max_services: int = 256) -> None:
        self._call("start_browsing")
        await self._maybe_hang("start_browsing")
        self.self_device_id = self_device_id
        self.on_event = on_event
        self.max_services = max_services
        if self.ctl.seed:
            from teleport.core.discovery import DeviceAdded

            seeded = [d for d in self.ctl.seed.values() if d.device_id != self_device_id]
            loop = asyncio.get_running_loop()
            for device in seeded:
                loop.call_soon(self.emit, DeviceAdded(device))

    def set_priority(self, device_ids) -> None:
        self._call("set_priority")
        self.priorities.append(frozenset(device_ids))

    @property
    def devices(self):
        from types import MappingProxyType

        return MappingProxyType(dict(self._devices))

    def emit(self, event) -> None:
        """Cập nhật `devices` rồi gọi `on_event` (như MdnsDiscovery); sau `close()` thì im lặng."""
        from teleport.core.discovery import DeviceRemoved

        if self.closing:
            return
        if isinstance(event, DeviceRemoved):
            self._devices.pop(event.device.device_id, None)
        else:
            self._devices[event.device.device_id] = event.device
        if self.on_event is not None:
            self.on_event(event)


class OpenControlCounter:
    """Bọc `teleport.app.node.open_control`: ghi mọi lời gọi (đặc tả PR-4 mục 4.1).

    Mỗi bản ghi: `SimpleNamespace(t, caller, fingerprint, addresses, port)`; `t` = time.monotonic().
    `rewrite_to="127.0.0.1"` → gọi hàm thật với địa chỉ đó thay cho địa chỉ mDNS (M2).
    """

    def __init__(self, real, rewrite_to: str | None) -> None:
        self.real = real
        self.rewrite_to = rewrite_to
        self.calls: list[SimpleNamespace] = []
        self._sig = inspect.signature(real)

    async def __call__(self, *args, **kwargs):
        bound = self._sig.bind(*args, **kwargs)
        a = bound.arguments
        self.calls.append(
            SimpleNamespace(
                t=time.monotonic(),
                caller=a["identity"].device_id,
                fingerprint=a["expected_fingerprint"],
                addresses=tuple(a["addresses"]),
                port=a["port"],
            )
        )
        if self.rewrite_to is not None:
            bound.arguments["addresses"] = [self.rewrite_to]
        return await self.real(*bound.args, **bound.kwargs)

    def count(self, *, to=None, caller=None) -> int:
        return len(self.of(to=to, caller=caller))

    def of(self, *, to=None, caller=None) -> list[SimpleNamespace]:
        """`to`: AppNode / Identity / fingerprint của máy đích; `caller`: AppNode / device_id của máy gọi."""
        if to is None or isinstance(to, str):
            fp = to
        elif isinstance(to, AppNode):
            fp = to.identity.fingerprint
        else:
            fp = to.fingerprint  # Identity
        cid = None if caller is None else (caller if isinstance(caller, str) else caller.identity.device_id)
        return [
            c for c in self.calls if (fp is None or c.fingerprint == fp) and (cid is None or c.caller == cid)
        ]


def make_discovered(
    target,
    *,
    addresses: tuple[str, ...] = (LOCAL,),
    port: int | None = None,
    name: str | None = None,
    os: str = "windows",
    device_id: str | None = None,
):
    """`DiscoveredDevice` trỏ tới `target` (AppNode thật, hoặc Identity khi có `port`)."""
    from teleport.core.discovery import DiscoveredDevice

    if isinstance(target, AppNode):
        did = target.id
        default_name = target.name
        default_port = target.port
    else:
        did = target.device_id
        default_name = "B"
        default_port = None
    return DiscoveredDevice(
        device_id=device_id or did,
        name=name or default_name,
        os=os,
        proto=PROTOCOL_VERSION,
        port=port if port is not None else default_port,
        addresses=tuple(addresses),
    )


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
        monkeypatch.setattr(app_node, "NETWORK_CHECK_INTERVAL", FAST.NETWORK_CHECK_INTERVAL, raising=False)
    try:
        from teleport.core import links
    except ImportError:
        links = None
    if links is not None:
        for name in ("RETRY_BASE", "RETRY_MAX", "EVENT_RETRY_GAP", "REPLACE_AFTER"):
            monkeypatch.setattr(links, name, getattr(FAST, name))
    return FAST


@pytest.fixture
def fake_mdns(monkeypatch):
    """Thay `teleport.app.node.MdnsDiscovery` bằng `FakeMdns`; trả `FakeMdnsControl`.

    Cũng thay `teleport.net.discovery.local_ipv4_addresses` (node đọc qua thuộc tính module) bằng
    `ctl.read_addresses`, để việc theo dõi mạng không phụ thuộc card mạng thật của máy chạy test.
    """
    from teleport.app import node as app_node
    from teleport.net import discovery as net_discovery

    ctl = FakeMdnsControl()

    def factory(*args, **kwargs):
        assert not args, f"MdnsDiscovery phải được tạo bằng đối số từ khóa, nhận {args!r}"
        return FakeMdns(ctl, **kwargs)

    monkeypatch.setattr(app_node, "MdnsDiscovery", factory)
    monkeypatch.setattr(net_discovery, "local_ipv4_addresses", ctl.read_addresses)
    return ctl


@pytest.fixture
def count_open_control(monkeypatch):
    """`count_open_control(rewrite_to=None)` → `OpenControlCounter` đã cài vào `teleport.app.node`."""
    from teleport.app import node as app_node

    installed: list[OpenControlCounter] = []

    def install(rewrite_to: str | None = None) -> OpenControlCounter:
        real = installed[0].real if installed else app_node.open_control
        counter = OpenControlCounter(real, rewrite_to)
        monkeypatch.setattr(app_node, "open_control", counter)
        installed.append(counter)
        return counter

    return install


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
        mdns=None,
        auto_connect: bool = True,
        raise_in_event: bool = False,
    ) -> AppNode:
        directory = tmp_path / label
        rec = Recorder(
            answer,
            raise_in_prompt=raise_in_prompt,
            raise_in_finished=raise_in_finished,
            raise_in_event=raise_in_event,
        )
        # PR-4: `mdns`/`auto_connect` chỉ truyền khi khác mặc định; `on_event` luôn ghi vào `rec.events`.
        extra: dict = {"on_event": rec.on_event}
        if mdns is not None:
            extra["mdns"] = mdns
        if auto_connect is not True:
            extra["auto_connect"] = auto_connect
        node = Node(
            directory,
            name=None if hostname else (name or label),
            port=port,
            host=host,
            accept_pairing=accept_pairing,
            on_pairing_prompt=rec.on_prompt,
            on_pairing_finished=rec.on_finished,
            **extra,
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
        dev=make_discovered,
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
