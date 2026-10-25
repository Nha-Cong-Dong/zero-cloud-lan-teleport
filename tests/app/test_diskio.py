"""Test `teleport.app.diskio`: executor đĩa có hạn chờ (P2-6a: D1).

Nguồn: `specs/spec-p2-6.md` mục A1 (API chốt `DiskExecutor` / `DiskJob`), `specs/plan-p2-6.md`
mục 2.5 và tiêu chí D1 mục 5. Viết từ đặc tả, không từ code. Không mở socket.

Mọi việc “treo” trong test chờ một `threading.Event`; khối `finally` của test luôn nhả nó, để không
còn thread treo sót sau test (spec A6).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from teleport.app import diskio
from teleport.app.diskio import DiskExecutor, DiskJob, DiskTimeout

SLACK = 2.0  # biên nới cho CI


class Gate:
    """Việc treo tới khi `release()`; ghi lại lúc bắt đầu/kết thúc và số việc chạy cùng lúc."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.started: list[str] = []
        self.finished: list[str] = []
        self.threads: list[threading.Thread] = []

    def job(self, label: str = "x", result: object = None) -> Callable[[], object]:
        def fn() -> object:
            with self.lock:
                self.running += 1
                self.max_running = max(self.max_running, self.running)
                self.started.append(label)
                self.threads.append(threading.current_thread())
            try:
                assert self.event.wait(30), "Gate không được nhả"
            finally:
                with self.lock:
                    self.running -= 1
                    self.finished.append(label)
            return result if result is not None else label

        return fn

    def release(self) -> None:
        self.event.set()


@pytest.fixture
def gate() -> Iterator[Gate]:
    g = Gate()
    try:
        yield g
    finally:
        g.release()


@pytest.fixture
def make_executor() -> Iterator[Callable[..., DiskExecutor]]:
    created: list[DiskExecutor] = []

    def make(*args, **kwargs) -> DiskExecutor:
        ex = DiskExecutor(*args, **kwargs)
        created.append(ex)
        return ex

    yield make
    for ex in created:
        with contextlib.suppress(Exception):
            ex.shutdown()


async def wait_until(predicate: Callable[[], bool], timeout: float = SLACK, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.005)


# =================================================================================================
# Hằng, kết quả, ngoại lệ
# =================================================================================================


def test_constants_match_spec():
    assert diskio.DISK_THREADS == 4
    assert issubclass(DiskTimeout, Exception)


async def test_d1_run_returns_result_with_positional_args(make_executor):
    ex = make_executor()
    assert await ex.run(divmod, 17, 5, timeout=5) == (3, 2)
    assert await ex.run(lambda: "xong", timeout=None) == "xong"


async def test_d1_runs_in_a_daemon_thread_off_the_loop(make_executor):
    ex = make_executor()
    loop_thread = threading.current_thread()
    t = await ex.run(threading.current_thread, timeout=5)
    assert t is not loop_thread
    assert t.daemon is True


async def test_d1_thread_name_uses_executor_name(make_executor):
    ex = make_executor(2, name="teleport.disk.test")
    t = await ex.run(threading.current_thread, timeout=5)
    assert t.name.startswith("teleport.disk.test"), t.name


async def test_d1_exception_propagates_unchanged(make_executor):
    ex = make_executor()
    err = OSError(28, "giả lập hết chỗ")

    def boom() -> None:
        raise err

    with pytest.raises(OSError) as info:
        await ex.run(boom, timeout=5)
    assert info.value is err
    assert not isinstance(info.value, DiskTimeout)


# =================================================================================================
# Quá hạn, busy, wait_idle
# =================================================================================================


async def test_d1_timeout_raises_disk_timeout_and_thread_keeps_running(make_executor, gate):
    ex = make_executor()
    t0 = time.monotonic()
    with pytest.raises(DiskTimeout):
        await ex.run(gate.job("treo"), timeout=0.2)
    assert 0.15 <= time.monotonic() - t0 <= 0.2 + SLACK
    assert gate.started == ["treo"] and gate.finished == []
    assert ex.busy == 1, "việc quá hạn vẫn chạy trong thread và vẫn tính vào busy"
    assert await ex.wait_idle(0.1) is False
    gate.release()
    assert await ex.wait_idle(SLACK) is True
    assert ex.busy == 0
    assert gate.finished == ["treo"]


