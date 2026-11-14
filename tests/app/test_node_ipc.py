"""Kiểm thử tích hợp IPC giữa Node và Client."""

from __future__ import annotations

from pathlib import Path

import pytest

from teleport.app.ipc import IPC_INFO_FILE, try_connect_ipc
from teleport.app.node import Node


@pytest.mark.asyncio
async def test_node_starts_and_stops_ipc_server(tmp_path: Path):
    node = Node(
        tmp_path,
        name="NodeWithIpc",
        port=0,
        accept_pairing=False,
        auto_connect=False,
        enable_ipc=True,
    )
    await node.start()
    try:
        assert (tmp_path / IPC_INFO_FILE).is_file()

        client = await try_connect_ipc(tmp_path)
        assert client is not None
        try:
            pong = await client.ping()
            assert pong["status"] == "ok"
            assert pong["name"] == "NodeWithIpc"
            assert pong["device_id"] == node.identity.device_id
        finally:
            await client.close()
    finally:
        await node.close()

    assert not (tmp_path / IPC_INFO_FILE).exists()


@pytest.mark.asyncio
async def test_node_default_does_not_start_ipc(tmp_path: Path):
    node = Node(
        tmp_path,
        name="NodeNoIpc",
        port=0,
        accept_pairing=False,
        auto_connect=False,
    )
    await node.start()
    try:
        assert not (tmp_path / IPC_INFO_FILE).exists()
        client = await try_connect_ipc(tmp_path)
        assert client is None
    finally:
        await node.close()
