"""Kiểm tham số, fuzz engine, hằng số khớp tài liệu, core không I/O.

Spec P2-4 mục 2.2, 2.7, 2.10; AC-17, AC-18. Viết từ spec và docs/protocol.md, không từ code. Mọi
lời gọi lỗi phải ném đúng loại lỗi và không đổi trạng thái quan sát được, kể cả mốc `now`.
"""

from __future__ import annotations

import ast
import itertools
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import teleport.core.keepalive as keepalive
import teleport.core.ratelimit as ratelimit
import teleport.core.transfer as transfer
from teleport.core.bitfield import Bitfield
from teleport.core.blocks import BLOCK_SIZE, MAX_BLOCKS, MAX_FILE_SIZE, block_count
from teleport.core.merkle import merkle_root
from teleport.core.messages import (
    MAX_OFFER_FILES,
    MAX_SAFE_INTEGER,
    Accept,
    Cancel,
    Complete,
    DataOpen,
    FileEntry,
    Hello,
    MessageError,
    Offer,
    Ping,
    Reject,
    Resume,
    Verified,
)
from teleport.core.transfer import (
    CheckSource,
    CloseData,
    Direction,
    DiskOp,
    Finalize,
    OpenData,
    OpenPart,
    ReceiveBlocks,
    Report,
    Reread,
    SendBlocks,
    SendMessage,
    SyncPart,
    TransferEnded,
    TransferEngine,
    TransferIdInUse,
    TransferKey,
    TransferStatus,
    WriteBlock,
)

ROOT = Path(__file__).resolve().parents[2]

# Giống tests/core/conftest.py (kiểm ở test_constants_match_conftest).
ME = "e" * 32
PEER = "a" * 32
OTHER = "c" * 32
T1, T2, T3 = (f"{n:032x}" for n in (1, 2, 3))
BAD_DEV = "A" * 32
BAD_T = "z" * 32
DG = b"\x01" * 32
NOW = 20.0  # lời gọi lỗi chạy lúc 20; lời gọi thành công trước đó lúc 10


def _files(*nblocks: int) -> list[FileEntry]:
    return [
        FileEntry(name=f"f{i}", size=(n - 1) * BLOCK_SIZE + 5 if n else 0, mtime=100 + i)
        for i, n in enumerate(nblocks)
    ]


def test_constants_match_conftest(tx):
    assert (tx.ME, tx.PEER, tx.OTHER, tx.T1, tx.T2, tx.T3) == (ME, PEER, OTHER, T1, T2, T3)


def rich_engine():
    """Engine có trạng thái thật: lượt nhận đang chạy từ PEER (kết nối dữ liệu `did`, một khối chờ
    fsync), lượt gửi tới OTHER với một kết nối chờ `resume` (`pend`, hạn 19,5). Lời gọi thành công
    cuối cùng lúc `now` = 10.
    """
    e = TransferEngine(ME, receiving=True)
    t = itertools.count(1)

    def now() -> float:
        return 1.0 + next(t) * 0.01

    e.control_up(PEER, 1, False, now())
    e.message(PEER, 1, Offer(transfer_id=T1, block_size=BLOCK_SIZE, files=tuple(_files(3))), now())
    e.space_checked(PEER, T1, 10**15, None, now())
    acts = e.decide(PEER, T1, True, now())
    acts += e.temp_created(PEER, T1, now())  # P3-2: `accept`/`resume` chỉ sau `temp_created`
    res = next(a.message for a in acts if isinstance(a, SendMessage) and isinstance(a.message, Resume))
    did = 100
    acts = e.incoming_data(PEER, did, DataOpen(transfer_id=T1, file_index=0, resume_seq=res.seq), now())
    assert any(isinstance(a, ReceiveBlocks) for a in acts)
    e.block_received(PEER, did, 0, DG, now())
    e.control_up(OTHER, 2, False, now())
    e.send(OTHER, T2, _files(2), now())
    pend = 200
    assert e.incoming_data(OTHER, pend, DataOpen(transfer_id=T2, file_index=0, resume_seq=5), 9.5) == []
    e.tick(10.0)
    return e, SimpleNamespace(did=did, pend=pend)


def _case(name: str, fn, exc):
    return pytest.param(fn, exc, id=name)


_DO = DataOpen(transfer_id=T1, file_index=0, resume_seq=1)
_KEY = TransferKey(PEER, Direction.RECEIVE, T1)

