"""Tự động kiểm tra, tải và cài đặt bản cập nhật mới nhất từ GitHub Releases.

Module này thuộc tầng `app`, hoàn toàn không phụ thuộc vào `PySide6` và không làm gián đoạn
hoạt động mạng nội bộ khi máy tính không có kết nối Internet.

Cơ chế hoạt động:
1. Truy vấn kho phát hành chính thức trên GitHub (`Nha-Cong-Dong/zero-cloud-lan-teleport`)
   qua thư viện chuẩn `urllib.request` với thời gian chờ ngắn (mặc định 4 giây).
2. So sánh phiên bản phát hành mới nhất với phiên bản đang chạy (`teleport.__version__`).
3. Khi có bản mới, hỗ trợ tải bộ cài đặt `.exe` về thư mục tạm kèm báo cáo tiến trình
   và kích hoạt bộ cài tự động nâng cấp (`/VERYSILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS`)
   để người dùng không cần mở trình duyệt tải lại thủ công.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from teleport import __version__

log = logging.getLogger(__name__)

DEFAULT_REPO = "Nha-Cong-Dong/zero-cloud-lan-teleport"
USER_AGENT = f"ZCL-Teleport-Updater/{__version__}"
CHUNK_SIZE = 64 * 1024

# Seam cấp module để kiểm thử tự động không cần mở kết nối mạng hay chạy tiến trình thật.
_JsonFetcher = Callable[[str, float], Any]
_BinaryDownloader = Callable[[str, Path, Callable[[int, int], None] | None, float], Path]
_ProcessLauncher = Callable[[list[str]], bool]


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    """Thông tin về phiên bản phát hành trên GitHub Releases."""

    version: str
    current_version: str
    is_newer: bool
    release_url: str
    download_url: str
    asset_name: str
    asset_size: int
    release_notes: str = ""


def parse_version_tuple(version_str: str) -> tuple[int, ...]:
    """Chuyển chuỗi phiên bản (ví dụ 'v1.2.0-alpha' hoặc '0.1.0') thành tuple số nguyên để so sánh."""
    cleaned = version_str.strip().lstrip("vV")
    main_part = re.split(r"[-+a-zA-Z]", cleaned)[0]
    nums: list[int] = []
    for piece in main_part.split("."):
        if piece.isdigit():
            nums.append(int(piece))
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def is_newer_version(latest_tag: str, current_ver: str = __version__) -> bool:
    """Trả về True nếu `latest_tag` mới hơn `current_ver`."""
    if not latest_tag or not current_ver:
        return False
    latest_clean = latest_tag.strip().lstrip("vV")
    current_clean = current_ver.strip().lstrip("vV")
    if latest_clean == current_clean:
        return False

    latest_tuple = parse_version_tuple(latest_clean)
    current_tuple = parse_version_tuple(current_clean)
    if latest_tuple != current_tuple:
        return latest_tuple > current_tuple

    # Nếu cùng nhánh số (ví dụ 1.0.0 vs 1.0.0-alpha), bản chính thức không hậu tố lớn hơn bản thử nghiệm
    latest_has_pre = bool(re.search(r"[-a-zA-Z]", latest_clean))
    current_has_pre = bool(re.search(r"[-a-zA-Z]", current_clean))
    return bool(current_has_pre and not latest_has_pre)


def _default_fetch_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw)


def _default_download_binary(
    url: str,
    dest_path: Path,
    progress_cb: Callable[[int, int], None] | None,
    timeout: float,
) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_part = dest_path.with_suffix(dest_path.suffix + ".part")

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        with open(temp_part, "wb") as out_file:
            while True:
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                out_file.write(chunk)
                downloaded += len(chunk)
                if progress_cb is not None:
                    progress_cb(downloaded, total)

    temp_part.replace(dest_path)
    return dest_path


def _default_launch_installer(cmd: list[str]) -> bool:
    creationflags = 0
    if sys.platform == "win32" and hasattr(subprocess, "DETACHED_PROCESS"):
        creationflags = subprocess.DETACHED_PROCESS
    subprocess.Popen(cmd, close_fds=True, creationflags=creationflags)
    return True


_fetcher: _JsonFetcher = _default_fetch_json
_downloader: _BinaryDownloader = _default_download_binary
_launcher: _ProcessLauncher = _default_launch_installer


def _select_windows_installer_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Ưu tiên chọn bộ cài đặt `.exe` cho Windows từ danh sách tệp đính kèm của bản phát hành."""
    if not assets:
        return None
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.endswith(".exe") and ("setup" in name or "installer" in name or "cli" not in name):
            return asset
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.endswith(".exe"):
            return asset
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.endswith(".zip"):
            return asset
    return None


