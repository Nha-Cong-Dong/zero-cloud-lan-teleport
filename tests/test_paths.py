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
