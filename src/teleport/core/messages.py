"""Message điều khiển: kiểu dữ liệu và kiểm tra hợp lệ từng trường.

Đặc tả ở docs/protocol.md mục 3. Module này nhận dict đã giải mã từ khung
(`teleport.core.framing`) và trả về dataclass đã kiểm tra; chiều ngược lại
`to_dict` cho ra dict để mã hóa thành khung.

Mọi message đến từ máy khác nên không bao giờ được tin: kiểu, độ dài và giá trị
của từng trường đều được kiểm trong `__post_init__`, nên kể cả dataclass tạo
bằng tay với giá trị sai cũng raise `MessageError`.

Bảng loại kết nối (`ConnectionKind`, `allowed_types`, `opening_kind`,
`check_message_kind`) quy định message nào hợp lệ trên loại kết nối nào (mục 2.1, 3.3).

Message truyền file (`data_open`, `offer`, `accept`, `reject`, `resume`, `complete`,
`verified`, `cancel`) chỉ được kiểm những gì kiểm được từ riêng message đó; các luật
cần trạng thái (`file_index` < số file của `offer`, độ dài bitfield theo số khối, `seq`
khớp `resume` gần nhất…) là việc của máy trạng thái truyền file.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import MISSING, dataclass, fields
from enum import StrEnum
from typing import Any, ClassVar

from teleport.core.bitfield import MAX_BITFIELD_BASE64_LEN, MAX_BITFIELD_BYTES, decode_base64
from teleport.core.blocks import BLOCK_SIZE, MAX_FILE_SIZE
from teleport.core.pairing_code import PROOF_SIGNATURE_SIZE

PROTOCOL_VERSION = 1
MAX_NAME_LEN = 64
MAX_APP_VERSION_LEN = 32
MAX_CERT_PEM_LEN = 8192
# Số nguyên lớn nhất mà JSON trao đổi an toàn giữa mọi bộ giải mã (số thực 64 bit
# biểu diễn chính xác). Cận trên của `t` (ping/pong), `seq`/`resume_seq`, `mtime`.
MAX_SAFE_INTEGER = 2**53 - 1
MAX_OFFER_FILES = 1000
MAX_FILE_NAME_BYTES = 255  # độ dài tên file trong `offer`, tính bằng byte UTF-8

CERT_PEM_HEADER = "-----BEGIN CERTIFICATE-----"

# Dùng re.fullmatch với lớp ký tự ASCII: `$` khớp cả trước "\n" cuối chuỗi, còn
# `\d`/`isdigit` nhận cả chữ số Unicode (ví dụ chữ số toàn chiều rộng).
_DEVICE_ID_RE = re.compile(r"[0-9a-f]{32}")
_HEX32_BYTES_RE = re.compile(r"[0-9a-f]{64}")
_SIGNATURE_HEX_LEN = 2 * PROOF_SIGNATURE_SIZE  # 128 ký tự hex = 64 byte r ‖ s
_SIGNATURE_RE = re.compile(rf"[0-9a-f]{{{_SIGNATURE_HEX_LEN}}}")
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
# Tên file chỉ bị từ chối vì Cc/Cs (Cs không mã hóa được UTF-8); ký tự hướng chữ, ký tự
# vô hình… là việc của bộ làm sạch tên file bên nhận, vốn biến đổi chứ không từ chối.
# Regex biên dịch sẵn thay cho `unicodedata.category` từng ký tự, vì `offer` 1000 tên được
# kiểm ngay trên event loop. Cc là U+0000–U+001F và U+007F–U+009F, Cs là U+D800–U+DFFF;
# Unicode cam kết không đổi hai tập này (test so với `unicodedata` trên mọi code point).
_FILE_NAME_FORBIDDEN_RE = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\ud800-\\udfff]")


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


class RejectReason(StrEnum):
    """Các lý do từ chối `offer` chuẩn. Chỉ dùng để so sánh: `Reject.reason` luôn là `str`."""

    USER = "user"
    NO_SPACE = "no_space"  # kèm `needed`, `available`
    BUSY = "busy"
    FILE_TOO_LARGE = "file_too_large"  # kèm `file_index`, `max_size`
    NOT_RECEIVING = "not_receiving"


class CancelReason(StrEnum):
    """Các lý do hủy lượt truyền chuẩn. Chỉ dùng để so sánh: `Cancel.reason` luôn là `str`."""

    USER = "user"
    SOURCE_CHANGED = "source_changed"
    UNKNOWN_TRANSFER = "unknown_transfer"
    ERROR = "error"
    EXPIRED = "expired"  # lượt đã thất bại vì mất kết nối quá lâu (protocol mục 6.3)


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


def _validate_reason(value: object) -> str:
    """Lý do (`pair_abort`, `reject`, `cancel`): 1–32 ký tự a-z hoặc _; giá trị lạ đúng dạng vẫn hợp lệ.

    Trả về `str` thuần (ví dụ `AbortReason.USER` → "user") để so sánh và mã hóa nhất quán.
    """
    if not isinstance(value, str) or not _REASON_RE.fullmatch(value):
        raise MessageError("reason phải gồm 1–32 ký tự a-z hoặc _")
    return value if type(value) is str else str.__str__(value)


def validate_transfer_id(value: object) -> str:
    """Mã lượt truyền: đúng 32 ký tự hex thường (không kiểm các bit phiên bản của UUID4).

    Trả về nguyên giá trị; sai thì raise MessageError.
    """
    if not isinstance(value, str) or not _DEVICE_ID_RE.fullmatch(value):
        raise MessageError("transfer_id phải là đúng 32 ký tự hex thường")
    return value


_validate_transfer_id = validate_transfer_id  # tên cũ, giữ cho code hiện có


def _validate_file_index(value: object) -> int:
    """Chỉ số file trong `offer`: 0…MAX_OFFER_FILES − 1. So với số file thật là việc của máy trạng thái."""
    return _validate_int(value, "file_index", 0, MAX_OFFER_FILES - 1)


def validate_file_name(value: object) -> str:
    """Tên file trong `offer` như máy kia gửi: ≤ 255 byte UTF-8, không có ký tự loại Cc hay Cs.

    Chuỗi rỗng vẫn hợp lệ. Ở đây **không** làm sạch tên: bên nhận luôn làm sạch trước khi
    dùng (protocol mục 3.3, 4.4). Trả về `str` thuần; sai thì raise MessageError.
    """
    if not isinstance(value, str):
        raise MessageError("name phải là chuỗi")
    # Mỗi code point tốn ít nhất 1 byte: chặn chuỗi quá dài trước khi tìm ký tự cấm.
    if len(value) > MAX_FILE_NAME_BYTES:
        raise MessageError(f"name dài quá {MAX_FILE_NAME_BYTES} byte UTF-8")
    bad = _FILE_NAME_FORBIDDEN_RE.search(value)
    if bad is not None:
        raise MessageError(f"name chứa ký tự không được phép: U+{ord(bad.group()):04X}")
    # Không còn surrogate lẻ nên encode không ném UnicodeEncodeError.
    if len(value.encode("utf-8")) > MAX_FILE_NAME_BYTES:
        raise MessageError(f"name dài quá {MAX_FILE_NAME_BYTES} byte UTF-8")
    return value if type(value) is str else str.__str__(value)


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


def validate_os(value: object) -> str:
    """Tên hệ điều hành: 1–16 ký tự a-z, 0-9 hoặc _; giá trị lạ đúng dạng vẫn hợp lệ.

    Trả về `str` thuần (ví dụ `OsName.WINDOWS` → "windows"); sai thì raise MessageError.
    """
    if not isinstance(value, str) or not _OS_RE.fullmatch(value):
        raise MessageError("os phải gồm 1–16 ký tự a-z, 0-9 hoặc _")
    # Lớp con của str (ví dụ OsName) đổi về str thuần để so sánh và mã hóa nhất quán.
    return value if type(value) is str else str.__str__(value)


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
        os_name = validate_os(self.os)
        if os_name is not self.os:
            # Ví dụ OsName.WINDOWS: đổi về str thuần để so sánh và mã hóa nhất quán.
            object.__setattr__(self, "os", os_name)


@dataclass(frozen=True, slots=True)
class PairRequest:
    """Bên khởi xướng gửi chứng chỉ của mình (protocol.md mục 5)."""

    TYPE: ClassVar[str] = "pair_request"

    cert: str

    def __post_init__(self) -> None:
        # Ở đây chỉ kiểm dạng; phân tích chứng chỉ là việc của teleport.trust.identity.
        if not isinstance(self.cert, str):
            raise MessageError("cert phải là chuỗi")
        if len(self.cert) > MAX_CERT_PEM_LEN:
            raise MessageError(f"cert dài quá {MAX_CERT_PEM_LEN} ký tự")
        if not self.cert.startswith(CERT_PEM_HEADER):
            raise MessageError(f"cert phải bắt đầu bằng {CERT_PEM_HEADER}")


@dataclass(frozen=True, slots=True)
class PairCommit:
    """Bên trả lời cam kết cho nonce n_B của mình, trước khi biết n_A."""

    TYPE: ClassVar[str] = "pair_commit"

    commitment: str

    def __post_init__(self) -> None:
        _validate_hex32(self.commitment, "commitment")


@dataclass(frozen=True, slots=True)
class PairNonce:
    """Bên khởi xướng gửi nonce n_A của mình, sau khi đã nhận `pair_commit`.

    `signature`: chữ ký của bên khởi xướng trên `proof_payload` (protocol mục 5.2), chứng
    minh nó giữ khóa riêng của chứng chỉ trong `pair_request`. Ở đây chỉ kiểm dạng.
    """

    TYPE: ClassVar[str] = "pair_nonce"

    nonce: str
    signature: str  # 128 ký tự hex thường = 64 byte r ‖ s

    def __post_init__(self) -> None:
        _validate_hex32(self.nonce, "nonce")
        if not isinstance(self.signature, str) or not _SIGNATURE_RE.fullmatch(self.signature):
            raise MessageError(f"signature phải là đúng {_SIGNATURE_HEX_LEN} ký tự hex thường")


@dataclass(frozen=True, slots=True)
class PairReveal:
    """Bên trả lời tiết lộ nonce n_B đã cam kết trong `pair_commit` (sau khi đã hiện mã)."""

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
        # Ví dụ AbortReason.USER: đổi về str thuần để so sánh và mã hóa nhất quán.
        object.__setattr__(self, "reason", _validate_reason(self.reason))


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
    """Trả lời `ping` với `t` của `ping` mới nhất chưa trả (protocol mục 2.3); `pong` không cần trả lời."""

    TYPE: ClassVar[str] = "pong"

    t: int

    def __post_init__(self) -> None:
        _validate_int(self.t, "t", 0, MAX_SAFE_INTEGER)


# ---- Truyền file (protocol mục 3.2, 3.3, 4) ------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataOpen:
    """Client mở đầu kết nối dữ liệu phục vụ `resume` có `seq` = `resume_seq` (protocol mục 4.1)."""

    TYPE: ClassVar[str] = "data_open"

    transfer_id: str
    file_index: int
    resume_seq: int

    def __post_init__(self) -> None:
        _validate_transfer_id(self.transfer_id)
        _validate_file_index(self.file_index)
        _validate_int(self.resume_seq, "resume_seq", 1, MAX_SAFE_INTEGER)


@dataclass(frozen=True, slots=True)
class FileEntry:
    """Một file trong `offer`. Không phải message: chỉ nằm trong `Offer.files`.

    `name` là tên máy kia gửi, **chưa** làm sạch (xem `validate_file_name`). `mtime`:
    mili giây Unix, số nguyên (bên gửi ép giá trị âm thành 0).
    """

    name: str
    size: int
    mtime: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", validate_file_name(self.name))
        _validate_int(self.size, "size", 0, MAX_FILE_SIZE)
        _validate_int(self.mtime, "mtime", 0, MAX_SAFE_INTEGER)

    @classmethod
    def from_dict(cls, obj: object) -> FileEntry:
        """Một phần tử của `offer.files`: object có `name`, `size`, `mtime`; trường lạ bị bỏ qua."""
        if not isinstance(obj, dict):
            raise MessageError("mỗi phần tử của files phải là một JSON object")
        for name in ("name", "size", "mtime"):
            if name not in obj:
                raise MessageError(f"phần tử của files thiếu trường {name}")
        return cls(name=obj["name"], size=obj["size"], mtime=obj["mtime"])

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "size": self.size, "mtime": self.mtime}


@dataclass(frozen=True, slots=True)
class Offer:
    """Bên gửi đề nghị gửi 1–1000 file. `files` luôn là tuple `FileEntry` (dict cũng nhận)."""

    TYPE: ClassVar[str] = "offer"

    transfer_id: str
    block_size: int  # luôn = BLOCK_SIZE (1 MiB)
    files: tuple[FileEntry, ...]

    def __post_init__(self) -> None:
        _validate_transfer_id(self.transfer_id)
        if type(self.block_size) is not int or self.block_size != BLOCK_SIZE:
            raise MessageError(f"block_size phải là {BLOCK_SIZE}")
        files: object = self.files
        if not isinstance(files, list | tuple):
            raise MessageError("files phải là một mảng")
        if not 1 <= len(files) <= MAX_OFFER_FILES:
            raise MessageError(f"files phải có 1–{MAX_OFFER_FILES} phần tử")
        entries = tuple(f if isinstance(f, FileEntry) else FileEntry.from_dict(f) for f in files)
        object.__setattr__(self, "files", entries)


@dataclass(frozen=True, slots=True)
class Accept:
    """Bên nhận nhận **tất cả** file trong `offer`; ngay sau đó gửi `resume` cho file đầu."""

    TYPE: ClassVar[str] = "accept"

    transfer_id: str

    def __post_init__(self) -> None:
        _validate_transfer_id(self.transfer_id)


@dataclass(frozen=True, slots=True)
class Reject:
    """Bên nhận từ chối cả `offer`. `reason` lạ đúng dạng vẫn được nhận.

    Trường kèm theo lý do: `no_space` bắt buộc có `needed`, `available` (byte);
    `file_too_large` bắt buộc có `file_index` (file vi phạm đầu tiên), `max_size` (byte,
    file lớn nhất mà hệ file nhận chứa được). Các trường này là tùy chọn với lý do khác,
    nhưng có mặt thì vẫn phải đúng kiểu; vắng mặt là None.
    """

    TYPE: ClassVar[str] = "reject"

    transfer_id: str
    reason: str
    needed: int | None = None
    available: int | None = None
    file_index: int | None = None
    max_size: int | None = None

    def __post_init__(self) -> None:
        _validate_transfer_id(self.transfer_id)
        object.__setattr__(self, "reason", _validate_reason(self.reason))
        for name in ("needed", "available", "max_size"):
            value = getattr(self, name)
            if value is not None:
                _validate_int(value, name, 0, MAX_SAFE_INTEGER)
        if self.file_index is not None:
            _validate_file_index(self.file_index)
        required = _REJECT_REQUIRED.get(self.reason, ())
        for name in required:
            if getattr(self, name) is None:
                raise MessageError(f"reject{{{self.reason}}} thiếu trường {name}")


_REJECT_REQUIRED: dict[str, tuple[str, ...]] = {
    RejectReason.NO_SPACE: ("needed", "available"),
    RejectReason.FILE_TOO_LARGE: ("file_index", "max_size"),
}


@dataclass(frozen=True, slots=True)
class Resume:
    """Bên nhận báo các khối đã có của một file (protocol mục 4.1); gửi lúc nào cũng được.

    `bitfield` giữ nguyên dạng base64 trên dây. Ở đây chỉ kiểm là base64 chuẩn và không dài
    quá bitfield của file lớn nhất; độ dài đúng theo số khối và bit thừa = 0 được kiểm khi
    đọc bằng `Bitfield.from_base64(bitfield, n)`, vì cần biết `size` từ `offer`.
    """

    TYPE: ClassVar[str] = "resume"

    transfer_id: str
    file_index: int
    seq: int
    bitfield: str

    def __post_init__(self) -> None:
        _validate_transfer_id(self.transfer_id)
        _validate_file_index(self.file_index)
        _validate_int(self.seq, "seq", 1, MAX_SAFE_INTEGER)
        if not isinstance(self.bitfield, str):
            raise MessageError("bitfield phải là chuỗi base64")
        # Chặn độ dài chuỗi trước khi giải mã (rẻ), rồi chặn số byte sau khi giải mã:
        # 524 289 byte cũng ra đúng MAX_BITFIELD_BASE64_LEN ký tự (524 290 byte thì dài
        # hơn, đã bị kiểm độ dài chuỗi chặn).
        if len(self.bitfield) > MAX_BITFIELD_BASE64_LEN:
            raise MessageError("bitfield dài quá bitfield của file lớn nhất")
        try:
            raw = decode_base64(self.bitfield)
        except ValueError as e:
            raise MessageError(f"bitfield: {e}") from None
        if len(raw) > MAX_BITFIELD_BYTES:
            raise MessageError("bitfield dài quá bitfield của file lớn nhất")
        if type(self.bitfield) is not str:
            object.__setattr__(self, "bitfield", str.__str__(self.bitfield))


@dataclass(frozen=True, slots=True)
class Complete:
    """Bên gửi đã gửi mọi khối của file ít nhất một lần; `merkle_root` 64 hex thường (mục 4.4)."""

    TYPE: ClassVar[str] = "complete"

    transfer_id: str
    file_index: int
    merkle_root: str

    def __post_init__(self) -> None:
        _validate_transfer_id(self.transfer_id)
        _validate_file_index(self.file_index)
        _validate_hex32(self.merkle_root, "merkle_root")


@dataclass(frozen=True, slots=True)
class Verified:
    """Bên nhận đã kiểm xong và đổi tên file sang tên thật (protocol mục 4.4)."""

    TYPE: ClassVar[str] = "verified"

    transfer_id: str
    file_index: int

    def __post_init__(self) -> None:
        _validate_transfer_id(self.transfer_id)
        _validate_file_index(self.file_index)


@dataclass(frozen=True, slots=True)
class Cancel:
    """Một bên hủy lượt truyền. `reason` lạ đúng dạng vẫn được nhận."""

    TYPE: ClassVar[str] = "cancel"

    transfer_id: str
    reason: str

    def __post_init__(self) -> None:
        _validate_transfer_id(self.transfer_id)
        object.__setattr__(self, "reason", _validate_reason(self.reason))


Message = (
    Hello
    | PairRequest
    | PairCommit
    | PairNonce
    | PairReveal
    | PairConfirm
    | PairAbort
    | ControlOpen
    | Ping
    | Pong
    | DataOpen
    | Offer
    | Accept
    | Reject
    | Resume
    | Complete
    | Verified
    | Cancel
)

_MESSAGE_TYPES: dict[str, type[Message]] = {
    cls.TYPE: cls
    for cls in (
        Hello,
        PairRequest,
        PairCommit,
        PairNonce,
        PairReveal,
        PairConfirm,
        PairAbort,
        ControlOpen,
        Ping,
        Pong,
        DataOpen,
        Offer,
        Accept,
        Reject,
        Resume,
        Complete,
        Verified,
        Cancel,
    )
}


def parse_message(obj: object) -> Message:
    """Đổi dict nhận từ khung thành message đã kiểm tra.

    Không phải dict, thiếu `type`, `type` không phải chuỗi, thiếu trường bắt buộc hay
    trường sai → MessageError. Trường tùy chọn (có giá trị mặc định, ví dụ `Reject.needed`)
    được vắng mặt, nhưng có mặt mà là `null` cũng là sai. `type` chưa biết →
    UnknownMessageType. Trường lạ bị bỏ qua.
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

    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        optional = f.default is not MISSING
        if f.name not in obj:
            if optional:
                continue
            raise MessageError(f"message {msg_type} thiếu trường {f.name}")
        if optional and obj[f.name] is None:
            raise MessageError(f"trường {f.name} của message {msg_type} không được là null")
        kwargs[f.name] = obj[f.name]
    return cls(**kwargs)


