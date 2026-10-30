"""`TransferInfo.unsynced_bytes` (spec hackathon PR-1 mục 1 “core/transfer.py”, mục 3 ý 4).

Viết từ spec, không từ code: bên nhận, `unsynced_bytes` = byte các khối của file hiện tại đã phát
`WriteBlock` mà chưa `synced`, về 0 sau `synced`; bên gửi luôn 0. `done_bytes` giữ nghĩa cũ (bên
nhận: chỉ tăng ở `synced`, bậc theo lô `SyncPart`). `done_bytes + unsynced_bytes` ≤ `total_bytes`.
Mô hình trong property test chỉ dựa vào hành động engine trả ra (`WriteBlock`, `SyncPart`), không
đọc trạng thái nội bộ.
"""

from __future__ import annotations

import dataclasses

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import teleport.core.transfer as transfer
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.transfer import SyncPart, WriteBlock

HYPO = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def block_len(size: int, index: int) -> int:
    return min(BLOCK_SIZE, size - index * BLOCK_SIZE)


def info_of(r):
    info = r.e.info(r.key)
    assert info is not None
    listed = [i for i in r.e.transfers() if i.key == r.key]
    assert listed == [info], "`transfers()` và `info()` lệch nhau"
    return info


def test_unsynced_bytes_is_last_field_with_default_zero():
    fields = dataclasses.fields(transfer.TransferInfo)
    assert fields[-1].name == "unsynced_bytes"
    assert fields[-1].default == 0
    assert [f.name for f in fields[:-1]] == [
        "key",
        "status",
        "files",
        "names",
        "file_index",
        "done_bytes",
        "total_bytes",
    ]


def test_receiver_counts_written_blocks_until_synced(tx):
    r = tx.Recv(tx.entries(20))
    r.accept()
    did, _ = r.open()
    assert info_of(r).unsynced_bytes == 0
    acts = r.feed(did, 0, range(5))
    assert len(tx.of(acts, WriteBlock)) == 5
    info = info_of(r)
    assert info.unsynced_bytes == 5 * BLOCK_SIZE
    assert info.done_bytes == 0, "`done_bytes` bên nhận phải chỉ tăng ở `synced`"


def test_unsynced_drops_to_zero_and_done_jumps_after_synced(tx):
    r = tx.Recv(tx.entries(20))
    r.accept()
    did, _ = r.open()
    acts = r.feed(did, 0, range(16))
    assert [[i for i, _ in sp.blocks] for sp in tx.of(acts, SyncPart)] == [list(range(16))]
    # Đã phát `SyncPart` nhưng chưa `synced`: vẫn là byte chưa bền.
    info = info_of(r)
    assert (info.done_bytes, info.unsynced_bytes) == (0, 16 * BLOCK_SIZE)
    r.sync(acts)
    info = info_of(r)
    assert (info.done_bytes, info.unsynced_bytes) == (16 * BLOCK_SIZE, 0)


def test_unsynced_keeps_only_blocks_after_the_synced_batch(tx):
    r = tx.Recv(tx.entries(30))
    r.accept()
    did, _ = r.open()
    acts = r.feed(did, 0, range(20))
    r.sync(acts)  # lô 0..15
    info = info_of(r)
    assert (info.done_bytes, info.unsynced_bytes) == (16 * BLOCK_SIZE, 4 * BLOCK_SIZE)


def test_unsynced_counts_short_last_block_by_its_real_length(tx):
    last = 1234
    files = tx.entries(3, last=last)
    r = tx.Recv(files)
    r.accept()
    did, _ = r.open()
    acts = r.feed(did, 0, [0, 1])
    assert info_of(r).unsynced_bytes == 2 * BLOCK_SIZE
    acts += r.feed(did, 0, [2])  # khối cuối, ngắn
    assert len(tx.of(acts, WriteBlock)) == 3
    info = info_of(r)
    assert info.unsynced_bytes == files[0].size == 2 * BLOCK_SIZE + last
    assert info.done_bytes + info.unsynced_bytes <= info.total_bytes
    r.sync_all(acts)
    info = info_of(r)
    assert (info.done_bytes, info.unsynced_bytes) == (files[0].size, 0)


def test_duplicate_block_is_not_counted_twice(tx):
    r = tx.Recv(tx.entries(10))
    r.accept()
    d1, _ = r.open()
    r.feed(d1, 0, [0, 1, 2])
    d2, _ = r.open()  # `data_open` trùng cùng `seq`
    acts = r.feed(d2, 0, [0, 1, 2])
    assert tx.of(acts, WriteBlock) == []
    assert info_of(r).unsynced_bytes == 3 * BLOCK_SIZE


def test_unsynced_is_zero_after_early_sync_on_data_closed(tx):
    r = tx.Recv(tx.entries(10))
    r.accept()
    did, _ = r.open()
    r.feed(did, 0, range(5))
    acts = r.call("data_closed", tx.PEER, did, False)
    assert info_of(r).unsynced_bytes == 5 * BLOCK_SIZE  # `SyncPart` sớm chưa xong
    r.sync(acts)
    info = info_of(r)
    assert (info.done_bytes, info.unsynced_bytes) == (5 * BLOCK_SIZE, 0)


