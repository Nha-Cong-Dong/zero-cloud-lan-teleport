"""`teleport.ui.offer.OfferBook`: nguồn sự thật duy nhất của lời mời nhận file phía ui (spec B4 mục 3.2,
mục 7 T-B; mục 11 thắng khi khác).

Viết từ spec, không từ code. Không widget: chỉ `OfferBook` (QObject) và dataclass thật của
`teleport.app.transfers`. Luật chính: mỗi offer có **đúng một** `answered` và chỉ khi đang PENDING
(chống trả lời hai lần, spec 3.2 “Luật”; engine không chặn `decide(False)` sau `decide(True)`).
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PEER_ID = "a71c04d2" + "e" * 24
OTHER_ID = "0f0f0f0f" + "1" * 24


# =================================================================================================
# Dữ liệu giả (định nghĩa tại chỗ: không import conftest)
# =================================================================================================


def tid(i: int) -> str:
    return f"{i:032x}"


def tkey(i: int = 1, *, device_id: str = PEER_ID, direction: str = "receive"):
    from teleport.core.transfer import Direction, TransferKey

    return TransferKey(device_id, Direction(direction), tid(i))


def make_offer(key, *, auto: bool = False, names=("video.mp4",)):
    from teleport.app.transfers import OfferReceived
    from teleport.core.messages import FileEntry

    files = tuple(FileEntry(n, 1000, 1_700_000_000_000) for n in names)
    return OfferReceived(
        key=key,
        device_name="PC-Nha",
        files=files,
        names=tuple(names),
        total_bytes=1000 * len(names),
        auto_accepted=auto,
    )


class Recorder:
    """Ghi mọi signal của book theo đúng thứ tự phát (một danh sách chung)."""

    def __init__(self, book) -> None:
        self.events: list[tuple] = []
        book.added.connect(lambda k: self.events.append(("added", k)))
        book.changed.connect(lambda k: self.events.append(("changed", k)))
        book.removed.connect(lambda k: self.events.append(("removed", k)))
        book.answered.connect(lambda k, a, al: self.events.append(("answered", k, a, al)))

    def of(self, kind: str) -> list[tuple]:
        return [e for e in self.events if e[0] == kind]

    def clear(self) -> None:
        self.events.clear()


@pytest.fixture
def book(qapp):
    from teleport.ui.offer import OfferBook

    b = OfferBook()
    b.rec = Recorder(b)  # type: ignore[attr-defined]
    return b


# =================================================================================================
# B1 add
# =================================================================================================


def test_add_receive_offer_is_pending_and_emits_added(book):
    from teleport.ui.offer import OfferPhase

    k = tkey(1)
    offer = make_offer(k)
    assert book.add(offer) is True
    entry = book.entry(k)
    assert entry is not None
    assert entry.offer is offer
    assert entry.phase is OfferPhase.PENDING
    assert entry.accepted is None
    assert entry.always is False
    assert entry.connected is True
    assert book.rec.events == [("added", k)]
    assert book.pending_keys() == [k]


def test_add_keeps_connected_flag_given_by_caller(book):
    k = tkey(1)
    assert book.add(make_offer(k), connected=False)
    assert book.entry(k).connected is False


def test_add_auto_accepted_offer_returns_false_and_does_nothing(book):
    k = tkey(1)
    assert book.add(make_offer(k, auto=True)) is False
    assert book.entry(k) is None
    assert book.pending_keys() == []
    assert book.rec.events == []


def test_add_same_key_twice_returns_false_and_keeps_first_entry(book):
    k = tkey(1)
    first = make_offer(k)
    assert book.add(first)
    book.rec.clear()
    assert book.add(make_offer(k, names=("khac.bin",))) is False
    assert book.entry(k).offer is first
    assert book.rec.events == []


def test_add_send_direction_returns_false(book):
    k = tkey(1, direction="send")
    assert book.add(make_offer(k)) is False
    assert book.entry(k) is None
    assert book.rec.events == []


def test_pending_keys_follow_arrival_order(book):
    keys = [tkey(i, device_id=(PEER_ID if i % 2 else OTHER_ID)) for i in (5, 2, 9, 1)]
    for k in keys:
        assert book.add(make_offer(k))
    assert book.pending_keys() == keys
    seqs = [book.entry(k).seq for k in keys]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq tăng dần theo thứ tự tới"


# =================================================================================================
# B2 answer: lần đầu thắng
# =================================================================================================


@pytest.mark.parametrize("accept", [True, False])
def test_first_answer_wins_emits_changed_then_answered_once(book, accept):
    from teleport.ui.offer import OfferPhase

    k = tkey(1)
    book.add(make_offer(k))
    book.rec.clear()
    assert book.answer(k, accept) is True
    assert book.rec.events == [("changed", k), ("answered", k, accept, False)]
    entry = book.entry(k)
    assert entry.phase is OfferPhase.ANSWERED
    assert entry.accepted is accept
    assert book.pending_keys() == []
    book.rec.clear()
    assert book.answer(k, not accept) is False
    assert book.answer(k, accept) is False
    assert book.rec.events == [], "lần trả lời thứ hai không phát gì"
    assert book.entry(k).accepted is accept


def test_answered_carries_always_flag(book):
    k = tkey(1)
    book.add(make_offer(k))
    book.set_always(k, True)
    book.rec.clear()
    assert book.answer(k, True)
    assert book.rec.of("answered") == [("answered", k, True, True)]


def test_answer_unknown_key_returns_false(book):
    assert book.answer(tkey(7), True) is False
    assert book.rec.events == []


def test_answer_one_key_does_not_touch_other(book):
    from teleport.ui.offer import OfferPhase

    a, b = tkey(1), tkey(2, device_id=OTHER_ID)
    book.add(make_offer(a))
    book.add(make_offer(b))
    book.answer(a, False)
    assert book.entry(b).phase is OfferPhase.PENDING
    assert book.pending_keys() == [b]


# =================================================================================================
# B3 finish
# =================================================================================================


def test_finish_pending_emits_removed_never_answered(book):
    k = tkey(1)
    book.add(make_offer(k))
    book.rec.clear()
    book.finish(k)
    assert book.rec.events == [("removed", k)]
    assert book.entry(k) is None
    assert book.answer(k, True) is False
    assert book.rec.of("answered") == []


def test_finish_answered_emits_removed(book):
    k = tkey(1)
    book.add(make_offer(k))
    book.answer(k, True)
    book.rec.clear()
    book.finish(k)
    assert book.rec.events == [("removed", k)]
    assert book.entry(k) is None


def test_finish_unknown_key_is_ignored(book):
    book.finish(tkey(3))
    assert book.rec.events == []


# =================================================================================================
# B4 set_always
# =================================================================================================


def test_set_always_only_while_pending_and_only_on_change(book):
    k = tkey(1)
    book.add(make_offer(k))
    book.rec.clear()
    book.set_always(k, False)  # đã False
    assert book.rec.events == []
    book.set_always(k, True)
    assert book.rec.events == [("changed", k)]
    assert book.entry(k).always is True
    book.set_always(k, True)
    assert book.rec.events == [("changed", k)], "giá trị không đổi: không phát"
    book.answer(k, False)
    book.rec.clear()
    book.set_always(k, False)
    assert book.rec.events == [], "ANSWERED: set_always không làm gì"
    assert book.entry(k).always is True


def test_set_always_unknown_key_is_ignored(book):
    book.set_always(tkey(4), True)
    assert book.rec.events == []


# =================================================================================================
# set_connected
# =================================================================================================


def test_set_connected_changes_entries_of_that_device_only(book):
    a1, a2, b = tkey(1), tkey(2), tkey(3, device_id=OTHER_ID)
    for k in (a1, a2, b):
        book.add(make_offer(k))
    book.rec.clear()
    book.set_connected(PEER_ID, False)
    assert {e[1] for e in book.rec.of("changed")} == {a1, a2}
    assert book.entry(a1).connected is False and book.entry(a2).connected is False
    assert book.entry(b).connected is True
    book.rec.clear()
    book.set_connected(PEER_ID, False)
    assert book.rec.events == [], "không đổi: không phát"
    book.set_connected(PEER_ID, True)
    assert {e[1] for e in book.rec.of("changed")} == {a1, a2}
    assert book.rec.of("answered") == []


# =================================================================================================
# B5 clear
# =================================================================================================


def test_clear_removes_every_key_without_answering(book):
    keys = [tkey(i) for i in range(4)]
    for k in keys:
        book.add(make_offer(k))
    book.answer(keys[1], True)
    book.rec.clear()
    book.clear()
    assert sorted(e[1].transfer_id for e in book.rec.of("removed")) == sorted(k.transfer_id for k in keys)
    assert book.rec.of("answered") == []
    assert [e for e in book.rec.events if e[0] != "removed"] == []
    for k in keys:
        assert book.entry(k) is None
    assert book.pending_keys() == []


def test_add_after_clear_works_again(book):
    k = tkey(1)
    book.add(make_offer(k))
    book.clear()
    assert book.add(make_offer(k)) is True


# =================================================================================================
# B6 OfferReceived lặp lại / tới muộn
# =================================================================================================


def test_repeated_offer_after_answer_is_not_added_again(book):
    from teleport.ui.offer import OfferPhase

    k = tkey(1)
    book.add(make_offer(k))
    book.answer(k, True)
    book.rec.clear()
    assert book.add(make_offer(k)) is False
    assert book.entry(k).phase is OfferPhase.ANSWERED
    assert book.rec.events == []
    assert book.pending_keys() == []


# =================================================================================================
# B7 trần OFFER_BOOK_MAX
# =================================================================================================


def test_offer_book_max_value():
    from teleport.ui.offer import OFFER_BOOK_MAX

    assert OFFER_BOOK_MAX == 1024


def test_cap_drops_oldest_answered_entry_never_pending(book):
    from teleport.ui.offer import OFFER_BOOK_MAX, OfferPhase

    keys = [tkey(i) for i in range(OFFER_BOOK_MAX)]
    for k in keys:
        assert book.add(make_offer(k))
    # PENDING cũ nhất là keys[0]; ANSWERED cũ nhất là keys[5].
    assert book.answer(keys[5], True)
    assert book.answer(keys[10], False)
    book.rec.clear()
    extra = tkey(OFFER_BOOK_MAX + 1)
    assert book.add(make_offer(extra)) is True
    assert book.rec.of("removed") == [("removed", keys[5])]
    assert book.rec.of("answered") == []
    assert book.entry(keys[5]) is None
    assert book.entry(keys[0]).phase is OfferPhase.PENDING
    assert book.entry(keys[10]).phase is OfferPhase.ANSWERED
    assert book.entry(extra).phase is OfferPhase.PENDING


def test_cap_never_drops_pending_even_when_all_pending(book):
    from teleport.ui.offer import OFFER_BOOK_MAX, OfferPhase

    keys = [tkey(i) for i in range(OFFER_BOOK_MAX)]
    for k in keys:
        book.add(make_offer(k))
    book.rec.clear()
    book.add(make_offer(tkey(OFFER_BOOK_MAX + 1)))
    assert book.rec.of("removed") == [], "không bao giờ gỡ PENDING"
    for k in keys:
        assert book.entry(k).phase is OfferPhase.PENDING


# =================================================================================================
# B8 property: chống trả lời hai lần dưới mọi thứ tự
# =================================================================================================

_KEY_INDEX = st.integers(0, 4)
_OP = st.one_of(
    st.tuples(st.just("add"), _KEY_INDEX, st.booleans(), st.booleans()),  # (auto, chiều SEND)
    st.tuples(st.just("answer"), _KEY_INDEX, st.booleans()),
    st.tuples(st.just("finish"), _KEY_INDEX),
    st.tuples(st.just("always"), _KEY_INDEX, st.booleans()),
    st.tuples(st.just("connected"), st.booleans()),
    st.tuples(st.just("clear")),
)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(_OP, max_size=40))
def test_property_each_offer_answered_at_most_once_and_only_from_pending(qapp, ops):
    """Mô hình: mỗi key ở (không có) / PENDING / ANSWERED. `answered` chỉ phát khi `answer` gặp PENDING,
    đúng một lần cho mỗi đời của key (từ `add` tới `finish`/`clear`), mang `always` của lúc đó."""
    from teleport.ui.offer import OfferBook, OfferPhase

    b = OfferBook()
    answered: list[tuple] = []
    b.answered.connect(lambda k, a, al: answered.append((k, a, al)))
    keys = [tkey(i, device_id=(PEER_ID if i < 3 else OTHER_ID)) for i in range(5)]
    model: dict = {}  # key -> {"phase", "always", "answers"}
    for op in ops:
        before = len(answered)
        kind = op[0]
        if kind == "add":
            _, i, auto, send = op
            k = tkey(i, direction="send") if send else keys[i]
            ok = b.add(make_offer(k, auto=auto))
            expect = not auto and not send and k not in model
            assert ok is expect, op
            if expect:
                model[k] = {"phase": "pending", "always": False, "answers": 0}
            assert len(answered) == before
        elif kind == "answer":
            _, i, accept = op
            k = keys[i]
            ok = b.answer(k, accept)
            state = model.get(k)
            if state is not None and state["phase"] == "pending":
                assert ok is True, op
                assert answered[before:] == [(k, accept, state["always"])]
                state["phase"] = "answered"
                state["answers"] += 1
            else:
                assert ok is False, op
                assert len(answered) == before
        elif kind == "finish":
            b.finish(keys[op[1]])
            model.pop(keys[op[1]], None)
            assert len(answered) == before
        elif kind == "always":
            _, i, on = op
            b.set_always(keys[i], on)
            state = model.get(keys[i])
            if state is not None and state["phase"] == "pending":
                state["always"] = on
            assert len(answered) == before
        elif kind == "connected":
            b.set_connected(PEER_ID, op[1])
            assert len(answered) == before
        else:
            b.clear()
            model.clear()
            assert len(answered) == before
        for k in keys:
            state = model.get(k)
            entry = b.entry(k)
            if state is None:
                assert entry is None, (op, k)
            else:
                assert entry is not None, (op, k)
                assert entry.phase is (
                    OfferPhase.PENDING if state["phase"] == "pending" else OfferPhase.ANSWERED
                )
                assert state["answers"] <= 1
        assert b.pending_keys() == sorted(
            (k for k, s in model.items() if s["phase"] == "pending"), key=lambda k: b.entry(k).seq
        )
