"""Máy chủ HTTP nội bộ siêu nhẹ phục vụ Chế độ khách qua trình duyệt Web (v1.3.0).

Không sử dụng bất kỳ framework web nặng nề nào bên ngoài (chỉ dùng `asyncio.start_server`),
giữ ứng dụng gọn nhẹ, bảo toàn tính tương thích bản quyền GPLv3.

Bảo mật nhiều lớp:
1. Chỉ phục vụ trong mạng nội bộ (kiểm tra `ipaddress.is_private` hoặc loopback).
2. Yêu cầu mã thông báo bảo mật 1 lần (`token`) so sánh hằng thời gian trên mọi yêu cầu,
   tự động khóa IP tạm thời nếu nhập sai quá nhiều lần.
3. Chống ghi đè và tấn công đường dẫn (Path Traversal) qua `teleport.core.filenames`.
4. Truyền và nhận dữ liệu dạng luồng byte (streaming 64 KiB, hỗ trợ nén ZIP trực tiếp trên
   luồng tải về), bộ nhớ RAM không tăng theo kích thước tệp.
5. Tự động đóng phiên sau thời gian không hoạt động (mặc định 10 phút).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import secrets
import shutil
import struct
import time
import urllib.parse
import zlib
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from teleport.core.filenames import sanitize_filename, sanitize_rel_path
from teleport.net.web_guest_assets import WEB_GUEST_HTML

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("teleport.net.web_guest")

DEFAULT_INACTIVITY_TIMEOUT = 600.0  # 10 phút
CHUNK_SIZE = 64 * 1024  # 64 KiB
MAX_FAILED_AUTH_ATTEMPTS = 5
AUTH_LOCKOUT_SECONDS = 300.0
_MAX_TRACKED_AUTH_IPS = 1024
_ZIP_MAX_UINT32 = 0xFFFFFFFF

MAX_TEXT_BYTES = 64 * 1024  # 64 KiB mỗi tin nhắn văn bản
MAX_TEXT_HISTORY = 50
TEXT_RATE_LIMIT_WINDOW = 10.0
TEXT_RATE_LIMIT_MAX = 20
ONE_SHOT_STOP_DELAY = 0.2


class GuestScope(StrEnum):
    """Phạm vi quyền của phiên chế độ khách."""

    BOTH = "both"
    RECEIVE_ONLY = "receive_only"
    SEND_ONLY = "send_only"


def _dos_timestamp(mtime: float) -> tuple[int, int]:
    """Chuyển epoch timestamp sang cặp (dos_time, dos_date) cho định dạng ZIP."""
    dt = datetime.datetime.fromtimestamp(mtime)
    year = max(1980, dt.year) - 1980
    dos_time = (dt.hour << 11) | (dt.minute << 5) | (dt.second // 2)
    dos_date = (year << 9) | (dt.month << 5) | dt.day
    return dos_time, dos_date


def _collect_zip_files(items: list[tuple[str, Path, bool]]) -> list[tuple[str, Path]]:
    """Thu thập danh sách cặp `(arcname, file_path)` không trùng lặp từ tệp và thư mục chia sẻ."""
    entries: list[tuple[str, Path]] = []
    used_arcnames: set[str] = set()

    def _unique_arcname(candidate: str) -> str:
        norm = candidate.replace("\\", "/").strip("/")
        if norm not in used_arcnames:
            used_arcnames.add(norm)
            return norm
        if "/" in norm:
            parent, leaf = norm.rsplit("/", 1)
            prefix = f"{parent}/"
        else:
            prefix = ""
            leaf = norm
        stem, suffix = os.path.splitext(leaf)
        counter = 1
        while True:
            alt = f"{prefix}{stem} ({counter}){suffix}"
            if alt not in used_arcnames:
                used_arcnames.add(alt)
                return alt
            counter += 1

    for name, path, is_dir in items:
        clean_root = sanitize_filename(name)
        if not is_dir:
            if path.is_file():
                entries.append((_unique_arcname(clean_root), path))
            continue
        if not path.is_dir():
            continue
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs.sort()
            files.sort()
            for fname in files:
                fpath = Path(root) / fname
                if not fpath.is_file():
                    continue
                rel = os.path.relpath(fpath, path).replace("\\", "/")
                clean_rel = sanitize_rel_path(f"{clean_root}/{rel}")
                entries.append((_unique_arcname(clean_rel), fpath))
    return entries


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
        max_failed_attempts: int = MAX_FAILED_AUTH_ATTEMPTS,
        auth_lockout_seconds: float = AUTH_LOCKOUT_SECONDS,
        scope: GuestScope = GuestScope.BOTH,
        one_shot: bool = False,
        on_text_received: Callable[[str], None] | None = None,
        on_clients_changed: Callable[[int], None] | None = None,
    ) -> None:
        self.token = token
        self.upload_dir = upload_dir
        self.device_name = device_name
        self.inactivity_timeout = inactivity_timeout
        self.max_failed_attempts = max(1, max_failed_attempts)
        self.auth_lockout_seconds = max(0.0, auth_lockout_seconds)
        self.scope = GuestScope(scope)
        if type(one_shot) is not bool:
            raise TypeError("one_shot phải là bool")
        self.one_shot = one_shot

        self.on_upload_start = on_upload_start
        self.on_upload_progress = on_upload_progress
        self.on_upload_complete = on_upload_complete
        self.on_session_timeout = on_session_timeout
        self.on_text_received = on_text_received
        self.on_clients_changed = on_clients_changed

        self.shared_files: dict[str, tuple[str, Path, int]] = {}
        self._shared_dirs: set[str] = set()
        self._failed_auth: dict[str, tuple[int, float]] = {}
        self._texts: list[dict[str, Any]] = []
        self._text_rate: dict[str, list[float]] = {}
        self._text_seq: int = 0
        self._consumed: bool = False
        self._server: asyncio.Server | None = None
        self._port: int = 0
        self._last_activity = time.monotonic()
        self._watchdog_task: asyncio.Task[None] | None = None
        self._one_shot_task: asyncio.Task[None] | None = None
        self._active_connections: set[asyncio.StreamWriter] = set()
        self._sse_writers: set[asyncio.StreamWriter] = set()
        self._last_notified_clients: int = 0
        self._closed = False

    @property
    def port(self) -> int:
        return self._port

    @property
    def active_clients(self) -> int:
        return len(self._sse_writers)

    def _notify_clients_changed(self) -> None:
        count = len(self._sse_writers)
        if count == self._last_notified_clients:
            return
        self._last_notified_clients = count
        if self.on_clients_changed:
            with contextlib.suppress(Exception):
                self.on_clients_changed(count)

    def add_shared_file(self, path: Path) -> str:
        """Đưa một tệp hoặc thư mục trên máy tính vào danh sách chia sẻ để điện thoại tải về."""
        if path.is_file():
            is_dir = False
            size = path.stat().st_size
        elif path.is_dir():
            is_dir = True
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        else:
            raise FileNotFoundError(f"Tệp hoặc thư mục không tồn tại: {path}")

        file_id = hashlib.sha256(
            f"{path.name}:{size}:{time.time()}:{secrets.token_hex(4)}".encode()
        ).hexdigest()[:12]
        self.shared_files[file_id] = (path.name, path, size)
        if is_dir:
            self._shared_dirs.add(file_id)
        else:
            self._shared_dirs.discard(file_id)
        self._broadcast_event("files_updated", {"files": self._get_files_meta()})
        return file_id

    add_shared_path = add_shared_file

    def remove_shared_file(self, file_id: str) -> None:
        """Bỏ một tệp hoặc thư mục khỏi danh sách chia sẻ."""
        if file_id in self.shared_files:
            del self.shared_files[file_id]
            self._shared_dirs.discard(file_id)
            self._broadcast_event("files_updated", {"files": self._get_files_meta()})

    def _get_files_meta(self) -> list[dict[str, Any]]:
        if self.scope is GuestScope.RECEIVE_ONLY:
            return []
        return [
            {
                "id": fid,
                "name": name,
                "size": size,
                "is_dir": fid in self._shared_dirs,
                "is_image": bool(
                    fid not in self._shared_dirs
                    and (mimetypes.guess_type(name)[0] or "").startswith("image/")
                ),
            }
            for fid, (name, _, size) in self.shared_files.items()
        ]

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

    def push_text(self, text: str) -> dict[str, Any]:
        """Máy tính gửi văn bản hoặc liên kết sang trình duyệt khách theo thời gian thực."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("văn bản không được rỗng")
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError(f"văn bản vượt quá {MAX_TEXT_BYTES} byte")
        return self._append_text("host", text)

    def _append_text(self, sender: str, text: str) -> dict[str, Any]:
        self._text_seq += 1
        item: dict[str, Any] = {
            "id": str(self._text_seq),
            "sender": sender,
            "text": text,
            "created_at": int(time.time()),
        }
        self._texts.append(item)
        if len(self._texts) > MAX_TEXT_HISTORY:
            del self._texts[: len(self._texts) - MAX_TEXT_HISTORY]
        self._last_activity = time.monotonic()
        self._broadcast_event("text_message", {"item": item})
        return item

    def _consume_one_shot(self) -> None:
        if not self.one_shot or self._consumed:
            return
        self._consumed = True
        self._broadcast_event("session_expired", {"reason": "one_shot"})
        if self.on_session_timeout:
            try:
                self.on_session_timeout()
            except Exception as e:
                log.error("Lỗi callback kết thúc one-shot: %s", e)
        if not self._closed and self._one_shot_task is None:
            self._one_shot_task = asyncio.create_task(self._stop_after_one_shot())

    async def _stop_after_one_shot(self) -> None:
        try:
            await asyncio.sleep(ONE_SHOT_STOP_DELAY)
            await self.stop()
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Dừng máy chủ và đóng toàn bộ kết nối đang mở."""
        if self._closed:
            return
        self._closed = True

        current = asyncio.current_task()
        if self._one_shot_task and self._one_shot_task is not current:
            self._one_shot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._one_shot_task

        if self._watchdog_task and self._watchdog_task is not current:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task

        if self._server:
            self._server.close()

        # Đóng toàn bộ socket khách (gồm kết nối SSE giữ mở) trước wait_closed() của Python 3.12+
        for writer in list(self._active_connections):
            with contextlib.suppress(Exception):
                writer.close()
        for writer in list(self._active_connections):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
        self._active_connections.clear()
        self._sse_writers.clear()
        self._notify_clients_changed()

        if self._server:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)
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
        if dead:
            self._notify_clients_changed()

    async def _close_writer(self, writer: asyncio.StreamWriter, timeout: float = 0.5) -> None:
        """Đóng StreamWriter có giới hạn thời gian; quá hạn thì abort transport."""
        with contextlib.suppress(Exception):
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=timeout)
            except TimeoutError:
                transport = getattr(writer, "transport", None)
                if transport is not None:
                    transport.abort()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._active_connections.add(writer)
        peer = writer.get_extra_info("peername")
        if not peer:
            await self._close_writer(writer)
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

            # Kiểm tra giới hạn sai token & xác thực mã thông báo 1 lần (token)
            req_token = query.get("token", [""])[0]
            if not req_token and "cookie" in headers:
                for cookie in headers["cookie"].split(";"):
                    if "teleport_token=" in cookie:
                        req_token = cookie.split("teleport_token=", 1)[1].strip()

            auth_error = self._verify_client_auth(client_ip, req_token)
            if auth_error is not None:
                if auth_error == 429:
                    log.warning("IP %s bị tạm khóa do nhập sai mã kết nối nhiều lần", client_ip)
                    await self._send_response(
                        writer,
                        429,
                        "text/plain; charset=utf-8",
                        b"Tu choi: Nhap sai ma ket noi qua nhieu lan. Vui long thu lai sau.\n",
                    )
                else:
                    log.warning("Yêu cầu từ %s có mã token không hợp lệ hoặc bị thiếu", client_ip)
                    await self._send_response(
                        writer, 403, "text/plain; charset=utf-8", b"Tu choi: Ma token khong chinh xac\n"
                    )
                return

            # Xử lý các tuyến đường
            if method != "OPTIONS" and self._consumed:
                await self._send_response(
                    writer,
                    410,
                    "application/json; charset=utf-8",
                    json.dumps(
                        {"ok": False, "error": "Phiên chỉ dùng một lần đã kết thúc"},
                        ensure_ascii=False,
                    ).encode("utf-8"),
                )
                return

            if method == "GET":
                await self._handle_get(path, query, headers, writer)
            elif method == "POST":
                await self._handle_post(client_ip, path, query, headers, reader, writer)
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
                await self._close_writer(writer)
                self._active_connections.discard(writer)

    def _verify_client_auth(self, client_ip: str, req_token: str) -> int | None:
        """Kiểm tra khóa IP và so sánh token theo thời gian hằng. Trả mã lỗi HTTP hoặc None nếu đạt."""
        now = time.monotonic()
        entry = self._failed_auth.get(client_ip)
        if entry is not None:
            fails, unlock_at = entry
            if fails >= self.max_failed_attempts:
                if now < unlock_at:
                    return 429
                del self._failed_auth[client_ip]

        token_ok = bool(req_token) and hmac.compare_digest(
            req_token.encode("utf-8", errors="replace"),
            self.token.encode("utf-8"),
        )
        if token_ok:
            self._failed_auth.pop(client_ip, None)
            return None

        prev_fails = self._failed_auth.get(client_ip, (0, 0.0))[0]
        fails = prev_fails + 1
        unlock_at = now + self.auth_lockout_seconds if fails >= self.max_failed_attempts else 0.0
        if len(self._failed_auth) >= _MAX_TRACKED_AUTH_IPS and client_ip not in self._failed_auth:
            expired = [
                ip for ip, (f, u) in self._failed_auth.items() if f >= self.max_failed_attempts and now >= u
            ]
            for ip in expired:
                del self._failed_auth[ip]
            if len(self._failed_auth) >= _MAX_TRACKED_AUTH_IPS:
                oldest = next(iter(self._failed_auth))
                del self._failed_auth[oldest]
        self._failed_auth[client_ip] = (fails, unlock_at)
        return 429 if fails > self.max_failed_attempts else 403

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
                "scope": self.scope.value,
                "one_shot": self.one_shot,
                "texts": list(self._texts),
            }
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            await self._send_response(writer, 200, "application/json; charset=utf-8", body)

        elif path == "/api/texts":
            body = json.dumps({"texts": list(self._texts)}, ensure_ascii=False).encode("utf-8")
            await self._send_response(writer, 200, "application/json; charset=utf-8", body)

        elif path == "/api/download-zip":
            if self.scope is GuestScope.RECEIVE_ONLY:
                await self._send_response(writer, 403, "text/plain; charset=utf-8", b"Forbidden\n")
                return
            if not self.shared_files:
                await self._send_response(writer, 404, "text/plain", b"No Files Shared\n")
                return
            items = [(name, p, fid in self._shared_dirs) for fid, (name, p, _) in self.shared_files.items()]
            entries = _collect_zip_files(items)
            if not entries:
                await self._send_response(writer, 404, "text/plain", b"No Files Available\n")
                return
            await self._serve_zip_stream("teleport-files.zip", entries, writer)
            self._consume_one_shot()

        elif path.startswith("/api/download/"):
            if self.scope is GuestScope.RECEIVE_ONLY:
                await self._send_response(writer, 403, "text/plain; charset=utf-8", b"Forbidden\n")
                return
            # Tải tệp hoặc thư mục xuống điện thoại
            file_id = path.split("/api/download/", 1)[1].strip("/")
            if file_id not in self.shared_files:
                await self._send_response(writer, 404, "text/plain", b"Not Found\n")
                return

            filename, file_path, file_size = self.shared_files[file_id]
            if file_id in self._shared_dirs:
                if not file_path.is_dir():
                    await self._send_response(writer, 404, "text/plain", b"Directory Missing\n")
                    return
                entries = _collect_zip_files([(filename, file_path, True)])
                if not entries:
                    await self._send_response(writer, 404, "text/plain", b"Empty Directory\n")
                    return
                zip_name = f"{sanitize_filename(filename)}.zip"
                await self._serve_zip_stream(zip_name, entries, writer)
                self._consume_one_shot()
                return

            if not file_path.is_file():
                await self._send_response(writer, 404, "text/plain", b"File Missing\n")
                return

            inline_req = query.get("inline", [""])[0] == "1"
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            is_inline_image = inline_req and mime_type.startswith("image/")
            await self._serve_file_download(
                file_path, filename, file_size, headers, writer, inline=is_inline_image
            )
            if not is_inline_image:
                self._consume_one_shot()

        elif path == "/api/events":
            # Server-Sent Events
            self._sse_writers.add(writer)
            self._notify_clients_changed()
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
            init_data = json.dumps(
                {
                    "device_name": self.device_name,
                    "files": self._get_files_meta(),
                    "scope": self.scope.value,
                    "one_shot": self.one_shot,
                    "texts": list(self._texts),
                },
                ensure_ascii=False,
            )
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
        *,
        inline: bool = False,
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
        disp = "inline" if inline else "attachment"
        cd_val = f"{disp}; filename=\"{ascii_filename}\"; filename*=UTF-8''{ascii_filename}"

        status_line = "HTTP/1.1 206 Partial Content" if is_range else "HTTP/1.1 200 OK"
        resp_headers = [
            status_line,
            f"Content-Type: {mime_type}",
            f"Content-Length: {content_length}",
            f"Content-Disposition: {cd_val}",
            "Accept-Ranges: bytes",
        ]
        if inline:
            resp_headers.append("Cache-Control: private, max-age=300")
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
                self._last_activity = time.monotonic()
                remaining -= len(chunk)

    async def _serve_zip_stream(
        self,
        zip_filename: str,
        entries: list[tuple[str, Path]],
        writer: asyncio.StreamWriter,
    ) -> None:
        """Đóng gói và truyền phát nhiều tệp dưới dạng luồng ZIP trực tiếp với RAM O(1)."""
        ascii_filename = urllib.parse.quote(zip_filename)
        cd_val = f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{ascii_filename}"
        resp_headers = [
            "HTTP/1.1 200 OK",
            "Content-Type: application/zip",
            f"Content-Disposition: {cd_val}",
            "Cache-Control: no-store",
            "Connection: close",
            "\r\n",
        ]
        writer.write("\r\n".join(resp_headers).encode("latin1"))
        await writer.drain()

        offset = 0
        central_dir_records: list[bytes] = []

        for arcname, file_path in entries:
            if self._closed or not file_path.is_file():
                continue
            stat_res = file_path.stat()
            if stat_res.st_size > _ZIP_MAX_UINT32 or offset > _ZIP_MAX_UINT32:
                log.warning("Bỏ qua tệp vượt ngưỡng ZIP 32-bit trong luồng tải ZIP: %s", arcname)
                continue

            name_bytes = arcname.replace("\\", "/").encode("utf-8")
            dos_time, dos_date = _dos_timestamp(stat_res.st_mtime)
            header_offset = offset

            local_hdr = (
                struct.pack(
                    "<4sHHHHHIIIHH",
                    b"PK\x03\x04",
                    20,
                    0x0808,
                    0,
                    dos_time,
                    dos_date,
                    0,
                    0,
                    0,
                    len(name_bytes),
                    0,
                )
                + name_bytes
            )
            writer.write(local_hdr)
            await writer.drain()
            offset += len(local_hdr)

            crc = 0
            file_size = 0
            with open(file_path, "rb") as f:
                while not self._closed:
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    crc = zlib.crc32(chunk, crc)
                    file_size += len(chunk)
                    offset += len(chunk)
                    writer.write(chunk)
                    await writer.drain()
                    self._last_activity = time.monotonic()

            crc &= _ZIP_MAX_UINT32
            data_desc = struct.pack("<4sIII", b"PK\x07\x08", crc, file_size, file_size)
            writer.write(data_desc)
            await writer.drain()
            offset += len(data_desc)

            cd_record = (
                struct.pack(
                    "<4sHHHHHHIIIHHHHHII",
                    b"PK\x01\x02",
                    20,
                    20,
                    0x0808,
                    0,
                    dos_time,
                    dos_date,
                    crc,
                    file_size,
                    file_size,
                    len(name_bytes),
                    0,
                    0,
                    0,
                    0,
                    0,
                    header_offset,
                )
                + name_bytes
            )
            central_dir_records.append(cd_record)

        cd_offset = offset
        cd_size = 0
        for rec in central_dir_records:
            writer.write(rec)
            cd_size += len(rec)
        await writer.drain()

        entry_count = len(central_dir_records)
        eocd = struct.pack(
            "<4sHHHHIIH",
            b"PK\x05\x06",
            0,
            0,
            entry_count,
            entry_count,
            cd_size,
            cd_offset,
            0,
        )
        writer.write(eocd)
        await writer.drain()

    def _resolve_upload_target(self, raw_filename: str, raw_rel_path: str) -> tuple[str, Path]:
        """Làm sạch tên tệp / đường dẫn tương đối và chọn đường dẫn đích an toàn không ghi đè."""
        upload_root = self.upload_dir.resolve()
        if raw_rel_path.strip():
            unquoted = urllib.parse.unquote(raw_rel_path).replace("\\", "/")
            clean_rel = sanitize_rel_path(unquoted)
            parts = clean_rel.split("/")
            if len(parts) >= 2:
                target_parent = self.upload_dir.joinpath(*parts[:-1])
                target_parent.mkdir(parents=True, exist_ok=True)
                if not target_parent.resolve().is_relative_to(upload_root):
                    raise ValueError("Đường dẫn lưu trữ vượt ngoài thư mục nhận")
                final_path = self._resolve_unique_path(target_parent / parts[-1])
                display_name = f"{'/'.join(parts[:-1])}/{final_path.name}"
                return display_name, final_path
            filename = parts[0]
        else:
            filename = sanitize_filename(urllib.parse.unquote(raw_filename))

        final_path = self._resolve_unique_path(self.upload_dir / filename)
        return final_path.name, final_path

    def _check_text_rate(self, client_ip: str) -> bool:
        now = time.monotonic()
        cutoff = now - TEXT_RATE_LIMIT_WINDOW
        recent = [ts for ts in self._text_rate.get(client_ip, ()) if ts >= cutoff]
        if len(recent) >= TEXT_RATE_LIMIT_MAX:
            self._text_rate[client_ip] = recent
            return False
        recent.append(now)
        if len(self._text_rate) >= _MAX_TRACKED_AUTH_IPS and client_ip not in self._text_rate:
            oldest = next(iter(self._text_rate))
            del self._text_rate[oldest]
        self._text_rate[client_ip] = recent
        return True

    async def _handle_post_text(
        self,
        client_ip: str,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if not self._check_text_rate(client_ip):
            await self._send_response(
                writer,
                429,
                "application/json; charset=utf-8",
                json.dumps({"ok": False, "error": "Gửi văn bản quá nhanh"}, ensure_ascii=False).encode(
                    "utf-8"
                ),
            )
            return

        try:
            content_length = int(headers.get("content-length", "0"))
        except ValueError:
            await self._send_response(writer, 400, "text/plain", b"Invalid Content-Length\n")
            return
        if content_length <= 0:
            await self._send_response(writer, 400, "text/plain", b"Empty body\n")
            return
        if content_length > MAX_TEXT_BYTES + 1024:
            await self._send_response(
                writer,
                413,
                "application/json; charset=utf-8",
                json.dumps(
                    {"ok": False, "error": "Văn bản vượt quá giới hạn 64 KiB"}, ensure_ascii=False
                ).encode("utf-8"),
            )
            return

        try:
            raw_body = await reader.readexactly(content_length)
        except asyncio.IncompleteReadError:
            await self._send_response(writer, 400, "text/plain", b"Incomplete body\n")
            return

        content_type = headers.get("content-type", "").lower()
        text: str
        if "application/json" in content_type:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                await self._send_response(writer, 400, "text/plain", b"Invalid JSON\n")
                return
            if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                await self._send_response(writer, 400, "text/plain", b"Missing text field\n")
                return
            text = payload["text"]
        else:
            try:
                text = raw_body.decode("utf-8")
            except UnicodeDecodeError:
                await self._send_response(writer, 400, "text/plain", b"Invalid UTF-8\n")
                return

        if not text.strip():
            await self._send_response(writer, 400, "text/plain", b"Blank text\n")
            return
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            await self._send_response(
                writer,
                413,
                "application/json; charset=utf-8",
                json.dumps(
                    {"ok": False, "error": "Văn bản vượt quá giới hạn 64 KiB"}, ensure_ascii=False
                ).encode("utf-8"),
            )
            return

        item = self._append_text("guest", text)
        if self.on_text_received:
            try:
                self.on_text_received(text)
            except Exception as e:
                log.error("Lỗi callback nhận văn bản từ khách: %s", e)

        body = json.dumps({"ok": True, "item": item}, ensure_ascii=False).encode("utf-8")
        await self._send_response(writer, 200, "application/json; charset=utf-8", body)

    async def _handle_post(
        self,
        client_ip: str,
        path: str,
        query: dict[str, list[str]],
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if path in ("/api/upload", "/api/text") and self.scope is GuestScope.SEND_ONLY:
            await self._send_response(writer, 403, "text/plain; charset=utf-8", b"Forbidden\n")
            return
        if path == "/api/text":
            await self._handle_post_text(client_ip, headers, reader, writer)
            return
        if path != "/api/upload":
            await self._send_response(writer, 404, "text/plain", b"Not Found\n")
            return

        raw_filename = query.get("name", [""])[0] or headers.get("x-filename", "")
        if not raw_filename:
            raw_filename = f"guest_upload_{int(time.time())}.bin"
        raw_rel_path = query.get("rel_path", [""])[0] or headers.get("x-rel-path", "")

        try:
            content_length = int(headers.get("content-length", "0"))
        except ValueError:
            await self._send_response(writer, 400, "text/plain", b"Invalid Content-Length\n")
            return
        if content_length < 0:
            await self._send_response(writer, 400, "text/plain", b"Invalid Content-Length\n")
            return

        if content_length > 0:
            try:
                free_bytes = shutil.disk_usage(self.upload_dir).free
            except OSError:
                free_bytes = content_length
            if free_bytes < content_length:
                await self._send_response(
                    writer,
                    507,
                    "application/json; charset=utf-8",
                    json.dumps({"ok": False, "error": "Không đủ chỗ trống trên ổ đĩa"}).encode("utf-8"),
                )
                return

        try:
            display_name, final_path = self._resolve_upload_target(raw_filename, raw_rel_path)
        except ValueError as e:
            await self._send_response(
                writer,
                400,
                "application/json; charset=utf-8",
                json.dumps({"ok": False, "error": str(e)}).encode("utf-8"),
            )
            return

        if self.on_upload_start:
            try:
                self.on_upload_start(display_name, content_length)
            except Exception as e:
                log.error("Lỗi callback bắt đầu tải lên: %s", e)

        # Ghi trực tiếp ra tệp tạm trong cùng thư mục đích
        temp_path = final_path.parent / f".part_{final_path.name}_{int(time.time() * 1000)}"
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
                    self._last_activity = time.monotonic()
                    if remaining != float("inf"):
                        remaining -= len(chunk)

                    if self.on_upload_progress and content_length > 0:
                        with contextlib.suppress(Exception):
                            self.on_upload_progress(display_name, received, content_length)

            final_path = self._resolve_unique_path(final_path)
            temp_path.replace(final_path)

            log.info(
                "Nhận thành công tệp %s (%d bytes, sha256=%s)",
                display_name,
                received,
                hasher.hexdigest(),
            )

            if self.on_upload_complete:
                try:
                    self.on_upload_complete(display_name, received, final_path)
                except Exception as e:
                    log.error("Lỗi callback hoàn tất tải lên: %s", e)

            # Phản hồi JSON 200
            res = {
                "ok": True,
                "filename": final_path.name,
                "rel_path": display_name,
                "size": received,
                "sha256": hasher.hexdigest(),
            }
            body = json.dumps(res, ensure_ascii=False).encode("utf-8")
            await self._send_response(writer, 200, "application/json; charset=utf-8", body)
            self._consume_one_shot()

        except Exception as e:
            if temp_path.exists():
                with contextlib.suppress(Exception):
                    temp_path.unlink()
            log.error("Lỗi nhận tệp %s: %s", display_name, e)
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
            410: "Gone",
            413: "Payload Too Large",
            429: "Too Many Requests",
            500: "Internal Server Error",
            507: "Insufficient Storage",
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
