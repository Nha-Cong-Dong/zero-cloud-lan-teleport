"""Hoàn tất: đọc lại và Merkle ở bên nhận, `complete` ở bên gửi, file rỗng.

Spec P2-4 mục 3.4, 3.8; AC-11, AC-12, AC-13 (phần một engine) và các ca biên “`complete` trùng có
cùng root trong lúc VERIFYING”, “file 1 khối”, “file có khối cuối ngắn”. Nguồn: docs/protocol.md
§4.4 (Merkle, F7). Viết từ spec, không từ code.
"""

from __future__ import annotations

import hashlib

import pytest

from teleport.core.bitfield import Bitfield
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.merkle import merkle_root
from teleport.core.messages import Cancel, Complete, FileEntry, Resume
from teleport.core.transfer import (
    CheckSource,
    CloseData,
    FailureDetail,
    Finalize,
    OpenData,
    Outcome,
    ReceiveBlocks,
    RemoveTemp,
    Reread,
    SendBlocks,
    TransferStatus,
)

EMPTY = hashlib.sha256(b"").digest()


def rereads(tx, acts):
    return tx.of(acts, Reread)


def full_and_synced(tx, r, f: int = 0):
    """Nhận mọi khối của file `f` trên một kết nối, fsync xong. Trả (data_id, hành động)."""
    did, acts = r.open()
    a = r.feed(did, f, range(r.n(f)))
    acts += a + r.sync_all(a)
    return did, acts


# ---- AC-11: đọc lại / Merkle -----------------------------------------------------------------------


def test_reread_gets_the_digests_received_in_block_order(tx):
    r = tx.Recv(tx.entries(3))
    r.accept()
    full_and_synced(tx, r)
    acts = r.complete(0)
    rr = tx.one(acts, Reread)
    assert (rr.device_id, rr.transfer_id, rr.file_index) == (tx.PEER, r.T, 0)
    assert bytes(rr.leaves) == r.dg(0, 0) + r.dg(0, 1) + r.dg(0, 2)
    assert tx.statuses(acts, r.key) == [TransferStatus.VERIFYING]


def test_complete_before_last_block_still_finishes(tx):
    r = tx.Recv(tx.entries(3))
    r.accept()
    did, _ = r.open()
    r.sync_all(r.feed(did, 0, [0, 1]))
    acts = r.complete(0)
    assert rereads(tx, acts) == []
    a = r.feed(did, 0, [2])
    acts = r.sync_all(a)
    rr = tx.one(acts, Reread)
    assert bytes(rr.leaves) == r.leaves(0)
    acts = r.call("reread_done", tx.PEER, r.T, 0, [], r.root(0))
    assert tx.one(acts, Finalize).file_index == 0


def test_complete_arriving_while_blocks_pending_fsync_waits_for_synced(tx):
    r = tx.Recv(tx.entries(2))
    r.accept()
    did, _ = r.open()
    a = r.feed(did, 0, [0, 1])  # đủ khối nhưng lô chưa `synced`
    assert rereads(tx, r.complete(0)) == []
    assert tx.one(r.sync(a), Reread).file_index == 0


def test_finalize_only_after_clean_reread_with_matching_root(tx):
    names = ["../../thu muc/bao cao.pdf"]
    files = (FileEntry(name=names[0], size=2 * BLOCK_SIZE + 7, mtime=1_234_567),)
    r = tx.Recv(files)
    r.accept()
    full_and_synced(tx, r)
    r.complete(0)
    acts = r.call("reread_done", tx.PEER, r.T, 0, [], r.root(0))
    fin = tx.one(acts, Finalize)
    assert (fin.device_id, fin.transfer_id, fin.file_index) == (tx.PEER, r.T, 0)
    assert fin.name == r.e.info(r.key).names[0] == "bao cao.pdf"
    assert fin.mtime == 1_234_567
    assert tx.sent(acts) == []


