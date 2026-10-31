"""Khởi động cùng Windows (`teleport.app.autostart`): spec D2 mục 1.2, test 4.1; mục 9.5–9.7 thắng.

Viết từ spec, không từ code:

- Lệnh ghép bằng `subprocess.list2cmdline`: bản đóng gói `[exe, "--background", "--config-dir", D]`;
  bản mã nguồn `[<thư mục của exe>\\pythonw.exe, "-m", "teleport.ui", "--background", "--config-dir", D]`;
  không có `pythonw.exe` → không hỗ trợ. `D` luôn tuyệt đối (`os.path.abspath`, 9.6).
- “Của bản này”: kiểu `REG_SZ` / `REG_EXPAND_SZ` (mở biến môi trường trước), tách tham số như Windows
  (`CommandLineToArgvW`) rồi so **từng tham số** sau `normcase` + `abspath` (9.6); khác → OTHER.
- BLOCKED: giá trị Run của bản này mà `StartupApproved\\Run\\Teleport` có byte đầu lẻ.
- `enable` ghi `REG_SZ`, xóa dấu chặn StartupApproved (chỉ khi đang chặn); `disable` chỉ xóa giá trị
  Run của bản này và xóa luôn StartupApproved; của bản khác thì không đụng.
- Lỗi registry → `AutostartError` với `__cause__` là `OSError`; log chỉ tên loại lỗi (N9).

Registry luôn là bản giả của fixture tự động `fake_registry` (`tests/conftest.py`): test không bao giờ
ghi registry thật. Test so với `CommandLineToArgvW` thật chỉ chạy trên win32 (9.5).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

win32_only = pytest.mark.skipif(
    sys.platform != "win32", reason="CommandLineToArgvW / registry chỉ có trên Windows"
)

# Mã kiểu giá trị (`winreg.REG_*`).
REG_SZ = 1
REG_EXPAND_SZ = 2
REG_BINARY = 3
REG_DWORD = 4

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "Teleport"

ENABLED_MARK = bytes([0x02]) + bytes(11)  # StartupApproved: byte đầu chẵn = cho phép
DISABLED_MARK = bytes([0x03]) + bytes(11)  # byte đầu lẻ = người dùng tắt trong Task Manager


# =================================================================================================
# Phụ trợ
# =================================================================================================


def argv_of(command: str) -> list[str]:
    """Tách dòng lệnh bằng `CommandLineToArgvW` thật của Windows (độc lập với code)."""
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    n = ctypes.c_int(0)
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(n))
    assert argv, "CommandLineToArgvW lỗi"
    try:
        return [argv[i] for i in range(n.value)]
    finally:
        kernel32.LocalFree(ctypes.cast(argv, wintypes.HLOCAL))


@pytest.fixture
def mod():
    from teleport.app import autostart

    return autostart


@pytest.fixture
def frozen(monkeypatch, tmp_path, mod):
    """Bản đóng gói giả: `_frozen()` True, exe ở một thư mục có dấu cách. Trả đường dẫn exe."""
    exe = tmp_path / "Program Files" / "Teleport" / "Teleport.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(mod, "_frozen", lambda: True)
    monkeypatch.setattr(mod, "_executable", lambda: str(exe))
    return exe


@pytest.fixture
def source(monkeypatch, tmp_path, mod):
    """Bản mã nguồn giả: `_frozen()` False, `python.exe` và `pythonw.exe` cùng một thư mục."""
    d = tmp_path / "venv dir" / "Scripts"
    d.mkdir(parents=True)
    (d / "python.exe").write_bytes(b"")
    (d / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(mod, "_frozen", lambda: False)
    monkeypatch.setattr(mod, "_executable", lambda: str(d / "python.exe"))
    return d


@pytest.fixture
def cfg(tmp_path):
    d = tmp_path / "cấu hình A"
    d.mkdir()
    return d


def frozen_args(exe: Path, cfg: Path) -> list[str]:
    return [os.path.abspath(exe), "--background", "--config-dir", os.path.abspath(cfg)]


def run_value(fake):
    return fake.values.get((RUN_KEY, VALUE_NAME))


def approved_value(fake):
    return fake.values.get((STARTUP_APPROVED_KEY, VALUE_NAME))


# =================================================================================================
# Hằng, không hỗ trợ
# =================================================================================================


def test_contract_constants_match_installer(mod):
    """Hợp đồng với bộ cài (architecture §4 “Bộ cài Windows”): tên khóa và tên giá trị."""
    assert mod.RUN_KEY == RUN_KEY
    assert mod.STARTUP_APPROVED_KEY == STARTUP_APPROVED_KEY
    assert mod.VALUE_NAME == VALUE_NAME
    assert {s.value for s in mod.AutostartState} == {"on", "off", "blocked", "other", "unsupported"}
    assert issubclass(mod.AutostartUnsupported, mod.AutostartError)


def test_unsupported_when_registry_none(monkeypatch, frozen, cfg, mod, fake_registry):
    monkeypatch.setattr(mod, "registry", None)
    assert mod.supported() is False
    assert mod.state(cfg) is mod.AutostartState.UNSUPPORTED
    with pytest.raises(mod.AutostartUnsupported):
        mod.enable(cfg)
    assert fake_registry.calls == [], "registry None: không đụng tới registry nào"


def test_unsupported_without_pythonw(monkeypatch, source, cfg, mod, fake_registry):
    (source / "pythonw.exe").unlink()
    assert mod.supported() is False
    assert mod.launch_command(cfg) is None
    assert mod.state(cfg) is mod.AutostartState.UNSUPPORTED
    with pytest.raises(mod.AutostartUnsupported):
        mod.enable(cfg)
    assert fake_registry.writes() == []


def test_enable_unsupported_raises(monkeypatch, frozen, cfg, mod, fake_registry):
    monkeypatch.setattr(mod, "registry", None)
    with pytest.raises(mod.AutostartUnsupported) as info:
        mod.enable(cfg)
    assert isinstance(info.value, mod.AutostartError)
    assert fake_registry.writes() == []


# =================================================================================================
# Lệnh khởi động
# =================================================================================================


def test_launch_command_frozen_exe_background_config_dir(monkeypatch, frozen, tmp_path, mod):
    monkeypatch.chdir(tmp_path)
    rel = Path("cấu hình tương đối")
    rel.mkdir()
    command = mod.launch_command(rel)
    assert command == subprocess.list2cmdline(frozen_args(frozen, tmp_path / rel))
    assert os.path.abspath(rel) in command, "--config-dir luôn tuyệt đối (Q2)"
    assert mod.supported() is True


def test_launch_command_source_uses_pythonw_module(source, cfg, mod):
    want = [
        os.path.join(str(source), "pythonw.exe"),
        "-m",
        "teleport.ui",
        "--background",
        "--config-dir",
        os.path.abspath(cfg),
    ]
    assert mod.launch_command(cfg) == subprocess.list2cmdline(want)
    assert mod.supported() is True


@win32_only
@pytest.mark.parametrize(
    "where",
    ["spaces", "unicode", "root", "trailing_space_dir"],
)
def test_command_roundtrip_spaces_unicode_and_root_dir(monkeypatch, tmp_path, mod, fake_registry, where):
    exe = tmp_path / "Thư mục cài" / "Tele port" / "Teleport.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(mod, "_frozen", lambda: True)
    monkeypatch.setattr(mod, "_executable", lambda: str(exe))
    if where == "root":
        config = Path(tmp_path.anchor)  # `C:\`: dấu `\` cuối đứng ngay trước ngoặc kép khi có quote
    elif where == "unicode":
        config = tmp_path / "Cấu_hình_日本語"
    elif where == "spaces":
        config = tmp_path / "cấu hình có  hai dấu cách"
    else:
        config = tmp_path / "a b" / "c d"
    if where != "root":
        config.mkdir(parents=True)
    command = mod.launch_command(config)
    assert argv_of(command) == frozen_args(exe, config)
    mod.enable(config)
    assert mod.state(config) is mod.AutostartState.ON


# =================================================================================================
# enable / state / disable
# =================================================================================================


def test_enable_writes_reg_sz_run_value(frozen, cfg, mod, fake_registry):
    mod.enable(cfg)
    assert run_value(fake_registry) == (mod.launch_command(cfg), REG_SZ)
    assert mod.state(cfg) is mod.AutostartState.ON
    keys = {c[1] for c in fake_registry.writes()}
    assert keys <= {RUN_KEY, STARTUP_APPROVED_KEY}, f"chỉ ghi vào khóa Run/StartupApproved: {keys}"


@pytest.mark.parametrize(("mark", "kept"), [(DISABLED_MARK, False), (ENABLED_MARK, True)])
def test_enable_clears_disabled_startup_approved_only(frozen, cfg, mod, fake_registry, mark, kept):
    fake_registry.values[(STARTUP_APPROVED_KEY, VALUE_NAME)] = (mark, REG_BINARY)
    mod.enable(cfg)
    assert (approved_value(fake_registry) is not None) is kept
    assert not any(c[0] == "set" and c[1] == STARTUP_APPROVED_KEY for c in fake_registry.calls), (
        "không bao giờ tự ghi StartupApproved (chỉ đọc byte đầu, chỉ xóa)"
    )
    assert mod.state(cfg) is mod.AutostartState.ON


def test_state_off_when_no_run_value(frozen, cfg, mod, fake_registry):
    assert mod.state(cfg) is mod.AutostartState.OFF
    fake_registry.values[(STARTUP_APPROVED_KEY, VALUE_NAME)] = (DISABLED_MARK, REG_BINARY)
    assert mod.state(cfg) is mod.AutostartState.OFF, "chỉ còn dấu StartupApproved mà không có Run → OFF"
    assert fake_registry.writes() == [], "state() chỉ đọc"


def test_state_on_with_surrounding_whitespace(frozen, cfg, mod, fake_registry):
    fake_registry.values[(RUN_KEY, VALUE_NAME)] = ("  " + mod.launch_command(cfg) + " ", REG_SZ)
    assert mod.state(cfg) is mod.AutostartState.ON


@win32_only
def test_state_on_when_value_matches_case_insensitive(frozen, cfg, mod, fake_registry):
    args = frozen_args(frozen, cfg)
    swapped = [args[0].swapcase(), args[1], args[2], args[3].swapcase()]
    fake_registry.values[(RUN_KEY, VALUE_NAME)] = (subprocess.list2cmdline(swapped), REG_SZ)
    assert mod.state(cfg) is mod.AutostartState.ON


@win32_only
def test_state_on_with_equivalent_quoting_per_argument(frozen, cfg, mod, fake_registry):
    """So từng tham số (9.6), không so chuỗi: thêm ngoặc kép thừa quanh cờ vẫn là của bản này."""
    a = frozen_args(frozen, cfg)
    value = f'"{a[0]}" "--background" "--config-dir" "{a[3]}"'
    assert argv_of(value) == a  # tiền đề: Windows tách ra đúng các tham số đó
    fake_registry.values[(RUN_KEY, VALUE_NAME)] = (value, REG_SZ)
    assert mod.state(cfg) is mod.AutostartState.ON


@win32_only
def test_state_on_for_reg_expand_sz_with_env_var(monkeypatch, frozen, cfg, mod, fake_registry):
    monkeypatch.setenv("TELEPORT_TEST_EXE_DIR", str(frozen.parent))
    # Thư mục exe có dấu cách: phần chưa mở biến phải tự có ngoặc kép (như người ta ghi tay).
    value = f'"%TELEPORT_TEST_EXE_DIR%\\Teleport.exe" --background --config-dir "{os.path.abspath(cfg)}"'
    fake_registry.values[(RUN_KEY, VALUE_NAME)] = (value, REG_EXPAND_SZ)
    assert mod.state(cfg) is mod.AutostartState.ON
    fake_registry.values[(RUN_KEY, VALUE_NAME)] = (value, REG_SZ)
    assert mod.state(cfg) is mod.AutostartState.OTHER, "REG_SZ không mở biến môi trường"


@pytest.mark.parametrize("first", [0x01, 0x03, 0x07])
def test_state_blocked_when_startup_approved_odd_byte(frozen, cfg, mod, fake_registry, first):
    mod.enable(cfg)
    fake_registry.values[(STARTUP_APPROVED_KEY, VALUE_NAME)] = (bytes([first]) + bytes(11), REG_BINARY)
    assert mod.state(cfg) is mod.AutostartState.BLOCKED
    fake_registry.values[(STARTUP_APPROVED_KEY, VALUE_NAME)] = (bytes([first - 1]) + bytes(11), REG_BINARY)
    assert mod.state(cfg) is mod.AutostartState.ON, "byte đầu chẵn: không chặn"


@pytest.mark.parametrize(
    "case",
    [
        "other_config_dir",
        "other_exe",
        "extra_arg",
        "missing_background",
        "binary_kind",
        "dword_kind",
        "empty",
    ],
)
def test_state_other_for_different_command_or_kind(frozen, cfg, tmp_path, mod, fake_registry, case):
    a = frozen_args(frozen, cfg)
    own = subprocess.list2cmdline(a)
    values = {
        "other_config_dir": (subprocess.list2cmdline([*a[:3], os.path.abspath(tmp_path / "khác")]), REG_SZ),
        "other_exe": (
            subprocess.list2cmdline([os.path.abspath(tmp_path / "old" / "Teleport.exe"), *a[1:]]),
            REG_SZ,
        ),
        "extra_arg": (own + " --verbose", REG_SZ),
        "missing_background": (subprocess.list2cmdline([a[0], "--config-dir", a[3]]), REG_SZ),
        "binary_kind": (own.encode("utf-16-le"), REG_BINARY),
        "dword_kind": (1, REG_DWORD),
        "empty": ("", REG_SZ),
    }
    fake_registry.values[(RUN_KEY, VALUE_NAME)] = values[case]
    # StartupApproved đang chặn: giá trị của bản khác vẫn là OTHER, không phải BLOCKED.
    fake_registry.values[(STARTUP_APPROVED_KEY, VALUE_NAME)] = (DISABLED_MARK, REG_BINARY)
    assert mod.state(cfg) is mod.AutostartState.OTHER


def test_state_other_when_run_value_of_same_install_but_other_config_dir(frozen, tmp_path, mod):
    """Một giá trị Run cho mọi thư mục cấu hình (9.7): bật ở A thì B thấy OTHER."""
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    mod.enable(a)
    assert mod.state(a) is mod.AutostartState.ON
    assert mod.state(b) is mod.AutostartState.OTHER
    mod.enable(b)
    assert mod.state(a) is mod.AutostartState.OTHER
    assert mod.state(b) is mod.AutostartState.ON


def test_disable_removes_own_value_and_startup_approved(frozen, cfg, mod, fake_registry):
    mod.enable(cfg)
    fake_registry.values[(STARTUP_APPROVED_KEY, VALUE_NAME)] = (DISABLED_MARK, REG_BINARY)
    assert mod.state(cfg) is mod.AutostartState.BLOCKED
    mod.disable(cfg)
    assert run_value(fake_registry) is None
    assert approved_value(fake_registry) is None
    assert mod.state(cfg) is mod.AutostartState.OFF


def test_disable_when_off_does_not_raise(frozen, cfg, mod, fake_registry):
    mod.disable(cfg)
    assert run_value(fake_registry) is None
    assert mod.state(cfg) is mod.AutostartState.OFF


def test_disable_keeps_other_value(frozen, cfg, tmp_path, mod, fake_registry):
    other = (subprocess.list2cmdline([str(tmp_path / "old" / "Teleport.exe"), "--background"]), REG_SZ)
    fake_registry.values[(RUN_KEY, VALUE_NAME)] = other
    fake_registry.values[(STARTUP_APPROVED_KEY, VALUE_NAME)] = (DISABLED_MARK, REG_BINARY)
    mod.disable(cfg)
    assert run_value(fake_registry) == other
    assert approved_value(fake_registry) == (DISABLED_MARK, REG_BINARY), "OTHER: không đụng gì"
    assert fake_registry.writes() == []


# =================================================================================================
# Lỗi, log
# =================================================================================================


@pytest.mark.parametrize(
    ("op", "action"),
    [("get", "state"), ("set", "enable"), ("delete", "disable"), ("get", "disable")],
)
def test_registry_oserror_raises_autostart_error_with_cause(frozen, cfg, mod, fake_registry, op, action):
    if action == "disable":
        mod.enable(cfg)
    error = PermissionError(13, "Access is denied")
    fake_registry.errors[op] = error
    with pytest.raises(mod.AutostartError) as info:
        getattr(mod, action)(cfg)
    assert not isinstance(info.value, mod.AutostartUnsupported)
    assert info.value.__cause__ is error


def test_logs_have_no_paths(frozen, cfg, mod, fake_registry, caplog):
    secret = str(cfg)
    fake_registry.errors["set"] = PermissionError(13, f"Access is denied: {secret}")
    fake_registry.errors["get"] = OSError(5, f"bad {secret}")
    caplog.set_level(logging.DEBUG)
    for action in (mod.enable, mod.state, mod.disable):
        with pytest.raises(mod.AutostartError):
            action(cfg)
    text = "\n".join(r.getMessage() for r in caplog.records) + "\n".join(
        str(r.exc_info[1]) for r in caplog.records if r.exc_info
    )
    assert caplog.records, "lỗi registry phải được ghi log (tên loại lỗi)"
    for needle in (secret, str(frozen), "Access is denied", cfg.name, "Teleport.exe"):
        assert needle not in text, f"log lộ {needle!r}: {text!r}"
    assert "PermissionError" in text or "OSError" in text


def test_autouse_fixture_replaces_real_registry(mod, fake_registry, frozen, cfg):
    assert fake_registry is not None
    assert mod.registry is fake_registry
    if sys.platform == "win32":
        import winreg

        def real_value():
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                    return winreg.QueryValueEx(k, VALUE_NAME)
            except FileNotFoundError:
                return None

        before = real_value()
        mod.enable(cfg)
        assert real_value() == before, "enable() trong test đã ghi registry thật"
        assert run_value(fake_registry) is not None
        assert type(mod.registry).__name__ != "WinRegistry"
