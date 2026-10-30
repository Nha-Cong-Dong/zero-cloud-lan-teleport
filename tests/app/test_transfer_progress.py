"""`TransferProgress` nói đúng sự thật khi dữ liệu ngừng chảy (spec hackathon PR-1 mục 1 “app/transfers.py”,
mục 3 ý 1–3).

Viết từ spec, không từ code:

- `_progress` mỗi `PROGRESS_INTERVAL`: phát khi `shown_bytes` đổi; **hoặc** lượt đã được phát ít nhất
  một lần và đang TRANSFERRING / RECONNECTING → phát mỗi nhịp kể cả khi đứng yên. Sau `SPEED_WINDOW`
  đứng yên: `bytes_per_second == 0`, `eta is None`, `idle_seconds` tăng dần. QUEUED / WAITING /
  VERIFYING không đổi thì không phát (hành vi cũ: tối đa một lần lúc mới thấy lượt).
- `shown_bytes = min(total, max(shown trước, done + unsynced))`: không bao giờ giảm, ≤ total.
  `info.done_bytes` giữ nghĩa cũ (bên nhận: bậc lô `SyncPart`); lịch sử ghi `done_bytes`.
- Tích hợp loopback ~5 MB/s (hằng thật): bên nhận, trong các đoạn TRANSFERRING hai `TransferProgress`
  liên tiếp cách ≤ 1,5 s (cộng độ trễ event loop đo được), không bước `shown_bytes` nào ≥ 8 MiB,
  dãy không giảm, kể cả qua ranh file.

Hai node thật trên 127.0.0.1 (`transfer_nodes`: A nhận, B gửi). “Mất mạng im lặng” giả bằng proxy TCP
(`tcp_proxy.block()`: không đóng gì, ngừng chuyển byte); “đang nối lại” bằng `block()` + `rst()` (kết
nối mới treo ở proxy nên lượt ở RECONNECTING tới khi `resume()`). `PROGRESS_INTERVAL` / `SPEED_WINDOW`
là seam cấp module (architecture §6), vá nhỏ để test nhanh.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import wait_until

from teleport.core import transfer as core_transfer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

LOCAL = "127.0.0.1"
MiB = 1024 * 1024
SYNC_BATCH = core_transfer.SYNC_EVERY * MiB  # bậc `done_bytes` bên nhận (khối 1 MiB, protocol §4.3)

INTERVAL = 0.2  # `PROGRESS_INTERVAL` vá trong test đứng yên
WINDOW = 0.6  # `SPEED_WINDOW` vá trong test đứng yên
STALL = 2.6  # giây đứng yên (dưới `IDLE_TIMEOUT` 5 s của `fast`, để lượt chưa sang RECONNECTING)
STALL_MAX = 4.0  # chờ đuôi đứng yên đủ dài tới tối đa chừng này giây sau khi chặn (< `IDLE_TIMEOUT`)
DRAIN_MAX = 3 * MiB  # byte `shown_bytes` còn được tăng sau khi chặn (bộ đệm socket, khối đang ghi/đọc trước)


def make_file(directory: Path, name: str, size: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with open(path, "wb") as f:
        left = size
        while left:
            chunk = os.urandom(min(left, 4 * MiB))
            f.write(chunk)
            left -= len(chunk)
    return path


def describe(fin) -> str:
    if fin is None:
        return "chưa có TransferFinished"
    return f"result={fin.result} by_peer={fin.by_peer} reason={fin.reason}"


def progress_of(node, tid: str) -> list[tuple[float, object]]:
    return node.trec.timed("TransferProgress", tid)


def assert_shown_invariants(prog) -> None:
    """Spec mục 1: 0 ≤ shown ≤ total; không bao giờ giảm; shown ≥ done (done là byte đã bền)."""
    shown = [p.shown_bytes for _, p in prog]
    assert all(b >= a for a, b in itertools.pairwise(shown)), f"shown_bytes giảm: {shown}"
    for _, p in prog:
        assert 0 <= p.shown_bytes <= p.info.total_bytes, (p.shown_bytes, p.info.total_bytes)
        assert p.shown_bytes >= p.info.done_bytes, (p.shown_bytes, p.info.done_bytes)
        assert p.idle_seconds >= 0.0
        assert p.bytes_per_second >= 0.0


def stalled_since(prog, t_block: float) -> float:
    """Mốc bắt đầu đứng yên: lần cuối `shown_bytes` đổi, không sớm hơn lúc chặn.

    Sau `block()` hai đầu vẫn còn chuyển nốt phần trong bộ đệm socket. Bên gửi đếm một khối (1 MiB)
    lúc đọc xong, trước khi ghi; khối kế được đếm khi khối trước đã vào bộ đệm socket, nên vẫn còn
    tăng tới khi bộ đệm đầy. Trên runner chậm việc đó có thể kéo dài quá 1 s sau khi chặn (CI
    windows / Python 3.12), nên mốc lấy theo số liệu thật chứ không theo một khoảng cố định; lượng
    byte còn tăng thì bị chặn bởi `DRAIN_MAX`.
    """
    t_move = t_block
    for (_, a), (t, b) in itertools.pairwise(prog):
        if b.shown_bytes != a.shown_bytes:
            t_move = max(t_move, t)
    return t_move


def assert_stalled_tail(tail, *, status: str, label: str) -> None:
    """Đoạn đuôi đứng yên (đã quá `SPEED_WINDOW`): tốc độ 0, eta None, shown không đổi, idle tăng."""
    assert len(tail) >= 3, f"{label}: chỉ {len(tail)} TransferProgress trong đoạn đứng yên"
    for _, p in tail:
        assert str(p.info.status) == status, f"{label}: trạng thái {p.info.status}, mong {status}"
        assert p.bytes_per_second == 0, f"{label}: đứng yên quá SPEED_WINDOW mà tốc độ {p.bytes_per_second}"
        assert p.eta is None, f"{label}: tốc độ 0 mà eta {p.eta}"
    shown = {p.shown_bytes for _, p in tail}
    assert len(shown) == 1, f"{label}: shown_bytes vẫn đổi khi đứng yên: {sorted(shown)}"
    idles = [p.idle_seconds for _, p in tail]
    assert all(b > a for a, b in itertools.pairwise(idles)), f"{label}: idle_seconds không tăng: {idles}"
    # idle đo từ lần cuối shown tăng: ít nhất bằng khoảng đã đứng yên trong đuôi.
    span = tail[-1][0] - tail[0][0]
    assert idles[-1] - idles[0] == pytest.approx(span, abs=2 * INTERVAL), (idles, span)
    assert idles[-1] >= WINDOW, f"{label}: idle_seconds cuối {idles[-1]}"


def assert_regular(prog, *, label: str, max_gap: float) -> None:
    gaps = [b - a for (a, _), (b, _) in itertools.pairwise(prog)]
    assert gaps, f"{label}: không đủ sự kiện để đo nhịp"
    assert max(gaps) <= max_gap, f"{label}: khoảng giữa hai TransferProgress {max(gaps):.2f} s > {max_gap} s"


@pytest.fixture
def T():
    from teleport.app import transfers

    return transfers


@pytest.fixture
def fast_progress(T, monkeypatch):
    monkeypatch.setattr(T, "PROGRESS_INTERVAL", INTERVAL)
    monkeypatch.setattr(T, "SPEED_WINDOW", WINDOW)


@pytest.fixture
def long_grace(monkeypatch):
    """Lượt không hết hạn nối lại trong lúc proxy chặn (`fast` đặt `RECONNECT_GRACE` ngắn)."""
    monkeypatch.setattr(core_transfer, "RECONNECT_GRACE", 120.0)


@pytest.fixture
def via_proxy(transfer_nodes, tcp_proxy):
    """Hai node nối kết nối điều khiển (và dữ liệu) **qua proxy**, B mở kết nối tới A."""

    async def make(**kw) -> SimpleNamespace:
        net = await transfer_nodes(client="sender", connect=False, **kw)
        A, B = net.A, net.B
        proxy = tcp_proxy(A.port)
        B.connect_hint(A.id, [LOCAL], proxy.port)
        await wait_until(
            lambda: A.id in B.connected_devices() and B.id in A.connected_devices(),
            5,
            "chưa nối được kết nối điều khiển qua proxy",
        )
        return SimpleNamespace(net=net, A=A, B=B, proxy=proxy)

    return make


class WriteGate:
    """Chặn lần ghi khối thứ `at` (mọi lượt) của bên nhận tới khi `release()` (≤ 60 s)."""

    def __init__(self, at: int) -> None:
        self.at = at
        self.count = 0
        self.lock = threading.Lock()
        self.hit = threading.Event()
        self.gate = threading.Event()

    def before_write(self) -> None:
        with self.lock:
            self.count += 1
            block = self.count == self.at
        if block:
            self.hit.set()
            if not self.gate.wait(60):
                raise AssertionError("cổng chặn ghi của test không được mở")

    def release(self) -> None:
        self.gate.set()


@pytest.fixture
def write_gate(monkeypatch):
    from teleport.app import storage

    gates: list[WriteGate] = []
    real = storage.PartFile.write_at

    def install(at: int) -> WriteGate:
        g = WriteGate(at)
        gates.append(g)

        def write_at(self, index, data):
            g.before_write()
            return real(self, index, data)

        monkeypatch.setattr(storage.PartFile, "write_at", write_at)
        return g

    yield install
    for g in gates:
        g.release()


# =================================================================================================
# Ý 1: đứng yên (mất mạng im lặng) vẫn phát mỗi nhịp; tốc độ về 0; idle tăng
# =================================================================================================


@pytest.mark.slow
@pytest.mark.parametrize("side", ["receiver", "sender"])
async def test_stalled_transferring_keeps_reporting_with_zero_speed(T, via_proxy, fast_progress, side):
    p = await via_proxy(sender_opts={"rate_limit": 4 * MiB})
    A, B = p.A, p.B
    src = make_file(p.net.src, "f.bin", 16 * MiB)
    key = await B.transfers.send(A.id, [src])
    tid = key.transfer_id
    node = A if side == "receiver" else B
    await wait_until(
        lambda: any(u.shown_bytes >= 2 * MiB for u in A.trec.of("TransferProgress", tid)),
        20,
        "bên nhận chưa nhận được 2 MiB",
    )
    p.proxy.block()
    t_block = time.monotonic()
    shown_at_block = max((u.shown_bytes for t, u in progress_of(node, tid) if t <= t_block), default=0)

    def tail() -> list[tuple[float, object]]:
        # Quá `SPEED_WINDOW` (thêm nửa nhịp cho độ trễ của nhịp) kể từ lần cuối `shown_bytes` đổi.
        prog = progress_of(node, tid)
        start = stalled_since(prog, t_block) + WINDOW + INTERVAL / 2
        return [(t, u) for t, u in prog if t >= start]

    await asyncio.sleep(STALL)
    await wait_until(
        lambda: len(tail()) >= 4,
        max(0.0, t_block + STALL_MAX - time.monotonic()),
        f"{side}: tiến độ chưa đứng yên sau khi chặn",
    )
    t_end = time.monotonic()
    assert node.trec.finished(tid) is None, describe(node.trec.finished(tid))

    during = [(t, u) for t, u in progress_of(node, tid) if t_block <= t <= t_end]
    assert len(during) >= STALL / INTERVAL / 2, (
        f"{side}: chỉ {len(during)} TransferProgress trong {STALL} s đứng yên (nhịp {INTERVAL} s)"
    )
    assert_regular(during, label=side, max_gap=3 * INTERVAL)
    assert_stalled_tail([(t, u) for t, u in tail() if t <= t_end], status="transferring", label=side)
    drained = during[-1][1].shown_bytes - shown_at_block
    assert drained <= DRAIN_MAX, f"{side}: mạng đã chặn mà shown_bytes còn tăng {drained} byte"
    assert_shown_invariants(progress_of(node, tid))

    p.proxy.resume()
    fin_b = await B.trec.wait_finished(tid, 60)
    fin_a = await A.trec.wait_finished(tid, 60)
    assert fin_b.result == T.TransferResult.DONE, describe(fin_b)
    assert fin_a.result == T.TransferResult.DONE, describe(fin_a)
    # Sau khi đi tiếp: tốc độ lại > 0 ở ít nhất một sự kiện, và không có tiến độ sau TransferFinished.
    after = [u for t, u in progress_of(node, tid) if t > t_end]
    assert any(u.bytes_per_second > 0 for u in after), f"{side}: đi tiếp mà tốc độ vẫn 0"
    t_fin = node.trec.timed("TransferFinished", tid)[0][0]
    assert all(t <= t_fin for t, _ in progress_of(node, tid)), "còn tiến độ sau TransferFinished"
    assert_shown_invariants(progress_of(node, tid))


@pytest.mark.slow
@pytest.mark.parametrize("side", ["receiver", "sender"])
async def test_reconnecting_transfer_keeps_reporting(T, via_proxy, fast_progress, long_grace, side):
    p = await via_proxy(sender_opts={"rate_limit": 4 * MiB})
    A, B = p.A, p.B
    src = make_file(p.net.src, "f.bin", 12 * MiB)
    key = await B.transfers.send(A.id, [src])
    tid = key.transfer_id
    node = A if side == "receiver" else B
    await wait_until(
        lambda: any(u.shown_bytes >= 2 * MiB for u in A.trec.of("TransferProgress", tid)),
        20,
        "bên nhận chưa nhận được 2 MiB",
    )
    p.proxy.block()
    p.proxy.rst()  # kết nối mới treo ở proxy tới khi resume(): lượt ở RECONNECTING
    await wait_until(
        lambda: "reconnecting" in node.trec.statuses(tid), 15, f"{side}: lượt chưa sang RECONNECTING"
    )
    t_rec = next(
        t for t, u in node.trec.timed("TransferStatusChanged", tid) if str(u.info.status) == "reconnecting"
    )
    await asyncio.sleep(STALL)
    t_end = time.monotonic()
    assert node.trec.finished(tid) is None, describe(node.trec.finished(tid))
    during = [(t, u) for t, u in progress_of(node, tid) if t_rec <= t <= t_end]
    assert len(during) >= STALL / INTERVAL / 2, f"{side}: chỉ {len(during)} TransferProgress khi RECONNECTING"
    assert_regular(during, label=side, max_gap=3 * INTERVAL)
    tail = [(t, u) for t, u in during if t >= t_rec + WINDOW + INTERVAL]
    assert_stalled_tail(tail, status="reconnecting", label=side)
    assert_shown_invariants(progress_of(node, tid))

    p.proxy.resume()
    fin_b = await B.trec.wait_finished(tid, 90)
    fin_a = await A.trec.wait_finished(tid, 90)
    assert fin_b.result == T.TransferResult.DONE, describe(fin_b)
    assert fin_a.result == T.TransferResult.DONE, describe(fin_a)
    assert_shown_invariants(progress_of(node, tid))


# =================================================================================================
# Ý 1 (vế sau): QUEUED / WAITING không đổi thì không phát mỗi nhịp
# =================================================================================================


@pytest.mark.slow
async def test_waiting_offer_is_not_reported_every_tick(T, transfer_nodes, fast_progress):
    net = await transfer_nodes(receiver_opts={"auto_accept": False})
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 3 * MiB)])
    tid = key.transfer_id
    offer = await A.trec.wait_kind("OfferReceived", tid)
    assert B.transfers.get(key).status == T.TransferStatus.WAITING
    t0 = time.monotonic()
    await asyncio.sleep(10 * INTERVAL)
    for node in (A, B):
        prog = progress_of(node, tid)
        assert len(prog) <= 1, f"{node.label}: {len(prog)} TransferProgress cho lượt WAITING không đổi"
        assert all(str(u.info.status) == "waiting" for _, u in prog)
    assert A.transfers.get(offer.key).status == T.TransferStatus.WAITING
    assert time.monotonic() - t0 >= 10 * INTERVAL
    A.transfers.decide(offer.key, False)
    await B.trec.wait_finished(tid, 15)
    await A.trec.wait_finished(tid, 15)


@pytest.mark.slow
async def test_queued_transfer_is_not_reported_every_tick(T, transfer_nodes, fast_progress, write_gate):
    """Lượt đầu bị chặn ghi giữa chừng (đứng yên ở TRANSFERRING), lượt hai cùng thiết bị cùng chiều
    QUEUED: lượt hai không có `TransferProgress` mỗi nhịp; lượt đầu thì có."""
    gate = write_gate(at=3)
    net = await transfer_nodes()
    A, B = net.A, net.B
    k1 = await B.transfers.send(A.id, [make_file(net.src, "a.bin", 8 * MiB)])
    await wait_until(gate.hit.is_set, 20, "bên nhận chưa ghi tới khối thứ 3")
    k2 = await B.transfers.send(A.id, [make_file(net.src, "b.bin", 2 * MiB)])
    await wait_until(
        lambda: B.transfers.get(k2) is not None and B.transfers.get(k2).status == T.TransferStatus.QUEUED,
        5,
        "lượt hai chưa QUEUED",
    )
    t0 = time.monotonic()
    await asyncio.sleep(10 * INTERVAL)
    try:
        for node in (A, B):
            queued = progress_of(node, k2.transfer_id)
            assert len(queued) <= 1, f"{node.label}: {len(queued)} TransferProgress cho lượt QUEUED"
            assert all(str(u.info.status) == "queued" for _, u in queued)
        first = [(t, u) for t, u in progress_of(A, k1.transfer_id) if t >= t0]
        assert len(first) >= 5, f"lượt đầu đứng yên ở TRANSFERRING chỉ có {len(first)} TransferProgress"
    finally:
        gate.release()
    for k in (k1, k2):
        fin = await B.trec.wait_finished(k.transfer_id, 60)
        assert fin.result == T.TransferResult.DONE, describe(fin)
        await A.trec.wait_finished(k.transfer_id, 60)


# =================================================================================================
# Ý 2: shown_bytes không giảm, ≤ total; done_bytes giữ nghĩa cũ (bên nhận bậc lô sync; lịch sử)
# =================================================================================================


@pytest.mark.slow
async def test_receiver_done_bytes_stays_on_sync_batches_while_shown_runs_ahead(
    T, transfer_nodes, fast_progress
):
    net = await transfer_nodes(sender_opts={"rate_limit": 8 * MiB})
    A, B = net.A, net.B
    size = 36 * MiB + 4321
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", size)])
    tid = key.transfer_id
    fin_a = await A.trec.wait_finished(tid, 60)
    fin_b = await B.trec.wait_finished(tid, 60)
    assert fin_a.result == T.TransferResult.DONE, describe(fin_a)
    assert fin_b.result == T.TransferResult.DONE, describe(fin_b)
    prog = progress_of(A, tid)
    assert len(prog) >= 5
    assert_shown_invariants(prog)
    for _, p in prog:
        d = p.info.done_bytes
        assert d % SYNC_BATCH == 0 or d == size, f"done_bytes bên nhận {d} không theo bậc lô sync"
    assert any(p.shown_bytes > p.info.done_bytes for _, p in prog), (
        "shown_bytes không bao giờ vượt done_bytes: byte đã ghi chưa sync không được tính"
    )
    assert_shown_invariants(progress_of(B, tid))
    for _, p in progress_of(B, tid):
        assert p.info.unsynced_bytes == 0


@pytest.mark.slow
async def test_cancelled_receive_records_durable_done_bytes_in_history(
    T, transfer_nodes, fast_progress, write_gate
):
    gate = write_gate(at=20)  # lô 0..15 đã synced; 16..19 đã giao ghi, chưa bền
    net = await transfer_nodes(sender_opts={"rate_limit": 8 * MiB})
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 32 * MiB)])
    tid = key.transfer_id
    await wait_until(gate.hit.is_set, 30, "bên nhận chưa ghi tới khối thứ 20")
    await wait_until(
        lambda: any(u.shown_bytes > SYNC_BATCH for u in A.trec.of("TransferProgress", tid)),
        5,
        "shown_bytes bên nhận chưa vượt lô sync đầu",
    )
    last_shown = A.trec.of("TransferProgress", tid)[-1].shown_bytes
    rkey = T.TransferKey(B.id, T.Direction.RECEIVE, tid)
    A.transfers.cancel(rkey)
    fin_a = await A.trec.wait_finished(tid, 15)
    gate.release()
    await B.trec.wait_finished(tid, 15)
    assert fin_a.result == T.TransferResult.CANCELLED_USER, describe(fin_a)
    done = fin_a.info.done_bytes
    assert done % SYNC_BATCH == 0 and done < last_shown, (done, last_shown)
    entry = next(e for e in A.transfers.history() if e.transfer_id == tid)
    assert entry.done_bytes == done, f"lịch sử ghi {entry.done_bytes}, mong done_bytes {done} (byte đã bền)"


# =================================================================================================
# Ý 3: tích hợp loopback ~5 MB/s, hằng thật: nhịp ≤ 1,5 s, bước < 8 MiB, không giảm (cả qua ranh file)
# =================================================================================================

LOOPBACK_RATE = 5_000_000  # byte/s (thực tế `--limit-rate` trên Windows ~4,6 MB/s)
MAX_GAP = 1.5
MAX_STEP = 8 * MiB
LAG_TICK = 0.05  # nhịp của task đo độ trễ event loop


class LoopLag:
    """Đo độ trễ event loop: task ngủ `LAG_TICK` liên tục, ghi (lúc thức, trễ so với hẹn).

    Loop bị kẹt (máy bận, Defender quét file, GC) thì `TransferProgress` trễ theo, không phải lỗi luật
    phát. `max_lag(a, b)`: tổng trễ của các lần kẹt chồng lên khoảng [a, b] (vài lần kẹt riêng
    cộng dồn; trễ ≤ `LAG_TICK` là độ mịn đồng hồ, không tính).
    """

    def __init__(self) -> None:
        self.samples: list[tuple[float, float]] = []
        self.task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            due = time.monotonic() + LAG_TICK
            await asyncio.sleep(LAG_TICK)
            now = time.monotonic()
            self.samples.append((now, max(0.0, now - due)))

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    def max_lag(self, a: float, b: float) -> float:
        # Lần kẹt kết thúc lúc `t`, bắt đầu khoảng `t - lag`: chồng lên [a, b] khi t ≥ a và t - lag ≤ b.
        return sum(lag for t, lag in self.samples if lag > LAG_TICK and t >= a and t - lag <= b)


def transferring_segments(node, tid: str, t_end: float) -> list[tuple[float, float]]:
    """Các đoạn [vào TRANSFERRING, đổi trạng thái kế tiếp) theo `TransferStatusChanged` của lượt."""
    changes = node.trec.timed("TransferStatusChanged", tid)
    segs = []
    for i, (t, u) in enumerate(changes):
        if str(u.info.status) != "transferring":
            continue
        nxt = changes[i + 1][0] if i + 1 < len(changes) else t_end
        segs.append((t, nxt))
    return segs


def assert_regular_while_transferring(node, tid: str, lag: LoopLag, *, t_end: float, max_gap: float) -> None:
    """Spec mục 3 ý 3: khi TRANSFERRING, ≥ 1 `TransferProgress` mỗi `max_gap` giây (cộng độ trễ loop).

    Chỉ đo trong từng đoạn TRANSFERRING: VERIFYING (đọc lại/đổi tên file giữa các file) không đổi thì
    không phát (spec mục 1), nên khoảng qua VERIFYING không tính. Trong đoạn: từ lúc vào TRANSFERRING tới
    progress đầu, giữa hai progress liên tiếp, và từ progress cuối tới lúc rời đoạn.
    """
    segs = transferring_segments(node, tid, t_end)
    assert segs, f"lượt {tid} không có đoạn TRANSFERRING nào"
    prog = progress_of(node, tid)
    checked = 0
    for start, stop in segs:
        inside = [t for t, p in prog if start <= t < stop and str(p.info.status) == "transferring"]
        marks = [start, *inside, stop]
        for a, b in itertools.pairwise(marks):
            allowed = max_gap + lag.max_lag(a, b)
            assert b - a <= allowed, (
                f"bên nhận: khoảng không có TransferProgress {b - a:.2f} s > {allowed:.2f} s "
                f"(đoạn TRANSFERRING {start - segs[0][0]:.2f}..{stop - segs[0][0]:.2f} s, "
                f"mốc {a - segs[0][0]:.2f}..{b - segs[0][0]:.2f} s, trễ loop {allowed - max_gap:.2f} s)"
            )
            checked += 1
    assert checked >= 5, f"chỉ {checked} khoảng trong các đoạn TRANSFERRING"


@pytest.mark.slow
@pytest.mark.parametrize(
    "sizes",
    [
        pytest.param((40 * MiB,), id="one-file"),
        pytest.param((12 * MiB, 9 * MiB + 12345, 14 * MiB), id="three-files"),
    ],
)
async def test_receiver_progress_is_smooth_at_5_mb_per_second(T, transfer_nodes, sizes):
    net = await transfer_nodes(sender_opts={"rate_limit": LOOPBACK_RATE})
    A, B = net.A, net.B
    paths = [make_file(net.src, f"f{i}.bin", s) for i, s in enumerate(sizes)]
    lag = LoopLag()
    lag.start()
    try:
        key = await B.transfers.send(A.id, paths)
        tid = key.transfer_id
        fin_a = await A.trec.wait_finished(tid, 90)
        fin_b = await B.trec.wait_finished(tid, 90)
    finally:
        await lag.stop()
    assert fin_a.result == T.TransferResult.DONE, describe(fin_a)
    assert fin_b.result == T.TransferResult.DONE, describe(fin_b)
    prog = progress_of(A, tid)
    total = sum(sizes)
    assert len(prog) >= 5, f"chỉ {len(prog)} TransferProgress cho lượt ~{total / LOOPBACK_RATE:.0f} s"
    assert_shown_invariants(prog)
    t_fin = A.trec.timed("TransferFinished", tid)[0][0]
    assert_regular_while_transferring(A, tid, lag, t_end=t_fin, max_gap=MAX_GAP)
    shown = [0] + [p.shown_bytes for _, p in prog]
    steps = [b - a for a, b in itertools.pairwise(shown)]
    assert max(steps) < MAX_STEP, f"bước shown_bytes lớn nhất {max(steps) / MiB:.1f} MiB: {steps}"
    if len(sizes) > 1:
        assert {p.info.file_index for _, p in prog} >= {0, 1, 2}, "chưa có tiến độ ở mọi file"


# =================================================================================================
# Luật phát từng nhịp (`_progress_tick`), không socket: engine giả trả `TransferInfo` do test dựng
# =================================================================================================


class FakeEngine:
    def __init__(self) -> None:
        self.infos: list = []

    def transfers(self):
        return list(self.infos)


def bare_manager(T) -> tuple[object, FakeEngine, list]:
    """`TransferManager` không qua `__init__`: chỉ đủ trường mà `_progress_tick` / `_emit` dùng."""
    events: list = []
    engine = FakeEngine()
    m = object.__new__(T.TransferManager)
    m._engine = engine
    m._samples = {}
    m._progress_states = {}
    m._closed = False
    m._on_event = events.append
    return m, engine, events


def make_info(T, status, done: int, unsynced: int, total: int = 10 * MiB):
    key = T.TransferKey("peer", T.Direction.RECEIVE, "t1")
    return core_transfer.TransferInfo(
        key=key,
        status=status,
        files=(),
        names=("f.bin",),
        file_index=0,
        done_bytes=done,
        total_bytes=total,
        unsynced_bytes=unsynced,
    )


def test_tick_reports_when_only_done_changes_in_verifying(T):
    """Lô cuối `synced` rồi sang VERIFYING: shown không đổi nhưng done tăng → vẫn phát (CLI thấy 100 %)."""
    m, engine, events = bare_manager(T)
    total = 10 * MiB
    engine.infos = [make_info(T, T.TransferStatus.TRANSFERRING, 8 * MiB, 2 * MiB, total)]
    m._progress_tick(1.0)
    assert [e.shown_bytes for e in events] == [total]
    assert events[-1].info.done_bytes == 8 * MiB
    engine.infos = [make_info(T, T.TransferStatus.VERIFYING, total, 0, total)]
    m._progress_tick(2.0)
    assert len(events) == 2, "done_bytes đổi khi VERIFYING mà không phát TransferProgress"
    assert events[-1].info.done_bytes == total
    assert events[-1].shown_bytes == total
    # VERIFYING không đổi gì nữa thì không phát mỗi nhịp.
    m._progress_tick(3.0)
    m._progress_tick(4.0)
    assert len(events) == 2


def test_tick_idle_counts_activity_even_when_shown_does_not_grow(T):
    """Dữ liệu chảy lại cho byte đã tính (raw giảm rồi tăng, shown kẹp không đổi) không bị báo đứng yên."""
    m, engine, events = bare_manager(T)
    S = T.TransferStatus.TRANSFERRING
    engine.infos = [make_info(T, S, 4 * MiB, 3 * MiB)]
    m._progress_tick(1.0)
    m._progress_tick(2.0)  # đứng yên thật: raw không đổi
    assert events[-1].idle_seconds == pytest.approx(1.0)
    engine.infos = [make_info(T, S, 4 * MiB, 0)]  # nối lại bỏ khối chưa sync: raw giảm
    m._progress_tick(3.0)
    assert events[-1].idle_seconds == 0.0
    assert events[-1].shown_bytes == 7 * MiB  # shown không giảm
    engine.infos = [make_info(T, S, 4 * MiB, 2 * MiB)]  # khối gửi lại: raw tăng nhưng < shown
    m._progress_tick(4.0)
    assert events[-1].idle_seconds == 0.0
    assert events[-1].shown_bytes == 7 * MiB
    m._progress_tick(5.5)
    assert events[-1].idle_seconds == pytest.approx(1.5)
