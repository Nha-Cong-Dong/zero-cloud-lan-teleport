"""Quản lý phiên Chế độ khách qua Web QR ở tầng ứng dụng (v1.2.0).

Điều phối máy chủ HTTP nội bộ, địa chỉ IP mạng nội bộ, sinh mã QR (ASCII / SVG)
và các thông báo tiến độ gửi/nhận tệp với điện thoại.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from teleport.core.qr import encode_qr, render_ascii, render_svg
from teleport.net.discovery import local_ipv4_addresses
from teleport.net.web_guest import DEFAULT_INACTIVITY_TIMEOUT, WebGuestHttpServer

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

log = logging.getLogger("teleport.app.web_guest")


@dataclass(frozen=True)
class GuestSessionInfo:
    """Thông tin chi tiết về phiên kết nối chế độ khách đang hoạt động."""

    token: str
    port: int
    local_ip: str
    url: str
    qr_matrix: list[list[bool]] = field(repr=False)
    qr_ascii: str = field(repr=False)
    qr_svg: str = field(repr=False)


class WebGuestManager:
    """Quản trị viên điều phối phiên chế độ khách cho ZCL Teleport."""

    def __init__(
        self,
        receive_dir: Path,
        device_name: str = "ZCL Teleport",
        inactivity_timeout: float = DEFAULT_INACTIVITY_TIMEOUT,
        on_upload_start: Callable[[str, int], None] | None = None,
        on_upload_progress: Callable[[str, int, int], None] | None = None,
        on_upload_complete: Callable[[str, int, Path], None] | None = None,
        on_session_timeout: Callable[[], None] | None = None,
    ) -> None:
        self.receive_dir = receive_dir
        self.device_name = device_name
        self.inactivity_timeout = inactivity_timeout

        self.on_upload_start = on_upload_start
        self.on_upload_progress = on_upload_progress
        self.on_upload_complete = on_upload_complete
        self.on_session_timeout = on_session_timeout

        self._server: WebGuestHttpServer | None = None
        self._session_info: GuestSessionInfo | None = None

    @property
    def session_info(self) -> GuestSessionInfo | None:
        return self._session_info

    @property
    def is_running(self) -> bool:
        return self._server is not None

    async def start_session(
        self,
        shared_files: Sequence[Path] | None = None,
        preferred_ip: str | None = None,
        port: int = 0,
    ) -> GuestSessionInfo:
        """Khởi động một phiên chế độ khách mới, sinh mã QR và URL kết nối."""
        if self._server is not None:
            await self.stop_session()

        token = secrets.token_hex(16)
        self.receive_dir.mkdir(parents=True, exist_ok=True)

        self._server = WebGuestHttpServer(
            token=token,
            upload_dir=self.receive_dir,
            device_name=self.device_name,
            inactivity_timeout=self.inactivity_timeout,
            on_upload_start=self.on_upload_start,
            on_upload_progress=self.on_upload_progress,
            on_upload_complete=self.on_upload_complete,
            on_session_timeout=self._handle_timeout,
        )

        if shared_files:
            for p in shared_files:
                if p.is_file():
                    self._server.add_shared_file(p)

        bound_port = await self._server.start(host="0.0.0.0", port=port)

        # Chọn địa chỉ IP mạng nội bộ tốt nhất
        local_ip = self._select_local_ip(preferred_ip)
        url = f"http://{local_ip}:{bound_port}/?token={token}"

        qr_matrix = encode_qr(url, level="L")
        qr_ascii = render_ascii(qr_matrix, border=2)
        qr_svg = render_svg(qr_matrix, border=4, scale=8)

        self._session_info = GuestSessionInfo(
            token=token,
            port=bound_port,
            local_ip=local_ip,
            url=url,
            qr_matrix=qr_matrix,
            qr_ascii=qr_ascii,
            qr_svg=qr_svg,
        )

        log.info("Khởi tạo thành công phiên khách: %s", url)
        return self._session_info

    async def stop_session(self) -> None:
        """Dừng phiên chế độ khách hiện tại."""
        if self._server:
            srv = self._server
            self._server = None
            self._session_info = None
            await srv.stop()
            log.info("Đã đóng phiên khách")

    def add_file_to_share(self, path: Path) -> str:
        """Thêm tệp vào danh sách chia sẻ khi phiên đang chạy."""
        if not self._server:
            raise RuntimeError("Phiên chế độ khách chưa được khởi động")
        return self._server.add_shared_file(path)

    def remove_shared_file(self, file_id: str) -> None:
        """Bỏ tệp khỏi danh sách chia sẻ."""
        if not self._server:
            return
        self._server.remove_shared_file(file_id)

    def _select_local_ip(self, preferred_ip: str | None = None) -> str:
        """Lựa chọn địa chỉ IPv4 nội bộ phù hợp để điện thoại kết nối."""
        if preferred_ip:
            return preferred_ip

        addrs = local_ipv4_addresses()
        for addr in addrs:
            try:
                ip_obj = ipaddress.ip_address(addr)
                if ip_obj.is_private and not ip_obj.is_loopback:
                    return addr
            except ValueError:
                continue

        # Nếu không tìm thấy IP riêng nào thì dùng loopback
        return addrs[0] if addrs else "127.0.0.1"

    def _handle_timeout(self) -> None:
        self._server = None
        self._session_info = None
        if self.on_session_timeout:
            try:
                self.on_session_timeout()
            except Exception as e:
                log.error("Lỗi callback timeout: %s", e)
