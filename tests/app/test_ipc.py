"""Kiểm thử tự động cho module IPC CLI ↔ GUI Daemon (`teleport.app.ipc`)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from teleport import __version__
from teleport.app.ipc import (
    IPC_INFO_FILE,
    IpcAuthError,
    IpcClient,
    IpcInfo,
    IpcServer,
    read_ipc_info,
    try_connect_ipc,
)


@pytest.fixture
def mock_node():
    node = MagicMock()
    node.identity.device_id = "test_device_id_12345"
    node.name = "TestPC"
    node.port = 47474
    node.devices.return_value = []
    return node


def test_ipc_info_serialization():
    info = IpcInfo(port=45678, token="abc123token", pid=9999)
    raw = info.to_json()
    loaded = IpcInfo.from_json(raw)

    assert loaded.port == 45678
    assert loaded.token == "abc123token"
    assert loaded.pid == 9999
    assert loaded.version == __version__
    assert loaded.proto == 1


@pytest.mark.asyncio
async def test_ipc_server_lifecycle_and_ping(tmp_path: Path, mock_node):
    server = IpcServer(tmp_path, mock_node)
    await server.start()

    try:
        assert server.port is not None
        assert server.port > 0

        info = read_ipc_info(tmp_path)
        assert info is not None
        assert info.port == server.port
        assert info.token == server._token

        client = IpcClient(info)
        await client.connect()
        try:
            resp = await client.ping()
            assert resp["status"] == "ok"
            assert resp["device_id"] == "test_device_id_12345"
            assert resp["name"] == "TestPC"
            assert resp["version"] == __version__
        finally:
            await client.close()
    finally:
        await server.close()

    assert not (tmp_path / IPC_INFO_FILE).exists()


@pytest.mark.asyncio
async def test_ipc_auth_failure_wrong_token(tmp_path: Path, mock_node):
    server = IpcServer(tmp_path, mock_node)
    await server.start()

    try:
        fake_info = IpcInfo(port=server.port, token="wrong_token_hex_999", pid=1111)
        client = IpcClient(fake_info)
        with pytest.raises(IpcAuthError):
            await client.connect()
        await client.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_ipc_list_devices(tmp_path: Path, mock_node):
    device_mock = MagicMock()
    device_mock.device_id = "peer_98765"
    device_mock.name = "Laptop_Peer"
    device_mock.online = True
    device_mock.paired = True
    device_mock.trusted = True
    device_mock.os = "windows"
    device_mock.addresses = ("192.168.1.100",)
    device_mock.port = 47474
    device_mock.last_address = ("192.168.1.100", 47474)

    mock_node.devices.return_value = [device_mock]

    server = IpcServer(tmp_path, mock_node)
    await server.start()

    try:
        client = await try_connect_ipc(tmp_path)
        assert client is not None
        try:
            res = await client.list_devices()
            assert res.get("status") == "ok"
            assert res.get("self", {}).get("name") == "TestPC"
            devs = res.get("devices", [])
            assert len(devs) == 1
            assert devs[0]["device_id"] == "peer_98765"
            assert devs[0]["name"] == "Laptop_Peer"
            assert devs[0]["online"] is True
            assert devs[0]["addresses"] == ["192.168.1.100"]
        finally:
            await client.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_try_connect_ipc_stale_file_cleanup(tmp_path: Path):
    # Ghi tệp ipc.json với port chết (không có listener)
    stale_info = IpcInfo(port=59999, token="dead_token", pid=99999)
    info_file = tmp_path / IPC_INFO_FILE
    info_file.write_text(stale_info.to_json(), encoding="utf-8")

    client = await try_connect_ipc(tmp_path)
    assert client is None
    # Tệp mồ côi phải tự động bị dọn dẹp
    assert not info_file.exists()
