"""Khóa file của hệ điều hành trong thư mục cấu hình; tiện ích dùng chung như `paths`.

`NodeLock(config_dir)` giữ `<config_dir>/node.lock`: mỗi thư mục cấu hình chỉ một
node chạy. Tên khác (ví dụ `identity.lock` của `teleport.trust.identity`) cho các
việc cần loại trừ nhau ngắn hạn giữa nhiều tiến trình.

Dùng khóa của hệ điều hành chứ không dựa vào việc file có tồn tại hay không: khi
tiến trình chết (kể cả bị kill), HĐH tự nhả khóa, nên không bao giờ còn "khóa
treo" phải xóa tay. File vì thế được giữ lại sau `release()`. Trên Windows, khóa
của tiến trình bị kill được nhả **không đồng bộ** (tài liệu `LockFileEx`), nên ngay
sau đó có thể vẫn thấy bị khóa trong chốc lát.

- Windows: `msvcrt.locking` khóa byte 0 của file (LK_NBLCK: không chờ).
- POSIX: `fcntl.flock(LOCK_EX | LOCK_NB)`. Khóa `flock` gắn với từng lần mở file
  (không như `lockf`), nên hai `NodeLock` trong cùng tiến trình cũng loại trừ nhau.
"""

from __future__ import annotations

import errno
import os
import sys
import time
from pathlib import Path
from types import TracebackType

# sys.platform thay vì os.name: mypy chỉ hiểu phép so sánh sys.platform để bỏ qua nhánh của
# hệ điều hành khác (msvcrt/fcntl chỉ có trên một phía).
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

LOCK_FILE = "node.lock"
_RETRY_DELAY = 0.05  # giây giữa hai lần thử khi acquire có timeout

# Mã lỗi khi byte đã bị khóa ở nơi khác (Windows trả EACCES với LK_NBLCK; một số
# bản CRT trả EDEADLOCK).
_WINDOWS_CONTENDED = frozenset({errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)})


class NodeLockedError(Exception):
    """Đã có một node Teleport khác giữ khóa của thư mục cấu hình này."""


class NodeLock:
    """Khóa không chờ trên `<config_dir>/<name>`. Dùng với `with` hoặc `acquire`/`release`."""

    def __init__(self, config_dir: Path, name: str = LOCK_FILE) -> None:
        self.path = config_dir / name
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, timeout: float = 0.0) -> None:
        """Lấy khóa; tạo `config_dir` nếu chưa có.

        `timeout=0` (mặc định): không chờ. `timeout > 0`: thử lại mỗi 50 ms tới hết hạn, dùng
        khi bên giữ khóa chỉ giữ ngắn, hoặc khóa của tiến trình vừa chết chưa kịp được nhả
        (Windows nhả không đồng bộ). Hết hạn mà vẫn bị giữ → NodeLockedError. Object này
        đang giữ → RuntimeError.
        """
        if self._fd is not None:
            raise RuntimeError("NodeLock này đã giữ khóa")
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._fd = self._try_lock()
                return
            except NodeLockedError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_RETRY_DELAY)

    def _try_lock(self) -> int:
        """Một lần thử: trả về fd đang giữ khóa; bị giữ ở nơi khác → NodeLockedError."""
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
        return fd

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
    if sys.platform == "win32":
        return isinstance(e, OSError) and e.errno in _WINDOWS_CONTENDED
    return isinstance(e, BlockingIOError)


if sys.platform == "win32":

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
