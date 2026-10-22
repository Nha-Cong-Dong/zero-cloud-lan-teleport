"""Test `teleport.core.display`: hiện tên do máy kia tự đặt (kế hoạch nhóm (a) mục H1, mvp mục 7.2).

Viết theo đặc tả, không theo code:
- Ký tự trông như khoảng trắng mà không phải U+0020 thành `·`: mọi ký tự loại Zs trừ U+0020, cùng
  U+2800, U+3164, U+115F, U+1160, U+FFA0.
- Độ rộng: `east_asian_width` W/F = 2 cột; Mn, Me và U+200D = 0 cột; còn lại 1 cột. Hàm `columns`
  dưới đây tính lại độc lập theo đúng luật đó.
- Vượt `max_width` (mặc định 32) thì cắt, phần giữ lại + `…` ≤ `max_width`; bỏ U+200D thừa ở cuối
  phần cắt. Không thêm ngoặc. `name` không phải chuỗi → TypeError; `max_width < 2` → ValueError.

Module được import trong fixture, để thiếu module chỉ làm đỏ các test ở đây, không chặn cả bộ test.
"""

import importlib
import unicodedata

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from teleport.core.messages import MessageError, validate_device_name

ELLIPSIS = "…"  # U+2026
MARK = "·"  # U+00B7 (BLANK_MARK)
ZWJ = "\u200d"
WIDTH = 32

# Danh sách nêu trong kế hoạch, cộng mọi ký tự Zs của bản Unicode đang chạy (trừ U+0020).
LISTED_BLANKS = (
    "\u00a0",
    "\u1680",
    *(chr(c) for c in range(0x2000, 0x200B)),
    "\u202f",
    "\u205f",
    "\u3000",
    "\u2800",
    "\u3164",
    "\u115f",
    "\u1160",
    "\uffa0",
)
ALL_ZS = tuple(chr(c) for c in range(0x110000) if unicodedata.category(chr(c)) == "Zs" and c != 0x20)
BLANKS = frozenset(LISTED_BLANKS) | frozenset(ALL_ZS)

# Ký tự vừa là dấu kết hợp (Mn/Me) vừa rộng (W/F): đặc tả không nói luật nào thắng, nên test
# không sinh các ký tự này (ví dụ U+302A, U+3099).
AMBIGUOUS_WIDTH = frozenset(
    chr(c)
    for c in range(0x110000)
    if unicodedata.category(chr(c)) in ("Mn", "Me") and unicodedata.east_asian_width(chr(c)) in ("W", "F")
)


def columns(text: str) -> int:
    """Độ rộng theo cột, tính lại độc lập theo đặc tả."""
    total = 0
    for ch in text:
        if ch == ZWJ or unicodedata.category(ch) in ("Mn", "Me"):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def replaced(text: str) -> str:
    """Tên sau khi thay ký tự trống bằng `·` (chưa cắt)."""
    return "".join(MARK if ch in BLANKS else ch for ch in text)


@pytest.fixture(scope="module")
def display():
    return importlib.import_module("teleport.core.display")


def is_valid_name(name: str) -> bool:
    try:
        validate_device_name(name)
    except MessageError:
        return False
    return True


# --- Hằng số -------------------------------------------------------------------------------------


def test_h1_constants_match_spec(display):
    assert display.DISPLAY_NAME_WIDTH == 32
    assert display.BLANK_MARK == MARK


# --- Ký tự trống thành `·` ------------------------------------------------------------------------


@pytest.mark.parametrize("ch", LISTED_BLANKS, ids=lambda ch: f"U+{ord(ch):04X}")
def test_h1_listed_blank_char_becomes_mark(display, ch):
    assert display.display_name(f"PC{ch}Nha") == f"PC{MARK}Nha"
    assert display.display_name(f"PC{ch}{ch}{ch}382") == f"PC{MARK}{MARK}{MARK}382"


def test_h1_every_unicode_space_except_ascii_space_becomes_mark(display):
    for ch in ALL_ZS:
        assert display.display_name(f"a{ch}b") == f"a{MARK}b", f"U+{ord(ch):04X}"


def test_h1_blank_chars_at_both_ends_become_mark(display):
    """Ký tự trống không phải khoảng trắng Python (U+2800, U+3164…) qua được `strip()` ở hai đầu."""
    name = "\u2800\u3164PC\uffa0\u115f"
    assert is_valid_name(name)
    assert display.display_name(name) == f"{MARK}{MARK}PC{MARK}{MARK}"


def test_h1_ascii_space_is_kept(display):
    assert display.display_name("PC Nha  2") == "PC Nha  2"


@pytest.mark.parametrize(
    "name",
    [
        "PC-Nha",
        "MacBook Pro của Lan",
        "MacBook Pro cu\u0309a Lan",  # NFD: dấu kết hợp 0 cột
        "Laptop-Bob",
        "中文电脑",
        "\U0001f468\u200d\U0001f469\u200d\U0001f467",  # emoji gia đình (ZWJ), 6 cột
        "\U0001f1fb\U0001f1f3 PC",  # cờ
        "x" * 32,  # đúng 32 cột
        "中" * 16,  # đúng 32 cột
    ],
    ids=repr,
)
def test_h1_ordinary_names_are_unchanged(display, name):
    assert is_valid_name(name)
    assert display.display_name(name) == name


def test_h1_does_not_add_quotes(display):
    out = display.display_name("PC-Nha")
    assert "“" not in out and "”" not in out and '"' not in out


