"""Kết nối dữ liệu qua seam không socket (spec P2-3 mục 2, 3.3–3.6; AC-8, 9, 10, 11, 13; mục 5.3, 5.4).

Viết từ spec, không từ code. Test dựng thẳng `Connection(kind=DATA, ...)` với:
- `StrictReader`: một `asyncio.StreamReader` thật (tạo trong loop đang chạy; test cấp byte bằng
  `feed_data`/`feed_eof`), chỉ cho đường đọc dùng `read(k)` với 1 ≤ k ≤ `_READ_CHUNK` (đọc lúc gọi) và
  `at_eof`; `readexactly`/`readline`/`readuntil` hay `read(-1)` là lỗi (spec mục 3.4 bước 2).
- `FakeWriter`: chỉ có đúng các thuộc tính spec liệt kê (`write`, `drain`, `close`, `wait_closed`,
  `transport.abort`, `transport.is_closing`, `get_extra_info`). Nó mô phỏng asyncio ở chỗ cần:
  `transport.abort()` (và đóng gọn xong) làm `StreamReader` thấy EOF như `connection_lost`, và đánh thức
  `drain()` đang chờ.

Thuộc tính `hypothesis` H1–H5 (spec mục 5.4) là test đồng bộ tự chạy `asyncio.run` cho mỗi ví dụ.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import inspect
import math
import random
import ssl
import struct
import time
import tracemalloc

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from teleport.core.blocks import BLOCK_SIZE, MAX_FILE_SIZE, BlockError, BlockHeader
from teleport.core.framing import FramingError
from teleport.core.messages import PROTOCOL_VERSION, ConnectionKind, DataOpen, Hello, Ping
from teleport.net import connection
from teleport.net.connection import ConnectionLostError, PeerInfo
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem

pytestmark = [
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

HDR = struct.Struct("!BII32s")  # protocol §4.2: type, block_index, length, sha256 (big-endian)
HDR_SIZE = 41
SHA = bytes(range(32))  # hash bất kỳ: tầng mạng không băm và không kiểm hash
OPENING = DataOpen(transfer_id="0123456789abcdef" * 2, file_index=0, resume_seq=1)

PROPERTY = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
        HealthCheck.data_too_large,
    ],
)
TEST_TIMEOUT = 30.0  # mỗi test seam xong trong vài giây; quá thì là treo (cài sai), không chờ mãi


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


def run_bounded(coro) -> None:
    """`asyncio.run` cho một ví dụ hypothesis, có hạn để cài sai không làm treo."""
    asyncio.run(asyncio.wait_for(coro, TEST_TIMEOUT))


# --- Seam ----------------------------------------------------------------------------------------


class StrictReader(asyncio.StreamReader):
    """`StreamReader` thật; ghi lại mọi `read(k)` và từ chối mọi cách đọc khác của tầng mạng."""

    def __init__(self) -> None:
        super().__init__()
        self.sizes: list[int] = []
        self.violations: list[str] = []
        self.strict = True

    async def read(self, n: int = -1) -> bytes:
        if self.strict:
            self.sizes.append(n)
            limit = connection._READ_CHUNK
            if not (isinstance(n, int) and 1 <= n <= limit):
                self.violations.append(f"read({n!r}) ngoài 1…_READ_CHUNK ({limit})")
                raise AssertionError(self.violations[-1])
        return await super().read(n)

    async def readexactly(self, n: int) -> bytes:
        if self.strict:
            self.violations.append(f"readexactly({n})")
            raise AssertionError("kết nối dữ liệu không được dùng readexactly (spec mục 3.4 bước 2)")
        return await super().readexactly(n)

    async def readuntil(self, separator: bytes = b"\n") -> bytes:
        if self.strict:
            self.violations.append("readuntil")
            raise AssertionError("kết nối dữ liệu không được dùng readuntil")
        return await super().readuntil(separator)

    async def readline(self) -> bytes:
        if self.strict:
            self.violations.append("readline")
            raise AssertionError("kết nối dữ liệu không được dùng readline")
        return await super().readline()

    async def rest(self) -> bytes:
        """Mọi byte còn lại trong bộ đệm: byte mà tầng mạng chưa tiêu thụ (đặt EOF trước khi đọc hết)."""
        self.strict = False
        self.feed_eof()
        return await super().read()


class FakeTransport:
    def __init__(self, writer: FakeWriter) -> None:
        self._writer = writer
        self.closing = False
        self.aborted = 0

    def abort(self) -> None:
        self.aborted += 1
        self.closing = True
        self._writer._lost()

    def is_closing(self) -> bool:
        return self.closing


class FakeWriter:
    """Writer giả: giữ nguyên **object** nhận được ở `write` (không chép), để thấy được tầng mạng có chép
    đoạn của bên gọi hay không; `drain` có thể bị giữ từ lần gọi thứ `hold_from`, hoặc ném `drain_error`."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader
        self.transport = FakeTransport(self)
        self.writes: list[object] = []
        self.drain_calls = 0
        self.hold_from: int | None = None
        self.held = asyncio.Event()
        self._unblock = asyncio.Event()
        self.drain_error: BaseException | None = None
        self.hang_close = False
        self.close_calls = 0
        self.lost = False

    # Đúng các thuộc tính spec cho phép.
    def write(self, data) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        self.drain_calls += 1
        if self.drain_error is not None:
            raise self.drain_error
        if self.hold_from is not None and self.drain_calls >= self.hold_from and not self._unblock.is_set():
            self.held.set()
            await self._unblock.wait()
        await asyncio.sleep(0)

    def close(self) -> None:
        self.close_calls += 1
        self.transport.closing = True

    async def wait_closed(self) -> None:
        if self.hang_close:
            await asyncio.Event().wait()  # máy kia không bao giờ trả close_notify
        self._lost()
        await asyncio.sleep(0)

    def get_extra_info(self, name, default=None):
        return default

    # Phần của test.
    def release(self) -> None:
        self._unblock.set()

    def _lost(self) -> None:
        """Như `connection_lost`: reader thấy EOF, drain đang chờ được đánh thức."""
        if not self.lost:
            self.lost = True
            self._reader.feed_eof()
            self._unblock.set()

    @property
    def nbytes(self) -> int:
        return sum(memoryview(w).nbytes for w in self.writes)

    @property
    def data(self) -> bytes:
        return b"".join(bytes(memoryview(w).cast("B")) for w in self.writes)


@pytest.fixture(scope="module")
def peer(tmp_path_factory) -> PeerInfo:
    identity = load_or_create_identity(tmp_path_factory.mktemp("frames-peer"))
    hello = Hello(
        protocol=PROTOCOL_VERSION, device_id=identity.device_id, name="Peer", app_version="0.0.0", os="linux"
    )
    return PeerInfo(
        address="192.0.2.7", port=47474, hello=hello, cert=peer_cert_from_pem(identity.cert_pem())
    )


def make_conn(peer: PeerInfo, *, kind: ConnectionKind = ConnectionKind.DATA, is_server: bool = False):
    """(conn, reader, writer); gọi bên trong loop đang chạy (StreamReader phải tạo trong loop)."""
    reader = StrictReader()
    writer = FakeWriter(reader)
    opening = OPENING if (is_server and kind is ConnectionKind.DATA) else None
    conn = connection.Connection(
        kind=kind,
        peer=peer,
        is_server=is_server,
        opening=opening,
        opened_at=0.0,
        reader=reader,
        writer=writer,
    )
    return conn, reader, writer


def header(index: int, length: int, sha: bytes = SHA, type_: int = 0x02) -> bytes:
    return HDR.pack(type_, index, length, sha)


