"""Chuyển động trong khung chat (spec G4 mục 3 và 6; mục 11 thắng mục 2–9 khi khác).

Viết từ spec, không từ code. Mặc định chế độ tức thì (autouse `motion_instant`). Ca hoạt ảnh gọi
`animate()` (fixture cùng tên) **sau** khi dựng xong luồng: tắt chế độ tức thì, vá `animations_enabled`
→ True (như `real_motion`) và vá `teleport.ui.chat_motion._now` bằng đồng hồ giả (mili giây, đứng yên
tới khi test tự đổi). Khung hình chạy bằng `thread.motion.tick(now)`; không `qWait`, không kiểm ảnh.

Quy ước đọc trạng thái (spec mục 3.3, 11.2): `ThreadMotion.state(widget)`; hiệu ứng tạm nằm trên
`_Item.wrapper` của hàng, tìm bằng cách đi lên từ bong bóng tới con trực tiếp của `chatThreadContent`.
Trạng thái mờ dần có thể gắn với bong bóng hay với wrapper (spec không nói rõ khóa): test đọc cả hai.
"""

from __future__ import annotations

import datetime
import itertools
import math
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from hypothesis import given, settings
from hypothesis import strategies as st
from PySide6.QtCore import QEasingCurve, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

PEER_ID = "a71c04d2" + "e" * 24
OTHER_ID = "0f0f0f0f" + "1" * 24
PEER = "PC-Nha"
OTHER = "Laptop-Minh"
WALL_NOW = datetime.datetime(2026, 11, 3, 10, 40).timestamp()
MiB = 1024 * 1024
T0 = 1_000_000.0  # mili giây của đồng hồ giả `chat_motion._now`


# =================================================================================================
# Hàm phụ trợ (định nghĩa tại chỗ: không import conftest)
# =================================================================================================


def settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


def is_alive(obj) -> bool:
    import shiboken6

    return obj is not None and shiboken6.isValid(obj)


def tid(i: int) -> str:
    return f"{i:032x}"


def key(direction: str = "receive", i: int = 1, device_id: str = PEER_ID):
    from teleport.core.transfer import Direction, TransferKey

    return TransferKey(device_id, Direction(direction), tid(i))


def info(k, status: str = "transferring", *, done: int = 0, total: int = 1000 * MiB):
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import TransferInfo, TransferStatus

    return TransferInfo(
        key=k,
        status=TransferStatus(status),
        files=(FileEntry("video.mp4", total, 1_700_000_000_000),),
        names=("video.mp4",),
        file_index=0,
        done_bytes=done,
        total_bytes=total,
    )


def status_changed(k, status: str = "transferring"):
    from teleport.app.transfers import TransferStatusChanged

    return TransferStatusChanged(info(k, status))


def progress(k, shown: int, total: int = 1000 * MiB):
    from teleport.app.transfers import TransferProgress

    return TransferProgress(info(k, total=total), 50.0 * MiB, 10.0, shown_bytes=shown, idle_seconds=0.0)


def finished(k, result: str, *, status: str = "transferring"):
    from teleport.app.transfers import TransferFinished, TransferResult

    return TransferFinished(
        info=info(k, status),
        result=TransferResult(result),
        by_peer=False,
        reason=None,
        file_index=0,
        reject=None,
    )


def make_offer(k):
    from teleport.app.transfers import OfferReceived
    from teleport.core.messages import FileEntry

    return OfferReceived(
        key=k,
        device_name=PEER,
        files=(FileEntry("video.mp4", 1000, 1_700_000_000_000),),
        names=("video.mp4",),
        total_bytes=1000,
        auto_accepted=False,
    )


def hentry(i: int, direction: str = "send", device_id: str = OTHER_ID):
    from teleport.core.history import HistoryEntry

    total = 3 * MiB
    return HistoryEntry(
        direction=direction,
        device_id=device_id,
        device_name=OTHER,
        transfer_id=tid(i),
        file_count=1,
        total_bytes=total,
        done_bytes=total,
        names=("video.mp4",),
        result="done",
        finished_at=datetime.datetime(2026, 11, 2, 9, i % 60, tzinfo=datetime.UTC),
        saved_path=None,
    )


def dview(device_id: str, name: str):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id,
        name=name,
        os="windows",
        paired=True,
        online=True,
        connected=True,
        addresses=("192.168.1.20",),
        port=47474,
    )


def wrapper_of(widget):
    """`_Item.wrapper` chứa `widget`: tổ tiên là con trực tiếp của `chatThreadContent` (None nếu không có)."""
    w = widget
    while is_alive(w):
        parent = w.parentWidget()
        if parent is not None and parent.objectName() == "chatThreadContent":
            return w
        w = parent
    return None


def effects_of(widget) -> list:
    """Mọi `graphicsEffect()` khác None trên `widget` và các tổ tiên của nó tới hết wrapper."""
    out = []
    w = widget
    stop = wrapper_of(widget)
    while is_alive(w):
        effect = w.graphicsEffect()
        if effect is not None:
            out.append(effect)
        if w is stop:
            break
        w = w.parentWidget()
    return out


def states(thread, widget) -> list:
    """Trạng thái (`BubbleAnim`) khác None của `widget` và của wrapper chứa nó."""
    motion = thread.motion
    out = []
    for w in (widget, wrapper_of(widget)):
        if w is None:
            continue
        st_ = motion.state(w)
        if st_ is not None and all(st_ is not s for s in out):
            out.append(st_)
    return out


def field(thread, widget, name: str):
    """Giá trị khác None đầu tiên của trường `name` trong trạng thái của `widget` (hay wrapper)."""
    for st_ in states(thread, widget):
        value = getattr(st_, name)
        if value is not None:
            return value
    return None


def bar_state(thread, bubble):
    st_ = thread.motion.state(bubble)
    return None if st_ is None else st_.bar


def drivers(thread) -> list:
    from teleport.ui import motion

    return [o for o in thread.findChildren(QObject) if isinstance(o, motion.FrameDriver) and is_alive(o)]


def assert_idle(thread, *, states_cleared: bool = True) -> None:
    """Không gì chạy (N3): sổ của `motion` rỗng, driver của luồng dừng, timer của nó tắt."""
    from teleport.ui import motion

    assert motion.running_count() == 0
    found = drivers(thread)
    assert found, "ChatThread phải có một FrameDriver (spec 3.3)"
    for driver in found:
        assert not driver.running
        assert not any(t.isActive() for t in driver.findChildren(QTimer))
    if states_cleared:
        assert thread.motion.active_count() == 0


