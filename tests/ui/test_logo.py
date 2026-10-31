"""Logo của dự án (`ui/logo.py`): SVG nhúng trong mã, vẽ đủ màu qua `icons.render_svg`, dùng cho icon
cửa sổ/khay (`tray.make_icon`) và `teleport.ico` (`packaging/pyinstaller/make_icon.py`)."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QImage

pytestmark = pytest.mark.gui

REPO = Path(__file__).resolve().parents[2]

# Màu trong SVG (lớp .cls-3 thân con mèo, .cls-4 mũi tên); nền vuông là màu đen mặc định.
BODY = QColor("#f5ece0")
ARROW = QColor("#cea3f3")
TOLERANCE = 6


@pytest.fixture
def logo(qapp):
    from teleport.ui import logo as module

    return module


def near(color: QColor, expected: QColor) -> bool:
    return (
        max(
            abs(color.red() - expected.red()),
            abs(color.green() - expected.green()),
            abs(color.blue() - expected.blue()),
        )
        <= TOLERANCE
    )


def test_svg_is_plain_xml_without_active_or_external_content(logo):
    text = logo.SVG.decode("ascii")
    root = ET.fromstring(text)
    assert root.tag.rsplit("}", 1)[-1] == "svg"
    assert root.get("viewBox") == "0 0 1024 1024"
    lowered = text.lower()
    for forbidden in ("<!doctype", "<!entity", "<script", "<image", "<foreignobject", "href", "url("):
        assert forbidden not in lowered, forbidden
    assert not re.search(r"\son[a-z]+\s*=", lowered)


@pytest.mark.parametrize("size", [16, 24, 32, 48, 64, 128, 256])
def test_pixmap_has_requested_size_and_transparent_rounded_corner(logo, size):
    pixmap = logo.pixmap(size)
    assert not pixmap.isNull()
    assert (pixmap.width(), pixmap.height()) == (size, size)
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    assert image.pixelColor(0, 0).alpha() < 64  # góc bo: trong suốt
    centre = image.pixelColor(size // 2, size // 2)
    assert centre.alpha() == 255


def test_pixmap_keeps_svg_colours_untinted(logo):
    image = logo.pixmap(1024).toImage().convertToFormat(QImage.Format.Format_ARGB32)
    assert image.pixelColor(512, 900) == QColor("#000000")  # nền vuông dưới hình
    assert image.pixelColor(330, 480) == QColor("#000000")  # lỗ vòng trái
    assert near(image.pixelColor(760, 650), BODY), image.pixelColor(760, 650).name()
    assert near(image.pixelColor(512, 512), ARROW), image.pixelColor(512, 512).name()


def test_pixmap_honours_device_pixel_ratio(logo):
    pixmap = logo.pixmap(32, dpr=2.0)
    assert (pixmap.width(), pixmap.height()) == (64, 64)
    assert pixmap.devicePixelRatio() == 2.0
    with pytest.raises(ValueError):
        logo.pixmap(32, dpr=0)


def test_render_svg_returns_null_pixmap_for_broken_svg(qapp):
    from teleport.ui import icons

    assert icons.render_svg(b"<svg", 32).isNull()
    assert icons.render_svg(b"not svg at all", 32).isNull()


def test_tray_icon_is_the_logo(qapp, logo):
    from teleport.ui import tray

    icon = tray.make_icon()
    assert not icon.isNull()
    sizes = {(s.width(), s.height()) for s in icon.availableSizes()}
    assert {(16, 16), (32, 32), (256, 256)} <= sizes
    assert icon.pixmap(256).toImage() == logo.pixmap(256).toImage()


def test_logo_render_logs_nothing(logo, caplog):
    with caplog.at_level(logging.WARNING):
        logo.pixmap(48)
    assert not caplog.records


@pytest.mark.slow
def test_committed_ico_matches_logo():
    """`teleport.ico` đã commit phải đúng là logo hiện tại (`make_icon.py --check`)."""
    result = subprocess.run(
        [sys.executable, str(REPO / "packaging" / "pyinstaller" / "make_icon.py"), "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=REPO,
        check=False,
    )
    assert result.returncode == 0, result.stderr
