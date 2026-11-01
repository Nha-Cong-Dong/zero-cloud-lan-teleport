"""Test `teleport.app.settings` theo đặc tả PR-3 mục 2.2 và 4.3 (ST1–ST9), quyết định Q3.

`load` không bao giờ raise vì nội dung file; file hỏng thì dùng mặc định và ghi WARNING (không ghi
nội dung). `save` chép file hỏng sang `settings.json.bak` trước khi ghi đè, và giữ trường lạ.
"""

import dataclasses
import json
import logging
import os
import re
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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


# =================================================================================================
# C1 (= P4-0): đủ trường F10, cài đặt theo thiết bị (spec-dot-c mục 1.2.1, 1.5; mục 11 thắng:
# `language` dùng gạch dưới `[a-z]{2,3}(_[A-Z]{2})?`).
#
# Tên mới (`DeviceSettings`, `SettingsSaveError`) import trong thân test: chưa có thì chỉ test C1 đỏ,
# các test ST1–ST9 ở trên vẫn chạy. Giả định: khóa JSON trùng tên trường của dataclass
# (`name`, `receive_dir`, …, `devices: {device_id: {ask_before_receive, clipboard}}`).
# =================================================================================================

DEV_A = "a1" * 16
DEV_B = "b2" * 16
DEV_C = "c3" * 16
MAX_SAFE = 2**53 - 1


def _min_rate() -> int:
    from teleport.app.transfers import MIN_RATE_LIMIT

    return MIN_RATE_LIMIT


def _abs_dir(name: str = "nhan") -> str:
    return os.path.join(os.path.abspath(os.sep), "teleport-test", name)


def _normalized(s) -> dict:
    """Mọi trường so được của `Settings`, `devices` chỉ gồm mục khác mặc định (ghi ra file)."""
    from teleport.app.settings import DeviceSettings

    default = DeviceSettings()
    devices = {
        k: (v.ask_before_receive, v.clipboard)
        for k, v in s.devices.items()
        if (v.ask_before_receive, v.clipboard) != (default.ask_before_receive, default.clipboard)
    }
    return {
        "port": s.port,
        "name": s.name,
        "receive_dir": s.receive_dir,
        "visible": s.visible,
        "rate_limit": s.rate_limit,
        "temp_retention_days": s.temp_retention_days,
        "language": s.language,
        "autostart": s.autostart,
        "first_run_done": s.first_run_done,
        "radar_view": s.radar_view,
        # D6: `getattr` để các test cũ vẫn chạy khi trường chưa có; test D6 ở cuối file kiểm trường có thật.
        "theme": getattr(s, "theme", None),
        "accent": getattr(s, "accent", None),
        "devices": devices,
    }


