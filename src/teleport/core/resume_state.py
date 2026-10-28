"""Trạng thái bền của bên nhận (sans-IO): nhật ký `.state`, `transfer.json`, `tombstones.json`.

Định dạng byte và luật đọc lại ở docs/architecture.md mục 4 (nguồn duy nhất); quy tắc bền
vững (thứ tự `fsync`, khi nào một khối được tính là đã có) ở docs/protocol.md mục 4.3, 4.4.
Module chỉ mã hóa và giải mã bytes: không mở file, không xem đồng hồ. Đọc/ghi file, `fsync`,
cắt file và đổi giờ tường ra tuổi là việc của `app`.

- `.state`: `encode_header` / `check_header`; một hàm cho mỗi loại bản ghi (`encode_mark`,
  `encode_clear`, `encode_clear_all`, `encode_target`, `encode_reserved`); `StateReplay` đọc
  lại cả file theo từng đoạn (header rồi các bản ghi), dừng sạch ở bản ghi hỏng đầu tiên và
  báo chỗ cắt (`ReplayResult.good_end`); trần độ dài `max_state_length`; gộp
  (`needs_compaction`, `compact_records`, độ dài bản gọn `compact_length_for`); bảng “Phục hồi
  tên đích” (`recover_target`).
- `transfer.json`: `TransferRecord`, `encode_transfer_record`, `decode_transfer_record`.
- `tombstones.json`: `TombstoneRecord`, `encode_tombstones`, `decode_tombstones`.

Mọi lỗi, cả tham số sai lẫn dữ liệu hỏng, là `StateError` (lớp con của ValueError). File trên
đĩa của chính máy nhận vẫn được kiểm như dữ liệu không tin cậy: chương trình khác cũng ghi được
vào thư mục nhận và thư mục cấu hình.

Bất biến:

- Đọc lại không bao giờ bật bit của khối không có `MARK` hợp lệ trong phần đã đọc; dữ liệu hỏng
  chỉ làm mất bản ghi (từ chỗ hỏng trở đi), nên chỉ có thể làm mất bit đã bật bởi các `MARK`
  sau chỗ hỏng. Ngoại lệ đã chấp nhận: mất một `CLEAR` / `CLEAR_ALL` thì khối đó vẫn bật, và
  lượt đọc lại cuối bắt lại (architecture mục 4).
- Tên trong `TARGET` / `RESERVED` luôn đi qua `sanitize_filename` không đổi, cả khi ghi lẫn khi
  đọc lại.
- Hash của khối có bit = 0 trong `ReplayResult.leaves` luôn là 32 byte 0, nên đọc lại bản gọn
  cho đúng kết quả như đọc lại nhật ký gốc.

Bẫy: `StateReplay` cần **cả** file, kể cả 41 byte header, vì mọi vị trí (`good_end`,
`log_length`, `compact_length`, `max_length`) tính từ byte đầu file. `check_header` vẫn phải gọi
riêng: replay chỉ kiểm cấu trúc header, không biết `transfer_id`, `file_index`, `size` đúng.
"""

from __future__ import annotations

import base64
import datetime
import json
import re
import struct
import zlib
from array import array
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum

from teleport.core.bitfield import Bitfield
from teleport.core.blocks import BLOCK_SIZE, DIGEST_SIZE, MAX_BLOCKS, MAX_FILE_SIZE
from teleport.core.filenames import MAX_NAME_BYTES, sanitize_filename
from teleport.core.messages import (
    MAX_OFFER_FILES,
    Cancel,
    MessageError,
    Offer,
    validate_device_id,
    validate_transfer_id,
)

STATE_MAGIC = b"TPRS"
STATE_VERSION = 1
_HEADER_BODY = struct.Struct("!4sB16sIQI")  # magic, version, transfer_id, file_index, size, block_size
_U32 = struct.Struct("!I")
_RECORD_HEAD = struct.Struct("!BH")  # [u8 loại][u16 n hay len]
HEADER_SIZE = _HEADER_BODY.size + _U32.size  # 41 byte

# Mã loại bản ghi (architecture mục 4).
RECORD_MARK = 0x01
RECORD_CLEAR = 0x02
RECORD_CLEAR_ALL = 0x03
RECORD_TARGET = 0x04
RECORD_RESERVED = 0x05

_MARK_ENTRY = struct.Struct("!I32s")  # 36 byte mỗi khối
_RECORD_OVERHEAD = _RECORD_HEAD.size + _U32.size  # 7 byte: loại, n/len, crc32
_CLEAR_ALL_LENGTH = 1 + _U32.size  # 5 byte
_CLEAR_ALL_RECORD = bytes([RECORD_CLEAR_ALL]) + _U32.pack(zlib.crc32(bytes([RECORD_CLEAR_ALL])))
# Các loại có trường `n` / `len`.
_SIZED_RECORDS = frozenset({RECORD_MARK, RECORD_CLEAR, RECORD_TARGET, RECORD_RESERVED})

MAX_RECORD_BLOCKS = 0xFFFF  # `n` của `MARK` / `CLEAR` là u16
MAX_RECORD_LENGTH = _RECORD_OVERHEAD + MAX_RECORD_BLOCKS * _MARK_ENTRY.size  # `MARK` lớn nhất
MAX_TARGET_NAME_BYTES = MAX_NAME_BYTES  # `len` của `TARGET` / `RESERVED`: protocol mục 3.3
COMPACT_MARK_BLOCKS = 4096  # số khối tối đa mỗi `MARK` của bản gọn
COMPACT_SLACK = 64 * 1024  # gộp khi `.state` dài hơn 2 × kích thước gọn + 64 KiB
# Số bản ghi `TARGET` + `RESERVED` tối đa mỗi `.state`; bản thứ `MAX_NAME_RECORDS + 1` là bản ghi
# hỏng. Nhật ký hợp lệ chỉ có vài bản (mỗi lần đổi tên thêm 2, bản gọn có tối đa 2); trần chặn
# việc dựng `.state` toàn tên để bắt bộ đọc chạy bộ làm sạch tên hàng triệu lần.
MAX_NAME_RECORDS = 1024

MAX_TRANSFER_RECORD = 2 * 1024 * 1024  # trần `transfer.json`, byte
MAX_TOMBSTONE_FILE = 8 * 1024 * 1024  # trần `tombstones.json`, byte
RECORD_VERSION = 1  # `version` của `transfer.json` và `tombstones.json`

