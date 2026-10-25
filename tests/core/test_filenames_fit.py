"""Test các API mới của `teleport.core.filenames` cho P2-5: `dedupe_key`, `fit_filename`,
`fallback_filename`, `TEMP_DIR_NAME`, `FALLBACK_MAX_BYTES`.

Viết theo đặc tả (spec P2-5 mục 2.1, AC-1…AC-3, H5 và danh sách ca biên ở mục 5), không theo code:
- `dedupe_key`: NFC → casefold → NFC; `dedupe_offer_names` giữ nguyên hành vi (test cũ ở
  `tests/core/test_filenames.py` vẫn phải đạt).
- `fit_filename(name, max_bytes)`: làm sạch, cắt còn ≤ `max_bytes` byte UTF-8 theo luật cắt của
  `numbered` (giữ đuôi ≤ 32 byte; không đủ chỗ thì bỏ đuôi, cắt ở cuối), làm sạch lại tới khi ổn
  định. Kết quả ≤ `max_bytes`, không rỗng, ổn định qua `sanitize_filename`, không phải tên thiết bị
  Windows hay `.teleport`; với `max_bytes` = 255 thì bằng `sanitize_filename`.
- `fallback_filename(name, max_bytes)`: làm sạch, thay mọi code point ngoài ASCII bằng `_`, rồi
  `fit_filename(…, min(max_bytes, 64))`.
- Tham số nguyên kiểm bằng `type(x) is int` (`True` là giá trị sai); `name` không phải `str` →
  `TypeError`.

Module được import trong fixture, để thiếu API chỉ làm đỏ các test ở đây.
"""

import importlib
import unicodedata

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

MAX_BYTES = 255
FALLBACK_BYTES = 64

DEVICE_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{d}" for d in "0123456789\u00b9\u00b2\u00b3"}
    | {f"LPT{d}" for d in "0123456789\u00b9\u00b2\u00b3"}
)


def is_windows_device(name: str) -> bool:
    """Windows xét phần trước dấu chấm đầu tiên, bỏ khoảng trắng, không phân biệt hoa thường."""
    stem = name.split(".", 1)[0].strip(" ")
    return stem.upper() in DEVICE_STEMS


def utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def key(name: str) -> str:
    """Khóa so trùng theo đặc tả: NFC → casefold → NFC (viết độc lập với code)."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


@pytest.fixture(scope="module")
def fn():
    return importlib.import_module("teleport.core.filenames")


def assert_fitted(fn, out: object, max_bytes: int) -> None:
    """Các bảo đảm chung của `fit_filename` (spec mục 2.1)."""
    assert isinstance(out, str)
    assert out != ""
    assert utf8_len(out) <= max_bytes, (ascii(out), max_bytes)
    assert fn.sanitize_filename(out) == out, ascii(out)
    assert not is_windows_device(out), ascii(out)
    assert out.casefold() != ".teleport", ascii(out)
    assert not set(out) & set("/\\:"), ascii(out)


# ---------------------------------------------------------------------------------------------
# Hằng (AC-3)
# ---------------------------------------------------------------------------------------------


def test_temp_dir_name_constant(fn):
    assert fn.TEMP_DIR_NAME == ".teleport"


def test_fallback_max_bytes_constant(fn):
    assert fn.FALLBACK_MAX_BYTES == 64


def test_temp_dir_name_is_renamed_by_sanitizer(fn):
    # `.teleport` là tên nội bộ: tên nhận được không bao giờ trùng nó, kể cả khác hoa thường.
    for raw in (fn.TEMP_DIR_NAME, fn.TEMP_DIR_NAME.upper()):
        assert key(fn.sanitize_filename(raw)) != key(fn.TEMP_DIR_NAME)


# ---------------------------------------------------------------------------------------------
# dedupe_key (AC-1)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("a.TXT", "A.txt"),
        ("\u00e9.txt", "e\u0301.txt"),  # é ghép sẵn và e + dấu sắc kết hợp
        ("\u00c9.TXT", "e\u0301.txt"),
        ("ß", "SS"),
        ("Straße.txt", "STRASSE.TXT"),
        ("Tiếng Việt.docx", unicodedata.normalize("NFD", "TIẾNG VIỆT.DOCX")),
    ],
)
def test_dedupe_key_equal_for_case_and_normalization_variants(fn, a, b):
    assert fn.dedupe_key(a) == fn.dedupe_key(b)


@pytest.mark.parametrize(("a", "b"), [("a.txt", "b.txt"), ("a.txt", "a (1).txt"), ("é", "e")])
def test_dedupe_key_differs_for_different_names(fn, a, b):
    assert fn.dedupe_key(a) != fn.dedupe_key(b)


def test_dedupe_key_matches_spec_definition(fn):
    for name in ["Ab.TXT", "e\u0301", "ß", "\u0130stanbul", "ﬃ", "Ω", "\u1e9e"]:
        assert fn.dedupe_key(name) == key(name), ascii(name)


@settings(max_examples=300, deadline=None)
@given(st.text(st.characters(exclude_categories=("Cs",)), max_size=40))
def test_property_dedupe_key_is_nfc_and_form_insensitive(fn, raw):
    out = fn.dedupe_key(raw)
    assert unicodedata.is_normalized("NFC", out)
    assert out == key(raw)
    assert fn.dedupe_key(unicodedata.normalize("NFD", raw)) == out


def test_dedupe_offer_names_still_uses_case_insensitive_key(fn):
    out = fn.dedupe_offer_names(["a.txt", "A.TXT", "e\u0301.txt", "\u00e9.txt"])
    assert out[0] == "a.txt"
    assert len({fn.dedupe_key(o) for o in out}) == 4


# ---------------------------------------------------------------------------------------------
# fit_filename (AC-2)
# ---------------------------------------------------------------------------------------------

EDGE_NAMES = [
    "",
    "..",
    ".",
    "./",
    "../../x",
    "C:\\Windows\\x",
    "\\\\?\\C:\\x",
    "a.txt:ads",
    "CON",
    "nul.txt",
    "COM\u00b9",
    ".teleport",
    ".TELEPORT",
    "x.",
    "x ",
    "photo\u202egpj.exe",
    unicodedata.normalize("NFD", "Tiếng Việt.txt"),
    "ệ" * 85,  # 255 byte toàn ký tự 3 byte
    "a" * 50 + "." + "b" * 32,  # đuôi 33 byte: coi như không có đuôi
    "a.txt",
    "a" * 300 + ".mp4",
]


@pytest.mark.parametrize("raw", EDGE_NAMES)
def test_fit_filename_at_255_equals_sanitize(fn, raw):
    assert fn.fit_filename(raw, 255) == fn.sanitize_filename(raw)


@pytest.mark.parametrize("raw", EDGE_NAMES)
def test_fit_filename_default_budget_is_255(fn, raw):
    assert fn.fit_filename(raw) == fn.sanitize_filename(raw)


@pytest.mark.parametrize("raw", EDGE_NAMES)
@pytest.mark.parametrize("max_bytes", [4, 5, 7, 8, 12, 33, 54, 64, 100, 254])
def test_fit_filename_edge_names_satisfy_guarantees(fn, raw, max_bytes):
    assert_fitted(fn, fn.fit_filename(raw, max_bytes), max_bytes)


def test_fit_filename_cuts_stem_and_keeps_extension(fn):
    out = fn.fit_filename("a" * 300 + ".mp4", 60)
    assert utf8_len(out) <= 60
    assert out.endswith(".mp4")
    # Luật cắt của `numbered`: cắt phần trước đuôi theo byte, không bỏ phí chỗ.
    assert out == "a" * 56 + ".mp4"


def test_fit_filename_multibyte_stem_not_cut_mid_character(fn):
    out = fn.fit_filename("ệ" * 100 + ".txt", 50)
    assert_fitted(fn, out, 50)
    assert out.endswith(".txt")
    assert set(out[: -len(".txt")]) == {"ệ"}
    assert utf8_len(out) > 50 - 3  # chỉ hụt tối đa một ký tự 3 byte


def test_fit_filename_device_name_created_by_cut_is_renamed(fn):
    out = fn.fit_filename("CONabc.txt", 7)
    assert utf8_len(out) <= 7
    assert not is_windows_device(out)
    assert fn.sanitize_filename(out) == out
    # Spec mục 2.1: cắt còn `CON.txt` thì phải thành `_CO.txt`.
    assert out == "_CO.txt"


def test_fit_filename_cut_leaving_trailing_dot_or_space_is_cleaned(fn):
    for raw, budget in [("ab. cd.txt", 7), ("ab .x" * 20, 9), ("a" * 10 + ". " * 10 + ".txt", 15)]:
        out = fn.fit_filename(raw, budget)
        assert_fitted(fn, out, budget)
        assert not out.endswith((".", " "))


def test_fit_filename_extension_of_33_bytes_is_cut_at_end(fn):
    raw = "a" * 100 + "." + "b" * 32  # đuôi 33 byte > MAX_EXTENSION_BYTES
    out = fn.fit_filename(raw, 60)
    assert_fitted(fn, out, 60)
    assert raw.startswith(out)


def test_fit_filename_extension_of_32_bytes_is_kept(fn):
    ext = "." + "b" * 31  # đúng 32 byte
    out = fn.fit_filename("a" * 100 + ext, 60)
    assert_fitted(fn, out, 60)
    assert out.endswith(ext)


def test_fit_filename_no_room_for_extension_drops_it(fn):
    out = fn.fit_filename("abcdef.mp4", 5)
    assert_fitted(fn, out, 5)


@pytest.mark.parametrize("max_bytes", [0, -1, 256, 1000, True, False, 1.5, 60.0, "60", None])
def test_fit_filename_rejects_bad_max_bytes(fn, max_bytes):
    with pytest.raises(ValueError):
        fn.fit_filename("a.txt", max_bytes)


@pytest.mark.parametrize("name", [None, b"a.txt", 123, ["a.txt"], bytearray(b"a")])
def test_fit_filename_rejects_non_str_name(fn, name):
    with pytest.raises(TypeError):
        fn.fit_filename(name, 60)


@pytest.mark.parametrize("max_bytes", [1, 2, 3])
@pytest.mark.parametrize("raw", ["CON", "a.txt", "ệ", "😀.txt", "x", ".teleport", "ab"])
def test_fit_filename_tiny_budget_either_meets_guarantees_or_raises_value_error(fn, raw, max_bytes):
    # Spec mục 2.1: chỉ khi max_bytes < 4 mới có thể không thỏa được → ValueError.
    try:
        out = fn.fit_filename(raw, max_bytes)
    except ValueError:
        return
    assert_fitted(fn, out, max_bytes)


# ---------------------------------------------------------------------------------------------
# fallback_filename (AC-3)
# ---------------------------------------------------------------------------------------------


def test_fallback_replaces_non_ascii_and_keeps_extension(fn):
    out = fn.fallback_filename("Ảnh \U00018df3.jpg")
    assert out.isascii()
    assert out.endswith(".jpg")
    assert utf8_len(out) <= 64
    assert fn.sanitize_filename(out) == out
    assert out == "_nh _.jpg"  # ví dụ trong spec mục 2.1


def test_fallback_of_all_non_ascii_name_is_not_empty(fn):
    out = fn.fallback_filename("漢字")
    assert out
    assert out.isascii()
    assert fn.sanitize_filename(out) == out


@pytest.mark.parametrize("raw", [*EDGE_NAMES, "ệ" * 200 + ".txt", "😀" * 70, "Báo cáo tháng 10.docx"])
def test_fallback_edge_names_are_ascii_short_and_stable(fn, raw):
    out = fn.fallback_filename(raw)
    assert out.isascii(), ascii(out)
    assert_fitted(fn, out, 64)


def test_fallback_long_name_keeps_ascii_extension(fn):
    out = fn.fallback_filename("ệ" * 200 + ".txt")
    assert utf8_len(out) <= 64
    assert out.endswith(".txt")


@pytest.mark.parametrize(("max_bytes", "limit"), [(20, 20), (64, 64), (100, 64), (255, 64), (4, 4)])
def test_fallback_budget_is_min_of_argument_and_64(fn, max_bytes, limit):
    out = fn.fallback_filename("tên rất dài " * 30 + ".bin", max_bytes)
    assert out.isascii()
    assert_fitted(fn, out, limit)


@pytest.mark.parametrize("max_bytes", [0, -1, 256, True, 1.5, None])
def test_fallback_rejects_bad_max_bytes(fn, max_bytes):
    with pytest.raises(ValueError):
        fn.fallback_filename("a.txt", max_bytes)


@pytest.mark.parametrize("name", [None, b"a.txt", 1])
def test_fallback_rejects_non_str_name(fn, name):
    with pytest.raises(TypeError):
        fn.fallback_filename(name)


# ---------------------------------------------------------------------------------------------
# Thuộc tính (H5)
# ---------------------------------------------------------------------------------------------

ANY_CHAR = st.characters(exclude_categories=())
TRICKY_PIECES = st.sampled_from(
    [
        "/",
        "\\",
        ":",
        "..",
        ".",
        " ",
        "C:",
        "\\\\?\\",
        "CON",
        "nul",
        "Com1",
        "LPT\u00b9",
        ".txt",
        ".mp4",
        ".teleport",
        ".TELEPORT",
        "\u202e",
        "\u200b",
        "\u200d",
        "\x00",
        "\ud800",
        "e\u0301",
        "é",
        "ệ",
        "😀",
        "\U00018df3",
        "\u0958",
        "a",
        "Z",
        "\u00df",
    ]
)
NAMES = st.one_of(
    st.text(ANY_CHAR, max_size=80),
    st.lists(st.one_of(ANY_CHAR, TRICKY_PIECES), max_size=60).map("".join),
    st.lists(
        st.sampled_from(["a", "ệ", "😀", "e\u0301", ".", " ", ".txt", "CON", "\u202e"]),
        min_size=50,
        max_size=200,
    ).map("".join),
)


@settings(max_examples=400, deadline=None)
@given(NAMES, st.integers(min_value=4, max_value=255))
def test_property_fit_filename_guarantees(fn, raw, max_bytes):
    assert_fitted(fn, fn.fit_filename(raw, max_bytes), max_bytes)


@settings(max_examples=300, deadline=None)
@given(NAMES)
def test_property_fit_filename_at_255_is_sanitize(fn, raw):
    assert fn.fit_filename(raw, 255) == fn.sanitize_filename(raw)


@settings(max_examples=400, deadline=None)
@given(NAMES, st.integers(min_value=4, max_value=255))
def test_property_fallback_filename_is_ascii_short_and_stable(fn, raw, max_bytes):
    out = fn.fallback_filename(raw, max_bytes)
    assert out.isascii(), ascii(out)
    assert_fitted(fn, out, min(max_bytes, FALLBACK_BYTES))


@settings(max_examples=300, deadline=None)
@given(NAMES)
def test_property_fallback_default_budget_is_64(fn, raw):
    out = fn.fallback_filename(raw)
    assert out.isascii()
    assert_fitted(fn, out, FALLBACK_BYTES)
