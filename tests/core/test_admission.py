"""Test `teleport.core.admission`: cửa nhận kết nối trước TLS (token bucket, hai pool, vé, ưu tiên).

Viết từ `docs/protocol.md` mục 2.3 (không từ code): thứ tự kiểm “chọn pool → số đang mở đầu (theo IP, rồi
tổng, của pool đó) → bucket theo IP → bucket tổng”; chỉ trừ token khi qua mọi bước; `trusted` hoàn
token đúng một lần, không vượt dung lượng; IP ưu tiên chỉ dùng pool ưu tiên; ưu tiên giữ khi còn kết
nối sống hoặc chưa quá `PRIORITY_TTL` từ lúc kết nối cuối đóng; `retain_trusted` thắng kết nối sống;
bảng có trần và không bỏ mục có vé sống; `now` hữu hạn, không giảm, sai → `ValueError`, trạng thái
không đổi.

Module chạy với **mặc định thật** (`real_admission_limits`: fixture tự động trong `tests/conftest.py`
không nới gì). Hằng được đặt lại bằng `monkeypatch` trên `teleport.core.admission` trước khi tạo gate.
Thời gian dùng bội số của 0,25 s nên phép cộng token là chính xác (không lệch dấu phẩy động).
"""

from __future__ import annotations

import dataclasses
import math
from collections import Counter

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.core import admission
from teleport.core.admission import AdmissionGate, Pool, Refusal, Ticket

pytestmark = pytest.mark.real_admission_limits

A, B, C, D, E, F = (f"192.168.1.{i}" for i in range(10, 16))
STRANGERS = [f"10.0.{i // 250}.{i % 250 + 1}" for i in range(400)]
FP = {ip: f"{i:02x}" * 32 for i, ip in enumerate((A, B, C, D, E, F))}
HOUR = 3600.0


# --- Hàm phụ trợ ---------------------------------------------------------------------------------
# Chữ ký `control_opened` / `retain_trusted` do lap-trinh chốt (đặc tả: “gate lưu fingerprint theo
# handle”); mọi lời gọi đi qua hai hàm dưới để chỉ phải khớp một chỗ.


def opened(gate: AdmissionGate, ip: str, now: float, fp: str | None = None) -> int:
    """Kết nối điều khiển đã xác thực mTLS tới/từ `ip` (thiết bị có fingerprint `fp`)."""
    return gate.control_opened(ip, FP.get(ip, "ee" * 32) if fp is None else fp, now)


def retain(gate: AdmissionGate, fps) -> None:
    """Chỉ giữ các thiết bị có fingerprint trong `fps` (hủy ghép đôi các thiết bị khác)."""
    gate.retain_trusted(frozenset(fps))


def ticket(result) -> Ticket:
    assert isinstance(result, Ticket), f"cần Ticket, nhận {result!r}"
    return result


def refused(result, reason: Refusal) -> None:
    assert not isinstance(result, Ticket), f"cần từ chối {reason}, nhận vé {result!r}"
    assert result == reason, f"cần từ chối {reason}, nhận {result!r}"


def refusal_counts(gate: AdmissionGate) -> dict[str, int]:
    counts = dict(gate.refusals)
    return {str(r): counts.get(r, 0) for r in Refusal}


def admit_and_finish(gate: AdmissionGate, ip: str, now: float) -> Ticket:
    t = ticket(gate.admit(ip, now))
    gate.finish(t, now)
    return t


def snapshot(gate: AdmissionGate) -> tuple:
    return (gate.opening_count, gate.anonymous_count, gate.tracked_ips, refusal_counts(gate))


# --- Hằng và kiểu --------------------------------------------------------------------------------


def test_constants_match_spec():
    assert (admission.MAX_OPENING_PER_IP, admission.MAX_OPENING_TOTAL) == (4, 64)
    assert (admission.MAX_ANONYMOUS_PER_IP, admission.MAX_ANONYMOUS_TOTAL) == (1, 8)
    assert (admission.HANDSHAKE_RATE_PER_IP, admission.HANDSHAKE_BURST_PER_IP) == (1, 2)
    assert (admission.HANDSHAKE_RATE_TOTAL, admission.HANDSHAKE_BURST_TOTAL) == (4, 4)
    assert (admission.PRIORITY_OPENING_PER_IP, admission.PRIORITY_OPENING_TOTAL) == (4, 16)
    assert (admission.PRIORITY_RATE_PER_IP, admission.PRIORITY_BURST_PER_IP) == (2, 4)
    assert (admission.PRIORITY_RATE_TOTAL, admission.PRIORITY_BURST_TOTAL) == (4, 4)
    assert admission.PRIORITY_TTL == 24 * HOUR
    assert admission.MAX_TRACKED_IPS == 4096
    assert admission.MAX_PRIORITY_IPS == 256


def test_limits_moved_out_of_net_connection():
    """Các hằng 4/64/1/8 chuyển từ `net.connection` sang `core.admission` (một nguồn duy nhất)."""
    from teleport.net import connection

    for name in ("MAX_OPENING_PER_IP", "MAX_OPENING_TOTAL", "MAX_ANONYMOUS_PER_IP", "MAX_ANONYMOUS_TOTAL"):
        assert not hasattr(connection, name), f"net.connection vẫn còn {name}: patch vào đó không có tác dụng"


def test_pool_and_refusal_members():
    assert {p.name for p in Pool} == {"GENERAL", "PRIORITY"}
    assert {r.name for r in Refusal} == {"OPENING_PER_IP", "OPENING_TOTAL", "RATE_PER_IP", "RATE_TOTAL"}
    assert all(isinstance(p, str) for p in Pool)
    assert all(isinstance(r, str) for r in Refusal)


def test_ticket_is_frozen_and_carries_ip_and_pool():
    gate = AdmissionGate()
    t = ticket(gate.admit(A, 0.0))
    assert t.ip == A
    assert t.pool is Pool.GENERAL
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.ip = B  # type: ignore[misc]


def test_ticket_ids_are_unique():
    gate = AdmissionGate()
    ids = [admit_and_finish(gate, ip, i * 1.0).id for i, ip in enumerate([A, A, B, C, A, D])]
    assert len(set(ids)) == len(ids)


