"""Test ngân sách lỗi và hạ ưu tiên của `teleport.core.admission` (`docs/protocol.md` mục 2.3, gạch
“Ngân sách lỗi và hạ ưu tiên”).

Viết từ đặc tả, không từ code:
- Mỗi mục ưu tiên có ngân sách lỗi, tạo đầy `PRIORITY_STRIKES` (= 4), hồi tuyến tính 1 lần mỗi
  `STRIKE_REFILL` (= 600 s) theo `now`, không vượt trần. Vé `PRIORITY` kết thúc mà **chưa** `trusted()`:
  ngân sách ≥ 1 → trừ 1, không hạ; < 1 → hạ. Mục ưu tiên đã bị xóa (retain, hết TTL) lúc vé
  kết thúc → không làm gì. Chẩn đoán: `priority_strikes(ip, now) -> float | None`.
- `trusted()` **không** cộng ngân sách; `control_closed(handle, now)` cộng 1 (không
  vượt trần) khi kết nối điều khiển (vào hoặc ra) đóng sau khi sống ≥ `STRIKE_CREDIT_AFTER` (= 60 s);
  handle đã bị xóa (retain, hạ) hoặc IP đang bị hạ thì không cộng; `control_opened` không cộng.
- Lý do có ngân sách: `_open_any` hủy các lần thử thua giữa bắt tay, nên máy tin cậy có nhiều địa chỉ để lại
  vài vé “không tin cậy” mỗi lần nối; không được bị hạ oan.
- Các test luật hạ cơ bản chạy với `PRIORITY_STRIKES = 0` (fixture `no_strike_budget`): lần kết
  thúc không tin cậy đầu tiên hạ ngay.
- Vé pool `PRIORITY` kết thúc (`finish(ticket, now)`) mà **chưa** được `trusted()` (TCP trần, bắt tay
  hỏng, hết giờ, ẩn danh, bị hủy), hết ngân sách lỗi → IP đó bị hạ ưu tiên tới `now + PRIORITY_TTL`.
- Hạ ưu tiên thắng handle sống, mốc đóng và cả `control_opened` mới trong thời gian bị hạ; trong
  thời gian đó kết nối mới từ IP ấy vào pool chung, `is_priority` → False.
- Vé `GENERAL` không bị ảnh hưởng; vé đã `trusted()` thì không hạ.
- Bảng hạ riêng, trần `MAX_DEMOTED_IPS` (= 1024, đọc lúc dùng): đầy thì bỏ mục hết hạn sớm nhất; mục
  hết hạn thì bỏ; `retain_trusted`, `finish` hay việc dọn bảng ưu tiên không xóa được.
- `finish(ticket, now)` kiểm `now` như các hàm khác (vé lạ / đã finish: không làm gì nhưng `now` vẫn
  phải hợp lệ).
- Thuộc tính đếm số IP bị hạ: `demoted_ips` (tên dự kiến; đọc qua `demoted_count` để chỉ sửa một chỗ).

Module chạy với mặc định thật (`real_admission_limits`). Thời gian dùng bội số của 0,25 s.
"""

from __future__ import annotations

from collections import Counter

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.core import admission
from teleport.core.admission import AdmissionGate, Pool, Refusal, Ticket

pytestmark = pytest.mark.real_admission_limits

A, B, C, D, E, F = (f"192.168.1.{i}" for i in range(10, 16))
STRANGERS = [f"10.1.{i // 250}.{i % 250 + 1}" for i in range(64)]
FP = {ip: f"{i:02x}" * 32 for i, ip in enumerate((A, B, C, D, E, F))}
OTHER_FP = "ee" * 32

RELAXED_RATES = {
    "HANDSHAKE_RATE_PER_IP": 1e6,
    "HANDSHAKE_BURST_PER_IP": 1e6,
    "HANDSHAKE_RATE_TOTAL": 1e6,
    "HANDSHAKE_BURST_TOTAL": 1e6,
    "PRIORITY_RATE_PER_IP": 1e6,
    "PRIORITY_BURST_PER_IP": 1e6,
    "PRIORITY_RATE_TOTAL": 1e6,
    "PRIORITY_BURST_TOTAL": 1e6,
}


# --- Hàm phụ trợ ---------------------------------------------------------------------------------


@pytest.fixture
def no_strike_budget(monkeypatch):
    """`PRIORITY_STRIKES = 0`: lần kết thúc không tin cậy đầu tiên hạ ngay."""
    monkeypatch.setattr(admission, "PRIORITY_STRIKES", 0)


zero_strikes = pytest.mark.usefixtures("no_strike_budget")


def ttl() -> float:
    return admission.PRIORITY_TTL


def strikes(gate: AdmissionGate, ip: str, now: float) -> float | None:
    """Ngân sách lỗi còn lại của IP (chẩn đoán)."""
    return gate.priority_strikes(ip, now)


def fail_once(gate: AdmissionGate, ip: str, now: float) -> None:
    """Một vé ưu tiên kết thúc mà không bắt tay tin cậy (TCP trần, lần thử thua bị hủy…)."""
    gate.finish(priority_ticket(gate, ip, now), now)


def trusted_once(gate: AdmissionGate, ip: str, now: float) -> None:
    t = priority_ticket(gate, ip, now)
    gate.trusted(t, now)
    gate.finish(t, now)


def demoted_count(gate: AdmissionGate) -> int:
    """Số IP đang bị hạ ưu tiên (thuộc tính đếm của đặc tả; tên dự kiến `demoted_ips`)."""
    return gate.demoted_ips


def opened(gate: AdmissionGate, ip: str, now: float, fp: str | None = None) -> int:
    """Kết nối điều khiển đã xác thực mTLS tới/từ `ip`."""
    return gate.control_opened(ip, FP.get(ip, OTHER_FP) if fp is None else fp, now)


def ticket(result) -> Ticket:
    assert isinstance(result, Ticket), f"cần Ticket, nhận {result!r}"
    return result


def priority_ticket(gate: AdmissionGate, ip: str, now: float) -> Ticket:
    t = ticket(gate.admit(ip, now))
    assert t.pool is Pool.PRIORITY, f"{ip} phải được vé ưu tiên (phép thử vô nghĩa nếu không)"
    return t


def general_ticket(gate: AdmissionGate, ip: str, now: float) -> Ticket:
    t = ticket(gate.admit(ip, now))
    assert t.pool is Pool.GENERAL, f"{ip} phải vào pool chung, nhận {t.pool}"
    return t


def demote(gate: AdmissionGate, ip: str, now: float) -> None:
    """IP có kết nối điều khiển sống, rồi một vé ưu tiên kết thúc mà không bắt tay tin cậy."""
    opened(gate, ip, now)
    gate.finish(priority_ticket(gate, ip, now), now)


def assert_demoted(gate: AdmissionGate, ip: str, now: float) -> None:
    """Bị hạ: không ưu tiên, kể cả ngay sau một `control_opened` mới; vé mới vào pool chung."""
    assert gate.is_priority(ip, now) is False, f"{ip} vẫn ưu tiên lúc {now}"
    opened(gate, ip, now)
    assert gate.is_priority(ip, now) is False, f"control_opened mới lúc {now} đã lấy lại ưu tiên cho {ip}"
    t = ticket(gate.admit(ip, now))
    assert t.pool is Pool.GENERAL, f"{ip} bị hạ nhưng vé lúc {now} vẫn ở pool {t.pool}"
    gate.trusted(t, now)
    gate.finish(t, now)


