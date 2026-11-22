"""Kiểm thử khôi phục hàng đợi gửi `outbox.json` giữa hai node thật (Khối 2A)."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import pytest
from support import wait_until

from teleport.app import outbox
from teleport.app import transfers as app_transfers
from teleport.core import transfer as core_transfer
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.transfer import Direction, TransferKey

pytestmark = [
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
leakcheck = pytest.mark.usefixtures("no_leaks")

LOCAL = "127.0.0.1"
MiB = 1024 * 1024
REAL_GRACE = core_transfer.RECONNECT_GRACE


def _make_file(directory: Path, name: str, size: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with open(path, "wb") as f:
        left = size
        while left:
            chunk = os.urandom(min(left, 4 * MiB))
            f.write(chunk)
            left -= len(chunk)
    return path


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4 * MiB):
            h.update(chunk)
    return h.hexdigest()


async def _reopen_node(
    node_factory, label: str, *, suspend_sends: bool = True, receive_dir: Path | None = None
):
    """Mở lại một node đã từng chạy cùng thư mục cấu hình và gắn recorder mới."""
    trec = _simple_recorder(label)
    extra = {
        "on_transfer_event": trec.on_update,
        "suspend_sends": suspend_sends,
    }
    if receive_dir is not None:
        extra["receive_dir"] = receive_dir
        extra["auto_accept"] = True
    handle = await node_factory(label, node_kwargs=extra)
    handle.trec = trec
    return handle


def _done_bytes_of(node, tid: str) -> int:
    for info in node.transfers.transfers():
        if info.key.transfer_id == tid:
            return info.done_bytes
    return 0


@leakcheck
async def test_dual_end_suspend_and_resume_skips_synced_blocks(
    transfer_nodes, node_factory, tcp_proxy, monkeypatch
):
    """Hai đầu cùng tạm dừng giữa lượt nhiều khối (B có `suspend_sends=True`, A nhận >= 16 MiB)
    → khởi động lại cả hai → B tự khôi phục từ `outbox.json`, nối lại và hoàn tất với
    `payload_sent < size`, SHA-256 trùng khớp, `outbox.json` được dọn sạch."""
    monkeypatch.setattr(core_transfer, "RECONNECT_GRACE", REAL_GRACE)
    monkeypatch.setattr(app_transfers, "CLOSE_DISK_WAIT", 30.0)

    net = await transfer_nodes(sender_opts={"suspend_sends": True}, connect=False)
    A, B = net.A, net.B
    proxy = tcp_proxy(A.port)
    B.connect_hint(A.id, [LOCAL], proxy.port)
    await wait_until(lambda: A.id in B.connected_devices() and B.id in A.connected_devices(), 15)

    size = 20 * BLOCK_SIZE + 321
    src = _make_file(net.src, "video.bin", size)
    expected_sha = _sha256_of(src)
    proxy.block_at("c2s", 17 * BLOCK_SIZE)

    key = await B.transfers.send(A.id, [src])
    tid = key.transfer_id

    await wait_until(lambda: proxy.fired, 60, "chưa tới điểm cắt proxy")
    await wait_until(lambda: _done_bytes_of(A, tid) >= 16 * BLOCK_SIZE, 30, "A chưa sync lô 16 MiB")
    await wait_until(lambda: len(outbox.load(B.dir)) == 1, 10, "outbox.json chưa lưu lượt gửi")

    proxy.rst()
    await asyncio.wait_for(B.close(), 30)
    await asyncio.wait_for(A.close(), 30)

    fin_b1 = B.trec.finished(tid)
    fin_a1 = A.trec.finished(tid)
    assert fin_b1 is not None and fin_b1.result is app_transfers.TransferResult.SUSPENDED
    assert fin_a1 is not None and fin_a1.result is app_transfers.TransferResult.SUSPENDED
    assert len(outbox.load(B.dir)) == 1

    A2 = await _reopen_node(node_factory, "A", suspend_sends=False, receive_dir=net.receive_dir)
    B2 = await _reopen_node(node_factory, "B", suspend_sends=True)

    resumed = B2.trec.of("TransferResumed", tid)
    assert len(resumed) == 1
    assert B2.transfers.get(TransferKey(A2.id, Direction.SEND, tid)) is not None

    B2.connect_hint(A2.id, [LOCAL], A2.port)
    fin_b2 = await B2.trec.wait_finished(tid, 45)
    fin_a2 = await A2.trec.wait_finished(tid, 45)
    assert fin_b2.result is app_transfers.TransferResult.DONE
    assert fin_a2.result is app_transfers.TransferResult.DONE
    assert fin_b2.payload_sent < size, f"phải gửi ít hơn tổng dung lượng, thực tế={fin_b2.payload_sent}"

    file_done = A2.trec.of("FileDone", tid)[-1]
    assert file_done.path is not None and _sha256_of(file_done.path) == expected_sha
    await wait_until(lambda: outbox.load(B2.dir) == [], 10, "outbox.json chưa dọn sau khi hoàn tất")


@leakcheck
async def test_sender_crash_abort_now_recovers_from_persisted_outbox(
    transfer_nodes, node_factory, tcp_proxy, monkeypatch
):
    """Mô phỏng sập nguồn bên gửi giữa chừng (`Node.close` bị hủy → `abort_now()`, không xả cuối):
    mở lại node B từ bản `outbox.json` đã bền từ lúc `send()` vẫn khôi phục và hoàn tất."""
    monkeypatch.setattr(core_transfer, "RECONNECT_GRACE", REAL_GRACE)
    net = await transfer_nodes(sender_opts={"suspend_sends": True}, connect=False)
    A, B = net.A, net.B
    proxy = tcp_proxy(A.port)
    B.connect_hint(A.id, [LOCAL], proxy.port)
    await wait_until(lambda: A.id in B.connected_devices() and B.id in A.connected_devices(), 15)

    size = 20 * BLOCK_SIZE + 513
    src = _make_file(net.src, "crash.bin", size)
    expected_sha = _sha256_of(src)
    proxy.block_at("c2s", 17 * BLOCK_SIZE)

    key = await B.transfers.send(A.id, [src])
    tid = key.transfer_id
    await wait_until(lambda: proxy.fired, 60, "chưa tới điểm cắt proxy")
    await wait_until(lambda: _done_bytes_of(A, tid) >= 16 * BLOCK_SIZE, 30, "A chưa sync lô đầu")
    await wait_until(lambda: len(outbox.load(B.dir)) == 1, 10, "outbox.json chưa được ghi")

    proxy.rst()

    held = asyncio.Event()
    real_tm_close = app_transfers.TransferManager.close

    async def stall_tm_close(self):
        if self is B.transfers and not held.is_set():
            held.set()
            await asyncio.Event().wait()
        return await real_tm_close(self)

    monkeypatch.setattr(app_transfers.TransferManager, "close", stall_tm_close)
    closing = asyncio.create_task(B.close())
    await asyncio.wait_for(held.wait(), 10)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert len(outbox.load(B.dir)) == 1

    B2 = await _reopen_node(node_factory, "B", suspend_sends=True)
    assert len(B2.trec.of("TransferResumed", tid)) == 1
    B2.connect_hint(A.id, [LOCAL], A.port)

    fin_b2 = await B2.trec.wait_finished(tid, 45)
    fin_a = await A.trec.wait_finished(tid, 45)
    assert fin_b2.result is app_transfers.TransferResult.DONE
    assert fin_a.result is app_transfers.TransferResult.DONE
    assert fin_b2.payload_sent < size

    file_done = A.trec.of("FileDone", tid)[-1]
    assert file_done.path is not None and _sha256_of(file_done.path) == expected_sha
    await wait_until(lambda: outbox.load(B2.dir) == [], 10, "outbox.json phải rỗng sau hoàn tất")


@leakcheck
async def test_offline_send_queue_persists_across_restart_then_completes_when_peer_online(
    node_factory, trust_each_other, tmp_path, monkeypatch
):
    """B gửi khi A đang tắt (hàng đợi gửi offline) → đóng B (`suspend_sends=True`) rồi mở lại →
    sau đó A khởi động → lượt tự động truyền xong và dọn `outbox.json`."""
    monkeypatch.setattr(core_transfer, "RECONNECT_GRACE", REAL_GRACE)
    trust_each_other("A", "B")

    b_trec = _simple_recorder("B")
    B = await node_factory(
        "B",
        node_kwargs={"on_transfer_event": b_trec.on_update, "suspend_sends": True},
    )
    B.trec = b_trec
    a_id = B.store.devices()[0].device_id

    src_dir = tmp_path / "nguon"
    src = _make_file(src_dir, "offline.bin", 128 * 1024 + 77)
    expected_sha = _sha256_of(src)

    key = await B.transfers.send(a_id, [src])
    tid = key.transfer_id
    await wait_until(lambda: len(outbox.load(B.dir)) == 1, 10, "outbox.json chưa lưu hàng đợi offline")

    await asyncio.wait_for(B.close(), 20)
    assert B.trec.finished(tid).result is app_transfers.TransferResult.SUSPENDED
    assert len(outbox.load(B.dir)) == 1

    B2 = await _reopen_node(node_factory, "B", suspend_sends=True)
    assert len(B2.trec.of("TransferResumed", tid)) == 1

    recv_dir = tmp_path / "nhan-A"
    A = await _reopen_node(node_factory, "A", suspend_sends=False, receive_dir=recv_dir)
    B2.connect_hint(A.id, [LOCAL], A.port)

    fin_b2 = await B2.trec.wait_finished(tid, 30)
    fin_a = await A.trec.wait_finished(tid, 30)
    assert fin_b2.result is app_transfers.TransferResult.DONE
    assert fin_a.result is app_transfers.TransferResult.DONE

    file_done = A.trec.of("FileDone", tid)[-1]
    assert file_done.path is not None and _sha256_of(file_done.path) == expected_sha
    await wait_until(lambda: outbox.load(B2.dir) == [], 10, "outbox.json chưa được dọn sạch")


@leakcheck
async def test_missing_or_modified_source_and_orderly_non_suspend_close(
    node_factory, trust_each_other, tmp_path, monkeypatch
):
    """Nguồn bị xóa/sửa trước khi khởi động lại bị loại bỏ an toàn và dọn `outbox.json`;
    node đóng với `suspend_sends=False` (mặc định CLI) vẫn hủy lượt và xóa `outbox.json`."""
    monkeypatch.setattr(core_transfer, "RECONNECT_GRACE", REAL_GRACE)
    trust_each_other("A", "B")

    # (a) Chuẩn bị node B với `suspend_sends=True`, gửi 2 file lúc A tắt
    b_trec = _simple_recorder("B")
    B = await node_factory(
        "B",
        node_kwargs={"on_transfer_event": b_trec.on_update, "suspend_sends": True},
    )
    a_id = B.store.devices()[0].device_id
    src_dir = tmp_path / "nguon"
    f_missing = _make_file(src_dir, "missing.bin", 4096)
    f_changed = _make_file(src_dir, "changed.bin", 4096)

    await B.transfers.send(a_id, [f_missing])
    await B.transfers.send(a_id, [f_changed])
    await wait_until(lambda: len(outbox.load(B.dir)) == 2, 10)
    await asyncio.wait_for(B.close(), 20)

    # Xóa f_missing, sửa kích thước f_changed trước khi khởi động lại
    f_missing.unlink()
    f_changed.write_bytes(b"changed-content-different-size")

    B2 = await _reopen_node(node_factory, "B", suspend_sends=True)
    assert B2.transfers.transfers() == ()
    assert B2.trec.of("TransferResumed") == []
    await wait_until(lambda: outbox.load(B2.dir) == [], 10, "outbox.json phải tự dọn gọn mục hỏng")
    await asyncio.wait_for(B2.close(), 20)

    # (b) Mặc định CLI (`suspend_sends=False`): đóng có trật tự phải hủy lượt và dọn `outbox.json`
    b3_trec = _simple_recorder("B3")
    B3 = await node_factory("B", node_kwargs={"on_transfer_event": b3_trec.on_update})
    f_cli = _make_file(src_dir, "cli.bin", 2048)
    k_cli = await B3.transfers.send(a_id, [f_cli])
    await wait_until(lambda: len(outbox.load(B3.dir)) == 1, 10)
    await asyncio.wait_for(B3.close(), 20)
    assert b3_trec.finished(k_cli.transfer_id).result is app_transfers.TransferResult.CANCELLED_USER
    assert outbox.load(B3.dir) == []


def _simple_recorder(label: str):
    events: list = []

    class _Rec:
        def on_update(self, u) -> None:
            events.append(u)

        def of(self, kind_name: str, tid: str | None = None) -> list:
            out = []
            for u in events:
                if type(u).__name__ != kind_name:
                    continue
                k = getattr(u, "key", None) or getattr(getattr(u, "info", None), "key", None)
                if tid is None or (k is not None and k.transfer_id == tid):
                    out.append(u)
            return out

        def finished(self, tid: str):
            found = self.of("TransferFinished", tid)
            return found[0] if found else None

        async def wait_finished(self, tid: str, timeout: float = 30.0):
            await wait_until(lambda: self.finished(tid) is not None, timeout, f"{label}: chưa xong {tid[:8]}")
            return self.finished(tid)

    return _Rec()
