"""Lớp phủ thả file trên vùng luồng của `ui.chat.ChatView` (spec G5 mục 3.1): `DropOverlay`.

Chỉ import `theme`, `motion`, `icons` và PySide6. objectName `chatDropOverlay`; không nhận chuột
(`WA_TransparentForMouseEvents`), không nhận tiêu điểm.

**Bố cục** (`QVBoxLayout`, lề `theme.SPACING[3] * 2`): stretch → chỗ trống cao `_BADGE + SPACING[2]` →
`label` → stretch. `label` (`_OverlayLabel`, chữ thuần, căn giữa, xuống dòng, `tone=accent`, font
SECTION) mang câu “Thả để gửi n file tới X”, tên trợ năng = câu. Huy hiệu tự vẽ trên chỗ trống: vòng
tròn `_BADGE` px nền `accent_subtle`, icon `arrow_upload` 20 px màu `accent_text` (như `EmptyState`),
căn giữa ngang, đáy cách đỉnh `label` `SPACING[2]`.

**Vẽ** (màu lấy bằng `theme.tokens_for` lúc vẽ, mọi lớp nhân độ mờ `opacity()`): nền `surface` alpha
0,88 bo `RADIUS_BLOCK` (rect lùi `SPACING[3] / 2`); viền 2 px `accent_strong` nét đứt `_DASH`, dời theo
pha viền; huy hiệu dịch dọc `icon_offset()` px.

**Vì sao `label` tự vẽ chữ**: để chữ mờ cùng lớp phủ mà không dùng `QGraphicsEffect` (`ui/motion.py`
cấm), trong khi chữ vẫn nằm trong một `QLabel` (test đọc chữ qua nhãn, trình đọc màn hình đọc nhãn).
`_OverlayLabel.paintEvent` vẽ bằng `style().drawItemText` với `setOpacity(opacity())` của lớp phủ;
màu chữ lấy `accent_text` từ token lúc vẽ (không dựa vào palette do QSS polish), cờ = căn lề |
`TextWordWrap` | `TextExpandTabs` như `QLabel` chữ thuần, nên lúc độ mờ 1 vẽ giống `QLabel`.

**Trạng thái**: `shown` là đích (True từ `show_text` tới `dismiss`). `show_text` khi chưa `shown`: lớp
phủ đang ẩn hẳn thì hiện ở độ mờ 0 và nảy huy hiệu một lần (`motion.EMPHASIS`, nhảy lên `_HOP_PX` px
rồi đáp; OutBack vọt quá 1 nên lúc đáp huy hiệu nhích xuống dưới chỗ nghỉ chưa tới 1 px); rồi mờ vào
(`motion.FAST`) và chạy viền (`motion.Loop`, một chu kỳ nét đứt mỗi `_DASH_PERIOD_MS`, ≈ 10 px/s, tự
dừng sau ≤ `motion.MAX_LOOP_MS`, khi ẩn hay thu nhỏ; dừng thì viền đứng ở pha đang có, không tự chạy
lại). `show_text` khi đã `shown` chỉ đổi chữ (mỗi lần kéo vào chỉ một lần chạy viền). `dismiss()`: dừng
viền, mờ ra (`FAST`) rồi `hide()`; kéo vào lại giữa lúc mờ ra thì hoạt ảnh mới thay cũ, không bị ẩn
muộn, không nảy. **`text()` trả rỗng ngay khi `dismiss()`** dù hình còn đang mờ ra.

Ẩn không tự phát (đổi trang, cha ẩn) coi như `dismiss()` tức thì; ẩn tự phát (thu nhỏ) giữ nguyên.

**Chuyển động**: tắt hiệu ứng hay chế độ tức thì thì nhảy thẳng (luật ở docstring `ui/motion.py`): độ
mờ 1/0, không nảy, viền không chạy (pha 0, ảnh chụp lặp lại được), `dismiss()` ẩn ngay. Rảnh thì không
hoạt ảnh hay timer nào chạy (mvp N3). Nhịp viền chỉ vẽ lại bốn dải viền (và góc bo), bỏ nhịp khi nét
dời chưa tới `_MIN_DASH_SHIFT_PX`. Mọi callback đưa vào `motion` chỉ giữ tham chiếu yếu tới lớp phủ.
"""

from __future__ import annotations

import math
import weakref
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QHideEvent, QPainter, QPaintEvent, QPalette, QPen, QRegion
from PySide6.QtWidgets import QLabel, QStyle, QVBoxLayout, QWidget
from shiboken6 import isValid

from teleport.ui import icons, motion, theme