def test_fresh_gate_is_empty():
    gate = AdmissionGate()
    assert gate.opening_count == 0
    assert gate.anonymous_count == 0
    assert gate.tracked_ips == 0
    assert refusal_counts(gate) == {str(r): 0 for r in Refusal}
    assert gate.is_priority(A, 0.0) is False


def test_ipv4_mapped_ipv6_is_the_same_ip():
    """Địa chỉ IPv4-mapped IPv6 chuẩn hóa về IPv4 trước khi tra bảng: không né được bucket theo IP."""
    gate = AdmissionGate()
    t = admit_and_finish(gate, "::ffff:" + A, 0.0)
    assert t.ip == A
    admit_and_finish(gate, A, 0.0)
    refused(gate.admit("::ffff:" + A, 0.0), Refusal.RATE_PER_IP)
    opened(gate, "::ffff:" + B, 0.0, fp=FP[B])
    assert gate.is_priority(B, 0.0) is True


@pytest.mark.parametrize("ip", ["", "localhost", "1.2.3", "999.1.1.1", None, 1, b"1.2.3.4"], ids=repr)
def test_admit_rejects_bad_ip_without_state_change(ip):
    gate = AdmissionGate()
    with pytest.raises((ValueError, TypeError)):
        gate.admit(ip, 0.0)
    assert snapshot(gate) == (0, 0, 0, {str(r): 0 for r in Refusal})


# --- Vé và bộ đếm --------------------------------------------------------------------------------


def test_admit_counts_opening_and_finish_releases():
    gate = AdmissionGate()
    t1 = ticket(gate.admit(A, 0.0))
    t2 = ticket(gate.admit(B, 0.0))
    assert gate.opening_count == 2
    gate.finish(t1, 0.0)
    assert gate.opening_count == 1
    gate.finish(t2, 0.0)
    assert gate.opening_count == 0


def test_finish_twice_is_noop():
    gate = AdmissionGate()
    t1 = ticket(gate.admit(A, 0.0))
    t2 = ticket(gate.admit(A, 0.0))
    gate.finish(t1, 0.0)
    gate.finish(t1, 0.0)
    gate.finish(t1, 0.0)
    assert gate.opening_count == 1, "finish lặp trên vé đã trả không được trả chỗ của vé khác"
    gate.finish(t2, 0.0)
    gate.finish(t2, 0.0)
    assert gate.opening_count == 0


def test_finish_releases_per_ip_opening_slot():
    """4 vé sống mỗi IP (pool chung); vé thứ 5 bị từ chối; trả một vé thì nhận lại được."""
    gate = AdmissionGate()
    live = [ticket(gate.admit(A, t)) for t in (0.0, 0.0, 1.0, 2.0)]  # burst 2 rồi 1/s
    refused(gate.admit(A, 3.0), Refusal.OPENING_PER_IP)
    gate.finish(live[0], 3.0)
    ticket(gate.admit(A, 3.0))  # token của giây thứ 3 còn nguyên: lần bị từ chối không trừ


def test_opening_total_limit_general(monkeypatch):
    monkeypatch.setattr(admission, "MAX_OPENING_TOTAL", 3)
    gate = AdmissionGate()
    live = [ticket(gate.admit(ip, 0.0)) for ip in (A, B, C)]
    refused(gate.admit(D, 0.0), Refusal.OPENING_TOTAL)
    gate.finish(live[1], 0.0)
    ticket(gate.admit(D, 0.0))


# --- Token bucket pool chung ----------------------------------------------------------------------


def test_general_per_ip_burst_then_refill():
    gate = AdmissionGate()
    admit_and_finish(gate, A, 0.0)
    admit_and_finish(gate, A, 0.0)
    refused(gate.admit(A, 0.0), Refusal.RATE_PER_IP)
    refused(gate.admit(A, 0.5), Refusal.RATE_PER_IP)  # mới đầy nửa token
    admit_and_finish(gate, A, 1.0)
    refused(gate.admit(A, 1.0), Refusal.RATE_PER_IP)
    admit_and_finish(gate, A, 3.0)
    admit_and_finish(gate, A, 3.0)  # đầy lại tới burst 2, không hơn
    refused(gate.admit(A, 3.0), Refusal.RATE_PER_IP)


def test_general_per_ip_bucket_caps_at_burst_after_long_idle():
    gate = AdmissionGate()
    admit_and_finish(gate, A, 0.0)
    for _ in range(2):
        admit_and_finish(gate, A, 1000.0)
    refused(gate.admit(A, 1000.0), Refusal.RATE_PER_IP)


def test_general_total_burst_across_ips():
    gate = AdmissionGate()
    for ip in (A, B, C, D):
        admit_and_finish(gate, ip, 0.0)
    refused(gate.admit(E, 0.0), Refusal.RATE_TOTAL)
    admit_and_finish(gate, E, 0.25)  # 4/s: một token mỗi 0,25 s
    refused(gate.admit(F, 0.25), Refusal.RATE_TOTAL)


def test_check_order_opening_per_ip_before_rate():
    gate = AdmissionGate()
    for t in (0.0, 0.0, 1.0, 2.0):
        ticket(gate.admit(A, t))
    # Ở t = 2 bucket của A cũng rỗng, nhưng số đang mở đầu được kiểm trước.
    refused(gate.admit(A, 2.0), Refusal.OPENING_PER_IP)


def test_check_order_opening_total_before_rate(monkeypatch):
    monkeypatch.setattr(admission, "MAX_OPENING_TOTAL", 2)
    gate = AdmissionGate()
    ticket(gate.admit(A, 0.0))
    ticket(gate.admit(A, 0.0))  # bucket của A rỗng, tổng đầy
    refused(gate.admit(A, 0.0), Refusal.OPENING_TOTAL)


def test_check_order_rate_per_ip_before_rate_total():
    gate = AdmissionGate()
    admit_and_finish(gate, A, 0.0)
    admit_and_finish(gate, A, 0.0)
    admit_and_finish(gate, B, 0.0)
    admit_and_finish(gate, C, 0.0)  # bucket tổng rỗng, bucket của A cũng rỗng
    refused(gate.admit(A, 0.0), Refusal.RATE_PER_IP)


