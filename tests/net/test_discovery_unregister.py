"""Hồi quy C2 (tan-cong kịch bản 3): `MdnsDiscovery.stop_advertising` không để lọt câu trả lời sau goodbye.

zeroconf (`async_unregister_service`) chỉ xóa registry; câu trả lời đã xếp trong `out_queue` /
`out_delay_queue` vẫn đi ra sau goodbye với TTL đầy đủ. Gói lọt dội về chính máy này qua multicast
loopback nằm trong cache, làm lần quảng bá lại báo nhầm trùng tên. Các test ở đây:

- xếp sẵn câu trả lời của chính máy này (và của một dịch vụ khác) vào hàng đợi rồi `stop_advertising`:
  không gói nào của máy này có TTL > 0 đi ra sau đó, câu trả lời của dịch vụ khác vẫn đi;
- kẻ hỏi PTR dồn dập lúc ẩn (như kịch bản của tan-cong): không gói TTL > 0 nào sau goodbye đầu;
- quảng bá lại ngay sau `stop_advertising`, kể cả khi cache còn PTR TTL đầy đủ của chính máy này;
- lần quảng bá đầu của một instance vẫn phát hiện trùng tên như zeroconf gốc.

mDNS thật trên 127.0.0.1 (bỏ qua khi `TELEPORT_SKIP_MDNS=1`); `device_id` ngẫu nhiên mỗi test.
"""

from __future__ import annotations

import asyncio
import secrets
import time

import pytest
from zeroconf import DNSOutgoing, DNSPointer, DNSQuestion, Zeroconf
from zeroconf._utils.time import current_time_millis
from zeroconf.const import _CLASS_IN, _FLAGS_QR_QUERY, _TYPE_PTR

from teleport.core.discovery import RECORD_TTL, SERVICE_TYPE, service_name
from teleport.net.discovery import DiscoveryError, MdnsDiscovery, build_service_info

pytestmark = [
    pytest.mark.integration,
    pytest.mark.mdns,
]

LOOPBACK = ["127.0.0.1"]
ADDRS = ["192.0.2.10"]
OTHER_TYPE = "_teleport-test._tcp.local."  # loại dịch vụ khác: không làm phiền browser của test khác


def new_id() -> str:
    return secrets.token_hex(16)


def info_of(device_id: str):
    return build_service_info(device_id=device_id, name="PC", os="windows", port=47474, addresses=ADDRS)


async def advertise(d: MdnsDiscovery, device_id: str) -> None:
    await d.advertise(device_id=device_id, name="PC", os="windows", port=47474, addresses=ADDRS)


def own_names(device_id: str) -> set[str]:
    return {service_name(device_id).lower(), f"teleport-{device_id}.local.".lower()}


def is_own(record, device_id: str) -> bool:
    names = own_names(device_id)
    alias = getattr(record, "alias", None)
    return record.name.lower() in names or (isinstance(alias, str) and alias.lower() in names)


@pytest.fixture
def sent(monkeypatch):
    """Mọi gói trả lời (không phải câu hỏi) zeroconf gửi trong tiến trình: (mốc, id(zc), [(bản ghi, ttl)])."""
    log: list[tuple[float, int, list]] = []
    real = Zeroconf.async_send

    def send(self, out, *args, **kwargs):
        if not out.is_query():
            items = [a[0] if isinstance(a, tuple) else a for a in [*out.answers, *out.additionals]]
            log.append((time.monotonic(), id(self), [(r, r.ttl) for r in items]))
        return real(self, out, *args, **kwargs)

    monkeypatch.setattr(Zeroconf, "async_send", send)
    return log


def own_live_after(log, zc, device_id: str, since: float) -> list:
    """Gói của `zc` sau `since` có bản ghi của máy này với TTL > 0."""
    return [
        (t, rows)
        for t, zid, rows in log
        if zid == id(zc) and t > since and any(is_own(r, device_id) and ttl > 0 for r, ttl in rows)
    ]


