"""Khung message điều khiển: [4 byte độ dài, big-endian][JSON UTF-8].

Đặc tả ở docs/protocol.md mục 3. Module này chỉ mã hóa và giải mã bytes (sans-IO);
việc đọc/ghi trên socket nằm ở `teleport.net.stream`.

TCP là một *dòng byte* chứ không phải dòng message: một lần `write()` 100 byte
có thể đến nơi thành hai lần đọc 60 + 40 byte, hoặc dính liền với message sau.
Tiền tố độ dài cho bên nhận biết chính xác một message kết thúc ở đâu.
"""

from __future__ import annotations

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


def decode_header(header: bytes) -> int:
    """Trả về độ dài thân message; raise FramingError nếu vượt giới hạn."""
    (length,) = HEADER.unpack(header)
    if length > MAX_MESSAGE_SIZE:
        raise FramingError(f"message quá lớn: {length} byte")
    return length


def decode_body(body: bytes) -> dict:
    try:
        msg = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FramingError("nội dung không phải JSON UTF-8 hợp lệ") from e
    if not isinstance(msg, dict):
        raise FramingError("message phải là một JSON object")
    return msg