def test_refused_by_rate_per_ip_does_not_consume_total():
    gate = AdmissionGate()
    admit_and_finish(gate, A, 0.0)
    admit_and_finish(gate, A, 0.0)
    for _ in range(20):
        refused(gate.admit(A, 0.0), Refusal.RATE_PER_IP)
    admit_and_finish(gate, B, 0.0)
    admit_and_finish(gate, C, 0.0)  # bucket tổng còn đúng 2 token sau 2 lần nhận của A


def test_refused_by_rate_total_does_not_consume_per_ip():
    gate = AdmissionGate()
    admit_and_finish(gate, A, 0.0)  # A còn 1 token
    admit_and_finish(gate, B, 0.0)
    admit_and_finish(gate, B, 0.0)
    admit_and_finish(gate, C, 0.0)  # tổng rỗng
    for _ in range(20):
        refused(gate.admit(A, 0.0), Refusal.RATE_TOTAL)
    # t = 0,25: tổng +1; A còn 1 + 0,25 nếu các lần bị từ chối không trừ gì của A.
    admit_and_finish(gate, A, 0.25)


def test_refused_by_opening_total_does_not_consume_tokens(monkeypatch):
    monkeypatch.setattr(admission, "MAX_OPENING_TOTAL", 1)
    gate = AdmissionGate()
    holder = ticket(gate.admit(A, 0.0))
    for _ in range(20):
        refused(gate.admit(B, 0.0), Refusal.OPENING_TOTAL)
    gate.finish(holder, 0.0)
    admit_and_finish(gate, B, 0.0)
    admit_and_finish(gate, B, 0.0)  # bucket của B còn nguyên burst 2
    admit_and_finish(gate, C, 0.0)  # bucket tổng: A + B + B + C = 4


def test_refusal_counters_count_each_reason_once(monkeypatch):
    monkeypatch.setattr(admission, "MAX_OPENING_TOTAL", 5)
    gate = AdmissionGate()
    live = [ticket(gate.admit(A, t)) for t in (0.0, 0.0, 1.0, 2.0)]  # bucket tổng ở t = 2: còn 3
    refused(gate.admit(A, 2.0), Refusal.OPENING_PER_IP)
    assert refusal_counts(gate) == {**{str(r): 0 for r in Refusal}, str(Refusal.OPENING_PER_IP): 1}
    ticket(gate.admit(B, 2.0))  # 5 vé sống; tổng còn 2
    refused(gate.admit(C, 2.0), Refusal.OPENING_TOTAL)
    for t in live:
        gate.finish(t, 2.0)
    admit_and_finish(gate, C, 2.0)
    admit_and_finish(gate, C, 2.0)  # bucket C rỗng, bucket tổng rỗng
    refused(gate.admit(C, 2.0), Refusal.RATE_PER_IP)
    refused(gate.admit(D, 2.0), Refusal.RATE_TOTAL)
    assert refusal_counts(gate) == {str(r): 1 for r in Refusal}


# --- Hoàn token (`trusted`) -----------------------------------------------------------------------


def test_trusted_refunds_per_ip_and_total():
    gate = AdmissionGate()
    for _ in range(6):  # A bắt tay tin cậy liên tiếp: không bao giờ bị giới hạn tốc độ
        t = ticket(gate.admit(A, 0.0))
        gate.trusted(t, 0.0)
        gate.finish(t, 0.0)
    admit_and_finish(gate, B, 0.0)
    admit_and_finish(gate, B, 0.0)
    admit_and_finish(gate, C, 0.0)
    admit_and_finish(gate, D, 0.0)  # tổng: 4 lần không hoàn
    refused(gate.admit(E, 0.0), Refusal.RATE_TOTAL)


def test_trusted_twice_refunds_once():
    gate = AdmissionGate()
    t1 = ticket(gate.admit(A, 0.0))
    ticket(gate.admit(A, 0.0))  # bucket A rỗng
    gate.trusted(t1, 0.0)
    gate.trusted(t1, 0.0)
    gate.trusted(t1, 0.0)
    ticket(gate.admit(A, 0.0))  # đúng một token được hoàn
    refused(gate.admit(A, 0.0), Refusal.RATE_PER_IP)


def test_refund_never_exceeds_per_ip_capacity():
    gate = AdmissionGate()
    t1 = ticket(gate.admit(A, 0.0))
    # t = 10: bucket A đã đầy lại (2); hoàn token của t1 không được đẩy lên 3.
    gate.trusted(t1, 10.0)
    gate.finish(t1, 10.0)
    admit_and_finish(gate, A, 10.0)
    admit_and_finish(gate, A, 10.0)
    refused(gate.admit(A, 10.0), Refusal.RATE_PER_IP)


def test_refund_never_exceeds_total_capacity():
    gate = AdmissionGate()
    t1 = ticket(gate.admit(A, 0.0))
    gate.trusted(t1, 10.0)  # bucket tổng đã đầy lại (4)
    gate.finish(t1, 10.0)
    for ip in (B, C, D, E):
        admit_and_finish(gate, ip, 10.0)
    refused(gate.admit(F, 10.0), Refusal.RATE_TOTAL)


def test_trusted_does_not_release_opening_slot():
    """`trusted` chỉ hoàn token; vé vẫn sống tới `finish`."""
    gate = AdmissionGate()
    t = ticket(gate.admit(A, 0.0))
    gate.trusted(t, 0.0)
    assert gate.opening_count == 1
    gate.finish(t, 0.0)
    assert gate.opening_count == 0


# --- Ẩn danh ------------------------------------------------------------------------------------


def test_anonymous_limit_per_ip_and_release_on_finish():
    gate = AdmissionGate()
    t1 = ticket(gate.admit(A, 0.0))
    t2 = ticket(gate.admit(A, 0.0))
    assert gate.anonymous(t1, 0.0) is True
    assert gate.anonymous_count == 1
    assert gate.anonymous(t2, 0.0) is False
    assert gate.anonymous_count == 1
    assert gate.opening_count == 2, "vé bị từ chối ẩn danh vẫn sống tới finish"
    gate.finish(t2, 0.0)
    assert (gate.opening_count, gate.anonymous_count) == (1, 1)
    gate.finish(t1, 0.0)
    assert (gate.opening_count, gate.anonymous_count) == (0, 0)
    t3 = ticket(gate.admit(A, 1.0))
    assert gate.anonymous(t3, 1.0) is True


