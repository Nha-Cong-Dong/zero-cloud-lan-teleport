"""Chủ đề đã lưu lúc khởi động (spec D6 mục 2.6, test 5.5; mục 13 ý 7 thắng khi khác).

Viết từ spec, không từ code.

- `ui/app.py::_saved_settings(config_dir) -> Settings` (spec-f1 R1, thay `_saved_ui_preferences`):
  `Settings.load` một lần, chỉ đọc; mọi `Exception` → `Settings()`. `_run` lấy language/theme/accent từ đó.
- `_run`: `i18n.install(app, language)` → `fonts.install()` → `theme.set_preference(app,
  theme.mode_from_setting(t), a)` → `SingleInstance.acquire` → cửa sổ. Tức chủ đề đã lưu được áp
  **trước** khi tạo cửa sổ đầu tiên (không nháy màu).
- Lựa chọn trong mục Giao diện được lưu và lần chạy sau áp lại.
- `--selftest` dựng `AppearanceSection` và chạy `theme.set_preference` một vòng, vẫn trả 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractButton, QApplication, QRadioButton

integration = pytest.mark.integration


# =================================================================================================
# _saved_settings
# =================================================================================================


@pytest.mark.parametrize(
    ("content", "want"),
    [
        ({"language": "en", "theme": "dark", "accent": "ocean"}, ("en", "dark", "ocean")),
        ({"theme": "light"}, (None, "light", None)),
        ({"accent": "khong_co"}, (None, None, "khong_co")),  # mã lạ đúng dạng: giữ, ui hiện mặc định
        ({"language": "vi", "theme": "system"}, ("vi", None, None)),  # "system" không hợp lệ: bỏ trường
        ({"theme": 5, "accent": "Sai Dang"}, (None, None, None)),
        ({}, (None, None, None)),
        (None, (None, None, None)),  # không có settings.json
        ("{hỏng", (None, None, None)),
        ("[]", (None, None, None)),
    ],
    ids=["du-ba", "chi-theme", "accent-la", "theme-system", "sai-kieu", "rong", "khong-file", "hong", "mang"],
)
def test_saved_settings_reads_three_fields_read_only(tmp_path, content, want):
    from teleport.app.settings import Settings
    from teleport.ui import app as ui_app

    d = tmp_path / "cfg"
    d.mkdir()
    path = d / "settings.json"
    if isinstance(content, dict):
        path.write_text(json.dumps(content), encoding="utf-8")
    elif isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    before = sorted(p.name for p in d.iterdir())
    raw = path.read_bytes() if path.exists() else None
    saved = ui_app._saved_settings(d)
    assert isinstance(saved, Settings)
    assert (saved.language, saved.theme, saved.accent) == want
    assert sorted(p.name for p in d.iterdir()) == before, "chỉ đọc: không tạo file nào"
    assert (path.read_bytes() if path.exists() else None) == raw, "chỉ đọc: không sửa settings.json"


def test_saved_settings_config_dir_is_file(tmp_path):
    from teleport.app.settings import Settings
    from teleport.ui import app as ui_app

    f = tmp_path / "la-file"
    f.write_text("x", encoding="utf-8")
    assert ui_app._saved_settings(f) == Settings()


@pytest.mark.parametrize("error", [RuntimeError("x"), ValueError("x"), OSError(5, "x"), RecursionError()])
def test_saved_settings_any_exception_defaults(tmp_path, monkeypatch, error):
    from teleport.app.settings import Settings
    from teleport.ui import app as ui_app

    def boom(cls, config_dir):
        raise error

    monkeypatch.setattr(Settings, "load", classmethod(boom))
    assert ui_app._saved_settings(tmp_path) == Settings()


def test_saved_settings_loads_settings_once(tmp_path, monkeypatch):
    from teleport.app.settings import Settings
    from teleport.ui import app as ui_app

    calls: list = []
    real = Settings.load.__func__

    def counting(cls, config_dir):
        calls.append(config_dir)
        return real(cls, config_dir)

    monkeypatch.setattr(Settings, "load", classmethod(counting))
    (tmp_path / "settings.json").write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    assert ui_app._saved_settings(tmp_path).theme == "dark"
    assert len(calls) == 1


# =================================================================================================
# Khởi động thật trong tiến trình (fixture `gui`)
# =================================================================================================


def _quit(run, ui_helpers) -> None:
    [action] = [
        a for a in run.tray.actions() if not a.isSeparator() and ui_helpers.plain(a.text()) == "Thoát"
    ]
    action.trigger()


@pytest.fixture
def startup_probe(monkeypatch, qapp):
    """Ghi thứ tự i18n.install / theme.set_preference / SingleInstance.acquire / MainWindow và trạng thái
    chủ đề lúc cửa sổ đầu tiên được dựng. Trước mỗi lần chạy: ép lại sáng + lựa chọn mặc định."""
    from teleport.ui import app as ui_app
    from teleport.ui import i18n, main_window, theme

    probe: dict = {"order": [], "at_window": []}
    real_install = i18n.install
    real_set = theme.set_preference
    real_acquire = ui_app.SingleInstance.acquire.__func__
    real_win_init = main_window.MainWindow.__init__

    def install(app, language=None):
        probe["order"].append("i18n")
        return real_install(app, language)

    def set_preference(app, mode, accent):
        probe["order"].append("theme")
        return real_set(app, mode, accent)

    def acquire(cls, *a, **k):
        probe["order"].append("acquire")
        return real_acquire(cls, *a, **k)

    def win_init(self, *a, **k):
        probe["order"].append("window")
        pal = QApplication.palette()
        probe["at_window"].append(
            {
                "dark": theme.is_dark_palette(pal),
                "mode": theme.current_mode(),
                "accent": theme.current_accent(),
                "highlight": pal.color(pal.ColorRole.Highlight).name(),
            }
        )
        real_win_init(self, *a, **k)

    monkeypatch.setattr(i18n, "install", install)
    monkeypatch.setattr(theme, "set_preference", set_preference)
    monkeypatch.setattr(ui_app.SingleInstance, "acquire", classmethod(acquire))
    monkeypatch.setattr(main_window.MainWindow, "__init__", win_init)

    def reset() -> None:
        theme._reset_for_tests()
        qapp.styleHints().unsetColorScheme()
        theme.apply(qapp, Qt.ColorScheme.Light)
        probe["order"].clear()
        probe["at_window"].clear()
        assert not theme.is_dark_palette(QApplication.palette()), "trước khi chạy: sáng"

    probe["reset"] = reset
    return probe


@integration
def test_saved_dark_theme_applied_before_first_window(gui, gui_wait, config_dir, ui_helpers, startup_probe):
    from teleport.ui import theme

    d = config_dir("A")
    (d / "settings.json").write_text(json.dumps({"theme": "dark", "accent": "ocean"}), encoding="utf-8")
    startup_probe["reset"]()

    def script(run):
        yield gui_wait(lambda: run.windows and run.hosts and run.hosts[-1].running, 15, "node chạy")
        _quit(run, ui_helpers)

    run = gui(["--config-dir", str(d)], script)
    assert run.code == 0
    first = startup_probe["at_window"][0]
    assert first["dark"] is True, "palette tối khi cửa sổ đầu được dựng"
    assert first["mode"] is theme.Mode.DARK
    assert first["accent"] == "ocean"
    assert first["highlight"] == theme.accent_swatch("ocean", True).name(), "Highlight theo màu nhấn đã lưu"
    order = startup_probe["order"]
    assert "theme" in order, f"_run phải gọi theme.set_preference: {order}"
    assert order.index("i18n") < order.index("theme") < order.index("acquire") < order.index("window"), order


@integration
def test_corrupt_settings_starts_with_default_theme(gui, gui_wait, config_dir, ui_helpers, startup_probe):
    from teleport.ui import theme

    d = config_dir("A")
    (d / "settings.json").write_text("{hỏng", encoding="utf-8")
    startup_probe["reset"]()

    def script(run):
        yield gui_wait(lambda: run.windows and run.trays, 15, "cửa sổ và khay")
        _quit(run, ui_helpers)

    run = gui(["--config-dir", str(d)], script)
    assert run.code == 0
    first = startup_probe["at_window"][0]
    assert first["mode"] is theme.Mode.SYSTEM
    assert first["accent"] == theme.DEFAULT_ACCENT
    assert first["dark"] is False, "offscreen: hệ điều hành không tối → sáng"


def _disk(config: Path) -> dict:
    path = config / "settings.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}


@integration
def test_theme_and_accent_remembered_across_runs(
    gui, gui_wait, config_dir, ui_helpers, monkeypatch, startup_probe
):
    from teleport.ui import app as ui_app
    from teleport.ui import theme
    from teleport.ui.settings import Section
    from teleport.ui.widgets import SegmentedControl

    apps: list = []
    real_init = ui_app.GuiApp.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        apps.append(self)

    monkeypatch.setattr(ui_app.GuiApp, "__init__", init)

    def loaded() -> bool:
        return bool(apps) and apps[-1].settings.page.name_edit.isEnabled()

    def controls(page):
        [seg] = page.findChildren(SegmentedControl)
        radios = sorted(seg.findChildren(QRadioButton), key=lambda b: b.mapTo(seg, b.rect().topLeft()).x())
        swatches = [b for b in page.findChildren(QAbstractButton) if b.accessibleDescription() == "Màu nhấn"]
        return radios, swatches

    d = config_dir("A")
    seen: dict = {}
    startup_probe["reset"]()

    def first(run):
        yield gui_wait(lambda: run.hosts and run.hosts[-1].running and loaded(), 15, "trang nạp xong")
        page = apps[-1].settings.page
        window = run.window
        window.settings_button.click()
        page.show_section(Section.APPEARANCE)
        QApplication.processEvents()
        radios, swatches = controls(page)
        radios[2].click()  # Tối
        [violet] = [b for b in swatches if b.accessibleName() == theme.accent_name("violet")]
        violet.click()
        yield gui_wait(
            lambda: _disk(d).get("theme") == "dark" and _disk(d).get("accent") == "violet",
            5,
            "settings.json có theme=dark, accent=violet",
        )
        yield gui_wait(lambda: theme.current_accent() == "violet", 5, "màu nhấn được áp")
        seen["applied"] = (
            theme.current_mode(),
            theme.current_accent(),
            theme.is_dark_palette(QApplication.palette()),
        )
        _quit(run, ui_helpers)

    def second(run):
        yield gui_wait(
            lambda: run.hosts and run.hosts[-1].running and len(apps) == 2 and loaded(), 15, "lần 2"
        )
        radios, swatches = controls(apps[-1].settings.page)
        seen["radio_dark"] = radios[2].isChecked()
        seen["violet_checked"] = [b.accessibleName() for b in swatches if b.isChecked()]
        _quit(run, ui_helpers)

    gui(["--config-dir", str(d)], first)
    assert startup_probe["at_window"][0]["mode"] is theme.Mode.SYSTEM, "lần đầu chưa lưu: theo hệ điều hành"
    assert seen["applied"] == (theme.Mode.DARK, "violet", True), "chọn xong áp ngay"
    startup_probe["reset"]()
    gui(["--config-dir", str(d)], second)
    at = startup_probe["at_window"][0]
    assert (at["mode"], at["accent"], at["dark"]) == (theme.Mode.DARK, "violet", True), (
        "lần sau áp lại trước cửa sổ"
    )
    assert seen["radio_dark"] is True
    assert seen["violet_checked"] == [theme.accent_name("violet")]


# =================================================================================================
# --selftest
# =================================================================================================


@pytest.mark.slow
def test_selftest_builds_appearance_section_and_runs_set_preference(tmp_path):
    import teleport

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["TELEPORT_HOME"] = str(tmp_path / "home")
    src = str(Path(teleport.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([src, env.get("PYTHONPATH", "")])
    code = (
        "from teleport.ui import appearance, theme\n"
        "made = []\n"
        "modes = []\n"
        "real_init = appearance.AppearanceSection.__init__\n"
        "def init(self, *a, **k):\n"
        "    real_init(self, *a, **k)\n"
        "    made.append(1)\n"
        "appearance.AppearanceSection.__init__ = init\n"
        "real_set = theme.set_preference\n"
        "def set_preference(app, mode, accent):\n"
        "    modes.append(str(theme.Mode(mode)))\n"
        "    return real_set(app, mode, accent)\n"
        "theme.set_preference = set_preference\n"
        "from teleport.ui.app import main\n"
        "rc = main(['--selftest'])\n"
        "print('APPEARANCE', len(made))\n"
        "print('MODES', ','.join(modes))\n"
        "raise SystemExit(rc)\n"
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=120)
    out = p.stdout.decode("utf-8", "replace")
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert b"Traceback" not in p.stderr
    assert "APPEARANCE 0" not in out and "APPEARANCE" in out, f"selftest phải dựng AppearanceSection:\n{out}"
    modes_line = next(line for line in out.splitlines() if line.startswith("MODES"))
    assert "dark" in modes_line and "system" in modes_line, (
        f"set_preference một vòng (DARK rồi SYSTEM): {out}"
    )
    assert not (tmp_path / "home" / "settings.json").exists(), "selftest không ghi settings.json"
