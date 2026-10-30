"""Ghi lỗi vào log mà không ghi nội dung lỗi (mvp N9): chỉ tên loại lỗi và traceback.

`str(e)` có thể chứa đường dẫn, tên file người dùng hay dữ liệu máy khác gửi tới, nên mọi thứ ở
đây chỉ ghi tên loại lỗi (có module nếu không phải lỗi dựng sẵn) và `traceback.format_tb` (đường
dẫn mã nguồn, số dòng, dòng mã), kể cả các lỗi trong chuỗi `__cause__` / `__context__`.

- `install_excepthooks`: `sys.excepthook` (gồm lỗi trong slot Qt, PySide6 chuyển về đây) và
  `threading.excepthook` ghi một dòng ERROR. `KeyboardInterrupt` để hook cũ xử lý; `SystemExit`
  trong thread bỏ qua như mặc định. Trả hàm khôi phục hook cũ. Hàm nào gỡ hook trên đường lỗi lan
  ra (như `ui.app.main`) thì tự gọi `log_uncaught` trước khi gỡ.
- `loop_exception_handler`: handler lỗi của loop asyncio (thay handler mặc định, vốn ghi
  `repr` của task, callback và lỗi).
- `RedactingFormatter`: formatter cho file log; `log.exception(...)` và `exc_info` ở mọi nơi chỉ
  ra traceback không có nội dung lỗi. Không dùng `exc_text` đã lưu sẵn trên bản ghi (handler khác
  có thể đã định dạng đủ nội dung vào đó).
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import sys
import threading
import traceback
from collections.abc import Callable
from types import TracebackType
from typing import Any

log = logging.getLogger(__name__)

_MAX_CHAIN = 8  # số lỗi tối đa trong chuỗi `__cause__` / `__context__` được ghi


def error_name(exc_type: type[BaseException]) -> str:
    """Tên loại lỗi: `ValueError`, `ssl.SSLError`, `teleport.app.node.ConfigDirError`."""
    if exc_type.__module__ == builtins.__name__:
        return exc_type.__qualname__
    return f"{exc_type.__module__}.{exc_type.__qualname__}"


def redacted_traceback(
    exc_type: type[BaseException], exc: BaseException | None, tb: TracebackType | None
) -> str:
    """Traceback như Python in ra nhưng mỗi lỗi chỉ còn tên loại, không có `str(e)`."""
    chain: list[tuple[type[BaseException], TracebackType | None, str]] = [(exc_type, tb, "")]
    seen = {id(exc)}
    current = exc
    while current is not None and len(chain) < _MAX_CHAIN:
        if current.__cause__ is not None:
            current, link = current.__cause__, "Lỗi trên là nguyên nhân trực tiếp của lỗi sau:"
        elif current.__context__ is not None and not current.__suppress_context__:
            current, link = current.__context__, "Trong lúc xử lý lỗi trên thì lỗi sau xảy ra:"
        else:
            break
        if id(current) in seen:
            break
        seen.add(id(current))
        chain.append((type(current), current.__traceback__, link))
    parts: list[str] = []
    for kind, frames, link in reversed(chain):
        block = "Traceback (most recent call last):\n" + "".join(traceback.format_tb(frames))
        parts.append(block + error_name(kind))
        if link:
            parts.append(link)
    return "\n\n".join(parts)


class RedactingFormatter(logging.Formatter):
    """Formatter chỉ ghi tên loại lỗi và traceback cho `exc_info`, không ghi nội dung lỗi."""

    def format(self, record: logging.LogRecord) -> str:
        cached = record.exc_text
        record.exc_text = None  # bỏ bản đã định dạng đủ của handler khác
        try:
            return super().format(record)
        finally:
            record.exc_text = cached

    def formatException(self, ei: Any) -> str:
        exc_type, exc, tb = ei
        if exc_type is None:
            return ""
        return redacted_traceback(exc_type, exc, tb)


def log_uncaught(exc_type: type[BaseException], exc: BaseException | None, tb: TracebackType | None) -> None:
    """Ghi một dòng ERROR cho lỗi không bắt được ở thread chính (tên loại + traceback)."""
    log.error("lỗi không bắt được: %s\n%s", error_name(exc_type), redacted_traceback(exc_type, exc, tb))


def install_excepthooks() -> Callable[[], None]:
    """Cài `sys.excepthook` và `threading.excepthook` ghi lỗi không bắt được; trả hàm khôi phục."""
    previous_sys = sys.excepthook
    previous_thread = threading.excepthook

    def sys_hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            previous_sys(exc_type, exc, tb)
            return
        log_uncaught(exc_type, exc, tb)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread is not None else "?"
        log.error(
            "lỗi không bắt được trong thread %s: %s\n%s",
            name,
            error_name(args.exc_type),
            redacted_traceback(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook

    def restore() -> None:
        if sys.excepthook is sys_hook:
            sys.excepthook = previous_sys
        if threading.excepthook is thread_hook:
            threading.excepthook = previous_thread

    return restore


def loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Handler lỗi của loop asyncio: không ghi `message` hay `repr` trong `context` (có thể chứa
    tham số của callback, đường dẫn), chỉ ghi tên loại lỗi và traceback."""
    exc = context.get("exception")
    if isinstance(exc, BaseException):
        log.error(
            "lỗi không bắt được trong loop asyncio: %s\n%s",
            error_name(type(exc)),
            redacted_traceback(type(exc), exc, exc.__traceback__),
        )
    else:
        log.error("loop asyncio báo lỗi không kèm ngoại lệ (khóa: %s)", ", ".join(sorted(context)))
