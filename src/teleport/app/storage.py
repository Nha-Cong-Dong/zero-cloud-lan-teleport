"""Lưu file nhận an toàn: thư mục tạm `.teleport`, file `.part`, đổi tên không ghi đè.

Bố cục và các luật ở docs/architecture.md mục 4 (“Thư mục nhận”, “Kết thúc một file”), luật
đổi tên ở docs/protocol.md mục 4.4:

    <thư mục nhận>/.teleport/<transfer_id>/<file_index>.part

- `ReceiveDir.open(root)` mở thư mục nhận (tạo nếu chưa có). Thư mục nhận được phép là symlink
  hay junction; luật cấm liên kết chỉ áp trong cây `.teleport`. `.teleport` (`0o700` trên
  Linux/macOS; Windows: ẩn khi vừa tạo, ACL kế thừa từ thư mục nhận) được tạo ở lần
  `create_temp` đầu tiên và không bao giờ bị xóa ở đây.
- `create_temp(transfer_id)` tạo độc quyền `<transfer_id>`; đã có → `TempExistsError`, không
  đụng nội dung cũ. `TransferTemp.create_part` tạo `<i>.part` bằng `O_EXCL`.
- `PartFile.write_at` ghi một khối đúng vị trí và không bao giờ tạo lỗ sau cuối phần đã ghi
  (`WriteGapError`); `sync` dùng `F_FULLFSYNC` trên macOS; `reread` trả hash SHA-256 của từng
  khối đọc lại từ đĩa bằng một bộ đệm 1 khối (mvp N3) và cắt `.part` thừa.
- `finalize` chọn tên (so trùng NFC + casefold trên mọi OS, giữ chỗ bằng `O_CREAT | O_EXCL`,
  cắt tên cho vừa giới hạn đường dẫn, tên dự phòng ASCII khi hệ file từ chối tên), kiểm lại
  file giữ chỗ và `.part`, rồi đổi tên đè lên chính file giữ chỗ. Không bao giờ ghi đè hay xóa
  file không phải của mình. Giai đoạn 2 chưa ghi `TARGET`/`RESERVED` vào `.state`.
- `remove_temp` chỉ xóa đúng các `.part` do lượt đó tạo (cùng danh tính) và thư mục
  `<transfer_id>` khi đã rỗng; không bao giờ ném `OSError`.
- `volume_info` nhận ra hệ file FAT (trần kích thước file ở protocol mục 3.3) và dung lượng trống.

Toàn bộ API là đồng bộ (chặn), gọi từ thread; executor và hạn cho mỗi lần ghi hay `fsync` do
`app/transfers.py` quyết. `ReceiveDir` an toàn khi nhiều thread gọi song song cho các lượt khác
nhau; một `TransferTemp` và các `PartFile` của nó chỉ được một thread dùng tại một thời điểm.
`remove_temp` và `close` đòi không thread nào đang dùng các `PartFile` mà chúng đóng (đóng fd
giữa lúc thread khác ghi có thể làm thread đó ghi vào một fd khác vừa được cấp lại cùng số).

Lỗi: dùng sai API → `ValueError` (riêng `name` không phải chuỗi → `TypeError`); lỗi I/O thật
→ `OSError` nguyên trạng; các tình huống riêng → lớp con của `StorageError` (không lớp nào là
con của `OSError`). Module không có câu chữ cho người dùng, và log không bao giờ chứa tên file
nhận được hay nội dung file (mvp N9): chỉ `transfer_id`, `file_index`, loại lỗi và `errno`.

Nhánh POSIX (`sys.platform != "win32"`) làm mọi thao tác trong thư mục nhận qua `dir_fd` và
`O_NOFOLLOW`; nhánh Windows dùng đường dẫn đầy đủ và `MoveFileExW`. Không có đường lùi từ POSIX
sang nhánh đường dẫn: `os.replace` không có trong `os.supports_dir_fd` dù vẫn nhận
`src_dir_fd`/`dst_dir_fd`.

Bẫy:
- Windows: thiếu `O_BINARY` thì CRT đổi `\\n` thành `\\r\\n`. Không xóa hay đổi tên được file
  đang mở, nên mọi handle đóng trước khi đổi tên; antivirus giữ file → thử lại khi gặp
  `PermissionError`. `O_EXCL` lên tên là thư mục hay file đang chờ xóa có thể báo
  `PermissionError` thay vì `FileExistsError`. Đặt `mtime` khi handle ghi còn mở có thể bị NTFS
  đặt lại lúc đóng, nên đặt sau khi đóng. `os.mkdir(path, 0o700)` gắn DACL được bảo vệ mà file
  nhận xong mang theo khi đổi tên, nên cây `.teleport` tạo không truyền mode (`_TEMP_DIR_MODE`).
  Đường dẫn file cuối luôn ≤ `WINDOWS_MAX_PATH`, không dùng `\\\\?\\`, kể cả khi máy đã bật long
  path. Reparse point không phải name-surrogate (OneDrive Files On-Demand) không bị coi là liên
  kết.
- `st_ino` = 0 (FAT, ổ mạng): không có danh tính, chỉ so loại file và kích thước. Cờ `renamed`
  của `finalize`, cùng bước kiểm `.part` đã rời thư mục tạm hay chưa, chặn việc xóa nhầm file
  0 byte vừa nhận.
- Ghi xa sau cuối file trên NTFS hay FAT/exFAT bắt hệ điều hành ghi số 0 cho cả khoảng trống, lâu
  và không hủy được: `write_at` từ chối trước mọi I/O (`WriteGapError`).
- `MAX_PATH` / `PC_PATH_MAX` chỉ đo lúc `open`. Mỗi `finalize` liệt kê lại cả thư mục nhận.
- Không dùng `os.readinto` (chỉ có từ Python 3.14).

Giới hạn đã biết:
- Windows không có `dir_fd`: một tiến trình có quyền ghi vào thư mục nhận mà đua thay `.teleport`
  hay `<transfer_id>` bằng junction đúng giữa bước kiểm và bước tạo thì có thể làm file tạm của
  chính lượt này được tạo ở chỗ khác. File của nạn nhân vẫn không bao giờ bị xóa, ghi đè hay sửa
  (tạo bằng `O_EXCL`, xóa thì so danh tính trước), và `.part` được kiểm lại danh tính trước khi
  đổi tên.
- POSIX: `ReceiveDir` giữ fd của thư mục nhận tới `close()`, nên chưa tháo được ổ USB khi
  `ReceiveDir` còn mở.
- POSIX: chủ hợp lệ của cây `.teleport` là euid hoặc chủ thư mục nhận; `chmod` lỗi trên hệ file
  không hỗ trợ (vfat, exfat, ntfs-3g, cifs mount theo uid cố định) thì bỏ qua. Chạy bằng root
  trên NFS có `root_squash` vẫn có thể bị từ chối.
- `MAX_NAME_CANDIDATES` chỉ là trần an toàn nội bộ chặn vòng lặp vô hạn khi chọn tên, không phải
  số của sản phẩm.

Seam cho test (biến cấp module, đọc lúc dùng): `REPLACE_ATTEMPTS`, `REPLACE_DELAY`,
`_fs_type_of`, `_MOUNTINFO_PATH`, `_parse_mountinfo`, `_path_limits`, `_after_reserve`,
`_move_file_ex` (chỉ Windows), `_full_fsync`. Thư viện chuẩn luôn được gọi qua module
(`os.open`, `os.replace`, `shutil.disk_usage`, `time.sleep`…).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import logging
import os
import re
import shutil
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from teleport import atomic
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
from teleport.core.messages import MAX_OFFER_FILES, validate_transfer_id

# sys.platform thay vì os.name: mypy chỉ hiểu phép so sánh sys.platform để bỏ qua nhánh của
# hệ điều hành khác (ctypes.WinDLL, fcntl.F_FULLFSYNC chỉ có trên một phía).
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
elif sys.platform == "darwin":
    import ctypes
    import fcntl

FAT_MAX_FILE_SIZE = (1 << 32) - 1  # protocol mục 3.3 (`reject.file_too_large`, FAT32)
# Tên hệ file (chữ thường) có trần FAT_MAX_FILE_SIZE (architecture mục 4 “Thư mục nhận”).
FAT_FS_TYPES = frozenset({"fat", "fat12", "fat16", "fat32", "vfat", "msdos", "umsdos"})
# MAX_PATH 260 trừ ký tự NUL, đơn vị UTF-16 code unit; áp cả khi máy đã bật long path.
WINDOWS_MAX_PATH = 259
# Phần độ dài thư mục nhận phải chừa (architecture mục 4 “Thư mục nhận”): thư mục
# `<root>\.teleport\<transfer_id>` dài root + 43 phải ≤ MAX_PATH − 12 − 1 = 247 (CreateDirectoryW
# chừa chỗ cho tên 8.3), chặt hơn `.part` dài nhất root + 52 ≤ 259.
TEMP_PATH_RESERVE = 55
REPLACE_ATTEMPTS = atomic.REPLACE_ATTEMPTS  # protocol mục 4.4; đọc lúc dùng (test vá)
REPLACE_DELAY = atomic.REPLACE_DELAY
MAX_NAME_CANDIDATES = 100_000  # trần an toàn nội bộ khi chọn tên (docstring module)
# Cờ của MoveFileExW (chỉ dùng trên Windows; architecture mục 4, ghi chú nền tảng).
MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_WRITE_THROUGH = 0x8

_MOUNTINFO_PATH = "/proc/self/mountinfo"

# Cờ nào hệ điều hành không có thì lấy 0. Windows bắt buộc O_BINARY.
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_DIR_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC
_DIR_NOFOLLOW_FLAGS = _DIR_FLAGS | _O_NOFOLLOW
_PART_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW | _O_CLOEXEC
_RESERVE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY | _O_NOFOLLOW | _O_CLOEXEC

# Chỉ dùng trên Linux/macOS. Windows tạo `.teleport` và `<transfer_id>` không truyền mode: từ
# Python 3.12.4 (CVE-2024-4030), `os.mkdir(path, 0o700)` gắn một DACL được bảo vệ (chỉ SYSTEM,
# Administrators, OWNER RIGHTS); `.part` kế thừa DACL đó và `MoveFileExW` giữ nguyên nó khi đổi
# tên, nên file nhận xong không kế thừa ACL của thư mục nhận như file tải về thường. Trên Windows
# dữ liệu đang nhận được che bằng ACL của chính thư mục nhận.
_TEMP_DIR_MODE = 0o700
_FILE_MODE = 0o666  # umask vẫn áp: file nhận xong mang quyền như file tải về thường

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
    """`.teleport/<transfer_id>` đã có; nội dung cũ không bị đụng."""


class NameUnavailableError(StorageError):
    """Hệ file từ chối cả tên dự phòng, hoặc đã thử quá `MAX_NAME_CANDIDATES` tên."""


class RereadStopped(StorageError):
    """`reread` bị dừng bằng `stop`."""


class WriteGapError(StorageError):
    """Ghi khối này sẽ tạo lỗ sau cuối phần đã ghi của `.part`; file không đổi."""


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


def _errno_name(e: OSError) -> str:
    return errno.errorcode.get(e.errno, str(e.errno)) if e.errno is not None else "-"


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


def _after_reserve(target: Path) -> None:
    """Seam cho test: chạy sau khi file giữ chỗ đã bền, trước bước kiểm lại và đổi tên."""


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

    _FS_NAME_CHARS = 261  # MAX_PATH + 1, như tài liệu GetVolumeInformationW

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

    def create_temp(self, transfer_id: str) -> TransferTemp:
        """Tạo `.teleport/<transfer_id>/` (`0o700`) cho một lượt nhận (architecture mục 4).

        `transfer_id` phải đúng 32 ký tự hex thường (protocol mục 3.3; kiểm lại ở đây vì nó thành
        tên thư mục; kiểm bằng `teleport.core.messages.validate_transfer_id`), sai → `MessageError`
        (lớp con của ValueError), không đụng gì trên đĩa. Đã có → `TempExistsError`; liên kết hay
        sai chủ trong cây `.teleport` → `UnsafeReceiveDirError`; lỗi đĩa → OSError.
        """
        self._ensure_open()
        validate_transfer_id(transfer_id)
        if sys.platform == "win32":
            temp = self._create_temp_windows(transfer_id)
        else:
            temp = self._create_temp_posix(transfer_id)
        with self._lock:
            if self._closed:
                temp._close_quietly()
                raise ValueError("ReceiveDir đã đóng")
            self._temps.add(temp)
        return temp

    def finalize(self, part: PartFile, name: str, *, mtime_ms: int | None = None) -> Path:
        """Đổi `.part` đã kiểm xong sang tên thật trong thư mục nhận; trả đường dẫn file cuối.

        Theo architecture mục 4 “Kết thúc một file” (bước 2, 3, 5, 6; Giai đoạn 2 chưa có
        `TARGET`/`RESERVED`): đặt `mtime` và `fsync` `.part` rồi đóng nó; chọn tên không trùng
        file nào đã có (NFC + casefold, cắt cho vừa giới hạn đường dẫn); giữ chỗ bằng
        `O_CREAT | O_EXCL` và làm bền; kiểm lại file giữ chỗ (khác đi → để nguyên, chọn tên mới)
        và `.part`; đổi tên đè lên chính file giữ chỗ (`PermissionError` → thử lại). Hệ file từ
        chối tên → tên dự phòng ASCII.

        Lỗi sau khi đã giữ chỗ mà chưa đổi tên xong → xóa file giữ chỗ nếu nó vẫn là của mình
        (0 byte, cùng danh tính); `.part` nằm nguyên trong thư mục tạm. `part` đã đóng từ bước
        `fsync`, nên không finalize lại được. Dùng sai API → ValueError (`name` không phải chuỗi →
        TypeError); `.part` khác kích thước hay bị thay → `StorageError`; hết tên →
        `NameUnavailableError`; không còn chỗ cho tên → `PathTooLongError`; lỗi đĩa → OSError.
        """
        self._ensure_open()
        if not isinstance(part, PartFile) or part._temp._owner is not self:
            raise ValueError("part không thuộc ReceiveDir này")
        if part._fd is None or part._finalized:
            raise ValueError("part đã đóng hoặc đã finalize")
        if mtime_ms is not None and (type(mtime_ms) is not int or mtime_ms < 0):
            raise ValueError("mtime_ms phải là None hoặc số nguyên ≥ 0")
        if not isinstance(name, str):
            raise TypeError("name phải là chuỗi")
        if os.fstat(part._fd).st_size != part.size:
            raise StorageError(f"{part._label()}: .part không đúng kích thước, không đổi tên")
        self._flush_part(part, part._fd, mtime_ms)  # F1
        taken = {dedupe_key(entry) for entry in self._list_root()}  # F2
        candidate = self._place(part, name, taken)  # F3–F8
        self._sync_root()  # F9
        return self._root / candidate

    def remove_temp(self, temp: TransferTemp) -> bool:
        """Xóa thư mục tạm của một lượt: chỉ đúng các `.part` nó tạo, rồi `<transfer_id>` khi rỗng.

        Đóng mọi `PartFile` còn mở của `temp` (điều kiện trước: không thread nào đang dùng
        chúng). `.part` chưa finalize mà còn là file thường cùng danh tính lúc tạo → xóa; khác
        đi → để nguyên. Không xóa theo mẫu tên, không đệ quy, không xóa `.teleport`. Gọi lại được.
        Trả True khi và chỉ khi `<transfer_id>` không còn. Không bao giờ ném OSError (lỗi chỉ ghi
        log DEBUG). `temp` của `ReceiveDir` khác, hay `ReceiveDir` đã đóng → ValueError.
        """
        if not isinstance(temp, TransferTemp) or temp._owner is not self:
            raise ValueError("temp không thuộc ReceiveDir này")
        self._ensure_open()
        if temp._removed:
            return self._temp_dir_gone(temp)
        for part in temp._parts.values():
            part._close_quietly()
        for part in temp._parts.values():
            if not part._finalized:
                self._remove_part(temp, part)
        temp._close_fd_quietly()
        gone = self._remove_temp_dir(temp)
        with self._lock:
            self._temps.discard(temp)
        temp._removed = True
        temp._closed = True
        return gone

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

        Chưa có: `create` → tạo (`0o700`) rồi mở lại; không thì trả None. Liên kết, không phải
        thư mục hay sai chủ → `UnsafeReceiveDirError`. `create` còn chỉnh quyền về `0o700`.
        """
        root_fd = self._root_fd_or_raise()
        try:
            fd = os.open(TEMP_DIR_NAME, _DIR_NOFOLLOW_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            if not create:
                return None
            # Tiến trình khác vừa tạo chen cũng được: bước mở dưới đây kiểm lại.
            with contextlib.suppress(FileExistsError):
                os.mkdir(TEMP_DIR_NAME, _TEMP_DIR_MODE, dir_fd=root_fd)
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

    def _create_temp_posix(self, transfer_id: str) -> TransferTemp:
        temp_root_fd = self._open_temp_root(create=True)
        assert temp_root_fd is not None
        try:
            try:
                os.mkdir(transfer_id, _TEMP_DIR_MODE, dir_fd=temp_root_fd)
            except FileExistsError as e:
                raise TempExistsError(f"thư mục tạm của lượt {transfer_id} đã có") from e
            fd = _open_dir_nofollow(transfer_id, temp_root_fd)
        finally:
            os.close(temp_root_fd)
        try:
            if not self._owner_ok(os.fstat(fd)):
                raise UnsafeReceiveDirError(f"thư mục tạm của lượt {transfer_id} thuộc người dùng khác")
        except BaseException:
            os.close(fd)
            raise
        return TransferTemp(_PRIVATE, self, transfer_id, self._temp_path(transfer_id), fd)

    def _create_temp_windows(self, transfer_id: str) -> TransferTemp:
        self._windows_temp_root(create=True)
        path = self._temp_path(transfer_id)
        try:
            os.mkdir(path)  # không truyền mode: xem _TEMP_DIR_MODE
        except FileExistsError as e:
            raise TempExistsError(f"thư mục tạm của lượt {transfer_id} đã có") from e
        st = os.lstat(path)
        if _is_link(st) or not stat.S_ISDIR(st.st_mode):
            raise UnsafeReceiveDirError(f"thư mục tạm của lượt {transfer_id} là liên kết")
        return TransferTemp(_PRIVATE, self, transfer_id, path, None)

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
        """F3–F8: chọn tên, giữ chỗ, kiểm lại, đổi tên. Trả tên cuối."""
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
                fd = self._open_in_root(candidate, _RESERVE_FLAGS)  # F4
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
        """F5–F8 với file giữ chỗ vừa tạo (`fd`). False: file giữ chỗ đã khác đi, chọn tên mới.

        Lỗi khi chưa đổi tên xong → xóa file giữ chỗ nếu nó vẫn là của mình, rồi lỗi bay ra.
        """
        identity: tuple[int, int] | None = None
        known = False  # đã ghi nhận được danh tính (có thể là None khi hệ file không có st_ino)
        renamed = False
        try:
            try:
                identity = _identity(os.fstat(fd))
                known = True
                if sys.platform == "win32":
                    os.fsync(fd)  # FlushFileBuffers trên handle của chính file giữ chỗ
            finally:
                os.close(fd)
            self._sync_root()  # F5 (POSIX)
            _after_reserve(self._root / candidate)  # F6
            if not self._reserve_intact(candidate, identity):  # F7
                return False
            if not self._part_unchanged(part):  # F7b
                raise StorageError(f"{part._label()}: .part đã bị thay, không đổi tên")
            self._replace(part, candidate)  # F8
            renamed = True
            part._finalized = True
            return True
        except BaseException:
            if known and not renamed:
                self._discard_reserve(part, candidate, identity)
            raise

    def _reserve_intact(self, candidate: str, identity: tuple[int, int] | None) -> bool:
        """File giữ chỗ còn là file thường, 0 byte, cùng danh tính."""
        try:
            st = self._lstat_in_root(candidate)
        except FileNotFoundError:
            return False
        return stat.S_ISREG(st.st_mode) and st.st_size == 0 and _same_identity(identity, st)

    def _discard_reserve(self, part: PartFile, candidate: str, identity: tuple[int, int] | None) -> None:
        """Dọn file giữ chỗ khi lỗi trước lúc đổi tên xong, chỉ khi nó vẫn là của mình. Không ném OSError."""
        try:
            if not self._reserve_intact(candidate, identity):
                return
            # Không có danh tính (st_ino = 0) thì không phân biệt được file giữ chỗ với file 0 byte
            # vừa nhận, khi lỗi bay ra sau lúc đổi tên thật mà trước khi kịp đặt cờ `renamed`
            # (ví dụ KeyboardInterrupt). `.part` đã rời thư mục tạm → coi như đã đổi tên, không xóa.
            # Không kiểm được (lỗi khác FileNotFoundError) → cũng không xóa.
            if identity is None and not self._part_present(part):
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
            if sys.platform == "win32":
                os.lstat(part._path)
            else:
                os.lstat(part._name, dir_fd=part._temp._fd_or_raise())
        except FileNotFoundError:
            return False
        return True

    def _part_unchanged(self, part: PartFile) -> bool:
        """`.part` còn là file thường, đúng `size` byte, cùng danh tính; Windows: không là reparse point."""
        try:
            if sys.platform == "win32":
                st = os.lstat(part._path)
            else:
                st = os.lstat(part._name, dir_fd=part._temp._fd_or_raise())
        except FileNotFoundError:
            return False
        return (
            stat.S_ISREG(st.st_mode)
            and st.st_size == part.size
            and _same_identity(part._identity, st)
            and not _has_reparse_point(st)
        )

    def _replace(self, part: PartFile, candidate: str) -> None:
        """F8: đổi tên `.part` đè lên file giữ chỗ; `PermissionError` → thử lại."""
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

    def _sync_root(self) -> None:
        """POSIX: `fsync` thư mục nhận; lỗi bỏ qua (như `teleport.atomic`). Windows: không làm gì."""
        if sys.platform == "win32":
            return
        try:
            _full_fsync(self._root_fd_or_raise())
        except OSError as e:
            log.debug("fsync thư mục nhận lỗi (%s), bỏ qua", _errno_name(e))

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

    # --- remove_temp ---

    def _remove_part(self, temp: TransferTemp, part: PartFile) -> None:
        """Xóa `.part` của mình nếu nó còn là file thường cùng danh tính lúc tạo. Không raise OSError."""
        try:
            if sys.platform == "win32":
                path = part._path
                st = os.lstat(path)
                if _has_reparse_point(st):
                    return
            else:
                temp_fd = temp._fd_or_raise()
                st = os.lstat(part._name, dir_fd=temp_fd)
            if not stat.S_ISREG(st.st_mode) or not _same_identity(part._identity, st):
                return
            if sys.platform == "win32":
                _retry_on_permission(lambda: os.unlink(path))
            else:
                _retry_on_permission(lambda: os.unlink(part._name, dir_fd=temp_fd))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.debug("%s: không xóa được .part (%s)", part._label(), type(e).__name__)

    def _remove_temp_dir(self, temp: TransferTemp) -> bool:
        """`rmdir <transfer_id>` (không đệ quy); trả True khi thư mục không còn. Không raise OSError."""
        try:
            if sys.platform == "win32":
                path = str(temp._path)
                if _is_link(os.lstat(path)):
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
        except (OSError, StorageError) as e:
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


class TransferTemp:
    """Thư mục tạm `<thư mục nhận>/.teleport/<transfer_id>/` của một lượt nhận.

    Chỉ `ReceiveDir.create_temp` tạo. POSIX: giữ fd của thư mục tới `remove_temp` hay
    `ReceiveDir.close`, mọi thao tác bên trong đi qua fd này.
    """

    def __init__(self, key: object, owner: ReceiveDir, transfer_id: str, path: Path, fd: int | None) -> None:
        _check_key(key)
        self._owner = owner
        self._transfer_id = transfer_id
        self._path = path
        self._fd = fd
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
    def closed(self) -> bool:
        """True sau `remove_temp` hoặc `ReceiveDir.close`."""
        return self._closed

    def create_part(self, file_index: int, size: int) -> PartFile:
        """Tạo `<file_index>.part` rỗng bằng `O_EXCL` (quyền `0o666`, umask áp).

        Temp đã đóng, `file_index` không phải số nguyên 0…`MAX_OFFER_FILES` − 1 hay đã dùng trong
        temp này, `size` sai (`core.blocks.block_count`) → ValueError. File đã có, kể cả symlink
        cài sẵn → FileExistsError, không đụng đích của link.
        """
        if self._closed:
            raise ValueError("TransferTemp đã đóng")
        if type(file_index) is not int or not 0 <= file_index < MAX_OFFER_FILES:
            raise ValueError(f"file_index phải là số nguyên 0…{MAX_OFFER_FILES - 1}")
        count = block_count(size)
        if file_index in self._parts:
            raise ValueError(f"file_index {file_index} đã dùng trong lượt này")
        name = f"{file_index}.part"
        if sys.platform == "win32":
            fd = os.open(str(self._path / name), _PART_FLAGS, _FILE_MODE)
        else:
            fd = os.open(name, _PART_FLAGS, _FILE_MODE, dir_fd=self._fd_or_raise())
        try:
            identity = _identity(os.fstat(fd))
        except BaseException:
            os.close(fd)
            raise
        part = PartFile(_PRIVATE, self, file_index, size, count, fd, identity)
        self._parts[file_index] = part
        return part

    def _fd_or_raise(self) -> int:
        fd = self._fd
        if fd is None:
            raise ValueError("TransferTemp đã đóng")
        return fd

    def _close_fd_quietly(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            _close_quietly(fd)

    def _close_quietly(self) -> None:
        """Đóng mọi `PartFile` còn mở và fd của thư mục; không xóa gì."""
        for part in self._parts.values():
            part._close_quietly()
        self._close_fd_quietly()
        self._closed = True


class PartFile:
    """File `.part` đang nhận của một file trong `offer`. Chỉ `TransferTemp.create_part` tạo."""

    def __init__(
        self,
        key: object,
        temp: TransferTemp,
        file_index: int,
        size: int,
        count: int,
        fd: int,
        identity: tuple[int, int] | None,
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
        self._end = 0  # byte cuối của phần đã ghi: write_at không ghi khối bắt đầu sau đây
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
    def closed(self) -> bool:
        return self._fd is None

    def write_at(self, index: int, data: bytes | bytearray | memoryview) -> None:
        """Ghi khối `index` (đủ `block_length(size, index)` byte) tại `index × BLOCK_SIZE`.

        Đã đóng, `index` ngoài 0…`block_count` − 1, `data` không phải `bytes`/`bytearray`/
        `memoryview` liền, sai độ dài → ValueError, file không đổi. Khối bắt đầu sau cuối phần đã
        ghi → `WriteGapError` trước mọi I/O (NTFS, FAT/exFAT sẽ ghi số 0 cho cả khoảng trống).
        Ghi thiếu thì ghi tiếp tới đủ; không chép `data`. OSError bay ra nguyên trạng.
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
        """Làm bền dữ liệu đã ghi (`_full_fsync`). Đã đóng → ValueError; lỗi `fsync` → OSError."""
        fd = self._fd
        if fd is None:
            raise ValueError("PartFile đã đóng")
        _full_fsync(fd)

    def reread(self, *, stop: threading.Event | None = None) -> bytearray:
        """Đọc lại `.part` từ đĩa; trả hash SHA-256 của từng khối nối liền (32 × `block_count` byte).

        `.part` dài hơn `size` → cắt về `size` rồi `fsync`. Khối đọc thiếu (kể cả nằm hẳn sau
        cuối file) giữ 32 byte 0, nên luôn bị coi là sai. Đọc bằng một bộ đệm 1 khối dùng lại
        (mvp N3), qua fd của chính `PartFile`. Không tính Merkle root. `stop` đã đặt trước khi đọc
        một khối → `RereadStopped`. Đã đóng, `stop` không phải `threading.Event` → ValueError.
        """
        fd = self._fd
        if fd is None:
            raise ValueError("PartFile đã đóng")
        if stop is not None and not isinstance(stop, threading.Event):
            raise ValueError("stop phải là None hoặc threading.Event")
        st_size = os.fstat(fd).st_size
        if st_size > self._size:
            os.ftruncate(fd, self._size)
            _full_fsync(fd)
        self._end = min(st_size, self._size)
        result = bytearray(DIGEST_SIZE * self._block_count)
        if not self._block_count:
            return result
        buffer = bytearray(BLOCK_SIZE)
        os.lseek(fd, 0, os.SEEK_SET)
        with io.FileIO(fd, "r", closefd=False) as reader, memoryview(buffer) as view:
            for i in range(self._block_count):
                if stop is not None and stop.is_set():
                    raise RereadStopped(f"{self._label()}: đọc lại bị dừng")
                length = block_length(self._size, i)
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

    def close(self) -> None:
        """Đóng fd. Gọi lại được; không xóa gì."""
        self._close_fd()

    def _close_fd(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def _close_quietly(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            _close_quietly(fd)

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
