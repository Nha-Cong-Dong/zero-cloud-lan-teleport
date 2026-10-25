"""Hiệu năng trên đường đi của từng khối (spec P2-4 mục 3.13; AC-24, mvp N2).

Trên `block_received`, `synced`, `block_read` engine chỉ được dùng bộ đếm O(1), không quét bitfield
hay `leaves` O(n). Cách kiểm: cùng một chuỗi 2000 khối, file 2²⁰ khối (`leaves` 32 MiB) không được
chậm hơn 10 lần file nhỏ, cộng 0,2 s. Đây là phép đo thời gian thật duy nhất của bộ test, đo một
lần, biên rộng.

Spec ghi file nhỏ n = 2¹⁰, nhưng 2¹⁰ = 1024 < 2000 khối của chuỗi: file nhỏ ở đây là 2¹¹ (lũy thừa
2 nhỏ nhất chứa đủ chuỗi). Tỉ lệ 2²⁰ / 2¹¹ = 512 vẫn làm lộ mọi chi phí O(n) mỗi khối.
"""

from __future__ import annotations

import time

from teleport.core.blocks import BLOCK_SIZE
from teleport.core.messages import Accept, DataOpen, FileEntry, Offer, Resume
from teleport.core.transfer import SendBlocks, SendMessage, SyncPart, TransferEngine

ME = "e" * 32
PEER = "a" * 32
T = "1" * 32
CHAIN = 2000
BIG = 1 << 20
SMALL = 1 << 11


def _digests() -> list[bytes]:
    import hashlib

    return [hashlib.sha256(i.to_bytes(4, "big")).digest() for i in range(CHAIN)]


def receiver_chain_seconds(nblocks: int, digests: list[bytes]) -> float:
    e = TransferEngine(ME, receiving=True)
    files = (FileEntry(name="lon.bin", size=nblocks * BLOCK_SIZE, mtime=1),)
    e.control_up(PEER, 1, False, 1.0)
    e.message(PEER, 1, Offer(transfer_id=T, block_size=BLOCK_SIZE, files=files), 1.0)
    e.space_checked(PEER, T, None, None, 1.0)
    acts = e.decide(PEER, T, True, 1.0)
    seq = next(a.message.seq for a in acts if isinstance(a, SendMessage) and isinstance(a.message, Resume))
    e.incoming_data(PEER, 100, DataOpen(transfer_id=T, file_index=0, resume_seq=seq), 1.0)
    now = 2.0
    start = time.perf_counter()
    for i in range(CHAIN):
        acts = e.block_received(PEER, 100, i, digests[i], now)
        for a in acts:
            if isinstance(a, SyncPart):
                e.synced(PEER, T, 0, a.token, now)
    elapsed = time.perf_counter() - start
    assert e.info(next(iter(e.transfers())).key).done_bytes == CHAIN * BLOCK_SIZE
    return elapsed


def sender_chain_seconds(nblocks: int, digests: list[bytes]) -> float:
    e = TransferEngine(ME, receiving=False)
    files = [FileEntry(name="lon.bin", size=nblocks * BLOCK_SIZE, mtime=1)]
    e.control_up(PEER, 1, False, 1.0)
    e.send(PEER, T, files, 1.0)
    e.message(PEER, 1, Accept(transfer_id=T), 1.0)
    from teleport.core.bitfield import Bitfield

    e.message(
        PEER, 1, Resume(transfer_id=T, file_index=0, seq=1, bitfield=Bitfield(nblocks).to_base64()), 1.0
    )
    acts = e.incoming_data(PEER, 100, DataOpen(transfer_id=T, file_index=0, resume_seq=1), 1.0)
    assert any(isinstance(a, SendBlocks) for a in acts)
    now = 2.0
    start = time.perf_counter()
    for i in range(CHAIN):
        e.block_read(PEER, 100, i, digests[i], now)
    elapsed = time.perf_counter() - start
    assert e.info(next(iter(e.transfers())).key).done_bytes == CHAIN * BLOCK_SIZE
    return elapsed


def test_receiver_per_block_cost_does_not_grow_with_file_size():
    digests = _digests()
    small = receiver_chain_seconds(SMALL, digests)
    big = receiver_chain_seconds(BIG, digests)
    assert big <= 10 * small + 0.2, (big, small)


def test_sender_per_block_cost_does_not_grow_with_file_size():
    digests = _digests()
    small = sender_chain_seconds(SMALL, digests)
    big = sender_chain_seconds(BIG, digests)
    assert big <= 10 * small + 0.2, (big, small)
