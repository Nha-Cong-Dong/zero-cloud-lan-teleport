"""Chuyển động trong luồng chat (spec G4; mvp N3, N12): hằng, hàm thuần, trạng thái theo bong bóng.

Luồng chat là widget thật (`ui.chat.ChatThread`: mỗi bong bóng một widget trong một hàng `_Item.wrapper`),
nên hoạt ảnh không dựng widget mới: mỗi bong bóng có một `BubbleAnim` (mốc thời gian, mili giây theo
`_now`) mà phần vẽ của bong bóng (`ui.chat_bubbles`: nền, viền, icon trạng thái, thanh tiến độ) đọc lúc
vẽ; mỗi khung chỉ `update()` đúng widget đang chuyển động. Một `ThreadMotion` cho mỗi `ChatThread`, chạy
trên một `motion.FrameDriver` (con của luồng): không còn việc thì dừng, luồng hay cửa sổ ẩn/thu nhỏ thì
mọi trạng thái nhảy về cuối.

**Chuyển động** (token ở `ui.motion`)
- Bong bóng mới tới (`reveal`): cả hàng (kể cả dòng mốc của nó) mờ dần và trượt lên `MAX_SHIFT_PX` trong
  `NORMAL.ms` (`reveal_values`).
- Tiến độ (`progress`): thanh vẽ giá trị nội suy tuyến tính (`ProgressTween`) tới số thật, không bao giờ
  vượt hay lùi; `value()` của thanh luôn là số thật (trình đọc màn hình không đổi). Bước < 2 px đặt thẳng.
- Xong (`done`): dấu tích quét lộ trái → phải (`check_fraction`), nền phủ `success` tới `TINT_MAX` rồi dịu
  về (`tint_alpha`), tổng `DONE_TOTAL_MS`.
- Lỗi (`fail`): hàng rung ngang `SHAKE_PX`, `SHAKE_CYCLES` dao động, một lần (`shake_offset`); viền DANGER
  hiện ngay (do `render_status`). Dòng mốc của hàng (phần trên `still_top`) không rung.
- Lời mời (`offer`): viền nhấn dày thêm tối đa `FLASH_EXTRA_PX` một nhịp (`flash_extra`), sau khi mờ dần
  xong nếu đang mờ dần.

**Luật**
- Mọi lệnh kích hoạt gọi `FrameDriver.wake()` trước; False (tắt hiệu ứng, luồng/cửa sổ ẩn hay thu nhỏ) →
  không dựng trạng thái, chỉ `update()`: trạng thái cuối ngay (N12). Tiêu điểm, tên trợ năng, hình học
  và cây trợ năng không đổi theo hoạt ảnh.
- Hiệu ứng đồ họa duy nhất là `_MotionEffect`, gắn lên **một** hàng (`_Item.wrapper`, qua `host` của
  `ThreadMotion`) chỉ trong lúc mờ dần hay rung (mỗi pha ≤ `SLOW.ms`), gỡ ngay khi xong; hàng đã có
  hiệu ứng khác thì không gắn. Qt xóa hiệu ứng C++ sau `setGraphicsEffect(None)`: mọi chỗ chạm hiệu ứng kiểm
  `isValid`. Phần dưới ≤ `MAX_SHIFT_PX` của hàng có thể bị khung cha cắt lúc đầu pha mờ dần (độ mờ ≈ 0):
  chấp nhận.
- Tween tiến độ được nhớ (kể cả `last_update`) tới khi bong bóng kết thúc (`stop_progress`), `forget`
  hay luồng `finish`; tween xong thì driver dừng dù trạng thái còn.
- `finish()` (và driver tự `finish()` khi ẩn) gỡ mọi hiệu ứng, xóa mọi trạng thái, vẽ lại.

**Seam test** (cấp module, đọc lúc gọi): `_now` (mili giây đơn điệu); `ThreadMotion.tick(now)` chạy một
khung, `ThreadMotion.state(widget)` đọc trạng thái. Quy ước fixture: `architecture.md` §6.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEasingCurve, QObject, QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QGraphicsEffect, QWidget
from shiboken6 import isValid

from teleport.ui.motion import EMPHASIS, FAST, MAX_SHIFT_PX, NORMAL, SLOW, FrameDriver
from teleport.ui.transfers_view import BAR_MAX

SHAKE_PX = 4
SHAKE_CYCLES = 2
SHAKE_MS = SLOW.ms
TINT_MAX = 0.16
FLASH_EXTRA_PX = 2
FLASH_MS = EMPHASIS.ms
PROGRESS_MIN_MS = NORMAL.ms
PROGRESS_MAX_MS = 1000
DONE_TOTAL_MS = EMPHASIS.ms + SLOW.ms
_SNAP_PX = 2  # bước tiến độ ngắn hơn mức này (px) thì đặt thẳng, không tween


def _monotonic_ms() -> float:
    return time.monotonic() * 1000.0


# Seam: đồng hồ đơn điệu (mili giây) của mọi hoạt ảnh trong luồng; đọc lúc gọi.
_now: Callable[[], float] = _monotonic_ms


# ---- Hàm thuần --------------------------------------------------------------------------------


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _progress(elapsed: float, duration: float) -> float:
    """`clamp(elapsed / duration, 0, 1)`; `duration` ≤ 0 → 1."""
    if duration <= 0:
        return 1.0
    return _clamp(elapsed / duration, 0.0, 1.0)


def reveal_values(elapsed: float) -> tuple[float, float]:
    """(độ mờ, dịch dọc px) của bong bóng mới sau `elapsed` ms: (0, `MAX_SHIFT_PX`) → (1, 0) trong
    `NORMAL.ms`."""
    p = _progress(elapsed, NORMAL.ms)
    if p >= 1.0:
        return 1.0, 0.0
    eased = _clamp(NORMAL.easing().valueForProgress(p), 0.0, 1.0)
    return eased, MAX_SHIFT_PX * (1.0 - eased)


def shake_offset(elapsed: float) -> float:
    """Dịch ngang px của lần rung khi lỗi: |dx| ≤ `SHAKE_PX`, 0 ở đầu và từ `SHAKE_MS` trở đi."""
    p = _progress(elapsed, SHAKE_MS)
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return SHAKE_PX * math.sin(2 * math.pi * SHAKE_CYCLES * p) * (1.0 - p)


def check_fraction(elapsed: float) -> float:
    """Phần dấu tích đã lộ (0…1, trái → phải) sau `elapsed` ms, theo `EMPHASIS`; không giảm."""
    p = _progress(elapsed, EMPHASIS.ms)
    if p >= 1.0:
        return 1.0
    return _clamp(EMPHASIS.easing().valueForProgress(p), 0.0, 1.0)


def tint_alpha(elapsed: float) -> float:
    """Độ phủ tương đối (0…1) của nền `success` khi xong: lên trong `FAST.ms`, giữ tới `EMPHASIS.ms`, dịu
    về 0 trong `SLOW.ms` (InOutCubic)."""
    if elapsed < 0:
        return 0.0
    if elapsed < FAST.ms:
        return elapsed / FAST.ms
    if elapsed < EMPHASIS.ms:
        return 1.0
    q = (elapsed - EMPHASIS.ms) / SLOW.ms if SLOW.ms > 0 else 1.0
    if q < 1.0:
        return _clamp(1.0 - QEasingCurve(QEasingCurve.Type.InOutCubic).valueForProgress(q), 0.0, 1.0)
    return 0.0


def flash_extra(elapsed: float) -> float:
    """Độ dày thêm px của viền nhấn khi có lời mời: một nhịp 0 → `FLASH_EXTRA_PX` → 0 trong `FLASH_MS`."""
    if elapsed <= 0 or elapsed >= FLASH_MS:
        return 0.0
    return FLASH_EXTRA_PX * math.sin(math.pi * _progress(elapsed, FLASH_MS))


# ---- Trạng thái -------------------------------------------------------------------------------


@dataclass(slots=True)
class ProgressTween:
    """Giá trị vẽ của thanh tiến độ (thang `BAR_MAX`), nội suy tuyến tính từ `start` tới `target`.

    Bất biến: `value_at(now)` ≤ `target` ≤ số thật; không giảm theo thời gian, kể cả khi `retarget` giữa
    chừng; từ `t0 + duration` trở đi bằng `target`.
    """

    start: float
    target: float
    t0: float
    duration: float
    last_update: float

    def value_at(self, now: float) -> float:
        p = _progress(now - self.t0, self.duration)
        return self.start + (self.target - self.start) * p

    def retarget(self, real: float, now: float) -> None:
        """Số thật mới `real` lúc `now`: thời lượng theo khoảng giữa hai lần báo, kẹp trong
        [`PROGRESS_MIN_MS`, `PROGRESS_MAX_MS`]; không tăng thì chỉ ghi lại lúc báo.

        Thứ tự: `start` = giá trị đang vẽ (`value_at(now)` theo `t0`, `duration` CŨ) phải tính trước khi
        đổi `duration` / `t0` / `target`, không thì thanh nhảy cóc."""
        if real <= self.target:
            self.last_update = now
            return
        start = self.value_at(now)
        gap = now - self.last_update
        self.duration = _clamp(gap, PROGRESS_MIN_MS, PROGRESS_MAX_MS)
        self.start = start
        self.target = real
        self.t0 = now
        self.last_update = now

    def active(self, now: float) -> bool:
        return self.value_at(now) < self.target


@dataclass(slots=True)
class BubbleAnim:
    """Trạng thái chuyển động của một bong bóng (mốc theo `_now`, ms; None: không có)."""

    reveal_at: float | None = None
    shake_at: float | None = None
    done_at: float | None = None
    flash_at: float | None = None
    bar: ProgressTween | None = None
    bar_px: int = -1  # bề ngang phần đã chạy lần vẽ trước (px); chỉ vẽ lại thanh khi đổi

    def active(self, now: float) -> bool:
        """Còn gì đang chuyển động lúc `now`."""
        return (
            (self.reveal_at is not None and now - self.reveal_at < NORMAL.ms)
            or (self.shake_at is not None and now - self.shake_at < SHAKE_MS)
            or (self.done_at is not None and now - self.done_at < DONE_TOTAL_MS)
            or (self.flash_at is not None and now - self.flash_at < FLASH_MS)
            or (self.bar is not None and self.bar.active(now))
        )

    def empty(self) -> bool:
        """Không còn mốc nào và không còn tween."""
        return (
            self.reveal_at is None
            and self.shake_at is None
            and self.done_at is None
            and self.flash_at is None
            and self.bar is None
        )


# ---- Hiệu ứng tạm trên một hàng ---------------------------------------------------------------


class _MotionEffect(QGraphicsEffect):
    """Mờ dần, trượt lên và rung của **một** hàng (xem docstring module, “Luật”).

    Không giữ tham chiếu tới widget: `ThreadMotion` chép mốc `reveal_at`, `shake_at` và `still_top` (phần
    trên, px, vẽ không dịch ngang: dòng mốc) vào đây; `draw` tính giá trị theo `_now()` lúc vẽ.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reveal_at: float | None = None
        self.shake_at: float | None = None
        self.still_top = 0

    def boundingRectFor(self, rect: QRectF | QRect) -> QRectF:
        return QRectF(rect).adjusted(-SHAKE_PX, 0, SHAKE_PX, MAX_SHIFT_PX)

    def values(self, now: float) -> tuple[float, float, float]:
        """(độ mờ, dx, dy) lúc `now`."""
        opacity, dy = (1.0, 0.0) if self.reveal_at is None else reveal_values(now - self.reveal_at)
        dx = 0.0 if self.shake_at is None else shake_offset(now - self.shake_at)
        return opacity, dx, dy

    def draw(self, painter: QPainter) -> None:
        opacity, dx, dy = self.values(_now())
        ix, iy = round(dx), round(dy)
        if opacity >= 1.0 and ix == 0 and iy == 0:
            self.drawSource(painter)
            return
        offset = QPoint()
        pixmap = self.sourcePixmap(
            Qt.CoordinateSystem.LogicalCoordinates, offset, QGraphicsEffect.PixmapPadMode.NoPad
        )
        if pixmap.isNull():
            return
        dpr = pixmap.devicePixelRatio() or 1.0
        width, height = pixmap.width() / dpr, pixmap.height() / dpr
        top = min(max(0, self.still_top), int(height)) if ix else 0
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * max(0.0, opacity))
            x, y = float(offset.x()), float(offset.y() + iy)
            if top <= 0:
                painter.drawPixmap(QPoint(offset.x() + ix, offset.y() + iy), pixmap)
            else:
                painter.drawPixmap(QRectF(x, y, width, top), pixmap, QRectF(0, 0, pixmap.width(), top * dpr))
                painter.drawPixmap(
                    QRectF(x + ix, y + top, width, height - top),
                    pixmap,
                    QRectF(0, top * dpr, pixmap.width(), pixmap.height() - top * dpr),
                )
        finally:
            painter.restore()


