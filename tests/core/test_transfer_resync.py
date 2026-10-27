"""Đồng bộ lại (A1), chờ nối lại (A2) và bảng nhớ lượt đã kết thúc.

Spec P2-4 mục 3.2, 3.3, 3.9, 3.14; AC-6, AC-15, AC-16. Nguồn: docs/protocol.md §6.2, §6.3. Viết từ
spec, không từ code; mọi thời gian là thời gian ảo.
"""

from __future__ import annotations

import pytest

import teleport.core.transfer as transfer
from teleport.core.bitfield import Bitfield
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.messages import (
    Accept,
    Cancel,
    Complete,
    DataOpen,
    Offer,
    Reject,
    Resume,
    Verified,
)
from teleport.core.transfer import (
    MAX_REMEMBERED,
    REMEMBER_TTL,
    AskUser,
    CheckSpace,
    CloseData,
    CreateTemp,
    Direction,
    OpenData,
    Outcome,
    RemoveTemp,
    SendBlocks,
    TransferEngine,
    TransferIdInUse,
    TransferKey,
    TransferStatus,
)

EPS = 1e-6
# Giá trị thật của hằng, đọc lúc import (trước mọi monkeypatch): spec-p3 P3-0, 24 giờ.
REAL_GRACE = transfer.RECONNECT_GRACE


@pytest.fixture(params=[REAL_GRACE, 2.0], ids=["grace-that", "grace-va-nho"])
def grace(request, monkeypatch):
    """Chạy ca hết hạn với `RECONNECT_GRACE` thật và với hằng vá nhỏ (engine đọc hằng lúc dùng)."""
    monkeypatch.setattr(transfer, "RECONNECT_GRACE", request.param)
    return request.param


# ---- AC-6: đồng bộ lại ---------------------------------------------------------------------------


def test_sender_resync_sends_unsent_cancels_then_offers_then_completes(tx):
    s0 = tx.Send(tx.entries(1), T=tx.T1)
    s1 = tx.Send(tx.entries(1, 1), T=tx.T2, like=s0)
    s2 = tx.Send(tx.entries(2), T=tx.T3, like=s0)
    s0.up()
    s0.start(up=False, accept=False)
    s1.start(up=False, accept=True)
    s2.start(up=False, accept=False)
    s1.serve_file(0)
    s1.msg(Verified(transfer_id=s1.T, file_index=0))
    s1.serve_file(1)  # file 1 có root, chưa `verified`
    s0.lost()
    acts = s0.call("cancel", s0.key)  # hủy lúc offline: chưa gửi được gì
    assert tx.sent(acts) == []
    assert tx.one(tx.ended(acts), object).reason == "user"
    acts = s0.up(cid=2)
    assert tx.sent(acts) == [
        Cancel(transfer_id=s0.T, reason="user"),
        s1.offer,
        s2.offer,
        Complete(transfer_id=s1.T, file_index=1, merkle_root=s1.root(1).hex()),
    ]
    assert all(a.control_id == 2 for a in tx.of(acts, tx.tr.SendMessage))
    # Bia mộ chỉ được gửi `cancel` một lần.
    s0.lost()
    acts = s0.up(cid=3)
    assert tx.sent(acts) == [
        s1.offer,
        s2.offer,
        Complete(transfer_id=s1.T, file_index=1, merkle_root=s1.root(1).hex()),
    ]


def test_sender_resync_without_disconnect_also_resends_everything(tx):
    s = tx.Send(tx.entries(1, 1))
    s.start()
    s.serve_file(0)
    acts = s.up(cid=2)  # kết nối thay thế, không có control_lost
    assert tx.sent(acts) == [s.offer, Complete(transfer_id=s.T, file_index=0, merkle_root=s.root(0).hex())]


def test_tombstone_made_while_connected_is_not_resent_on_resync(tx):
    s0 = tx.Send(tx.entries(1), T=tx.T1)
    s1 = tx.Send(tx.entries(1), T=tx.T2, like=s0)
    s0.start()
    s1.start(up=False)
    assert tx.sent(s0.call("cancel", s0.key)) == [Cancel(transfer_id=s0.T, reason="user")]
    s0.lost()
    assert tx.sent(s0.up(cid=2)) == [s1.offer]


