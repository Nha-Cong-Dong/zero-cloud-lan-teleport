"""Test `teleport.core.blocks` theo docs/protocol.md §4.2 và kế hoạch GĐ2 P2-1.

Đặc tả: header khung khối là `struct.pack("!BII32s", type, block_index, length, sha256)`, 41 byte,
`type` = 0x02. Khối cố định 1 MiB; file `size` byte có ⌈size / 1 MiB⌉ khối (file rỗng: 0 khối);
`length` phải đúng bằng độ dài khối đó suy ra từ `size` (khối cuối có thể ngắn hơn); `type` lạ,
`block_index` ngoài phạm vi hay `length` sai → `BlockError` (bên nhận đóng kết nối dữ liệu).

API: `parse_header(data)` chỉ giải mã (độ dài, `type`), `check_block(header, size)` đối chiếu với
kích thước file. Test lỗi đi qua cả hai bước (`_receive`) để không phụ thuộc bước nào bắt lỗi.
"""

import hashlib
import struct

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from teleport.core.blocks import (
    BLOCK_HEADER,
    BLOCK_SIZE,
    BLOCK_TYPE,
    BlockError,
    BlockHeader,
    block_count,
    block_length,
    check_block,
    pack_header,
    parse_header,
)

MiB = 1 << 20
TiB = 1 << 40
DIGEST = hashlib.sha256(b"payload").digest()


def ref_header(index: int, length: int, digest: bytes, block_type: int = 0x02) -> bytes:
    """Header viết độc lập theo §4.2."""
    return struct.pack("!BII32s", block_type, index, length, digest)


def ref_count(size: int) -> int:
    return (size + MiB - 1) // MiB


def ref_length(size: int, index: int) -> int:
    return min(MiB, size - index * MiB)


def _receive(raw: bytes, size: int) -> BlockHeader:
    """Bên nhận: giải mã rồi đối chiếu với kích thước file."""
    header = parse_header(raw)
    check_block(header, size)
    return header


# --- Hằng số -----------------------------------------------------------------------------------


def test_constants_match_spec():
    assert BLOCK_SIZE == 1_048_576
    assert BLOCK_TYPE == 0x02
    assert BLOCK_HEADER.size == 41
    assert BLOCK_HEADER.format in ("!BII32s", b"!BII32s")


def test_block_error_is_value_error():
    assert issubclass(BlockError, ValueError)


# --- block_count / block_length ở biên ---------------------------------------------------------


@pytest.mark.parametrize(
    ("size", "count"),
    [
        (0, 0),
        (1, 1),
        (MiB - 1, 1),
        (MiB, 1),
        (MiB + 1, 2),
        (2 * MiB, 2),
        (10 * 1024 * MiB, 10240),  # 10 GiB
        (4 * TiB - 1, 2**22),
        (4 * TiB, 2**22),
    ],
)
def test_block_count_at_boundaries(size, count):
    assert block_count(size) == count
    assert block_count(size) == ref_count(size)


@pytest.mark.parametrize(
    ("size", "index", "length"),
    [
        (1, 0, 1),
        (MiB - 1, 0, MiB - 1),
        (MiB, 0, MiB),
        (MiB + 1, 0, MiB),
        (MiB + 1, 1, 1),
        (2 * MiB, 1, MiB),
        (4 * TiB, 0, MiB),
        (4 * TiB, 2**22 - 1, MiB),
        (4 * TiB - 1, 2**22 - 1, MiB - 1),
    ],
)
def test_block_length_at_boundaries(size, index, length):
    assert block_length(size, index) == length


@pytest.mark.parametrize(
    ("size", "index"), [(0, 0), (1, 1), (MiB, 1), (MiB + 1, 2), (MiB, -1), (4 * TiB, 2**22)]
)
def test_block_length_index_out_of_range_rejected(size, index):
    with pytest.raises(ValueError):
        block_length(size, index)


@pytest.mark.parametrize("size", [-1, 4 * TiB + 1, 1.0, True, "1", None])
def test_block_count_rejects_invalid_size(size):
    # protocol §3.3: `size` là số nguyên 0…2⁴² (4 TiB).
    with pytest.raises(ValueError):
        block_count(size)


@settings(max_examples=300, deadline=None)
@given(st.integers(min_value=0, max_value=4 * TiB))
def test_property_block_lengths_sum_to_size(size):
    count = block_count(size)
    assert count == ref_count(size)
    if count:
        assert block_length(size, 0) == min(MiB, size)
        assert block_length(size, count - 1) == ref_length(size, count - 1)
        assert (count - 1) * MiB + block_length(size, count - 1) == size
        assert 1 <= block_length(size, count - 1) <= MiB


# --- pack_header / parse_header ----------------------------------------------------------------


def test_pack_header_matches_struct_layout():
    raw = pack_header(5, 1234, DIGEST)
    assert raw == ref_header(5, 1234, DIGEST)
    assert len(raw) == 41
    assert raw[0] == 0x02
    assert raw[1:5] == (5).to_bytes(4, "big")
    assert raw[5:9] == (1234).to_bytes(4, "big")
    assert raw[9:] == DIGEST


