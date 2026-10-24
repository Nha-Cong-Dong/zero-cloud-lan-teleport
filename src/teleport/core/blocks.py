"""Khung khối nhị phân trên kết nối dữ liệu và cách chia file thành khối.

Đặc tả ở docs/protocol.md mục 4.2. Mỗi khối đi trên dây là một header 41 byte
`[u8 type = 0x02][u32 block_index][u32 length][32 byte SHA-256(payload)]` (big-endian)
rồi tới `length` byte payload. Hash nằm trong header của chính khối đó: bên gửi chỉ
giữ một khối trong RAM, và hash không bao giờ tới sau dữ liệu.

Module chỉ tính toán (sans-IO): payload không đi qua đây, chỉ header và `(index, sha256)`.
Đọc payload từ socket, băm và so với `sha256` là việc của tầng gọi.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

BLOCK_SIZE = 1 << 20  # 1 MiB, cũng là `block_size` duy nhất hợp lệ trong `offer`
BLOCK_TYPE = 0x02
# Không đặt tên `HEADER` để khỏi nhầm với `teleport.core.framing.HEADER` (khung message).
BLOCK_HEADER = struct.Struct("!BII32s")  # 41 byte
DIGEST_SIZE = 32  # SHA-256

# Trần kích thước một file (4 TiB, protocol mục 3.3): `resume` của file lớn nhất có
# bitfield 512 KiB, mã hóa base64 vẫn vừa trần 1 MiB của message điều khiển.
MAX_FILE_SIZE = 1 << 42
MAX_BLOCKS = MAX_FILE_SIZE // BLOCK_SIZE  # 4 194 304 khối


class BlockError(ValueError):
    """Header khối sai: sai độ dài, `type` lạ, `block_index` ngoài phạm vi hay `length` sai."""


@dataclass(frozen=True, slots=True)
class BlockHeader:
    """Header của một khung khối đã giải mã (chưa đối chiếu với kích thước file)."""

    index: int
    length: int
    sha256: bytes


def _check_size(size: object) -> int:
    # bool là lớp con của int: bị từ chối như mọi kiểu khác.
    if type(size) is not int or not 0 <= size <= MAX_FILE_SIZE:
        raise ValueError(f"size phải là số nguyên 0…{MAX_FILE_SIZE}")
    return size


def block_count(size: int) -> int:
    """Số khối của file `size` byte: ⌈size / BLOCK_SIZE⌉; file rỗng có 0 khối.

    `size` không phải số nguyên 0…`MAX_FILE_SIZE` → ValueError.
    """
    return -(-_check_size(size) // BLOCK_SIZE)


def block_length(size: int, index: int) -> int:
    """Độ dài khối `index` của file `size` byte: `BLOCK_SIZE`, riêng khối cuối có thể ngắn hơn.

    `size` sai hoặc `index` ngoài 0…block_count(size) − 1 → ValueError.
    """
    count = block_count(size)
    if type(index) is not int or not 0 <= index < count:
        raise ValueError(f"index phải là số nguyên 0…{count - 1}")
    return min(BLOCK_SIZE, size - index * BLOCK_SIZE)


def pack_header(index: int, length: int, sha256: bytes) -> bytes:
    """Mã hóa header 41 byte của khối `index` có `length` byte payload và hash `sha256`.

    Tham số sai (index ngoài uint32, length ngoài 1…BLOCK_SIZE, hash khác 32 byte) → ValueError.
    """
    if type(index) is not int or not 0 <= index <= 0xFFFFFFFF:
        raise ValueError("index phải là số nguyên 0…2³² − 1")
    if type(length) is not int or not 1 <= length <= BLOCK_SIZE:
        raise ValueError(f"length phải là số nguyên 1…{BLOCK_SIZE}")
    if not isinstance(sha256, bytes) or len(sha256) != DIGEST_SIZE:
        raise ValueError(f"sha256 phải là đúng {DIGEST_SIZE} byte")
    return BLOCK_HEADER.pack(BLOCK_TYPE, index, length, sha256)


def parse_header(data: bytes | bytearray | memoryview) -> BlockHeader:
    """Giải mã header 41 byte nhận từ máy kia.

    Khác 41 byte, `type` khác 0x02, hay `length` ngoài 1…BLOCK_SIZE → BlockError. Chưa
    kiểm `block_index` và `length` theo kích thước file: việc đó là `check_block`.
    """
    if len(data) != BLOCK_HEADER.size:
        raise BlockError(f"header khối phải dài đúng {BLOCK_HEADER.size} byte")
    block_type, index, length, sha256 = BLOCK_HEADER.unpack(data)
    if block_type != BLOCK_TYPE:
        raise BlockError(f"type khối lạ: 0x{block_type:02x}")
    if not 1 <= length <= BLOCK_SIZE:
        # Chặn trước khi đọc payload: máy kia không bắt được bên nhận đọc quá 1 MiB.
        raise BlockError(f"length phải là 1…{BLOCK_SIZE}")
    return BlockHeader(index=index, length=length, sha256=sha256)


def check_block(header: BlockHeader, size: int) -> None:
    """Header có khớp file `size` byte không: `block_index` trong phạm vi, `length` đúng bằng
    độ dài của khối đó (protocol mục 4.2). Sai → BlockError; `size` sai → ValueError.
    """
    count = block_count(size)
    if not 0 <= header.index < count:
        raise BlockError(f"block_index {header.index} ngoài phạm vi 0…{count - 1}")
    expected = block_length(size, header.index)
    if header.length != expected:
        raise BlockError(f"khối {header.index} phải dài {expected} byte, header ghi {header.length}")