def snapshot(gate: AdmissionGate) -> tuple:
    return (gate.opening_count, gate.anonymous_count, gate.tracked_ips, demoted_count(gate))


# --- Hằng và bộ đếm --------------------------------------------------------------------------------


def test_max_demoted_ips_constant_matches_spec():
    assert admission.MAX_DEMOTED_IPS == 1024


def test_strike_constants_match_spec():
    assert admission.PRIORITY_STRIKES == 4
    assert admission.STRIKE_REFILL == 600.0


def test_fresh_gate_has_no_demoted_ips():
    assert demoted_count(AdmissionGate()) == 0


# --- Ngân sách lỗi -------------------------------------------------------------------------------


def test_priority_strikes_is_none_for_ip_without_priority_entry():
    gate = AdmissionGate()
    assert strikes(gate, A, 0.0) is None
    gate.finish(general_ticket(gate, A, 0.0), 0.0)
    assert strikes(gate, A, 0.0) is None, "vé pool chung không tạo mục ưu tiên"


def test_new_priority_entry_starts_with_full_strike_budget():
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    assert strikes(gate, A, 0.0) == 4.0


def test_priority_ip_is_forgiven_strikes_then_demoted_on_next_untrusted_ending(monkeypatch):
    """Tha đúng `PRIORITY_STRIKES` lần kết thúc không tin cậy (vẫn ưu tiên, ngân sách giảm dần); lần kế
    tiếp trong cửa sổ thì hạ."""
    for name, value in RELAXED_RATES.items():
        monkeypatch.setattr(admission, name, value)
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    for k in range(4):
        fail_once(gate, A, 0.0)
        assert gate.is_priority(A, 0.0) is True, f"lần lỗi thứ {k + 1} phải được tha"
        assert strikes(gate, A, 0.0) == 3.0 - k
    assert demoted_count(gate) == 0
    fail_once(gate, A, 0.0)
    assert demoted_count(gate) == 1
    assert_demoted(gate, A, 0.0)


@pytest.mark.parametrize("how", ["bare_tcp", "anonymous", "timeout"])
def test_every_kind_of_untrusted_ending_costs_one_strike(how):
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    t = priority_ticket(gate, A, 0.0)
    end = 0.0
    if how == "anonymous":
        assert gate.anonymous(t, 0.0) is True
    elif how == "timeout":
        end = 6.0  # 6/600 = 0,01: ngân sách đã đầy, hồi thêm không vượt trần
    gate.finish(t, end)
    assert strikes(gate, A, end) == 3.0
    assert gate.is_priority(A, end) is True


def test_strike_budget_refills_linearly_and_caps():
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    for _ in range(4):
        fail_once(gate, A, 0.0)
    assert strikes(gate, A, 0.0) == 0.0
    assert strikes(gate, A, 150.0) == 0.25
    assert strikes(gate, A, 600.0) == 1.0
    assert strikes(gate, A, 1500.0) == 2.5
    assert strikes(gate, A, 2400.0) == 4.0
    assert strikes(gate, A, 100_000.0) == 4.0, "không vượt trần PRIORITY_STRIKES"


@pytest.mark.parametrize(("later", "demoted"), [(599.0, True), (600.0, False)])
def test_refilled_strike_is_spent_before_demoting(later, demoted):
    """Hết ngân sách lúc 0; sau `STRIKE_REFILL` giây hồi đủ 1 lần nên lần lỗi tiếp được tha; sớm hơn
    thì hạ."""
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    for _ in range(4):
        fail_once(gate, A, 0.0)
    fail_once(gate, A, later)
    assert gate.is_priority(A, later) is (not demoted)
    if not demoted:
        assert strikes(gate, A, later) == 0.0


def test_trusted_does_not_add_strikes():
    """`trusted()` chỉ hoàn token, không cộng ngân sách lỗi (nếu cộng, kẻ relay cắt kết nối để máy
    tin cậy nối lại qua nó sau 1 s và tự nạp lại được)."""
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    fail_once(gate, A, 0.0)
    fail_once(gate, A, 0.0)
    for i in range(1, 6):
        trusted_once(gate, A, float(i))  # mỗi giây một lần: token đã hồi
    assert strikes(gate, A, 5.0) == pytest.approx(2.0 + 5.0 / admission.STRIKE_REFILL)


def test_control_opened_does_not_add_strikes():
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    fail_once(gate, A, 0.0)
    fail_once(gate, A, 0.0)
    for _ in range(5):
        opened(gate, A, 0.0)
    assert strikes(gate, A, 0.0) == 2.0


def test_strike_budget_is_per_ip():
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    opened(gate, B, 0.0)
    fail_once(gate, A, 0.0)
    fail_once(gate, A, 0.0)
    assert strikes(gate, A, 0.0) == 2.0
    assert strikes(gate, B, 0.0) == 4.0


def test_strike_constants_are_read_at_use_time(monkeypatch):
    gate = AdmissionGate()
    monkeypatch.setattr(admission, "PRIORITY_STRIKES", 2)
    monkeypatch.setattr(admission, "STRIKE_REFILL", 10.0)
    opened(gate, A, 0.0)
    assert strikes(gate, A, 0.0) == 2.0
    fail_once(gate, A, 0.0)
    assert strikes(gate, A, 5.0) == 1.5


# --- Cộng lại khi kết nối điều khiển sống lâu đóng ----------------------------------------------


@pytest.fixture
def no_refill(monkeypatch):
    """Hồi theo thời gian gần như bằng 0: tách riêng phần cộng lại khi đóng kết nối."""
    monkeypatch.setattr(admission, "STRIKE_REFILL", 1e12)


def test_strike_credit_after_constant_matches_spec():
    assert admission.STRIKE_CREDIT_AFTER == 60.0


@pytest.mark.usefixtures("no_refill")
@pytest.mark.parametrize(("lived", "credited"), [(59.75, False), (60.0, True), (3600.0, True)])
def test_closing_control_connection_credits_one_strike_only_after_living_long_enough(lived, credited):
    gate = AdmissionGate()
    h = opened(gate, A, 0.0)
    opened(gate, A, 0.0)  # một kết nối khác còn sống: mục ưu tiên vẫn còn sau khi đóng `h`
    fail_once(gate, A, 0.0)
    fail_once(gate, A, 0.0)
    gate.control_closed(h, lived)
    assert strikes(gate, A, lived) == pytest.approx(3.0 if credited else 2.0, abs=1e-6)


@pytest.mark.usefixtures("no_refill")
def test_closing_long_lived_connection_never_exceeds_cap():
    gate = AdmissionGate()
    handles = [opened(gate, A, 0.0) for _ in range(3)]
    fail_once(gate, A, 0.0)
    for h in handles:
        gate.control_closed(h, 100.0)
    assert strikes(gate, A, 100.0) == pytest.approx(4.0, abs=1e-6)


