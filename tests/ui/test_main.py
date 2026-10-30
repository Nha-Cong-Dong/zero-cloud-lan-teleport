"""`teleport.ui.app.main` bằng tiến trình con (spec-p4-3 mục 3, mục 6.9 và mục 10).

`--selftest` → 0 (dựng QApplication, i18n, theme, cửa sổ, khay mà không hiện, không lấy khóa, không mở
mạng); tham số sai → 2; `sys.stderr is None` (exe windowed) thì không in, không crash.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytest.importorskip("PySide6")

TIMEOUT = 60


def env_for(home) -> dict:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["TELEPORT_HOME"] = str(home)
    return env


def run_module(args: list[str], home) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "teleport.ui", *args],
        capture_output=True,
        env=env_for(home),
        timeout=TIMEOUT,
    )


def run_without_stderr(args: list[str], home) -> subprocess.CompletedProcess:
    """Như exe windowed: `sys.stderr` và `sys.stdout` là None trước khi gọi `main`."""
    code = (
        "import sys\n"
        "sys.stderr = None\n"
        "sys.stdout = None\n"
        "from teleport.ui.app import main\n"
        f"raise SystemExit(main({args!r}))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, env=env_for(home), timeout=TIMEOUT
    )


@pytest.mark.slow
def test_selftest_returns_0_without_locks_or_files(tmp_path):
    home = tmp_path / "home"
    p = run_module(["--selftest", "--config-dir", str(home)], tmp_path / "khong-dung")
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert b"Traceback" not in p.stderr
    # Không lấy khóa, không tạo danh tính.
    for name in ("gui.lock", "node.lock", "device.crt", "device.key"):
        assert not (home / name).exists(), name


@pytest.mark.slow
def test_selftest_uses_teleport_home_without_creating_identity(tmp_path):
    home = tmp_path / "home"
    p = run_module(["--selftest"], home)
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    assert not (home / "device.crt").exists()


@pytest.mark.slow
@pytest.mark.parametrize(
    "args", [["--khong-co"], ["--config-dir"], ["thua"]], ids=["unknown", "missing", "extra"]
)
def test_bad_arguments_exit_2(tmp_path, args):
    p = run_module(args, tmp_path / "home")
    assert p.returncode == 2, (p.returncode, p.stderr.decode("utf-8", "replace"))
    assert b"Traceback" not in p.stderr
    assert not (tmp_path / "home" / "gui.lock").exists()


@pytest.mark.slow
def test_bad_arguments_with_stderr_none_exit_2_without_crash(tmp_path):
    p = run_without_stderr(["--khong-co"], tmp_path / "home")
    assert p.returncode == 2, (p.returncode, p.stderr.decode("utf-8", "replace"))


@pytest.mark.slow
def test_selftest_with_stderr_none_returns_0(tmp_path):
    p = run_without_stderr(["--selftest"], tmp_path / "home")
    assert p.returncode == 0, (p.returncode, p.stderr.decode("utf-8", "replace"))


@pytest.mark.slow
def test_help_does_not_crash_with_stderr_none(tmp_path):
    p = run_without_stderr(["--help"], tmp_path / "home")
    assert p.returncode in (0, 2), (p.returncode, p.stderr.decode("utf-8", "replace"))
