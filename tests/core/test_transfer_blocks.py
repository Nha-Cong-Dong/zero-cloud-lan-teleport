"""Bên nhận: khối, ghi đĩa, `fsync` theo lô, khối sai và thứ tự khối.

Spec P2-4 mục 3.6, 3.7; AC-9, AC-10 và các ca biên “khối đầu tiên của kết nối là khối cuối file”,
“`data_open` trùng cùng `seq` ở bên nhận là server”. Nguồn: docs/protocol.md §4.2, §4.3. Viết từ
spec, không từ code.
"""

from __future__ import annotations

from collections import deque

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import teleport.core.transfer as transfer
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.messages import Cancel, Resume
from teleport.core.transfer import (
    CloseData,
    FailureDetail,
    OpenData,
    Outcome,
    ReceiveBlocks,
    RemoveTemp,
    SyncPart,
    WriteBlock,
)

HYPO = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def writes(tx, acts) -> list[int]:
    return [a.index for a in tx.of(acts, WriteBlock)]


def batches(tx, acts) -> list[list[int]]:
    return [[i for i, _ in sp.blocks] for sp in tx.of(acts, SyncPart)]


def bad_round(tx, r, did):
    """`block_bad` rồi trả lời mọi `SyncPart` đẩy sớm (để `resume` được gửi)."""
    acts = r.call("block_bad", tx.PEER, did)
    return acts + r.sync_all(acts)


# ---- AC-9: bit chỉ bật sau `synced` --------------------------------------------------------------


def test_sync_part_every_sixteen_blocks_and_when_file_is_complete(tx):
    r = tx.Recv(tx.entries(40))
    r.accept()
    did, _ = r.open()
    acts = r.feed(did, 0, range(40))
    assert writes(tx, acts) == list(range(40))
    sps = tx.of(acts, SyncPart)
    assert [[i for i, _ in sp.blocks] for sp in sps] == [
        list(range(16)),
        list(range(16, 32)),
        list(range(32, 40)),
    ]
    for sp in sps:
        assert (sp.device_id, sp.transfer_id, sp.file_index) == (tx.PEER, r.T, 0)
        assert [d for _, d in sp.blocks] == [r.dg(0, i) for i, _ in sp.blocks]
    tokens = [sp.token for sp in sps]
    assert len(set(tokens)) == 3
    assert all(type(t) is int and t >= 1 for t in tokens)


def test_write_block_precedes_the_sync_part_containing_it(tx):
    r = tx.Recv(tx.entries(16))
    r.accept()
    did, _ = r.open()
    acts = r.feed(did, 0, range(15))
    assert tx.of(acts, SyncPart) == []
    acts = r.call("block_received", tx.PEER, did, 15, r.dg(0, 15))
    assert [type(a) for a in acts if not isinstance(a, tx.tr.Report)] == [WriteBlock, SyncPart]


def test_sync_every_is_read_at_use(tx, monkeypatch):
    monkeypatch.setattr(transfer, "SYNC_EVERY", 4)
    r = tx.Recv(tx.entries(10))
    r.accept()
    did, _ = r.open()
    assert batches(tx, r.feed(did, 0, range(10))) == [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]]


def test_bits_and_progress_change_only_on_synced(tx):
    r = tx.Recv(tx.entries(20))
    r.accept()
    did, _ = r.open()
    acts = r.feed(did, 0, range(16))
    assert r.e.info(r.key).done_bytes == 0  # đã ghi, chưa fsync
    r.sync(acts)
    assert r.e.info(r.key).done_bytes == 16 * BLOCK_SIZE


def test_resume_waits_for_early_sync_of_unsynced_blocks(tx):
    r = tx.Recv(tx.entries(10))
    r.accept()
    did, _ = r.open()
    r.feed(did, 0, range(5))
    acts = r.call("data_closed", tx.PEER, did, False)  # có khối mới: xin `resume` ngay
    assert batches(tx, acts) == [[0, 1, 2, 3, 4]]
    assert tx.sent(acts, Resume) == []
    acts = r.sync(acts)
    res = tx.one(tx.sent(acts, Resume), object)
    assert res.seq == 2
    assert tx.bits_on(res.bitfield, 10) == {0, 1, 2, 3, 4}


def test_resume_waits_for_every_pending_batch(tx):
    r = tx.Recv(tx.entries(30))
    r.accept()
    did, _ = r.open()
    a = r.feed(did, 0, range(20))
    first = tx.of(a, SyncPart)
    assert batches(tx, a) == [list(range(16))]
    b = r.call("data_closed", tx.PEER, did, False)
    second = tx.of(b, SyncPart)
    assert batches(tx, b) == [[16, 17, 18, 19]]
    assert tx.sent(r.sync(first), Resume) == []
    res = tx.one(tx.sent(r.sync(second), Resume), object)
    assert tx.bits_on(res.bitfield, 30) == set(range(20))