@pytest.mark.parametrize("client", [False, True])
def test_receiver_resync_sends_accept_then_resume_with_seq_one(tx, client):
    r = tx.Recv(tx.entries(3), client=client)
    r.accept()
    r.lost()
    acts = r.up(cid=2)
    assert tx.sent(acts) == [
        Accept(transfer_id=r.T),
        Resume(transfer_id=r.T, file_index=0, seq=1, bitfield=Bitfield(3).to_base64()),
    ]
    ods = tx.of(acts, OpenData)
    if client:
        assert len(ods) == 1
        assert ods[0].control_id == 2
        assert ods[0].message == DataOpen(transfer_id=r.T, file_index=0, resume_seq=1)
        i_res = tx.index_of(
            acts, lambda a: isinstance(a, tx.tr.SendMessage) and isinstance(a.message, Resume)
        )
        assert i_res < acts.index(ods[0])  # G3
    else:
        assert ods == []


def test_receiver_resync_reports_progress_in_resume(tx):
    r = tx.Recv(tx.entries(40))
    r.accept()
    did, _ = r.open()
    r.sync_all(r.feed(did, 0, range(16)))
    r.lost()
    acts = r.up(cid=2)
    res = tx.one(tx.sent(acts, Resume), object)
    assert res.seq == 1
    assert tx.bits_on(res.bitfield, 40) == set(range(16))


def test_receiver_resync_ignores_offers_waiting_or_queued(tx):
    r1 = tx.Recv(tx.entries(1))
    r1.to_asking()
    r2 = tx.Recv(tx.entries(1), T=tx.T2, like=r1)
    r2.send_offer()
    r1.lost()
    acts = r1.up(cid=2)
    assert tx.sent(acts) == []
    assert not tx.of(acts, (AskUser, CheckSpace))


def test_receiver_connection_switch_closes_old_data_and_restarts_seq(tx):
    r = tx.Recv(tx.entries(3))
    r.accept()
    did, _ = r.open()
    acts = r.up(cid=2)  # LinkSwitched: không có control_lost
    assert CloseData(device_id=tx.PEER, data_id=did) in acts
    res = tx.one(tx.sent(acts, Resume), object)
    assert res.seq == 1
    # Message trên kết nối điều khiển cũ bị bỏ qua.
    assert r.complete(0, cid=1) == []
    assert r.msg(Cancel(transfer_id=r.T, reason="user"), cid=1) == []
    assert r.e.info(r.key) is not None
    # Kết nối dữ liệu cũ đã bị quên.
    assert r.call("block_received", tx.PEER, did, 0, r.dg(0, 0)) == [
        CloseData(device_id=tx.PEER, data_id=did)
    ]


@pytest.mark.parametrize("client", [False, True])
def test_sender_connection_switch_closes_data_and_forgets_old_seq(tx, client):
    s = tx.Send(tx.entries(3), client=client)
    s.start()
    s.resume(0, seq=5)
    did, acts = s.open(0, 5)
    assert tx.one(acts, SendBlocks).data_id == did
    acts = s.up(cid=2)
    assert CloseData(device_id=tx.PEER, data_id=did) in acts
    # `max_seq` về 0: `resume` seq 1 trên kết nối mới được phục vụ.
    acts = s.resume(0, seq=1)
    if client:
        assert tx.one(acts, OpenData).message == DataOpen(transfer_id=s.T, file_index=0, resume_seq=1)
        assert tx.one(acts, OpenData).control_id == 2
    else:
        _, acts = s.open(0, 1)
        assert tx.one(acts, SendBlocks).transfer_id == s.T
    # Message trên kết nối cũ bị bỏ qua.
    assert s.msg(Resume(transfer_id=s.T, file_index=0, seq=9, bitfield=Bitfield(3).to_base64()), cid=1) == []
    assert (
        s.call("outgoing_data_failed", tx.PEER, 1, DataOpen(transfer_id=s.T, file_index=0, resume_seq=9))
        == []
    )


