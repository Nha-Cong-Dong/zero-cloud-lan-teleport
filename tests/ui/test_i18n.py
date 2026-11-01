"""Chọn ngôn ngữ (spec D2 mục 1.4, 9.13; test 4.5): `available_languages`, `language_name`,
`apply_preference` của `teleport.ui.i18n` và ngôn ngữ đã lưu được áp lúc mở app (`ui/app.py::_run`).

Viết từ spec, không từ code:

- `available_languages()` = `SOURCE_LANGUAGE` + mọi `i18n/teleport_<mã>.qm` có thật, sắp xếp; lỗi đọc thư
  mục (`FileNotFoundError` / `NotADirectoryError` / `OSError`) → `("vi",)`.
- `language_name(code)`: tên tự gọi (`vi` “Tiếng Việt”, `en` “English”), mã lạ → chính mã.
- `apply_preference(None)` theo hệ điều hành; mã sai dạng → chuỗi nguồn (không ném lỗi).
- `_run` gọi `i18n.install(app, ngôn ngữ)` với ngôn ngữ từ `_saved_ui_preferences(config_dir)` (spec D6
  mục 2.6, thay `_saved_language` của D2): phần tử đầu là `Settings.load(config_dir).language`, mọi lỗi
  → None. Chỉ đọc.

Giả định seam (chưa có trong spec): thư mục bản dịch được tìm qua `i18n.resources.files("teleport.ui")`
(như `_load` hiện có), nên test thay `resources.files` để giả thư mục có file `.qm`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

integration = pytest.mark.integration


@pytest.fixture
def fake_package(monkeypatch, tmp_path):
    """Giả gói `teleport.ui` có thư mục `i18n/` với các file cho trước. Trả hàm `make(names)`."""
    from teleport.ui import i18n

    root = tmp_path / "pkg"
    (root / "i18n").mkdir(parents=True)

    def make(names: list[str]) -> Path:
        for name in names:
            (root / "i18n" / name).write_bytes(b"")
        return root

    real_files = i18n.resources.files

    def files(package):
        if package == "teleport.ui":
            return root
        return real_files(package)

    monkeypatch.setattr(i18n.resources, "files", files)
    return make


def test_available_languages_today_is_source_only():
    from teleport.ui import i18n

    langs = i18n.available_languages()
    assert langs[0:1] == ("vi",) or "vi" in langs
    assert i18n.SOURCE_LANGUAGE in langs
    assert list(langs) == sorted(langs)


def test_available_languages_source_plus_qm_files(fake_package):
    from teleport.ui import i18n

    fake_package(
        [
            "teleport_en.qm",
            "teleport_fr_CA.qm",
            "teleport_vi.qm",  # trùng chuỗi nguồn: không lặp
            "teleport_EN.qm",  # sai dạng mã
            "teleport_en.ts",  # không phải .qm
            "khac_de.qm",  # không đúng tiền tố
            "teleport_.qm",
        ]
    )
    assert i18n.available_languages() == ("en", "fr_CA", "vi")


def test_available_languages_ignores_directory_named_like_qm(fake_package):
    from teleport.ui import i18n

    root = fake_package(["teleport_en.qm"])
    (root / "i18n" / "teleport_de.qm").mkdir()
    assert i18n.available_languages() == ("en", "vi")


@pytest.mark.parametrize("error", [FileNotFoundError, NotADirectoryError, PermissionError, OSError])
def test_available_languages_read_error_falls_back_to_source(monkeypatch, error):
    from teleport.ui import i18n

    def boom(package):
        raise error("không đọc được")

    monkeypatch.setattr(i18n.resources, "files", boom)
    assert i18n.available_languages() == ("vi",)


def test_language_name_endonyms_and_unknown():
    from teleport.ui import i18n

    assert i18n.language_name("vi") == "Tiếng Việt"
    assert i18n.language_name("en") == "English"
    assert i18n.language_name("xx") == "xx"
    assert i18n.language_name("zz_ZZ") == "zz_ZZ"


def test_language_name_is_not_translated(qapp):
    """Tên tự gọi không qua bản dịch: người không đọc được ngôn ngữ đang hiện vẫn tìm thấy ngôn ngữ mình."""
    from PySide6.QtCore import QCoreApplication, QTranslator

    from teleport.ui import i18n

    class Prefix(QTranslator):
        def isEmpty(self):
            return False

        def translate(self, context, source_text, disambiguation=None, n=-1):
            return "[x]" + source_text if source_text else source_text

    t = Prefix()
    QCoreApplication.installTranslator(t)
    try:
        assert i18n.language_name("vi") == "Tiếng Việt"
        assert i18n.language_name("en") == "English"
    finally:
        QCoreApplication.removeTranslator(t)


def test_apply_preference_none_follows_system(qapp, monkeypatch):
    from teleport.ui import i18n

    calls: list[str] = []
    monkeypatch.setattr(i18n, "system_language", lambda: "en_US")
    monkeypatch.setattr(i18n, "set_language", calls.append)
    i18n.apply_preference(None)
    assert calls == ["en_US"]
    i18n.apply_preference("vi")
    assert calls[-1] == "vi"


@pytest.mark.parametrize("bad", ["C", "../en", "en-US", "EN", "e", "vi_vn", ""])
def test_apply_preference_bad_code_uses_source(qapp, monkeypatch, bad):
    from teleport.ui import i18n

    calls: list[str] = []
    monkeypatch.setattr(i18n, "system_language", lambda: "vi")
    monkeypatch.setattr(i18n, "set_language", calls.append)
    i18n.apply_preference(bad)  # không ném lỗi
    assert calls == ["vi"]


def test_apply_preference_real_switch_keeps_source_when_no_qm(qapp):
    from teleport.ui import i18n

    i18n.apply_preference("zz")  # không có teleport_zz.qm: dùng chuỗi nguồn, không lỗi
    assert i18n.current_language() == "vi"
    i18n.apply_preference(None)
    assert i18n.current_language() in i18n.available_languages() or i18n.current_language() == "vi"


@pytest.mark.parametrize(
    ("content", "want"),
    [
        ({"language": "en"}, "en"),
        ({"language": "vi_VN"}, "vi_VN"),
        ({}, None),
        (None, None),  # không có settings.json
        ("{hỏng", None),
        ({"language": 5}, None),
    ],
)
def test_saved_language_reads_settings_only(tmp_path, content, want):
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
    assert ui_app._saved_ui_preferences(d)[0] == want
    assert sorted(p.name for p in d.iterdir()) == before, "chỉ đọc: không tạo file nào"
    assert (path.read_bytes() if path.exists() else None) == raw, "chỉ đọc: không sửa settings.json"


def test_saved_language_unreadable_dir_is_none(tmp_path):
    from teleport.ui import app as ui_app

    f = tmp_path / "la-file"
    f.write_text("x", encoding="utf-8")
    assert ui_app._saved_ui_preferences(f) == (None, None, None)


@integration
def test_saved_language_used_at_startup(gui, gui_wait, config_dir, ui_helpers, monkeypatch):
    from teleport.ui import i18n

    d = config_dir("A")
    (d / "settings.json").write_text(json.dumps({"language": "en"}), encoding="utf-8")
    seen: list = []
    real_install = i18n.install

    def install(app, language=None):
        seen.append(language)
        return real_install(app, language)

    monkeypatch.setattr(i18n, "install", install)

    def script(run):
        from teleport.ui import texts

        yield gui_wait(lambda: run.windows and run.hosts and run.hosts[-1].running, 15, "node chạy")
        # Đang chạy bằng English nên so với câu đã dịch, không ghim chữ Việt.
        quit_text = ui_helpers.plain(texts.menu_quit())
        [action] = [
            a for a in run.tray.actions() if not a.isSeparator() and ui_helpers.plain(a.text()) == quit_text
        ]
        action.trigger()

    run = gui(["--config-dir", str(d)], script)
    assert seen and seen[0] == "en", f"i18n.install phải nhận ngôn ngữ đã lưu: {seen}"
    assert run.code == 0
