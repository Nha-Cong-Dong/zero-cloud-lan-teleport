"""Kiểm thử tính năng gửi trọn gói thư mục (v1.1.0) ở tầng ứng dụng và lưu trữ."""

from pathlib import Path

import pytest

from teleport.app import sources
from teleport.app.storage import ReceiveDir
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.messages import FileEntry, Offer
from teleport.core.resume_state import TransferRecord


def test_prepare_sources_with_directory(tmp_path: Path) -> None:
    """Tạo cấu trúc thư mục lồng nhau và kiểm tra `prepare_sources(..., allow_dirs=True)`."""
    src_dir = tmp_path / "my_project"
    src_dir.mkdir()
    (src_dir / "README.md").write_text("hello world", encoding="utf-8")

    sub_dir = src_dir / "src" / "pkg"
    sub_dir.mkdir(parents=True)
    (sub_dir / "main.py").write_text("print('test')", encoding="utf-8")

    empty_sub = src_dir / "empty_dir"
    empty_sub.mkdir()

    # Thử quét với allow_dirs=False -> phải báo SourceError (không cho phép thư mục)
    with pytest.raises(sources.SourceError):
        sources.prepare_sources([src_dir], allow_dirs=False)

    # Thử quét với allow_dirs=True -> thành công
    prepared = sources.prepare_sources([src_dir], allow_dirs=True)
    assert len(prepared) == 3

    # Kiểm tra FileEntry và relative path
    rel_paths = {p.entry.rel_path for p in prepared}
    assert "my_project/README.md" in rel_paths
    assert "my_project/src/pkg/main.py" in rel_paths
    assert "my_project/empty_dir" in rel_paths

    # Kiểm tra is_dir
    empty_entry = next(p.entry for p in prepared if p.entry.rel_path == "my_project/empty_dir")
    assert empty_entry.is_dir is True
    assert empty_entry.size == 0

    file_entry = next(p.entry for p in prepared if p.entry.rel_path == "my_project/README.md")
    assert file_entry.is_dir is False
    assert file_entry.size == len("hello world")


def test_source_reader_directory(tmp_path: Path) -> None:
    """Kiểm tra `SourceReader` mở và đọc khối của tệp và thư mục rỗng."""
    src_dir = tmp_path / "bundle"
    src_dir.mkdir()
    file_path = src_dir / "file.txt"
    file_path.write_bytes(b"A" * 100)
    empty_path = src_dir / "empty"
    empty_path.mkdir()

    prepared = sources.prepare_sources([src_dir], allow_dirs=True)
    p_file = next(p for p in prepared if not p.entry.is_dir)
    p_dir = next(p for p in prepared if p.entry.is_dir)

    # Đọc file thường
    with sources.SourceReader.open(p_file.path, p_file.entry) as reader:
        payload, _h = reader.read_block(0)
        assert bytes(payload) == b"A" * 100

    # Đọc thư mục rỗng -> không có khối nào
    with sources.SourceReader.open(p_dir.path, p_dir.entry) as dir_reader, pytest.raises(ValueError):
        dir_reader.read_block(0)


def test_receive_dir_finalize_rel_path(tmp_path: Path) -> None:
    """Kiểm tra `ReceiveDir.finalize` tạo đúng thư mục con theo `rel_path`."""
    recv_root = tmp_path / "received"
    recv_root.mkdir()

    offer_files = (
        FileEntry(name="doc.txt", size=10, mtime=1000, rel_path="docs/sub/doc.txt"),
        FileEntry(name="empty_folder", size=0, mtime=1000, rel_path="docs/empty_folder", is_dir=True),
    )
    t_id = "0" * 32
    offer = Offer(transfer_id=t_id, files=offer_files, block_size=BLOCK_SIZE)
    record = TransferRecord(receiver="a" * 32, sender="b" * 32, offer=offer, verified=0, done_at=None)

    with ReceiveDir.open(recv_root) as rd:
        temp = rd.create_temp(t_id, record)

        # 1. Finalize tệp trong thư mục con
        part0 = temp.create_part(0, 10)
        part0.write_at(0, b"0123456789")
        final_file = rd.finalize(part0, "doc.txt", rel_path="docs/sub/doc.txt")
        assert final_file == recv_root / "docs" / "sub" / "doc.txt"
        assert final_file.is_file()
        assert final_file.read_bytes() == b"0123456789"

        # 2. Finalize thư mục rỗng
        part1 = temp.create_part(1, 0)
        final_dir = rd.finalize(part1, "empty_folder", rel_path="docs/empty_folder", is_dir=True)
        assert final_dir == recv_root / "docs" / "empty_folder"
        assert final_dir.is_dir()


def test_receive_dir_finalize_rel_path_deduplication(tmp_path: Path) -> None:
    """Kiểm tra khi tệp trong thư mục con bị trùng tên, tự động đánh số `doc (1).txt`."""
    recv_root = tmp_path / "received"
    recv_root.mkdir()

    # Tạo trước file đã tồn tại
    existing_dir = recv_root / "docs"
    existing_dir.mkdir(parents=True)
    (existing_dir / "notes.txt").write_text("old content", encoding="utf-8")

    offer_files = (FileEntry(name="notes.txt", size=4, mtime=2000, rel_path="docs/notes.txt"),)
    t_id = "0" * 32
    offer = Offer(transfer_id=t_id, files=offer_files, block_size=BLOCK_SIZE)
    record = TransferRecord(receiver="a" * 32, sender="b" * 32, offer=offer, verified=0, done_at=None)

    with ReceiveDir.open(recv_root) as rd:
        temp = rd.create_temp(t_id, record)
        part = temp.create_part(0, 4)
        part.write_at(0, b"new!")
        final = rd.finalize(part, "notes.txt", rel_path="docs/notes.txt")

        # File mới phải mang tên `notes (1).txt` bên trong thư mục `docs`
        assert final == recv_root / "docs" / "notes (1).txt"
        assert final.is_file()
        assert final.read_bytes() == b"new!"
        assert (existing_dir / "notes.txt").read_bytes() == b"old content"
