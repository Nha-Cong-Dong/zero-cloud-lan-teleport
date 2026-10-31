"""Hệ thiết kế của giao diện (mvp N12): token màu, thang chữ, khoảng cách, góc bo; theme sáng/tối.

`ui/theme.py` là nguồn duy nhất của màu, cỡ chữ, khoảng cách và góc bo. Màn nào cũng lấy từ đây,
không ghi mã màu cứng.

**Token** (`tokens(dark)` → `Tokens`): giá trị theo mockup khung chat của kế hoạch nâng cấp giao diện
(spec A1). Chỗ lệch mockup để đạt tương phản: `border_strong` sáng `#8C7B62` (mockup `#9C8C72` chỉ
≈ 2,8:1 trên sidebar); `accent_hover`/`accent_pressed`, `success_bg`, `warning`, `warning_bg` là token
mới (nút chính nền nhạt chữ đậm nên hover/pressed là màu riêng, không `darker()`). `offline` chỉ dùng
cho chấm/icon (≥ 3:1); chữ “ngoại tuyến” dùng `text_muted`. Cặp đã kiểm: chữ ≥ `MIN_CONTRAST` trên
các nền liệt kê ở tiêu chí (a) của `tests/ui/test_tokens.py`; viền/icon (`border_strong`,
`accent_strong`, `success`, `warning`, `danger`, `offline`) ≥ 3:1 trên nền cửa sổ, nền nổi và
sidebar; `accent_strong` (thanh tiến độ) ≥ 3:1 trên `track`. Spec A3 thêm cặp cho `ui/widgets.py`
(bong bóng, `InfoBar`, badge, avatar trên hàng chọn); để `offline` ≥ 3:1 trên `selected`, `offline`
sáng làm tối từ `#8A7F70` của A1 xuống `#897E6F` (bước nhỏ nhất đạt ngưỡng).

**Chế độ của một widget** (`tokens_for(widget)`): widget tự vẽ (`ui/widgets.py`) lấy token theo
`is_dark_palette(QApplication.palette())`: tối khi độ sáng của `Window` thấp hơn của `WindowText`, nên
không cần biến toàn cục và test không gọi `apply` thì là sáng. Đọc bảng màu **của ứng dụng**, không
của widget: QStyleSheetStyle ghi màu nền trong suốt vào bảng màu của widget khớp luật
`background: transparent`, làm phép so độ sáng sai. Widget tự vẽ bắt `PaletteChange` và
`ApplicationPaletteChange` để vẽ lại khi đổi chế độ.

**Bảng màu** (`palette(dark)`, style Fusion): Window = surface; Base = surface_raised; AlternateBase =
sidebar; Button = sidebar (sáng) / selected (tối: nền sidebar gần như trùng nền cửa sổ, nút Fusion
mất cạnh); chữ (Text, WindowText, ButtonText, ToolTipText) = text; ToolTipBase =
surface_raised; Highlight = accent_strong; HighlightedText = surface_raised (sáng) / surface (tối);
Link = LinkVisited = accent_text; PlaceholderText và chữ mục bị tắt = text_muted; Light =
surface_raised; Midlight = selected; Mid = border_strong; Dark = border_strong đậm thêm
(`darker(115)`, ≥ 3:1 trên surface ở cả hai chế độ); Shadow = text; BrightText = surface_raised (sáng)
/ surface (tối). Highlight là `accent_strong` chứ không phải bạc hà nhạt `accent`, vì Highlight còn tô
thanh tiến độ, viền thả, hàng chọn, mục menu khay và viền tiêu điểm Fusion (`highlightedOutline`;
sáng là `highlight().darker(125)`, tối Fusion vẽ sáng hơn): bạc hà nhạt chỉ ≈ 1,3:1 trên nền (WCAG
1.4.11). `apply(app)` đặt style Fusion và bảng màu theo
`QStyleHints.colorScheme()` (`Unknown` → sáng), rồi theo `colorSchemeChanged` để đổi theo hệ điều
hành. Không dùng màu để mang thông tin: trạng thái luôn có chữ đi kèm.

**Thang chữ** (`font_for(style, base)`): TITLE +6 pt đậm (Bold), SECTION +2 pt Medium, cả hai dùng font
điểm nhấn `ACCENT_FAMILY` nếu đã nạp (`accent_family()`), không thì font của `base`; BODY là `base`;
CAPTION −1 pt, font của `base`, màu qua `TONE_PROPERTY`. `base` mặc định `QApplication.font()`; font
đặt cỡ theo pixel thì cộng TITLE +8 px, SECTION +3 px, CAPTION −1 px. Không ghi cứng font thân: dùng
font của hệ điều hành. Không tải font hay ảnh nào từ mạng.

**Stylesheet ứng dụng** (`stylesheet(dark, button_height)`, `apply` đặt): mọi luật có bộ chọn hẹp
(`[property]` hoặc `#objectName`), nên widget không mang property thì Fusion vẽ như cũ.
- `QPushButton[primary="true"]` — **nút chính** (hành động chính của mỗi màn / hộp: “Bắt đầu”, “Ghép
  đôi” của khung nhập IP, “Nhận”, “Trùng khớp”): nền `accent`, chữ `accent_text`, viền 2 px
  `accent_strong`, bo `RADIUS_CONTROL`, mã màu viết thẳng từ `primary_colors()` (không qua
  `palette(highlight)`, vì Highlight là `accent_strong`). `min-height` theo chiều cao nút Fusion
  thường đo lúc `apply` (cỡ chữ và lề Fusion khác nhau giữa Windows, macOS, Linux), nên cao bằng nút
  bên cạnh. `:hover`/`:pressed` dùng `accent_hover`/`accent_pressed`; `:disabled` về màu nút thường với
  chữ của mục bị tắt. **Tiêu điểm** (`:focus`) đổi hai thứ, nút không đổi cỡ: viền sang
  `focus_ring_color` (màu chữ cửa sổ) và chữ gạch chân. Viền tiêu điểm ≥ 3:1 với cả nền nút lẫn nền
  cửa sổ, nhưng so với viền thường `accent_strong` chỉ khác ≈ 1,7:1 ở chế độ tối, nên gạch chân (màu
  chữ của nút, ≥ 4,5:1 với nền nút) là dấu hiệu thứ hai. Tô màu chỉ để nhấn: nút mặc định (Enter)
  vẫn là nút an toàn của hộp (“Từ chối”, “Không trùng”), không phải nút chính.
- `QToolButton[iconButton="true"]` — **nút icon** (`widgets.IconButton`): nền trong suốt, viền 2 px
  trong suốt (giữ nguyên cỡ nút khi có tiêu điểm), bo `RADIUS_CONTROL`, padding 4 px, chữ `text`;
  `:hover` nền `selected`; `:pressed` nền `border`; `:focus` viền `focus_ring_color` (khớp luật thì
  Fusion không vẽ viền tiêu điểm nữa, nên luật này bắt buộc); `:disabled` chữ `text_muted`;
  `::menu-indicator` không ảnh, rộng 0 (nút ⋯ có menu không hiện mũi tên).
- `QLabel[tone="muted|success|warning|danger|accent"]`: màu chữ theo token cùng tên (`muted` →
  `text_muted`, `accent` → `accent_text`).
- `QFrame[surface="sidebar|raised|card"]`: nền `sidebar` (viền phải `border`, không bo) / nền
  `surface_raised`, viền `border`, bo `RADIUS_CARD` / như `raised` nhưng bo `RADIUS_BLOCK`.

Property (`PRIMARY_PROPERTY`, `TONE_PROPERTY`, `SURFACE_PROPERTY`, `ICON_BUTTON_PROPERTY`) đặt
**trước khi hiện** (stylesheet đọc property lúc polish); đặt sau khi widget đã hiện thì gọi
`style().unpolish(w)` rồi `style().polish(w)`.

`DIALOG_MIN_WIDTH`: bề ngang tối thiểu chung của hộp nhận file và hộp ghép đôi. `SPACING`: thang
khoảng cách (px). `RADIUS_*`: góc bo (px) của control, thẻ, khối, bong bóng và đuôi bong bóng.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

MIN_CONTRAST = 4.5
PRIMARY_PROPERTY = "primary"  # dynamic property của nút chính (xem docstring module)
TONE_PROPERTY = "tone"  # QLabel[tone="muted|success|warning|danger|accent"]
SURFACE_PROPERTY = "surface"  # QFrame[surface="sidebar|raised|card"]
ICON_BUTTON_PROPERTY = "iconButton"  # QToolButton[iconButton="true"] (widgets.IconButton)
_PRIMARY_FRAME = 8  # px: padding dọc 2·2 + viền 2·2 của nút chính (`stylesheet`)
DIALOG_MIN_WIDTH = 420  # px, hộp nhận file và hộp ghép đôi
SPACING = (4, 8, 12, 16, 24, 32)  # px
RADIUS_CONTROL, RADIUS_CARD, RADIUS_BLOCK, RADIUS_BUBBLE, RADIUS_BUBBLE_TAIL = 6, 8, 12, 14, 4
ACCENT_FAMILY = "Bricolage Grotesque"
_CONNECTED_PROPERTY = "teleport_theme_connected"

_Role = QPalette.ColorRole
_Group = QPalette.ColorGroup


@dataclass(frozen=True)
class Tokens:
    """Màu theo vai trò của một chế độ (sáng hoặc tối); xem docstring module."""

    surface: QColor
    surface_raised: QColor
    sidebar: QColor
    selected: QColor
    border: QColor
    border_strong: QColor
    text: QColor
    text_muted: QColor
    accent: QColor
    accent_hover: QColor
    accent_pressed: QColor
    accent_text: QColor
    accent_strong: QColor
    accent_subtle: QColor
    bubble_out: QColor
    bubble_in: QColor
    track: QColor
    offline: QColor
    success: QColor
    success_bg: QColor
    warning: QColor
    warning_bg: QColor
    danger: QColor
    danger_bg: QColor


# (sáng, tối) cho từng token; thiếu hay thừa tên so với `Tokens` thì `tokens()` ném TypeError.
_TOKENS: dict[str, tuple[str, str]] = {
    "surface": ("#FBF6EC", "#1E1A16"),
    "surface_raised": ("#FFFDF8", "#2D2721"),
    "sidebar": ("#F3EBDC", "#25201B"),
    "selected": ("#E9DFCB", "#3A3229"),
    "border": ("#E2D6C2", "#3D352C"),
    "border_strong": ("#8C7B62", "#857761"),
    "text": ("#2A251E", "#F2EBDF"),
    "text_muted": ("#665B4D", "#B8AC9A"),
    "accent": ("#BFEBD9", "#2B5A4A"),
    "accent_hover": ("#AEE3CD", "#25503F"),
    "accent_pressed": ("#9DDBC1", "#204636"),
    "accent_text": ("#0B5E47", "#A3E6CB"),
    "accent_strong": ("#1E7F61", "#5CC9A0"),
    "accent_subtle": ("#D9F2E6", "#21443A"),
    "bubble_out": ("#D9F2E6", "#21443A"),
    "bubble_in": ("#FFFDF8", "#2D2721"),
    "track": ("#E7DECD", "#3D352C"),
    "offline": ("#897E6F", "#9A8E7D"),
    "success": ("#256B42", "#6CD49A"),
    "success_bg": ("#E1F1E6", "#1F3A2A"),
    "warning": ("#8A5A00", "#F0C46C"),
    "warning_bg": ("#FBEFD5", "#45381F"),
    "danger": ("#A8361F", "#F2A38C"),
    "danger_bg": ("#FBE6DD", "#4A2A22"),
}


class TextStyle(Enum):
    """Bậc chữ của thang chữ (xem docstring module)."""

    TITLE = "title"
    SECTION = "section"
    BODY = "body"
    CAPTION = "caption"


# Cộng thêm vào cỡ của font gốc: (pt, px) cho font cỡ điểm / font cỡ pixel.
_SIZE_DELTA: dict[TextStyle, tuple[float, int]] = {
    TextStyle.TITLE: (6.0, 8),
    TextStyle.SECTION: (2.0, 3),
    TextStyle.BODY: (0.0, 0),
    TextStyle.CAPTION: (-1.0, -1),
}


def tokens(dark: bool) -> Tokens:
    """Token màu của chế độ sáng (`dark=False`) hay tối."""
    index = 1 if dark else 0
    return Tokens(**{name: QColor(pair[index]) for name, pair in _TOKENS.items()})


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


def is_dark_palette(pal: QPalette) -> bool:
    """True khi `Window` của `pal` tối hơn `WindowText` (bảng màu tối)."""
    return _luminance(pal.color(_Role.Window)) < _luminance(pal.color(_Role.WindowText))


def tokens_for(widget: QWidget) -> Tokens:
    """Token theo chế độ đang áp cho ứng dụng của `widget` (xem docstring module).

    Đọc `QApplication.palette()`, không đọc `widget.palette()`; `widget` giữ trong chữ ký để nơi gọi
    không phải biết chuyện đó.
    """
    del widget
    return tokens(is_dark_palette(QApplication.palette()))


def palette(dark: bool) -> QPalette:
    """Bảng màu sáng hay tối của Teleport, sinh từ token (ánh xạ ở docstring module)."""
    t = tokens(dark)
    on_highlight = t.surface if dark else t.surface_raised
    roles: dict[QPalette.ColorRole, QColor] = {
        _Role.Window: t.surface,
        _Role.WindowText: t.text,
        _Role.Base: t.surface_raised,
        _Role.AlternateBase: t.sidebar,
        _Role.Text: t.text,
        _Role.Button: t.selected if dark else t.sidebar,
        _Role.ButtonText: t.text,
        _Role.BrightText: on_highlight,
        _Role.Highlight: t.accent_strong,
        _Role.HighlightedText: on_highlight,
        _Role.Link: t.accent_text,
        _Role.LinkVisited: t.accent_text,
        _Role.ToolTipBase: t.surface_raised,
        _Role.ToolTipText: t.text,
        _Role.PlaceholderText: t.text_muted,
        _Role.Light: t.surface_raised,
        _Role.Midlight: t.selected,
        _Role.Mid: t.border_strong,
        _Role.Dark: t.border_strong.darker(115),
        _Role.Shadow: t.text,
    }
    pal = QPalette()
    for role, color in roles.items():
        pal.setColor(role, color)
    # Chữ của mục bị tắt: vẫn đọc được (≥ MIN_CONTRAST trên nền của nó) nhưng khác rõ chữ thường.
    for role in (_Role.WindowText, _Role.Text, _Role.ButtonText):
        pal.setColor(_Group.Disabled, role, t.text_muted)
    return pal


def primary_colors(dark: bool) -> dict[str, tuple[QColor, QColor]]:
    """(chữ, nền) của nút chính theo trạng thái: `normal`, `hover`, `pressed`, `disabled`."""
    t = tokens(dark)
    pal = palette(dark)
    return {
        "normal": (t.accent_text, t.accent),
        "hover": (t.accent_text, t.accent_hover),
        "pressed": (t.accent_text, t.accent_pressed),
        "disabled": (pal.color(_Group.Disabled, _Role.ButtonText), pal.color(_Role.Button)),
    }


def focus_ring_color(dark: bool) -> QColor:
    """Màu viền tiêu điểm của nút chính (màu chữ cửa sổ)."""
    return palette(dark).color(_Role.WindowText)


def accent_family() -> str | None:
    """`ACCENT_FAMILY` nếu font điểm nhấn đã nạp vào ứng dụng, không thì None."""
    return ACCENT_FAMILY if QFontDatabase.hasFamily(ACCENT_FAMILY) else None


def font_for(style: TextStyle, base: QFont | None = None) -> QFont:
    """Font của bậc chữ `style`, tính từ `base` (mặc định `QApplication.font()`)."""
    font = QFont(base if base is not None else QApplication.font())
    pt_delta, px_delta = _SIZE_DELTA[style]
    if font.pointSizeF() > 0:
        font.setPointSizeF(max(1.0, font.pointSizeF() + pt_delta))
    elif font.pixelSize() > 0:
        font.setPixelSize(max(1, font.pixelSize() + px_delta))
    if style in (TextStyle.TITLE, TextStyle.SECTION):
        family = accent_family()
        if family is not None:
            font.setFamilies([family])
        font.setWeight(QFont.Weight.Bold if style is TextStyle.TITLE else QFont.Weight.Medium)
    return font


def stylesheet(dark: bool, button_height: int | None = None) -> str:
    """Stylesheet ứng dụng, chỉ bộ chọn hẹp (xem docstring module).

    `button_height`: chiều cao `sizeHint` của nút Fusion thường trên máy này (`apply` đo); có thì nút
    chính cao đúng bằng nó, vì cỡ chữ và lề của Fusion khác nhau giữa các hệ điều hành.
    """
    t = tokens(dark)
    pal = palette(dark)
    colors = primary_colors(dark)
    ring = focus_ring_color(dark).name()
    edge = t.accent_strong.name()
    primary = f'QPushButton[{PRIMARY_PROPERTY}="true"]'
    lines = [
        f"{primary} {{",
        f"  background-color: {colors['normal'][1].name()};",
        f"  color: {colors['normal'][0].name()};",
        f"  border: 2px solid {edge};",
        f"  border-radius: {RADIUS_CONTROL}px;",
        "  padding: 2px 12px;",  # + viền 2 px: cao bằng nút Fusion (chữ + 8 px)
        "  min-width: 52px;",  # Fusion cho nút có chữ rộng ít nhất 80 px: 52 + 2·12 + 2·2
        *([f"  min-height: {max(0, button_height - _PRIMARY_FRAME)}px;"] if button_height else []),
        "}",
        f"{primary}:hover {{ background-color: {colors['hover'][1].name()};"
        f" color: {colors['hover'][0].name()}; }}",
        f"{primary}:pressed {{ background-color: {colors['pressed'][1].name()};"
        f" color: {colors['pressed'][0].name()}; }}",
        f"{primary}:focus {{ border-color: {ring}; text-decoration: underline; }}",
        f"{primary}:disabled {{ background-color: {colors['disabled'][1].name()};"
        f" color: {colors['disabled'][0].name()}; border-color: {pal.color(_Role.Mid).name()}; }}",
    ]
    icon_button = f'QToolButton[{ICON_BUTTON_PROPERTY}="true"]'
    lines += [
        f"{icon_button} {{ background: transparent; border: 2px solid transparent;"
        f" border-radius: {RADIUS_CONTROL}px; padding: 4px; color: {t.text.name()}; }}",
        f"{icon_button}:hover {{ background: {t.selected.name()}; }}",
        f"{icon_button}:pressed {{ background: {t.border.name()}; }}",
        f"{icon_button}:focus {{ border-color: {ring}; }}",
        f"{icon_button}:disabled {{ color: {t.text_muted.name()}; }}",
        f"{icon_button}::menu-indicator {{ image: none; width: 0px; }}",
    ]
    tones = {
        "muted": t.text_muted,
        "success": t.success,
        "warning": t.warning,
        "danger": t.danger,
        "accent": t.accent_text,
    }
    lines += [
        f'QLabel[{TONE_PROPERTY}="{name}"] {{ color: {color.name()}; }}' for name, color in tones.items()
    ]
    surface = f"QFrame[{SURFACE_PROPERTY}="
    border = t.border.name()
    lines += [
        f'{surface}"sidebar"] {{ background-color: {t.sidebar.name()}; border: none;'
        f" border-right: 1px solid {border}; border-radius: 0px; }}",
        f'{surface}"raised"] {{ background-color: {t.surface_raised.name()};'
        f" border: 1px solid {border}; border-radius: {RADIUS_CARD}px; }}",
        f'{surface}"card"] {{ background-color: {t.surface_raised.name()};'
        f" border: 1px solid {border}; border-radius: {RADIUS_BLOCK}px; }}",
    ]
    return "\n".join(lines)


def apply(app: QGuiApplication, scheme: Qt.ColorScheme | None = None) -> None:
    """Đặt style Fusion, bảng màu và stylesheet ứng dụng theo `scheme` (None: theo hệ điều hành);
    theo dõi đổi chế độ."""
    if scheme is None:
        scheme = app.styleHints().colorScheme()
    _set_style(app)
    dark = is_dark(scheme)
    app.setPalette(palette(dark))
    if isinstance(app, QApplication):
        app.setStyleSheet(stylesheet(dark, _plain_button_height()))
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
