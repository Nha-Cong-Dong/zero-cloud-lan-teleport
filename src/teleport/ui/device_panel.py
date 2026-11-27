"""Bảng thông tin và thao tác nhanh bên phải luồng trò chuyện (`DeviceInfoPanel`).

Hiển thị hồ sơ máy đang chọn (avatar, tên, trạng thái, nhãn kết nối mã hóa an toàn),
hàng 3 nút tròn thao tác nhanh (Gửi file, Tìm kiếm, Cài đặt) và các nhóm tùy chọn
đóng/mở gọn gàng dùng `HeightAnimator`, tôn trọng bảng màu sáng/tối từ `ui.theme`,
âm thanh nhẹ từ `ui.sounds` và quy chuẩn trợ năng (chữ thuần, không HTML, đủ vai).
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QRectF, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QHideEvent,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from teleport.ui import icons, motion, sounds, texts, theme
from teleport.ui.buttons import ButtonLevels
from teleport.ui.sounds import SoundCue
from teleport.ui.widgets import DeviceAvatar, Presence, ToggleSwitch, safe_tooltip

PANEL_WIDTH = 268
_HEADER_AVATAR = 60
_CIRCLE_SIZE = 38
_ACTION_ICON_SIZE = 18
_PILL_ICON_SIZE = 14
_ASSET_ICON_SIZE = 20
_PALETTE_EVENTS = (
    QEvent.Type.PaletteChange,
    QEvent.Type.ApplicationPaletteChange,
    QEvent.Type.StyleChange,
)
_ENTER_KEYS = (Qt.Key.Key_Enter, Qt.Key.Key_Return)


def _demibold(font: QFont) -> QFont:
    out = QFont(font)
    out.setWeight(QFont.Weight.DemiBold)
    return out


def _plain_label(
    text: str = "",
    parent: QWidget | None = None,
    *,
    tone: str | None = None,
    style: theme.TextStyle | None = None,
    align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft,
    wrap: bool = True,
) -> QLabel:
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    label.setAlignment(align)
    label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    if style is not None:
        label.setFont(theme.font_for(style, label.font()))
    if tone is not None:
        label.setProperty(theme.TONE_PROPERTY, tone)
    return label


class _EncryptedPill(QWidget):
    """Nhãn viên thuốc nhỏ gọn hiển thị biểu tượng ổ khóa và “Kết nối mã hóa an toàn”."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("devicePanelEncryptedPill")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 12, 4)
        layout.setSpacing(6)
        self._icon = QLabel(self)
        self._icon.setFixedSize(_PILL_ICON_SIZE, _PILL_ICON_SIZE)
        self._icon.setScaledContents(True)
        self._icon.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._label = _plain_label("", self, tone="muted", style=theme.TextStyle.CAPTION, wrap=False)
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.retranslate()

    def retranslate(self) -> None:
        t = QCoreApplication.translate
        text = t("DevicePanel", "Kết nối mã hóa an toàn")
        self._label.setText(text)
        self.setAccessibleName(text)
        self._refresh_icon()
        self.update()

    def _refresh_icon(self) -> None:
        t = theme.tokens_for(self)
        pix = icons.pixmap("lock_closed", _ASSET_ICON_SIZE, t.text_muted, self.devicePixelRatioF())
        self._icon.setPixmap(pix)

    def paintEvent(self, event: QPaintEvent) -> None:
        t = theme.tokens_for(self)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(t.border, 1))
            painter.setBrush(t.surface_raised)
            painter.drawRoundedRect(rect, radius, radius)
        finally:
            painter.end()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if "_icon" not in self.__dict__:
            return
        if event.type() in _PALETTE_EVENTS:
            self._refresh_icon()
            self.update()
        elif event.type() == QEvent.Type.LanguageChange:
            self.retranslate()


def _has_keyboard_focus(widget: QWidget) -> bool:
    return widget.hasFocus() and widget.window().testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange)


