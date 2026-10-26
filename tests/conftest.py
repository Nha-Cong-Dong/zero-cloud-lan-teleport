"""Fixture chung cho mọi test.

1. Nới giới hạn tốc độ bắt tay (`docs/protocol.md` mục 2.3).

`teleport.core.admission` giới hạn **tốc độ** bắt tay theo IP (burst 2, 1/s cho IP lạ). Mọi test chạy
trên 127.0.0.1, nên một test mở vài kết nối liên tiếp sẽ vấp giới hạn đó dù không thử nó. Fixture tự
động dưới đây chỉ nới **tốc độ và burst** (cả pool chung lẫn pool ưu tiên); các giới hạn số kết nối
đồng thời (`MAX_OPENING_*`, `PRIORITY_OPENING_*`, `MAX_ANONYMOUS_*`) giữ nguyên mặc định, test nào
cần thì tự đặt lại.

Test cần mặc định thật (test giới hạn tốc độ, test tích hợp chạy “như sản phẩm”) gắn marker
`real_admission_limits`. Test chạy node bằng tiến trình con không nhận monkeypatch: chúng chạy với
mặc định thật (đã kiểm: mỗi tiến trình `listen` trong `tests/test_cli.py` chỉ nhận một kết nối).

2. Fixture dùng chung giữa `tests/app/`, `tests/test_cli.py` và `tests/test_cli_transfer.py` (P2-7,
spec-p2-7 mục A5). pytest chạy với `--import-mode=importlib` và không có `tests/__init__.py`, nên test
không import được conftest: lớp phụ trợ (`CliRun`, `Proc`, mDNS giả) chỉ lấy được qua fixture.

- `fast` (trước ở `tests/app/conftest.py`): rút ngắn hằng thời gian của ghép đôi, kết nối, `core.links`,
  `core.transfer`; trả `FAST` (`fast_values` trả `FAST` mà không vá gì). `tests/net/conftest.py` có
  `fast` riêng, ghi đè bản này trong `tests/net/`.
- `fast_cli`, `cli_no_leaks`, `cli_runs` (`CliRun`), `procs` (`Proc`), `cli_mdns` (trước ở
  `tests/test_cli.py`). `cli_runs` chạy được `listen`, `pair`, `send`, `receive` trong tiến trình.

Module `teleport.*` ngoài `core.admission` chỉ được import **bên trong** fixture, để một module đang
viết dở chỉ làm đỏ các test dùng tới nó.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from teleport.core import admission

REAL_LIMITS_MARKER = "real_admission_limits"

# Hằng tốc độ/burst của `teleport.core.admission` (`docs/protocol.md` mục 2.3).
RATE_CONSTANTS = (
    "HANDSHAKE_RATE_PER_IP",
    "HANDSHAKE_BURST_PER_IP",
    "HANDSHAKE_RATE_TOTAL",
    "HANDSHAKE_BURST_TOTAL",
    "PRIORITY_RATE_PER_IP",
    "PRIORITY_BURST_PER_IP",
    "PRIORITY_RATE_TOTAL",
    "PRIORITY_BURST_TOTAL",
)
RELAXED_RATE = 1_000_000.0


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{REAL_LIMITS_MARKER}: không nới tốc độ/burst bắt tay của teleport.core.admission (mặc định thật)",
    )


@pytest.fixture(autouse=True)
def relaxed_admission_rates(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nới tốc độ và burst bắt tay cho test không thử chúng; tắt bằng `@pytest.mark.real_admission_limits`."""
    if request.node.get_closest_marker(REAL_LIMITS_MARKER) is not None:
        return
    # Không `raising=False`: đổi tên hay xóa một hằng thì mọi test đỏ ngay, thay vì lặng lẽ chạy với
    # tốc độ thật.
    for name in RATE_CONSTANTS:
        monkeypatch.setattr(admission, name, RELAXED_RATE)


# =================================================================================================
# Hằng thời gian rút ngắn (đặc tả PR-3 mục 4.1, PR-4 mục 4.1, P2-6b việc 6b-T0)
# =================================================================================================