@pytest.mark.usefixtures("no_refill")
def test_credit_counts_from_each_handles_own_open_time():
    gate = AdmissionGate()
    early = opened(gate, A, 0.0)
    late = opened(gate, A, 50.0)
    opened(gate, A, 50.0)  # giữ mục ưu tiên sống
    fail_once(gate, A, 50.0)
    fail_once(gate, A, 50.0)
    fail_once(gate, A, 50.0)
    gate.control_closed(late, 70.0)  # sống 20 s: không cộng
    assert strikes(gate, A, 70.0) == pytest.approx(1.0, abs=1e-6)
    gate.control_closed(early, 70.0)  # sống 70 s: cộng 1
    assert strikes(gate, A, 70.0) == pytest.approx(2.0, abs=1e-6)


@pytest.mark.usefixtures("no_refill")
def test_closing_same_handle_twice_credits_once():
    gate = AdmissionGate()
    h = opened(gate, A, 0.0)
    opened(gate, A, 0.0)
    for _ in range(3):
        fail_once(gate, A, 0.0)
    gate.control_closed(h, 100.0)
    gate.control_closed(h, 200.0)
    assert strikes(gate, A, 200.0) == pytest.approx(2.0, abs=1e-6)


@pytest.mark.usefixtures("no_refill")
def test_handle_removed_by_retain_trusted_gives_no_credit():
    """Hai thiết bị sau một IP (NAT): hủy ghép đôi thiết bị X xóa handle của X; đóng handle đó sau đó
    không cộng (mục ưu tiên của IP vẫn còn nhờ thiết bị Y)."""
    x_fp, y_fp = "aa" * 32, "bb" * 32
    gate = AdmissionGate()
    hx = opened(gate, A, 0.0, fp=x_fp)
    opened(gate, A, 0.0, fp=y_fp)
    fail_once(gate, A, 0.0)
    fail_once(gate, A, 0.0)
    gate.retain_trusted(frozenset({y_fp}))
    assert gate.is_priority(A, 100.0) is True
    gate.control_closed(hx, 100.0)
    assert strikes(gate, A, 100.0) == pytest.approx(2.0, abs=1e-6)


@pytest.mark.usefixtures("no_refill")
@zero_strikes
def test_closing_connection_of_demoted_ip_gives_no_credit_and_does_not_undo_demotion():
    gate = AdmissionGate()
    h = opened(gate, A, 0.0)
    fail_once(gate, A, 0.0)  # ngân sách 0: hạ
    before = strikes(gate, A, 100.0)
    gate.control_closed(h, 100.0)
    after = strikes(gate, A, 100.0)
    if before is None:
        assert after is None
    else:
        assert after == pytest.approx(before, abs=1e-6), "IP đang bị hạ không được cộng"
    assert_demoted(gate, A, 100.0)


def test_strike_credit_after_is_read_at_use_time(monkeypatch):
    monkeypatch.setattr(admission, "STRIKE_REFILL", 1e12)
    gate = AdmissionGate()
    monkeypatch.setattr(admission, "STRIKE_CREDIT_AFTER", 10.0)
    opened(gate, A, 0.0)  # giữ mục ưu tiên sống
    h1 = opened(gate, A, 0.0)
    h2 = opened(gate, A, 0.0)
    for _ in range(3):
        fail_once(gate, A, 0.0)
    gate.control_closed(h1, 9.75)
    assert strikes(gate, A, 9.75) == pytest.approx(1.0, abs=1e-6)
    gate.control_closed(h2, 10.0)
    assert strikes(gate, A, 10.0) == pytest.approx(2.0, abs=1e-6)


# --- Máy tin cậy nhiều địa chỉ (lần thử thua bị `_open_any` hủy giữa bắt tay) ---------------------


def connect_multi(gate: AdmissionGate, ip: str, now: float, addresses: int) -> int:
    """Một lần `open_control` tới máy có `addresses` địa chỉ, cùng IP nguồn: `addresses` vé cùng lúc; lần
    thắng bắt tay tin cậy và được giao (`control_opened`), các lần thua bị hủy giữa bắt tay. Trả handle."""
    winner, *losers = (ticket(gate.admit(ip, now)) for _ in range(addresses))
    gate.trusted(winner, now)  # server bắt tay xong lần thắng trước khi client hủy các lần thua
    for t in losers:
        gate.finish(t, now)
    gate.finish(winner, now)
    return opened(gate, ip, now)


def test_three_address_device_survives_three_back_to_back_connections_and_fourth_demotes(monkeypatch):
    """Máy 3 địa chỉ, IP chưa ưu tiên. Lần nối 1 làm IP thành ưu tiên (các lần thua của nó
    còn ở pool chung, không tốn gì); lần 2 và 3 mỗi lần tốn 2 (4 → 2 → 0): vẫn ưu tiên. Lần nối thứ 4
    liền sau đó (kết nối trước sống < 60 s, không cộng lại) làm IP bị hạ."""
    for name, value in RELAXED_RATES.items():
        monkeypatch.setattr(admission, name, value)
    gate = AdmissionGate()
    previous = None
    for n in range(1, 4):
        now = float(n)
        if previous is not None:
            gate.control_closed(previous, now)  # sống 1 s
        previous = connect_multi(gate, A, now, addresses=3)
        assert gate.is_priority(A, now) is True, f"lần nối thứ {n} làm IP bị hạ oan"
    assert strikes(gate, A, 3.0) == pytest.approx(0.0, abs=0.01)
    gate.control_closed(previous, 4.0)
    connect_multi(gate, A, 4.0, addresses=3)
    assert gate.is_priority(A, 4.0) is False, "lần nối thứ 4 liền nhau phải làm IP bị hạ (protocol mục 2.3)"


@pytest.mark.parametrize("lifetime", [60.0, 600.0, 3600.0])
def test_two_address_device_with_long_lived_connections_is_never_demoted(lifetime):
    """Máy 2 địa chỉ (mỗi lần nối tốn 1), kết nối sống ≥ 60 s rồi đứt và nối lại ngay, 100 lần:
    lần đóng cộng lại 1 nên ngân sách cân bằng, không bao giờ bị hạ (tốc độ mặc định thật)."""
    gate = AdmissionGate()
    h = connect_multi(gate, A, 0.0, addresses=2)
    for n in range(1, 101):
        now = n * lifetime
        gate.control_closed(h, now)
        h = connect_multi(gate, A, now, addresses=2)
        assert gate.is_priority(A, now) is True, f"lần nối thứ {n + 1} làm IP bị hạ oan"
    assert demoted_count(gate) == 0


def test_three_address_device_reconnecting_every_strike_refill_is_never_demoted(monkeypatch):
    """Máy 3 địa chỉ nối lại mỗi `STRIKE_REFILL` giây (tốn 2; hồi 1 + kết nối cũ sống 600 s cộng 1), 50 lần.
    Tốc độ nới: lần nối đầu (IP chưa ưu tiên) cần 3 vé pool chung cùng lúc, quá burst 2 theo IP."""
    for name, value in RELAXED_RATES.items():
        monkeypatch.setattr(admission, name, value)
    gate = AdmissionGate()
    h = connect_multi(gate, A, 0.0, addresses=3)
    for n in range(1, 51):
        now = n * admission.STRIKE_REFILL
        gate.control_closed(h, now)
        h = connect_multi(gate, A, now, addresses=3)
        assert gate.is_priority(A, now) is True, f"lần nối thứ {n + 1} làm IP bị hạ oan"
    assert demoted_count(gate) == 0


