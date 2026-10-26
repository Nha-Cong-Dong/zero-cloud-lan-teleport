"""Test `teleport.core.links` (đặc tả PR-4 mục 2.1, 3.1 và 4.2: LK1–LK23).

Viết từ đặc tả, không từ code. `LinkTracker` là máy trạng thái sans-IO giữ kết nối điều khiển:
luật chống trùng (protocol §6.1), lịch nối lại và chặn (§6.2), giới hạn tốc độ, báo lỗi một lần.
Test chỉ dùng API công khai (`connected`, `attempting`, `next_wakeup` và các hành động trả về).

Các con số trong đặc tả (lịch 1, 2, 4… ≤ 30 s, ≤ 16 lần thử trong 300 s…) tính với hằng mặc định,
nên file này không patch hằng số (trừ test kiểm hằng được đọc lúc dùng).
"""

from __future__ import annotations

import contextlib
import dataclasses
import heapq
import itertools
import math
from collections.abc import Callable

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

import teleport.core.links as links
from teleport.core.links import (
    CancelConnect,
    CloseConnection,
    Connect,
    ConnectResult,
    LinkDown,
    LinkFailed,
    LinkSwitched,
    LinkTracker,
    LinkUp,
    Notify,
)
from teleport.core.messages import MessageError
from teleport.net.connection import OPENING_TIMEOUT

OWN = "8" * 32
LO = "1" * 32  # nhỏ hơn OWN
HI = "f" * 32  # lớn hơn OWN
PEER = "a" * 32  # lớn hơn OWN
Q = "b" * 32
R = "c" * 32
ADDR = ("192.0.2.10",)
ADDR2 = ("192.0.2.20", "198.51.100.7")
PORT = 47474
PORT2 = 50123

OK = ConnectResult.OK
UNREACHABLE = ConnectResult.UNREACHABLE
BLOCKING = [ConnectResult.REJECTED, ConnectResult.IDENTITY_MISMATCH, ConnectResult.VERSION_MISMATCH]

HYPO = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def delay(n: int) -> float:
    """`delay(n)` của đặc tả mục 3.1 với hằng mặc định: 1, 2, 4, 8, 16, 30, 30…"""
    return min(1.0 * 2 ** min(n - 1, 20), 30.0)


def tracker(*trusted: str, own: str = OWN, now: float = 0.0) -> LinkTracker:
    t = LinkTracker(own)
    if trusted:
        assert [a for a in t.trusted_changed(list(trusted), now) if isinstance(a, Connect)] == []
    return t


def only(actions: list, kind: type) -> list:
    return [a for a in actions if isinstance(a, kind)]


def the_connect(actions: list) -> Connect:
    found = only(actions, Connect)
    assert len(found) == 1, actions
    return found[0]


def notifies(actions: list) -> list:
    return [a.event for a in actions if isinstance(a, Notify)]


def start_attempt(t: LinkTracker, dev: str = PEER, now: float = 0.0, addrs=ADDR, port: int = PORT) -> Connect:
    """Thiết bị đã tin cậy: `device_seen` → đúng một `Connect`."""
    return the_connect(t.device_seen(dev, addrs, port, now))


def tick_to_wakeup(t: LinkTracker) -> tuple[list, float]:
    nw = t.next_wakeup()
    assert nw is not None, "không có lần thử nào được hẹn"
    return t.tick(nw), nw


def fail_n_unreachable(t: LinkTracker, dev: str, n: int, start: float = 0.0, took: float = 0.1) -> float:
    """Thiết bị đã tin cậy, chưa thấy: thấy ở `start`, rồi thất bại `UNREACHABLE` n lần liên tiếp theo
    lịch. Trả thời điểm bắt đầu lần thử cuối cùng (lần thất bại thứ n xong ở đó + `took`)."""
    c = start_attempt(t, dev, start)
    last_start = start
    for i in range(n):
        t.connect_finished(dev, c.attempt_id, UNREACHABLE, None, last_start + took)
        if i == n - 1:
            break
        acts, last_start = tick_to_wakeup(t)
        c = the_connect(acts)
    return last_start


# --- Hằng số, kiểu ---------------------------------------------------------------------------------


def test_constants_match_spec():
    assert links.REPLACE_AFTER == 15.0
    assert links.RETRY_BASE == 1.0
    assert links.RETRY_MAX == 30.0
    assert links.EVENT_RETRY_GAP == 2.0
    assert links.UNREACHABLE_REPORT_AFTER == 3


def test_replace_after_exceeds_opening_timeout():
    """Lần mở chậm nhất (net) của hai bên gọi nhau cùng lúc không được bị coi là “nối lại” (§6.1).

    Có phần dư cho độ lệch đo giữa hai bên: biên thật là hạn mở cộng hai lần độ trễ giao, nên
    ngưỡng chỉ nhỉnh hơn hạn mở (ví dụ 10,01 s) vẫn lặp lại được vòng đóng/mở của LK22."""
    assert links.REPLACE_AFTER >= OPENING_TIMEOUT + 2.0


def test_connect_result_values():
    assert {r.value for r in ConnectResult} == {
        "ok",
        "unreachable",
        "rejected",
        "identity_mismatch",
        "version_mismatch",
    }


def test_actions_are_frozen_dataclasses():
    objs = [
        Connect(PEER, 1, ADDR, PORT),
        CancelConnect(PEER, 1),
        CloseConnection(PEER, 1),
        LinkUp(PEER, 1, True),
        LinkDown(PEER, 1),
        LinkFailed(PEER, UNREACHABLE),
        Notify(LinkDown(PEER, 1)),
    ]
    for obj in objs:
        assert dataclasses.is_dataclass(obj)
        # Gán vào chính một trường của đối tượng: gán thuộc tính lạ vào dataclass
        # frozen + slots ra TypeError thay vì FrozenInstanceError trên Python 3.12.
        field = dataclasses.fields(obj)[0].name
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, field, Q)


def test_constants_are_read_at_call_time(monkeypatch):
    """Đặc tả mục 2: hằng thời gian là biến toàn cục, đọc lúc dùng (fixture `fast` dựa vào điều này)."""
    monkeypatch.setattr(links, "RETRY_BASE", 0.2)
    t = tracker(PEER)
    c = start_attempt(t)
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 1.0)
    assert t.next_wakeup() == pytest.approx(1.2)


def test_new_tracker_is_empty():
    t = LinkTracker(OWN)
    assert t.connected() == {}
    assert t.attempting() == {}
    assert t.next_wakeup() is None
    assert t.tick(100.0) == []


@pytest.mark.parametrize("own", ["xyz", "", "A" * 32, "0" * 31, None, 5])
def test_constructor_rejects_bad_own_id(own):
    with pytest.raises(ValueError):  # MessageError là ValueError; đặc tả không nói rõ lớp nào
        LinkTracker(own)


def test_connected_and_attempting_return_copies():
    t = tracker(PEER, Q)
    t.incoming_control(PEER, 1, 0.0)
    start_attempt(t, Q, 0.0)
    t.connected()[Q] = 99
    t.attempting()[PEER] = 99
    assert t.connected() == {PEER: 1}
    assert t.attempting() == {Q: 1}


# --- LK1: kiểm tham số --------------------------------------------------------------------------------


def _lk1_state() -> tuple[LinkTracker, int]:
    """PEER đang kết nối (conn 1), Q đang chờ lịch (next_wakeup 2.0), R đang thử. `now` cuối = 1.0."""
    t = tracker(PEER, Q, R)
    assert notifies(t.incoming_control(PEER, 1, 0.0)) == [LinkUp(PEER, 1, False)]
    cq = start_attempt(t, Q, 0.0)
    cr = start_attempt(t, R, 0.5)
    t.connect_finished(Q, cq.attempt_id, UNREACHABLE, None, 1.0)
    assert t.next_wakeup() == pytest.approx(2.0)
    return t, cr.attempt_id


def _bad_calls(ar: int) -> list[tuple[str, Callable[[LinkTracker], object], type | tuple]]:
    big = 1000.0  # `now` hợp lệ; lỗi phải đến từ tham số khác và không làm đồng hồ nhích lên
    return [
        ("id_ngan", lambda t: t.device_seen("abc", ADDR, PORT, big), MessageError),
        ("id_hoa", lambda t: t.device_seen("A" * 32, ADDR, PORT, big), MessageError),
        ("id_none", lambda t: t.device_gone(None, big), MessageError),
        ("id_retry", lambda t: t.retry_now("x" * 32, big), MessageError),
        ("id_incoming", lambda t: t.incoming_control("g" * 32, 7, big), MessageError),
        ("id_lost", lambda t: t.connection_lost("0" * 33, 1, big), MessageError),
        ("id_finish", lambda t: t.connect_finished("zz", ar, UNREACHABLE, None, big), MessageError),
        ("ids_str", lambda t: t.trusted_changed("ab" * 16, big), MessageError),
        ("ids_bytes", lambda t: t.trusted_changed(b"ab" * 16, big), MessageError),
        ("ids_phan_tu_sai", lambda t: t.trusted_changed([PEER, "bad"], big), MessageError),
        ("dia_chi_rong", lambda t: t.device_seen(Q, [], PORT, big), ValueError),
        (
            "17_dia_chi",
            lambda t: t.device_seen(Q, [f"192.0.2.{i}" for i in range(1, 18)], PORT, big),
            ValueError,
        ),
        ("0.0.0.0", lambda t: t.device_seen(Q, ["0.0.0.0"], PORT, big), ValueError),
        ("1.2.3", lambda t: t.device_seen(Q, ["1.2.3"], PORT, big), ValueError),
        ("chuoi_don", lambda t: t.device_seen(Q, "192.0.2.10", PORT, big), ValueError),
        ("multicast", lambda t: t.device_seen(Q, ["224.0.0.251"], PORT, big), ValueError),
        ("broadcast", lambda t: t.device_seen(Q, ["255.255.255.255"], PORT, big), ValueError),
        ("khong_chuan", lambda t: t.device_seen(Q, ["192.000.2.10"], PORT, big), ValueError),
        ("ipv6", lambda t: t.device_seen(Q, ["::1"], PORT, big), ValueError),
        ("dia_chi_khong_phai_chuoi", lambda t: t.device_seen(Q, [3232235777], PORT, big), ValueError),
        ("cong_0", lambda t: t.device_seen(Q, ADDR, 0, big), ValueError),
        ("cong_65536", lambda t: t.device_seen(Q, ADDR, 65536, big), ValueError),
        ("cong_true", lambda t: t.device_seen(Q, ADDR, True, big), ValueError),
        ("cong_chuoi", lambda t: t.device_seen(Q, ADDR, "47474", big), ValueError),
        ("cong_float", lambda t: t.device_seen(Q, ADDR, 47474.0, big), ValueError),
        ("now_nan", lambda t: t.tick(math.nan), ValueError),
        ("now_inf", lambda t: t.device_seen(Q, ADDR, PORT, math.inf), ValueError),
        ("now_giam", lambda t: t.device_seen(Q, ADDR, PORT, 0.5), ValueError),
        ("now_giam_tick", lambda t: t.tick(0.0), ValueError),
        ("now_bool", lambda t: t.tick(True), (ValueError, TypeError)),
        ("now_chuoi", lambda t: t.network_changed("5"), (ValueError, TypeError)),
        ("conn_true", lambda t: t.incoming_control(Q, True, big), ValueError),
        ("conn_0", lambda t: t.incoming_control(Q, 0, big), ValueError),
        ("conn_am", lambda t: t.connection_lost(PEER, -1, big), ValueError),
        ("conn_lost_true", lambda t: t.connection_lost(PEER, True, big), ValueError),
        ("attempt_true", lambda t: t.connect_finished(R, True, UNREACHABLE, None, big), ValueError),
        ("attempt_0", lambda t: t.connect_finished(R, 0, UNREACHABLE, None, big), ValueError),
        ("ok_thieu_conn", lambda t: t.connect_finished(R, ar, OK, None, big), ValueError),
        ("unreachable_co_conn", lambda t: t.connect_finished(R, ar, UNREACHABLE, 9, big), ValueError),
        ("ket_qua_la", lambda t: t.connect_finished(R, ar, "khong-ro", None, big), (ValueError, TypeError)),
        ("conn_trung_incoming", lambda t: t.incoming_control(Q, 1, big), ValueError),
        ("conn_trung_finish", lambda t: t.connect_finished(R, ar, OK, 1, big), ValueError),
    ]


_BAD_NAMES = [name for name, _, _ in _bad_calls(1)]


@pytest.mark.parametrize("case", _BAD_NAMES)
def test_lk1_bad_arguments_raise_and_leave_state_unchanged(case):
    t, ar = _lk1_state()
    call, exc = next((fn, e) for name, fn, e in _bad_calls(ar) if name == case)
    before = (t.connected(), t.attempting(), t.next_wakeup())
    with pytest.raises(exc):
        call(t)
    assert (t.connected(), t.attempting(), t.next_wakeup()) == before
    # Lời gọi lỗi không được làm đồng hồ nhích lên: `now` = 1.5 (≥ 1.0 của lần gọi hợp lệ cuối) vẫn hợp lệ.
    assert t.tick(1.5) == []
    acts, _ = tick_to_wakeup(t)
    assert the_connect(acts).device_id == Q


def test_lk1_result_accepts_string_value():
    t = tracker(PEER)
    c = start_attempt(t)
    assert notifies(t.connect_finished(PEER, c.attempt_id, "ok", 5, 1.0)) == [LinkUp(PEER, 5, True)]
    assert t.connected() == {PEER: 5}


def test_lk1_equal_now_is_allowed():
    t = tracker(PEER)
    start_attempt(t, PEER, 3.0)
    assert t.tick(3.0) == []
    assert t.network_changed(3.0) == []


def test_lk1_addresses_deduplicated_keeping_order():
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0, ["192.0.2.2", "192.0.2.1", "192.0.2.2", "127.0.0.1"])
    assert c.addresses == ("192.0.2.2", "192.0.2.1", "127.0.0.1")


def test_lk1_sixteen_addresses_and_loopback_are_valid():
    t = tracker(PEER)
    addrs = [f"192.0.2.{i}" for i in range(1, 16)] + ["127.0.0.1"]
    c = start_attempt(t, PEER, 0.0, addrs, 65535)
    assert c.addresses == tuple(addrs)
    assert c.port == 65535


def test_lk1_own_device_id_is_ignored_everywhere():
    t = LinkTracker(OWN)
    assert t.trusted_changed([OWN, PEER], 0.0) == []
    assert t.device_seen(OWN, ADDR, PORT, 0.0) == []
    assert t.incoming_control(OWN, 3, 0.0) == []
    assert t.connection_lost(OWN, 3, 0.0) == []
    assert t.retry_now(OWN, 0.0) == []
    assert t.device_gone(OWN, 0.0) == []
    assert t.connect_finished(OWN, 1, UNREACHABLE, None, 0.0) == []
    assert t.connect_finished(OWN, 1, OK, 4, 0.0) == []
    assert t.connected() == {} and t.attempting() == {}
    assert t.next_wakeup() is None


# --- LK2, LK3 -------------------------------------------------------------------------------------------


def test_lk2_trusted_and_seen_connects():
    t = tracker(PEER)
    assert t.device_seen(PEER, ADDR2, PORT2, 0.0) == [Connect(PEER, 1, ADDR2, PORT2)]
    assert t.attempting() == {PEER: 1}
    assert t.connected() == {}


def test_lk2_seen_but_untrusted_does_not_connect():
    t = tracker(PEER)
    assert t.device_seen(Q, ADDR, PORT, 0.0) == []
    assert t.attempting() == {}
    assert t.tick(3600.0) == []
    assert t.network_changed(3600.0) == []


def test_lk2_seen_first_then_trusted_connects_in_trusted_changed():
    t = LinkTracker(OWN)
    assert t.device_seen(PEER, ADDR, PORT, 0.0) == []
    assert t.trusted_changed([PEER], 5.0) == [Connect(PEER, 1, ADDR, PORT)]
    assert t.attempting() == {PEER: 1}


def test_lk2_own_device_seen_is_ignored():
    t = tracker(PEER)
    assert t.device_seen(OWN, ADDR, PORT, 0.0) == []
    assert t.trusted_changed([OWN], 0.0) == []
    assert t.attempting() == {}


def test_lk3_ok_gives_link_up_and_no_more_attempts():
    t = tracker(PEER)
    c = start_attempt(t)
    acts = t.connect_finished(PEER, c.attempt_id, OK, 7, 0.5)
    assert acts == [Notify(LinkUp(PEER, 7, True))]
    assert t.connected() == {PEER: 7}
    assert t.attempting() == {}
    assert t.next_wakeup() is None
    assert t.tick(3600.0) == []
    for i in range(20):
        assert t.device_seen(PEER, ADDR2 if i % 2 else ADDR, PORT, 3600.0 + i * 10) == []
    assert t.network_changed(4000.0) == []
    assert t.connected() == {PEER: 7}


