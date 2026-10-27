"""Test `ReceiveDir.finalize` của `teleport.app.storage` (P2-5): đổi tên không ghi đè.

Viết theo đặc tả (spec P2-5 mục 3.5, 3.6, 3.8, AC-16…AC-23, AC-29, H1), không theo code. Nguồn:
architecture §4 “Kết thúc một file” (bước 2, 3, 5, 6) và ghi chú nền tảng, protocol §4.4 bước 6
(`O_CREAT | O_EXCL`, đổi tên thử lại 5 × 50 ms khi `PermissionError`, không bao giờ ghi đè), mvp F4
(trùng tên thêm ` (1)`, so trùng không phân biệt hoa thường), mvp N9 (log không chứa tên file).

Luật kiểm ở đây:
- Tên cuối = `fit_filename(name)` khi không trùng; trùng (NFC + casefold, với file, thư mục, link)
  thì ` (n)`; file có sẵn không đổi byte, `st_ino` hay `st_mtime_ns`.
- Kẻ chen tạo đích trước khi giữ chỗ, hay ghi vào file giữ chỗ, không bị ghi đè; `.part` bị thay
  trước khi đổi tên → `StorageError`, không đưa file lạ lên tên đích, file giữ chỗ bị dọn.
- Đổi tên: Windows `_move_file_ex(…, REPLACE_EXISTING | WRITE_THROUGH)`, POSIX `os.replace`; thử
  lại `PermissionError` đúng `REPLACE_ATTEMPTS` lần; lỗi khác không thử lại; lỗi thì dọn file giữ chỗ
  (chỉ khi nó còn là file 0 byte của mình), `.part` nằm nguyên.
- Hệ file từ chối tên (`EILSEQ`, `EINVAL`, `ENAMETOOLONG`) → tên dự phòng ASCII; từ chối mọi tên →
  `NameUnavailableError`.
- Đường dẫn cuối vừa `PathLimits` (đơn vị: UTF-16 trên Windows, byte trên POSIX).

P3-3a (spec-p3-3a mục 4 V1, 8.1): `create_temp` bắt buộc `record` khớp `size` từng part (fixture
`new_temp`), `finalize` chỉ nhận file kế tiếp; `_write_atomic_in` (`.state`, `transfer.json`) cũng đổi
tên qua cùng seam, nên các test đếm đổi tên lọc theo tên (`.part` → tên thật). Bước 1, 4, 7, 8 và hủy
giữ chỗ bằng `TARGET`: `test_storage_resume.py`; sập ở từng bước: `test_storage_recovery.py`.

Không test nào mở socket (spec mục 5). Mọi `ReceiveDir` được đóng để Windows dọn được `tmp_path`.
"""

import errno
import hashlib
import importlib
import itertools
import logging
import os
import random
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.core.blocks import BLOCK_SIZE, block_count

BS = BLOCK_SIZE
ID = "0123456789abcdef0123456789abcdef"
ID2 = "fedcba9876543210fedcba9876543210"
WINDOWS = sys.platform == "win32"

posix_only = pytest.mark.skipif(WINDOWS, reason="nhánh POSIX")
windows_only = pytest.mark.skipif(not WINDOWS, reason="chỉ Windows")
macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="chỉ macOS")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="chỉ Linux")


@pytest.fixture(scope="module")
def storage():
    return importlib.import_module("teleport.app.storage")


@pytest.fixture(scope="module")
def fn():
    return importlib.import_module("teleport.core.filenames")


@pytest.fixture
def root(tmp_path):
    return tmp_path / "recv"


@pytest.fixture
def rd(storage, root):
    with storage.ReceiveDir.open(root) as receive_dir:
        yield receive_dir


@pytest.fixture
def new_temp(rd, record_for):
    """`new_temp(sizes, tid=ID)`: thư mục tạm của lượt có `record` khớp `size` từng part (P3-3a:
    `create_temp` bắt buộc `record`, `create_part` kiểm `size` theo nó; spec-p3-3a mục 8.1 (c))."""

    def make(sizes, tid: str = ID):
        return rd.create_temp(tid, record_for(tid, sizes))

    return make


@pytest.fixture
def quick_retry(storage, monkeypatch):
    monkeypatch.setattr(storage, "REPLACE_DELAY", 0.001)


# ---------------------------------------------------------------------------------------------
# Hàm phụ trợ
# ---------------------------------------------------------------------------------------------


def payload(size: int, seed: int = 1) -> bytes:
    data = bytearray(random.Random(seed).randbytes(size))
    for off in range(0, max(size - 4, 0), 4093):
        data[off : off + 4] = b"\r\n\x1a\n"
    return bytes(data)


def make_part(temp, index: int, data: bytes):
    part = temp.create_part(index, len(data))
    view = memoryview(data)
    for i in range(block_count(len(data))):
        part.write_at(i, view[i * BS : (i + 1) * BS])
    return part


