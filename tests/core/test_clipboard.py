"""Test `teleport.core.clipboard` (sans-IO) theo spec P5-0 mục 2.3–2.8, 5.2–5.6; mục 9 thắng khi khác.

Viết từ đặc tả, không từ code. Các mốc thời gian là số biểu diễn chính xác bằng float (9.5).
Hằng của spec (1 MiB, 10 MiB, 768 KiB…) được chép lại ở `test_constants_match_spec` để test không
lặp lại sai số của code; các test khác dùng hằng của module.

Mô hình tham chiếu độc lập: `ref_normalize` (9.7), `ref_image_fingerprint` (2.7), sha256 bằng
`hashlib` trực tiếp. Mỗi test “bị bỏ vì X” dựng dãy mảnh chỉ sai đúng ở X.
"""

import ast
import base64
import dataclasses
import hashlib
import inspect
import itertools
import json
import math

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from teleport.core import clipboard as clip_mod
from teleport.core.clipboard import (
    CLIP_ASSEMBLY_TIMEOUT,
    CLIP_BURST,
    CLIP_RATE,
    MAX_CLIP_BYTES,
    MAX_CLIP_MESSAGE_SIZE,
    MAX_IMAGE_SIDE,
    MAX_PNG_BYTES,
    MAX_TEXT_BYTES,
    PNG_FRAGMENT_BYTES,
    PNG_SIGNATURE,
    SENSITIVE_DWORD_ZERO_FORMATS,
    SENSITIVE_PRESENCE_FORMATS,
    ClipAssembler,
    ClipDropped,
    ClipReady,
    ClipTooLarge,
    DropReason,
    LoopGuard,
    image_fingerprint,
    is_sensitive,
    normalize_text,
    split_clip,
    text_fingerprint,
)
from teleport.core.framing import decode_body, encode_message
from teleport.core.messages import Clipboard, ClipKind, MessageError, parse_message, to_dict

MiB = 1024 * 1024
D1, D2, D3 = "a1" * 16, "b2" * 16, "c3" * 16
R = DropReason


def cid(n: int) -> str:
    return f"{n:032x}"


X, Y, Z = cid(0xA), cid(0xB), cid(0xC)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def text_frags(clip_id: str, pieces: list[str], **over) -> list[Clipboard]:
    """Mảnh `text` nhất quán (sha256, total, count đúng) từ các đoạn đã cho; `over` đè trường chung."""
    raw = "".join(pieces).encode("utf-8")
    base = {"clip_id": clip_id, "kind": "text", "sha256": sha(raw), "total": len(raw), "count": len(pieces)}
    base.update(over)
    return [Clipboard(index=i, data=p, **base) for i, p in enumerate(pieces)]


def png_frags(clip_id: str, pieces: list[bytes], **over) -> list[Clipboard]:
    raw = b"".join(pieces)
    base = {
        "clip_id": clip_id,
        "kind": "image/png",
        "sha256": sha(raw),
        "total": len(raw),
        "count": len(pieces),
    }
    base.update(over)
    return [
        Clipboard(index=i, data=base64.b64encode(p).decode("ascii"), **base) for i, p in enumerate(pieces)
    ]


def single(clip_id: str, text: str = "xin chào") -> Clipboard:
    return text_frags(clip_id, [text])[0]


def drop(device: str, clip_id: str, kind: str, reason: DropReason) -> ClipDropped:
    return ClipDropped(device_id=device, clip_id=clip_id, kind=kind, reason=reason)


def feed_all(a: ClipAssembler, device: str, msgs, now: float = 0.0) -> list:
    out: list = []
    for m in msgs:
        out.extend(a.feed(device, m, now))
    return out


def assert_ready(events, device: str, clip_id: str, kind: ClipKind, content) -> ClipReady:
    assert len(events) == 1, events
    (ev,) = events
    assert isinstance(ev, ClipReady), ev
    assert ev.device_id == device
    assert ev.clip_id == clip_id
    assert ev.kind == kind
    assert isinstance(ev.kind, ClipKind)
    assert type(ev.content) is type(content)
    assert ev.content == content
    return ev


def wire_roundtrip(m: Clipboard) -> object:
    return parse_message(decode_body(encode_message(to_dict(m))[4:]))


def ref_normalize(t: str) -> str:
    """Mô hình độc lập của spec 2.3 + 9.7: ghép cặp surrogate, surrogate lẻ → U+FFFD, rồi CRLF → LF."""
    out: list[str] = []
    i = 0
    while i < len(t):
        o = ord(t[i])
        if 0xD800 <= o <= 0xDBFF and i + 1 < len(t) and 0xDC00 <= ord(t[i + 1]) <= 0xDFFF:
            out.append(chr(0x10000 + ((o - 0xD800) << 10) + (ord(t[i + 1]) - 0xDC00)))
            i += 2
            continue
        out.append("\ufffd" if 0xD800 <= o <= 0xDFFF else t[i])
        i += 1
    return "".join(out).replace("\r\n", "\n")


def ref_image_fingerprint(w: int, h: int, fmt: str, pixels: bytes) -> str:
    return sha(
        b"teleport-image-v1\0"
        + w.to_bytes(4, "big")
        + h.to_bytes(4, "big")
        + fmt.encode("ascii")
        + b"\0"
        + pixels
    )


# --- Hằng và ranh giới module -------------------------------------------------------------------


def test_constants_match_spec():
    assert MAX_TEXT_BYTES == 1024 * 1024
    assert MAX_PNG_BYTES == 10 * 1024 * 1024
    assert dict(MAX_CLIP_BYTES) == {"text": 1024 * 1024, "image/png": 10 * 1024 * 1024}
    assert MAX_IMAGE_SIDE == 8192
    assert MAX_CLIP_MESSAGE_SIZE == 768 * 1024
    assert PNG_FRAGMENT_BYTES == 512 * 1024
    assert CLIP_ASSEMBLY_TIMEOUT == 15.0
    assert CLIP_RATE == 5.0
    assert CLIP_BURST == 5
    assert PNG_SIGNATURE == b"\x89PNG\r\n\x1a\n"


def test_drop_reasons_match_spec():
    assert {r.value for r in DropReason} == {
        "duplicate",
        "rate_limited",
        "replaced",
        "unsupported_kind",
        "too_large",
        "inconsistent",
        "orphan",
        "out_of_order",
        "size_mismatch",
        "hash_mismatch",
        "invalid_content",
        "expired",
        "link_lost",
    }


def test_module_imports_stay_sans_io():
    """Spec 2.1: không `time`, không `secrets`, không IO; chỉ dựa vào bốn module `core` được phép."""
    tree = ast.parse(inspect.getsource(clip_mod))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    forbidden = {"time", "secrets", "random", "os", "asyncio", "socket", "threading", "uuid"}
    assert not {n.split(".")[0] for n in names} & forbidden, names
    teleport_imports = {n for n in names if n.startswith("teleport")}
    allowed = {
        "teleport.core.framing",
        "teleport.core.messages",
        "teleport.core.ratelimit",
        "teleport.core.bitfield",
    }
    assert teleport_imports <= allowed, teleport_imports


def test_events_do_not_expose_content():
    ready = ClipReady(device_id=D1, clip_id=X, kind=ClipKind.TEXT, content="BI-MAT-xyz", sha256="0" * 64)
    assert "BI-MAT" not in repr(ready)
    assert {f.name for f in dataclasses.fields(ClipDropped)} == {"device_id", "clip_id", "kind", "reason"}
    with pytest.raises(dataclasses.FrozenInstanceError):
        ready.content = "khác"  # type: ignore[misc]


# --- normalize_text (2.3, 9.7) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\ud800", "\ufffd"),
        ("\udc00", "\ufffd"),
        ("\udc00\ud800", "\ufffd\ufffd"),
        ("\ud800\U00010000", "\ufffd\U00010000"),
        ("abc\ud83d", "abc\ufffd"),
        # Cặp surrogate dựng bằng chr(): hai escape surrogate liền nhau trong literal đã được Python ghép.
        (chr(0xD83D) + chr(0xDE00), "😀"),
        (chr(0xD800) + chr(0xDC00), "\U00010000"),
        (chr(0xDBFF) + chr(0xDFFF), "\U0010ffff"),
        (chr(0xD83D) + chr(0xDE00) + chr(0xDE00), "😀\ufffd"),
        ("a" + chr(0xD83D) + chr(0xDE00) + "b" + chr(0xD83D), "a😀b\ufffd"),
        ("\ufeffabc", "\ufeffabc"),
        ("a\x00b", "a\x00b"),
        ("\x00", "\x00"),
        ("a\rb", "a\rb"),
        ("\r", "\r"),
        ("a\r\nb", "a\nb"),
        ("\r\n\r\n", "\n\n"),
        ("\n\r", "\n\r"),
        ("\r\r\n", "\r\n"),
        ("\x01\x1f\x7f\u2028\u2029\x9f", "\x01\x1f\x7f\u2028\u2029\x9f"),
        ("\ud83d\r\n", "\ufffd\n"),
        ("", ""),
        ("Tiếng Việt có dấu", "Tiếng Việt có dấu"),
    ],
    ids=repr,
)
def test_normalize_text_examples(text, expected):
    assert normalize_text(text) == expected


SPECIAL_PIECES = [
    "\ud800",
    "\udbff",
    "\udc00",
    "\udfff",
    "\ud83d",
    "\ude00",
    "\r",
    "\n",
    "\r\n",
    "\x00",
    "\x01",
    "\x1f",
    "\x7f",
    '"',
    "\\",
    "😀",
    "𐀀",
    "\u2028",
    "\ufeff",
    "\ufffd",
    "a",
    "é",
]
any_text = st.lists(
    st.one_of(st.sampled_from(SPECIAL_PIECES), st.characters(), st.characters(categories=["Cs"])),
    max_size=40,
).map("".join)