# --- LK4, LK5: lịch UNREACHABLE và báo một lần --------------------------------------------------------


def test_lk4_unreachable_schedule_doubles_up_to_30s():
    t = tracker(PEER)
    c = start_attempt(t)
    aid = c.attempt_id
    now = 0.0
    for d in [1, 2, 4, 8, 16, 30, 30, 30]:
        now += 0.25  # lần thử mất 0,25 s
        acts = t.connect_finished(PEER, aid, UNREACHABLE, None, now)
        assert only(acts, Connect) == []
        assert t.attempting() == {}
        assert t.next_wakeup() == pytest.approx(now + d)
        assert t.tick(now + d - 0.01) == []
        now += d
        nxt = the_connect(t.tick(now))
        assert nxt.attempt_id > aid
        assert (nxt.device_id, nxt.addresses, nxt.port) == (PEER, ADDR, PORT)
        assert t.attempting() == {PEER: nxt.attempt_id}
        aid = nxt.attempt_id


def test_lk4_attempt_ids_strictly_increase_across_devices():
    t = tracker(PEER, Q)
    ids = [start_attempt(t, PEER, 0.0).attempt_id, start_attempt(t, Q, 0.0).attempt_id]
    assert ids[0] == 1
    for dev, aid in ((PEER, ids[0]), (Q, ids[1])):
        t.connect_finished(dev, aid, UNREACHABLE, None, 0.1)
    acts = t.tick(1.1)
    ids += [c.attempt_id for c in only(acts, Connect)]
    assert len(ids) == 4
    assert ids == sorted(ids) and len(set(ids)) == 4


def test_lk5_unreachable_reported_once_on_third_failure():
    t = tracker(PEER)
    c = start_attempt(t)
    aid, now = c.attempt_id, 0.0
    for k in range(1, 11):
        now += 0.1
        acts = t.connect_finished(PEER, aid, UNREACHABLE, None, now)
        expected = [LinkFailed(PEER, UNREACHABLE)] if k == 3 else []
        assert notifies(acts) == expected, f"lần thất bại {k}"
        acts, now = tick_to_wakeup(t)
        aid = the_connect(acts).attempt_id

    # Thành công rồi mất kết nối: bộ đếm và danh sách đã báo được xóa.
    assert notifies(t.connect_finished(PEER, aid, OK, 1, now + 0.1)) == [LinkUp(PEER, 1, True)]
    assert notifies(t.connection_lost(PEER, 1, now + 1)) == [LinkDown(PEER, 1)]
    acts, now = tick_to_wakeup(t)
    aid = the_connect(acts).attempt_id
    for k in range(1, 6):
        now += 0.1
        acts = t.connect_finished(PEER, aid, UNREACHABLE, None, now)
        expected = [LinkFailed(PEER, UNREACHABLE)] if k == 3 else []
        assert notifies(acts) == expected, f"sau LinkUp, lần thất bại {k}"
        acts, now = tick_to_wakeup(t)
        aid = the_connect(acts).attempt_id


def test_lk5_failure_after_device_gone_is_not_reported():
    t = tracker(PEER)
    last = fail_n_unreachable(t, PEER, 2)
    acts, now = tick_to_wakeup(t)
    aid = the_connect(acts).attempt_id
    assert last < now
    assert t.device_gone(PEER, now + 0.1) == []
    assert t.attempting() == {PEER: aid}, "device_gone không hủy lần thử đang chạy"
    acts = t.connect_finished(PEER, aid, UNREACHABLE, None, now + 0.2)
    assert acts == []
    assert t.next_wakeup() is None
    assert t.tick(now + 3600) == []


def test_lk5_reported_failures_survive_gone_and_seen_again():
    """R10: `device_gone` không xóa `failures`/`reported`: thấy lại rồi thất bại tiếp → không báo lại."""
    t = tracker(PEER)
    c = start_attempt(t)
    aid, now = c.attempt_id, 0.0
    for _ in range(3):
        now += 0.1
        t.connect_finished(PEER, aid, UNREACHABLE, None, now)
        acts, now = tick_to_wakeup(t)
        aid = the_connect(acts).attempt_id
    t.connect_finished(PEER, aid, UNREACHABLE, None, now + 0.1)
    t.device_gone(PEER, now + 0.2)
    c = start_attempt(t, PEER, now + 100)
    assert notifies(t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, now + 100.1)) == []


# --- LK6, LK7 -------------------------------------------------------------------------------------------


def test_lk6_device_gone_stops_scheduled_retries():
    t = tracker(PEER)
    fail_n_unreachable(t, PEER, 2)
    assert t.next_wakeup() is not None
    assert t.device_gone(PEER, 1.5) == []
    assert t.next_wakeup() is None
    assert t.tick(3600.0) == []
    assert t.network_changed(3600.0) == []


def test_lk6_device_gone_keeps_live_connection():
    t = tracker(PEER)
    c = start_attempt(t)
    t.connect_finished(PEER, c.attempt_id, OK, 3, 0.1)
    assert t.device_gone(PEER, 0.2) == []
    assert t.connected() == {PEER: 3}
    assert t.tick(3600.0) == []
    assert t.connected() == {PEER: 3}


def test_lk6_untrusted_device_gone_is_forgotten():
    """R10: `device_gone` của thiết bị chưa tin cậy xóa hẳn mục: tin cậy sau đó không nối (không còn thấy)."""
    t = LinkTracker(OWN)
    t.device_seen(PEER, ADDR, PORT, 0.0)
    t.device_gone(PEER, 1.0)
    assert t.trusted_changed([PEER], 2.0) == []
    assert t.attempting() == {}


def test_lk7_new_address_while_waiting_long_retries_now():
    t = tracker(PEER)
    last = fail_n_unreachable(t, PEER, 5)  # lần thứ 5 thất bại → chờ 16 s
    assert t.next_wakeup() == pytest.approx(last + 0.1 + 16)
    now = last + links.EVENT_RETRY_GAP + 1
    acts = t.device_seen(PEER, ADDR2, PORT2, now)
    c = the_connect(acts)
    assert acts == [Connect(PEER, c.attempt_id, ADDR2, PORT2)]
    assert t.attempting() == {PEER: c.attempt_id}


def test_lk7_new_address_too_soon_waits_for_event_gap():
    t = tracker(PEER)
    last = fail_n_unreachable(t, PEER, 5)
    assert t.device_seen(PEER, ADDR2, PORT2, last + 1.0) == []
    assert t.next_wakeup() == pytest.approx(last + links.EVENT_RETRY_GAP)
    assert t.tick(last + links.EVENT_RETRY_GAP - 0.01) == []
    c = the_connect(t.tick(last + links.EVENT_RETRY_GAP))
    assert (c.addresses, c.port) == (ADDR2, PORT2)


# --- LK8, LK9: chặn ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("result", BLOCKING)
def test_lk8_blocking_result_reports_once_and_waits_for_event(result):
    t = tracker(PEER)
    c = start_attempt(t)
    acts = t.connect_finished(PEER, c.attempt_id, result, None, 0.1)
    assert acts == [Notify(LinkFailed(PEER, result))]
    assert t.next_wakeup() is None
    assert t.tick(0.5) == []
    # Sự kiện mDNS sớm: chờ max(EVENT_RETRY_GAP, delay(1)) = 2 s tính từ lần thử trước (sửa F2:
    # lần thử do sự kiện luôn cách lần trước ≥ EVENT_RETRY_GAP, kể cả khi bị chặn).
    wait = max(links.EVENT_RETRY_GAP, delay(1))
    assert t.device_seen(PEER, ADDR, PORT, 0.5) == []
    assert t.next_wakeup() == pytest.approx(0.0 + wait)
    assert t.tick(wait - 0.01) == []
    c = the_connect(t.tick(wait))
    # Lần hai cũng bị chặn: không báo lại; chờ sự kiện, không chờ lịch.
    assert notifies(t.connect_finished(PEER, c.attempt_id, result, None, wait + 0.1)) == []
    assert t.next_wakeup() is None
    assert t.tick(3600.0) == []
    c = the_connect(t.device_seen(PEER, ADDR, PORT, 3600.0))
    assert c.attempt_id == t.attempting()[PEER]


@pytest.mark.parametrize("result", BLOCKING)
def test_lk8_flood_of_mdns_events_while_blocked_is_rate_limited(result):
    sim = Sim(tracker(PEER), lambda dev, aid: result)
    for i in range(1000):
        sim.call("device_seen", PEER, ADDR, PORT, at=i * 0.06)
    sim.advance(60.0)
    assert len(sim.connects()) <= 7, sim.connect_times()
    assert sim.failed() == [LinkFailed(PEER, result)]


@pytest.mark.parametrize("result", BLOCKING)
def test_lk8_blocked_first_failure_event_retry_waits_event_gap(result):
    """Sửa F2 (protocol §6.2, chốt 4): bị chặn với failures=1, delay(1) = 1 s < EVENT_RETRY_GAP = 2 s.

    Sự kiện mDNS ở `last_start + 1 s` không được kích thử; ở `last_start + EVENT_RETRY_GAP` thì có."""
    t = tracker(PEER)
    c = start_attempt(t, now=10.0)  # last_start = 10
    t.connect_finished(PEER, c.attempt_id, result, None, 10.1)
    assert only(t.device_seen(PEER, ADDR, PORT, 10.0 + delay(1)), Connect) == []
    assert only(t.network_changed(10.0 + 1.5), Connect) == []
    assert only(t.device_seen(PEER, ADDR, PORT, 10.0 + links.EVENT_RETRY_GAP - 0.01), Connect) == []
    assert t.attempting() == {}
    c2 = the_connect(t.device_seen(PEER, ADDR, PORT, 10.0 + links.EVENT_RETRY_GAP))
    assert t.attempting() == {PEER: c2.attempt_id}


@pytest.mark.parametrize("result", BLOCKING)
def test_lk8_blocked_early_event_schedules_no_earlier_than_event_gap(result):
    """Sửa F2: sự kiện tới sớm thì hẹn lần thử ở `last_start + max(EVENT_RETRY_GAP, delay(1))`."""
    t = tracker(PEER)
    c = start_attempt(t, now=0.0)
    t.connect_finished(PEER, c.attempt_id, result, None, 0.1)
    assert only(t.device_seen(PEER, ADDR, PORT, 0.2), Connect) == []
    assert t.next_wakeup() is not None and t.next_wakeup() >= links.EVENT_RETRY_GAP - 1e-9
    assert t.tick(delay(1)) == []
    assert t.tick(links.EVENT_RETRY_GAP - 0.01) == []
    the_connect(t.tick(links.EVENT_RETRY_GAP))


def _blocked_n_times(t: LinkTracker, result: ConnectResult, n: int) -> float:
    """Thiết bị đã tin cậy: thử rồi bị chặn n lần liên tiếp (lần sau do sự kiện, sớm nhất được phép).
    Trả `last_start` của lần thử cuối."""
    last = 0.0
    c = start_attempt(t, now=last)
    for i in range(1, n + 1):
        t.connect_finished(PEER, c.attempt_id, result, None, last + 0.1)
        if i == n:
            break
        last += max(links.EVENT_RETRY_GAP, delay(i))
        c = the_connect(t.device_seen(PEER, ADDR, PORT, last))
    return last


@pytest.mark.parametrize("n", [1, 2, 3, 4, 7])
@pytest.mark.parametrize("result", BLOCKING)
def test_lk8_blocked_event_retry_waits_max_of_gap_and_schedule(result, n):
    """Sửa F2: lần thử do sự kiện của thiết bị bị chặn sớm nhất ở `last_start + max(GAP, delay(n))`."""
    t = tracker(PEER)
    last = _blocked_n_times(t, result, n)
    wait = max(links.EVENT_RETRY_GAP, delay(n))
    assert only(t.device_seen(PEER, ADDR, PORT, last + wait - 0.01), Connect) == []
    assert only(t.network_changed(last + wait - 0.005), Connect) == []
    the_connect(t.device_seen(PEER, ADDR, PORT, last + wait))


@pytest.mark.parametrize("result", BLOCKING)
def test_lk19_blocked_after_dirty_attempt_waits_event_gap(result):
    """Sửa F2: có sự kiện trong lúc thử (dirty), lần thử bị chặn → lần sau không sớm hơn
    `last_start + max(EVENT_RETRY_GAP, delay(1))`, không phải `last_start + delay(1)`."""
    t = tracker(PEER)
    c = start_attempt(t, now=0.0)
    assert t.device_seen(PEER, ADDR, PORT, 0.5) == []  # đang thử: chỉ đánh dấu
    acts = t.connect_finished(PEER, c.attempt_id, result, None, 0.6)
    assert only(acts, Connect) == []
    nw = t.next_wakeup()
    assert nw is not None and nw >= links.EVENT_RETRY_GAP - 1e-9, nw
    assert t.tick(delay(1)) == []
    assert only(t.device_seen(PEER, ADDR, PORT, 1.5), Connect) == []
    the_connect(t.tick(max(nw, links.EVENT_RETRY_GAP)))


@pytest.mark.parametrize("result", BLOCKING)
def test_lk9_retry_now_unblocks_and_reports_again(result):
    t = tracker(PEER)
    c = start_attempt(t)
    t.connect_finished(PEER, c.attempt_id, result, None, 0.1)
    c = the_connect(t.retry_now(PEER, 0.2))  # không xét khoảng cách
    assert t.attempting() == {PEER: c.attempt_id}
    assert notifies(t.connect_finished(PEER, c.attempt_id, result, None, 0.3)) == [LinkFailed(PEER, result)]


def test_lk9_retry_now_while_attempting_or_connected_does_nothing():
    t = tracker(PEER)
    c = start_attempt(t)
    assert t.retry_now(PEER, 0.1) == []
    assert t.attempting() == {PEER: c.attempt_id}
    t.connect_finished(PEER, c.attempt_id, OK, 4, 0.2)
    assert t.retry_now(PEER, 0.3) == []
    assert t.connected() == {PEER: 4} and t.attempting() == {}


def test_lk9_retry_now_untrusted_or_unseen_does_nothing():
    t = tracker(PEER)
    assert t.retry_now(PEER, 0.0) == []  # tin cậy nhưng chưa thấy
    t.device_seen(Q, ADDR, PORT, 0.0)
    assert t.retry_now(Q, 0.0) == []  # thấy nhưng chưa tin cậy
    assert t.attempting() == {}


def test_lk9_retry_now_clears_unreachable_counter():
    t = tracker(PEER)
    fail_n_unreachable(t, PEER, 2, took=0.1)  # failures = 2, đang chờ lịch
    c = the_connect(t.retry_now(PEER, 1.5))
    # failures về 0: lần thất bại kế là lần 1 → lịch +1 s, không báo
    assert notifies(t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 1.6)) == []
    assert t.next_wakeup() == pytest.approx(1.6 + delay(1))


# --- LK10–LK13: kết nối vào, chống trùng -------------------------------------------------------------------


def test_lk10_first_incoming_gives_link_up_incoming():
    t = tracker(PEER)
    assert t.incoming_control(PEER, 5, 0.0) == [Notify(LinkUp(PEER, 5, False))]
    assert t.connected() == {PEER: 5}


def test_lk10_incoming_from_untrusted_is_closed_without_notify():
    t = tracker(PEER)
    assert t.incoming_control(Q, 5, 0.0) == [CloseConnection(Q, 5)]
    assert t.connected() == {}
    t.device_seen(Q, ADDR, PORT, 0.0)
    assert t.incoming_control(Q, 6, 0.0) == [CloseConnection(Q, 6)]


@pytest.mark.parametrize("own", [LO, HI], ids=["own<peer", "own>peer"])
@pytest.mark.parametrize("combo", ["in-in", "in-out", "out-in"])
def test_lk11_old_connection_replaced_after_replace_after(own, combo):
    """Kết nối cũ đã mở ≥ REPLACE_AFTER → kết nối mới thay, không xét bên nào mở; không báo người
    dùng, chỉ `Notify(LinkSwitched)` đứng sau `CloseConnection(cũ)` (spec P2-6 A3).

    Tổ hợp ra→ra không dựng được qua API công khai: tracker không bao giờ mở lần thử khi đang có
    kết nối (R1), nên kết nối ra mới chỉ tới khi kết nối cũ là kết nối vào (xem báo cáo)."""
    t = LinkTracker(own)
    t.trusted_changed([PEER], 0.0)
    gap = links.REPLACE_AFTER
    if combo == "in-in":
        t.incoming_control(PEER, 1, 0.0)
        acts = t.incoming_control(PEER, 2, gap)
    elif combo == "in-out":
        c = start_attempt(t, PEER, 0.0)
        t.incoming_control(PEER, 1, 0.1)
        acts = t.connect_finished(PEER, c.attempt_id, OK, 2, 0.1 + gap)
    else:  # out-in
        c = start_attempt(t, PEER, 0.0)
        t.connect_finished(PEER, c.attempt_id, OK, 1, 0.1)
        acts = t.incoming_control(PEER, 2, 0.1 + gap)
    new_is_outgoing = combo == "in-out"
    assert acts == [CloseConnection(PEER, 1), Notify(LinkSwitched(PEER, 1, 2, new_is_outgoing))]
    assert t.connected() == {PEER: 2}


