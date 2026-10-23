"""Giao diện dòng lệnh: `teleport <lệnh>`.

Mỗi lệnh là một hàm `cmd_<tên>(args) -> int` (mã thoát). Lệnh mới được thêm
theo từng giai đoạn: `send`, `receive`.

Mã thoát: 0 thành công; 1 lỗi lúc chạy (khóa bận, danh tính/kho hỏng, không mở được
cổng, không bật được mDNS, ghép đôi không thành, không tìm thấy thiết bị); 2 cú pháp
lệnh sai (argparse, đích trông như địa chỉ mà sai dạng, `--name` không hợp lệ).

`list` chỉ duyệt mDNS vài giây: không giữ `node.lock`, không quảng bá máy này.
`listen` quảng bá và duyệt mDNS, tự giữ kết nối với các máy đã ghép đôi và in sự kiện
thiết bị. `pair <tên>` duyệt mDNS (không quảng bá, không tự kết nối) rồi chọn đúng một
máy; `pair <ip>` không dùng mDNS. mDNS bật khi nghe trên mọi địa chỉ (mặc định), hoặc
khi có cờ ẩn `--mdns-interface` (test).

`listen` và `pair` chạy một node trong tiến trình này (tới v0.3) và hỏi người dùng qua
stdin. stdin được đọc bằng `os.read` trong một thread daemon, không dùng `input()`:
thread bị chặn trong bộ đệm của `sys.stdin` có thể làm Python báo “Fatal Python error”
lúc thoát. Mỗi dòng mang mốc `time.perf_counter()` lúc đọc; dòng gõ trước lúc hiện mã
bị bỏ, nên gõ sẵn `c` không xác nhận được một mã chưa thấy. Dùng `perf_counter` chứ
không dùng `monotonic`: trên Windows `monotonic` chỉ chính xác tới ~15,6 ms, nên một
dòng đọc trước lúc hiện mã vài ms vẫn có thể mang cùng mốc.

Lời hỏi ghép đôi theo bố cục docs/mvp.md mục 7.2: dòng mã chỉ gồm nhãn cố định và mã, đứng
trước mọi chữ do máy kia đặt; tên máy kia (chưa xác thực) hiện qua `display_name`, trong “…”.

`pair` bị Ctrl+C khi kết quả đã có (ví dụ lúc đang đóng kết nối sau khi đã ghép đôi) thì
vẫn báo đúng kết quả đó và trả mã thoát theo nó; chỉ khi chưa có kết quả mới báo đã hủy.

`--json`: `listen` / `pair` in JSON Lines (mỗi sự kiện một dòng), các lệnh khác in một
object. Khóa tiếng Anh, có `time` UTC.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import functools
import ipaddress
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from teleport import __version__
from teleport.app.devices import (
    AmbiguousDevice,
    DeviceNotFound,
    DeviceView,
    parse_manual_address,
    resolve_device,
)
from teleport.app.node import (
    MAX_SCAN_TIMEOUT,
    SCAN_TIMEOUT,
    ConfigDirError,
    ConnectFailed,
    DeviceConnected,
    DeviceDisconnected,
    DeviceOffline,
    DeviceOnline,
    DiscoveryUnavailable,
    MdnsOptions,
    Node,
    NodeEvent,
    scan_devices,
    unpair_offline,
)
from teleport.app.pairing import PairingFinished, PairingOutcome, PairingPrompt
from teleport.app.settings import Settings
from teleport.core import pairing as core_pairing  # WARN_RECENT_CODES đọc lúc dùng
from teleport.core.display import display_name
from teleport.core.links import ConnectResult
from teleport.core.messages import MessageError
from teleport.core.pairing import Role
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
    try:
        identity = load_or_create_identity(directory)
    except OSError as e:  # ví dụ đường dẫn là một file
        _config_dir_error(_Output(sys.stdout, sys.stderr, False), directory, e)
        return 1
    port = Settings.load(directory).port or DEFAULT_PORT
    print(f"Mã thiết bị:   {identity.device_id}")
    print(f"Pin ngắn:      {identity.short_fingerprint}")
    print(f"Fingerprint:   {format_fingerprint(identity.fingerprint)}")
    print(f"Cổng:          {port}")
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


def outcome_message(result: PairingFinished, *, seen_name: str | None = None) -> list[str]:
    """Câu báo kết quả ghép đôi (docs/mvp.md mục 8); dòng đầu là câu chính.

    `seen_name`: tên của đích thấy qua mDNS (`pair <tên>`); khi có, “không kết nối được”
    đổi thành câu “Thấy X nhưng không kết nối được…”.

    Đã ghép đôi: dùng tên vừa lưu (`device.name`, tên máy kia gửi trong `hello`, ở cả hai
    vai, kể cả khi ghép đôi lại). Phiên chưa xong mà chưa hiện mã
    (`code_shown` False) thì không nhắc so mã, câu theo vai. Danh tính khác ở vai trả lời:
    `pair_request` chưa được xác minh nên không gợi ý hủy ghép đôi máy thật.
    """
    outcome = result.outcome
    target = _target(result)
    x = result.peer_name or seen_name or target
    responder = result.role is Role.RESPONDER
    if outcome is PairingOutcome.UNREACHABLE and seen_name is not None:
        return [_unreachable_seen(seen_name), _HOTSPOT_HINT]
    if outcome is _PAIRED:
        name = result.device.name if result.device is not None else x
        return [f"Đã ghép đôi với {name}."]
    if outcome in _UNFINISHED:
        if result.code_shown:
            return [
                f"Ghép đôi với {x} chưa xong. Nếu mã trên hai máy khác nhau, có thể có người đang giả mạo."
            ]
        if responder:
            return [f"Một máy tự xưng là {x} đã yêu cầu ghép đôi nhưng phiên dừng trước khi hiện mã."]
        return [f"Ghép đôi với {x} chưa xong vì phiên dừng trước khi hiện mã. Hãy thử lại."]
    if outcome is PairingOutcome.IDENTITY_CONFLICT:
        known = result.known_name or x
        if responder:
            return [
                f"Một máy chưa xác minh tự xưng là {known} nhưng dùng danh tính khác với lúc ghép đôi. "
                "Có thể có người đang giả mạo máy đó. Yêu cầu ghép đôi đã bị chặn."
            ]
        short_id = (result.peer_device_id or "")[:8]
        return [
            f"{known} đang dùng một danh tính khác với lúc ghép đôi. Có thể có người đang giả mạo máy này. "
            "Kết nối đã bị chặn.",
            "Nếu chắc chắn máy đó đã đổi danh tính, hãy hủy ghép đôi mục cũ trước: "
            f"`teleport unpair {short_id}`",
        ]
    if outcome in (PairingOutcome.PEER_BUSY, PairingOutcome.REJECTED):
        return [
            f"{x} chưa sẵn sàng ghép đôi: máy đó đang ẩn, đang ghép đôi với máy khác, "
            "chưa mở chế độ chờ ghép đôi, hoặc cần chờ một lát sau lần thử trước. Hãy thử lại sau.",
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
    if outcome is PairingOutcome.PROMPT_FAILED:
        return ["Không hiện được mã ghép đôi trên máy này nên đã hủy ghép đôi. Hãy thử lại."]
    return ["Đã hủy ghép đôi."]  # CANCELLED


def _unreachable_seen(name: str) -> str:
    return (
        f"Thấy {name} nhưng không kết nối được. Mạng Wi-Fi này có thể đang chặn các thiết bị nói chuyện "
        "với nhau (hay gặp ở quán cà phê, khách sạn)."
    )


_HOTSPOT_HINT = (
    "Hãy thử dùng điểm phát Wi-Fi từ điện thoại, và kiểm tra tường lửa trên máy kia đã cho phép Teleport."
)
_OS_LABELS = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}


def _os_label(os_name: str | None) -> str:
    if os_name is None:
        return "?"
    return _OS_LABELS.get(os_name, os_name)


def _paired_label(view: DeviceView) -> str:
    return "đã ghép đôi" if view.paired else "chưa ghép đôi"


def _view_dict(view: DeviceView) -> dict[str, Any]:
    return {
        "device_id": view.device_id,
        "name": view.name,
        "os": view.os,
        "paired": view.paired,
        "online": view.online,
        "addresses": list(view.addresses),
        "port": view.port,
    }


def connect_failed_message(view: DeviceView, result: ConnectResult) -> list[str]:
    """Câu báo không kết nối được tới thiết bị đã ghép đôi (docs/mvp.md mục 8)."""
    x = view.name
    short_id = view.device_id[:8]
    if result is ConnectResult.REJECTED:
        return [
            f"{x} từ chối kết nối từ máy này. Có thể máy đó đã hủy ghép đôi.",
            f"Để ghép đôi lại: `teleport pair {short_id}`",
        ]
    if result is ConnectResult.IDENTITY_MISMATCH:
        return [
            f"{x} đang dùng một danh tính khác với lúc ghép đôi. Có thể có người đang giả mạo máy này. "
            "Kết nối đã bị chặn.",
            "Nếu chắc chắn máy đó đã đổi danh tính, hãy hủy ghép đôi mục cũ trước: "
            f"`teleport unpair {short_id}`",
        ]
    if result is ConnectResult.VERSION_MISMATCH:
        return [f"{x} đang dùng phiên bản Teleport khác. Hãy cập nhật cả hai máy lên cùng phiên bản."]
    return [_unreachable_seen(x)]


def _print_event(output: _Output, event: NodeEvent) -> None:
    """In một `NodeEvent` của `teleport listen`."""
    view = event.device
    who = f"“{display_name(view.name)}” ({view.device_id[:8]}"
    if isinstance(event, DeviceOnline):
        where = f"{view.addresses[0]}:{view.port}" if view.addresses else "?"
        output.text(f"Thấy {who}, {_os_label(view.os)}, {_paired_label(view)}) tại {where}.")
        output.event(
            "device_online",
            device_id=view.device_id,
            name=view.name,
            os=view.os,
            paired=view.paired,
            addresses=list(view.addresses),
            port=view.port,
        )
    elif isinstance(event, DeviceOffline):
        output.text(f"{who}) đã rời mạng.")
        output.event("device_offline", device_id=view.device_id, name=view.name)
    elif isinstance(event, DeviceConnected):
        output.text(f"Đã kết nối với {who}).")
        output.event("device_connected", device_id=view.device_id, name=view.name, outgoing=event.outgoing)
    elif isinstance(event, DeviceDisconnected):
        output.text(f"Mất kết nối với {who})." + (" Đang nối lại…" if view.online else ""))
        output.event("device_disconnected", device_id=view.device_id, name=view.name)
    elif isinstance(event, ConnectFailed):
        lines = connect_failed_message(view, event.result)
        output.text(*lines)
        output.event(
            "connect_failed",
            device_id=view.device_id,
            name=view.name,
            result=event.result.value,
            message=" ".join(lines),
        )


def _emit_result(output: _Output, result: PairingFinished, *, seen_name: str | None = None) -> None:
    lines = outcome_message(result, seen_name=seen_name)
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


def _code_warning(prompt: PairingPrompt) -> str | None:
    """Cảnh báo khi máy này (vai trả lời) đã hiện nhiều mã trong 1 phút (docs/mvp.md mục 8)."""
    if prompt.role is not Role.RESPONDER or prompt.recent_codes < core_pairing.WARN_RECENT_CODES:
        return None
    return (
        f"Cảnh báo: máy này đã hiện {prompt.recent_codes} mã ghép đôi trong 1 phút qua. "
        "Nếu bạn không tự ghép đôi nhiều lần như vậy, có thể có người đang thử giả mạo: "
        "hãy từ chối (gõ k)."
    )


# Dòng đầu của lời hỏi xét theo lệnh (mvp mục 7.2); không chứa chữ số hay chữ do máy kia đặt.
_PROMPT_HEADER_LISTEN = "Có yêu cầu ghép đôi. Hãy so mã dưới đây với mã trên màn hình máy kia."
_PROMPT_HEADER_PAIR = "Hãy so mã dưới đây với mã trên màn hình máy kia."


def _prompt_lines(prompt: PairingPrompt, *, listening: bool) -> list[str]:
    """Các dòng của lời hỏi ghép đôi trước câu “Trùng khớp?” (mvp mục 7.2).

    Thứ tự bắt buộc: tiêu đề cố định, dòng mã (chỉ nhãn và mã), cảnh báo số mã (nếu có),
    tên máy kia tự xưng, tên đã lưu (nếu đã ghép đôi), tên mới (nếu ghép đôi lại sẽ đổi tên).
    Tên chỉ hiện qua `display_name`, trong “…”, sau dòng mã: kẻ đứng giữa đặt tên trông
    như mã cũng không chen được lên trước.
    """
    short_id = prompt.peer_device_id[:8]
    lines = [
        _PROMPT_HEADER_LISTEN if listening else _PROMPT_HEADER_PAIR,
        f"Mã ghép đôi:   {format_code(prompt.code)}",
    ]
    warning = _code_warning(prompt)
    if warning is not None:
        lines.append(warning)
    name = display_name(prompt.peer_name)
    lines.append(f"Máy kia tự xưng là “{name}” (mã máy {short_id}, {prompt.address}).")
    if prompt.known_name is not None:
        known = display_name(prompt.known_name)
        lines.append(f"Máy này đã ghép đôi với mã máy {short_id} trước đây, tên đã lưu: “{known}”.")
        # Ghép đôi lại cập nhật tên theo hello ở cả hai vai (protocol mục 5.5).
        if prompt.known_name != prompt.peer_name:
            lines.append(f"Ghép đôi xong, tên sẽ đổi thành “{name}”.")
    return lines


@dataclass
class _PairState:
    """Kết quả của `pair`, ghi ngay khi có để `cmd_pair` vẫn báo đúng khi bị Ctrl+C giữa chừng."""

    result: PairingFinished | None = None  # kết quả cuối, ghi trước mọi `await` sau khi có
    seen_name: str | None = None  # `pair <tên>`: tên thấy qua mDNS (câu “Thấy X nhưng…”)
    emitted: bool = False  # đã in kết quả: cmd_pair không in thêm


class _Prompter:
    """Hiện lời hỏi ghép đôi và chuyển câu trả lời từ stdin tới `PairingService.decide`."""

    def __init__(self, output: _Output, *, listening: bool, state: _PairState | None = None) -> None:
        self._output = output
        self._listening = listening  # listen: in mọi kết quả; pair: run_pair tự in kết quả
        self._state = state  # pair: ghi kết quả ngay trong on_finished
        self.node: Node | None = None
        self._prompt: PairingPrompt | None = None
        self._shown_at = 0.0
        self._stdin_closed = False
        self.stopping = False  # listen: node đang đóng (Ctrl+C), không mời chờ tiếp

    def on_prompt(self, prompt: PairingPrompt) -> None:
        self._prompt = prompt
        self._shown_at = time.perf_counter()  # cùng đồng hồ với mốc của StdinReader
        out = self._output
        out.text(*_prompt_lines(prompt, listening=self._listening))
        out.ask("Trùng khớp? [c/k]: ")
        # JSON giữ nguyên tên gốc: giao diện đọc JSON tự hiện theo mvp mục 7.2.
        out.event(
            "pair_prompt",
            session_id=prompt.session_id,
            role=prompt.role.value,
            peer_name=prompt.peer_name,
            peer_device_id=prompt.peer_device_id,
            address=prompt.address,
            code=prompt.code,
            expires_in=round(max(0.0, prompt.expires_at - asyncio.get_running_loop().time()), 3),
            recent_codes=prompt.recent_codes,
            warning=_code_warning(prompt),
            known_name=prompt.known_name,
        )
        if self._stdin_closed:
            self._decide(False)

    def on_finished(self, result: PairingFinished) -> None:
        if self._prompt is not None and self._prompt.session_id == result.session_id:
            self._prompt = None  # lời hỏi tự hủy (hết giờ, máy kia hủy…)
        if self._state is not None and not self._state.emitted:
            self._state.result = result
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


def _mdns_options(args: argparse.Namespace, *, advertise: bool) -> MdnsOptions | None:
    """Luật bật mDNS: có `--mdns-interface`, hoặc nghe trên mọi địa chỉ (`--host` mặc định)."""
    interfaces = getattr(args, "mdns_interface", None)
    if interfaces:
        advertised = tuple(getattr(args, "advertise_address", None) or ()) or None
        return MdnsOptions(advertise=advertise, interfaces=tuple(interfaces), advertise_addresses=advertised)
    if getattr(args, "host", _ANY_HOST) == _ANY_HOST:
        return MdnsOptions(advertise=advertise)
    return None


def _make_node(
    args: argparse.Namespace,
    output: _Output,
    prompter: _Prompter,
    *,
    accept_pairing: bool,
    mdns: MdnsOptions | None = None,
    auto_connect: bool = True,
    on_event: Callable[[NodeEvent], None] | None = None,
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
            mdns=mdns,
            auto_connect=auto_connect,
            on_event=on_event,
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


class _EventPrinter:
    """In `NodeEvent` của `listen`; giữ lại các sự kiện tới trước khi in xong phần đầu."""

    def __init__(self, output: _Output) -> None:
        self._output = output
        self._pending: list[NodeEvent] | None = []

    def __call__(self, event: NodeEvent) -> None:
        if self._pending is not None:
            self._pending.append(event)
        else:
            _print_event(self._output, event)

    def ready(self) -> None:
        pending, self._pending = self._pending, None
        for event in pending or ():
            _print_event(self._output, event)


def _error_text(error: BaseException) -> str:
    return str(error) or type(error).__name__


async def run_listen(args: argparse.Namespace, lines: Lines, out: TextIO, err: TextIO) -> int:
    """`teleport listen`: chờ ghép đôi tới khi bị hủy; chỉ trả về khi lỗi khởi động."""
    output = _Output(out, err, bool(getattr(args, "json", False)))
    prompter = _Prompter(output, listening=True)
    printer = _EventPrinter(output)
    mdns = _mdns_options(args, advertise=True)
    node = _make_node(
        args, output, prompter, accept_pairing=True, mdns=mdns, auto_connect=True, on_event=printer
    )
    if isinstance(node, int):
        return node
    code = await _start(node, output)
    if code is not None:
        return code
    try:
        addresses = await node.local_addresses()
        port = node.port
        assert port is not None
        discovery_error = node.discovery_error
        output.event(
            "listening",
            name=node.name,
            device_id=node.identity.device_id,
            port=port,
            addresses=list(addresses),
            discovery=node.discovery_active,
            discovery_error=None if discovery_error is None else _error_text(discovery_error),
        )
        suffix = "" if port == DEFAULT_PORT else f":{port}"
        target = f"{addresses[0]}{suffix}" if addresses else f"<địa chỉ IP của máy này>{suffix}"
        discovery_lines: list[str] = []
        if mdns is not None and discovery_error is None:
            discovery_lines.append(
                "Máy khác trong mạng thấy máy này qua `teleport list` và tự kết nối nếu đã ghép đôi."
            )
        elif mdns is not None:
            discovery_lines.append(
                f"Cảnh báo: chưa quảng bá được qua mDNS ({_error_text(discovery_error)}); "
                "máy khác vẫn ghép đôi được bằng IP."
            )
        output.text(
            "Teleport đang chờ ghép đôi.",
            f"  Tên:      {node.name}",
            f"  Mã máy:   {node.identity.device_id[:8]}",
            f"  Cổng:     {port}",
            f"  Địa chỉ:  {', '.join(addresses) if addresses else '(không tìm thấy địa chỉ IPv4 nào)'}",
            f"Trên máy kia, chạy: teleport pair {target}",
            *discovery_lines,
            "Nhấn Ctrl+C để dừng.",
        )
        printer.ready()
        await _feed_lines(lines, prompter)
        return 0  # không tới đây: _feed_lines chạy tới khi bị hủy
    finally:
        prompter.stopping = True
        await node.close()


# ---- pair ----------------------------------------------------------------------------------

_PAIR_BROWSE_TIMEOUT = 3.0  # giây duyệt mDNS trước khi chọn thiết bị theo tên (đọc lúc dùng)
_BROWSE_POLL = 0.05
# Chuỗi “trông như địa chỉ”: chỉ gồm ký tự của IPv4/IPv6 và có “:”, hoặc chỉ gồm số và “.”.
_ADDRESS_LIKE_RE = re.compile(r"[0-9a-fA-F.:\[\]]*:[0-9a-fA-F.:\[\]]*")
_DOTTED_LIKE_RE = re.compile(r"[0-9.]*\.[0-9.]*")
_FULL_ID_RE = re.compile(r"[0-9a-f]{32}")
_IP_HINT = "teleport pair <ip[:cổng]>"
_PAIR_USAGE = "teleport pair <tên | tiền tố mã | ip[:cổng]>"


def _bad_address_message(target: str) -> str:
    return (
        f"“{target}” không phải địa chỉ IPv4 hợp lệ. Hãy nhập địa chỉ IP, ví dụ 192.168.1.5 hoặc "
        "192.168.1.5:47474, hoặc tên / mã thiết bị như `teleport list` hiện."
    )


def _looks_like_address(text: str) -> bool:
    return _ADDRESS_LIKE_RE.fullmatch(text) is not None or _DOTTED_LIKE_RE.fullmatch(text) is not None


def _pair_target(text: str) -> tuple[str, tuple[str, int] | None]:
    """Phân loại đích của `pair`: trả `(đích đã bỏ khoảng trắng hai đầu, (ip, cổng) hoặc None)`.

    None nghĩa là tìm theo tên. Khoảng trắng hai đầu (cả Unicode, như NBSP khi dán từ
    web) bị bỏ. Không còn khoảng trắng bên trong: trông như địa chỉ (chỉ gồm ký tự của
    IPv4/IPv6 và có “:”, hoặc chỉ gồm số và “.”) thì là địa chỉ, không phải IPv4[:cổng]
    hợp lệ → ValueError. Có khoảng trắng bên trong: chỉ là địa chỉ khi bỏ hết khoảng
    trắng ra được IPv4[:cổng] hợp lệ (“1.2.3.4 :47474”), ngược lại là tên (“PC Nha”).
    """
    target = text.strip()
    compact = "".join(target.split())
    if compact == target:
        if not _looks_like_address(target):
            return target, None
        return target, parse_manual_address(target)
    try:
        return target, parse_manual_address(compact)
    except ValueError:
        return target, None


async def run_pair(
    args: argparse.Namespace, lines: Lines, out: TextIO, err: TextIO, *, state: _PairState | None = None
) -> int:
    """`teleport pair <tên | tiền tố mã | ip[:cổng]>`: ghép đôi với một máy; 0 nếu đã ghép đôi, ngược lại 1.

    Đích rỗng (sau khi bỏ khoảng trắng hai đầu) → mã 2. Địa chỉ hay tên: xem
    `_pair_target`; trông như địa chỉ mà không phải IPv4 hợp lệ → mã 2, không đem đi
    tìm theo tên.

    `state` (của `cmd_pair`) nhận kết quả ngay khi có, tên thấy qua mDNS và việc đã in kết
    quả hay chưa, để lệnh bị hủy giữa chừng vẫn báo đúng kết quả đã có.
    """
    if state is None:
        state = _PairState()
    output = _Output(out, err, bool(getattr(args, "json", False)))
    target = args.address.strip()
    if not target:
        output.error("invalid_address", f"Hãy nhập tên, mã thiết bị hoặc địa chỉ IP: {_PAIR_USAGE}")
        return 2
    try:
        target, endpoint = _pair_target(target)
    except ValueError:
        output.error("invalid_address", _bad_address_message(target))
        return 2
    if endpoint is not None:
        return await _pair_by_address(args, lines, output, *endpoint, state=state)
    return await _pair_by_name(args, lines, output, target, state=state)


async def _pair_by_address(
    args: argparse.Namespace, lines: Lines, output: _Output, ip: str, port: int, *, state: _PairState
) -> int:
    prompter = _Prompter(output, listening=False, state=state)
    node = _make_node(args, output, prompter, accept_pairing=False)
    if isinstance(node, int):
        return node
    code = await _start(node, output)
    if code is not None:
        return code
    try:
        output.text(f"Đang kết nối tới {ip}:{port}…")
        result = await _pair_with(node, lines, prompter, [ip], port, state=state)
        _emit_result(output, result)
        state.emitted = True
        return 0 if result.outcome is _PAIRED else 1
    finally:
        await node.close()


async def _pair_with(
    node: Node,
    lines: Lines,
    prompter: _Prompter,
    addresses: Sequence[str],
    port: int,
    *,
    state: _PairState,
) -> PairingFinished:
    """`PairingService.pair_with` kèm đọc stdin; ghi kết quả vào `state` trước mọi `await` dọn dẹp.

    `pair_with` bị hủy sau khi hai bên đã xác nhận thì vẫn trả kết quả thật (không ném).
    """
    feeder = asyncio.create_task(_feed_lines(lines, prompter), name="teleport.cli.stdin")
    try:
        result = await node.pairing.pair_with(addresses, port)
        state.result = result
        return result
    finally:
        feeder.cancel()
        await asyncio.wait({feeder})


async def _pair_by_name(
    args: argparse.Namespace, lines: Lines, output: _Output, query: str, *, state: _PairState
) -> int:
    """`pair <tên | tiền tố mã>`: duyệt mDNS (không quảng bá, không tự nối), chọn đúng một máy."""
    prompter = _Prompter(output, listening=False, state=state)
    mdns = _mdns_options(args, advertise=False)
    node = _make_node(args, output, prompter, accept_pairing=False, mdns=mdns, auto_connect=False)
    if isinstance(node, int):
        return node
    if mdns is None:
        output.error(
            "discovery_error",
            f"Không tìm được thiết bị theo tên vì khám phá mDNS đang tắt. Hãy nhập địa chỉ IP: {_IP_HINT}",
        )
        return 1
    code = await _start(node, output)
    if code is not None:
        return code
    try:
        output.text(f"Đang tìm “{query}” trong mạng…")
        if not node.discovery_active:
            reason = node.discovery_error
            why = "" if reason is None else f" ({_error_text(reason)})"
            output.error(
                "discovery_error",
                f"Không tìm được thiết bị theo tên vì không bật được khám phá mDNS{why}. "
                f"Hãy nhập địa chỉ IP: {_IP_HINT}",
            )
            return 1
        await _browse(node, query)
        view = _choose_device(output, query, node.devices())
        if view is None:
            return 1
        assert view.port is not None and view.addresses
        state.seen_name = view.name
        output.text(
            f"Đang kết nối tới “{display_name(view.name)}” ({view.device_id[:8]}) "
            f"tại {view.addresses[0]}:{view.port}…"
        )
        result = await _pair_with(node, lines, prompter, view.addresses, view.port, state=state)
        _emit_result(output, result, seen_name=view.name)
        state.emitted = True
        return 0 if result.outcome is _PAIRED else 1
    finally:
        await node.close()


async def _browse(node: Node, query: str) -> None:
    """Chờ đủ `_PAIR_BROWSE_TIMEOUT`; chỉ dừng sớm khi `query` là mã đủ 32 ký tự và đã thấy máy đó."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PAIR_BROWSE_TIMEOUT
    full_id = query.strip().lower()
    exact = _FULL_ID_RE.fullmatch(full_id) is not None
    while (left := deadline - loop.time()) > 0:
        if exact and any(v.device_id == full_id and v.online for v in node.devices()):
            return
        await asyncio.sleep(min(left, _BROWSE_POLL))