def test_parse_header_round_trip():
    header = parse_header(pack_header(7, MiB, DIGEST))
    assert header == BlockHeader(index=7, length=MiB, sha256=DIGEST)
    assert header.index == 7
    assert header.length == MiB
    assert header.sha256 == DIGEST


@pytest.mark.parametrize("wrap", [bytes, bytearray, memoryview], ids=lambda f: f.__name__)
def test_parse_header_accepts_bytes_like(wrap):
    raw = ref_header(3, 100, DIGEST)
    assert parse_header(wrap(raw)) == BlockHeader(index=3, length=100, sha256=DIGEST)


def test_parse_header_reads_independent_encoding():
    # Header dựng bằng struct trong test (không qua pack_header).
    raw = ref_header(2**22 - 1, MiB - 1, b"\xab" * 32)
    assert parse_header(raw) == BlockHeader(index=2**22 - 1, length=MiB - 1, sha256=b"\xab" * 32)


@pytest.mark.parametrize("n", [0, 1, 9, 40, 42, 41 + MiB])
def test_truncated_or_overlong_header_rejected(n):
    raw = (ref_header(0, 1, DIGEST) * 2)[:n] if n <= 82 else bytes(n)
    with pytest.raises(BlockError):
        parse_header(raw)


@pytest.mark.parametrize("block_type", [0x00, 0x01, 0x03, 0x20, 0x82, 0xFF])
def test_unknown_type_rejected(block_type):
    with pytest.raises(BlockError):
        _receive(ref_header(0, 1, DIGEST, block_type), 1)


@pytest.mark.parametrize(
    ("index", "length", "digest"),
    [
        (-1, 1, DIGEST),
        (2**32, 1, DIGEST),
        (0, 0, DIGEST),
        (0, MiB + 1, DIGEST),
        (0, 1, DIGEST[:31]),
        (0, 1, DIGEST + b"\x00"),
    ],
)
def test_pack_header_rejects_invalid_arguments(index, length, digest):
    with pytest.raises(ValueError):
        pack_header(index, length, digest)


@settings(max_examples=300, deadline=None)
@given(
    st.integers(min_value=0, max_value=2**32 - 1),
    st.integers(min_value=1, max_value=MiB),
    st.binary(min_size=32, max_size=32),
)
def test_property_pack_parse_round_trip(index, length, digest):
    raw = pack_header(index, length, digest)
    assert raw == ref_header(index, length, digest)
    assert parse_header(raw) == BlockHeader(index=index, length=length, sha256=digest)


# --- check_block: đối chiếu với kích thước file ------------------------------------------------


@pytest.mark.parametrize(
    ("size", "index"),
    [(1, 0), (MiB, 0), (MiB + 1, 0), (MiB + 1, 1), (5 * MiB - 3, 4), (4 * TiB, 2**22 - 1)],
)
def test_valid_block_accepted(size, index):
    header = _receive(ref_header(index, ref_length(size, index), DIGEST), size)
    assert header.index == index


@pytest.mark.parametrize(
    ("size", "index"),
    [(0, 0), (1, 1), (MiB, 1), (MiB + 1, 2), (4 * TiB, 2**22), (MiB, 2**32 - 1)],
)
def test_block_index_out_of_range_rejected(size, index):
    with pytest.raises(BlockError):
        _receive(ref_header(index, 1, DIGEST), size)


@pytest.mark.parametrize(
    ("size", "index", "length"),
    [
        (1, 0, 0),
        (1, 0, 2),
        (MiB, 0, MiB - 1),
        (MiB, 0, MiB + 1),
        (MiB + 1, 0, 1),  # khối không phải cuối phải đủ 1 MiB
        (MiB + 1, 1, MiB),  # khối cuối phải đúng phần còn lại
        (MiB + 1, 1, 2),
        (MiB + 1, 1, 0),
        (2 * MiB, 0, 2**32 - 1),
    ],
)
def test_wrong_length_rejected(size, index, length):
    with pytest.raises(BlockError):
        _receive(ref_header(index, length, DIGEST), size)


def test_empty_file_accepts_no_block():
    with pytest.raises(BlockError):
        _receive(ref_header(0, 1, DIGEST), 0)


@settings(max_examples=300, deadline=None)
@given(st.integers(min_value=1, max_value=4 * TiB), st.data())
def test_property_check_block_accepts_iff_index_and_length_match(size, data):
    count = ref_count(size)
    index = data.draw(st.integers(min_value=0, max_value=min(count + 2, 2**32 - 1)), label="index")
    length = data.draw(
        st.sampled_from([1, MiB, MiB - 1, MiB + 1, 0, size % MiB, (size % MiB) + 1, 2**32 - 1]),
        label="length",
    )
    ok = index < count and length == ref_length(size, index)
    raw = ref_header(index, length, DIGEST)
    try:
        _receive(raw, size)
    except BlockError:
        assert not ok, f"khối hợp lệ bị từ chối: size={size}, index={index}, length={length}"
        return
    assert ok, f"khối sai được nhận: size={size}, index={index}, length={length}"