def _wire_value(value: object) -> object:
    if isinstance(value, FileEntry):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_wire_value(v) for v in value]
    return value


def to_dict(msg: Message) -> dict:
    """Dict để mã hóa thành khung; luôn có `type`. Bảo đảm `parse_message(to_dict(m)) == m`.

    Trường tùy chọn đang là None thì không ghi ra.
    """
    if type(msg) not in _MESSAGE_TYPES.values():
        raise TypeError(f"không phải message: {type(msg).__name__}")
    result: dict[str, object] = {"type": msg.TYPE}
    for f in fields(msg):
        value = getattr(msg, f.name)
        if value is None and f.default is not MISSING:
            continue
        result[f.name] = _wire_value(value)
    return result


# ---- Loại kết nối --------------------------------------------------------------------------


class ConnectionKind(StrEnum):
    """Loại kết nối trên cổng chung (protocol.md mục 2.1)."""

    PAIRING = "pairing"
    CONTROL = "control"
    DATA = "data"


class WrongConnectionKind(MessageError):
    """Message hợp lệ nhưng không được phép trên loại kết nối này (protocol.md mục 3.3)."""


# `hello` và message mở đầu (`control_open`, `data_open`) không thuộc tập nào: chúng chỉ
# xuất hiện đúng một lần, trước khi loại kết nối được xác định.
_ALLOWED_TYPES: dict[ConnectionKind, frozenset[str]] = {
    ConnectionKind.PAIRING: frozenset(
        cls.TYPE for cls in (PairRequest, PairCommit, PairNonce, PairReveal, PairConfirm, PairAbort)
    ),
    ConnectionKind.CONTROL: frozenset(
        cls.TYPE for cls in (Ping, Pong, Offer, Accept, Reject, Resume, Complete, Verified, Cancel)
    ),
    # Sau `data_open` chỉ có khung khối nhị phân (protocol.md mục 4.2).
    ConnectionKind.DATA: frozenset(),
}