def _choose_device(output: _Output, query: str, views: tuple[DeviceView, ...]) -> DeviceView | None:
    """Chọn đúng một thiết bị đang thấy; không được thì in lỗi và trả None."""
    try:
        view = resolve_device(query, views)
    except DeviceNotFound:
        online = [v for v in views if v.online]
        if online:
            extra = ["Thiết bị đang thấy:", *(f"  {v.device_id[:8]}  {v.name}" for v in online)]
        else:
            extra = ["(chưa thấy thiết bị nào)"]
        output.error(
            "not_found",
            f"Không thấy thiết bị nào khớp “{query}” trong mạng.",
            extra=[*extra, f"Nếu mạng chặn mDNS, hãy nhập địa chỉ IP: {_IP_HINT}"],
            candidates=_candidates(tuple(online)),
        )
        return None
    except AmbiguousDevice as e:
        output.error(
            "ambiguous",
            f"“{query}” khớp nhiều thiết bị; hãy nhập mã thiết bị dài hơn:",
            extra=[f"  {d.device_id}  {d.name}" for d in e.candidates],
            candidates=_candidates(e.candidates),
        )
        return None
    if not view.online or not view.addresses or view.port is None:
        output.error(
            "offline",
            f"“{view.name}” ({view.device_id[:8]}) không có trong mạng lúc này. "
            f"Nếu mạng chặn mDNS, hãy nhập địa chỉ IP: {_IP_HINT}",
            device_id=view.device_id,
            name=view.name,
        )
        return None
    return view


