"""Test `PairingGate`: chống làm phiền theo IP và phân xử hai bên cùng chủ động.

Viết từ đặc tả (kế hoạch PR 1 mục 2.9, câu hỏi 4–5; docs/protocol.md §5.3), không từ code.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from teleport.core import pairing as core_pairing
from teleport.core.messages import CERT_PEM_HEADER, Hello, PairAbort, PairNonce
from teleport.core.pairing import (
    BACKOFF_BASE,
    BACKOFF_FORGET_AFTER,
    BACKOFF_MAX,
    MAX_TRACKED_IPS,
    Admission,
    Decision,
    Outcome,
    PairingGate,
    Role,
    Slot,
)

OWN = "8" * 32
LOWER = "1" * 32  # device_id nhỏ hơn OWN
HIGHER = "f" * 32  # device_id lớn hơn OWN
PEER = LOWER
IP = "192.168.1.20"
IP2 = "192.168.1.21"

# Mọi outcome tính là thất bại (trừ PAIRED xóa đếm và CANCELLED giữ nguyên).
FAILURE_OUTCOMES = [o for o in Outcome if o not in (Outcome.PAIRED, Outcome.CANCELLED)]


def gate(accepting: bool = True) -> PairingGate:
    return PairingGate(OWN, accepting=accepting)


def assert_busy(adm: Admission) -> None:
    assert adm.decision == Decision.BUSY
    assert adm.slot is None
    assert adm.cancel_outgoing is None


def assert_accept(adm: Admission, ip: str) -> Slot:
    assert adm.decision == Decision.ACCEPT
    assert isinstance(adm.slot, Slot)
    assert adm.slot.role == Role.RESPONDER
    assert adm.slot.ip == ip
    return adm.slot


def fail_once(g: PairingGate, ip: str, now: float, outcome: Outcome = Outcome.TIMEOUT) -> None:
    slot = assert_accept(g.admit(ip, PEER, now), ip)
    g.finish(slot, outcome, now)


# --- Hằng số, mặc định -------------------------------------------------------------------------


def test_constants():
    assert BACKOFF_BASE == 10.0
    assert BACKOFF_MAX == 600.0
    assert BACKOFF_FORGET_AFTER == 600.0
    assert MAX_TRACKED_IPS == 1024


def test_decision_values():
    assert Decision.ACCEPT == "accept"
    assert Decision.BUSY == "busy"


def test_admission_defaults():
    adm = Admission(Decision.BUSY)
    assert adm.slot is None
    assert adm.cancel_outgoing is None


def test_new_gate_defaults():
    g = PairingGate(OWN)
    assert g.accepting is False
    assert g.active is None
    assert g.failures(IP) == 0
    assert g.blocked_until(IP) is None


def test_accepting_is_writable():
    g = PairingGate(OWN)
    assert_busy(g.admit(IP, PEER, 0.0))
    g.accepting = True
    assert g.accepting is True
    assert_accept(g.admit(IP, PEER, 0.0), IP)


# --- Luật 1–6 của admit -------------------------------------------------------------------------


def test_rule1_own_device_id_is_busy():
    g = gate()
    assert_busy(g.admit(IP, OWN, 0.0))
    assert g.active is None


def test_rule1_own_device_id_beats_tie_break():
    g = gate()
    out = g.begin_outgoing(IP)
    assert_busy(g.admit(IP, OWN, 0.0))
    assert g.active == out


def test_rule3_not_accepting_is_busy():
    g = gate(accepting=False)
    assert_busy(g.admit(IP, PEER, 0.0))
    assert g.active is None


def test_rule4_active_responder_session_makes_others_busy():
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    assert_busy(g.admit(IP2, HIGHER, 1.0))
    assert_busy(g.admit(IP, PEER, 1.0))
    assert g.active == slot


def test_rule4_outgoing_to_other_ip_makes_request_busy():
    # Luật 2 chỉ áp khi yêu cầu tới từ ĐÚNG IP mình đang kết nối tới.
    g = gate()
    out = g.begin_outgoing(IP)
    assert_busy(g.admit(IP2, LOWER, 0.0))
    assert g.active == out


def test_rule4_busy_does_not_count_as_failure():
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    assert_busy(g.admit(IP2, PEER, 1.0))
    assert g.failures(IP2) == 0
    assert g.blocked_until(IP2) is None
    g.finish(slot, Outcome.PAIRED, 2.0)


def test_rule5_blocked_ip_is_busy():
    g = gate()
    fail_once(g, IP, 0.0)
    assert g.blocked_until(IP) == 10.0
    assert_busy(g.admit(IP, PEER, 9.999))
    assert g.active is None


def test_rule5_busy_while_blocked_does_not_extend_or_count():
    g = gate()
    fail_once(g, IP, 0.0)
    for t in (1.0, 5.0, 9.0, 9.999):
        assert_busy(g.admit(IP, PEER, t))
    assert g.failures(IP) == 1
    assert g.blocked_until(IP) == 10.0
    assert_accept(g.admit(IP, PEER, 10.0), IP)


def test_rule6_accept_creates_responder_slot_and_becomes_active():
    g = gate()
    adm = g.admit(IP, PEER, 0.0)
    slot = assert_accept(adm, IP)
    assert adm.cancel_outgoing is None
    assert g.active == slot


def test_slot_ids_increase():
    g = gate()
    s1 = g.begin_outgoing(IP)
    g.finish(s1, Outcome.PAIRED, 0.0)
    s2 = assert_accept(g.admit(IP, PEER, 0.0), IP)
    g.finish(s2, Outcome.PAIRED, 0.0)
    s3 = g.begin_outgoing(IP2)
    assert s1.id < s2.id < s3.id


# --- begin_outgoing -----------------------------------------------------------------------------


def test_begin_outgoing_creates_initiator_slot():
    g = gate()
    slot = g.begin_outgoing(IP)
    assert isinstance(slot, Slot)
    assert slot.role == Role.INITIATOR
    assert slot.ip == IP
    assert g.active == slot


def test_begin_outgoing_ignores_accepting():
    g = gate(accepting=False)
    assert g.begin_outgoing(IP) is not None


def test_begin_outgoing_none_when_session_active():
    g = gate()
    first = g.begin_outgoing(IP)
    assert g.begin_outgoing(IP2) is None
    assert g.active == first
    g.finish(first, Outcome.TIMEOUT, 0.0)
    responder = assert_accept(g.admit(IP, PEER, 0.0), IP)
    assert g.begin_outgoing(IP2) is None
    assert g.active == responder


def test_begin_outgoing_ignores_backoff():
    # Chờ tăng dần chỉ chặn yêu cầu đến, không chặn máy này chủ động.
    g = gate()
    fail_once(g, IP, 0.0)
    assert g.begin_outgoing(IP) is not None


# --- Luật 2: hai bên cùng chủ động --------------------------------------------------------------


def test_tie_break_larger_own_id_yields_outgoing():
    g = gate()
    out = g.begin_outgoing(IP)
    adm = g.admit(IP, LOWER, 0.0)
    slot = assert_accept(adm, IP)
    assert adm.cancel_outgoing == out
    assert slot != out
    assert g.active == slot


def test_tie_break_smaller_own_id_keeps_outgoing():
    g = gate()
    out = g.begin_outgoing(IP)
    assert_busy(g.admit(IP, HIGHER, 0.0))
    assert g.active == out


def test_tie_break_compares_strings_not_numbers():
    # "9…" > "10…" khi so chuỗi (ngược với so số).
    g = PairingGate("9" + "0" * 31, accepting=True)
    out = g.begin_outgoing(IP)
    adm = g.admit(IP, "1" + "0" * 31, 0.0)
    assert adm.decision == Decision.ACCEPT
    assert adm.cancel_outgoing == out


def test_tie_break_stands_above_not_accepting():
    g = gate(accepting=False)
    out = g.begin_outgoing(IP)
    adm = g.admit(IP, LOWER, 0.0)
    assert_accept(adm, IP)
    assert adm.cancel_outgoing == out


def test_tie_break_stands_above_backoff():
    g = gate()
    fail_once(g, IP, 0.0)
    out = g.begin_outgoing(IP)
    adm = g.admit(IP, LOWER, 1.0)  # IP đang bị chặn tới 10.0
    assert_accept(adm, IP)
    assert adm.cancel_outgoing == out


def test_tie_break_lower_own_id_busy_even_when_accepting_and_unblocked():
    g = gate(accepting=True)
    out = g.begin_outgoing(IP)
    assert_busy(g.admit(IP, HIGHER, 0.0))
    assert g.active == out
    assert g.failures(IP) == 0


def test_finish_of_yielded_outgoing_slot_is_ignored():
    g = gate()
    out = g.begin_outgoing(IP)
    adm = g.admit(IP, LOWER, 0.0)
    new = assert_accept(adm, IP)
    # App gửi Cancel(BUSY) cho phiên khởi xướng rồi báo finish với slot cũ.
    g.finish(out, Outcome.CANCELLED, 0.5)
    assert g.active == new
    g.finish(out, Outcome.TIMEOUT, 0.6)
    assert g.active == new
    assert g.failures(IP) == 0


@settings(max_examples=300, deadline=None)
@given(
    id_a=st.text(alphabet="0123456789abcdef", min_size=32, max_size=32),
    id_b=st.text(alphabet="0123456789abcdef", min_size=32, max_size=32),
    acc_a=st.booleans(),
    acc_b=st.booleans(),
    a_first=st.booleans(),
)
def test_property_exactly_one_side_yields(id_a, id_b, acc_a, acc_b, a_first):
    assume(id_a != id_b)
    ip_a, ip_b = "10.0.0.1", "10.0.0.2"
    ga = PairingGate(id_a, accepting=acc_a)
    gb = PairingGate(id_b, accepting=acc_b)
    out_a = ga.begin_outgoing(ip_b)
    out_b = gb.begin_outgoing(ip_a)
    if a_first:
        ra = ga.admit(ip_b, id_b, 1.0)
        rb = gb.admit(ip_a, id_a, 1.0)
    else:
        rb = gb.admit(ip_a, id_a, 1.0)
        ra = ga.admit(ip_b, id_b, 1.0)
    accepted = [r.decision == Decision.ACCEPT for r in (ra, rb)]
    assert accepted.count(True) == 1, "đúng một bên nhường"
    if id_a > id_b:
        assert ra.cancel_outgoing == out_a
        assert ga.active == ra.slot
        assert ra.slot.role == Role.RESPONDER
        assert_busy(rb)
        assert gb.active == out_b
    else:
        assert rb.cancel_outgoing == out_b
        assert gb.active == rb.slot
        assert rb.slot.role == Role.RESPONDER
        assert_busy(ra)
        assert ga.active == out_a


# --- finish ------------------------------------------------------------------------------------


def test_finish_releases_active():
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    g.finish(slot, Outcome.PAIRED, 1.0)
    assert g.active is None


def test_finish_when_nothing_active_is_ignored():
    g = gate()
    g.finish(Slot(id=999, role=Role.RESPONDER, ip=IP), Outcome.TIMEOUT, 0.0)
    assert g.active is None
    assert g.failures(IP) == 0


def test_finish_with_stale_slot_is_ignored():
    g = gate()
    old = assert_accept(g.admit(IP, PEER, 0.0), IP)
    g.finish(old, Outcome.CANCELLED, 0.0)
    current = assert_accept(g.admit(IP2, PEER, 1.0), IP2)
    g.finish(old, Outcome.TIMEOUT, 2.0)
    assert g.active == current
    assert g.failures(IP) == 0


def test_finish_twice_counts_once():
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    g.finish(slot, Outcome.TIMEOUT, 1.0)
    g.finish(slot, Outcome.TIMEOUT, 1.0)
    assert g.failures(IP) == 1


@pytest.mark.parametrize("outcome", FAILURE_OUTCOMES, ids=str)
def test_initiator_session_never_counts(outcome):
    # Chỉ tính phiên mà máy này là bên trả lời (câu 4).
    g = gate()
    slot = g.begin_outgoing(IP)
    g.finish(slot, outcome, 0.0)
    assert g.active is None
    assert g.failures(IP) == 0
    assert g.blocked_until(IP) is None


def test_initiator_paired_does_not_clear_responder_failures():
    # PAIRED chỉ xóa bộ đếm khi slot là RESPONDER.
    g = gate()
    fail_once(g, IP, 0.0)
    slot = g.begin_outgoing(IP)
    g.finish(slot, Outcome.PAIRED, 1.0)
    assert g.failures(IP) == 1
    assert g.blocked_until(IP) == 10.0


@pytest.mark.parametrize("outcome", FAILURE_OUTCOMES, ids=str)
def test_every_non_paired_non_cancelled_outcome_counts(outcome):
    # Gồm cả CONNECTION_LOST và IDENTITY_CONFLICT (câu 4).
    g = gate()
    fail_once(g, IP, 5.0, outcome)
    assert g.failures(IP) == 1
    assert g.blocked_until(IP) == 15.0


def test_cancelled_keeps_counter_unchanged():
    g = gate()
    fail_once(g, IP, 0.0)
    fail_once(g, IP, 10.0)
    assert (g.failures(IP), g.blocked_until(IP)) == (2, 30.0)
    slot = assert_accept(g.admit(IP, PEER, 30.0), IP)
    g.finish(slot, Outcome.CANCELLED, 35.0)
    assert (g.failures(IP), g.blocked_until(IP)) == (2, 30.0)


def test_cancelled_on_fresh_ip_creates_no_block():
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    g.finish(slot, Outcome.CANCELLED, 1.0)
    assert g.failures(IP) == 0
    assert g.blocked_until(IP) is None


# --- Dãy chờ tăng dần --------------------------------------------------------------------------


def test_backoff_sequence_10_to_600():
    g = gate()
    expected = [10, 20, 40, 80, 160, 320, 600, 600, 600]
    t = 1000.0
    for k, wait in enumerate(expected, start=1):
        slot = assert_accept(g.admit(IP, PEER, t), IP)
        g.finish(slot, Outcome.USER_REJECTED, t + 1)
        assert g.failures(IP) == k
        assert g.blocked_until(IP) == t + 1 + wait, f"lần thất bại thứ {k}"
        assert_busy(g.admit(IP, PEER, t + 1 + wait - 0.001))
        t = t + 1 + wait  # đúng lúc hết chờ thì được nhận lại


def test_backoff_measured_from_finish_time():
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    g.finish(slot, Outcome.TIMEOUT, 75.0)
    assert g.blocked_until(IP) == 85.0


def test_paired_clears_counter():
    g = gate()
    t = 0.0
    for _ in range(4):
        fail_once(g, IP, t)
        t = g.blocked_until(IP)
    assert g.failures(IP) == 4
    slot = assert_accept(g.admit(IP, PEER, t), IP)
    g.finish(slot, Outcome.PAIRED, t + 1)
    assert g.failures(IP) == 0
    assert g.blocked_until(IP) is None
    # Thất bại kế tiếp bắt đầu lại từ 10 s.
    fail_once(g, IP, t + 2)
    assert g.blocked_until(IP) == t + 2 + 10


def test_other_ip_not_affected():
    g = gate()
    fail_once(g, IP, 0.0)
    fail_once(g, IP, 10.0)
    assert g.failures(IP2) == 0
    assert g.blocked_until(IP2) is None
    assert_accept(g.admit(IP2, PEER, 11.0), IP2)


def test_ipv6_and_ipv4_tracked_separately():
    g = gate()
    fail_once(g, "fe80::1", 0.0)
    assert g.failures("fe80::1") == 1
    assert_accept(g.admit("192.168.1.1", PEER, 1.0), "192.168.1.1")


# --- Quên sau 600 s và giới hạn 1024 IP --------------------------------------------------------


def test_entry_kept_until_600s_after_block_ends():
    g = gate()
    fail_once(g, IP, 0.0)  # blocked_until = 10
    slot = assert_accept(g.admit(IP, PEER, 609.999), IP)
    assert g.failures(IP) == 1
    g.finish(slot, Outcome.CANCELLED, 609.999)
    assert g.failures(IP) == 1


def test_entry_forgotten_600s_after_block_ends():
    g = gate()
    fail_once(g, IP, 0.0)
    fail_once(g, IP, 10.0)  # blocked_until = 30
    slot = assert_accept(g.admit(IP, PEER, 630.0), IP)
    assert g.failures(IP) == 0
    assert g.blocked_until(IP) is None
    g.finish(slot, Outcome.TIMEOUT, 630.0)
    # Đếm lại từ đầu: chờ 10 s, không phải 40 s.
    assert g.failures(IP) == 1
    assert g.blocked_until(IP) == 640.0


def test_forget_happens_on_admit_from_other_ip():
    g = gate()
    fail_once(g, IP, 0.0)
    slot = assert_accept(g.admit(IP2, PEER, 610.0), IP2)
    g.finish(slot, Outcome.CANCELLED, 610.0)
    assert g.failures(IP) == 0
    assert g.blocked_until(IP) is None


def test_forget_happens_on_finish():
    g = gate()
    fail_once(g, IP, 0.0)
    out = g.begin_outgoing(IP2)
    g.finish(out, Outcome.TIMEOUT, 610.0)
    assert g.failures(IP) == 0


def test_tracks_at_most_1024_ips_evicting_earliest_block():
    g = gate()
    ips = [f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}" for i in range(MAX_TRACKED_IPS + 1)]
    for i, ip in enumerate(ips):
        fail_once(g, ip, i * 0.01)  # blocked_until tăng dần theo i
    # Một lời gọi admit nữa để chắc chắn đã dọn (dọn khi admit hoặc finish).
    extra = assert_accept(g.admit("172.16.0.1", PEER, 20.0), "172.16.0.1")
    g.finish(extra, Outcome.CANCELLED, 20.0)
    tracked = [ip for ip in ips if g.failures(ip) > 0]
    assert len(tracked) == MAX_TRACKED_IPS
    assert ips[0] not in tracked, "phải xóa mục có blocked_until nhỏ nhất"
    assert g.failures(ips[-1]) == 1
    assert g.failures(ips[1]) == 1


def test_1024_ips_are_all_kept():
    g = gate()
    ips = [f"10.1.{i // 256}.{i % 256}" for i in range(MAX_TRACKED_IPS)]
    for i, ip in enumerate(ips):
        fail_once(g, ip, i * 0.01)
    extra = assert_accept(g.admit("172.16.0.1", PEER, 20.0), "172.16.0.1")
    g.finish(extra, Outcome.CANCELLED, 20.0)
    assert all(g.failures(ip) == 1 for ip in ips)


def test_ip_with_active_responder_session_is_not_forgotten():
    # Mục của IP đang có phiên trả lời không được dọn, dù đã quá blocked_until + 600.
    g = gate()
    fail_once(g, IP, 0.0)  # chặn tới 10
    slot = assert_accept(g.admit(IP, PEER, 609.0), IP)
    assert_busy(g.admit(IP2, PEER, 700.0))  # lời gọi này có dọn bộ nhớ
    g.finish(slot, Outcome.TIMEOUT, 700.0)
    assert g.failures(IP) == 2
    assert g.blocked_until(IP) == 720.0


def test_just_failed_ip_is_never_evicted_when_over_limit():
    g = gate()
    ips = [f"10.2.{i // 256}.{i % 256}" for i in range(MAX_TRACKED_IPS)]
    for ip in ips:
        fail_once(g, ip, 0.0)  # chặn tới 10
    for ip in ips:
        fail_once(g, ip, 10.0)  # lần hai: chặn tới 30
    assert all(g.blocked_until(ip) == 30.0 for ip in ips)
    newcomer = "172.16.5.5"
    fail_once(g, newcomer, 11.0)  # blocked_until 21: nhỏ nhất, nhưng là IP vừa thất bại
    assert g.failures(newcomer) == 1
    assert g.blocked_until(newcomer) == 21.0
    assert_busy(g.admit(newcomer, PEER, 12.0))
    assert g.failures(newcomer) == 1
    # Mục bị xóa phải là một trong các IP cũ (blocked_until nhỏ nhất trong số còn lại).
    tracked_old = [ip for ip in ips if g.failures(ip) > 0]
    assert len(tracked_old) == MAX_TRACKED_IPS - 1


def test_backoff_stays_600_after_1100_failures():
    g = gate()
    t = 0.0
    for _ in range(1100):
        fail_once(g, IP, t)
        t = g.blocked_until(IP)
    assert g.failures(IP) == 1100
    slot = assert_accept(g.admit(IP, PEER, t), IP)
    g.finish(slot, Outcome.TIMEOUT, t)
    assert g.failures(IP) == 1101
    assert g.blocked_until(IP) == t + 600.0


# --- Đầu vào sai --------------------------------------------------------------------------------


@pytest.mark.parametrize("own", ["xyz", "", "A" * 32, "0" * 31, None, 1], ids=repr)
def test_gate_rejects_bad_own_device_id(own):
    with pytest.raises(ValueError):
        PairingGate(own)


@pytest.mark.parametrize("value", [1, 0, "yes", None], ids=repr)
def test_accepting_assignment_requires_bool(value):
    g = gate()
    with pytest.raises(ValueError):
        g.accepting = value


@pytest.mark.parametrize("bad_now", [float("nan"), float("inf"), float("-inf")], ids=repr)
def test_admit_rejects_non_finite_now(bad_now):
    with pytest.raises(ValueError):
        gate().admit(IP, PEER, bad_now)


@pytest.mark.parametrize("bad_now", [True, "1", None], ids=repr)
def test_admit_rejects_non_number_now(bad_now):
    with pytest.raises(TypeError):
        gate().admit(IP, PEER, bad_now)


def test_admit_rejects_time_going_backwards():
    g = gate()
    fail_once(g, IP, 50.0)
    with pytest.raises(ValueError):
        g.admit(IP2, PEER, 49.0)


@pytest.mark.parametrize("bad_now", [float("nan"), float("inf"), float("-inf")], ids=repr)
def test_finish_rejects_non_finite_now(bad_now):
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    with pytest.raises(ValueError):
        g.finish(slot, Outcome.TIMEOUT, bad_now)


@pytest.mark.parametrize("bad_now", [True, "1", None], ids=repr)
def test_finish_rejects_non_number_now(bad_now):
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    with pytest.raises(TypeError):
        g.finish(slot, Outcome.TIMEOUT, bad_now)


def test_finish_rejects_time_going_backwards():
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 10.0), IP)
    with pytest.raises(ValueError):
        g.finish(slot, Outcome.TIMEOUT, 9.0)


@pytest.mark.parametrize("ip", ["", None, 1, b"192.168.1.1"], ids=repr)
def test_admit_rejects_bad_ip(ip):
    with pytest.raises(ValueError):
        gate().admit(ip, PEER, 0.0)


@pytest.mark.parametrize("peer_id", ["", "xyz", "A" * 32, "0" * 31, "0" * 33, None, 1], ids=repr)
def test_admit_rejects_bad_peer_device_id(peer_id):
    with pytest.raises(ValueError):
        gate().admit(IP, peer_id, 0.0)


@pytest.mark.parametrize("slot", [None, 1, "slot", (1, Role.RESPONDER, IP)], ids=repr)
def test_finish_rejects_non_slot(slot):
    g = gate()
    assert_accept(g.admit(IP, PEER, 0.0), IP)
    with pytest.raises(ValueError):
        g.finish(slot, Outcome.TIMEOUT, 1.0)


@pytest.mark.parametrize("outcome", [None, "xyz", 1, Decision.BUSY], ids=repr)
def test_finish_rejects_unknown_outcome(outcome):
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    with pytest.raises(ValueError):
        g.finish(slot, outcome, 1.0)


# =================================================================================================
# PR-4: địa chỉ khác của cùng máy đích (`begin_outgoing(ip, also=...)`, `Slot.aliases`; đặc tả PR-4
# mục 2.3, PG1–PG3). PG3 = mọi test ở trên chạy không sửa.
# =================================================================================================

IP3 = "192.168.1.22"


def test_pg_slot_aliases_default_is_empty():
    slot = Slot(1, Role.INITIATOR, IP)
    assert slot.aliases == frozenset()
    assert gate().begin_outgoing(IP).aliases == frozenset()


def test_pg1_request_from_alias_counts_as_both_initiating_when_own_is_larger():
    g = gate(accepting=False)
    out = g.begin_outgoing(IP, also=[IP2])
    assert out.ip == IP and out.aliases == frozenset({IP2})
    adm = g.admit(IP2, LOWER, 0.0)
    slot = assert_accept(adm, IP2)
    assert adm.cancel_outgoing == out
    assert g.active == slot


def test_pg1_request_from_alias_is_busy_when_own_is_smaller():
    g = gate()
    out = g.begin_outgoing(IP, also=[IP2])
    assert_busy(g.admit(IP2, HIGHER, 0.0))
    assert g.active == out
    assert g.failures(IP2) == 0


def test_pg1_request_from_other_ip_is_busy():
    g = gate(accepting=False)
    out = g.begin_outgoing(IP, also=[IP2])
    assert_busy(g.admit(IP3, LOWER, 0.0))
    assert g.active == out


def test_pg1_alias_rule_also_beats_backoff():
    g = gate()
    fail_once(g, IP2, 0.0)  # IP2 đang bị chặn tới 10.0
    out = g.begin_outgoing(IP, also=[IP2])
    adm = g.admit(IP2, LOWER, 1.0)
    assert_accept(adm, IP2)
    assert adm.cancel_outgoing == out


@pytest.mark.parametrize(
    "also",
    [
        [IP2, 1],  # `_check_ip`: chuỗi khác rỗng
        [IP2, ""],
        [None],
        [b"192.168.1.21"],
        "192.168.1.21",
        b"192.168.1.21",
        [f"10.0.0.{i}" for i in range(1, 17)],  # 16 địa chỉ khác ip
    ],
    ids=["khong_phai_chuoi", "rong", "none", "bytes_phan_tu", "chuoi", "bytes", "16_dia_chi"],
)
def test_pg2_bad_also_raises_and_gate_stays_free(also):
    g = gate()
    with pytest.raises(ValueError):
        g.begin_outgoing(IP, also=also)
    assert g.active is None
    assert g.begin_outgoing(IP) is not None


def test_pg2_fifteen_other_addresses_are_allowed():
    g = gate()
    also = [f"10.0.0.{i}" for i in range(1, 16)]
    out = g.begin_outgoing(IP, also=[*also, IP, also[0]])  # chính ip và phần tử trùng không tính
    assert out.aliases == frozenset(also)


def test_pg2_own_ip_and_duplicates_are_dropped():
    g = gate()
    out = g.begin_outgoing(IP, also=(IP, IP2, IP2))
    assert out.aliases == frozenset({IP2})
    adm = g.admit(IP, LOWER, 0.0)  # ip chính vẫn khớp như cũ
    assert adm.cancel_outgoing == out


def test_pg2_also_accepts_any_iterable():
    g = gate()
    out = g.begin_outgoing(IP, also=(x for x in [IP2, IP3]))
    assert out.aliases == frozenset({IP2, IP3})


def test_pg_responder_slot_has_no_aliases():
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    assert slot.aliases == frozenset()


# =================================================================================================
# L4: giới hạn số mã hiện ra ở vai trả lời (kế hoạch L1/L4 mục "L4"). Hằng số và hàm mới được đọc
# qua module (`core_pairing.X`) để các test ở trên vẫn chạy khi chúng chưa có, và để monkeypatch
# được (hằng số là biến toàn cục đọc lúc dùng).
# =================================================================================================

IP4 = "192.168.1.23"


def show_code(g: PairingGate, now: float, ip: str = IP, outcome: Outcome = Outcome.CANCELLED) -> int:
    """Một phiên trả lời được nhận, hiện mã lúc `now`, rồi kết thúc (mặc định CANCELLED: không
    đụng bộ đếm theo IP). Trả giá trị `code_shown`."""
    slot = assert_accept(g.admit(ip, PEER, now), ip)
    count = g.code_shown(slot, now)
    g.finish(slot, outcome, now)
    return count


def test_code_limit_constants():
    assert core_pairing.CODE_WINDOW == 60.0
    assert core_pairing.MAX_CODES_PER_WINDOW == 5
    assert core_pairing.WARN_RECENT_CODES == 3


def test_new_gate_has_no_recent_codes():
    assert gate().recent_codes(0.0) == 0


def test_code_shown_counts_itself_and_previous_codes():
    g = gate()
    assert [show_code(g, float(t)) for t in range(5)] == [1, 2, 3, 4, 5]
    assert g.recent_codes(4.0) == 5


def test_sixth_request_in_window_is_busy():
    g = gate()
    for t in range(5):
        show_code(g, float(t))
    assert_busy(g.admit(IP2, PEER, 5.0))
    assert g.active is None
    assert_busy(g.admit(IP3, PEER, 59.999))  # mã đầu (t=0) vẫn còn trong cửa sổ


def test_limit_frees_exactly_when_first_code_leaves_window():
    """Mã hiện lúc t không còn được đếm đúng tại t + 60 (đếm các mốc có t > now − 60)."""
    g = gate()
    t0 = 1000.0
    for i in range(5):
        show_code(g, t0 + i)
    assert_busy(g.admit(IP2, PEER, t0 + 59.5))
    assert g.recent_codes(t0 + 59.5) == 5
    assert g.recent_codes(t0 + 60.0) == 4
    assert_accept(g.admit(IP2, PEER, t0 + 60.0), IP2)


def test_recent_codes_boundary_is_exactly_t_plus_window():
    g = gate()
    show_code(g, 100.0)
    assert g.recent_codes(100.0) == 1
    assert g.recent_codes(159.75) == 1
    assert g.recent_codes(160.0) == 0
    assert g.recent_codes(1000.0) == 0


def test_window_slides_one_code_at_a_time():
    g = gate()
    for t in (0.0, 10.0, 20.0, 30.0, 40.0):
        show_code(g, t)
    assert_busy(g.admit(IP, PEER, 59.5))
    assert show_code(g, 60.0) == 5  # mã t=0 vừa hết, mã mới được đếm
    assert_busy(g.admit(IP, PEER, 69.5))
    assert show_code(g, 70.0) == 5  # mã t=10 vừa hết
    assert g.recent_codes(70.0) == 5


def test_limit_counts_codes_not_requests():
    """Phiên được nhận nhưng chưa hiện mã (máy kia bỏ đi trước `pair_nonce`) không tính."""
    g = gate()
    for t in range(10):
        slot = assert_accept(g.admit(IP, PEER, float(t)), IP)
        g.finish(slot, Outcome.CANCELLED, float(t))
    assert g.recent_codes(10.0) == 0
    assert_accept(g.admit(IP, PEER, 10.0), IP)


def test_busy_by_code_limit_does_not_touch_ip_counters():
    g = gate()
    fail_once(g, IP2, 0.0)  # IP2: 1 lần thất bại, chặn tới 10
    for t in range(5):
        show_code(g, 20.0 + t, ip=IP4)
    assert_busy(g.admit(IP, PEER, 30.0))
    assert_busy(g.admit(IP2, PEER, 30.0))
    assert (g.failures(IP), g.blocked_until(IP)) == (0, None)
    assert (g.failures(IP2), g.blocked_until(IP2)) == (1, 10.0)
    assert (g.failures(IP4), g.blocked_until(IP4)) == (0, None)


def test_codes_from_failed_sessions_count_too():
    # Mã đã hiện thì được đếm, bất kể phiên kết thúc thế nào (giới hạn tính cho cả máy, không theo IP).
    g = gate()
    for i, ip in enumerate((IP, IP2, IP3, IP4, "10.0.0.9")):
        show_code(g, float(i), ip=ip, outcome=Outcome.USER_REJECTED)
    assert g.recent_codes(5.0) == 5
    assert_busy(g.admit("10.0.0.10", PEER, 5.0))
    assert g.failures("10.0.0.10") == 0


def test_tie_break_is_not_blocked_by_code_limit_but_its_code_counts():
    g = gate()
    for t in range(5):
        show_code(g, float(t))
    out = g.begin_outgoing(IP2)
    adm = g.admit(IP2, LOWER, 10.0)  # máy này lớn hơn: nhường phiên khởi xướng
    slot = assert_accept(adm, IP2)
    assert adm.cancel_outgoing == out
    assert g.code_shown(slot, 10.0) == 6
    assert g.recent_codes(10.0) == 6
    g.finish(slot, Outcome.PAIRED, 11.0)
    assert_busy(g.admit(IP3, PEER, 12.0))


def test_tie_break_lower_own_id_still_busy_under_limit():
    g = gate()
    for t in range(5):
        show_code(g, float(t))
    out = g.begin_outgoing(IP2)
    assert_busy(g.admit(IP2, HIGHER, 10.0))
    assert g.active == out


def test_code_shown_with_initiator_slot_records_nothing():
    g = gate()
    show_code(g, 0.0)
    show_code(g, 1.0)
    out = g.begin_outgoing(IP2)
    assert g.code_shown(out, 2.0) == 2
    assert g.code_shown(out, 3.0) == 2
    assert g.recent_codes(3.0) == 2
    g.finish(out, Outcome.PAIRED, 4.0)
    assert g.recent_codes(4.0) == 2


def test_code_shown_with_initiator_slot_on_fresh_gate_returns_zero():
    g = gate()
    out = g.begin_outgoing(IP)
    assert g.code_shown(out, 0.0) == 0
    assert g.recent_codes(0.0) == 0


def test_code_shown_with_finished_slot_records_nothing():
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    g.finish(slot, Outcome.CANCELLED, 1.0)
    assert g.code_shown(slot, 2.0) == 0
    assert g.recent_codes(2.0) == 0


def test_code_shown_with_stale_slot_while_other_session_active_records_nothing():
    g = gate()
    old = assert_accept(g.admit(IP, PEER, 0.0), IP)
    g.finish(old, Outcome.CANCELLED, 0.0)
    current = assert_accept(g.admit(IP2, PEER, 1.0), IP2)
    assert g.code_shown(old, 2.0) == 0
    assert g.code_shown(current, 3.0) == 1
    assert g.recent_codes(3.0) == 1


def test_code_shown_with_yielded_outgoing_slot_records_nothing():
    g = gate()
    out = g.begin_outgoing(IP)
    responder = assert_accept(g.admit(IP, LOWER, 0.0), IP)
    assert g.code_shown(out, 1.0) == 0
    assert g.code_shown(responder, 1.0) == 1


def test_code_shown_with_unknown_slot_records_nothing():
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    other = Slot(slot.id + 1, Role.RESPONDER, IP)
    assert g.code_shown(other, 1.0) == 0
    assert g.recent_codes(1.0) == 0
    assert g.code_shown(slot, 1.0) == 1


@pytest.mark.parametrize("limit", [65, 10_000])
def test_limit_above_64_makes_admit_raise_without_changing_state(monkeypatch, limit):
    """Bộ nhớ chỉ giữ 64 mốc: MAX_CODES_PER_WINDOW > 64 thì giới hạn sẽ âm thầm mất tác dụng, nên
    `admit` raise ValueError ngay đầu hàm, trước mọi thay đổi trạng thái (kể cả đồng hồ và việc dọn
    mốc hết hạn)."""
    g = gate()
    fail_once(g, IP2, 0.0)  # IP2: 1 lần thất bại, chặn tới 10
    show_code(g, 1.0)
    show_code(g, 2.0)
    monkeypatch.setattr(core_pairing, "MAX_CODES_PER_WINDOW", limit)
    with pytest.raises(ValueError):
        g.admit(IP, PEER, 100.0)  # 100 > 2 + 60: nếu dọn mốc trước khi raise thì sẽ mất 2 mã
    assert g.active is None
    assert (g.failures(IP), g.blocked_until(IP)) == (0, None)
    assert (g.failures(IP2), g.blocked_until(IP2)) == (1, 10.0)

    monkeypatch.setattr(core_pairing, "MAX_CODES_PER_WINDOW", 5)
    assert g.recent_codes(3.0) == 2  # đồng hồ không bị đẩy tới 100, hai mã còn nguyên
    assert show_code(g, 3.0) == 3


def test_limit_above_64_raises_while_session_active_and_keeps_it(monkeypatch):
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    monkeypatch.setattr(core_pairing, "MAX_CODES_PER_WINDOW", 65)
    with pytest.raises(ValueError):
        g.admit(IP2, PEER, 1.0)
    assert g.active == slot
    monkeypatch.setattr(core_pairing, "MAX_CODES_PER_WINDOW", 5)
    assert g.code_shown(slot, 1.0) == 1


def test_limit_of_64_is_fully_enforced(monkeypatch):
    """MAX = 64 (mức tối đa cho phép): 64 mã trong cùng cửa sổ được đếm đủ, `admit` thứ 65 → BUSY."""
    monkeypatch.setattr(core_pairing, "MAX_CODES_PER_WINDOW", 64)
    g = gate()
    times = [i * 0.5 for i in range(64)]  # 0.0 … 31.5, cùng trong một cửa sổ 60 s
    assert [show_code(g, t) for t in times] == list(range(1, 65))
    assert g.recent_codes(31.5) == 64
    assert_busy(g.admit(IP2, PEER, 32.0))
    assert g.recent_codes(32.0) == 64
    assert (g.failures(IP2), g.blocked_until(IP2)) == (0, None)


def test_keeps_newest_64_codes_when_tie_break_adds_a_65th(monkeypatch):
    """Luật hai bên cùng chủ động không bị giới hạn chặn, nên có thể có mã thứ 65: khi đó bộ nhớ
    64 mốc bỏ mốc CŨ nhất."""
    monkeypatch.setattr(core_pairing, "MAX_CODES_PER_WINDOW", 64)
    g = gate()
    for i in range(64):
        show_code(g, i * 0.5)  # 0.0 … 31.5
    out = g.begin_outgoing(IP2)
    adm = g.admit(IP2, LOWER, 32.0)
    slot = assert_accept(adm, IP2)
    assert adm.cancel_outgoing == out
    assert g.code_shown(slot, 32.0) == 64
    g.finish(slot, Outcome.PAIRED, 32.0)
    # Còn giữ 0.5 … 32.0: tại 60.5 mốc 0.5 vừa hết → 63. (Nếu bỏ mốc mới 32.0 thì còn 62.)
    assert g.recent_codes(60.5) == 63


def test_code_limit_constants_are_read_at_use_time(monkeypatch):
    monkeypatch.setattr(core_pairing, "MAX_CODES_PER_WINDOW", 2)
    monkeypatch.setattr(core_pairing, "CODE_WINDOW", 10.0)
    g = gate()
    show_code(g, 0.0)
    show_code(g, 1.0)
    assert_busy(g.admit(IP, PEER, 9.5))
    assert g.recent_codes(10.0) == 1
    assert_accept(g.admit(IP, PEER, 10.0), IP)


@pytest.mark.parametrize("slot", [None, 1, "slot", (1, Role.RESPONDER, IP)], ids=repr)
def test_code_shown_rejects_non_slot(slot):
    g = gate()
    assert_accept(g.admit(IP, PEER, 0.0), IP)
    with pytest.raises(ValueError):
        g.code_shown(slot, 1.0)
    assert g.recent_codes(1.0) == 0


@pytest.mark.parametrize("bad_now", [float("nan"), float("inf"), float("-inf")], ids=repr)
def test_code_shown_rejects_non_finite_now(bad_now):
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    with pytest.raises(ValueError):
        g.code_shown(slot, bad_now)


@pytest.mark.parametrize("bad_now", [True, "1", None], ids=repr)
def test_code_shown_rejects_non_number_now(bad_now):
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 0.0), IP)
    with pytest.raises(TypeError):
        g.code_shown(slot, bad_now)


def test_code_shown_rejects_time_going_backwards_and_records_nothing():
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 10.0), IP)
    with pytest.raises(ValueError):
        g.code_shown(slot, 9.0)
    assert g.recent_codes(10.0) == 0
    assert g.code_shown(slot, 10.0) == 1


def test_code_shown_shares_the_gate_clock():
    # `now` kiểm như các hàm khác của gate: sau code_shown(20) thì finish(19) là thời gian lùi.
    g = gate()
    slot = assert_accept(g.admit(IP, PEER, 10.0), IP)
    g.code_shown(slot, 20.0)
    with pytest.raises(ValueError):
        g.finish(slot, Outcome.CANCELLED, 19.0)


_GAPS = [0.0, 0.5, 1.0, 7.5, 15.0, 30.0, 59.5, 60.0, 60.5, 120.0]  # cộng dồn vẫn chính xác trong float


@settings(max_examples=300, deadline=None)
@given(gaps=st.lists(st.sampled_from(_GAPS), min_size=1, max_size=40))
def test_property_admit_matches_sliding_window_model(gaps):
    """Mô hình: BUSY khi đã có ≥ MAX mã với t > now − WINDOW; ngược lại ACCEPT và mã mới được đếm."""
    window = core_pairing.CODE_WINDOW
    limit = core_pairing.MAX_CODES_PER_WINDOW
    g = gate()
    shown: list[float] = []
    now = 0.0
    for gap in gaps:
        now += gap
        in_window = [t for t in shown if t > now - window]
        assert g.recent_codes(now) == len(in_window)
        adm = g.admit(IP, PEER, now)
        if len(in_window) >= limit:
            assert_busy(adm)
            continue
        slot = assert_accept(adm, IP)
        assert g.code_shown(slot, now) == len(in_window) + 1
        shown.append(now)
        g.finish(slot, Outcome.CANCELLED, now)
        assert g.failures(IP) == 0


# =================================================================================================
# Kẻ lạ nhận pair_commit rồi im lặng: phiên trả lời hết giờ ở NONCE_TIMEOUT (không phải
# MESSAGE_TIMEOUT 10 s), nhả slot duy nhất của gate. Gate không đổi: kết quả TIMEOUT tính là một
# lần thất bại của IP đó, IP khác được nhận ngay, và không có mã nào được ghi (chưa hiện mã).
# =================================================================================================

_FAKE_PEM = f"{CERT_PEM_HEADER}\nZmFrZQ==\n-----END CERTIFICATE-----\n"
_LOCAL_CERT = core_pairing.DeviceCert(device_id=OWN, fingerprint="b" * 64, pem=_FAKE_PEM)
_PEER_CERT = core_pairing.DeviceCert(device_id=PEER, fingerprint="a" * 64, pem=_FAKE_PEM)
_PEER_HELLO = Hello(protocol=1, device_id=PEER, name="Kẻ lạ", app_version="0.1.0", os="linux")


def _verify_never_true(payload: bytes, signature: bytes) -> bool:
    """`verify_proof` giả: máy lạ không có khóa, chữ ký luôn sai (protocol 5.1–5.2)."""
    return False


def _admit_responder(g: PairingGate, ip: str, now: float, verify_proof=_verify_never_true):
    """admit → ACCEPT → tạo phiên trả lời và Start (gửi pair_commit) ở `now`."""
    slot = assert_accept(g.admit(ip, PEER, now), ip)
    session = core_pairing.ResponderSession(
        local=_LOCAL_CERT,
        peer=_PEER_CERT,
        peer_hello=_PEER_HELLO,
        nonce=bytes(32),
        known_fingerprint=None,
        verify_proof=verify_proof,
    )
    out = session.handle(core_pairing.Start(), now)
    assert [type(a.message).__name__ for a in out] == ["PairCommit"]
    return slot, session


def test_silent_peer_after_commit_frees_gate_at_nonce_timeout():
    g = gate()
    t0 = 100.0
    nonce_timeout = core_pairing.NONCE_TIMEOUT
    slot, session = _admit_responder(g, IP, t0)

    # Còn trong hạn: phiên vẫn giữ slot, mọi IP khác nhận BUSY.
    assert session.handle(core_pairing.Tick(), t0 + nonce_timeout - 0.001) == []
    assert g.active == slot
    assert_busy(g.admit(IP2, HIGHER, t0 + nonce_timeout - 0.001))

    # Đúng t0 + NONCE_TIMEOUT: pair_abort{timeout} + Close(TIMEOUT); app gọi finish ngay lúc đó.
    out = session.handle(core_pairing.Tick(), t0 + nonce_timeout)
    assert [a.message for a in out[:-1]] == [PairAbort("timeout")]
    close = out[-1]
    assert isinstance(close, core_pairing.Close)
    assert close.outcome == Outcome.TIMEOUT
    g.finish(slot, close.outcome, t0 + nonce_timeout)

    assert g.active is None
    assert g.recent_codes(t0 + nonce_timeout) == 0, "chưa hiện mã thì không được đếm mã"
    assert g.failures(IP) == 1
    end = t0 + nonce_timeout + BACKOFF_BASE
    assert g.blocked_until(IP) == end

    # IP khác được nhận ngay; cùng IP nhận BUSY tới hết thời gian chờ tăng dần.
    other = assert_accept(g.admit(IP2, PEER, t0 + nonce_timeout), IP2)
    g.finish(other, Outcome.CANCELLED, t0 + nonce_timeout)
    assert_busy(g.admit(IP, PEER, t0 + nonce_timeout))
    assert_busy(g.admit(IP, PEER, end - 0.001))
    assert_accept(g.admit(IP, PEER, end), IP)
    assert g.recent_codes(end) == 0


def test_silent_peer_slot_is_not_held_until_message_timeout():
    # Hồi quy: trước đây slot bị giữ tới MESSAGE_TIMEOUT. Ở t0 + NONCE_TIMEOUT phiên đã kết thúc.
    g = gate()
    slot, session = _admit_responder(g, IP, 0.0)
    assert session.deadline == core_pairing.NONCE_TIMEOUT
    assert session.deadline < core_pairing.MESSAGE_TIMEOUT
    out = session.handle(core_pairing.Tick(), core_pairing.NONCE_TIMEOUT)
    assert out and out[-1].outcome == Outcome.TIMEOUT
    g.finish(slot, out[-1].outcome, core_pairing.NONCE_TIMEOUT)
    assert g.active is None


def test_late_nonce_after_nonce_timeout_never_reaches_code_shown():
    g = gate()
    slot, session = _admit_responder(g, IP, 0.0)
    out = session.handle(
        core_pairing.MessageReceived(PairNonce(nonce="11" * 32, signature="22" * 64)),
        core_pairing.NONCE_TIMEOUT,
    )
    assert not any(isinstance(a, core_pairing.ShowCode) for a in out)
    assert out[-1].outcome == Outcome.TIMEOUT
    g.finish(slot, out[-1].outcome, core_pairing.NONCE_TIMEOUT)
    assert g.recent_codes(core_pairing.NONCE_TIMEOUT) == 0
    assert g.active is None


# =================================================================================================
# Chữ ký sai trong `pair_nonce` (protocol 5.1): PROTOCOL_ERROR như mọi message sai, tính một lần
# thất bại cho IP đó (chờ tăng dần), không có mã nào được hiện hay đếm.
# =================================================================================================


def test_bad_signature_counts_as_failure_for_backoff_without_code():
    g = gate()
    calls: list[tuple[bytes, bytes]] = []

    def verify(payload: bytes, signature: bytes) -> bool:
        calls.append((payload, signature))
        return False

    slot, session = _admit_responder(g, IP, 0.0, verify_proof=verify)
    out = session.handle(core_pairing.MessageReceived(PairNonce(nonce="11" * 32, signature="22" * 64)), 1.0)
    assert len(calls) == 1
    assert [a.message for a in out[:-1]] == [PairAbort("invalid")]
    assert not any(isinstance(a, core_pairing.ShowCode) for a in out)
    close = out[-1]
    assert isinstance(close, core_pairing.Close)
    assert close.outcome == Outcome.PROTOCOL_ERROR
    g.finish(slot, close.outcome, 1.0)
    assert g.active is None
    assert g.failures(IP) == 1
    assert g.blocked_until(IP) == 1.0 + BACKOFF_BASE
    assert g.recent_codes(1.0) == 0
    other = assert_accept(g.admit(IP2, PEER, 1.0), IP2)  # IP khác không bị ảnh hưởng
    g.finish(other, Outcome.CANCELLED, 1.0)
    assert_busy(g.admit(IP, PEER, 1.0 + BACKOFF_BASE - 0.001))


def test_repeated_bad_signatures_grow_backoff():
    g = gate()
    now = 0.0
    for i in range(1, 4):
        slot, session = _admit_responder(g, IP, now)
        out = session.handle(
            core_pairing.MessageReceived(PairNonce(nonce="11" * 32, signature="22" * 64)), now
        )
        g.finish(slot, out[-1].outcome, now)
        assert g.failures(IP) == i
        until = g.blocked_until(IP)
        assert until == now + min(BACKOFF_BASE * 2 ** (i - 1), BACKOFF_MAX)
        now = until
    assert g.recent_codes(now) == 0
