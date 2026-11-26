"""Kiểm thử Watch Portal tầng app (`teleport.app.watch`) và tích hợp `Node`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from support import wait_until

from teleport.app.diskio import DiskExecutor
from teleport.app.transfers import TransferResult
from teleport.app.watch import WatchPortalEvent, WatchPortalService
from teleport.core.watch import WatchPortalRule


async def test_watch_portal_service_debounce_offline_queue_and_idle_zero_cpu(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "preexisting.txt").write_bytes(b"old-file")

    executor = DiskExecutor()
    connected = False
    trusted = True
    sent_calls: list[tuple[str, tuple[Path, ...]]] = []
    events: list[WatchPortalEvent] = []

    async def fake_send(device_id: str, paths: list[Path]):
        sent_calls.append((device_id, tuple(paths)))
        return type("K", (), {"transfer_id": "a" * 32})()

    svc = WatchPortalService(
        executor=executor,
        is_trusted=lambda d: trusted and d == "b" * 32,
        is_connected=lambda d: connected and d == "b" * 32,
        send_files=fake_send,
        on_event=events.append,
        stability=0.05,
    )
    svc.start()
    assert svc._task is None  # 0% CPU khi chưa có quy tắc bật

    rule = WatchPortalRule(
        id="w1",
        name="Drop",
        source_dir=str(drop),
        device_id="b" * 32,
        patterns=("*.txt",),
    )
    svc.set_rules([rule])
    assert svc._task is not None

    # Seed ban đầu bỏ qua tệp có sẵn
    await svc.scan_once()
    assert sent_calls == []

    # Tạo tệp mới + tệp tạm bị bỏ qua
    new_file = drop / "fresh.txt"
    new_file.write_bytes(b"hello-portal")
    (drop / "downloading.part").write_bytes(b"ignored")

    await svc.scan_once()
    assert sent_calls == []
    await asyncio.sleep(0.08)
    # Máy đích đang offline: vẫn chưa gửi, giữ vào hàng đệm
    await svc.scan_once()
    assert sent_calls == []

    # Khi máy đích kết nối: đẩy ngay tệp đã sẵn sàng
    connected = True
    svc.on_peer_connected("b" * 32)
    await wait_until(lambda: len(sent_calls) == 1, 5, "watch portal tự đẩy khi nối lại")
    assert sent_calls[0] == ("b" * 32, (new_file,))
    assert events and events[0].transfer_id == "a" * 32

    # Tắt quy tắc -> dừng task quét
    svc.set_rules([])
    await asyncio.sleep(0.01)
    assert svc._task is None

    await svc.close()
    executor.shutdown()


@pytest.mark.integration
async def test_node_watch_portal_auto_pushes_stable_file(transfer_nodes, monkeypatch, tmp_path: Path) -> None:
    from teleport.app import watch as app_watch

    monkeypatch.setattr(app_watch, "SCAN_INTERVAL", 0.1)
    monkeypatch.setattr(app_watch, "STABILITY_SECONDS", 0.05)

    portal_dir = tmp_path / "portal-out"
    portal_dir.mkdir()
    (portal_dir / "existing.txt").write_bytes(b"already-there")

    net = await transfer_nodes()
    rule = WatchPortalRule(
        id="portal-1",
        name="Portal",
        source_dir=str(portal_dir),
        device_id=net.A.id,
        patterns=("*.txt",),
    )
    net.B.node.set_watch_portals([rule])
    await asyncio.sleep(0.25)

    added = portal_dir / "auto-send.txt"
    added.write_bytes(b"portal-payload-123")

    await wait_until(
        lambda: (net.receive_dir / "auto-send.txt").is_file(),
        15,
        "node nhận được file từ Watch Portal",
    )
    fin_b = await net.B.trec.wait_finished()
    assert (net.receive_dir / "auto-send.txt").read_bytes() == b"portal-payload-123"
    assert not (net.receive_dir / "existing.txt").exists()
    assert fin_b.result is TransferResult.DONE