def assert_still(thread, widget) -> None:
    """`widget` không có trạng thái, không có hiệu ứng (trên nó hay trên hàng)."""
    assert states(thread, widget) == [], states(thread, widget)
    assert effects_of(widget) == []


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Harness:
    """`ChatView` + `OfferBook` thật trong một cửa sổ; luồng PC-Nha đang mở, có sẵn một bong bóng."""

    def __init__(self, qtbot, receive_dir: Path) -> None:
        from teleport.ui.chat import ChatView
        from teleport.ui.offer import OfferBook
        from teleport.ui.radar import DeviceListModel

        self.host = QWidget()
        qtbot.addWidget(self.host)
        self.devices = DeviceListModel(self.host)
        self.devices.reset([dview(PEER_ID, PEER), dview(OTHER_ID, OTHER)])
        self.before = QPushButton("Trước", self.host)
        self.view = ChatView(self.devices, self.host)
        lay = QVBoxLayout(self.host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.before)
        lay.addWidget(self.view, 1)
        self.host.resize(720, 560)
        self.book = OfferBook(self.host)
        v = self.view
        v.set_device_names({PEER_ID: PEER, OTHER_ID: OTHER})
        v.set_connected(PEER_ID, True)
        v.set_connected(OTHER_ID, True)
        v.set_receive_dir(receive_dir)
        v.set_offer_book(self.book)

    @property
    def thread(self):
        return self.view.thread_for(PEER_ID)

    def offer(self, k):
        """Như controller: `view.handle` rồi `book.add`. Trả bong bóng."""
        o = make_offer(k)
        self.view.handle(o)
        assert self.book.add(o)
        settle()
        return self.view.row(k)

    def new_send(self, i: int):
        k = key("send", i)
        self.view.handle(status_changed(k))
        settle()
        return self.view.row(k)


@pytest.fixture
def h(qtbot, tmp_path, monkeypatch):
    from teleport.ui import chat

    monkeypatch.setattr(chat, "_wall_now", lambda: WALL_NOW)
    harness = Harness(qtbot, tmp_path / "nhan")
    harness.host.show()
    qtbot.waitExposed(harness.host)
    harness.host.activateWindow()
    harness.host.raise_()
    qtbot.waitUntil(harness.host.isActiveWindow, timeout=3000)
    harness.view.set_device(PEER_ID)
    # Một bong bóng có sẵn (tức thì): trang của luồng đang hiện, không phải trang “trống”.
    k = key("send", 900)
    harness.view.handle(status_changed(k))
    harness.view.handle(finished(k, "done"))
    settle()
    assert harness.thread.isVisible()
    yield harness
    harness.book.clear()
    settle()


@pytest.fixture
def animate(monkeypatch):
    """`animate() -> Clock`: bật hoạt ảnh thật (như `real_motion`) và vá `chat_motion._now`.

    Gọi sau khi đã dựng xong cảnh ở chế độ tức thì. Dọn: `motion_instant` (autouse) dừng mọi thứ."""

    def on(t: float = T0) -> Clock:
        from teleport.ui import chat_motion, motion

        clock = Clock(t)
        monkeypatch.setattr(chat_motion, "_now", clock)
        motion._force_instant(False)
        monkeypatch.setattr(motion, "animations_enabled", lambda: True)
        return clock

    return on


@pytest.fixture
def cm():
    from teleport.ui import chat_motion

    return chat_motion


def grid(lo: float, hi: float, step: float) -> list[float]:
    n = round((hi - lo) / step)
    return [lo + i * step for i in range(n + 1)]


# =================================================================================================
# Hằng (mục 3.2)
# =================================================================================================


def test_chat_motion_constants_match_spec(cm):
    from teleport.ui import motion

    assert cm.SHAKE_PX == 4
    assert cm.SHAKE_CYCLES == 2
    assert cm.SHAKE_MS == motion.SLOW.ms == 280
    assert abs(cm.TINT_MAX - 0.16) < 1e-9
    assert cm.FLASH_EXTRA_PX == 2
    assert cm.FLASH_MS == motion.EMPHASIS.ms == 450
    assert cm.PROGRESS_MIN_MS == motion.NORMAL.ms == 200
    assert cm.PROGRESS_MAX_MS == 1000
    assert cm.DONE_TOTAL_MS == motion.EMPHASIS.ms + motion.SLOW.ms == 730


def test_now_seam_is_monotonic_milliseconds(cm):
    import time

    a = cm._now()
    b = time.monotonic() * 1000
    c = cm._now()
    assert a <= c
    assert abs(b - a) < 1000, "mili giây theo time.monotonic()"


# =================================================================================================
# T1–T5 hàm thuần
# =================================================================================================


def test_t1_reveal_values_ends_and_is_monotonic(cm):
    from teleport.ui import motion

    assert cm.reveal_values(0) == pytest.approx((0.0, 8.0))
    assert cm.reveal_values(-50) == pytest.approx((0.0, 8.0)), "elapsed âm → p = 0"
    for t in (motion.NORMAL.ms, motion.NORMAL.ms + 1, 10_000):
        assert cm.reveal_values(t) == (1.0, 0.0)
    curve = motion.NORMAL.easing()
    prev = (-1.0, math.inf)
    for t in grid(0, motion.NORMAL.ms + 40, 10):
        opacity, dy = cm.reveal_values(t)
        e = curve.valueForProgress(min(1.0, t / motion.NORMAL.ms))
        assert opacity == pytest.approx(e)
        assert dy == pytest.approx(motion.MAX_SHIFT_PX * (1 - e))
        assert 0 <= dy <= motion.MAX_SHIFT_PX
        assert opacity >= prev[0], f"opacity giảm tại {t}"
        assert dy <= prev[1], f"dy tăng tại {t}"
        prev = (opacity, dy)


def test_t2_shake_offset_bounded_and_zero_at_ends(cm):
    assert cm.shake_offset(0) == pytest.approx(0.0, abs=1e-9)
    for t in (cm.SHAKE_MS, cm.SHAKE_MS + 1, 5000):
        assert cm.shake_offset(t) == pytest.approx(0.0, abs=1e-9)
    assert cm.shake_offset(-30) == pytest.approx(0.0, abs=1e-9)
    values = [cm.shake_offset(t) for t in grid(0, cm.SHAKE_MS, 1)]
    assert all(abs(v) <= cm.SHAKE_PX + 1e-9 for v in values)
    assert max(abs(v) for v in values) > 1.0, "có rung thật"
    for t in grid(0, cm.SHAKE_MS, 7):
        p = t / cm.SHAKE_MS
        want = cm.SHAKE_PX * math.sin(2 * math.pi * cm.SHAKE_CYCLES * p) * (1 - p)
        assert cm.shake_offset(t) == pytest.approx(want, abs=1e-9)


