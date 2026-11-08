"""Chuyển động của giao diện (mvp N3, N12, WCAG 2.2.2 – 2.3.3): token, cổng bật/tắt và helper hoạt ảnh.

Mọi chuyển động phải có lý do (báo trạng thái đổi, dẫn mắt, xác nhận thao tác). Module chỉ dùng
QtCore/QtGui/QtWidgets.

**Token** (`Token`: thời lượng, đường cong, `overshoot` tùy chọn; `Token.easing()` dựng `QEasingCurve`)

| Token | ms | Đường cong | Dùng cho |
|---|---|---|---|
| `INSTANT` | 0 | Linear | khi tắt hiệu ứng |
| `FAST` | 120 | OutCubic | hover, pressed, đổi màu nút, chấm trạng thái |
| `NORMAL` | 200 | OutCubic | công tắc, thanh chọn trượt, mở/đóng theo chiều cao, bong bóng mới |
| `SLOW` | 280 | InOutCubic | đổi trang, hộp thoại/cửa sổ hiện |
| `EMPHASIS` | 450 | OutBack, overshoot 1.2 | dấu tích “đã xong”, một lần |

Giới hạn: quãng dịch tối đa `MAX_SHIFT_PX` (8 px), không phóng to/thu nhỏ quá `MAX_SCALE_DELTA` (2%),
hoạt ảnh lặp tối đa `MAX_LOOP_MS` (5 s). Các hằng là biến cấp module, đọc lúc dùng.

**Hai cổng**
- `animations_enabled()`: chỉ đọc cài đặt hiệu ứng của hệ điều hành, **mỗi lần gọi** (người dùng đổi
  lúc app đang chạy thì lần chạy hoạt ảnh sau theo ngay); không bao giờ ném lỗi, lỗi → True (hoạt
  ảnh của Teleport đều ngắn, tự dừng, chỉ để trang trí; thiếu cài đặt thì không đáng chặn).
  - Windows: `SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION)` qua `ctypes` (“Hiện hiệu ứng động
    trong Windows” của Cài đặt › Trợ năng › Hiệu ứng hình ảnh). Tắt → False.
  - macOS: “Giảm chuyển động” (`NSWorkspace.accessibilityDisplayShouldReduceMotion`) qua `ctypes` và
    objc runtime (nạp libobjc + AppKit lười một lần; mỗi kiểu trả về một prototype `objc_msgSend`
    riêng, cần cho arm64). Bật → False. Đọc mỗi lần.
  - Linux: khóa `org.gnome.desktop.interface enable-animations` qua `gsettings` (KDE và các môi
    trường khác vẫn có khóa này khi cài thư viện GNOME; không có `gsettings` → True). Gọi tiến trình
    con tốn thời gian nên kết quả (cả lỗi đã đổi thành True) nhớ `_GNOME_CACHE_TTL` giây; mỗi lần
    hỏi lại chặn luồng giao diện **tối đa** `_GSETTINGS_TIMEOUT` (0,5 s, khi dbus treo).
  - Hệ khác: True.
- `should_animate()` = không ở chế độ tức thì của test, đã có `QCoreApplication` và
  `animations_enabled()` (gọi qua tên toàn cục của module, test vá được). Đây là cổng của **mọi
  helper** bên dưới: False → nhảy thẳng tới trạng thái cuối (không bỏ trạng thái, chỉ bỏ phần chuyển).

**Luật**
- Hoạt ảnh lặp (`Loop`) tự dừng sau ≤ `MAX_LOOP_MS` và dừng khi widget hay cửa sổ ẩn/thu nhỏ; không tự
  chạy lại. Không timer chạy nền khi không có hoạt ảnh (N3).
- Bàn phím và trình đọc màn hình không chờ hoạt ảnh: tiêu điểm chuyển ngay. `HeightAnimator.collapse`
  không dời tiêu điểm, bên gọi dời trước.
- `FrameDriver` là nhịp khung chung cho hoạt ảnh tự vẽ dài (tiến độ suốt lượt truyền, không hợp `Loop`):
  timer chỉ chạy giữa `wake()` thành công và lúc dừng; widget hay cửa sổ ẩn/thu nhỏ → `finish()` (trạng
  thái nhảy về cuối); không tự chạy lại.
- Không `QGraphicsOpacityEffect` (hay hiệu ứng đồ họa khác) trên cây widget lớn: vẽ lại cả cây, tốn
  CPU. Mờ chéo dùng ảnh chụp (`Fader`), cửa sổ cấp cao dùng `windowOpacity` (`fade_in_window`). Ngoại
  lệ duy nhất: hiệu ứng đồ họa tạm trên **một** widget nhỏ (một hàng của luồng chat), mỗi pha tối đa
  `SLOW.ms`, gỡ ngay khi xong (`ui/chat_motion.py`): mờ dần, trượt và rung phải dịch cả các widget con thật.
- Giá trị trả về của `animate` / `animate_value` là hoạt ảnh `DeleteWhenStopped`: chỉ dùng khi
  `shiboken6.isValid(anim)`. Mỗi `(target, prop)` (hay `(owner, key)`) chỉ một hoạt ảnh: hoạt ảnh mới
  thay hoạt ảnh cũ (cũ dừng, `on_finished` cũ không chạy, mới chạy từ giá trị hiện tại).
- Sổ đăng ký giữ tham chiếu mạnh tới hoạt ảnh đang chạy, xóa khi `finished` / `destroyed`; khóa
  và closure chỉ giữ tham chiếu yếu tới đích (lý do ở chú thích của sổ). Bên dùng cũng đừng nối vào
  signal của hoạt ảnh một closure giữ chặt đích do Python sở hữu. `finish_all()` đưa mọi hoạt ảnh
  tới giá trị cuối và gọi `finish()` của mọi `FrameDriver` (dùng trước `grab()`), `running_count()`
  đếm số đang chạy, kể cả `Loop` và `FrameDriver` (test N3).

**Helper và chỗ dùng**
- `Loop`: sóng radar (`ui.discover`), minh họa (`ui.illustrations`).
- `Fader`: đổi trang (`ui.main_window.PageStack`) và khung phải `stream_pane` (spec G3).
- `HeightAnimator`: mở/đóng `widgets.InfoBar`.
- `SlidingRect`: hình chữ nhật trượt (thanh chọn), chủ tự vẽ `rect` trong `paintEvent`: nền hàng
  được chọn của danh sách cuộc trò chuyện (`ui.conversations`), vạch nhấn cột mục Cài đặt
  (`ui.settings`).
- `fade_in_on_show(window)`: bộ lọc sự kiện chỉ trên một hộp, mỗi lần hiện thì `fade_in_window`: hộp
  hỏi nhận file (`ui.offer`), hộp của `ui.pairing`, hộp hỏi Thoát (`ui.app`), hộp xóa lịch sử
  (`ui.settings`). Không dùng bộ lọc cấp `QApplication` (mọi sự kiện đi qua Python, tốn CPU, N3).

**Seam test** (cấp module, đọc lúc gọi): `_instant` / `_force_instant(on)` (chế độ tức thì, không chạm
`animations_enabled`), `_reset_for_tests()` (xóa cache GNOME, tắt chế độ tức thì),
`_stop_all_for_tests()` (dừng mọi hoạt ảnh, `Loop` và `FrameDriver` không gọi `on_finished` /
`on_stop` / `on_finish`, trả `windowOpacity` về 1.0, xóa sổ), `FrameDriver.FRAME_MS`,
`_query_client_area_animation()` (Windows), `_query_macos_reduce_motion()`, `_query_gnome_animations()`,
`_monotonic`, `_GNOME_CACHE_TTL`, `_GSETTINGS_TIMEOUT`. Quy ước fixture: `architecture.md` §6.
"""

