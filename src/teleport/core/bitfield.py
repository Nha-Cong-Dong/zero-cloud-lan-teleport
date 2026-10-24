"""Bitfield các khối bên nhận đã có, dùng trong `resume` (docs/protocol.md mục 3.3, 4.1).

Thứ tự bit MSB trước: khối `i` là bit `0x80 >> (i % 8)` của byte `i // 8`. Bitfield của
file `n` khối dài đúng ⌈n / 8⌉ byte, các bit thừa ở byte cuối bằng 0; trên dây là base64
chuẩn (RFC 4648, có `=` đệm), file rỗng là chuỗi rỗng `""`.

Bit bật nghĩa là khối đã khớp hash và đã nằm bền trong `.part` (protocol mục 4.3); việc
quyết định khi nào bật bit là của máy trạng thái truyền file, không phải ở đây.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator

from teleport.core.blocks import MAX_BLOCKS

# Độ dài tối đa của bitfield trên dây: file lớn nhất (`MAX_BLOCKS` khối) → 512 KiB → base64.
MAX_BITFIELD_BYTES = -(-MAX_BLOCKS // 8)
MAX_BITFIELD_BASE64_LEN = 4 * -(-MAX_BITFIELD_BYTES // 3)


def _byte_len(n: int) -> int:
    return -(-n // 8)


def _base64_len(n: int) -> int:
    return 4 * -(-_byte_len(n) // 3)


class Bitfield:
    """Bảng `n` bit, ban đầu toàn 0. `n` không phải số nguyên 0…MAX_BLOCKS → ValueError."""

    __slots__ = ("_bits", "_n")

    def __init__(self, n: int) -> None:
        if type(n) is not int or not 0 <= n <= MAX_BLOCKS:
            raise ValueError(f"n phải là số nguyên 0…{MAX_BLOCKS}")
        self._n = n
        self._bits = bytearray(_byte_len(n))

    def __repr__(self) -> str:
        return f"<Bitfield {self.count()}/{self._n}>"

    def __len__(self) -> int:
        """Số khối (số bit có nghĩa), không phải số bit đang bật."""
        return self._n

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Bitfield):
            return NotImplemented
        # Có __eq__ mà không có __hash__ nên Python tự đặt __hash__ = None: đổi được, không băm được.
        return self._n == other._n and self._bits == other._bits

    def _check_index(self, i: object) -> int:
        if type(i) is not int or not 0 <= i < self._n:
            raise IndexError(f"chỉ số khối phải là số nguyên 0…{self._n - 1}")
        return i

    def set(self, i: int) -> None:
        """Bật bit của khối `i`. `i` ngoài 0…n − 1 → IndexError."""
        i = self._check_index(i)
        self._bits[i >> 3] |= 0x80 >> (i & 7)

    def clear(self, i: int) -> None:
        """Tắt bit của khối `i`. `i` ngoài 0…n − 1 → IndexError."""
        i = self._check_index(i)
        self._bits[i >> 3] &= ~(0x80 >> (i & 7)) & 0xFF

    def __contains__(self, i: object) -> bool:
        """`i in bitfield`: khối `i` đã có chưa. Không phải chỉ số hợp lệ → False."""
        if type(i) is not int or not 0 <= i < self._n:
            return False
        return bool(self._bits[i >> 3] & (0x80 >> (i & 7)))

    def count(self) -> int:
        """Số bit đang bật."""
        return sum(b.bit_count() for b in self._bits)

    def all_set(self) -> bool:
        """Mọi khối đều đã có (file rỗng: luôn True)."""
        full, rest = divmod(self._n, 8)
        if any(b != 0xFF for b in self._bits[:full]):
            return False
        return not rest or self._bits[full] == (0xFF << (8 - rest)) & 0xFF

    def missing(self) -> Iterator[int]:
        """Các khối có bit = 0, theo thứ tự tăng dần."""
        for byte_index, b in enumerate(self._bits):
            if b == 0xFF:
                continue
            base = byte_index * 8
            for bit in range(min(8, self._n - base)):
                if not b & (0x80 >> bit):
                    yield base + bit

    def to_bytes(self) -> bytes:
        """⌈n / 8⌉ byte, MSB trước, bit thừa bằng 0."""
        return bytes(self._bits)

    def to_base64(self) -> str:
        """Dạng trên dây (base64 chuẩn có `=` đệm); file rỗng → `""`."""
        return base64.b64encode(self._bits).decode("ascii")

    @classmethod
    def from_base64(cls, s: str, n: int) -> Bitfield:
        """Đọc bitfield `n` khối nhận từ máy kia.

        Không phải base64 chuẩn (ký tự lạ, thiếu `=` đệm, khoảng trắng, dạng không chuẩn
        như bit đệm ≠ 0), sai độ dài so với ⌈n / 8⌉ byte, hay bit thừa ở cuối ≠ 0 → ValueError.
        """
        field = cls(n)
        if not isinstance(s, str):
            raise ValueError("bitfield phải là chuỗi base64")
        # Kiểm độ dài trước khi giải mã: chuỗi quá dài không tốn công giải.
        if len(s) != _base64_len(n):
            raise ValueError(f"bitfield phải dài đúng {_byte_len(n)} byte")
        raw = decode_base64(s)
        if len(raw) != _byte_len(n):
            raise ValueError(f"bitfield phải dài đúng {_byte_len(n)} byte")
        rest = n % 8
        if rest and raw[-1] & (0xFF >> rest):
            raise ValueError("các bit thừa ở cuối bitfield phải bằng 0")
        field._bits[:] = raw
        return field


def decode_base64(s: str) -> bytes:
    """Giải base64 chuẩn, chặt: chỉ nhận đúng dạng mà `base64.b64encode` sinh ra.

    Sai → ValueError. Dùng cho bitfield nhận từ máy kia (cả ở `teleport.core.messages`).
    """
    if not isinstance(s, str) or not s.isascii():
        raise ValueError("base64 chỉ gồm ký tự ASCII")
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("base64 không hợp lệ") from e
    # b64decode nhận cả dạng không chuẩn (bit đệm ≠ 0, ví dụ "AB==" giải giống "AA=="):
    # mã hóa lại phải ra đúng chuỗi ban đầu, để mỗi bitfield chỉ có một dạng trên dây.
    if base64.b64encode(raw).decode("ascii") != s:
        raise ValueError("base64 không ở dạng chuẩn")
    return raw
