"""P3-2: engine khôi phục, bia mộ bền, tạm dừng nhận (spec-p3 mục 2.1 H3, H4, S1, M3, Q3; mục 2.3 P3-2).

Viết từ spec, không từ code: API `TransferEngine.restore(*, receives, tombstones, done, now)`,
`export_tombstones(now)`, `suspend_receiving(now)`, các bản ghi `RestoredReceive`, `RestoredTombstone`,
`RestoredDone`, hành động mới `ClearBlocks`, `SuspendTemp`, `TombstonesChanged`, trường mới
`CreateTemp.offer`, `RemoveTemp.keep_record`, `Outcome.SUSPENDED`. Luật đồng bộ lại: protocol §6.3.

Mô phỏng hai engine có mô hình sập ở `tests/core/test_transfer_sim.py`. Mọi tên mới lấy qua
`tx.tr` (fixture), để module chưa có tên đó chỉ làm đỏ test dùng tới nó.
"""

from __future__ import annotations

import random

import pytest

from teleport.core.blocks import BLOCK_SIZE, DIGEST_SIZE, block_count
from teleport.core.messages import Cancel, DataOpen, Offer, Resume

# ---- Phụ trợ ----------------------------------------------------------------------------------


def make_leaves(tx, r, f: int, on) -> bytearray:
    """`leaves` của file `f` như đọc lại từ `.state`: hash đúng ở các khối đã đánh dấu, 0 ở chỗ khác."""
    out = bytearray(DIGEST_SIZE * r.n(f))
    for i in on:
        out[DIGEST_SIZE * i : DIGEST_SIZE * (i + 1)] = r.dg(f, i)
    return out


def restored_recv(
    tx,
    files,
    *,
    f: int = 0,
    on=(),
    part_blocks: int | None = None,
    age: float = 5.0,
    now: float = 1000.0,
    T: str | None = None,
    peer: str | None = None,
    tombstones=(),
    done=(),
    receiving: bool = True,
):
    """Engine mới vừa khôi phục một lượt nhận của `peer`; trả `(driver Recv, hành động của restore)`."""
    tr = tx.tr
    e = tr.TransferEngine(tx.ME, receiving=receiving)
    r = tx.Recv(files, engine=e, T=T or tx.T1, peer=peer or tx.PEER, clock=tx.Clock(now))
    on = sorted(set(on))
    if part_blocks is None:
        part_blocks = (max(on) + 1) if on else 0
    item = tr.RestoredReceive(
        device_id=r.peer,
        offer=r.offer,
        file_index=f,
        bits=tx.bits(r.n(f), on),
        leaves=make_leaves(tx, r, f, on),
        part_blocks=part_blocks,
        age=age,
    )
    acts = e.restore(receives=(item,), tombstones=tuple(tombstones), done=tuple(done), now=now)
    assert isinstance(acts, list)
    tx.check_order(acts)
    return r, acts


def tomb(tx, *, device=None, direction=None, T=None, reason="user", unsent=False, age=10.0):
    tr = tx.tr
    return tr.RestoredTombstone(
        device_id=device or tx.PEER,
        direction=direction or tr.Direction.SEND,
        transfer_id=T or tx.T1,
        reason=reason,
        unsent=unsent,
        age=age,
    )


def fresh(tx, *, receiving: bool = True):
    return tx.tr.TransferEngine(tx.ME, receiving=receiving)


def restore_only(tx, e, *, receives=(), tombstones=(), done=(), now: float = 1000.0):
    acts = e.restore(receives=tuple(receives), tombstones=tuple(tombstones), done=tuple(done), now=now)
    assert isinstance(acts, list)
    tx.check_order(acts)
    return acts


def by_device(items) -> dict[str, list]:
    out: dict[str, list] = {}
    for it in items:
        out.setdefault(it.device_id, []).append(it)
    return out


def changed(tx, acts) -> bool:
    return bool(tx.of(acts, tx.tr.TombstonesChanged))


def no_wire_no_remove(tx, acts) -> None:
    assert tx.of(acts, tx.tr.SendMessage) == [], acts
    assert tx.of(acts, tx.tr.RemoveTemp) == [], acts


# ---- restore: lời gọi đầu tiên, kiểm tham số ---------------------------------------------------


@pytest.mark.parametrize("first", ["control_up", "tick", "send", "control_lost", "restore"])
def test_restore_after_any_other_call_raises_runtime_error(tx, first):
    e = fresh(tx)
    if first == "control_up":
        e.control_up(tx.PEER, 1, True, 1.0)
    elif first == "tick":
        e.tick(1.0)
    elif first == "send":
        e.send(tx.PEER, tx.T1, list(tx.entries(1)), 1.0)
    elif first == "control_lost":
        e.control_lost(tx.PEER, 1, 1.0)
    else:
        restore_only(tx, e, now=1.0)
    with pytest.raises(RuntimeError):
        e.restore(receives=(), tombstones=(), done=(), now=2.0)


def test_restore_with_nothing_returns_no_action_and_engine_works_normally(tx):
    e = fresh(tx)
    assert restore_only(tx, e) == []
    assert e.transfers() == () and e.export_tombstones(1000.0) == ()
    r = tx.Recv(tx.entries(1), engine=e, clock=tx.Clock(1000.0))
    acts = r.to_asking()
    assert tx.of(acts, tx.tr.AskUser)


def test_restore_rejects_two_receives_from_same_device(tx):
    tr = tx.tr
    e = fresh(tx)
    files = tx.entries(2)
    items = [
        tr.RestoredReceive(
            device_id=tx.PEER,
            offer=Offer(transfer_id=T, block_size=BLOCK_SIZE, files=files),
            file_index=0,
            bits=tx.bits(2),
            leaves=bytearray(2 * DIGEST_SIZE),
            part_blocks=0,
            age=1.0,
        )
        for T in (tx.T1, tx.T2)
    ]
    with pytest.raises(ValueError):
        e.restore(receives=tuple(items), tombstones=(), done=(), now=10.0)


def test_restore_accepts_one_receive_per_device_from_several_devices(tx):
    """Đối chứng của test trên: hai thiết bị, mỗi thiết bị một lượt → hợp lệ."""
    tr = tx.tr
    e = fresh(tx)
    files = tx.entries(2)
    items = [
        tr.RestoredReceive(
            device_id=dev,
            offer=Offer(transfer_id=tx.T1, block_size=BLOCK_SIZE, files=files),
            file_index=0,
            bits=tx.bits(2),
            leaves=bytearray(2 * DIGEST_SIZE),
            part_blocks=0,
            age=1.0,
        )
        for dev in (tx.PEER, tx.OTHER)
    ]
    restore_only(tx, e, receives=items, now=10.0)
    assert {i.key.device_id for i in e.transfers()} == {tx.PEER, tx.OTHER}


def _receive_item(tx, *, device=None, f=0, on=(), part_blocks=None, nblocks=(4,)):
    tr = tx.tr
    files = tx.entries(*nblocks)
    n = block_count(files[min(f, len(files) - 1)].size)
    on = sorted(on)
    return tr.RestoredReceive(
        device_id=device or tx.PEER,
        offer=Offer(transfer_id=tx.T1, block_size=BLOCK_SIZE, files=files),
        file_index=f,
        bits=tx.bits(n, on),
        leaves=bytearray(DIGEST_SIZE * n),
        part_blocks=(max(on) + 1 if on else 0) if part_blocks is None else part_blocks,
        age=1.0,
    )


