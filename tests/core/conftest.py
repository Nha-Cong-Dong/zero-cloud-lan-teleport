"""Fixture dùng chung cho test máy trạng thái truyền file (spec P2-4, `teleport.core.transfer`).

Test không import được conftest (`--import-mode=importlib`), nên các hàm và lớp phụ trợ đi qua
fixture `tx` (phạm vi session để dùng được trong test hypothesis). `teleport.core.transfer` chỉ được
import bên trong fixture: module chưa có hay lỗi thì chỉ các test dùng tới nó hỏng, các test core
khác vẫn chạy.

Hai “bộ lái” (`Recv`, `Send`) đưa một `TransferEngine` qua các bước thường gặp theo đúng hợp đồng
thực thi (spec mục 2.8): gọi engine với `now` tăng dần, kiểm thứ tự G5 của mọi danh sách hành động
trả về và ghi lại nhật ký. Chúng chỉ dùng API công khai (spec mục 2.10).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from teleport.core.bitfield import Bitfield
from teleport.core.blocks import BLOCK_SIZE, block_count
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

ME = "e" * 32  # máy chạy engine
PEER = "a" * 32  # máy kia
OTHER = "c" * 32  # thiết bị tin cậy thứ ba
FREE = 10**15  # dung lượng trống “thoải mái” cho `space_checked`
EMPTY_ROOT = hashlib.sha256(b"").digest()


def tid(n: int) -> str:
    """`transfer_id` hợp lệ (32 hex thường) thứ `n`."""
    return f"{n:032x}"


T1, T2, T3 = tid(1), tid(2), tid(3)


def size_of(nblocks: int, last: int = BLOCK_SIZE) -> int:
    """Kích thước file `nblocks` khối, khối cuối dài `last` byte (file rỗng: 0)."""
    return 0 if nblocks == 0 else (nblocks - 1) * BLOCK_SIZE + last


def entries(
    *nblocks: int, last: int = BLOCK_SIZE, names: Sequence[str] | None = None
) -> tuple[FileEntry, ...]:
    """Các `FileEntry` có số khối cho trước; `mtime` khác nhau theo chỉ số file."""
    return tuple(
        FileEntry(name=names[i] if names else f"f{i}.bin", size=size_of(n, last), mtime=1_700_000_000_000 + i)
        for i, n in enumerate(nblocks)
    )


def digest(i: int, salt: str = "d") -> bytes:
    return hashlib.sha256(f"{salt}:{i}".encode()).digest()


def leaves_of(n: int, salt: str = "d") -> bytes:
    return b"".join(digest(i, salt) for i in range(n))


def bits(n: int, on: Iterable[int] = ()) -> Bitfield:
    b = Bitfield(n)
    for i in on:
        b.set(i)
    return b


def bits_on(b64: str, n: int) -> set[int]:
    b = Bitfield.from_base64(b64, n)
    return set(range(n)) - set(b.missing())


class Clock:
    """Đồng hồ ảo dùng chung cho mọi bộ lái của cùng một engine."""

    def __init__(self, now: float = 1.0) -> None:
        self.now = now


def _make(tr: ModuleType) -> SimpleNamespace:
    """Dựng không gian tên phụ trợ quanh module `teleport.core.transfer` đã import."""

    def of(actions: Sequence[object], cls: type | tuple[type, ...]) -> list[Any]:
        return [a for a in actions if isinstance(a, cls)]

    def one(actions: Sequence[object], cls: type | tuple[type, ...]) -> Any:
        found = of(actions, cls)
        assert len(found) == 1, f"cần đúng một {cls}, có {len(found)}: {actions}"
        return found[0]

    def sent(actions: Sequence[object], cls: type | tuple[type, ...] | None = None) -> list[Any]:
        """Message trong các `SendMessage`, theo thứ tự, lọc theo kiểu nếu có."""
        msgs = [a.message for a in of(actions, tr.SendMessage)]
        return msgs if cls is None else [m for m in msgs if isinstance(m, cls)]

    def events(actions: Sequence[object], cls: type | tuple[type, ...] | None = None) -> list[Any]:
        evs = [a.event for a in of(actions, tr.Report)]
        return evs if cls is None else [e for e in evs if isinstance(e, cls)]

    def ended(actions: Sequence[object]) -> list[Any]:
        return events(actions, tr.TransferEnded)

    def statuses(actions: Sequence[object], key: Any) -> list[Any]:
        return [e.status for e in events(actions, tr.StatusChanged) if e.key == key]

    def index_of(actions: Sequence[object], pred: Any) -> int:
        for i, a in enumerate(actions):
            if pred(a):
                return i
        raise AssertionError(f"không có hành động thỏa điều kiện trong {actions}")

    def check_order(actions: Sequence[object]) -> None:
        """G5: các `Report` đứng cuối danh sách; trong nhóm đó `StatusChanged` đứng sau mọi Report khác."""
        rank = [
            (2 if isinstance(a.event, tr.StatusChanged) else 1) if isinstance(a, tr.Report) else 0
            for a in actions
        ]
        assert rank == sorted(rank), f"vi phạm G5: {actions}"
        # P3-2: `TombstonesChanged` chỉ báo “bảng bia mộ đã đổi”, nên mỗi lời gọi phát tối đa một lần.
        changes = [a for a in actions if isinstance(a, tr.TombstonesChanged)]
        assert len(changes) <= 1, f"hơn một TombstonesChanged trong một lời gọi: {actions}"

    def snapshot(engine: Any) -> tuple[Any, Any, Any]:
        """Trạng thái quan sát được qua API công khai (dùng để so trước/sau một lời gọi lỗi)."""
        return engine.transfers(), engine.next_wakeup(), engine.promised_bytes()

    class Driver:
        """Gọi engine với `now` tăng dần, kiểm G5, ghi nhật ký."""

        def __init__(
            self,
            engine: Any,
            peer: str,
            *,
            cid: int = 1,
            client: bool = False,
            clock: Clock | None = None,
            first_did: int = 100,
        ) -> None:
            self.e = engine
            self.peer = peer
            self.cid = cid
            self.client = client
            self.clock = clock or Clock()
            self.log: list[tuple[str, list[Any]]] = []
            self._did = first_did

        @property
        def now(self) -> float:
            return self.clock.now

        @now.setter
        def now(self, value: float) -> None:
            self.clock.now = value

        def call(self, name: str, *args: Any, now: float | None = None) -> list[Any]:
            if now is None:
                self.clock.now += 0.01
            else:
                self.clock.now = now
            acts = getattr(self.e, name)(*args, self.clock.now)
            assert isinstance(acts, list), f"{name} phải trả list, trả {acts!r}"
            check_order(acts)
            self.log.append((name, acts))
            return acts

        def all_actions(self) -> list[Any]:
            return [a for _, acts in self.log for a in acts]

        def new_did(self) -> int:
            self._did += 1
            return self._did

        def up(
            self, cid: int | None = None, client: bool | None = None, *, now: float | None = None
        ) -> list[Any]:
            if cid is not None:
                self.cid = cid
            if client is not None:
                self.client = client
            return self.call("control_up", self.peer, self.cid, self.client, now=now)

        def lost(self, *, now: float | None = None) -> list[Any]:
            return self.call("control_lost", self.peer, self.cid, now=now)

        def msg(self, m: Any, *, cid: int | None = None, now: float | None = None) -> list[Any]:
            return self.call("message", self.peer, self.cid if cid is None else cid, m, now=now)

        def tick(self, now: float | None = None) -> list[Any]:
            return self.call("tick", now=now)

        def tick_due(self) -> list[Any]:
            """Gọi `tick` đúng lúc `next_wakeup()` (không lùi thời gian)."""
            w = self.e.next_wakeup()
            assert w is not None, "không có hẹn giờ nào"
            return self.tick(now=max(w, self.now))

    class Recv(Driver):
        """Bên nhận: engine của máy `ME`, máy `peer` gửi lượt `T` gồm `files`."""

        def __init__(
            self,
            files: Sequence[FileEntry],
            *,
            T: str = T1,
            engine: Any = None,
            receiving: bool = True,
            peer: str = PEER,
            like: Driver | None = None,
            **kw: Any,
        ) -> None:
            if like is not None:
                engine = like.e
                kw.setdefault("clock", like.clock)
                kw.setdefault("first_did", like._did + 1000)
            super().__init__(engine or tr.TransferEngine(ME, receiving=receiving), peer, **kw)
            self.T = T
            self.files = tuple(files)
            self.offer = Offer(transfer_id=T, block_size=BLOCK_SIZE, files=self.files)
            self.key = tr.TransferKey(peer, tr.Direction.RECEIVE, T)

        def n(self, f: int) -> int:
            return block_count(self.files[f].size)

        def salt(self, f: int) -> str:
            return f"{self.T}:{f}"

        def dg(self, f: int, i: int) -> bytes:
            return digest(i, self.salt(f))

        def leaves(self, f: int) -> bytes:
            return leaves_of(self.n(f), self.salt(f))

        def root(self, f: int) -> bytes:
            return merkle_root(self.leaves(f))

        def send_offer(self, offer: Offer | None = None, **kw: Any) -> list[Any]:
            return self.msg(offer or self.offer, **kw)

        def to_asking(
            self, *, up: bool = True, free: int | None = FREE, max_size: int | None = None
        ) -> list[Any]:
            acts = self.up() if up else []
            acts += self.send_offer()
            acts += self.call("space_checked", self.peer, self.T, free, max_size)
            return acts

        def accept(self, *, up: bool = True, created: bool = True) -> list[Any]:
            """`offer` → `space_checked` → `decide(True)` → (mặc định) `temp_created`.

            Trả hành động của `decide` nối với của `temp_created` (P3-2: bên nhận chỉ gửi `accept` và
            `resume` đầu tiên sau `temp_created`), nên test cũ vẫn thấy `CreateTemp`, `accept`, `resume`
            trong cùng danh sách. `created=False`: dừng ngay sau `decide`.
            """
            self.to_asking(up=up)
            acts = self.call("decide", self.peer, self.T, True)
            if created:
                acts = acts + self.created()  # không `+=`: danh sách của `decide` đã nằm trong `log`
            return acts

        def created(self) -> list[Any]:
            """Hàng đĩa báo `CreateTemp` đã xong (thư mục tạm và `transfer.json` đã bền)."""
            return self.call("temp_created", self.peer, self.T)

        def resumes(self) -> list[Resume]:
            return [m for m in sent(self.all_actions(), Resume) if m.transfer_id == self.T]

        def last_resume(self) -> Resume:
            rs = self.resumes()
            assert rs, "chưa gửi resume nào"
            return rs[-1]

        def open(
            self, *, seq: int | None = None, f: int | None = None, did: int | None = None
        ) -> tuple[int, list[Any]]:
            """Kết nối dữ liệu cho `resume` gần nhất (server: `incoming_data`; client: `outgoing_data`)."""
            r = self.last_resume()
            m = DataOpen(
                transfer_id=self.T,
                file_index=r.file_index if f is None else f,
                resume_seq=r.seq if seq is None else seq,
            )
            did = did or self.new_did()
            if self.client:
                return did, self.call("outgoing_data", self.peer, self.cid, did, m)
            return did, self.call("incoming_data", self.peer, did, m)

        def feed(self, did: int, f: int, idxs: Iterable[int], *, salt: str | None = None) -> list[Any]:
            acts: list[Any] = []
            for i in idxs:
                acts += self.call("block_received", self.peer, did, i, digest(i, salt or self.salt(f)))
            return acts

        def sync(self, acts: Sequence[object]) -> list[Any]:
            """Báo `synced` cho mọi `SyncPart` trong `acts` (theo thứ tự, như hàng FIFO đĩa)."""
            out: list[Any] = []
            for sp in of(acts, tr.SyncPart):
                out += self.call("synced", self.peer, sp.transfer_id, sp.file_index, sp.token)
            return out

        def sync_all(self, acts: Sequence[object]) -> list[Any]:
            """Như `sync`, lặp tới khi không còn `SyncPart` mới."""
            out: list[Any] = []
            todo = list(acts)
            while of(todo, tr.SyncPart):
                todo = self.sync(todo)
                out += todo
            return out

        def complete(self, f: int, root: bytes | None = None, **kw: Any) -> list[Any]:
            r = self.root(f) if root is None else root
            return self.msg(Complete(transfer_id=self.T, file_index=f, merkle_root=r.hex()), **kw)

        def receive_blocks(self, f: int) -> list[Any]:
            """Mở kết nối cho `resume` gần nhất, nhận mọi khối còn thiếu của file `f`, fsync, đóng."""
            acts: list[Any] = []
            if self.n(f) == 0:
                return acts
            have = bits_on(self.last_resume().bitfield, self.n(f))
            did, a = self.open()
            acts += a
            a = self.feed(did, f, [i for i in range(self.n(f)) if i not in have])
            acts += a
            acts += self.sync_all(a)
            acts += self.call("data_closed", self.peer, did, False)
            return acts

        def finish_file(self, f: int) -> list[Any]:
            """Nhận trọn file `f`: khối → fsync → `complete` → đọc lại sạch → đổi tên."""
            acts = self.receive_blocks(f)
            acts += self.complete(f)
            rr = of(acts, tr.Reread)
            assert rr and rr[-1].file_index == f, acts
            acts += self.call("reread_done", self.peer, self.T, f, [], self.root(f))
            fin = of(acts, tr.Finalize)[-1]
            acts += self.call("finalized", self.peer, self.T, f, fin.name)
            return acts

        def finish_all(self) -> list[Any]:
            acts: list[Any] = []
            for f in range(len(self.files)):
                acts += self.finish_file(f)
            return acts

    class Send(Driver):
        """Bên gửi: engine của máy `ME` gửi lượt `T` gồm `files` cho máy `peer`."""

        def __init__(
            self,
            files: Sequence[FileEntry],
            *,
            T: str = T1,
            engine: Any = None,
            peer: str = PEER,
            like: Driver | None = None,
            client: bool = True,
            **kw: Any,
        ) -> None:
            if like is not None:
                engine = like.e
                kw.setdefault("clock", like.clock)
                kw.setdefault("first_did", like._did + 1000)
            super().__init__(engine or tr.TransferEngine(ME, receiving=False), peer, client=client, **kw)
            self.T = T
            self.files = tuple(files)
            self.offer = Offer(transfer_id=T, block_size=BLOCK_SIZE, files=self.files)
            self.key = tr.TransferKey(peer, tr.Direction.SEND, T)
            self.seq = 0

        def n(self, f: int) -> int:
            return block_count(self.files[f].size)

        def salt(self, f: int) -> str:
            return f"{self.T}:{f}"

        def dg(self, f: int, i: int) -> bytes:
            return digest(i, self.salt(f))

        def root(self, f: int) -> bytes:
            return merkle_root(leaves_of(self.n(f), self.salt(f)))

        def start(self, *, up: bool = True, accept: bool = True) -> list[Any]:
            acts = self.up() if up else []
            acts += self.call("send", self.peer, self.T, list(self.files))
            if accept:
                acts += self.msg(Accept(transfer_id=self.T))
            return acts

        def resume(
            self, f: int, have: Bitfield | None = None, *, seq: int | None = None, **kw: Any
        ) -> list[Any]:
            seq = self.seq + 1 if seq is None else seq
            self.seq = max(self.seq, seq)
            have = Bitfield(self.n(f)) if have is None else have
            return self.msg(
                Resume(transfer_id=self.T, file_index=f, seq=seq, bitfield=have.to_base64()), **kw
            )

        def open(self, f: int, seq: int, *, did: int | None = None) -> tuple[int, list[Any]]:
            m = DataOpen(transfer_id=self.T, file_index=f, resume_seq=seq)
            did = did or self.new_did()
            if self.client:
                return did, self.call("outgoing_data", self.peer, self.cid, did, m)
            return did, self.call("incoming_data", self.peer, did, m)

        def read(self, did: int, f: int, idxs: Iterable[int], *, salt: str | None = None) -> list[Any]:
            acts: list[Any] = []
            for i in idxs:
                acts += self.call("block_read", self.peer, did, i, digest(i, salt or self.salt(f)))
            return acts

        def check(
            self, acts: Sequence[object], *, size: int | None = None, mtime: int | None = None
        ) -> list[Any]:
            """Trả lời mọi `CheckSource` trong `acts` bằng `source_checked` (mặc định: nguồn không đổi)."""
            out: list[Any] = []
            for cs in of(acts, tr.CheckSource):
                f = cs.file_index
                out += self.call(
                    "source_checked",
                    self.peer,
                    self.T,
                    f,
                    self.files[f].size if size is None else size,
                    self.files[f].mtime if mtime is None else mtime,
                    merkle_root(bytes(cs.leaves)),
                )
            return out

        def serve(self, f: int, have: Bitfield | None = None) -> tuple[int | None, list[Any]]:
            """`resume` file `f` → mở kết nối → đọc mọi khối thiếu. Không trả lời `CheckSource`."""
            acts = self.resume(f, have)
            seq = self.seq
            have = Bitfield(self.n(f)) if have is None else have
            if have.all_set():
                return None, acts
            did, a = self.open(f, seq)
            acts += a
            sb = one(a, tr.SendBlocks)
            acts += self.read(did, f, list(sb.have.missing()))
            return did, acts

        def serve_file(self, f: int, have: Bitfield | None = None) -> list[Any]:
            """`serve` + `source_checked` khớp + đóng gọn kết nối. Kết quả có `complete` của file `f`."""
            did, acts = self.serve(f, have)
            acts += self.check(acts)
            if did is not None:
                acts += self.call("data_closed", self.peer, did, True)
            return acts

    return SimpleNamespace(
        tr=tr,
        ME=ME,
        PEER=PEER,
        OTHER=OTHER,
        FREE=FREE,
        EMPTY_ROOT=EMPTY_ROOT,
        T1=T1,
        T2=T2,
        T3=T3,
        tid=tid,
        size_of=size_of,
        entries=entries,
        digest=digest,
        leaves_of=leaves_of,
        bits=bits,
        bits_on=bits_on,
        Clock=Clock,
        of=of,
        one=one,
        sent=sent,
        events=events,
        ended=ended,
        statuses=statuses,
        index_of=index_of,
        check_order=check_order,
        snapshot=snapshot,
        Driver=Driver,
        Recv=Recv,
        Send=Send,
        Accept=Accept,
        Cancel=Cancel,
        Complete=Complete,
        DataOpen=DataOpen,
        Offer=Offer,
        Reject=Reject,
        Resume=Resume,
        Verified=Verified,
    )


@pytest.fixture(scope="session")
def tx() -> SimpleNamespace:
    """Phụ trợ cho test `teleport.core.transfer` (test không import được conftest)."""
    import teleport.core.transfer as tr

    return _make(tr)
