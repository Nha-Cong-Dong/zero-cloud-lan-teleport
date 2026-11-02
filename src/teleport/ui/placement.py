"""Nhớ trạng thái cửa sổ: chụp và áp lại hình học của cửa sổ chính giữa các lần mở app.

`ui.app` đọc `Settings.window` một lần lúc khởi động, gọi `restore` ở dòng đầu `GuiApp.start()` (cửa sổ
còn ẩn, cả khi `--background`) và `capture` khi lưu (ẩn xuống khay, Thoát, `commitDataRequest`,
`shutdown`). Giá trị lưu là `app.settings.WindowGeometry`: hình học **thường** (không phóng to) theo
pixel logic, cờ phóng to, tên màn hình (`QScreen.name()`) và tỉ lệ điểm ảnh lúc lưu.

**`fit_window`** (thuần, không Qt) đặt lại hình đã lưu vào các màn hình hiện có (`ScreenArea`, vùng
khả dụng `availableGeometry`). Vùng khả dụng được co mép trên đi `title_allowance` (thanh tiêu đề do
hệ điều hành vẽ nằm ngoài `geometry()`; không cộng lề trái, phải, dưới):
1. Không có màn hình nào → None (giữ cỡ mặc định).
2. Có màn hình cùng tên, `dpr` lệch không quá `DPR_TOLERANCE` và hình đã lưu nằm trọn trong vùng đã co
   → trả nguyên (kể cả cỡ nhỏ hơn `min_size`).
3. Còn lại: màn hình đích = cùng tên, rồi màn hình chứa tâm hình, rồi `screens[0]` (màn hình chính).
   Cỡ kẹp vào `[min_size, vùng]` (vùng nhỏ hơn `min_size` thì lấy cỡ vùng); vị trí kẹp vào vùng, cận
   dưới thắng: vùng nhỏ hơn `min_size` thì góc trên trái ở góc trên trái của vùng đã co.
4. Thiếu `screen` hay `dpr` → không khớp luật 2 (đi luật 3).
Kết quả luôn nằm trọn trong vùng đã co của màn hình đích (trừ khi vùng đó cao không quá
`title_allowance`). `dpr` chỉ dùng để quyết định luật 2: hiện nó không đổi vị trí hay cỡ trả về.

**`capture`** không bao giờ raise: cửa sổ chưa từng hiện (`MainWindow.shown_once`) → None; phóng to /
thu nhỏ → `normalGeometry()` (không lưu trạng thái thu nhỏ), không thì `geometry()`; `normalGeometry()`
rỗng thì dùng hình của `fallback` (mốc đã lưu) với cờ phóng to hiện tại, không có mốc → None. Tên màn
hình sai luật của `WindowGeometry` → `screen=None`, `dpr` ngoài khoảng → `dpr=None`; dựng vẫn lỗi →
None (log DEBUG).

**`restore`**: `fit_window(saved, screens_now(), MIN_SIZE, TITLE_ALLOWANCE)`; màn hình đích khác màn
hình của cửa sổ thì `setScreen` trước (để DPI theo màn hình đích), rồi `setGeometry` (luôn
`geometry()`/`setGeometry()`, không `move()`, nên chụp lại không trôi), rồi mới
`setWindowState(WindowMaximized)` để hình thường là hình đã đặt. Không hiện cửa sổ. Việc chuyển màn
hình chưa kiểm trên máy thật nhiều màn hình (khác DPI).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QGuiApplication, QScreen
from PySide6.QtWidgets import QWidget

from teleport.app.settings import MAX_DPR, MAX_SCREEN_NAME_LEN, MIN_DPR, WindowGeometry
from teleport.ui.main_window import MIN_SIZE

TITLE_ALLOWANCE = 24  # px logic chừa cho thanh tiêu đề ở mép trên vùng khả dụng
DPR_TOLERANCE = 0.01  # lệch tỉ lệ điểm ảnh tối đa vẫn coi là cùng DPI

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScreenArea:
    """Một màn hình: tên, vùng khả dụng (`availableGeometry`, pixel logic) và tỉ lệ điểm ảnh."""

    name: str
    x: int
    y: int
    width: int
    height: int
    dpr: float


def _usable(area: ScreenArea, title_allowance: int) -> tuple[int, int, int, int]:
    """Vùng khả dụng đã co mép trên đi `title_allowance` (cao ít nhất 1)."""
    return area.x, area.y + title_allowance, area.width, max(1, area.height - title_allowance)


def _contains(rect: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> bool:
    x, y, w, h = rect
    ox, oy, ow, oh = outer
    return x >= ox and y >= oy and x + w <= ox + ow and y + h <= oy + oh


def fit_window(
    saved: WindowGeometry,
    screens: Sequence[ScreenArea],
    min_size: tuple[int, int],
    title_allowance: int,
) -> tuple[str, int, int, int, int] | None:
    """(tên màn hình đích, x, y, rộng, cao) cho hình đã lưu; không có màn hình → None (docstring module)."""
    if not screens:
        return None
    rect = (saved.x, saved.y, saved.width, saved.height)
    same = next((s for s in screens if saved.screen is not None and s.name == saved.screen), None)
    if (
        same is not None
        and saved.dpr is not None
        and abs(same.dpr - saved.dpr) <= DPR_TOLERANCE
        and _contains(rect, _usable(same, title_allowance))
    ):
        return (same.name, *rect)
    target = same
    if target is None:
        cx, cy = saved.x + saved.width // 2, saved.y + saved.height // 2
        target = next(
            (s for s in screens if s.x <= cx < s.x + s.width and s.y <= cy < s.y + s.height),
            screens[0],
        )
    ax, ay, aw, ah = _usable(target, title_allowance)
    min_w, min_h = min_size
    w = min(max(saved.width, min_w), aw)
    h = min(max(saved.height, min_h), ah)
    x = max(ax, min(saved.x, ax + aw - w))
    y = max(ay, min(saved.y, ay + ah - h))
    return target.name, x, y, w, h


def _area(screen: QScreen) -> ScreenArea:
    g = screen.availableGeometry()
    return ScreenArea(screen.name(), g.x(), g.y(), g.width(), g.height(), float(screen.devicePixelRatio()))


def screens_now() -> list[ScreenArea]:
    """Các màn hình hiện có, màn hình chính đứng đầu (seam cấp module: test vá được)."""
    primary = QGuiApplication.primaryScreen()
    others = [s for s in QGuiApplication.screens() if s is not primary]
    return [_area(s) for s in ([primary] if primary is not None else []) + others]


def _screen_fields(window: QWidget) -> tuple[str | None, float | None]:
    """(tên, dpr) của màn hình đang chứa cửa sổ; giá trị sai luật của `WindowGeometry` → None."""
    screen = window.screen()
    if screen is None:
        return None, None
    name: str | None = screen.name()
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_SCREEN_NAME_LEN or "\x00" in name:
        name = None
    ratio = float(screen.devicePixelRatio())
    dpr = ratio if math.isfinite(ratio) and MIN_DPR <= ratio <= MAX_DPR else None
    return name, dpr


def capture(window: QWidget, *, fallback: WindowGeometry | None = None) -> WindowGeometry | None:
    """Hình học hiện tại của cửa sổ để lưu; None khi chưa có gì đáng lưu. Không bao giờ raise."""
    try:
        if not getattr(window, "shown_once", False):
            return None
        state = window.windowState()
        maximized = bool(state & Qt.WindowState.WindowMaximized)
        special = (
            Qt.WindowState.WindowMaximized | Qt.WindowState.WindowMinimized | Qt.WindowState.WindowFullScreen
        )
        if state & special:
            rect = window.normalGeometry()
            if not rect.isValid() or rect.isEmpty():
                return None if fallback is None else replace(fallback, maximized=maximized)
        else:
            rect = window.geometry()
            if not rect.isValid() or rect.isEmpty():
                return None
        screen, dpr = _screen_fields(window)
        return WindowGeometry(
            rect.x(), rect.y(), rect.width(), rect.height(), maximized=maximized, screen=screen, dpr=dpr
        )
    except Exception as e:
        log.debug("không chụp được hình học cửa sổ: %s", type(e).__name__)
        return None


def restore(window: QWidget, saved: WindowGeometry | None) -> None:
    """Áp hình đã lưu lên cửa sổ đang ẩn (không hiện nó); None hay không có màn hình → để nguyên."""
    if saved is None:
        return
    fitted = fit_window(saved, screens_now(), (MIN_SIZE.width(), MIN_SIZE.height()), TITLE_ALLOWANCE)
    if fitted is None:
        return
    name, x, y, w, h = fitted
    current = window.screen()
    if current is None or current.name() != name:
        target = next((s for s in QGuiApplication.screens() if s.name() == name), None)
        if target is not None:
            window.setScreen(target)
    window.setGeometry(QRect(x, y, w, h))
    if saved.maximized:
        window.setWindowState(window.windowState() | Qt.WindowState.WindowMaximized)