def test_finalized_sends_verified_and_starts_next_file(tx):
    r = tx.Recv(tx.entries(1, 2))
    r.accept()
    full_and_synced(tx, r)
    r.complete(0)
    r.call("reread_done", tx.PEER, r.T, 0, [], r.root(0))
    acts = r.call("finalized", tx.PEER, r.T, 0, "f0 (1).bin")
    assert tx.Verified(transfer_id=r.T, file_index=0) in tx.sent(acts)
    res = tx.one(tx.sent(acts, Resume), object)
    assert (res.file_index, res.seq, res.bitfield) == (1, 2, Bitfield(2).to_base64())
    op = tx.one(acts, tx.tr.OpenPart)
    assert (op.file_index, op.size) == (1, r.files[1].size)
    fv = tx.one(tx.events(acts, tx.tr.FileVerified), object)
    assert (fv.key, fv.file_index, fv.name) == (r.key, 0, "f0 (1).bin")
    assert r.e.info(r.key).file_index == 1


def test_finalized_of_last_file_ends_done(tx):
    r = tx.Recv(tx.entries(1))
    r.accept()
    did, _ = full_and_synced(tx, r)
    r.complete(0)
    r.call("reread_done", tx.PEER, r.T, 0, [], r.root(0))
    acts = r.call("finalized", tx.PEER, r.T, 0, "f0.bin")
    assert tx.sent(acts) == [tx.Verified(transfer_id=r.T, file_index=0)]
    assert CloseData(device_id=tx.PEER, data_id=did) in acts
    assert tx.one(acts, RemoveTemp).transfer_id == r.T
    end = tx.one(tx.ended(acts), object)
    assert end.outcome == Outcome.DONE
    assert end.info.done_bytes == end.info.total_bytes == r.files[0].size
    assert r.e.info(r.key) is None


def test_wrong_root_clears_all_bits_and_asks_everything_again(tx):
    r = tx.Recv(tx.entries(3))
    r.accept()
    full_and_synced(tx, r)
    r.complete(0)
    acts = r.call("reread_done", tx.PEER, r.T, 0, [], b"\x99" * 32)
    assert tx.of(acts, Finalize) == []
    res = tx.one(tx.sent(acts, Resume), object)
    assert tx.bits_on(res.bitfield, 3) == set()
    assert r.e.info(r.key).done_bytes == 0


def test_bad_block_on_reread_asks_only_that_block(tx):
    r = tx.Recv(tx.entries(5))
    r.accept()
    full_and_synced(tx, r)
    r.complete(0)
    acts = r.call("reread_done", tx.PEER, r.T, 0, [3], None)
    assert tx.of(acts, Finalize) == []
    res = tx.one(tx.sent(acts, Resume), object)
    assert tx.bits_on(res.bitfield, 5) == {0, 1, 2, 4}
    assert r.e.info(r.key).done_bytes == 4 * BLOCK_SIZE
    # Nhận lại đúng khối 3 rồi đọc lại sạch → xong.
    did, acts = r.open()
    acts = r.feed(did, 0, [3])
    assert [a.index for a in tx.of(acts, tx.tr.WriteBlock)] == [3]
    rr = tx.one(r.sync_all(acts), Reread)
    assert bytes(rr.leaves) == r.leaves(0)
    acts = r.call("reread_done", tx.PEER, r.T, 0, [], r.root(0))
    assert tx.one(acts, Finalize).file_index == 0


def test_fourth_verify_failure_cancels(tx):
    r = tx.Recv(tx.entries(3))
    r.accept()
    full_and_synced(tx, r)
    r.complete(0)
    for k in range(4):
        acts = r.call("reread_done", tx.PEER, r.T, 0, [1], None)
        if k == 3:
            break
        assert tx.ended(acts) == []
        did, _ = r.open()
        a = r.feed(did, 0, [1])
        assert tx.one(r.sync_all(a), Reread).file_index == 0
    assert tx.sent(acts) == [Cancel(transfer_id=r.T, reason="error")]
    end = tx.one(tx.ended(acts), object)
    assert (end.reason, end.by_peer, end.detail, end.file_index) == (
        "error",
        False,
        FailureDetail.VERIFY_FAILED,
        0,
    )
    assert tx.one(acts, RemoveTemp).transfer_id == r.T


def test_wrong_root_counts_toward_verify_failures(tx):
    r = tx.Recv(tx.entries(2))
    r.accept()
    full_and_synced(tx, r)
    r.complete(0)
    for k in range(4):
        acts = r.call("reread_done", tx.PEER, r.T, 0, [], b"\x01" * 32)
        if k == 3:
            break
        assert tx.ended(acts) == []
        full_and_synced(tx, r)
    assert tx.one(tx.ended(acts), object).detail == FailureDetail.VERIFY_FAILED


