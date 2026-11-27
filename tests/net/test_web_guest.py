"""Kiểm thử tự động cho máy chủ HTTP nội bộ Chế độ khách (teleport.net.web_guest)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from teleport.net.web_guest import (
    MAX_TEXT_BYTES,
    TEXT_RATE_LIMIT_MAX,
    GuestScope,
    WebGuestHttpServer,
)
from teleport.net.web_guest_assets import WEB_GUEST_HTML
from tests.test_http_client import SimpleHttpClient


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
        async with SimpleHttpClient(base_url=f"http://127.0.0.1:{port}") as client:
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
        async with SimpleHttpClient(
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
        async with SimpleHttpClient(base_url=f"http://127.0.0.1:{port}") as client:
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
        async with SimpleHttpClient(base_url=f"http://127.0.0.1:{port}") as client:
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


def test_web_guest_html_size_under_50kb() -> None:
    assert len(WEB_GUEST_HTML.encode("utf-8")) < 50 * 1024


@pytest.mark.asyncio
async def test_web_guest_download_zip_stream(temp_dirs: tuple[Path, Path]) -> None:
    upload_dir, shared_dir = temp_dirs
    file_a = shared_dir / "bao_cao.txt"
    file_a.write_bytes(b"NOI_DUNG_BAO_CAO")

    folder = shared_dir / "anh_du_lich"
    sub = folder / "ngay1"
    sub.mkdir(parents=True)
    (folder / "cover.jpg").write_bytes(b"COVER_BYTES")
    (sub / "pic1.jpg").write_bytes(b"PIC1_BYTES")

    server = WebGuestHttpServer(token="zip_tok", upload_dir=upload_dir)
    server.add_shared_file(file_a)
    dir_id = server.add_shared_file(folder)
    port = await server.start(host="127.0.0.1", port=0)

    try:
        async with SimpleHttpClient(base_url=f"http://127.0.0.1:{port}") as client:
            # 1. Tải toàn bộ danh sách dạng luồng ZIP
            resp = await client.get("/api/download-zip", params={"token": "zip_tok"})
            assert resp.status_code == 200
            assert "application/zip" in resp.headers.get("Content-Type", "")
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                names = set(zf.namelist())
                assert names == {
                    "bao_cao.txt",
                    "anh_du_lich/cover.jpg",
                    "anh_du_lich/ngay1/pic1.jpg",
                }
                assert zf.read("bao_cao.txt") == b"NOI_DUNG_BAO_CAO"
                assert zf.read("anh_du_lich/cover.jpg") == b"COVER_BYTES"
                assert zf.read("anh_du_lich/ngay1/pic1.jpg") == b"PIC1_BYTES"

            # 2. Tải riêng thư mục qua /api/download/<dir_id> cũng trả ZIP
            resp_dir = await client.get(f"/api/download/{dir_id}", params={"token": "zip_tok"})
            assert resp_dir.status_code == 200
            assert "application/zip" in resp_dir.headers.get("Content-Type", "")
            with zipfile.ZipFile(io.BytesIO(resp_dir.content)) as zf_dir:
                assert set(zf_dir.namelist()) == {
                    "anh_du_lich/cover.jpg",
                    "anh_du_lich/ngay1/pic1.jpg",
                }
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_web_guest_upload_with_rel_path_and_traversal_guard(temp_dirs: tuple[Path, Path]) -> None:
    upload_dir, _ = temp_dirs
    server = WebGuestHttpServer(token="rel_tok", upload_dir=upload_dir)
    port = await server.start(host="127.0.0.1", port=0)

    try:
        async with SimpleHttpClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/api/upload",
                params={"name": "img.jpg", "rel_path": "album/2026/img.jpg", "token": "rel_tok"},
                content=b"ALBUM_IMG",
                headers={"Content-Type": "application/octet-stream"},
            )
            assert resp.status_code == 200
            saved = upload_dir / "album" / "2026" / "img.jpg"
            assert saved.is_file()
            assert saved.read_bytes() == b"ALBUM_IMG"

            # Thử path traversal trong rel_path -> đoạn '..' phải bị loại, lưu an toàn trong upload_dir
            resp_evil = await client.post(
                "/api/upload",
                params={"name": "passwd", "rel_path": "../../../etc/passwd", "token": "rel_tok"},
                content=b"SAFE",
                headers={"Content-Type": "application/octet-stream"},
            )
            assert resp_evil.status_code == 200
            assert (upload_dir / "etc" / "passwd").is_file()
            assert not (upload_dir.parent / "etc" / "passwd").exists()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_web_guest_token_brute_force_rate_limit(temp_dirs: tuple[Path, Path]) -> None:
    upload_dir, _ = temp_dirs
    server = WebGuestHttpServer(
        token="real_secret",
        upload_dir=upload_dir,
        max_failed_attempts=5,
        auth_lockout_seconds=60.0,
    )
    port = await server.start(host="127.0.0.1", port=0)

    try:
        async with SimpleHttpClient(base_url=f"http://127.0.0.1:{port}") as client:
            for _ in range(5):
                resp = await client.get("/api/info", params={"token": "bad"})
                assert resp.status_code == 403

            # Lần thứ 6 bị khóa 429 kể cả khi thử gửi đúng token
            locked_resp = await client.get("/api/info", params={"token": "real_secret"})
            assert locked_resp.status_code == 429
    finally:
        await server.stop()


def test_web_guest_html_budget_and_camera_input() -> None:
    """HTML nhúng nằm dưới trần 50 KiB và có nút chụp ảnh trực tiếp từ camera."""
    assert len(WEB_GUEST_HTML.encode("utf-8")) < 50 * 1024
    assert 'capture="environment"' in WEB_GUEST_HTML
    assert 'accept="image/*"' in WEB_GUEST_HTML


@pytest.mark.asyncio
async def test_web_guest_scope_enforcement(temp_dirs: tuple[Path, Path]) -> None:
    """RECEIVE_ONLY chặn tải về (403); SEND_ONLY chặn tải lên & gửi chữ (403)."""
    upload_dir, shared_dir = temp_dirs
    shared = shared_dir / "file.bin"
    shared.write_bytes(b"DATA_OUT")

    recv_srv = WebGuestHttpServer(token="r_tok", upload_dir=upload_dir, scope=GuestScope.RECEIVE_ONLY)
    fid = recv_srv.add_shared_file(shared)
    port_r = await recv_srv.start(host="127.0.0.1", port=0)
    try:
        async with SimpleHttpClient(base_url=f"http://127.0.0.1:{port_r}") as client:
            info = (await client.get("/api/info", params={"token": "r_tok"})).json()
            assert info["scope"] == "receive_only"
            assert info["files"] == []
            assert (await client.get(f"/api/download/{fid}", params={"token": "r_tok"})).status_code == 403
            assert (await client.get("/api/download-zip", params={"token": "r_tok"})).status_code == 403
            up = await client.post(
                "/api/upload",
                params={"name": "up.txt", "token": "r_tok"},
                content=b"IN",
                headers={"Content-Type": "application/octet-stream"},
            )
            assert up.status_code == 200
    finally:
        await recv_srv.stop()

    send_srv = WebGuestHttpServer(token="s_tok", upload_dir=upload_dir, scope=GuestScope.SEND_ONLY)
    fid2 = send_srv.add_shared_file(shared)
    port_s = await send_srv.start(host="127.0.0.1", port=0)
    try:
        async with SimpleHttpClient(base_url=f"http://127.0.0.1:{port_s}") as client:
            info = (await client.get("/api/info", params={"token": "s_tok"})).json()
            assert info["scope"] == "send_only"
            assert len(info["files"]) == 1
            assert (
                await client.post(
                    "/api/upload",
                    params={"name": "nope.txt", "token": "s_tok"},
                    content=b"NOPE",
                    headers={"Content-Type": "application/octet-stream"},
                )
            ).status_code == 403
            assert (
                await client.post(
                    "/api/text",
                    params={"token": "s_tok"},
                    content=b"hi",
                    headers={"Content-Type": "text/plain"},
                )
            ).status_code == 403
            assert (await client.get(f"/api/download/{fid2}", params={"token": "s_tok"})).status_code == 200
    finally:
        await send_srv.stop()


@pytest.mark.asyncio
async def test_web_guest_one_shot_expires_and_stops(temp_dirs: tuple[Path, Path]) -> None:
    """Chế độ 1 lượt cho phép đúng 1 lượt tải lên hoặc tải về rồi trả 410 Gone và tự tắt."""
    upload_dir, shared_dir = temp_dirs
    timeout_called = False

    def on_timeout() -> None:
        nonlocal timeout_called
        timeout_called = True

    srv_up = WebGuestHttpServer(
        token="once_up",
        upload_dir=upload_dir,
        one_shot=True,
        on_session_timeout=on_timeout,
    )
    port_u = await srv_up.start(host="127.0.0.1", port=0)
    try:
        async with SimpleHttpClient(base_url=f"http://127.0.0.1:{port_u}") as client:
            first = await client.post(
                "/api/upload",
                params={"name": "one.bin", "token": "once_up"},
                content=b"ONE_SHOT_UPLOAD",
                headers={"Content-Type": "application/octet-stream"},
            )
            assert first.status_code == 200
            second = await client.post(
                "/api/upload",
                params={"name": "two.bin", "token": "once_up"},
                content=b"SECOND",
                headers={"Content-Type": "application/octet-stream"},
            )
            assert second.status_code == 410
        assert timeout_called
        await asyncio.sleep(0.35)
        assert srv_up._closed
    finally:
        await srv_up.stop()

    doc = shared_dir / "once.txt"
    doc.write_bytes(b"ONE_SHOT_DOWNLOAD")
    srv_dl = WebGuestHttpServer(token="once_dl", upload_dir=upload_dir, one_shot=True)
    fid = srv_dl.add_shared_file(doc)
    port_d = await srv_dl.start(host="127.0.0.1", port=0)
    try:
        async with SimpleHttpClient(base_url=f"http://127.0.0.1:{port_d}") as client:
            dl1 = await client.get(f"/api/download/{fid}", params={"token": "once_dl"})
            assert dl1.status_code == 200
            assert dl1.content == b"ONE_SHOT_DOWNLOAD"
            dl2 = await client.get(f"/api/download/{fid}", params={"token": "once_dl"})
            assert dl2.status_code == 410
    finally:
        await srv_dl.stop()


@pytest.mark.asyncio
async def test_web_guest_two_way_text_sse_limits_and_rate(temp_dirs: tuple[Path, Path]) -> None:
    """Trao đổi văn bản 2 chiều qua /api/text, /api/texts, push_text (SSE) + trần 64 KiB + 429."""
    upload_dir, _ = temp_dirs
    received_from_guest: list[str] = []

    server = WebGuestHttpServer(
        token="txt_tok",
        upload_dir=upload_dir,
        on_text_received=received_from_guest.append,
    )
    port = await server.start(host="127.0.0.1", port=0)

    writer = None
    try:
        # Mở stream SSE nhận sự kiện push_text trực tiếp
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/events?token=txt_tok HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()

        async def _read_until_substr(target: bytes) -> str:
            buf = b""
            for _ in range(40):
                chunk = await asyncio.wait_for(reader.read(4096), timeout=2.0)
                buf += chunk
                if target in buf:
                    return buf.decode("utf-8", errors="replace")
            raise AssertionError(f"Không nhận được {target!r} trong SSE: {buf!r}")

        init_raw = await _read_until_substr(b"event: init")
        assert '"scope": "both"' in init_raw

        async with SimpleHttpClient(base_url=f"http://127.0.0.1:{port}") as client:
            # 1. Host gửi chữ sang điện thoại -> SSE phát event: text_message
            pushed = server.push_text("https://example.org/doc")
            assert pushed["sender"] == "host"
            sse_msg = await _read_until_substr(b"event: text_message")
            assert "https://example.org/doc" in sse_msg

            # 2. Điện thoại gửi chữ JSON & text/plain -> callback & GET /api/texts
            r1 = await client.post(
                "/api/text",
                params={"token": "txt_tok"},
                content=json.dumps({"text": "Xin chào PC"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            assert r1.status_code == 200
            r2 = await client.post(
                "/api/text",
                params={"token": "txt_tok"},
                content="Tin nhắn thuần".encode(),
                headers={"Content-Type": "text/plain"},
            )
            assert r2.status_code == 200
            assert received_from_guest == ["Xin chào PC", "Tin nhắn thuần"]

            hist = (await client.get("/api/texts", params={"token": "txt_tok"})).json()
            assert [m["text"] for m in hist["texts"]] == [
                "https://example.org/doc",
                "Xin chào PC",
                "Tin nhắn thuần",
            ]

            # 3. Văn bản > 64 KiB trả 413
            oversize = await client.post(
                "/api/text",
                params={"token": "txt_tok"},
                content=b"a" * (MAX_TEXT_BYTES + 1),
                headers={"Content-Type": "text/plain"},
            )
            assert oversize.status_code == 413

            # 4. Vượt trần TEXT_RATE_LIMIT_MAX trong 10s -> 429 (đã gửi 3 POST: 2 hợp lệ + 1 quá cỡ)
            remaining = TEXT_RATE_LIMIT_MAX - 3
            for idx in range(remaining):
                ok = await client.post(
                    "/api/text",
                    params={"token": "txt_tok"},
                    content=f"msg-{idx}".encode(),
                    headers={"Content-Type": "text/plain"},
                )
                assert ok.status_code == 200
            limited = await client.post(
                "/api/text",
                params={"token": "txt_tok"},
                content=b"overflow",
                headers={"Content-Type": "text/plain"},
            )
            assert limited.status_code == 429

    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except TimeoutError:
                writer.transport.abort()
            except OSError:
                pass
        await server.stop()


@pytest.mark.asyncio
async def test_web_guest_inline_image_preview_and_one_shot(temp_dirs: tuple[Path, Path]) -> None:
    """`/api/info` báo `is_image`, `?inline=1` trả `inline` và không làm hết hạn phiên 1 lượt."""
    upload_dir, shared_dir = temp_dirs
    img = shared_dir / "photo.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nPNG_DATA")
    txt = shared_dir / "note.txt"
    txt.write_bytes(b"NOTE_DATA")
    sub = shared_dir / "album"
    sub.mkdir()
    (sub / "a.png").write_bytes(b"A")

    server = WebGuestHttpServer(token="img_tok", upload_dir=upload_dir, one_shot=True)
    img_id = server.add_shared_file(img)
    txt_id = server.add_shared_file(txt)
    dir_id = server.add_shared_file(sub)
    port = await server.start(host="127.0.0.1", port=0)

    try:
        async with SimpleHttpClient(base_url=f"http://127.0.0.1:{port}") as client:
            info = (await client.get("/api/info", params={"token": "img_tok"})).json()
            by_id = {f["id"]: f for f in info["files"]}
            assert by_id[img_id]["is_image"] is True
            assert by_id[txt_id]["is_image"] is False
            assert by_id[dir_id]["is_image"] is False

            # Xem trước ảnh với ?inline=1 không làm tiêu thụ phiên 1 lượt
            preview = await client.get(f"/api/download/{img_id}", params={"token": "img_tok", "inline": "1"})
            assert preview.status_code == 200
            assert preview.headers.get("Content-Disposition", "").startswith("inline")
            assert "Cache-Control" in preview.headers
            assert preview.content == b"\x89PNG\r\n\x1a\nPNG_DATA"

            # Tải thường vẫn còn hoạt động (200), sau đó mới chuyển sang 410
            dl = await client.get(f"/api/download/{img_id}", params={"token": "img_tok"})
            assert dl.status_code == 200
            assert dl.headers.get("Content-Disposition", "").startswith("attachment")
            dl_after = await client.get(f"/api/download/{txt_id}", params={"token": "img_tok"})
            assert dl_after.status_code == 410
    finally:
        await server.stop()


def test_web_guest_v2_html_enhancements() -> None:
    """HTML Đợt 2: vẫn < 50 KiB, bỏ `user-scalable=no`, có auto-lang, badge, cancel/retry & preview."""
    assert len(WEB_GUEST_HTML.encode("utf-8")) < 50 * 1024
    assert "user-scalable=no" not in WEB_GUEST_HTML
    assert "navigator.language" in WEB_GUEST_HTML
    assert "badgeDownload" in WEB_GUEST_HTML and "badgeText" in WEB_GUEST_HTML
    assert "batchBox" in WEB_GUEST_HTML
    assert "cancelUploadEntry" in WEB_GUEST_HTML and "retryUploadEntry" in WEB_GUEST_HTML
    assert "file-thumb" in WEB_GUEST_HTML
    assert "soundBtn" in WEB_GUEST_HTML and "playSfx" in WEB_GUEST_HTML


@pytest.mark.asyncio
async def test_web_guest_active_clients_sse_tracking(temp_dirs: tuple[Path, Path]) -> None:
    """SSE `/api/events` cập nhật `active_clients` và gọi `on_clients_changed` khi nối / ngắt."""
    upload_dir, _ = temp_dirs
    counts: list[int] = []
    server = WebGuestHttpServer(
        token="sse_tok",
        upload_dir=upload_dir,
        on_clients_changed=counts.append,
    )
    port = await server.start(host="127.0.0.1", port=0)
    writer = None
    try:
        assert server.active_clients == 0
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        req = f"GET /api/events?token=sse_tok HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode("ascii")
        writer.write(req)
        await writer.drain()
        raw = await asyncio.wait_for(reader.readuntil(b"event: init"), timeout=2.0)
        assert b"200 OK" in raw
        assert server.active_clients == 1
        assert counts == [1]
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        await server.stop()
    assert server.active_clients == 0
    assert counts[-1] == 0