def block_count(size: int) -> int:
    return -(-size // BLOCK_SIZE)


def block_length(size: int, index: int) -> int:
    return min(BLOCK_SIZE, size - index * BLOCK_SIZE)


def header_is_valid(type_: int, index: int, length: int, size: int) -> bool:
    """Oracle theo protocol §4.2: type 0x02, length 1…1 MiB, index < số khối, length đúng độ dài khối."""
    return (
        type_ == 0x02
        and 1 <= length <= BLOCK_SIZE
        and index < block_count(size)
        and length == block_length(size, index)
    )


def check_hook_trace(calls: list[tuple[int, int]], *, prefix: int, total: int, chunk: int) -> None:
    """`calls`: (n, số byte writer đã nhận lúc móc được gọi) cho từng lần gọi móc (AC-9 b)."""
    assert len(calls) == -(-total // chunk), f"móc được gọi {len(calls)} lần, cần ⌈{total}/{chunk}⌉"
    sent = 0
    for k, (n, seen) in enumerate(calls):
        assert 1 <= n <= chunk, f"lần {k}: n = {n} ngoài 1…{chunk}"
        assert seen == prefix + sent, (
            f"lần {k}: writer đã nhận {seen} byte lúc gọi móc, cần {prefix + sent} "
            "(móc chạy trước byte của đoạn đó, sau khi đoạn trước đã ghi)"
        )
        sent += n
    assert sent == total


class Hook:
    """Móc `before_chunk` ghi lại (n, số byte writer đã nhận); có thể ném ở lần gọi `raise_on` hoặc
    chờ `gate` ở lần gọi `block_on` (đánh số từ 1)."""

    def __init__(
        self, writer: FakeWriter, *, raise_on: int | None = None, block_on: int | None = None
    ) -> None:
        self.writer = writer
        self.calls: list[tuple[int, int]] = []
        self.raise_on = raise_on
        self.block_on = block_on
        self.error = HookError("móc giới hạn tốc độ hỏng")
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def __call__(self, n: int) -> None:
        self.calls.append((n, self.writer.nbytes))
        if len(self.calls) == self.raise_on:
            raise self.error
        if len(self.calls) == self.block_on:
            self.entered.set()
            await self.gate.wait()


class HookError(Exception):
    pass


async def cancel_and_wait(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def feed_pieces(reader: asyncio.StreamReader, data: bytes, cuts, *, eof: bool = True) -> None:
    """Cấp `data` theo các điểm cắt, nhường loop sau mỗi mảnh (bên đọc thật sự phải chờ socket)."""
    pos = 0
    for c in [*sorted(set(cuts)), len(data)]:
        if c > pos:
            reader.feed_data(data[pos:c])
            pos = c
        await asyncio.sleep(0)
    if eof:
        reader.feed_eof()


# =================================================================================================
# AC-8: dùng sai API — ValueError trước mọi I/O, thứ tự kiểm
# =================================================================================================


async def test_receive_on_data_is_value_error_before_io(peer):
    conn, reader, _ = make_conn(peer)
    reader.feed_data(b"\x00\x00\x00\x02{}")
    with pytest.raises(ValueError):
        await conn.receive()
    assert not conn.closed
    assert await reader.rest() == b"\x00\x00\x00\x02{}"


@pytest.mark.parametrize("msg", [Ping(t=1), OPENING], ids=lambda m: type(m).__name__)
async def test_send_on_data_is_value_error_before_io(peer, msg):
    conn, _, writer = make_conn(peer)
    with pytest.raises(ValueError):
        await conn.send(msg)
    assert writer.data == b""
    assert not conn.closed


DATA_CALLS = {
    "read_raw": lambda c: c.read_raw(1, at_boundary=True),
    "read_block": lambda c: c.read_block(size=10),
    "write_raw": lambda c: c.write_raw(b"abc"),
    "write_block": lambda c: c.write_block(0, SHA, b"abc"),
}


@pytest.mark.parametrize("kind", [ConnectionKind.PAIRING, ConnectionKind.CONTROL], ids=str)
@pytest.mark.parametrize("method", list(DATA_CALLS))
async def test_data_methods_on_pairing_or_control_are_value_error(peer, kind, method):
    conn, reader, writer = make_conn(peer, kind=kind)
    reader.feed_data(header(0, 10) + b"x" * 10)
    with pytest.raises(ValueError):
        await DATA_CALLS[method](conn)
    assert writer.data == b""
    assert not conn.closed
    assert reader.sizes == [], "không được đọc socket khi sai loại kết nối"


@pytest.mark.parametrize("method", list(DATA_CALLS))
async def test_wrong_kind_is_checked_before_closed(peer, method):
    """Thứ tự kiểm (spec mục 2): sai loại kết nối → ValueError, kể cả khi kết nối đã đóng."""
    conn, _, _ = make_conn(peer, kind=ConnectionKind.CONTROL)
    conn.abort()
    with pytest.raises(ValueError):
        await DATA_CALLS[method](conn)


BAD_CALLS_ON_CLOSED = {
    "read_raw-n0": lambda c: c.read_raw(0, at_boundary=True),
    "read_block-size-1": lambda c: c.read_block(size=-1),
    "write_raw-str": lambda c: c.write_raw("abc"),
    "write_block-sha31": lambda c: c.write_block(0, bytes(31), b"abc"),
    "read_raw-idle0": lambda c: c.read_raw(1, at_boundary=True, idle_timeout=0),
}


@pytest.mark.parametrize("call", list(BAD_CALLS_ON_CLOSED))
async def test_bad_arguments_are_checked_before_closed(peer, call):
    """Thứ tự kiểm: tham số sai → ValueError (không phải ConnectionLostError) dù kết nối đã đóng."""
    conn, _, _ = make_conn(peer)
    conn.abort()
    with pytest.raises(ValueError):
        await BAD_CALLS_ON_CLOSED[call](conn)


async def test_closed_is_checked_before_concurrent_read(peer):
    """Thứ tự kiểm: đã đóng → ConnectionLostError, đứng trước “đang có read_* khác chờ” (RuntimeError)."""
    conn, _, _ = make_conn(peer)
    first = asyncio.create_task(conn.read_raw(1, at_boundary=True, idle_timeout=5))
    await asyncio.sleep(0.01)
    assert not first.done()
    conn.abort()
    with pytest.raises(ConnectionLostError):
        await conn.read_raw(1, at_boundary=True)
    with pytest.raises(ConnectionLostError):
        await asyncio.wait_for(first, 2)


GOOD_BLOCK = {"index": 0, "sha256": SHA, "payload": b"x" * 10}
BAD_BLOCK_ARGS = {
    "payload-empty": {"payload": b""},
    "payload-1MiB+1": {"payload": bytes(BLOCK_SIZE + 1)},
    "payload-str": {"payload": "abc"},
    "payload-list": {"payload": [1, 2, 3]},
    "payload-none": {"payload": None},
    "payload-int": {"payload": 5},
    "payload-array": {"payload": array.array("B", b"abc")},
    "sha-31": {"sha256": bytes(31)},
    "sha-33": {"sha256": bytes(33)},
    "sha-bytearray": {"sha256": bytearray(32)},
    "sha-memoryview": {"sha256": memoryview(bytes(32))},
    "sha-str": {"sha256": "ab" * 16},
    "sha-none": {"sha256": None},
    "index-negative": {"index": -1},
    "index-2^32": {"index": 1 << 32},
    "index-bool": {"index": True},
    "index-float": {"index": 0.0},
}


@pytest.mark.parametrize("case", list(BAD_BLOCK_ARGS))
async def test_write_block_rejects_bad_arguments_without_writing(peer, case):
    conn, _, writer = make_conn(peer)
    args = {**GOOD_BLOCK, **BAD_BLOCK_ARGS[case]}
    hook = Hook(writer)
    with pytest.raises(ValueError) as exc:
        await conn.write_block(args["index"], args["sha256"], args["payload"], before_chunk=hook)
    assert not isinstance(exc.value, BlockError), "tham số sai của bên gọi không phải BlockError"
    assert writer.data == b"", "tham số sai phải bị chặn trước mọi byte"
    assert hook.calls == []
    assert not conn.closed
    await conn.write_block(0, SHA, b"ok")  # kết nối vẫn dùng được
    assert writer.data == header(0, 2) + b"ok"


@pytest.mark.parametrize("data", ["abc", [1, 2], None, 5, 1.5, array.array("B", b"abc")], ids=repr)
async def test_write_raw_rejects_non_buffer_data(peer, data):
    conn, _, writer = make_conn(peer)
    with pytest.raises(ValueError):
        await conn.write_raw(data)
    assert writer.data == b""
    assert not conn.closed


@pytest.mark.parametrize("method", ["write_raw", "write_block"])
async def test_non_contiguous_memoryview_is_value_error(peer, method):
    conn, _, writer = make_conn(peer)
    strided = memoryview(bytes(range(64)))[::2]
    assert not strided.c_contiguous
    with pytest.raises(ValueError):
        if method == "write_raw":
            await conn.write_raw(strided)
        else:
            await conn.write_block(0, SHA, strided)
    assert writer.data == b""
    assert not conn.closed


async def test_multidimensional_contiguous_memoryview_is_written_flat(peer):
    """Spec mục 3.3 bước 1: C-contiguous nhiều chiều → `cast("B")`; độ dài là `nbytes`."""
    conn, _, writer = make_conn(peer)
    raw = bytes(range(12))
    mv = memoryview(bytearray(raw)).cast("B", (3, 4))
    await conn.write_block(5, SHA, mv)
    assert writer.data == header(5, 12) + raw


@pytest.mark.parametrize("n", [0, -1, BLOCK_SIZE + 1, True, False, 1.0, None, "1"], ids=repr)
async def test_read_raw_rejects_bad_n(peer, n):
    conn, reader, _ = make_conn(peer)
    reader.feed_data(b"abc")
    with pytest.raises(ValueError):
        await conn.read_raw(n, at_boundary=True)
    assert reader.sizes == []
    assert not conn.closed


@pytest.mark.parametrize("at_boundary", [1, 0, None, "True"], ids=repr)
async def test_read_raw_rejects_non_bool_at_boundary(peer, at_boundary):
    conn, reader, _ = make_conn(peer)
    reader.feed_data(b"abc")
    with pytest.raises(ValueError):
        await conn.read_raw(1, at_boundary=at_boundary)
    assert reader.sizes == []


async def test_read_raw_requires_at_boundary(peer):
    conn, _, _ = make_conn(peer)
    with pytest.raises(TypeError):
        await conn.read_raw(1)  # type: ignore[call-arg]


@pytest.mark.parametrize("size", [-1, MAX_FILE_SIZE + 1, True, False, 1.0, None, "1"], ids=repr)
async def test_read_block_rejects_bad_size(peer, size):
    conn, reader, _ = make_conn(peer)
    reader.feed_data(header(0, 1) + b"x")
    with pytest.raises(ValueError) as exc:
        await conn.read_block(size=size)
    assert not isinstance(exc.value, BlockError), "size sai do bên gọi đưa vào không phải BlockError"
    assert reader.sizes == []
    assert not conn.closed


BAD_IDLE = [0, 0.0, -1, -0.5, math.inf, -math.inf, math.nan, True, False, "1"]


@pytest.mark.parametrize("method", ["read_raw", "read_block"])
@pytest.mark.parametrize("idle", BAD_IDLE, ids=repr)
async def test_read_rejects_bad_idle_timeout(peer, method, idle):
    conn, reader, _ = make_conn(peer)
    reader.feed_data(header(0, 1) + b"x")
    with pytest.raises(ValueError):
        if method == "read_raw":
            await conn.read_raw(1, at_boundary=True, idle_timeout=idle)
        else:
            await conn.read_block(size=1, idle_timeout=idle)
    assert reader.sizes == []
    assert not conn.closed


@pytest.mark.parametrize("hook", [5, "x", b"x", object()], ids=repr)
@pytest.mark.parametrize("method", ["write_raw", "write_block"])
async def test_before_chunk_must_be_callable(peer, method, hook):
    conn, _, writer = make_conn(peer)
    with pytest.raises(ValueError):
        if method == "write_raw":
            await conn.write_raw(b"abc", before_chunk=hook)
        else:
            await conn.write_block(0, SHA, b"abc", before_chunk=hook)
    assert writer.data == b""
    assert not conn.closed


@pytest.mark.parametrize(
    "second",
    [
        lambda c: c.read_raw(1, at_boundary=True),
        lambda c: c.read_raw(1, at_boundary=False),
        lambda c: c.read_block(size=10),
    ],
    ids=["read_raw", "read_raw-not-boundary", "read_block"],
)
@pytest.mark.parametrize("first_kind", ["read_raw", "read_block"])
async def test_concurrent_reads_are_runtime_error(peer, first_kind, second):
    conn, reader, _ = make_conn(peer)
    if first_kind == "read_raw":
        first = asyncio.create_task(conn.read_raw(4, at_boundary=True))
    else:
        first = asyncio.create_task(conn.read_block(size=4))
    await asyncio.sleep(0.01)
    with pytest.raises(RuntimeError):
        await second(conn)
    assert not conn.closed, "lời gọi đọc thứ hai không được làm hỏng lời gọi đang chờ"
    if first_kind == "read_raw":
        reader.feed_data(b"abcd")
        assert await asyncio.wait_for(first, 2) == bytearray(b"abcd")
    else:
        reader.feed_data(header(0, 4) + b"abcd")
        hdr, payload = await asyncio.wait_for(first, 2)
        assert (hdr, payload) == (BlockHeader(0, 4, SHA), bytearray(b"abcd"))


@pytest.mark.parametrize("how", ["abort", "close"])
async def test_use_after_abort_or_close_is_connection_lost(peer, how):
    conn, _, writer = make_conn(peer)
    if how == "abort":
        conn.abort()
    else:
        await asyncio.wait_for(conn.close(), 2)
    assert conn.closed
    for call in DATA_CALLS.values():
        with pytest.raises(ConnectionLostError):
            await call(conn)
    with pytest.raises(ConnectionLostError):
        await conn.write_raw(b"")  # spec mục 3.3 bước 2: kể cả data rỗng
    assert writer.data == b""


async def test_abort_is_synchronous_idempotent_and_aborts_transport(peer):
    conn, _, writer = make_conn(peer)
    assert conn.abort() is None
    assert conn.closed, "abort() chạy đồng bộ: closed là True ngay khi trả về"
    assert writer.transport.aborted >= 1, "abort() phải đóng ngay transport (transport.abort)"
    conn.abort()
    conn.abort()
    await asyncio.wait_for(conn.close(), 1.0)  # close sau abort trả về ngay, không lỗi
    assert conn.closed


async def test_write_raw_empty_on_open_connection_does_nothing(peer):
    conn, _, writer = make_conn(peer)
    hook = Hook(writer)
    await conn.write_raw(b"", before_chunk=hook)
    await conn.write_raw(bytearray(), before_chunk=hook)
    await conn.write_raw(memoryview(b""), before_chunk=hook)
    assert hook.calls == [], "write_raw(b'') không gọi móc"
    assert writer.data == b""
    assert not conn.closed


# =================================================================================================
# AC-9 (seam): chia đoạn, móc before_chunk, kiểm đóng
# =================================================================================================


async def test_write_raw_chunks_and_calls_hook_before_each_chunk(peer, monkeypatch):
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    conn, _, writer = make_conn(peer)
    data = bytes(range(20))
    hook = Hook(writer)
    await conn.write_raw(data, before_chunk=hook)
    assert writer.data == data
    check_hook_trace(hook.calls, prefix=0, total=20, chunk=8)
    assert not conn.closed


async def test_write_block_writes_header_in_one_write_and_hooks_payload_only(peer, monkeypatch):
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    conn, _, writer = make_conn(peer)
    payload = bytes(range(50))
    hook = Hook(writer)
    await conn.write_block(7, SHA, payload, before_chunk=hook)
    assert writer.data == header(7, 50) + payload
    assert bytes(writer.writes[0]) == header(7, 50), "header 41 byte phải đi trong một lần write (spec 3.3)"
    check_hook_trace(hook.calls, prefix=HDR_SIZE, total=50, chunk=8)


async def test_write_block_does_not_hash_or_check_sha(peer):
    """`write_block` không băm lại payload: hash bên gọi đưa vào đi nguyên trong header."""
    conn, _, writer = make_conn(peer)
    wrong = b"\xee" * 32
    await conn.write_block(0, wrong, b"payload")
    assert writer.data == header(0, 7, wrong) + b"payload"


@pytest.mark.parametrize("method", ["write_raw", "write_block"])
async def test_length_is_nbytes_and_caller_buffer_is_copied(peer, monkeypatch, method):
    """AC-9 (c): memoryview(array('I')) → length = nbytes; sửa buffer sau khi trả về không đổi byte đã ghi."""
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    conn, _, writer = make_conn(peer)
    arr = array.array("I", range(1, 11))
    expected = arr.tobytes()
    assert len(memoryview(arr)) == 10 and memoryview(arr).nbytes == len(expected) == 40
    if method == "write_raw":
        await conn.write_raw(memoryview(arr))
        prefix = b""
    else:
        await conn.write_block(3, SHA, memoryview(arr))
        prefix = header(3, 40)
    for i in range(len(arr)):
        arr[i] = 0xDEADBEEF
    assert writer.data == prefix + expected, "Connection còn giữ tham chiếu tới buffer của bên gọi"


async def test_bytearray_caller_buffer_can_be_reused_after_return(peer, monkeypatch):
    monkeypatch.setattr(connection, "_SEND_CHUNK", 4)
    conn, _, writer = make_conn(peer)
    buf = bytearray(b"0123456789")
    await conn.write_block(0, SHA, buf)
    buf[:] = b"x" * 10
    await conn.write_block(1, SHA, buf)
    assert writer.data == header(0, 10) + b"0123456789" + header(1, 10) + b"x" * 10


async def test_hook_error_on_first_chunk_of_write_raw_keeps_connection(peer, monkeypatch):
    """AC-9 (e): chưa ghi byte nào của khung → kết nối vẫn dùng được; ngoại lệ bay ra nguyên trạng."""
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    conn, _, writer = make_conn(peer)
    hook = Hook(writer, raise_on=1)
    with pytest.raises(HookError) as exc:
        await conn.write_raw(bytes(32), before_chunk=hook)
    assert exc.value is hook.error
    assert not conn.closed
    assert writer.data == b""
    await conn.write_raw(b"sau")
    assert writer.data == b"sau"


async def test_hook_error_after_first_chunk_aborts(peer, monkeypatch):
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    conn, _, writer = make_conn(peer)
    data = bytes(range(32))
    hook = Hook(writer, raise_on=2)
    with pytest.raises(HookError) as exc:
        await conn.write_raw(data, before_chunk=hook)
    assert exc.value is hook.error
    assert conn.closed, "đã ghi một phần khung: phải abort"
    assert writer.transport.aborted >= 1
    assert writer.data == data[:8]


async def test_hook_error_on_first_payload_chunk_of_write_block_aborts(peer, monkeypatch):
    """Header đã ghi trước móc của đoạn payload đầu → móc ném thì phải abort."""
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    conn, _, writer = make_conn(peer)
    hook = Hook(writer, raise_on=1)
    with pytest.raises(HookError) as exc:
        await conn.write_block(0, SHA, bytes(32), before_chunk=hook)
    assert exc.value is hook.error
    assert conn.closed
    assert writer.data == header(0, 32)


# Móc có thể chờ hay kiểm một kết nối khác (ví dụ kết nối điều khiển) và ném lỗi mất kết nối của kết nối
# đó. Với kết nối dữ liệu đang ghi, đó vẫn chỉ là “móc ném ngoại lệ” (spec mục 3.3 bước 6).
LOST_FROM_HOOK = {
    "ConnectionLostError": lambda: ConnectionLostError("kết nối điều khiển đã mất"),
    "IdleTimeout": lambda: connection.IdleTimeout("hết hạn ở một kết nối khác"),
}


@pytest.mark.parametrize("error", list(LOST_FROM_HOOK))
@pytest.mark.parametrize(
    ("method", "raise_on"),
    [("write_block", 1), ("write_block", 2), ("write_raw", 2)],
    ids=["write_block-payload-1", "write_block-payload-2", "write_raw-chunk-2"],
)
async def test_hook_raising_connection_lost_mid_frame_aborts(peer, monkeypatch, method, raise_on, error):
    """Hồi quy (soat-code P2-3, mức trung bình): móc ném ConnectionLostError hay IdleTimeout khi đã ghi ít
    nhất 1 byte của khung → kết nối này vẫn phải bị cắt (spec mục 3.3 bước 6; mục 3.4 bước 5: mọi lỗi ghi
    trên DATA để lại `closed` là True). Không cắt thì lần ghi sau nối byte ngay sau nửa khung trên dây, và
    bên nhận đọc lệch khung (BlockError bị tính vào luật quá 3 lần, hoặc một khối sai hash)."""
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    conn, _, writer = make_conn(peer)
    data = bytes(range(32))
    hook = Hook(writer, raise_on=raise_on)
    hook.error = LOST_FROM_HOOK[error]()
    with pytest.raises(ConnectionLostError) as exc:
        if method == "write_raw":
            await conn.write_raw(data, before_chunk=hook)
        else:
            await conn.write_block(0, SHA, data, before_chunk=hook)
    assert exc.value is hook.error, "ngoại lệ của móc phải bay ra nguyên trạng"
    sent = (b"" if method == "write_raw" else header(0, 32)) + data[: 8 * (raise_on - 1)]
    assert writer.data == sent
    assert conn.closed, "đã ghi một phần khung mà móc ném ConnectionLostError: phải abort"
    assert writer.transport.aborted >= 1, "phải cắt (transport.abort), không để socket sống"
    with pytest.raises(ConnectionLostError):
        await conn.write_block(1, SHA, b"tiep")
    with pytest.raises(ConnectionLostError):
        await conn.write_raw(b"tiep")
    assert writer.data == sent, "không byte nào được nối sau nửa khung"


@pytest.mark.parametrize("error", list(LOST_FROM_HOOK))
async def test_hook_raising_connection_lost_before_any_byte_keeps_connection(peer, monkeypatch, error):
    """Spec mục 3.3 bước 6, nửa còn lại: chưa ghi byte nào của khung (móc của đoạn đầu `write_raw`) thì kết
    nối vẫn dùng được, kể cả khi móc ném ConnectionLostError của kết nối khác; ngoại lệ bay ra nguyên trạng.
    Chặn cách sửa lỗi ở test trên bằng cách cắt mọi kết nối hễ móc ném ConnectionLostError."""
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    conn, _, writer = make_conn(peer)
    hook = Hook(writer, raise_on=1)
    hook.error = LOST_FROM_HOOK[error]()
    with pytest.raises(ConnectionLostError) as exc:
        await conn.write_raw(bytes(32), before_chunk=hook)
    assert exc.value is hook.error
    assert not conn.closed
    assert writer.transport.aborted == 0
    assert writer.data == b""
    await conn.write_block(0, SHA, b"sau")
    assert writer.data == header(0, 3) + b"sau"


async def _start_write_held_at_first_drain(conn, writer, method: str, data: bytes) -> asyncio.Task:
    writer.hold_from = 1
    if method == "write_raw":
        task = asyncio.create_task(conn.write_raw(data))
    else:
        task = asyncio.create_task(conn.write_block(0, SHA, data))
    await asyncio.wait_for(writer.held.wait(), 2)
    return task


@pytest.mark.parametrize("method", ["write_raw", "write_block"])
@pytest.mark.parametrize("how", ["close", "abort", "transport-closing"])
async def test_close_between_chunks_stops_writing(peer, monkeypatch, method, how):
    """AC-9 (g): giữa hai đoạn, kết nối bị đóng (close/abort của task khác, hay transport đang đóng) →
    lần ghi đó ném ConnectionLostError, không đoạn nào được ghi thêm, closed là True."""
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 5.0)
    conn, _, writer = make_conn(peer)
    data = bytes(range(40))
    task = await _start_write_held_at_first_drain(conn, writer, method, data)
    before = writer.data
    prefix = b"" if method == "write_raw" else header(0, 40)
    assert before == prefix + data[:8]
    closer = None
    if how == "close":
        closer = asyncio.create_task(conn.close())
        await asyncio.sleep(0.01)
    elif how == "abort":
        conn.abort()
    else:
        writer.transport.closing = True
    writer.release()
    with pytest.raises(ConnectionLostError):
        await asyncio.wait_for(task, 2)
    if closer is not None:
        await asyncio.wait_for(closer, 2)
    assert conn.closed
    assert writer.data == before, "không đoạn nào được ghi sau khi kết nối đã đóng"


@pytest.mark.parametrize("method", ["write_raw", "write_block"])
async def test_write_waiting_for_lock_does_not_call_hook_after_abort(peer, monkeypatch, method):
    """Spec mục 3.3 bước 3 (hồi quy soat-code P2-3, mức thấp): trong khóa gửi, `_closed` được kiểm lại
    trước móc. Lần ghi đang chờ khóa mà kết nối bị cắt → ConnectionLostError, không gọi móc (móc giới hạn
    tốc độ không trừ token hay ngủ cho một kết nối đã chết) và không ghi byte nào."""
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 5.0)
    conn, _, writer = make_conn(peer)
    first_hook = Hook(writer, block_on=1)
    first = asyncio.create_task(conn.write_raw(bytes(16), before_chunk=first_hook))
    await asyncio.wait_for(first_hook.entered.wait(), 2)  # lần ghi đầu giữ khóa, móc của nó đang ngủ
    second_hook = Hook(writer)
    if method == "write_raw":
        second = asyncio.create_task(conn.write_raw(b"second", before_chunk=second_hook))
    else:
        second = asyncio.create_task(conn.write_block(1, SHA, b"second", before_chunk=second_hook))
    try:
        await asyncio.sleep(0.02)
        assert not second.done(), "lần ghi thứ hai phải đang chờ khóa gửi"
        conn.abort()
        first_hook.gate.set()
        with pytest.raises(ConnectionLostError):
            await asyncio.wait_for(first, 2)
        with pytest.raises(ConnectionLostError):
            await asyncio.wait_for(second, 2)
    finally:
        first_hook.gate.set()
        for task in (first, second):
            task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
    assert second_hook.calls == [], "móc được gọi cho một kết nối đã đóng"
    assert writer.data == b""
    assert conn.closed


@pytest.mark.parametrize("method", ["write_raw", "write_block"])
async def test_peer_eof_before_write_is_connection_lost(peer, method):
    """AC-9 (g): máy kia đã đóng chiều gửi của nó (`reader.at_eof()`) → không ghi gì, abort."""
    conn, reader, writer = make_conn(peer)
    reader.feed_eof()
    with pytest.raises(ConnectionLostError):
        if method == "write_raw":
            await conn.write_raw(b"abc")
        else:
            await conn.write_block(0, SHA, b"abc")
    assert conn.closed
    assert writer.data == b""


async def test_abort_while_write_waits_for_drain_is_connection_lost(peer, monkeypatch):
    """Spec mục 5.3: abort() trong lúc write_block đang chờ drain → lần ghi đó ném ConnectionLostError."""
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 5.0)
    conn, _, writer = make_conn(peer)
    task = await _start_write_held_at_first_drain(conn, writer, "write_block", b"abc")
    conn.abort()
    with pytest.raises(ConnectionLostError):
        await asyncio.wait_for(task, 2)
    assert conn.closed


