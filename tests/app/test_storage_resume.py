"""Test đường ghi trạng thái nối lại của `teleport.app.storage` (P3-3a, việc V3).

Viết từ đặc tả, không từ code: `specs/spec-p3-3a.md` mục 2.2 (`create_temp(transfer_id, record)` H2/M7,
`create_part` + header `.state`, `ResumeLog`, `PartFile.commit`/`clear`, `finalize` bước 1–8, hủy giữ
chỗ), mục 4 (V3 “Xong khi”), mục 8.1, 8.3 (`_busy`, M7, `compact_estimate`, bước 1, F7, `ResumeLog`),
8.4 (B1); `specs/spec-p3.md` mục 2.2 (bố cục, `.state`); architecture mục 4 (định dạng `.state`, “Kết
thúc một file”, “Gộp”, trần bản ghi tên).

Nội dung `.state` được tách bằng bộ đọc độc lập `parse_state` (tests/app/conftest.py) và đối chiếu với
bộ mã hóa của `teleport.core.resume_state` (đã kiểm riêng ở tests/core/test_resume_state.py). Thứ tự
`fsync` được kiểm bằng spy trên seam `_full_fsync` / `_fsync_dir`, nhận ra file theo danh tính
`(st_dev, st_ino)`.

Không mở socket.
"""

from __future__ import annotations

import ast
import contextlib
import datetime
import errno
import hashlib
import importlib
import os
import sys
import threading
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.core import resume_state as rs
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.filenames import sanitize_filename

BS = BLOCK_SIZE
ID = "0123456789abcdef0123456789abcdef"
ID2 = "fedcba9876543210fedcba9876543210"
WINDOWS = sys.platform == "win32"
HEADER = 41

posix_only = pytest.mark.skipif(WINDOWS, reason="nhánh POSIX (fsync thư mục qua _fsync_dir)")
SRC = Path(__file__).resolve().parents[2] / "src" / "teleport" / "app" / "storage.py"


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
def quick_retry(storage, monkeypatch):
    monkeypatch.setattr(storage, "REPLACE_DELAY", 0.001)


# ---------------------------------------------------------------------------------------------
# Hàm phụ trợ
# ---------------------------------------------------------------------------------------------


def ident(path) -> tuple[int, int]:
    st_ = os.stat(path)
    return st_.st_dev, st_.st_ino


def fd_ident(fd: int) -> tuple[int, int]:
    st_ = os.fstat(fd)
    return st_.st_dev, st_.st_ino


def payload(size: int, seed: int = 1) -> bytes:
    import random

    return random.Random(seed).randbytes(size)


def state_path(part) -> Path:
    return part.path.with_name(f"{part.file_index}.state")


def replay(path, n_blocks: int):
    data = Path(path).read_bytes()
    r = rs.StateReplay(n_blocks, max_length=rs.max_state_length(n_blocks))
    r.feed(data)
    return r.finish()


def make_symlink(target, link, *, is_dir: bool) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"không tạo được symlink: {e}")


def link_dir(kind: str, target, link) -> None:
    if kind == "junction":
        if not WINDOWS:
            pytest.skip("junction chỉ có trên Windows")
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        make_symlink(target, link, is_dir=True)


def fake_hashes(indices) -> list[tuple[int, bytes]]:
    return [(i, hashlib.sha256(i.to_bytes(4, "big")).digest()) for i in indices]


# ---------------------------------------------------------------------------------------------
# API, ranh giới tầng
# ---------------------------------------------------------------------------------------------


def test_resume_mode_is_str_enum_with_three_members(storage):
    from enum import StrEnum

    assert issubclass(storage.ResumeMode, StrEnum)
    assert {m.name for m in storage.ResumeMode} == {"NORMAL", "VERIFY_TARGET", "REUSE_PLACEHOLDER"}


def test_storage_imports_no_private_names_from_core():
    # spec-p3-3a mục 8.1 (B5): `app` không import tên bắt đầu bằng `_` (dùng `compact_length_for`).
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("teleport.core"):
            bad += [f"{node.module}.{a.name}" for a in node.names if a.name.startswith("_")]
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in {"resume_state", "rs"}
            and node.attr.startswith("_")
        ):
            bad.append(f"{node.value.id}.{node.attr}")
    assert bad == []


# ---------------------------------------------------------------------------------------------
# `create_temp(transfer_id, record)`: kiểm trước I/O, ghi `transfer.json`
# ---------------------------------------------------------------------------------------------


def test_create_temp_writes_transfer_json_before_returning(rd, record_for):
    record = record_for(ID, [5, 0, 7], names=["Ảnh.jpg", "rỗng", "b.bin"])
    temp = rd.create_temp(ID, record)
    data = (temp.path / "transfer.json").read_bytes()
    assert data == rs.encode_transfer_record(record)
    assert rs.decode_transfer_record(data, dir_name=ID) == record
    assert temp.record == record
    assert os.listdir(temp.path) == ["transfer.json"]


def _bad_record(kind: str, record_for):
    if kind == "none":
        return None
    if kind == "dict":
        return {"receiver": "e" * 32}
    if kind == "id-khac":
        return record_for(ID2, [1])
    if kind == "verified":
        return record_for(ID, [1, 1], verified=1)
    if kind == "done":
        return record_for(ID, [1], verified=1)
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["none", "dict", "id-khac", "verified", "done"])
def test_create_temp_rejects_bad_record_before_any_io(rd, record_for, resume_helpers, tmp_path, kind):
    before = resume_helpers.snapshot(tmp_path)
    with pytest.raises(ValueError):
        rd.create_temp(ID, _bad_record(kind, record_for))
    assert resume_helpers.snapshot(tmp_path) == before
    assert not (rd.root / ".teleport").exists()


def test_create_temp_error_after_mkdir_cleans_up_what_it_created(storage, rd, record_for, monkeypatch):
    def broken(*args):
        raise OSError(errno.EIO, "loi khi thay transfer.json")

    monkeypatch.setattr(storage, "_before_atomic_replace", broken)
    with pytest.raises(OSError) as exc:
        rd.create_temp(ID, record_for(ID, [1]))
    assert exc.value.errno == errno.EIO
    assert not os.path.lexists(rd.root / ".teleport" / ID)
    assert (rd.root / ".teleport").is_dir()  # `.teleport` không bao giờ bị xóa
    monkeypatch.undo()
    temp = rd.create_temp(ID, record_for(ID, [1]))  # tạo lại được
    assert (temp.path / "transfer.json").is_file()


