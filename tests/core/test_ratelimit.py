"""Test `teleport.core.ratelimit` (spec P2-4 mục 2.1, 3.11; AC-1).

Viết từ spec, không từ code. `TokenBucket`: bucket đầy trước lần `take` đầu tiên; chỉ `take` dời
mốc thời gian; `available`/`delay` là hàm thuần; `take` được để nợ (token âm). Mọi thời gian là
thời gian ảo, không `sleep`.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.core.ratelimit import CHUNK_SIZE, TokenBucket

KIB = 1024
MIB = 1024 * KIB

HYPO = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def drive(bucket: TokenBucket, ns: list[int], t0: float, *, target: float, max_takes: int = 100_000):
    """Bên gọi đúng hợp đồng: `d = delay(n, t); t += d; take(n, t)` tới khi tổng ≥ `target`."""
    t = t0
    total = 0
    waits: list[tuple[int, float]] = []
    for k in range(max_takes):
        n = ns[k % len(ns)]
        d = bucket.delay(n, t)
        waits.append((n, d))
        t += d
        bucket.take(n, t)
        total += n
        if total >= target:
            break
    return total, t - t0, waits


def test_chunk_size_is_64_kib():
    assert CHUNK_SIZE == 64 * KIB


def test_default_burst_is_one_chunk():
    b = TokenBucket(1000.0)
    assert b.burst == CHUNK_SIZE
    assert b.rate == 1000.0


def test_rate_and_burst_properties_echo_arguments():
    b = TokenBucket(5_000, 2_000)
    assert b.rate == 5_000
    assert b.burst == 2_000


@pytest.mark.slow
@HYPO
@given(
    rate=st.floats(CHUNK_SIZE, 2**30, allow_nan=False, allow_infinity=False),
    ns=st.lists(st.integers(1, CHUNK_SIZE), min_size=1, max_size=50),
    t0=st.floats(0.0, 1e5, allow_nan=False, allow_infinity=False),
)
def test_bucket_keeps_rate_and_never_waits_longer_than_needed(rate, ns, t0):
    burst = CHUNK_SIZE
    b = TokenBucket(rate, burst)
    total, elapsed, waits = drive(b, ns, t0, target=64 * burst)
    for n, d in waits:
        assert 0 <= d <= n / rate * (1 + 1e-9) + 1e-9, (n, d)
    assert abs(total - burst - rate * elapsed) <= burst + 1e-9 * total
    if total >= 64 * burst:
        assert abs((total - burst) / elapsed - rate) <= 0.02 * rate


@pytest.mark.parametrize("rate", [64 * KIB, 1 * MIB, 100 * MIB])
def test_full_chunks_at_fixed_rates_stay_within_two_percent(rate):
    b = TokenBucket(float(rate))
    total, elapsed, waits = drive(b, [CHUNK_SIZE], 0.0, target=math.inf, max_takes=1000)
    assert len(waits) == 1000
    assert abs((total - CHUNK_SIZE) / elapsed - rate) <= 0.02 * rate
    assert max(d for _, d in waits) <= 1.0


@pytest.mark.parametrize("now", [0.0, 3.5, 1e12, -10.0])
def test_bucket_is_full_before_first_take(now):
    b = TokenBucket(100.0, 4_000)
    assert b.available(now) == 4_000
    assert b.delay(4_000, now) == 0


def test_available_and_delay_are_pure():
    b = TokenBucket(1_000.0, 5_000)
    b.take(4_000, 10.0)
    assert b.available(12.0) == b.available(12.0) == 3_000
    assert b.delay(4_000, 12.0) == b.delay(4_000, 12.0) == 1.0
    # `now` lớn rồi nhỏ hơn (vẫn ≥ mốc của `take`): không lỗi, không dời mốc.
    assert b.available(100.0) == 5_000
    assert b.delay(10, 100.0) == 0
    assert b.available(11.0) == 2_000
    assert b.delay(4_000, 10.5) == 2.5
    assert b.available(10.0) == 1_000


def test_take_moves_the_reference_time():
    b = TokenBucket(1_000.0, 5_000)
    b.take(1_000, 10.0)
    b.take(1_000, 12.0)
    # Sau `take(n, t)`: tokens = available(t) − n, mốc = t.
    assert b.available(12.0) == 4_000
    with pytest.raises(ValueError):
        b.available(11.0)


def test_take_beyond_available_goes_into_debt():
    b = TokenBucket(10.0, 100)
    b.take(100, 0.0)
    b.take(50, 0.0)
    assert b.available(0.0) == -50
    assert b.delay(30, 0.0) == pytest.approx((30 - (-50)) / 10.0)
    assert b.available(1.0) == pytest.approx(-40.0)


def test_available_never_exceeds_burst_after_long_idle():
    b = TokenBucket(1_000.0, 2_000)
    b.take(2_000, 0.0)
    assert b.available(1e9) == 2_000


@pytest.mark.parametrize("rate", ["1", None, True, False, [1]])
def test_rejects_rate_of_wrong_type(rate):
    with pytest.raises(TypeError):
        TokenBucket(rate)


@pytest.mark.parametrize("rate", [0, 0.0, -1, -0.5, math.nan, math.inf, -math.inf])
def test_rejects_rate_not_positive_finite(rate):
    with pytest.raises(ValueError):
        TokenBucket(rate)


@pytest.mark.parametrize("burst", ["1", None, True, False])
def test_rejects_burst_of_wrong_type(burst):
    with pytest.raises(TypeError):
        TokenBucket(100.0, burst)


@pytest.mark.parametrize("burst", [0, 0.5, 0.999, -1, math.nan, math.inf])
def test_rejects_burst_below_one_or_not_finite(burst):
    with pytest.raises(ValueError):
        TokenBucket(100.0, burst)


def test_accepts_burst_of_exactly_one():
    b = TokenBucket(100.0, 1)
    assert b.burst == 1
    b.take(1, 0.0)


def _bucket_after_take() -> TokenBucket:
    b = TokenBucket(100.0, 1_000)
    b.take(10, 5.0)
    return b


def _state(b: TokenBucket) -> tuple[float, float, float]:
    return b.available(5.0), b.available(6.0), b.delay(1_000, 5.0)


@pytest.mark.parametrize("n", [0, -1, 1_001, True, False, 1.0, "1", None])
@pytest.mark.parametrize("method", ["delay", "take"])
def test_rejects_n_not_int_in_one_to_burst(method, n):
    b = _bucket_after_take()
    before = _state(b)
    with pytest.raises(ValueError):
        getattr(b, method)(n, 5.0)
    assert _state(b) == before


def test_n_above_float_burst_is_rejected():
    b = TokenBucket(100.0, 1_000.5)
    b.take(1_000, 0.0)
    with pytest.raises(ValueError):
        b.delay(1_001, 0.0)


@pytest.mark.parametrize("now", ["5", None, [5.0]])
@pytest.mark.parametrize("method", ["available", "delay", "take"])
def test_rejects_now_that_is_not_a_number(method, now):
    b = _bucket_after_take()
    before = _state(b)
    args = (now,) if method == "available" else (1, now)
    with pytest.raises(TypeError):
        getattr(b, method)(*args)
    assert _state(b) == before


@pytest.mark.parametrize("now", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("method", ["available", "delay", "take"])
def test_rejects_now_not_finite(method, now):
    b = _bucket_after_take()
    before = _state(b)
    args = (now,) if method == "available" else (1, now)
    with pytest.raises(ValueError):
        getattr(b, method)(*args)
    assert _state(b) == before


@pytest.mark.parametrize("method", ["available", "delay", "take"])
def test_rejects_now_before_last_take(method):
    b = _bucket_after_take()
    before = _state(b)
    args = (4.999,) if method == "available" else (1, 4.999)
    with pytest.raises(ValueError):
        getattr(b, method)(*args)
    assert _state(b) == before


def test_rejects_non_finite_now_even_before_first_take():
    b = TokenBucket(100.0, 1_000)
    with pytest.raises(ValueError):
        b.available(math.nan)
    with pytest.raises(TypeError):
        b.delay(1, "0")
    assert b.available(0.0) == 1_000


HUGE = 10**400  # `int` hữu hạn nhưng không đổi được sang `float`


@pytest.mark.parametrize(
    "kwargs",
    [pytest.param({"rate": HUGE}, id="rate"), pytest.param({"rate": 1_000.0, "burst": HUGE}, id="burst")],
)
def test_huge_int_rate_or_burst_raises_only_spec_errors(kwargs):
    """Spec 2.1 chỉ cho `TypeError`/`ValueError` (soat-code tái hiện `OverflowError` với `10**400`).
    Nếu bucket nhận tham số đó thì mọi lời gọi hợp lệ sau đó cũng không được ném lỗi.
    """
    try:
        b = TokenBucket(**kwargs)
    except (TypeError, ValueError):
        return
    b.available(0.0)
    b.delay(1, 0.0)
    b.take(1, 0.0)
    b.available(1.0)
    b.delay(1, 1.0)


@pytest.mark.parametrize("method", ["available", "delay", "take"])
def test_huge_int_now_raises_only_spec_errors_and_keeps_state(method):
    """`now` là `int` quá lớn cho `float`: chỉ `TypeError`/`ValueError`, trạng thái giữ nguyên (spec 2.1)."""
    b = _bucket_after_take()
    before = _state(b)
    args = (HUGE,) if method == "available" else (1, HUGE)
    try:
        getattr(b, method)(*args)
    except (TypeError, ValueError):
        assert _state(b) == before
