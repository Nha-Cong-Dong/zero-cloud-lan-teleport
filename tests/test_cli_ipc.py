"""Kiểm thử tích hợp các lệnh CLI (`list`, `unpair`, `send`) tương tác với daemon IPC."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from teleport.app.ipc import IpcServer
from teleport.cli import run_list, run_send
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore


@pytest.fixture
def mock_node():
    node = MagicMock()
    node.identity.device_id = "this_device_12345"
    node.name = "MyComputer"
    node.port = 47474

    peer = MagicMock()
    peer.device_id = "target_peer_67890"
    peer.name = "FriendLaptop"
    peer.os = "linux"
    peer.online = True
    peer.paired = True
    peer.trusted = True
    peer.addresses = ("192.168.1.50",)
    peer.port = 47474
    peer.last_address = ("192.168.1.50", 47474)

    node.devices.return_value = [peer]
    node.unpair = AsyncMock(return_value=peer)
    return node


@pytest.mark.asyncio
async def test_cli_list_via_ipc(tmp_path: Path, mock_node):
    """Kiểm tra `teleport list` tự động truy vấn daemon IPC và xuất kết quả."""
    server = IpcServer(tmp_path, mock_node)
    await server.start()

    try:
        # 1. Chế độ text
        args = argparse.Namespace(config_dir=tmp_path, json=False)
        out = io.StringIO()
        err = io.StringIO()

        code = await run_list(args, out, err)
        assert code == 0
        output_text = out.getvalue()
        assert "MyComputer" in output_text
        assert "FriendLaptop" in output_text
        assert "192.168.1.50:47474" in output_text

        # 2. Chế độ json
        args_json = argparse.Namespace(config_dir=tmp_path, json=True)
        out_json = io.StringIO()
        err_json = io.StringIO()

        code_json = await run_list(args_json, out_json, err_json)
        assert code_json == 0
        data = json.loads(out_json.getvalue())
        assert data["self"]["name"] == "MyComputer"
        assert len(data["devices"]) == 1
        assert data["devices"][0]["device_id"] == "target_peer_67890"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_ipc_unpair_command(tmp_path: Path, mock_node):
    """Kiểm tra gọi lệnh unpair trực tiếp qua IpcClient tới daemon."""
    server = IpcServer(tmp_path, mock_node)
    await server.start()

    try:
        from teleport.app.ipc import try_connect_ipc

        client = await try_connect_ipc(tmp_path)
        assert client is not None
        try:
            res = await client.unpair("target_peer_67890")
            assert res.get("status") == "ok"
            assert res.get("device_id") == "target_peer_67890"
            mock_node.unpair.assert_called_once_with("target_peer_67890")
        finally:
            await client.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_cli_send_via_ipc(tmp_path: Path, mock_node):
    """Kiểm tra `teleport send` ủy quyền truyền file cho daemon IPC."""
    # Tạo danh tính máy gửi và máy nhận
    load_or_create_identity(tmp_path)
    peer_ident = load_or_create_identity(tmp_path / "peer_ident")
    TrustStore.load(tmp_path).add(peer_cert_from_pem(peer_ident.cert_pem()), "FriendLaptop")

    # Tạo tệp cần gửi
    file_to_send = tmp_path / "test_doc.txt"
    file_to_send.write_text("Hello via IPC daemon!", encoding="utf-8")

    server = IpcServer(tmp_path, mock_node)
    await server.start()

    try:
        args = argparse.Namespace(
            config_dir=tmp_path,
            files=[str(file_to_send)],
            to=["FriendLaptop"],
            limit_rate=None,
            address=None,
            host="0.0.0.0",
            name=None,
            json=False,
        )
        out = io.StringIO()
        err = io.StringIO()
        lines = MagicMock()

        # Mock Transfers của mock_node
        transfers_mock = MagicMock()
        mock_node.transfers = transfers_mock

        async def fake_send(tid, paths, allow_dirs=True):
            # Phát sự kiện hoàn tất qua listener của server
            from teleport.app.transfers import (
                Direction,
                TransferFinished,
                TransferInfo,
                TransferKey,
                TransferResult,
                TransferStatus,
            )
            from teleport.core.messages import FileEntry

            key = TransferKey(tid, Direction.SEND, "transfer_123")
            size = len("Hello via IPC daemon!")
            info = TransferInfo(
                key=key,
                status=TransferStatus.TRANSFERRING,
                files=(FileEntry(name="test_doc.txt", size=size, mtime=1000),),
                names=("test_doc.txt",),
                file_index=0,
                done_bytes=size,
                total_bytes=size,
            )
            ev = TransferFinished(
                info=info,
                result=TransferResult.DONE,
                by_peer=False,
                reason=None,
                file_index=0,
                reject=None,
            )
            if hasattr(transfers_mock, "_on_event") and transfers_mock._on_event:
                transfers_mock._on_event(ev)

        transfers_mock.send = AsyncMock(side_effect=fake_send)
        transfers_mock._on_event = None

        code = await run_send(args, lines, out, err)
        assert code == 0
        output_text = out.getvalue()
        assert "Đã gửi xong 1 file" in output_text
        assert "FriendLaptop" in output_text
    finally:
        await server.close()