def test_control_up_with_same_id_is_a_no_op(tx):
    r = tx.Recv(tx.entries(2))
    r.accept()
    did, _ = r.open()
    assert r.up() == []
    assert r.call("block_received", tx.PEER, did, 0, r.dg(0, 0))  # vẫn là kết nối hiện tại


def test_control_lost_of_old_connection_is_ignored(tx):
    r = tx.Recv(tx.entries(2))
    r.accept()
    r.up(cid=2)
    assert r.call("control_lost", tx.PEER, 1) == []
    assert r.e.info(r.key).status == TransferStatus.TRANSFERRING


# ---- AC-15: chờ nối lại (A2) --------------------------------------------------------------------


@pytest.mark.parametrize("side", ["recv", "send"])
def test_control_lost_reports_reconnecting_and_quick_return_resyncs(tx, side, grace):
    d = tx.Recv(tx.entries(1)) if side == "recv" else tx.Send(tx.entries(1))
    if side == "recv":
        d.accept()
    else:
        d.start()
    acts = d.lost(now=50.0)
    assert tx.statuses(acts, d.key) == [TransferStatus.RECONNECTING]
    assert tx.ended(d.tick(now=50.0 + grace - 1)) == []
    acts = d.up(cid=2, now=50.0 + grace - 1)
    assert tx.statuses(acts, d.key) == [TransferStatus.TRANSFERRING]
    # Hạn A2 đã bỏ. Bên nhận là server chỉ còn hạn chờ `data_open` của `resume` thiếu khối vừa gửi
    # (protocol §4.1: “Server là bên nhận… trong thời hạn đó”).
    expected = d.now + transfer.DATA_OPEN_WAIT if side == "recv" else None
    assert d.e.next_wakeup() == expected
    assert tx.ended(d.tick(now=50.0 + grace)) == []
    if side == "recv":
        acts = d.finish_all()
    else:
        d.serve_file(0)
        acts = d.msg(Verified(transfer_id=d.T, file_index=0))
    assert tx.one(tx.ended(acts), object).outcome == Outcome.DONE


def test_running_receive_expires_exactly_at_grace(tx, grace):
    r = tx.Recv(tx.entries(2))
    r.accept()
    r.lost(now=100.0)
    assert r.e.next_wakeup() == 100.0 + grace
    assert tx.ended(r.tick(now=100.0 + grace - EPS)) == []
    assert r.e.info(r.key) is not None
    acts = r.tick(now=100.0 + grace)
    end = tx.one(tx.ended(acts), object)
    assert (end.key, end.outcome, end.reason, end.by_peer, end.detail) == (
        r.key,
        Outcome.CANCELLED,
        "expired",
        False,
        None,
    )
    assert tx.one(acts, RemoveTemp).transfer_id == r.T
    assert tx.sent(acts) == []
    assert r.e.info(r.key) is None


def test_unanswered_offer_expires_too(tx, grace):
    r = tx.Recv(tx.entries(2))
    r.to_asking()
    acts = r.lost(now=100.0)
    assert tx.statuses(acts, r.key) == []  # ASKING vẫn là WAITING khi mất kết nối
    assert tx.ended(r.tick(now=100.0 + grace - EPS)) == []
    acts = r.tick(now=100.0 + grace)
    end = tx.one(tx.ended(acts), object)
    assert (end.outcome, end.reason, end.by_peer) == (Outcome.CANCELLED, "expired", False)
    assert not tx.of(acts, RemoveTemp)
    # Người dùng trả lời muộn: không tạo gì.
    assert r.call("decide", tx.PEER, r.T, True) == []


def test_sending_side_expires_exactly_at_grace(tx, grace):
    s = tx.Send(tx.entries(2))
    s.start()
    s.lost(now=10.0)
    assert tx.ended(s.tick(now=10.0 + grace - EPS)) == []
    end = tx.one(tx.ended(s.tick(now=10.0 + grace)), object)
    assert (end.key, end.reason, end.by_peer) == (s.key, "expired", False)


