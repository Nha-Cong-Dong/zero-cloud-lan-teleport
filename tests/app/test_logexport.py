"""Test `teleport.app.logexport.export_logs` (C1, spec-dot-c mục 1.2.5; mục 11 L5).

`export_logs(log_files, dest) -> Path`: zip (ghi nguyên tử) gồm các file log còn tồn tại, đúng thứ tự
đưa vào, cùng `info.txt` (phiên bản app, phiên bản giao thức, hệ điều hành, phiên bản Python); không
có tên máy, `device_id`, `settings.json`, `trusted.json`. File lớn hơn `MAX_EXPORT_FILE` (8 MiB) thì
giữ 8 MiB **cuối** và ghi chú trong `info.txt`. `dest` không tuyệt đối → ValueError; OSError bay ra.

Giả định (spec không nói): tên mục trong zip của một file log là tên file (`Path.name`).
Module chỉ import trong fixture: chưa có thì từng test đỏ.
"""

import io
import platform
import socket
import zipfile
from pathlib import Path

import pytest

import teleport
from teleport import atomic
from teleport.core.messages import PROTOCOL_VERSION
from teleport.trust.identity import load_or_create_identity

MiB = 1024 * 1024


@pytest.fixture
def L():
    from teleport.app import logexport

    return logexport


@pytest.fixture
def logs(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    old = d / "teleport.log.1"
    new = d / "teleport.log"
    old.write_bytes(b"dong cu 1\ndong cu 2\n")
    new.write_bytes("dòng mới: lỗi giả lập\n".encode())
    return [old, new]


def read_zip(path: Path) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(path.read_bytes()))


def info_text(z: zipfile.ZipFile) -> str:
    return z.read("info.txt").decode("utf-8")


def test_max_export_file_is_8_mib(L):
    assert 8 * MiB == L.MAX_EXPORT_FILE


def test_zip_contents_and_order(L, logs, tmp_path):
    dest = tmp_path / "xuat" / "teleport-logs.zip"
    out = L.export_logs(logs, dest)
    assert Path(out) == dest
    z = read_zip(dest)
    assert z.testzip() is None
    names = z.namelist()
    assert set(names) == {"teleport.log.1", "teleport.log", "info.txt"}
    log_names = [n for n in names if n != "info.txt"]
    assert log_names == ["teleport.log.1", "teleport.log"], "phải giữ thứ tự đưa vào"
    assert z.read("teleport.log.1") == logs[0].read_bytes()
    assert z.read("teleport.log") == logs[1].read_bytes()
    info = info_text(z)
    assert teleport.__version__ in info
    assert str(PROTOCOL_VERSION) in info
    assert node_os() in info
    assert platform.python_version() in info


def node_os() -> str:
    from teleport.app.node import device_os

    return device_os()


def test_order_follows_argument_not_name(L, logs, tmp_path):
    dest = tmp_path / "b.zip"
    L.export_logs(list(reversed(logs)), dest)
    names = [n for n in read_zip(dest).namelist() if n != "info.txt"]
    assert names == ["teleport.log", "teleport.log.1"]


def test_missing_files_skipped(L, logs, tmp_path):
    dest = tmp_path / "c.zip"
    missing = tmp_path / "logs" / "khong-co.log"
    L.export_logs([missing, logs[1]], dest)
    z = read_zip(dest)
    assert set(z.namelist()) == {"teleport.log", "info.txt"}


def test_no_log_files_still_gives_info_only(L, tmp_path):
    dest = tmp_path / "d.zip"
    L.export_logs([], dest)
    assert read_zip(dest).namelist() == ["info.txt"]


