"""Danh tính của app trên Windows: AppUserModelID (`ui.app`) và icon của bóng thoại khay (`ui.tray`).

Không có hai thứ này thì taskbar hiện icon Python và toast ghi “Python” với icon chữ “i”.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="AppUserModelID chỉ có trên Windows")


class _Stop(Exception):
    pass


def fake_shell32(monkeypatch, fn) -> None:
    import ctypes

    shell32 = SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=fn)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(shell32=shell32), raising=False)  # Linux, macOS


@windows_only
def test_sets_fixed_app_user_model_id(monkeypatch):
    from teleport.ui import app

    calls: list[str] = []
    fake_shell32(monkeypatch, lambda value: calls.append(value) or 0)
    app._set_app_user_model_id()
    assert calls == ["NhaCongDong.Teleport"] == [app.APP_USER_MODEL_ID]


@windows_only
@pytest.mark.parametrize("outcome", ["raise", "hresult"])
def test_app_user_model_id_failure_logs_debug_and_continues(monkeypatch, caplog, outcome):
    from teleport.ui import app

    def fn(value):
        if outcome == "raise":
            raise OSError("không có")
        return -2147024809  # E_INVALIDARG

    fake_shell32(monkeypatch, fn)
    with caplog.at_level(logging.DEBUG, logger=app.__name__):
        app._set_app_user_model_id()
    assert [r.levelno for r in caplog.records] == [logging.DEBUG]


@pytest.mark.parametrize("platform, frozen", [("linux", False), ("darwin", False), ("win32", True)])
def test_app_user_model_id_only_for_source_runs_on_windows(monkeypatch, platform, frozen):
    """Ngoài Windows không có; bản exe giữ danh tính của `Teleport.exe` (bản zip không hiện định danh)."""
    from teleport.ui import app

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    fake_shell32(monkeypatch, lambda value: pytest.fail("không được gọi"))
    app._set_app_user_model_id()


def test_main_sets_app_user_model_id_before_qapplication(monkeypatch):
    from teleport.ui import app

    order: list[str] = []
    monkeypatch.setattr(app, "_set_app_user_model_id", lambda: order.append("aumid"))

    def application():
        order.append("qapp")
        raise _Stop

    monkeypatch.setattr(app, "_application", application)
    with pytest.raises(_Stop):
        app.main(["--selftest"])
    assert order == ["aumid", "qapp"]


@windows_only
@pytest.mark.slow
def test_real_process_reads_back_app_user_model_id(tmp_path):
    """Gọi thật trong tiến trình con (không đổi danh tính của tiến trình pytest)."""
    code = (
        "import ctypes\n"
        "from ctypes import wintypes\n"
        "from teleport.ui import app\n"
        "app._set_app_user_model_id()\n"
        "out = ctypes.c_wchar_p()\n"
        "shell32 = ctypes.windll.shell32\n"
        "shell32.GetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.POINTER(ctypes.c_wchar_p)]\n"
        "assert shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(out)) == 0\n"
        "print(out.value)\n"
        "ctypes.windll.ole32.CoTaskMemFree(out)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "NhaCongDong.Teleport"


def test_tray_message_carries_logo_icon_not_standard_icon(qapp, monkeypatch):
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QSystemTrayIcon

    from teleport.ui import tray as tray_module
    from teleport.ui.tray import Tray

    shown: dict[int, bool] = {}
    calls: list[tuple] = []
    monkeypatch.setattr(tray_module, "is_available", lambda: True)
    monkeypatch.setattr(QSystemTrayIcon, "show", lambda self: shown.__setitem__(id(self), True))
    monkeypatch.setattr(QSystemTrayIcon, "isVisible", lambda self: shown.get(id(self), False))
    monkeypatch.setattr(QSystemTrayIcon, "showMessage", lambda self, *args: calls.append(args))

    tray = Tray()
    try:
        tray.show()
        tray.show_message("Đã nhận a.txt từ PC-Nha.")
        assert len(calls) == 1
        title, body, icon = calls[0][:3]
        assert (title, body) == ("Teleport", "Đã nhận a.txt từ PC-Nha.")
        assert isinstance(icon, QIcon)
        assert not icon.isNull()
        assert not icon.pixmap(32, 32).isNull()
    finally:
        tray.dispose()