def test_send_without_connection_starts_the_grace_clock(tx, grace):
    s1 = tx.Send(tx.entries(1), T=tx.T1)
    s2 = tx.Send(tx.entries(1), T=tx.T2, like=s1)
    acts = s1.call("send", tx.PEER, s1.T, list(s1.files), now=5.0)
    assert tx.sent(acts) == []
    assert tx.statuses(acts, s1.key) == [TransferStatus.RECONNECTING]
    s2.call("send", tx.PEER, s2.T, list(s2.files), now=5.0 + grace / 2)  # không dời mốc offline_since
    assert s1.e.next_wakeup() == 5.0 + grace
    assert tx.ended(s1.tick(now=5.0 + grace - EPS)) == []
    acts = s1.tick(now=5.0 + grace)
    assert {e.key for e in tx.ended(acts)} == {s1.key, s2.key}
    assert {e.reason for e in tx.ended(acts)} == {"expired"}


def test_grace_clock_restarts_after_all_transfers_ended(tx, grace):
    s1 = tx.Send(tx.entries(1), T=tx.T1)
    s1.call("send", tx.PEER, s1.T, list(s1.files), now=5.0)
    s1.tick(now=5.0 + grace)
    s2 = tx.Send(tx.entries(1), T=tx.T2, like=s1)
    t2 = 5.0 + grace + 50.0
    s2.call("send", tx.PEER, s2.T, list(s2.files), now=t2)
    assert tx.ended(s2.tick(now=t2 + grace - EPS)) == []
    assert tx.one(tx.ended(s2.tick(now=t2 + grace)), object).key == s2.key


def test_send_while_offline_sends_offer_on_connect(tx):
    s = tx.Send(tx.entries(1))
    s.call("send", tx.PEER, s.T, list(s.files), now=5.0)
    acts = s.up(now=6.0)
    assert tx.sent(acts) == [s.offer]
    assert s.e.next_wakeup() is None


def test_expired_sender_sends_cancel_expired_on_next_connection(tx, grace):
    s = tx.Send(tx.entries(2))
    s.start()
    s.lost(now=10.0)
    s.tick(now=10.0 + grace)
    acts = s.up(cid=2, now=10.0 + grace + 90.0)
    assert tx.sent(acts) == [Cancel(transfer_id=s.T, reason="expired")]
    # Message tới sau đó: `cancel` cùng lý do.
    acts = s.resume(0)
    assert tx.sent(acts) == [Cancel(transfer_id=s.T, reason="expired")]
    s.lost(now=10.0 + grace + 100.0)
    assert tx.sent(s.up(cid=3, now=10.0 + grace + 101.0)) == []


def test_expired_receiver_sends_cancel_expired_on_next_connection(tx, grace):
    r = tx.Recv(tx.entries(2))
    r.accept()
    r.lost(now=10.0)
    r.tick(now=10.0 + grace)
    acts = r.up(cid=2, now=10.0 + grace + 90.0)
    assert tx.sent(acts) == [Cancel(transfer_id=r.T, reason="expired")]
    assert tx.sent(r.send_offer()) == [Cancel(transfer_id=r.T, reason="expired")]
    assert tx.sent(r.complete(0)) == [Cancel(transfer_id=r.T, reason="expired")]


def test_expiry_counts_from_receiver_control_lost_not_from_last_message(tx, grace):
    r = tx.Recv(tx.entries(2))
    r.accept()
    r.tick(now=500.0)  # thời gian trôi khi còn kết nối: không tính
    r.lost(now=600.0)
    assert tx.ended(r.tick(now=600.0 + grace - EPS)) == []
    assert tx.ended(r.tick(now=600.0 + grace))


def test_grace_is_read_at_use(tx, monkeypatch):
    monkeypatch.setattr(transfer, "RECONNECT_GRACE", 2.0)
    r = tx.Recv(tx.entries(1))
    r.accept()
    r.lost(now=10.0)
    assert tx.ended(r.tick(now=12.0))


