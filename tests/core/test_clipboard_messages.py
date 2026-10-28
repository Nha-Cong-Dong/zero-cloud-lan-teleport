"""Test message `clipboard` (`teleport.core.messages.Clipboard`) theo spec P5-0 mục 2.2 và 9.6, 9.8.

Viết từ đặc tả, không từ code: luật từng trường (mỗi ví dụ sai đúng một trường, các trường khác
hợp lệ), kind lạ đúng dạng vẫn hợp lệ, fuzz dict tùy ý `type: clipboard` chỉ ra `Clipboard` hoặc
`MessageError`, `repr` và thông điệp lỗi không lộ `data` (mvp N9), bảng loại kết nối.
"""

import base64
import dataclasses
import json
import re
import typing
from enum import StrEnum

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from teleport.core.framing import decode_body, encode_message
from teleport.core.messages import (
    MAX_CLIP_KIND_LEN,
    MAX_SAFE_INTEGER,
    Clipboard,
    ClipKind,
    ConnectionKind,
    Message,
    MessageError,
    UnknownMessageType,
    WrongConnectionKind,
    allowed_types,
    check_message_kind,
    opening_kind,
    parse_message,
    to_dict,
)

CLIP_ID = "0123456789abcdef" * 2
SHA = "0123456789abcdef" * 4
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"  # chép từ spec mục 2.4, không lấy từ code
PNG_B64 = base64.b64encode(PNG_SIGNATURE + b"\x00" * 5).decode("ascii")
FIELDS = ("clip_id", "kind", "sha256", "total", "index", "count", "data")
# Regex của spec mục 2.2, viết lại độc lập để làm mẫu cho hypothesis.
KIND_RE = r"[a-z0-9][a-z0-9.+-]*(?:/[a-z0-9][a-z0-9.+-]*)?"
SECRET = "mat-khau-BI-MAT-123456"


def wire(**overrides) -> dict:
    """Dict `clipboard` hợp lệ (text, một mảnh) với các trường đè."""
    d: dict = {
        "type": "clipboard",
        "clip_id": CLIP_ID,
        "kind": "text",
        "sha256": SHA,
        "total": 5,
        "index": 0,
        "count": 1,
        "data": "hello",
    }
    d.update(overrides)
    return d


def make(**overrides) -> Clipboard:
    kw = wire(**overrides)
    del kw["type"]
    return Clipboard(**kw)


def through_frame(msg: Clipboard) -> object:
    return parse_message(decode_body(encode_message(to_dict(msg))[4:]))


# --- Hằng, kiểu, thứ tự trường -----------------------------------------------------------------


def test_constants_and_clip_kind():
    assert MAX_CLIP_KIND_LEN == 64
    assert issubclass(ClipKind, StrEnum)
    assert {k.value for k in ClipKind} == {"text", "image/png"}
    assert ClipKind.TEXT == "text"
    assert ClipKind.PNG == "image/png"


def test_type_and_field_order():
    assert Clipboard.TYPE == "clipboard"
    assert [f.name for f in dataclasses.fields(Clipboard)] == list(FIELDS)


def test_clipboard_is_frozen():
    m = make()
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.data = "khác"  # type: ignore[misc]


def test_clipboard_is_member_of_message_union():
    assert Clipboard in typing.get_args(Message)


# --- Hợp lệ và round-trip ----------------------------------------------------------------------

VALID_CASES = [
    pytest.param(wire(), id="text"),
    pytest.param(wire(data="a\x00b\x01\r\n\x7f\ufeff\ufffd😀"), id="text-control-chars-kept"),
    pytest.param(wire(kind="image/png", data=PNG_B64, total=13), id="png"),
    # Ở tầng message không kiểm chữ ký PNG (đó là việc của bên ráp, bảng §2.6 dòng 14).
    pytest.param(
        wire(kind="image/png", data=base64.b64encode(b"x").decode()), id="png-one-byte-no-signature"
    ),
    pytest.param(wire(kind="text/html", data="<b>đậm</b>"), id="unknown-kind-text-html"),
    pytest.param(wire(kind="image/svg+xml", data="!!! không phải base64"), id="unknown-kind-svg"),
    pytest.param(wire(kind="image/jpeg", data="@@@"), id="unknown-image-kind-any-str"),
    pytest.param(wire(kind="a" * 64), id="kind-64-chars"),
    pytest.param(wire(kind="a" * 31 + "/" + "b" * 32), id="kind-64-chars-with-slash"),
    pytest.param(wire(kind="0"), id="kind-one-digit"),
    pytest.param(wire(kind="x.y+z-1/0.a+b-"), id="kind-all-allowed-punctuation"),
    pytest.param(wire(clip_id="0" * 32), id="clip-id-zero-uuid-bits-not-checked"),
    pytest.param(wire(total=1, data="a"), id="total-1"),
    pytest.param(wire(total=MAX_SAFE_INTEGER), id="total-max"),
    pytest.param(wire(index=MAX_SAFE_INTEGER - 1, count=MAX_SAFE_INTEGER), id="index-max"),
    pytest.param(wire(index=4, count=5), id="last-index"),
    # count > total, cỡ mảnh và trần theo kind không kiểm ở tầng message (spec mục 2.2).
    pytest.param(wire(total=1, count=9, index=8), id="count-greater-than-total"),
    pytest.param(wire(total=MAX_SAFE_INTEGER, kind="image/png", data=PNG_B64), id="png-total-over-cap"),
    pytest.param(wire(future="trường lạ"), id="unknown-field-ignored"),
]


