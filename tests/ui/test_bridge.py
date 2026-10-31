"""`teleport.ui.bridge.NodeHost` (spec-p4-3 mục 3, D1, D2, D8, mục 6.2 và mục 10).

Node chạy trong thread riêng với event loop asyncio chuẩn; Qt chỉ gọi node qua `call`/`submit`; mỗi
phương thức công khai `assert` đang ở thread Qt; `stop()` không chặn; đóng treo thì vẫn `stopped`
sau `close_time_limit()`; dừng lúc đang khởi động thì chỉ phát `stopped`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QTimer, Slot

integration = pytest.mark.integration


class Probe(QObject):
    """Nhận tín hiệu của `NodeHost` bằng slot của một QObject sống ở thread Qt (như cửa sổ thật)."""

    def __init__(self, host) -> None:
        super().__init__()
        self.log: list[tuple[str, tuple, int]] = []
        host.started.connect(self.on_started)
        host.start_failed.connect(self.on_start_failed)
        host.stopped.connect(self.on_stopped)

    @Slot()
    def on_started(self) -> None:
        self.log.append(("started", (), threading.get_ident()))

    @Slot(str, str)
    def on_start_failed(self, code: str, detail: str) -> None:
        self.log.append(("start_failed", (code, detail), threading.get_ident()))

    @Slot()
    def on_stopped(self) -> None:
        self.log.append(("stopped", (), threading.get_ident()))

    def names(self) -> list[str]:
        return [name for name, _, _ in self.log]

    def count(self, name: str) -> int:
        return self.names().count(name)


def wait_joined(qtbot, host, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not host.join(0.05):
        if time.monotonic() > deadline:
            return False
        qtbot.wait(10)
    return True


def start_and_wait(qtbot, host, probe, timeout_ms: int = 10_000) -> None:
    host.start()
    qtbot.waitUntil(
        lambda: bool(probe.names()) and probe.names()[0] in ("started", "start_failed"), timeout=timeout_ms
    )
    assert probe.names()[0] == "started", probe.log


# --- Khởi động ----------------------------------------------------------------------------------------


@integration
def test_start_emits_started_within_10s_on_qt_thread(qtbot, config_dir, node_options, node_hosts):
    host = node_hosts(node_options(config_dir("A")))
    probe = Probe(host)
    assert not host.running
    t0 = time.monotonic()
    start_and_wait(qtbot, host, probe)
    assert time.monotonic() - t0 < 10
    assert probe.log[0][2] == threading.get_ident(), "slot của started phải chạy trên thread Qt"
    assert host.running


@integration
def test_start_twice_raises_runtime_error(qtbot, config_dir, node_options, node_hosts):
    host = node_hosts(node_options(config_dir("A")))
    probe = Probe(host)
    start_and_wait(qtbot, host, probe)
    with pytest.raises(RuntimeError):
        host.start()


@integration
def test_start_failed_locked_when_node_lock_held(qtbot, config_dir, node_options, node_hosts, fast_lock):
    from teleport.lock import NodeLock

    d = config_dir("A")
    holder = NodeLock(d)
    holder.acquire()
    try:
        host = node_hosts(node_options(d))
        probe = Probe(host)
        host.start()
        qtbot.waitUntil(lambda: "start_failed" in probe.names() or "started" in probe.names(), timeout=10_000)
        assert "started" not in probe.names()
        [(_, (code, detail), ident)] = [e for e in probe.log if e[0] == "start_failed"]
        assert code == "locked"
        assert isinstance(detail, str)
        assert ident == threading.get_ident()
        assert not host.running
        assert wait_joined(qtbot, host, 5), "thread node phải kết thúc sau khi khởi động lỗi"
        assert holder.held, "bên đang giữ khóa không bị đụng tới"
    finally:
        holder.release()


def test_start_error_codes_are_the_spec_values():
    from teleport.ui.bridge import StartError

    assert {e.value for e in StartError} == {
        "locked",
        "config_dir",
        "identity",
        "trust_store",
        "receive_dir",
        "port",
        "other",
    }
    assert StartError.LOCKED == "locked"


@integration
def test_start_failed_receive_dir_when_receive_dir_is_a_file(
    qtbot, config_dir, node_options, node_hosts, tmp_path
):
    d = config_dir("A")
    not_a_dir = tmp_path / "nhan-la-file"
    not_a_dir.write_bytes(b"x")
    host = node_hosts(node_options(d, receive_dir=not_a_dir))
    probe = Probe(host)
    host.start()
    qtbot.waitUntil(lambda: bool(probe.names()), timeout=15_000)
    assert probe.log[0][0] == "start_failed", probe.log
    assert probe.log[0][1][0] == "receive_dir"
    assert wait_joined(qtbot, host, 5)
    assert not_a_dir.read_bytes() == b"x"


@integration
def test_start_failed_port_when_port_busy(qtbot, config_dir, node_options, node_hosts):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        busy = s.getsockname()[1]
        host = node_hosts(node_options(config_dir("A"), port=busy))
        probe = Probe(host)
        host.start()
        qtbot.waitUntil(lambda: bool(probe.names()), timeout=15_000)
        assert probe.log[0][0] == "start_failed", probe.log
        assert probe.log[0][1][0] == "port"
        assert wait_joined(qtbot, host, 5)
    finally:
        s.close()


def test_start_failed_config_dir_when_config_dir_is_a_file(qtbot, node_options, node_hosts, tmp_path):
    f = tmp_path / "cfg-la-file"
    f.write_bytes(b"khong phai thu muc")
    host = node_hosts(node_options(f))
    probe = Probe(host)
    host.start()
    qtbot.waitUntil(lambda: bool(probe.names()), timeout=15_000)
    assert probe.log[0][0] == "start_failed", probe.log
    assert probe.log[0][1][0] == "config_dir"
    assert wait_joined(qtbot, host, 5)
    assert f.read_bytes() == b"khong phai thu muc"


# --- call / submit -------------------------------------------------------------------------------------


@integration
def test_call_runs_on_node_thread_and_on_done_on_qt_thread(qtbot, config_dir, node_options, node_hosts):
    host = node_hosts(node_options(config_dir("A")))
    probe = Probe(host)
    start_and_wait(qtbot, host, probe)
    qt_thread = threading.get_ident()
    seen: dict = {}

    def fn(node):
        seen["fn_thread"] = threading.get_ident()
        seen["loop"] = asyncio.get_running_loop()
        return node.port

    def on_done(result):
        seen["done_thread"] = threading.get_ident()
        seen["result"] = result

    host.call(fn, on_done)
    qtbot.waitUntil(lambda: "result" in seen, timeout=5_000)
    assert seen["fn_thread"] != qt_thread
    assert seen["done_thread"] == qt_thread
    assert isinstance(seen["result"], int) and seen["result"] > 0


@integration
def test_call_exception_is_delivered_to_on_done(qtbot, config_dir, node_options, node_hosts):
    host = node_hosts(node_options(config_dir("A")))
    probe = Probe(host)
    start_and_wait(qtbot, host, probe)
    got: list = []

    def boom(node):
        raise ValueError("lỗi giả")

    host.call(boom, got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=5_000)
    assert isinstance(got[0], ValueError)
    assert host.running, "lỗi trong fn không làm node dừng"


@integration
def test_submit_awaits_coroutine_on_node_loop(qtbot, config_dir, node_options, node_hosts):
    host = node_hosts(node_options(config_dir("A")))
    probe = Probe(host)
    start_and_wait(qtbot, host, probe)
    qt_thread = threading.get_ident()
    seen: dict = {}

    async def work(node):
        seen["fn_thread"] = threading.get_ident()
        await asyncio.sleep(0.01)
        return node.identity.device_id

    def on_done(result):
        seen["done_thread"] = threading.get_ident()
        seen["result"] = result

    host.submit(work, on_done)
    qtbot.waitUntil(lambda: "result" in seen, timeout=5_000)
    assert seen["fn_thread"] != qt_thread
    assert seen["done_thread"] == qt_thread
    assert isinstance(seen["result"], str) and len(seen["result"]) == 32


def test_call_before_start_reports_runtime_error(qtbot, config_dir, node_options, node_hosts):
    host = node_hosts(node_options(config_dir("A")))
    got: list = []
    host.call(lambda node: 1, got.append)
    host.submit(_noop_coro, got.append)
    qtbot.waitUntil(lambda: len(got) == 2, timeout=2_000)
    assert all(isinstance(r, RuntimeError) for r in got), got


async def _noop_coro(node):
    return 1


@integration
def test_call_after_stop_reports_runtime_error(qtbot, config_dir, node_options, node_hosts):
    host = node_hosts(node_options(config_dir("A")))
    probe = Probe(host)
    start_and_wait(qtbot, host, probe)
    host.stop()
    qtbot.waitUntil(lambda: "stopped" in probe.names(), timeout=30_000)
    got: list = []
    host.call(lambda node: 1, got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=2_000)
    assert isinstance(got[0], RuntimeError)


@integration
@pytest.mark.parametrize("method", ["call", "submit", "stop", "start", "join"])
def test_public_methods_assert_qt_thread(qtbot, config_dir, node_options, node_hosts, method):
    """D2: mọi phương thức công khai của `NodeHost` `assert` đang ở thread Qt."""
    host = node_hosts(node_options(config_dir("A")))
    probe = Probe(host)
    if method != "start":
        start_and_wait(qtbot, host, probe)
    errors: list[BaseException] = []

    def other_thread():
        try:
            if method == "call":
                host.call(lambda node: 1, None)
            elif method == "submit":
                host.submit(_noop_coro, None)
            elif method == "stop":
                host.stop()
            elif method == "start":
                host.start()
            else:
                host.join(0.01)
        except BaseException as e:
            errors.append(e)

    t = threading.Thread(target=other_thread)
    t.start()
    t.join(5)
    assert errors and isinstance(errors[0], AssertionError), errors
    if method == "stop":
        qtbot.wait(100)
        assert host.running, "stop() từ thread khác không được có hiệu lực"
    if method == "start":
        qtbot.wait(100)
        assert probe.names() == [], "start() từ thread khác không được có hiệu lực"


# --- stop -------------------------------------------------------------------------------------------


def test_stop_before_start_emits_stopped(qtbot, config_dir, node_options, node_hosts):
    host = node_hosts(node_options(config_dir("A")))
    probe = Probe(host)
    host.stop()
    qtbot.waitUntil(lambda: "stopped" in probe.names(), timeout=2_000)
    host.stop()  # lần hai: không lỗi, không phát thêm
    qtbot.wait(100)
    assert probe.count("stopped") == 1, probe.log
    assert not host.running
    assert host.join(1)


@integration
def test_stop_after_start_releases_lock_and_joins(qtbot, config_dir, node_options, node_hosts, ui_helpers):
    d = config_dir("A")
    host = node_hosts(node_options(d))
    probe = Probe(host)
    start_and_wait(qtbot, host, probe)
    assert not ui_helpers.lock_is_free(d)
    t0 = time.monotonic()
    host.stop()
    assert time.monotonic() - t0 < 0.5, "stop() không được chặn thread Qt"
    host.stop()  # idempotent
    qtbot.waitUntil(lambda: "stopped" in probe.names(), timeout=30_000)
    assert probe.log[-1][2] == threading.get_ident()
    assert host.join(5)
    assert not host.running
    assert ui_helpers.lock_is_free(d), "node.lock phải lấy lại được sau stop"
    qtbot.wait(100)
    assert probe.count("stopped") == 1, probe.log


@integration
def test_stop_with_hanging_close_still_emits_stopped_and_qt_keeps_running(
    qtbot, config_dir, node_options, node_hosts, monkeypatch
):
    """D8 + mục 10: `close()` treo → hủy sau `app.node.close_time_limit()` (đọc lúc dùng), vẫn `stopped`;
    thread Qt không bị chặn (QTimer vẫn chạy)."""
    from teleport.app import node as app_node

    limit = 0.5
    monkeypatch.setattr(app_node, "close_time_limit", lambda: limit)
    real_close = app_node.Node.close
    calls = {"close": 0, "cancelled": 0}

    async def hanging_close(self):
        calls["close"] += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            calls["cancelled"] += 1
            # Dọn thật để không rò cổng/khóa sang test sau.
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(real_close(self), 15)
            raise

    monkeypatch.setattr(app_node.Node, "close", hanging_close)
    host = node_hosts(node_options(config_dir("A")))
    probe = Probe(host)
    start_and_wait(qtbot, host, probe)

    ticks: list[float] = []
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    try:
        t0 = time.monotonic()
        host.stop()
        assert time.monotonic() - t0 < 0.5, "stop() không được chặn thread Qt"
        qtbot.waitUntil(lambda: "stopped" in probe.names(), timeout=int((limit + 10) * 1000))
        elapsed = time.monotonic() - t0
    finally:
        timer.stop()
    assert calls["close"] >= 1
    assert calls["cancelled"] >= 1, "close() treo phải bị hủy khi quá hạn"
    assert elapsed >= limit * 0.8, f"không được hủy close() trước hạn ({elapsed:.2f} s < {limit} s)"
    in_window = [t for t in ticks if t0 <= t <= t0 + elapsed]
    assert len(in_window) >= 5, f"QTimer của Qt chỉ chạy {len(in_window)} lần trong {elapsed:.2f} s"
    assert wait_joined(qtbot, host, 15)


@pytest.mark.slow
@integration
def test_stop_during_start_closes_without_cancelling_start(
    qtbot, config_dir, node_options, node_hosts, monkeypatch, ui_helpers
):
    """Mục 10: `stop` lúc `start` chưa xong → gọi `node.close()`, không hủy task start; `RuntimeError` do
    đang dừng → chỉ phát `stopped`, không phát `start_failed`."""
    from teleport.app import node as app_node

    entered = threading.Event()
    seen = {"start_cancelled": False, "close_calls": 0}
    real_start = app_node.Node.start
    real_close = app_node.Node.close

    async def slow_start(self):
        entered.set()
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            seen["start_cancelled"] = True
            raise
        return await real_start(self)

    async def counting_close(self):
        seen["close_calls"] += 1
        return await real_close(self)

    monkeypatch.setattr(app_node.Node, "start", slow_start)
    monkeypatch.setattr(app_node.Node, "close", counting_close)
    d = config_dir("A")
    host = node_hosts(node_options(d))
    probe = Probe(host)
    host.start()
    qtbot.waitUntil(entered.is_set, timeout=10_000)
    host.stop()
    qtbot.waitUntil(lambda: "stopped" in probe.names(), timeout=30_000)
    qtbot.wait(200)
    assert "start_failed" not in probe.names(), probe.log
    assert "started" not in probe.names(), probe.log
    assert seen["close_calls"] >= 1, "stop lúc đang khởi động phải gọi node.close()"
    assert not seen["start_cancelled"], "không được cancel task start"
    assert wait_joined(qtbot, host, 5)
    assert ui_helpers.lock_is_free(d)


# --- Thư mục nhận đã lưu (spec-b1b mục 2.1, 6.1) ----------------------------------------------------


def _same_dir(a, b) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def test_node_options_defaults_to_saved_receive_dir(tmp_path):
    from teleport.app.node import ReceiveDirChoice
    from teleport.ui.bridge import NodeOptions

    assert NodeOptions(config_dir=tmp_path).receive_dir is ReceiveDirChoice.SAVED


def test_gui_default_options_use_saved_receive_dir(tmp_path):
    """Spec-b1b 2.7: app mặc định dùng thư mục đã lưu, không ép `paths.default_receive_dir()`."""
    from teleport.app.node import ReceiveDirChoice
    from teleport.ui import app as ui_app

    assert ui_app._default_options(tmp_path).receive_dir is ReceiveDirChoice.SAVED


@integration
def test_saved_receive_dir_uses_default_when_nothing_saved(
    qtbot, config_dir, node_options, node_hosts, fake_default_receive_dir
):
    from teleport.app.node import ReceiveDirChoice

    host = node_hosts(node_options(config_dir("A"), receive_dir=ReceiveDirChoice.SAVED))
    probe = Probe(host)
    start_and_wait(qtbot, host, probe)
    assert host.receive_dir is not None
    assert _same_dir(host.receive_dir, fake_default_receive_dir)
    assert host.receive_dir_error is None
    assert fake_default_receive_dir.is_dir()


def test_receive_dir_props_none_before_started(qtbot, config_dir, node_options, node_hosts, tmp_path):
    host = node_hosts(node_options(config_dir("A"), receive_dir=tmp_path / "nhan-co-dinh"))
    assert host.receive_dir is None
    assert host.receive_dir_error is None


@integration
def test_receive_dir_props_none_while_starting(qtbot, config_dir, node_options, node_hosts, tmp_path):
    host = node_hosts(node_options(config_dir("A"), receive_dir=tmp_path / "nhan-co-dinh"))
    probe = Probe(host)
    host.start()
    seen_before: list = []

    def check() -> bool:
        if not probe.names():
            seen_before.append((host.receive_dir, host.receive_dir_error))
            return False
        return True

    qtbot.waitUntil(check, timeout=10_000)
    assert all(v == (None, None) for v in seen_before), seen_before
    assert probe.names()[0] == "started"
    assert _same_dir(host.receive_dir, tmp_path / "nhan-co-dinh")


@integration
def test_saved_dir_is_a_file_starts_running_with_receive_dir_error(
    qtbot, config_dir, node_options, node_hosts, tmp_path
):
    """Thư mục đã lưu hỏng (là một file): node vẫn chạy, không nhận file (mvp F10, spec-b1b mục 1)."""
    from teleport.app.node import ReceiveDirChoice
    from teleport.app.settings import Settings

    d = config_dir("A")
    not_a_dir = tmp_path / "da-luu-la-file"
    not_a_dir.write_bytes(b"x")
    Settings(receive_dir=str(not_a_dir)).save(d)
    host = node_hosts(node_options(d, receive_dir=ReceiveDirChoice.SAVED))
    probe = Probe(host)
    host.start()
    qtbot.waitUntil(lambda: bool(probe.names()), timeout=15_000)
    assert probe.names()[0] == "started", probe.log
    assert host.running
    assert host.receive_dir is None
    assert host.receive_dir_error is not None
    assert _same_dir(host.receive_dir_error, not_a_dir)
    assert not_a_dir.read_bytes() == b"x", "file nằm ở chỗ thư mục đã lưu không được bị đụng tới"
    assert Settings.load(d).receive_dir == str(not_a_dir), "lỗi lúc start không được xóa cài đặt đã lưu"


@integration
def test_receive_dir_props_are_snapshot_at_started(qtbot, config_dir, node_options, node_hosts, tmp_path):
    """Spec-b1b 2.1: property là ảnh chụp lúc `started`; đổi thư mục sau đó không làm chúng đổi."""
    old = tmp_path / "nhan-cu"
    new = tmp_path / "nhan-moi"
    new.mkdir()
    host = node_hosts(node_options(config_dir("A"), receive_dir=old))
    probe = Probe(host)
    start_and_wait(qtbot, host, probe)
    assert _same_dir(host.receive_dir, old)
    got: list = []
    host.submit(lambda node: node.set_receive_dir(new), got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=10_000)
    assert isinstance(got[0], Path) and _same_dir(got[0], new), got
    assert _same_dir(host.receive_dir, old)
    assert host.receive_dir_error is None