def test_anonymous_total_limit():
    gate = AdmissionGate()
    tickets = [ticket(gate.admit(STRANGERS[i], i * 0.25)) for i in range(9)]
    for t in tickets[:8]:
        assert gate.anonymous(t, 2.0) is True
    assert gate.anonymous(tickets[8], 3.0) is False
    assert gate.anonymous_count == 8
    gate.finish(tickets[0], 3.0)
    assert gate.anonymous_count == 7
    t = ticket(gate.admit(STRANGERS[9], 3.0))
    assert gate.anonymous(t, 3.0) is True


def test_anonymous_limit_is_shared_by_both_pools(monkeypatch):
    monkeypatch.setattr(admission, "MAX_ANONYMOUS_TOTAL", 1)
    gate = AdmissionGate()
    opened(gate, B, 0.0)
    general = ticket(gate.admit(A, 0.0))
    priority = ticket(gate.admit(B, 0.0))
    assert (general.pool, priority.pool) == (Pool.GENERAL, Pool.PRIORITY)
    assert gate.anonymous(general, 0.0) is True
    assert gate.anonymous(priority, 0.0) is False
    gate.finish(general, 0.0)
    other = ticket(gate.admit(B, 0.0))
    assert gate.anonymous(other, 0.0) is True


def test_anonymous_per_ip_limit_applies_in_priority_pool():
    gate = AdmissionGate()
    opened(gate, B, 0.0)
    t1 = ticket(gate.admit(B, 0.0))
    t2 = ticket(gate.admit(B, 0.0))
    assert gate.anonymous(t1, 0.0) is True
    assert gate.anonymous(t2, 0.0) is False


# --- Pool ưu tiên --------------------------------------------------------------------------------


def test_control_connection_makes_ip_priority():
    gate = AdmissionGate()
    assert gate.is_priority(A, 0.0) is False
    assert ticket(gate.admit(A, 0.0)).pool is Pool.GENERAL
    opened(gate, A, 0.0)
    assert gate.is_priority(A, 0.0) is True
    assert ticket(gate.admit(A, 0.0)).pool is Pool.PRIORITY
    assert gate.is_priority(B, 0.0) is False
    assert ticket(gate.admit(B, 0.0)).pool is Pool.GENERAL


def test_control_opened_returns_distinct_handles():
    gate = AdmissionGate()
    handles = [opened(gate, A, 0.0), opened(gate, A, 0.0), opened(gate, B, 0.0)]
    assert len(set(handles)) == 3
    assert all(isinstance(h, int) for h in handles)


def test_priority_ip_does_not_fall_back_to_general_when_full():
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    live = [ticket(gate.admit(A, 0.0)) for _ in range(4)]
    assert {t.pool for t in live} == {Pool.PRIORITY}
    refused(gate.admit(A, 0.0), Refusal.OPENING_PER_IP)
    # Pool chung vẫn trống: nếu rơi về pool chung thì lần trên đã được nhận.
    ticket(gate.admit(B, 0.0))


def test_priority_ip_admitted_while_general_pool_is_exhausted(monkeypatch):
    monkeypatch.setattr(admission, "MAX_OPENING_TOTAL", 4)
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    for ip in (B, C, D, E):  # pool chung: đủ 4 vé sống và bucket tổng rỗng
        ticket(gate.admit(ip, 0.0))
    refused(gate.admit(F, 0.0), Refusal.OPENING_TOTAL)
    refused(gate.admit(F, 1.0), Refusal.OPENING_TOTAL)
    t = ticket(gate.admit(A, 1.0))
    assert t.pool is Pool.PRIORITY


# Các test tốc độ của pool ưu tiên giữ vé **sống** (không `finish`): vé ưu tiên kết thúc mà chưa
# `trusted()` tốn ngân sách lỗi và có thể làm IP bị hạ ưu tiên, còn `trusted()` lại hoàn token, nên
# cả hai cách đều không đo được tốc độ thô. Số vé sống mỗi IP được nới để bước bị chặn là bucket,
# không phải số đang mở.


def admit_live(gate: AdmissionGate, ip: str, now: float) -> Ticket:
    return ticket(gate.admit(ip, now))


def test_priority_ip_admitted_while_general_rate_is_exhausted():
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    for ip in (B, C, D, E):
        admit_and_finish(gate, ip, 0.0)
    refused(gate.admit(F, 0.0), Refusal.RATE_TOTAL)
    for _ in range(4):
        assert admit_live(gate, A, 0.0).pool is Pool.PRIORITY


def test_priority_per_ip_rate(monkeypatch):
    monkeypatch.setattr(admission, "PRIORITY_OPENING_PER_IP", 100)
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    for _ in range(4):
        admit_live(gate, A, 0.0)
    refused(gate.admit(A, 0.0), Refusal.RATE_PER_IP)
    assert admit_live(gate, A, 0.5).pool is Pool.PRIORITY  # 2/s
    refused(gate.admit(A, 0.5), Refusal.RATE_PER_IP)


def test_priority_total_rate(monkeypatch):
    monkeypatch.setattr(admission, "PRIORITY_OPENING_PER_IP", 100)
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    opened(gate, B, 0.0)
    for _ in range(3):
        admit_live(gate, A, 0.0)
    admit_live(gate, B, 0.0)
    refused(gate.admit(B, 0.0), Refusal.RATE_TOTAL)
    assert admit_live(gate, B, 0.25).pool is Pool.PRIORITY  # 4/s
    # Pool chung không bị ảnh hưởng.
    for ip in (C, D, E, F):
        assert admit_and_finish(gate, ip, 0.25).pool is Pool.GENERAL


