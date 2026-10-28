"""Theme sáng/tối theo hệ điều hành (mvp N12): bảng màu Fusion, đổi theo `colorSchemeChanged`.

`apply(app)` đặt style Fusion (vẽ đúng bảng màu trên mọi hệ điều hành) và bảng màu sáng hay tối
theo `QStyleHints.colorScheme()` (`Unknown` → sáng), rồi theo dõi `colorSchemeChanged` để đổi theo
hệ điều hành. Mọi cặp chữ/nền của bảng màu, kể cả chữ mờ (placeholder) và chữ của mục bị tắt,
đạt tương phản ≥ `MIN_CONTRAST` (WCAG 2.x AA cho chữ thường); `contrast_ratio` tính theo WCAG.
Không dùng màu để mang thông tin: trạng thái luôn có chữ đi kèm.

Không tải font hay ảnh nào: dùng font của hệ điều hành.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

MIN_CONTRAST = 4.5
_CONNECTED_PROPERTY = "teleport_theme_connected"

_Role = QPalette.ColorRole
_Group = QPalette.ColorGroup

# Màu theo vai trò: (sáng, tối).
_COLORS: dict[QPalette.ColorRole, tuple[str, str]] = {
    _Role.Window: ("#f5f6f8", "#1e2126"),
    _Role.WindowText: ("#1b1f24", "#e8eaed"),
    _Role.Base: ("#ffffff", "#16181c"),
    _Role.AlternateBase: ("#eef0f3", "#23272d"),
    _Role.Text: ("#1b1f24", "#e8eaed"),
    _Role.Button: ("#e9ecef", "#2a2e35"),
    _Role.ButtonText: ("#1b1f24", "#e8eaed"),
    _Role.BrightText: ("#ffffff", "#0b0d10"),
    _Role.Highlight: ("#0b5cad", "#7ab8ff"),
    _Role.HighlightedText: ("#ffffff", "#0b0d10"),
    _Role.Link: ("#0b5cad", "#7ab8ff"),
    _Role.LinkVisited: ("#5b3a9e", "#c3a6ff"),
    _Role.ToolTipBase: ("#ffffff", "#2a2e35"),
    _Role.ToolTipText: ("#1b1f24", "#e8eaed"),
    _Role.PlaceholderText: ("#545b66", "#a9b0bb"),
    _Role.Light: ("#ffffff", "#3a3f47"),
    _Role.Midlight: ("#dfe3e8", "#30353c"),
    _Role.Mid: ("#b8bec7", "#4a5059"),
    _Role.Dark: ("#8a919c", "#0f1114"),
    _Role.Shadow: ("#5c626b", "#000000"),
}
# Chữ của mục bị tắt: vẫn đọc được (≥ MIN_CONTRAST trên nền của nó) nhưng khác rõ chữ thường.
_DISABLED_TEXT = ("#5c636e", "#9aa1ad")
_DISABLED_ROLES = (_Role.WindowText, _Role.Text, _Role.ButtonText)


def _linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _luminance(color: QColor) -> float:
    return 0.2126 * _linear(color.redF()) + 0.7152 * _linear(color.greenF()) + 0.0722 * _linear(color.blueF())


def contrast_ratio(fg: QColor, bg: QColor) -> float:
    """Tương phản WCAG 2.x giữa hai màu (1–21), không phụ thuộc thứ tự."""
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def is_dark(scheme: Qt.ColorScheme) -> bool:
    return scheme == Qt.ColorScheme.Dark


def palette(dark: bool) -> QPalette:
    """Bảng màu sáng hay tối của Teleport."""
    index = 1 if dark else 0
    pal = QPalette()
    for role, colors in _COLORS.items():
        pal.setColor(role, QColor(colors[index]))
    for role in _DISABLED_ROLES:
        pal.setColor(_Group.Disabled, role, QColor(_DISABLED_TEXT[index]))
    return pal


def apply(app: QGuiApplication, scheme: Qt.ColorScheme | None = None) -> None:
    """Đặt style Fusion và bảng màu theo `scheme` (None: theo hệ điều hành); theo dõi đổi chế độ."""
    if scheme is None:
        scheme = app.styleHints().colorScheme()
    _set_style(app)
    app.setPalette(palette(is_dark(scheme)))
    if not app.property(_CONNECTED_PROPERTY):
        app.setProperty(_CONNECTED_PROPERTY, True)
        app.styleHints().colorSchemeChanged.connect(lambda new: apply(app, new))


def _set_style(app: QGuiApplication) -> None:
    if isinstance(app, QApplication) and app.style().name().lower() != "fusion":
        app.setStyle("Fusion")