@pytest.mark.parametrize(
    "error", [ConnectionResetError("giả lập"), ssl.SSLError("giả lập"), BrokenPipeError()], ids=repr
)
async def test_socket_error_while_writing_is_connection_lost(peer, error):
    conn, _, writer = make_conn(peer)
    writer.drain_error = error
    with pytest.raises(ConnectionLostError):
        await conn.write_block(0, SHA, b"abc")
    assert conn.closed
    assert writer.transport.aborted >= 1


async def test_drain_timeout_is_connection_lost(peer, monkeypatch):
    """protocol §2.3: một đoạn drain() quá DRAIN_TIMEOUT → abort rồi ConnectionLostError."""
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 0.3)
    conn, _, writer = make_conn(peer)
    writer.hold_from = 1  # không bao giờ thả
    start = time.monotonic()
    with pytest.raises(ConnectionLostError):
        await asyncio.wait_for(conn.write_block(0, SHA, b"abc"), 5)
    elapsed = time.monotonic() - start
    assert 0.25 <= elapsed <= 1.3, elapsed
    assert conn.closed
    assert writer.transport.aborted >= 1


async def test_concurrent_write_blocks_do_not_interleave(peer, monkeypatch):
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    conn, _, writer = make_conn(peer)
    a, b = bytes(range(40)), bytes(range(100, 160))
    await asyncio.gather(conn.write_block(0, SHA, a), conn.write_block(1, SHA, b))
    fa, fb = header(0, 40) + a, header(1, 60) + b
    assert writer.data in (fa + fb, fb + fa)


