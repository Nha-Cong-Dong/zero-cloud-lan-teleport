"""Kiểm thử tầng core cho tính năng gửi thư mục (directory transfer).

Kiểm tra:
- FileEntry với `rel_path` và `is_dir`.
- validate_rel_path.
- sanitize_rel_path: chống path traversal, làm sạch ký tự cấm, tên thiết bị Windows.
- dedupe_offer_paths: khử trùng đường dẫn thư mục có đánh số đúng phần tên tệp.
- prepare_outgoing_rel_path.
"""

import pytest

from teleport.core.filenames import (
    FALLBACK_NAME,
    MAX_PATH_DEPTH,
    OutgoingNameError,
    dedupe_offer_paths,
    prepare_outgoing_rel_path,
    sanitize_rel_path,
)
from teleport.core.messages import (
    FileEntry,
    MessageError,
    validate_rel_path,
)


def test_file_entry_with_rel_path_and_is_dir() -> None:
    entry = FileEntry(
        name="test.txt",
        size=1024,
        mtime=1700000000000,
        rel_path="docs/sub/test.txt",
        is_dir=False,
    )
    assert entry.name == "test.txt"
    assert entry.size == 1024
    assert entry.rel_path == "docs/sub/test.txt"
    assert not entry.is_dir

    wire = entry.to_dict()
    assert wire["name"] == "test.txt"
    assert wire["rel_path"] == "docs/sub/test.txt"
    assert "is_dir" not in wire  # False không cần ghi ra dây

    restored = FileEntry.from_dict(wire)
    assert restored == entry


def test_file_entry_empty_dir() -> None:
    dir_entry = FileEntry(
        name="empty_folder",
        size=0,
        mtime=1700000000000,
        rel_path="projects/empty_folder",
        is_dir=True,
    )
    assert dir_entry.is_dir
    wire = dir_entry.to_dict()
    assert wire["is_dir"] is True
    assert FileEntry.from_dict(wire) == dir_entry


def test_validate_rel_path() -> None:
    assert validate_rel_path("") == ""
    assert validate_rel_path("a/b/c.txt") == "a/b/c.txt"
    assert validate_rel_path("folder\\file.txt") == "folder\\file.txt"

    with pytest.raises(MessageError, match="tuyệt đối"):
        validate_rel_path("/etc/passwd")

    with pytest.raises(MessageError, match="tuyệt đối"):
        validate_rel_path("\\Windows\\System32")

    with pytest.raises(MessageError, match="ổ đĩa"):
        validate_rel_path("C:\\file.txt")

    with pytest.raises(MessageError, match=r"\.\."):
        validate_rel_path("a/../b")

    with pytest.raises(MessageError, match="dài quá"):
        validate_rel_path("a/" * 3000)


def test_sanitize_rel_path_path_traversal() -> None:
    # Path traversal phải bị chặn hoàn toàn
    assert sanitize_rel_path("../../etc/passwd") == "etc/passwd"
    assert sanitize_rel_path("a/../../b/c.txt") == "a/b/c.txt"
    assert sanitize_rel_path("///") == FALLBACK_NAME
    assert sanitize_rel_path(".../...") == "file/file"


def test_sanitize_rel_path_windows_reserved() -> None:
    # Tên thiết bị Windows ở bất kỳ cấp nào đều được làm sạch an toàn
    assert sanitize_rel_path("folder/CON/file.txt") == "folder/_CON/file.txt"
    assert sanitize_rel_path("NUL/sub/AUX.dat") == "_NUL/sub/_AUX.dat"
    assert sanitize_rel_path(".teleport/data") == "_.teleport/data"


def test_sanitize_rel_path_max_depth() -> None:
    deep_path = "/".join(f"level{i}" for i in range(50))
    result = sanitize_rel_path(deep_path, max_depth=MAX_PATH_DEPTH)
    segments = result.split("/")
    assert len(segments) == MAX_PATH_DEPTH


def test_dedupe_offer_paths() -> None:
    paths = [
        "docs/readme.txt",
        "docs/readme.txt",
        "docs/readme (1).txt",
        "photos/beach.png",
        "photos/beach.png",
    ]
    deduped = dedupe_offer_paths(paths)
    assert deduped == [
        "docs/readme.txt",
        "docs/readme (2).txt",
        "docs/readme (1).txt",
        "photos/beach.png",
        "photos/beach (1).png",
    ]


def test_prepare_outgoing_rel_path() -> None:
    assert prepare_outgoing_rel_path("") == ""
    assert prepare_outgoing_rel_path("docs/sub/file.txt") == "docs/sub/file.txt"
    with pytest.raises(OutgoingNameError):
        prepare_outgoing_rel_path("docs/../file.txt")
