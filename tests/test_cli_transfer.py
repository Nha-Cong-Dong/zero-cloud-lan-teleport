"""Test `teleport send` / `teleport receive` (P2-7): viết theo `specs/plan-p2-7.md` + `specs/spec-p2-7.md`.

Câu thông báo so **nguyên văn** với câu đã duyệt (spec-p2-7 mục D: M1–M13, B1–B13, C1–C12; mvp §8;
giai-doan-2 mục 6b, mục 10 A3/A4), chỗ điền thay bằng giá trị thật. Ba phần:

- T1 (thuần, không socket): `parse_rate`, `transfer_result_message`, `offer_prompt_lines`,
  `progress_line`, `source_error_message`, `receive_dir_error_message`; lỗi tham số → mã 2.
- T2 (trong tiến trình, `integration`): `run_send` / `run_receive` trên 127.0.0.1 qua `cli_runs`
  (tests/conftest.py), Ctrl+C giả bằng `Interrupts.press()`; I1–I14 và các ca thêm của plan mục 6.
- T3 (hai tiến trình con thật, `integration`): P1–P5.

`teleport.cli` và các module P2-7 chỉ được import trong fixture / thân test, để thiếu một tên chỉ làm
đỏ test dùng tới nó.
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import errno
import hashlib
import io
import itertools
import json
import os
import re
import socket
import sys
import time
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.lock import NodeLock, NodeLockedError
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore

LOCAL = "127.0.0.1"
SENDER = "Laptop-Minh"  # tên máy gửi (trong trusted.json của máy nhận)
RECEIVER = "PC-Nha"  # tên máy nhận (trong trusted.json của máy gửi)
SENDER_ID = "a71c04d2" + "1" * 24
RECEIVER_ID = "b0" * 16
TID = "c3" * 16
KiB = 1024
MiB = 1024**2
GiB = 1024**3
TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

# --- Câu thông báo đã duyệt ------------------------------------------------------------------------
# Bên gửi ({p} = máy nhận)
S_DONE = "Đã gửi xong {n} file ({size}) tới {p}."  # C10
S_FILE_DONE = "Đã gửi {file} ({i}/{n})."  # C10
S_REJECTED = "{p} đã từ chối nhận file."  # mvp §8, A4
S_NO_SPACE = "{p} không đủ chỗ trống để nhận: cần {needed}, ổ lưu file trên máy đó còn {available}."  # M1
S_TOO_LARGE = (  # mvp §8
    "{p} không nhận được {file} ({size}): ổ lưu file trên máy đó chỉ chứa được file nhỏ hơn {limit}."
)
S_NOT_RECEIVING = "{p} chưa bật nhận file. Trên máy đó hãy chạy `teleport receive` rồi gửi lại."  # A3
S_PEER_STOPPED = "{p} đã dừng nhận giữa chừng. Các file đã gửi xong vẫn nằm trên máy đó."  # 6b
S_SELF_CANCELLED = "Đã hủy lượt gửi tới {p}. Các file đã gửi xong vẫn nằm trên máy đó."  # M2
S_SOURCE_CHANGED = "{file} đã bị thay đổi trên máy gửi nên không thể gửi tiếp. Hãy gửi lại."  # mvp §8
S_EXPIRED = "Mất kết nối với {p} quá lâu nên lượt gửi đã dừng. Hãy gửi lại."  # M3
S_SOURCE_READ = (  # 6b
    "Không đọc được {file} trên máy này nữa (file có thể đang bị chương trình khác khóa, hoặc nằm trên "
    "ổ vừa bị rút ra). Đã dừng gửi tới {p}."
)
S_PEER_ERROR = (  # M16 (spec-p2-8 mục A; thay câu 6b “… không lưu được {file} …” ở ba dòng mvp §8)
    "{p} gặp lỗi khi nhận nên đã dừng lượt gửi. Hãy xem thông báo trên máy đó rồi gửi lại."
)
S_STOPPED = "{p} đã dừng lượt gửi."  # A4
PROTOCOL = (  # M4 (hai bên)
    "Lượt gửi với {x} đã dừng vì máy đó gửi dữ liệu không hợp lệ. Hãy cập nhật Teleport trên cả hai máy "
    "rồi thử lại."
)
# Bên nhận ({s} = máy gửi)
R_DONE = "Đã nhận xong {n} file ({size}) từ {s}."  # C11
R_SAVED = "Đã lưu: {path}"  # C11
R_REJECTED_SELF = "Đã từ chối."  # C6
R_NO_SPACE = "Đã từ chối {n} file từ {s}: không đủ chỗ trống, cần {needed}, {drive} còn {available}."  # M5
R_TOO_LARGE = (  # mvp §8 + M6
    "Đã từ chối {n} file từ {s}: {drive} chỉ chứa được file nhỏ hơn {limit}, mà {file} nặng {size}."
)
R_SENDER_CANCELLED = (  # 6b
    "{s} đã hủy lượt gửi. Các file chưa nhận xong đã được xóa; file đã nhận xong vẫn giữ nguyên."
)
R_SENDER_CANCELLED_WAITING = "{s} đã hủy lượt gửi."  # M7
R_SELF_CANCELLED = "Đã hủy nhận từ {s}. File đã nhận xong vẫn giữ nguyên."  # M8
R_SOURCE_CHANGED = (  # M9
    "{file} đã bị thay đổi trên {s} nên không nhận tiếp được. Các file đã nhận xong vẫn giữ nguyên; "
    "hãy nhờ gửi lại."
)
R_EXPIRED = (  # M10
    "Mất kết nối với {s} quá lâu nên đã dừng nhận. Các file chưa nhận xong đã được xóa; file đã nhận "
    "xong vẫn giữ nguyên."
)
R_DISK_FAILED = (  # 6b
    "Không lưu được {file} vào {dir} vì ổ đĩa báo lỗi khi ghi (ổ có thể đã đầy, bị rút ra hoặc bị hỏng). "
    "Đã dừng nhận từ {s}; các file đã nhận xong vẫn giữ nguyên."
)
R_TEMP_EXISTS = (  # M11
    "Không nhận được lượt gửi từ {s} vì thư mục tạm của lượt này còn sót lại từ lần trước (ví dụ "
    "Teleport vừa bị tắt đột ngột). Hãy nhờ gửi lại."
)
R_CORRUPT = (  # M12
    "Dữ liệu nhận từ {s} bị hỏng nhiều lần liên tiếp nên đã dừng nhận {file}. Các file đã nhận xong vẫn "
    "giữ nguyên; hãy nhờ gửi lại."
)
R_PEER_ERROR = (  # 6b
    "{s} đã dừng gửi {file} vì gặp lỗi trên máy đó. Các file đã nhận xong vẫn giữ nguyên; hãy nhờ gửi lại."
)
R_UNKNOWN = "{s} đã hủy lượt gửi {file}. Hãy nhờ gửi lại."  # mvp §8
R_STOPPED = "{s} đã dừng lượt gửi."  # A4
# Trạng thái
C7_WAITING = "Đang chờ {p} trả lời…"
A4_QUEUED = "Đang chờ {p} nhận xong lượt trước…"
RECONNECTING = "Đang nối lại…"
C8_VERIFYING = "Đang kiểm tra {file}…"
M13_DATA_UNREACHABLE = (
    "Không mở được kết nối truyền dữ liệu tới {x}; tường lửa trên máy đó có thể đang chặn Teleport. "
    "Teleport vẫn đang thử lại."
)
# Lỗi trước khi gửi / lúc khởi động
B1 = "Tốc độ không hợp lệ: “{x}”. Hãy nhập ví dụ 20M (20 MB/s) hoặc 512K; tối thiểu 64K."
B2 = "{x} không phải địa chỉ IPv4 hợp lệ. Hãy nhập ví dụ 192.168.1.5 hoặc 192.168.1.5:47474."
B3 = "Mỗi lần gửi được tối đa {n} file."
B4_SUFFIX = " không phải là file. Teleport chưa gửi được cả thư mục; hãy chọn từng file."
B5 = "Có file được chọn hai lần. Hãy chọn mỗi file một lần."
B6A = "Không tìm thấy file {path}."
B6B = "Không đọc được {file}: ổ đĩa báo lỗi."
PERMISSION = "Không mở được {file}: máy này không có quyền đọc file đó."  # 6b
TOO_LARGE_SOURCE = (  # mvp §8
    "Không gửi được {file} ({size}): Teleport chỉ gửi được file tối đa 4 TB. Hãy chia file thành nhiều "
    "phần nhỏ hơn rồi gửi."
)
BAD_NAME = (  # mvp §8
    "Không gửi được {file}: tên file có ký tự không hiển thị được. Hãy đổi tên file rồi gửi lại."
)
LOCKED = "Teleport đang chạy với thư mục cấu hình này."  # câu đã có trong cli.py
B7 = "Hãy dừng `teleport receive` hoặc `teleport listen` đang chạy rồi thử lại."
B8A = (
    "Không thấy {x} trong mạng. Hãy kiểm tra máy đó đang chạy `teleport receive` và hai máy dùng chung "
    "một mạng; nếu mạng chặn mDNS, hãy thêm `--address <ip[:cổng]>`."
)
B8B_PREFIX = "Không tìm được {x} vì không bật được khám phá mDNS ("
B8B_SUFFIX = "). Hãy thêm `--address <ip[:cổng]>`."
B9 = "Không dùng được thư mục nhận {dir}: {why}"
B9A = (
    "thư mục ẩn `.teleport` bên trong là liên kết hoặc thuộc người dùng khác. Hãy xóa nó hoặc chọn thư "
    "mục khác bằng `--dir`."
)
B9B = "đường dẫn quá dài. Hãy chọn thư mục có đường dẫn ngắn hơn bằng `--dir`."
B9C = "máy này không có quyền ghi vào đó. Hãy chọn thư mục khác bằng `--dir`."
B9D = "ổ đĩa báo lỗi hoặc không phản hồi. Hãy chọn thư mục khác bằng `--dir`."
B9E = (  # spec-p2-8 A
    "đó không phải là thư mục (có thể là một file trùng tên). Hãy chọn thư mục khác bằng `--dir`."
)
B10 = "Cảnh báo: đang tự nhận mọi file từ mọi thiết bị đã ghép đôi, không hỏi trước (`--yes`)."
B11 = (
    "Máy này chưa ghép đôi với thiết bị nào nên chưa nhận được file. Hãy ghép đôi trước: `teleport listen` "
    "trên một máy, `teleport pair` trên máy kia."
)
B12_PREFIX = "Cảnh báo: chưa quảng bá được qua mDNS ("
B12_SUFFIX = "); máy khác vẫn gửi được bằng `teleport send … --address <IP của máy này>`."
B13 = "Không đọc được câu trả lời (đầu vào đã đóng) nên đã từ chối. Hãy dùng `--yes` để tự nhận."
M14 = "Đã từ chối {n} file từ {s} vì không đọc được câu trả lời (đầu vào đã đóng)."  # spec-p2-8 A
M15 = "Đã từ chối {n} file từ {s} vì đang dừng nhận."  # spec-p2-8 A
C1_HEAD = "Teleport đang chờ nhận file."
C1_TAIL = "Nhấn Ctrl+C để dừng."
C2_SEARCH = "Đang tìm “{x}” trong mạng…"
C2_CONNECT = "Đang kết nối tới “{x}” tại {addr}…"
C2_LIMIT = "Giới hạn tốc độ gửi: {rate}/s."
C3 = "Đã kết nối với “{x}”. Gửi {n} file ({size})."
C4_HEAD = "“{s}” muốn gửi {n} file ({size}):"
C4_ASK = "Nhận? [c/k]: "
C5 = "  … và {n} file khác"
C6_HINT = "Hãy gõ c (nhận) hoặc k (từ chối)."
C12 = "Đang dừng… (nhấn Ctrl+C lần nữa để thoát ngay)"
CANCELLED = "Đã hủy."
STOPPED = "Đã dừng."
MVP_UNREACHABLE_ADDRESS = (
    "Không kết nối được tới {target}. Hãy kiểm tra Teleport đang chạy trên máy đó, hai máy dùng chung "
    "một mạng, và tường lửa cho phép Teleport."
)
MVP_REJECTED = "{x} từ chối kết nối từ máy này. Có thể máy đó đã hủy ghép đôi."
MVP_SEEN_UNREACHABLE = (
    "Thấy {x} nhưng không kết nối được. Mạng Wi-Fi này có thể đang chặn các thiết bị nói chuyện "
    "với nhau (hay gặp ở quán cà phê, khách sạn)."
)
UNPAIR_NOT_FOUND = "Không có thiết bị tin cậy nào khớp “{q}”."
UNPAIR_AMBIGUOUS = "“{q}” khớp nhiều thiết bị; hãy nhập mã thiết bị dài hơn:"

RESULT_FIELDS = {
    "event",
    "time",
    "transfer_id",
    "direction",
    "device_id",
    "device_name",
    "result",
    "by_peer",
    "reason",
    "file_index",
    "files_done",
    "file_count",
    "done_bytes",
    "total_bytes",
    "message",
}
PROGRESS_FIELDS = {
    "event",
    "time",
    "transfer_id",
    "direction",
    "device_id",
    "done_bytes",
    "total_bytes",
    "percent",
    "bytes_per_second",
    "eta_seconds",
    "file_index",
    "file_count",
}

integration = pytest.mark.integration
socket_test = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cli_no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
]


def with_marks(marks):
    def deco(fn):
        for mark in reversed(marks):
            fn = mark(fn)
        return fn

    return deco


# =================================================================================================
# Hàm phụ trợ
# =================================================================================================


def columns(text: str) -> int:
    """Độ rộng theo cột, đo bằng `core.display._width` (spec-p2-7 mục B, T1)."""
    from teleport.core import display

    return sum(display._width(ch) for ch in text)


def drive_label(directory: Path) -> str:
    """Cách gọi ổ chứa thư mục nhận (M6): Windows “ổ D:” / “ổ mạng \\\\máy\\chia-sẻ”; nơi khác
    “ổ chứa <thư mục nhận>”."""
    if sys.platform == "win32":
        drive, _ = os.path.splitdrive(str(directory))
        if drive.startswith("\\\\"):
            return f"ổ mạng {drive}"
        return f"ổ {drive}"
    return f"ổ chứa {directory}"


def as_lines(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    return list(value)


def make_file(directory: Path, name: str, size: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with open(path, "wb") as f:
        left = size
        while left:
            chunk = os.urandom(min(left, 4 * MiB))
            f.write(chunk)
            left -= len(chunk)
    return path


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def lock_is_free(directory) -> bool:
    lock = NodeLock(directory)
    try:
        lock.acquire()
    except NodeLockedError:
        return False
    lock.release()
    return True


def closed_port() -> int:
    """Một cổng TCP vừa được nhả trên 127.0.0.1 (không ai nghe)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((LOCAL, 0))
        return s.getsockname()[1]


