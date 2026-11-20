"""Kiểm thử `TransferManager.promised_bytes()` và `TransferManager.cancel_transfer()` (PR 2.2)."""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from pathlib import Path

import pytest
from support import wait_until

from teleport.app import storage
from teleport.app.transfers import DISK_OP_TIMEOUT, TransferProgress
from teleport.core import transfer as core_transfer
from teleport.core.blocks import BLOCK_SIZE
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore

pytestmark = [
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
    receive_dir: Path,
    resume_helpers,
    *,
    tid: str,
    size: int = 2 * BLOCK_SIZE,
) -> tuple[str, str]:
    """Tạo một lượt nhận dở hợp lệ (`TempKind.RUNNING`) trên đĩa; trả `(receiver_id, sender_id)`."""
    ident_recv = load_or_create_identity(cfg_dir)
    ident_send = load_or_create_identity(cfg_dir.parent / f"sender_{tid[:8]}")
    TrustStore.load(cfg_dir).add(peer_cert_from_pem(ident_send.cert_pem()), "Sender")

    rec = resume_helpers.record_for(
        tid,
        [size],
        receiver=ident_recv.device_id,
        sender=ident_send.device_id,
    )
    receive_dir.mkdir(parents=True, exist_ok=True)
    rd = storage.ReceiveDir.open(receive_dir)
    try:
        temp = rd.create_temp(tid, rec)
        try:
            part = temp.create_part(0, size)
            part.close()
        finally:
            rd.close_temp(temp)
    finally:
        rd.close()
    return ident_recv.device_id, ident_send.device_id


@pytest.mark.integration
async def test_promised_bytes_zero_initially_and_after_cancel(node_factory, tmp_path: Path, resume_helpers):
    """`promised_bytes()` = 0 khi rỗng, > 0 khi có lượt nhận dở và về 0 sau `cancel_transfer`."""
    cfg_dir = tmp_path / "A"
    recv_dir = tmp_path / "nhan-A"
    tid = uuid.uuid4().hex
    _, sender_id = _seed_running_temp(cfg_dir, recv_dir, resume_helpers, tid=tid, size=3 * BLOCK_SIZE)

    node_a = await node_factory("A", node_kwargs={"receive_dir": recv_dir, "auto_accept": True})
    try:
        tm = node_a.node._transfers
        assert tm.promised_bytes() == 3 * BLOCK_SIZE

        key = core_transfer.TransferKey(sender_id, core_transfer.Direction.RECEIVE, tid)
        ok = await tm.cancel_transfer(key, wait_disk=True)
        assert ok is True
        assert tm.promised_bytes() == 0
        assert not (recv_dir / ".teleport" / tid).exists()
    finally:
        await node_a.node.close()


@pytest.mark.integration
async def test_cancel_transfer_unknown_key_returns_false(node_factory, tmp_path: Path):
    """`cancel_transfer` với key không tồn tại trả về `False`."""
    node_a = await node_factory("A", node_kwargs={"receive_dir": tmp_path / "nhan-A", "auto_accept": True})
    try:
        tm = node_a.node._transfers
        assert tm.promised_bytes() == 0
        unknown = core_transfer.TransferKey("0" * 64, core_transfer.Direction.RECEIVE, uuid.uuid4().hex)
        assert await tm.cancel_transfer(unknown) is False
    finally:
        await node_a.node.close()


@pytest.mark.integration
async def test_cancel_transfer_send_returns_true_immediately(transfer_nodes, tmp_path: Path):
    """Lượt `SEND` (không có `_DiskLane`) được hủy và trả về `True` ngay."""
    net = await transfer_nodes(sender_opts={"rate_limit": 256 * 1024})
    src_file = _make_file(net.src, "slow.bin", 2 * MiB)

    key = await net.B.transfers.send(net.A.id, [src_file])
    await wait_until(
        lambda: len(net.B.trec.of(TransferProgress, key.transfer_id)) >= 1,
        10,
        "bên gửi chưa phát tiến độ",
    )

    t0 = time.monotonic()
    ok = await net.B.node._transfers.cancel_transfer(key, wait_disk=True)
    elapsed = time.monotonic() - t0
    assert ok is True
    assert elapsed < 2.0


