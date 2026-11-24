"""Kiểm thử tự động cho WebGuestManager ở tầng ứng dụng (teleport.app.web_guest)."""

from __future__ import annotations

from pathlib import Path

import pytest

from teleport.app.web_guest import WebGuestManager, make_wifi_qr
from teleport.net.web_guest import GuestScope
from tests.test_http_client import SimpleHttpClient


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
    async with SimpleHttpClient() as client:
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
        async with SimpleHttpClient() as client:
            resp = await client.get(f"http://127.0.0.1:{info.port}/api/info?token={info.token}")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["files"]) == 1
            assert data["files"][0]["name"] == "tailieu.pdf"

            # Tải tệp xuống qua HTTP
            file_id = data["files"][0]["id"]
            dl_resp = await client.get(
                f"http://127.0.0.1:{info.port}/api/download/{file_id}?token={info.token}"
            )
            assert dl_resp.status_code == 200
            assert dl_resp.content == b"%PDF-1.4 dummy content"
    finally:
        await manager.stop_session()


@pytest.mark.asyncio
async def test_web_guest_manager_shared_directory(temp_receive_dir: Path, tmp_path: Path) -> None:
    import io
    import zipfile

    folder = tmp_path / "thumuc_chiase"
    folder.mkdir()
    (folder / "ghi_chu.txt").write_bytes(b"THU_MUC_CHIA_SE")

    manager = WebGuestManager(receive_dir=temp_receive_dir)
    info = await manager.start_session(shared_files=[folder], preferred_ip="127.0.0.1")

    try:
        async with SimpleHttpClient() as client:
            resp = await client.get(f"http://127.0.0.1:{info.port}/api/info?token={info.token}")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["files"]) == 1
            assert data["files"][0]["name"] == "thumuc_chiase"
            assert data["files"][0]["is_dir"] is True

            dir_id = data["files"][0]["id"]
            dl_resp = await client.get(
                f"http://127.0.0.1:{info.port}/api/download/{dir_id}?token={info.token}"
            )
            assert dl_resp.status_code == 200
            with zipfile.ZipFile(io.BytesIO(dl_resp.content)) as zf:
                assert zf.namelist() == ["thumuc_chiase/ghi_chu.txt"]
                assert zf.read("thumuc_chiase/ghi_chu.txt") == b"THU_MUC_CHIA_SE"
    finally:
        await manager.stop_session()


@pytest.mark.asyncio
async def test_web_guest_manager_scope_one_shot_and_text(temp_receive_dir: Path) -> None:
    received: list[str] = []
    manager = WebGuestManager(
        receive_dir=temp_receive_dir,
        on_text_received=received.append,
    )
    info = await manager.start_session(
        preferred_ip="127.0.0.1",
        scope=GuestScope.RECEIVE_ONLY,
        one_shot=True,
    )
    assert info.scope is GuestScope.RECEIVE_ONLY
    assert info.one_shot is True

    try:
        item = manager.send_text("Ghi chú từ PC")
        assert item["text"] == "Ghi chú từ PC"

        async with SimpleHttpClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{info.port}/api/text",
                params={"token": info.token},
                content=b"Tin nhan tu dien thoai",
                headers={"Content-Type": "text/plain"},
            )
            assert resp.status_code == 200
            assert received == ["Tin nhan tu dien thoai"]

            texts = (
                await client.get(f"http://127.0.0.1:{info.port}/api/texts", params={"token": info.token})
            ).json()["texts"]
            assert [t["text"] for t in texts] == ["Ghi chú từ PC", "Tin nhan tu dien thoai"]
    finally:
        await manager.stop_session()


def test_make_wifi_qr() -> None:
    payload, matrix, ascii_art, svg = make_wifi_qr("Nha-WiFi", "Pass1234", security="WPA", hidden=True)
    assert payload == "WIFI:T:WPA;S:Nha-WiFi;P:Pass1234;H:true;;"
    assert len(matrix) > 0
    assert ascii_art
    assert svg.startswith("<svg")