# --- Kẻ relay ép máy tin cậy nối lại -------------------------------------------------------------


@pytest.mark.parametrize("session", [1.0, 60.0])
def test_relay_forcing_reconnects_is_demoted_after_bounded_strikes(session):
    """Kẻ ở IP Y relay cho máy tin cậy T: cắt relay sau `session` giây, T nối lại qua Y sau 1 s (mỗi lần T
    bắt tay tin cậy), lặp lại; giữa các lượt Y bắn TCP trần hết mức token cho phép (mặc định thật). Y bị hạ
    sau tối đa `PRIORITY_STRIKES + 1` lần lỗi cộng số phiên của T sống ≥ 60 s (cộng phần hồi theo thời
    gian); nối lại nhanh không nạp lại được gì."""
    gate = AdmissionGate()
    Y = E
    now = 0.0
    failures = 0
    long_sessions = 0
    for _ in range(200):
        h = opened(gate, Y, now, fp=FP[A])  # T nối qua relay: kết nối điều khiển tin cậy, nguồn là Y
        trusted_once(gate, Y, now)
        for _ in range(10):  # Y bắn TCP trần
            result = gate.admit(Y, now + 0.5)
            if isinstance(result, Ticket):
                if result.pool is Pool.PRIORITY:
                    failures += 1
                gate.finish(result, now + 0.5)
        now += session
        if gate.is_priority(Y, now) and session >= admission.STRIKE_CREDIT_AFTER:
            long_sessions += 1
        gate.control_closed(h, now)  # Y cắt relay
        if not gate.is_priority(Y, now):
            break
        now += 1.0  # T nối lại sau 1 s
    else:
        pytest.fail("Y không bao giờ bị hạ")
    refill = int(now / admission.STRIKE_REFILL)
    bound = admission.PRIORITY_STRIKES + 1 + long_sessions + refill
    assert failures <= bound, (failures, long_sessions, refill)
    if session < admission.STRIKE_CREDIT_AFTER:
        assert failures == admission.PRIORITY_STRIKES + 1, "nối lại nhanh không được nạp lại ngân sách"


@zero_strikes
def test_demoted_ips_counts_ips_not_tickets():
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    t1 = priority_ticket(gate, A, 0.0)
    t2 = priority_ticket(gate, A, 0.0)
    gate.finish(t1, 0.0)
    gate.finish(t2, 0.0)
    assert demoted_count(gate) == 1
    demote(gate, B, 0.0)
    assert demoted_count(gate) == 2


def test_general_tickets_never_count_as_demoted():
    gate = AdmissionGate()
    for i, ip in enumerate(STRANGERS[:8]):
        gate.finish(general_ticket(gate, ip, i * 1.0), i * 1.0)
    assert demoted_count(gate) == 0


# --- Khi nào bị hạ --------------------------------------------------------------------------------


@zero_strikes
@pytest.mark.parametrize("how", ["bare_tcp", "anonymous", "anonymous_over_cap", "timeout"])
def test_priority_ticket_ending_without_trusted_handshake_demotes_ip(how):
    """TCP trần / ẩn danh (kể cả ẩn danh bị từ chối) / hết giờ từ IP ưu tiên → IP bị hạ, dù kết nối
    điều khiển cấp ưu tiên vẫn còn sống."""
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    t = priority_ticket(gate, A, 0.0)
    end = 0.0
    if how == "anonymous":
        assert gate.anonymous(t, 0.0) is True
    elif how == "anonymous_over_cap":
        holder = priority_ticket(gate, A, 0.0)
        assert gate.anonymous(holder, 0.0) is True
        assert gate.anonymous(t, 0.0) is False  # vượt 1 ẩn danh/IP
    elif how == "timeout":
        end = 10.0  # OPENING_TIMEOUT của net
    assert gate.is_priority(A, end) is True, "trước finish vẫn ưu tiên"
    gate.finish(t, end)
    assert demoted_count(gate) == 1
    assert_demoted(gate, A, end)


@zero_strikes
def test_trusted_ticket_finishing_does_not_demote():
    """Vé đã `trusted()` (bắt tay xong với chứng chỉ tin cậy) rồi mới hỏng / hết giờ: không hạ."""
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    for i in range(10):
        now = float(i)
        t = priority_ticket(gate, A, now)
        gate.trusted(t, now)
        gate.finish(t, now + 0.5 if i % 2 else now)
    assert gate.is_priority(A, 10.0) is True
    assert priority_ticket(gate, A, 10.0).pool is Pool.PRIORITY
    assert demoted_count(gate) == 0


@zero_strikes
def test_general_ticket_ending_untrusted_does_not_touch_priority():
    """Vé GENERAL (cấp trước khi IP có ưu tiên) kết thúc không tin cậy: không hạ."""
    gate = AdmissionGate()
    early = general_ticket(gate, A, 0.0)
    opened(gate, A, 0.0)
    gate.finish(early, 0.0)
    assert gate.is_priority(A, 0.0) is True
    assert demoted_count(gate) == 0


def test_stranger_general_ticket_does_not_block_later_priority():
    gate = AdmissionGate()
    gate.finish(general_ticket(gate, B, 0.0), 0.0)
    opened(gate, B, 1.0)
    assert gate.is_priority(B, 1.0) is True


@zero_strikes
def test_demotion_overrides_priority_kept_by_ttl_after_close():
    gate = AdmissionGate()
    h = opened(gate, A, 0.0)
    gate.control_closed(h, 1.0)
    t = priority_ticket(gate, A, 2.0)  # ưu tiên nhờ mốc đóng chưa quá TTL
    gate.finish(t, 2.0)
    assert_demoted(gate, A, 2.0)


@zero_strikes
@pytest.mark.parametrize("how", ["retain_trusted", "ttl_expired"])
def test_ticket_ending_after_priority_entry_is_gone_does_nothing(how):
    """Mục ưu tiên đã bị xóa (hủy ghép đôi, hết TTL) lúc vé PRIORITY kết thúc không tin cậy → không
    còn ưu tiên để hạ, không làm gì: thiết bị ghép đôi lại / kết nối mới được ưu tiên ngay."""
    gate = AdmissionGate()
    h = opened(gate, A, 0.0)
    if how == "retain_trusted":
        t = priority_ticket(gate, A, 0.0)
        gate.retain_trusted(frozenset())
        end = 1.0
    else:
        gate.control_closed(h, 1.0)
        t = priority_ticket(gate, A, 2.0)  # ưu tiên nhờ mốc đóng
        end = 1.0 + ttl() + 10.0  # vé kết thúc khi mốc đóng đã quá TTL
        assert gate.is_priority(A, end) is False
    gate.finish(t, end)
    assert demoted_count(gate) == 0
    opened(gate, A, end)
    assert gate.is_priority(A, end) is True