@pytest.mark.parametrize("client", [False, True])
@pytest.mark.parametrize("trigger", ["data_closed", "block_bad"])
def test_old_data_open_is_refused_while_new_resume_waits_for_fsync(tx, trigger, client):
    """Bên nhận xin `resume` mới trong lúc lô cuối còn chờ `fsync`, rồi một kết nối dữ liệu cho
    `resume` cũ tới:
    - máy này là server: bên gửi (client) mở lại đúng `data_open` cũ (protocol §4.1: client là bên
      gửi mở lại cùng `resume_seq` khi kết nối đóng trước khi gửi hết) → `incoming_data` cũ;
    - máy này là client: một lần mở cho `data_open` cũ báo thành công muộn → `outgoing_data` cũ.
    Kết nối đó không được nhận khối:
    - protocol §4.1 gạch cuối: “bên nhận đóng kết nối dữ liệu hiện tại của T mỗi khi xin `resume`
      mới, và từ lúc xin (kể cả trong lúc chờ `fsync` trước khi gửi, mục 4.3) không nhận kết nối
      dữ liệu nào cho `resume` cũ nữa”; spec 3.6 “Xin `resume` ngay” bước 2 và D50: không có khối
      mới tới trong lúc chờ `fsync`;
    - spec 3.6 Vai RECEIVE / `outgoing_data`: không khớp → `CloseData`.
    `resume` gửi ở `synced` chứa mọi khối đã ghi (spec 3.6: “`resume` không bao giờ xin lại khối đã
    ghi”; protocol §4.3: `fsync` trước `resume` để `resume` không xin lại chúng).
    """
    r = tx.Recv(tx.entries(30), client=client)
    r.accept()  # `resume` seq 1
    d1, _ = r.open()
    first = r.feed(d1, 0, range(20))
    assert batches(tx, first) == [list(range(16))]
    assert tx.sent(r.sync(first), Resume) == []
    extra = (False,) if trigger == "data_closed" else ()  # `data_closed(finished=False)` / `block_bad`
    acts = r.call(trigger, tx.PEER, d1, *extra)
    second = tx.of(acts, SyncPart)
    assert batches(tx, second) == [[16, 17, 18, 19]]
    assert tx.sent(acts, Resume) == []  # chờ `fsync` lô 16..19
    assert tx.of(acts, OpenData) == []  # `OpenData` chỉ đi sau `resume` của nó (G3)
    # Kết nối dữ liệu cho `resume` seq 1 tới trong lúc bên nhận chờ `fsync`.
    d2, acts = r.open(seq=1)
    assert tx.of(acts, ReceiveBlocks) == [], "kết nối của `resume` cũ được nhận trong lúc chờ fsync"
    assert CloseData(device_id=tx.PEER, data_id=d2) in acts
    # Khung tới trên kết nối đó (nếu app kịp đọc) không được ghi.
    assert writes(tx, r.feed(d2, 0, [20])) == []
    acts = r.sync(second)
    res = tx.one(tx.sent(acts, Resume), object)
    assert res.seq == 2
    if client:
        assert tx.one(acts, OpenData).message.resume_seq == 2
    on = tx.bits_on(res.bitfield, 30)
    assert set(writes(tx, r.all_actions())) <= on, "`resume` xin lại khối đã ghi"
    assert on == set(range(20))
    # `resume` mới được phục vụ bình thường.
    d3, acts = r.open()
    assert tx.one(acts, ReceiveBlocks).data_id == d3
    assert writes(tx, r.feed(d3, 0, [20])) == [20]


def test_duplicate_blocks_are_not_written_again(tx):
    r = tx.Recv(tx.entries(6))
    r.accept()
    d1, _ = r.open()
    r.feed(d1, 0, [0, 1, 2])  # đang chờ fsync
    d2, acts = r.open()  # `data_open` trùng cùng `seq`: thay kết nối cũ
    assert tx.index_of(acts, lambda a: a == CloseData(device_id=tx.PEER, data_id=d1)) < tx.index_of(
        acts, lambda a: isinstance(a, ReceiveBlocks) and a.data_id == d2
    )
    acts = r.feed(d2, 0, [0, 1, 2])
    assert writes(tx, acts) == []
    assert tx.of(acts, CloseData) == []
    acts = r.feed(d2, 0, [3])
    assert writes(tx, acts) == [3]