def test_priority_opening_total_limit(monkeypatch):
    monkeypatch.setattr(admission, "PRIORITY_OPENING_TOTAL", 2)
    gate = AdmissionGate()
    for ip in (A, B, C):
        opened(gate, ip, 0.0)
    live = [ticket(gate.admit(A, 0.0)), ticket(gate.admit(B, 0.0))]
    refused(gate.admit(C, 0.0), Refusal.OPENING_TOTAL)
    gate.finish(live[0], 0.0)
    assert ticket(gate.admit(C, 0.0)).pool is Pool.PRIORITY


def test_priority_refund_restores_priority_buckets():
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    for _ in range(12):  # máy tin cậy mở nhiều kết nối liên tiếp: không bị chặn bởi tốc độ
        t = ticket(gate.admit(A, 0.0))
        gate.trusted(t, 0.0)
        gate.finish(t, 0.0)


def test_priority_kept_while_connection_alive_beyond_ttl():
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    assert gate.is_priority(A, 10 * 24 * HOUR) is True
    assert ticket(gate.admit(A, 10 * 24 * HOUR)).pool is Pool.PRIORITY


def test_priority_ttl_counts_from_last_close():
    gate = AdmissionGate()
    h1 = opened(gate, A, 0.0)
    h2 = opened(gate, A, 0.0)
    gate.control_closed(h1, 100.0)
    assert gate.is_priority(A, 100.0 + 2 * admission.PRIORITY_TTL) is True, "h2 còn sống"
    t_close = 100.0 + 2 * admission.PRIORITY_TTL
    gate.control_closed(h2, t_close)
    assert gate.is_priority(A, t_close + admission.PRIORITY_TTL - 1.0) is True
    assert ticket(gate.admit(A, t_close + admission.PRIORITY_TTL - 1.0)).pool is Pool.PRIORITY


def test_priority_expires_back_to_general_pool():
    gate = AdmissionGate()
    h = opened(gate, A, 0.0)
    gate.control_closed(h, 5.0)
    later = 5.0 + admission.PRIORITY_TTL + 1.0
    assert gate.is_priority(A, later) is False
    assert ticket(gate.admit(A, later)).pool is Pool.GENERAL


def test_priority_ttl_is_patchable(monkeypatch):
    monkeypatch.setattr(admission, "PRIORITY_TTL", 2.0)
    gate = AdmissionGate()
    h = opened(gate, A, 0.0)
    gate.control_closed(h, 1.0)
    assert gate.is_priority(A, 2.5) is True
    assert gate.is_priority(A, 3.5) is False


def test_control_closed_twice_and_unknown_handle_are_noops():
    gate = AdmissionGate()
    h1 = opened(gate, A, 0.0)
    h2 = opened(gate, A, 0.0)
    gate.control_closed(h1, 1.0)
    gate.control_closed(h1, 2.0)  # lặp: không được tính là đóng h2
    gate.control_closed(10_000_000, 2.0)  # handle không có
    later = 2.0 + 2 * admission.PRIORITY_TTL
    assert gate.is_priority(A, later) is True, "h2 vẫn sống"
    gate.control_closed(h2, later)
    assert gate.is_priority(A, later + admission.PRIORITY_TTL + 1.0) is False


def test_retain_trusted_drops_priority_even_with_live_connection():
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    opened(gate, B, 0.0)
    retain(gate, {FP[B]})
    assert gate.is_priority(A, 0.0) is False
    assert ticket(gate.admit(A, 0.0)).pool is Pool.GENERAL
    assert gate.is_priority(B, 0.0) is True


def test_retain_trusted_drops_priority_kept_by_ttl():
    gate = AdmissionGate()
    h = opened(gate, A, 0.0)
    gate.control_closed(h, 1.0)
    assert gate.is_priority(A, 2.0) is True
    retain(gate, set())
    assert gate.is_priority(A, 2.0) is False


def test_closing_handle_after_retain_dropped_it_is_noop():
    gate = AdmissionGate()
    h = opened(gate, A, 0.0)
    retain(gate, set())
    gate.control_closed(h, 1.0)
    assert gate.is_priority(A, 1.0) is False


def test_repaired_device_regains_priority_with_new_connection():
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    retain(gate, set())
    assert gate.is_priority(A, 1.0) is False
    opened(gate, A, 1.0)
    assert gate.is_priority(A, 1.0) is True


def test_retain_keeps_ip_shared_by_two_devices_if_one_is_still_trusted():
    """Một IP giữ nhiều thiết bị (NAT): hủy ghép đôi một thiết bị không lấy ưu tiên của thiết bị kia."""
    gate = AdmissionGate()
    opened(gate, A, 0.0, fp=FP[A])
    opened(gate, A, 0.0, fp=FP[B])
    retain(gate, {FP[B]})
    assert gate.is_priority(A, 0.0) is True


def test_max_priority_ips_full_of_live_connections_grants_no_new_priority(monkeypatch):
    monkeypatch.setattr(admission, "MAX_PRIORITY_IPS", 2)
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    opened(gate, B, 0.0)
    opened(gate, C, 0.0)
    assert gate.is_priority(A, 0.0) is True
    assert gate.is_priority(B, 0.0) is True
    assert gate.is_priority(C, 0.0) is False
    assert ticket(gate.admit(C, 0.0)).pool is Pool.GENERAL


def test_max_priority_ips_never_drops_ip_with_live_connection(monkeypatch):
    monkeypatch.setattr(admission, "MAX_PRIORITY_IPS", 2)
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    h = opened(gate, B, 0.0)
    gate.control_closed(h, 1.0)  # B chỉ còn ưu tiên theo TTL
    for i, ip in enumerate((C, D, E, F)):
        opened(gate, ip, 2.0 + i)
        assert gate.is_priority(A, 2.0 + i) is True, "A còn kết nối sống: không được bị bỏ"


# --- Trần bảng theo IP ---------------------------------------------------------------------------