@pytest.mark.parametrize("order", ["local-first", "remote-first"])
@pytest.mark.parametrize(("own", "keep_local"), [(LO, True), (HI, False)], ids=["own<peer", "own>peer"])
def test_lk12_tie_break_by_device_id(order, own, keep_local):
    """Hai kết nối khác bên mở, cách nhau < REPLACE_AFTER: giữ kết nối do bên có id nhỏ hơn mở.

    Spec P2-6 A3: luật hòa giữ kết nối MỚI → thêm `Notify(LinkSwitched)` sau `CloseConnection(cũ)`;
    giữ kết nối cũ → chỉ `CloseConnection(mới)`."""
    t = LinkTracker(own)
    t.trusted_changed([PEER], 0.0)
    c = start_attempt(t, PEER, 0.0)
    if order == "local-first":
        t.connect_finished(PEER, c.attempt_id, OK, 1, 0.1)
        local, remote = 1, 2
        acts = t.incoming_control(PEER, 2, 0.1 + links.REPLACE_AFTER - 0.2)
    else:
        t.incoming_control(PEER, 1, 0.1)
        local, remote = 2, 1
        acts = t.connect_finished(PEER, c.attempt_id, OK, 2, 0.1 + links.REPLACE_AFTER - 0.2)
    keep, drop = (local, remote) if keep_local else (remote, local)
    old, new = 1, 2
    if keep == new:
        assert acts == [CloseConnection(PEER, old), Notify(LinkSwitched(PEER, old, new, new == local))]
    else:
        assert acts == [CloseConnection(PEER, drop)]
    assert t.connected() == {PEER: keep}
    assert t.attempting() == {}


def test_lk13_same_side_new_connection_replaces_quickly():
    t = tracker(PEER)
    t.incoming_control(PEER, 1, 0.0)
    assert t.incoming_control(PEER, 2, 0.1) == [
        CloseConnection(PEER, 1),
        Notify(LinkSwitched(PEER, 1, 2, False)),
    ]
    assert t.connected() == {PEER: 2}
    assert t.incoming_control(PEER, 3, 0.15) == [
        CloseConnection(PEER, 2),
        Notify(LinkSwitched(PEER, 2, 3, False)),
    ]
    assert t.connected() == {PEER: 3}


def test_lk13_replacement_measures_age_from_the_new_connection():
    """Sau khi thay, mốc “lúc mở” là của kết nối mới (R7 so `now - old.since` với kết nối hiện tại)."""
    t = LinkTracker(HI)  # own > peer: khi hòa thì giữ kết nối do máy kia mở
    t.trusted_changed([PEER], 0.0)
    gap = links.REPLACE_AFTER
    c = start_attempt(t, PEER, 0.0)
    t.incoming_control(PEER, 1, 0.1)
    # Cùng bên: thay (kèm LinkSwitched, spec P2-6 A3); mốc gap - 1.
    assert t.incoming_control(PEER, 2, gap - 1) == [
        CloseConnection(PEER, 1),
        Notify(LinkSwitched(PEER, 1, 2, False)),
    ]
    # Lần thử ra xong ở gap + 1: kết nối hiện tại (2) mới 2 s tuổi → hòa → giữ kết nối do máy kia mở.
    # Nếu mốc vẫn là 0.1 (của kết nối 1) thì gap + 0,9 s ≥ REPLACE_AFTER và kết nối mới (3) bị giữ nhầm.
    assert t.connect_finished(PEER, c.attempt_id, OK, 3, gap + 1) == [CloseConnection(PEER, 3)]
    assert t.connected() == {PEER: 2}


# --- LK14, LK15 --------------------------------------------------------------------------------------------


def test_lk14_connection_lost_of_current_schedules_quick_retry():
    t = tracker(PEER)
    c = start_attempt(t)
    t.connect_finished(PEER, c.attempt_id, OK, 3, 0.1)
    assert t.connection_lost(PEER, 99, 1.0) == []
    assert t.connection_lost(PEER, 3, 2.0) == [Notify(LinkDown(PEER, 3))]
    assert t.connected() == {}
    assert t.next_wakeup() == pytest.approx(2.0 + links.RETRY_BASE)
    assert t.connection_lost(PEER, 3, 2.1) == []  # lần hai: không còn là kết nối hiện tại
    c = the_connect(t.tick(2.0 + links.RETRY_BASE))
    assert c.device_id == PEER


def test_lk14_connection_lost_when_not_seen_has_no_retry():
    t = tracker(PEER)
    t.incoming_control(PEER, 3, 0.0)
    assert t.connection_lost(PEER, 3, 1.0) == [Notify(LinkDown(PEER, 3))]
    assert t.next_wakeup() is None
    assert t.tick(3600.0) == []


def test_lk14_connection_lost_of_unknown_device_or_conn():
    t = tracker(PEER)
    assert t.connection_lost(Q, 1, 0.0) == []
    assert t.connection_lost(PEER, 1, 0.0) == []


def test_lk15_incoming_while_attempting_then_attempt_ok_applies_tie_break():
    t = LinkTracker(LO)  # own < peer: kết nối ra (của máy này) được giữ khi hòa
    t.trusted_changed([PEER], 0.0)
    c = start_attempt(t, PEER, 0.0)
    acts = t.incoming_control(PEER, 1, 0.1)
    assert acts == [Notify(LinkUp(PEER, 1, False))], "kết nối vào không được hủy lần thử đang chạy"
    assert t.attempting() == {PEER: c.attempt_id}
    acts = t.connect_finished(PEER, c.attempt_id, OK, 2, 0.2)
    assert acts == [CloseConnection(PEER, 1), Notify(LinkSwitched(PEER, 1, 2, True))]
    assert t.connected() == {PEER: 2}


@pytest.mark.parametrize("result", [UNREACHABLE, *BLOCKING])
def test_lk15_failed_attempt_after_incoming_is_ignored(result):
    t = tracker(PEER)
    c = start_attempt(t)
    t.incoming_control(PEER, 1, 0.1)
    assert t.connect_finished(PEER, c.attempt_id, result, None, 0.2) == []
    assert t.next_wakeup() is None
    assert t.attempting() == {}
    assert t.connected() == {PEER: 1}
    # Không bị đếm, không bị chặn: mất kết nối → nối lại theo lịch +1 s
    t.connection_lost(PEER, 1, 1.0)
    assert t.next_wakeup() == pytest.approx(1.0 + links.RETRY_BASE)


# --- LK16, LK17: thôi tin cậy, attempt_id cũ ------------------------------------------------------------


@pytest.mark.parametrize("connected_dev", [LO, HI])
def test_lk16_untrust_closes_and_cancels_in_order(connected_dev):
    attempt_dev = HI if connected_dev == LO else LO
    t = tracker(LO, HI)
    t.incoming_control(connected_dev, 1, 0.0)
    c = start_attempt(t, attempt_dev, 0.0)
    acts = t.trusted_changed([], 1.0)
    per_dev = {
        connected_dev: [CloseConnection(connected_dev, 1), Notify(LinkDown(connected_dev, 1))],
        attempt_dev: [CancelConnect(attempt_dev, c.attempt_id)],
    }
    assert acts == per_dev[LO] + per_dev[HI]  # duyệt theo device_id tăng dần
    assert t.connected() == {} and t.attempting() == {}
    assert t.next_wakeup() is None

    # Lần thử cũ hoàn tất muộn → đóng ngay; kết nối vào → đóng; thấy lại → không thử.
    assert t.connect_finished(attempt_dev, c.attempt_id, OK, 9, 1.1) == [CloseConnection(attempt_dev, 9)]
    assert t.incoming_control(connected_dev, 10, 1.2) == [CloseConnection(connected_dev, 10)]
    assert t.device_seen(connected_dev, ADDR, PORT, 1.3) == []
    assert t.device_seen(attempt_dev, ADDR, PORT, 1.3) == []
    assert t.tick(3600.0) == []
    assert t.connected() == {}


def test_lk16_trust_again_while_still_seen_connects_immediately():
    """R11 giữ `seen` của thiết bị thôi tin cậy; tin cậy lại → R2: thử ngay, không xét khoảng cách."""
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0)
    t.connect_finished(PEER, c.attempt_id, ConnectResult.REJECTED, None, 0.1)
    t.trusted_changed([], 0.2)
    c = the_connect(t.trusted_changed([PEER], 0.3))
    assert (c.addresses, c.port) == (ADDR, PORT)
    # Tin cậy lại: bộ đếm và danh sách đã báo được xóa → báo lại
    assert notifies(t.connect_finished(PEER, c.attempt_id, ConnectResult.REJECTED, None, 0.4)) == [
        LinkFailed(PEER, ConnectResult.REJECTED)
    ]


def test_lk16_trusted_changed_keeps_unchanged_devices():
    t = tracker(PEER, Q)
    t.incoming_control(PEER, 1, 0.0)
    assert t.trusted_changed([PEER, Q, R], 1.0) == []
    assert t.trusted_changed([PEER, Q], 2.0) == []
    assert t.connected() == {PEER: 1}


def test_lk17_stale_attempt_ids():
    t = tracker(PEER)
    c = start_attempt(t)
    assert t.connect_finished(PEER, c.attempt_id + 1, OK, 5, 0.1) == [CloseConnection(PEER, 5)]
    assert t.connect_finished(PEER, c.attempt_id + 1, UNREACHABLE, None, 0.1) == []
    assert t.connect_finished(PEER, 999, ConnectResult.REJECTED, None, 0.1) == []
    assert t.attempting() == {PEER: c.attempt_id}
    assert t.connected() == {}
    # Lần thử thật vẫn chạy bình thường
    assert notifies(t.connect_finished(PEER, c.attempt_id, OK, 6, 0.2)) == [LinkUp(PEER, 6, True)]
    # attempt_id đã xong → giờ là cũ
    assert t.connect_finished(PEER, c.attempt_id, OK, 7, 0.3) == [CloseConnection(PEER, 7)]
    assert t.connected() == {PEER: 6}


# --- LK18–LK20 ----------------------------------------------------------------------------------------------

D1, D2, D3, D4 = "1" * 32, "2" * 32, "3" * 32, "4" * 32


def test_lk18_network_changed_retries_eligible_devices_by_gap():
    t = tracker(D1, D2, D3, D4)
    a1 = start_attempt(t, D1, 0.0).attempt_id
    a2 = start_attempt(t, D2, 0.0).attempt_id
    start_attempt(t, D3, 0.0)
    t.connect_finished(D1, a1, OK, 1, 0.1)  # D1 đang kết nối
    t.connect_finished(D2, a2, UNREACHABLE, None, 0.1)  # D2: lần 1, chờ 1.1
    a2 = the_connect(t.tick(1.1)).attempt_id
    t.connect_finished(D2, a2, UNREACHABLE, None, 1.2)  # lần 2, chờ 3.2
    a2 = the_connect(t.tick(3.2)).attempt_id
    t.connect_finished(D2, a2, UNREACHABLE, None, 3.3)  # lần 3, chờ 7.3; last_start = 3.2
    a4 = start_attempt(t, D4, 5.0).attempt_id
    t.connect_finished(D4, a4, UNREACHABLE, None, 5.1)  # D4: chờ 6.1; last_start = 5.0
    # D3 vẫn đang thử.
    acts = t.network_changed(5.5)
    c = the_connect(acts)
    assert c == Connect(D2, c.attempt_id, ADDR, PORT)  # 5.5 ≥ 3.2 + 2
    assert not any(isinstance(a, Connect) and a.device_id in (D1, D3, D4) for a in acts)
    assert t.next_wakeup() == pytest.approx(6.1)  # D4: min(6.1, 5.0 + 2)
    assert t.connected() == {D1: 1}


def test_lk18_network_changed_orders_by_device_id():
    t = tracker(D3, D1, D2)
    aids = {dev: start_attempt(t, dev, 0.0).attempt_id for dev in (D3, D1, D2)}
    for dev in (D3, D1, D2):
        t.connect_finished(dev, aids[dev], UNREACHABLE, None, 0.1)
    acts = t.network_changed(0.5 + links.EVENT_RETRY_GAP)
    assert [a.device_id for a in only(acts, Connect)] == [D1, D2, D3]
    ids = [a.attempt_id for a in only(acts, Connect)]
    assert ids == sorted(ids)


def test_lk18_network_changed_while_waiting_too_soon_only_reschedules():
    t = tracker(PEER)
    last = fail_n_unreachable(t, PEER, 5)  # chờ 16 s
    assert t.network_changed(last + 1.0) == []
    assert t.next_wakeup() == pytest.approx(last + links.EVENT_RETRY_GAP)


def test_lk19_dirty_with_long_schedule_retries_after_event_gap():
    """`dirty` + UNREACHABLE: `next_at = min(now + delay, max(now, last_start + GAP))` → +2 s, không +16 s."""
    t = tracker(PEER)
    fail_n_unreachable(t, PEER, 4)  # lần 4 thất bại → chờ 8 s
    acts, s = tick_to_wakeup(t)  # lần thử 5 bắt đầu ở s
    c = the_connect(acts)
    assert t.device_seen(PEER, ADDR2, PORT, s + 0.1) == []  # đang thử → chỉ đánh dấu dirty
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, s + 0.5)
    assert t.next_wakeup() == pytest.approx(s + links.EVENT_RETRY_GAP)  # không phải s + 0,5 + 16
    assert t.tick(s + links.EVENT_RETRY_GAP - 0.01) == []
    assert the_connect(t.tick(s + links.EVENT_RETRY_GAP)).addresses == ADDR2


def test_lk19_dirty_slow_attempt_retries_immediately_after_it_ends():
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0)
    t.device_seen(PEER, ADDR2, PORT, 0.2)
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 3.0)
    assert t.next_wakeup() == pytest.approx(3.0)  # max(3.0, 0 + 2), không phải 3.0 + 1
    assert the_connect(t.tick(3.0)).addresses == ADDR2


def test_lk19_network_changed_during_attempt_marks_dirty():
    t = tracker(PEER)
    fail_n_unreachable(t, PEER, 4)
    acts, s = tick_to_wakeup(t)
    c = the_connect(acts)
    assert t.network_changed(s + 0.1) == []
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, s + 0.5)
    assert t.next_wakeup() == pytest.approx(s + links.EVENT_RETRY_GAP)


def test_lk20_next_wakeup_is_minimum_and_updates_after_tick():
    t = tracker(D1, D2, D3)
    # D1 thất bại ở 0.1 → 1.1; D2 ở 0.4 → 1.4; D3 hai lần → +2
    a = {dev: start_attempt(t, dev, 0.0).attempt_id for dev in (D1, D2, D3)}
    t.connect_finished(D1, a[D1], UNREACHABLE, None, 0.1)
    t.connect_finished(D3, a[D3], UNREACHABLE, None, 0.2)  # → 1.2
    t.connect_finished(D2, a[D2], UNREACHABLE, None, 0.4)  # → 1.4
    assert t.next_wakeup() == pytest.approx(1.1)
    assert [c.device_id for c in only(t.tick(1.1), Connect)] == [D1]
    assert t.next_wakeup() == pytest.approx(1.2)
    assert [c.device_id for c in only(t.tick(1.3), Connect)] == [D3]
    assert t.next_wakeup() == pytest.approx(1.4)
    assert [c.device_id for c in only(t.tick(1.4), Connect)] == [D2]
    assert t.next_wakeup() is None
    assert t.attempting().keys() == {D1, D2, D3}


def test_lk20_tick_late_starts_all_due_in_id_order():
    t = tracker(D3, D1)
    aids = {dev: start_attempt(t, dev, 0.0).attempt_id for dev in (D3, D1)}
    for dev in (D3, D1):
        t.connect_finished(dev, aids[dev], UNREACHABLE, None, 0.1)
    assert [c.device_id for c in only(t.tick(50.0), Connect)] == [D1, D3]
    assert t.next_wakeup() is None


# --- Mô phỏng một tracker (LK8, LK23) -------------------------------------------------------------------


