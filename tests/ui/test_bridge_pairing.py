"""`NodeHost` cho ghép đôi trên GUI (PR2, hợp đồng spec-pr2-api.md mục 1; spec-hackathon D2, 9.4).

- Node GUI dựng với `accept_pairing=True`; `pairing_prompt` / `pairing_finished` phát trên thread Qt.
- `submit` trả `SubmitJob`: `cancel()` hủy được `pair_with` đang chạy → `on_done(CancelledError)` và
  `pairing_finished` CANCELLED; `cancel()` lúc nào cũng không lỗi (trước khi task kịp tạo, sau khi xong,
  khi node đã dừng); `done` thành True khi `on_done` đã chạy.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

integration = pytest.mark.integration

LOCAL = "127.0.0.1"


class PairPeer:
    """Máy B: `Node` thật nhận ghép đôi, tự quyết mỗi lời hỏi bằng `answer`."""

    def __init__(self, config: Path, answer: bool | None = True) -> None:
        self.config = config
        self.answer = answer
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="test-pair-peer", daemon=True)
        self.node = None
        self.prompts: list = []
        self.results: list = []
        self._lock = threading.Lock()

    def _prompt(self, prompt) -> None:
        with self._lock:
            self.prompts.append(prompt)
        if self.answer is not None:
            self.loop.call_soon(self.node.pairing.decide, prompt.session_id, self.answer)

    def _finished(self, result) -> None:
        with self._lock:
            self.results.append(result)

    def start(self) -> PairPeer:
        from teleport.app.node import Node

        self.thread.start()

        async def go():
            self.node = Node(
                self.config,
                name="May-B",
                host=LOCAL,
                port=0,
                accept_pairing=True,
                on_pairing_prompt=self._prompt,
                on_pairing_finished=self._finished,
            )
            await self.node.start()

        asyncio.run_coroutine_threadsafe(go(), self.loop).result(20)
        return self

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def close(self) -> None:
        if self.node is not None:
            with contextlib.suppress(Exception):
                self.submit(asyncio.wait_for(self.node.close(), 30)).result(35)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        if not self.thread.is_alive():
            self.loop.close()


@pytest.fixture
def pair_peer():
    made: list[PairPeer] = []

    def make(config: Path, **kw) -> PairPeer:
        peer = PairPeer(config, **kw)
        made.append(peer)
        return peer.start()

    yield make
    for peer in reversed(made):
        peer.close()


@pytest.fixture
def silent_server():
    """Cổng TCP nhận kết nối (backlog) nhưng không bao giờ trả lời: bắt tay TLS treo."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((LOCAL, 0))
    s.listen(8)
    yield s.getsockname()[1]
    s.close()


