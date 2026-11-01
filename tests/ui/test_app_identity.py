"""Danh tính của app trên Windows: AppUserModelID (`ui.app`) và icon của bóng thoại khay (`ui.tray`).

Không có hai thứ này thì taskbar hiện icon Python và toast ghi “Python” với icon chữ “i”.
"""

from __future__ import annotations

import contextlib
import logging
import os
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


class FakeWinreg:
    """`winreg` giả: một khóa HKCU trong dict; đếm lần ghi; `fail` làm `CreateKeyEx` ném lỗi."""

    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1
    REG_EXPAND_SZ = 2

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, tuple[str, int]]] = {}
        self.writes: list[tuple[str, str]] = []
        self.fail: OSError | None = None

    def CreateKeyEx(self, root, path, reserved, access):
        assert root is self.HKEY_CURRENT_USER
        assert access & self.KEY_SET_VALUE
        if self.fail is not None:
            raise self.fail
        values = self.keys.setdefault(path, {})
        return contextlib.nullcontext(values)

    def QueryValueEx(self, key, name):
        if name not in key:
            raise FileNotFoundError(name)
        return key[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        key[name] = (value, kind)
        self.writes.append((name, value))


@pytest.fixture
def fake_winreg(monkeypatch):
    fake = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    return fake


KEY = r"Software\Classes\AppUserModelId\NhaCongDong.Teleport"


def test_registers_display_name_and_icon(qapp, fake_winreg, aumid_registration, tmp_path):
    """Toast khi chạy từ nguồn hiện “ZCL Teleport” thay vì chuỗi định danh; icon là logo ở thư mục cấu hình"""
    from PySide6.QtGui import QImage

    from teleport.ui import app

    aumid_registration(tmp_path)
    values = fake_winreg.keys[KEY]
    assert values["DisplayName"] == ("ZCL Teleport", FakeWinreg.REG_SZ)
    icon_path, kind = values["IconUri"]
    assert kind == FakeWinreg.REG_SZ
    assert icon_path == str(tmp_path / app.AUMID_ICON_FILE)
    image = QImage(icon_path)
    assert not image.isNull()
    assert (image.width(), image.height()) == (app.AUMID_ICON_SIZE, app.AUMID_ICON_SIZE)


def test_registration_is_idempotent(qapp, fake_winreg, aumid_registration, tmp_path):
    from teleport.ui import app

    aumid_registration(tmp_path)
    assert len(fake_winreg.writes) == 2
    icon = tmp_path / app.AUMID_ICON_FILE
    old = 1_600_000_000 * 10**9  # bội của 100 ns (độ phân giải mtime của NTFS)
    os.utime(icon, ns=(old, old))
    aumid_registration(tmp_path)
    assert len(fake_winreg.writes) == 2, "giá trị đã đúng thì không ghi lại"
    assert icon.stat().st_mtime_ns == old, "icon cùng nội dung thì không ghi lại"
    icon.write_bytes(b"cu")
    aumid_registration(tmp_path)
    assert icon.read_bytes().startswith(b"\x89PNG"), "icon khác nội dung thì ghi lại"


def test_registration_rewrites_stale_values(qapp, fake_winreg, aumid_registration, tmp_path):
    """Giá trị cũ (tên cũ, icon ở thư mục cấu hình khác, kiểu khác) được ghi đè."""
    fake_winreg.keys[KEY] = {
        "DisplayName": ("Teleport", FakeWinreg.REG_SZ),
        "IconUri": (str(tmp_path / "khac" / "aumid-icon.png"), FakeWinreg.REG_SZ),
    }
    aumid_registration(tmp_path)
    assert [name for name, _ in fake_winreg.writes] == ["DisplayName", "IconUri"]
    fake_winreg.keys[KEY]["DisplayName"] = ("ZCL Teleport", FakeWinreg.REG_EXPAND_SZ)
    aumid_registration(tmp_path)
    assert fake_winreg.writes[-1] == ("DisplayName", "ZCL Teleport")
    assert fake_winreg.keys[KEY]["DisplayName"] == ("ZCL Teleport", FakeWinreg.REG_SZ)


@pytest.mark.parametrize("failure", ["registry", "icon"])
def test_registration_failure_logs_debug_and_continues(
    qapp, fake_winreg, aumid_registration, tmp_path, caplog, failure
):
    from teleport.ui import app

    if failure == "registry":
        fake_winreg.fail = PermissionError("bị chặn")
    else:
        (tmp_path / app.AUMID_ICON_FILE).mkdir()  # chỗ của icon là thư mục: không đọc, không ghi được
    with caplog.at_level(logging.DEBUG, logger=app.__name__):
        aumid_registration(tmp_path)
    assert [r.levelno for r in caplog.records] == [logging.DEBUG]
    assert fake_winreg.writes == []


@pytest.mark.parametrize("platform, frozen", [("linux", False), ("darwin", False), ("win32", True)])
def test_registration_only_for_source_runs_on_windows(
    qapp, fake_winreg, aumid_registration, tmp_path, monkeypatch, platform, frozen
):
    """Bản exe dùng FileDescription của `Teleport.exe`; ngoài Windows không có AppUserModelID."""
    from teleport.ui import app

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    aumid_registration(tmp_path)
    assert fake_winreg.keys == {}
    assert not (tmp_path / app.AUMID_ICON_FILE).exists()


def test_run_registers_after_taking_gui_lock(qapp, monkeypatch, tmp_path):
    """`_run` đăng ký đúng thư mục cấu hình, chỉ sau khi giữ được `gui.lock` (bản thứ hai không ghi)."""
    from types import SimpleNamespace as NS

    from teleport.ui import app

    order: list[object] = []
    monkeypatch.setattr(app.SingleInstance, "acquire", staticmethod(lambda d: order.append("lock") or None))
    monkeypatch.setattr(app, "_register_app_user_model_id", lambda d: order.append(d))
    assert app._run(qapp, NS(background=False), tmp_path) == app.EXIT_OK
    assert order == ["lock"], "bản thứ hai (không giữ được khóa) không đăng ký"

    def broken_init(self, *args, **kwargs):
        raise _Stop

    monkeypatch.setattr(app.SingleInstance, "acquire", staticmethod(lambda d: object()))
    monkeypatch.setattr(app.GuiApp, "__init__", broken_init)
    monkeypatch.setattr(app, "_attach_file_log", lambda d: None)
    with pytest.raises(_Stop):
        app._run(qapp, NS(background=False), tmp_path)
    assert order == ["lock", tmp_path]


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
        assert (title, body) == ("ZCL Teleport", "Đã nhận a.txt từ PC-Nha.")
        assert isinstance(icon, QIcon)
        assert not icon.isNull()
        assert not icon.pixmap(32, 32).isNull()
    finally:
        tray.dispose()