def test_create_temp_on_closed_receive_dir_mid_way_removes_new_dir(storage, root, record_for, monkeypatch):
    # spec-p3-3a mục 8.1: nhánh “ReceiveDir đã đóng” cũng dọn thư mục vừa tạo (có `transfer.json` bền).
    rd = storage.ReceiveDir.open(root)
    errors: list[BaseException] = []

    def close_meanwhile(*args):
        t = threading.Thread(target=lambda: rd.close())
        t.start()
        t.join(10)
        if t.is_alive():
            errors.append(AssertionError("close() bị chặn trong lúc create_temp làm I/O"))

    monkeypatch.setattr(storage, "_before_atomic_replace", close_meanwhile)
    with pytest.raises((ValueError, OSError)):
        rd.create_temp(ID, record_for(ID, [1]))
    monkeypatch.undo()
    assert errors == []
    assert not os.path.lexists(root / ".teleport" / ID)


# ---------------------------------------------------------------------------------------------
# H2: ma trận thư mục `<transfer_id>` có sẵn
# ---------------------------------------------------------------------------------------------


def test_h2_missing_dir_is_created(rd, record_for):
    temp = rd.create_temp(ID, record_for(ID, [1]))
    assert temp.path.is_dir()


def test_h2_empty_existing_dir_is_reused(storage, root, record_for):
    (root / ".teleport" / ID).mkdir(parents=True)
    with storage.ReceiveDir.open(root) as rd:
        temp = rd.create_temp(ID, record_for(ID, [1]))
        assert os.listdir(temp.path) == ["transfer.json"]


def test_h2_dir_with_only_layout_tmp_files_is_cleared_and_reused(storage, root, record_for):
    old = root / ".teleport" / ID
    old.mkdir(parents=True)
    for name in (
        "transfer.json.0123456789abcdef.tmp",
        "0.state.fedcba9876543210.tmp",
        "999.state.aaaaaaaaaaaaaaaa.tmp",
    ):
        (old / name).write_bytes(b"sot lai")
    with storage.ReceiveDir.open(root) as rd:
        temp = rd.create_temp(ID, record_for(ID, [1]))
        assert os.listdir(temp.path) == ["transfer.json"]


def _plant(kind: str, d: Path, outside: Path) -> None:
    if kind == "part":
        (d / "0.part").write_bytes(b"du lieu cu")
    elif kind == "transfer-json":
        (d / "transfer.json").write_bytes(b"{}")
    elif kind == "state":
        (d / "0.state").write_bytes(b"TPRS")
    elif kind == "file-la":
        (d / "la.bin").write_bytes(b"cua ai do")
    elif kind == "tmp-sai-mau":
        (d / "transfer.json.0123456789ABCDEF.tmp").write_bytes(b"x")
    elif kind == "tmp-kem-file-la":
        (d / "transfer.json.0123456789abcdef.tmp").write_bytes(b"x")
        (d / "ghi-chu.txt").write_bytes(b"y")
    elif kind == "thu-muc-con":
        (d / "con").mkdir()
    elif kind == "symlink-ten-tmp":
        make_symlink(outside, d / "transfer.json.0123456789abcdef.tmp", is_dir=False)
    elif kind == "symlink-part":
        make_symlink(outside, d / "0.part", is_dir=False)
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind",
    [
        "part",
        "transfer-json",
        "state",
        "file-la",
        "tmp-sai-mau",
        "tmp-kem-file-la",
        "thu-muc-con",
        "symlink-ten-tmp",
        "symlink-part",
    ],
)
def test_h2_dir_with_anything_else_raises_temp_exists_and_is_untouched(
    storage, tmp_path, record_for, resume_helpers, kind
):
    root = tmp_path / "recv"
    old = root / ".teleport" / ID
    old.mkdir(parents=True)
    victim = tmp_path / "nan-nhan.bin"
    victim.write_bytes(b"cua nan nhan")
    _plant(kind, old, victim)
    before = resume_helpers.snapshot(tmp_path)
    with storage.ReceiveDir.open(root) as rd, pytest.raises(storage.TempExistsError):
        rd.create_temp(ID, record_for(ID, [1]))
    assert resume_helpers.snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_h2_transfer_entry_that_is_a_link_to_empty_dir_is_refused(
    storage, tmp_path, record_for, resume_helpers, kind
):
    # Liên kết tới thư mục rỗng của nạn nhân: không được coi là “thư mục rỗng” để dùng lại.
    root = tmp_path / "recv"
    (root / ".teleport").mkdir(parents=True)
    victim = tmp_path / "nan-nhan"
    victim.mkdir()
    link_dir(kind, victim, root / ".teleport" / ID)
    before = resume_helpers.snapshot(tmp_path)
    with storage.ReceiveDir.open(root) as rd, pytest.raises(storage.TempExistsError):
        rd.create_temp(ID, record_for(ID, [1]))
    assert resume_helpers.snapshot(tmp_path) == before
    assert os.listdir(victim) == []


def test_h2_regular_file_named_like_transfer_dir_is_refused(storage, tmp_path, record_for, resume_helpers):
    root = tmp_path / "recv"
    (root / ".teleport").mkdir(parents=True)
    (root / ".teleport" / ID).write_bytes(b"file thuong")
    before = resume_helpers.snapshot(tmp_path)
    with storage.ReceiveDir.open(root) as rd, pytest.raises(storage.TempExistsError):
        rd.create_temp(ID, record_for(ID, [1]))
    assert resume_helpers.snapshot(tmp_path) == before


def test_h2_open_temp_with_same_id_raises_temp_exists(storage, rd, record_for):
    temp = rd.create_temp(ID, record_for(ID, [1]))
    with pytest.raises(storage.TempExistsError):
        rd.create_temp(ID, record_for(ID, [2]))
    assert (temp.path / "transfer.json").read_bytes() == rs.encode_transfer_record(record_for(ID, [1]))


