"""Giao diện dòng lệnh: `teleport <lệnh>`.

Mỗi lệnh là một hàm `cmd_<tên>(args) -> int` (mã thoát). Lệnh mới được thêm
theo từng giai đoạn: `list`, `send`, `receive`.

Mã thoát: 0 thành công; 1 lỗi lúc chạy (khóa bận, danh tính/kho hỏng, không mở được
cổng, ghép đôi không thành, không tìm thấy thiết bị); 2 cú pháp lệnh sai (argparse,
địa chỉ sai dạng, `--name` không hợp lệ).

`listen` và `pair` chạy một node trong tiến trình này (tới v0.3) và hỏi người dùng qua
stdin. stdin được đọc bằng `os.read` trong một thread daemon, không dùng `input()`:
thread bị chặn trong bộ đệm của `sys.stdin` có thể làm Python báo “Fatal Python error”
lúc thoát. Mỗi dòng mang mốc `time.perf_counter()` lúc đọc; dòng gõ trước lúc hiện mã
bị bỏ, nên gõ sẵn `c` không xác nhận được một mã chưa thấy. Dùng `perf_counter` chứ
không dùng `monotonic`: trên Windows `monotonic` chỉ chính xác tới ~15,6 ms, nên một
dòng đọc trước lúc hiện mã vài ms vẫn có thể mang cùng mốc.

`--json`: `listen` / `pair` in JSON Lines (mỗi sự kiện một dòng), các lệnh khác in một
object. Khóa tiếng Anh, có `time` UTC.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import ipaddress
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from teleport import __version__
from teleport.app.devices import AmbiguousDevice, DeviceNotFound, parse_manual_address
from teleport.app.node import ConfigDirError, Node, unpair_offline
from teleport.app.pairing import PairingFinished, PairingOutcome, PairingPrompt
from teleport.core.messages import MessageError
from teleport.core.pairing_code import format_code
from teleport.lock import NodeLockedError
from teleport.net.connection import DEFAULT_PORT
from teleport.paths import ENV_HOME, config_dir
from teleport.trust.identity import IdentityError, format_fingerprint, load_or_create_identity
from teleport.trust.store import TrustedDevice, TrustStoreError

# (time.perf_counter() lúc đọc, dòng không kèm "\n"); dòng None = EOF
Lines = asyncio.Queue[tuple[float, str | None]]

_ANY_HOST = "0.0.0.0"
_MAX_QUEUED_LINES = 100  # hàng đợi stdin đầy thì bỏ dòng mới (người dùng gõ dồn)
_MAX_LINE_BYTES = 4096  # dòng dài hơn bị bỏ (không giữ trong RAM)
# Console Windows: Ctrl+C làm lần đọc stdin đang chờ trả 0 byte, trông như EOF. Chờ một
# chút trước khi báo EOF để lệnh hủy (Ctrl+C) tới trước, thay vì bị coi là “từ chối”.
_TTY_EOF_DELAY = 0.3
_YES = frozenset({"c", "co", "y", "yes"})
_NO = frozenset({"k", "khong", "n", "no"})

_PAIRED = PairingOutcome.PAIRED
_UNFINISHED = frozenset(
    {
        PairingOutcome.USER_REJECTED,
        PairingOutcome.TIMEOUT,
        PairingOutcome.PEER_REJECTED,
        PairingOutcome.PEER_TIMEOUT,
        PairingOutcome.PROTOCOL_ERROR,
        PairingOutcome.PEER_INVALID,
        PairingOutcome.PEER_ABORTED,
        PairingOutcome.CONNECTION_LOST,
    }
)


def cmd_identity(args: argparse.Namespace) -> int:
    directory = args.config_dir or config_dir()
    identity = load_or_create_identity(directory)
    print(f"Mã thiết bị:   {identity.device_id}")
    print(f"Pin ngắn:      {identity.short_fingerprint}")
    print(f"Fingerprint:   {format_fingerprint(identity.fingerprint)}")
    print(f"Thư mục:       {directory}")
    return 0


# ---- Đầu ra --------------------------------------------------------------------------------


def _utc_now() -> str:
    """Thời điểm hiện tại dạng `YYYY-MM-DDTHH:MM:SS.mmmZ` (UTC)."""
    now = datetime.datetime.now(datetime.UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class _Output:
    """In ra `out` / `err` theo chế độ text hoặc JSON; mọi dòng đều flush ngay."""

    def __init__(self, out: TextIO, err: TextIO, json_mode: bool) -> None:
        self.out = out
        self.err = err
        self.json = json_mode
        self._line_open = False  # vừa in “[c/k]: ” và chưa xuống dòng

    def text(self, *lines: str) -> None:
        """Dòng cho người đọc (chỉ ở chế độ text)."""
        if self.json:
            return
        self._close_line()
        for line in lines:
            print(line, file=self.out, flush=True)

    def ask(self, question: str) -> None:
        """In câu hỏi, để con trỏ ở cuối dòng."""
        if self.json:
            return
        self._close_line()
        print(question, end="", file=self.out, flush=True)
        self._line_open = True

    def answered(self) -> None:
        """Người dùng đã gõ Enter: terminal đã xuống dòng."""
        self._line_open = False

    def event(self, kind: str, /, **fields: Any) -> None:
        """Một dòng JSON (chỉ ở chế độ `--json`)."""
        if not self.json:
            return
        obj = {"event": kind, "time": _utc_now(), **fields}
        print(json.dumps(obj, ensure_ascii=False), file=self.out, flush=True)

    def error(self, code: str, message: str, *, extra: Sequence[str] = (), **fields: Any) -> None:
        """Lỗi: text → “Lỗi: …” ra `err`; JSON → object `error` ra `out`."""
        if self.json:
            self.event("error", error=code, message=message, **fields)
            return
        self._close_line()
        print(f"Lỗi: {message}", file=self.err, flush=True)
        for line in extra:
            print(line, file=self.err, flush=True)

    def _close_line(self) -> None:
        if self._line_open:
            print(file=self.out, flush=True)
            self._line_open = False


def _target(result: PairingFinished) -> str:
    return result.address if result.port is None else f"{result.address}:{result.port}"


def outcome_message(result: PairingFinished) -> list[str]:
    """Câu báo kết quả ghép đôi (docs/mvp.md mục 8); dòng đầu là câu chính."""
    outcome = result.outcome
    target = _target(result)
    x = result.peer_name or target
    if outcome is _PAIRED:
        return [f"Đã ghép đôi với {x}."]
    if outcome in _UNFINISHED:
        return [f"Ghép đôi với {x} chưa xong. Nếu mã trên hai máy khác nhau, có thể có người đang giả mạo."]
    if outcome is PairingOutcome.IDENTITY_CONFLICT:
        known = result.known_name or x
        short_id = (result.peer_device_id or "")[:8]
        return [
            f"{known} đang dùng một danh tính khác với lúc ghép đôi. Có thể có người đang giả mạo máy này. "
            "Kết nối đã bị chặn.",
            "Nếu chắc chắn máy đó đã đổi danh tính, hãy hủy ghép đôi mục cũ trước: "
            f"`teleport unpair {short_id}`",
        ]
    if outcome in (PairingOutcome.PEER_BUSY, PairingOutcome.REJECTED):
        return [
            f"{x} chưa sẵn sàng ghép đôi: máy đó đang ghép đôi với máy khác, chưa mở chế độ chờ ghép đôi, "
            "hoặc cần chờ một lát sau lần thử trước. Hãy thử lại sau.",
            "Trên máy đó, hãy chạy: `teleport listen`",
        ]
    if outcome is PairingOutcome.UNREACHABLE:
        return [
            f"Không kết nối được tới {target}. Hãy kiểm tra Teleport đang chạy trên máy đó, "
            "hai máy dùng chung một mạng, và tường lửa cho phép Teleport."
        ]
    if outcome is PairingOutcome.IDENTITY_MISMATCH:
        return [
            f"Thiết bị ở {target} gửi danh tính không hợp lệ nên kết nối đã bị chặn. "
            "Có thể có người đang giả mạo."
        ]
    if outcome is PairingOutcome.VERSION_MISMATCH:
        return [f"{target} đang dùng phiên bản Teleport khác. Hãy cập nhật cả hai máy lên cùng phiên bản."]
    if outcome is PairingOutcome.SELF:
        return ["Đó là chính máy này. Hãy nhập địa chỉ của máy kia."]
    if outcome is PairingOutcome.BUSY:
        return ["Máy này đang có một phiên ghép đôi khác. Hãy thử lại sau."]
    if outcome is PairingOutcome.SAVE_FAILED:
        return [
            f"Đã xác nhận ghép đôi với {x} nhưng không lưu được vào trusted.json. "
            "Hãy kiểm tra quyền ghi thư mục cấu hình rồi ghép đôi lại."
        ]
    return ["Đã hủy ghép đôi."]  # CANCELLED


def _emit_result(output: _Output, result: PairingFinished) -> None:
    lines = outcome_message(result)
    output.text(*lines)
    output.event(
        "pair_result",
        session_id=result.session_id,
        role=result.role.value,
        outcome=result.outcome.value,
        peer_name=result.peer_name,
        peer_device_id=result.peer_device_id,
        address=result.address,
        message=" ".join(lines),
    )


# ---- Hỏi người dùng ------------------------------------------------------------------------


class _Prompter:
    """Hiện lời hỏi ghép đôi và chuyển câu trả lời từ stdin tới `PairingService.decide`."""

    def __init__(self, output: _Output, *, listening: bool) -> None:
        self._output = output
        self._listening = listening  # listen: in mọi kết quả; pair: run_pair tự in kết quả
        self.node: Node | None = None
        self._prompt: PairingPrompt | None = None
        self._shown_at = 0.0
        self._stdin_closed = False
        self.stopping = False  # listen: node đang đóng (Ctrl+C), không mời chờ tiếp

    def on_prompt(self, prompt: PairingPrompt) -> None:
        self._prompt = prompt
        self._shown_at = time.perf_counter()  # cùng đồng hồ với mốc của StdinReader
        out = self._output
        where = f"({prompt.address}, mã máy {prompt.peer_device_id[:8]})"
        if self._listening:
            first = f"Yêu cầu ghép đôi từ “{prompt.peer_name}” {where}."
        else:
            first = f"Ghép đôi với “{prompt.peer_name}” {where}."
        code = format_code(prompt.code)
        out.text(first, f"Kiểm tra mã này trùng với màn hình của {prompt.peer_name}:   {code}")
        out.ask("Trùng khớp? [c/k]: ")
        out.event(
            "pair_prompt",
            session_id=prompt.session_id,
            role=prompt.role.value,
            peer_name=prompt.peer_name,
            peer_device_id=prompt.peer_device_id,
            address=prompt.address,
            code=prompt.code,
            expires_in=round(max(0.0, prompt.expires_at - asyncio.get_running_loop().time()), 3),
        )
        if self._stdin_closed:
            self._decide(False)

    def on_finished(self, result: PairingFinished) -> None:
        if self._prompt is not None and self._prompt.session_id == result.session_id:
            self._prompt = None  # lời hỏi tự hủy (hết giờ, máy kia hủy…)
        if not self._listening:
            return
        _emit_result(self._output, result)
        if not self.stopping and result.outcome is not PairingOutcome.CANCELLED:
            self._output.text("Tiếp tục chờ ghép đôi… (Ctrl+C để dừng)")

    def feed(self, read_at: float, line: str | None) -> None:
        """Một dòng từ stdin (`None` = EOF)."""
        if line is None:
            self._stdin_closed = True
            if self._prompt is not None:
                self._decide(False)
            return
        if self._prompt is None or read_at < self._shown_at:
            return  # không có lời hỏi, hoặc dòng gõ trước lúc hiện mã
        self._output.answered()
        answer = line.strip().casefold()
        if answer.isascii() and answer in _YES:
            if self._decide(True):
                self._output.text("Đang chờ máy kia xác nhận…")
        elif answer.isascii() and answer in _NO:
            self._decide(False)
        else:
            self._output.text("Hãy gõ c (trùng) hoặc k (không trùng).")
            self._output.ask("Trùng khớp? [c/k]: ")

    def _decide(self, accepted: bool) -> bool:
        prompt, self._prompt = self._prompt, None
        if prompt is None or self.node is None:
            return False
        return self.node.pairing.decide(prompt.session_id, accepted)


async def _feed_lines(lines: Lines, prompter: _Prompter) -> None:
    while True:
        read_at, line = await lines.get()
        prompter.feed(read_at, line)


# ---- Khởi động node ------------------------------------------------------------------------


def _config(args: argparse.Namespace) -> Path:
    return getattr(args, "config_dir", None) or config_dir()


def _valid_host(host: object) -> bool:
    if not isinstance(host, str):
        return False
    try:
        return str(ipaddress.IPv4Address(host)) == host
    except ValueError:
        return False


def _make_node(
    args: argparse.Namespace, output: _Output, prompter: _Prompter, *, accept_pairing: bool
) -> Node | int:
    """Tạo `Node` từ tham số dòng lệnh; tham số sai → mã thoát 2 (đã in lỗi)."""
    host = getattr(args, "host", _ANY_HOST)
    if not _valid_host(host):
        output.error("invalid_address", f"Địa chỉ lắng nghe không hợp lệ: {host!r}")
        return 2
    try:
        node = Node(
            _config(args),
            name=getattr(args, "name", None),
            port=getattr(args, "port", None),
            host=host,
            accept_pairing=accept_pairing,
            on_pairing_prompt=prompter.on_prompt,
            on_pairing_finished=prompter.on_finished,
        )
    except MessageError as e:
        output.error("invalid_name", f"Tên thiết bị không hợp lệ: {e}")
        return 2
    except ValueError as e:
        output.error("invalid_address", f"Cổng không hợp lệ: {e}")
        return 2
    prompter.node = node
    return node


async def _start(node: Node, output: _Output) -> int | None:
    """`node.start()`; lỗi → in lỗi, trả mã thoát. Thành công → None."""
    try:
        await node.start()
    except NodeLockedError:
        output.error("node_locked", "Teleport đang chạy với thư mục cấu hình này.")
        return 1
    except IdentityError as e:
        output.error("identity_error", str(e))
        return 1
    except TrustStoreError as e:
        output.error("trust_store_error", str(e))
        return 1
    except ConfigDirError as e:
        _config_dir_error(output, e.path, e.error)
        return 1
    except OSError as e:  # Node.start: OSError còn lại chỉ đến từ cổng lắng nghe
        output.error("port_unavailable", f"Không mở được cổng để lắng nghe: {e}")
        return 1
    return None


def _config_dir_error(output: _Output, path: Path, error: OSError) -> None:
    output.error("config_dir_error", f"Không dùng được thư mục cấu hình {path}: {error}")


# ---- listen --------------------------------------------------------------------------------


async def run_listen(args: argparse.Namespace, lines: Lines, out: TextIO, err: TextIO) -> int:
    """`teleport listen`: chờ ghép đôi tới khi bị hủy; chỉ trả về khi lỗi khởi động."""
    output = _Output(out, err, bool(getattr(args, "json", False)))
    prompter = _Prompter(output, listening=True)
    node = _make_node(args, output, prompter, accept_pairing=True)
    if isinstance(node, int):
        return node
    code = await _start(node, output)
    if code is not None:
        return code
    try:
        addresses = await node.local_addresses()
        port = node.port
        assert port is not None
        output.event(
            "listening",
            name=node.name,
            device_id=node.identity.device_id,
            port=port,
            addresses=list(addresses),
        )
        suffix = "" if port == DEFAULT_PORT else f":{port}"
        target = f"{addresses[0]}{suffix}" if addresses else f"<địa chỉ IP của máy này>{suffix}"
        output.text(
            "Teleport đang chờ ghép đôi.",
            f"  Tên:      {node.name}",
            f"  Mã máy:   {node.identity.device_id[:8]}",
            f"  Cổng:     {port}",
            f"  Địa chỉ:  {', '.join(addresses) if addresses else '(không tìm thấy địa chỉ IPv4 nào)'}",
            f"Trên máy kia, chạy: teleport pair {target}",
            "Nhấn Ctrl+C để dừng.",
        )
        await _feed_lines(lines, prompter)
        return 0  # không tới đây: _feed_lines chạy tới khi bị hủy
    finally:
        prompter.stopping = True
        await node.close()


# ---- pair ----------------------------------------------------------------------------------

_PAIR_ADDRESS_HELP = "`teleport pair` hiện chỉ nhận địa chỉ IP, ví dụ 192.168.1.5 hoặc 192.168.1.5:47474."


async def run_pair(args: argparse.Namespace, lines: Lines, out: TextIO, err: TextIO) -> int:
    """`teleport pair <ip[:cổng]>`: ghép đôi với một máy; 0 nếu đã ghép đôi, ngược lại 1."""
    output = _Output(out, err, bool(getattr(args, "json", False)))
    try:
        ip, port = parse_manual_address(args.address)
    except ValueError:
        output.error("invalid_address", _PAIR_ADDRESS_HELP)
        return 2
    prompter = _Prompter(output, listening=False)
    node = _make_node(args, output, prompter, accept_pairing=False)
    if isinstance(node, int):
        return node
    code = await _start(node, output)
    if code is not None:
        return code
    try:
        output.text(f"Đang kết nối tới {ip}:{port}…")
        feeder = asyncio.create_task(_feed_lines(lines, prompter), name="teleport.cli.stdin")
        try:
            result = await node.pairing.pair_with([ip], port)
        finally:
            feeder.cancel()
            await asyncio.wait({feeder})
        _emit_result(output, result)
        return 0 if result.outcome is _PAIRED else 1
    finally:
        await node.close()


# ---- Chạy lệnh async -----------------------------------------------------------------------


class StdinReader:
    """Thread daemon tên "teleport-stdin": os.read(fd, 1024), tách dòng, giải mã UTF-8 errors="replace",
    đưa (time.perf_counter(), dòng) vào `lines` qua loop.call_soon_threadsafe. EOF/lỗi → (ts, None) rồi dừng.
    Hàng đợi đã có ≥ 100 mục thì bỏ dòng mới. Loop đã đóng (RuntimeError) → thread dừng.
    stdin là None hoặc không có fileno → đưa (ts, None) ngay."""

    def __init__(self, loop: asyncio.AbstractEventLoop, lines: Lines) -> None:
        self._loop = loop
        self._lines = lines

    def start(self) -> None:
        try:
            fd = sys.stdin.fileno() if sys.stdin is not None else None
        except (AttributeError, OSError, ValueError):  # pytest thay stdin bằng object không có fileno
            fd = None
        if fd is None:
            self._post(None)
            return
        threading.Thread(target=self._run, args=(fd,), name="teleport-stdin", daemon=True).start()

    def _run(self, fd: int) -> None:
        buf = bytearray()
        skipping = False  # đang bỏ phần còn lại của một dòng quá dài
        while True:
            try:
                chunk = os.read(fd, 1024)
            except OSError:
                chunk = b""
            if not chunk:
                if buf and not skipping:
                    self._post(bytes(buf))
                if _isatty(fd):
                    time.sleep(_TTY_EOF_DELAY)
                self._post(None)
                return
            buf += chunk
            while (end := buf.find(b"\n")) >= 0:
                line = bytes(buf[:end])
                del buf[: end + 1]
                if skipping:
                    skipping = False
                elif not self._post(line):
                    return
            if len(buf) > _MAX_LINE_BYTES:
                buf.clear()
                skipping = True

    def _post(self, line: bytes | None) -> bool:
        """Đưa một dòng sang loop; False khi loop đã đóng (thread nên dừng)."""
        read_at = time.perf_counter()
        text = None if line is None else line.decode("utf-8", errors="replace").rstrip("\r")
        try:
            self._loop.call_soon_threadsafe(self._put, read_at, text)
        except RuntimeError:
            return False
        return True

    def _put(self, read_at: float, text: str | None) -> None:
        if text is not None and self._lines.qsize() >= _MAX_QUEUED_LINES:
            return  # EOF luôn được đưa vào, để lời hỏi không treo
        self._lines.put_nowait((read_at, text))


def _isatty(fd: int) -> bool:
    try:
        return os.isatty(fd)
    except OSError:
        return False


RunFn = Callable[[argparse.Namespace, Lines, TextIO, TextIO], Awaitable[int]]


async def _main(args: argparse.Namespace, run: RunFn) -> int:
    loop = asyncio.get_running_loop()
    lines: Lines = asyncio.Queue()
    StdinReader(loop, lines).start()
    restore = _handle_ctrl_break(loop, asyncio.current_task())
    try:
        return await run(args, lines, sys.stdout, sys.stderr)
    finally:
        restore()


def _handle_ctrl_break(loop: asyncio.AbstractEventLoop, task: asyncio.Task | None) -> Callable[[], None]:
    """Windows: Ctrl+Break (SIGBREAK) hủy lệnh như Ctrl+C. Trả hàm khôi phục handler cũ."""
    sig = getattr(signal, "SIGBREAK", None)
    if sig is None or task is None:
        return lambda: None

    def handler(signum: int, frame: object) -> None:
        loop.call_soon_threadsafe(task.cancel)

    try:
        previous = signal.signal(sig, handler)
    except (ValueError, OSError):  # không ở thread chính
        return lambda: None
    return lambda: signal.signal(sig, previous)


def _run_async(args: argparse.Namespace, run: RunFn) -> int | None:
    """Chạy lệnh trong event loop riêng; bị hủy (Ctrl+C, Ctrl+Break) → None."""
    with asyncio.Runner() as runner:
        try:
            return runner.run(_main(args, run))
        except (KeyboardInterrupt, asyncio.CancelledError):
            return None


def cmd_listen(args: argparse.Namespace) -> int:
    code = _run_async(args, run_listen)
    if code is not None:
        return code
    output = _Output(sys.stdout, sys.stderr, args.json)
    output.text("Đã dừng.")
    output.event("stopped")
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    code = _run_async(args, run_pair)
    if code is not None:
        return code
    output = _Output(sys.stdout, sys.stderr, args.json)
    message = "Đã hủy ghép đôi."
    output.text(message)
    try:
        address = parse_manual_address(args.address)[0]
    except ValueError:
        address = args.address
    output.event(
        "pair_result",
        session_id=None,
        role="initiator",
        outcome=PairingOutcome.CANCELLED.value,
        peer_name=None,
        peer_device_id=None,
        address=address,
        message=message,
    )
    return 1


# ---- unpair --------------------------------------------------------------------------------


def _candidates(devices: tuple[TrustedDevice, ...]) -> list[dict[str, str]]:
    return [{"device_id": d.device_id, "name": d.name} for d in devices]


def cmd_unpair(args: argparse.Namespace) -> int:
    output = _Output(sys.stdout, sys.stderr, args.json)
    query = args.device
    directory = _config(args)
    try:
        device = unpair_offline(directory, query)
    except OSError as e:
        _config_dir_error(output, directory, e)
        return 1
    except NodeLockedError:
        output.error(
            "node_locked", "Teleport đang chạy với thư mục cấu hình này; hãy dừng `teleport listen` trước."
        )
        return 1
    except TrustStoreError as e:
        output.error("trust_store_error", str(e))
        return 1
    except DeviceNotFound as e:
        if e.known:
            extra = ["Các thiết bị tin cậy:", *(f"  {d.device_id[:8]}  {d.name}" for d in e.known)]
        else:
            extra = ["(chưa ghép đôi với thiết bị nào)"]
        output.error(
            "not_found",
            f"Không có thiết bị tin cậy nào khớp “{query}”.",
            extra=extra,
            candidates=_candidates(e.known),
        )
        return 1
    except AmbiguousDevice as e:
        output.error(
            "ambiguous",
            f"“{query}” khớp nhiều thiết bị; hãy nhập mã thiết bị dài hơn:",
            extra=[f"  {d.device_id}  {d.name}" for d in e.candidates],
            candidates=_candidates(e.candidates),
        )
        return 1
    if output.json:
        obj = {"time": _utc_now(), "removed": True, "device_id": device.device_id, "name": device.name}
        print(json.dumps(obj, ensure_ascii=False), flush=True)
    else:
        print(f"Đã hủy ghép đôi với {device.name} ({device.device_id[:8]}).", flush=True)
    return 0


# ---- Tham số -------------------------------------------------------------------------------


def _port_type(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"cổng phải là số nguyên 0–65535: {text!r}") from None
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"cổng phải là số nguyên 0–65535: {text!r}")
    return value


def _add_node_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="in sự kiện dạng JSON Lines")
    parser.add_argument("--name", help="tên thiết bị cho lần chạy này (mặc định: tên máy)")
    # Ẩn: chỉ để test bind 127.0.0.1 và cổng 0 (tránh hộp thoại tường lửa Windows).
    parser.add_argument("--host", default=_ANY_HOST, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=_port_type, default=None, help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="teleport",
        description="Chuyển file và clipboard trong mạng LAN, không qua cloud.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config-dir",
        type=Path,
        help=f"thư mục cấu hình (mặc định theo hệ điều hành, hoặc biến môi trường {ENV_HOME})",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<lệnh>")
    sub.add_parser("identity", help="hiện mã thiết bị và fingerprint của máy này").set_defaults(
        handler=cmd_identity
    )

    listen = sub.add_parser("listen", help="chờ máy khác ghép đôi với máy này")
    _add_node_options(listen)
    listen.set_defaults(handler=cmd_listen)

    pair = sub.add_parser("pair", help="ghép đôi với máy khác theo địa chỉ IP")
    pair.add_argument("address", metavar="<ip[:cổng]>", help="địa chỉ máy kia, ví dụ 192.168.1.5")
    _add_node_options(pair)
    pair.set_defaults(handler=cmd_pair)

    unpair = sub.add_parser("unpair", help="hủy ghép đôi với một thiết bị (khi Teleport không chạy)")
    unpair.add_argument("device", metavar="<thiết bị>", help="tên hoặc tiền tố mã thiết bị (≥ 4 ký tự)")
    unpair.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    unpair.set_defaults(handler=cmd_unpair)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Ép UTF-8 để tiếng Việt hiển thị đúng cả khi đầu ra bị chuyển hướng (pipe).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except IdentityError as e:
        # Lỗi người dùng tự xử lý được: in thông báo, không in traceback.
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1
