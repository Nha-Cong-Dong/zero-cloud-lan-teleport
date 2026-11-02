"""`ui/placement.py` và phần cửa sổ của nhớ trạng thái (`specs/spec-f1.md` D6, D7, R8–R14).

Viết từ spec, không từ code (module chưa có khi viết test).

- `fit_window(saved, screens, min_size, title_allowance)` (R9, R10, R12) thuần, trả
  `(tên màn hình đích, x, y, w, h)` hoặc None. Luật 1–4 của D6, với `avail` đã co mép trên đi
  `title_allowance`. Tiêu chí 4: cửa sổ không bao giờ ra ngoài vùng khả dụng (đã co) của màn hình đích.
- `screens_now()`: màn hình chính đứng đầu.
- `capture(window, fallback=None)` (D6, R8, R11): không bao giờ raise; tên màn hình lấy từ
  `QGuiApplication.primaryScreen().name()` (nền `offscreen` trả chuỗi rỗng → `screen=None` theo R8).
- `restore(window, saved)` (D6, R11, R13): `setGeometry` trước `setWindowState(WindowMaximized)`; không
  hiện cửa sổ; restore → show → capture không trôi.
- `MainWindow.shown_once` (D7); `MainWindow.bring_to_front` giữ phóng to khi đang thu nhỏ (R11).

Giả định API: `ScreenArea(name, x, y, width, height, dpr)` theo thứ tự trong spec; `TITLE_ALLOWANCE` là
hằng của `placement`; `capture` nhận `fallback` bằng từ khóa, mặc định None; `restore` đọc màn hình qua
`placement.screens_now` (seam cấp module) và dùng `main_window.MIN_SIZE`.
"""

from __future__ import annotations

import logging
import math

import pytest

pytest.importorskip("PySide6")

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QGuiApplication

from teleport.app.settings import WindowGeometry

MIN = (720, 460)
TA = 24  # R9: TITLE_ALLOWANCE


def area(name="DP-1", x=0, y=0, width=1920, height=1040, dpr=1.0):
    from teleport.ui.placement import ScreenArea

    return ScreenArea(name, x, y, width, height, dpr)


def wg(x=100, y=100, width=1000, height=700, *, maximized=False, screen="DP-1", dpr=1.0):
    return WindowGeometry(x, y, width, height, maximized=maximized, screen=screen, dpr=dpr)


def fit(saved, screens, min_size=MIN, title_allowance=TA):
    from teleport.ui.placement import fit_window

    return fit_window(saved, screens, min_size, title_allowance)


def inside(rect, a, ta=TA) -> bool:
    """`rect = (x, y, w, h)` nằm trọn trong vùng khả dụng của `a` đã co mép trên đi `ta`."""
    x, y, w, h = rect
    return x >= a.x and y >= a.y + ta and x + w <= a.x + a.width and y + h <= a.y + a.height


# =================================================================================================
# ScreenArea, TITLE_ALLOWANCE
# =================================================================================================


def test_screen_area_fields_and_frozen():
    import dataclasses

    a = area("HDMI-1", -1920, 10, 1920, 1050, 1.25)
    assert (a.name, a.x, a.y, a.width, a.height, a.dpr) == ("HDMI-1", -1920, 10, 1920, 1050, 1.25)
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.x = 0  # type: ignore[misc]


def test_title_allowance_constant():
    from teleport.ui import placement

    assert placement.TITLE_ALLOWANCE == 24


# =================================================================================================
# fit_window: luật 1–4, R9, R12
# =================================================================================================


def test_rule1_no_screens_returns_none():
    assert fit(wg(), []) is None


def test_rule2_same_screen_same_dpr_inside_returned_unchanged():
    assert fit(wg(100, 100, 1000, 700), [area()]) == ("DP-1", 100, 100, 1000, 700)


def test_rule2_dpr_within_tolerance_still_unchanged():
    assert fit(wg(dpr=1.009), [area(dpr=1.0)]) == ("DP-1", 100, 100, 1000, 700)
    assert fit(wg(dpr=1.5), [area(dpr=1.505)]) == ("DP-1", 100, 100, 1000, 700)


def test_rule2_keeps_size_below_min_size_when_it_matches():
    """Luật 2 trả nguyên, kể cả cỡ nhỏ hơn `min_size` (khác luật 3: luật 3 nới ra `min`)."""
    assert fit(wg(50, 60, 500, 300), [area()]) == ("DP-1", 50, 60, 500, 300)


