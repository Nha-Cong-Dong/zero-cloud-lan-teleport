"""Token màu, thang chữ và stylesheet của `ui/theme.py` (spec A1, tiêu chí (a)–(g), cộng mục “Áp dụng
hoi-lai 30/10”, mục này ghi đè phần trên của spec).

Viết từ spec, không từ code:

- Tương phản tính **độc lập** với `theme.contrast_ratio` (công thức WCAG 2.x viết lại ở đây): chữ ≥ 4,5
  (mvp N12), viền/icon/thành phần không phải chữ ≥ 3 (WCAG 1.4.11). Mọi tiêu chí chạy ở cả sáng và tối.
- Không ghim mã màu của bảng token: spec cho giao-dien chỉnh tối thiểu khi test đỏ. Test chỉ kiểm quan
  hệ (vai trong QPalette, ngưỡng tương phản, `accent_subtle = bubble_out`).
- Ánh xạ QPalette theo bản ghi đè: `Highlight = accent_strong`, `HighlightedText = surface_raised`
  (sáng) / `surface` (tối), Light/Midlight/Mid/Dark/Shadow/Link/LinkVisited/BrightText.
- Nút chính: stylesheet viết thẳng mã màu từ `primary_colors()`, không qua `palette(highlight)`.
- (g): `QLabel[tone=...]` chữ đậm cỡ lớn, `grab()` phải có điểm ảnh lệch mỗi kênh ≤ 8 so với màu tone.

Marker `gui` do `tests/ui/conftest.py` gắn; Qt chạy `offscreen`. Fixture riêng của file này nằm ngay
dưới đây (không ở conftest, theo spec mục “Thứ tự merge”).
"""

from __future__ import annotations

import dataclasses
import re

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette

Role = QPalette.ColorRole
Group = QPalette.ColorGroup

MODES = pytest.mark.parametrize("dark", [False, True], ids=["sang", "toi"])

TOKEN_FIELDS = (
    "surface",
    "surface_raised",
    "sidebar",
    "selected",
    "border",
    "border_strong",
    "text",
    "text_muted",
    "accent",
    "accent_hover",
    "accent_pressed",
    "accent_text",
    "accent_strong",
    "accent_subtle",
    "bubble_out",
    "bubble_in",
    "track",
    "offline",
    "success",
    "success_bg",
    "warning",
    "warning_bg",
    "danger",
    "danger_bg",
)

TEXT_MIN = 4.5  # mvp N12, WCAG 1.4.3
NON_TEXT_MIN = 3.0  # WCAG 1.4.11

# (a) chữ ≥ 4,5: màu chữ → các nền nó phải đọc được.
TEXT_PAIRS: dict[str, tuple[str, ...]] = {
    "text": ("surface", "surface_raised", "sidebar", "selected", "bubble_in", "bubble_out"),
    "text_muted": ("surface", "surface_raised", "sidebar", "selected", "bubble_in", "bubble_out"),
    "accent_text": ("accent", "accent_hover", "accent_pressed", "surface", "surface_raised", "accent_subtle"),
    "danger": ("surface", "surface_raised", "danger_bg", "bubble_in", "bubble_out"),
    "success": ("surface", "surface_raised", "sidebar", "success_bg"),
    "warning": ("surface", "surface_raised", "warning_bg"),
}

# (b) viền/icon ≥ 3.
NON_TEXT_PAIRS: dict[str, tuple[str, ...]] = {
    "border_strong": ("surface", "surface_raised", "sidebar"),
    "accent_strong": ("surface", "surface_raised", "sidebar", "track"),
    "success": ("surface", "surface_raised", "sidebar"),
    "warning": ("surface", "surface_raised", "sidebar"),
    "danger": ("surface", "surface_raised", "sidebar"),
    "offline": ("surface", "surface_raised", "sidebar"),
}

# spec A3 (mục 10, ý 12): thêm luật `QLabel[tone="accent"]` màu `accent_text` (chữ của `StatusBadge` BUSY).
TONES = {
    "muted": "text_muted",
    "success": "success",
    "warning": "warning",
    "danger": "danger",
    "accent": "accent_text",
}