@pytest.mark.integration
async def test_cancel_transfer_waits_for_disk_lane_done(
    node_factory, tmp_path: Path, resume_helpers, monkeypatch
):
    """`wait_disk=True` chờ `_DiskLane.done` xóa thư mục tạm xong mới trả về `True`."""
    cfg_dir = tmp_path / "A"
    recv_dir = tmp_path / "nhan-A"
    tid = uuid.uuid4().hex
    _, sender_id = _seed_running_temp(cfg_dir, recv_dir, resume_helpers, tid=tid)

    entered = threading.Event()
    release = threading.Event()
    real_remove = storage.ReceiveDir.remove_temp

    def slow_remove(rd, temp, *args, **kwargs) -> None:
        if getattr(temp, "transfer_id", None) == tid:
            entered.set()
            release.wait(timeout=5.0)
        real_remove(rd, temp, *args, **kwargs)

    monkeypatch.setattr(storage.ReceiveDir, "remove_temp", slow_remove)

    node_a = await node_factory("A", node_kwargs={"receive_dir": recv_dir, "auto_accept": True})
    try:
        tm = node_a.node._transfers
        key = core_transfer.TransferKey(sender_id, core_transfer.Direction.RECEIVE, tid)
        task = asyncio.create_task(tm.cancel_transfer(key, wait_disk=True, timeout=DISK_OP_TIMEOUT))

        await wait_until(entered.is_set, 5, "remove_temp chưa được gọi")
        await asyncio.sleep(0.05)
        assert not task.done(), "cancel_transfer(wait_disk=True) không được trả về trước khi disk lane xong"

        release.set()
        assert await task is True
        assert not (recv_dir / ".teleport" / tid).exists()
        assert tm.promised_bytes() == 0
    finally:
        release.set()
        await node_a.node.close()


@pytest.mark.integration
async def test_cancel_transfer_timeout_still_returns_true(
    node_factory, tmp_path: Path, resume_helpers, monkeypatch
):
    """Hết `timeout` khi chờ xóa đĩa vẫn trả về `True` mà không treo."""
    cfg_dir = tmp_path / "A"
    recv_dir = tmp_path / "nhan-A"
    tid = uuid.uuid4().hex
    _, sender_id = _seed_running_temp(cfg_dir, recv_dir, resume_helpers, tid=tid)

    entered = threading.Event()
    release = threading.Event()
    real_remove = storage.ReceiveDir.remove_temp

    def slow_remove(rd, temp, *args, **kwargs) -> None:
        if getattr(temp, "transfer_id", None) == tid:
            entered.set()
            release.wait(timeout=5.0)
        real_remove(rd, temp, *args, **kwargs)

    monkeypatch.setattr(storage.ReceiveDir, "remove_temp", slow_remove)

    node_a = await node_factory("A", node_kwargs={"receive_dir": recv_dir, "auto_accept": True})
    try:
        tm = node_a.node._transfers
        key = core_transfer.TransferKey(sender_id, core_transfer.Direction.RECEIVE, tid)

        t0 = time.monotonic()
        ok = await tm.cancel_transfer(key, wait_disk=True, timeout=0.08)
        elapsed = time.monotonic() - t0

        assert entered.is_set()
        assert ok is True
        assert elapsed < 1.0
    finally:
        release.set()
        await node_a.node.close()


@pytest.mark.integration
async def test_cancel_transfer_wait_disk_false_returns_immediately(
    node_factory, tmp_path: Path, resume_helpers, monkeypatch
):
    """`wait_disk=False` trả về `True` ngay mà không chờ `_DiskLane.done`."""
    cfg_dir = tmp_path / "A"
    recv_dir = tmp_path / "nhan-A"
    tid = uuid.uuid4().hex
    _, sender_id = _seed_running_temp(cfg_dir, recv_dir, resume_helpers, tid=tid)

    entered = threading.Event()
    release = threading.Event()
    real_remove = storage.ReceiveDir.remove_temp

    def slow_remove(rd, temp, *args, **kwargs) -> None:
        if getattr(temp, "transfer_id", None) == tid:
            entered.set()
            release.wait(timeout=5.0)
        real_remove(rd, temp, *args, **kwargs)

    monkeypatch.setattr(storage.ReceiveDir, "remove_temp", slow_remove)

    node_a = await node_factory("A", node_kwargs={"receive_dir": recv_dir, "auto_accept": True})
    try:
        tm = node_a.node._transfers
        key = core_transfer.TransferKey(sender_id, core_transfer.Direction.RECEIVE, tid)

        t0 = time.monotonic()
        ok = await tm.cancel_transfer(key, wait_disk=False)
        elapsed = time.monotonic() - t0

        assert ok is True
        assert elapsed < 0.5
        await wait_until(entered.is_set, 5, "remove_temp chưa chạy nền")
    finally:
        release.set()
        await node_a.node.close()