def test_dpr_changed_goes_to_rule3_and_enforces_min_size():
    """DPI đổi (> 0.01) → luật 3: cỡ nhỏ hơn `min` bị nới ra `min` (phân biệt được với luật 2)."""
    assert fit(wg(50, 60, 500, 300, dpr=1.0), [area(dpr=1.25)]) == ("DP-1", 50, 60, 720, 460)
    assert fit(wg(50, 60, 500, 300, dpr=1.0), [area(dpr=1.02)]) == ("DP-1", 50, 60, 720, 460)


@pytest.mark.parametrize("kw", [{"screen": None}, {"dpr": None}, {"screen": None, "dpr": None}])
def test_rule4_missing_screen_or_dpr_is_treated_as_mismatch(kw):
    assert fit(wg(50, 60, 500, 300, **kw), [area()]) == ("DP-1", 50, 60, 720, 460)


def test_rule2_rect_touching_edges_of_shrunk_area_is_inside():
    a = area(x=0, y=0, width=1920, height=1040)
    saved = wg(0, TA, 1920, 1040 - TA)
    assert fit(saved, [a]) == ("DP-1", 0, TA, 1920, 1040 - TA)


def test_r9_top_edge_under_title_allowance_is_moved_down():
    """R9: mép trên ở trong dải thanh tiêu đề → không “nằm trọn” → luật 3 đẩy xuống `avail.y + TA`."""
    assert fit(wg(100, TA - 1, 1000, 700), [area()]) == ("DP-1", 100, TA, 1000, 700)
    assert fit(wg(100, 0, 1000, 700), [area()]) == ("DP-1", 100, TA, 1000, 700)
    assert fit(wg(100, -500, 1000, 700), [area(y=0)]) == ("DP-1", 100, TA, 1000, 700)


def test_r9_title_allowance_parameter_is_used():
    assert fit(wg(100, 10, 1000, 700), [area()], title_allowance=0) == ("DP-1", 100, 10, 1000, 700)
    assert fit(wg(100, 10, 1000, 700), [area()], title_allowance=40) == ("DP-1", 100, 40, 1000, 700)


def test_r9_height_limited_to_area_minus_title_allowance():
    a = area(x=0, y=0, width=1920, height=1040)
    assert fit(wg(0, 0, 1920, 1040), [a]) == ("DP-1", 0, TA, 1920, 1040 - TA)


def test_rule3_too_large_shrunk_to_area():
    a = area(x=0, y=0, width=1366, height=728)
    assert fit(wg(-40, -40, 3000, 2000), [a]) == ("DP-1", 0, TA, 1366, 728 - TA)


def test_rule3_overflowing_right_bottom_moved_inside_same_size():
    a = area(x=0, y=0, width=1920, height=1040)
    assert fit(wg(1500, 900, 1000, 700), [a]) == ("DP-1", 920, 340, 1000, 700)


def test_rule3_screen_with_negative_origin():
    left = area("DP-2", x=-1920, y=-200, width=1920, height=1080)
    assert fit(wg(-2500, -400, 1000, 700, screen="DP-2"), [area(), left]) == (
        "DP-2",
        -1920,
        -200 + TA,
        1000,
        700,
    )


def test_rule3_target_is_same_name_even_if_center_on_other_screen():
    main = area("DP-1", 0, 0, 1920, 1040)
    second = area("DP-2", 1920, 0, 1920, 1040)
    saved = wg(100, 100, 1000, 700, screen="DP-2", dpr=2.0)  # dpr khác → luật 3; tâm nằm ở DP-1
    assert fit(saved, [main, second]) == ("DP-2", 1920, 100, 1000, 700)


def test_rule3_unknown_name_uses_screen_containing_center():
    main = area("DP-1", 0, 0, 1920, 1040)
    second = area("DP-2", 1920, 0, 2560, 1400, dpr=1.5)
    saved = wg(2200, 200, 1000, 700, screen="DA-THAO", dpr=1.5)
    assert fit(saved, [main, second]) == ("DP-2", 2200, 200, 1000, 700)


def test_rule3_lost_screen_falls_back_to_first_screen():
    """Màn hình đã lưu bị rút, tâm không thuộc màn hình nào → `screens[0]` (màn hình chính)."""
    main = area("DP-1", 0, 0, 1920, 1040)
    second = area("DP-2", -1280, 0, 1280, 1000)
    saved = wg(5000, 300, 1000, 700, screen="HDMI-9")
    assert fit(saved, [main, second]) == ("DP-1", 920, 300, 1000, 700)