def test_duplicates_of_synced_blocks_are_not_written_again(tx):
    r = tx.Recv(tx.entries(20))
    r.accept()
    d1, _ = r.open()
    r.sync_all(r.feed(d1, 0, range(16)))
    d2, _ = r.open()
    acts = r.feed(d2, 0, range(16))
    assert writes(tx, acts) == []
    assert writes(tx, r.feed(d2, 0, [16])) == [16]


def test_unknown_or_finished_token_is_ignored(tx):
    r = tx.Recv(tx.entries(16))
    r.accept()
    did, _ = r.open()
    acts = r.feed(did, 0, range(16))
    sp = tx.one(acts, SyncPart)
    assert r.call("synced", tx.PEER, r.T, 0, sp.token + 12345) == []
    assert r.call("synced", tx.PEER, tx.T3, 0, sp.token) == []
    r.sync(acts)
    assert r.call("synced", tx.PEER, r.T, 0, sp.token) == []


def test_full_file_without_complete_waits_silently(tx):
    r = tx.Recv(tx.entries(3))
    r.accept()
    did, _ = r.open()
    acts = r.feed(did, 0, range(3))
    acts = r.sync(acts)
    assert tx.sent(acts) == []
    assert tx.of(acts, (tx.tr.Reread, OpenData)) == []
    assert r.call("data_closed", tx.PEER, did, False) == []
    assert r.e.next_wakeup() is None


@pytest.mark.parametrize("client", [False, True])
@pytest.mark.parametrize("how", ["switch", "lost"])
def test_resync_resume_waiting_for_fsync_is_sent_even_when_file_becomes_full(tx, how, client):
    """Kết nối điều khiển mới tới khi bên nhận đã ghi đủ khối của file nhưng lô cuối còn chờ `fsync`.
    Protocol §6.3 cột “Nhận”: trên kết nối mới, bên nhận gửi `accept` **rồi** `resume` cho file hiện
    tại; spec 3.3 bước 5 và 3.6 (“Xin `resume` ngay” bước 5): còn lô chờ thì `resume` đi ở `synced`
    cuối cùng. Lô đó làm file vừa đủ bit, nhưng `resume` đã hoãn vẫn phải đi (đủ bit: không mở kết
    nối dữ liệu, protocol §4.1), vì đó là cách duy nhất để bên gửi trả lại `cancel` đã mất (§6.3).
    Spec 3.7 “chưa có root → chờ `complete`, không gửi gì” (D5) chỉ nói bên nhận không *tự* xin
    `resume` để kéo `complete`; xem `test_full_file_without_complete_waits_silently`.
    """
    r = tx.Recv(tx.entries(20), client=client)
    r.accept()
    did, _ = r.open()
    acts = r.feed(did, 0, range(20))
    first, last = tx.of(acts, SyncPart)
    assert [i for i, _ in last.blocks] == list(range(16, 20))
    r.sync([first])
    waiting = [last]
    if how == "lost":
        waiting += tx.of(r.lost(), SyncPart)  # lô 16..19 vẫn chờ `fsync` khi kết nối có lại
    acts = r.up(cid=2, client=client)
    assert tx.sent(acts) == [tx.Accept(transfer_id=r.T)]  # `resume` chờ `fsync` lô 16..19
    assert tx.of(acts, OpenData) == []
    acts = r.sync(waiting)
    assert tx.sent(acts) == [
        Resume(transfer_id=r.T, file_index=0, seq=1, bitfield=tx.bits(20, range(20)).to_base64())
    ]
    assert tx.of(acts, (OpenData, ReceiveBlocks, transfer.Reread)) == []  # chưa có root: chưa đọc lại
    assert r.e.next_wakeup() is None  # `resume` đủ bit: không chờ `data_open`, không hẹn gì
    # `complete` tới (bên gửi gửi lại ở lần đồng bộ) → đọc lại như thường.
    assert tx.one(r.complete(0), transfer.Reread).file_index == 0