def test_c1_settings_save_error_and_device_settings_exist():
    from teleport.app.settings import DeviceSettings, SettingsSaveError

    assert issubclass(SettingsSaveError, Exception)
    d = DeviceSettings()
    assert d.ask_before_receive is True
    assert d.clipboard is False
    assert dict(d.extra) == {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.ask_before_receive = False  # type: ignore[misc]


def test_c1_new_fields_have_spec_defaults():
    s = Settings()
    assert s.name is None
    assert s.receive_dir is None
    assert s.visible is True
    assert s.rate_limit is None
    assert s.temp_retention_days is None
    assert s.language is None
    assert s.autostart is False
    assert s.first_run_done is False
    assert s.radar_view is None
    assert dict(s.devices) == {}


def test_old_file_port_only_loads_defaults(cfg, caplog):
    """Tương thích: file cũ `{version, port}` đọc ra mọi trường mới ở mặc định, không cảnh báo."""
    caplog.set_level(logging.DEBUG)
    write(cfg, '{"version": 1, "port": 47475}')
    s = Settings.load(cfg)
    assert s.port == 47475
    assert _normalized(s) == _normalized(Settings(port=47475))
    assert warnings_of(caplog) == []


def test_new_fields_are_not_left_in_extra(cfg):
    write(cfg, json.dumps({"version": 1, "name": "PC-Nha", "visible": False, "devices": {}}))
    s = Settings.load(cfg)
    assert s.name == "PC-Nha" and s.visible is False
    assert set(s.extra) <= {"version"}


def test_full_file_loads_every_field(cfg, caplog):
    caplog.set_level(logging.DEBUG)
    rd = _abs_dir()
    data = {
        "version": 1,
        "port": 5000,
        "name": "Máy Nhà",
        "receive_dir": rd,
        "visible": False,
        "rate_limit": _min_rate(),
        "temp_retention_days": 7,
        "language": "vi_VN",
        "autostart": True,
        "first_run_done": True,
        "radar_view": "list",
        "devices": {DEV_A: {"ask_before_receive": False, "clipboard": True}},
    }
    write(cfg, json.dumps(data, ensure_ascii=False))
    s = Settings.load(cfg)
    assert warnings_of(caplog) == []
    assert (s.name, s.receive_dir, s.visible, s.rate_limit) == ("Máy Nhà", rd, False, _min_rate())
    assert (s.temp_retention_days, s.language, s.autostart, s.first_run_done) == (7, "vi_VN", True, True)
    assert s.radar_view == "list"
    dev = s.device(DEV_A)
    assert (dev.ask_before_receive, dev.clipboard) == (False, True)


def test_unknown_top_level_and_device_fields_survive_save(cfg, caplog):
    """`extra` giữ ở cấp file và ở từng mục; mục toàn mặc định nhưng có `extra` vẫn được ghi."""
    caplog.set_level(logging.DEBUG)
    write(
        cfg,
        json.dumps(
            {
                "version": 1,
                "port": 5000,
                "tuong_lai": {"x": [1, 2]},
                "devices": {
                    DEV_A: {"ask_before_receive": False, "moi": "giu"},
                    DEV_B: {"chi_co_extra": 1},
                },
            }
        ),
    )
    s = Settings.load(cfg)
    assert warnings_of(caplog) == []
    assert s.extra.get("tuong_lai") == {"x": [1, 2]}
    assert dict(s.device(DEV_A).extra) == {"moi": "giu"}
    assert dict(s.device(DEV_B).extra) == {"chi_co_extra": 1}
    dataclasses.replace(s, port=6000, name="Moi").save(cfg)
    data = on_disk(cfg)
    assert data["tuong_lai"] == {"x": [1, 2]}
    assert data["port"] == 6000 and data["name"] == "Moi" and data["version"] == 1
    assert data["devices"][DEV_A] == {"ask_before_receive": False, "moi": "giu"}
    assert data["devices"][DEV_B] == {"chi_co_extra": 1}
    assert not (cfg / BACKUP_FILE).exists()


def test_older_build_keys_round_trip_through_extra_of_this_build(cfg):
    """Khóa mới hơn bản này (bản cũ đọc file mới) rơi vào `extra` và được giữ khi ghi cổng."""
    write(cfg, '{"version": 2, "port": 5000, "khoa_ban_sau": true}')
    s = Settings.load(cfg)
    dataclasses.replace(s, port=5001).save(cfg)
    assert on_disk(cfg) == {"version": 2, "port": 5001, "khoa_ban_sau": True}


BAD_FIELDS = [
    # (trường, giá trị JSON sai)
    ("name", "5"),
    ("name", '""'),
    ("name", '" dau-cach"'),
    ("name", json.dumps("x" * 65)),
    ("name", "true"),
    ("receive_dir", '"tuong-doi/thu-muc"'),
    ("receive_dir", "5"),
    ("receive_dir", "true"),
    ("receive_dir", json.dumps(_abs_dir("a\x00b"))),
    ("receive_dir", json.dumps(_abs_dir("x" * 32800))),
    ("visible", "1"),
    ("visible", "0"),
    ("visible", '"true"'),
    ("rate_limit", "true"),
    ("rate_limit", "1"),
    ("rate_limit", str(2**60)),
    ("rate_limit", str(2**53)),
    ("rate_limit", "65536.0"),
    ("rate_limit", '"65536"'),
    ("temp_retention_days", "true"),
    ("temp_retention_days", "0"),
    ("temp_retention_days", "366"),
    ("temp_retention_days", "1.5"),
    ("temp_retention_days", str(2**60)),
    ("language", '"vi_VN!"'),
    ("language", '"vi-VN"'),
    ("language", '"VI"'),
    ("language", '"v"'),
    ("language", '"vi_vn"'),
    ("language", '"việt"'),
    ("language", "5"),
    ("autostart", "1"),
    ("autostart", '"yes"'),
    ("first_run_done", "0"),
    ("radar_view", '"grid"'),
    ("radar_view", '"Radar"'),
    ("radar_view", "1"),
    # D6 (spec-d6 mục 2.1, 12 ý 9): `"system"` không phải giá trị lưu được (vắng = theo hệ điều hành).
    ("theme", '"system"'),
    ("theme", '"Dark"'),
    ("theme", '""'),
    ("theme", "1"),
    ("theme", "true"),
    ("accent", '"Ocean"'),
    ("accent", '"1ocean"'),
    ("accent", '""'),
    ("accent", json.dumps("a" * 33)),
    ("accent", "5"),
    ("devices", "[]"),
    ("devices", '"x"'),
    ("devices", "5"),
    ("devices", '{"ABC": {"ask_before_receive": false}}'),
    ("devices", json.dumps({DEV_A: [1]})),
    ("devices", json.dumps({DEV_A: "khong-phai-object"})),
]


@pytest.mark.parametrize(("field", "bad"), BAD_FIELDS, ids=[f"{f}={b[:20]}" for f, b in BAD_FIELDS])
def test_invalid_field_dropped_with_warning_and_bak_on_next_save(cfg, caplog, field, bad):
    """Trường sai: WARNING chỉ nêu tên trường, bỏ trường đó (về mặc định), trường khác giữ;
    lần `save` sau chép nguyên byte gốc sang `.bak`."""
    caplog.set_level(logging.DEBUG)
    raw = write(cfg, f'{{"version": 1, "port": 5000, "{field}": {bad}, "khac": "giu"}}')
    s = Settings.load(cfg)
    assert s.port == 5000
    assert s.extra.get("khac") == "giu"
    assert field not in s.extra
    assert _normalized(s) == _normalized(Settings(port=5000))
    warns = warnings_of(caplog)
    assert warns, "trường sai phải có WARNING"
    assert any(field in r.getMessage() for r in warns), [r.getMessage() for r in warns]
    s.save(cfg)
    assert (cfg / BACKUP_FILE).read_bytes() == raw
    data = on_disk(cfg)
    assert field not in data or (field == "devices" and data[field] == {})
    assert data["khac"] == "giu"


def test_valid_and_invalid_device_entries_are_handled_separately(cfg, caplog):
    """Khóa sai hay giá trị không phải object: bỏ mục đó; mục khác giữ. Trong mục: bool sai → mặc định."""
    caplog.set_level(logging.DEBUG)
    write(
        cfg,
        json.dumps(
            {
                "version": 1,
                "devices": {
                    "ABC": {"ask_before_receive": False},
                    DEV_A: {"ask_before_receive": False},
                    DEV_B: 5,
                    DEV_C: {"ask_before_receive": 0, "clipboard": True},
                },
            }
        ),
    )
    s = Settings.load(cfg)
    assert set(s.devices) <= {DEV_A, DEV_C}
    assert s.device(DEV_A).ask_before_receive is False
    assert s.device(DEV_B).ask_before_receive is True
    c = s.device(DEV_C)
    assert (c.ask_before_receive, c.clipboard) == (True, True)
    assert "ABC" not in s.devices


def test_language_accepts_underscore_form_and_rejects_dash_form(cfg):
    """Mục 11 (sửa 2, M3): `vi_VN` hợp lệ (như `QLocale.name()`), `vi-VN` sai."""
    for ok in ("vi", "en", "fil", "vi_VN", "en_US", "fil_PH"):
        assert Settings(language=ok).language == ok
        write(cfg, json.dumps({"version": 1, "language": ok}))
        assert Settings.load(cfg).language == ok
    for bad in ("vi-VN", "en-US", "vi_VN!", "VI", "vi_vn", "vi_VNM", "abcd", "", "vi\n", "ví"):
        with pytest.raises((ValueError, TypeError)):
            Settings(language=bad)
        write(cfg, json.dumps({"version": 1, "language": bad}))
        assert Settings.load(cfg).language is None


CONSTRUCT_BAD = [
    {"name": ""},
    {"name": " a"},
    {"name": "x" * 65},
    {"name": 5},
    {"receive_dir": "tuong-doi"},
    {"receive_dir": _abs_dir("a\x00b")},
    {"receive_dir": _abs_dir("x" * 32800)},
    {"receive_dir": 5},
    {"visible": 1},
    {"visible": None},
    {"rate_limit": True},
    {"rate_limit": 1},
    {"rate_limit": 2**53},
    {"rate_limit": 65536.0},
    {"temp_retention_days": True},
    {"temp_retention_days": 0},
    {"temp_retention_days": 366},
    {"temp_retention_days": 7.0},
    {"language": "vi-VN"},
    {"language": 5},
    {"autostart": 1},
    {"first_run_done": "x"},
    {"radar_view": "grid"},
    {"devices": [("a", 1)]},
    {"devices": {"ABC": None}},
]


@pytest.mark.parametrize("kwargs", CONSTRUCT_BAD, ids=[repr(k)[:40] for k in CONSTRUCT_BAD])
def test_c1_invalid_construction_raises(kwargs):
    with pytest.raises((ValueError, TypeError)):
        Settings(**kwargs)


def test_c1_invalid_device_value_in_constructor_raises():
    from teleport.app.settings import DeviceSettings

    with pytest.raises((ValueError, TypeError)):
        Settings(devices={"ABC": DeviceSettings()})
    with pytest.raises((ValueError, TypeError)):
        Settings(devices={DEV_A: {"ask_before_receive": False}})


def test_c1_boundaries_are_valid():
    m = _min_rate()
    assert Settings(rate_limit=m).rate_limit == m
    assert Settings(rate_limit=MAX_SAFE).rate_limit == MAX_SAFE
    assert Settings(temp_retention_days=1).temp_retention_days == 1
    assert Settings(temp_retention_days=365).temp_retention_days == 365
    assert Settings(name="x" * 64).name == "x" * 64
    with pytest.raises((ValueError, TypeError)):
        Settings(rate_limit=m - 1)


def test_defaults_not_written(cfg):
    """Chỉ ghi trường khác mặc định; mục thiết bị toàn mặc định, không `extra` thì không ghi."""
    from teleport.app.settings import DeviceSettings

    Settings(devices={DEV_A: DeviceSettings()}).save(cfg)
    assert on_disk(cfg) == {"version": 1}
    Settings(devices={DEV_A: DeviceSettings(ask_before_receive=False), DEV_B: DeviceSettings()}).save(cfg)
    assert on_disk(cfg) == {"version": 1, "devices": {DEV_A: {"ask_before_receive": False}}}
    Settings(visible=True, autostart=False, first_run_done=False).save(cfg)
    assert on_disk(cfg) == {"version": 1}
    Settings(visible=False, autostart=True, first_run_done=True, radar_view="radar").save(cfg)
    assert on_disk(cfg) == {
        "version": 1,
        "visible": False,
        "autostart": True,
        "first_run_done": True,
        "radar_view": "radar",
    }


def test_device_with_device_and_pruned_are_pure():
    from teleport.app.settings import DeviceSettings

    off = DeviceSettings(ask_before_receive=False)
    s = Settings(port=5000)
    s2 = s.with_device(DEV_A, off)
    assert s.device(DEV_A) == DeviceSettings()  # bản gốc không đổi
    assert s2.device(DEV_A).ask_before_receive is False
    assert s2.port == 5000
    s3 = s2.with_device(DEV_A, None)
    assert DEV_A not in s3.devices
    assert s3.device(DEV_A) == DeviceSettings()
    assert s3.with_device(DEV_B, None).devices == s3.devices  # xóa mục không có: không lỗi


def test_pruned_drops_untrusted():
    from teleport.app.settings import DeviceSettings

    off = DeviceSettings(ask_before_receive=False)
    s = Settings(name="X", devices={DEV_A: off, DEV_B: off, DEV_C: off})
    p = s.pruned([DEV_A, DEV_C])
    assert set(p.devices) == {DEV_A, DEV_C}
    assert p.name == "X"
    assert set(s.devices) == {DEV_A, DEV_B, DEV_C}
    assert dict(s.pruned([]).devices) == {}
    assert set(s.pruned(iter([DEV_B])).devices) == {DEV_B}


def test_warning_never_contains_values(cfg, caplog):
    """N9: mọi trường sai mang chuỗi bí mật; log (mọi mức) không bao giờ chứa giá trị."""
    caplog.set_level(logging.DEBUG)
    data = {
        "version": 1,
        "name": SECRET * 10,
        "receive_dir": "tuong-doi-" + SECRET,
        "visible": SECRET,
        "rate_limit": SECRET,
        "temp_retention_days": SECRET,
        "language": SECRET,
        "autostart": SECRET,
        "first_run_done": SECRET,
        "radar_view": SECRET,
        "theme": SECRET,
        "accent": SECRET,
        "devices": {
            SECRET: {"ask_before_receive": False},
            DEV_A: {"ask_before_receive": SECRET, "clipboard": SECRET},
            DEV_B: SECRET,
        },
    }
    write(cfg, json.dumps(data))
    s = Settings.load(cfg)
    s.save(cfg)
    assert warnings_of(caplog)
    for r in caplog.records:
        assert SECRET not in r.getMessage(), r.getMessage()


# --- Ngẫu nhiên: ghi rồi đọc lại ra đúng -------------------------------------------------------------

_NAMES = st.one_of(
    st.none(),
    st.sampled_from(["PC-Nha", "Máy Nhà", "Laptop của Minh", "x" * 64]),
    st.from_regex(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,30}[A-Za-z0-9]", fullmatch=True),
)
_DEVICE_IDS = st.from_regex(r"[0-9a-f]{32}", fullmatch=True)


@st.composite
def _settings(draw):
    from teleport.app.settings import DeviceSettings

    devices = draw(
        st.dictionaries(
            _DEVICE_IDS,
            st.builds(DeviceSettings, ask_before_receive=st.booleans(), clipboard=st.booleans()),
            max_size=4,
        )
    )
    return Settings(
        port=draw(st.none() | st.integers(1, 65535)),
        name=draw(_NAMES),
        receive_dir=draw(st.none() | st.from_regex(r"[A-Za-z0-9_-]{1,20}", fullmatch=True).map(_abs_dir)),
        visible=draw(st.booleans()),
        rate_limit=draw(st.none() | st.integers(_min_rate(), MAX_SAFE)),
        temp_retention_days=draw(st.none() | st.integers(1, 365)),
        language=draw(st.none() | st.from_regex(r"[a-z]{2,3}(_[A-Z]{2})?", fullmatch=True)),
        autostart=draw(st.booleans()),
        first_run_done=draw(st.booleans()),
        radar_view=draw(st.sampled_from([None, "radar", "list"])),
        theme=draw(st.sampled_from([None, "light", "dark"])),
        accent=draw(st.none() | st.from_regex(r"[a-z][a-z0-9_]{0,31}", fullmatch=True)),
        devices=devices,
    )


@pytest.mark.slow
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_settings())
def test_roundtrip_hypothesis(s):
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d)
        s.save(cfg)
        loaded = Settings.load(cfg)
        assert _normalized(loaded) == _normalized(s)
        assert not (cfg / BACKUP_FILE).exists()
        # Ghi lại bản đã đọc thì ra đúng byte như lần đầu (ổn định).
        first = (cfg / SETTINGS_FILE).read_bytes()
        loaded.save(cfg)
        assert (cfg / SETTINGS_FILE).read_bytes() == first


