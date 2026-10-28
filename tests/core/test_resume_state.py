"""Test `teleport.core.resume_state` (spec P3-1; architecture mục 4: `.state`, “Phục hồi tên đích”,
“Gộp”, `transfer.json`, `tombstones.json`).

Viết từ đặc tả, không từ code. Bố cục byte của header và bản ghi được dựng lại độc lập ở đây bằng
`struct` + `zlib.crc32` (bộ mã hóa tham chiếu `_ref_*`), và một bộ đọc tham chiếu `_ref_replay` làm
đúng luật “Đọc lại” của architecture mục 4. Mọi test của `StateReplay` so kết quả thật với bộ đọc
tham chiếu đó (kể cả trên dữ liệu bị cắt cụt, lật bit hay ngẫu nhiên).

Quy ước đơn vị: `StateReplay` được cấp **cả file `.state`** (header 41 byte rồi các bản ghi), nên
`good_end`, `log_length`, `compact_length` và `max_length` đều tính trên cả file, như độ dài thật
của `.state` trên đĩa (architecture mục 4: “kích thước gọn là độ dài của header, cộng …”).
"""

from __future__ import annotations

import ast
import contextlib
import datetime
import json
import re
import struct
import time
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, event, given, settings
from hypothesis import strategies as st

from teleport.core import resume_state as rs
from teleport.core.bitfield import Bitfield
from teleport.core.blocks import BLOCK_SIZE, MAX_BLOCKS, MAX_FILE_SIZE
from teleport.core.filenames import sanitize_filename
from teleport.core.messages import FileEntry, Offer

REPO = Path(__file__).resolve().parents[2]
ARCHITECTURE = REPO / "docs" / "architecture.md"

TID = "0123456789abcdef0123456789abcdef"
TID2 = "fedcba9876543210fedcba9876543210"
RECEIVER = "e" * 32
SENDER = "a" * 32
MAX_NAME = 255  # protocol mục 3.3: `name` ≤ 255 byte UTF-8
MAX_MARK = 65_535
KIB64 = 64 * 1024
STAMP = "2026-10-27T10:00:00Z"

SETTINGS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# --- Bộ mã hóa tham chiếu (architecture mục 4) ---------------------------------------------------

_HEADER_BODY = struct.Struct("!4sB16sIQI")  # magic, version, transfer_id, file_index, size, block_size
HEADER_LEN = _HEADER_BODY.size + 4  # 41
MARK, CLEAR, CLEAR_ALL, TARGET, RESERVED = 0x01, 0x02, 0x03, 0x04, 0x05


def _crc(data: bytes) -> bytes:
    return struct.pack("!I", zlib.crc32(data))


def _ref_header(
    transfer_id: str = TID,
    file_index: int = 0,
    size: int = 0,
    block_size: int = BLOCK_SIZE,
    *,
    magic: bytes = b"TPRS",
    version: int = 1,
) -> bytes:
    body = _HEADER_BODY.pack(magic, version, bytes.fromhex(transfer_id), file_index, size, block_size)
    return body + _crc(body)


def _ref_record(kind: int, payload: bytes) -> bytes:
    """Bản ghi có crc32 đúng: dùng để dựng cả bản ghi sai nội dung mà vẫn qua crc."""
    body = bytes([kind]) + payload
    return body + _crc(body)


def _ref_mark(pairs: list[tuple[int, bytes]]) -> bytes:
    payload = struct.pack("!H", len(pairs)) + b"".join(struct.pack("!I", i) + h for i, h in pairs)
    return _ref_record(MARK, payload)


def _ref_clear(indices: list[int]) -> bytes:
    return _ref_record(
        CLEAR, struct.pack("!H", len(indices)) + b"".join(struct.pack("!I", i) for i in indices)
    )


def _ref_clear_all() -> bytes:
    return _ref_record(CLEAR_ALL, b"")


def _ref_name(kind: int, raw: bytes) -> bytes:
    return _ref_record(kind, struct.pack("!H", len(raw)) + raw)


def _case_id(value: object) -> str:
    """Tên ca = chuỗi mô tả; bỏ phần dữ liệu (bytes rất dài làm hỏng PYTEST_CURRENT_TEST)."""
    return value if isinstance(value, str) else "data"


def _digest(i: int, salt: int = 0) -> bytes:
    return bytes([(i * 7 + salt) & 0xFF]) * 16 + struct.pack("!QQ", i, salt)


# --- Bộ đọc tham chiếu (architecture mục 4, “Đọc lại”) -------------------------------------------


@dataclass
class Model:
    bits: set[int] = field(default_factory=set)
    leaves: dict[int, bytes] = field(default_factory=dict)
    target: str | None = None
    reserved: bool = False
    good_end: int = 0  # tính trên các byte bản ghi (sau header)
    record_ends: list[int] = field(default_factory=list)
    name_records: int = 0  # số bản ghi TARGET + RESERVED đã áp

    def snapshot(self) -> tuple[frozenset[int], dict[int, bytes], str | None, bool]:
        return frozenset(self.bits), {i: self.leaves[i] for i in self.bits}, self.target, self.reserved


def _valid_name(raw: bytes) -> str | None:
    if not 1 <= len(raw) <= MAX_NAME:
        return None
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return name if sanitize_filename(name) == name else None


def _ref_parse_one(data: bytes, pos: int, n_blocks: int) -> tuple[int, tuple] | None:
    """(cuối bản ghi, thao tác) hoặc None nếu bản ghi ở `pos` hỏng / cụt."""
    kind = data[pos]
    if kind in (MARK, CLEAR, TARGET, RESERVED):
        if pos + 3 > len(data):
            return None
        (n,) = struct.unpack_from("!H", data, pos + 1)
        item = {MARK: 36, CLEAR: 4, TARGET: 1, RESERVED: 1}[kind]
        if kind in (MARK, CLEAR) and n == 0:
            return None
        if kind in (TARGET, RESERVED) and not 1 <= n <= MAX_NAME:
            return None
        end = pos + 3 + n * item + 4
    elif kind == CLEAR_ALL:
        end = pos + 5
    else:
        return None
    if end > len(data) or data[end - 4 : end] != _crc(data[pos : end - 4]):
        return None
    body = data[pos + 3 : end - 4]
    if kind == MARK:
        pairs = [
            (struct.unpack_from("!I", body, 36 * k)[0], body[36 * k + 4 : 36 * k + 36]) for k in range(n)
        ]
        if any(i >= n_blocks for i, _ in pairs):
            return None
        return end, ("mark", pairs)
    if kind == CLEAR:
        idx = [struct.unpack_from("!I", body, 4 * k)[0] for k in range(n)]
        if any(i >= n_blocks for i in idx):
            return None
        return end, ("clear", idx)
    if kind == CLEAR_ALL:
        return end, ("clear_all",)
    name = _valid_name(body)
    if name is None:
        return None
    return end, ("target" if kind == TARGET else "reserved", name)


def _ref_replay(n_blocks: int, records: bytes) -> Model:
    m = Model()
    pos = 0
    while pos < len(records):
        parsed = _ref_parse_one(records, pos, n_blocks)
        if parsed is None:
            break
        if parsed[1][0] in ("target", "reserved"):
            # Bản ghi tên thứ `MAX_NAME_RECORDS` + 1 là bản ghi hỏng (architecture mục 4).
            if m.name_records >= rs.MAX_NAME_RECORDS:
                break
            m.name_records += 1  # cả RESERVED khác tên (bị bỏ qua nhưng vẫn là bản ghi hợp lệ)
        pos, op = parsed
        if op[0] == "mark":
            for i, h in op[1]:
                m.bits.add(i)
                m.leaves[i] = h
        elif op[0] == "clear":
            m.bits.difference_update(op[1])
        elif op[0] == "clear_all":
            m.bits.clear()
        elif op[0] == "target":
            m.target, m.reserved = op[1], False
        elif op[1] == m.target:
            m.reserved = True
        m.record_ends.append(pos)
    m.good_end = pos
    return m


def _ref_ops(n_blocks: int, records: bytes) -> list[tuple]:
    ops, pos = [], 0
    while pos < len(records):
        parsed = _ref_parse_one(records, pos, n_blocks)
        assert parsed is not None, "bản gọn phải đọc lại được trọn"
        pos, op = parsed
        ops.append(op)
    return ops


# --- Chuẩn hóa kết quả của module (kiểu `bits` / `leaves` không do spec chốt) -------------------


def _bit_set(bits: Any, n: int) -> set[int]:
    if isinstance(bits, bytes | bytearray | memoryview):
        raw = bytes(bits)
        return {i for i in range(n) if raw[i >> 3] & (0x80 >> (i & 7))}
    if isinstance(bits, set | frozenset):
        return set(bits)
    return {i for i in range(n) if i in bits}


def _leaf(leaves: Any, i: int) -> bytes:
    if isinstance(leaves, Mapping):
        return bytes(leaves[i])
    if isinstance(leaves, bytes | bytearray | memoryview):
        return bytes(leaves[32 * i : 32 * i + 32])
    return bytes(leaves[i])


def _snap(result: Any, n: int) -> tuple[frozenset[int], dict[int, bytes], str | None, bool]:
    bits = _bit_set(result.bits, n)
    return frozenset(bits), {i: _leaf(result.leaves, i) for i in bits}, result.target, bool(result.reserved)


def _replay(
    n_blocks: int, data: bytes, *, max_length: int | None = None, chunks: list[int] | None = None
) -> Any:
    """Cấp `data` cho `StateReplay` theo từng đoạn (cỡ lấy vòng từ `chunks`), dừng khi `feed` trả False."""
    if max_length is None:
        max_length = rs.max_state_length(n_blocks)
    replay = rs.StateReplay(n_blocks, max_length=max_length)
    sizes = chunks or [len(data) or 1]
    pos = k = 0
    while pos < len(data):
        step = sizes[k % len(sizes)]
        k += 1
        if not replay.feed(data[pos : pos + step]):
            break
        pos += step
    return replay.finish()


def _compact_bytes(result: Any, **kw: Any) -> list[bytes]:
    return [
        bytes(r) for r in rs.compact_records(result.bits, result.leaves, result.target, result.reserved, **kw)
    ]


# --- Chiến lược hypothesis -------------------------------------------------------------------------

names = st.text(min_size=1, max_size=40).map(sanitize_filename)
digests = st.binary(min_size=32, max_size=32)


@st.composite
def logs(draw: st.DrawFn, max_blocks: int = 60) -> tuple[int, list[bytes]]:
    """(n_blocks, danh sách bản ghi hợp lệ) theo bộ mã hóa tham chiếu."""
    n = draw(st.integers(1, max_blocks))
    index = st.integers(0, n - 1)
    used_names: list[str] = []
    records: list[bytes] = []
    for _ in range(draw(st.integers(0, 14))):
        kind = draw(st.sampled_from(["mark", "mark", "mark", "clear", "clear_all", "target", "reserved"]))
        if kind == "mark":
            idx = draw(st.lists(index, min_size=1, max_size=20, unique=True))
            records.append(_ref_mark([(i, draw(digests)) for i in idx]))
        elif kind == "clear":
            records.append(_ref_clear(draw(st.lists(index, min_size=1, max_size=8))))
        elif kind == "clear_all":
            records.append(_ref_clear_all())
        else:
            if used_names and draw(st.booleans()):
                name = draw(st.sampled_from(used_names))
            else:
                name = draw(names)
                used_names.append(name)
            records.append(_ref_name(TARGET if kind == "target" else RESERVED, name.encode()))
    return n, records