@zero_strikes
def test_ipv4_mapped_priority_ticket_demotes_the_ipv4_address():
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    t = priority_ticket(gate, "::ffff:" + A, 0.0)
    gate.finish(t, 0.0)
    assert gate.is_priority("::ffff:" + A, 0.0) is False
    assert_demoted(gate, A, 0.0)


# --- Hạ thắng mọi thứ, trong đúng PRIORITY_TTL ----------------------------------------------------


@zero_strikes
def test_new_control_connections_during_demotion_grant_no_priority():
    gate = AdmissionGate()
    demote(gate, A, 10.0)
    for now in (10.0, 11.0, 10.0 + ttl() / 2, 10.0 + ttl() - 1.0):
        assert_demoted(gate, A, now)


@zero_strikes
def test_demotion_ends_after_priority_ttl_and_new_control_connection_restores_priority():
    gate = AdmissionGate()
    demote(gate, A, 10.0)
    later = 10.0 + ttl() + 1.0
    opened(gate, A, later)
    assert gate.is_priority(A, later) is True
    assert priority_ticket(gate, A, later).pool is Pool.PRIORITY


@zero_strikes
def test_after_demotion_expires_ip_without_connection_is_not_priority():
    gate = AdmissionGate()
    h = opened(gate, A, 0.0)
    gate.control_closed(h, 1.0)
    gate.finish(priority_ticket(gate, A, 2.0), 2.0)
    assert gate.is_priority(A, 2.0 + ttl() + 1.0) is False


@zero_strikes
def test_demotion_length_reads_priority_ttl_at_use_time(monkeypatch):
    gate = AdmissionGate()
    monkeypatch.setattr(admission, "PRIORITY_TTL", 10.0)
    demote(gate, A, 0.0)
    assert_demoted(gate, A, 9.0)
    opened(gate, A, 11.0)
    assert gate.is_priority(A, 11.0) is True


@zero_strikes
def test_finishing_same_ticket_again_does_not_extend_demotion():
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    t = priority_ticket(gate, A, 0.0)
    gate.finish(t, 0.0)
    gate.finish(t, 100.0)  # vé đã trả: không làm gì, không gia hạn
    later = ttl() + 1.0  # < 100 + TTL
    opened(gate, A, later)
    assert gate.is_priority(A, later) is True


@zero_strikes
def test_demotion_survives_retain_trusted():
    gate = AdmissionGate()
    demote(gate, A, 0.0)
    gate.retain_trusted(frozenset({FP[A]}))
    assert_demoted(gate, A, 1.0)
    gate.retain_trusted(frozenset())
    assert_demoted(gate, A, 2.0)


@zero_strikes
def test_demotion_survives_trusted_connections_and_finishes_from_same_ip():
    """Máy tin cậy thật nối lại từ IP bị hạ: bắt tay tin cậy (hoàn token) không gỡ hạ ưu tiên."""
    gate = AdmissionGate()
    demote(gate, A, 0.0)
    for i in range(1, 4):
        t = general_ticket(gate, A, float(i))
        gate.trusted(t, float(i))
        gate.finish(t, float(i))
        opened(gate, A, float(i))
    assert_demoted(gate, A, 4.0)


@zero_strikes
def test_demotion_survives_priority_table_pruning(monkeypatch):
    monkeypatch.setattr(admission, "MAX_PRIORITY_IPS", 2)
    gate = AdmissionGate()
    h = opened(gate, A, 0.0)
    t = priority_ticket(gate, A, 0.0)
    gate.control_closed(h, 1.0)  # A chỉ còn mốc đóng: mục ưu tiên của A bỏ được khi bảng đầy
    gate.finish(t, 1.0)
    hb = opened(gate, B, 2.0)
    hc = opened(gate, C, 3.0)  # bảng đầy: mục của A bị dọn
    gate.control_closed(hb, 4.0)
    gate.control_closed(hc, 4.0)  # B, C chỉ còn mốc đóng: bảng có chỗ dọn được cho IP mới
    assert_demoted(gate, A, 10.0)
    opened(gate, F, 10.0)
    assert gate.is_priority(F, 10.0) is True, "bảng còn chỗ: phép thử với A mới có nghĩa"


@zero_strikes
def test_demoting_one_ip_leaves_other_priority_ips_alone():
    gate = AdmissionGate()
    opened(gate, B, 0.0)
    demote(gate, A, 0.0)
    assert gate.is_priority(B, 0.0) is True
    assert priority_ticket(gate, B, 0.0).pool is Pool.PRIORITY


def test_flood_from_relay_ip_uses_at_most_strikes_plus_one_priority_tickets():
    """Kịch bản `priority_starve.py` với mặc định thật: IP A (giành ưu tiên qua relay) lũ TCP trần mỗi
    0,25 s. A chỉ lấy được `PRIORITY_STRIKES + 1` vé ưu tiên (tha 4 lần, lần thứ 5 bị hạ), sau đó mọi
    kết nối của A vào pool chung; từ bước kế tiếp máy tin cậy ở IP ưu tiên B luôn được nhận."""
    gate = AdmissionGate()
    opened(gate, A, 0.0)
    opened(gate, B, 0.0)
    a_priority = 0
    demoted_at: float | None = None
    now = 0.0
    while now <= 10.0:
        for _ in range(10):
            result = gate.admit(A, now)
            if isinstance(result, Ticket):
                if result.pool is Pool.PRIORITY:
                    a_priority += 1
                gate.finish(result, now)
        if demoted_at is None and not gate.is_priority(A, now):
            demoted_at = now
        if demoted_at is not None and now > demoted_at:
            trusted_once(gate, B, now)  # B không bị bỏ đói
        now += 0.25
    assert a_priority == admission.PRIORITY_STRIKES + 1
    assert demoted_at is not None and demoted_at <= 1.0, demoted_at
    assert gate.is_priority(B, now) is True


# --- Trần bảng hạ (MAX_DEMOTED_IPS) ---------------------------------------------------------------


@zero_strikes
def test_demoted_table_never_exceeds_cap_read_at_use_time(monkeypatch):
    for name, value in RELAXED_RATES.items():
        monkeypatch.setattr(admission, name, value)
    gate = AdmissionGate()
    monkeypatch.setattr(admission, "MAX_DEMOTED_IPS", 3)  # đặt sau khi tạo gate: đọc lúc dùng
    for i, ip in enumerate(STRANGERS[:20]):
        demote(gate, ip, i * 0.25)
        assert demoted_count(gate) <= 3
    assert demoted_count(gate) == 3
    for ip in STRANGERS[17:20]:  # ba IP bị hạ gần nhất còn trong bảng
        assert_demoted(gate, ip, 5.0)


@zero_strikes
def test_full_demoted_table_drops_entry_expiring_first(monkeypatch):
    for name, value in RELAXED_RATES.items():
        monkeypatch.setattr(admission, name, value)
    monkeypatch.setattr(admission, "MAX_DEMOTED_IPS", 2)
    gate = AdmissionGate()
    demote(gate, A, 1.0)
    demote(gate, B, 2.0)
    demote(gate, C, 3.0)  # bảng đầy: bỏ A (hết hạn sớm nhất)
    assert demoted_count(gate) == 2
    assert_demoted(gate, B, 4.0)
    assert_demoted(gate, C, 4.0)
    opened(gate, A, 4.0)
    assert gate.is_priority(A, 4.0) is True, "A đã bị bỏ khỏi bảng hạ: control_opened mới phải cấp ưu tiên"


