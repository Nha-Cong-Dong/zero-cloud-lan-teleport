"""Kiểm thử tự động cho WebGuestManager ở tầng ứng dụng (teleport.app.web_guest)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from teleport.app.web_guest import WebGuestManager


@pytest.fixture
def temp_receive_dir(tmp_path: Path) -> Path:
    recv = tmp_path / "teleport_recv"
    recv.mkdir()
    return recv


@pytest.mark.asyncio
async def test_web_guest_manager_lifecycle(temp_receive_dir: Path) -> None:
    manager = WebGuestManager(receive_dir=temp_receive_dir, device_name="PC-Nha")
    assert not manager.is_running
    assert manager.session_info is None

    # Khởi động phiên với preferred_ip là 127.0.0.1 để dễ test
    info = await manager.start_session(preferred_ip="127.0.0.1")
    assert manager.is_running
    assert info.port > 0
    assert info.local_ip == "127.0.0.1"
    assert info.token
    assert f":{info.port}/?token={info.token}" in info.url
    assert len(info.qr_matrix) > 0
    assert len(info.qr_ascii) > 0
    assert info.qr_svg.startswith("<svg")

    # Kiểm tra kết nối HTTP qua URL đã sinh
    async with httpx.AsyncClient() as client:
        resp = await client.get(info.url)
        assert resp.status_code == 200
        assert "ZCL Teleport" in resp.text

    # Dừng phiên
    await manager.stop_session()
    assert not manager.is_running
    assert manager.session_info is None


@pytest.mark.asyncio
async def test_web_guest_manager_shared_files(temp_receive_dir: Path, tmp_path: Path) -> None:
    doc = tmp_path / "tailieu.pdf"
    doc.write_bytes(b"%PDF-1.4 dummy content")

    manager = WebGuestManager(receive_dir=temp_receive_dir)
    info = await manager.start_session(shared_files=[doc], preferred_ip="127.0.0.1")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{info.port}/api/info?token={info.token}")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["files"]) == 1
            assert data["files"][0]["name"] == "tailieu.pdf"

            # Tải tệp xuống qua HTTP
            file_id = data["files"][0]["id"]
            dl_resp = await client.get(f"http://127.0.0.1:{info.port}/api/download/{file_id}?token={info.token}")
            assert dl_resp.status_code == 200
            assert dl_resp.content == b"%PDF-1.4 dummy content"
    finally:
        await manager.stop_session()
