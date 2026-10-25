"""Giới hạn tốc độ gửi (sans-IO): token bucket tính bằng byte (docs/protocol.md mục 4.2).

Tầng `app` dùng **một** `TokenBucket` chung cho mọi lượt gửi và áp nó theo từng đoạn
payload ≤ `CHUNK_SIZE` của khung khối, trước khi ghi đoạn đó:

    d = bucket.delay(n, loop.time())
    if d > 0:
        await asyncio.sleep(d)
    bucket.take(n, loop.time())

- **Chỉ `take` dời mốc thời gian.** `available` và `delay` là hàm thuần: không đổi trạng
  thái, gọi lặp với cùng tham số cho cùng kết quả.
- Bên gọi phải đọc đồng hồ mới (monotonic) ngay trước **mỗi** lời gọi; `now` nhỏ hơn mốc
  của `take` gần nhất → ValueError.
- `take` để tồn số âm (nợ), để sai số làm tròn của bên gọi (ngủ thiếu một chút) không bao
  giờ gây lỗi: lần chờ sau tự dài thêm. Vì vậy nhiều lượt dùng chung một bucket thì mỗi lần
  chờ có thể dài hơn `n / rate`, còn tổng tốc độ vẫn đúng `rate`.
- Burst mặc định bằng một đoạn: dòng byte không bao giờ vượt `rate` quá một đoạn, và với
  `rate` ≥ `CHUNK_SIZE`/s mỗi lần chờ ≤ 1 s.
"""

from __future__ import annotations

import math

# protocol §4.2: giới hạn tốc độ áp theo từng đoạn 64 KiB payload; burst bằng một đoạn.
# Bằng cỡ đoạn ghi khung khối của `teleport.net.connection`.
CHUNK_SIZE = 64 * 1024


def _finite_number(name: str, value: object) -> float:
    """Số `int`/`float` hữu hạn (bool bị từ chối) → TypeError / ValueError; số nguyên quá lớn cho
    float cũng là “không hữu hạn” (ValueError, không phải OverflowError)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} phải là số")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError(f"{name} phải là số hữu hạn") from None
    if not math.isfinite(result):
        raise ValueError(f"{name} phải là số hữu hạn")
    return result


def _check_positive_number(name: str, value: object, minimum: float, *, strict: bool) -> float:
    """Số `int`/`float` hữu hạn (bool bị từ chối); `strict`: > `minimum`, không thì ≥ `minimum`."""
    result = _finite_number(name, value)
    if result < minimum or (strict and result == minimum):
        relation = ">" if strict else "≥"
        raise ValueError(f"{name} phải {relation} {minimum:g}")
    return result


class TokenBucket:
    """Bucket `burst` byte, nạp lại `rate` byte/giây; trước lần `take` đầu tiên luôn đầy.

    `rate`: số `int`/`float` (bool bị từ chối, TypeError), hữu hạn và > 0 (ValueError).
    `burst`: như vậy, hữu hạn và ≥ 1. Ném lỗi thì trạng thái giữ nguyên.
    """

    __slots__ = ("_burst", "_rate", "_stamp", "_tokens")

    def __init__(self, rate: float, burst: float = CHUNK_SIZE) -> None:
        self._rate = _check_positive_number("rate", rate, 0.0, strict=True)
        self._burst = _check_positive_number("burst", burst, 1.0, strict=False)
        self._tokens = self._burst  # đúng tại `_stamp`
        self._stamp: float | None = None  # mốc của `take` gần nhất; None: chưa `take` lần nào

    def __repr__(self) -> str:
        return f"<TokenBucket rate={self._rate:g} B/s burst={self._burst:g} B>"

    @property
    def rate(self) -> float:
        """Tốc độ nạp lại, byte/giây."""
        return self._rate

    @property
    def burst(self) -> float:
        """Số token tối đa, byte."""
        return self._burst

    def available(self, now: float) -> float:
        """Số token tại `now`: ≤ `burst`, có thể âm (nợ). Hàm thuần, không dời mốc."""
        now = self._check_now(now)
        if self._stamp is None:
            return self._burst
        return min(self._burst, self._tokens + self._rate * (now - self._stamp))

    def delay(self, n: int, now: float) -> float:
        """Số giây (≥ 0) phải chờ từ `now` để lấy được `n` token. Hàm thuần, không dời mốc.

        `n` không phải `int` (bool bị từ chối) hoặc ngoài 1 … `burst` → ValueError: `n` lớn
        hơn `burst` không bao giờ thỏa được.
        """
        n = self._check_n(n)
        return max(0.0, (n - self.available(now)) / self._rate)

    def take(self, n: int, now: float) -> None:
        """Trừ `n` token tại `now` (được phép âm: nợ) rồi dời mốc về `now`."""
        n = self._check_n(n)
        tokens = self.available(now) - n
        self._tokens = tokens
        self._stamp = float(now)

    # ---- Nội bộ --------------------------------------------------------------------------

    def _check_n(self, n: object) -> int:
        if type(n) is not int or not 1 <= n <= self._burst:
            raise ValueError(f"n phải là số nguyên 1…{self._burst:g}")
        return n

    def _check_now(self, now: object) -> float:
        if isinstance(now, bool) or not isinstance(now, int | float):
            raise TypeError("now phải là số giây (đồng hồ monotonic)")
        value = _finite_number("now", now)
        if self._stamp is not None and value < self._stamp:
            raise ValueError("now không được nhỏ hơn mốc của lần take gần nhất")
        return value
