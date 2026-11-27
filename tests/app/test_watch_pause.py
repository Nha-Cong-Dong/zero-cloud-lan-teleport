"""Kiểm thử tạm dừng / tiếp tục toàn bộ cổng đẩy tự động (`WatchPortalService.set_paused`)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from teleport.app.diskio import DiskExecutor
from teleport.app.watch import WatchPortalService
from teleport.core.watch import WatchPortalRule


@pytest.mark.asyncio
async def test_watch_portal_pause_and_resume(tmp_path: Path) -> None:
    watch_dir = tmp_path / "drop"
    watch_dir.mkdir()
    sent_batches: list[tuple[str, tuple[Path, ...]]] = []
    connected = False

    async def fake_send(device_id: str, paths) -> object:
        sent_batches.append((device_id, tuple(paths)))
        return type("K", (), {"transfer_id": "t" * 32})()

    executor = DiskExecutor(threads=1)
    rule = WatchPortalRule(
        id="r1",
        name="Drop",
        source_dir=str(watch_dir),
        device_id="a" * 32,
        patterns=(),
        recursive=False,
        enabled=True,
    )
    service = WatchPortalService(
        executor=executor,
        is_trusted=lambda _d: True,
        is_connected=lambda _d: connected,
        send_files=fake_send,
        stability=0.0,
    )
    try:
        service.set_rules([rule])
        service.start()
        assert service._task is not None
        await service.scan_once()

        # Khi tạm dừng: hủy task nền, scan_once không quét
        assert service.set_paused(True) is True
        assert service.paused is True
        assert service._task is None

        (watch_dir / "photo.png").write_bytes(b"IMG")
        await service.scan_once()
        assert sent_batches == []

        # Tiếp tục khi thiết bị đã kết nối -> task chạy lại và đẩy tệp
        connected = True
        assert service.set_paused(False) is False
        assert service.paused is False
        assert service._task is not None

        for _ in range(5):
            await service.scan_once()
            if sent_batches:
                break
            await asyncio.sleep(0.02)
        assert len(sent_batches) == 1
        assert sent_batches[0][0] == "a" * 32
        assert [p.name for p in sent_batches[0][1]] == ["photo.png"]
    finally:
        await service.close()
        executor.shutdown()
