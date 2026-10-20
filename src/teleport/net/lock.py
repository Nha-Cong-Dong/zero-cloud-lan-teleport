"""File khóa `<config_dir>/node.lock`: mỗi thư mục cấu hình chỉ một node chạy.

Dùng khóa của hệ điều hành chứ không dựa vào việc file có tồn tại hay không: khi
tiến trình chết (kể cả bị kill), HĐH tự nhả khóa, nên không bao giờ còn "khóa
treo" phải xóa tay. File vì thế được giữ lại sau `release()`.

- Windows: `msvcrt.locking` khóa byte 0 của file (LK_NBLCK: không chờ).
- POSIX: `fcntl.flock(LOCK_EX | LOCK_NB)`. Khóa `flock` gắn với từng lần mở file
  (không như `lockf`), nên hai `NodeLock` trong cùng tiến trình cũng loại trừ nhau.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from types import TracebackType

if os.name == "nt":
    import msvcrt
else:
    import fcntl

LOCK_FILE = "node.lock"

# Mã lỗi khi byte đã bị khóa ở nơi khác (Windows trả EACCES với LK_NBLCK; một số
# bản CRT trả EDEADLOCK).
_WINDOWS_CONTENDED = frozenset({errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)})


class NodeLockedError(Exception):
    """Đã có một node Teleport khác giữ khóa của thư mục cấu hình này."""


class NodeLock:
    """Khóa không chờ trên `<config_dir>/node.lock`. Dùng với `with` hoặc `acquire`/`release`."""

    def __init__(self, config_dir: Path) -> None:
        self.path = config_dir / LOCK_FILE
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Lấy khóa ngay (không chờ); tạo `config_dir` nếu chưa có.

        Node khác đang giữ → NodeLockedError. Object này đang giữ → RuntimeError.
        """
        if self._fd is not None:
            raise RuntimeError("NodeLock này đã giữ khóa")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        try:
            _lock(fd)
        except BaseException as e:
            os.close(fd)
            if _is_contended(e):
                raise NodeLockedError(
                    f"Teleport đang chạy với thư mục cấu hình này ({self.path.parent})"
                ) from e
            raise  # lỗi khác (ổ đĩa, quyền…) giữ nguyên OSError
        self._fd = fd

    def release(self) -> None:
        """Nhả khóa. Gọi nhiều lần không sao; file khóa được giữ lại."""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            _unlock(fd)
        except OSError:
            pass  # đóng fd bên dưới cũng nhả khóa
        finally:
            os.close(fd)

    def __enter__(self) -> NodeLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def _is_contended(e: BaseException) -> bool:
    """Lỗi do khóa đang bị giữ ở nơi khác (khác với lỗi I/O thật)."""
    if os.name == "nt":
        return isinstance(e, OSError) and e.errno in _WINDOWS_CONTENDED
    return isinstance(e, BlockingIOError)


if os.name == "nt":

    def _lock(fd: int) -> None:
        # msvcrt.locking khóa tính từ vị trí con trỏ file hiện tại: luôn về byte 0.
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