# --- Độ rộng và rút gọn ----------------------------------------------------------------------------


def test_h1_long_ascii_name_is_cut_to_32_columns_with_ellipsis(display):
    out = display.display_name("x" * 64)
    assert out.endswith(ELLIPSIS)
    assert columns(out) <= WIDTH
    assert out == "x" * 31 + ELLIPSIS


def test_h1_one_column_over_the_limit_is_cut(display):
    assert display.display_name("x" * 33) == "x" * 31 + ELLIPSIS


def test_h1_cjk_counts_two_columns(display):
    out = display.display_name("中" * 17)  # 34 cột
    assert out == "中" * 15 + ELLIPSIS  # 30 + 1 cột; thêm một chữ nữa thì thành 33
    assert columns(out) == 31
    out = display.display_name("a" + "中" * 16)  # 33 cột
    assert out == "a" + "中" * 15 + ELLIPSIS
    assert columns(out) == 32


def test_h1_combining_marks_and_zwj_count_zero_columns(display):
    for name in ("e\u0301" * 32, "a\u20dd" * 32, "a\u200d" * 31 + "a"):  # Mn, Me, ZWJ
        assert columns(name) == 32
        assert display.display_name(name) == name


def test_h1_blank_mark_counts_one_column(display):
    """U+3000 (rộng 2 cột) thành `·` (1 cột): 30 chữ + 2 ký tự trống vẫn vừa 32 cột."""
    name = "x" * 15 + "\u3000\u3000" + "y" * 15
    assert display.display_name(name) == "x" * 15 + MARK * 2 + "y" * 15


def test_h1_cut_never_ends_with_zwj(display):
    out = display.display_name("x" * 31 + ZWJ + "yyy")
    assert out == "x" * 31 + ELLIPSIS
    out = display.display_name("x" * 31 + ZWJ + ZWJ + "yy")
    assert out == "x" * 31 + ELLIPSIS
    out = display.display_name("x" * 29 + "\U0001f468\u200d\U0001f469\u200d\U0001f467")  # 35 cột
    assert out.endswith(ELLIPSIS) and columns(out) <= WIDTH
    assert not out[:-1].endswith(ZWJ), repr(out)


@pytest.mark.parametrize(
    ("name", "width", "expected"), [("x" * 10, 5, "xxxx…"), ("abc", 2, "a…"), ("ab", 2, "ab")]
)
def test_h1_custom_max_width(display, name, width, expected):
    assert display.display_name(name, max_width=width) == expected


def test_h1_max_width_two_with_wide_chars_still_fits(display):
    out = display.display_name("中中", max_width=2)
    assert out.endswith(ELLIPSIS)
    assert columns(out) <= 2


@pytest.mark.parametrize("value", [None, b"PC", 123, ["PC"], 1.5])
def test_h1_rejects_non_string(display, value):
    with pytest.raises(TypeError):
        display.display_name(value)


@pytest.mark.parametrize("width", [1, 0, -1])
def test_h1_rejects_max_width_below_two(display, width):
    with pytest.raises(ValueError):
        display.display_name("PC-Nha", max_width=width)


# --- Hypothesis --------------------------------------------------------------------------------------

_INTERESTING = (
    *sorted(BLANKS),
    "中",
    "\U0001f468",
    ZWJ,
    "\u0301",
    "\u20dd",
    "x",
    " ",
    "3",
    ":",
    ELLIPSIS,
    MARK,
    "\uff13",  # chữ số toàn chiều rộng
)
_CHARS = st.one_of(
    st.characters(exclude_categories=("Cc", "Cs")).filter(lambda ch: ch not in AMBIGUOUS_WIDTH),
    st.sampled_from(_INTERESTING),
)


def _check(name: str, out: str, width: int) -> None:
    assert isinstance(out, str)
    assert columns(out) <= width, (name, out)
    assert not (set(out) & BLANKS), (name, out)
    shown = replaced(name)
    if columns(shown) <= width and columns(name) <= width:
        assert out == shown, (name, out)
    if columns(shown) > width:
        assert out.endswith(ELLIPSIS), (name, out)
        kept = out[:-1]
        assert shown.startswith(kept), "phần giữ lại phải là phần đầu của tên"
        assert not kept.endswith(ZWJ), (name, out)
        # Không bỏ thừa: chữ kế tiếp rộng tối đa 2 cột, nên phần giữ lại + `…` thiếu tối đa 1 cột.
        # Đặc tả không nói đo trước hay sau khi thay ký tự trống (U+3000, U+3164 rộng 2 cột thành
        # `·` 1 cột), nên chấp nhận cả hai cách đo.
        original_kept = name[: len(kept)]  # thay ký tự trống là thay từng ký tự một
        assert max(columns(kept), columns(original_kept)) + 1 >= width - 1, (name, out)


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
@given(name=st.text(alphabet=_CHARS, min_size=1, max_size=64))
def test_h1_any_valid_device_name_fits_32_columns_without_blank_chars(display, name):
    assume(is_valid_name(name))
    _check(name, display.display_name(name), WIDTH)


@settings(max_examples=300, deadline=None)
@given(name=st.text(alphabet=_CHARS, max_size=200), width=st.integers(min_value=2, max_value=80))
def test_h1_any_text_fits_any_width(display, name, width):
    _check(name, display.display_name(name, max_width=width), width)
