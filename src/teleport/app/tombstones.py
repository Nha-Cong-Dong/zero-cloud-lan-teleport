"""Bia mộ bền: `<config_dir>/tombstones.json` (docs/architecture.md mục 4 `tombstones.json`).

Mã hóa, giải mã và kiểm từng mục ở `teleport.core.resume_state` (`TombstoneRecord`,
`encode_tombstones`, `decode_tombstones`); ở đây chỉ đọc/ghi file trong thư mục cấu hình và đổi
giữa mốc giờ tường (`at`) với tuổi mà `core.transfer` dùng (`RestoredTombstone.age`). Lọc tuổi
(`REMEMBER_TTL`) và trần số mục mỗi thiết bị là việc của `TransferEngine.restore`.

API đồng bộ (chặn): `app/transfers.py` gọi qua `DiskExecutor`, đọc hàm qua thuộc tính module
(`tombstones.load`, `tombstones.save`) để test thay được.

Log (mvp N9): chỉ loại lỗi và `errno`, không bao giờ nội dung file.
"""

from __future__ import annotations

import datetime
import logging
import math
import os
import stat
from collections.abc import Collection, Sequence
from pathlib import Path

from teleport import atomic
from teleport.core.resume_state import (
    MAX_TOMBSTONE_FILE,
    StateError,
    TombstoneRecord,
    decode_tombstones,
    encode_tombstones,
)
from teleport.core.transfer import Direction, RestoredTombstone

TOMBSTONES_NAME = "tombstones.json"

# Mở để đọc: không theo symlink (POSIX), FIFO cài sẵn không làm treo thread đĩa.
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)

log = logging.getLogger(__name__)


def load(config_dir: Path) -> list[TombstoneRecord]:
    """Đọc `tombstones.json`; không bao giờ ném lỗi (trừ khi bị ngắt).

    Chưa có file → `[]`. Không phải file thường (liên kết, thư mục…), dài quá
    `MAX_TOMBSTONE_FILE` (đọc tối đa trần + 1 byte), hỏng cả file hay lỗi đọc → log WARNING (chỉ
    loại lỗi) và `[]`. Mục hỏng bị bỏ riêng (`decode_tombstones`).
    """
    path = Path(config_dir) / TOMBSTONES_NAME
    try:
        data = _read_limited(path)
    except FileNotFoundError:
        return []
    except Exception as e:  # OSError, StateError (không phải file thường)…: không chặn app
        log.warning("không đọc được %s (%s), coi như không có bia mộ", TOMBSTONES_NAME, _label(e))
        return []
    if len(data) > MAX_TOMBSTONE_FILE:
        log.warning("%s dài quá %d byte, coi như không có bia mộ", TOMBSTONES_NAME, MAX_TOMBSTONE_FILE)
        return []
    try:
        return decode_tombstones(data)
    except Exception as e:  # StateError; phòng thủ: mọi lỗi giải mã khác
        log.warning("%s hỏng (%s), coi như không có bia mộ", TOMBSTONES_NAME, type(e).__name__)
        return []


def save(config_dir: Path, records: Sequence[TombstoneRecord]) -> None:
    """Ghi `tombstones.json` nguyên tử (`atomic.write_atomic`); vượt trần thì bỏ mục cũ nhất.

    OSError bay ra nguyên trạng; phần tử không phải `TombstoneRecord` → `StateError`.
    """
    atomic.write_atomic(Path(config_dir) / TOMBSTONES_NAME, encode_tombstones(records))


def to_restored(
    records: Sequence[TombstoneRecord], trusted_ids: Collection[str], wall_now: float
) -> list[RestoredTombstone]:
    """Mục đã đọc → `RestoredTombstone` cho `TransferEngine.restore`, giữ thứ tự.

    Chỉ giữ mục của thiết bị trong `trusted_ids`; tuổi = `wall_now − at`, kẹp ≥ 0 (đồng hồ lùi
    thì tuổi là 0). Không lọc tuổi hay số mục: `restore` làm.
    """
    trusted = frozenset(trusted_ids)
    result: list[RestoredTombstone] = []
    for record in records:
        if record.device_id not in trusted:
            continue
        age = max(0.0, wall_now - record.at.timestamp())
        result.append(
            RestoredTombstone(
                device_id=record.device_id,
                direction=Direction(record.direction),
                transfer_id=record.transfer_id,
                reason=record.reason,
                unsent=record.unsent,
                age=age,
            )
        )
    return result


def to_records(items: Sequence[RestoredTombstone], wall_now: float) -> list[TombstoneRecord]:
    """`export_tombstones` → mục để ghi: `at` = UTC(`wall_now − age`) làm tròn xuống giây.

    Mục sai (không dựng được `TombstoneRecord`, mốc ngoài khoảng biểu diễn được) → `StateError`
    hay ValueError/OverflowError/OSError của `datetime` bay ra.
    """
    result: list[TombstoneRecord] = []
    for item in items:
        seconds = math.floor(wall_now - item.age)
        at = datetime.datetime.fromtimestamp(seconds, datetime.UTC)
        result.append(
            TombstoneRecord(
                device_id=item.device_id,
                direction=str(item.direction),
                transfer_id=item.transfer_id,
                reason=item.reason,
                unsent=item.unsent,
                at=at,
            )
        )
    return result


def _read_limited(path: Path) -> bytes:
    """Đọc tối đa `MAX_TOMBSTONE_FILE` + 1 byte của một file thường; không phải file thường → StateError."""
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise StateError("tombstones.json không phải file thường")
    fd = os.open(path, _READ_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise StateError("tombstones.json không phải file thường")
        chunks: list[bytes] = []
        remaining = MAX_TOMBSTONE_FILE + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _label(e: BaseException) -> str:
    """Loại lỗi kèm `errno` nếu có; không bao giờ `str(e)` (có thể mang đường dẫn, mvp N9)."""
    number = getattr(e, "errno", None)
    return type(e).__name__ if number is None else f"{type(e).__name__} errno={number}"