CASES = [
    # device_id sai
    _case("control_up-dev", lambda e, k: e.control_up(BAD_DEV, 9, False, NOW), MessageError),
    _case("control_up-dev-int", lambda e, k: e.control_up(123, 9, False, NOW), MessageError),
    _case("control_lost-dev", lambda e, k: e.control_lost(BAD_DEV, 1, NOW), MessageError),
    _case("message-dev", lambda e, k: e.message(BAD_DEV, 1, Accept(transfer_id=T1), NOW), MessageError),
    _case("send-dev", lambda e, k: e.send(BAD_DEV, T3, _files(1), NOW), MessageError),
    _case("space_checked-dev", lambda e, k: e.space_checked(BAD_DEV, T1, None, None, NOW), MessageError),
    _case("decide-dev", lambda e, k: e.decide(BAD_DEV, T1, True, NOW), MessageError),
    _case("incoming_data-dev", lambda e, k: e.incoming_data(BAD_DEV, 900, _DO, NOW), MessageError),
    _case("outgoing_data-dev", lambda e, k: e.outgoing_data(BAD_DEV, 1, 900, _DO, NOW), MessageError),
    _case("outgoing_failed-dev", lambda e, k: e.outgoing_data_failed(BAD_DEV, 1, _DO, NOW), MessageError),
    _case("data_closed-dev", lambda e, k: e.data_closed(BAD_DEV, k.did, False, NOW), MessageError),
    _case("block_read-dev", lambda e, k: e.block_read(BAD_DEV, k.did, 0, DG, NOW), MessageError),
    _case("block_received-dev", lambda e, k: e.block_received(BAD_DEV, k.did, 1, DG, NOW), MessageError),
    _case("block_bad-dev", lambda e, k: e.block_bad(BAD_DEV, k.did, NOW), MessageError),
    _case("synced-dev", lambda e, k: e.synced(BAD_DEV, T1, 0, 1, NOW), MessageError),
    _case("reread_done-dev", lambda e, k: e.reread_done(BAD_DEV, T1, 0, [], DG, NOW), MessageError),
    _case("finalized-dev", lambda e, k: e.finalized(BAD_DEV, T1, 0, "x", NOW), MessageError),
    _case("storage_failed-dev", lambda e, k: e.storage_failed(BAD_DEV, T1, DiskOp.WRITE, NOW), MessageError),
    _case("source_checked-dev", lambda e, k: e.source_checked(BAD_DEV, T2, 0, 1, 1, DG, NOW), MessageError),
    _case("source_failed-dev", lambda e, k: e.source_failed(BAD_DEV, T2, 0, False, NOW), MessageError),
    # transfer_id sai
    _case("send-tid", lambda e, k: e.send(PEER, BAD_T, _files(1), NOW), MessageError),
    _case(
        "send-tid-upper",
        lambda e, k: e.send(PEER, T3.upper().replace("0", "A"), _files(1), NOW),
        MessageError,
    ),
    _case("space_checked-tid", lambda e, k: e.space_checked(PEER, BAD_T, None, None, NOW), MessageError),
    _case("decide-tid", lambda e, k: e.decide(PEER, BAD_T, True, NOW), MessageError),
    _case("synced-tid", lambda e, k: e.synced(PEER, BAD_T, 0, 1, NOW), MessageError),
    _case("reread_done-tid", lambda e, k: e.reread_done(PEER, BAD_T, 0, [], DG, NOW), MessageError),
    _case("finalized-tid", lambda e, k: e.finalized(PEER, BAD_T, 0, "x", NOW), MessageError),
    _case("storage_failed-tid", lambda e, k: e.storage_failed(PEER, BAD_T, DiskOp.SYNC, NOW), MessageError),
    _case("source_checked-tid", lambda e, k: e.source_checked(OTHER, BAD_T, 0, 1, 1, DG, NOW), MessageError),
    _case("source_failed-tid", lambda e, k: e.source_failed(OTHER, BAD_T, 0, True, NOW), MessageError),
    # control_id / data_id / token không phải int ≥ 1
    *[
        _case(f"{name}-{v!r}", fn(v), ValueError)
        for v in (0, -1, True, 1.0, "1", None)
        for name, fn in (
            ("control_up-cid", lambda v: lambda e, k: e.control_up(PEER, v, False, NOW)),
            ("control_lost-cid", lambda v: lambda e, k: e.control_lost(PEER, v, NOW)),
            ("message-cid", lambda v: lambda e, k: e.message(PEER, v, Accept(transfer_id=T1), NOW)),
            ("outgoing_data-cid", lambda v: lambda e, k: e.outgoing_data(PEER, v, 900, _DO, NOW)),
            ("outgoing_failed-cid", lambda v: lambda e, k: e.outgoing_data_failed(PEER, v, _DO, NOW)),
            ("incoming_data-did", lambda v: lambda e, k: e.incoming_data(PEER, v, _DO, NOW)),
            ("outgoing_data-did", lambda v: lambda e, k: e.outgoing_data(PEER, 1, v, _DO, NOW)),
            ("data_closed-did", lambda v: lambda e, k: e.data_closed(PEER, v, False, NOW)),
            ("block_read-did", lambda v: lambda e, k: e.block_read(PEER, v, 0, DG, NOW)),
            ("block_received-did", lambda v: lambda e, k: e.block_received(PEER, v, 0, DG, NOW)),
            ("block_bad-did", lambda v: lambda e, k: e.block_bad(PEER, v, NOW)),
            ("synced-token", lambda v: lambda e, k: e.synced(PEER, T1, 0, v, NOW)),
        )
    ],
    # now sai
    *[
        _case(f"{name}-now-{v!r}", fn(v), exc)
        for v, exc in (
            ("20", TypeError),
            (None, TypeError),
            (math.nan, ValueError),
            (math.inf, ValueError),
            (5.0, ValueError),
        )
        for name, fn in (
            ("tick", lambda v: lambda e, k: e.tick(v)),
            ("control_up", lambda v: lambda e, k: e.control_up(PEER, 9, False, v)),
            ("message", lambda v: lambda e, k: e.message(PEER, 1, Accept(transfer_id=T1), v)),
            ("block_received", lambda v: lambda e, k: e.block_received(PEER, k.did, 1, DG, v)),
            ("synced", lambda v: lambda e, k: e.synced(PEER, T1, 0, 1, v)),
            ("send", lambda v: lambda e, k: e.send(PEER, T3, _files(1), v)),
            ("cancel", lambda v: lambda e, k: e.cancel(_KEY, v)),
            ("incoming_data", lambda v: lambda e, k: e.incoming_data(PEER, 901, _DO, v)),
        )
    ],
    # msg sai kiểu
    _case(
        "message-hello",
        lambda e, k: e.message(
            PEER, 1, Hello(protocol=1, device_id=PEER, name="x", app_version="1", os="linux"), NOW
        ),
        TypeError,
    ),
    _case("message-ping", lambda e, k: e.message(PEER, 1, Ping(t=1), NOW), TypeError),
    _case("message-data_open", lambda e, k: e.message(PEER, 1, _DO, NOW), TypeError),
    _case(
        "message-dict", lambda e, k: e.message(PEER, 1, {"type": "accept", "transfer_id": T1}, NOW), TypeError
    ),
    _case("message-none", lambda e, k: e.message(PEER, 1, None, NOW), TypeError),
    _case(
        "incoming_data-resume",
        lambda e, k: e.incoming_data(
            PEER, 900, Resume(transfer_id=T1, file_index=0, seq=1, bitfield=""), NOW
        ),
        TypeError,
    ),
    _case(
        "outgoing_data-accept",
        lambda e, k: e.outgoing_data(PEER, 1, 900, Accept(transfer_id=T1), NOW),
        TypeError,
    ),
    _case(
        "outgoing_failed-offer",
        lambda e, k: e.outgoing_data_failed(
            PEER, 1, Offer(transfer_id=T1, block_size=BLOCK_SIZE, files=tuple(_files(1))), NOW
        ),
        TypeError,
    ),
    # bool
    _case("control_up-client-int", lambda e, k: e.control_up(PEER, 9, 1, NOW), TypeError),
    _case("control_up-client-none", lambda e, k: e.control_up(PEER, 9, None, NOW), TypeError),
    _case("decide-accept-int", lambda e, k: e.decide(PEER, T1, 1, NOW), TypeError),
    _case("decide-accept-str", lambda e, k: e.decide(PEER, T1, "yes", NOW), TypeError),
    _case("data_closed-finished", lambda e, k: e.data_closed(PEER, k.did, 0, NOW), TypeError),
    _case("source_failed-changed", lambda e, k: e.source_failed(OTHER, T2, 0, 1, NOW), TypeError),
    # op
    _case("storage_failed-op-str", lambda e, k: e.storage_failed(PEER, T1, "bogus", NOW), ValueError),
    _case("storage_failed-op-int", lambda e, k: e.storage_failed(PEER, T1, 7, NOW), ValueError),
    _case("storage_failed-op-none", lambda e, k: e.storage_failed(PEER, T1, None, NOW), ValueError),
    # digest / root
    _case("block_read-digest-31", lambda e, k: e.block_read(PEER, k.did, 0, b"x" * 31, NOW), ValueError),
    _case(
        "block_received-digest-33", lambda e, k: e.block_received(PEER, k.did, 1, b"x" * 33, NOW), ValueError
    ),
    _case(
        "block_read-digest-str-unknown", lambda e, k: e.block_read(PEER, 999, 0, "a" * 32, NOW), ValueError
    ),
    _case(
        "block_received-digest-none-unknown",
        lambda e, k: e.block_received(PEER, 999, 0, None, NOW),
        ValueError,
    ),
    _case(
        "source_checked-root-31",
        lambda e, k: e.source_checked(OTHER, T2, 0, 1, 1, b"x" * 31, NOW),
        ValueError,
    ),
    _case(
        "source_checked-root-none", lambda e, k: e.source_checked(OTHER, T2, 0, 1, 1, None, NOW), ValueError
    ),
    _case("reread_done-root-31", lambda e, k: e.reread_done(PEER, T1, 0, [], b"x" * 31, NOW), ValueError),
    # index
    _case("block_read-index-neg", lambda e, k: e.block_read(PEER, k.did, -1, DG, NOW), ValueError),
    _case("block_read-index-max", lambda e, k: e.block_read(PEER, 999, MAX_BLOCKS, DG, NOW), ValueError),
    _case("block_received-index-bool", lambda e, k: e.block_received(PEER, k.did, True, DG, NOW), ValueError),
    _case("block_received-index-float", lambda e, k: e.block_received(PEER, k.did, 1.0, DG, NOW), ValueError),
    _case(
        "block_received-index-max",
        lambda e, k: e.block_received(PEER, k.did, MAX_BLOCKS, DG, NOW),
        ValueError,
    ),
    # file_index
    _case("synced-fi-neg", lambda e, k: e.synced(PEER, T1, -1, 1, NOW), ValueError),
    _case("synced-fi-max", lambda e, k: e.synced(PEER, T1, MAX_OFFER_FILES, 1, NOW), ValueError),
    _case("reread_done-fi-bool", lambda e, k: e.reread_done(PEER, T1, True, [], DG, NOW), ValueError),
    _case("finalized-fi-neg", lambda e, k: e.finalized(PEER, T1, -1, "x", NOW), ValueError),
    _case(
        "source_checked-fi-max",
        lambda e, k: e.source_checked(OTHER, T2, MAX_OFFER_FILES, 1, 1, DG, NOW),
        ValueError,
    ),
    _case("source_failed-fi-neg", lambda e, k: e.source_failed(OTHER, T2, -1, True, NOW), ValueError),
    # size / mtime
    _case("source_checked-size-neg", lambda e, k: e.source_checked(OTHER, T2, 0, -1, 1, DG, NOW), ValueError),
    _case(
        "source_checked-size-max",
        lambda e, k: e.source_checked(OTHER, T2, 0, MAX_FILE_SIZE + 1, 1, DG, NOW),
        ValueError,
    ),
    _case(
        "source_checked-size-bool", lambda e, k: e.source_checked(OTHER, T2, 0, True, 1, DG, NOW), ValueError
    ),
    _case(
        "source_checked-mtime-neg", lambda e, k: e.source_checked(OTHER, T2, 0, 1, -1, DG, NOW), ValueError
    ),
    _case(
        "source_checked-mtime-max",
        lambda e, k: e.source_checked(OTHER, T2, 0, 1, MAX_SAFE_INTEGER + 1, DG, NOW),
        ValueError,
    ),
    # free_bytes / max_file_size
    _case("space_checked-free-neg", lambda e, k: e.space_checked(PEER, T1, -1, None, NOW), ValueError),
    _case("space_checked-free-bool", lambda e, k: e.space_checked(PEER, T1, True, None, NOW), ValueError),
    _case("space_checked-free-float", lambda e, k: e.space_checked(PEER, T1, 1.5, None, NOW), ValueError),
    _case("space_checked-max-neg", lambda e, k: e.space_checked(PEER, T1, None, -1, NOW), ValueError),
    _case("space_checked-max-bool", lambda e, k: e.space_checked(PEER, T1, None, False, NOW), ValueError),
    # name
    _case("finalized-name-int", lambda e, k: e.finalized(PEER, T1, 0, 5, NOW), TypeError),
    _case("finalized-name-none", lambda e, k: e.finalized(PEER, T1, 0, None, NOW), TypeError),
    _case("finalized-name-empty", lambda e, k: e.finalized(PEER, T1, 0, "", NOW), ValueError),
    # cancel(key)
    _case("cancel-tuple", lambda e, k: e.cancel((PEER, "receive", T1), NOW), TypeError),
    _case("cancel-str", lambda e, k: e.cancel("x", NOW), TypeError),
    _case("cancel-key-dev", lambda e, k: e.cancel(TransferKey(BAD_DEV, Direction.SEND, T1), NOW), ValueError),
    _case("cancel-key-tid", lambda e, k: e.cancel(TransferKey(PEER, Direction.SEND, BAD_T), NOW), ValueError),
    _case("cancel-key-dir", lambda e, k: e.cancel(TransferKey(PEER, "sideways", T1), NOW), ValueError),
    # data_id đang được theo dõi
    _case("incoming_data-tracked", lambda e, k: e.incoming_data(OTHER, k.pend, _DO, NOW), ValueError),
    _case("incoming_data-tracked-recv", lambda e, k: e.incoming_data(PEER, k.did, _DO, NOW), ValueError),
    _case("outgoing_data-tracked", lambda e, k: e.outgoing_data(PEER, 1, k.did, _DO, NOW), ValueError),
    # send
    _case("send-own-device", lambda e, k: e.send(ME, T3, _files(1), NOW), ValueError),
    _case("send-no-files", lambda e, k: e.send(PEER, T3, [], NOW), MessageError),
    _case(
        "send-too-many-files",
        lambda e, k: e.send(PEER, T3, _files(*[0] * (MAX_OFFER_FILES + 1)), NOW),
        MessageError,
    ),
    _case(
        "send-bad-entry",
        lambda e, k: e.send(PEER, T3, [{"name": "a", "size": -1, "mtime": 0}], NOW),
        MessageError,
    ),
    _case("send-id-in-use-receive", lambda e, k: e.send(PEER, T1, _files(1), NOW), TransferIdInUse),
    _case("send-id-in-use-send", lambda e, k: e.send(OTHER, T2, _files(1), NOW), TransferIdInUse),
]


