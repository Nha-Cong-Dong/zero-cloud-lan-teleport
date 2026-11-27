"""Đầu ra và định dạng thông báo thuần cho giao diện dòng lệnh `teleport.cli`."""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import json
import ntpath
import re
import sys
import unicodedata
from collections.abc import Sequence
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, TextIO

from teleport.app import sources as app_sources  # MAX_FILE_SIZE đọc lúc dùng
from teleport.app import transfers as app_transfers  # MIN_RATE_LIMIT đọc lúc dùng
from teleport.app.devices import AmbiguousDevice, DeviceNotFound, DeviceView
from teleport.app.node import (
    ConnectFailed,
    DeviceConnected,
    DeviceDisconnected,
    DeviceOffline,
    DeviceOnline,
    NodeEvent,
    ReceiveDirUnavailable,
)
from teleport.app.pairing import PairingFinished, PairingOutcome, PairingPrompt
from teleport.app.storage import PathTooLongError, UnsafeReceiveDirError
from teleport.app.transfers import (
    Direction,
    SourceError,
    SourceErrorKind,
    TransferFinished,
    TransferInfo,
    TransferResult,
    TransferStatus,
)
from teleport.core import pairing as core_pairing  # WARN_RECENT_CODES đọc lúc dùng
from teleport.core.display import display_filename, display_name, format_size, text_width
from teleport.core.filenames import INVISIBLE_CHARS
from teleport.core.links import ConnectResult
from teleport.core.pairing import Role
from teleport.core.pairing_code import format_code
from teleport.paths import config_dir
from teleport.trust.store import TrustedDevice

_ANY_HOST = "0.0.0.0"


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


