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
- `tcp_proxy` (`TcpProxy`, P3-0; dùng tiếp ở P3-5): proxy TCP chèn giữa hai node để cắt RST, chặn
  (ngừng chuyển byte, không đóng) rồi RST, hay chặn rồi đi tiếp; bộ đệm nhỏ có backpressure. Test của
  chính proxy: `tests/test_tcp_proxy.py`.

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
import socket
import struct
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


# =================================================================================================
# Proxy TCP chèn giữa hai node (P3-0, dùng tiếp ở P3-5): cắt RST, chặn, chặn rồi đi tiếp
# =================================================================================================


def _rst_writer(writer: asyncio.StreamWriter | None) -> None:
    """Đóng kiểu RST, để máy kia thấy reset chứ không phải EOF: `SO_LINGER` = (1, 0), `abort()`, rồi
    đóng ngay socket thật. Bẫy: trên Windows, transport proactor gọi `shutdown(SHUT_RDWR)` (gửi FIN)
    trước khi đóng, nên máy kia đọc được EOF trước reset; đóng socket thật trước lúc đó (fileno thành
    -1) thì transport bỏ qua `shutdown`."""
    if writer is None:
        return
    tsock = writer.get_extra_info("socket")
    raw = getattr(tsock, "_sock", tsock)  # `asyncio.trsock.TransportSocket` bọc socket thật
    if raw is not None:
        with contextlib.suppress(OSError, ValueError):
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    with contextlib.suppress(Exception):
        writer.transport.abort()
    if raw is not None:
        with contextlib.suppress(OSError):
            raw.close()


class _ProxyPair:
    """Một kết nối qua proxy: phía client (đã accept) và phía server (chỉ nối khi không bị chặn)."""

    def __init__(self, c_writer: asyncio.StreamWriter) -> None:
        self.c_writer = c_writer
        self.s_writer: asyncio.StreamWriter | None = None
        self.task: asyncio.Task | None = None

    def rst(self) -> None:
        _rst_writer(self.c_writer)
        _rst_writer(self.s_writer)