@pytest.mark.parametrize(("call", "exc"), CASES)
def test_static_param_errors_raise_and_leave_state_unchanged(tx, call, exc):
    e, k = rich_engine()
    before = tx.snapshot(e)
    receiving = e.receiving
    with pytest.raises(exc):
        call(e, k)
    assert tx.snapshot(e) == before
    assert e.receiving is receiving
    # Mốc `now` không bị dời bởi lời gọi lỗi lúc 20: lời gọi lúc 15 vẫn hợp lệ.
    e.tick(15.0)


def test_rich_engine_state_is_non_trivial(tx):
    e, _k = rich_engine()
    infos = e.transfers()
    assert [i.key for i in infos] == [
        TransferKey(PEER, Direction.RECEIVE, T1),
        TransferKey(OTHER, Direction.SEND, T2),
    ]
    assert e.next_wakeup() == 19.5
    assert e.promised_bytes() > 0


def test_now_must_not_go_backwards_after_success(tx):
    e, _ = rich_engine()
    with pytest.raises(ValueError):
        e.tick(9.0)
    e.tick(10.0)  # bằng mốc: hợp lệ
    e.tick(10.0)


def test_failed_call_with_earlier_now_does_not_move_mark(tx):
    e, k = rich_engine()
    with pytest.raises(ValueError):
        e.block_received(PEER, k.did, 1, b"short", 12.0)
    e.tick(11.0)