@pytest.mark.parametrize("d", VALID_CASES)
def test_valid_clipboard_parses_and_round_trips(d):
    m = parse_message(d)
    assert type(m) is Clipboard
    for name in FIELDS:
        assert getattr(m, name) == d[name]
    assert parse_message(to_dict(m)) == m
    assert through_frame(m) == m
    out = to_dict(m)
    assert out["type"] == "clipboard"
    assert set(out) == {"type", *FIELDS}


def test_kind_enum_member_becomes_plain_str():
    m = make(kind=ClipKind.PNG, data=PNG_B64)
    assert type(m.kind) is str
    assert m.kind == "image/png"
    assert type(to_dict(m)["kind"]) is str
    assert parse_message(to_dict(m)) == m
    t = make(kind=ClipKind.TEXT)
    assert type(t.kind) is str and t.kind == "text"


# --- Luật từng trường: mỗi ca sai đúng một trường ----------------------------------------------

BAD_FIELD_VALUES = {
    "clip_id": [
        "0123456789ABCDEF" * 2,
        "0" * 31,
        "0" * 33,
        "g" * 32,
        "0" * 31 + "\n",
        " " + "0" * 31,
        "",
        0,
        None,
        b"0" * 32,
        ["0" * 32],
    ],
    "kind": [
        "",
        "Text",
        "TEXT",
        "image/",
        "/png",
        "a/b/c",
        "text\n",
        " text",
        "text ",
        "ảnh",
        "ｔext",
        "-text",
        ".text",
        "+a",
        "a//b",
        "a/-b",
        "a/.b",
        "image/PNG",
        "text\x00",
        "a_b",
        "a b",
        "a" * 65,
        "a" * 32 + "/" + "b" * 32,
        1,
        None,
        b"text",
        ["text"],
        True,
    ],
    "sha256": ["0" * 63, "0" * 65, "A" * 64, "g" * 64, "", 0, None, b"0" * 64, "0" * 63 + "\n"],
    "total": [0, -1, MAX_SAFE_INTEGER + 1, True, False, 1.0, 5.0, "5", None, [5]],
    "index": [-1, MAX_SAFE_INTEGER + 1, True, False, 0.0, "0", None],
    "count": [0, -1, MAX_SAFE_INTEGER + 1, True, 1.0, "1", None],
    "data": ["", None, 0, b"hello", ["hello"], {"a": 1}],
}

BAD_CASES = [
    pytest.param(field, value, id=f"{field}={value!r:.40}")
    for field, values in BAD_FIELD_VALUES.items()
    for value in values
]


@pytest.mark.parametrize(("field", "value"), BAD_CASES)
def test_rejects_bad_field(field, value):
    with pytest.raises(MessageError):
        parse_message(wire(**{field: value}))
    kw = wire(**{field: value})
    del kw["type"]
    with pytest.raises(MessageError):
        Clipboard(**kw)


@pytest.mark.parametrize(("index", "count"), [(1, 1), (5, 5), (6, 5), (MAX_SAFE_INTEGER, MAX_SAFE_INTEGER)])
def test_rejects_index_not_below_count(index, count):
    with pytest.raises(MessageError):
        parse_message(wire(index=index, count=count))


@pytest.mark.parametrize("field", FIELDS)
def test_rejects_missing_field(field):
    d = wire()
    del d[field]
    with pytest.raises(MessageError):
        parse_message(d)


@pytest.mark.parametrize("field", FIELDS)
def test_rejects_null_field(field):
    with pytest.raises(MessageError):
        parse_message(wire(**{field: None}))


# `kind == "text"`: không chứa surrogate lẻ.
@pytest.mark.parametrize(
    "data",
    # Cặp cao + thấp dựng bằng chr(): hai escape surrogate liền nhau trong literal được Python ghép sẵn.
    ["\ud800", "\udfff", "a\udc00b", chr(0xD83D) + chr(0xDE00), "abc\ud83d", "\udc00\ud800"],
    ids=repr,
)
def test_text_rejects_surrogate(data):
    with pytest.raises(MessageError):
        make(data=data)


