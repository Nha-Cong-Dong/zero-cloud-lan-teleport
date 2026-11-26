"""Kiểm thử sans-IO cho Smart Delta Teleport (`teleport.core.delta`, `core.messages`, `core.transfer`)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from teleport.core.blocks import BLOCK_SIZE
from teleport.core.delta import (
    DELTA_INDEX_VERSION,
    MAX_DELTA_INDEX_ENTRIES,
    DeltaIndexEntry,
    decode_delta_index,
    delta_lookup_key,
    encode_delta_index,
    remaining_batches,
    sample_block_indices,
)
from teleport.core.messages import Accept, DeltaMatch, DeltaProbe, FileEntry, Offer, Resume, to_dict
from teleport.core.transfer import (
    AskUser,
    CheckDelta,
    CheckSpace,
    CreateTemp,
    DeltaMatched,
    PrepareDelta,
    SendMessage,
    TransferEngine,
)

DEV_S = "a" * 32
DEV_R = "b" * 32
TID = "1" * 32


def test_sample_block_indices_and_remaining_batches() -> None:
    assert sample_block_indices(0) == ()
    assert sample_block_indices(1) == (0,)
    assert sample_block_indices(4) == (0, 1, 2, 3)
    assert sample_block_indices(10) == (0, 3, 6, 9)
    assert sample_block_indices(12) == (0, 4, 8, 11)

    batches = remaining_batches(10, {0, 3, 6, 9}, batch_size=3)
    assert batches == [(1, 2, 4), (5, 7, 8)]


def test_delta_index_codec_bounds_and_lookup_key(tmp_path: Path) -> None:
    assert delta_lookup_key("file.bin", "docs\\sub\\file.bin") == "docs/sub/file.bin"
    assert delta_lookup_key("photo.png") == "photo.png"

    entries = [
        DeltaIndexEntry(
            key=f"f{i}.bin",
            path=str(tmp_path / f"f{i}.bin"),
            size=1024 + i,
            mtime_ns=1_000 + i,
            merkle_root="ab" * 32 if i % 2 == 0 else "",
        )
        for i in range(MAX_DELTA_INDEX_ENTRIES + 5)
    ]
    raw = encode_delta_index(entries)
    decoded = decode_delta_index(raw)
    assert len(decoded) == MAX_DELTA_INDEX_ENTRIES
    assert decoded[0].key == "f5.bin"
    assert decoded[-1].key == f"f{MAX_DELTA_INDEX_ENTRIES + 4}.bin"
    assert DELTA_INDEX_VERSION == 1

    # Bỏ mục hỏng hoặc JSON không hợp lệ mà không raise
    assert decode_delta_index(b"not-json") == []
    bad_json = b'{"version": 1, "entries": [{"key": "", "path": "x", "size": 1, "mtime_ns": 1}]}'
    assert decode_delta_index(bad_json) == []


def test_offer_backward_compat_serialization() -> None:
    entry = FileEntry("data.bin", 2 * BLOCK_SIZE, 1000)
    plain_offer = Offer(TID, BLOCK_SIZE, (entry,), delta=False)
    assert "delta" not in to_dict(plain_offer)

    delta_offer = Offer(TID, BLOCK_SIZE, (entry,), delta=True)
    assert to_dict(delta_offer)["delta"] is True


def test_transfer_engine_delta_level1_zero_data_and_level2_partial() -> None:
    size = 4 * BLOCK_SIZE
    h0 = hashlib.sha256(b"B0").hexdigest()
    h1 = hashlib.sha256(b"B1").hexdigest()
    h2 = hashlib.sha256(b"B2").hexdigest()
    h3 = hashlib.sha256(b"B3").hexdigest()
    entry = FileEntry("archive.bin", size, 1700000000000)

    # Mức 1: cả 4 khối đều khớp -> delta_reused_bytes == size và resume mang đủ bit
    sender = TransferEngine(DEV_S, receiving=False, delta_enabled=True)
    receiver = TransferEngine(DEV_R, receiving=True, delta_enabled=True)
    sender.control_up(DEV_R, 1, True, 1.0)
    receiver.control_up(DEV_S, 1, False, 1.0)

    s_acts = sender.send(DEV_R, TID, [entry], 2.0, delta=True)
    offer = next(a.message for a in s_acts if isinstance(a, SendMessage))
    assert isinstance(offer, Offer) and offer.delta is True

    r_acts = receiver.message(DEV_S, 1, offer, 2.0)
    assert any(isinstance(a, CheckSpace) for a in r_acts)
    r_acts = receiver.space_checked(DEV_S, TID, 10 * size, 2**40, 2.1)
    assert any(isinstance(a, AskUser) for a in r_acts)
    r_acts = receiver.decide(DEV_S, TID, True, 2.15)
    assert any(isinstance(a, CreateTemp) for a in r_acts)
    assert any(isinstance(a, PrepareDelta) for a in r_acts)
    r_acts = receiver.temp_created(DEV_S, TID, 2.2)
    assert any(isinstance(a, SendMessage) and isinstance(a.message, Accept) for a in r_acts)

    # Bên nhận gửi delta_probe cho cả 4 khối
    r_acts = receiver.send_delta_probe(DEV_S, TID, 0, 1, [(0, h0), (1, h1), (2, h2), (3, h3)], 2.3)
    probe = next(
        a.message for a in r_acts if isinstance(a, SendMessage) and isinstance(a.message, DeltaProbe)
    )
    s_acts = sender.message(DEV_R, 1, probe, 2.4)
    check = next(a for a in s_acts if isinstance(a, CheckDelta))

    matched = [(i, bytes.fromhex(h)) for i, h in ((0, h0), (1, h1), (2, h2), (3, h3))]
    s_acts = sender.delta_checked(DEV_R, TID, 0, check.seq, matched, 2.5)
    match_msg = next(
        a.message for a in s_acts if isinstance(a, SendMessage) and isinstance(a.message, DeltaMatch)
    )
    assert match_msg.matched == (0, 1, 2, 3)
    r_acts = receiver.message(DEV_S, 1, match_msg, 2.6)
    dm = next(a for a in r_acts if isinstance(a, DeltaMatched))
    assert dm.matched == (0, 1, 2, 3)

    r_acts = receiver.delta_done(DEV_S, TID, 0, matched, 4, 2.7)
    assert receiver.transfers()[0].delta_reused_bytes == size
    resume = next(a.message for a in r_acts if isinstance(a, SendMessage) and isinstance(a.message, Resume))
    assert resume.file_index == 0
    assert resume.bitfield == "8A=="  # 0b1111_0000 = 0xF0


def test_transfer_engine_delta_partial_match_sets_reused_bytes() -> None:
    size = 3 * BLOCK_SIZE
    h0 = hashlib.sha256(b"same0").hexdigest()
    h2 = hashlib.sha256(b"same2").hexdigest()
    entry = FileEntry("partial.bin", size, 1700000000000)

    sender = TransferEngine(DEV_S, receiving=False, delta_enabled=True)
    receiver = TransferEngine(DEV_R, receiving=True, delta_enabled=True)
    sender.control_up(DEV_R, 1, True, 1.0)
    receiver.control_up(DEV_S, 1, False, 1.0)

    s_acts = sender.send(DEV_R, TID, [entry], 2.0)
    offer = next(a.message for a in s_acts if isinstance(a, SendMessage))
    receiver.message(DEV_S, 1, offer, 2.0)
    receiver.space_checked(DEV_S, TID, 10 * size, 2**40, 2.1)
    receiver.decide(DEV_S, TID, True, 2.15)
    receiver.temp_created(DEV_S, TID, 2.2)

    r_acts = receiver.send_delta_probe(
        DEV_S,
        TID,
        0,
        1,
        [(0, h0), (1, hashlib.sha256(b"old1").hexdigest()), (2, h2)],
        2.3,
    )
    probe = next(
        a.message for a in r_acts if isinstance(a, SendMessage) and isinstance(a.message, DeltaProbe)
    )
    s_acts = sender.message(DEV_R, 1, probe, 2.4)
    check = next(a for a in s_acts if isinstance(a, CheckDelta))

    # Chỉ khối 0 và 2 khớp -> 2 * BLOCK_SIZE được tái sử dụng
    matched = [(0, bytes.fromhex(h0)), (2, bytes.fromhex(h2))]
    s_acts = sender.delta_checked(DEV_R, TID, 0, check.seq, matched, 2.5)
    match_msg = next(
        a.message for a in s_acts if isinstance(a, SendMessage) and isinstance(a.message, DeltaMatch)
    )
    assert match_msg.matched == (0, 2)

    receiver.message(DEV_S, 1, match_msg, 2.6)
    r_acts = receiver.delta_done(DEV_S, TID, 0, matched, 3, 2.7)
    assert receiver.transfers()[0].delta_reused_bytes == 2 * BLOCK_SIZE
    resume = next(
        (a.message for a in r_acts if isinstance(a, SendMessage) and isinstance(a.message, Resume)),
        None,
    )
    assert resume is not None and resume.bitfield == "oA=="  # 0b1010_0000 = 0xA0