def test_receiving_setter_rejects_non_bool_without_change(tx):
    e = TransferEngine(ME, receiving=False)
    with pytest.raises(TypeError):
        e.receiving = 1
    assert e.receiving is False


def test_static_errors_win_over_unknown_transfer(tx):
    e = TransferEngine(ME, receiving=True)
    with pytest.raises(ValueError):
        e.synced(PEER, T3, 0, 0, 1.0)
    with pytest.raises(ValueError):
        e.block_read(PEER, 999, 0, b"short", 1.0)
    with pytest.raises(ValueError):
        e.reread_done(PEER, T3, 0, [2, 1], None, 1.0)
    with pytest.raises(MessageError):
        e.decide("nope", T3, True, 1.0)
    assert e.transfers() == ()


def test_block_read_beyond_file_closes_forgotten_connection_but_errors_on_served(tx):
    s = tx.Send(tx.entries(2), client=False)
    s.start()
    s.resume(0)
    did, acts = s.open(0, 1)
    assert tx.one(acts, SendBlocks).data_id == did
    before = tx.snapshot(s.e)
    with pytest.raises(ValueError):
        s.e.block_read(tx.PEER, did, 2, s.dg(0, 2), s.now + 1)
    assert tx.snapshot(s.e) == before
    s.call("data_closed", tx.PEER, did, False)
    assert s.call("block_read", tx.PEER, did, 2, s.dg(0, 2)) == [CloseData(device_id=tx.PEER, data_id=did)]


def test_reread_done_for_ended_transfer_is_ignored(tx):
    r = tx.Recv(tx.entries(2))
    r.accept()
    r.call("cancel", r.key)
    assert r.call("reread_done", tx.PEER, r.T, 0, [1], None) == []
    assert r.call("reread_done", tx.PEER, r.T, 0, [], DG) == []


