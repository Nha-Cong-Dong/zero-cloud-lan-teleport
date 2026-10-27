"""Test `_write_atomic_in` và handle canh Windows của `teleport.app.storage` (P3-3a, việc V2).

Viết từ đặc tả, không từ code: `specs/spec-p3-3a.md` mục 2.2 (`_write_atomic_in`, `_fsync_dir`, seam
mới), mục 4 (V2 “Xong khi”), mục 8.2 (handle canh Windows, A1), `specs/spec-p3.md` mục 2.2 (regex bố
cục) và H1.

Luật kiểm ở đây:
- `_write_atomic_in(temp, name, chunks)` chỉ nhận `transfer.json` hoặc `<i>.state` (i 0…999), khác →
  `ValueError`, không đụng đĩa; ghi nội dung nối các đoạn, trả `(st_dev, st_ino)` của file mới.
- Tên tạm `"{name}.{16 hex}.tmp"` khớp regex bố cục, tạo bằng `O_CREAT | O_EXCL | O_NOFOLLOW`
  (Windows thêm `O_BINARY`), quyền `0o600`; ngẫu nhiên mỗi lần.
- Sập ở `_before_atomic_replace` (chép cây thư mục trong seam) → bản cũ nguyên, chỉ thêm `.tmp` đã
  đủ nội dung mới. Lỗi đổi tên → `.tmp` bị xóa, `OSError` bay ra nguyên trạng; `PermissionError` thử
  lại (`_move_file_ex` trên Windows, `os.replace` trên POSIX).
- Tên đích là symlink → chỉ liên kết bị thay; file nạn nhân không đổi byte.
- POSIX: `fsync` thư mục `<transfer_id>` qua seam `_fsync_dir` sau khi đổi tên.
- Windows (mục 8.2): khi `TransferTemp` còn mở, `<transfer_id>` và `.teleport` không đổi tên / xóa /
  thay bằng junction được; file bên trong vẫn đổi tên được (cả `.part` → thư mục nhận). A1: không lấy
  được handle canh → không xóa, không cắt gì theo tên.

Không mở socket. Module import trong fixture: chưa có thì chỉ test dùng nó đỏ.
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import itertools
import os
import re
import stat
import sys
import time
from pathlib import Path

import pytest

ID = "0123456789abcdef0123456789abcdef"
ID2 = "fedcba9876543210fedcba9876543210"
WINDOWS = sys.platform == "win32"

posix_only = pytest.mark.skipif(WINDOWS, reason="nhánh POSIX (dir_fd, fsync thư mục)")
windows_only = pytest.mark.skipif(not WINDOWS, reason="chỉ Windows (handle canh, MoveFileExW)")


@pytest.fixture(scope="module")
def storage():
    return importlib.import_module("teleport.app.storage")


@pytest.fixture
def root(tmp_path):
    return tmp_path / "recv"


@pytest.fixture
def rd(storage, root):
    with storage.ReceiveDir.open(root) as receive_dir:
        yield receive_dir


@pytest.fixture
def temp(rd, record_for):
    return rd.create_temp(ID, record_for(ID, [5]))


@pytest.fixture
def quick_retry(storage, monkeypatch):
    monkeypatch.setattr(storage, "REPLACE_DELAY", 0.001)


def make_symlink(target, link, *, is_dir: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as e:  # Windows cần quyền (WinError 1314)
        pytest.skip(f"không tạo được symlink: {e}")


def tmp_entries(directory) -> list[str]:
    return sorted(n for n in os.listdir(directory) if n.endswith(".tmp"))


def install_atomic_rename(storage, monkeypatch, behavior) -> list[str]:
    """Thay bước đổi tên `.tmp` → đích của `_write_atomic_in`: Windows `_move_file_ex`, POSIX
    `os.replace`. Chỉ lần đổi tên có nguồn `.tmp` đi qua `behavior(real, args, kwargs)`; trả danh sách
    tên đích của các lần đó."""
    owner, attr = (storage, "_move_file_ex") if WINDOWS else (os, "replace")
    real = getattr(owner, attr)
    seen: list[str] = []

    def wrapper(*args, **kwargs):
        if not os.fsdecode(args[0]).endswith(".tmp"):
            return real(*args, **kwargs)
        seen.append(os.path.basename(os.fsdecode(args[1])))
        return behavior(real, args, kwargs)

    monkeypatch.setattr(owner, attr, wrapper)
    return seen


# ---------------------------------------------------------------------------------------------
# Hằng, regex bố cục
# ---------------------------------------------------------------------------------------------


def test_constants(storage):
    assert storage.TEMP_RETENTION == 7 * 24 * 3600.0
    assert storage.STATE_READ_CHUNK == 1 << 20
    assert issubclass(storage.NotResumableError, storage.StorageError)
    assert not issubclass(storage.NotResumableError, OSError)


LAYOUT_GOOD = [
    "transfer.json",
    "0.part",
    "0.state",
    "9.part",
    "10.state",
    "999.part",
    "999.state",
    "transfer.json.0123456789abcdef.tmp",
    "0.state.0123456789abcdef.tmp",
    "999.state.ffffffffffffffff.tmp",
]
LAYOUT_BAD = [
    "transfer.json\n",
    "0.part\n",
    "0.state.0123456789abcdef.tmp\n",
    "1000.part",
    "01.part",
    "00.state",
    "-1.part",
    "０.part",  # chữ số toàn chiều rộng: `\d` khớp, `[0-9]` thì không
    "٠.part",
    "0.part.0123456789abcdef.tmp",  # `.part` không bao giờ có bản tạm
    "transfer.json.0123456789ABCDEF.tmp",
    "transfer.json.0123456789abcde.tmp",
    "transfer.json.0123456789abcdef0.tmp",
    "transfer.json.tmp",
    "TRANSFER.JSON",
    "0.PART",
    "transfer.jsonx",
    "x0.part",
    ".state",
    "",
]


@pytest.mark.parametrize("name", LAYOUT_GOOD + LAYOUT_BAD, ids=repr)
def test_layout_regex_is_spec_regex_with_fullmatch(storage, resume_helpers, name):
    # spec-p3 mục 2.2; spec-p3-3a mục 2.2: luôn `fullmatch` (`$` của `match` khớp trước `\n` cuối).
    expected = resume_helpers.LAYOUT_RE.fullmatch(name) is not None
    assert expected is (name in LAYOUT_GOOD)
    pattern = storage._LAYOUT_RE
    assert (pattern.fullmatch(name) is not None) is expected


def test_layout_regex_documented_in_architecture(resume_helpers):
    # Dữ liệu mẫu trong docs phải khớp: architecture mục 4 (gạch Bố cục) chép đúng regex của spec.
    text = (Path(__file__).resolve().parents[2] / "docs" / "architecture.md").read_text(encoding="utf-8")
    assert resume_helpers.LAYOUT_PATTERN in text


# ---------------------------------------------------------------------------------------------
# Đường đi đúng, tên sai
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["transfer.json", "0.state", "999.state"])
def test_write_atomic_in_writes_joined_chunks_and_returns_identity(storage, temp, name):
    chunks = [b"ab", bytearray(b"cd"), memoryview(b"ef"), b""]
    result = storage._write_atomic_in(temp, name, chunks)
    path = temp.path / name
    assert path.read_bytes() == b"abcdef"
    st = os.stat(path)
    assert result == (None if st.st_ino == 0 else (st.st_dev, st.st_ino))
    assert tmp_entries(temp.path) == []


def test_write_atomic_in_accepts_generator_and_many_chunks(storage, temp):
    pieces = [bytes([i % 256]) * 1000 for i in range(300)]
    storage._write_atomic_in(temp, "0.state", (p for p in pieces))
    assert (temp.path / "0.state").read_bytes() == b"".join(pieces)


def test_write_atomic_in_replaces_existing_content(storage, temp):
    storage._write_atomic_in(temp, "0.state", [b"cu" * 100])
    storage._write_atomic_in(temp, "0.state", [b"moi"])
    assert (temp.path / "0.state").read_bytes() == b"moi"


BAD_NAMES = [
    "0.part",
    "x",
    "../transfer.json",
    "..\\transfer.json",
    "transfer.json.tmp",
    "transfer.json.0123456789abcdef.tmp",
    "1000.state",
    "01.state",
    "0.state\n",
    "transfer.json\n",
    "TRANSFER.JSON",
    "0.STATE",
    "",
    ".state",
    "-1.state",
    "/abs/transfer.json",
    "C:\\transfer.json",
]


@pytest.mark.parametrize("name", BAD_NAMES, ids=repr)
def test_write_atomic_in_rejects_names_outside_layout(storage, temp, resume_helpers, tmp_path, name):
    before = resume_helpers.snapshot(tmp_path)
    with pytest.raises(ValueError):
        storage._write_atomic_in(temp, name, [b"x"])
    assert resume_helpers.snapshot(tmp_path) == before


# ---------------------------------------------------------------------------------------------
# Tên tạm, cờ mở, quyền
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["transfer.json", "7.state"])
def test_temp_name_matches_layout_regex_and_is_random(storage, temp, resume_helpers, monkeypatch, name):
    seen: list[list[str]] = []

    def before_replace(*args):
        seen.append(tmp_entries(temp.path))

    monkeypatch.setattr(storage, "_before_atomic_replace", before_replace)
    storage._write_atomic_in(temp, name, [b"1"])
    storage._write_atomic_in(temp, name, [b"2"])
    assert len(seen) == 2 and all(len(s) == 1 for s in seen), seen
    own = re.compile(re.escape(name) + r"\.[0-9a-f]{16}\.tmp")
    for (tmp,) in seen:
        assert resume_helpers.LAYOUT_RE.fullmatch(tmp), tmp
        assert own.fullmatch(tmp), tmp
    assert seen[0] != seen[1], "phần 16 hex phải ngẫu nhiên mỗi lần"


def test_before_atomic_replace_seam_receives_temp_path_and_name(storage, temp, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(storage, "_before_atomic_replace", lambda *args: calls.append(args))
    storage._write_atomic_in(temp, "3.state", [b"x"])
    assert calls == [(temp.path, "3.state")]


def test_temp_file_is_opened_exclusively_without_following_links(storage, temp, monkeypatch):
    real_open = os.open
    opens: list[tuple] = []

    def spy(path, flags, *args, **kwargs):
        if not isinstance(path, int) and os.fsdecode(path).endswith(".tmp"):
            opens.append((os.path.basename(os.fsdecode(path)), flags, args, kwargs))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    storage._write_atomic_in(temp, "transfer.json", [b"x"])
    monkeypatch.undo()
    assert len(opens) == 1, opens
    _, flags, args, kwargs = opens[0]
    assert flags & os.O_CREAT and flags & os.O_EXCL and flags & os.O_WRONLY
    if WINDOWS:
        assert flags & os.O_BINARY
    else:
        assert flags & os.O_NOFOLLOW
        assert type(kwargs.get("dir_fd")) is int, "POSIX: tạo `.tmp` qua dir_fd của thư mục tạm"
    mode = args[0] if args else kwargs.get("mode")
    assert mode == 0o600


@posix_only
def test_posix_new_file_has_mode_600(storage, temp):
    storage._write_atomic_in(temp, "0.state", [b"x"])
    assert stat.S_IMODE(os.stat(temp.path / "0.state").st_mode) == 0o600


# ---------------------------------------------------------------------------------------------
# Sập, lỗi, thử lại
# ---------------------------------------------------------------------------------------------


def test_crash_before_replace_keeps_old_file_and_only_adds_tmp(
    storage, temp, rd, resume_helpers, monkeypatch, tmp_path
):
    old = b"ban cu " * 50
    new = b"ban moi " * 70
    storage._write_atomic_in(temp, "0.state", [old])
    before = resume_helpers.snapshot(rd.root)
    crash = tmp_path / "sap"

    def before_replace(*args):
        resume_helpers.crash_copy(rd.root, crash)

    monkeypatch.setattr(storage, "_before_atomic_replace", before_replace)
    storage._write_atomic_in(temp, "0.state", [new[:100], new[100:]])
    after_crash = resume_helpers.snapshot(crash)
    extra = {k: v for k, v in after_crash.items() if k not in before}
    assert {k: v for k, v in after_crash.items() if k in before} == before, "bản cũ phải nguyên"
    assert len(extra) == 1, extra
    ((key, value),) = extra.items()
    assert re.fullmatch(rf"/\.teleport/{ID}/0\.state\.[0-9a-f]{{16}}\.tmp", key), key
    assert value[:3] == ("file", len(new), hashlib.sha256(new).hexdigest()), (
        "`.tmp` đã đủ nội dung trước khi đổi tên"
    )
    assert (temp.path / "0.state").read_bytes() == new  # không sập thì xong bình thường


def test_replace_error_removes_tmp_and_propagates(storage, temp, monkeypatch, quick_retry):
    old = (temp.path / "transfer.json").read_bytes()

    def broken(real, args, kwargs):
        raise OSError(errno.EIO, "loi I/O")

    calls = install_atomic_rename(storage, monkeypatch, broken)
    with pytest.raises(OSError) as exc:
        storage._write_atomic_in(temp, "transfer.json", [b"moi"])
    assert exc.value.errno == errno.EIO
    assert calls == ["transfer.json"], "lỗi khác PermissionError không thử lại"
    assert tmp_entries(temp.path) == []
    assert (temp.path / "transfer.json").read_bytes() == old


def test_permission_error_on_replace_is_retried(storage, temp, monkeypatch):
    monkeypatch.setattr(storage, "REPLACE_DELAY", 0.02)
    stamps: list[float] = []

    def flaky(real, args, kwargs):
        stamps.append(time.perf_counter())
        if len(stamps) <= 2:
            raise PermissionError(errno.EACCES, "dang bi giu")
        return real(*args, **kwargs)

    install_atomic_rename(storage, monkeypatch, flaky)
    storage._write_atomic_in(temp, "0.state", [b"sau khi thu lai"])
    assert len(stamps) == 3
    assert all(b - a >= 0.015 for a, b in itertools.pairwise(stamps))
    assert (temp.path / "0.state").read_bytes() == b"sau khi thu lai"
    assert tmp_entries(temp.path) == []


def test_permission_error_forever_gives_up_and_removes_tmp(storage, temp, monkeypatch, quick_retry):
    monkeypatch.setattr(storage, "REPLACE_ATTEMPTS", 3)

    def locked(real, args, kwargs):
        raise PermissionError(errno.EACCES, "dang bi giu")

    calls = install_atomic_rename(storage, monkeypatch, locked)
    with pytest.raises(PermissionError):
        storage._write_atomic_in(temp, "0.state", [b"x"])
    assert len(calls) == 3
    assert tmp_entries(temp.path) == []
    assert not os.path.lexists(temp.path / "0.state")


def test_fsync_error_of_tmp_removes_tmp_and_propagates(storage, temp, monkeypatch):
    # Bước (2): ghi các đoạn rồi `_full_fsync` (seam) `.tmp`; lỗi → xóa `.tmp`, OSError nguyên trạng.
    def failing_fsync(fd):
        raise OSError(errno.EIO, "fsync loi")

    monkeypatch.setattr(storage, "_full_fsync", failing_fsync)
    with pytest.raises(OSError) as exc:
        storage._write_atomic_in(temp, "0.state", [b"x" * 100])
    monkeypatch.undo()
    assert exc.value.errno == errno.EIO
    assert tmp_entries(temp.path) == []
    assert not os.path.lexists(temp.path / "0.state")


def test_symlink_at_target_name_replaces_only_the_link(storage, temp, tmp_path):
    victim = tmp_path / "nan-nhan.json"
    victim.write_bytes(b"cua nan nhan")
    link = temp.path / "transfer.json"
    os.unlink(link)
    make_symlink(victim, link, is_dir=False)
    storage._write_atomic_in(temp, "transfer.json", [b"moi"])
    assert not os.path.islink(link)
    assert stat.S_ISREG(os.lstat(link).st_mode)
    assert link.read_bytes() == b"moi"
    assert victim.read_bytes() == b"cua nan nhan"


@windows_only
def test_symlink_at_target_name_on_windows_junction_like_dir_is_not_followed(storage, temp, tmp_path):
    # Một thư mục (junction tới thư mục nạn nhân) mang tên đích: đổi tên lên đó phải lỗi, không ghi vào
    # thư mục nạn nhân.
    import _winapi

    victim = tmp_path / "nan-nhan"
    victim.mkdir()
    (victim / "giu.txt").write_bytes(b"x")
    os.unlink(temp.path / "transfer.json")
    _winapi.CreateJunction(str(victim), str(temp.path / "transfer.json"))
    with pytest.raises(OSError):
        storage._write_atomic_in(temp, "transfer.json", [b"moi"])
    assert sorted(os.listdir(victim)) == ["giu.txt"]
    assert tmp_entries(temp.path) == []


@windows_only
def test_windows_replace_uses_move_file_ex_with_replace_and_write_through(storage, temp, monkeypatch):
    real = storage._move_file_ex
    calls: list[tuple] = []

    def spy(src, dst, flags):
        calls.append((os.path.basename(src), os.path.basename(dst), flags))
        return real(src, dst, flags)

    monkeypatch.setattr(storage, "_move_file_ex", spy)
    storage._write_atomic_in(temp, "transfer.json", [b"x"])
    assert len(calls) == 1
    src, dst, flags = calls[0]
    assert src.startswith("transfer.json.") and src.endswith(".tmp")
    assert dst == "transfer.json"
    assert flags == storage.MOVEFILE_REPLACE_EXISTING | storage.MOVEFILE_WRITE_THROUGH == 0x9


@posix_only
def test_posix_fsyncs_temp_dir_through_fsync_dir_after_replace(storage, temp, monkeypatch):
    target = temp.path / "0.state"
    temp_id = (os.stat(temp.path).st_dev, os.stat(temp.path).st_ino)
    seen: list[tuple] = []
    real = storage._fsync_dir

    def spy(fd):
        st = os.fstat(fd)
        content = target.read_bytes() if target.exists() else None
        seen.append(((st.st_dev, st.st_ino), content))
        return real(fd)

    monkeypatch.setattr(storage, "_fsync_dir", spy)
    storage._write_atomic_in(temp, "0.state", [b"moi"])
    assert (temp_id, b"moi") in seen, "phải fsync <transfer_id> sau khi đổi tên"


@posix_only
def test_posix_fsync_dir_errors_are_ignored(storage, temp, monkeypatch):
    real_fsync = os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "khong ho tro")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    storage._write_atomic_in(temp, "0.state", [b"x"])
    assert (temp.path / "0.state").read_bytes() == b"x"


# ---------------------------------------------------------------------------------------------
# Handle canh Windows (spec-p3-3a mục 8.2)
# ---------------------------------------------------------------------------------------------


@windows_only
def test_windows_open_temp_dir_cannot_be_renamed_or_removed(rd, temp, tmp_path):
    away = tmp_path / "di-cho-khac"
    with pytest.raises(OSError):
        os.rename(temp.path, away)
    with pytest.raises(OSError):
        os.rename(rd.root / ".teleport", away)
    assert temp.path.is_dir() and not away.exists()
    assert (temp.path / "transfer.json").is_file()
    # Xóa: làm rỗng `<id>` (file bên trong vẫn xóa được) rồi `rmdir` phải thất bại.
    os.unlink(temp.path / "transfer.json")
    with pytest.raises(OSError):
        os.rmdir(temp.path)
    assert temp.path.is_dir()


@windows_only
def test_windows_open_temp_dir_cannot_be_swapped_for_junction(rd, temp, tmp_path):
    import _winapi

    victim = tmp_path / "nan-nhan"
    victim.mkdir()
    moved = tmp_path / "cu"
    # Thay `<transfer_id>` bằng junction cần dời hay xóa nó trước: cả hai phải thất bại.
    os.unlink(temp.path / "transfer.json")
    for attempt in (lambda: os.rename(temp.path, moved), lambda: os.rmdir(temp.path)):
        with pytest.raises(OSError):
            attempt()
    with pytest.raises(OSError):
        _winapi.CreateJunction(str(victim), str(temp.path))
    st = os.lstat(temp.path)
    assert stat.S_ISDIR(st.st_mode) and not st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    assert os.listdir(victim) == []


@windows_only
def test_windows_files_inside_held_dirs_can_still_be_renamed(rd, record_for, resume_helpers):
    data = b"du lieu " * 1000
    temp = rd.create_temp(ID2, record_for(ID2, [len(data)]))
    part = temp.create_part(0, len(data))
    part.commit(resume_helpers.write_blocks(part, data))
    final = rd.finalize(part, "a.bin")  # `.part` → thư mục nhận, `transfer.json` thay trong `<id>`
    assert final.read_bytes() == data
    assert final.parent == rd.root


@windows_only
def test_windows_close_temp_releases_guard_handles(rd, temp, tmp_path):
    rd.close_temp(temp)
    away = tmp_path / "di-cho-khac"
    os.rename(temp.path, away)  # không còn handle canh
    assert away.is_dir()


@windows_only
def test_windows_remove_temp_releases_guard_before_rmdir(rd, temp):
    assert rd.remove_temp(temp) is True
    assert not os.path.lexists(temp.path)
    os.rename(rd.root / ".teleport", rd.root / "tam")  # `.teleport` cũng không còn bị giữ
    os.rename(rd.root / "tam", rd.root / ".teleport")


def _fail_hold(*args, **kwargs):
    raise OSError(errno.EACCES, "khong lay duoc handle canh")


@windows_only
def test_windows_a1_cleanup_without_guard_deletes_nothing(
    storage, root, record_for, resume_helpers, monkeypatch
):
    # A1: không lấy được handle canh → không xóa theo tên; để nguyên, trả như “không xóa được”.
    orphan = root / ".teleport" / ID
    orphan.mkdir(parents=True)
    (orphan / "0.part").write_bytes(b"x")
    (orphan / "0.state").write_bytes(b"y")
    old = time.time() - 30 * 24 * 3600
    for p in orphan.iterdir():
        os.utime(p, (old, old))
    before = resume_helpers.snapshot(root)
    with storage.ReceiveDir.open(root) as rd:
        monkeypatch.setattr(storage, "_hold_dir", _fail_hold)
        removed = rd.cleanup(
            time.time(), own_device_id=resume_helpers.RECEIVER, skip=frozenset(), retention=1.0
        )
    assert removed == 0
    assert resume_helpers.snapshot(root) == before


@windows_only
def test_windows_a1_create_temp_without_guard_does_not_delete_leftover_tmp(
    storage, root, record_for, resume_helpers, monkeypatch
):
    leftover = root / ".teleport" / ID
    leftover.mkdir(parents=True)
    (leftover / "transfer.json.0123456789abcdef.tmp").write_bytes(b"sot")
    before = resume_helpers.snapshot(root)
    with storage.ReceiveDir.open(root) as rd:
        monkeypatch.setattr(storage, "_hold_dir", _fail_hold)
        with pytest.raises((storage.StorageError, OSError)):
            rd.create_temp(ID, record_for(ID, [1]))
    assert (leftover / "transfer.json.0123456789abcdef.tmp").read_bytes() == b"sot"
    assert {k: v for k, v in resume_helpers.snapshot(root).items() if k in before} == before


@windows_only
def test_windows_a1_open_for_resume_without_guard_neither_deletes_nor_truncates(
    storage, root, record_for, resume_helpers, monkeypatch
):
    from teleport.core.resume_state import encode_header, encode_mark

    data = b"z" * 100
    with storage.ReceiveDir.open(root) as rd:
        temp = rd.create_temp(ID, record_for(ID, [len(data)]))
        part = temp.create_part(0, len(data))
        part.commit(resume_helpers.write_blocks(part, data))
        rd.close_temp(temp)
    tdir = root / ".teleport" / ID
    # Có việc cho pha 2: `.tmp` sót và rác sau bản ghi tốt cuối.
    (tdir / "0.state.0123456789abcdef.tmp").write_bytes(b"sot")
    with open(tdir / "0.state", "ab") as f:
        f.write(b"\xffRAC")
    assert (
        (tdir / "0.state")
        .read_bytes()
        .startswith(encode_header(ID, 0, len(data)) + encode_mark([(0, hashlib.sha256(data).digest())]))
    )
    before = resume_helpers.snapshot(root)
    with storage.ReceiveDir.open(root) as rd:
        monkeypatch.setattr(storage, "_hold_dir", _fail_hold)
        try:
            scans = rd.scan_temps(resume_helpers.RECEIVER, {resume_helpers.SENDER})
            running = [s for s in scans if s.kind == storage.TempKind.RUNNING]
            if running:
                with pytest.raises((storage.StorageError, OSError)):
                    rd.open_for_resume(running[0])
        except (storage.StorageError, OSError):
            pass
    assert resume_helpers.snapshot(root) == before
