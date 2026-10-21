"""Test `teleport.atomic.write_atomic` theo đặc tả PR-3 mục 2.1 và 4.2 (AT1–AT7).

Viết theo đặc tả, không theo code: ghi `<path>.tmp` → flush + fsync → `os.replace` (thử lại khi
gặp `PermissionError`) → fsync thư mục cha (POSIX). Lỗi thì xóa `.tmp` rồi raise `OSError` nguyên
trạng; file đích hoặc còn bản cũ, hoặc đã là bản mới.
"""

import itertools
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from teleport import atomic
from teleport.atomic import REPLACE_ATTEMPTS, REPLACE_DELAY, write_atomic
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore, TrustStoreError


def tmp_of(path):
    return path.with_name(path.name + ".tmp")


@pytest.fixture
def quick_retry(monkeypatch):
    """Rút ngắn khoảng chờ giữa hai lần thử os.replace (hằng số đọc lúc dùng, mục 2)."""
    monkeypatch.setattr(atomic, "REPLACE_DELAY", 0.001)


def test_constants_match_spec():
    assert REPLACE_ATTEMPTS == 5
    assert REPLACE_DELAY == 0.05


# --- AT1, AT2: đường đi đúng ----------------------------------------------------------------------


def test_creates_parent_dirs_and_writes_exact_content(tmp_path):
    """AT1."""
    path = tmp_path / "a" / "b" / "settings.json"
    write_atomic(path, b'{"x": 1}\n')
    assert path.read_bytes() == b'{"x": 1}\n'
    assert not tmp_of(path).exists()
    assert sorted(p.name for p in path.parent.iterdir()) == ["settings.json"]


def test_writes_empty_content(tmp_path):
    path = tmp_path / "empty"
    write_atomic(path, b"")
    assert path.exists()
    assert path.read_bytes() == b""
    assert not tmp_of(path).exists()


def test_overwrites_existing_file(tmp_path):
    """AT2."""
    path = tmp_path / "f.json"
    path.write_bytes(b"cu" * 1000)
    write_atomic(path, b"moi")
    assert path.read_bytes() == b"moi"
    assert not tmp_of(path).exists()


def test_leftover_tmp_from_previous_run_is_truncated(tmp_path):
    """File .tmp còn sót (dài hơn nội dung mới) không được để lại byte thừa trong file đích."""
    path = tmp_path / "f.json"
    tmp_of(path).write_bytes(b"X" * 4096)
    write_atomic(path, b"ngan")
    assert path.read_bytes() == b"ngan"
    assert not tmp_of(path).exists()


def test_binary_content_is_not_translated(tmp_path):
    """Không đổi \\n thành \\r\\n trên Windows (ghi nhị phân)."""
    path = tmp_path / "bin"
    data = bytes(range(256)) + b"\n\r\n"
    write_atomic(path, data)
    assert path.read_bytes() == data


# --- AT3: os.replace bị khóa ------------------------------------------------------------------------


def test_replace_permission_error_twice_then_succeeds(tmp_path, monkeypatch, quick_retry):
    """AT3 (a): PermissionError 2 lần rồi thành công → ghi được."""
    path = tmp_path / "f.json"
    path.write_bytes(b"cu")
    real_replace = os.replace
    calls = []

    def flaky(src, dst, *args, **kwargs):
        calls.append((os.fspath(src), os.fspath(dst)))
        if len(calls) <= 2:
            raise PermissionError(13, "giả lập antivirus giữ file")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)
    write_atomic(path, b"moi")
    monkeypatch.undo()

    assert len(calls) == 3
    assert all(os.path.basename(src) == "f.json.tmp" for src, _ in calls)
    assert all(os.path.basename(dst) == "f.json" for _, dst in calls)
    assert path.read_bytes() == b"moi"
    assert not tmp_of(path).exists()


def test_replace_permission_error_forever_raises_and_keeps_old_file(tmp_path, monkeypatch, quick_retry):
    """AT3 (b): raise mãi → PermissionError (là OSError) sau ≤ REPLACE_ATTEMPTS lần; .tmp bị xóa."""
    path = tmp_path / "f.json"
    path.write_bytes(b"ban cu")
    calls = []

    def always_locked(src, dst, *args, **kwargs):
        calls.append(dst)
        raise PermissionError(13, "giả lập file đang bị khóa")

    monkeypatch.setattr(os, "replace", always_locked)
    with pytest.raises(PermissionError) as exc:
        write_atomic(path, b"ban moi")
    monkeypatch.undo()

    assert isinstance(exc.value, OSError)
    assert 2 <= len(calls) <= REPLACE_ATTEMPTS
    assert path.read_bytes() == b"ban cu"
    assert not tmp_of(path).exists()


def test_replace_waits_between_attempts(tmp_path, monkeypatch):
    """Giữa hai lần thử có chờ (REPLACE_DELAY, đọc lúc dùng), không thử dồn dập."""
    monkeypatch.setattr(atomic, "REPLACE_DELAY", 0.02)
    path = tmp_path / "f.json"
    real_replace = os.replace
    stamps = []

    def flaky(src, dst, *args, **kwargs):
        stamps.append(time.monotonic())
        if len(stamps) <= 2:
            raise PermissionError(13, "khóa")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)
    write_atomic(path, b"x")
    monkeypatch.undo()
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    assert all(g >= 0.015 for g in gaps), gaps