def test_leaves_are_not_modified_while_verifying(tx):
    r = tx.Recv(tx.entries(3))
    r.accept()
    full_and_synced(tx, r)
    rr = tx.one(r.complete(0), Reread)
    before = bytes(rr.leaves)
    # Khối trùng mang hash khác (kết nối `data_open` trùng cùng `seq`).
    did, acts = r.open()
    assert tx.one(acts, ReceiveBlocks).data_id == did
    r.feed(did, 0, [0, 1, 2], salt="khac")
    # `complete` trùng cùng root: không đọc lại lần nữa, không hủy.
    acts = r.complete(0)
    assert rereads(tx, acts) == []
    assert tx.sent(acts, Cancel) == []
    # Kết nối điều khiển đổi.
    acts = r.up(cid=2)
    assert rereads(tx, acts) == []
    assert bytes(rr.leaves) == before
    acts = r.call("reread_done", tx.PEER, r.T, 0, [], r.root(0))
    assert tx.one(acts, Finalize).file_index == 0


def test_complete_with_other_root_for_current_file_cancels(tx):
    r = tx.Recv(tx.entries(2))
    r.accept()
    r.complete(0)
    acts = r.complete(0, root=b"\x42" * 32)
    assert tx.sent(acts) == [Cancel(transfer_id=r.T, reason="error")]
    assert tx.one(tx.ended(acts), object).detail == FailureDetail.PROTOCOL


def test_complete_for_future_file_cancels(tx):
    r = tx.Recv(tx.entries(1, 1))
    r.accept()
    acts = r.complete(1)
    assert tx.sent(acts) == [Cancel(transfer_id=r.T, reason="error")]


def test_complete_beyond_files_of_running_transfer_cancels_without_file_index(tx):
    r = tx.Recv(tx.entries(1, 1))
    r.accept()
    acts = r.msg(Complete(transfer_id=r.T, file_index=2, merkle_root="ab" * 32))
    assert tx.sent(acts) == [Cancel(transfer_id=r.T, reason="error")]
    end = tx.one(tx.ended(acts), object)
    assert (end.detail, end.file_index) == (FailureDetail.PROTOCOL, None)


def test_reread_done_without_pending_reread_is_ignored(tx):
    r = tx.Recv(tx.entries(2))
    r.accept()
    full_and_synced(tx, r)
    assert r.call("reread_done", tx.PEER, r.T, 0, [], r.root(0)) == []
    assert r.call("finalized", tx.PEER, r.T, 0, "f0.bin") == []


@pytest.mark.parametrize(
    ("bad", "root"),
    [
        ([1, 1], None),
        ([2, 1], None),
        ([-1], None),
        ([True], None),
        ([1.0], None),
        ({1}, None),
        ("1", None),
        (None, None),
        ([], None),
        ([1], b"\x00" * 32),
        ([], b"\x00" * 31),
        ([], "00" * 32),
    ],
)
@pytest.mark.parametrize("known", [True, False])
def test_reread_done_static_param_errors(tx, bad, root, known):
    r = tx.Recv(tx.entries(3))
    r.accept()
    if known:
        full_and_synced(tx, r)
        r.complete(0)
    T = r.T if known else tx.T3
    before = tx.snapshot(r.e)
    with pytest.raises(ValueError):
        r.e.reread_done(tx.PEER, T, 0, bad, root, r.now + 1)
    assert tx.snapshot(r.e) == before


def test_reread_done_bad_beyond_file_is_error_only_when_matching(tx):
    r = tx.Recv(tx.entries(3))
    r.accept()
    full_and_synced(tx, r)
    r.complete(0)
    before = tx.snapshot(r.e)
    with pytest.raises(ValueError):
        r.e.reread_done(tx.PEER, r.T, 0, [3], None, r.now + 1)
    assert tx.snapshot(r.e) == before
    assert r.call("reread_done", tx.PEER, tx.T3, 0, [3], None) == []
    assert r.call("reread_done", tx.PEER, r.T, 1, [3], None) == []