# =================================================================================================
# AC-10: read_block kiểm header trước payload; EOF
# =================================================================================================

SIZE4 = 3 * BLOCK_SIZE + 123  # 4 khối, khối cuối 123 byte
BAD_HEADERS = {
    "type-0x00": (SIZE4, header(0, BLOCK_SIZE, type_=0x00)),
    "type-0x03": (SIZE4, header(0, BLOCK_SIZE, type_=0x03)),
    "type-0xFF": (SIZE4, header(0, BLOCK_SIZE, type_=0xFF)),
    "length-0": (SIZE4, header(0, 0)),
    "length-1MiB+1": (SIZE4, header(0, BLOCK_SIZE + 1)),
    "length-0xFFFFFFFF": (SIZE4, header(0, 0xFFFFFFFF)),
    "index-eq-count": (SIZE4, header(4, 123)),
    "index-2^32-1": (SIZE4, header(0xFFFFFFFF, 123)),
    "length-short-not-last": (SIZE4, header(0, BLOCK_SIZE - 1)),
    "length-last-too-long": (SIZE4, header(3, 124)),
    "length-last-too-short": (SIZE4, header(3, 122)),
    "length-last-full": (SIZE4, header(3, BLOCK_SIZE)),
    "size-0-len-1": (0, header(0, 1)),
    "size-0-len-1MiB": (0, header(0, BLOCK_SIZE)),
    "size-1-len-2": (1, header(0, 2)),
}