class Done:
    """Ghi mọi lần `on_done` (kết quả, thread)."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, int]] = []

    def __call__(self, result) -> None:
        self.calls.append((result, threading.get_ident()))

    @property
    def results(self) -> list:
        return [r for r, _ in self.calls]


def started_host(qtbot, node_hosts, node_options, config_dir, label: str = "A"):
    host = node_hosts(node_options(config_dir(label)))
    host.start()
    qtbot.waitUntil(lambda: host.running, timeout=15_000)
    return host


def host_call(qtbot, host, fn, timeout_ms: int = 5_000):
    box: dict = {}
    host.call(fn, lambda r: box.setdefault("r", r))
    qtbot.waitUntil(lambda: "r" in box, timeout=timeout_ms)
    if isinstance(box["r"], BaseException):
        raise box["r"]
    return box["r"]


# --- accept_pairing và hai signal mới -------------------------------------------------------------


@integration
def test_gui_node_accepts_pairing(qtbot, config_dir, node_options, node_hosts):
    host = started_host(qtbot, node_hosts, node_options, config_dir)
    assert host_call(qtbot, host, lambda n: n.accept_pairing) is True
    assert host_call(qtbot, host, lambda n: n.pairing.accepting) is True


@integration
def test_pairing_prompt_and_finished_are_emitted_on_qt_thread(
    qtbot, config_dir, node_options, node_hosts, pair_peer
):
    from teleport.app.pairing import PairingFinished, PairingOutcome, PairingPrompt
    from teleport.core.pairing import Role

    qt_thread = threading.get_ident()
    host = node_hosts(node_options(config_dir("A")))
    prompts: list[tuple[object, int]] = []
    finished: list[tuple[object, int]] = []
    host.pairing_prompt.connect(lambda p: prompts.append((p, threading.get_ident())))
    host.pairing_finished.connect(lambda r: finished.append((r, threading.get_ident())))
    host.start()
    qtbot.waitUntil(lambda: host.running, timeout=15_000)
    port = host_call(qtbot, host, lambda n: n.port)

    b = pair_peer(config_dir("B"), answer=True)
    fut = b.submit(b.node.pairing.pair_with([LOCAL], port))
    qtbot.waitUntil(lambda: bool(prompts), timeout=10_000)
    prompt, ident = prompts[0]
    assert ident == qt_thread
    assert isinstance(prompt, PairingPrompt)
    assert prompt.role is Role.RESPONDER
    assert prompt.peer_device_id == b.node.identity.device_id
    assert host_call(qtbot, host, lambda n: n.pairing.decide(prompt.session_id, True)) is True

    qtbot.waitUntil(lambda: bool(finished), timeout=10_000)
    result, ident = finished[0]
    assert ident == qt_thread
    assert isinstance(result, PairingFinished)
    assert result.outcome is PairingOutcome.PAIRED
    assert result.session_id == prompt.session_id
    assert fut.result(10).outcome is PairingOutcome.PAIRED
    qtbot.wait(100)
    assert len(prompts) == 1 and len(finished) == 1


# --- SubmitJob ------------------------------------------------------------------------------------


@integration
def test_submit_returns_submit_job_done_after_on_done(qtbot, config_dir, node_options, node_hosts):
    from teleport.ui.bridge import SubmitJob

    host = started_host(qtbot, node_hosts, node_options, config_dir)
    done = Done()

    async def work(node):
        await asyncio.sleep(0.05)
        return node.port

    job = host.submit(work, done)
    assert isinstance(job, SubmitJob)
    assert job.done is False
    qtbot.waitUntil(lambda: bool(done.calls), timeout=5_000)
    assert job.done is True
    assert isinstance(done.results[0], int)
    assert done.calls[0][1] == threading.get_ident()


def test_submit_before_start_still_returns_job(qtbot, config_dir, node_options, node_hosts):
    from teleport.ui.bridge import SubmitJob

    host = node_hosts(node_options(config_dir("A")))
    done = Done()

    async def work(node):
        return 1

    job = host.submit(work, done)
    assert isinstance(job, SubmitJob)
    qtbot.waitUntil(lambda: bool(done.calls), timeout=2_000)
    assert isinstance(done.results[0], RuntimeError)
    assert job.done is True
    job.cancel()  # không lỗi
    qtbot.wait(50)
    assert len(done.calls) == 1


@integration
def test_cancel_running_pair_with_reports_cancelled(
    qtbot, config_dir, node_options, node_hosts, silent_server
):
    """9.4: nút “Hủy” → `job.cancel()` → `on_done(CancelledError)` và `pairing_finished` CANCELLED."""
    from teleport.app.pairing import PairingOutcome

    host = started_host(qtbot, node_hosts, node_options, config_dir)
    finished: list = []
    host.pairing_finished.connect(finished.append)
    done = Done()
    job = host.submit(lambda n: n.pairing.pair_with([LOCAL], silent_server), done)
    qtbot.wait(300)
    assert not done.calls, "pair_with tới máy không trả lời phải còn đang chạy"
    assert job.done is False
    t0 = time.monotonic()
    job.cancel()
    qtbot.waitUntil(lambda: bool(done.calls), timeout=5_000)
    assert time.monotonic() - t0 < 3
    assert isinstance(done.results[0], asyncio.CancelledError)
    assert done.calls[0][1] == threading.get_ident()
    assert job.done is True
    qtbot.waitUntil(lambda: bool(finished), timeout=5_000)
    assert [r.outcome for r in finished] == [PairingOutcome.CANCELLED]
    # Gọi lại sau khi xong: không lỗi, không gọi on_done lần nữa.
    job.cancel()
    job.cancel()
    qtbot.wait(100)
    assert len(done.calls) == 1
    # Node vẫn dùng được: lần ghép đôi sau không bị BUSY bởi phiên cũ.
    assert host_call(qtbot, host, lambda n: n.pairing.gate.active) is None


@integration
def test_cancel_right_after_submit_is_safe(qtbot, config_dir, node_options, node_hosts):
    """Hủy ngay trong cùng nhịp với `submit` (task có thể chưa kịp tạo): không lỗi, on_done đúng một lần."""
    host = started_host(qtbot, node_hosts, node_options, config_dir)
    done = Done()

    async def slow(node):
        await asyncio.sleep(30)
        return "xong"

    job = host.submit(slow, done)
    job.cancel()
    qtbot.waitUntil(lambda: bool(done.calls), timeout=5_000)
    assert isinstance(done.results[0], asyncio.CancelledError)
    assert job.done is True
    qtbot.wait(100)
    assert len(done.calls) == 1


@integration
def test_cancel_after_completion_does_nothing(qtbot, config_dir, node_options, node_hosts):
    host = started_host(qtbot, node_hosts, node_options, config_dir)
    done = Done()

    async def quick(node):
        return 42

    job = host.submit(quick, done)
    qtbot.waitUntil(lambda: bool(done.calls), timeout=5_000)
    job.cancel()
    qtbot.wait(100)
    assert done.results == [42]
    assert job.done is True


@integration
def test_cancel_when_host_stopped_is_safe(qtbot, config_dir, node_options, node_hosts):
    host = started_host(qtbot, node_hosts, node_options, config_dir)
    done = Done()

    async def slow(node):
        await asyncio.sleep(30)

    job = host.submit(slow, done)
    qtbot.wait(100)
    host.stop()
    qtbot.waitUntil(lambda: bool(done.calls), timeout=15_000)
    assert isinstance(done.results[0], (asyncio.CancelledError, RuntimeError))
    job.cancel()  # đang dừng
    deadline = time.monotonic() + 30
    while not host.join(0.05):
        qtbot.wait(20)
        assert time.monotonic() < deadline
    job.cancel()  # đã dừng hẳn
    qtbot.wait(50)
    assert len(done.calls) == 1
    assert job.done is True


@integration
def test_call_still_returns_none(qtbot, config_dir, node_options, node_hosts):
    host = started_host(qtbot, node_hosts, node_options, config_dir)
    assert host.call(lambda n: n.port) is None