class Sim:
    """Một tracker và đồng hồ ảo: tự gọi `tick` đúng `next_wakeup()`, tự trả kết quả mỗi `Connect`.

    `result_for(device_id, attempt_id)` cho kết quả; `latency(attempt_id)` cho thời gian thử.
    """

    def __init__(
        self,
        t: LinkTracker,
        result_for: Callable[[str, int], ConnectResult],
        latency: Callable[[int], float] = lambda aid: 0.0,
    ) -> None:
        self.t = t
        self.now = 0.0
        self.result_for = result_for
        self.latency = latency
        self.pending: dict[int, tuple[float, str]] = {}
        self.log: list[tuple[float, object]] = []
        self._conn = itertools.count(10_000)

    def apply(self, actions: list) -> None:
        for a in actions:
            self.log.append((self.now, a))
            if isinstance(a, Connect):
                assert a.attempt_id not in self.pending
                assert all(dev != a.device_id for _, dev in self.pending.values()), "hai lần thử cùng lúc"
                self.pending[a.attempt_id] = (self.now + self.latency(a.attempt_id), a.device_id)
            elif isinstance(a, CancelConnect):
                self.pending.pop(a.attempt_id, None)

    def advance(self, until: float) -> None:
        stuck = 0
        while True:
            cands = []
            if self.pending:
                aid, (ft, dev) = min(self.pending.items(), key=lambda kv: (kv[1][0], kv[0]))
                cands.append((ft, 0, aid, dev))
            nw = self.t.next_wakeup()
            if nw is not None:
                cands.append((max(nw, self.now), 1, 0, ""))
            cands = [c for c in cands if c[0] <= until]
            if not cands:
                break
            when, kind, aid, dev = min(cands)
            self.now = max(self.now, when)
            if kind == 0:
                del self.pending[aid]
                r = self.result_for(dev, aid)
                cid = next(self._conn) if r == OK else None
                self.apply(self.t.connect_finished(dev, aid, r, cid, self.now))
                stuck = 0
            else:
                before = self.t.next_wakeup()
                self.apply(self.t.tick(self.now))
                after = self.t.next_wakeup()
                stuck = stuck + 1 if after is not None and after <= self.now and after == before else 0
                assert stuck < 3, f"tick({self.now}) không tiêu next_wakeup ({after})"
        self.now = max(self.now, until)

    def call(self, method: str, *args, at: float) -> list:
        self.advance(at)
        acts = getattr(self.t, method)(*args, self.now)
        self.apply(acts)
        return acts

    def connects(self) -> list[Connect]:
        return [a for _, a in self.log if isinstance(a, Connect)]

    def connect_times(self) -> list[float]:
        return [round(when, 3) for when, a in self.log if isinstance(a, Connect)]

    def failed(self) -> list[LinkFailed]:
        return [a.event for _, a in self.log if isinstance(a, Notify) and isinstance(a.event, LinkFailed)]


def test_sim_counts_blocked_schedule_without_events():
    """Kiểm bộ mô phỏng: bị chặn mà không có sự kiện nào → đúng 1 lần thử."""
    sim = Sim(tracker(PEER), lambda dev, aid: ConnectResult.REJECTED)
    sim.call("device_seen", PEER, ADDR, PORT, at=0.0)
    sim.advance(3600.0)
    assert len(sim.connects()) == 1


def test_unreachable_flood_attempts_at_most_once_per_event_gap():
    """Hệ quả mục 3.1: UNREACHABLE + sự kiện dồn dập → ≤ 1 lần thử mỗi EVENT_RETRY_GAP (tính tổng).

    Hai lần thử liền nhau có thể cách nhau chỉ `RETRY_BASE` khi lần sau do lịch R5 kích (không phải
    sự kiện): lần thử đầu thất bại → hẹn +1 s, sự kiện tới sau đó chỉ được dời hẹn sớm hơn (R1 dùng
    `min`). Vì vậy test kiểm tổng số lần và khoảng cách ≥ RETRY_BASE, không kiểm từng cặp ≥ 2 s."""
    sim = Sim(tracker(PEER), lambda dev, aid: UNREACHABLE, latency=lambda aid: 0.05)
    for i in range(3000):
        sim.call("device_seen", PEER, ADDR, PORT, at=i * 0.1)
    sim.advance(300.0)
    times = sim.connect_times()
    assert len(times) <= 300 / links.EVENT_RETRY_GAP + 1
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert min(gaps) >= links.RETRY_BASE - 1e-6, gaps
    assert sum(1 for g in gaps if g < links.EVENT_RETRY_GAP - 1e-6) <= 2, gaps
    assert sim.failed() == [LinkFailed(PEER, UNREACHABLE)]


def test_blocked_flood_attempt_counts_per_window():
    """Hệ quả mục 3.1: bị chặn + sự kiện dồn dập: ≤ 16 lần thử trong 300 s đầu, ≤ 11 mỗi 300 s sau."""
    sim = Sim(tracker(PEER), lambda dev, aid: ConnectResult.IDENTITY_MISMATCH, latency=lambda aid: 0.05)
    for i in range(12_000):
        if i % 7:
            sim.call("device_seen", PEER, ADDR, PORT, at=i * 0.1)
        else:
            sim.call("device_gone", PEER, at=i * 0.1)  # mDNS báo đi rồi đến lại: bộ đếm không bị xóa
    sim.advance(1200.0)
    times = sim.connect_times()
    assert sum(1 for x in times if x < 300) <= 16, times
    for w in range(1, 4):
        assert sum(1 for x in times if 300 * w <= x < 300 * (w + 1)) <= 11, times
    assert len(sim.failed()) == 1


# --- LK21: hypothesis, một tracker ------------------------------------------------------------------------

DEVS = ["1" * 32, "3" * 32, "c" * 32, "e" * 32, OWN]
ADDR_CHOICES = [("192.0.2.1",), ("192.0.2.2", "192.0.2.3", "192.0.2.2"), ("127.0.0.1",)]
PORTS = [47474, 50000]
RESULTS = [OK, UNREACHABLE, *BLOCKING]
OPS = ["seen", "gone", "trust", "network", "retry", "tick", "wake", "finish", "stale", "incoming", "lost"]
DTS = [0.0, 0.05, 0.3, 1.0, 2.0, 3.5, 6.0, 31.0]


def _dedup(addrs: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(addrs))


@HYPO
@given(
    st.lists(
        st.tuples(st.sampled_from(OPS), st.integers(0, 4), st.integers(0, 15), st.sampled_from(DTS)),
        max_size=80,
    )
)
def test_lk21_single_tracker_invariants(ops):
    t = LinkTracker(OWN)
    now = 0.0
    trusted: set[str] = set()
    seen: dict[str, tuple[tuple[str, ...], int]] = {}
    outstanding: dict[int, str] = {}  # attempt_id → device (theo Connect/CancelConnect/finish)
    finished_aids: dict[int, str] = {}
    conn_dev: dict[int, str] = {}  # mọi conn đã tạo
    closed: set[int] = set()  # đã CloseConnection
    lost: set[int] = set()  # đã giao connection_lost
    up: dict[str, int] = {}  # theo LinkUp/LinkDown
    reported: dict[str, set[ConnectResult]] = {}
    blocked: dict[str, bool] = {}
    last_connect: dict[str, float] = {}
    last_aid = 0
    conn_ids = itertools.count(1)

    for op, i, j, dt in ops:
        now += dt
        dev = DEVS[i]
        before_attempting = t.attempting()
        before_connected = t.connected()
        bypass: set[str] = set()  # thiết bị được thử ngay không xét khoảng cách (R2)
        event_triggered = False
        finishing: tuple[str, int] | None = None
        expect: list | None = None

        if op == "seen":
            addrs, port = ADDR_CHOICES[j % 3], PORTS[j % 2]
            acts = t.device_seen(dev, addrs, port, now)
            if dev != OWN:
                seen[dev] = (_dedup(addrs), port)
            event_triggered = True
        elif op == "gone":
            acts = t.device_gone(dev, now)
            seen.pop(dev, None)
            if dev != OWN:
                assert only(acts, Connect) == [] and only(acts, CancelConnect) == []
                assert only(acts, CloseConnection) == []
        elif op == "trust":
            ids = {DEVS[k] for k in range(4) if j >> k & 1}
            if i == 4:
                ids.add(OWN)
            ids.discard(OWN)
            newly, removed = ids - trusted, trusted - ids
            for d in newly | removed:
                reported[d] = set()
                blocked[d] = False
            bypass = newly
            trusted = ids
            acts = t.trusted_changed(sorted(ids | ({OWN} if i == 4 else set())), now)
        elif op == "network":
            acts = t.network_changed(now)
            event_triggered = True
        elif op == "retry":
            acts = t.retry_now(dev, now)
            if dev != OWN:
                reported[dev] = set()
                blocked[dev] = False
                bypass = {dev}
        elif op in ("tick", "wake"):
            if op == "wake" and t.next_wakeup() is not None:
                now = max(now, t.next_wakeup())
            acts = t.tick(now)
        elif op == "finish":
            if not outstanding:
                continue
            aid = sorted(outstanding)[j % len(outstanding)]
            dev = outstanding[aid]
            r = RESULTS[i]
            cid = next(conn_ids) if r == OK else None
            if cid is not None:
                conn_dev[cid] = dev
            had_current = dev in before_connected
            acts = t.connect_finished(dev, aid, r, cid, now)
            del outstanding[aid]
            finished_aids[aid] = dev
            finishing = (dev, aid)
            if not had_current:
                if r in (OK, UNREACHABLE):
                    blocked[dev] = False
                elif dev in trusted:
                    blocked[dev] = True
            if dev not in trusted and r == OK:
                expect = [CloseConnection(dev, cid)]
        elif op == "stale":
            if finished_aids and j % 2 == 0:
                aid = sorted(finished_aids)[j % len(finished_aids)]
                sdev = finished_aids[aid]
            else:
                aid, sdev = 100_000 + j, DEVS[i % 4]
            if aid in outstanding:
                continue
            r = RESULTS[i]
            cid = next(conn_ids) if r == OK else None
            if cid is not None:
                conn_dev[cid] = sdev
            acts = t.connect_finished(sdev, aid, r, cid, now)
            expect = [CloseConnection(sdev, cid)] if r == OK else []
            dev = sdev
        elif op == "incoming":
            cid = next(conn_ids)
            acts = t.incoming_control(dev, cid, now)
            if dev == OWN:
                expect = []
            else:
                conn_dev[cid] = dev
                if dev not in trusted:
                    expect = [CloseConnection(dev, cid)]
        else:  # lost
            live = [c for c in sorted(conn_dev) if c not in lost]
            if not live:
                continue
            cid = live[j % len(live)]
            dev = conn_dev[cid]
            lost.add(cid)
            is_current = before_connected.get(dev) == cid
            acts = t.connection_lost(dev, cid, now)
            if not is_current:
                expect = []
            else:
                assert notifies(acts) == [LinkDown(dev, cid)]

        if dev == OWN and op not in ("trust", "network", "tick", "wake"):
            assert acts == [], (op, acts)
        if expect is not None:
            assert acts == expect, (op, acts, expect)

        # Kiểm từng hành động.
        for a in acts:
            if isinstance(a, Connect):
                d = a.device_id
                assert d in trusted, f"Connect tới thiết bị chưa tin cậy {d[:4]}"
                assert d in seen, f"Connect tới thiết bị không thấy {d[:4]}"
                assert (a.addresses, a.port) == seen[d], (
                    "Connect phải mang địa chỉ/cổng của lần thấy gần nhất"
                )
                assert a.attempt_id > last_aid, "attempt_id phải tăng ngặt"
                last_aid = a.attempt_id
                assert d not in before_attempting or finishing == (d, before_attempting[d]), (
                    "hai lần thử cùng lúc cho một thiết bị"
                )
                assert d not in t.connected(), "thử khi đang có kết nối"
                assert t.attempting().get(d) == a.attempt_id
                if d not in bypass and d in last_connect:
                    gap = now - last_connect[d]
                    assert gap >= links.RETRY_BASE - 1e-9, f"thử lại sau {gap} s"
                    # Sửa F2: lần thử do sự kiện cách lần trước ≥ EVENT_RETRY_GAP, kể cả khi bị chặn;
                    # thiết bị bị chặn chỉ được thử lại do sự kiện (ngay, hoặc theo hẹn mà sự kiện đặt).
                    if event_triggered or blocked.get(d, False):
                        assert gap >= links.EVENT_RETRY_GAP - 1e-9, f"sự kiện kích thử lại sau {gap} s"
                last_connect[d] = now
                outstanding[a.attempt_id] = d
            elif isinstance(a, CancelConnect):
                assert outstanding.get(a.attempt_id) == a.device_id
                del outstanding[a.attempt_id]
                finished_aids[a.attempt_id] = a.device_id
            elif isinstance(a, CloseConnection):
                assert conn_dev.get(a.conn_id) == a.device_id, "đóng một kết nối lạ"
                assert a.conn_id not in closed, "đóng một kết nối hai lần"
                closed.add(a.conn_id)
            else:
                assert isinstance(a, Notify)
                ev = a.event
                if isinstance(ev, LinkUp):
                    assert ev.device_id not in up, "LinkUp hai lần liền"
                    up[ev.device_id] = ev.conn_id
                    reported[ev.device_id] = set()
                    blocked[ev.device_id] = False
                elif isinstance(ev, LinkDown):
                    assert up.get(ev.device_id) is not None, "LinkDown khi chưa LinkUp"
                    del up[ev.device_id]
                elif isinstance(ev, LinkSwitched):
                    # Spec P2-6 A3: đổi kết nối hiện tại; không phải LinkUp/LinkDown.
                    assert up.get(ev.device_id) == ev.old_conn_id, "LinkSwitched từ kết nối không hiện tại"
                    assert ev.conn_id != ev.old_conn_id
                    up[ev.device_id] = ev.conn_id
                else:
                    assert isinstance(ev, LinkFailed)
                    assert ev.result != OK
                    assert ev.result not in reported.setdefault(ev.device_id, set()), "báo lỗi lặp lại"
                    reported[ev.device_id].add(ev.result)

        # Kiểm trạng thái sau lời gọi.
        conn = t.connected()
        assert set(conn) == set(up), "connected() lệch chuỗi LinkUp/LinkDown"
        assert set(conn) <= trusted
        for d, c in conn.items():
            assert conn_dev.get(c) == d
            assert c not in closed and c not in lost, "kết nối hiện tại đã bị đóng hoặc đã mất"
        assert t.attempting() == {d: a for a, d in outstanding.items()}


# --- LK22: hypothesis, hai tracker hội tụ ------------------------------------------------------------------


@dataclasses.dataclass
class G:
    """Một kết nối toàn cục giữa hai máy."""

    gid: int
    opener: int
    cids: dict[int, int]
    open_at: dict[int, float]  # side → lúc giao sự kiện mở (OK / incoming) cho bên đó
    created: float
    alive: bool = True
    lost_at: dict[int, float] = dataclasses.field(default_factory=dict)


