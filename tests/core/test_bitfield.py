"""Test `teleport.core.bitfield.Bitfield` theo kế hoạch GĐ2 mục 4, 6f và docs/protocol.md §3.3.

Đặc tả: bit i nằm ở `byte[i // 8] & (0x80 >> (i % 8))` (MSB trước); `bitfield` trên dây là
base64 của đúng ⌈n / 8⌉ byte, các bit thừa ở cuối bằng 0; file rỗng (n = 0) ↔ `""`.
`from_base64(s, n)` sai độ dài hoặc bit thừa ≠ 0 → `ValueError`.

Giá trị kỳ vọng dựng độc lập bằng `base64` của thư viện chuẩn (base64 chuẩn RFC 4648, có `=`).
"""

import base64

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from teleport.core.bitfield import Bitfield


def ref_bytes(n: int, bits: set[int]) -> bytes:
    out = bytearray((n + 7) // 8)
    for i in bits:
        out[i // 8] |= 0x80 >> (i % 8)
    return bytes(out)


def ref_b64(n: int, bits: set[int]) -> str:
    return base64.b64encode(ref_bytes(n, bits)).decode("ascii")


def contents(bf: Bitfield, n: int) -> set[int]:
    return {i for i in range(n) if i in bf}


def decoded(bf: Bitfield) -> bytes:
    return base64.b64decode(bf.to_base64(), validate=True)


# --- Tạo mới, set / clear / in ------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 7, 8, 9, 16, 17, 1000])
def test_new_bitfield_is_all_zero(n):
    bf = Bitfield(n)
    assert contents(bf, n) == set()
    assert list(bf.missing()) == list(range(n))
    assert decoded(bf) == bytes((n + 7) // 8)


def test_negative_size_rejected():
    with pytest.raises(ValueError):
        Bitfield(-1)


@pytest.mark.parametrize(
    ("i", "byte_index", "mask"), [(0, 0, 0x80), (1, 0, 0x40), (7, 0, 0x01), (8, 1, 0x80)]
)
def test_bit_order_is_msb_first(i, byte_index, mask):
    n = 16
    bf = Bitfield(n)
    bf.set(i)
    raw = decoded(bf)
    assert raw[byte_index] == mask
    assert sum(raw) == mask  # không bit nào khác
    assert raw == ref_bytes(n, {i})


def test_set_bit_zero_of_one_bit_field_gives_0x80():
    bf = Bitfield(1)
    bf.set(0)
    assert bf.to_base64() == "gA=="  # base64 của b"\x80"


def test_set_is_idempotent_and_clear_resets():
    bf = Bitfield(10)
    bf.set(3)
    bf.set(3)
    assert 3 in bf
    assert contents(bf, 10) == {3}
    bf.clear(3)
    assert 3 not in bf
    bf.clear(3)
    assert contents(bf, 10) == set()


def test_last_bit_of_non_multiple_of_8():
    bf = Bitfield(9)
    bf.set(8)
    assert decoded(bf) == b"\x00\x80"
    assert list(bf.missing()) == list(range(8))


@pytest.mark.parametrize("i", [10, 11, 100, -1])
def test_index_out_of_range_rejected(i):
    # Lựa chọn của test: chỉ số ngoài [0, n) là lỗi (IndexError hoặc ValueError), không âm thầm
    # bật bit thừa hay đếm từ cuối kiểu Python.
    bf = Bitfield(10)
    with pytest.raises((IndexError, ValueError)):
        bf.set(i)
    with pytest.raises((IndexError, ValueError)):
        bf.clear(i)
    assert contents(bf, 10) == set()
    assert decoded(bf) == bytes(2)


# --- all_set / missing -------------------------------------------------------------------------


def test_empty_bitfield_is_all_set_and_has_nothing_missing():
    bf = Bitfield(0)
    assert bf.all_set() is True
    assert list(bf.missing()) == []
    assert bf.to_base64() == ""


@pytest.mark.parametrize("n", [1, 8, 9, 23])
def test_all_set_only_when_every_bit_set(n):
    bf = Bitfield(n)
    for i in range(n - 1):
        bf.set(i)
    assert bf.all_set() is False
    assert list(bf.missing()) == [n - 1]
    bf.set(n - 1)
    assert bf.all_set() is True
    assert list(bf.missing()) == []
    bf.clear(0)
    assert bf.all_set() is False
    assert list(bf.missing()) == [0]


def test_missing_is_ascending():
    bf = Bitfield(20)
    for i in (0, 2, 3, 9, 19):
        bf.set(i)
    assert list(bf.missing()) == [1, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15, 16, 17, 18]


# --- from_base64 -------------------------------------------------------------------------------


def test_zero_blocks_round_trips_with_empty_string():
    bf = Bitfield.from_base64("", 0)
    assert bf.all_set() is True
    assert bf.to_base64() == ""


@pytest.mark.parametrize("n", [1, 7, 8])
def test_empty_string_for_non_empty_file_rejected(n):
    with pytest.raises(ValueError):
        Bitfield.from_base64("", n)


@pytest.mark.parametrize(
    ("n", "raw"),
    [
        (0, b"\x00"),  # file rỗng phải là "" (0 byte)
        (1, b""),
        (1, b"\x00\x00"),
        (8, b"\x00\x00"),
        (9, b"\x00"),
        (9, b"\x00\x00\x00"),
        (16, b"\x00"),
        (17, b"\x00\x00"),
    ],
)
def test_wrong_length_rejected(n, raw):
    with pytest.raises(ValueError):
        Bitfield.from_base64(base64.b64encode(raw).decode(), n)


@pytest.mark.parametrize(
    ("n", "raw"),
    [
        (1, b"\x40"),  # bit 1 ngoài phạm vi
        (1, b"\x01"),
        (7, b"\x01"),
        (9, b"\xff\x40"),
        (9, b"\x00\x01"),
        (15, b"\x00\x01"),
        (17, b"\xff\xff\x7f"),
    ],
)
def test_nonzero_trailing_bits_rejected(n, raw):
    with pytest.raises(ValueError):
        Bitfield.from_base64(base64.b64encode(raw).decode(), n)


@pytest.mark.parametrize(
    ("n", "raw"), [(1, b"\x80"), (7, b"\xfe"), (8, b"\xff"), (9, b"\xff\x80"), (17, b"\xff\xff\x80")]
)
def test_all_ones_with_zero_padding_accepted(n, raw):
    bf = Bitfield.from_base64(base64.b64encode(raw).decode(), n)
    assert bf.all_set() is True
    assert decoded(bf) == raw


@pytest.mark.parametrize("s", ["!!!!", "gA=", "g", "gA==gA==", "gA ==", "*A=="], ids=repr)
def test_malformed_base64_rejected(s):
    with pytest.raises(ValueError):
        Bitfield.from_base64(s, 1)


def test_from_base64_reads_msb_first():
    bf = Bitfield.from_base64(base64.b64encode(b"\xa0\x01").decode(), 16)
    assert contents(bf, 16) == {0, 2, 15}


# --- Thuộc tính (hypothesis) -------------------------------------------------------------------


@st.composite
def sized_bit_sets(draw):
    n = draw(st.integers(min_value=0, max_value=3000))
    bits = draw(st.sets(st.integers(min_value=0, max_value=n - 1), max_size=200)) if n else set()
    return n, bits


@settings(max_examples=300, deadline=None)
@given(sized_bit_sets())
def test_property_to_base64_matches_reference_and_round_trips(case):
    n, bits = case
    bf = Bitfield(n)
    for i in bits:
        bf.set(i)
    s = bf.to_base64()
    assert s == ref_b64(n, bits)
    back = Bitfield.from_base64(s, n)
    assert contents(back, n) == bits
    assert back.to_base64() == s
    assert list(back.missing()) == sorted(set(range(n)) - bits)
    assert back.all_set() == (len(bits) == n)


@settings(max_examples=300, deadline=None)
@given(sized_bit_sets())
def test_property_from_reference_encoding(case):
    n, bits = case
    bf = Bitfield.from_base64(ref_b64(n, bits), n)
    assert contents(bf, n) == bits


@settings(max_examples=300, deadline=None)
@given(st.integers(min_value=0, max_value=200), st.binary(max_size=30))
def test_property_from_base64_accepts_iff_length_and_padding_ok(n, raw):
    expected_len = (n + 7) // 8
    pad_bits = expected_len * 8 - n
    ok = len(raw) == expected_len and (pad_bits == 0 or raw[-1] & ((1 << pad_bits) - 1) == 0)
    s = base64.b64encode(raw).decode()
    try:
        bf = Bitfield.from_base64(s, n)
    except ValueError:
        assert not ok, f"bitfield hợp lệ bị từ chối: n={n}, raw={raw.hex()}"
        return
    assert ok, f"bitfield sai độ dài / bit thừa lại được nhận: n={n}, raw={raw.hex()}"
    assert bf.to_base64() == s


def test_four_tib_file_bitfield_size():
    # 4 TiB / 1 MiB = 2²² khối → 512 KiB bitfield → 699 052 ký tự base64 (protocol §3.3).
    n = 2**22
    bf = Bitfield(n)
    assert len(bf.to_base64()) == 4 * ((n // 8 + 2) // 3)
    assert len(base64.b64decode(bf.to_base64())) == 512 * 1024
