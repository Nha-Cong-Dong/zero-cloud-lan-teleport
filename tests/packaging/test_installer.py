"""Test thật trên bộ cài Inno Setup của bản GUI (PR E2, spec `spec-e2-bo-cai.md` việc #7).

Bộ cài lấy từ biến môi trường `TELEPORT_INSTALLER_EXE`; không có biến, hoặc không chạy trên Windows,
thì cả module bị bỏ qua. Chạy riêng:

    $env:TELEPORT_INSTALLER_EXE = "<repo>\\installer\\<file setup>.exe"
    pytest tests/packaging/test_installer.py -m frozen

Các ca:

- (a) `/CURRENTUSER` cài rồi gỡ silent vào `/DIR=<tmp_path>`: cài đủ file, không tạo rule tường lửa,
  không ghi giá trị Run, gỡ xong không còn gì (thư mục cài, khóa `{AppId}_is1`, lối tắt Start Menu),
  không đụng `%APPDATA%\\Teleport` khi silent; gỡ chỉ xóa `Run\\Teleport` (và `StartupApproved`) khi
  dữ liệu trỏ vào đúng `{app}\\Teleport.exe`; cài đè dọn `{app}\\_internal`.
- (b) Chỉ khi tiến trình test đang elevated: `/ALLUSERS` tạo hai rule `Teleport (TCP-In)` /
  `Teleport (UDP-In)` (vào, cho phép, Riêng tư, `LocalSubnet`) cho đúng `{app}\\Teleport.exe`, gỡ xóa
  chúng; `/CURRENTUSER` dù elevated vẫn không tạo rule. Rule luôn được dọn trong `finally`.
- (c) App đã cài đang chạy (`--config-dir <tmp>`) thì gỡ silent không thành công, cài đè silent cũng
  không (spec mục 0: chạy silent thì hủy với mã lỗi).

Test kiểm **trạng thái** (chờ tối đa `STATE_TIMEOUT` giây tới khi `{app}\\Teleport.exe` và khóa
`…_is1` mất/còn), không chỉ mã thoát: bộ gỡ của Inno tự chép sang `%TEMP%` rồi chạy bản chép.

An toàn trên máy phát triển: không bao giờ dùng `/ALLUSERS` khi chưa elevated; bỏ qua nếu khóa
`{AppId}_is1` đã có ở HKCU hoặc HKLM (máy đã cài Teleport thật); giá trị `Run\\Teleport` thật của
người dùng thì để nguyên (ca cần tự ghi giá trị đó sẽ bỏ qua); `%APPDATA%\\Teleport` thật chỉ được
chụp danh sách tên/cỡ/mtime (không chép nội dung, vì trong đó có khóa riêng `device.key`), bộ gỡ lỡ
xóa hay đổi gì thì test hỏng. AppId đọc từ `packaging/inno/teleport.iss`. Mỗi test chỉ chạy bộ gỡ
trong thư mục cài của chính nó (kể cả khi `install_ok` hỏng giữa chừng: dọn bằng
`<thư mục cài>\\unins000.exe`, rồi kiểm khóa `_is1` không còn trỏ vào đó). Đừng chạy module này cùng
lúc với một lần cài thử khác cùng AppId trên máy: khóa `{AppId}_is1` và lối tắt Start Menu là chung,
bộ gỡ của test sẽ xóa cả khóa của lần cài kia.

Ca (c) chạy exe đã cài với `--background --config-dir <tmp>` mới, chưa có `device.crt`: app chỉ dựng
cửa sổ (ẩn) ở màn “Bắt đầu”, không chạy node, không mở socket, không quảng bá mDNS (docstring
`teleport.ui.app`), nên không có hộp hỏi tường lửa. Ca phiên khác (`SessionId`) và Restart Manager
khi cài đè bằng tay không kiểm ở đây.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

if sys.platform == "win32":
    import winreg

ENV_INSTALLER = "TELEPORT_INSTALLER_EXE"
INSTALLER = os.environ.get(ENV_INSTALLER, "").strip()

if sys.platform != "win32":
    pytest.skip("bộ cài Inno Setup chỉ chạy trên Windows", allow_module_level=True)
if not INSTALLER:
    pytest.skip(f"không có {ENV_INSTALLER}: chưa có bộ cài để kiểm", allow_module_level=True)

pytestmark = [pytest.mark.frozen, pytest.mark.integration, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[2]
ISS = REPO / "packaging" / "inno" / "teleport.iss"
SETUP_TIMEOUT = 300.0  # giải nén lzma2/max cả bản build
STATE_TIMEOUT = 60.0  # spec mục 4 điểm 9
STABLE_FOR = 10.0  # ca "không thành công": trạng thái phải giữ nguyên chừng này sau khi bộ gỡ đã xong
RULE_NAMES = ("Teleport (TCP-In)", "Teleport (UDP-In)")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
RUN_VALUE = "Teleport"


def _read_app_id() -> str:
    if not ISS.is_file():
        pytest.skip(f"chưa có {ISS}: không biết AppId", allow_module_level=True)
    m = re.search(r"(?im)^\s*AppId\s*=\s*(\S+)\s*$", ISS.read_text(encoding="utf-8-sig"))
    if m is None:
        pytest.fail(f"{ISS} không có AppId", pytrace=False)
    # Trong .iss, `{{` là ký tự `{` (escape hằng); khóa gỡ cài đặt là `<AppId>_is1`.
    return m.group(1).strip('"').replace("{{", "{")


APP_ID = _read_app_id()
IS1 = f"{UNINSTALL_KEY}\\{APP_ID}_is1"


# ---- tiện ích -------------------------------------------------------------------------------


def wait_for(predicate: Callable[[], bool], what: str, timeout: float = STATE_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"quá {timeout:.0f} s vẫn chưa: {what}")
        time.sleep(0.25)


def stays(predicate: Callable[[], bool], what: str, duration: float = STABLE_FOR) -> None:
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        assert predicate(), f"không giữ được: {what}"
        time.sleep(0.25)


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except OSError:
        return False


def _views() -> tuple[int, ...]:
    return (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)


def key_exists(root: int, path: str) -> bool:
    for view in _views():
        try:
            winreg.CloseKey(winreg.OpenKey(root, path, 0, winreg.KEY_READ | view))
        except OSError:
            continue
        return True
    return False


def query_value(root: int, path: str, name: str) -> object | None:
    for view in _views():
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_READ | view) as key:
                return winreg.QueryValueEx(key, name)[0]
        except OSError:
            continue
    return None


def set_value(path: str, name: str, kind: int, data: object) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, kind, data)


def delete_value(path: str, name: str) -> None:
    with (
        contextlib.suppress(OSError),
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key,
    ):
        winreg.DeleteValue(key, name)


def installed_in(root: int) -> bool:
    return key_exists(root, IS1)


def installed_anywhere() -> bool:
    return installed_in(winreg.HKEY_CURRENT_USER) or installed_in(winreg.HKEY_LOCAL_MACHINE)


def read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-4000:]
    except OSError:
        return "(không có log)"


def run_setup(app_dir: Path, log: Path, *mode: str) -> int:
    assert "/ALLUSERS" not in mode or is_elevated(), "không bao giờ /ALLUSERS khi chưa elevated"
    p = subprocess.run(
        [
            INSTALLER,
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            *mode,
            f"/DIR={app_dir}",
            f"/LOG={log}",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=SETUP_TIMEOUT,
        check=False,
    )
    return p.returncode


def uninstaller_of(root: int) -> Path:
    raw = query_value(root, IS1, "UninstallString")
    assert isinstance(raw, str) and raw, f"khóa {IS1} không có UninstallString"
    return Path(raw.strip().strip('"'))


def run_uninstall(uninstaller: Path, log: Path) -> int:
    p = subprocess.run(
        [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=SETUP_TIMEOUT,
        check=False,
    )
    return p.returncode


def uninstaller_copies_running() -> bool:
    """Bản chép của bộ gỡ (`%TEMP%\\_iu*.tmp`) còn chạy không."""
    p = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq _iu*"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "_iu" in p.stdout.lower()


def firewall_rules_for(program: Path) -> list[dict[str, str]]:
    """Mọi rule tường lửa có chương trình là `program` (đọc bằng cmdlet, không phụ thuộc ngôn ngữ)."""
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8;"
        "$p = $env:TP_PROGRAM;"
        "$out = @(Get-NetFirewallApplicationFilter -Program $p -ErrorAction SilentlyContinue |"
        " ForEach-Object {"
        " $r = $_ | Get-NetFirewallRule;"
        " [pscustomobject]@{"
        "  Name = [string]$r.DisplayName; Direction = [string]$r.Direction; Action = [string]$r.Action;"
        "  Profile = [string]$r.Profile; Enabled = [string]$r.Enabled;"
        "  Edge = [string]$r.EdgeTraversalPolicy;"
        "  Protocol = [string]($r | Get-NetFirewallPortFilter).Protocol;"
        "  RemoteAddress = [string](($r | Get-NetFirewallAddressFilter).RemoteAddress -join ',');"
        "  Program = [string]$_.Program } });"
        "ConvertTo-Json -InputObject $out -Compress"
    )
    env = dict(os.environ, TP_PROGRAM=str(program))
    p = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert p.returncode == 0, p.stderr.decode("utf-8", "replace")
    text = p.stdout.decode("utf-8-sig", "replace").strip()
    data = json.loads(text) if text else []
    return data if isinstance(data, list) else [data]


def delete_rules_for(program: Path) -> None:
    """Dọn rule của `program` (chỉ dùng khi đã elevated); luôn kèm `program=` như spec mục 4 điểm 3."""
    for name in (*RULE_NAMES, "all"):
        subprocess.run(
            ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}", f"program={program}"],
            capture_output=True,
            timeout=60,
            check=False,
        )


def start_menu_dirs() -> list[Path]:
    dirs = [Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"]
    if programdata := os.environ.get("PROGRAMDATA"):
        dirs.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return dirs


def shortcuts(dirs: list[Path]) -> set[Path]:
    found: set[Path] = set()
    for d in dirs:
        if d.is_dir():
            found.update(p for p in d.rglob("*.lnk") if "teleport" in str(p.relative_to(d)).lower())
    return found


def desktop_shortcuts() -> set[Path]:
    dirs = [Path(os.environ["USERPROFILE"]) / "Desktop"]
    if public := os.environ.get("PUBLIC"):
        dirs.append(Path(public) / "Desktop")
    return {p for d in dirs if d.is_dir() for p in d.glob("*.lnk") if "teleport" in p.name.lower()}


# ---- fixture --------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def not_already_installed() -> None:
    if installed_anywhere():
        pytest.skip(f"khóa {APP_ID}_is1 đã có ở HKCU/HKLM: máy đang cài Teleport, không thử cài đè")


def file_stats(root: Path) -> dict[str, tuple[int, int]]:
    """Tên tương đối → (cỡ, mtime_ns) của mọi file dưới `root`; không đọc nội dung."""
    if not root.is_dir():
        return {}
    stats: dict[str, tuple[int, int]] = {}
    for p in root.rglob("*"):
        with contextlib.suppress(OSError):
            st = p.stat()
            stats[str(p.relative_to(root))] = (st.st_size if p.is_file() else -1, st.st_mtime_ns)
    return stats


def _volatile(rel: str) -> bool:
    """File mà một Teleport thật đang chạy trên máy có thể tự đổi (log, khóa): không tính là “đổi”."""
    parts = Path(rel).parts
    return (bool(parts) and parts[0].lower() == "logs") or rel.lower().endswith(".lock")


@pytest.fixture
def user_data_guard() -> Iterator[Path]:
    """Chụp tên/cỡ/mtime trong `%APPDATA%\\Teleport` thật (không chép nội dung); bộ gỡ đụng thì hỏng."""
    real = Path(os.environ["APPDATA"]) / "Teleport"
    before = file_stats(real)
    yield real
    after = file_stats(real)
    lost = sorted(set(before) - set(after))
    changed = sorted(
        rel
        for rel in set(before) & set(after)
        if before[rel][0] >= 0 and before[rel] != after[rel] and not _volatile(rel)
    )
    assert not lost, f"bộ gỡ silent đã xóa dữ liệu người dùng trong {real}: {lost[:10]}"
    assert not changed, f"dữ liệu người dùng trong {real} bị đổi: {changed[:10]}"


@pytest.fixture
def run_value_free() -> Iterator[None]:
    """Ca tự ghi `HKCU\\…\\Run\\Teleport`: bỏ qua nếu người dùng đã có giá trị đó; dọn sau test."""
    if query_value(winreg.HKEY_CURRENT_USER, RUN_KEY, RUN_VALUE) is not None:
        pytest.skip("HKCU Run\\Teleport đã có (của người dùng): không ghi đè")
    if query_value(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_KEY, RUN_VALUE) is not None:
        pytest.skip("HKCU StartupApproved\\Run\\Teleport đã có (của người dùng): không ghi đè")
    yield
    delete_value(RUN_KEY, RUN_VALUE)
    delete_value(STARTUP_APPROVED_KEY, RUN_VALUE)


class Install:
    """Một lần cài vào `tmp_path/app` và dọn chắc chắn ở cuối (gỡ, rồi xóa rule nếu elevated)."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.app = tmp_path / "app"
        self.exe = self.app / "Teleport.exe"
        self.root = winreg.HKEY_CURRENT_USER
        self.uninstaller: Path | None = None
        self._n = 0

    def log(self, kind: str) -> Path:
        self._n += 1
        return self.tmp / f"{kind}-{self._n}.log"

    def install(self, *mode: str) -> tuple[int, Path]:
        log = self.log("setup")
        if "/ALLUSERS" in mode:
            self.root = winreg.HKEY_LOCAL_MACHINE
        return run_setup(self.app, log, *mode), log

    def install_ok(self, *mode: str) -> None:
        code, log = self.install(*mode)
        assert code == 0, f"cài thất bại, mã {code}\n{read_log(log)}"
        wait_for(lambda: self.exe.is_file() and installed_in(self.root), "đã cài xong")
        # Bộ gỡ của chính lần cài này (không bao giờ chạy bộ gỡ khóa _is1 trỏ tới, phòng khi có lần cài
        # khác cùng AppId chạy song song trên máy và ghi đè khóa).
        uninstaller = uninstaller_of(self.root)
        assert uninstaller.parent.resolve() == self.app.resolve(), (
            f"khóa {IS1} trỏ tới {uninstaller}, không phải lần cài này (có lần cài khác chạy song song?)"
        )
        self.uninstaller = uninstaller

    def uninstall(self) -> tuple[int, Path]:
        log = self.log("uninstall")
        assert self.uninstaller is not None, "chưa cài"
        return run_uninstall(self.uninstaller, log), log

    def uninstall_ok(self) -> None:
        _, log = self.uninstall()
        wait_for(
            lambda: not self.exe.exists() and not installed_in(self.root),
            f"đã gỡ xong (Teleport.exe và khóa _is1 mất)\n{read_log(log)}",
        )

    def registered_here(self) -> list[str]:
        """Các gốc (HKCU/HKLM) có khóa `_is1` mà `UninstallString` nằm trong thư mục cài này."""
        found = []
        for name, root in (("HKCU", winreg.HKEY_CURRENT_USER), ("HKLM", winreg.HKEY_LOCAL_MACHINE)):
            raw = query_value(root, IS1, "UninstallString")
            if not isinstance(raw, str) or not raw.strip():
                continue
            path = Path(raw.strip().strip('"'))
            if os.path.normcase(os.path.realpath(path.parent)) == os.path.normcase(
                os.path.realpath(self.app)
            ):
                found.append(name)
        return found

    def cleanup(self) -> None:
        """Gỡ bản cài của test kể cả khi `install_ok` hỏng trước lúc gán `uninstaller`.

        Không gỡ thì khóa `_is1` và lối tắt nằm lại, mọi lần chạy sau bị `not_already_installed` bỏ qua
        cả module; nên sau khi dọn còn khóa `_is1` trỏ vào thư mục này thì báo lỗi.
        """
        uninstaller = self.uninstaller
        if uninstaller is None and (self.app / "unins000.exe").is_file():
            uninstaller = self.app / "unins000.exe"
        if uninstaller is not None and uninstaller.is_file():
            try:
                run_uninstall(uninstaller, self.log("cleanup-uninstall"))
                wait_for(lambda: not self.registered_here(), "dọn: gỡ xong")
            except (AssertionError, OSError, subprocess.TimeoutExpired):
                pass
        if is_elevated():
            delete_rules_for(self.exe)
        left = self.registered_here()
        assert not left, (
            f"dọn xong mà khóa {IS1} ở {left} vẫn trỏ vào {self.app}: tự gỡ bản cài này rồi mới chạy lại"
        )