def test_restore_rejects_receive_from_own_device(tx):
    e = fresh(tx)
    with pytest.raises(ValueError):
        e.restore(receives=(_receive_item(tx, device=tx.ME),), tombstones=(), done=(), now=10.0)


def test_restore_rejects_tombstone_of_own_device(tx):
    e = fresh(tx)
    with pytest.raises(ValueError):
        e.restore(receives=(), tombstones=(tomb(tx, device=tx.ME),), done=(), now=10.0)


def test_restore_rejects_done_of_own_device(tx):
    tr = tx.tr
    e = fresh(tx)
    done = tr.RestoredDone(
        device_id=tx.ME, offer=Offer(transfer_id=tx.T1, block_size=BLOCK_SIZE, files=tx.entries(1)), age=1.0
    )
    with pytest.raises(ValueError):
        e.restore(receives=(), tombstones=(), done=(done,), now=10.0)


def test_restore_rejects_file_index_not_below_file_count(tx):
    e = fresh(tx)
    item = _receive_item(tx, f=2, nblocks=(4, 4))  # n = 2 → file_index 2 sai
    with pytest.raises(ValueError):
        e.restore(receives=(item,), tombstones=(), done=(), now=10.0)


def test_restore_accepts_last_file_index(tx):
    """Đối chứng: `file_index` = n − 1 hợp lệ."""
    e = fresh(tx)
    restore_only(tx, e, receives=(_receive_item(tx, f=1, nblocks=(4, 4)),), now=10.0)
    assert e.transfers()[0].file_index == 1


def test_restore_rejects_bit_at_part_blocks(tx):
    """Bit ≥ `part_blocks` phải tắt (H4: app đã xóa bit khối không nằm trọn trong `.part`)."""
    e = fresh(tx)
    item = _receive_item(tx, on=(0, 2), part_blocks=2)  # bit 2 bật, `.part` chỉ chứa trọn 2 khối
    with pytest.raises(ValueError):
        e.restore(receives=(item,), tombstones=(), done=(), now=10.0)


def test_restore_accepts_bits_below_part_blocks(tx):
    """Đối chứng: cùng bit, `part_blocks` = 3 → hợp lệ."""
    e = fresh(tx)
    restore_only(tx, e, receives=(_receive_item(tx, on=(0, 2), part_blocks=3),), now=10.0)
    assert len(e.transfers()) == 1


def test_failed_restore_leaves_engine_unchanged(tx):
    """Ném lỗi thì trạng thái giữ nguyên (hợp đồng chung của engine, docstring module): không lượt nào,
    không bia mộ nào được nạp dở, và lời gọi `restore` hợp lệ sau đó vẫn được nhận."""
    e = fresh(tx)
    good = tomb(tx, T=tx.T2)
    with pytest.raises(ValueError):
        e.restore(receives=(_receive_item(tx, f=5),), tombstones=(good,), done=(), now=10.0)
    assert e.transfers() == ()
    assert e.export_tombstones(10.0) == ()
    restore_only(tx, e, tombstones=(good,), now=10.0)
    assert [t.transfer_id for t in e.export_tombstones(10.0)] == [tx.T2]


# ---- restore: lượt nhận ------------------------------------------------------------------------


def test_restored_receive_reports_reconnecting_without_disk_actions(tx):
    r, acts = restored_recv(tx, tx.entries(6), on=(0, 1, 2), age=4.0)
    tr = tx.tr
    assert acts == [tr.Report(tr.StatusChanged(r.key, tr.TransferStatus.RECONNECTING))]
    info = r.e.info(r.key)
    assert info is not None and info.status is tr.TransferStatus.RECONNECTING
    assert info.file_index == 0


def test_restored_receive_progress_counts_verified_files_and_restored_blocks(tx):
    """Dòng mvp §8 “đã nhận *4,2 GB* / *8,0 GB*” lấy từ bản chụp: file trước đã xong + khối đã khôi phục."""
    files = tx.entries(3, 5, last=1000)
    r, _ = restored_recv(tx, files, f=1, on=(0, 1, 4))
    info = r.e.info(r.key)
    assert info.total_bytes == sum(x.size for x in files)
    assert info.done_bytes == files[0].size + 2 * BLOCK_SIZE + 1000


def test_restored_receive_resyncs_with_restored_bitfield_on_control_up(tx):
    """protocol §6.3 cột “Nhận”: kết nối mới → `accept` rồi `resume` cho file hiện tại, bitfield khôi phục;
    `offer` gửi lại → chỉ `accept` (đã gửi `resume` trên kết nối này)."""
    r, _ = restored_recv(tx, tx.entries(2, 8), f=1, on=(0, 1, 2, 3, 5))
    acts = r.up(cid=7, client=False)
    msgs = tx.sent(acts)
    assert [type(m).__name__ for m in msgs] == ["Accept", "Resume"]
    res = msgs[1]
    assert (res.transfer_id, res.file_index) == (r.T, 1)
    assert tx.bits_on(res.bitfield, 8) == {0, 1, 2, 3, 5}
    assert tx.of(acts, tx.tr.OpenPart) == [] and tx.of(acts, tx.tr.CreateTemp) == []
    again = r.send_offer()
    assert [type(m).__name__ for m in tx.sent(again)] == ["Accept"]
    assert tx.statuses(acts, r.key) == [tx.tr.TransferStatus.TRANSFERRING]


def test_restored_receive_as_client_opens_data_after_resume(tx):
    r, _ = restored_recv(tx, tx.entries(4), on=(0,))
    acts = r.up(cid=3, client=True)
    od = tx.one(acts, tx.tr.OpenData)
    res = tx.sent(acts, Resume)[-1]
    assert od.message == DataOpen(transfer_id=r.T, file_index=0, resume_seq=res.seq)


def test_restored_receive_accepts_block_at_part_blocks_without_gap(tx):
    """`hw = part_blocks − 1`: khối `part_blocks` là khối kế tiếp hợp lệ (không coi là nhảy cóc)."""
    r, _ = restored_recv(tx, tx.entries(8), on=(0, 1), part_blocks=4)
    r.up()
    did, a = r.open()
    assert tx.of(a, tx.tr.ReceiveBlocks)
    acts = r.feed(did, 0, [4])
    assert [w.index for w in tx.of(acts, tx.tr.WriteBlock)] == [4]
    assert tx.of(acts, tx.tr.CloseData) == []


def test_restored_receive_rejects_block_beyond_part_blocks_as_gap(tx):
    """Đối chứng: khối `part_blocks + 1` khi chưa có khối `part_blocks` → khối sai, không ghi."""
    r, _ = restored_recv(tx, tx.entries(8), on=(0, 1), part_blocks=4)
    r.up()
    did, _ = r.open()
    acts = r.feed(did, 0, [5])
    assert tx.of(acts, tx.tr.WriteBlock) == []
    assert [c.data_id for c in tx.of(acts, tx.tr.CloseData)] == [did]


def test_restored_receive_finishes_with_only_missing_blocks_and_keeps_record(tx):
    r, _ = restored_recv(tx, tx.entries(6), on=(0, 1, 2, 3), part_blocks=4)
    r.up()
    have = tx.bits_on(r.last_resume().bitfield, 6)
    assert have == {0, 1, 2, 3}
    acts = r.receive_blocks(0)
    assert sorted(w.index for w in tx.of(acts, tx.tr.WriteBlock)) == [4, 5]
    acts = r.complete(0)
    rr = tx.one(acts, tx.tr.Reread)
    assert bytes(rr.leaves) == r.leaves(0)
    acts = r.call("reread_done", r.peer, r.T, 0, [], r.root(0))
    fin = tx.one(acts, tx.tr.Finalize)
    acts = r.call("finalized", r.peer, r.T, 0, fin.name)
    assert [m.file_index for m in tx.sent(acts, tx.Verified)] == [0]
    (end,) = tx.ended(acts)
    assert end.outcome is tx.tr.Outcome.DONE
    rm = tx.one(acts, tx.tr.RemoveTemp)
    assert rm.keep_record is True and rm.transfer_id == r.T


