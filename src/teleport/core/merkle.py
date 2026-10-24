"""Merkle root của một file từ hash SHA-256 của từng khối.

Đặc tả ở docs/protocol.md mục 4.4 (kèm test vector): lá là hash của từng khối theo thứ
tự; nút cha = SHA-256(con trái ‖ con phải); tầng có số nút lẻ thì nút cuối được đẩy
thẳng lên tầng trên; root là nút còn lại cuối cùng (một lá thì root chính là lá đó).
File rỗng (0 khối): root = SHA-256(b"").

Hash các khối được giữ liền nhau trong một `bytearray` 32·N byte (không phải list N
đối tượng `bytes`), nên file 10 GiB chỉ tốn 320 KiB.
"""

from __future__ import annotations

import hashlib

LEAF_SIZE = 32  # SHA-256

_EMPTY_ROOT = hashlib.sha256(b"").digest()


def merkle_root(leaves: bytes | bytearray) -> bytes:
    """Root 32 byte của các lá `leaves` (hash từng khối nối liền nhau, theo thứ tự khối).

    `leaves` không phải bytes/bytearray → TypeError; độ dài không chia hết cho 32 → ValueError.

    Tầng lá được đọc thẳng qua `memoryview`, không chép; mỗi tầng trên chỉ bằng một nửa tầng
    dưới. Tốn khoảng N lần SHA-256 trên 64 byte (file 4 TiB: ~4 triệu lần, cỡ vài giây), nên
    với file lớn tầng `app` phải gọi hàm này qua `asyncio.to_thread`, không gọi trên event loop.
    Không được sửa `leaves` trong lúc hàm đang chạy.
    """
    if not isinstance(leaves, bytes | bytearray):
        raise TypeError("leaves phải là bytes hoặc bytearray")
    if len(leaves) % LEAF_SIZE:
        raise ValueError(f"độ dài leaves phải là bội của {LEAF_SIZE}")
    if not leaves:
        return _EMPTY_ROOT
    with memoryview(leaves) as view:
        level = _parent_level(view)
    while len(level) > LEAF_SIZE:
        level = _parent_level(memoryview(level))
    return bytes(level)


def _parent_level(level: memoryview) -> bytearray:
    """Tầng trên của `level` (≥ 1 nút); một nút thì trả lại đúng nút đó."""
    count = len(level) // LEAF_SIZE
    parent = bytearray()
    sha256 = hashlib.sha256
    for start in range(0, (count - 1) * LEAF_SIZE, 2 * LEAF_SIZE):
        parent += sha256(level[start : start + 2 * LEAF_SIZE]).digest()
    if count % 2:
        parent += level[-LEAF_SIZE:]  # nút lẻ cuối tầng: đẩy thẳng lên
    return parent