@pytest.mark.parametrize("case", list(BAD_HEADERS))
@pytest.mark.parametrize("eof", [True, False], ids=["eof", "no-eof"])
async def test_read_block_rejects_bad_header_before_reading_payload(peer, case, eof):
    size, hdr = BAD_HEADERS[case]
    conn, reader, writer = make_conn(peer, is_server=True)
    tail = b"PAYLOAD-" * 16
    reader.feed_data(hdr + tail)
    if eof:
        reader.feed_eof()
    with pytest.raises(BlockError):
        await asyncio.wait_for(conn.read_block(size=size, idle_timeout=5), 2)
    assert conn.closed, "header sai → abort"
    assert writer.transport.aborted >= 1
    assert await reader.rest() == tail, "payload không được đọc khi header sai"
    assert reader.violations == []


async def test_read_block_eof_before_first_byte_is_none(peer):
    conn, reader, _ = make_conn(peer)
    reader.feed_eof()
    assert await conn.read_block(size=100) is None
    assert not conn.closed, "None không tự đóng kết nối"
    assert await conn.read_block(size=100) is None


GOOD_FRAME = header(0, 100) + bytes(range(100))
CUTS = [1, 2, 20, 40, 41, 42, 41 + 50, 41 + 99]


@pytest.mark.parametrize("cut", CUTS)
async def test_read_block_eof_mid_frame_is_connection_lost(peer, cut):
    """EOF sau 1…40 byte header, ngay sau header, hay giữa payload → ConnectionLostError (D6)."""
    conn, reader, writer = make_conn(peer)
    reader.feed_data(GOOD_FRAME[:cut])
    reader.feed_eof()
    with pytest.raises(ConnectionLostError) as exc:
        await asyncio.wait_for(conn.read_block(size=100), 2)
    assert not isinstance(exc.value, (BlockError, FramingError))
    assert conn.closed
    assert writer.transport.aborted >= 1


async def test_read_raw_eof_not_at_boundary_is_connection_lost(peer):
    conn, reader, _ = make_conn(peer)
    reader.feed_eof()
    with pytest.raises(ConnectionLostError):
        await conn.read_raw(4, at_boundary=False)
    assert conn.closed


async def test_read_raw_eof_after_some_bytes_is_connection_lost(peer):
    conn, reader, _ = make_conn(peer)
    reader.feed_data(b"ab")
    reader.feed_eof()
    with pytest.raises(ConnectionLostError):
        await conn.read_raw(4, at_boundary=True)
    assert conn.closed


async def test_read_raw_none_is_sticky(peer):
    conn, reader, _ = make_conn(peer)
    reader.feed_eof()
    assert await conn.read_raw(1, at_boundary=True) is None
    assert await conn.read_raw(5, at_boundary=True) is None
    assert not conn.closed