def check_for_update(
    repo: str = DEFAULT_REPO,
    current_version: str = __version__,
    timeout: float = 4.0,
    fetcher: _JsonFetcher | None = None,
) -> UpdateInfo | None:
    """Kiểm tra phiên bản mới nhất trên GitHub Releases.

    Nếu máy tính đang chỉ dùng mạng nội bộ (không có Internet) hoặc gặp lỗi kết nối,
    hàm trả về `None` một cách an toàn mà không ném ngoại lệ.
    """
    fetch = fetcher or _fetcher
    api_url = f"https://api.github.com/repos/{repo}/releases?per_page=3"
    try:
        data = fetch(api_url, timeout)
        if not isinstance(data, list) or not data:
            return None

        release = next(
            (r for r in data if isinstance(r, dict) and isinstance(r.get("assets"), list) and r["assets"]),
            data[0] if isinstance(data[0], dict) else None,
        )
        if not release:
            return None

        tag_name = str(release.get("tag_name") or "").strip()
        if not tag_name:
            return None

        html_url = str(release.get("html_url") or f"https://github.com/{repo}/releases")
        notes = str(release.get("body") or "").strip()
        assets = release.get("assets")
        asset = _select_windows_installer_asset(assets if isinstance(assets, list) else [])

        if asset:
            asset_name = str(asset.get("name") or "teleport-setup-latest-x64.exe")
            download_url = str(
                asset.get("browser_download_url")
                or f"https://github.com/{repo}/releases/latest/download/{asset_name}"
            )
            asset_size = int(asset.get("size") or 0)
        else:
            asset_name = "teleport-setup-latest-x64.exe"
            download_url = f"https://github.com/{repo}/releases/latest/download/{asset_name}"
            asset_size = 0

        return UpdateInfo(
            version=tag_name,
            current_version=current_version,
            is_newer=is_newer_version(tag_name, current_version),
            release_url=html_url,
            download_url=download_url,
            asset_name=asset_name,
            asset_size=asset_size,
            release_notes=notes,
        )
    except Exception:
        log.debug("khong the kiem tra ban cap nhat tu github", exc_info=True)
        return None


def download_update_installer(
    info: UpdateInfo,
    dest_dir: Path | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
    timeout: float = 30.0,
    downloader: _BinaryDownloader | None = None,
) -> Path:
    """Tải bộ cài đặt của phiên bản mới về thư mục tạm trên máy người dùng."""
    dl = downloader or _downloader
    target_dir = dest_dir or (Path(tempfile.gettempdir()) / "zcl_teleport_updates")
    safe_name = Path(info.asset_name).name or "teleport-setup-latest-x64.exe"
    dest_path = target_dir / safe_name
    return dl(info.download_url, dest_path, progress_cb, timeout)


def apply_update_installer(
    installer_path: Path,
    silent: bool = True,
    launcher: _ProcessLauncher | None = None,
) -> bool:
    """Khởi chạy bộ cài đặt mới để tự động nâng cấp phần mềm và mở lại ứng dụng."""
    if not installer_path.exists() or installer_path.stat().st_size == 0:
        return False

    launch = launcher or _launcher
    cmd = [str(installer_path)]
    if silent and installer_path.suffix.lower() == ".exe":
        cmd.extend(
            [
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART",
                "/CLOSEAPPLICATIONS",
                "/RESTARTAPPLICATIONS",
            ]
        )

    try:
        return launch(cmd)
    except Exception:
        log.debug("khong the khoi chay bo cai cap nhat", exc_info=True)
        return False


def check_in_background(
    on_result: Callable[[UpdateInfo | None], None],
    repo: str = DEFAULT_REPO,
    current_version: str = __version__,
) -> threading.Thread:
    """Chạy kiểm tra bản cập nhật mới trên luồng nền mà không làm trễ lúc mở ứng dụng."""

    def _worker() -> None:
        result = check_for_update(repo=repo, current_version=current_version)
        try:
            on_result(result)
        except Exception:
            log.debug("loi callback kiem tra cap nhat", exc_info=True)

    thread = threading.Thread(target=_worker, name="zcl-teleport-updater", daemon=True)
    thread.start()
    return thread
