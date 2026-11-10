"""Kiểm thử tự động bộ kiểm tra và nâng cấp phiên bản mới (`teleport.app.updater`)."""

from __future__ import annotations

from pathlib import Path

from teleport.app import updater


def test_parse_version_and_comparison():
    assert updater.parse_version_tuple("v1.2.3") == (1, 2, 3)
    assert updater.parse_version_tuple("0.1.0-alpha") == (0, 1, 0)
    assert updater.is_newer_version("v1.1.0", "1.0.0") is True
    assert updater.is_newer_version("v1.0.0", "1.0.0") is False
    assert updater.is_newer_version("v1.0.0", "1.0.0-alpha") is True
    assert updater.is_newer_version("v0.9.0", "1.0.0") is False


def test_check_for_update_selects_windows_installer():
    fake_releases = [
        {
            "tag_name": "v2.0.0",
            "html_url": "https://github.com/Nha-Cong-Dong/zero-cloud-lan-teleport/releases/tag/v2.0.0",
            "body": "Bản cập nhật tốc độ cao",
            "assets": [
                {
                    "name": "teleport-cli-windows-x64.exe",
                    "size": 5_000_000,
                    "browser_download_url": "https://example.com/cli.exe",
                },
                {
                    "name": "teleport-setup-latest-x64.exe",
                    "size": 12_500_000,
                    "browser_download_url": "https://example.com/teleport-setup-latest-x64.exe",
                },
            ],
        }
    ]

    info = updater.check_for_update(
        current_version="1.0.0",
        fetcher=lambda url, timeout: fake_releases,
    )
    assert info is not None
    assert info.version == "v2.0.0"
    assert info.is_newer is True
    assert info.asset_name == "teleport-setup-latest-x64.exe"
    assert info.asset_size == 12_500_000
    assert info.download_url == "https://example.com/teleport-setup-latest-x64.exe"


def test_check_for_update_returns_none_when_offline():
    def _offline_fetcher(url: str, timeout: float):
        raise OSError("Network unreachable")

    info = updater.check_for_update(current_version="1.0.0", fetcher=_offline_fetcher)
    assert info is None


def test_download_and_apply_update_installer(tmp_path: Path):
    info = updater.UpdateInfo(
        version="v2.1.0",
        current_version="1.0.0",
        is_newer=True,
        release_url="https://example.com/releases/v2.1.0",
        download_url="https://example.com/teleport-setup-latest-x64.exe",
        asset_name="teleport-setup-latest-x64.exe",
        asset_size=1024,
    )

    progress_calls: list[tuple[int, int]] = []

    def _fake_downloader(url: str, dest: Path, cb, timeout: float) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"MZ-fake-installer-binary")
        if cb:
            cb(24, 24)
        return dest

    installer_path = updater.download_update_installer(
        info,
        dest_dir=tmp_path,
        progress_cb=lambda done, total: progress_calls.append((done, total)),
        downloader=_fake_downloader,
    )
    assert installer_path.exists()
    assert progress_calls == [(24, 24)]

    launched_cmds: list[list[str]] = []

    def _fake_launcher(cmd: list[str]) -> bool:
        launched_cmds.append(cmd)
        return True

    ok = updater.apply_update_installer(
        installer_path,
        silent=True,
        launcher=_fake_launcher,
    )
    assert ok is True
    assert len(launched_cmds) == 1
    assert launched_cmds[0][0] == str(installer_path)
    assert "/VERYSILENT" in launched_cmds[0]
    assert "/RESTARTAPPLICATIONS" in launched_cmds[0]
