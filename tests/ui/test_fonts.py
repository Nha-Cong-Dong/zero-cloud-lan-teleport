"""Font điểm nhấn Bricolage Grotesque của `teleport.ui.fonts` (spec-a1-a2, mục A2 và “Áp dụng hoi-lai 30/10”).

Viết theo đặc tả, không theo code:

- `install() -> str | None`: nạp `assets/fonts/*.ttf` qua `importlib.resources` bằng
  `QFontDatabase.addApplicationFontFromData`; trả `theme.ACCENT_FAMILY` nếu có trong
  `applicationFontFamilies(id)` (không lấy phần tử đầu); lỗi → `log.debug`, `None`, không ngoại lệ; gọi lại
  không nạp hai lần.
- `_reset()`: seam cho test (`removeApplicationFont` + xóa đệm), sau đó `install()` nạp lại được.
- `missing_glyphs(text, font) -> set[str]`.
- `test_accent_weights_are_real_instances`: SECTION là “Medium”, TITLE là “Bold” thật (không đậm giả).
- Lỗi nạp thì `font_for(TITLE)` rơi về font hệ thống (vẫn đậm).
- `ui/app.py` `_selftest` trả `EXIT_ERROR` (1) khi thiếu file tài sản hoặc `addApplicationFontFromData`
  trả −1.

Fixture `fonts` trả state về như cũ: hoàn tác monkeypatch, `_reset()` rồi `install()` lại nếu trước test font
đang được nạp. Giả định: máy chạy test không cài sẵn Bricolage Grotesque vào hệ thống (khi đó các test “rơi
về hệ thống” không kiểm được, và báo rõ lý do).
"""

from __future__ import annotations

import importlib.resources
import logging
import os
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QFont, QFontDatabase, QFontInfo
from PySide6.QtWidgets import QApplication

UI_SRC = Path(__file__).resolve().parents[2] / "src" / "teleport" / "ui"
FONT_FILE = UI_SRC / "assets" / "fonts" / "BricolageGrotesque.ttf"
ACCENT = "Bricolage Grotesque"  # theme.ACCENT_FAMILY theo spec A1


def _vietnamese_letters() -> str:
    """Mọi chữ cái tiếng Việt dựng sẵn (NFC), thường và hoa: 12 nguyên âm x 6 thanh, đ, phụ âm."""
    bases = ["a", "ă", "â", "e", "ê", "i", "o", "ô", "ơ", "u", "ư", "y"]
    tones = ["", "̀", "́", "̉", "̃", "̣"]
    lower = {unicodedata.normalize("NFC", b + t) for b in bases for t in tones}
    lower |= set("đbcdghklmnpqrstvx")
    assert all(len(ch) == 1 for ch in lower)
    return "".join(sorted(lower | {ch.upper() for ch in lower}))


VIETNAMESE = _vietnamese_letters()
PUNCTUATION = "…“”·–—"
ASCII_ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def fonts(qapp, monkeypatch):
    from teleport.ui import fonts as module

    was_installed = QFontDatabase.hasFamily(ACCENT)
    yield module
    monkeypatch.undo()  # trước khi nạp lại, để install() dưới đây chạy với Qt và resources thật
    module._reset()
    if was_installed:
        module.install()


@pytest.fixture
def theme(qapp):
    from teleport.ui import theme as module

    return module


@pytest.fixture
def fresh(fonts):
    """`fonts` sau `_reset()`: font điểm nhấn chưa được nạp."""
    fonts._reset()
    if QFontDatabase.hasFamily(ACCENT):
        pytest.fail(
            f"sau _reset() Qt vẫn thấy {ACCENT!r}: _reset() không gỡ font, hoặc máy đã cài font này vào hệ"
            " thống (khi đó các test rơi về hệ thống không kiểm được)"
        )
    return fonts


# =================================================================================================
# Tài sản
# =================================================================================================


