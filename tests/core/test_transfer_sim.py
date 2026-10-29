"""Mô phỏng hai `TransferEngine` nối qua kênh giả, thời gian ảo (spec P2-4 mục 2.8; AC-19, AC-20,
AC-21, AC-13, AC-6 phần hai engine, ca biên 1000 file và kịch bản cố định của AC-19).

Bộ thực thi theo đúng hợp đồng ở spec mục 2.8:
- kết quả của mọi hành động được giao sau qua hàng sự kiện FIFO của từng máy (như `loop.call_soon`),
  không bao giờ gọi engine lồng nhau;
- hành động đĩa của mỗi lượt chạy theo một hàng FIFO; `RemoveTemp` bỏ các thao tác chưa chạy;
- `SendBlocks`: đọc khối, `block_read`, chỉ ghi khung khi kết quả không có `CloseData`;
- `ReceiveBlocks`: khung hỏng → `block_bad` rồi cắt; chỉ ghi payload khi có `WriteBlock` đúng chỉ số.

Mô hình kênh: mỗi kết nối (điều khiển hay dữ liệu) là một FIFO riêng; giữa các kết nối, thứ tự giao
tùy ý. Mất mạng làm mất message đang trên đường; hai bên thấy mất kết nối ở lúc khác nhau, có khi chỉ
thấy kết nối bị thay; kết nối mới có client chọn ngẫu nhiên; kết nối dữ liệu có thể mất trước khi tới
engine bên server. Đĩa giả lưu `chỉ số → digest`; `fsync` có lúc chậm (hàng đĩa của lượt bận tới lúc
xong). Bất biến kiểm ở mỗi hành động: G1–G5, `WriteBlock` ≤ hw + 1, mọi `resume` của bên nhận chứa
mọi khối đã ghi.
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import random
from collections import Counter, defaultdict, deque
from functools import partial

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

import teleport.core.transfer as transfer
from teleport.core.bitfield import Bitfield
from teleport.core.blocks import BLOCK_SIZE, block_count
from teleport.core.filenames import dedupe_offer_names
from teleport.core.keepalive import DEAD_AFTER
from teleport.core.merkle import merkle_root
from teleport.core.messages import (
    Accept,
    Cancel,
    Complete,
    DataOpen,
    FileEntry,
    Offer,
    Reject,
    Resume,
    Verified,
)
from teleport.core.transfer import (
    DATA_OPEN_WAIT,
    DATA_RETRY_MAX,
    AskUser,
    CheckSource,
    CheckSpace,
    ClearBlocks,
    CloseData,
    CreateTemp,
    Direction,
    DiskOp,
    FailureDetail,
    FileVerified,
    Finalize,
    OpenData,
    OpenPart,
    Outcome,
    ReceiveBlocks,
    RemoveTemp,
    Report,
    Reread,
    RestoredDone,
    RestoredReceive,
    SendBlocks,
    SendMessage,
    StatusChanged,
    SuspendTemp,
    SyncPart,
    TombstonesChanged,
    TransferEnded,
    TransferEngine,
    TransferKey,
    TransferStatus,
    WriteBlock,
)

SA = "a" * 32  # máy gửi
RB = "b" * 32  # máy nhận
TID = "5" * 32
FREE = 10**15
ZERO = b"\x00" * 32
GARBAGE = b"\xee" * 32
EMPTY_ROOT = hashlib.sha256(b"").digest()

SIM = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def check_order(acts) -> None:
    """G5: Report đứng cuối; trong nhóm Report, StatusChanged đứng sau. P3-2: ≤ 1 TombstonesChanged."""
    rank = [(2 if isinstance(a.event, StatusChanged) else 1) if isinstance(a, Report) else 0 for a in acts]
    assert rank == sorted(rank), f"vi phạm G5: {acts}"
    assert sum(isinstance(a, TombstonesChanged) for a in acts) <= 1, f"hơn một TombstonesChanged: {acts}"


class Link:
    """Kết nối điều khiển giữa hai máy. `q[n]`: message đang trên đường tới máy `n`."""

    def __init__(self, sim: Sim, client: Node) -> None:
        self.alive = True
        self.client = client
        self.ids = {n.name: next(sim.ids) for n in sim.nodes}
        self.q: dict[str, deque] = {n.name: deque() for n in sim.nodes}


class Conn:
    """Kết nối dữ liệu. `frames`: khung (chỉ số, sha256 header, digest payload) đang trên đường."""

    def __init__(self, sim: Sim, client: Node, server: Node, msg: DataOpen) -> None:
        self.client = client
        self.server = server
        self.msg = msg
        self.ids = {client.name: next(sim.ids), server.name: next(sim.ids)}
        self.frames: deque[tuple[int, bytes, bytes]] = deque()
        self.closed = False
        self.eof = False
        self.arrived = False


class Node:
    def __init__(self, sim: Sim, name: str, device_id: str, receiving: bool) -> None:
        self.sim = sim
        self.name = name
        self.device_id = device_id
        self.engine = TransferEngine(device_id, receiving=receiving)
        self.peer: Node
        self.inbox: deque[tuple[str, tuple]] = deque()
        self.link_events: deque[tuple[str, Link]] = deque()
        self.current: Link | None = None
        self.seen: set[int] = set()
        self.data: dict[int, Conn] = {}
        self.send_jobs: dict[int, list] = {}
        self.recv_jobs: dict[int, tuple[str, int]] = {}
        # Đĩa giả (bên nhận).
        self.temps: set[str] = set()
        self.parts: dict[tuple[str, int], dict[int, bytes]] = {}
        self.finals: dict[str, tuple[str, int, dict[int, bytes], int]] = {}
        self.disk_q: dict[str, deque] = {}
        self.disk_dead: set[str] = set()
        self.disk_busy: dict[str, float] = {}  # hàng FIFO đĩa của lượt bận tới lúc này (fsync chậm)
        # Quan sát.
        self.ended: dict[TransferKey, TransferEnded] = {}
        self.ended_at: dict[TransferKey, float] = {}  # thời gian ảo lúc có TransferEnded
        self.file_verified: list[FileVerified] = []
        self.cancels_sent: dict[str, list[str]] = defaultdict(list)
        self.completes: list[Complete] = []
        self.finalize_count: Counter = Counter()
        self.hw: dict[tuple[str, int], int] = {}
        self.written: dict[tuple[str, int], set[int]] = defaultdict(set)
        self.created: set[str] = set()
        self.removed: set[str] = set()
        self.active: dict[str, int] = {}
        self.write_log: list[tuple[int, str, int, int]] = []  # (bước, T, file, chỉ số)
        self.send_log: list[tuple[int, int, list[int]]] = []  # (bước, file, khối thiếu)
        self.resumes: list[Resume] = []
        self.known: set[TransferKey] = set()
        self.kept: set[str] = set()  # thư mục tạm chỉ còn `transfer.json` của lượt đã xong (keep_record)
        self.tomb_changes = 0
        self.temp_ok: set[str] = set()  # lượt đã được báo `temp_created` (hay khôi phục từ đĩa)


class Sim:
    """Hai máy: A gửi lượt `TID` cho B. `faults`: tập lỗi được chèn (xem `maybe_fault`)."""

    def __init__(
        self,
        rng: random.Random,
        nblocks: list[int],
        *,
        faults: frozenset[str] = frozenset(),
        short_last: bool = True,
        user_cancel: bool = False,
        reject_sometimes: bool = False,
        first_client: str | None = None,
        fault_rate: float = 0.01,
        lose_before_arrival: int = 0,
        slow_sync: float = 0.3,
        sync_delay: tuple[float, float] = (0.5, 3.0),
        break_mid_send: bool = False,
        data_breaks: int = 5,
    ) -> None:
        self.rng = rng
        self.now = 1.0
        self.steps = 0
        self.ids = itertools.count(1)
        self.A = Node(self, "A", SA, receiving=False)
        self.B = Node(self, "B", RB, receiving=True)
        self.A.peer, self.B.peer = self.B, self.A
        self.nodes = [self.A, self.B]
        files = []
        for i, n in enumerate(nblocks):
            last = rng.randint(1, BLOCK_SIZE) if short_last and rng.random() < 0.5 else BLOCK_SIZE
            files.append(
                FileEntry(name=f"tep{i}.bin", size=0 if n == 0 else (n - 1) * BLOCK_SIZE + last, mtime=10 + i)
            )
        self.files = tuple(files)
        self.n = [block_count(f.size) for f in files]
        self.src = {
            (f, i): hashlib.sha256(f"src:{f}:{i}".encode()).digest()
            for f in range(len(files))
            for i in range(self.n[f])
        }
        self.src_mtime = [f.mtime for f in files]
        self.timers: list = []
        self.tseq = itertools.count()
        self.links: list[Link] = []
        self.conns: list[Conn] = []
        self.net_down = False
        self.current_link: Link | None = None
        self.faults = faults
        self.fault_rate = fault_rate
        self.fault_budget = {"outage": 3, "long_outage": 1, "switch": 2, "data_break": data_breaks}
        self.user_cancel = user_cancel
        self.reject_sometimes = reject_sometimes
        self.first_client = first_client
        # Chèn lỗi có chủ đích (AC-20).
        self.corrupt_file: int | None = None
        self.disk_corrupt: tuple[int, int] | None = None  # (file, khối) sửa trên đĩa ở lần đọc lại đầu
        self.source_change_on_reread = False
        self.corrupted_step: int | None = None
        # Số kết nối dữ liệu đầu tiên bị mất trước khi tới engine bên server (xem `arrive`).
        self.lose_before_arrival = lose_before_arrival
        self.dropped = 0
        self.sync_delay = sync_delay
        self.slow_sync = slow_sync  # xác suất một `SyncPart` chạy chậm, mất `sync_delay` giây ảo
        self.break_mid_send = break_mid_send

    # ---- tiện ích ----

    def at(self, t: float, fn) -> None:
        heapq.heappush(self.timers, (t, next(self.tseq), fn))

    def next_due(self) -> float | None:
        cands = [self.timers[0][0]] if self.timers else []
        for n in self.nodes:
            w = n.engine.next_wakeup()
            if w is not None:
                cands.append(w)
        return min(cands) if cands else None

    def advance(self, dt: float) -> None:
        limit = self.next_due()
        new = self.now + dt
        if limit is not None:
            new = min(new, max(limit, self.now))
        self.now = new

    # ---- khởi động ----

    def start(self) -> None:
        client = {"A": self.A, "B": self.B}.get(self.first_client or "", self.rng.choice(self.nodes))
        link = Link(self, client)
        self.links.append(link)
        self.current_link = link
        for n in self.nodes:
            n.link_events.append(("up", link))
        for n in self.nodes:
            self.process_link_event(n)
        acts = self.A.engine.send(RB, TID, list(self.files), self.now)
        self.execute(self.A, acts, "send")
        if self.user_cancel:
            for _ in range(self.rng.randint(0, 2)):
                self.at(
                    self.now + self.rng.uniform(0, 8),
                    partial(self.cancel_by_user, self.rng.choice(self.nodes)),
                )

    # ---- vòng chính ----

    def run(self, max_steps: int = 30_000) -> None:
        while self.steps < max_steps:
            self.steps += 1
            if self.timers and self.timers[0][0] <= self.now:
                _, _, fn = heapq.heappop(self.timers)
                fn()
                continue
            node = next((n for n in self.nodes if n.link_events), None)
            if node is not None:
                self.process_link_event(node)
                continue
            tasks = self.tasks()
            if tasks:
                self.rng.choice(tasks)()
                self.maybe_fault()
                if self.rng.random() < 0.2:
                    self.advance(self.rng.choice([0.001, 0.01, 0.05, 0.3]))
                continue
            nxt = self.next_due()
            if nxt is None:
                return
            self.now = max(self.now, nxt)
        raise AssertionError(f"mô phỏng không dừng sau {max_steps} bước")

    def tasks(self) -> list:
        out = []
        for n in self.nodes:
            if n.inbox:
                out.append(partial(self.process_inbox, n))
            for T, q in n.disk_q.items():
                if q and n.disk_busy.get(T, 0.0) <= self.now:
                    out.append(partial(self.disk_step, n, T))
            w = n.engine.next_wakeup()
            if w is not None and w <= self.now:
                out.append(partial(self.tick, n))
            for link in self.links:
                if link.q[n.name] and id(link) in n.seen:
                    out.append(partial(self.deliver, link, n))
        for c in self.conns:
            if c.closed:
                continue
            if not c.arrived:
                out.append(partial(self.arrive, c))
            for n in (c.client, c.server):
                did = c.ids[n.name]
                if n.data.get(did) is not c:
                    continue
                if did in n.send_jobs:
                    out.append(partial(self.send_step, n, did))
                if did in n.recv_jobs and (c.frames or c.eof):
                    out.append(partial(self.recv_step, n, did))
        return out

    # ---- kết nối điều khiển ----

    def process_link_event(self, n: Node) -> None:
        kind, link = n.link_events.popleft()
        cid = link.ids[n.name]
        if kind == "up" and link is not self.current_link:
            return  # kết nối đã bị thay trước khi kịp báo: app không bao giờ báo LinkUp cho nó
        if kind == "up":
            n.current = link
            n.seen.add(id(link))
            acts = n.engine.control_up(n.peer.device_id, cid, link.client is n, self.now)
            self.execute(n, acts, "control_up")
            return
        if id(link) not in n.seen:
            return
        was_current = n.current is link
        if was_current:
            n.current = None
        acts = n.engine.control_lost(n.peer.device_id, cid, self.now)
        if not was_current:
            assert acts == [], "control_lost của kết nối cũ phải bị bỏ qua"
        self.execute(n, acts, "control_lost")

    def deliver(self, link: Link, n: Node) -> None:
        msg = link.q[n.name].popleft()
        acts = n.engine.message(n.peer.device_id, link.ids[n.name], msg, self.now)
        if n.current is not link:
            assert acts == [], f"message trên kết nối điều khiển cũ phải bị bỏ qua: {msg} → {acts}"
        if isinstance(msg, Cancel):
            assert [a for a in acts if isinstance(a, SendMessage)] == [], "cancel không bao giờ được trả lời"
        self.execute(n, acts, "message", (msg,))

    # ---- hàng sự kiện ----

    def process_inbox(self, n: Node) -> None:
        name, args = n.inbox.popleft()
        if name == "source_checked*":
            T, f, ref, snap = args
            assert bytes(ref) == snap, "leaves của CheckSource bị sửa"
            args = (n.peer.device_id, T, f, self.files[f].size, self.src_mtime[f], merkle_root(bytes(ref)))
            name = "source_checked"
        acts = getattr(n.engine, name)(*args, self.now)
        self.execute(n, acts, name, args)

    def tick(self, n: Node) -> None:
        self.execute(n, n.engine.tick(self.now), "tick")

    def cancel_by_user(self, n: Node) -> None:
        infos = n.engine.transfers()
        if infos:
            self.execute(n, n.engine.cancel(infos[0].key, self.now), "cancel")

    # ---- thực thi hành động ----

    def execute(self, n: Node, acts: list, call: str, args: tuple = (), payload: tuple | None = None) -> None:
        assert isinstance(acts, list)
        check_order(acts)
        if call == "control_up":
            self.check_resync_order(n, acts)
        if call == "temp_created":
            n.temp_ok.add(args[1])
        if n is self.B and call == "reread_done":
            # Khối đọc lại sai (hay root sai: mọi khối) bị tắt bit và phải được xin lại (spec 3.8):
            # bỏ chúng khỏi tập “đã ghi” mà mọi `resume` sau phải chứa (xem `do`, SendMessage).
            _, T, f, bad, _root = args
            if bad:
                n.written[(T, f)] -= set(bad)
            elif not any(isinstance(a, Finalize) for a in acts):
                n.written[(T, f)].clear()
        if n is self.B and call == "reread_done":
            # QĐ2: khối đọc lại sai (hay root sai) → `ClearBlocks` cùng danh sách, trước mọi `SyncPart`
            # sau đó (FIFO đĩa của lượt), trừ khi lượt kết thúc ngay (quá số lần đọc lại sai).
            _, T, f, bad, _root = args
            ends = [a for a in acts if isinstance(a, Report) and isinstance(a.event, TransferEnded)]
            if not any(isinstance(a, Finalize) for a in acts) and not ends:
                clears = [a for a in acts if isinstance(a, ClearBlocks)]
                want = tuple(bad) if bad else None
                assert clears == [ClearBlocks(n.peer.device_id, T, f, want)], acts
        for a in acts:
            if isinstance(a, RemoveTemp):
                # `keep_record` True đúng khi lượt kết thúc DONE trong cùng danh sách (spec-p3 P3-2).
                key = TransferKey(n.peer.device_id, Direction.RECEIVE, a.transfer_id)
                done = any(
                    isinstance(b, Report)
                    and isinstance(b.event, TransferEnded)
                    and b.event.key == key
                    and b.event.outcome == Outcome.DONE
                    for b in acts
                )
                assert a.keep_record is done, (a, acts)
        if n is self.B:
            for i, a in enumerate(acts):
                if isinstance(a, OpenData):
                    assert any(
                        isinstance(b, SendMessage)
                        and isinstance(b.message, Resume)
                        and b.message.seq == a.message.resume_seq
                        for b in acts[:i]
                    ), f"G3: OpenData phải đứng sau resume cùng seq: {acts}"
        for a in acts:
            self.do(n, a, call, args, payload)

    def check_resync_order(self, n: Node, acts: list) -> None:
        msgs = [a.message for a in acts if isinstance(a, SendMessage)]
        if n is self.A:
            rank = {Cancel: 0, Offer: 1, Complete: 2}
            ranks = [rank[type(m)] for m in msgs if type(m) in rank]
            assert ranks == sorted(ranks), f"G1 (control_up bên gửi): {msgs}"
        else:
            kinds = [type(m) for m in msgs if isinstance(m, Accept | Resume)]
            if Resume in kinds:
                assert kinds.index(Accept) < kinds.index(Resume), f"G1: accept trước resume: {msgs}"

    def do(self, n: Node, a, call: str, args: tuple, payload) -> None:
        peer = n.peer
        if isinstance(a, SendMessage):
            assert n.current is not None and a.control_id == n.current.ids[n.name], (
                a,
                "kết nối không hiện tại",
            )
            link = n.current
            m = a.message
            if isinstance(m, Cancel):
                n.cancels_sent[m.transfer_id].append(m.reason)
            if isinstance(m, Complete):
                n.completes.append(m)
            if n is self.B and isinstance(m, Accept | Resume):
                # spec-p3 “`temp_created`”: bên nhận chỉ gửi `accept`/`resume` sau khi thư mục tạm và
                # `transfer.json` đã bền, nên máy gửi không bao giờ có `complete` mà B có thể quên.
                assert m.transfer_id in n.temp_ok, f"{type(m).__name__} trước temp_created: {m}"
            if isinstance(m, Resume):
                n.resumes.append(m)
                if n is self.B:
                    # Spec 3.6 (“Nhờ fsync trước khi gửi, `resume` không bao giờ xin lại khối đã ghi”),
                    # D50, protocol §4.3: mọi khối đã `WriteBlock` của file phải có bit trong `resume`.
                    on = _bits(m.bitfield, self.n[m.file_index])
                    again = n.written[(m.transfer_id, m.file_index)] - on
                    assert not again, f"resume seq={m.seq} xin lại khối đã ghi {sorted(again)}"
            if link.alive:
                link.q[peer.name].append(m)
        elif isinstance(a, OpenData):
            assert (
                n.current is not None and a.control_id == n.current.ids[n.name] and n.current.client is n
            ), a
            assert self.n[a.message.file_index] > 0, "không mở kết nối dữ liệu cho file rỗng"
            self.at(self.now + self.rng.uniform(0, 0.05), partial(self.open_attempt, n, n.current, a.message))
        elif isinstance(a, CloseData):
            self.close_local(n, a.data_id)
        elif isinstance(a, SendBlocks | ReceiveBlocks):
            assert self.n[a.file_index] > 0 and a.size == self.files[a.file_index].size, a
            old = n.active.get(a.transfer_id)
            assert old is None or old == a.data_id or old not in n.data, "G4: kết nối cũ chưa đóng"
            n.active[a.transfer_id] = a.data_id
            conn = n.data.get(a.data_id)
            if isinstance(a, SendBlocks):
                n.send_log.append((self.steps, a.file_index, list(a.have.missing())))
                if conn is not None:
                    n.send_jobs[a.data_id] = [a.transfer_id, a.file_index, list(a.have.missing())]
            elif conn is not None:
                n.recv_jobs[a.data_id] = (a.transfer_id, a.file_index)
        elif isinstance(a, CreateTemp | OpenPart | WriteBlock | SyncPart | Reread | Finalize | ClearBlocks):
            T = a.transfer_id
            assert T not in n.removed, f"G2: hành động đĩa sau RemoveTemp: {a}"
            extra = None
            if isinstance(a, CreateTemp):
                # `CreateTemp.offer`: app ghi `transfer.json` từ nó (spec-p3 mục 2.2).
                assert a.offer == Offer(transfer_id=T, block_size=BLOCK_SIZE, files=self.files), a
                n.created.add(T)
            elif isinstance(a, OpenPart):
                assert T in n.created, "G2: OpenPart trước CreateTemp"
                n.hw[(T, a.file_index)] = -1
            elif isinstance(a, WriteBlock):
                assert call == "block_received" and payload is not None and payload[0] == a.index, (
                    "WriteBlock không khớp khối vừa nhận",
                    a,
                )
                h = n.hw.get((T, a.file_index), -1)
                assert a.index <= h + 1, f"WriteBlock({a.index}) với hw = {h}"
                n.hw[(T, a.file_index)] = max(h, a.index)
                n.written[(T, a.file_index)].add(a.index)
                n.write_log.append((self.steps, T, a.file_index, a.index))
                extra = payload[1]
            elif isinstance(a, SyncPart):
                for i, _ in a.blocks:
                    assert i in n.written[(T, a.file_index)], "G2: SyncPart chứa khối chưa WriteBlock"
            elif isinstance(a, Reread):
                extra = (a.leaves, bytes(a.leaves))
            elif isinstance(a, Finalize):
                assert call == "reread_done" and list(args[3]) == [], "Finalize chỉ sau reread_done sạch"
            if T not in n.disk_dead:
                n.disk_q.setdefault(T, deque()).append((a, extra))
        elif isinstance(a, RemoveTemp):
            T = a.transfer_id
            if a.keep_record:
                n.kept.add(T)
            n.removed.add(T)
            n.disk_dead.add(T)
            n.disk_q.pop(T, None)
            n.temps.discard(T)
            for key in [k for k in n.parts if k[0] == T]:
                del n.parts[key]
        elif isinstance(a, CheckSpace):
            n.inbox.append(("space_checked", (peer.device_id, a.transfer_id, FREE, None)))
        elif isinstance(a, AskUser):
            assert a.names == tuple(dedupe_offer_names([f.name for f in a.offer.files]))
            accept = not (self.reject_sometimes and self.rng.random() < 0.15)
            n.inbox.append(("decide", (peer.device_id, a.transfer_id, accept)))
        elif isinstance(a, CheckSource):
            n.inbox.append(("source_checked*", (a.transfer_id, a.file_index, a.leaves, bytes(a.leaves))))
        elif isinstance(a, Report):
            ev = a.event
            if hasattr(ev, "key"):
                n.known.add(ev.key)
            if isinstance(ev, TransferEnded):
                assert ev.key not in n.ended, "TransferEnded hai lần"
                n.ended[ev.key] = ev
                n.ended_at[ev.key] = self.now
            elif isinstance(ev, FileVerified):
                n.file_verified.append(ev)
        elif isinstance(a, TombstonesChanged):
            n.tomb_changes += 1
        elif isinstance(a, SuspendTemp):
            raise AssertionError(f"SuspendTemp khi không ai gọi suspend_receiving: {a}")
        else:
            raise AssertionError(f"hành động lạ: {a!r}")

    # ---- kết nối dữ liệu ----

    def open_attempt(self, n: Node, link: Link, msg: DataOpen) -> None:
        cid = link.ids[n.name]
        if self.net_down:
            n.inbox.append(("outgoing_data_failed", (n.peer.device_id, cid, msg)))
            return
        conn = Conn(self, n, n.peer, msg)
        self.conns.append(conn)
        n.data[conn.ids[n.name]] = conn
        n.inbox.append(("outgoing_data", (n.peer.device_id, cid, conn.ids[n.name], msg)))

    def arrive(self, conn: Conn) -> None:
        if self.lose_before_arrival > 0:
            # Kết nối mất trước khi `Listener` bên server giao nó cho engine (ví dụ cắt lúc bắt tay).
            # Bên gửi file là client thì chờ nó ghi xong mọi khung (file nhỏ nằm gọn trong bộ đệm TCP):
            # bên gửi thấy `finished=True`, bên nhận không bao giờ thấy kết nối.
            if conn.client is self.A and not self.sender_done(conn):
                return
            self.lose_before_arrival -= 1
            self.dropped += 1
            conn.arrived = True
            self.abort(conn, by=None)
            return
        conn.arrived = True
        s = conn.server
        did = conn.ids[s.name]
        s.data[did] = conn
        acts = s.engine.incoming_data(conn.client.device_id, did, conn.msg, self.now)
        self.execute(s, acts, "incoming_data")

    def close_local(self, n: Node, did: int) -> None:
        conn = n.data.pop(did, None)
        n.send_jobs.pop(did, None)
        n.recv_jobs.pop(did, None)
        if conn is not None and not conn.closed:
            self.abort(conn, by=n)

    def abort(self, conn: Conn, by: Node | None) -> None:
        conn.closed = True
        conn.frames.clear()
        for m in (conn.client, conn.server):
            if m is by:
                continue
            did = conn.ids[m.name]
            if m.data.get(did) is conn:
                # `finished` = đã ghi xong mọi khung được yêu cầu, bất kể kết nối bị cắt (spec 2.8 bước 3).
                job = m.send_jobs.get(did)
                finished = job is not None and not job[2]
                del m.data[did]
                m.send_jobs.pop(did, None)
                m.recv_jobs.pop(did, None)
                m.inbox.append(("data_closed", (m.peer.device_id, did, finished)))

    def send_step(self, n: Node, did: int) -> None:
        _T, f, remaining = n.send_jobs[did]
        conn = n.data[did]
        if not remaining:
            del n.send_jobs[did]
            del n.data[did]
            conn.eof = True
            n.inbox.append(("data_closed", (n.peer.device_id, did, True)))
            return
        i = remaining[0]
        dg = self.src[(f, i)]
        acts = n.engine.block_read(n.peer.device_id, did, i, dg, self.now)
        self.execute(n, acts, "block_read")
        if did not in n.send_jobs or conn.closed:
            return
        payload = GARBAGE if self.corrupt_file == f else dg
        conn.frames.append((i, dg, payload))
        remaining.pop(0)

    def recv_step(self, n: Node, did: int) -> None:
        conn = n.data[did]
        if conn.frames:
            i, hdr, payload = conn.frames.popleft()
            if payload != hdr:
                self.execute(n, n.engine.block_bad(n.peer.device_id, did, self.now), "block_bad")
                self.close_local(n, did)
                return
            acts = n.engine.block_received(n.peer.device_id, did, i, hdr, self.now)
            self.execute(n, acts, "block_received", payload=(i, payload))
            return
        del n.recv_jobs[did]
        del n.data[did]
        conn.closed = True
        n.inbox.append(("data_closed", (n.peer.device_id, did, False)))

    # ---- đĩa ----

    def disk_step(self, n: Node, T: str) -> None:
        a, extra = n.disk_q[T].popleft()
        peer = n.peer.device_id
        if isinstance(a, ClearBlocks):
            return  # đĩa giả của mô phỏng thường không có `.state`; xem `CrashSim`
        if isinstance(a, CreateTemp):
            if T in n.temps:
                n.disk_q.pop(T, None)
                n.disk_dead.add(T)
                n.inbox.append(("storage_failed", (peer, T, DiskOp.CREATE_TEMP)))
                return
            n.temps.add(T)
            # Hợp đồng 5 (P3-2): `CreateTemp` thành công → `temp_created`, báo trước kết quả của mọi thao
            # tác đứng sau nó trong hàng (cùng hàng sự kiện FIFO của máy).
            n.inbox.append(("temp_created", (peer, T)))
        elif isinstance(a, OpenPart):
            assert T in n.temps
            n.parts[(T, a.file_index)] = {}
        elif isinstance(a, WriteBlock):
            n.parts[(T, a.file_index)][a.index] = extra
        elif isinstance(a, SyncPart):
            report = partial(n.inbox.append, ("synced", (peer, T, a.file_index, a.token)))
            if self.rng.random() < self.slow_sync:
                # `fsync` chậm (đĩa chậm, protocol §4.2): hàng FIFO đĩa của lượt bận tới lúc xong, kết
                # quả báo đúng lúc đó; trong lúc chờ, kết nối dữ liệu vẫn chạy (AC-19: hàng đĩa chạy ở
                # thời điểm ngẫu nhiên).
                done = self.now + self.rng.uniform(*self.sync_delay)
                n.disk_busy[T] = done
                self.at(done, report)
            else:
                report()
        elif isinstance(a, Reread):
            ref, snap = extra
            assert bytes(ref) == snap, "engine sửa leaves trong lúc VERIFYING"
            f = a.file_index
            part = n.parts[(T, f)]
            if self.disk_corrupt is not None and self.disk_corrupt[0] == f and self.corrupted_step is None:
                k = self.disk_corrupt[1]
                part[k] = GARBAGE
                if self.source_change_on_reread:
                    self.src[(f, k)] = hashlib.sha256(b"nguon-da-doi").digest()
                self.corrupted_step = self.steps
            leaves = bytes(a.leaves)
            bad = [i for i in range(len(leaves) // 32) if part.get(i, ZERO) != leaves[32 * i : 32 * i + 32]]
            root = merkle_root(leaves) if not bad else None
            n.inbox.append(("reread_done", (peer, T, f, bad, root)))
        elif isinstance(a, Finalize):
            content = n.parts.pop((T, a.file_index))
            name = a.name
            k = 1
            while name in n.finals:
                name = f"{a.name} ({k})"
                k += 1
            n.finals[name] = (T, a.file_index, content, a.mtime)
            n.finalize_count[(T, a.file_index)] += 1
            n.inbox.append(("finalized", (peer, T, a.file_index, name)))

    # ---- lỗi mạng ----

    def maybe_fault(self) -> None:
        kinds = [k for k in ("outage", "long_outage", "switch", "data_break") if k in self.faults]
        if not kinds or self.rng.random() > self.fault_rate:
            return
        kind = self.rng.choice(kinds)
        if self.fault_budget[kind] <= 0:
            return
        if kind == "data_break":
            # Cắt bất kỳ kết nối dữ liệu nào còn sống, kể cả kết nối chưa tới engine bên server mà bên
            # gửi (client) đã ghi xong mọi khung (AC-19; D7: không treo khi kết nối bị chặn trước khi
            # tới engine bên kia).
            live = [c for c in self.conns if not c.closed]
            if self.break_mid_send:
                # Chỉ cắt kết nối mà bên gửi còn khung chưa ghi (bên gửi là client sẽ mở lại đúng
                # `data_open` cũ) và đã tới engine bên nhận.
                live = [c for c in live if self.sender_has_more(c) and c.arrived]
            if live:
                self.fault_budget[kind] -= 1
                self.abort(self.rng.choice(live), by=None)
            return
        link = self.current_link
        if self.net_down or link is None or not link.alive:
            return
        self.fault_budget[kind] -= 1
        if kind == "switch":
            link.alive = False  # app đóng kết nối cũ; message còn trên đường có thể vẫn tới (bị bỏ qua)
            self.new_link(spread=0.3)
            return
        # Mất mạng: kết nối điều khiển và mọi kết nối dữ liệu chết, message đang trên đường mất.
        link.alive = False
        for q in link.q.values():
            q.clear()
        self.net_down = True
        for c in list(self.conns):
            if not c.closed:
                self.abort(c, by=None)
        grace = transfer.RECONNECT_GRACE  # đọc lúc dùng: test vá được
        if kind == "long_outage":
            d = self.rng.uniform(grace + 1, grace + 30)
            p_lost = 1.0
        else:
            # Mất mạng ngắn: 0,1–25 s như trước (hạn 24 giờ không được biến mọi lần mất mạng ngắn
            # thành hàng giờ), và luôn ngắn hơn hạn kể cả khi hạn bị vá nhỏ.
            d = self.rng.uniform(0.1, min(25.0, grace - 5) if grace > 10 else grace / 2)
            p_lost = 0.7
        for node in self.nodes:
            if self.rng.random() < p_lost:
                t = self.now + self.rng.uniform(0, min(d, 5.0) * 0.99)
                self.at(t, partial(node.link_events.append, ("lost", link)))
        self.at(self.now + d, self.reconnect)

    def sender_done(self, c: Conn) -> bool:
        """Bên gửi file là client của `c` và đã ghi xong mọi khung được yêu cầu."""
        job = c.client.send_jobs.get(c.ids[c.client.name])
        return c.eof or (job is not None and not job[2])

    def sender_has_more(self, c: Conn) -> bool:
        """Bên gửi file là client của `c` và còn khung được yêu cầu chưa ghi."""
        job = c.client.send_jobs.get(c.ids[c.client.name])
        return job is not None and bool(job[2])

    def reconnect(self) -> None:
        self.net_down = False
        self.new_link(spread=0.5)

    def new_link(self, spread: float) -> None:
        link = Link(self, self.rng.choice(self.nodes))
        self.links.append(link)
        self.current_link = link
        for node in self.nodes:
            self.at(self.now + self.rng.uniform(0, spread), partial(node.link_events.append, ("up", link)))

    # ---- kết luận ----

    @property
    def key_a(self) -> TransferKey:
        return TransferKey(RB, Direction.SEND, TID)

    @property
    def key_b(self) -> TransferKey:
        return TransferKey(SA, Direction.RECEIVE, TID)

    def receiver_content_ok(self) -> bool:
        got = {f: c for (T, f, c, _) in self.B.finals.values() if T == TID}
        return all(
            got.get(f) == {i: self.src[(f, i)] for i in range(self.n[f])} for f in range(len(self.files))
        )

    def assert_done(self) -> None:
        ea, eb = self.A.ended.get(self.key_a), self.B.ended.get(self.key_b)
        assert ea is not None and eb is not None, (ea, eb)
        assert (ea.outcome, eb.outcome) == (Outcome.DONE, Outcome.DONE), (ea, eb)
        assert self.receiver_content_ok()
        for f in range(len(self.files)):
            assert self.B.finalize_count[(TID, f)] == 1, f
        assert self.B.temps == set()
        assert TID in self.B.kept, "lượt xong phải giữ `transfer.json` (RemoveTemp.keep_record)"
        assert sorted(e.file_index for e in self.A.file_verified) == list(range(len(self.files)))
        assert sorted(e.file_index for e in self.B.file_verified) == list(range(len(self.files)))
        assert self.A.engine.transfers() == () and self.B.engine.transfers() == ()
        for m in self.A.completes:
            if self.n[m.file_index] == 0:
                assert m.merkle_root == EMPTY_ROOT.hex()


def _blocks():
    return st.lists(st.integers(0, 40), min_size=1, max_size=4)


# ---- AC-19: hai engine, mạng chập chờn, không chèn lỗi --------------------------------------------


@pytest.mark.slow
@SIM
@given(rng=st.randoms(use_true_random=False), nblocks=_blocks(), faults=st.booleans())
def test_two_engines_always_finish_with_identical_content(rng, nblocks, faults):
    sim = Sim(rng, nblocks, faults=frozenset({"outage", "switch", "data_break"}) if faults else frozenset())
    sim.start()
    sim.run()
    sim.assert_done()
    for node in sim.nodes:
        bad = [r for reasons in node.cancels_sent.values() for r in reasons if r != "unknown_transfer"]
        assert bad == [], (node.name, bad)


@pytest.mark.parametrize("first_client", ["A", "B"])
@pytest.mark.parametrize("seed", range(4))
def test_two_engines_finish_with_fixed_seeds(seed, first_client):
    sim = Sim(
        random.Random(seed),
        [1, 17, 0, 33],
        faults=frozenset({"outage", "switch", "data_break"}),
        first_client=first_client,
    )
    sim.start()
    sim.run()
    sim.assert_done()


@pytest.mark.parametrize("seed", range(40))
def test_data_breaks_during_slow_fsync_never_request_written_blocks_again(seed):
    """Bên nhận là server, mọi `fsync` đều chậm (1,2–3 s ảo, dài hơn lịch mở lại 1 s của bên gửi), một
    lần cắt kết nối dữ liệu khi bên gửi còn khung chưa ghi: bên gửi (client) mở lại đúng `data_open` cũ
    sau 1 s (protocol §4.1), trong lúc bên nhận còn chờ `fsync` trước `resume` mới. Bộ mô phỏng kiểm
    mọi `resume` chứa mọi khối đã ghi (spec 3.6, D50; protocol §4.1 gạch cuối, §4.3); hai bên vẫn DONE.
    Với code trước bản sửa (`resume` cũ còn hiệu lực trong lúc chờ `fsync`), khoảng 1/3 số seed đỏ.
    """
    sim = Sim(
        random.Random(seed),
        [40, 33],
        faults=frozenset({"data_break"}),
        first_client="A",
        fault_rate=0.05,
        slow_sync=1.0,
        sync_delay=(1.2, 3.0),
        break_mid_send=True,
        data_breaks=1,
    )
    sim.start()
    sim.run()
    sim.assert_done()


def test_two_engines_finish_one_thousand_files():
    nblocks = [1 if i % 100 == 0 else 0 for i in range(1000)]
    sim = Sim(random.Random(7), nblocks)
    sim.start()
    sim.run(max_steps=200_000)
    sim.assert_done()


def test_resume_scheduled_before_file_verified_does_not_cause_error():
    """Kịch bản cố định của AC-19: kết nối dữ liệu đứt khi file chưa đủ, bên nhận hẹn xin `resume`;
    bên gửi mở lại cùng `data_open`, file xong và `verified`; lịch hẹn bắn sau đó. Không được có
    `cancel` nào.
    """
    S = TransferEngine(SA, receiving=False)  # client của kết nối điều khiển
    R = TransferEngine(RB, receiving=True)
    files = (FileEntry(name="a.bin", size=2 * BLOCK_SIZE, mtime=1), FileEntry(name="b.bin", size=5, mtime=2))
    dg = {(f, i): hashlib.sha256(f"{f}:{i}".encode()).digest() for f, n in ((0, 2), (1, 1)) for i in range(n)}
    log: list = []

    def msgs(acts):
        log.extend(acts)
        return [a.message for a in acts if isinstance(a, SendMessage)]

    def to_r(ms, t):
        out = []
        for m in ms:
            out += R.message(SA, 2, m, t)
        log.extend(out)
        return out

    def to_s(ms, t):
        out = []
        for m in ms:
            out += S.message(RB, 1, m, t)
        log.extend(out)
        return out

    t = 1.0
    S.control_up(RB, 1, True, t)
    R.control_up(SA, 2, False, t)
    r_acts = to_r(msgs(S.send(RB, TID, list(files), t)), t)
    assert any(isinstance(a, CheckSpace) for a in r_acts)
    R.space_checked(SA, TID, FREE, None, t)
    r_acts = R.decide(SA, TID, True, t)
    r_acts += R.temp_created(SA, TID, t)  # P3-2: `accept`/`resume` chỉ sau khi thư mục tạm đã bền
    s_acts = to_s(msgs(r_acts), t)
    od = next(a for a in s_acts if isinstance(a, OpenData))
    msg = od.message
    assert msg == DataOpen(transfer_id=TID, file_index=0, resume_seq=1)
    assert any(isinstance(a, SendBlocks) for a in S.outgoing_data(RB, 1, 11, msg, t))
    assert any(isinstance(a, ReceiveBlocks) for a in R.incoming_data(SA, 21, msg, t))
    # Kết nối dữ liệu đứt trước khi mang được khối nào.
    log.extend(S.data_closed(RB, 11, False, 10.0))
    log.extend(R.data_closed(SA, 21, False, 10.2))
    w_r = R.next_wakeup()
    assert w_r is not None and w_r > 10.2  # bên nhận đã hẹn xin `resume`
    w_s = S.next_wakeup()
    assert w_s is not None and w_s < w_r
    # Bên gửi mở lại đúng `data_open` cũ trước khi lịch của bên nhận bắn.
    t = w_s
    od2 = next(a for a in S.tick(t) if isinstance(a, OpenData))
    assert od2.message == msg
    sb = next(a for a in S.outgoing_data(RB, 1, 12, msg, t) if isinstance(a, SendBlocks))
    assert list(sb.have.missing()) == [0, 1]
    assert any(
        isinstance(a, ReceiveBlocks) for a in R.incoming_data(SA, 22, msg, t)
    )  # `data_open` trùng cùng seq
    r_acts, s_acts = [], []
    for i in range(2):
        s_acts += S.block_read(RB, 12, i, dg[(0, i)], t)
        r_acts += R.block_received(SA, 22, i, dg[(0, i)], t)
    for sp in [a for a in r_acts if isinstance(a, SyncPart)]:
        r_acts += R.synced(SA, TID, 0, sp.token, t)
    cs = next(a for a in s_acts if isinstance(a, CheckSource))
    s_acts = S.source_checked(RB, TID, 0, files[0].size, files[0].mtime, merkle_root(bytes(cs.leaves)), t)
    log.extend(S.data_closed(RB, 12, True, t))
    r_acts = to_r(msgs(s_acts), t)  # complete
    rr = next(a for a in r_acts if isinstance(a, Reread))
    fin = next(
        a for a in R.reread_done(SA, TID, 0, [], merkle_root(bytes(rr.leaves)), t) if isinstance(a, Finalize)
    )
    r_acts = R.finalized(SA, TID, 0, fin.name, t)
    s_acts = to_s(msgs(r_acts), t)  # verified(0) + resume file 1
    msgs(s_acts)
    # Lịch hẹn cũ của bên nhận bắn sau khi file 0 đã `verified`.
    r_tick = R.tick(max(w_r, t))
    s_acts += to_s(msgs(r_tick), max(w_r, t))
    resumes = [m for m in (a.message for a in log if isinstance(a, SendMessage)) if isinstance(m, Resume)]
    assert all(r.file_index == 1 for r in resumes if r.seq > 1)
    # Hoàn tất file 1 theo `resume` mới nhất.
    t = max(w_r, t) + 0.1
    last = resumes[-1]
    m1 = DataOpen(transfer_id=TID, file_index=1, resume_seq=last.seq)
    od = [a for a in s_acts if isinstance(a, OpenData) and a.message == m1]
    assert od, s_acts
    assert any(isinstance(a, SendBlocks) for a in S.outgoing_data(RB, 1, 13, m1, t))
    assert any(isinstance(a, ReceiveBlocks) for a in R.incoming_data(SA, 23, m1, t))
    s_acts = S.block_read(RB, 13, 0, dg[(1, 0)], t)
    r_acts = R.block_received(SA, 23, 0, dg[(1, 0)], t)
    for sp in [a for a in r_acts if isinstance(a, SyncPart)]:
        r_acts += R.synced(SA, TID, 1, sp.token, t)
    cs = next(a for a in s_acts if isinstance(a, CheckSource))
    r_acts = to_r(msgs(S.source_checked(RB, TID, 1, 5, 2, merkle_root(bytes(cs.leaves)), t)), t)
    rr = next(a for a in r_acts if isinstance(a, Reread))
    fin = next(
        a for a in R.reread_done(SA, TID, 1, [], merkle_root(bytes(rr.leaves)), t) if isinstance(a, Finalize)
    )
    r_end = R.finalized(SA, TID, 1, fin.name, t)
    s_end = to_s(msgs(r_end), t)
    log.extend(s_end)
    assert [
        a.event.outcome for a in r_end if isinstance(a, Report) and isinstance(a.event, TransferEnded)
    ] == [Outcome.DONE]
    assert [
        a.event.outcome for a in s_end if isinstance(a, Report) and isinstance(a.event, TransferEnded)
    ] == [Outcome.DONE]
    cancels = [a.message for a in log if isinstance(a, SendMessage) and isinstance(a.message, Cancel)]
    assert cancels == []


# ---- AC-13: file rỗng ---------------------------------------------------------------------------------


@pytest.mark.parametrize("nblocks", [[0, 3, 0, 2, 0], [0, 0, 0], [0], [2, 0]])
@pytest.mark.parametrize("seed", range(3))
def test_empty_files_finish_without_data_connections(nblocks, seed):
    sim = Sim(random.Random(seed), nblocks)
    sim.start()
    sim.run()
    sim.assert_done()  # gồm: không có OpenData/SendBlocks/ReceiveBlocks cho file rỗng; root = SHA-256("")
    empty = [f for f, n in enumerate(sim.n) if n == 0]
    assert {m.file_index for m in sim.A.completes} >= set(empty)


# ---- AC-20: chèn lỗi ----------------------------------------------------------------------------------


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large]
)
@given(
    rng=st.randoms(use_true_random=False),
    nblocks=st.lists(st.integers(1, 12), min_size=1, max_size=3),
    data=st.data(),
)
def test_every_frame_of_a_file_corrupt_cancels_both_sides(rng, nblocks, data):
    sim = Sim(rng, nblocks)
    sim.corrupt_file = data.draw(st.integers(0, len(nblocks) - 1))
    sim.start()
    sim.run()
    ea, eb = sim.A.ended[sim.key_a], sim.B.ended[sim.key_b]
    assert (eb.outcome, eb.reason, eb.by_peer, eb.detail) == (
        Outcome.CANCELLED,
        "error",
        False,
        FailureDetail.CORRUPT_BLOCKS,
    )
    assert (ea.outcome, ea.reason, ea.by_peer) == (Outcome.CANCELLED, "error", True)
    assert eb.file_index == sim.corrupt_file
    assert sorted(e.file_index for e in sim.B.file_verified) == list(range(sim.corrupt_file))
    assert sim.B.temps == set()


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large]
)
@given(
    rng=st.randoms(use_true_random=False),
    nblocks=st.lists(st.integers(1, 20), min_size=1, max_size=3),
    data=st.data(),
)
def test_disk_damage_after_sync_resends_only_that_block(rng, nblocks, data):
    sim = Sim(rng, nblocks)
    f = data.draw(st.integers(0, len(nblocks) - 1))
    k = data.draw(st.integers(0, sim.n[f] - 1))
    sim.disk_corrupt = (f, k)
    sim.start()
    sim.run()
    sim.assert_done()
    assert sim.corrupted_step is not None
    rewritten = {i for (step, T, ff, i) in sim.B.write_log if step > sim.corrupted_step and ff == f}
    assert rewritten == {k}
    after = [r for r in sim.B.resumes if r.file_index == f and r.seq > 0]
    assert any(set(range(sim.n[f])) - _bits(r.bitfield, sim.n[f]) == {k} for r in after)


def _bits(b64: str, n: int) -> set[int]:
    return set(range(n)) - set(Bitfield.from_base64(b64, n).missing())


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large]
)
@given(
    rng=st.randoms(use_true_random=False),
    nblocks=st.lists(st.integers(0, 10), min_size=1, max_size=3),
    data=st.data(),
)
def test_source_mtime_change_cancels_both_sides_with_source_changed(rng, nblocks, data):
    sim = Sim(rng, nblocks)
    f = data.draw(st.integers(0, len(nblocks) - 1))
    sim.src_mtime[f] += 1
    sim.start()
    sim.run()
    ea, eb = sim.A.ended[sim.key_a], sim.B.ended[sim.key_b]
    assert (ea.outcome, ea.reason, ea.by_peer, ea.file_index) == (
        Outcome.CANCELLED,
        "source_changed",
        False,
        f,
    )
    assert (eb.outcome, eb.reason, eb.by_peer) == (Outcome.CANCELLED, "source_changed", True)
    assert sim.B.temps == set()


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large]
)
@given(
    rng=st.randoms(use_true_random=False),
    nblocks=st.lists(st.integers(1, 10), min_size=1, max_size=3),
    data=st.data(),
)
def test_source_block_change_detected_on_resend_cancels_both_sides(rng, nblocks, data):
    sim = Sim(rng, nblocks)
    f = data.draw(st.integers(0, len(nblocks) - 1))
    k = data.draw(st.integers(0, sim.n[f] - 1))
    sim.disk_corrupt = (f, k)
    sim.source_change_on_reread = True
    sim.start()
    sim.run()
    ea, eb = sim.A.ended[sim.key_a], sim.B.ended[sim.key_b]
    assert (ea.outcome, ea.reason, ea.by_peer, ea.file_index) == (
        Outcome.CANCELLED,
        "source_changed",
        False,
        f,
    )
    assert (eb.outcome, eb.reason, eb.by_peer) == (Outcome.CANCELLED, "source_changed", True)


# ---- AC-21: mô phỏng tự do ----------------------------------------------------------------------------


@SIM
@given(rng=st.randoms(use_true_random=False), nblocks=_blocks())
def test_free_simulation_always_ends_consistently(rng, nblocks):
    sim = Sim(
        rng,
        nblocks,
        faults=frozenset({"outage", "long_outage", "switch", "data_break"}),
        user_cancel=True,
        reject_sometimes=True,
    )
    sim.start()
    sim.run()
    ea, eb = sim.A.ended.get(sim.key_a), sim.B.ended.get(sim.key_b)
    assert ea is not None, "bên gửi phải kết thúc"
    if eb is None:
        # Bên nhận chưa từng biết lượt này (offer mất trước khi tới, rồi bên gửi hủy hay hết hạn).
        assert sim.key_b not in sim.B.known and ea.outcome == Outcome.CANCELLED, ea
        return
    for me, other, end in ((sim.A, sim.B, ea), (sim.B, sim.A, eb)):
        if end.by_peer and end.outcome == Outcome.CANCELLED:
            assert end.reason in other.cancels_sent[TID], (me.name, end.reason, other.cancels_sent[TID])
    assert sim.B.temps == set()
    if eb.outcome == Outcome.DONE:
        assert sim.receiver_content_ok()
        if ea.outcome != Outcome.DONE:
            assert ea.outcome == Outcome.CANCELLED and ea.reason in ("expired", "user") and not ea.by_peer, ea
    if ea.outcome == Outcome.DONE:
        assert eb.outcome == Outcome.DONE


# ---- AC-6: hủy lúc offline, bên nhận còn `offer` chờ ----------------------------------------------------


@pytest.mark.parametrize("state", ["asking", "queued"])
def test_offline_send_cancel_reaches_receiver_waiting_offer(state):
    S = TransferEngine(SA, receiving=False)
    R = TransferEngine(RB, receiving=True)
    files = [FileEntry(name="x.bin", size=10, mtime=1)]
    t0 = "6" * 32
    t = 1.0
    S.control_up(RB, 1, True, t)
    R.control_up(SA, 7, False, t)
    tids = [t0, TID] if state == "queued" else [TID]
    for T in tids:
        for a in S.send(RB, T, files, t):
            if isinstance(a, SendMessage):
                R.message(SA, 7, a.message, t)
    head = tids[0]
    asks = [a for a in R.space_checked(SA, head, FREE, None, t) if isinstance(a, AskUser)]
    assert [a.transfer_id for a in asks] == [head]
    # Mất kết nối; người dùng máy gửi hủy lượt TID lúc offline.
    t = 2.0
    S.control_lost(RB, 1, t)
    R.control_lost(SA, 7, t)
    acts = S.cancel(TransferKey(RB, Direction.SEND, TID), t)
    assert [a for a in acts if isinstance(a, SendMessage)] == []
    # Nối lại trong hạn: bên gửi gửi `cancel` trước các `offer`.
    t = 5.0
    up_s = S.control_up(RB, 3, True, t)
    R.control_up(SA, 8, False, t)
    sent = [a.message for a in up_s if isinstance(a, SendMessage)]
    assert sent[0] == Cancel(transfer_id=TID, reason="user")
    r_acts = []
    for m in sent:
        r_acts += R.message(SA, 8, m, t)
    ends = [a.event for a in r_acts if isinstance(a, Report) and isinstance(a.event, TransferEnded)]
    key_r = TransferKey(SA, Direction.RECEIVE, TID)
    assert [(e.key, e.outcome, e.reason, e.by_peer) for e in ends] == [
        (key_r, Outcome.CANCELLED, "user", True)
    ]
    assert [a for a in r_acts if isinstance(a, SendMessage)] == []
    assert R.info(key_r) is None
    # Người dùng máy nhận trả lời muộn / lượt đầu hàng kết thúc: không còn lời hỏi nào cho TID.
    later = R.decide(SA, TID, True, t)
    assert later == []
    if state == "queued":
        later = R.decide(SA, t0, False, t)
    assert not [a for a in later if isinstance(a, AskUser | CheckSpace) and a.transfer_id == TID]


# ---- Kết nối dữ liệu mất trước khi tới engine bên nhận -------------------------------------------------


LOST_FILE = FileEntry(name="a.bin", size=2 * BLOCK_SIZE, mtime=1)
LOST_DG = [hashlib.sha256(f"k:{i}".encode()).digest() for i in range(2)]
T_RESUME = 1.0  # lúc bên nhận gửi `resume` đầu tiên (seq 1)
T_LOST = 2.0  # lúc bên gửi đóng gọn kết nối dữ liệu mà bên nhận không bao giờ thấy


def _sender_finishes_connection_receiver_never_sees():
    """S (gửi) là client của kết nối điều khiển, R (nhận) là server. R gửi `resume` seq 1; S mở kết
    nối dữ liệu 11, ghi xong mọi khung, `complete` tới R, rồi S đóng gọn (`finished=True`, spec 2.8
    bước 3). Kết nối 11 mất trước khi `Listener` bên R giao nó cho engine (ví dụ cắt lúc bắt tay ở
    phía server; file nhỏ nằm gọn trong bộ đệm TCP). Kết nối điều khiển vẫn sống. Trả (S, R).
    """
    S = TransferEngine(SA, receiving=False)
    R = TransferEngine(RB, receiving=True)
    t = T_RESUME
    S.control_up(RB, 1, True, t)
    R.control_up(SA, 2, False, t)
    for a in S.send(RB, TID, [LOST_FILE], t):
        if isinstance(a, SendMessage):
            R.message(SA, 2, a.message, t)
    R.space_checked(SA, TID, FREE, None, t)
    s_acts = []
    for a in R.decide(SA, TID, True, t) + R.temp_created(SA, TID, t):
        if isinstance(a, SendMessage):
            s_acts += S.message(RB, 1, a.message, t)
    msg = next(a for a in s_acts if isinstance(a, OpenData)).message
    assert any(isinstance(a, SendBlocks) for a in S.outgoing_data(RB, 1, 11, msg, t))
    s_acts = S.block_read(RB, 11, 0, LOST_DG[0], t) + S.block_read(RB, 11, 1, LOST_DG[1], t)
    cs = next(a for a in s_acts if isinstance(a, CheckSource))
    root = merkle_root(bytes(cs.leaves))
    for a in S.source_checked(RB, TID, 0, LOST_FILE.size, LOST_FILE.mtime, root, t):
        if isinstance(a, SendMessage):
            R.message(SA, 2, a.message, t)
    S.data_closed(RB, 11, True, T_LOST)
    return S, R


def test_data_connection_lost_before_reaching_receiver_server_does_not_hang():
    """Bên gửi (client) ghi xong mọi khung rồi đóng gọn, nhưng kết nối dữ liệu không bao giờ tới
    engine bên nhận (server). Kết nối điều khiển vẫn sống.

    Nguồn yêu cầu:
    - AC-19: mô hình kênh cho phép ngắt kết nối dữ liệu ở bước ngẫu nhiên; khi không chèn lỗi thì hai
      bên đều DONE, không treo;
    - D7: “không treo khi kết nối bị chặn trước khi tới engine bên kia”;
    - protocol §4.1: “Không mở được kết nối dữ liệu trong 10 s trong khi kết nối điều khiển vẫn sống
      → thử lại theo lịch ở mục 6.2”. Vì vậy lần thử lại đầu tiên phải tới chậm nhất
      `DATA_OPEN_WAIT + DATA_RETRY_MAX` sau `resume` (biên rộng, không ép cách sửa).

    Test không ép bên nào sửa: chỉ đòi một bên tự hẹn giờ rồi xin `resume` mới hay mở lại kết nối.
    """
    S, R = _sender_finishes_connection_receiver_never_sees()
    recovered = False
    now = T_LOST
    deadline = T_RESUME + DATA_OPEN_WAIT + DATA_RETRY_MAX
    while not recovered:
        wakeups = [(w, e) for e in (S, R) if (w := e.next_wakeup()) is not None]
        assert wakeups, "không còn hẹn giờ nào: lượt treo vô hạn dù kết nối điều khiển còn sống"
        now, engine = min(wakeups, key=lambda x: x[0])
        assert now <= deadline, f"lần thử lại đầu tiên tới quá muộn: {now} > {deadline}"
        acts = engine.tick(now)
        recovered = any(
            isinstance(a, OpenData) or (isinstance(a, SendMessage) and isinstance(a.message, Resume))
            for a in acts
        )


def test_receiver_server_retries_without_hot_loop_while_data_connections_never_arrive():
    """Như test trên, nhưng mọi kết nối dữ liệu sau đó cũng mất trước khi tới engine bên nhận. Trong
    120 s ảo, lượt phải tiếp tục thử lại (protocol §4.1: “thử lại theo lịch ở mục 6.2”), nhưng không
    thành vòng nóng: ≤ 15 lần mở (ngưỡng của spec mục 6, kịch bản 10). Bộ thực thi theo spec 2.8:
    hàng sự kiện FIFO, không gọi engine lồng nhau.
    """
    S, R = _sender_finishes_connection_receiver_never_sees()
    ids = itertools.count(100)
    events: deque = deque()
    opens: list[float] = []

    def run(engine: TransferEngine, acts: list, now: float) -> None:
        check_order(acts)
        for a in acts:
            if isinstance(a, SendMessage) and engine is R:
                events.append(partial(S.message, RB, 1, a.message))
            elif isinstance(a, SendMessage):
                events.append(partial(R.message, SA, 2, a.message))
            elif isinstance(a, OpenData):
                assert engine is S and a.control_id == 1, a
                opens.append(now)
                events.append(partial(S.outgoing_data, RB, 1, next(ids), a.message))
            elif isinstance(a, SendBlocks):
                # Ghi xong mọi khung được yêu cầu rồi đóng gọn; kết nối không bao giờ tới R.
                for i in a.have.missing():
                    events.append(partial(S.block_read, RB, a.data_id, i, LOST_DG[i]))
                events.append(partial(S.data_closed, RB, a.data_id, True))

    def owner(fn: partial) -> TransferEngine:
        return fn.func.__self__

    now = T_LOST
    end = T_LOST + 120.0
    while True:
        while events:
            fn = events.popleft()
            run(owner(fn), fn(now), now)
        wakeups = [(w, e) for e in (S, R) if (w := e.next_wakeup()) is not None]
        assert wakeups, "không còn hẹn giờ nào: lượt treo vô hạn dù kết nối điều khiển còn sống"
        now, engine = min(wakeups, key=lambda x: x[0])
        if now > end:
            break
        run(engine, engine.tick(now), now)
    assert S.transfers() and R.transfers()  # kết nối điều khiển còn sống: chưa hết hạn A2
    assert 2 <= len(opens) <= 15, opens


@pytest.mark.parametrize("first_client", ["A", "B"])
@pytest.mark.parametrize("lost", [1, 3])
@pytest.mark.parametrize("seed", range(3))
def test_data_connections_lost_before_reaching_server_engine_still_finish(seed, lost, first_client):
    """AC-19 (mô hình kênh, không treo) và D7, ở dạng mô phỏng cố định: `lost` kết nối dữ liệu đầu
    tiên mất trước khi tới engine bên server. `first_client="A"`: bên gửi là client, đã ghi xong mọi
    khung nên thấy `finished=True`; `"B"`: bên nhận là client (đối chứng). Hai bên vẫn DONE.
    """
    sim = Sim(random.Random(seed), [2, 5, 0, 3], first_client=first_client, lose_before_arrival=lost)
    sim.start()
    sim.run()
    ea, eb = sim.A.ended.get(sim.key_a), sim.B.ended.get(sim.key_b)
    assert ea is not None and eb is not None, (
        f"lượt treo sau {sim.dropped} kết nối mất trước khi tới server",
        sim.A.engine.transfers(),
        sim.B.engine.transfers(),
    )
    sim.assert_done()
    assert sim.dropped == lost


@pytest.mark.parametrize("how", ["source_changed", "user"])
def test_sender_cancel_lost_while_receiver_file_awaits_fsync_still_reaches_receiver(how):
    """Bên gửi hủy lượt khi tưởng kết nối điều khiển còn sống (kết nối thực ra vừa bị thay): `cancel`
    mất, bia mộ không được đánh dấu “chưa gửi”. Bên nhận lúc đó đã ghi đủ khối của file nhưng lô cuối
    còn chờ `fsync`. Trên kết nối mới, bên nhận gửi `accept` rồi phải gửi `resume` cho file hiện tại
    (protocol §6.3 cột “Nhận”; spec 3.3 bước 5 và 3.6: `resume` hoãn vì `fsync` được gửi ở `synced`
    cuối cùng) để bên gửi trả lại `cancel` cùng lý do (§6.3: “`cancel` của bên gửi bị mất … bên nhận
    gửi `resume` trên kết nối mới, bên gửi trả lại `cancel` cùng lý do”). Không thì lượt treo.
    """
    S = TransferEngine(SA, receiving=False)
    R = TransferEngine(RB, receiving=True)
    files = (FileEntry(name="a.bin", size=2 * BLOCK_SIZE, mtime=1),)
    dg = [hashlib.sha256(f"k:{i}".encode()).digest() for i in range(2)]
    t = 1.0
    S.control_up(RB, 1, True, t)
    R.control_up(SA, 2, False, t)
    for a in S.send(RB, TID, list(files), t):
        if isinstance(a, SendMessage):
            R.message(SA, 2, a.message, t)
    R.space_checked(SA, TID, FREE, None, t)
    s_acts = []
    for a in R.decide(SA, TID, True, t) + R.temp_created(SA, TID, t):
        if isinstance(a, SendMessage):
            s_acts += S.message(RB, 1, a.message, t)
    msg = next(a for a in s_acts if isinstance(a, OpenData)).message
    S.outgoing_data(RB, 1, 11, msg, t)
    R.incoming_data(SA, 21, msg, t)
    s_acts = S.block_read(RB, 11, 0, dg[0], t) + S.block_read(RB, 11, 1, dg[1], t)
    r_acts = R.block_received(SA, 21, 0, dg[0], t) + R.block_received(SA, 21, 1, dg[1], t)
    sync = [a for a in r_acts if isinstance(a, SyncPart)]
    assert len(sync) == 1  # đủ khối: lô cuối được đẩy, chưa `synced`
    # Kết nối điều khiển 1/2 vừa bị thay (chưa bên nào biết); bên gửi hủy, `cancel` mất trên kết nối cũ.
    if how == "source_changed":
        cs = next(a for a in s_acts if isinstance(a, CheckSource))
        lost = S.source_checked(
            RB, TID, 0, files[0].size, files[0].mtime + 1, merkle_root(bytes(cs.leaves)), t
        )
    else:
        lost = S.cancel(TransferKey(RB, Direction.SEND, TID), t)
    assert [a.message for a in lost if isinstance(a, SendMessage)] == [Cancel(transfer_id=TID, reason=how)]
    # Kết nối mới 3/4: bên nhận là client.
    t = 2.0
    to_s = [a.message for a in R.control_up(SA, 4, True, t) if isinstance(a, SendMessage)]
    to_r = [a.message for a in S.control_up(RB, 3, False, t) if isinstance(a, SendMessage)]
    to_s += [a.message for a in R.synced(SA, TID, 0, sync[0].token, t) if isinstance(a, SendMessage)]
    assert any(isinstance(m, Resume) for m in to_s), f"bên nhận không gửi resume trên kết nối mới: {to_s}"
    for m in to_s:
        to_r += [a.message for a in S.message(RB, 3, m, t) if isinstance(a, SendMessage)]
    r_end = []
    for m in to_r:
        r_end += R.message(SA, 4, m, t)
    ends = [a.event for a in r_end if isinstance(a, Report) and isinstance(a.event, TransferEnded)]
    assert [(e.outcome, e.reason, e.by_peer) for e in ends] == [(Outcome.CANCELLED, how, True)]


# ---- P3-0 (spec-p3 mục 2.3): chờ nối lại tới `RECONNECT_GRACE` (24 giờ), hết hạn đúng lúc đó ---------

# Giá trị thật, đọc lúc import (trước mọi monkeypatch): protocol §6.2, 24 giờ.
REAL_GRACE = transfer.RECONNECT_GRACE
GRACES = pytest.mark.parametrize("grace", [REAL_GRACE, 2.0], ids=["grace-that", "grace-va-nho"])


class OutageSim(Sim):
    """Sim không chèn lỗi ngẫu nhiên; chỉ mất mạng có chủ đích theo `outages`: danh sách
    `(số khối bên nhận đã ghi, thời lượng)`. Mỗi lần: kết nối điều khiển và mọi kết nối dữ liệu chết,
    message đang trên đường mất, **cả hai máy** thấy mất kết nối ngay lúc đó (`t_lost`); mạng có lại
    sau đúng `thời lượng` giây ảo, kết nối mới báo lên hai bên cùng lúc.

    `skew={"A": d}`: máy A thấy mất kết nối muộn `d` giây (keepalive phát hiện lệch nhau, tới
    `DEAD_AFTER`); trong lúc đó nó vẫn coi kết nối là sống, nhưng mọi thứ nó gửi đều mất. Lớp con đổi
    điều kiện chèn bằng `ready(after)` và làm gì đó ngay trước lúc mất mạng bằng `before_outage()`."""

    def __init__(
        self, *args, outages: list[tuple[int, float]], skew: dict[str, float] | None = None, **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)
        self.outages = list(outages)
        self.skew = dict(skew or {})
        self.t_lost: list[float] = []

    def written_blocks(self) -> int:
        return sum(len(v) for (T, _f), v in self.B.written.items() if T == TID)

    def ready(self, after: int) -> bool:
        return self.written_blocks() >= after

    def before_outage(self) -> None:
        pass

    def maybe_fault(self) -> None:
        if not self.outages or self.net_down:
            return
        after, duration = self.outages[0]
        link = self.current_link
        if not self.ready(after) or link is None or not link.alive:
            return
        if self.A.current is not link or self.B.current is not link:
            return  # kết nối mới chưa báo lên đủ hai bên
        self.outages.pop(0)
        self.before_outage()
        link.alive = False
        for q in link.q.values():
            q.clear()
        self.net_down = True
        for c in list(self.conns):
            if not c.closed:
                self.abort(c, by=None)
        self.t_lost.append(self.now)
        for node in self.nodes:
            self.at(
                self.now + self.skew.get(node.name, 0.0), partial(node.link_events.append, ("lost", link))
            )
        self.at(self.now + duration, self.back)

    def back(self) -> None:
        self.net_down = False
        self.new_link(spread=0.0)


def _assert_expired_exactly_at(sim: OutageSim, deadline: float) -> None:
    """Hai bên `expired` (không do máy kia) đúng lúc `deadline`; bên nhận không còn thư mục tạm.
    Bên nhận chưa từng biết lượt (offer mất cùng lần mất mạng) thì chỉ xét bên gửi."""
    sides = [(sim.A, sim.key_a)]
    if sim.key_b in sim.B.known:
        sides.append((sim.B, sim.key_b))
    for node, key in sides:
        end = node.ended.get(key)
        assert end is not None, f"{node.name}: lượt chưa kết thúc"
        assert (end.outcome, end.reason, end.by_peer) == (Outcome.CANCELLED, "expired", False), end
        assert node.ended_at[key] == pytest.approx(deadline, abs=1e-6), (
            f"{node.name}: hết hạn lúc {node.ended_at[key]!r}, phải đúng {deadline!r}"
        )
    assert sim.B.temps == set(), "bên nhận hết hạn phải xóa thư mục tạm"
    if TID in sim.B.created:
        assert TID in sim.B.removed


@GRACES
@pytest.mark.parametrize("first_client", ["A", "B"])
@pytest.mark.parametrize("seed", range(3))
def test_outage_just_shorter_than_grace_resumes_and_finishes(monkeypatch, grace, first_client, seed):
    """Mất mạng ở 85 % và có lại ngay trước hạn (`RECONNECT_GRACE` − 1 ms): không bên nào hết hạn
    sớm; hai bên đồng bộ lại và DONE, nội dung khớp, không có `cancel` nào ngoài `unknown_transfer`."""
    monkeypatch.setattr(transfer, "RECONNECT_GRACE", grace)
    sim = OutageSim(random.Random(seed), [40], first_client=first_client, outages=[(34, grace - 0.001)])
    sim.start()
    sim.run()
    assert len(sim.t_lost) == 1, "chưa chèn được lần mất mạng (lượt xong trước 85 %?)"
    sim.assert_done()
    back_at = sim.t_lost[0] + grace - 0.001
    assert sim.A.ended_at[sim.key_a] >= back_at and sim.B.ended_at[sim.key_b] >= back_at
    for node in sim.nodes:
        assert [r for r in node.cancels_sent[TID] if r != "unknown_transfer"] == [], node.name


@GRACES
@pytest.mark.parametrize("first_client", ["A", "B"])
@pytest.mark.parametrize("seed", range(3))
def test_outage_longer_than_grace_expires_both_sides_exactly_at_grace(monkeypatch, grace, first_client, seed):
    """Mất mạng ở 85 % và lâu hơn hạn: cả hai bên kết thúc `expired` (không do máy kia) **đúng** lúc
    `t_lost + RECONNECT_GRACE`, không sớm hơn và không đợi tới lúc có mạng lại; bên nhận xóa thư mục tạm."""
    monkeypatch.setattr(transfer, "RECONNECT_GRACE", grace)
    sim = OutageSim(random.Random(seed), [40], first_client=first_client, outages=[(34, grace + 60.0)])
    sim.start()
    sim.run()
    assert len(sim.t_lost) == 1
    _assert_expired_exactly_at(sim, sim.t_lost[0] + grace)


@GRACES
def test_each_outage_gets_a_full_grace_from_its_own_start(monkeypatch, grace):
    """Hai lần mất mạng, mỗi lần 90 % hạn (tổng thời gian mất mạng > hạn): hạn tính lại từ đầu ở mỗi
    lần mất kết nối (protocol §6.2: “kể từ lúc mất kết nối”), nên lượt vẫn DONE."""
    monkeypatch.setattr(transfer, "RECONNECT_GRACE", grace)
    sim = OutageSim(random.Random(11), [40], first_client="A", outages=[(10, grace * 0.9), (30, grace * 0.9)])
    sim.start()
    sim.run()
    assert len(sim.t_lost) == 2
    sim.assert_done()


@GRACES
@settings(SIM, max_examples=60)
@given(
    rng=st.randoms(use_true_random=False),
    frac=st.floats(min_value=0.0, max_value=2.0, allow_nan=False),
    after=st.integers(0, 39),
)
def test_outage_outcome_depends_only_on_grace(grace, rng, frac, after):
    """Mất mạng ở vị trí bất kỳ, thời lượng `frac × RECONNECT_GRACE`: ngắn hơn hạn (cách hạn > 1 ms)
    → DONE, nội dung khớp; dài hơn hạn → `expired` đúng lúc `t_lost + RECONNECT_GRACE`."""
    duration = frac * grace
    assume(abs(duration - grace) > 0.001)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transfer, "RECONNECT_GRACE", grace)
        sim = OutageSim(rng, [40], outages=[(after, duration)])
        sim.start()
        sim.run()
    if not sim.t_lost or duration < grace:
        sim.assert_done()
    else:
        _assert_expired_exactly_at(sim, sim.t_lost[0] + grace)


# ---- P3-0 (soat-code): lời cuối bị mất, hai bên thấy mất kết nối lệch nhau, nối lại sát hạn ----------
#
# Bên đã kết thúc lượt (hủy, từ chối, hay đã `verified` file cuối) nhớ lượt đó trong bảng nhớ
# (`REMEMBER_TTL`); bên kia còn chờ nối lại tới `RECONNECT_GRACE` **tính từ lúc nó thấy mất kết nối**,
# mà lúc đó có thể muộn tới `DEAD_AFTER` (keepalive). Nối lại ngay trước hạn của bên chờ thì bên đã kết
# thúc phải còn nhớ, để trả đúng `cancel{user}` / `reject` / `verified` (protocol §6.3), chứ không coi
# là lượt lạ (hỏi lại người dùng, hay `cancel{unknown_transfer}`).

SKEW = DEAD_AFTER  # bên chờ thấy mất kết nối muộn nhất có thể
REJOIN_AFTER = REAL_GRACE + DEAD_AFTER / 2  # tính từ lúc lời cuối bị mất; trước hạn của bên chờ


class LostLastWordSim(OutageSim):
    """`case`: `"cancel-receiver"` / `"cancel-sender"` (hủy ở 85 %), `"reject"` (bên nhận từ chối lúc
    hỏi), `"verified"` (`verified` của file cuối). Message đó bị mất cùng lần mất mạng ngay sau khi gửi;
    bên kia (bên chờ) thấy mất kết nối muộn `SKEW` giây; mạng có lại sau `REJOIN_AFTER` giây."""

    def __init__(self, rng: random.Random, nblocks: list[int], case: str, **kwargs) -> None:
        self.case = case
        self.sent_log: list[tuple[str, object]] = []
        self.asks: list[float] = []  # thời điểm mỗi AskUser (bên nhận)
        waiting = "B" if case == "cancel-sender" else "A"
        super().__init__(rng, nblocks, outages=[(34, REJOIN_AFTER)], skew={waiting: SKEW}, **kwargs)

    def do(self, n: Node, a, call: str, args: tuple, payload) -> None:
        if isinstance(a, SendMessage):
            self.sent_log.append((n.name, a.message))
        if isinstance(a, AskUser):
            self.asks.append(self.now)
            if self.case == "reject":
                n.inbox.append(("decide", (n.peer.device_id, a.transfer_id, False)))
                return
        super().do(n, a, call, args, payload)

    def ready(self, after: int) -> bool:
        if self.case == "reject":
            return any(name == "B" and isinstance(m, Reject) for name, m in self.sent_log)
        if self.case == "verified":
            last = len(self.files) - 1
            return any(
                name == "B" and isinstance(m, Verified) and m.file_index == last for name, m in self.sent_log
            )
        return super().ready(after)

    def before_outage(self) -> None:
        if self.case == "cancel-receiver":
            self.cancel_by_user(self.B)
        elif self.case == "cancel-sender":
            self.cancel_by_user(self.A)


@pytest.mark.parametrize("first_client", ["A", "B"])
@pytest.mark.parametrize("seed", range(2))
@pytest.mark.parametrize("case", ["cancel-receiver", "cancel-sender", "reject", "verified"])
def test_last_word_lost_before_skewed_outage_is_repeated_when_peer_rejoins_near_grace(
    case, seed, first_client
):
    sim = LostLastWordSim(random.Random(seed), [40], case, first_client=first_client)
    sim.start()
    sim.run()
    assert len(sim.t_lost) == 1, "chưa chèn được lần mất mạng"
    t_lost = sim.t_lost[0]
    rejoin = t_lost + REJOIN_AFTER
    asks_after = [t for t in sim.asks if t > t_lost]
    assert asks_after == [], f"bên nhận hỏi lại người dùng sau khi nối lại (lượt bị coi là mới): {asks_after}"
    ea, eb = sim.A.ended.get(sim.key_a), sim.B.ended.get(sim.key_b)
    assert ea is not None and eb is not None, (ea, eb)
    if case == "cancel-receiver":
        assert (eb.outcome, eb.reason, eb.by_peer) == (Outcome.CANCELLED, "user", False), eb
        assert (ea.outcome, ea.reason, ea.by_peer) == (Outcome.CANCELLED, "user", True), ea
        waiting, wkey = sim.A, sim.key_a
    elif case == "cancel-sender":
        assert (ea.outcome, ea.reason, ea.by_peer) == (Outcome.CANCELLED, "user", False), ea
        assert (eb.outcome, eb.reason, eb.by_peer) == (Outcome.CANCELLED, "user", True), eb
        waiting, wkey = sim.B, sim.key_b
    elif case == "reject":
        assert (ea.outcome, ea.by_peer) == (Outcome.REJECTED, True), ea
        waiting, wkey = sim.A, sim.key_a
    else:
        sim.assert_done()  # gồm: mỗi file chỉ được hoàn tất một lần, không có lượt nhận mới
        waiting, wkey = sim.A, sim.key_a
    # Bên chờ chỉ kết thúc khi đã nối lại (nhận lời cuối được nhắc lại), không hết hạn trước đó.
    assert waiting.ended_at[wkey] >= rejoin, (waiting.name, waiting.ended_at[wkey], rejoin)
    assert sim.B.temps == set()
    unknown = [r for node in sim.nodes for r in node.cancels_sent[TID] if r == "unknown_transfer"]
    assert unknown == [], "bên đã kết thúc quên lượt trước khi bên kia thôi nối lại"


def test_remember_ttl_outlives_peer_reconnect_window_with_keepalive_skew():
    """Bảng nhớ phải sống lâu hơn thời gian bên kia còn có thể nối lại: hạn chờ nối lại tính từ lúc
    bên kia thấy mất kết nối, muộn tới `DEAD_AFTER` (keepalive) so với bên này."""
    assert transfer.REMEMBER_TTL > transfer.RECONNECT_GRACE + DEAD_AFTER


# ---- P3-2: mô hình sập của bên nhận (spec-p3 mục 2.3 P3-2, “Xong khi”) -------------------------------

WRITE_QUEUE = 8  # protocol §4.3: hàng ghi đĩa có giới hạn 8 khối → backpressure
MAX_RESENT_PER_CRASH = 25  # protocol §4.3: khối đã nhận mà chưa đánh dấu ≤ 16 + 8 + 1


class CrashSim(Sim):
    """`Sim` có thêm lỗi “tiến trình nhận bị giết” (B sập) và đĩa giả có `fsync`.

    Đĩa giả của B (sống qua lần sập, giữ ở `Sim`):
    - `records[T]`: thư mục tạm + `transfer.json` (offer lấy từ `CreateTemp.offer`, `verified`, đã xong
      chưa, lúc hoạt động cuối); bền từ lúc `CreateTemp` chạy xong, cũng là lúc `temp_created` được báo
      (trước đó B chưa gửi `accept`/`resume` nào, spec-p3 “`temp_created`”); mất khi `RemoveTemp` không
      `keep_record`.
    - `.part`: phần bền (`durable`) và phần chưa `fsync` (`volatile`). `SyncPart` chạy xong mới làm bền
      mọi khối đã ghi trước nó và nối `MARK` vào `.state` (`marks`), rồi mới báo `synced` (bất biến bên
      gọi: `synced` chỉ sau MARK bền). `ClearBlocks` nối `CLEAR` theo FIFO đĩa, không `fsync` riêng.
      `disk_corrupt` làm hỏng khối trên cả phần bền (còn sau khi sập).
    - Sập: hàng đĩa mất; mỗi khối chưa `fsync` còn hay mất ngẫu nhiên (trang bộ nhớ đệm có thể đã được
      ghi); `SyncPart` đang chạy dở thì `MARK` của nó còn trọn hay mất (bản ghi cụt bị bỏ khi đọc lại);
      các `CLEAR` sau `MARK` bền cuối có thể mất (một đoạn đuôi); `cut_durable` > 0 cắt thêm tới chừng
      ấy bản ghi cuối đã bền (`.state` hỏng đuôi, mvp N5).
    - Mở lại: dựng `RestoredReceive` / `RestoredDone` từ đĩa như app (P3-3a): `bits`, `leaves` từ việc
      đọc lại `.state`, `part_blocks` = số khối nằm trọn trong `.part` bền, tuổi = lúc này − hoạt động cuối.

    Backpressure: B chỉ đọc khung tiếp khi hàng đĩa của lượt còn < `WRITE_QUEUE` `WriteBlock`.

    Mô hình này giữ `.state` ở mức bản ghi; mã hóa byte và cắt cụt ở byte ngẫu nhiên được kiểm riêng ở
    `tests/core/test_resume_state.py` (`StateReplay`).
    """

    def __init__(
        self,
        rng: random.Random,
        nblocks: list[int],
        *,
        crashes: int = 1,
        crash_rate: float = 0.01,
        cut_durable: int = 0,
        crash_when=None,
        **kw,
    ) -> None:
        kw.setdefault("slow_sync", 0.3)
        kw.setdefault("sync_delay", (0.2, 1.5))
        super().__init__(rng, nblocks, **kw)
        self.crash_budget = crashes
        self.crash_rate = crash_rate
        self.cut_durable = cut_durable
        self.crash_when = crash_when  # điều kiện sập cố định (thay cho sập ngẫu nhiên)
        self.b_down = False
        self.records: dict[str, dict] = {}
        self.durable: dict[tuple[str, int], dict[int, bytes]] = {}
        self.volatile: dict[tuple[str, int], dict[int, bytes]] = {}
        self.marks: dict[tuple[str, int], list[tuple[str, object]]] = {}
        self.inflight: dict[str, SyncPart] = {}
        self.crash_steps: list[int] = []
        self.lost_per_crash: list[int] = []
        self.restored: list[tuple[int, set[int]]] = []  # (file, bit đã khôi phục) mỗi lần mở lại
        self.restore_steps: list[int] = []
        self.b_resumes: list[tuple[int, Resume]] = []  # (bước, resume) B đã gửi, qua mọi lần sập
        self.offer_replies: list[list[str]] = []  # B trả lời `offer` gửi lại sau khi mở lại
        self.clears_done = 0  # số `ClearBlocks` hàng đĩa đã chạy
        self.cleared_lost = 0  # số bản ghi `CLEAR` mất vì sập
        self.asks = 0  # số lần B hỏi người dùng (hỏi lại sau sập trước `temp_created`)

    # ---- đĩa có fsync ----

    def touch(self, T: str) -> None:
        rec = self.records.get(T)
        if rec is not None:
            rec["last"] = self.now

    def disk_step(self, n: Node, T: str) -> None:
        if n is not self.B:
            super().disk_step(n, T)
            return
        a, _ = n.disk_q[T][0]
        if isinstance(a, SyncPart):
            n.disk_q[T].popleft()
            self.inflight[T] = a
            if self.rng.random() < self.slow_sync:
                done = self.now + self.rng.uniform(*self.sync_delay)
                n.disk_busy[T] = done
                self.at(done, partial(self.sync_done, n, T, a))
            else:
                self.sync_done(n, T, a)
            return
        if isinstance(a, ClearBlocks):
            n.disk_q[T].popleft()
            self.marks.setdefault((T, a.file_index), []).append(("clear", a.indices))
            self.clears_done += 1
            self.touch(T)
            return
        existed = T in n.temps
        corrupted = self.corrupted_step
        super().disk_step(n, T)
        if isinstance(a, CreateTemp) and not existed and T in n.temps:
            # P3-3a: `create_temp` ghi `transfer.json` và fsync thư mục trước khi trả về; `temp_created`
            # được báo ngay sau (xem `Sim.disk_step`). Sập trước bước này: không có gì trên đĩa.
            self.records[T] = {"offer": a.offer, "verified": 0, "done": False, "last": self.now}
        elif isinstance(a, Reread) and corrupted is None and self.corrupted_step is not None:
            # Hỏng khối nằm trên đĩa thật (phần bền), không chỉ trong bản nhìn thấy: còn sau khi sập.
            f, k = self.disk_corrupt  # type: ignore[misc]
            if k in self.durable.get((T, f), {}):
                self.durable[(T, f)][k] = GARBAGE
        elif isinstance(a, OpenPart):
            key = (T, a.file_index)
            self.durable[key] = {}
            self.volatile[key] = {}
            self.marks[key] = []  # header `.state` (bất biến bên gọi: `OpenPart` tạo `.part` + header)
        elif isinstance(a, WriteBlock):
            self.volatile[(T, a.file_index)][a.index] = n.parts[(T, a.file_index)][a.index]
        elif isinstance(a, Finalize):
            rec = self.records[T]
            rec["verified"] += 1
            rec["done"] = rec["verified"] == len(self.files)
            for d in (self.durable, self.volatile, self.marks):
                d.pop((T, a.file_index), None)
        self.touch(T)

    def sync_done(self, n: Node, T: str, a: SyncPart) -> None:
        if n is not self.B or self.inflight.get(T) is not a:
            return  # tiến trình đã sập: không báo gì
        del self.inflight[T]
        key = (T, a.file_index)
        self.durable[key].update(self.volatile[key])
        self.volatile[key].clear()
        self.marks[key].append(("mark", dict(a.blocks)))
        self.touch(T)
        n.inbox.append(("synced", (n.peer.device_id, T, a.file_index, a.token)))

    def do(self, n: Node, a, call: str, args: tuple, payload) -> None:
        if n is self.B and isinstance(a, SendMessage) and isinstance(a.message, Resume):
            self.b_resumes.append((self.steps, a.message))
        if n is self.B and isinstance(a, AskUser):
            self.asks += 1
        if n is self.B and isinstance(a, RemoveTemp):
            T = a.transfer_id
            if a.keep_record and T in self.records:
                self.records[T]["done"] = True
                self.touch(T)
            else:
                self.records.pop(T, None)
            for d in (self.durable, self.volatile, self.marks):
                for key in [k for k in d if k[0] == T]:
                    del d[key]
            self.inflight.pop(T, None)
        super().do(n, a, call, args, payload)

    # ---- backpressure, kết nối tới tiến trình đã chết ----

    def tasks(self) -> list:
        return [t for t in super().tasks() if not self.blocked(t)]

    def blocked(self, task) -> bool:
        if getattr(task.func, "__name__", "") != "recv_step" or task.args[0] is not self.B:
            return False
        job = self.B.recv_jobs.get(task.args[1])
        if job is None:
            return False
        q = self.B.disk_q.get(job[0], ())
        return sum(isinstance(a, WriteBlock) for a, _ in q) >= WRITE_QUEUE

    def open_attempt(self, n: Node, link: Link, msg: DataOpen) -> None:
        if n not in self.nodes:
            return  # tiến trình đã chết
        if self.b_down or not link.alive:
            n.inbox.append(("outgoing_data_failed", (n.peer.device_id, link.ids[n.name], msg)))
            return
        super().open_attempt(n, link, msg)

    def arrive(self, conn: Conn) -> None:
        if conn.client not in self.nodes or conn.server not in self.nodes:
            conn.arrived = True
            self.abort(conn, by=None)
            return
        super().arrive(conn)

    def execute(self, n: Node, acts: list, call: str, args: tuple = (), payload: tuple | None = None) -> None:
        if n is self.B and call == "message" and isinstance(args[0], Offer) and self.restore_steps:
            self.offer_replies.append([type(a.message).__name__ for a in acts if isinstance(a, SendMessage)])
        super().execute(n, acts, call, args, payload)

    # ---- sập và mở lại ----

    def maybe_fault(self) -> None:
        if self.can_crash():
            due = (
                self.crash_when(self) if self.crash_when is not None else self.rng.random() < self.crash_rate
            )
            if due:
                self.crash()
                return
        super().maybe_fault()

    def can_crash(self) -> bool:
        return (
            self.crash_budget > 0
            and not self.b_down
            and not self.net_down
            and self.current_link is not None
            and self.B.ended.get(self.key_b) is None  # sau khi B xong: xem test riêng của `RestoredDone`
        )

    def crash(self) -> None:
        self.crash_budget -= 1
        self.crash_steps.append(self.steps)
        old = self.B
        self.b_down = True
        link = self.current_link
        assert link is not None
        link.alive = False
        for q in link.q.values():
            q.clear()
        self.current_link = None
        self.nodes = [self.A]
        for c in list(self.conns):
            if not c.closed:
                self.abort(c, by=None)
        self.at(self.now + self.rng.uniform(0, 3.0), partial(self.A.link_events.append, ("lost", link)))
        # `fsync` `.part` + MARK đang chạy dở: còn trọn hay mất.
        for T, sp in list(self.inflight.items()):
            key = (T, sp.file_index)
            if key in self.marks and self.rng.random() < 0.5:
                self.durable[key].update(self.volatile[key])
                self.volatile[key].clear()
                self.marks[key].append(("mark", dict(sp.blocks)))
        self.inflight.clear()
        # Khối chưa `fsync`: mỗi khối còn hay mất.
        for key, vol in self.volatile.items():
            for i, dg in vol.items():
                if self.rng.random() < 0.5:
                    self.durable[key][i] = dg
            vol.clear()
        # `CLEAR` không có `fsync` riêng: chỉ bền khi `MARK` sau nó đã `fsync` `.state`. `CLEAR` đứng sau
        # `MARK` bền cuối cùng có thể mất (một đoạn đuôi ngẫu nhiên); khối của nó “sống lại” và lượt đọc
        # lại cuối bắt lại (QĐ2).
        for log in self.marks.values():
            tail = 0
            while tail < len(log) and log[len(log) - 1 - tail][0] == "clear":
                tail += 1
            drop = self.rng.randint(0, tail)
            if drop:
                self.cleared_lost += drop
                del log[len(log) - drop :]
        if self.cut_durable:
            for log in self.marks.values():
                del log[max(0, len(log) - self.rng.randint(0, self.cut_durable)) :]
        written = {k: set(v) for k, v in old.written.items() if k[0] == TID}
        downtime = self.rng.uniform(0.5, min(5.0, transfer.RECONNECT_GRACE / 6))
        self.at(self.now + downtime, partial(self.restart, old, written))

    def replay(self, key: tuple[str, int], n: int) -> tuple[set[int], bytearray]:
        """Đọc lại `.state` giả: bit và `leaves` sau mọi `MARK` / `CLEAR`."""
        on: set[int] = set()
        leaves = bytearray(32 * n)
        for kind, data in self.marks.get(key, []):
            if kind == "mark":
                assert isinstance(data, dict)
                for i, dg in data.items():
                    on.add(i)
                    leaves[32 * i : 32 * i + 32] = dg
            elif data is None:
                on.clear()
            else:
                assert isinstance(data, tuple)
                on -= set(data)
        return on, leaves

    def restart(self, old: Node, written: dict[tuple[str, int], set[int]]) -> None:
        new = Node(self, "B", RB, receiving=True)
        for attr in ("finals", "finalize_count", "ended", "cancels_sent", "completes", "resumes", "known"):
            setattr(new, attr, getattr(old, attr))
        new.file_verified = list(old.file_verified)
        new.removed = set(old.removed)
        new.kept = set(old.kept)
        # Có `transfer.json` bền ⇔ `CreateTemp` đã chạy xong: lượt khôi phục coi như đã `temp_created`.
        new.temp_ok = set(self.records)
        receives, done = [], []
        lost = 0
        for T, rec in self.records.items():
            age = self.now - rec["last"]
            if rec["done"]:
                done.append(RestoredDone(device_id=SA, offer=rec["offer"], age=age))
                continue
            f = rec["verified"]
            key = (T, f)
            n = self.n[f]
            # App tạo `.part` + header khi file hiện tại chưa có (sập ngay sau `CreateTemp` hay `Finalize`).
            self.durable.setdefault(key, {})
            self.volatile.setdefault(key, {})
            self.marks.setdefault(key, [])
            on, leaves = self.replay(key, n)
            part = self.durable[key]
            part_blocks = max(part) + 1 if part else 0
            assert all(i < part_blocks for i in on), "mô hình đĩa sai: MARK cho khối chưa bền"
            bits = Bitfield(n)
            for i in on:
                bits.set(i)
            receives.append(
                RestoredReceive(
                    device_id=SA,
                    offer=rec["offer"],
                    file_index=f,
                    bits=bits,
                    leaves=leaves,
                    part_blocks=part_blocks,
                    age=age,
                )
            )
            new.temps.add(T)
            new.created.add(T)
            new.parts[key] = dict(part)
            new.hw[key] = part_blocks - 1
            new.written[key] = set(on)
            self.restored.append((f, set(on)))
            if T == TID:
                lost += len(written.get(key, set()) - on)
                lost += sum(len(v) for (_, g), v in written.items() if g > f)
        if TID not in self.records:
            lost += sum(len(v) for v in written.values())  # chưa có thư mục tạm: mất mọi khối đã nhận
        self.lost_per_crash.append(lost)
        self.B = new
        self.A.peer, new.peer = new, self.A
        self.nodes = [self.A, new]
        acts = new.engine.restore(receives=tuple(receives), tombstones=(), done=tuple(done), now=self.now)
        if receives:
            assert acts == [Report(StatusChanged(self.key_b, TransferStatus.RECONNECTING))], acts
        else:
            assert acts == [], acts
        self.execute(new, acts, "restore")
        self.restore_steps.append(self.steps)
        self.b_down = False
        self.new_link(spread=0.5)

    # ---- kết luận ----

    def assert_finished(self) -> None:
        ea, eb = self.A.ended.get(self.key_a), self.B.ended.get(self.key_b)
        assert ea is not None and ea.outcome == Outcome.DONE, ea
        if eb is None:
            # B sập sau khi mọi `Finalize` đã bền mà trước `finalized`: mở lại bằng `RestoredDone`, nên
            # không engine nào của B phát `TransferEnded`; `transfer.json` ghi lượt đã xong.
            assert self.records[TID]["done"], self.records
        else:
            assert eb.outcome == Outcome.DONE, eb
            assert TID in self.B.kept
        assert self.receiver_content_ok(), "file cuối khác nguồn"
        for f in range(len(self.files)):
            assert self.B.finalize_count[(TID, f)] == 1, f
        assert self.B.temps == set()
        assert self.A.engine.transfers() == () and self.B.engine.transfers() == ()
        for node in (self.A, self.B):
            bad = [r for reasons in node.cancels_sent.values() for r in reasons if r != "unknown_transfer"]
            assert bad == [], (node.name, bad)


def long_grace():
    """Giữ `RECONNECT_GRACE` ≥ 1 giờ trong mô hình sập (hằng của sản phẩm là 24 giờ, nên thường không
    đổi gì), để “mất mạng rồi sập” không vượt hạn nối lại kể cả khi hằng bị vá nhỏ. Hết hạn của lượt
    khôi phục có test riêng (`test_transfer_restore.py`).
    """
    from teleport.core import transfer

    mp = pytest.MonkeyPatch()
    mp.setattr(transfer, "RECONNECT_GRACE", max(transfer.RECONNECT_GRACE, 3600.0))
    return mp


CRASH = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


@pytest.mark.slow
@CRASH
@given(
    rng=st.randoms(use_true_random=False),
    nblocks=st.lists(st.integers(0, 60), min_size=1, max_size=3),
    crashes=st.integers(1, 3),
    net_faults=st.booleans(),
)
def test_receiver_crash_model_ends_with_source_content_and_bounded_resend(rng, nblocks, crashes, net_faults):
    """Mô hình sập (spec-p3 P3-2 “Xong khi”; ke_hoach GĐ3 (c), (d)): B bị giết ở bước ngẫu nhiên, kể cả
    giữa `decide` và `temp_created`; đĩa mất phần chưa `fsync`, MARK đang ghi bị cụt, CLEAR cuối có thể
    mất; mở lại → `restore` → nối lại. Không lần sập nào sinh `cancel` (B sập trước khi bền thì được hỏi
    lại, mô phỏng tự nhận); hai bên DONE, file khớp nguồn; mỗi lần sập phải gửi lại ≤ 25 khối đã nhận
    (protocol §4.3, mvp N4)."""
    sim = CrashSim(
        rng,
        nblocks,
        crashes=crashes,
        crash_rate=0.02,
        faults=frozenset({"outage", "switch", "data_break"}) if net_faults else frozenset(),
    )
    mp = long_grace()
    try:
        sim.start()
        sim.run(max_steps=60_000)
    finally:
        mp.undo()
    sim.assert_finished()
    assert all(lost <= MAX_RESENT_PER_CRASH for lost in sim.lost_per_crash), sim.lost_per_crash


@settings(
    max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large]
)
@given(
    rng=st.randoms(use_true_random=False),
    nblocks=st.lists(st.integers(1, 50), min_size=1, max_size=2),
    cut=st.integers(1, 4),
    data=st.data(),
)
def test_receiver_crash_with_damaged_state_tail_still_ends_with_source_content(rng, nblocks, cut, data):
    """Như trên, cộng `.state` hỏng đuôi: mất tới `cut` bản ghi cuối đã bền, và có khi một khối bị hỏng
    trên đĩa (nên có `CLEAR` trong nhật ký). Bỏ `MARK` làm thiếu bit (gửi lại nhiều hơn); bỏ `CLEAR` làm
    bit của khối hỏng sống lại, và lượt đọc lại cuối bắt lại nó (QĐ2). Vì vậy file cuối vẫn đúng; biến thể
    này không kiểm trần 25 khối."""
    sim = CrashSim(rng, nblocks, crashes=2, crash_rate=0.02, cut_durable=cut)
    if data.draw(st.booleans()):
        f = data.draw(st.integers(0, len(nblocks) - 1))
        sim.disk_corrupt = (f, data.draw(st.integers(0, sim.n[f] - 1)))
    sim.start()
    sim.run(max_steps=60_000)
    sim.assert_finished()


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large]
)
@given(
    rng=st.randoms(use_true_random=False),
    nblocks=st.lists(st.integers(1, 50), min_size=1, max_size=2),
    data=st.data(),
)
def test_receiver_crash_with_corrupt_block_ends_with_source_content_and_bounded_resend(rng, nblocks, data):
    """Mô hình sập có một khối hỏng trên đĩa sau khi đã đánh dấu (mvp F7): lượt đọc lại phát `ClearBlocks`,
    `CLEAR` có thể mất khi sập ngay sau đó. File cuối vẫn khớp nguồn, mỗi lần sập ≤ 25 khối đã nhận."""
    sim = CrashSim(rng, nblocks, crashes=2, crash_rate=0.03)
    f = data.draw(st.integers(0, len(nblocks) - 1))
    sim.disk_corrupt = (f, data.draw(st.integers(0, sim.n[f] - 1)))
    sim.start()
    sim.run(max_steps=60_000)
    sim.assert_finished()
    assert all(lost <= MAX_RESENT_PER_CRASH for lost in sim.lost_per_crash), sim.lost_per_crash


def test_receiver_crash_right_after_clear_blocks_loses_clear_and_still_finishes():
    """Ca cố định: B sập ngay khi hàng đĩa vừa nối `CLEAR` của khối hỏng (chưa có `MARK` nào sau nó), nên
    `CLEAR` có thể mất; khối hỏng “sống lại” trong bitfield khôi phục rồi bị lượt đọc lại cuối bắt lại.
    Qua 12 seed, phải có seed thật sự mất `CLEAR` (không thì test không thử gì)."""
    lost_clear = 0
    for seed in range(12):
        sim = CrashSim(random.Random(seed), [20], crashes=1, crash_when=lambda s: s.clears_done > 0)
        sim.disk_corrupt = (0, 7)
        sim.start()
        sim.run(max_steps=60_000)
        sim.assert_finished()
        assert sim.clears_done >= 1 and sim.crash_steps, (seed, "không có ClearBlocks hay không sập")
        lost_clear += sim.cleared_lost
    assert lost_clear > 0


@pytest.mark.parametrize("seed", range(12))
def test_receiver_crash_with_fixed_seeds_really_crashes_and_finishes(seed):
    """Đối chứng cho test hypothesis: với seed cố định, B thật sự sập giữa lượt (không chỉ chạy suôn)."""
    sim = CrashSim(random.Random(seed), [45, 3, 30], crashes=2, crash_rate=0.03)
    sim.start()
    sim.run(max_steps=60_000)
    sim.assert_finished()
    assert sim.crash_steps, "seed này không sập: tăng crash_rate"
    assert all(lost <= MAX_RESENT_PER_CRASH for lost in sim.lost_per_crash), sim.lost_per_crash


def _marked_at_least(k: int, f: int = 0):
    def when(sim: CrashSim) -> bool:
        on, _ = sim.replay((TID, f), sim.n[f])
        return len(on) >= k

    return when


@pytest.mark.parametrize("first_client", ["A", "B"])
@pytest.mark.parametrize("seed", range(4))
def test_restored_receive_resumes_with_restored_bitfield_and_sender_sends_only_missing(seed, first_client):
    """spec-p3 P3-2 “Xong khi”: khôi phục + `offer` gửi lại → `accept` + `resume` mang bitfield khôi phục;
    bên gửi chỉ gửi khối thiếu. B sập khi `.state` đã đánh dấu ≥ 20 khối của file 0 (64 khối)."""
    sim = CrashSim(
        random.Random(seed), [64, 2], crashes=1, crash_when=_marked_at_least(20), first_client=first_client
    )
    sim.start()
    sim.run(max_steps=60_000)
    sim.assert_finished()
    assert len(sim.restore_steps) == 1
    f, on = sim.restored[0]
    assert f == 0 and len(on) >= 20
    after = sim.restore_steps[0]
    first = next(m for step, m in sim.b_resumes if step >= after)
    assert first.file_index == 0 and _bits(first.bitfield, 64) == on
    sends = [miss for step, ff, miss in sim.A.send_log if step >= after and ff == 0]
    assert sends, "bên gửi không gửi gì cho file 0 sau khi B mở lại"
    assert set(sends[0]) == set(range(64)) - on
    assert sim.offer_replies, "bên gửi phải gửi lại offer trên kết nối mới"
    assert all(reply == ["Accept"] for reply in sim.offer_replies), sim.offer_replies


def _done_before_sender_knows(sim: CrashSim) -> bool:
    """B đã `RemoveTemp(keep_record=True)` (lượt xong) mà A chưa nhận `verified` của file cuối."""
    return sim.records.get(TID, {}).get("done", False) and sim.A.ended.get(sim.key_a) is None


class DoneCrashSim(CrashSim):
    """B sập ngay sau khi xong mà `verified` của file cuối chưa tới A (message trên đường bị mất)."""

    def can_crash(self) -> bool:
        return self.crash_budget > 0 and not self.b_down and self.current_link is not None


@pytest.mark.parametrize("seed", range(6))
def test_resent_complete_after_restoring_finished_transfer_gets_verified(seed):
    """spec-p3 P3-2 “Xong khi”: `complete` gửi lại sau khôi phục lượt xong (`RestoredDone`) → `verified`."""
    sim = DoneCrashSim(random.Random(seed), [3, 2], crashes=1, crash_when=_done_before_sender_knows)
    sim.start()
    sim.run(max_steps=60_000)
    assert sim.crash_steps, "B không sập sau khi xong"
    ea = sim.A.ended.get(sim.key_a)
    assert ea is not None and ea.outcome == Outcome.DONE, ea
    assert sim.receiver_content_ok()
    assert all(sim.B.finalize_count[(TID, f)] == 1 for f in range(2))
    assert sim.restored == []  # chỉ có `RestoredDone`, không lượt nhận nào


def test_crash_before_record_durable_asks_again():
    """spec-p3 “`temp_created`” (thay hàng “Thấp” của mục 2.1): bên nhận chỉ gửi `accept`/`resume` đầu
    tiên sau `temp_created`. B `decide(True)` (file 0 rỗng), chưa có `temp_created` → không gửi `accept`
    hay `resume`, nên A không có `complete` nào. B sập trước khi thư mục tạm bền; mở lại với `restore()`
    rỗng. A `control_up` chỉ gửi lại `offer` (không `complete`); B xét như `offer` mới và hỏi lại người
    dùng (`AskUser`); không bên nào gửi `cancel`.
    """
    files = [FileEntry(name="rong.bin", size=0, mtime=1), FileEntry(name="b.bin", size=5, mtime=2)]
    A = TransferEngine(SA, receiving=False)
    B1 = TransferEngine(RB, receiving=True)
    t = 1.0
    A.control_up(RB, 1, True, t)
    B1.control_up(SA, 2, False, t)
    for m in [a.message for a in A.send(RB, TID, files, t) if isinstance(a, SendMessage)]:
        B1.message(SA, 2, m, t)
    B1.space_checked(SA, TID, FREE, None, t)
    b_acts = B1.decide(SA, TID, True, t)
    assert any(isinstance(a, CreateTemp) for a in b_acts)
    assert [a for a in b_acts if isinstance(a, SendMessage)] == [], "accept/resume trước temp_created"
    # B sập: `CreateTemp` chưa chạy xong, không `temp_created`. Mở lại, không có gì để khôi phục.
    t = 5.0
    A.control_lost(RB, 1, t)
    B2 = TransferEngine(RB, receiving=True)
    assert B2.restore(receives=(), tombstones=(), done=(), now=t) == []
    up_a = A.control_up(RB, 3, True, t)
    resent = [a.message for a in up_a if isinstance(a, SendMessage)]
    assert [type(m) for m in resent] == [Offer], resent
    B2.control_up(SA, 4, False, t)
    replies: list = []
    for m in resent:
        replies += B2.message(SA, 4, m, t)
    replies += B2.space_checked(SA, TID, FREE, None, t)
    cancels = [a.message for a in replies if isinstance(a, SendMessage) and isinstance(a.message, Cancel)]
    assert cancels == [], f"B hủy lượt thay vì hỏi lại: {cancels}"
    assert any(isinstance(a, AskUser) and a.transfer_id == TID for a in replies), replies


@pytest.mark.parametrize("seed", range(12))
def test_crash_between_decide_and_temp_created_asks_again_and_finishes(seed):
    """Như trên trong mô phỏng: B sập ngay sau `decide`, trước khi `CreateTemp` chạy xong. Lượt được hỏi
    lại (mô phỏng tự nhận), không `cancel`, hai bên DONE, file khớp nguồn."""

    def when(sim: CrashSim) -> bool:
        return TID in sim.B.created and TID not in sim.records

    sim = CrashSim(random.Random(seed), [0, 3, 17], crashes=1, crash_when=when)
    sim.start()
    sim.run(max_steps=60_000)
    assert sim.crash_steps, "B không sập trong cửa sổ decide → temp_created"
    sim.assert_finished()
    assert sim.asks >= 2, "B phải hỏi lại sau khi sập"
    assert sim.lost_per_crash == [0], "trước temp_created không có khối nào được nhận"