@pytest.mark.parametrize("kind", ["text/html", "image/svg+xml", "x"])
def test_unknown_kind_rejects_surrogate(kind):
    # Mọi `data` trên dây phải mã hóa lại được UTF-8, kể cả kind lạ.
    with pytest.raises(MessageError):
        make(kind=kind, data="a\ud800b")


def test_text_rejects_lone_surrogate_from_json_escape():
    # `json.loads` tạo được surrogate lẻ từ "\ud800" (spec mục 2.2): đúng đường của dữ liệu thật.
    body = json.dumps(wire(data="x")).replace('"x"', '"a\\ud800b"')
    obj = json.loads(body)
    assert obj["data"] == "a\ud800b"
    with pytest.raises(MessageError):
        parse_message(obj)


def test_text_accepts_escaped_surrogate_pair_from_json():
    # Cặp hợp lệ trong JSON là một code point astral, không phải surrogate.
    obj = json.loads(json.dumps(wire(data="x")).replace('"x"', '"\\ud83d\\ude00"'))
    assert parse_message(obj).data == "😀"


# `kind == "image/png"`: base64 chuẩn chặt, giải ra ≥ 1 byte; lỗi gói thành MessageError (9.8).
@pytest.mark.parametrize(
    "data",
    [
        "!!!!",
        "AA",  # thiếu đệm
        "AA=",
        "AB==",  # bit đệm khác 0: không phải dạng chuẩn
        "AAAA\n",
        " AAAA",
        "AA AA",
        "AAAA====",
        "QUFB" + "é",
        "-_-_",  # bảng chữ base64url
        "=",
        "====",
        "\ud800",
    ],
    ids=repr,
)
def test_png_rejects_non_canonical_base64(data):
    with pytest.raises(MessageError):
        parse_message(wire(kind="image/png", data=data))


def test_png_base64_error_is_message_error_not_bare_value_error():
    with pytest.raises(MessageError) as info:
        make(kind="image/png", data="AB==")
    assert isinstance(info.value, MessageError)


@pytest.mark.parametrize("kind", [1, None, b"text", ["text"], 1.5, True], ids=repr)
def test_non_str_kind_is_message_error_not_type_error(kind):
    # 9.8: `isinstance` trước regex; bytes vào `re.fullmatch` của mẫu str sẽ ném TypeError.
    with pytest.raises(MessageError):
        make(kind=kind)


def test_non_str_data_with_png_kind_is_message_error():
    with pytest.raises(MessageError):
        make(kind="image/png", data=b"AAAA")


def test_unknown_message_type_still_distinct():
    with pytest.raises(UnknownMessageType):
        parse_message(wire(type="clipboard2"))


# --- N9: repr và thông điệp lỗi không lộ nội dung ----------------------------------------------


def test_repr_does_not_contain_data():
    m = make(data=SECRET)
    assert SECRET not in repr(m)
    assert SECRET not in str(m)
    assert CLIP_ID in repr(m)  # các trường khác vẫn hiện để chẩn đoán


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"kind": "image/png", "data": SECRET + "!"}, id="png-bad-base64"),
        pytest.param({"kind": "image/png", "data": SECRET + "=="}, id="png-bad-padding"),
        pytest.param({"data": SECRET + "\ud800"}, id="text-surrogate"),
        pytest.param({"clip_id": "X" * 32, "data": SECRET}, id="other-field-bad"),
        pytest.param({"index": 3, "count": 1, "data": SECRET}, id="index-not-below-count"),
        pytest.param({"kind": "Bad Kind", "data": SECRET}, id="kind-bad"),
    ],
)
def test_error_message_does_not_contain_data(overrides):
    with pytest.raises(MessageError) as info:
        parse_message(wire(**overrides))
    text = str(info.value) + repr(info.value) + "".join(map(str, info.value.args))
    assert "BI-MAT" not in text


def test_error_message_truncates_long_kind():
    with pytest.raises(MessageError) as info:
        parse_message(wire(kind="Q" * 10_000))
    text = str(info.value)
    assert "Q" * 65 not in text
    assert len(text) < 400


# --- Fuzz: dict tùy ý `type: clipboard` chỉ ra Clipboard hoặc MessageError ---------------------