def test_max_tracked_ips_evicts_least_recently_used_entry_without_live_ticket(monkeypatch):
    monkeypatch.setattr(admission, "MAX_TRACKED_IPS", 3)
    monkeypatch.setattr(admission, "HANDSHAKE_RATE_TOTAL", 1000.0)
    monkeypatch.setattr(admission, "HANDSHAKE_BURST_TOTAL", 1000.0)
    gate = AdmissionGate()
    admit_and_finish(gate, A, 0.0)
    admit_and_finish(gate, B, 0.0)
    admit_and_finish(gate, C, 0.0)
    admit_and_finish(gate, A, 0.0)  # A dùng gần nhất, bucket A rỗng; B dùng lâu nhất
    admit_and_finish(gate, D, 0.0)
    assert gate.tracked_ips <= 3
    # A còn trong bảng (bucket rỗng): bị từ chối. B đã bị bỏ: vào lại với bucket mới đầy (2 lần).
    refused(gate.admit(A, 0.0), Refusal.RATE_PER_IP)
    admit_and_finish(gate, B, 0.0)
    admit_and_finish(gate, B, 0.0)
    assert gate.tracked_ips <= 3


def test_max_tracked_ips_never_evicts_entry_with_live_ticket(monkeypatch):
    monkeypatch.setattr(admission, "MAX_TRACKED_IPS", 3)
    monkeypatch.setattr(admission, "HANDSHAKE_RATE_TOTAL", 1000.0)
    monkeypatch.setattr(admission, "HANDSHAKE_BURST_TOTAL", 1000.0)
    gate = AdmissionGate()
    live = [ticket(gate.admit(A, t)) for t in (0.0, 0.0, 1.0, 2.0)]
    ticket(gate.admit(B, 2.0))
    ticket(gate.admit(C, 2.0))
    # Bảng đầy, mục nào cũng có vé sống: IP mới bị từ chối như OPENING_TOTAL.
    refused(gate.admit(D, 2.0), Refusal.OPENING_TOTAL)
    assert gate.tracked_ips <= 3
    # A vẫn bị giới hạn 4 vé sống: mục của A không bị bỏ rồi tạo lại.
    refused(gate.admit(A, 3.0), Refusal.OPENING_PER_IP)
    assert gate.opening_count == 6
    for t in live:
        gate.finish(t, 3.0)
    assert gate.opening_count == 2


def test_tracked_ips_never_exceeds_cap_under_flood_of_ips(monkeypatch):
    monkeypatch.setattr(admission, "MAX_TRACKED_IPS", 16)
    gate = AdmissionGate()
    now = 0.0
    for ip in STRANGERS:
        result = gate.admit(ip, now)
        if isinstance(result, Ticket):
            gate.finish(result, now)
        assert gate.tracked_ips <= 16
        now += 0.05
    assert gate.opening_count == 0


def test_refilled_entries_without_tickets_are_dropped():
    """“Bucket đầy lại và không có vé thì bỏ”: IP đã đi từ lâu không còn nằm trong bảng."""
    gate = AdmissionGate()
    for i, ip in enumerate(STRANGERS[:100]):
        admit_and_finish(gate, ip, float(i))
    for i, ip in enumerate(STRANGERS[100:110]):
        admit_and_finish(gate, ip, 10_000.0 + i)
    assert gate.tracked_ips <= 20, f"bảng giữ {gate.tracked_ips} IP đã đầy bucket và không có vé"


# --- `now` sai -------------------------------------------------------------------------------------

BAD_NOW = [float("nan"), float("inf"), float("-inf")]


def _prepared_gate() -> tuple[AdmissionGate, dict]:
    """Gate ở `now` = 10 với đủ loại trạng thái: vé sống, vé ẩn danh, kết nối điều khiển."""
    gate = AdmissionGate()
    live = ticket(gate.admit(A, 10.0))
    anon = ticket(gate.admit(B, 10.0))
    assert gate.anonymous(anon, 10.0) is True
    handle = opened(gate, C, 10.0)
    spare = ticket(gate.admit(D, 10.0))
    return gate, {"live": live, "anon": anon, "handle": handle, "spare": spare}


def _follow_up(gate: AdmissionGate, objs: dict) -> list:
    """Kịch bản cố định chạy sau lời gọi sai; hai gate cùng trạng thái phải cho cùng kết quả."""
    out: list = []
    for ip in (A, A, B, C, C, C, E, E, E):
        r = gate.admit(ip, 10.0)
        out.append(("admit", ip, r.pool if isinstance(r, Ticket) else r))
    out.append(("anon", gate.anonymous(objs["spare"], 10.0)))
    out.append(("prio", gate.is_priority(C, 10.0), gate.is_priority(A, 10.0)))
    out.append(snapshot(gate))
    return out


def _bad_calls(objs: dict, now: float) -> dict:
    return {
        "admit": lambda g: g.admit(E, now),
        "anonymous": lambda g: g.anonymous(objs["spare"], now),
        "trusted": lambda g: g.trusted(objs["live"], now),
        "control_opened": lambda g: opened(g, E, now),
        "control_closed": lambda g: g.control_closed(objs["handle"], now),
        "is_priority": lambda g: g.is_priority(C, now),
    }


METHODS = ["admit", "anonymous", "trusted", "control_opened", "control_closed", "is_priority"]


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("bad_now", [*BAD_NOW, 9.75], ids=["nan", "inf", "-inf", "backwards"])
def test_bad_now_raises_value_error_and_changes_nothing(method, bad_now):
    gate, objs = _prepared_gate()
    twin, twin_objs = _prepared_gate()
    before = snapshot(gate)
    with pytest.raises(ValueError):
        _bad_calls(objs, bad_now)[method](gate)
    assert snapshot(gate) == before
    assert _follow_up(gate, objs) == _follow_up(twin, twin_objs)


@pytest.mark.parametrize("method", METHODS)
def test_equal_now_is_accepted(method):
    gate, objs = _prepared_gate()
    _bad_calls(objs, 10.0)[method](gate)


def test_time_is_shared_across_methods():
    """`now` không giảm tính chung cho mọi phương thức, không riêng từng hàm."""
    gate = AdmissionGate()
    t = ticket(gate.admit(A, 0.0))
    opened(gate, B, 50.0)
    with pytest.raises(ValueError):
        gate.admit(A, 49.0)
    with pytest.raises(ValueError):
        gate.trusted(t, 49.0)
    gate.finish(t, 50.0)