# --- Lỗi đọc tạm thời không được làm mất cài đặt (soát code C1, mục 2) ------------------------------


def test_transient_read_error_then_save_backs_up_original(cfg, monkeypatch):
    """`load` gặp OSError (không phải "chưa có file") → mặc định có `load_failed`; ghi bản đó (qua một
    setter bất kỳ, kể cả `with_device` / `pruned`) phải chép nguyên file cũ sang `.bak` trước khi ghi đè."""
    raw = write(cfg, '{"version": 1, "name": "PC-Nha", "rate_limit": 1048576, "future_key": 1}')
    real = settings_mod._read_limited

    def boom(path):
        raise PermissionError(13, "giả lập phần mềm diệt virus đang khóa file")

    monkeypatch.setattr(settings_mod, "_read_limited", boom)
    loaded = Settings.load(cfg)
    monkeypatch.setattr(settings_mod, "_read_limited", real)
    assert loaded.name is None and loaded.load_failed
    changed = dataclasses.replace(loaded, language="vi").pruned([])
    assert changed.load_failed, "bản dựng từ bản nạp lỗi vẫn phải mang cờ"
    changed.save(cfg)
    assert (cfg / BACKUP_FILE).read_bytes() == raw
    assert on_disk(cfg) == {"version": 1, "language": "vi"}


