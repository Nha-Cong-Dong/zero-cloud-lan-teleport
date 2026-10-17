import asyncio

import pytest

from teleport.core.framing import (
    HEADER,
    MAX_MESSAGE_SIZE,
    FramingError,
    encode_message,
    read_message,
)


def reader_with(data: bytes, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


async def test_roundtrip_keeps_unicode():
    msg = {"type": "msg", "text": "Xin chào, Việt Nam 🚀"}
    assert await read_message(reader_with(encode_message(msg))) == msg


async def test_back_to_back_messages_are_split_correctly():
    msgs = [{"n": i} for i in range(5)]
    reader = reader_with(b"".join(encode_message(m) for m in msgs))
    assert [await read_message(reader) for _ in msgs] == msgs
    assert await read_message(reader) is None


async def test_message_arriving_in_pieces():
    data = encode_message({"type": "msg", "text": "ghép từ nhiều mảnh"})
    reader = asyncio.StreamReader()
    task = asyncio.create_task(read_message(reader))
    for i in range(len(data)):  # mỗi lần chỉ 1 byte, như TCP có thể làm
        reader.feed_data(data[i : i + 1])
        await asyncio.sleep(0)
    assert (await task)["text"] == "ghép từ nhiều mảnh"


async def test_clean_eof_returns_none():
    assert await read_message(reader_with(b"")) is None


async def test_eof_inside_header_raises():
    with pytest.raises(FramingError):
        await read_message(reader_with(b"\x00\x00"))


async def test_eof_inside_body_raises():
    data = encode_message({"text": "bị cắt"})
    with pytest.raises(FramingError):
        await read_message(reader_with(data[:-3]))


async def test_oversized_header_is_rejected_before_reading_body():
    reader = reader_with(HEADER.pack(MAX_MESSAGE_SIZE + 1), eof=False)
    with pytest.raises(FramingError, match="quá lớn"):
        await read_message(reader)


@pytest.mark.parametrize("body", [b"not json", b"[1, 2, 3]", b"\xff\xfe"])
async def test_invalid_body_raises(body):
    with pytest.raises(FramingError):
        await read_message(reader_with(HEADER.pack(len(body)) + body))