@pytest.mark.parametrize("n", [1, 7, 64 * 1024 + 1, BLOCK_SIZE])
async def test_read_raw_returns_new_bytearray_of_exact_length(peer, n):
    conn, reader, _ = make_conn(peer)
    data = random.Random(n).randbytes(n)
    reader.feed_data(data + data)
    first = await conn.read_raw(n, at_boundary=True)
    second = await conn.read_raw(n, at_boundary=False)
    assert type(first) is bytearray and type(second) is bytearray
    assert first == data and second == data
    assert first is not second
    assert reader.violations == []
    assert all(1 <= k <= connection._READ_CHUNK for k in reader.sizes)


async def test_read_block_last_block_of_exact_block_size(peer):
    """Ca biên 5.3: `size` chia hết cho BLOCK_SIZE → khối cuối dài đúng BLOCK_SIZE."""
    size = 2 * BLOCK_SIZE
    conn, reader, _ = make_conn(peer)
    p0, p1 = random.Random(0).randbytes(BLOCK_SIZE), random.Random(1).randbytes(BLOCK_SIZE)
    reader.feed_data(header(1, BLOCK_SIZE) + p1 + header(0, BLOCK_SIZE) + p0)
    reader.feed_eof()
    h1, got1 = await conn.read_block(size=size)
    h0, got0 = await conn.read_block(size=size)
    assert (h1, got1) == (BlockHeader(1, BLOCK_SIZE, SHA), p1)
    assert (h0, got0) == (BlockHeader(0, BLOCK_SIZE, SHA), p0)
    assert type(got0) is bytearray and got0 is not got1
    assert await conn.read_block(size=size) is None


async def test_read_block_one_byte_file(peer):
    conn, reader, _ = make_conn(peer)
    reader.feed_data(header(0, 1) + b"Z")
    reader.feed_eof()
    assert await conn.read_block(size=1) == (BlockHeader(0, 1, SHA), bytearray(b"Z"))
    assert await conn.read_block(size=1) is None


async def test_read_block_does_not_check_order_or_hash(peer):
    """`net` không kiểm thứ tự `block_index`, không chống trùng và không băm (spec mục 3.4 bước 6)."""
    size = 2 * BLOCK_SIZE + 5  # khối cuối (index 2) dài 5 byte
    conn, reader, _ = make_conn(peer)
    wrong = b"\x01" * 32
    reader.feed_data(header(2, 5, wrong) + b"abcde" + header(2, 5, wrong) + b"abcde")
    reader.feed_eof()
    assert await conn.read_block(size=size) == (BlockHeader(2, 5, wrong), bytearray(b"abcde"))
    assert await conn.read_block(size=size) == (BlockHeader(2, 5, wrong), bytearray(b"abcde"))
    assert await conn.read_block(size=size) is None


async def test_frame_arriving_one_byte_at_a_time(peer):
    """Ca biên 5.3: header cắt thành 41 lần feed_data 1 byte; payload tới từng byte."""
    conn, reader, _ = make_conn(peer)
    payload = bytes(range(60))
    data = header(0, 60) + payload
    feeder = asyncio.create_task(feed_pieces(reader, data, range(1, len(data))))
    try:
        got = await asyncio.wait_for(conn.read_block(size=60, idle_timeout=5), 5)
        assert got == (BlockHeader(0, 60, SHA), bytearray(payload))
        assert await asyncio.wait_for(conn.read_block(size=60, idle_timeout=5), 5) is None
    finally:
        await cancel_and_wait(feeder)
    assert reader.violations == []


@pytest.mark.parametrize("error", [ConnectionResetError("giả lập"), ssl.SSLError("giả lập")], ids=repr)
@pytest.mark.parametrize("method", ["read_raw", "read_block"])
async def test_socket_error_while_reading_is_connection_lost(peer, method, error):
    conn, reader, writer = make_conn(peer)
    reader.set_exception(error)
    with pytest.raises(ConnectionLostError):
        if method == "read_raw":
            await conn.read_raw(4, at_boundary=True)
        else:
            await conn.read_block(size=10)
    assert conn.closed
    assert writer.transport.aborted >= 1


async def test_read_block_grows_payload_buffer_with_received_bytes(peer):
    """D26: không cấp sẵn `header.length` byte ngay sau header; bộ đệm lớn dần theo byte thật."""
    conn, reader, _ = make_conn(peer)
    reader.feed_data(header(0, BLOCK_SIZE) + b"x" * 10)
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        await asyncio.sleep(0)
        base = tracemalloc.get_traced_memory()[0]
        task = asyncio.create_task(conn.read_block(size=BLOCK_SIZE, idle_timeout=10))
        await asyncio.sleep(0.05)
        assert not task.done()
        grown = tracemalloc.get_traced_memory()[0] - base
    finally:
        if not was_tracing:
            tracemalloc.stop()
    conn.abort()
    with pytest.raises(ConnectionLostError):
        await asyncio.wait_for(task, 2)
    assert grown < 256 * 1024, f"đọc 10 byte payload mà đã cấp {grown} byte (cấp sẵn length?)"


# =================================================================================================
# AC-11 (seam): hạn không nhận byte
# =================================================================================================


async def test_idle_timeout_without_any_byte(peer):
    conn, _, writer = make_conn(peer)
    start = time.monotonic()
    with pytest.raises(connection.IdleTimeout) as exc:
        await asyncio.wait_for(conn.read_raw(1, at_boundary=True, idle_timeout=0.3), 5)
    elapsed = time.monotonic() - start
    assert 0.25 <= elapsed <= 1.3, elapsed
    assert isinstance(exc.value, ConnectionLostError)
    assert conn.closed, "IdleTimeout bay ra thì kết nối đã bị cắt"
    assert writer.transport.aborted >= 1


async def test_idle_timeout_in_the_middle_of_a_payload(peer):
    conn, reader, _ = make_conn(peer)
    reader.feed_data(header(0, 100) + b"x" * 30)
    start = time.monotonic()
    with pytest.raises(connection.IdleTimeout):
        await asyncio.wait_for(conn.read_block(size=100, idle_timeout=0.3), 5)
    assert 0.25 <= time.monotonic() - start <= 1.3
    assert conn.closed


async def test_idle_timeout_in_the_middle_of_a_header(peer):
    conn, reader, _ = make_conn(peer)
    reader.feed_data(header(0, 100)[:10])
    with pytest.raises(connection.IdleTimeout):
        await asyncio.wait_for(conn.read_block(size=100, idle_timeout=0.3), 5)
    assert conn.closed


@pytest.mark.slow
async def test_trickling_bytes_reset_the_idle_timer(peer):
    """AC-11 (c): 1 byte mỗi 0,1 s trong 2 s, idle_timeout = 0,5 → không hết hạn."""
    conn, reader, _ = make_conn(peer)
    data = bytes(range(20))

    async def drip():
        for i in range(len(data)):
            await asyncio.sleep(0.1)
            reader.feed_data(data[i : i + 1])

    dripper = asyncio.create_task(drip())
    try:
        got = await asyncio.wait_for(conn.read_raw(20, at_boundary=True, idle_timeout=0.5), 10)
    finally:
        await cancel_and_wait(dripper)
    assert got == data
    assert not conn.closed


async def test_idle_timeout_none_reads_module_constant_at_call_time(peer, monkeypatch):
    conn, _, _ = make_conn(peer)
    monkeypatch.setattr(connection, "IDLE_TIMEOUT", 0.3)
    start = time.monotonic()
    with pytest.raises(connection.IdleTimeout):
        await asyncio.wait_for(conn.read_block(size=10), 5)
    assert 0.25 <= time.monotonic() - start <= 1.3


@pytest.mark.slow
async def test_time_without_a_read_call_is_not_counted(peer):
    """AC-11 (e): khối đã nằm sẵn; bên đọc ngủ 3 × idle_timeout rồi mới đọc → thành công; khoảng giữa hai
    lần đọc cũng không tính."""
    conn, reader, _ = make_conn(peer)
    reader.feed_data(header(0, 10) + b"0123456789")
    await asyncio.sleep(0.6)
    assert await conn.read_block(size=10, idle_timeout=0.2) == (
        BlockHeader(0, 10, SHA),
        bytearray(b"0123456789"),
    )
    await asyncio.sleep(0.6)
    reader.feed_data(header(0, 10) + b"abcdefghij")
    assert await conn.read_block(size=10, idle_timeout=0.2) == (
        BlockHeader(0, 10, SHA),
        bytearray(b"abcdefghij"),
    )
    assert not conn.closed