_BADGE = 56  # px, vòng tròn của huy hiệu
_BADGE_ICON = 20  # px, icon `arrow_upload`
_HOP_PX = 6  # px, độ nảy (≤ motion.MAX_SHIFT_PX)
_DASH = (4.0, 2.0)  # nét đứt theo bề rộng bút (bằng `Qt.DashLine`)
_DASH_PERIOD_MS = 1200  # ms, nét đứt dời một chu kỳ
_PEN = 2  # px, viền
_BACKGROUND_ALPHA = 0.88
_MIN_DASH_SHIFT_PX = 0.5  # px, nét dời ít hơn thì bỏ nhịp vẽ lại
_DASH_STRIP = 3  # px, nửa bề dày dải vẽ lại quanh viền
_PALETTE_EVENTS = (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange)


def _weak(method: Callable[..., Any]) -> Callable[..., None]:
    """Bọc phương thức của một QObject bằng tham chiếu yếu (xem sổ đăng ký ở `ui/motion.py`)."""
    ref = weakref.WeakMethod(method)

    def call(*args: Any) -> None:
        bound = ref()
        if bound is not None and isValid(getattr(bound, "__self__", None)):
            bound(*args)

    return call


def _unit(value: Any) -> float:
    # Bản riêng (không import `widgets`): lỗi → 0.0 = lớp phủ coi như ẩn; `widgets._unit` trả 1.0 = đã xong.
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


class _OverlayLabel(QLabel):
    """Nhãn của lớp phủ: tự vẽ chữ để mờ cùng lớp phủ (xem docstring module)."""

    def paintEvent(self, event: QPaintEvent) -> None:
        parent = self.parentWidget()
        opacity = parent.opacity() if isinstance(parent, DropOverlay) else 1.0
        if opacity <= 0.0:
            return
        palette = QPalette(self.palette())
        palette.setColor(QPalette.ColorRole.WindowText, theme.tokens_for(self).accent_text)
        flags = (
            int(QStyle.visualAlignment(self.layoutDirection(), self.alignment()))
            | int(Qt.TextFlag.TextWordWrap)
            | int(Qt.TextFlag.TextExpandTabs)
        )
        painter = QPainter(self)
        try:
            painter.setOpacity(opacity)
            self.style().drawItemText(
                painter,
                self.contentsRect(),
                flags,
                palette,
                self.isEnabled(),
                self.text(),
                self.foregroundRole(),
            )
        finally:
            painter.end()