def test_r12_area_smaller_than_min_size_lower_bound_wins():
    """R12: vùng khả dụng nhỏ hơn `min_size` → cỡ bằng vùng, góc trên trái ở `avail.topLeft` (+ TA)."""
    tiny = area("DP-1", 100, 50, 600, 400)
    for saved in (wg(0, 0, 1000, 700), wg(5000, 5000, 1000, 700), wg(-5000, -5000, 300, 200)):
        assert fit(saved, [tiny]) == ("DP-1", 100, 50 + TA, 600, 400 - TA)


def test_r12_only_width_smaller_than_min():
    narrow = area("DP-1", 0, 0, 640, 1040)
    assert fit(wg(300, 100, 1000, 700), [narrow]) == ("DP-1", 0, 100, 640, 700)


def test_rule3_min_size_applied_when_area_is_big_enough():
    assert fit(wg(10, 30, 100, 100, screen=None), [area()]) == ("DP-1", 10, 30, 720, 460)


def test_fit_window_is_pure():
    saved = wg(5000, 5000)
    screens = [area(), area("DP-2", 1920, 0)]
    before = list(screens)
    fit(saved, screens)
    assert screens == before
    assert saved == wg(5000, 5000)


# --- Ngẫu nhiên: tiêu chí 4 -----------------------------------------------------------------------

_NAMES = ["DP-1", "DP-2", "HDMI-1"]
_DPRS = [1.0, 1.25, 1.5, 2.0]


@st.composite
def _screens(draw):
    from teleport.ui.placement import ScreenArea

    names = draw(st.lists(st.sampled_from(_NAMES), min_size=1, max_size=3, unique=True))
    return [
        ScreenArea(
            name,
            draw(st.integers(-5000, 5000)),
            draw(st.integers(-5000, 5000)),
            draw(st.integers(100, 4000)),
            draw(st.integers(TA + 50, 3000)),
            draw(st.sampled_from(_DPRS)),
        )
        for name in names
    ]


_SAVED = st.builds(
    WindowGeometry,
    x=st.integers(-100_000, 100_000),
    y=st.integers(-100_000, 100_000),
    width=st.integers(1, 100_000),
    height=st.integers(1, 100_000),
    maximized=st.booleans(),
    screen=st.sampled_from([None, "KHONG-CO", *_NAMES]),
    dpr=st.sampled_from([None, *_DPRS]),
)


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_SAVED, _screens(), st.integers(1, 1000), st.integers(1, 800))
def test_result_always_inside_target_area(saved, screens, min_w, min_h):
    result = fit(saved, screens, (min_w, min_h))
    assert result is not None
    name, x, y, w, h = result
    by_name = {s.name: s for s in screens}
    assert name in by_name
    target = by_name[name]
    if saved.screen in by_name:
        assert name == saved.screen, "luật 3: ưu tiên màn hình cùng tên"
    assert w >= 1 and h >= 1
    assert inside((x, y, w, h), target), (result, target)
    if (x, y, w, h) != (saved.x, saved.y, saved.width, saved.height):
        assert w >= min(min_w, target.width)
        assert h >= min(min_h, target.height - TA)
    # Ổn định: kết quả, lưu lại với đúng màn hình/DPI đích, đi qua luật 2 và không đổi nữa.
    again = WindowGeometry(x, y, w, h, screen=name, dpr=target.dpr)
    assert fit(again, screens, (min_w, min_h)) == result


@settings(max_examples=200, deadline=None)
@given(_screens(), st.data())
def test_rule2_exact_match_never_moves(screens, data):
    target = data.draw(st.sampled_from(screens))
    w = data.draw(st.integers(1, target.width))
    h = data.draw(st.integers(1, target.height - TA))
    x = data.draw(st.integers(target.x, target.x + target.width - w))
    y = data.draw(st.integers(target.y + TA, target.y + target.height - h))
    saved = WindowGeometry(x, y, w, h, screen=target.name, dpr=target.dpr)
    assert fit(saved, screens) == (target.name, x, y, w, h)


# =================================================================================================
# screens_now
# =================================================================================================


