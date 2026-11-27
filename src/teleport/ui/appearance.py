"""Mục “Giao diện” của trang Cài đặt (spec D6 mục 2.5, 3; mvp F10, F11, N12).

`AppearanceSection` là **view**: không bao giờ gọi node. Người dùng chọn gì thì phát signal, còn
`set_*` đặt giá trị mà **không** phát signal. `ui.settings.SettingsPage` nhúng nó vào mục
`Section.APPEARANCE` và chuyển tiếp signal; `SettingsController` lưu qua node rồi mới áp
(`theme.set_preference`, `i18n.apply_preference`).

**Ba hàng** (`widgets.SettingsRow`):
- **Chế độ màu** (`themeRow`): `widgets.SegmentedControl` (`themeControl`) ba nút “Theo hệ điều hành”
  · “Sáng” · “Tối” (giá trị `theme.Mode`). `theme_selected` phát giá trị trường `theme` của
  `settings.json`: None (theo hệ điều hành) / `"light"` / `"dark"`.
- **Màu nhấn** (`accentRow`): `AccentPicker` (`accentPicker`) sáu `AccentSwatch` theo `theme.ACCENTS`,
  dòng “Đang dùng: {tên}” (`accentCurrent`) dưới gợi ý. `accent_selected` phát mã màu, riêng
  `theme.DEFAULT_ACCENT` phát None (mặc định thì không ghi vào `settings.json`). Mã đã lưu mà lạ (bản
  mới hơn ghi) thì hiện màu mặc định.
- **Ngôn ngữ** (`languageRow`, chuyển từ mục Chung của spec D2): `QComboBox` (`languageCombo`) “Theo hệ
  điều hành” rồi tên tự gọi của từng ngôn ngữ (`i18n.language_name`); mã đã lưu không có bản dịch vẫn
  hiện (bằng chính mã). `language_selected` phát mã hay None.

**`AccentSwatch`** (`QAbstractButton` checkable, vùng bấm `SWATCH_SIZE`, vòng tròn `SWATCH_DIAMETER`
tô `theme.accent_swatch`): ô đang chọn có icon `checkmark` màu `HighlightedText` của bảng màu ở giữa
và vòng trong 2 px màu `text` sát mép vòng tròn (lựa chọn không chỉ nói bằng màu); tiêu điểm là vòng
2 px `theme.focus_ring_color` cách vòng tròn 2 px, ngoài vòng chọn nên hai dấu hiệu không lẫn nhau.
Tên trợ năng và tooltip = tên màu (`theme.accent_name`), mô tả trợ năng “Màu nhấn”. Màu đọc lúc vẽ
từ bảng màu ứng dụng, vẽ lại khi nhận `PaletteChange` / `ApplicationPaletteChange`.

**Bàn phím** (mvp N12): mỗi nhóm (chế độ, màu) là một `widgets.RovingGroup`: Tab chỉ dừng ở phần tử
đang chọn, mũi tên trái/phải (lên/xuống) đi và chọn luôn, Home/End tới đầu/cuối. `focus_widgets()`
trả mọi control theo thứ tự để trang nối chuỗi Tab.

`set_loaded(False)` tắt cả ba hàng (lưu qua node, chưa có ảnh chụp cài đặt thì chưa dùng được). Câu
chữ ở ngữ cảnh `Appearance` (tên màu ở ngữ cảnh `Theme`), gọi `QCoreApplication.translate` lúc dùng;
`retranslate()` khi nhận `LanguageChange`, giữ lựa chọn đang có.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QCoreApplication, QEvent, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from teleport.ui import i18n, icons, sounds, texts, theme
from teleport.ui.buttons import AnimatedButton
from teleport.ui.sounds import SoundCue, SoundMode
from teleport.ui.widgets import RovingGroup, SegmentedControl, SettingsRow, ToggleSwitch, safe_tooltip

SWATCH_SIZE = 36  # px, vùng bấm của một ô màu (≥ 24, WCAG 2.5.8)
SWATCH_DIAMETER = 28  # px, vòng tròn màu
_SWATCH_SPACING = theme.SPACING[0]  # 4 px giữa hai ô
_RING = 2  # px, vòng chọn (trong) và vòng tiêu điểm (ngoài)
_FOCUS_GAP = 2  # px, từ vòng tròn màu tới vòng tiêu điểm
_CHECK_ICON = 20  # px (`checkmark` chỉ có cỡ 20)
_DISABLED_OPACITY = 0.5
_PALETTE_EVENTS = (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange)
MODES: tuple[theme.Mode, ...] = (theme.Mode.SYSTEM, theme.Mode.LIGHT, theme.Mode.DARK)  # thứ tự nút


# ---- Câu chữ -----------------------------------------------------------------------------------


def section_title() -> str:
    return QCoreApplication.translate("Appearance", "Giao diện")


def mode_text(mode: theme.Mode) -> str:
    t = QCoreApplication.translate
    return {
        theme.Mode.SYSTEM: t("Appearance", "Theo hệ điều hành"),
        theme.Mode.LIGHT: t("Appearance", "Sáng"),
        theme.Mode.DARK: t("Appearance", "Tối"),
    }[theme.Mode(mode)]


def current_accent_text(accent: str) -> str:
    """“Đang dùng: {tên}” của màu nhấn `accent`."""
    return texts.fill(
        QCoreApplication.translate("Appearance", "Đang dùng: {name}"), name=theme.accent_name(accent)
    )


# ---- Ô màu -------------------------------------------------------------------------------------


class AccentSwatch(QAbstractButton):
    """Một ô màu nhấn tròn (xem docstring module); `accent` là mã trong `theme.ACCENTS`."""

    def __init__(self, accent: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._accent = accent
        self.setObjectName(f"accentSwatch_{accent}")
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(SWATCH_SIZE, SWATCH_SIZE)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.toggled.connect(lambda _checked: self.update())
        self.retranslate()

    @property
    def accent(self) -> str:
        return self._accent

    def sizeHint(self) -> QSize:
        return QSize(SWATCH_SIZE, SWATCH_SIZE)

    def retranslate(self) -> None:
        name = theme.accent_name(self._accent)
        self.setAccessibleName(name)
        self.setToolTip(safe_tooltip(name))
        self.setAccessibleDescription(QCoreApplication.translate("Appearance", "Màu nhấn"))

    def color(self) -> object:
        """Màu vòng tròn theo chế độ đang áp (`theme.accent_swatch`)."""
        return theme.accent_swatch(self._accent, theme.is_dark_palette(QApplication.palette()))

    def paintEvent(self, event: QPaintEvent) -> None:
        pal = QApplication.palette()
        dark = theme.is_dark_palette(pal)
        t = theme.tokens(dark)
        center = QRectF(self.rect()).center()
        radius = SWATCH_DIAMETER / 2
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            if self.hasFocus():
                ring = radius + _FOCUS_GAP + _RING / 2
                painter.setPen(QPen(theme.focus_ring_color(dark), _RING))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(center, ring, ring)
            if not self.isEnabled():
                painter.setOpacity(_DISABLED_OPACITY)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.accent_swatch(self._accent, dark))
            painter.drawEllipse(center, radius, radius)
            if self.isChecked():
                inner = radius - _RING / 2
                painter.setPen(QPen(t.text, _RING))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(center, inner, inner)
                mark = icons.pixmap(
                    "checkmark",
                    _CHECK_ICON,
                    pal.color(QPalette.ColorRole.HighlightedText),
                    self.devicePixelRatioF(),
                )
                if not mark.isNull():
                    half = _CHECK_ICON / 2
                    painter.drawPixmap(
                        QRectF(center.x() - half, center.y() - half, _CHECK_ICON, _CHECK_ICON),
                        mark,
                        QRectF(mark.rect()),
                    )
        finally:
            painter.end()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in _PALETTE_EVENTS:
            self.update()
        elif event.type() == QEvent.Type.LanguageChange:
            self.retranslate()


class AccentPicker(QWidget):
    """Sáu ô màu nhấn theo `theme.ACCENTS`, chọn một (xem docstring module)."""

    accent_chosen = Signal(str)  # mã màu người dùng vừa chọn (khác mã đang có)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._roving = RovingGroup(self)
        self._swatches: list[AccentSwatch] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_SWATCH_SPACING)
        for accent in theme.ACCENTS:
            swatch = AccentSwatch(accent, self)
            layout.addWidget(swatch)
            self._roving.add(swatch)
            self._swatches.append(swatch)
        self._accent = theme.DEFAULT_ACCENT
        self._stale = False  # mã đã lưu lạ: bấm ô mặc định vẫn phát
        self._roving.group.idClicked.connect(self._on_clicked)
        self.set_accent(theme.DEFAULT_ACCENT)

    def swatches(self) -> list[AccentSwatch]:
        return list(self._swatches)

    def swatch(self, accent: str) -> AccentSwatch:
        """Ô của mã `accent`; mã lạ → `ValueError`."""
        return self._swatches[theme.ACCENTS.index(accent)]

    def accent(self) -> str:
        """Mã đang chọn (luôn trong `theme.ACCENTS`)."""
        return self._accent

    def set_accent(self, accent: str | None) -> None:
        """Chọn ô (không phát signal); None hay mã lạ → `theme.DEFAULT_ACCENT`.

        Mã lạ nhưng đúng dạng (bản mới hơn ghi) hiện ô mặc định; bấm ô đó vẫn phát, để người dùng xóa
        được mã lạ về mặc định."""
        self._accent = theme.accent_from_setting(accent)
        self._stale = accent is not None and accent != self._accent
        self._roving.set_checked(theme.ACCENTS.index(self._accent))

    def retranslate(self) -> None:
        for swatch in self._swatches:
            swatch.retranslate()

    def _on_clicked(self, index: int) -> None:
        accent = theme.ACCENTS[index]
        if accent == self._accent and not self._stale:
            return
        self._accent = accent
        self._stale = False
        sounds.play(SoundCue.TOGGLE_TICK, window_visible=self.isVisible())
        self.accent_chosen.emit(accent)


# ---- Mục ---------------------------------------------------------------------------------------


def _plain(parent: QWidget, object_name: str) -> QLabel:
    label = QLabel(parent)
    label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return label


class AppearanceSection(QWidget):
    """Mục “Giao diện” (xem docstring module). Không gọi node; `set_*` không phát signal."""

    theme_selected = Signal(object)  # None | "light" | "dark"
    accent_selected = Signal(object)  # None (= mặc định) | mã màu
    language_selected = Signal(object)  # mã ngôn ngữ | None (theo hệ điều hành)
    sound_mode_selected = Signal(object)  # None (= full) | "notify" | "off"
    sound_volume_selected = Signal(object)  # None (= 70) | int 0..100
    auto_dismiss_notices_toggled = Signal(bool)
    tray_notifications_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("appearanceSection")
        self._language: str | None = None
        self._languages: tuple[str, ...] = (i18n.SOURCE_LANGUAGE,)
        self._loaded = False
        self._sound_mode = sounds.DEFAULT_MODE
        self._sound_volume = sounds.DEFAULT_VOLUME
        self._auto_dismiss_notices = True
        self._tray_notifications = True
        self._volume_timer = QTimer(self)
        self._volume_timer.setSingleShot(True)
        self._volume_timer.setInterval(180)
        self._volume_timer.timeout.connect(self._commit_sound_volume)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACING[1])

        self.theme_row = SettingsRow(self, object_name="themeRow")
        self.theme_control = SegmentedControl(MODES, self.theme_row)
        self.theme_control.setObjectName("themeControl")
        self.theme_control.value_selected.connect(self._on_theme)
        self.theme_row.set_control(self.theme_control)
        layout.addWidget(self.theme_row)

        self.accent_row = SettingsRow(self, object_name="accentRow")
        self.accent_picker = AccentPicker(self.accent_row)
        self.accent_picker.setObjectName("accentPicker")
        self.accent_picker.accent_chosen.connect(self._on_accent)
        self.accent_row.set_control(self.accent_picker)
        self.accent_current = _plain(self.accent_row, "accentCurrent")
        self.accent_current.setProperty(theme.TONE_PROPERTY, "muted")
        self.accent_row.add_line(self.accent_current)
        layout.addWidget(self.accent_row)

        self.language_row = SettingsRow(self, object_name="languageRow")
        self.language_combo = QComboBox(self.language_row)
        self.language_combo.setObjectName("languageCombo")
        self.language_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.language_combo.activated.connect(self._on_language_activated)
        self.language_row.set_control(self.language_combo)
        layout.addWidget(self.language_row)

        self.sound_mode_row = SettingsRow(self, object_name="soundModeRow")
        self.sound_mode_combo = QComboBox(self.sound_mode_row)
        self.sound_mode_combo.setObjectName("soundModeCombo")
        self.sound_mode_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.sound_mode_combo.activated.connect(self._on_sound_mode_activated)
        self.sound_mode_row.set_control(self.sound_mode_combo)
        layout.addWidget(self.sound_mode_row)

        self.sound_volume_row = SettingsRow(self, object_name="soundVolumeRow")
        volume_controls = QWidget(self.sound_volume_row)
        volume_controls.setProperty("keepAccessible", True)
        volume_layout = QHBoxLayout(volume_controls)
        volume_layout.setContentsMargins(0, 0, 0, 0)
        volume_layout.setSpacing(theme.SPACING[1])
        self.sound_volume_slider = QSlider(Qt.Orientation.Horizontal, volume_controls)
        self.sound_volume_slider.setObjectName("soundVolumeSlider")
        self.sound_volume_slider.setRange(sounds.MIN_VOLUME, sounds.MAX_VOLUME)
        self.sound_volume_slider.setSingleStep(5)
        self.sound_volume_slider.setPageStep(10)
        self.sound_volume_slider.setFixedWidth(130)
        self.sound_volume_slider.setValue(self._sound_volume)
        self.sound_volume_slider.valueChanged.connect(self._on_sound_volume_changed)
        self.sound_volume_slider.sliderReleased.connect(self._commit_sound_volume)
        self.sound_volume_label = _plain(volume_controls, "soundVolumeLabel")
        self.sound_volume_label.setProperty(theme.TONE_PROPERTY, "muted")
        self.sound_volume_label.setMinimumWidth(38)
        self.sound_volume_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.sound_preview_button = AnimatedButton("", volume_controls)
        self.sound_preview_button.setObjectName("soundPreviewButton")
        self.sound_preview_button.clicked.connect(self._preview_sound)
        volume_layout.addWidget(self.sound_volume_slider)
        volume_layout.addWidget(self.sound_volume_label)
        volume_layout.addWidget(self.sound_preview_button)
        self.sound_volume_row.set_control(volume_controls)
        layout.addWidget(self.sound_volume_row)

        self.auto_dismiss_row = SettingsRow(self, object_name="autoDismissNoticesRow")
        self.auto_dismiss_toggle = ToggleSwitch(self.auto_dismiss_row)
        self.auto_dismiss_toggle.setObjectName("autoDismissNoticesToggle")
        self.auto_dismiss_toggle.setChecked(self._auto_dismiss_notices)
        self.auto_dismiss_toggle.clicked.connect(self._on_auto_dismiss_toggled)
        self.auto_dismiss_row.set_control(self.auto_dismiss_toggle)
        layout.addWidget(self.auto_dismiss_row)

        self.tray_notifications_row = SettingsRow(self, object_name="trayNotificationsRow")
        self.tray_notifications_toggle = ToggleSwitch(self.tray_notifications_row)
        self.tray_notifications_toggle.setObjectName("trayNotificationsToggle")
        self.tray_notifications_toggle.setChecked(self._tray_notifications)
        self.tray_notifications_toggle.clicked.connect(self._on_tray_notifications_toggled)
        self.tray_notifications_row.set_control(self.tray_notifications_toggle)
        layout.addWidget(self.tray_notifications_row)

        self._update_volume_label()
        self.retranslate()

    # ---- API -----------------------------------------------------------------------------------

    def theme_value(self) -> str | None:
        """Giá trị trường `theme` đang hiện (None = theo hệ điều hành)."""
        return theme.Mode(str(self.theme_control.value())).setting_value

    def accent_value(self) -> str:
        """Mã màu nhấn đang hiện (luôn trong `theme.ACCENTS`)."""
        return self.accent_picker.accent()

    def set_theme(self, value: str | None) -> None:
        """Đặt chế độ từ giá trị đã lưu (None / lạ → theo hệ điều hành), không phát signal."""
        self.theme_control.set_value(theme.mode_from_setting(value))

    def set_accent(self, value: str | None) -> None:
        """Đặt màu nhấn từ giá trị đã lưu (None / lạ → mặc định), không phát signal."""
        self.accent_picker.set_accent(value)
        self._update_accent_text()

    def set_language(self, code: str | None, available: Sequence[str]) -> None:
        """Đặt ngôn ngữ đã lưu và các ngôn ngữ có bản dịch, không phát signal."""
        self._language = code
        self._languages = tuple(available)
        self._fill_languages()

    def set_sound_mode(self, value: str | None) -> None:
        """Đặt chế độ âm thanh từ giá trị đã lưu (None → mặc định FULL), không phát signal."""
        self._sound_mode = sounds.mode_from_setting(value)
        self._fill_sound_modes()
        self._sync_sound_enabled()

    def set_sound_volume(self, value: int | None) -> None:
        """Đặt âm lượng từ giá trị đã lưu (None → 70), không phát signal."""
        self._volume_timer.stop()
        self._sound_volume = sounds.volume_from_setting(value)
        old = self.sound_volume_slider.blockSignals(True)
        try:
            self.sound_volume_slider.setValue(self._sound_volume)
        finally:
            self.sound_volume_slider.blockSignals(old)
        self._update_volume_label()

    def set_auto_dismiss_notices(self, enabled: bool) -> None:
        """Đặt trạng thái tự động tắt thông báo từ giá trị đã lưu, không phát signal."""
        self._auto_dismiss_notices = bool(enabled)
        old = self.auto_dismiss_toggle.blockSignals(True)
        try:
            self.auto_dismiss_toggle.setChecked(self._auto_dismiss_notices)
        finally:
            self.auto_dismiss_toggle.blockSignals(old)

    def set_tray_notifications(self, enabled: bool) -> None:
        """Đặt trạng thái bóng thoại khay hệ thống từ giá trị đã lưu, không phát signal."""
        self._tray_notifications = bool(enabled)
        old = self.tray_notifications_toggle.blockSignals(True)
        try:
            self.tray_notifications_toggle.setChecked(self._tray_notifications)
        finally:
            self.tray_notifications_toggle.blockSignals(old)

    def set_loaded(self, loaded: bool) -> None:
        """Đã có ảnh chụp cài đặt: bật các hàng."""
        self._loaded = bool(loaded)
        for row in (
            self.theme_row,
            self.accent_row,
            self.language_row,
            self.sound_mode_row,
            self.auto_dismiss_row,
            self.tray_notifications_row,
        ):
            row.setEnabled(self._loaded)
        self._sync_sound_enabled()

    def focus_widgets(self) -> list[QWidget]:
        """Mọi control theo thứ tự Tab (chế độ, màu, ngôn ngữ, âm thanh, thông báo); Tab dừng ở nút chọn."""
        return [
            *self.theme_control.buttons(),
            *self.accent_picker.swatches(),
            self.language_combo,
            self.sound_mode_combo,
            self.sound_volume_slider,
            self.sound_preview_button,
            self.auto_dismiss_toggle,
            self.tray_notifications_toggle,
        ]

    def retranslate(self) -> None:
        t = QCoreApplication.translate
        self.setAccessibleName(section_title())
        mode_label = t("Appearance", "Chế độ màu")
        self.theme_row.set_text(
            mode_label,
            t("Appearance", "Chọn “Theo hệ điều hành” để Teleport đổi sáng/tối cùng hệ điều hành."),
        )
        self.theme_control.set_texts([mode_text(mode) for mode in MODES])
        for button in self.theme_control.buttons():
            button.setAccessibleDescription(mode_label)
        accent_label = t("Appearance", "Màu nhấn")
        accent_hint = t("Appearance", "Dùng cho nút chính, thanh tiến độ và khung file gửi đi.")
        self.accent_row.set_text(accent_label, accent_hint)
        self.accent_picker.retranslate()
        self._update_accent_text()
        self.language_row.set_text(
            t("Appearance", "Ngôn ngữ"), t("Appearance", "Đổi ngay, không cần mở lại Teleport.")
        )
        self._fill_languages()
        sound_label = t("Appearance", "Âm thanh hiệu ứng")
        self.sound_mode_row.set_text(
            sound_label,
            t("Appearance", "Phát âm báo nhẹ khi nhận lời mời, truyền xong hoặc tương tác trong ứng dụng."),
        )
        self._fill_sound_modes()
        vol_label = t("Appearance", "Âm lượng hiệu ứng")
        self.sound_volume_row.set_text(
            vol_label,
            t("Appearance", "Tự động im lặng khi máy bật chế độ Không làm phiền."),
        )
        self.sound_volume_slider.setAccessibleName(vol_label)
        self.sound_preview_button.setText(t("Appearance", "Nghe thử"))
        self.sound_preview_button.setAccessibleName(t("Appearance", "Nghe thử âm thanh hiệu ứng"))
        self.auto_dismiss_row.set_text(
            t("Appearance", "Tự động tắt thông báo"),
            t(
                "Appearance",
                "Tự ẩn các thanh thông báo sau vài giây (tạm dừng khi rê chuột vào). "
                "Tắt để giữ thông báo cho tới khi đóng tay.",
            ),
        )
        self.tray_notifications_row.set_text(
            t("Appearance", "Thông báo khay hệ thống"),
            t(
                "Appearance",
                "Hiện bóng thoại thông báo ngoài màn hình khi nhận file xong hoặc có lời mời mới. "
                "Tắt để giảm pop-up.",
            ),
        )

    # ---- Bên trong -----------------------------------------------------------------------------

    def _sync_sound_enabled(self) -> None:
        self.sound_volume_row.setEnabled(self._loaded and self._sound_mode is not SoundMode.OFF)

    def _update_volume_label(self) -> None:
        self.sound_volume_label.setText(f"{self.sound_volume_slider.value()}%")

    def _fill_sound_modes(self) -> None:
        t = QCoreApplication.translate
        combo = self.sound_mode_combo
        old = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem(t("Appearance", "Đầy đủ (mặc định)"), SoundMode.FULL.value)
            combo.addItem(t("Appearance", "Chỉ thông báo"), SoundMode.NOTIFY.value)
            combo.addItem(t("Appearance", "Tắt"), SoundMode.OFF.value)
            index = combo.findData(self._sound_mode.value)
            combo.setCurrentIndex(max(0, index))
        finally:
            combo.blockSignals(old)

    def _on_sound_mode_activated(self, index: int) -> None:
        raw = self.sound_mode_combo.itemData(index)
        mode = sounds.mode_from_setting(raw if isinstance(raw, str) else None)
        if mode is self._sound_mode:
            return
        self._sound_mode = mode
        self._sync_sound_enabled()
        saved = None if mode is sounds.DEFAULT_MODE else mode.value
        self.sound_mode_selected.emit(saved)

    def _on_sound_volume_changed(self, _value: int) -> None:
        self._update_volume_label()
        if not self.sound_volume_slider.isSliderDown():
            self._volume_timer.start()

    def _commit_sound_volume(self) -> None:
        self._volume_timer.stop()
        value = int(self.sound_volume_slider.value())
        if value == self._sound_volume:
            return
        self._sound_volume = value
        saved = None if value == sounds.DEFAULT_VOLUME else value
        self.sound_volume_selected.emit(saved)

    def _preview_sound(self) -> None:
        vol = int(self.sound_volume_slider.value())
        prev_mode, prev_vol = sounds.current_mode(), sounds.current_volume()
        sounds.set_preference(self._sound_mode, vol)
        try:
            sounds.play(SoundCue.TRANSFER_DONE, force_preview=True)
        finally:
            sounds.set_preference(prev_mode, prev_vol)

    def _on_auto_dismiss_toggled(self, checked: bool) -> None:
        self._auto_dismiss_notices = bool(checked)
        self.auto_dismiss_notices_toggled.emit(self._auto_dismiss_notices)

    def _on_tray_notifications_toggled(self, checked: bool) -> None:
        self._tray_notifications = bool(checked)
        self.tray_notifications_toggled.emit(self._tray_notifications)

    # ---- Bên trong -----------------------------------------------------------------------------

    def _update_accent_text(self) -> None:
        text = current_accent_text(self.accent_picker.accent())
        self.accent_current.setText(text)
        hint = QCoreApplication.translate(
            "Appearance", "Dùng cho nút chính, thanh tiến độ và khung file gửi đi."
        )
        self.accent_picker.setAccessibleDescription(f"{hint} {text}")

    def _fill_languages(self) -> None:
        combo = self.language_combo
        old = combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem(QCoreApplication.translate("Appearance", "Theo hệ điều hành"), None)
            codes = list(self._languages)
            if self._language is not None and self._language not in codes:
                codes.append(self._language)
            for code in codes:
                combo.addItem(i18n.language_name(code), code)
            index = combo.findData(self._language) if self._language is not None else 0
            combo.setCurrentIndex(max(0, index))
        finally:
            combo.blockSignals(old)

    def _on_theme(self, value: object) -> None:
        self.theme_selected.emit(theme.Mode(str(value)).setting_value)

    def _on_accent(self, accent: str) -> None:
        self._update_accent_text()
        self.accent_selected.emit(None if accent == theme.DEFAULT_ACCENT else accent)

    def _on_language_activated(self, index: int) -> None:
        code = self.language_combo.itemData(index)
        code = code if isinstance(code, str) else None
        if code == self._language:
            return
        self._language = code
        self.language_selected.emit(code)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange and "language_combo" in self.__dict__:
            self.retranslate()