@pytest.mark.slow
@settings(max_examples=500, deadline=None)
@given(t=any_text)
def test_property_normalize_text_matches_reference(t):
    n = normalize_text(t)
    assert n == ref_normalize(t)
    # Kết quả mã hóa UTF-8 được (không còn surrogate nào).
    n.encode("utf-8")


# --- Fingerprint (2.7) -------------------------------------------------------------------------


def test_text_fingerprint_is_sha256_of_normalized_utf8():
    assert text_fingerprint("abc") == sha(b"abc")
    assert text_fingerprint("a\r\nb") == text_fingerprint("a\nb") == sha(b"a\nb")
    assert text_fingerprint("a\rb") != text_fingerprint("a\nb")
    assert text_fingerprint("\ud800") == sha("\ufffd".encode())
    assert text_fingerprint("a\x00") != text_fingerprint("a")


@settings(max_examples=300, deadline=None)
@given(t=any_text)
def test_property_text_fingerprint_equals_wire_sha256(t):
    assume(normalize_text(t) != "")
    msgs = split_clip(X, "text", t)
    assert text_fingerprint(t) == sha(ref_normalize(t).encode("utf-8"))
    assert all(m.sha256 == text_fingerprint(t) for m in msgs)


def test_image_fingerprint_matches_reference_and_is_deterministic():
    px = bytes(range(256)) * 3
    fp = image_fingerprint(16, 12, "RGBA8888", px)
    assert fp == image_fingerprint(16, 12, "RGBA8888", px)
    assert fp == ref_image_fingerprint(16, 12, "RGBA8888", px)
    assert len(fp) == 64 and fp == fp.lower()


def test_image_fingerprint_is_sensitive_to_every_input():
    px = bytes(range(256)) * 3
    base = image_fingerprint(16, 12, "RGBA8888", px)
    variants = {
        image_fingerprint(12, 16, "RGBA8888", px),  # đổi chỗ rộng/cao, cùng số pixel
        image_fingerprint(16, 13, "RGBA8888", px),
        image_fingerprint(17, 12, "RGBA8888", px),
        image_fingerprint(16, 12, "ARGB32", px),
        image_fingerprint(16, 12, "RGBA8888", px[:-1] + b"\x00"),
        image_fingerprint(16, 12, "RGBA8888", px + b"\x00"),
    }
    assert base not in variants
    assert len(variants) == 6


def test_image_fingerprint_fields_are_not_ambiguous():
    # Tên định dạng kết thúc bằng NUL: không thể dồn byte pixel vào tên định dạng.
    assert image_fingerprint(1, 1, "AB", b"C") != image_fingerprint(1, 1, "A", b"BC")


@pytest.mark.parametrize("side", [1, MAX_IMAGE_SIDE])
def test_image_fingerprint_accepts_side_bounds(side):
    assert image_fingerprint(side, side, "RGBA8888", b"") == ref_image_fingerprint(
        side, side, "RGBA8888", b""
    )


@pytest.mark.parametrize(("w", "h"), [(0, 1), (1, 0), (-1, 1), (1, -5), (MAX_IMAGE_SIDE + 1, 1), (1, 8193)])
def test_image_fingerprint_rejects_bad_size(w, h):
    with pytest.raises(ValueError):
        image_fingerprint(w, h, "RGBA8888", b"\x00" * 4)


# --- LoopGuard (2.7) ---------------------------------------------------------------------------


def test_loop_guard_starts_empty():
    g = LoopGuard()
    assert g.is_current("f" * 64) is False
    assert g.local_changed("f" * 64) is True
    assert g.is_current("f" * 64) is True


def test_loop_guard_has_no_clock():
    for name in ("local_changed", "remote_written", "is_current", "reset"):
        assert "now" not in inspect.signature(getattr(LoopGuard, name)).parameters


def test_loop_guard_two_machines_no_echo():
    a, b = LoopGuard(), LoopGuard()
    t, u = text_fingerprint("văn bản A"), text_fingerprint("văn bản B")
    # A: người dùng chép t → gửi.
    assert a.local_changed(t) is True
    # B nhận t, ghi vào clipboard; trình theo dõi của B báo thay đổi → không gửi ngược.
    b.remote_written(t)
    assert b.is_current(t)
    assert b.local_changed(t) is False
    # A thấy lại chính t (ví dụ OS báo hai lần) → không gửi lại.
    assert a.local_changed(t) is False
    # Người dùng ở B chép u → gửi; A nhận, ghi, không vọng lại.
    assert b.local_changed(u) is True
    assert b.is_current(t) is False
    a.remote_written(u)
    assert a.local_changed(u) is False
    # A chép lại t (đã cũ) → là thay đổi mới, gửi.
    assert a.local_changed(t) is True


def test_loop_guard_three_machines_no_forwarding():
    a, b, c = LoopGuard(), LoopGuard(), LoopGuard()
    t = text_fingerprint("từ A")
    assert a.local_changed(t) is True  # A gửi thẳng cho B và C
    for g in (b, c):
        g.remote_written(t)
    # B và C không chuyển tiếp cho nhau, không gửi ngược về A.
    assert b.local_changed(t) is False
    assert c.local_changed(t) is False
    # C chép u → gửi cho A và B; không ai vọng lại.
    u = text_fingerprint("từ C")
    assert c.local_changed(u) is True
    a.remote_written(u)
    b.remote_written(u)
    assert a.local_changed(u) is False
    assert b.local_changed(u) is False
    assert a.is_current(t) is False


def test_loop_guard_remote_written_replaces_set_with_all_given():
    g = LoopGuard()
    x, y, z = "1" * 64, "2" * 64, "3" * 64
    g.remote_written(x, y)
    assert g.is_current(x) and g.is_current(y)
    assert g.local_changed(y) is False
    g.remote_written(z)
    assert not g.is_current(x) and not g.is_current(y) and g.is_current(z)
    assert g.local_changed(x) is True
    assert g.is_current(x) and not g.is_current(z)


def test_loop_guard_reset():
    g = LoopGuard()
    g.remote_written("1" * 64)
    g.reset()
    assert g.is_current("1" * 64) is False
    assert g.local_changed("1" * 64) is True


# --- is_sensitive (2.8) ------------------------------------------------------------------------


class ValueOf:
    """`value_of` giả: ghi lại mọi tên được hỏi."""

    def __init__(self, values: dict | None = None) -> None:
        self.values = values or {}
        self.calls: list[str] = []

    def __call__(self, name: str) -> bytes | None:
        self.calls.append(name)
        return self.values.get(name)


def qt(name: str) -> str:
    return f'application/x-qt-windows-mime;value="{name}"'


def test_sensitive_format_sets_match_spec():
    assert frozenset(SENSITIVE_PRESENCE_FORMATS) == {
        "ExcludeClipboardContentFromMonitorProcessing",
        "org.nspasteboard.ConcealedType",
        "x-kde-passwordManagerHint",
    }
    assert frozenset(SENSITIVE_DWORD_ZERO_FORMATS) == {
        "CanIncludeInClipboardHistory",
        "CanUploadToCloudClipboard",
    }


PRESENCE_NAMES = [
    "ExcludeClipboardContentFromMonitorProcessing",
    "excludeclipboardcontentfrommonitorprocessing",
    "EXCLUDECLIPBOARDCONTENTFROMMONITORPROCESSING",
    qt("ExcludeClipboardContentFromMonitorProcessing"),
    qt("excludeClipboardContentFromMonitorProcessing"),
    "org.nspasteboard.ConcealedType",
    "ORG.NSPASTEBOARD.CONCEALEDTYPE",
    "x-kde-passwordManagerHint",
    "X-KDE-PASSWORDMANAGERHINT",
    'application/x-qt-image-mime;value="org.nspasteboard.ConcealedType"',
]


@pytest.mark.parametrize("name", PRESENCE_NAMES)
def test_presence_format_is_sensitive_without_reading_value(name):
    v = ValueOf()
    assert is_sensitive(["text/plain", name], v) is True
    assert v.calls == []


def test_presence_wins_over_dword_without_reading_value():
    v = ValueOf({"CanIncludeInClipboardHistory": b"\x01\0\0\0"})
    assert is_sensitive(["CanIncludeInClipboardHistory", "x-kde-passwordManagerHint"], v) is True
    assert v.calls == []


DWORD_NAMES = [
    "CanIncludeInClipboardHistory",
    "canincludeinclipboardhistory",
    qt("CanIncludeInClipboardHistory"),
    qt("CANINCLUDEINCLIPBOARDHISTORY"),
    "CanUploadToCloudClipboard",
    qt("CanUploadToCloudClipboard"),
]


@pytest.mark.parametrize("name", DWORD_NAMES)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (b"\0\0\0\0", True),
        (b"\x01\0\0\0", False),
        (b"\0\0\0\x01", False),  # little-endian: 0x01000000 ≠ 0
        (b"\xff\xff\xff\xff", False),
        (None, True),  # không đọc được → coi là nhạy cảm (nguyên tắc 2)
        (b"", True),
        (b"\0", True),
        (b"\x01\0\0", True),  # < 4 byte, dù khác 0
        (b"\0\0\0\0\x01", True),  # chỉ xét 4 byte đầu
        (b"\x01\0\0\0\0", False),
    ],
    ids=repr,
)
def test_dword_zero_format(name, value, expected):
    v = ValueOf({name: value})
    assert is_sensitive(["text/plain", name], v) is expected
    assert v.calls == [name], "value_of phải được gọi với tên đúng như trong formats"