def _utc_now() -> str:
    """Thời điểm hiện tại dạng `YYYY-MM-DDTHH:MM:SS.mmmZ` (UTC)."""
    now = datetime.datetime.now(datetime.UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _json_line(obj: object) -> str:
    """Một dòng JSON (UTF-8, không thoát chữ thường); riêng U+2028 và U+2029 được viết thoát,
    vì nhiều bên đọc (ví dụ `str.splitlines`) coi chúng là xuống dòng và làm vỡ JSON Lines."""
    return json.dumps(obj, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _stream_isatty(stream: object) -> bool:
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return False


class _Output:
    """In ra `out` / `err` theo chế độ text hoặc JSON; mọi dòng đều flush ngay.

    `tty` (mặc định `out.isatty()`): dòng tiến độ được vẽ lại tại chỗ bằng CR, không dùng mã
    ANSI; in dòng text khác thì xuống dòng trước.

    Câu hỏi đang mở (`ask`, tới khi `answered` / `close_question`): dòng khác in chen vào thì câu
    hỏi được in lại ngay sau, để người dùng luôn thấy mình đang được hỏi; dòng tiến độ không vẽ.
    """

    def __init__(self, out: TextIO, err: TextIO, json_mode: bool, *, tty: bool | None = None) -> None:
        self.out = out
        self.err = err
        self.json = json_mode
        self.tty = _stream_isatty(out) if tty is None else tty
        self._line_open = False  # vừa in “[c/k]: ” và chưa xuống dòng
        self._question: str | None = None  # câu hỏi đang mở (chưa trả lời, chưa đóng)
        self._progress_width = 0  # TTY: số cột của dòng tiến độ đang vẽ (chưa xuống dòng); 0 = không có

    def text(self, *lines: str) -> None:
        """Dòng cho người đọc (chỉ ở chế độ text)."""
        if self.json:
            return
        self._close_line()
        for line in lines:
            print(line, file=self.out, flush=True)
        self._reask()

    def ask(self, question: str) -> None:
        """In câu hỏi, để con trỏ ở cuối dòng; câu hỏi mở tới khi `answered` / `close_question`."""
        if self.json:
            return
        self._close_line()
        self._question = question
        self._reask()

    def answered(self) -> None:
        """Người dùng đã gõ Enter: terminal đã xuống dòng, câu hỏi đóng (hỏi lại thì `ask` lần nữa)."""
        self._line_open = False
        self._question = None

    def close_question(self) -> None:
        """Câu hỏi đóng mà không có câu trả lời (máy kia hủy, đầu vào đóng, đang dừng…)."""
        self._question = None

    def _reask(self) -> None:
        if self._question is not None:
            print(self._question, end="", file=self.out, flush=True)
            self._line_open = True

    def progress(self, line: str) -> None:
        """Dòng tiến độ (chỉ ở chế độ text).

        TTY: vẽ lại dòng hiện tại bằng CR, đệm khoảng trắng tới độ dài dòng trước; không vẽ khi
        đang có lời hỏi mở. Không TTY: in như một dòng thường.
        """
        if self.json:
            return
        if not self.tty:
            self.text(line)
            return
        if self._question is not None or self._line_open:
            return  # đang hỏi: không vẽ đè lên lời hỏi
        width = text_width(line)
        pad = " " * max(0, self._progress_width - width)
        print("\r" + line + pad, end="", file=self.out, flush=True)
        self._progress_width = max(width, 1)

    def event(self, kind: str, /, **fields: Any) -> None:
        """Một dòng JSON (chỉ ở chế độ `--json`)."""
        if not self.json:
            return
        obj = {"event": kind, "time": _utc_now(), **fields}
        print(_json_line(obj), file=self.out, flush=True)

    def error(self, code: str, message: str, *, extra: Sequence[str] = (), **fields: Any) -> None:
        """Lỗi: text → “Lỗi: …” ra `err`; JSON → object `error` ra `out`."""
        if self.json:
            self.event("error", error=code, message=message, **fields)
            return
        self._close_line()
        print(f"Lỗi: {message}", file=self.err, flush=True)
        for line in extra:
            print(line, file=self.err, flush=True)
        self._reask()

    def _close_line(self) -> None:
        if self._line_open or self._progress_width:
            print(file=self.out, flush=True)
            self._line_open = False
            self._progress_width = 0


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
    peer = result.peer_name or seen_name
    x = _name_label(peer) if peer else target
    responder = result.role is Role.RESPONDER
    if outcome is PairingOutcome.UNREACHABLE and seen_name is not None:
        return [_unreachable_seen(_name_label(seen_name)), _HOTSPOT_HINT]
    if outcome is _PAIRED:
        name = _name_label(result.device.name) if result.device is not None else x
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
        known = _name_label(result.known_name) if result.known_name else x
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


def connect_failed_message(
    view: DeviceView,
    result: ConnectResult,
    *,
    address: str | None = None,
    port: int | None = None,
    actual_device_id: str | None = None,
    actual_name: str | None = None,
) -> list[str]:
    """Câu báo không kết nối được tới thiết bị đã ghép đôi (docs/mvp.md mục 8)."""
    x = _name_label(view.name)
    short_id = view.device_id[:8]
    if result is ConnectResult.REJECTED:
        return [
            f"{x} từ chối kết nối từ máy này. Có thể máy đó đã hủy ghép đôi.",
            f"Để ghép đôi lại: `teleport pair {short_id}`",
        ]
    if result is ConnectResult.IDENTITY_MISMATCH:
        if actual_device_id is not None and actual_name is not None:
            where = (
                f"{address}:{port}"
                if address is not None and port is not None
                else (address or "địa chỉ này")
            )
            actual_label = f"{_name_label(actual_name)} ({actual_device_id[:8]})"
            return [
                f"Địa chỉ {where} đang thuộc về thiết bị tin cậy {actual_label}, không phải {x}.",
                f"Hãy kiểm tra lại địa chỉ IP của {x} hoặc chọn gửi tới {actual_label}.",
            ]
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
    who = f"“{_name_label(view.name)}” ({view.device_id[:8]}"
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
        lines = connect_failed_message(
            view,
            event.result,
            address=event.address,
            port=event.port,
            actual_device_id=event.actual_device_id,
            actual_name=event.actual_name,
        )
        output.text(*lines)
        fields: dict[str, Any] = {
            "device_id": view.device_id,
            "name": view.name,
            "result": event.result.value,
            "message": " ".join(lines),
        }
        if event.address is not None:
            fields["address"] = event.address
        if event.actual_device_id is not None:
            fields["actual_device_id"] = event.actual_device_id
        if event.actual_name is not None:
            fields["actual_name"] = event.actual_name
        output.event("connect_failed", **fields)


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
    name = _name_label(prompt.peer_name)
    lines.append(f"Máy kia tự xưng là “{name}” (mã máy {short_id}, {prompt.address}).")
    if prompt.known_name is not None:
        known = _name_label(prompt.known_name)
        lines.append(f"Máy này đã ghép đôi với mã máy {short_id} trước đây, tên đã lưu: “{known}”.")
        # Ghép đôi lại cập nhật tên theo hello ở cả hai vai (protocol mục 5.5).
        if prompt.known_name != prompt.peer_name:
            lines.append(f"Ghép đôi xong, tên sẽ đổi thành “{name}”.")
    return lines


def _config(args: argparse.Namespace) -> Path:
    return getattr(args, "config_dir", None) or config_dir()


def _valid_host(host: object) -> bool:
    if not isinstance(host, str):
        return False
    try:
        return str(ipaddress.IPv4Address(host)) == host
    except ValueError:
        return False


def _config_dir_error(output: _Output, path: Path, error: OSError) -> None:
    output.error("config_dir_error", f"Không dùng được thư mục cấu hình {path}: {error}")


def _error_text(error: BaseException) -> str:
    return str(error) or type(error).__name__


_MAX_RATE = 1 << 40  # --limit-rate tối đa (byte/giây)
_MAX_RATE_TEXT = 64  # chuỗi --limit-rate dài hơn thì sai luôn (không đổi số khổng lồ)
_RATE_RE = re.compile(r"\s*(\d+(?:[.,]\d+)?)\s*([kKmMgG])?[bB]?(?:/s)?\s*", re.ASCII)
_RATE_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
_GLOB_CHARS = frozenset("*?[")
_REPLACEMENT = "�"  # U+FFFD: ký tự điều khiển trong tên file cục bộ (mvp mục 8)

_STOPPING = "Đang dừng… (nhấn Ctrl+C lần nữa để thoát ngay)"
_CANCELLED = "Đã hủy."
_RECONNECTING = "Đang nối lại…"
_NODE_LOCKED_HINT = (
    "Hãy dừng `teleport receive` hoặc `teleport listen` đang chạy, "
    "hoặc chọn Thoát trong menu khay của Teleport, rồi thử lại."
)
_ASK_RECEIVE = "Nhận? [c/k]: "
_STDIN_CLOSED = "Không đọc được câu trả lời (đầu vào đã đóng) nên đã từ chối. Hãy dùng `--yes` để tự nhận."
_RECEIVE_REJECTED = "Đã từ chối."
_PAIR_STDIN_CLOSED = "Không đọc được câu trả lời (đầu vào đã đóng) nên đã từ chối ghép đôi."


def _printable(text: str) -> str:
    """Ký tự điều khiển (Cc), surrogate lẻ (Cs) và ký tự vô hình / điều khiển hướng chữ
    (`core.filenames.INVISIBLE_CHARS`) → U+FFFD, trước khi in tên hay đường dẫn.

    Tên file nhận đã được làm sạch ở `core.filenames`, tên file và đường dẫn trên máy này thì
    chưa (mvp mục 8: ký tự điều khiển không in thẳng ra màn hình); thay cả ở đây để không phụ
    thuộc một tầng làm sạch duy nhất.
    """
    return "".join(
        _REPLACEMENT if ch in INVISIBLE_CHARS or unicodedata.category(ch) in ("Cc", "Cs") else ch
        for ch in text
    )


def _file_label(name: str) -> str:
    return display_filename(_printable(name))


def _name_label(name: str) -> str:
    """Tên máy để in (không ngoặc): `_printable` rồi `display_name`.

    Tên máy đã qua `validate_device_name` (ở `trusted.json`, mDNS hay ghép đôi); vẫn qua `_printable`
    (phòng thủ chiều sâu, không đổi tên hợp lệ), nên mọi chỗ in cùng một tên ra cùng một chuỗi.
    """
    return display_name(_printable(name))


def _peer_label(name: str | None, device_id: str) -> str:
    """Tên máy kia trong câu (không ngoặc): `_name_label`, không có tên thì 8 ký tự đầu của mã."""
    return _name_label(name) if name else _printable(device_id[:8])


def _file_at(info: TransferInfo, index: int | None) -> str:
    """Tên file thứ `index` (None: `info.file_index`) của lượt, kẹp trong khoảng hợp lệ."""
    if not info.names:
        return "?"
    i = info.file_index if index is None else index
    return _file_label(info.names[min(max(i, 0), len(info.names) - 1)])


def _drive_label(receive_dir: Path | None) -> str:
    """Cách gọi ổ chứa thư mục nhận (mvp mục 8): “ổ D:”, “ổ mạng \\\\máy\\chia-sẻ”, “ổ chứa /home/…”."""
    if receive_dir is None:
        return "ổ lưu file"
    text = str(receive_dir)
    if sys.platform == "win32":
        if text.startswith("\\\\?\\UNC\\"):
            text = "\\\\" + text[len("\\\\?\\UNC\\") :]
        elif text.startswith("\\\\?\\"):
            text = text[len("\\\\?\\") :]
        drive = ntpath.splitdrive(text)[0]
        if drive.startswith(("\\\\", "//")):
            return f"ổ mạng {_printable(drive)}"
        if drive:
            return f"ổ {_printable(drive.upper())}"
    return f"ổ chứa {_printable(text)}"


def _dir_label(receive_dir: Path | None) -> str:
    return "thư mục nhận" if receive_dir is None else _printable(str(receive_dir))


def transfer_result_message(
    finished: TransferFinished, *, peer_name: str | None, receive_dir: Path | None
) -> list[str]:
    """Câu báo kết quả một lượt truyền (docs/mvp.md mục 8); `[]` khi chỉ báo qua JSON.

    `peer_name`: tên máy kia (chưa qua `display_name`; None → 8 ký tự đầu của mã). `receive_dir`:
    thư mục nhận (bên nhận: câu lỗi ghi đĩa, câu từ chối vì ổ). Tên file theo
    `finished.file_index` (không có thì `info.file_index`), kẹp trong khoảng hợp lệ.
    """
    info = finished.info
    p = _peer_label(peer_name, info.key.device_id)
    if info.key.direction is Direction.SEND:
        return [_send_result(finished, p)]
    return _receive_result(finished, p, receive_dir)


def _send_result(finished: TransferFinished, p: str) -> str:
    info, result, reject = finished.info, finished.result, finished.reject
    file = _file_at(info, finished.file_index)
    if result is TransferResult.DONE:
        if finished.delta_reused_bytes > 0:
            return (
                f"Đã gửi xong {len(info.files)} file ({format_size(info.total_bytes)}, "
                f"tái sử dụng {format_size(finished.delta_reused_bytes)} phần không đổi) tới {p}."
            )
        return f"Đã gửi xong {len(info.files)} file ({format_size(info.total_bytes)}) tới {p}."
    if (
        result is TransferResult.REJECTED_NO_SPACE
        and reject is not None
        and reject.needed is not None
        and reject.available is not None
    ):
        return (
            f"{p} không đủ chỗ trống để nhận: cần {format_size(reject.needed)}, "
            f"ổ lưu file trên máy đó còn {format_size(reject.available)}."
        )
    if result is TransferResult.REJECTED_FILE_TOO_LARGE and reject is not None:
        index, max_size = reject.file_index, reject.max_size
        if index is not None and max_size is not None and 0 <= index < len(info.files):
            return (
                f"{p} không nhận được {_file_at(info, index)} ({format_size(info.files[index].size)}): "
                f"ổ lưu file trên máy đó chỉ chứa được file nhỏ hơn {format_size(max_size + 1, trim=True)}."
            )
    if result is TransferResult.REJECTED_NOT_RECEIVING:
        return f"{p} chưa bật nhận file. Trên máy đó hãy chạy `teleport receive` rồi gửi lại."
    if result in _REJECTED:
        return f"{p} đã từ chối nhận file."
    if result is TransferResult.CANCELLED_USER:
        if finished.by_peer:
            return f"{p} đã dừng nhận giữa chừng. Các file đã gửi xong vẫn nằm trên máy đó."
        return f"Đã hủy lượt gửi tới {p}. Các file đã gửi xong vẫn nằm trên máy đó."
    if result is TransferResult.SOURCE_CHANGED:
        return f"{file} đã bị thay đổi trên máy gửi nên không thể gửi tiếp. Hãy gửi lại."
    if result is TransferResult.EXPIRED:
        return f"Mất kết nối với {p} quá lâu nên lượt gửi đã dừng. Hãy gửi lại."
    if result is TransferResult.SOURCE_READ_FAILED:
        return (
            f"Không đọc được {file} trên máy này nữa (file có thể đang bị chương trình khác khóa, "
            f"hoặc nằm trên ổ vừa bị rút ra). Đã dừng gửi tới {p}."
        )
    if result is TransferResult.PEER_ERROR:  # máy nhận gửi `cancel{error}`: lỗi đĩa, dữ liệu hỏng…
        return f"{p} gặp lỗi khi nhận nên đã dừng lượt gửi. Hãy xem thông báo trên máy đó rồi gửi lại."
    if result is TransferResult.PROTOCOL_ERROR:
        return _protocol_error(p)
    return f"{p} đã dừng lượt gửi."  # UNKNOWN_TRANSFER, CANCELLED_OTHER, UNPAIRED, CORRUPT…


_REJECTED = frozenset(
    {
        TransferResult.REJECTED_USER,
        TransferResult.REJECTED_NO_SPACE,
        TransferResult.REJECTED_FILE_TOO_LARGE,
        TransferResult.REJECTED_BUSY,
        TransferResult.REJECTED_OTHER,
    }
)
# Bên nhận: kết quả chỉ báo qua JSON (máy này tự trả lời, không phải lỗi người dùng cần biết).
# `SUSPENDED` (Ctrl+C khi đang nhận: tạm dừng, không hủy) có câu riêng, không thuộc nhóm này.
_RECEIVE_SILENT = frozenset(
    {
        TransferResult.REJECTED_BUSY,
        TransferResult.REJECTED_OTHER,
        TransferResult.REJECTED_NOT_RECEIVING,
    }
)
_KEPT = "Các file đã nhận xong vẫn giữ nguyên; hãy nhờ gửi lại."


def _rejected_no_answer(n: int, s: str) -> str:
    """Máy nhận: lượt chưa hiện lời hỏi bị từ chối vì đầu vào đã đóng (mvp mục 8)."""
    return f"Đã từ chối {n} file từ {s} vì không đọc được câu trả lời (đầu vào đã đóng)."


def _rejected_stopping(n: int, s: str) -> str:
    """Máy nhận: lượt chưa hiện lời hỏi bị từ chối vì Ctrl+C (mvp mục 8)."""
    return f"Đã từ chối {n} file từ {s} vì đang dừng nhận."


def _protocol_error(p: str) -> str:
    return (
        f"Lượt gửi với {p} đã dừng vì máy đó gửi dữ liệu không hợp lệ. "
        "Hãy cập nhật Teleport trên cả hai máy rồi thử lại."
    )


def _receive_result(finished: TransferFinished, s: str, receive_dir: Path | None) -> list[str]:
    info, result, reject = finished.info, finished.result, finished.reject
    file = _file_at(info, finished.file_index)
    n = len(info.files)
    if result is TransferResult.DONE:
        if finished.delta_reused_bytes > 0:
            return [
                f"Đã nhận xong {n} file ({format_size(info.total_bytes)}, "
                f"tái sử dụng {format_size(finished.delta_reused_bytes)} phần không đổi) từ {s}."
            ]
        return [f"Đã nhận xong {n} file ({format_size(info.total_bytes)}) từ {s}."]
    if result is TransferResult.REJECTED_USER and not finished.by_peer:
        return [_RECEIVE_REJECTED]
    if result in _RECEIVE_SILENT:
        return []
    if result is TransferResult.SUSPENDED:
        # “24 giờ” là `RECONNECT_GRACE`, viết thẳng trong câu. Không in `--dir` thật.
        what = f"{n} file" if n > 1 else file
        return [
            f"Đã tạm dừng nhận {what} từ {s}. Chạy lại `teleport receive` (cùng thư mục nhận) để nhận "
            "tiếp phần còn thiếu, miễn là máy gửi vẫn đang chờ; quá 24 giờ kể từ lúc mất kết nối thì "
            "lần chạy sau sẽ xóa phần nhận dở."
        ]
    if (
        result is TransferResult.REJECTED_NO_SPACE
        and reject is not None
        and reject.needed is not None
        and reject.available is not None
    ):
        return [
            f"Đã từ chối {n} file từ {s}: không đủ chỗ trống, cần {format_size(reject.needed)}, "
            f"{_drive_label(receive_dir)} còn {format_size(reject.available)}."
        ]
    if result is TransferResult.REJECTED_FILE_TOO_LARGE and reject is not None:
        index, max_size = reject.file_index, reject.max_size
        if index is not None and max_size is not None and 0 <= index < n:
            return [
                f"Đã từ chối {n} file từ {s}: {_drive_label(receive_dir)} chỉ chứa được file nhỏ hơn "
                f"{format_size(max_size + 1, trim=True)}, mà {_file_at(info, index)} nặng "
                f"{format_size(info.files[index].size)}."
            ]
    if result is TransferResult.CANCELLED_USER:
        if not finished.by_peer:
            return [f"Đã hủy nhận từ {s}. File đã nhận xong vẫn giữ nguyên."]
        if info.status in (TransferStatus.WAITING, TransferStatus.QUEUED):
            return [f"{s} đã hủy lượt gửi."]  # hủy khi máy này còn đang hỏi / xếp hàng
        return [
            f"{s} đã hủy lượt gửi. Các file chưa nhận xong đã được xóa; file đã nhận xong vẫn giữ nguyên."
        ]
    if result is TransferResult.SOURCE_CHANGED:
        return [f"{file} đã bị thay đổi trên {s} nên không nhận tiếp được. {_KEPT}"]
    if result is TransferResult.EXPIRED:
        return [
            f"Mất kết nối với {s} quá lâu nên đã dừng nhận. "
            "Các file chưa nhận xong đã được xóa; file đã nhận xong vẫn giữ nguyên."
        ]
    if result is TransferResult.DISK_FAILED:
        return [
            f"Không lưu được {file} vào {_dir_label(receive_dir)} vì ổ đĩa báo lỗi khi ghi "
            f"(ổ có thể đã đầy, bị rút ra hoặc bị hỏng). Đã dừng nhận từ {s}; "
            "các file đã nhận xong vẫn giữ nguyên."
        ]
    if result is TransferResult.RECEIVE_DIR_MISSING:
        return [
            f"Không nhận được lượt gửi từ {s} vì thư mục nhận {_dir_label(receive_dir)} không còn "
            "hoặc không dùng được, và Teleport không tạo lại được (có thể thư mục chứa nó đã bị xóa, "
            "hoặc ổ đã bị rút ra). "
            "Hãy kiểm tra lại rồi nhờ gửi lại."
        ]
    if result is TransferResult.TEMP_EXISTS:
        return [
            f"Không nhận được lượt gửi từ {s} vì thư mục tạm của lượt này đã có sẵn mà Teleport "
            "không dùng tiếp được. Hãy nhờ gửi lại."
        ]
    if result is TransferResult.CORRUPT:
        return [f"Dữ liệu nhận từ {s} bị hỏng nhiều lần liên tiếp nên đã dừng nhận {file}. {_KEPT}"]
    if result is TransferResult.PEER_ERROR:
        return [f"{s} đã dừng gửi {file} vì gặp lỗi trên máy đó. {_KEPT}"]
    if result is TransferResult.UNKNOWN_TRANSFER:
        return [f"{s} đã hủy lượt gửi {file}. Hãy nhờ gửi lại."]
    if result is TransferResult.PROTOCOL_ERROR:
        return [_protocol_error(s)]
    return [f"{s} đã dừng lượt gửi."]  # CANCELLED_OTHER, UNPAIRED…


def transfer_resumed_message(info: TransferInfo, *, peer_name: str | None) -> str:
    """Câu báo lượt nhận dở được khôi phục khi khởi động (`TransferResumed`, docs/mvp.md mục 8).

    `peer_name`: tên máy gửi (chưa qua `display_name`; None → 8 ký tự đầu của mã). Số file là
    `len(info.files)`; dung lượng đã nhận / tổng qua `format_size`.
    """
    p = _peer_label(peer_name, info.key.device_id)
    done = format_size(max(0, info.done_bytes))
    total = format_size(max(0, info.total_bytes))
    return (
        f"Tiếp tục nhận {len(info.files)} file từ {p} (đã nhận {done} / {total}). "
        "Đang chờ máy đó kết nối lại…"
    )


def _percent_tenths(done: int, total: int) -> int:
    """Phần nghìn đã xong, làm tròn xuống (không hiện 100,0% trước khi xong); total 0 → 1000."""
    if total <= 0:
        return 1000
    return min(1000, max(0, done) * 1000 // total)


def _cut(text: str, limit: int) -> str:
    """Cắt `text` ở cuối cho vừa `limit` cột (kèm `…`)."""
    if text_width(text) <= limit:
        return text
    if limit <= 0:
        return ""
    used = 0
    end = 0
    for ch in text:
        width = text_width(ch)
        if used + width > limit - 1:
            break
        used += width
        end += 1
    return text[:end] + "…"


def parse_rate(text: str) -> int:
    """`--limit-rate` → byte/giây: “20M”, “512K”, “1,5M”, “64KB/s”, “65536” (hệ nhị phân, làm tròn xuống).

    Chỉ chữ số ASCII; hậu tố K/M/G không phân biệt hoa thường, sau đó tùy chọn “B” và “/s”;
    không hậu tố là byte/giây. Sai dạng, hoặc ngoài `transfers.MIN_RATE_LIMIT` … 2⁴⁰ → ValueError.
    """
    if not isinstance(text, str) or len(text) > _MAX_RATE_TEXT:
        raise ValueError("tốc độ không hợp lệ")
    match = _RATE_RE.fullmatch(text)
    if match is None:
        raise ValueError("tốc độ không hợp lệ")
    number = Decimal(match.group(1).replace(",", "."))
    unit = _RATE_UNITS[(match.group(2) or "").lower()]
    value = int((number * unit).to_integral_value(rounding=ROUND_FLOOR))
    if not app_transfers.MIN_RATE_LIMIT <= value <= _MAX_RATE:
        raise ValueError("tốc độ ngoài khoảng cho phép")
    return value


def source_error_message(error: SourceError) -> tuple[int, str, list[str]]:
    """File không gửi được → `(mã thoát, mã lỗi JSON, các dòng)`; thư mục là lỗi cách dùng (mã 2)."""
    path = error.path
    name = _file_label(path.name or str(path))
    kind = error.kind
    if kind is SourceErrorKind.NOT_A_FILE:
        return (
            2,
            "not_a_file",
            [f"{name} không phải là file hay thư mục hợp lệ để gửi."],
        )
    if kind is SourceErrorKind.NOT_FOUND:
        return 1, "source_not_found", [f"Không tìm thấy file {_printable(str(path))}."]
    if kind is SourceErrorKind.PERMISSION:
        return 1, "source_permission", [f"Không mở được {name}: máy này không có quyền đọc file đó."]
    if kind is SourceErrorKind.TOO_LARGE:
        size = "?" if error.size is None else format_size(error.size)
        limit = format_size(app_sources.MAX_FILE_SIZE, trim=True)
        return (
            1,
            "source_too_large",
            [
                f"Không gửi được {name} ({size}): Teleport chỉ gửi được file tối đa {limit}. "
                "Hãy chia file thành nhiều phần nhỏ hơn rồi gửi."
            ],
        )
    if kind is SourceErrorKind.BAD_NAME:
        return (
            1,
            "source_bad_name",
            [f"Không gửi được {name}: tên file có ký tự không hiển thị được. Hãy đổi tên file rồi gửi lại."],
        )
    return 1, "source_os_error", [f"Không đọc được {name}: ổ đĩa báo lỗi."]


def receive_dir_error_message(error: ReceiveDirUnavailable) -> list[str]:
    """Câu báo không dùng được thư mục nhận, theo lỗi gốc (`__cause__`).

    `NotADirectoryError` / `FileExistsError`: đường dẫn là file (POSIX: cả khi một thư mục
    cha là file; Windows báo ca đó bằng `FileNotFoundError` nên ra câu ổ đĩa).
    """
    head = f"Không dùng được thư mục nhận {_printable(str(error.path))}:"
    cause = error.__cause__
    if isinstance(cause, UnsafeReceiveDirError):
        tail = (
            "thư mục ẩn `.teleport` bên trong là liên kết hoặc thuộc người dùng khác. "
            "Hãy xóa nó hoặc chọn thư mục khác bằng `--dir`."
        )
    elif isinstance(cause, PathTooLongError):
        tail = "đường dẫn quá dài. Hãy chọn thư mục có đường dẫn ngắn hơn bằng `--dir`."
    elif isinstance(cause, NotADirectoryError | FileExistsError):
        tail = "đó không phải là thư mục (có thể là một file trùng tên). Hãy chọn thư mục khác bằng `--dir`."
    elif isinstance(cause, PermissionError):
        tail = "máy này không có quyền ghi vào đó. Hãy chọn thư mục khác bằng `--dir`."
    else:
        tail = "ổ đĩa báo lỗi hoặc không phản hồi. Hãy chọn thư mục khác bằng `--dir`."
    return [f"{head} {tail}"]


def _reconnect_unreachable_message(p: str) -> str:
    """`send` đang chờ nối lại mà `core.links` báo không tới được máy nhận (mvp mục 8).

    “24 giờ” là `RECONNECT_GRACE`, viết thẳng trong câu.
    """
    return (
        f"Chưa nối lại được với {p}. Teleport vẫn đang thử lại (tối đa 24 giờ). "
        "Hãy kiểm tra máy đó vẫn đang chạy `teleport receive` và hai máy còn chung một mạng."
    )


def _unreachable_data_message(p: str) -> str:
    return (
        f"Không mở được kết nối truyền dữ liệu tới {p}; tường lửa trên máy đó có thể đang chặn Teleport. "
        "Teleport vẫn đang thử lại."
    )


def _trusted_lookup_error(output: _Output, query: str, error: DeviceNotFound | AmbiguousDevice) -> None:
    """`--to` / `unpair`: không có, hoặc khớp nhiều thiết bị tin cậy."""
    if isinstance(error, AmbiguousDevice):
        output.error(
            "ambiguous",
            f"“{query}” khớp nhiều thiết bị; hãy nhập mã thiết bị dài hơn:",
            extra=[f"  {d.device_id}  {_name_label(d.name)}" for d in error.candidates],
            candidates=_candidates(error.candidates),
        )
        return
    if error.known:
        extra = [
            "Các thiết bị tin cậy:",
            *(f"  {d.device_id[:8]}  {_name_label(d.name)}" for d in error.known),
        ]
    else:
        extra = ["(chưa ghép đôi với thiết bị nào)"]
    output.error(
        "not_found",
        f"Không có thiết bị tin cậy nào khớp “{query}”.",
        extra=extra,
        candidates=_candidates(error.known),
    )


def _source_error(output: _Output, error: SourceError) -> int:
    code, kind, lines = source_error_message(error)
    output.error(kind, lines[0], extra=lines[1:])
    return code


def _duplicate_error(output: _Output) -> int:
    output.error("duplicate_file", "Có file được chọn hai lần. Hãy chọn mỗi file một lần.")
    return 2


def _no_discovery_message(name: str, reason: str) -> str:
    return (
        f"Không tìm được {name} vì không bật được khám phá mDNS ({reason}). Hãy thêm `--address <ip[:cổng]>`."
    )


def _candidates(devices: Sequence[TrustedDevice | DeviceView]) -> list[dict[str, str]]:
    return [{"device_id": d.device_id, "name": d.name} for d in devices]