@zero_strikes
def test_expired_demotions_never_crowd_out_new_ones(monkeypatch):
    for name, value in RELAXED_RATES.items():
        monkeypatch.setattr(admission, name, value)
    monkeypatch.setattr(admission, "MAX_DEMOTED_IPS", 2)
    gate = AdmissionGate()
    demote(gate, A, 0.0)
    demote(gate, B, 0.0)
    later = ttl() + 10.0
    demote(gate, C, later)
    demote(gate, D, later + 1.0)
    assert demoted_count(gate) <= 2
    assert_demoted(gate, C, later + 2.0)
    assert_demoted(gate, D, later + 2.0)
    for ip in (A, B):
        opened(gate, ip, later + 2.0)
        assert gate.is_priority(ip, later + 2.0) is True


# --- `finish(ticket, now)`: kiểm `now` -------------------------------------------------------------

BAD_NOW = [float("nan"), float("inf"), float("-inf"), 9.75]
BAD_NOW_IDS = ["nan", "inf", "-inf", "backwards"]


def _gate_with_priority_ticket() -> tuple[AdmissionGate, Ticket]:
    gate = AdmissionGate()
    opened(gate, A, 10.0)
    return gate, priority_ticket(gate, A, 10.0)


@zero_strikes
@pytest.mark.parametrize("bad_now", BAD_NOW, ids=BAD_NOW_IDS)
def test_finish_with_bad_now_raises_value_error_and_changes_nothing(bad_now):
    gate, t = _gate_with_priority_ticket()
    before = snapshot(gate)
    with pytest.raises(ValueError):
        gate.finish(t, bad_now)
    assert snapshot(gate) == before, "vé phải còn sống, IP chưa bị hạ"
    assert gate.is_priority(A, 10.0) is True
    gate.finish(t, 10.0)  # lần gọi hợp lệ vẫn trả vé và hạ IP như thường
    assert gate.opening_count == 0
    assert gate.is_priority(A, 10.0) is False


@pytest.mark.parametrize("bad_now", [None, "10", True, b"10", [10.0]], ids=repr)
def test_finish_with_non_number_now_is_type_error(bad_now):
    gate, t = _gate_with_priority_ticket()
    before = snapshot(gate)
    with pytest.raises(TypeError):
        gate.finish(t, bad_now)
    assert snapshot(gate) == before


@pytest.mark.parametrize("bad", [None, "ticket", 1, (1, A, Pool.GENERAL)], ids=repr)
def test_finish_rejects_non_ticket(bad):
    gate, _ = _gate_with_priority_ticket()
    with pytest.raises(TypeError):
        gate.finish(bad, 10.0)
    assert gate.opening_count == 1


def test_finish_equal_now_is_accepted():
    gate, t = _gate_with_priority_ticket()
    gate.finish(t, 10.0)
    assert gate.opening_count == 0


def test_finish_unknown_or_finished_ticket_is_noop_with_valid_now():
    gate, t = _gate_with_priority_ticket()
    foreign = Ticket(id=10**9, ip=A, pool=Pool.PRIORITY)  # không do gate này cấp (cùng IP, pool ưu tiên)
    gate.finish(foreign, 10.0)
    assert gate.opening_count == 1
    assert gate.is_priority(A, 10.0) is True, "vé lạ không được hạ IP"
    assert strikes(gate, A, 10.0) == admission.PRIORITY_STRIKES, "vé lạ không được trừ ngân sách lỗi"
    gate.trusted(t, 10.0)
    gate.finish(t, 10.0)
    gate.finish(t, 11.0)  # đã finish
    assert gate.opening_count == 0
    assert gate.is_priority(A, 11.0) is True


@pytest.mark.parametrize("bad_now", BAD_NOW, ids=BAD_NOW_IDS)
def test_finish_unknown_or_finished_ticket_still_checks_now(bad_now):
    gate, t = _gate_with_priority_ticket()
    foreign = Ticket(id=10**9, ip=B, pool=Pool.GENERAL)
    with pytest.raises(ValueError):
        gate.finish(foreign, bad_now)
    gate.trusted(t, 10.0)
    gate.finish(t, 10.0)
    with pytest.raises(ValueError):
        gate.finish(t, bad_now)


def test_finish_now_shares_the_gate_clock():
    gate, t = _gate_with_priority_ticket()
    gate.trusted(t, 10.0)
    gate.finish(t, 100.0)
    with pytest.raises(ValueError):
        gate.is_priority(A, 50.0)
    with pytest.raises(ValueError):
        gate.admit(B, 50.0)


# --- Hypothesis: máy trạng thái có thao tác hạ ưu tiên ---------------------------------------------

H_IPS = (A, B, C, D)
H_FPS = tuple(FP[ip] for ip in H_IPS)
H_TTL = 5.0
H_STRIKES = 2
H_REFILL = 2.0  # 0,25 s hồi 0,125 lần: mọi phép cộng đều chính xác
H_CREDIT_AFTER = 1.0
EPS = 1e-9
H_LIMITS = {
    "MAX_OPENING_TOTAL": 6,
    "PRIORITY_OPENING_TOTAL": 5,
    "MAX_ANONYMOUS_TOTAL": 2,
    "PRIORITY_TTL": H_TTL,
    "PRIORITY_STRIKES": H_STRIKES,
    "STRIKE_REFILL": H_REFILL,
    "STRIKE_CREDIT_AFTER": H_CREDIT_AFTER,
}

