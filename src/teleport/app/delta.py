"""Dịch vụ I/O đĩa cho Smart Delta Teleport: dò khối 1 MiB, chỉ mục bản nền và chép khối khớp.

Mọi hàm I/O trong module là đồng bộ (chặn) và chỉ gọi qua `DiskExecutor`, không gọi trên
event loop. Tuân thủ mvp N9: không ghi tên file hay nội dung file vào log.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from pathlib import Path

from teleport.app import sources
from teleport.app.sources import SourceChanged, _open_regular, _read_block_digest
from teleport.app.storage import PartFile, RereadStopped
from teleport.atomic import write_atomic
from teleport.core.blocks import BLOCK_SIZE, block_length
from teleport.core.delta import (
    MAX_DELTA_INDEX_BYTES,
    MAX_DELTA_INDEX_ENTRIES,
    DeltaIndexEntry,
    decode_delta_index,
    delta_lookup_key,
    encode_delta_index,
)
from teleport.core.filenames import TEMP_DIR_NAME, sanitize_filename, sanitize_rel_path
from teleport.core.messages import FileEntry
from teleport.core.transfer import mtime_ms

DELTA_INDEX_FILE = "delta_index.json"
_REPARSE_TAG_NAME_SURROGATE = 0x20000000
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _is_link_or_reparse(st: os.stat_result) -> bool:
    if stat.S_ISLNK(st.st_mode):
        return True
    if sys.platform == "win32":
        attrs = getattr(st, "st_file_attributes", 0)
        tag = getattr(st, "st_reparse_tag", 0)
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT) and bool(tag & _REPARSE_TAG_NAME_SURROGATE)
    return False


def is_safe_regular_file(path: Path) -> bool:
    """True nếu `path` tồn tại, là file thường (không symlink/junction) và kích thước > 0."""
    try:
        if path.is_symlink():
            return False
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or _is_link_or_reparse(st):
        return False
    return st.st_size > 0


class DeltaIndexStore:
    """Bộ nhớ chỉ mục các tệp đã nhận (`<config_dir>/delta_index.json`), giới hạn kiểu LRU."""

    def __init__(self, entries: Sequence[DeltaIndexEntry] = ()) -> None:
        self._entries: OrderedDict[tuple[str, str], DeltaIndexEntry] = OrderedDict()
        for entry in entries[-MAX_DELTA_INDEX_ENTRIES:]:
            self._entries[(entry.key, entry.path)] = entry

    def entries(self) -> list[DeltaIndexEntry]:
        """Danh sách mục theo thứ tự cũ trước, mới sau."""
        return list(self._entries.values())

    @classmethod
    def load(cls, config_dir: Path) -> DeltaIndexStore:
        """Đọc `delta_index.json`; file thiếu hoặc hỏng trả kho rỗng."""
        path = config_dir / DELTA_INDEX_FILE
        try:
            with path.open("rb") as f:
                raw = f.read(MAX_DELTA_INDEX_BYTES + 1)
        except OSError:
            return cls()
        return cls(decode_delta_index(raw))

    def save(self, config_dir: Path) -> None:
        """Ghi nguyên tử `delta_index.json`."""
        write_atomic(config_dir / DELTA_INDEX_FILE, encode_delta_index(self.entries()))

    def record(
        self,
        key: str,
        path: str | Path,
        size: int,
        mtime_ns: int,
        merkle_root: str = "",
    ) -> None:
        """Thêm hoặc cập nhật mục mới nhất cho `(key, path)` (giữ tối đa `MAX_DELTA_INDEX_ENTRIES`)."""
        try:
            entry = DeltaIndexEntry(
                key=delta_lookup_key(key) or key,
                path=os.fspath(path),
                size=size,
                mtime_ns=max(0, int(mtime_ns)),
                merkle_root=merkle_root,
            )
        except (TypeError, ValueError):
            return
        k = (entry.key, entry.path)
        self._entries.pop(k, None)
        self._entries[k] = entry
        while len(self._entries) > MAX_DELTA_INDEX_ENTRIES:
            self._entries.popitem(last=False)

    def lookup(self, key: str) -> list[DeltaIndexEntry]:
        """Tìm các mục khớp `key` (ưu tiên mới nhất trước; hỗ trợ so không phân biệt hoa thường)."""
        clean = delta_lookup_key(key) or key
        exact: list[DeltaIndexEntry] = []
        fold: list[DeltaIndexEntry] = []
        target_cf = clean.casefold()
        for entry in reversed(self._entries.values()):
            if entry.key == clean:
                exact.append(entry)
            elif entry.key.casefold() == target_cf:
                fold.append(entry)
        return [*exact, *fold]


def _candidate_in_dir(dest_dir: Path, entry: FileEntry) -> Path | None:
    if entry.rel_path:
        clean_rel = sanitize_rel_path(entry.rel_path)
        candidate = dest_dir.joinpath(*clean_rel.split("/"))
    else:
        candidate = dest_dir / sanitize_filename(entry.name)
    try:
        base_resolved = dest_dir.resolve()
        cand_resolved = candidate.resolve()
        if not cand_resolved.is_relative_to(base_resolved):
            return None
        if TEMP_DIR_NAME in cand_resolved.relative_to(base_resolved).parts:
            return None
    except OSError:
        return None
    return candidate


def find_basis_file(
    entry: FileEntry,
    dest_dir: Path | None,
    index_store: DeltaIndexStore | None,
    *,
    subfolder: str | None = None,
) -> Path | None:
    """Tìm bản nền cục bộ hợp lệ trên đĩa cho `entry`, ưu tiên thư mục nhận rồi đến chỉ mục."""
    candidates: list[Path] = []
    if dest_dir is not None:
        if subfolder:
            sub_dir = dest_dir / subfolder
            cand = _candidate_in_dir(sub_dir, entry)
            if cand is not None:
                candidates.append(cand)
        direct = _candidate_in_dir(dest_dir, entry)
        if direct is not None:
            candidates.append(direct)
    if index_store is not None:
        lookup_key = delta_lookup_key(entry.name, entry.rel_path)
        for item in index_store.lookup(lookup_key):
            candidates.append(Path(item.path))
    seen: set[str] = set()
    for cand in candidates:
        norm = os.path.normcase(os.path.abspath(os.fspath(cand)))
        if norm in seen:
            continue
        seen.add(norm)
        if is_safe_regular_file(cand):
            return cand
    return None


def check_source_blocks(
    source_path: Path,
    expected_size: int,
    hashes: Sequence[tuple[int, str]],
    *,
    expected_mtime: int | None = None,
) -> list[tuple[int, bytes]]:
    """Đọc các khối được yêu cầu từ file nguồn bên gửi và trả những khối có SHA-256 khớp."""
    with _open_regular(source_path) as file:
        st = os.fstat(file.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size != expected_size:
            raise SourceChanged("file nguồn đã đổi kích thước hoặc không còn là file thường")
        if expected_mtime is not None and mtime_ms(st.st_mtime_ns) != expected_mtime:
            raise SourceChanged("file nguồn đã đổi thời điểm sửa")
        matched: list[tuple[int, bytes]] = []
        for idx, expected_hex in hashes:
            _, digest = _read_block_digest(file, expected_size, idx)
            if digest.hex() == expected_hex:
                matched.append((idx, digest))
        return matched


def _open_basis_fd(basis_path: Path) -> int:
    flags = os.O_RDONLY | _O_BINARY
    if sys.platform != "win32":
        flags |= _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC
    fd = os.open(basis_path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or _is_link_or_reparse(st):
            raise OSError("bản nền không phải file thường")
        if sys.platform != "win32":
            os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_exact_at(fd: int, offset: int, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    pos = offset
    while remaining > 0:
        if sys.platform == "win32":
            os.lseek(fd, pos, os.SEEK_SET)
            piece = os.read(fd, remaining)
        else:
            piece = os.pread(fd, remaining, pos)
        if not piece:
            break
        chunks.append(piece)
        pos += len(piece)
        remaining -= len(piece)
    return b"".join(chunks)


def hash_basis_blocks(
    basis_path: Path,
    indices: Sequence[int],
    offer_size: int,
    stop: threading.Event,
) -> list[tuple[int, str]]:
    """Băm các khối `indices` của bản nền `basis_path`; dừng sớm nếu `stop` được đặt."""
    if not indices or stop.is_set() or not is_safe_regular_file(basis_path):
        return []
    fd = _open_basis_fd(basis_path)
    try:
        basis_size = os.fstat(fd).st_size
        result: list[tuple[int, str]] = []
        for idx in indices:
            if stop.is_set():
                return []
            blen = block_length(offer_size, idx)
            offset = idx * BLOCK_SIZE
            if offset + blen > basis_size:
                continue
            data = _read_exact_at(fd, offset, blen)
            if len(data) != blen:
                continue
            result.append((idx, hashlib.sha256(data).hexdigest()))
        return result
    finally:
        os.close(fd)


def seed_part_from_basis(
    part: PartFile,
    basis_path: Path,
    offer_size: int,
    matched_hex: Mapping[int, str],
    stop: threading.Event,
) -> tuple[list[tuple[int, bytes]], int]:
    """Chép tuần tự các khối khớp từ `basis_path` vào `.part` (điền 0 cho khe hở) và `MARK` bền.

    Trả về `(committed, part_blocks)` trong đó `committed` chỉ gồm các khối thực sự khớp hash
    đã được ghi `MARK`, còn `part_blocks` là số khối đã mở rộng trong `.part` (`highest + 1`).
    """
    if not matched_hex or stop.is_set() or not is_safe_regular_file(basis_path):
        return ([], 0)
    fd = _open_basis_fd(basis_path)
    try:
        basis_size = os.fstat(fd).st_size
        valid_matched = {
            idx: hex_digest
            for idx, hex_digest in matched_hex.items()
            if idx * BLOCK_SIZE + block_length(offer_size, idx) <= basis_size
        }
        if not valid_matched:
            return ([], 0)
        highest = max(valid_matched)
        committed: list[tuple[int, bytes]] = []
        zero_block = b"\x00" * BLOCK_SIZE
        for idx in range(highest + 1):
            if stop.is_set():
                raise RereadStopped("hủy chuẩn bị delta")
            blen = block_length(offer_size, idx)
            expected_hex = valid_matched.get(idx)
            if expected_hex is not None:
                data = _read_exact_at(fd, idx * BLOCK_SIZE, blen)
                if len(data) == blen:
                    digest = hashlib.sha256(data).digest()
                    if digest.hex() == expected_hex:
                        part.write_at(idx, data)
                        committed.append((idx, digest))
                        continue
            part.write_at(idx, zero_block[:blen])
        part.commit(committed)
        return (committed, highest + 1)
    finally:
        os.close(fd)


__all__ = [
    "DELTA_INDEX_FILE",
    "DeltaIndexStore",
    "check_source_blocks",
    "find_basis_file",
    "hash_basis_blocks",
    "is_safe_regular_file",
    "seed_part_from_basis",
    "sources",
]