# ---- AC-12: `complete` bên gửi -------------------------------------------------------------------


@pytest.mark.parametrize("client", [True, False])
def test_check_source_only_after_every_block_read_once(tx, client):
    s = tx.Send(tx.entries(3), client=client)
    s.start()
    s.resume(0)
    did, _ = s.open(0, 1)
    assert tx.of(s.read(did, 0, [0, 1]), CheckSource) == []
    acts = s.read(did, 0, [2])
    cs = tx.one(acts, CheckSource)
    assert (cs.device_id, cs.transfer_id, cs.file_index) == (tx.PEER, s.T, 0)
    assert bytes(cs.leaves) == tx.leaves_of(3, s.salt(0))
    assert tx.sent(acts) == []
    acts = s.check(acts)
    assert tx.sent(acts) == [Complete(transfer_id=s.T, file_index=0, merkle_root=s.root(0).hex())]


def test_short_last_block_counts_exact_bytes(tx):
    files = (FileEntry(name="a", size=2 * BLOCK_SIZE + 10, mtime=5),)
    s = tx.Send(files)
    s.start()
    s.resume(0)
    did, _ = s.open(0, 1)
    s.read(did, 0, [0, 1])
    assert s.e.info(s.key).done_bytes == 2 * BLOCK_SIZE
    s.read(did, 0, [2])
    assert s.e.info(s.key).done_bytes == files[0].size
    assert s.e.info(s.key).total_bytes == files[0].size


@pytest.mark.parametrize("field", ["size", "mtime"])
def test_source_changed_on_check_cancels(tx, field):
    s = tx.Send(tx.entries(2, 1))
    s.start()
    _did, acts = s.serve(0)
    kw = {field: getattr(s.files[0], field) + 1}
    acts = s.check(acts, **kw)
    assert tx.sent(acts) == [Cancel(transfer_id=s.T, reason="source_changed")]
    end = tx.one(tx.ended(acts), object)
    assert (end.outcome, end.reason, end.by_peer, end.file_index) == (
        Outcome.CANCELLED,
        "source_changed",
        False,
        0,
    )


def test_reread_block_with_other_hash_means_source_changed(tx):
    s = tx.Send(tx.entries(3), client=False)
    s.start()
    s.resume(0)
    d1, _ = s.open(0, 1)
    s.read(d1, 0, [0, 1])
    s.call("data_closed", tx.PEER, d1, False)
    s.resume(0, tx.bits(3, [0]))
    d2, acts = s.open(0, 2)
    assert list(tx.one(acts, SendBlocks).have.missing()) == [1, 2]
    acts = s.call("block_read", tx.PEER, d2, 1, tx.digest(1, "nguon-da-doi"))
    assert tx.sent(acts) == [Cancel(transfer_id=s.T, reason="source_changed")]
    assert CloseData(device_id=tx.PEER, data_id=d2) in acts
    assert tx.one(tx.ended(acts), object).file_index == 0


def test_reread_block_with_same_hash_is_fine(tx):
    s = tx.Send(tx.entries(3), client=False)
    s.start()
    s.resume(0)
    d1, _ = s.open(0, 1)
    s.read(d1, 0, [0, 1])
    s.call("data_closed", tx.PEER, d1, False)
    s.resume(0, tx.bits(3, [0]))
    d2, _ = s.open(0, 2)
    acts = s.read(d2, 0, [1, 2])
    assert tx.sent(acts) == []
    assert tx.one(acts, CheckSource).file_index == 0


@pytest.mark.parametrize(
    ("changed", "reason", "detail"),
    [(True, "source_changed", None), (False, "error", FailureDetail.SOURCE_READ)],
)
def test_source_failed_cancels(tx, changed, reason, detail):
    s = tx.Send(tx.entries(2, 2))
    s.start()
    s.resume(0)
    did, _ = s.open(0, 1)
    acts = s.call("source_failed", tx.PEER, s.T, 0, changed)
    assert tx.sent(acts) == [Cancel(transfer_id=s.T, reason=reason)]
    end = tx.one(tx.ended(acts), object)
    assert (end.reason, end.file_index) == (reason, 0)
    if detail is not None:
        assert end.detail == detail
    assert CloseData(device_id=tx.PEER, data_id=did) in acts