async def test_d1_wait_idle_true_immediately_when_idle(make_executor):
    ex = make_executor()
    assert ex.busy == 0
    t0 = time.monotonic()
    assert await ex.wait_idle(5) is True
    assert time.monotonic() - t0 < 1.0


async def test_d1_wait_idle_waits_for_queued_jobs_too(make_executor, gate):
    ex = make_executor(1)
    first = ex.submit(gate.job("a"))
    second = ex.submit(gate.job("b"))
    await wait_until(lambda: first.started, what="việc đầu chưa chạy")
    assert ex.busy == 1, "busy không tính việc đang xếp hàng"
    assert second.started is False
    assert await ex.wait_idle(0.1) is False
    gate.release()
    assert await ex.wait_idle(SLACK) is True
    assert first.done and second.done
    assert gate.finished == ["a", "b"]


async def test_d1_never_more_than_threads_jobs_at_once(make_executor, gate):
    ex = make_executor(2)
    jobs = [ex.submit(gate.job(str(i))) for i in range(5)]
    await wait_until(lambda: len(gate.started) == 2, what="chưa đủ 2 việc chạy")
    await asyncio.sleep(0.2)
    assert len(gate.started) == 2 and gate.max_running == 2
    assert ex.busy == 2
    assert sum(j.started for j in jobs) == 2
    gate.release()
    assert await ex.wait_idle(SLACK) is True
    assert sorted(await asyncio.gather(*(j.result(5) for j in jobs))) == ["0", "1", "2", "3", "4"]
    assert gate.max_running <= 2


async def test_d1_default_thread_count_is_disk_threads(make_executor, gate):
    ex = make_executor()
    for i in range(diskio.DISK_THREADS + 2):
        ex.submit(gate.job(str(i)))
    await wait_until(lambda: len(gate.started) == diskio.DISK_THREADS, what="chưa đủ việc chạy")
    await asyncio.sleep(0.2)
    assert gate.max_running == diskio.DISK_THREADS
    gate.release()
    assert await ex.wait_idle(SLACK)


# =================================================================================================
# DiskJob: started, done, result, settled, cancel
# =================================================================================================


async def test_d1_job_flags_and_result(make_executor, gate):
    ex = make_executor(1)
    job = ex.submit(gate.job("a", result=42))
    assert isinstance(job, DiskJob)
    await wait_until(lambda: job.started, what="việc chưa bắt đầu")
    assert job.done is False
    gate.release()
    assert await job.result(5) == 42
    assert job.started and job.done
    assert await job.result(5) == 42, "gọi lại sau khi done trả lại đúng kết quả"
    assert await job.result(None) == 42


async def test_d1_result_deadline_counts_from_thread_start(make_executor, gate):
    """Hạn của `result(timeout)` tính từ lúc thread bắt đầu việc, không tính lúc xếp hàng."""
    ex = make_executor(1)
    blocker = ex.submit(gate.job("chan"))
    queued = ex.submit(lambda: "nhanh")
    waiter = asyncio.create_task(queued.result(0.2))
    await asyncio.sleep(0.6)  # gấp 3 lần hạn, nhưng việc chưa bắt đầu
    assert not waiter.done(), "việc còn xếp hàng không được quá hạn"
    assert queued.started is False
    gate.release()
    assert await asyncio.wait_for(waiter, SLACK) == "nhanh"
    await blocker.settled()


async def test_d1_job_exception_is_reraised_on_every_result_and_settled_never_raises(make_executor):
    ex = make_executor()
    err = ValueError("lỗi giả lập")

    def boom() -> None:
        raise err

    job = ex.submit(boom)
    await job.settled()
    assert job.done
    for _ in range(2):
        with pytest.raises(ValueError) as info:
            await job.result(5)
        assert info.value is err


