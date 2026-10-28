"""Tạm dừng lượt nhận khi đóng node có trật tự, `keep_record`, `payload_sent` (P3-3b, việc V2 (b), (d), V3).

Viết từ đặc tả, không từ code: `specs/spec-p3-3b.md` mục 2.1 (`TransferResult.SUSPENDED`,
`TransferFinished.payload_sent`, `TransferResumed`), 2.4 (tạm dừng: `begin_close` gọi
`suspend_receiving` trước rồi mới hủy lượt gửi và `offer` chờ; `SuspendTemp` chạy sau các thao tác đứng
trước trong hàng đĩa; `close_temp` thay `remove_temp`; `RemoveTemp.keep_record`), 2.5 (`payload_sent`), 5
(V2 (b), (d); V3: đóng trật tự giữa lượt → mở lại → hoàn tất trong ngưỡng mvp N4; S1), 8.1 mục 2, 5;
`specs/spec-p3.md` mục P3-3b (“Xong khi”, S1); protocol §6.2 (“Tắt app nhận”).

Quy ước như `test_transfers.py`: hai node thật trên 127.0.0.1 (`transfer_nodes`), **A nhận, B gửi**. Mở
lại một node = `node_factory` cùng nhãn (cùng thư mục cấu hình) với `TransferRecorder` mới. Hạn chờ nối
lại đặt lại giá trị thật (24 giờ) để lượt không hết hạn trong lúc A tắt (fixture `fast` đặt 4 s).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from teleport.app import storage
from teleport.core import transfer as core_transfer
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.merkle import merkle_root
from teleport.core.messages import Cancel, Complete, DataOpen, FileEntry, Offer, Reject, Resume
from teleport.net import connection

pytestmark = [
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
integration = pytest.mark.integration
leakcheck = pytest.mark.usefixtures("no_leaks")

ROOT = Path(__file__).resolve().parents[2]
LOCAL = "127.0.0.1"
MiB = 1024 * 1024
REAL_GRACE = core_transfer.RECONNECT_GRACE  # đọc lúc import, trước khi `fast` vá


def n4_slack_bytes() -> int:
    """Phần cộng thêm của mvp N4 (“gửi lại ≤ phần chưa nhận + X MiB”), đọc từ bảng yêu cầu."""
    text = (ROOT / "docs" / "mvp.md").read_text(encoding="utf-8")
    m = re.search(r"^\| N4 \|[^\n]*?phần chưa nhận \+ (\d+) MiB", text, re.MULTILINE)
    assert m, "không tìm thấy ngưỡng N4 trong docs/mvp.md"
    return int(m.group(1)) * MiB


N4_SLACK = n4_slack_bytes()


# --- Hàm phụ trợ ---------------------------------------------------------------------------------


async def wait_until(predicate, timeout: float = 5.0, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.01)


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


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4 * MiB):
            h.update(chunk)
    return h.hexdigest()


def temp_dir(receive_dir: Path, tid: str) -> Path:
    return receive_dir / ".teleport" / tid


def describe(fin) -> str:
    if fin is None:
        return "chưa có TransferFinished"
    return f"result={fin.result} by_peer={fin.by_peer} reason={fin.reason} file_index={fin.file_index}"


def marked_blocks(resume_helpers, state: Path) -> set[int]:
    """Các khối đang được đánh dấu trong `.state` (MARK trừ CLEAR/CLEAR_ALL sau đó), đọc độc lập."""
    _, records = resume_helpers.parse_state(state.read_bytes())
    marked: set[int] = set()
    for r in records:
        if r[0] == "mark":
            marked |= {i for i, _ in r[1]}
        elif r[0] == "clear":
            marked -= set(r[1])
        elif r[0] == "clear_all":
            marked.clear()
    return marked


@pytest.fixture
def T():
    from teleport.app import transfers

    return transfers


@pytest.fixture
def grace_real(monkeypatch):
    """Hạn chờ nối lại thật (24 giờ): lượt không hết hạn trong lúc một bên tắt."""
    monkeypatch.setattr(core_transfer, "RECONNECT_GRACE", REAL_GRACE)


@pytest.fixture
def restart(node_factory, app_helpers):
    """`await restart(label, **node_kwargs)` → `AppNode` mới cùng thư mục cấu hình, có `trec` mới."""

    async def make(label: str, **node_kwargs):
        trec = app_helpers.TransferRecorder(f"{label}'")
        node_kwargs.setdefault("on_transfer_event", trec.on_update)
        node = await node_factory(label, node_kwargs=node_kwargs)
        node.trec = trec
        return node

    return make


@pytest.fixture
def write_spy(monkeypatch):
    """Spy trên `storage.PartFile.write_at`: `done` = (transfer_id, file_index, index) mỗi lần ghi xong;
    `hooks`: hàm `(part, index)` chạy trong thread đĩa trước khi ghi thật."""
    spy = SimpleNamespace(calls=[], done=[], hooks=[], lock=threading.Lock())
    real = storage.PartFile.write_at

    def write_at(self, index, data):
        rec = (self.path.parent.name, self.file_index, index)
        with spy.lock:
            spy.calls.append(rec)
        for hook in list(spy.hooks):
            hook(self, index)
        real(self, index, data)
        with spy.lock:
            spy.done.append(rec)

    def count(tid: str, *, done: bool = False) -> int:
        with spy.lock:
            return sum(1 for r in (spy.done if done else spy.calls) if r[0] == tid)

    spy.count = count
    monkeypatch.setattr(storage.PartFile, "write_at", write_at)
    return spy


@pytest.fixture
def temp_spy(monkeypatch):
    """Spy trên `ReceiveDir.close_temp` / `remove_temp`: (tên lệnh, transfer_id, keep_record)."""
    calls: list[tuple[str, str, bool | None]] = []
    real_close, real_remove = storage.ReceiveDir.close_temp, storage.ReceiveDir.remove_temp

    def close_temp(self, temp):
        calls.append(("close_temp", temp.path.name, None))
        return real_close(self, temp)

    def remove_temp(self, temp, *, keep_record=False):
        calls.append(("remove_temp", temp.path.name, keep_record))
        return real_remove(self, temp, keep_record=keep_record)

    monkeypatch.setattr(storage.ReceiveDir, "close_temp", close_temp)
    monkeypatch.setattr(storage.ReceiveDir, "remove_temp", remove_temp)
    return calls


@pytest.fixture
def wire_spy(monkeypatch):
    """Spy trên `Connection.send`: mọi (kết nối, message) đã đưa ra kết nối điều khiển, cả hai node."""
    sent: list[tuple[object, object]] = []
    real = connection.Connection.send

    async def send(self, msg):
        sent.append((self, msg))
        return await real(self, msg)

    monkeypatch.setattr(connection.Connection, "send", send)
    return sent


def cancels_or_rejects(sent, tid: str) -> list:
    return [m for _, m in sent if isinstance(m, Cancel | Reject) and m.transfer_id == tid]


# =================================================================================================
# API (không socket)
# =================================================================================================


def test_transfer_finished_payload_sent_defaults_to_zero(T):
    info = core_transfer.TransferInfo(
        key=core_transfer.TransferKey("a" * 32, core_transfer.Direction.SEND, "b" * 32),
        status=core_transfer.TransferStatus.TRANSFERRING,
        files=(FileEntry("x.bin", 1, 0),),
        names=("x.bin",),
        file_index=0,
        done_bytes=0,
        total_bytes=1,
    )
    fin = T.TransferFinished(info, T.TransferResult.DONE, False, None, None, None)
    assert fin.payload_sent == 0
    assert T.TransferResumed(info).info is info


def test_transfer_resumed_is_part_of_transfer_update(T):
    assert T.TransferResumed in T.TransferUpdate.__args__


def test_wall_time_seam_defaults_to_time_time(T):
    """Seam giờ tường (spec-p3-3b mục 2.1): tuổi lượt khôi phục, `at` của bia mộ, `wall_now` của dọn tạm."""
    assert T._wall_time is time.time


# =================================================================================================
# V2 (d): `payload_sent`
# =================================================================================================


@integration
@leakcheck
@pytest.mark.parametrize("rate", [None, 16 * MiB], ids=["khong-gioi-han", "gioi-han"])
async def test_payload_sent_of_clean_transfer_is_size_within_one_chunk_receiver_zero(T, transfer_nodes, rate):
    sizes = [0, MiB, 3 * MiB + 7]
    net = await transfer_nodes(sender_opts={"rate_limit": rate} if rate else None)
    A, B = net.A, net.B
    files = [make_file(net.src, f"f{k}.bin", size) for k, size in enumerate(sizes)]
    key = await B.transfers.send(A.id, files)
    fin_b = await B.trec.wait_finished(key.transfer_id, 60)
    fin_a = await A.trec.wait_finished(key.transfer_id, 60)
    assert fin_b.result == T.TransferResult.DONE, describe(fin_b)
    assert fin_a.result == T.TransferResult.DONE, describe(fin_a)
    total = sum(sizes)
    assert total <= fin_b.payload_sent <= total + connection._SEND_CHUNK, fin_b.payload_sent
    assert fin_a.payload_sent == 0


@integration
@leakcheck
async def test_payload_sent_after_sender_cancel_counts_at_least_what_receiver_wrote(
    T, transfer_nodes, write_spy
):
    net = await transfer_nodes(sender_opts={"rate_limit": 4 * MiB})
    A, B = net.A, net.B
    size = 16 * MiB
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", size)])
    tid = key.transfer_id
    await wait_until(lambda: write_spy.count(tid, done=True) >= 3, 20, "A chưa ghi khối nào")
    B.transfers.cancel(key)
    fin_b = await B.trec.wait_finished(tid, 10)
    assert fin_b.result == T.TransferResult.CANCELLED_USER
    written = write_spy.count(tid, done=True) * BLOCK_SIZE
    assert written <= fin_b.payload_sent <= size + connection._SEND_CHUNK, (written, fin_b.payload_sent)


# =================================================================================================
# V2 (b): tạm dừng, `keep_record`
# =================================================================================================


@integration
@leakcheck
async def test_done_transfer_removes_temp_with_keep_record_and_keeps_only_transfer_json(
    T, transfer_nodes, temp_spy
):
    net = await transfer_nodes()
    A, B = net.A, net.B
    src = make_file(net.src, "f.bin", 2 * MiB + 1)
    key = await B.transfers.send(A.id, [src])
    tid = key.transfer_id
    assert (await A.trec.wait_finished(tid, 30)).result == T.TransferResult.DONE
    await wait_until(lambda: any(c[1] == tid for c in temp_spy), 10, "chưa dọn thư mục tạm")
    assert [c for c in temp_spy if c[1] == tid] == [("remove_temp", tid, True)]
    d = temp_dir(net.receive_dir, tid)
    await wait_until(lambda: d.is_dir() and os.listdir(d) == ["transfer.json"], 10, "chỉ còn transfer.json")
    from teleport.core import resume_state as rs

    rec = rs.decode_transfer_record((d / "transfer.json").read_bytes(), dir_name=tid)
    assert rec.verified == 1 and rec.done_at is not None
    assert (rec.receiver, rec.sender) == (A.id, B.id)


@integration
@leakcheck
async def test_cancelled_transfer_removes_temp_without_keep_record(T, transfer_nodes, temp_spy, write_spy):
    net = await transfer_nodes(sender_opts={"rate_limit": 4 * MiB})
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 16 * MiB)])
    tid = key.transfer_id
    await wait_until(lambda: write_spy.count(tid) >= 2, 20, "A chưa nhận khối nào")
    A.transfers.cancel(core_transfer.TransferKey(B.id, core_transfer.Direction.RECEIVE, tid))
    await wait_until(lambda: not temp_dir(net.receive_dir, tid).exists(), 10, "thư mục tạm chưa bị xóa")
    assert [c for c in temp_spy if c[1] == tid] == [("remove_temp", tid, False)]


@integration
@leakcheck
async def test_suspend_runs_queued_writes_and_sync_then_only_closes_handles(
    T, transfer_nodes, write_spy, temp_spy, resume_helpers, grace_real, monkeypatch
):
    """Hàng đĩa đang kẹt ở một lần ghi khi node đóng: các `WriteBlock` và `SyncPart` đứng trước `SuspendTemp`
    vẫn chạy (không bị bỏ), nên mọi khối đã ghi xong đều có `MARK` bền trong `.state`; rồi chỉ `close_temp`,
    không `remove_temp`; `.part`, `.state`, `transfer.json` còn.

    `CLOSE_DISK_WAIT` nới rộng: ca này kiểm thao tác không bị bỏ, không kiểm thời gian (hàng đĩa chạy nốt
    quá `CLOSE_DISK_WAIT` thì mất `MARK` lô cuối là rủi ro R5 đã chấp nhận, spec-p3-3b mục 6)."""
    monkeypatch.setattr(T, "CLOSE_DISK_WAIT", 30.0)
    net = await transfer_nodes(sender_opts={"rate_limit": 8 * MiB})
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 24 * MiB)])
    tid = key.transfer_id
    gate = threading.Event()
    hit = threading.Event()

    def hold_block_5(part, index):
        if part.path.parent.name == tid and index == 5 and not hit.is_set():
            hit.set()
            gate.wait(30)

    read: list[int] = []  # chỉ số các khung A đã đọc được (mỗi khung → một `WriteBlock` xếp hàng)
    real_read = connection.Connection.read_block

    async def read_block(self, *args, **kwargs):
        result = await real_read(self, *args, **kwargs)
        if result is not None:
            read.append(result[0].index)
        return result

    monkeypatch.setattr(connection.Connection, "read_block", read_block)
    write_spy.hooks.append(hold_block_5)
    await wait_until(hit.is_set, 20, "chưa tới khối 5")
    await wait_until(lambda: max(read, default=0) >= 8, 10, "chưa có khối nào xếp sau khối 5")
    closing = asyncio.create_task(A.close())
    try:
        await A.trec.wait_kind("TransferFinished", tid, timeout=5)
        assert A.trec.finished(tid).result == T.TransferResult.SUSPENDED
        gate.set()  # trong CLOSE_DISK_WAIT: hàng đĩa chạy nốt
        await asyncio.wait_for(closing, 15)
    finally:
        gate.set()
    d = temp_dir(net.receive_dir, tid)
    assert {"transfer.json", "0.part", "0.state"} <= set(os.listdir(d))
    written = {r[2] for r in write_spy.done if r[0] == tid and r[1] == 0}
    assert 5 in written and max(written) > 5
    assert marked_blocks(resume_helpers, d / "0.state") == written, "mọi khối đã ghi phải có MARK bền"
    mine = [c for c in temp_spy if c[1] == tid]
    assert mine == [("close_temp", tid, None)], mine


@integration
@leakcheck
async def test_begin_close_suspends_receiving_before_cancelling_sends_and_pending_offers(
    T, transfer_nodes, node_factory, app_helpers, wire_spy, write_spy, grace_real, tmp_path
):
    """A vừa nhận (từ B, đang chạy) vừa gửi (cho B) và có `offer` chờ hỏi (từ C): đóng A → lượt nhận
    `SUSPENDED` (không `cancel`), lượt gửi `cancel{user}`, `offer` chờ hỏi `reject{user}` (spec mục 8.1.4);
    `SUSPENDED` được báo trước các kết quả kia."""
    net = await transfer_nodes(
        receiver_opts={"auto_accept": False, "rate_limit": MiB},
        sender_opts={"rate_limit": 4 * MiB, "receive_dir": tmp_path / "nhan-B", "auto_accept": True},
        extra_trusted=("C",),
    )
    A, B = net.A, net.B
    crec = app_helpers.TransferRecorder("C")
    C = await node_factory("C", node_kwargs={"on_transfer_event": crec.on_update})
    C.connect_hint(A.id, [LOCAL], A.port)
    await wait_until(lambda: C.id in A.connected_devices(), 5, "C chưa nối tới A")
    recv_key = await B.transfers.send(A.id, [make_file(net.src, "lon.bin", 16 * MiB)])
    offer = await A.trec.wait_kind("OfferReceived", recv_key.transfer_id)
    A.transfers.decide(offer.key, True)
    await wait_until(lambda: write_spy.count(recv_key.transfer_id) >= 2, 20, "A chưa nhận khối nào")
    waiting_key = await C.transfers.send(A.id, [make_file(net.src / "c", "c.bin", MiB)])
    await A.trec.wait_kind("OfferReceived", waiting_key.transfer_id)
    send_key = await A.transfers.send(B.id, [make_file(net.src / "a", "a.bin", 8 * MiB)])

    await asyncio.wait_for(A.close(), 15)
    results = {u.info.key.transfer_id: u for u in A.trec.of("TransferFinished")}
    assert results[recv_key.transfer_id].result == T.TransferResult.SUSPENDED
    assert results[send_key.transfer_id].result == T.TransferResult.CANCELLED_USER
    assert results[waiting_key.transfer_id].result in (
        T.TransferResult.REJECTED_USER,
        T.TransferResult.CANCELLED_USER,
    ), describe(results[waiting_key.transfer_id])
    order = [u.info.key.transfer_id for u in A.trec.of("TransferFinished")]
    assert order.index(recv_key.transfer_id) < order.index(send_key.transfer_id)
    assert order.index(recv_key.transfer_id) < order.index(waiting_key.transfer_id)
    assert cancels_or_rejects(wire_spy, recv_key.transfer_id) == []
    sent_to_c = [m for _, m in wire_spy if getattr(m, "transfer_id", None) == waiting_key.transfer_id]
    assert any(isinstance(m, Reject) and m.reason == "user" for m in sent_to_c), sent_to_c
    assert any(
        isinstance(m, Cancel) and m.reason == "user" and m.transfer_id == send_key.transfer_id
        for _, m in wire_spy
    )


# =================================================================================================
# S1: message của lượt tạm dừng tới giữa `begin_close` và lúc đóng kết nối
# =================================================================================================


class RawSender:
    """Bên gửi giả (danh tính của node C) trên kết nối điều khiển `cc` (fixture `control_client`): gửi
    `offer`, chờ `resume`, mở kết nối dữ liệu và ghi khung khối thật (`Connection.write_block`)."""

    def __init__(self, cc, C, A, size: int) -> None:
        self.cc, self.C, self.A = cc, C, A
        self.data = os.urandom(size)
        self.tid = uuid.uuid4().hex
        self.entry = FileEntry("s1.bin", size, 1_600_000_000_000)
        self.offer = Offer(self.tid, BLOCK_SIZE, (self.entry,))
        n = -(-size // BLOCK_SIZE)
        self.blocks = [self.data[i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE] for i in range(n)]
        self.leaves = b"".join(hashlib.sha256(b).digest() for b in self.blocks)
        self.data_conns: list = []

    def received(self, cls) -> list:
        return [
            m for m in self.cc.received if isinstance(m, cls) and getattr(m, "transfer_id", None) == self.tid
        ]

    async def wait(self, cls, timeout: float = 10.0):
        await wait_until(lambda: self.received(cls), timeout, f"C chưa nhận {cls.__name__}")
        return self.received(cls)[-1]

    async def open_data(self, seq: int):
        conn = await connection.open_data(
            LOCAL,
            self.A.port,
            self.C.identity,
            self.C.hello,
            self.A.identity.cert_pem(),
            DataOpen(self.tid, 0, seq),
        )
        self.data_conns.append(conn)
        return conn

    def close(self) -> None:
        for conn in self.data_conns:
            conn.abort()


@integration
@leakcheck
async def test_s1_complete_offer_and_data_open_during_close_do_not_cancel_suspended_transfer(
    T, transfer_nodes, node_factory, control_client, wire_spy, write_spy, monkeypatch, grace_real
):
    """S1 (spec-p3 mục P3-3b; spec-p3-3b mục 5): giữa `begin_close` và lúc đóng kết nối (giữ cửa sổ bằng
    cách chặn `flush_control`, bước 3 của `Node.close`), C gửi `complete`, `offer` gửi lại và mở thêm một
    kết nối dữ liệu cho lượt đang tạm dừng → A không gửi `cancel`/`reject` nào của lượt đó, kết nối dữ liệu
    mới bị đóng, thư mục tạm còn đủ, `TransferFinished(SUSPENDED)` đúng một lần."""
    net = await transfer_nodes(connect=False, extra_trusted=("C",))
    A = net.A
    C = await node_factory("C", auto_connect=False)
    cc = await control_client(C, A)
    await wait_until(lambda: C.id in A.connected_devices(), 5, "A chưa ghi nhận C")
    sender = RawSender(cc, C, A, 20 * MiB)
    try:
        await cc.conn.send(sender.offer)
        resume = await sender.wait(Resume)
        data = await sender.open_data(resume.seq)
        for i in range(18):
            await data.write_block(i, hashlib.sha256(sender.blocks[i]).digest(), sender.blocks[i])
        await wait_until(lambda: write_spy.count(sender.tid, done=True) >= 17, 20, "A chưa ghi đủ khối")

        began, proceed = asyncio.Event(), asyncio.Event()
        real_flush = T.TransferManager.flush_control

        async def flush_control(self, timeout):
            if self is A.transfers:
                began.set()
                await proceed.wait()
            return await real_flush(self, timeout)

        monkeypatch.setattr(T.TransferManager, "flush_control", flush_control)
        closing = asyncio.create_task(A.close())
        try:
            await asyncio.wait_for(began.wait(), 5)
            assert A.trec.finished(sender.tid).result == T.TransferResult.SUSPENDED
            await cc.conn.send(Complete(sender.tid, 0, merkle_root(sender.leaves).hex()))
            await cc.conn.send(sender.offer)
            try:
                late = await sender.open_data(resume.seq)
            except (connection.ConnectionLostError, OSError):
                late = None
            if late is not None:
                try:
                    got = await asyncio.wait_for(late.read_raw(1, at_boundary=True, idle_timeout=5), 6)
                except connection.ConnectionLostError:
                    got = None
                assert got is None, "A gửi dữ liệu trên kết nối dữ liệu của lượt tạm dừng"
            await asyncio.sleep(0.3)  # cho A xử lý các message trên
        finally:
            proceed.set()
        await asyncio.wait_for(closing, 15)
    finally:
        sender.close()

    assert cancels_or_rejects(wire_spy, sender.tid) == []
    assert sender.received(Cancel) == [] and sender.received(Reject) == []
    finished = A.trec.of("TransferFinished", sender.tid)
    assert len(finished) == 1 and finished[0].result == T.TransferResult.SUSPENDED
    left = set(os.listdir(temp_dir(net.receive_dir, sender.tid)))
    assert {"transfer.json", "0.part", "0.state"} <= left, left


# =================================================================================================
# V3: đóng trật tự giữa lượt → mở lại → hoàn tất, trong ngưỡng mvp N4
# =================================================================================================


@integration
@leakcheck
@pytest.mark.parametrize("client", ["sender", "receiver"])
async def test_orderly_close_mid_transfer_then_reopen_finishes_within_n4(
    T, transfer_nodes, restart, write_spy, grace_real, client
):
    net = await transfer_nodes(sender_opts={"rate_limit": 8 * MiB}, client=client)
    A, B = net.A, net.B
    size = 24 * MiB + 3
    src = make_file(net.src, "lon.bin", size)
    key = await B.transfers.send(A.id, [src])
    tid = key.transfer_id
    await wait_until(lambda: write_spy.count(tid, done=True) >= 20, 30, "A chưa ghi đủ 20 khối")
    await asyncio.wait_for(A.close(), 15)
    d = temp_dir(net.receive_dir, tid)
    assert {"transfer.json", "0.part", "0.state"} <= set(os.listdir(d))
    assert A.trec.finished(tid).result == T.TransferResult.SUSPENDED
    assert B.trec.finished(tid) is None

    A2 = await restart("A", receive_dir=net.receive_dir, auto_accept=True)
    [resumed] = A2.trec.of("TransferResumed", tid)
    rkey = core_transfer.TransferKey(B.id, core_transfer.Direction.RECEIVE, tid)
    assert resumed.info.key == rkey
    assert resumed.info.status == core_transfer.TransferStatus.RECONNECTING
    assert [(f.name, f.size) for f in resumed.info.files] == [("lon.bin", size)]
    assert resumed.info.total_bytes == size
    assert A2.transfers.get(rkey) is not None
    if client == "sender":
        B.connect_hint(A2.id, [LOCAL], A2.port)
    else:
        A2.connect_hint(B.id, [LOCAL], B.port)

    fin_b = await B.trec.wait_finished(tid, 60)
    fin_a = await A2.trec.wait_finished(tid, 60)
    assert fin_b.result == T.TransferResult.DONE, describe(fin_b)
    assert fin_a.result == T.TransferResult.DONE, describe(fin_a)
    assert A2.trec.of("OfferReceived", tid) == [], "lượt khôi phục không được hỏi lại"
    assert sha256_of(net.receive_dir / "lon.bin") == sha256_of(src)
    excess = fin_b.payload_sent - size
    assert 0 <= excess <= N4_SLACK, f"gửi thừa {excess / MiB:.1f} MiB (ngưỡng mvp N4 {N4_SLACK // MiB} MiB)"
    assert fin_a.payload_sent == 0


@integration
@leakcheck
async def test_suspend_drops_queued_finalize_of_last_file_then_reopen_finishes(
    T, transfer_nodes, restart, resume_helpers, monkeypatch, grace_real
):
    """F1 (spec-p3-3b mục 8.4, thắng 8.1.10): `Finalize` của file cuối đã giao cho hàng đĩa nhưng **chưa
    bắt đầu** khi node nhận đóng → lượt `SUSPENDED` một lần và trên đĩa lượt **chưa** xong: `.part`,
    `.state` còn, `transfer.json` chưa có `done_at`, file chưa mang tên thật. Mở lại → `TransferResumed`
    một lần; máy gửi nối lại (gửi lại `complete`) → đọc lại, đổi tên → hai bên DONE, file khớp nguồn.

    Cách giữ `Finalize` ở trạng thái “chưa bắt đầu”: `DiskExecutor` của node một thread (thay
    `teleport.app.node.DiskExecutor`); lúc đọc lại, hẹn giờ dọn tạm (rút ngắn `CLEANUP_INTERVAL`) xếp một
    việc `cleanup` vào sau, việc đó chặn thread ở cổng của test, nên `Finalize` giao sau nó phải chờ. Đóng
    A rồi mới mở cổng (trong `CLOSE_DISK_WAIT` đã nới rộng, để `Finalize` còn kịp chạy nếu không bị bỏ).
    """
    from teleport.app import diskio
    from teleport.app import node as node_mod
    from teleport.core import resume_state as rs

    real_executor = diskio.DiskExecutor
    monkeypatch.setattr(node_mod, "DiskExecutor", lambda *a, **kw: real_executor(1, **kw))
    monkeypatch.setattr(T, "CLEANUP_INTERVAL", 0.05)
    monkeypatch.setattr(T, "CLOSE_DISK_WAIT", 10.0)
    armed, cleanup_hit, gate = threading.Event(), threading.Event(), threading.Event()
    finalize_calls: list[str] = []
    real_reread, real_cleanup, real_finalize = (
        storage.PartFile.reread,
        storage.ReceiveDir.cleanup,
        storage.ReceiveDir.finalize,
    )

    def reread(self, *, stop=None):
        result = real_reread(self, stop=stop)
        if not armed.is_set():
            armed.set()
            time.sleep(0.5)  # nhiều chu kỳ CLEANUP_INTERVAL: một việc `cleanup` xếp hàng sau việc này
        return result

    def cleanup(self, wall_now, *, own_device_id, skip, retention):
        if armed.is_set() and not cleanup_hit.is_set():
            cleanup_hit.set()
            gate.wait(30)
        return real_cleanup(self, wall_now, own_device_id=own_device_id, skip=skip, retention=retention)

    def finalize(self, part, name, **kwargs):
        finalize_calls.append(part.path.parent.name)
        return real_finalize(self, part, name, **kwargs)

    monkeypatch.setattr(storage.PartFile, "reread", reread)
    monkeypatch.setattr(storage.ReceiveDir, "cleanup", cleanup)
    monkeypatch.setattr(storage.ReceiveDir, "finalize", finalize)

    net = await transfer_nodes()
    A, B = net.A, net.B
    src = make_file(net.src, "f.bin", 3 * MiB + 5)
    key = await B.transfers.send(A.id, [src])
    tid = key.transfer_id
    try:
        await wait_until(cleanup_hit.is_set, 30, "việc cleanup chưa chặn thread đĩa sau lượt đọc lại")
        await asyncio.sleep(0.3)  # `reread_done` → `Finalize` đã được giao, đang chờ thread
        assert finalize_calls == [], "Finalize không được chạy trước khi thread rảnh"
        closing = asyncio.create_task(A.close())
        await A.trec.wait_kind("TransferFinished", tid, timeout=5)
        gate.set()
        await asyncio.wait_for(closing, 30)
    finally:
        gate.set()

    finished = A.trec.of("TransferFinished", tid)
    assert len(finished) == 1 and finished[0].result == T.TransferResult.SUSPENDED, [
        describe(f) for f in finished
    ]
    assert finalize_calls == [], "Finalize chưa bắt đầu phải bị bỏ khi tạm dừng"
    d = temp_dir(net.receive_dir, tid)
    assert {"transfer.json", "0.part", "0.state"} <= set(os.listdir(d))
    rec = rs.decode_transfer_record((d / "transfer.json").read_bytes(), dir_name=tid)
    assert rec.done_at is None and rec.verified == 0
    assert not (net.receive_dir / "f.bin").exists()
    assert B.trec.finished(tid) is None

    A2 = await restart("A", receive_dir=net.receive_dir, auto_accept=True)
    assert len(A2.trec.of("TransferResumed", tid)) == 1
    B.connect_hint(A2.id, [LOCAL], A2.port)
    fin_b = await B.trec.wait_finished(tid, 60)
    fin_a = await A2.trec.wait_finished(tid, 60)
    assert fin_b.result == T.TransferResult.DONE, describe(fin_b)
    assert fin_a.result == T.TransferResult.DONE, describe(fin_a)
    assert sha256_of(net.receive_dir / "f.bin") == sha256_of(src)