@pytest.fixture
def inst(tmp_path: Path) -> Iterator[Install]:
    i = Install(tmp_path)
    try:
        yield i
    finally:
        i.cleanup()


def start_app(inst: Install) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    for name in ("TELEPORT_HOME", "QT_QPA_PLATFORM", "QT_PLUGIN_PATH"):
        env.pop(name, None)
    proc = subprocess.Popen(
        [str(inst.exe), "--background", "--config-dir", str(inst.tmp / "cfg")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    time.sleep(3.0)
    assert proc.poll() is None, f"app đã cài thoát ngay, mã {proc.returncode}"
    return proc


def stop_app(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=30)


# ---- (a) /CURRENTUSER cài và gỡ silent ------------------------------------------------------


def test_currentuser_silent_install_and_uninstall_leave_nothing(inst: Install, user_data_guard: Path) -> None:
    run_before = query_value(winreg.HKEY_CURRENT_USER, RUN_KEY, RUN_VALUE)
    menu_before = shortcuts(start_menu_dirs())
    desktop_before = desktop_shortcuts()

    inst.install_ok("/CURRENTUSER")

    assert installed_in(winreg.HKEY_CURRENT_USER)
    assert not installed_in(winreg.HKEY_LOCAL_MACHINE), "/CURRENTUSER không được ghi HKLM"
    assert uninstaller_of(winreg.HKEY_CURRENT_USER).parent.resolve() == inst.app.resolve()
    assert (inst.app / "_internal").is_dir()
    assert (inst.app / "LICENSE").is_file()
    assert not (inst.app / "DOC-TRUOC.txt").exists(), "DOC-TRUOC.txt không áp cho bản cài (D4)"
    licenses = inst.app / "licenses"
    assert licenses.is_dir() and any("inno" in p.name.lower() for p in licenses.iterdir()), (
        "giấy phép Inno Setup phải nằm trong {app}\\licenses\\ (D11)"
    )
    new_menu = shortcuts(start_menu_dirs()) - menu_before
    assert new_menu, "phải có lối tắt Start Menu (D5)"
    assert desktop_shortcuts() == desktop_before, "lối tắt Desktop là task mặc định tắt (D5)"
    assert query_value(winreg.HKEY_CURRENT_USER, RUN_KEY, RUN_VALUE) == run_before, (
        "bộ cài không được ghi Run (không có task autostart, D7)"
    )
    assert firewall_rules_for(inst.exe) == [], "/CURRENTUSER không tạo rule tường lửa (D2)"

    # Bản đã cài chạy được (đủ _internal): --selftest không mở mạng, không ghi gì.
    cfg = inst.tmp / "cfg-selftest"
    p = subprocess.run(
        [str(inst.exe), "--selftest", "--config-dir", str(cfg)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert p.returncode == 0

    inst.uninstall_ok()

    wait_for(lambda: not inst.app.exists(), "thư mục cài bị xóa (dirifempty {app})")
    assert not (shortcuts(start_menu_dirs()) & new_menu), "gỡ phải xóa lối tắt Start Menu"
    assert query_value(winreg.HKEY_CURRENT_USER, RUN_KEY, RUN_VALUE) == run_before
    # user_data_guard kiểm %APPDATA%\Teleport còn nguyên (silent luôn giữ dữ liệu, D9).


def test_reinstall_over_existing_clears_stale_internal_files(inst: Install) -> None:
    inst.install_ok("/CURRENTUSER")
    stale = inst.app / "_internal" / "file-cu-tu-ban-truoc.dll"
    stale.write_bytes(b"x")

    inst.install_ok("/CURRENTUSER")

    assert not stale.exists(), "[InstallDelete] {app}\\_internal phải dọn file của bản cũ (D4)"
    assert inst.exe.is_file()
    inst.uninstall_ok()


def test_uninstall_removes_run_value_pointing_to_this_install(inst: Install, run_value_free: None) -> None:
    inst.install_ok("/CURRENTUSER")
    # Khác hoa thường và có ngoặc kép: so không phân biệt hoa thường, bỏ ngoặc kép (spec mục 4 điểm 5).
    data = f'"{str(inst.exe).upper()}" --background'
    set_value(RUN_KEY, RUN_VALUE, winreg.REG_SZ, data)
    set_value(STARTUP_APPROVED_KEY, RUN_VALUE, winreg.REG_BINARY, b"\x02" + b"\x00" * 11)

    inst.uninstall_ok()

    assert query_value(winreg.HKEY_CURRENT_USER, RUN_KEY, RUN_VALUE) is None
    assert query_value(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_KEY, RUN_VALUE) is None


def test_uninstall_keeps_run_value_pointing_elsewhere(inst: Install, run_value_free: None) -> None:
    inst.install_ok("/CURRENTUSER")
    other = inst.tmp / "ban-khac" / "Teleport.exe"
    data = f'"{other}" --background'
    set_value(RUN_KEY, RUN_VALUE, winreg.REG_SZ, data)
    approved = b"\x02" + b"\x00" * 11
    set_value(STARTUP_APPROVED_KEY, RUN_VALUE, winreg.REG_BINARY, approved)

    inst.uninstall_ok()

    assert query_value(winreg.HKEY_CURRENT_USER, RUN_KEY, RUN_VALUE) == data
    assert query_value(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_KEY, RUN_VALUE) == approved


def test_uninstall_keeps_run_value_of_install_dir_prefix_sibling(inst: Install, run_value_free: None) -> None:
    """`<tmp>\\app2\\Teleport.exe` không chứa `<tmp>\\app\\Teleport.exe`: phải giữ."""
    inst.install_ok("/CURRENTUSER")
    data = f'"{inst.tmp / "app2" / "Teleport.exe"}" --background'
    set_value(RUN_KEY, RUN_VALUE, winreg.REG_SZ, data)

    inst.uninstall_ok()

    assert query_value(winreg.HKEY_CURRENT_USER, RUN_KEY, RUN_VALUE) == data


# ---- (c) app đang chạy ----------------------------------------------------------------------


def test_silent_uninstall_fails_while_installed_app_is_running(inst: Install) -> None:
    inst.install_ok("/CURRENTUSER")
    proc = start_app(inst)
    try:
        code, log = inst.uninstall()
        wait_for(lambda: not uninstaller_copies_running(), "bản chép của bộ gỡ thoát")
        stays(
            lambda: installed_in(inst.root) and inst.exe.is_file() and (inst.app / "_internal").is_dir(),
            f"gỡ silent khi app đang chạy phải hủy (mã thoát {code})\n{read_log(log)}",
        )
        assert proc.poll() is None, "bộ gỡ không được đóng app"
    finally:
        stop_app(proc)

    inst.uninstall_ok()  # thoát app rồi thì gỡ được


def test_silent_reinstall_fails_while_installed_app_is_running(inst: Install) -> None:
    inst.install_ok("/CURRENTUSER")
    proc = start_app(inst)
    try:
        code, log = inst.install("/CURRENTUSER")
        assert code != 0, f"cài đè silent khi app đang chạy phải hủy với mã lỗi\n{read_log(log)}"
        assert proc.poll() is None, "cài silent không được đóng app (PrepareToInstall hủy trước)"
        assert installed_in(inst.root) and inst.exe.is_file()
    finally:
        stop_app(proc)

    inst.uninstall_ok()


# ---- (b) chế độ admin: rule tường lửa -------------------------------------------------------

needs_admin = pytest.mark.skipif(not is_elevated(), reason="cần tiến trình test elevated (CI)")


def _rule_problems(rules: list[dict[str, str]], exe: Path) -> list[str]:
    problems = []
    by_name = {r["Name"]: r for r in rules}
    for name, proto in zip(RULE_NAMES, ("TCP", "UDP"), strict=True):
        r = by_name.get(name)
        if r is None:
            problems.append(f"thiếu rule {name}")
            continue
        expected = {
            "Direction": "Inbound",
            "Action": "Allow",
            "Profile": "Private",
            "Enabled": "True",
            "Edge": "Block",
            "Protocol": proto,
            "RemoteAddress": "LocalSubnet",
        }
        for field, value in expected.items():
            if r.get(field, "").lower() != value.lower():
                problems.append(f"{name}: {field} = {r.get(field)!r}, cần {value!r}")
        if os.path.normcase(r["Program"]) != os.path.normcase(str(exe)):
            problems.append(f"{name}: Program = {r['Program']!r}")
    return problems


@needs_admin
def test_admin_install_creates_private_rules_and_uninstall_removes_them(inst: Install) -> None:
    inst.install_ok("/ALLUSERS")

    assert installed_in(winreg.HKEY_LOCAL_MACHINE)
    rules = firewall_rules_for(inst.exe)
    assert _rule_problems(rules, inst.exe) == [], rules
    assert len([r for r in rules if r["Name"] in RULE_NAMES]) == 2, "mỗi tên đúng một rule"

    inst.uninstall_ok()

    assert firewall_rules_for(inst.exe) == [], "gỡ (admin) phải xóa mọi rule vào của {app}\\Teleport.exe"


@needs_admin
def test_admin_reinstall_does_not_duplicate_rules(inst: Install) -> None:
    inst.install_ok("/ALLUSERS")
    inst.install_ok("/ALLUSERS")

    rules = [r for r in firewall_rules_for(inst.exe) if r["Name"] in RULE_NAMES]
    assert sorted(r["Name"] for r in rules) == sorted(RULE_NAMES), "cài lại phải xóa rule cũ trước khi tạo"
    inst.uninstall_ok()


@needs_admin
def test_currentuser_install_creates_no_rules_even_when_elevated(inst: Install) -> None:
    inst.install_ok("/CURRENTUSER")

    assert not installed_in(winreg.HKEY_LOCAL_MACHINE)
    assert firewall_rules_for(inst.exe) == [], "rule chỉ tạo khi IsAdminInstallMode (D6)"
    inst.uninstall_ok()