def test_busy_id_being_created_is_exclusive(storage, rd, record_for, monkeypatch):
    # spec-p3-3a mục 8.3: dưới `_lock`, trước mọi I/O, `create_temp` kiểm `transfer_id ∉ _busy`.
    results: list[object] = []

    def second_call(*args):
        if results:
            return

        def run():
            try:
                results.append(rd.create_temp(ID, record_for(ID, [2])))
            except BaseException as e:
                results.append(e)

        t = threading.Thread(target=run)
        t.start()
        t.join(10)
        if t.is_alive():
            results.append(AssertionError("create_temp thứ hai bị chặn (khóa giữ trong lúc I/O?)"))

    monkeypatch.setattr(storage, "_before_atomic_replace", second_call)
    temp = rd.create_temp(ID, record_for(ID, [1]))
    monkeypatch.undo()
    assert len(results) == 1
    assert isinstance(results[0], storage.TempExistsError), results
    assert temp.record == record_for(ID, [1])
    assert (temp.path / "transfer.json").read_bytes() == rs.encode_transfer_record(record_for(ID, [1]))


def test_cleanup_during_create_temp_does_not_touch_busy_dir(
    storage, rd, record_for, monkeypatch, resume_helpers
):
    # `.teleport/<id>` đang được tạo chỉ có `.tmp` (như mồ côi); `cleanup` phải bỏ qua nhờ `_busy`.
    removed: list[object] = []

    def cleanup_meanwhile(*args):
        if removed:
            return

        def run():
            try:
                removed.append(
                    rd.cleanup(
                        4_000_000_000.0,
                        own_device_id=resume_helpers.RECEIVER,
                        skip=frozenset(),
                        retention=0.0,
                    )
                )
            except BaseException as e:
                removed.append(e)

        t = threading.Thread(target=run)
        t.start()
        t.join(10)
        if t.is_alive():
            removed.append(AssertionError("cleanup bị chặn trong lúc create_temp làm I/O"))

    monkeypatch.setattr(storage, "_before_atomic_replace", cleanup_meanwhile)
    temp = rd.create_temp(ID, record_for(ID, [1]))
    monkeypatch.undo()
    assert removed == [0]
    assert (temp.path / "transfer.json").is_file()


# ---------------------------------------------------------------------------------------------
# M7: fsync thư mục (POSIX, qua seam `_fsync_dir`)
# ---------------------------------------------------------------------------------------------


def _spy_fsync_dir(storage, monkeypatch, watch: dict[str, Path]) -> list[tuple[str, frozenset[str]]]:
    """Ghi (tên thư mục theo `watch`, tập mục đang có trong thư mục đó) mỗi lần `_fsync_dir(fd)`."""
    calls: list[tuple[str, frozenset[str]]] = []
    real = storage._fsync_dir

    def spy(fd):
        who = fd_ident(fd)
        for label, path in watch.items():
            if path.exists() and ident(path) == who:
                calls.append((label, frozenset(os.listdir(path))))
                break
        else:
            calls.append(("?", frozenset()))
        return real(fd)

    monkeypatch.setattr(storage, "_fsync_dir", spy)
    return calls


@posix_only
def test_m7_create_temp_fsyncs_parent_teleport_and_transfer_dir(storage, rd, record_for, monkeypatch):
    watch = {"root": rd.root, "teleport": rd.root / ".teleport", "id": rd.root / ".teleport" / ID}
    calls = _spy_fsync_dir(storage, monkeypatch, watch)
    rd.create_temp(ID, record_for(ID, [1]))
    labels = [c[0] for c in calls]
    # `.teleport` vừa được tạo → fsync thư mục nhận (cha của `.teleport`).
    assert any(lbl == "root" and ".teleport" in entries for lbl, entries in calls), calls
    # Sau `mkdir <id>` → fsync `.teleport` (đã có `<id>`).
    assert any(lbl == "teleport" and ID in entries for lbl, entries in calls), calls
    # Sau khi thay `transfer.json` → fsync `<id>`.
    assert any(lbl == "id" and "transfer.json" in entries for lbl, entries in calls), calls
    assert labels.index("teleport") < max(i for i, c in enumerate(calls) if c[0] == "id")


@posix_only
def test_m7_existing_teleport_still_fsynced_after_mkdir(storage, root, record_for, monkeypatch):
    (root / ".teleport").mkdir(parents=True, mode=0o700)
    with storage.ReceiveDir.open(root) as rd:
        watch = {"teleport": root / ".teleport", "id": root / ".teleport" / ID}
        calls = _spy_fsync_dir(storage, monkeypatch, watch)
        rd.create_temp(ID, record_for(ID, [1]))
    assert any(lbl == "teleport" and ID in entries for lbl, entries in calls), calls


@posix_only
def test_m7_create_part_fsyncs_transfer_dir_after_part_and_state(storage, rd, record_for, monkeypatch):
    temp = rd.create_temp(ID, record_for(ID, [10]))
    calls = _spy_fsync_dir(storage, monkeypatch, {"id": temp.path})
    temp.create_part(0, 10)
    assert any(lbl == "id" and {"0.part", "0.state"} <= entries for lbl, entries in calls), calls


# ---------------------------------------------------------------------------------------------
# `create_part`: header `.state`, kiểm theo `record`
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("index", "size"), [(0, 0), (0, 1), (2, 3 * BS + 5), (999, BS)])
def test_create_part_writes_state_header_and_opens_log(storage, rd, record_for, index, size):
    temp = rd.create_temp(ID, record_for(ID, {index: size}))
    part = temp.create_part(index, size)
    header = rs.encode_header(ID, index, size)
    assert state_path(part).read_bytes() == header
    assert rs.check_header(header, transfer_id=ID, file_index=index, size=size, block_size=BS)
    assert part.mode == storage.ResumeMode.NORMAL
    assert part.log.length == HEADER and part.log.name_records == 0 and not part.log.closed
    assert os.path.getsize(part.path) == 0


@pytest.mark.parametrize(("index", "size"), [(2, 1), (5, 1), (0, 2), (1, 0), (0, BS)])
def test_create_part_rejects_index_or_size_not_in_record(storage, rd, record_for, index, size):
    temp = rd.create_temp(ID, record_for(ID, [1, 1]))
    with pytest.raises(ValueError):
        temp.create_part(index, size)
    assert os.listdir(temp.path) == ["transfer.json"]


# ---------------------------------------------------------------------------------------------
# `PartFile.commit`: fsync `.part` → `MARK` → fsync `.state`
# ---------------------------------------------------------------------------------------------