# ---- AC-16: bảng nhớ ------------------------------------------------------------------------------


def _remember_cancelled(tx, r, T: str) -> None:
    """`offer` mới rồi `complete` khi chưa chấp nhận → bia mộ (RECEIVE, T, "error")."""
    r.msg(Offer(transfer_id=T, block_size=BLOCK_SIZE, files=r.files))
    acts = r.msg(Complete(transfer_id=T, file_index=0, merkle_root="00" * 32))
    assert tx.sent(acts) == [Cancel(transfer_id=T, reason="error")]


def test_memory_table_evicts_oldest_beyond_limit(tx):
    r = tx.Recv(tx.entries(1))
    r.up()
    ids = [tx.tid(1000 + k) for k in range(MAX_REMEMBERED + 1)]
    for T in ids:
        _remember_cancelled(tx, r, T)
    # Mục thứ 2 vẫn còn; tra cứu không làm mục mới hơn.
    assert tx.sent(r.msg(Offer(transfer_id=ids[1], block_size=BLOCK_SIZE, files=r.files))) == [
        Cancel(transfer_id=ids[1], reason="error")
    ]
    # Mục đầu tiên đã bị đẩy ra: `offer` gửi lại là `offer` mới.
    acts = r.msg(Offer(transfer_id=ids[0], block_size=BLOCK_SIZE, files=r.files))
    assert tx.sent(acts) == []
    assert tx.one(acts, CheckSpace).transfer_id == ids[0]


def test_memory_table_is_per_device(tx):
    ra = tx.Recv(tx.entries(1))
    rc = tx.Recv(tx.entries(1), peer=tx.OTHER, like=ra, cid=2)
    ra.up()
    rc.up()
    _remember_cancelled(tx, rc, tx.tid(7))
    for k in range(MAX_REMEMBERED + 1):
        _remember_cancelled(tx, ra, tx.tid(1000 + k))
    assert tx.sent(rc.msg(Offer(transfer_id=tx.tid(7), block_size=BLOCK_SIZE, files=rc.files))) == [
        Cancel(transfer_id=tx.tid(7), reason="error")
    ]


def test_memory_entry_is_forgotten_after_ttl(tx):
    r = tx.Recv(tx.entries(1))
    r.up(now=1.0)
    r.to_asking(up=False)
    r.call("decide", tx.PEER, r.T, False, now=10.0)  # nhớ REJECT lúc 10
    rej = Reject(transfer_id=r.T, reason="user")
    assert tx.sent(r.send_offer(now=10.0 + REMEMBER_TTL - EPS)) == [rej]
    acts = r.send_offer(now=10.0 + REMEMBER_TTL)
    assert tx.sent(acts) == []
    assert tx.one(acts, CheckSpace).transfer_id == r.T


def test_tombstone_is_forgotten_after_ttl(tx):
    r = tx.Recv(tx.entries(1))
    r.accept()
    r.call("cancel", r.key, now=50.0)
    assert tx.sent(r.send_offer(now=50.0 + REMEMBER_TTL - EPS)) == [Cancel(transfer_id=r.T, reason="user")]
    acts = r.send_offer(now=50.0 + REMEMBER_TTL)
    assert tx.one(acts, CheckSpace).transfer_id == r.T


@pytest.mark.parametrize("reason", ["not_receiving", "busy"])
def test_busy_and_not_receiving_leave_no_memory_entry(tx, reason):
    # Hành vi `offer` gửi lại: test_transfer_offer.py (not_receiving, busy). Ở đây kiểm bảng nhớ
    # qua `TransferIdInUse`: không có mục nhớ thì `transfer_id` đó vẫn dùng được cho lượt gửi.
    first = tx.Recv(tx.entries(1), T=tx.tid(1), receiving=reason == "busy")
    first.up()
    if reason == "busy":
        for k in range(1, 9):
            tx.Recv(tx.entries(1), T=tx.tid(k), like=first).send_offer()
    last = tx.Recv(tx.entries(1), T=tx.tid(9), like=first)
    assert tx.sent(last.send_offer()) == [Reject(transfer_id=last.T, reason=reason)]
    acts = last.call("send", tx.PEER, last.T, list(tx.entries(1)))
    assert tx.statuses(acts, TransferKey(tx.PEER, Direction.SEND, last.T))


