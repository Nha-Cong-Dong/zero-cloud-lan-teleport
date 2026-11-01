"""C2 = P4-1: chế độ ẩn (F10) ở tầng `app` (`Node.visible`, `Node.set_visible`).

Viết theo đặc tả, không theo code: `specs/spec-dot-c.md` mục 5.1–5.3, 5.5 và mục 11 (thắng mục 5 khi
khác): netwatch chạy cả khi ẩn; tiêu chí “không quảng bá lọt” tính từ lúc goodbye của lần chuyển sang
ẩn đã gửi xong; Q9 (app không tự đổi `accept_pairing` khi đổi `visible`). Nguồn khác: mvp F3, F10;
protocol §2.3 (máy đang ẩn), §5.3 (hai bên cùng chủ động), §8 (những gì máy này để lộ).

- Phần lớn dùng mDNS giả `fake_mdns` (tests/app/conftest.py): quảng bá là lời gọi `advertise`, goodbye
  là `stop_advertising`. Node vẫn có Listener thật trên 127.0.0.1, nên mọi test là integration.
- `test_hide_goodbye_disappears_from_list_within_10s` dùng mDNS thật trên loopback (như
  `test_node_mdns.py`), bỏ qua khi `TELEPORT_SKIP_MDNS=1`.
- Máy “đang ẩn lúc khởi động”: ghi `settings.json` có `"visible": false` trước khi start (`start` đọc
  `settings.visible`, mặc định True).
- Lỗi ghi `settings.json` giả bằng cách vá `Settings.save` (ném OSError), như `test_node_settings.py`.

Chờ theo điều kiện (`wait_until`); chỉ ngủ cố định khi kiểm “không có gì xảy ra thêm”.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import wait_until

from teleport.app import node as node_mod
from teleport.app import settings as settings_mod
from teleport.app.node import DeviceConnected, DeviceOffline, DeviceOnline, MdnsOptions
from teleport.app.pairing import PairingOutcome
from teleport.core.discovery import DeviceAdded
from teleport.trust.store import TrustStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

LOCAL = "127.0.0.1"
ADVERTISING = ("advertise", "stop_advertising")
QUIET = 0.8  # chờ “không có gì xảy ra thêm”: 4 vòng netwatch (NETWORK_CHECK_INTERVAL = 0,2 s nhờ `fast`)


# --- Hàm phụ trợ ---------------------------------------------------------------------------------


def write_settings(config_dir: Path, data: dict) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, **data}
    (config_dir / "settings.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def on_disk(config_dir: Path) -> dict:
    path = config_dir / "settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def failing_save(monkeypatch) -> SimpleNamespace:
    """`Settings.save` ném OSError tới khi `state.fail = False`."""
    real = settings_mod.Settings.save
    state = SimpleNamespace(fail=True, calls=0)

    def save(self, config_dir):
        state.calls += 1
        if state.fail:
            raise OSError(28, "giả lập không ghi được settings.json")
        return real(self, config_dir)

    monkeypatch.setattr(settings_mod.Settings, "save", save)
    return state


def mdns_of(ctl, node) -> list:
    """Mọi instance mDNS giả (cả đã đóng) mà `node` đã duyệt, theo thứ tự tạo."""
    return [m for m in ctl.instances if m.self_device_id == node.id]


def adv_calls(ctl, node) -> list[str]:
    """`advertise` / `stop_advertising` của `node`, theo đúng thứ tự toàn cục mọi instance."""
    mine = {m.index for m in mdns_of(ctl, node)}
    return [name for index, name in ctl.log if index in mine and name in ADVERTISING]


def advertised_payloads(ctl, node) -> list[dict]:
    return [kw for m in mdns_of(ctl, node) for kw in m.advertised]


def last_adv(ctl, node) -> str | None:
    calls = adv_calls(ctl, node)
    return calls[-1] if calls else None


async def wait_last_adv(ctl, node, name: str, timeout: float = 3.0) -> None:
    await wait_until(lambda: last_adv(ctl, node) == name, timeout, f"lời gọi mDNS cuối chưa là {name}")


async def anonymous_gets_hello(raw_client, port: int) -> bool:
    """Kết nối ẩn danh: True nếu server gửi `hello`; False nếu server đóng mà không gửi byte nào."""
    peer = await raw_client(port)
    try:
        msg = await peer.read(3.0)
    except Exception:  # đóng/reset trước khi có message
        msg = None
    if msg is None:
        _, rest = await peer.expect_closed(3.0)
        assert rest == b"", f"máy ẩn gửi {len(rest)} byte cho kết nối ẩn danh"
        peer.abort()
        return False
    assert msg.get("type") == "hello", msg
    peer.abort()
    return True


async def assert_anonymous_refused_zero_bytes(raw_client, port: int) -> None:
    peer = await raw_client(port)
    elapsed, data = await peer.expect_closed(3.0)
    assert data == b"", f"máy đang ẩn gửi {len(data)} byte ứng dụng cho kết nối ẩn danh: {data[:80]!r}"
    assert elapsed < 1.5, f"đóng sau {elapsed:.2f} s: có vẻ chờ hết giờ mở đầu thay vì từ chối ngay"
    peer.abort()


# --- mDNS thật -----------------------------------------------------------------------------------


def real_opts(n: int) -> MdnsOptions:
    return MdnsOptions(
        interfaces=(LOCAL,),
        advertise_addresses=(f"192.0.2.{n}",),
        probe_interval=1.0,
        resolve_timeout=0.5,
        probe_tick=0.2,
    )


@pytest.mark.slow
@pytest.mark.mdns
async def test_hide_goodbye_disappears_from_list_within_10s(node_factory):
    A = await node_factory("A", mdns=real_opts(61))
    B = await node_factory("B", mdns=real_opts(62))
    assert A.discovery_active and B.discovery_active, (A.discovery_error, B.discovery_error)
    await B.rec.wait_event(DeviceOnline, A.id, timeout=8)
    online_before = len(B.rec.events_of(DeviceOnline, A.id))

    t0 = time.monotonic()
    A.set_visible(False)
    assert A.visible is False
    await B.rec.wait_event(DeviceOffline, A.id, timeout=10)
    elapsed = time.monotonic() - t0
    assert elapsed <= 10.0, f"máy kia còn thấy máy ẩn sau {elapsed:.1f} s (N1: < 10 s)"
    assert not any(v.device_id == A.id and v.online for v in B.devices())

    # Không còn bản quảng bá nào sau goodbye: B dò lại mỗi 1 s mà không thấy A nữa.
    await asyncio.sleep(3.0)
    assert len(B.rec.events_of(DeviceOnline, A.id)) == online_before, "máy ẩn lại hiện ra sau goodbye"
    assert not any(v.device_id == A.id and v.online for v in B.devices())
    # Máy ẩn vẫn duyệt mDNS.
    assert A.discovery_active is True
    assert any(v.device_id == B.id and v.online for v in A.devices())


# --- Quảng bá với mDNS giả -------------------------------------------------------------------------


async def test_show_readvertises(node_factory, fake_mdns):
    A = await node_factory("A", mdns=MdnsOptions())
    assert A.visible is True
    await wait_last_adv(fake_mdns, A, "advertise")

    A.set_visible(False)
    assert A.visible is False
    await wait_last_adv(fake_mdns, A, "stop_advertising")

    n = len(advertised_payloads(fake_mdns, A))
    A.set_visible(True)
    assert A.visible is True
    await wait_last_adv(fake_mdns, A, "advertise")
    payloads = advertised_payloads(fake_mdns, A)
    assert len(payloads) == n + 1
    assert payloads[-1]["device_id"] == A.id
    assert payloads[-1]["name"] == A.name
    assert payloads[-1]["port"] == A.port
    assert A.discovery_active is True
    assert not any(m.closing for m in mdns_of(fake_mdns, A)), "bật/tắt hiện không cần khởi động lại mDNS"


async def test_set_visible_same_value_is_harmless(node_factory, fake_mdns):
    A = await node_factory("A", mdns=MdnsOptions())
    await wait_last_adv(fake_mdns, A, "advertise")
    A.set_visible(False)
    await wait_last_adv(fake_mdns, A, "stop_advertising")
    A.set_visible(False)
    await asyncio.sleep(QUIET)
    assert last_adv(fake_mdns, A) == "stop_advertising"
    assert "advertise" not in adv_calls(fake_mdns, A)[adv_calls(fake_mdns, A).index("stop_advertising") :]
    assert A.visible is False


@pytest.mark.slow
async def test_hidden_still_browses_and_autoconnects_to_visible_trusted(
    node_factory, trust_each_other, fake_mdns, app_helpers
):
    trust_each_other("A", "B")
    A = await node_factory("A", mdns=MdnsOptions())
    B = await node_factory("B", mdns=MdnsOptions())
    A.set_visible(False)
    await wait_last_adv(fake_mdns, A, "stop_advertising")
    assert A.discovery_active is True

    # A (đang ẩn) thấy B (đang hiện, tin cậy) qua mDNS → tự mở kết nối điều khiển tới B.
    fake_mdns.of(A).emit(DeviceAdded(app_helpers.dev(B)))
    await wait_until(lambda: B.id in A.connected_devices(), 5, "A đang ẩn chưa tự nối tới B")
    await wait_until(lambda: A.id in B.connected_devices(), 5, "B chưa ghi nhận A")
    [up] = A.rec.events_of(DeviceConnected, B.id)
    assert up.outgoing is True
    [view] = [v for v in A.devices() if v.device_id == B.id]
    assert view.online and view.paired and view.connected
    assert last_adv(fake_mdns, A) == "stop_advertising", "tự nối ra không được làm máy ẩn quảng bá lại"


async def test_hidden_accepts_incoming_control_from_trusted(node_factory, trust_each_other, control_client):
    """Kết nối có chứng chỉ client tin cậy không bị chế độ ẩn ảnh hưởng (spec 5.3)."""
    trust_each_other("A", "B")
    A = await node_factory("A")
    B = await node_factory("B")
    A.set_visible(False)
    await control_client(B, A)
    await wait_until(
        lambda: B.id in A.connected_devices(), 5, "A đang ẩn không nhận kết nối điều khiển của B"
    )


@pytest.mark.slow
async def test_hidden_at_start_no_advertise_ever(node_factory, fake_mdns, tmp_path):
    fake_mdns.local_addresses = ("192.0.2.5",)
    write_settings(tmp_path / "A", {"visible": False})
    A = await node_factory("A", mdns=MdnsOptions())
    assert A.visible is False
    assert A.settings.visible is False
    assert A.discovery_active is True, "máy ẩn vẫn duyệt mDNS"
    first = mdns_of(fake_mdns, A)
    assert first and "start_browsing" in first[0].calls

    # Đổi tên lúc đang ẩn, rồi đổi địa chỉ card mạng (netwatch khởi động lại mDNS): vẫn không quảng bá.
    A.set_name("Ten-Moi")
    reads = fake_mdns.addr_reads
    await wait_until(lambda: fake_mdns.addr_reads >= reads + 2, 3, "netwatch chưa đọc địa chỉ card mạng")
    n = len(fake_mdns.instances)
    fake_mdns.local_addresses = ("192.0.2.6",)
    await wait_until(lambda: len(fake_mdns.instances) > n, 3, "netwatch không chạy khi ẩn (mục 11, sửa 1)")
    await wait_until(
        lambda: "start_browsing" in mdns_of(fake_mdns, A)[-1].calls, 3, "mDNS mới chưa duyệt lại"
    )
    await asyncio.sleep(QUIET)
    assert adv_calls(fake_mdns, A).count("advertise") == 0, adv_calls(fake_mdns, A)
    assert advertised_payloads(fake_mdns, A) == []
    assert A.discovery_active is True


@pytest.mark.slow
async def test_netwatch_runs_while_hidden_and_show_advertises_on_current_instance(
    node_factory, fake_mdns, tmp_path
):
    """Mục 11, sửa 1: netwatch chạy cả khi ẩn, và `set_visible(True)` không cần tạo lại nó."""
    fake_mdns.local_addresses = ("192.0.2.5",)
    write_settings(tmp_path / "A", {"visible": False})
    A = await node_factory("A", mdns=MdnsOptions())
    reads = fake_mdns.addr_reads
    await wait_until(lambda: fake_mdns.addr_reads >= reads + 3, 3, "netwatch không chạy khi đang ẩn")

    A.set_visible(True)
    await wait_last_adv(fake_mdns, A, "advertise")
    current = mdns_of(fake_mdns, A)[-1]
    assert not current.closing
    assert current.advertised and current.advertised[-1]["device_id"] == A.id

    # Sau khi hiện: đổi địa chỉ → mDNS khởi động lại và instance mới quảng bá (netwatch vẫn sống, một bản).
    n = len(fake_mdns.instances)
    fake_mdns.local_addresses = ("192.0.2.7",)
    await wait_until(lambda: len(fake_mdns.instances) > n, 3, "netwatch không khởi động lại mDNS")
    await wait_until(lambda: mdns_of(fake_mdns, A)[-1].advertised, 3, "instance mới không quảng bá")
    await asyncio.sleep(QUIET)
    assert len(fake_mdns.instances) == n + 1, "nhiều netwatch cùng chạy: mDNS bị khởi động lại nhiều lần"


@pytest.mark.slow
async def test_set_name_while_hidden_no_advertise(node_factory, fake_mdns):
    A = await node_factory("A", mdns=MdnsOptions())
    await wait_last_adv(fake_mdns, A, "advertise")
    A.set_visible(False)
    await wait_last_adv(fake_mdns, A, "stop_advertising")
    n = len(advertised_payloads(fake_mdns, A))

    assert A.set_name("Ten-Moi") == "Ten-Moi"
    assert A.name == "Ten-Moi"
    await asyncio.sleep(QUIET)
    assert last_adv(fake_mdns, A) == "stop_advertising", adv_calls(fake_mdns, A)
    assert len(advertised_payloads(fake_mdns, A)) == n

    A.set_visible(True)
    await wait_last_adv(fake_mdns, A, "advertise")
    assert advertised_payloads(fake_mdns, A)[-1]["name"] == "Ten-Moi"


@pytest.mark.slow
async def test_toggle_race_ends_in_last_state(node_factory, fake_mdns, tmp_path):
    """Bật/tắt dồn dập (kể cả lúc quảng bá / goodbye đang chờ): cuối cùng mDNS khớp giá trị gọi sau
    cùng; nếu là ẩn thì từ goodbye cuối không còn quảng bá nào (mục 11, sửa 8)."""
    A = await node_factory("A", mdns=MdnsOptions())
    await wait_last_adv(fake_mdns, A, "advertise")
    m = mdns_of(fake_mdns, A)[-1]
    real_advertise, real_stop = m.advertise, m.stop_advertising
    delays = random.Random(7)

    async def slow_advertise(**kw):
        await asyncio.sleep(delays.choice((0, 0.005, 0.02)))
        await real_advertise(**kw)

    async def slow_stop():
        await asyncio.sleep(delays.choice((0, 0.005, 0.02)))
        await real_stop()

    m.advertise = slow_advertise
    m.stop_advertising = slow_stop

    rng = random.Random(20261031)
    for round_no in range(12):
        value = A.visible
        for _ in range(rng.randint(1, 6)):
            value = rng.choice((True, False))
            A.set_visible(value)
            pause = rng.choice((0, 0, 0.001, 0.01, 0.03))
            if pause:
                await asyncio.sleep(pause)
        where = f"vòng {round_no}, giá trị cuối {value}, các lời gọi: {adv_calls(fake_mdns, A)[-12:]}"
        want = "advertise" if value else "stop_advertising"
        await wait_last_adv(fake_mdns, A, want, timeout=5)
        await asyncio.sleep(0.15)
        assert last_adv(fake_mdns, A) == want, where
        assert A.visible is value, where
        assert A.settings.visible is value, where
        assert on_disk(A.dir).get("visible", True) is value, where

    # Kết thúc ở trạng thái ẩn: từ goodbye cuối không còn quảng bá nào, kể cả qua vài vòng netwatch.
    A.set_visible(True)
    A.set_visible(False)
    await wait_last_adv(fake_mdns, A, "stop_advertising", timeout=5)
    mark = len(adv_calls(fake_mdns, A))
    await asyncio.sleep(QUIET)
    assert adv_calls(fake_mdns, A)[mark:] == [], adv_calls(fake_mdns, A)[mark - 3 :]
    assert not m.closing


@pytest.mark.slow
async def test_visible_persisted(node_factory, fake_mdns):
    A = await node_factory("A", mdns=MdnsOptions())
    assert A.visible is True and A.settings.visible is True
    A.set_visible(False)
    assert A.settings.visible is False
    assert on_disk(A.dir).get("visible") is False  # ghi file ngay trong lời gọi (đồng bộ)
    await A.close()

    before = {m.index for m in fake_mdns.instances}
    A2 = await node_factory("A", mdns=MdnsOptions())
    assert A2.visible is False and A2.settings.visible is False
    await asyncio.sleep(QUIET)
    fresh = [m for m in mdns_of(fake_mdns, A2) if m.index not in before]
    assert all("advertise" not in m.calls for m in fresh), [m.calls for m in fresh]
    A2.set_visible(True)
    assert A2.settings.visible is True
    assert on_disk(A2.dir).get("visible", True) is True
    await A2.close()

    A3 = await node_factory("A", mdns=MdnsOptions())
    assert A3.visible is True
    await wait_last_adv(fake_mdns, A3, "advertise")


async def test_restart_hidden_never_advertises(node_factory, fake_mdns):
    """Lần chạy sau của máy đã chọn ẩn: không có `advertise` nào trên instance mới."""
    A = await node_factory("A", mdns=MdnsOptions())
    await wait_last_adv(fake_mdns, A, "advertise")
    A.set_visible(False)
    await wait_last_adv(fake_mdns, A, "stop_advertising")
    await A.close()
    before = {m.index for m in fake_mdns.instances}
    A2 = await node_factory("A", mdns=MdnsOptions())
    await asyncio.sleep(QUIET)
    fresh = [m for m in mdns_of(fake_mdns, A2) if m.index not in before]
    assert fresh, "lần chạy mới không bật mDNS"
    assert all("advertise" not in m.calls for m in fresh), [m.calls for m in fresh]
    assert all("start_browsing" in m.calls for m in fresh)


async def test_set_visible_save_error_changes_nothing(node_factory, fake_mdns, raw_client, monkeypatch):
    A = await node_factory("A", mdns=MdnsOptions())
    await wait_last_adv(fake_mdns, A, "advertise")
    calls_before = list(adv_calls(fake_mdns, A))
    state = failing_save(monkeypatch)

    with pytest.raises(node_mod.SettingsSaveError):
        A.set_visible(False)
    assert state.calls >= 1
    assert A.visible is True
    assert A.settings.visible is True
    assert on_disk(A.dir).get("visible", True) is True
    await asyncio.sleep(QUIET)
    assert adv_calls(fake_mdns, A) == calls_before, "lưu lỗi mà vẫn gửi goodbye"
    assert await anonymous_gets_hello(raw_client, A.port), "lưu lỗi mà luật ẩn danh đã đổi"

    state.fail = False
    A.set_visible(False)
    assert A.visible is False
    await wait_last_adv(fake_mdns, A, "stop_advertising")


# --- Luật nhận kết nối ẩn danh của node: `_visible or accept_pairing or đang khởi xướng tới ip` ----------


async def test_hidden_not_waiting_refuses_anonymous_with_zero_bytes(node_factory, raw_client):
    A = await node_factory("A", accept_pairing=False)
    assert await anonymous_gets_hello(raw_client, A.port), "máy đang hiện phải gửi hello như trước"
    A.set_visible(False)
    await assert_anonymous_refused_zero_bytes(raw_client, A.port)
    assert A.rec.prompts == []


async def test_anonymous_rule_follows_visible_and_accepting_at_runtime(node_factory, raw_client):
    A = await node_factory("A", accept_pairing=False)
    A.set_visible(False)
    await assert_anonymous_refused_zero_bytes(raw_client, A.port)
    A.accept_pairing = True  # chế độ chờ ghép đôi (F3): nhận ẩn danh dù đang ẩn
    assert await anonymous_gets_hello(raw_client, A.port)
    A.accept_pairing = False
    await assert_anonymous_refused_zero_bytes(raw_client, A.port)
    A.set_visible(True)
    assert await anonymous_gets_hello(raw_client, A.port)


async def test_hidden_listen_pair_by_ip(node_factory, fake_mdns):
    A = await node_factory("A", accept_pairing=True, answer=True, mdns=MdnsOptions())
    A.set_visible(False)
    await wait_last_adv(fake_mdns, A, "stop_advertising")
    B = await node_factory("B", answer=True)
    result = await asyncio.wait_for(B.pairing.pair_with([LOCAL], A.port), 15)
    assert result.outcome == PairingOutcome.PAIRED, result
    await A.rec.wait_finished()
    assert A.rec.finished[0].outcome == PairingOutcome.PAIRED
    assert TrustStore.load(A.dir).get(B.id) is not None
    assert TrustStore.load(B.dir).get(A.id) is not None
    assert last_adv(fake_mdns, A) == "stop_advertising", "ghép đôi xong không được làm máy ẩn quảng bá"


async def test_hidden_not_waiting_cannot_be_paired_by_ip(node_factory):
    A = await node_factory("A", accept_pairing=False, answer=True)
    A.set_visible(False)
    B = await node_factory("B", answer=True)
    result = await asyncio.wait_for(B.pairing.pair_with([LOCAL], A.port), 15)
    assert result.outcome != PairingOutcome.PAIRED, result
    assert A.rec.prompts == []
    assert TrustStore.load(A.dir).get(B.id) is None


@pytest.mark.parametrize("hidden", ["lo", "hi", "both"])
async def test_hidden_initiator_receives_simultaneous_pair_request(node_factory, hidden):
    """Hai `teleport pair` chạy cùng lúc (`accept_pairing=False`), bên ẩn vẫn nhận kết nối ẩn danh từ
    địa chỉ nó đang khởi xướng tới, nên luật hai bên cùng chủ động (protocol §5.3) vẫn cho PAIRED."""
    A = await node_factory("A", accept_pairing=False, answer=True)
    B = await node_factory("B", accept_pairing=False, answer=True)
    lo, hi = sorted([A, B], key=lambda n: n.id)
    for side in {"lo": [lo], "hi": [hi], "both": [lo, hi]}[hidden]:
        side.set_visible(False)
    r_lo, r_hi = await asyncio.wait_for(
        asyncio.gather(lo.pairing.pair_with([LOCAL], hi.port), hi.pairing.pair_with([LOCAL], lo.port)), 20
    )
    assert r_lo.outcome == PairingOutcome.PAIRED, (r_lo, r_hi)
    assert r_hi.outcome == PairingOutcome.PAIRED, (r_lo, r_hi)
    assert TrustStore.load(lo.dir).get(hi.id) is not None
    assert TrustStore.load(hi.dir).get(lo.id) is not None
    assert lo.rec.prompts[0].code == hi.rec.prompts[0].code


# --- Q9 và mvp F3: `visible` không đụng `accept_pairing` hay phiên đang chạy ------------------------


@pytest.mark.parametrize("accepting", [True, False])
async def test_set_visible_does_not_change_accept_pairing(node_factory, accepting):
    A = await node_factory("A", accept_pairing=accepting, answer=True)
    for visible in (False, True, False, True):
        A.set_visible(visible)
        assert A.accept_pairing is accepting, f"set_visible({visible}) đổi accept_pairing"
        assert A.pairing.accepting is accepting


async def test_hiding_does_not_cancel_running_pairing_session(node_factory):
    """mvp F3: chuyển sang ẩn không hủy phiên ghép đôi đã nhận (A đang hiện mã thì người dùng tắt hiện)."""
    hid: list[bool] = []

    def hide_then_accept(prompt) -> None:
        A.set_visible(False)
        hid.append(A.visible)
        A.pairing.decide(prompt.session_id, True)

    A = await node_factory("A", accept_pairing=True, answer=hide_then_accept)
    B = await node_factory("B", answer=True)
    result = await asyncio.wait_for(B.pairing.pair_with([LOCAL], A.port), 15)
    assert hid == [False]
    assert result.outcome == PairingOutcome.PAIRED, result
    await A.rec.wait_finished()
    assert A.rec.finished[0].outcome == PairingOutcome.PAIRED
    assert TrustStore.load(A.dir).get(B.id) is not None
