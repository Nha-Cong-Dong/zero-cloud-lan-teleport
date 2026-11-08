"""Nút bấm có chuyển động (spec G2): `AnimatedButton` và mức hover/ấn dùng chung `ButtonLevels`.

Chỉ import `theme`, `motion` và PySide6. `widgets.IconButton` dùng lại `ButtonLevels`.

**Cách vẽ: QSS vẽ viền, chữ và đo cỡ; nút chỉ vẽ phần nền động.** `paintEvent` lặp đúng chuỗi của
`QCommonStyle::drawControl(CE_PushButton)`: `CE_PushButtonBevel` → lớp phủ → `CE_PushButtonLabel` (rect
`SE_PushButtonContents`) → `PE_FrameFocusRect` (rect `SE_PushButtonFocusRect`, chỉ khi có
`State_HasFocus`). `QStyleOptionButton` bỏ `State_MouseOver` và `State_Sunken` (QSS đọc `:hover` /
`:pressed` từ `opt.state`, đã kiểm bằng pixel), nên QSS và Fusion luôn vẽ dạng “thường”; tiêu điểm
(`:focus`), `:disabled`, nút mặc định vẫn do style vẽ và **tĩnh**. Nút chưa chuyển vẫn vẽ bằng luật cũ.
- Nút chính (`primary=True`, mang `theme.PRIMARY_PROPERTY`): phủ phần **trong viền** (lùi
  `theme.PRIMARY_BORDER_WIDTH`, bo `RADIUS_CONTROL - PRIMARY_BORDER_WIDTH`) màu
  `blend(blend(normal, hover, h), pressed, p)` lấy nền từ `theme.primary_colors(dark)` lúc vẽ. h = p = 0
  hay nút tắt: không phủ, nên lúc nghỉ trùng `QPushButton` cùng property từng điểm ảnh.
- Nút phụ: phủ màu `text` với độ mờ `_SECONDARY_HOVER_ALPHA·h + (_SECONDARY_PRESS_ALPHA −
  _SECONDARY_HOVER_ALPHA)·p` (lùi 1 px, bo 2 px), tức 0,14 khi vừa rê vừa ấn hẳn.
- Ấn lún: nút đang xuống (`isDown()`) và không chạy thì chữ (và spinner) dịch xuống 1 px
  (`content_offset()`); viền, nền không dịch.
- Đổi sáng/tối, màu nhấn: màu đọc lúc vẽ; `PaletteChange` / `ApplicationPaletteChange` → `update()`.

**Mức hover h và mức ấn p** (`ButtonLevels`, ∈ [0, 1]): đích h = `underMouse()`, nút bật và không đang
chạy; p = `isDown()` và không đang chạy. `ButtonLevels` chỉ giữ **tham chiếu yếu** tới nút: nó được
nối vào `pressed` / `released` của nút (signal giữ chặt nó), nên giữ chặt nút là vòng tham chiếu qua C++
mà GC không gỡ được, nút không cha không bao giờ bị hủy.
Đồng bộ đích ở các sự kiện chuột trên, `pressed` / `released`, `EnabledChange`, `set_running`; ẩn
nút → nhảy về 0. Chạy bằng `motion.animate_value(owner, …, motion.FAST, key="hover"|"press")`; tức
thì / tắt hiệu ứng thì gán ngay. Không timer nào ngoài `motion`, rảnh thì không có hoạt ảnh (mvp N3).

**Đang chạy** (`set_running(True, text, description)`): lưu trạng thái nghỉ (chữ, tên và mô tả trợ năng,
`isDefault()`, `autoDefault()`, `shortcut()`), đặt chữ và tên trợ năng = `text` (None: giữ chữ), mô
tả = `description` (None: giữ), gỡ default/autoDefault (Enter của `QDialog` gọi `click()` C++ lên nút
mặc định), nhả nút, h/p về 0. Gọi lại khi đang chạy chỉ đổi chữ, tên, mô tả (dịch lại).
`set_running(False)` trả lại mọi thứ đã lưu. Nút **không** `setEnabled(False)`: tiêu điểm ở lại, không xám.
- Khóa bấm (cố gắng tối đa): `hitButton` False; `event()` nuốt chuột (nhấn, nhả, nhấn đúp), Space,
  Select và `Shortcut`; Enter/Return bị bỏ qua (`ignore`) để đi tiếp lên hộp thoại cha (nút mặc định
  của hộp nhận); `click()` / `animateClick()` gọi từ Python không làm gì.
- **Bẫy nút mặc định chính của `QDialog`**: `setDefault(False)` không xóa `mainDef` của hộp, nên khi
  tiêu điểm rời một nút autoDefault khác Qt gọi lại `setDefault(True)` (C++) lên nút đang chạy. Bộ lọc
  trên cửa sổ vì vậy gỡ lại default ngay khi thấy Enter/Return lúc đang chạy (trước
  `QDialog::keyPressEvent`), và `setDefault` / `setAutoDefault` / `setShortcut` gọi từ Python khi đang
  chạy chỉ ghi vào bản nghỉ.
- **Phím tắt**: `QAbstractButton::setText` thay phím tắt bằng mnemonic của chữ mới, nên mỗi lần đổi chữ
  lúc chạy và lúc trả chữ nghỉ, phím tắt nghỉ được đặt lại (`setShortcut` của bên dùng không mất).
  `setText` từ Python khi đang chạy đặt phím tắt nghỉ theo mnemonic của chữ mới, như `QPushButton`.
- **Bẫy `click()` C++**: không chặn được `click()` gọi từ C++ (hành động “Press” của trình đọc màn
  hình qua `QAccessibleButton`), nên `clicked` vẫn có thể phát khi đang chạy. **Bên dùng phải bỏ qua
  lần bấm thứ hai** (ví dụ cờ `_resolved` của `pairing.PairingDialog`).
- **Bẫy `setText` khi chạy**: `setText` / `setAccessibleName` gọi từ Python khi đang chạy chỉ ghi vào
  bản nghỉ (hiện lại khi dừng); gọi từ C++ thì không chặn được.
- Cỡ: lúc nghỉ `sizeHint()` của `QPushButton`; đang chạy cao = cao lúc nghỉ, ngang = max(ngang lúc nghỉ
  tính lại bằng `style().sizeFromContents(CT_PushButton)` với chữ nghỉ hiện tại, chữ chạy + spinner +
  khoảng cách): chỉ nở, không co.
- Spinner: cung 270°, nét `_SPINNER_PEN`, đầu tròn, màu chữ của nút lúc vẽ; cạnh `min(_SPINNER_MAX,
  fontMetrics().height())`, bên trái chữ cách `theme.SPACING[1]`, khối spinner + chữ căn giữa. Quay
  bằng `motion.Loop` (một vòng `_SPIN_PERIOD_MS`, tổng ≤ `motion.MAX_LOOP_MS`). Dừng ở **khung tĩnh**
  (góc 90°) khi không được hoạt ảnh, `Loop` chạm trần, nút / cửa sổ ẩn hay thu nhỏ; chữ “Đang …” luôn
  còn, nên trạng thái không chỉ nói bằng chuyển động (mvp N12). Lúc chạy nút lọc sự kiện của cửa sổ
  (`Show`, `WindowStateChange` hết thu nhỏ; nút sang cửa sổ khác thì `showEvent` cài lại bộ lọc) và
  bắt `showEvent` của chính nó để quay lại sau khi ẩn / thu nhỏ; chạm trần (Loop dừng khi nút và cửa
  sổ vẫn hiện, không thu nhỏ: tự hết hay `finish_all()`) thì không quay lại cho tới lần chạy sau.
  `set_running(False)` dừng `Loop` và gỡ bộ lọc.

API test: `hover_level()`, `press_level()`, `content_offset()`, `spinner_active()`.

**“Đã chép”** (spec G5): `CopiedFeedback` giữ dạng ✓ “Đã chép” của một nút trong `COPIED_FEEDBACK_MS`
(không phải hoạt ảnh, nên chạy cả khi tắt hiệu ứng); dùng ở nút “Chép” của `ui.wait_pairing` và nút
“Chép địa chỉ” của `ui.home.MachineCard`.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QHideEvent,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QStyleOptionFocusRect,
    QWidget,
)
from shiboken6 import isValid

from teleport.ui import motion, theme

_SPIN_PERIOD_MS = 1000  # ms, một vòng spinner
_SPINNER_MAX = 16  # px, cạnh lớn nhất của spinner
_SPINNER_PEN = 2  # px, nét spinner
_SPINNER_SPAN = 270  # độ, cung của spinner
_SPINNER_STATIC_ANGLE = 90.0  # độ, khung tĩnh
_SECONDARY_HOVER_ALPHA = 0.08  # độ mờ lớp phủ `text` của nút phụ khi rê chuột hẳn
_SECONDARY_PRESS_ALPHA = 0.14  # … khi vừa rê vừa ấn hẳn
_SECONDARY_INSET = 1  # px
_SECONDARY_RADIUS = 2  # px
_PALETTE_EVENTS = (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange)
_ENTER_EVENTS = (QEvent.Type.Enter, QEvent.Type.HoverEnter)
_LEAVE_EVENTS = (QEvent.Type.Leave, QEvent.Type.HoverLeave)
_MOUSE_EVENTS = (
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonRelease,
    QEvent.Type.MouseButtonDblClick,
)
_KEY_EVENTS = (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
_ACTIVATE_KEYS = (Qt.Key.Key_Space, Qt.Key.Key_Select)
_ENTER_KEYS = (Qt.Key.Key_Enter, Qt.Key.Key_Return)
_PLAIN_STATE = ~(QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Sunken)


def strip_interaction_state(state: QStyle.StateFlag) -> QStyle.StateFlag:
    """`state` bỏ `State_MouseOver` và `State_Sunken`: QSS và Fusion vẽ dạng “thường”."""
    return state & _PLAIN_STATE


class ButtonLevels:
    """Mức hover và mức ấn của một nút, chuyển dần bằng `motion.animate_value` (xem docstring module).

    `owner` gọi `on_event(event)` đầu `event()`, `sync()` khi trạng thái đổi; `blocked()` (ví dụ
    “đang chạy”) cho đích về 0.
    """

    def __init__(self, owner: QAbstractButton) -> None:
        self._owner_ref = weakref.ref(owner)  # yếu: xem docstring module
        self.hover = 0.0
        self.press = 0.0
        self._hover_target = 0.0
        self._press_target = 0.0
        self.blocked = False
        owner.pressed.connect(self.sync)
        owner.released.connect(self.sync)

    def _owner(self) -> QAbstractButton | None:
        owner = self._owner_ref()
        return owner if owner is not None and isValid(owner) else None

    def on_event(self, event: QEvent) -> None:
        kind = event.type()
        if kind in _ENTER_EVENTS or kind in _LEAVE_EVENTS or kind == QEvent.Type.EnabledChange:
            self.sync()

    def sync(self) -> None:
        """Chạy h và p tới đích hiện tại (đích không đổi thì không làm gì)."""
        owner = self._owner()
        if owner is None:
            return
        hover = 1.0 if (owner.underMouse() and owner.isEnabled() and not self.blocked) else 0.0
        press = 1.0 if (owner.isDown() and not self.blocked) else 0.0
        if hover != self._hover_target:
            self._hover_target = hover
            motion.animate_value(owner, self.hover, hover, motion.FAST, self._set_hover, key="hover")
        if press != self._press_target:
            self._press_target = press
            motion.animate_value(owner, self.press, press, motion.FAST, self._set_press, key="press")

    def reset(self) -> None:
        """Nhảy h và p về 0 ngay (không hoạt ảnh)."""
        self._hover_target = 0.0
        self._press_target = 0.0
        owner = self._owner()
        if owner is None:
            self.hover = self.press = 0.0
            return
        motion.animate_value(owner, self.hover, 0.0, motion.INSTANT, self._set_hover, key="hover")
        motion.animate_value(owner, self.press, 0.0, motion.INSTANT, self._set_press, key="press")

    def _set_hover(self, value: object) -> None:
        self.hover = _unit(value)
        self._repaint()

    def _set_press(self, value: object) -> None:
        self.press = _unit(value)
        self._repaint()

    def _repaint(self) -> None:
        owner = self._owner()
        if owner is not None:
            owner.update()


def _unit(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _dark() -> bool:
    return theme.is_dark_palette(QApplication.palette())


@dataclass
class _Rest:
    """Trạng thái nghỉ lưu lúc bắt đầu chạy (xem “Đang chạy” ở docstring module)."""

    text: str
    name: str
    description: str
    default: bool
    auto_default: bool
    shortcut: QKeySequence


class AnimatedButton(QPushButton):
    """Nút đổi màu mượt khi hover/ấn, có trạng thái “đang chạy” kèm spinner (xem docstring module)."""

    # Mức lớp: phương thức ảo có thể được Qt gọi trước khi `__init__` gán xong thuộc tính.
    _rest: _Rest | None = None

    def __init__(self, text: str = "", parent: QWidget | None = None, *, primary: bool = False) -> None:
        super().__init__(text, parent)
        self._primary = bool(primary)
        if self._primary:
            self.setProperty(theme.PRIMARY_PROPERTY, True)
        self._levels = ButtonLevels(self)
        self._rest: _Rest | None = None
        self._spin_angle = _SPINNER_STATIC_ANGLE
        self._spin_capped = False
        self._watched_window: weakref.ref[QWidget] | None = None
        self._spin = motion.Loop(self, _SPIN_PERIOD_MS, self._on_spin_step, on_stop=self._on_spin_stop)
        # Tạm cho tới khi sửa `motion.Loop`: khóa sổ của Loop chứa `weakref.ref`, mà weakref chỉ băm được
        # khi đối tượng còn sống. Loop chưa từng `start()` thì khóa chưa băm, và lúc hủy, slot `destroyed`
        # của nó (`_registry.pop(key)`) ném TypeError. Băm sẵn ở đây (weakref nhớ giá trị băm).
        hash(self._spin._key)

    # ---- API -----------------------------------------------------------------------------------

    @property
    def primary(self) -> bool:
        return self._primary

    @property
    def running(self) -> bool:
        return self._rest is not None

    def set_running(self, on: bool, text: str | None = None, description: str | None = None) -> None:
        """Bật/tắt trạng thái đang chạy (xem docstring module)."""
        if not on:
            self._stop_running()
            return
        rest = self._rest
        if rest is None:
            rest = _Rest(
                text=self.text(),
                name=self.accessibleName(),
                description=self.accessibleDescription(),
                default=self.isDefault(),
                auto_default=self.autoDefault(),
                shortcut=QKeySequence(self.shortcut()),
            )
            QPushButton.setDefault(self, False)
            QPushButton.setAutoDefault(self, False)
            self._rest = rest
            self.setDown(False)
            self._levels.blocked = True
            self._levels.reset()
            self._spin_capped = False
            self._watch_window()
            shown = rest.text if text is None else text
            self._show_text(shown, rest.shortcut)
            super().setAccessibleName(shown)
            if description is not None:
                self.setAccessibleDescription(description)
            self.updateGeometry()
            self._spin.start()
            self.update()
            return
        if text is not None:
            self._show_text(text, rest.shortcut)
            super().setAccessibleName(text)
        if description is not None:
            self.setAccessibleDescription(description)
        self.updateGeometry()

    def _show_text(self, text: str, shortcut: QKeySequence) -> None:
        """Đặt chữ hiện ra mà giữ phím tắt `shortcut` (`setText` thay phím tắt bằng mnemonic)."""
        super().setText(text)
        if self.shortcut() != shortcut:
            super().setShortcut(shortcut)

    def hover_level(self) -> float:
        return self._levels.hover

    def press_level(self) -> float:
        return self._levels.press

    def content_offset(self) -> int:
        """Số px chữ dịch xuống (1 khi đang ấn và không chạy)."""
        return 1 if (self.isDown() and not self.running) else 0

    def spinner_active(self) -> bool:
        """Spinner đang quay (False: khung tĩnh hay không chạy)."""
        return self.running and self._spin.running

    # ---- Ghi đè chữ khi đang chạy --------------------------------------------------------------

    def setText(self, text: str) -> None:
        rest = self._rest
        if rest is None:
            super().setText(text)
            return
        rest.text = text
        rest.shortcut = QKeySequence.mnemonic(text)
        self.updateGeometry()

    def setShortcut(self, key: QKeySequence | str) -> None:  # type: ignore[override]
        rest = self._rest
        if rest is None:
            super().setShortcut(key)
            return
        rest.shortcut = QKeySequence(key)
        super().setShortcut(rest.shortcut)  # vẫn của nút này (bị nuốt lúc chạy), trả đúng khi dừng

    def setDefault(self, on: bool) -> None:
        rest = self._rest
        if rest is None:
            super().setDefault(on)
            return
        rest.default = bool(on)

    def setAutoDefault(self, on: bool) -> None:
        rest = self._rest
        if rest is None:
            super().setAutoDefault(on)
            return
        rest.auto_default = bool(on)

    def setAccessibleName(self, name: str) -> None:
        rest = self._rest
        if rest is None:
            super().setAccessibleName(name)
            return
        rest.name = name

    def click(self) -> None:
        if self._rest is None:
            super().click()

    def animateClick(self) -> None:
        if self._rest is None:
            super().animateClick()

    def hitButton(self, pos: QPoint) -> bool:
        if self._rest is not None:
            return False
        return super().hitButton(pos)

    # ---- Cỡ ------------------------------------------------------------------------------------

    def sizeHint(self) -> QSize:
        rest = self._rest
        if rest is None:
            return super().sizeHint()
        rest_size = self._size_for(rest.text, 0)
        side, gap = self._spinner_metrics()
        running_size = self._size_for(self.text(), side + gap)
        return QSize(max(rest_size.width(), running_size.width()), rest_size.height())

    def _size_for(self, text: str, extra_width: int) -> QSize:
        """Như `QPushButton::sizeHint` với chữ `text` và `extra_width` px thêm vào phần nội dung."""
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        opt.text = text
        width = height = 0
        if not self.icon().isNull():
            width += opt.iconSize.width() + 4
            height = max(height, opt.iconSize.height())
        empty = not text
        size = self.fontMetrics().size(int(Qt.TextFlag.TextShowMnemonic), "XXXX" if empty else text)
        if not empty or not width:
            width += size.width()
        if not empty or not height:
            height = max(height, size.height())
        width += extra_width
        opt.rect.setSize(QSize(width, height))
        if self.menu() is not None:
            width += self.style().pixelMetric(QStyle.PixelMetric.PM_MenuButtonIndicator, opt, self)
        return self.style().sizeFromContents(
            QStyle.ContentsType.CT_PushButton, opt, QSize(width, height), self
        )

    def _spinner_metrics(self) -> tuple[int, int]:
        """(cạnh spinner, khoảng cách tới chữ)."""
        return min(_SPINNER_MAX, self.fontMetrics().height()), theme.SPACING[1]

    # ---- Sự kiện -------------------------------------------------------------------------------

    def event(self, event: QEvent) -> bool:
        if "_levels" not in self.__dict__:  # đang dựng hay đang hủy
            return super().event(event)
        self._levels.on_event(event)
        if self._rest is not None:
            kind = event.type()
            if kind in _MOUSE_EVENTS or kind == QEvent.Type.Shortcut:
                event.accept()
                return True
            if kind in _KEY_EVENTS and isinstance(event, QKeyEvent):
                if event.key() in _ACTIVATE_KEYS:
                    event.accept()
                    return True
                if event.key() in _ENTER_KEYS:
                    self._drop_default()
                    event.ignore()  # đi tiếp lên hộp thoại cha, không bấm nút này
                    return False
        return super().event(event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in _PALETTE_EVENTS:
            self.update()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._rest is not None:
            self._watch_window()  # nút có thể đã sang cửa sổ khác
        self._maybe_restart_spinner()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._levels.reset()

    def _drop_default(self) -> None:
        """Gỡ lại default mà `QDialog` đặt lại bằng C++ lúc đang chạy (xem docstring module)."""
        if self.isDefault():
            QPushButton.setDefault(self, False)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Lúc cây widget bị hủy, PySide có thể đã xóa thuộc tính Python (xem `motion.Loop`).
        if "_rest" not in self.__dict__ or self.__dict__["_rest"] is None or not isValid(self):
            return False
        kind = event.type()
        if kind == QEvent.Type.KeyPress and isinstance(event, QKeyEvent) and event.key() in _ENTER_KEYS:
            self._drop_default()  # trước `QDialog::keyPressEvent`
        elif kind == QEvent.Type.Show or (
            kind == QEvent.Type.WindowStateChange
            and isinstance(watched, QWidget)
            and not watched.isMinimized()
        ):
            self._maybe_restart_spinner()
        return False

    # ---- Spinner -------------------------------------------------------------------------------

    def _watch_window(self) -> None:
        window = self.window()
        watched = self._watched_window() if self._watched_window is not None else None
        if window is watched:
            return
        self._unwatch_window()
        if window is self:
            return
        window.installEventFilter(self)
        self._watched_window = weakref.ref(window)

    def _unwatch_window(self) -> None:
        ref = self._watched_window
        self._watched_window = None
        window = ref() if ref is not None else None
        if window is not None and isValid(window):
            window.removeEventFilter(self)

    def _maybe_restart_spinner(self) -> None:
        if self._rest is not None and not self._spin_capped and not self._spin.running:
            self._spin.start()

    def _on_spin_step(self, value: float) -> None:
        self._spin_angle = value * 360.0
        self.update()

    def _on_spin_stop(self) -> None:
        self._spin_angle = _SPINNER_STATIC_ANGLE
        if not isValid(self):
            return
        window = self.window()
        if self._rest is not None and self.isVisible() and window.isVisible() and not window.isMinimized():
            self._spin_capped = True  # chạm trần (tự hết hay `finish_all`): không quay lại
        self.update()

    def _stop_running(self) -> None:
        rest = self._rest
        if rest is None:
            return
        self._rest = None
        self._spin.stop()
        self._spin_capped = False
        self._unwatch_window()
        self._show_text(rest.text, rest.shortcut)
        super().setAccessibleName(rest.name)
        self.setAccessibleDescription(rest.description)
        QPushButton.setAutoDefault(self, rest.auto_default)
        QPushButton.setDefault(self, rest.default)
        self._levels.blocked = False
        self._levels.sync()
        self.updateGeometry()
        self.update()

    # ---- Vẽ ------------------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        style = self.style()
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        opt.state = strip_interaction_state(opt.state)
        painter = QPainter(self)
        try:
            style.drawControl(QStyle.ControlElement.CE_PushButtonBevel, opt, painter, self)
            self._paint_overlay(painter)
            label = QStyleOptionButton(opt)
            contents = style.subElementRect(QStyle.SubElement.SE_PushButtonContents, opt, self)
            if self.content_offset():
                contents.translate(0, self.content_offset())
            if self._rest is not None:
                contents = self._paint_spinner(painter, contents)
            label.rect = contents
            style.drawControl(QStyle.ControlElement.CE_PushButtonLabel, label, painter, self)
            if opt.state & QStyle.StateFlag.State_HasFocus:
                focus = QStyleOptionFocusRect()
                focus.initFrom(self)
                focus.state = opt.state
                focus.rect = style.subElementRect(QStyle.SubElement.SE_PushButtonFocusRect, opt, self)
                style.drawPrimitive(QStyle.PrimitiveElement.PE_FrameFocusRect, focus, painter, self)
        finally:
            painter.end()

    def _paint_overlay(self, painter: QPainter) -> None:
        h, p = self._levels.hover, self._levels.press
        if (h <= 0.0 and p <= 0.0) or not self.isEnabled():
            return
        if self._primary:
            colors = theme.primary_colors(_dark())
            color = theme.blend(
                theme.blend(colors["normal"][1], colors["hover"][1], h), colors["pressed"][1], p
            )
            inset = theme.PRIMARY_BORDER_WIDTH
            radius = max(0, theme.RADIUS_CONTROL - theme.PRIMARY_BORDER_WIDTH)
        else:
            color = QColor(theme.tokens_for(self).text)
            alpha = _SECONDARY_HOVER_ALPHA * h + (_SECONDARY_PRESS_ALPHA - _SECONDARY_HOVER_ALPHA) * p
            color.setAlphaF(max(0.0, min(1.0, alpha)))
            inset = _SECONDARY_INSET
            radius = _SECONDARY_RADIUS
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        if rect.isEmpty():
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(rect, radius, radius)
        painter.restore()

    def _text_color(self) -> QColor:
        if self._primary:
            colors = theme.primary_colors(_dark())
            return QColor(colors["normal" if self.isEnabled() else "disabled"][0])
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        return QApplication.palette().color(group, QPalette.ColorRole.ButtonText)

    def _paint_spinner(self, painter: QPainter, contents: QRect) -> QRect:
        """Vẽ spinner bên trái chữ (khối căn giữa trong `contents`); trả rect cho chữ."""
        side, gap = self._spinner_metrics()
        text_width = self.fontMetrics().horizontalAdvance(self.text())
        block = side + gap + text_width
        left = contents.left() + (contents.width() - block) // 2
        top = contents.top() + (contents.height() - side) // 2
        half = _SPINNER_PEN / 2
        arc = QRectF(left, top, side, side).adjusted(half, half, -half, -half)
        pen = QPen(self._text_color(), _SPINNER_PEN)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(arc, round(-self._spin_angle * 16), _SPINNER_SPAN * 16)
        painter.restore()
        # Chữ căn giữa trong rect dịch sang phải nửa phần spinner: cả khối spinner + chữ căn giữa.
        return contents.translated((side + gap + 1) // 2, 0)


# ---- Phản hồi “đã chép” (spec G5 mục 3.4) -----------------------------------------------------------

COPIED_FEEDBACK_MS = 1500  # ms, nút hiện ✓ “Đã chép” (mvp §7.5); đọc lúc `trigger`, test vá được


class CopiedFeedback(QObject):
    """Phản hồi “đã chép” trên một nút trong `COPIED_FEEDBACK_MS`. Không phải hoạt ảnh: luôn chạy, kể cả
    khi tắt hiệu ứng hay chế độ tức thì của test (mvp N12: phản hồi trạng thái).

    Con của `button`, lọc sự kiện của nó: nút nhận `Hide` (kể cả tự phát, ví dụ thu nhỏ) → `cancel()`,
    nên mở lại trang thì nút đã về dạng thường. `QTimer` một lần chỉ chạy trong khoảng hẹn sau
    `trigger()` (mvp N3). Bên dùng tự vẽ lại nút theo `active` trong **một** hàm, gọi cả khi
    `active_changed` và khi dịch lại; closure nối vào signal chỉ được giữ tham chiếu yếu tới nút (như
    `ButtonLevels`).
    """

    active_changed = Signal(bool)

    def __init__(self, button: QWidget) -> None:
        super().__init__(button)
        self._button_ref = weakref.ref(button)
        self._active = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.cancel)
        button.installEventFilter(self)

    @property
    def active(self) -> bool:
        return self._active

    def trigger(self) -> None:
        """Bật “đã chép” (phát nếu đổi) và hẹn lại từ đầu `COPIED_FEEDBACK_MS`."""
        self._timer.start(max(0, int(COPIED_FEEDBACK_MS)))
        if not self._active:
            self._active = True
            self.active_changed.emit(True)

    def cancel(self) -> None:
        """Đang “đã chép”: dừng hẹn, trở lại dạng thường và phát; không thì thôi."""
        if not self._active:
            return
        self._timer.stop()
        self._active = False
        self.active_changed.emit(False)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # Lúc cây widget bị hủy, PySide có thể đã xóa thuộc tính Python (xem `motion.Loop`).
        if "_active" not in self.__dict__ or not self.__dict__["_active"] or not isValid(self):
            return False
        if event.type() == QEvent.Type.Hide and watched is self._button_ref():
            self.cancel()
        return False
