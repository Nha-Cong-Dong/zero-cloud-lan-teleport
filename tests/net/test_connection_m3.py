"""Test tích hợp `Listener` + `core.admission` cho PR 2.3 (ngân sách lỗi theo fingerprint / chống M3).

Kiểm chứng trên socket thật (`127.0.0.1`, và `127.0.0.2` khi hệ điều hành cho phép bind):
- Thiết bị tin cậy `fp` cạn `DEVICE_PRIORITY_STRIKES` trên một IP bị hạ: IP mới mở với cùng `fp`
  không được cấp ưu tiên, trong khi IP sạch đã gắn `fp` từ trước vẫn giữ ưu tiên và vẫn mở thêm
  kết nối được khi pool chung đóng.
- Một `fp` chỉ được giữ tối đa `MAX_PRIORITY_IPS_PER_DEVICE` (= 2) IP ưu tiên đồng thời; IP thứ 3
  không được ưu tiên, không đẩy IP cũ đang dùng ra.
"""

import asyncio
import contextlib
import socket
from types import SimpleNamespace

import pytest
from support import wait_until

from teleport.core import admission
from teleport.core.admission import Refusal
from teleport.core.messages import PROTOCOL_VERSION, Hello
from teleport.net import connection
from teleport.net.connection import open_control
from teleport.trust.identity import load_or_create_identity

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_admission_limits,
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

LOCAL = "127.0.0.1"
SECOND_LOCAL = "127.0.0.2"
CLEAN_TEST_IP = "192.0.2.10"
ROTATED_TEST_IP = "192.0.2.20"


def _hello_of(identity, name: str = "B") -> Hello:
    return Hello(
        protocol=PROTOCOL_VERSION,
        device_id=identity.device_id,
        name=name,
        app_version="0.0.0",
        os="windows",
    )


