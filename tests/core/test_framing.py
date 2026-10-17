import pytest

from teleport.core.framing import (
    HEADER,
    MAX_MESSAGE_SIZE,
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


def test_oversized_message_cannot_be_encoded():
    with pytest.raises(FramingError, match="quá lớn"):
        encode_message({"data": "x" * MAX_MESSAGE_SIZE})


@pytest.mark.parametrize("body", [b"not json", b"[1, 2, 3]", b'"chuoi"', b"\xff\xfe"])
def test_invalid_body_raises(body):
    with pytest.raises(FramingError):
        decode_body(body)