def _spy_full_fsync(storage, monkeypatch, part) -> list[tuple[str, int]]:
    """Ghi (`part`/`state`/`?`, cỡ `.state` lúc đó) mỗi lần `_full_fsync(fd)`."""
    calls: list[tuple[str, int]] = []
    real = storage._full_fsync
    part_id, state_id = ident(part.path), ident(state_path(part))
    spath = state_path(part)

    def spy(fd):
        who = fd_ident(fd)
        label = "part" if who == part_id else "state" if who == state_id else "?"
        calls.append((label, os.path.getsize(spath)))
        return real(fd)

    monkeypatch.setattr(storage, "_full_fsync", spy)
    return calls


def test_commit_fsyncs_part_then_appends_mark_then_fsyncs_state(
    storage, rd, record_for, resume_helpers, monkeypatch
):
    data = payload(2 * BS + 9)
    temp = rd.create_temp(ID, record_for(ID, [len(data)]))
    part = temp.create_part(0, len(data))
    blocks = resume_helpers.write_blocks(part, data)
    calls = _spy_full_fsync(storage, monkeypatch, part)
    part.commit(blocks)
    mark = rs.encode_mark(blocks)
    assert calls == [("part", HEADER), ("state", HEADER + len(mark))]
    assert state_path(part).read_bytes() == rs.encode_header(ID, 0, len(data)) + mark
    assert part.log.length == HEADER + len(mark)


def test_commit_of_no_blocks_only_fsyncs_part(storage, rd, record_for, monkeypatch):
    temp = rd.create_temp(ID, record_for(ID, [5]))
    part = temp.create_part(0, 5)
    calls = _spy_full_fsync(storage, monkeypatch, part)
    part.commit([])
    assert calls == [("part", HEADER)]
    assert state_path(part).read_bytes() == rs.encode_header(ID, 0, 5)


def test_commit_splits_marks_at_65535_blocks(storage, rd, record_for, resume_helpers):
    n = 70_000
    temp = rd.create_temp(ID, record_for(ID, [n * BS]))
    part = temp.create_part(0, n * BS)  # không ghi gì: commit chỉ cần `.part` fsync được
    part.commit(fake_hashes(range(n)))
    _, records = resume_helpers.parse_state(state_path(part).read_bytes())
    marks = [r for r in records if r[0] == "mark"]
    assert all(1 <= len(m[1]) <= 65_535 for m in marks)
    assert sorted(i for m in marks for i, _ in m[1]) == list(range(n))


def test_commit_never_appends_mark_after_failed_part_fsync(
    storage, rd, record_for, resume_helpers, monkeypatch
):
    data = payload(BS + 3)
    temp = rd.create_temp(ID, record_for(ID, [len(data)]))
    part = temp.create_part(0, len(data))
    blocks = resume_helpers.write_blocks(part, data)
    before = state_path(part).read_bytes()
    part_id = ident(part.path)
    real = storage._full_fsync

    def failing(fd):
        if fd_ident(fd) == part_id:
            raise OSError(errno.EIO, "fsync .part loi")
        return real(fd)

    monkeypatch.setattr(storage, "_full_fsync", failing)
    with pytest.raises(OSError) as exc:
        part.commit(blocks)
    assert exc.value.errno == errno.EIO
    assert state_path(part).read_bytes() == before
    monkeypatch.undo()  # fsync lần sau thành công vẫn không được nối MARK (architecture mục 4)
    with pytest.raises(storage.StorageError):
        part.commit(blocks)
    with pytest.raises(storage.StorageError):
        part.commit([])
    assert state_path(part).read_bytes() == before


def test_failed_state_fsync_breaks_the_log(storage, rd, record_for, resume_helpers, monkeypatch):
    data = payload(BS + 3)
    temp = rd.create_temp(ID, record_for(ID, [len(data)]))
    part = temp.create_part(0, len(data))
    blocks = resume_helpers.write_blocks(part, data)
    state_id = ident(state_path(part))
    real = storage._full_fsync

    def failing(fd):
        if fd_ident(fd) == state_id:
            raise OSError(errno.EIO, "fsync .state loi")
        return real(fd)

    monkeypatch.setattr(storage, "_full_fsync", failing)
    with pytest.raises(OSError):
        part.commit(blocks)
    monkeypatch.undo()
    after_failure = state_path(part).read_bytes()
    with pytest.raises(storage.StorageError):
        part.log.append(rs.encode_clear_all())
    with pytest.raises(storage.StorageError):
        part.log.sync()
    with pytest.raises(storage.StorageError):
        part.commit(blocks)
    assert state_path(part).read_bytes() == after_failure


def test_short_writes_to_state_are_continued(storage, rd, record_for, monkeypatch):
    temp = rd.create_temp(ID, record_for(ID, [20 * BS]))
    part = temp.create_part(0, 20 * BS)
    state_id = ident(state_path(part))
    real_write = os.write

    def short(fd, data):
        if fd_ident(fd) == state_id:
            return real_write(fd, bytes(memoryview(data)[:3]))
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", short)
    blocks = fake_hashes(range(20))
    part.commit(blocks)
    monkeypatch.undo()
    assert state_path(part).read_bytes() == rs.encode_header(ID, 0, 20 * BS) + rs.encode_mark(blocks)


# ---------------------------------------------------------------------------------------------
# `PartFile.clear`
# ---------------------------------------------------------------------------------------------


def test_clear_appends_clear_without_fsync(storage, rd, record_for, monkeypatch):
    temp = rd.create_temp(ID, record_for(ID, [4 * BS]))
    part = temp.create_part(0, 4 * BS)
    part.commit(fake_hashes(range(4)))
    before = state_path(part).read_bytes()
    calls = _spy_full_fsync(storage, monkeypatch, part)
    part.clear([1, 3])
    assert calls == []
    assert state_path(part).read_bytes() == before + rs.encode_clear([1, 3])
    part.clear(None)
    assert state_path(part).read_bytes() == before + rs.encode_clear([1, 3]) + rs.encode_clear_all()
    assert calls == []
    r = replay(state_path(part), 4)
    assert r.bits.count() == 0


