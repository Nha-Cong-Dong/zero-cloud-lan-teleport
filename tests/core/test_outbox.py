"""Kiểm thử mã hóa / giải mã hàng đợi gửi `outbox.json` (`teleport.core.outbox`)."""

from __future__ import annotations

import datetime
import json

import pytest

from teleport.core import outbox as core_outbox
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.messages import FileEntry, Offer
from teleport.core.outbox import (
    MAX_OUTBOX_ENTRIES,
    MAX_OUTBOX_FILE,
    OutboxError,
    OutboxRecord,
    decode_outbox,
    encode_outbox,
)

DEV = "a" * 32
ROOT0 = "1" * 64
ROOT1 = "2" * 64


def _tid(n: int) -> str:
    return f"{n:032x}"


def _make_record(
    n: int = 1,
    *,
    is_dir: bool = False,
    roots: tuple[tuple[int, str], ...] = (),
    hour_offset: int = 0,
) -> OutboxRecord:
    tz = datetime.timezone(datetime.timedelta(hours=hour_offset))
    entry = FileEntry(
        "empty_dir" if is_dir else f"file_{n}.bin", 0 if is_dir else 2048, 1730000000000, is_dir=is_dir
    )
    offer = Offer(_tid(n), BLOCK_SIZE, (entry,))
    return OutboxRecord(
        device_id=DEV,
        offer=offer,
        paths=(f"/tmp/src_{n}",),
        created_at=datetime.datetime(2026, 11, 22, 7, 0, n % 60, 123456, tzinfo=tz),
        roots=roots,
    )


def test_outbox_record_round_trip_with_roots_and_dir():
    f0 = FileEntry("a.bin", 1024, 1000, rel_path="folder/a.bin", is_dir=False)
    f1 = FileEntry("sub", 0, 2000, rel_path="folder/sub", is_dir=True)
    offer = Offer(_tid(10), BLOCK_SIZE, (f0, f1))
    tz_plus7 = datetime.timezone(datetime.timedelta(hours=7))
    created = datetime.datetime(2026, 11, 22, 14, 30, 45, 999999, tzinfo=tz_plus7)
    rec = OutboxRecord(
        device_id=DEV,
        offer=offer,
        paths=("C:\\src\\folder\\a.bin", "C:\\src\\folder\\sub"),
        created_at=created,
        roots=((0, ROOT0),),
    )
    assert rec.created_at == datetime.datetime(2026, 11, 22, 7, 30, 45, tzinfo=datetime.UTC)

    data = encode_outbox([rec])
    decoded = decode_outbox(data)
    assert decoded == [rec]

    updated = rec.with_root(1, ROOT1)
    assert updated.roots == ((0, ROOT0), (1, ROOT1))


def test_outbox_validation_errors():
    entry = FileEntry("x.bin", 10, 1)
    offer = Offer(_tid(1), BLOCK_SIZE, (entry,))
    now = datetime.datetime(2026, 11, 22, 7, 0, 0, tzinfo=datetime.UTC)

    with pytest.raises(OutboxError):
        OutboxRecord("bad", offer, ("/a",), now)
    with pytest.raises(OutboxError):
        OutboxRecord(DEV, "not-offer", ("/a",), now)  # type: ignore[arg-type]
    with pytest.raises(OutboxError):
        OutboxRecord(DEV, offer, (), now)
    with pytest.raises(OutboxError):
        OutboxRecord(DEV, offer, ("",), now)
    with pytest.raises(OutboxError):
        OutboxRecord(DEV, offer, ("/a\x00b",), now)
    with pytest.raises(OutboxError):
        OutboxRecord(DEV, offer, ("/a",), datetime.datetime(2026, 11, 22, 7, 0, 0))
    with pytest.raises(OutboxError):
        OutboxRecord(DEV, offer, ("/a",), now, roots=((1, ROOT0),))
    with pytest.raises(OutboxError):
        OutboxRecord(DEV, offer, ("/a",), now, roots=((0, ROOT0), (0, ROOT0)))
    with pytest.raises(OutboxError):
        OutboxRecord(DEV, offer, ("/a",), now, roots=((0, "bad"),))


def test_encode_outbox_caps_by_entries_and_bytes(monkeypatch):
    many = [_make_record(i + 1) for i in range(MAX_OUTBOX_ENTRIES + 5)]
    encoded = encode_outbox(many)
    decoded = decode_outbox(encoded)
    assert len(decoded) == MAX_OUTBOX_ENTRIES
    assert decoded[0].offer.transfer_id == _tid(6)
    assert decoded[-1].offer.transfer_id == _tid(MAX_OUTBOX_ENTRIES + 5)

    three = [_make_record(i + 1) for i in range(3)]
    two_len = len(encode_outbox(three[1:]))
    monkeypatch.setattr(core_outbox, "MAX_OUTBOX_FILE", two_len)
    trimmed = decode_outbox(encode_outbox(three))
    assert [r.offer.transfer_id for r in trimmed] == [_tid(2), _tid(3)]


def test_decode_outbox_skips_bad_entries_and_rejects_bad_root():
    good1 = _make_record(1, roots=((0, ROOT0),))
    good2 = _make_record(2, is_dir=True)
    raw_obj = json.loads(encode_outbox([good1, good2]).decode("utf-8"))
    raw_obj["entries"].insert(1, {"device_id": "invalid"})
    bad_roots = good1.to_dict()
    bad_roots["roots"] = {"00": ROOT0}
    raw_obj["entries"].append(bad_roots)
    bad_path = good2.to_dict()
    bad_path["paths"] = []
    raw_obj["entries"].append(bad_path)

    parsed = decode_outbox(json.dumps(raw_obj).encode("utf-8"))
    assert parsed == [good1, good2]

    for bad in (
        b"not json",
        b"[]",
        b'{"version": 2, "entries": []}',
        b'{"version": 1, "entries": {}}',
        b'{"version": 1, "entries": [NaN]}',
        b"x" * (MAX_OUTBOX_FILE + 1),
    ):
        with pytest.raises(OutboxError):
            decode_outbox(bad)