# =================================================================================================
# Header
# =================================================================================================


def test_header_layout_matches_architecture():
    data = rs.encode_header(TID, 3, 5 * BLOCK_SIZE + 7)
    assert len(data) == HEADER_LEN == 41
    assert data == _ref_header(TID, 3, 5 * BLOCK_SIZE + 7, BLOCK_SIZE)
    assert data[:4] == b"TPRS"
    assert data[4] == 1


def test_header_with_explicit_block_size():
    assert rs.encode_header(TID, 0, 10, block_size=4096) == _ref_header(TID, 0, 10, 4096)


def test_header_length_documented_in_architecture():
    text = ARCHITECTURE.read_text(encoding="utf-8")
    m = re.search(r"\*\*Header\*\* \((\d+) byte\)", text)
    assert m is not None, "architecture mục 4 phải ghi độ dài header"
    assert int(m.group(1)) == len(rs.encode_header(TID, 0, 0))


def test_check_header_accepts_matching_header():
    data = rs.encode_header(TID, 2, 12345)
    assert rs.check_header(data, transfer_id=TID, file_index=2, size=12345, block_size=BLOCK_SIZE) is True


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (_ref_header(TID, 2, 12345, magic=b"TPRX"), "sai magic (crc vẫn đúng)"),
        (_ref_header(TID, 2, 12345, version=2), "version lạ (crc vẫn đúng)"),
        (_ref_header(TID, 2, 12345, version=0), "version 0 (crc vẫn đúng)"),
        (_ref_header(TID, 2, 12345)[:-1] + bytes([_ref_header(TID, 2, 12345)[-1] ^ 1]), "sai crc32"),
        (_ref_header(TID, 2, 12345)[:40], "thiếu 1 byte"),
        (b"", "rỗng"),
        (_ref_header(TID2, 2, 12345), "transfer_id khác"),
        (_ref_header(TID, 3, 12345), "file_index khác"),
        (_ref_header(TID, 2, 12346), "size khác"),
        (_ref_header(TID, 2, 12345, 4096), "block_size khác"),
    ],
    ids=_case_id,
)
def test_check_header_rejects_single_wrong_field(data: bytes, why: str):
    assert rs.check_header(data, transfer_id=TID, file_index=2, size=12345, block_size=BLOCK_SIZE) is False, (
        why
    )


def test_check_header_flip_in_body_byte_is_caught_by_crc():
    """Lật một bit trong transfer_id mà giữ crc cũ: chỉ crc bắt được (bên gọi đối chiếu tid mới)."""
    data = bytearray(_ref_header(TID, 2, 12345))
    data[10] ^= 0x01
    flipped_tid = bytes(data[5:21]).hex()
    assert (
        rs.check_header(bytes(data), transfer_id=flipped_tid, file_index=2, size=12345, block_size=BLOCK_SIZE)
        is False
    )


@SETTINGS
@given(st.binary(max_size=80))
def test_check_header_never_raises_on_garbage(data: bytes):
    result = rs.check_header(data, transfer_id=TID, file_index=0, size=0, block_size=BLOCK_SIZE)
    assert result is (data == _ref_header(TID, 0, 0))


@SETTINGS
@given(
    st.binary(min_size=16, max_size=16).map(bytes.hex),
    st.integers(0, 999),
    st.integers(0, MAX_FILE_SIZE),
)
def test_header_roundtrip(tid: str, file_index: int, size: int):
    data = rs.encode_header(tid, file_index, size)
    assert data == _ref_header(tid, file_index, size)
    assert rs.check_header(data, transfer_id=tid, file_index=file_index, size=size, block_size=BLOCK_SIZE)


@pytest.mark.parametrize(
    "args",
    [("0123", 0, 0), (TID.upper(), 0, 0), (TID, -1, 0), (TID, 2**32, 0), (TID, 0, -1), (TID, 0, 2**64)],
)
def test_encode_header_rejects_bad_arguments(args: tuple):
    with pytest.raises(ValueError):
        rs.encode_header(*args)


# =================================================================================================
# Bản ghi
# =================================================================================================


def test_mark_of_16_blocks_is_583_bytes():
    pairs = [(i, _digest(i)) for i in range(16)]
    data = rs.encode_mark(pairs)
    assert len(data) == 583
    assert data == _ref_mark(pairs)


def test_mark_size_documented_in_architecture():
    text = ARCHITECTURE.read_text(encoding="utf-8")
    m = re.search(r"Một `MARK` 16 khối dài (\d+) byte", text)
    assert m is not None, "architecture mục 4 phải ghi độ dài MARK 16 khối"
    assert int(m.group(1)) == len(rs.encode_mark([(i, _digest(i)) for i in range(16)]))


def test_mark_accepts_maximum_count():
    pairs = [(i, _digest(i)) for i in range(MAX_MARK)]
    data = rs.encode_mark(pairs)
    assert len(data) == 1 + 2 + 36 * MAX_MARK + 4
    assert data == _ref_mark(pairs)


@pytest.mark.parametrize(
    "pairs",
    [
        [],
        [(i, _digest(i)) for i in range(MAX_MARK + 1)],
        [(0, b"\x00" * 31)],
        [(0, b"\x00" * 33)],
        [(-1, _digest(0))],
        [(2**32, _digest(0))],
    ],
    ids=["empty", "65536", "hash31", "hash33", "neg-index", "index-2^32"],
)
def test_mark_rejects_bad_input(pairs: list):
    with pytest.raises(ValueError):
        rs.encode_mark(pairs)


def test_clear_layout():
    assert rs.encode_clear([5, 0, 70000]) == _ref_clear([5, 0, 70000])


@pytest.mark.parametrize(
    "indices", [[], list(range(MAX_MARK + 1)), [-1], [2**32]], ids=["empty", "65536", "neg", "2^32"]
)
def test_clear_rejects_bad_input(indices: list):
    with pytest.raises(ValueError):
        rs.encode_clear(indices)


def test_clear_all_layout():
    assert rs.encode_clear_all() == _ref_clear_all()
    assert len(rs.encode_clear_all()) == 5


@pytest.mark.parametrize("name", ["video.mp4", "Ảnh chụp.jpg", "a", "x" * 255, "é" * 127, "😀" * 63])
def test_target_and_reserved_layout(name: str):
    raw = name.encode()
    assert rs.encode_target(name) == _ref_name(TARGET, raw)
    assert rs.encode_reserved(name) == _ref_name(RESERVED, raw)


@pytest.mark.parametrize(
    "name",
    [
        "",  # len = 0
        "x" * 256,  # 256 byte
        "é" * 128,  # 256 byte UTF-8
        "../evil",  # đổi qua sanitize_filename
        "a/b",
        "C:\\x",
        "CON",
        "trailing.",
        ".teleport",
        "e\u0301",  # chưa NFC
        "bad\x00",
    ],
)
def test_target_and_reserved_reject_names_changed_by_sanitizer_or_out_of_range(name: str):
    for encode in (rs.encode_target, rs.encode_reserved):
        with pytest.raises(ValueError):
            encode(name)


# =================================================================================================
# Đọc lại
# =================================================================================================


def test_replay_empty_log_after_header():
    result = _replay(10, rs.encode_header(TID, 0, 10 * BLOCK_SIZE))
    assert _bit_set(result.bits, 10) == set()
    assert result.target is None
    assert not result.reserved
    assert result.good_end == HEADER_LEN
    assert result.truncated is False
    assert result.log_length == HEADER_LEN


def test_replay_later_mark_wins_and_clear_semantics():
    n = 8
    h = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    data = (
        h
        + rs.encode_mark([(0, _digest(0)), (1, _digest(1)), (2, _digest(2))])
        + rs.encode_mark([(1, _digest(1, 9))])
        + rs.encode_clear([2])
        + rs.encode_mark([(7, _digest(7))])
    )
    result = _replay(n, data)
    bits, leaves, _, _ = _snap(result, n)
    assert bits == {0, 1, 7}
    assert leaves[1] == _digest(1, 9)
    assert result.good_end == len(data)
    assert result.truncated is False

    after_clear_all = _replay(n, data + rs.encode_clear_all())
    assert _bit_set(after_clear_all.bits, n) == set()


def test_replay_target_forgets_previous_reserved_and_reserved_needs_same_name():
    h = rs.encode_header(TID, 0, BLOCK_SIZE)
    a, b = "a.txt", "b.txt"
    only_target = _replay(1, h + rs.encode_target(a))
    assert (only_target.target, bool(only_target.reserved)) == (a, False)

    both = _replay(1, h + rs.encode_target(a) + rs.encode_reserved(a))
    assert (both.target, bool(both.reserved)) == (a, True)

    wrong = _replay(1, h + rs.encode_target(a) + rs.encode_reserved(b))
    assert (wrong.target, bool(wrong.reserved)) == (a, False)
    assert wrong.truncated is False, "RESERVED khác tên bị bỏ qua, không phải bản ghi hỏng"

    retarget = _replay(1, h + rs.encode_target(a) + rs.encode_reserved(a) + rs.encode_target(b))
    assert (retarget.target, bool(retarget.reserved)) == (b, False)

    reserved_first = _replay(1, h + rs.encode_reserved(a))
    assert reserved_first.target is None
    assert not reserved_first.reserved


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        (_ref_record(MARK, struct.pack("!H", 0)), "MARK n = 0, crc đúng"),
        (_ref_record(CLEAR, struct.pack("!H", 0)), "CLEAR n = 0, crc đúng"),
        (_ref_mark([(4, _digest(4))]), "MARK index = n_blocks"),
        (_ref_clear([4]), "CLEAR index = n_blocks"),
        (_ref_record(0x06, b""), "loại lạ 0x06"),
        (_ref_record(0x00, b""), "loại 0x00"),
        (_ref_record(0xFF, b"\x00\x00"), "loại 0xFF"),
        (_ref_mark([(0, _digest(0))])[:-1] + b"\x00", "MARK sai crc"),
        (_ref_name(TARGET, b""), "TARGET len = 0"),
        (_ref_name(TARGET, b"x" * 256), "TARGET len = 256"),
        (_ref_name(TARGET, b"\xff\xfe"), "TARGET không phải UTF-8"),
        (_ref_name(TARGET, b"\xed\xa0\x80"), "TARGET surrogate UTF-8"),
        (_ref_name(TARGET, b"../x"), "TARGET đổi qua bộ làm sạch"),
        (_ref_name(TARGET, b"CON"), "TARGET tên thiết bị Windows"),
        (_ref_name(TARGET, "e\u0301".encode()), "TARGET chưa NFC"),
        (_ref_name(RESERVED, b"\xff"), "RESERVED không phải UTF-8"),
        (_ref_name(RESERVED, b"a/b"), "RESERVED đổi qua bộ làm sạch"),
        (_ref_mark([(0, _digest(0))])[:-2], "MARK cụt"),
        (b"\x01", "chỉ có byte loại"),
    ],
    ids=_case_id,
)
def test_replay_stops_at_first_bad_record_and_drops_the_rest(bad: bytes, why: str):
    n = 4
    h = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    good = rs.encode_mark([(0, _digest(0))]) + rs.encode_target("ok.bin")
    tail = rs.encode_mark([(1, _digest(1))]) + rs.encode_clear_all()
    data = h + good + bad + tail
    result = _replay(n, data)
    assert result.good_end == len(h + good), why
    assert result.truncated is True, why
    bits, leaves, target, reserved = _snap(result, n)
    assert bits == {0}, why
    assert leaves[0] == _digest(0)
    assert target == "ok.bin"
    assert reserved is False