HUGE = 10**400  # `int` hữu hạn nhưng không đổi được sang `float`


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda e: e.tick(HUGE), id="tick"),
        pytest.param(lambda e: e.control_up(PEER, 9, False, HUGE), id="control_up"),
        pytest.param(lambda e: e.control_lost(PEER, 1, HUGE), id="control_lost"),
        pytest.param(lambda e: e.send(OTHER, T3, _files(1), HUGE), id="send"),
        pytest.param(lambda e: e.cancel(TransferKey(PEER, Direction.RECEIVE, T1), HUGE), id="cancel"),
    ],
)
def test_huge_int_now_raises_only_spec_errors(tx, call):
    """Mục 2.7 và AC-18: `now` sai chỉ ném `TypeError`/`ValueError`; lỗi thì trạng thái và mốc `now`
    giữ nguyên (soat-code tái hiện `OverflowError` với `tick(10**400)`). Nếu engine nhận giá trị đó
    thì các lời gọi đọc trạng thái và `tick` sau đó cũng không được ném lỗi.
    """
    e, _ = rich_engine()
    before = tx.snapshot(e)
    try:
        call(e)
    except (TypeError, ValueError):
        assert tx.snapshot(e) == before
        e.tick(15.0)
        return
    tx.snapshot(e)
    e.tick(HUGE)
    tx.snapshot(e)


def test_huge_free_space_and_max_file_size_are_valid(tx):
    """`free_bytes`, `max_file_size`: `int` ≥ 0 là hợp lệ (mục 2.6, 2.7), kể cả rất lớn."""
    offer = Offer(transfer_id=T1, block_size=BLOCK_SIZE, files=tuple(_files(2)))
    assert transfer.check_offer(offer, free_bytes=HUGE, max_file_size=HUGE) is None
    r = tx.Recv(tx.entries(2))
    acts = r.to_asking(free=HUGE, max_size=HUGE)
    assert tx.one(acts, transfer.AskUser).transfer_id == r.T


def test_events_for_own_device_are_ignored(tx):
    e = TransferEngine(ME, receiving=True)
    assert e.control_up(ME, 1, False, 1.0) == []
    assert e.message(ME, 1, Offer(transfer_id=T1, block_size=BLOCK_SIZE, files=tuple(_files(1))), 1.1) == []
    assert e.transfers() == ()


# ---- Hằng số khớp tài liệu, core không I/O ----------------------------------------------------------


def test_constants_match_protocol_text():
    protocol = (ROOT / "docs" / "protocol.md").read_text(encoding="utf-8")
    assert f"**Mỗi {transfer.SYNC_EVERY} khối**" in protocol
    assert f"tối đa **{transfer.MAX_QUEUED_OFFERS} `offer` chưa trả lời**" in protocol
    assert f"tối đa **{transfer.MAX_PENDING_DATA} kết nối dữ liệu đang chờ `resume`**" in protocol
    assert f"chờ tối đa {transfer.DATA_OPEN_WAIT:g} s" in protocol
    assert f"Quá **{transfer.MAX_BAD_BLOCKS} lần** liên tiếp" in protocol
    assert f"vẫn sai **quá {transfer.MAX_VERIFY_FAILURES} lần**" in protocol
    assert f"tối đa **{transfer.MAX_REMEMBERED}** mục" in protocol
    assert f"sau {transfer.DATA_FAIL_REPORT_AFTER} lần thất bại liên tiếp" in protocol
    assert f"lịch {transfer.DATA_RETRY_BASE:g} s, 2 s, 4 s… tối đa {transfer.DATA_RETRY_MAX:g} s" in protocol
    # soat-code P3-0: thời hạn nhớ (bia mộ, `reject`, lượt đã xong) ghi một chỗ ở gạch “Bia mộ” của
    # protocol §6.3, tính bằng giờ; phải dài hơn hạn nối lại của máy kia cộng độ lệch keepalive.
    sec63 = protocol.split("### 6.3.", 1)[1].split("\n## ", 1)[0]
    ttl_hours = transfer.REMEMBER_TTL / 3600
    assert ttl_hours == int(ttl_hours)
    assert f"trong **{ttl_hours:g} giờ**" in sec63
    assert transfer.REMEMBER_TTL > transfer.RECONNECT_GRACE + keepalive.DEAD_AFTER
    assert "trong 24 giờ, bằng thời hạn bên kia còn thử nối lại" not in sec63
    assert f"từng đoạn {ratelimit.CHUNK_SIZE // 1024} KiB" in protocol
    # spec-p3 P3-0: hạn chờ nối lại là 24 giờ, ghi ở protocol §6.2; gạch “Giai đoạn 2 dừng sớm hơn” đã bỏ.
    # (ke_hoach §5.2 Giai đoạn 2 còn ghi 30 s như lịch sử của giai đoạn đó, không phải giá trị hiện tại.)
    sec62 = protocol.split("### 6.2.", 1)[1].split("### 6.3.", 1)[0]
    assert transfer.RECONNECT_GRACE == 24 * 3600
    assert "trong tối đa 24 giờ" in sec62
    assert "dừng sớm hơn 24 giờ" not in sec62
    assert "thời hạn chờ nối lại ngắn hơn" not in sec62


def test_architecture_lists_transfer_and_ratelimit_as_done():
    arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    rows = {line.split("|")[2].strip(" `"): line for line in arch.splitlines() if line.startswith("| core |")}
    assert "ratelimit.py" in rows and rows["ratelimit.py"].rstrip().endswith("✅ |")
    assert rows["transfer.py"].rstrip().endswith("✅ |")


@pytest.mark.parametrize("module", ["transfer.py", "ratelimit.py"])
def test_core_modules_have_no_clock_randomness_or_io(module):
    src = (ROOT / "src" / "teleport" / "core" / module).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    forbidden = {
        "time",
        "random",
        "secrets",
        "uuid",
        "os",
        "datetime",
        "threading",
        "asyncio",
        "socket",
        "pathlib",
    }
    assert imported.isdisjoint(forbidden), imported & forbidden


def test_message_module_exposes_validate_transfer_id():
    from teleport.core import messages

    assert messages.validate_transfer_id(T1) == T1
    with pytest.raises(MessageError):
        messages.validate_transfer_id(BAD_T)
    with pytest.raises(MessageError):
        messages.validate_transfer_id(None)
    assert messages._validate_transfer_id(T2) == T2  # bí danh cũ vẫn dùng được


