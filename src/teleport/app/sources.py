"""Đọc file nguồn của bên gửi: kiểm trước khi gửi `offer`, đọc từng khối, kiểm lại lúc xác minh.

- `prepare_sources(paths)` kiểm các file người dùng chọn và dựng `FileEntry` cho `offer`
  (tên qua `core.filenames.prepare_outgoing_name`, `mtime` qua `core.transfer.mtime_ms`). Mở thử
  từng file rồi đóng ngay: không giữ handle nào.
- `SourceReader` đọc khối `index` tại `index × BLOCK_SIZE` và băm SHA-256 (hợp đồng `SendBlocks`,
  docstring `core/transfer.py` mục 3). File không còn là file thường, size/mtime khác `offer`
  lúc mở, hay đọc thiếu byte → `SourceChanged`.
- `check_source(path, leaves)` cho `CheckSource`: `stat` theo đường dẫn và tính Merkle root.

Toàn bộ API là đồng bộ (chặn), chỉ gọi trong thread (`app.diskio.DiskExecutor`), không bao giờ
trên event loop. Lỗi I/O thật → `OSError` nguyên trạng, trừ `prepare_sources` (đổi thành
`SourceError` có `kind`). Module không có câu chữ cho người dùng.

Log và tên file (mvp N9): thông điệp của các ngoại lệ module này tự tạo (`SourceError`,
`SourceChanged`, ValueError, TypeError) không chứa tên hay đường dẫn file; đường dẫn chỉ nằm ở
thuộc tính `SourceError.path`. `OSError` bay ra nguyên trạng (kể cả `SourceError.__cause__`) thì
**có thể** mang đường dẫn trong `str(e)` / `e.filename`, nên bên gọi (P2-6b) chỉ log
`type(e).__name__` và `errno`, không log `str(e)` hay traceback, kể cả ở mức DEBUG.

Bẫy:
- `SourceReader` không thread-safe: không gọi `close()` song song với `read_block()`/`open()`
  đang chạy ở thread khác. Bên gọi (P2-6b) chỉ đóng sau `DiskJob.settled()` của mọi việc đọc.
- Windows: trong lúc một `SourceReader` mở (một kết nối dữ liệu đang gửi), file nguồn không xóa
  hay đổi tên được. Chấp nhận: handle chỉ mở trong lúc một `SendBlocks` chạy.
- POSIX: mở một FIFO để đọc sẽ chặn tới khi có bên ghi, nên mọi lần mở dùng `O_NONBLOCK`, kiểm
  `S_ISREG` bằng `fstat` trên chính fd đó, rồi mới bỏ `O_NONBLOCK` (`_open_regular`). Đường dẫn
  bị đổi thành FIFO giữa lúc `stat` và lúc mở vì vậy không làm thread treo.

Seam cho test (biến cấp module, đọc lúc dùng): `MAX_FILE_SIZE`.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

from teleport.core import blocks
from teleport.core.blocks import BLOCK_SIZE, block_count, block_length
from teleport.core.filenames import OutgoingNameError, prepare_outgoing_name
from teleport.core.merkle import merkle_root
from teleport.core.messages import MAX_OFFER_FILES, FileEntry, MessageError
from teleport.core.transfer import mtime_ms

MAX_FILE_SIZE = blocks.MAX_FILE_SIZE  # protocol §3.3; test patch nhỏ lại để thử TOO_LARGE


class SourceErrorKind(StrEnum):
    """Lý do một file nguồn không gửi được."""

    NOT_FOUND = "not_found"
    NOT_A_FILE = "not_a_file"  # thư mục, thiết bị, pipe…
    PERMISSION = "permission"
    TOO_LARGE = "too_large"  # lớn hơn `MAX_FILE_SIZE`
    BAD_NAME = "bad_name"  # tên rỗng hay có ký tự cấm (`OutgoingNameError`)
    OS_ERROR = "os_error"


class SourceError(Exception):
    """Một file nguồn không gửi được; `__cause__` là lỗi gốc (nếu có).

    `size` chỉ có với `TOO_LARGE`; `errno` là errno của OSError gốc (nếu có).
    """

    def __init__(
        self, path: Path, kind: SourceErrorKind, *, size: int | None = None, errno: int | None = None
    ) -> None:
        super().__init__(f"file nguồn không gửi được: {kind.value}")
        self.path = path
        self.kind = kind
        self.size = size
        self.errno = errno


class SourceChanged(Exception):
    """File nguồn không còn là file thường, đọc thiếu byte, hay size/mtime khác manifest."""


@dataclass(frozen=True, slots=True)
class SourceFile:
    """Một file nguồn đã kiểm, kèm mục của nó trong `offer`."""

    path: Path  # tuyệt đối (os.path.abspath, không resolve symlink)
    entry: FileEntry  # name = prepare_outgoing_name(path.name), size, mtime = mtime_ms(st_mtime_ns)


def prepare_sources(paths: Sequence[str | os.PathLike[str]]) -> list[SourceFile]:
    """Kiểm các file sẽ gửi theo thứ tự và dựng `SourceFile`; ném lỗi đầu tiên gặp.

    `paths` là một chuỗi/bytes/`PathLike` đơn lẻ, không phải dãy, hay phần tử không phải
    `str`/`PathLike[str]` → TypeError. Số path ngoài 1…`MAX_OFFER_FILES`, hay hai path chỉ cùng
    một file (cùng `os.path.normcase(abspath)`, hoặc cùng `(st_dev, st_ino)` khi `st_ino` ≠ 0)
    → ValueError. Từng file: không có → `NOT_FOUND`; không phải file thường → `NOT_A_FILE`;
    lớn hơn `MAX_FILE_SIZE` → `TOO_LARGE` (kèm `size`); tên không gửi được → `BAD_NAME`; mở
    đọc thử bị từ chối → `PERMISSION`; OSError khác → `OS_ERROR` (đều là `SourceError`).
    Không giữ handle nào sau khi trả về.
    """
    if isinstance(paths, str | bytes | os.PathLike) or not isinstance(paths, Sequence):
        raise TypeError("paths phải là một dãy đường dẫn, không phải một đường dẫn")
    items = list(paths)
    if not 1 <= len(items) <= MAX_OFFER_FILES:
        raise ValueError(f"số file phải trong 1…{MAX_OFFER_FILES}")
    absolute: list[Path] = []
    names: set[str] = set()
    for item in items:
        if not isinstance(item, str | os.PathLike):
            raise TypeError("mỗi đường dẫn phải là str hoặc os.PathLike[str]")
        raw = os.fspath(item)
        if not isinstance(raw, str):
            raise TypeError("mỗi đường dẫn phải là str hoặc os.PathLike[str]")
        text = os.path.abspath(raw)
        key = os.path.normcase(text)
        if key in names:
            raise ValueError("một file được chọn hai lần")
        names.add(key)
        absolute.append(Path(text))
    identities: set[tuple[int, int]] = set()
    result: list[SourceFile] = []
    for path in absolute:
        st = _stat_source(path)
        if st.st_ino:
            identity = (st.st_dev, st.st_ino)
            if identity in identities:
                raise ValueError("một file được chọn hai lần")
            identities.add(identity)
        result.append(SourceFile(path, _entry_for(path, st)))
    return result


def _stat_source(path: Path) -> os.stat_result:
    """`stat` một file nguồn: phải là file thường, không quá `MAX_FILE_SIZE`."""
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError) as e:
        raise SourceError(path, SourceErrorKind.NOT_FOUND, errno=e.errno) from e
    except PermissionError as e:
        raise SourceError(path, SourceErrorKind.PERMISSION, errno=e.errno) from e
    except OSError as e:
        raise SourceError(path, SourceErrorKind.OS_ERROR, errno=e.errno) from e
    if not stat.S_ISREG(st.st_mode):
        raise SourceError(path, SourceErrorKind.NOT_A_FILE)
    if st.st_size > MAX_FILE_SIZE:
        raise SourceError(path, SourceErrorKind.TOO_LARGE, size=st.st_size)
    return st


def _entry_for(path: Path, st: os.stat_result) -> FileEntry:
    """Tên gửi đi, rồi mở đọc thử (đóng ngay) để báo sớm file không đọc được."""
    try:
        entry = FileEntry(prepare_outgoing_name(path.name), st.st_size, mtime_ms(st.st_mtime_ns))
    except (OutgoingNameError, MessageError) as e:
        raise SourceError(path, SourceErrorKind.BAD_NAME) from e
    try:
        with _open_regular(path):
            pass
    except SourceChanged as e:  # đổi thành thứ khác file thường ngay sau `stat`
        raise SourceError(path, SourceErrorKind.NOT_A_FILE) from e
    except PermissionError as e:
        raise SourceError(path, SourceErrorKind.PERMISSION, errno=e.errno) from e
    except FileNotFoundError as e:
        raise SourceError(path, SourceErrorKind.NOT_FOUND, errno=e.errno) from e
    except OSError as e:
        raise SourceError(path, SourceErrorKind.OS_ERROR, errno=e.errno) from e
    return entry


def check_source(path: Path, leaves: bytearray) -> tuple[int, int, bytes]:
    """Kết quả cho `CheckSource`: `(size, mtime_ms, merkle_root(leaves))` của file nguồn.

    `stat` theo đường dẫn (theo symlink). Không còn là file thường → `SourceChanged`; OSError
    (kể cả không còn file) bay ra nguyên trạng. `leaves` là của engine: chỉ đọc.
    """
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise SourceChanged("file nguồn không còn là file thường")
    return st.st_size, mtime_ms(st.st_mtime_ns), merkle_root(leaves)


def _open_regular(path: Path) -> io.FileIO:
    """Mở `path` để đọc; không phải file thường → `SourceChanged` (không giữ fd). OSError bay ra.

    POSIX mở bằng `O_NONBLOCK` (FIFO không làm chặn), kiểm `fstat`, rồi bỏ `O_NONBLOCK`. Windows mở
    thẳng; mở một thư mục báo PermissionError nên khi mở lỗi thì `stat` lại để phân biệt.
    """
    if sys.platform == "win32":
        try:
            file = io.FileIO(path, "r")
        except OSError as e:
            if not _is_regular_or_missing(path):
                raise SourceChanged("file nguồn không còn là file thường") from e
            raise
        try:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise SourceChanged("file nguồn không còn là file thường")
        except BaseException:
            file.close()
            raise
        return file
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SourceChanged("file nguồn không còn là file thường")
        os.set_blocking(fd, True)
        return io.FileIO(fd, "r", closefd=True)
    except BaseException:
        os.close(fd)
        raise


def _is_regular_or_missing(path: Path) -> bool:
    """False khi `path` còn tồn tại mà không phải file thường; lỗi `stat` → True (giữ lỗi gốc)."""
    try:
        st = os.stat(path)
    except OSError:
        return True
    return stat.S_ISREG(st.st_mode)


class SourceReader:
    """Handle đọc một file nguồn trong lúc một `SendBlocks` chạy. Chỉ `SourceReader.open` tạo.

    Không thread-safe (docstring module, mục Bẫy). Dùng được với `with`.
    """

    def __init__(self, path: Path, entry: FileEntry, file: io.FileIO) -> None:
        self._path = path
        self._entry = entry
        self._file: io.FileIO | None = file
        self._block_count = block_count(entry.size)

    @classmethod
    def open(cls, path: Path, entry: FileEntry) -> SourceReader:
        """Mở `path` để đọc và so với `entry` (mục trong `offer`).

        Không phải file thường (FIFO không làm chặn; Windows: kể cả khi mở bị từ chối vì đó là thư
        mục), hay `size`/`mtime` (ms) khác `entry` → `SourceChanged` (handle đã đóng). OSError khác
        khi mở bay ra nguyên trạng.
        """
        if not isinstance(entry, FileEntry):
            raise TypeError("entry phải là FileEntry")
        file = _open_regular(path)
        try:
            st = os.fstat(file.fileno())
            if st.st_size != entry.size or mtime_ms(st.st_mtime_ns) != entry.mtime:
                raise SourceChanged("file nguồn đã đổi kích thước hay thời gian sửa")
        except BaseException:
            file.close()
            raise
        return cls(path, entry, file)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def entry(self) -> FileEntry:
        return self._entry

    @property
    def closed(self) -> bool:
        return self._file is None

    def read_block(self, index: int) -> tuple[bytearray, bytes]:
        """Đọc khối `index` (`block_length(size, index)` byte tại `index × BLOCK_SIZE`).

        Trả `(payload, sha256(payload))`; payload là `bytearray` mới (bên gọi giữ). Đã đóng hay
        `index` ngoài 0…`block_count` − 1 → ValueError. Đọc thiếu (file bị cắt) → `SourceChanged`.
        OSError bay ra nguyên trạng.
        """
        file = self._file
        if file is None:
            raise ValueError("SourceReader đã đóng")
        if type(index) is not int or not 0 <= index < self._block_count:
            raise ValueError(f"index phải là số nguyên 0…{self._block_count - 1}")
        length = block_length(self._entry.size, index)
        payload = bytearray(length)
        file.seek(index * BLOCK_SIZE)
        got = 0
        with memoryview(payload) as view:
            while got < length:
                n = file.readinto(view[got:])
                if not n:
                    break
                got += n
        if got < length:
            raise SourceChanged("file nguồn ngắn hơn manifest")
        return payload, hashlib.sha256(payload).digest()

    def close(self) -> None:
        """Đóng handle. Gọi lại được."""
        file, self._file = self._file, None
        if file is not None:
            file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