def test_replay_max_size_mark_with_many_blocks():
    n = MAX_MARK
    h = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    data = h + rs.encode_mark([(i, _digest(i)) for i in range(n)])
    result = _replay(n, data, chunks=[1 << 16])
    assert _bit_set(result.bits, n) == set(range(n))
    assert result.good_end == len(data)


def test_replay_zero_block_file_rejects_any_mark():
    h = rs.encode_header(TID, 0, 0)
    data = h + rs.encode_target("empty.txt") + rs.encode_mark([(0, _digest(0))])
    result = _replay(0, data)
    assert _bit_set(result.bits, 0) == set()
    assert result.target == "empty.txt"
    assert result.good_end == len(h + rs.encode_target("empty.txt"))


@SETTINGS
@given(logs(), st.lists(st.integers(1, 97), min_size=1, max_size=5))
def test_replay_roundtrip_matches_reference(log: tuple[int, list[bytes]], chunks: list[int]):
    n, records = log
    body = b"".join(records)
    data = rs.encode_header(TID, 0, n * BLOCK_SIZE) + body
    result = _replay(n, data, chunks=chunks)
    model = _ref_replay(n, body)
    assert model.good_end == len(body)  # bộ sinh chỉ tạo bản ghi hợp lệ
    assert _snap(result, n) == model.snapshot()
    assert result.name_records == model.name_records
    assert result.good_end == len(data)
    assert result.log_length == len(data)
    assert result.truncated is False


@SETTINGS
@given(logs(), st.data())
def test_replay_cut_at_any_byte_stops_cleanly(log: tuple[int, list[bytes]], data: st.DataObject):
    n, records = log
    body = b"".join(records)
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    cut = data.draw(st.integers(HEADER_LEN, len(header) + len(body)))
    fed = (header + body)[:cut]
    result = _replay(n, fed, chunks=data.draw(st.lists(st.integers(1, 64), min_size=1, max_size=3)))
    model = _ref_replay(n, fed[HEADER_LEN:])
    assert _snap(result, n) == model.snapshot()
    assert result.name_records == model.name_records
    assert result.good_end == HEADER_LEN + model.good_end
    assert result.good_end <= cut
    assert result.truncated is (result.good_end < cut)
    full = _ref_replay(n, body)
    bits = _bit_set(result.bits, n)
    ever_marked = {i for rec in records if rec[0] == MARK for i, _ in _ref_parse_one(rec, 0, n)[1][1]}
    assert bits <= ever_marked, "bit chỉ bật cho khối đã có MARK"
    assert result.good_end in {HEADER_LEN, *(HEADER_LEN + e for e in full.record_ends)}


@SETTINGS
@given(logs(), st.data())
def test_replay_bit_flip_in_records_stops_at_flipped_record(
    log: tuple[int, list[bytes]], data: st.DataObject
):
    n, records = log
    assume(records)
    body = bytearray(b"".join(records))
    pos = data.draw(st.integers(0, len(body) - 1))
    body[pos] ^= 1 << data.draw(st.integers(0, 7))
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    fed = header + bytes(body)
    result = _replay(n, fed, chunks=data.draw(st.lists(st.integers(1, 64), min_size=1, max_size=3)))
    # Bản ghi chứa byte bị lật là bản ghi hỏng đầu tiên (crc32 bắt mọi lỗi 1 bit).
    starts = [0]
    for rec in records:
        starts.append(starts[-1] + len(rec))
    k = max(i for i, s in enumerate(starts[:-1]) if s <= pos)
    expected = _ref_replay(n, b"".join(records[:k]))
    assert _snap(result, n) == expected.snapshot()
    assert result.name_records == expected.name_records
    assert result.good_end == HEADER_LEN + starts[k]
    assert result.good_end <= len(fed)
    assert result.truncated is True


@SETTINGS
@given(st.integers(0, HEADER_LEN - 1), st.integers(0, 7), st.booleans())
def test_replay_damaged_header_raises_state_error_or_stops_cleanly(pos: int, bit: int, cut: bool):
    n = 4
    good = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    if cut:
        fed = good[:pos]
    else:
        flipped = bytearray(good)
        flipped[pos] ^= 1 << bit
        fed = bytes(flipped) + rs.encode_mark([(0, _digest(0))])
    try:
        result = _replay(n, fed)
    except rs.StateError:
        return
    # Không ném thì phải là “chưa có trạng thái”: không bit, không tên đích, cắt về ≤ độ dài.
    assert _bit_set(result.bits, n) == set()
    assert result.target is None
    assert result.good_end <= len(fed)


@SETTINGS
@given(st.integers(0, 64), st.binary(max_size=400), st.lists(st.integers(1, 50), min_size=1, max_size=3))
def test_replay_fuzz_after_header_matches_reference(n: int, garbage: bytes, chunks: list[int]):
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    try:
        result = _replay(n, header + garbage, chunks=chunks)
    except rs.StateError:
        pytest.fail("header đúng thì phần sau là bản ghi: hỏng chỉ làm dừng đọc, không ném")
    model = _ref_replay(n, garbage)
    assert _snap(result, n) == model.snapshot()
    assert result.name_records == model.name_records
    assert result.good_end == HEADER_LEN + model.good_end <= len(header) + len(garbage)
    assert all(0 <= i < n for i in _bit_set(result.bits, n))


@SETTINGS
@given(st.binary(max_size=200), st.integers(0, 64))
def test_replay_fuzz_whole_file_only_state_error(data: bytes, n: int):
    try:
        result = _replay(n, data)
    except rs.StateError:
        return
    assert result.good_end <= len(data)
    assert all(0 <= i < n for i in _bit_set(result.bits, n))


def _structured_records() -> st.SearchStrategy[bytes]:
    """Bản ghi gần đúng: crc đúng, nhưng loại / n / index / tên tùy ý (đào sâu hơn bytes ngẫu nhiên)."""
    raw_name = st.one_of(names.map(str.encode), st.binary(max_size=8))
    return st.one_of(
        st.lists(st.tuples(st.integers(0, 80), digests), max_size=4).map(_ref_mark),
        st.lists(st.integers(0, 80), max_size=4).map(_ref_clear),
        st.just(_ref_clear_all()),
        raw_name.map(lambda r: _ref_name(TARGET, r)),
        raw_name.map(lambda r: _ref_name(RESERVED, r)),
        st.tuples(st.integers(0, 255), st.binary(max_size=6)).map(lambda t: _ref_record(*t)),
        st.binary(max_size=6),
    )


@SETTINGS
@given(st.integers(0, 64), st.lists(_structured_records(), max_size=10))
def test_replay_structured_fuzz_matches_reference(n: int, records: list[bytes]):
    body = b"".join(records)
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    result = _replay(n, header + body)
    model = _ref_replay(n, body)
    assert _snap(result, n) == model.snapshot()
    assert result.name_records == model.name_records
    assert result.good_end == HEADER_LEN + model.good_end
    assert result.truncated is (model.good_end < len(body))


# --- Trần đọc `max_length` -------------------------------------------------------------------------