# Hằng thời gian sau khi patch; test đọc từ giá trị trả về của `fast` để tính biên.
FAST = SimpleNamespace(
    MESSAGE_TIMEOUT=1.0,
    NONCE_TIMEOUT=0.5,  # bên trả lời chờ pair_nonce; nhỏ hơn MESSAGE_TIMEOUT để phân biệt hai hạn
    CONFIRM_TIMEOUT=2.0,
    CONFIRM_GRACE=0.5,
    BACKOFF_BASE=0.5,
    BACKOFF_MAX=2.0,
    OPENING_TIMEOUT=2.0,
    CLOSE_TIMEOUT=0.5,
    DRAIN_TIMEOUT=1.0,
    SEND_TIMEOUT=1.0,
    BUSY_REPLY_TIMEOUT=0.5,
    ABORT_ON_CANCEL_TIMEOUT=0.5,
    CLOSE_SESSIONS_TIMEOUT=1.0,
    LOCK_TIMEOUT=0.5,
    # PR-4 (đặc tả PR-4 mục 4.1): teleport.core.links và teleport.app.node
    RETRY_BASE=0.2,
    RETRY_MAX=1.0,
    EVENT_RETRY_GAP=0.2,
    REPLACE_AFTER=2.0,
    NETWORK_CHECK_INTERVAL=0.2,
    SLACK=2.5,  # biên trên nới cho CI: thời gian chờ + 2,5 s
    # P2-6b (kế hoạch mục 4, việc 6b-T0): teleport.core.transfer và teleport.net.connection
    RECONNECT_GRACE=4.0,
    DATA_OPEN_WAIT=2.0,
    DATA_RETRY_BASE=0.2,
    DATA_RETRY_MAX=1.0,
    IDLE_TIMEOUT=5.0,
    # `transfer_nodes` đặt lại `connection.DRAIN_TIMEOUT` (spec-p2-6 B12: ≥ 10 s), vì bên nhận có thể
    # ngừng đọc trong lúc chờ đĩa và một đoạn ghi của bên gửi phải chờ được lâu hơn 1 s.
    TRANSFER_DRAIN_TIMEOUT=10.0,
)


@pytest.fixture
def fast_values():
    """`FAST` mà không vá gì (cho `app_helpers`)."""
    return FAST


@pytest.fixture
def fast(monkeypatch):
    """Rút ngắn hằng thời gian (đặc tả PR-3 mục 4.1). Patch qua module, trước khi tạo Node."""
    from teleport.core import pairing as core_pairing
    from teleport.net import connection

    for name in (
        "MESSAGE_TIMEOUT",
        "NONCE_TIMEOUT",
        "CONFIRM_TIMEOUT",
        "CONFIRM_GRACE",
        "BACKOFF_BASE",
        "BACKOFF_MAX",
    ):
        monkeypatch.setattr(core_pairing, name, getattr(FAST, name))
    for name in ("OPENING_TIMEOUT", "CLOSE_TIMEOUT", "DRAIN_TIMEOUT", "IDLE_TIMEOUT"):
        monkeypatch.setattr(connection, name, getattr(FAST, name))
    try:
        from teleport.core import transfer as core_transfer
    except ImportError:
        core_transfer = None
    if core_transfer is not None:
        for name in ("RECONNECT_GRACE", "DATA_OPEN_WAIT", "DATA_RETRY_BASE", "DATA_RETRY_MAX"):
            monkeypatch.setattr(core_transfer, name, getattr(FAST, name))
    try:
        from teleport.app import pairing as app_pairing
    except ImportError:
        app_pairing = None
    if app_pairing is not None:
        for name in (
            "SEND_TIMEOUT",
            "BUSY_REPLY_TIMEOUT",
            "ABORT_ON_CANCEL_TIMEOUT",
            "CLOSE_SESSIONS_TIMEOUT",
        ):
            monkeypatch.setattr(app_pairing, name, getattr(FAST, name))
    try:
        from teleport.app import node as app_node
    except ImportError:
        app_node = None
    if app_node is not None:
        monkeypatch.setattr(app_node, "LOCK_TIMEOUT", FAST.LOCK_TIMEOUT)
        monkeypatch.setattr(app_node, "NETWORK_CHECK_INTERVAL", FAST.NETWORK_CHECK_INTERVAL, raising=False)
    try:
        from teleport.core import links
    except ImportError:
        links = None
    if links is not None:
        for name in ("RETRY_BASE", "RETRY_MAX", "EVENT_RETRY_GAP", "REPLACE_AFTER"):
            monkeypatch.setattr(links, name, getattr(FAST, name))
    return FAST