_ZERO_LEAF = bytes(DIGEST_SIZE)
_ZERO_CHUNK = bytes(1 << 20)  # xóa `leaves` theo đoạn, không cấp thêm một bản cùng cỡ
# Dạng giờ trên đĩa: UTC, đúng `YYYY-MM-DDTHH:MM:SSZ`. Lớp ký tự ASCII, không dùng `\d`.
_TIME_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z")
_DIRECTIONS = frozenset({"send", "receive"})  # giá trị của `core.transfer.Direction`


class StateError(ValueError):
    """Tham số sai, hoặc dữ liệu `.state` / `transfer.json` / `tombstones.json` hỏng hay vượt trần."""


def _byte_len(n: int) -> int:
    return -(-n // 8)


def _check_int(value: object, field: str, lo: int, hi: int) -> int:
    # bool là lớp con của int: bị từ chối như mọi kiểu khác.
    if type(value) is not int or not lo <= value <= hi:
        raise StateError(f"{field} phải là số nguyên {lo}…{hi}")
    return value


def _check_n_blocks(n_blocks: object) -> int:
    return _check_int(n_blocks, "n_blocks", 0, MAX_BLOCKS)


def _check_index(index: object) -> int:
    return _check_int(index, "index", 0, MAX_BLOCKS - 1)


def _check_digest(digest: object) -> bytes:
    if not isinstance(digest, bytes | bytearray | memoryview):
        raise StateError(f"sha256 phải là {DIGEST_SIZE} byte")
    raw = bytes(digest)
    if len(raw) != DIGEST_SIZE:
        raise StateError(f"sha256 phải là đúng {DIGEST_SIZE} byte")
    return raw


def _check_name(name: object) -> bytes:
    """Tên đích hợp lệ cho `TARGET` / `RESERVED`: 1…255 byte UTF-8, ổn định qua bộ làm sạch."""
    if not isinstance(name, str):
        raise StateError("tên đích phải là chuỗi")
    try:
        raw = name.encode("utf-8")
    except UnicodeEncodeError:
        raise StateError("tên đích không mã hóa được UTF-8") from None
    if not 1 <= len(raw) <= MAX_TARGET_NAME_BYTES:
        raise StateError(f"tên đích phải dài 1…{MAX_TARGET_NAME_BYTES} byte UTF-8")
    if sanitize_filename(name) != name:
        raise StateError("tên đích đổi đi khi qua bộ làm sạch tên file")
    return raw


def _record(kind: int, count: int, body: bytes | bytearray) -> bytes:
    head = _RECORD_HEAD.pack(kind, count) + body
    return head + _U32.pack(zlib.crc32(head))


# ---- Header --------------------------------------------------------------------------------


def encode_header(transfer_id: str, file_index: int, size: int, block_size: int = BLOCK_SIZE) -> bytes:
    """Header 41 byte của `.state` cho file `file_index` (`size` byte) của lượt `transfer_id`.

    `transfer_id` không phải 32 hex thường, `file_index` ngoài 0…999, `size` ngoài
    0…`MAX_FILE_SIZE`, `block_size` ngoài 1…2³² − 1 → StateError.
    """
    try:
        validate_transfer_id(transfer_id)
    except MessageError as e:
        raise StateError(str(e)) from None
    _check_int(file_index, "file_index", 0, MAX_OFFER_FILES - 1)
    _check_int(size, "size", 0, MAX_FILE_SIZE)
    _check_int(block_size, "block_size", 1, 0xFFFFFFFF)
    body = _HEADER_BODY.pack(
        STATE_MAGIC, STATE_VERSION, bytes.fromhex(transfer_id), file_index, size, block_size
    )
    return body + _U32.pack(zlib.crc32(body))


def check_header(
    data: bytes | bytearray | memoryview, *, transfer_id: str, file_index: int, size: int, block_size: int
) -> bool:
    """`data` có mở đầu bằng đúng header của file này không (so với `transfer.json`).

    Chỉ xét `HEADER_SIZE` byte đầu của `data`, nên đưa cả đầu file vào cũng được. Thiếu byte,
    sai `magic`, `version` lạ, sai `crc32` hay một trường khác → False: coi như chưa có trạng
    thái (architecture mục 4). Tham số sai → StateError; `data` không phải bytes → StateError.
    """
    expected = encode_header(transfer_id, file_index, size, block_size)
    if not isinstance(data, bytes | bytearray | memoryview):
        raise StateError("data phải là bytes")
    try:
        head = bytes(memoryview(data).cast("B")[:HEADER_SIZE])
    except TypeError:  # memoryview không liền mạch
        raise StateError("data phải là bytes liền mạch") from None
    return head == expected


# ---- Bản ghi -------------------------------------------------------------------------------


def encode_mark(blocks: Iterable[tuple[int, bytes]]) -> bytes:
    """Bản ghi `MARK` cho các khối `(index, sha256)` vừa bền trong `.part`.

    1 ≤ số khối ≤ 65 535; `index` 0…`MAX_BLOCKS` − 1 (bộ đọc còn so với số khối của file);
    `sha256` đúng 32 byte; index trùng nhau được phép (bản ghi sau thắng khi đọc lại). Sai →
    StateError. `MARK` 16 khối dài 583 byte.
    """
    body = bytearray()
    count = 0
    try:
        for index, digest in blocks:
            count += 1
            if count > MAX_RECORD_BLOCKS:
                raise StateError(f"MARK chỉ chứa tối đa {MAX_RECORD_BLOCKS} khối")
            body += _MARK_ENTRY.pack(_check_index(index), _check_digest(digest))
    except StateError:
        raise
    except (TypeError, ValueError):  # không lặp được, hay phần tử không phải cặp
        raise StateError("blocks phải là các cặp (index, sha256)") from None
    if count == 0:
        raise StateError("MARK phải có ít nhất 1 khối")
    return _record(RECORD_MARK, count, body)


def encode_clear(indices: Iterable[int]) -> bytes:
    """Bản ghi `CLEAR`: tắt bit các khối `indices` (1…65 535 chỉ số 0…`MAX_BLOCKS` − 1). Sai → StateError."""
    body = bytearray()
    count = 0
    try:
        for index in indices:
            count += 1
            if count > MAX_RECORD_BLOCKS:
                raise StateError(f"CLEAR chỉ chứa tối đa {MAX_RECORD_BLOCKS} khối")
            body += _U32.pack(_check_index(index))
    except TypeError:
        raise StateError("indices phải là các số nguyên") from None
    if count == 0:
        raise StateError("CLEAR phải có ít nhất 1 khối")
    return _record(RECORD_CLEAR, count, body)


def encode_clear_all() -> bytes:
    """Bản ghi `CLEAR_ALL` (5 byte): tắt mọi bit."""
    return _CLEAR_ALL_RECORD


def encode_target(name: str) -> bytes:
    """Bản ghi `TARGET`: đã chọn tên đích `name` (chưa chứng minh file mang tên đó là của mình).

    `name` phải dài 1…255 byte UTF-8 và đi qua `sanitize_filename` không đổi; sai → StateError.
    """
    raw = _check_name(name)
    return _record(RECORD_TARGET, len(raw), raw)


def encode_reserved(name: str) -> bytes:
    """Bản ghi `RESERVED`: `O_CREAT | O_EXCL` đã tạo được file giữ chỗ `name`.

    Luật tên như `encode_target`.
    """
    raw = _check_name(name)
    return _record(RECORD_RESERVED, len(raw), raw)


# ---- Trần độ dài và gộp --------------------------------------------------------------------


def _marks_length(count: int, max_mark: int) -> int:
    """Tổng độ dài các `MARK` chứa `count` khối, mỗi `MARK` tối đa `max_mark` khối."""
    return count * _MARK_ENTRY.size + -(-count // max_mark) * _RECORD_OVERHEAD


def _name_record_length(name_bytes: int) -> int:
    return _RECORD_OVERHEAD + name_bytes


def max_state_length(n_blocks: int) -> int:
    """Trần đọc `.state` của file `n_blocks` khối; phần vượt trần là rác (architecture mục 4).

    = 2 × kích thước gọn lớn nhất (header, `TARGET` và `RESERVED` tên dài nhất, `MARK` tối đa
    `COMPACT_MARK_BLOCKS` khối của mọi khối) + `COMPACT_SLACK` + một bản ghi lớn nhất
    (`MAX_RECORD_LENGTH`): nhật ký được gộp ngay khi vượt 2 × gọn + 64 KiB nên không bao giờ dài
    hơn thế quá một bản ghi. `n_blocks` ngoài 0…`MAX_BLOCKS` → StateError.
    """
    n = _check_n_blocks(n_blocks)
    largest_compact = (
        HEADER_SIZE + 2 * _name_record_length(MAX_TARGET_NAME_BYTES) + _marks_length(n, COMPACT_MARK_BLOCKS)
    )
    return 2 * largest_compact + COMPACT_SLACK + MAX_RECORD_LENGTH


def needs_compaction(log_length: int, compact_length: int) -> bool:
    """`.state` dài `log_length` byte có phải ghi lại bản gọn (`compact_length` byte) không.

    True khi `log_length` > 2 × `compact_length` + `COMPACT_SLACK`. Sau khi đọc lại thì dùng độ
    dài sau khi cắt (`ReplayResult.good_end`). Tham số không phải số nguyên ≥ 0 → StateError.
    """
    _check_int(log_length, "log_length", 0, 1 << 63)
    _check_int(compact_length, "compact_length", 0, 1 << 63)
    return log_length > 2 * compact_length + COMPACT_SLACK


def _compact_length(count: int, target: bytes | None, reserved: bool) -> int:
    length = HEADER_SIZE + _marks_length(count, COMPACT_MARK_BLOCKS)
    if target is not None:
        length += _name_record_length(len(target))
        if reserved:
            length += _name_record_length(len(target))
    return length


def compact_length_for(marked: int, target: str | None, reserved: str | None) -> int:
    """Độ dài bản gọn (kể cả header): `marked` khối bật, `TARGET` cuối `target`, `RESERVED` `reserved`.

    Cùng công thức với `ReplayResult.compact_length` và độ dài thật của header + `compact_records`
    (mỗi `MARK` tối đa `COMPACT_MARK_BLOCKS` khối). `RESERVED` chỉ được tính khi trùng tên
    `target`, như khi đọc lại. Bên ghi nhật ký dùng hàm này để biết khi nào gộp mà không đọc lại
    file. `marked` ngoài 0…`MAX_BLOCKS`, tên sai luật `encode_target` → StateError.
    """
    count = _check_int(marked, "marked", 0, MAX_BLOCKS)
    raw = None if target is None else _check_name(target)
    if reserved is not None:
        _check_name(reserved)
    return _compact_length(count, raw, raw is not None and reserved == target)


def compact_records(
    bits: Bitfield,
    leaves: bytes | bytearray,
    target: str | None,
    reserved: bool,
    *,
    max_mark: int = COMPACT_MARK_BLOCKS,
) -> Iterator[bytes]:
    """Các bản ghi của bản gọn, theo thứ tự ghi; đặt sau header (`encode_header`) để có `.state` gọn.

    `TARGET` (nếu có) trước, rồi `RESERVED` (chỉ khi `reserved`), rồi các `MARK` chứa đúng các
    khối có bit bật theo thứ tự tăng dần, mỗi `MARK` tối đa `max_mark` khối, hash lấy từ
    `leaves`. Mỗi phần tử là một bản ghi, để `app` ghi theo đoạn mà không dựng cả bản gọn trong
    RAM. Đọc lại header + các bản ghi này cho đúng `bits`, `leaves` (khối bit 0 có hash 0),
    `target`, `reserved` như nhật ký gốc; với `max_mark` mặc định, độ dài tổng (kể cả header)
    bằng `ReplayResult.compact_length`.

    Tham số được kiểm ngay lúc gọi (không chờ lần lặp đầu): `bits` không phải Bitfield,
    `leaves` khác 32 × số khối byte, tên sai luật `encode_target`, `reserved` mà không có
    `target`, `max_mark` ngoài 1…65 535 → StateError. Không được sửa `bits`, `leaves` trong lúc
    còn đang lặp.
    """
    if not isinstance(bits, Bitfield):
        raise StateError("bits phải là Bitfield")
    if not isinstance(leaves, bytes | bytearray) or len(leaves) != DIGEST_SIZE * len(bits):
        raise StateError(f"leaves phải là {DIGEST_SIZE} × số khối byte")
    if type(reserved) is not bool:
        raise StateError("reserved phải là bool")
    if target is not None:
        _check_name(target)
    elif reserved:
        raise StateError("RESERVED cần có TARGET")
    limit = _check_int(max_mark, "max_mark", 1, MAX_RECORD_BLOCKS)
    return _iter_compact(bits, leaves, target, reserved, limit)


def _iter_compact(
    bits: Bitfield, leaves: bytes | bytearray, target: str | None, reserved: bool, max_mark: int
) -> Iterator[bytes]:
    if target is not None:
        yield encode_target(target)
        if reserved:
            yield encode_reserved(target)
    body = bytearray()
    count = 0
    pack_index = _U32.pack
    with memoryview(leaves) as view:
        for byte_index, byte in enumerate(bits.to_bytes()):
            if not byte:
                continue
            base = byte_index * 8
            for bit in range(8):
                if not byte & (0x80 >> bit):
                    continue
                i = base + bit
                body += pack_index(i)
                body += view[i * DIGEST_SIZE : (i + 1) * DIGEST_SIZE]
                count += 1
                if count == max_mark:
                    yield _record(RECORD_MARK, count, body)
                    body = bytearray()
                    count = 0
    if count:
        yield _record(RECORD_MARK, count, body)


# ---- Đọc lại -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """Kết quả đọc lại `.state` (`StateReplay.finish`). Mọi độ dài, vị trí tính từ byte đầu file.

    - `bits`, `leaves`: khối đã có và hash từng khối (32 × số khối byte; khối bit 0 có hash 0).
    - `target`: tên của `TARGET` cuối cùng (None nếu không có); `reserved`: có `RESERVED` trùng
      tên đó, đứng sau nó.
    - `good_end`: cuối bản ghi tốt cuối cùng (≥ `HEADER_SIZE`); `truncated`: sau đó còn byte
      (bản ghi hỏng, cụt hay vượt `max_length`), `app` phải cắt file về `good_end` rồi `fsync`
      trước khi nối thêm.
    - `log_length`: số byte đã đưa vào `feed` (≥ `good_end`).
    - `compact_length`: độ dài bản gọn của trạng thái này, kể cả header (`compact_records`).
    - `name_records`: số bản ghi `TARGET` + `RESERVED` đã áp (đều nằm trước `good_end`). Bên ghi
      phải gộp (bản gọn có ≤ 2 bản ghi tên) trước khi nối `TARGET`/`RESERVED` khi số này, cộng các
      bản ghi tên đã nối từ lần đọc lại, ≥ `MAX_NAME_RECORDS` − 2. Lý do: vượt trần thì bộ đọc dừng
      ở bản thứ `MAX_NAME_RECORDS` + 1, cặp `TARGET X` + `RESERVED X` cũ trước đó có thể được coi
      là của mình và `REUSE_PLACEHOLDER` ghi đè file 0 byte của người khác mang tên `X`.
    """

    bits: Bitfield
    leaves: bytearray
    target: str | None
    reserved: bool
    good_end: int
    truncated: bool
    log_length: int
    compact_length: int
    name_records: int


class StateReplay:
    """Đọc lại một `.state` từ byte đầu (cả header) theo từng đoạn, không nạp cả file vào RAM.

    Gọi `feed` với các đoạn liên tiếp của file, rồi `finish`. RAM ngoài bitfield và hash chỉ là
    một bản ghi dở (≤ `MAX_RECORD_LENGTH`) cộng một đoạn.

    - Header: chỉ kiểm cấu trúc (`magic`, `version`, `crc32`, số khối theo `size` và
      `block_size` bằng `n_blocks`); sai → StateError, `app` coi như chưa có trạng thái. Đối
      chiếu với `transfer.json` là `check_header`.
    - Bản ghi: phát lần lượt theo luật ở architecture mục 4. Bản ghi đầu tiên bị cụt, sai
      `crc32`, có loại lạ, `n` = 0, index ≥ `n_blocks`, `len` ngoài 1…255, tên không phải UTF-8
      hay đổi đi khi qua bộ làm sạch → dừng sạch ở đó, không ném: phần còn lại bị bỏ.
    - Byte thứ `max_length` trở đi là rác (thường `max_state_length(n_blocks)`): dừng như gặp
      bản ghi hỏng.

    - Bản ghi `TARGET`/`RESERVED` thứ `MAX_NAME_RECORDS` + 1 là bản ghi hỏng.

    Chi phí CPU tuyến tính theo số byte đã đọc (≤ `max_length`), kể cả với `.state` do chương
    trình khác dựng: `CLEAR_ALL` chỉ tắt các khối đã bật từ lần `CLEAR_ALL` trước, tên đã kiểm
    được nhớ lại. Bản ghi tí hon đắt nhất mỗi byte. Đo trên máy phát triển (Windows, 2026-10,
    máy đang tải): nhật ký hợp lệ (`MARK` 16 khối) khoảng 45 MiB/s; xấu nhất (`CLEAR` 1 khối,
    hay `MARK` 1 khối xen `CLEAR_ALL`) khoảng 5–8 MiB/s tùy tải, tức đọc trọn `max_state_length`
    của file 4 TiB (≈ 290 MiB) mất cỡ 35–60 s, của file 100 GiB (≈ 9 MiB) vài giây. Hạn của mỗi
    lượt khôi phục ở `app` phải tính theo `max_state_length(n_blocks)` và chừa biên rộng hơn các
    số này (máy chậm hơn), và việc đọc chạy trên thread đĩa,
    không trên event loop. Đưa vào `feed` các đoạn lớn (cỡ 1 MiB): mỗi lời gọi có chi phí cố định.

    `n_blocks` ngoài 0…`MAX_BLOCKS` hay `max_length` < `HEADER_SIZE` → StateError; `feed` sau
    `finish` → RuntimeError.
    """

    __slots__ = (
        "_bits",
        "_buf",
        "_buf_start",
        "_dirty",
        "_dirty_limit",
        "_error",
        "_fed",
        "_good_end",
        "_header_ok",
        "_leaves",
        "_max_length",
        "_n",
        "_name_records",
        "_names",
        "_reserved",
        "_result",
        "_stopped",
        "_target",
        "_target_raw",
    )

    def __init__(self, n_blocks: int, *, max_length: int) -> None:
        self._n = _check_n_blocks(n_blocks)
        self._max_length = _check_int(max_length, "max_length", HEADER_SIZE, 1 << 62)
        self._bits = bytearray(_byte_len(self._n))
        self._leaves = bytearray(DIGEST_SIZE * self._n)
        self._target: str | None = None
        self._target_raw: bytes | None = None
        self._reserved = False
        # Các index đã chuyển bit 0 → 1 từ lần `CLEAR_ALL` trước (mỗi index đã được trả bằng ≥ 36
        # byte `MARK`), để `CLEAR_ALL` chỉ tắt đúng các khối đó thay vì xóa cả mảng mỗi lần: nhật
        # ký toàn `CLEAR_ALL` 5 byte không được tốn O(n) mỗi bản ghi. Vượt `_dirty_limit` (n / 8,
        # chặn RAM ở ≤ n / 2 byte) thì thành None: lần `CLEAR_ALL` sau xóa cả mảng, mà chi phí đó
        # đã được trả bằng hơn n / 8 × 36 byte `MARK` trước nó.
        self._dirty: array[int] | None = array("I")
        self._dirty_limit = self._n // 8
        self._name_records = 0
        self._names: dict[bytes, str] = {}  # tên đã kiểm, ≤ `MAX_NAME_RECORDS` mục
        self._buf = bytearray()  # byte chưa xử lý, bắt đầu ở vị trí `_buf_start` của file
        self._buf_start = 0
        self._fed = 0
        self._good_end = 0
        self._header_ok = False
        self._stopped = False
        self._error: str | None = None
        self._result: ReplayResult | None = None

    def feed(self, chunk: bytes | bytearray | memoryview) -> bool:
        """Đưa đoạn kế tiếp của file vào. True: còn đọc tiếp; False: đã dừng, khỏi đọc phần còn lại.

        Đoạn đưa vào sau khi đã dừng chỉ được đếm vào `log_length`. Header hỏng → StateError (và
        mọi lời gọi sau cũng thế); `chunk` không phải bytes → StateError.
        """
        if self._result is not None:
            raise RuntimeError("đã gọi finish")
        if self._error is not None:
            raise StateError(self._error)
        if not isinstance(chunk, bytes | bytearray | memoryview):
            raise StateError("chunk phải là bytes")
        try:
            view = memoryview(chunk).cast("B")
        except TypeError:  # memoryview không liền mạch
            raise StateError("chunk phải là bytes liền mạch") from None
        with view:
            self._fed += len(view)
            if self._stopped:
                return False
            room = self._max_length - (self._buf_start + len(self._buf))
            over = len(view) > room
            self._buf += view[:room] if over else view
        self._parse()
        if over:
            self._stop()
        return not self._stopped

    def finish(self) -> ReplayResult:
        """Kết quả đọc lại; gọi lại trả đúng kết quả đó. Chưa đủ 41 byte header → StateError."""
        if self._result is not None:
            return self._result
        if self._error is not None:
            raise StateError(self._error)
        if not self._header_ok:
            self._error = "thiếu byte header"
            raise StateError(self._error)
        bits = Bitfield.from_base64(base64.b64encode(self._bits).decode("ascii"), self._n)
        count = sum(b.bit_count() for b in self._bits)
        self._result = ReplayResult(
            bits=bits,
            leaves=self._leaves,
            target=self._target,
            reserved=self._reserved,
            good_end=self._good_end,
            truncated=self._fed > self._good_end,
            log_length=self._fed,
            compact_length=_compact_length(count, self._target_raw, self._reserved),
            name_records=self._name_records,
        )
        self._buf = bytearray()
        return self._result

    def _stop(self) -> None:
        self._stopped = True
        self._buf = bytearray()  # phần còn lại là rác: không giữ trong RAM

    def _fail(self, message: str) -> None:
        self._error = message
        self._buf = bytearray()
        raise StateError(message)

    def _check_header(self, head: bytes) -> None:
        body, crc = head[: _HEADER_BODY.size], head[_HEADER_BODY.size :]
        magic, version, _tid, _index, size, block_size = _HEADER_BODY.unpack(body)
        if magic != STATE_MAGIC:
            self._fail("sai magic của .state")
        if version != STATE_VERSION:
            self._fail(f"version .state lạ: {version}")
        if _U32.unpack(crc)[0] != zlib.crc32(body):
            self._fail("sai crc32 của header .state")
        if block_size == 0 or size > MAX_FILE_SIZE or -(-size // block_size) != self._n:
            self._fail("header .state không khớp số khối của file")

    def _parse(self) -> None:
        buf = self._buf
        pos = 0
        if not self._header_ok:
            if len(buf) < HEADER_SIZE:
                return
            self._check_header(bytes(buf[:HEADER_SIZE]))
            self._header_ok = True
            pos = HEADER_SIZE
            self._good_end = self._buf_start + pos
        # Vòng lặp nóng: bản ghi tí hon (`CLEAR_ALL` 5 byte, `MARK` 1 khối 43 byte) đắt nhất mỗi
        # byte, nên đọc thẳng `bytearray` bằng chỉ số và `unpack_from`, ít cắt memoryview.
        end = len(buf)
        crc32 = zlib.crc32
        read_u32 = _U32.unpack_from
        with memoryview(buf) as view:
            while pos < end:
                kind = buf[pos]
                if kind == RECORD_CLEAR_ALL:
                    # Chỉ có một dạng đúng (kể cả CRC): so nguyên 5 byte, khỏi tính CRC.
                    if end - pos < _CLEAR_ALL_LENGTH:
                        break  # cần thêm byte
                    if buf[pos : pos + _CLEAR_ALL_LENGTH] != _CLEAR_ALL_RECORD:
                        self._stopped = True
                        break
                    if self._dirty is None or self._dirty:
                        self._clear_all()
                    pos += _CLEAR_ALL_LENGTH
                    continue
                if kind in _SIZED_RECORDS:
                    if end - pos < _RECORD_HEAD.size:
                        break  # cần thêm byte
                    count = buf[pos + 1] << 8 | buf[pos + 2]
                    if kind == RECORD_MARK:
                        length = _RECORD_OVERHEAD + count * _MARK_ENTRY.size
                    elif kind == RECORD_CLEAR:
                        length = _RECORD_OVERHEAD + count * _U32.size
                    elif count <= MAX_TARGET_NAME_BYTES:
                        length = _RECORD_OVERHEAD + count
                    else:
                        count = 0  # `len` của tên ngoài 1…255: hỏng như `n` = 0
                    if count == 0:
                        self._stopped = True
                        break
                else:
                    self._stopped = True  # loại lạ
                    break
                if end - pos < length:
                    break  # cần thêm byte
                crc_at = pos + length - _U32.size
                if crc32(view[pos:crc_at]) != read_u32(buf, crc_at)[0] or not self._apply(
                    kind, buf, view, pos + _RECORD_HEAD.size, crc_at
                ):
                    self._stopped = True
                    break
                pos += length
        self._good_end = self._buf_start + pos
        if self._stopped:
            self._stop()
            return
        del buf[:pos]  # một lần mỗi `feed`, không xóa đầu bộ đệm sau từng bản ghi
        self._buf_start += pos

    def _apply(self, kind: int, buf: bytearray, view: memoryview, start: int, stop: int) -> bool:
        """Phát bản ghi `kind` có thân `buf[start:stop]`, CRC đã đúng. False: hỏng, trạng thái không đổi."""
        n = self._n
        bits, leaves = self._bits, self._leaves
        if kind == RECORD_MARK:
            entries: list[tuple[int, bytes]]
            if stop - start == _MARK_ENTRY.size:  # `MARK` 1 khối: khỏi dựng bộ lặp
                entries = [_MARK_ENTRY.unpack_from(buf, start)]
            else:
                entries = list(_MARK_ENTRY.iter_unpack(view[start:stop]))
            if max(entries)[0] >= n:  # so tuple theo index trước: max chạy trong C
                return False
            dirty = self._dirty
            for i, digest in entries:
                mask = 0x80 >> (i & 7)
                if not bits[i >> 3] & mask:
                    bits[i >> 3] |= mask
                    if dirty is not None:
                        dirty.append(i)
                        if len(dirty) > self._dirty_limit:
                            self._dirty = dirty = None
                leaves[i * DIGEST_SIZE : (i + 1) * DIGEST_SIZE] = digest
        elif kind == RECORD_CLEAR:
            indices: list[int]
            if stop - start == _U32.size:
                indices = [_U32.unpack_from(buf, start)[0]]
            else:
                indices = [i for (i,) in _U32.iter_unpack(view[start:stop])]
            if max(indices) >= n:
                return False
            for i in indices:
                mask = 0x80 >> (i & 7)
                if bits[i >> 3] & mask:
                    bits[i >> 3] &= ~mask & 0xFF
                    leaves[i * DIGEST_SIZE : (i + 1) * DIGEST_SIZE] = _ZERO_LEAF
        else:  # TARGET, RESERVED (`CLEAR_ALL` xử lý ngay trong `_parse`)
            if self._name_records >= MAX_NAME_RECORDS:
                return False
            raw = bytes(view[start:stop])
            name = self._names.get(raw)
            if name is None:
                try:
                    name = raw.decode("utf-8")
                except UnicodeDecodeError:
                    return False
                if sanitize_filename(name) != name:
                    return False
                self._names[raw] = name
            self._name_records += 1
            if kind == RECORD_TARGET:
                self._target, self._target_raw, self._reserved = name, raw, False
            elif name == self._target:
                self._reserved = True
        return True

    def _clear_all(self) -> None:
        """Tắt mọi bit: chỉ các khối trong `_dirty`, hoặc cả mảng khi `_dirty` đã tràn."""
        bits, leaves, dirty = self._bits, self._leaves, self._dirty
        if dirty is None:
            bits[:] = bytes(len(bits))
            for start in range(0, len(leaves), len(_ZERO_CHUNK)):
                end = min(start + len(_ZERO_CHUNK), len(leaves))
                leaves[start:end] = _ZERO_CHUNK[: end - start]
        else:
            # Khối đã bị `CLEAR` sau khi bật vẫn còn trong `_dirty`: tắt lại là vô hại.
            for i in dirty:
                bits[i >> 3] &= ~(0x80 >> (i & 7)) & 0xFF
                leaves[i * DIGEST_SIZE : (i + 1) * DIGEST_SIZE] = _ZERO_LEAF
        self._dirty = array("I")


# ---- Phục hồi tên đích ---------------------------------------------------------------------


class TargetRecovery(StrEnum):
    """Dòng khớp của bảng “Phục hồi tên đích” (architecture mục 4): `app` làm gì với tên đích."""

    # Dòng 1, 2 và dòng cuối khi còn `.part`: không đụng file mang tên đích (nếu có); tới bước
    # đổi tên thì chọn tên như bình thường. Các khối giữ nguyên, trừ khối không nằm trọn trong
    # `.part` mà `app` luôn tắt bit; dòng 1, 2 mà không còn `.part` thì `app` tạo lại `.part`
    # và mọi bit tắt theo đúng luật đó.
    NEW_NAME = "new_name"
    # Đã sập sau `os.replace`: đọc lại file đích theo hash đã lưu; khớp hết → làm tiếp từ bước 6.
    VERIFY_TARGET = "verify_target"
    # File đích 0 byte là file giữ chỗ của mình, còn `.part`: đọc lại `.part`, rồi làm tiếp từ bước 5.
    REUSE_PLACEHOLDER = "reuse_placeholder"
    # File đích 0 byte của mình nhưng mất `.part`: ghi lại header, bitfield toàn 0, nhận xong thì
    # dùng lại file đó làm file giữ chỗ (nối lại `TARGET` + `RESERVED` sau header mới).
    RESTART_REUSE = "restart_reuse"
    # Dòng cuối khi mất `.part`: không đụng file mang tên đích, ghi lại header, bitfield toàn 0,
    # tới bước đổi tên thì chọn tên mới.
    RESTART = "restart"


def recover_target(
    *, target: str | None, reserved: bool, target_size: int | None, part_exists: bool, size: int
) -> TargetRecovery:
    """Dòng đầu tiên khớp của bảng “Phục hồi tên đích” (architecture mục 4), không I/O.

    `target`, `reserved` lấy từ `ReplayResult`; `target_size`: kích thước file thường mang tên
    đích trong thư mục nhận, None nếu không có (kể cả khi tên đó là thư mục hay liên kết);
    `part_exists`: còn `.part`; `size`: kích thước file theo `transfer.json`. Chỉ gọi khi
    `transfer.json` chưa ghi file là `verified`. Tham số sai kiểu hay giá trị → StateError.
    """
    if target is not None and not isinstance(target, str):
        raise StateError("target phải là chuỗi hoặc None")
    if type(reserved) is not bool or type(part_exists) is not bool:
        raise StateError("reserved, part_exists phải là bool")
    if target_size is not None:
        _check_int(target_size, "target_size", 0, 1 << 63)
    _check_int(size, "size", 0, MAX_FILE_SIZE)
    if target is None or not reserved:
        return TargetRecovery.NEW_NAME
    # Dòng “đúng `size`, không còn `.part`” đứng trước dòng “0 byte”: file rỗng có `size` = 0.
    if target_size == size and not part_exists:
        return TargetRecovery.VERIFY_TARGET
    if target_size == 0:
        return TargetRecovery.REUSE_PLACEHOLDER if part_exists else TargetRecovery.RESTART_REUSE
    return TargetRecovery.NEW_NAME if part_exists else TargetRecovery.RESTART


# ---- Giờ trên đĩa và JSON ------------------------------------------------------------------


def check_time(value: object, field: str) -> datetime.datetime:
    """Thời điểm có múi giờ → UTC, làm tròn xuống giây (dạng lưu trên đĩa); sai → StateError nêu `field`."""
    if not isinstance(value, datetime.datetime) or value.utcoffset() is None:
        raise StateError(f"{field} phải là datetime có múi giờ")
    try:
        return value.astimezone(datetime.UTC).replace(microsecond=0)
    except (OverflowError, ValueError):
        raise StateError(f"{field} ngoài khoảng biểu diễn được") from None


def format_time(value: datetime.datetime) -> str:
    """Dạng giờ trên đĩa `YYYY-MM-DDTHH:MM:SSZ` của một thời điểm đã đổi sang UTC (dùng chung với
    `core.history`). Không đổi múi giờ hay làm tròn: bên gọi đưa giá trị đã qua bước đó."""
    # Không dùng strftime: `%Y` không đệm số 0 cho năm < 1000 trên mọi nền tảng.
    return (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d}T"
        f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}Z"
    )


def parse_time(value: object, field: str) -> datetime.datetime:
    """Đọc `YYYY-MM-DDTHH:MM:SSZ` (chỉ chữ số ASCII) → datetime UTC; sai → StateError nêu `field`."""
    if not isinstance(value, str):
        raise StateError(f"{field} phải là chuỗi YYYY-MM-DDTHH:MM:SSZ")
    match = _TIME_RE.fullmatch(value)
    if match is None:
        raise StateError(f"{field} phải có dạng YYYY-MM-DDTHH:MM:SSZ")
    try:
        year, month, day, hour, minute, second = (int(part) for part in match.groups())
        return datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.UTC)
    except ValueError:
        raise StateError(f"{field} không phải thời điểm hợp lệ") from None


def _reject_constant(name: str) -> object:
    raise StateError(f"JSON không được chứa {name}")


def _load_json(data: object, limit: int, what: str) -> object:
    """Giải JSON UTF-8 (chấp nhận BOM) không quá `limit` byte; hỏng → StateError."""
    if not isinstance(data, bytes | bytearray | memoryview):
        raise StateError("data phải là bytes")
    # Kiểm trần trước khi chép hay giải mã: file quá lớn không tốn công.
    with memoryview(data) as view:
        if view.nbytes > limit:
            raise StateError(f"{what} dài quá {limit} byte")
    raw = bytes(data)
    try:
        return json.loads(raw.decode("utf-8-sig"), parse_constant=_reject_constant)
    except StateError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        # ValueError gồm JSONDecodeError và số nguyên quá nhiều chữ số; RecursionError: lồng quá sâu.
        raise StateError(f"{what} không phải JSON UTF-8 hợp lệ") from None


def _require(obj: dict[str, object], keys: tuple[str, ...], what: str) -> None:
    for key in keys:
        if key not in obj:
            raise StateError(f"{what} thiếu trường {key}")


def _check_version(value: object, what: str) -> None:
    if type(value) is not int or value != RECORD_VERSION:
        raise StateError(f"{what}: version phải là {RECORD_VERSION}")


def _check_device_id(value: object, field: str) -> str:
    try:
        # Lớp con của str đổi về str thuần để so sánh và mã hóa nhất quán.
        return str.__str__(validate_device_id(value))
    except MessageError:
        raise StateError(f"{field} phải là đúng 32 ký tự hex thường") from None


# ---- transfer.json -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransferRecord:
    """Nội dung `transfer.json` của một lượt nhận (architecture mục 4). Kiểm trong `__post_init__`.

    - `receiver`: `device_id` của máy này; `sender`: `device_id` của máy gửi.
    - `offer`: manifest của lượt (chỉ nằm ở đây; header `.state` chỉ đối chiếu).
    - `verified`: số file đầu đã đổi sang tên thật, 0…số file.
    - `done_at`: lúc file cuối xong (UTC, làm tròn xuống giây); có khi và chỉ khi `verified`
      bằng số file.

    Sai → StateError. Không kiểm `receiver` là máy này hay `sender` còn tin cậy: việc của `app`.
    """

    receiver: str
    sender: str
    offer: Offer
    verified: int
    done_at: datetime.datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "receiver", _check_device_id(self.receiver, "receiver"))
        object.__setattr__(self, "sender", _check_device_id(self.sender, "sender"))
        if not isinstance(self.offer, Offer):
            raise StateError("offer phải là Offer")
        n = len(self.offer.files)
        _check_int(self.verified, "verified", 0, n)
        if self.done_at is not None:
            object.__setattr__(self, "done_at", check_time(self.done_at, "done_at"))
        if (self.done_at is not None) != (self.verified == n):
            raise StateError("done_at có khi và chỉ khi mọi file đã verified")


_TRANSFER_KEYS = (
    "version",
    "receiver",
    "sender",
    "transfer_id",
    "block_size",
    "files",
    "verified",
    "done_at",
)


def encode_transfer_record(record: TransferRecord) -> bytes:
    """Nội dung `transfer.json`: UTF-8, `indent=2`, giữ nguyên ký tự Unicode. Sai → StateError."""
    if not isinstance(record, TransferRecord):
        raise StateError("record phải là TransferRecord")
    obj = {
        "version": RECORD_VERSION,
        "receiver": record.receiver,
        "sender": record.sender,
        "transfer_id": record.offer.transfer_id,
        "block_size": record.offer.block_size,
        "files": [f.to_dict() for f in record.offer.files],
        "verified": record.verified,
        "done_at": None if record.done_at is None else format_time(record.done_at),
    }
    data = (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # Không xảy ra với `offer` hợp lệ (1000 tên dài nhất cỡ 0,6 MiB), nhưng bộ đọc từ chối file quá trần.
    if len(data) > MAX_TRANSFER_RECORD:
        raise StateError(f"transfer.json dài quá {MAX_TRANSFER_RECORD} byte")
    return data


def decode_transfer_record(data: bytes | bytearray | memoryview, *, dir_name: str) -> TransferRecord:
    """Đọc `transfer.json` của thư mục tạm `dir_name`; sai bất kỳ điều gì → StateError.

    Kiểm: ≤ `MAX_TRANSFER_RECORD` byte (trước khi giải mã), JSON UTF-8 (chấp nhận BOM, từ chối
    `NaN`/`Infinity`), object có đủ mọi trường; `version` = 1; `transfer_id` hợp lệ và bằng
    `dir_name`; `block_size` = `BLOCK_SIZE`; `files` qua đúng bộ kiểm `offer` của
    `teleport.core.messages`; `verified` 0…số file; `done_at` null hoặc
    `YYYY-MM-DDTHH:MM:SSZ`, có khi và chỉ khi `verified` = số file. Trường lạ bị bỏ qua.
    """
    obj = _load_json(data, MAX_TRANSFER_RECORD, "transfer.json")
    if not isinstance(obj, dict):
        raise StateError("transfer.json phải là một JSON object")
    _require(obj, _TRANSFER_KEYS, "transfer.json")
    _check_version(obj["version"], "transfer.json")
    transfer_id = obj["transfer_id"]
    if not isinstance(dir_name, str) or transfer_id != dir_name:
        raise StateError("transfer_id khác tên thư mục tạm")
    if type(obj["block_size"]) is not int or obj["block_size"] != BLOCK_SIZE:
        raise StateError(f"block_size phải là {BLOCK_SIZE}")
    try:
        offer = Offer(transfer_id=transfer_id, block_size=BLOCK_SIZE, files=obj["files"])
    except MessageError as e:
        raise StateError(f"transfer.json: {e}") from None
    done_at = obj["done_at"]
    return TransferRecord(
        receiver=obj["receiver"],
        sender=obj["sender"],
        offer=offer,
        verified=obj["verified"],
        done_at=None if done_at is None else parse_time(done_at, "done_at"),
    )


# ---- tombstones.json -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TombstoneRecord:
    """Một bia mộ trong `tombstones.json` (architecture mục 4, protocol mục 6.3).

    `direction`: `"send"` hay `"receive"` (giá trị của `core.transfer.Direction`, luôn lưu dạng
    `str` thuần); `reason`: như `cancel.reason`; `unsent`: chưa gửi được `cancel`; `at`: lúc
    lập (UTC, làm tròn xuống giây). Sai → StateError.
    """

    device_id: str
    direction: str
    transfer_id: str
    reason: str
    unsent: bool
    at: datetime.datetime

    def __post_init__(self) -> None:
        device_id = _check_device_id(self.device_id, "device_id")
        if not isinstance(self.direction, str) or self.direction not in _DIRECTIONS:
            raise StateError("direction phải là send hoặc receive")
        try:
            cancel = Cancel(transfer_id=self.transfer_id, reason=self.reason)
        except MessageError as e:
            raise StateError(str(e)) from None
        if type(self.unsent) is not bool:
            raise StateError("unsent phải là bool")
        # Lớp con của str (ví dụ Direction.SEND) đổi về str thuần để so sánh và mã hóa nhất quán.
        object.__setattr__(self, "device_id", device_id)
        object.__setattr__(self, "direction", str.__str__(self.direction))
        object.__setattr__(self, "transfer_id", str.__str__(cancel.transfer_id))
        object.__setattr__(self, "reason", cancel.reason)
        object.__setattr__(self, "at", check_time(self.at, "at"))

    def _to_line(self) -> str:
        return json.dumps(
            {
                "device_id": self.device_id,
                "direction": self.direction,
                "transfer_id": self.transfer_id,
                "reason": self.reason,
                "unsent": self.unsent,
                "at": format_time(self.at),
            }
        )


_TOMBSTONE_KEYS = ("device_id", "direction", "transfer_id", "reason", "unsent", "at")
_TOMBSTONES_HEAD = f'{{\n  "version": {RECORD_VERSION},\n  "entries": ['
_TOMBSTONES_EMPTY = f'{{\n  "version": {RECORD_VERSION},\n  "entries": []\n}}\n'
_TOMBSTONES_LEAD = "\n    "
_TOMBSTONES_TAIL = "\n  ]\n}\n"
_TOMBSTONES_SEP = ",\n    "


def encode_tombstones(entries: Iterable[TombstoneRecord]) -> bytes:
    """Nội dung `tombstones.json`, mỗi mục một dòng, giữ thứ tự của `entries`.

    Vượt `MAX_TOMBSTONE_FILE` thì bỏ mục cũ nhất (`at` nhỏ nhất; cùng `at` thì mục đứng trước)
    tới khi vừa. Phần tử không phải TombstoneRecord → StateError.
    """
    items = list(entries)
    if not all(isinstance(item, TombstoneRecord) for item in items):
        raise StateError("entries phải là các TombstoneRecord")
    lines = [item._to_line() for item in items]  # chỉ có ASCII: độ dài chuỗi = số byte
    keep = [True] * len(lines)
    # Mỗi mục tốn độ dài dòng + một dấu phân cách; mục đầu dùng `_TOMBSTONES_LEAD` thay dấu phân cách.
    base = len(_TOMBSTONES_HEAD) + len(_TOMBSTONES_LEAD) + len(_TOMBSTONES_TAIL) - len(_TOMBSTONES_SEP)
    total = base + sum(len(line) + len(_TOMBSTONES_SEP) for line in lines)
    if total > MAX_TOMBSTONE_FILE:
        oldest_first = sorted(range(len(items)), key=lambda k: (items[k].at, k))
        for k in oldest_first:
            if total <= MAX_TOMBSTONE_FILE:
                break
            keep[k] = False
            total -= len(lines[k]) + len(_TOMBSTONES_SEP)
    kept = [line for line, ok in zip(lines, keep, strict=True) if ok]
    if not kept:
        text = _TOMBSTONES_EMPTY
    else:
        text = _TOMBSTONES_HEAD + _TOMBSTONES_LEAD + _TOMBSTONES_SEP.join(kept) + _TOMBSTONES_TAIL
    data = text.encode("utf-8")
    if len(data) > MAX_TOMBSTONE_FILE:  # không xảy ra: phòng sai sót khi tính độ dài
        raise StateError(f"tombstones.json dài quá {MAX_TOMBSTONE_FILE} byte")
    return data


def _tombstone_from_dict(obj: object) -> TombstoneRecord:
    if not isinstance(obj, dict):
        raise StateError("mỗi mục phải là một JSON object")
    _require(obj, _TOMBSTONE_KEYS, "mục bia mộ")
    return TombstoneRecord(
        device_id=obj["device_id"],
        direction=obj["direction"],
        transfer_id=obj["transfer_id"],
        reason=obj["reason"],
        unsent=obj["unsent"],
        at=parse_time(obj["at"], "at"),
    )


def decode_tombstones(data: bytes | bytearray | memoryview) -> list[TombstoneRecord]:
    """Đọc `tombstones.json`; giữ thứ tự trong file, bỏ riêng từng mục hỏng.

    Cả file hỏng → StateError: vượt `MAX_TOMBSTONE_FILE` byte (kiểm trước khi giải mã), không
    phải JSON UTF-8 (chấp nhận BOM), không phải object, `version` khác 1, `entries` không phải
    mảng. Mục thiếu hay sai trường bị bỏ. Không lọc thiết bị còn tin cậy, tuổi (hạn nhớ
    `core.transfer.REMEMBER_TTL`) hay số mục mỗi thiết bị: việc của `app` và `core.transfer`.
    Trường lạ bị bỏ qua.
    """
    obj = _load_json(data, MAX_TOMBSTONE_FILE, "tombstones.json")
    if not isinstance(obj, dict):
        raise StateError("tombstones.json phải là một JSON object")
    _require(obj, ("version", "entries"), "tombstones.json")
    _check_version(obj["version"], "tombstones.json")
    entries = obj["entries"]
    if not isinstance(entries, list):
        raise StateError("entries phải là một mảng")
    result = []
    for entry in entries:
        try:
            result.append(_tombstone_from_dict(entry))
        except StateError:
            continue
    return result