def test_any_dword_zero_format_is_enough():
    v = ValueOf({"CanIncludeInClipboardHistory": b"\x01\0\0\0", "CanUploadToCloudClipboard": b"\0\0\0\0"})
    assert is_sensitive(["CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"], v) is True
    v2 = ValueOf({"CanIncludeInClipboardHistory": b"\x01\0\0\0", "CanUploadToCloudClipboard": b"\x02\0\0\0"})
    assert is_sensitive(["CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"], v2) is False
    assert sorted(v2.calls) == ["CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"]


@pytest.mark.parametrize(
    "formats",
    [
        [],
        ["text/plain"],
        ["image/png"],
        ["text/plain", "text/html", "image/png", 'application/x-qt-windows-mime;value="Rich Text Format"'],
        ["MyExcludeClipboardContentFromMonitorProcessing"],
        ["ExcludeClipboardContentFromMonitorProcessingX"],
        ['x-application/x-qt-windows-mime;value="ExcludeClipboardContentFromMonitorProcessing"'],
        ['application/x-qt-windows-mime;value="ExcludeClipboardContentFromMonitorProcessing";x'],
        [" ExcludeClipboardContentFromMonitorProcessing"],
    ],
    ids=repr,
)
def test_ordinary_formats_are_not_sensitive_and_value_of_not_called(formats):
    v = ValueOf()
    assert is_sensitive(formats, v) is False
    assert v.calls == []


def test_formats_may_be_any_iterable():
    v = ValueOf()
    assert is_sensitive(iter(["text/plain", "org.nspasteboard.ConcealedType"]), v) is True
    assert is_sensitive((f for f in ["text/plain"]), v) is False


def test_value_of_exception_propagates():
    def boom(name):
        raise RuntimeError("lỗi đọc")

    with pytest.raises(RuntimeError):
        is_sensitive(["CanUploadToCloudClipboard"], boom)


# --- split_clip: lỗi đầu vào (2.5, 9.3) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "content"),
    [
        ("text", b"abc"),
        ("text", 123),
        ("image/png", "abc"),
        ("text/html", "abc"),
        ("image/jpeg", PNG_SIGNATURE),
        ("TEXT", "abc"),
        ("image/png", b"not a png"),
        ("image/png", PNG_SIGNATURE[:-1]),
        ("image/png", b"\x00" + PNG_SIGNATURE),
    ],
    ids=repr,
)
def test_split_rejects_bad_input(kind, content):
    with pytest.raises(ValueError) as info:
        split_clip(X, kind, content)
    assert not isinstance(info.value, ClipTooLarge)
    assert not isinstance(info.value, MessageError)


@pytest.mark.parametrize(("kind", "content"), [("text", ""), ("image/png", b"")], ids=repr)
def test_split_rejects_empty(kind, content):
    with pytest.raises(ValueError) as info:
        split_clip(X, kind, content)
    assert not isinstance(info.value, ClipTooLarge)


def test_split_bad_clip_id_raises_message_error():
    with pytest.raises(MessageError):
        split_clip("X" * 32, "text", "abc")


def test_clip_too_large_is_value_error_with_fields():
    assert issubclass(ClipTooLarge, ValueError)
    with pytest.raises(ClipTooLarge) as info:
        split_clip(X, "text", "a" * (MAX_TEXT_BYTES + 1))
    e = info.value
    assert (e.kind, e.size, e.limit) == ("text", MAX_TEXT_BYTES + 1, MAX_TEXT_BYTES)


