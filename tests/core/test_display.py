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
import re
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


# =================================================================================================
# P2-7 (plan-p2-7 mục 2.5, 3; mvp §8 hệ nhị phân): `format_size`, `format_duration`,
# `display_filename`, `FILE_NAME_WIDTH`. Viết theo đặc tả:
# - `format_size(n, *, trim=False)`: < 1024 → “N byte”; KB/MB/GB/TB (1 KB = 1024 byte), dấu phẩy thập
#   phân; giá trị < 100 → 1 chữ số thập phân, ≥ 100 → số nguyên; làm tròn chạm 1024 → lên đơn vị;
#   `trim` bỏ “,0”.
# - `format_duration(s)`: “45 giây”, “2 phút 10 giây”, “1 giờ 5 phút”, “2 ngày 3 giờ”.
# - `display_filename(name, max_width=48)`: ký tự trông như khoảng trắng → `·` (như `display_name`);
#   quá rộng thì cắt giữa, giữ đuôi (đuôi file).
# Ca biên mà đặc tả không chốt (99,96 KB; “1 phút 0 giây” hay “1 phút”; làm tròn hay cắt phút) không
# được kiểm.
# =================================================================================================

KIB = 1024
MIB = 1024**2
GIB = 1024**3
TIB = 1024**4


def test_p27_file_name_width_is_48(display):
    assert display.FILE_NAME_WIDTH == 48


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0 byte"),
        (1, "1 byte"),
        (1023, "1023 byte"),
        (1024, "1,0 KB"),
        (1536, "1,5 KB"),
        (10 * KIB, "10,0 KB"),
        (int(99.4 * KIB), "99,4 KB"),
        (100 * KIB, "100 KB"),
        (380 * KIB, "380 KB"),
        (1023 * KIB, "1023 KB"),
        (int(1023.6 * KIB), "1,0 MB"),  # làm tròn chạm 1024 → lên đơn vị
        (MIB, "1,0 MB"),
        (int(2.1 * MIB), "2,1 MB"),
        (int(85.2 * MIB), "85,2 MB"),
        (int(3.4 * GIB), "3,4 GB"),
        (8 * GIB, "8,0 GB"),
        (int(8.4 * GIB), "8,4 GB"),
        (int(12.4 * GIB), "12,4 GB"),
        (TIB, "1,0 TB"),
        (int(5.2 * TIB), "5,2 TB"),
        (4 * TIB, "4,0 TB"),
    ],
)
def test_p27_format_size_matches_spec_examples(display, n, expected):
    assert display.format_size(n) == expected


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (500, "500 byte"),
        (KIB, "1 KB"),
        (1536, "1,5 KB"),
        (100 * KIB, "100 KB"),
        (20 * MIB, "20 MB"),
        (64 * KIB, "64 KB"),
        (4 * GIB, "4 GB"),  # FAT32: “nhỏ hơn 4 GB” = format_size(max_size + 1, trim=True)
        (4 * TIB, "4 TB"),
        (int(8.4 * GIB), "8,4 GB"),
    ],
)
def test_p27_format_size_trim_drops_only_trailing_zero_decimal(display, n, expected):
    assert display.format_size(n, trim=True) == expected


def test_p27_format_size_beyond_tb_stays_in_tb(display):
    assert display.format_size(2**52).endswith(" TB")


_SIZE_RE = re.compile(r"(\d+)(?:,(\d))? (byte|KB|MB|GB|TB)")
_UNIT = {"KB": KIB, "MB": MIB, "GB": GIB, "TB": TIB}


@settings(max_examples=500, deadline=None)
@given(n=st.one_of(st.integers(0, 2**52), st.integers(0, 4 * KIB), st.integers(1000 * MIB, 1030 * MIB)))
def test_p27_format_size_is_close_to_value_and_well_formed(display, n):
    out = display.format_size(n)
    m = _SIZE_RE.fullmatch(out)
    assert m, out
    whole, frac, unit = m.groups()
    if unit == "byte":
        assert n < 1024 and frac is None and int(whole) == n
        return
    assert n >= 1024, out
    value = int(whole) + (int(frac) / 10 if frac is not None else 0.0)
    mult = _UNIT[unit]
    step = 0.05 if frac is not None else 0.5
    assert abs(value * mult - n) <= step * mult + 1e-6 * mult, out
    assert value >= 1.0, out
    if unit != "TB":
        assert value < 1024, f"chạm 1024 phải lên đơn vị: {out}"
    exact = n / mult
    if exact < 99.9:
        assert frac is not None, f"< 100 phải có 1 chữ số thập phân: {out}"
    if exact >= 100 and value >= 100:
        assert frac is None, f"≥ 100 phải là số nguyên: {out}"
    trimmed = display.format_size(n, trim=True)
    assert trimmed == (out.replace(",0 ", " ") if ",0 " in out else out)