class TcpProxy:
    """Proxy TCP `host:port` → `target_host:target_port`, chạy event loop riêng trong một thread daemon,
    nên dùng được cả từ test async (node trong tiến trình) lẫn test đồng bộ (node là tiến trình con).

    Điều khiển (gọi từ thread nào cũng được, có hiệu lực khi trả về):

    - `block()`: mạng “tắt” mà không đóng gì: ngừng chuyển byte cả hai chiều (đoạn đang cầm, tối đa
      `chunk` byte, được giữ lại), không đọc thêm, không chuyển EOF hay reset. Khi `block()` trả về,
      proxy không ghi thêm byte nào; phần đã ghi trước đó (≤ một đoạn mỗi chiều mỗi kết nối trong bộ
      đệm transport, cộng bộ đệm hệ điều hành) vẫn có thể tới nơi. Kết nối mới vẫn được
      accept ở mức TCP nhưng **không** được nối tới server cho tới khi hết chặn (bắt tay TLS treo như
      khi mạng mất thật).
    - `resume()`: chuyển tiếp lại, đúng thứ tự, không mất hay lặp byte (“chặn rồi đi tiếp”). Phía nào
      đã tự đóng trong lúc chặn thì proxy thấy lỗi khi chuyển tiếp lại và RST phía kia.
    - `rst()`: cắt **mọi** kết nối hiện có bằng RST cả hai phía (kể cả kết nối đang treo vì chặn);
      kết nối mới sau đó chạy bình thường nếu không còn chặn. “Chặn rồi RST” = `block()`, chờ, `rst()`
      rồi `resume()` (test async: `await outage(seconds, end="rst")`).

    Bộ đệm nhỏ, có backpressure, để proxy không làm phình lượng byte mất khi cắt (byte nằm trong proxy
    lúc RST là byte bên gửi phải gửi lại): mỗi chiều đọc tối đa `chunk` byte, ghi xong phải `drain()`
    (ngưỡng ghi của transport = `chunk`) mới đọc tiếp; `SO_RCVBUF`/`SO_SNDBUF` của socket proxy đặt
    `sock_buf` byte. EOF một chiều chuyển bằng `write_eof()` (đóng nửa, như TCP thật); lỗi socket ở
    một phía → RST phía kia.

    Đếm: `accepted` (kết nối vào), `forwarded["c2s"]` / `forwarded["s2c"]` (byte đã chuyển), `resets`
    (kết nối bị `rst()` cắt), `live()` (kết nối đang mở).
    """

    def __init__(
        self,
        target_port: int,
        *,
        target_host: str = "127.0.0.1",
        host: str = "127.0.0.1",
        chunk: int = 64 * 1024,
        sock_buf: int = 64 * 1024,
    ) -> None:
        self.target = (target_host, target_port)
        self.host = host
        self.chunk = chunk
        self.sock_buf = sock_buf
        self.port = 0
        self.accepted = 0
        self.resets = 0
        self.forwarded = {"c2s": 0, "s2c": 0}
        self._blocked = False
        self._pairs: set[_ProxyPair] = set()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="test-tcp-proxy", daemon=True)
        self._server: asyncio.Server | None = None
        self._resumed: asyncio.Event | None = None
        self._closed = False

    # ---- vòng đời ----

    def start(self) -> TcpProxy:
        self._thread.start()
        self._run(self._start())
        return self

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._run(self._close(), timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)
        if not self._thread.is_alive():
            self._loop.close()

    def _run(self, coro, timeout: float = 5.0):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _sock(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
            with contextlib.suppress(OSError):
                s.setsockopt(socket.SOL_SOCKET, opt, self.sock_buf)
        return s

    async def _start(self) -> None:
        self._resumed = asyncio.Event()
        self._resumed.set()
        lsock = self._sock()
        lsock.bind((self.host, 0))
        lsock.listen(64)
        lsock.setblocking(False)
        self.port = lsock.getsockname()[1]
        self._server = await asyncio.start_server(self._handle, sock=lsock, limit=self.chunk)

    async def _close(self) -> None:
        if self._server is not None:
            self._server.close()
        self._blocked = False
        if self._resumed is not None:
            self._resumed.set()
        tasks = self._rst_all(count=False)
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        if self._server is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), 5)

    # ---- điều khiển ----

    def block(self) -> None:
        """Chặn. Trả về sau khi loop proxy đã qua một vòng: từ đó proxy không ghi thêm byte nào ra
        bất kỳ phía nào. Byte proxy đã ghi trước đó vẫn có thể tới nơi: tối đa một đoạn (`chunk`) mỗi
        chiều mỗi kết nối còn trong bộ đệm transport, cộng phần đã nằm trong bộ đệm hệ điều hành."""
        self._blocked = True  # pump đọc cờ ở mỗi đoạn; sự kiện được xóa trong loop proxy (`_gate`)
        if self._thread.is_alive() and threading.current_thread() is not self._thread and not self._closed:
            # Đồng bộ với loop proxy: bước nào đang chạy (đã qua `_gate`, sắp ghi) xong trước khi trả về.
            self._run(asyncio.sleep(0))

    def resume(self) -> None:
        self._blocked = False  # đặt cờ trước rồi mới đánh thức (xem `_gate`)
        if self._resumed is not None and not self._closed:
            self._loop.call_soon_threadsafe(self._resumed.set)

    @property
    def blocked(self) -> bool:
        return self._blocked

    def rst(self) -> int:
        """Cắt RST mọi kết nối hiện có (chờ tới khi đã cắt xong); trả số kết nối bị cắt."""

        async def go() -> int:
            n = len(self._pairs)
            tasks = self._rst_all(count=True)
            if tasks:
                await asyncio.wait(tasks, timeout=5)
            return n

        return self._run(go())

    async def outage(self, seconds: float, *, end: str = "rst") -> None:
        """Chặn `seconds` giây (thời gian thật) rồi `end`: `"rst"` (cắt RST, kết nối mới đi bình thường)
        hoặc `"resume"` (đi tiếp). Cho test async; test đồng bộ tự gọi `block()`, `time.sleep`, …"""
        if end not in ("rst", "resume"):
            raise ValueError(end)
        self.block()
        await asyncio.sleep(seconds)
        if end == "rst":
            self.rst()
        self.resume()

    def live(self) -> int:
        return len(self._pairs)

    # ---- trong loop proxy ----

    def _rst_all(self, *, count: bool) -> list[asyncio.Task]:
        tasks = []
        for pair in list(self._pairs):
            pair.rst()
            if count:
                self.resets += 1
            if pair.task is not None and not pair.task.done():
                pair.task.cancel()
                tasks.append(pair.task)
        return tasks

    async def _gate(self) -> None:
        while self._blocked:
            assert self._resumed is not None
            # Xóa trong loop proxy rồi mới chờ: `resume()` đặt cờ trước khi hẹn `set()`, nên không mất
            # lần đánh thức, và không quay vòng bận khi sự kiện còn bật từ lần trước.
            self._resumed.clear()
            if not self._blocked:
                break
            await self._resumed.wait()

    async def _pump(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter, direction: str) -> None:
        while True:
            await self._gate()
            data = await src.read(self.chunk)
            if not data:
                await self._gate()  # EOF cũng không đi qua khi mạng “tắt”
                if dst.can_write_eof():
                    dst.write_eof()
                return
            await self._gate()  # bị chặn trong lúc đọc: giữ đoạn này (≤ chunk) tới khi hết chặn
            dst.write(data)
            self.forwarded[direction] += len(data)
            await dst.drain()

    async def _handle(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter) -> None:
        self.accepted += 1
        pair = _ProxyPair(c_writer)
        pair.task = asyncio.current_task()
        self._pairs.add(pair)
        c_writer.transport.set_write_buffer_limits(high=self.chunk)
        pumps: list[asyncio.Task] = []
        clean = False
        try:
            await self._gate()  # mạng đang “tắt”: chưa nối tới server
            sock = self._sock()
            sock.setblocking(False)
            try:
                await asyncio.get_running_loop().sock_connect(sock, self.target)
                s_reader, s_writer = await asyncio.open_connection(sock=sock, limit=self.chunk)
            except OSError:
                sock.close()
                return
            except BaseException:
                sock.close()
                raise
            pair.s_writer = s_writer
            s_writer.transport.set_write_buffer_limits(high=self.chunk)
            pumps = [
                asyncio.create_task(self._pump(c_reader, s_writer, "c2s")),
                asyncio.create_task(self._pump(s_reader, c_writer, "s2c")),
            ]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_EXCEPTION)
            clean = all(t.done() and not t.cancelled() and t.exception() is None for t in pumps)
        except (ConnectionError, OSError):
            pass
        finally:
            for t in pumps:
                t.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            if clean:  # hai chiều đều EOF: đóng thường
                for w in (c_writer, pair.s_writer):
                    if w is not None:
                        with contextlib.suppress(Exception):
                            w.close()
            else:
                pair.rst()
            self._pairs.discard(pair)


@pytest.fixture
def tcp_proxy():
    """`tcp_proxy(target_port, **kw)` → `TcpProxy` đã chạy (cổng ở `.port`); mọi proxy được đóng khi
    test xong. Dùng được trong test async lẫn đồng bộ (proxy chạy event loop riêng)."""
    made: list[TcpProxy] = []

    def make(target_port: int, **kwargs) -> TcpProxy:
        proxy = TcpProxy(target_port, **kwargs).start()
        made.append(proxy)
        return proxy

    yield make
    for proxy in reversed(made):
        proxy.close()
