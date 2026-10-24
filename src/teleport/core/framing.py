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
from array import array
from itertools import accumulate

HEADER = struct.Struct("!I")  # "!" = thứ tự byte mạng (big-endian), "I" = uint32

# 1 MiB cho kết nối điều khiển (docs/protocol.md mục 2.3): đủ cho `offer` 1000 file và
# `resume` của file lớn nhất; clipboard được chia mảnh nhỏ hơn (mục 3.2). Giới hạn này
# chặn việc một header hỏng (hoặc độc hại) bắt ta cấp phát nhiều bộ nhớ, và giữ mỗi lần
# giải mã JSON (chạy trên event loop) ở mức ngắn.
MAX_MESSAGE_SIZE = 1024 * 1024

# Kết nối ghép đôi chưa xác thực ai cả (docs/protocol.md mục 5): message `pair_*` lớn
# nhất (`pair_request` kèm chứng chỉ ≤ 8192 ký tự) chỉ khoảng 8,3 KiB, nên giới hạn
# chặt hơn hẳn để máy lạ không bắt ta cấp phát 1 MiB cho mỗi kết nối.
MAX_PAIRING_MESSAGE_SIZE = 16 * 1024

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


def decode_header(header: bytes | bytearray, max_size: int = MAX_MESSAGE_SIZE) -> int:
    """Trả về độ dài thân message; raise FramingError nếu vượt `max_size`."""
    (length,) = HEADER.unpack(header)
    if length > max_size:
        raise FramingError(f"message quá lớn: {length} byte")
    return length


def decode_body(body: bytes | bytearray) -> dict:
    """Giải mã thân khung thành JSON object; nhận cả `bytearray` để bên đọc khỏi chép thêm.

    Thân phải là UTF-8 (protocol mục 3.1): giải mã UTF-8 tường minh rồi mới đưa chuỗi cho
    `json.loads`, vì `json.loads(bytes)` tự dò và nhận cả UTF-16/UTF-32. Có BOM UTF-8 cũng
    là lỗi (Teleport không bao giờ gửi BOM).
    """
    try:
        msg = json.loads(body.decode("utf-8"))
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
    # Số dấu "[" và "{" là cận trên của độ sâu: ít hơn giới hạn thì khỏi kiểm.
    if body.count(b"[") + body.count(b"{") > MAX_JSON_DEPTH and _depth_exceeds(body, MAX_JSON_DEPTH):
        raise FramingError(f"JSON lồng quá {MAX_JSON_DEPTH} cấp")
    return msg


# Cho `_depth_exceeds`: gộp "{}" vào "[]", bỏ mọi byte khác ngoài dấu nháy và ngoặc;
# rồi đổi "[" → +1, "]" → −1 (byte có dấu).
_BRACKETS_ONLY = bytes.maketrans(b"{}", b"[]")
_NOT_BRACKET_OR_QUOTE = bytes(i for i in range(256) if i not in b'"[]{}')
_BRACKET_STEP = bytes.maketrans(b"[]", b"\x01\xff")


def _depth_exceeds(body: bytes | bytearray, limit: int) -> bool:
    """Độ sâu lồng của `body` (JSON **hợp lệ**, đã qua `json.loads`) có vượt `limit` không.

    Đếm trên bytes thay vì duyệt cây đã giải mã: mọi bước là thao tác bytes chạy trong C,
    nên khung 1 MiB xấu nhất (hàng trăm nghìn container) chỉ tốn vài chục ms, không
    đệ quy. Cách đếm như duyệt cây: object ngoài cùng là cấp 1, mỗi container lồng thêm
    một cấp; ngoặc nằm trong chuỗi không tính. Đếm theo văn bản nên khóa trùng cũng tính,
    kể cả nhánh bị `json.loads` ghi đè (chặt hơn duyệt cây). Chỉ đúng với JSON hợp lệ: ngoài chuỗi
    không có `\\` hay `"`, và trong chuỗi mọi `\\` mở đầu một escape.
    """
    # 1. Bỏ escape: chạy `\\` trước (replace ghép cặp từ trái, như bộ giải mã), rồi `\"`.
    #    Sau đó mọi `"` còn lại đều là biên chuỗi. `"` và `\` không bao giờ nằm trong một
    #    ký tự UTF-8 nhiều byte, nên làm trên bytes là đúng.
    marks = body.replace(b"\\\\", b"").replace(b'\\"', b"")
    # 2. Chỉ giữ `"` và ngoặc.
    marks = marks.translate(_BRACKETS_ONLY, _NOT_BRACKET_OR_QUOTE)
    # 3. Bỏ các cặp `""` liền nhau: không đổi tính chẵn lẻ của số `"` đứng trước mỗi ngoặc,
    #    tức không đổi ngoặc nào nằm trong chuỗi (và bớt hẳn số mảnh ở bước 4).
    marks = marks.replace(b'""', b"")
    # 4. Ngoặc ngoài chuỗi là các mảnh vị trí chẵn khi tách theo `"`.
    if b'"' in marks:
        marks = b"".join(marks.split(b'"')[::2])
    # 5. Độ sâu = tổng tiền tố lớn nhất của +1 / −1.
    return max(accumulate(array("b", marks.translate(_BRACKET_STEP))), default=0) > limit