class DropOverlay(QWidget):
    """Lớp phủ khi đang kéo file vào luồng (xem docstring module)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("chatDropOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._shown = False
        self._opacity = 0.0
        self._bounce = 1.0
        self._dash_phase = 0.0
        self._painted_phase = 0.0  # pha của lần vẽ lại viền gần nhất
        self.label: QLabel = _OverlayLabel(self)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setProperty(theme.TONE_PROPERTY, "accent")
        self.label.setFont(theme.font_for(theme.TextStyle.SECTION, self.label.font()))
        margin = theme.SPACING[3] * 2
        layout = QVBoxLayout(self)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(0)
        layout.addStretch(1)
        layout.addSpacing(_BADGE + theme.SPACING[2])
        layout.addWidget(self.label)
        layout.addStretch(1)
        self._dash = motion.Loop(self, _DASH_PERIOD_MS, _weak(self._set_phase))
        # Như `buttons.AnimatedButton`: khóa sổ của Loop chứa `weakref.ref`, chỉ băm được khi đối tượng
        # còn sống. Loop chưa từng `start()` thì khóa chưa băm, và lúc hủy, slot `destroyed` của nó
        # (`_registry.pop(key)`) ném TypeError. Băm sẵn ở đây (weakref nhớ giá trị băm).
        hash(self._dash._key)
        self.setVisible(False)

    # ---- API -----------------------------------------------------------------------------------

    @property
    def shown(self) -> bool:
        """Đích: True từ `show_text` tới `dismiss` (hình có thể còn đang mờ ra)."""
        return self._shown

    def text(self) -> str:
        """Chữ đang hiện khi `shown`, "" khi không."""
        return self.label.text() if self._shown else ""

    def opacity(self) -> float:
        return self._opacity

    def icon_offset(self) -> float:
        """Độ dịch dọc của huy hiệu (px, âm = lên trên); 0 khi không nảy."""
        bounce = self._bounce
        if bounce >= 1.0 or bounce <= 0.0:
            return 0.0
        return round(-_HOP_PX * math.sin(math.pi * bounce), 3)

    def dash_running(self) -> bool:
        return self._dash.running

    def show_text(self, text: str) -> None:
        """Hiện lớp phủ với câu `text` (đã hiện thì chỉ đổi câu)."""
        self.label.setText(text)
        self.label.setAccessibleName(text)
        if self._shown:
            return
        self._shown = True
        if self.isHidden():
            self._opacity = 0.0
            self._bounce = 0.0
            self.show()
            self.raise_()
            motion.animate_value(self, 0.0, 1.0, motion.EMPHASIS, _weak(self._set_bounce), key="bounce")
        motion.animate_value(self, self._opacity, 1.0, motion.FAST, _weak(self._set_opacity), key="opacity")
        self._dash.start()

    def dismiss(self) -> None:
        """Mờ ra rồi ẩn; `text()` rỗng ngay."""
        if not self._shown:
            return
        self._shown = False
        self._dash.stop()
        motion.animate_value(
            self,
            self._opacity,
            0.0,
            motion.FAST,
            _weak(self._set_opacity),
            key="opacity",
            on_finished=_weak(self._after_fade),
        )

    # ---- Bên trong -----------------------------------------------------------------------------

    def _set_opacity(self, value: Any) -> None:
        self._opacity = _unit(value)
        self.update()

    def _set_bounce(self, value: Any) -> None:
        # Không kẹp: OutBack vọt quá 1 rồi về (huy hiệu đáp xuống dưới chỗ nghỉ chưa tới 1 px).
        try:
            self._bounce = float(value)
        except (TypeError, ValueError):
            self._bounce = 1.0
        self.update()

    def _set_phase(self, value: float) -> None:
        self._dash_phase = _unit(value)
        shift = abs(self._dash_phase - self._painted_phase) % 1.0
        shift = min(shift, 1.0 - shift) * sum(_DASH) * _PEN  # px, theo vòng chu kỳ
        if shift < _MIN_DASH_SHIFT_PX:
            return
        self._painted_phase = self._dash_phase
        self.update(self._border_region())

    def _after_fade(self) -> None:
        if not self._shown:
            self.hide()

    def _frame_rect(self) -> QRectF:
        inset = theme.SPACING[3] / 2
        return QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)

    def _border_region(self) -> QRegion:
        """Bốn dải quanh viền (±`_DASH_STRIP` px) và bốn góc bo."""
        frame = self._frame_rect().toAlignedRect()
        strip = _DASH_STRIP
        outer = frame.adjusted(-strip, -strip, strip, strip)
        inner = frame.adjusted(strip, strip, -strip, -strip)
        region = QRegion(outer)
        if inner.isValid():
            region = region.subtracted(QRegion(inner))
        corner = theme.RADIUS_BLOCK + strip
        for x, y in (
            (outer.left(), outer.top()),
            (outer.right() - corner + 1, outer.top()),
            (outer.left(), outer.bottom() - corner + 1),
            (outer.right() - corner + 1, outer.bottom() - corner + 1),
        ):
            region = region.united(QRegion(QRect(x, y, corner, corner)))
        return region

    def _badge_rect(self) -> QRectF:
        label = self.label.geometry()
        top = label.top() - theme.SPACING[2] - _BADGE + self.icon_offset()
        return QRectF(self.width() / 2 - _BADGE / 2, top, _BADGE, _BADGE)

    def paintEvent(self, event: QPaintEvent) -> None:
        opacity = self._opacity
        if opacity <= 0.0:
            return
        t = theme.tokens_for(self)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setOpacity(opacity)
            background = t.surface
            background.setAlphaF(_BACKGROUND_ALPHA)
            painter.setBrush(background)
            pen = QPen(t.accent_strong, _PEN)
            pen.setDashPattern(list(_DASH))
            pen.setDashOffset(-self._dash_phase * sum(_DASH))
            painter.setPen(pen)
            painter.drawRoundedRect(self._frame_rect(), theme.RADIUS_BLOCK, theme.RADIUS_BLOCK)

            badge = self._badge_rect()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(t.accent_subtle)
            painter.drawEllipse(badge)
            pm = icons.pixmap("arrow_upload", _BADGE_ICON, t.accent_text, self.devicePixelRatioF())
            if not pm.isNull():
                center = badge.center()
                painter.drawPixmap(QPointF(center.x() - _BADGE_ICON / 2, center.y() - _BADGE_ICON / 2), pm)
        finally:
            painter.end()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        # Lúc cây widget bị hủy, PySide có thể đã xóa thuộc tính Python mà Qt vẫn gửi sự kiện: bỏ qua.
        if "_dash" not in self.__dict__ or not isValid(self):
            return
        if event.spontaneous():  # thu nhỏ: giữ nguyên (Loop tự dừng)
            return
        self._shown = False
        self._dash.stop()
        motion.animate_value(self, self._bounce, 1.0, motion.INSTANT, _weak(self._set_bounce), key="bounce")
        motion.animate_value(
            self,
            self._opacity,
            0.0,
            motion.INSTANT,
            _weak(self._set_opacity),
            key="opacity",
            on_finished=_weak(self._after_fade),
        )

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in _PALETTE_EVENTS:
            self.update()