field_values = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-3, max_value=10),
    st.integers(),
    st.sampled_from([MAX_SAFE_INTEGER, MAX_SAFE_INTEGER + 1, 2**64]),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
    st.from_regex(KIND_RE, fullmatch=True),
    st.sampled_from(
        [
            CLIP_ID,
            CLIP_ID.upper(),
            SHA,
            "text",
            "image/png",
            "text/html",
            PNG_B64,
            "AB==",
            "AAAA",
            "\ud800",
            "a\udc00",
            "Q" * 200,
            SECRET,
            SECRET + "\ud800",
            "!" + SECRET,
        ]
    ),
    st.binary(max_size=8).map(lambda b: base64.b64encode(b).decode()),
    st.lists(st.integers(), max_size=2),
    st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
)
fuzz_dicts = st.builds(
    lambda base, extra, drop: {k: v for k, v in {**base, **extra}.items() if k not in drop},
    st.just(wire()),
    st.dictionaries(st.sampled_from([*FIELDS, "x"]), field_values, max_size=7),
    st.sets(st.sampled_from(FIELDS), max_size=2),
)


@settings(max_examples=500, deadline=None)
@given(d=fuzz_dicts)
def test_property_any_clipboard_dict_parses_or_raises_message_error(d):
    try:
        m = parse_message(d)
    except MessageError as e:
        assert not isinstance(e, UnknownMessageType)
        # N9: dấu "BI-MAT" chỉ được lộ nếu nó nằm ở trường khác (vd `kind` được phép hiện ≤ 64 ký tự).
        others = [repr(v) for k, v in d.items() if k != "data"]
        if (
            isinstance(d.get("data"), str)
            and "BI-MAT" in d["data"]
            and not any("BI-MAT" in o for o in others)
        ):
            assert "BI-MAT" not in str(e)
        return
    assert type(m) is Clipboard
    assert type(m.kind) is str
    assert parse_message(to_dict(m)) == m


def _valid_clipboards(draw_kind_data):
    return st.builds(
        lambda clip_id, sha, total, kd, idx_count: Clipboard(
            clip_id=clip_id,
            kind=kd[0],
            sha256=sha,
            total=total,
            index=idx_count[0],
            count=idx_count[1],
            data=kd[1],
        ),
        st.from_regex(r"[0-9a-f]{32}", fullmatch=True),
        st.from_regex(r"[0-9a-f]{64}", fullmatch=True),
        st.integers(1, MAX_SAFE_INTEGER),
        draw_kind_data,
        st.integers(1, MAX_SAFE_INTEGER).flatmap(lambda c: st.tuples(st.integers(0, c - 1), st.just(c))),
    )


text_data = st.text(min_size=1)  # mặc định hypothesis không sinh surrogate
png_data = st.binary(min_size=1, max_size=64).map(lambda b: base64.b64encode(b).decode("ascii"))
other_kinds = st.from_regex(KIND_RE, fullmatch=True).filter(
    lambda k: len(k) <= MAX_CLIP_KIND_LEN and k not in ("text", "image/png")
)
kind_data = st.one_of(
    st.tuples(st.just("text"), text_data),
    st.tuples(st.just("image/png"), png_data),
    st.tuples(other_kinds, st.text(min_size=1)),
)


@settings(max_examples=300, deadline=None)
@given(m=_valid_clipboards(kind_data))
def test_property_valid_clipboard_round_trips_through_frame(m):
    assert parse_message(to_dict(m)) == m
    assert through_frame(m) == m


@settings(max_examples=300, deadline=None)
@given(kind=st.text(max_size=70))
def test_property_kind_rule_matches_spec_regex(kind):
    expected_ok = (
        1 <= len(kind) <= MAX_CLIP_KIND_LEN and re.fullmatch(KIND_RE, kind, flags=re.ASCII) is not None
    )
    data = PNG_B64 if kind == "image/png" else "x"
    try:
        m = make(kind=kind, data=data)
    except MessageError:
        assert not expected_ok, f"kind hợp lệ bị từ chối: {kind!r}"
    else:
        assert expected_ok, f"kind sai được nhận: {kind!r}"
        assert m.kind == kind


# --- Bảng loại kết nối -------------------------------------------------------------------------


def test_clipboard_allowed_on_control_only():
    assert "clipboard" in allowed_types(ConnectionKind.CONTROL)
    assert "clipboard" not in allowed_types(ConnectionKind.PAIRING)
    assert "clipboard" not in allowed_types(ConnectionKind.DATA)


@pytest.mark.parametrize("d", VALID_CASES[:4])
def test_check_message_kind_for_clipboard(d):
    m = parse_message(d)
    assert check_message_kind(ConnectionKind.CONTROL, m) is None
    assert check_message_kind("control", m) is None
    for kind in (ConnectionKind.PAIRING, ConnectionKind.DATA, "pairing", "data"):
        with pytest.raises(WrongConnectionKind):
            check_message_kind(kind, m)


@pytest.mark.parametrize("has_cert", [False, True])
def test_clipboard_cannot_open_a_connection(has_cert):
    with pytest.raises(WrongConnectionKind):
        opening_kind(make(), has_client_cert=has_cert)
