"""Kết thúc lượt: hủy, lỗi đĩa, nội dung `TransferEnded`, trạng thái báo giao diện.

Spec P2-4 mục 2.5, 3.9, 3.10; AC-14, AC-22 và các ca biên “`control_lost` trong lúc `Reread` hay
`Finalize`”, “`finalized` tới sau `cancel`”, “`space_checked` cho lượt đã bị hủy”, “`decide` hai
lần”. Nguồn: docs/protocol.md §6.3. Viết từ spec, không từ code.
"""

from __future__ import annotations

import pytest

from teleport.core.blocks import BLOCK_SIZE
from teleport.core.filenames import dedupe_offer_names
from teleport.core.messages import Accept, Cancel, DataOpen, Reject, Resume, Verified
from teleport.core.transfer import (
    RECONNECT_GRACE,
    AskUser,
    CloseData,
    CreateTemp,
    Direction,
    DiskOp,
    FailureDetail,
    Finalize,
    Outcome,
    RemoveTemp,
    Reread,
    TransferKey,
    TransferStatus,
)

# ---- AC-14: người dùng hủy ------------------------------------------------------------------------


def _sender_in(tx, state: str):
    s = tx.Send(tx.entries(2, 1), client=False)
    if state == "waiting":
        s.start(accept=False)
    elif state == "queued":
        first = tx.Send(tx.entries(1), T=tx.T2, like=s)
        first.start(accept=False)
        s.start(up=False, accept=False)
        assert s.e.info(s.key).status == TransferStatus.QUEUED
    elif state == "transferring":
        s.start()
        s.resume(0)
        s.data = s.open(0, 1)[0]
    elif state == "reconnecting":
        s.start()
        s.lost()
    return s


@pytest.mark.parametrize("state", ["waiting", "queued", "transferring"])
def test_user_cancels_send_while_connected(tx, state):
    s = _sender_in(tx, state)
    acts = s.call("cancel", s.key)
    assert tx.sent(acts) == [Cancel(transfer_id=s.T, reason="user")]
    end = tx.one(tx.ended(acts), object)
    assert (end.key, end.outcome, end.reason, end.by_peer, end.detail) == (
        s.key,
        Outcome.CANCELLED,
        "user",
        False,
        None,
    )
    if state == "transferring":
        assert CloseData(device_id=tx.PEER, data_id=s.data) in acts
    assert s.e.info(s.key) is None
    assert s.call("cancel", s.key) == []


def test_user_cancels_send_while_offline_sends_cancel_on_reconnect(tx):
    s = _sender_in(tx, "reconnecting")
    acts = s.call("cancel", s.key)
    assert tx.sent(acts) == []
    assert tx.one(tx.ended(acts), object).reason == "user"
    assert tx.sent(s.up(cid=2)) == [Cancel(transfer_id=s.T, reason="user")]


def _receiver_in(tx, state: str):
    if state == "queued":
        head = tx.Recv(tx.entries(1), T=tx.T2)
        head.to_asking()
        r = tx.Recv(tx.entries(2, 1), like=head)
        r.send_offer()
        return r
    r = tx.Recv(tx.entries(2, 1))
    if state == "checking":
        r.up()
        r.send_offer()
    elif state == "asking":
        r.to_asking()
    else:
        r.accept()
        if state == "verifying":
            did, _acts = r.open()
            r.sync_all(r.feed(did, 0, range(2)))
            assert tx.one(r.complete(0), Reread)
        elif state == "reconnecting":
            r.lost()
    return r


@pytest.mark.parametrize("state", ["checking", "asking", "queued"])
def test_user_cancel_of_unanswered_offer_rejects_and_remembers(tx, state):
    r = _receiver_in(tx, state)
    acts = r.call("cancel", r.key)
    rej = Reject(transfer_id=r.T, reason="user")
    assert tx.sent(acts) == [rej]
    end = tx.one(tx.ended(acts), object)
    assert (end.outcome, end.reason, end.by_peer, end.reject) == (Outcome.REJECTED, "user", False, rej)
    assert not tx.of(acts, (RemoveTemp, CreateTemp))
    assert tx.sent(r.send_offer()) == [rej]