class TwoSim:
    """Mô phỏng hai máy theo đặc tả mục 4.2.1."""

    def __init__(self, ids: tuple[str, str], p: dict) -> None:
        self.ids = ids
        self.p = p
        self.tr = [LinkTracker(ids[0]), LinkTracker(ids[1])]
        self.now = 0.0
        self.q: list = []
        self.seq = itertools.count()
        self.cid = itertools.count(1)
        self.gs: list[G] = []
        self.by_cid: dict[tuple[int, int], G] = {}
        self.attempt_g: dict[tuple[int, int], G | None] = {}
        self.log: list[tuple[float, int, object]] = []
        self.tick_at: list[float | None] = [None, None]
        self.disturb_end = 1.0
        self._lat = itertools.cycle(p["lats"])
        self._skew = itertools.cycle(p["skews"])
        self._delta = itertools.cycle(p["deltas"])
        for side in (0, 1):
            assert self.tr[side].trusted_changed([ids[1 - side]], 0.0) == []
        for side, when in enumerate(p["seen"]):
            self.push(when, side, "seen", None)
        for when, side in p["extra_seen"]:
            self.push(when, side, "seen", None)
            self.disturb_end = max(self.disturb_end, when)
        for start, length, kas in p["partitions"]:
            self.push(start, -1, "partition", kas)
            self.disturb_end = max(self.disturb_end, start + length)

    def in_partition(self, t: float) -> bool:
        return any(s <= t < s + length for s, length, _ in self.p["partitions"])

    def push(self, when: float, side: int, kind: str, payload) -> None:
        heapq.heappush(self.q, (when, next(self.seq), side, kind, payload))

    def schedule_lost(self, g: G, side: int, desired: float, *, disturbance: bool) -> None:
        if side not in g.cids and side not in g.open_at:
            return
        when = max(desired, g.open_at.get(side, desired))
        if side in g.lost_at and g.lost_at[side] <= when:
            return
        g.lost_at[side] = when
        self.push(when, side, "lost", g)
        if disturbance:
            self.disturb_end = max(self.disturb_end, when)

    def run(self, until: float) -> None:
        steps = 0
        while self.q and self.q[0][0] <= until:
            steps += 1
            assert steps < 200_000, "mô phỏng không dừng"
            when, _, side, kind, payload = heapq.heappop(self.q)
            self.now = max(self.now, when)
            if kind == "partition":
                for g in self.gs:
                    if g.alive:
                        g.alive = False
                        for s in (0, 1):
                            self.schedule_lost(g, s, self.now + payload[s], disturbance=True)
                continue
            t = self.tr[side]
            peer = self.ids[1 - side]
            before_att = t.attempting()
            finishing = None
            if kind == "seen":
                acts = t.device_seen(peer, (f"192.0.2.{2 - side}",), PORT, self.now)
            elif kind == "tick":
                if self.tick_at[side] != when:
                    continue
                self.tick_at[side] = None
                acts = t.tick(self.now)
            elif kind == "finish":
                aid, g = payload
                finishing = aid
                if g is None:
                    acts = t.connect_finished(peer, aid, UNREACHABLE, None, self.now)
                else:
                    acts = t.connect_finished(peer, aid, OK, g.cids[side], self.now)
            elif kind == "incoming":
                g = payload
                acts = t.incoming_control(peer, g.cids[side], self.now)
            else:  # lost
                g = payload
                if g.lost_at.get(side) != when:
                    continue
                acts = t.connection_lost(peer, g.cids[side], self.now)
            self.apply(side, acts, before_att, finishing)
            nw = t.next_wakeup()
            if nw is not None:
                at = max(nw, self.now)
                if self.tick_at[side] != at:
                    self.tick_at[side] = at
                    self.push(at, side, "tick", None)

    def apply(self, side: int, acts: list, before_att: dict, finishing: int | None) -> None:
        t = self.tr[side]
        peer = self.ids[1 - side]
        assert len(t.connected()) <= 1
        for a in acts:
            self.log.append((self.now, side, a))
            if isinstance(a, Connect):
                assert peer not in t.connected(), "Connect khi đang có kết nối"
                assert peer not in before_att or before_att[peer] == finishing, "hai lần thử cùng lúc"
                lat = next(self._lat)
                if self.in_partition(self.now):
                    self.push(self.now + lat, side, "finish", (a.attempt_id, None))
                    continue
                other = 1 - side
                g = G(len(self.gs), side, {}, {}, self.now)
                g.cids[side] = next(self.cid)
                g.cids[other] = next(self.cid)
                self.gs.append(g)
                for s in (0, 1):
                    self.by_cid[(s, g.cids[s])] = g
                t_open = self.now + lat
                t_in = max(self.now, self.now + lat + next(self._skew))
                g.open_at[side] = t_open
                g.open_at[other] = t_in
                self.push(t_open, side, "finish", (a.attempt_id, g))
                self.push(t_in, other, "incoming", g)
            elif isinstance(a, CloseConnection):
                g = self.by_cid[(side, a.conn_id)]
                g.alive = False
                self.schedule_lost(g, 1 - side, self.now + next(self._delta), disturbance=False)
            elif isinstance(a, CancelConnect):
                raise AssertionError("không có CancelConnect khi hai bên luôn tin nhau")

    def current_g(self, side: int) -> G | None:
        conn = self.tr[side].connected()
        if not conn:
            return None
        ((_, cid),) = conn.items()
        return self.by_cid[(side, cid)]


_floats = st.floats
LK22_PARAMS = st.fixed_dictionaries(
    {
        "seen": st.tuples(_floats(0, 1), _floats(0, 1)),
        # Thời gian mở kết nối tới hạn mở kết nối của net (OPENING_TIMEOUT, protocol §2.3): lần mở
        # chậm nhất có thể của hai bên gọi nhau cùng lúc vẫn phải hội tụ (T4, protocol §6.1).
        "lats": st.lists(_floats(0, OPENING_TIMEOUT), min_size=1, max_size=8),
        "skews": st.lists(_floats(-0.3, 0.3), min_size=1, max_size=8),
        "deltas": st.lists(_floats(0, 0.5), min_size=1, max_size=8),
        "partitions": st.lists(
            st.tuples(_floats(0, 60), _floats(0.5, 20), st.tuples(_floats(0, 30), _floats(0, 30))),
            max_size=3,
        ),
        "extra_seen": st.lists(st.tuples(_floats(0, 60), st.integers(0, 1)), max_size=5),
    }
)


@pytest.mark.parametrize("first_is_lower", [True, False], ids=["X<Y", "X>Y"])
@settings(max_examples=1000, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(p=LK22_PARAMS)
# Ca cố định đỏ chắc chắn với ngưỡng cũ 5 s (lần mở 9,2 s): hypothesis hiếm khi tự tìm lại nó.
@example(
    p={
        "seen": (0.392, 0.321),
        "lats": [4.07, 9.20],
        "skews": [0.239],
        "deltas": [0.0005],
        "partitions": [],
        "extra_seen": [],
    }
)
def test_lk22_two_trackers_converge_to_one_connection(first_is_lower, p):
    ids = (LO, HI) if first_is_lower else (HI, LO)
    sim = TwoSim(ids, p)
    sim.run(500.0)  # chạy đủ lâu để mọi nhiễu đã xảy ra
    t_d = sim.disturb_end
    sim.run(t_d + 60 + 300)

    late = [(w, s, a) for w, s, a in sim.log if w > t_d and isinstance(a, Connect | CloseConnection)]
    last_action = max((w for w, _, _ in late), default=t_d)
    shown = [(round(w, 2), s, type(a).__name__) for w, s, a in late][:30]
    detail = f"t_d={t_d:.2f}, hành động sau nhiễu: {shown}"
    assert last_action <= t_d + 60, "chưa ổn định sau 60 s: " + detail
    gx, gy = sim.current_g(0), sim.current_g(1)
    assert gx is not None and gy is not None, "cuối mô phỏng có bên không có kết nối: " + detail
    assert gx is gy, "hai bên giữ hai kết nối khác nhau: " + detail
    assert gx.alive, "kết nối hai bên giữ đã chết: " + detail
    opened_after = sum(1 for g in sim.gs if g.created > t_d)
    assert opened_after <= 6, f"mở {opened_after} kết nối sau nhiễu cuối: " + detail


def test_lk22_simulation_simple_case_converges():
    """Ca cố định (kiểm bộ mô phỏng): gọi nhau cùng lúc, không nhiễu."""
    p = {
        "seen": (0.0, 0.0),
        "lats": [0.5],
        "skews": [0.0],
        "deltas": [0.1],
        "partitions": [],
        "extra_seen": [],
    }
    sim = TwoSim((LO, HI), p)
    sim.run(400.0)
    gx, gy = sim.current_g(0), sim.current_g(1)
    assert gx is gy and gx is not None and gx.alive
    assert gx.opener == 0, "bên có device_id nhỏ hơn mở kết nối được giữ"
    assert len(sim.gs) == 2


# --- LK23: hypothesis, giả mạo -----------------------------------------------------------------------------

GAPS = [0.1, 0.1, 0.1, 0.2, 0.5, 1.0, 3.0, 10.0, 40.0]


@HYPO
@given(
    pattern=st.lists(
        st.tuples(st.sampled_from(GAPS), st.sampled_from(["seen", "gone"])), min_size=1, max_size=30
    ),
    lat=st.sampled_from([0.0, 0.05, 0.5, 1.5]),
    addr_flip=st.booleans(),
)
def test_lk23_spoofed_mdns_identity_mismatch_is_rate_limited(pattern, lat, addr_flip):
    sim = Sim(tracker(PEER), lambda dev, aid: ConnectResult.IDENTITY_MISMATCH, latency=lambda aid: lat)
    when = 0.0
    for k, (gap, kind) in enumerate(itertools.cycle(pattern)):
        when += gap
        if when >= 300.0:
            break
        if kind == "seen":
            addrs = ADDR2 if addr_flip and k % 2 else ADDR
            sim.call("device_seen", PEER, addrs, PORT, at=when)
        else:
            sim.call("device_gone", PEER, at=when)
    sim.advance(300.0)
    times = [x for x in sim.connect_times() if x < 300.0]
    assert len(times) <= 16, times
    finished = [x for x in times if x + lat <= 300.0]
    expected = [LinkFailed(PEER, ConnectResult.IDENTITY_MISMATCH)] if finished else []
    assert sim.failed() == expected


@HYPO
@given(
    pattern=st.lists(
        st.tuples(st.sampled_from(GAPS), st.sampled_from(["seen", "gone"])), min_size=1, max_size=30
    ),
    lat=st.sampled_from([0.0, 0.05, 0.5, 1.5]),
)
def test_lk23_spoofed_mdns_unreachable_is_rate_limited(pattern, lat):
    sim = Sim(tracker(PEER), lambda dev, aid: UNREACHABLE, latency=lambda aid: lat)
    when = 0.0
    for gap, kind in itertools.cycle(pattern):
        when += gap
        if when >= 300.0:
            break
        if kind == "seen":
            sim.call("device_seen", PEER, ADDR, PORT, at=when)
        else:
            sim.call("device_gone", PEER, at=when)
    sim.advance(300.0)
    times = [x for x in sim.connect_times() if x < 300.0]
    assert len(times) <= 300 / links.EVENT_RETRY_GAP + 1, len(times)
    assert len(sim.failed()) <= 1


# =========================================================================================================
# P2-6a (spec-p2-6.md mục A3, plan-p2-6.md mục 2.1): `LinkSwitched`, `hint`, `hold` — L1–L4
# =========================================================================================================

ADDR3 = ("192.0.2.30",)


def _connected_out(t: LinkTracker, dev: str = PEER, cid: int = 1, now: float = 0.0, addrs=ADDR, port=PORT):
    """Thiết bị đã tin cậy: thấy qua mDNS ở `now`, thử, nối được (`cid`) ở `now + 0.1`."""
    c = start_attempt(t, dev, now, addrs, port)
    assert notifies(t.connect_finished(dev, c.attempt_id, OK, cid, now + 0.1)) == [LinkUp(dev, cid, True)]


# --- L1: LinkSwitched -------------------------------------------------------------------------------------


def test_l1_link_switched_is_frozen_dataclass_with_spec_fields():
    ev = LinkSwitched(device_id=PEER, old_conn_id=1, conn_id=2, outgoing=True)
    assert [f.name for f in dataclasses.fields(ev)] == ["device_id", "old_conn_id", "conn_id", "outgoing"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.conn_id = 3  # type: ignore[misc]
    assert Notify(ev).event is ev


@pytest.mark.parametrize("own", [LO, HI], ids=["own<peer", "own>peer"])
def test_l1_same_side_switch_emits_link_switched_after_close(own):
    """Cùng một bên mở hai kết nối (ở đây: máy kia mở cả hai) → giữ mới, ngay cả khi cũ còn trẻ."""
    t = LinkTracker(own)
    t.trusted_changed([PEER], 0.0)
    t.incoming_control(PEER, 1, 0.0)
    acts = t.incoming_control(PEER, 2, 0.01)
    assert acts == [CloseConnection(PEER, 1), Notify(LinkSwitched(PEER, 1, 2, False))]
    assert t.connected() == {PEER: 2}


@pytest.mark.parametrize("combo", ["in-out", "out-in"])
@pytest.mark.parametrize("own", [LO, HI], ids=["own<peer", "own>peer"])
def test_l1_old_connection_past_replace_after_emits_link_switched(own, combo):
    t = LinkTracker(own)
    t.trusted_changed([PEER], 0.0)
    if combo == "in-out":
        c = start_attempt(t, PEER, 0.0)
        t.incoming_control(PEER, 1, 0.1)
        acts = t.connect_finished(PEER, c.attempt_id, OK, 2, 0.1 + links.REPLACE_AFTER + 0.5)
        outgoing = True
    else:
        _connected_out(t, PEER, 1, 0.0)
        acts = t.incoming_control(PEER, 2, 0.1 + links.REPLACE_AFTER + 0.5)
        outgoing = False
    assert acts == [CloseConnection(PEER, 1), Notify(LinkSwitched(PEER, 1, 2, outgoing))]
    assert t.connected() == {PEER: 2}


@pytest.mark.parametrize("order", ["local-first", "remote-first"])
def test_l1_tie_break_keeping_old_connection_emits_no_link_switched(order):
    """Luật hòa giữ kết nối cũ → chỉ `CloseConnection(mới)`, không `Notify` nào."""
    # local-first: own < peer giữ kết nối ra (cũ). remote-first: own > peer giữ kết nối vào (cũ).
    own = LO if order == "local-first" else HI
    t = LinkTracker(own)
    t.trusted_changed([PEER], 0.0)
    c = start_attempt(t, PEER, 0.0)
    if order == "local-first":
        t.connect_finished(PEER, c.attempt_id, OK, 1, 0.1)
        acts = t.incoming_control(PEER, 2, 0.5)
    else:
        t.incoming_control(PEER, 1, 0.1)
        acts = t.connect_finished(PEER, c.attempt_id, OK, 2, 0.5)
    assert acts == [CloseConnection(PEER, 2)]
    assert notifies(acts) == []
    assert t.connected() == {PEER: 1}


@pytest.mark.parametrize("order", ["local-first", "remote-first"])
def test_l1_tie_break_keeping_new_connection_emits_link_switched(order):
    # local-first + own > peer: giữ kết nối vào (mới). remote-first + own < peer: giữ kết nối ra (mới).
    own = HI if order == "local-first" else LO
    t = LinkTracker(own)
    t.trusted_changed([PEER], 0.0)
    c = start_attempt(t, PEER, 0.0)
    if order == "local-first":
        t.connect_finished(PEER, c.attempt_id, OK, 1, 0.1)
        acts = t.incoming_control(PEER, 2, 0.5)
        outgoing = False
    else:
        t.incoming_control(PEER, 1, 0.1)
        acts = t.connect_finished(PEER, c.attempt_id, OK, 2, 0.5)
        outgoing = True
    assert acts == [CloseConnection(PEER, 1), Notify(LinkSwitched(PEER, 1, 2, outgoing))]
    assert t.connected() == {PEER: 2}


def test_l1_after_switch_old_connection_lost_is_silent_and_new_one_gives_link_down():
    t = tracker(PEER)
    t.incoming_control(PEER, 1, 0.0)
    t.incoming_control(PEER, 2, 0.1)
    assert t.connection_lost(PEER, 1, 0.2) == []
    assert t.connected() == {PEER: 2}
    assert t.connection_lost(PEER, 2, 0.3) == [Notify(LinkDown(PEER, 2))]
    assert t.connected() == {}


def test_l1_first_connection_and_untrusted_incoming_never_emit_link_switched():
    t = tracker(PEER)
    assert t.incoming_control(PEER, 1, 0.0) == [Notify(LinkUp(PEER, 1, False))]
    assert t.incoming_control(Q, 5, 0.1) == [CloseConnection(Q, 5)]


# --- L2: hint ---------------------------------------------------------------------------------------------


def test_l2_hint_for_trusted_device_connects_now_with_hint_address():
    t = tracker(PEER)
    assert t.hint(PEER, ADDR2, PORT2, 0.0) == [Connect(PEER, 1, ADDR2, PORT2)]
    assert t.attempting() == {PEER: 1}


def test_l2_hint_addresses_are_deduplicated_keeping_order():
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ["192.0.2.2", "192.0.2.1", "192.0.2.2"], PORT, 0.0))
    assert c.addresses == ("192.0.2.2", "192.0.2.1")


def test_l2_hint_for_untrusted_device_only_remembers():
    t = LinkTracker(OWN)
    assert t.hint(PEER, ADDR2, PORT2, 0.0) == []
    assert t.attempting() == {}
    assert t.tick(100.0) == [] and t.network_changed(100.0) == []
    # Được tin cậy sau đó: nối tới địa chỉ đã nhớ (plan 2.1: “chưa tin cậy thì chỉ nhớ”).
    assert t.trusted_changed([PEER], 101.0) == [Connect(PEER, 1, ADDR2, PORT2)]


@pytest.mark.parametrize("result", BLOCKING)
def test_l2_hint_unblocks_and_counts_as_user_action(result):
    t = tracker(PEER)
    c = start_attempt(t)
    assert notifies(t.connect_finished(PEER, c.attempt_id, result, None, 0.1)) == [LinkFailed(PEER, result)]
    assert t.next_wakeup() is None
    # Ngay sau đó (chưa đủ EVENT_RETRY_GAP): vẫn thử ngay vì là thao tác người dùng.
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.2))
    assert (c.addresses, c.port) == (ADDR2, PORT2)
    # Đã gỡ chặn và xóa danh sách đã báo: lỗi cùng loại được báo lại.
    assert notifies(t.connect_finished(PEER, c.attempt_id, result, None, 0.3)) == [LinkFailed(PEER, result)]