# --- Hypothesis: chuỗi thao tác tùy ý --------------------------------------------------------------

H_IPS = (A, B, C, D)
H_FPS = tuple(FP[ip] for ip in H_IPS)
H_TTL = 5.0
H_LIMITS = {
    "MAX_OPENING_TOTAL": 6,
    "PRIORITY_OPENING_TOTAL": 5,
    "MAX_ANONYMOUS_TOTAL": 2,
    "PRIORITY_TTL": H_TTL,
    "PRIORITY_STRIKES": 10**9,  # coi như vô hạn: test này không kiểm hạ ưu tiên
}

op_strategy = st.one_of(
    st.tuples(st.just("tick"), st.sampled_from([0.0, 0.25, 0.5, 1.0, 2.0, 4.0])),
    st.tuples(st.just("admit"), st.integers(0, len(H_IPS) - 1)),
    st.tuples(st.just("admit"), st.integers(0, len(H_IPS) - 1)),
    st.tuples(st.just("finish"), st.integers(0, 63)),
    st.tuples(st.just("trusted"), st.integers(0, 63)),
    st.tuples(st.just("anonymous"), st.integers(0, 63)),
    st.tuples(st.just("open"), st.integers(0, len(H_IPS) - 1)),
    st.tuples(st.just("close"), st.integers(0, 63)),
    st.tuples(st.just("retain"), st.frozensets(st.sampled_from(H_FPS))),
)


def _limits(pool: Pool) -> dict:
    if pool is Pool.PRIORITY:
        return {
            "per_ip": admission.PRIORITY_OPENING_PER_IP,
            "total": admission.PRIORITY_OPENING_TOTAL,
            "rate_ip": admission.PRIORITY_RATE_PER_IP,
            "burst_ip": admission.PRIORITY_BURST_PER_IP,
            "rate_total": admission.PRIORITY_RATE_TOTAL,
            "burst_total": admission.PRIORITY_BURST_TOTAL,
        }
    return {
        "per_ip": admission.MAX_OPENING_PER_IP,
        "total": admission.MAX_OPENING_TOTAL,
        "rate_ip": admission.HANDSHAKE_RATE_PER_IP,
        "burst_ip": admission.HANDSHAKE_BURST_PER_IP,
        "rate_total": admission.HANDSHAKE_RATE_TOTAL,
        "burst_total": admission.HANDSHAKE_BURST_TOTAL,
    }


