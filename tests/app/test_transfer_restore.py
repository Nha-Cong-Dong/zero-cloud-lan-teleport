"""Khôi phục khi `Node.start` (P3-3b, việc V2 (c) và V3 trong tiến trình).

Viết từ đặc tả, không từ code: `specs/spec-p3-3b.md` mục 2.2 (`TransferManager.restore`: bia mộ, quét,
xếp loại, mở lại song song có hạn, một khối đồng bộ, `TransferResumed`), 2.6 (`Node.start`: khôi phục
trước khi mở cổng; hẹn giờ dọn tạm với `skip` chụp đồng bộ), 3 (Q4, Q6), 5 (V2 (c), V3), 8.1 mục 6
(hạn tổng của bước mở lại), 8 (lỗi `engine.restore` → gọi lại một lần với `receives=()`); `specs/spec-p3.md`
mục P3-3b (“Xong khi”); protocol §6.2, §6.3; architecture mục 4 (“Khôi phục”, “Dọn tạm”).

Hai cách dựng trạng thái: (1) chạy lượt thật B → A rồi đóng A có trật tự (tạm dừng), mở lại A bằng
`node_factory` cùng nhãn; (2) dựng thẳng thư mục nhận bằng API `teleport.app.storage` (lượt nhận dở của
máy gửi giả C, D…; mtime đặt bằng `os.utime`). Máy gửi giả nói chuyện với A qua `control_client` (kết nối
điều khiển thật, danh tính của node C). Đổi đích của proxy (`TcpProxy.target`, đọc lúc nối) khi A mở lại
trên cổng mới. Hạn chờ nối lại đặt lại giá trị thật (fixture `fast` đặt 4 s).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from teleport.app import storage
from teleport.core import resume_state as rs
from teleport.core import transfer as core_transfer
from teleport.core.bitfield import Bitfield
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.merkle import merkle_root
from teleport.core.messages import Accept, Cancel, Complete, Resume, Verified
from teleport.trust.store import TrustStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
leakcheck = pytest.mark.usefixtures("no_leaks")

LOCAL = "127.0.0.1"
MiB = 1024 * 1024
REAL_GRACE = core_transfer.RECONNECT_GRACE  # đọc lúc import, trước khi `fast` vá
SIZE = 5 * MiB + 3  # 6 khối, khối cuối ngắn
RECEIVE = core_transfer.Direction.RECEIVE


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


def leaves_of(data: bytes) -> bytes:
    n = max(1, -(-len(data) // BLOCK_SIZE))
    return b"".join(hashlib.sha256(data[i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE]).digest() for i in range(n))


def temp_dir(receive_dir: Path, tid: str) -> Path:
    return receive_dir / ".teleport" / tid


def content_of(d: Path) -> dict[str, tuple[int, str]]:
    """Tên → (cỡ, SHA-256) của các file trong thư mục (không tính mtime)."""
    return {
        p.name: (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(d.iterdir())
    }


def describe(fin) -> str:
    if fin is None:
        return "chưa có TransferFinished"
    return f"result={fin.result} by_peer={fin.by_peer} reason={fin.reason} file_index={fin.file_index}"


def messages(cc, cls, tid: str | None = None) -> list:
    return [m for m in cc.received if isinstance(m, cls) and (tid is None or m.transfer_id == tid)]


def build_running(
    storage_mod, root: Path, record_for, h, tid: str, *, sender: str, receiver: str, blocks, age: float
):
    """Lượt nhận dở trên đĩa: `transfer.json` (máy nhận `receiver`, máy gửi `sender`, một file `SIZE`),
    `0.part` với các khối `blocks` đã ghi và `MARK` bền; mọi file và thư mục có mtime = bây giờ − `age`."""
    data = random.Random(tid).randbytes(SIZE)
    with storage_mod.ReceiveDir.open(root) as rd:
        temp = rd.create_temp(tid, record_for(tid, [SIZE], receiver=receiver, sender=sender))
        part = temp.create_part(0, SIZE)
        if blocks:
            part.commit(h.write_blocks(part, data, blocks))
        rd.close_temp(temp)
    d = temp_dir(root, tid)
    t = time.time() - age
    for p in d.iterdir():
        os.utime(p, (t, t))
    os.utime(d, (t, t))
    return d


@pytest.fixture
def T():
    from teleport.app import transfers

    return transfers


@pytest.fixture
def grace_real(monkeypatch):
    monkeypatch.setattr(core_transfer, "RECONNECT_GRACE", REAL_GRACE)


@pytest.fixture
def restart(node_factory, app_helpers):
    """`await restart(label, **node_kwargs)` → `AppNode` (mới) cùng thư mục cấu hình, có `trec` mới."""

    async def make(label: str, **node_kwargs):
        trec = app_helpers.TransferRecorder(f"{label}'")
        node_kwargs.setdefault("on_transfer_event", trec.on_update)
        node = await node_factory(label, node_kwargs=node_kwargs)
        node.trec = trec
        return node

    return make


@pytest.fixture
def world(trust_each_other, tmp_path, app_helpers):
    """`world(*senders)` → (device_id của A, {nhãn: device_id}); A và mỗi máy gửi tin nhau."""

    def make(*senders: str):
        ids = {}
        for label in senders:
            _, ident = trust_each_other("A", label, name_b=label)
            ids[label] = ident.device_id
        return app_helpers.ensure_identity(tmp_path / "A").device_id, ids

    return make


@pytest.fixture
def restore_spy(monkeypatch):
    """Mọi lời gọi `TransferEngine.restore`: đối số (dãy đã chép thành tuple) và lỗi nếu có."""
    calls: list[SimpleNamespace] = []
    real = core_transfer.TransferEngine.restore

    def restore(self, *args, **kwargs):
        rec = SimpleNamespace(
            args=args,
            receives=tuple(kwargs.get("receives", ())),
            tombstones=tuple(kwargs.get("tombstones", ())),
            done=tuple(kwargs.get("done", ())),
            error=None,
        )
        calls.append(rec)
        try:
            return real(self, *args, **kwargs)
        except BaseException as e:
            rec.error = e
            raise

    monkeypatch.setattr(core_transfer.TransferEngine, "restore", restore)
    return calls


@pytest.fixture
def cleanup_spy(monkeypatch):
    """Mọi lần `ReceiveDir.cleanup` chạy xong: `(wall_now, skip, retention, số thư mục đã xóa)`."""
    calls: list[SimpleNamespace] = []
    real = storage.ReceiveDir.cleanup

    def cleanup(self, wall_now, *, own_device_id, skip, retention):
        n = real(self, wall_now, own_device_id=own_device_id, skip=skip, retention=retention)
        calls.append(SimpleNamespace(wall_now=wall_now, skip=frozenset(skip), retention=retention, removed=n))
        return n

    monkeypatch.setattr(storage.ReceiveDir, "cleanup", cleanup)
    return calls


@pytest.fixture
def write_spy(monkeypatch):
    spy = SimpleNamespace(done=[], lock=threading.Lock())
    real = storage.PartFile.write_at

    def write_at(self, index, data):
        real(self, index, data)
        with spy.lock:
            spy.done.append((self.path.parent.name, self.file_index, index))

    def count(tid: str) -> int:
        with spy.lock:
            return sum(1 for r in spy.done if r[0] == tid)

    spy.count = count
    monkeypatch.setattr(storage.PartFile, "write_at", write_at)
    return spy


class HangingOpen:
    """Thay `ReceiveDir.open_for_resume`: lượt trong `hung` chặn ở cổng (trong thread đĩa) rồi mới gọi bản
    thật với đúng `stop` nó nhận; ghi lại `stop` để kiểm nó đã được đặt khi quá hạn."""

    def __init__(self, hung) -> None:
        self.hung = set(hung)
        self.gate = threading.Event()
        self.stops: dict[str, object] = {}
        self.real = storage.ReceiveDir.open_for_resume

    def install(self, monkeypatch) -> None:
        spy = self

        def open_for_resume(rd, scan, *, stop=None):
            if scan.transfer_id in spy.hung:
                spy.stops[scan.transfer_id] = stop
                spy.gate.wait(30)
            return spy.real(rd, scan, stop=stop)

        monkeypatch.setattr(storage.ReceiveDir, "open_for_resume", open_for_resume)


# =================================================================================================
# V2 (c): `engine.restore` đúng một lần
# =================================================================================================


@leakcheck
@pytest.mark.parametrize("with_dir", [False, True], ids=["khong-thu-muc-nhan", "co-thu-muc-nhan"])
async def test_engine_restore_is_called_exactly_once_and_first(restore_spy, node_factory, tmp_path, with_dir):
    kwargs = {"receive_dir": tmp_path / "nhan", "auto_accept": True} if with_dir else {}
    A = await node_factory("A", node_kwargs=kwargs)
    assert len(restore_spy) == 1
    call = restore_spy[0]
    assert call.error is None, f"engine.restore ném {call.error!r} (không phải lời gọi đầu tiên?)"
    assert call.receives == () and call.done == () and call.tombstones == ()
    await A.close()
    assert len(restore_spy) == 1


@leakcheck
async def test_tombstones_of_node_without_receive_dir_survive_restart(
    restore_spy, node_factory, trust_each_other, tmp_path, app_helpers, grace_real
):
    _, id_b = trust_each_other("A", "B")
    a_id = app_helpers.ensure_identity(tmp_path / "A").device_id
    src = make_file(tmp_path / "nguon", "f.bin", 10)
    B = await node_factory("B")
    key = await B.transfers.send(a_id, [src])
    B.transfers.cancel(key)
    await B.close()
    B2 = await node_factory("B")
    assert B2.id == id_b.device_id
    call = restore_spy[-1]
    assert call.error is None
    assert [(t.device_id, t.direction, t.transfer_id, t.reason, t.unsent) for t in call.tombstones] == [
        (a_id, core_transfer.Direction.SEND, key.transfer_id, "user", True)
    ]
    assert all(0 <= t.age < 60 for t in call.tombstones)


# =================================================================================================
# V2 (c): nhiều lượt RUNNING của cùng một máy gửi → giữ lượt gần nhất
# =================================================================================================


@leakcheck
@pytest.mark.parametrize("tie", [False, True], ids=["moi-nhat", "bang-nhau"])
async def test_several_running_from_one_sender_keep_latest_discard_others_and_cancel_expired(
    T, world, restart, node_factory, control_client, record_for, resume_helpers, tmp_path, grace_real, tie
):
    a_id, ids = world("C")
    c_id = ids["C"]
    root = tmp_path / "nhan"
    tids = sorted(uuid.uuid4().hex for _ in range(3))
    ages = [100.0, 100.0, 100.0] if tie else [300.0, 100.0, 200.0]
    for tid, age in zip(tids, ages, strict=True):
        build_running(
            storage,
            root,
            record_for,
            resume_helpers,
            tid,
            sender=c_id,
            receiver=a_id,
            blocks=(0, 1, 2),
            age=age,
        )
    keep = max(tids) if tie else tids[1]
    others = [t for t in tids if t != keep]

    A = await restart("A", receive_dir=root, auto_accept=True)
    resumed = A.trec.of("TransferResumed")
    assert [u.info.key.transfer_id for u in resumed] == [keep]
    info = resumed[0].info
    assert info.key == core_transfer.TransferKey(c_id, RECEIVE, keep)
    assert info.status == core_transfer.TransferStatus.RECONNECTING
    assert info.file_index == 0
    assert [(f.name, f.size) for f in info.files] == [("f0.bin", SIZE)]
    assert info.total_bytes == SIZE
    assert info.done_bytes == 3 * BLOCK_SIZE
    assert [i.key.transfer_id for i in A.transfers.transfers()] == [keep]
    await wait_until(
        lambda: all(not temp_dir(root, t).exists() for t in others),
        10,
        "lượt thừa chưa bị xóa (discard_temp)",
    )
    assert {"transfer.json", "0.part", "0.state"} <= set(os.listdir(temp_dir(root, keep)))

    C = await node_factory("C", auto_connect=False)
    cc = await control_client(C, A)
    await wait_until(
        lambda: all(messages(cc, Cancel, t) for t in others) and messages(cc, Resume, keep),
        10,
        "C chưa nhận cancel{expired} của lượt thừa và resume của lượt giữ lại",
    )
    for t in others:
        assert [m.reason for m in messages(cc, Cancel, t)] == ["expired"]
    assert messages(cc, Cancel, keep) == []
    order = [m for m in cc.received if getattr(m, "transfer_id", None) in tids]
    first_accept = next(i for i, m in enumerate(order) if isinstance(m, Accept | Resume))
    assert all(isinstance(m, Cancel) for m in order[:first_accept]) and first_accept == len(others), order
    [resume] = messages(cc, Resume, keep)
    assert resume.file_index == 0
    bits = Bitfield.from_base64(resume.bitfield, 6)
    assert {i for i in range(6) if i in bits} == {0, 1, 2}

    def saved() -> dict[str, rs.TombstoneRecord]:
        try:
            return {e.transfer_id: e for e in rs.decode_tombstones((A.dir / "tombstones.json").read_bytes())}
        except (FileNotFoundError, PermissionError, rs.StateError):
            return {}

    await wait_until(
        lambda: all(t in saved() and not saved()[t].unsent for t in others),
        5,
        "tombstones.json chưa ghi bia mộ expired đã gửi",
    )
    for t in others:
        e = saved()[t]
        assert (e.device_id, e.direction, e.reason) == (c_id, "receive", "expired")


# =================================================================================================
# V2 (c): mở lại treo, hạn tổng
# =================================================================================================


@leakcheck
async def test_hanging_open_for_resume_skips_only_that_transfer_and_start_stays_bounded(
    T, world, restart, record_for, resume_helpers, tmp_path, monkeypatch, grace_real
):
    deadline = 0.5
    monkeypatch.setattr(T, "DISK_SYNC_TIMEOUT", deadline)
    a_id, ids = world("C", "D")
    root = tmp_path / "nhan"
    hung, ok = uuid.uuid4().hex, uuid.uuid4().hex
    hung_dir = build_running(
        storage, root, record_for, resume_helpers, hung, sender=ids["C"], receiver=a_id, blocks=(0,), age=50
    )
    build_running(
        storage, root, record_for, resume_helpers, ok, sender=ids["D"], receiver=a_id, blocks=(0, 1), age=50
    )
    before = resume_helpers.snapshot(hung_dir)
    fake = HangingOpen([hung])
    fake.install(monkeypatch)
    try:
        t0 = time.monotonic()
        A = await restart("A", receive_dir=root, auto_accept=True)
        elapsed = time.monotonic() - t0
        assert elapsed <= deadline + 3.0, f"Node.start mất {elapsed:.2f} s dù chỉ một lượt treo"
        assert [u.info.key.transfer_id for u in A.trec.of("TransferResumed")] == [ok]
        assert [i.key.transfer_id for i in A.transfers.transfers()] == [ok]
        stop = fake.stops.get(hung)
        assert isinstance(stop, threading.Event), "open_for_resume phải nhận `stop`"
        assert stop.is_set(), "quá hạn mở lại phải đặt `stop`"
    finally:
        fake.gate.set()
    await asyncio.sleep(0.5)  # lượt treo chạy nốt (bị `stop` chặn) và kết quả muộn được dọn
    assert resume_helpers.snapshot(hung_dir) == before, "lượt quá hạn phải để nguyên trên đĩa"


@leakcheck
async def test_more_hanging_restores_than_disk_threads_still_bound_start_by_total_deadline(
    T, world, restart, record_for, resume_helpers, tmp_path, monkeypatch, grace_real
):
    """spec-p3-3b mục 8.1.6: > `DISK_THREADS` lượt cùng treo → `Node.start` xong trong
    `DISK_SYNC_TIMEOUT × ⌈k / DISK_THREADS⌉` + ε; mọi lượt để nguyên, không lượt nào được khôi phục."""
    from teleport.app import diskio

    deadline = 0.5
    monkeypatch.setattr(T, "DISK_SYNC_TIMEOUT", deadline)
    k = diskio.DISK_THREADS + 1
    labels = [f"S{i}" for i in range(k)]
    a_id, ids = world(*labels)
    root = tmp_path / "nhan"
    tids = [uuid.uuid4().hex for _ in labels]
    dirs = [
        build_running(
            storage,
            root,
            record_for,
            resume_helpers,
            tid,
            sender=ids[label],
            receiver=a_id,
            blocks=(0,),
            age=50,
        )
        for tid, label in zip(tids, labels, strict=True)
    ]
    before = [resume_helpers.snapshot(d) for d in dirs]
    fake = HangingOpen(tids)
    fake.install(monkeypatch)
    try:
        t0 = time.monotonic()
        A = await restart("A", receive_dir=root, auto_accept=True)
        elapsed = time.monotonic() - t0
        total = deadline * -(-k // diskio.DISK_THREADS)
        assert elapsed <= total + 3.0, f"Node.start mất {elapsed:.2f} s (hạn tổng {total} s)"
        assert A.trec.of("TransferResumed") == []
        assert A.transfers.transfers() == ()
    finally:
        fake.gate.set()
    await asyncio.sleep(0.5)
    assert [resume_helpers.snapshot(d) for d in dirs] == before


# =================================================================================================
# V2 (c): lượt quá hạn nối lại, máy gửi không còn tin cậy, engine lỗi
# =================================================================================================


@leakcheck
async def test_restored_transfer_older_than_reconnect_grace_expires_without_transfer_resumed(
    T, world, restart, node_factory, control_client, record_for, resume_helpers, tmp_path, monkeypatch
):
    """Q4 + spec-p3 P3-2: tuổi ≥ `RECONNECT_GRACE` → không `TransferResumed`; hết hạn ngay (thư mục tạm bị
    xóa, bia mộ `expired`), máy gửi nối vào thì nhận `cancel{expired}`."""
    monkeypatch.setattr(core_transfer, "RECONNECT_GRACE", 600.0)
    a_id, ids = world("C")
    root = tmp_path / "nhan"
    tid = uuid.uuid4().hex
    d = build_running(
        storage, root, record_for, resume_helpers, tid, sender=ids["C"], receiver=a_id, blocks=(0,), age=3600
    )
    A = await restart("A", receive_dir=root, auto_accept=True)
    assert A.trec.of("TransferResumed") == []
    fin = await A.trec.wait_finished(tid, 10)
    assert fin.result == T.TransferResult.EXPIRED, describe(fin)
    await wait_until(lambda: not d.exists(), 10, "thư mục tạm của lượt quá hạn chưa bị xóa")
    C = await node_factory("C", auto_connect=False)
    cc = await control_client(C, A)
    await wait_until(lambda: messages(cc, Cancel, tid), 10, "C chưa nhận cancel")
    assert [m.reason for m in messages(cc, Cancel, tid)] == ["expired"]


@leakcheck
async def test_engine_restore_error_retries_once_without_receives_and_keeps_temp(
    T, world, restart, record_for, resume_helpers, tmp_path, monkeypatch, caplog, grace_real
):
    """spec-p3-3b mục 8.1.8: `engine.restore` ném ValueError → `log.exception`, gọi lại đúng một lần với
    `receives=()` (bia mộ, lượt xong giữ nguyên); lượt vừa mở được đóng kiểu tạm dừng (`close_temp`), đĩa
    không mất gì; node vẫn chạy."""
    a_id, ids = world("C")
    root = tmp_path / "nhan"
    tid = uuid.uuid4().hex
    d = build_running(
        storage, root, record_for, resume_helpers, tid, sender=ids["C"], receiver=a_id, blocks=(0, 1), age=50
    )
    before = content_of(d)
    calls: list[dict] = []
    real = core_transfer.TransferEngine.restore

    def restore(self, **kwargs):
        calls.append({k: tuple(v) if k != "now" else v for k, v in kwargs.items()})
        if len(calls) == 1:
            raise ValueError("lỗi giả của engine.restore")
        return real(self, **kwargs)

    removed: list[str] = []
    real_remove = storage.ReceiveDir.remove_temp

    def remove_temp(self, temp, *, keep_record=False):
        removed.append(temp.path.name)
        return real_remove(self, temp, keep_record=keep_record)

    monkeypatch.setattr(core_transfer.TransferEngine, "restore", restore)
    monkeypatch.setattr(storage.ReceiveDir, "remove_temp", remove_temp)
    caplog.set_level(logging.ERROR)
    A = await restart("A", receive_dir=root, auto_accept=True)
    assert len(calls) == 2
    assert len(calls[0]["receives"]) == 1 and calls[1]["receives"] == ()
    assert calls[1]["tombstones"] == calls[0]["tombstones"] and calls[1]["done"] == calls[0]["done"]
    assert A.trec.of("TransferResumed") == [] and A.transfers.transfers() == ()
    assert any(r.levelno >= logging.ERROR and r.name.startswith("teleport") for r in caplog.records)
    await asyncio.sleep(0.3)
    assert removed == []
    assert content_of(d) == before


@leakcheck
async def test_sender_unpaired_while_receiver_was_off_is_not_restored_and_dir_untouched(
    T, transfer_nodes, restart, write_spy, cleanup_spy, resume_helpers, grace_real
):
    net = await transfer_nodes(sender_opts={"rate_limit": 8 * MiB})
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 16 * MiB)])
    tid = key.transfer_id
    await wait_until(lambda: write_spy.count(tid) >= 4, 20, "A chưa ghi khối nào")
    await asyncio.wait_for(A.close(), 15)
    TrustStore.load(A.dir).remove(B.id)
    d = temp_dir(net.receive_dir, tid)
    before = resume_helpers.snapshot(d)
    A2 = await restart("A", receive_dir=net.receive_dir, auto_accept=True)
    await wait_until(lambda: len(cleanup_spy) >= 1, 5, "hẹn giờ dọn chưa chạy lần đầu")
    assert A2.trec.of("TransferResumed") == [] and A2.transfers.transfers() == ()
    assert resume_helpers.snapshot(d) == before


# =================================================================================================
# V2 (c): `VERIFY_TARGET` mà file đích sai → nhận lại vào tên mới, file đích không đổi
# =================================================================================================


@leakcheck
async def test_restored_verify_target_with_wrong_target_is_received_again_under_new_name(
    T, transfer_nodes, restart, tcp_proxy, resume_helpers, monkeypatch, tmp_path, grace_real
):
    """“Sập” ngay sau khi đổi tên file 0 (seam `_after_replace`: chép cây thư mục nhận, đồng thời chặn proxy
    để `verified` không tới B). Mở A trên bản chép với file đích bị sửa 1 byte → lượt khôi phục ở
    `VERIFY_TARGET`; B gửi lại `complete`, A đọc lại file đích, thấy sai → nhận lại cả file vào tên mới;
    file đích sai để nguyên; lượt DONE, cả hai file khớp nguồn."""
    net = await transfer_nodes(connect=False, sender_opts={"rate_limit": 8 * MiB})
    A, B = net.A, net.B
    proxy = tcp_proxy(A.port)
    B.connect_hint(A.id, [LOCAL], proxy.port)
    await wait_until(lambda: A.id in B.connected_devices(), 5, "B chưa nối qua proxy")
    f0 = make_file(net.src, "a.bin", 3 * MiB + 5)
    f1 = make_file(net.src, "b.bin", 4 * MiB)
    crash = tmp_path / "sap"
    copied = threading.Event()

    def after_replace(target):
        if not copied.is_set() and Path(target).name == "a.bin":
            proxy.block()
            resume_helpers.crash_copy(net.receive_dir, crash)
            copied.set()

    monkeypatch.setattr(storage, "_after_replace", after_replace)
    key = await B.transfers.send(A.id, [f0, f1])
    tid = key.transfer_id
    await wait_until(copied.is_set, 30, "chưa tới bước đổi tên file 0")
    await asyncio.wait_for(A.close(), 15)
    proxy.rst()
    target = crash / "a.bin"
    wrong = bytearray(target.read_bytes())
    wrong[MiB + 1] ^= 0xFF
    target.write_bytes(bytes(wrong))

    A2 = await restart("A", receive_dir=crash, auto_accept=True)
    [resumed] = A2.trec.of("TransferResumed", tid)
    assert resumed.info.file_index == 0
    proxy.target = (LOCAL, A2.port)
    proxy.resume()
    fin_b = await B.trec.wait_finished(tid, 60)
    fin_a = await A2.trec.wait_finished(tid, 60)
    assert fin_b.result == T.TransferResult.DONE, describe(fin_b)
    assert fin_a.result == T.TransferResult.DONE, describe(fin_a)
    assert target.read_bytes() == bytes(wrong), "file đích sai phải để nguyên"
    done = {u.file_index: u for u in A2.trec.of("FileDone", tid)}
    assert set(done) == {0, 1}
    assert done[0].path is not None and done[0].path != target
    assert sha256_of(done[0].path) == sha256_of(f0)
    assert sha256_of(done[1].path) == sha256_of(f1)


# =================================================================================================
# V3: các ca trong tiến trình
# =================================================================================================


@leakcheck
async def test_sender_that_lost_its_state_answers_unknown_transfer_and_receiver_cleans_up(
    T, transfer_nodes, restart, write_spy, grace_real
):
    """Bên gửi đã khởi động lại mà không còn biết lượt (như bị giết trước khi kịp lưu bia mộ) → A khôi phục
    lượt, gửi `accept`/`resume`, nhận `cancel{unknown_transfer}` → `UNKNOWN_TRANSFER`, dọn thư mục tạm."""
    net = await transfer_nodes(sender_opts={"rate_limit": 8 * MiB})
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 16 * MiB)])
    tid = key.transfer_id
    await wait_until(lambda: write_spy.count(tid) >= 4, 20, "A chưa ghi khối nào")
    await asyncio.wait_for(A.close(), 15)
    await asyncio.wait_for(B.close(), 15)
    (B.dir / "tombstones.json").unlink(missing_ok=True)
    A2 = await restart("A", receive_dir=net.receive_dir, auto_accept=True)
    assert len(A2.trec.of("TransferResumed", tid)) == 1
    B2 = await restart("B")
    B2.connect_hint(A2.id, [LOCAL], A2.port)
    fin = await A2.trec.wait_finished(tid, 20)
    assert fin.result == T.TransferResult.UNKNOWN_TRANSFER and fin.by_peer is True, describe(fin)
    await wait_until(lambda: not temp_dir(net.receive_dir, tid).exists(), 10, "thư mục tạm chưa bị dọn")


@leakcheck
async def test_receiver_tombstone_survives_restart_sender_gets_cancel_user_and_no_new_offer_prompt(
    T, transfer_nodes, restart, tcp_proxy, write_spy, grace_real
):
    net = await transfer_nodes(connect=False, sender_opts={"rate_limit": 4 * MiB})
    A, B = net.A, net.B
    proxy = tcp_proxy(A.port)
    B.connect_hint(A.id, [LOCAL], proxy.port)
    await wait_until(lambda: A.id in B.connected_devices() and B.id in A.connected_devices(), 5)
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 16 * MiB)])
    tid = key.transfer_id
    await wait_until(lambda: write_spy.count(tid) >= 2, 20, "A chưa ghi khối nào")
    proxy.block()
    proxy.rst()
    await wait_until(
        lambda: B.id not in A.connected_devices() and A.id not in B.connected_devices(),
        10,
        "chưa mất kết nối",
    )
    A.transfers.cancel(core_transfer.TransferKey(B.id, RECEIVE, tid))
    fin_a = await A.trec.wait_finished(tid, 5)
    assert fin_a.result == T.TransferResult.CANCELLED_USER and fin_a.by_peer is False
    await asyncio.wait_for(A.close(), 15)
    stones = rs.decode_tombstones((A.dir / "tombstones.json").read_bytes())
    assert [(e.device_id, e.direction, e.transfer_id, e.reason, e.unsent) for e in stones] == [
        (B.id, "receive", tid, "user", True)
    ]

    A2 = await restart("A", receive_dir=net.receive_dir, auto_accept=True)
    proxy.target = (LOCAL, A2.port)
    proxy.resume()
    fin_b = await B.trec.wait_finished(tid, 30)
    assert fin_b.result == T.TransferResult.CANCELLED_USER and fin_b.by_peer is True, describe(fin_b)
    await asyncio.sleep(0.5)
    assert A2.trec.of("OfferReceived", tid) == [], "lượt có bia mộ không được hỏi lại"
    assert not temp_dir(net.receive_dir, tid).exists()


@leakcheck
async def test_sender_closed_while_disconnected_sends_unsent_cancel_after_restart_and_new_send_works(
    T, transfer_nodes, restart, tcp_proxy, write_spy, grace_real
):
    """`send` Ctrl+C khi mất kết nối (B đóng lúc không có kết nối điều khiển) rồi `send` mới (B mở lại):
    `cancel{user}` chưa gửi tới được A ngay khi nối lại; A dọn thư mục tạm; lượt mới chạy bình thường."""
    net = await transfer_nodes(connect=False, sender_opts={"rate_limit": 4 * MiB})
    A, B = net.A, net.B
    proxy = tcp_proxy(A.port)
    B.connect_hint(A.id, [LOCAL], proxy.port)
    await wait_until(lambda: A.id in B.connected_devices() and B.id in A.connected_devices(), 5)
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 16 * MiB)])
    tid = key.transfer_id
    await wait_until(lambda: write_spy.count(tid) >= 2, 20, "A chưa ghi khối nào")
    proxy.block()
    proxy.rst()
    await wait_until(lambda: A.id not in B.connected_devices(), 10, "B chưa mất kết nối")
    await asyncio.wait_for(B.close(), 15)
    assert B.trec.finished(tid).result == T.TransferResult.CANCELLED_USER
    stones = rs.decode_tombstones((B.dir / "tombstones.json").read_bytes())
    assert [(e.device_id, e.direction, e.transfer_id, e.reason, e.unsent) for e in stones] == [
        (A.id, "send", tid, "user", True)
    ]
    assert A.trec.finished(tid) is None

    proxy.resume()
    B2 = await restart("B")
    B2.connect_hint(A.id, [LOCAL], proxy.port)
    fin_a = await A.trec.wait_finished(tid, 30)
    assert fin_a.result == T.TransferResult.CANCELLED_USER and fin_a.by_peer is True, describe(fin_a)
    await wait_until(lambda: not temp_dir(net.receive_dir, tid).exists(), 10, "thư mục tạm chưa bị dọn")

    src2 = make_file(net.src, "moi.bin", MiB + 1)
    key2 = await B2.transfers.send(A.id, [src2])
    assert (await B2.trec.wait_finished(key2.transfer_id, 30)).result == T.TransferResult.DONE
    assert sha256_of(net.receive_dir / "moi.bin") == sha256_of(src2)


@leakcheck
async def test_done_transfer_answers_verified_after_restart_and_record_is_cleaned_after_remember_ttl(
    T, transfer_nodes, restart, control_client, cleanup_spy, monkeypatch, grace_real
):
    net = await transfer_nodes()
    A, B = net.A, net.B
    src = make_file(net.src, "f.bin", 3 * MiB + 1)
    key = await B.transfers.send(A.id, [src])
    tid = key.transfer_id
    assert (await A.trec.wait_finished(tid, 30)).result == T.TransferResult.DONE
    d = temp_dir(net.receive_dir, tid)
    await wait_until(lambda: d.is_dir() and os.listdir(d) == ["transfer.json"], 10, "chỉ còn transfer.json")
    peer_b = SimpleNamespace(identity=B.identity, hello=B.hello)
    await asyncio.wait_for(B.close(), 15)
    await asyncio.wait_for(A.close(), 15)

    A2 = await restart("A", receive_dir=net.receive_dir, auto_accept=True)
    assert A2.trec.of("TransferResumed") == []
    await wait_until(lambda: len(cleanup_spy) >= 1, 5, "hẹn giờ dọn chưa chạy lần đầu")
    assert os.listdir(d) == ["transfer.json"], "lượt xong còn trong hạn nhớ không được dọn"
    cc = await control_client(peer_b, A2)
    await cc.conn.send(Complete(tid, 0, merkle_root(leaves_of(src.read_bytes())).hex()))
    await wait_until(lambda: messages(cc, Verified, tid), 10, "A không trả verified sau khởi động lại")
    assert [m.file_index for m in messages(cc, Verified, tid)] == [0]
    assert messages(cc, Cancel, tid) == []
    await asyncio.wait_for(A2.close(), 15)

    monkeypatch.setattr(T, "_wall_time", lambda: time.time() + core_transfer.REMEMBER_TTL + 3600)
    n = len(cleanup_spy)
    await restart("A", receive_dir=net.receive_dir, auto_accept=True)
    await wait_until(lambda: len(cleanup_spy) > n, 5, "hẹn giờ dọn chưa chạy")
    await wait_until(lambda: not d.exists(), 10, "transfer.json quá hạn nhớ chưa bị dọn")
    assert sha256_of(net.receive_dir / "f.bin") == sha256_of(src), "file đã nhận không được đụng"
    assert (net.receive_dir / ".teleport").is_dir()


@leakcheck
async def test_two_configs_sharing_receive_dir_do_not_touch_each_other(
    T, transfer_nodes, restart, write_spy, cleanup_spy, resume_helpers, app_helpers, tmp_path, grace_real
):
    net = await transfer_nodes(sender_opts={"rate_limit": 8 * MiB})
    A, B = net.A, net.B
    src = make_file(net.src, "f.bin", 16 * MiB)
    key = await B.transfers.send(A.id, [src])
    tid = key.transfer_id
    await wait_until(lambda: write_spy.count(tid) >= 4, 20, "A chưa ghi khối nào")
    await asyncio.wait_for(A.close(), 15)
    d = temp_dir(net.receive_dir, tid)
    before = resume_helpers.snapshot(d)

    app_helpers.ensure_identity(tmp_path / "C")
    app_helpers.seed_trust(tmp_path / "C", B.identity.cert_pem(), "B")  # C cũng tin máy gửi B
    n = len(cleanup_spy)
    C = await restart("C", receive_dir=net.receive_dir, auto_accept=True)
    await wait_until(lambda: len(cleanup_spy) > n, 5, "hẹn giờ dọn của C chưa chạy")
    assert C.trec.of("TransferResumed") == [] and C.transfers.transfers() == ()
    assert resume_helpers.snapshot(d) == before, (
        "node khác dùng chung thư mục nhận không được đụng lượt của A"
    )
    await asyncio.wait_for(C.close(), 15)
    assert resume_helpers.snapshot(d) == before

    A2 = await restart("A", receive_dir=net.receive_dir, auto_accept=True)
    B.connect_hint(A2.id, [LOCAL], A2.port)
    fin_a = await A2.trec.wait_finished(tid, 60)
    assert fin_a.result == T.TransferResult.DONE, describe(fin_a)
    assert sha256_of(net.receive_dir / "f.bin") == sha256_of(src)


@leakcheck
async def test_cleanup_timer_never_removes_running_transfer(
    T, transfer_nodes, write_spy, cleanup_spy, monkeypatch, grace_real
):
    """Hẹn giờ dọn (rút ngắn `CLEANUP_INTERVAL`) chạy nhiều lần trong lúc lượt đang nhận, với giờ tường
    vá xa hơn mọi ngưỡng: `skip` luôn có lượt đó, thư mục tạm còn, lượt DONE."""
    monkeypatch.setattr(T, "CLEANUP_INTERVAL", 0.2)
    net = await transfer_nodes(sender_opts={"rate_limit": 4 * MiB})
    A, B = net.A, net.B
    src = make_file(net.src, "f.bin", 12 * MiB)
    key = await B.transfers.send(A.id, [src])
    tid = key.transfer_id
    await wait_until(lambda: write_spy.count(tid) >= 2, 20, "A chưa ghi khối nào")
    future = time.time() + 10 * core_transfer.REMEMBER_TTL
    monkeypatch.setattr(T, "_wall_time", lambda: future)
    n = len(cleanup_spy)
    await wait_until(lambda: len(cleanup_spy) >= n + 2, 5, "hẹn giờ dọn không chạy lặp lại")
    assert temp_dir(net.receive_dir, tid).is_dir()
    fin_a = await A.trec.wait_finished(tid, 60)
    assert fin_a.result == T.TransferResult.DONE, describe(fin_a)
    during = [c for c in cleanup_spy[n:] if c.wall_now == future]
    assert during and all(tid in c.skip for c in during[:2]), [sorted(c.skip) for c in during]
    assert all(c.retention == storage.TEMP_RETENTION for c in cleanup_spy)
    assert sha256_of(net.receive_dir / "f.bin") == sha256_of(src)


# =================================================================================================
# API của `TransferManager`: `restore` một lần, `cleanup_temps`, `abort_now` giữa lúc khôi phục
# =================================================================================================


@leakcheck
async def test_transfer_manager_restore_twice_raises_runtime_error(node_factory, tmp_path):
    A = await node_factory("A", node_kwargs={"receive_dir": tmp_path / "nhan", "auto_accept": True})
    with pytest.raises(RuntimeError):
        await A.transfers.restore()


@leakcheck
async def test_cleanup_temps_is_none_without_receive_dir_or_when_closing(node_factory, tmp_path):
    B = await node_factory("B")
    assert await B.transfers.cleanup_temps() is None
    A = await node_factory("A", node_kwargs={"receive_dir": tmp_path / "nhan", "auto_accept": True})
    results: list[int | None] = []

    async def until_ran() -> None:
        while True:
            n = await A.transfers.cleanup_temps()  # None khi lần dọn trước (lúc khởi động) chưa xong
            if n is not None:
                results.append(n)
                return
            await asyncio.sleep(0.05)

    await asyncio.wait_for(until_ran(), 5)
    assert results == [0]
    A.transfers.begin_close()
    assert await A.transfers.cleanup_temps() is None


@leakcheck
async def test_start_cancelled_during_restore_sets_stop_and_leaves_disk_untouched(
    world, node_factory, app_helpers, record_for, resume_helpers, tmp_path, monkeypatch, grace_real
):
    """`Node.start` bị hủy khi đang mở lại một lượt (thread đĩa treo) → `abort_now` đặt `stop` của việc mở
    lại; lượt đó để nguyên trên đĩa, `node.lock` được nhả."""
    a_id, ids = world("C")
    root = tmp_path / "nhan"
    tid = uuid.uuid4().hex
    d = build_running(
        storage, root, record_for, resume_helpers, tid, sender=ids["C"], receiver=a_id, blocks=(0,), age=50
    )
    before = resume_helpers.snapshot(d)
    fake = HangingOpen([tid])
    fake.install(monkeypatch)
    A = await node_factory("A", start=False, node_kwargs={"receive_dir": root, "auto_accept": True})
    starting = asyncio.create_task(A.node.start())
    try:
        await wait_until(lambda: tid in fake.stops, 10, "chưa tới bước mở lại")
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        stop = fake.stops[tid]
        assert isinstance(stop, threading.Event) and stop.is_set()
    finally:
        fake.gate.set()
    await asyncio.sleep(0.5)
    assert resume_helpers.snapshot(d) == before
    await wait_until(lambda: app_helpers.lock_is_free(tmp_path / "A"), 5, "node.lock chưa được nhả")