def test_clear_of_many_indices_splits_records_and_stays_under_cap(storage, rd, record_for, resume_helpers):
    n = 70_000
    temp = rd.create_temp(ID, record_for(ID, [n * BS]))
    part = temp.create_part(0, n * BS)
    part.clear(list(range(n)))
    data = state_path(part).read_bytes()
    _, records = resume_helpers.parse_state(data)
    assert all(r[0] == "clear" and 1 <= len(r[1]) <= 65_535 for r in records)
    assert len(data) <= rs.max_state_length(n)
    # 8.3 (b): kiểm gộp sau từng bản ghi: bản gọn (chỉ header) dài 41, nên ngay `CLEAR` 65 535 khối đầu
    # (≈ 256 KiB > 2 × 41 + 64 KiB) đã phải gộp; nhật ký không thể chứa đủ cả 70 000 index.
    assert len(data) < HEADER + len(rs.encode_clear(range(65_535)))
    assert replay(state_path(part), n).bits.count() == 0


# ---------------------------------------------------------------------------------------------
# `ResumeLog`: rewrite_header, compact, trần độ dài, trần bản ghi tên
# ---------------------------------------------------------------------------------------------


def test_rewrite_header_resets_log_and_keeps_appending(rd, record_for):
    temp = rd.create_temp(ID, record_for(ID, [3 * BS]))
    part = temp.create_part(0, 3 * BS)
    part.commit(fake_hashes(range(3)))
    part.log.append(rs.encode_target("a.txt"))
    part.log.rewrite_header()
    header = rs.encode_header(ID, 0, 3 * BS)
    assert state_path(part).read_bytes() == header
    assert part.log.length == HEADER and part.log.name_records == 0
    part.commit(fake_hashes([1]))
    assert state_path(part).read_bytes() == header + rs.encode_mark(fake_hashes([1]))


def test_rewrite_header_with_target_writes_target_and_reserved_at_once(
    storage, rd, record_for, monkeypatch, tmp_path, resume_helpers
):
    temp = rd.create_temp(ID, record_for(ID, [3 * BS]))
    part = temp.create_part(0, 3 * BS)
    part.commit(fake_hashes(range(3)))
    old = state_path(part).read_bytes()
    crash = tmp_path / "sap"
    monkeypatch.setattr(
        storage, "_before_atomic_replace", lambda *a: resume_helpers.crash_copy(rd.root, crash)
    )
    part.log.rewrite_header(target="Ảnh (1).jpg")
    expected = (
        rs.encode_header(ID, 0, 3 * BS) + rs.encode_target("Ảnh (1).jpg") + rs.encode_reserved("Ảnh (1).jpg")
    )
    assert state_path(part).read_bytes() == expected
    assert part.log.name_records == 2
    # Một lần nguyên tử: sập ngay trước khi thay thì `.state` cũ còn nguyên.
    assert (crash / ".teleport" / ID / "0.state").read_bytes() == old


def test_compact_rewrites_exact_compact_form_atomically(
    storage, rd, record_for, monkeypatch, tmp_path, resume_helpers
):
    n = 40
    temp = rd.create_temp(ID, record_for(ID, [n * BS]))
    part = temp.create_part(0, n * BS)
    for k in range(5):
        part.commit(fake_hashes(range(k, n, 3)))
        part.clear(list(range(k, n, 7)))
    part.log.append(rs.encode_target("x.bin"))
    part.log.append(rs.encode_reserved("x.bin"))
    old = state_path(part).read_bytes()
    before = replay(state_path(part), n)
    crash = tmp_path / "sap"
    monkeypatch.setattr(
        storage, "_before_atomic_replace", lambda *a: resume_helpers.crash_copy(rd.root, crash)
    )
    part.log.compact()
    monkeypatch.undo()
    expected = rs.encode_header(ID, 0, n * BS) + b"".join(
        rs.compact_records(before.bits, before.leaves, before.target, before.reserved)
    )
    assert state_path(part).read_bytes() == expected
    assert part.log.length == len(expected) and part.log.name_records == 2
    assert (crash / ".teleport" / ID / "0.state").read_bytes() == old  # sập giữa chừng: bản cũ nguyên
    # Đang mở (Windows không thay được file đang mở): sau khi gộp vẫn nối tiếp được.
    part.commit(fake_hashes([0]))
    after = replay(state_path(part), n)
    assert 0 in after.bits and after.target == "x.bin" and after.reserved


def test_compact_reads_in_state_read_chunks(storage, rd, record_for, monkeypatch):
    monkeypatch.setattr(storage, "STATE_READ_CHUNK", 7)  # seam đọc lúc dùng
    n = 10
    temp = rd.create_temp(ID, record_for(ID, [n * BS]))
    part = temp.create_part(0, n * BS)
    part.commit(fake_hashes(range(n)))
    part.clear([2])
    part.log.compact()
    r = replay(state_path(part), n)
    assert set(range(n)) - {2} == {i for i in range(n) if i in r.bits}


def test_name_record_ceiling_compacts_before_target(rd, record_for):
    # Architecture mục 4 “Trần bản ghi tên”: gộp trước khi nối TARGET/RESERVED khi đã có
    # ≥ MAX_NAME_RECORDS − 2 bản ghi tên, để bộ đọc không bao giờ dừng ở bản thứ 1025.
    temp = rd.create_temp(ID, record_for(ID, [2 * BS]))
    part = temp.create_part(0, 2 * BS)
    part.commit(fake_hashes([1]))
    total = 3 * rs.MAX_NAME_RECORDS
    for k in range(total):
        name = f"t{k % 7}.bin"
        part.log.append(rs.encode_target(name))
        if k % 2:
            part.log.append(rs.encode_reserved(name))
        assert part.log.name_records <= rs.MAX_NAME_RECORDS
    part.log.sync()
    r = replay(state_path(part), 2)
    last = f"t{(total - 1) % 7}.bin"
    assert r.target == last and r.reserved is True
    assert not r.truncated and r.good_end == os.path.getsize(state_path(part))
    assert r.name_records <= rs.MAX_NAME_RECORDS
    assert r.name_records == part.log.name_records
    assert 1 in r.bits


def test_remarking_same_block_keeps_log_bounded_by_real_compact_length(storage, rd, record_for, monkeypatch):
    # 8.3 (a): ước lượng gọn kẹp số khối ≤ n: đánh dấu lại cùng một khối không làm nới ngưỡng gộp.
    monkeypatch.setattr(storage, "_full_fsync", lambda fd: None)
    temp = rd.create_temp(ID, record_for(ID, [1]))
    part = temp.create_part(0, 1)
    mark = rs.encode_mark(fake_hashes([0]))
    real_compact = rs.compact_length_for(1, None, None)
    for _ in range(3000):
        part.commit(fake_hashes([0]))
        assert part.log.length <= 2 * real_compact + rs.COMPACT_SLACK + len(mark)
    assert os.path.getsize(state_path(part)) == part.log.length