def test_screens_now_primary_first_with_available_geometry(qapp):
    from teleport.ui.placement import ScreenArea, screens_now

    screens = screens_now()
    primary = QGuiApplication.primaryScreen()
    assert primary is not None
    assert screens and all(isinstance(s, ScreenArea) for s in screens)
    assert len(screens) == len(QGuiApplication.screens())
    first = screens[0]
    g = primary.availableGeometry()
    assert first.name == primary.name()
    assert (first.x, first.y, first.width, first.height) == (g.x(), g.y(), g.width(), g.height())
    assert first.dpr == pytest.approx(primary.devicePixelRatio())


# =================================================================================================
# capture (D6, R8, R11), shown_once (D7)
# =================================================================================================


def expected_screen() -> tuple[str | None, float | None]:
    """(screen, dpr) mà `capture` phải lưu cho màn hình chính (R8: tên sai luật → None)."""
    from teleport.app.settings import MAX_DPR, MAX_SCREEN_NAME_LEN, MIN_DPR

    primary = QGuiApplication.primaryScreen()
    name = primary.name()
    ok_name = name if 1 <= len(name) <= MAX_SCREEN_NAME_LEN and "\x00" not in name else None
    dpr = float(primary.devicePixelRatio())
    ok_dpr = dpr if math.isfinite(dpr) and MIN_DPR <= dpr <= MAX_DPR else None
    return ok_name, ok_dpr


@pytest.fixture
def window(qtbot):
    from teleport.ui.main_window import MainWindow

    w = MainWindow()
    qtbot.addWidget(w)
    return w


def show(qtbot, w) -> None:
    w.show()
    qtbot.waitUntil(w.isVisible, timeout=2000)
    qtbot.wait(10)


def avail_or_skip() -> QRect:
    g = QGuiApplication.primaryScreen().availableGeometry()
    if g.width() < 780 or g.height() < 600:
        pytest.skip("màn hình (offscreen) quá nhỏ cho ca này")
    return g


def saved_inside(*, maximized=False, screen=None, dpr=None) -> WindowGeometry:
    g = avail_or_skip()
    return WindowGeometry(g.x() + 30, g.y() + 60, 740, 500, maximized=maximized, screen=screen, dpr=dpr)


def test_shown_once_false_until_first_show_then_stays_true(window, qtbot):
    assert window.shown_once is False
    show(qtbot, window)
    assert window.shown_once is True
    window.hide()
    assert window.shown_once is True


def test_capture_never_shown_window_is_none(window):
    from teleport.ui.placement import capture

    window.setGeometry(QRect(10, 40, 800, 500))
    assert capture(window) is None
    assert capture(window, fallback=saved_inside()) is None


def test_capture_normal_window(window, qtbot):
    from teleport.ui.placement import capture

    g = avail_or_skip()
    window.setGeometry(QRect(g.x() + 20, g.y() + 50, 760, 520))
    show(qtbot, window)
    got = window.geometry()
    screen, dpr = expected_screen()
    assert capture(window) == WindowGeometry(
        got.x(), got.y(), got.width(), got.height(), maximized=False, screen=screen, dpr=dpr
    )


def test_capture_after_hide_keeps_last_geometry(window, qtbot):
    from teleport.ui.placement import capture

    show(qtbot, window)
    before = capture(window)
    window.hide()
    assert before is not None
    assert capture(window) == before


def test_capture_maximized_uses_normal_geometry(window, qtbot):
    from teleport.ui.placement import capture

    g = avail_or_skip()
    normal = QRect(g.x() + 20, g.y() + 50, 760, 520)
    window.setGeometry(normal)
    show(qtbot, window)
    window.setWindowState(Qt.WindowState.WindowMaximized)
    qtbot.waitUntil(lambda: bool(window.windowState() & Qt.WindowState.WindowMaximized), timeout=2000)
    got = capture(window)
    assert got is not None
    assert got.maximized is True
    n = window.normalGeometry()
    assert (got.x, got.y, got.width, got.height) == (n.x(), n.y(), n.width(), n.height())


def test_capture_minimized_saves_normal_geometry_not_minimized(window, qtbot):
    from teleport.ui.placement import capture

    g = avail_or_skip()
    window.setGeometry(QRect(g.x() + 20, g.y() + 50, 760, 520))
    show(qtbot, window)
    window.setWindowState(Qt.WindowState.WindowMinimized)
    qtbot.wait(20)
    got = capture(window)
    assert got is not None
    assert got.maximized is False
    n = window.normalGeometry()
    assert (got.x, got.y, got.width, got.height) == (n.x(), n.y(), n.width(), n.height())