def _max_compact(n: int) -> int:
    """Độ dài gọn lớn nhất của file `n` khối theo luật gộp (MARK ≤ 4096 khối), kể cả header."""
    marks = -(-n // 4096)
    name_record = 1 + 2 + MAX_NAME + 4
    return HEADER_LEN + 2 * name_record + 7 * marks + 36 * n


_MAX_RECORD = 1 + 2 + 36 * MAX_MARK + 4


@pytest.mark.parametrize("n", [0, 1, 16, 4095, 4096, 4097, 100_000, 4_194_304])
def test_max_state_length_follows_formula(n: int):
    """2 × độ dài gọn lớn nhất + 64 KiB + một MARK 65 535 khối (spec 2.2). Cho phép lệch đúng
    phần header (2 × 41) tùy cách tính gọn có header hay không, không hơn."""
    upper = 2 * _max_compact(n) + KIB64 + _MAX_RECORD
    lower = upper - 2 * HEADER_LEN
    assert lower <= rs.max_state_length(n) <= upper


def test_max_state_length_is_monotonic():
    values = [rs.max_state_length(n) for n in range(0, 10_000, 97)]
    assert values == sorted(values)


def test_legit_log_just_below_compaction_plus_one_record_is_not_dropped():
    """Nhật ký thật dài nhất (vừa chưa tới ngưỡng gộp, rồi nối thêm một bản ghi) không bị coi là rác."""
    n = 32
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    one = rs.encode_mark([(0, _digest(0))])
    data = header
    # Đánh dấu lại khối 0 nhiều lần: gọn chỉ còn 1 MARK, nhật ký thì dài ra.
    while len(data) + len(one) <= 2 * (HEADER_LEN + len(one)) + KIB64:
        data += one
    assert not rs.needs_compaction(len(data), HEADER_LEN + len(one))
    data += rs.encode_mark([(i, _digest(i)) for i in range(n)])
    result = _replay(n, data)
    assert result.good_end == len(data)
    assert _bit_set(result.bits, n) == set(range(n))


@SETTINGS
@given(logs(max_blocks=20), st.data())
def test_replay_drops_bytes_beyond_max_length(log: tuple[int, list[bytes]], data: st.DataObject):
    n, records = log
    body = b"".join(records) + rs.encode_mark([(0, _digest(0))]) * 4
    full = rs.encode_header(TID, 0, n * BLOCK_SIZE) + body
    limit = data.draw(st.integers(HEADER_LEN, len(full) - 1))
    result = _replay(
        n, full, max_length=limit, chunks=data.draw(st.lists(st.integers(1, 64), min_size=1, max_size=3))
    )
    model = _ref_replay(n, full[HEADER_LEN:limit])
    assert result.good_end <= limit
    assert result.good_end == HEADER_LEN + model.good_end
    assert _snap(result, n) == model.snapshot()
    assert result.name_records == model.name_records
    assert result.truncated is True


def test_replay_feed_returns_false_once_over_max_length():
    n = 4
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    mark = rs.encode_mark([(1, _digest(1))])
    replay = rs.StateReplay(n, max_length=len(header) + len(mark))
    if replay.feed(header + mark):  # đúng bằng trần: chưa vượt
        assert replay.feed(rs.encode_mark([(2, _digest(2))])) is False
    result = replay.finish()
    assert _bit_set(result.bits, n) == {1}
    assert result.good_end == len(header) + len(mark)
    assert result.truncated is True


# =================================================================================================
# Gộp
# =================================================================================================


@pytest.mark.parametrize(
    ("log_length", "compact_length", "expected"),
    [
        (2 * 1000 + KIB64, 1000, False),
        (2 * 1000 + KIB64 + 1, 1000, True),
        (0, 0, False),
        (KIB64, 0, False),
        (KIB64 + 1, 0, True),
    ],
)
def test_needs_compaction_threshold(log_length: int, compact_length: int, expected: bool):
    assert rs.needs_compaction(log_length, compact_length) is expected


@SETTINGS
@given(logs(), st.lists(st.integers(1, 97), min_size=1, max_size=3))
def test_compact_replays_to_same_state_and_length_is_exact(log: tuple[int, list[bytes]], chunks: list[int]):
    n, records = log
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    original = _replay(n, header + b"".join(records), chunks=chunks)
    compact = _compact_bytes(original)
    compact_file = header + b"".join(compact)
    assert original.compact_length == len(compact_file), "compact_length = độ dài thật của bản gọn"

    again = _replay(n, compact_file, chunks=chunks)
    assert _snap(again, n) == _snap(original, n)
    assert again.good_end == len(compact_file)
    assert again.truncated is False
    assert again.compact_length == len(compact_file)
    assert again.name_records == (original.target is not None) + original.reserved <= 2

    ops = _ref_ops(n, b"".join(compact))
    marked = [i for op in ops if op[0] == "mark" for i, _ in op[1]]
    assert sorted(marked) == sorted(_bit_set(original.bits, n)), (
        "bản gọn chỉ MARK đúng các khối đang bật, mỗi khối một lần"
    )
    kinds = [op[0] for op in ops]
    assert "clear" not in kinds and "clear_all" not in kinds
    expected_names = [("target", original.target)] if original.target is not None else []
    if original.reserved:
        expected_names.append(("reserved", original.target))
    assert [op for op in ops if op[0] in ("target", "reserved")] == expected_names
    assert all(len(op[1]) <= 4096 for op in ops if op[0] == "mark")


@SETTINGS
@given(logs(max_blocks=30), st.integers(1, 5))
def test_compact_respects_small_max_mark(log: tuple[int, list[bytes]], max_mark: int):
    n, records = log
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    original = _replay(n, header + b"".join(records))
    compact = _compact_bytes(original, max_mark=max_mark)
    ops = _ref_ops(n, b"".join(compact))
    assert all(1 <= len(op[1]) <= max_mark for op in ops if op[0] == "mark")
    assert _snap(_replay(n, header + b"".join(compact)), n) == _snap(original, n)


def test_compact_splits_large_bitfield_into_marks_of_4096():
    n = 4096 * 2 + 5
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    data = header + b"".join(
        rs.encode_mark([(i, _digest(i)) for i in range(start, min(start + 1000, n))])
        for start in range(0, n, 1000)
    )
    original = _replay(n, data)
    compact = _compact_bytes(original)
    ops = _ref_ops(n, b"".join(compact))
    assert sorted(len(op[1]) for op in ops) == [5, 4096, 4096]
    assert original.compact_length == HEADER_LEN + sum(map(len, compact)) == HEADER_LEN + 3 * 7 + 36 * n


def test_compact_of_empty_state_is_header_only():
    header = rs.encode_header(TID, 0, 3 * BLOCK_SIZE)
    data = header + rs.encode_mark([(0, _digest(0))]) + rs.encode_clear_all()
    result = _replay(3, data)
    assert _compact_bytes(result) == []
    assert result.compact_length == HEADER_LEN
    assert result.log_length == len(data)


def test_compact_writes_target_before_reserved():
    header = rs.encode_header(TID, 0, 2 * BLOCK_SIZE)
    data = (
        header + rs.encode_target("x.bin") + rs.encode_mark([(1, _digest(1))]) + rs.encode_reserved("x.bin")
    )
    result = _replay(2, data)
    compact = _compact_bytes(result)
    kinds = [rec[0] for rec in compact]
    assert kinds.index(TARGET) < kinds.index(RESERVED)
    assert _snap(_replay(2, header + b"".join(compact)), 2) == (
        frozenset({1}),
        {1: _digest(1)},
        "x.bin",
        True,
    )


# --- `compact_length_for` (spec-p3-3a mục 8.1, B5) ------------------------------------------------
# Hàm công khai, thuần: độ dài bản gọn (kể cả header) khi có `marked` khối bật, `TARGET` cuối `target`
# và `RESERVED` khớp tên `reserved` (None = không có). Phải bằng độ dài thật mà `compact_records` sinh ra.


@SETTINGS
@given(logs(), st.lists(st.integers(1, 97), min_size=1, max_size=3))
def test_compact_length_for_equals_real_compact_of_replayed_log(
    log: tuple[int, list[bytes]], chunks: list[int]
):
    n, records = log
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    result = _replay(n, header + b"".join(records), chunks=chunks)
    compact_file = header + b"".join(_compact_bytes(result))
    marked = len(_bit_set(result.bits, n))
    reserved = result.target if result.reserved else None
    assert rs.compact_length_for(marked, result.target, reserved) == len(compact_file)


@SETTINGS
@given(
    st.integers(0, 3 * 4096 + 7),
    st.one_of(st.none(), names, st.just("ệ" * 85)),
    st.booleans(),
)
def test_compact_length_for_matches_compact_records_across_mark_split(
    marked: int, target: str | None, reserved: bool
):
    # Số khối vượt qua ranh giới 4096 khối mỗi `MARK` của bản gọn; tên dài tới 255 byte.
    reserved = reserved and target is not None
    n = max(marked, 1)
    bits = Bitfield(n)
    for i in range(marked):
        bits.set(i)
    leaves = bytes(32 * n)
    compact = b"".join(rs.compact_records(bits, leaves, target, reserved))
    expected = len(rs.encode_header(TID, 0, n * BLOCK_SIZE)) + len(compact)
    assert rs.compact_length_for(marked, target, target if reserved else None) == expected


def test_compact_length_for_of_empty_state_is_header_only():
    assert rs.compact_length_for(0, None, None) == HEADER_LEN


def test_compact_length_for_is_public_and_pure():
    assert not rs.compact_length_for.__name__.startswith("_")
    assert rs.compact_length_for(16, "a.txt", "a.txt") == rs.compact_length_for(16, "a.txt", "a.txt")
    # 16 khối, `TARGET` + `RESERVED` tên 5 byte: 41 + 2 × (7 + 5) + 583.
    assert rs.compact_length_for(16, "a.txt", "a.txt") == HEADER_LEN + 2 * 12 + 583
    assert rs.compact_length_for(16, "a.txt", None) == HEADER_LEN + 12 + 583


# =================================================================================================
# Phục hồi tên đích (architecture mục 4, bảng)
# =================================================================================================


def _state_of(kind: str) -> Any:
    """ReplayResult thật cho từng loại `.state`: lấy `target`/`reserved` đúng kiểu module dùng."""
    header = rs.encode_header(TID, 0, BLOCK_SIZE)
    records = {
        "none": b"",
        "target": rs.encode_target("đích.bin"),
        "target+reserved": rs.encode_target("đích.bin") + rs.encode_reserved("đích.bin"),
        "target+other-reserved": rs.encode_target("đích.bin") + rs.encode_reserved("khác.bin"),
        "reserved-then-target": rs.encode_target("cũ.bin")
        + rs.encode_reserved("cũ.bin")
        + rs.encode_target("đích.bin"),
    }[kind]
    return _replay(1, header + records)


def _member(*names: str) -> Any:
    """Member đầu tiên có trong `TargetRecovery`: spec không chốt tên, chỉ chốt ý nghĩa từng dòng."""
    for name in names:
        if name in rs.TargetRecovery.__members__:
            return rs.TargetRecovery[name]
    raise AssertionError(f"TargetRecovery thiếu kết quả cho {names}")


# Ý nghĩa → tên có thể có. “Bỏ, nhận lại từ đầu” (mất `.part`) được phép là member riêng.
_OUTCOMES = {
    "NEW": ("NORMAL", "NEW_NAME"),  # không đụng file đích, tới bước đổi tên thì chọn tên mới
    "VERIFY": ("VERIFY_TARGET",),  # đọc lại file đích, khớp → làm tiếp từ bước 6
    "REUSE": ("REUSE_PLACEHOLDER",),  # dùng lại file 0 byte làm file giữ chỗ, đọc lại `.part`
    "REUSE_RESTART": ("RESTART_REUSE", "REUSE_PLACEHOLDER"),  # dùng lại file 0 byte, nhận từ đầu
    "NEW_RESTART": ("RESTART", "NORMAL", "NEW_NAME"),  # chọn tên mới, mất `.part` → nhận từ đầu
}


def _expected_recovery(
    has_target: bool, reserved: bool, target_size: int | None, part_exists: bool, size: int
) -> str:
    """Bảng “Phục hồi tên đích”, xét từ trên xuống, dòng đầu tiên khớp."""
    if not has_target or not reserved:
        return "NEW"  # dòng 1, 2
    if target_size == size and not part_exists:
        return "VERIFY"  # dòng 3 (đứng trước dòng 5: file rỗng có size = 0)
    if target_size == 0:
        return "REUSE" if part_exists else "REUSE_RESTART"  # dòng 4, 5
    return "NEW" if part_exists else "NEW_RESTART"  # dòng 6


def test_target_recovery_is_str_enum_with_table_outcomes():
    assert issubclass(rs.TargetRecovery, StrEnum)
    for names in _OUTCOMES.values():
        _member(*names)
    assert len(set(rs.TargetRecovery)) == len(rs.TargetRecovery.__members__)


@pytest.mark.parametrize(
    ("state", "target_size", "part_exists", "size", "expected"),
    [
        # Dòng 1: không có TARGET
        ("none", None, True, 10, "NEW"),
        ("none", 10, False, 10, "NEW"),
        ("none", 0, True, 10, "NEW"),
        # Dòng 2: TARGET không có RESERVED khớp, file mang tên đó bất kỳ
        ("target", 10, False, 10, "NEW"),
        ("target", 0, True, 10, "NEW"),
        ("target", 0, False, 0, "NEW"),
        ("target+other-reserved", 10, False, 10, "NEW"),
        ("target+other-reserved", 0, True, 10, "NEW"),
        ("reserved-then-target", 10, False, 10, "NEW"),
        ("reserved-then-target", 0, True, 10, "NEW"),
        # Dòng 3: đúng size, không còn .part → kiểm file đích
        ("target+reserved", 10, False, 10, "VERIFY"),
        ("target+reserved", 0, False, 0, "VERIFY"),  # file rỗng: dòng 3 đứng trước dòng 5
        # Dòng 4: 0 byte, còn .part → dùng lại làm file giữ chỗ
        ("target+reserved", 0, True, 10, "REUSE"),
        ("target+reserved", 0, True, 0, "REUSE"),
        # Dòng 5: 0 byte, không còn .part → của máy này, nhận lại từ đầu, giữ tên
        ("target+reserved", 0, False, 10, "REUSE_RESTART"),
        # Dòng 6: mọi trường hợp còn lại
        ("target+reserved", None, True, 10, "NEW"),
        ("target+reserved", None, False, 10, "NEW_RESTART"),
        ("target+reserved", 10, True, 10, "NEW"),  # đúng size nhưng còn .part
        ("target+reserved", 5, False, 10, "NEW_RESTART"),
        ("target+reserved", 11, False, 10, "NEW_RESTART"),
        ("target+reserved", 5, True, 10, "NEW"),
        ("target+reserved", 7, False, 0, "NEW_RESTART"),
    ],
)
def test_recover_target_table_rows(
    state: str, target_size: int | None, part_exists: bool, size: int, expected: str
):
    replay = _state_of(state)
    got = rs.recover_target(
        target=replay.target,
        reserved=replay.reserved,
        target_size=target_size,
        part_exists=part_exists,
        size=size,
    )
    assert got is _member(*_OUTCOMES[expected])


@SETTINGS
@given(
    st.sampled_from(["none", "target", "target+reserved", "target+other-reserved", "reserved-then-target"]),
    st.one_of(st.none(), st.integers(0, 3), st.integers(0, MAX_FILE_SIZE)),
    st.booleans(),
    st.one_of(st.integers(0, 3), st.integers(0, MAX_FILE_SIZE)),
)
def test_recover_target_matches_table_everywhere(
    state: str, target_size: int | None, part_exists: bool, size: int
):
    replay = _state_of(state)
    got = rs.recover_target(
        target=replay.target,
        reserved=replay.reserved,
        target_size=target_size,
        part_exists=part_exists,
        size=size,
    )
    expected = _expected_recovery(
        replay.target is not None, bool(replay.reserved), target_size, part_exists, size
    )
    assert got is _member(*_OUTCOMES[expected])


# =================================================================================================
# transfer.json
# =================================================================================================


def _record_dict(**overrides: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "version": 1,
        "receiver": RECEIVER,
        "sender": SENDER,
        "transfer_id": TID,
        "block_size": BLOCK_SIZE,
        "files": [
            {"name": "video.mp4", "size": 5 * BLOCK_SIZE + 3, "mtime": 1_700_000_000_000},
            {"name": "rỗng.txt", "size": 0, "mtime": 0},
        ],
        "verified": 0,
        "done_at": None,
    }
    d.update(overrides)
    return d


def _dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _decode_record(obj: Any, dir_name: str = TID) -> Any:
    return rs.decode_transfer_record(_dumps(obj), dir_name=dir_name)


def test_transfer_record_limits():
    assert rs.MAX_TRANSFER_RECORD == 2 * 1024 * 1024
    assert rs.MAX_TOMBSTONE_FILE == 8 * 1024 * 1024


def test_decode_transfer_record_fields():
    rec = _decode_record(_record_dict())
    assert rec.receiver == RECEIVER
    assert rec.sender == SENDER
    assert isinstance(rec.offer, Offer)
    assert rec.offer == Offer(
        transfer_id=TID,
        block_size=BLOCK_SIZE,
        files=(FileEntry("video.mp4", 5 * BLOCK_SIZE + 3, 1_700_000_000_000), FileEntry("rỗng.txt", 0, 0)),
    )
    assert rec.verified == 0
    assert rec.done_at is None


def test_encode_transfer_record_has_exactly_documented_fields():
    done = _decode_record(_record_dict(verified=2, done_at=STAMP))
    out = json.loads(rs.encode_transfer_record(done).decode("utf-8-sig"))
    assert out == _record_dict(verified=2, done_at=STAMP)


def test_decode_transfer_record_accepts_utf8_bom():
    rec = rs.decode_transfer_record(b"\xef\xbb\xbf" + _dumps(_record_dict()), dir_name=TID)
    assert rec.offer.transfer_id == TID


def test_decode_transfer_record_accepts_partial_verified():
    rec = _decode_record(_record_dict(verified=1))
    assert rec.verified == 1
    assert rec.done_at is None


def test_transfer_record_size_limit_exact_boundary():
    base = _dumps(_record_dict())
    at_limit = base + b" " * (rs.MAX_TRANSFER_RECORD - len(base))
    assert len(at_limit) == rs.MAX_TRANSFER_RECORD
    assert rs.decode_transfer_record(at_limit, dir_name=TID).offer.transfer_id == TID
    with pytest.raises(rs.StateError):
        rs.decode_transfer_record(at_limit + b" ", dir_name=TID)


def test_largest_legit_transfer_record_fits_under_limit():
    """1000 file, tên 255 byte UTF-8 nặng nhất khi escape: bản ghi vẫn trong trần và đọc lại được."""
    heavy = ["😀" * 63 + "abc", "é" * 127 + "a", '"' * 255, "\\" * 255]
    files = [{"name": heavy[i % 4], "size": MAX_FILE_SIZE, "mtime": 2**53 - 1} for i in range(1000)]
    rec = _decode_record(_record_dict(files=files, verified=1000, done_at=STAMP))
    data = rs.encode_transfer_record(rec)
    assert len(data) <= rs.MAX_TRANSFER_RECORD
    assert rs.decode_transfer_record(data, dir_name=TID) == rec


def _drop(key: str) -> dict[str, Any]:
    d = _record_dict()
    del d[key]
    return d


_BAD_RECORDS: list[tuple[str, Any]] = [
    ("version=2", _record_dict(version=2)),
    ("version=0", _record_dict(version=0)),
    ('version="1"', _record_dict(version="1")),
    ("version=true", _record_dict(version=True)),
    ("version=1.0", _record_dict(version=1.0)),
    ("version=null", _record_dict(version=None)),
    ("block_size khác", _record_dict(block_size=4096)),
    ("block_size chuỗi", _record_dict(block_size=str(BLOCK_SIZE))),
    ("verified âm", _record_dict(verified=-1)),
    ("verified > n", _record_dict(verified=3)),
    ("verified > n có done_at", _record_dict(verified=3, done_at=STAMP)),
    ("verified chuỗi", _record_dict(verified="0")),
    ("verified bool", _record_dict(verified=False)),
    ("verified float", _record_dict(verified=0.0)),
    ("done_at khi chưa xong", _record_dict(verified=1, done_at=STAMP)),
    ("done_at khi verified=0", _record_dict(verified=0, done_at=STAMP)),
    ("thiếu done_at khi xong", _record_dict(verified=2, done_at=None)),
    ("done_at sai dạng (có khoảng trắng)", _record_dict(verified=2, done_at="2026-10-27 10:00:00Z")),
    ("done_at có múi giờ", _record_dict(verified=2, done_at="2026-10-27T10:00:00+00:00")),
    ("done_at không Z", _record_dict(verified=2, done_at="2026-10-27T10:00:00")),
    ("done_at phần lẻ giây", _record_dict(verified=2, done_at="2026-10-27T10:00:00.5Z")),
    ("done_at tháng 13", _record_dict(verified=2, done_at="2026-13-01T10:00:00Z")),
    ("done_at số", _record_dict(verified=2, done_at=1_700_000_000)),
    ("done_at chữ số Ả Rập-Ấn", _record_dict(verified=2, done_at="٢٠٢٦-10-27T10:00:00Z")),
    ("receiver hoa", _record_dict(receiver=RECEIVER.upper())),
    ("receiver ngắn", _record_dict(receiver="e" * 31)),
    ("receiver số", _record_dict(receiver=1)),
    ("receiver null", _record_dict(receiver=None)),
    ("sender ngắn", _record_dict(sender="a" * 33)),
    ("sender không hex", _record_dict(sender="g" * 32)),
    ("sender null", _record_dict(sender=None)),
    ("files rỗng", _record_dict(files=[])),
    ("files không phải mảng", _record_dict(files={"name": "a", "size": 0, "mtime": 0})),
    ("files 1001 phần tử", _record_dict(files=[{"name": "a", "size": 0, "mtime": 0}] * 1001)),
    ("file thiếu mtime", _record_dict(files=[{"name": "a", "size": 0}])),
    ("file size âm", _record_dict(files=[{"name": "a", "size": -1, "mtime": 0}])),
    ("file size vượt trần", _record_dict(files=[{"name": "a", "size": MAX_FILE_SIZE + 1, "mtime": 0}])),
    ("file name có ký tự điều khiển", _record_dict(files=[{"name": "a\x00b", "size": 0, "mtime": 0}])),
    ("file name 256 byte", _record_dict(files=[{"name": "x" * 256, "size": 0, "mtime": 0}])),
    ("file name không phải chuỗi", _record_dict(files=[{"name": 7, "size": 0, "mtime": 0}])),
    ("file là chuỗi", _record_dict(files=["a"])),
    ("transfer_id hoa (dir cùng tên)", (_record_dict(transfer_id=TID.upper()), TID.upper())),
    ("transfer_id khác dir_name", (_record_dict(transfer_id=TID2), TID)),
    ("transfer_id ngắn", _record_dict(transfer_id="0123")),
    *[(f"thiếu {key}", _drop(key)) for key in _record_dict()],
    ("gốc là mảng", [_record_dict()]),
    ("gốc là null", None),
    ("gốc là số", 1),
    ("gốc là chuỗi", "x"),
]


@pytest.mark.parametrize(("why", "case"), _BAD_RECORDS, ids=[w for w, _ in _BAD_RECORDS])
def test_decode_transfer_record_rejects_each_wrong_field(why: str, case: Any):
    obj, dir_name = case if isinstance(case, tuple) else (case, TID)
    with pytest.raises(rs.StateError):
        _decode_record(obj, dir_name=dir_name)


def test_transfer_id_matching_dir_name_is_accepted_for_other_ids():
    """Đối chứng của ca “transfer_id khác dir_name”: cùng tid khác thì hợp lệ."""
    assert _decode_record(_record_dict(transfer_id=TID2), dir_name=TID2).offer.transfer_id == TID2


@pytest.mark.parametrize(
    ("why", "data"),
    [
        ("rỗng", b""),
        ("không phải JSON", b"{not json"),
        ("không phải UTF-8", b'{"version": 1, "x": "\xff"}'),
        ("UTF-16", json.dumps(_record_dict()).encode("utf-16")),
        ("lồng sâu", b"[" * 100_000 + b"]" * 100_000),
        ("số khổng lồ", b'{"version": 1' + b"0" * 5000 + b"}"),
        ("NaN", _dumps(_record_dict()).replace(b'"verified": 0', b'"verified": NaN')),
        ("Infinity", _dumps(_record_dict()).replace(b'"verified": 0', b'"verified": 1e400')),
        ("rác sau JSON", _dumps(_record_dict()) + b"x"),
        ("hai BOM", b"\xef\xbb\xbf\xef\xbb\xbf" + _dumps(_record_dict())),
    ],
    ids=_case_id,
)
def test_decode_transfer_record_rejects_malformed_bytes(why: str, data: bytes):
    with pytest.raises(rs.StateError):
        rs.decode_transfer_record(data, dir_name=TID)


valid_file_names = st.text(max_size=30).filter(
    lambda s: re.search("[\x00-\x1f\x7f-\x9f\ud800-\udfff]", s) is None
)
json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
    max_leaves=12,
)


