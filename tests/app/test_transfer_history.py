"""Lịch sử truyền qua hai node thật (P4-2, việc 7: T19–T30; spec `specs/spec-p4-2.md` mục 2, 3.3, 5, 7, 11).

Viết từ đặc tả, không từ code. Quy ước như `test_transfers.py` (spec mục 11.1): fixture `transfer_nodes`,
**A nhận** (`receive_dir`, `auto_accept=True`), **B gửi**; `trust_each_other` đặt tên B trong kho của A
là "B" và tên A trong kho của B là "A". Mở lại một node = `node_factory` cùng nhãn (cùng thư mục cấu
hình). Helper `restart`, `grace_real`, `close_cleanly` chép từ `test_transfer_suspend.py` (spec 11.4: không
sửa `conftest.py`).

Nội dung `history.json` được đọc độc lập bằng `json` (spec mục 4). Seam: `teleport.app.history`
(`load`, `save`, `HISTORY_MIN_INTERVAL`) và `teleport.app.transfers._wall_time`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import gc
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

import pytest

from teleport.core import transfer as core_transfer

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
leakcheck = pytest.mark.usefixtures("no_leaks")

LOCAL = "127.0.0.1"
MiB = 1024 * 1024
UTC = datetime.UTC
REAL_GRACE = core_transfer.RECONNECT_GRACE  # đọc lúc import, trước khi `fast` vá
EMPTY = {"version": 1, "entries": []}


# --- Hàm phụ trợ ---------------------------------------------------------------------------------


async def wait_until(predicate, timeout: float = 5.0, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.01)


def make_file(directory: Path, name: str, size: int, content: bytes | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if content is not None:
        path.write_bytes(content)
        return path
    with open(path, "wb") as f:
        left = size
        while left:
            chunk = os.urandom(min(left, 4 * MiB))
            f.write(chunk)
            left -= len(chunk)
    return path


# Chép từ `test_transfer_suspend.py` (spec 11.4).
CLOSE_WARNINGS = ("việc dọn đĩa chưa xong", "không đóng thư mục nhận")


async def close_cleanly(node, caplog, timeout: float = 45.0) -> None:
    """Đóng `node` (test đã nới `transfers.CLOSE_DISK_WAIT`) rồi khẳng định hàng đĩa xong trước hạn."""
    caplog.set_level(logging.WARNING, logger="teleport.app.transfers")
    start = len(caplog.records)
    await asyncio.wait_for(node.close(), timeout)
    bad = [
        r.getMessage()
        for r in caplog.records[start:]
        if r.name == "teleport.app.transfers" and any(w in r.getMessage() for w in CLOSE_WARNINGS)
    ]
    assert not bad, f"đóng node mà hàng đĩa chưa xong: {bad}"


def entries_of(node, tid: str) -> list:
    return [e for e in node.transfers.history() if e.transfer_id == tid]


def read_replaced(path: Path, budget: float = 1.0) -> bytes:
    """Đọc `path` mà node có thể đang thay nguyên tử (`os.replace` trên thread đĩa). Trên Windows, mở file
    đúng lúc nó đang được thay báo `PermissionError`: thử lại mỗi 20 ms trong `budget` giây, hết hạn thì
    ném lại lỗi thật (không nuốt vĩnh viễn)."""
    deadline = time.monotonic() + budget
    while True:
        try:
            return path.read_bytes()
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def file_obj(config: Path) -> dict | None:
    try:
        return json.loads(read_replaced(config / "history.json"))
    except FileNotFoundError:
        return None


def file_ids(config: Path) -> list[str]:
    obj = file_obj(config)
    return [] if obj is None else [e["transfer_id"] for e in obj["entries"]]


def errors_logged(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def old_entry_json(k: int, device_id: str) -> dict:
    return {
        "direction": "receive",
        "device_id": device_id,
        "device_name": "May-Cu",
        "transfer_id": f"{k + 1:032x}",
        "file_count": 1,
        "total_bytes": 10,
        "done_bytes": 10,
        "names": [f"cu-{k}.bin"],
        "result": "done",
        "finished_at": "2026-01-01T00:00:00Z",
        "saved_path": None,
    }


class WriteGate:
    """Chặn bên nhận **giữa lượt** một cách tất định (không dựa vào nhịp `TransferProgress`): lần ghi khối
    thứ `at` của một lượt (`PartFile.write_at`, chạy trong thread đĩa) chờ `gate` trước khi ghi. Lượt
    không thể xong khi cổng còn đóng, nên test làm việc “giữa chừng” (hủy, hủy ghép đôi, đóng) rồi mới
    `release()`. Chỉ chặn một lần."""

    def __init__(self, at: int) -> None:
        self.at = at
        self.gate = threading.Event()
        self.hit = threading.Event()
        self.lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.tid: str | None = None

    def before_write(self, part) -> None:
        tid = part.path.parent.name
        with self.lock:
            self.counts[tid] = self.counts.get(tid, 0) + 1
            block = self.counts[tid] == self.at and not self.hit.is_set()
            if block:
                self.tid = tid
                self.hit.set()
        if block and not self.gate.wait(60):
            raise AssertionError("cổng chặn ghi của test không được mở")

    async def wait_hit(self, tid: str, timeout: float = 30.0) -> None:
        await wait_until(self.hit.is_set, timeout, f"bên nhận chưa ghi tới khối thứ {self.at}")
        assert self.tid == tid, f"chặn nhầm lượt {self.tid} (mong {tid})"

    def release(self) -> None:
        self.gate.set()


@pytest.fixture
def write_gate(monkeypatch):
    """`write_gate(at=3)` → `WriteGate` đã cài trên `storage.PartFile.write_at`; cổng luôn được mở khi
    test xong."""
    from teleport.app import storage

    gates: list[WriteGate] = []
    real = storage.PartFile.write_at

    def install(at: int = 3) -> WriteGate:
        g = WriteGate(at)
        gates.append(g)

        def write_at(self, index, data):
            g.before_write(self)
            return real(self, index, data)

        monkeypatch.setattr(storage.PartFile, "write_at", write_at)
        return g

    yield install
    for g in gates:
        g.release()


@pytest.fixture
def T():
    from teleport.app import transfers

    return transfers


@pytest.fixture
def HM():
    from teleport.app import history

    return history


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


# =================================================================================================
# T19: một lượt DONE, cả hai bên
# =================================================================================================


@leakcheck
async def test_done_transfer_leaves_one_entry_on_each_side(T, transfer_nodes, monkeypatch):
    """T19 (vai theo spec 11.1). Đột biến “`_record_history` sau `_forget_transfer`” làm `saved_path` của A
    thành None → đỏ. Kèm 11.14: trong callback `TransferFinished`, `history()[0]` đã là lượt đó."""
    wall = 1_800_000_000.75
    monkeypatch.setattr(T, "_wall_time", lambda: wall)
    net = await transfer_nodes()
    A, B = net.A, net.B
    seen_first: dict[str, list] = {"A": [], "B": []}

    def hook_for(label, node):
        def hook(update):
            if type(update).__name__ == "TransferFinished":
                top = node.transfers.history()
                seen_first[label].append((update.info.key.transfer_id, top[0].transfer_id if top else None))

        return hook

    A.trec.hooks.append(hook_for("A", A))
    B.trec.hooks.append(hook_for("B", B))
    sizes = (MiB + 5, 777)
    files = [make_file(net.src, "phim.mp4", sizes[0]), make_file(net.src, "ghi-chu.txt", sizes[1])]
    key = await B.transfers.send(A.id, files)
    tid = key.transfer_id
    fin_b = await B.trec.wait_finished(tid)
    fin_a = await A.trec.wait_finished(tid)
    assert fin_a.result == T.TransferResult.DONE and fin_b.result == T.TransferResult.DONE
    assert seen_first["A"] == [(tid, tid)], "history()[0] phải là lượt vừa xong ngay trong callback"
    assert seen_first["B"] == [(tid, tid)]

    expected_time = datetime.datetime.fromtimestamp(1_800_000_000, UTC)
    [ea] = entries_of(A, tid)
    [eb] = entries_of(B, tid)
    assert A.transfers.history()[0] == ea and B.transfers.history()[0] == eb
    for e, direction, peer, peer_name in (
        (ea, core_transfer.Direction.RECEIVE, B.id, "B"),
        (eb, core_transfer.Direction.SEND, A.id, "A"),
    ):
        assert e.direction == direction
        assert e.device_id == peer
        assert e.device_name == peer_name
        assert e.file_count == 2
        assert e.total_bytes == sum(sizes)
        assert e.done_bytes == e.total_bytes
        assert e.names == ("phim.mp4", "ghi-chu.txt")
        assert e.result == "done"
        assert e.finished_at == expected_time
    [done0] = [u for u in A.trec.of("FileDone", tid) if u.file_index == 0]
    assert done0.path is not None
    assert ea.saved_path == str(done0.path)
    assert Path(ea.saved_path).is_file()
    assert eb.saved_path is None


@leakcheck
async def test_history_is_recorded_without_on_transfer_event(transfer_nodes):
    """Spec H4: ghi lịch sử không đi qua `_emit`, nên node không có `on_transfer_event` vẫn có mục."""
    net = await transfer_nodes(receiver_opts={"on_transfer_event": None})
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "a.bin", 1000)])
    tid = key.transfer_id
    await B.trec.wait_finished(tid)
    await wait_until(lambda: entries_of(A, tid), 10, "A chưa có mục lịch sử")
    [ea] = entries_of(A, tid)
    assert ea.result == "done" and ea.saved_path is not None


# =================================================================================================
# T20–T22: nạp, cắt 100, hỏng, xóa
# =================================================================================================


@leakcheck
async def test_preloaded_100_entries_of_other_device_plus_one_transfer_keeps_101_across_restart(
    transfer_nodes, restart, tmp_path, caplog
):
    """T20. Trần theo máy (mvp F5): 100 mục của máy khác không bị lượt mới với B đẩy ra."""
    old_dev = "c" * 32
    config_a = tmp_path / "A"
    config_a.mkdir(parents=True, exist_ok=True)
    old = [old_entry_json(k, old_dev) for k in range(100)]  # k = 0 mới nhất
    (config_a / "history.json").write_text(json.dumps({"version": 1, "entries": old}), encoding="utf-8")
    net = await transfer_nodes()
    A, B = net.A, net.B
    assert len(A.transfers.history()) == 100
    key = await B.transfers.send(A.id, [make_file(net.src, "moi.bin", 2000)])
    tid = key.transfer_id
    await A.trec.wait_finished(tid)
    hist = A.transfers.history()
    assert len(hist) == 101
    assert hist[0].transfer_id == tid
    assert [e.transfer_id for e in hist[1:]] == [d["transfer_id"] for d in old], "máy khác không mất mục"
    assert len(A.transfers.history(old_dev)) == 100
    assert [e.transfer_id for e in A.transfers.history(B.id)] == [tid]

    await close_cleanly(A, caplog)
    assert file_ids(config_a) == [e.transfer_id for e in hist]
    A2 = await restart("A", receive_dir=net.receive_dir, auto_accept=True)
    assert A2.transfers.history() == hist


@leakcheck
async def test_corrupt_history_file_does_not_block_start_and_is_overwritten(transfer_nodes, tmp_path, caplog):
    """T21."""
    config_a = tmp_path / "A"
    config_a.mkdir(parents=True, exist_ok=True)
    (config_a / "history.json").write_bytes(b'{"version": 1, "entries": [')
    caplog.set_level(logging.WARNING)
    net = await transfer_nodes()
    A, B = net.A, net.B
    assert A.port is not None
    assert A.transfers.history() == ()
    assert any(r.name.startswith("teleport") and r.levelno >= logging.WARNING for r in caplog.records)
    key = await B.transfers.send(A.id, [make_file(net.src, "a.bin", 3000)])
    tid = key.transfer_id
    fin = await A.trec.wait_finished(tid)
    assert str(fin.result) == "done"
    await close_cleanly(A, caplog)
    obj = file_obj(config_a)
    assert obj is not None and obj["version"] == 1
    assert [e["transfer_id"] for e in obj["entries"]] == [tid]


@leakcheck
async def test_clear_history_empties_now_and_after_restart(
    transfer_nodes, restart, tmp_path, HM, monkeypatch, caplog
):
    """T22. Đột biến “`clear` chỉ xóa trong bộ nhớ” và “bỏ xả trong `close`” làm test này đỏ: lần ghi
    sau `clear_history()` phải chờ `HISTORY_MIN_INTERVAL` (vá 30 s), nên chỉ lần xả lúc đóng ghi nó."""
    monkeypatch.setattr(HM, "HISTORY_MIN_INTERVAL", 30.0)
    net = await transfer_nodes()
    A, B = net.A, net.B
    config_a = tmp_path / "A"
    key = await B.transfers.send(A.id, [make_file(net.src, "a.bin", 3000)])
    tid = key.transfer_id
    await A.trec.wait_finished(tid)
    await wait_until(lambda: file_ids(config_a) == [tid], 10, "history.json của A chưa có lượt")
    A.transfers.clear_history()
    assert A.transfers.history() == ()
    await close_cleanly(A, caplog)
    assert (config_a / "history.json").exists(), "clear_history không xóa file"
    assert file_obj(config_a) == EMPTY
    A2 = await restart("A", receive_dir=net.receive_dir, auto_accept=True)
    assert A2.transfers.history() == ()
    assert file_obj(config_a) == EMPTY


@leakcheck
async def test_clear_history_after_close_only_clears_memory_with_warning(transfer_nodes, tmp_path, caplog):
    """Spec 3.3: sau `close`, `clear_history()` chỉ xóa trong bộ nhớ (WARNING), file giữ nguyên."""
    net = await transfer_nodes()
    A, B = net.A, net.B
    config_a = tmp_path / "A"
    key = await B.transfers.send(A.id, [make_file(net.src, "a.bin", 3000)])
    tid = key.transfer_id
    await A.trec.wait_finished(tid)
    await close_cleanly(A, caplog)
    before = (config_a / "history.json").read_bytes()
    caplog.set_level(logging.WARNING)
    start = len(caplog.records)
    A.transfers.clear_history()
    assert A.transfers.history() == ()
    await asyncio.sleep(0.3)
    assert (config_a / "history.json").read_bytes() == before
    assert any(r.levelno >= logging.WARNING for r in caplog.records[start:])


# =================================================================================================
# T23: N9
# =================================================================================================


@leakcheck
async def test_history_holds_only_metadata_and_logs_hold_no_file_name(transfer_nodes, tmp_path, caplog):
    """T23 (mvp N9, spec 11.15): dải byte đánh dấu của nội dung không có trong `history.json` (thô lẫn
    hex); tên file có; log (DEBUG, logger `teleport`) không có tên file."""
    caplog.set_level(logging.DEBUG, logger="teleport")
    marker = secrets.token_bytes(32)
    content = os.urandom(5000) + marker + os.urandom(5000)
    name = f"bao-cao-{secrets.token_hex(6)}.bin"
    net = await transfer_nodes()
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, name, len(content), content)])
    tid = key.transfer_id
    await B.trec.wait_finished(tid)
    await A.trec.wait_finished(tid)
    await asyncio.wait_for(A.close(), 30)
    await asyncio.wait_for(B.close(), 30)
    for label in ("A", "B"):
        data = (tmp_path / label / "history.json").read_bytes()
        assert marker not in data
        assert marker.hex().encode() not in data
        assert marker.hex().upper().encode() not in data
        assert name.encode() in data, f"history.json của {label} phải có tên file"
    leaked = [
        r.getMessage() for r in caplog.records if r.name.startswith("teleport") and name in r.getMessage()
    ]
    assert leaked == [], f"log chứa tên file: {leaked}"


# =================================================================================================
# T24–T27: từ chối, hủy, tạm dừng, hủy ghép đôi
# =================================================================================================


@leakcheck
async def test_rejected_by_user_has_an_entry_on_both_sides(transfer_nodes):
    """T24 (spec 11.1): A không tự nhận rồi từ chối."""
    net = await transfer_nodes(receiver_opts={"auto_accept": False})
    A, B = net.A, net.B
    files = [make_file(net.src, f"f{k}.bin", 100 + k) for k in range(3)]
    key = await B.transfers.send(A.id, files)
    tid = key.transfer_id
    offer = await A.trec.wait_kind("OfferReceived", tid)
    A.transfers.decide(offer.key, False)
    await B.trec.wait_finished(tid)
    await A.trec.wait_finished(tid)
    [ea] = entries_of(A, tid)
    [eb] = entries_of(B, tid)
    for e in (ea, eb):
        assert e.result == "rejected_user"
        assert e.saved_path is None
        assert e.file_count == 3
        assert e.total_bytes == 100 + 101 + 102
        assert e.done_bytes == 0
    assert eb.names == ("f0.bin", "f1.bin", "f2.bin")


@pytest.mark.slow
@leakcheck
async def test_cancelled_mid_transfer_records_partial_bytes(transfer_nodes, write_gate):
    """T25: B hủy giữa chừng → cả hai `cancelled_user`, `0 < done_bytes < total_bytes`. Giữa chừng =
    A đang bị chặn ở lần ghi khối thứ 3 (`write_gate`)."""
    gate = write_gate(at=3)
    net = await transfer_nodes(sender_opts={"rate_limit": 4 * MiB})
    A, B = net.A, net.B
    size = 32 * MiB
    key = await B.transfers.send(A.id, [make_file(net.src, "lon.bin", size)])
    tid = key.transfer_id
    await gate.wait_hit(tid)
    B.transfers.cancel(key)
    gate.release()
    fin_b = await B.trec.wait_finished(tid)
    fin_a = await A.trec.wait_finished(tid)
    for node, fin in ((A, fin_a), (B, fin_b)):
        [e] = entries_of(node, tid)
        assert e.result == "cancelled_user", node.label
        assert str(fin.result) == "cancelled_user"
        assert e.total_bytes == size
        assert e.done_bytes < size, (node.label, e.done_bytes)
        assert e.done_bytes == fin.info.done_bytes
        assert e.saved_path is None
    # Bên nhận chỉ cộng byte theo lô đã ghi (có thể vẫn 0 lúc hủy); bên gửi đếm byte đã gửi nên chắc > 0.
    [eb] = entries_of(B, tid)
    assert eb.done_bytes > 0


@pytest.mark.slow
@leakcheck
async def test_suspended_transfer_has_no_entry_and_resumed_one_has_exactly_one(
    T, transfer_nodes, restart, grace_real, write_gate, monkeypatch, caplog, tmp_path
):
    """T26 (spec 11.4): A đóng giữa lượt (`SUSPENDED`) → A không có mục; mở lại, nối lại tới DONE → A đúng
    một mục `done`, B đúng một mục `done`. Đột biến “ghi cả SUSPENDED” làm test này đỏ.

    Giữa lượt một cách tất định: A bị chặn ở lần ghi khối thứ 4 (`write_gate`); bắt đầu đóng A khi cổng
    còn chặn, chờ A báo `SUSPENDED` (tạm dừng xảy ra trước khi hàng đĩa xả) rồi mới mở cổng, để hàng đĩa
    chạy nốt trong `CLOSE_DISK_WAIT` (nới 30 s)."""
    gate = write_gate(at=4)
    net = await transfer_nodes(sender_opts={"rate_limit": 4 * MiB})
    A, B = net.A, net.B
    size = 24 * MiB + 3
    key = await B.transfers.send(A.id, [make_file(net.src, "lon.bin", size)])
    tid = key.transfer_id
    await gate.wait_hit(tid)
    monkeypatch.setattr(T, "CLOSE_DISK_WAIT", 30.0)
    closing = asyncio.create_task(close_cleanly(A, caplog))
    try:
        # Chỉ để mở cổng muộn nhất có thể; nếu `SUSPENDED` chỉ tới sau khi hàng đĩa xả thì vẫn mở cổng
        # sau 10 s, khẳng định ở dưới quyết định.
        with contextlib.suppress(AssertionError):
            await wait_until(lambda: A.trec.finished(tid) is not None, 10)
    finally:
        gate.release()
    await closing
    assert A.trec.finished(tid).result == T.TransferResult.SUSPENDED
    assert entries_of(A, tid) == [], "lượt tạm dừng không được có mục"
    assert tid not in file_ids(tmp_path / "A")

    A2 = await restart("A", receive_dir=net.receive_dir, auto_accept=True)
    assert entries_of(A2, tid) == []
    B.transfers.rate_limit = None  # phần còn lại đi nhanh: máy bận không làm test chậm
    B.connect_hint(A2.id, [LOCAL], A2.port)
    fin_b = await B.trec.wait_finished(tid, 90)
    fin_a = await A2.trec.wait_finished(tid, 90)
    assert fin_a.result == T.TransferResult.DONE and fin_b.result == T.TransferResult.DONE
    [ea] = entries_of(A2, tid)
    [eb] = entries_of(B, tid)
    assert ea.result == "done" and eb.result == "done"
    assert ea.done_bytes == ea.total_bytes == size
    assert len(A2.transfers.history()) == 1
    await close_cleanly(A2, caplog)
    assert file_ids(tmp_path / "A") == [tid]


@pytest.mark.slow
@leakcheck
async def test_unpair_mid_transfer_records_unpaired_without_device_name(transfer_nodes, write_gate):
    """T27 (spec 11.5): chỉ khẳng định phía A. Giữa lượt = A bị chặn ở lần ghi khối thứ 3."""
    gate = write_gate(at=3)
    net = await transfer_nodes(sender_opts={"rate_limit": 4 * MiB})
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "lon.bin", 32 * MiB)])
    tid = key.transfer_id
    await gate.wait_hit(tid)
    try:
        await A.unpair(B.id)
    finally:
        gate.release()
    fin = await A.trec.wait_finished(tid)
    assert str(fin.result) == "unpaired"
    [ea] = entries_of(A, tid)
    assert ea.result == "unpaired"
    assert ea.device_name is None
    assert ea.device_id == B.id
    assert ea.saved_path is None


# =================================================================================================
# T28–T30: khởi động lại, hủy start/close, API
# =================================================================================================


@pytest.mark.slow
@leakcheck
async def test_restore_of_done_and_tombstoned_transfers_adds_no_entries(
    transfer_nodes, restart, write_gate, caplog, T, monkeypatch
):
    """T28 (spec 11.6): một lượt DONE (A còn `transfer.json` → `RestoredDone`) và một lượt bị hủy (bia mộ);
    khởi động lại cả A và B → số mục không đổi, không có `transfer_id` trùng."""
    monkeypatch.setattr(T, "CLOSE_DISK_WAIT", 30.0)
    gate = write_gate(at=3)  # lượt nhỏ (1 khối) không chạm tới; lượt lớn bị chặn ở khối thứ 3
    net = await transfer_nodes(sender_opts={"rate_limit": 4 * MiB})
    A, B = net.A, net.B
    k1 = await B.transfers.send(A.id, [make_file(net.src, "nho.bin", 4000)])
    await A.trec.wait_finished(k1.transfer_id)
    await B.trec.wait_finished(k1.transfer_id)
    k2 = await B.transfers.send(A.id, [make_file(net.src, "lon.bin", 32 * MiB)])
    await gate.wait_hit(k2.transfer_id)
    B.transfers.cancel(k2)
    gate.release()
    await A.trec.wait_finished(k2.transfer_id)
    await B.trec.wait_finished(k2.transfer_id)
    hist_a, hist_b = A.transfers.history(), B.transfers.history()
    assert [e.transfer_id for e in hist_a] == [k2.transfer_id, k1.transfer_id]
    assert [e.transfer_id for e in hist_b] == [k2.transfer_id, k1.transfer_id]
    await close_cleanly(A, caplog)
    await close_cleanly(B, caplog)

    A2 = await restart("A", receive_dir=net.receive_dir, auto_accept=True)
    B2 = await restart("B")
    await asyncio.sleep(0.5)
    for before, node in ((hist_a, A2), (hist_b, B2)):
        after = node.transfers.history()
        assert after == before, node.label
        ids = [e.transfer_id for e in after]
        assert len(ids) == len(set(ids))


@leakcheck
async def test_history_flush_error_does_not_block_close(transfer_nodes, HM, monkeypatch, caplog):
    """T16 mở rộng (spec 11.7): `history.save` ném OSError → `Node.close` vẫn xong trong
    `close_time_limit()`, chỉ WARNING."""
    from teleport.app.node import close_time_limit

    caplog.set_level(logging.WARNING)

    def failing_save(config_dir, entries):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(HM, "save", failing_save)
    net = await transfer_nodes()
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "a.bin", 3000)])
    await A.trec.wait_finished(key.transfer_id)
    await B.trec.wait_finished(key.transfer_id)
    limit = close_time_limit()
    for node in (A, B):
        t0 = time.monotonic()
        await asyncio.wait_for(node.close(), limit + 5)
        elapsed = time.monotonic() - t0
        assert elapsed <= limit, f"close() mất {elapsed:.2f} s > {limit:.2f} s"
    gc.collect()
    await asyncio.sleep(0)
    assert errors_logged(caplog) == [], [r.getMessage() for r in errors_logged(caplog)]


@leakcheck
async def test_start_cancelled_while_loading_history_leaves_no_task(node_factory, HM, monkeypatch, caplog):
    """T29 (phần `Node.start`) và T30 (`history()` gọi được khi `restore` chưa xong, trả `tuple`)."""
    caplog.set_level(logging.WARNING)
    gate, hit = threading.Event(), threading.Event()
    real_load = HM.load

    def slow_load(config_dir):
        hit.set()
        gate.wait(30)
        return real_load(config_dir)

    monkeypatch.setattr(HM, "load", slow_load)
    A = await node_factory("A", start=False)
    starting = asyncio.create_task(A.node.start())
    try:
        await wait_until(hit.is_set, 10, "Node.start chưa tới bước nạp lịch sử")
        got = A.node.transfers.history()
        assert isinstance(got, tuple) and got == ()
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
    finally:
        gate.set()
    await asyncio.sleep(0.3)
    gc.collect()
    await asyncio.sleep(0)
    assert errors_logged(caplog) == [], [r.getMessage() for r in errors_logged(caplog)]


@pytest.mark.slow
@leakcheck
async def test_close_cancelled_aborts_pending_history_write(
    transfer_nodes, T, HM, monkeypatch, caplog, tmp_path
):
    """T29 (phần `Node.close` bị hủy → `abort_now` → `HistoryStore.stop()`): lần ghi đang hẹn
    (`HISTORY_MIN_INTERVAL` vá 60 s) bị hủy, không ghi thêm, không còn task, không ERROR."""
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(HM, "HISTORY_MIN_INTERVAL", 60.0)
    config_b = tmp_path / "B"
    calls: list[Path] = []
    real_save = HM.save

    def counting_save(config_dir, entries):
        calls.append(Path(config_dir))
        return real_save(config_dir, entries)

    monkeypatch.setattr(HM, "save", counting_save)
    net = await transfer_nodes()
    A, B = net.A, net.B
    k1 = await B.transfers.send(A.id, [make_file(net.src, "a.bin", 1000)])
    await B.trec.wait_finished(k1.transfer_id)
    await wait_until(lambda: file_ids(config_b) == [k1.transfer_id], 10, "B chưa ghi lượt đầu")
    k2 = await B.transfers.send(A.id, [make_file(net.src, "b.bin", 1000)])
    await B.trec.wait_finished(k2.transfer_id)
    await asyncio.sleep(0.2)
    b_calls = [c for c in calls if c == config_b]
    assert len(b_calls) == 1, "lần ghi thứ hai phải đang chờ HISTORY_MIN_INTERVAL"

    held = asyncio.Event()
    real_close = T.TransferManager.close

    async def close(self):
        if self is B.transfers and not held.is_set():
            held.set()
            await asyncio.Event().wait()  # giữ tới khi bị hủy
        return await real_close(self)

    monkeypatch.setattr(T.TransferManager, "close", close)
    closing = asyncio.create_task(B.close())
    await asyncio.wait_for(held.wait(), 10)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await asyncio.sleep(0.5)
    assert [c for c in calls if c == config_b] == b_calls, "abort_now không được ghi lịch sử"
    assert file_ids(config_b) == [k1.transfer_id]
    gc.collect()
    await asyncio.sleep(0)
    assert errors_logged(caplog) == [], [r.getMessage() for r in errors_logged(caplog)]


@leakcheck
async def test_history_api_returns_tuple(transfer_nodes):
    """T30."""
    net = await transfer_nodes()
    got = net.A.transfers.history()
    assert isinstance(got, tuple) and got == ()
    assert isinstance(net.B.transfers.history(), tuple)
