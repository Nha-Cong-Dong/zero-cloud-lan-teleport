"""Hàm thuần của `teleport.ui.send` (spec PR3 pha 1, mục `ui/send.py`; spec-hackathon 9.9, mục 10 A2).

Kiểm theo đặc tả, không theo code: câu chữ lấy từ mvp mục 8 (dòng “Chọn gửi…”, “Chọn quá số file”,
“Chọn một file hai lần”, “Máy kia từ chối máy này”, “Danh tính thiết bị thay đổi”, “Khác phiên bản”).
Câu mới (chưa có trong mvp) là đề xuất của spec, để thành hằng ở đầu file cho dễ sửa khi người dùng
duyệt lại. Test `test_section8_sentences_match_mvp_doc` đọc lại mvp.md để hằng không lệch tài liệu.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

PEER = "PC-Nha"

# ---- Câu mvp mục 8 (dạng mẫu: `{p}` tên máy, `{name}` tên file, `{path}` đường dẫn, `{size}` cỡ) ----
TOO_MANY = "Mỗi lần gửi được tối đa {n} file."
DUPLICATE = "Có file được chọn hai lần. Hãy chọn mỗi file một lần."
NOT_FOUND = "Không tìm thấy file {path}."
NOT_A_FILE = "{name} không phải là file. Teleport chưa gửi được cả thư mục; hãy chọn từng file."
PERMISSION = "Không mở được {name}: máy này không có quyền đọc file đó."
TOO_LARGE = (
    "Không gửi được {name} ({size}): Teleport chỉ gửi được file tối đa 4 TB. "
    "Hãy chia file thành nhiều phần nhỏ hơn rồi gửi."
)
BAD_NAME = "Không gửi được {name}: tên file có ký tự không hiển thị được. Hãy đổi tên file rồi gửi lại."
OS_ERROR = "Không đọc được {name}: ổ đĩa báo lỗi."
BLOCKED_REJECTED = "{p} từ chối kết nối từ máy này. Có thể máy đó đã hủy ghép đôi."
BLOCKED_IDENTITY = (
    "{p} đang dùng một danh tính khác với lúc ghép đôi. Có thể có người đang giả mạo máy này. "
    "Kết nối đã bị chặn."
)
BLOCKED_VERSION = "{p} đang dùng phiên bản Teleport khác. Hãy cập nhật cả hai máy lên cùng phiên bản."

# ---- Câu mới (spec PR3 pha 1, đề xuất; chờ người dùng duyệt) ----
NOT_PAIRED = "Chưa ghép đôi với {p} nên chưa gửi được. Hãy ghép đôi rồi gửi lại."
SEND_FAILED = "Không gửi được file tới {p}. Hãy thử lại."

REPLACEMENT = "\ufffd"
_MVP = Path(__file__).resolve().parents[2] / "docs" / "mvp.md"


def fill(template: str, **values: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), template)


def source_error(path: Path, kind_name: str, **kw):
    from teleport.app.sources import SourceError, SourceErrorKind

    return SourceError(path, SourceErrorKind[kind_name.upper()], **kw)


@pytest.fixture
def send():
    from teleport.ui import send as module

    return module


@pytest.fixture
def no_disk():
    """Ngữ cảnh: mọi lời gọi chạm đĩa (stat, exists, isdir, open, realpath…) ném lỗi.

    Dùng `with no_disk(): …` để chỉ chặn trong lúc gọi hàm cần kiểm (pytest cần đĩa ngoài đó).
    """

    def boom(*args, **kwargs):
        raise AssertionError(f"thread Qt không được chạm đĩa (D7): {args!r}")

    @contextlib.contextmanager
    def guard():
        with pytest.MonkeyPatch.context() as m:
            for name in ("stat", "lstat", "scandir", "listdir"):
                m.setattr(os, name, boom)
            for name in ("exists", "isdir", "isfile", "islink", "realpath", "getsize", "lexists"):
                m.setattr(os.path, name, boom)
            for name in ("stat", "lstat", "exists", "is_dir", "is_file", "resolve", "open", "is_symlink"):
                m.setattr(pathlib.Path, name, boom)
            m.setattr("builtins.open", boom)
            yield

    return guard


# =================================================================================================
# local_paths
# =================================================================================================


def test_local_paths_keeps_local_files_in_order_and_skips_other_urls(send, tmp_path):
    from PySide6.QtCore import QUrl

    a, b, c = tmp_path / "b.txt", tmp_path / "a.txt", tmp_path / "loi tat.lnk"
    urls = [
        QUrl("https://example.com/x.zip"),
        QUrl.fromLocalFile(str(a)),
        QUrl("ftp://example.com/y.bin"),
        QUrl.fromLocalFile(str(b)),
        QUrl("mailto:ai@example.com"),
        QUrl.fromLocalFile(str(c)),
        QUrl(),
    ]
    assert send.local_paths(urls) == [a, b, c]


def test_local_paths_keeps_lnk_as_is(send, tmp_path):
    """`.lnk` không được giải lối tắt: gửi nguyên file `.lnk`."""
    from PySide6.QtCore import QUrl

    link = tmp_path / "Video.lnk"
    assert send.local_paths([QUrl.fromLocalFile(str(link))]) == [link]


def test_local_paths_empty_and_only_remote_give_empty_list(send):
    from PySide6.QtCore import QUrl

    assert send.local_paths([]) == []
    assert send.local_paths([QUrl("https://example.com/a.txt")]) == []


def test_local_paths_does_not_touch_disk(send, tmp_path, no_disk):
    """File không tồn tại vẫn được trả về; không có lời gọi chạm đĩa nào (D7)."""
    from PySide6.QtCore import QUrl

    paths = [tmp_path / "khong-co" / f"f{i}.bin" for i in range(3)]
    urls = [QUrl.fromLocalFile(str(p)) for p in paths]
    with no_disk():
        result = send.local_paths(urls)
    assert result == paths


# =================================================================================================
# selection_error (9.9)
# =================================================================================================


def test_selection_error_empty_selection_is_none(send):
    assert send.selection_error([]) is None


def test_selection_error_distinct_files_is_none(send, tmp_path):
    paths = [tmp_path / "a.txt", tmp_path / "sub" / "a.txt", tmp_path / "b.txt"]
    assert send.selection_error(paths) is None


def test_selection_error_exactly_max_files_is_none(send, tmp_path):
    from teleport.core.messages import MAX_OFFER_FILES

    paths = [tmp_path / f"f{i}.bin" for i in range(MAX_OFFER_FILES)]
    assert send.selection_error(paths) is None


def test_selection_error_one_over_max_files(send, tmp_path):
    from teleport.core.messages import MAX_OFFER_FILES

    paths = [tmp_path / f"f{i}.bin" for i in range(MAX_OFFER_FILES + 1)]
    assert send.selection_error(paths) == fill(TOO_MANY, n=str(MAX_OFFER_FILES))
    assert MAX_OFFER_FILES == 1000  # câu mvp mục 8 viết “1000”


def test_selection_error_reads_limit_at_call_time(send, tmp_path, monkeypatch):
    """Số trong câu lấy từ `MAX_OFFER_FILES` lúc gọi, không viết cứng 1000."""
    from teleport.core import messages

    monkeypatch.setattr(messages, "MAX_OFFER_FILES", 3)
    monkeypatch.setattr(send, "MAX_OFFER_FILES", 3, raising=False)
    paths = [tmp_path / f"f{i}.bin" for i in range(4)]
    assert send.selection_error(paths) == fill(TOO_MANY, n="3")
    assert send.selection_error(paths[:3]) is None


def test_selection_error_same_path_twice(send, tmp_path):
    a = tmp_path / "a.txt"
    assert send.selection_error([a, tmp_path / "b.txt", a]) == DUPLICATE


def test_selection_error_relative_and_absolute_same_file(send, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert send.selection_error([Path("a.txt"), tmp_path / "a.txt"]) == DUPLICATE
    assert send.selection_error([Path("sub") / ".." / "a.txt", Path("a.txt")]) == DUPLICATE


@pytest.mark.skipif(sys.platform != "win32", reason="Windows: đường dẫn không phân biệt hoa/thường")
def test_selection_error_case_insensitive_duplicate_on_windows(send):
    assert send.selection_error([Path(r"C:\Data\Video.MP4"), Path(r"c:\data\video.mp4")]) == DUPLICATE
    assert send.selection_error([Path("C:/Data/a.txt"), Path(r"C:\Data\a.txt")]) == DUPLICATE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX: đường dẫn phân biệt hoa/thường")
def test_selection_error_case_differs_is_distinct_on_posix(send):
    assert send.selection_error([Path("/data/Video.mp4"), Path("/data/video.mp4")]) is None


def test_selection_error_counts_before_duplicates(send, tmp_path):
    """Quá số file VÀ có trùng → câu quá số file (đếm xét trước)."""
    from teleport.core.messages import MAX_OFFER_FILES

    paths = [tmp_path / "a.txt"] * (MAX_OFFER_FILES + 1)
    assert send.selection_error(paths) == fill(TOO_MANY, n=str(MAX_OFFER_FILES))


def test_selection_error_does_not_touch_disk(send, tmp_path, no_disk, monkeypatch):
    monkeypatch.chdir(tmp_path)
    many = [tmp_path / f"f{i}.bin" for i in range(1001)]
    dup = [Path("a.txt"), tmp_path / "a.txt"]
    ok = [tmp_path / "x" / "a.txt", tmp_path / "b.txt"]
    with no_disk():
        results = (send.selection_error(many), send.selection_error(dup), send.selection_error(ok))
    assert results == (fill(TOO_MANY, n="1000"), DUPLICATE, None)


# =================================================================================================
# send_error_message
# =================================================================================================


def test_send_error_not_found_shows_full_path(send, tmp_path):
    path = tmp_path / "x" / "video.mp4"
    assert send.send_error_message(source_error(path, "not_found"), PEER) == fill(NOT_FOUND, path=str(path))


def test_send_error_not_found_replaces_control_chars_in_path(send, tmp_path):
    path = tmp_path / "th\x1bu\x07muc" / "bao\ncao.txt"
    msg = send.send_error_message(source_error(path, "not_found"), PEER)
    assert msg is not None
    assert not any(ch in msg for ch in "\x1b\x07\n")
    expected = str(path).replace("\x1b", REPLACEMENT).replace("\x07", REPLACEMENT).replace("\n", REPLACEMENT)
    assert msg == fill(NOT_FOUND, path=expected)


def test_send_error_not_a_file(send, tmp_path):
    msg = send.send_error_message(source_error(tmp_path / "ảnh", "not_a_file"), PEER)
    assert msg == fill(NOT_A_FILE, name="ảnh")


def test_send_error_permission(send, tmp_path):
    msg = send.send_error_message(source_error(tmp_path / "video.mp4", "permission"), PEER)
    assert msg == fill(PERMISSION, name="video.mp4")


def test_send_error_too_large_includes_size(send, tmp_path):
    from teleport.core.display import format_size

    size = int(5.2 * 2**40)
    msg = send.send_error_message(source_error(tmp_path / "backup.img", "too_large", size=size), PEER)
    assert format_size(size) == "5,2 TB"
    assert msg == fill(TOO_LARGE, name="backup.img", size="5,2 TB")


def test_send_error_bad_name_shows_replacement_char(send, tmp_path):
    """mvp mục 8: “Không gửi được *bao�cao.txt*…”; ký tự điều khiển không in thẳng."""
    msg = send.send_error_message(source_error(tmp_path / "bao\ncao.txt", "bad_name"), PEER)
    assert msg is not None and "\n" not in msg
    assert msg == fill(BAD_NAME, name=f"bao{REPLACEMENT}cao.txt")


def test_send_error_os_error(send, tmp_path):
    msg = send.send_error_message(source_error(tmp_path / "video.mp4", "os_error", errno=5), PEER)
    assert msg == fill(OS_ERROR, name="video.mp4")


def test_send_error_every_source_kind_has_message_without_control_chars(send, tmp_path):
    from teleport.app.sources import SourceErrorKind

    path = tmp_path / "a\x00\x1f\x7fb.txt"
    for kind in SourceErrorKind:
        kw = {"size": 2**43} if kind is SourceErrorKind.TOO_LARGE else {}
        msg = send.send_error_message(source_error(path, kind.value, **kw), PEER)
        assert msg, kind
        assert not any(ch in msg for ch in "\x00\x1f\x7f"), (kind, msg)
        assert "`" not in msg, (kind, msg)  # GUI không dùng backtick


def test_send_error_long_file_name_is_cut_to_48_columns(send, tmp_path):
    from teleport.core.display import text_width

    name = "x" * 300 + ".mp4"
    msg = send.send_error_message(source_error(tmp_path / name, "permission"), PEER)
    assert msg is not None and name not in msg
    shown = msg.removeprefix("Không mở được ").split(":", 1)[0]
    assert text_width(shown) <= 48 and shown.endswith(".mp4")


def test_send_error_key_error_means_not_paired(send):
    assert send.send_error_message(KeyError("abc"), PEER) == fill(NOT_PAIRED, p=PEER)


def test_send_error_value_error_gives_generic_message(send):
    """Chọn trùng đã được `selection_error` lọc trước: `ValueError` không đoán là trùng, báo câu chung."""
    assert send.send_error_message(ValueError("trùng"), PEER) == fill(SEND_FAILED, p=PEER)


def test_not_paired_message_matches_key_error_sentence(send):
    assert send.not_paired_message(PEER) == fill(NOT_PAIRED, p=PEER)


def test_send_error_runtime_error_is_silent(send):
    """Node đang dừng (`NodeHost` trả RuntimeError): không báo gì."""
    assert send.send_error_message(RuntimeError("node đã dừng"), PEER) is None


@pytest.mark.parametrize("error", [OSError(5, "io"), TypeError("x"), Exception("y"), ConnectionError()])
def test_send_error_other_errors_give_generic_message(send, error):
    assert send.send_error_message(error, PEER) == fill(SEND_FAILED, p=PEER)


def test_send_error_does_not_leak_exception_text(send):
    msg = send.send_error_message(Exception("<b>chi-tiet-noi-bo</b>"), PEER)
    assert msg is not None and "chi-tiet-noi-bo" not in msg


# =================================================================================================
# blocked_message (mục 10 A2)
# =================================================================================================


@pytest.mark.parametrize(
    ("value", "template"),
    [
        ("rejected", BLOCKED_REJECTED),
        ("identity_mismatch", BLOCKED_IDENTITY),
        ("version_mismatch", BLOCKED_VERSION),
    ],
)
def test_blocked_message_for_blocking_results(send, value, template):
    from teleport.core.links import ConnectResult

    expected = fill(template, p=PEER)
    assert send.blocked_message(ConnectResult(value), PEER) == expected
    assert send.blocked_message(value, PEER) == expected  # nhận cả chuỗi giá trị


@pytest.mark.parametrize("value", ["ok", "unreachable", "khong-ro"])
def test_blocked_message_other_results_do_not_block(send, value):
    assert send.blocked_message(value, PEER) is None


def test_blocked_message_every_non_blocking_connect_result_is_none(send):
    from teleport.core.links import ConnectResult

    blocking = {"rejected", "identity_mismatch", "version_mismatch"}
    for result in ConnectResult:
        if result.value not in blocking:
            assert send.blocked_message(result, PEER) is None, result


# =================================================================================================
# Câu mục 8 trong file này khớp mvp.md (tài liệu là nguồn sự thật)
# =================================================================================================


def test_section8_sentences_match_mvp_doc():
    doc = _MVP.read_text(encoding="utf-8")
    samples = [
        fill(TOO_MANY, n="*1000*"),
        DUPLICATE,
        fill(NOT_FOUND, path="*D:\\x\\video.mp4*"),
        fill(NOT_A_FILE, name="*ảnh*"),
        fill(PERMISSION, name="*video.mp4*"),
        fill(TOO_LARGE, name="*backup.img*", size="5,2 TB"),
        fill(BAD_NAME, name="*bao�cao.txt*"),
        fill(OS_ERROR, name="*video.mp4*"),
        fill(BLOCKED_REJECTED, p="*PC-Nha*"),
        fill(BLOCKED_IDENTITY, p="*PC-Nha*"),
        fill(BLOCKED_VERSION, p="*PC-Nha*"),
        fill(NOT_PAIRED, p="*PC-Nha*"),
        fill(SEND_FAILED, p="*PC-Nha*"),
    ]
    for sample in samples:
        assert sample in doc, sample