def test_l2_hint_clears_unreachable_counter():
    """Hint là thao tác người dùng: xóa bộ đếm (lịch tính lại từ delay(1)). spec-p2-7 E2: lần thử do hint
    gây ra mà UNREACHABLE thì báo ngay, kể cả khi mDNS tươi (trước E2: không báo)."""
    t = tracker(PEER)
    fail_n_unreachable(t, PEER, 2)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 1.5))
    assert notifies(t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 1.6)) == [
        LinkFailed(PEER, UNREACHABLE)
    ]
    assert t.next_wakeup() == pytest.approx(1.6 + delay(1))


def test_l2_hint_while_attempting_or_connected_starts_nothing():
    t = tracker(PEER)
    c = start_attempt(t)
    acts = t.hint(PEER, ADDR2, PORT2, 0.1)
    assert only(acts, Connect) == [] and only(acts, CancelConnect) == []
    assert t.attempting() == {PEER: c.attempt_id}
    t.connect_finished(PEER, c.attempt_id, OK, 4, 0.2)
    assert t.hint(PEER, ADDR3, PORT2, 0.3) == []
    assert t.connected() == {PEER: 4} and t.attempting() == {}


def test_l2_hint_survives_device_gone_but_without_schedule():
    """Spec C1: sau `device_gone` (không held) hint vẫn được nhớ, nhưng chỉ dùng cho lần thử do người
    dùng / sự kiện: không hẹn lịch §6.2 (lịch chỉ chạy khi mDNS tươi hoặc đang held)."""
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    t.connect_finished(PEER, c.attempt_id, OK, 1, 0.1)
    assert t.device_seen(PEER, ADDR, PORT, 0.2) == []
    assert t.device_gone(PEER, 0.3) == []
    assert t.connection_lost(PEER, 1, 1.0) == [Notify(LinkDown(PEER, 1))]
    assert t.next_wakeup() is None, "chỉ còn hint, không held: không hẹn lịch"
    assert t.tick(3600.0) == []
    # Sự kiện (mạng đổi): thử tới địa chỉ hint.
    c = the_connect(t.network_changed(3600.0))
    assert (c.addresses, c.port) == (ADDR2, PORT2)


def test_l2_hint_survives_device_gone_for_network_event_after_failure():
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0, ADDR, PORT)
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.2))
    t.device_gone(PEER, 0.3)
    acts = t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.4)
    # spec-p2-7 A1: lần thử do `hint()` gây ra thất bại lúc không có lịch (mDNS đã báo biến mất,
    # không held) → báo `LinkFailed(UNREACHABLE)` một lần (trước P2-7: không báo).
    assert notifies(acts) == [LinkFailed(PEER, UNREACHABLE)] and only(acts, Connect) == []
    assert t.next_wakeup() is None
    c = the_connect(t.network_changed(0.2 + links.EVENT_RETRY_GAP))
    assert (c.addresses, c.port) == (ADDR2, PORT2)


def test_l2_hint_survives_device_gone_for_user_retry():
    t = tracker(PEER)
    t.hint(PEER, ADDR2, PORT2, 0.0)
    t.connect_finished(PEER, 1, UNREACHABLE, None, 0.1)
    c = the_connect(t.device_seen(PEER, ADDR, PORT, 5.0))  # mDNS tươi, khác cổng: thử theo mDNS
    assert (c.addresses, c.port) == (ADDR, PORT)
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 5.1)
    t.device_gone(PEER, 5.2)
    c = the_connect(t.retry_now(PEER, 5.3))
    assert (c.addresses, c.port) == (ADDR2, PORT2)


def test_l2_new_hint_replaces_old_one():
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)
    c = the_connect(t.hint(PEER, ADDR3, PORT, 0.2))
    assert (c.addresses, c.port) == (ADDR3, PORT)
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.3)
    assert t.next_wakeup() is None  # C1: chỉ có hint → không lịch
    c = the_connect(t.retry_now(PEER, 0.4))
    assert (c.addresses, c.port) == (ADDR3, PORT), "lần thử sau phải dùng hint mới"


# --- C1: lịch §6.2 chỉ chạy khi mDNS tươi hoặc đang held --------------------------------------------------


def test_c1_hint_only_failure_has_no_schedule_and_reports_unreachable_once():
    """C1 + spec-p2-7 A1: chỉ có hint → không lịch; nhưng lần thử do `hint()` gây ra thất bại thì báo
    `LinkFailed(UNREACHABLE)` ngay (để `teleport send --address` tới cổng đóng dừng được), đúng một lần."""
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    acts = t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)
    assert acts == [Notify(LinkFailed(PEER, UNREACHABLE))]
    assert t.next_wakeup() is None
    assert t.tick(3600.0) == []


def test_c1_hint_only_connection_lost_without_hold_has_no_schedule():
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))  # chưa từng có mDNS
    t.connect_finished(PEER, c.attempt_id, OK, 1, 0.1)
    assert t.connection_lost(PEER, 1, 1.0) == [Notify(LinkDown(PEER, 1))]
    assert t.next_wakeup() is None
    assert t.tick(3600.0) == []
    c = the_connect(t.retry_now(PEER, 3600.0))  # thao tác người dùng vẫn nối được
    assert (c.addresses, c.port) == (ADDR2, PORT2)


def test_c1_hint_only_connection_lost_while_held_retries_after_1s():
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    t.connect_finished(PEER, c.attempt_id, OK, 1, 0.1)
    assert t.hold(PEER, True, 0.2) == []
    assert t.connection_lost(PEER, 1, 1.0) == [Notify(LinkDown(PEER, 1))]
    assert t.next_wakeup() == pytest.approx(1.0 + links.RETRY_BASE)
    c = the_connect(t.tick(1.0 + links.RETRY_BASE))
    assert (c.addresses, c.port) == (ADDR2, PORT2)
    # Held: lịch §6.2 tiếp tục sau thất bại.
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 2.1)
    assert t.next_wakeup() == pytest.approx(2.1 + delay(1))


def test_c1_hint_only_failures_while_held_follow_schedule_and_report_once():
    t = tracker(PEER)
    assert t.hold(PEER, True, 0.0) == []  # chưa có địa chỉ: không thử
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    now = 0.0
    reports = []
    for i in range(5):
        now += 0.1
        acts = notifies(t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, now))
        if i == 0:
            # spec-p2-7 E2: lần thử đầu do hint gây ra → báo ngay, dù đang held (có lịch).
            assert acts == [LinkFailed(PEER, UNREACHABLE)]
        reports += acts
        nw = t.next_wakeup()
        assert nw is not None, "đang held: phải có lịch"
        c = the_connect(t.tick(nw))
        now = nw
    # Đã báo rồi thì các lần thất bại theo lịch không báo lại (mỗi loại lỗi một lần).
    assert reports == [LinkFailed(PEER, UNREACHABLE)]


def test_c1_fresh_mdns_keeps_schedule_when_hint_also_present():
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0, ADDR, PORT)
    t.connect_finished(PEER, c.attempt_id, OK, 1, 0.1)
    assert t.hint(PEER, ADDR2, PORT2, 0.2) == []
    t.connection_lost(PEER, 1, 1.0)
    assert t.next_wakeup() == pytest.approx(1.0 + links.RETRY_BASE)


# --- C2: hint() khi đang thử ------------------------------------------------------------------------------


def _next_connect_after_failure(t: LinkTracker, aid: int, now: float) -> Connect:
    """Lần thử `aid` thất bại; trả `Connect` kế tiếp (ngay trong kết quả, hoặc ở `tick` theo lịch)."""
    found = only(t.connect_finished(PEER, aid, UNREACHABLE, None, now), Connect)
    if found:
        assert len(found) == 1
        return found[0]
    nw = t.next_wakeup()
    assert nw is not None, "mDNS tươi: phải có lần thử kế tiếp"
    return the_connect(t.tick(max(nw, now)))


def test_c2_hint_with_other_port_while_attempting_is_used_by_next_attempt():
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0, ADDR, PORT)
    acts = t.hint(PEER, ADDR2, PORT2, 0.05)
    assert only(acts, Connect) == [] and only(acts, CancelConnect) == []
    assert t.attempting() == {PEER: c.attempt_id}
    c = _next_connect_after_failure(t, c.attempt_id, 0.1)
    assert (c.addresses, c.port) == (ADDR2, PORT2), "lần thử kế tiếp sau hint phải dùng địa chỉ/cổng hint"
    # Cờ chỉ cho một lần `_start`: lần thử theo lịch sau đó dùng mDNS tươi (A3).
    c = _next_connect_after_failure(t, c.attempt_id, 5.0)
    assert (c.addresses, c.port) == (ADDR, PORT)


def test_c2_hint_with_same_port_while_attempting_is_merged_into_next_attempt():
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0, ("192.0.2.1",), PORT)
    t.hint(PEER, ("192.0.2.9",), PORT, 0.05)
    c = _next_connect_after_failure(t, c.attempt_id, 0.1)
    assert (c.addresses, c.port) == (("192.0.2.9", "192.0.2.1"), PORT)


def _hint_pending_after_failure(t: LinkTracker, *, gone_before_failure: bool) -> None:
    """mDNS (ADDR, PORT) đang thử từ 0.0; `hint` khác cổng lúc 0.5; lần thử thất bại lúc 1.0;
    mDNS báo thiết bị biến mất (trước hoặc sau lúc thất bại)."""
    c = start_attempt(t, PEER, 0.0, ADDR, PORT)
    assert only(t.hint(PEER, ADDR2, PORT2, 0.5), Connect) == []
    if gone_before_failure:
        assert t.device_gone(PEER, 0.8) == []
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 1.0)
    if not gone_before_failure:
        assert t.device_gone(PEER, 1.2) == []


@pytest.mark.parametrize("gone_before_failure", [False, True], ids=["gone-sau-that-bai", "gone-truoc"])
def test_c2_pending_hint_attempt_survives_device_gone(gone_before_failure):
    """Soát lần 2, ca A/A': lần thử do `hint()` gây ra (đã hẹn) không bị `device_gone` xóa mất."""
    t = tracker(PEER)
    _hint_pending_after_failure(t, gone_before_failure=gone_before_failure)
    nw = t.next_wakeup()
    assert nw is not None, "lần thử hint đã hẹn phải còn sau device_gone"
    assert nw <= 1.0 + links.EVENT_RETRY_GAP + 1e-9
    c = the_connect(t.tick(nw))
    assert (c.addresses, c.port) == (ADDR2, PORT2)


def test_c2_pending_hint_is_consumed_and_later_mdns_event_uses_mdns():
    """Soát lần 2, ca B: sau khi lần thử hint đã chạy, không còn cờ treo: mDNS thấy lại lâu sau → mDNS."""
    t = tracker(PEER)
    _hint_pending_after_failure(t, gone_before_failure=False)
    c = the_connect(t.tick(t.next_wakeup()))
    assert (c.addresses, c.port) == (ADDR2, PORT2)
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 5.0)
    assert t.next_wakeup() is None, "chỉ còn hint, không held: không lịch (C1)"
    c = the_connect(t.device_seen(PEER, ADDR, PORT, 3605.0))
    assert (c.addresses, c.port) == (ADDR, PORT)


def test_c2_pending_hint_runs_once_after_release_then_no_schedule():
    """Soát lần 2, ca C: chỉ có hint, held, có lịch; đang thử thì `hint()` rồi `hold(False)` → lần thử
    hint vẫn chạy đúng một lần, sau đó không còn lịch."""
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    assert t.hold(PEER, True, 0.1) == []
    assert only(t.hint(PEER, ADDR3, PORT2, 0.5), Connect) == []  # đang thử → cờ cho lần kế tiếp
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 1.0)
    assert t.next_wakeup() is not None  # held: có lịch
    assert t.hold(PEER, False, 1.1) == []
    nw = t.next_wakeup()
    assert nw is not None, "lần thử hint đã hẹn vẫn phải chạy sau hold(False)"
    c = the_connect(t.tick(nw))
    assert (c.addresses, c.port) == (ADDR3, PORT2)
    # Lần thử này do `hint()` thứ hai gây ra, thất bại lúc không còn lịch → spec-p2-7 A1: báo một lần.
    acts = t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, nw + 0.1)
    assert acts == [Notify(LinkFailed(PEER, UNREACHABLE))]
    assert t.next_wakeup() is None
    assert t.tick(nw + 3600.0) == []


def test_c2_hint_while_attempting_then_attempt_ok_keeps_connection():
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0, ADDR, PORT)
    t.hint(PEER, ADDR2, PORT2, 0.05)
    assert notifies(t.connect_finished(PEER, c.attempt_id, OK, 1, 0.1)) == [LinkUp(PEER, 1, True)]
    assert t.connected() == {PEER: 1} and t.attempting() == {}


def test_l2_hint_is_forgotten_when_device_is_untrusted():
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    assert t.trusted_changed([], 0.5) == [CancelConnect(PEER, c.attempt_id)]
    assert t.trusted_changed([PEER], 1.0) == []
    assert t.attempting() == {}
    assert t.retry_now(PEER, 1.1) == []
    assert t.network_changed(10.0) == [] and t.tick(100.0) == []


def test_l2_fresh_mdns_beats_hint_for_scheduled_attempts_when_ports_differ():
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0, ADDR, PORT)
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)
    # Lần thử do chính hint gây ra: địa chỉ/cổng hint.
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.2))
    assert (c.addresses, c.port) == (ADDR2, PORT2)
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.3)
    # Lần thử theo lịch: mDNS tươi trước.
    c = the_connect(t.tick(t.next_wakeup()))
    assert (c.addresses, c.port) == (ADDR, PORT)


def test_l2_fresh_mdns_beats_hint_for_event_attempts_when_ports_differ():
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)
    c = the_connect(t.device_seen(PEER, ADDR, PORT, links.EVENT_RETRY_GAP))
    assert (c.addresses, c.port) == (ADDR, PORT)


def test_l2_same_port_merges_hint_then_mdns_addresses():
    t = tracker(PEER)
    seen = ("192.0.2.1", "192.0.2.2")
    hint = ("192.0.2.3", "192.0.2.1")
    c = start_attempt(t, PEER, 0.0, seen, PORT)
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)
    c = the_connect(t.hint(PEER, hint, PORT, 0.2))
    assert c.addresses == ("192.0.2.3", "192.0.2.1", "192.0.2.2")
    assert c.port == PORT


def test_l2_same_port_merge_also_applies_to_scheduled_attempts():
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0, ("192.0.2.1",), PORT)
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)
    c = the_connect(t.hint(PEER, ("192.0.2.9",), PORT, 0.2))
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.3)
    c = the_connect(t.tick(t.next_wakeup()))
    assert (c.addresses, c.port) == (("192.0.2.9", "192.0.2.1"), PORT)


def test_l2_merged_addresses_are_capped_like_device_seen():
    t = tracker(PEER)
    seen = tuple(f"192.0.2.{i}" for i in range(1, 11))
    hint = tuple(f"198.51.100.{i}" for i in range(1, 11))
    c = start_attempt(t, PEER, 0.0, seen, PORT)
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)
    c = the_connect(t.hint(PEER, hint, PORT, 0.2))
    assert len(c.addresses) == 16, "cùng trần 16 địa chỉ như device_seen"
    assert c.addresses == hint + seen[:6]


def test_l2_hint_beats_stale_mdns_address_of_held_device():
    t = tracker(PEER)
    _connected_out(t, PEER, 1, 0.0, ADDR, PORT)
    assert t.hint(PEER, ADDR2, PORT2, 0.2) == []
    assert t.hold(PEER, True, 0.3) == []
    assert t.device_gone(PEER, 0.4) == []  # đang giữ: địa chỉ mDNS thành cũ, không bị xóa
    t.connection_lost(PEER, 1, 1.0)
    c = the_connect(t.tick(t.next_wakeup()))
    assert (c.addresses, c.port) == (ADDR2, PORT2), "_target: mDNS tươi, rồi hint, rồi mDNS cũ"