def test_load_failed_not_written_and_not_compared(cfg):
    s = Settings(load_failed=True)
    assert s == Settings()
    s.save(cfg)
    assert on_disk(cfg) == {"version": 1}
    assert not Settings.load(cfg).load_failed


def test_missing_file_is_not_load_failed(tmp_path):
    assert not Settings.load(tmp_path / "chua-co").load_failed


# =================================================================================================
# D6: chế độ màu `theme` và màu nhấn `accent` (spec-d6 mục 2.1, 5.1; mục 12 ý 9 thắng)
#
# `theme`: None = theo hệ điều hành; hợp lệ ∈ `THEMES = ("light", "dark")`; `"system"` không hợp lệ.
# `accent`: None = mặc định; `app` chỉ kiểm **dạng** `[a-z][a-z0-9_]{0,31}` (ASCII), mã đúng dạng nhưng
# lạ được giữ nguyên. Sai kiểu → TypeError, sai giá trị → ValueError (như `language`).
# =================================================================================================

ACCENT_OK = ["mint", "ocean", "a", "a" + "b" * 31, "teal_2", "x_", "z9"]
ACCENT_BAD_VALUE = [
    "",  # rỗng
    "Ocean",  # chữ hoa
    "1ocean",  # không bắt đầu bằng chữ
    "_ocean",
    "ocean-blue",  # gạch ngang
    "ocean blue",
    "a" * 33,  # vượt giới hạn 1 ký tự
    "a" + "b" * 32,
    "đại_dương",  # không phải ASCII
    "ｏcean",  # chữ “o” toàn khổ: `\w`/`str.isalpha` nhận nhầm
    "ocean\n",  # `$` của `re.match` nhận dòng mới cuối chuỗi
    "ocean\x00",
    "../ocean",
]


