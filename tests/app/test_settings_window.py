"""Nhớ trạng thái cửa sổ: ba trường mới của `Settings` (`specs/spec-f1.md` D1–D3, R3, R7).

Viết từ spec, không từ code. Nguồn: D1 (`WindowGeometry` và luật từng khóa), D2 (`window`,
`last_conversation`, `tray_hint_shown`: mặc định, luật, khi nào ghi ra file), D3 (`pruned` bỏ cả
`last_conversation` của máy không tin cậy), R3 (`last_conversation` sai → ValueError), R7 (danh sách ca
hỏng phải phủ). Tiêu chí xong 1: mọi dạng hỏng không làm app lỗi, chỉ mất đúng trường đó.

Giả định API (theo spec): `WindowGeometry`, `MAX_WINDOW_COORD`, `MAX_WINDOW_SIZE`, `MAX_SCREEN_NAME_LEN`,
`MIN_DPR`, `MAX_DPR` nằm ở `teleport.app.settings`; khóa JSON trùng tên trường.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.app.settings import BACKUP_FILE, SETTINGS_FILE, Settings

DEV_A = "a" * 32
DEV_B = "0123456789abcdef" * 2
SECRET = "BI-MAT-MAN-HINH-9Z"  # tên màn hình bí mật: không được lọt vào log (mvp N9)
WINDOW_WARNING = "trường window không hợp lệ, bỏ qua"


@pytest.fixture
def cfg(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    return d


def wg(**kw):
    from teleport.app.settings import WindowGeometry

    values = {"x": 100, "y": 80, "width": 1024, "height": 700}
    values.update(kw)
    return WindowGeometry(**values)


def write_raw(cfg: Path, text: str) -> None:
    (cfg / SETTINGS_FILE).write_text(text, encoding="utf-8")


def on_disk(cfg: Path) -> dict:
    return json.loads((cfg / SETTINGS_FILE).read_text(encoding="utf-8"))


def warnings_of(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


GOOD_WINDOW = '{"x": 10, "y": 20, "width": 800, "height": 600}'


# =================================================================================================
# D1: hằng số và `WindowGeometry`
# =================================================================================================


def test_window_constants_match_spec():
    from teleport.app import settings as mod

    assert mod.MAX_WINDOW_COORD == 100_000
    assert mod.MAX_WINDOW_SIZE == 100_000
    assert mod.MAX_SCREEN_NAME_LEN == 256
    assert mod.MIN_DPR == 0.25
    assert mod.MAX_DPR == 16


def test_window_geometry_defaults_and_frozen():
    g = wg()
    assert (g.x, g.y, g.width, g.height) == (100, 80, 1024, 700)
    assert g.maximized is False
    assert g.screen is None
    assert g.dpr is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        g.x = 5  # type: ignore[misc]
    assert wg() == wg()
    assert wg(screen="DP-1") != wg(screen="DP-2")


def test_window_geometry_int_dpr_stored_as_float():
    g = wg(dpr=2)
    assert g.dpr == 2.0
    assert type(g.dpr) is float


@pytest.mark.parametrize(
    "kw",
    [
        {"x": -100_000, "y": 100_000},
        {"x": 100_000, "y": -100_000},
        {"width": 1, "height": 1},
        {"width": 100_000, "height": 100_000},
        {"screen": "x"},
        {"screen": "s" * 256},
        {"screen": "Màn hình \\\\.\\DISPLAY1"},
        {"dpr": 0.25},
        {"dpr": 16},
        {"dpr": 16.0},
        {"dpr": 1.5},
        {"maximized": True},
        {"screen": None, "dpr": None},
    ],
    ids=lambda kw: repr(kw)[:40],
)
def test_window_geometry_boundaries_are_valid(kw):
    g = wg(**kw)
    for key, value in kw.items():
        assert getattr(g, key) == value


@pytest.mark.parametrize(
    "kw",
    [
        {"x": 100_001},
        {"x": -100_001},
        {"y": 100_001},
        {"y": -100_001},
        {"width": 0},
        {"width": -5},
        {"width": 100_001},
        {"height": 0},
        {"height": 100_001},
        {"x": 1.0},
        {"x": True},
        {"y": False},
        {"width": 800.0},
        {"height": True},
        {"x": "10"},
        {"x": None},
        {"maximized": 1},
        {"maximized": None},
        {"maximized": "true"},
        {"screen": ""},
        {"screen": "DP\x001"},
        {"screen": "s" * 257},
        {"screen": 5},
        {"screen": b"DP-1"},
        {"dpr": math.nan},
        {"dpr": math.inf},
        {"dpr": -math.inf},
        {"dpr": True},
        {"dpr": False},
        {"dpr": 0},
        {"dpr": 0.24},
        {"dpr": 16.01},
        {"dpr": -1.0},
        {"dpr": "1.0"},
    ],
    ids=lambda kw: repr(kw)[:40],
)
def test_window_geometry_rejects_invalid(kw):
    with pytest.raises((TypeError, ValueError)):
        wg(**kw)


# =================================================================================================
# D2: constructor của `Settings`
# =================================================================================================


def test_new_fields_default_values():
    s = Settings()
    assert s.window is None
    assert s.last_conversation is None
    assert s.tray_hint_shown is False


@pytest.mark.parametrize(
    "window",
    [{"x": 1, "y": 2, "width": 3, "height": 4}, (1, 2, 3, 4), "x", 5],
    ids=["dict", "tuple", "str", "int"],
)
def test_settings_window_must_be_window_geometry(window):
    with pytest.raises(TypeError):
        Settings(window=window)


@pytest.mark.parametrize(
    "value",
    ["A" * 32, "a" * 31, "a" * 33, "g" * 32, "", "a" * 31 + " "],
    ids=["hoa", "31", "33", "khong-hex", "rong", "khoang-trang"],
)
def test_settings_last_conversation_invalid_is_value_error(value):
    """R3: kiểm bằng `validate_device_id` (MessageError là ValueError)."""
    with pytest.raises(ValueError):
        Settings(last_conversation=value)


@pytest.mark.parametrize("value", [5, b"a" * 32])
def test_settings_last_conversation_wrong_type_raises(value):
    with pytest.raises((TypeError, ValueError)):
        Settings(last_conversation=value)


@pytest.mark.parametrize("value", [None, 1, 0, "true"])
def test_settings_tray_hint_shown_must_be_bool(value):
    with pytest.raises(TypeError):
        Settings(tray_hint_shown=value)


def test_settings_accepts_valid_new_fields():
    s = Settings(
        window=wg(maximized=True, screen="DP-1", dpr=1.25), last_conversation=DEV_A, tray_hint_shown=True
    )
    assert s.window == wg(maximized=True, screen="DP-1", dpr=1.25)
    assert s.last_conversation == DEV_A
    assert s.tray_hint_shown is True


def test_new_fields_not_in_extra_after_load(cfg):
    write_raw(
        cfg,
        json.dumps(
            {
                "version": 1,
                "window": {"x": 1, "y": 2, "width": 300, "height": 400},
                "last_conversation": DEV_A,
                "tray_hint_shown": True,
            }
        ),
    )
    s = Settings.load(cfg)
    assert {"window", "last_conversation", "tray_hint_shown"}.isdisjoint(s.extra)


@pytest.mark.parametrize("key", ["window", "last_conversation", "tray_hint_shown"])
def test_extra_cannot_hold_new_field_keys(key):
    with pytest.raises(ValueError):
        Settings(extra={key: 1})


# =================================================================================================
# D2: ghi ra file
# =================================================================================================


def test_defaults_write_none_of_the_new_keys(cfg):
    Settings().save(cfg)
    data = on_disk(cfg)
    assert "window" not in data
    assert "last_conversation" not in data
    assert "tray_hint_shown" not in data


def test_tray_hint_shown_false_not_written_true_written(cfg):
    Settings(tray_hint_shown=False).save(cfg)
    assert "tray_hint_shown" not in on_disk(cfg)
    Settings(tray_hint_shown=True).save(cfg)
    assert on_disk(cfg)["tray_hint_shown"] is True


def test_last_conversation_written_when_set(cfg):
    Settings(last_conversation=DEV_B).save(cfg)
    assert on_disk(cfg)["last_conversation"] == DEV_B


def test_window_written_with_five_keys_when_screen_and_dpr_none(cfg):
    Settings(window=wg(x=-50, y=0, width=900, height=640)).save(cfg)
    assert on_disk(cfg)["window"] == {"x": -50, "y": 0, "width": 900, "height": 640, "maximized": False}


def test_window_written_with_screen_and_dpr_when_set(cfg):
    Settings(window=wg(maximized=True, screen="\\\\.\\DISPLAY2", dpr=1.5)).save(cfg)
    assert on_disk(cfg)["window"] == {
        "x": 100,
        "y": 80,
        "width": 1024,
        "height": 700,
        "maximized": True,
        "screen": "\\\\.\\DISPLAY2",
        "dpr": 1.5,
    }


def test_window_with_only_screen_writes_screen_not_dpr(cfg):
    Settings(window=wg(screen="DP-1")).save(cfg)
    w = on_disk(cfg)["window"]
    assert w["screen"] == "DP-1"
    assert "dpr" not in w


def test_new_fields_roundtrip(cfg, caplog):
    caplog.set_level(logging.DEBUG)
    s = Settings(
        name="May-A",
        window=wg(x=-1920, y=-10, width=1280, height=720, maximized=True, screen="Màn hình 2", dpr=2.0),
        last_conversation=DEV_A,
        tray_hint_shown=True,
    )
    s.save(cfg)
    loaded = Settings.load(cfg)
    assert loaded == s
    assert loaded.window == s.window
    assert type(loaded.window.dpr) is float
    assert warnings_of(caplog) == []
    assert not (cfg / BACKUP_FILE).exists()


def test_integer_dpr_in_file_loads_as_float(cfg, caplog):
    caplog.set_level(logging.DEBUG)
    write_raw(cfg, '{"version": 1, "window": {"x": 0, "y": 0, "width": 10, "height": 10, "dpr": 2}}')
    w = Settings.load(cfg).window
    assert w is not None and w.dpr == 2.0 and type(w.dpr) is float
    assert warnings_of(caplog) == []


def test_window_without_optional_keys_loads_with_defaults(cfg, caplog):
    caplog.set_level(logging.DEBUG)
    write_raw(cfg, '{"version": 1, "window": ' + GOOD_WINDOW + "}")
    w = Settings.load(cfg).window
    assert w == wg(x=10, y=20, width=800, height=600)
    assert (w.maximized, w.screen, w.dpr) == (False, None, None)
    assert warnings_of(caplog) == []


def test_window_null_screen_and_dpr_are_defaults_without_warning(cfg, caplog):
    caplog.set_level(logging.DEBUG)
    write_raw(
        cfg,
        '{"window": {"x": 10, "y": 20, "width": 800, "height": 600, "maximized": true, '
        '"screen": null, "dpr": null}}',
    )
    w = Settings.load(cfg).window
    assert w == wg(x=10, y=20, width=800, height=600, maximized=True)
    assert warnings_of(caplog) == []


# =================================================================================================
# D2, R7: đọc `window` hỏng → mất cả `window`, đúng một WARNING, `.bak` khi ghi, trường khác còn
# =================================================================================================

# Giá trị `window` (chuỗi JSON thô, để viết được NaN/Infinity) không hợp lệ.
BAD_WINDOWS = {
    "x-float": '{"x": 1.0, "y": 0, "width": 10, "height": 10}',
    "x-true": '{"x": true, "y": 0, "width": 10, "height": 10}',
    "x-1e6": '{"x": 1000000, "y": 0, "width": 10, "height": 10}',
    "x-NaN": '{"x": NaN, "y": 0, "width": 10, "height": 10}',
    "x-chuoi": '{"x": "1", "y": 0, "width": 10, "height": 10}',
    "x-null": '{"x": null, "y": 0, "width": 10, "height": 10}',
    "y-am-qua": '{"x": 0, "y": -100001, "width": 10, "height": 10}',
    "width-0": '{"x": 0, "y": 0, "width": 0, "height": 10}',
    "width-qua": '{"x": 0, "y": 0, "width": 100001, "height": 10}',
    "height-am": '{"x": 0, "y": 0, "width": 10, "height": -1}',
    "height-Infinity": '{"x": 0, "y": 0, "width": 10, "height": Infinity}',
    "thieu-x": '{"y": 0, "width": 10, "height": 10}',
    "thieu-y": '{"x": 0, "width": 10, "height": 10}',
    "thieu-width": '{"x": 0, "y": 0, "height": 10}',
    "thieu-height": '{"x": 0, "y": 0, "width": 10}',
    "maximized-1": '{"x": 0, "y": 0, "width": 10, "height": 10, "maximized": 1}',
    "maximized-chuoi": '{"x": 0, "y": 0, "width": 10, "height": 10, "maximized": "yes"}',
    "maximized-null": '{"x": 0, "y": 0, "width": 10, "height": 10, "maximized": null}',
    "dpr-NaN": '{"x": 0, "y": 0, "width": 10, "height": 10, "dpr": NaN}',
    "dpr-Infinity": '{"x": 0, "y": 0, "width": 10, "height": 10, "dpr": Infinity}',
    "dpr-true": '{"x": 0, "y": 0, "width": 10, "height": 10, "dpr": true}',
    "dpr-0": '{"x": 0, "y": 0, "width": 10, "height": 10, "dpr": 0}',
    "dpr-qua": '{"x": 0, "y": 0, "width": 10, "height": 10, "dpr": 16.5}',
    "dpr-chuoi": '{"x": 0, "y": 0, "width": 10, "height": 10, "dpr": "2"}',
    "screen-rong": '{"x": 0, "y": 0, "width": 10, "height": 10, "screen": ""}',
    "screen-NUL": '{"x": 0, "y": 0, "width": 10, "height": 10, "screen": "DP\\u00001"}',
    "screen-257": '{"x": 0, "y": 0, "width": 10, "height": 10, "screen": "' + "s" * 257 + '"}',
    "screen-so": '{"x": 0, "y": 0, "width": 10, "height": 10, "screen": 5}',
    "mang": "[]",
    "mang-4-so": "[0, 0, 10, 10]",
    "chuoi": '"x"',
    "so": "5",
    "true": "true",
    "object-rong": "{}",
    # Số nguyên khổng lồ: đổi sang float là OverflowError, không được lọt ra khỏi `Settings.load`.
    "dpr-khong-lo": '{"x": 0, "y": 0, "width": 10, "height": 10, "dpr": 1' + "0" * 400 + "}",
    "x-khong-lo": '{"x": 1' + "0" * 400 + ', "y": 0, "width": 10, "height": 10}',
}


@pytest.mark.parametrize("raw", list(BAD_WINDOWS.values()), ids=list(BAD_WINDOWS))
def test_bad_window_dropped_with_one_warning_and_other_fields_kept(cfg, caplog, raw):
    caplog.set_level(logging.DEBUG)
    write_raw(
        cfg,
        '{"version": 1, "name": "May-A", "theme": "dark", "last_conversation": "' + DEV_A + '", '
        '"tray_hint_shown": true, "window": ' + raw + "}",
    )
    s = Settings.load(cfg)
    assert s.window is None
    # Chỉ mất đúng trường `window`.
    assert (s.name, s.theme, s.last_conversation, s.tray_hint_shown) == ("May-A", "dark", DEV_A, True)
    warns = warnings_of(caplog)
    assert len(warns) == 1, [r.getMessage() for r in warns]
    assert WINDOW_WARNING in warns[0].getMessage()
    assert "window" not in s.extra


@pytest.mark.parametrize("raw", list(BAD_WINDOWS.values()), ids=list(BAD_WINDOWS))
def test_bad_window_is_corruption_backed_up_on_next_save(cfg, raw):
    text = '{"version": 1, "window": ' + raw + "}"
    write_raw(cfg, text)
    s = Settings.load(cfg)
    s.save(cfg)
    assert (cfg / BACKUP_FILE).read_text(encoding="utf-8") == text
    assert "window" not in on_disk(cfg)


def test_bad_window_warning_never_contains_values(cfg, caplog):
    """N9: tên màn hình (dù sai luật) không lọt vào log."""
    caplog.set_level(logging.DEBUG)
    bad_screen = SECRET + "\\u0000" + SECRET
    write_raw(
        cfg,
        '{"window": {"x": 0, "y": 0, "width": 10, "height": 10, "screen": "' + bad_screen + '"}, '
        '"last_conversation": "' + SECRET + '", "tray_hint_shown": "' + SECRET + '"}',
    )
    s = Settings.load(cfg)
    s.save(cfg)
    assert warnings_of(caplog)
    for r in caplog.records:
        assert SECRET not in r.getMessage(), r.getMessage()


def test_window_null_is_default_without_warning_or_backup(cfg, caplog):
    caplog.set_level(logging.DEBUG)
    write_raw(cfg, '{"version": 1, "name": "May-A", "window": null}')
    s = Settings.load(cfg)
    assert s.window is None and s.name == "May-A"
    assert warnings_of(caplog) == []
    s.save(cfg)
    assert not (cfg / BACKUP_FILE).exists()
    assert "window" not in on_disk(cfg)


def test_unknown_key_inside_window_ignored_without_warning_and_not_kept(cfg, caplog):
    """D2 + mặc định 3: khóa lạ bên trong `window` bị bỏ qua (không giữ, không cảnh báo)."""
    caplog.set_level(logging.DEBUG)
    write_raw(
        cfg,
        '{"version": 1, "window": {"x": 10, "y": 20, "width": 800, "height": 600, '
        '"tuong_lai": {"a": 1}, "z_order": 3}}',
    )
    s = Settings.load(cfg)
    assert s.window == wg(x=10, y=20, width=800, height=600)
    assert warnings_of(caplog) == []
    assert "tuong_lai" not in s.extra and "z_order" not in s.extra
    s.save(cfg)
    assert on_disk(cfg)["window"] == {"x": 10, "y": 20, "width": 800, "height": 600, "maximized": False}
    assert not (cfg / BACKUP_FILE).exists()


def test_unknown_top_level_keys_still_kept_next_to_window(cfg):
    write_raw(cfg, '{"version": 1, "tuong_lai": [1, 2], "window": ' + GOOD_WINDOW + "}")
    s = Settings.load(cfg)
    s.save(cfg)
    data = on_disk(cfg)
    assert data["tuong_lai"] == [1, 2]
    assert data["window"]["width"] == 800


# =================================================================================================
# D2, R7: `last_conversation`, `tray_hint_shown` hỏng
# =================================================================================================

BAD_SIMPLE = [
    ("last_conversation", '"' + "A" * 32 + '"'),  # viết hoa
    ("last_conversation", '"' + "a" * 31 + '"'),  # 31 ký tự
    ("last_conversation", '"' + "a" * 33 + '"'),
    ("last_conversation", '""'),
    ("last_conversation", "5"),
    ("last_conversation", "true"),
    ("last_conversation", "[]"),
    ("last_conversation", "{}"),
    ("tray_hint_shown", "null"),  # trường bool: null là sai
    ("tray_hint_shown", "1"),
    ("tray_hint_shown", "0"),
    ("tray_hint_shown", '"true"'),
    ("tray_hint_shown", "[]"),
]


@pytest.mark.parametrize(("field", "raw"), BAD_SIMPLE, ids=[f"{f}={r[:12]}" for f, r in BAD_SIMPLE])
def test_bad_simple_field_dropped_with_warning_and_backed_up(cfg, caplog, field, raw):
    caplog.set_level(logging.DEBUG)
    text = '{"version": 1, "name": "May-A", "window": ' + GOOD_WINDOW + ', "' + field + '": ' + raw + "}"
    write_raw(cfg, text)
    s = Settings.load(cfg)
    assert getattr(s, field) == getattr(Settings(), field)
    assert s.name == "May-A"
    assert s.window == wg(x=10, y=20, width=800, height=600)
    warns = warnings_of(caplog)
    assert len(warns) == 1
    assert field in warns[0].getMessage()
    assert field not in s.extra
    s.save(cfg)
    assert (cfg / BACKUP_FILE).read_text(encoding="utf-8") == text


def test_last_conversation_null_is_default_without_warning(cfg, caplog):
    caplog.set_level(logging.DEBUG)
    write_raw(cfg, '{"version": 1, "last_conversation": null, "tray_hint_shown": false}')
    s = Settings.load(cfg)
    assert s.last_conversation is None and s.tray_hint_shown is False
    assert warnings_of(caplog) == []
    s.save(cfg)
    assert not (cfg / BACKUP_FILE).exists()


def test_old_file_without_new_keys_loads_defaults_without_warning(cfg, caplog):
    """Tương thích: file của bản trước (không có ba khóa mới)."""
    caplog.set_level(logging.DEBUG)
    write_raw(cfg, '{"version": 1, "port": 47474, "theme": "light"}')
    s = Settings.load(cfg)
    assert (s.window, s.last_conversation, s.tray_hint_shown) == (None, None, False)
    assert warnings_of(caplog) == []


# =================================================================================================
# D3: `pruned`
# =================================================================================================


def test_pruned_drops_last_conversation_of_untrusted():
    s = Settings(name="X", last_conversation=DEV_A, tray_hint_shown=True, window=wg())
    p = s.pruned([DEV_B])
    assert p.last_conversation is None
    assert (p.name, p.tray_hint_shown, p.window) == ("X", True, wg())
    assert s.last_conversation == DEV_A, "hàm thuần: bản gốc không đổi"


def test_pruned_drops_last_conversation_when_nobody_trusted():
    assert Settings(last_conversation=DEV_A).pruned([]).last_conversation is None
    assert Settings(last_conversation=DEV_A).pruned(iter([])).last_conversation is None


def test_pruned_keeps_trusted_last_conversation_and_returns_self():
    s = Settings(last_conversation=DEV_A, window=wg())
    assert s.pruned([DEV_A, DEV_B]) is s
    assert s.pruned(iter([DEV_A])) is s


def test_pruned_without_last_conversation_returns_self():
    s = Settings(window=wg(), tray_hint_shown=True)
    assert s.pruned([]) is s


def test_pruned_drops_device_entry_and_last_conversation_together():
    from teleport.app.settings import DeviceSettings

    s = Settings(devices={DEV_A: DeviceSettings(ask_before_receive=False)}, last_conversation=DEV_A)
    p = s.pruned([DEV_B])
    assert dict(p.devices) == {}
    assert p.last_conversation is None


def test_pruned_untrusted_device_entry_but_trusted_last_conversation():
    from teleport.app.settings import DeviceSettings

    s = Settings(devices={DEV_B: DeviceSettings(ask_before_receive=False)}, last_conversation=DEV_A)
    p = s.pruned([DEV_A])
    assert dict(p.devices) == {}
    assert p.last_conversation == DEV_A


# =================================================================================================
# Ngẫu nhiên: mọi `WindowGeometry` hợp lệ ghi rồi đọc lại ra đúng, không cảnh báo
# =================================================================================================

_COORD = st.integers(-100_000, 100_000)
_SIZE = st.integers(1, 100_000)
_SCREEN = st.none() | st.text(
    st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)), min_size=1, max_size=256
)
_DPR = st.none() | st.floats(0.25, 16.0, allow_nan=False, allow_infinity=False) | st.integers(1, 16)


@st.composite
def _windows(draw):
    from teleport.app.settings import WindowGeometry

    return WindowGeometry(
        x=draw(_COORD),
        y=draw(_COORD),
        width=draw(_SIZE),
        height=draw(_SIZE),
        maximized=draw(st.booleans()),
        screen=draw(_SCREEN),
        dpr=draw(_DPR),
    )


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_windows(), st.none() | st.from_regex(r"[0-9a-f]{32}", fullmatch=True), st.booleans())
def test_window_roundtrip_hypothesis(window, last, hint):
    s = Settings(window=window, last_conversation=last, tray_hint_shown=hint)
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d)
        s.save(cfg)
        loaded = Settings.load(cfg)
        assert loaded == s
        assert not (cfg / BACKUP_FILE).exists()
        first = (cfg / SETTINGS_FILE).read_bytes()
        loaded.save(cfg)
        assert (cfg / SETTINGS_FILE).read_bytes() == first