@pytest.mark.parametrize(
    ("s", "expected"),
    [
        (1, "1 giây"),
        (45, "45 giây"),
        (59, "59 giây"),
        (62, "1 phút 2 giây"),
        (62.4, "1 phút 2 giây"),
        (130, "2 phút 10 giây"),
        (3599, "59 phút 59 giây"),
        (3900, "1 giờ 5 phút"),
        (7500, "2 giờ 5 phút"),
        (2 * 86400 + 3 * 3600, "2 ngày 3 giờ"),
    ],
)
def test_p27_format_duration_matches_spec_examples(display, s, expected):
    assert display.format_duration(s) == expected


@settings(max_examples=300, deadline=None)
@given(
    case=st.one_of(
        st.integers(1, 59).map(lambda s: (s, f"{s} giây")),
        st.tuples(st.integers(1, 59), st.integers(1, 59)).map(
            lambda t: (60 * t[0] + t[1], f"{t[0]} phút {t[1]} giây")
        ),
        st.tuples(st.integers(1, 23), st.integers(1, 59)).map(
            lambda t: (3600 * t[0] + 60 * t[1], f"{t[0]} giờ {t[1]} phút")
        ),
        st.tuples(st.integers(1, 400), st.integers(1, 23)).map(
            lambda t: (86400 * t[0] + 3600 * t[1], f"{t[0]} ngày {t[1]} giờ")
        ),
    )
)
def test_p27_format_duration_uses_two_largest_units(display, case):
    s, expected = case
    assert display.format_duration(s) == expected


def test_p27_display_filename_keeps_short_name(display):
    assert display.display_filename("video.mp4") == "video.mp4"
    assert display.display_filename("báo cáo (1).docx") == "báo cáo (1).docx"


@pytest.mark.parametrize("ch", LISTED_BLANKS, ids=lambda ch: f"U+{ord(ch):04X}")
def test_p27_display_filename_shows_blank_chars_as_mark(display, ch):
    assert display.display_filename(f"bao{ch}cao.txt") == f"bao{MARK}cao.txt"


def test_p27_display_filename_exactly_48_columns_is_kept_and_49_is_cut(display):
    name48 = "a" * 44 + ".mp4"
    assert display.display_filename(name48) == name48
    name49 = "a" * 45 + ".mp4"
    out = display.display_filename(name49)
    assert columns(out) <= 48 and ELLIPSIS in out and out.endswith(".mp4")


def test_p27_display_filename_cuts_middle_and_keeps_extension(display):
    name = "Chuyen-di-Da-Lat-thang-10-" * 5 + "ban-cuoi.mp4"
    out = display.display_filename(name)
    assert columns(out) <= 48
    assert out.endswith(".mp4"), out
    head, sep, tail = out.partition(ELLIPSIS)
    assert sep == ELLIPSIS and head and name.startswith(head) and name.endswith(tail), out


def test_p27_display_filename_counts_wide_chars_as_two_columns(display):
    name = "中" * 40 + ".txt"  # 84 cột
    out = display.display_filename(name)
    assert columns(out) <= 48 and out.endswith(".txt"), out


def test_p27_display_filename_custom_width(display):
    out = display.display_filename("x" * 30 + ".tar.gz", max_width=20)
    assert columns(out) <= 20 and out.endswith(".gz"), out


@settings(max_examples=400, deadline=None)
@given(
    stem=st.text(alphabet=_CHARS, min_size=1, max_size=200),
    ext=st.sampled_from(["", ".mp4", ".txt", ".tar.gz", ".docx"]),
    width=st.integers(min_value=16, max_value=80),
)
def test_p27_display_filename_fits_width_and_keeps_head_and_tail(display, stem, ext, width):
    name = stem + ext
    out = display.display_filename(name, max_width=width)
    assert columns(out) <= width, (name, out)
    assert not (set(out) & BLANKS), (name, out)
    shown = replaced(name)
    if columns(shown) <= width and columns(name) <= width:
        assert out == shown, (name, out)
        return
    if columns(shown) > width:
        # Tên có thể tự chứa `…`: tìm một chỗ `…` chia `out` thành phần đầu + phần cuối của tên.
        splits = [
            (out[:i], out[i + 1 :])
            for i, ch in enumerate(out)
            if ch == ELLIPSIS and shown.startswith(out[:i]) and shown.endswith(out[i + 1 :])
        ]
        assert splits, ("cắt giữa: phần đầu + … + phần cuối", name, out)
        assert any(head for head, _ in splits), "cắt giữa: vẫn giữ phần đầu"
        if ext:
            assert out.endswith(ext), "giữ đuôi file"
        # Không bỏ thừa nhiều: mỗi chỗ cắt thiếu tối đa 1 cột (chữ rộng 2 cột), cộng 1 cột nới cho
        # ký tự trống rộng 2 cột thành `·`.
        assert columns(out) >= width - 3, (name, out)