# ---- list ----------------------------------------------------------------------------------


async def run_list(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    """`teleport list`: duyệt mDNS vài giây rồi in các thiết bị; không giữ `node.lock`, không quảng bá."""
    output = _Output(out, err, bool(getattr(args, "json", False)))
    directory = _config(args)
    interfaces = getattr(args, "mdns_interface", None)
    mdns = MdnsOptions(advertise=False, interfaces=tuple(interfaces)) if interfaces else None
    timeout = getattr(args, "timeout", None) or SCAN_TIMEOUT
    try:
        result = await scan_devices(directory, timeout=timeout, mdns=mdns)
    except IdentityError as e:
        output.error("identity_error", str(e))
        return 1
    except TrustStoreError as e:
        output.error("trust_store_error", str(e))
        return 1
    except ConfigDirError as e:
        _config_dir_error(output, e.path, e.error)
        return 1
    except DiscoveryUnavailable as e:
        cause = e.__cause__ if e.__cause__ is not None else e
        output.error("discovery_error", f"Không bật được khám phá mDNS ({_error_text(cause)}).")
        return 1
    if output.json:
        obj = {
            "time": _utc_now(),
            "self": {"device_id": result.device_id, "name": result.name, "port": result.port},
            "devices": [_view_dict(v) for v in result.devices],
        }
        print(json.dumps(obj, ensure_ascii=False), file=out, flush=True)
        return 0
    online = [v for v in result.devices if v.online]
    offline_paired = [v for v in result.devices if v.paired and not v.online]
    text = [f"Máy này: {result.name} ({result.device_id[:8]}), cổng {result.port}"]
    if online:
        text.append("Thiết bị trong mạng:")
        for v in online:
            where = ", ".join(f"{ip}:{v.port}" for ip in v.addresses)
            text.append(f"  {v.name}  {v.device_id[:8]}  {_os_label(v.os)}  {_paired_label(v)}  {where}")
    else:
        text.append(_NO_DEVICES)
    if offline_paired:
        text.append("Thiết bị đã ghép đôi nhưng không thấy trong mạng:")
        text.extend(f"  {v.name}  {v.device_id[:8]}" for v in offline_paired)
    # Gợi ý cài lại (mvp F3): máy chưa ghép đôi trùng tên với một máy đã ghép đôi đang offline.
    offline_names = {_fold(v.name) for v in offline_paired}
    hinted: set[str] = set()
    for v in online:
        key = _fold(v.name)
        if not v.paired and key in offline_names and key not in hinted:
            hinted.add(key)
            text.append(
                f"Có một {v.name} mới chưa ghép đôi. Nếu máy đó vừa cài lại Teleport, hãy ghép đôi lại."
            )
    output.text(*text)
    return 0


_NO_DEVICES = (
    "Chưa thấy thiết bị nào. Hãy mở Teleport trên máy kia và kiểm tra hai máy dùng chung một mạng Wi-Fi."
)


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


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


async def _list_main(args: argparse.Namespace) -> int:
    restore = _handle_ctrl_break(asyncio.get_running_loop(), asyncio.current_task())
    try:
        return await run_list(args, sys.stdout, sys.stderr)
    finally:
        restore()


def cmd_list(args: argparse.Namespace) -> int:
    with asyncio.Runner() as runner:
        try:
            return runner.run(_list_main(args))
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
    output = _Output(sys.stdout, sys.stderr, args.json)
    output.text("Đã hủy.")
    output.event("error", error="cancelled", message="Đã hủy.")
    return 1


def cmd_pair(args: argparse.Namespace) -> int:
    state = _PairState()
    code = _run_async(args, functools.partial(run_pair, state=state))
    if code is not None:
        return code
    # Bị hủy (Ctrl+C, Ctrl+Break). Kết quả đã có thì nó mới là sự thật, kể cả khi lệnh hủy
    # tới lúc đang đóng kết nối hay đóng node: đã in thì thôi, chưa in thì in bây giờ.
    result = state.result
    if result is not None and (state.emitted or result.outcome is not PairingOutcome.CANCELLED):
        if not state.emitted:
            _emit_result(_Output(sys.stdout, sys.stderr, args.json), result, seen_name=state.seen_name)
        return 0 if result.outcome is _PAIRED else 1
    output = _Output(sys.stdout, sys.stderr, args.json)
    message = "Đã hủy ghép đôi."
    output.text(message)
    try:
        target, endpoint = _pair_target(args.address)
        address = target if endpoint is None else endpoint[0]  # như PairingFinished.address: chỉ IPv4
    except ValueError:
        address = args.address.strip()
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


def _candidates(devices: Sequence[TrustedDevice | DeviceView]) -> list[dict[str, str]]:
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
    # Ẩn: chỉ duyệt/quảng bá mDNS trên các card này (test dùng 127.0.0.1).
    parser.add_argument("--mdns-interface", action="append", type=_ipv4_type, help=argparse.SUPPRESS)


def _ipv4_type(text: str) -> str:
    if not _valid_host(text):
        raise argparse.ArgumentTypeError(f"phải là địa chỉ IPv4 dạng chấm: {text!r}")
    return text


def _timeout_type(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"thời gian phải là số giây: {text!r}") from None
    if not math.isfinite(value) or not 0 < value <= MAX_SCAN_TIMEOUT:
        raise argparse.ArgumentTypeError(
            f"thời gian phải trong khoảng (0, {MAX_SCAN_TIMEOUT:g}] giây: {text!r}"
        )
    return value


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

    list_ = sub.add_parser("list", help="liệt kê các máy Teleport trong mạng")
    list_.add_argument(
        "--timeout",
        type=_timeout_type,
        default=SCAN_TIMEOUT,
        metavar="GIÂY",
        help=f"thời gian tìm (mặc định {SCAN_TIMEOUT:g} giây)",
    )
    list_.add_argument("--json", action="store_true", help="in kết quả dạng JSON")
    list_.add_argument("--mdns-interface", action="append", type=_ipv4_type, help=argparse.SUPPRESS)
    list_.set_defaults(handler=cmd_list)

    listen = sub.add_parser("listen", help="chờ ghép đôi và giữ kết nối với các máy đã ghép đôi")
    _add_node_options(listen)
    # Ẩn: địa chỉ quảng bá cố định (test), không theo dõi đổi địa chỉ.
    listen.add_argument("--advertise-address", action="append", type=_ipv4_type, help=argparse.SUPPRESS)
    listen.set_defaults(handler=cmd_listen)

    pair = sub.add_parser("pair", help="ghép đôi với máy khác theo tên, mã thiết bị hoặc địa chỉ IP")
    pair.add_argument(
        "address",
        metavar="<tên | tiền tố mã | ip[:cổng]>",
        help="máy kia như `teleport list` hiện, hoặc địa chỉ IP, ví dụ 192.168.1.5",
    )
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