async def test_stop_advertising_drops_queued_answers_before_goodbye(sent):
    x = new_id()
    async with MdnsDiscovery(interfaces=LOOPBACK) as a:
        await advertise(a, x)
        zc = a._aiozc.zeroconf  # API nội bộ zeroconf, như bản sửa
        info = info_of(x)
        foreign = DNSPointer(OTHER_TYPE, _TYPE_PTR, _CLASS_IN, RECORD_TTL, f"khac.{OTHER_TYPE}")
        now = current_time_millis()
        zc.out_queue.async_add(now, {info.dns_pointer(): {info.dns_service(), info.dns_text()}})
        zc.out_delay_queue.async_add(
            now, {info.dns_pointer(): {info.dns_service()}, foreign: {info.dns_text()}}
        )
        before = time.monotonic()
        await a.stop_advertising()
        await asyncio.sleep(2.0)  # out_delay_queue: trễ ≥ 1 s
        assert own_live_after(sent, zc, x, before) == [], "câu trả lời của máy này đi ra sau khi ẩn"
        foreign_sent = [
            rows for t, zid, rows in sent if zid == id(zc) and t > before and (foreign, RECORD_TTL) in rows
        ]
        assert foreign_sent, "câu trả lời của dịch vụ khác phải vẫn được gửi"
        assert all(not is_own(r, x) for rows in foreign_sent for r, _ in rows), (
            "phần kèm theo của máy này bị lọt"
        )


async def flood(zc: Zeroconf, stop: asyncio.Event) -> None:
    """Hỏi PTR `_teleport._tcp.local.` multicast mỗi 0,1 s tới khi `stop`."""
    while not stop.is_set():
        out = DNSOutgoing(_FLAGS_QR_QUERY)
        out.add_question(DNSQuestion(SERVICE_TYPE, _TYPE_PTR, _CLASS_IN))
        zc.async_send(out)
        await asyncio.sleep(0.1)


@pytest.mark.slow
async def test_no_answer_after_goodbye_under_query_flood(sent):
    """Kẻ hỏi PTR mỗi 0,1 s trong lúc ẩn (tan-cong kịch bản 3): không gói TTL > 0 nào sau goodbye đầu."""
    async with MdnsDiscovery(interfaces=LOOPBACK) as asker:
        asker_zc = asker._aiozc.zeroconf
        for _ in range(3):
            x = new_id()
            async with MdnsDiscovery(interfaces=LOOPBACK) as a:
                await advertise(a, x)
                zc = a._aiozc.zeroconf
                stop = asyncio.Event()
                task = asyncio.create_task(flood(asker_zc, stop))
                try:
                    await asyncio.sleep(1.0)
                    await a.stop_advertising()
                    goodbyes = [
                        t
                        for t, zid, rows in sent
                        if zid == id(zc) and any(is_own(r, x) and ttl == 0 for r, ttl in rows)
                    ]
                    assert goodbyes, "không thấy goodbye"
                    await asyncio.sleep(2.0)
                finally:
                    stop.set()
                    await task
                assert own_live_after(sent, zc, x, goodbyes[0]) == []


async def test_readvertise_right_after_stop_despite_own_echo_in_cache():
    x = new_id()
    async with MdnsDiscovery(interfaces=LOOPBACK) as a:
        await advertise(a, x)
        await a.stop_advertising()
        # Gói TTL đầy đủ của chính máy này dội về sau goodbye (trước bản sửa): cache giữ ~1 h.
        zc = a._aiozc.zeroconf
        zc.cache.async_add_records([info_of(x).dns_pointer()])
        await advertise(a, x)  # không được báo trùng tên
        await a.stop_advertising()
        await advertise(a, x)  # hiện lại ngay (gói của chính mình dội về còn trong cache)


async def test_first_advertise_still_detects_duplicate_in_cache():
    x = new_id()
    async with MdnsDiscovery(interfaces=LOOPBACK) as a:
        zc = a._aiozc.zeroconf
        zc.cache.async_add_records([info_of(x).dns_pointer()])  # máy khác đang quảng bá cùng mã
        with pytest.raises(DiscoveryError):
            await advertise(a, x)


async def test_real_duplicate_detected_on_every_retry():
    x = new_id()
    async with MdnsDiscovery(interfaces=LOOPBACK) as a, MdnsDiscovery(interfaces=LOOPBACK) as b:
        await advertise(a, x)
        for _ in range(4):  # lần trùng thật không được làm lần thử sau xóa bản ghi của máy kia
            with pytest.raises(DiscoveryError):
                await advertise(b, x)