# spec A3, mục 6 T1: các cặp mà thành phần dùng chung (`ui/widgets.py`) vẽ. Tách riêng khỏi bảng A1
# để khi đỏ biết ngay cặp nào là của A3. Không hạ ngưỡng: đỏ thì chỉnh token tối thiểu.
A3_TEXT_PAIRS: dict[str, tuple[str, ...]] = {
    "text": ("accent_subtle", "success_bg", "warning_bg", "danger_bg"),  # chữ của InfoBar
    "accent_text": ("sidebar", "selected", "bubble_in", "bubble_out"),  # badge BUSY, chữ nhấn
    "success": ("bubble_in", "bubble_out", "selected"),
    "warning": ("sidebar",),
}
A3_NON_TEXT_PAIRS: dict[str, tuple[str, ...]] = {
    "accent_strong": ("accent_subtle", "bubble_in", "selected"),  # viền InfoBar INFO, ATTENTION, chấm BUSY
    "success": ("selected",),
    "offline": ("selected",),  # chấm/vòng OFFLINE trên hàng đang chọn
    "danger": ("bubble_out",),  # viền DANGER của bong bóng gửi đi
}

# Dòng chữ của các nhóm màu QPalette mà người dùng thấy khi cửa sổ có/không có tiêu điểm.
LIVE_GROUPS = (Group.Active, Group.Inactive)


# =================================================================================================
# Hàm phụ trợ (định nghĩa tại chỗ: không import conftest)
# =================================================================================================


