"""Lệnh giữa hai tiến trình `teleport-gui` của cùng một thư mục cấu hình: định dạng, luật, bộ đọc.

Phần thuần (không Qt, không I/O) của kênh `QLocalSocket` mà `ui.app.SingleInstance` mở (docstring
`ui.app`, “Một bản mỗi thư mục cấu hình”). Bản mở sau gửi đúng một lệnh rồi chờ bản đang chạy đóng
kết nối: server đóng sau khi quyết định, và việc đóng đó là dấu duy nhất cho biết lệnh đã tới (không
có byte trả lời, không có số phiên bản). Bản khác đời tính tên server khác nên không nối được; bên gửi
hết hạn chờ và hiện hộp “chưa phản hồi”. Đây là nơi duy nhất ghi các con số của kênh; tài liệu khác
chỉ trỏ tới đây. Bộ đọc quét mỗi byte một lần (giữ vị trí quét giữa các lần `feed`).

**Định dạng** (mỗi kết nối một lệnh)::

    show      := "show\\n"                                   ; mở cửa sổ
    send      := "send\\n" path-line{1,SEND_MAX_FILES} "\\n"   ; mở hộp gửi; kết thúc bằng dòng rỗng
    path-line := UTF-8(path) "\\n"

`path` là chuỗi đã qua `check_path`. UTF-8 chặt (giải mã lỗi thì bỏ cả lệnh), không CR, không BOM,
không byte nào sau dòng rỗng kết thúc. Lệnh `send` chỉ mở hộp chọn máy (`ui.send_picker`); không bao
giờ tự gửi.

**Hằng** (đọc lúc dùng, test đổi được):

| Hằng | Giá trị | Ý nghĩa |
|---|---|---|
| `SHOW_COMMAND` | `b"show\\n"` | Lệnh `show`, byte y như các bản trước |
| `SEND_HEADER` | `b"send\\n"` | Dòng đầu của lệnh `send` |
| `HEADER_MAX_BYTES` | 16 | Quá từng này byte mà chưa thấy `\\n` đầu tiên thì bỏ lệnh |
| `MAX_BYTES` | 131 072 (128 KiB) | Trần cả lệnh (dòng lệnh Windows ≤ 32 767 UTF-16, ≤ 98 301 byte UTF-8) |
| `SEND_MAX_FILES` | `core.messages.MAX_OFFER_FILES` | Trần số đường dẫn của một lệnh `send` |
| `PATH_MAX_BYTES` | 4096 | Trần byte UTF-8 của một đường dẫn |

Hạn đọc một lệnh và số kết nối cùng lúc là việc của server (`ui.app`: `IPC_READ_TIMEOUT_MS`,
`IPC_MAX_CONNECTIONS`).

**Luật đường dẫn** (`check_path`, không chạm đĩa: thread Qt không `stat` file người dùng; file có
tồn tại, có đọc được không do `transfers.send` kiểm lúc bấm Gửi). Trả chuỗi đã chuẩn hóa, hoặc None.
Luật chung:
1. Chuỗi không rỗng, mã hóa UTF-8 chặt được (surrogate lẻ là sai), ≤ `PATH_MAX_BYTES` byte.
2. Không có ký tự loại Cc (`\\0`, `\\t`, `\\r`, `\\n`, C0, DEL, C1).
3. Không có thành phần `.` hay `..` (xét trước khi chuẩn hóa).
4. Tên cuối không rỗng: không kết thúc bằng dấu phân cách, không phải gốc (`C:\\`, `\\\\máy\\chia-sẻ`, `/`).

Windows (`windows=True`), xét sau khi đổi `/` thành `\\`:
5. Chỉ hai dạng: ổ đĩa (một chữ ASCII, `:`, `\\`) và UNC `\\\\máy\\chia-sẻ\\…` (máy, chia sẻ không rỗng).
6. Từ chối `\\\\?\\`, `\\\\.\\` và `\\??\\` (kể cả `\\\\?\\C:\\…`), `\\abc` (gốc không ổ), `C:abc` (tương
   đối theo ổ) và đường dẫn tương đối.
7. Mỗi thành phần (cả máy và chia sẻ của UNC): không rỗng (chỉ `\\\\` mở đầu UNC được phép); không
   kết thúc bằng `.` hay dấu cách (nên cũng không chỉ gồm chấm/cách); không có `<>"|?*`; không có
   `:` (chỉ ở vị trí ổ đĩa: chặn alternate data stream); không là tên thiết bị
   (`core.filenames.is_device_name`: `CON`, `nul.txt`…).
8. Chuẩn hóa bằng `ntpath.normpath`.

POSIX (`windows=False`):
9. Bắt đầu bằng `/`; chuẩn hóa bằng `posixpath.normpath`, kết quả không bắt đầu bằng `//`.

`windows=None` là `os.name == "nt"`; test truyền True/False để thử cả hai luật trên mọi hệ điều
hành (`ntpath` / `posixpath`, không `os.path`).

**`prepare_send(args, *, cwd)`** (cho `main`, tham số `--send`): đường dẫn không ổ, không gốc thì
nối với `cwd` trước (vẫn từ chối `\\abc`, `C:abc`); rỗng hay có phần tử bị `check_path` từ chối →
`SendArgsError(BAD_PATH)` kèm phần tử sai **đầu tiên** (nguyên văn); bỏ trùng không báo, giữ lần đầu
(khóa so trùng: `normcase` của chuỗi đã chuẩn hóa, cùng kết quả với `ui.send.selection_error`); còn
quá `SEND_MAX_FILES` → `TOO_MANY`. `encode_send` dựng lệnh `send`; quá `MAX_BYTES` → `TOO_LARGE`.

**`IpcReader`** (server, mỗi kết nối một bản): máy trạng thái có đúng một quyết định; đã quyết thì
`feed`/`close` trả lại quyết định đó và không giữ byte nào. Server nghiêm hơn client (client đã tự
lọc): lệnh sai luật nào cũng bị bỏ cả lệnh, kèm lý do `DropReason` (server chỉ ghi lý do, không bao
giờ ghi byte hay đường dẫn nhận được, mvp N9):
- `size`: bộ đệm vượt `MAX_BYTES`.
- `header`: chưa thấy `\\n` mà bộ đệm không còn có thể là dòng đầu hợp lệ (hay vượt
  `HEADER_MAX_BYTES`); dòng đầu khác `show`/`send` (`SHOW`, `show\\r`, BOM…).
- `trailing`: `show\\n` có byte theo sau; byte sau dòng rỗng kết thúc của `send`.
- `count`: `send\\n\\n` (không đường dẫn nào) hay quá `SEND_MAX_FILES` đường dẫn.
- `utf8`: một dòng không giải mã UTF-8 chặt được. `path`: `check_path` từ chối một dòng.
- `dup`: hai đường dẫn trùng (khóa như `prepare_send`).
- `eof`: máy kia ngắt khi lệnh chưa đủ (`close`). `timeout`: server tự gán khi hết hạn đọc.
Lệnh `send` đọc tới lần đầu gặp `b"\\n\\n"` sau dòng đầu; dòng tách bằng `b"\\n"` trên byte (không
`splitlines()`, vốn tách cả `\\r`, `\\x1c`, U+2028…).
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import string
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from teleport.core.filenames import is_device_name
from teleport.core.messages import MAX_OFFER_FILES

SHOW_COMMAND = b"show\n"
SEND_HEADER = b"send\n"
HEADER_MAX_BYTES = 16
MAX_BYTES = 131_072
SEND_MAX_FILES = MAX_OFFER_FILES
PATH_MAX_BYTES = 4096

_END = b"\n\n"
_DRIVE_LETTERS = frozenset(string.ascii_letters)
_WINDOWS_FORBIDDEN = frozenset('<>"|?*:')
_WINDOWS_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")


class SendArgsErrorKind(StrEnum):
    """Lý do tham số `--send` không dùng được (câu ở `ui.app.send_args_hint`)."""

    BAD_PATH = "bad_path"
    TOO_MANY = "too_many"
    TOO_LARGE = "too_large"


class SendArgsError(ValueError):
    """Tham số `--send` sai; `path` là phần tử sai đầu tiên (nguyên văn) khi `kind` là BAD_PATH."""

    def __init__(self, kind: SendArgsErrorKind, path: str | None = None) -> None:
        super().__init__(kind.value)  # không đưa đường dẫn vào thông điệp lỗi (log, mvp N9)
        self.kind = kind
        self.path = path


class DropReason(StrEnum):
    """Lý do bỏ một lệnh (docstring module); chỉ mã này được ghi log."""

    HEADER = "header"
    SIZE = "size"
    UTF8 = "utf8"
    PATH = "path"
    COUNT = "count"
    DUP = "dup"
    TRAILING = "trailing"
    TIMEOUT = "timeout"
    EOF = "eof"


@dataclass(frozen=True, slots=True)
class ShowCommand:
    """Lệnh `show`: hiện cửa sổ."""


@dataclass(frozen=True, slots=True)
class SendCommand:
    """Lệnh `send`: các đường dẫn đã chuẩn hóa, đúng thứ tự, không trùng."""

    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Dropped:
    """Lệnh bị bỏ."""

    reason: DropReason


Decision = ShowCommand | SendCommand | Dropped


def _is_windows(windows: bool | None) -> bool:
    return os.name == "nt" if windows is None else windows


# ---- Luật đường dẫn --------------------------------------------------------------------------


def check_path(text: str, *, windows: bool | None = None) -> str | None:
    """Đường dẫn đầy đủ tới một file, đã chuẩn hóa, hoặc None (luật ở docstring module). Không I/O."""
    if not isinstance(text, str) or not text:
        return None
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:  # surrogate lẻ
        return None
    if size > PATH_MAX_BYTES:
        return None
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        return None
    if _is_windows(windows):
        return _check_windows(text)
    return _check_posix(text)


def _check_windows(text: str) -> str | None:
    text = text.replace("/", "\\")
    if text.startswith(_WINDOWS_DEVICE_PREFIXES):
        return None
    if len(text) >= 3 and text[0] in _DRIVE_LETTERS and text[1] == ":" and text[2] == "\\":
        names = text[3:].split("\\")
    elif text.startswith("\\\\"):
        names = text[2:].split("\\")
        if len(names) < 3:  # `\\máy\chia-sẻ` chỉ là gốc của chia sẻ
            return None
    else:  # `\abc`, `C:abc`, tương đối
        return None
    if not all(_windows_component_ok(name) for name in names):
        return None
    return ntpath.normpath(text)


def _windows_component_ok(name: str) -> bool:
    if not name or name[-1] in ". ":
        return False
    if any(ch in _WINDOWS_FORBIDDEN for ch in name):
        return False
    return not is_device_name(name)


def _check_posix(text: str) -> str | None:
    if not text.startswith("/") or text.endswith("/"):
        return None
    if any(name in (".", "..") for name in text.split("/")):
        return None
    normalized = posixpath.normpath(text)
    if normalized.startswith("//"):
        return None
    return normalized


def _dup_key(normalized: str, windows: bool) -> str:
    """Khóa so trùng (cùng kết quả với `normcase(abspath(...))` của `ui.send.selection_error`)."""
    return ntpath.normcase(normalized) if windows else posixpath.normcase(normalized)


def _absolute(arg: str, cwd: str, windows: bool) -> str:
    """`arg` nối với `cwd` khi không ổ, không gốc; còn lại giữ nguyên để `check_path` xét."""
    if windows:
        flipped = arg.replace("/", "\\")
        if flipped.startswith("\\") or flipped[1:2] == ":" or not cwd:
            return arg
        return ntpath.join(cwd, arg)
    if arg.startswith("/") or not cwd:
        return arg
    return posixpath.join(cwd, arg)


def prepare_send(args: Sequence[str], *, cwd: str, windows: bool | None = None) -> tuple[Path, ...]:
    """Đường dẫn của `--send`, đã kiểm, chuẩn hóa và bỏ trùng; sai → `SendArgsError`. Không I/O."""
    win = _is_windows(windows)
    if not args:
        raise SendArgsError(SendArgsErrorKind.BAD_PATH)
    kept: list[str] = []
    seen: set[str] = set()
    for arg in args:
        normalized = check_path(_absolute(arg, cwd, win), windows=win) if isinstance(arg, str) else None
        if normalized is None:
            raise SendArgsError(SendArgsErrorKind.BAD_PATH, arg if isinstance(arg, str) else None)
        key = _dup_key(normalized, win)
        if key in seen:
            continue
        seen.add(key)
        kept.append(normalized)
    if len(kept) > SEND_MAX_FILES:
        raise SendArgsError(SendArgsErrorKind.TOO_MANY)
    return tuple(Path(p) for p in kept)


def encode_send(paths: Sequence[Path | str]) -> bytes:
    """Lệnh `send` cho `paths` (kết quả của `prepare_send`).

    Rỗng hay đường dẫn không mã hóa / đóng khung được → BAD_PATH; quá `SEND_MAX_FILES` → TOO_MANY;
    quá `MAX_BYTES` → TOO_LARGE.
    """
    if not paths:
        raise SendArgsError(SendArgsErrorKind.BAD_PATH)
    if len(paths) > SEND_MAX_FILES:
        raise SendArgsError(SendArgsErrorKind.TOO_MANY)
    parts = [SEND_HEADER]
    for path in paths:
        text = os.fspath(path)
        try:
            line = text.encode("utf-8")
        except UnicodeEncodeError:
            raise SendArgsError(SendArgsErrorKind.BAD_PATH, text) from None
        if not line or b"\n" in line:
            raise SendArgsError(SendArgsErrorKind.BAD_PATH, text)
        parts.append(line + b"\n")
    parts.append(b"\n")
    data = b"".join(parts)
    if len(data) > MAX_BYTES:
        raise SendArgsError(SendArgsErrorKind.TOO_LARGE)
    return data


# ---- Bộ đọc của server -----------------------------------------------------------------------


class IpcReader:
    """Đọc một lệnh từ dòng byte của một kết nối (luật ở docstring module)."""

    def __init__(self, *, windows: bool | None = None) -> None:
        self._windows = _is_windows(windows)
        self._buffer = bytearray()
        self._decision: Decision | None = None
        self._header_end = -1  # vị trí `\n` của dòng đầu `send` đã xét xong; -1: chưa
        self._scan = 0  # tìm dòng rỗng kết thúc từ đây: phần trước đã quét, mỗi byte quét một lần

    @property
    def decided(self) -> bool:
        return self._decision is not None

    def feed(self, data: bytes) -> Decision | None:
        """Thêm `data`; trả quyết định khi đã có, None khi cần thêm dữ liệu."""
        if self._decision is not None:
            return self._decision
        self._buffer += data
        if len(self._buffer) > MAX_BYTES:
            return self._decide(Dropped(DropReason.SIZE))
        decision = self._parse()
        return None if decision is None else self._decide(decision)

    def close(self) -> Decision:
        """Máy kia ngắt kết nối: lệnh chưa đủ → `Dropped(EOF)`."""
        if self._decision is not None:
            return self._decision
        return self._decide(Dropped(DropReason.EOF))

    def _decide(self, decision: Decision) -> Decision:
        self._decision = decision
        self._buffer = bytearray()  # không giữ byte nào sau khi đã quyết
        return decision

    def _parse(self) -> Decision | None:
        buffer = self._buffer
        if self._header_end >= 0:
            return self._parse_send(self._header_end)
        newline = buffer.find(b"\n", 0, HEADER_MAX_BYTES + 1)  # dòng đầu nằm trong `HEADER_MAX_BYTES` byte
        if newline < 0 and len(buffer) > HEADER_MAX_BYTES:
            return Dropped(DropReason.HEADER)
        if newline < 0:
            head = bytes(buffer)
            if len(head) > HEADER_MAX_BYTES:
                return Dropped(DropReason.HEADER)
            if SHOW_COMMAND.startswith(head) or SEND_HEADER.startswith(head):
                return None
            return Dropped(DropReason.HEADER)
        header = bytes(buffer[: newline + 1])
        if header == SHOW_COMMAND:
            if len(buffer) != len(SHOW_COMMAND):
                return Dropped(DropReason.TRAILING)
            return ShowCommand()
        if header != SEND_HEADER:
            return Dropped(DropReason.HEADER)
        self._header_end = newline
        self._scan = newline
        return self._parse_send(newline)

    def _parse_send(self, newline: int) -> Decision | None:
        """Lệnh `send`: chờ dòng rỗng kết thúc (quét tiếp từ `_scan`, tổng O(n)), rồi xét thân."""
        buffer = self._buffer
        end = buffer.find(_END, self._scan)
        if end < 0:
            self._scan = max(newline, len(buffer) - 1)  # `\n\n` có thể vắt qua hai lần `feed`
            return None
        if end + len(_END) != len(buffer):
            return Dropped(DropReason.TRAILING)
        if end == newline:  # `send\n\n`
            return Dropped(DropReason.COUNT)
        return self._parse_paths(bytes(buffer[newline + 1 : end]))

    def _parse_paths(self, body: bytes) -> Decision:
        lines = body.split(b"\n")
        if len(lines) > SEND_MAX_FILES:
            return Dropped(DropReason.COUNT)
        kept: list[str] = []
        seen: set[str] = set()
        for line in lines:
            try:
                text = line.decode("utf-8", "strict")
            except UnicodeDecodeError:
                return Dropped(DropReason.UTF8)
            normalized = check_path(text, windows=self._windows)
            if normalized is None:
                return Dropped(DropReason.PATH)
            key = _dup_key(normalized, self._windows)
            if key in seen:
                return Dropped(DropReason.DUP)
            seen.add(key)
            kept.append(normalized)
        return SendCommand(tuple(kept))
