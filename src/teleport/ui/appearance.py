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

from PySide6.QtCore import QCoreApplication, QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from teleport.ui import i18n, icons, texts, theme
from teleport.ui.widgets import RovingGroup, SegmentedControl, SettingsRow, safe_tooltip

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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("appearanceSection")
        self._language: str | None = None
        self._languages: tuple[str, ...] = (i18n.SOURCE_LANGUAGE,)

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

    def set_loaded(self, loaded: bool) -> None:
        """Đã có ảnh chụp cài đặt: bật cả ba hàng."""
        for row in (self.theme_row, self.accent_row, self.language_row):
            row.setEnabled(bool(loaded))

    def focus_widgets(self) -> list[QWidget]:
        """Mọi control theo thứ tự Tab (nút chế độ, ô màu, ô ngôn ngữ); Tab chỉ dừng ở nút đang chọn."""
        return [*self.theme_control.buttons(), *self.accent_picker.swatches(), self.language_combo]

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