async def test_hanging_close_makes_pending_read_connection_lost_not_idle(peer, monkeypatch):
    """AC-11 (g): close() treo (wait_closed không trả về) trong lúc read_raw(idle_timeout=0,2) đang chờ →
    ConnectionLostError, không phải IdleTimeout."""
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 10.0)
    monkeypatch.setattr(connection, "CLOSE_TIMEOUT", 10.0)
    conn, _, writer = make_conn(peer)
    writer.hang_close = True
    reading = asyncio.create_task(conn.read_raw(1, at_boundary=True, idle_timeout=0.2))
    await asyncio.sleep(0.05)
    closing = asyncio.create_task(conn.close())
    try:
        with pytest.raises(ConnectionLostError) as exc:
            await asyncio.wait_for(reading, 3)
        assert not isinstance(exc.value, connection.IdleTimeout), (
            "close() đang chạy: lỗi phải là ConnectionLostError"
        )
        assert not closing.done(), "phép thử vô nghĩa: close() đã xong"
    finally:
        await cancel_and_wait(closing)
    assert conn.closed


@pytest.mark.parametrize("how", ["abort", "close"])
@pytest.mark.parametrize("method", ["read_raw", "read_block", "read_raw-not-boundary"])
async def test_abort_or_close_during_pending_read_is_connection_lost(peer, how, method):
    """Spec mục 3.4 bước 9: không trả None, không ném IdleTimeout."""
    conn, _, _ = make_conn(peer)
    if method == "read_raw":
        task = asyncio.create_task(conn.read_raw(1, at_boundary=True, idle_timeout=5))
    elif method == "read_block":
        task = asyncio.create_task(conn.read_block(size=10, idle_timeout=5))
    else:
        task = asyncio.create_task(conn.read_raw(1, at_boundary=False, idle_timeout=5))
    await asyncio.sleep(0.01)
    if how == "abort":
        conn.abort()
    else:
        await asyncio.wait_for(conn.close(), 2)
    with pytest.raises(ConnectionLostError) as exc:
        await asyncio.wait_for(task, 2)
    assert not isinstance(exc.value, connection.IdleTimeout)


# =================================================================================================
# Mục 3.6: close() của DATA tối đa DRAIN_TIMEOUT (không phải CLOSE_TIMEOUT)
# =================================================================================================


async def test_data_close_is_bounded_by_drain_timeout_not_close_timeout(peer, monkeypatch):
    monkeypatch.setattr(connection, "CLOSE_TIMEOUT", 0.1)
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 0.6)
    conn, _, writer = make_conn(peer)
    writer.hang_close = True
    start = time.monotonic()
    await asyncio.wait_for(conn.close(), 5)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.5, (
        f"close() của DATA chỉ chờ {elapsed:.2f} s: dùng CLOSE_TIMEOUT thay vì DRAIN_TIMEOUT"
    )
    assert elapsed <= 0.6 + 1.0, elapsed
    assert conn.closed
    assert writer.transport.aborted >= 1, "quá hạn đóng gọn thì abort"
    await asyncio.wait_for(conn.close(), 1)  # gọi lại không lỗi


async def test_data_close_is_graceful_when_peer_answers(peer):
    conn, _, writer = make_conn(peer)
    await asyncio.wait_for(conn.close(), 2)
    assert conn.closed
    assert writer.close_calls >= 1, "close() của DATA phải đóng gọn (writer.close)"


# =================================================================================================
# AC-13 (seam): hủy
# =================================================================================================


async def test_cancel_read_block_before_first_byte_keeps_connection(peer):
    conn, reader, _ = make_conn(peer)
    task = asyncio.create_task(conn.read_block(size=10, idle_timeout=5))
    await asyncio.sleep(0.02)
    await cancel_and_wait(task)
    assert not conn.closed
    reader.feed_data(header(0, 10) + b"0123456789")
    assert await conn.read_block(size=10) == (BlockHeader(0, 10, SHA), bytearray(b"0123456789"))


async def test_cancel_read_raw_at_boundary_before_first_byte_keeps_connection(peer):
    conn, reader, _ = make_conn(peer)
    task = asyncio.create_task(conn.read_raw(3, at_boundary=True, idle_timeout=5))
    await asyncio.sleep(0.02)
    await cancel_and_wait(task)
    assert not conn.closed
    reader.feed_data(b"abc")
    assert await conn.read_raw(3, at_boundary=True) == bytearray(b"abc")


@pytest.mark.parametrize(
    "fed", [10, HDR_SIZE, HDR_SIZE + 5], ids=["mid-header", "after-header", "mid-payload"]
)
async def test_cancel_read_block_after_some_bytes_aborts(peer, fed):
    conn, reader, _ = make_conn(peer)
    reader.feed_data(GOOD_FRAME[:fed])
    task = asyncio.create_task(conn.read_block(size=100, idle_timeout=5))
    await asyncio.sleep(0.02)
    assert not task.done()
    await cancel_and_wait(task)
    assert conn.closed, "bị hủy giữa khung mà kết nối vẫn mở (lần đọc sau sẽ lệch khung)"


async def test_cancel_read_raw_with_partial_bytes_aborts(peer):
    conn, reader, _ = make_conn(peer)
    reader.feed_data(b"ab")
    task = asyncio.create_task(conn.read_raw(5, at_boundary=True, idle_timeout=5))
    await asyncio.sleep(0.02)
    await cancel_and_wait(task)
    assert conn.closed


async def test_cancel_read_raw_not_at_boundary_aborts_even_without_bytes(peer):
    conn, _, _ = make_conn(peer)
    task = asyncio.create_task(conn.read_raw(5, at_boundary=False, idle_timeout=5))
    await asyncio.sleep(0.02)
    await cancel_and_wait(task)
    assert conn.closed


async def test_cancel_write_block_waiting_for_lock_keeps_connection(peer, monkeypatch):
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 5.0)
    conn, _, writer = make_conn(peer)
    first = await _start_write_held_at_first_drain(conn, writer, "write_block", b"first")
    waiting = asyncio.create_task(conn.write_block(1, SHA, b"second"))
    await asyncio.sleep(0.02)
    assert not waiting.done()
    await cancel_and_wait(waiting)
    writer.release()
    await asyncio.wait_for(first, 2)
    assert not conn.closed
    await conn.write_block(2, SHA, b"third")
    assert writer.data == header(0, 5) + b"first" + header(2, 5) + b"third"


async def test_cancel_write_block_after_first_chunk_aborts_with_only_a_prefix_sent(peer, monkeypatch):
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", 5.0)
    conn, _, writer = make_conn(peer)
    payload = bytes(range(40))
    task = await _start_write_held_at_first_drain(conn, writer, "write_block", payload)
    await cancel_and_wait(task)
    assert conn.closed
    full = header(0, 40) + payload
    assert len(writer.data) < len(full) and full.startswith(writer.data)
    with pytest.raises(ConnectionLostError):
        await conn.write_block(1, SHA, b"x")


async def test_cancel_during_hook_before_any_byte_keeps_connection(peer, monkeypatch):
    """Spec mục 3.3 bước 7: bị hủy trong lúc móc (của đoạn đầu write_raw) đang ngủ, chưa ghi byte nào."""
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    conn, _, writer = make_conn(peer)
    hook = Hook(writer, block_on=1)
    task = asyncio.create_task(conn.write_raw(bytes(32), before_chunk=hook))
    await asyncio.wait_for(hook.entered.wait(), 2)
    await cancel_and_wait(task)
    assert not conn.closed
    assert writer.data == b""
    await conn.write_raw(b"sau")
    assert writer.data == b"sau"


async def test_cancel_during_hook_after_header_aborts(peer, monkeypatch):
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    conn, _, writer = make_conn(peer)
    hook = Hook(writer, block_on=1)
    task = asyncio.create_task(conn.write_block(0, SHA, bytes(32), before_chunk=hook))
    await asyncio.wait_for(hook.entered.wait(), 2)
    assert writer.data == header(0, 32), "header phải đã ghi trước móc của đoạn payload đầu"
    await cancel_and_wait(task)
    assert conn.closed


