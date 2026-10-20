"""Message điều khiển: kiểu dữ liệu và kiểm tra hợp lệ từng trường.

Đặc tả ở docs/protocol.md mục 3. Module này nhận dict đã giải mã từ khung
(`teleport.core.framing`) và trả về dataclass đã kiểm tra; chiều ngược lại
`to_dict` cho ra dict để mã hóa thành khung.

Mọi message đến từ máy khác nên không bao giờ được tin: kiểu, độ dài và giá trị
của từng trường đều được kiểm trong `__post_init__`, nên kể cả dataclass tạo
bằng tay với giá trị sai cũng raise `MessageError`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import ClassVar

PROTOCOL_VERSION = 1
MAX_NAME_LEN = 64
MAX_APP_VERSION_LEN = 32
MAX_CERT_PEM_LEN = 8192
# Số nguyên lớn nhất mà JSON trao đổi an toàn giữa mọi bộ giải mã (số thực 64 bit
# biểu diễn chính xác). Cận trên của `t` (ping/pong), sau này của seq/resume_seq.
MAX_SAFE_INTEGER = 2**53 - 1

CERT_PEM_HEADER = "-----BEGIN CERTIFICATE-----"

# Dùng re.fullmatch với lớp ký tự ASCII: `$` khớp cả trước "\n" cuối chuỗi, còn
# `\d`/`isdigit` nhận cả chữ số Unicode (ví dụ chữ số toàn chiều rộng).
_DEVICE_ID_RE = re.compile(r"[0-9a-f]{32}")
_HEX32_BYTES_RE = re.compile(r"[0-9a-f]{64}")
_REASON_RE = re.compile(r"[a-z_]{1,32}")
_OS_RE = re.compile(r"[a-z0-9_]{1,16}")

# Ký tự cấm trong tên hiển thị, ngoài nhóm Cc (điều khiển) và Cs (surrogate lẻ):
# xuống dòng Unicode, ký tự vô hình và ký tự điều khiển hướng chữ (dùng để giả
# mạo tên, ví dụ "a<U+202E>b" hiện thành chữ đảo ngược). ZWJ U+200D vẫn hợp lệ vì
# emoji ghép (gia đình, cờ…) cần nó. Viết bằng escape, không dùng ký tự thật, để
# mã nguồn không chứa ký tự ẩn (kiểu tấn công "trojan source").
_FORBIDDEN_CHARS = frozenset(
    "\u2028\u2029"  # phân cách dòng / đoạn
    "\u200b\ufeff\u2060"  # khoảng trắng độ rộng 0, BOM, word joiner
    "\u200e\u200f"  # LRM, RLM
    "\u202a\u202b\u202c\u202d\u202e"  # LRE, RLE, PDF, LRO, RLO
    "\u2066\u2067\u2068\u2069"  # LRI, RLI, FSI, PDI
)
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cs"})


class MessageError(ValueError):
    """Message không hợp lệ: không phải object, thiếu trường, sai kiểu hoặc sai giá trị."""


class UnknownMessageType(MessageError):
    """Trường `type` là chuỗi hợp lệ nhưng chưa biết (có thể do bên kia dùng bản mới hơn)."""


class OsName(StrEnum):
    """Hệ điều hành chuẩn trong `hello.os`. Chỉ dùng để so sánh: `Hello.os` luôn là `str`."""

    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"


class AbortReason(StrEnum):
    """Các lý do hủy ghép đôi chuẩn. Chỉ dùng để so sánh: `PairAbort.reason` luôn là `str`."""

    USER = "user"
    BUSY = "busy"
    TIMEOUT = "timeout"
    INVALID = "invalid"


def _validate_text(value: object, field: str, max_len: int) -> str:
    """Chuỗi hiển thị: 1–`max_len` code point, không khoảng trắng đầu/cuối, không ký tự cấm."""
    if not isinstance(value, str):
        raise MessageError(f"{field} phải là chuỗi")
    if not 1 <= len(value) <= max_len:
        raise MessageError(f"{field} phải dài 1–{max_len} ký tự")
    if value != value.strip():
        raise MessageError(f"{field} không được có khoảng trắng ở đầu hoặc cuối")
    for ch in value:
        if ch in _FORBIDDEN_CHARS or unicodedata.category(ch) in _FORBIDDEN_CATEGORIES:
            raise MessageError(f"{field} chứa ký tự không được phép: U+{ord(ch):04X}")
    return value


def _validate_int(value: object, field: str, lo: int, hi: int) -> int:
    """Số nguyên JSON trong đoạn `lo`…`hi`.

    Dùng `type(...) is int`: bool là lớp con của int, còn float 1.0 bằng 1; cả hai
    (và mọi lớp con khác của int) đều bị từ chối.
    """
    if type(value) is not int:
        raise MessageError(f"{field} phải là số nguyên")
    if not lo <= value <= hi:
        raise MessageError(f"{field} phải là số nguyên {lo}…{hi}")
    return value


def _validate_hex32(value: object, field: str) -> str:
    """32 byte viết thành đúng 64 ký tự hex thường (nonce, commitment)."""
    if not isinstance(value, str) or not _HEX32_BYTES_RE.fullmatch(value):
        raise MessageError(f"{field} phải là đúng 64 ký tự hex thường")
    return value


def validate_device_id(value: object) -> str:
    """Mã thiết bị: đúng 32 ký tự hex thường. Trả về nguyên giá trị; sai thì raise MessageError."""
    if not isinstance(value, str) or not _DEVICE_ID_RE.fullmatch(value):
        raise MessageError("device_id phải là đúng 32 ký tự hex thường")
    return value


def validate_device_name(value: object) -> str:
    """Tên thiết bị: 1–64 code point, không khoảng trắng đầu/cuối, không ký tự điều khiển.

    Trả về nguyên giá trị (không chuẩn hóa NFC); sai thì raise MessageError.
    """
    return _validate_text(value, "name", MAX_NAME_LEN)


@dataclass(frozen=True, slots=True)
class Hello:
    """Gửi ngay sau khi bắt tay TLS, cả hai chiều."""

    TYPE: ClassVar[str] = "hello"

    protocol: int
    device_id: str
    name: str
    app_version: str
    os: str  # "windows" · "macos" · "linux"; giá trị lạ đúng dạng vẫn được nhận

    def __post_init__(self) -> None:
        # bool là lớp con của int, float 1.0 bằng 1: cả hai đều bị từ chối.
        if type(self.protocol) is not int or self.protocol < 1:
            raise MessageError("protocol phải là số nguyên ≥ 1")
        validate_device_id(self.device_id)
        validate_device_name(self.name)
        _validate_text(self.app_version, "app_version", MAX_APP_VERSION_LEN)
        if not isinstance(self.os, str) or not _OS_RE.fullmatch(self.os):
            raise MessageError("os phải gồm 1–16 ký tự a-z, 0-9 hoặc _")
        if type(self.os) is not str:
            # Ví dụ OsName.WINDOWS: đổi về str thuần để so sánh và mã hóa nhất quán.
            object.__setattr__(self, "os", str.__str__(self.os))


@dataclass(frozen=True, slots=True)
class PairRequest:
    """Bên khởi xướng gửi chứng chỉ của mình và cam kết cho nonce n_A (protocol.md mục 5)."""

    TYPE: ClassVar[str] = "pair_request"

    cert: str
    commitment: str

    def __post_init__(self) -> None:
        # Ở đây chỉ kiểm dạng; phân tích chứng chỉ là việc của teleport.trust.identity.
        if not isinstance(self.cert, str):
            raise MessageError("cert phải là chuỗi")
        if len(self.cert) > MAX_CERT_PEM_LEN:
            raise MessageError(f"cert dài quá {MAX_CERT_PEM_LEN} ký tự")
        if not self.cert.startswith(CERT_PEM_HEADER):
            raise MessageError(f"cert phải bắt đầu bằng {CERT_PEM_HEADER}")
        _validate_hex32(self.commitment, "commitment")


@dataclass(frozen=True, slots=True)
class PairChallenge:
    """Bên trả lời gửi nonce n_B của mình."""

    TYPE: ClassVar[str] = "pair_challenge"

    nonce: str

    def __post_init__(self) -> None:
        _validate_hex32(self.nonce, "nonce")


@dataclass(frozen=True, slots=True)
class PairReveal:
    """Bên khởi xướng tiết lộ nonce n_A đã cam kết trong `pair_request`."""

    TYPE: ClassVar[str] = "pair_reveal"

    nonce: str

    def __post_init__(self) -> None:
        _validate_hex32(self.nonce, "nonce")


@dataclass(frozen=True, slots=True)
class PairConfirm:
    """Người dùng đã xác nhận mã 6 số trùng nhau."""

    TYPE: ClassVar[str] = "pair_confirm"


@dataclass(frozen=True, slots=True)
class PairAbort:
    """Hủy ghép đôi. `reason` lạ (từ bản mới hơn) vẫn được nhận nếu đúng dạng."""

    TYPE: ClassVar[str] = "pair_abort"

    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not _REASON_RE.fullmatch(self.reason):
            raise MessageError("reason phải gồm 1–32 ký tự a-z hoặc _")
        if type(self.reason) is not str:
            # Ví dụ AbortReason.USER: đổi về str thuần để so sánh và mã hóa nhất quán.
            object.__setattr__(self, "reason", str.__str__(self.reason))


@dataclass(frozen=True, slots=True)
class ControlOpen:
    """Client mở đầu kết nối điều khiển (protocol.md mục 2.1). Không có trường."""

    TYPE: ClassVar[str] = "control_open"


@dataclass(frozen=True, slots=True)
class Ping:
    """Giữ kết nối điều khiển sống. `t`: mili giây theo đồng hồ bên gửi, bên nhận không diễn giải."""

    TYPE: ClassVar[str] = "ping"

    t: int

    def __post_init__(self) -> None:
        _validate_int(self.t, "t", 0, MAX_SAFE_INTEGER)


@dataclass(frozen=True, slots=True)
class Pong:
    """Trả lời `ping` với đúng `t` của nó; `pong` không cần trả lời."""

    TYPE: ClassVar[str] = "pong"

    t: int

    def __post_init__(self) -> None:
        _validate_int(self.t, "t", 0, MAX_SAFE_INTEGER)


Message = (
    Hello | PairRequest | PairChallenge | PairReveal | PairConfirm | PairAbort | ControlOpen | Ping | Pong
)

_MESSAGE_TYPES: dict[str, type[Message]] = {
    cls.TYPE: cls
    for cls in (
        Hello,
        PairRequest,
        PairChallenge,
        PairReveal,
        PairConfirm,
        PairAbort,
        ControlOpen,
        Ping,
        Pong,
    )
}


def parse_message(obj: object) -> Message:
    """Đổi dict nhận từ khung thành message đã kiểm tra.

    Không phải dict, thiếu `type`, `type` không phải chuỗi, thiếu trường hay trường
    sai → MessageError. `type` chưa biết → UnknownMessageType. Trường lạ bị bỏ qua.
    """
    if not isinstance(obj, dict):
        raise MessageError("message phải là một JSON object")
    if "type" not in obj:
        raise MessageError("message thiếu trường type")
    msg_type = obj["type"]
    if not isinstance(msg_type, str):
        raise MessageError("trường type phải là chuỗi")
    cls = _MESSAGE_TYPES.get(msg_type)
    if cls is None:
        raise UnknownMessageType(f"loại message chưa biết: {msg_type!r}")

    kwargs = {}
    for f in fields(cls):
        if f.name not in obj:
            raise MessageError(f"message {msg_type} thiếu trường {f.name}")
        kwargs[f.name] = obj[f.name]
    return cls(**kwargs)


def to_dict(msg: Message) -> dict:
    """Dict để mã hóa thành khung; luôn có `type`. Bảo đảm `parse_message(to_dict(m)) == m`."""
    if type(msg) not in _MESSAGE_TYPES.values():
        raise TypeError(f"không phải message: {type(msg).__name__}")
    return {"type": msg.TYPE, **{f.name: getattr(msg, f.name) for f in fields(msg)}}