@pytest.mark.parametrize("state", ["transferring", "verifying"])
def test_user_cancel_of_running_receive_cancels_and_removes_temp(tx, state):
    r = _receiver_in(tx, state)
    acts = r.call("cancel", r.key)
    assert tx.sent(acts) == [Cancel(transfer_id=r.T, reason="user")]
    assert tx.one(acts, RemoveTemp).transfer_id == r.T
    end = tx.one(tx.ended(acts), object)
    assert (end.outcome, end.reason, end.by_peer, end.detail) == (Outcome.CANCELLED, "user", False, None)


def test_user_cancel_of_receive_while_offline_sends_cancel_on_reconnect(tx):
    r = _receiver_in(tx, "reconnecting")
    acts = r.call("cancel", r.key)
    assert tx.sent(acts) == []
    assert tx.one(acts, RemoveTemp).transfer_id == r.T
    assert tx.sent(r.up(cid=2)) == [Cancel(transfer_id=r.T, reason="user")]


def test_user_cancel_of_unknown_transfer_does_nothing(tx):
    e = tx.tr.TransferEngine(tx.ME, receiving=True)
    for d in (Direction.SEND, Direction.RECEIVE):
        assert e.cancel(TransferKey(tx.PEER, d, tx.T1), 1.0) == []


@pytest.mark.parametrize(
    "state", ["checking", "asking", "queued", "transferring", "verifying", "reconnecting"]
)
def test_peer_cancel_of_receive_never_answers(tx, state):
    r = _receiver_in(tx, state)
    if state == "reconnecting":
        r.up(cid=2)
    acts = r.msg(Cancel(transfer_id=r.T, reason="source_changed"))
    assert tx.sent(acts) == []
    end = tx.one(tx.ended(acts), object)
    assert (end.key, end.outcome, end.reason, end.by_peer, end.detail) == (
        r.key,
        Outcome.CANCELLED,
        "source_changed",
        True,
        None,
    )
    owns_temp = state in ("transferring", "verifying", "reconnecting")
    assert bool(tx.of(acts, RemoveTemp)) == owns_temp


@pytest.mark.parametrize("state", ["waiting", "queued", "transferring"])
def test_peer_cancel_of_send_never_answers(tx, state):
    s = _sender_in(tx, state)
    acts = s.msg(Cancel(transfer_id=s.T, reason="error"))
    assert tx.sent(acts) == []
    end = tx.one(tx.ended(acts), object)
    assert (end.outcome, end.reason, end.by_peer, end.detail) == (Outcome.CANCELLED, "error", True, None)
    # Lý do lạ đúng dạng vẫn được nhận và giữ nguyên.
    s2 = tx.Send(tx.entries(1), T=tx.T3, like=s)
    s2.start(up=False)
    assert (
        tx.one(tx.ended(s2.msg(Cancel(transfer_id=s2.T, reason="ly_do_moi"))), object).reason == "ly_do_moi"
    )


def test_cancel_of_unknown_or_ended_transfer_is_ignored(tx):
    s = tx.Send(tx.entries(1))
    s.start()
    assert s.msg(Cancel(transfer_id=tx.T3, reason="user")) == []
    s.msg(Cancel(transfer_id=s.T, reason="user"))
    assert s.msg(Cancel(transfer_id=s.T, reason="user")) == []


def test_tombstoned_send_answers_resume_and_data_open_with_same_reason(tx):
    s = tx.Send(tx.entries(2), client=False)
    s.start()
    s.msg(Cancel(transfer_id=s.T, reason="source_changed"))
    assert tx.sent(s.resume(0)) == [Cancel(transfer_id=s.T, reason="source_changed")]
    _did, acts = s.open(0, 1)
    assert [type(a) for a in acts] == [tx.tr.SendMessage, CloseData]
    assert acts[0].message == Cancel(transfer_id=s.T, reason="source_changed")
    for m in (
        Accept(transfer_id=s.T),
        Reject(transfer_id=s.T, reason="user"),
        Verified(transfer_id=s.T, file_index=0),
        Cancel(transfer_id=s.T, reason="user"),
    ):
        assert s.msg(m) == []