NAME_OPS = st.sampled_from(["a.txt", "b.bin", "Ảnh.jpg", "x" * 200])


@st.composite
def log_ops(draw, n: int):
    ops = []
    for _ in range(draw(st.integers(1, 14))):
        kind = draw(
            st.sampled_from(
                [
                    "commit",
                    "commit",
                    "clear",
                    "clear_big",
                    "clear_all",
                    "target",
                    "reserved",
                    "compact",
                    "header",
                    "header_target",
                ]
            )
        )
        if kind == "commit":
            ops.append(("commit", sorted(draw(st.sets(st.integers(0, n - 1), min_size=0, max_size=300)))))
        elif kind == "clear":
            ops.append(("clear", draw(st.lists(st.integers(0, n - 1), min_size=1, max_size=50))))
        elif kind == "clear_big":
            start = draw(st.integers(0, n - 1))
            count = draw(st.integers(1, n))
            ops.append(("clear", [(start + k) % n for k in range(count)]))
        elif kind in ("target", "reserved", "header_target"):
            ops.append((kind, draw(NAME_OPS)))
        else:
            ops.append((kind,))
    return ops


@pytest.mark.slow
@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(data=st.data())
def test_property_log_matches_model_and_never_exceeds_max_state_length(
    storage, record_for, monkeypatch, data
):
    import tempfile

    n = 20_000
    ops = data.draw(log_ops(n), label="ops")
    monkeypatch.setattr(storage, "_full_fsync", lambda fd: None)  # chỉ xét nội dung, bỏ fsync cho nhanh
    with (
        tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d,
        storage.ReceiveDir.open(Path(d) / "recv") as rd,
    ):
        temp = rd.create_temp(ID, record_for(ID, [n * BS]))
        part = temp.create_part(0, n * BS)
        bits: dict[int, bytes] = {}
        target: str | None = None
        reserved = False
        for op in ops:
            if op[0] == "commit":
                blocks = fake_hashes(op[1])
                part.commit(blocks)
                bits.update(blocks)
            elif op[0] == "clear":
                part.clear(op[1])
                for i in op[1]:
                    bits.pop(i, None)
            elif op[0] == "clear_all":
                part.clear(None)
                bits.clear()
            elif op[0] == "target":
                part.log.append(rs.encode_target(op[1]))
                target, reserved = op[1], False
            elif op[0] == "reserved":
                part.log.append(rs.encode_reserved(op[1]))
                if op[1] == target:
                    reserved = True
            elif op[0] == "compact":
                part.log.compact()
            elif op[0] == "header":
                part.log.rewrite_header()
                bits, target, reserved = {}, None, False
            else:
                part.log.rewrite_header(target=op[1])
                bits, target, reserved = {}, op[1], True
            size = os.path.getsize(state_path(part))
            assert size <= rs.max_state_length(n), op[0]
            assert part.log.length == size
        r = replay(state_path(part), n)
        assert not r.truncated
        assert {i for i in range(n) if i in r.bits} == set(bits)
        assert all(bytes(r.leaves[32 * i : 32 * i + 32]) == h for i, h in bits.items())
        assert (r.target, r.reserved) == (target, reserved)


# ---------------------------------------------------------------------------------------------
# `finalize` bước 1–8 (đường NORMAL)
# ---------------------------------------------------------------------------------------------


def _ready_part(rd, record_for, resume_helpers, sizes, index=0, seed=1, tid=ID, temp=None):
    if temp is None:
        temp = rd.create_temp(tid, record_for(tid, sizes))
    data = payload(sizes[index], seed)
    part = temp.create_part(index, len(data))
    part.commit(resume_helpers.write_blocks(part, data))
    return temp, part, data


