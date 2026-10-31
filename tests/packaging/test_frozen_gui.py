"""Test khói cho bản build PyInstaller của GUI (PR-W1, `Teleport.exe` windowed, `--onedir`).

Như `test_frozen_cli.py` nhưng exe lấy từ biến môi trường `TELEPORT_FROZEN_GUI_EXE` (biến riêng, để
`pytest -m frozen` chạy được cả hai bản cùng lúc mà test CLI không chạy nhầm exe GUI). Không có biến
thì cả module bị bỏ qua. Chạy riêng:

    $env:TELEPORT_FROZEN_GUI_EXE = "<dist>\\teleport-gui\\Teleport.exe"
    pytest tests/packaging -m frozen

Exe windowed không có stdout/stderr (`sys.stderr is None`, docstring `teleport/ui/app.py`), nên chỉ
kiểm mã thoát của `--selftest` (dựng QApplication, i18n, theme, font, icon, cửa sổ, khay mà không
hiện; không lấy khóa, không mở mạng; chi tiết ở docstring `teleport/ui/app.py`) và bố cục thư mục
build. Chạy bằng nền tảng Qt thật (`qwindows`): bản build chỉ đóng plugin đó.

Giới hạn: không chạy node thật từ exe (sẽ bật hộp hỏi tường lửa của Windows); giải nén trên máy sạch
(Windows Sandbox), khay và radar kiểm bằng tay. Khi `ui` ghi log ra `<config_dir>/logs/` (spec
hackathon Q7), xem lại test "selftest không tạo file" bên dưới.
"""

from __future__ import annotations

import os
import struct
import subprocess
from pathlib import Path

import pytest

ENV_EXE = "TELEPORT_FROZEN_GUI_EXE"
EXE = os.environ.get(ENV_EXE, "").strip()

if not EXE:
    pytest.skip(f"không có {ENV_EXE}: chưa có bản build GUI để kiểm", allow_module_level=True)

pytestmark = [pytest.mark.frozen, pytest.mark.integration]

TIMEOUT = 60.0
EXIT_OK, EXIT_USAGE = 0, 2  # teleport.ui.app.EXIT_*
DIST = Path(EXE).resolve().parent
INTERNAL = DIST / "_internal"
IMAGE_SUBSYSTEM_WINDOWS_GUI = 2


def base_env(home: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for name in ("TELEPORT_HOME", "TELEPORT_FROZEN_EXE", ENV_EXE, "QT_QPA_PLATFORM", "QT_PLUGIN_PATH"):
        env.pop(name, None)
    if home is not None:
        env["TELEPORT_HOME"] = str(home)
    return env


def run(argv: list[str], *, env: dict[str, str]) -> int:
    """Chạy exe tới khi thoát, trả mã thoát. Treo (ví dụ hộp báo lỗi của bootloader) thì hỏng test."""
    p = subprocess.run(
        [EXE, *argv],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=TIMEOUT,
        env=env,
        check=False,
    )
    return p.returncode


def pe_subsystem(path: Path) -> int:
    """Trường Subsystem trong optional header của file PE."""
    data = path.read_bytes()[:4096]
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[pe : pe + 4] == b"PE\0\0", "không phải file PE"
    return struct.unpack_from("<H", data, pe + 24 + 68)[0]


def test_exe_is_windowed_onedir() -> None:
    assert Path(EXE).name == "Teleport.exe"
    assert pe_subsystem(Path(EXE)) == IMAGE_SUBSYSTEM_WINDOWS_GUI
    assert (INTERNAL / "PySide6" / "Qt6Core.dll").is_file()  # --onedir: Qt là file riêng, thay được


@pytest.mark.parametrize("rel", ["PySide6/__init__.py", "shiboken6/__init__.py", "zeroconf/__init__.py"])
def test_lgpl_python_packages_are_separate_files_not_in_exe(rel: str) -> None:
    assert (INTERNAL / rel).is_file(), rel


def test_selftest_with_config_dir_returns_0_and_creates_nothing(tmp_path: Path) -> None:
    home = tmp_path / "cau-hinh"
    assert run(["--selftest", "--config-dir", str(home)], env=base_env(tmp_path / "khong-dung")) == EXIT_OK
    assert not home.exists()
    assert not (tmp_path / "khong-dung").exists()


def test_selftest_with_teleport_home_returns_0(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert run(["--selftest"], env=base_env(home)) == EXIT_OK
    assert not (home / "device.crt").exists()


def test_bad_argument_returns_usage_without_hanging(tmp_path: Path) -> None:
    assert run(["--khong-co-co-nay"], env=base_env(tmp_path / "home")) == EXIT_USAGE


def test_license_files_next_to_exe() -> None:
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.txt", "SOURCE.txt"):
        assert (DIST / name).is_file(), name
    notices = (DIST / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")
    assert "bản GUI cho Windows" in notices
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in notices
    assert "Ghi công: Portions of this software" in notices
    assert "Fluent UI System Icons" in notices
    assert "MIT License" in notices
    assert "SIL OPEN FONT LICENSE Version 1.1" in notices
    source = (DIST / "SOURCE.txt").read_text(encoding="utf-8")
    assert "_internal\\PySide6\\" in source
    assert "qtbase-everywhere-src-" in source


@pytest.mark.parametrize(
    "pattern",
    [
        "PySide6/opengl32sw.dll",
        "PySide6/Qt6Qml*.dll",
        "PySide6/Qt6Quick*.dll",
        "PySide6/Qt6Pdf*.dll",
        "PySide6/Qt6SvgWidgets*.dll",  # Qt6Svg.dll thì cần (QSvgRenderer của ui/icons.py)
        "PySide6/QtQml*.pyd",
        "PySide6/translations/*",
        "PySide6/plugins/tls/*",
        "PySide6/plugins/imageformats/*",
        "PySide6/plugins/iconengines/*",  # icon vẽ bằng QSvgRenderer, không qua QIcon(svg)
        "PySide6/plugins/platforms/qoffscreen.dll",
        "api-ms-win-*.dll",
        "ucrtbase.dll",
    ],
)
def test_unused_qt_parts_are_not_bundled(pattern: str) -> None:
    assert not list(INTERNAL.glob(pattern)), pattern


@pytest.mark.parametrize(
    "rel",
    [
        "PySide6/Qt6Gui.dll",
        "PySide6/Qt6Widgets.dll",
        "PySide6/Qt6Network.dll",  # QLocalServer (một bản mỗi thư mục cấu hình)
        "PySide6/plugins/platforms/qwindows.dll",
        "PySide6/plugins/styles/qmodernwindowsstyle.dll",
        "PySide6/Qt6Svg.dll",
        "PySide6/QtSvg.pyd",
        "teleport/ui/assets/icons/LICENSE",
        "teleport/ui/assets/icons/checkmark_circle_20_regular.svg",
        "teleport/ui/assets/fonts/BricolageGrotesque.ttf",
        "teleport/ui/assets/fonts/OFL.txt",
    ],
)
def test_needed_qt_parts_are_bundled(rel: str) -> None:
    assert (INTERNAL / rel).is_file(), rel