def _can_bind(ip: str) -> bool:
    """macOS chỉ có 127.0.0.1 trên lo0: bind 127.0.0.2 lỗi EADDRNOTAVAIL."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((ip, 0))
        except OSError:
            return False
    return True


async def _bare_tcp_from(h, src: str) -> None:
    """Mở kết nối TCP trần từ địa chỉ nguồn `src`, chờ server nhận rồi cắt ngay."""
    before = sum(h.listener.refusals.values())
    _, writer = await asyncio.wait_for(asyncio.open_connection(LOCAL, h.port, local_addr=(src, 0)), 3)
    try:
        await wait_until(
            lambda: h.listener.opening_count > 0 or sum(h.listener.refusals.values()) > before,
            2.0,
            "server chưa nhận/từ chối kết nối TCP trần",
        )
    finally:
        writer.transport.abort()
        with contextlib.suppress(Exception):
            writer.close()
    await h.wait_idle()


@pytest.fixture(scope="module")
def ids(tmp_path_factory):
    base = tmp_path_factory.mktemp("m3-ids")
    return SimpleNamespace(
        server=load_or_create_identity(base / "server"),
        b=load_or_create_identity(base / "b"),
    )


@pytest.fixture
def slow(fast, monkeypatch):
    """OPENING_TIMEOUT 5 s: tránh hết giờ sớm khi kiểm tra kết nối bị từ chối/chấp nhận."""
    monkeypatch.setattr(connection, "OPENING_TIMEOUT", 5.0)
    return fast


async def test_listener_blocks_new_priority_ip_after_device_exhausts_strikes_but_keeps_clean_ip(
    slow, monkeypatch, listener_factory, raw_client, ids
):
    """Đốt hết ngân sách `DEVICE_PRIORITY_STRIKES` trên `LOCAL`: `LOCAL` bị hạ, IP mới của cùng `fp`
    (`ROTATED_TEST_IP`) không được cấp ưu tiên, trong khi `CLEAN_TEST_IP` mở từ trước vẫn ưu tiên."""
    for name, value in {
        "PRIORITY_RATE_PER_IP": 0.001,
        "PRIORITY_BURST_PER_IP": 8,
        "PRIORITY_BURST_TOTAL": 8,
    }.items():
        monkeypatch.setattr(admission, name, value)

    h = await listener_factory(ids.server, trusted=[ids.b])
    h.listener.note_outgoing_control(CLEAN_TEST_IP, ids.b.fingerprint)
    control = await open_control([LOCAL], h.port, ids.b, _hello_of(ids.b), h.identity.cert_pem())
    try:
        await h.next()
        assert h.listener.is_priority(CLEAN_TEST_IP) is True
        assert h.listener.is_priority(LOCAL) is True

        for _ in range(admission.DEVICE_PRIORITY_STRIKES + 1):
            p = await raw_client(h.port, use_tls=False)
            await wait_until(lambda: h.listener.opening_count == 1, 2.0, "server chưa nhận kết nối")
            p.abort()
            await h.wait_idle()

        assert h.listener.is_priority(LOCAL) is False
        assert h.listener.is_priority(CLEAN_TEST_IP) is True

        h.listener.note_outgoing_control(ROTATED_TEST_IP, ids.b.fingerprint)
        assert h.listener.is_priority(ROTATED_TEST_IP) is False
        assert h.listener.is_priority(CLEAN_TEST_IP) is True
    finally:
        await control.close()


async def test_listener_caps_priority_ips_per_device_and_keeps_existing_ip_admitted(
    slow, monkeypatch, listener_factory, ids
):
    """Một `fp` chỉ được tối đa `MAX_PRIORITY_IPS_PER_DEVICE` (= 2) IP ưu tiên: IP thứ 3 không
    được cấp ưu tiên, trong khi IP đầu (`LOCAL`) vẫn mở thêm kết nối được khi pool chung đóng."""
    h = await listener_factory(ids.server, trusted=[ids.b])
    c1 = await open_control([LOCAL], h.port, ids.b, _hello_of(ids.b), h.identity.cert_pem())
    c2 = None
    try:
        await h.next()
        h.listener.note_outgoing_control(CLEAN_TEST_IP, ids.b.fingerprint)
        h.listener.note_outgoing_control(ROTATED_TEST_IP, ids.b.fingerprint)

        assert h.listener.is_priority(LOCAL) is True
        assert h.listener.is_priority(CLEAN_TEST_IP) is True
        assert h.listener.is_priority(ROTATED_TEST_IP) is False

        monkeypatch.setattr(admission, "MAX_OPENING_TOTAL", 0)
        c2 = await open_control([LOCAL], h.port, ids.b, _hello_of(ids.b), h.identity.cert_pem())
        await h.next()
        assert h.listener.refusals.get(Refusal.OPENING_TOTAL, 0) == 0
    finally:
        if c2 is not None:
            await c2.close()
        await c1.close()


async def test_listener_two_ips_device_exhaustion_keeps_clean_ip_admitted_when_general_closed(
    slow, monkeypatch, listener_factory, ids
):
    """Hai IP loopback thật: `LOCAL` là IP sạch, `SECOND_LOCAL` đốt hết `DEVICE_PRIORITY_STRIKES`.
    `ROTATED_TEST_IP` mở mới bị từ chối ưu tiên, nhưng `LOCAL` vẫn mở thêm kết nối được khi
    `MAX_OPENING_TOTAL = 0`."""
    if not _can_bind(SECOND_LOCAL):
        pytest.skip(f"Hệ điều hành không cho bind {SECOND_LOCAL}")

    for name, value in {
        "PRIORITY_RATE_PER_IP": 0.001,
        "PRIORITY_BURST_PER_IP": 8,
        "PRIORITY_BURST_TOTAL": 8,
    }.items():
        monkeypatch.setattr(admission, name, value)

    h = await listener_factory(ids.server, trusted=[ids.b])
    clean_ctrl = await open_control([LOCAL], h.port, ids.b, _hello_of(ids.b), h.identity.cert_pem())
    extra_ctrl = None
    try:
        await h.next()
        h.listener.note_outgoing_control(SECOND_LOCAL, ids.b.fingerprint)
        assert h.listener.is_priority(LOCAL) is True
        assert h.listener.is_priority(SECOND_LOCAL) is True

        for _ in range(admission.DEVICE_PRIORITY_STRIKES + 1):
            await _bare_tcp_from(h, SECOND_LOCAL)

        assert h.listener.is_priority(SECOND_LOCAL) is False
        assert h.listener.is_priority(LOCAL) is True

        h.listener.note_outgoing_control(ROTATED_TEST_IP, ids.b.fingerprint)
        assert h.listener.is_priority(ROTATED_TEST_IP) is False

        monkeypatch.setattr(admission, "MAX_OPENING_TOTAL", 0)
        extra_ctrl = await open_control([LOCAL], h.port, ids.b, _hello_of(ids.b), h.identity.cert_pem())
        await h.next()
    finally:
        if extra_ctrl is not None:
            await extra_ctrl.close()
        await clean_ctrl.close()