@st.composite
def record_dicts(draw: st.DrawFn) -> dict[str, Any]:
    files = draw(
        st.lists(
            st.fixed_dictionaries(
                {
                    "name": valid_file_names,
                    "size": st.integers(0, MAX_FILE_SIZE),
                    "mtime": st.integers(0, 2**53 - 1),
                }
            ),
            min_size=1,
            max_size=6,
        )
    )
    verified = draw(st.integers(0, len(files)))
    return _record_dict(
        receiver=draw(st.binary(min_size=16, max_size=16)).hex(),
        sender=draw(st.binary(min_size=16, max_size=16)).hex(),
        files=files,
        verified=verified,
        done_at=STAMP if verified == len(files) else None,
    )


@SETTINGS
@given(record_dicts())
def test_transfer_record_roundtrip(d: dict[str, Any]):
    rec = _decode_record(d)
    data = rs.encode_transfer_record(rec)
    assert len(data) <= rs.MAX_TRANSFER_RECORD
    assert rs.decode_transfer_record(data, dir_name=TID) == rec
    assert json.loads(data.decode("utf-8-sig")) == d


@SETTINGS
@given(record_dicts(), st.data())
def test_transfer_record_field_fuzz_only_state_error(d: dict[str, Any], data: st.DataObject):
    key = data.draw(st.sampled_from(sorted(d)))
    d[key] = data.draw(json_values)
    try:
        rec = _decode_record(d)
    except rs.StateError:
        return
    # Nhận thì phải là bản ghi hợp lệ: khứ hồi được.
    assert rs.decode_transfer_record(rs.encode_transfer_record(rec), dir_name=TID) == rec


@SETTINGS
@given(st.one_of(st.binary(max_size=300), json_values.map(_dumps)))
def test_transfer_record_fuzz_only_state_error(data: bytes):
    with contextlib.suppress(rs.StateError):
        rs.decode_transfer_record(data, dir_name=TID)


