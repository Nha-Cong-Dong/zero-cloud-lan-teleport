"""Helper dùng chung cho test, import bằng `from support import …` (`pythonpath` trong `pyproject.toml`).

Fixture vẫn ở các `conftest.py`; file này chỉ chứa hàm thường mà nhiều file test cần gọi trực tiếp.
"""

import asyncio
import time
from collections.abc import Callable


async def wait_until(
    predicate: Callable[[], bool], timeout: float = 5.0, what: str = "điều kiện", *, poll: float = 0.01
) -> None:
    """Chờ tới khi `predicate()` đúng, hỏi lại mỗi `poll` giây; quá `timeout` giây thì `AssertionError`."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(poll)