def test_restored_receive_with_all_bits_goes_through_normal_verification(tx):
    """H4: lượt sập sau khi đủ khối (hay giữa lúc đổi tên) đi luồng VERIFYING thường: `resume` đủ bit
    (không mở kết nối dữ liệu), chờ `complete` gửi lại, rồi `Reread` và `Finalize`."""
    r, _ = restored_recv(tx, tx.entries(3), on=(0, 1, 2))
    acts = r.up(client=True)
    assert tx.bits_on(tx.sent(acts, Resume)[-1].bitfield, 3) == {0, 1, 2}
    assert tx.of(acts, tx.tr.OpenData) == []
    acts = r.complete(0)
    assert tx.one(acts, tx.tr.Reread).file_index == 0
    acts = r.call("reread_done", r.peer, r.T, 0, [], r.root(0))
    assert tx.one(acts, tx.tr.Finalize).file_index == 0


def test_restored_receive_answers_complete_of_earlier_file_with_verified(tx):
    """`verified` của file trước có thể đã mất lúc sập: `complete` gửi lại của nó → `verified`."""
    r, _ = restored_recv(tx, tx.entries(2, 3), f=1)
    r.up()
    acts = r.complete(0)
    assert tx.sent(acts) == [tx.Verified(transfer_id=r.T, file_index=0)]


def test_restored_receive_cancelled_by_sender_that_forgot_it_removes_temp(tx):
    """Bên gửi đã khởi động lại → `cancel{unknown_transfer}` → lượt kết thúc, xóa thư mục tạm."""
    r, _ = restored_recv(tx, tx.entries(3), on=(0,))
    r.up()
    acts = r.msg(Cancel(transfer_id=r.T, reason="unknown_transfer"))
    (end,) = tx.ended(acts)
    assert (end.outcome, end.by_peer) == (tx.tr.Outcome.CANCELLED, True)
    assert tx.one(acts, tx.tr.RemoveTemp).keep_record is False


def test_restored_receive_offer_with_other_manifest_cancels_with_error(tx):
    r, _ = restored_recv(tx, tx.entries(3), on=(0,))
    r.up()
    other = Offer(transfer_id=r.T, block_size=BLOCK_SIZE, files=tx.entries(4))
    acts = r.send_offer(other)
    assert Cancel(transfer_id=r.T, reason="error") in tx.sent(acts)
    assert tx.one(acts, tx.tr.RemoveTemp).keep_record is False
    assert changed(tx, acts)


# ---- restore: hết hạn (Q3) ---------------------------------------------------------------------


def test_restored_receive_older_than_grace_expires_on_first_tick(tx):
    tr = tx.tr
    now = 50_000.0
    r, _ = restored_recv(tx, tx.entries(3), on=(0,), age=tr.RECONNECT_GRACE, now=now)
    w = r.e.next_wakeup()
    assert w is not None and w <= now
    acts = r.tick(now=now)
    (end,) = tx.ended(acts)
    assert (end.key, end.outcome, end.reason, end.by_peer) == (r.key, tr.Outcome.CANCELLED, "expired", False)
    assert tx.one(acts, tr.RemoveTemp) == tr.RemoveTemp(device_id=r.peer, transfer_id=r.T, keep_record=False)
    assert changed(tx, acts)
    assert tx.of(acts, tr.SendMessage) == []
    (t,) = r.e.export_tombstones(now)
    assert (t.device_id, t.direction, t.transfer_id, t.reason, t.unsent) == (
        r.peer,
        tr.Direction.RECEIVE,
        r.T,
        "expired",
        True,
    )
    assert t.age == pytest.approx(0.0)
    # Lần kết nối sau: `cancel{expired}` trước mọi message khác (protocol §6.3).
    up = r.up(now=now + 1)
    assert tx.sent(up)[0] == Cancel(transfer_id=r.T, reason="expired")


def test_restored_receive_younger_than_grace_expires_exactly_at_grace(tx):
    tr = tx.tr
    now = 50_000.0
    age = tr.RECONNECT_GRACE - 5.0
    r, _ = restored_recv(tx, tx.entries(3), age=age, now=now)
    assert r.e.next_wakeup() == pytest.approx(now + 5.0)
    assert tx.ended(r.tick(now=now)) == []
    assert tx.ended(r.tick(now=now + 4.9)) == []
    assert r.e.info(r.key) is not None
    (end,) = tx.ended(r.tick(now=now + 5.0))
    assert end.reason == "expired"


def test_restored_receive_reconnecting_before_grace_does_not_expire(tx):
    tr = tx.tr
    now = 50_000.0
    r, _ = restored_recv(tx, tx.entries(3), age=tr.RECONNECT_GRACE - 5.0, now=now)
    r.up(now=now + 1.0)
    assert tx.ended(r.tick(now=now + 10.0)) == []
    assert r.e.info(r.key) is not None


# ---- restore: lượt đã xong ---------------------------------------------------------------------


def _restored_done(tx, files, *, age=100.0, now=1000.0):
    tr = tx.tr
    e = fresh(tx)
    r = tx.Recv(files, engine=e, clock=tx.Clock(now))
    restore_only(tx, e, done=(tr.RestoredDone(device_id=r.peer, offer=r.offer, age=age),), now=now)
    return r


def test_restored_done_answers_resent_complete_with_verified(tx):
    r = _restored_done(tx, tx.entries(2, 3))
    r.up()
    for f in (0, 1):
        assert tx.sent(r.complete(f)) == [tx.Verified(transfer_id=r.T, file_index=f)]
    assert r.e.transfers() == ()


def test_restored_done_answers_same_offer_with_accept_only(tx):
    r = _restored_done(tx, tx.entries(2))
    r.up()
    acts = r.send_offer()
    assert tx.sent(acts) == [tx.Accept(transfer_id=r.T)]
    assert tx.of(acts, tx.tr.CheckSpace) == []


def test_restored_done_offer_with_other_manifest_gets_error_and_tombstone(tx):
    tr = tx.tr
    r = _restored_done(tx, tx.entries(2))
    r.up()
    acts = r.send_offer(Offer(transfer_id=r.T, block_size=BLOCK_SIZE, files=tx.entries(3)))
    assert tx.sent(acts) == [Cancel(transfer_id=r.T, reason="error")]
    assert changed(tx, acts)  # bia mộ ghi đè mục “đã xong”
    (t,) = r.e.export_tombstones(r.now)
    assert (t.direction, t.transfer_id, t.reason) == (tr.Direction.RECEIVE, r.T, "error")


def test_restored_done_older_than_ttl_is_dropped(tx):
    tr = tx.tr
    r = _restored_done(tx, tx.entries(1), age=tr.REMEMBER_TTL)
    r.up()
    acts = r.send_offer()
    assert tx.of(acts, tr.CheckSpace), "mục quá TTL phải bị bỏ: offer gửi lại là offer mới"


def test_restored_done_expires_at_ttl_measured_from_restored_age(tx):
    tr = tx.tr
    now = 1000.0
    r = _restored_done(tx, tx.entries(1), age=tr.REMEMBER_TTL - 10.0, now=now)
    r.up(now=now + 1.0)
    assert tx.sent(r.send_offer(now=now + 9.5)) == [tx.Accept(transfer_id=r.T)]
    assert tx.of(r.send_offer(now=now + 10.0), tr.CheckSpace)