def _l2_bad_hint_calls() -> list[tuple[str, Callable[[LinkTracker], object], type | tuple]]:
    big = 1000.0
    return [
        ("id_sai", lambda t: t.hint("abc", ADDR, PORT, big), MessageError),
        ("id_hoa", lambda t: t.hint("A" * 32, ADDR, PORT, big), MessageError),
        ("dia_chi_rong", lambda t: t.hint(Q, [], PORT, big), ValueError),
        ("17_dia_chi", lambda t: t.hint(Q, [f"192.0.2.{i}" for i in range(1, 18)], PORT, big), ValueError),
        ("chuoi_don", lambda t: t.hint(Q, "192.0.2.10", PORT, big), ValueError),
        ("ipv6", lambda t: t.hint(Q, ["::1"], PORT, big), ValueError),
        ("ten_mien", lambda t: t.hint(Q, ["localhost"], PORT, big), ValueError),
        ("0.0.0.0", lambda t: t.hint(Q, ["0.0.0.0"], PORT, big), ValueError),
        ("multicast", lambda t: t.hint(Q, ["224.0.0.251"], PORT, big), ValueError),
        ("broadcast", lambda t: t.hint(Q, ["255.255.255.255"], PORT, big), ValueError),
        ("cong_0", lambda t: t.hint(Q, ADDR, 0, big), ValueError),
        ("cong_65536", lambda t: t.hint(Q, ADDR, 65536, big), ValueError),
        ("cong_true", lambda t: t.hint(Q, ADDR, True, big), ValueError),
        ("cong_chuoi", lambda t: t.hint(Q, ADDR, "47474", big), ValueError),
        ("now_giam", lambda t: t.hint(Q, ADDR, PORT, 0.5), ValueError),
        ("now_nan", lambda t: t.hint(Q, ADDR, PORT, math.nan), ValueError),
    ]


@pytest.mark.parametrize("case", [name for name, _, _ in _l2_bad_hint_calls()])
def test_l2_bad_hint_arguments_raise_and_leave_state_unchanged(case):
    t, _ar = _lk1_state()  # Q đang chờ lịch (2.0), R đang thử, PEER đang kết nối; `now` cuối 1.0
    call, exc = next((fn, e) for name, fn, e in _l2_bad_hint_calls() if name == case)
    before = (t.connected(), t.attempting(), t.next_wakeup())
    with pytest.raises(exc):
        call(t)
    assert (t.connected(), t.attempting(), t.next_wakeup()) == before
    # Lần thử kế của Q vẫn dùng địa chỉ mDNS cũ (hint sai không được nhớ), `now` không nhích lên.
    assert t.tick(1.5) == []
    c = the_connect(t.tick(2.0))
    assert (c.device_id, c.addresses, c.port) == (Q, ADDR, PORT)


def test_l2_hint_for_own_device_is_ignored():
    t = tracker(PEER)
    # Chấp nhận cả ValueError: node.connect_hint báo ValueError cho chính máy này (plan 2.6).
    with contextlib.suppress(ValueError):
        assert t.hint(OWN, ADDR, PORT, 0.0) == []
    assert t.attempting() == {} and t.connected() == {}


# --- L3: hold ---------------------------------------------------------------------------------------------


def test_l3_held_device_gone_then_lost_still_retries_last_address_after_1s():
    t = tracker(PEER)
    _connected_out(t, PEER, 1, 0.0, ADDR2, PORT2)
    assert t.hold(PEER, True, 0.2) == []
    assert t.device_gone(PEER, 0.3) == []
    assert t.connection_lost(PEER, 1, 1.0) == [Notify(LinkDown(PEER, 1))]
    assert t.next_wakeup() == pytest.approx(1.0 + links.RETRY_BASE)
    c = the_connect(t.tick(1.0 + links.RETRY_BASE))
    assert (c.addresses, c.port) == (ADDR2, PORT2)
    # Vẫn theo lịch §6.2 sau khi thất bại.
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 2.1)
    assert t.next_wakeup() == pytest.approx(2.1 + delay(1))


def test_l3_held_device_gone_keeps_pending_schedule():
    t = tracker(PEER)
    c = start_attempt(t)
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)
    assert t.next_wakeup() == pytest.approx(1.1)
    assert t.hold(PEER, True, 0.2) == []
    assert t.device_gone(PEER, 0.3) == []
    assert t.next_wakeup() == pytest.approx(1.1)
    c = the_connect(t.tick(1.1))
    assert (c.addresses, c.port) == (ADDR, PORT)


def test_l3_release_after_gone_drops_schedule_and_forgets_address():
    t = tracker(PEER)
    _connected_out(t)
    t.hold(PEER, True, 0.2)
    t.device_gone(PEER, 0.3)
    t.connection_lost(PEER, 1, 1.0)
    assert t.next_wakeup() is not None
    acts = t.hold(PEER, False, 1.5)
    assert only(acts, CloseConnection) == [] and notifies(acts) == []
    assert t.next_wakeup() is None
    assert t.retry_now(PEER, 1.6) == [], "đã quên địa chỉ mDNS cũ"
    assert t.network_changed(10.0) == [] and t.tick(100.0) == []


def test_l3_release_while_mdns_is_fresh_keeps_address():
    t = tracker(PEER)
    _connected_out(t)
    t.hold(PEER, True, 0.2)
    assert t.hold(PEER, False, 0.3) == []
    t.connection_lost(PEER, 1, 1.0)
    assert t.next_wakeup() == pytest.approx(1.0 + links.RETRY_BASE)


def test_l3_device_seen_clears_stale_mark():
    t = tracker(PEER)
    _connected_out(t)
    t.hold(PEER, True, 0.2)
    t.device_gone(PEER, 0.3)
    assert t.device_seen(PEER, ADDR2, PORT2, 0.4) == []  # đang kết nối: chỉ nhớ địa chỉ mới
    t.hold(PEER, False, 0.5)  # địa chỉ đã tươi lại: thả ra không xóa gì
    t.connection_lost(PEER, 1, 1.0)
    c = the_connect(t.tick(t.next_wakeup()))
    assert (c.addresses, c.port) == (ADDR2, PORT2)


def test_l3_unheld_device_gone_still_stops_retries():
    """Hành vi cũ (LK6) giữ nguyên khi không `hold`."""
    t = tracker(PEER)
    _connected_out(t)
    t.device_gone(PEER, 0.3)
    t.connection_lost(PEER, 1, 1.0)
    assert t.next_wakeup() is None


def _hint_only_failed(t: LinkTracker) -> None:
    """Thiết bị chỉ có hint, lần thử ở 0.0 thất bại ở 0.1: đủ điều kiện, không có lịch (C1)."""
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)
    assert t.next_wakeup() is None


def test_l3_hold_eligible_device_without_schedule_retries_after_event_gap():
    t = tracker(PEER)
    _hint_only_failed(t)
    assert t.hold(PEER, True, 0.5) == []  # chưa đủ EVENT_RETRY_GAP kể từ lần thử trước
    nw = t.next_wakeup()
    assert nw is not None and nw >= links.EVENT_RETRY_GAP - 1e-9
    assert t.tick(links.EVENT_RETRY_GAP - 0.01) == []
    c = the_connect(t.tick(max(nw, links.EVENT_RETRY_GAP)))
    assert (c.addresses, c.port) == (ADDR2, PORT2)


def test_l3_hold_eligible_device_long_after_last_attempt_retries_now():
    t = tracker(PEER)
    _hint_only_failed(t)
    c = the_connect(t.hold(PEER, True, 50.0))
    assert (c.addresses, c.port) == (ADDR2, PORT2)


# --- C4: hold(True) chỉ thử khi held đổi False→True và thiết bị không bị chặn ----------------------------


@pytest.mark.parametrize("result", BLOCKING)
def test_c4_hold_on_blocked_device_does_not_connect(result):
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0)
    t.connect_finished(PEER, c.attempt_id, result, None, 0.1)
    assert t.hold(PEER, True, 0.5) == []
    assert t.next_wakeup() is None
    assert t.hold(PEER, True, 50.0) == []
    assert t.tick(3600.0) == []
    assert t.attempting() == {}
    # Sự kiện mDNS vẫn theo luật chặn cũ (LK8): đủ khoảng thì được thử.
    the_connect(t.device_seen(PEER, ADDR, PORT, 3600.0))


@pytest.mark.parametrize("result", BLOCKING)
def test_c4_hold_on_blocked_hint_only_device_does_not_connect(result):
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    t.connect_finished(PEER, c.attempt_id, result, None, 0.1)
    assert t.hold(PEER, True, 50.0) == []
    assert t.next_wakeup() is None and t.attempting() == {}


def test_c4_repeated_hold_true_is_noop():
    t = tracker(PEER)
    _hint_only_failed(t)
    assert t.hold(PEER, True, 0.5) == []
    nw = t.next_wakeup()
    assert nw is not None
    assert t.hold(PEER, True, 0.6) == []
    assert t.next_wakeup() == nw
    c = the_connect(t.tick(nw))
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, nw + 0.1)
    scheduled = t.next_wakeup()  # held: lịch §6.2
    assert scheduled is not None
    # Gọi lại hold(True) khi đã đủ EVENT_RETRY_GAP: không thử ngay, không đổi lịch.
    assert t.hold(PEER, True, nw + 0.1 + links.EVENT_RETRY_GAP) == []
    assert t.next_wakeup() == scheduled


def test_c4_repeated_hold_true_while_eligible_does_not_retry_again():
    t = tracker(PEER)
    _hint_only_failed(t)
    c = the_connect(t.hold(PEER, True, 50.0))
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 50.1)
    t.hold(PEER, False, 50.2)
    assert only(t.hold(PEER, True, 100.0), Connect) != [], "False → True: thử lại theo sự kiện"
    aid = t.attempting()[PEER]
    t.connect_finished(PEER, aid, UNREACHABLE, None, 100.1)
    scheduled = t.next_wakeup()
    assert t.hold(PEER, True, 200.0) == [], "held vẫn True: gọi lặp là no-op"
    assert t.next_wakeup() == scheduled


def test_l3_hold_while_connected_or_attempting_does_nothing():
    t = tracker(PEER, Q)
    _connected_out(t, PEER, 1)
    start_attempt(t, Q, 0.2)
    assert t.hold(PEER, True, 0.5) == []
    assert t.hold(Q, True, 0.5) == []
    assert t.connected() == {PEER: 1} and set(t.attempting()) == {Q}


def test_l3_hold_never_returns_close_or_notify():
    t = tracker(PEER, Q, R)
    _connected_out(t, PEER, 1)
    start_attempt(t, Q, 0.2)
    c = start_attempt(t, R, 0.2)
    t.connect_finished(R, c.attempt_id, ConnectResult.REJECTED, None, 0.3)
    now = 0.4
    for held in (True, False, True, True, False, False):
        for dev in (PEER, Q, R, HI, OWN):
            now += 0.7
            acts = t.hold(dev, held, now)
            assert all(isinstance(a, Connect) for a in acts), acts
            for a in acts:
                t.connect_finished(a.device_id, a.attempt_id, UNREACHABLE, None, now)


def test_l3_hold_for_unknown_untrusted_or_own_device_is_noop():
    t = LinkTracker(OWN)
    assert t.hold(PEER, True, 0.0) == []
    assert t.hold(OWN, True, 0.0) == []
    t.device_seen(Q, ADDR, PORT, 0.1)
    assert t.hold(Q, True, 0.2) == []
    # Không tin cậy → không “giữ”: mDNS báo đi thì quên như cũ (LK6), tin cậy sau đó không nối.
    t.device_gone(Q, 0.3)
    assert t.trusted_changed([Q], 0.4) == []
    assert t.attempting() == {}


def test_l3_held_state_does_not_survive_untrust():
    t = tracker(PEER)
    _connected_out(t, PEER, 1)
    t.hold(PEER, True, 0.2)
    t.trusted_changed([], 0.3)
    c = the_connect(t.trusted_changed([PEER], 0.4))  # địa chỉ mDNS vẫn còn (LK16)
    t.connect_finished(PEER, c.attempt_id, OK, 2, 0.5)
    t.device_gone(PEER, 0.6)
    t.connection_lost(PEER, 2, 1.0)
    assert t.next_wakeup() is None, "hold cũ không được sống qua thôi tin cậy"


def _l3_bad_hold_calls() -> list[tuple[str, Callable[[LinkTracker], object], type | tuple]]:
    big = 1000.0
    return [
        ("id_sai", lambda t: t.hold("abc", True, big), MessageError),
        ("id_none", lambda t: t.hold(None, True, big), MessageError),
        ("held_int", lambda t: t.hold(Q, 1, big), (ValueError, TypeError)),
        ("held_chuoi", lambda t: t.hold(Q, "yes", big), (ValueError, TypeError)),
        ("held_none", lambda t: t.hold(Q, None, big), (ValueError, TypeError)),
        ("now_giam", lambda t: t.hold(Q, True, 0.5), ValueError),
        ("now_inf", lambda t: t.hold(Q, True, math.inf), ValueError),
        ("now_chuoi", lambda t: t.hold(Q, True, "5"), (ValueError, TypeError)),
    ]


@pytest.mark.parametrize("case", [name for name, _, _ in _l3_bad_hold_calls()])
def test_l3_bad_hold_arguments_raise_and_leave_state_unchanged(case):
    t, _ar = _lk1_state()
    call, exc = next((fn, e) for name, fn, e in _l3_bad_hold_calls() if name == case)
    before = (t.connected(), t.attempting(), t.next_wakeup())
    with pytest.raises(exc):
        call(t)
    assert (t.connected(), t.attempting(), t.next_wakeup()) == before
    # Q không bị “giữ”: mDNS báo đi thì lịch bị xóa như cũ.
    assert t.device_gone(Q, 1.5) == []
    assert t.next_wakeup() is None


# --- L4: hypothesis, một tracker với hint/hold ------------------------------------------------------------

L4_OPS = [
    "seen",
    "gone",
    "trust",
    "network",
    "retry",
    "tick",
    "wake",
    "finish",
    "stale",
    "incoming",
    "lost",
    "hint",
    "hold",
    "release",
    "bad",
]
L4_ADDRS = [
    ("192.0.2.1",),
    ("192.0.2.2", "192.0.2.3", "192.0.2.2"),
    ("127.0.0.1",),
    ("192.0.2.1", "10.0.0.9"),
]
L4_PORTS = [47474, 50000]
# `back`: một `now` chắc chắn không hợp lệ (nhỏ hơn `now` của lời gọi hợp lệ trước, hoặc vô hạn).
L4_BAD: list[Callable[[LinkTracker, str, float, float], object]] = [
    lambda t, d, now, back: t.hint(d, [], 47474, now),
    lambda t, d, now, back: t.hint(d, ["::1"], 47474, now),
    lambda t, d, now, back: t.hint(d, "192.0.2.1", 47474, now),
    lambda t, d, now, back: t.hint(d, ["192.0.2.1"], 0, now),
    lambda t, d, now, back: t.hint(d, ["192.0.2.1"], True, now),
    lambda t, d, now, back: t.hint("x" * 32, ["192.0.2.1"], 47474, now),
    lambda t, d, now, back: t.hint(d, ["192.0.2.1"], 47474, back),
    lambda t, d, now, back: t.hold(d, 1, now),
    lambda t, d, now, back: t.hold(d, None, now),
    lambda t, d, now, back: t.hold("A" * 32, True, now),
    lambda t, d, now, back: t.hold(d, True, back),
    lambda t, d, now, back: t.hold(d, True, math.nan),
]

_Target = tuple[tuple[str, ...], int]


def _merge(hint: _Target, seen: _Target) -> _Target:
    """Spec A3: cùng cổng → hint trước rồi mDNS, bỏ trùng, cắt theo trần 16 địa chỉ."""
    return tuple(dict.fromkeys(hint[0] + seen[0]))[:16], hint[1]