op_strategy = st.one_of(
    st.tuples(st.just("tick"), st.sampled_from([0.0, 0.25, 0.5, 1.0, 2.0, 4.0, 6.0])),
    st.tuples(st.just("admit"), st.integers(0, len(H_IPS) - 1)),
    st.tuples(st.just("admit"), st.integers(0, len(H_IPS) - 1)),
    st.tuples(st.just("finish"), st.integers(0, 63)),
    st.tuples(st.just("trusted"), st.integers(0, 63)),
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
    """Mô hình ưu tiên + ngân sách lỗi + hạ ưu tiên theo đặc tả (mỗi IP một thiết bị).

    Ngân sách lỗi của mỗi IP được giữ dưới dạng khoảng [dưới, trên] (hồi tuyến tính, trần
    `H_STRIKES`): biết chắc khi mục ưu tiên được tạo mới (đầy) và qua các lần trừ / cộng chắc chắn;
    chưa chắc khi mục có thể đã bị dọn rồi tạo lại (đầy) hoặc sau khi bị hạ.

    Chỗ đặc tả không quyết định thì mô hình trả None (không kiểm): đúng biên TTL; khoảng giữa hai
    hạn khi một IP bị hạ lần nữa lúc đang bị hạ (gia hạn hay giữ hạn cũ đều chấp nhận); lần kết thúc
    không tin cậy khi chưa chắc ngân sách ≥ 1 hay < 1 (có thể hạ); và kết nối điều khiển sống hoặc
    mốc đóng có từ lúc IP (có thể) bị hạ, sau khi hạ hết hạn (“bị bẩn”: gate được xóa hay giữ).
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.tickets: list[Ticket] = []
        self.live: set[int] = set()
        self.anon_true: set[int] = set()
        self.marked: set[int] = set()  # vé đã gọi anonymous hoặc trusted
        self.refunded: set[int] = set()  # vé đã trusted (lần đầu)
        self.handles: list[tuple[int, str]] = []  # (handle, ip)
        self.handle_live: set[int] = set()
        self.opened_at: list[float] = []  # lúc mở của từng handle
        self.tainted: set[int] = set()  # handle sống trong lúc IP bị hạ
        self.clean_close: dict[str, float] = {}
        self.tainted_close: dict[str, float] = {}
        self.demotion: dict[str, tuple[float, float]] = {}  # ip → (hạn chắc chắn, hạn muộn nhất)
        self.budget: dict[str, tuple[float, float, float]] = {}  # ip → (dưới, trên, lúc ghi)
        self.events: list[tuple[float, str, Pool, int]] = []

    def strike_bounds(self, ip: str) -> tuple[float, float]:
        if ip not in self.budget:
            return 0.0, float(H_STRIKES)
        lo, hi, t = self.budget[ip]
        gained = (self.now - t) / H_REFILL
        return min(H_STRIKES, lo + gained), min(H_STRIKES, hi + gained)

    def set_budget(self, ip: str, lo: float, hi: float) -> None:
        self.budget[ip] = (lo, hi, self.now)

    def maybe_demote(self, ip: str) -> None:
        """Có thể bị hạ (hoặc gia hạn) lúc này, có thể không."""
        deadline = self.now + H_TTL
        cur = self.demotion.get(ip)
        self.demotion[ip] = (float("-inf"), deadline) if cur is None else (cur[0], max(cur[1], deadline))
        self._taint(ip)

    def _taint(self, ip: str) -> None:
        for i in self.handle_live:
            if self.handles[i][1] == ip:
                self.tainted.add(i)
        if ip in self.clean_close:
            self.tainted_close[ip] = max(self.tainted_close.get(ip, -1.0), self.clean_close.pop(ip))

    def demotion_state(self, ip: str) -> bool | None:
        d = self.demotion.get(ip)
        if d is None or self.now > d[1]:
            return False
        if self.now < d[0]:
            return True
        return None

    def maybe_demoted(self, ip: str) -> bool:
        d = self.demotion.get(ip)
        return d is not None and self.now <= d[1]

    def demote(self, ip: str) -> None:
        deadline = self.now + H_TTL
        cur = self.demotion.get(ip)
        if cur is None or self.now > cur[0]:
            self.demotion[ip] = (deadline, deadline)
        else:
            self.demotion[ip] = (cur[0], deadline)
        self._taint(ip)

    def priority(self, ip: str) -> bool | None:
        state = self.demotion_state(ip)
        if state is True:
            return False
        if state is None:
            return None
        mine = [i for i in self.handle_live if self.handles[i][1] == ip]
        if any(i not in self.tainted for i in mine):
            return True
        cc = self.clean_close.get(ip)
        if cc is None:
            clean: bool | None = False
        elif self.now == cc + H_TTL:
            clean = None
        else:
            clean = self.now < cc + H_TTL
        if clean is True:
            return True
        tc = self.tainted_close.get(ip)
        if mine or (tc is not None and self.now <= tc + H_TTL):
            return None  # chỉ còn dấu vết “bị bẩn”: đặc tả không quyết định
        return clean

    def surely_demoted(self) -> int:
        return sum(1 for ip in H_IPS if self.demotion_state(ip) is True)


def _model_untrusted_ending(m: _Model, ip: str) -> None:
    """Vé PRIORITY của `ip` kết thúc mà chưa `trusted()`."""
    p = m.priority(ip)
    lo, hi = m.strike_bounds(ip)
    if p is True:
        if lo >= 1 + EPS:
            m.set_budget(ip, lo - 1, hi - 1)  # chắc chắn được tha
        elif hi < 1 - EPS:
            m.demote(ip)  # chắc chắn hết ngân sách
            m.set_budget(ip, 0.0, float(H_STRIKES))
        else:
            m.maybe_demote(ip)
            m.set_budget(ip, 0.0, hi)
    elif p is None:
        m.maybe_demote(ip)
        m.set_budget(ip, 0.0, hi)
    elif m.demotion_state(ip) is True:
        m.maybe_demote(ip)  # đang bị hạ: gia hạn hay không đều được
    # p False, không bị hạ: mục ưu tiên đã mất (retain, hết TTL) → không làm gì


def _model_close_credit(m: _Model, i: int, ip: str) -> None:
    """Kết nối điều khiển sống ≥ `STRIKE_CREDIT_AFTER` rồi đóng → cộng 1 (không vượt trần);
    handle đã bị xóa (retain, hạ) hoặc IP đang bị hạ → không cộng."""
    if m.now - m.opened_at[i] < H_CREDIT_AFTER:
        return
    state = m.demotion_state(ip)
    if state is True:
        return
    lo, hi = m.strike_bounds(ip)
    if i in m.tainted or state is None:
        m.set_budget(ip, lo, min(H_STRIKES, hi + 1))  # handle có thể đã bị xóa khi (có thể) bị hạ
    else:
        m.set_budget(ip, min(H_STRIKES, lo + 1), min(H_STRIKES, hi + 1))


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
            gate.retain_trusted(op[1])
    return gate


def _check_invariants(gate: AdmissionGate, m: _Model) -> None:
    assert gate.opening_count == len(m.live)
    assert gate.anonymous_count == len(m.anon_true & m.live)
    anon_ips = Counter(m.tickets[i].ip for i in m.anon_true & m.live)
    assert all(n <= admission.MAX_ANONYMOUS_PER_IP for n in anon_ips.values())
    assert sum(anon_ips.values()) <= admission.MAX_ANONYMOUS_TOTAL
    for pool in Pool:
        lim = _limits(pool)
        live = [m.tickets[i] for i in m.live if m.tickets[i].pool is pool]
        assert len(live) <= lim["total"]
        assert all(n <= lim["per_ip"] for n in Counter(t.ip for t in live).values())
    assert gate.tracked_ips <= admission.MAX_TRACKED_IPS
    assert m.surely_demoted() <= demoted_count(gate) <= admission.MAX_DEMOTED_IPS
    for ip in H_IPS:
        expected = m.priority(ip)
        if expected is not None:
            assert gate.is_priority(ip, m.now) is expected, f"{ip} lúc {m.now}: ưu tiên phải là {expected}"
        if expected is True:
            lo, hi = m.strike_bounds(ip)
            value = strikes(gate, ip, m.now)
            assert value is not None, f"{ip} ưu tiên mà không có ngân sách lỗi"
            assert lo - EPS <= value <= hi + EPS, f"{ip} lúc {m.now}: ngân sách {value} ngoài [{lo}, {hi}]"


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
                    assert net <= burst + rate * (t2 - t1) + 1e-9, f"{pool} {key or 'tổng'} [{t1}, {t2}]"


@pytest.mark.slow
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops=st.lists(op_strategy, max_size=70))
def test_random_sequences_never_give_priority_to_demoted_ip(ops):
    """Bất biến: ngân sách lỗi trừ 1 mỗi lần vé PRIORITY kết thúc không tin cậy, cộng 1 khi kết nối điều
    khiển sống ≥ `STRIKE_CREDIT_AFTER` đóng (không cộng khi `trusted()`), hồi theo thời gian, không vượt
    trần; hết ngân sách thì hạ; IP bị hạ không bao giờ `is_priority` (và vé của nó không vào pool ưu
    tiên) trước `now + PRIORITY_TTL`, kể cả khi có `control_opened`; vé GENERAL / vé đã trusted không
    hạ; mục ưu tiên đã mất thì không hạ; bảng hạ ≤ `MAX_DEMOTED_IPS`; các bất biến cũ (bộ đếm, giới hạn
    số vé, tốc độ, ẩn danh, retain) giữ nguyên."""
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
                expected = m.priority(ip)
                result = gate.admit(ip, m.now)
                if isinstance(result, Ticket):
                    assert result.ip == ip
                    assert result.id not in seen_ids
                    seen_ids.add(result.id)
                    if expected is not None:
                        assert (result.pool is Pool.PRIORITY) is expected, (
                            f"{ip} lúc {m.now}: pool {result.pool}"
                        )
                    m.live.add(len(m.tickets))
                    m.tickets.append(result)
                    m.events.append((m.now, ip, result.pool, +1))
                    twin_log.append(("admit", ip, m.now))
                else:
                    assert result in set(Refusal)
                    # Bị từ chối không đổi gì: gate chưa từng thấy lần này cũng từ chối như vậy.
                    assert _replay(twin_log).admit(ip, m.now) == result
            elif kind == "finish" and m.tickets:
                i = op[1] % len(m.tickets)
                gate.finish(m.tickets[i], m.now)
                if i in m.live:
                    t = m.tickets[i]
                    if t.pool is Pool.PRIORITY and i not in m.refunded:
                        _model_untrusted_ending(m, t.ip)
                    m.live.discard(i)
                twin_log.append(("finish", i, m.now))
            elif kind == "trusted" and m.live:
                candidates = sorted(m.live - (m.marked - m.refunded))
                if candidates:
                    i = candidates[op[1] % len(candidates)]
                    gate.trusted(m.tickets[i], m.now)
                    m.marked.add(i)
                    if i not in m.refunded:
                        m.refunded.add(i)
                        t = m.tickets[i]
                        m.events.append((m.now, t.ip, t.pool, -1))
                        # trusted() không cộng ngân sách lỗi.
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
                demoted_now = m.maybe_demoted(ip)
                was_priority = m.priority(ip)
                h = opened(gate, ip, m.now)
                assert isinstance(h, int)
                if demoted_now:
                    m.set_budget(ip, 0.0, float(H_STRIKES))
                elif ip not in m.budget:
                    m.set_budget(ip, float(H_STRIKES), float(H_STRIKES))  # mục mới: đầy
                elif was_priority is not True:
                    lo, _ = m.strike_bounds(ip)  # mục cũ còn (giữ ngân sách) hay tạo lại (đầy)
                    m.set_budget(ip, lo, float(H_STRIKES))
                if not demoted_now:
                    assert h >= 1
                idx = len(m.handles)
                m.handles.append((h, ip))
                m.opened_at.append(m.now)
                m.handle_live.add(idx)
                if demoted_now:
                    m.tainted.add(idx)
                twin_log.append(("open", ip, m.now))
            elif kind == "close" and m.handles:
                i = op[1] % len(m.handles)
                gate.control_closed(m.handles[i][0], m.now)
                if i in m.handle_live:
                    ip = m.handles[i][1]
                    _model_close_credit(m, i, ip)
                    m.handle_live.discard(i)
                    if i in m.tainted:
                        m.tainted_close[ip] = m.now
                    else:
                        m.clean_close[ip] = m.now
                twin_log.append(("close", i, m.now))
            elif kind == "retain":
                gate.retain_trusted(op[1])
                for i in list(m.handle_live):
                    if FP[m.handles[i][1]] not in op[1]:
                        m.handle_live.discard(i)
                for ip in H_IPS:
                    if FP[ip] not in op[1]:
                        m.clean_close.pop(ip, None)
                        m.tainted_close.pop(ip, None)
                twin_log.append(("retain", op[1]))
            _check_invariants(gate, m)
        _check_rate_windows(m)
        for t in m.tickets:
            gate.finish(t, m.now)
        assert (gate.opening_count, gate.anonymous_count) == (0, 0)


CAP_IPS = tuple(STRANGERS[:6])
CAP_TTL = 5.1  # không phải bội số của 0,25: hạn không bao giờ trùng đúng một mốc `now`


@pytest.mark.slow
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    cap=st.integers(1, 3),
    ops=st.lists(
        st.one_of(
            st.tuples(st.just("tick"), st.sampled_from([0.25, 1.0, 3.0, 6.0])),
            st.tuples(st.just("demote"), st.integers(0, len(CAP_IPS) - 1)),
            st.tuples(st.just("demote"), st.integers(0, len(CAP_IPS) - 1)),
        ),
        max_size=40,
    ),
)
def test_demoted_table_cap_drops_entry_expiring_first(cap, ops):
    """Bảng hạ ≤ `MAX_DEMOTED_IPS`; đầy thì bỏ mục hết hạn sớm nhất, mục hết hạn thì bỏ: tập IP bị hạ
    luôn là các IP có hạn muộn nhất (chưa hết hạn), tối đa `cap` IP."""
    with pytest.MonkeyPatch.context() as mp:
        limits = {**RELAXED_RATES, "MAX_DEMOTED_IPS": cap, "PRIORITY_TTL": CAP_TTL, "PRIORITY_STRIKES": 0}
        for name, value in limits.items():
            mp.setattr(admission, name, value)
        gate = AdmissionGate()
        now = 0.0
        entries: dict[str, float] = {}  # mô hình bảng hạ: ip → hạn
        for op in ops:
            if op[0] == "tick":
                now += op[1]
            else:
                now += 0.25  # mỗi lần hạ ở một mốc riêng: không có hai hạn bằng nhau
                ip = CAP_IPS[op[1]]
                for other in [k for k, v in entries.items() if v <= now]:
                    del entries[other]
                opened(gate, ip, now)
                t = ticket(gate.admit(ip, now))
                assert (t.pool is Pool.GENERAL) is (ip in entries), f"{ip} lúc {now}: pool {t.pool}"
                gate.finish(t, now)
                if t.pool is Pool.PRIORITY:
                    if len(entries) >= cap:
                        del entries[min(entries, key=entries.__getitem__)]
                    entries[ip] = now + CAP_TTL
            for k in [k for k, v in entries.items() if v <= now]:
                del entries[k]
            assert demoted_count(gate) <= cap
            for ip in CAP_IPS:
                opened(gate, ip, now)
                expected = ip not in entries
                assert gate.is_priority(ip, now) is expected, f"{ip} lúc {now}: ưu tiên phải là {expected}"
