"""Icon SVG tô màu của `teleport.ui.icons` (spec-a1-a2, mục A2 và “Áp dụng hoi-lai 30/10”).

Viết theo đặc tả, không theo code:

- 42 tên Fluent UI System Icons (`_20_regular`; năm tên có thêm `_24_regular`), 47 file
  `assets/icons/<tên>_<cỡ>_regular.svg` đọc qua `importlib.resources.files("teleport.ui")`.
- `pixmap(name, size, color, dpr=1.0)`: cỡ vật lý `round(size*dpr)`, `devicePixelRatio() == dpr`, tô bằng
  `CompositionMode_SourceIn` (mọi điểm ảnh alpha ≥ 128 có RGB = màu truyền, lệch ≤ 2 mỗi kênh); cache theo
  (tên, cỡ, màu, dpr); tên hay cỡ không có → `KeyError`.
- `icon(name, size, color, disabled=None)`: dựng từ pixmap (Normal/Disabled) ở dpr 1 và 2, không
  `QIcon(đường dẫn svg)`.
- SVG là dữ liệu tải về (không tin cậy): không `<script`, `<image`, `<foreignObject`, `href` ra ngoài.

Module `teleport.ui.icons` chỉ import bên trong test/fixture, để khi module chưa có chỉ các test dùng nó đỏ.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from importlib.resources import files
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QIcon, QImage, QPixmap

UI_SRC = Path(__file__).resolve().parents[2] / "src" / "teleport" / "ui"

# Danh sách tên trong spec (mục A2, “Icon”), 42 tên theo bản sửa của hoi-lai.
SPEC_NAMES = frozenset(
    {
        "search", "settings", "add", "chevron_left", "more_horizontal",
        "desktop", "laptop",
        "checkmark_circle", "error_circle", "warning", "info", "plug_disconnected", "eye_off",
        "pause_circle",
        "pause", "play", "dismiss", "arrow_clockwise", "arrow_down", "arrow_upload", "arrow_download",
        "attach", "folder", "folder_open", "clipboard_paste", "copy", "delete", "link_dismiss",
        "checkmark",
        "document", "document_pdf", "image", "video", "music_note_2", "folder_zip",
        "cloud_off", "shield_checkmark", "arrow_sync",
        "globe", "lock_closed", "arrow_swap", "wrench",
    }
)  # fmt: skip
# Tên có dấu † trong spec: thêm bản 24 px.
WITH_24 = frozenset({"desktop", "laptop", "cloud_off", "shield_checkmark", "arrow_sync"})
EXPECTED_SIZES = {name: ((20, 24) if name in WITH_24 else (20,)) for name in SPEC_NAMES}
ALL_PAIRS = sorted((name, size) for name, sizes in EXPECTED_SIZES.items() for size in sizes)

# Token dùng tô icon trên giao diện (theme.Tokens), cả sáng và tối.
TINT_TOKENS = (
    "text",
    "text_muted",
    "accent_strong",
    "accent_text",
    "success",
    "warning",
    "danger",
    "offline",
)

TOLERANCE = 2  # mỗi kênh, cho điểm ảnh alpha ≥ 128 (hoi-lai 30/10)


def icons_dir():
    return files("teleport.ui") / "assets" / "icons"


def svg_paths() -> list:
    return sorted((p for p in icons_dir().iterdir() if p.name.lower().endswith(".svg")), key=lambda p: p.name)


@pytest.fixture
def icons(qapp):
    from teleport.ui import icons as module

    return module


def as_argb(pixmap: QPixmap) -> QImage:
    """Ảnh không nhân trước alpha, để đọc RGB thật của điểm ảnh bán trong suốt."""
    return pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)


def opaque_pixels(image: QImage, threshold: int = 128) -> list[QColor]:
    out = []
    for y in range(image.height()):
        for x in range(image.width()):
            c = image.pixelColor(x, y)
            if c.alpha() >= threshold:
                out.append(c)
    return out


def assert_tinted(pixmap: QPixmap, color: QColor, label: str = "") -> None:
    pixels = opaque_pixels(as_argb(pixmap))
    assert pixels, f"{label}: không có điểm ảnh alpha ≥ 128 (icon rỗng?)"
    bad = [
        (c.red(), c.green(), c.blue())
        for c in pixels
        if max(abs(c.red() - color.red()), abs(c.green() - color.green()), abs(c.blue() - color.blue()))
        > TOLERANCE
    ]
    assert not bad, f"{label}: {len(bad)}/{len(pixels)} điểm ảnh lệch màu {color.name()}, ví dụ {bad[:3]}"


# =================================================================================================
# Tài sản trên đĩa
# =================================================================================================


def test_spec_lists_42_names_and_47_files():
    """Tự kiểm bảng của test: spec (hoi-lai 30/10) chốt 42 tên, 47 file."""
    assert len(SPEC_NAMES) == 42
    assert len(ALL_PAIRS) == 47


@pytest.mark.parametrize(("name", "size"), ALL_PAIRS, ids=[f"{n}-{s}" for n, s in ALL_PAIRS])
def test_every_spec_icon_has_its_svg_file(name, size):
    path = icons_dir() / f"{name}_{size}_regular.svg"
    assert path.is_file(), f"thiếu {path}"


def test_icons_dir_has_exactly_the_spec_svgs_and_license():
    found = {p.name for p in icons_dir().iterdir()}
    expected = {f"{n}_{s}_regular.svg" for n, s in ALL_PAIRS} | {"LICENSE"}
    assert found == expected, (sorted(found - expected), sorted(expected - found))


def test_icons_license_is_mit_text():
    text = (icons_dir() / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Microsoft" in text
    assert "Permission is hereby granted, free of charge" in text


_FORBIDDEN_TAGS = re.compile(r"<\s*(script|image|foreignObject|iframe|object|embed)\b", re.IGNORECASE)
_HREF = re.compile(r"\b(?:xlink:)?href\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
_EXTERNAL_URL = re.compile(r"url\(\s*['\"]?\s*(?!#)", re.IGNORECASE)
_EVENT_ATTR = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)


@pytest.mark.parametrize("path", svg_paths(), ids=lambda p: p.name)
def test_svg_has_no_active_or_external_content(path):
    text = path.read_text(encoding="utf-8")
    assert not _FORBIDDEN_TAGS.search(text), _FORBIDDEN_TAGS.search(text)
    for _quote, target in _HREF.findall(text):
        assert target.strip().startswith("#"), f"href ra ngoài: {target!r}"
    assert not _EXTERNAL_URL.search(text), "url(...) không trỏ vào chính file"
    assert not _EVENT_ATTR.search(text), "thuộc tính sự kiện on*="
    # Không DOCTYPE/ENTITY (XXE, “billion laughs”), và là XML hợp lệ với gốc <svg>.
    assert "<!DOCTYPE" not in text.upper()
    assert "<!ENTITY" not in text.upper()
    root = ET.fromstring(text)
    assert root.tag.rsplit("}", 1)[-1] == "svg"


def test_svg_scanner_catches_forbidden_content():
    """Bộ dò ở test trên bắt được nội dung độc (không thì nó xanh rỗng)."""
    assert _FORBIDDEN_TAGS.search("<svg><script>alert(1)</script></svg>")
    assert _FORBIDDEN_TAGS.search('<svg><image href="x.png"/></svg>')
    assert _FORBIDDEN_TAGS.search("<svg><foreignObject/></svg>")
    hrefs = [t for _q, t in _HREF.findall('<use xlink:href="http://evil/x.svg#a"/><use href="#ok"/>')]
    assert hrefs == ["http://evil/x.svg#a", "#ok"]
    assert _EXTERNAL_URL.search('<path fill="url(https://evil/p)"/>')
    assert not _EXTERNAL_URL.search('<path fill="url(#grad)"/>')
    assert _EVENT_ATTR.search('<svg onload="x()">')


# =================================================================================================
# NAMES, SIZES
# =================================================================================================


def test_names_match_spec(icons):
    assert isinstance(icons.NAMES, frozenset)
    assert icons.NAMES == SPEC_NAMES, (sorted(icons.NAMES - SPEC_NAMES), sorted(SPEC_NAMES - icons.NAMES))


def test_sizes_match_spec(icons):
    assert set(icons.SIZES) == set(SPEC_NAMES)
    assert {name: tuple(sorted(sizes)) for name, sizes in icons.SIZES.items()} == EXPECTED_SIZES


# =================================================================================================
# pixmap
# =================================================================================================


@pytest.mark.parametrize("dpr", [1.0, 2.0])
@pytest.mark.parametrize(("name", "size"), ALL_PAIRS, ids=[f"{n}-{s}" for n, s in ALL_PAIRS])
def test_pixmap_renders_non_empty_at_physical_size(icons, name, size, dpr):
    color = QColor("#1E7F61")
    pm = icons.pixmap(name, size, color, dpr)
    assert not pm.isNull()
    physical = round(size * dpr)
    assert (pm.width(), pm.height()) == (physical, physical)
    assert pm.devicePixelRatio() == pytest.approx(dpr)
    assert_tinted(pm, color, f"{name}_{size}@{dpr}")


def test_pixmap_default_dpr_is_1(icons):
    pm = icons.pixmap("search", 20, QColor("#2A251E"))
    assert (pm.width(), pm.height()) == (20, 20)
    assert pm.devicePixelRatio() == pytest.approx(1.0)


@pytest.mark.parametrize(("size", "dpr", "physical"), [(20, 1.25, 25), (20, 1.5, 30), (24, 1.75, 42)])
def test_pixmap_fractional_dpr_uses_rounded_physical_size(icons, size, dpr, physical):
    name = "desktop"  # có cả 20 và 24
    pm = icons.pixmap(name, size, QColor("#0B5E47"), dpr)
    assert (pm.width(), pm.height()) == (physical, physical)
    assert pm.devicePixelRatio() == pytest.approx(dpr)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("token", TINT_TOKENS)
def test_pixmap_is_tinted_with_theme_token(icons, dark, token):
    from teleport.ui import theme

    color = getattr(theme.tokens(dark), token)
    for name, size in (("checkmark_circle", 20), ("arrow_sync", 24), ("folder_zip", 20)):
        for dpr in (1.0, 2.0):
            assert_tinted(icons.pixmap(name, size, color, dpr), color, f"{token}/{name}@{dpr}")


def test_pixmap_cache_distinguishes_color(icons):
    red, blue = QColor(200, 0, 0), QColor(0, 0, 200)
    assert_tinted(icons.pixmap("play", 20, red), red, "đỏ")
    assert_tinted(icons.pixmap("play", 20, blue), blue, "xanh (không lấy nhầm cache màu đỏ)")
    assert_tinted(icons.pixmap("play", 20, red), red, "đỏ lần hai")


def test_pixmap_cache_distinguishes_dpr_and_size(icons):
    color = QColor("#A8361F")
    assert icons.pixmap("laptop", 20, color, 1.0).width() == 20
    assert icons.pixmap("laptop", 20, color, 2.0).width() == 40
    assert icons.pixmap("laptop", 24, color, 1.0).width() == 24
    assert icons.pixmap("laptop", 20, color, 1.0).width() == 20


def test_pixmap_same_args_give_same_image(icons):
    color = QColor("#665B4D")
    a = icons.pixmap("settings", 20, color, 2.0).toImage()
    b = icons.pixmap("settings", 20, color, 2.0).toImage()
    assert a == b


def test_pixmap_keeps_shape_of_different_icons_distinct(icons):
    """Hai icon khác nhau thì khác ảnh (không trả một ảnh chung cho mọi tên)."""
    color = QColor("#2A251E")
    assert icons.pixmap("play", 20, color).toImage() != icons.pixmap("pause", 20, color).toImage()


@pytest.mark.parametrize(
    "name",
    [
        "khong_co",
        "",
        "Search",  # phân biệt hoa thường: tên trong NAMES là chữ thường
        "search_20_regular",  # tên file, không phải tên icon
        "ic_fluent_search_20_regular",
        "../icons/search",
        "..\\icons\\search",
        "assets/icons/search",
        "C:\\Windows\\search",
        "LICENSE",
        "search\x00",
        "a" * 10_000,
    ],
    ids=lambda n: repr(n)[:30],
)
def test_pixmap_unknown_name_raises_key_error(icons, name):
    with pytest.raises(KeyError):
        icons.pixmap(name, 20, QColor("black"))


@pytest.mark.parametrize(
    ("name", "size"), [("search", 24), ("search", 16), ("desktop", 32), ("add", 0), ("add", -20)]
)
def test_pixmap_unknown_size_raises_key_error(icons, name, size):
    with pytest.raises(KeyError):
        icons.pixmap(name, size, QColor("black"))


# =================================================================================================
# icon
# =================================================================================================


def test_icon_normal_mode_is_tinted(icons):
    color = QColor("#1E7F61")
    ic = icons.icon("folder", 20, color)
    assert isinstance(ic, QIcon)
    assert not ic.isNull()
    assert_tinted(ic.pixmap(QSize(20, 20), QIcon.Mode.Normal), color, "Normal")


def test_icon_disabled_mode_uses_disabled_color(icons):
    color, disabled = QColor("#2A251E"), QColor("#B8AC9A")
    ic = icons.icon("delete", 20, color, disabled)
    assert_tinted(ic.pixmap(QSize(20, 20), QIcon.Mode.Disabled), disabled, "Disabled")
    assert_tinted(ic.pixmap(QSize(20, 20), QIcon.Mode.Normal), color, "Normal")


def test_icon_without_disabled_color_still_works(icons):
    ic = icons.icon("delete", 20, QColor("#2A251E"))
    assert not ic.isNull()
    assert not ic.pixmap(QSize(20, 20), QIcon.Mode.Disabled).isNull()


@pytest.mark.parametrize("dpr", [1.0, 2.0])
def test_icon_holds_sharp_pixmap_at_dpr(icons, dpr):
    """`icon()` thêm pixmap ở dpr 1 và 2: QIcon trả đúng ảnh của `pixmap(…, dpr)`, không phóng ảnh 1x."""
    color, disabled = QColor("#0B5E47"), QColor("#8A7F70")
    ic = icons.icon("arrow_sync", 24, color, disabled)
    for mode, c in ((QIcon.Mode.Normal, color), (QIcon.Mode.Disabled, disabled)):
        got = ic.pixmap(QSize(24, 24), dpr, mode)
        want = icons.pixmap("arrow_sync", 24, c, dpr)
        assert (got.width(), got.height()) == (want.width(), want.height()), mode
        assert as_argb(got) == as_argb(want), f"{mode} @{dpr}: ảnh khác pixmap() cùng dpr (bị phóng?)"


def test_icon_unknown_name_raises_key_error(icons):
    with pytest.raises(KeyError):
        icons.icon("khong_co", 20, QColor("black"))


# =================================================================================================
# Không dựng QIcon từ đường dẫn file trong src/teleport/ui
# =================================================================================================


def _is_path_like(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add, ast.Mod)):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        return name in {"str", "Path", "PurePath", "fspath", "joinpath", "as_posix", "format", "join"}
    return False


def qicon_path_calls(source: str) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name == "QIcon" and node.args and _is_path_like(node.args[0]):
            lines.append(node.lineno)
    return lines


def test_ui_never_builds_qicon_from_path():
    violations = []
    for path in sorted(UI_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8-sig")
        # Đọc qua AST (lời gọi thật), không quét chữ: docstring được phép nhắc tới `QIcon("….svg")`.
        violations += [f"{path.relative_to(UI_SRC)}:{line}" for line in qicon_path_calls(text)]
    assert not violations, "\n".join(violations)


def test_qicon_path_detector_catches_string_and_path_args():
    src = (
        "QIcon('a.svg')\n"
        "QtGui.QIcon(f'{d}/a.svg')\n"
        "QIcon(str(p))\n"
        "QIcon(Path('x') / 'a.svg')\n"
        "QIcon(base / 'a.svg')\n"
        "QIcon(pixmap)\n"
        "QIcon()\n"
    )
    assert qicon_path_calls(src) == [1, 2, 3, 4, 5]


# =================================================================================================
# Selftest của app khi thiếu file SVG
# =================================================================================================


def _copy_package_without(tmp_path: Path, missing: str | None) -> Path:
    """Bản sao `src/teleport` dưới tmp_path, thiếu một file tài sản; trả thư mục để đặt PYTHONPATH."""
    import shutil

    root = tmp_path / "pkg"
    shutil.copytree(UI_SRC.parent, root / "teleport", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if missing is not None:
        (root / "teleport" / "ui" / missing).unlink()
    return root


def _selftest_with_package(pkg_root: Path, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["TELEPORT_HOME"] = str(home)
    env["PYTHONPATH"] = os.pathsep.join([str(pkg_root), env.get("PYTHONPATH", "")])
    code = (
        "import sys, teleport\n"
        f"assert teleport.__file__.startswith({str(pkg_root)!r}), teleport.__file__\n"
        "from teleport.ui.app import main\n"
        "raise SystemExit(main(['--selftest']))\n"
    )
    return subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=60)


@pytest.mark.slow
def test_selftest_fails_when_icon_file_missing(tmp_path):
    root = _copy_package_without(tmp_path, "assets/icons/checkmark_circle_20_regular.svg")
    p = _selftest_with_package(root, tmp_path / "home")
    err = p.stderr.decode("utf-8", "replace")
    assert "AssertionError" not in err, err  # bản sao phải được nạp, không thì test vô nghĩa
    assert p.returncode == 1, (p.returncode, err)  # EXIT_ERROR


@pytest.mark.slow
def test_selftest_passes_with_copied_package(tmp_path):
    """Đối chứng cho test trên: bản sao đầy đủ thì selftest qua (lỗi ở trên đúng là do thiếu file)."""
    root = _copy_package_without(tmp_path, None)
    p = _selftest_with_package(root, tmp_path / "home")
    assert p.returncode == 0, (p.returncode, p.stderr.decode("utf-8", "replace"))