# ---- AC-18: fuzz engine ------------------------------------------------------------------------------


FILESETS = [(1,), (0,), (2, 0), (3, 1), (0, 0, 2)]
RUNNING = {TransferStatus.TRANSFERRING, TransferStatus.VERIFYING, TransferStatus.RECONNECTING}
UNANSWERED = {TransferStatus.WAITING, TransferStatus.QUEUED}


def _salt_digest(i: int, salt: str) -> bytes:
    import hashlib

    return hashlib.sha256(f"{salt}:{i}".encode()).digest()


class Fuzz:
    """Gọi engine ngẫu nhiên (đúng kiểu, giá trị tùy ý) và kiểm bất biến sau mỗi lời gọi."""

    def __init__(self, tx, data, e: TransferEngine) -> None:
        self.tx = tx
        self.data = data
        self.e = e
        self.now = 1.0
        self.ids = itertools.count(1000)
        self.cur: dict[str, tuple[int, bool] | None] = {PEER: None, OTHER: None}
        self.files: dict[str, tuple[FileEntry, ...]] = {}
        self.live: dict[tuple[str, int], tuple[str, int]] = {}  # (dev, did) → (T, số khối)
        self.serving: set[tuple[str, int]] = set()
        self.pending: dict[str, set[int]] = {PEER: set(), OTHER: set()}
        self.active: dict[tuple[str, str], int] = {}
        self.hw: dict[tuple[str, str, int], int] = {}
        self.tokens: list[tuple[str, str, int, int]] = []
        self.rereads: list[tuple[str, str, int, bytes]] = []
        self.finals: list[tuple[str, str, int]] = []
        self.checks: list[tuple[str, str, int, bytes]] = []
        self.opens: list[tuple[str, int, DataOpen]] = []
        self.asks: list[tuple[str, str]] = []

    def draw(self, strategy):
        return self.data.draw(strategy)

    def pick(self, seq, fallback=None):
        seq = list(seq)
        if not seq:
            return fallback
        return seq[self.draw(st.integers(0, len(seq) - 1))]

    # -- gọi engine và kiểm bất biến --

    def call(self, name, *args, allowed=()):
        before = {i.key for i in self.e.transfers()}
        try:
            acts = getattr(self.e, name)(*args, self.now)
        except allowed:
            assert {i.key for i in self.e.transfers()} == before
            return None
        self.observe(name, args, acts, before)
        return acts

    def observe(self, name, args, acts, before):
        tx = self.tx
        assert isinstance(acts, list)
        tx.check_order(acts)
        if name == "message" and isinstance(args[2], Cancel):
            assert tx.of(acts, SendMessage) == [], "cancel nhận được không được trả lời"
        ended_keys = []
        for a in acts:
            dev = getattr(a, "device_id", None)
            if isinstance(a, SendMessage):
                cur = self.cur[a.device_id]
                assert cur is not None and cur[0] == a.control_id, (a, cur)
            elif isinstance(a, OpenData):
                cur = self.cur[a.device_id]
                assert cur is not None and cur == (a.control_id, True), (a, cur)
                self.opens.append((a.device_id, a.control_id, a.message))
            elif isinstance(a, CloseData):
                self.forget(dev, a.data_id)
            elif isinstance(a, SendBlocks | ReceiveBlocks):
                key = (dev, a.transfer_id)
                old = self.active.get(key)
                assert old is None or old == a.data_id or (dev, old) not in self.live, (
                    "hai kết nối dữ liệu cùng phục vụ một lượt",
                    a,
                )
                assert block_count(a.size) > 0, "không có kết nối dữ liệu cho file rỗng"
                self.active[key] = a.data_id
                self.live[(dev, a.data_id)] = (a.transfer_id, block_count(a.size))
                if isinstance(a, SendBlocks):
                    self.serving.add((dev, a.data_id))
                self.pending[dev].discard(a.data_id)
            elif isinstance(a, OpenPart):
                self.hw[(dev, a.transfer_id, a.file_index)] = -1
            elif isinstance(a, WriteBlock):
                k = (dev, a.transfer_id, a.file_index)
                h = self.hw.get(k, -1)
                assert a.index <= h + 1, f"WriteBlock({a.index}) với hw = {h}"
                self.hw[k] = max(h, a.index)
            elif isinstance(a, SyncPart):
                self.tokens.append((dev, a.transfer_id, a.file_index, a.token))
            elif isinstance(a, Reread):
                self.rereads.append((dev, a.transfer_id, a.file_index, bytes(a.leaves)))
            elif isinstance(a, Finalize):
                assert name == "reread_done" and list(args[3]) == [], "Finalize chỉ sau reread_done sạch"
                self.finals.append((dev, a.transfer_id, a.file_index))
            elif isinstance(a, CheckSource):
                self.checks.append((dev, a.transfer_id, a.file_index, bytes(a.leaves)))
            elif isinstance(a, transfer.AskUser | transfer.CheckSpace):
                self.asks.append((dev, a.transfer_id))
            elif isinstance(a, Report) and isinstance(a.event, TransferEnded):
                ended_keys.append(a.event.key)
        if name == "incoming_data":
            dev, did = args[0], args[1]
            if (dev, did) not in self.live and not any(
                isinstance(a, CloseData) and a.data_id == did for a in acts
            ):
                self.pending[dev].add(did)
        if name == "data_closed":
            self.forget(args[0], args[1])
        after = {i.key for i in self.e.transfers()}
        assert len(ended_keys) == len(set(ended_keys)), "TransferEnded hai lần trong một lời gọi"
        assert set(ended_keys) <= before, "TransferEnded cho lượt không tồn tại"
        assert before - after == set(ended_keys), "lượt biến mất mà không có TransferEnded (hay ngược lại)"
        for key in ended_keys:
            assert self.e.info(key) is None
        self.check_counts()

    def forget(self, dev, did):
        self.live.pop((dev, did), None)
        self.serving.discard((dev, did))
        self.pending[dev].discard(did)

    def check_counts(self):
        infos = self.e.transfers()
        for dev in (PEER, OTHER):
            recv = [i for i in infos if i.key.device_id == dev and i.key.direction == Direction.RECEIVE]
            assert sum(i.status in RUNNING for i in recv) <= 1
            assert sum(i.status in UNANSWERED for i in recv) <= transfer.MAX_QUEUED_OFFERS
            assert len(self.pending[dev]) <= transfer.MAX_PENDING_DATA
        for i in infos:
            assert 0 <= i.done_bytes <= i.total_bytes == sum(f.size for f in i.files)

    # -- sinh giá trị --

    def dev(self):
        return self.draw(st.sampled_from([PEER, OTHER]))

    def tid(self):
        return self.draw(st.sampled_from([T1, T2, T3]))

    def cid_for(self, dev):
        cur = self.cur[dev]
        if cur is not None and self.draw(st.integers(0, 4)):
            return cur[0]
        return self.draw(st.integers(1, 6))

    def fileset(self):
        return tuple(_files(*self.draw(st.sampled_from(FILESETS))))

    def nblocks(self, T, f):
        files = self.files.get(T)
        if files is None or f >= len(files):
            return None
        return block_count(files[f].size)

    def bitfield(self, T, f):
        n = self.nblocks(T, f)
        if n is not None and self.draw(st.integers(0, 3)):
            return self.bits(n).to_base64()
        return self.draw(st.sampled_from(["", "AA==", "gA==", "/w==", "AAA="]))

    def bits(self, n):
        b = Bitfield(n)
        for i in range(n):
            if self.draw(st.booleans()):
                b.set(i)
        return b

    def root_for(self, T, f):
        n = self.nblocks(T, f)
        choices = ["00" * 32]
        if n is not None:
            choices += [merkle_root(b"".join(_salt_digest(i, "s") for i in range(n))).hex()]
        return self.draw(st.sampled_from(choices))

    def message(self, T):
        kind = self.draw(
            st.sampled_from(["offer", "accept", "reject", "resume", "complete", "verified", "cancel"])
        )
        f = self.draw(st.integers(0, 2))
        if kind == "offer":
            files = self.files.get(T) if self.draw(st.booleans()) and T in self.files else self.fileset()
            self.files.setdefault(T, files)
            return Offer(transfer_id=T, block_size=BLOCK_SIZE, files=files)
        if kind == "accept":
            return Accept(transfer_id=T)
        if kind == "reject":
            reason = self.draw(
                st.sampled_from(["user", "busy", "no_space", "file_too_large", "not_receiving"])
            )
            extra = {}
            if reason == "no_space":
                extra = {"needed": 10, "available": 1}
            elif reason == "file_too_large":
                extra = {"file_index": f, "max_size": 1}
            return Reject(transfer_id=T, reason=reason, **extra)
        if kind == "resume":
            seq = self.draw(st.sampled_from([1, 2, 3, 4, 5, MAX_SAFE_INTEGER]))
            return Resume(transfer_id=T, file_index=f, seq=seq, bitfield=self.bitfield(T, f))
        if kind == "complete":
            return Complete(transfer_id=T, file_index=f, merkle_root=self.root_for(T, f))
        if kind == "verified":
            return Verified(transfer_id=T, file_index=f)
        reason = self.draw(
            st.sampled_from(["user", "error", "source_changed", "unknown_transfer", "expired"])
        )
        return Cancel(transfer_id=T, reason=reason)

    def data_open(self):
        return DataOpen(
            transfer_id=self.tid(),
            file_index=self.draw(st.integers(0, 2)),
            resume_seq=self.draw(st.sampled_from([1, 2, 3, 4, 5, MAX_SAFE_INTEGER])),
        )

    def some_did(self, dev, *, role: str = "any"):
        """data_id để báo sự kiện: thường là kết nối đã biết đúng vai, đôi khi là id lạ (< 1000).

        Không gửi sự kiện của vai này cho kết nối của vai kia (bên gửi: `block_read`; bên nhận:
        `block_received`, `block_bad`): app biết mỗi kết nối là của vai nào.
        """
        live = [d for (dv, d) in self.live if dv == dev]
        if role == "send":
            live = [d for d in live if (dev, d) in self.serving]
        elif role == "recv":
            live = [d for d in live if (dev, d) not in self.serving]
        known = live + (sorted(self.pending[dev]) if role in ("any", "send") else [])
        if known and self.draw(st.integers(0, 4)):
            return self.pick(known)
        return self.draw(st.integers(1, 999))

    # -- các thao tác --

    def step(self):
        self.now += self.draw(st.sampled_from([0.0, 0.001, 0.5, 3.0, 11.0, 31.0]))
        op = self.draw(
            st.sampled_from(
                [
                    "control_up",
                    "control_lost",
                    "message",
                    "message",
                    "message",
                    "send",
                    "space_checked",
                    "decide",
                    "cancel",
                    "incoming_data",
                    "outgoing_data",
                    "outgoing_data_failed",
                    "data_closed",
                    "block_read",
                    "block_received",
                    "block_received",
                    "block_bad",
                    "synced",
                    "reread_done",
                    "finalized",
                    "storage_failed",
                    "source_checked",
                    "source_failed",
                    "tick",
                ]
            )
        )
        getattr(self, "op_" + op)()

    def op_control_up(self):
        dev = self.dev()
        cid = self.cid_for(dev)
        client = self.draw(st.booleans())
        cur = self.cur[dev]
        if cur is None or cur[0] != cid:
            self.cur[dev] = (cid, client)  # trước lời gọi: SendMessage của lần đồng bộ mang cid mới
        self.call("control_up", dev, cid, client)

    def op_control_lost(self):
        dev = self.dev()
        cid = self.cid_for(dev)
        if self.cur[dev] is not None and self.cur[dev][0] == cid:
            self.cur[dev] = None
        self.call("control_lost", dev, cid)

    def op_message(self):
        dev = self.dev()
        self.call("message", dev, self.cid_for(dev), self.message(self.tid()))

    def op_send(self):
        dev = self.dev()
        T = self.tid()
        files = self.fileset()
        acts = self.call("send", dev, T, list(files), allowed=(TransferIdInUse,))
        if acts is not None:
            self.files.setdefault(T, files)

    def op_space_checked(self):
        dev, T = self.pick(self.asks, (self.dev(), self.tid()))
        free = self.draw(st.sampled_from([None, 0, 10**15]))
        mx = self.draw(st.sampled_from([None, 0, 10**12]))
        self.call("space_checked", dev, T, free, mx)

    def op_decide(self):
        dev, T = self.pick(self.asks, (self.dev(), self.tid()))
        self.call("decide", dev, T, self.draw(st.booleans()))

    def op_cancel(self):
        keys = [i.key for i in self.e.transfers()]
        key = self.pick(keys) if keys and self.draw(st.integers(0, 3)) else None
        if key is None:
            key = TransferKey(self.dev(), self.draw(st.sampled_from(list(Direction))), self.tid())
        self.call("cancel", key)

    def op_incoming_data(self):
        self.call("incoming_data", self.dev(), next(self.ids), self.data_open())

    def op_outgoing_data(self):
        dev, cid, msg = self.pick(self.opens, (self.dev(), None, None))
        if msg is None:
            cid, msg = self.cid_for(dev), self.data_open()
        self.call("outgoing_data", dev, cid, next(self.ids), msg)

    def op_outgoing_data_failed(self):
        dev, cid, msg = self.pick(self.opens, (self.dev(), None, None))
        if msg is None:
            cid, msg = self.cid_for(dev), self.data_open()
        self.call("outgoing_data_failed", dev, cid, msg)

    def op_data_closed(self):
        dev = self.dev()
        self.call("data_closed", dev, self.some_did(dev), self.draw(st.booleans()))

    def op_block_read(self):
        dev = self.dev()
        did = self.some_did(dev, role="send")
        n = self.live[(dev, did)][1] if (dev, did) in self.serving else 4
        i = self.draw(st.integers(0, max(n - 1, 0)))
        salt = self.draw(st.sampled_from(["s", "s", "khac"]))
        self.call("block_read", dev, did, i, _salt_digest(i, salt))

    def op_block_received(self):
        dev = self.dev()
        did = self.some_did(dev, role="recv")
        n = self.live.get((dev, did), (None, 4))[1]
        i = self.draw(st.integers(0, n))  # n: ngoài file, phải bị tính là khối sai
        salt = self.draw(st.sampled_from(["s", "s", "khac"]))
        self.call("block_received", dev, did, i, _salt_digest(i, salt))

    def op_block_bad(self):
        dev = self.dev()
        self.call("block_bad", dev, self.some_did(dev, role="recv"))

    def op_synced(self):
        if self.tokens and self.draw(st.integers(0, 4)):
            dev, T, f, tok = self.tokens[0] if self.draw(st.booleans()) else self.pick(self.tokens)
        else:
            dev, T, f, tok = (
                self.dev(),
                self.tid(),
                self.draw(st.integers(0, 2)),
                self.draw(st.integers(1, 50)),
            )
        self.call("synced", dev, T, f, tok)

    def op_reread_done(self):
        if self.rereads and self.draw(st.integers(0, 4)):
            dev, T, f, leaves = self.pick(self.rereads)
            n = len(leaves) // 32
            bad = sorted(self.draw(st.sets(st.integers(0, n - 1), max_size=3))) if n else []
            root = None if bad else self.draw(st.sampled_from([merkle_root(leaves), b"\x07" * 32]))
        else:
            dev, T, f = self.dev(), self.tid(), self.draw(st.integers(0, 2))
            bad, root = [], b"\x08" * 32
        self.call("reread_done", dev, T, f, bad, root)

    def op_finalized(self):
        dev, T, f = self.pick(self.finals, (self.dev(), self.tid(), 0))
        self.call("finalized", dev, T, f, self.draw(st.sampled_from(["a.bin", "a (1).bin"])))

    def op_storage_failed(self):
        self.call("storage_failed", self.dev(), self.tid(), self.draw(st.sampled_from(list(DiskOp))))

    def op_source_checked(self):
        if self.checks and self.draw(st.integers(0, 4)):
            dev, T, f, leaves = self.pick(self.checks)
            files = self.files.get(T)
            fe = files[f] if files and f < len(files) else FileEntry(name="x", size=0, mtime=0)
            size = fe.size if self.draw(st.integers(0, 3)) else fe.size + 1
            mtime = fe.mtime if self.draw(st.integers(0, 3)) else fe.mtime + 1
            root = merkle_root(leaves)
        else:
            dev, T, f = self.dev(), self.tid(), self.draw(st.integers(0, 2))
            size, mtime, root = 1, 1, b"\x09" * 32
        self.call("source_checked", dev, T, f, size, mtime, root)

    def op_source_failed(self):
        self.call(
            "source_failed", self.dev(), self.tid(), self.draw(st.integers(0, 2)), self.draw(st.booleans())
        )

    def op_tick(self):
        w = self.e.next_wakeup()
        if w is not None and w > self.now and self.draw(st.booleans()):
            self.now = w
        self.call("tick")


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(data=st.data(), receiving=st.booleans())
def test_fuzz_engine_never_raises_outside_spec_and_keeps_invariants(tx, data, receiving):
    fz = Fuzz(tx, data, TransferEngine(ME, receiving=receiving))
    for _ in range(data.draw(st.integers(1, 60), label="steps")):
        fz.step()