def test_finalize_records_verified_done_at_and_removes_state(
    storage, rd, record_for, resume_helpers, monkeypatch
):
    stamp = datetime.datetime(2030, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
    monkeypatch.setattr(storage, "_utc_now", lambda: stamp)
    temp, part, data = _ready_part(rd, record_for, resume_helpers, [BS + 1, 7])
    final = rd.finalize(part, "a.bin")
    assert final.read_bytes() == data
    assert not os.path.lexists(temp.path / "0.state")
    rec = rs.decode_transfer_record((temp.path / "transfer.json").read_bytes(), dir_name=ID)
    assert (rec.verified, rec.done_at) == (1, None)
    assert temp.record == rec
    _, part1, data1 = _ready_part(rd, record_for, resume_helpers, [BS + 1, 7], index=1, temp=temp)
    final1 = rd.finalize(part1, "b.bin")
    rec = rs.decode_transfer_record((temp.path / "transfer.json").read_bytes(), dir_name=ID)
    assert (rec.verified, rec.done_at) == (2, stamp)
    assert final1.read_bytes() == data1
    assert sorted(os.listdir(temp.path)) == ["transfer.json"]


def test_finalize_rejects_part_that_is_not_next_before_any_io(rd, record_for, resume_helpers):
    temp = rd.create_temp(ID, record_for(ID, [3, 3]))
    p0 = temp.create_part(0, 3)
    p1 = temp.create_part(1, 3)
    p1.commit(resume_helpers.write_blocks(p1, b"abc"))
    with pytest.raises(ValueError):
        rd.finalize(p1, "b.txt")
    assert not p1.closed
    assert [n for n in os.listdir(rd.root) if n != ".teleport"] == []
    p0.commit(resume_helpers.write_blocks(p0, b"xyz"))
    assert rd.finalize(p0, "a.txt").read_bytes() == b"xyz"
    assert rd.finalize(p1, "b.txt").read_bytes() == b"abc"


def test_step1_target_is_durable_before_reservation(storage, rd, record_for, resume_helpers, monkeypatch):
    _temp, part, _data = _ready_part(rd, record_for, resume_helpers, [100])
    spath = state_path(part)
    real_open = os.open
    at_reserve: list[list[tuple]] = []

    def spy(path, flags, *args, **kwargs):
        if not isinstance(path, int) and flags & os.O_EXCL and os.path.basename(os.fsdecode(path)) == "a.txt":
            at_reserve.append(resume_helpers.parse_state(spath.read_bytes())[1])
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    rd.finalize(part, "a.txt")
    monkeypatch.undo()
    assert len(at_reserve) == 1
    assert at_reserve[0][-1] == ("target", "a.txt")


def test_step1_target_only_for_candidates_really_tried(storage, rd, record_for, resume_helpers, monkeypatch):
    # 8.3: ứng viên bị loại bởi tập `taken` (liệt kê thư mục) không được nối TARGET; ứng viên thật sự
    # thử `O_EXCL` (kể cả bị kẻ chen chiếm ngay lúc đó) thì có.
    (rd.root / "a.txt").write_bytes(b"co san")
    _temp, part, _data = _ready_part(rd, record_for, resume_helpers, [100])
    tried: list[str] = []
    monkeypatch.setattr(storage, "_after_target", lambda cand, *a: tried.append(os.path.basename(str(cand))))
    states: list[list[tuple]] = []
    monkeypatch.setattr(
        storage,
        "_after_reserved",
        lambda *a: states.append(resume_helpers.parse_state(state_path(part).read_bytes())[1]),
    )
    real_open = os.open
    raced: list[bool] = []

    def racing_open(path, flags, *args, **kwargs):
        if (
            not raced
            and not isinstance(path, int)
            and flags & os.O_EXCL
            and os.path.basename(os.fsdecode(path)) == "a (1).txt"
        ):
            raced.append(True)
            with open(rd.root / "a (1).txt", "xb") as f:
                f.write(b"KE CHEN")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    final = rd.finalize(part, "a.txt")
    monkeypatch.undo()
    assert final == rd.root / "a (2).txt"
    assert tried == ["a (1).txt", "a (2).txt"]
    names = [r for r in states[-1] if r[0] in ("target", "reserved")]
    assert names == [("target", "a (1).txt"), ("target", "a (2).txt"), ("reserved", "a (2).txt")]
    assert (rd.root / "a.txt").read_bytes() == b"co san"
    assert (rd.root / "a (1).txt").read_bytes() == b"KE CHEN"


def test_candidate_rejected_by_encode_target_is_skipped_not_fatal(
    storage, rd, record_for, resume_helpers, monkeypatch
):
    real = rs.encode_target

    def picky(name):
        if name == "a.txt":
            raise rs.StateError("giả: bộ mã hóa từ chối ứng viên")
        return real(name)

    monkeypatch.setattr(rs, "encode_target", picky)
    monkeypatch.setattr(storage, "encode_target", picky, raising=False)
    _temp, part, data = _ready_part(rd, record_for, resume_helpers, [100])
    final = rd.finalize(part, "a.txt")
    monkeypatch.undo()
    assert final == rd.root / "a (1).txt"
    assert final.read_bytes() == data
    assert not os.path.lexists(rd.root / "a.txt")


NAMES = st.one_of(
    st.text(st.characters(exclude_categories=()), max_size=40),
    st.lists(
        st.sampled_from(
            [
                "/",
                "\\",
                ":",
                "..",
                ".",
                " ",
                "CON",
                "nul",
                "‮",
                "\x00",
                "é",
                "É",
                "😀",
                "x" * 120,
                " (1)",
                ".teleport",
            ]
        ),
        max_size=12,
    ).map("".join),
    st.sampled_from(["", ".", "..", "CON", "x" * 251 + ".txt", "ệ" * 90 + ".mp4"]),
)


@pytest.mark.slow
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(name=NAMES, taken=st.integers(0, 3))
def test_property_every_tried_candidate_passes_encode_target(
    storage, record_for, resume_helpers, monkeypatch, name, taken
):
    # Rủi ro R4: mọi ứng viên (cắt, đánh số, dự phòng) phải qua được `encode_target`, và lượt không bị hủy.
    import tempfile

    from teleport.core.filenames import fit_filename, numbered

    tried: list[str] = []
    monkeypatch.setattr(storage, "_after_target", lambda cand, *a: tried.append(os.path.basename(str(cand))))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        root = Path(d) / "recv"
        root.mkdir()
        base = fit_filename(name)
        for k in range(taken):
            with contextlib.suppress(OSError, ValueError):
                (root / (base if k == 0 else numbered(name, k))).write_bytes(b"co san")
        with storage.ReceiveDir.open(root) as rd:
            temp = rd.create_temp(ID, record_for(ID, [4]))
            part = temp.create_part(0, 4)
            part.commit(resume_helpers.write_blocks(part, b"abcd"))
            tried.clear()
            final = rd.finalize(part, name)
            assert final.read_bytes() == b"abcd"
    assert tried, "bước 1 phải nối TARGET cho ứng viên thử"
    for cand in tried:
        assert sanitize_filename(cand) == cand
        assert 1 <= len(cand.encode("utf-8")) <= 255
        rs.encode_target(cand)
    assert tried[-1] == final.name


def test_placeholder_changed_after_reserved_is_never_overwritten(
    storage, rd, record_for, resume_helpers, monkeypatch
):
    # 8.3 F7: kiểm lại file giữ chỗ thêm một lần ngay trước bước 5 (sau RESERVED bền).
    _temp, part, data = _ready_part(rd, record_for, resume_helpers, [3000])
    hit: list[Path] = []

    def tamper(path, *a):
        if not hit:
            hit.append(Path(path))
            with open(rd.root / "a.txt", "ab") as f:
                f.write(b"KE CHEN")

    monkeypatch.setattr(storage, "_after_reserved", tamper)
    try:
        final = rd.finalize(part, "a.txt")
    except storage.StorageError:
        final = None
    assert hit
    assert (rd.root / "a.txt").read_bytes() == b"KE CHEN"
    if final is not None:
        assert final != rd.root / "a.txt"
        assert final.read_bytes() == data


def _rename_part(storage, monkeypatch, behavior) -> None:
    owner, attr = (storage, "_move_file_ex") if WINDOWS else (os, "replace")
    real = getattr(owner, attr)

    def wrapper(*args, **kwargs):
        if not os.fsdecode(args[0]).endswith(".part"):
            return real(*args, **kwargs)
        return behavior(real, args, kwargs)

    monkeypatch.setattr(owner, attr, wrapper)


def test_failed_rename_after_reserved_appends_target_again_before_deleting_placeholder(
    storage, rd, record_for, resume_helpers, monkeypatch, quick_retry
):
    _temp, part, data = _ready_part(rd, record_for, resume_helpers, [2000])

    def broken(real, args, kwargs):
        raise OSError(errno.EIO, "loi I/O")

    _rename_part(storage, monkeypatch, broken)
    with pytest.raises(OSError):
        rd.finalize(part, "a.txt")
    monkeypatch.undo()
    assert not os.path.lexists(rd.root / "a.txt")  # file giữ chỗ đã dọn
    _, records = resume_helpers.parse_state(state_path(part).read_bytes())
    names = [r for r in records if r[0] in ("target", "reserved")]
    assert names[-3:] == [("target", "a.txt"), ("reserved", "a.txt"), ("target", "a.txt")]
    r = replay(state_path(part), 1)
    assert (r.target, r.reserved) == ("a.txt", False)
    assert part.path.read_bytes() == data


def test_failed_rename_keeps_placeholder_when_state_cannot_be_written(
    storage, rd, record_for, resume_helpers, monkeypatch, quick_retry
):
    _temp, part, data = _ready_part(rd, record_for, resume_helpers, [2000])
    real_append = storage.ResumeLog.append
    broken_log = []

    def append(self, record):
        if broken_log:
            raise OSError(errno.EIO, "khong ghi duoc .state")
        return real_append(self, record)

    def broken(real, args, kwargs):
        broken_log.append(True)
        raise OSError(errno.EIO, "loi I/O")

    monkeypatch.setattr(storage.ResumeLog, "append", append)
    _rename_part(storage, monkeypatch, broken)
    with pytest.raises(OSError):
        rd.finalize(part, "a.txt")
    monkeypatch.undo()
    placeholder = rd.root / "a.txt"
    assert placeholder.is_file() and os.path.getsize(placeholder) == 0  # không quên được RESERVED → để nguyên
    assert part.path.read_bytes() == data


def test_step7_failure_keeps_final_file_and_raises_os_error(
    storage, rd, record_for, resume_helpers, monkeypatch
):
    # 8.4 B1: lỗi bước 7 sau khi file đã mang tên thật → OSError; file đã nhận giữ nguyên.
    temp, part, data = _ready_part(rd, record_for, resume_helpers, [5000])

    def broken(temp_path, name, *a):
        if name == "transfer.json":
            raise OSError(errno.ENOSPC, "day dia")

    monkeypatch.setattr(storage, "_before_atomic_replace", broken)
    with pytest.raises(OSError):
        rd.finalize(part, "a.txt")
    monkeypatch.undo()
    assert (rd.root / "a.txt").read_bytes() == data
    assert not os.path.lexists(part.path)
    rec = rs.decode_transfer_record((temp.path / "transfer.json").read_bytes(), dir_name=ID)
    assert rec.verified == 0


def test_step8_state_delete_error_is_ignored(storage, rd, record_for, resume_helpers, monkeypatch):
    temp, part, data = _ready_part(rd, record_for, resume_helpers, [50])
    real_unlink, real_remove = os.unlink, os.remove

    def refuse(path, *args, **kwargs):
        if not isinstance(path, int) and os.fsdecode(path).endswith(".state"):
            raise PermissionError(errno.EACCES, "dang bi giu")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refuse)
    monkeypatch.setattr(os, "remove", refuse)
    monkeypatch.setattr(storage, "REPLACE_DELAY", 0.001)
    final = rd.finalize(part, "a.txt")
    monkeypatch.setattr(os, "unlink", real_unlink)
    monkeypatch.setattr(os, "remove", real_remove)
    assert final.read_bytes() == data
    rec = rs.decode_transfer_record((temp.path / "transfer.json").read_bytes(), dir_name=ID)
    assert rec.verified == 1