def test_theme_and_accent_constants_match_spec():
    assert settings_mod.THEMES == ("light", "dark")
    pattern = settings_mod.ACCENT_RE
    for ok in ACCENT_OK:
        assert re.fullmatch(pattern, ok), ok
    for bad in ACCENT_BAD_VALUE:
        assert not re.fullmatch(pattern, bad), repr(bad)


def test_theme_default_none_not_written(cfg):
    s = Settings()
    assert s.theme is None
    s.save(cfg)
    assert on_disk(cfg) == {"version": 1}
    Settings(theme=None, accent=None).save(cfg)
    assert "theme" not in on_disk(cfg)


@pytest.mark.parametrize("value", ["light", "dark"])
def test_theme_light_dark_roundtrip(cfg, caplog, value):
    caplog.set_level(logging.DEBUG)
    Settings(theme=value).save(cfg)
    assert on_disk(cfg) == {"version": 1, "theme": value}
    loaded = Settings.load(cfg)
    assert loaded.theme == value
    assert warnings_of(caplog) == []
    # Đọc rồi ghi lại ra đúng byte (không đổi gì khi không có gì đổi).
    first = (cfg / SETTINGS_FILE).read_bytes()
    loaded.save(cfg)
    assert (cfg / SETTINGS_FILE).read_bytes() == first
    assert not (cfg / BACKUP_FILE).exists()


