"""Kênh giao tiếp liên tiến trình (IPC) cục bộ giữa CLI và Node Daemon.

Cho phép các lệnh dòng lệnh (như `teleport send`, `teleport list`, `teleport unpair`)
giao tiếp trực tiếp với tiến trình Node đang chạy nền (GUI hoặc daemon) mà không bị
xung đột khóa độc quyền `node.lock`.

Bảo mật cục bộ:
- Chỉ lắng nghe trên giao diện nội bộ `127.0.0.1` (IPv4 loopback).
- Cổng được cấp phát động ngẫu nhiên (port 0).
- Xác thực bằng mã thông báo bảo mật ngẫu nhiên 32-byte (auth token).
- Tệp `ipc.json` lưu trong thư mục cấu hình cá nhân của người dùng,
  được ghi nguyên tử và dọn dẹp sạch sẽ khi tiến trình kết thúc.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from teleport import __version__
from teleport.atomic import write_atomic

if TYPE_CHECKING:
    from teleport.app.node import Node

log = logging.getLogger(__name__)

IPC_INFO_FILE = "ipc.json"
IPC_VERSION = 1
IPC_HANDSHAKE_TIMEOUT = 5.0
IPC_COMMAND_TIMEOUT = 10.0
IPC_MAX_LINE_BYTES = 131_072  # 128 KiB trần cho một thông điệp JSON


@dataclass(frozen=True, slots=True)
class IpcInfo:
    """Thông tin kết nối IPC được lưu trong thư mục cấu hình."""

    port: int
    token: str
    pid: int
    version: str = __version__
    proto: int = IPC_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str | bytes) -> IpcInfo:
        obj = json.loads(data)
        return cls(
            port=int(obj["port"]),
            token=str(obj["token"]),
            pid=int(obj["pid"]),
            version=str(obj.get("version", "")),
            proto=int(obj.get("proto", IPC_VERSION)),
        )


class IpcAuthError(Exception):
    """Mã xác thực IPC không hợp lệ hoặc bị từ chối."""


class IpcProtocolError(Exception):
    """Lỗi định dạng giao thức IPC hoặc thông điệp không hợp lệ."""


class IpcClientError(Exception):
    """Lỗi phía client IPC."""


class IpcServer:
    """Máy chủ IPC chạy trên tiến trình giữ node.lock để nhận lệnh từ CLI."""

    def __init__(self, config_dir: Path, node: Node) -> None:
        self._config_dir = Path(config_dir)
        self._node = node
        self._token = secrets.token_hex(32)
        self._server: asyncio.Server | None = None
        self._info_path = self._config_dir / IPC_INFO_FILE
        self._closing = False
        self._active_connections: set[asyncio.Task[None]] = set()

    @property
    def port(self) -> int | None:
        if self._server is None or not self._server.sockets:
            return None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        """Khởi động máy chủ IPC loopback và ghi tệp thông tin kết nối."""
        if self._server is not None:
            return

        self._server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=0,
        )
        port = self.port
        assert port is not None, "Server socket must have a valid port"

        info = IpcInfo(
            port=port,
            token=self._token,
            pid=os.getpid(),
        )
        write_atomic(self._info_path, info.to_json().encode("utf-8"))
        log.debug("IPC server da lang nghe tai 127.0.0.1:%d (PID %d)", port, info.pid)

    async def close(self) -> None:
        """Đóng máy chủ IPC và dọn dẹp tệp cấu hình."""
        self._closing = True
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

        # Hủy các task đang phục vụ client
        for task in list(self._active_connections):
            task.cancel()
        if self._active_connections:
            await asyncio.gather(*self._active_connections, return_exceptions=True)
            self._active_connections.clear()

        # Dọn dẹp tệp ipc.json
        try:
            if self._info_path.exists():
                self._info_path.unlink(missing_ok=True)
        except OSError as e:
            log.debug("Khong the xoa tep %s: %s", self._info_path, e)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Xử lý một kết nối client từ tiến trình CLI."""
        task = asyncio.current_task()
        if task is not None:
            self._active_connections.add(task)
            task.add_done_callback(self._active_connections.discard)

        try:
            # 1. Xác thực handshake
            line = await asyncio.wait_for(reader.readline(), timeout=IPC_HANDSHAKE_TIMEOUT)
            if not line or len(line) > IPC_MAX_LINE_BYTES:
                return

            try:
                auth_data = json.loads(line.decode("utf-8").strip())
            except Exception:
                return

            if auth_data.get("auth") != self._token:
                log.warning("Tu choi ket noi IPC: sai auth token")
                return

            writer.write(json.dumps({"status": "ok", "msg": "authenticated"}).encode("utf-8") + b"\n")
            await writer.drain()

            # 2. Vòng lặp nhận và xử lý lệnh
            while not self._closing:
                line = await reader.readline()
                if not line:
                    break
                if len(line) > IPC_MAX_LINE_BYTES:
                    writer.write(json.dumps({"error": "message_too_large"}).encode("utf-8") + b"\n")
                    await writer.drain()
                    break

                try:
                    cmd_obj = json.loads(line.decode("utf-8").strip())
                except Exception as e:
                    writer.write(
                        json.dumps({"error": "invalid_json", "detail": str(e)}).encode("utf-8") + b"\n"
                    )
                    await writer.drain()
                    break

                await self._dispatch_command(cmd_obj, writer)

        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            log.debug("Loi xu ly ket noi IPC: %s", e, exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _dispatch_command(self, cmd_obj: dict[str, Any], writer: asyncio.StreamWriter) -> None:
        """Điều phối và thực thi lệnh từ CLI."""
        cmd = cmd_obj.get("command")
        if cmd == "ping":
            resp = {
                "status": "ok",
                "device_id": self._node.identity.device_id,
                "name": self._node.name,
                "version": __version__,
                "pid": os.getpid(),
            }
            writer.write(json.dumps(resp).encode("utf-8") + b"\n")
            await writer.drain()

        elif cmd == "list":
            self_port = self._node.port if isinstance(self._node.port, int) else 0
            self_data = {
                "device_id": str(self._node.identity.device_id),
                "name": str(self._node.name),
                "port": self_port,
            }
            devices_data = []
            for d in self._node.devices():
                devices_data.append(
                    {
                        "device_id": str(d.device_id),
                        "name": str(d.name),
                        "os": d.os,
                        "paired": bool(getattr(d, "paired", False) or getattr(d, "trusted", False)),
                        "online": bool(getattr(d, "online", False)),
                        "addresses": list(getattr(d, "addresses", ())),
                        "port": getattr(d, "port", None),
                    }
                )
            writer.write(
                json.dumps({"status": "ok", "self": self_data, "devices": devices_data}).encode("utf-8")
                + b"\n"
            )
            await writer.drain()

        elif cmd == "unpair":
            target = cmd_obj.get("target")
            if not target:
                writer.write(json.dumps({"error": "missing_target"}).encode("utf-8") + b"\n")
                await writer.drain()
                return

            try:
                removed = await self._node.unpair(target)
                writer.write(
                    json.dumps({"status": "ok", "device_id": removed.device_id, "name": removed.name}).encode(
                        "utf-8"
                    )
                    + b"\n"
                )
            except Exception as e:
                writer.write(json.dumps({"error": "unpair_failed", "detail": str(e)}).encode("utf-8") + b"\n")
            await writer.drain()

        elif cmd == "send":
            paths = [Path(p) for p in cmd_obj.get("paths", [])]
            target_ids = cmd_obj.get("targets", [])
            rate_limit = cmd_obj.get("rate_limit")

            if not paths or not target_ids:
                writer.write(json.dumps({"error": "invalid_send_args"}).encode("utf-8") + b"\n")
                await writer.drain()
                return

            # Thực hiện gửi file qua Node và stream sự kiện về client
            await self._handle_send_request(paths, target_ids, rate_limit, writer)

        else:
            writer.write(json.dumps({"error": "unknown_command", "command": cmd}).encode("utf-8") + b"\n")
            await writer.drain()

    async def _handle_send_request(
        self,
        paths: list[Path],
        target_ids: list[str],
        rate_limit: int | None,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Bắt đầu truyền file qua TransferManager của Node và stream tiến độ tới CLI."""
        from teleport.app.transfers import (
            DataUnreachable,
            FileDone,
            OfferReceived,
            TransferFinished,
            TransferProgress,
            TransferResult,
            TransferResumed,
            TransferStatusChanged,
            TransferUpdate,
        )

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        done_targets: set[str] = set()

        def on_transfer_event(update: TransferUpdate) -> None:
            if isinstance(update, DataUnreachable):
                if update.device_id in target_ids:
                    with contextlib.suppress(asyncio.QueueFull):
                        queue.put_nowait({"event": "unreachable", "device_id": update.device_id})
                return

            if isinstance(update, OfferReceived | FileDone):
                key = update.key
            elif isinstance(
                update,
                TransferStatusChanged | TransferProgress | TransferFinished | TransferResumed,
            ):
                key = update.info.key
            else:
                return

            if key.device_id not in target_ids:
                return

            if isinstance(update, TransferFinished):
                ev_name = "finished"
            elif isinstance(update, TransferProgress):
                ev_name = "progress"
            elif isinstance(update, FileDone):
                ev_name = "file_done"
            elif isinstance(update, OfferReceived):
                ev_name = "offer"
            else:
                ev_name = "status"

            data: dict[str, Any] = {
                "event": ev_name,
                "device_id": key.device_id,
                "transfer_id": key.transfer_id,
            }
            if hasattr(update, "bytes_sent"):
                data["bytes_sent"] = getattr(update, "bytes_sent", 0)
                data["total_bytes"] = getattr(update, "total_bytes", 0)
                data["speed"] = getattr(update, "speed", 0.0)
                data["file_index"] = getattr(update, "file_index", 0)
            if hasattr(update, "result"):
                res = update.result
                data["result"] = res.value if isinstance(res, TransferResult) else str(res)
                data["success"] = res is TransferResult.DONE
                data["reason"] = getattr(update, "reason", None)
                data["by_peer"] = getattr(update, "by_peer", False)

            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(data)

        # Đăng ký listener bọc ngoài để không làm mất callback của Node daemon
        original_callback = self._node.transfers._on_event

        def wrapped_callback(update: TransferUpdate) -> None:
            if original_callback is not None:
                with contextlib.suppress(Exception):
                    original_callback(update)
            on_transfer_event(update)

        self._node.transfers._on_event = wrapped_callback

        try:
            for tid in target_ids:
                try:
                    await self._node.transfers.send(
                        tid,
                        paths,
                        allow_dirs=True,
                    )
                    writer.write(json.dumps({"event": "started", "device_id": tid}).encode("utf-8") + b"\n")
                    await writer.drain()
                except Exception as e:
                    writer.write(
                        json.dumps({"event": "failed_to_start", "device_id": tid, "error": str(e)}).encode(
                            "utf-8"
                        )
                        + b"\n"
                    )
                    await writer.drain()
                    done_targets.add(tid)

            # Stream các sự kiện cho tới khi tất cả các target hoàn thành
            while len(done_targets) < len(target_ids):
                item = await queue.get()
                writer.write(json.dumps(item).encode("utf-8") + b"\n")
                await writer.drain()

                if item.get("event") in ("finished", "settled", "rejected", "cancelled"):
                    done_targets.add(item["device_id"])

            writer.write(json.dumps({"event": "all_done", "status": "ok"}).encode("utf-8") + b"\n")
            await writer.drain()

        except Exception as e:
            writer.write(json.dumps({"event": "error", "message": str(e)}).encode("utf-8") + b"\n")
            await writer.drain()
        finally:
            self._node.transfers._on_event = original_callback


class IpcClient:
    """Client kết nối tới IpcServer của Node daemon từ tiến trình CLI."""

    def __init__(self, info: IpcInfo) -> None:
        self._info = info
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        """Kết nối tới máy chủ IPC và thực hiện xác thực handshake."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", self._info.port),
            timeout=IPC_HANDSHAKE_TIMEOUT,
        )
        self._reader = reader
        self._writer = writer

        # Gửi handshake
        auth_msg = json.dumps({"auth": self._info.token}).encode("utf-8") + b"\n"
        writer.write(auth_msg)
        await writer.drain()

        # Đọc xác nhận
        resp_line = await asyncio.wait_for(reader.readline(), timeout=IPC_HANDSHAKE_TIMEOUT)
        if not resp_line:
            raise IpcAuthError("Server ngat ket noi trong luc handshake")

        resp = json.loads(resp_line.decode("utf-8").strip())
        if resp.get("status") != "ok":
            raise IpcAuthError(f"Handshake that bai: {resp}")

    async def close(self) -> None:
        """Đóng kết nối client."""
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
                await self._writer.wait_closed()
            self._writer = None
            self._reader = None

    async def ping(self) -> dict[str, Any]:
        """Gửi lệnh ping kiểm tra tình trạng daemon."""
        return await self._call({"command": "ping"})

    async def list_devices(self) -> dict[str, Any]:
        """Lấy danh sách thiết bị và thông tin node đang có trong daemon."""
        return await self._call({"command": "list"})

    async def unpair(self, target: str) -> dict[str, Any]:
        """Yêu cầu daemon hủy ghép đôi thiết bị."""
        return await self._call({"command": "unpair", "target": target})

    async def send(
        self,
        paths: Sequence[Path | str],
        targets: list[str],
        rate_limit: int | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> bool:
        """Gửi yêu cầu truyền file qua daemon và stream sự kiện."""
        if self._reader is None or self._writer is None:
            raise IpcClientError("Client chua duoc ket noi")

        cmd = {
            "command": "send",
            "paths": [str(Path(p).resolve()) for p in paths],
            "targets": targets,
            "rate_limit": rate_limit,
        }
        self._writer.write(json.dumps(cmd).encode("utf-8") + b"\n")
        await self._writer.drain()

        success = True
        while True:
            line = await self._reader.readline()
            if not line:
                break
            ev = json.loads(line.decode("utf-8").strip())
            if on_event is not None:
                on_event(ev)

            if ev.get("event") == "all_done":
                break
            if ev.get("event") in ("failed_to_start", "error"):
                success = False

        return success

    async def _call(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Gửi một lệnh request-response đơn giản."""
        if self._reader is None or self._writer is None:
            raise IpcClientError("Client chua duoc ket noi")
        self._writer.write(json.dumps(msg).encode("utf-8") + b"\n")
        await self._writer.drain()

        line = await asyncio.wait_for(self._reader.readline(), timeout=IPC_COMMAND_TIMEOUT)
        if not line:
            raise IpcProtocolError("Server dong ket noi bat ngo")
        return json.loads(line.decode("utf-8").strip())


def read_ipc_info(config_dir: Path) -> IpcInfo | None:
    """Đọc tệp ipc.json từ thư mục cấu hình nếu tồn tại và hợp lệ."""
    path = Path(config_dir) / IPC_INFO_FILE
    if not path.is_file():
        return None
    try:
        content = path.read_bytes()
        return IpcInfo.from_json(content)
    except Exception as e:
        log.debug("Khong the doc %s: %s", path, e)
        return None


async def try_connect_ipc(config_dir: Path) -> IpcClient | None:
    """Thử kết nối tới daemon IPC đang chạy trong config_dir.

    Nếu kết nối hoặc handshake thất bại (daemon đã chết, tệp rác), dọn dẹp và trả về None.
    """
    info = read_ipc_info(config_dir)
    if info is None:
        return None

    client = IpcClient(info)
    try:
        await client.connect()
        # Thử ping xác nhận
        await client.ping()
        return client
    except Exception as e:
        log.debug("Khong the ket noi toi IPC daemon (port %d, PID %d): %s", info.port, info.pid, e)
        await client.close()
        # Dọn dẹp tệp ipc.json mồ côi
        with contextlib.suppress(OSError):
            (Path(config_dir) / IPC_INFO_FILE).unlink(missing_ok=True)
        return None