# ---- Bia mộ: khôi phục, xuất, khứ hồi ---------------------------------------------------------


def test_restored_send_tombstone_answers_resume_with_same_reason(tx):
    e = fresh(tx)
    restore_only(tx, e, tombstones=(tomb(tx, reason="source_changed"),))
    s = tx.Send(tx.entries(2), engine=e, clock=tx.Clock(1000.0))
    s.up()
    acts = s.resume(0)
    assert tx.sent(acts) == [Cancel(transfer_id=s.T, reason="source_changed")]


def test_restored_receive_tombstone_answers_offer_and_complete_with_same_reason(tx):
    tr = tx.tr
    e = fresh(tx)
    restore_only(tx, e, tombstones=(tomb(tx, direction=tr.Direction.RECEIVE, reason="user"),))
    r = tx.Recv(tx.entries(2), engine=e, clock=tx.Clock(1000.0))
    r.up()
    assert tx.sent(r.send_offer()) == [Cancel(transfer_id=r.T, reason="user")]
    assert tx.sent(r.complete(0)) == [Cancel(transfer_id=r.T, reason="user")]


def test_restored_tombstone_blocks_reusing_transfer_id(tx):
    """protocol §6.3 “Khóa”: bên gửi không sinh `transfer_id` còn bia mộ."""
    e = fresh(tx)
    restore_only(tx, e, tombstones=(tomb(tx),))
    with pytest.raises(tx.tr.TransferIdInUse):
        e.send(tx.PEER, tx.T1, list(tx.entries(1)), 1001.0)


def test_unsent_restored_tombstone_sends_cancel_first_on_control_up_and_reports_change(tx):
    e = fresh(tx)
    restore_only(tx, e, tombstones=(tomb(tx, reason="user", unsent=True),), now=1000.0)
    (before,) = e.export_tombstones(1000.0)
    assert before.unsent is True
    s = tx.Send(tx.entries(1), T=tx.T2, engine=e, clock=tx.Clock(1000.0))
    s.call("send", s.peer, s.T, list(s.files))  # một lượt khác đang chờ: `offer` của nó đi sau `cancel`
    acts = s.up()
    msgs = tx.sent(acts)
    assert msgs[0] == Cancel(transfer_id=tx.T1, reason="user")
    assert isinstance(msgs[1], Offer) and msgs[1].transfer_id == tx.T2
    assert changed(tx, acts)
    (after,) = e.export_tombstones(s.now)
    assert after.unsent is False
    # Kết nối sau không gửi lại `cancel` đó nữa, và không còn gì đổi.
    s.lost()
    again = s.up(cid=2)
    assert Cancel(transfer_id=tx.T1, reason="user") not in tx.sent(again)
    assert not changed(tx, again)


def test_control_up_without_unsent_tombstones_reports_no_change(tx):
    e = fresh(tx)
    restore_only(tx, e, tombstones=(tomb(tx, unsent=False),))
    acts = e.control_up(tx.PEER, 1, True, 1001.0)
    tx.check_order(acts)
    assert not changed(tx, acts)
    assert tx.sent(acts) == []


def test_restored_tombstones_older_than_ttl_are_dropped(tx):
    tr = tx.tr
    e = fresh(tx)
    old = tomb(tx, T=tx.T1, age=tr.REMEMBER_TTL)
    young = tomb(tx, T=tx.T2, age=tr.REMEMBER_TTL - 1.0)
    restore_only(tx, e, tombstones=(old, young), now=1000.0)
    assert [t.transfer_id for t in e.export_tombstones(1000.0)] == [tx.T2]
    s = tx.Send(tx.entries(1), T=tx.T1, engine=e, clock=tx.Clock(1000.0))
    s.up()
    assert tx.sent(s.resume(0)) == [Cancel(transfer_id=tx.T1, reason="unknown_transfer")]


def test_restored_tombstone_expires_at_ttl_measured_from_restored_age(tx):
    tr = tx.tr
    e = fresh(tx)
    now = 1000.0
    restore_only(tx, e, tombstones=(tomb(tx, age=tr.REMEMBER_TTL - 10.0, reason="user"),), now=now)
    assert [t.transfer_id for t in e.export_tombstones(now + 9.9)] == [tx.T1]
    assert e.export_tombstones(now + 10.0) == ()
    s = tx.Send(tx.entries(1), engine=e, clock=tx.Clock(now))
    s.up(now=now + 1.0)
    assert tx.sent(s.resume(0, now=now + 9.9)) == [Cancel(transfer_id=s.T, reason="user")]
    assert tx.sent(s.resume(0, now=now + 10.0)) == [Cancel(transfer_id=s.T, reason="unknown_transfer")]


def test_export_tombstones_ages_follow_query_time_and_skip_expired_entries(tx):
    tr = tx.tr
    e = fresh(tx)
    restore_only(tx, e, tombstones=(tomb(tx, T=tx.T1, age=100.0), tomb(tx, T=tx.T2, age=40.0)), now=1000.0)
    a = e.export_tombstones(1000.0)
    b = e.export_tombstones(1060.0)
    assert [(t.transfer_id, t.age) for t in a] == [
        (tx.T1, pytest.approx(100.0)),
        (tx.T2, pytest.approx(40.0)),
    ]
    assert [(t.transfer_id, t.age) for t in b] == [
        (tx.T1, pytest.approx(160.0)),
        (tx.T2, pytest.approx(100.0)),
    ]
    late = 1000.0 + tr.REMEMBER_TTL - 100.0
    assert [t.transfer_id for t in e.export_tombstones(late)] == [tx.T2]
    # Truy vấn thuần: gọi lại cho cùng kết quả, không có hành động nào.
    assert e.export_tombstones(1060.0) == b


def test_export_tombstones_excludes_rejects_and_finished_transfers(tx):
    e = fresh(tx)
    clock = tx.Clock(10.0)
    done = tx.Recv(tx.entries(0), T=tx.T1, engine=e, clock=clock)
    done.accept()
    done.finish_all()
    assert e.transfers() == ()
    rej = tx.Recv(tx.entries(1), T=tx.T2, engine=e, clock=clock)
    rej.to_asking(up=False)
    acts = rej.call("decide", rej.peer, rej.T, False)
    assert tx.sent(acts, tx.Reject)
    assert not changed(tx, acts), "reject đã nhớ chỉ nằm trong RAM: không phải bia mộ"
    assert e.export_tombstones(clock.now) == ()


def _engine_with_real_tombstones(tx):
    """Engine có bia mộ sinh từ sự kiện thật, ở hai thiết bị, cả hai chiều, cách nhau về thời gian."""
    e = fresh(tx)
    clock = tx.Clock(10.0)
    s = tx.Send(tx.entries(2), T=tx.T1, engine=e, clock=clock)
    s.start()
    clock.now = 20.0
    assert changed(tx, s.call("cancel", s.key))
    r = tx.Recv(tx.entries(2), T=tx.T2, engine=e, clock=clock, first_did=500)
    r.accept(up=False)
    clock.now = 30.0
    assert changed(tx, r.msg(Cancel(transfer_id=r.T, reason="source_changed")))
    o = tx.Send(tx.entries(1), T=tx.T3, engine=e, peer=tx.OTHER, clock=clock)
    o.call("send", o.peer, o.T, list(o.files))  # OTHER chưa có kết nối điều khiển
    clock.now = 45.0
    assert changed(tx, o.call("cancel", o.key))
    clock.now = 60.0
    return e, clock.now