# ---------------------------------------------------------------------------------------------
# `remove_temp(keep_record)`, `close_temp`
# ---------------------------------------------------------------------------------------------


def test_remove_temp_deletes_state_record_and_layout_tmp_but_not_foreign(rd, record_for, resume_helpers):
    temp, part, _ = _ready_part(rd, record_for, resume_helpers, [100])
    (temp.path / "0.state.0123456789abcdef.tmp").write_bytes(b"sot")
    (temp.path / "ghi-chu.txt").write_bytes(b"la")
    assert rd.remove_temp(temp) is False
    assert sorted(os.listdir(temp.path)) == ["ghi-chu.txt"]
    assert part.log.closed


def test_remove_temp_keep_record_keeps_only_transfer_json(rd, record_for, resume_helpers):
    temp, _part, _ = _ready_part(rd, record_for, resume_helpers, [100])
    assert rd.remove_temp(temp, keep_record=True) is False
    assert os.listdir(temp.path) == ["transfer.json"]


def test_remove_temp_full_returns_true_and_keeps_teleport(rd, record_for, resume_helpers):
    temp, _part, _ = _ready_part(rd, record_for, resume_helpers, [100])
    assert rd.remove_temp(temp) is True
    assert not os.path.lexists(temp.path)
    assert (rd.root / ".teleport").is_dir()


def test_remove_temp_does_not_follow_state_symlink(rd, record_for, tmp_path):
    temp = rd.create_temp(ID, record_for(ID, [1]))
    victim = tmp_path / "nan-nhan.state"
    victim.write_bytes(b"cua nan nhan")
    make_symlink(victim, temp.path / "5.state", is_dir=False)
    assert rd.remove_temp(temp) is False
    assert victim.read_bytes() == b"cua nan nhan"
    assert os.path.islink(temp.path / "5.state")


def test_close_temp_closes_handles_without_deleting(storage, rd, record_for, resume_helpers):
    temp, part, _data = _ready_part(rd, record_for, resume_helpers, [100])
    before = sorted(os.listdir(temp.path))
    rd.close_temp(temp)
    assert temp.closed and part.closed and part.log.closed
    assert sorted(os.listdir(temp.path)) == before == ["0.part", "0.state", "transfer.json"]
    for name in before:  # Windows: xóa được nghĩa là không còn handle mở
        os.unlink(temp.path / name)
    with pytest.raises(ValueError):
        rd.remove_temp(temp)
    with pytest.raises(ValueError):
        temp.create_part(1, 1)
    os.rmdir(temp.path)