def allowed_types(kind: ConnectionKind | str) -> frozenset[str]:
    """Các `type` được nhận/gửi trên kết nối `kind` SAU message mở đầu.

    `kind` là chuỗi bằng giá trị của `ConnectionKind` cũng được; giá trị khác → ValueError.
    """
    return _ALLOWED_TYPES[ConnectionKind(kind)]


def opening_kind(msg: Message, *, has_client_cert: bool) -> ConnectionKind:
    """Loại kết nối do message mở đầu của client khai báo (protocol.md mục 2.1).

    `pair_request` chỉ hợp lệ khi client không gửi chứng chỉ (kết nối ẩn danh),
    `control_open` và `data_open` chỉ khi có. Mọi trường hợp khác → WrongConnectionKind.
    Không kiểm danh sách tin cậy: đó là việc của tầng `net`.
    """
    if isinstance(msg, PairRequest) and not has_client_cert:
        return ConnectionKind.PAIRING
    if isinstance(msg, ControlOpen) and has_client_cert:
        return ConnectionKind.CONTROL
    if isinstance(msg, DataOpen) and has_client_cert:
        return ConnectionKind.DATA
    msg_type = getattr(msg, "TYPE", type(msg).__name__)
    cert_note = "có" if has_client_cert else "không có"
    raise WrongConnectionKind(f"{msg_type} không mở được kết nối khi client {cert_note} chứng chỉ")


def check_message_kind(kind: ConnectionKind | str, msg: Message) -> None:
    """`msg.TYPE` không thuộc `allowed_types(kind)` → WrongConnectionKind."""
    if type(msg) not in _MESSAGE_TYPES.values():
        raise TypeError(f"không phải message: {type(msg).__name__}")
    kind = ConnectionKind(kind)
    if msg.TYPE not in _ALLOWED_TYPES[kind]:
        raise WrongConnectionKind(f"{msg.TYPE} không hợp lệ trên kết nối {kind.value}")
