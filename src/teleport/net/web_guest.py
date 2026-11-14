"""Máy chủ HTTP nội bộ siêu nhẹ phục vụ Chế độ khách qua trình duyệt Web (v1.2.0).

Không sử dụng bất kỳ framework web nặng nề nào bên ngoài (chỉ dùng `asyncio.start_server`),
giữ ứng dụng gọn nhẹ, bảo toàn tính tương thích bản quyền GPLv3.

Bảo mật nhiều lớp:
1. Chỉ phục vụ trong mạng nội bộ (kiểm tra `ipaddress.is_private` hoặc loopback).
2. Yêu cầu mã thông báo bảo mật 1 lần (`token`) trên mọi yêu cầu.
3. Chống ghi đè và tấn công đường dẫn (Path Traversal) qua `teleport.core.filenames`.
4. Truyền và nhận dữ liệu dạng luồng byte (streaming 64 KiB), bộ nhớ RAM không tăng theo kích thước tệp.
5. Tự động đóng phiên sau thời gian không hoạt động (mặc định 10 phút).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import mimetypes
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from teleport.core.filenames import sanitize_filename
from teleport.net.web_guest_assets import WEB_GUEST_HTML

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("teleport.net.web_guest")

DEFAULT_INACTIVITY_TIMEOUT = 600.0  # 10 phút
CHUNK_SIZE = 64 * 1024  # 64 KiB


class WebGuestHttpServer:
    """Máy chủ HTTP/1.1 bất đồng bộ siêu nhẹ cho chế độ khách điện thoại."""

    def __init__(
        self,
        token: str,
        upload_dir: Path,
        device_name: str = "Teleport Host",
        inactivity_timeout: float = DEFAULT_INACTIVITY_TIMEOUT,
        on_upload_start: Callable[[str, int], None] | None = None,
        on_upload_progress: Callable[[str, int, int], None] | None = None,
        on_upload_complete: Callable[[str, int, Path], None] | None = None,
        on_session_timeout: Callable[[], None] | None = None,
    ) -> None:
        self.token = token
        self.upload_dir = upload_dir
        self.device_name = device_name
        self.inactivity_timeout = inactivity_timeout

        self.on_upload_start = on_upload_start
        self.on_upload_progress = on_upload_progress
        self.on_upload_complete = on_upload_complete
        self.on_session_timeout = on_session_timeout

        self.shared_files: dict[str, tuple[str, Path, int]] = {}
        self._server: asyncio.Server | None = None
        self._port: int = 0
        self._last_activity = time.monotonic()
        self._watchdog_task: asyncio.Task[None] | None = None
        self._active_connections: set[asyncio.StreamWriter] = set()
        self._sse_writers: set[asyncio.StreamWriter] = set()
        self._closed = False

    @property
    def port(self) -> int:
        return self._port

    def add_shared_file(self, path: Path) -> str:
        """Đưa một tệp trên máy tính vào danh sách chia sẻ để điện thoại tải về."""
        if not path.is_file():
            raise FileNotFoundError(f"Tệp không tồn tại: {path}")
        file_id = hashlib.sha256(f"{path.name}:{path.stat().st_size}:{time.time()}".encode()).hexdigest()[:12]
        size = path.stat().st_size
        self.shared_files[file_id] = (path.name, path, size)
        self._broadcast_event("files_updated", {"files": self._get_files_meta()})
        return file_id

    def remove_shared_file(self, file_id: str) -> None:
        """Bỏ một tệp khỏi danh sách chia sẻ."""
        if file_id in self.shared_files:
            del self.shared_files[file_id]
            self._broadcast_event("files_updated", {"files": self._get_files_meta()})

    def _get_files_meta(self) -> list[dict[str, Any]]:
        return [{"id": fid, "name": name, "size": size} for fid, (name, _, size) in self.shared_files.items()]

    async def start(self, host: str = "0.0.0.0", port: int = 0) -> int:
        """Khởi động máy chủ HTTP trên cổng ngẫu nhiên hoặc chỉ định."""
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_server(self._handle_client, host, port)
        sockets = self._server.sockets
        if not sockets:
            raise RuntimeError("Không thể gắn socket cho máy chủ Web khách")
        self._port = sockets[0].getsockname()[1]
        self._last_activity = time.monotonic()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        log.info("Máy chủ Web khách đã mở trên %s:%d (token=%s)", host, self._port, self.token)
        return self._port

    async def stop(self) -> None:
        """Dừng máy chủ và đóng toàn bộ kết nối đang mở."""
        if self._closed:
            return
        self._closed = True

        if self._watchdog_task:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Đóng toàn bộ socket khách
        for writer in list(self._active_connections):
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
        self._active_connections.clear()
        self._sse_writers.clear()
        log.info("Đã tắt máy chủ Web khách")

    async def _watchdog_loop(self) -> None:
        """Bộ đếm thời gian tự động hủy phiên sau 10 phút không hoạt động."""
        while not self._closed:
            await asyncio.sleep(5)
            idle = time.monotonic() - self._last_activity
            if idle >= self.inactivity_timeout:
                log.info("Máy chủ Web khách hết hạn do không có kết nối sau %.0f giây", idle)
                if self.on_session_timeout:
                    try:
                        self.on_session_timeout()
                    except Exception as e:
                        log.error("Lỗi callback hết hạn phiên: %s", e)
                await self.stop()
                break

    def _broadcast_event(self, event: str, data: dict[str, Any]) -> None:
        """Gửi thông báo Server-Sent Events tới các trình duyệt đang mở."""
        if not self._sse_writers:
            return
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
        dead = []
        for writer in self._sse_writers:
            try:
                writer.write(payload)
            except Exception:
                dead.append(writer)
        for w in dead:
            self._sse_writers.discard(w)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._active_connections.add(writer)
        peer = writer.get_extra_info("peername")
        if not peer:
            writer.close()
            await writer.wait_closed()
            self._active_connections.discard(writer)
            return

        client_ip = peer[0]
        try:
            ip_obj = ipaddress.ip_address(client_ip)
            # Chỉ chấp nhận kết nối từ dải địa chỉ nội bộ hoặc loopback
            if not (ip_obj.is_private or ip_obj.is_loopback):
                log.warning("Từ chối kết nối ngoài mạng nội bộ từ IP: %s", client_ip)
                await self._send_response(
                    writer, 403, "text/plain; charset=utf-8", b"Tu choi: Chi cho phep mang noi bo\n"
                )
                return

            self._last_activity = time.monotonic()

            # Đọc dòng yêu cầu đầu tiên (Request line)
            req_line_bytes = await reader.readline()
            if not req_line_bytes:
                return
            req_line = req_line_bytes.decode("latin1", errors="replace").strip()
            parts = req_line.split()
            if len(parts) < 2:
                await self._send_response(writer, 400, "text/plain", b"Bad Request\n")
                return

            method, raw_path = parts[0].upper(), parts[1]
            parsed_url = urllib.parse.urlsplit(raw_path)
            path = parsed_url.path
            query = urllib.parse.parse_qs(parsed_url.query)

            # Đọc toàn bộ tiêu đề (Headers)
            headers: dict[str, str] = {}
            while True:
                line_bytes = await reader.readline()
                if not line_bytes or line_bytes in (b"\r\n", b"\n"):
                    break
                line = line_bytes.decode("latin1", errors="replace").strip()
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            # Xác thực mã thông báo 1 lần (token)
            req_token = query.get("token", [""])[0]
            if not req_token and "cookie" in headers:
                # Tìm trong Cookie
                for cookie in headers["cookie"].split(";"):
                    if "teleport_token=" in cookie:
                        req_token = cookie.split("teleport_token=", 1)[1].strip()

            if req_token != self.token:
                log.warning("Yêu cầu từ %s có mã token không hợp lệ hoặc bị thiếu", client_ip)
                await self._send_response(
                    writer, 403, "text/plain; charset=utf-8", b"Tu choi: Ma token khong chinh xac\n"
                )
                return

            # Xử lý các tuyến đường
            if method == "GET":
                await self._handle_get(path, query, headers, writer)
            elif method == "POST":
                await self._handle_post(path, query, headers, reader, writer)
            elif method == "OPTIONS":
                await self._send_response(
                    writer,
                    200,
                    "text/plain",
                    b"",
                    extra_headers={"Allow": "GET, POST, OPTIONS"},
                )
            else:
                await self._send_response(writer, 405, "text/plain", b"Method Not Allowed\n")

        except Exception as e:
            log.error("Lỗi xử lý yêu cầu HTTP từ %s: %s", client_ip, e)
        finally:
            if writer not in self._sse_writers:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                self._active_connections.discard(writer)

    async def _handle_get(
        self,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        writer: asyncio.StreamWriter,
    ) -> None:
        if path in ("/", "/index.html"):
            # Trả về giao diện web khách
            body = WEB_GUEST_HTML.encode("utf-8")
            cookie_hdr = f"teleport_token={self.token}; Path=/; SameSite=Strict"
            await self._send_response(
                writer,
                200,
                "text/html; charset=utf-8",
                body,
                extra_headers={"Set-Cookie": cookie_hdr},
            )

        elif path == "/api/info":
            # Thông tin phiên và danh sách tệp
            data = {
                "device_name": self.device_name,
                "files": self._get_files_meta(),
            }
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            await self._send_response(writer, 200, "application/json; charset=utf-8", body)

        elif path.startswith("/api/download/"):
            # Tải tệp xuống điện thoại
            file_id = path.split("/api/download/", 1)[1].strip("/")
            if file_id not in self.shared_files:
                await self._send_response(writer, 404, "text/plain", b"Not Found\n")
                return

            filename, file_path, file_size = self.shared_files[file_id]
            if not file_path.is_file():
                await self._send_response(writer, 404, "text/plain", b"File Missing\n")
                return

            await self._serve_file_download(file_path, filename, file_size, headers, writer)

        elif path == "/api/events":
            # Server-Sent Events
            self._sse_writers.add(writer)
            header_lines = [
                "HTTP/1.1 200 OK",
                "Content-Type: text/event-stream",
                "Cache-Control: no-cache",
                "Connection: keep-alive",
                "Access-Control-Allow-Origin: *",
                "\r\n",
            ]
            writer.write("\r\n".join(header_lines).encode("latin1"))
            await writer.drain()

            # Gửi dữ liệu khởi tạo
            init_data = json.dumps({"files": self._get_files_meta()}, ensure_ascii=False)
            writer.write(f"event: init\ndata: {init_data}\n\n".encode())
            await writer.drain()

        else:
            await self._send_response(writer, 404, "text/plain", b"Not Found\n")

    async def _serve_file_download(
        self,
        path: Path,
        filename: str,
        size: int,
        headers: dict[str, str],
        writer: asyncio.StreamWriter,
    ) -> None:
        """Phục vụ tải tệp xuống kèm hỗ trợ HTTP Range ngắt quãng."""
        range_header = headers.get("range", "")
        start = 0
        end = size - 1

        is_range = False
        if range_header.startswith("bytes="):
            try:
                parts = range_header[6:].split("-", 1)
                if parts[0].strip():
                    start = int(parts[0].strip())
                if len(parts) > 1 and parts[1].strip():
                    end = int(parts[1].strip())
                if 0 <= start <= end < size:
                    is_range = True
            except ValueError:
                pass

        content_length = end - start + 1
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        ascii_filename = urllib.parse.quote(filename)
        cd_val = f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{ascii_filename}"

        status_line = "HTTP/1.1 206 Partial Content" if is_range else "HTTP/1.1 200 OK"
        resp_headers = [
            status_line,
            f"Content-Type: {mime_type}",
            f"Content-Length: {content_length}",
            f"Content-Disposition: {cd_val}",
            "Accept-Ranges: bytes",
        ]
        if is_range:
            resp_headers.append(f"Content-Range: bytes {start}-{end}/{size}")
        resp_headers.append("\r\n")

        writer.write("\r\n".join(resp_headers).encode("latin1"))
        await writer.drain()

        # Đọc theo khối 64 KiB và gửi ra socket (backpressure qua writer.drain)
        with open(path, "rb") as f:
            if start > 0:
                f.seek(start)
            remaining = content_length
            while remaining > 0 and not self._closed:
                to_read = min(CHUNK_SIZE, remaining)
                chunk = f.read(to_read)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
                remaining -= len(chunk)

    async def _handle_post(
        self,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if path != "/api/upload":
            await self._send_response(writer, 404, "text/plain", b"Not Found\n")
            return

        raw_filename = query.get("name", [""])[0] or headers.get("x-filename", "")
        if not raw_filename:
            raw_filename = f"guest_upload_{int(time.time())}.bin"

        # Lọc sạch tên file chống tấn công Path Traversal
        filename = sanitize_filename(urllib.parse.unquote(raw_filename))
        content_length = int(headers.get("content-length", "0"))

        if self.on_upload_start:
            try:
                self.on_upload_start(filename, content_length)
            except Exception as e:
                log.error("Lỗi callback bắt đầu tải lên: %s", e)

        # Ghi trực tiếp ra tệp tạm trên đĩa
        temp_path = self.upload_dir / f".part_{filename}_{int(time.time())}"
        hasher = hashlib.sha256()
        received = 0

        try:
            with open(temp_path, "wb") as f:
                remaining = content_length if content_length > 0 else float("inf")
                while remaining > 0:
                    read_size = min(CHUNK_SIZE, int(remaining)) if remaining != float("inf") else CHUNK_SIZE
                    chunk = await reader.read(read_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    received += len(chunk)
                    if remaining != float("inf"):
                        remaining -= len(chunk)

                    if self.on_upload_progress and content_length > 0:
                        with contextlib.suppress(Exception):
                            self.on_upload_progress(filename, received, content_length)

            # Đổi tên tệp hoàn tất, tự động thêm số thứ tự nếu trùng tên
            final_path = self._resolve_unique_path(self.upload_dir / filename)
            temp_path.replace(final_path)

            log.info("Nhận thành công tệp %s (%d bytes, sha256=%s)", filename, received, hasher.hexdigest())

            if self.on_upload_complete:
                try:
                    self.on_upload_complete(filename, received, final_path)
                except Exception as e:
                    log.error("Lỗi callback hoàn tất tải lên: %s", e)

            # Phản hồi JSON 200
            res = {
                "ok": True,
                "filename": filename,
                "size": received,
                "sha256": hasher.hexdigest(),
            }
            body = json.dumps(res, ensure_ascii=False).encode("utf-8")
            await self._send_response(writer, 200, "application/json; charset=utf-8", body)

        except Exception as e:
            if temp_path.exists():
                with contextlib.suppress(Exception):
                    temp_path.unlink()
            log.error("Lỗi nhận tệp %s: %s", filename, e)
            await self._send_response(
                writer,
                500,
                "application/json; charset=utf-8",
                json.dumps({"ok": False, "error": str(e)}).encode("utf-8"),
            )

    def _resolve_unique_path(self, target: Path) -> Path:
        """Đảm bảo tên tệp không ghi đè nếu đã tồn tại."""
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        parent = target.parent
        counter = 1
        while True:
            candidate = parent / f"{stem} ({counter}){suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    async def _send_response(
        self,
        writer: asyncio.StreamWriter,
        status_code: int,
        content_type: str,
        body: bytes,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        reasons = {
            200: "OK",
            206: "Partial Content",
            400: "Bad Request",
            403: "Forbidden",
            404: "Not Found",
            405: "Method Not Allowed",
            500: "Internal Server Error",
        }
        reason = reasons.get(status_code, "Unknown")
        lines = [
            f"HTTP/1.1 {status_code} {reason}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        if extra_headers:
            for k, v in extra_headers.items():
                lines.append(f"{k}: {v}")
        lines.append("\r\n")
        writer.write("\r\n".join(lines).encode("latin1") + body)
        await writer.drain()