def test_tombstoned_receive_answers_offer_complete_and_data_open_with_same_reason(tx):
    r = tx.Recv(tx.entries(2))
    r.accept()
    r.call("storage_failed", tx.PEER, r.T, DiskOp.SYNC)  # bia mộ "error"
    assert tx.sent(r.send_offer()) == [Cancel(transfer_id=r.T, reason="error")]
    assert tx.sent(r.complete(0)) == [Cancel(transfer_id=r.T, reason="error")]
    did = 777
    acts = r.call("incoming_data", tx.PEER, did, DataOpen(transfer_id=r.T, file_index=0, resume_seq=1))
    assert [type(a) for a in acts] == [tx.tr.SendMessage, CloseData]
    assert acts[0].message == Cancel(transfer_id=r.T, reason="error")
    assert acts[1] == CloseData(device_id=tx.PEER, data_id=did)
    assert r.msg(Cancel(transfer_id=r.T, reason="user")) == []


@pytest.mark.parametrize("op", list(DiskOp))
def test_storage_failure_cancels_and_removes_only_owned_temp(tx, op):
    r = tx.Recv(tx.entries(2))
    acts = r.accept()
    assert tx.one(acts, CreateTemp).transfer_id == r.T
    acts = r.call("storage_failed", tx.PEER, r.T, op)
    assert tx.sent(acts) == [Cancel(transfer_id=r.T, reason="error")]
    end = tx.one(tx.ended(acts), object)
    assert (end.outcome, end.reason, end.by_peer, end.detail, end.file_index) == (
        Outcome.CANCELLED,
        "error",
        False,
        FailureDetail.DISK,
        0,
    )
    assert bool(tx.of(acts, RemoveTemp)) == (op != DiskOp.CREATE_TEMP)


def test_storage_failed_for_unknown_transfer_is_ignored(tx):
    r = tx.Recv(tx.entries(1))
    r.up()
    assert r.call("storage_failed", tx.PEER, r.T, DiskOp.WRITE) == []


def test_remove_temp_is_last_disk_action_of_transfer(tx):
    r = tx.Recv(tx.entries(3))
    r.accept()
    did, _ = r.open()
    r.feed(did, 0, [0, 1])
    acts = r.msg(Cancel(transfer_id=r.T, reason="user"))
    disk = (CreateTemp, tx.tr.OpenPart, tx.tr.WriteBlock, tx.tr.SyncPart, Reread, Finalize, RemoveTemp)
    kinds = [type(a) for a in acts if isinstance(a, disk)]
    assert kinds[-1] is RemoveTemp
    assert kinds.count(RemoveTemp) == 1


def test_cancel_of_same_transfer_id_from_other_device_touches_only_its_own_transfer(tx):
    """Kịch bản 9 của spec mục 6: hai thiết bị gửi cùng `transfer_id`. Lượt của OTHER bị hủy trước khi
    `CreateTemp` của nó có kết quả → `RemoveTemp` mang `device_id` của OTHER (mục 2.4; app chỉ xóa thư
    mục tạm mà chính lượt đó đã tạo, mục 2.8 bước 5), không hành động nào nhắm lượt của PEER, và lượt
    của PEER vẫn nhận khối bình thường (spec mục 6: “Lượt của thiết bị khác không bị đổi”).
    """
    p = tx.Recv(tx.entries(3))
    p.accept()
    d1, _ = p.open()
    p.feed(d1, 0, [0])
    o = tx.Recv(tx.entries(2), peer=tx.OTHER, like=p, cid=2)
    assert o.T == p.T
    o.to_asking()
    acts = o.call("decide", tx.OTHER, o.T, True)
    assert tx.one(acts, CreateTemp) == CreateTemp(device_id=tx.OTHER, transfer_id=o.T)
    before = p.e.info(p.key)
    acts = o.call("cancel", o.key)  # chưa có kết quả `CreateTemp`
    assert tx.one(acts, RemoveTemp) == RemoveTemp(device_id=tx.OTHER, transfer_id=o.T)
    assert {a.device_id for a in acts if not isinstance(a, tx.tr.Report)} == {tx.OTHER}
    assert [e for e in tx.events(acts) if getattr(e, "key", None) == p.key] == []
    assert p.e.info(p.key) == before
    assert [a.index for a in tx.of(p.feed(d1, 0, [1]), tx.tr.WriteBlock)] == [1]


