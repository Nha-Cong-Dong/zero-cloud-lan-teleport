import sys
from pathlib import Path

import pytest

from teleport import paths


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (paths.ENV_HOME, "APPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)


def test_teleport_home_overrides_everything(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
    assert paths.config_dir() == tmp_path


def test_windows_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert paths.config_dir() == tmp_path / "Teleport"


def test_macos_uses_application_support(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert paths.config_dir().parts[-3:] == ("Library", "Application Support", "Teleport")


def test_linux_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    assert paths.config_dir() == Path.home() / ".config" / "teleport"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert paths.config_dir() == tmp_path / "teleport"


# --- P2-7: `default_receive_dir` (plan-p2-7 mục 3) -------------------------------------------------
# Windows: thư mục Downloads thật (Known Folder, seam `_known_folder_downloads`); Linux:
# `XDG_DOWNLOAD_DIR` trong `$XDG_CONFIG_HOME/user-dirs.dirs` (seam `_xdg_user_dirs_path`, xử lý `$HOME/`
# và ngoặc kép); macOS: `~/Downloads`; lỗi → `~/Downloads`. Luôn thêm "Teleport".


def _fallback() -> Path:
    return Path.home() / "Downloads" / "Teleport"


def _forbid(name: str):
    def call(*args, **kwargs):
        raise AssertionError(f"không được gọi {name} trên hệ điều hành này")

    return call


def test_windows_receive_dir_uses_known_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    downloads = tmp_path / "Tải về"
    monkeypatch.setattr(paths, "_known_folder_downloads", lambda: downloads)
    monkeypatch.setattr(paths, "_xdg_user_dirs_path", _forbid("_xdg_user_dirs_path"))
    assert paths.default_receive_dir() == downloads / "Teleport"


@pytest.mark.parametrize("failure", ["tra-none", "nem-oserror"])
def test_windows_receive_dir_falls_back_when_known_folder_fails(monkeypatch, failure):
    """Không đọc được Known Folder → `~/Downloads/Teleport`. Đặc tả không chốt seam báo lỗi bằng cách
    trả None hay ném OSError (lỗi ctypes là OSError), nên kiểm cả hai."""

    def broken():
        if failure == "tra-none":
            return None
        raise OSError("SHGetKnownFolderPath thất bại")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(paths, "_known_folder_downloads", broken)
    assert paths.default_receive_dir() == _fallback()


@pytest.mark.skipif(sys.platform != "win32", reason="Known Folder chỉ có trên Windows")
def test_windows_real_known_folder_is_absolute():
    downloads = paths._known_folder_downloads()
    assert Path(downloads).is_absolute()
    receive = paths.default_receive_dir()
    assert receive.is_absolute() and receive.name == "Teleport"


def _user_dirs(directory: Path, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / "user-dirs.dirs"
    f.write_text(body, encoding="utf-8")
    return f


def test_xdg_user_dirs_path_follows_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    assert paths._xdg_user_dirs_path() == Path.home() / ".config" / "user-dirs.dirs"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert paths._xdg_user_dirs_path() == tmp_path / "cfg" / "user-dirs.dirs"


def test_linux_receive_dir_reads_xdg_download_dir_with_home(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(paths, "_known_folder_downloads", _forbid("_known_folder_downloads"))
    _user_dirs(
        tmp_path / "cfg",
        "# This file is written by xdg-user-dirs-update\n"
        'XDG_DESKTOP_DIR="$HOME/Desktop"\n'
        'XDG_DOWNLOAD_DIR="$HOME/Tải về"\n'
        'XDG_MUSIC_DIR="$HOME/Music"\n',
    )
    assert paths.default_receive_dir() == Path.home() / "Tải về" / "Teleport"


def test_linux_receive_dir_reads_absolute_xdg_download_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    f = _user_dirs(tmp_path / "cfg", 'XDG_DOWNLOAD_DIR="/mnt/data/dl"\n')
    monkeypatch.setattr(paths, "_xdg_user_dirs_path", lambda: f)
    assert paths.default_receive_dir() == Path("/mnt/data/dl") / "Teleport"


@pytest.mark.parametrize(
    "body",
    [None, "", "# chỉ có chú thích\n", 'XDG_DESKTOP_DIR="$HOME/Desktop"\n'],
    ids=["khong-co-file", "rong", "chu-thich", "khong-co-download"],
)
def test_linux_receive_dir_falls_back_without_xdg_download_dir(monkeypatch, tmp_path, body):
    monkeypatch.setattr(sys, "platform", "linux")
    f = tmp_path / "cfg" / "user-dirs.dirs"
    if body is not None:
        _user_dirs(tmp_path / "cfg", body)
    monkeypatch.setattr(paths, "_xdg_user_dirs_path", lambda: f)
    assert paths.default_receive_dir() == _fallback()


def test_macos_receive_dir_is_home_downloads(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(paths, "_known_folder_downloads", _forbid("_known_folder_downloads"))
    monkeypatch.setattr(paths, "_xdg_user_dirs_path", _forbid("_xdg_user_dirs_path"))
    assert paths.default_receive_dir() == _fallback()


def test_old_default_download_dir_is_renamed():
    """plan-p2-7 mục 3: `default_receive_dir` thay `default_download_dir` (không giữ hai tên)."""
    assert not hasattr(paths, "default_download_dir")
