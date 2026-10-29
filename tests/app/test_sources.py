"""Test `teleport.app.sources`: đọc file nguồn của bên gửi (P2-6a: S1–S3).

Nguồn: `specs/plan-p2-6.md` mục 2.4 và tiêu chí S1–S3 mục 5, `specs/spec-p2-6.md` mục A2;
protocol §3.3 (`FileEntry`: tên ≤ 255 byte UTF-8, `mtime` mili giây), §4.2 (khối 1 MiB, SHA-256),
§4.4 (Merkle root). Viết từ đặc tả, không từ code. Không mở socket.

API đồng bộ (chỉ gọi trong thread đĩa), nên test gọi thẳng.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import os
import sys
from pathlib import Path

import pytest

from teleport.app import sources
from teleport.app.sources import (
    SourceChanged,
    SourceError,
    SourceErrorKind,
    SourceFile,
    SourceReader,
    check_source,
    prepare_sources,
)
from teleport.core import blocks
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.filenames import MAX_NAME_BYTES, OutgoingNameError, prepare_outgoing_name
from teleport.core.merkle import merkle_root
from teleport.core.messages import MAX_OFFER_FILES, FileEntry

WINDOWS = sys.platform == "win32"
posix_only = pytest.mark.skipif(WINDOWS, reason="chỉ POSIX")
MTIME_NS = 1_700_000_000_123_456_789  # có phần lẻ dưới mili giây để kiểm phép chia nguyên


def make_file(path: Path, data: bytes = b"", mtime_ns: int = MTIME_NS) -> Path:
    path.write_bytes(data)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def expected_mtime(path: Path) -> int:
    """`mtime` theo spec S1: `st_mtime_ns // 10**6` của chính file (độ phân giải hệ file tùy nơi)."""
    return os.stat(path).st_mtime_ns // 10**6


def payload(size: int, seed: int = 1) -> bytes:
    """Byte giả ngẫu nhiên tất định (không trùng lặp theo khối, để lệch vị trí đọc thì lộ ra)."""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        counter += 1
    return bytes(out[:size])


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def same_path(a: os.PathLike[str] | str, b: os.PathLike[str] | str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def raises_source_error(paths, kind: SourceErrorKind) -> SourceError:
    with pytest.raises(SourceError) as info:
        prepare_sources(paths)
    assert info.value.kind is kind, info.value.kind
    return info.value


# =================================================================================================
# Hằng, kiểu
# =================================================================================================


def test_max_file_size_follows_protocol():
    assert sources.MAX_FILE_SIZE == blocks.MAX_FILE_SIZE


def test_source_error_kinds_match_spec():
    assert {k.name for k in SourceErrorKind} == {
        "NOT_FOUND",
        "NOT_A_FILE",
        "PERMISSION",
        "TOO_LARGE",
        "BAD_NAME",
        "OS_ERROR",
    }


def test_source_file_is_frozen_dataclass(tmp_path):
    f = make_file(tmp_path / "a.bin", b"x")
    [sf] = prepare_sources([f])
    assert isinstance(sf, SourceFile)
    assert [fl.name for fl in dataclasses.fields(sf)] == ["path", "entry"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        sf.path = f  # type: ignore[misc]


# =================================================================================================
# S1: prepare_sources
# =================================================================================================


def test_s1_builds_file_entries_in_order(tmp_path):
    a = make_file(tmp_path / "rỗng.txt", b"")
    b = make_file(tmp_path / "b.bin", payload(5), mtime_ns=MTIME_NS + 7_000_000)
    result = prepare_sources([a, str(b)])
    assert [type(x) for x in result] == [SourceFile, SourceFile]
    assert result[0].entry == FileEntry(name="rỗng.txt", size=0, mtime=expected_mtime(a))
    assert result[1].entry == FileEntry(name="b.bin", size=5, mtime=expected_mtime(b))
    assert same_path(result[0].path, a) and same_path(result[1].path, b)
    assert all(isinstance(x.path, Path) and x.path.is_absolute() for x in result)


def test_s1_mtime_is_floor_of_nanoseconds_in_milliseconds(tmp_path):
    f = make_file(tmp_path / "a.bin", b"abc", mtime_ns=1_600_000_000_999_999_999)
    [sf] = prepare_sources([f])
    assert sf.entry.mtime == os.stat(f).st_mtime_ns // 10**6


def test_s1_relative_path_becomes_absolute_without_resolving(tmp_path, monkeypatch):
    sub = tmp_path / "sub"
    sub.mkdir()
    make_file(sub / "a.bin", b"1")
    monkeypatch.chdir(tmp_path)
    [sf] = prepare_sources([os.path.join("sub", "..", "sub", "a.bin")])
    assert sf.path == Path(os.path.abspath(os.path.join("sub", "..", "sub", "a.bin")))
    assert sf.path.is_absolute()


def test_s1_long_name_is_cut_keeping_extension(tmp_path):
    name = "ạ" * 100 + ".mkv"  # 104 ký tự, 304 byte UTF-8
    assert len(name.encode()) > MAX_NAME_BYTES
    try:
        f = make_file(tmp_path / name, b"x")
    except OSError:
        pytest.skip("hệ file không cho tên dài hơn 255 byte UTF-8 (ví dụ ext4)")
    [sf] = prepare_sources([f])
    assert len(sf.entry.name.encode()) <= MAX_NAME_BYTES
    assert sf.entry.name.endswith(".mkv")
    assert sf.entry.name.startswith("ạ")
    assert sf.entry.name == prepare_outgoing_name(f.name)


def test_s1_missing_file_is_not_found(tmp_path):
    missing = tmp_path / "khong-co.bin"
    e = raises_source_error([missing], SourceErrorKind.NOT_FOUND)
    assert same_path(e.path, missing)
    assert e.errno == errno.ENOENT
    assert isinstance(e.__cause__, OSError)
    assert e.size is None


def test_s1_directory_is_not_a_file(tmp_path):
    d = tmp_path / "thu-muc"
    d.mkdir()
    e = raises_source_error([d], SourceErrorKind.NOT_A_FILE)
    assert same_path(e.path, d)


def test_s1_too_large_reports_size_and_limit_is_read_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "MAX_FILE_SIZE", 10)
    ok = make_file(tmp_path / "vua.bin", payload(10))
    big = make_file(tmp_path / "lon.bin", payload(11))
    assert prepare_sources([ok])[0].entry.size == 10  # đúng giới hạn: hợp lệ
    e = raises_source_error([big], SourceErrorKind.TOO_LARGE)
    assert e.size == 11
    assert same_path(e.path, big)


@posix_only
def test_s1_name_with_newline_is_bad_name(tmp_path):
    f = make_file(tmp_path / "a\nb.txt", b"x")
    e = raises_source_error([f], SourceErrorKind.BAD_NAME)
    assert isinstance(e.__cause__, OutgoingNameError)
    assert same_path(e.path, f)


@pytest.mark.skipif(sys.platform != "linux", reason="chỉ Linux cho tên file không phải UTF-8")
def test_s1_undecodable_name_is_bad_name(tmp_path):
    raw = os.fsencode(tmp_path) + b"/\xff\xfe.bin"
    try:
        with open(raw, "wb") as fh:
            fh.write(b"x")
    except OSError:
        pytest.skip("hệ file không cho tên không phải UTF-8")
    raises_source_error([os.fsdecode(raw)], SourceErrorKind.BAD_NAME)


@posix_only
@pytest.mark.skipif(is_root(), reason="root đọc được file chmod 0")
def test_s1_unreadable_file_is_permission(tmp_path):
    f = make_file(tmp_path / "khoa.bin", b"x")
    os.chmod(f, 0)
    try:
        e = raises_source_error([f], SourceErrorKind.PERMISSION)
        assert isinstance(e.__cause__, PermissionError)
        assert e.errno in (errno.EACCES, errno.EPERM)
    finally:
        os.chmod(f, 0o600)


def test_s1_first_error_in_path_order_wins(tmp_path):
    good = make_file(tmp_path / "tot.bin", b"x")
    missing = tmp_path / "thieu.bin"
    d = tmp_path / "thu-muc"
    d.mkdir()
    raises_source_error([good, missing, d], SourceErrorKind.NOT_FOUND)
    raises_source_error([good, d, missing], SourceErrorKind.NOT_A_FILE)


def test_s1_zero_paths_is_value_error():
    with pytest.raises(ValueError) as info:
        prepare_sources([])
    assert not isinstance(info.value, SourceError)


@pytest.mark.slow
def test_s1_path_count_limit(tmp_path):
    files = [make_file(tmp_path / f"{i}.bin") for i in range(MAX_OFFER_FILES + 1)]
    assert len(prepare_sources(files[:MAX_OFFER_FILES])) == MAX_OFFER_FILES
    with pytest.raises(ValueError) as info:
        prepare_sources(files)
    assert not isinstance(info.value, SourceError)


def test_s1_same_path_twice_is_value_error(tmp_path, monkeypatch):
    f = make_file(tmp_path / "a.bin", b"x")
    for pair in ([f, f], [f, str(f)]):
        with pytest.raises(ValueError):
            prepare_sources(pair)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        prepare_sources([f, "a.bin"])  # tuyệt đối và tương đối của cùng một file


@pytest.mark.skipif(not WINDOWS, reason="Windows: tên file không phân biệt hoa thường")
def test_s1_same_path_with_other_case_is_value_error_on_windows(tmp_path):
    f = make_file(tmp_path / "a.bin", b"x")
    with pytest.raises(ValueError):
        prepare_sources([f, tmp_path / "A.BIN"])


def test_s1_hard_link_to_same_file_is_value_error(tmp_path):
    f = make_file(tmp_path / "a.bin", b"x")
    link = tmp_path / "b.bin"
    try:
        os.link(f, link)
    except (OSError, NotImplementedError):
        pytest.skip("hệ file không hỗ trợ hard link")
    with pytest.raises(ValueError):
        prepare_sources([f, link])


def test_s1_two_different_files_with_same_content_are_fine(tmp_path):
    a = make_file(tmp_path / "a.bin", b"giong")
    b = make_file(tmp_path / "b.bin", b"giong")
    assert len(prepare_sources([a, b])) == 2


@pytest.mark.parametrize("kind", ["str", "bytes", "path"])
def test_s1_single_path_instead_of_sequence_is_type_error(tmp_path, kind):
    f = make_file(tmp_path / "a.bin", b"x")
    value = {"str": str(f), "bytes": os.fsencode(f), "path": f}[kind]
    with pytest.raises(TypeError):
        prepare_sources(value)


def test_s1_keeps_no_handle_open(tmp_path):
    """Windows: xóa / đổi tên được file ngay sau khi `prepare_sources` trả về."""
    a = make_file(tmp_path / "a.bin", payload(100))
    b = make_file(tmp_path / "b.bin", payload(100))
    prepare_sources([a, b])
    os.remove(a)
    os.replace(b, tmp_path / "c.bin")
    assert not a.exists() and not b.exists()


def test_s1_failed_call_keeps_no_handle_open(tmp_path):
    a = make_file(tmp_path / "a.bin", payload(100))
    with pytest.raises(SourceError):
        prepare_sources([a, tmp_path / "thieu.bin"])
    os.remove(a)


# =================================================================================================
# S2: SourceReader
# =================================================================================================


def _reader_for(path: Path) -> SourceReader:
    [sf] = prepare_sources([path])
    return SourceReader.open(sf.path, sf.entry)


def test_s2_read_block_returns_bytes_and_sha256_last_block_short(tmp_path):
    data = payload(2 * BLOCK_SIZE + 5)
    f = make_file(tmp_path / "a.bin", data)
    reader = _reader_for(f)
    try:
        for i in (2, 0, 1, 2):  # đọc không theo thứ tự: luôn đọc tại index × BLOCK_SIZE
            got, digest = reader.read_block(i)
            want = data[i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE]
            assert isinstance(got, bytearray)
            assert bytes(got) == want
            assert digest == hashlib.sha256(want).digest()
        assert len(reader.read_block(2)[0]) == 5
    finally:
        reader.close()


def test_s2_file_of_exact_block_multiple(tmp_path):
    data = payload(BLOCK_SIZE)
    f = make_file(tmp_path / "a.bin", data)
    with _reader_for(f) as reader:
        got, digest = reader.read_block(0)
    assert bytes(got) == data and digest == hashlib.sha256(data).digest()


def test_s2_truncated_file_raises_source_changed(tmp_path):
    f = make_file(tmp_path / "a.bin", payload(2 * BLOCK_SIZE))
    reader = _reader_for(f)
    try:
        with open(f, "r+b") as fh:  # một tiến trình khác cắt file trong lúc gửi
            fh.truncate(BLOCK_SIZE + 3)
        with pytest.raises(SourceChanged):
            reader.read_block(1)
    finally:
        reader.close()


@pytest.mark.parametrize("field", ["size", "mtime"])
def test_s2_open_with_size_or_mtime_differing_from_entry_raises(tmp_path, field):
    f = make_file(tmp_path / "a.bin", payload(10))
    [sf] = prepare_sources([f])
    changed = dataclasses.replace(sf.entry, **{field: getattr(sf.entry, field) + 1})
    with pytest.raises(SourceChanged):
        SourceReader.open(sf.path, changed)
    SourceReader.open(sf.path, sf.entry).close()  # entry đúng thì mở được: lỗi chỉ do trường đã đổi


def test_s2_file_modified_after_prepare_raises_on_open(tmp_path):
    f = make_file(tmp_path / "a.bin", payload(10))
    [sf] = prepare_sources([f])
    make_file(f, payload(10, seed=2), mtime_ns=MTIME_NS + 5_000_000_000)  # cùng cỡ, mtime khác
    with pytest.raises(SourceChanged):
        SourceReader.open(sf.path, sf.entry)


def test_s2_open_on_directory_raises_source_changed(tmp_path):
    f = make_file(tmp_path / "a.bin", payload(10))
    [sf] = prepare_sources([f])
    os.remove(f)
    f.mkdir()
    with pytest.raises(SourceChanged):
        SourceReader.open(sf.path, sf.entry)


def test_s2_close_releases_file_and_is_idempotent(tmp_path):
    f = make_file(tmp_path / "a.bin", payload(BLOCK_SIZE + 1))
    reader = _reader_for(f)
    reader.read_block(1)
    reader.close()
    reader.close()
    os.remove(f)  # Windows: hết handle thì xóa được


def test_s2_context_manager_closes(tmp_path):
    f = make_file(tmp_path / "a.bin", payload(100))
    with _reader_for(f) as reader:
        assert bytes(reader.read_block(0)[0]) == payload(100)
    os.remove(f)


# =================================================================================================
# S3: check_source
# =================================================================================================


def _leaves(data: bytes) -> bytearray:
    out = bytearray()
    for start in range(0, len(data), BLOCK_SIZE):
        out += hashlib.sha256(data[start : start + BLOCK_SIZE]).digest()
    return out


def test_s3_returns_size_mtime_and_merkle_root(tmp_path):
    data = payload(2 * BLOCK_SIZE + 9)
    f = make_file(tmp_path / "a.bin", data)
    leaves = _leaves(data)
    size, mtime, root = check_source(f, leaves)
    assert (size, mtime) == (len(data), expected_mtime(f))
    assert root == merkle_root(leaves)
    assert len(root) == 32


def test_s3_empty_file(tmp_path):
    f = make_file(tmp_path / "rong.bin", b"")
    assert check_source(f, bytearray()) == (0, expected_mtime(f), hashlib.sha256(b"").digest())


def test_s3_reads_current_state_of_path(tmp_path):
    f = make_file(tmp_path / "a.bin", payload(10))
    leaves = _leaves(payload(10))
    first = check_source(f, leaves)
    make_file(f, payload(12), mtime_ns=MTIME_NS + 3_000_000_000)
    second = check_source(f, leaves)
    assert second[0] == 12 and second[1] == expected_mtime(f)
    assert second[1] != first[1]


def test_s3_directory_raises_source_changed(tmp_path):
    d = tmp_path / "thu-muc"
    d.mkdir()
    with pytest.raises(SourceChanged):
        check_source(d, bytearray())


def test_s3_missing_file_raises_os_error(tmp_path):
    with pytest.raises(OSError) as info:
        check_source(tmp_path / "thieu.bin", bytearray())
    assert not isinstance(info.value, SourceChanged)


# =================================================================================================
# C5 (spec-p2-6 mục C): FIFO thay cho file nguồn (POSIX) — không treo
# =================================================================================================

FIFO_TIMEOUT = 5.0


def _run_bounded(fn, fifo: Path):
    """Chạy `fn()` trong thread có hạn; quá hạn (đang treo ở open FIFO) thì mở đầu ghi để nhả rồi fail."""
    import concurrent.futures

    pool = concurrent.futures.ThreadPoolExecutor(1)
    fut = pool.submit(fn)
    try:
        return fut.result(FIFO_TIMEOUT)
    except concurrent.futures.TimeoutError:
        with contextlib.suppress(OSError):
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        pytest.fail(f"treo quá {FIFO_TIMEOUT} s khi gặp FIFO")
    finally:
        pool.shutdown(wait=False)


def _mkfifo(path: Path) -> None:
    try:
        os.mkfifo(path)
    except (OSError, AttributeError):
        pytest.skip("không tạo được FIFO")


@posix_only
def test_c5_fifo_is_not_a_file_without_hanging(tmp_path):
    fifo = tmp_path / "ong.bin"
    _mkfifo(fifo)

    def call() -> SourceError | None:
        try:
            prepare_sources([fifo])
        except SourceError as e:
            return e
        return None

    e = _run_bounded(call, fifo)
    assert e is not None and e.kind is SourceErrorKind.NOT_A_FILE


@posix_only
def test_c5_source_replaced_by_fifo_raises_source_changed_on_open(tmp_path):
    f = make_file(tmp_path / "a.bin", payload(10))
    [sf] = prepare_sources([f])
    os.remove(f)
    _mkfifo(f)

    def call() -> BaseException | None:
        try:
            SourceReader.open(sf.path, sf.entry).close()
        except BaseException as e:  # trả về để kiểm ở thread chính
            return e
        return None

    err = _run_bounded(call, f)
    assert isinstance(err, SourceChanged), repr(err)


@posix_only
def test_c5_check_source_on_fifo_raises_source_changed(tmp_path):
    fifo = tmp_path / "ong.bin"
    _mkfifo(fifo)

    def call() -> BaseException | None:
        try:
            check_source(fifo, bytearray())
        except BaseException as e:
            return e
        return None

    assert isinstance(_run_bounded(call, fifo), SourceChanged)


@posix_only
def test_c5_fifo_passing_stat_check_is_not_a_file_at_open_without_hanging(tmp_path, monkeypatch):
    """Đường dẫn đổi thành FIFO giữa lúc `stat` và lúc mở: bước mở (O_NONBLOCK + fstat) phải phát hiện.

    Seam `sources._stat_source` được vá để trả `stat` của một file thường khác, nên chỉ bước mở
    mới thấy FIFO."""
    fifo = tmp_path / "ong.bin"
    _mkfifo(fifo)
    regular = make_file(tmp_path / "thuong.bin", payload(10))
    real_stat = os.stat(regular)
    monkeypatch.setattr(sources, "_stat_source", lambda path, *a, **kw: real_stat)

    def call() -> SourceError | None:
        try:
            prepare_sources([fifo])
        except SourceError as e:
            return e
        return None

    e = _run_bounded(call, fifo)
    assert e is not None and e.kind is SourceErrorKind.NOT_A_FILE
