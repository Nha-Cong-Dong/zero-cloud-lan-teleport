"""Hàng đợi gửi bền của bên gửi (`outbox.json`, sans-IO).

Mã hóa và giải mã danh sách các lượt gửi chưa kết thúc (`OutboxRecord`) để bên gửi giữ
được lượt truyền qua các lần tắt hoặc sập ứng dụng. Module chỉ xử lý dữ liệu trong bộ nhớ:
không đọc/ghi đĩa, không xem đồng hồ (`app/outbox.py` chịu trách nhiệm I/O nguyên tử và kiểm
lại nguồn trên đĩa).

Mọi lỗi tham số hay dữ liệu hỏng là `OutboxError` (lớp con của `StateError`).
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from teleport.core.blocks import BLOCK_SIZE
from teleport.core.messages import (
    MessageError,
    Offer,
    validate_device_id,
    validate_transfer_id,
)
from teleport.core.resume_state import (
    RECORD_VERSION,
    StateError,
    check_time,
    format_time,
    parse_time,
)

MAX_OUTBOX_FILE = 4 * 1024 * 1024  # trần kích thước `outbox.json`, byte (4 MiB)
MAX_OUTBOX_ENTRIES = 64  # số lượt gửi tối đa lưu trong `outbox.json`

_HEX32_RE = re.compile(r"[0-9a-f]{64}")
_EMPTY_JSON = f'{{\n  "version": {RECORD_VERSION},\n  "entries": []\n}}\n'.encode()


class OutboxError(StateError):
    """Tham số sai, hoặc dữ liệu `outbox.json` hỏng hay vượt trần."""


def _reject_constant(name: str) -> object:
    raise OutboxError(f"JSON không được chứa {name}")


def _check_device_id(value: object) -> str:
    try:
        return str.__str__(validate_device_id(value))
    except MessageError:
        raise OutboxError("device_id phải là đúng 32 ký tự hex thường") from None


def _check_paths(paths: object, expected_len: int) -> tuple[str, ...]:
    if isinstance(paths, str | bytes | bytearray) or not isinstance(paths, Sequence):
        raise OutboxError("paths phải là dãy đường dẫn")
    if len(paths) != expected_len:
        raise OutboxError(f"số paths ({len(paths)}) phải bằng số file trong offer ({expected_len})")
    cleaned: list[str] = []
    for item in paths:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise OutboxError("mỗi path phải là chuỗi không rỗng và không chứa ký tự null")
        cleaned.append(item if type(item) is str else str.__str__(item))
    return tuple(cleaned)


def _check_roots(roots: object, n_files: int) -> tuple[tuple[int, str], ...]:
    if isinstance(roots, str | bytes | bytearray) or not isinstance(roots, Sequence):
        raise OutboxError("roots phải là dãy cặp (file_index, merkle_root)")
    seen: set[int] = set()
    normalized: list[tuple[int, str]] = []
    for pair in roots:
        if isinstance(pair, str | bytes) or not isinstance(pair, Sequence) or len(pair) != 2:
            raise OutboxError("mỗi phần tử của roots phải là cặp (file_index, merkle_root)")
        idx, digest = pair
        if type(idx) is not int or not 0 <= idx < n_files:
            raise OutboxError(f"chỉ số file trong roots không hợp lệ: {idx!r}")
        if idx in seen:
            raise OutboxError(f"trùng chỉ số file {idx} trong roots")
        if not isinstance(digest, str) or _HEX32_RE.fullmatch(digest) is None:
            raise OutboxError("merkle_root trong roots phải là đúng 64 ký tự hex thường")
        seen.add(idx)
        normalized.append((idx, str.__str__(digest)))
    normalized.sort(key=lambda item: item[0])
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """Một lượt gửi chưa kết thúc lưu trong `outbox.json`.

    - `device_id`: mã thiết bị nhận (32 hex thường).
    - `offer`: manifest của lượt gửi (`transfer_id`, `block_size`, `files`).
    - `paths`: dãy đường dẫn nguồn trên đĩa, tương ứng 1-1 với `offer.files`.
    - `created_at`: thời điểm tạo lượt (UTC, làm tròn xuống giây).
    - `roots`: các cặp `(file_index, merkle_root_hex)` của những file đã từng phát `Complete`.
    """

    device_id: str
    offer: Offer
    paths: tuple[str, ...]
    created_at: datetime.datetime
    roots: tuple[tuple[int, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "device_id", _check_device_id(self.device_id))
        if not isinstance(self.offer, Offer):
            raise OutboxError("offer phải là Offer")
        n_files = len(self.offer.files)
        object.__setattr__(self, "paths", _check_paths(self.paths, n_files))
        try:
            checked_time = check_time(self.created_at, "created_at")
        except StateError as e:
            raise OutboxError(str(e)) from None
        object.__setattr__(self, "created_at", checked_time)
        object.__setattr__(self, "roots", _check_roots(self.roots, n_files))

    def with_root(self, file_index: int, merkle_root_hex: str) -> OutboxRecord:
        """Trả bản sao mới có thêm/cập nhật `(file_index, merkle_root_hex)`."""
        existing = {idx: root for idx, root in self.roots}
        existing[file_index] = merkle_root_hex
        return OutboxRecord(
            device_id=self.device_id,
            offer=self.offer,
            paths=self.paths,
            created_at=self.created_at,
            roots=tuple(sorted(existing.items())),
        )

    def to_dict(self) -> dict[str, object]:
        obj: dict[str, object] = {
            "device_id": self.device_id,
            "transfer_id": self.offer.transfer_id,
            "block_size": self.offer.block_size,
            "files": [f.to_dict() for f in self.offer.files],
            "paths": list(self.paths),
            "created_at": format_time(self.created_at),
        }
        if self.roots:
            obj["roots"] = {str(idx): digest for idx, digest in self.roots}
        return obj


def _serialize_entries(items: Sequence[OutboxRecord]) -> bytes:
    if not items:
        return _EMPTY_JSON
    payload = {
        "version": RECORD_VERSION,
        "entries": [item.to_dict() for item in items],
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def encode_outbox(records: Sequence[OutboxRecord]) -> bytes:
    """Mã hóa danh sách `OutboxRecord` thành JSON UTF-8 (`<= MAX_OUTBOX_FILE`, tối đa 64 mục).

    Nếu vượt `MAX_OUTBOX_ENTRIES` hoặc `MAX_OUTBOX_FILE`, lần lượt bỏ các mục cũ nhất ở đầu
    danh sách tới khi vừa trần. Phần tử không phải `OutboxRecord` → `OutboxError`.
    """
    if isinstance(records, str | bytes | bytearray) or not isinstance(records, Sequence):
        raise OutboxError("records phải là một dãy OutboxRecord")
    items = list(records)
    if not all(isinstance(item, OutboxRecord) for item in items):
        raise OutboxError("mọi phần tử của records phải là OutboxRecord")
    if len(items) > MAX_OUTBOX_ENTRIES:
        items = items[-MAX_OUTBOX_ENTRIES:]
    data = _serialize_entries(items)
    while items and len(data) > MAX_OUTBOX_FILE:
        items.pop(0)
        data = _serialize_entries(items)
    if len(data) > MAX_OUTBOX_FILE:
        raise OutboxError(f"outbox.json dài quá {MAX_OUTBOX_FILE} byte")
    return data


def _roots_from_dict(raw: object) -> tuple[tuple[int, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise OutboxError("roots phải là JSON object")
    pairs: list[tuple[int, str]] = []
    for key, val in raw.items():
        if not isinstance(key, str) or not key.isascii() or not key.isdigit():
            raise OutboxError("khóa của roots phải là chỉ số nguyên không âm")
        idx = int(key)
        if str(idx) != key:
            raise OutboxError("khóa của roots không được có số 0 đứng đầu")
        if not isinstance(val, str):
            raise OutboxError("merkle_root trong roots phải là chuỗi")
        pairs.append((idx, val))
    return tuple(pairs)


def _record_from_dict(obj: object) -> OutboxRecord:
    if not isinstance(obj, dict):
        raise OutboxError("mỗi mục trong outbox.json phải là JSON object")
    for key in ("device_id", "transfer_id", "block_size", "files", "paths", "created_at"):
        if key not in obj:
            raise OutboxError(f"mục outbox thiếu trường {key}")
    if type(obj["block_size"]) is not int or obj["block_size"] != BLOCK_SIZE:
        raise OutboxError(f"block_size phải là {BLOCK_SIZE}")
    transfer_id = obj["transfer_id"]
    try:
        validate_transfer_id(transfer_id)
        offer = Offer(transfer_id=transfer_id, block_size=BLOCK_SIZE, files=obj["files"])
        created_at = parse_time(obj["created_at"], "created_at")
    except (MessageError, StateError) as e:
        raise OutboxError(f"mục outbox không hợp lệ: {e}") from None
    roots = _roots_from_dict(obj.get("roots"))
    return OutboxRecord(
        device_id=obj["device_id"],
        offer=offer,
        paths=obj["paths"],
        created_at=created_at,
        roots=roots,
    )


def decode_outbox(data: bytes | bytearray | memoryview) -> list[OutboxRecord]:
    """Đọc `outbox.json`; bỏ qua riêng từng mục hỏng, sai cấu trúc gốc → `OutboxError`."""
    if not isinstance(data, bytes | bytearray | memoryview):
        raise OutboxError("data phải là bytes")
    with memoryview(data) as view:
        if view.nbytes > MAX_OUTBOX_FILE:
            raise OutboxError(f"outbox.json dài quá {MAX_OUTBOX_FILE} byte")
    try:
        obj = json.loads(bytes(data).decode("utf-8-sig"), parse_constant=_reject_constant)
    except OutboxError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise OutboxError("outbox.json không phải JSON UTF-8 hợp lệ") from None
    if not isinstance(obj, dict):
        raise OutboxError("outbox.json phải là một JSON object")
    for key in ("version", "entries"):
        if key not in obj:
            raise OutboxError(f"outbox.json thiếu trường {key}")
    if type(obj["version"]) is not int or obj["version"] != RECORD_VERSION:
        raise OutboxError(f"outbox.json: version phải là {RECORD_VERSION}")
    entries = obj["entries"]
    if not isinstance(entries, list):
        raise OutboxError("entries phải là một mảng")
    result: list[OutboxRecord] = []
    for item in entries:
        try:
            result.append(_record_from_dict(item))
        except OutboxError:
            continue
    return result
