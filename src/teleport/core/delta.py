"""Tiện ích thuần (sans-IO) cho Smart Delta Teleport: dò khối 1 MiB và chỉ mục bản nền.

Module không thực hiện I/O đĩa hay mạng:
- `sample_block_indices`: chọn tối đa 4 khối mẫu (đầu, 1/3, 2/3, cuối) để sàng lọc nhanh các
  tệp khác hoàn toàn trước khi quét toàn bộ bản nền.
- `remaining_batches`: chia phần khối chưa dò thành từng lô nhỏ vừa khung `DeltaProbe`.
- `DeltaIndexEntry`, `encode_delta_index`, `decode_delta_index`: định dạng JSON bền cho
  `<config_dir>/delta_index.json` (tối đa `MAX_DELTA_INDEX_ENTRIES` mục).
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from teleport.core.blocks import MAX_FILE_SIZE
from teleport.core.messages import MAX_SAFE_INTEGER

DELTA_SAMPLE_THRESHOLD = 4
DELTA_BATCH_SIZE = 128
MAX_DELTA_INDEX_ENTRIES = 2048
DELTA_INDEX_VERSION = 1
MAX_DELTA_INDEX_BYTES = 4 * 1024 * 1024  # 4 MiB

_HEX32_RE = re.compile(r"[0-9a-f]{64}")


def sample_block_indices(total_blocks: int) -> tuple[int, ...]:
    """Chỉ số tối đa 4 khối mẫu (đầu, 1/3, 2/3, cuối), tăng ngặt và không trùng lặp."""
    if total_blocks <= 0:
        return ()
    if total_blocks <= DELTA_SAMPLE_THRESHOLD:
        return tuple(range(total_blocks))
    candidates = (0, total_blocks // 3, (2 * total_blocks) // 3, total_blocks - 1)
    return tuple(sorted(set(candidates)))


def remaining_batches(
    total_blocks: int,
    probed: Collection[int],
    batch_size: int = DELTA_BATCH_SIZE,
) -> list[tuple[int, ...]]:
    """Chia các chỉ số khối còn lại trong `[0, total_blocks)` chưa nằm trong `probed` thành từng lô."""
    if batch_size < 1:
        raise ValueError("batch_size phải >= 1")
    if total_blocks <= 0:
        return []
    seen = probed if isinstance(probed, set | frozenset) else set(probed)
    pending = [idx for idx in range(total_blocks) if idx not in seen]
    return [tuple(pending[i : i + batch_size]) for i in range(0, len(pending), batch_size)]


def delta_lookup_key(name: str, rel_path: str = "") -> str:
    """Khóa tra cứu chuẩn hóa từ tên/đường dẫn tương đối của tệp trong `Offer`."""
    raw = rel_path if rel_path else name
    parts = [part for part in raw.replace("\\", "/").strip().split("/") if part and part != "."]
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class DeltaIndexEntry:
    """Một mục trong chỉ mục tệp đã nhận để tái sử dụng làm bản nền delta."""

    key: str
    path: str
    size: int
    mtime_ns: int
    merkle_root: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key or "\x00" in self.key:
            raise ValueError("key phải là chuỗi không rỗng và không chứa NUL")
        if not isinstance(self.path, str) or not self.path or "\x00" in self.path:
            raise ValueError("path phải là chuỗi không rỗng và không chứa NUL")
        if type(self.size) is not int or not 0 <= self.size <= MAX_FILE_SIZE:
            raise ValueError("size không hợp lệ")
        if type(self.mtime_ns) is not int or self.mtime_ns < 0:
            raise ValueError("mtime_ns phải là số nguyên >= 0")
        if not isinstance(self.merkle_root, str) or (
            self.merkle_root and _HEX32_RE.fullmatch(self.merkle_root) is None
        ):
            raise ValueError("merkle_root phải rỗng hoặc đúng 64 ký tự hex thường")


def _entry_to_dict(entry: DeltaIndexEntry) -> dict[str, object]:
    data: dict[str, object] = {
        "key": entry.key,
        "path": entry.path,
        "size": entry.size,
        "mtime_ns": entry.mtime_ns,
    }
    if entry.merkle_root:
        data["merkle_root"] = entry.merkle_root
    return data


def _entry_from_dict(obj: object) -> DeltaIndexEntry | None:
    if not isinstance(obj, dict):
        return None
    key = obj.get("key")
    path = obj.get("path")
    size = obj.get("size")
    mtime_ns = obj.get("mtime_ns")
    merkle_root = obj.get("merkle_root", "")
    if (
        not isinstance(key, str)
        or not isinstance(path, str)
        or type(size) is not int
        or type(mtime_ns) is not int
    ):
        return None
    if mtime_ns > MAX_SAFE_INTEGER * 1_000_000_000:
        return None
    try:
        return DeltaIndexEntry(
            key=key,
            path=path,
            size=size,
            mtime_ns=mtime_ns,
            merkle_root=merkle_root if isinstance(merkle_root, str) else "",
        )
    except ValueError:
        return None


def encode_delta_index(entries: Sequence[DeltaIndexEntry]) -> bytes:
    """Mã hóa danh sách mục chỉ mục delta ra JSON UTF-8 (giữ tối đa `MAX_DELTA_INDEX_ENTRIES` mục cuối)."""
    bounded = list(entries[-MAX_DELTA_INDEX_ENTRIES:])
    payload = {
        "version": DELTA_INDEX_VERSION,
        "entries": [_entry_to_dict(item) for item in bounded],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return (json.dumps(payload, ensure_ascii=True, indent=2) + "\n").encode("ascii")


def decode_delta_index(raw: bytes) -> list[DeltaIndexEntry]:
    """Giải mã chỉ mục delta từ JSON UTF-8; bỏ qua các mục hỏng và giới hạn số mục."""
    if not raw or len(raw) > MAX_DELTA_INDEX_BYTES:
        return []
    try:
        obj = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return []
    if not isinstance(obj, dict):
        return []
    version = obj.get("version")
    if type(version) is not int or version < 1:
        return []
    raw_entries = obj.get("entries")
    if not isinstance(raw_entries, list):
        return []
    parsed: list[DeltaIndexEntry] = []
    for item in raw_entries:
        entry = _entry_from_dict(item)
        if entry is not None:
            parsed.append(entry)
    return parsed[-MAX_DELTA_INDEX_ENTRIES:]