@pytest.fixture
def fast_cli(monkeypatch):
    """Như `fast` nhưng chỉ phần ghép đôi/kết nối/khóa (đặc tả PR-3 mục 4.1); trước khi tạo Node."""
    from teleport.app import node as app_node
    from teleport.app import pairing as app_pairing
    from teleport.core import pairing as core_pairing
    from teleport.net import connection

    values = {
        core_pairing: {
            "MESSAGE_TIMEOUT": 1.0,
            "NONCE_TIMEOUT": 0.5,
            "CONFIRM_TIMEOUT": 2.0,
            "CONFIRM_GRACE": 0.5,
            "BACKOFF_BASE": 0.5,
            "BACKOFF_MAX": 2.0,
        },
        connection: {"OPENING_TIMEOUT": 2.0, "CLOSE_TIMEOUT": 0.5, "DRAIN_TIMEOUT": 1.0},
        app_pairing: {
            "SEND_TIMEOUT": 1.0,
            "BUSY_REPLY_TIMEOUT": 0.5,
            "ABORT_ON_CANCEL_TIMEOUT": 0.5,
            "CLOSE_SESSIONS_TIMEOUT": 1.0,
        },
        app_node: {"LOCK_TIMEOUT": 0.5},
    }
    for module, names in values.items():
        for name, value in names.items():
            monkeypatch.setattr(module, name, value)


# =================================================================================================
# CLI trong tiến trình (`run_listen`, `run_pair`, `run_send`, `run_receive`)
# =================================================================================================


@pytest.fixture
async def cli_no_leaks():
    """Không còn task nào của test hay của Teleport sau khi test xong."""
    before = asyncio.all_tasks()
    yield
    await asyncio.sleep(0.05)
    me = asyncio.current_task()
    left = [t for t in asyncio.all_tasks() if t is not me and t not in before and not t.done()]
    assert not left, [t.get_name() for t in left]