def test_t3_check_fraction_in_unit_range_and_monotonic(cm):
    from teleport.ui import motion

    assert cm.check_fraction(0) == pytest.approx(0.0, abs=1e-9)
    assert cm.check_fraction(-10) == pytest.approx(0.0, abs=1e-9)
    for t in (motion.EMPHASIS.ms, motion.EMPHASIS.ms + 1, 9999):
        assert cm.check_fraction(t) == pytest.approx(1.0)
    prev = -1.0
    for t in grid(0, motion.EMPHASIS.ms + 50, 5):
        f = cm.check_fraction(t)
        assert 0.0 <= f <= 1.0
        assert f >= prev - 1e-12, f"check_fraction giảm tại {t}"
        prev = f


def test_t4_tint_alpha_phases(cm):
    from teleport.ui import motion

    assert cm.tint_alpha(-1) == 0
    assert cm.tint_alpha(-500) == 0
    assert cm.tint_alpha(0) == pytest.approx(0.0, abs=1e-9)
    assert cm.tint_alpha(motion.FAST.ms / 2) == pytest.approx(0.5)
    for t in grid(motion.FAST.ms, motion.EMPHASIS.ms - 1, 1):
        assert cm.tint_alpha(t) == pytest.approx(1.0), t
    curve = QEasingCurve(QEasingCurve.Type.InOutCubic)
    for t in grid(motion.EMPHASIS.ms, cm.DONE_TOTAL_MS - 1, 10):
        q = (t - motion.EMPHASIS.ms) / motion.SLOW.ms
        assert cm.tint_alpha(t) == pytest.approx(1 - curve.valueForProgress(q), abs=1e-9)
    for t in (cm.DONE_TOTAL_MS, cm.DONE_TOTAL_MS + 1, 100_000):
        assert cm.tint_alpha(t) == pytest.approx(0.0, abs=1e-9)
    for t in grid(-20, cm.DONE_TOTAL_MS + 20, 3):
        assert 0.0 <= cm.tint_alpha(t) <= 1.0


def test_t5_flash_extra_single_pulse(cm):
    assert cm.flash_extra(0) == pytest.approx(0.0, abs=1e-9)
    assert cm.flash_extra(-5) == 0
    for t in (cm.FLASH_MS, cm.FLASH_MS + 1, 10_000):
        assert cm.flash_extra(t) == pytest.approx(0.0, abs=1e-9)
    assert cm.flash_extra(cm.FLASH_MS / 2) == pytest.approx(cm.FLASH_EXTRA_PX)
    values = [cm.flash_extra(t) for t in grid(0, cm.FLASH_MS, 5)]
    assert all(0.0 <= v <= cm.FLASH_EXTRA_PX + 1e-9 for v in values)
    peak = values.index(max(values))
    assert all(a <= b + 1e-12 for a, b in itertools.pairwise(values[: peak + 1])), "lên"
    assert all(a >= b - 1e-12 for a, b in itertools.pairwise(values[peak:])), "xuống"


# =================================================================================================
# T6 ProgressTween
# =================================================================================================


def first_tween(old: float, now: float):
    """Tween đầu theo spec 3.2: `start` = giá trị cũ của thanh, `last_update = now − PROGRESS_MIN_MS`."""
    from teleport.ui import chat_motion as cm

    return cm.ProgressTween(
        start=old, target=old, t0=now, duration=cm.PROGRESS_MIN_MS, last_update=now - cm.PROGRESS_MIN_MS
    )


def test_t6_first_retarget_runs_progress_min_ms(cm):
    tw = first_tween(100, T0)
    tw.retarget(500, T0)
    assert (tw.start, tw.target, tw.t0, tw.duration, tw.last_update) == (100, 500, T0, cm.PROGRESS_MIN_MS, T0)
    assert tw.value_at(T0) == pytest.approx(100)
    assert tw.value_at(T0 + cm.PROGRESS_MIN_MS / 2) == pytest.approx(300)
    assert tw.value_at(T0 + cm.PROGRESS_MIN_MS) == pytest.approx(500)
    assert tw.active(T0 + 1)
    assert not tw.active(T0 + cm.PROGRESS_MIN_MS)


@pytest.mark.parametrize(
    ("gap", "want"),
    [(0, 200), (50, 200), (200, 200), (650, 650), (1000, 1000), (1001, 1000), (60_000, 1000)],
)
def test_t6_duration_is_gap_clamped(cm, gap, want):
    tw = first_tween(0, T0)
    tw.retarget(100, T0)
    tw.retarget(200, T0 + gap)
    assert tw.duration == want
    assert cm.PROGRESS_MIN_MS <= tw.duration <= cm.PROGRESS_MAX_MS


def test_t6_retarget_without_increase_only_updates_last_update(cm):
    tw = first_tween(0, T0)
    tw.retarget(400, T0)
    before = (tw.start, tw.target, tw.t0, tw.duration)
    tw.retarget(400, T0 + 300)
    assert (tw.start, tw.target, tw.t0, tw.duration) == before
    assert tw.last_update == T0 + 300
    tw.retarget(100, T0 + 700)  # số nhỏ hơn: không lùi
    assert (tw.start, tw.target, tw.t0, tw.duration) == before
    assert tw.last_update == T0 + 700
    tw.retarget(900, T0 + 1000)
    assert tw.duration == 300, "gap tính từ last_update mới nhất (T0 + 700)"


def test_t6_retarget_mid_way_starts_from_shown_value(cm):
    tw = first_tween(0, T0)
    tw.retarget(1000, T0)  # 200 ms tới 1000
    shown = tw.value_at(T0 + 100)
    assert shown == pytest.approx(500)
    tw.retarget(1000 + 0, T0 + 100)  # không tăng
    tw.retarget(1500, T0 + 150)
    assert tw.start == pytest.approx(tw.value_at(T0 + 150))
    assert tw.start >= shown
    assert tw.target == 1500


def test_t6_retarget_mid_tween_does_not_skip_ahead(cm):
    """`start = value_at(now)` là giá trị **đang hiện** (tính theo `duration` cũ), không phải giá trị
    tính lại theo `duration` mới: báo sớm giữa một tween dài không được làm thanh nhảy cóc tới đích."""
    tw = first_tween(0, T0)
    tw.retarget(100, T0)
    tw.retarget(1000, T0 + 1000)  # gap 1000 → tween 100 → 1000 trong 1000 ms
    assert tw.duration == 1000
    shown = tw.value_at(T0 + 1300)
    assert shown == pytest.approx(100 + 900 * 0.3)
    tw.retarget(1200, T0 + 1300)  # gap 300 → tween mới 300 ms
    assert tw.duration == 300
    assert tw.start == pytest.approx(shown), "start phải là giá trị đang hiện"
    assert tw.value_at(T0 + 1300) == pytest.approx(shown), "thanh nhảy khi retarget"


