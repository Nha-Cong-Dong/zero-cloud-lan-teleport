"""Kiểm thử IPC `list_transfers` và `cancel_transfer` (`teleport.app.ipc`, PR 2.2)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from support import wait_until

from teleport.app import storage
from teleport.app.ipc import try_connect_ipc
from teleport.app.transfers import TransferProgress, TransferResult
from teleport.core.blocks import BLOCK_SIZE
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

MiB = 1024 * 1024


def _make_file(directory: Path, name: str, size: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(os.urandom(size))
    return path


def _seed_running_temp(
    cfg_dir: Path,
    recv_dir: Path,
    resume_helpers,
    *,
    tid: str,
    size: int = 2 * BLOCK_SIZE,
    sender_label: str = "peer_sender",
) -> str:
    ident_recv = load_or_create_identity(cfg_dir)
    ident_send = load_or_create_identity(cfg_dir.parent / sender_label)
    TrustStore.load(cfg_dir).add(peer_cert_from_pem(ident_send.cert_pem()), sender_label)

    rec = resume_helpers.record_for(
        tid,
        [size],
        receiver=ident_recv.device_id,
        sender=ident_send.device_id,
    )
    recv_dir.mkdir(parents=True, exist_ok=True)
    rd = storage.ReceiveDir.open(recv_dir)
    try:
        temp = rd.create_temp(tid, rec)
        try:
            temp.create_part(0, size).close()
        finally:
            rd.close_temp(temp)
    finally:
        rd.close()
    return ident_send.device_id


async def test_ipc_list_and_cancel_active_send_and_receive(transfer_nodes, tmp_path: Path):
    """`list_transfers` liệt kê cả `send` và `receive`; `cancel_transfer` hủy được từng chiều."""
    net = await transfer_nodes(
        receiver_opts={"enable_ipc": True, "receive_dir": tmp_path / "nhan-A", "auto_accept": True},
        sender_opts={"enable_ipc": True, "rate_limit": 128 * 1024},
    )
    src_file = _make_file(net.src, "stream.bin", 2 * MiB)
    key = await net.B.transfers.send(net.A.id, [src_file])

    await wait_until(
        lambda: (
            len(net.A.trec.of(TransferProgress, key.transfer_id)) >= 1
            and len(net.B.trec.of(TransferProgress, key.transfer_id)) >= 1
        ),
        15,
        "hai bên chưa bắt đầu truyền",
    )

    client_a = await try_connect_ipc(net.A.dir)
    client_b = await try_connect_ipc(net.B.dir)
    assert client_a is not None and client_b is not None
    try:
        res_a = await client_a.list_transfers()
        assert res_a.get("status") == "ok"
        items_a = res_a.get("transfers", [])
        assert any(t["transfer_id"] == key.transfer_id and t["direction"] == "receive" for t in items_a)

        res_b = await client_b.list_transfers()
        assert res_b.get("status") == "ok"
        items_b = res_b.get("transfers", [])
        assert any(t["transfer_id"] == key.transfer_id and t["direction"] == "send" for t in items_b)

        # Hủy lượt SEND từ phía B qua IPC
        cancel_b = await client_b.cancel_transfer(target=key.transfer_id[:8])
        assert cancel_b.get("status") == "ok"

        fin_b = await net.B.trec.wait_finished(key.transfer_id, timeout=15)
        assert fin_b.result == TransferResult.CANCELLED_USER
        assert (await client_b.list_transfers()).get("transfers") == []
    finally:
        await client_a.close()
        await client_b.close()


async def test_ipc_cancel_receive_frees_promised_bytes_and_removes_temp(
    node_factory, tmp_path: Path, resume_helpers
):
    """Hủy lượt nhận qua IPC làm `promised_bytes()` giảm về 0 và xóa thư mục tạm."""
    cfg_dir = tmp_path / "A"
    recv_dir = tmp_path / "nhan-A"
    tid = "cafe1234" + uuid.uuid4().hex[8:]
    _seed_running_temp(cfg_dir, recv_dir, resume_helpers, tid=tid, size=3 * BLOCK_SIZE)

    node_a = await node_factory(
        "A", node_kwargs={"receive_dir": recv_dir, "auto_accept": True, "enable_ipc": True}
    )
    try:
        assert node_a.node._transfers.promised_bytes() == 3 * BLOCK_SIZE
        assert (recv_dir / ".teleport" / tid).is_dir()

        client = await try_connect_ipc(cfg_dir)
        assert client is not None
        try:
            res = await client.cancel_transfer(target=tid[:6])
            assert res.get("status") == "ok"
            assert node_a.node._transfers.promised_bytes() == 0
            assert not (recv_dir / ".teleport" / tid).exists()
            assert (await client.list_transfers()).get("transfers") == []
        finally:
            await client.close()
    finally:
        await node_a.node.close()


async def test_ipc_cancel_all_ambiguous_and_not_found(node_factory, tmp_path: Path, resume_helpers):
    """Kiểm tra `transfer_not_found`, `ambiguous_transfer`, và `cancel_all=True` qua IPC."""
    cfg_dir = tmp_path / "A"
    recv_dir = tmp_path / "nhan-A"
    tid_1 = "abcd1111" + uuid.uuid4().hex[8:]
    tid_2 = "abcd2222" + uuid.uuid4().hex[8:]
    _seed_running_temp(cfg_dir, recv_dir, resume_helpers, tid=tid_1, size=BLOCK_SIZE, sender_label="sender1")
    _seed_running_temp(cfg_dir, recv_dir, resume_helpers, tid=tid_2, size=BLOCK_SIZE, sender_label="sender2")

    node_a = await node_factory(
        "A", node_kwargs={"receive_dir": recv_dir, "auto_accept": True, "enable_ipc": True}
    )
    try:
        client = await try_connect_ipc(cfg_dir)
        assert client is not None
        try:
            not_found = await client.cancel_transfer(target="deadbeef")
            assert not_found.get("error") == "transfer_not_found"

            ambig = await client.cancel_transfer(target="abcd")
            assert ambig.get("error") == "ambiguous_transfer"

            res_all = await client.cancel_transfer(cancel_all=True)
            assert res_all.get("status") == "ok"
            assert node_a.node._transfers.promised_bytes() == 0
            assert not (recv_dir / ".teleport" / tid_1).exists()
            assert not (recv_dir / ".teleport" / tid_2).exists()
        finally:
            await client.close()
    finally:
        await node_a.node.close()


@pytest.mark.parametrize("bad_cancel_all", ["false", 1, None])
async def test_ipc_cancel_transfer_rejects_non_bool_cancel_all(
    node_factory, tmp_path: Path, resume_helpers, bad_cancel_all
):
    """`cancel_all` không phải `bool` (ví dụ `"false"`, `1`, `None`) trả `invalid_target`, không hủy lượt."""
    cfg_dir = tmp_path / "A"
    recv_dir = tmp_path / "nhan-A"
    tid = "abcd1111" + uuid.uuid4().hex[8:]
    _seed_running_temp(cfg_dir, recv_dir, resume_helpers, tid=tid, size=BLOCK_SIZE)

    node_a = await node_factory(
        "A", node_kwargs={"receive_dir": recv_dir, "auto_accept": True, "enable_ipc": True}
    )
    try:
        client = await try_connect_ipc(cfg_dir)
        assert client is not None
        try:
            resp = await client._call({"command": "cancel_transfer", "cancel_all": bad_cancel_all})
            assert resp.get("error") == "invalid_target"
            assert node_a.node._transfers.promised_bytes() == BLOCK_SIZE
            assert (recv_dir / ".teleport" / tid).is_dir()
            assert len((await client.list_transfers()).get("transfers", [])) == 1
        finally:
            await client.close()
    finally:
        await node_a.node.close()


async def test_ipc_cancel_transfer_rejects_both_cancel_all_and_target(
    node_factory, tmp_path: Path, resume_helpers
):
    """Gửi đồng thời `cancel_all=True` và `target` trả `invalid_target`, không hủy lượt."""
    cfg_dir = tmp_path / "A"
    recv_dir = tmp_path / "nhan-A"
    tid = "abcd1111" + uuid.uuid4().hex[8:]
    _seed_running_temp(cfg_dir, recv_dir, resume_helpers, tid=tid, size=BLOCK_SIZE)

    node_a = await node_factory(
        "A", node_kwargs={"receive_dir": recv_dir, "auto_accept": True, "enable_ipc": True}
    )
    try:
        client = await try_connect_ipc(cfg_dir)
        assert client is not None
        try:
            resp = await client._call({"command": "cancel_transfer", "cancel_all": True, "target": "abcd"})
            assert resp.get("error") == "invalid_target"
            assert node_a.node._transfers.promised_bytes() == BLOCK_SIZE
            assert (recv_dir / ".teleport" / tid).is_dir()
            assert len((await client.list_transfers()).get("transfers", [])) == 1
        finally:
            await client.close()
    finally:
        await node_a.node.close()


@pytest.mark.parametrize("bad_target", ["", "a", "abc", "zzzz", "12g4", 1234, ["abcd"], None])
async def test_ipc_cancel_transfer_rejects_invalid_target(
    node_factory, tmp_path: Path, resume_helpers, bad_target
):
    """`target` ngắn hơn 4 ký tự, non-hex, hoặc không phải `str` trả `invalid_target`, không hủy lượt."""
    cfg_dir = tmp_path / "A"
    recv_dir = tmp_path / "nhan-A"
    tid = "abcd1111" + uuid.uuid4().hex[8:]
    _seed_running_temp(cfg_dir, recv_dir, resume_helpers, tid=tid, size=BLOCK_SIZE)

    node_a = await node_factory(
        "A", node_kwargs={"receive_dir": recv_dir, "auto_accept": True, "enable_ipc": True}
    )
    try:
        client = await try_connect_ipc(cfg_dir)
        assert client is not None
        try:
            resp = await client._call(
                {"command": "cancel_transfer", "target": bad_target, "cancel_all": False}
            )
            assert resp.get("error") == "invalid_target"
            assert node_a.node._transfers.promised_bytes() == BLOCK_SIZE
            assert (recv_dir / ".teleport" / tid).is_dir()
        finally:
            await client.close()
    finally:
        await node_a.node.close()