def test_full_resume_with_root_resends_complete(tx):
    s = tx.Send(tx.entries(2))
    s.start()
    s.serve_file(0)
    acts = s.resume(0, tx.bits(2, [0, 1]))
    assert tx.sent(acts) == [Complete(transfer_id=s.T, file_index=0, merkle_root=s.root(0).hex())]
    assert tx.of(acts, (OpenData, SendBlocks, CheckSource)) == []


def test_full_resume_while_checking_does_nothing(tx):
    s = tx.Send(tx.entries(2))
    s.start()
    _did, acts = s.serve(0)
    assert tx.one(acts, CheckSource)
    acts = s.resume(0, tx.bits(2, [0, 1]))
    assert tx.sent(acts) == []
    assert tx.of(acts, (OpenData, SendBlocks, CheckSource)) == []


def test_receiver_claiming_unsent_blocks_cancels(tx):
    s = tx.Send(tx.entries(4))
    s.start()
    acts = s.resume(0, tx.bits(4, [2]))
    assert tx.sent(acts) == [Cancel(transfer_id=s.T, reason="error")]
    end = tx.one(tx.ended(acts), object)
    assert (end.detail, end.file_index) == (FailureDetail.PROTOCOL, 0)


def test_resume_beyond_files_cancels(tx):
    s = tx.Send(tx.entries(1))
    s.start()
    acts = s.msg(Resume(transfer_id=s.T, file_index=1, seq=1, bitfield=""))
    assert tx.sent(acts) == [Cancel(transfer_id=s.T, reason="error")]
    assert tx.one(tx.ended(acts), object).detail == FailureDetail.PROTOCOL


@pytest.mark.parametrize("b64", ["", "AAA=", "AQ==", "Hw=="])
def test_resume_with_bitfield_of_wrong_length_or_extra_bits_cancels(tx, b64):
    s = tx.Send(tx.entries(3))  # 3 khối: đúng 1 byte, 5 bit thừa phải = 0 ("AA==" là hợp lệ)
    s.start()
    acts = s.msg(Resume(transfer_id=s.T, file_index=0, seq=1, bitfield=b64))
    assert tx.sent(acts) == [Cancel(transfer_id=s.T, reason="error")]


# ---- AC-13: file rỗng (một engine; mô phỏng hai engine ở test_transfer_sim.py) -----------------------


@pytest.mark.parametrize("client", [False, True])
def test_receiver_empty_file_never_opens_data_connection(tx, client):
    r = tx.Recv(tx.entries(0, 1), client=client)
    acts = r.accept()
    res = tx.one(tx.sent(acts, Resume), object)
    assert (res.file_index, res.bitfield) == (0, "")
    assert tx.of(acts, (OpenData, ReceiveBlocks)) == []
    acts = r.complete(0, root=EMPTY)
    rr = tx.one(acts, Reread)
    assert bytes(rr.leaves) == b""
    acts = r.call("reread_done", tx.PEER, r.T, 0, [], EMPTY)
    fin = tx.one(acts, Finalize)
    acts = r.call("finalized", tx.PEER, r.T, 0, fin.name)
    assert tx.Verified(transfer_id=r.T, file_index=0) in tx.sent(acts)
    assert tx.one(tx.sent(acts, Resume), object).file_index == 1


@pytest.mark.parametrize("client", [False, True])
def test_sender_empty_file_checks_source_without_data_connection(tx, client):
    s = tx.Send(tx.entries(0), client=client)
    s.start()
    acts = s.resume(0, Bitfield(0))
    assert tx.of(acts, (OpenData, SendBlocks)) == []
    cs = tx.one(acts, CheckSource)
    assert bytes(cs.leaves) == b""
    acts = s.check(acts)
    assert tx.sent(acts) == [Complete(transfer_id=s.T, file_index=0, merkle_root=EMPTY.hex())]
    assert merkle_root(b"") == EMPTY


def test_one_block_file_root_is_the_block_hash(tx):
    s = tx.Send(tx.entries(1))
    s.start()
    acts = s.serve_file(0)
    assert tx.sent(acts, Complete) == [Complete(transfer_id=s.T, file_index=0, merkle_root=s.dg(0, 0).hex())]