_steps = st.lists(
    st.tuples(st.integers(min_value=0, max_value=3000), st.integers(min_value=0, max_value=1000)),
    min_size=1,
    max_size=30,
)


@settings(max_examples=35, deadline=None)
@given(st.integers(min_value=0, max_value=1000), _steps)
def test_t6_property_never_overshoots_never_goes_back_and_lands(old, steps):
    """I1 `value_at ≤ target ≤ real`; I2 không giảm theo thời gian kể cả khi retarget giữa chừng;
    I3 hết `duration` thì bằng `target`; `duration` luôn trong [MIN, MAX]. `real` là số thật lớn nhất
    đã báo (thanh không lùi); dãy có cả báo dồn (dt 0), trễ (dt lớn) và không tăng."""
    from teleport.ui import chat_motion as cm

    now = T0
    tw = first_tween(old, now)
    real = old
    shown = tw.value_at(now)
    for dt, value in steps:
        # Lấy mẫu giữa hai lần báo: không giảm, không vượt.
        for k in range(1, 5):
            t = now + dt * k / 4
            v = tw.value_at(t)
            assert v >= shown - 1e-9, "I2"
            assert v <= tw.target + 1e-9 <= real + 2e-9, "I1"
            shown = v
        now += dt
        before = tw.value_at(now)
        real = max(real, value)
        tw.retarget(value, now)
        after = tw.value_at(now)
        assert after == pytest.approx(before, abs=1e-9), "retarget không làm nhảy giá trị đang hiện"
        assert tw.target == real
        assert tw.last_update == now
        assert cm.PROGRESS_MIN_MS <= tw.duration <= cm.PROGRESS_MAX_MS
        assert after <= tw.target + 1e-9 <= real + 2e-9, "I1"
        shown = after
    end = tw.t0 + tw.duration
    assert tw.value_at(end) == pytest.approx(tw.target), "I3"
    assert tw.value_at(end + 5000) == pytest.approx(tw.target), "I3"
    assert not tw.active(end)


# =================================================================================================
# Gán `motion` (mục 3.7, 11.8)
# =================================================================================================


def test_bubbles_get_thread_motion_and_lose_it_on_remove(h, tmp_path):
    thread = h.thread
    b = h.view.row(key("send", 900))
    assert b.motion is thread.motion
    token = h.view.add_pending(PEER_ID, [tmp_path / "a.bin"])
    pending = h.view.pending_bubbles()[-1]
    assert pending.motion is thread.motion
    note = h.view.show_clipboard(PEER_ID, ok=True, kind="text", size=3, preview="abc")
    assert getattr(note, "motion", None) is None, "chỉ `_Bubble` được gán motion (11.8)"
    h.view.remove_pending(token)
    assert pending.motion is None
    h.view.remove(key("send", 900))
    assert b.motion is None


# =================================================================================================
# T8 tức thì: trạng thái cuối ngay
# =================================================================================================


def test_t8_instant_new_receive_bubble_has_no_state_or_effect(h):
    from teleport.ui import motion

    k = key("receive", 1)
    h.view.handle(make_offer(k))
    settle()
    b = h.view.row(k)
    assert_still(h.thread, b)
    assert motion.running_count() == 0


def test_t8_instant_progress_sets_real_value_without_tween(h):
    from teleport.ui import motion

    b = h.new_send(1)
    h.view.handle(progress(key("send", 1), 500 * MiB))
    assert b.progress_bar.value() == 500
    assert bar_state(h.thread, b) is None
    assert_still(h.thread, b)
    hook = getattr(b.progress_bar, "display_fraction", None)
    if hook is not None:
        value = hook()
        assert value is None or value == pytest.approx(0.5)
    assert motion.running_count() == 0


def test_t8_instant_done_shows_checkmark_now(h):
    from teleport.ui import motion

    b = h.new_send(1)
    h.view.handle(finished(key("send", 1), "done"))
    assert (b.status_icon.icon_name, b.status_icon.token) == ("checkmark", "success")
    assert_still(h.thread, b)
    color, extra = b.motion_paint()
    assert color is None
    assert extra == 0
    sweep = b.status_icon.sweep
    assert sweep is not None, "_Bubble gán sweep cho status_icon (11.9)"
    f = sweep()
    assert f is None or f >= 1
    assert motion.running_count() == 0


def test_t8_instant_error_shows_warning_and_danger_now(h):
    from teleport.ui import motion
    from teleport.ui.widgets import BubbleEmphasis

    b = h.new_send(1)
    h.view.handle(finished(key("send", 1), "disk_failed"))
    assert b.emphasis is BubbleEmphasis.DANGER
    assert (b.status_icon.icon_name, b.status_icon.token) == ("warning", "danger")
    assert_still(h.thread, b)
    assert motion.running_count() == 0


def test_t8_instant_offer_has_attention_and_no_flash(h):
    from teleport.ui import motion
    from teleport.ui.widgets import BubbleEmphasis

    b = h.offer(key("receive", 1))
    assert b.offer_panel is not None
    assert b.emphasis is BubbleEmphasis.ATTENTION
    assert_still(h.thread, b)
    assert motion.running_count() == 0


# =================================================================================================
# T9 mờ dần bong bóng nhận mới
# =================================================================================================


def test_t9_new_receive_bubble_fades_in_then_settles(h, animate):
    from teleport.ui import motion

    clock = animate()
    k = key("receive", 1)
    h.view.handle(make_offer(k))
    b = h.view.row(k)
    thread = h.thread
    assert field(thread, b, "reveal_at") == T0
    wrapper = wrapper_of(b)
    assert wrapper is not None
    assert wrapper.graphicsEffect() is not None, "hiệu ứng gắn lên `_Item.wrapper` (11.2)"
    assert len(effects_of(b)) == 1, "một hiệu ứng duy nhất, không gắn lên bong bóng"
    assert b.graphicsEffect() is None
    assert motion.running_count() == 1, "driver đang chạy"
    clock.t = T0 + motion.NORMAL.ms / 2
    assert thread.motion.tick(clock.t) is True
    assert wrapper.graphicsEffect() is not None
    clock.t = T0 + motion.NORMAL.ms + 1
    assert thread.motion.tick(clock.t) is False
    assert wrapper.graphicsEffect() is None
    assert_still(thread, b)
    assert_idle(thread)


