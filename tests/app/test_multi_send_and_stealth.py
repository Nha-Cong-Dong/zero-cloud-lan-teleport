"""Kiểm thử cài đặt stealth và chuyển đổi trạng thái ẩn danh trong Node."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from teleport.app.node import Node
from teleport.app.settings import Settings


def test_settings_stealth(tmp_path: Path) -> None:
    settings_file = tmp_path
    settings = Settings.load(settings_file)
    assert not settings.stealth

    updated = replace(settings, stealth=True)
    updated.save(settings_file)

    reloaded = Settings.load(settings_file)
    assert reloaded.stealth is True


@pytest.mark.asyncio
async def test_node_stealth_flag(tmp_path: Path) -> None:
    node = Node(tmp_path, stealth=True, mdns=None)
    await node.start()
    try:
        assert node.stealth is True
        node.set_stealth(False)
        assert node.stealth is False
    finally:
        await node.close()
