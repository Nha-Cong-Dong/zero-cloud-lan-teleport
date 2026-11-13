"""Kiểm thử tự động cho máy chủ HTTP nội bộ Chế độ khách (teleport.net.web_guest)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from teleport.net.web_guest import WebGuestHttpServer


@pytest.fixture
def temp_dirs(tmp_path: Path) -> tuple[Path, Path]:
    upload_dir = tmp_path / "uploads"
    shared_dir = tmp_path / "shared"
    upload_dir.mkdir()
    shared_dir.mkdir()
    return upload_dir, shared_dir


@pytest.mark.asyncio
async def test_web_guest_start_and_stop(temp_dirs: tuple[Path, Path]) -> None:
    upload_dir, _ = temp_dirs
    server = WebGuestHttpServer(token="secret123", upload_dir=upload_dir)
    port = await server.start(host="127.0.0.1", port=0)
    assert port > 0
    assert server.port == port
    await server.stop()


@pytest.mark.asyncio
async def test_web_guest_token_validation(temp_dirs: tuple[Path, Path]) -> None:
    upload_dir, _ = temp_dirs
    server = WebGuestHttpServer(token="secret123", upload_dir=upload_dir)
    port = await server.start(host="127.0.0.1", port=0)

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            # 1. Yêu cầu thiếu token phải bị từ chối 403
            resp = await client.get("/")
            assert resp.status_code == 403

            # 2. Yêu cầu sai token phải bị từ chối 403
            resp = await client.get("/?token=wrong")
            assert resp.status_code == 403

            # 3. Yêu cầu đúng token phải thành công 200
            resp = await client.get("/?token=secret123")
            assert resp.status_code == 200
            assert "ZCL Teleport" in resp.text
            assert "Set-Cookie" in resp.headers
            assert "teleport_token=secret123" in resp.headers["Set-Cookie"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_web_guest_cookie_auth(temp_dirs: tuple[Path, Path]) -> None:
    upload_dir, _ = temp_dirs
    server = WebGuestHttpServer(token="secret123", upload_dir=upload_dir)
    port = await server.start(host="127.0.0.1", port=0)

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            cookies={"teleport_token": "secret123"},
        ) as client:
            # Gửi request không có query param nhưng có Cookie
            resp = await client.get("/api/info")
            assert resp.status_code == 200
            data = resp.json()
            assert "device_name" in data
            assert data["device_name"] == "Teleport Host"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_web_guest_download_and_range(temp_dirs: tuple[Path, Path]) -> None:
    upload_dir, shared_dir = temp_dirs
    dummy_file = shared_dir / "sample.txt"
    payload = b"HELLO_WORLD_ZCL_TELEPORT"
    dummy_file.write_bytes(payload)

    server = WebGuestHttpServer(token="tok_abc", upload_dir=upload_dir)
    file_id = server.add_shared_file(dummy_file)
    port = await server.start(host="127.0.0.1", port=0)

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            # Tải toàn bộ tệp
            resp = await client.get(f"/api/download/{file_id}", params={"token": "tok_abc"})
            assert resp.status_code == 200
            assert resp.content == payload
            assert resp.headers.get("Content-Length") == str(len(payload))
            assert "attachment;" in resp.headers.get("Content-Disposition", "")

            # Tải ngắt quãng bằng HTTP Range (bytes=0-4 -> 5 byte đầu "HELLO")
            resp_range = await client.get(
                f"/api/download/{file_id}",
                params={"token": "tok_abc"},
                headers={"Range": "bytes=0-4"},
            )
            assert resp_range.status_code == 206
            assert resp_range.content == b"HELLO"
            assert resp_range.headers.get("Content-Length") == "5"
            assert resp_range.headers.get("Content-Range") == f"bytes 0-4/{len(payload)}"

            # Bỏ tệp khỏi danh sách
            server.remove_shared_file(file_id)
            resp_missing = await client.get(f"/api/download/{file_id}", params={"token": "tok_abc"})
            assert resp_missing.status_code == 404
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_web_guest_upload_and_collision(temp_dirs: tuple[Path, Path]) -> None:
    upload_dir, _ = temp_dirs

    progress_events: list[tuple[str, int, int]] = []
    completed_events: list[tuple[str, int, Path]] = []

    def on_progress(name: str, current: int, total: int) -> None:
        progress_events.append((name, current, total))

    def on_complete(name: str, size: int, path: Path) -> None:
        completed_events.append((name, size, path))

    server = WebGuestHttpServer(
        token="upload_key",
        upload_dir=upload_dir,
        on_upload_progress=on_progress,
        on_upload_complete=on_complete,
    )
    port = await server.start(host="127.0.0.1", port=0)

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            payload = b"DULIEU_ANH_CHUP_DIEN_THOAI_1234567890"

            # 1. Tải lên tệp đầu tiên
            resp = await client.post(
                "/api/upload",
                params={"name": "photo.jpg", "token": "upload_key"},
                content=payload,
                headers={"Content-Type": "application/octet-stream"},
            )
            assert resp.status_code == 200
            res_json = resp.json()
            assert res_json["ok"] is True
            assert res_json["filename"] == "photo.jpg"

            target_file = upload_dir / "photo.jpg"
            assert target_file.is_file()
            assert target_file.read_bytes() == payload
            assert len(completed_events) == 1
            assert completed_events[0][0] == "photo.jpg"
            assert completed_events[0][1] == len(payload)

            # 2. Tải lên tệp thứ hai cùng tên -> Tự động thêm số thứ tự, không ghi đè
            resp2 = await client.post(
                "/api/upload",
                params={"name": "photo.jpg", "token": "upload_key"},
                content=payload + b"_V2",
                headers={"Content-Type": "application/octet-stream"},
            )
            assert resp2.status_code == 200
            second_file = upload_dir / "photo (1).jpg"
            assert second_file.is_file()
            assert second_file.read_bytes() == payload + b"_V2"
            # File gốc giữ nguyên
            assert target_file.read_bytes() == payload

            # 3. Thử tải lên tên file có tấn công Path Traversal -> Bị làm sạch
            resp3 = await client.post(
                "/api/upload",
                params={"name": "../../evil.txt", "token": "upload_key"},
                content=b"evil",
                headers={"Content-Type": "application/octet-stream"},
            )
            assert resp3.status_code == 200
            # Phải nằm an toàn trong upload_dir với tên evil.txt
            assert (upload_dir / "evil.txt").is_file()
            assert not (upload_dir.parent / "evil.txt").exists()
    finally:
        await server.stop()