def key(name: str) -> str:
    """Khóa so trùng theo đặc tả (NFC → casefold → NFC), viết độc lập với code."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def entries(root) -> list[str]:
    """Các mục trong thư mục nhận, trừ `.teleport`, đã chuẩn hóa NFC (APFS/HFS+ có thể trả NFD)."""
    return sorted(unicodedata.normalize("NFC", n) for n in os.listdir(root) if n != ".teleport")


def units(s: str) -> int:
    if WINDOWS:
        return len(s.encode("utf-16-le")) // 2
    return len(os.fsencode(s))


def make_symlink(target, link, *, is_dir: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"không tạo được symlink: {e}")


def basename_of(path) -> str:
    return os.path.basename(os.fsdecode(path))


def install_rename(storage, monkeypatch, behavior) -> None:
    """Thay bước đổi tên của finalize: Windows `_move_file_ex`, POSIX `os.replace` (AC-19).

    `behavior(real, args, kwargs)` quyết định gọi hàm thật hay ném lỗi.
    """
    owner, attr = (storage, "_move_file_ex") if WINDOWS else (os, "replace")
    real = getattr(owner, attr)

    def wrapper(*args, **kwargs):
        # P3-3a: `_write_atomic_in` (`.state`, `transfer.json`) cũng đổi tên qua seam này; chỉ lần
        # đổi tên `.part` → tên thật đi qua `behavior` (lọc theo tên, spec-p3-3a mục 8.1 (b)).
        if not os.fsdecode(args[0]).endswith(".part"):
            return real(*args, **kwargs)
        return behavior(real, args, kwargs)

    monkeypatch.setattr(owner, attr, wrapper)


def rename_dst(rd, args) -> Path:
    dst = Path(os.fsdecode(args[1]))
    return dst if dst.is_absolute() else rd.root / dst


def reject_names(monkeypatch, err: int, *, everything: bool = False) -> list[str]:
    """Giả hệ file từ chối tên: lần mở có `O_EXCL` với tên chứa ký tự ngoài ASCII (hoặc mọi tên)."""
    real_open = os.open
    rejected: list[str] = []

    def fake_open(path, flags, *args, **kwargs):
        if not isinstance(path, int) and flags & os.O_EXCL:
            base = basename_of(path)
            if everything or not base.isascii():
                rejected.append(base)
                raise OSError(err, os.strerror(err), base)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)
    return rejected


def expected_name(fn, rd, name: str) -> str:
    """Tên ứng viên đầu tiên theo ngân sách ở spec mục 3.5 (viết lại độc lập từ đặc tả)."""
    budget = rd.limits.max_name_bytes
    while True:
        cand = fn.fit_filename(name, budget)
        excess = units(os.path.join(str(rd.root), cand)) - rd.limits.max_path
        if excess <= 0:
            return cand
        budget -= max(1, excess)


def without_inode(st: os.stat_result) -> os.stat_result:
    _, (seq, extra) = st.__reduce__()
    fields = list(seq)
    fields[1] = 0  # st_ino
    return os.stat_result(fields, extra)


def filesystem_without_inodes(monkeypatch) -> None:
    """Giả hệ file không có `st_ino` (FAT, ổ mạng; spec mục 3.3, 7): mọi `stat` trả `st_ino` = 0."""
    for name in ("stat", "lstat", "fstat"):
        real = getattr(os, name)

        def fake(*args, _real=real, **kwargs):
            return without_inode(_real(*args, **kwargs))

        monkeypatch.setattr(os, name, fake)


def limit_root(storage, monkeypatch, root, *, extra: int, max_name_bytes: int = 255) -> int:
    max_path = units(os.path.abspath(root)) + extra
    monkeypatch.setattr(storage, "_path_limits", lambda r: storage.PathLimits(max_path, max_name_bytes))
    return max_path


# ---------------------------------------------------------------------------------------------
# AC-16 Trường hợp cơ bản
# ---------------------------------------------------------------------------------------------

NAMES_ON_DISK = [
    "a.txt",
    "Báo cáo tháng 10.docx",
    "../../evil.txt",
    "C:\\Windows\\x",
    "\\\\?\\C:\\x",
    "CON",
    "nul.txt",
    "COM\u00b9",
    ".teleport",
    ".TELEPORT",
    "a.txt:ads",
    "x.",
    "x ",
    "",
    "..",
    ".",
    "./",
    "photo\u202egpj.exe",
    unicodedata.normalize("NFD", "Tiếng Việt.txt"),
    "ệ" * 50,
    "a" * 50 + "." + "b" * 32,
    "x" * 100_000 + ".txt",
]
# id ngắn: tên 100 000 ký tự làm biến môi trường PYTEST_CURRENT_TEST vượt giới hạn của Windows.
NAME_IDS = [f"{i}-{ascii(n)[:24]}" for i, n in enumerate(NAMES_ON_DISK)]


@pytest.mark.parametrize("name", NAMES_ON_DISK, ids=NAME_IDS)
def test_finalize_moves_part_to_sanitized_name(fn, rd, new_temp, tmp_path, name):
    temp = new_temp([1000])
    data = payload(1000)
    part = make_part(temp, 0, data)
    part_path = part.path
    final = rd.finalize(part, name)
    # `fit_filename(name)` khi đường dẫn vừa giới hạn; tên rất dài thì cắt theo ngân sách mục 3.5.
    assert final == rd.root / expected_name(fn, rd, name)
    if units(os.path.join(str(rd.root), fn.fit_filename(name))) <= rd.limits.max_path:
        assert final == rd.root / fn.fit_filename(name)
    assert final.read_bytes() == data
    assert not os.path.lexists(part_path)
    assert part.closed
    assert entries(rd.root) == [unicodedata.normalize("NFC", final.name)]
    assert os.listdir(tmp_path) == ["recv"]  # không thoát khỏi thư mục nhận


@pytest.mark.parametrize("size", [0, 1, BS, BS + 1])
def test_finalize_boundary_sizes(rd, new_temp, size):
    temp = new_temp([size])
    data = payload(size, seed=size)
    final = rd.finalize(make_part(temp, 0, data), "f.bin")
    assert final.read_bytes() == data


@pytest.mark.parametrize("mtime_ms", [1_700_000_000_123, 0])
def test_finalize_sets_mtime(rd, new_temp, mtime_ms):
    temp = new_temp([BS + 5])
    final = rd.finalize(make_part(temp, 0, payload(BS + 5)), "a.txt", mtime_ms=mtime_ms)
    assert os.stat(final).st_mtime_ns // 1_000_000 == mtime_ms


def test_finalize_huge_mtime_still_succeeds(rd, new_temp):
    temp = new_temp([100])
    data = payload(100)
    final = rd.finalize(make_part(temp, 0, data), "a.txt", mtime_ms=2**53 - 1)
    assert final.read_bytes() == data


@pytest.mark.parametrize(
    "error", [OSError(errno.EPERM, "khong cho"), OverflowError("tran"), ValueError("sai")]
)
def test_finalize_ignores_utime_errors(rd, new_temp, monkeypatch, error):
    temp = new_temp([100])
    data = payload(100)
    part = make_part(temp, 0, data)

    def broken_utime(*args, **kwargs):
        raise error

    monkeypatch.setattr(os, "utime", broken_utime)
    final = rd.finalize(part, "a.txt", mtime_ms=1_700_000_000_000)
    assert final.read_bytes() == data


def test_finalize_without_mtime_keeps_content(rd, new_temp):
    temp = new_temp([10])
    data = payload(10)
    final = rd.finalize(make_part(temp, 0, data), "a.txt", mtime_ms=None)
    assert final.read_bytes() == data


# ---------------------------------------------------------------------------------------------
# AC-17 File canh gác
# ---------------------------------------------------------------------------------------------


def test_existing_file_is_never_overwritten(rd, new_temp):
    temp = new_temp([5000])
    sentinel = rd.root / "a.txt"
    content = os.urandom(64 * 1024)
    sentinel.write_bytes(content)
    before = os.stat(sentinel)
    data = payload(5000)
    final = rd.finalize(make_part(temp, 0, data), "a.txt")
    assert final == rd.root / "a (1).txt"
    assert final.read_bytes() == data
    after = os.stat(sentinel)
    assert sentinel.read_bytes() == content
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_second_number_when_first_taken(rd, new_temp):
    temp = new_temp([3])
    (rd.root / "a.txt").write_bytes(b"0")
    (rd.root / "a (1).txt").write_bytes(b"1")
    final = rd.finalize(make_part(temp, 0, b"moi"), "a.txt")
    assert final == rd.root / "a (2).txt"
    assert (rd.root / "a.txt").read_bytes() == b"0"
    assert (rd.root / "a (1).txt").read_bytes() == b"1"


def test_case_insensitive_match_on_every_os(rd, new_temp):
    temp = new_temp([3])
    (rd.root / "A.TXT").write_bytes(b"hoa")
    final = rd.finalize(make_part(temp, 0, b"moi"), "a.txt")
    assert final == rd.root / "a (1).txt"
    assert entries(rd.root) == ["A.TXT", "a (1).txt"]
    assert (rd.root / "A.TXT").read_bytes() == b"hoa"


def test_nfc_existing_matches_nfd_incoming(rd, new_temp):
    temp = new_temp([3])
    nfc = "\u00c9.txt"
    (rd.root / nfc).write_bytes(b"cu")
    final = rd.finalize(make_part(temp, 0, b"moi"), "E\u0301.txt")
    assert unicodedata.normalize("NFC", final.name) == "\u00c9 (1).txt"
    assert final.read_bytes() == b"moi"
    assert (rd.root / nfc).read_bytes() == b"cu"


def test_existing_directory_with_same_name(rd, new_temp):
    temp = new_temp([3])
    (rd.root / "a.txt").mkdir()
    final = rd.finalize(make_part(temp, 0, b"moi"), "a.txt")
    assert final == rd.root / "a (1).txt"
    assert (rd.root / "a.txt").is_dir()
    assert os.listdir(rd.root / "a.txt") == []


def test_existing_dangling_symlink_with_same_name(rd, new_temp, tmp_path):
    temp = new_temp([3])
    target = tmp_path / "khong-ton-tai.txt"
    link = rd.root / "a.txt"
    make_symlink(target, link, is_dir=False)
    # So với chính link trước khi finalize, không so với str(target): trên Windows os.readlink trả
    # đường dẫn thay thế có tiền tố `\\?\` (Python ≥ 3.8), nên so với target sẽ đỏ dù link không đổi.
    link_before = (os.readlink(link), os.lstat(link).st_ino)
    final = rd.finalize(make_part(temp, 0, b"moi"), "a.txt")
    assert final == rd.root / "a (1).txt"
    assert os.path.islink(link)
    assert (os.readlink(link), os.lstat(link).st_ino) == link_before
    assert not os.path.lexists(target)


def test_thousand_case_variants_of_same_name(storage, rd, new_temp, monkeypatch):
    temp = new_temp([2] * 1000)
    # Ca biên spec mục 5: 1000 tên trùng nhau, khác hoa thường. Test chỉ xét chọn tên, nên bỏ
    # fsync (qua seam `_full_fsync` và `os.fsync`): 3000 lần fsync, nhất là F_FULLFSYNC trên macOS,
    # làm test chậm mà không kiểm thêm gì.
    monkeypatch.setattr(storage, "_full_fsync", lambda fd: None)
    monkeypatch.setattr(os, "fsync", lambda fd: None)
    rng = random.Random(1000)
    finals = []
    for i in range(1000):
        name = "".join(c.upper() if rng.random() < 0.5 else c for c in "anh.jpg")
        finals.append(rd.finalize(make_part(temp, i, i.to_bytes(2, "big")), name))
    keys = [key(p.name) for p in finals]
    assert len(set(keys)) == 1000
    for i, path in enumerate(finals):
        assert path.parent == rd.root
        assert path.read_bytes() == i.to_bytes(2, "big")


# ---------------------------------------------------------------------------------------------
# AC-18 Kẻ chen
# ---------------------------------------------------------------------------------------------


def test_racer_creating_target_before_reservation_is_not_overwritten(rd, new_temp, monkeypatch):
    temp = new_temp([3000])
    data = payload(3000)
    part = make_part(temp, 0, data)
    real_open = os.open
    fired = []

    def racing_open(path, flags, *args, **kwargs):
        if not fired and not isinstance(path, int) and flags & os.O_EXCL and basename_of(path) == "a.txt":
            fired.append(True)
            with open(rd.root / "a.txt", "xb") as f:  # io.open không đi qua os.open
                f.write(b"KE CHEN")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    final = rd.finalize(part, "a.txt")
    monkeypatch.undo()
    assert fired
    assert final == rd.root / "a (1).txt"
    assert (rd.root / "a.txt").read_bytes() == b"KE CHEN"
    assert final.read_bytes() == data


def test_racer_writing_into_reservation_gets_next_name(storage, rd, new_temp, monkeypatch):
    temp = new_temp([3000])
    data = payload(3000)
    part = make_part(temp, 0, data)
    calls: list[Path] = []

    def after_reserve(target):
        calls.append(Path(target))
        if len(calls) == 1:
            with open(target, "ab") as f:
                f.write(b"KE CHEN")

    monkeypatch.setattr(storage, "_after_reserve", after_reserve)
    final = rd.finalize(part, "a.txt")
    assert len(calls) == 2
    assert calls[0] == rd.root / "a.txt"
    assert (rd.root / "a.txt").read_bytes() == b"KE CHEN"
    assert final == rd.root / "a (1).txt"
    assert final.read_bytes() == data


def test_part_swapped_before_rename_is_not_published(storage, rd, new_temp, monkeypatch):
    temp = new_temp([3000])
    data = payload(3000)
    impostor = payload(3000, seed=99)
    part = make_part(temp, 0, data)
    part_path = part.path
    swapped = []

    def after_reserve(target):
        if not swapped:
            swapped.append(True)
            other = part_path.with_name("ke-chen.tmp")
            other.write_bytes(impostor)  # cùng kích thước, khác danh tính
            os.replace(other, part_path)

    monkeypatch.setattr(storage, "_after_reserve", after_reserve)
    with pytest.raises(storage.StorageError):
        rd.finalize(part, "a.txt")
    assert swapped
    assert entries(rd.root) == []  # file giữ chỗ đã bị xóa, không có tên đích
    assert part_path.read_bytes() == impostor
    assert part.closed


@posix_only
def test_part_swapped_for_symlink_before_rename_is_not_published(
    storage, rd, new_temp, monkeypatch, tmp_path
):
    temp = new_temp([3000])
    data = payload(3000)
    victim = tmp_path / "victim.bin"
    victim.write_bytes(payload(3000, seed=42))
    part = make_part(temp, 0, data)
    part_path = part.path

    def after_reserve(target):
        if not os.path.islink(part_path):
            os.unlink(part_path)
            os.symlink(victim, part_path)

    monkeypatch.setattr(storage, "_after_reserve", after_reserve)
    with pytest.raises(storage.StorageError):
        rd.finalize(part, "a.txt")
    assert victim.read_bytes() == payload(3000, seed=42)
    assert entries(rd.root) == []
    assert not any(os.path.islink(rd.root / n) for n in os.listdir(rd.root))


# ---------------------------------------------------------------------------------------------
# AC-19 Thử lại và dọn
# ---------------------------------------------------------------------------------------------


def test_rename_permission_error_twice_then_succeeds(storage, rd, new_temp, monkeypatch):
    temp = new_temp([2000])
    monkeypatch.setattr(storage, "REPLACE_DELAY", 0.02)
    data = payload(2000)
    part = make_part(temp, 0, data)
    stamps: list[float] = []

    def flaky(real, args, kwargs):
        # perf_counter: monotonic trên Windows/3.12 chỉ chính xác 15,6 ms (như tests/test_atomic.py).
        stamps.append(time.perf_counter())
        if len(stamps) <= 2:
            raise PermissionError(errno.EACCES, "dang bi giu")
        return real(*args, **kwargs)

    install_rename(storage, monkeypatch, flaky)
    final = rd.finalize(part, "a.txt")
    assert len(stamps) == 3
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    assert all(g >= 0.015 for g in gaps), gaps
    assert final.read_bytes() == data


@pytest.mark.parametrize("attempts", [None, 3])
def test_rename_permission_error_forever_cleans_reservation(
    storage, rd, new_temp, monkeypatch, quick_retry, attempts
):
    temp = new_temp([2000])
    if attempts is not None:
        monkeypatch.setattr(storage, "REPLACE_ATTEMPTS", attempts)  # đọc lúc dùng
    sentinel = rd.root / "a.txt"
    sentinel.write_bytes(b"canh gac")
    data = payload(2000)
    part = make_part(temp, 0, data)
    part_path = part.path
    calls: list[Path] = []

    def locked(real, args, kwargs):
        calls.append(rename_dst(rd, args))
        raise PermissionError(errno.EACCES, "dang bi giu")

    install_rename(storage, monkeypatch, locked)
    with pytest.raises(PermissionError):
        rd.finalize(part, "a.txt")
    assert len(calls) == storage.REPLACE_ATTEMPTS
    assert set(calls) == {rd.root / "a (1).txt"}
    assert not os.path.lexists(rd.root / "a (1).txt")  # file giữ chỗ đã bị xóa
    assert entries(rd.root) == ["a.txt"]
    assert sentinel.read_bytes() == b"canh gac"
    assert part_path.read_bytes() == data
    assert part.closed
    with pytest.raises(ValueError):
        rd.finalize(part, "a.txt")  # đã đóng, không finalize lại được


def test_rename_other_error_is_not_retried(storage, rd, new_temp, monkeypatch, quick_retry):
    temp = new_temp([2000])
    data = payload(2000)
    part = make_part(temp, 0, data)
    part_path = part.path
    calls = []

    def broken(real, args, kwargs):
        calls.append(args)
        raise OSError(errno.EIO, "loi I/O")

    install_rename(storage, monkeypatch, broken)
    with pytest.raises(OSError) as exc:
        rd.finalize(part, "a.txt")
    assert exc.value.errno == errno.EIO
    assert len(calls) == 1
    assert entries(rd.root) == []
    assert part_path.read_bytes() == data


def test_reservation_changed_by_someone_else_is_kept(storage, rd, new_temp, monkeypatch, quick_retry):
    temp = new_temp([2000])
    data = payload(2000)
    part = make_part(temp, 0, data)
    part_path = part.path
    wrote = []

    def write_then_fail(real, args, kwargs):
        if not wrote:
            wrote.append(True)
            with open(rename_dst(rd, args), "ab") as f:
                f.write(b"NGUOI KHAC")
        raise PermissionError(errno.EACCES, "dang bi giu")

    install_rename(storage, monkeypatch, write_then_fail)
    with pytest.raises(PermissionError):
        rd.finalize(part, "a.txt")
    assert (rd.root / "a.txt").read_bytes() == b"NGUOI KHAC"
    assert part_path.read_bytes() == data


@pytest.mark.parametrize("size", [0, 2000])
def test_interrupt_after_real_rename_keeps_final_file(storage, rd, new_temp, monkeypatch, size):
    temp = new_temp([size])
    data = payload(size)
    part = make_part(temp, 0, data)

    def rename_then_interrupt(real, args, kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt

    install_rename(storage, monkeypatch, rename_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        rd.finalize(part, "a.txt")
    final = rd.root / "a.txt"
    assert final.is_file()
    assert final.read_bytes() == data


@pytest.mark.parametrize("size", [0, 2000])
def test_interrupt_after_real_rename_keeps_final_file_without_inode(storage, rd, new_temp, monkeypatch, size):
    temp = new_temp([size])
    # AC-19 trên hệ file không có `st_ino` (spec mục 3.3, 3.6 “Dọn khi lỗi”, D27, mục 7): khi đó file
    # giữ chỗ và file 0 byte vừa nhận trông y như nhau (file thường, 0 byte). Đổi tên thật đã xong thì
    # file cuối không bao giờ bị xóa, dù lỗi bay ra trước khi kịp ghi nhận việc đổi tên.
    # Hồi quy soat-code (thấp): `size` 0 làm mất file vừa nhận.
    filesystem_without_inodes(monkeypatch)
    data = payload(size)
    part = make_part(temp, 0, data)
    part_path = part.path

    def rename_then_interrupt(real, args, kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt

    install_rename(storage, monkeypatch, rename_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        rd.finalize(part, "a.txt")
    monkeypatch.undo()
    final = rd.root / "a.txt"
    assert final.is_file()
    assert final.read_bytes() == data
    assert not os.path.lexists(part_path)


@pytest.mark.parametrize("size", [0, 2000])
def test_rename_failure_without_inode_still_cleans_reservation(
    storage, rd, new_temp, monkeypatch, quick_retry, size
):
    temp = new_temp([size])
    # Mặt kia của ca trên: đổi tên chưa xảy ra (`.part` còn trong thư mục tạm) thì file giữ chỗ vẫn
    # bị dọn khi không có `st_ino` (spec mục 3.6 “Dọn khi lỗi”: chỉ so loại file và kích thước).
    filesystem_without_inodes(monkeypatch)
    data = payload(size)
    part = make_part(temp, 0, data)
    part_path = part.path

    def locked(real, args, kwargs):
        raise PermissionError(errno.EACCES, "dang bi giu")

    install_rename(storage, monkeypatch, locked)
    with pytest.raises(PermissionError):
        rd.finalize(part, "a.txt")
    monkeypatch.undo()
    assert entries(rd.root) == []  # file giữ chỗ đã bị xóa
    assert part_path.read_bytes() == data


def test_cleanup_retries_unlink_of_reservation(storage, rd, new_temp, monkeypatch, quick_retry):
    temp = new_temp([100])
    part = make_part(temp, 0, payload(100))

    def broken(real, args, kwargs):
        raise OSError(errno.EIO, "loi I/O")

    install_rename(storage, monkeypatch, broken)
    calls: list[str] = []
    for name in ("unlink", "remove"):
        real = getattr(os, name)

        def flaky(path, *args, _real=real, **kwargs):
            if not isinstance(path, int) and basename_of(path) == "a.txt":
                calls.append("a.txt")
                if len(calls) <= 2:
                    raise PermissionError(errno.EACCES, "dang bi giu")
            return _real(path, *args, **kwargs)

        monkeypatch.setattr(os, name, flaky)
    with pytest.raises(OSError):
        rd.finalize(part, "a.txt")
    monkeypatch.undo()
    assert len(calls) >= 3
    assert not os.path.lexists(rd.root / "a.txt")


# ---------------------------------------------------------------------------------------------
# AC-20 Theo OS
# ---------------------------------------------------------------------------------------------


@windows_only
def test_windows_uses_move_file_ex_with_write_through(storage, rd, new_temp, monkeypatch):
    temp = new_temp([2000])
    data = payload(2000)
    part = make_part(temp, 0, data)
    events: list[tuple] = []
    real_move = storage._move_file_ex
    real_replace = os.replace
    real_open = os.open
    real_fsync = os.fsync

    def spy_move(src, dst, flags):
        if basename_of(dst) == "a.txt":  # bỏ các lần thay `.state`/`transfer.json` (spec-p3-3a 8.1 (b))
            events.append(("move", flags))
        return real_move(src, dst, flags)

    def spy_replace(*args, **kwargs):
        events.append(("replace",))
        return real_replace(*args, **kwargs)

    def spy_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if not isinstance(path, int) and flags & os.O_EXCL and basename_of(path) == "a.txt":
            events.append(("reserve",))
        return fd

    def spy_fsync(fd):
        events.append(("fsync",))
        return real_fsync(fd)

    monkeypatch.setattr(storage, "_move_file_ex", spy_move)
    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    final = rd.finalize(part, "a.txt")
    monkeypatch.undo()
    flags = storage.MOVEFILE_REPLACE_EXISTING | storage.MOVEFILE_WRITE_THROUGH
    assert flags == 0x9
    moves = [e for e in events if e[0] == "move"]
    assert moves == [("move", flags)]
    assert ("replace",) not in events
    reserve_at = events.index(("reserve",))
    move_at = events.index(moves[0])
    assert ("fsync",) in events[reserve_at + 1 : move_at]
    assert final.read_bytes() == data


@macos_only
def test_macos_finalize_uses_full_fsync(rd, new_temp, monkeypatch):
    temp = new_temp([100])
    import fcntl

    part = make_part(temp, 0, payload(100))
    real = fcntl.fcntl
    commands = []

    def spy(fd, cmd, *args):
        commands.append(cmd)
        return real(fd, cmd, *args)

    monkeypatch.setattr(fcntl, "fcntl", spy)
    rd.finalize(part, "a.txt")
    assert fcntl.F_FULLFSYNC in commands


@linux_only
def test_linux_fsyncs_after_replace(rd, new_temp, monkeypatch):
    temp = new_temp([100])
    part = make_part(temp, 0, payload(100))
    events: list[str] = []
    real_replace = os.replace
    real_fsync = os.fsync

    def spy_replace(*args, **kwargs):
        events.append("replace")
        return real_replace(*args, **kwargs)

    def spy_fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    rd.finalize(part, "a.txt")
    assert "replace" in events
    assert "fsync" in events[events.index("replace") + 1 :]


# ---------------------------------------------------------------------------------------------
# AC-21 Tên dự phòng
# ---------------------------------------------------------------------------------------------

ODD_NAME = "Ảnh \U00018df3.jpg"


@pytest.mark.parametrize("err", [errno.EILSEQ, errno.EINVAL, errno.ENAMETOOLONG])
def test_rejected_name_falls_back_to_ascii(fn, rd, new_temp, monkeypatch, err):
    temp = new_temp([3000])
    data = payload(3000)
    part = make_part(temp, 0, data)
    rejected = reject_names(monkeypatch, err)
    final = rd.finalize(part, ODD_NAME)
    monkeypatch.undo()
    assert rejected
    assert final == rd.root / fn.fallback_filename(ODD_NAME)
    assert final.name.isascii()
    assert final.read_bytes() == data


def test_fallback_name_is_numbered_when_taken(fn, rd, new_temp, monkeypatch):
    temp = new_temp([300])
    fallback = fn.fallback_filename(ODD_NAME)
    (rd.root / fallback).write_bytes(b"cu")
    data = payload(300)
    part = make_part(temp, 0, data)
    reject_names(monkeypatch, errno.EILSEQ)
    final = rd.finalize(part, ODD_NAME)
    monkeypatch.undo()
    assert final == rd.root / fn.numbered(fallback, 1, max_bytes=fn.FALLBACK_MAX_BYTES)
    assert final.read_bytes() == data
    assert (rd.root / fallback).read_bytes() == b"cu"


def test_every_name_rejected_raises_name_unavailable(storage, rd, new_temp, monkeypatch):
    temp = new_temp([300])
    data = payload(300)
    part = make_part(temp, 0, data)
    part_path = part.path
    reject_names(monkeypatch, errno.EILSEQ, everything=True)
    with pytest.raises(storage.NameUnavailableError) as exc:
        rd.finalize(part, "a.txt")
    monkeypatch.undo()
    assert isinstance(exc.value.__cause__, OSError)  # giữ lỗi gốc bằng `from`
    assert entries(rd.root) == []
    assert part_path.read_bytes() == data


def test_candidate_cap_stops_endless_interloper(storage, rd, new_temp, monkeypatch):
    temp = new_temp([300])
    # D19: kẻ chen làm hỏng mọi file giữ chỗ → dừng bằng NameUnavailableError sau trần ứng viên
    # (F3: tính cả các lần quay lại từ F7). Trần là biến cấp module, vá nhỏ lại để test nhanh.
    monkeypatch.setattr(storage, "MAX_NAME_CANDIDATES", 5)
    data = payload(300)
    part = make_part(temp, 0, data)
    part_path = part.path
    tainted: list[Path] = []

    def always_tamper(target):
        tainted.append(Path(target))
        with open(target, "ab") as f:
            f.write(b"KE CHEN")

    monkeypatch.setattr(storage, "_after_reserve", always_tamper)
    with pytest.raises(storage.NameUnavailableError):
        rd.finalize(part, "a.txt")
    assert 1 <= len(tainted) <= 5
    for path in tainted:
        assert path.read_bytes() == b"KE CHEN"  # file đã đổi khác thì không bị xóa
    assert part_path.read_bytes() == data


@macos_only
def test_apfs_unassigned_code_point_still_finalizes(fn, rd, new_temp):
    temp = new_temp([300])
    data = payload(300)
    final = rd.finalize(make_part(temp, 0, data), ODD_NAME)
    name = unicodedata.normalize("NFC", final.name)
    assert name in {fn.fit_filename(ODD_NAME), fn.fallback_filename(ODD_NAME)}
    assert final.read_bytes() == data


# ---------------------------------------------------------------------------------------------
# AC-22 Độ dài đường dẫn ở finalize
# ---------------------------------------------------------------------------------------------

LONG_ASCII = "x" * 246 + ".mp4"  # 250 byte
LONG_UNICODE = "ệ" * 100 + ".mp4"


@pytest.mark.parametrize("name", [LONG_ASCII, LONG_UNICODE])
def test_long_name_is_cut_to_fit_path_limit(storage, fn, root, monkeypatch, name, record_for):
    max_path = limit_root(storage, monkeypatch, root, extra=storage.TEMP_PATH_RESERVE)
    with storage.ReceiveDir.open(root) as rd:
        temp = rd.create_temp(ID, record_for(ID, [1, 1]))
        first = rd.finalize(make_part(temp, 0, b"1"), name)
        second = rd.finalize(make_part(temp, 1, b"2"), name)
        for final in (first, second):
            assert final.parent == rd.root
            assert final.name.endswith(".mp4")
            assert units(str(final)) <= max_path
            assert fn.sanitize_filename(final.name) == final.name
        assert second.name.endswith(" (1).mp4")
        assert key(first.name) != key(second.name)
        assert first.read_bytes() == b"1"
        assert second.read_bytes() == b"2"


def test_long_name_fits_path_limit_in_fallback_mode(storage, fn, root, monkeypatch, record_for):
    max_path = limit_root(storage, monkeypatch, root, extra=storage.TEMP_PATH_RESERVE)
    with storage.ReceiveDir.open(root) as rd:
        temp = rd.create_temp(ID, record_for(ID, [1, 1]))
        parts = [make_part(temp, i, bytes([i])) for i in range(2)]
        reject_names(monkeypatch, errno.EILSEQ)
        finals = [rd.finalize(p, LONG_UNICODE) for p in parts]
        for i, final in enumerate(finals):
            assert final.name.isascii()
            assert final.name.endswith(".mp4")
            assert units(str(final)) <= max_path
            assert fn.sanitize_filename(final.name) == final.name
            assert final.read_bytes() == bytes([i])
        assert key(finals[0].name) != key(finals[1].name)


def test_name_respects_max_name_bytes(storage, fn, root, monkeypatch, record_for):
    limit_root(storage, monkeypatch, root, extra=10_000, max_name_bytes=100)
    with storage.ReceiveDir.open(root) as rd:
        temp = rd.create_temp(ID, record_for(ID, [1]))
        final = rd.finalize(make_part(temp, 0, b"x"), LONG_UNICODE)
        assert len(final.name.encode("utf-8")) <= 100
        assert final.name.endswith(".mp4")


# ---------------------------------------------------------------------------------------------
# AC-23 Dùng sai API
# ---------------------------------------------------------------------------------------------


def test_finalize_part_of_other_receive_dir_raises(storage, tmp_path, record_for):
    with storage.ReceiveDir.open(tmp_path / "a") as rd1, storage.ReceiveDir.open(tmp_path / "b") as rd2:
        part = make_part(rd2.create_temp(ID, record_for(ID, [3])), 0, b"abc")
        with pytest.raises(ValueError):
            rd1.finalize(part, "a.txt")
        assert not part.closed
        assert os.listdir(rd1.root) == []
        assert entries(rd2.root) == []
        final = rd2.finalize(part, "a.txt")
        assert final.read_bytes() == b"abc"


def test_finalize_closed_part_raises(rd, new_temp):
    temp = new_temp([3])
    part = make_part(temp, 0, b"abc")
    part.close()
    with pytest.raises(ValueError):
        rd.finalize(part, "a.txt")
    assert entries(rd.root) == []
    assert part.path.read_bytes() == b"abc"


def test_finalize_twice_raises_and_index_stays_used(rd, new_temp):
    temp = new_temp([3])
    part = make_part(temp, 0, b"abc")
    final = rd.finalize(part, "a.txt")
    with pytest.raises(ValueError):
        rd.finalize(part, "b.txt")
    with pytest.raises(ValueError):
        temp.create_part(0, 3)  # file_index đã dùng, kể cả đã finalize
    assert entries(rd.root) == ["a.txt"]
    assert final.read_bytes() == b"abc"


@pytest.mark.parametrize("mtime_ms", [-1, True, False, 1.5, "1", 1e12])
def test_finalize_rejects_bad_mtime_before_touching_part(rd, new_temp, mtime_ms):
    temp = new_temp([3])
    part = make_part(temp, 0, b"abc")
    with pytest.raises(ValueError):
        rd.finalize(part, "a.txt", mtime_ms=mtime_ms)
    assert not part.closed
    assert entries(rd.root) == []
    assert rd.finalize(part, "a.txt").read_bytes() == b"abc"


@pytest.mark.parametrize("name", [None, b"a.txt", 1, Path("a.txt")])
def test_finalize_rejects_non_str_name(rd, new_temp, name):
    temp = new_temp([3])
    part = make_part(temp, 0, b"abc")
    with pytest.raises(TypeError):
        rd.finalize(part, name)
    assert not part.closed
    assert entries(rd.root) == []


def test_finalize_incomplete_part_raises_storage_error(storage, rd, new_temp):
    temp = new_temp([2 * BS + 5])
    size = 2 * BS + 5
    data = payload(size)
    part = temp.create_part(0, size)
    part.write_at(0, data[:BS])  # thiếu khối 1 và 2
    with pytest.raises(storage.StorageError):
        rd.finalize(part, "a.txt")
    assert entries(rd.root) == []
    assert os.path.getsize(part.path) == BS


def test_closed_receive_dir_rejects_every_call(storage, root, record_for):
    rd = storage.ReceiveDir.open(root)
    temp = rd.create_temp(ID, record_for(ID, [3, 1]))
    part = make_part(temp, 0, b"abc")
    rd.close()
    with pytest.raises(ValueError):
        rd.create_temp(ID2, record_for(ID2, [1]))
    with pytest.raises(ValueError):
        rd.finalize(part, "a.txt")
    with pytest.raises(ValueError):
        rd.volume_info()
    with pytest.raises(ValueError):
        rd.remove_temp(temp)
    with pytest.raises(ValueError):
        temp.create_part(1, 1)
    assert entries(root) == []


def test_removed_temp_rejects_create_part(rd, new_temp):
    temp = new_temp([1])
    assert rd.remove_temp(temp) is True
    assert temp.closed
    with pytest.raises(ValueError):
        temp.create_part(0, 1)


# ---------------------------------------------------------------------------------------------
# Log không chứa tên file (spec mục 3.8, mvp N9)
# ---------------------------------------------------------------------------------------------


def test_logs_never_contain_received_names(storage, fn, rd, new_temp, monkeypatch, caplog, quick_retry):
    temp = new_temp([1, 1])
    caplog.set_level(logging.DEBUG)
    secret = "BiMatTuyetDoi-Ảnh riêng.txt"
    forms = {secret, fn.sanitize_filename(secret), fn.fallback_filename(secret), "BiMatTuyetDoi"}
    # Đường tên dự phòng (log INFO) …
    with pytest.MonkeyPatch.context() as mp:
        reject_names(mp, errno.EILSEQ)
        final = rd.finalize(make_part(temp, 0, b"1"), secret)
    forms.add(final.name)
    # … và đường đổi tên lỗi phải dọn file giữ chỗ.
    part = make_part(temp, 1, b"2")

    def broken(real, args, kwargs):
        raise PermissionError(errno.EACCES, "dang bi giu")

    with pytest.MonkeyPatch.context() as mp:
        install_rename(storage, mp, broken)
        with pytest.raises(PermissionError):
            rd.finalize(part, secret)
    rd.remove_temp(temp)
    formatter = logging.Formatter("%(message)s")
    for record in caplog.records:
        text = formatter.format(record)  # gồm cả traceback nếu log kèm exc_info
        for form in forms:
            assert form not in text, (record.name, record.levelname, text)


# ---------------------------------------------------------------------------------------------
# AC-29 Đồng thời
# ---------------------------------------------------------------------------------------------


def test_two_threads_finalizing_same_name(rd, record_for):
    def sizes(k: int) -> list[int]:
        return [len(f"luot {k} file {i}\n".encode()) * (i + 1) for i in range(20)]

    temps = [rd.create_temp(ID, record_for(ID, sizes(0))), rd.create_temp(ID2, record_for(ID2, sizes(1)))]
    results: list[list[tuple[Path, bytes]]] = [[], []]
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(k: int) -> None:
        try:
            barrier.wait(timeout=30)
            for i in range(20):
                data = f"luot {k} file {i}\n".encode() * (i + 1)
                part = make_part(temps[k], i, data)
                results[k].append((rd.finalize(part, "a.txt"), data))
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    done = results[0] + results[1]
    assert len(done) == 40
    assert len({key(p.name) for p, _ in done}) == 40
    for path, data in done:
        assert path.parent == rd.root
        assert path.read_bytes() == data
    names = entries(rd.root)
    assert len(names) == 40
    assert all(os.path.getsize(rd.root / n) > 0 for n in names)


# ---------------------------------------------------------------------------------------------
# H1 Chọn tên (thuộc tính)
# ---------------------------------------------------------------------------------------------

ANY_CHAR = st.characters(exclude_categories=())
TRICKY_PIECES = st.sampled_from(
    [
        "/",
        "\\",
        ":",
        "..",
        ".",
        " ",
        "C:",
        "CON",
        "nul",
        "Com1",
        ".txt",
        ".teleport",
        "\u202e",
        "\u200b",
        "\x00",
        "\n",
        "\ud800",
        "e\u0301",
        "é",
        "É",
        "ệ",
        "😀",
        "\U00018df3",
        "a",
        "A",
        "\u00df",
        "SS",
        " (1)",
    ]
)
NAMES = st.one_of(
    st.text(ANY_CHAR, max_size=40),
    st.lists(st.one_of(ANY_CHAR, TRICKY_PIECES), max_size=30).map("".join),
    st.sampled_from(["a.txt", "A.TXT", "a (1).txt", "", ".", "CON", "x" * 251 + ".txt"]),
)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_property_finalize_never_overwrites_and_names_are_distinct(storage, fn, data, record_for):
    names = data.draw(st.lists(NAMES, min_size=1, max_size=8), label="names")
    preexisting = data.draw(st.sets(st.integers(min_value=0, max_value=len(names) - 1)), label="pre")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        root = Path(d) / "recv"
        root.mkdir()
        existing: dict[str, tuple[bytes, int]] = {}
        seen_keys: set[str] = set()
        for idx in sorted(preexisting):
            base = fn.sanitize_filename(names[idx])
            flips = data.draw(st.lists(st.booleans(), min_size=len(base), max_size=len(base)))
            variant = "".join(c.swapcase() if f else c for c, f in zip(base, flips, strict=True))
            if key(variant) in seen_keys:
                continue
            content = f"co san {idx}".encode()
            try:
                with open(root / variant, "xb") as f:
                    f.write(content)
            except OSError:
                continue  # hệ điều hành từ chối tên lúc dựng: bỏ
            seen_keys.add(key(variant))
            existing[variant] = (content, os.stat(root / variant).st_ino)
        count_before = len(os.listdir(root))
        finals: list[tuple[Path, bytes]] = []
        with storage.ReceiveDir.open(root) as rd:
            root_abs = rd.root
            sizes = [len(f"file {i} ".encode()) + 8 for i in range(len(names))]
            temp = rd.create_temp(ID, record_for(ID, sizes))
            for i, name in enumerate(names):
                content = f"file {i} ".encode() + os.urandom(8)
                finals.append((rd.finalize(make_part(temp, i, content), name), content))
            assert rd.remove_temp(temp) is True
            assert os.listdir(root / ".teleport") == []
        for path, content in finals:
            assert path.parent == root_abs
            assert path.read_bytes() == content
            assert fn.sanitize_filename(path.name) == path.name
        final_keys = [key(p.name) for p, _ in finals]
        assert len(set(final_keys)) == len(final_keys)
        assert not set(final_keys) & seen_keys
        for variant, (content, ino) in existing.items():
            assert (root / variant).read_bytes() == content
            assert os.stat(root / variant).st_ino == ino
        after = [n for n in os.listdir(root) if n != ".teleport"]
        assert len(after) == count_before + len(names)


def test_finalized_content_hash_matches_reread(rd, new_temp):
    temp = new_temp([2 * BS + 17])
    # Đọc lại rồi finalize: nội dung cuối đúng với hash đã đọc lại (chuỗi P2-6 sẽ dùng).
    data = payload(2 * BS + 17)
    part = make_part(temp, 0, data)
    digests = bytes(part.reread())
    final = rd.finalize(part, "a.bin")
    content = final.read_bytes()
    expected = b"".join(hashlib.sha256(content[i : i + BS]).digest() for i in range(0, len(content), BS))
    assert digests == expected