def test_t9_effect_paints_mid_reveal_and_mid_shake(h, animate):
    """Đường vẽ thật của hiệu ứng (11.2: thăm dò `sourcePixmap`): vẽ giữa chừng không ném (pytest-qt bắt
    lỗi trong hàm ảo). Không kiểm điểm ảnh."""
    from teleport.ui import motion

    clock = animate()
    k = key("receive", 1)
    h.view.handle(make_offer(k))
    e = h.new_send(2)
    h.view.handle(finished(key("send", 2), "disk_failed"))
    clock.t = T0 + 60
    h.thread.motion.tick(clock.t)
    assert wrapper_of(h.view.row(k)).graphicsEffect() is not None
    assert wrapper_of(e).graphicsEffect() is not None
    image = h.host.grab()
    assert not image.isNull()
    clock.t = T0 + motion.SLOW.ms + 1
    h.thread.motion.tick(clock.t)
    assert wrapper_of(e).graphicsEffect() is None


# =================================================================================================
# T10 lịch sử, ảnh chụp, mở luồng: không hoạt ảnh
# =================================================================================================


def test_t10_load_history_never_animates(h, animate, qtbot):
    from teleport.ui import motion
    from teleport.ui.chat import HistoryState

    animate()
    with qtbot.waitSignal(h.view.history_needed, timeout=3000):
        h.view.set_device(OTHER_ID)
    thread = h.view.thread_for(OTHER_ID)
    assert thread.history_state is HistoryState.LOADING
    h.view.load_history(OTHER_ID, [hentry(i) for i in range(1, 6)])
    settle()
    bubbles = thread.history_bubbles()
    assert len(bubbles) == 5
    for b in bubbles:
        assert b.motion is thread.motion, "add_many cũng gán motion"
        assert_still(thread, b)
    assert motion.running_count() == 0


def test_t10_load_snapshot_never_animates_or_tweens(h, animate):
    from teleport.ui import motion

    animate()
    k = key("receive", 7)
    h.view.load_snapshot([info(k, done=300 * MiB)])
    settle()
    b = h.view.row(k)
    assert b is not None
    assert b.progress_bar.value() == 300
    assert bar_state(h.thread, b) is None, "load_snapshot không tween (11.3)"
    assert_still(h.thread, b)
    assert motion.running_count() == 0


def test_t10_opening_thread_does_not_animate_bubbles_that_arrived_while_closed(h, animate):
    from teleport.ui import motion

    animate()
    k = key("receive", 3, OTHER_ID)
    h.view.handle(make_offer(k))
    h.view.handle(progress(k, 400 * MiB))
    settle()
    b = h.view.row(k)
    other = h.view.thread_for(OTHER_ID)
    assert_still(other, b)
    h.view.set_device(OTHER_ID)
    settle()
    assert other.isVisible()
    assert_still(other, b)
    assert bar_state(other, b) is None
    assert motion.running_count() == 0


# =================================================================================================
# T11 không mờ dần khi luồng không mở, view ẩn, cửa sổ thu nhỏ, không ghim
# =================================================================================================


@pytest.mark.parametrize("case", ["thread_closed", "view_hidden", "window_minimized"])
def test_t11_no_reveal_when_not_on_screen(h, animate, case):
    from teleport.ui import motion

    device = PEER_ID
    if case == "thread_closed":
        device = OTHER_ID
    elif case == "view_hidden":
        h.view.hide()
    else:
        h.host.setWindowState(h.host.windowState() | Qt.WindowState.WindowMinimized)
        assert h.host.isMinimized()
    settle()
    animate()
    k = key("receive", 1, device)
    h.view.handle(make_offer(k))
    settle()
    b = h.view.row(k)
    thread = h.view.thread_for(device)
    assert_still(thread, b)
    assert motion.running_count() == 0


def test_t11_no_reveal_when_not_pinned_but_unseen_counts(h, animate, qtbot):
    from teleport.ui import motion

    for i in range(14):
        k = key("send", 100 + i)
        h.view.handle(status_changed(k))
        h.view.handle(finished(k, "done"))
    settle()
    thread = h.thread
    bar = thread.scroll_area.verticalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > 0, timeout=2000)
    bar.setValue(0)
    settle()
    assert not thread.is_pinned()
    before = thread.unseen()
    animate()
    k = key("receive", 1)
    h.view.handle(make_offer(k))
    settle()
    assert thread.unseen() == before + 1, "vẫn đếm “↓ Mới”"
    assert_still(thread, h.view.row(k))
    assert motion.running_count() == 0


# =================================================================================================
# T12 SEND và bong bóng tạm không mờ dần; note clipboard có (11.10)
# =================================================================================================


def test_t12_new_send_bubble_does_not_fade(h, animate):
    from teleport.ui import motion

    animate()
    b = h.new_send(1)
    assert_still(h.thread, b)
    assert motion.running_count() == 0


def test_t12_pending_bubble_does_not_fade(h, animate, tmp_path):
    from teleport.ui import motion

    animate()
    h.view.add_pending(PEER_ID, [tmp_path / "a.bin"])
    settle()
    pending = h.view.pending_bubbles()[-1]
    assert_still(h.thread, pending)
    assert motion.running_count() == 0


def test_t12_clipboard_note_fades_in(h, animate):
    from teleport.ui import motion

    clock = animate()
    note = h.view.show_clipboard(PEER_ID, ok=True, kind="text", size=3, preview="abc")
    thread = h.thread
    assert field(thread, note, "reveal_at") == T0
    wrapper = wrapper_of(note)
    assert wrapper.graphicsEffect() is not None
    clock.t = T0 + motion.NORMAL.ms + 1
    thread.motion.tick(clock.t)
    assert_still(thread, note)
    assert_idle(thread)


def test_t12_replacing_clipboard_note_mid_fade_does_not_raise(h, animate):
    clock = animate()
    first = h.view.show_clipboard(PEER_ID, ok=True, kind="text", size=3, preview="abc")
    clock.t = T0 + 50
    h.thread.motion.tick(clock.t)
    second = h.view.show_clipboard(PEER_ID, ok=True, kind="text", size=3, preview="xyz")
    assert h.thread.motion.state(first) is None
    assert field(h.thread, second, "reveal_at") == T0 + 50
    clock.t = T0 + 1000
    h.thread.motion.tick(clock.t)
    settle()
    assert_idle(h.thread)


# =================================================================================================
# T13 tiến độ mượt, không vượt, không lùi
# =================================================================================================


