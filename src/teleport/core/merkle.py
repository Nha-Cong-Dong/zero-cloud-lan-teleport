"""Merkle root của một file từ hash SHA-256 của từng khối.

Đặc tả ở docs/protocol.md mục 4.4 (kèm test vector): lá là hash của từng khối theo thứ
tự; nút cha = SHA-256(con trái ‖ con phải); tầng có số nút lẻ thì nút cuối được đẩy
thẳng lên tầng trên; root là nút còn lại cuối cùng (một lá thì root chính là lá đó).
File rỗng (0 khối): root = SHA-256(b"").

Hỗ trợ tính dạng luồng qua `MerkleStream` với bộ nhớ O(log2 N) (ngăn xếp đỉnh trung
gian) cũng như tính một lần từ `bytes`/`bytearray` qua `merkle_root`.
"""

from __future__ import annotations

import hashlib

LEAF_SIZE = 32  # SHA-256

_EMPTY_ROOT = hashlib.sha256(b"").digest()


class MerkleStream:
    """Bộ tính Merkle root dạng luồng chỉ giữ O(log2 N) đỉnh trung gian trong RAM."""

    __slots__ = ("_count", "_stack")

    def __init__(self) -> None:
        self._stack: list[tuple[int, bytes]] = []
        self._count: int = 0

    @property
    def count(self) -> int:
        """Tổng số lá đã nạp vào luồng."""
        return self._count

    @property
    def stack_depth(self) -> int:
        """Số đỉnh trung gian đang giữ trong ngăn xếp (<= floor(log2 N) + 1)."""
        return len(self._stack)

    def feed(self, leaf: bytes | bytearray | memoryview) -> None:
        """Nạp một lá 32 byte vào luồng."""
        if not isinstance(leaf, bytes | bytearray | memoryview):
            raise TypeError("leaf phải là bytes, bytearray hoặc memoryview")
        if len(leaf) != LEAF_SIZE:
            raise ValueError(f"độ dài leaf phải đúng {LEAF_SIZE} byte")
        node = bytes(leaf)
        level = 0
        sha256 = hashlib.sha256
        stack = self._stack
        while stack and stack[-1][0] == level:
            _, left = stack.pop()
            node = sha256(left + node).digest()
            level += 1
        stack.append((level, node))
        self._count += 1

    def update(self, leaves: bytes | bytearray | memoryview) -> None:
        """Nạp liên tiếp 0 hoặc nhiều lá (bội số của 32 byte) vào luồng."""
        if not isinstance(leaves, bytes | bytearray | memoryview):
            raise TypeError("leaves phải là bytes, bytearray hoặc memoryview")
        if len(leaves) % LEAF_SIZE:
            raise ValueError(f"độ dài leaves phải là bội của {LEAF_SIZE}")
        if not leaves:
            return
        with memoryview(leaves) as view:
            for start in range(0, len(view), LEAF_SIZE):
                self.feed(view[start : start + LEAF_SIZE])

    def root(self) -> bytes:
        """Tính Merkle root hiện tại mà không làm thay đổi trạng thái ngăn xếp."""
        stack = self._stack
        if not stack:
            return _EMPTY_ROOT
        acc = stack[-1][1]
        sha256 = hashlib.sha256
        for i in range(len(stack) - 2, -1, -1):
            acc = sha256(stack[i][1] + acc).digest()
        return acc


def merkle_root(leaves: bytes | bytearray) -> bytes:
    """Root 32 byte của các lá `leaves` (hash từng khối nối liền nhau, theo thứ tự khối).

    `leaves` không phải bytes/bytearray → TypeError; độ dài không chia hết cho 32 → ValueError.

    Đọc qua `memoryview` và tính trực tiếp trên ngăn xếp `MerkleStream` với bộ nhớ phụ
    O(log2 N), không cấp phát lại mảng tầng trung gian.
    """
    if not isinstance(leaves, bytes | bytearray):
        raise TypeError("leaves phải là bytes hoặc bytearray")
    if len(leaves) % LEAF_SIZE:
        raise ValueError(f"độ dài leaves phải là bội của {LEAF_SIZE}")
    if not leaves:
        return _EMPTY_ROOT
    stream = MerkleStream()
    stream.update(leaves)
    return stream.root()