class _CircleButton(QAbstractButton):
    """Nút tròn thao tác nhanh với icon đơn sắc và chuyển màu mượt theo hover/ấn."""

    def __init__(self, icon_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._levels = ButtonLevels(self)
        self.setFixedSize(_CIRCLE_SIZE, _CIRCLE_SIZE)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def sizeHint(self) -> QSize:
        return QSize(_CIRCLE_SIZE, _CIRCLE_SIZE)

    def event(self, event: QEvent) -> bool:
        levels = self.__dict__.get("_levels")
        if levels is not None:
            levels.on_event(event)
        return super().event(event)

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._levels.reset()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in _ENTER_KEYS and not event.isAutoRepeat():
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        t = theme.tokens_for(self)
        dark = theme.is_dark_palette(QApplication.palette())
        h, p = self._levels.hover, self._levels.press
        bg = theme.blend(t.surface_raised, t.selected, h)
        if p > 0:
            bg = theme.blend(bg, t.border, p * 0.65)
        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            if _has_keyboard_focus(self):
                painter.setPen(QPen(theme.focus_ring_color(dark), 2))
            else:
                painter.setPen(QPen(t.border, 1))
            painter.setBrush(bg)
            painter.drawEllipse(rect)
            color = t.text if self.isEnabled() else t.text_muted
            pix = icons.pixmap(self._icon_name, _ASSET_ICON_SIZE, color, self.devicePixelRatioF())
            x = (self.width() - _ACTION_ICON_SIZE) / 2
            y = (self.height() - _ACTION_ICON_SIZE) / 2 + p * 0.5
            painter.drawPixmap(
                QRectF(x, y, _ACTION_ICON_SIZE, _ACTION_ICON_SIZE),
                pix,
                QRectF(0, 0, pix.width(), pix.height()),
            )
        finally:
            painter.end()


class _CircleAction(QWidget):
    """Một nút tròn kèm nhãn chú thích ngắn bên dưới."""

    clicked = Signal()

    def __init__(self, icon_name: str, object_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.button = _CircleButton(icon_name, self)
        self.button.setObjectName(object_name)
        self.button.clicked.connect(self.clicked)
        self.label = _plain_label(
            "",
            self,
            tone="muted",
            style=theme.TextStyle.CAPTION,
            align=Qt.AlignmentFlag.AlignCenter,
            wrap=True,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(5)
        layout.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.label)

    def set_text(self, label: str, tooltip: str | None = None) -> None:
        self.label.setText(label)
        self.button.setAccessibleName(tooltip or label)
        self.button.setToolTip(safe_tooltip(tooltip or label))


class _PanelActionRow(QAbstractButton):
    """Hàng thao tác có icon đơn sắc bên trái, chữ thuần và nền hover chuyển dần."""

    def __init__(
        self,
        icon_name: str,
        object_name: str,
        parent: QWidget | None = None,
        *,
        danger: bool = False,
    ) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._danger = danger
        self._levels = ButtonLevels(self)
        self.setObjectName(object_name)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_label(self, text: str) -> None:
        self.setText(text)
        self.setAccessibleName(text)
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        h = max(34, metrics.height() + 14)
        w = max(120, metrics.horizontalAdvance(self.text()) + 48)
        return QSize(w, h)

    def event(self, event: QEvent) -> bool:
        levels = self.__dict__.get("_levels")
        if levels is not None:
            levels.on_event(event)
        return super().event(event)

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._levels.reset()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in _ENTER_KEYS and not event.isAutoRepeat():
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        t = theme.tokens_for(self)
        dark = theme.is_dark_palette(QApplication.palette())
        h, p = self._levels.hover, self._levels.press
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
            if h > 0.0 or p > 0.0:
                clear = QColor(t.selected)
                clear.setAlpha(0)
                bg = theme.blend(theme.blend(clear, t.selected, h), t.border, p * 0.5)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(bg)
                painter.drawRoundedRect(rect, theme.RADIUS_CONTROL, theme.RADIUS_CONTROL)
            if _has_keyboard_focus(self):
                painter.setPen(QPen(theme.focus_ring_color(dark), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect, theme.RADIUS_CONTROL, theme.RADIUS_CONTROL)
            fg = t.danger if self._danger else (t.text if self.isEnabled() else t.text_muted)
            pix = icons.pixmap(self._icon_name, _ASSET_ICON_SIZE, fg, self.devicePixelRatioF())
            iy = (self.height() - _ACTION_ICON_SIZE) / 2
            painter.drawPixmap(
                QRectF(10.0, iy, _ACTION_ICON_SIZE, _ACTION_ICON_SIZE),
                pix,
                QRectF(0, 0, pix.width(), pix.height()),
            )
            text_x = 10.0 + _ACTION_ICON_SIZE + 10.0
            text_rect = QRectF(text_x, 0.0, max(0.0, self.width() - text_x - 8.0), float(self.height()))
            metrics = QFontMetrics(self.font())
            elided = metrics.elidedText(self.text(), Qt.TextElideMode.ElideRight, int(text_rect.width()))
            painter.setPen(fg)
            painter.drawText(
                text_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                elided,
            )
        finally:
            painter.end()


class _AccordionHeader(QAbstractButton):
    """Tiêu đề mục đóng/mở kèm mũi tên vẽ vector tối giản bên phải."""

    def __init__(self, object_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._expanded = True
        self._levels = ButtonLevels(self)
        self.setObjectName(object_name)
        self.setCheckable(True)
        self.setChecked(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFont(_demibold(theme.font_for(theme.TextStyle.CAPTION, self.font())))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        with QSignalBlocker(self):
            self.setChecked(self._expanded)
        self.update()

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        return QSize(140, max(32, metrics.height() + 12))

    def event(self, event: QEvent) -> bool:
        levels = self.__dict__.get("_levels")
        if levels is not None:
            levels.on_event(event)
        return super().event(event)

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._levels.reset()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in _ENTER_KEYS and not event.isAutoRepeat():
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        t = theme.tokens_for(self)
        dark = theme.is_dark_palette(QApplication.palette())
        h = self._levels.hover
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
            if h > 0.0:
                clear = QColor(t.selected)
                clear.setAlpha(0)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.blend(clear, t.selected, h))
                painter.drawRoundedRect(rect, theme.RADIUS_CONTROL, theme.RADIUS_CONTROL)
            if _has_keyboard_focus(self):
                painter.setPen(QPen(theme.focus_ring_color(dark), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect, theme.RADIUS_CONTROL, theme.RADIUS_CONTROL)
            painter.setFont(self.font())
            painter.setPen(t.text)
            text_rect = QRectF(10.0, 0.0, max(0.0, self.width() - 34.0), float(self.height()))
            metrics = QFontMetrics(self.font())
            elided = metrics.elidedText(self.text(), Qt.TextElideMode.ElideRight, int(text_rect.width()))
            painter.drawText(
                text_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                elided,
            )
            # Mũi tên chữ V tối giản (stroke 1.8 px) quay lên khi mở, quay xuống khi đóng.
            cx = float(self.width()) - 16.0
            cy = float(self.height()) / 2.0
            arrow = QPainterPath()
            if self._expanded:
                arrow.moveTo(cx - 4.0, cy + 2.0)
                arrow.lineTo(cx, cy - 2.0)
                arrow.lineTo(cx + 4.0, cy + 2.0)
            else:
                arrow.moveTo(cx - 4.0, cy - 2.0)
                arrow.lineTo(cx, cy + 2.0)
                arrow.lineTo(cx + 4.0, cy - 2.0)
            pen = QPen(
                t.text_muted,
                1.8,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(arrow)
        finally:
            painter.end()


class _AccordionSection(QWidget):
    """Một nhóm thao tác có tiêu đề bấm để thu gọn/mở rộng mượt bằng `HeightAnimator`."""

    def __init__(self, object_name: str, parent: QWidget | None = None, *, expanded: bool = True) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._expanded = bool(expanded)
        self.header = _AccordionHeader(f"{object_name}Header", self)
        self.header.set_expanded(self._expanded)
        self.header.clicked.connect(self._toggle)

        self.body = QWidget(self)
        self.body.setObjectName(f"{object_name}Body")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 2, 0, 4)
        self.body_layout.setSpacing(2)
        self.body.setVisible(self._expanded)
        self._anim = motion.HeightAnimator(self.body)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.header)
        layout.addWidget(self.body)

    @property
    def expanded(self) -> bool:
        return self._expanded

    def set_title(self, title: str) -> None:
        self.header.setText(title)
        self.header.setAccessibleName(title)
        self.header.update()

    def set_expanded(self, expanded: bool, *, animate: bool = True) -> None:
        target = bool(expanded)
        if target == self._expanded:
            return
        self._expanded = target
        self.header.set_expanded(target)
        if not animate:
            self.body.setVisible(target)
            return
        if target:
            self._anim.expand()
        else:
            self._anim.collapse()

    def _toggle(self, _checked: bool = False) -> None:
        sounds.play(SoundCue.TOGGLE_TICK, window_visible=self.isVisible())
        self.set_expanded(not self._expanded, animate=True)


class DeviceInfoPanel(QFrame):
    """Cột thông tin & thao tác nhanh bên phải luồng trò chuyện (không trùng lặp nút bên trái)."""

    search_requested = Signal()
    retry_connect_requested = Signal()
    device_settings_requested = Signal()
    connect_ip_requested = Signal()
    clear_history_requested = Signal()
    unpair_requested = Signal()
    clipboard_toggled = Signal(bool)
    auto_accept_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("deviceInfoPanel")
        self.setMaximumWidth(PANEL_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._key: str | None = None
        self._os_name: str | None = None

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("deviceInfoScroll")
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        inner = QWidget()
        inner.setObjectName("deviceInfoContent")
        col = QVBoxLayout(inner)
        col.setContentsMargins(theme.SPACING[2], theme.SPACING[3], theme.SPACING[2], theme.SPACING[3])
        col.setSpacing(theme.SPACING[2])

        # ---- Hồ sơ máy -------------------------------------------------------------------------
        self.avatar = DeviceAvatar(inner, diameter=_HEADER_AVATAR, presence=Presence.OFFLINE, ring="surface")
        self.name_label = _plain_label(
            "",
            inner,
            style=theme.TextStyle.SECTION,
            align=Qt.AlignmentFlag.AlignHCenter,
        )
        self.name_label.setObjectName("devicePanelName")
        self.status_label = _plain_label(
            "",
            inner,
            tone="muted",
            style=theme.TextStyle.CAPTION,
            align=Qt.AlignmentFlag.AlignHCenter,
        )
        self.status_label.setObjectName("devicePanelStatus")
        self.encrypted_pill = _EncryptedPill(inner)

        col.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(self.name_label)
        col.addWidget(self.status_label)
        col.addWidget(self.encrypted_pill, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addSpacing(theme.SPACING[1])

        # ---- Hàng 3 nút tròn thao tác nhanh ---------------------------------------------------
        quick_row = QHBoxLayout()
        quick_row.setContentsMargins(0, 0, 0, 0)
        quick_row.setSpacing(theme.SPACING[1])
        self.quick_search_action = _CircleAction("search", "devicePanelQuickSearch", inner)
        self.quick_retry_action = _CircleAction("arrow_clockwise", "devicePanelQuickRetry", inner)
        self.quick_settings_action = _CircleAction("settings", "devicePanelQuickSettings", inner)
        self.quick_search_action.clicked.connect(self.search_requested)
        self.quick_retry_action.clicked.connect(self.retry_connect_requested)
        self.quick_settings_action.clicked.connect(self.device_settings_requested)
        quick_row.addStretch(1)
        quick_row.addWidget(self.quick_search_action)
        quick_row.addWidget(self.quick_retry_action)
        quick_row.addWidget(self.quick_settings_action)
        quick_row.addStretch(1)
        col.addLayout(quick_row)
        col.addSpacing(theme.SPACING[1])

        # ---- Nhóm 1: Tùy chỉnh cho máy này ----------------------------------------------------
        self.prefs_section = _AccordionSection("devicePanelPrefs", inner, expanded=True)
        clip_row_w = QWidget(self.prefs_section.body)
        clip_row = QHBoxLayout(clip_row_w)
        clip_row.setContentsMargins(10, 4, 8, 4)
        clip_row.setSpacing(theme.SPACING[1])
        self.clip_switch_label = _plain_label("", clip_row_w)
        self.clipboard_switch = ToggleSwitch(clip_row_w)
        self.clipboard_switch.setObjectName("panelClipboardSwitch")
        self.clipboard_switch.toggled.connect(self.clipboard_toggled)
        clip_row.addWidget(self.clip_switch_label, 1)
        clip_row.addWidget(self.clipboard_switch, 0, Qt.AlignmentFlag.AlignVCenter)
        self.prefs_section.body_layout.addWidget(clip_row_w)

        auto_row_w = QWidget(self.prefs_section.body)
        auto_row = QHBoxLayout(auto_row_w)
        auto_row.setContentsMargins(10, 4, 8, 4)
        auto_row.setSpacing(theme.SPACING[1])
        self.auto_switch_label = _plain_label("", auto_row_w)
        self.auto_accept_switch = ToggleSwitch(auto_row_w)
        self.auto_accept_switch.setObjectName("panelAutoAcceptSwitch")
        self.auto_accept_switch.toggled.connect(self.auto_accept_toggled)
        auto_row.addWidget(self.auto_switch_label, 1)
        auto_row.addWidget(self.auto_accept_switch, 0, Qt.AlignmentFlag.AlignVCenter)
        self.prefs_section.body_layout.addWidget(auto_row_w)
        col.addWidget(self.prefs_section)

        # ---- Nhóm 2: Kết nối và bảo mật --------------------------------------------------------
        self.security_section = _AccordionSection("devicePanelSecurity", inner, expanded=True)
        self.meta_label = _plain_label(
            "",
            self.security_section.body,
            tone="muted",
            style=theme.TextStyle.CAPTION,
        )
        self.meta_label.setObjectName("devicePanelMeta")
        self.meta_label.setContentsMargins(10, 4, 8, 4)
        self.meta_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.meta_label.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.security_section.body_layout.addWidget(self.meta_label)

        self.connect_ip_row = _PanelActionRow("globe", "devicePanelConnectIp", self.security_section.body)
        self.connect_ip_row.clicked.connect(self.connect_ip_requested)
        self.security_section.body_layout.addWidget(self.connect_ip_row)

        self.clear_history_row = _PanelActionRow(
            "delete", "devicePanelClearHistory", self.security_section.body
        )
        self.clear_history_row.clicked.connect(self.clear_history_requested)
        self.security_section.body_layout.addWidget(self.clear_history_row)

        self.unpair_row = _PanelActionRow(
            "link_dismiss", "devicePanelUnpair", self.security_section.body, danger=True
        )
        self.unpair_row.clicked.connect(self.unpair_requested)
        self.security_section.body_layout.addWidget(self.unpair_row)
        col.addWidget(self.security_section)

        col.addStretch(1)
        self.scroll_area.setWidget(inner)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.scroll_area)

        self.retranslate()

    def sizeHint(self) -> QSize:
        base = super().sizeHint()
        return QSize(PANEL_WIDTH, base.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def set_device(
        self,
        name: str,
        status: str,
        presence: Presence,
        *,
        key: str | None = None,
        os_name: str | None = None,
    ) -> None:
        """Cập nhật máy đang hiển thị ở bảng thông tin bên phải."""
        same = key is not None and key == self._key
        self._key = key
        self._os_name = os_name
        self.name_label.setText(name)
        self.status_label.setText(status)
        self.status_label.setVisible(bool(status))
        self.avatar.set_presence(presence, animate=same)
        self._update_meta()
        self.setAccessibleName(f"{name}. {status}" if status else name)

    def set_quick_state(self, *, clipboard: bool, auto_accept: bool) -> None:
        """Đồng bộ trạng thái hai công tắc mà không phát signal ngược về controller."""
        with QSignalBlocker(self.clipboard_switch):
            self.clipboard_switch.setChecked(bool(clipboard))
        with QSignalBlocker(self.auto_accept_switch):
            self.auto_accept_switch.setChecked(bool(auto_accept))

    def _update_meta(self) -> None:
        t = QCoreApplication.translate
        parts: list[str] = []
        if self._key:
            parts.append(texts.fill(t("DevicePanel", "Mã máy: {id8}"), id8=self._key[:8]))
        os_label = texts.os_label(self._os_name or "")
        if os_label:
            parts.append(os_label)
        text = "\n".join(parts)
        self.meta_label.setText(text)
        self.meta_label.setAccessibleName(text)
        self.meta_label.setVisible(bool(text))

    def retranslate(self) -> None:
        t = QCoreApplication.translate
        self.encrypted_pill.retranslate()
        self.quick_search_action.set_text(
            t("DevicePanel", "Tìm kiếm"),
            t("DevicePanel", "Tìm trong cuộc trò chuyện"),
        )
        self.quick_retry_action.set_text(
            t("DevicePanel", "Kết nối lại"),
            t("DevicePanel", "Thử kết nối lại với máy này"),
        )
        self.quick_settings_action.set_text(
            t("DevicePanel", "Cài đặt"),
            t("DevicePanel", "Cài đặt cho thiết bị này"),
        )

        self.prefs_section.set_title(t("DevicePanel", "Tùy chỉnh cho máy này"))
        clip_text = t("DevicePanel", "Đồng bộ clipboard")
        self.clip_switch_label.setText(clip_text)
        self.clipboard_switch.setAccessibleName(clip_text)
        self.clipboard_switch.retranslate()
        auto_text = t("DevicePanel", "Tự động nhận file")
        self.auto_switch_label.setText(auto_text)
        self.auto_accept_switch.setAccessibleName(auto_text)
        self.auto_accept_switch.retranslate()

        self.security_section.set_title(t("DevicePanel", "Kết nối và bảo mật"))
        self._update_meta()
        self.connect_ip_row.set_label(t("DevicePanel", "Kết nối bằng IP…"))
        self.clear_history_row.set_label(t("DevicePanel", "Xóa lịch sử với máy này…"))
        self.unpair_row.set_label(t("DevicePanel", "Hủy ghép đôi…"))

    def paintEvent(self, event: QPaintEvent) -> None:
        t = theme.tokens_for(self)
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), t.surface)
            painter.setPen(QPen(t.border, 1))
            painter.drawLine(0, 0, 0, self.height())
        finally:
            painter.end()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if "_key" not in self.__dict__:
            return
        if event.type() in _PALETTE_EVENTS:
            self.update()
        elif event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