def test_font_and_ofl_files_are_packaged_resources():
    d = importlib.resources.files("teleport.ui") / "assets" / "fonts"
    names = {p.name for p in d.iterdir()}
    assert "BricolageGrotesque.ttf" in names
    assert "OFL.txt" in names
    assert {n for n in names if not n.endswith((".ttf", ".txt"))} == set(), names
    ofl = (d / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in ofl
    assert "Bricolage" in ofl


def test_accent_family_constant_matches_spec(theme):
    assert theme.ACCENT_FAMILY == ACCENT


# =================================================================================================
# install
# =================================================================================================


def test_install_returns_accent_family_and_qt_knows_it(fresh, theme):
    assert fresh.install() == theme.ACCENT_FAMILY
    assert QFontDatabase.hasFamily(theme.ACCENT_FAMILY)
    assert theme.ACCENT_FAMILY in QFontDatabase.families()
    assert theme.accent_family() == theme.ACCENT_FAMILY


def test_install_twice_loads_font_once(fresh, monkeypatch):
    real = QFontDatabase.addApplicationFontFromData
    calls: list[int] = []

    def counting(data):
        calls.append(len(bytes(data)))
        return real(data)

    monkeypatch.setattr(QFontDatabase, "addApplicationFontFromData", staticmethod(counting))
    first = fresh.install()
    second = fresh.install()
    assert first == second == ACCENT
    assert len(calls) == 1, calls  # assets/fonts có đúng một file .ttf


def test_install_after_reset_loads_again(fresh, monkeypatch):
    assert fresh.install() == ACCENT
    fresh._reset()
    assert not QFontDatabase.hasFamily(ACCENT), "_reset() không gỡ font đã nạp"
    real = QFontDatabase.addApplicationFontFromData
    calls: list[int] = []
    monkeypatch.setattr(
        QFontDatabase, "addApplicationFontFromData", staticmethod(lambda d: calls.append(1) or real(d))
    )
    assert fresh.install() == ACCENT
    assert calls == [1], "_reset() không xóa đệm: install() sau đó không nạp lại"
    assert QFontDatabase.hasFamily(ACCENT)


def test_install_picks_accent_family_not_first_in_list(fresh, monkeypatch):
    """hoi-lai 30/10: trả ACCENT_FAMILY nếu có trong `applicationFontFamilies(id)`, không lấy phần tử đầu."""
    real = QFontDatabase.applicationFontFamilies
    monkeypatch.setattr(
        QFontDatabase,
        "applicationFontFamilies",
        staticmethod(lambda font_id: ["Zzz Khac", *real(font_id)]),
    )
    assert fresh.install() == ACCENT


# =================================================================================================
# Rơi về font hệ thống khi file hỏng hoặc thiếu
# =================================================================================================


def _fake_resources(monkeypatch, fonts_module, root: Path) -> None:
    """Thay `importlib.resources.files` (và bản đã import vào module `fonts`, nếu có) để
    `files("teleport.ui")` trả `root`."""

    def fake_files(anchor=None):
        return root

    monkeypatch.setattr(importlib.resources, "files", fake_files)
    if hasattr(fonts_module, "files"):
        monkeypatch.setattr(fonts_module, "files", fake_files)


def _fake_root(tmp_path: Path, ttf: bytes | None) -> Path:
    root = tmp_path / "fake_ui"
    d = root / "assets" / "fonts"
    d.mkdir(parents=True)
    shutil.copyfile(UI_SRC / "assets" / "fonts" / "OFL.txt", d / "OFL.txt")
    if ttf is not None:
        (d / "BricolageGrotesque.ttf").write_bytes(ttf)
    return root


def _fallback_cases() -> list:
    real = FONT_FILE.read_bytes()
    return [
        pytest.param(None, id="thieu-file-ttf"),
        pytest.param(b"", id="ttf-0-byte"),
        pytest.param(b"day khong phai font" * 50, id="ttf-rac"),
        pytest.param(real[:2048], id="ttf-cut-cut"),
    ]


@pytest.mark.parametrize("ttf", _fallback_cases())
def test_install_failure_returns_none_logs_debug_and_title_falls_back(
    fresh, theme, monkeypatch, tmp_path, caplog, ttf
):
    _fake_resources(monkeypatch, fresh, _fake_root(tmp_path, ttf))
    caplog.set_level(logging.DEBUG)
    assert fresh.install() is None
    ours = [r for r in caplog.records if r.name.startswith("teleport")]
    assert any(r.levelno == logging.DEBUG for r in ours), [r.getMessage() for r in ours]
    assert not [r for r in ours if r.levelno >= logging.WARNING], "lỗi nạp font chỉ ghi DEBUG"
    assert theme.accent_family() is None
    title = theme.font_for(theme.TextStyle.TITLE)
    assert title.family() == QApplication.font().family()
    assert title.bold()


def test_install_failure_when_assets_dir_missing(fresh, theme, monkeypatch, tmp_path, caplog):
    root = tmp_path / "khong_co_assets"
    root.mkdir()
    _fake_resources(monkeypatch, fresh, root)
    caplog.set_level(logging.DEBUG)
    assert fresh.install() is None
    assert any(r.levelno == logging.DEBUG and r.name.startswith("teleport") for r in caplog.records)
    assert theme.font_for(theme.TextStyle.SECTION).family() == QApplication.font().family()


def test_install_failure_when_qt_rejects_font(fresh, theme, monkeypatch, caplog):
    """File thật nhưng Qt trả −1 (ví dụ nền không nạp được font): vẫn None, không ngoại lệ."""
    monkeypatch.setattr(QFontDatabase, "addApplicationFontFromData", staticmethod(lambda data: -1))
    caplog.set_level(logging.DEBUG)
    assert fresh.install() is None
    assert any(r.levelno == logging.DEBUG and r.name.startswith("teleport") for r in caplog.records)
    assert theme.font_for(theme.TextStyle.TITLE).family() == QApplication.font().family()


def test_fake_resources_seam_is_really_used(fresh, monkeypatch, tmp_path):
    """Đối chứng: thay resources bằng thư mục có font thật thì install() thành công, nên các ca lỗi ở trên
    đỏ/xanh đúng là do nội dung file, không do seam không tác dụng."""
    _fake_resources(monkeypatch, fresh, _fake_root(tmp_path, FONT_FILE.read_bytes()))
    assert fresh.install() == ACCENT


# =================================================================================================
# Thang chữ với font điểm nhấn
# =================================================================================================


def test_title_and_section_use_accent_family_after_install(fresh, theme):
    assert fresh.install() == ACCENT
    assert theme.font_for(theme.TextStyle.TITLE).family() == ACCENT
    assert theme.font_for(theme.TextStyle.SECTION).family() == ACCENT


def test_accent_weights_are_real_instances(fresh, theme):
    """Spec A2 (Font): bản variable phải cho instance Medium/Bold thật, không đậm giả."""
    assert fresh.install() == ACCENT
    section = QFontInfo(theme.font_for(theme.TextStyle.SECTION))
    title = QFontInfo(theme.font_for(theme.TextStyle.TITLE))
    assert section.family() == ACCENT, section.family()
    assert title.family() == ACCENT, title.family()
    assert section.styleName() == "Medium", section.styleName()
    assert title.styleName() == "Bold", title.styleName()


# =================================================================================================
# missing_glyphs
# =================================================================================================


def test_vietnamese_test_set_is_complete():
    """Tự kiểm bảng chữ của test: 12 nguyên âm x 6 thanh + đ + 16 phụ âm, hai kiểu chữ."""
    assert len(VIETNAMESE) == 2 * (12 * 6 + 1 + 16)
    for ch in "ẮẰẲẴẶắằẳẵặỚỜỞỠỢớờởỡợỨỪỬỮỰứừửữựĐđỹỸ":
        assert ch in VIETNAMESE, ch


@pytest.mark.parametrize(
    "text",
    [VIETNAMESE, PUNCTUATION, ASCII_ALNUM, "Teleport"],
    ids=["tieng-viet", "dau-cau", "chu-so-latin", "Teleport"],
)
def test_accent_font_has_every_needed_glyph(fresh, text):
    assert fresh.install() == ACCENT
    assert fresh.missing_glyphs(text, QFont(ACCENT)) == set()


def test_missing_glyphs_empty_text(fresh):
    assert fresh.install() == ACCENT
    assert fresh.missing_glyphs("", QFont(ACCENT)) == set()


def test_missing_glyphs_reports_characters_the_font_lacks(fresh):
    """Đối chứng: chữ Hán và emoji không có trong Bricolage, phải bị báo (không thì hàm luôn trả rỗng)."""
    assert fresh.install() == ACCENT
    missing = fresh.missing_glyphs("Tê漢\U0001f600", QFont(ACCENT))
    assert missing == {"漢", "\U0001f600"}


# =================================================================================================
# Selftest của app
# =================================================================================================


def _run_selftest(
    home: Path, prelude: str = "", pythonpath: Path | None = None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["TELEPORT_HOME"] = str(home)
    if pythonpath is not None:
        env["PYTHONPATH"] = os.pathsep.join([str(pythonpath), env.get("PYTHONPATH", "")])
    code = prelude + "from teleport.ui.app import main\nraise SystemExit(main(['--selftest']))\n"
    return subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=60)


@pytest.mark.slow
def test_selftest_fails_when_qt_rejects_font(tmp_path):
    prelude = (
        "from PySide6.QtGui import QFontDatabase\n"
        "QFontDatabase.addApplicationFontFromData = staticmethod(lambda data: -1)\n"
    )
    p = _run_selftest(tmp_path / "home", prelude)
    assert p.returncode == 1, (p.returncode, p.stderr.decode("utf-8", "replace"))  # EXIT_ERROR


@pytest.mark.slow
def test_selftest_fails_when_font_file_missing(tmp_path):
    root = tmp_path / "pkg"
    shutil.copytree(UI_SRC.parent, root / "teleport", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (root / "teleport" / "ui" / "assets" / "fonts" / "BricolageGrotesque.ttf").unlink()
    prelude = f"import teleport\nassert teleport.__file__.startswith({str(root)!r}), teleport.__file__\n"
    p = _run_selftest(tmp_path / "home", prelude, pythonpath=root)
    err = p.stderr.decode("utf-8", "replace")
    assert "AssertionError" not in err, err  # bản sao phải được nạp, không thì test vô nghĩa
    assert p.returncode == 1, (p.returncode, err)


@pytest.mark.slow
def test_selftest_passes_with_real_assets_offscreen(tmp_path):
    """hoi-lai 30/10: offscreen trên ba HĐH vẫn qua (font nạp được từ dữ liệu)."""
    p = _run_selftest(tmp_path / "home")
    assert p.returncode == 0, (p.returncode, p.stderr.decode("utf-8", "replace"))