def test_info_has_no_name_or_device_id(L, logs, tmp_path, monkeypatch):
    """N9: zip không có tên máy, `device_id`, `settings.json`, `trusted.json` (kể cả khi thư mục cấu
    hình của node có đủ các file đó)."""
    home = tmp_path / "cfg"
    monkeypatch.setenv("TELEPORT_HOME", str(home))
    ident = load_or_create_identity(home)
    (home / "settings.json").write_text('{"version": 1, "name": "Ten-Bi-Mat-Q7"}', encoding="utf-8")
    (home / "trusted.json").write_text('{"version": 1, "devices": []}', encoding="utf-8")
    dest = tmp_path / "e.zip"
    L.export_logs(logs, dest)
    z = read_zip(dest)
    assert not any(Path(n).name in ("settings.json", "trusted.json") for n in z.namelist())
    blob = b"".join(z.read(n) for n in z.namelist())
    assert ident.device_id.encode() not in blob
    assert ident.device_id[:8].encode() not in z.read("info.txt")
    assert b"Ten-Bi-Mat-Q7" not in blob
    host = socket.gethostname()
    if len(host) >= 4:
        assert host.encode() not in z.read("info.txt")


def test_large_file_keeps_last_8_mib_and_notes_it(L, tmp_path):
    big = tmp_path / "teleport.log"
    head = b"DAU-FILE-BI-CAT\n"
    tail = b"CUOI-FILE-PHAI-GIU\n"
    filler = b"x" * (L.MAX_EXPORT_FILE + 100 - len(head) - len(tail))
    big.write_bytes(head + filler + tail)
    dest = tmp_path / "f.zip"
    L.export_logs([big], dest)
    z = read_zip(dest)
    got = z.read("teleport.log")
    assert len(got) == L.MAX_EXPORT_FILE
    assert got == big.read_bytes()[-L.MAX_EXPORT_FILE :]
    assert got.endswith(tail) and head not in got
    assert "teleport.log" in info_text(z)


def test_file_exactly_at_limit_is_not_cut(L, tmp_path):
    f = tmp_path / "teleport.log"
    f.write_bytes(b"A" + b"y" * (L.MAX_EXPORT_FILE - 1))
    dest = tmp_path / "g.zip"
    L.export_logs([f], dest)
    assert read_zip(dest).read("teleport.log") == f.read_bytes()


@pytest.mark.parametrize("dest", ["tuong-doi.zip", "logs/x.zip"])
def test_relative_dest_is_value_error(L, logs, dest, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        L.export_logs(logs, Path(dest))
    assert not (tmp_path / dest).exists()


def test_os_error_propagates(L, logs, tmp_path):
    blocker = tmp_path / "la-file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        L.export_logs(logs, blocker / "con" / "h.zip")


def test_write_is_atomic(L, logs, tmp_path, monkeypatch):
    """Ghi qua `atomic.write_atomic` đúng một lần với `dest`; ghi lỗi thì file cũ còn nguyên."""
    dest = tmp_path / "i.zip"
    dest.write_bytes(b"BAN CU")
    real = atomic.write_atomic
    seen: list[Path] = []

    def spy(path, content):
        seen.append(Path(path))
        return real(path, content)

    monkeypatch.setattr(atomic, "write_atomic", spy)
    if hasattr(L, "write_atomic"):
        monkeypatch.setattr(L, "write_atomic", spy)
    L.export_logs(logs, dest)
    assert seen == [dest]
    assert zipfile.is_zipfile(dest)

    dest.write_bytes(b"BAN CU")

    def failing(path, content):
        raise OSError(28, "giả lập hết chỗ trống")

    monkeypatch.setattr(atomic, "write_atomic", failing)
    if hasattr(L, "write_atomic"):
        monkeypatch.setattr(L, "write_atomic", failing)
    with pytest.raises(OSError):
        L.export_logs(logs, dest)
    assert dest.read_bytes() == b"BAN CU"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["i.zip", "logs"]


def test_does_not_touch_source_logs(L, logs, tmp_path):
    before = [p.read_bytes() for p in logs]
    L.export_logs(logs, tmp_path / "j.zip")
    assert [p.read_bytes() for p in logs] == before