@pytest.mark.parametrize(
    "bad", ['"system"', '"Light"', '"DARK"', '""', '" dark"', "1", "true", "[]", '{"a": 1}']
)
def test_theme_system_string_or_other_value_dropped_with_warning(cfg, caplog, bad):
    """`"system"` hay giá trị lạ trong file: bỏ trường (về None = theo HĐH) + WARNING nêu tên trường,
    không nêu giá trị; trường khác giữ; lần ghi sau sinh `.bak` (spec-d6 mục 12 ý 9)."""
    caplog.set_level(logging.DEBUG)
    raw = write(cfg, f'{{"version": 1, "language": "vi", "accent": "ocean", "theme": {bad}}}')
    s = Settings.load(cfg)
    assert s.theme is None
    assert "theme" not in s.extra
    assert (s.language, s.accent) == ("vi", "ocean")
    warns = warnings_of(caplog)
    assert any("theme" in r.getMessage() for r in warns), [r.getMessage() for r in warns]
    s.save(cfg)
    assert (cfg / BACKUP_FILE).read_bytes() == raw
    assert on_disk(cfg) == {"version": 1, "language": "vi", "accent": "ocean"}


def test_theme_null_in_file_is_none_without_warning(cfg, caplog):
    caplog.set_level(logging.DEBUG)
    write(cfg, '{"version": 1, "theme": null, "accent": null}')
    s = Settings.load(cfg)
    assert (s.theme, s.accent) == (None, None)
    assert warnings_of(caplog) == []