@HYPO
@given(
    st.lists(
        st.tuples(st.sampled_from(L4_OPS), st.integers(0, 4), st.integers(0, 15), st.sampled_from(DTS)),
        max_size=80,
    )
)
def test_l4_tracker_with_hint_and_hold_keeps_invariants(ops):
    """L4: chỉ `Connect` thiết bị tin cậy có địa chỉ (đúng địa chỉ theo luật `_target` / gộp hint);
    không bao giờ có 2 kết nối hiện tại; `LinkSwitched` khi và chỉ khi kết nối hiện tại đổi giữa hai
    kết nối; tham số sai → trạng thái giữ nguyên.

    Mô hình địa chỉ: `seen` (mDNS tươi), `stale` (mDNS cũ của thiết bị đang giữ), `hints`, và `maybe`
    (địa chỉ mDNS cũ còn sót qua thôi tin cậy, spec không chốt là tươi hay cũ: được phép, không bắt buộc).
    """
    t = LinkTracker(OWN)
    now = 0.0
    trusted: set[str] = set()
    seen: dict[str, _Target] = {}
    stale: dict[str, _Target] = {}
    hints: dict[str, _Target] = {}
    maybe: dict[str, set[_Target]] = {}
    held: set[str] = set()
    pending_hint: set[str] = set()  # C2: hint() trong lúc đang thử → lần `_start` kế tiếp dùng hint
    outstanding: dict[int, str] = {}
    finished_aids: dict[int, str] = {}
    given_conns: dict[str, set[int]] = {}
    conn_dev: dict[int, str] = {}
    conn_local: dict[int, bool] = {}
    closed: set[int] = set()
    lost: set[int] = set()
    last_connect: dict[str, float] = {}
    last_now: float | None = None  # `now` của lời gọi hợp lệ gần nhất
    conn_ids = itertools.count(1)

    def give(dev: str, cid: int, local: bool) -> None:
        given_conns.setdefault(dev, set()).add(cid)
        conn_dev[cid] = dev
        conn_local[cid] = local

    def expected_targets(d: str, via_hint: bool) -> set[_Target]:
        h, s, old = hints.get(d), seen.get(d), stale.get(d)
        exp: set[_Target] = set()
        if via_hint and h is not None:
            exp.add(_merge(h, s) if s is not None and s[1] == h[1] else h)
        elif s is not None and h is not None and s[1] == h[1]:
            exp.add(_merge(h, s))
        elif s is not None:
            exp.add(s)
        elif h is not None:
            exp.add(h)
        elif old is not None:
            exp.add(old)
        for m in maybe.get(d, set()):
            exp.add(m)
            if h is not None and h[1] == m[1]:
                exp.add(_merge(h, m))
        return exp

    for op, i, j, dt in ops:
        now += dt
        dev = DEVS[i]
        before_attempting = t.attempting()
        before_connected = t.connected()
        bypass: set[str] = set()
        hinted: str | None = None
        finishing: tuple[str, int] | None = None

        if op == "seen":
            addrs, port = L4_ADDRS[j % 4], L4_PORTS[j % 2]
            acts = t.device_seen(dev, addrs, port, now)
            if dev != OWN:
                seen[dev] = (_dedup(addrs), port)
                stale.pop(dev, None)
                maybe.pop(dev, None)
        elif op == "gone":
            acts = t.device_gone(dev, now)
            if dev != OWN:
                if dev in trusted and dev in held:
                    if dev in seen:
                        stale[dev] = seen.pop(dev)
                else:
                    seen.pop(dev, None)
                    stale.pop(dev, None)
                    maybe.pop(dev, None)
        elif op == "trust":
            ids = {DEVS[k] for k in range(4) if j >> k & 1}
            newly, removed = ids - trusted, trusted - ids
            for d in removed:
                hints.pop(d, None)
                held.discard(d)
                pending_hint.discard(d)
                if d in stale:
                    maybe.setdefault(d, set()).add(stale.pop(d))
            bypass = newly
            trusted = ids
            acts = t.trusted_changed(sorted(ids | ({OWN} if i == 4 else set())), now)
        elif op == "network":
            acts = t.network_changed(now)
        elif op == "retry":
            acts = t.retry_now(dev, now)
            bypass = {dev}
        elif op in ("tick", "wake"):
            if op == "wake" and t.next_wakeup() is not None:
                now = max(now, t.next_wakeup())
            acts = t.tick(now)
        elif op == "finish":
            if not outstanding:
                continue
            aid = sorted(outstanding)[j % len(outstanding)]
            dev = outstanding.pop(aid)
            r = RESULTS[i]
            cid = next(conn_ids) if r == OK else None
            if cid is not None:
                give(dev, cid, True)
            acts = t.connect_finished(dev, aid, r, cid, now)
            finished_aids[aid] = dev
            finishing = (dev, aid)
        elif op == "stale":
            if finished_aids and j % 2 == 0:
                aid = sorted(finished_aids)[j % len(finished_aids)]
                sdev = finished_aids[aid]
            else:
                aid, sdev = 100_000 + j, DEVS[i % 4]
            if aid in outstanding:
                continue
            r = RESULTS[i]
            cid = next(conn_ids) if r == OK else None
            if cid is not None:
                give(sdev, cid, True)
            acts = t.connect_finished(sdev, aid, r, cid, now)
            assert acts == ([CloseConnection(sdev, cid)] if r == OK else []), acts
            dev = sdev
        elif op == "incoming":
            cid = next(conn_ids)
            acts = t.incoming_control(dev, cid, now)
            if dev != OWN:
                give(dev, cid, False)
        elif op == "lost":
            live = [c for c in sorted(conn_dev) if c not in lost]
            if not live:
                continue
            cid = live[j % len(live)]
            dev = conn_dev[cid]
            lost.add(cid)
            acts = t.connection_lost(dev, cid, now)
        elif op == "hint":
            if dev == OWN or dev not in trusted:
                continue
            addrs, port = L4_ADDRS[j % 4], L4_PORTS[(j >> 2) % 2]
            acts = t.hint(dev, addrs, port, now)
            hints[dev] = (_dedup(addrs), port)
            bypass = {dev}
            hinted = dev
            if dev in before_attempting:
                pending_hint.add(dev)
            assert all(isinstance(a, Connect) for a in acts), acts
        elif op in ("hold", "release"):
            acts = t.hold(dev, op == "hold", now)
            assert all(isinstance(a, Connect) for a in acts), ("hold chỉ được trả Connect", acts)
            if dev != OWN and dev in trusted:
                if op == "hold":
                    held.add(dev)
                else:
                    held.discard(dev)
                    stale.pop(dev, None)
        else:  # bad
            snap = (t.connected(), t.attempting(), t.next_wakeup())
            with pytest.raises((ValueError, TypeError)):
                back = last_now - 1.0 if last_now is not None else math.inf
                L4_BAD[j % len(L4_BAD)](t, dev if dev != OWN else DEVS[0], now, back)
            assert (t.connected(), t.attempting(), t.next_wakeup()) == snap
            continue
        last_now = now

        # --- Kiểm từng hành động -----------------------------------------------------------------------
        switched: dict[str, list[LinkSwitched]] = {}
        ups: dict[str, list[LinkUp]] = {}
        downs: dict[str, list[LinkDown]] = {}
        for a in acts:
            if isinstance(a, Connect):
                d = a.device_id
                assert d in trusted, f"Connect tới thiết bị chưa tin cậy {d[:4]}"
                exp = expected_targets(d, via_hint=d == hinted)
                if d in pending_hint:  # C2 (không chốt cờ còn sống bao lâu: chấp nhận cả hai)
                    exp |= expected_targets(d, via_hint=True)
                    pending_hint.discard(d)
                assert exp, f"Connect tới thiết bị không có địa chỉ {d[:4]}"
                assert (a.addresses, a.port) in exp, ((a.addresses, a.port), exp)
                assert d not in before_attempting or finishing == (d, before_attempting[d]), "hai lần thử"
                assert d not in t.connected(), "thử khi đang có kết nối"
                assert t.attempting().get(d) == a.attempt_id
                if d not in bypass and d in last_connect:
                    assert now - last_connect[d] >= links.RETRY_BASE - 1e-9, "thử lại quá sớm"
                last_connect[d] = now
                outstanding[a.attempt_id] = d
            elif isinstance(a, CancelConnect):
                assert outstanding.get(a.attempt_id) == a.device_id
                del outstanding[a.attempt_id]
                finished_aids[a.attempt_id] = a.device_id
            elif isinstance(a, CloseConnection):
                assert conn_dev.get(a.conn_id) == a.device_id, "đóng một kết nối lạ"
                assert a.conn_id not in closed, "đóng một kết nối hai lần"
                closed.add(a.conn_id)
            else:
                assert isinstance(a, Notify)
                ev = a.event
                if isinstance(ev, LinkSwitched):
                    switched.setdefault(ev.device_id, []).append(ev)
                elif isinstance(ev, LinkUp):
                    ups.setdefault(ev.device_id, []).append(ev)
                elif isinstance(ev, LinkDown):
                    downs.setdefault(ev.device_id, []).append(ev)
                else:
                    assert isinstance(ev, LinkFailed)

        # --- Kiểm trạng thái sau lời gọi -----------------------------------------------------------------
        after = t.connected()
        assert set(after) <= trusted
        for d in set(before_connected) | set(after) | set(switched) | set(ups) | set(downs):
            old, new = before_connected.get(d), after.get(d)
            if old is not None and new is not None and old != new:
                assert switched.get(d) == [LinkSwitched(d, old, new, conn_local[new])], (d[:4], acts)
            else:
                assert d not in switched, ("LinkSwitched khi kết nối hiện tại không đổi", acts)
            assert (d in ups) == (old is None and new is not None), ("LinkUp", acts)
            assert (d in downs) == (old is not None and new is None), ("LinkDown", acts)
        for d, cids in given_conns.items():
            live = cids - closed - lost
            cur = after.get(d)
            assert live == ({cur} if cur is not None else set()), (
                f"thiết bị {d[:4]}: kết nối còn mở {sorted(live)}, kết nối hiện tại {cur}"
            )
        assert t.attempting() == {d: a for a, d in outstanding.items()}


# =========================================================================================================
# P2-7 (spec-p2-7.md mục E2, thay A1): lần thử do `hint()` gây ra mà UNREACHABLE → **luôn**
# `LinkFailed(UNREACHABLE)` một lần cho mỗi lần gọi `hint`, có lịch §6.2 (mDNS tươi, held) hay không.
# REJECTED / IDENTITY / VERSION giữ như cũ. Lịch §6.2 và bộ đếm `UNREACHABLE_REPORT_AFTER` vẫn chạy như
# cũ khi có lịch (xem thêm `test_l2_hint_clears_unreachable_counter`,
# `test_c1_hint_only_failures_while_held_follow_schedule_and_report_once`).
# =========================================================================================================


def test_a1_each_hint_call_reports_unreachable_again():
    t = tracker(PEER)
    for k in range(3):
        start = 10.0 * k
        c = the_connect(t.hint(PEER, ADDR2, PORT2, start))
        acts = t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, start + 0.1)
        assert notifies(acts) == [LinkFailed(PEER, UNREACHABLE)], f"lần gọi hint thứ {k + 1}"
        assert only(acts, Connect) == [] and t.next_wakeup() is None


def test_a1_event_attempt_after_hint_failure_is_not_reported_again():
    """Chỉ lần thử do chính `hint()` gây ra được báo; lần thử do sự kiện mạng sau đó (vẫn tới địa chỉ
    hint, vẫn không lịch) thất bại thì không báo thêm."""
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    assert notifies(t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)) == [
        LinkFailed(PEER, UNREACHABLE)
    ]
    now = 0.0
    for _ in range(3):
        now += links.EVENT_RETRY_GAP + 1.0
        c = the_connect(t.network_changed(now))
        assert (c.addresses, c.port) == (ADDR2, PORT2)
        assert t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, now + 0.1) == []
        assert t.next_wakeup() is None


@pytest.mark.parametrize("result", BLOCKING)
def test_a1_blocking_result_of_hint_attempt_is_reported_as_itself_only(result):
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    acts = t.connect_finished(PEER, c.attempt_id, result, None, 0.1)
    assert notifies(acts) == [LinkFailed(PEER, result)], "không kèm thêm LinkFailed(UNREACHABLE)"
    assert t.next_wakeup() is None


def test_a1_hint_attempt_that_connects_reports_nothing():
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    assert notifies(t.connect_finished(PEER, c.attempt_id, OK, 1, 0.1)) == [LinkUp(PEER, 1, True)]
    # Mất kết nối, không held, chỉ có hint: không lịch, không báo (C1); hint lần nữa mà thất bại → báo.
    assert t.connection_lost(PEER, 1, 1.0) == [Notify(LinkDown(PEER, 1))]
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 2.0))
    assert notifies(t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 2.1)) == [
        LinkFailed(PEER, UNREACHABLE)
    ]


def test_a1_hint_attempt_fails_after_device_gone_is_reported():
    """mDNS tươi lúc gọi hint (cùng cổng: gộp địa chỉ), rồi mDNS báo biến mất trước khi lần thử xong:
    lúc thất bại thiết bị không còn lịch → báo một lần."""
    t = tracker(PEER)
    assert only(t.device_seen(PEER, ADDR, PORT, 0.0), Connect) != []
    t.connect_finished(PEER, 1, UNREACHABLE, None, 0.1)
    c = the_connect(t.hint(PEER, ADDR2, PORT, 0.2))
    assert t.device_gone(PEER, 0.3) == []
    acts = t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.4)
    assert notifies(acts) == [LinkFailed(PEER, UNREACHABLE)]
    assert t.next_wakeup() is None


def test_a1_two_hint_calls_during_one_attempt_report_for_each_attempt_they_caused():
    """Hint thứ hai gọi lúc lần thử của hint thứ nhất đang chạy → lần thử kế tiếp dùng hint mới (C2).
    Mỗi lần thử do một lần gọi hint gây ra mà thất bại không lịch → mỗi lần một báo."""
    t = tracker(PEER)
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.0))
    assert only(t.hint(PEER, ADDR3, PORT2, 0.05), Connect) == []
    acts = t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)
    reports = notifies(acts)
    found = only(acts, Connect)
    if not found:
        nw = t.next_wakeup()
        assert nw is not None, "lần thử do hint thứ hai gây ra phải còn được hẹn"
        found = only(t.tick(nw), Connect)
    [c2] = found
    assert (c2.addresses, c2.port) == (ADDR3, PORT2)
    reports += notifies(t.connect_finished(PEER, c2.attempt_id, UNREACHABLE, None, 5.0))
    assert reports == [LinkFailed(PEER, UNREACHABLE)] * 2
    assert t.next_wakeup() is None


@HYPO
@given(ops=st.lists(st.sampled_from(["hint", "event"]), min_size=1, max_size=12))
def test_a1_unreachable_reports_equal_failed_hint_calls_without_schedule(ops):
    """Thiết bị chỉ có hint (không mDNS, không held): số `LinkFailed(UNREACHABLE)` đúng bằng số lần gọi
    `hint` có lần thử thất bại; lần thử do sự kiện mạng thất bại không bao giờ được báo."""
    t = tracker(PEER)
    now = 0.0
    hint_failures = 0
    reports: list = []
    for op in ops:
        now += links.EVENT_RETRY_GAP + 1.0
        acts = t.hint(PEER, ADDR2, PORT2, now) if op == "hint" else t.network_changed(now)
        found = only(acts, Connect)
        if op == "hint":
            assert len(found) == 1, "hint là thao tác người dùng: thử ngay"
        if not found:
            continue
        if op == "hint":
            hint_failures += 1
        after = t.connect_finished(PEER, found[0].attempt_id, UNREACHABLE, None, now + 0.1)
        reports += notifies(after)
        assert only(after, Connect) == [] and t.next_wakeup() is None
    assert reports == [LinkFailed(PEER, UNREACHABLE)] * hint_failures


def test_e2_seen_then_failed_then_hint_failed_reports_now_and_device_gone_does_not_hang():
    """Ca soat-code: `device_seen` → thất bại → `hint` → thất bại → phải có `LinkFailed(UNREACHABLE)` ngay
    (không chờ đủ 3 lần theo lịch); sau đó `device_gone` (không held) thì không còn lần thử treo/hẹn."""
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0, ADDR, PORT)
    assert notifies(t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)) == []
    c = the_connect(t.hint(PEER, ADDR2, PORT2, 0.2))
    acts = t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.3)
    assert notifies(acts) == [LinkFailed(PEER, UNREACHABLE)]
    assert t.next_wakeup() is not None, "mDNS tươi: lịch §6.2 vẫn chạy"
    assert t.device_gone(PEER, 0.4) == []
    assert t.attempting() == {} and t.connected() == {}
    assert t.next_wakeup() is None
    assert t.tick(3600.0) == []


def test_e2_each_hint_reports_once_even_with_fresh_mdns_and_schedule_does_not_repeat_it():
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0, ADDR, PORT)
    t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 0.1)
    now = 0.1
    for k in range(3):
        now += 10.0
        c = the_connect(t.hint(PEER, ADDR2, PORT2, now))
        acts = t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, now + 0.1)
        assert notifies(acts) == [LinkFailed(PEER, UNREACHABLE)], f"hint thứ {k + 1}"
    # Lịch §6.2 chạy tiếp; UNREACHABLE đã báo nên ≥ 3 lần thất bại theo lịch không báo lại.
    reports = []
    for _ in range(4):
        acts, at = tick_to_wakeup(t)
        c = the_connect(acts)
        reports += notifies(t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, at + 0.1))
    assert reports == []


def test_e2_hint_merged_with_fresh_mdns_same_port_reports_once():
    """Cùng cổng: lần thử do hint gộp địa chỉ hint + mDNS; thất bại vẫn tính là do hint → báo một lần."""
    t = tracker(PEER)
    c = start_attempt(t, PEER, 0.0, ADDR, PORT)
    t.connect_finished(PEER, c.attempt_id, OK, 1, 0.1)
    t.connection_lost(PEER, 1, 1.0)
    c = the_connect(t.hint(PEER, ADDR2, PORT, 1.2))
    assert c.addresses[: len(ADDR2)] == ADDR2
    assert notifies(t.connect_finished(PEER, c.attempt_id, UNREACHABLE, None, 1.3)) == [
        LinkFailed(PEER, UNREACHABLE)
    ]