# ---- Ca biên: kết quả đĩa tới muộn, trả lời người dùng tới muộn -------------------------------------


def test_late_reread_result_after_expiry_is_ignored(tx):
    r = tx.Recv(tx.entries(2))
    r.accept()
    did, _ = r.open()
    r.sync_all(r.feed(did, 0, range(2)))
    assert tx.one(r.complete(0), Reread)
    r.lost(now=100.0)
    assert tx.one(tx.ended(r.tick(now=100.0 + RECONNECT_GRACE)), object).reason == "expired"
    assert r.call("reread_done", tx.PEER, r.T, 0, [], r.root(0)) == []


def test_late_finalized_after_expiry_or_cancel_is_ignored(tx):
    for how in ("expire", "cancel"):
        r = tx.Recv(tx.entries(1, 1), T=tx.T1 if how == "expire" else tx.T2)
        r.accept()
        did, _ = r.open()
        r.sync_all(r.feed(did, 0, [0]))
        r.complete(0)
        fin = tx.one(r.call("reread_done", tx.PEER, r.T, 0, [], r.root(0)), Finalize)
        if how == "expire":
            r.lost(now=500.0)
            r.tick(now=500.0 + RECONNECT_GRACE)
        else:
            r.call("cancel", r.key)
        acts = r.call("finalized", tx.PEER, r.T, 0, fin.name)
        assert acts == []


def test_space_checked_for_cancelled_offer_is_ignored(tx):
    r = tx.Recv(tx.entries(1))
    r.up()
    r.send_offer()
    r.msg(Cancel(transfer_id=r.T, reason="user"))
    assert r.call("space_checked", tx.PEER, r.T, tx.FREE, None) == []


def test_decide_twice_only_first_counts(tx):
    r = tx.Recv(tx.entries(1))
    r.to_asking()
    first = r.call("decide", tx.PEER, r.T, True)
    assert tx.one(first, CreateTemp)
    assert r.call("decide", tx.PEER, r.T, True) == []
    assert r.call("decide", tx.PEER, r.T, False) == []
    assert r.e.info(r.key).status == TransferStatus.TRANSFERRING


def test_decide_before_space_check_is_ignored(tx):
    r = tx.Recv(tx.entries(1))
    r.up()
    r.send_offer()
    assert r.call("decide", tx.PEER, r.T, True) == []
    assert r.e.info(r.key).status == TransferStatus.WAITING


def test_accept_while_offline_sends_accept_and_resume_on_connect(tx):
    r = tx.Recv(tx.entries(2))
    r.to_asking()
    r.lost()
    acts = r.call("decide", tx.PEER, r.T, True)
    assert tx.sent(acts) == []
    assert tx.one(acts, CreateTemp)
    acts = r.up(cid=2)
    assert tx.sent(acts)[0] == Accept(transfer_id=r.T)
    assert tx.one(tx.sent(acts, Resume), object).seq == 1


# ---- AC-22: nội dung TransferEnded -----------------------------------------------------------------


def test_receiver_ended_info_is_last_snapshot(tx):
    raw = ["a.txt", "A.TXT", "x/../b.bin"]
    files = tx.entries(1, 3, 2, names=raw)
    r = tx.Recv(files)
    r.accept()
    r.finish_file(0)
    did, _ = r.open()
    r.feed(did, 1, [0, 1])
    r.sync_all(r.call("data_closed", tx.PEER, did, False))  # đẩy lô sớm rồi fsync
    acts = r.msg(Cancel(transfer_id=r.T, reason="user"))
    end = tx.one(tx.ended(acts), object)
    assert end.file_index == 1
    info = end.info
    assert info.key == r.key
    assert info.files == files
    assert info.names == tuple(dedupe_offer_names(raw))
    assert info.file_index == 1
    assert info.done_bytes == files[0].size + 2 * BLOCK_SIZE
    assert info.total_bytes == sum(f.size for f in files)
    assert info.status == TransferStatus.TRANSFERRING
    assert r.e.info(r.key) is None
    assert r.e.transfers() == ()