def test_t13_progress_tween_stays_below_real_and_lands(h, animate):
    from teleport.ui import chat_motion as cm
    from teleport.ui import motion
    from teleport.ui.transfers_view import BAR_MAX

    b = h.new_send(1)
    k = key("send", 1)
    thread = h.thread
    clock = animate()
    bar = b.progress_bar
    assert bar.width() > 0
    h.view.handle(progress(k, 500 * MiB))
    assert bar.value() == 500, "value() là số thật ngay (trợ năng)"
    tw = bar_state(thread, b)
    assert tw is not None
    assert tw.target == 500
    assert tw.duration == cm.PROGRESS_MIN_MS, "tween đầu: last_update = now − MIN"
    assert tw.value_at(T0) == pytest.approx(0)
    assert motion.running_count() == 1
    clock.t = T0 + 100
    assert thread.motion.tick(clock.t) is True
    mid = bar_state(thread, b).value_at(clock.t)
    assert 0 < mid <= bar.value()
    hook = bar.display_fraction
    assert hook is not None, "BubbleBar.display_fraction được gán (11.9)"
    shown = hook()
    assert shown is not None
    assert shown == pytest.approx(mid / BAR_MAX)
    assert shown <= bar.value() / BAR_MAX
    clock.t = T0 + cm.PROGRESS_MIN_MS + 1
    thread.motion.tick(clock.t)
    tw = bar_state(thread, b)
    if tw is not None:  # tween được nhớ (11.6)
        assert tw.value_at(clock.t) == pytest.approx(500)
        assert not tw.active(clock.t)
    assert_idle(thread, states_cleared=False)
    assert bar.value() == 500


def test_t13_second_report_continues_from_shown_value_over_gap(h, animate):
    """11.6: tween (gồm `last_update`) được nhớ, nên báo sau 1 s chạy 1000 ms từ giá trị đang hiện."""
    from teleport.ui import chat_motion as cm
    from teleport.ui import motion

    b = h.new_send(1)
    k = key("send", 1)
    thread = h.thread
    clock = animate()
    h.view.handle(progress(k, 500 * MiB))
    clock.t = T0 + cm.PROGRESS_MIN_MS + 1
    thread.motion.tick(clock.t)
    clock.t = T0 + 1000
    h.view.handle(progress(k, 800 * MiB))
    assert b.progress_bar.value() == 800
    tw = bar_state(thread, b)
    assert tw is not None
    assert tw.start == pytest.approx(500)
    assert tw.target == 800
    assert tw.t0 == T0 + 1000
    assert tw.duration == 1000
    assert motion.running_count() == 1, "driver chạy lại"
    prev = 500.0
    for step in range(1, 11):
        clock.t = T0 + 1000 + step * 100
        thread.motion.tick(clock.t)
        tw = bar_state(thread, b)
        value = 800.0 if tw is None else tw.value_at(clock.t)
        assert prev - 1e-9 <= value <= 800 + 1e-9, "không lùi, không vượt"
        prev = value
    assert prev == pytest.approx(800)
    clock.t += 1
    thread.motion.tick(clock.t)
    assert_idle(thread, states_cleared=False)


def test_t13_tiny_step_is_set_directly(h, animate):
    """11.6: bước < 2 px không tween."""
    from teleport.ui import motion

    b = h.new_send(1)
    k = key("send", 1)
    thread = h.thread
    clock = animate()
    h.view.handle(progress(k, 500 * MiB))
    clock.t = T0 + 5000
    thread.motion.tick(clock.t)
    assert b.progress_bar.width() < 2000, "1/1000 thang < 2 px"
    h.view.handle(progress(k, 501 * MiB))
    assert b.progress_bar.value() == 501
    tw = bar_state(thread, b)
    assert tw is None or tw.value_at(clock.t) == pytest.approx(501)
    thread.motion.tick(clock.t)
    assert motion.running_count() == 0


def test_t13_no_tween_when_animations_disabled(h, animate, monkeypatch):
    from teleport.ui import motion

    b = h.new_send(1)
    animate()
    monkeypatch.setattr(motion, "animations_enabled", lambda: False)
    h.view.handle(progress(key("send", 1), 500 * MiB))
    assert b.progress_bar.value() == 500
    assert bar_state(h.thread, b) is None
    assert motion.running_count() == 0


def test_t13_finish_drops_bar_tween(h, animate):
    """11.7: `TransferBubble.finish` xóa tween thanh."""
    from teleport.ui import motion

    b = h.new_send(1)
    k = key("send", 1)
    clock = animate()
    h.view.handle(progress(k, 500 * MiB))
    assert bar_state(h.thread, b) is not None
    h.view.handle(finished(k, "cancelled_user"))
    assert bar_state(h.thread, b) is None
    h.thread.motion.tick(clock.t)
    assert motion.running_count() == 0


# =================================================================================================
# T14 xong / lỗi
# =================================================================================================


def test_t14_done_sweeps_check_and_tints_then_settles(h, animate):
    from teleport.ui import chat_motion as cm

    b = h.new_send(1)
    clock = animate()
    h.view.handle(finished(key("send", 1), "done"))
    thread = h.thread
    assert field(thread, b, "done_at") == T0
    assert (b.status_icon.icon_name, b.status_icon.token) == ("checkmark", "success"), "icon đúng ngay"
    f = b.status_icon.sweep()
    assert f is not None and 0 <= f < 1, "dấu tích đang quét"
    clock.t = T0 + 200  # giữa FAST và EMPHASIS: nền phủ đậm nhất
    color, _extra = b.motion_paint()
    assert color is not None
    assert color.alphaF() == pytest.approx(cm.TINT_MAX, abs=0.01)
    assert thread.motion.tick(clock.t) is True
    clock.t = T0 + cm.DONE_TOTAL_MS + 1
    thread.motion.tick(clock.t)
    assert_still(thread, b)
    assert_idle(thread)
    color, extra = b.motion_paint()
    assert color is None and extra == 0
    f = b.status_icon.sweep()
    assert f is None or f >= 1
    assert (b.status_icon.icon_name, b.status_icon.token) == ("checkmark", "success")


def test_t14_error_shakes_row_and_danger_is_immediate(h, animate):
    from teleport.ui import chat_motion as cm
    from teleport.ui.widgets import BubbleEmphasis

    b = h.new_send(1)
    clock = animate()
    h.view.handle(finished(key("send", 1), "disk_failed"))
    thread = h.thread
    assert b.emphasis is BubbleEmphasis.DANGER
    assert (b.status_icon.icon_name, b.status_icon.token) == ("warning", "danger")
    assert field(thread, b, "shake_at") == T0
    assert wrapper_of(b).graphicsEffect() is not None
    assert b.graphicsEffect() is None
    clock.t = T0 + cm.SHAKE_MS + 1
    thread.motion.tick(clock.t)
    assert_still(thread, b)
    assert_idle(thread)
    assert b.emphasis is BubbleEmphasis.DANGER


def test_t14_error_without_animations_has_no_shake(h, animate, monkeypatch):
    from teleport.ui import motion
    from teleport.ui.widgets import BubbleEmphasis

    b = h.new_send(1)
    animate()
    monkeypatch.setattr(motion, "animations_enabled", lambda: False)
    h.view.handle(finished(key("send", 1), "disk_failed"))
    assert b.emphasis is BubbleEmphasis.DANGER
    assert field(h.thread, b, "shake_at") is None
    assert_still(h.thread, b)
    assert motion.running_count() == 0


