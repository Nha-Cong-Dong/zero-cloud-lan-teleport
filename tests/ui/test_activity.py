"""`teleport.ui.activity`: hoạt động theo máy cho danh sách cuộc trò chuyện (spec B2 mục 2.3, 12.8, 12.9,
12.18; T-A1…A8).

Viết từ spec, không từ code. Module là hàm thuần, không Qt: test dựng `TransferUpdate` thật của
`teleport.app.transfers` và `HistoryEntry` thật của `teleport.core.history`, rồi gọi `ActivityTracker`
với `wall_now` tự chọn. Có một property test (`hypothesis`) cho chuỗi sự kiện lộn xộn.
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

DEV_A = "a" * 32
DEV_B = "b" * 32
TID_1 = "1" * 32
TID_2 = "2" * 32
TID_3 = "3" * 32


# =================================================================================================
# Dữ liệu giả (định nghĩa tại chỗ: không import conftest)
# =================================================================================================


def key(device_id: str = DEV_A, tid: str = TID_1, direction: str = "send"):
    from teleport.app.transfers import Direction, TransferKey

    return TransferKey(device_id=device_id, direction=Direction(direction), transfer_id=tid)


def info(
    k=None, *, status: str = "transferring", done: int = 0, unsynced: int = 0, total: int = 1000, names=None
):
    from teleport.app.transfers import TransferInfo, TransferStatus
    from teleport.core.messages import FileEntry

    k = k or key()
    names = ("bao-cao.pdf",) if names is None else tuple(names)
    per = total // max(1, len(names))
    files = tuple(
        FileEntry(name=n, size=per if i < len(names) - 1 else total - per * (len(names) - 1), mtime=0)
        for i, n in enumerate(names)
    )
    return TransferInfo(
        key=k,
        status=TransferStatus(status),
        files=files,
        names=names,
        file_index=0,
        done_bytes=done,
        total_bytes=total,
        unsynced_bytes=unsynced,
    )


def status_changed(k=None, **kw):
    from teleport.app.transfers import TransferStatusChanged

    return TransferStatusChanged(info=info(k, **kw))


def resumed(k=None, **kw):
    from teleport.app.transfers import TransferResumed

    return TransferResumed(info=info(k, **kw))


def progress(k=None, shown: int = 0, **kw):
    from teleport.app.transfers import TransferProgress

    return TransferProgress(
        info=info(k, **kw), bytes_per_second=0.0, eta=None, shown_bytes=shown, idle_seconds=0.0
    )


def finished(k=None, result: str = "done", **kw):
    from teleport.app.transfers import TransferFinished, TransferResult

    return TransferFinished(
        info=info(k, **kw),
        result=TransferResult(result),
        by_peer=False,
        reason=None,
        file_index=None,
        reject=None,
    )


def offer(k=None, names=("anh.jpg", "b.txt"), total: int = 500):
    from teleport.app.transfers import OfferReceived
    from teleport.core.messages import FileEntry

    k = k or key(direction="receive")
    files = tuple(FileEntry(name=n, size=total // len(names), mtime=0) for n in names)
    return OfferReceived(
        key=k, device_name="PC-Nha", files=files, names=tuple(names), total_bytes=total, auto_accepted=False
    )


def file_done(k=None):
    from teleport.app.transfers import FileDone

    return FileDone(key=k or key(), file_index=0, name="bao-cao.pdf", path=None)


def data_unreachable(device_id: str = DEV_A):
    from teleport.app.transfers import DataUnreachable

    return DataUnreachable(device_id=device_id)


def entry(
    device_id: str = DEV_A,
    *,
    tid: str = TID_1,
    direction: str = "send",
    result: str = "done",
    at=None,
    names=("a.txt",),
    count: int = 1,
):
    from teleport.core.history import HistoryEntry

    at = at or datetime.datetime(2026, 10, 30, 9, 15, tzinfo=datetime.UTC)
    return HistoryEntry(
        direction=direction,
        device_id=device_id,
        device_name="PC-Nha",
        transfer_id=tid,
        file_count=count,
        total_bytes=10,
        done_bytes=10 if result == "done" else 0,
        names=tuple(names),
        result=result,
        finished_at=at,
        saved_path=None,
    )


@pytest.fixture
def tracker():
    from teleport.ui.activity import ActivityTracker

    return ActivityTracker()


# =================================================================================================
# A1: lượt đang chạy, shown_bytes
# =================================================================================================


def test_status_changed_makes_device_busy_and_returns_its_id(tracker):
    from teleport.app.transfers import TransferStatus

    assert tracker.handle(status_changed(done=10, unsynced=5), 100.0) == DEV_A
    act = tracker.get(DEV_A)
    assert act.busy
    [a] = act.active
    assert a.key == key()
    assert a.status is TransferStatus.TRANSFERRING
    assert a.shown_bytes == 15, "shown_bytes = done_bytes + unsynced_bytes"
    assert a.total_bytes == 1000
    assert a.file_count == 1
    assert a.first_name == "bao-cao.pdf"
    assert a.started_at == 100.0
    assert act.at == 100.0


def test_progress_increases_shown_bytes_but_never_decreases_it(tracker):
    tracker.handle(status_changed(), 100.0)
    tracker.handle(progress(shown=400), 101.0)
    assert tracker.get(DEV_A).active[0].shown_bytes == 400
    tracker.handle(progress(shown=200), 102.0)
    assert tracker.get(DEV_A).active[0].shown_bytes == 400, "Progress nhỏ hơn không làm giảm"
    tracker.handle(status_changed(done=100, unsynced=0), 103.0)
    assert tracker.get(DEV_A).active[0].shown_bytes == 400, "StatusChanged nhỏ hơn không làm giảm"
    tracker.handle(status_changed(done=500, unsynced=20), 104.0)
    assert tracker.get(DEV_A).active[0].shown_bytes == 520


def test_shown_bytes_never_exceeds_total(tracker):
    tracker.handle(status_changed(total=1000), 100.0)
    tracker.handle(progress(shown=5000, total=1000), 101.0)
    a = tracker.get(DEV_A).active[0]
    assert a.shown_bytes <= a.total_bytes == 1000


def test_status_change_updates_status_and_keeps_first_seen_time(tracker):
    from teleport.app.transfers import TransferStatus

    tracker.handle(status_changed(status="waiting"), 100.0)
    tracker.handle(status_changed(status="verifying"), 250.0)
    [a] = tracker.get(DEV_A).active
    assert a.status is TransferStatus.VERIFYING
    assert a.started_at == 100.0, "started_at là lúc thấy lần đầu"


def test_transfer_resumed_adds_an_active_transfer(tracker):
    from teleport.app.transfers import TransferStatus

    assert tracker.handle(resumed(key(direction="receive"), status="reconnecting", done=300), 50.0) == DEV_A
    [a] = tracker.get(DEV_A).active
    assert a.status is TransferStatus.RECONNECTING
    assert a.shown_bytes == 300


# =================================================================================================
# A2: kết thúc
# =================================================================================================


def test_finished_done_ends_busy_and_sets_last(tracker):
    from teleport.app.transfers import Direction, TransferResult

    tracker.handle(status_changed(names=("x.mp4", "y.mp4")), 100.0)
    assert tracker.handle(finished(names=("x.mp4", "y.mp4")), 200.0) == DEV_A
    act = tracker.get(DEV_A)
    assert not act.busy
    assert act.active == ()
    assert act.last is not None
    assert act.last.result is TransferResult.DONE
    assert act.last.at == 200.0
    assert act.last.direction is Direction.SEND
    assert act.last.file_count == 2
    assert act.last.first_name == "x.mp4"
    assert act.at == 200.0
    assert DEV_A not in tracker.busy_devices()


def test_finished_suspended_keeps_transfer_active(tracker):
    tracker.handle(status_changed(key(direction="receive")), 100.0)
    assert tracker.handle(finished(key(direction="receive"), result="suspended"), 200.0) is None
    act = tracker.get(DEV_A)
    assert act.busy, "SUSPENDED giữ lượt như khung chat"
    assert act.last is None
    assert DEV_A in tracker.busy_devices()


def test_finished_without_prior_events_still_sets_last(tracker):
    from teleport.app.transfers import TransferResult

    assert tracker.handle(finished(result="rejected_user"), 300.0) == DEV_A
    act = tracker.get(DEV_A)
    assert not act.busy
    assert act.last.result is TransferResult.REJECTED_USER


# =================================================================================================
# A3: sự kiện muộn, OfferReceived
# =================================================================================================


@pytest.mark.parametrize("late", ["status", "progress", "resumed"])
def test_late_event_of_finished_transfer_does_not_rebuild_it(tracker, late):
    tracker.handle(status_changed(), 100.0)
    tracker.handle(finished(), 200.0)
    event = {"status": status_changed(), "progress": progress(shown=10), "resumed": resumed()}[late]
    tracker.handle(event, 300.0)
    act = tracker.get(DEV_A)
    assert not act.busy, f"{late} muộn dựng lại lượt đã kết thúc"
    assert act.last.at == 200.0


def test_late_offer_of_finished_transfer_does_not_rebuild_it(tracker):
    k = key(direction="receive")
    tracker.handle(offer(k), 100.0)
    tracker.handle(finished(k, result="rejected_user"), 150.0)
    tracker.handle(offer(k), 160.0)
    assert not tracker.get(DEV_A).busy


def test_offer_received_adds_waiting_receive_transfer(tracker):
    from teleport.app.transfers import Direction, TransferStatus

    k = key(direction="receive")
    assert tracker.handle(offer(k, names=("anh.jpg", "b.txt"), total=500), 100.0) == DEV_A
    [a] = tracker.get(DEV_A).active
    assert a.key.direction is Direction.RECEIVE
    assert a.status is TransferStatus.WAITING
    assert a.file_count == 2
    assert a.total_bytes == 500
    assert a.first_name == "anh.jpg"
    assert a.started_at == 100.0


def test_offer_after_known_transfer_does_not_reset_it(tracker):
    from teleport.app.transfers import TransferStatus

    k = key(direction="receive")
    tracker.handle(status_changed(k, done=300, total=500, names=("anh.jpg", "b.txt")), 100.0)
    tracker.handle(offer(k, total=500), 200.0)
    [a] = tracker.get(DEV_A).active
    assert a.status is TransferStatus.TRANSFERRING, "OfferReceived chỉ thêm khi chưa có"
    assert a.shown_bytes == 300
    assert a.started_at == 100.0


def test_finished_keys_are_remembered_up_to_1024_most_recent(tracker):
    """Spec 2.3: nhớ tối đa 1024 khóa đã kết thúc gần nhất."""
    tids = [f"{i:032x}" for i in range(1025)]
    for i, tid in enumerate(tids):
        tracker.handle(finished(key(tid=tid)), float(i))
    # Khóa thứ 2 (index 1) vẫn trong 1024 khóa gần nhất: sự kiện muộn bị bỏ.
    tracker.handle(status_changed(key(tid=tids[1])), 5000.0)
    assert not tracker.get(DEV_A).busy, "khóa nằm trong 1024 khóa gần nhất bị dựng lại"
    # Khóa đầu tiên đã ra khỏi cửa sổ 1024: bộ nhớ có giới hạn, nên nó được thêm lại như lượt mới.
    tracker.handle(status_changed(key(tid=tids[0])), 5001.0)
    assert tracker.get(DEV_A).busy, "bộ nhớ khóa đã kết thúc không có giới hạn 1024"


# =================================================================================================
# A4: nhiều lượt, busy_devices, clear
# =================================================================================================


def test_two_transfers_same_device_newest_first(tracker):
    tracker.handle(status_changed(key(tid=TID_1)), 100.0)
    tracker.handle(status_changed(key(tid=TID_2, direction="receive")), 200.0)
    act = tracker.get(DEV_A)
    assert [a.key.transfer_id for a in act.active] == [TID_2, TID_1]
    assert act.at == 200.0


def test_same_start_time_order_does_not_depend_on_arrival_order():
    from teleport.ui.activity import ActivityTracker

    orders = []
    for tids in ((TID_1, TID_2, TID_3), (TID_3, TID_1, TID_2)):
        t = ActivityTracker()
        for tid in tids:
            t.handle(status_changed(key(tid=tid)), 100.0)
        orders.append([a.key.transfer_id for a in t.get(DEV_A).active])
    assert orders[0] == orders[1]


def test_busy_devices_and_clear(tracker):
    from teleport.ui.activity import EMPTY_ACTIVITY

    tracker.handle(status_changed(key(DEV_A)), 100.0)
    tracker.handle(status_changed(key(DEV_B, TID_2)), 100.0)
    tracker.handle(finished(key(DEV_B, TID_3)), 120.0)
    assert tracker.busy_devices() == frozenset({DEV_A, DEV_B})
    tracker.handle(finished(key(DEV_B, TID_2)), 130.0)
    assert tracker.busy_devices() == frozenset({DEV_A})
    tracker.clear()
    assert tracker.busy_devices() == frozenset()
    assert tracker.get(DEV_A) == EMPTY_ACTIVITY
    assert tracker.get(DEV_B) == EMPTY_ACTIVITY


def test_clear_forgets_finished_keys(tracker):
    """Host mới thì xóa (spec 2.3): khóa của host cũ không chặn lượt cùng khóa của host mới."""
    tracker.handle(finished(), 100.0)
    tracker.clear()
    tracker.handle(status_changed(), 200.0)
    assert tracker.get(DEV_A).busy


def test_empty_activity_and_unknown_device():
    from teleport.ui.activity import EMPTY_ACTIVITY, ActivityTracker

    assert EMPTY_ACTIVITY.active == ()
    assert EMPTY_ACTIVITY.last is None
    assert not EMPTY_ACTIVITY.busy
    assert EMPTY_ACTIVITY.at is None
    assert ActivityTracker().get("f" * 32) == EMPTY_ACTIVITY


def test_activity_at_is_max_of_active_start_and_last(tracker):
    tracker.handle(finished(key(tid=TID_1)), 500.0)
    tracker.handle(status_changed(key(tid=TID_2)), 300.0)
    assert tracker.get(DEV_A).at == 500.0
    tracker.handle(status_changed(key(tid=TID_3)), 900.0)
    assert tracker.get(DEV_A).at == 900.0


def test_activity_dataclasses_are_frozen(tracker):
    import dataclasses

    tracker.handle(status_changed(), 100.0)
    act = tracker.get(DEV_A)
    with pytest.raises(dataclasses.FrozenInstanceError):
        act.last = None  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        act.active[0].shown_bytes = 1  # type: ignore[misc]


# =================================================================================================
# A5: lịch sử
# =================================================================================================


def test_latest_by_device_keeps_first_entry_of_each_device():
    from teleport.app.transfers import Direction, TransferResult
    from teleport.ui.activity import latest_by_device

    new_a = entry(
        DEV_A,
        tid=TID_3,
        direction="receive",
        at=datetime.datetime(2026, 10, 31, 9, 0, tzinfo=datetime.UTC),
        names=("moi.txt", "k.txt"),
        count=2,
    )
    old_a = entry(
        DEV_A, tid=TID_1, at=datetime.datetime(2026, 10, 1, 9, 0, tzinfo=datetime.UTC), names=("cu.txt",)
    )
    b = entry(
        DEV_B,
        tid=TID_2,
        result="cancelled_user",
        at=datetime.datetime(2026, 10, 20, 9, 0, tzinfo=datetime.UTC),
    )
    latest = latest_by_device([new_a, b, old_a])  # mới nhất trước, như `history()`
    assert set(latest) == {DEV_A, DEV_B}
    la = latest[DEV_A]
    assert la.direction is Direction.RECEIVE
    assert la.result is TransferResult.DONE
    assert la.file_count == 2
    assert la.first_name == "moi.txt"
    assert la.at == new_a.finished_at.timestamp()
    assert latest[DEV_B].result is TransferResult.CANCELLED_USER


def test_latest_by_device_unknown_result_becomes_none():
    from teleport.ui.activity import latest_by_device

    latest = latest_by_device([entry(result="ket_qua_moi_cua_ban_sau")])
    assert latest[DEV_A].result is None


def test_latest_by_device_empty_names_gives_no_first_name():
    from teleport.ui.activity import latest_by_device

    latest = latest_by_device([entry(names=(), count=3)])
    assert latest[DEV_A].first_name is None
    assert latest[DEV_A].file_count == 3


def test_latest_by_device_of_nothing_is_empty():
    from teleport.ui.activity import latest_by_device

    assert latest_by_device([]) == {}
    assert latest_by_device(iter(())) == {}


def test_load_history_sets_last_and_returns_changed_ids(tracker):
    from teleport.ui.activity import latest_by_device

    latest = latest_by_device([entry(DEV_A), entry(DEV_B, tid=TID_2)])
    assert tracker.load_history(latest) == frozenset({DEV_A, DEV_B})
    assert tracker.get(DEV_A).last == latest[DEV_A]
    assert not tracker.get(DEV_A).busy


def test_load_history_does_not_override_session_last(tracker):
    """Spec 12.9: mục lịch sử chỉ dùng khi máy **chưa có `last` nào trong phiên** (không so giờ)."""
    from teleport.app.transfers import TransferResult
    from teleport.ui.activity import latest_by_device

    tracker.handle(finished(result="cancelled_user"), 100.0)  # mốc phiên cũ hơn mục lịch sử
    future = datetime.datetime.fromtimestamp(10_000_000, tz=datetime.UTC)
    latest = latest_by_device([entry(DEV_A, at=future), entry(DEV_B, tid=TID_2)])
    changed = tracker.load_history(latest)
    assert DEV_A not in changed
    assert DEV_B in changed
    assert tracker.get(DEV_A).last.result is TransferResult.CANCELLED_USER
    assert tracker.get(DEV_A).last.at == 100.0


def test_load_history_keeps_active_transfers(tracker):
    from teleport.ui.activity import latest_by_device

    tracker.handle(status_changed(), 100.0)
    tracker.load_history(latest_by_device([entry(DEV_A)]))
    act = tracker.get(DEV_A)
    assert act.busy
    assert act.last is not None


# =================================================================================================
# A6: ảnh chụp `transfers()` lúc started
# =================================================================================================


def test_load_snapshot_marks_busy_with_wall_now_and_returns_ids(tracker):
    infos = [info(key(DEV_A, TID_1)), info(key(DEV_B, TID_2, "receive"), status="waiting")]
    assert tracker.load_snapshot(infos, 777.0) == frozenset({DEV_A, DEV_B})
    for dev in (DEV_A, DEV_B):
        act = tracker.get(dev)
        assert act.busy
        assert [a.started_at for a in act.active] == [777.0]
    assert tracker.busy_devices() == frozenset({DEV_A, DEV_B})


def test_load_snapshot_of_nothing_changes_nothing(tracker):
    assert tracker.load_snapshot([], 1.0) == frozenset()
    assert tracker.busy_devices() == frozenset()


def test_load_snapshot_skips_finished_keys(tracker):
    """Spec 12.8: ảnh chụp tới sau `TransferFinished` không dựng lại lượt."""
    tracker.handle(finished(key(DEV_A, TID_1)), 100.0)
    tracker.load_snapshot([info(key(DEV_A, TID_1))], 200.0)
    assert not tracker.get(DEV_A).busy


def test_load_snapshot_keeps_first_seen_time_of_known_transfer(tracker):
    tracker.handle(status_changed(done=600), 100.0)
    tracker.load_snapshot([info(done=10)], 200.0)
    [a] = tracker.get(DEV_A).active
    assert a.started_at == 100.0
    assert a.shown_bytes == 600, "shown_bytes không giảm"


# =================================================================================================
# A7: sự kiện không đổi gì
# =================================================================================================


def test_file_done_and_data_unreachable_change_nothing(tracker):
    tracker.handle(status_changed(), 100.0)
    before = tracker.get(DEV_A)
    assert tracker.handle(file_done(), 200.0) is None
    assert tracker.handle(data_unreachable(DEV_A), 200.0) is None
    assert tracker.get(DEV_A) == before


def test_unknown_update_type_returns_none(tracker):
    assert tracker.handle(object(), 1.0) is None  # type: ignore[arg-type]
    assert tracker.busy_devices() == frozenset()


# =================================================================================================
# Property test: chuỗi sự kiện lộn xộn
# =================================================================================================

_KEYS = [(DEV_A, TID_1, "send"), (DEV_A, TID_2, "receive"), (DEV_B, TID_3, "send")]
_EVENT = st.tuples(
    st.sampled_from(["status", "progress", "resumed", "offer", "finished", "suspended", "file_done"]),
    st.integers(0, len(_KEYS) - 1),
    st.integers(0, 1500),
)


@settings(max_examples=150, deadline=None)
@given(st.lists(_EVENT, max_size=40))
def test_tracker_invariants_for_any_event_order(events):
    from teleport.ui.activity import ActivityTracker

    t = ActivityTracker()
    shown: dict = {}
    ended: set = set()
    for i, (kind, ki, amount) in enumerate(events):
        dev, tid, direction = _KEYS[ki]
        k = key(dev, tid, direction)
        now = float(i)
        if kind == "status":
            ev = status_changed(k, done=min(amount, 1000), total=1000)
        elif kind == "progress":
            ev = progress(k, shown=amount, total=1000)
        elif kind == "resumed":
            ev = resumed(k, done=min(amount, 1000), total=1000)
        elif kind == "offer":
            ev = offer(k, total=1000)
        elif kind == "finished":
            ev = finished(k, total=1000)
            ended.add(k)
        elif kind == "suspended":
            ev = finished(k, result="suspended", total=1000)
        else:
            ev = file_done(k)
        t.handle(ev, now)

        active_keys = set()
        for d in (DEV_A, DEV_B):
            act = t.get(d)
            assert act.busy == bool(act.active)
            assert (d in t.busy_devices()) == act.busy
            starts = [a.started_at for a in act.active]
            assert starts == sorted(starts, reverse=True), "mới nhất trước"
            for a in act.active:
                assert a.key.device_id == d
                assert 0 <= a.shown_bytes <= a.total_bytes
                assert a.shown_bytes >= shown.get(a.key, 0), "shown_bytes giảm"
                shown[a.key] = a.shown_bytes
                active_keys.add(a.key)
        assert not (active_keys & ended), "lượt đã kết thúc bị dựng lại"


# =================================================================================================
# A8: không import Qt
# =================================================================================================


def test_activity_module_does_not_import_pyside6():
    import teleport

    src = str(Path(teleport.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    code = (
        "import sys, teleport.ui.activity; print(sorted(m for m in sys.modules if m.startswith('PySide6')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", f"teleport.ui.activity kéo theo PySide6: {out.stdout.strip()}"
