"""Executor cho I/O đĩa của truyền file: số thread cố định, hạn tính từ lúc thread bắt đầu việc.

Luật (docs/architecture.md mục 5): mọi thao tác chạm hệ file của truyền file (`app.storage`,
`app.sources`) đi qua một `DiskExecutor`; việc chỉ tốn CPU (SHA-256 payload nhận, `merkle_root`)
dùng `asyncio.to_thread`. Khác `asyncio.to_thread`:

- Không quá `threads` việc chạy cùng lúc; việc thừa xếp hàng FIFO.
- `DiskJob.result(timeout)` đếm hạn từ lúc **thread bắt đầu** việc, không tính lúc xếp hàng. Quá
  hạn → `DiskTimeout`, nhưng việc vẫn chạy tới cùng trong thread (Python không dừng được một
  thread đang kẹt trong I/O) và vẫn tính vào `busy`.
- `DiskJob.settled()` chờ việc thật sự xong (kể cả việc đã quá hạn hay đã bị hủy `await`): bên
  gọi dùng nó trước khi đóng fd mà việc đó đang dùng (đóng fd giữa lúc ghi có thể làm thread ghi
  vào một fd khác vừa được cấp lại cùng số, xem docstring `app/storage.py`), và để dọn kết quả
  tới muộn.
- Hủy task đang `await` không dừng việc đã bắt đầu; việc chưa bắt đầu thì `run` bỏ nó.
- `busy` và `wait_idle` đếm phía loop: một việc tính là bận từ lúc thread nhận nó tới khi loop
  đã giao kết quả (`done` True, các bên chờ `settled()` đã được đánh thức). Dù vậy bên gọi (P2-6b)
  vẫn phải `await settled()` của mọi việc mình giữ và chạy xong việc dọn của nó trước
  `shutdown()`: `wait_idle` chỉ nói thread đã rảnh, không nói việc dọn phía loop đã xong.

Mọi phương thức gọi trên event loop (thread của loop). Thread là daemon, nhận việc qua
`queue.SimpleQueue`, trả kết quả qua `loop.call_soon_threadsafe`; loop đã đóng thì kết quả bị bỏ
(thread không ném) và việc đó không bao giờ thôi tính vào `busy`. `shutdown()` không chờ thread:
việc đang chạy chạy tiếp, việc chưa chạy bị bỏ.

Bẫy: việc treo mãi (ổ mạng mất) giữ thread của nó mãi; `threads` việc như thế làm executor ngừng
nhận việc mới. Bên gọi đặt hạn, và dùng `busy` / `wait_idle` để biết còn thread nào bận.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import queue
import threading
import time
from collections.abc import Callable
from enum import Enum
from typing import Any

DISK_THREADS = 4  # architecture §5: số thread I/O đĩa của truyền file


class DiskTimeout(Exception):
    """Việc đĩa chạy quá hạn (tính từ lúc thread bắt đầu); việc vẫn chạy tiếp trong thread."""


class _State(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"
    CANCELLED = "cancelled"  # bỏ trước khi chạy


def _check_timeout(timeout: object) -> float | None:
    if timeout is None:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise TypeError("timeout phải là None hoặc số giây")
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout phải là số hữu hạn ≥ 0")
    return float(timeout)


def _post(loop: asyncio.AbstractEventLoop, callback: Callable[..., object], *args: object) -> None:
    """Giao `callback` cho loop từ thread khác; loop đã đóng thì bỏ."""
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(callback, *args)


class DiskJob[T]:
    """Một việc đã giao cho `DiskExecutor`. Chỉ `DiskExecutor.submit` tạo.

    Trạng thái nhìn từ thread (`started`, `cancel`) đổi dưới khóa của executor; trạng thái nhìn từ
    loop (`done`, `result`, `settled`) đổi khi loop nhận thông báo của thread.
    """

    def __init__(
        self,
        executor: DiskExecutor,
        loop: asyncio.AbstractEventLoop,
        fn: Callable[..., T],
        args: tuple[object, ...],
    ) -> None:
        self._executor = executor
        self._loop = loop
        self._fn: Callable[..., T] | None = fn  # None sau khi chạy hay bị bỏ: nhả tham chiếu
        self._args = args
        self._state = _State.QUEUED  # đổi dưới executor._lock
        self._start_mono = 0.0  # time.monotonic() lúc thread bắt đầu việc
        self._started_event = asyncio.Event()  # loop: thread đã bắt đầu, hoặc việc bị bỏ
        self._settled_event = asyncio.Event()  # loop: thread đã xong, hoặc việc bị bỏ
        self._ok = False
        self._value: Any = None  # kết quả, hoặc ngoại lệ khi `_ok` False

    @property
    def started(self) -> bool:
        """Thread đã nhận việc (đang chạy hoặc đã xong)."""
        return self._state in (_State.RUNNING, _State.FINISHED)

    @property
    def done(self) -> bool:
        """Thread đã chạy xong việc (loop đã nhận kết quả), hoặc việc bị bỏ trước khi chạy."""
        return self._settled_event.is_set()

    def cancel(self) -> bool:
        """Bỏ việc nếu chưa bắt đầu (không chạy, không tính `busy`) → True; đã bắt đầu → False."""
        executor = self._executor
        with executor._lock:
            if self._state is _State.CANCELLED:
                return True
            if self._state is not _State.QUEUED:
                return False
            self._state = _State.CANCELLED
            executor._queued.discard(self)
            waiters = executor._take_idle_waiters()
        self._mark_cancelled()
        executor._wake(waiters)
        return True

    async def result(self, timeout: float | None) -> T:
        """Chờ việc bắt đầu (không hạn), rồi chờ xong tối đa `timeout` giây tính từ lúc thread bắt đầu.

        Quá hạn → `DiskTimeout` (việc vẫn chạy). Ngoại lệ của việc bay ra nguyên trạng. Việc bị bỏ
        → `asyncio.CancelledError`. Gọi lại sau khi xong → đúng kết quả/ngoại lệ đó. Hủy task
        đang chờ không hủy việc. `timeout` sai → TypeError / ValueError.
        """
        limit = _check_timeout(timeout)
        await self._started_event.wait()
        if not self._settled_event.is_set() and limit is not None:
            remaining = limit - (time.monotonic() - self._start_mono)
            try:
                async with asyncio.timeout(max(remaining, 0.0)):
                    await self._settled_event.wait()
            except TimeoutError:
                if self._state is not _State.FINISHED:
                    raise DiskTimeout(f"thao tác đĩa quá {limit:g} s") from None
                # Thread vừa xong đúng lúc hết hạn: kết quả đang trên đường tới loop.
        await self._settled_event.wait()
        if self._state is _State.CANCELLED:
            raise asyncio.CancelledError()
        if not self._ok:
            raise self._value
        result: T = self._value
        return result

    async def settled(self) -> None:
        """Chờ tới khi `done`; không bao giờ ném lỗi của việc (kể cả khi việc ném hay bị bỏ)."""
        await self._settled_event.wait()

    # ---- Nội bộ --------------------------------------------------------------------------

    def _mark_cancelled(self) -> None:
        """(loop) Việc bị bỏ trước khi chạy: đánh thức mọi bên đang chờ."""
        self._fn = None
        self._args = ()
        self._started_event.set()
        self._settled_event.set()

    def _on_started(self) -> None:
        self._started_event.set()

    def _on_settled(self, ok: bool, value: object) -> None:
        """(loop) Giao kết quả, đánh thức các bên chờ `settled()`, rồi mới thôi tính `busy`."""
        self._ok = ok
        self._value = value
        self._started_event.set()
        self._settled_event.set()
        executor = self._executor
        with executor._lock:
            executor._busy -= 1
            waiters = executor._take_idle_waiters()
        executor._wake(waiters)

    def _run(self) -> None:
        """(thread) Chạy việc nếu chưa bị bỏ, rồi báo kết quả cho loop."""
        executor = self._executor
        with executor._lock:
            if self._state is not _State.QUEUED:
                return
            self._start_mono = time.monotonic()
            self._state = _State.RUNNING
            executor._queued.discard(self)
            executor._busy += 1
        _post(self._loop, self._on_started)
        fn, args = self._fn, self._args
        self._fn, self._args = None, ()
        assert fn is not None
        try:
            value: object = fn(*args)
            ok = True
        except BaseException as e:  # thread không được chết vì việc: mọi lỗi giao về loop
            value, ok = e, False
        del fn, args
        with executor._lock:
            self._state = _State.FINISHED  # `busy` chỉ giảm khi loop đã nhận kết quả (`_on_settled`)
        _post(self._loop, self._on_settled, ok, value)


class DiskExecutor:
    """Tối đa `threads` thread daemon (tạo dần khi cần) chạy việc đĩa; xem docstring module."""

    def __init__(self, threads: int = DISK_THREADS, *, name: str = "teleport.disk") -> None:
        """`threads` không phải số nguyên ≥ 1 → ValueError; `name` (tiền tố tên thread) sai → TypeError."""
        if type(threads) is not int or threads < 1:
            raise ValueError("threads phải là số nguyên ≥ 1")
        if not isinstance(name, str):
            raise TypeError("name phải là chuỗi")
        self._max_threads = threads
        self._name = name
        self._lock = threading.Lock()
        self._queue: queue.SimpleQueue[DiskJob[Any] | None] = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self._idle_tokens = 0  # số thread sắp rảnh (vừa xong một việc), như concurrent.futures
        self._queued: set[DiskJob[Any]] = set()  # việc đang xếp hàng (chưa bắt đầu, chưa bị bỏ)
        self._busy = 0
        self._idle_waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
        self._shutdown = False

    @property
    def busy(self) -> int:
        """Số việc thread đã nhận mà loop chưa nhận kết quả (kể cả việc đã quá hạn).

        Không tính việc đang xếp hàng. `busy == 0` ⇒ mọi việc đã giao đều `done` và các bên chờ
        `settled()` của chúng đã được đánh thức.
        """
        with self._lock:
            return self._busy

    def submit[R](self, fn: Callable[..., R], /, *args: object) -> DiskJob[R]:
        """Giao `fn(*args)` cho một thread (gọi trên loop). Sau `shutdown()` → RuntimeError."""
        loop = asyncio.get_running_loop()
        job = DiskJob(self, loop, fn, args)
        with self._lock:
            if self._shutdown:
                raise RuntimeError("DiskExecutor đã shutdown")
            self._queued.add(job)
            if self._idle_tokens:
                self._idle_tokens -= 1
            elif len(self._threads) < self._max_threads:
                thread = threading.Thread(
                    target=self._worker, name=f"{self._name}-{len(self._threads)}", daemon=True
                )
                thread.start()
                self._threads.append(thread)
            self._queue.put(job)
        return job

    async def run[R](self, fn: Callable[..., R], /, *args: object, timeout: float | None) -> R:
        """`submit` rồi `result(timeout)`. Task bị hủy khi việc chưa bắt đầu → bỏ việc."""
        _check_timeout(timeout)
        job = self.submit(fn, *args)
        try:
            return await job.result(timeout)
        except asyncio.CancelledError:
            job.cancel()
            raise

    async def wait_idle(self, timeout: float) -> bool:
        """Chờ tới khi không còn việc chạy hay xếp hàng, tối đa `timeout` giây; True nếu đạt."""
        limit = _check_timeout(timeout)
        if limit is None:
            raise TypeError("timeout phải là số giây")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit
        while True:
            event = asyncio.Event()
            with self._lock:
                if self._is_idle():
                    return True
                waiter = (loop, event)
                self._idle_waiters.append(waiter)
            try:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    async with asyncio.timeout(remaining):
                        await event.wait()
                except TimeoutError:
                    break
            finally:
                with self._lock:
                    if waiter in self._idle_waiters:
                        self._idle_waiters.remove(waiter)
        with self._lock:
            return self._is_idle()

    def shutdown(self) -> None:
        """Không nhận việc mới; việc chưa chạy bị bỏ (`result` → CancelledError). Không chờ thread."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            dropped = list(self._queued)
            for job in dropped:
                job._state = _State.CANCELLED
            self._queued.clear()
            waiters = self._take_idle_waiters()
            for _ in self._threads:
                self._queue.put(None)  # mỗi thread thoát sau việc đang chạy
        for job in dropped:
            job._mark_cancelled()
        self._wake(waiters)

    # ---- Nội bộ --------------------------------------------------------------------------

    def _is_idle(self) -> bool:
        return self._busy == 0 and not self._queued

    def _take_idle_waiters(self) -> list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]:
        """(giữ `_lock`) Rảnh hẳn thì lấy hết các bên đang chờ `wait_idle`."""
        if not self._is_idle() or not self._idle_waiters:
            return []
        waiters, self._idle_waiters = self._idle_waiters, []
        return waiters

    @staticmethod
    def _wake(waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]]) -> None:
        for loop, event in waiters:
            _post(loop, event.set)

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                job._run()
            finally:
                with self._lock:
                    self._idle_tokens += 1