def test_capture_minimized_from_maximized_keeps_maximized_flag(window, qtbot):
    from teleport.ui.placement import capture

    show(qtbot, window)
    window.setWindowState(Qt.WindowState.WindowMaximized)
    qtbot.wait(10)
    window.setWindowState(window.windowState() | Qt.WindowState.WindowMinimized)
    qtbot.wait(10)
    got = capture(window)
    assert got is not None and got.maximized is True


class FakeScreen:
    def __init__(self, name, dpr):
        self._name, self._dpr = name, dpr

    def name(self):
        return self._name

    def devicePixelRatio(self):
        return self._dpr


@pytest.mark.parametrize(
    ("name", "dpr", "want_screen", "want_dpr"),
    [
        ("", 1.0, None, 1.0),
        ("DP\x001", 1.0, None, 1.0),
        ("s" * 257, 1.0, None, 1.0),
        ("DP-1", 100.0, "DP-1", None),
        ("DP-1", 0.1, "DP-1", None),
        ("DP-1", math.nan, "DP-1", None),
        ("DP-1", math.inf, "DP-1", None),
        ("", math.nan, None, None),
    ],
    ids=["ten-rong", "ten-NUL", "ten-257", "dpr-100", "dpr-0.1", "dpr-NaN", "dpr-inf", "ca-hai"],
)
def test_r8_capture_bad_screen_name_or_dpr_becomes_none(
    window, qtbot, monkeypatch, name, dpr, want_screen, want_dpr
):
    from teleport.ui.placement import capture

    show(qtbot, window)
    monkeypatch.setattr(window, "screen", lambda: FakeScreen(name, dpr))
    got = capture(window)
    assert got is not None
    assert (got.screen, got.dpr) == (want_screen, want_dpr)
    g = window.geometry()
    assert (got.x, got.y, got.width, got.height) == (g.x(), g.y(), g.width(), g.height())


def test_r8_capture_invalid_geometry_returns_none_without_raising(window, qtbot, monkeypatch, caplog):
    """R8: dựng `WindowGeometry` vẫn lỗi (cỡ vượt `MAX_WINDOW_SIZE`) → None, chỉ log DEBUG."""
    from teleport.ui.placement import capture

    caplog.set_level(logging.DEBUG)
    show(qtbot, window)
    monkeypatch.setattr(window, "geometry", lambda: QRect(0, 0, 200_000, 500))
    assert capture(window) is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_r11_capture_maximized_with_invalid_normal_geometry_uses_fallback(window, qtbot, monkeypatch):
    from teleport.ui.placement import capture

    show(qtbot, window)
    window.setWindowState(Qt.WindowState.WindowMaximized)
    qtbot.wait(10)
    monkeypatch.setattr(window, "normalGeometry", lambda: QRect())
    fallback = WindowGeometry(11, 33, 900, 600, maximized=False, screen="DP-7", dpr=1.5)
    got = capture(window, fallback=fallback)
    assert got is not None
    assert (got.x, got.y, got.width, got.height) == (11, 33, 900, 600)
    assert got.maximized is True, "cờ phóng to là của cửa sổ hiện tại, không phải của mốc"


def test_r11_capture_invalid_normal_geometry_without_fallback_is_none(window, qtbot, monkeypatch):
    from teleport.ui.placement import capture

    show(qtbot, window)
    window.setWindowState(Qt.WindowState.WindowMaximized)
    qtbot.wait(10)
    monkeypatch.setattr(window, "normalGeometry", lambda: QRect())
    assert capture(window) is None
    assert capture(window, fallback=None) is None


# =================================================================================================
# restore (D6, R11, R13)
# =================================================================================================


def test_restore_none_changes_nothing(window):
    from teleport.ui.placement import restore

    before = window.geometry()
    state = window.windowState()
    restore(window, None)
    assert window.geometry() == before
    assert window.windowState() == state
    assert not window.isVisible()


def test_restore_sets_geometry_and_keeps_window_hidden(window):
    from teleport.ui.placement import restore

    saved = saved_inside()
    restore(window, saved)
    assert not window.isVisible(), "restore không được hiện cửa sổ (gọi khi cửa sổ còn ẩn)"
    g = window.geometry()
    assert (g.x(), g.y(), g.width(), g.height()) == (saved.x, saved.y, saved.width, saved.height)
    assert not window.windowState() & Qt.WindowState.WindowMaximized