# =================================================================================================
# tombstones.json
# =================================================================================================


def _entry(**overrides: Any) -> dict[str, Any]:
    e: dict[str, Any] = {
        "device_id": SENDER,
        "direction": "send",
        "transfer_id": TID,
        "reason": "user",
        "unsent": False,
        "at": STAMP,
    }
    e.update(overrides)
    return e


def _tomb(entries: Any, **top: Any) -> bytes:
    obj: dict[str, Any] = {"version": 1, "entries": entries}
    obj.update(top)
    return _dumps(obj)


def _entries_json(records: Any) -> list[dict[str, Any]]:
    return json.loads(rs.encode_tombstones(records).decode("utf-8-sig"))["entries"]


def test_decode_tombstones_fields():
    data = _tomb([_entry(), _entry(direction="receive", transfer_id=TID2, reason="expired", unsent=True)])
    recs = list(rs.decode_tombstones(data))
    assert len(recs) == 2
    first, second = recs
    assert (first.device_id, first.direction, first.transfer_id, first.reason, first.unsent) == (
        SENDER,
        "send",
        TID,
        "user",
        False,
    )
    assert (second.direction, second.transfer_id, second.reason, second.unsent) == (
        "receive",
        TID2,
        "expired",
        True,
    )
    assert _entries_json(recs) == [
        _entry(),
        _entry(direction="receive", transfer_id=TID2, reason="expired", unsent=True),
    ]


def test_encode_tombstones_top_level_shape():
    out = json.loads(rs.encode_tombstones(list(rs.decode_tombstones(_tomb([_entry()])))).decode("utf-8-sig"))
    assert set(out) == {"version", "entries"}
    assert out["version"] == 1


def test_decode_tombstones_accepts_bom_and_empty_list():
    (only,) = rs.decode_tombstones(b"\xef\xbb\xbf" + _tomb([_entry()]))
    assert only.transfer_id == TID
    assert list(rs.decode_tombstones(_tomb([]))) == []


_BAD_ENTRIES: list[tuple[str, Any]] = [
    ("device_id hoa", _entry(device_id=SENDER.upper())),
    ("device_id ngắn", _entry(device_id="a" * 31)),
    ("device_id null", _entry(device_id=None)),
    ("direction lạ", _entry(direction="both")),
    ("direction hoa", _entry(direction="SEND")),
    ("direction số", _entry(direction=1)),
    ("transfer_id ngắn", _entry(transfer_id="0123")),
    ("transfer_id hoa", _entry(transfer_id=TID.upper())),
    ("reason rỗng", _entry(reason="")),
    ("reason hoa", _entry(reason="USER")),
    ("reason quá dài", _entry(reason="a" * 33)),
    ("reason số", _entry(reason=1)),
    ("unsent số", _entry(unsent=0)),
    ("unsent chuỗi", _entry(unsent="false")),
    ("at sai dạng", _entry(at="2026-10-27 10:00:00")),
    ("at có múi giờ", _entry(at="2026-10-27T10:00:00+07:00")),
    ("at tháng 13", _entry(at="2026-13-01T00:00:00Z")),
    ("at số", _entry(at=1_700_000_000)),
    *[(f"thiếu {k}", {kk: vv for kk, vv in _entry().items() if kk != k}) for k in _entry()],
    ("mục là mảng", [1, 2]),
    ("mục là chuỗi", "x"),
    ("mục null", None),
]


@pytest.mark.parametrize(("why", "bad"), _BAD_ENTRIES, ids=[w for w, _ in _BAD_ENTRIES])
def test_decode_tombstones_skips_only_the_bad_entry(why: str, bad: Any):
    good_a = _entry(transfer_id="1" * 32)
    good_b = _entry(transfer_id="2" * 32, direction="receive")
    recs = list(rs.decode_tombstones(_tomb([good_a, bad, good_b])))
    assert [r.transfer_id for r in recs] == ["1" * 32, "2" * 32], why


@pytest.mark.parametrize(
    ("why", "data"),
    [
        ("rỗng", b""),
        ("không phải JSON", b"{"),
        ("không phải UTF-8", b'{"version": 1, "entries": ["\xff"]}'),
        ("version 2", _tomb([_entry()], version=2)),
        ("version chuỗi", _tomb([_entry()], version="1")),
        ("version true", _tomb([_entry()], version=True)),
        ("thiếu version", _dumps({"entries": [_entry()]})),
        ("thiếu entries", _dumps({"version": 1})),
        ("entries không phải mảng", _tomb({"a": 1})),
        ("gốc là mảng", _dumps([_entry()])),
        # Lồng ở `version`, không ở `entries`: từ Python 3.14 `json` giải được mảng lồng 100 000 tầng
        # thay vì báo RecursionError, và trong `entries` nó chỉ là một mục hỏng bị bỏ riêng.
        ("lồng sâu", b'{"version": ' + b"[" * 100_000 + b"]" * 100_000 + b', "entries": []}'),
        ("số khổng lồ", b'{"version": 1' + b"0" * 5000 + b', "entries": []}'),
    ],
    ids=_case_id,
)
def test_decode_tombstones_rejects_broken_file(why: str, data: bytes):
    with pytest.raises(rs.StateError):
        rs.decode_tombstones(data)


def test_decode_tombstones_size_limit_exact_boundary():
    base = _tomb([_entry()])
    at_limit = base + b" " * (rs.MAX_TOMBSTONE_FILE - len(base))
    assert len(list(rs.decode_tombstones(at_limit))) == 1
    with pytest.raises(rs.StateError):
        rs.decode_tombstones(at_limit + b" ")


def _stamp(k: int) -> str:
    """Giờ UTC hợp lệ, tăng dần theo `k` (giây) trong vòng vài trăm ngày."""
    month, day = 1 + k // (28 * 86400) % 12, 1 + k // 86400 % 28
    return f"2026-{month:02d}-{day:02d}T{k // 3600 % 24:02d}:{k // 60 % 60:02d}:{k % 60:02d}Z"


def test_encode_tombstones_over_limit_drops_oldest():
    """Ghi: vượt trần thì bỏ mục cũ nhất (spec 2.2). Mục xếp theo `at` tăng dần."""
    per_file = 20_000
    total = 3 * per_file
    records: list[Any] = []
    for part in range(3):
        entries = [
            _entry(device_id=f"{k:032x}", transfer_id=f"{k:032x}", at=_stamp(k))
            for k in range(part * per_file, (part + 1) * per_file)
        ]
        records.extend(rs.decode_tombstones(_tomb(entries)))
    assert len(records) == total
    one = len(rs.encode_tombstones(records[:2])) - len(rs.encode_tombstones(records[:1]))
    assert one * total > rs.MAX_TOMBSTONE_FILE, "dữ liệu thử phải đủ lớn để vượt trần"
    data = rs.encode_tombstones(records)
    assert len(data) <= rs.MAX_TOMBSTONE_FILE
    kept = list(rs.decode_tombstones(data))
    assert 0 < len(kept) < total
    assert kept == records[total - len(kept) :], "giữ các mục mới nhất, bỏ mục cũ nhất"


@st.composite
def entry_dicts(draw: st.DrawFn) -> dict[str, Any]:
    return _entry(
        device_id=draw(st.binary(min_size=16, max_size=16)).hex(),
        direction=draw(st.sampled_from(["send", "receive"])),
        transfer_id=draw(st.binary(min_size=16, max_size=16)).hex(),
        reason=draw(st.sampled_from(["user", "expired", "error", "source_changed", "unknown_transfer"])),
        unsent=draw(st.booleans()),
        at=_stamp(draw(st.integers(0, 10**7))),
    )


@SETTINGS
@given(st.lists(entry_dicts(), max_size=8))
def test_tombstones_roundtrip(entries: list[dict[str, Any]]):
    recs = list(rs.decode_tombstones(_tomb(entries)))
    assert _entries_json(recs) == entries
    assert list(rs.decode_tombstones(rs.encode_tombstones(recs))) == recs


@SETTINGS
@given(st.lists(entry_dicts(), min_size=1, max_size=4), st.data())
def test_tombstones_entry_field_fuzz_only_skips(entries: list[dict[str, Any]], data: st.DataObject):
    k = data.draw(st.integers(0, len(entries) - 1))
    key = data.draw(st.sampled_from(sorted(entries[k])))
    entries[k][key] = data.draw(json_values)
    try:
        recs = list(rs.decode_tombstones(_tomb(entries)))
    except rs.StateError:
        pytest.fail("mục hỏng phải bị bỏ riêng, không làm hỏng cả file")
    others = [e for i, e in enumerate(entries) if i != k]
    got = _entries_json(recs)
    # Mục bị sửa hoặc bị bỏ riêng, hoặc (giá trị mới vẫn hợp lệ) được giữ nguyên chỗ.
    assert got == others or (len(got) == len(entries) and [g for i, g in enumerate(got) if i != k] == others)


@SETTINGS
@given(st.one_of(st.binary(max_size=300), json_values.map(_dumps)))
def test_tombstones_fuzz_only_state_error(data: bytes):
    with contextlib.suppress(rs.StateError):
        list(rs.decode_tombstones(data))


# =================================================================================================
# Bất biến chung
# =================================================================================================


def test_state_error_is_value_error():
    assert issubclass(rs.StateError, ValueError)


