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

# Độ sâu lồng tối đa của JSON (object ngoài cùng tính là 1). Message thật chỉ sâu
# vài cấp; giới hạn rõ ràng để không phụ thuộc vào việc bộ giải mã có gặp
# RecursionError hay không — trên Python 3.14 Linux/macOS, JSON lồng 100.000 cấp
# vẫn giải mã được bình thường.
MAX_JSON_DEPTH = 64


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
    except ValueError as e:
        # Số nguyên quá dài (> 4300 chữ số): Python từ chối đổi sang int.
        raise FramingError("JSON chứa số nguyên quá dài") from e
    except RecursionError as e:
        # Trên một số nền tảng, bộ giải mã vượt giới hạn đệ quy trước khi tới _depth_exceeds.
        raise FramingError("JSON lồng quá sâu") from e
    if not isinstance(msg, dict):
        raise FramingError("message phải là một JSON object")
    # Số dấu "[" và "{" là cận trên của độ sâu: ít hơn giới hạn thì khỏi duyệt cây.
    if body.count(b"[") + body.count(b"{") > MAX_JSON_DEPTH and _depth_exceeds(msg, MAX_JSON_DEPTH):
        raise FramingError(f"JSON lồng quá {MAX_JSON_DEPTH} cấp")
    return msg


def _depth_exceeds(value: object, limit: int) -> bool:
    """Duyệt bằng ngăn xếp (không đệ quy) để chính việc kiểm tra không tràn stack."""
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            return True
        children = node.values() if isinstance(node, dict) else node
        stack.extend((child, depth + 1) for child in children if isinstance(child, dict | list))
    return False