def test_theme_constructor_rejects_bad_type_and_value():
    for bad_type in (1, True, b"dark", ["dark"], 0.0):
        with pytest.raises(TypeError):
            Settings(theme=bad_type)
    for bad_value in ("system", "System", "Light", "DARK", "", " dark", "dark ", "auto"):
        with pytest.raises(ValueError):
            Settings(theme=bad_value)
    assert Settings(theme="light").theme == "light"
    assert Settings(theme="dark").theme == "dark"


def test_accent_default_none_not_written(cfg):
    s = Settings()
    assert s.accent is None
    s.save(cfg)
    assert "accent" not in on_disk(cfg)


@pytest.mark.parametrize("accent", ["ocean", "tuong_lai_9", "a" + "b" * 31])
def test_accent_unknown_well_formed_id_kept_on_save(cfg, caplog, accent):
    """Mã đúng dạng (kể cả mã bản này không biết, ví dụ của bản sau) giữ nguyên khi đọc và ghi lại;
    không WARNING, không `.bak`."""
    caplog.set_level(logging.DEBUG)
    write(cfg, json.dumps({"version": 1, "accent": accent}))
    s = Settings.load(cfg)
    assert s.accent == accent
    assert "accent" not in s.extra
    assert warnings_of(caplog) == []
    dataclasses.replace(s, theme="dark").save(cfg)
    assert on_disk(cfg) == {"version": 1, "theme": "dark", "accent": accent}
    assert not (cfg / BACKUP_FILE).exists()
    assert Settings(accent=accent).accent == accent


@pytest.mark.parametrize("bad", ACCENT_BAD_VALUE, ids=[repr(b)[:20] for b in ACCENT_BAD_VALUE])
def test_accent_rejects_bad_form(cfg, caplog, bad):
    with pytest.raises(ValueError):
        Settings(accent=bad)
    caplog.set_level(logging.DEBUG)
    raw = write(cfg, json.dumps({"version": 1, "theme": "light", "accent": bad}))
    s = Settings.load(cfg)
    assert s.accent is None and s.theme == "light"
    assert any("accent" in r.getMessage() for r in warnings_of(caplog))
    s.save(cfg)
    assert (cfg / BACKUP_FILE).read_bytes() == raw
    assert on_disk(cfg) == {"version": 1, "theme": "light"}


@pytest.mark.parametrize("bad", [5, True, b"ocean", ["ocean"], 1.5])
def test_accent_rejects_bad_type(bad):
    with pytest.raises(TypeError):
        Settings(accent=bad)


def test_old_file_without_theme_accent_defaults(cfg, caplog):
    """File của bản trước D6 (có trường C1, không `theme`/`accent`): đọc ra None, không WARNING, ghi lại
    không thêm khóa nào."""
    caplog.set_level(logging.DEBUG)
    original = {"version": 1, "port": 5000, "language": "vi", "radar_view": "list", "first_run_done": True}
    write(cfg, json.dumps(original))
    s = Settings.load(cfg)
    assert (s.theme, s.accent) == (None, None)
    assert warnings_of(caplog) == []
    s.save(cfg)
    assert on_disk(cfg) == original
    assert not (cfg / BACKUP_FILE).exists()


def test_theme_and_accent_not_left_in_extra(cfg):
    write(cfg, '{"version": 1, "theme": "dark", "accent": "rose"}')
    s = Settings.load(cfg)
    assert (s.theme, s.accent) == ("dark", "rose")
    assert set(s.extra) <= {"version"}
