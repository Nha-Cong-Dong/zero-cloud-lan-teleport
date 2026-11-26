"""Thực thi phân loại tệp nhận vào thư mục đích an toàn (Smart Receive Routing).

Chuyển tệp đã kiểm tra toàn vẹn từ thư mục nhận mặc định sang thư mục đích của quy tắc:
- Không bao giờ ghi đè tệp có sẵn (giữ chỗ bằng `O_CREAT | O_EXCL`, trùng tên thêm `(1)`, `(2)`).
- Chống thoát thư mục (`sanitize_rel_path` + `is_relative_to`) và không đi theo symlink bên trong.
- Cùng phân vùng dùng `os.replace` nguyên tử; khác phân vùng sao chép luồng 64 KiB + `fsync` + `os.replace`.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from pathlib import Path

from teleport.core.filenames import numbered, sanitize_filename, sanitize_rel_path

STREAM_COPY_CHUNK = 64 * 1024
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_RESERVE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW | _O_CLOEXEC
_TMP_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW | _O_CLOEXEC
_MAX_COLLISIONS = 10_000


def _ensure_dir_no_symlink(base: Path, parts: tuple[str, ...]) -> Path:
    """Tạo từng cấp thư mục con trong `base`, từ chối nếu bất kỳ cấp nào là symlink."""
    base.mkdir(parents=True, exist_ok=True)
    if base.is_symlink() or not base.is_dir():
        raise OSError(errno.ENOTDIR, "thư mục đích không hợp lệ")
    cur = base
    for part in parts:
        cur = cur / part
        if cur.is_symlink():
            raise OSError(errno.ELOOP, "thư mục đích chứa liên kết")
        if cur.exists():
            if not cur.is_dir():
                raise OSError(errno.ENOTDIR, "đường dẫn đích trùng tên tệp")
        else:
            cur.mkdir(exist_ok=True)
    return cur


def _reserve_unique(parent: Path, filename: str) -> Path:
    """Giữ chỗ một tên tệp duy nhất bằng `O_CREAT | O_EXCL` để không ghi đè tệp có sẵn."""
    for idx in range(_MAX_COLLISIONS):
        candidate_name = filename if idx == 0 else numbered(filename, idx)
        candidate = parent / candidate_name
        try:
            fd = os.open(candidate, _RESERVE_FLAGS, 0o600)
        except FileExistsError:
            continue
        except PermissionError as e:
            if candidate.exists() or candidate.is_dir():
                continue
            raise e
        os.close(fd)
        return candidate
    raise OSError(errno.EEXIST, "không tìm được tên đích chưa tồn tại")


def _apply_mtime(path: Path, mtime_ms: int) -> None:
    if mtime_ms <= 0:
        return
    ns = mtime_ms * 1_000_000
    with contextlib.suppress(OSError):
        os.utime(path, ns=(ns, ns))


def _stream_copy_replace(src_path: Path, target: Path, mtime_ms: int) -> None:
    """Sao chép theo luồng 64 KiB sang tệp tạm cạnh `target`, `fsync` rồi `os.replace`."""
    tmp_path = target.parent / f".{target.name}.{secrets.token_hex(6)}.tmp"
    src_fd = os.open(src_path, os.O_RDONLY | _O_BINARY | _O_CLOEXEC)
    dst_fd: int | None = None
    try:
        st = os.fstat(src_fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "tệp nguồn không phải file thường")
        dst_fd = os.open(tmp_path, _TMP_WRITE_FLAGS, 0o600)
        while True:
            chunk = os.read(src_fd, STREAM_COPY_CHUNK)
            if not chunk:
                break
            view = memoryview(chunk)
            written = 0
            while written < len(chunk):
                n = os.write(dst_fd, view[written:])
                if n <= 0:
                    raise OSError(errno.EIO, "ghi không tiến được")
                written += n
        os.fsync(dst_fd)
        os.close(dst_fd)
        dst_fd = None
        os.close(src_fd)
        src_fd = -1
        os.replace(tmp_path, target)
        _apply_mtime(target, mtime_ms)
    except BaseException:
        if dst_fd is not None:
            with contextlib.suppress(OSError):
                os.close(dst_fd)
        if src_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(src_fd)
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def deliver_to_routed_dir(
    src_path: Path,
    dest_dir: Path,
    *,
    name: str,
    rel_path: str,
    mtime_ms: int,
    is_dir: bool,
) -> Path:
    """Chuyển an toàn `src_path` đã nhận xong sang thư mục đích `dest_dir`.

    Không bao giờ ghi đè tệp hiện có và giữ nguyên `src_path` nếu chuyển thất bại.
    """
    if rel_path:
        clean_rel = sanitize_rel_path(rel_path)
        parts = tuple(p for p in clean_rel.split("/") if p)
    else:
        parts = (sanitize_filename(name),)
    if not parts:
        parts = (sanitize_filename(name),)

    if is_dir:
        target_dir = _ensure_dir_no_symlink(dest_dir, parts)
        if not target_dir.resolve().is_relative_to(dest_dir.resolve()):
            raise OSError(errno.EACCES, "đường dẫn đích vượt ra ngoài thư mục quy tắc")
        with contextlib.suppress(OSError):
            src_path.rmdir()
        return target_dir

    target_parent = _ensure_dir_no_symlink(dest_dir, parts[:-1])
    leaf_name = sanitize_filename(parts[-1])
    base_resolved = dest_dir.resolve()
    parent_resolved = target_parent.resolve()
    if not parent_resolved.is_relative_to(base_resolved):
        raise OSError(errno.EACCES, "đường dẫn đích vượt ra ngoài thư mục quy tắc")

    target = _reserve_unique(target_parent, leaf_name)
    try:
        try:
            os.replace(src_path, target)
            _apply_mtime(target, mtime_ms)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            _stream_copy_replace(src_path, target, mtime_ms)
            src_path.unlink()
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink()
        raise
    return target


__all__ = ["STREAM_COPY_CHUNK", "deliver_to_routed_dir"]