async def test_cancel_during_hook_of_second_chunk_aborts(peer, monkeypatch):
    monkeypatch.setattr(connection, "_SEND_CHUNK", 8)
    conn, _, writer = make_conn(peer)
    hook = Hook(writer, block_on=2)
    task = asyncio.create_task(conn.write_raw(bytes(32), before_chunk=hook))
    await asyncio.wait_for(hook.entered.wait(), 2)
    await cancel_and_wait(task)
    assert conn.closed
    assert writer.data == bytes(8)


# =================================================================================================
# Thuộc tính hypothesis H1–H5 (spec mục 5.4)
# =================================================================================================


@st.composite
def frame_sequences(draw, *, big_weight: int = 10):
    """(size, [(index, payload, sha)]): phần lớn file < 1 MiB, thỉnh thoảng nhiều khối."""
    if draw(st.integers(0, big_weight - 1)) == 0:
        size = draw(st.integers(BLOCK_SIZE, 3 * BLOCK_SIZE + 1000))
    else:
        size = draw(st.integers(0, 3000))
    count = block_count(size)
    indices = draw(st.lists(st.integers(0, count - 1), max_size=4)) if count else []
    rng = random.Random(draw(st.integers(0, 2**32 - 1)))
    frames = [(i, rng.randbytes(block_length(size, i)), rng.randbytes(32)) for i in indices]
    return size, frames


def encode_frames(frames) -> tuple[bytes, list[int]]:
    """Luồng byte và vị trí các ranh giới khung (gồm 0 và cuối)."""
    out = bytearray()
    bounds = [0]
    for i, payload, sha in frames:
        out += header(i, len(payload), sha) + payload
        bounds.append(len(out))
    return bytes(out), bounds


@pytest.mark.slow
@PROPERTY
@given(seq=frame_sequences(), data=st.data())
def test_h1_round_trip_of_any_valid_frame_sequence_cut_anywhere(peer, seq, data):
    size, frames = seq
    stream, _ = encode_frames(frames)
    cuts = data.draw(st.lists(st.integers(0, len(stream)), max_size=12), label="cuts")

    async def run():
        conn, reader, _ = make_conn(peer, is_server=True)
        feeder = asyncio.create_task(feed_pieces(reader, stream, cuts))
        try:
            got = []
            while (item := await conn.read_block(size=size, idle_timeout=10)) is not None:
                hdr, payload = item
                assert type(payload) is bytearray
                got.append((hdr.index, bytes(payload), hdr.sha256, hdr.length))
            assert got == [(i, p, s, len(p)) for i, p, s in frames]
            assert await conn.read_block(size=size) is None
            assert not conn.closed
            assert reader.violations == []
        finally:
            await cancel_and_wait(feeder)

    run_bounded(run())


@PROPERTY
@given(
    size=st.one_of(st.just(0), st.integers(1, 5 * BLOCK_SIZE), st.integers(0, MAX_FILE_SIZE)),
    type_=st.one_of(st.just(0x02), st.integers(0, 255)),
    index=st.one_of(st.integers(0, 8), st.integers(0, 2**32 - 1)),
    length=st.one_of(
        st.sampled_from([0, 1, 123, BLOCK_SIZE - 1, BLOCK_SIZE, BLOCK_SIZE + 1, 0xFFFFFFFF]),
        st.integers(0, 2**32 - 1),
    ),
    tail=st.binary(max_size=64),
    eof=st.booleans(),
)
def test_h2_any_rejected_header_is_block_error_and_payload_is_not_read(
    peer, size, type_, index, length, tail, eof
):
    assume(not header_is_valid(type_, index, length, size))

    async def run():
        conn, reader, _ = make_conn(peer)
        reader.feed_data(HDR.pack(type_, index, length, SHA) + tail)
        if eof:
            reader.feed_eof()
        with pytest.raises(BlockError):
            await asyncio.wait_for(conn.read_block(size=size, idle_timeout=5), 2)
        assert conn.closed
        assert await reader.rest() == tail
        assert reader.violations == []

    run_bounded(run())


@pytest.mark.slow
@PROPERTY
@given(seq=frame_sequences(big_weight=20), data=st.data())
def test_h3_truncated_stream_is_connection_lost_mid_frame_and_none_at_boundary(peer, seq, data):
    size, frames = seq
    stream, bounds = encode_frames(frames)
    assume(len(stream) > 0)
    cut = data.draw(st.integers(0, len(stream) - 1), label="cut")
    complete = sum(1 for b in bounds[1:] if b <= cut)

    async def run():
        conn, reader, _ = make_conn(peer)
        reader.feed_data(stream[:cut])
        reader.feed_eof()
        for i, payload, sha in frames[:complete]:
            assert await conn.read_block(size=size) == (BlockHeader(i, len(payload), sha), payload)
        if cut in bounds:
            assert await conn.read_block(size=size) is None
            assert not conn.closed
        else:
            with pytest.raises(ConnectionLostError) as exc:
                await conn.read_block(size=size)
            assert not isinstance(exc.value, (BlockError, FramingError))
            assert conn.closed

    run_bounded(run())


@st.composite
def fuzz_streams(draw):
    if draw(st.booleans()):
        return draw(st.binary(max_size=300))
    # Header gần đúng dạng (type 0x02, length nhỏ) để đi qua được nhiều nhánh hơn.
    index = draw(st.integers(0, 6))
    length = draw(st.integers(0, 200))
    return HDR.pack(0x02, index, length, SHA) + draw(st.binary(max_size=300))


@PROPERTY
@given(
    stream=fuzz_streams(),
    size=st.one_of(st.integers(0, 600), st.integers(0, 6 * BLOCK_SIZE)),
    read_chunk=st.integers(1, 64 * 1024),
)
def test_h4_fuzzed_stream_only_yields_frames_none_block_error_or_connection_lost(
    peer, stream, size, read_chunk
):
    async def run():
        conn, reader, _ = make_conn(peer)
        reader.feed_data(stream)
        reader.feed_eof()
        for _ in range(len(stream) + 2):
            try:
                item = await asyncio.wait_for(conn.read_block(size=size, idle_timeout=5), 5)
            except BlockError:
                assert conn.closed
                break
            except ConnectionLostError:
                assert conn.closed
                break
            except BaseException as e:  # mọi ngoại lệ khác là lỗi
                raise AssertionError(f"read_block ném ngoại lệ lạ: {type(e).__name__}: {e}") from e
            if item is None:
                break
            hdr, payload = item
            assert header_is_valid(0x02, hdr.index, hdr.length, size)
            assert len(payload) == hdr.length
        assert reader.violations == []
        assert reader.sizes or not stream, "phép thử vô nghĩa: không đọc byte nào"
        assert all(1 <= k <= read_chunk for k in reader.sizes), reader.sizes

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(connection, "_READ_CHUNK", read_chunk)
        run_bounded(run())


def _as_buffer(kind: str, data: bytes):
    if kind == "bytes":
        return data, None
    buf = bytearray(data)
    return (buf if kind == "bytearray" else memoryview(buf)), buf


@PROPERTY
@given(
    chunk=st.integers(1, 64),
    data=st.data(),
    kind=st.sampled_from(["bytes", "bytearray", "memoryview"]),
    method=st.sampled_from(["write_raw", "write_block"]),
)
def test_h5_chunking_hook_and_copy(peer, chunk, data, kind, method):
    lo = 1 if method == "write_block" else 0
    length = data.draw(st.integers(lo, 5 * chunk), label="L")
    payload = data.draw(st.binary(min_size=length, max_size=length), label="payload")

    async def run():
        conn, _, writer = make_conn(peer)
        hook = Hook(writer)
        buf, mutable = _as_buffer(kind, payload)
        if method == "write_raw":
            await conn.write_raw(buf, before_chunk=hook)
            prefix = b""
        else:
            await conn.write_block(9, SHA, buf, before_chunk=hook)
            prefix = header(9, length)
            assert bytes(writer.writes[0]) == prefix, "header không được cắt theo _SEND_CHUNK"
        if mutable is not None:
            mutable[:] = b"\xaa" * len(mutable)
        assert writer.data == prefix + payload
        check_hook_trace(hook.calls, prefix=len(prefix), total=length, chunk=chunk)
        assert not conn.closed

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(connection, "_SEND_CHUNK", chunk)
        run_bounded(run())
