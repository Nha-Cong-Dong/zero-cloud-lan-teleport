"""Lưu file nhận an toàn: thư mục tạm `.teleport`, `.part`, `.state`, `transfer.json`, đổi tên không ghi đè.

Bố cục và các luật ở docs/architecture.md mục 4 (“Thư mục nhận”, `transfer.json`, `.state`, “Kết
thúc một file”, “Phục hồi tên đích”), luật đổi tên ở docs/protocol.md mục 4.4:

    <thư mục nhận>/.teleport/<transfer_id>/transfer.json
    <thư mục nhận>/.teleport/<transfer_id>/<file_index>.part
    <thư mục nhận>/.teleport/<transfer_id>/<file_index>.state

Chỉ các tên khớp `_LAYOUT_RE` (kể cả file tạm `*.<16 hex>.tmp` của `_write_atomic_in`) là của
Teleport; mọi thao tác xóa theo tên chỉ xóa file thường khớp regex này, không đệ quy, không theo
liên kết.

- `ReceiveDir.open(root)` mở thư mục nhận (tạo nếu chưa có). Thư mục nhận được phép là symlink
  hay junction; luật cấm liên kết chỉ áp trong cây `.teleport`. `.teleport` (`0o700` trên
  Linux/macOS; Windows: ẩn khi vừa tạo, ACL kế thừa từ thư mục nhận) được tạo ở lần
  `create_temp` đầu tiên và không bao giờ bị xóa ở đây.
- `create_temp(transfer_id, record)` tạo `<transfer_id>` và ghi `transfer.json` bền trước khi trả
  về. Thư mục có sẵn mà rỗng (hay chỉ có `.tmp` của bố cục) được dùng lại; còn lại →
  `TempExistsError`, không đụng nội dung cũ. `TransferTemp.create_part` tạo `<i>.part` bằng
  `O_EXCL` và header `<i>.state`.
- `PartFile.write_at` ghi một khối đúng vị trí và không bao giờ tạo lỗ sau cuối phần đã ghi
  (`WriteGapError`); `commit` làm bền `.part` rồi nối `MARK` vào `.state` và làm bền nó (`sync`
  dùng `F_FULLFSYNC` trên macOS); `clear` nối `CLEAR`/`CLEAR_ALL`; `reread` trả hash SHA-256 của
  từng khối đọc lại từ đĩa bằng một bộ đệm 1 khối (mvp N3) và cắt `.part` thừa.
- `ResumeLog` (`PartFile.log`): nhật ký `.state` chỉ ghi thêm; tự gộp khi dài quá (dựa trên cận
  trên độ dài bản gọn, nên không bao giờ vượt `max_state_length`), gộp trước khi nối
  `TARGET`/`RESERVED` khi số bản ghi tên gần trần.
- `finalize` theo đúng 8 bước của “Kết thúc một file”: `TARGET`, giữ chỗ bằng `O_CREAT | O_EXCL`,
  `fsync` thư mục, `RESERVED`, đổi tên đè lên chính file giữ chỗ, `fsync` thư mục, `transfer.json`
  với `verified` mới, xóa `.state`. Chọn tên so trùng NFC + casefold trên mọi OS, cắt tên cho vừa
  giới hạn đường dẫn, tên dự phòng ASCII khi hệ file từ chối tên. Không bao giờ ghi đè hay xóa
  file không phải của mình.
- `scan_temps` xếp loại các thư mục lượt; `open_for_resume` mở lại một lượt đang chạy (pha 1 chỉ
  đọc, pha 2 sửa đĩa theo bảng “Phục hồi tên đích”, không băm file lúc khởi động).
- `remove_temp` xóa các `.part` do lượt tạo (cùng danh tính), các file khác của bố cục, rồi
  `<transfer_id>` khi đã rỗng; không bao giờ ném `OSError`. `close_temp` chỉ đóng handle.
  `cleanup` dọn thư mục lượt cũ theo ba ngưỡng; `discard_temp` xóa ngay một lượt nhận dở của
  máy này (không xét ngưỡng).
- `volume_info` nhận ra hệ file FAT (trần kích thước file ở protocol mục 3.3) và dung lượng trống.

Toàn bộ API là đồng bộ (chặn), gọi từ thread; executor và hạn cho mỗi lần ghi hay `fsync` do
`app/transfers.py` quyết. `ReceiveDir` an toàn khi nhiều thread gọi song song cho các lượt khác
nhau (một `transfer_id` chỉ được một lời gọi `create_temp`, `open_for_resume`, `cleanup` hay
`discard_temp` giữ tại một thời điểm: tập `_busy`); một `TransferTemp` và các `PartFile` của nó chỉ
được một thread dùng tại một thời điểm. `remove_temp`, `close_temp` và `close` đòi không thread
nào đang dùng các `PartFile` mà chúng đóng (đóng fd giữa lúc thread khác ghi có thể làm thread đó
ghi vào một fd khác vừa được cấp lại cùng số).

Lỗi: dùng sai API → `ValueError` (riêng `name` không phải chuỗi → `TypeError`); lỗi I/O thật
→ `OSError` nguyên trạng; các tình huống riêng → lớp con của `StorageError` (không lớp nào là
con của `OSError`). Module không có câu chữ cho người dùng, và log không bao giờ chứa tên file
nhận được hay nội dung file (mvp N9): chỉ `transfer_id`, `file_index`, loại lỗi và `errno`.

Nhánh POSIX (`sys.platform != "win32"`) làm mọi thao tác trong thư mục nhận qua `dir_fd` và
`O_NOFOLLOW`; nhánh Windows dùng đường dẫn đầy đủ và `MoveFileExW`, và giữ “handle canh” (`_hold_dir`:
`FILE_FLAG_OPEN_REPARSE_POINT`, không `FILE_SHARE_DELETE`) tới `.teleport` và `<transfer_id>` suốt
đời một `TransferTemp`, nên không ai đổi tên, xóa hay thay hai thư mục đó bằng junction được. Không
có đường lùi từ POSIX sang nhánh đường dẫn: `os.replace` không có trong `os.supports_dir_fd` dù
vẫn nhận `src_dir_fd`/`dst_dir_fd`.

Bẫy:
- Windows: thiếu `O_BINARY` thì CRT đổi `\\n` thành `\\r\\n`. Không xóa hay đổi tên được file
  đang mở, nên mọi handle đóng trước khi đổi tên (cả fd nối thêm của `ResumeLog` trước khi gộp);
  antivirus giữ file → thử lại khi gặp `PermissionError`. `O_EXCL` lên tên là thư mục hay file
  đang chờ xóa có thể báo `PermissionError` thay vì `FileExistsError`. Đặt `mtime` khi handle ghi
  còn mở có thể bị NTFS đặt lại lúc đóng, nên đặt sau khi đóng. `os.mkdir(path, 0o700)` gắn DACL
  được bảo vệ mà file nhận xong mang theo khi đổi tên, nên cây `.teleport` tạo không truyền mode
  (`_TEMP_DIR_MODE`). Đường dẫn file cuối luôn ≤ `WINDOWS_MAX_PATH`, không dùng `\\\\?\\`, kể cả
  khi máy đã bật long path. Reparse point không phải name-surrogate (OneDrive Files On-Demand)
  không bị coi là liên kết. Handle canh của `<transfer_id>` phải đóng trước `rmdir` của chính nó.
- `st_ino` = 0 (FAT, ổ mạng): không có danh tính, chỉ so loại file và kích thước. Cờ `renamed`
  của `finalize`, cùng bước kiểm `.part` đã rời thư mục tạm hay chưa, chặn việc xóa nhầm file
  0 byte vừa nhận.
- Ghi xa sau cuối file trên NTFS hay FAT/exFAT bắt hệ điều hành ghi số 0 cho cả khoảng trống, lâu
  và không hủy được: `write_at` từ chối trước mọi I/O (`WriteGapError`).
- `MAX_PATH` / `PC_PATH_MAX` chỉ đo lúc `open`. Mỗi `finalize` liệt kê lại cả thư mục nhận.
- `_LAYOUT_RE` luôn dùng `fullmatch` (`$` của `re.match` khớp cả trước `\\n` cuối).
- Không dùng `os.readinto` (chỉ có từ Python 3.14).

Giới hạn đã biết:
- Windows không có `dir_fd`: giữa bước kiểm `.teleport` và lúc lấy được handle canh, một tiến
  trình có quyền ghi vào thư mục nhận vẫn đua được (handle canh tự kiểm lại, nên chỉ làm lượt
  lỗi). File của nạn nhân vẫn không bao giờ bị xóa, ghi đè hay sửa (tạo bằng `O_EXCL`, xóa thì so
  danh tính trước), và `.part` được kiểm lại danh tính trước khi đổi tên. Không lấy được handle
  canh thì mọi thao tác xóa, cắt hay thay file theo tên trong thư mục đó không chạy (A1).
- POSIX: `ReceiveDir` giữ fd của thư mục nhận tới `close()`, nên chưa tháo được ổ USB khi
  `ReceiveDir` còn mở.
- POSIX: chủ hợp lệ của cây `.teleport` là euid hoặc chủ thư mục nhận; `chmod` lỗi trên hệ file
  không hỗ trợ (vfat, exfat, ntfs-3g, cifs mount theo uid cố định) thì bỏ qua. Chạy bằng root
  trên NFS có `root_squash` vẫn có thể bị từ chối.
- Dùng lại thư mục chỉ có `.tmp` (H2) có thể đua với node khác dùng chung thư mục nhận; mtime
  trên FAT chỉ chính xác tới 2 s; mtime ở tương lai thì `cleanup` không dọn.
- `MAX_NAME_CANDIDATES` chỉ là trần an toàn nội bộ chặn vòng lặp vô hạn khi chọn tên, không phải
  số của sản phẩm.

Seam cho test (biến cấp module, đọc lúc dùng): `REPLACE_ATTEMPTS`, `REPLACE_DELAY`,
`STATE_READ_CHUNK`, `_fs_type_of`, `_MOUNTINFO_PATH`, `_parse_mountinfo`, `_path_limits`,
`_after_target`, `_after_reserve`, `_after_reserved`, `_after_replace`, `_after_record`,
`_before_atomic_replace`, `_move_file_ex` và `_hold_dir` (chỉ Windows), `_full_fsync`,
`_fsync_dir`, `_utc_now`. Thư viện chuẩn luôn được gọi qua module (`os.open`, `os.replace`,
`shutil.disk_usage`, `time.sleep`…). `core.transfer.RECONNECT_GRACE` / `REMEMBER_TTL` đọc lúc dùng.
"""

from __future__ import annotations

import contextlib
import datetime
import errno
import hashlib
import io
import itertools
import logging
import os
import re
import secrets
import shutil
import stat
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from teleport import atomic
from teleport.core import transfer as core_transfer
from teleport.core.bitfield import Bitfield
from teleport.core.blocks import BLOCK_SIZE, DIGEST_SIZE, block_count, block_length
from teleport.core.filenames import (
    FALLBACK_MAX_BYTES,
    MAX_NAME_BYTES,
    TEMP_DIR_NAME,
    dedupe_key,
    fallback_filename,
    fit_filename,
    numbered,
)
from teleport.core.messages import MAX_OFFER_FILES, validate_device_id, validate_transfer_id
from teleport.core.resume_state import (
    HEADER_SIZE,
    MAX_NAME_RECORDS,
    MAX_RECORD_BLOCKS,
    MAX_TRANSFER_RECORD,
    RECORD_MARK,
    RECORD_RESERVED,
    RECORD_TARGET,
    ReplayResult,
    StateError,
    StateReplay,
    TargetRecovery,
    TransferRecord,
    check_header,
    compact_length_for,
    compact_records,
    decode_transfer_record,
    encode_clear,
    encode_clear_all,
    encode_header,
    encode_mark,
    encode_reserved,
    encode_target,
    encode_transfer_record,
    max_state_length,
    needs_compaction,
    recover_target,
)

# sys.platform thay vì os.name: mypy chỉ hiểu phép so sánh sys.platform để bỏ qua nhánh của
# hệ điều hành khác (ctypes.WinDLL, fcntl.F_FULLFSYNC chỉ có trên một phía).
if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes
elif sys.platform == "darwin":
    import ctypes
    import fcntl

FAT_MAX_FILE_SIZE = (1 << 32) - 1  # protocol mục 3.3 (`reject.file_too_large`, FAT32)
# Tên hệ file (chữ thường) có trần FAT_MAX_FILE_SIZE (architecture mục 4 “Thư mục nhận”).
FAT_FS_TYPES = frozenset({"fat", "fat12", "fat16", "fat32", "vfat", "msdos", "umsdos"})
# MAX_PATH 260 trừ ký tự NUL, đơn vị UTF-16 code unit; áp cả khi máy đã bật long path.
WINDOWS_MAX_PATH = 259
# Phần độ dài thư mục nhận phải chừa (architecture mục 4 “Thư mục nhận”): file dài nhất của thư mục
# tạm, `<root>\.teleport\<transfer_id>\transfer.json.<16 hex>.tmp`, dài root + 78 ≤ 259; chặt hơn
# thư mục `<root>\.teleport\<transfer_id>` (root + 43 ≤ MAX_PATH − 12 − 1 = 247, CreateDirectoryW
# chừa chỗ cho tên 8.3) và `.part` dài nhất (root + 52 ≤ 259).
TEMP_PATH_RESERVE = 78
REPLACE_ATTEMPTS = atomic.REPLACE_ATTEMPTS  # protocol mục 4.4; đọc lúc dùng (test vá)
REPLACE_DELAY = atomic.REPLACE_DELAY
MAX_NAME_CANDIDATES = 100_000  # trần an toàn nội bộ khi chọn tên (docstring module)
# Thư mục lượt không còn hoạt động: dọn sau 7 ngày nếu không phải lượt của máy này (architecture
# mục 4 “Dọn tạm”); giây, đọc lúc dùng.
TEMP_RETENTION = 7 * 24 * 3600.0
STATE_READ_CHUNK = 1 << 20  # cỡ đoạn khi đọc lại hay gộp `.state`; đọc lúc dùng (test vá)
TRANSFER_RECORD_NAME = "transfer.json"
# Cờ của MoveFileExW (chỉ dùng trên Windows; architecture mục 4, ghi chú nền tảng).
MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_WRITE_THROUGH = 0x8

_MOUNTINFO_PATH = "/proc/self/mountinfo"

# Bố cục `<transfer_id>/` (architecture mục 4; `file_index` 0…999). Luôn `fullmatch`; lớp
# `[0-9]` chứ không `\d` (khớp cả chữ số Unicode).
_LAYOUT_RE = re.compile(
    r"transfer\.json|(0|[1-9][0-9]{0,2})\.(part|state)"
    r"|(transfer\.json|(0|[1-9][0-9]{0,2})\.state)\.[0-9a-f]{16}\.tmp"
)
_LAYOUT_TMP_RE = re.compile(r"(transfer\.json|(0|[1-9][0-9]{0,2})\.state)\.[0-9a-f]{16}\.tmp")
_STATE_NAME_RE = re.compile(r"(0|[1-9][0-9]{0,2})\.state")

# Cờ nào hệ điều hành không có thì lấy 0. Windows bắt buộc O_BINARY.
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
# Mở file có sẵn để kiểm (rồi `fstat`): FIFO cài sẵn không làm `open` treo thread đĩa. Với file
# thường cờ này không có tác dụng.
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIR_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC
_DIR_NOFOLLOW_FLAGS = _DIR_FLAGS | _O_NOFOLLOW
_PART_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW | _O_CLOEXEC
_PART_REOPEN_FLAGS = os.O_RDWR | _O_BINARY | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK
_RESERVE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW | _O_CLOEXEC
_ATOMIC_TMP_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY | _O_CLOEXEC
_APPEND_FLAGS = os.O_WRONLY | os.O_APPEND | _O_BINARY | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK
_READ_FLAGS = os.O_RDONLY | _O_BINARY | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK

# Chỉ dùng trên Linux/macOS. Windows tạo `.teleport` và `<transfer_id>` không truyền mode: từ
# Python 3.12.4 (CVE-2024-4030), `os.mkdir(path, 0o700)` gắn một DACL được bảo vệ (chỉ SYSTEM,
# Administrators, OWNER RIGHTS); `.part` kế thừa DACL đó và `MoveFileExW` giữ nguyên nó khi đổi
# tên, nên file nhận xong không kế thừa ACL của thư mục nhận như file tải về thường. Trên Windows
# dữ liệu đang nhận được che bằng ACL của chính thư mục nhận.
_TEMP_DIR_MODE = 0o700
_FILE_MODE = 0o666  # umask vẫn áp: file nhận xong mang quyền như file tải về thường
_STATE_FILE_MODE = 0o600  # `transfer.json`, `.state`: chỉ chủ đọc được

# Mở thư mục bằng O_NOFOLLOW | O_DIRECTORY: symlink → ELOOP, không phải thư mục → ENOTDIR.
_UNSAFE_OPEN_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR})
# fchmod lỗi trên hệ file không hỗ trợ quyền POSIX (vfat, exfat, ntfs-3g, cifs): bỏ qua.
_CHMOD_IGNORED_ERRNOS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EROFS, errno.EINVAL})
# Hệ file từ chối tên (APFS: code point lạ; vfat không UTF-8; tên quá dài) → tên dự phòng.
_NAME_REJECTED_ERRNOS = frozenset({errno.EILSEQ, errno.EINVAL, errno.ENAMETOOLONG})
# F_FULLFSYNC không được hỗ trợ (ví dụ SMB) → lùi về os.fsync.
_FULLFSYNC_UNSUPPORTED_ERRNOS = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOTTY})

# Reparse point có bit name-surrogate (symlink, junction) mới là liên kết; OneDrive Files
# On-Demand gắn reparse point không có bit này cho thư mục bình thường.
_REPARSE_TAG_NAME_SURROGATE = 0x20000000

_MOUNT_ESCAPE_RE = re.compile(r"\\([0-3][0-7]{2})")

_PRIVATE = object()  # constructor của các lớp dưới đây không phải API công khai

log = logging.getLogger(__name__)


class StorageError(Exception):
    """Lỗi của tầng lưu file nhận; `__cause__` giữ `OSError` gốc nếu có."""