def test_other_oserror_on_replace_is_not_retried(tmp_path, monkeypatch, quick_retry):
    """Chỉ PermissionError mới được thử lại; lỗi khác bay ra ngay, .tmp bị xóa."""
    path = tmp_path / "f.json"
    path.write_bytes(b"cu")
    calls = []

    def broken(src, dst, *args, **kwargs):
        calls.append(dst)
        raise OSError(28, "giả lập hết chỗ trống")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError) as exc:
        write_atomic(path, b"moi")
    monkeypatch.undo()
    assert exc.value.errno == 28
    assert len(calls) == 1
    assert path.read_bytes() == b"cu"
    assert not tmp_of(path).exists()


def test_target_is_directory_raises_oserror_and_cleans_tmp(tmp_path, quick_retry):
    path = tmp_path / "f.json"
    path.mkdir()
    with pytest.raises(OSError):
        write_atomic(path, b"x")
    assert path.is_dir()
    assert not tmp_of(path).exists()


# --- AT4: fsync lỗi ---------------------------------------------------------------------------------


def test_fsync_error_raises_oserror_cleans_tmp_keeps_old(tmp_path, monkeypatch):
    """AT4."""
    path = tmp_path / "f.json"
    path.write_bytes(b"ban cu")
    replaced = []
    real_replace = os.replace

    def bad_fsync(fd):
        raise OSError(5, "giả lập lỗi I/O")

    def spy_replace(src, dst, *args, **kwargs):
        replaced.append(dst)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", bad_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    with pytest.raises(OSError) as exc:
        write_atomic(path, b"ban moi")
    monkeypatch.undo()

    assert exc.value.errno == 5
    assert replaced == [], "không được os.replace khi fsync file thất bại"
    assert path.read_bytes() == b"ban cu"
    assert not tmp_of(path).exists()


def test_error_is_not_wrapped(tmp_path, monkeypatch):
    """Tiện ích dùng chung raise OSError nguyên trạng (không bọc thành TrustStoreError hay lớp khác)."""

    def bad_fsync(fd):
        raise OSError(5, "giả lập lỗi I/O")

    monkeypatch.setattr(os, "fsync", bad_fsync)
    with pytest.raises(OSError) as exc:
        write_atomic(tmp_path / "f", b"x")
    monkeypatch.undo()
    assert type(exc.value) is OSError
    assert not isinstance(exc.value, TrustStoreError)


# --- AT5: thứ tự --------------------------------------------------------------------------------------


def test_file_fsync_happens_before_replace(tmp_path, monkeypatch):
    """AT5: fsync của file .tmp được gọi trước os.replace."""
    path = tmp_path / "f.json"
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd):
        events.append("fsync")
        return real_fsync(fd)

    def spy_replace(src, dst, *args, **kwargs):
        # Lúc đổi tên, .tmp đã chứa đủ nội dung.
        events.append(("replace", Path(src).read_bytes()))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    write_atomic(path, b"noi dung")
    monkeypatch.undo()

    replace_at = next(i for i, e in enumerate(events) if isinstance(e, tuple))
    assert "fsync" in events[:replace_at], events
    assert events[replace_at] == ("replace", b"noi dung")
    if os.name == "posix":
        # POSIX: fsync thư mục cha sau khi đổi tên.
        assert "fsync" in events[replace_at + 1 :], events


# --- AT6: quyền file (POSIX) ----------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="quyền 0o600 chỉ có nghĩa trên POSIX")
def test_permissions_are_0600_even_with_stale_0644_tmp(tmp_path):
    """AT6."""
    path = tmp_path / "f.json"
    tmp = tmp_of(path)
    tmp.write_bytes(b"cu")
    os.chmod(tmp, 0o644)
    old_umask = os.umask(0)
    try:
        write_atomic(path, b"moi")
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    write_atomic(tmp_path / "new.json", b"x")
    assert stat.S_IMODE((tmp_path / "new.json").stat().st_mode) == 0o600


# --- AT7: TrustStore bọc lỗi ----------------------------------------------------------------------


def test_trust_store_wraps_replace_failure_in_trust_store_error(tmp_path, monkeypatch, quick_retry):
    """AT7: store dùng write_atomic và tự bọc lỗi thành TrustStoreError (nguyên nhân là PermissionError)."""
    cfg = tmp_path / "cfg"
    peer = peer_cert_from_pem(load_or_create_identity(tmp_path / "peer").cert_pem())
    store = TrustStore.load(cfg)

    def always_locked(src, dst, *args, **kwargs):
        raise PermissionError(13, "giả lập file đang bị khóa")

    monkeypatch.setattr(os, "replace", always_locked)
    with pytest.raises(TrustStoreError) as exc:
        store.add(peer, "PC")
    monkeypatch.undo()

    chain = []
    e: BaseException | None = exc.value
    while e is not None and len(chain) < 10:
        chain.append(e)
        e = e.__cause__ or e.__context__
    assert any(isinstance(x, PermissionError) for x in chain), chain
    assert store.devices() == []
    assert not (cfg / "trusted.json").exists()
    assert not (cfg / "trusted.json.tmp").exists()
