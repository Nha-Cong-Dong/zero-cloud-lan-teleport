"""Hồi quy C2 (tan-cong kịch bản 3): node đang hiện mà quảng bá lỗi thì netwatch thử lại.

Trước bản sửa, một lần `advertise` lỗi (ví dụ zeroconf báo trùng tên vì gói của chính máy này dội
về) làm node kẹt ở trạng thái "hiện" mà không quảng bá, vì netwatch chỉ thử lại khi mDNS tắt hẳn.
Dùng mDNS giả `fake_mdns`; NETWORK_CHECK_INTERVAL = 0,2 s nhờ fixture `fast`.
"""

from __future__ import annotations

import pytest
from support import wait_until

from teleport.app.node import MdnsOptions
from teleport.net.discovery import DiscoveryError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]


def advertise_count(ctl, node) -> int:
    return sum(len(m.advertised) for m in ctl.instances if m.self_device_id == node.id)


async def test_visible_failed_advertise_is_retried_by_netwatch(node_factory, fake_mdns):
    fake_mdns.fail_advertise = DiscoveryError("giả lập: trùng tên instance")
    A = await node_factory("A", mdns=MdnsOptions())
    assert isinstance(A.discovery_error, DiscoveryError)
    n = len(fake_mdns.instances)
    fake_mdns.fail_advertise = None
    await wait_until(lambda: A.discovery_error is None, 3, "netwatch chưa thử quảng bá lại")
    assert advertise_count(fake_mdns, A) >= 1
    assert len(fake_mdns.instances) == n, "thử quảng bá lại không cần khởi động lại mDNS"


async def test_hiding_stops_advertise_retries(node_factory, fake_mdns):
    fake_mdns.fail_advertise = DiscoveryError("giả lập: trùng tên instance")
    A = await node_factory("A", mdns=MdnsOptions())
    assert isinstance(A.discovery_error, DiscoveryError)
    A.set_visible(False)
    await wait_until(lambda: "stop_advertising" in fake_mdns.of(A).calls, 3, "chưa gửi goodbye khi ẩn")
    fake_mdns.fail_advertise = None
    reads = fake_mdns.addr_reads
    await wait_until(lambda: fake_mdns.addr_reads >= reads + 3, 3, "netwatch chưa chạy")
    assert advertise_count(fake_mdns, A) == 0, "đang ẩn: netwatch không được quảng bá"
    assert A.discovery_error is None