def parse_time(text: str) -> datetime.datetime:
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.UTC)


async def wait_until(predicate, timeout: float = 5.0, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.01)


def run_main(argv: list[str]) -> int:
    from teleport.cli import main

    try:
        return main(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


def json_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class TtyOut(io.StringIO):
    """`StringIO` giả TTY; ghi lại (thời điểm, đoạn) của mỗi lần ghi."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[float, str]] = []

    def isatty(self) -> bool:
        return True

    def write(self, s: str) -> int:
        self.writes.append((time.monotonic(), s))
        return super().write(s)


# =================================================================================================
# T1 — hàm thuần
# =================================================================================================


@pytest.fixture(scope="module")
def cli():
    from teleport import cli as cli_mod

    return cli_mod


@pytest.fixture(scope="module")
def T():
    from teleport.app import transfers

    return transfers


# --- parse_rate (plan 2.1) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("64K", 65536),
        ("64k", 65536),
        ("64KB", 65536),
        ("64kb/s", 65536),
        ("64K/s", 65536),
        ("  64K  ", 65536),
        ("64 K", 65536),
        ("65536", 65536),
        ("65536.9", 65536),  # làm tròn xuống
        ("512K", 512 * KiB),
        ("20M", 20 * MiB),
        ("1,5M", 3 * MiB // 2),
        ("1.5M", 3 * MiB // 2),
        ("0,0625M", 65536),
        ("64.5K", 66048),
        ("1G", GiB),
        ("1024G", 2**40),  # trần 2⁴⁰ (bao gồm)
        ("1,0000001M", MiB),  # floor
    ],
)
def test_parse_rate_accepts_binary_units(cli, text, expected):
    assert cli.parse_rate(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "63K",
        "65535",
        "0",
        "0K",
        "nan",
        "inf",
        "1e9",
        "1T",
        "1025G",
        "",
        "   ",
        "-64K",
        "64KK",
        "K",
        "64 K B/s/s",
        "６４K",  # chữ số toàn chiều rộng (re.ASCII)
        "٦٤K",  # chữ số Ả Rập
        "64M/h",
        "1,5,5M",
    ],
)
def test_parse_rate_rejects_out_of_range_or_malformed(cli, text):
    with pytest.raises(ValueError):
        cli.parse_rate(text)


def test_parse_rate_minimum_follows_min_rate_limit(cli, T):
    assert cli.parse_rate(str(T.MIN_RATE_LIMIT)) == T.MIN_RATE_LIMIT
    with pytest.raises(ValueError):
        cli.parse_rate(str(T.MIN_RATE_LIMIT - 1))


# --- transfer_result_message (plan mục 4) -----------------------------------------------------------

NAMES = ("video.mp4", "thumbnail.png", "script.docx")
SIZES = (8 * GiB, int(2.1 * MiB), 380 * KiB)  # tổng 8,0 GB
TOTAL = sum(SIZES)
TOTAL_TEXT = "8,0 GB"


def make_info(
    direction: str,
    *,
    status: str = "transferring",
    names=NAMES,
    sizes=SIZES,
    file_index: int = 0,
    done_bytes: int = 0,
    device_id: str | None = None,
):
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import Direction, TransferInfo, TransferKey, TransferStatus

    d = Direction(direction)
    if device_id is None:
        device_id = RECEIVER_ID if d is Direction.SEND else SENDER_ID
    files = tuple(FileEntry(name=n, size=s, mtime=0) for n, s in zip(names, sizes, strict=True))
    return TransferInfo(
        key=TransferKey(device_id=device_id, direction=d, transfer_id=TID),
        status=TransferStatus(status),
        files=files,
        names=tuple(names),
        file_index=file_index,
        done_bytes=done_bytes,
        total_bytes=sum(sizes),
    )


def finished(
    direction: str,
    result: str,
    *,
    by_peer: bool = False,
    reason: str | None = None,
    file_index: int | None = None,
    reject=None,
    file_index_info: int | None = None,
    **info_kw,
):
    """`TransferFinished`; `file_index_info` là `TransferInfo.file_index` (mặc định 0)."""
    from teleport.app.transfers import TransferFinished, TransferResult

    return TransferFinished(
        info=make_info(direction, file_index=file_index_info or 0, **info_kw),
        result=TransferResult(result),
        by_peer=by_peer,
        reason=reason,
        file_index=file_index,
        reject=reject,
    )


def reject(reason: str, **kw):
    from teleport.core.messages import Reject

    return Reject(transfer_id=TID, reason=reason, **kw)


RECV_DIR = (
    Path(r"D:\Downloads\Teleport") if sys.platform == "win32" else Path("/home/minh/Downloads/Teleport")
)


def message(cli, fin, *, peer, receive_dir=RECV_DIR) -> list[str]:
    return as_lines(cli.transfer_result_message(fin, peer_name=peer, receive_dir=receive_dir))


SEND_CASES = [
    # (result, by_peer, file_index, reject kwargs, câu mong đợi)
    ("done", False, None, None, S_DONE.format(n=3, size=TOTAL_TEXT, p=RECEIVER)),
    ("rejected_user", True, None, ("user", {}), S_REJECTED.format(p=RECEIVER)),
    (
        "rejected_no_space",
        True,
        None,
        ("no_space", {"needed": int(12.4 * GiB), "available": int(8.1 * GiB)}),
        S_NO_SPACE.format(p=RECEIVER, needed="12,4 GB", available="8,1 GB"),
    ),
    (
        "rejected_file_too_large",
        True,
        0,
        ("file_too_large", {"file_index": 0, "max_size": (1 << 32) - 1}),
        S_TOO_LARGE.format(p=RECEIVER, file="video.mp4", size="8,0 GB", limit="4 GB"),
    ),
    ("rejected_not_receiving", True, None, ("not_receiving", {}), S_NOT_RECEIVING.format(p=RECEIVER)),
    ("rejected_busy", True, None, ("busy", {}), S_REJECTED.format(p=RECEIVER)),
    ("rejected_other", True, None, ("la_qua", {}), S_REJECTED.format(p=RECEIVER)),
    ("cancelled_user", True, None, None, S_PEER_STOPPED.format(p=RECEIVER)),
    ("cancelled_user", False, None, None, S_SELF_CANCELLED.format(p=RECEIVER)),
    ("source_changed", False, 1, None, S_SOURCE_CHANGED.format(file="thumbnail.png")),
    ("expired", False, None, None, S_EXPIRED.format(p=RECEIVER)),
    ("source_read_failed", False, 2, None, S_SOURCE_READ.format(file="script.docx", p=RECEIVER)),
    ("peer_error", True, 1, None, S_PEER_ERROR.format(p=RECEIVER)),  # M16
    ("protocol_error", False, None, None, PROTOCOL.format(x=RECEIVER)),
    ("protocol_error", True, None, None, PROTOCOL.format(x=RECEIVER)),
    ("unknown_transfer", True, None, None, S_STOPPED.format(p=RECEIVER)),
    ("cancelled_other", True, None, None, S_STOPPED.format(p=RECEIVER)),
    ("unpaired", False, None, None, S_STOPPED.format(p=RECEIVER)),
    ("corrupt", False, None, None, S_STOPPED.format(p=RECEIVER)),
]


@pytest.mark.parametrize(
    ("result", "by_peer", "file_index", "rej", "expected"),
    SEND_CASES,
    ids=[f"{c[0]}-{'peer' if c[1] else 'self'}" for c in SEND_CASES],
)
def test_transfer_result_message_sender(cli, result, by_peer, file_index, rej, expected):
    r = None if rej is None else reject(rej[0], **rej[1])
    fin = finished("send", result, by_peer=by_peer, file_index=file_index, reject=r, file_index_info=None)
    assert message(cli, fin, peer=RECEIVER) == [expected]


RECV_CASES = [
    ("done", False, None, None, "transferring", R_DONE.format(n=3, size=TOTAL_TEXT, s=SENDER)),
    ("rejected_user", False, None, ("user", {}), "waiting", R_REJECTED_SELF),
    (
        "rejected_no_space",
        False,
        None,
        ("no_space", {"needed": int(12.4 * GiB), "available": int(8.1 * GiB)}),
        "waiting",
        R_NO_SPACE.format(n=3, s=SENDER, needed="12,4 GB", drive=drive_label(RECV_DIR), available="8,1 GB"),
    ),
    (
        "rejected_file_too_large",
        False,
        0,
        ("file_too_large", {"file_index": 0, "max_size": (1 << 32) - 1}),
        "waiting",
        R_TOO_LARGE.format(
            n=3, s=SENDER, drive=drive_label(RECV_DIR), limit="4 GB", file="video.mp4", size="8,0 GB"
        ),
    ),
    ("cancelled_user", True, None, None, "transferring", R_SENDER_CANCELLED.format(s=SENDER)),
    ("cancelled_user", True, None, None, "waiting", R_SENDER_CANCELLED_WAITING.format(s=SENDER)),
    ("cancelled_user", False, None, None, "transferring", R_SELF_CANCELLED.format(s=SENDER)),
    (
        "source_changed",
        True,
        1,
        None,
        "transferring",
        R_SOURCE_CHANGED.format(file="thumbnail.png", s=SENDER),
    ),
    ("expired", False, None, None, "reconnecting", R_EXPIRED.format(s=SENDER)),
    (
        "disk_failed",
        False,
        1,
        None,
        "transferring",
        R_DISK_FAILED.format(file="thumbnail.png", dir=RECV_DIR, s=SENDER),
    ),
    ("temp_exists", False, None, None, "transferring", R_TEMP_EXISTS.format(s=SENDER)),
    ("corrupt", False, 2, None, "verifying", R_CORRUPT.format(s=SENDER, file="script.docx")),
    ("peer_error", True, 0, None, "transferring", R_PEER_ERROR.format(s=SENDER, file="video.mp4")),
    ("unknown_transfer", True, 1, None, "reconnecting", R_UNKNOWN.format(s=SENDER, file="thumbnail.png")),
    ("protocol_error", False, None, None, "transferring", PROTOCOL.format(x=SENDER)),
    ("cancelled_other", False, None, None, "transferring", R_STOPPED.format(s=SENDER)),
    ("unpaired", False, None, None, "transferring", R_STOPPED.format(s=SENDER)),
]


@pytest.mark.parametrize(
    ("result", "by_peer", "file_index", "rej", "status", "expected"),
    RECV_CASES,
    ids=[f"{c[0]}-{'peer' if c[1] else 'self'}-{c[4]}" for c in RECV_CASES],
)
def test_transfer_result_message_receiver(cli, result, by_peer, file_index, rej, status, expected):
    r = None if rej is None else reject(rej[0], **rej[1])
    fin = finished("receive", result, by_peer=by_peer, file_index=file_index, reject=r, status=status)
    assert message(cli, fin, peer=SENDER) == [expected]


@pytest.mark.parametrize(
    ("result", "rej"),
    [("rejected_busy", "busy"), ("rejected_other", "la_qua"), ("rejected_not_receiving", "not_receiving")],
)
def test_transfer_result_message_receiver_silent_rejections(cli, result, rej):
    """Bên nhận không in gì cho busy/lý do lạ/not_receiving (chỉ JSON, `message` rỗng; spec A20)."""
    fin = finished("receive", result, by_peer=False, reject=reject(rej), status="waiting")
    assert message(cli, fin, peer=SENDER) == []


@pytest.mark.parametrize("status", ["transferring", "verifying", "reconnecting"])
@pytest.mark.parametrize(("file_index", "info_index"), [(None, 0), (0, 0), (2, 1), (None, 3)])
def test_m16_sender_sentence_for_receiver_error_names_no_file(cli, status, file_index, info_index):
    """M16 (spec-p2-8 A): bên nhận gửi `cancel{error}` (lỗi ghi đĩa, dữ liệu hỏng, thư mục tạm còn sót —
    máy gửi không phân biệt được) → máy gửi in đúng một câu M16, không nêu tên file, không còn câu 6b cũ."""
    fin = finished(
        "send",
        "peer_error",
        by_peer=True,
        file_index=file_index,
        file_index_info=info_index,
        status=status,
    )
    lines = message(cli, fin, peer=RECEIVER)
    assert lines == [S_PEER_ERROR.format(p=RECEIVER)]
    assert "không lưu được" not in lines[0]
    assert not any(name in lines[0] for name in NAMES)


@pytest.mark.parametrize(
    ("file_index", "info_index", "expected_file"),
    [(1, 0, "thumbnail.png"), (None, 0, "video.mp4"), (None, 1, "thumbnail.png"), (None, 3, "script.docx")],
    ids=["tu-ket-qua", "info-0", "info-1", "info-bang-n-kep-ve-cuoi"],
)
def test_transfer_result_message_file_comes_from_finished_then_info_clamped(
    cli, file_index, info_index, expected_file
):
    fin = finished("send", "source_changed", file_index=file_index, file_index_info=info_index)
    assert message(cli, fin, peer=RECEIVER) == [S_SOURCE_CHANGED.format(file=expected_file)]


def test_transfer_result_message_without_peer_name_uses_short_device_id(cli):
    fin = finished("send", "rejected_user", by_peer=True, reject=reject("user"))
    assert message(cli, fin, peer=None) == [S_REJECTED.format(p=RECEIVER_ID[:8])]


def test_transfer_result_message_shows_peer_name_through_display_name(cli):
    from teleport.core.display import display_name

    raw = "PC\u00a0Nha\u3000"
    fin = finished("send", "rejected_user", by_peer=True, reject=reject("user"))
    [line] = message(cli, fin, peer=raw)
    assert line == S_REJECTED.format(p=display_name(raw))
    assert "\u00a0" not in line and "\u3000" not in line


@pytest.mark.skipif(sys.platform != "win32", reason="ổ mạng UNC chỉ có trên Windows")
def test_transfer_result_message_receiver_names_unc_share_as_network_drive(cli):
    share = Path(r"\\may-chu\chia-se\Teleport")
    fin = finished(
        "receive",
        "rejected_no_space",
        reject=reject("no_space", needed=int(12.4 * GiB), available=int(8.1 * GiB)),
        status="waiting",
    )
    [line] = message(cli, fin, peer=SENDER, receive_dir=share)
    assert line == R_NO_SPACE.format(
        n=3, s=SENDER, needed="12,4 GB", drive=r"ổ mạng \\may-chu\chia-se", available="8,1 GB"
    )


_RESULTS = [
    "done",
    "rejected_user",
    "rejected_no_space",
    "rejected_file_too_large",
    "rejected_busy",
    "rejected_not_receiving",
    "rejected_other",
    "unpaired",
    "cancelled_user",
    "source_changed",
    "expired",
    "disk_failed",
    "temp_exists",
    "corrupt",
    "protocol_error",
    "source_read_failed",
    "peer_error",
    "unknown_transfer",
    "cancelled_other",
]
_REJECT_FOR = {
    "rejected_user": ("user", {}),
    "rejected_no_space": ("no_space", {"needed": 5, "available": 1}),
    "rejected_file_too_large": ("file_too_large", {"file_index": 0, "max_size": 10}),
    "rejected_busy": ("busy", {}),
    "rejected_not_receiving": ("not_receiving", {}),
    "rejected_other": ("khac", {}),
}
_PEER_NAMES = st.one_of(
    st.none(),
    st.sampled_from(["PC-Nha", "PC\u00a0Nha", "中文電腦", "x" * 64]),
)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    result=st.sampled_from(_RESULTS),
    direction=st.sampled_from(["send", "receive"]),
    by_peer=st.booleans(),
    status=st.sampled_from(["queued", "waiting", "transferring", "verifying", "reconnecting"]),
    file_index=st.sampled_from([None, 0, 2]),
    info_index=st.integers(0, 3),
    peer=_PEER_NAMES,
)
def test_transfer_result_message_never_crashes_and_is_clean(
    cli, result, direction, by_peer, status, file_index, info_index, peer
):
    """Mọi `TransferResult` × chiều × `by_peer`: trả danh sách câu, không ném, không ký tự điều khiển."""
    rej = _REJECT_FOR.get(result)
    fin = finished(
        direction,
        result,
        by_peer=by_peer,
        file_index=file_index,
        reject=None if rej is None else reject(rej[0], **rej[1]),
        status=status,
        file_index_info=info_index,
    )
    lines = message(cli, fin, peer=peer)
    assert all(isinstance(line, str) for line in lines)
    for line in lines:
        assert not any(unicodedata.category(ch) == "Cc" for ch in line), line


# --- Interrupts và seam (plan 2.4, mục 3) -----------------------------------------------------------


async def test_interrupts_first_press_sets_stop_second_sets_hard(cli):
    i = cli.Interrupts()
    assert not i.stop.is_set() and not i.hard.is_set()
    i.press()
    assert i.stop.is_set() and not i.hard.is_set()
    i.press()
    assert i.stop.is_set() and i.hard.is_set()


def test_module_seams_have_planned_defaults(cli):
    assert cli._SEND_CONNECT_TIMEOUT == 20
    assert cli._PLAIN_PROGRESS_INTERVAL == 10
    assert cli._OFFER_LIST_MAX == 10
    width = cli._terminal_width()
    assert isinstance(width, int) and width > 0


# --- offer_prompt_lines (plan 2.5, mvp §7.3 bản CLI, C4/C5) -------------------------------------------


def make_offer(names, sizes, *, device_name=SENDER, auto=False):
    from teleport.app.transfers import OfferReceived

    info = make_info("receive", names=names, sizes=sizes)
    return OfferReceived(
        key=info.key,
        device_name=device_name,
        files=info.files,
        names=tuple(names),
        total_bytes=sum(sizes),
        auto_accepted=auto,
    )


def prompt_lines(cli, offer, *, peer=SENDER, receive_dir=RECV_DIR) -> list[str]:
    lines = as_lines(cli.offer_prompt_lines(offer, peer_name=peer, receive_dir=receive_dir))
    if lines and lines[-1].strip() == C4_ASK.strip():
        lines = lines[:-1]  # câu hỏi có thể nằm trong danh sách hoặc do `ask` in riêng
    return lines


def test_offer_prompt_matches_mvp_layout(cli):
    lines = prompt_lines(cli, make_offer(NAMES, SIZES))
    assert lines[0] == C4_HEAD.format(s=SENDER, n=3, size=TOTAL_TEXT)
    assert lines[-1] == f"Lưu vào: {RECV_DIR}"
    body = lines[1:-1]
    assert len(body) == 3
    for line, name, size in zip(body, NAMES, ("8,0 GB", "2,1 MB", "380 KB"), strict=True):
        assert re.fullmatch(rf"  {re.escape(name)} +{re.escape(size)}", line), line


@pytest.mark.parametrize(("count", "shown", "more"), [(10, 10, None), (11, 10, 1), (1000, 10, 990)])
def test_offer_prompt_lists_at_most_ten_files_then_count_of_others(cli, count, shown, more):
    names = [f"f{i:04d}.bin" for i in range(count)]
    lines = prompt_lines(cli, make_offer(names, [1] * count))
    body = lines[1:-1]
    files = [line for line in body if re.fullmatch(r"  f\d{4}\.bin +\d+ byte", line)]
    assert [line.split()[0] for line in files] == names[:shown]
    rest = [line for line in body if line not in files]
    assert rest == ([] if more is None else [C5.format(n=more)])


def test_offer_prompt_reads_offer_list_max_at_call_time(cli, monkeypatch):
    monkeypatch.setattr(cli, "_OFFER_LIST_MAX", 3)
    names = [f"f{i}.bin" for i in range(5)]
    body = prompt_lines(cli, make_offer(names, [1] * 5))[1:-1]
    assert len(body) == 4 and body[-1] == C5.format(n=2)


def test_offer_prompt_shows_sender_through_display_name_in_quotes(cli):
    from teleport.core.display import display_name

    raw = "Laptop Minh" + "x" * 50
    lines = prompt_lines(cli, make_offer(NAMES, SIZES, device_name=raw), peer=raw)
    assert lines[0] == C4_HEAD.format(s=display_name(raw), n=3, size=TOTAL_TEXT)


def test_offer_prompt_file_names_cannot_fake_the_save_line(cli):
    """Tên file do máy kia đặt chỉ nằm trong dòng thụt lề; dòng “Lưu vào:” thật là dòng cuối, duy nhất."""
    names = ["Lưu vào: C-Windows", "Nhận? [c-k]", "a" * 200 + ".mp4"]
    lines = prompt_lines(cli, make_offer(names, [1, 2, 3]))
    assert [line for line in lines if line.startswith("Lưu vào:")] == [f"Lưu vào: {RECV_DIR}"]
    assert lines[-1] == f"Lưu vào: {RECV_DIR}"
    for line in lines[1:-1]:
        assert line.startswith("  ")
    long_line = lines[3]
    assert "a" * 100 not in long_line and "…" in long_line and ".mp4" in long_line


# --- progress_line (plan 2.5, C9) ---------------------------------------------------------------------

P_NAMES = ("video.mp4", "b.png", "c.docx")
P_SIZES = (600 * 2**24, 200 * 2**24, 200 * 2**24)  # tổng 15,625 GiB → “15,6 GB”
P_DONE = 420 * 2**24  # 42,0 % đúng; 6,5625 GiB → “6,6 GB”
P_LINE = "42,0% · 6,6 GB / 15,6 GB · 85,2 MB/s · còn 1 phút 2 giây · video.mp4 (1/3)"


def make_progress(
    *, done=P_DONE, speed=int(85.2 * MiB), eta=62.0, file_index=0, names=P_NAMES, direction="send"
):
    from teleport.app.transfers import TransferProgress

    info = make_info(direction, names=names, sizes=P_SIZES, file_index=file_index, done_bytes=done)
    return TransferProgress(info=info, bytes_per_second=speed, eta=eta)


def test_progress_line_matches_c9(cli):
    assert cli.progress_line(make_progress(), peer_name=None, width=200).strip() == P_LINE


def test_progress_line_without_eta_says_still_estimating(cli):
    """spec A18: lần báo đầu tốc độ 0, `eta` None → “đang tính thời gian còn lại”."""
    line = cli.progress_line(make_progress(done=0, speed=0.0, eta=None), peer_name=None, width=200)
    assert line.strip().startswith("0,0% · 0 byte / 15,6 GB")
    assert "đang tính thời gian còn lại" in line
    assert not re.search(r"· còn \d", line)


@pytest.mark.parametrize(
    ("index", "expected"), [(0, "video.mp4 (1/3)"), (1, "b.png (2/3)"), (3, "c.docx (3/3)")]
)
def test_progress_line_file_position_is_clamped(cli, index, expected):
    line = cli.progress_line(make_progress(file_index=index), peer_name=None, width=200)
    assert line.rstrip().endswith(expected), line


def test_progress_line_prefixes_sender_name_on_receive(cli):
    line = cli.progress_line(make_progress(direction="receive"), peer_name=SENDER, width=200)
    assert SENDER in line and line.rstrip().endswith(P_LINE)


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    width=st.integers(min_value=20, max_value=200),
    name=st.text(
        alphabet=st.characters(exclude_categories=("Cc", "Cs", "Mn", "Me")), min_size=1, max_size=120
    ).filter(lambda s: s.strip() == s),
    peer=st.one_of(st.none(), st.sampled_from([SENDER, "中文電腦名字很長很長很長", "x" * 64])),
)
def test_progress_line_fits_width_measured_in_columns(cli, width, name, peer):
    """Độ rộng đo bằng `core.display._width` (spec-p2-7 mục B, T1), không bằng `len`."""
    progress = make_progress(names=(name, "b.png", "c.docx"))
    line = cli.progress_line(progress, peer_name=peer, width=width)
    assert columns(line) <= width, (width, line)
    assert "\r" not in line and "\n" not in line and "\x1b" not in line


# --- source_error_message (plan mục 4: B4–B6, mvp §8, 6b) ----------------------------------------------


def source_error(kind: str, path: Path, **kw):
    from teleport.app.sources import SourceError, SourceErrorKind

    return SourceError(path, SourceErrorKind(kind), **kw)


def test_source_error_not_found_names_full_path(cli, tmp_path):
    path = tmp_path / "x" / "video.mp4"
    code, err, lines = cli.source_error_message(source_error("not_found", path))
    assert (code, err, as_lines(lines)) == (1, "source_not_found", [B6A.format(path=path)])


def test_source_error_directory_is_usage_error(cli, tmp_path):
    path = tmp_path / "ảnh"
    code, err, lines = cli.source_error_message(source_error("not_a_file", path))
    [line] = as_lines(lines)
    assert (code, err) == (2, "not_a_file")
    assert line.endswith(B4_SUFFIX)
    assert line[: -len(B4_SUFFIX)] in ("ảnh", str(path)), line


def test_source_error_permission(cli, tmp_path):
    code, err, lines = cli.source_error_message(source_error("permission", tmp_path / "video.mp4"))
    assert (code, err, as_lines(lines)) == (1, "source_permission", [PERMISSION.format(file="video.mp4")])


def test_source_error_too_large(cli, tmp_path):
    e = source_error("too_large", tmp_path / "backup.img", size=int(5.2 * 1024**4))
    code, err, lines = cli.source_error_message(e)
    assert (code, err, as_lines(lines)) == (
        1,
        "source_too_large",
        [TOO_LARGE_SOURCE.format(file="backup.img", size="5,2 TB")],
    )


def test_source_error_bad_name_shows_control_chars_as_replacement(cli, tmp_path):
    e = source_error("bad_name", tmp_path / "bao\ncao.txt")
    code, err, lines = cli.source_error_message(e)
    assert (code, err, as_lines(lines)) == (1, "source_bad_name", [BAD_NAME.format(file="bao�cao.txt")])


def test_source_error_os_error(cli, tmp_path):
    e = source_error("os_error", tmp_path / "video.mp4", errno=errno.EIO)
    code, err, lines = cli.source_error_message(e)
    assert (code, err, as_lines(lines)) == (1, "source_os_error", [B6B.format(file="video.mp4")])


# --- receive_dir_error_message (B9a–d) ----------------------------------------------------------------


def receive_dir_text(cli, cause: BaseException, path: Path) -> str:
    from teleport.app.node import ReceiveDirUnavailable

    e = ReceiveDirUnavailable(path)
    e.__cause__ = cause
    result = cli.receive_dir_error_message(e)
    if isinstance(result, tuple) and result and not isinstance(result[0], str):
        result = result[-1]  # dạng (mã, …, dòng) nếu có
    return " ".join(as_lines(result))


def _cause(kind: str) -> BaseException:
    from teleport.app import storage
    from teleport.app.diskio import DiskTimeout

    return {
        "unsafe": storage.UnsafeReceiveDirError("liên kết"),
        "too_long": storage.PathTooLongError("dài"),
        "permission": PermissionError(errno.EACCES, "từ chối"),
        "eio": OSError(errno.EIO, "lỗi I/O"),
        "timeout": DiskTimeout(),
        # B9e (spec-p2-8 A): `--dir` không phải thư mục — `NotADirectoryError`, hoặc `mkdir` gặp file
        # trùng tên (`FileExistsError`).
        "not_a_dir": NotADirectoryError(errno.ENOTDIR, "không phải thư mục"),
        "file_exists": FileExistsError(errno.EEXIST, "đã có file"),
    }[kind]


@pytest.mark.parametrize(
    ("kind", "why"),
    [
        ("unsafe", B9A),
        ("too_long", B9B),
        ("permission", B9C),
        ("eio", B9D),
        ("timeout", B9D),
        ("not_a_dir", B9E),
        ("file_exists", B9E),
    ],
)
def test_receive_dir_error_message_by_cause(cli, tmp_path, kind, why):
    path = tmp_path / "Nhận"
    assert receive_dir_text(cli, _cause(kind), path) == B9.format(dir=path, why=why)


# --- Tham số sai → mã 2, lỗi trước khi có lượt (không socket) --------------------------------------------


@pytest.fixture
def pair_dirs(tmp_path):
    """S (gửi) và R (nhận) tin nhau: S lưu R là “PC-Nha”, R lưu S là “Laptop-Minh”."""
    s_dir, r_dir = tmp_path / "S", tmp_path / "R"
    ident_s, ident_r = load_or_create_identity(s_dir), load_or_create_identity(r_dir)
    TrustStore.load(s_dir).add(peer_cert_from_pem(ident_r.cert_pem()), RECEIVER)
    TrustStore.load(r_dir).add(peer_cert_from_pem(ident_s.cert_pem()), SENDER)
    return SimpleNamespace(
        S=s_dir,
        R=r_dir,
        sid=ident_s.device_id,
        rid=ident_r.device_id,
        recv=tmp_path / "nhan",
        src=tmp_path / "nguon",
        tmp=tmp_path,
    )


def send_argv(net, files, port: int | None = None, *, json_mode=True, to=RECEIVER, config=None, extra=()):
    address = [] if port is None else ["--address", f"{LOCAL}:{port}"]
    return [
        "--config-dir",
        str(net.S if config is None else config),
        "send",
        *(str(f) for f in files),
        "--to",
        to,
        *address,
        *(["--json"] if json_mode else []),
        "--name",
        SENDER,
        "--host",
        LOCAL,
        *extra,
    ]


def receive_argv(net, *, json_mode=True, yes=True, config=None, recv=None, extra=()):
    return [
        "--config-dir",
        str(net.R if config is None else config),
        "receive",
        "--dir",
        str(net.recv if recv is None else recv),
        *(["--json"] if json_mode else []),
        *(["--yes"] if yes else []),
        "--name",
        RECEIVER,
        "--host",
        LOCAL,
        "--port",
        "0",
        *extra,
    ]


@pytest.mark.parametrize(
    "argv_tail",
    [
        ["send", "--to", RECEIVER],  # không có file
        ["send", "a.bin"],  # thiếu --to
        ["send", "a.bin", "--address", "127.0.0.1:47474"],  # --to bắt buộc kể cả khi có --address
        ["send", "a.bin", "--to", RECEIVER, "--bogus"],
        ["receive", "--bogus"],
        ["receive", "--port", "70000"],
    ],
)
def test_bad_command_line_exits_2(tmp_path, argv_tail):
    assert run_main(["--config-dir", str(tmp_path / "cfg"), *argv_tail]) == 2


@pytest.mark.parametrize("cmd", ["send", "receive"])
def test_hidden_options_are_not_in_help(capsys, cmd):
    from teleport.cli import main

    with pytest.raises(SystemExit):
        main([cmd, "--help"])
    out = capsys.readouterr().out
    for hidden in ("--host", "--port", "--mdns-interface", "--advertise-address"):
        assert hidden not in out, hidden
    expected = (
        ("--to", "--address", "--limit-rate", "--json", "--name")
        if cmd == "send"
        else (
            "--dir",
            "--yes",
            "--json",
            "--name",
        )
    )
    for option in expected:
        assert option in out, option


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("rate", ["63K", "0", "nan", "1e9", "1T"])
def test_bad_limit_rate_exits_2_before_touching_config(tmp_path, capsys, json_mode, rate):
    cfg = tmp_path / "cfg"
    argv = ["--config-dir", str(cfg), "send", "a.bin", "--to", RECEIVER, "--limit-rate", rate]
    assert run_main([*argv, *(["--json"] if json_mode else [])]) == 2
    captured = capsys.readouterr()
    if json_mode:
        [ev] = json_lines(captured.out)
        assert (ev["event"], ev["error"], ev["message"]) == ("error", "invalid_rate", B1.format(x=rate))
    else:
        assert B1.format(x=rate) in captured.out + captured.err
    assert not cfg.exists()


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("address", ["999.1.1.1", "1.2.3", "localhost", "1.2.3.4:99999", "0.0.0.0"])
def test_bad_address_exits_2(tmp_path, capsys, json_mode, address):
    cfg = tmp_path / "cfg"
    argv = ["--config-dir", str(cfg), "send", "a.bin", "--to", RECEIVER, "--address", address]
    assert run_main([*argv, *(["--json"] if json_mode else [])]) == 2
    captured = capsys.readouterr()
    if json_mode:
        [ev] = json_lines(captured.out)
        assert (ev["error"], ev["message"]) == ("invalid_address", B2.format(x=address))
    else:
        assert B2.format(x=address) in captured.out + captured.err
    assert not cfg.exists()


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_too_many_files_exits_2(tmp_path, capsys, json_mode):
    from teleport.core.messages import MAX_OFFER_FILES

    files = [str(tmp_path / f"f{i}.bin") for i in range(MAX_OFFER_FILES + 1)]
    argv = ["--config-dir", str(tmp_path / "cfg"), "send", *files, "--to", RECEIVER]
    assert run_main([*argv, *(["--json"] if json_mode else [])]) == 2
    captured = capsys.readouterr()
    text = B3.format(n=MAX_OFFER_FILES)
    if json_mode:
        [ev] = json_lines(captured.out)
        assert (ev["error"], ev["message"]) == ("too_many_files", text)
    else:
        assert text in captured.out + captured.err


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_directory_exits_2(pair_dirs, capsys, json_mode):
    folder = pair_dirs.tmp / "ảnh"
    folder.mkdir()
    assert run_main(send_argv(pair_dirs, [folder], json_mode=json_mode)) == 2
    captured = capsys.readouterr()
    if json_mode:
        [ev] = json_lines(captured.out)
        assert ev["error"] == "not_a_file" and ev["message"].endswith(B4_SUFFIX)
    else:
        assert B4_SUFFIX in captured.out + captured.err
    assert lock_is_free(pair_dirs.S)


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_same_file_twice_exits_2(pair_dirs, capsys, json_mode):
    f = make_file(pair_dirs.src, "a.bin", 10)
    assert run_main(send_argv(pair_dirs, [f, f], json_mode=json_mode)) == 2
    captured = capsys.readouterr()
    if json_mode:
        [ev] = json_lines(captured.out)
        assert (ev["error"], ev["message"]) == ("duplicate_file", B5)
    else:
        assert B5 in captured.out + captured.err


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_missing_file_exits_1_without_taking_node_lock(pair_dirs, capsys, json_mode):
    """plan mục 6 “Thêm”: file thiếu → 1, báo trước khi lấy `node.lock` (khóa đang bị giữ mà vẫn báo
    đúng lỗi file, không chờ khóa)."""
    missing = pair_dirs.src / "khong-co.mp4"
    lock = NodeLock(pair_dirs.S)
    lock.acquire()
    try:
        t0 = time.monotonic()
        assert run_main(send_argv(pair_dirs, [missing], json_mode=json_mode)) == 1
        assert time.monotonic() - t0 < 1.5
    finally:
        lock.release()
    captured = capsys.readouterr()
    text = B6A.format(path=os.path.abspath(missing))
    if json_mode:
        [ev] = json_lines(captured.out)
        assert (ev["error"], ev["message"]) == ("source_not_found", text)
    else:
        assert text in captured.out + captured.err
        assert LOCKED not in captured.out + captured.err


# --- I12: `--to` không thấy / khớp nhiều; mDNS tắt mà không `--address` (không socket) ------------------


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
async def test_i12_unknown_target_lists_trusted_devices(pair_dirs, cli_runs, json_mode):
    f = make_file(pair_dirs.src, "a.bin", 10)
    run = cli_runs("send", send_argv(pair_dirs, [f], 47474, to="khong-co", json_mode=json_mode))
    assert await run.code(10) == 1
    if json_mode:
        [ev] = run.events_of("error")
        assert ev["error"] == "not_found"
        assert ev["message"] == UNPAIR_NOT_FOUND.format(q="khong-co")
        assert ev["candidates"] == [{"device_id": pair_dirs.rid, "name": RECEIVER}]
    else:
        text = run.text()
        assert UNPAIR_NOT_FOUND.format(q="khong-co") in text
        assert "Các thiết bị tin cậy:" in text and f"  {pair_dirs.rid[:8]}  {RECEIVER}" in text
    assert lock_is_free(pair_dirs.S)


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
async def test_i12_ambiguous_target_lists_candidates(pair_dirs, cli_runs, json_mode):
    store = TrustStore.load(pair_dirs.S)
    extra = [load_or_create_identity(pair_dirs.tmp / f"x{i}") for i in range(2)]
    store.add(peer_cert_from_pem(extra[0].cert_pem()), "pc")
    store.add(peer_cert_from_pem(extra[1].cert_pem()), "PC")
    f = make_file(pair_dirs.src, "a.bin", 10)
    run = cli_runs("send", send_argv(pair_dirs, [f], 47474, to="pc", json_mode=json_mode))
    assert await run.code(10) == 1
    if json_mode:
        [ev] = run.events_of("error")
        assert ev["error"] == "ambiguous" and ev["message"] == UNPAIR_AMBIGUOUS.format(q="pc")
        assert {c["device_id"] for c in ev["candidates"]} == {x.device_id for x in extra}
    else:
        text = run.text()
        assert UNPAIR_AMBIGUOUS.format(q="pc") in text
        for x in extra:
            assert f"  {x.device_id}  " in text


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
async def test_i12_without_mdns_and_without_address_exits_1(pair_dirs, cli_runs, json_mode):
    """`--host 127.0.0.1` không kèm `--mdns-interface` → mDNS tắt; không có `--address` → B8b, mã 1."""
    f = make_file(pair_dirs.src, "a.bin", 10)
    run = cli_runs("send", send_argv(pair_dirs, [f], None, json_mode=json_mode))
    assert await run.code(10) == 1
    prefix, suffix = B8B_PREFIX.format(x=RECEIVER), B8B_SUFFIX
    if json_mode:
        [ev] = run.events_of("error")
        assert ev["error"] == "discovery_error"
        assert ev["message"].startswith(prefix) and ev["message"].endswith(suffix)
    else:
        assert prefix in run.text() and suffix in run.text()
    assert lock_is_free(pair_dirs.S)


# =================================================================================================
# T2 — trong tiến trình, socket thật trên 127.0.0.1 (plan mục 6: I1–I14 và các ca “Thêm”)
# =================================================================================================


@pytest.fixture
def transfer_env(fast, monkeypatch):
    """Hằng rút ngắn như tests/app (`fast`) + `connection.DRAIN_TIMEOUT` = 10 s như `transfer_nodes`."""
    from teleport.net import connection

    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", fast.TRANSFER_DRAIN_TIMEOUT)
    return fast


@pytest.fixture
def node_spy(monkeypatch):
    """Ghi lại mọi `Node` mà `teleport.cli` tạo: danh sách `(config_dir, node)`."""
    from teleport import cli

    real = cli.Node
    made: list[tuple[Path, object]] = []

    class SpyNode(real):
        def __init__(self, config_dir, *args, **kwargs) -> None:
            super().__init__(config_dir, *args, **kwargs)
            made.append((Path(config_dir), self))

    monkeypatch.setattr(cli, "Node", SpyNode)
    return made


async def node_for(spy, directory: Path, timeout: float = 8.0):
    directory = Path(directory)
    await wait_until(
        lambda: any(d == directory for d, _ in spy), timeout, f"cli chưa tạo Node cho {directory}"
    )
    return next(n for d, n in reversed(spy) if d == directory)


async def port_for(spy, directory: Path) -> int:
    node = await node_for(spy, directory)
    await wait_until(lambda: node.port is not None, 8.0, "node chưa mở cổng")
    return node.port


def patch_close(monkeypatch, spy, directory: Path, *, before: float = 0.0, hang: bool = False) -> None:
    """Vá `Node.close` **chỉ cho node của `directory`** (spec-p2-7 mục B, I9/I10): chờ `before` giây rồi
    đóng thật; `hang=True` → đóng thật xong thì treo mãi (tới khi bị hủy)."""
    from teleport.app.node import Node

    real = Node.close
    directory = Path(directory)

    async def close(self):
        mine = any(n is self for d, n in spy if d == directory)
        if mine and before:
            await asyncio.sleep(before)
        await real(self)
        if mine and hang:
            await asyncio.Event().wait()

    monkeypatch.setattr(Node, "close", close)


async def start_receiver(cli_runs, net, spy=None, **kw):
    """`receive`; trả `(run, port)`. JSON: cổng từ sự kiện `receiving`; text: qua `node_spy`."""
    run = cli_runs("receive", receive_argv(net, **kw))
    if kw.get("json_mode", True):
        port = (await run.wait_event("receiving"))["port"]
    else:
        await run.wait_text(C1_TAIL)
        assert spy is not None, "receive dạng text cần node_spy để biết cổng"
        port = await port_for(spy, net.R if kw.get("config") is None else kw["config"])
    return run, port


async def transferring(run, timeout: float = 10.0, settle: float = 0.5) -> dict:
    """Chờ sự kiện `status` = transferring (bên nhận đã bắt đầu nhận khối), rồi thêm `settle` giây.

    Không dựa vào `progress`: `done_bytes` có thể chỉ tăng theo khối 1 MiB, nên với `--limit-rate 64K`
    sự kiện tiến độ đầu tiên có `done_bytes` > 0 tới sau ~16 s.
    """
    deadline = time.monotonic() + timeout
    while True:
        for ev in run.events_of("status"):
            if ev["status"] == "transferring":
                await asyncio.sleep(settle)
                return ev
        if run.task.done():
            raise AssertionError(f"lệnh đã dừng trước khi bắt đầu nhận: {run.text()!r}")
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s chưa bắt đầu nhận: {run.text()!r}")
        await asyncio.sleep(0.01)


async def time_until(predicate, timeout: float) -> float:
    """Chờ `predicate()` (dò mỗi 5 ms); trả số giây đã chờ."""
    t0 = time.monotonic()
    while not predicate():
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"hết {timeout} s mà điều kiện chưa đúng")
        await asyncio.sleep(0.005)
    return time.monotonic() - t0


def assert_json_clean(run) -> None:
    raw = run.out.getvalue()
    assert "\r" not in raw and "\x1b" not in raw
    for ev in run.events():
        assert TIME_RE.match(ev["time"]), ev


# --- I1: --address, 3 file (có file rỗng), --yes ----------------------------------------------------------


@with_marks(socket_test)
async def test_i1_send_three_files_by_address_to_auto_accepting_receiver(pair_dirs, transfer_env, cli_runs):
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net))
    ready = await recv.wait_event("receiving")
    port = ready["port"]
    assert ready["device_id"] == net.rid and ready["name"] == RECEIVER and port > 0
    assert ready["receive_dir"] == str(net.recv) and ready["auto_accept"] is True
    assert {"addresses", "discovery", "discovery_error"} <= ready.keys()

    files = [
        make_file(net.src, "a.bin", 300 * KiB),
        make_file(net.src, "rong.txt", 0),
        make_file(net.src, "c.bin", 2 * MiB + 5),
    ]
    total = sum(f.stat().st_size for f in files)  # 2,3 MB
    send = cli_runs("send", send_argv(net, files, port))
    assert await send.code(30) == 0, send.text()

    kinds = [e["event"] for e in send.events()]
    assert kinds.count("result") == 1 and "error" not in kinds, kinds
    assert kinds.count("file_done") == 3
    last_file_done = max(i for i, k in enumerate(kinds) if k == "file_done")
    assert kinds.index("offer") < kinds.index("file_done") and last_file_done < kinds.index("result")

    [conn] = send.events_of("connecting")
    assert conn["device_id"] == net.rid and conn["name"] == RECEIVER
    assert conn["address"] == f"{LOCAL}:{port}"
    assert (conn["file_count"], conn["total_bytes"], conn["rate_limit"]) == (3, total, None)

    [offer] = send.events_of("offer")
    assert offer["direction"] == "send" and offer["device_id"] == net.rid
    assert offer["files"] == [{"name": f.name, "size": f.stat().st_size} for f in files]
    assert offer["total_bytes"] == total

    done = send.events_of("file_done")
    assert [(e["file_index"], e["name"], e["path"]) for e in done] == [
        (0, "a.bin", None),
        (1, "rong.txt", None),
        (2, "c.bin", None),
    ]

    [res] = send.events_of("result")
    assert res.keys() >= RESULT_FIELDS
    assert (res["result"], res["direction"], res["device_id"], res["device_name"]) == (
        "done",
        "send",
        net.rid,
        RECEIVER,
    )
    assert res["by_peer"] is False and res["files_done"] == res["file_count"] == 3
    assert res["done_bytes"] == res["total_bytes"] == total
    assert res["message"] == S_DONE.format(n=3, size="2,3 MB", p=RECEIVER)
    assert res["transfer_id"] == offer["transfer_id"]

    got = await recv.wait_event("result", timeout=10)
    assert got["result"] == "done" and got["direction"] == "receive" and got["device_name"] == SENDER
    assert got["message"] == R_DONE.format(n=3, size="2,3 MB", s=SENDER)
    [r_offer] = recv.events_of("offer")
    assert r_offer["auto_accepted"] is True and r_offer["receive_dir"] == str(net.recv)
    assert r_offer["device_name"] == SENDER and r_offer["direction"] == "receive"
    r_done = recv.events_of("file_done")
    assert len(r_done) == 3
    for ev, src in zip(r_done, files, strict=True):
        assert isinstance(ev["path"], str)
        assert sha256_of(Path(ev["path"])) == sha256_of(src)
        assert Path(ev["path"]).parent == net.recv
    assert_json_clean(send)
    assert_json_clean(recv)


# --- I2–I4: lời hỏi [c/k] ---------------------------------------------------------------------------------


@with_marks(socket_test)
async def test_i2_answer_c_receives_and_both_sides_print_spec_lines(
    pair_dirs, transfer_env, cli_runs, node_spy
):
    net = pair_dirs
    recv, port = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=False)
    f = make_file(net.src, "a.bin", 300 * KiB)
    send = cli_runs("send", send_argv(net, [f], port, json_mode=False))
    await recv.wait_text(C4_ASK)
    text = recv.text()
    assert C4_HEAD.format(s=SENDER, n=1, size="300 KB") in text
    assert re.search(r"(?m)^  a\.bin +300 KB$", text), text
    assert f"Lưu vào: {net.recv}" in text
    await send.wait_text(C7_WAITING.format(p=RECEIVER))
    recv.say("c")
    assert await send.code(30) == 0, send.text()
    out = send.text()
    assert C2_CONNECT.format(x=RECEIVER, addr=f"{LOCAL}:{port}") in out
    assert C3.format(x=RECEIVER, n=1, size="300 KB") in out
    assert S_FILE_DONE.format(file="a.bin", i=1, n=1) in out
    assert S_DONE.format(n=1, size="300 KB", p=RECEIVER) in out
    await recv.wait_text(R_DONE.format(n=1, size="300 KB", s=SENDER))
    assert R_SAVED.format(path=net.recv / "a.bin") in recv.text()
    verifying = C8_VERIFYING.format(file="a.bin")
    assert verifying in recv.text() or verifying in send.text()
    assert sha256_of(net.recv / "a.bin") == sha256_of(f)
    assert "\r" not in send.out.getvalue() + recv.out.getvalue()


@with_marks(socket_test)
async def test_i2_answer_k_rejects_and_receiver_prints_no_error(pair_dirs, transfer_env, cli_runs, node_spy):
    net = pair_dirs
    recv, port = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=False)
    send = cli_runs("send", send_argv(net, [make_file(net.src, "a.bin", 1000)], port))
    await recv.wait_text(C4_ASK)
    recv.say("không hiểu")
    await recv.wait_text(C6_HINT)
    await recv.wait_text(C4_ASK, n=2)
    recv.say("k")
    assert await send.code(15) == 1
    [res] = send.events_of("result")
    assert res["result"] == "rejected_user" and res["by_peer"] is True
    assert res["message"] == S_REJECTED.format(p=RECEIVER)
    await recv.wait_text(R_REJECTED_SELF)
    assert "Lỗi:" not in recv.text()
    assert not (net.recv / "a.bin").exists()


@with_marks(socket_test)
async def test_i3_stdin_closed_before_offer_rejects_every_offer_and_warns_once(
    pair_dirs, transfer_env, cli_runs, node_spy
):
    net = pair_dirs
    recv, port = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=False)
    recv.say(None)  # EOF
    await asyncio.sleep(0.2)
    for i in range(2):
        send = cli_runs("send", send_argv(net, [make_file(net.src, f"a{i}.bin", 1000)], port))
        assert await send.code(15) == 1
        assert send.events_of("result")[0]["result"] == "rejected_user"
    assert recv.text().count(B13) == 1


@with_marks(socket_test)
async def test_i3_stdin_closed_while_prompt_open_rejects(pair_dirs, transfer_env, cli_runs, node_spy):
    net = pair_dirs
    recv, port = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=False)
    send = cli_runs("send", send_argv(net, [make_file(net.src, "a.bin", 1000)], port))
    await recv.wait_text(C4_ASK)
    recv.say(None)
    assert await send.code(15) == 1
    assert send.events_of("result")[0]["result"] == "rejected_user"
    await recv.wait_text(B13)


@with_marks(socket_test)
async def test_i4_line_typed_before_prompt_is_ignored(pair_dirs, transfer_env, cli_runs, node_spy):
    net = pair_dirs
    recv, port = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=False)
    recv.say("c")  # gõ sẵn trước khi có lời hỏi
    send = cli_runs("send", send_argv(net, [make_file(net.src, "a.bin", 1000)], port))
    await recv.wait_text(C4_ASK)
    await asyncio.sleep(0.5)
    assert not send.task.done(), "dòng gõ trước lời hỏi không được tính là câu trả lời"
    assert not any(e["status"] == "transferring" for e in send.events_of("status"))
    recv.say("k")
    assert await send.code(15) == 1
    assert send.events_of("result")[0]["result"] == "rejected_user"


@with_marks(socket_test)
async def test_json_receive_without_yes_reads_answers_from_stdin(pair_dirs, transfer_env, cli_runs):
    """plan 2.3 bước 5: `--json` không `--yes` → `offer` có `auto_accepted:false`, đọc `c`/`k` từ stdin."""
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net, yes=False))
    port = (await recv.wait_event("receiving"))["port"]
    f = make_file(net.src, "a.bin", 1000)
    send = cli_runs("send", send_argv(net, [f], port))
    offer = await recv.wait_event("offer")
    assert offer["auto_accepted"] is False and offer["files"] == [{"name": "a.bin", "size": 1000}]
    recv.say("??")  # câu trả lời sai: im lặng như `pair --json`
    await asyncio.sleep(0.2)
    assert not send.task.done()
    recv.say("c")
    assert await send.code(15) == 0
    assert (await recv.wait_event("result"))["result"] == "done"
    assert_json_clean(recv)


# --- I5: gửi tới máy chạy `listen` -------------------------------------------------------------------------


@with_marks(socket_test)
async def test_i5_send_to_listen_is_not_receiving(pair_dirs, transfer_env, cli_runs):
    net = pair_dirs
    argv = [
        "--config-dir",
        str(net.R),
        "listen",
        "--json",
        "--name",
        RECEIVER,
        "--host",
        LOCAL,
        "--port",
        "0",
    ]
    listen = cli_runs("listen", argv)
    port = (await listen.wait_event("listening"))["port"]
    send = cli_runs("send", send_argv(net, [make_file(net.src, "a.bin", 1000)], port, json_mode=False))
    assert await send.code(15) == 1
    assert S_NOT_RECEIVING.format(p=RECEIVER) in send.text()


# --- I6–I10: Ctrl+C ----------------------------------------------------------------------------------------


@with_marks(socket_test)
async def test_i6_ctrl_c_on_send_cancels_receiver_within_one_second(pair_dirs, transfer_env, cli_runs):
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    f = make_file(net.src, "lon.bin", 2 * MiB)
    send = cli_runs("send", send_argv(net, [f], port, extra=["--limit-rate", "64K"]))
    status = await transferring(recv)
    tid = status["transfer_id"]
    assert (net.recv / ".teleport" / tid).exists()
    send.press()
    waited = await time_until(lambda: bool(recv.events_of("result")), 5.0)
    [res] = recv.events_of("result")
    assert (res["result"], res["by_peer"]) == ("cancelled_user", True)
    assert waited <= 1.0, f"cancel tới bên nhận sau {waited:.3f} s (yêu cầu ≤ 1,0 s)"
    assert res["message"] == R_SENDER_CANCELLED.format(s=SENDER)
    assert await send.code(10) == 1
    [mine] = send.events_of("result")
    assert (mine["result"], mine["by_peer"]) == ("cancelled_user", False)
    assert mine["message"] == S_SELF_CANCELLED.format(p=RECEIVER)
    assert send.events_of("error") == []
    # Xóa thư mục tạm là việc nền: dò có hạn (spec-p2-7 mục B).
    await wait_until(lambda: not (net.recv / ".teleport" / tid).exists(), 5, "thư mục tạm chưa bị xóa")
    assert not (net.recv / "lon.bin").exists()


@with_marks(socket_test)
async def test_send_text_mode_ctrl_c_prints_stopping_line(pair_dirs, transfer_env, cli_runs):
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    f = make_file(net.src, "lon.bin", 2 * MiB)
    send = cli_runs("send", send_argv(net, [f], port, json_mode=False, extra=["--limit-rate", "64K"]))
    await transferring(recv)
    send.press()
    assert await send.code(10) == 1
    assert C12 in send.text()
    assert S_SELF_CANCELLED.format(p=RECEIVER) in send.text()


@with_marks(socket_test)
async def test_i7_ctrl_c_on_receive_mid_transfer(pair_dirs, transfer_env, cli_runs):
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    f = make_file(net.src, "lon.bin", 2 * MiB)
    send = cli_runs("send", send_argv(net, [f], port, extra=["--limit-rate", "64K"]))
    await transferring(recv)
    recv.press()
    assert await recv.code(15) == 0
    [mine] = recv.events_of("result")
    assert (mine["result"], mine["by_peer"]) == ("cancelled_user", False)
    assert mine["message"] == R_SELF_CANCELLED.format(s=SENDER)
    assert recv.events()[-1]["event"] == "stopped"
    assert await send.code(10) == 1
    [res] = send.events_of("result")
    assert (res["result"], res["by_peer"]) == ("cancelled_user", True)
    assert res["message"] == S_PEER_STOPPED.format(p=RECEIVER)
    assert lock_is_free(net.R)


@with_marks(socket_test)
async def test_i8_ctrl_c_after_done_still_exits_0(pair_dirs, transfer_env, cli_runs, node_spy, monkeypatch):
    net = pair_dirs
    patch_close(monkeypatch, node_spy, net.S, before=0.8)  # đóng chậm: Ctrl+C rơi vào lúc đang đóng
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    send = cli_runs("send", send_argv(net, [make_file(net.src, "a.bin", 1000)], port))
    res = await send.wait_event("result", timeout=15)
    assert res["result"] == "done"
    assert not send.task.done(), "lệnh lẽ ra còn đang đóng node (close đã bị làm chậm)"
    send.press()
    assert await send.code(10) == 0
    assert send.events_of("error") == []


@integration
@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_i9_second_ctrl_c_exits_within_one_second_when_close_hangs(
    pair_dirs, transfer_env, cli_runs, node_spy, monkeypatch
):
    net = pair_dirs
    patch_close(monkeypatch, node_spy, net.S, hang=True)
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    f = make_file(net.src, "lon.bin", 2 * MiB)
    send = cli_runs("send", send_argv(net, [f], port, extra=["--limit-rate", "64K"]))
    await transferring(recv)
    send.press()
    await asyncio.sleep(0.3)
    assert not send.task.done(), "close() bị vá treo: lệnh phải còn chờ"
    t0 = time.monotonic()
    send.press()
    code = await send.code(5)
    assert time.monotonic() - t0 <= 1.0
    assert code == 1


@integration
@pytest.mark.filterwarnings("ignore::ResourceWarning")
async def test_i10_close_time_limit_bounds_exit_when_close_hangs(
    pair_dirs, transfer_env, cli_runs, node_spy, monkeypatch
):
    from teleport.app import node as app_node

    net = pair_dirs
    limit = 0.5
    monkeypatch.setattr(app_node, "close_time_limit", lambda: limit)
    patch_close(monkeypatch, node_spy, net.S, hang=True)
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    f = make_file(net.src, "lon.bin", 2 * MiB)
    send = cli_runs("send", send_argv(net, [f], port, extra=["--limit-rate", "64K"]))
    await transferring(recv)
    t0 = time.monotonic()
    send.press()
    code = await send.code(10)
    assert time.monotonic() - t0 <= limit + 0.5
    assert code == 1


# --- I11: khóa bận ---------------------------------------------------------------------------------------


@with_marks(socket_test)
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
async def test_i11_send_while_receive_holds_lock(pair_dirs, transfer_env, cli_runs, json_mode):
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    f = make_file(net.src, "a.bin", 1000)
    # Cùng thư mục cấu hình với `receive`: R tin S (“Laptop-Minh”).
    argv = send_argv(net, [f], port, to=SENDER, config=net.R, json_mode=json_mode)
    t0 = time.monotonic()
    send = cli_runs("send", argv)
    assert await send.code(15) == 1
    assert time.monotonic() - t0 <= transfer_env.LOCK_TIMEOUT + 2.0
    if json_mode:
        [ev] = send.events_of("error")
        assert ev["error"] == "node_locked"
    else:
        assert LOCKED in send.text() and B7 in send.text()
    assert not recv.task.done(), "receive phải chạy tiếp"


# --- I13: --address không tới được / máy kia đã hủy ghép đôi ----------------------------------------------


@with_marks(socket_test)
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
async def test_i13_address_with_closed_port_exits_1_quickly(pair_dirs, transfer_env, cli_runs, json_mode):
    """Phụ thuộc spec-p2-7 A1: lần thử do `hint()` thất bại → `ConnectFailed(UNREACHABLE)` ngay, không
    chờ hạn 20 s."""
    net = pair_dirs
    port = closed_port()
    f = make_file(net.src, "a.bin", 1000)
    t0 = time.monotonic()
    send = cli_runs("send", send_argv(net, [f], port, json_mode=json_mode))
    assert await send.code(15) == 1
    assert time.monotonic() - t0 < transfer_env.OPENING_TIMEOUT + transfer_env.SLACK + 3.0
    expected = MVP_UNREACHABLE_ADDRESS.format(target=f"{LOCAL}:{port}")
    if json_mode:
        [ev] = send.events_of("error")
        assert ev["error"] == "connect_failed" and ev["result"] == "unreachable"
        assert ev["message"].startswith(expected)
    else:
        assert expected in send.text()
    assert lock_is_free(net.S)


@with_marks(socket_test)
async def test_i13_receiver_that_unpaired_rejects_connection(pair_dirs, transfer_env, cli_runs, node_spy):
    net = pair_dirs
    TrustStore.load(net.R).remove(net.sid)  # máy nhận đã hủy ghép đôi; máy gửi vẫn tin máy nhận
    recv, port = await start_receiver(cli_runs, net, node_spy, json_mode=False)
    assert B11 in recv.text()
    send = cli_runs("send", send_argv(net, [make_file(net.src, "a.bin", 1000)], port, json_mode=False))
    assert await send.code(15) == 1
    assert MVP_REJECTED.format(x=RECEIVER) in send.text()


# --- I14 và mDNS (mDNS giả `cli_mdns`) ----------------------------------------------------------------------

MDNS_OPTS = ["--mdns-interface", LOCAL]


def discovered(device_id: str, name: str, port: int):
    from teleport.core.discovery import DiscoveredDevice
    from teleport.core.messages import PROTOCOL_VERSION

    return DiscoveredDevice(
        device_id=device_id, name=name, os="windows", proto=PROTOCOL_VERSION, port=port, addresses=(LOCAL,)
    )


@with_marks(socket_test)
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
async def test_i14_device_not_seen_within_timeout(
    pair_dirs, transfer_env, cli_runs, cli_mdns, monkeypatch, json_mode
):
    from teleport import cli

    monkeypatch.setattr(cli, "_SEND_CONNECT_TIMEOUT", 0.5)
    net = pair_dirs
    f = make_file(net.src, "a.bin", 1000)
    t0 = time.monotonic()
    send = cli_runs("send", send_argv(net, [f], None, json_mode=json_mode, extra=MDNS_OPTS))
    assert await send.code(10) == 1
    assert time.monotonic() - t0 < 0.5 + 4.0
    expected = B8A.format(x=RECEIVER)
    if json_mode:
        [ev] = send.events_of("error")
        assert (ev["error"], ev["message"]) == ("not_seen", expected)
    else:
        assert C2_SEARCH.format(x=RECEIVER) in send.text()
        assert expected in send.text()


@with_marks(socket_test)
async def test_seen_by_mdns_but_unreachable(pair_dirs, transfer_env, cli_runs, cli_mdns, monkeypatch):
    from teleport import cli

    monkeypatch.setattr(cli, "_SEND_CONNECT_TIMEOUT", 3.0)
    net = pair_dirs
    cli_mdns.seed[net.rid] = discovered(net.rid, RECEIVER, closed_port())
    send = cli_runs(
        "send", send_argv(net, [make_file(net.src, "a.bin", 1000)], None, json_mode=False, extra=MDNS_OPTS)
    )
    assert await send.code(15) == 1
    assert MVP_SEEN_UNREACHABLE.format(x=RECEIVER) in send.text()


@with_marks(socket_test)
async def test_mdns_failing_to_start_reports_b8b_immediately(pair_dirs, transfer_env, cli_runs, cli_mdns):
    """spec-p2-7 A14: mDNS không chạy sau `start()` → B8b ngay, không chờ 20 s."""
    cli_mdns.fail_start = OSError("giả lập mDNS hỏng")
    net = pair_dirs
    t0 = time.monotonic()
    send = cli_runs(
        "send", send_argv(net, [make_file(net.src, "a.bin", 1000)], None, json_mode=False, extra=MDNS_OPTS)
    )
    assert await send.code(10) == 1
    assert time.monotonic() - t0 < 5.0
    text = send.text()
    assert B8B_PREFIX.format(x=RECEIVER) in text and B8B_SUFFIX in text


@with_marks(socket_test)
async def test_ctrl_c_before_transfer_reports_cancelled(pair_dirs, transfer_env, cli_runs, cli_mdns):
    """Ctrl+C khi chưa có lượt → “Đã hủy.” / `error{cancelled}`, mã 1; không có `result`."""
    net = pair_dirs
    send = cli_runs("send", send_argv(net, [make_file(net.src, "a.bin", 1000)], None, extra=MDNS_OPTS))
    await send.wait_event("connecting")
    send.press()
    assert await send.code(10) == 1
    assert send.events_of("result") == []
    [ev] = send.events_of("error")
    assert (ev["error"], ev["message"]) == ("cancelled", CANCELLED)
    assert lock_is_free(net.S)


# --- receive: phần đầu, cảnh báo, dừng ----------------------------------------------------------------------


@with_marks(socket_test)
async def test_receive_text_header_and_yes_warning(pair_dirs, transfer_env, cli_runs, node_spy):
    net = pair_dirs
    recv, _ = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=True)
    lines = recv.out.getvalue().splitlines()
    expected = [C1_HEAD, f"  Tên:      {RECEIVER}", f"  Mã máy:   {net.rid[:8]}", f"  Lưu vào:  {net.recv}"]
    idx = [lines.index(line) for line in expected]
    assert idx == sorted(idx), lines
    assert C1_TAIL in lines and lines.index(C1_TAIL) > idx[-1]
    assert recv.text().count(B10) == 1
    assert B11 not in recv.text()


@with_marks(socket_test)
async def test_receive_without_paired_devices_says_so(tmp_path, transfer_env, cli_runs, node_spy):
    net = SimpleNamespace(R=tmp_path / "R", recv=tmp_path / "nhan")
    recv, _ = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=False)
    assert B11 in recv.text()
    assert B10 not in recv.text()


@with_marks(socket_test)
async def test_receive_mdns_failure_warns_and_keeps_running(
    pair_dirs, transfer_env, cli_runs, cli_mdns, node_spy
):
    cli_mdns.fail_start = OSError("giả lập mDNS hỏng")
    net = pair_dirs
    recv, _ = await start_receiver(cli_runs, net, node_spy, json_mode=False, extra=MDNS_OPTS)
    text = recv.text()
    assert B12_PREFIX in text and B12_SUFFIX in text
    assert not recv.task.done()


@with_marks(socket_test)
@pytest.mark.parametrize("presses", [1, 2])
async def test_receive_ctrl_c_when_idle_exits_0(pair_dirs, transfer_env, cli_runs, node_spy, presses):
    net = pair_dirs
    recv, _ = await start_receiver(cli_runs, net, node_spy, json_mode=False)
    for _ in range(presses):
        recv.press()
    assert await recv.code(15) == 0
    assert C12 in recv.text() and STOPPED in recv.text()
    assert lock_is_free(net.R)


@with_marks(socket_test)
async def test_receive_json_ctrl_c_ends_with_stopped(pair_dirs, transfer_env, cli_runs):
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net))
    await recv.wait_event("receiving")
    recv.press()
    assert await recv.code(15) == 0
    assert recv.events()[-1]["event"] == "stopped"
    assert_json_clean(recv)


@with_marks(socket_test)
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
async def test_receive_dir_with_foreign_temp_root_exits_1(pair_dirs, transfer_env, cli_runs, json_mode):
    """B9a: `.teleport` trong thư mục nhận không phải thư mục của Teleport (ở đây: một file)."""
    net = pair_dirs
    net.recv.mkdir()
    (net.recv / ".teleport").write_text("không phải thư mục", encoding="utf-8")
    recv = cli_runs("receive", receive_argv(net, json_mode=json_mode))
    assert await recv.code(15) == 1
    expected = B9.format(dir=net.recv, why=B9A)
    if json_mode:
        [ev] = recv.events_of("error")
        assert (ev["error"], ev["message"]) == ("receive_dir_error", expected)
    else:
        assert expected in recv.text()
    assert lock_is_free(net.R)


@with_marks(socket_test)
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
async def test_receive_dir_that_is_a_file_exits_1_with_b9e(pair_dirs, transfer_env, cli_runs, json_mode):
    """B9e (spec-p2-8 A): `--dir` trỏ vào một file → mã 1, đúng câu B9e (mọi OS); file không bị đụng."""
    net = pair_dirs
    target = net.tmp / "la-file"
    target.write_text("x", encoding="utf-8")
    recv = cli_runs("receive", receive_argv(net, recv=target, json_mode=json_mode))
    assert await recv.code(15) == 1
    expected = B9.format(dir=target, why=B9E)
    if json_mode:
        [ev] = recv.events_of("error")
        assert (ev["error"], ev["message"]) == ("receive_dir_error", expected)
    else:
        assert expected in recv.text(), recv.text()
    assert target.read_text(encoding="utf-8") == "x"
    assert lock_is_free(net.R)


# --- Kết quả lỗi đúng câu ở cả hai phía ---------------------------------------------------------------------


def _no_space(monkeypatch):
    from teleport.app import storage

    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: usage(10 * MiB, 9 * MiB, MiB))
    return [("f.bin", 2 * MiB)]


def _fat32(monkeypatch):
    from teleport.app import storage

    monkeypatch.setattr(storage, "_fs_type_of", lambda path: "vfat")
    monkeypatch.setattr(storage, "FAT_MAX_FILE_SIZE", MiB)
    return [("lon.bin", MiB + 1)]


def _write_error(monkeypatch):
    from teleport.app import storage

    real = storage.PartFile.write_at

    def write_at(self, index, data):
        if index >= 1:
            raise OSError(errno.ENOSPC, "giả lập đầy đĩa")
        return real(self, index, data)

    monkeypatch.setattr(storage.PartFile, "write_at", write_at)
    return [("f.bin", 3 * MiB)]


def _source_changed(monkeypatch):
    from teleport.app import sources

    real = sources.SourceReader.read_block

    def read_block(self, index):
        result = real(self, index)
        if index == 0:
            st = os.stat(self.path)
            os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        return result

    monkeypatch.setattr(sources.SourceReader, "read_block", read_block)
    return [("f.bin", 3 * MiB)]


FAILURES = {
    "no_space": (
        _no_space,
        lambda net: S_NO_SPACE.format(p=RECEIVER, needed="2,0 MB", available="1,0 MB"),
        lambda net: R_NO_SPACE.format(
            n=1, s=SENDER, needed="2,0 MB", drive=drive_label(net.recv), available="1,0 MB"
        ),
    ),
    "fat32": (
        _fat32,
        lambda net: S_TOO_LARGE.format(p=RECEIVER, file="lon.bin", size="1,0 MB", limit="1 MB"),
        lambda net: R_TOO_LARGE.format(
            n=1, s=SENDER, drive=drive_label(net.recv), limit="1 MB", file="lon.bin", size="1,0 MB"
        ),
    ),
    "write_error": (
        _write_error,
        lambda net: S_PEER_ERROR.format(p=RECEIVER),  # M16
        lambda net: R_DISK_FAILED.format(file="f.bin", dir=net.recv, s=SENDER),
    ),
    "source_changed": (
        _source_changed,
        lambda net: S_SOURCE_CHANGED.format(file="f.bin"),
        lambda net: R_SOURCE_CHANGED.format(file="f.bin", s=SENDER),
    ),
}


@with_marks(socket_test)
@pytest.mark.parametrize("case", list(FAILURES))
async def test_failure_sentences_on_both_sides(
    pair_dirs, transfer_env, cli_runs, node_spy, monkeypatch, caplog, case
):
    setup, sender_line, receiver_line = FAILURES[case]
    net = pair_dirs
    specs = setup(monkeypatch)
    files = [make_file(net.src, name, size) for name, size in specs]
    caplog.set_level("DEBUG", logger="teleport")
    recv, port = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=True)
    send = cli_runs("send", send_argv(net, files, port, json_mode=False))
    assert await send.code(30) == 1, send.text()
    assert sender_line(net) in send.text()
    await recv.wait_text(receiver_line(net), timeout=10)
    assert not recv.task.done()
    # mvp N9: tên file không vào log (chỉ in ra màn hình).
    for rec in caplog.records:
        for name, _ in specs:
            assert name not in rec.getMessage(), (rec.name, rec.getMessage())


@with_marks(socket_test)
async def test_m16_sender_json_result_for_receiver_write_error(
    pair_dirs, transfer_env, cli_runs, monkeypatch
):
    """M16 (spec-p2-8 A) ở `--json`: bên nhận lỗi ghi đĩa → gửi `cancel{error}`; bên gửi có `result`
    `peer_error`, `by_peer: true`, `message` = M16; mã 1. Bên nhận vẫn báo câu của chính nó (6b)."""
    net = pair_dirs
    specs = _write_error(monkeypatch)
    files = [make_file(net.src, name, size) for name, size in specs]
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    send = cli_runs("send", send_argv(net, files, port))
    assert await send.code(30) == 1, send.text()
    [res] = send.events_of("result")
    assert (res["result"], res["by_peer"]) == ("peer_error", True), res
    assert res["message"] == S_PEER_ERROR.format(p=RECEIVER)
    got = await recv.wait_event("result", timeout=10)
    assert got["result"] == "disk_failed"
    assert got["message"] == R_DISK_FAILED.format(file="f.bin", dir=net.recv, s=SENDER)


# --- Giới hạn tốc độ, tiến độ JSON / TTY / không TTY --------------------------------------------------------


@with_marks(socket_test)
async def test_limit_rate_64k_reaches_node(pair_dirs, transfer_env, cli_runs, node_spy):
    """`--limit-rate 64K` → `node.transfers.rate_limit == 65536` (plan mục 6 “Thêm”), JSON `connecting`."""
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    f = make_file(net.src, "a.bin", 1000)
    send = cli_runs("send", send_argv(net, [f], port, extra=["--limit-rate", "64K"]))
    conn = await send.wait_event("connecting")
    assert conn["rate_limit"] == 65536
    node = await node_for(node_spy, net.S)
    assert node.transfers.rate_limit == 65536
    assert await send.code(30) == 0


# Lượt kéo dài ~3 s và `done_bytes` đổi nhiều lần (khối 1 MiB) để có vài sự kiện tiến độ.
PACED = ["--limit-rate", "4M"]
PACED_SIZE = 12 * MiB


@with_marks(socket_test)
async def test_json_progress_is_paced_and_well_formed(pair_dirs, transfer_env, cli_runs):
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    f = make_file(net.src, "a.bin", PACED_SIZE)
    send = cli_runs("send", send_argv(net, [f], port, extra=PACED))
    assert await send.code(40) == 0
    await recv.wait_event("result", timeout=10)
    # Bên nhận chỉ tính `done_bytes` theo khối đã ghi bền (fsync mỗi 16 khối), nên với 12 MiB có thể chỉ
    # có một lần báo; bên gửi phải có vài lần.
    assert len(send.events_of("progress")) >= 2, send.text()
    for run in (send, recv):
        events = run.events_of("progress")
        assert events, run.text()
        for ev in events:
            assert ev.keys() >= PROGRESS_FIELDS
            assert isinstance(ev["bytes_per_second"], int)
            assert ev["eta_seconds"] is None or isinstance(ev["eta_seconds"], float)
            assert ev["file_count"] == 1 and ev["total_bytes"] == PACED_SIZE
            # 1 chữ số thập phân; đặc tả không chốt làm tròn hay cắt, nên chỉ đòi sai số < 0,1.
            exact = 100 * ev["done_bytes"] / ev["total_bytes"]
            assert round(ev["percent"], 1) == ev["percent"] and abs(ev["percent"] - exact) < 0.1, ev
        times = [parse_time(ev["time"]) for ev in events]
        gaps = [(b - a).total_seconds() for a, b in itertools.pairwise(times)]
        assert all(g >= 0.9 for g in gaps), gaps
        assert_json_clean(run)


@with_marks(socket_test)
async def test_text_send_shows_rate_limit_line(pair_dirs, transfer_env, cli_runs):
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    send = cli_runs(
        "send",
        send_argv(
            net, [make_file(net.src, "a.bin", 1000)], port, json_mode=False, extra=["--limit-rate", "20M"]
        ),
    )
    assert await send.code(15) == 0
    assert C2_LIMIT.format(rate="20 MB") in send.text()


@with_marks(socket_test)
async def test_tty_progress_redraws_with_cr_fits_width_and_paces(
    pair_dirs, transfer_env, cli_runs, monkeypatch
):
    from teleport import cli

    monkeypatch.setattr(cli, "_terminal_width", lambda *a, **k: 40)
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    tty = TtyOut()
    f = make_file(net.src, "a.bin", PACED_SIZE)
    send = cli_runs("send", send_argv(net, [f], port, json_mode=False, extra=PACED), out=tty)
    assert await send.code(40) == 0
    out = tty.getvalue()
    assert "\r" in out, out
    assert "\x1b" not in out
    for segment in re.split(r"[\r\n]", out):
        if re.search(r"\d+,\d%", segment):
            assert columns(segment) <= 39, segment
    assert re.search(r"(?:^|\n)" + re.escape(S_DONE.format(n=1, size="12,0 MB", p=RECEIVER)), out), out
    draws = [t for t, s in tty.writes if "\r" in s]
    clusters = [t for i, t in enumerate(draws) if i == 0 or t - draws[i - 1] > 0.1]
    span = clusters[-1] - clusters[0] if clusters else 0.0
    assert len(clusters) <= span + 2, (len(clusters), span)


@with_marks(socket_test)
async def test_plain_progress_has_no_cr_and_respects_interval(pair_dirs, transfer_env, cli_runs, monkeypatch):
    from teleport import cli

    monkeypatch.setattr(cli, "_PLAIN_PROGRESS_INTERVAL", 1000.0)
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    f = make_file(net.src, "a.bin", PACED_SIZE)
    send = cli_runs("send", send_argv(net, [f], port, json_mode=False, extra=PACED))
    assert await send.code(40) == 0
    out = send.out.getvalue()
    assert "\r" not in out and "\x1b" not in out
    assert len(re.findall(r"(?m)\d+,\d% · ", out)) <= 1, out


@with_marks(socket_test)
async def test_control_connection_lost_prints_reconnecting_then_done(
    pair_dirs, transfer_env, cli_runs, node_spy
):
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    f = make_file(net.src, "a.bin", 384 * KiB)
    send = cli_runs("send", send_argv(net, [f], port, json_mode=False, extra=["--limit-rate", "64K"]))
    await transferring(recv)
    node = await node_for(node_spy, net.S)
    conn = node.control_connection(net.rid)
    assert conn is not None
    conn.abort()
    assert await send.code(40) == 0, send.text()
    assert RECONNECTING in send.text()
    assert (await recv.wait_event("result", timeout=10))["result"] == "done"
    assert sha256_of(net.recv / "a.bin") == sha256_of(f)


@with_marks(socket_test)
async def test_data_unreachable_prints_firewall_line_then_recovers(
    pair_dirs, transfer_env, cli_runs, monkeypatch
):
    """M13: mở kết nối dữ liệu thất bại liên tiếp → câu tường lửa (một lần), “Đang nối lại…”, rồi DONE."""
    from teleport.app import transfers as app_transfers
    from teleport.net import connection

    real = app_transfers.open_data
    blocked = [True]

    async def open_data(*args, **kwargs):
        if blocked[0]:
            raise connection.Unreachable("giả lập tường lửa")
        return await real(*args, **kwargs)

    monkeypatch.setattr(app_transfers, "open_data", open_data)
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    send = cli_runs("send", send_argv(net, [make_file(net.src, "a.bin", 300 * KiB)], port, json_mode=False))
    line = M13_DATA_UNREACHABLE.format(x=RECEIVER)
    await send.wait_text(line, timeout=15)
    assert RECONNECTING in send.text()
    blocked[0] = False
    assert await send.code(30) == 0
    assert send.text().count(line) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="spec-p2-7 A19: chỉ Windows tự mở rộng `*?[`")
@with_marks(socket_test)
async def test_windows_glob_pattern_is_expanded_sorted(pair_dirs, transfer_env, cli_runs):
    net = pair_dirs
    files = [make_file(net.src, n, 100 + i) for i, n in enumerate(["b.bin", "a.bin", "c.txt"])]
    recv = cli_runs("receive", receive_argv(net))
    port = (await recv.wait_event("receiving"))["port"]
    send = cli_runs("send", send_argv(net, [net.src / "*.bin"], port))
    assert await send.code(15) == 0
    [offer] = send.events_of("offer")
    assert [f["name"] for f in offer["files"]] == ["a.bin", "b.bin"]
    assert files


@pytest.mark.skipif(sys.platform != "win32", reason="spec-p2-7 A19: chỉ Windows tự mở rộng `*?[`")
async def test_windows_glob_without_match_is_not_found(pair_dirs, cli_runs):
    net = pair_dirs
    pattern = net.src / "*.khong"
    net.src.mkdir()
    send = cli_runs("send", send_argv(net, [pattern], 47474))
    assert await send.code(10) == 1
    [ev] = send.events_of("error")
    assert ev["error"] == "source_not_found"


# =================================================================================================
# T3 — hai tiến trình con thật (`python -m teleport`), hằng thời gian thật (plan mục 6: P1–P5)
#
# Điều kiện (spec-p2-7 mục B): PR LK22 (`REPLACE_AFTER` 15 s) đã vào main và nhánh đã đưa lên main.
# =================================================================================================


def receive_cmd(net, *, config=None, extra=()) -> list[str]:
    return [
        "--config-dir",
        str(net.R if config is None else config),
        "receive",
        "--json",
        "--yes",
        "--dir",
        str(net.recv),
        "--name",
        RECEIVER,
        "--host",
        LOCAL,
        "--port",
        "0",
        *extra,
    ]


def send_cmd(net, files, port=None, *, config=None, to=RECEIVER, extra=()) -> list[str]:
    address = [] if port is None else ["--address", f"{LOCAL}:{port}"]
    return [
        "--config-dir",
        str(net.S if config is None else config),
        "send",
        *(str(f) for f in files),
        "--to",
        to,
        *address,
        "--json",
        "--name",
        SENDER,
        "--host",
        LOCAL,
        *extra,
    ]


def wait_status(proc, status: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        ev = proc.wait_json("status", max(0.1, deadline - time.monotonic()))
        if ev["status"] == status:
            return ev


def assert_clean_exit(proc) -> None:
    err = proc.err_text()
    assert "Traceback" not in err and "Fatal Python error" not in err, err


@integration
def test_p1_subprocess_send_by_address(pair_dirs, procs):
    net = pair_dirs
    recv = procs(receive_cmd(net))
    port = recv.wait_json("receiving")["port"]
    f = make_file(net.src, "a.bin", 3 * MiB + 7)
    send = procs(send_cmd(net, [f], port))
    assert send.finish(60) == 0, send.err_text()
    results = [e for e in send.events() if e["event"] == "result"]
    assert [r["result"] for r in results] == ["done"]
    assert recv.wait_json("result", 15)["result"] == "done"
    assert sha256_of(net.recv / "a.bin") == sha256_of(f)
    recv.interrupt()
    assert recv.finish(30) == 0
    assert_clean_exit(send)
    assert_clean_exit(recv)


@with_marks(socket_test)
async def test_p2_send_finds_receiver_through_discovery(pair_dirs, transfer_env, cli_runs, cli_mdns):
    """P2 (quyết định của phiên chính, 2026-10-26): trong tiến trình, mDNS giả `cli_mdns`. `receive` bật
    mDNS và quảng bá; `send` không có `--address` tìm máy nhận qua khám phá rồi gửi xong, mã 0. Không chạy
    bằng tiến trình con: địa chỉ loopback không được quảng bá qua mDNS thật (giữ nguyên luật lọc)."""
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net, extra=MDNS_OPTS))
    ready = await recv.wait_event("receiving")
    assert ready["discovery"] is True and ready["discovery_error"] is None, ready
    [mdns_recv] = cli_mdns.instances
    await wait_until(lambda: "advertise" in mdns_recv.calls, 5, "receive chưa quảng bá qua mDNS")
    # Máy gửi “thấy” máy nhận qua mDNS (bản giả báo `DeviceAdded` sau `start_browsing`).
    cli_mdns.seed[net.rid] = discovered(net.rid, RECEIVER, ready["port"])
    f = make_file(net.src, "a.bin", MiB + 3)
    send = cli_runs("send", send_argv(net, [f], None, extra=MDNS_OPTS))
    assert await send.code(30) == 0, send.text()
    [conn] = send.events_of("connecting")
    assert conn["address"] is None
    assert [r["result"] for r in send.events_of("result")] == ["done"]
    assert (await recv.wait_event("result", timeout=10))["result"] == "done"
    assert sha256_of(net.recv / "a.bin") == sha256_of(f)


@integration
def test_p3_interrupting_send_cancels_receiver_quickly(pair_dirs, procs):
    """P3: Ctrl+C (Windows: CTRL_BREAK; POSIX: SIGINT) ở `send` đang gửi → bên nhận có `result`
    `cancelled_user`, `by_peer:true` ≤ 2,0 s sau lúc ngắt (đồng hồ hai tiến trình; spec-p2-7 mục B)."""
    net = pair_dirs
    recv = procs(receive_cmd(net))
    port = recv.wait_json("receiving")["port"]
    f = make_file(net.src, "lon.bin", 2 * MiB)
    send = procs(send_cmd(net, [f], port, extra=["--limit-rate", "64K"]))
    wait_status(recv, "transferring")
    time.sleep(0.5)
    sent_at = datetime.datetime.now(datetime.UTC)
    send.interrupt()
    res = recv.wait_json("result", 15)
    delay = (parse_time(res["time"]) - sent_at).total_seconds()
    print(f"P3 cancel_delay_s={delay:.3f}")  # ASCII: console Windows có thể không in được tiếng Việt
    assert (res["result"], res["by_peer"]) == ("cancelled_user", True), res
    assert delay <= 2.0, f"bên nhận báo hủy sau {delay:.3f} s (P3 yêu cầu ≤ 2,0 s)"
    assert send.finish(30) == 1
    mine = [e for e in send.events() if e["event"] == "result"]
    assert [(r["result"], r["by_peer"]) for r in mine] == [("cancelled_user", False)]
    assert_clean_exit(send)


@integration
def test_p4_interrupting_idle_receive_exits_0(pair_dirs, procs):
    net = pair_dirs
    recv = procs(receive_cmd(net))
    recv.wait_json("receiving")
    time.sleep(0.3)
    recv.interrupt()
    assert recv.finish(30) == 0, recv.err_text()
    last = [e for e in recv.events() if e.get("event")][-1]
    assert last["event"] == "stopped" and TIME_RE.match(last["time"])
    lock = NodeLock(net.R)
    lock.acquire(timeout=2)
    lock.release()
    assert_clean_exit(recv)


@integration
def test_p5_send_while_receive_holds_lock(pair_dirs, procs):
    net = pair_dirs
    recv = procs(receive_cmd(net))
    port = recv.wait_json("receiving")["port"]
    f = make_file(net.src, "a.bin", 1000)
    send = procs(send_cmd(net, [f], port, config=net.R, to=SENDER))
    assert send.finish(30) == 1
    errors = [e for e in send.events() if e["event"] == "error"]
    assert [e["error"] for e in errors] == ["node_locked"]
    assert recv.p.poll() is None, "receive phải chạy tiếp"
    assert_clean_exit(send)


# =================================================================================================
# spec-p2-7 mục E (sau soat-code): E1 lời hỏi mở, E3 dừng khi còn offer chờ, E4 EOF với offer chờ
# =================================================================================================


def add_sender(net, label: str, name: str) -> Path:
    """Thêm một máy gửi `label` tin máy nhận R và được R tin với tên `name` (gọi trước khi R chạy)."""
    d = net.tmp / label
    ident = load_or_create_identity(d)
    TrustStore.load(d).add(peer_cert_from_pem(load_or_create_identity(net.R).cert_pem()), RECEIVER)
    TrustStore.load(net.R).add(peer_cert_from_pem(ident.cert_pem()), name)
    return d


@with_marks(socket_test)
async def test_e1_open_prompt_is_reprinted_after_other_lines_and_blocks_progress(
    pair_dirs, transfer_env, cli_runs, node_spy
):
    """E1: lời hỏi đang mở mà có dòng khác in chen (ở đây “Đã lưu:” / “Đã nhận xong” của lượt khác) →
    câu hỏi được in lại sau dòng đó; không vẽ dòng tiến độ `\r` nào cho tới khi trả lời."""
    net = pair_dirs
    c_dir = add_sender(net, "C", "May-C")
    tty = TtyOut()
    recv = cli_runs("receive", receive_argv(net, json_mode=False, yes=False), out=tty)
    await recv.wait_text(C1_TAIL)
    port = await port_for(node_spy, net.R)
    # Lượt 1 (từ C): nhận, chạy ~5 s.
    big = make_file(net.tmp / "nguon-c", "c.bin", 20 * MiB)
    send_c = cli_runs("send", send_argv(net, [big], port, config=c_dir, extra=["--limit-rate", "4M"]))
    await recv.wait_text(C4_HEAD.format(s="May-C", n=1, size="20,0 MB"))
    recv.say("c")
    await asyncio.sleep(0.5)
    # Lượt 2 (từ S): lời hỏi mở trong lúc lượt 1 còn chạy.
    send_s = cli_runs("send", send_argv(net, [make_file(net.src, "s.bin", 1000)], port))
    head_s = C4_HEAD.format(s=SENDER, n=1, size="1000 byte")
    await recv.wait_text(head_s)
    opened = time.monotonic()
    done_c = R_DONE.format(n=1, size="20,0 MB", s="May-C")
    await recv.wait_text(done_c, timeout=30)
    await asyncio.sleep(0.3)
    answered = time.monotonic()
    out = tty.getvalue()
    assert out.index(head_s) < out.index(done_c), "lượt 1 phải xong trong lúc lời hỏi lượt 2 đang mở"
    assert C4_ASK in out[out.index(done_c) :], "câu hỏi phải được in lại sau dòng chen vào"
    redraws = [t for t, s in tty.writes if "\r" in s and opened <= t <= answered]
    assert redraws == [], "không vẽ tiến độ khi đang có lời hỏi mở"
    recv.say("k")
    assert await send_s.code(15) == 1
    assert await send_c.code(15) == 0


@with_marks(socket_test)
async def test_e3_ctrl_c_with_pending_offers_opens_no_new_prompt(pair_dirs, transfer_env, cli_runs, node_spy):
    """E3: Ctrl+C `receive` khi có ≥ 3 offer chờ hỏi → không in thêm lời hỏi mới sau lúc dừng; mã 0."""
    net = pair_dirs
    others = [add_sender(net, f"X{i}", f"May-{i}") for i in range(2)]
    recv, port = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=False)
    sends = [cli_runs("send", send_argv(net, [make_file(net.src, "s.bin", 1000)], port))]
    for i, d in enumerate(others):
        f = make_file(net.tmp / f"nguon-{i}", f"x{i}.bin", 1000)
        sends.append(cli_runs("send", send_argv(net, [f], port, config=d)))
    await recv.wait_text(C4_ASK)
    for run in sends:
        await run.wait_event("status")  # đã có lượt (đang chờ trả lời)
    await asyncio.sleep(0.3)
    assert recv.text().count("muốn gửi") == 1, "chỉ một lời hỏi mở, hai offer còn xếp hàng"
    mark = len(recv.text())
    recv.press()
    assert await recv.code(15) == 0
    after = recv.text()[mark:]
    assert "muốn gửi" not in after, after
    for run in sends:
        assert await run.code(15) == 1


@with_marks(socket_test)
async def test_e4_stdin_eof_with_pending_offers_prints_b13_once_and_no_c6(
    pair_dirs, transfer_env, cli_runs, node_spy
):
    """E4: stdin đóng khi có offer chờ (một đang hỏi, một xếp hàng) → từ chối cả hai, in B13 đúng một lần,
    không in C6 “Đã từ chối.” cho các offer đó."""
    net = pair_dirs
    c_dir = add_sender(net, "C", "May-C")
    recv, port = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=False)
    send_s = cli_runs("send", send_argv(net, [make_file(net.src, "s.bin", 1000)], port))
    await recv.wait_text(C4_ASK)
    send_c = cli_runs(
        "send", send_argv(net, [make_file(net.tmp / "nguon-c", "c.bin", 1000)], port, config=c_dir)
    )
    await send_c.wait_event("status")
    await asyncio.sleep(0.3)
    recv.say(None)
    for run in (send_s, send_c):
        assert await run.code(15) == 1
        assert run.events_of("result")[0]["result"] == "rejected_user"
    await asyncio.sleep(0.3)
    text = recv.text()
    assert text.count(B13) == 1, text
    assert R_REJECTED_SELF not in text, text


# =================================================================================================
# spec-p2-8 mục A (P2-8a): M14 (offer chưa hiện bị từ chối vì đầu vào đóng), M15 (Ctrl+C khi còn offer
# chưa hiện). Câu so nguyên văn với câu đã duyệt.
# =================================================================================================


def text_lines(run) -> list[str]:
    """Các dòng chữ của một lần chạy (out + err), bỏ phần “[c/k]: ” dính đầu dòng nếu có."""
    found = []
    for raw in run.text().splitlines():
        line = raw
        while line.startswith(C4_ASK):
            line = line[len(C4_ASK) :]
        found.append(line)
    return found


@with_marks(socket_test)
async def test_m14_offers_arriving_after_stdin_eof_print_m14_each_and_b13_once(
    pair_dirs, transfer_env, cli_runs, node_spy
):
    """M14: stdin đóng trước khi có offer; hai offer tới sau (2 file từ Laptop-Minh, rồi 1 file từ May-C)
    → cả hai bị từ chối, không hiện lời hỏi; B13 đúng một lần; M14 đúng một lần cho mỗi offer, đúng số
    file và tên thiết bị; không có dòng C6 “Đã từ chối.”."""
    net = pair_dirs
    c_dir = add_sender(net, "C", "May-C")
    recv, port = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=False)
    recv.say(None)  # EOF
    await asyncio.sleep(0.2)
    two = [make_file(net.src, "a.bin", 1000), make_file(net.src, "b.bin", 10)]
    send_s = cli_runs("send", send_argv(net, two, port))
    assert await send_s.code(15) == 1
    assert send_s.events_of("result")[0]["result"] == "rejected_user"
    one = [make_file(net.tmp / "nguon-c", "c.bin", 1000)]
    send_c = cli_runs("send", send_argv(net, one, port, config=c_dir))
    assert await send_c.code(15) == 1
    assert send_c.events_of("result")[0]["result"] == "rejected_user"
    m14_s, m14_c = M14.format(n=2, s=SENDER), M14.format(n=1, s="May-C")
    await recv.wait_text(m14_c)
    await asyncio.sleep(0.3)
    text = recv.text()
    assert text.count(B13) == 1, text
    assert text.count(m14_s) == 1, text
    assert text.count(m14_c) == 1, text
    assert "muốn gửi" not in text, "offer tới sau EOF không được hiện lời hỏi"
    assert R_REJECTED_SELF not in text_lines(recv), text
    assert not recv.task.done(), "receive phải chạy tiếp sau EOF"


@with_marks(socket_test)
async def test_m14_offer_queued_at_eof_gets_m14_and_open_prompt_gets_b13(
    pair_dirs, transfer_env, cli_runs, node_spy
):
    """M14: lúc stdin đóng có một offer đang hỏi (Laptop-Minh) và một offer xếp hàng (May-C, 1 file) →
    offer đang hỏi: B13, không M14; offer xếp hàng: M14 đúng một lần; B13 đúng một lần; không C6."""
    net = pair_dirs
    c_dir = add_sender(net, "C", "May-C")
    recv, port = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=False)
    send_s = cli_runs("send", send_argv(net, [make_file(net.src, "s.bin", 1000)], port))
    await recv.wait_text(C4_ASK)
    send_c = cli_runs(
        "send", send_argv(net, [make_file(net.tmp / "nguon-c", "c.bin", 1000)], port, config=c_dir)
    )
    await send_c.wait_event("status")
    await asyncio.sleep(0.3)
    assert recv.text().count("muốn gửi") == 1, "chỉ một lời hỏi mở, offer kia còn xếp hàng"
    recv.say(None)
    for run in (send_s, send_c):
        assert await run.code(15) == 1
        assert run.events_of("result")[0]["result"] == "rejected_user"
    m14_c = M14.format(n=1, s="May-C")
    await recv.wait_text(m14_c)
    await asyncio.sleep(0.3)
    text = recv.text()
    assert text.count(B13) == 1, text
    assert text.count(m14_c) == 1, text
    assert M14.format(n=1, s=SENDER) not in text, "offer đang hỏi lúc EOF chỉ có B13"
    assert R_REJECTED_SELF not in text_lines(recv), text
    assert text.count("muốn gửi") == 1, "offer xếp hàng không được hiện lời hỏi sau EOF"


@with_marks(socket_test)
async def test_m15_ctrl_c_with_open_and_queued_offer(pair_dirs, transfer_env, cli_runs, node_spy):
    """M15: Ctrl+C khi một offer đang hỏi (Laptop-Minh) và một offer xếp hàng (May-C, 2 file) → offer
    đang hỏi không in C6; offer xếp hàng in M15 đúng một lần; không B13, không M14; `receive` mã 0."""
    net = pair_dirs
    c_dir = add_sender(net, "C", "May-C")
    recv, port = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=False)
    send_s = cli_runs("send", send_argv(net, [make_file(net.src, "s.bin", 1000)], port))
    await recv.wait_text(C4_ASK)
    src_c = net.tmp / "nguon-c"
    files_c = [make_file(src_c, "c1.bin", 1000), make_file(src_c, "c2.bin", 1000)]
    send_c = cli_runs("send", send_argv(net, files_c, port, config=c_dir))
    await send_c.wait_event("status")
    await asyncio.sleep(0.3)
    assert recv.text().count("muốn gửi") == 1, "chỉ một lời hỏi mở, offer kia còn xếp hàng"
    recv.press()
    assert await recv.code(15) == 0
    for run in (send_s, send_c):
        assert await run.code(15) == 1
    text = recv.text()
    m15_c = M15.format(n=2, s="May-C")
    assert text.count(m15_c) == 1, text
    assert M15.format(n=1, s=SENDER) not in text, "offer đang hỏi không in M15"
    assert R_REJECTED_SELF not in text_lines(recv), "offer đang hỏi không in C6"
    assert B13 not in text, text
    assert "vì không đọc được câu trả lời" not in text, text
    assert STOPPED in text, text
    assert text.count("muốn gửi") == 1, text


# --- Bổ sung sau soat-code 8a: JSON giữ `message` “Đã từ chối.”; M15 cho lượt chưa từng có `offer` --------


class _FakeReceiveNode:
    """Node giả cho `_ReceiveSession`: tên máy gửi trong `store`, thư mục nhận, ghi lại `decide`."""

    def __init__(self, name: str = SENDER) -> None:
        self.decided: list[tuple[object, bool]] = []
        self.store = SimpleNamespace(get=lambda device_id: SimpleNamespace(name=name))
        self.transfers = SimpleNamespace(
            receive_dir=RECV_DIR, decide=lambda key, accept: self.decided.append((key, accept))
        )


def _receive_session(cli, *, json_mode: bool):
    out, err = io.StringIO(), io.StringIO()
    session = cli._ReceiveSession(cli._Output(out, err, json_mode))
    session.node = _FakeReceiveNode()
    session.ready()
    return session, out, err


def _rejected_by_me():
    """`TransferFinished(rejected_user)` của bên nhận, do chính máy này từ chối (`by_peer` False)."""
    return finished("receive", "rejected_user", by_peer=False, reject=reject("user"), status="waiting")


def _mark(session, fin, why: str) -> None:
    if why == "stop":
        session.on_stop()
    else:
        session._eof_rejected.add(fin.info.key)


@pytest.mark.parametrize("why", ["eof", "stop"])
def test_json_result_message_stays_c6_when_rejected_by_eof_or_stop(cli, why):
    """`--json` (spec-p2-8 A: JSON không đổi): lượt bị từ chối vì EOF hay vì đang dừng vẫn có `result`
    `rejected_user` với `message` “Đã từ chối.”; không dòng JSON nào chứa câu M14/M15."""
    session, out, err = _receive_session(cli, json_mode=True)
    fin = _rejected_by_me()
    _mark(session, fin, why)
    session.on_transfer_event(fin)
    events = json_lines(out.getvalue())
    [res] = [e for e in events if e["event"] == "result"]
    assert (res["result"], res["by_peer"]) == ("rejected_user", False)
    assert res["message"] == R_REJECTED_SELF
    raw = out.getvalue() + err.getvalue()
    for sentence in (M14.format(n=3, s=SENDER), M15.format(n=3, s=SENDER)):
        assert sentence not in raw, raw
    assert "vì không đọc được câu trả lời" not in raw and "vì đang dừng nhận" not in raw, raw


@pytest.mark.parametrize(("why", "sentence"), [("eof", M14), ("stop", M15)], ids=["eof", "stop"])
def test_text_rejection_by_eof_or_stop_without_prompt_prints_m14_or_m15(cli, why, sentence):
    """Đối chứng cho test JSON ở trên (cùng đầu vào, chế độ text): lượt chưa hiện lời hỏi in đúng một câu
    M14 (EOF) / M15 (dừng), không C6 — để biết nhánh EOF/dừng thật sự được đi qua."""
    session, out, _ = _receive_session(cli, json_mode=False)
    fin = _rejected_by_me()
    _mark(session, fin, why)
    session.on_transfer_event(fin)
    lines = out.getvalue().splitlines()
    assert lines.count(sentence.format(n=3, s=SENDER)) == 1, lines
    assert R_REJECTED_SELF not in lines, lines


def test_m15_for_transfer_that_never_emitted_offer(cli):
    """M15: lượt chưa từng phát `offer` (`core` còn giữ trong hàng của thiết bị) bị từ chối lúc dừng →
    đúng một câu M15 với đúng số file (3) và tên máy gửi; không C6; không có sự kiện `offer` nào."""
    session, out, err = _receive_session(cli, json_mode=False)
    fin = _rejected_by_me()
    assert fin.info.key not in session._offered
    session.on_stop()
    session.on_transfer_event(fin)
    lines = out.getvalue().splitlines()
    assert lines == [M15.format(n=3, s=SENDER)], lines
    assert err.getvalue() == ""


@with_marks(socket_test)
async def test_m15_ctrl_c_with_second_transfer_from_same_sender_still_queued_in_core(
    pair_dirs, transfer_env, cli_runs, node_spy
):
    """M15, tích hợp: Laptop-Minh (một `Node` trong tiến trình — hai `teleport send` cùng thư mục cấu
    hình không chạy song song được vì `node.lock`) gửi lượt 1 (lời hỏi đang mở) rồi lượt 2 (2 file; xếp
    hàng sau lượt 1 trong `core` nên `receive` chưa có `offer`) → Ctrl+C: lượt 2 in đúng một M15 (2 file,
    Laptop-Minh), lượt 1 không in C6; mã 0."""
    from teleport.app.node import Node

    net = pair_dirs
    recv, port = await start_receiver(cli_runs, net, node_spy, json_mode=False, yes=False)
    sender = Node(net.S, name=SENDER, host=LOCAL, port=0)
    await sender.start()
    try:
        sender.connect_hint(net.rid, [LOCAL], port)
        await wait_until(lambda: net.rid in sender.connected_devices(), 8, "máy gửi chưa kết nối")
        await sender.transfers.send(net.rid, [make_file(net.src, "mot.bin", 1000)])
        await recv.wait_text(C4_ASK)
        src2 = net.tmp / "nguon-2"
        await sender.transfers.send(
            net.rid, [make_file(src2, "hai.bin", 1000), make_file(src2, "ba.bin", 10)]
        )
        await asyncio.sleep(0.5)
        assert recv.text().count("muốn gửi") == 1, "lượt 2 phải còn chờ, chưa hiện lời hỏi"
        recv.press()
        assert await recv.code(15) == 0
    finally:
        await sender.close()
    text = recv.text()
    assert text.count(M15.format(n=2, s=SENDER)) == 1, text
    assert M15.format(n=1, s=SENDER) not in text, "lượt đang hỏi không in M15"
    assert R_REJECTED_SELF not in text_lines(recv), text
    assert B13 not in text, text
