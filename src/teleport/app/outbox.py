"""Hàng đợi gửi bền trên đĩa: `<config_dir>/outbox.json`.

Đọc/ghi danh sách `OutboxRecord` trong thư mục cấu hình và kiểm lại từng file/thư mục nguồn
trên đĩa trước khi giao cho `TransferEngine.restore`. Mã hóa/giải mã JSON ở `core/outbox.py`.

API đồng bộ (chặn): `app/transfers.py` gọi qua `DiskExecutor`, đọc hàm qua thuộc tính module
(`outbox.load`, `outbox.save`, `outbox.verify_record_sources`) để test thay được.

Log (mvp N9): chỉ loại lỗi và `errno`, không bao giờ tên hay đường dẫn file nguồn.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Sequence
from pathlib import Path

from teleport import atomic
from teleport.app import sources
from teleport.app.sources import SourceChanged, SourceFile, SourceReader
from teleport.core.outbox import (
    MAX_OUTBOX_FILE,
    OutboxError,
    OutboxRecord,
    decode_outbox,
    encode_outbox,
)
from teleport.core.transfer import RestoredSend

OUTBOX_NAME = "outbox.json"

_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)

log = logging.getLogger(__name__)


def load(config_dir: Path) -> list[OutboxRecord]:
    """Đọc `outbox.json`; không bao giờ ném lỗi (trừ khi bị ngắt).

    Chưa có file → `[]`. Không phải file thường, vượt `MAX_OUTBOX_FILE` hoặc lỗi đọc/giải mã →
    log WARNING (chỉ loại lỗi) và `[]`. Mục hỏng bị bỏ riêng (`decode_outbox`).
    """
    path = Path(config_dir) / OUTBOX_NAME
    try:
        data = _read_limited(path)
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning("không đọc được %s (%s), bỏ qua hàng đợi gửi", OUTBOX_NAME, _label(e))
        return []
    if len(data) > MAX_OUTBOX_FILE:
        log.warning("%s dài quá %d byte, bỏ qua hàng đợi gửi", OUTBOX_NAME, MAX_OUTBOX_FILE)
        return []
    try:
        return decode_outbox(data)
    except Exception as e:
        log.warning("%s hỏng (%s), bỏ qua hàng đợi gửi", OUTBOX_NAME, type(e).__name__)
        return []


def save(config_dir: Path, records: Sequence[OutboxRecord]) -> None:
    """Ghi `outbox.json` nguyên tử (`atomic.write_atomic`)."""
    atomic.write_atomic(Path(config_dir) / OUTBOX_NAME, encode_outbox(records))


def verify_record_sources(record: OutboxRecord) -> list[SourceFile] | None:
    """Kiểm mọi đường dẫn nguồn của `record` còn nguyên trên đĩa; sai lệch → None."""
    result: list[SourceFile] = []
    for path_str, entry in zip(record.paths, record.offer.files, strict=True):
        path = Path(path_str)
        try:
            with SourceReader.open(path, entry):
                pass
        except (OSError, SourceChanged, sources.SourceError, ValueError):
            return None
        result.append(SourceFile(path, entry))
    return result


def to_restored_send(record: OutboxRecord, wall_now: float) -> RestoredSend:
    """Chuyển `OutboxRecord` thành `RestoredSend` cho `TransferEngine.restore`."""
    age = max(0.0, wall_now - record.created_at.timestamp())
    roots = tuple((idx, bytes.fromhex(digest)) for idx, digest in record.roots)
    return RestoredSend(
        device_id=record.device_id,
        offer=record.offer,
        age=age,
        roots=roots,
    )


def _read_limited(path: Path) -> bytes:
    """Đọc tối đa `MAX_OUTBOX_FILE` + 1 byte của một file thường."""
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise OutboxError("outbox.json không phải file thường")
    fd = os.open(path, _READ_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OutboxError("outbox.json không phải file thường")
        chunks: list[bytes] = []
        remaining = MAX_OUTBOX_FILE + 1
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
    number = getattr(e, "errno", None)
    return type(e).__name__ if number is None else f"{type(e).__name__} errno={number}"