@HYPO
@given(data=st.data())
def test_bits_are_never_ahead_of_synced_batches(tx, data):
    n = data.draw(st.integers(1, 40), label="n")
    r = tx.Recv(tx.entries(n))
    r.accept()
    did, _ = r.open()
    pending: deque[tuple[int, list[int]]] = deque()
    synced: set[int] = set()
    live = True
    for _ in range(data.draw(st.integers(1, 120), label="steps")):
        op = data.draw(st.sampled_from(["block", "block", "block", "sync", "close", "bad", "reopen"]))
        if op == "block" and did is not None:
            i = data.draw(st.integers(0, n - 1))
            acts = r.call("block_received", tx.PEER, did, i, r.dg(0, i))
        elif op == "sync" and pending:
            tok, idxs = pending.popleft()
            acts = r.call("synced", tx.PEER, r.T, 0, tok)
            synced |= set(idxs)
        elif op == "close" and did is not None:
            acts = r.call("data_closed", tx.PEER, did, False)
            did = None
        elif op == "bad" and did is not None:
            acts = r.call("block_bad", tx.PEER, did)
            did = None
        elif op == "reopen":
            res = r.last_resume()
            if tx.bits_on(res.bitfield, n) == set(range(n)):
                continue
            did, acts = r.open()
        else:
            continue
        for sp in tx.of(acts, SyncPart):
            pending.append((sp.token, [i for i, _ in sp.blocks]))
        for res in tx.sent(acts, Resume):
            assert tx.bits_on(res.bitfield, n) <= synced
        if did is not None and CloseData(device_id=tx.PEER, data_id=did) in acts:
            did = None
        if tx.ended(acts):
            live = False
            break
        assert r.e.info(r.key).done_bytes == len(synced) * BLOCK_SIZE
    if live:
        assert r.e.info(r.key).done_bytes == len(synced) * BLOCK_SIZE


# ---- AC-10: khối sai và thứ tự khối --------------------------------------------------------------


@pytest.mark.parametrize("client", [False, True])
def test_three_bad_blocks_resume_immediately_fourth_cancels(tx, client):
    r = tx.Recv(tx.entries(4), client=client)
    r.accept()
    for k in range(3):
        did, acts = r.open()
        assert tx.one(acts, ReceiveBlocks).data_id == did
        acts = bad_round(tx, r, did)
        assert CloseData(device_id=tx.PEER, data_id=did) in acts
        res = tx.one(tx.sent(acts, Resume), object)
        assert res.seq == k + 2
        if client:
            assert tx.one(acts, OpenData).message.resume_seq == res.seq
        # `resume` gửi ngay, không hẹn giờ (D33). Máy này là server thì chỉ còn hạn chờ `data_open`
        # của `resume` thiếu khối vừa gửi (protocol §4.1).
        assert r.e.next_wakeup() == (None if client else pytest.approx(r.now + transfer.DATA_OPEN_WAIT))
        assert tx.ended(acts) == []
    did, _ = r.open()
    acts = bad_round(tx, r, did)
    assert tx.sent(acts) == [Cancel(transfer_id=r.T, reason="error")]
    end = tx.one(tx.ended(acts), object)
    assert (end.outcome, end.reason, end.by_peer, end.detail, end.file_index) == (
        Outcome.CANCELLED,
        "error",
        False,
        FailureDetail.CORRUPT_BLOCKS,
        0,
    )
    assert tx.one(acts, RemoveTemp).transfer_id == r.T


def test_new_good_block_resets_bad_counter(tx):
    r = tx.Recv(tx.entries(6))
    r.accept()
    for _ in range(3):
        did, _ = r.open()
        bad_round(tx, r, did)
    did, _ = r.open()
    assert writes(tx, r.feed(did, 0, [0])) == [0]
    acts = bad_round(tx, r, did)
    assert tx.ended(acts) == []
    for _ in range(2):
        did, _ = r.open()
        assert tx.ended(bad_round(tx, r, did)) == []
    did, _ = r.open()
    assert tx.one(tx.ended(bad_round(tx, r, did)), object).detail == FailureDetail.CORRUPT_BLOCKS


def test_duplicate_block_does_not_reset_bad_counter(tx):
    r = tx.Recv(tx.entries(6))
    r.accept()
    did, _ = r.open()
    r.feed(did, 0, [0])  # khối mới: bộ đếm = 0
    for k in range(4):
        if k:
            did, _ = r.open()
            assert writes(tx, r.feed(did, 0, [0])) == []  # khối trùng
        acts = bad_round(tx, r, did)
        if k < 3:
            assert tx.ended(acts) == []
    end = tx.one(tx.ended(acts), object)
    assert (end.reason, end.detail) == ("error", FailureDetail.CORRUPT_BLOCKS)


def test_plain_data_close_does_not_count_as_bad_block(tx):
    r = tx.Recv(tx.entries(6))
    r.accept()
    for _ in range(2):
        did, _ = r.open()
        bad_round(tx, r, did)
    for _ in range(5):
        did, _ = r.open()
        acts = r.call("data_closed", tx.PEER, did, False)  # không mang khối nào: hẹn `resume`
        assert tx.ended(acts) == []
        r.tick_due()
    did, _ = r.open()
    assert tx.ended(bad_round(tx, r, did)) == []  # lần sai thứ 3
    did, _ = r.open()
    assert tx.ended(bad_round(tx, r, did))  # lần sai thứ 4


