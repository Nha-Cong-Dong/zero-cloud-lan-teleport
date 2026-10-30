"""Theme sáng/tối theo hệ điều hành (mvp N12): bảng màu Fusion, đổi theo `colorSchemeChanged`.

`apply(app)` đặt style Fusion (vẽ đúng bảng màu trên mọi hệ điều hành) và bảng màu sáng hay tối
theo `QStyleHints.colorScheme()` (`Unknown` → sáng), rồi theo dõi `colorSchemeChanged` để đổi theo
hệ điều hành. Mọi cặp chữ/nền của bảng màu, kể cả chữ mờ (placeholder) và chữ của mục bị tắt,
đạt tương phản ≥ `MIN_CONTRAST` (WCAG 2.x AA cho chữ thường); `contrast_ratio` tính theo WCAG.
Không dùng màu để mang thông tin: trạng thái luôn có chữ đi kèm.

**Nút chính** (nút của hành động chính trên mỗi màn / hộp: “Bắt đầu”, “Ghép đôi” của khung nhập IP,
“Nhận”, “Trùng khớp”): đặt dynamic property `PRIMARY_PROPERTY` = True **trước khi hiện** (stylesheet
đọc property lúc polish). `apply` đặt stylesheet ứng dụng (`primary_stylesheet`) chỉ có luật cho
`QPushButton[primary="true"]`: nền `palette(highlight)`, chữ `palette(highlighted-text)`, viền 2 px
cùng màu nền, lề trong 2 px, và `min-height` theo chiều cao nút Fusion thường đo lúc `apply` (cỡ chữ
và lề của Fusion khác nhau giữa Windows, macOS, Linux), nên cao đúng bằng nút bên cạnh; `:hover` /
`:pressed` đậm hơn (sáng) hay nhạt hơn (tối); `:disabled` về màu nút thường với chữ của mục bị tắt.
**Tiêu điểm** (`:focus`) đổi hai thứ cùng lúc, nút không đổi cỡ, nền giữ màu nhấn: viền sang
`focus_ring_color` (màu chữ cửa sổ, tương phản cao với nền cửa sổ) và chữ của nút gạch chân. Một màu
viền không thể vừa ≥ 3:1 với nền cửa sổ vừa ≥ 3:1 với nền nút (ở chế độ tối viền sáng trên nút xanh
nhạt chỉ 1,7:1), nên gạch chân (màu chữ của nút, ≥ 4,5:1 với nền nút) là dấu hiệu thứ hai, thấy rõ ở
cả sáng lẫn tối. Mọi cặp chữ/nền đạt `MIN_CONTRAST` ở cả sáng lẫn tối (`primary_colors`). Các nút
khác không có luật nào nên Fusion vẽ như cũ. Tô màu chỉ để nhấn: nút mặc định (Enter) vẫn là nút an
toàn của hộp (“Từ chối”, “Không trùng”), không phải nút chính.

`DIALOG_MIN_WIDTH`: bề ngang tối thiểu chung của hộp nhận file và hộp ghép đôi.

Không tải font hay ảnh nào: dùng font của hệ điều hành.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QPushButton

MIN_CONTRAST = 4.5
PRIMARY_PROPERTY = "primary"  # dynamic property của nút chính (xem docstring module)
_PRIMARY_FRAME = 8  # px: padding dọc 2·2 + viền 2·2 của nút chính (`primary_stylesheet`)
DIALOG_MIN_WIDTH = 420  # px, hộp nhận file và hộp ghép đôi
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


def primary_colors(dark: bool) -> dict[str, tuple[QColor, QColor]]:
    """(chữ, nền) của nút chính theo trạng thái: `normal`, `hover`, `pressed`, `disabled`."""
    pal = palette(dark)
    text = pal.color(_Role.HighlightedText)
    base = pal.color(_Role.Highlight)
    # Nền sáng: hover/pressed đậm hơn; nền tối (nút xanh nhạt, chữ tối): nhạt hơn. Cả hai cách đều
    # làm tương phản với chữ tăng lên.
    hover = base.lighter(110) if dark else base.darker(115)
    pressed = base.lighter(120) if dark else base.darker(130)
    disabled = (pal.color(_Group.Disabled, _Role.ButtonText), pal.color(_Role.Button))
    return {
        "normal": (text, base),
        "hover": (text, hover),
        "pressed": (text, pressed),
        "disabled": disabled,
    }


def focus_ring_color(dark: bool) -> QColor:
    """Màu viền tiêu điểm của nút chính (màu chữ cửa sổ)."""
    return palette(dark).color(_Role.WindowText)


def primary_stylesheet(dark: bool, button_height: int | None = None) -> str:
    """Stylesheet ứng dụng: chỉ tô `QPushButton[primary="true"]` (xem docstring module).

    `button_height`: chiều cao `sizeHint` của nút Fusion thường trên máy này (`apply` đo); có thì nút
    chính cao đúng bằng nó, vì cỡ chữ và lề của Fusion khác nhau giữa các hệ điều hành.
    """
    pal = palette(dark)
    colors = primary_colors(dark)
    ring = focus_ring_color(dark).name()
    selector = f'QPushButton[{PRIMARY_PROPERTY}="true"]'
    return "\n".join(
        [
            f"{selector} {{",
            "  background-color: palette(highlight);",
            "  color: palette(highlighted-text);",
            f"  border: 2px solid {colors['normal'][1].name()};",
            "  border-radius: 4px;",
            "  padding: 2px 12px;",  # + viền 2 px: cao bằng nút Fusion (chữ + 8 px)
            "  min-width: 52px;",  # Fusion cho nút có chữ rộng ít nhất 80 px: 52 + 2·12 + 2·2
            *([f"  min-height: {max(0, button_height - _PRIMARY_FRAME)}px;"] if button_height else []),
            "}",
            f"{selector}:hover {{ background-color: {colors['hover'][1].name()};"
            f" border-color: {colors['hover'][1].name()}; }}",
            f"{selector}:pressed {{ background-color: {colors['pressed'][1].name()};"
            f" border-color: {colors['pressed'][1].name()}; }}",
            f"{selector}:focus {{ border-color: {ring}; text-decoration: underline; }}",
            f"{selector}:disabled {{ background-color: {colors['disabled'][1].name()};"
            f" color: {colors['disabled'][0].name()}; border-color: {pal.color(_Role.Mid).name()}; }}",
        ]
    )


def apply(app: QGuiApplication, scheme: Qt.ColorScheme | None = None) -> None:
    """Đặt style Fusion, bảng màu và stylesheet nút chính theo `scheme` (None: theo hệ điều hành);
    theo dõi đổi chế độ."""
    if scheme is None:
        scheme = app.styleHints().colorScheme()
    _set_style(app)
    dark = is_dark(scheme)
    app.setPalette(palette(dark))
    if isinstance(app, QApplication):
        app.setStyleSheet(primary_stylesheet(dark, _plain_button_height()))
    if not app.property(_CONNECTED_PROPERTY):
        app.setProperty(_CONNECTED_PROPERTY, True)
        app.styleHints().colorSchemeChanged.connect(lambda new: apply(app, new))


def _plain_button_height() -> int:
    """Chiều cao `sizeHint` của một nút thường (không mang `PRIMARY_PROPERTY`, nên Fusion vẽ)."""
    probe = QPushButton("Ag")
    try:
        return probe.sizeHint().height()
    finally:
        probe.deleteLater()


def _set_style(app: QGuiApplication) -> None:
    if isinstance(app, QApplication) and app.style().name().lower() != "fusion":
        app.setStyle("Fusion")