class _Model:
    """Mô hình ưu tiên theo đặc tả (mỗi IP một thiết bị) và nhật ký vé để kiểm bất biến.

    Chạy với ngân sách lỗi coi như vô hạn (`H_LIMITS["PRIORITY_STRIKES"]`): không IP nào bị hạ, nên
    các bất biến ưu tiên giữ nguyên dạng gốc. Ngân sách lỗi và hạ ưu tiên được
    kiểm ở `tests/core/test_admission_demotion.py`.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.tickets: list[Ticket] = []
        self.live: set[int] = set()  # chỉ số vé còn sống
        self.anon_true: set[int] = set()
        self.marked: set[int] = set()  # vé đã gọi anonymous hoặc trusted
        self.refunded: set[int] = set()
        self.handles: list[tuple[int, str]] = []  # (handle, ip)
        self.handle_live: set[int] = set()  # chỉ số handle còn sống trong gate
        self.last_close: dict[str, float] = {}
        self.events: list[tuple[float, str, Pool, int]] = []  # (t, ip, pool, +1 nhận / −1 hoàn)

    def priority(self, ip: str) -> bool | None:
        """True/False khi đặc tả quyết định rõ; None ở đúng biên TTL."""
        if any(self.handles[i][1] == ip for i in self.handle_live):
            return True
        closed = self.last_close.get(ip)
        if closed is None:
            return False
        if self.now == closed + H_TTL:
            return None
        return self.now < closed + H_TTL


def _replay(log: list) -> AdmissionGate:
    gate = AdmissionGate()
    tickets: list[Ticket] = []
    handles: list[int] = []
    for op in log:
        kind = op[0]
        if kind == "admit":
            tickets.append(ticket(gate.admit(op[1], op[2])))
        elif kind == "finish":
            gate.finish(tickets[op[1]], op[2])
        elif kind == "trusted":
            gate.trusted(tickets[op[1]], op[2])
        elif kind == "anonymous":
            gate.anonymous(tickets[op[1]], op[2])
        elif kind == "open":
            handles.append(opened(gate, op[1], op[2]))
        elif kind == "close":
            gate.control_closed(handles[op[1]], op[2])
        elif kind == "retain":
            retain(gate, op[1])
    return gate


def _check_invariants(gate: AdmissionGate, m: _Model) -> None:
    assert gate.opening_count == len(m.live) >= 0
    assert gate.anonymous_count == len(m.anon_true & m.live) >= 0
    anon_ips = Counter(m.tickets[i].ip for i in m.anon_true & m.live)
    assert all(n <= admission.MAX_ANONYMOUS_PER_IP for n in anon_ips.values())
    assert sum(anon_ips.values()) <= admission.MAX_ANONYMOUS_TOTAL
    for pool in Pool:
        lim = _limits(pool)
        live = [m.tickets[i] for i in m.live if m.tickets[i].pool is pool]
        assert len(live) <= lim["total"], f"{pool}: {len(live)} vé sống > {lim['total']}"
        per_ip = Counter(t.ip for t in live)
        assert all(n <= lim["per_ip"] for n in per_ip.values()), f"{pool}: {per_ip}"
    assert gate.tracked_ips <= admission.MAX_TRACKED_IPS
    assert gate.demoted_ips == 0, "ngân sách lỗi vô hạn: không IP nào được bị hạ"
    for ip in H_IPS:
        expected = m.priority(ip)
        if expected is not None:
            assert gate.is_priority(ip, m.now) is expected, f"{ip} ưu tiên phải là {expected}"


def _check_rate_windows(m: _Model) -> None:
    """Mọi cửa sổ [t1, t2]: số lần nhận − số lần hoàn ≤ burst + rate·(t2 − t1), theo IP và tổng."""
    for pool in Pool:
        lim = _limits(pool)
        evs = [e for e in m.events if e[2] is pool]
        groups: dict[str | None, list] = {None: evs}
        for ip in H_IPS:
            groups[ip] = [e for e in evs if e[1] == ip]
        for key, group in groups.items():
            rate, burst = (
                (lim["rate_total"], lim["burst_total"]) if key is None else (lim["rate_ip"], lim["burst_ip"])
            )
            times = sorted({e[0] for e in group})
            for i, t1 in enumerate(times):
                for t2 in times[i:]:
                    net = sum(e[3] for e in group if t1 <= e[0] <= t2)
                    bound = burst + rate * (t2 - t1)
                    assert net <= bound + 1e-9, f"{pool} {key or 'tổng'} [{t1}, {t2}]: {net} > {bound}"


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops=st.lists(op_strategy, max_size=60))
def test_random_operation_sequences_keep_every_invariant(ops):
    with pytest.MonkeyPatch.context() as mp:
        for name, value in H_LIMITS.items():
            mp.setattr(admission, name, value)
        gate = AdmissionGate()
        m = _Model()
        twin_log: list = []  # mọi thao tác trừ các lần admit bị từ chối
        seen_ids: set = set()
        for op in ops:
            kind = op[0]
            if kind == "tick":
                m.now += op[1]
            elif kind == "admit":
                ip = H_IPS[op[1]]
                expected_pool = m.priority(ip)
                before = refusal_counts(gate)
                result = gate.admit(ip, m.now)
                if isinstance(result, Ticket):
                    assert result.ip == ip
                    assert result.id not in seen_ids
                    seen_ids.add(result.id)
                    if expected_pool is not None:
                        assert (result.pool is Pool.PRIORITY) is expected_pool
                    assert refusal_counts(gate) == before
                    m.live.add(len(m.tickets))
                    m.tickets.append(result)
                    m.events.append((m.now, ip, result.pool, +1))
                    twin_log.append(("admit", ip, m.now))
                else:
                    assert result in set(Refusal)
                    after = refusal_counts(gate)
                    assert after[str(result)] == before[str(result)] + 1
                    assert sum(after.values()) == sum(before.values()) + 1
                    # Bị từ chối không đổi gì: gate chưa từng thấy lần này cũng từ chối đúng như vậy.
                    replica = _replay(twin_log)
                    assert replica.admit(ip, m.now) == result
            elif kind == "finish" and m.tickets:
                i = op[1] % len(m.tickets)
                gate.finish(m.tickets[i], m.now)
                m.live.discard(i)
                twin_log.append(("finish", i, m.now))
            elif kind == "trusted" and m.live:
                live = sorted(m.live - (m.marked - m.refunded))
                if live:
                    i = live[op[1] % len(live)]
                    gate.trusted(m.tickets[i], m.now)
                    m.marked.add(i)
                    if i not in m.refunded:
                        m.refunded.add(i)
                        t = m.tickets[i]
                        m.events.append((m.now, t.ip, t.pool, -1))
                    twin_log.append(("trusted", i, m.now))
            elif kind == "anonymous" and m.live:
                fresh = sorted(m.live - m.marked)
                if fresh:
                    i = fresh[op[1] % len(fresh)]
                    m.marked.add(i)
                    if gate.anonymous(m.tickets[i], m.now):
                        m.anon_true.add(i)
                    twin_log.append(("anonymous", i, m.now))
            elif kind == "open":
                ip = H_IPS[op[1]]
                h = opened(gate, ip, m.now)
                assert h >= 1
                m.handle_live.add(len(m.handles))
                m.handles.append((h, ip))
                twin_log.append(("open", ip, m.now))
            elif kind == "close" and m.handles:
                i = op[1] % len(m.handles)
                gate.control_closed(m.handles[i][0], m.now)
                if i in m.handle_live:
                    m.handle_live.discard(i)
                    ip = m.handles[i][1]
                    if not any(m.handles[j][1] == ip for j in m.handle_live):
                        m.last_close[ip] = m.now
                twin_log.append(("close", i, m.now))
            elif kind == "retain":
                retain(gate, op[1])
                for i in list(m.handle_live):
                    if FP[m.handles[i][1]] not in op[1]:
                        m.handle_live.discard(i)
                for ip in H_IPS:
                    if FP[ip] not in op[1]:
                        m.last_close.pop(ip, None)
                twin_log.append(("retain", op[1]))
            _check_invariants(gate, m)
        _check_rate_windows(m)
        # Trả mọi vé: bộ đếm về 0.
        for t in m.tickets:
            gate.finish(t, m.now)
        assert (gate.opening_count, gate.anonymous_count) == (0, 0)


@settings(max_examples=200, deadline=None)
@given(
    times=st.lists(st.sampled_from([0.0, 0.25, 0.5, 1.0, 3.0]), min_size=1, max_size=40),
    ips=st.lists(st.sampled_from(STRANGERS[:6]), min_size=40, max_size=40),
    trusted_mask=st.lists(st.booleans(), min_size=40, max_size=40),
)
def test_rate_bound_holds_for_many_ips_with_refunds(times, ips, trusted_mask):
    """Pool chung, nhiều IP: số lần nhận − số lần hoàn ≤ burst + rate·T cho bucket tổng."""
    gate = AdmissionGate()
    now = 0.0
    events: list[tuple[float, int]] = []
    for dt, ip, is_trusted in zip(times, ips, trusted_mask, strict=False):
        now += dt
        result = gate.admit(ip, now)
        if isinstance(result, Ticket):
            events.append((now, +1))
            if is_trusted:
                gate.trusted(result, now)
                events.append((now, -1))
            gate.finish(result, now)
    stamps = sorted({e[0] for e in events})
    for i, t1 in enumerate(stamps):
        for t2 in stamps[i:]:
            net = sum(d for t, d in events if t1 <= t <= t2)
            bound = admission.HANDSHAKE_BURST_TOTAL + admission.HANDSHAKE_RATE_TOTAL * (t2 - t1)
            assert net <= bound + 1e-9
    assert gate.opening_count == 0
    assert math.isfinite(now)