def test_done_entry_answers_offer_and_complete(tx):
    r = tx.Recv(tx.entries(1, 1))
    r.accept()
    r.finish_all()
    assert tx.sent(r.send_offer()) == [Accept(transfer_id=r.T)]
    assert tx.sent(r.complete(1)) == [Verified(transfer_id=r.T, file_index=1)]
    assert r.e.info(r.key) is None


def test_offer_with_other_manifest_than_done_entry_only_cancels(tx):
    r = tx.Recv(tx.entries(1))
    r.accept()
    r.finish_all()
    other = Offer(transfer_id=r.T, block_size=BLOCK_SIZE, files=tx.entries(2))
    acts = r.send_offer(other)
    assert tx.sent(acts) == [Cancel(transfer_id=r.T, reason="error")]
    assert tx.events(acts) == []  # không có TransferEnded thứ hai
    assert not tx.of(acts, (RemoveTemp, CreateTemp, CheckSpace))
    # Bia mộ đè lên mục DONE: lần sau, kể cả đúng manifest, chỉ nhận `cancel{error}`.
    assert tx.sent(r.send_offer()) == [Cancel(transfer_id=r.T, reason="error")]
    assert tx.sent(r.complete(0)) == [Cancel(transfer_id=r.T, reason="error")]


@pytest.mark.parametrize("what", ["running", "tombstone", "reject", "done"])
def test_send_with_id_known_as_receive_raises_transfer_id_in_use(tx, what):
    r = tx.Recv(tx.entries(1))
    if what == "reject":
        r.to_asking()
        r.call("decide", tx.PEER, r.T, False)
    else:
        r.accept()
        if what == "tombstone":
            r.call("cancel", r.key)
        elif what == "done":
            r.finish_all()
    before = tx.snapshot(r.e)
    with pytest.raises(TransferIdInUse):
        r.e.send(tx.PEER, r.T, list(tx.entries(1)), r.now + 1)
    assert tx.snapshot(r.e) == before
    assert issubclass(TransferIdInUse, ValueError)
    # Thiết bị khác: cùng `transfer_id` là một lượt khác.
    acts = r.call("send", tx.OTHER, r.T, list(tx.entries(1)))
    assert tx.statuses(acts, TransferKey(tx.OTHER, Direction.SEND, r.T))


@pytest.mark.parametrize("what", ["running", "tombstone"])
def test_send_with_id_known_as_send_raises_transfer_id_in_use(tx, what):
    s = tx.Send(tx.entries(1))
    s.start()
    if what == "tombstone":
        s.call("cancel", s.key)
    with pytest.raises(TransferIdInUse):
        s.e.send(tx.PEER, s.T, list(tx.entries(2)), s.now + 1)


def test_send_with_id_tombstoned_in_opposite_direction_raises(tx):
    e = TransferEngine(tx.ME, receiving=True)
    r = tx.Recv(tx.entries(1), engine=e)
    r.accept()
    r.msg(Cancel(transfer_id=r.T, reason="user"))  # bia mộ (RECEIVE, T, "user")
    with pytest.raises(TransferIdInUse):
        e.send(tx.PEER, r.T, list(tx.entries(1)), r.now + 1)


def test_rejected_by_peer_send_is_not_remembered(tx):
    s = tx.Send(tx.entries(1))
    s.start(accept=False)
    s.msg(Reject(transfer_id=s.T, reason="user"))
    # `reject` nhận được không lập bia mộ: `resume` sau đó là lượt không biết.
    assert tx.sent(s.resume(0)) == [Cancel(transfer_id=s.T, reason="unknown_transfer")]
