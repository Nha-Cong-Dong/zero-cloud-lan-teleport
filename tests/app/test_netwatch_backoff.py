"""Hồi quy lỗi phát/thử lại liên tục khi mạng/multicast bị chặn: netwatch giãn nhịp mDNS theo backoff."""

from __future__ import annotations

import asyncio

import pytest
from support import wait_until

from teleport.app import node as node_mod
from teleport.app.node import MdnsOptions
from teleport.net.discovery import DiscoveryError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]


async def test_netwatch_backs_off_on_repeated_mdns_start_failures_and_resets_on_addr_change(
    node_factory, fake_mdns, monkeypatch
):
    monkeypatch.setattr(node_mod, "NETWORK_CHECK_INTERVAL", 0.05)
    monkeypatch.setattr(node_mod, "MDNS_RETRY_BASE", 1.5)
    monkeypatch.setattr(node_mod, "MDNS_RETRY_MAX", 6.0)
    fake_mdns.fail_start = DiscoveryError("giả lập: multicast bị tường lửa chặn")

    A = await node_factory("A", mdns=MdnsOptions())
    assert not A.discovery_active
    # Chờ lượt netwatch đầu tiên thử lại ngay (lúc mới bật mdns_next_retry = 0.0) -> tạo instance thứ 2.
    await wait_until(lambda: len(fake_mdns.instances) >= 2, 2.0, "netwatch chưa thử lại lần đầu")
    count_after_first_retry = len(fake_mdns.instances)

    # Trong cửa sổ backoff (< 1.5s), netwatch vẫn đọc địa chỉ nhiều lần nhưng không tạo lại mDNS.
    reads = fake_mdns.addr_reads
    await wait_until(lambda: fake_mdns.addr_reads >= reads + 4, 2.0, "netwatch không tiếp tục chạy")
    assert len(fake_mdns.instances) == count_after_first_retry

    # Khi địa chỉ mạng đổi -> reset backoff và phục hồi mDNS ngay lượt netwatch kế tiếp.
    fake_mdns.fail_start = None
    fake_mdns.local_addresses = ("10.0.0.99",)
    await wait_until(lambda: A.discovery_active, 2.0, "netwatch không thử lại ngay khi địa chỉ đổi")
    assert len(fake_mdns.instances) == count_after_first_retry + 1
    await asyncio.sleep(0)