class UnsafeReceiveDirError(StorageError):
    """Cây `.teleport` có symlink/junction, mục không phải thư mục, hay chủ không hợp lệ."""


class PathTooLongError(StorageError):
    """Đường dẫn thư mục nhận quá dài: không còn chỗ cho file tạm hay tên file."""


class TempExistsError(StorageError):
    """`.teleport/<transfer_id>` đã có mà không dùng lại được, hay đang được dùng; nội dung cũ không đổi."""


class NameUnavailableError(StorageError):
    """Hệ file từ chối cả tên dự phòng, hoặc đã thử quá `MAX_NAME_CANDIDATES` tên."""


class RereadStopped(StorageError):
    """`reread` hay `open_for_resume` bị dừng bằng `stop`."""


class WriteGapError(StorageError):
    """Ghi khối này sẽ tạo lỗ sau cuối phần đã ghi của `.part`; file không đổi."""


class NotResumableError(StorageError):
    """`open_for_resume` từ chối lượt này (liên kết, file lạ, bản ghi đổi, đang bận…); đĩa không đổi."""


class TempKind(StrEnum):
    """Loại thư mục lượt mà `scan_temps` thấy (architecture mục 4 “Khôi phục”)."""

    RUNNING = "running"  # `transfer.json` hợp lệ, của máy này, máy gửi còn tin cậy, chưa xong
    DONE = "done"  # như trên, đã xong (`done_at`)
    OTHER_NODE = "other_node"  # `transfer.json` hợp lệ của máy nhận khác (dùng chung thư mục nhận)
    ORPHAN = "orphan"  # không có `transfer.json`, hay máy gửi không còn tin cậy
    BROKEN = "broken"  # `transfer.json` hỏng, vượt trần, là liên kết hay không phải file thường


class ResumeMode(StrEnum):
    """Nguồn và bước bắt đầu của `PartFile` (bảng “Phục hồi tên đích”)."""

    NORMAL = "normal"  # `.part` thường; `finalize` từ bước 1
    VERIFY_TARGET = "verify_target"  # không có `.part`: đọc lại file đích; `finalize` từ bước 6
    REUSE_PLACEHOLDER = "reuse_placeholder"  # dùng lại file giữ chỗ đã có; `finalize` từ bước 5


@dataclass(frozen=True, slots=True)
class VolumeInfo:
    """Hệ file và dung lượng trống của ổ chứa thư mục nhận."""

    # Tên hệ file chữ thường như OS báo ("ntfs", "fat32", "vfat", "msdos", "exfat", "apfs",
    # "ext4", "fuseblk"…); None = không nhận ra.
    fs_type: str | None
    max_file_size: int | None  # FAT_MAX_FILE_SIZE với hệ file FAT; None = không có giới hạn đã biết
    free_bytes: int  # shutil.disk_usage(root).free


@dataclass(frozen=True, slots=True)
class PathLimits:
    """Giới hạn độ dài đo lúc mở thư mục nhận."""

    max_path: int  # đường dẫn đầy đủ tối đa, không tính NUL; Windows: UTF-16 code unit, POSIX: byte
    max_name_bytes: int  # tên tối đa, byte UTF-8, ≤ 255


@dataclass(frozen=True, slots=True)
class TempScan:
    """Một thư mục lượt trong `.teleport` (`ReceiveDir.scan_temps`).

    `record`: `transfer.json` đã giải mã (None khi không có hay hỏng); `last_activity`: “hoạt
    động cuối” theo giờ tường (giây từ epoch, architecture mục 4 “Dọn tạm”), None nếu không đọc được.
    """

    transfer_id: str
    kind: TempKind
    record: TransferRecord | None
    last_activity: float | None


@dataclass(frozen=True, slots=True)
class ResumeHandle:
    """Lượt nhận đã mở lại bằng `ReceiveDir.open_for_resume`.

    `bits`, `leaves`: khối đã có và hash từng khối (khối bit 0 có hash 0) sau khi đã tắt mọi khối
    không nằm trọn trong `.part`; `part_blocks`: số khối đầu nằm trọn trong `.part` (`n` nếu
    `.part` đủ `size`, hay ở `VERIFY_TARGET`). `temp.record` là `record`.
    """

    temp: TransferTemp
    record: TransferRecord
    file_index: int
    part: PartFile
    bits: Bitfield
    leaves: bytearray
    part_blocks: int
    mode: ResumeMode
    last_activity: float


def _check_key(key: object) -> None:
    if key is not _PRIVATE:
        raise TypeError("không tạo trực tiếp: dùng ReceiveDir.open, create_temp, create_part")


def _units(path: str) -> int:
    """Độ dài `path` theo đơn vị của `PathLimits.max_path`."""
    if sys.platform == "win32":
        return len(path.encode("utf-16-le", "surrogatepass")) // 2
    return len(os.fsencode(path))


def _identity(st: os.stat_result) -> tuple[int, int] | None:
    """Danh tính `(st_dev, st_ino)`; `st_ino` = 0 (FAT, ổ mạng) → None: chỉ so loại file và kích thước."""
    if st.st_ino == 0:
        return None
    return (st.st_dev, st.st_ino)


def _same_identity(identity: tuple[int, int] | None, st: os.stat_result) -> bool:
    return identity is None or (st.st_dev, st.st_ino) == identity


def _is_link(st: os.stat_result) -> bool:
    """Symlink, hoặc (Windows) reparse point name-surrogate như junction."""
    if stat.S_ISLNK(st.st_mode):
        return True
    if sys.platform == "win32":
        return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) and bool(
            st.st_reparse_tag & _REPARSE_TAG_NAME_SURROGATE
        )
    return False


def _has_reparse_point(st: os.stat_result) -> bool:
    if sys.platform == "win32":
        return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _is_plain_file(st: os.stat_result) -> bool:
    """File thường, không phải liên kết, (Windows) không gắn reparse point nào."""
    return stat.S_ISREG(st.st_mode) and not _has_reparse_point(st)


def _errno_name(e: OSError) -> str:
    return errno.errorcode.get(e.errno, str(e.errno)) if e.errno is not None else "-"


def _is_transfer_id(name: str) -> bool:
    try:
        validate_transfer_id(name)
    except ValueError:
        return False
    return True


def _retry_on_permission(action: Callable[[], None]) -> None:
    """Chạy `action`; `PermissionError` → ngủ REPLACE_DELAY rồi thử lại, tối đa REPLACE_ATTEMPTS lần."""
    attempts = max(1, REPLACE_ATTEMPTS)
    for attempt in range(attempts):
        try:
            action()
            return
        except PermissionError:
            if attempt >= attempts - 1:
                raise
            time.sleep(REPLACE_DELAY)


def _full_fsync(fd: int) -> None:
    """Làm bền dữ liệu của `fd`: macOS `F_FULLFSYNC` (không hỗ trợ → `os.fsync`), OS khác `os.fsync`."""
    if sys.platform == "darwin":
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError as e:
            if e.errno not in _FULLFSYNC_UNSUPPORTED_ERRNOS:
                raise
    os.fsync(fd)


def _fsync_dir(fd: int) -> None:
    """POSIX: làm bền mục của thư mục `fd` (qua `_full_fsync`); lỗi bỏ qua, log DEBUG. Windows: không làm gì.

    Một số hệ file (SMB…) không `fsync` được thư mục, như `teleport.atomic`.
    """
    if sys.platform == "win32":
        return
    try:
        _full_fsync(fd)
    except OSError as e:
        log.debug("fsync thư mục lỗi (%s), bỏ qua", _errno_name(e))


def _utc_now() -> datetime.datetime:
    """Giờ tường UTC cho `done_at` của `transfer.json` (seam cho test)."""
    return datetime.datetime.now(datetime.UTC)


def _after_target(target: Path) -> None:
    """Seam cho test: bước 1 của “Kết thúc một file” — `TARGET` của ứng viên `target` đã bền."""


def _after_reserve(target: Path) -> None:
    """Seam cho test: chạy sau khi file giữ chỗ đã bền, trước bước kiểm lại và đổi tên."""


def _after_reserved(target: Path) -> None:
    """Seam cho test: bước 4 — `RESERVED` của `target` đã bền, chưa đổi tên."""


def _after_replace(target: Path) -> None:
    """Seam cho test: bước 6 — file đã mang tên thật `target` và thư mục nhận đã `fsync`."""


def _after_record(target: Path) -> None:
    """Seam cho test: bước 7 — `transfer.json` đã ghi file `target` là `verified`, `.state` còn."""


def _before_atomic_replace(directory: Path, name: str) -> None:
    """Seam cho test: `_write_atomic_in` đã ghi và `fsync` file tạm của `name`, chưa đổi tên."""


def _path_limits(root: str) -> PathLimits:
    """Giới hạn độ dài đường dẫn và tên của thư mục `root` (architecture mục 4 “Thư mục nhận”)."""
    if sys.platform == "win32":
        return PathLimits(WINDOWS_MAX_PATH, MAX_NAME_BYTES)
    max_path = 1023
    max_name = MAX_NAME_BYTES
    try:
        value = os.pathconf(root, "PC_PATH_MAX")
        if value > 1:
            max_path = value - 1
    except (OSError, ValueError):
        pass
    try:
        value = os.pathconf(root, "PC_NAME_MAX")
        if value >= 1:
            max_name = min(MAX_NAME_BYTES, value)
    except (OSError, ValueError):
        pass
    return PathLimits(max_path, max_name)


def _unescape_mount_field(field: str) -> str:
    """Giải mã escape bát phân `\\NNN` của mountinfo (`\\040` dấu cách…); escape sai để nguyên."""

    def decode(match: re.Match[str]) -> str:
        value = int(match.group(1), 8)
        # Byte ≥ 0x80 giữ ở dạng surrogateescape, như os.fsdecode.
        return chr(value) if value < 0x80 else chr(0xDC00 + value)

    return _MOUNT_ESCAPE_RE.sub(decode, field)


def _parse_mountinfo(text: str, path: str) -> str | None:
    """`fstype` của mount point dài nhất chứa `path` trong nội dung `/proc/self/mountinfo`.

    Mỗi dòng: `id parent major:minor root mountpoint options [tùy chọn…] - fstype source superopts`.
    Mount point khớp khi bằng `path`, là `/`, hoặc là tiền tố của `path` theo ranh giới thành phần
    (`/mnt/usb` không khớp `/mnt/usb2/x`). Trùng mount point thì dòng sau thắng (mount sau che
    mount trước). Dòng hỏng bị bỏ qua; không khớp dòng nào → None. Không bao giờ raise.
    """
    best: str | None = None
    best_len = -1
    for line in text.split("\n"):
        fields = line.split(" ")
        try:
            separator = fields.index("-", 6)
        except ValueError:
            continue
        if separator + 1 >= len(fields) or not fields[separator + 1]:
            continue
        mount = _unescape_mount_field(fields[4])
        if not mount.startswith("/") or len(mount) < best_len:
            continue
        if mount == path or mount == "/" or path.startswith(mount + "/"):
            best, best_len = fields[separator + 1], len(mount)
    return best


