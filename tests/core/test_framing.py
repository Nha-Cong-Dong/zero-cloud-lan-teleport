import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from teleport.core.framing import (
    HEADER,
    MAX_JSON_DEPTH,
    MAX_MESSAGE_SIZE,
    MAX_PAIRING_MESSAGE_SIZE,
    FramingError,
    decode_body,
    decode_header,
    encode_message,
)


def split(frame: bytes) -> tuple[int, bytes]:
    return decode_header(frame[: HEADER.size]), frame[HEADER.size :]


def test_roundtrip_keeps_unicode():
    msg = {"type": "msg", "text": "Xin chào, Việt Nam 🚀"}
    length, body = split(encode_message(msg))
    assert length == len(body)
    assert decode_body(body) == msg


def test_encoding_is_compact_utf8():
    _, body = split(encode_message({"a": "đ"}))
    assert body == '{"a":"đ"}'.encode()


def test_header_at_limit_is_accepted():
    assert decode_header(HEADER.pack(MAX_MESSAGE_SIZE)) == MAX_MESSAGE_SIZE


def test_oversized_header_is_rejected():
    with pytest.raises(FramingError, match="quá lớn"):
        decode_header(HEADER.pack(MAX_MESSAGE_SIZE + 1))


def test_custom_limit_for_pairing_connections():
    assert decode_header(HEADER.pack(MAX_PAIRING_MESSAGE_SIZE), MAX_PAIRING_MESSAGE_SIZE) == 16 * 1024
    with pytest.raises(FramingError):
        decode_header(HEADER.pack(MAX_PAIRING_MESSAGE_SIZE + 1), MAX_PAIRING_MESSAGE_SIZE)


def test_oversized_message_cannot_be_encoded():
    with pytest.raises(FramingError, match="quá lớn"):
        encode_message({"data": "x" * MAX_MESSAGE_SIZE})


@pytest.mark.parametrize("body", [b"not json", b"[1, 2, 3]", b'"chuoi"', b"\xff\xfe"])
def test_invalid_body_raises(body):
    with pytest.raises(FramingError):
        decode_body(body)


# --- Phụ lục điểm 15: số nguyên quá dài, JSON lồng quá sâu → FramingError --------------------


@pytest.mark.parametrize(
    "body",
    [
        b'{"a":' + b"1" * 5000 + b"}",
        b'{"a":-' + b"9" * 4301 + b"}",
        b'{"a":[' + b"7" * 100_000 + b"]}",
    ],
    ids=["5000-chu-so", "4301-chu-so-am", "100k-chu-so-trong-list"],
)
def test_huge_integer_raises_framing_error(body):
    with pytest.raises(FramingError):
        decode_body(body)


@pytest.mark.parametrize(
    "body",
    [
        b"[" * 100_000,
        b"[" * 100_000 + b"]" * 100_000,
        b'{"a":' * 100_000 + b"1" + b"}" * 100_000,
        b'{"a":' + b"[" * 50_000 + b"]" * 50_000 + b"}",
    ],
    ids=["mo-ngoac-100k", "list-long-100k", "object-long-100k", "trong-object"],
)
def test_deeply_nested_json_raises_framing_error(body):
    with pytest.raises(FramingError):
        decode_body(body)


def test_moderately_nested_json_still_decodes():
    body = b'{"a":' + b"[" * 50 + b"]" * 50 + b"}"
    value = decode_body(body)["a"]
    depth = 0
    while value != []:
        assert isinstance(value, list) and len(value) == 1
        value = value[0]
        depth += 1
    assert depth == 49


def nested(depth: int) -> bytes:
    """Object ngoài cùng (cấp 1) chứa list lồng nhau, tổng cộng đúng `depth` cấp."""
    inner = depth - 1
    return b'{"a":' + b"[" * inner + b"]" * inner + b"}"


def test_depth_exactly_at_limit_is_accepted():
    assert isinstance(decode_body(nested(MAX_JSON_DEPTH)), dict)


@pytest.mark.parametrize("depth", [MAX_JSON_DEPTH + 1, 1_000])
def test_depth_over_limit_is_rejected_on_every_platform(depth):
    with pytest.raises(FramingError, match="lồng"):
        decode_body(nested(depth))


def test_depth_limit_counts_objects_too():
    def objects(depth: int) -> bytes:
        return b'{"a":' * depth + b"1" + b"}" * depth

    assert isinstance(decode_body(objects(MAX_JSON_DEPTH)), dict)
    with pytest.raises(FramingError):
        decode_body(objects(MAX_JSON_DEPTH + 1))


def test_brackets_inside_strings_do_not_count_as_nesting():
    body = json.dumps({"text": "[{" * 500, "list": [1, 2, 3]}).encode()
    assert decode_body(body)["text"] == "[{" * 500


def test_wide_but_shallow_json_is_accepted():
    body = json.dumps({"hashes": [[i] for i in range(10_000)]}).encode()
    assert len(decode_body(body)["hashes"]) == 10_000


# --- Fuzz decode_body (phụ lục điểm 18) --------------------------------------------------------

json_like = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=20),
    lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(max_size=8), c, max_size=4),
    max_leaves=20,
)


@settings(max_examples=500, deadline=None)
@given(
    st.binary(max_size=400)
    | json_like.map(lambda v: json.dumps(v).encode())
    | st.text(alphabet='{}[]":,0123456789-+.eE nultrfasNI\\u', max_size=200).map(str.encode)
)
def test_fuzz_decode_body_returns_dict_or_framing_error(body):
    try:
        msg = decode_body(body)
    except FramingError:
        return
    assert isinstance(msg, dict)


@settings(max_examples=200, deadline=None)
@given(st.dictionaries(st.text(max_size=10), json_like, max_size=5))
def test_fuzz_encode_decode_roundtrip_of_objects(obj):
    frame = encode_message(obj)
    length, body = split(frame)
    assert length == len(body)
    # NaN != NaN nên so sánh qua dạng JSON chuẩn.
    assert json.dumps(decode_body(body), sort_keys=True) == json.dumps(obj, sort_keys=True)