async def test_d1_cancel_before_start_drops_job(make_executor, gate):
    ex = make_executor(1)
    blocker = ex.submit(gate.job("chan"))
    ran: list[str] = []
    job = ex.submit(lambda: ran.append("chay"))
    await wait_until(lambda: blocker.started, what="việc chặn chưa chạy")
    assert job.cancel() is True
    assert job.done is True and job.started is False
    with pytest.raises(asyncio.CancelledError):
        await job.result(5)
    await asyncio.wait_for(job.settled(), SLACK)  # không ném
    assert ex.busy == 1
    gate.release()
    assert await ex.wait_idle(SLACK)
    await asyncio.sleep(0.05)
    assert ran == [], "việc đã hủy trước khi chạy không bao giờ chạy"


async def test_d1_cancel_after_start_returns_false_and_job_runs_to_end(make_executor, gate):
    ex = make_executor(1)
    job = ex.submit(gate.job("a"))
    await wait_until(lambda: job.started, what="việc chưa bắt đầu")
    assert job.cancel() is False
    assert ex.busy == 1
    gate.release()
    assert await job.result(5) == "a"
    assert gate.finished == ["a"]


async def test_d1_cancelling_task_awaiting_result_does_not_cancel_job(make_executor, gate):
    ex = make_executor(1)
    job = ex.submit(gate.job("a"))
    waiter = asyncio.create_task(job.result(None))
    await wait_until(lambda: job.started, what="việc chưa bắt đầu")
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert ex.busy == 1 and job.done is False
    gate.release()
    assert await job.result(5) == "a"


async def test_d1_cancelling_await_of_run_does_not_stop_thread(make_executor, gate):
    ex = make_executor(1)
    task = asyncio.create_task(ex.run(gate.job("a"), timeout=None))
    await wait_until(lambda: gate.started == ["a"], what="việc chưa bắt đầu")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ex.busy == 1, "hủy await không dừng thread"
    gate.release()
    assert await ex.wait_idle(SLACK)
    assert gate.finished == ["a"]


async def test_d1_cancelling_run_before_job_starts_drops_job(make_executor, gate):
    ex = make_executor(1)
    blocker = ex.submit(gate.job("chan"))
    await wait_until(lambda: blocker.started, what="việc chặn chưa chạy")
    ran: list[str] = []
    task = asyncio.create_task(ex.run(lambda: ran.append("chay"), timeout=None))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.release()
    assert await ex.wait_idle(SLACK)
    await asyncio.sleep(0.05)
    assert ran == [], "run bị hủy khi việc chưa bắt đầu → job.cancel()"


# =================================================================================================
# shutdown
# =================================================================================================


async def test_d1_shutdown_does_not_block_on_hung_job(make_executor, gate):
    ex = make_executor(1)
    running = ex.submit(gate.job("treo"))
    queued_ran: list[str] = []
    queued = ex.submit(lambda: queued_ran.append("chay"))
    await wait_until(lambda: running.started, what="việc chưa bắt đầu")
    t0 = time.monotonic()
    ex.shutdown()
    assert time.monotonic() - t0 < 0.5, "shutdown() không được join thread"
    # Việc chưa chạy bị bỏ: result → CancelledError, settled trả về.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(queued.result(5), SLACK)
    await asyncio.wait_for(queued.settled(), SLACK)
    assert queued.done is True
    # Sau shutdown không nhận việc mới.
    with pytest.raises(RuntimeError):
        ex.submit(lambda: None)
    with pytest.raises(RuntimeError):
        await ex.run(lambda: None, timeout=1)
    # Việc đang chạy vẫn chạy tới cùng; thread là daemon nên không giữ tiến trình.
    assert all(t.daemon for t in gate.threads)
    gate.release()
    assert await running.result(SLACK) == "treo"
    await asyncio.sleep(0.05)
    assert queued_ran == []


async def test_d1_shutdown_twice_is_harmless(make_executor):
    ex = make_executor()
    assert await ex.run(lambda: 1, timeout=5) == 1
    ex.shutdown()
    ex.shutdown()
    with pytest.raises(RuntimeError):
        await ex.run(lambda: 1, timeout=5)


# =================================================================================================
# Loop đã đóng
# =================================================================================================


