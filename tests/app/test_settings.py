"""Test `teleport.app.settings` theo đặc tả PR-3 mục 2.2 và 4.3 (ST1–ST9), quyết định Q3.

`load` không bao giờ raise vì nội dung file; file hỏng thì dùng mặc định và ghi WARNING (không ghi
nội dung). `save` chép file hỏng sang `settings.json.bak` trước khi ghi đè, và giữ trường lạ.
"""

import dataclasses
import json
import logging

import pytest

from teleport import atomic
from teleport.app import settings as settings_mod
from teleport.app.settings import (
    BACKUP_FILE,
    MAX_SETTINGS_SIZE,
    SETTINGS_FILE,
    SETTINGS_VERSION,
    Settings,
)

SECRET = "NOI-DUNG-BI-MAT-7Q2"  # nằm trong file hỏng; không được xuất hiện trong log (N9)


@pytest.fixture
def cfg(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    return d


def write(cfg, data: bytes | str) -> bytes:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    (cfg / SETTINGS_FILE).write_bytes(raw)
    return raw


def on_disk(cfg) -> dict:
    raw = (cfg / SETTINGS_FILE).read_bytes()
    return json.loads(raw.decode("utf-8"))


def warnings_of(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def no_tmp(cfg) -> bool:
    return not any(p.name.endswith(".tmp") for p in cfg.iterdir())


def test_constants_match_spec():
    assert SETTINGS_FILE == "settings.json"
    assert BACKUP_FILE == "settings.json.bak"
    assert SETTINGS_VERSION == 1
    assert MAX_SETTINGS_SIZE == 1024 * 1024


# --- ST1 ------------------------------------------------------------------------------------------


def test_missing_file_gives_defaults_and_creates_nothing(tmp_path, caplog):
    """ST1 (kể cả khi thư mục cấu hình chưa có)."""
    caplog.set_level(logging.DEBUG)
    cfg = tmp_path / "chua-co"
    s = Settings.load(cfg)
    assert s.port is None
    assert dict(s.extra) == {}
    assert not cfg.exists()
    assert warnings_of(caplog) == []

    cfg.mkdir()
    assert Settings.load(cfg).port is None
    assert list(cfg.iterdir()) == []


# --- ST2 ------------------------------------------------------------------------------------------


def test_save_then_load_roundtrip_and_file_format(cfg):
    """ST2."""
    Settings(port=47475).save(cfg)
    raw = (cfg / SETTINGS_FILE).read_bytes()
    text = raw.decode("utf-8")
    assert text.endswith("\n")
    assert json.loads(text) == {"version": 1, "port": 47475}
    assert Settings.load(cfg).port == 47475
    assert no_tmp(cfg)
    assert not (cfg / BACKUP_FILE).exists()


def test_save_without_port_writes_only_version(cfg):
    Settings().save(cfg)
    assert on_disk(cfg) == {"version": 1}
    assert Settings.load(cfg).port is None


def test_save_creates_config_dir(tmp_path):
    cfg = tmp_path / "moi" / "sau"
    Settings(port=5000).save(cfg)
    assert Settings.load(cfg).port == 5000


def test_save_keeps_non_ascii_unescaped(cfg):
    """`ensure_ascii=False`: chuỗi tiếng Việt nằm nguyên trong file UTF-8."""
    Settings(port=5000, extra={"ghi_chu": "Máy nhà"}).save(cfg)
    assert "Máy nhà" in (cfg / SETTINGS_FILE).read_text(encoding="utf-8")


# --- ST3 ------------------------------------------------------------------------------------------


def test_unknown_fields_survive_rewrite(cfg):
    """ST3."""
    write(cfg, '{"version":1,"port":5000,"future":{"a":[1,2]}}')
    s = Settings.load(cfg)
    assert s.port == 5000
    dataclasses.replace(s, port=6000).save(cfg)
    data = on_disk(cfg)
    assert data["port"] == 6000
    assert data["future"] == {"a": [1, 2]}
    assert data["version"] == 1
    assert Settings.load(cfg).port == 6000
    assert not (cfg / BACKUP_FILE).exists()


def test_extra_holds_every_key_except_port(cfg):
    write(cfg, '{"version":1,"port":5000,"a":1,"b":null}')
    s = Settings.load(cfg)
    assert dict(s.extra) == {"version": 1, "a": 1, "b": None}


# --- ST4 ------------------------------------------------------------------------------------------


CORRUPT = {
    "rac": f"day khong phai json {SECRET}".encode(),
    "utf8_hong": b'{"port": 5000, "x": "' + SECRET.encode() + b'\xff\xfe"}',
    "mang": f'["{SECRET}", 5000]'.encode(),
    "chuoi": f'"{SECRET}"'.encode(),
    "so": b"5000",
    "long_sau": b"[" * 100_000,
    "qua_lon": (b'{"port": 5000, "pad": "' + SECRET.encode() + b"x" * (MAX_SETTINGS_SIZE + 10) + b'"}'),
}


@pytest.mark.parametrize("kind", list(CORRUPT))
def test_corrupt_file_load_gives_defaults_with_warning_without_content(cfg, caplog, kind):
    """ST4 (load): mặc định, có WARNING không chứa nội dung, file không bị đổi."""
    caplog.set_level(logging.DEBUG)
    raw = write(cfg, CORRUPT[kind])
    s = Settings.load(cfg)
    assert s.port is None
    assert dict(s.extra) == {}
    assert warnings_of(caplog), "phải có log WARNING khi file hỏng"
    assert SECRET not in caplog.text
    assert (cfg / SETTINGS_FILE).read_bytes() == raw
    assert not (cfg / BACKUP_FILE).exists()


@pytest.mark.parametrize("kind", list(CORRUPT))
def test_corrupt_file_save_backs_up_original_bytes_first(cfg, kind):
    """ST4 (save): `.bak` đúng từng byte bản cũ, file mới hợp lệ."""
    raw = write(cfg, CORRUPT[kind])
    s = Settings.load(cfg)
    dataclasses.replace(s, port=5555).save(cfg)
    assert (cfg / BACKUP_FILE).read_bytes() == raw
    assert on_disk(cfg) == {"version": 1, "port": 5555}
    assert no_tmp(cfg)


def test_file_exactly_at_size_limit_is_not_too_big(cfg):
    """Biên: đúng MAX_SETTINGS_SIZE byte vẫn đọc (chỉ lớn hơn mới bị coi là hỏng)."""
    head = b'{"port": 5000, "pad": "'
    tail = b'"}'
    raw = head + b"x" * (MAX_SETTINGS_SIZE - len(head) - len(tail)) + tail
    assert len(raw) == MAX_SETTINGS_SIZE
    write(cfg, raw)
    assert Settings.load(cfg).port == 5000
    write(cfg, raw[:-2] + b'x"}')  # vượt 1 byte
    assert Settings.load(cfg).port is None


def test_unreadable_settings_path_gives_defaults_with_warning(cfg, caplog):
    """Lỗi đọc khác FileNotFoundError (ở đây settings.json là thư mục) → WARNING, mặc định."""
    caplog.set_level(logging.DEBUG)
    (cfg / SETTINGS_FILE).mkdir()
    s = Settings.load(cfg)
    assert s.port is None
    assert warnings_of(caplog)


# --- ST5 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ['"47474"', "0", "65536", "-1", "true", "false", "1.5", "47474.0", "[1]"])
def test_bad_port_is_dropped_with_warning_and_backed_up_on_save(cfg, caplog, bad):
    """ST5: port sai → None + WARNING, khóa bị bỏ; save tạo `.bak`."""
    caplog.set_level(logging.DEBUG)
    raw = write(cfg, f'{{"version": 1, "port": {bad}, "khac": "giu"}}')
    s = Settings.load(cfg)
    assert s.port is None
    assert "port" not in s.extra
    assert s.extra.get("khac") == "giu"
    assert warnings_of(caplog)
    s.save(cfg)
    assert (cfg / BACKUP_FILE).read_bytes() == raw
    assert on_disk(cfg) == {"version": 1, "khac": "giu"}


def test_null_port_is_none_without_warning_and_no_backup(cfg, caplog):
    """ST5: `"port": null` → None, không WARNING; save không tạo `.bak`."""
    caplog.set_level(logging.DEBUG)
    write(cfg, '{"version": 1, "port": null}')
    s = Settings.load(cfg)
    assert s.port is None
    assert warnings_of(caplog) == []
    s.save(cfg)
    assert not (cfg / BACKUP_FILE).exists()
    assert on_disk(cfg) == {"version": 1}


@pytest.mark.parametrize("port", [1, 65535, 47474])
def test_valid_port_boundaries_load_without_warning(cfg, caplog, port):
    caplog.set_level(logging.DEBUG)
    write(cfg, f'{{"version": 1, "port": {port}}}')
    assert Settings.load(cfg).port == port
    assert warnings_of(caplog) == []


def test_missing_version_is_not_corrupt(cfg, caplog):
    """Không có `version` thì không phải lỗi; save ghi `version: 1`."""
    caplog.set_level(logging.DEBUG)
    write(cfg, '{"port": 5000}')
    s = Settings.load(cfg)
    assert s.port == 5000
    assert warnings_of(caplog) == []
    s.save(cfg)
    assert not (cfg / BACKUP_FILE).exists()
    assert on_disk(cfg) == {"version": 1, "port": 5000}


# --- ST6 ------------------------------------------------------------------------------------------


def test_newer_version_is_kept_not_an_error(cfg, caplog):
    """ST6: `version: 7` giữ nguyên cùng trường lạ; không WARNING, không `.bak`."""
    caplog.set_level(logging.DEBUG)
    write(cfg, '{"version": 7, "port": 5000, "thiet_bi": {"x": 1}}')
    s = Settings.load(cfg)
    assert s.port == 5000
    assert warnings_of(caplog) == []
    dataclasses.replace(s, port=5001).save(cfg)
    assert not (cfg / BACKUP_FILE).exists()
    assert on_disk(cfg) == {"version": 7, "port": 5001, "thiet_bi": {"x": 1}}


@pytest.mark.parametrize("bad", ['"1"', "0", "-3", "true", "1.0", "null", "[1]"])
def test_bad_version_is_dropped_with_warning_and_backed_up(cfg, caplog, bad):
    """ST6: `version` không phải int ≥ 1 → WARNING; save tạo `.bak` và ghi `version: 1`."""
    caplog.set_level(logging.DEBUG)
    raw = write(cfg, f'{{"version": {bad}, "port": 5000}}')
    s = Settings.load(cfg)
    assert s.port == 5000
    assert "version" not in s.extra
    assert warnings_of(caplog)
    s.save(cfg)
    assert (cfg / BACKUP_FILE).read_bytes() == raw
    assert on_disk(cfg) == {"version": 1, "port": 5000}


# --- ST7 ------------------------------------------------------------------------------------------


def test_utf8_bom_is_accepted(cfg, caplog):
    """ST7."""
    caplog.set_level(logging.DEBUG)
    write(cfg, b"\xef\xbb\xbf" + b'{"version": 1, "port": 5000}')
    assert Settings.load(cfg).port == 5000
    assert warnings_of(caplog) == []


# --- ST8 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"port": 0},
        {"port": 70000},
        {"port": 65536},
        {"port": -1},
        {"port": True},
        {"port": False},
        {"port": "47474"},
        {"port": 47474.0},
        {"extra": {"port": 1}},
        {"extra": {1: "x"}},
        {"extra": [("a", 1)]},
        {"extra": None},
    ],
)
def test_invalid_construction_raises_value_error(kwargs):
    """ST8."""
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_valid_construction_and_extra_is_copied():
    src = {"a": 1}
    s = Settings(port=1, extra=src)
    src["a"] = 2
    src["b"] = 3
    assert dict(s.extra) == {"a": 1}
    assert Settings(port=65535).port == 65535
    # extra không tham gia so sánh.
    assert Settings(port=5, extra={"x": 1}) == Settings(port=5)