def test_second_file_counts_only_its_own_unsynced_blocks(tx):
    files = tx.entries(3, 6)
    r = tx.Recv(files)
    r.accept()
    r.finish_file(0)
    info = info_of(r)
    assert (info.file_index, info.done_bytes, info.unsynced_bytes) == (1, files[0].size, 0)
    did, _ = r.open()
    r.feed(did, 1, range(4))
    info = info_of(r)
    assert info.done_bytes == files[0].size
    assert info.unsynced_bytes == 4 * BLOCK_SIZE


def test_ended_info_and_done_receiver_has_no_unsynced(tx):
    files = tx.entries(2, 3)
    r = tx.Recv(files)
    r.accept()
    acts = r.finish_all()
    ends = tx.ended(acts)
    assert len(ends) == 1
    assert ends[0].info.unsynced_bytes == 0
    assert ends[0].info.done_bytes == ends[0].info.total_bytes


def test_sender_unsynced_is_always_zero(tx):
    s = tx.Send(tx.entries(20, 3))
    s.start()
    assert s.e.info(s.key).unsynced_bytes == 0
    did, acts = s.serve(0)
    info = s.e.info(s.key)
    assert info.done_bytes > 0, "bên gửi phải đếm byte đã gửi"
    assert info.unsynced_bytes == 0
    acts += s.check(acts)
    s.call("data_closed", s.peer, did, True)
    for info in s.e.transfers():
        assert info.unsynced_bytes == 0
    for end in tx.ended(s.all_actions()):
        assert end.info.unsynced_bytes == 0


@st.composite
def receive_script(draw):
    nblocks = draw(st.integers(1, 40))
    last = draw(st.sampled_from([1, 777, BLOCK_SIZE]))
    # Khối tới theo thứ tự tăng (protocol §4.2); xen vài khối trùng đã nhận.
    feed = list(range(nblocks))
    for _ in range(draw(st.integers(0, 4))):
        pos = draw(st.integers(1, len(feed)))
        feed.insert(pos, draw(st.integers(0, feed[pos - 1])))
    # Sau mỗi khối: bao nhiêu lô `SyncPart` đang chờ thì báo `synced` (theo thứ tự FIFO).
    syncs = draw(st.lists(st.integers(0, 2), min_size=len(feed), max_size=len(feed)))
    return nblocks, last, feed, syncs


@HYPO
@given(script=receive_script())
def test_unsynced_matches_written_minus_synced_model(tx, script):
    """Mô hình: `unsynced` = tổng độ dài các khối đã có `WriteBlock` mà chưa nằm trong lô `SyncPart`
    đã được `synced`; `done` = tổng độ dài khối trong các lô đã `synced`; luôn `done + unsynced ≤ total`."""
    old = transfer.SYNC_EVERY
    transfer.SYNC_EVERY = 4  # lô nhỏ để đi qua nhiều lô; khôi phục ở `finally`
    try:
        _run_model(tx, *script)
    finally:
        transfer.SYNC_EVERY = old


def _run_model(tx, nblocks, last, feed, syncs) -> None:
    files = tx.entries(nblocks, last=last)
    size = files[0].size
    r = tx.Recv(files)
    r.accept()
    did, _ = r.open()
    written: set[int] = set()
    synced: set[int] = set()
    pending: list[SyncPart] = []

    def take(acts) -> None:
        for a in acts:
            if isinstance(a, WriteBlock):
                assert a.index not in written, "ghi lại một khối đã ghi"
                written.add(a.index)
            elif isinstance(a, SyncPart):
                pending.append(a)

    def check() -> None:
        info = r.e.info(r.key)
        if info is None or info.file_index > 0:
            return
        exp_unsynced = sum(block_len(size, i) for i in written - synced)
        exp_done = sum(block_len(size, i) for i in synced)
        assert info.unsynced_bytes == exp_unsynced
        assert info.done_bytes == exp_done
        assert 0 <= info.done_bytes + info.unsynced_bytes <= info.total_bytes

    for idx, n_sync in zip(feed, syncs, strict=True):
        take(r.call("block_received", tx.PEER, did, idx, r.dg(0, idx)))
        check()
        for _ in range(min(n_sync, len(pending))):
            sp = pending.pop(0)
            take(r.call("synced", tx.PEER, sp.transfer_id, sp.file_index, sp.token))
            synced.update(i for i, _ in sp.blocks)
            check()
    while pending:
        sp = pending.pop(0)
        take(r.call("synced", tx.PEER, sp.transfer_id, sp.file_index, sp.token))
        synced.update(i for i, _ in sp.blocks)
        check()
    info = r.e.info(r.key)
    assert info is not None
    assert info.unsynced_bytes == 0
