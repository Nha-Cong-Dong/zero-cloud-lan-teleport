"""Khung message điều khiển: [4 byte độ dài, big-endian][JSON UTF-8].

Đây là định dạng của kết nối điều khiển (ke_hoach.md mục 3.4). Bài tập chat ở
Giai đoạn 0 dùng lại nó để làm quen trước khi viết giao thức thật.

TCP là một *dòng byte* chứ không phải dòng message: một lần `write()` 100 byte
có thể đến nơi thành hai lần đọc 60 + 40 byte, hoặc dính liền với message sau.
Tiền tố độ dài cho bên nhận biết chính xác một message kết thúc ở đâu.
"""

from __future__ import annotations

import asyncio
import json
import struct

HEADER = struct.Struct("!I")  # "!" = thứ tự byte mạng (big-endian), "I" = uint32

# 16 MiB: đủ cho clipboard 10MB sau khi mã hóa base64. Giới hạn này chặn việc
# một header hỏng (hoặc độc hại) bắt ta cấp phát hàng GB bộ nhớ.
MAX_MESSAGE_SIZE = 16 * 1024 * 1024


class FramingError(Exception):
    """Khung nhận được không hợp lệ: quá lớn, bị cắt giữa chừng hoặc không phải JSON object."""


def encode_message(msg: dict) -> bytes:
    body = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_MESSAGE_SIZE:
        raise FramingError(f"message quá lớn: {len(body)} byte")
    return HEADER.pack(len(body)) + body


async def read_message(reader: asyncio.StreamReader) -> dict | None:
    """Đọc một message.

    Trả về None nếu bên kia đóng kết nối đúng ở ranh giới giữa hai message
    (đóng gọn gàng). Đóng giữa chừng một message thì raise FramingError.
    """
    try:
        header = await reader.readexactly(HEADER.size)
    except asyncio.IncompleteReadError as e:
        if not e.partial:
            return None
        raise FramingError("kết nối bị cắt giữa header") from e

    (length,) = HEADER.unpack(header)
    if length > MAX_MESSAGE_SIZE:
        raise FramingError(f"message quá lớn: {length} byte")

    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as e:
        raise FramingError(f"kết nối bị cắt: mới nhận {len(e.partial)}/{length} byte") from e

    try:
        msg = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FramingError("nội dung không phải JSON UTF-8 hợp lệ") from e
    if not isinstance(msg, dict):
        raise FramingError("message phải là một JSON object")
    return msg


async def write_message(writer: asyncio.StreamWriter, msg: dict) -> None:
    writer.write(encode_message(msg))
    # drain() chờ khi bộ đệm gửi vượt ngưỡng: đây là backpressure ở mức nhỏ nhất.
    await writer.drain()