def test_tombstones_round_trip_keeps_order_age_and_unsent_flag(tx):
    tr = tx.tr
    e1, t1 = _engine_with_real_tombstones(tx)
    out1 = e1.export_tombstones(t1)
    assert all(isinstance(t, tr.RestoredTombstone) for t in out1)
    got = {
        d: [(t.direction, t.transfer_id, t.reason, t.unsent) for t in ts] for d, ts in by_device(out1).items()
    }
    assert got == {
        tx.PEER: [
            (tr.Direction.SEND, tx.T1, "user", False),
            (tr.Direction.RECEIVE, tx.T2, "source_changed", False),
        ],
        tx.OTHER: [(tr.Direction.SEND, tx.T3, "user", True)],
    }
    ages1 = {(t.device_id, t.transfer_id): t.age for t in out1}
    assert ages1 == {
        (tx.PEER, tx.T1): pytest.approx(t1 - 20.0, abs=0.1),
        (tx.PEER, tx.T2): pytest.approx(t1 - 30.0, abs=0.1),
        (tx.OTHER, tx.T3): pytest.approx(t1 - 45.0, abs=0.1),
    }
    # Khôi phục vào engine mới với gốc thời gian monotonic khác, đầu vào xáo trộn.
    e2 = fresh(tx)
    shuffled = list(out1)
    random.Random(3).shuffle(shuffled)
    restore_only(tx, e2, tombstones=shuffled, now=777_000.0)
    out2 = e2.export_tombstones(777_000.0 + 7.0)
    g1, g2 = by_device(out1), by_device(out2)
    assert g1.keys() == g2.keys()
    for d in g1:
        assert [(t.direction, t.transfer_id, t.reason, t.unsent) for t in g2[d]] == [
            (t.direction, t.transfer_id, t.reason, t.unsent) for t in g1[d]
        ]
        assert [t.age for t in g2[d]] == [pytest.approx(t.age + 7.0) for t in g1[d]]
    # Bia mộ khôi phục hoạt động như bia mộ gốc.
    s = tx.Send(tx.entries(2), T=tx.T1, engine=e2, clock=tx.Clock(777_010.0))
    s.up()
    assert tx.sent(s.resume(0)) == [Cancel(transfer_id=tx.T1, reason="user")]
    up_other = e2.control_up(tx.OTHER, 9, True, 777_011.0)
    assert tx.sent(up_other)[0] == Cancel(transfer_id=tx.T3, reason="user")


def test_restore_sorts_tombstones_oldest_first_so_eviction_drops_the_oldest(tx, monkeypatch):
    """H3: bảng nhớ coi thứ tự chèn là thứ tự `written_at`; `restore` sắp theo tuổi giảm dần. Với trần
    3 mục, nạp 4 mục xáo trộn → còn 3 mục trẻ nhất, theo thứ tự cũ → mới."""
    tr = tx.tr
    monkeypatch.setattr(tr, "MAX_REMEMBERED", 3)
    e = fresh(tx)
    items = [tomb(tx, T=tx.tid(k), age=float(age)) for k, age in ((1, 30), (2, 400), (3, 10), (4, 200))]
    restore_only(tx, e, tombstones=items, now=1000.0)
    assert [(t.transfer_id, t.age) for t in e.export_tombstones(1000.0)] == [
        (tx.tid(4), pytest.approx(200.0)),
        (tx.tid(1), pytest.approx(30.0)),
        (tx.tid(3), pytest.approx(10.0)),
    ]


# ---- TombstonesChanged --------------------------------------------------------------------------


def test_user_cancel_online_reports_tombstones_changed(tx):
    s = tx.Send(tx.entries(1))
    s.start()
    acts = s.call("cancel", s.key)
    assert tx.sent(acts) == [Cancel(transfer_id=s.T, reason="user")]
    assert changed(tx, acts)


def test_peer_cancel_reports_tombstones_changed(tx):
    r = tx.Recv(tx.entries(2))
    r.accept()
    acts = r.msg(Cancel(transfer_id=r.T, reason="user"))
    assert changed(tx, acts)


def test_expiry_while_offline_reports_tombstones_changed(tx):
    tr = tx.tr
    s = tx.Send(tx.entries(1))
    s.start()
    s.lost(now=100.0)
    acts = s.tick(now=100.0 + tr.RECONNECT_GRACE)
    (end,) = tx.ended(acts)
    assert end.reason == "expired"
    assert changed(tx, acts)


def test_tombstone_evicted_by_new_memo_reports_tombstones_changed(tx, monkeypatch):
    """Bảng đầy: `reject` mới đẩy bia mộ cũ ra → bia mộ đổi."""
    tr = tx.tr
    monkeypatch.setattr(tr, "MAX_REMEMBERED", 1)
    e = fresh(tx)
    clock = tx.Clock(10.0)
    a = tx.Recv(tx.entries(1), T=tx.T1, engine=e, clock=clock)
    a.accept()
    assert changed(tx, a.msg(Cancel(transfer_id=a.T, reason="user")))
    b = tx.Recv(tx.entries(1), T=tx.T2, engine=e, clock=clock, first_did=900)
    b.to_asking(up=False)
    acts = b.call("decide", b.peer, b.T, False)
    assert e.export_tombstones(clock.now) == ()
    assert changed(tx, acts)


def test_rejecting_offer_reports_no_tombstone_change(tx):
    r = tx.Recv(tx.entries(1))
    r.to_asking()
    acts = r.call("decide", r.peer, r.T, False)
    assert tx.sent(acts, tx.Reject)
    assert not changed(tx, acts)


def test_tombstones_changed_is_an_action_before_reports(tx):
    """`TombstonesChanged` là hành động (không phải `Report`): đứng trước nhóm `Report` (G5)."""
    s = tx.Send(tx.entries(1))
    s.start()
    acts = s.call("cancel", s.key)
    i = tx.index_of(acts, lambda a: isinstance(a, tx.tr.TombstonesChanged))
    assert all(not isinstance(a, tx.tr.Report) for a in acts[:i])


# ---- Tạm dừng nhận (S1) -------------------------------------------------------------------------


def _receiving_mid_file(tx, *, client: bool = False):
    r = tx.Recv(tx.entries(40, 3))
    r.client = client
    r.accept()
    did, _ = r.open()
    r.sync_all(r.feed(did, 0, range(20)))
    return r, did


@pytest.mark.parametrize("client", [False, True])
def test_suspend_receiving_closes_data_and_suspends_temp_without_wire_or_removal(tx, client):
    tr = tx.tr
    r, did = _receiving_mid_file(tx, client=client)
    acts = r.call("suspend_receiving")
    no_wire_no_remove(tx, acts)
    assert not changed(tx, acts)
    assert [c.data_id for c in tx.of(acts, tr.CloseData)] == [did]
    assert tx.one(acts, tr.SuspendTemp) == tr.SuspendTemp(device_id=r.peer, transfer_id=r.T)
    (end,) = tx.ended(acts)
    assert (end.key, end.outcome) == (r.key, tr.Outcome.SUSPENDED)
    assert r.e.export_tombstones(r.now) == ()
    assert r.e.info(r.key) is None


def test_suspend_receiving_while_offline_only_suspends_temp(tx):
    tr = tx.tr
    r, _ = _receiving_mid_file(tx)
    r.lost()
    acts = r.call("suspend_receiving")
    no_wire_no_remove(tx, acts)
    assert tx.of(acts, tr.CloseData) == []
    assert tx.one(acts, tr.SuspendTemp).transfer_id == r.T
    assert [e.outcome for e in tx.ended(acts)] == [tr.Outcome.SUSPENDED]