def test_sender_ended_file_index_is_smallest_unverified_served_file(tx):
    s = tx.Send(tx.entries(1, 2, 1))
    s.start()
    s.serve_file(0)
    s.msg(Verified(transfer_id=s.T, file_index=0))
    s.resume(1)
    acts = s.msg(Cancel(transfer_id=s.T, reason="error"))
    end = tx.one(tx.ended(acts), object)
    assert end.file_index == 1
    assert end.info.file_index == 1  # số file đã `verified`
    assert end.info.names == tuple(f.name for f in s.files)
    assert end.info.status == TransferStatus.TRANSFERRING


def test_sender_ended_before_any_resume_has_no_file_index(tx):
    s = tx.Send(tx.entries(1, 1))
    s.start(accept=False)
    end = tx.one(tx.ended(s.msg(Cancel(transfer_id=s.T, reason="user"))), object)
    assert end.file_index is None
    assert end.info.status == TransferStatus.WAITING
    assert end.info.done_bytes == 0


def test_receiver_ended_before_running_has_no_file_index(tx):
    r = tx.Recv(tx.entries(1, 1))
    r.to_asking()
    end = tx.one(tx.ended(r.msg(Cancel(transfer_id=r.T, reason="user"))), object)
    assert end.file_index is None
    assert end.info.status == TransferStatus.WAITING


def test_expiry_uses_same_file_index_rule_and_status_before_end(tx):
    r = tx.Recv(tx.entries(1, 2))
    r.accept()
    r.finish_file(0)
    r.lost(now=100.0)
    end = tx.one(tx.ended(r.tick(now=100.0 + RECONNECT_GRACE)), object)
    assert end.file_index == 1
    assert end.info.status == TransferStatus.RECONNECTING
    s = tx.Send(tx.entries(1, 1))
    s.start()
    s.serve_file(0)
    s.lost(now=100.0)
    end = tx.one(tx.ended(s.tick(now=100.0 + RECONNECT_GRACE)), object)
    assert end.file_index == 0
    assert end.info.status == TransferStatus.RECONNECTING


def test_out_of_range_file_index_becomes_none(tx):
    s = tx.Send(tx.entries(1, 1))
    s.start()
    end = tx.one(tx.ended(s.msg(Verified(transfer_id=s.T, file_index=9))), object)
    assert end.file_index is None


@pytest.mark.parametrize(("peer_index", "expected"), [(1, 1), (5, None)])
def test_peer_file_too_large_reject_keeps_valid_file_index(tx, peer_index, expected):
    s = tx.Send(tx.entries(1, 1))
    s.start(accept=False)
    rej = Reject(transfer_id=s.T, reason="file_too_large", file_index=peer_index, max_size=10)
    end = tx.one(tx.ended(s.msg(rej)), object)
    assert (end.outcome, end.reason, end.by_peer, end.reject, end.file_index) == (
        Outcome.REJECTED,
        "file_too_large",
        True,
        rej,
        expected,
    )


def test_self_file_too_large_reject_reports_violating_file(tx):
    r = tx.Recv(tx.entries(1, 1, 3))
    r.up()
    r.send_offer()
    end = tx.one(tx.ended(r.call("space_checked", tx.PEER, r.T, tx.FREE, tx.size_of(2))), object)
    assert end.file_index == 2
    assert end.info.status == TransferStatus.WAITING


def test_each_transfer_ends_exactly_once(tx):
    r = tx.Recv(tx.entries(1))
    r.accept()
    r.finish_all()
    r.msg(Cancel(transfer_id=r.T, reason="user"))
    r.call("cancel", r.key)
    r.lost()
    r.tick(now=r.now + RECONNECT_GRACE + 1)
    assert len([e for e in tx.ended(r.all_actions()) if e.key == r.key]) == 1


# ---- Trạng thái (§3.10) -----------------------------------------------------------------------------