class CliRun:
    """Một lần chạy `run_listen` / `run_pair` / `run_send` / `run_receive` trong tiến trình.

    Mỗi lần chạy có `Lines` và đầu ra riêng. `send`/`receive` (P2-7) nhận thêm `interrupts`
    (`teleport.cli.Interrupts`): `press()` giả lập một lần Ctrl+C. `out` thay được (ví dụ một
    `StringIO` có `isatty()` True để giả TTY).
    """

    def __init__(self, coro_fn, argv: list[str], *, interrupts=None, out=None) -> None:
        from teleport.cli import build_parser

        self.args = build_parser().parse_args(argv)
        self.lines: asyncio.Queue = asyncio.Queue()
        self.out = io.StringIO() if out is None else out
        self.err = io.StringIO()
        self.interrupts = interrupts
        kwargs = {} if interrupts is None else {"interrupts": interrupts}
        self.task = asyncio.create_task(
            coro_fn(self.args, self.lines, self.out, self.err, **kwargs), name="test.cli"
        )

    def say(self, text: str | None, ts: float | None = None) -> None:
        # Hợp đồng `Lines` (sửa F2): mốc đọc là `time.perf_counter()`.
        self.lines.put_nowait((time.perf_counter() if ts is None else ts, text))

    def press(self) -> None:
        """Một lần Ctrl+C (chỉ `send`/`receive`)."""
        assert self.interrupts is not None, "lần chạy này không có Interrupts"
        self.interrupts.press()

    def text(self) -> str:
        return self.out.getvalue() + self.err.getvalue()

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.out.getvalue().splitlines() if line.strip()]

    def events_of(self, name: str) -> list[dict]:
        return [e for e in self.events() if e.get("event") == name]

    async def wait_event(self, name: str, n: int = 1, timeout: float = 8.0) -> dict:
        deadline = time.monotonic() + timeout
        while len(self.events_of(name)) < n:
            if self.task.done():
                raise AssertionError(f"lệnh đã dừng trước khi có {name}: {self.text()!r}")
            if time.monotonic() > deadline:
                raise AssertionError(f"hết {timeout} s chưa có {name}: {self.text()!r}")
            await asyncio.sleep(0.01)
        return self.events_of(name)[n - 1]

    async def wait_text(self, needle: str, n: int = 1, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while self.text().count(needle) < n:
            if self.task.done():
                raise AssertionError(f"lệnh đã dừng mà chưa in {needle!r}: {self.text()!r}")
            if time.monotonic() > deadline:
                raise AssertionError(f"hết {timeout} s chưa in {needle!r}: {self.text()!r}")
            await asyncio.sleep(0.01)

    async def code(self, timeout: float = 15.0) -> int:
        """Chờ lệnh tự trả về; trả mã thoát."""
        done, _ = await asyncio.wait({self.task}, timeout=timeout)
        assert done, f"lệnh chưa trả về sau {timeout} s: {self.text()!r}"
        return self.task.result()

    async def stop(self) -> None:
        if self.task.done():
            with contextlib.suppress(BaseException):
                self.task.result()
            return
        if self.interrupts is not None:
            # Dừng như người dùng: Ctrl+C, rồi Ctrl+C lần hai nếu lệnh không tự trả về.
            for wait in (15.0, 5.0):
                with contextlib.suppress(BaseException):
                    self.interrupts.press()
                await asyncio.wait({self.task}, timeout=wait)
                if self.task.done():
                    break
        if not self.task.done():
            self.task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(self.task, 15)


@pytest.fixture
async def cli_runs():
    """`start(kind, argv, *, out=None)` → `CliRun`; `kind` là listen/pair/send/receive.

    `send`/`receive` luôn chạy với một `teleport.cli.Interrupts` mới (`run.press()`). Mọi lần chạy
    được dừng khi test xong (send/receive: Ctrl+C như người dùng; listen/pair: hủy task).
    """
    runs: list[CliRun] = []

    def start(kind: str, argv: list[str], *, out=None) -> CliRun:
        from teleport import cli

        if kind in ("listen", "pair"):
            run = CliRun(cli.run_listen if kind == "listen" else cli.run_pair, argv, out=out)
        elif kind in ("send", "receive"):
            fn = cli.run_send if kind == "send" else cli.run_receive
            run = CliRun(fn, argv, interrupts=cli.Interrupts(), out=out)
        else:
            raise ValueError(f"kind lạ: {kind!r}")
        runs.append(run)
        return run

    yield start

    for run in reversed(runs):
        await run.stop()


# =================================================================================================
# CLI bằng tiến trình con
# =================================================================================================


class Proc:
    """`python -m teleport …` thật; stdout đọc theo dòng trong thread; luôn bị dừng khi xong."""

    def __init__(self, argv: list[str], *, env: dict | None = None) -> None:
        from teleport.paths import ENV_HOME

        full_env = dict(os.environ)
        full_env.pop(ENV_HOME, None)
        full_env.update(env or {})
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.p = subprocess.Popen(
            [sys.executable, "-m", "teleport", *argv],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=flags,
            env=full_env,
        )
        self.lines: queue.Queue = queue.Queue()
        self.out: list[str] = []
        self.err = bytearray()
        self._t_out = threading.Thread(target=self._read_out, daemon=True)
        self._t_err = threading.Thread(target=self._read_err, daemon=True)
        self._t_out.start()
        self._t_err.start()

    def _read_out(self) -> None:
        with contextlib.suppress(Exception):
            for raw in self.p.stdout:
                self.lines.put(raw.decode("utf-8", "replace").rstrip("\r\n"))

    def _read_err(self) -> None:
        with contextlib.suppress(Exception):
            for raw in self.p.stderr:
                self.err += raw

    def err_text(self) -> str:
        return self.err.decode("utf-8", "replace")

    def wait_json(self, event: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"hết {timeout} s chưa có {event}: {self.out!r} {self.err_text()}")
            try:
                line = self.lines.get(timeout=min(left, 0.5))
            except queue.Empty:
                if self.p.poll() is not None and self.lines.empty():
                    detail = f"{self.out!r} {self.err_text()}"
                    raise AssertionError(
                        f"tiến trình đã thoát ({self.p.returncode}) trước {event}: {detail}"
                    ) from None
                continue
            self.out.append(line)
            with contextlib.suppress(ValueError):
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("event") == event:
                    return obj

    def events(self) -> list[dict]:
        """Các dòng JSON đã đọc (gọi sau `finish` để có đủ)."""
        found = []
        for line in self.out:
            with contextlib.suppress(ValueError):
                obj = json.loads(line)
                if isinstance(obj, dict):
                    found.append(obj)
        return found

    def write(self, text: str) -> None:
        self.p.stdin.write(text.encode("utf-8"))
        self.p.stdin.flush()

    def interrupt(self) -> None:
        if sys.platform == "win32":
            os.kill(self.p.pid, signal.CTRL_BREAK_EVENT)
        else:
            self.p.send_signal(signal.SIGINT)

    def finish(self, timeout: float = 30.0) -> int:
        code = self.p.wait(timeout)
        self._t_out.join(5)
        self._t_err.join(5)
        while not self.lines.empty():
            self.out.append(self.lines.get())
        return code

    def kill(self) -> None:
        if self.p.poll() is None:
            self.p.kill()
            with contextlib.suppress(Exception):
                self.p.wait(10)
        for stream in (self.p.stdin, self.p.stdout, self.p.stderr):
            with contextlib.suppress(Exception):
                stream.close()
        self._t_out.join(5)
        self._t_err.join(5)


@pytest.fixture
def procs():
    """`start(argv, *, env=None)` → `Proc`; mọi tiến trình bị giết khi test xong."""
    started: list[Proc] = []

    def start(argv: list[str], *, env: dict | None = None) -> Proc:
        proc = Proc(argv, env=env)
        started.append(proc)
        return proc

    yield start

    for proc in started:
        proc.kill()


# =================================================================================================
# mDNS giả cho CLI
# =================================================================================================


class CliFakeMdnsControl:
    def __init__(self) -> None:
        self.instances: list = []
        self.fail_start: BaseException | None = None
        self.fail_advertise: BaseException | None = None
        self.seed: dict = {}

    @property
    def last(self):
        assert self.instances, "chưa có MdnsDiscovery nào được tạo"
        return self.instances[-1]


class CliFakeMdns:
    """Bản giả `MdnsDiscovery` (như `FakeMdns` của tests/app): không socket; thiết bị trong `seed`
    được báo `DeviceAdded` ngay sau `start_browsing`."""

    def __init__(self, ctl: CliFakeMdnsControl, **kwargs) -> None:
        self.ctl = ctl
        self.kwargs = kwargs
        self.calls: list[str] = []
        self.advertised: list[dict] = []
        self.closing = False
        self.on_event = None
        self._devices: dict = {}
        ctl.instances.append(self)

    async def start(self) -> None:
        self.calls.append("start")
        if self.ctl.fail_start is not None:
            raise self.ctl.fail_start

    async def close(self) -> None:
        self.calls.append("close")
        self.closing = True

    async def advertise(self, **kw) -> None:
        self.calls.append("advertise")
        if self.ctl.fail_advertise is not None:
            raise self.ctl.fail_advertise
        self.advertised.append(kw)

    async def stop_advertising(self) -> None:
        self.calls.append("stop_advertising")

    async def start_browsing(self, *, self_device_id: str, on_event, max_services: int = 256) -> None:
        from teleport.core.discovery import DeviceAdded

        self.calls.append("start_browsing")
        self.on_event = on_event
        loop = asyncio.get_running_loop()
        for d in self.ctl.seed.values():
            if d.device_id != self_device_id:
                loop.call_soon(self.emit, DeviceAdded(d))

    def set_priority(self, device_ids) -> None:
        self.calls.append("set_priority")
        list(device_ids)

    @property
    def devices(self):
        return dict(self._devices)

    def emit(self, event) -> None:
        from teleport.core.discovery import DeviceRemoved

        if self.closing:
            return
        if isinstance(event, DeviceRemoved):
            self._devices.pop(event.device.device_id, None)
        else:
            self._devices[event.device.device_id] = event.device
        if self.on_event is not None:
            self.on_event(event)


@pytest.fixture
def cli_mdns(monkeypatch):
    """Thay `teleport.app.node.MdnsDiscovery`; địa chỉ card mạng cố định (không khởi động lại mDNS)."""
    from teleport.app import node as node_mod
    from teleport.net import discovery as net_discovery

    ctl = CliFakeMdnsControl()
    monkeypatch.setattr(node_mod, "MdnsDiscovery", lambda **kw: CliFakeMdns(ctl, **kw))
    monkeypatch.setattr(net_discovery, "local_ipv4_addresses", lambda: ("192.0.2.5",))
    return ctl