def test_suspend_receiving_restored_lane_suspends_it(tx):
    tr = tx.tr
    r, _ = restored_recv(tx, tx.entries(5), on=(0, 1))
    acts = r.call("suspend_receiving")
    no_wire_no_remove(tx, acts)
    assert tx.one(acts, tr.SuspendTemp).transfer_id == r.T
    assert [e.outcome for e in tx.ended(acts)] == [tr.Outcome.SUSPENDED]


def test_suspend_receiving_with_nothing_running_does_nothing(tx):
    e = fresh(tx)
    assert e.suspend_receiving(1.0) == []


def test_suspend_receiving_leaves_send_lanes_alone(tx):
    tr = tx.tr
    e = fresh(tx)
    clock = tx.Clock(1.0)
    r = tx.Recv(tx.entries(4), engine=e, clock=clock)
    r.accept()
    s = tx.Send(tx.entries(2), T=tx.T2, engine=e, peer=tx.OTHER, clock=clock, cid=5)
    s.start()
    before = e.info(s.key)
    acts = r.call("suspend_receiving")
    assert [ev.key for ev in tx.ended(acts)] == [r.key]
    assert all(getattr(a, "device_id", tx.PEER) == tx.PEER for a in acts if not isinstance(a, tr.Report))
    assert e.info(s.key) == before


def test_suspended_lane_ignores_complete(tx):
    r, _ = _receiving_mid_file(tx)
    r.call("suspend_receiving")
    assert r.complete(0) == []


def test_suspended_lane_ignores_resent_offer(tx):
    r, _ = _receiving_mid_file(tx)
    r.call("suspend_receiving")
    assert r.send_offer() == []


def test_suspended_lane_ignores_peer_cancel(tx):
    r, _ = _receiving_mid_file(tx)
    r.call("suspend_receiving")
    assert r.msg(Cancel(transfer_id=r.T, reason="user")) == []
    assert r.e.export_tombstones(r.now) == ()


def test_suspended_lane_answers_data_open_only_with_close(tx):
    tr = tx.tr
    r, _ = _receiving_mid_file(tx)
    seq = r.last_resume().seq
    r.call("suspend_receiving")
    for s in (seq, seq + 1):
        did = r.new_did()
        acts = r.call("incoming_data", r.peer, did, DataOpen(transfer_id=r.T, file_index=0, resume_seq=s))
        assert acts == [tr.CloseData(device_id=r.peer, data_id=did)]


def test_suspended_lane_ignores_complete_offer_cancel_in_any_order(tx):
    """Hỗn hợp thứ tự (S1): không message nào sinh `SendMessage` hay `RemoveTemp`, không bia mộ."""
    r, _ = _receiving_mid_file(tx)
    r.call("suspend_receiving")
    rng = random.Random(11)
    steps = ["complete0", "complete1", "offer", "cancel", "data"] * 3
    rng.shuffle(steps)
    for step in steps:
        if step == "complete0":
            acts = r.complete(0)
        elif step == "complete1":
            acts = r.complete(1)
        elif step == "offer":
            acts = r.send_offer()
        elif step == "cancel":
            acts = r.msg(Cancel(transfer_id=r.T, reason="error"))
        else:
            did = r.new_did()
            acts = r.call("incoming_data", r.peer, did, DataOpen(transfer_id=r.T, file_index=0, resume_seq=1))
            assert [type(a) for a in acts] == [tx.tr.CloseData], acts
            continue
        assert acts == [], (step, acts)
    assert r.e.export_tombstones(r.now) == ()


def test_new_offer_after_suspend_is_handled_as_not_receiving(tx):
    """Tạm dừng nhận là đóng node: `suspend_receiving` tắt nhận, nên `offer` mới (`transfer_id` khác) sau
    đó được trả `reject{not_receiving}` (A3), không hỏi người dùng, không tạo thư mục tạm."""
    tr = tx.tr
    r, _ = _receiving_mid_file(tx)
    r.call("suspend_receiving")
    assert r.e.receiving is False
    other = Offer(transfer_id=tx.T2, block_size=BLOCK_SIZE, files=tx.entries(1))
    acts = r.send_offer(other)
    assert tx.sent(acts) == [tx.Reject(transfer_id=tx.T2, reason="not_receiving")]
    assert tx.of(acts, tr.CheckSpace | tr.AskUser | tr.CreateTemp) == []


# ---- Hành động mới: CreateTemp.offer, RemoveTemp.keep_record, ClearBlocks -----------------------


def test_create_temp_carries_the_accepted_offer(tx):
    r = tx.Recv(tx.entries(2, 0))
    acts = r.accept()
    ct = tx.one(acts, tx.tr.CreateTemp)
    assert (ct.device_id, ct.transfer_id, ct.offer) == (r.peer, r.T, r.offer)


def test_remove_temp_keeps_record_when_done(tx):
    r = tx.Recv(tx.entries(2, 0, 1))
    r.accept()
    acts = r.finish_all()
    (rm,) = tx.of(acts, tx.tr.RemoveTemp)
    assert rm.keep_record is True
    assert [e.outcome for e in tx.ended(acts)] == [tx.tr.Outcome.DONE]


@pytest.mark.parametrize("how", ["user", "peer", "disk", "expired"])
def test_remove_temp_does_not_keep_record_when_not_done(tx, how):
    tr = tx.tr
    r = tx.Recv(tx.entries(3))
    r.accept()
    if how == "user":
        acts = r.call("cancel", r.key)
    elif how == "peer":
        acts = r.msg(Cancel(transfer_id=r.T, reason="user"))
    elif how == "disk":
        acts = r.call("storage_failed", r.peer, r.T, tr.DiskOp.WRITE)
    else:
        r.lost(now=100.0)
        acts = r.tick(now=100.0 + tr.RECONNECT_GRACE)
    (rm,) = tx.of(acts, tr.RemoveTemp)
    assert rm.keep_record is False


def _all_blocks_reread(tx, n=4):
    r = tx.Recv(tx.entries(n))
    r.accept()
    did, _ = r.open()
    r.sync_all(r.feed(did, 0, range(n)))
    r.call("data_closed", r.peer, did, False)
    assert tx.one(r.complete(0), tx.tr.Reread)
    return r


def test_reread_with_bad_blocks_emits_clear_blocks_for_them(tx):
    tr = tx.tr
    r = _all_blocks_reread(tx)
    acts = r.call("reread_done", r.peer, r.T, 0, [1, 3], None)
    assert tx.one(acts, tr.ClearBlocks) == tr.ClearBlocks(
        device_id=r.peer, transfer_id=r.T, file_index=0, indices=(1, 3)
    )
    assert tx.bits_on(tx.sent(acts, Resume)[-1].bitfield, 4) == {0, 2}


def test_reread_with_wrong_root_emits_clear_all(tx):
    tr = tx.tr
    r = _all_blocks_reread(tx)
    acts = r.call("reread_done", r.peer, r.T, 0, [], b"\x01" * 32)
    assert tx.one(acts, tr.ClearBlocks) == tr.ClearBlocks(
        device_id=r.peer, transfer_id=r.T, file_index=0, indices=None
    )
    assert tx.bits_on(tx.sent(acts, Resume)[-1].bitfield, 4) == set()