def test_d1_result_after_loop_closed_is_dropped_without_error_in_thread(monkeypatch, caplog):
    """Kết quả về qua `loop.call_soon_threadsafe`; loop đã đóng thì bỏ qua, không ném trong thread."""
    caplog.set_level(logging.DEBUG, logger="teleport")
    thread_errors: list[object] = []
    monkeypatch.setattr(threading, "excepthook", thread_errors.append)
    gate = threading.Event()
    finished = threading.Event()

    def slow() -> str:
        try:
            assert gate.wait(30)
            return "muon"
        finally:
            finished.set()

    ex = DiskExecutor(1)
    loop = asyncio.new_event_loop()
    try:

        async def start() -> DiskJob[str]:
            job = ex.submit(slow)
            while not job.started:
                await asyncio.sleep(0.005)
            return job

        loop.run_until_complete(asyncio.wait_for(start(), 5))
    finally:
        loop.close()
    try:
        gate.set()
        assert finished.wait(5)
        time.sleep(0.3)  # thread trả kết quả về loop đã đóng
        assert thread_errors == []
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [], [r.getMessage() for r in errors]
        # Thread vẫn sống và nhận việc từ loop khác.
        assert asyncio.run(asyncio.wait_for(ex.run(lambda: 7, timeout=5), 5)) == 7
    finally:
        gate.set()
        ex.shutdown()


# =================================================================================================
# C3 (spec-p2-6 mục C): busy/wait_idle đếm ở phía loop — busy == 0 ⇒ mọi job đã done
# =================================================================================================


async def test_c3_busy_counts_job_until_loop_delivers_result(make_executor, gate):
    """Thread xong việc trong lúc loop đang bận (time.sleep đồng bộ): kết quả chưa được giao về loop,
    nên `busy` chưa được về 0 khi job chưa `done`."""
    ex = make_executor(1)
    job = ex.submit(gate.job("a"))
    waiter = asyncio.create_task(job.settled())
    await wait_until(lambda: job.started, what="việc chưa bắt đầu")
    gate.release()
    # Chặn loop (chờ đồng bộ, có hạn) tới khi thread chạy xong: call_soon_threadsafe chưa được xử lý.
    deadline = time.monotonic() + SLACK
    while not gate.finished and time.monotonic() < deadline:
        time.sleep(0.01)
    assert gate.finished == ["a"]
    assert ex.busy != 0 or job.done, "busy == 0 trong khi job chưa done"
    assert ex.busy == 1, "loop chưa giao kết quả: việc vẫn tính là bận (C3)"
    assert await ex.wait_idle(SLACK) is True
    assert job.done
    await asyncio.sleep(0)
    assert waiter.done(), "wait_idle True ⇒ mọi waiter của settled() đã được đánh thức"
    assert await job.result(0) == "a"


async def test_c3_wait_idle_true_implies_all_jobs_done(make_executor, gate):
    ex = make_executor(2)
    jobs = [ex.submit(gate.job(str(i))) for i in range(5)]
    waiters = [asyncio.create_task(j.settled()) for j in jobs]
    await wait_until(lambda: len(gate.started) == 2, what="chưa đủ 2 việc chạy")
    gate.release()
    time.sleep(0.2)  # các thread xong (và nhận việc mới) trong lúc loop bận
    assert ex.busy != 0 or all(j.done for j in jobs)
    assert await ex.wait_idle(SLACK) is True
    assert ex.busy == 0
    assert all(j.done for j in jobs), [j.done for j in jobs]
    await asyncio.sleep(0)
    assert all(w.done() for w in waiters)


async def test_c3_timed_out_job_counts_until_delivered(make_executor, gate):
    ex = make_executor(1)
    job = ex.submit(gate.job("treo"))
    with pytest.raises(DiskTimeout):
        await job.result(0.1)
    gate.release()
    deadline = time.monotonic() + SLACK
    while not gate.finished and time.monotonic() < deadline:
        time.sleep(0.01)  # chặn loop: kết quả chưa được giao
    assert gate.finished == ["treo"]
    assert ex.busy == 1, "việc quá hạn vẫn tính là bận tới khi loop giao kết quả (C3)"
    assert await ex.wait_idle(SLACK) is True
    assert job.done