def test_block_not_strictly_increasing_on_connection_counts_as_bad(tx):
    r = tx.Recv(tx.entries(5))
    r.accept()
    did, _ = r.open()
    r.feed(did, 0, [0, 1])
    acts = r.call("block_received", tx.PEER, did, 1, r.dg(0, 1))
    assert writes(tx, acts) == []
    assert CloseData(device_id=tx.PEER, data_id=did) in acts
    acts += r.sync_all(acts)
    assert tx.one(tx.sent(acts, Resume), object).seq == 2


def test_block_going_backwards_on_connection_counts_as_bad(tx):
    r = tx.Recv(tx.entries(5))
    r.accept()
    did, _ = r.open()
    r.feed(did, 0, [0, 1, 2])
    acts = r.call("block_received", tx.PEER, did, 0, r.dg(0, 0))
    assert writes(tx, acts) == []
    assert CloseData(device_id=tx.PEER, data_id=did) in acts


def test_first_block_of_connection_being_last_block_of_file_counts_as_bad(tx):
    r = tx.Recv(tx.entries(5))
    r.accept()
    did, _ = r.open()
    acts = r.call("block_received", tx.PEER, did, 4, r.dg(0, 4))
    assert writes(tx, acts) == []
    assert CloseData(device_id=tx.PEER, data_id=did) in acts
    assert tx.one(tx.sent(acts, Resume), object).seq == 2


def test_new_block_beyond_highest_written_plus_one_counts_as_bad(tx):
    r = tx.Recv(tx.entries(8))
    r.accept()
    did, _ = r.open()
    r.feed(did, 0, [0, 1])
    acts = r.call("block_received", tx.PEER, did, 3, r.dg(0, 3))  # hw = 1, khối 3 > hw + 1
    assert writes(tx, acts) == []
    assert CloseData(device_id=tx.PEER, data_id=did) in acts


def test_block_index_beyond_file_counts_as_bad_not_error(tx):
    r = tx.Recv(tx.entries(3))
    r.accept()
    did, _ = r.open()
    acts = r.call("block_received", tx.PEER, did, 3, r.dg(0, 3))
    assert writes(tx, acts) == []
    assert CloseData(device_id=tx.PEER, data_id=did) in acts


def test_four_order_violations_cancel_the_transfer(tx):
    r = tx.Recv(tx.entries(5))
    r.accept()
    for k in range(4):
        did, _ = r.open()
        acts = r.call("block_received", tx.PEER, did, 4, r.dg(0, 4))
        acts += r.sync_all(acts)
        if k < 3:
            assert tx.ended(acts) == []
    assert tx.one(tx.ended(acts), object).detail == FailureDetail.CORRUPT_BLOCKS


def test_duplicate_data_open_resending_received_blocks_counts_nothing(tx):
    r = tx.Recv(tx.entries(8))
    r.accept()
    d1, _ = r.open()
    r.feed(d1, 0, [0, 1, 2])
    d2, acts = r.open()  # bên gửi mở lại cùng `data_open`, gửi lại từ đầu
    assert CloseData(device_id=tx.PEER, data_id=d1) in acts
    acts = r.feed(d2, 0, [0, 1, 2, 3])
    assert writes(tx, acts) == [3]
    assert tx.of(acts, CloseData) == []
    # Không có lần sai nào bị đếm: còn chịu được 3 lần sai.
    for _ in range(3):
        did = d2 if d2 is not None else r.open()[0]
        assert tx.ended(bad_round(tx, r, did)) == []
        d2 = None
    did, _ = r.open()
    assert tx.ended(bad_round(tx, r, did))


def test_block_events_on_unknown_connection_return_close_data(tx):
    r = tx.Recv(tx.entries(3))
    r.accept()
    assert r.call("block_received", tx.PEER, 999, 0, r.dg(0, 0)) == [
        CloseData(device_id=tx.PEER, data_id=999)
    ]
    assert r.call("block_bad", tx.PEER, 999) == [CloseData(device_id=tx.PEER, data_id=999)]
    assert r.call("block_read", tx.PEER, 999, 0, r.dg(0, 0)) == [CloseData(device_id=tx.PEER, data_id=999)]


def test_data_closed_of_unknown_connection_is_ignored(tx):
    r = tx.Recv(tx.entries(3))
    r.accept()
    assert r.call("data_closed", tx.PEER, 999, False) == []
