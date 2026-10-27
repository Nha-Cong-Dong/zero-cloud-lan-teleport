"""Test của chính proxy TCP dùng chung (`tcp_proxy` / `TcpProxy` ở `tests/conftest.py`; spec-p3 P3-0).

Proxy là công cụ đo của các test nối lại (P3-0, P3-5), nên hành vi của nó phải đúng như mô tả: RST
thật (máy kia thấy reset, không phải EOF), chặn không đóng và không chuyển byte nào, chặn rồi đi tiếp
không mất/lặp byte, kết nối mới lúc chặn không tới server, và có backpressure (không nuốt hết dữ liệu
của bên gửi khi bên kia không đọc). Mọi test mở socket thật trên 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import socket
import threading
import time

import pytest

pytestmark = pytest.mark.integration

LOCAL = "127.0.0.1"
MiB = 1024 * 1024
# Trần lỏng cho lượng byte “biến mất” vào đường ống khi bên kia không đọc (bộ đệm socket của hệ điều
# hành + bộ đệm nhỏ của proxy). Proxy không có backpressure sẽ nuốt hết `FLOOD` byte.
FLOOD = 64 * MiB
PIPE_BOUND = 24 * MiB


class Sink:
    """Server TCP trong loop của test: ghi lại mọi kết nối, mỗi kết nối đọc theo lệnh."""

    def __init__(self) -> None:
        self.conns: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> Sink:
        self.server = await asyncio.start_server(self._on, LOCAL, 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def _on(self, reader, writer) -> None:
        self.conns.append((reader, writer))

    async def nth(self, n: int = 0, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while len(self.conns) <= n:
            assert time.monotonic() < deadline, f"server chưa nhận kết nối thứ {n + 1}"
            await asyncio.sleep(0.01)
        return self.conns[n]

    async def close(self) -> None:
        for _, w in self.conns:
            w.transport.abort()
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.server.wait_closed(), 3)


@pytest.fixture
async def sink():
    s = await Sink().start()
    yield s
    await s.close()


async def read_exactly_within(reader: asyncio.StreamReader, n: int, timeout: float) -> bytes:
    async with asyncio.timeout(timeout):
        return await reader.readexactly(n)


async def read_outcome(reader: asyncio.StreamReader, timeout: float) -> str:
    """Đọc tới khi kết nối kết thúc: `"reset"` (lỗi kết nối), `"eof"` (đóng thường) hay `"open"`."""
    try:
        async with asyncio.timeout(timeout):
            while True:
                if not await reader.read(65536):
                    return "eof"
    except ConnectionError:
        return "reset"
    except TimeoutError:
        return "open"


async def abort_client(writer: asyncio.StreamWriter) -> None:
    writer.transport.abort()
    await asyncio.sleep(0)


# ---- đi bình thường ---------------------------------------------------------------------------------


async def test_forwards_both_directions_and_passes_half_close(tcp_proxy, sink):
    proxy = tcp_proxy(sink.port)
    data = os.urandom(3 * MiB + 17)
    reader, writer = await asyncio.open_connection(LOCAL, proxy.port)
    try:
        writer.write(data)
        writer.write_eof()  # đóng nửa: server phải thấy EOF, chiều ngược vẫn mở
        s_reader, s_writer = await sink.nth()
        got = await asyncio.wait_for(s_reader.read(-1), 10)
        assert got == data
        reply = hashlib.sha256(got).digest()
        s_writer.write(reply)
        s_writer.close()
        assert await asyncio.wait_for(reader.read(-1), 5) == reply
        assert proxy.forwarded == {"c2s": len(data), "s2c": len(reply)}
        assert proxy.accepted == 1 and proxy.resets == 0
    finally:
        writer.close()


# ---- RST --------------------------------------------------------------------------------------------


async def test_rst_resets_both_sides_not_eof(tcp_proxy, sink):
    proxy = tcp_proxy(sink.port)
    reader, writer = await asyncio.open_connection(LOCAL, proxy.port)
    try:
        writer.write(b"ping")
        s_reader, _ = await sink.nth()
        assert await read_exactly_within(s_reader, 4, 3) == b"ping"
        assert proxy.rst() == 1
        assert await read_outcome(reader, 3) == "reset"
        assert await read_outcome(s_reader, 3) == "reset"
        assert proxy.resets == 1 and proxy.live() == 0
    finally:
        writer.transport.abort()


async def test_new_connection_after_rst_works_normally(tcp_proxy, sink):
    proxy = tcp_proxy(sink.port)
    _r1, w1 = await asyncio.open_connection(LOCAL, proxy.port)
    await sink.nth(0)
    proxy.rst()
    w1.transport.abort()
    r2, w2 = await asyncio.open_connection(LOCAL, proxy.port)
    try:
        w2.write(b"lan-hai")
        s_reader, s_writer = await sink.nth(1)
        assert await read_exactly_within(s_reader, 7, 3) == b"lan-hai"
        s_writer.write(b"ok")
        assert await read_exactly_within(r2, 2, 3) == b"ok"
    finally:
        w2.transport.abort()


# ---- chặn -------------------------------------------------------------------------------------------


async def test_block_moves_no_bytes_and_closes_nothing_then_resume_delivers_in_order(tcp_proxy, sink):
    proxy = tcp_proxy(sink.port)
    reader, writer = await asyncio.open_connection(LOCAL, proxy.port)
    try:
        first = os.urandom(MiB)
        writer.write(first)
        s_reader, s_writer = await sink.nth()
        assert await read_exactly_within(s_reader, len(first), 5) == first
        proxy.block()
        before = dict(proxy.forwarded)
        rest = os.urandom(2 * MiB)
        writer.write(rest)
        s_writer.write(b"nguoc-chieu")
        # Trong lúc chặn: không byte nào qua, không bên nào thấy EOF hay reset.
        assert await read_outcome(s_reader, 1.0) == "open"
        assert await read_outcome(reader, 0.2) == "open"
        assert proxy.forwarded == before and proxy.live() == 1
        proxy.resume()
        assert await read_exactly_within(s_reader, len(rest), 10) == rest
        assert await read_exactly_within(reader, 11, 5) == b"nguoc-chieu"
        assert proxy.forwarded == {"c2s": 3 * MiB, "s2c": 11}
    finally:
        writer.transport.abort()


async def test_block_then_rst_resets_held_connection(tcp_proxy, sink):
    proxy = tcp_proxy(sink.port)
    reader, writer = await asyncio.open_connection(LOCAL, proxy.port)
    try:
        writer.write(b"x" * 1000)
        s_reader, _ = await sink.nth()
        await read_exactly_within(s_reader, 1000, 3)
        await proxy.outage(0.5, end="rst")
        assert not proxy.blocked
        assert await read_outcome(reader, 3) == "reset"
        assert await read_outcome(s_reader, 3) == "reset"
    finally:
        writer.transport.abort()


async def test_eof_does_not_pass_while_blocked(tcp_proxy, sink):
    proxy = tcp_proxy(sink.port)
    _reader, writer = await asyncio.open_connection(LOCAL, proxy.port)
    try:
        s_reader, _ = await sink.nth()
        proxy.block()
        writer.write_eof()
        assert await read_outcome(s_reader, 0.8) == "open"
        proxy.resume()
        assert await read_outcome(s_reader, 3) == "eof"
    finally:
        writer.transport.abort()


async def test_connection_opened_while_blocked_reaches_server_only_after_resume(tcp_proxy, sink):
    proxy = tcp_proxy(sink.port)
    proxy.block()
    _reader, writer = await asyncio.open_connection(LOCAL, proxy.port)  # TCP vẫn nối được tới proxy
    try:
        writer.write(b"hello")
        await asyncio.sleep(0.5)
        assert sink.conns == [], "lúc chặn mà proxy đã nối tới server"
        assert proxy.accepted == 1
        proxy.resume()
        s_reader, _ = await sink.nth(0, timeout=3)
        assert await read_exactly_within(s_reader, 5, 3) == b"hello"
    finally:
        writer.transport.abort()


async def test_rst_also_cuts_connections_held_before_reaching_server(tcp_proxy, sink):
    proxy = tcp_proxy(sink.port)
    proxy.block()
    reader, writer = await asyncio.open_connection(LOCAL, proxy.port)
    try:
        await asyncio.sleep(0.2)
        assert proxy.rst() == 1
        proxy.resume()
        assert await read_outcome(reader, 3) == "reset"
        await asyncio.sleep(0.3)
        assert sink.conns == []
    finally:
        writer.transport.abort()


# ---- backpressure -----------------------------------------------------------------------------------


class Flood:
    """Bên gửi ghi `FLOOD` byte theo khối 1 MiB, mỗi khối `drain()`; `absorbed()` = byte đã rời bộ
    đệm asyncio phía client (đã vào hệ điều hành, proxy hay server)."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.sent = 0
        writer.transport.set_write_buffer_limits(high=MiB)
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        chunk = os.urandom(MiB)
        for _ in range(FLOOD // MiB):
            self.writer.write(chunk)
            self.sent += len(chunk)
            await self.writer.drain()

    def absorbed(self) -> int:
        return self.sent - self.writer.transport.get_write_buffer_size()


@pytest.mark.parametrize("how", ["blocked", "server-not-reading"])
async def test_pipe_through_proxy_has_backpressure(tcp_proxy, sink, how):
    """Proxy đang chặn, hoặc server không đọc: bên gửi bị chặn lại (backpressure) và lượng byte nó đẩy
    được vào đường ống (bộ đệm hệ điều hành + bộ đệm nhỏ của proxy) có trần, xa dưới `FLOOD`."""
    proxy = tcp_proxy(sink.port)
    _reader, writer = await asyncio.open_connection(LOCAL, proxy.port)
    try:
        await sink.nth()
        if how == "blocked":
            proxy.block()
        flood = Flood(writer)
        await asyncio.sleep(1.5)
        assert not flood.task.done(), "bên gửi ghi hết dữ liệu: proxy không có backpressure"
        absorbed = flood.absorbed()
        assert absorbed <= PIPE_BOUND, f"đường ống nuốt {absorbed / MiB:.1f} MiB (trần {PIPE_BOUND // MiB})"
        if how == "blocked":
            assert proxy.forwarded["c2s"] == 0
        else:
            assert proxy.forwarded["c2s"] <= absorbed
        proxy.rst()
        with pytest.raises((ConnectionError, OSError)):
            await asyncio.wait_for(flood.task, 10)
    finally:
        writer.transport.abort()


# ---- dùng từ code đồng bộ (test có node là tiến trình con) --------------------------------------------


def test_usable_from_synchronous_test(tcp_proxy):
    listener = socket.create_server((LOCAL, 0))
    accepted: list[socket.socket] = []

    def serve() -> None:
        with contextlib.suppress(OSError):
            conn, _ = listener.accept()
            accepted.append(conn)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    proxy = tcp_proxy(listener.getsockname()[1])
    client = socket.create_connection((LOCAL, proxy.port), timeout=3)
    try:
        client.sendall(b"abc")
        t.join(3)
        assert accepted, "server không nhận được kết nối qua proxy"
        accepted[0].settimeout(3)
        assert accepted[0].recv(3) == b"abc"
        proxy.block()
        client.sendall(b"def")
        accepted[0].settimeout(0.5)
        with pytest.raises(TimeoutError):
            accepted[0].recv(3)
        proxy.rst()
        client.settimeout(3)
        with pytest.raises(ConnectionError):
            while client.recv(65536):
                pass
    finally:
        client.close()
        for c in accepted:
            c.close()
        listener.close()