def _linear(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(color: QColor) -> float:
    return 0.2126 * _linear(color.red()) + 0.7152 * _linear(color.green()) + 0.0722 * _linear(color.blue())


def wcag_ratio(fg: QColor, bg: QColor) -> float:
    """Tỉ lệ tương phản WCAG 2.x, độc lập với `theme.contrast_ratio`."""
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _hex(color: QColor) -> str:
    return color.name().upper()


def _close(a: QColor, b: QColor, tol: int) -> bool:
    return (
        abs(a.red() - b.red()) <= tol
        and abs(a.green() - b.green()) <= tol
        and abs(a.blue() - b.blue()) <= tol
    )


def _has_pixel_near(image, color: QColor, tol: int) -> bool:
    for y in range(image.height()):
        for x in range(image.width()):
            px = image.pixelColor(x, y)
            if px.alpha() >= 250 and _close(px, color, tol):
                return True
    return False


_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_RULE = re.compile(r"([^{}]*)\{([^{}]*)\}")


def parse_rules(css: str) -> list[tuple[list[str], dict[str, str]]]:
    """Tách stylesheet Qt thành [(các bộ chọn, {thuộc tính: giá trị})]. Ngoặc không khớp → AssertionError."""
    css = _COMMENT.sub("", css)
    rules: list[tuple[list[str], dict[str, str]]] = []
    consumed = 0
    for m in _RULE.finditer(css):
        assert not css[consumed : m.start()].strip(), (
            f"stylesheet có đoạn ngoài luật: {css[consumed : m.start()]!r}"
        )
        consumed = m.end()
        selectors = [s.strip() for s in m.group(1).split(",")]
        decls: dict[str, str] = {}
        for part in m.group(2).split(";"):
            if ":" in part:
                key, value = part.split(":", 1)
                decls[key.strip().lower()] = value.strip()
        rules.append((selectors, decls))
    assert not css[consumed:].strip(), (
        f"stylesheet có đoạn ngoài luật (ngoặc không khớp?): {css[consumed:]!r}"
    )
    return rules


def _norm_selector(selector: str) -> str:
    return re.sub(r"[\s\"']", "", selector)


def find_rule(css: str, selector: str) -> dict[str, str]:
    """Gộp khai báo của mọi luật có đúng bộ chọn `selector` (so sau khi bỏ dấu nháy, khoảng trắng)."""
    want = _norm_selector(selector)
    merged: dict[str, str] = {}
    found = False
    for selectors, decls in parse_rules(css):
        if any(_norm_selector(s) == want for s in selectors):
            found = True
            merged.update(decls)
    assert found, f"stylesheet không có luật cho {selector}"
    return merged


def css_color(value: str) -> QColor:
    """Màu đầu tiên dạng #rgb/#rrggbb trong một giá trị CSS (`1px solid #abc` cũng được)."""
    m = re.search(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", value)
    assert m, f"giá trị CSS không có mã màu #…: {value!r}"
    return QColor(m.group(0))


def background_of(decls: dict[str, str]) -> QColor:
    value = decls.get("background-color", decls.get("background"))
    assert value is not None, f"luật không đặt nền: {decls}"
    return css_color(value)


# =================================================================================================
# Fixture riêng
# =================================================================================================


@pytest.fixture
def themed(qapp):
    """`themed(dark) -> Tokens`: `theme.apply(qapp, scheme)`; khôi phục palette, stylesheet khi xong."""
    from teleport.ui import theme

    old_palette = QPalette(qapp.palette())
    old_css = qapp.styleSheet()

    def run(dark: bool):
        theme.apply(qapp, Qt.ColorScheme.Dark if dark else Qt.ColorScheme.Light)
        return theme.tokens(dark)

    yield run
    qapp.setStyleSheet(old_css)
    qapp.setPalette(old_palette)


# =================================================================================================
# API và hằng số
# =================================================================================================


def test_public_constants_match_spec():
    from teleport.ui import theme

    assert theme.SPACING == (4, 8, 12, 16, 24, 32)
    assert (
        theme.RADIUS_CONTROL,
        theme.RADIUS_CARD,
        theme.RADIUS_BLOCK,
        theme.RADIUS_BUBBLE,
        theme.RADIUS_BUBBLE_TAIL,
    ) == (6, 8, 12, 14, 4)
    assert theme.TONE_PROPERTY == "tone"
    assert theme.SURFACE_PROPERTY == "surface"
    assert theme.ACCENT_FAMILY == "Bricolage Grotesque"
    assert {s.name for s in theme.TextStyle} == {"TITLE", "SECTION", "BODY", "CAPTION"}


@MODES
def test_tokens_has_every_spec_field_as_opaque_valid_color(dark):
    from teleport.ui import theme

    tok = theme.tokens(dark)
    assert dataclasses.is_dataclass(tok)
    names = {f.name for f in dataclasses.fields(tok)}
    assert names == set(TOKEN_FIELDS)
    for name in TOKEN_FIELDS:
        color = getattr(tok, name)
        assert isinstance(color, QColor), name
        assert color.isValid(), name
        assert color.alpha() == 255, f"{name} phải đục (alpha 255), tương phản mới tính đúng"


def test_tokens_are_frozen():
    from teleport.ui import theme

    tok = theme.tokens(False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        tok.surface = QColor("#000000")  # type: ignore[misc]


def test_light_and_dark_tokens_differ_and_dark_is_darker():
    from teleport.ui import theme

    light, dark = theme.tokens(False), theme.tokens(True)
    assert _luminance(dark.surface) < _luminance(light.surface)
    assert _luminance(dark.text) > _luminance(light.text)


@MODES
def test_tokens_returns_independent_copies(dark):
    """Sửa QColor của một lần gọi (QColor có thể đổi tại chỗ) không làm lệch lần gọi sau."""
    from teleport.ui import theme

    first = theme.tokens(dark)
    original = _hex(first.surface)
    first.surface.setRgb(1, 2, 3)
    assert _hex(theme.tokens(dark).surface) == original


@MODES
def test_accent_subtle_is_bubble_out(dark):
    from teleport.ui import theme

    tok = theme.tokens(dark)
    assert _hex(tok.accent_subtle) == _hex(tok.bubble_out)


def test_contrast_ratio_matches_wcag_formula():
    from teleport.ui import theme

    assert theme.contrast_ratio(QColor("#000000"), QColor("#ffffff")) == pytest.approx(21.0, abs=1e-6)
    assert theme.contrast_ratio(QColor("#777777"), QColor("#777777")) == pytest.approx(1.0, abs=1e-6)
    for fg, bg in (("#0B5E47", "#BFEBD9"), ("#665B4D", "#F3EBDC"), ("#F2A38C", "#4A2A22")):
        a, b = QColor(fg), QColor(bg)
        assert theme.contrast_ratio(a, b) == pytest.approx(wcag_ratio(a, b), rel=1e-3)


# =================================================================================================
# (a) chữ ≥ 4,5 và (b) viền/icon ≥ 3
# =================================================================================================


@MODES
@pytest.mark.parametrize("fg_name", list(TEXT_PAIRS))
def test_text_tokens_reach_4_5_on_their_backgrounds(dark, fg_name):
    from teleport.ui import theme

    tok = theme.tokens(dark)
    fg = getattr(tok, fg_name)
    low = [
        f"{fg_name} {_hex(fg)} / {bg} {_hex(getattr(tok, bg))}: {wcag_ratio(fg, getattr(tok, bg)):.2f}"
        for bg in TEXT_PAIRS[fg_name]
        if wcag_ratio(fg, getattr(tok, bg)) < TEXT_MIN
    ]
    assert not low, f"chữ < {TEXT_MIN}:1: {low}"


@MODES
@pytest.mark.parametrize("fg_name", list(NON_TEXT_PAIRS))
def test_border_and_icon_tokens_reach_3_on_their_backgrounds(dark, fg_name):
    from teleport.ui import theme

    tok = theme.tokens(dark)
    fg = getattr(tok, fg_name)
    low = [
        f"{fg_name} {_hex(fg)} / {bg} {_hex(getattr(tok, bg))}: {wcag_ratio(fg, getattr(tok, bg)):.2f}"
        for bg in NON_TEXT_PAIRS[fg_name]
        if wcag_ratio(fg, getattr(tok, bg)) < NON_TEXT_MIN
    ]
    assert not low, f"viền/icon < {NON_TEXT_MIN}:1: {low}"


@MODES
@pytest.mark.parametrize("fg_name", list(A3_TEXT_PAIRS))
def test_a3_text_pairs_reach_4_5(dark, fg_name):
    from teleport.ui import theme

    tok = theme.tokens(dark)
    fg = getattr(tok, fg_name)
    low = [
        f"{fg_name} {_hex(fg)} / {bg} {_hex(getattr(tok, bg))}: {wcag_ratio(fg, getattr(tok, bg)):.2f}"
        for bg in A3_TEXT_PAIRS[fg_name]
        if wcag_ratio(fg, getattr(tok, bg)) < TEXT_MIN
    ]
    assert not low, f"chữ < {TEXT_MIN}:1: {low}"


@MODES
@pytest.mark.parametrize("fg_name", list(A3_NON_TEXT_PAIRS))
def test_a3_non_text_pairs_reach_3(dark, fg_name):
    from teleport.ui import theme

    tok = theme.tokens(dark)
    fg = getattr(tok, fg_name)
    low = [
        f"{fg_name} {_hex(fg)} / {bg} {_hex(getattr(tok, bg))}: {wcag_ratio(fg, getattr(tok, bg)):.2f}"
        for bg in A3_NON_TEXT_PAIRS[fg_name]
        if wcag_ratio(fg, getattr(tok, bg)) < NON_TEXT_MIN
    ]
    assert not low, f"viền/icon < {NON_TEXT_MIN}:1: {low}"


@MODES
def test_focus_ring_color_reaches_3_on_surface(dark):
    from teleport.ui import theme

    tok = theme.tokens(dark)
    ring = theme.focus_ring_color(dark)
    assert wcag_ratio(ring, tok.surface) >= NON_TEXT_MIN, f"{_hex(ring)} trên {_hex(tok.surface)}"


@MODES
def test_fusion_focus_frame_reaches_3_on_surfaces(dark):
    """Khung tiêu điểm Fusion (`highlightedOutline`): sáng là `highlight().darker(125)`, tối Fusion vẽ
    sáng hơn (tương phản cao hơn), nên `darker(125)` là chặn dưới ở cả hai chế độ."""
    from teleport.ui import theme

    tok = theme.tokens(dark)
    frame = theme.palette(dark).color(Role.Highlight).darker(125)
    for bg_name in ("surface", "surface_raised"):
        bg = getattr(tok, bg_name)
        assert wcag_ratio(frame, bg) >= NON_TEXT_MIN, (
            f"khung tiêu điểm {_hex(frame)} trên {bg_name} {_hex(bg)}"
        )


# =================================================================================================
# (c) QPalette
# =================================================================================================


def _expected_roles(tok, dark: bool) -> dict:
    bright = tok.surface if dark else tok.surface_raised
    return {
        Role.Window: tok.surface,
        Role.Base: tok.surface_raised,
        Role.AlternateBase: tok.sidebar,
        Role.Text: tok.text,
        Role.WindowText: tok.text,
        Role.ButtonText: tok.text,
        Role.ToolTipText: tok.text,
        Role.ToolTipBase: tok.surface_raised,
        Role.Highlight: tok.accent_strong,
        Role.HighlightedText: bright,
        Role.Link: tok.accent_text,
        Role.LinkVisited: tok.accent_text,
        Role.PlaceholderText: tok.text_muted,
        Role.Light: tok.surface_raised,
        Role.Midlight: tok.selected,
        Role.Mid: tok.border_strong,
        Role.Shadow: tok.text,
        Role.BrightText: bright,
    }


@MODES
def test_palette_maps_roles_to_tokens(dark):
    from teleport.ui import theme

    tok = theme.tokens(dark)
    pal = theme.palette(dark)
    wrong = []
    for group in LIVE_GROUPS:
        for role, want in _expected_roles(tok, dark).items():
            got = pal.color(group, role)
            if _hex(got) != _hex(want):
                wrong.append(f"{role.name} ({group.name}): {_hex(got)} ≠ {_hex(want)}")
    assert not wrong, wrong


@MODES
def test_palette_dark_role_is_darker_border_with_3_on_surface(dark):
    """Dark = border_strong tối hơn, vẫn ≥ 3:1 trên surface (viền nút/khung Fusion)."""
    from teleport.ui import theme

    tok = theme.tokens(dark)
    for group in LIVE_GROUPS:
        dark_role = theme.palette(dark).color(group, Role.Dark)
        assert _luminance(dark_role) < _luminance(tok.border_strong), f"Dark ({group.name}) {_hex(dark_role)}"
        assert wcag_ratio(dark_role, tok.surface) >= NON_TEXT_MIN, f"Dark ({group.name}) {_hex(dark_role)}"


@MODES
def test_palette_disabled_text_is_text_muted(dark):
    from teleport.ui import theme

    tok = theme.tokens(dark)
    pal = theme.palette(dark)
    for role in (Role.WindowText, Role.Text, Role.ButtonText):
        assert _hex(pal.color(Group.Disabled, role)) == _hex(tok.text_muted), role.name


@MODES
def test_palette_text_pairs_reach_4_5(dark):
    """ButtonText/Button (Button do giao-dien chọn), HighlightedText/Highlight và chữ tắt trên nút ≥ 4,5."""
    from teleport.ui import theme

    pal = theme.palette(dark)
    pairs = [
        (Group.Active, Role.ButtonText, Role.Button),
        (Group.Inactive, Role.ButtonText, Role.Button),
        (Group.Active, Role.HighlightedText, Role.Highlight),
        (Group.Inactive, Role.HighlightedText, Role.Highlight),
        (Group.Active, Role.Text, Role.Base),
        (Group.Active, Role.WindowText, Role.Window),
        (Group.Active, Role.ToolTipText, Role.ToolTipBase),
        (Group.Active, Role.Text, Role.AlternateBase),
        (Group.Active, Role.PlaceholderText, Role.Base),
        (Group.Active, Role.Link, Role.Base),
        (Group.Disabled, Role.ButtonText, Role.Button),
    ]
    low = []
    for group, fg_role, bg_role in pairs:
        fg, bg = pal.color(group, fg_role), pal.color(group, bg_role)
        if wcag_ratio(fg, bg) < TEXT_MIN:
            low.append(f"{fg_role.name}/{bg_role.name} ({group.name}): {wcag_ratio(fg, bg):.2f}")
    assert not low, low


@MODES
def test_apply_installs_token_palette(themed, qapp, dark):
    tok = themed(dark)
    pal = qapp.palette()
    assert _hex(pal.color(Role.Window)) == _hex(tok.surface)
    assert _hex(pal.color(Role.Highlight)) == _hex(tok.accent_strong)


# =================================================================================================
# (d) nút chính
# =================================================================================================


@MODES
def test_primary_colors_come_from_accent_tokens(dark):
    """(chữ, nền): normal/hover/pressed = accent_text trên accent / accent_hover / accent_pressed."""
    from teleport.ui import theme

    tok = theme.tokens(dark)
    colors = theme.primary_colors(dark)
    assert {"normal", "hover", "pressed", "disabled"} <= set(colors)
    for state, bg_name in (("normal", "accent"), ("hover", "accent_hover"), ("pressed", "accent_pressed")):
        text, bg = colors[state]
        assert _hex(text) == _hex(tok.accent_text), state
        assert _hex(bg) == _hex(getattr(tok, bg_name)), state


@MODES
def test_primary_states_are_distinguishable(dark):
    from teleport.ui import theme

    colors = theme.primary_colors(dark)
    backgrounds = {_hex(colors[s][1]) for s in ("normal", "hover", "pressed")}
    assert len(backgrounds) == 3, "hover/pressed phải khác nền thường"


@MODES
def test_primary_colors_every_state_reaches_4_5(dark):
    from teleport.ui import theme

    low = [
        f"{state}: {_hex(text)} / {_hex(bg)} = {wcag_ratio(text, bg):.2f}"
        for state, (text, bg) in theme.primary_colors(dark).items()
        if wcag_ratio(text, bg) < TEXT_MIN
    ]
    assert not low, low


# =================================================================================================
# (e) stylesheet
# =================================================================================================


@MODES
@pytest.mark.parametrize("button_height", [None, 30])
def test_every_stylesheet_selector_is_narrow(dark, button_height):
    """Mọi bộ chọn có `[property]` hoặc `#objectName`: không đè style Fusion của widget khác."""
    from teleport.ui import theme

    css = theme.stylesheet(dark, button_height)
    rules = parse_rules(css)
    assert rules, "stylesheet rỗng"
    broad = [s for selectors, _ in rules for s in selectors if "[" not in s and "#" not in s]
    assert not broad, f"bộ chọn rộng: {broad}"


@MODES
def test_primary_rule_uses_primary_colors_directly(dark):
    from teleport.ui import theme

    css = theme.stylesheet(dark)
    tok = theme.tokens(dark)
    colors = theme.primary_colors(dark)
    sel = f'QPushButton[{theme.PRIMARY_PROPERTY}="true"]'
    base = find_rule(css, sel)
    assert "palette(" not in " ".join(base.values()), f"nút chính không đi qua palette(...): {base}"
    assert _hex(background_of(base)) == _hex(colors["normal"][1]) == _hex(tok.accent)
    assert _hex(css_color(base["color"])) == _hex(colors["normal"][0]) == _hex(tok.accent_text)
    assert base.get("border-radius") == f"{theme.RADIUS_CONTROL}px"
    assert _hex(background_of(find_rule(css, f"{sel}:hover"))) == _hex(tok.accent_hover)
    assert _hex(background_of(find_rule(css, f"{sel}:pressed"))) == _hex(tok.accent_pressed)


@MODES
@pytest.mark.parametrize("tone", list(TONES))
def test_tone_rule_sets_text_color(dark, tone):
    from teleport.ui import theme

    tok = theme.tokens(dark)
    decls = find_rule(theme.stylesheet(dark), f'QLabel[{theme.TONE_PROPERTY}="{tone}"]')
    assert _hex(css_color(decls["color"])) == _hex(getattr(tok, TONES[tone]))


@MODES
@pytest.mark.parametrize(("surface", "token"), [("sidebar", "sidebar"), ("raised", "surface_raised")])
def test_surface_rule_sets_background_and_border(dark, surface, token):
    from teleport.ui import theme

    tok = theme.tokens(dark)
    decls = find_rule(theme.stylesheet(dark), f'QFrame[{theme.SURFACE_PROPERTY}="{surface}"]')
    assert _hex(background_of(decls)) == _hex(getattr(tok, token))
    # Viền có thể chỉ ở một cạnh (thanh bên: `border-right`); mọi màu viền khai báo phải là `border`.
    border_colors = [
        css_color(value)
        for key, value in decls.items()
        if key.startswith("border") and "radius" not in key and re.search(r"#[0-9a-fA-F]{3,6}\b", value)
    ]
    assert border_colors, f"QFrame[surface={surface}] không có viền màu: {decls}"
    assert {_hex(c) for c in border_colors} == {_hex(tok.border)}


@MODES
def test_card_surface_rule_exists(dark):
    from teleport.ui import theme

    decls = find_rule(theme.stylesheet(dark), f'QFrame[{theme.SURFACE_PROPERTY}="card"]')
    background_of(decls)


@MODES
def test_apply_installs_stylesheet_of_the_scheme(themed, qapp, dark):
    from teleport.ui import theme

    themed(dark)
    # `apply` đo chiều cao nút Fusion nên không so nguyên văn; mọi luật của `stylesheet(dark)` phải có.
    installed = {_norm_selector(s) for selectors, _ in parse_rules(qapp.styleSheet()) for s in selectors}
    wanted = {_norm_selector(s) for selectors, _ in parse_rules(theme.stylesheet(dark)) for s in selectors}
    assert wanted <= installed


# =================================================================================================
# (f) thang chữ
# =================================================================================================


def _point_base(family: str = "Teleport Test Sans", size: float = 10.0) -> QFont:
    font = QFont(family)
    font.setPointSizeF(size)
    return font


def test_accent_family_reports_none_when_font_missing(monkeypatch):
    from PySide6.QtGui import QFontDatabase

    from teleport.ui import theme

    asked: list[str] = []

    def has_family(name):
        asked.append(name)
        return False

    monkeypatch.setattr(QFontDatabase, "hasFamily", staticmethod(has_family))
    assert theme.accent_family() is None
    assert theme.ACCENT_FAMILY in asked


def test_accent_family_reports_name_when_font_present(monkeypatch):
    from PySide6.QtGui import QFontDatabase

    from teleport.ui import theme

    monkeypatch.setattr(QFontDatabase, "hasFamily", staticmethod(lambda name: name == theme.ACCENT_FAMILY))
    assert theme.accent_family() == theme.ACCENT_FAMILY


@pytest.mark.parametrize("accent", [None, "Bricolage Grotesque"], ids=["khong-co-font", "co-font"])
def test_font_for_point_scale(monkeypatch, qapp, accent):
    from teleport.ui import theme

    monkeypatch.setattr(theme, "accent_family", lambda: accent)
    base = _point_base()
    title = theme.font_for(theme.TextStyle.TITLE, base)
    section = theme.font_for(theme.TextStyle.SECTION, base)
    body = theme.font_for(theme.TextStyle.BODY, base)
    caption = theme.font_for(theme.TextStyle.CAPTION, base)

    assert title.pointSizeF() == pytest.approx(16.0)
    assert title.weight() == QFont.Weight.Bold
    assert section.pointSizeF() == pytest.approx(12.0)
    assert section.weight() == QFont.Weight.Medium
    assert body.pointSizeF() == pytest.approx(10.0)
    assert caption.pointSizeF() == pytest.approx(9.0)
    # Thân và chú thích luôn theo font được truyền (không ghi cứng font thân).
    assert body.family() == base.family()
    assert caption.family() == base.family()
    expected_heading = accent if accent is not None else base.family()
    assert title.family() == expected_heading
    assert section.family() == expected_heading


def test_font_for_without_accent_keeps_app_family_and_still_bold(monkeypatch, qapp):
    """Vá `accent_family` → None: TITLE/SECTION dùng font ứng dụng nhưng vẫn đậm (spec (f))."""
    from teleport.ui import theme

    monkeypatch.setattr(theme, "accent_family", lambda: None)
    app_font = qapp.font()
    title = theme.font_for(theme.TextStyle.TITLE)
    section = theme.font_for(theme.TextStyle.SECTION)
    assert title.family() == app_font.family()
    assert section.family() == app_font.family()
    assert title.weight() == QFont.Weight.Bold
    assert section.weight() == QFont.Weight.Medium


def test_font_for_defaults_to_application_font(monkeypatch, qapp):
    from teleport.ui import theme

    monkeypatch.setattr(theme, "accent_family", lambda: None)
    old = qapp.font()
    custom = _point_base("Teleport App Font", 11.0)
    qapp.setFont(custom)
    try:
        body = theme.font_for(theme.TextStyle.BODY)
        title = theme.font_for(theme.TextStyle.TITLE)
        assert body.family() == "Teleport App Font"
        assert body.pointSizeF() == pytest.approx(11.0)
        assert title.pointSizeF() == pytest.approx(17.0)
    finally:
        qapp.setFont(old)


def test_font_for_pixel_sized_base_scales_in_pixels(monkeypatch, qapp):
    """Font cỡ pixel: TITLE +8 px, SECTION +3 px, BODY giữ, CAPTION −1 px (bản ghi đè hoi-lai)."""
    from teleport.ui import theme

    monkeypatch.setattr(theme, "accent_family", lambda: None)
    base = QFont("Teleport Test Sans")
    base.setPixelSize(13)
    sizes = {style: theme.font_for(style, base).pixelSize() for style in theme.TextStyle}
    assert sizes == {
        theme.TextStyle.TITLE: 21,
        theme.TextStyle.SECTION: 16,
        theme.TextStyle.BODY: 13,
        theme.TextStyle.CAPTION: 12,
    }


def test_font_for_does_not_mutate_base(monkeypatch, qapp):
    from teleport.ui import theme

    monkeypatch.setattr(theme, "accent_family", lambda: "Bricolage Grotesque")
    base = _point_base()
    before = base.toString()
    for style in theme.TextStyle:
        theme.font_for(style, base)
    assert base.toString() == before


# =================================================================================================
# (g) điểm ảnh thật
# =================================================================================================


def _big_bold_label(qtbot, text: str, tone: str | None):
    from PySide6.QtWidgets import QLabel

    from teleport.ui import theme

    label = QLabel(text)
    font = QFont(label.font())
    font.setPixelSize(48)
    font.setWeight(QFont.Weight.Black)
    label.setFont(font)
    if tone is not None:
        label.setProperty(theme.TONE_PROPERTY, tone)  # trước khi hiện: không cần polish lại
    qtbot.addWidget(label)
    label.resize(320, 80)
    label.show()
    qtbot.waitExposed(label)
    return label


@MODES
@pytest.mark.parametrize("tone", list(TONES))
def test_tone_label_paints_tone_color(themed, qtbot, dark, tone):
    tok = themed(dark)
    want = getattr(tok, TONES[tone])
    label = _big_bold_label(qtbot, "WMWM", tone)
    image = label.grab().toImage()
    assert _has_pixel_near(image, want, 8), f"không có điểm ảnh gần {_hex(want)} (tone={tone})"


@MODES
def test_label_without_tone_does_not_paint_danger(themed, qtbot, dark):
    """Đối chứng cho (g): không có `tone` thì không có màu danger (test trên không xanh vì tình cờ)."""
    tok = themed(dark)
    label = _big_bold_label(qtbot, "WMWM", None)
    assert not _has_pixel_near(label.grab().toImage(), tok.danger, 8)


@MODES
@pytest.mark.parametrize(("surface", "token"), [("sidebar", "sidebar"), ("raised", "surface_raised")])
def test_surface_frame_paints_its_background(themed, qtbot, dark, surface, token):
    from PySide6.QtWidgets import QFrame

    from teleport.ui import theme

    tok = themed(dark)
    frame = QFrame()
    frame.setProperty(theme.SURFACE_PROPERTY, surface)
    qtbot.addWidget(frame)
    frame.resize(120, 80)
    frame.show()
    qtbot.waitExposed(frame)
    center = frame.grab().toImage().pixelColor(60, 40)
    assert _close(center, getattr(tok, token), 2), f"{_hex(center)} ≠ {_hex(getattr(tok, token))}"


@MODES
def test_primary_button_paints_accent_background(themed, qtbot, dark):
    from PySide6.QtWidgets import QPushButton

    from teleport.ui import theme

    tok = themed(dark)
    button = QPushButton("Gửi")
    button.setProperty(theme.PRIMARY_PROPERTY, True)
    qtbot.addWidget(button)
    button.resize(140, 40)
    button.show()
    qtbot.waitExposed(button)
    image = button.grab().toImage()
    assert _has_pixel_near(image, tok.accent, 2), f"nút chính không có nền {_hex(tok.accent)}"