def test_text_one_byte_over_after_normalization_is_too_large():
    # 4 byte mỗi emoji: MAX/4 emoji = đúng trần; thêm 1 byte ASCII → vượt 1 byte.
    with pytest.raises(ClipTooLarge) as info:
        split_clip(X, "text", "😀" * (MAX_TEXT_BYTES // 4) + "a")
    assert (info.value.size, info.value.limit) == (MAX_TEXT_BYTES + 1, MAX_TEXT_BYTES)


def test_text_crlf_counts_after_normalization():
    # 2 MiB ký tự nhưng chuẩn hóa ra đúng 1 MiB: không vượt trần (len == 2 * MAX, kiểm nhanh R3 không áp).
    t = "\r\n" * MAX_TEXT_BYTES
    assert len(t) == 2 * MAX_TEXT_BYTES
    msgs = split_clip(X, "text", t)
    assert msgs[0].total == MAX_TEXT_BYTES
    assert "".join(m.data for m in msgs) == "\n" * MAX_TEXT_BYTES


def test_text_fast_size_check_uses_code_point_length():
    # R3 (9.3): len(text) > 2 * MAX → ClipTooLarge(size=len(text)) trước khi chuẩn hóa.
    t = "\r\n" * MAX_TEXT_BYTES + "\n"
    with pytest.raises(ClipTooLarge) as info:
        split_clip(X, "text", t)
    assert info.value.size == len(t) == 2 * MAX_TEXT_BYTES + 1
    assert info.value.limit == MAX_TEXT_BYTES


def test_png_one_byte_over_is_too_large():
    content = PNG_SIGNATURE + b"\0" * (MAX_PNG_BYTES - len(PNG_SIGNATURE) + 1)
    with pytest.raises(ClipTooLarge) as info:
        split_clip(X, "image/png", content)
    e = info.value
    assert (e.kind, e.size, e.limit) == ("image/png", MAX_PNG_BYTES + 1, MAX_PNG_BYTES)


# --- split_clip: hình dạng mảnh ----------------------------------------------------------------


def check_split(msgs: list[Clipboard], kind: str, raw: bytes) -> None:
    """Bất biến của 2.5: cùng trường chung, index 0…n−1, khung ≤ trần, round-trip qua khung."""
    assert msgs, "phải có ít nhất một mảnh"
    for i, m in enumerate(msgs):
        assert type(m) is Clipboard
        assert m.index == i
        assert m.count == len(msgs)
        assert m.clip_id == msgs[0].clip_id
        assert type(m.kind) is str and m.kind == kind
        assert m.sha256 == sha(raw)
        assert m.total == len(raw)
        assert m.data != ""
        frame = encode_message(to_dict(m))
        assert len(frame) <= MAX_CLIP_MESSAGE_SIZE, f"mảnh {i}: khung {len(frame)} byte"
        assert len(frame) <= 768 * 1024
        assert wire_roundtrip(m) == m


def test_split_short_text_is_one_fragment():
    msgs = split_clip(X, ClipKind.TEXT, "xin chào\r\nthế giới")
    norm = "xin chào\nthế giới"
    check_split(msgs, "text", norm.encode())
    assert len(msgs) == 1 and msgs[0].data == norm and msgs[0].clip_id == X


def test_split_png_cuts_raw_bytes_and_pads_each_fragment():
    content = PNG_SIGNATURE + bytes(range(256)) * ((3 * PNG_FRAGMENT_BYTES) // 256) + b"\x01" * 7
    msgs = split_clip(X, "image/png", content)
    check_split(msgs, "image/png", content)
    assert len(msgs) == math.ceil(len(content) / PNG_FRAGMENT_BYTES) == 4
    for i, m in enumerate(msgs):
        chunk = content[i * PNG_FRAGMENT_BYTES : (i + 1) * PNG_FRAGMENT_BYTES]
        assert m.data == base64.b64encode(chunk).decode("ascii")


@pytest.mark.parametrize(
    ("size", "count"),
    [
        (len(PNG_SIGNATURE), 1),
        (PNG_FRAGMENT_BYTES, 1),
        (PNG_FRAGMENT_BYTES + 1, 2),
        (2 * PNG_FRAGMENT_BYTES, 2),
    ],
)
def test_split_png_fragment_count_at_boundaries(size, count):
    content = PNG_SIGNATURE + b"\x07" * (size - len(PNG_SIGNATURE))
    msgs = split_clip(X, "image/png", content)
    assert len(msgs) == count
    assert b"".join(base64.b64decode(m.data) for m in msgs) == content


def assemble_ok(msgs: list[Clipboard], kind: ClipKind, content) -> ClipReady:
    a = ClipAssembler()
    events: list = []
    for m in msgs[:-1]:
        assert a.feed(D1, m, 0.0) == ()
    events.extend(a.feed(D1, msgs[-1], 0.0))
    ev = assert_ready(events, D1, msgs[0].clip_id, kind, content)
    assert a.buffered_bytes(D1) == 0
    return ev


# 5.3: text đúng 1 MiB theo nhiều kiểu thoát JSON khác nhau.
BIG_TEXTS = {
    "all-0x01": "\x01" * MAX_TEXT_BYTES,
    "all-quote": '"' * MAX_TEXT_BYTES,
    "all-backslash": "\\" * MAX_TEXT_BYTES,
    "all-newline": "\n" * MAX_TEXT_BYTES,
    "all-emoji": "😀" * (MAX_TEXT_BYTES // 4),
    "c0-then-ascii": "\x01" * (MAX_TEXT_BYTES // 2) + "a" * (MAX_TEXT_BYTES // 2),
    "ascii-then-c0": "a" * (MAX_TEXT_BYTES // 2) + "\x1f" * (MAX_TEXT_BYTES // 2),
    "all-ascii": "a" * MAX_TEXT_BYTES,
    "u2028": "\u2028" * (MAX_TEXT_BYTES // 3) + "a",
    "mixed": ('\x00é😀"\\\t\u2028a' * (MAX_TEXT_BYTES // 20))[: MAX_TEXT_BYTES // 4],
}


@pytest.mark.parametrize("name", list(BIG_TEXTS))
def test_big_text_fragments_fit_and_reassemble(name):
    t = BIG_TEXTS[name]
    raw = normalize_text(t).encode("utf-8")
    assert len(raw) <= MAX_TEXT_BYTES
    msgs = split_clip(X, "text", t)
    check_split(msgs, "text", raw)
    assert "".join(m.data for m in msgs) == normalize_text(t)
    assemble_ok(msgs, ClipKind.TEXT, normalize_text(t))


def test_png_exactly_ten_mib_fits_and_reassembles():
    content = PNG_SIGNATURE + bytes(range(256)) * ((MAX_PNG_BYTES - len(PNG_SIGNATURE)) // 256)
    content += b"\xee" * (MAX_PNG_BYTES - len(content))
    assert len(content) == MAX_PNG_BYTES
    msgs = split_clip(X, "image/png", content)
    check_split(msgs, "image/png", content)
    assert len(msgs) == MAX_PNG_BYTES // PNG_FRAGMENT_BYTES
    assemble_ok(msgs, ClipKind.PNG, content)


# --- 5.2: chia rồi ráp (hypothesis) ------------------------------------------------------------


@pytest.mark.slow
@settings(max_examples=300, deadline=None)
@given(t=any_text)
def test_property_text_split_then_assemble(t):
    norm = ref_normalize(t)
    assume(norm != "")
    msgs = split_clip(X, "text", t)
    check_split(msgs, "text", norm.encode("utf-8"))
    on_wire = "".join(m.data for m in msgs)
    assert on_wire == normalize_text(t) == norm
    a = ClipAssembler()
    events = feed_all(a, D1, msgs)
    # 5.2 (9.13 thay 9.1): bên nhận không chuẩn hóa lại, nên content == normalize_text(t) với mọi t.
    ev = assert_ready(events, D1, X, ClipKind.TEXT, normalize_text(t))
    assert ev.sha256 == text_fingerprint(t)


@pytest.mark.parametrize("t", ["a\r\r\nb", "\r\r\n", "\r\r\r\n\r", "x\r\r\n\r\r\ny"], ids=repr)
def test_cr_before_crlf_survives_send_and_receive(t):
    # Chuẩn hóa không lũy đẳng ("\r\r\n" → "\r\n"): bên nhận chuẩn hóa lại sẽ làm mất "\r" (9.13).
    events = feed_all(ClipAssembler(), D1, split_clip(X, "text", t))
    ev = assert_ready(events, D1, X, ClipKind.TEXT, normalize_text(t))
    assert "\r\n" in ev.content


@settings(max_examples=300, deadline=None)
@given(body=st.binary(max_size=2048))
def test_property_png_split_then_assemble(body):
    content = PNG_SIGNATURE + body
    msgs = split_clip(X, "image/png", content)
    check_split(msgs, "image/png", content)
    ev = assemble_ok(msgs, ClipKind.PNG, content)
    assert ev.sha256 == sha(content)


# Văn bản lớn để thật sự bị cắt: vài đoạn lặp ký tự “nặng” (thoát 2–6 byte, UTF-8 3–4 byte).
HEAVY = ["\x01", "\x1f", '"', "\\", "\n", "\t", "😀", "\u2028", "é", "a", "\x7f", "\r\n", "\ud800"]
run_length = st.integers(1, 300_000) | st.integers(100_000, 300_000)
heavy_text = st.lists(st.tuples(st.sampled_from(HEAVY), run_length), min_size=1, max_size=6).map(
    lambda runs: "".join(ch * n for ch, n in runs)
)


@pytest.mark.slow
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(t=heavy_text)
def test_property_large_text_fragments_fit_frame_limit(t):
    norm = normalize_text(t)
    raw = norm.encode("utf-8")
    if len(raw) > MAX_TEXT_BYTES:
        with pytest.raises(ClipTooLarge):
            split_clip(X, "text", t)
        return
    msgs = split_clip(X, "text", t)
    check_split(msgs, "text", raw)
    assert "".join(m.data for m in msgs) == norm
    # Mỗi mảnh: cỡ mã hóa của `data` tính độc lập bằng json.dumps (9.2), cộng phần vỏ, vẫn ≤ trần.
    for m in msgs:
        data_len = len(json.dumps(m.data, ensure_ascii=False).encode("utf-8"))
        assert data_len <= MAX_CLIP_MESSAGE_SIZE
    events = feed_all(ClipAssembler(), D1, msgs)
    assert len(events) == 1 and isinstance(events[0], ClipReady)
    assert events[0].sha256 == sha(raw)


# --- Ráp: đường đúng (bảng 2.6 dòng 7, 15, 16) -------------------------------------------------


def test_middle_fragments_return_nothing_and_buffer_grows():
    a = ClipAssembler()
    f = text_frags(X, ["ab", "cde", "f"])
    assert a.feed(D1, f[0], 0.0) == ()
    assert a.buffered_bytes(D1) == 2
    assert a.feed(D1, f[1], 0.0) == ()
    assert a.buffered_bytes(D1) == 5
    assert a.buffered_bytes() == 5
    assert_ready(a.feed(D1, f[2], 0.0), D1, X, ClipKind.TEXT, "abcdef")
    assert a.buffered_bytes(D1) == 0


def test_feed_returns_tuple():
    a = ClipAssembler()
    assert isinstance(a.feed(D1, text_frags(X, ["a", "b"])[0], 0.0), tuple)
    assert isinstance(a.feed(D1, single(Y), 0.0), tuple)
    assert isinstance(a.tick(0.0), tuple)
    assert isinstance(a.link_lost(D1), tuple)


def test_ready_text_carries_wire_sha_and_utf8_content():
    a = ClipAssembler()
    f = text_frags(X, ["Tiếng ", "Việt 😀"])
    feed_all(a, D1, f[:1])
    ev = assert_ready(a.feed(D1, f[1], 0.0), D1, X, ClipKind.TEXT, "Tiếng Việt 😀")
    assert ev.sha256 == f[0].sha256


@pytest.mark.parametrize("pieces", [["a\r\nb"], ["a\r", "\nb"], ["\r\n\r\n"]], ids=repr)
def test_ready_text_from_non_normalizing_sender_is_kept_verbatim(pieces):
    # 9.13: máy kia không chuẩn hóa mà sha256 đúng byte gửi → content giữ nguyên "\r\n", không chuẩn
    # hóa lại; vì vậy LoopGuard phải dùng text_fingerprint(content), không dùng ClipReady.sha256.
    raw = "".join(pieces)
    f = text_frags(X, pieces)
    ev = assert_ready(feed_all(ClipAssembler(), D1, f), D1, X, ClipKind.TEXT, raw)
    assert ev.sha256 == sha(raw.encode("utf-8"))
    assert text_fingerprint(ev.content) != ev.sha256
    assert text_fingerprint(ev.content) == sha(raw.replace("\r\n", "\n").encode("utf-8"))


def test_ready_text_keeps_nul_and_control_chars():
    content = "a\x00b\x01\x1f\x7f\rc"
    assert_ready(feed_all(ClipAssembler(), D1, text_frags(X, [content])), D1, X, ClipKind.TEXT, content)


def test_ready_png_returns_original_bytes():
    content = PNG_SIGNATURE + bytes(range(256))
    f = png_frags(X, [content[:5], content[5:100], content[100:]])
    ev = assert_ready(feed_all(ClipAssembler(), D1, f), D1, X, ClipKind.PNG, content)
    assert ev.sha256 == sha(content)


def test_png_fragments_of_any_size_are_accepted():
    # Bên nhận không giả định cỡ mảnh (protocol §3.3): mảnh 1 byte vẫn ráp được.
    content = PNG_SIGNATURE + b"xyz"
    f = png_frags(X, [content[i : i + 1] for i in range(len(content))])
    assert_ready(feed_all(ClipAssembler(), D1, f), D1, X, ClipKind.PNG, content)


def test_same_clip_sent_again_after_completion_is_ready_again():
    a = ClipAssembler()
    assert_ready(a.feed(D1, single(X, "abc"), 0.0), D1, X, ClipKind.TEXT, "abc")
    assert_ready(a.feed(D1, single(X, "abc"), 1.0), D1, X, ClipKind.TEXT, "abc")


def test_devices_assemble_independently():
    a = ClipAssembler()
    f1 = text_frags(X, ["11", "111"])
    f2 = png_frags(X, [PNG_SIGNATURE, b"22"])
    assert a.feed(D1, f1[0], 0.0) == ()
    assert a.feed(D2, f2[0], 0.0) == ()
    assert a.buffered_bytes(D1) == 2
    assert a.buffered_bytes(D2) == len(PNG_SIGNATURE)
    assert a.buffered_bytes() == 2 + len(PNG_SIGNATURE)
    assert a.buffered_bytes(D3) == 0
    assert_ready(a.feed(D2, f2[1], 0.0), D2, X, ClipKind.PNG, PNG_SIGNATURE + b"22")
    assert_ready(a.feed(D1, f1[1], 0.0), D1, X, ClipKind.TEXT, "11111")
    assert a.buffered_bytes() == 0


# --- Ráp: các đường bỏ clip (bảng 2.6) ---------------------------------------------------------


def test_row0_expired_on_feed_at_exact_boundary():
    a = ClipAssembler()
    f = text_frags(X, ["ab", "cd"])
    assert a.feed(D1, f[0], 0.0) == ()
    assert a.feed(D1, f[1], 15.0) == (drop(D1, X, "text", R.EXPIRED),)
    assert a.buffered_bytes(D1) == 0


def test_row0_not_expired_just_before_boundary():
    a = ClipAssembler()
    f = text_frags(X, ["ab", "cd"])
    a.feed(D1, f[0], 1.0)
    assert_ready(a.feed(D1, f[1], 15.75), D1, X, ClipKind.TEXT, "abcd")


def test_row0_timeout_counts_from_index_zero_not_last_fragment():
    a = ClipAssembler()
    f = text_frags(X, ["a", "b", "c"])
    assert a.feed(D1, f[0], 1.0) == ()
    assert a.feed(D1, f[1], 10.0) == ()
    assert a.feed(D1, f[2], 16.0) == (drop(D1, X, "text", R.EXPIRED),)
    assert a.buffered_bytes(D1) == 0


def test_row0_expired_then_new_clip_is_processed_in_same_feed():
    a = ClipAssembler()
    a.feed(D1, text_frags(X, ["ab", "cd"])[0], 1.0)
    events = a.feed(D1, single(Y, "mới"), 16.0)
    assert len(events) == 2
    assert events[0] == drop(D1, X, "text", R.EXPIRED)
    assert_ready(events[1:], D1, Y, ClipKind.TEXT, "mới")


def test_row0_expired_via_tick_at_exact_boundary():
    a = ClipAssembler()
    f = png_frags(X, [PNG_SIGNATURE, b"rest"])
    assert a.feed(D1, f[0], 2.0) == ()
    wake = a.next_wakeup()
    assert wake is not None and wake <= 17.0
    assert a.tick(16.75) == ()
    assert a.buffered_bytes(D1) == len(PNG_SIGNATURE)
    assert a.tick(17.0) == (drop(D1, X, "image/png", R.EXPIRED),)
    assert a.buffered_bytes(D1) == 0
    assert a.tick(17.0) == ()


def test_row0_custom_timeout():
    a = ClipAssembler(timeout=0.5)
    f = text_frags(X, ["ab", "cd"])
    a.feed(D1, f[0], 0.0)
    assert a.feed(D1, f[1], 0.5) == (drop(D1, X, "text", R.EXPIRED),)


def test_next_wakeup_none_when_idle():
    a = ClipAssembler()
    assert a.next_wakeup() is None
    a.feed(D1, single(X), 0.0)
    a.tick(1000.0)
    assert a.next_wakeup() is None


def test_tick_expires_every_device():
    a = ClipAssembler()
    a.feed(D1, text_frags(X, ["a", "b"])[0], 0.0)
    a.feed(D2, text_frags(Y, ["a", "b"])[0], 5.0)
    assert a.tick(15.0) == (drop(D1, X, "text", R.EXPIRED),)
    assert a.buffered_bytes(D2) == 1
    assert a.tick(20.0) == (drop(D2, Y, "text", R.EXPIRED),)
    assert a.buffered_bytes() == 0


def test_row1_duplicate_index_zero_drops_clip():
    a = ClipAssembler()
    f = text_frags(X, ["ab", "cd"])
    assert a.feed(D1, f[0], 0.0) == ()
    assert a.feed(D1, f[0], 0.0) == (drop(D1, X, "text", R.DUPLICATE),)
    assert a.buffered_bytes(D1) == 0
    assert a.feed(D1, f[1], 0.0) == ()  # 9.4: mảnh còn lại của clip vừa bỏ bị bỏ im lặng
    assert a.buffered_bytes(D1) == 0


def test_row3_new_clip_replaces_clip_in_progress():
    a = ClipAssembler()
    fx = text_frags(X, ["ab", "cd"])
    a.feed(D1, fx[0], 0.0)
    events = a.feed(D1, single(Y, "y"), 1.0)
    assert len(events) == 2
    assert events[0] == drop(D1, X, "text", R.REPLACED)
    assert_ready(events[1:], D1, Y, ClipKind.TEXT, "y")
    assert a.feed(D1, fx[1], 1.0) == ()
    assert a.buffered_bytes(D1) == 0


def test_row3_replaced_then_new_clip_rejected():
    a = ClipAssembler()
    a.feed(D1, png_frags(X, [PNG_SIGNATURE, b"x"])[0], 0.0)
    html = Clipboard(clip_id=Y, kind="text/html", sha256="0" * 64, total=3, index=0, count=1, data="<b>")
    assert a.feed(D1, html, 1.0) == (
        drop(D1, X, "image/png", R.REPLACED),
        drop(D1, Y, "text/html", R.UNSUPPORTED_KIND),
    )
    assert a.buffered_bytes(D1) == 0


def test_row4_unsupported_kind():
    a = ClipAssembler()
    html = Clipboard(clip_id=X, kind="text/html", sha256="0" * 64, total=3, index=0, count=1, data="<b>")
    assert a.feed(D1, html, 0.0) == (drop(D1, X, "text/html", R.UNSUPPORTED_KIND),)
    assert a.buffered_bytes(D1) == 0


def test_row4_kind_missing_from_custom_limits_is_unsupported():
    a = ClipAssembler(limits={"text": MAX_TEXT_BYTES})
    f = png_frags(X, [PNG_SIGNATURE])
    assert a.feed(D1, f[0], 0.0) == (drop(D1, X, "image/png", R.UNSUPPORTED_KIND),)
    assert_ready(a.feed(D1, single(Y, "ok"), 0.0), D1, Y, ClipKind.TEXT, "ok")


@pytest.mark.parametrize(
    "limits",
    [{"text/html": 10}, {"text": 10, "image/png": 10, "image/jpeg": 10}, {"TEXT": 10}],
    ids=repr,
)
def test_limits_with_unknown_kind_is_value_error(limits):
    with pytest.raises(ValueError):
        ClipAssembler(limits=limits)


def test_row5_too_large_text():
    a = ClipAssembler()
    m = Clipboard(
        clip_id=X, kind="text", sha256="0" * 64, total=MAX_TEXT_BYTES + 1, index=0, count=2, data="a"
    )
    assert a.feed(D1, m, 0.0) == (drop(D1, X, "text", R.TOO_LARGE),)
    assert a.buffered_bytes(D1) == 0


def test_row5_text_exactly_at_cap_is_accepted():
    a = ClipAssembler()
    m = Clipboard(clip_id=X, kind="text", sha256="0" * 64, total=MAX_TEXT_BYTES, index=0, count=2, data="a")
    assert a.feed(D1, m, 0.0) == ()
    assert a.buffered_bytes(D1) == 1


def test_row5_too_large_png():
    a = ClipAssembler()
    data = base64.b64encode(PNG_SIGNATURE).decode()
    m = Clipboard(
        clip_id=X, kind="image/png", sha256="0" * 64, total=MAX_PNG_BYTES + 1, index=0, count=2, data=data
    )
    assert a.feed(D1, m, 0.0) == (drop(D1, X, "image/png", R.TOO_LARGE),)
    ok = Clipboard(
        clip_id=Y, kind="image/png", sha256="0" * 64, total=MAX_PNG_BYTES, index=0, count=2, data=data
    )
    assert a.feed(D1, ok, 1.0) == ()


def test_row5_custom_limits():
    a = ClipAssembler(limits={"text": 10, "image/png": 20})
    assert a.feed(D1, single(X, "a" * 11), 0.0) == (drop(D1, X, "text", R.TOO_LARGE),)
    assert_ready(a.feed(D1, single(Y, "b" * 10), 0.0), D1, Y, ClipKind.TEXT, "b" * 10)


def test_row6_count_greater_than_total():
    a = ClipAssembler()
    m = Clipboard(clip_id=X, kind="text", sha256="0" * 64, total=2, index=0, count=3, data="a")
    assert a.feed(D1, m, 0.0) == (drop(D1, X, "text", R.INCONSISTENT),)
    assert a.buffered_bytes(D1) == 0


def test_row8_orphan_without_clip_in_progress():
    a = ClipAssembler()
    f = text_frags(X, ["ab", "cd"])
    assert a.feed(D1, f[1], 0.0) == (drop(D1, X, "text", R.ORPHAN),)
    assert a.buffered_bytes(D1) == 0


def test_row8_orphan_does_not_damage_clip_in_progress():
    a = ClipAssembler()
    fx = text_frags(X, ["ab", "cd", "ef"])
    fz = png_frags(Z, [PNG_SIGNATURE, b"zz"])
    a.feed(D1, fx[0], 0.0)
    assert a.feed(D1, fz[1], 0.0) == (drop(D1, Z, "image/png", R.ORPHAN),)
    assert a.buffered_bytes(D1) == 2
    assert a.feed(D1, fx[1], 0.0) == ()
    assert_ready(a.feed(D1, fx[2], 0.0), D1, X, ClipKind.TEXT, "abcdef")


@pytest.mark.parametrize(
    "over",
    [
        {"kind": "text/html"},
        {"kind": "image/png"},
        {"sha256": "f" * 64},
        {"total": 7},
        {"count": 4},
    ],
    ids=repr,
)
def test_row9_later_fragment_with_different_header_is_inconsistent(over):
    a = ClipAssembler()
    # "QUFB" là base64 hợp lệ, nên mảnh 1 dựng được cả với kind image/png.
    f = text_frags(X, ["ab", "QUFB", "cd"])
    bad = dataclasses.replace(f[1], **over)
    a.feed(D1, f[0], 0.0)
    assert a.feed(D1, bad, 0.0) == (drop(D1, X, "text", R.INCONSISTENT),)
    assert a.buffered_bytes(D1) == 0
    assert a.feed(D1, f[2], 0.0) == ()


def test_row10_skipped_index_is_out_of_order():
    a = ClipAssembler()
    f = text_frags(X, ["a", "b", "c"])
    a.feed(D1, f[0], 0.0)
    assert a.feed(D1, f[2], 0.0) == (drop(D1, X, "text", R.OUT_OF_ORDER),)
    assert a.buffered_bytes(D1) == 0
    assert a.feed(D1, f[1], 0.0) == ()


def test_row10_repeated_index_is_out_of_order():
    a = ClipAssembler()
    f = text_frags(X, ["a", "b", "c"])
    a.feed(D1, f[0], 0.0)
    a.feed(D1, f[1], 0.0)
    assert a.feed(D1, f[1], 0.0) == (drop(D1, X, "text", R.OUT_OF_ORDER),)
    assert a.buffered_bytes(D1) == 0


def test_row10_reversed_order():
    a = ClipAssembler()
    f = text_frags(X, ["a", "b", "c"])
    assert a.feed(D1, f[2], 0.0) == (drop(D1, X, "text", R.ORPHAN),)
    # Spec không chốt mảnh mồ côi có được nhớ như “clip vừa bỏ” (9.4) hay không: chấp nhận cả hai.
    assert a.feed(D1, f[1], 0.0) in ((drop(D1, X, "text", R.ORPHAN),), ())
    assert a.buffered_bytes(D1) == 0
    assert a.feed(D1, f[0], 0.0) == ()  # mảnh 0 tới sau cùng: mở clip mới, chờ mảnh 1 (không bao giờ tới)
    assert a.buffered_bytes(D1) == 1


def test_row11_first_fragment_larger_than_total():
    a = ClipAssembler()
    m = Clipboard(clip_id=X, kind="text", sha256="0" * 64, total=3, index=0, count=2, data="abcd")
    assert a.feed(D1, m, 0.0) == (drop(D1, X, "text", R.SIZE_MISMATCH),)
    assert a.buffered_bytes(D1) == 0


def test_row11_later_fragment_overflows_total():
    a = ClipAssembler()
    f = text_frags(X, ["ab", "cd"], total=3)
    a.feed(D1, f[0], 0.0)
    assert a.feed(D1, f[1], 0.0) == (drop(D1, X, "text", R.SIZE_MISMATCH),)
    assert a.buffered_bytes(D1) == 0


def test_row11_png_overflow_counts_decoded_bytes():
    a = ClipAssembler()
    content = PNG_SIGNATURE + b"1234"
    f = png_frags(X, [content[:6], content[6:]], total=len(content) - 1)
    a.feed(D1, f[0], 0.0)
    assert a.feed(D1, f[1], 0.0) == (drop(D1, X, "image/png", R.SIZE_MISMATCH),)
    assert a.buffered_bytes(D1) == 0


def test_row11_multibyte_text_counts_utf8_bytes():
    # "é" là 2 byte UTF-8: total đếm theo code point (2) thì mảnh 4 byte vượt.
    a = ClipAssembler()
    m = Clipboard(clip_id=X, kind="text", sha256=sha("éé".encode()), total=2, index=0, count=1, data="éé")
    assert a.feed(D1, m, 0.0) == (drop(D1, X, "text", R.SIZE_MISMATCH),)


def test_row12_last_fragment_short_of_total():
    a = ClipAssembler()
    f = text_frags(X, ["ab", "c"], total=5)
    a.feed(D1, f[0], 0.0)
    assert a.feed(D1, f[1], 0.0) == (drop(D1, X, "text", R.SIZE_MISMATCH),)
    assert a.buffered_bytes(D1) == 0


def test_row12_single_fragment_short_of_total():
    m = text_frags(X, ["abc"], total=4)[0]
    assert ClipAssembler().feed(D1, m, 0.0) == (drop(D1, X, "text", R.SIZE_MISMATCH),)


def test_row12_size_checked_before_hash():
    m = text_frags(X, ["abc"], total=4, sha256="0" * 64)[0]
    assert ClipAssembler().feed(D1, m, 0.0) == (drop(D1, X, "text", R.SIZE_MISMATCH),)


def test_row13_wrong_sha256_text():
    a = ClipAssembler()
    f = text_frags(X, ["ab", "cd"], sha256=sha(b"abce"))  # cùng độ dài, khác nội dung
    a.feed(D1, f[0], 0.0)
    assert a.feed(D1, f[1], 0.0) == (drop(D1, X, "text", R.HASH_MISMATCH),)
    assert a.buffered_bytes(D1) == 0


def test_row13_wrong_last_byte_png():
    a = ClipAssembler()
    content = PNG_SIGNATURE + b"pixels"
    f = png_frags(X, [content[:8], content[8:-1] + b"X"], sha256=sha(content))
    a.feed(D1, f[0], 0.0)
    assert a.feed(D1, f[1], 0.0) == (drop(D1, X, "image/png", R.HASH_MISMATCH),)
    assert a.buffered_bytes(D1) == 0


def test_row13_hash_checked_before_png_signature():
    content = b"NOTAPNG!" + b"data"
    m = png_frags(X, [content], sha256=sha(b"other!!!data"))[0]
    assert ClipAssembler().feed(D1, m, 0.0) == (drop(D1, X, "image/png", R.HASH_MISMATCH),)


@pytest.mark.parametrize(
    "content",
    [b"NOTAPNG!data", PNG_SIGNATURE[:-1] + b"\x0b" + b"data", b"\x89PNG", PNG_SIGNATURE[:7]],
    ids=repr,
)
def test_row14_png_without_signature_is_invalid_content(content):
    a = ClipAssembler()
    f = png_frags(X, [content])  # sha256 và total đúng: chỉ sai chữ ký
    assert a.feed(D1, f[0], 0.0) == (drop(D1, X, "image/png", R.INVALID_CONTENT),)
    assert a.buffered_bytes(D1) == 0


def test_text_does_not_need_png_signature():
    assert_ready(ClipAssembler().feed(D1, single(X, "NOTAPNG!"), 0.0), D1, X, ClipKind.TEXT, "NOTAPNG!")


def test_link_lost_drops_clip_in_progress():
    a = ClipAssembler()
    f = text_frags(X, ["ab", "cd"])
    a.feed(D1, f[0], 0.0)
    a.feed(D2, text_frags(Y, ["x", "y"])[0], 0.0)
    assert a.link_lost(D1) == (drop(D1, X, "text", R.LINK_LOST),)
    assert a.buffered_bytes(D1) == 0
    assert a.buffered_bytes(D2) == 1
    assert a.link_lost(D1) == ()
    assert a.link_lost(D3) == ()


def test_link_lost_keeps_rate_bucket():
    a = ClipAssembler()
    for i in range(4):
        a.feed(D1, single(cid(i)), 0.0)
    a.feed(D1, text_frags(X, ["a", "b"])[0], 0.0)  # token thứ 5
    assert a.link_lost(D1) == (drop(D1, X, "text", R.LINK_LOST),)
    assert a.feed(D1, single(Y), 0.0) == (drop(D1, Y, "text", R.RATE_LIMITED),)


def test_forget_device_clears_everything_silently():
    a = ClipAssembler()
    for i in range(4):
        a.feed(D1, single(cid(i)), 0.0)
    f = text_frags(X, ["a", "b"])
    a.feed(D1, f[0], 0.0)
    assert a.forget_device(D1) is None
    assert a.buffered_bytes(D1) == 0
    # Mảnh còn lại không còn được nhận ra (đã quên hết, kể cả clip vừa bỏ): mồ côi.
    assert a.feed(D1, f[1], 0.0) == (drop(D1, X, "text", R.ORPHAN),)
    # Bucket cũng bị xóa: clip mới cùng `now` qua.
    assert_ready(a.feed(D1, single(Y, "y"), 0.0), D1, Y, ClipKind.TEXT, "y")
    a.forget_device(D3)  # thiết bị chưa từng thấy: không lỗi


def test_forget_device_clears_recently_dropped_memory():
    a = ClipAssembler()
    f = text_frags(X, ["a", "b"])
    a.feed(D1, f[0], 0.0)
    a.feed(D1, f[0], 0.0)  # DUPLICATE → X là clip vừa bỏ
    a.forget_device(D1)
    assert a.feed(D1, f[1], 0.0) == (drop(D1, X, "text", R.ORPHAN),)


# --- 9.4: mảnh còn lại của clip vừa bỏ ----------------------------------------------------------


def _drop_at_index_zero(a: ClipAssembler, reason: str) -> list[Clipboard]:
    """Làm clip X bị bỏ ở mảnh 0 vì `reason`; trả các mảnh 0..2 của X."""
    if reason == "unsupported":
        f = [
            Clipboard(clip_id=X, kind="text/html", sha256="0" * 64, total=9, index=i, count=3, data="abc")
            for i in range(3)
        ]
        expected = drop(D1, X, "text/html", R.UNSUPPORTED_KIND)
    elif reason == "too_large":
        f = [
            Clipboard(
                clip_id=X, kind="text", sha256="0" * 64, total=MAX_TEXT_BYTES + 1, index=i, count=3, data="a"
            )
            for i in range(3)
        ]
        expected = drop(D1, X, "text", R.TOO_LARGE)
    elif reason == "count_gt_total":
        f = [
            Clipboard(clip_id=X, kind="text", sha256="0" * 64, total=2, index=i, count=3, data="a")
            for i in range(3)
        ]
        expected = drop(D1, X, "text", R.INCONSISTENT)
    else:  # size: mảnh 0 đã vượt total
        f = text_frags(X, ["abcd", "e", "f"], total=3)
        expected = drop(D1, X, "text", R.SIZE_MISMATCH)
    assert a.feed(D1, f[0], 0.0) == (expected,)
    return f


@pytest.mark.parametrize("reason", ["unsupported", "too_large", "count_gt_total", "size"])
def test_rest_of_clip_dropped_at_index_zero_is_silent(reason):
    a = ClipAssembler()
    f = _drop_at_index_zero(a, reason)
    assert a.feed(D1, f[1], 0.0) == ()
    assert a.feed(D1, f[2], 0.0) == ()
    assert a.buffered_bytes(D1) == 0


def test_rest_of_dropped_clip_is_silent_and_keeps_new_clip_in_progress():
    a = ClipAssembler()
    fx = text_frags(X, ["a", "b", "c"])
    fy = text_frags(Y, ["y1", "y2", "y3"])
    a.feed(D1, fx[0], 0.0)
    assert a.feed(D1, fx[2], 0.0) == (drop(D1, X, "text", R.OUT_OF_ORDER),)
    assert a.feed(D1, fy[0], 0.0) == ()
    assert a.feed(D1, fx[1], 0.0) == ()  # X vừa bỏ: im lặng, Y không đổi
    assert a.buffered_bytes(D1) == 2
    assert a.feed(D1, fy[1], 0.0) == ()
    assert_ready(a.feed(D1, fy[2], 0.0), D1, Y, ClipKind.TEXT, "y1y2y3")


def test_only_the_most_recently_dropped_clip_is_remembered():
    a = ClipAssembler()
    fx = text_frags(X, ["a", "b", "c"])
    fy = text_frags(Y, ["y1", "y2", "y3"])
    a.feed(D1, fx[0], 0.0)
    a.feed(D1, fx[2], 0.0)  # X bỏ (OUT_OF_ORDER)
    a.feed(D1, fy[0], 0.0)
    a.feed(D1, fy[2], 0.0)  # Y bỏ (OUT_OF_ORDER): giờ Y là clip vừa bỏ
    assert a.feed(D1, fy[1], 0.0) == ()
    assert a.feed(D1, fx[1], 0.0) == (drop(D1, X, "text", R.ORPHAN),)


def test_recently_dropped_memory_is_per_device():
    a = ClipAssembler()
    f = text_frags(X, ["a", "b", "c"])
    a.feed(D1, f[0], 0.0)
    a.feed(D1, f[2], 0.0)
    assert a.feed(D2, f[1], 0.0) == (drop(D2, X, "text", R.ORPHAN),)


def test_tick_prunes_idle_device_and_its_dropped_memory():
    a = ClipAssembler()
    f = text_frags(X, ["a", "b", "c"])
    a.feed(D1, f[0], 0.0)
    a.feed(D1, f[0], 0.0)  # DUPLICATE
    assert a.tick(0.0) == ()  # bucket chưa đầy lại: chưa dọn
    assert a.feed(D1, f[1], 0.0) == ()
    assert a.tick(100.0) == ()  # rảnh, bucket đầy: dọn cả trí nhớ clip vừa bỏ
    assert a.feed(D1, f[2], 100.0) == (drop(D1, X, "text", R.ORPHAN),)


def test_link_lost_clip_rest_is_silent():
    a = ClipAssembler()
    f = text_frags(X, ["a", "b"])
    a.feed(D1, f[0], 0.0)
    a.link_lost(D1)
    assert a.feed(D1, f[1], 0.0) == ()


# --- 5.5: giới hạn tốc độ ----------------------------------------------------------------------


def test_five_new_clips_pass_sixth_is_rate_limited():
    a = ClipAssembler()
    for i in range(5):
        assert_ready(a.feed(D1, single(cid(i), f"c{i}"), 0.0), D1, cid(i), ClipKind.TEXT, f"c{i}")
    assert a.feed(D1, single(cid(5)), 0.0) == (drop(D1, cid(5), "text", R.RATE_LIMITED),)
    assert a.buffered_bytes(D1) == 0


def test_bucket_refills_at_five_per_second():
    a = ClipAssembler()
    for i in range(5):
        a.feed(D1, single(cid(i)), 0.0)
    assert a.feed(D1, single(cid(10)), 0.125) == (drop(D1, cid(10), "text", R.RATE_LIMITED),)
    assert isinstance(a.feed(D1, single(cid(11)), 0.25)[0], ClipReady)
    assert a.feed(D1, single(cid(12)), 0.25) == (drop(D1, cid(12), "text", R.RATE_LIMITED),)
    # 0,25 token còn lại + 3,75 nạp trong 0,75 s = 4 token tại 1,0 s.
    for i in range(4):
        assert isinstance(a.feed(D1, single(cid(20 + i)), 1.0)[0], ClipReady)
    assert a.feed(D1, single(cid(30)), 1.0) == (drop(D1, cid(30), "text", R.RATE_LIMITED),)


def test_bucket_never_exceeds_burst():
    a = ClipAssembler()
    a.feed(D1, single(cid(0)), 0.0)
    for i in range(5):
        assert isinstance(a.feed(D1, single(cid(1 + i)), 100.0)[0], ClipReady)
    assert a.feed(D1, single(cid(9)), 100.0) == (drop(D1, cid(9), "text", R.RATE_LIMITED),)


def test_later_fragments_do_not_take_tokens():
    a = ClipAssembler()
    f = text_frags(X, ["1", "2", "3", "4", "5", "6", "7"])
    events = feed_all(a, D1, f)
    assert_ready(events, D1, X, ClipKind.TEXT, "1234567")
    for i in range(4):  # 1 token cho X + 4 = 5
        assert isinstance(a.feed(D1, single(cid(i)), 0.0)[0], ClipReady)
    assert a.feed(D1, single(cid(9)), 0.0) == (drop(D1, cid(9), "text", R.RATE_LIMITED),)


def test_rate_limited_clip_does_not_damage_clip_in_progress():
    a = ClipAssembler()
    for i in range(4):
        a.feed(D1, single(cid(i)), 0.0)
    fx = text_frags(X, ["ab", "cd"])
    assert a.feed(D1, fx[0], 0.0) == ()
    assert a.feed(D1, single(Y), 0.0) == (drop(D1, Y, "text", R.RATE_LIMITED),)
    assert a.buffered_bytes(D1) == 2
    assert_ready(a.feed(D1, fx[1], 0.0), D1, X, ClipKind.TEXT, "abcd")


def test_rest_of_rate_limited_clip_is_silent_and_harmless():
    a = ClipAssembler()
    for i in range(4):
        a.feed(D1, single(cid(i)), 0.0)
    fx = text_frags(X, ["ab", "cd"])
    fy = text_frags(Y, ["y", "z"])
    a.feed(D1, fx[0], 0.0)
    assert a.feed(D1, fy[0], 0.0) == (drop(D1, Y, "text", R.RATE_LIMITED),)
    assert a.feed(D1, fy[1], 0.0) == ()  # 9.4: Y là clip vừa bỏ
    assert_ready(a.feed(D1, fx[1], 0.0), D1, X, ClipKind.TEXT, "abcd")


def test_invalid_clips_also_take_tokens():
    a = ClipAssembler()
    for i in range(3):
        html = Clipboard(
            clip_id=cid(i), kind="text/html", sha256="0" * 64, total=1, index=0, count=1, data="x"
        )
        assert a.feed(D1, html, 0.0)[-1].reason == R.UNSUPPORTED_KIND
    big = Clipboard(
        clip_id=cid(7), kind="text", sha256="0" * 64, total=MAX_TEXT_BYTES + 1, index=0, count=1, data="x"
    )
    assert a.feed(D1, big, 0.0) == (drop(D1, cid(7), "text", R.TOO_LARGE),)
    bad = Clipboard(clip_id=cid(8), kind="text", sha256="0" * 64, total=1, index=0, count=2, data="x")
    assert a.feed(D1, bad, 0.0) == (drop(D1, cid(8), "text", R.INCONSISTENT),)
    assert a.feed(D1, single(Y), 0.0) == (drop(D1, Y, "text", R.RATE_LIMITED),)


def test_rate_limit_checked_before_kind_and_size():
    a = ClipAssembler()
    for i in range(5):
        a.feed(D1, single(cid(i)), 0.0)
    html = Clipboard(clip_id=X, kind="text/html", sha256="0" * 64, total=1, index=0, count=1, data="x")
    assert a.feed(D1, html, 0.0) == (drop(D1, X, "text/html", R.RATE_LIMITED),)


def test_duplicate_does_not_take_a_token():
    # Bảng 2.6 xét theo thứ tự: dòng 1 (trùng) đứng trước dòng 2 (lấy token).
    a = ClipAssembler()
    f = text_frags(X, ["a", "b"])
    a.feed(D1, f[0], 0.0)  # token 1
    assert a.feed(D1, f[0], 0.0) == (drop(D1, X, "text", R.DUPLICATE),)
    for i in range(4):  # token 2..5
        assert isinstance(a.feed(D1, single(cid(i)), 0.0)[0], ClipReady)
    assert a.feed(D1, single(Y), 0.0) == (drop(D1, Y, "text", R.RATE_LIMITED),)


def test_bucket_is_per_device():
    a = ClipAssembler()
    for i in range(5):
        a.feed(D1, single(cid(i)), 0.0)
    assert a.feed(D1, single(X), 0.0)[0].reason == R.RATE_LIMITED
    for i in range(5):
        assert isinstance(a.feed(D2, single(cid(i)), 0.0)[0], ClipReady)
    assert a.feed(D2, single(X), 0.0)[0].reason == R.RATE_LIMITED


def test_custom_rate_and_burst():
    a = ClipAssembler(rate=1.0, burst=2)
    assert isinstance(a.feed(D1, single(cid(1)), 0.0)[0], ClipReady)
    assert isinstance(a.feed(D1, single(cid(2)), 0.0)[0], ClipReady)
    assert a.feed(D1, single(cid(3)), 0.5)[0].reason == R.RATE_LIMITED
    assert isinstance(a.feed(D1, single(cid(4)), 1.0)[0], ClipReady)


def test_time_going_backwards_does_not_raise():
    a = ClipAssembler()
    f = text_frags(X, ["ab", "cd"])
    assert a.feed(D1, f[0], 10.0) == ()
    assert a.tick(5.0) == ()
    assert a.next_wakeup() is not None
    # `now` bị kẹp về 10,0: chưa quá hạn, X ráp xong.
    assert_ready(a.feed(D1, f[1], 3.0), D1, X, ClipKind.TEXT, "abcd")
    events = a.feed(D1, single(Y), 1.0)
    assert len(events) == 1
    a.link_lost(D1)
    a.tick(0.0)


def test_time_going_backwards_does_not_refill_bucket():
    a = ClipAssembler()
    for i in range(5):
        a.feed(D1, single(cid(i)), 10.0)
    assert a.feed(D1, single(X), 2.0)[0].reason == R.RATE_LIMITED


# --- 5.4: hypothesis, chuỗi Clipboard hợp lệ tùy ý từ ≤ 3 thiết bị --------------------------------

SMALL_LIMITS = {"text": 40, "image/png": 60}
POOL = [cid(i) for i in range(1, 5)]
# Không "\r" (để chuẩn hóa ở bên nhận là phép đồng nhất) và không surrogate (khai báo `exclude_characters`
# làm hypothesis bỏ mặc định loại Cs).
safe_text = st.text(
    alphabet=st.characters(exclude_characters="\r", exclude_categories=["Cs"]), min_size=1, max_size=24
)


@st.composite
def good_clip(draw) -> list[Clipboard]:
    """Mảnh nhất quán của một clip (có khi quá trần nhỏ, có khi PNG không chữ ký)."""
    clip_id = draw(st.sampled_from(POOL))
    if draw(st.booleans()):
        t = draw(safe_text)
        cuts = sorted(draw(st.sets(st.integers(1, max(1, len(t) - 1)), max_size=3))) if len(t) > 1 else []
        bounds = [0, *cuts, len(t)]
        pieces = [t[i:j] for i, j in itertools.pairwise(bounds) if t[i:j]]
        return text_frags(clip_id, pieces)
    head = PNG_SIGNATURE if draw(st.integers(0, 4)) else b"bad-sig!"
    body = head + draw(st.binary(max_size=56))
    cuts = sorted(draw(st.sets(st.integers(1, len(body) - 1), max_size=3)))
    bounds = [0, *cuts, len(body)]
    return png_frags(clip_id, [body[i:j] for i, j in itertools.pairwise(bounds)])


@st.composite
def random_fragment(draw) -> Clipboard:
    kind = draw(st.sampled_from(["text", "image/png", "text/html"]))
    count = draw(st.integers(1, 4))
    if kind == "image/png":
        data = base64.b64encode(draw(st.binary(min_size=1, max_size=30))).decode()
    else:
        data = draw(safe_text)
    return Clipboard(
        clip_id=draw(st.sampled_from(POOL)),
        kind=kind,
        sha256=draw(st.sampled_from(["0" * 64, sha(data.encode())])),
        total=draw(st.integers(1, 80)),
        index=draw(st.integers(0, count - 1)),
        count=count,
        data=data,
    )


DT = st.sampled_from([0.0, 0.0, 0.0, 0.125, 0.25, 1.0, 7.5, 15.0])
op = st.one_of(
    st.tuples(st.just("clip"), st.integers(0, 2), good_clip(), DT),
    st.tuples(st.just("frag"), st.integers(0, 2), random_fragment(), DT),
    st.tuples(st.just("tick"), st.just(0), st.none(), DT),
    st.tuples(st.just("lost"), st.integers(0, 2), st.none(), DT),
    st.tuples(st.just("forget"), st.integers(0, 2), st.none(), DT),
)


def _check_events(events, device: str | None) -> None:
    assert isinstance(events, tuple)
    for ev in events:
        assert isinstance(ev, ClipReady | ClipDropped), ev
        if device is not None:
            assert ev.device_id == device
        if isinstance(ev, ClipReady):
            raw = ev.content.encode("utf-8") if ev.kind == ClipKind.TEXT else ev.content
            assert sha(raw) == ev.sha256, "ClipReady phải khớp sha256"
            if ev.kind == ClipKind.PNG:
                assert ev.content.startswith(PNG_SIGNATURE)
                assert len(raw) <= SMALL_LIMITS["image/png"]
            else:
                assert len(raw) <= SMALL_LIMITS["text"]
        else:
            assert isinstance(ev.reason, DropReason)
            assert isinstance(ev.kind, str)


@pytest.mark.slow
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops=st.lists(op, max_size=25), data=st.data())
def test_property_arbitrary_sequences_never_raise_and_stay_bounded(ops, data):
    devices = [D1, D2, D3]
    a = ClipAssembler(limits=SMALL_LIMITS)
    now = 0.0
    pending: list[tuple[str, list[Clipboard]]] = []
    for kind, dev_idx, payload, dt in ops:
        now += dt
        device = devices[dev_idx]
        if kind == "clip":
            pending.append((device, list(payload)))
        elif kind == "frag":
            pending.append((device, [payload]))
        elif kind == "tick":
            _check_events(a.tick(now), None)
        elif kind == "lost":
            _check_events(a.link_lost(device), device)
            assert a.buffered_bytes(device) == 0
        else:
            a.forget_device(device)
            assert a.buffered_bytes(device) == 0
        # Xen kẽ: đưa vào một số mảnh của các clip đang chờ, thứ tự giữa các clip do hypothesis chọn.
        steps = data.draw(st.integers(0, 4))
        for _ in range(steps):
            live = [p for p in pending if p[1]]
            if not live:
                break
            dev, queue = data.draw(st.sampled_from(live))
            _check_events(a.feed(dev, queue.pop(0), now), dev)
        for d in devices:
            assert 0 <= a.buffered_bytes(d) <= max(SMALL_LIMITS.values())
        assert a.buffered_bytes() == sum(a.buffered_bytes(d) for d in devices)
        wake = a.next_wakeup()
        assert wake is None or isinstance(wake, float | int)
    for dev, queue in pending:
        for m in queue:
            _check_events(a.feed(dev, m, now), dev)
    _check_events(a.tick(now + 100.0), None)
    assert a.buffered_bytes() == 0


@pytest.mark.slow
@settings(max_examples=200, deadline=None)
@given(clips=st.lists(st.tuples(st.integers(0, 2), good_clip()), min_size=1, max_size=6), data=st.data())
def test_property_interleaved_devices_each_clip_ready_or_dropped(clips, data):
    """Mỗi thiết bị gửi tuần tự, liền mạch các clip của mình; các thiết bị xen nhau tùy ý. Mỗi clip có
    đúng một kết cục: clip trong trần (và PNG có chữ ký) → ClipReady đúng nội dung; còn lại → đúng một
    ClipDropped (TOO_LARGE ở mảnh 0 hoặc INVALID_CONTENT ở mảnh cuối)."""
    devices = [D1, D2, D3]
    a = ClipAssembler(limits=SMALL_LIMITS, rate=1000.0, burst=1000)
    queues: dict[str, list[Clipboard]] = {d: [] for d in devices}
    expected: dict[str, list[tuple]] = {d: [] for d in devices}
    for dev_idx, msgs in clips:
        d = devices[dev_idx]
        queues[d].extend(msgs)
        first = msgs[0]
        if first.kind == "text":
            content: str | bytes = "".join(m.data for m in msgs)
        else:
            content = b"".join(base64.b64decode(m.data) for m in msgs)
        if first.total > SMALL_LIMITS[first.kind]:
            expected[d].append(("drop", first.clip_id, R.TOO_LARGE))
        elif first.kind == "image/png" and not content.startswith(PNG_SIGNATURE):
            expected[d].append(("drop", first.clip_id, R.INVALID_CONTENT))
        else:
            expected[d].append(("ready", first.clip_id, content))
    got: dict[str, list[tuple]] = {d: [] for d in devices}
    now = 0.0
    while any(queues.values()):
        dev = data.draw(st.sampled_from([d for d in devices if queues[d]]))
        events = a.feed(dev, queues[dev].pop(0), now)
        _check_events(events, dev)
        for ev in events:
            if isinstance(ev, ClipReady):
                got[dev].append(("ready", ev.clip_id, ev.content))
            else:
                got[dev].append(("drop", ev.clip_id, ev.reason))
        now += 0.125
    assert got == expected
    assert a.tick(now + 100.0) == ()
    assert a.buffered_bytes() == 0


# ---- Bảng cỡ mã hóa JSON (spec 9.2): đối chiếu với json.dumps thật ----------------------


def _real_json_len(s: str) -> int:
    return len(json.dumps(s, ensure_ascii=False).encode("utf-8")) - 2


def test_json_cost_table_matches_json_dumps_for_every_code_point():
    # Quét trọn dải code point (trừ surrogate): mỗi ký tự tính thiếu là một khung có thể vượt trần.
    chars = "".join(chr(cp) for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF)
    for i in range(0, len(chars), 4096):
        block = chars[i : i + 4096]
        if clip_mod._json_text_len(block) != _real_json_len(block):
            bad = [c for c in block if clip_mod._json_text_len(c) != _real_json_len(c)]
            pytest.fail(f"lệch cỡ ở {[hex(ord(c)) for c in bad[:10]]}")


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.characters(exclude_categories=["Cs"])))
def test_property_json_cost_matches_json_dumps(s):
    assert clip_mod._json_text_len(s) == _real_json_len(s)