def test_settings_is_frozen():
    s = Settings(port=5000)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.port = 6000  # type: ignore[misc]


def test_unserializable_extra_raises_type_error(cfg):
    with pytest.raises(TypeError):
        Settings(port=5000, extra={"x": object()}).save(cfg)


# --- ST9 ------------------------------------------------------------------------------------------


def test_old_backup_is_overwritten(cfg):
    """ST9 (a)."""
    (cfg / BACKUP_FILE).write_bytes(b"BAN SAO RAT CU")
    raw = write(cfg, b"rac moi")
    Settings.load(cfg).save(cfg)
    assert (cfg / BACKUP_FILE).read_bytes() == raw


def test_backup_write_failure_raises_and_keeps_main_file(cfg, monkeypatch):
    """ST9 (b): ghi `.bak` lỗi → OSError; file chính còn nguyên, không ghi file chính."""
    raw = write(cfg, b"rac can giu")
    real = atomic.write_atomic
    written = []

    def failing(path, content):
        written.append(path.name)
        if path.name == BACKUP_FILE:
            raise OSError(28, "giả lập hết chỗ trống")
        return real(path, content)

    monkeypatch.setattr(atomic, "write_atomic", failing)
    if hasattr(settings_mod, "write_atomic"):
        monkeypatch.setattr(settings_mod, "write_atomic", failing)

    s = Settings.load(cfg)
    with pytest.raises(OSError):
        dataclasses.replace(s, port=5000).save(cfg)
    assert (cfg / SETTINGS_FILE).read_bytes() == raw
    assert SETTINGS_FILE not in written
    assert BACKUP_FILE in written


def test_main_write_failure_raises_oserror(cfg, monkeypatch):
    """Lỗi ghi file chính bay ra (OSError), không bị nuốt."""
    real = atomic.write_atomic

    def failing(path, content):
        if path.name == SETTINGS_FILE:
            raise PermissionError(13, "giả lập không có quyền ghi")
        return real(path, content)

    monkeypatch.setattr(atomic, "write_atomic", failing)
    if hasattr(settings_mod, "write_atomic"):
        monkeypatch.setattr(settings_mod, "write_atomic", failing)
    with pytest.raises(OSError):
        Settings(port=5000).save(cfg)