def test_clean_reread_emits_no_clear_blocks(tx):
    r = _all_blocks_reread(tx)
    acts = r.call("reread_done", r.peer, r.T, 0, [], r.root(0))
    assert tx.of(acts, tx.tr.ClearBlocks) == []
    assert tx.one(acts, tx.tr.Finalize)


def test_clear_blocks_precedes_next_sync_part_of_the_file(tx):
    """QĐ2: `ClearBlocks` đi FIFO đĩa trước `SyncPart` kế tiếp, để `MARK` của khối nhận lại đứng sau
    `CLEAR` trong `.state`."""
    tr = tx.tr
    r = _all_blocks_reread(tx)
    mark = len(r.all_actions())
    r.call("reread_done", r.peer, r.T, 0, [2], None)
    did, _ = r.open()
    r.sync_all(r.feed(did, 0, [2]))
    later = r.all_actions()[mark:]
    disk = [a for a in later if isinstance(a, tr.ClearBlocks | tr.SyncPart | tr.WriteBlock)]
    kinds = [type(a) for a in disk]
    assert kinds.index(tr.ClearBlocks) < kinds.index(tr.SyncPart), kinds
    sp = next(a for a in disk if isinstance(a, tr.SyncPart))
    assert [i for i, _ in sp.blocks] == [2]


def test_disk_results_arriving_after_suspend_send_nothing_and_remove_nothing(tx):
    """S1: hàng đĩa của lượt tạm dừng chạy nốt (`SyncPart`, `Reread`, `Finalize` đang chạy) rồi mới đóng
    handle; kết quả của chúng tới sau `suspend_receiving` không được sinh `verified`, `cancel` hay xóa
    thư mục tạm."""
    tr = tx.tr
    r = tx.Recv(tx.entries(3, 2))
    r.accept()
    did, _ = r.open()
    acts = r.feed(did, 0, range(3))
    sp = tx.of(acts, tr.SyncPart)[-1]
    r.call("suspend_receiving")
    later = []
    later += r.call("synced", r.peer, r.T, 0, sp.token)
    later += r.call("block_received", r.peer, did, 0, r.dg(0, 0))
    later += r.call("reread_done", r.peer, r.T, 0, [], r.root(0))
    later += r.call("finalized", r.peer, r.T, 0, "f0.bin")
    later += r.call("storage_failed", r.peer, r.T, tr.DiskOp.SYNC)
    no_wire_no_remove(tx, later)
    assert tx.ended(later) == []
    assert r.e.export_tombstones(r.now) == ()


def test_pure_queries_before_restore_do_not_block_it(tx):
    e = fresh(tx)
    assert e.export_tombstones(5.0) == ()
    assert e.next_wakeup() is None and e.transfers() == () and e.promised_bytes() == 0
    restore_only(tx, e, tombstones=(tomb(tx),), now=5.0)
    assert [t.transfer_id for t in e.export_tombstones(5.0)] == [tx.T1]


def test_restored_receive_keeps_promised_space_for_what_is_still_missing(tx):
    """M2: `promised_bytes` giữ chỗ cho phần còn thiếu của lượt khôi phục, để `offer` khác không lấy mất."""
    files = tx.entries(2, 6, 1, last=700)
    r, _ = restored_recv(tx, files, f=1, on=(0, 1, 2))
    total = sum(x.size for x in files)
    assert r.e.promised_bytes() == total - files[0].size - 3 * BLOCK_SIZE


# ---- temp_created: `accept`/`resume` đầu tiên chỉ sau khi thư mục tạm đã bền ---------------------


def _types(msgs) -> list[str]:
    return [type(m).__name__ for m in msgs]


@pytest.mark.parametrize("client", [False, True])
def test_decide_holds_accept_and_resume_until_temp_created(tx, client):
    """spec-p3 “`temp_created`”: `decide` phát `CreateTemp` (và `OpenPart`) nhưng không gửi gì; sau
    `temp_created`: `accept` rồi `resume(0)`, mỗi loại đúng một lần (G1), `OpenData` sau `resume` (G3)."""
    tr = tx.tr
    r = tx.Recv(tx.entries(3, 1))
    r.client = client
    acts = r.accept(created=False)
    assert tx.one(acts, tr.CreateTemp).transfer_id == r.T
    assert tx.of(acts, tr.OpenPart), "decide vẫn mở .part file 0"
    assert tx.sent(acts) == [] and tx.of(acts, tr.OpenData) == []
    acts = r.created()
    msgs = tx.sent(acts)
    assert _types(msgs) == ["Accept", "Resume"]
    assert msgs[0] == tx.Accept(transfer_id=r.T)
    assert (msgs[1].file_index, tx.bits_on(msgs[1].bitfield, 3)) == (0, set())
    if client:
        od = tx.one(acts, tr.OpenData)
        assert od.message.resume_seq == msgs[1].seq
        assert tx.index_of(acts, lambda a: isinstance(a, tr.OpenData)) > tx.index_of(
            acts, lambda a: isinstance(a, tr.SendMessage) and isinstance(a.message, Resume)
        )
    else:
        assert tx.of(acts, tr.OpenData) == []
    assert tx.of(acts, tr.CreateTemp | tr.OpenPart) == []
    # Chỉ một lần: các lời gọi sau không gửi lại.
    assert tx.sent(r.created()) == []


def test_control_up_before_temp_created_sends_nothing_then_temp_created_sends_both(tx):
    r = tx.Recv(tx.entries(2))
    r.accept(created=False)
    r.lost()
    up = r.up(cid=2)
    assert [m for m in tx.sent(up) if isinstance(m, tx.Accept | Resume)] == []
    acts = r.created()
    assert _types(tx.sent(acts)) == ["Accept", "Resume"]
    assert all(a.control_id == 2 for a in tx.of(acts, tx.tr.SendMessage))


def test_temp_created_while_offline_waits_for_control_up(tx):
    r = tx.Recv(tx.entries(2))
    r.accept(created=False)
    r.lost()
    assert tx.sent(r.created()) == []
    up = r.up(cid=2)
    assert _types(tx.sent(up)) == ["Accept", "Resume"]


def test_resent_offer_before_temp_created_is_ignored(tx):
    r = tx.Recv(tx.entries(2))
    r.accept(created=False)
    acts = r.send_offer()
    assert tx.sent(acts) == []
    assert tx.ended(acts) == [] and r.e.info(r.key) is not None


def test_complete_before_temp_created_is_an_error_with_tombstone(tx):
    """Như luật 5 (protocol §6.3): `complete` của lượt mà máy này chưa gửi `accept` → `cancel{error}` +
    bia mộ; thư mục tạm mà lượt đã đặt tạo bị xóa."""
    tr = tx.tr
    r = tx.Recv(tx.entries(2))
    r.accept(created=False)
    acts = r.complete(0)
    assert tx.sent(acts) == [Cancel(transfer_id=r.T, reason="error")]
    (end,) = tx.ended(acts)
    assert (end.outcome, end.reason) == (tr.Outcome.CANCELLED, "error")
    assert tx.one(acts, tr.RemoveTemp).keep_record is False
    assert changed(tx, acts)
    (t,) = r.e.export_tombstones(r.now)
    assert (t.direction, t.transfer_id, t.reason) == (tr.Direction.RECEIVE, r.T, "error")


def test_data_open_before_temp_created_is_only_closed(tx):
    tr = tx.tr
    r = tx.Recv(tx.entries(2))
    r.accept(created=False)
    did = r.new_did()
    acts = r.call("incoming_data", r.peer, did, DataOpen(transfer_id=r.T, file_index=0, resume_seq=1))
    assert acts == [tr.CloseData(device_id=r.peer, data_id=did)]