@pytest.mark.parametrize(
    ("result", "status"), [("cancelled_user", "transferring"), ("rejected_user", "waiting")]
)
def test_t14_cancel_or_reject_has_no_animation(h, animate, result, status):
    from teleport.ui import motion

    k = key("send", 1)
    h.view.handle(status_changed(k, status))
    b = h.view.row(k)
    clock = animate()
    h.view.handle(finished(k, result, status=status))
    assert_still(h.thread, b)
    h.thread.motion.tick(clock.t)
    assert motion.running_count() == 0


def test_t14_render_status_again_does_not_retrigger(h, animate):
    from teleport.ui import chat_motion as cm

    b = h.new_send(1)
    clock = animate()
    h.view.handle(finished(key("send", 1), "done"))
    clock.t = T0 + 300
    b.render_status()
    h.view.set_device_names({PEER_ID: "PC-Nha-2", OTHER_ID: OTHER})
    assert field(h.thread, b, "done_at") == T0, "render_status không đặt lại done_at"
    clock.t = T0 + cm.DONE_TOTAL_MS + 1
    h.thread.motion.tick(clock.t)
    assert_idle(h.thread)
    b.render_status()
    h.view.set_device_names({PEER_ID: PEER, OTHER_ID: OTHER})
    h.view.set_connected(PEER_ID, False)
    h.view.set_connected(PEER_ID, True)
    assert_still(h.thread, b)
    assert_idle(h.thread)


# =================================================================================================
# T15 lời mời nháy viền một lần
# =================================================================================================


def test_t15_offer_flash_waits_for_reveal_then_settles(h, animate):
    from teleport.ui import chat_motion as cm
    from teleport.ui import motion
    from teleport.ui.widgets import BubbleEmphasis

    clock = animate()
    b = h.offer(key("receive", 1))
    thread = h.thread
    reveal_at = field(thread, b, "reveal_at")
    flash_at = field(thread, b, "flash_at")
    assert reveal_at == T0
    assert flash_at is not None
    assert flash_at >= reveal_at + motion.NORMAL.ms
    assert flash_at == pytest.approx(max(T0, reveal_at + motion.NORMAL.ms))
    assert b.emphasis is BubbleEmphasis.ATTENTION, "viền nhấn ngay"
    clock.t = flash_at + cm.FLASH_MS / 2
    _color, extra = b.motion_paint()
    assert 0 < extra <= cm.FLASH_EXTRA_PX
    assert extra == pytest.approx(cm.FLASH_EXTRA_PX)
    thread.motion.tick(clock.t)
    clock.t = flash_at + cm.FLASH_MS + 1
    thread.motion.tick(clock.t)
    assert_still(thread, b)
    assert_idle(thread)
    _color, extra = b.motion_paint()
    assert extra == 0
    assert b.emphasis is BubbleEmphasis.ATTENTION


def test_t15_offer_on_settled_bubble_flashes_now(h, animate):
    from teleport.ui import motion

    clock = animate()
    k = key("receive", 1)
    h.view.handle(status_changed(k, "waiting"))
    b = h.view.row(k)
    clock.t = T0 + motion.NORMAL.ms + 50
    h.thread.motion.tick(clock.t)
    assert_still(h.thread, b)
    clock.t = T0 + 5000
    assert h.book.add(make_offer(k))
    assert field(h.thread, b, "flash_at") == T0 + 5000


def test_t15_offer_flashes_only_once(h, animate, tmp_path):
    from teleport.ui import chat_motion as cm

    clock = animate()
    k = key("receive", 1)
    b = h.offer(k)
    clock.t = T0 + 5000
    h.thread.motion.tick(clock.t)
    assert_still(h.thread, b)
    h.book.set_always(k, True)
    h.view.handle(make_offer(k))
    h.view.set_receive_dir(tmp_path / "khac")
    assert field(h.thread, b, "flash_at") is None
    assert b.set_offer(None) is True
    assert b.set_offer(h.book.entry(k), tmp_path / "khac", None) is True, "panel dựng lại"
    assert b.offer_panel is not None
    assert field(h.thread, b, "flash_at") is None, "cờ `_offer_flashed`: không nháy lần hai"
    clock.t += cm.FLASH_MS + 1
    h.thread.motion.tick(clock.t)
    assert_idle(h.thread)


def test_t15_offer_while_thread_closed_never_flashes_later(h, animate):
    from teleport.ui import motion

    clock = animate()
    k = key("receive", 1, OTHER_ID)
    h.view.handle(make_offer(k))
    assert h.book.add(make_offer(k))
    settle()
    b = h.view.row(k)
    other = h.view.thread_for(OTHER_ID)
    assert b.offer_panel is not None
    assert_still(other, b)
    clock.t = T0 + 100
    h.view.set_device(OTHER_ID)
    settle()
    h.book.set_always(k, True)
    settle()
    assert_still(other, b)
    assert motion.running_count() == 0


# =================================================================================================
# T16 / T17 dừng giữa chừng → trạng thái cuối
# =================================================================================================


def start_everything(h, clock):
    """Một cảnh đang chuyển động: mờ dần (nhận), tween thanh, dấu tích, rung. Trả các bong bóng."""
    recv = key("receive", 1)
    h.view.handle(make_offer(recv))
    s = h.new_send(2)
    d = h.new_send(3)
    e = h.new_send(4)
    clock.t = T0
    h.view.handle(progress(key("send", 2), 500 * MiB))
    h.view.handle(finished(key("send", 3), "done"))
    h.view.handle(finished(key("send", 4), "disk_failed"))
    r = h.view.row(recv)
    thread = h.thread
    assert field(thread, r, "reveal_at") is not None
    assert bar_state(thread, s) is not None
    assert field(thread, d, "done_at") is not None
    assert field(thread, e, "shake_at") is not None
    from teleport.ui import motion

    assert motion.running_count() == 1
    clock.t = T0 + 60
    thread.motion.tick(clock.t)
    return [r, s, d, e]


@pytest.mark.parametrize("case", ["switch_device", "hide_view", "hide_window", "minimize_window"])
def test_t16_leaving_mid_animation_jumps_to_end(h, animate, qtbot, case):
    from teleport.ui import motion

    clock = animate()
    bubbles = start_everything(h, clock)
    thread = h.thread
    if case == "switch_device":
        h.view.set_device(OTHER_ID)
    elif case == "hide_view":
        h.view.hide()
    elif case == "hide_window":
        h.host.hide()
    else:
        h.host.setWindowState(h.host.windowState() | Qt.WindowState.WindowMinimized)
        qtbot.waitUntil(lambda: motion.running_count() == 0, timeout=3000)
    settle()
    for b in bubbles:
        assert_still(thread, b)
        assert bar_state(thread, b) is None
    assert_idle(thread)