# ---- Một luồng --------------------------------------------------------------------------------

# `host(widget)` → (hàng chứa widget, `still_top`), hay None khi widget không còn trong luồng.
Host = Callable[[QWidget], "tuple[QWidget, int] | None"]


class ThreadMotion(QObject):
    """Chuyển động của một `ChatThread` (xem docstring module); con của `thread`.

    `host` cho biết hàng (widget nhận `_MotionEffect`) của một bong bóng; không có thì chính bong bóng.
    Các lệnh kích hoạt trả True khi đã dựng trạng thái (hoạt ảnh sẽ chạy), False khi nhảy thẳng tới cuối.
    """

    def __init__(self, thread: QWidget, *, host: Host | None = None) -> None:
        super().__init__(thread)
        self._host = host
        self._states: dict[QWidget, BubbleAnim] = {}
        self._effects: dict[QWidget, tuple[QWidget, _MotionEffect]] = {}
        self._bars: dict[QWidget, QWidget] = {}
        self._driver = FrameDriver(thread, self.tick, self._settle_all)

    @property
    def driver(self) -> FrameDriver:
        return self._driver

    def state(self, widget: QWidget) -> BubbleAnim | None:
        return self._states.get(widget)

    def active_count(self) -> int:
        """Số bong bóng còn đang chuyển động (tween tiến độ đã xong không tính)."""
        now = _now()
        return sum(1 for st in self._states.values() if st.active(now))

    # -- Kích hoạt ------------------------------------------------------------------------------

    def reveal(self, widget: QWidget) -> bool:
        """Hàng của `widget` mờ dần, trượt lên."""
        host = self._host_of(widget)
        if host is None or self._foreign_effect(widget, host[0]):
            return False
        if not self._driver.wake():
            widget.update()
            return False
        now = _now()
        st = self._states.setdefault(widget, BubbleAnim())
        st.reveal_at = now
        effect = self._effect_for(widget, host[0])
        effect.reveal_at = now
        effect.update()
        return True

    def progress(self, bubble: QWidget, bar: QWidget, old: int, new: int) -> None:
        """Thanh `bar` của `bubble` vừa tăng `old` → `new` (thang `BAR_MAX`, đã `setValue(new)`)."""
        st = self._states.get(bubble)
        tween = None if st is None else st.bar
        if not self._driver.wake():
            if st is not None and tween is not None:
                st.bar = None
                st.bar_px = -1
                self._bars.pop(bubble, None)
                self._drop_if_empty(bubble)
            bar.update()
            return
        now = _now()
        if st is None:
            st = self._states.setdefault(bubble, BubbleAnim())
        if tween is None:
            tween = ProgressTween(float(old), float(old), now, PROGRESS_MIN_MS, now - PROGRESS_MIN_MS)
            st.bar = tween
        self._bars[bubble] = bar
        if new > tween.target:
            shown = tween.value_at(now)
            if (new - shown) * max(0, bar.width()) / BAR_MAX < _SNAP_PX:
                tween.start = tween.target = float(new)
                tween.t0 = now
                tween.last_update = now
            else:
                tween.retarget(float(new), now)
        else:
            tween.retarget(float(new), now)
        st.bar_px = -1
        bar.update()

    def stop_progress(self, bubble: QWidget) -> None:
        """Bỏ tween thanh của `bubble` (lượt kết thúc)."""
        st = self._states.get(bubble)
        self._bars.pop(bubble, None)
        if st is not None and st.bar is not None:
            st.bar = None
            st.bar_px = -1
            self._drop_if_empty(bubble)

    def done(self, bubble: QWidget) -> bool:
        """Lượt xong: dấu tích quét lộ, nền phủ `success` rồi dịu về."""
        if not self._driver.wake():
            self._repaint(bubble)
            return False
        st = self._states.setdefault(bubble, BubbleAnim())
        st.done_at = _now()
        self._repaint(bubble)
        return True

    def fail(self, bubble: QWidget) -> bool:
        """Lượt lỗi: hàng rung ngang một lần."""
        host = self._host_of(bubble)
        if host is None or self._foreign_effect(bubble, host[0]):
            bubble.update()
            return False
        if not self._driver.wake():
            bubble.update()
            return False
        now = _now()
        st = self._states.setdefault(bubble, BubbleAnim())
        st.shake_at = now
        effect = self._effect_for(bubble, host[0])
        effect.shake_at = now
        effect.still_top = host[1]
        effect.update()
        return True

    def offer(self, bubble: QWidget) -> bool:
        """Có lời mời: viền nhấn nháy một nhịp (sau khi mờ dần xong, nếu đang mờ dần)."""
        if not self._driver.wake():
            bubble.update()
            return False
        now = _now()
        st = self._states.setdefault(bubble, BubbleAnim())
        start = now
        if st.reveal_at is not None:
            start = max(now, st.reveal_at + NORMAL.ms)
        st.flash_at = start
        bubble.update()
        return True

    def forget(self, widget: QWidget) -> None:
        """Bỏ mọi trạng thái và hiệu ứng của `widget` (bong bóng bị gỡ khỏi luồng)."""
        self._remove_effect(widget)
        self._states.pop(widget, None)
        self._bars.pop(widget, None)
        if not self._states:
            self._driver.stop()

    def finish(self) -> None:
        """Dừng driver, mọi trạng thái nhảy về cuối."""
        self._driver.stop()
        self._settle_all()

    # -- Khung ----------------------------------------------------------------------------------

    def tick(self, now: float | None = None) -> bool:
        """Một khung lúc `now` (mặc định `_now()`); True nếu còn gì đang chuyển động. Hết việc thì dừng
        driver (tween tiến độ đã xong vẫn được nhớ)."""
        if now is None:
            now = _now()
        for widget, st in list(self._states.items()):
            if not isValid(widget):
                self._effects.pop(widget, None)
                self._bars.pop(widget, None)
                self._states.pop(widget, None)
                continue
            self._step(widget, st, now)
            self._drop_if_empty(widget)
        working = any(st.active(now) for st in self._states.values())
        if not working:
            self._driver.stop()
        return working

    def _step(self, widget: QWidget, st: BubbleAnim, now: float) -> None:
        if st.reveal_at is not None and now - st.reveal_at >= NORMAL.ms:
            st.reveal_at = None
        if st.shake_at is not None and now - st.shake_at >= SHAKE_MS:
            st.shake_at = None
        entry = self._effects.get(widget)
        if entry is not None:
            effect = entry[1]
            if st.reveal_at is None and st.shake_at is None:
                self._remove_effect(widget)
            elif isValid(effect):
                effect.reveal_at = st.reveal_at
                effect.shake_at = st.shake_at
                effect.update()
            else:  # Qt đã xóa hiệu ứng (hàng bị hủy hay hiệu ứng bị thay)
                self._effects.pop(widget, None)
        paint = False
        if st.done_at is not None:
            paint = True
            icon = getattr(widget, "status_icon", None)
            if isinstance(icon, QWidget) and isValid(icon):
                icon.update()
            if now - st.done_at >= DONE_TOTAL_MS:
                st.done_at = None
        if st.flash_at is not None:
            paint = paint or now >= st.flash_at
            if now - st.flash_at >= FLASH_MS:
                st.flash_at = None
        if paint:
            widget.update()
        if st.bar is not None:
            bar = self._bars.get(widget)
            if bar is None or not isValid(bar) or bar.isHidden():
                st.bar = None
                st.bar_px = -1
                self._bars.pop(widget, None)
            else:
                px = round(bar.width() * st.bar.value_at(now) / BAR_MAX)
                if px != st.bar_px:
                    st.bar_px = px
                    bar.update()

    # -- Bên trong ------------------------------------------------------------------------------

    def _host_of(self, widget: QWidget) -> tuple[QWidget, int] | None:
        if self._host is None:
            return widget, 0
        return self._host(widget)

    def _foreign_effect(self, widget: QWidget, host: QWidget) -> bool:
        """Hàng đã có hiệu ứng đồ họa không phải của mình."""
        current = host.graphicsEffect()
        if current is None:
            return False
        entry = self._effects.get(widget)
        return entry is None or entry[1] is not current

    def _effect_for(self, widget: QWidget, host: QWidget) -> _MotionEffect:
        entry = self._effects.get(widget)
        if entry is not None and isValid(entry[1]) and host.graphicsEffect() is entry[1]:
            return entry[1]
        effect = _MotionEffect()
        host.setGraphicsEffect(effect)
        self._effects[widget] = (host, effect)
        return effect

    def _remove_effect(self, widget: QWidget) -> None:
        entry = self._effects.pop(widget, None)
        if entry is None:
            return
        host, effect = entry
        if isValid(effect) and isValid(host) and host.graphicsEffect() is effect:
            # Stub của PySide6 không nhận None, Qt thì nhận (gỡ và xóa hiệu ứng C++).
            host.setGraphicsEffect(None)  # type: ignore[arg-type]

    def _drop_if_empty(self, widget: QWidget) -> None:
        st = self._states.get(widget)
        if st is not None and st.empty():
            del self._states[widget]
            self._bars.pop(widget, None)

    def _repaint(self, widget: QWidget) -> None:
        if not isValid(widget):
            return
        widget.update()
        icon = getattr(widget, "status_icon", None)
        if isinstance(icon, QWidget) and isValid(icon):
            icon.update()

    def _settle_all(self) -> None:
        """Gỡ mọi hiệu ứng, xóa mọi trạng thái, vẽ lại (trạng thái cuối)."""
        # Lúc cây widget bị hủy, thuộc tính Python có thể đã mất (bẫy giống `motion.Loop.eventFilter`).
        if "_states" not in self.__dict__:
            return
        for widget in list(self._effects):
            self._remove_effect(widget)
        bars = list(self._bars.values())
        widgets = list(self._states)
        self._states.clear()
        self._bars.clear()
        for widget in widgets:
            self._repaint(widget)
        for bar in bars:
            if isValid(bar):
                bar.update()