def test_sender_status_sequence(tx):
    s1 = tx.Send(tx.entries(1), T=tx.T1)
    s2 = tx.Send(tx.entries(1), T=tx.T2, like=s1)
    s1.up()
    assert tx.statuses(s1.start(up=False, accept=False), s1.key) == [TransferStatus.WAITING]
    acts = s2.start(up=False, accept=False)
    assert tx.statuses(acts, s2.key) == [TransferStatus.QUEUED]  # lượt cũ hơn tới cùng thiết bị (A4)
    acts = s1.msg(Accept(transfer_id=s1.T))
    assert tx.statuses(acts, s1.key) == [TransferStatus.TRANSFERRING]
    acts = s1.lost()
    assert tx.statuses(acts, s1.key) == [TransferStatus.RECONNECTING]
    assert tx.statuses(acts, s2.key) == [TransferStatus.RECONNECTING]
    acts = s1.up(cid=2)
    assert tx.statuses(acts, s1.key) == [TransferStatus.TRANSFERRING]
    assert tx.statuses(acts, s2.key) == [TransferStatus.QUEUED]
    acts = s1.msg(Cancel(transfer_id=s1.T, reason="user"))
    assert tx.statuses(acts, s2.key) == [TransferStatus.WAITING]
    # G5: StatusChanged đứng sau TransferEnded.
    reports = [a.event for a in acts if isinstance(a, tx.tr.Report)]
    assert isinstance(reports[-1], tx.tr.StatusChanged)


def test_receiver_status_sequence(tx):
    r1 = tx.Recv(tx.entries(2))
    r2 = tx.Recv(tx.entries(1), T=tx.T2, like=r1)
    r1.up()
    assert tx.statuses(r1.send_offer(), r1.key) == [TransferStatus.WAITING]
    assert tx.statuses(r2.send_offer(), r2.key) == [TransferStatus.QUEUED]
    assert tx.statuses(r1.call("space_checked", tx.PEER, r1.T, tx.FREE, None), r1.key) == []  # vẫn WAITING
    assert tx.statuses(r1.call("decide", tx.PEER, r1.T, True), r1.key) == [TransferStatus.TRANSFERRING]
    did, _ = r1.open()
    r1.sync_all(r1.feed(did, 0, range(2)))
    assert tx.statuses(r1.complete(0), r1.key) == [TransferStatus.VERIFYING]
    acts = r1.call("reread_done", tx.PEER, r1.T, 0, [], r1.root(0))
    assert tx.statuses(acts, r1.key) == []  # đang đổi tên: vẫn VERIFYING
    assert r1.e.info(r1.key).status == TransferStatus.VERIFYING
    acts = r1.lost()
    assert tx.statuses(acts, r1.key) == [TransferStatus.RECONNECTING]
    assert tx.statuses(acts, r2.key) == []  # QUEUED vẫn QUEUED khi mất kết nối


def test_status_reports_only_on_change_and_transfers_keep_creation_order(tx):
    s1 = tx.Send(tx.entries(1), T=tx.T3)
    s2 = tx.Send(tx.entries(1), T=tx.T1, like=s1)
    s1.start(accept=False)
    s2.start(up=False, accept=False)
    assert [i.key for i in s1.e.transfers()] == [s1.key, s2.key]
    assert tx.events(s1.tick()) == []
    assert tx.events(s1.msg(Accept(transfer_id=s1.T)), tx.tr.StatusChanged) == [
        tx.tr.StatusChanged(key=s1.key, status=TransferStatus.TRANSFERRING)
    ]
    assert tx.events(s1.msg(Accept(transfer_id=s1.T))) == []


def test_info_reports_totals_and_names(tx):
    raw = ["CON", "con.txt"]
    files = tx.entries(2, 0, names=raw)
    r = tx.Recv(files)
    r.up()
    r.send_offer()
    info = r.e.info(r.key)
    assert info.key == r.key
    assert info.files == files
    assert info.names == tuple(dedupe_offer_names(raw))
    assert info.total_bytes == tx.size_of(2)
    assert info.done_bytes == 0
    s = tx.Send(files, T=tx.T2, like=r)
    s.start(up=False, accept=False)
    sinfo = s.e.info(s.key)
    assert sinfo.names == tuple(raw)
    assert sinfo.file_index == 0
    assert sinfo.key.direction == Direction.SEND
    ask = tx.one(r.call("space_checked", tx.PEER, r.T, tx.FREE, None), AskUser)
    assert ask.names == info.names