from __future__ import annotations

import subprocess
import sys
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import (
    QAbstractAnimation,
    QCoreApplication,
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QGuiApplication, QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import QWidget
from shiboken6 import delete, isValid

# --- Token ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Token:
    """Một mức chuyển động: thời lượng (ms), đường cong và `overshoot` (chỉ cho đường cong Back)."""

    ms: int
    curve: QEasingCurve.Type
    overshoot: float | None = None

    def easing(self) -> QEasingCurve:
        curve = QEasingCurve(self.curve)
        if self.overshoot is not None:
            curve.setOvershoot(self.overshoot)
        return curve


INSTANT = Token(0, QEasingCurve.Type.Linear)
FAST = Token(120, QEasingCurve.Type.OutCubic)
NORMAL = Token(200, QEasingCurve.Type.OutCubic)
SLOW = Token(280, QEasingCurve.Type.InOutCubic)
EMPHASIS = Token(450, QEasingCurve.Type.OutBack, 1.2)

MAX_SHIFT_PX = 8
MAX_SCALE_DELTA = 0.02
MAX_LOOP_MS = 5000

_QWIDGETSIZE_MAX = (1 << 24) - 1
_PLATFORMS_WITHOUT_WINDOW_OPACITY = frozenset({"offscreen", "wayland", "minimal"})

# --- Cổng bật/tắt --------------------------------------------------------------------------------

SPI_GETCLIENTAREAANIMATION = 0x1042

_GNOME_CACHE_TTL = 5.0
_GSETTINGS_TIMEOUT = 0.5
_monotonic: Callable[[], float] = time.monotonic

_instant = False
_gnome_cache: tuple[float, bool] | None = None  # (lúc hỏi theo `_monotonic`, kết quả)
_objc_libs: tuple[Any, Any] | None = None  # (libobjc, AppKit), nạp lười một lần


def _query_client_area_animation() -> bool:
    """Giá trị `SPI_GETCLIENTAREAANIMATION` của Windows; gọi lỗi → OSError. Ngoài Windows → True."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        value = wintypes.BOOL(True)
        if not ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(value), 0
        ):
            raise OSError("SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION) lỗi")
        return bool(value.value)
    return True


def _load_objc() -> tuple[Any, Any]:
    global _objc_libs
    if _objc_libs is None:
        import ctypes

        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        # Nạp AppKit để lớp NSWorkspace có trong runtime (Qt thường đã nạp sẵn).
        appkit = ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        _objc_libs = (objc, appkit)
    return _objc_libs


def _query_macos_reduce_motion() -> bool:
    """True nếu macOS bật “Giảm chuyển động”; lỗi gì cũng ném (bên gọi đổi thành “có hoạt ảnh”).

    Mỗi kiểu trả về một prototype `objc_msgSend` riêng (`CFUNCTYPE` trên địa chỉ hàm): trên arm64 gọi
    variadic với prototype sai là đọc rác. BOOL đọc bằng `c_bool`.
    """
    import ctypes

    objc, _appkit = _load_objc()
    address = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    if not address:
        raise OSError("không thấy objc_msgSend")
    send_id = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(address)
    send_bool = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(address)
    send_bool_sel = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
        address
    )
    workspace_class = objc.objc_getClass(b"NSWorkspace")
    if not workspace_class:
        raise OSError("không thấy lớp NSWorkspace")
    workspace = send_id(workspace_class, objc.sel_registerName(b"sharedWorkspace"))
    if not workspace:
        raise OSError("sharedWorkspace trả nil")
    reduce_motion = objc.sel_registerName(b"accessibilityDisplayShouldReduceMotion")
    if not send_bool_sel(workspace, objc.sel_registerName(b"respondsToSelector:"), reduce_motion):
        return False  # macOS quá cũ, không có cài đặt này
    return bool(send_bool(workspace, reduce_motion))


def _query_gnome_animations() -> bool:
    """Khóa `enable-animations` của GNOME; không có `gsettings` → True; mã thoát ≠ 0 → OSError;
    chữ khác `true`/`false` → ValueError; quá `_GSETTINGS_TIMEOUT` → `subprocess.TimeoutExpired`."""
    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "enable-animations"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GSETTINGS_TIMEOUT,
        )
    except FileNotFoundError:
        return True
    if result.returncode != 0:
        raise OSError(f"gsettings thoát mã {result.returncode}")
    value = (result.stdout or "").strip()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"gsettings trả giá trị lạ: {value[:40]!r}")


def _gnome_animations_cached() -> bool:
    global _gnome_cache
    now = _monotonic()
    cached = _gnome_cache
    if cached is not None and now - cached[0] < _GNOME_CACHE_TTL:
        return cached[1]
    try:
        value = bool(_query_gnome_animations())
    except Exception:
        value = True
    _gnome_cache = (now, value)
    return value


def animations_enabled() -> bool:
    """True nếu cài đặt hệ điều hành cho chạy hoạt ảnh trang trí (xem docstring module); không bao giờ ném."""
    try:
        if sys.platform == "win32":
            return bool(_query_client_area_animation())
        if sys.platform == "darwin":
            return not _query_macos_reduce_motion()
        if sys.platform.startswith("linux"):
            return _gnome_animations_cached()
    except Exception:
        return True
    return True


def should_animate() -> bool:
    """Cổng của mọi helper: False khi test ở chế độ tức thì, chưa có `QCoreApplication` hay HĐH tắt
    hiệu ứng."""
    if _instant:
        return False
    if QCoreApplication.instance() is None:
        return False
    return animations_enabled()


def _force_instant(on: bool) -> None:
    """Seam test: bật/tắt chế độ tức thì (mọi helper nhảy thẳng tới giá trị cuối)."""
    global _instant
    _instant = bool(on)


def _reset_for_tests() -> None:
    """Seam test: xóa cache GNOME và tắt chế độ tức thì."""
    global _instant, _gnome_cache
    _instant = False
    _gnome_cache = None


# --- Sổ đăng ký ----------------------------------------------------------------------------------

# Khóa sổ: (loại, `weakref.ref(đối tượng)`, tên): ("prop", target, prop), ("value", owner, key),
# ("loop", Loop, ""), ("driver", FrameDriver, ""). Khóa và closure nối vào hoạt ảnh KHÔNG giữ tham
# chiếu mạnh tới đích hay chủ: hoạt ảnh là con của chúng, nên lúc hoạt ảnh bị hủy mà closure thả tham
# chiếu cuối của một đích do Python sở hữu thì đích bị xóa ngay giữa destructor của chính hoạt ảnh con →
# xóa hai lần, tiến trình chết.
_Key = tuple[str, Any, str]


def _key(kind: str, obj: QObject, name: str) -> _Key:
    return (kind, weakref.ref(obj), name)


@dataclass(eq=False, slots=True)
class _Entry:
    """Một hoạt ảnh đang chạy. Callback xóa về None khi xong hay bị dừng (thả tham chiếu sớm).

    `window`: cửa sổ (tham chiếu yếu) khi đây là `fade_in_window`, dừng thì trả `windowOpacity` 1.0.
    """

    key: _Key
    anim: QVariantAnimation
    on_finished: Callable[[], None] | None = None
    on_value: Callable[[Any], None] | None = None
    window: weakref.ref[QWidget] | None = None

    def release(self) -> None:
        self.on_finished = None
        self.on_value = None


_registry: dict[_Key, _Entry | Loop | FrameDriver] = {}


def _drop(key: _Key, entry: object) -> None:
    if _registry.get(key) is entry:
        del _registry[key]


def _cancel(key: _Key) -> None:
    """Gỡ hoạt ảnh trên `key` khỏi sổ (đồng bộ, trước `stop()` vì `stop()` không phát `finished`) rồi
    dừng; `on_finished` của nó không chạy."""
    entry = _registry.pop(key, None)
    if isinstance(entry, _Entry):
        entry.release()
        if isValid(entry.anim):
            entry.anim.stop()


def _on_entry_finished(entry: _Entry) -> None:
    callback = entry.on_finished
    entry.release()
    _drop(entry.key, entry)
    if callback is not None:
        callback()


def _on_entry_value(entry: _Entry, value: Any) -> None:
    callback = entry.on_value
    if callback is not None:
        callback(value)


def _register(
    key: _Key,
    anim: QVariantAnimation,
    on_finished: Callable[[], None] | None,
    on_value: Callable[[Any], None] | None = None,
) -> _Entry:
    entry = _Entry(key, anim, on_finished, on_value)
    _registry[key] = entry
    if on_value is not None:
        anim.valueChanged.connect(lambda value: _on_entry_value(entry, value))
    anim.finished.connect(lambda: _on_entry_finished(entry))
    anim.destroyed.connect(lambda *_: _drop(key, entry))
    return entry


def _restore_window_opacity(entry: _Entry) -> None:
    window = entry.window() if entry.window is not None else None
    if window is not None and isValid(window):
        window.setWindowOpacity(1.0)


def finish_all() -> int:
    """Đưa mọi hoạt ảnh đang chạy tới giá trị cuối (`finished` và `on_finished` chạy), dừng mọi `Loop`,
    gọi `finish()` của mọi `FrameDriver` đang chạy.

    Lặp tới khi sổ rỗng (callback có thể chạy hoạt ảnh mới), tối đa 8 vòng; bỏ qua đối tượng đã hủy.
    Trả tổng số đã xử lý. Gọi trước `grab()` để ảnh chụp không bắt giữa chừng.
    """
    total = 0
    for _ in range(8):
        if not _registry:
            break
        for key, entry in list(_registry.items()):
            if _registry.get(key) is not entry:
                continue
            if isinstance(entry, Loop | FrameDriver):
                if isValid(entry):
                    if isinstance(entry, Loop):
                        entry.stop()  # tự gỡ khỏi sổ; `on_stop` có thể đã đăng ký lại vòng mới
                    else:
                        entry.finish()  # tự gỡ khỏi sổ rồi gọi `on_finish`
                    total += 1
                elif _registry.get(key) is entry:
                    _drop(key, entry)
                continue
            anim = entry.anim
            if not isValid(anim):
                entry.release()
                _drop(key, entry)
                continue
            anim.setCurrentTime(anim.totalDuration())  # phát `finished` đồng bộ
            if _registry.get(key) is entry:  # phòng khi không phát
                _cancel(key)
            total += 1
    return total


def running_count() -> int:
    """Số hoạt ảnh, `Loop` và `FrameDriver` đang chạy (rảnh thì 0, mvp N3)."""
    count = 0
    for entry in _registry.values():
        if isinstance(entry, Loop | FrameDriver):
            count += isValid(entry) and entry.running
        elif isValid(entry.anim):
            count += 1
    return count


def _stop_all_for_tests() -> None:
    """Seam test: dừng mọi hoạt ảnh, `Loop` và `FrameDriver` mà không gọi `on_finished` / `on_stop` /
    `on_finish`, rồi xóa sổ."""
    entries = list(_registry.values())
    _registry.clear()
    for entry in entries:
        if isinstance(entry, Loop):
            if isValid(entry):
                entry._halt(notify=False)
        elif isinstance(entry, FrameDriver):
            if isValid(entry):
                entry.stop()
        else:
            entry.release()
            if isValid(entry.anim):
                entry.anim.stop()
            _restore_window_opacity(entry)


# --- Chạy hoạt ảnh -------------------------------------------------------------------------------


def animate(
    target: QObject,
    prop: str,
    end: Any,
    token: Token | None = None,
    *,
    start: Any = None,
    on_finished: Callable[[], None] | None = None,
) -> QPropertyAnimation | None:
    """Chạy `prop` của `target` tới `end` theo `token` (mặc định `NORMAL`).

    `prop` phải là Q_PROPERTY của `target` (cả hai chế độ), không thì `ValueError`. Không được hoạt
    ảnh (`should_animate()` False) hay token 0 ms: dừng hoạt ảnh cũ, gán ngay `end`, gọi
    `on_finished()` đồng bộ rồi trả None. Ngược lại trả hoạt ảnh (`DeleteWhenStopped`, con của
    `target`), chạy từ `start` hay giá trị hiện tại.
    """
    token = NORMAL if token is None else token
    if target.metaObject().indexOfProperty(prop) < 0:
        raise ValueError(f"{type(target).__name__} không có property {prop!r}")
    key = _key("prop", target, prop)
    if not should_animate() or token.ms <= 0:
        _cancel(key)
        target.setProperty(prop, end)
        if on_finished is not None:
            on_finished()
        return None
    if start is None:
        start = target.property(prop)
    _cancel(key)
    anim = QPropertyAnimation(target, prop.encode(), target)
    anim.setDuration(token.ms)
    anim.setEasingCurve(token.easing())
    anim.setStartValue(start)
    anim.setEndValue(end)
    _register(key, anim, on_finished)
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def animate_value(
    owner: QObject,
    start: Any,
    end: Any,
    token: Token,
    on_value: Callable[[Any], None],
    *,
    key: str = "",
    on_finished: Callable[[], None] | None = None,
) -> QVariantAnimation | None:
    """Như `animate` cho giá trị tự vẽ: `on_value` nhận từng giá trị; mỗi `(owner, key)` một hoạt ảnh
    (con của `owner`).

    Tức thì: gọi `on_value(end)` rồi `on_finished()`, trả None.
    """
    registry_key = _key("value", owner, key)
    if not should_animate() or token.ms <= 0:
        _cancel(registry_key)
        on_value(end)
        if on_finished is not None:
            on_finished()
        return None
    _cancel(registry_key)
    anim = QVariantAnimation(owner)
    anim.setDuration(token.ms)
    anim.setEasingCurve(token.easing())
    anim.setStartValue(start)
    anim.setEndValue(end)
    _register(registry_key, anim, on_finished, on_value)
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


# --- Hoạt ảnh lặp --------------------------------------------------------------------------------


class Loop(QObject):
    """Hoạt ảnh lặp 0→1 mỗi `period_ms`, tổng ≤ `min(max_ms, MAX_LOOP_MS)`; con của `widget`.

    `start()` không làm gì khi không được hoạt ảnh, widget không hiện, cửa sổ đang thu nhỏ hay đang
    chạy. Widget hay cửa sổ ẩn, cửa sổ thu nhỏ → `stop()`; không tự chạy lại (bên gọi `start()` lại
    khi cần). `on_stop` chạy đúng một lần mỗi lần dừng (tự hết, `stop()`, `finish_all()`).
    """

    def __init__(
        self,
        widget: QWidget,
        period_ms: int,
        on_step: Callable[[float], None],
        *,
        max_ms: int | None = None,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(widget)
        self._widget = widget
        self._period_ms = period_ms
        self._max_ms = max_ms
        self._on_step = on_step
        self._on_stop = on_stop
        self._anim: QVariantAnimation | None = None
        self._watched: list[QObject] = []
        self._key = _key("loop", self, "")
        key = self._key
        self.destroyed.connect(lambda *_: _registry.pop(key, None))

    @property
    def running(self) -> bool:
        return self._anim is not None

    def start(self) -> None:
        if self._anim is not None or not should_animate():
            return
        widget = self._widget
        if not isValid(widget) or not widget.isVisible():
            return
        window = widget.window()
        if window.isMinimized():
            return
        cap = min(MAX_LOOP_MS if self._max_ms is None else self._max_ms, MAX_LOOP_MS)
        cap = max(1, cap)
        period = min(max(1, int(self._period_ms)), cap)
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(period)
        anim.setLoopCount(max(1, cap // period))
        anim.valueChanged.connect(self._step)
        anim.finished.connect(self._finished)
        self._anim = anim
        for watched in (widget, window) if window is not widget else (widget,):
            watched.installEventFilter(self)
            self._watched.append(watched)
        _registry[self._key] = self
        anim.start()

    def stop(self) -> None:
        self._halt(notify=True)

    def _step(self, value: Any) -> None:
        if self._anim is not None:
            self._on_step(float(value))

    def _finished(self) -> None:
        self._halt(notify=True)

    def _halt(self, *, notify: bool) -> None:
        anim = self._anim
        if anim is None:  # cờ chống dừng (và gọi `on_stop`) hai lần
            return
        self._anim = None
        _drop(self._key, self)
        for watched in self._watched:
            if isValid(watched):
                watched.removeEventFilter(self)
        self._watched.clear()
        if isValid(anim):
            anim.stop()
            anim.deleteLater()
        if notify and self._on_stop is not None:
            self._on_stop()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Lúc cây widget bị hủy, PySide có thể đã xóa thuộc tính Python mà Qt vẫn gửi sự kiện tới bộ
        # lọc (gặp trên CI macOS, xem `discover.py`): khi đó bỏ qua.
        if "_anim" not in self.__dict__ or self.__dict__["_anim"] is None or not isValid(self):
            return False
        kind = event.type()
        if kind == QEvent.Type.Hide or (
            kind == QEvent.Type.WindowStateChange
            and isValid(watched)
            and isinstance(watched, QWidget)
            and watched.isWindow()
            and watched.isMinimized()
        ):
            self.stop()
        return False


# --- Nhịp khung ----------------------------------------------------------------------------------


class FrameDriver(QObject):
    """Nhịp khung chung (`FRAME_MS`) cho hoạt ảnh tự vẽ không có trần `MAX_LOOP_MS`; con của `widget`.

    `wake()`: đang chạy → True; không được hoạt ảnh, widget không hiện hay cửa sổ thu nhỏ → False (bên
    gọi nhảy thẳng tới trạng thái cuối); còn lại chạy timer rồi True. Mỗi khung gọi `on_frame()`, trả
    False thì dừng (không gọi `on_finish`). Widget hay cửa sổ ẩn, cửa sổ thu nhỏ → `finish()`. `finish()`
    dừng (nếu đang chạy) rồi **luôn** gọi `on_finish()` đúng một lần; `stop()` chỉ dừng. Có trong sổ đăng
    ký khi chạy (`finish_all`, `running_count`, `_stop_all_for_tests`).
    """

    FRAME_MS = 16

    def __init__(
        self,
        widget: QWidget,
        on_frame: Callable[[], bool],
        on_finish: Callable[[], None],
    ) -> None:
        super().__init__(widget)
        self._widget = widget
        self._on_frame = on_frame
        self._on_finish = on_finish
        self._running = False
        self._watched: list[QObject] = []
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._frame)
        self._key = _key("driver", self, "")
        key = self._key
        self.destroyed.connect(lambda *_: _registry.pop(key, None))

    @property
    def running(self) -> bool:
        return self._running

    def can_run(self) -> bool:
        """Được hoạt ảnh, widget đang hiện và cửa sổ không thu nhỏ."""
        widget = self._widget
        if not should_animate() or not isValid(widget) or not widget.isVisible():
            return False
        return not widget.window().isMinimized()

    def wake(self) -> bool:
        if self._running:
            if should_animate():
                return True
            # Hệ điều hành vừa tắt hiệu ứng giữa chừng: về cuối ngay, không đợi driver tự dừng (N12).
            self.finish()
            return False
        if not self.can_run():
            return False
        widget = self._widget
        window = widget.window()
        for watched in (widget, window) if window is not widget else (widget,):
            watched.installEventFilter(self)
            self._watched.append(watched)
        self._running = True
        _registry[self._key] = self
        self._timer.start()
        return True

    def finish(self) -> None:
        self._halt()
        self._on_finish()

    def stop(self) -> None:
        self._halt()

    def _frame(self) -> None:
        if self._running and not self._on_frame():
            self._halt()

    def _halt(self) -> None:
        if not self._running:  # cờ chống dừng hai lần
            return
        self._running = False
        _drop(self._key, self)
        if isValid(self._timer):
            self._timer.stop()
        for watched in self._watched:
            if isValid(watched):
                watched.removeEventFilter(self)
        self._watched.clear()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Bẫy giống `Loop.eventFilter`: lúc cây widget bị hủy, thuộc tính Python có thể đã mất.
        if not self.__dict__.get("_running") or not isValid(self):
            return False
        kind = event.type()
        if kind == QEvent.Type.Hide or (
            kind == QEvent.Type.WindowStateChange
            and isValid(watched)
            and isinstance(watched, QWidget)
            and watched.isWindow()
            and watched.isMinimized()
        ):
            self.finish()
        return False


# --- Mờ dần --------------------------------------------------------------------------------------


class _SnapshotOverlay(QWidget):
    """Ảnh chụp cũ phủ trên container, mờ dần (và dịch theo `shift`); không nhận chuột, không tiêu điểm."""

    def __init__(self, parent: QWidget, pixmap: QPixmap, shift: QPoint) -> None:
        super().__init__(parent)
        self.setObjectName("motionOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._pixmap = pixmap  # giữ `devicePixelRatio` của `grab()`, vẽ đúng trên màn HiDPI
        self._shift = shift
        self._opacity = 1.0

    def set_opacity(self, value: Any) -> None:
        self._opacity = max(0.0, min(1.0, float(value)))
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        progress = 1.0 - self._opacity
        offset = QPoint(round(self._shift.x() * progress), round(self._shift.y() * progress))
        painter = QPainter(self)
        painter.setOpacity(self._opacity)
        painter.drawPixmap(offset, self._pixmap)
        painter.end()


def _clamp_shift(shift: QPoint | None) -> QPoint:
    if shift is None:
        return QPoint()
    limit = MAX_SHIFT_PX
    return QPoint(max(-limit, min(limit, shift.x())), max(-limit, min(limit, shift.y())))


class Fader(QObject):
    """Mờ chéo nội dung `container` qua ảnh chụp: không vẽ hai cây cùng lúc, không hiệu ứng đồ họa.

    `crossfade(change)` chụp container (kèm overlay đang mờ, nếu có), gọi `change()` **ngay** (tiêu
    điểm, trình đọc màn hình thấy trạng thái mới tức thì), rồi phủ ảnh cũ mờ dần 1→0 (dịch theo
    `shift`, kẹp ≤ `MAX_SHIFT_PX`). Không được hoạt ảnh, container ẩn hay rỗng: chỉ `change()`.
    """

    _KEY = "crossfade"

    def __init__(self, container: QWidget) -> None:
        super().__init__(container)
        self._container_ref = weakref.ref(container)  # tham chiếu yếu (như `HeightAnimator`)
        self._overlay: _SnapshotOverlay | None = None
        container.installEventFilter(self)

    def crossfade(
        self, change: Callable[[], Any], token: Token | None = None, *, shift: QPoint | None = None
    ) -> None:
        token = SLOW if token is None else token
        container = self._container_ref()
        if container is None or not isValid(container):
            change()
            return
        if (
            not should_animate()
            or token.ms <= 0
            or not container.isVisible()
            or container.width() <= 0
            or container.height() <= 0
        ):
            self._remove_overlay()
            change()
            return
        pixmap = container.grab()  # vẽ cả overlay cũ nên không giật khi đổi liên tiếp
        self._remove_overlay()
        change()
        self._remove_overlay()  # `change()` có thể gọi lồng `crossfade` và đã dựng overlay riêng
        overlay = _SnapshotOverlay(container, pixmap, _clamp_shift(shift))
        overlay.setGeometry(container.rect())
        overlay.show()
        overlay.raise_()
        self._overlay = overlay
        animate_value(
            self, 1.0, 0.0, token, self._set_opacity, key=self._KEY, on_finished=self._remove_overlay
        )

    def show(self, widget: QWidget, token: Token | None = None) -> None:
        self.crossfade(widget.show, NORMAL if token is None else token)

    def hide(self, widget: QWidget, token: Token | None = None) -> None:
        self.crossfade(widget.hide, NORMAL if token is None else token)

    def _set_opacity(self, value: Any) -> None:
        overlay = self._overlay
        if overlay is not None and isValid(overlay):
            overlay.set_opacity(value)

    def _remove_overlay(self) -> None:
        _cancel(_key("value", self, self._KEY))
        overlay = self._overlay
        self._overlay = None
        if overlay is not None and isValid(overlay):
            overlay.hide()
            delete(overlay)  # xóa ngay (hoạt ảnh là con của Fader, không phải của overlay)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if "_overlay" not in self.__dict__:
            return False
        overlay = self.__dict__["_overlay"]
        if overlay is not None and event.type() == QEvent.Type.Resize and isValid(overlay):
            container = self.__dict__["_container_ref"]()
            if container is not None and isValid(container):
                overlay.setGeometry(container.rect())
        return False


def fade_in_window(window: QWidget, token: Token | None = None) -> QPropertyAnimation | None:
    """`windowOpacity` 0→1 cho cửa sổ cấp cao (mặc định `SLOW`).

    Nền không hỗ trợ độ mờ cửa sổ (`offscreen`, `wayland`, `minimal`) hay không được hoạt ảnh: đặt ngay
    1.0. Dừng theo đường nào (thay, `finish_all`, dọn test) cửa sổ cũng về 1.0.
    """
    token = SLOW if token is None else token
    key = _key("prop", window, "windowOpacity")
    if (
        not should_animate()
        or token.ms <= 0
        or QGuiApplication.platformName() in _PLATFORMS_WITHOUT_WINDOW_OPACITY
    ):
        _cancel(key)
        window.setWindowOpacity(1.0)
        return None
    anim = animate(window, "windowOpacity", 1.0, token, start=0.0)
    if anim is None:
        return None
    window_ref = weakref.ref(window)  # tham chiếu yếu: hoạt ảnh là con của cửa sổ (xem sổ đăng ký)
    entry = _registry.get(key)
    if isinstance(entry, _Entry) and entry.anim is anim:
        entry.window = window_ref

    def on_state(new_state: QAbstractAnimation.State, _old: QAbstractAnimation.State) -> None:
        current = window_ref()
        if new_state == QAbstractAnimation.State.Stopped and current is not None and isValid(current):
            current.setWindowOpacity(1.0)

    anim.stateChanged.connect(on_state)
    return anim


# --- Chiều cao -----------------------------------------------------------------------------------


class HeightAnimator(QObject):
    """Mở/đóng `widget` theo chiều cao (`maximumHeight`); con của `widget`.

    `expand` hiện widget ngay rồi nới `maximumHeight` từ chiều cao hiện tại tới chiều cao đo được
    (`heightForWidth` theo bề ngang của cha nếu có, không thì `sizeHint`), xong trả `maximumHeight` về
    không giới hạn. `collapse` thu về 0, xong mới `hide()`. Đảo chiều giữa chừng chạy tiếp từ chiều
    cao hiện tại. `minimumHeight` được hạ về 0 trong lúc chạy và trả lại khi xong.

    `collapse` không dời tiêu điểm: nếu tiêu điểm đang ở trong widget, bên gọi dời đi trước.
    """

    _PROP = "maximumHeight"

    def __init__(self, widget: QWidget) -> None:
        super().__init__(widget)
        # Tham chiếu yếu: widget giữ animator (thường trong `__dict__`), animator giữ mạnh widget sẽ
        # thành vòng; GC dọn vòng đó xóa `__dict__` của widget trong khi đối tượng C++ còn sống (chờ
        # `deleteLater`) và vẫn nhận sự kiện. Closure của hoạt ảnh cũng chỉ giữ tham chiếu yếu.
        self._widget_ref = weakref.ref(widget)
        self._expanded = not widget.isHidden()
        self._saved_min: int | None = None

    def _target(self) -> QWidget | None:
        widget = self._widget_ref()
        return widget if widget is not None and isValid(widget) else None

    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, on: bool, token: Token | None = None) -> None:
        widget = self._target()
        if widget is None:
            return
        running = _key("prop", widget, self._PROP) in _registry
        if on == self._expanded and not running:
            return
        if on:
            self.expand(token)
        else:
            self.collapse(token)

    def expand(self, token: Token | None = None, on_finished: Callable[[], None] | None = None) -> None:
        widget = self._target()
        if widget is None:
            return
        self._expanded = True
        if widget.isHidden():
            widget.setMaximumHeight(0)
            start = 0
        else:
            start = min(widget.height(), widget.maximumHeight())
        self._hold_minimum(widget)
        widget.show()
        target = self._measure(widget)
        widget_ref = self._widget_ref
        self_ref = weakref.ref(self)

        def done() -> None:
            current = widget_ref()
            if current is not None and isValid(current):
                current.setMaximumHeight(_QWIDGETSIZE_MAX)
            owner = self_ref()
            if owner is not None and isValid(owner):
                owner._release_minimum()
            if on_finished is not None:
                on_finished()

        animate(widget, self._PROP, target, token, start=start, on_finished=done)

    def collapse(self, token: Token | None = None, on_finished: Callable[[], None] | None = None) -> None:
        widget = self._target()
        if widget is None:
            return
        self._expanded = False
        key = _key("prop", widget, self._PROP)
        if widget.isHidden():
            _cancel(key)
            widget.setMaximumHeight(_QWIDGETSIZE_MAX)
            self._release_minimum()
            if on_finished is not None:
                on_finished()
            return
        start = min(widget.height(), widget.maximumHeight())
        self._hold_minimum(widget)
        widget_ref = self._widget_ref
        self_ref = weakref.ref(self)

        def done() -> None:
            current = widget_ref()
            if current is not None and isValid(current):
                current.hide()
                current.setMaximumHeight(_QWIDGETSIZE_MAX)
            owner = self_ref()
            if owner is not None and isValid(owner):
                owner._release_minimum()
            if on_finished is not None:
                on_finished()

        animate(widget, self._PROP, 0, token, start=start, on_finished=done)

    @staticmethod
    def _measure(widget: QWidget) -> int:
        # Gọi sau `widget.show()`: kích hoạt layout của cha để `widget.width()` là bề ngang thật; chỉ khi
        # vẫn bằng 0 (cha chưa có layout, chưa xếp) mới lấy bề ngang vùng nội dung của cha.
        parent = widget.parentWidget()
        parent_layout = parent.layout() if parent is not None else None
        if parent_layout is not None:
            parent_layout.activate()
        layout = widget.layout()
        if layout is not None:
            layout.activate()
        width = widget.width()
        if width <= 0 and parent is not None:
            width = parent.contentsRect().width()
        height = -1
        if widget.hasHeightForWidth():
            height = widget.heightForWidth(width)
        if height < 0:
            height = widget.sizeHint().height()
        return max(0, height)

    def _hold_minimum(self, widget: QWidget) -> None:
        # Đảo chiều giữa chừng: giá trị đã lưu vẫn là của lúc bắt đầu, giữ nguyên.
        if self._saved_min is None:
            self._saved_min = widget.minimumHeight()
            widget.setMinimumHeight(0)

    def _release_minimum(self) -> None:
        saved = self._saved_min
        self._saved_min = None
        widget = self._target()
        if saved is not None and widget is not None:
            widget.setMinimumHeight(saved)


# --- Thanh chọn trượt ----------------------------------------------------------------------------


class SlidingRect(QObject):
    """Một hình chữ nhật trượt giữa các vị trí (thanh chọn); con của `target`, chủ tự vẽ `rect`.

    `move_to(end, animate=True)` trượt từ `rect` đang vẽ tới `end` theo `token` (mặc định `NORMAL`),
    mỗi bước gọi `target.update()`. Nhảy thẳng khi `animate=False`, lần đầu (`rect` None), không được
    hoạt ảnh hay token 0 ms; nhánh này chỉ gọi `update()` khi vừa hủy một hoạt ảnh đang chạy (còn lại bên
    gọi đang vẽ hoặc tự gọi). `end` None → hủy, không vẽ. Đổi đích giữa chừng chạy tiếp từ `rect` đang vẽ.
    """

    _KEY = "slide"

    def __init__(self, target: QWidget, token: Token | None = None) -> None:
        super().__init__(target)
        self._target_ref = weakref.ref(target)  # tham chiếu yếu (luật sổ đăng ký)
        self._token = NORMAL if token is None else token
        self._rect: QRectF | None = None
        self._end: QRectF | None = None

    @property
    def rect(self) -> QRectF | None:
        return None if self._rect is None else QRectF(self._rect)

    @property
    def end(self) -> QRectF | None:
        return None if self._end is None else QRectF(self._end)

    def move_to(self, end: QRectF | None, *, animate: bool) -> None:
        if end is None:
            if self._end is None and self._rect is None:
                return
            self._cancel()
            self._end = None
            self._rect = None
            return
        if self._end is not None and self._end == end:
            return
        self._end = QRectF(end)
        if not animate or self._rect is None or not should_animate() or self._token.ms <= 0:
            was_running = _key("value", self, self._KEY) in _registry
            self._cancel()
            self._rect = QRectF(end)
            if was_running:  # vẽ lại cả vùng: phần nền đang trượt dở không còn sót (cuộn bằng blit)
                target = self._target_ref()
                if target is not None and isValid(target):
                    target.update()
            return
        target_ref = self._target_ref
        self_ref = weakref.ref(self)

        def on_value(value: Any) -> None:
            owner = self_ref()
            if owner is None or not isValid(owner) or not isinstance(value, QRectF):
                return
            owner._rect = QRectF(value)
            target = target_ref()
            if target is not None and isValid(target):
                target.update()

        animate_value(self, QRectF(self._rect), QRectF(end), self._token, on_value, key=self._KEY)

    def stop(self) -> None:
        """Nhảy tới `end` ngay, hủy hoạt ảnh, rồi `target.update()`."""
        self._cancel()
        self._rect = None if self._end is None else QRectF(self._end)
        target = self._target_ref()
        if target is not None and isValid(target):
            target.update()

    def _cancel(self) -> None:
        _cancel(_key("value", self, self._KEY))


# --- Hộp thoại mờ dần khi hiện -------------------------------------------------------------------


class _ShowFade(QObject):
    """Bộ lọc sự kiện chỉ trên một cửa sổ: lần hiện không do HĐH → `fade_in_window`."""

    def __init__(self, window: QWidget, token: Token) -> None:
        super().__init__(window)
        self.setObjectName("motionShowFade")
        self._window_ref = weakref.ref(window)
        self._token = token
        window.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Lúc cây bị hủy PySide có thể đã xóa thuộc tính Python mà Qt vẫn gửi sự kiện (xem `Loop`).
        if "_window_ref" not in self.__dict__ or not isValid(self):
            return False
        if event.type() != QEvent.Type.Show or event.spontaneous():
            return False
        window = self.__dict__["_window_ref"]()
        if window is None or watched is not window or not isinstance(window, QWidget):
            return False
        if not isValid(window):
            return False
        if not window.isWindow():  # hộp nhúng như widget con (cờ `Qt.Widget`): bỏ qua
            return False
        fade_in_window(window, self._token)
        return False


def fade_in_on_show(window: QWidget, token: Token | None = None) -> None:
    """Mỗi lần `window` hiện (không do HĐH) thì mờ dần vào bằng `fade_in_window` (mặc định `SLOW`).

    Gắn bộ lọc sự kiện con `motionShowFade` **chỉ trên** `window`; gọi lần hai không gắn thêm. Không dùng
    bộ lọc cấp `QApplication`: mọi sự kiện của app sẽ đi qua Python, tốn CPU cả khi rảnh (mvp N3).
    Hộp nhúng như widget con thì không mờ. Đóng hộp vẫn tức thì.
    """
    for child in window.children():
        if isinstance(child, _ShowFade) and isValid(child):
            return
    _ShowFade(window, SLOW if token is None else token)