def test_create_temp_failure_before_temp_created_never_sends_accept(tx):
    tr = tx.tr
    r = tx.Recv(tx.entries(2))
    r.accept(created=False)
    r.now += 0.01
    acts = r.e.storage_failed(r.peer, r.T, tr.DiskOp.CREATE_TEMP, r.now, detail=tr.FailureDetail.TEMP_EXISTS)
    tx.check_order(acts)
    r.log.append(("storage_failed", acts))
    assert tx.sent(acts) == [Cancel(transfer_id=r.T, reason="error")]
    assert tx.of(acts, tr.RemoveTemp) == []  # không tạo được thì không phải của lượt này
    (end,) = tx.ended(acts)
    assert end.detail is tr.FailureDetail.TEMP_EXISTS
    assert r.created() == []
    assert [m for m in tx.sent(r.all_actions()) if isinstance(m, tx.Accept)] == []


def test_temp_created_for_unknown_duplicate_or_ended_transfer_does_nothing(tx):
    r = tx.Recv(tx.entries(2))
    assert r.call("temp_created", r.peer, tx.T3) == []  # lượt lạ
    r.accept()
    assert r.created() == []  # lặp
    r.call("cancel", r.key)
    assert r.created() == []  # đã kết thúc
    other = tx.Recv(tx.entries(1), T=tx.T2, peer=tx.OTHER, like=r, cid=4)
    assert other.call("temp_created", tx.OTHER, r.T) == []  # cùng `transfer_id`, thiết bị khác


def test_temp_created_for_offer_not_yet_accepted_does_nothing(tx):
    r = tx.Recv(tx.entries(2))
    r.to_asking()
    assert r.created() == []
    acts = r.call("decide", r.peer, r.T, True)
    assert tx.sent(acts) == [], "temp_created tới trước decide không được tính"


def test_restored_receive_does_not_wait_for_temp_created(tx):
    r, _ = restored_recv(tx, tx.entries(4), on=(0,))
    assert _types(tx.sent(r.up())) == ["Accept", "Resume"]
    assert r.created() == []


def test_user_cancel_before_temp_created_sends_cancel_and_removes_temp(tx):
    """Hủy lúc chưa `temp_created`: máy gửi chưa thấy `accept`, vẫn nhận `cancel{user}` và bia mộ."""
    tr = tx.tr
    r = tx.Recv(tx.entries(2))
    r.accept(created=False)
    acts = r.call("cancel", r.key)
    assert tx.sent(acts) == [Cancel(transfer_id=r.T, reason="user")]
    assert tx.one(acts, tr.RemoveTemp).keep_record is False


# ---- restore: lượt nhận trùng khóa bia mộ hay lượt đã xong -------------------------------------


@pytest.mark.parametrize("unsent", [True, False])
def test_restored_receive_with_tombstone_of_same_key_only_removes_temp(tx, unsent):
    """Lượt đã hủy mà chưa kịp dọn: bia mộ RECEIVE cùng khóa thắng → chỉ `RemoveTemp`, không có lượt.
    Bia mộ `unsent` thì `control_up` gửi `cancel`; không thì `offer` gửi lại nhận lại `cancel` đó."""
    tr = tx.tr
    files = tx.entries(3)
    stone = tomb(tx, direction=tr.Direction.RECEIVE, T=tx.T1, reason="user", unsent=unsent, age=50.0)
    r, acts = restored_recv(tx, files, on=(0,), tombstones=(stone,), age=40.0)
    assert tx.of(acts, tr.RemoveTemp) == [tr.RemoveTemp(device_id=r.peer, transfer_id=r.T, keep_record=False)]
    assert tx.statuses(acts, r.key) == []
    assert r.e.transfers() == () and r.e.info(r.key) is None
    up = r.up()
    sent = tx.sent(up)
    if unsent:
        assert sent == [Cancel(transfer_id=r.T, reason="user")]
    else:
        assert sent == []
        assert tx.sent(r.send_offer()) == [Cancel(transfer_id=r.T, reason="user")]
    assert [m for m in tx.sent(r.all_actions()) if isinstance(m, tx.Accept | Resume)] == []


def test_restored_receive_with_send_tombstone_of_same_id_is_still_restored(tx):
    """Đối chứng: bia mộ chiều SEND cùng `transfer_id` là khóa khác → lượt nhận vẫn được khôi phục."""
    tr = tx.tr
    stone = tomb(tx, direction=tr.Direction.SEND, T=tx.T1)
    r, acts = restored_recv(tx, tx.entries(3), on=(0,), tombstones=(stone,))
    assert tx.of(acts, tr.RemoveTemp) == []
    assert r.e.info(r.key) is not None


def test_restore_rejects_receive_with_same_key_as_live_done(tx):
    tr = tx.tr
    e = fresh(tx)
    item = _receive_item(tx)
    done = tr.RestoredDone(device_id=tx.PEER, offer=item.offer, age=10.0)
    with pytest.raises(ValueError):
        e.restore(receives=(item,), tombstones=(), done=(done,), now=1000.0)
    assert e.transfers() == ()


def test_restore_accepts_receive_with_same_key_as_expired_done(tx):
    """Đối chứng: lượt đã xong quá `REMEMBER_TTL` bị bỏ, nên không còn trùng khóa."""
    tr = tx.tr
    e = fresh(tx)
    item = _receive_item(tx)
    done = tr.RestoredDone(device_id=tx.PEER, offer=item.offer, age=tr.REMEMBER_TTL)
    restore_only(tx, e, receives=(item,), done=(done,), now=1000.0)
    assert len(e.transfers()) == 1


# ---- control_up xử lý hết hạn trước khi đồng bộ lại ---------------------------------------------


def test_control_up_before_first_tick_expires_restored_receive_older_than_grace(tx):
    """`restore(age ≥ RECONNECT_GRACE)` rồi `control_up` tới trước `tick`: lượt hết hạn ngay (Q3) và
    `cancel{expired}` được gửi trên kết nối đó; không `accept`, không `resume`."""
    tr = tx.tr
    now = 50_000.0
    r, _ = restored_recv(tx, tx.entries(3), on=(0,), age=tr.RECONNECT_GRACE, now=now)
    acts = r.up(now=now + 0.5)
    sent = tx.sent(acts)
    assert Cancel(transfer_id=r.T, reason="expired") in sent
    assert [m for m in sent if isinstance(m, tx.Accept | Resume)] == []
    (end,) = tx.ended(acts)
    assert (end.key, end.outcome, end.reason) == (r.key, tr.Outcome.CANCELLED, "expired")
    assert tx.one(acts, tr.RemoveTemp).keep_record is False
    assert tx.of(acts, tr.OpenData) == []
    # Bia mộ đã gửi trên kết nối này: không còn `unsent`.
    (t,) = r.e.export_tombstones(r.now)
    assert (t.reason, t.unsent) == ("expired", False)
    assert tx.sent(r.send_offer()) == [Cancel(transfer_id=r.T, reason="expired")]


def test_control_up_before_grace_still_resyncs_restored_receive(tx):
    """Đối chứng: tuổi ngay dưới hạn → `control_up` đồng bộ lại bình thường."""
    tr = tx.tr
    now = 50_000.0
    r, _ = restored_recv(tx, tx.entries(3), on=(0,), age=tr.RECONNECT_GRACE - 1.0, now=now)
    acts = r.up(now=now + 0.5)
    assert _types(tx.sent(acts)) == ["Accept", "Resume"]
    assert tx.ended(acts) == []