def test_module_does_no_io():
    """Sans-IO: không import module hệ file / tiến trình, không gọi `open`."""
    tree = ast.parse(Path(rs.__file__).read_text(encoding="utf-8"))
    forbidden = {"os", "io", "pathlib", "shutil", "tempfile", "subprocess", "socket", "asyncio"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & forbidden
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in forbidden
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "open"


@pytest.mark.parametrize("decoder", ["transfer", "tombstones"])
@pytest.mark.parametrize("shape", ["arrays", "objects", "inside-entries"])
def test_decoders_reject_deepest_nesting_within_size_cap(decoder: str, shape: str):
    """Lồng sâu hết cỡ mà vẫn vừa trần kích thước: chỉ StateError (không RecursionError, không sập)."""
    cap = rs.MAX_TRANSFER_RECORD if decoder == "transfer" else rs.MAX_TOMBSTONE_FILE
    head = b'{"version": 1, "entries": '
    data = {
        "arrays": b"[" * cap,
        "objects": b'{"a":' * (cap // 5),
        "inside-entries": head + b"[" * (cap - len(head)),
    }[shape]
    assert len(data) <= cap
    with pytest.raises(rs.StateError):
        if decoder == "transfer":
            rs.decode_transfer_record(data, dir_name=TID)
        else:
            rs.decode_tombstones(data)


# =================================================================================================
# Chi phí đọc lại (soat-code: `CLEAR_ALL` 5 byte không được tốn công theo số khối của file)
# =================================================================================================

_BIG_N = MAX_BLOCKS  # file lớn nhất: bitfield 512 KiB, `leaves` 128 MiB
_COST_RECORDS = 400


def _timed_feed(replay: Any, data: bytes, chunk: int = 1 << 16, budget: float | None = None) -> float:
    """Thời gian cấp `data` cho `replay` (không gồm lúc dựng); quá `budget` giây thì fail ngay."""
    start = time.perf_counter()
    for pos in range(0, len(data), chunk):
        if not replay.feed(data[pos : pos + chunk]):
            break
        elapsed = time.perf_counter() - start
        if budget is not None and elapsed > budget:
            pytest.fail(f"đọc lại quá {budget} s sau {pos + chunk} / {len(data)} byte")
    return time.perf_counter() - start


@pytest.mark.parametrize("pattern", ["clear_all", "mark1+clear_all", "mark-many+clear_all"])
def test_clear_all_cost_does_not_grow_with_file_size(pattern: str):
    """Cùng số bản ghi, file `MAX_BLOCKS` khối không được chậm hơn hẳn file 1024 khối.

    So tương đối (cùng máy, cùng lúc) để khỏi phụ thuộc tốc độ máy CI; trần rộng (10× + 1 s).
    Bản xóa cả `leaves` (128 MiB) mỗi `CLEAR_ALL` thì 400 bản ghi tốn hàng chục giây.
    """
    clear_all = rs.encode_clear_all()
    unit = {
        "clear_all": clear_all,
        "mark1+clear_all": rs.encode_mark([(0, _digest(0))]) + clear_all,
        "mark-many+clear_all": rs.encode_mark([(i, _digest(i)) for i in range(0, 1000, 10)]) + clear_all,
    }[pattern]
    body = unit * _COST_RECORDS
    times = {}
    for n in (1024, _BIG_N):
        replay = rs.StateReplay(n, max_length=rs.max_state_length(n))
        assert replay.feed(rs.encode_header(TID, 0, n * BLOCK_SIZE))
        times[n] = _timed_feed(replay, body, budget=60.0)
        result = replay.finish()
        assert result.bits.count() == 0
        assert result.good_end == HEADER_LEN + len(body)
        del replay, result
    assert times[_BIG_N] <= 10 * times[1024] + 1.0, times


@pytest.mark.parametrize("pattern", ["clear_all", "mark1+clear_all"])
def test_clear_all_flood_up_to_max_length_finishes(pattern: str):
    """`.state` dài đúng `max_state_length(n)` toàn `CLEAR_ALL` (hay `MARK` 1 khối + `CLEAR_ALL`).

    n = 65 536 khối (64 GiB): trần đọc khoảng 7 MiB, tức 1,4 triệu `CLEAR_ALL`. Trần 20 s rộng
    cho máy tải nặng; bản xóa 2 MiB `leaves` mỗi bản ghi thì chạy hàng giờ (test dừng sớm).
    """
    n = 65_536
    clear_all = rs.encode_clear_all()
    unit = clear_all if pattern == "clear_all" else rs.encode_mark([(n - 1, _digest(n - 1))]) + clear_all
    limit = rs.max_state_length(n)
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    body = unit * ((limit - HEADER_LEN) // len(unit))
    replay = rs.StateReplay(n, max_length=limit)
    assert replay.feed(header)
    _timed_feed(replay, body, chunk=1 << 20, budget=20.0)
    result = replay.finish()
    assert result.bits.count() == 0
    assert result.good_end == HEADER_LEN + len(body) <= limit
    assert result.truncated is False


@st.composite
def clear_all_heavy_logs(draw: st.DrawFn) -> tuple[int, list[bytes]]:
    """Nhật ký dày `CLEAR_ALL` xen `MARK`, `CLEAR`, đánh dấu lại: soi cách theo dõi khối đã bật."""
    # n nhỏ: danh sách khối vừa bật dễ tràn (xóa cả mảng); n lớn: chỉ tắt các khối đã ghi nhận.
    n = draw(st.integers(1, 200))
    index = st.integers(0, n - 1)
    records: list[bytes] = []
    for _ in range(draw(st.integers(1, 25))):
        kind = draw(st.sampled_from(["mark", "mark", "clear", "clear_all", "clear_all"]))
        if kind == "mark":
            idx = draw(st.lists(index, min_size=1, max_size=6, unique=True))
            records.append(_ref_mark([(i, draw(digests)) for i in idx]))
        elif kind == "clear":
            records.append(_ref_clear(draw(st.lists(index, min_size=1, max_size=4))))
        else:
            records.append(_ref_clear_all())
    return n, records


@SETTINGS
@given(clear_all_heavy_logs(), st.lists(st.integers(1, 64), min_size=1, max_size=3))
def test_clear_all_heavy_logs_match_reference_and_zero_cleared_leaves(
    log: tuple[int, list[bytes]], chunks: list[int]
):
    """Khớp bộ đọc tham chiếu; khối bit 0 có hash 32 byte 0 (docstring của module), kể cả khối
    đã bật trước một `CLEAR_ALL` cũ rồi bị `CLEAR` riêng, hay bật lại sau đó."""
    n, records = log
    body = b"".join(records)
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    result = _replay(n, header + body, chunks=chunks)
    model = _ref_replay(n, body)
    assert _snap(result, n) == model.snapshot()
    assert result.name_records == model.name_records
    bits = _bit_set(result.bits, n)
    for i in range(n):
        if i not in bits:
            assert _leaf(result.leaves, i) == bytes(32), i
    again = _replay(n, header + b"".join(_compact_bytes(result)))
    assert _snap(again, n) == _snap(result, n)


# =================================================================================================
# Trần số bản ghi `TARGET` / `RESERVED` mỗi file
# =================================================================================================


def _name_record_cap() -> int:
    """Hằng trần của module (spec chưa đặt tên): đúng một hằng nguyên `MAX_…(NAME|TARGET)…RECORDS`."""
    found = {
        name: value
        for name, value in vars(rs).items()
        if re.fullmatch(r"MAX_\w*(NAME|TARGET)\w*RECORDS?", name) and type(value) is int
    }
    if len(found) != 1:
        pytest.fail(f"chờ code: cần đúng một hằng trần số bản ghi TARGET/RESERVED, thấy {sorted(found)}")
    return next(iter(found.values()))


def _name_flood(count: int) -> list[bytes]:
    """`count` bản ghi tên: TARGET rồi RESERVED khớp tên, xen kẽ; có `MARK` chen giữa (không tính)."""
    out = []
    for k in range(count):
        name = f"f{k // 2}.bin"
        out.append(rs.encode_target(name) if k % 2 == 0 else rs.encode_reserved(name))
        if k % 7 == 0:
            out.append(rs.encode_mark([(k % 4, _digest(k))]))
    return out


def test_name_records_up_to_cap_are_all_read():
    cap = _name_record_cap()
    n = 4
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    body = b"".join(_name_flood(cap))
    result = _replay(n, header + body, chunks=[1 << 16])
    assert result.good_end == HEADER_LEN + len(body)
    assert result.truncated is False
    assert _snap(result, n) == _ref_replay(n, body).snapshot()


@pytest.mark.parametrize("kind", ["target", "reserved"])
def test_name_record_over_cap_stops_cleanly_at_that_record(kind: str):
    cap = _name_record_cap()
    n = 4
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    within = b"".join(_name_flood(cap))
    last_name = f"f{(cap - 1) // 2}.bin"
    over = rs.encode_target("thừa.bin") if kind == "target" else rs.encode_reserved(last_name)
    tail = rs.encode_mark([(3, _digest(3))]) + rs.encode_target("sau.bin")
    result = _replay(n, header + within + over + tail, chunks=[1 << 16])
    assert result.good_end == HEADER_LEN + len(within), "dừng đúng tại bản ghi tên thứ cap + 1"
    assert result.truncated is True
    assert _snap(result, n) == _ref_replay(n, within).snapshot()


def test_name_record_cap_documented_in_architecture():
    """Trần là luật định dạng `.state`: architecture mục 4 phải ghi đúng con số của hằng."""
    cap = _name_record_cap()
    text = ARCHITECTURE.read_text(encoding="utf-8")
    section = text[text.index("### `<file_index>.state`") :]
    numbers = {int(m.replace(" ", "").replace(" ", "")) for m in re.findall(r"\d[\d  ]*\d|\d", section)}
    assert cap in numbers, f"architecture mục 4 chưa ghi trần {cap} bản ghi TARGET/RESERVED"


# =================================================================================================
# memoryview không liền mạch
# =================================================================================================


def _strided(data: bytes) -> memoryview:
    view = memoryview(bytes(b for byte in data for b in (byte, 0)))[::2]
    assert not view.c_contiguous and bytes(view) == data
    return view


def test_check_header_rejects_non_contiguous_memoryview():
    header = rs.encode_header(TID, 0, BLOCK_SIZE)
    with pytest.raises(rs.StateError):
        rs.check_header(
            _strided(header), transfer_id=TID, file_index=0, size=BLOCK_SIZE, block_size=BLOCK_SIZE
        )


def test_feed_rejects_non_contiguous_memoryview():
    header = rs.encode_header(TID, 0, BLOCK_SIZE)
    replay = rs.StateReplay(1, max_length=rs.max_state_length(1))
    with pytest.raises(rs.StateError):
        replay.feed(_strided(header))


@pytest.mark.parametrize("decoder", ["transfer", "tombstones"])
def test_json_decoders_handle_non_contiguous_memoryview(decoder: str):
    """Không quy định nhận hay từ chối, nhưng chỉ được ra StateError, không TypeError/BufferError."""
    good = _dumps(_record_dict()) if decoder == "transfer" else _tomb([_entry()])
    with contextlib.suppress(rs.StateError):
        if decoder == "transfer":
            assert rs.decode_transfer_record(_strided(good), dir_name=TID).offer.transfer_id == TID
        else:
            assert [r.transfer_id for r in rs.decode_tombstones(_strided(good))] == [TID]


# =================================================================================================
# `ReplayResult.name_records`
# =================================================================================================


def _names_log(count: int) -> bytes:
    """`count` bản ghi tên (TARGET / RESERVED khớp, RESERVED khác tên) xen `MARK`, `CLEAR`, `CLEAR_ALL`."""
    out = []
    for k in range(count):
        name = f"g{k // 3}.bin"
        if k % 3 == 0:
            out.append(rs.encode_target(name))
        elif k % 3 == 1:
            out.append(rs.encode_reserved(name))
        else:
            out.append(rs.encode_reserved(f"khác{k}.bin"))  # bị bỏ qua nhưng vẫn là bản ghi hợp lệ
        out.append(rs.encode_mark([(k % 4, _digest(k))]))
        if k % 5 == 0:
            out.append(rs.encode_clear([k % 4]))
        if k % 11 == 0:
            out.append(rs.encode_clear_all())
    return b"".join(out)


@pytest.mark.parametrize("count", [0, 1, 2, 3, 7, "cap"])
def test_name_records_counts_applied_name_records(count: int | str):
    cap = rs.MAX_NAME_RECORDS
    k = cap if count == "cap" else count
    assert isinstance(k, int)
    n = 4
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    body = _names_log(k)
    result = _replay(n, header + body, chunks=[1 << 16])
    model = _ref_replay(n, body)
    assert model.good_end == len(body)
    assert result.name_records == model.name_records == k
    assert result.good_end == HEADER_LEN + len(body)
    assert result.truncated is False
    assert _snap(result, n) == model.snapshot()


def test_name_records_stops_at_cap_and_excludes_the_record_over_it():
    cap = rs.MAX_NAME_RECORDS
    n = 4
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    within = _names_log(cap)
    data = header + within + rs.encode_mark([(1, _digest(1))]) + rs.encode_target("vượt.bin")
    data += rs.encode_mark([(2, _digest(2))])
    result = _replay(n, data, chunks=[1 << 16])
    model = _ref_replay(n, data[HEADER_LEN:])
    assert result.name_records == model.name_records == cap
    # MARK ngay trước bản ghi vượt trần vẫn được áp; dừng đúng tại bản ghi tên thứ cap + 1.
    assert result.good_end == HEADER_LEN + len(within) + len(rs.encode_mark([(1, _digest(1))]))
    assert result.truncated is True
    assert result.target != "vượt.bin"
    assert _snap(result, n) == model.snapshot()


def test_name_records_zero_for_truncated_or_bad_name_record():
    n = 2
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    bad = _ref_name(TARGET, b"../x")  # crc đúng, tên đổi qua bộ làm sạch → hỏng, không tính
    result = _replay(n, header + bad + rs.encode_target("a.bin"))
    assert result.name_records == 0
    cut = _replay(n, header + rs.encode_target("a.bin")[:-1])
    assert cut.name_records == 0


@SETTINGS
@given(logs(), st.integers(1, 5))
def test_compact_has_at_most_two_name_records(log: tuple[int, list[bytes]], max_mark: int):
    n, records = log
    header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
    original = _replay(n, header + b"".join(records))
    again = _replay(n, header + b"".join(_compact_bytes(original, max_mark=max_mark)))
    assert again.name_records == (original.target is not None) + original.reserved <= 2
    assert again.name_records <= original.name_records


# =================================================================================================
# Trường id luôn là `str` thuần (lớp con của str do bên gọi đưa vào bị đổi về str)
# =================================================================================================


class _SubStr(str):
    """Lớp con của str: có thể mang __eq__/__hash__ riêng, nên không được lọt vào bản ghi."""


def test_transfer_record_coerces_id_fields_to_plain_str():
    offer = Offer(transfer_id=TID, block_size=BLOCK_SIZE, files=(FileEntry("a.bin", 1, 0),))
    rec = rs.TransferRecord(
        receiver=_SubStr(RECEIVER), sender=_SubStr(SENDER), offer=offer, verified=0, done_at=None
    )
    assert type(rec.receiver) is str
    assert type(rec.sender) is str
    assert (rec.receiver, rec.sender) == (RECEIVER, SENDER)
    plain = rs.TransferRecord(receiver=RECEIVER, sender=SENDER, offer=offer, verified=0, done_at=None)
    assert rec == plain
    assert rs.encode_transfer_record(rec) == rs.encode_transfer_record(plain)


def test_tombstone_record_coerces_id_fields_to_plain_str():
    at = datetime.datetime(2026, 10, 27, 10, 0, 0, tzinfo=datetime.UTC)
    rec = rs.TombstoneRecord(
        device_id=_SubStr(SENDER),
        direction=_SubStr("send"),
        transfer_id=_SubStr(TID),
        reason=_SubStr("user"),
        unsent=False,
        at=at,
    )
    for name in ("device_id", "direction", "transfer_id", "reason"):
        assert type(getattr(rec, name)) is str, name
    plain = rs.TombstoneRecord(
        device_id=SENDER, direction="send", transfer_id=TID, reason="user", unsent=False, at=at
    )
    assert rec == plain
    assert rs.encode_tombstones([rec]) == rs.encode_tombstones([plain])


def test_decoded_records_have_plain_str_ids():
    rec = _decode_record(_record_dict())
    assert type(rec.receiver) is str and type(rec.sender) is str
    (tomb,) = rs.decode_tombstones(_tomb([_entry()]))
    for name in ("device_id", "direction", "transfer_id", "reason"):
        assert type(getattr(tomb, name)) is str, name


# =================================================================================================
# V13 / tan-cong T1 mục 7 (spec-p3-5): bên ghi gộp theo ngưỡng → `.state` không bao giờ chạm trần đọc
# =================================================================================================
# Mô hình bên ghi theo architecture mục 4 “Gộp”: sau mỗi lần nối, nếu nhật ký dài hơn 2 × kích thước gọn
# + 64 KiB thì ghi lại bản gọn; kích thước gọn là cận trên `compact_length_for` của số khối đã đánh dấu
# (bit lúc mở + mọi khối trong các `MARK` đã nối, kể cả đánh dấu lại, không quá số khối), đặt lại bằng
# đúng độ dài bản vừa ghi sau mỗi lần gộp. Hệ quả cần chứng minh (docstring `max_state_length`): nhật ký
# dài hơn ngưỡng đó nhiều nhất một bản ghi, nên luôn ≤ `max_state_length(n)` và đọc lại trọn.
# Với n nhỏ trần đọc có thêm một `MARK` 65 535 khối (≈ 2,36 MB), nên chỉ trần đọc thì phải nối hàng
# trăm nghìn bản ghi mới thấy vượt; test hypothesis kiểm thêm ngưỡng chặt hơn của architecture, test
# tất định cuối nối chính bản ghi lớn nhất để chạm thẳng trần đọc.


def _model_apply(bits: set[int], leaves: dict[int, bytes], op: tuple) -> None:
    if op[0] == "mark":
        for i, digest in op[1]:
            bits.add(i)
            leaves[i] = digest
    elif op[0] == "clear":
        bits.difference_update(op[1])
    else:
        bits.clear()


def _read_back_exact(n: int, data: bytes, bits: set[int], leaves: dict[int, bytes]) -> None:
    """`data` đọc lại trọn (không cắt) và khớp mô hình bitfield tham chiếu."""
    result = _replay(n, data, chunks=[4096])
    assert result.good_end == len(data) and result.truncated is False
    assert _snap(result, n) == (frozenset(bits), {i: leaves[i] for i in bits}, None, False)


class _Writer:
    """Bên ghi `.state` theo architecture mục 4 “Gộp”, chỉ dùng hàm của `rs`; giữ cả mô hình tham chiếu."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.header = rs.encode_header(TID, 0, n * BLOCK_SIZE)
        self.log = bytearray(self.header)
        self.marked = 0
        self.bits: set[int] = set()
        self.leaves: dict[int, bytes] = {}
        self.limit = rs.max_state_length(n)
        self.compactions = 0
        self.longest = len(self.log)

    def append(self, op: tuple) -> None:
        if op[0] == "mark":
            record = rs.encode_mark(op[1])
            self.marked = min(self.n, self.marked + len(op[1]))
        elif op[0] == "clear":
            record = rs.encode_clear(op[1])
        else:
            record = rs.encode_clear_all()
        _model_apply(self.bits, self.leaves, op)
        self.log += record
        self.longest = max(self.longest, len(self.log))
        estimate = rs.compact_length_for(self.marked, None, None)
        assert len(self.log) <= self.limit, "nhật ký vượt trần đọc"
        assert len(self.log) <= 2 * estimate + KIB64 + len(record), "chưa gộp khi đã vượt ngưỡng"
        if rs.needs_compaction(len(self.log), estimate):
            self.compact()
        assert len(self.log) <= 2 * rs.compact_length_for(self.marked, None, None) + KIB64

    def compact(self) -> None:
        # Sập ngay trước khi ghi bản gọn: nhật ký dài nhất vẫn đọc lại trọn.
        _read_back_exact(self.n, bytes(self.log), self.bits, self.leaves)
        result = _replay(self.n, bytes(self.log))
        compact = self.header + b"".join(_compact_bytes(result))
        assert len(compact) == result.compact_length <= self.limit, "bản gọn vượt trần đọc"
        _read_back_exact(self.n, compact, self.bits, self.leaves)
        self.log = bytearray(compact)
        self.marked = len(self.bits)
        self.compactions += 1


@st.composite
def _writer_steps(draw: st.DrawFn) -> tuple[int, list[tuple[str, tuple, int]]]:
    """(n, các bước (loại, khối, số lần lặp)). `flip` = `MARK` rồi `CLEAR` cùng khối (đồng bộ lại lặp);
    số lần lặp đôi khi đủ lớn để một mình bước đó ép gộp."""
    n = draw(st.integers(1, 200))
    index = st.integers(0, n - 1)
    steps = []
    for _ in range(draw(st.integers(1, 8))):
        kind = draw(st.sampled_from(["mark", "mark", "clear", "clear_all", "flip"]))
        if kind == "clear_all":
            blocks: tuple = ()
            size = 5
        elif kind == "clear":
            blocks = tuple(draw(st.lists(index, min_size=1, max_size=4, unique=True)))
            size = 7 + 4 * len(blocks)
        else:
            idx = draw(st.lists(index, min_size=1, max_size=min(n, 24), unique=True))
            salt = draw(st.integers(0, 255))
            blocks = tuple((i, _digest(i, salt)) for i in idx)
            size = 7 + 36 * len(blocks) + (7 + 4 * len(blocks) if kind == "flip" else 0)
        if draw(st.integers(0, 3)) == 0:
            reps = draw(st.integers(KIB64 // size, 3 * KIB64 // (2 * size) + 2))
        else:
            reps = draw(st.integers(1, 3))
        steps.append((kind, blocks, reps))
    return n, steps


def _run_writer(n: int, steps: list[tuple[str, tuple, int]]) -> _Writer:
    w = _Writer(n)
    for kind, blocks, reps in steps:
        for _ in range(reps):
            if kind == "flip":
                w.append(("mark", blocks))
                w.append(("clear", tuple(i for i, _ in blocks)))
            else:
                w.append((kind, blocks))
    _read_back_exact(n, bytes(w.log), w.bits, w.leaves)
    return w


@SETTINGS
@given(_writer_steps())
def test_writer_compacting_at_threshold_keeps_log_within_read_ceiling(case: tuple[int, list]):
    n, steps = case
    w = _run_writer(n, steps)
    event(f"số lần gộp: {min(w.compactions, 3)}{'+' if w.compactions > 3 else ''}")


@pytest.mark.parametrize(
    ("n", "steps"),
    [
        pytest.param(1, [("mark", ((0, _digest(0)),), 1), ("clear", (0,), 13_000)], id="clear-1-khoi-lap"),
        pytest.param(1, [("mark", ((0, _digest(0, 1)),), 3_500)], id="mark-1-khoi-lap"),
        pytest.param(5, [("mark", ((4, _digest(4)),), 1), ("clear_all", (), 30_000)], id="clear-all-lap"),
        pytest.param(3, [("flip", ((1, _digest(1)), (2, _digest(2))), 2_000)], id="mark-clear-xen-ke"),
    ],
)
def test_writer_forced_compaction_by_repeated_tiny_records(n: int, steps: list):
    """Các mẫu tan-cong mục 7 dùng để ép gộp: mỗi mẫu phải gộp ít nhất hai lần."""
    w = _run_writer(n, steps)
    assert w.compactions >= 2, w.compactions


def test_writer_appending_largest_record_right_at_threshold_stays_within_read_ceiling():
    """Nhật ký vừa chưa tới ngưỡng gộp rồi nối `MARK` lớn nhất (65 535 khối, chỉ số trùng được phép):
    dài nhất đúng là ngưỡng + một bản ghi lớn nhất, vẫn ≤ trần đọc; gộp xong lại nối tiếp được."""
    n = 1
    w = _Writer(n)
    w.append(("mark", ((0, _digest(0)),)))
    one = len(rs.encode_clear([0]))
    while len(w.log) + one <= 2 * rs.compact_length_for(w.marked, None, None) + KIB64:
        w.append(("clear", (0,)))
    assert w.compactions == 0
    largest = ("mark", ((0, _digest(0, 9)),) * MAX_MARK)
    for _ in range(3):
        w.append(largest)
    assert w.longest > 2 * rs.compact_length_for(1, None, None) + KIB64, "phải vượt ngưỡng đúng một bản ghi"
    assert w.longest <= rs.max_state_length(n)
    assert w.compactions == 3
    _read_back_exact(n, bytes(w.log), w.bits, w.leaves)