if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _GetVolumePathNameW = _kernel32.GetVolumePathNameW
    _GetVolumePathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    _GetVolumePathNameW.restype = wintypes.BOOL

    _GetVolumeInformationW = _kernel32.GetVolumeInformationW
    _GetVolumeInformationW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPDWORD,
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    _GetVolumeInformationW.restype = wintypes.BOOL

    _SetFileAttributesW = _kernel32.SetFileAttributesW
    _SetFileAttributesW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    _SetFileAttributesW.restype = wintypes.BOOL

    _MoveFileExW = _kernel32.MoveFileExW
    _MoveFileExW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    _MoveFileExW.restype = wintypes.BOOL

    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _CreateFileW.restype = wintypes.HANDLE

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)
    _CloseHandle.restype = wintypes.BOOL

    _GetFileInformationByHandleEx = _kernel32.GetFileInformationByHandleEx
    _GetFileInformationByHandleEx.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    _GetFileInformationByHandleEx.restype = wintypes.BOOL

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = (("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD))

    _FS_NAME_CHARS = 261  # MAX_PATH + 1, như tài liệu GetVolumeInformationW
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_WRITE = 0x2
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_ATTRIBUTE_TAG_INFO_CLASS = 9  # FileAttributeTagInfo
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    def _windows_fs_type(path: str) -> str | None:
        size = 2 * len(path) + 64  # đủ cho đường dẫn dài, kể cả dấu `\` cuối
        volume = ctypes.create_unicode_buffer(size)
        if not _GetVolumePathNameW(path, volume, size):
            return None
        fs_name = ctypes.create_unicode_buffer(_FS_NAME_CHARS)
        if not _GetVolumeInformationW(volume.value, None, 0, None, None, None, fs_name, _FS_NAME_CHARS):
            return None
        return fs_name.value or None

    def _move_file_ex(src: str, dst: str, flags: int) -> None:
        """`MoveFileExW`; lỗi → OSError từ `ctypes.WinError` (WinError 5/32 → PermissionError)."""
        if not _MoveFileExW(src, dst, flags):
            raise ctypes.WinError(ctypes.get_last_error())

    def _set_hidden(path: str, attributes: int) -> None:
        """Thêm FILE_ATTRIBUTE_HIDDEN, giữ các thuộc tính khác; lỗi → log DEBUG."""
        if not _SetFileAttributesW(path, attributes | stat.FILE_ATTRIBUTE_HIDDEN):
            log.debug("không đặt được thuộc tính ẩn cho .teleport (WinError %d)", ctypes.get_last_error())

    def _hold_dir(path: str) -> int:
        """Lấy “handle canh” của thư mục `path`; trả fd (đóng bằng `os.close`).

        `CreateFileW(FILE_LIST_DIRECTORY, FILE_SHARE_READ | FILE_SHARE_WRITE,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT)`: không chia sẻ quyền xóa, nên
        khi handle còn mở không ai đổi tên, xóa hay thay thư mục đó (kể cả bằng junction); file bên
        trong vẫn tạo, đổi tên, xóa được. Kiểm trên chính handle (không `lstat` lại sau đó): không
        phải thư mục hay là reparse point name-surrogate → `UnsafeReceiveDirError`; lỗi mở → OSError.
        Chương trình khác (antivirus, trình lập chỉ mục) đang mở thư mục mà không chia sẻ ghi →
        `PermissionError` (WinError 32): thử lại như `_retry_on_permission`.
        """
        attempts = max(1, REPLACE_ATTEMPTS)
        for attempt in range(attempts):
            handle = _CreateFileW(
                path,
                _FILE_LIST_DIRECTORY,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                None,
                _OPEN_EXISTING,
                _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if handle is not None and handle != _INVALID_HANDLE_VALUE:
                break
            error = ctypes.WinError(ctypes.get_last_error())
            if not isinstance(error, PermissionError) or attempt >= attempts - 1:
                raise error
            time.sleep(REPLACE_DELAY)
        try:
            info = _FileAttributeTagInfo()
            if not _GetFileInformationByHandleEx(
                handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not info.FileAttributes & stat.FILE_ATTRIBUTE_DIRECTORY:
                raise UnsafeReceiveDirError("mục trong cây .teleport không phải thư mục")
            if info.FileAttributes & stat.FILE_ATTRIBUTE_REPARSE_POINT and (
                info.ReparseTag & _REPARSE_TAG_NAME_SURROGATE
            ):
                raise UnsafeReceiveDirError("mục trong cây .teleport là liên kết")
            return msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            _CloseHandle(handle)
            raise

elif sys.platform == "darwin":

    class _StatFs(ctypes.Structure):
        # struct statfs bố cục 64-bit inode (<sys/mount.h>, MFSTYPENAMELEN 16, MAXPATHLEN 1024).
        _fields_ = (
            ("f_bsize", ctypes.c_uint32),
            ("f_iosize", ctypes.c_int32),
            ("f_blocks", ctypes.c_uint64),
            ("f_bfree", ctypes.c_uint64),
            ("f_bavail", ctypes.c_uint64),
            ("f_files", ctypes.c_uint64),
            ("f_ffree", ctypes.c_uint64),
            ("f_fsid", ctypes.c_int32 * 2),
            ("f_owner", ctypes.c_uint32),
            ("f_type", ctypes.c_uint32),
            ("f_flags", ctypes.c_uint32),
            ("f_fssubtype", ctypes.c_uint32),
            ("f_fstypename", ctypes.c_char * 16),
            ("f_mntonname", ctypes.c_char * 1024),
            ("f_mntfromname", ctypes.c_char * 1024),
            ("f_flags_ext", ctypes.c_uint32),
            ("f_reserved", ctypes.c_uint32 * 7),
        )

    def _darwin_fs_type(path: str) -> str | None:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        try:
            statfs = getattr(libc, "statfs$INODE64")  # x86_64
        except AttributeError:
            statfs = libc.statfs  # arm64: bố cục 64-bit inode là mặc định
        statfs.argtypes = (ctypes.c_char_p, ctypes.POINTER(_StatFs))
        statfs.restype = ctypes.c_int
        buf = _StatFs()
        if statfs(os.fsencode(path), ctypes.byref(buf)) != 0:
            return None
        name: bytes = buf.f_fstypename
        return name.decode("utf-8", "replace") or None


def _guard(path: str) -> int:
    """Windows: handle canh của thư mục `path` qua seam `_hold_dir` (đọc lúc dùng). Nơi khác không gọi."""
    if sys.platform == "win32":
        return _hold_dir(path)
    raise AssertionError("handle canh chỉ có trên Windows")


def _fs_type_of(path: str) -> str | None:
    """Tên hệ file chứa `path` như hệ điều hành báo; không nhận ra hay lỗi → None. Không raise."""
    try:
        if sys.platform == "win32":
            return _windows_fs_type(path)
        if sys.platform == "darwin":
            return _darwin_fs_type(path)
        if sys.platform.startswith("linux"):
            with open(_MOUNTINFO_PATH, encoding="utf-8", errors="surrogateescape") as f:
                return _parse_mountinfo(f.read(), path)
    except Exception as e:
        log.debug("không nhận ra hệ file: %s", type(e).__name__)
    return None


def volume_info(path: str | os.PathLike[str]) -> VolumeInfo:
    """Hệ file và dung lượng trống của ổ chứa `path` (ke_hoach mục 8, dòng FAT32).

    Hệ file thuộc `FAT_FS_TYPES` → `max_file_size = FAT_MAX_FILE_SIZE`; `exfat`, `fuseblk` và
    hệ file không nhận ra → None (lỗi `EFBIG` đi theo lỗi ghi đĩa). Lỗi đọc dung lượng → OSError.
    """
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        fs_type = None
    else:
        fs_type = _fs_type_of(real)
    if fs_type is not None:
        fs_type = fs_type.strip().lower() or None
    max_file_size = FAT_MAX_FILE_SIZE if fs_type in FAT_FS_TYPES else None
    free = shutil.disk_usage(path).free
    return VolumeInfo(fs_type=fs_type, max_file_size=max_file_size, free_bytes=free)


# --- thao tác theo tên trong một thư mục đã mở ------------------------------------------------


class _DirRef:
    """Thư mục đã mở để thao tác theo tên bên trong.

    POSIX: qua `fd` (`dir_fd`, mọi lần mở thêm `O_NOFOLLOW`); Windows: qua đường dẫn `path`, bên
    gọi giữ handle canh của thư mục đó. Không sở hữu fd: bên gọi đóng.
    """

    __slots__ = ("_fd", "_path")

    def __init__(self, path: str, fd: int | None) -> None:
        self._path = path
        self._fd = fd

    def _dir_fd(self) -> int:
        if self._fd is None:
            raise ValueError("thư mục đã đóng")
        return self._fd

    def join(self, name: str) -> str:
        return os.path.join(self._path, name)

    def lstat(self, name: str) -> os.stat_result:
        if sys.platform == "win32":
            return os.lstat(self.join(name))
        return os.lstat(name, dir_fd=self._dir_fd())

    def open(self, name: str, flags: int, mode: int = _FILE_MODE) -> int:
        if sys.platform == "win32":
            return os.open(self.join(name), flags, mode)
        return os.open(name, flags, mode, dir_fd=self._dir_fd())

    def unlink(self, name: str) -> None:
        """Xóa mục `name` (không theo liên kết); `PermissionError` → thử lại."""
        if sys.platform == "win32":
            path = self.join(name)
            _retry_on_permission(lambda: os.unlink(path))
            return
        fd = self._dir_fd()
        _retry_on_permission(lambda: os.unlink(name, dir_fd=fd))

    def listdir(self) -> list[str]:
        if sys.platform == "win32":
            return os.listdir(self._path)
        # fd mới cho mỗi lần liệt kê: fd nhân bản dùng chung vị trí đọc thư mục.
        fd = os.open(".", _DIR_FLAGS, dir_fd=self._dir_fd())
        try:
            return os.listdir(fd)
        finally:
            os.close(fd)


def _write_all(fd: int, data: bytes | bytearray | memoryview) -> None:
    """Ghi hết `data` vào `fd`; ghi thiếu thì ghi tiếp tới đủ. Lỗi → OSError."""
    with memoryview(data) as raw:
        view = raw if raw.format == "B" and raw.ndim == 1 else raw.cast("B")
        written = 0
        total = view.nbytes
        while written < total:
            n = os.write(fd, view[written:])
            if n <= 0:
                raise OSError(errno.EIO, "ghi không tiến được")
            written += n


def _read_limited(fd: int, limit: int) -> bytes:
    """Đọc từ `fd` tới hết file hay tới `limit` byte (đủ để biết file dài quá `limit − 1`)."""
    chunks: list[bytes] = []
    got = 0
    while got < limit:
        chunk = os.read(fd, min(limit - got, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _refused(transfer_id: str) -> NotResumableError:
    return NotResumableError(f"lượt {transfer_id}: không khôi phục")


def _check_stop(stop: threading.Event | None, what: str) -> None:
    if stop is not None and stop.is_set():
        raise RereadStopped(f"{what}: bị dừng")


def _hash_blocks(fd: int, size: int, count: int, stop: threading.Event | None, label: str) -> bytearray:
    """Hash SHA-256 của từng khối đọc từ `fd` (từ đầu), nối liền; khối đọc thiếu giữ 32 byte 0."""
    result = bytearray(DIGEST_SIZE * count)
    if not count:
        return result
    buffer = bytearray(BLOCK_SIZE)
    os.lseek(fd, 0, os.SEEK_SET)
    with io.FileIO(fd, "r", closefd=False) as reader, memoryview(buffer) as view:
        for i in range(count):
            _check_stop(stop, label)
            length = block_length(size, i)
            got = 0
            while got < length:
                n = reader.readinto(view[got:length])
                if not n:
                    break
                got += n
            if got < length:
                break  # hết file: khối này và mọi khối sau giữ 32 byte 0
            start = DIGEST_SIZE * i
            result[start : start + DIGEST_SIZE] = hashlib.sha256(view[:length]).digest()
    return result


def _clear_bits_from(bits: Bitfield, leaves: bytearray, start: int) -> list[int]:
    """Tắt mọi bit ≥ `start` (hash về 0); trả các chỉ số vừa tắt, tăng dần."""
    cleared: list[int] = []
    raw = bits.to_bytes()
    for byte_index in range(start >> 3, len(raw)):
        byte = raw[byte_index]
        if not byte:
            continue
        base = byte_index * 8
        for bit in range(8):
            i = base + bit
            if i >= start and byte & (0x80 >> bit):
                cleared.append(i)
    for i in cleared:
        bits.clear(i)
        leaves[i * DIGEST_SIZE : (i + 1) * DIGEST_SIZE] = bytes(DIGEST_SIZE)
    return cleared


def _write_atomic_in(
    temp: TransferTemp, name: str, chunks: Iterable[bytes | bytearray | memoryview]
) -> tuple[int, int] | None:
    """Ghi nguyên tử file `name` (`transfer.json` hay `<i>.state`) trong thư mục tạm của `temp`.

    Tạo `"{name}.{16 hex}.tmp"` bằng `O_CREAT | O_EXCL | O_NOFOLLOW` (quyền `0o600`), ghi các
    đoạn, `fsync`, đóng; seam `_before_atomic_replace`; đổi tên đè lên `name` (POSIX `os.replace`
    qua `dir_fd` của thư mục tạm; Windows kiểm `<transfer_id>` còn là thư mục cũ, không phải liên
    kết, rồi `MoveFileExW(REPLACE_EXISTING | WRITE_THROUGH)`), thử lại khi `PermissionError`;
    POSIX `fsync` thư mục tạm (`_fsync_dir`). Trả danh tính file mới (None khi `st_ino` = 0).

    Bên gọi phải đóng mọi handle tới `name` trước. `name` khác hai dạng trên → ValueError. Lỗi →
    xóa file tạm (bỏ qua lỗi xóa), ném OSError nguyên trạng: `name` hoặc là bản cũ, hoặc đã là
    bản mới. Windows không giữ được handle canh → `UnsafeReceiveDirError`, không làm gì (A1).
    """
    if name != TRANSFER_RECORD_NAME and _STATE_NAME_RE.fullmatch(name) is None:
        raise ValueError("chỉ ghi nguyên tử transfer.json hay <file_index>.state")
    ref = temp._ref()
    temp._check_dir()
    tmp = f"{name}.{secrets.token_hex(8)}.tmp"
    fd = ref.open(tmp, _ATOMIC_TMP_FLAGS, _STATE_FILE_MODE)
    moved = False
    try:
        try:
            for chunk in chunks:
                _write_all(fd, chunk)
            _full_fsync(fd)
            identity = _identity(os.fstat(fd))
        finally:
            os.close(fd)
        _before_atomic_replace(temp.path, name)
        if sys.platform == "win32":
            temp._check_dir()
            src, dst = ref.join(tmp), ref.join(name)
            flags = MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
            _retry_on_permission(lambda: _move_file_ex(src, dst, flags))
        else:
            temp_fd = temp._fd_or_raise()
            _retry_on_permission(lambda: os.replace(tmp, name, src_dir_fd=temp_fd, dst_dir_fd=temp_fd))
        moved = True
    except BaseException:
        if not moved:
            try:
                ref.unlink(tmp)
            except (OSError, ValueError) as e:
                log.debug("lượt %s: không xóa được file tạm (%s)", temp.transfer_id, type(e).__name__)
        raise
    if sys.platform != "win32":
        _fsync_dir(temp._fd_or_raise())
    return identity


class ReceiveDir:
    """Thư mục nhận đã mở. Chỉ tạo bằng `ReceiveDir.open`; dùng với `with` để đóng fd."""

    def __init__(self, key: object, root: str, limits: PathLimits, root_fd: int | None) -> None:
        _check_key(key)
        self._root = Path(root)
        self._root_str = str(self._root)
        self._limits = limits
        self._root_fd = root_fd  # POSIX: fd của thư mục nhận, giữ tới close()
        self._lock = threading.Lock()  # chỉ khóa sổ sách; I/O nằm ngoài khóa
        self._closed = False
        self._temps: set[TransferTemp] = set()  # temp còn mở, để close() đóng fd
        # `transfer_id` đang được `create_temp`, `open_for_resume` hay `cleanup` làm việc.
        self._busy: set[str] = set()

    @classmethod
    def open(cls, root: str | os.PathLike[str]) -> ReceiveDir:
        """Mở thư mục nhận `root` (tạo nếu chưa có, kể cả thư mục cha, quyền theo umask).

        `root` được phép là symlink hay junction. Không phải thư mục → NotADirectoryError; lỗi
        tạo → OSError. Quá dài so với giới hạn đường dẫn (phải chừa `TEMP_PATH_RESERVE`) →
        `PathTooLongError`. Không tạo `.teleport`; nếu đã có mà là liên kết, không phải thư mục
        hay sai chủ → `UnsafeReceiveDirError`.
        """
        try:
            root_str = os.fspath(root)
        except TypeError as e:
            raise ValueError("root phải là đường dẫn") from e
        if not isinstance(root_str, str):
            raise ValueError("root phải là đường dẫn dạng chuỗi")
        root_str = os.path.abspath(root_str)
        if not os.path.isdir(root_str):
            _check_root_length(root_str)  # kiểm trước khi tạo: không tạo thư mục không dùng được
            with contextlib.suppress(FileExistsError):  # có mà không phải thư mục: báo ngay dưới đây
                os.makedirs(root_str, exist_ok=True)
            if not os.path.isdir(root_str):
                raise NotADirectoryError(errno.ENOTDIR, "thư mục nhận không phải là thư mục", root_str)
        limits = _check_root_length(root_str)
        root_fd = None
        if sys.platform != "win32":
            root_fd = os.open(root_str, _DIR_FLAGS)
        receive_dir = cls(_PRIVATE, root_str, limits, root_fd)
        try:
            receive_dir._check_existing_temp_root()
        except BaseException:
            receive_dir.close()
            raise
        return receive_dir

    @property
    def root(self) -> Path:
        """Đường dẫn tuyệt đối (`os.path.abspath`) của thư mục nhận; không resolve symlink."""
        return self._root

    @property
    def limits(self) -> PathLimits:
        return self._limits

    @property
    def closed(self) -> bool:
        return self._closed

    def volume_info(self) -> VolumeInfo:
        """`volume_info` của thư mục nhận. Đã đóng → ValueError; lỗi đọc dung lượng → OSError."""
        self._ensure_open()
        return volume_info(self._root)

    def create_temp(self, transfer_id: str, record: TransferRecord) -> TransferTemp:
        """Tạo `.teleport/<transfer_id>/` (`0o700`) và ghi `transfer.json` bền (architecture mục 4).

        Kiểm trước mọi I/O: `transfer_id` đúng 32 ký tự hex thường (kiểm lại ở đây vì nó thành tên
        thư mục; `teleport.core.messages.validate_transfer_id`, sai → `MessageError`, lớp con của
        ValueError); `record` là `TransferRecord` của đúng lượt này, `verified` = 0, chưa có
        `done_at` (sai → ValueError). Lượt đang được tạo, mở lại hay dọn trong `ReceiveDir` này,
        hay đã có temp mở cùng `transfer_id` → `TempExistsError`.

        Thư mục đã có (H2): là thư mục thật, đúng chủ, rỗng hay chỉ có file thường khớp dạng
        `.tmp` của bố cục → xóa chúng rồi tạo lại; còn lại (có `.part`, `transfer.json`, file lạ,
        liên kết, `<transfer_id>` là file) → `TempExistsError`, không đụng gì. POSIX: `fsync`
        thư mục nhận khi vừa tạo `.teleport`, `fsync` `.teleport` sau khi tạo `<transfer_id>`;
        `transfer.json` ghi bằng `_write_atomic_in`, nên đã bền khi hàm trả về. Liên kết hay sai
        chủ ở `.teleport` → `UnsafeReceiveDirError`; lỗi đĩa → OSError (đã dọn đúng những gì
        hàm này tạo).
        """
        self._ensure_open()
        validate_transfer_id(transfer_id)
        if not isinstance(record, TransferRecord):
            raise ValueError("record phải là TransferRecord")
        if record.offer.transfer_id != transfer_id:
            raise ValueError("record.offer.transfer_id khác transfer_id")
        if record.verified != 0 or record.done_at is not None:
            raise ValueError("record của lượt mới phải có verified = 0 và chưa có done_at")
        data = encode_transfer_record(record)  # StateError (ValueError) trước mọi I/O
        self._claim(transfer_id, TempExistsError(f"thư mục tạm của lượt {transfer_id} đang được dùng"))
        try:
            if sys.platform == "win32":
                temp = self._create_temp_windows(transfer_id, record, data)
                parent_fd = None
            else:
                temp, parent_fd = self._create_temp_posix(transfer_id, record, data)
            try:
                with self._lock:
                    closed = self._closed
                    if not closed:
                        self._temps.add(temp)
                if closed:
                    self._abandon_new_temp(temp, parent_fd)
                    raise ValueError("ReceiveDir đã đóng")
            finally:
                if parent_fd is not None:
                    _close_quietly(parent_fd)
            return temp
        finally:
            self._release(transfer_id)

    def finalize(self, part: PartFile, name: str, *, mtime_ms: int | None = None) -> Path:
        """Đổi `.part` đã kiểm xong sang tên thật trong thư mục nhận; trả đường dẫn file cuối.

        Theo architecture mục 4 “Kết thúc một file”, bước bắt đầu suy từ `part.mode`:

        - `NORMAL` (bước 1–8): đặt `mtime`, `fsync` `.part` rồi đóng nó; chọn tên không trùng file
          nào đã có (NFC + casefold, cắt cho vừa giới hạn đường dẫn); với mỗi ứng viên thật sự thử
          giữ chỗ: nối `TARGET` + `fsync` `.state`, giữ chỗ bằng `O_CREAT | O_EXCL`, làm bền tên đó,
          kiểm lại file giữ chỗ (khác đi → để nguyên, chọn tên mới), nối `RESERVED` + `fsync`, kiểm
          lại file giữ chỗ và `.part`, đổi tên đè lên chính file giữ chỗ (`PermissionError` → thử
          lại); hệ file từ chối tên → tên dự phòng ASCII; ứng viên `encode_target` từ chối bị bỏ.
        - `REUSE_PLACEHOLDER`: tên lấy từ `TARGET` đã lưu (không dùng `name`); file giữ chỗ còn
          nguyên (file thường, 0 byte, cùng danh tính) → bước 5–8; khác đi → nối lại `TARGET` tên
          đó (quên `RESERVED`) rồi chạy như `NORMAL`.
        - `VERIFY_TARGET`: file đích còn là file thường, đúng `size`, cùng danh tính → bước 6–8;
          khác → `StorageError`.

        Sau đổi tên: `fsync` thư mục nhận (6), `transfer.json` với `verified` = `file_index` + 1
        (và `done_at` nếu là file cuối) (7), đóng nhật ký và xóa `.state` (8, lỗi chỉ log DEBUG).

        Lỗi sau khi giữ chỗ mà chưa đổi tên xong → xóa file giữ chỗ nếu nó vẫn là của mình (0 byte,
        cùng danh tính); nếu `RESERVED` đã có thể bền thì trước đó nối lại `TARGET` tên đó + `fsync`
        (không ghi được → để nguyên file giữ chỗ). Lỗi sau bước 5 → OSError, file đã mang tên thật
        giữ nguyên. `part` đã đóng từ bước `fsync`, nên không finalize lại được. Dùng sai API
        (kể cả `transfer.json` chưa ghi đủ các file trước) → ValueError (`name` không phải chuỗi →
        TypeError); `.part` khác kích thước hay bị thay → `StorageError`; hết tên →
        `NameUnavailableError`; không còn chỗ cho tên → `PathTooLongError`; lỗi đĩa → OSError.
        """
        self._ensure_open()
        if not isinstance(part, PartFile) or part._temp._owner is not self:
            raise ValueError("part không thuộc ReceiveDir này")
        if part._closed or part._finalized:
            raise ValueError("part đã đóng hoặc đã finalize")
        if mtime_ms is not None and (type(mtime_ms) is not int or mtime_ms < 0):
            raise ValueError("mtime_ms phải là None hoặc số nguyên ≥ 0")
        if not isinstance(name, str):
            raise TypeError("name phải là chuỗi")
        if part._temp._record.verified != part.file_index:
            raise ValueError("transfer.json chưa ghi đủ các file trước file này")
        if part.mode is ResumeMode.VERIFY_TARGET:
            candidate = part._target_name
            assert candidate is not None
            if not self._target_intact(part):
                raise StorageError(f"{part._label()}: file đích đã bị thay, không ghi là verified")
            part._closed = True
            part._finalized = True
        else:
            fd = part._fd
            if fd is None:
                raise ValueError("part đã đóng hoặc đã finalize")
            if os.fstat(fd).st_size != part.size:
                raise StorageError(f"{part._label()}: .part không đúng kích thước, không đổi tên")
            self._flush_part(part, fd, mtime_ms)  # F1
            reused: str | None = None
            if part.mode is ResumeMode.REUSE_PLACEHOLDER:
                reused = self._reuse_reserved(part)  # bước 5
            if reused is None:
                taken = {dedupe_key(entry) for entry in self._list_root()}  # F2
                reused = self._place(part, name, taken)  # bước 1–5
            candidate = reused
        path = self._root / candidate
        self._sync_root()  # bước 6
        _after_replace(path)
        self._record_verified(part)  # bước 7
        _after_record(path)
        self._drop_state(part)  # bước 8
        return path

    def remove_temp(self, temp: TransferTemp, *, keep_record: bool = False) -> bool:
        """Xóa thư mục tạm của một lượt (architecture mục 4 “Dọn một lượt”).

        Đóng mọi `PartFile` (và nhật ký `.state`) còn mở của `temp` (điều kiện trước: không thread
        nào đang dùng chúng). `.part` chưa finalize mà còn là file thường cùng danh tính lúc tạo →
        xóa; khác đi → để nguyên. Xóa thêm các file thường khớp bố cục: `.state`, `.tmp` và
        `transfer.json` (trừ khi `keep_record`); file lạ, liên kết, thư mục con để nguyên; không đệ
        quy, không xóa `.teleport`. Rồi `<transfer_id>` khi đã rỗng. Gọi lại được. Trả True khi và
        chỉ khi `<transfer_id>` không còn. Không bao giờ ném OSError (lỗi chỉ ghi log DEBUG).
        `temp` của `ReceiveDir` khác, đã `close_temp`, hay `ReceiveDir` đã đóng → ValueError.
        """
        if not isinstance(temp, TransferTemp) or temp._owner is not self:
            raise ValueError("temp không thuộc ReceiveDir này")
        if type(keep_record) is not bool:
            raise ValueError("keep_record phải là bool")
        self._ensure_open()
        if temp._removed:
            return self._temp_dir_gone(temp)
        if temp._closed:
            raise ValueError("TransferTemp đã đóng")
        for part in temp._parts.values():
            part._close_quietly()
        for part in temp._parts.values():
            if not part._finalized and part._own_part:
                self._remove_part(temp, part)
        try:
            temp._check_dir()  # Windows: không còn handle canh → không xóa theo tên (A1)
            self._remove_layout(temp._ref(), keep_record=keep_record, include_part=False)
        except (OSError, StorageError, ValueError) as e:
            log.debug("lượt %s: không dọn được thư mục tạm (%s)", temp.transfer_id, type(e).__name__)
        temp._close_dir_handle()
        gone = self._remove_temp_dir(temp)
        temp._close_fd_quietly()
        with self._lock:
            self._temps.discard(temp)
        temp._removed = True
        temp._closed = True
        return gone

    def close_temp(self, temp: TransferTemp) -> None:
        """Chỉ đóng mọi handle của `temp` (`.part`, `.state`, thư mục); không xóa gì. Gọi lại được.

        Điều kiện trước như `remove_temp`. Sau đó `remove_temp(temp)` → ValueError. `temp` của
        `ReceiveDir` khác → ValueError.
        """
        if not isinstance(temp, TransferTemp) or temp._owner is not self:
            raise ValueError("temp không thuộc ReceiveDir này")
        with self._lock:
            self._temps.discard(temp)
        temp._close_quietly()

    def scan_temps(self, own_device_id: str, trusted_ids: Collection[str]) -> list[TempScan]:
        """Xếp loại mọi thư mục lượt trong `.teleport` (architecture mục 4 “Khôi phục”); chỉ đọc.

        Chỉ xét mục có tên là `transfer_id` hợp lệ và là thư mục thật (POSIX: đúng chủ), không đệ
        quy; `.teleport` chưa có → `[]`. `transfer.json` đọc tối đa `MAX_TRANSFER_RECORD` + 1 byte
        bằng `O_NOFOLLOW`, phải là file thường. `RUNNING`: hợp lệ, `receiver` = `own_device_id`,
        `sender` ∈ `trusted_ids`, chưa xong; `DONE`: như trên, đã xong; `OTHER_NODE`: hợp lệ,
        `receiver` khác; `ORPHAN`: không có `transfer.json` hay `sender` không còn tin cậy;
        `BROKEN`: `transfer.json` hỏng, vượt trần, liên kết, không phải file thường. Sắp theo
        `transfer_id`. `own_device_id` sai → ValueError; `.teleport` không an toàn →
        `UnsafeReceiveDirError`; lỗi liệt kê → OSError.
        """
        self._ensure_open()
        validate_device_id(own_device_id)
        trusted = frozenset(trusted_ids)
        wall_now = time.time()
        root = self._open_temp_root_ref()
        if root is None:
            return []
        results: list[TempScan] = []
        try:
            for transfer_id in sorted(root.names()):
                if not _is_transfer_id(transfer_id):
                    continue
                sub = root.open_dir(transfer_id)
                if sub is None:
                    continue
                try:
                    results.append(self._inspect(sub, transfer_id, own_device_id, trusted, wall_now))
                finally:
                    sub.close()
        finally:
            root.close()
        return results

    def open_for_resume(self, scan: TempScan, *, stop: threading.Event | None = None) -> ResumeHandle:
        """Mở lại lượt nhận đang chạy `scan` (architecture mục 4 “Khôi phục”); không băm file.

        Pha 1, chỉ đọc (`stop` đã đặt → `RereadStopped`; từ chối → `NotResumableError`; đĩa không
        đổi): mở thư mục lượt (không theo liên kết); đọc lại `transfer.json`, phải bằng
        `scan.record`; với file `i = verified`: `.state` hay `.part` là liên kết hay không phải file
        thường → từ chối; đọc lại `.state` theo đoạn `STATE_READ_CHUNK` trong trần
        `max_state_length` (hỏng từ header hay header không khớp → coi như chưa có trạng thái);
        `part_blocks` = số khối nằm trọn trong `.part`; áp bảng “Phục hồi tên đích”
        (`recover_target`; `VERIFY_TARGET` mà chưa đủ bit → `RESTART`).

        Pha 2, sửa đĩa: xóa `.tmp` bố cục sót lại và `<j>.state` của file đã `verified`; không có
        trạng thái dùng được → ghi lại header (`RESTART_REUSE`: kèm `TARGET` + `RESERVED`); không
        thì cắt `.state` về bản ghi tốt cuối + `fsync`; tắt bit các khối ≥ `part_blocks`; theo dòng
        của bảng: giữ hay tạo lại `.part`, nhớ danh tính file giữ chỗ / file đích. Cuối cùng gộp
        (từ bitfield và hash trong RAM) nếu cần, không thì nối `CLEAR` các bit vừa tắt + `fsync`.

        `scan` không phải `RUNNING`, `stop` sai kiểu → ValueError; lượt đang bận → `NotResumableError`;
        lỗi đĩa ở pha 2 → OSError.
        """
        self._ensure_open()
        if not isinstance(scan, TempScan) or scan.kind is not TempKind.RUNNING or scan.record is None:
            raise ValueError("chỉ mở lại được thư mục lượt RUNNING của scan_temps")
        if stop is not None and not isinstance(stop, threading.Event):
            raise ValueError("stop phải là None hoặc threading.Event")
        transfer_id = scan.transfer_id
        validate_transfer_id(transfer_id)
        self._claim(transfer_id, NotResumableError(f"lượt {transfer_id} đang được dùng"))
        try:
            return self._open_for_resume(scan, stop)
        finally:
            self._release(transfer_id)

    def cleanup(self, wall_now: float, *, own_device_id: str, skip: frozenset[str], retention: float) -> int:
        """Dọn thư mục lượt không còn hoạt động trong `.teleport` (architecture mục 4 “Dọn tạm”).

        Bỏ qua (trước khi mở bất kỳ file nào trong thư mục): `transfer_id` trong `skip`, temp đang
        mở trong `ReceiveDir` này, lượt đang được tạo, mở lại hay dọn. Ngưỡng tính từ “hoạt động
        cuối” (`last_activity`): lượt của máy này (`receiver` = `own_device_id`) chưa xong →
        `core.transfer.RECONNECT_GRACE`, đã xong → `core.transfer.REMEMBER_TTL` (đọc lúc dùng);
        còn lại (node khác, mồ côi, hỏng) → `retention` (thường `TEMP_RETENTION`). Hoạt động cuối ở
        tương lai so với `wall_now` → không dọn. Chỉ xóa file thường khớp bố cục, không theo liên
        kết, không đệ quy; `rmdir` khi rỗng; không bao giờ xóa `.teleport`. Windows không lấy được
        handle canh của một thư mục → bỏ qua nó. Trả số thư mục lượt đã xóa hết.

        Tham số sai → ValueError; `.teleport` không an toàn → `UnsafeReceiveDirError`; lỗi liệt kê
        `.teleport` → OSError (lỗi trong một thư mục lượt chỉ log DEBUG).
        """
        self._ensure_open()
        if type(wall_now) not in (int, float) or wall_now != wall_now:
            raise ValueError("wall_now phải là số giây")
        if type(retention) not in (int, float) or not retention >= 0:
            raise ValueError("retention phải là số giây ≥ 0")
        if not isinstance(skip, frozenset):
            raise ValueError("skip phải là frozenset")
        validate_device_id(own_device_id)
        root = self._open_temp_root_ref()
        if root is None:
            return 0
        removed = 0
        try:
            for transfer_id in sorted(root.names()):
                if not _is_transfer_id(transfer_id) or transfer_id in skip:
                    continue
                with self._lock:
                    if self._in_use(transfer_id):
                        continue
                    self._busy.add(transfer_id)
                try:
                    if self._cleanup_one(root, transfer_id, float(wall_now), own_device_id, float(retention)):
                        removed += 1
                except (OSError, StorageError, ValueError) as e:
                    log.debug("lượt %s: không dọn được thư mục tạm (%s)", transfer_id, type(e).__name__)
                finally:
                    self._release(transfer_id)
        finally:
            root.close()
        return removed

    def discard_temp(self, transfer_id: str, *, own_device_id: str) -> bool:
        """Xóa ngay thư mục lượt nhận dở `transfer_id` của máy này (architecture mục 4 “Khôi phục”).

        Như `cleanup` cho đúng một thư mục nhưng không xét ngưỡng thời gian: chỉ xóa khi đọc lại
        `transfer.json` thấy hợp lệ, `receiver` = `own_device_id` và chưa xong (`done_at` None).
        Lượt đang mở trong `ReceiveDir` này, đang được tạo, mở lại hay dọn → không làm gì. Chỉ xóa
        file thường khớp bố cục, không theo liên kết, không đệ quy, rồi `rmdir` khi rỗng; không
        bao giờ xóa `.teleport`. Trả True khi chính lần này xóa được `<transfer_id>`.

        Tham số sai → ValueError; `.teleport` không an toàn → `UnsafeReceiveDirError`; lỗi trong
        thư mục lượt chỉ log DEBUG (trả False).
        """
        self._ensure_open()
        validate_transfer_id(transfer_id)
        validate_device_id(own_device_id)
        root = self._open_temp_root_ref()
        if root is None:
            return False
        try:
            with self._lock:
                if self._in_use(transfer_id):
                    return False
                self._busy.add(transfer_id)
            try:
                return self._discard_one(root, transfer_id, own_device_id)
            except (OSError, StorageError, ValueError) as e:
                log.debug("lượt %s: không xóa được thư mục tạm (%s)", transfer_id, type(e).__name__)
                return False
            finally:
                self._release(transfer_id)
        finally:
            root.close()

    def close(self) -> None:
        """Đóng mọi fd (thư mục nhận, `TransferTemp`, `PartFile` còn mở); không xóa gì. Gọi lại được.

        Điều kiện trước: không thread nào đang dùng `ReceiveDir` hay các `PartFile` của nó.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            temps = list(self._temps)
            self._temps.clear()
        for temp in temps:
            temp._close_quietly()
        fd, self._root_fd = self._root_fd, None
        if fd is not None:
            _close_quietly(fd)

    def __enter__(self) -> ReceiveDir:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- sổ sách ---

    def _in_use(self, transfer_id: str) -> bool:
        """Gọi khi giữ `_lock`."""
        return transfer_id in self._busy or any(t.transfer_id == transfer_id for t in self._temps)

    def _claim(self, transfer_id: str, error: StorageError) -> None:
        with self._lock:
            if self._closed:
                raise ValueError("ReceiveDir đã đóng")
            if self._in_use(transfer_id):
                raise error
            self._busy.add(transfer_id)

    def _release(self, transfer_id: str) -> None:
        with self._lock:
            self._busy.discard(transfer_id)

    # --- kiểm và mở cây .teleport ---

    def _ensure_open(self) -> None:
        with self._lock:
            if self._closed:
                raise ValueError("ReceiveDir đã đóng")

    def _root_fd_or_raise(self) -> int:
        fd = self._root_fd
        if fd is None:
            raise ValueError("ReceiveDir đã đóng")
        return fd

    def _owner_ok(self, st: os.stat_result) -> bool:
        """POSIX: chủ hợp lệ là euid hoặc chủ của thư mục nhận (USB mount theo uid cố định)."""
        if sys.platform == "win32":
            return True
        return st.st_uid in {os.geteuid(), os.fstat(self._root_fd_or_raise()).st_uid}

    def _check_existing_temp_root(self) -> None:
        """Lúc `open`: `.teleport` đã có thì phải là thư mục thật, chủ hợp lệ. Không tạo, không chmod."""
        if sys.platform == "win32":
            self._windows_temp_root(create=False)
            return
        fd = self._open_temp_root(create=False)
        if fd is not None:
            os.close(fd)

    def _open_temp_root(self, *, create: bool) -> int | None:
        """POSIX: mở `.teleport` bằng `O_NOFOLLOW` qua fd của thư mục nhận; trả fd (bên gọi đóng).

        Chưa có: `create` → tạo (`0o700`), `fsync` thư mục nhận, rồi mở lại; không thì trả None.
        Liên kết, không phải thư mục hay sai chủ → `UnsafeReceiveDirError`. `create` còn chỉnh
        quyền về `0o700`.
        """
        root_fd = self._root_fd_or_raise()
        try:
            fd = os.open(TEMP_DIR_NAME, _DIR_NOFOLLOW_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            if not create:
                return None
            # Tiến trình khác vừa tạo chen cũng được: bước mở dưới đây kiểm lại.
            try:
                os.mkdir(TEMP_DIR_NAME, _TEMP_DIR_MODE, dir_fd=root_fd)
            except FileExistsError:
                pass
            else:
                _fsync_dir(root_fd)  # M7: tên `.teleport` bền trước khi có gì bên trong
            fd = _open_dir_nofollow(TEMP_DIR_NAME, root_fd)
        except OSError as e:
            _raise_if_unsafe_open(e)
            raise
        try:
            st = os.fstat(fd)
            if not self._owner_ok(st):
                raise UnsafeReceiveDirError(".teleport thuộc người dùng khác")
            if create and stat.S_IMODE(st.st_mode) != _TEMP_DIR_MODE:
                _fchmod_tolerant(fd, _TEMP_DIR_MODE)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _windows_temp_root(self, *, create: bool) -> str | None:
        """Windows: kiểm (và tạo nếu `create`) `.teleport`; trả đường dẫn, chưa có mà không tạo → None."""
        path = os.path.join(self._root_str, TEMP_DIR_NAME)
        created = False
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(path)  # không truyền mode: xem _TEMP_DIR_MODE
                created = True
            except FileExistsError:
                pass
            st = os.lstat(path)
        if _is_link(st) or not stat.S_ISDIR(st.st_mode):
            raise UnsafeReceiveDirError(".teleport là liên kết hoặc không phải thư mục")
        if sys.platform == "win32" and created:
            try:
                _set_hidden(path, st.st_file_attributes)
            except Exception as e:
                log.debug("không đặt được thuộc tính ẩn cho .teleport: %s", type(e).__name__)
        return path

    def _open_temp_root_ref(self) -> _TempRoot | None:
        """Mở `.teleport` để liệt kê và mở các thư mục lượt; chưa có → None.

        Windows: giữ handle canh của `.teleport` (không lấy được → None: không làm gì, A1).
        """
        if sys.platform == "win32":
            path = self._windows_temp_root(create=False)
            if path is None:
                return None
            try:
                guard = _guard(path)
            except (OSError, StorageError) as e:
                log.debug("không giữ được handle canh của .teleport (%s)", type(e).__name__)
                return None
            return _TempRoot(self, path, None, guard)
        fd = self._open_temp_root(create=False)
        if fd is None:
            return None
        return _TempRoot(self, os.path.join(self._root_str, TEMP_DIR_NAME), fd, None)

    def _create_temp_posix(
        self, transfer_id: str, record: TransferRecord, data: bytes
    ) -> tuple[TransferTemp, int]:
        """POSIX: tạo `<transfer_id>` (H2, M7), ghi `transfer.json`; trả temp và fd của `.teleport`."""
        temp_root_fd = self._open_temp_root(create=True)
        assert temp_root_fd is not None
        try:
            self._make_temp_dir_posix(temp_root_fd, transfer_id)
            _fsync_dir(temp_root_fd)  # M7
            try:
                fd = _open_dir_nofollow(transfer_id, temp_root_fd)
                try:
                    if not self._owner_ok(os.fstat(fd)):
                        raise UnsafeReceiveDirError(
                            f"thư mục tạm của lượt {transfer_id} thuộc người dùng khác"
                        )
                except BaseException:
                    os.close(fd)
                    raise
            except BaseException:
                self._rmdir_quietly(transfer_id, temp_root_fd)
                raise
            temp = TransferTemp(_PRIVATE, self, transfer_id, self._temp_path(transfer_id), fd, record)
            self._write_first_record(temp, data, temp_root_fd)
        except BaseException:
            os.close(temp_root_fd)
            raise
        return temp, temp_root_fd

    def _make_temp_dir_posix(self, temp_root_fd: int, transfer_id: str) -> None:
        try:
            os.mkdir(transfer_id, _TEMP_DIR_MODE, dir_fd=temp_root_fd)
            return
        except FileExistsError:
            pass
        # H2: thư mục sót lại mà rỗng (hay chỉ có `.tmp` của bố cục) → xóa rồi tạo lại.
        try:
            fd = os.open(transfer_id, _DIR_NOFOLLOW_FLAGS, dir_fd=temp_root_fd)
        except OSError as e:
            raise TempExistsError(f"thư mục tạm của lượt {transfer_id} đã có") from e
        try:
            if not self._owner_ok(os.fstat(fd)):
                raise TempExistsError(f"thư mục tạm của lượt {transfer_id} đã có")
            self._clear_reusable(_DirRef("", fd), transfer_id)
        finally:
            os.close(fd)
        try:
            os.rmdir(transfer_id, dir_fd=temp_root_fd)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise TempExistsError(f"thư mục tạm của lượt {transfer_id} đã có") from e
        try:
            os.mkdir(transfer_id, _TEMP_DIR_MODE, dir_fd=temp_root_fd)
        except FileExistsError as e:
            raise TempExistsError(f"thư mục tạm của lượt {transfer_id} đã có") from e

    def _clear_reusable(self, ref: _DirRef, transfer_id: str) -> None:
        """H2: chỉ có file thường dạng `.tmp` của bố cục → xóa chúng; còn lại → `TempExistsError`."""
        names = ref.listdir()
        for entry in names:
            if _LAYOUT_TMP_RE.fullmatch(entry) is None:
                raise TempExistsError(f"thư mục tạm của lượt {transfer_id} đã có")
            try:
                st = ref.lstat(entry)
            except FileNotFoundError:
                continue
            if not _is_plain_file(st):
                raise TempExistsError(f"thư mục tạm của lượt {transfer_id} đã có")
        for entry in names:
            with contextlib.suppress(FileNotFoundError):
                ref.unlink(entry)

    def _create_temp_windows(self, transfer_id: str, record: TransferRecord, data: bytes) -> TransferTemp:
        """Windows: canh `.teleport`, tạo `<transfer_id>` (H2), canh nó, ghi `transfer.json`."""
        root_path = self._windows_temp_root(create=True)
        assert root_path is not None
        root_guard = _guard(root_path)
        try:
            path = str(self._temp_path(transfer_id))
            try:
                os.mkdir(path)  # không truyền mode: xem _TEMP_DIR_MODE
            except FileExistsError:
                self._reuse_windows(path, transfer_id)
            try:
                dir_guard = _guard(path)
            except BaseException:
                self._rmdir_quietly(transfer_id, None)
                raise
            try:
                dir_identity = _identity(os.fstat(dir_guard))
            except BaseException:
                _close_quietly(dir_guard)
                self._rmdir_quietly(transfer_id, None)
                raise
        except BaseException:
            _close_quietly(root_guard)
            raise
        temp = TransferTemp(
            _PRIVATE,
            self,
            transfer_id,
            Path(path),
            None,
            record,
            guards=(root_guard, dir_guard),
            dir_identity=dir_identity,
        )
        self._write_first_record(temp, data, None)
        return temp

    def _reuse_windows(self, path: str, transfer_id: str) -> None:
        """Windows, H2: dọn và tạo lại `<transfer_id>` có sẵn nếu dùng lại được."""
        try:
            guard = _guard(path)
        except (OSError, StorageError) as e:
            raise TempExistsError(f"thư mục tạm của lượt {transfer_id} đã có") from e
        try:
            self._clear_reusable(_DirRef(path, None), transfer_id)
        finally:
            _close_quietly(guard)
        try:
            _retry_on_permission(lambda: os.rmdir(path))
        except FileNotFoundError:
            pass
        except OSError as e:
            raise TempExistsError(f"thư mục tạm của lượt {transfer_id} đã có") from e
        try:
            os.mkdir(path)
        except FileExistsError as e:
            raise TempExistsError(f"thư mục tạm của lượt {transfer_id} đã có") from e

    def _write_first_record(self, temp: TransferTemp, data: bytes, temp_root_fd: int | None) -> None:
        """Ghi `transfer.json` đầu tiên; lỗi → dọn thư mục vừa tạo rồi lỗi bay ra."""
        try:
            temp._record_identity = _write_atomic_in(temp, TRANSFER_RECORD_NAME, [data])
        except BaseException:
            self._abandon_new_temp(temp, temp_root_fd, record_written=False)
            raise

    def _abandon_new_temp(
        self, temp: TransferTemp, temp_root_fd: int | None, *, record_written: bool = True
    ) -> None:
        """Dọn thư mục lượt vừa tạo (`transfer.json` của mình, rồi thư mục khi rỗng); không ném OSError."""
        if record_written:
            self._unlink_own(temp, TRANSFER_RECORD_NAME, temp._record_identity)
        temp._close_dir_handle()
        self._rmdir_quietly(temp.transfer_id, temp_root_fd)
        temp._close_quietly()

    def _rmdir_quietly(self, transfer_id: str, temp_root_fd: int | None) -> None:
        """`rmdir .teleport/<transfer_id>` (không đệ quy, không theo liên kết); lỗi chỉ log DEBUG."""
        try:
            if sys.platform == "win32":
                path = str(self._temp_path(transfer_id))
                if _is_link(os.lstat(path)):
                    return
                _retry_on_permission(lambda: os.rmdir(path))
            else:
                assert temp_root_fd is not None
                os.rmdir(transfer_id, dir_fd=temp_root_fd)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.debug("lượt %s: không xóa được thư mục tạm (%s)", transfer_id, type(e).__name__)

    def _temp_path(self, transfer_id: str) -> Path:
        return self._root / TEMP_DIR_NAME / transfer_id

    # --- finalize ---

    def _flush_part(self, part: PartFile, fd: int, mtime_ms: int | None) -> None:
        """F1: đặt `mtime`, `fsync` rồi đóng `.part` (Windows: đặt `mtime` sau khi đóng và kiểm lại).

        Từ đây `part` đã đóng, dù thành công hay lỗi.
        """
        if sys.platform == "win32":
            try:
                _full_fsync(fd)
            finally:
                part._close_fd()
            if not self._part_unchanged(part):
                raise StorageError(f"{part._label()}: .part đã bị thay, không đổi tên")
            if mtime_ms is not None:
                _set_mtime(part._path, mtime_ms, part)
            return
        try:
            if mtime_ms is not None:
                _set_mtime(fd, mtime_ms, part)
            _full_fsync(fd)
        finally:
            part._close_fd()

    def _list_root(self) -> list[str]:
        """F2: tên mọi mục của thư mục nhận (file, thư mục, link). Lỗi → OSError."""
        if sys.platform == "win32":
            return os.listdir(self._root_str)
        # fd mới cho mỗi lần liệt kê: fd nhân bản dùng chung vị trí đọc thư mục, hai thread liệt
        # kê trên cùng root_fd sẽ làm nhau bỏ sót mục.
        fd = os.open(".", _DIR_FLAGS, dir_fd=self._root_fd_or_raise())
        try:
            return os.listdir(fd)
        finally:
            os.close(fd)

    def _place(self, part: PartFile, name: str, taken: set[str]) -> str:
        """Bước 1–5: chọn tên, `TARGET`, giữ chỗ, kiểm lại, `RESERVED`, đổi tên. Trả tên cuối."""
        fallback = False
        n = 0
        tried = 0
        while True:
            tried += 1
            if tried > MAX_NAME_CANDIDATES:
                raise NameUnavailableError(f"{part._label()}: đã thử quá {MAX_NAME_CANDIDATES} tên")
            candidate = self._candidate(name, n, fallback=fallback)
            key = dedupe_key(candidate)
            if key in taken:
                n += 1
                continue
            try:
                intent = encode_target(candidate)
            except StateError:
                # Tên không qua được luật của `TARGET` (đổi đi khi làm sạch lại): bỏ ứng viên này.
                taken.add(key)
                n += 1
                continue
            part.log.append(intent)  # bước 1
            part.log.sync()
            _after_target(self._root / candidate)
            try:
                fd = self._open_in_root(candidate, _RESERVE_FLAGS)  # bước 2
            except FileExistsError:
                taken.add(key)
                n += 1
                continue
            except PermissionError:
                # Windows báo EACCES khi tên là thư mục hay file đang chờ xóa.
                if not self._exists_in_root(candidate):
                    raise
                taken.add(key)
                n += 1
                continue
            except OSError as e:
                if e.errno not in _NAME_REJECTED_ERRNOS:
                    raise
                if fallback:
                    raise NameUnavailableError(f"{part._label()}: hệ file từ chối cả tên dự phòng") from e
                log.info("%s: hệ file từ chối tên (%s), dùng tên dự phòng", part._label(), _errno_name(e))
                fallback = True
                n = 0
                continue
            if self._reserve_and_replace(part, candidate, fd):
                return candidate
            taken.add(key)  # F7: file giữ chỗ đã khác đi → để nguyên, chọn tên mới
            n += 1

    def _candidate(self, name: str, n: int, *, fallback: bool) -> str:
        """Ứng viên thứ `n`, cắt cho vừa giới hạn đường dẫn (architecture mục 4 “Thư mục nhận”)."""
        budget = self._limits.max_name_bytes
        if fallback:
            budget = min(budget, FALLBACK_MAX_BYTES)
        while budget >= 1:
            try:
                if fallback:
                    base = fallback_filename(name, budget)
                    candidate = base if n == 0 else numbered(base, n, max_bytes=budget)
                else:
                    candidate = fit_filename(name, budget) if n == 0 else numbered(name, n, max_bytes=budget)
            except ValueError as e:
                raise PathTooLongError("đường dẫn thư mục nhận quá dài: không còn chỗ cho tên file") from e
            excess = _units(os.path.join(self._root_str, candidate)) - self._limits.max_path
            if excess <= 0:
                return candidate
            budget -= max(1, excess)
        raise PathTooLongError("đường dẫn thư mục nhận quá dài: không còn chỗ cho tên file")

    def _reserve_and_replace(self, part: PartFile, candidate: str, fd: int) -> bool:
        """Bước 3–5 với file giữ chỗ vừa tạo (`fd`). False: file giữ chỗ đã khác đi, chọn tên mới.

        Lỗi khi chưa đổi tên xong → xóa file giữ chỗ nếu nó vẫn là của mình (sau khi đã quên
        `RESERVED` nếu nó có thể đã bền), rồi lỗi bay ra.
        """
        identity: tuple[int, int] | None = None
        known = False  # đã ghi nhận được danh tính (có thể là None khi hệ file không có st_ino)
        reserved = False  # `RESERVED` có thể đã nằm trong `.state`
        renamed = False
        target = self._root / candidate
        try:
            try:
                identity = _identity(os.fstat(fd))
                known = True
                if sys.platform == "win32":
                    os.fsync(fd)  # FlushFileBuffers trên handle của chính file giữ chỗ
            finally:
                os.close(fd)
            self._sync_root()  # bước 3 (POSIX)
            _after_reserve(target)
            if not self._reserve_intact(candidate, identity):  # F7
                return False
            intent = encode_reserved(candidate)
            part.log._make_room(RECORD_RESERVED)  # gộp (nếu cần) trước: lỗi ở đây chưa ghi `RESERVED`
            reserved = True
            part.log.append(intent)  # bước 4
            part.log.sync()
            _after_reserved(target)
            if not self._reserve_intact(candidate, identity):  # F7 lần hai, sau khi `RESERVED` bền
                self._forget_reserved(part, candidate)
                return False
            if not self._part_unchanged(part):  # F7b
                raise StorageError(f"{part._label()}: .part đã bị thay, không đổi tên")
            self._replace(part, candidate)  # bước 5
            renamed = True
            part._finalized = True
            return True
        except BaseException:
            if known and not renamed:
                self._discard_reserve(part, candidate, identity, reserved=reserved)
            raise

    def _reuse_reserved(self, part: PartFile) -> str | None:
        """`REUSE_PLACEHOLDER`, bước 5: đổi tên đè lên file giữ chỗ đã lưu; nó đã khác đi → None.

        Khác đi thì nối lại `TARGET` tên đó (quên `RESERVED`) trước khi bên gọi chọn tên mới.
        """
        candidate = part._target_name
        identity = part._target_identity
        assert candidate is not None
        renamed = False
        try:
            if not self._reserve_intact(candidate, identity):
                self._forget_reserved(part, candidate)
                return None
            if not self._part_unchanged(part):
                raise StorageError(f"{part._label()}: .part đã bị thay, không đổi tên")
            self._replace(part, candidate)
            renamed = True
            part._finalized = True
            return candidate
        except BaseException:
            if not renamed:
                self._discard_reserve(part, candidate, identity, reserved=True)
            raise

    @staticmethod
    def _forget_reserved(part: PartFile, candidate: str) -> None:
        """Nối lại `TARGET` cùng tên + `fsync`: đọc lại sẽ quên `RESERVED` trước đó."""
        part.log.append(encode_target(candidate))
        part.log.sync()

    def _reserve_intact(self, candidate: str, identity: tuple[int, int] | None) -> bool:
        """File giữ chỗ còn là file thường, 0 byte, cùng danh tính."""
        try:
            st = self._lstat_in_root(candidate)
        except FileNotFoundError:
            return False
        return stat.S_ISREG(st.st_mode) and st.st_size == 0 and _same_identity(identity, st)

    def _target_intact(self, part: PartFile) -> bool:
        """`VERIFY_TARGET`: file đích còn là file thường, đúng `size`, cùng danh tính lúc mở lại."""
        assert part._target_name is not None
        try:
            st = self._lstat_in_root(part._target_name)
        except FileNotFoundError:
            return False
        return _is_plain_file(st) and st.st_size == part.size and _same_identity(part._target_identity, st)

    def _discard_reserve(
        self, part: PartFile, candidate: str, identity: tuple[int, int] | None, *, reserved: bool
    ) -> None:
        """Dọn file giữ chỗ khi lỗi trước lúc đổi tên xong, chỉ khi nó vẫn là của mình. Không ném OSError.

        `reserved`: `RESERVED` có thể đã bền → phải quên nó (nối lại `TARGET`) trước khi xóa; không
        ghi được `.state` thì để nguyên file giữ chỗ.
        """
        try:
            if not self._reserve_intact(candidate, identity):
                return
            # Không có danh tính (st_ino = 0) thì không phân biệt được file giữ chỗ với file 0 byte
            # vừa nhận, khi lỗi bay ra sau lúc đổi tên thật mà trước khi kịp đặt cờ `renamed`
            # (ví dụ KeyboardInterrupt). `.part` đã rời thư mục tạm → coi như đã đổi tên, không xóa.
            # Không kiểm được (lỗi khác FileNotFoundError) → cũng không xóa.
            if identity is None and not self._part_present(part):
                return
            if reserved:
                try:
                    self._forget_reserved(part, candidate)
                except (OSError, StorageError, ValueError) as e:
                    log.debug(
                        "%s: không quên được RESERVED, để nguyên file giữ chỗ (%s)",
                        part._label(),
                        type(e).__name__,
                    )
                    return
            if sys.platform == "win32":
                path = os.path.join(self._root_str, candidate)
                _retry_on_permission(lambda: os.unlink(path))
            else:
                root_fd = self._root_fd_or_raise()
                _retry_on_permission(lambda: os.unlink(candidate, dir_fd=root_fd))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.debug("%s: không xóa được file giữ chỗ (%s)", part._label(), type(e).__name__)

    def _part_present(self, part: PartFile) -> bool:
        """`.part` còn trong thư mục tạm (mục bất kỳ). Lỗi khác FileNotFoundError → OSError/ValueError."""
        try:
            part._temp._ref().lstat(part._name)
        except FileNotFoundError:
            return False
        return True

    def _part_unchanged(self, part: PartFile) -> bool:
        """`.part` còn là file thường, đúng `size` byte, cùng danh tính; Windows: không là reparse point."""
        try:
            st = part._temp._ref().lstat(part._name)
        except FileNotFoundError:
            return False
        return (
            stat.S_ISREG(st.st_mode)
            and st.st_size == part.size
            and _same_identity(part._identity, st)
            and not _has_reparse_point(st)
        )

    def _replace(self, part: PartFile, candidate: str) -> None:
        """Bước 5: đổi tên `.part` đè lên file giữ chỗ; `PermissionError` → thử lại."""
        if sys.platform == "win32":
            src = str(part._path)
            dst = os.path.join(self._root_str, candidate)
            flags = MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
            _retry_on_permission(lambda: _move_file_ex(src, dst, flags))
            return
        temp_fd = part._temp._fd_or_raise()
        root_fd = self._root_fd_or_raise()
        _retry_on_permission(
            lambda: os.replace(part._name, candidate, src_dir_fd=temp_fd, dst_dir_fd=root_fd)
        )

    def _record_verified(self, part: PartFile) -> None:
        """Bước 7: `transfer.json` với `verified` = `file_index` + 1 (`done_at` khi là file cuối)."""
        temp = part._temp
        old = temp._record
        verified = part.file_index + 1
        done = verified == len(old.offer.files)
        new = TransferRecord(
            receiver=old.receiver,
            sender=old.sender,
            offer=old.offer,
            verified=verified,
            done_at=_utc_now() if done else None,
        )
        temp._record_identity = _write_atomic_in(temp, TRANSFER_RECORD_NAME, [encode_transfer_record(new)])
        temp._record = new

    def _drop_state(self, part: PartFile) -> None:
        """Bước 8: đóng nhật ký, xóa `<file_index>.state` nếu nó vẫn là của mình; lỗi chỉ log DEBUG."""
        part.log.close()
        self._unlink_own(part._temp, part.log._name, part.log._identity)

    def _unlink_own(self, temp: TransferTemp, name: str, identity: tuple[int, int] | None) -> None:
        """Xóa file thường `name` trong thư mục tạm nếu cùng danh tính; không ném OSError."""
        try:
            ref = temp._ref()
            temp._check_dir()
            st = ref.lstat(name)
            if not _is_plain_file(st) or not _same_identity(identity, st):
                return
            ref.unlink(name)
        except FileNotFoundError:
            pass
        except (OSError, StorageError, ValueError) as e:
            log.debug("lượt %s: không xóa được %s (%s)", temp.transfer_id, name, type(e).__name__)

    def _sync_root(self) -> None:
        """POSIX: `fsync` thư mục nhận (`_fsync_dir`, lỗi bỏ qua). Windows: không làm gì."""
        if sys.platform == "win32":
            return
        _fsync_dir(self._root_fd_or_raise())

    def _open_in_root(self, entry: str, flags: int) -> int:
        if sys.platform == "win32":
            return os.open(os.path.join(self._root_str, entry), flags, _FILE_MODE)
        return os.open(entry, flags, _FILE_MODE, dir_fd=self._root_fd_or_raise())

    def _lstat_in_root(self, entry: str) -> os.stat_result:
        if sys.platform == "win32":
            return os.lstat(os.path.join(self._root_str, entry))
        return os.lstat(entry, dir_fd=self._root_fd_or_raise())

    def _exists_in_root(self, entry: str) -> bool:
        try:
            self._lstat_in_root(entry)
        except OSError:
            return False
        return True

    def _open_target_readonly(self, part: PartFile) -> int | None:
        """`VERIFY_TARGET`: mở file đích chỉ đọc (`O_NOFOLLOW`); không còn là file đã nhớ → None."""
        assert part._target_name is not None
        try:
            if sys.platform == "win32":
                if not _is_plain_file(self._lstat_in_root(part._target_name)):
                    return None
            fd = self._open_in_root(part._target_name, _READ_FLAGS)
        except OSError as e:
            log.debug("%s: không mở được file đích (%s)", part._label(), type(e).__name__)
            return None
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_size != part.size
                or not _same_identity(part._target_identity, st)
            ):
                os.close(fd)
                return None
        except BaseException:
            os.close(fd)
            raise
        return fd

    # --- remove_temp, cleanup ---

    def _remove_part(self, temp: TransferTemp, part: PartFile) -> None:
        """Xóa `.part` của mình nếu nó còn là file thường cùng danh tính lúc tạo. Không raise OSError."""
        try:
            ref = temp._ref()
            st = ref.lstat(part._name)
            if _has_reparse_point(st):
                return
            if not stat.S_ISREG(st.st_mode) or not _same_identity(part._identity, st):
                return
            ref.unlink(part._name)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.debug("%s: không xóa được .part (%s)", part._label(), type(e).__name__)

    @staticmethod
    def _remove_layout(ref: _DirRef, *, keep_record: bool, include_part: bool) -> None:
        """Xóa file thường khớp bố cục trong `ref` (`.part` chỉ khi `include_part`). Không raise OSError."""
        try:
            names = ref.listdir()
        except OSError as e:
            log.debug("không liệt kê được thư mục tạm (%s)", type(e).__name__)
            return
        for entry in names:
            if _LAYOUT_RE.fullmatch(entry) is None:
                continue
            if entry.endswith(".part") and not include_part:
                continue
            if keep_record and entry == TRANSFER_RECORD_NAME:
                continue
            try:
                if not _is_plain_file(ref.lstat(entry)):
                    continue
                ref.unlink(entry)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.debug("không xóa được file trong thư mục tạm (%s)", type(e).__name__)

    def _remove_temp_dir(self, temp: TransferTemp) -> bool:
        """`rmdir <transfer_id>` (không đệ quy); trả True khi thư mục không còn. Không raise OSError."""
        try:
            if sys.platform == "win32":
                path = str(temp._path)
                st = os.lstat(path)
                if _is_link(st) or not (st.st_ino == 0 or _same_identity(temp._dir_identity, st)):
                    return False
                _retry_on_permission(lambda: os.rmdir(path))
                return True
            temp_root_fd = self._open_temp_root(create=False)
            if temp_root_fd is None:
                return True
            try:
                _retry_on_permission(lambda: os.rmdir(temp.transfer_id, dir_fd=temp_root_fd))
            finally:
                os.close(temp_root_fd)
            return True
        except FileNotFoundError:
            return True
        except (OSError, StorageError, ValueError) as e:
            # Thư mục còn file lạ (không rỗng), liên kết, hay lỗi khác: để nguyên.
            log.debug("lượt %s: không xóa được thư mục tạm (%s)", temp.transfer_id, type(e).__name__)
            return False

    def _temp_dir_gone(self, temp: TransferTemp) -> bool:
        """`<transfer_id>` đã không còn. Không raise OSError; không xác định được → False."""
        try:
            if sys.platform == "win32":
                os.lstat(temp._path)
                return False
            temp_root_fd = self._open_temp_root(create=False)
            if temp_root_fd is None:
                return True
            try:
                os.lstat(temp.transfer_id, dir_fd=temp_root_fd)
            finally:
                os.close(temp_root_fd)
            return False
        except FileNotFoundError:
            return True
        except (OSError, StorageError) as e:
            log.debug("lượt %s: không kiểm được thư mục tạm (%s)", temp.transfer_id, type(e).__name__)
            return False

    def _cleanup_one(
        self, root: _TempRoot, transfer_id: str, wall_now: float, own: str, retention: float
    ) -> bool:
        """Dọn một thư mục lượt nếu đã quá ngưỡng; True khi đã xóa hết nó."""
        sub = root.open_dir(transfer_id)
        if sub is None:
            return False
        try:
            scan = self._inspect(sub, transfer_id, own, None, wall_now)
            record = scan.record
            if record is not None and record.receiver == own:
                if record.done_at is not None:
                    threshold = float(core_transfer.REMEMBER_TTL)
                else:
                    threshold = float(core_transfer.RECONNECT_GRACE)
            else:
                threshold = retention
            last = scan.last_activity
            if last is None or last > wall_now or wall_now - last < threshold:
                return False
            self._remove_layout(sub.ref, keep_record=False, include_part=True)
        finally:
            sub.close()  # Windows: đóng handle canh của `<transfer_id>` trước `rmdir`
        return root.rmdir(transfer_id)

    def _discard_one(self, root: _TempRoot, transfer_id: str, own: str) -> bool:
        """`discard_temp` cho một thư mục đã giữ trong `_busy`; True khi đã xóa hết nó."""
        sub = root.open_dir(transfer_id)
        if sub is None:
            return False
        try:
            record, broken = self._read_record(sub.ref, transfer_id)
            if broken or record is None or record.receiver != own or record.done_at is not None:
                return False
            self._remove_layout(sub.ref, keep_record=False, include_part=True)
        finally:
            sub.close()  # Windows: đóng handle canh của `<transfer_id>` trước `rmdir`
        return root.rmdir(transfer_id)

    # --- đọc thư mục lượt ---

    def _read_record(self, ref: _DirRef, transfer_id: str) -> tuple[TransferRecord | None, bool]:
        """Đọc `transfer.json`; trả (bản ghi, hỏng). Không có → (None, False). Không raise OSError."""
        try:
            st = ref.lstat(TRANSFER_RECORD_NAME)
        except FileNotFoundError:
            return None, False
        except OSError:
            return None, True
        if not _is_plain_file(st):
            return None, True
        try:
            fd = ref.open(TRANSFER_RECORD_NAME, _READ_FLAGS)
        except OSError:
            return None, True
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode) or not _same_identity(_identity(st), fst):
                return None, True
            data = _read_limited(fd, MAX_TRANSFER_RECORD + 1)
        except OSError:
            return None, True
        finally:
            _close_quietly(fd)
        if len(data) > MAX_TRANSFER_RECORD:
            return None, True
        try:
            return decode_transfer_record(data, dir_name=transfer_id), False
        except StateError:
            return None, True

    def _inspect(
        self,
        sub: _TempDir,
        transfer_id: str,
        own: str,
        trusted: Collection[str] | None,
        wall_now: float,
    ) -> TempScan:
        """Xếp loại một thư mục lượt và tính hoạt động cuối (`trusted` None: không xét máy gửi)."""
        record, broken = self._read_record(sub.ref, transfer_id)
        if broken:
            kind = TempKind.BROKEN
        elif record is None:
            kind = TempKind.ORPHAN
        elif record.receiver != own:
            kind = TempKind.OTHER_NODE
        elif trusted is not None and record.sender not in trusted:
            kind = TempKind.ORPHAN
        elif record.done_at is not None:
            kind = TempKind.DONE
        else:
            kind = TempKind.RUNNING
        running = record if record is not None and record.receiver == own and record.done_at is None else None
        return TempScan(transfer_id, kind, record, self._last_activity(sub, running, wall_now))

    @staticmethod
    def _last_activity(sub: _TempDir, running: TransferRecord | None, wall_now: float) -> float | None:
        """mtime lớn nhất của file thường khớp bố cục (lượt đang chạy: chỉ `transfer.json`, `.state`
        và `.part` của file hiện tại); không có file nào → mtime thư mục. Bỏ mtime ở tương lai của
        `.part` và `.tmp` (mtime của `.part` có thể là `mtime` từ `offer`). Lỗi → None.
        """
        allowed: set[str] | None = None
        if running is not None:
            current = running.verified
            allowed = {TRANSFER_RECORD_NAME, f"{current}.state", f"{current}.part"}
        best: float | None = None
        try:
            for entry in sub.ref.listdir():
                if _LAYOUT_RE.fullmatch(entry) is None or (allowed is not None and entry not in allowed):
                    continue
                try:
                    st = sub.ref.lstat(entry)
                except FileNotFoundError:
                    continue
                if not _is_plain_file(st):
                    continue
                mtime = st.st_mtime
                if mtime > wall_now and entry.endswith((".part", ".tmp")):
                    continue
                best = mtime if best is None else max(best, mtime)
            if best is None:
                best = sub.dir_mtime()
        except OSError as e:
            log.debug("không đọc được hoạt động cuối của thư mục tạm (%s)", type(e).__name__)
            return None
        return best

    # --- open_for_resume ---

    def _open_existing_temp(self, scan: TempScan) -> TransferTemp:
        """Mở thư mục lượt có sẵn (không theo liên kết; Windows: handle canh); lỗi → `NotResumableError`."""
        transfer_id = scan.transfer_id
        assert scan.record is not None
        if sys.platform == "win32":
            try:
                root_path = self._windows_temp_root(create=False)
                if root_path is None:
                    raise _refused(transfer_id)
                root_guard = _guard(root_path)
            except NotResumableError:
                raise
            except (OSError, StorageError) as e:
                raise _refused(transfer_id) from e
            try:
                path = str(self._temp_path(transfer_id))
                st = os.lstat(path)
                if _is_link(st) or not stat.S_ISDIR(st.st_mode):
                    raise _refused(transfer_id)
                dir_guard = _guard(path)
                try:
                    dir_identity = _identity(os.fstat(dir_guard))
                except BaseException:
                    _close_quietly(dir_guard)
                    raise
            except NotResumableError:
                _close_quietly(root_guard)
                raise
            except (OSError, StorageError) as e:
                _close_quietly(root_guard)
                raise _refused(transfer_id) from e
            except BaseException:
                _close_quietly(root_guard)
                raise
            return TransferTemp(
                _PRIVATE,
                self,
                transfer_id,
                Path(path),
                None,
                scan.record,
                guards=(root_guard, dir_guard),
                dir_identity=dir_identity,
            )
        try:
            temp_root_fd = self._open_temp_root(create=False)
        except (OSError, StorageError) as e:
            raise _refused(transfer_id) from e
        if temp_root_fd is None:
            raise _refused(transfer_id)
        try:
            fd = os.open(transfer_id, _DIR_NOFOLLOW_FLAGS, dir_fd=temp_root_fd)
        except OSError as e:
            raise _refused(transfer_id) from e
        finally:
            os.close(temp_root_fd)
        try:
            if not self._owner_ok(os.fstat(fd)):
                raise _refused(transfer_id)
        except BaseException:
            os.close(fd)
            raise
        return TransferTemp(_PRIVATE, self, transfer_id, self._temp_path(transfer_id), fd, scan.record)

    def _open_for_resume(self, scan: TempScan, stop: threading.Event | None) -> ResumeHandle:
        temp = self._open_existing_temp(scan)
        try:
            handle = self._resume_in(temp, scan, stop)
        except BaseException:
            temp._close_quietly()
            raise
        with self._lock:
            closed = self._closed
            if not closed:
                self._temps.add(temp)
        if closed:
            temp._close_quietly()
            raise ValueError("ReceiveDir đã đóng")
        return handle

    def _resume_in(self, temp: TransferTemp, scan: TempScan, stop: threading.Event | None) -> ResumeHandle:
        transfer_id = temp.transfer_id
        label = f"lượt {transfer_id}"
        ref = temp._ref()
        # ---- Pha 1: chỉ đọc ----
        record, broken = self._read_record(ref, transfer_id)
        if broken or record is None or record != scan.record or record.done_at is not None:
            raise _refused(transfer_id)
        index = record.verified
        files = record.offer.files
        if index >= len(files):
            raise _refused(transfer_id)
        size = files[index].size
        n = block_count(size)
        state_name, part_name = f"{index}.state", f"{index}.part"
        _check_stop(stop, label)
        state_st = self._lstat_member(ref, state_name, transfer_id)
        part_st = self._lstat_member(ref, part_name, transfer_id)
        replay = None
        state_identity: tuple[int, int] | None = None
        state_size = 0
        if state_st is not None:
            loaded = self._replay_state(ref, state_name, state_st, transfer_id, index, size, n, stop)
            if loaded is not None:
                replay, state_identity, state_size = loaded
        part_exists = part_st is not None
        part_size = part_st.st_size if part_st is not None else 0
        part_blocks = n if part_exists and part_size >= size else part_size // BLOCK_SIZE
        target = replay.target if replay is not None else None
        reserved = replay.reserved if replay is not None else False
        target_size: int | None = None
        target_identity: tuple[int, int] | None = None
        if target is not None:
            try:
                st = self._lstat_in_root(target)
            except FileNotFoundError:
                pass
            except OSError as e:
                raise _refused(transfer_id) from e
            else:
                if _is_plain_file(st):
                    target_size, target_identity = st.st_size, _identity(st)
        recovery = recover_target(
            target=target, reserved=reserved, target_size=target_size, part_exists=part_exists, size=size
        )
        bits = replay.bits if replay is not None else Bitfield(n)
        leaves = replay.leaves if replay is not None else bytearray(DIGEST_SIZE * n)
        if recovery is TargetRecovery.VERIFY_TARGET and not bits.all_set():
            recovery = TargetRecovery.RESTART
        _check_stop(stop, label)
        # ---- Pha 2: sửa đĩa ----
        temp._check_dir()
        self._remove_stale(ref, index)
        keep_part = part_exists and recovery in (TargetRecovery.NEW_NAME, TargetRecovery.REUSE_PLACEHOLDER)
        restart = recovery is not TargetRecovery.VERIFY_TARGET and not keep_part
        if restart:
            bits, leaves, part_blocks = Bitfield(n), bytearray(DIGEST_SIZE * n), 0
        cleared = _clear_bits_from(bits, leaves, part_blocks) if keep_part else []
        if recovery is TargetRecovery.VERIFY_TARGET:
            part_blocks = n
        mode = ResumeMode.NORMAL
        if recovery in (TargetRecovery.REUSE_PLACEHOLDER, TargetRecovery.RESTART_REUSE):
            mode = ResumeMode.REUSE_PLACEHOLDER
        elif recovery is TargetRecovery.VERIFY_TARGET:
            mode = ResumeMode.VERIFY_TARGET
        part_fd: int | None = None
        part_identity: tuple[int, int] | None = None
        end = 0
        log_: ResumeLog | None = None
        try:
            if keep_part:
                assert part_st is not None
                part_fd = ref.open(part_name, _PART_REOPEN_FLAGS)
                fst = os.fstat(part_fd)
                if not stat.S_ISREG(fst.st_mode) or not _same_identity(_identity(part_st), fst):
                    raise NotResumableError(f"{label}: .part đã bị thay")
                part_identity, end = _identity(fst), fst.st_size
            if replay is None or restart:
                # Header (kèm `TARGET` + `RESERVED` khi `RESTART_REUSE`) bền trước, `.part` mới tạo
                # sau: sập ở giữa thì lần sau vẫn thiếu `.part` và rơi lại đúng dòng này của bảng,
                # không bao giờ thành “còn `.part`” cạnh `TARGET` + `RESERVED` cũ.
                reuse = target if recovery is TargetRecovery.RESTART_REUSE else None
                log_ = ResumeLog._create(temp, index, size, n, target=reuse)
            else:
                log_ = ResumeLog._reopen(
                    temp,
                    index,
                    size,
                    n,
                    state_identity,
                    good_end=replay.good_end,
                    truncate=state_size > replay.good_end,
                    name_records=replay.name_records,
                    marked=bits.count(),
                    target=target,
                    reserved=target if reserved else None,
                )
                # `NEW_NAME` mà còn `RESERVED`: file mang tên đích không còn là file giữ chỗ của
                # mình, nên quên `RESERVED` bền ngay (bản gọn không có nó) trước khi trả handle; không
                # thì sau một lần sập nữa, file đó bị ai cắt về 0 byte sẽ bị coi là file giữ chỗ.
                forget = recovery is TargetRecovery.NEW_NAME and reserved
                pending_reserved = target if reserved and not forget else None
                if (
                    forget
                    or log_.name_records >= MAX_NAME_RECORDS - 2
                    or needs_compaction(
                        log_.length, compact_length_for(bits.count(), target, pending_reserved)
                    )
                ):
                    log_._write_compact(bits, leaves, target, pending_reserved)
                elif cleared:
                    for start in range(0, len(cleared), MAX_RECORD_BLOCKS):
                        log_.append(encode_clear(cleared[start : start + MAX_RECORD_BLOCKS]))
                    log_.sync()
            if restart:
                part_fd = ref.open(part_name, _PART_FLAGS)
                part_identity = _identity(os.fstat(part_fd))
        except BaseException:
            if log_ is not None:
                log_._close_quietly()
            if part_fd is not None:
                _close_quietly(part_fd)
            raise
        part = PartFile(
            _PRIVATE,
            temp,
            index,
            size,
            n,
            part_fd,
            part_identity,
            log_,
            mode=mode,
            end=end,
            own_part=part_fd is not None,
            target_name=target if mode is not ResumeMode.NORMAL else None,
            target_identity=target_identity if mode is not ResumeMode.NORMAL else None,
        )
        temp._parts[index] = part
        last = scan.last_activity if scan.last_activity is not None else time.time()
        return ResumeHandle(temp, record, index, part, bits, leaves, part_blocks, mode, last)

    @staticmethod
    def _lstat_member(ref: _DirRef, name: str, transfer_id: str) -> os.stat_result | None:
        """`lstat` `name` trong thư mục lượt: không có → None; liên kết, không phải file thường → từ chối."""
        try:
            st = ref.lstat(name)
        except FileNotFoundError:
            return None
        except OSError as e:
            raise _refused(transfer_id) from e
        if not _is_plain_file(st):
            raise _refused(transfer_id)
        return st

    @staticmethod
    def _replay_state(
        ref: _DirRef,
        name: str,
        st: os.stat_result,
        transfer_id: str,
        index: int,
        size: int,
        n: int,
        stop: threading.Event | None,
    ) -> tuple[ReplayResult, tuple[int, int] | None, int] | None:
        """Đọc lại `.state` theo đoạn trong trần; hỏng từ header hay header không khớp → None."""
        try:
            fd = ref.open(name, _READ_FLAGS)
        except OSError as e:
            raise _refused(transfer_id) from e
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode) or not _same_identity(_identity(st), fst):
                raise _refused(transfer_id)
            replay = StateReplay(n, max_length=max_state_length(n))
            head = bytearray()
            try:
                while True:
                    _check_stop(stop, f"lượt {transfer_id}, file {index}")
                    chunk = os.read(fd, max(1, STATE_READ_CHUNK))
                    if not chunk:
                        break
                    if len(head) < HEADER_SIZE:
                        head += chunk[: HEADER_SIZE - len(head)]
                    if not replay.feed(chunk):
                        break
                result = replay.finish()
            except StateError:
                return None
            if not check_header(
                head, transfer_id=transfer_id, file_index=index, size=size, block_size=BLOCK_SIZE
            ):
                return None
            return result, _identity(fst), fst.st_size
        except OSError as e:
            raise _refused(transfer_id) from e
        finally:
            _close_quietly(fd)

    @staticmethod
    def _remove_stale(ref: _DirRef, index: int) -> None:
        """Pha 2: xóa `.tmp` bố cục sót lại và `<j>.state` của file đã `verified` (j < `index`)."""
        try:
            names = ref.listdir()
        except OSError as e:
            log.debug("không liệt kê được thư mục tạm (%s)", type(e).__name__)
            return
        for entry in names:
            stale = _LAYOUT_TMP_RE.fullmatch(entry) is not None
            match = _STATE_NAME_RE.fullmatch(entry)
            if match is not None and int(match.group(1)) < index:
                stale = True
            if not stale:
                continue
            try:
                if _is_plain_file(ref.lstat(entry)):
                    ref.unlink(entry)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.debug("không xóa được file sót trong thư mục tạm (%s)", type(e).__name__)


class _TempDir:
    """Thư mục lượt mở tạm cho `scan_temps` / `cleanup` (POSIX fd, Windows handle canh)."""

    def __init__(self, path: str, fd: int | None, guard: int | None) -> None:
        self.ref = _DirRef(path, fd)
        self._fd = fd
        self._guard = guard

    def dir_mtime(self) -> float:
        handle = self._fd if self._fd is not None else self._guard
        assert handle is not None
        return os.fstat(handle).st_mtime

    def close(self) -> None:
        for fd in (self._fd, self._guard):
            if fd is not None:
                _close_quietly(fd)
        self._fd = self._guard = None


class _TempRoot:
    """`.teleport` mở để liệt kê và mở các thư mục lượt (POSIX fd, Windows handle canh)."""

    def __init__(self, owner: ReceiveDir, path: str, fd: int | None, guard: int | None) -> None:
        self._owner = owner
        self._path = path
        self._fd = fd
        self._guard = guard

    def names(self) -> list[str]:
        return _DirRef(self._path, self._fd).listdir()

    def open_dir(self, transfer_id: str) -> _TempDir | None:
        """Mở thư mục lượt nếu là thư mục thật (POSIX: đúng chủ; Windows: giữ được handle canh)."""
        path = os.path.join(self._path, transfer_id)
        try:
            if sys.platform == "win32":
                st = os.lstat(path)
                if _is_link(st) or not stat.S_ISDIR(st.st_mode):
                    return None
                return _TempDir(path, None, _guard(path))
            assert self._fd is not None
            st = os.lstat(transfer_id, dir_fd=self._fd)
            if not stat.S_ISDIR(st.st_mode):
                return None
            fd = os.open(transfer_id, _DIR_NOFOLLOW_FLAGS, dir_fd=self._fd)
            try:
                if not self._owner._owner_ok(os.fstat(fd)):
                    os.close(fd)
                    return None
            except BaseException:
                os.close(fd)
                raise
            return _TempDir(path, fd, None)
        except (OSError, StorageError) as e:
            log.debug("lượt %s: bỏ qua thư mục tạm (%s)", transfer_id, type(e).__name__)
            return None

    def rmdir(self, transfer_id: str) -> bool:
        """`rmdir` thư mục lượt (không theo liên kết); True khi chính lần này xóa được."""
        try:
            if sys.platform == "win32":
                path = os.path.join(self._path, transfer_id)
                if _is_link(os.lstat(path)):
                    return False
                _retry_on_permission(lambda: os.rmdir(path))
            else:
                assert self._fd is not None
                os.rmdir(transfer_id, dir_fd=self._fd)
            return True
        except OSError as e:
            log.debug("lượt %s: không xóa được thư mục tạm (%s)", transfer_id, type(e).__name__)
            return False

    def close(self) -> None:
        for fd in (self._fd, self._guard):
            if fd is not None:
                _close_quietly(fd)
        self._fd = self._guard = None


class TransferTemp:
    """Thư mục tạm `<thư mục nhận>/.teleport/<transfer_id>/` của một lượt nhận.

    Chỉ `ReceiveDir.create_temp` hay `open_for_resume` tạo. POSIX: giữ fd của thư mục tới
    `remove_temp`, `close_temp` hay `ReceiveDir.close`, mọi thao tác bên trong đi qua fd này.
    Windows: giữ handle canh của `.teleport` và của chính thư mục suốt thời gian đó.
    """

    def __init__(
        self,
        key: object,
        owner: ReceiveDir,
        transfer_id: str,
        path: Path,
        fd: int | None,
        record: TransferRecord,
        *,
        guards: tuple[int, int] | None = None,
        dir_identity: tuple[int, int] | None = None,
    ) -> None:
        _check_key(key)
        self._owner = owner
        self._transfer_id = transfer_id
        self._path = path
        self._fd = fd
        self._record = record
        self._record_identity: tuple[int, int] | None = None
        # Windows: (handle canh `.teleport`, handle canh `<transfer_id>`), fd do `_hold_dir` trả.
        self._root_guard = guards[0] if guards is not None else None
        self._dir_guard = guards[1] if guards is not None else None
        self._dir_identity = dir_identity
        self._parts: dict[int, PartFile] = {}  # mọi .part đã tạo, kể cả đã finalize
        self._closed = False
        self._removed = False

    @property
    def transfer_id(self) -> str:
        return self._transfer_id

    @property
    def path(self) -> Path:
        """`<thư mục nhận>/.teleport/<transfer_id>`."""
        return self._path

    @property
    def record(self) -> TransferRecord:
        """Nội dung hiện tại của `transfer.json` (`finalize` cập nhật sau bước 7)."""
        return self._record

    @property
    def closed(self) -> bool:
        """True sau `remove_temp`, `close_temp` hoặc `ReceiveDir.close`."""
        return self._closed

    def create_part(self, file_index: int, size: int) -> PartFile:
        """Tạo `<file_index>.part` rỗng bằng `O_EXCL` (quyền `0o666`, umask áp) và header `.state`.

        Header ghi bằng `_write_atomic_in` (POSIX: `fsync` thư mục tạm sau đó, làm bền cả hai tên);
        `.state` được mở để nối thêm (`PartFile.log`). Temp đã đóng, `file_index` không phải số
        nguyên 0…`MAX_OFFER_FILES` − 1, ngoài số file của `record`, hay đã dùng trong temp này,
        `size` sai (`core.blocks.block_count`) hay khác `size` trong `record` → ValueError. File đã
        có, kể cả symlink cài sẵn → FileExistsError, không đụng đích của link. Lỗi ghi header →
        OSError (đã xóa `.part` vừa tạo).
        """
        if self._closed:
            raise ValueError("TransferTemp đã đóng")
        if type(file_index) is not int or not 0 <= file_index < MAX_OFFER_FILES:
            raise ValueError(f"file_index phải là số nguyên 0…{MAX_OFFER_FILES - 1}")
        count = block_count(size)
        files = self._record.offer.files
        if file_index >= len(files) or size != files[file_index].size:
            raise ValueError(f"file {file_index} không có trong transfer.json hay khác kích thước")
        if file_index in self._parts:
            raise ValueError(f"file_index {file_index} đã dùng trong lượt này")
        name = f"{file_index}.part"
        ref = self._ref()
        self._check_dir()
        fd = ref.open(name, _PART_FLAGS)
        identity: tuple[int, int] | None = None
        known = False
        try:
            identity = _identity(os.fstat(fd))
            known = True
            resume_log = ResumeLog._create(self, file_index, size, count)
        except BaseException:
            _close_quietly(fd)
            if known:
                self._owner._unlink_own(self, name, identity)
            raise
        part = PartFile(_PRIVATE, self, file_index, size, count, fd, identity, resume_log)
        self._parts[file_index] = part
        return part

    def _ref(self) -> _DirRef:
        if sys.platform != "win32" and self._fd is None:
            raise ValueError("TransferTemp đã đóng")
        return _DirRef(str(self._path), self._fd)

    def _check_dir(self) -> None:
        """Windows: còn giữ handle canh, và `<transfer_id>` vẫn là thư mục cũ, không phải liên kết.

        Sai → `UnsafeReceiveDirError` (không có handle canh thì không xóa, cắt hay thay file theo
        tên, A1). POSIX: không làm gì (mọi thao tác đi qua fd của thư mục).
        """
        if sys.platform != "win32":
            return
        if self._dir_guard is None or self._root_guard is None:
            raise UnsafeReceiveDirError(f"lượt {self._transfer_id}: không còn handle canh của thư mục tạm")
        st = os.lstat(self._path)
        # `st_ino` = 0 (hệ file không có danh tính): chỉ dựa vào handle canh.
        same = st.st_ino == 0 or _same_identity(self._dir_identity, st)
        if _is_link(st) or not stat.S_ISDIR(st.st_mode) or not same:
            raise UnsafeReceiveDirError(f"lượt {self._transfer_id}: thư mục tạm đã bị thay")

    def _fd_or_raise(self) -> int:
        fd = self._fd
        if fd is None:
            raise ValueError("TransferTemp đã đóng")
        return fd

    def _close_dir_handle(self) -> None:
        """Đóng fd (POSIX) hay handle canh (Windows) của chính thư mục; giữ handle canh `.teleport`."""
        fd, self._fd = self._fd, None
        if fd is not None:
            _close_quietly(fd)
        guard, self._dir_guard = self._dir_guard, None
        if guard is not None:
            _close_quietly(guard)

    def _close_fd_quietly(self) -> None:
        self._close_dir_handle()
        guard, self._root_guard = self._root_guard, None
        if guard is not None:
            _close_quietly(guard)

    def _close_quietly(self) -> None:
        """Đóng mọi `PartFile` còn mở, nhật ký và fd/handle canh của thư mục; không xóa gì."""
        for part in self._parts.values():
            part._close_quietly()
        self._close_fd_quietly()
        self._closed = True


class ResumeLog:
    """Nhật ký `<file_index>.state` của một file (architecture mục 4), mở để nối thêm. Lấy qua `PartFile.log`.

    - `append(record)`: nối một bản ghi (đã mã hóa bằng `core.resume_state`) vào cuối file. Trước
      khi nối `TARGET`/`RESERVED` mà số bản ghi tên ≥ `MAX_NAME_RECORDS` − 2 thì gộp trước. Sau
      mỗi bản ghi, nếu độ dài > 2 × cận trên độ dài bản gọn + 64 KiB (`needs_compaction`) thì gộp.
      Cận trên tính từ số khối đã đánh dấu (bit lúc mở + khối trong các `MARK` đã nối, ≤ số khối)
      và tên `TARGET`/`RESERVED` cuối (`compact_length_for`); nó ≤ cỡ gọn lớn nhất, nên nhật ký
      không bao giờ vượt `max_state_length`.
    - `sync()`: `fsync` `.state`.
    - `rewrite_header(target=None)`: thay cả file bằng header (kèm `TARGET` + `RESERVED` của
      `target` nếu có) trong một lần ghi nguyên tử.
    - `compact()`: đọc lại chính `.state` theo đoạn `STATE_READ_CHUNK` trong trần rồi ghi bản gọn
      nguyên tử (`_write_atomic_in`, theo đoạn); RAM tạm 32 byte mỗi khối chỉ trong lúc gộp.

    Ghi thiếu thì ghi tiếp tới đủ; `OSError` khi ghi hay `fsync` làm nhật ký hỏng: mọi `append`,
    `sync`, `compact` sau → `StorageError`, không bao giờ nối sau một bản ghi có thể đã cụt (lần
    mở lại đọc lại và cắt). Khi thay file (gộp, ghi lại header) fd nối thêm được đóng trước và mở
    lại sau, so danh tính với file vừa ghi. Đã đóng → ValueError.
    """

    def __init__(
        self,
        key: object,
        temp: TransferTemp,
        file_index: int,
        size: int,
        count: int,
        fd: int,
        identity: tuple[int, int] | None,
        *,
        length: int,
        name_records: int,
        marked: int,
        target: str | None,
        reserved: str | None,
    ) -> None:
        _check_key(key)
        self._temp = temp
        self._file_index = file_index
        self._size = size
        self._count = count
        self._name = f"{file_index}.state"
        self._fd: int | None = fd
        self._identity = identity
        self._length = length
        self._name_records = name_records
        self._marked = marked  # cận trên số khối có bit bật (đếm cả khối đánh dấu lại)
        self._target = target  # tên `TARGET` cuối
        self._reserved = reserved  # tên `RESERVED` trùng `TARGET` cuối (None nếu chưa giữ chỗ)
        self._broken = False
        self._closed = False

    @classmethod
    def _create(
        cls, temp: TransferTemp, file_index: int, size: int, count: int, *, target: str | None = None
    ) -> ResumeLog:
        """Ghi header (kèm `TARGET` + `RESERVED` nếu `target`) bằng `_write_atomic_in`, mở để nối."""
        chunks = cls._header_chunks(temp.transfer_id, file_index, size, target)
        identity = _write_atomic_in(temp, f"{file_index}.state", chunks)
        fd = cls._open_append(temp, f"{file_index}.state", identity)
        return cls(
            _PRIVATE,
            temp,
            file_index,
            size,
            count,
            fd,
            identity,
            length=sum(len(c) for c in chunks),
            name_records=2 if target is not None else 0,
            marked=0,
            target=target,
            reserved=target,
        )

    @classmethod
    def _reopen(
        cls,
        temp: TransferTemp,
        file_index: int,
        size: int,
        count: int,
        identity: tuple[int, int] | None,
        *,
        good_end: int,
        truncate: bool,
        name_records: int,
        marked: int,
        target: str | None,
        reserved: str | None,
    ) -> ResumeLog:
        """Mở `.state` đã đọc lại để nối; `truncate` → cắt về `good_end` rồi `fsync` trước khi nối."""
        name = f"{file_index}.state"
        fd = cls._open_append(temp, name, identity)
        try:
            if truncate:
                os.ftruncate(fd, good_end)
                _full_fsync(fd)
        except BaseException:
            _close_quietly(fd)
            raise
        return cls(
            _PRIVATE,
            temp,
            file_index,
            size,
            count,
            fd,
            identity,
            length=good_end,
            name_records=name_records,
            marked=marked,
            target=target,
            reserved=reserved,
        )

    @staticmethod
    def _header_chunks(transfer_id: str, file_index: int, size: int, target: str | None) -> list[bytes]:
        chunks = [encode_header(transfer_id, file_index, size)]
        if target is not None:
            chunks += [encode_target(target), encode_reserved(target)]
        return chunks

    @staticmethod
    def _open_append(temp: TransferTemp, name: str, identity: tuple[int, int] | None) -> int:
        """Mở `name` để nối (`O_APPEND | O_NOFOLLOW`); khác danh tính file vừa ghi → `StorageError`."""
        temp._check_dir()
        fd = temp._ref().open(name, _APPEND_FLAGS, _STATE_FILE_MODE)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or not _same_identity(identity, st):
                raise StorageError(f"lượt {temp.transfer_id}: .state đã bị thay")
        except BaseException:
            _close_quietly(fd)
            raise
        return fd

    @property
    def length(self) -> int:
        """Độ dài hiện tại của `.state`, byte (kể cả header)."""
        return self._length

    @property
    def name_records(self) -> int:
        """Số bản ghi `TARGET` + `RESERVED` trong `.state` hiện tại."""
        return self._name_records

    @property
    def closed(self) -> bool:
        return self._closed

    def append(self, record: bytes) -> None:
        """Nối một bản ghi; có thể gộp trước (bản ghi tên gần trần) hay sau (`needs_compaction`).

        Đã đóng hay bản ghi rỗng → ValueError; nhật ký hỏng → `StorageError`; lỗi ghi → OSError
        (nhật ký hỏng từ đó).
        """
        data = bytes(record)
        if not data:
            raise ValueError("bản ghi rỗng")
        kind = data[0]
        self._make_room(kind)
        fd = self._writable()
        try:
            _write_all(fd, data)
        except OSError:
            self._broken = True
            raise
        self._length += len(data)
        if kind == RECORD_MARK:
            self._marked = min(self._count, self._marked + int.from_bytes(data[1:3], "big"))
        elif kind in (RECORD_TARGET, RECORD_RESERVED):
            name = data[3:-4].decode("utf-8")
            self._name_records += 1
            if kind == RECORD_TARGET:
                self._target, self._reserved = name, None
            elif name == self._target:
                self._reserved = name
        if needs_compaction(self._length, self._estimate()):
            self.compact()

    def _make_room(self, kind: int) -> None:
        """Trước khi nối bản ghi loại `kind`: bản ghi tên mà số bản ghi tên ≥ `MAX_NAME_RECORDS` − 2 → gộp.

        Lỗi ở đây xảy ra trước khi ghi byte nào của bản ghi đó (`finalize` dựa vào điều này để biết
        `RESERVED` chưa thể nằm trong `.state`). Đã đóng → ValueError; hỏng → `StorageError`.
        """
        self._writable()
        if kind in (RECORD_TARGET, RECORD_RESERVED) and self._name_records >= MAX_NAME_RECORDS - 2:
            self.compact()

    def sync(self) -> None:
        """`fsync` `.state`. Đã đóng → ValueError; hỏng → `StorageError`; lỗi → OSError (hỏng từ đó)."""
        fd = self._writable()
        try:
            _full_fsync(fd)
        except OSError:
            self._broken = True
            raise

    def rewrite_header(self, *, target: str | None = None) -> None:
        """Thay `.state` bằng header (kèm `TARGET` + `RESERVED` của `target`) trong một lần ghi nguyên tử.

        Quên mọi bản ghi cũ. Đã đóng → ValueError; tên sai luật `encode_target` → `StateError`;
        lỗi đĩa → OSError (nhật ký hỏng). Thành công thì nhật ký hết hỏng.
        """
        if self._closed:
            raise ValueError("ResumeLog đã đóng")
        chunks = self._header_chunks(self._temp.transfer_id, self._file_index, self._size, target)
        self._replace(
            chunks,
            marked=0,
            target=target,
            reserved=target,
            name_records=2 if target is not None else 0,
        )

    def compact(self) -> None:
        """Ghi lại bản gọn của chính `.state` (đọc lại theo đoạn), nguyên tử. Lỗi → nhật ký hỏng."""
        self._writable()
        self._close_fd()
        try:
            result = self._read_back()
        except BaseException:
            self._broken = True
            raise
        self._write_compact(
            result.bits, result.leaves, result.target, result.target if result.reserved else None
        )

    def close(self) -> None:
        """Đóng fd. Gọi lại được; không xóa gì."""
        self._closed = True
        self._close_fd()

    # --- nội bộ ---

    def _writable(self) -> int:
        if self._closed:
            raise ValueError("ResumeLog đã đóng")
        if self._broken or self._fd is None:
            raise StorageError(f"lượt {self._temp.transfer_id}, file {self._file_index}: .state hỏng")
        return self._fd

    def _estimate(self) -> int:
        return compact_length_for(min(self._marked, self._count), self._target, self._reserved)

    def _close_fd(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            _close_quietly(fd)

    def _close_quietly(self) -> None:
        self.close()

    def _read_back(self) -> ReplayResult:
        """Đọc lại chính `.state` (cả header) bằng `StateReplay` trong trần; header sai → `StorageError`."""
        fd = self._temp._ref().open(self._name, _READ_FLAGS)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or not _same_identity(self._identity, st):
                raise StorageError(f"lượt {self._temp.transfer_id}: .state đã bị thay")
            replay = StateReplay(self._count, max_length=max_state_length(self._count))
            head = bytearray()
            try:
                while True:
                    chunk = os.read(fd, max(1, STATE_READ_CHUNK))
                    if not chunk:
                        break
                    if len(head) < HEADER_SIZE:
                        head += chunk[: HEADER_SIZE - len(head)]
                    if not replay.feed(chunk):
                        break
                result = replay.finish()
            except StateError as e:
                raise StorageError(f"lượt {self._temp.transfer_id}: .state hỏng") from e
        finally:
            _close_quietly(fd)
        if not check_header(
            head,
            transfer_id=self._temp.transfer_id,
            file_index=self._file_index,
            size=self._size,
            block_size=BLOCK_SIZE,
        ):
            raise StorageError(f"lượt {self._temp.transfer_id}: header .state không khớp")
        return result

    def _write_compact(
        self, bits: Bitfield, leaves: bytearray, target: str | None, reserved: str | None
    ) -> None:
        """Thay `.state` bằng bản gọn của `bits`, `leaves`, `target`, `reserved` (ghi theo đoạn)."""
        header = encode_header(self._temp.transfer_id, self._file_index, self._size)
        records = compact_records(bits, leaves, target, reserved is not None)
        self._replace(
            itertools.chain([header], records),
            marked=bits.count(),
            target=target,
            reserved=reserved,
            name_records=(target is not None) + (reserved is not None),
        )

    def _replace(
        self,
        chunks: Iterable[bytes],
        *,
        marked: int,
        target: str | None,
        reserved: str | None,
        name_records: int,
    ) -> None:
        """Đóng fd nối thêm, ghi nguyên tử `chunks`, mở lại để nối; đặt lại bộ đếm theo đúng file mới."""
        self._close_fd()
        length = 0

        def counted() -> Iterator[bytes]:
            nonlocal length
            for chunk in chunks:
                length += len(chunk)
                yield chunk

        try:
            identity = _write_atomic_in(self._temp, self._name, counted())
            fd = self._open_append(self._temp, self._name, identity)
        except BaseException:
            self._broken = True
            raise
        self._fd, self._identity = fd, identity
        self._length = length
        self._marked = marked
        self._target, self._reserved = target, reserved
        self._name_records = name_records
        self._broken = False


class PartFile:
    """File đang nhận của một file trong `offer`: `.part` và nhật ký `.state` (`log`).

    Chỉ `TransferTemp.create_part` hay `ReceiveDir.open_for_resume` tạo. `mode`
    (`ResumeMode`): `NORMAL` / `REUSE_PLACEHOLDER` có `.part`; `VERIFY_TARGET` không có fd
    `.part` (`write_at`, `sync`, `commit` → ValueError) và `reread` đọc file đích.
    """

    def __init__(
        self,
        key: object,
        temp: TransferTemp,
        file_index: int,
        size: int,
        count: int,
        fd: int | None,
        identity: tuple[int, int] | None,
        resume_log: ResumeLog,
        *,
        mode: ResumeMode = ResumeMode.NORMAL,
        end: int = 0,
        own_part: bool = True,
        target_name: str | None = None,
        target_identity: tuple[int, int] | None = None,
    ) -> None:
        _check_key(key)
        self._temp = temp
        self._file_index = file_index
        self._size = size
        self._block_count = count
        self._name = f"{file_index}.part"
        self._path = temp.path / self._name
        self._fd: int | None = fd
        self._identity = identity
        self._log = resume_log
        self._mode = mode
        self._end = end  # byte cuối của phần đã ghi: write_at không ghi khối bắt đầu sau đây
        self._own_part = own_part  # `.part` do PartFile này tạo hay mở (VERIFY_TARGET: không có)
        # REUSE_PLACEHOLDER: tên và danh tính file giữ chỗ; VERIFY_TARGET: của file đích.
        self._target_name = target_name
        self._target_identity = target_identity
        self._sync_failed = False  # một lần `fsync` `.part` lỗi: không bao giờ nối `MARK` nữa
        self._closed = False
        self._finalized = False

    @property
    def file_index(self) -> int:
        return self._file_index

    @property
    def size(self) -> int:
        return self._size

    @property
    def block_count(self) -> int:
        return self._block_count

    @property
    def path(self) -> Path:
        """`<thư mục tạm>/<file_index>.part`."""
        return self._path

    @property
    def log(self) -> ResumeLog:
        """Nhật ký `<file_index>.state` của file này."""
        return self._log

    @property
    def mode(self) -> ResumeMode:
        return self._mode

    @property
    def closed(self) -> bool:
        return self._closed

    def write_at(self, index: int, data: bytes | bytearray | memoryview) -> None:
        """Ghi khối `index` (đủ `block_length(size, index)` byte) tại `index × BLOCK_SIZE`.

        Đã đóng (hay `VERIFY_TARGET`), `index` ngoài 0…`block_count` − 1, `data` không phải
        `bytes`/`bytearray`/`memoryview` liền, sai độ dài → ValueError, file không đổi. Khối bắt
        đầu sau cuối phần đã ghi → `WriteGapError` trước mọi I/O (NTFS, FAT/exFAT sẽ ghi số 0 cho
        cả khoảng trống). Ghi thiếu thì ghi tiếp tới đủ; không chép `data`. OSError bay ra nguyên trạng.
        """
        fd = self._fd
        if fd is None:
            raise ValueError("PartFile đã đóng")
        if type(index) is not int or not 0 <= index < self._block_count:
            raise ValueError(f"index phải là số nguyên 0…{self._block_count - 1}")
        if isinstance(data, (bytes, bytearray)):
            view = memoryview(data)
        elif isinstance(data, memoryview):
            if not data.c_contiguous:
                raise ValueError("memoryview phải liền (C-contiguous)")
            view = data.cast("B")
        else:
            raise ValueError("data phải là bytes, bytearray hoặc memoryview")
        with view:
            total = view.nbytes
            if total != block_length(self._size, index):
                raise ValueError(f"khối {index} phải dài {block_length(self._size, index)} byte")
            offset = index * BLOCK_SIZE
            if offset > self._end:
                raise WriteGapError(f"{self._label()}: khối {index} nằm sau cuối phần đã ghi")
            written = 0
            try:
                if sys.platform == "win32":
                    os.lseek(fd, offset, os.SEEK_SET)
                    while written < total:
                        n = os.write(fd, view[written:])
                        if n <= 0:
                            raise OSError(errno.EIO, "ghi không tiến được")
                        written += n
                else:
                    while written < total:
                        n = os.pwrite(fd, view[written:], offset + written)
                        if n <= 0:
                            raise OSError(errno.EIO, "ghi không tiến được")
                        written += n
            finally:
                if written:
                    self._end = max(self._end, offset + written)

    def sync(self) -> None:
        """Làm bền dữ liệu đã ghi (`_full_fsync`). Đã đóng → ValueError; lỗi `fsync` → OSError.

        Lỗi thì từ đó `commit` không bao giờ nối `MARK` nữa.
        """
        fd = self._fd
        if fd is None:
            raise ValueError("PartFile đã đóng")
        try:
            _full_fsync(fd)
        except OSError:
            self._sync_failed = True
            raise

    def commit(self, blocks: Sequence[tuple[int, bytes]]) -> None:
        """`SyncPart`: `fsync` `.part` → nối `MARK` các khối `blocks` (`(index, sha256)`) → `fsync` `.state`.

        Mỗi `MARK` tối đa 65 535 khối; `blocks` rỗng → chỉ `fsync` `.part`. Bên gọi (engine) chỉ
        đưa khối đã ghi xong. Một lần `fsync` `.part` lỗi thì không bao giờ nối `MARK` nữa:
        lỗi đó bay ra (OSError), mọi `commit` sau → `StorageError` (protocol mục 4.3). Đã đóng (hay
        `VERIFY_TARGET`), `blocks` sai → ValueError, không I/O.
        """
        fd = self._fd
        if fd is None or self._closed:
            raise ValueError("PartFile đã đóng")
        items = self._check_blocks(blocks)
        if self._sync_failed:
            raise StorageError(f"{self._label()}: fsync .part đã lỗi, không đánh dấu khối nữa")
        try:
            _full_fsync(fd)
        except OSError:
            self._sync_failed = True
            raise
        if not items:
            return
        for start in range(0, len(items), MAX_RECORD_BLOCKS):
            self._log.append(encode_mark(items[start : start + MAX_RECORD_BLOCKS]))
        self._log.sync()

    def clear(self, indices: Sequence[int] | None) -> None:
        """`ClearBlocks`: nối `CLEAR` các khối `indices` (mỗi bản ghi tối đa 65 535), `None` → `CLEAR_ALL`.

        Không `fsync` (mất `CLEAR` thì lượt đọc lại cuối bắt lại, architecture mục 4). `indices`
        rỗng → không làm gì. Đã đóng, chỉ số sai → ValueError, không I/O.
        """
        if self._closed:
            raise ValueError("PartFile đã đóng")
        if indices is None:
            self._log.append(encode_clear_all())
            return
        items = list(indices)
        for i in items:
            if type(i) is not int or not 0 <= i < self._block_count:
                raise ValueError(f"chỉ số khối phải là số nguyên 0…{self._block_count - 1}")
        for start in range(0, len(items), MAX_RECORD_BLOCKS):
            self._log.append(encode_clear(items[start : start + MAX_RECORD_BLOCKS]))

    def reread(self, *, stop: threading.Event | None = None) -> bytearray:
        """Đọc lại từ đĩa; trả hash SHA-256 của từng khối nối liền (32 × `block_count` byte).

        Nguồn theo `mode`: `VERIFY_TARGET` đọc file đích trong thư mục nhận (chỉ đọc, `O_NOFOLLOW`,
        phải là file thường đúng `size`, cùng danh tính lúc `open_for_resume`; không thì mọi khối có
        hash 0), không cắt, không ghi; còn lại đọc `.part`: dài hơn `size` → cắt về `size` rồi
        `fsync`. Khối đọc thiếu (kể cả nằm hẳn sau cuối file) giữ 32 byte 0, nên luôn bị coi là
        sai. Đọc bằng một bộ đệm 1 khối dùng lại (mvp N3). Không tính Merkle root. `stop` đã đặt
        trước khi đọc một khối → `RereadStopped`. Đã đóng, `stop` không phải `threading.Event` →
        ValueError.
        """
        if self._closed:
            raise ValueError("PartFile đã đóng")
        if stop is not None and not isinstance(stop, threading.Event):
            raise ValueError("stop phải là None hoặc threading.Event")
        if self._mode is ResumeMode.VERIFY_TARGET:
            fd = self._temp._owner._open_target_readonly(self)
            if fd is None:
                return bytearray(DIGEST_SIZE * self._block_count)
            try:
                return _hash_blocks(fd, self._size, self._block_count, stop, self._label())
            finally:
                _close_quietly(fd)
        fd = self._fd
        if fd is None:
            raise ValueError("PartFile đã đóng")
        st_size = os.fstat(fd).st_size
        if st_size > self._size:
            os.ftruncate(fd, self._size)
            _full_fsync(fd)
        self._end = min(st_size, self._size)
        return _hash_blocks(fd, self._size, self._block_count, stop, self._label())

    def drop_target(self) -> None:
        """`VERIFY_TARGET` mà file đích sai: ghi lại header `.state`, rồi tạo `.part` rỗng mới (`O_EXCL`).

        Header bền trước (quên `TARGET` + `RESERVED`, file đích không còn được coi là của mình),
        `.part` tạo sau: sập ở giữa thì lần sau rơi vào dòng “thiếu `.part`” của bảng, không bao giờ
        còn `.part` cạnh `TARGET` + `RESERVED` cũ. Từ đó `mode` = `NORMAL`, phần đã ghi rỗng; không
        đụng file đích (bên gọi báo mọi khối sai, và tới bước đổi tên thì chọn tên mới). `mode` khác
        hay đã đóng → ValueError; `.part` đã có → FileExistsError; lỗi đĩa → OSError.
        """
        if self._closed or self._mode is not ResumeMode.VERIFY_TARGET:
            raise ValueError("drop_target chỉ dùng khi mode là VERIFY_TARGET")
        ref = self._temp._ref()
        self._temp._check_dir()
        self._log.rewrite_header()
        self._target_name = self._target_identity = None
        try:
            fd = ref.open(self._name, _PART_FLAGS)
            try:
                identity = _identity(os.fstat(fd))
            except BaseException:
                _close_quietly(fd)
                raise
        except BaseException:
            self._closed = True  # không còn nguồn để đọc lại: PartFile này hết dùng được
            raise
        self._fd, self._identity, self._own_part = fd, identity, True
        self._mode = ResumeMode.NORMAL
        self._end = 0

    def close(self) -> None:
        """Đóng fd `.part` và nhật ký. Gọi lại được; không xóa gì."""
        self._closed = True
        self._close_fd()
        self._log.close()

    def _close_fd(self) -> None:
        """Đóng fd `.part` (bước F1 của `finalize`); từ đây `closed`. Nhật ký vẫn mở."""
        self._closed = True
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def _close_quietly(self) -> None:
        self._closed = True
        fd, self._fd = self._fd, None
        if fd is not None:
            _close_quietly(fd)
        self._log._close_quietly()

    def _check_blocks(self, blocks: Sequence[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
        """Kiểm `(index, sha256)` trước mọi I/O: chỉ số trong file (bộ đọc coi index ≥ số khối là hỏng)."""
        items: list[tuple[int, bytes]] = []
        try:
            for index, digest in blocks:
                if type(index) is not int or not 0 <= index < self._block_count:
                    raise ValueError(f"chỉ số khối phải là số nguyên 0…{self._block_count - 1}")
                if (
                    not isinstance(digest, bytes | bytearray | memoryview)
                    or len(bytes(digest)) != DIGEST_SIZE
                ):
                    raise ValueError(f"sha256 phải là {DIGEST_SIZE} byte")
                items.append((index, bytes(digest)))
        except TypeError as e:  # không lặp được, hay phần tử không phải cặp
            raise ValueError("blocks phải là các cặp (index, sha256)") from e
        return items

    def _label(self) -> str:
        """Nhãn cho log và thông điệp lỗi: chỉ `transfer_id` và `file_index`, không bao giờ tên file."""
        return f"lượt {self._temp.transfer_id}, file {self._file_index}"


def _check_root_length(root: str) -> PathLimits:
    limits = _path_limits(root)
    if _units(root) + TEMP_PATH_RESERVE > limits.max_path:
        raise PathTooLongError("đường dẫn thư mục nhận quá dài: không còn chỗ cho file tạm và tên file")
    return limits


def _raise_if_unsafe_open(e: OSError) -> None:
    if e.errno in _UNSAFE_OPEN_ERRNOS:
        raise UnsafeReceiveDirError("cây .teleport có liên kết hoặc mục không phải thư mục") from e


def _open_dir_nofollow(entry: str, dir_fd: int) -> int:
    """POSIX: mở thư mục con `entry` của `dir_fd` bằng `O_NOFOLLOW`; liên kết → `UnsafeReceiveDirError`."""
    try:
        return os.open(entry, _DIR_NOFOLLOW_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        _raise_if_unsafe_open(e)
        raise


def _fchmod_tolerant(fd: int, mode: int) -> None:
    """POSIX `fchmod`; hệ file không hỗ trợ quyền (EPERM, ENOTSUP…) → bỏ qua, log DEBUG."""
    if sys.platform == "win32":
        return
    try:
        os.fchmod(fd, mode)
    except OSError as e:
        if e.errno not in _CHMOD_IGNORED_ERRNOS:
            raise
        log.debug("không chmod được .teleport (%s), bỏ qua", _errno_name(e))


def _set_mtime(target: int | Path, mtime_ms: int, part: PartFile) -> None:
    """`atime` = giờ hiện tại, `mtime` = `mtime_ms`; mọi lỗi (kể cả tràn `time_t`/FILETIME) bỏ qua."""
    try:
        os.utime(target, ns=(time.time_ns(), mtime_ms * 1_000_000))
    except (OSError, OverflowError, ValueError) as e:
        log.debug("%s: không đặt được mtime (%s)", part._label(), type(e).__name__)


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError as e:
        log.debug("đóng fd lỗi (%s)", _errno_name(e))