def test_t16_clear_mid_animation_does_not_raise(h, animate):
    from teleport.ui import motion

    clock = animate()
    start_everything(h, clock)
    old = h.thread
    h.view.clear()
    if is_alive(old):
        old.motion.tick(clock.t + 10)  # không ném dù luồng đã bị tách khỏi view
    settle()
    assert motion.running_count() == 0
    new = h.view.thread_for(PEER_ID)
    assert new is not None and new is not old


def test_t17_finish_all_mid_animation_jumps_to_end(h, animate):
    from teleport.ui import motion

    clock = animate()
    bubbles = start_everything(h, clock)
    total = motion.finish_all()
    assert total >= 1
    thread = h.thread
    for b in bubbles:
        assert_still(thread, b)
        assert bar_state(thread, b) is None
    assert_idle(thread)
    assert bubbles[1].progress_bar.value() == 500, "số thật giữ nguyên"


# =================================================================================================
# T18 hình học không đổi (hồi quy, 11.1)
# =================================================================================================


def test_t18_geometry_unchanged(h, qtbot):
    from teleport.ui import theme
    from teleport.ui.widgets import BUBBLE_MAX_RATIO

    margin = theme.SPACING[3]
    out = h.view.row(key("send", 900))
    k_in = key("receive", 1)
    h.view.handle(status_changed(k_in))
    note = h.view.show_clipboard(PEER_ID, ok=True, kind="text", size=3, preview="abc")
    h.thread.set_history_failed()
    settle()
    inc = h.view.row(k_in)
    container = wrapper_of(out).parentWidget()
    assert container.objectName() == "chatThreadContent"
    width = container.width()
    qtbot.waitUntil(
        lambda: out.maximumWidth() == round(out.parentWidget().width() * BUBBLE_MAX_RATIO), timeout=2000
    )
    for b in (out, inc):
        row = b.parentWidget()
        assert row.width() == width - 2 * margin
        assert b.maximumWidth() == round(row.width() * BUBBLE_MAX_RATIO)
    out_right = out.mapTo(container, out.rect().topLeft()).x() + out.width()
    assert abs(out_right - (width - margin)) <= 1, (out_right, width)
    assert inc.mapTo(container, inc.rect().topLeft()).x() == margin
    assert note.mapTo(container, note.rect().topLeft()).x() == margin
    separator = h.thread.separators()[0]
    assert separator.mapTo(container, separator.rect().topLeft()).x() == margin
    assert separator.width() == width - 2 * margin
    hist = h.thread.history_note
    assert hist.mapTo(container, hist.rect().topLeft()).x() == margin


# =================================================================================================
# T19 tiêu điểm, trợ năng không chờ hoạt ảnh
# =================================================================================================


def test_t19_focus_and_accessible_name_are_immediate_during_reveal(h, animate):
    animate()
    k = key("receive", 1)
    h.view.handle(make_offer(k))
    b = h.view.row(k)
    assert field(h.thread, b, "reveal_at") is not None
    b.setFocus(Qt.FocusReason.OtherFocusReason)
    assert b.hasFocus()
    assert b.accessibleName()


# =================================================================================================
# T20 N3: không còn gì chạy sau mọi kịch bản
# =================================================================================================


def test_t20_full_scenario_leaves_nothing_running(h, animate):
    from teleport.ui import motion

    clock = animate()
    bubbles = start_everything(h, clock)
    offer_bubble = h.offer(key("receive", 9))
    note = h.view.show_clipboard(PEER_ID, ok=True, kind="text", size=3, preview="abc")
    h.view.handle(progress(key("send", 2), 900 * MiB))
    results = []
    for step in range(0, 61):
        clock.t = T0 + step * 50
        results.append(h.thread.motion.tick(clock.t))
        assert isinstance(results[-1], bool)
    for b in [*bubbles, offer_bubble, note]:
        assert effects_of(b) == []
    assert motion.running_count() == 0
    assert_idle(h.thread, states_cleared=False)


# =================================================================================================
# T21 gỡ giữa chừng
# =================================================================================================


def test_t21_remove_mid_reveal_clears_state(h, animate):
    clock = animate()
    k = key("receive", 1)
    h.view.handle(make_offer(k))
    b = h.view.row(k)
    assert field(h.thread, b, "reveal_at") is not None
    wrapper = wrapper_of(b)
    h.view.remove(k)
    assert h.thread.motion.state(b) is None
    if is_alive(wrapper):
        assert h.thread.motion.state(wrapper) is None
    assert b.motion is None
    clock.t = T0 + 50
    h.thread.motion.tick(clock.t)
    settle()
    clock.t = T0 + 5000
    h.thread.motion.tick(clock.t)
    assert_idle(h.thread)


def test_t21_trim_mid_done_clears_state(h, animate, monkeypatch):
    from teleport.ui import chat

    monkeypatch.setattr(chat, "MAX_THREAD_BUBBLES", 3)
    sends = [h.new_send(10 + i) for i in range(2)]
    clock = animate()
    for i in range(2):
        h.view.handle(finished(key("send", 10 + i), "done"))
    more = [h.new_send(20 + i) for i in range(3)]
    for i in range(3):
        h.view.handle(finished(key("send", 20 + i), "done"))
    kept = set(h.thread.bubbles())
    removed = [b for b in [*sends, *more] if b not in kept]
    assert removed, "trim phải gỡ bong bóng cũ"
    for b in removed:
        assert h.thread.motion.state(b) is None
    h.thread.motion.tick(clock.t + 10)
    settle()
    clock.t = T0 + 5000
    h.thread.motion.tick(clock.t)
    assert_idle(h.thread)


def test_t21_remove_pending_mid_reveal_does_not_raise(h, animate, tmp_path):
    clock = animate()
    k = key("receive", 1)
    h.view.handle(make_offer(k))
    token = h.view.add_pending(PEER_ID, [tmp_path / "a.bin"])
    pending = h.view.pending_bubbles()[-1]
    h.view.remove_pending(token)
    assert h.thread.motion.state(pending) is None
    clock.t = T0 + 50
    h.thread.motion.tick(clock.t)
    assert field(h.thread, h.view.row(k), "reveal_at") == T0, "mờ dần của bong bóng khác chạy tiếp"
    settle()
    clock.t = T0 + 5000
    h.thread.motion.tick(clock.t)
    assert_idle(h.thread)


def test_t21_forget_unknown_widget_is_harmless(h, animate):
    animate()
    stray = QWidget()
    h.thread.motion.forget(stray)
    assert h.thread.motion.state(stray) is None
    h.thread.motion.finish()
    assert_idle(h.thread)
    stray.deleteLater()