def test_r13_restore_show_capture_does_not_drift(qtbot):
    """R13: restore → show → capture bằng đúng hình đã lưu; lặp hai vòng vẫn không trôi."""
    from teleport.ui import main_window, placement

    screen, dpr = expected_screen()
    saved = saved_inside(screen=screen, dpr=dpr)
    for _ in range(2):
        w = main_window.MainWindow()
        qtbot.addWidget(w)
        placement.restore(w, saved)
        show(qtbot, w)
        got = placement.capture(w)
        assert got == saved
        w.hide()
        saved = got


def test_r11_restore_maximized_then_show_normal_geometry_is_fitted(window, qtbot):
    from teleport.ui.placement import restore

    saved = saved_inside(maximized=True)
    restore(window, saved)
    assert window.windowState() & Qt.WindowState.WindowMaximized
    show(qtbot, window)
    assert window.windowState() & Qt.WindowState.WindowMaximized
    n = window.normalGeometry()
    assert (n.x(), n.y(), n.width(), n.height()) == (saved.x, saved.y, saved.width, saved.height)


def test_r11_restore_maximized_round_trip_through_capture(window, qtbot):
    from teleport.ui.placement import capture, restore

    screen, dpr = expected_screen()
    saved = saved_inside(maximized=True, screen=screen, dpr=dpr)
    restore(window, saved)
    show(qtbot, window)
    assert capture(window) == saved


def test_restore_off_screen_window_is_moved_inside_available_area(window):
    from teleport.ui.placement import restore

    g = avail_or_skip()
    restore(window, WindowGeometry(50_000, -50_000, 740, 500, screen="MAN-HINH-DA-RUT", dpr=1.0))
    r = window.geometry()
    assert r.x() >= g.x() and r.y() >= g.y() + TA
    assert r.x() + r.width() <= g.x() + g.width()
    assert r.y() + r.height() <= g.y() + g.height()


def test_restore_uses_screens_now_seam_and_min_size(window, monkeypatch):
    """`restore` = `fit_window(saved, screens_now(), MIN_SIZE, TITLE_ALLOWANCE)` → `setGeometry`."""
    from teleport.ui import main_window, placement

    name = QGuiApplication.primaryScreen().name() or "offscreen"
    fake = [placement.ScreenArea(name, 0, 0, 1000, 700, 1.0)]
    monkeypatch.setattr(placement, "screens_now", lambda: fake)
    min_w, min_h = main_window.MIN_SIZE.width(), main_window.MIN_SIZE.height()
    placement.restore(window, WindowGeometry(5000, 5000, 100, 100, screen="KHAC", dpr=1.0))
    g = window.geometry()
    assert (g.width(), g.height()) == (min_w, min_h)
    assert (g.x(), g.y()) == (1000 - min_w, 700 - min_h)


def test_restore_with_no_screens_keeps_default_size(window, monkeypatch):
    from teleport.ui import placement

    monkeypatch.setattr(placement, "screens_now", lambda: [])
    before = window.geometry()
    placement.restore(window, saved_inside())
    assert window.geometry().size() == before.size()


# =================================================================================================
# bring_to_front (R11)
# =================================================================================================


def test_r11_bring_to_front_from_minimized_keeps_maximized(window, qtbot):
    show(qtbot, window)
    window.setWindowState(Qt.WindowState.WindowMaximized)
    qtbot.wait(10)
    window.setWindowState(window.windowState() | Qt.WindowState.WindowMinimized)
    qtbot.wait(10)
    assert window.isMinimized()
    window.bring_to_front()
    qtbot.wait(10)
    state = window.windowState()
    assert not state & Qt.WindowState.WindowMinimized
    assert state & Qt.WindowState.WindowMaximized, "thu nhỏ từ phóng to: mở lại vẫn phóng to"
    assert window.isVisible()


def test_bring_to_front_from_minimized_normal_window_is_normal(window, qtbot):
    show(qtbot, window)
    window.setWindowState(Qt.WindowState.WindowMinimized)
    qtbot.wait(10)
    window.bring_to_front()
    qtbot.wait(10)
    state = window.windowState()
    assert not state & Qt.WindowState.WindowMinimized
    assert not state & Qt.WindowState.WindowMaximized
    assert window.isVisible()


def test_bring_to_front_hidden_maximized_window_stays_maximized(window, qtbot):
    show(qtbot, window)
    window.setWindowState(Qt.WindowState.WindowMaximized)
    qtbot.wait(10)
    window.hide()
    window.bring_to_front()
    qtbot.wait(10)
    assert window.isVisible()
    assert window.windowState() & Qt.WindowState.WindowMaximized
