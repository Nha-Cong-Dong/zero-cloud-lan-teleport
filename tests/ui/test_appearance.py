"""Mục “Giao diện” của Cài đặt: `teleport.ui.appearance.AppearanceSection` và `widgets.SegmentedControl`
(spec D6 mục 2.5, câu chữ mục 3, test 5.5; mục 12 và 13 thắng các mục trên khi khác).

Viết từ spec, không từ code. `AppearanceSection` là view thuần: người dùng đổi gì thì phát signal
(`theme_selected` None | "light" | "dark", `accent_selected` None | mã, `language_selected`), còn
`set_*` đặt giá trị mà **không** phát signal. Ba hàng từ trên xuống: Chế độ màu (`SegmentedControl` ba
`QRadioButton`: Theo hệ điều hành · Sáng · Tối), Màu nhấn (6 ô theo thứ tự `theme.ACCENTS`, nhãn
“Đang dùng: {tên}”; chọn `mint` phát None), Ngôn ngữ (ô `language_combo` chuyển từ D2).

Giả định để tìm control (không phụ thuộc tên thuộc tính riêng của giao-dien):
- Ba nút chế độ là các `QRadioButton` con của đúng một `SegmentedControl` trong mục, sắp theo trục x.
- Ô màu là các `QAbstractButton` có `accessibleDescription() == "Màu nhấn"` (spec 2.5), sắp theo vị trí
  (y rồi x); tên trợ năng là tên màu (`theme.accent_name`).
- Nhãn hàng là `QLabel` có chữ đúng bằng nhãn ở bảng mục 3.
"""

from __future__ import annotations

import html
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QPoint, Qt, QTranslator
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication, QLabel, QRadioButton, QWidget

# ---- Câu chữ (spec D6 mục 3) ---------------------------------------------------------------------
MODE_LABEL = "Chế độ màu"
MODE_OPTIONS = ("Theo hệ điều hành", "Sáng", "Tối")
MODE_VALUES = (None, "light", "dark")
MODE_HINT = "Chọn “Theo hệ điều hành” để Teleport đổi sáng/tối cùng hệ điều hành."
ACCENT_LABEL = "Màu nhấn"
ACCENT_HINT = "Dùng cho nút chính, thanh tiến độ và khung file gửi đi."
CURRENT = "Đang dùng: {name}"
ACCENT_IDS = ("mint", "ocean", "violet", "rose", "amber", "graphite")
ACCENT_NAMES = ("Bạc hà", "Đại dương", "Tím", "Hồng", "Hổ phách", "Than chì")
LANGUAGE_LABEL = "Ngôn ngữ"
LANGUAGE_SYSTEM = "Theo hệ điều hành"
LANGUAGE_HINT = "Đổi ngay, không cần mở lại Teleport."
ACCENT_DESCRIPTION = "Màu nhấn"

MIN_HIT = 24  # px, vùng bấm tối thiểu (WCAG 2.5.8)
MIN_NON_TEXT = 3.0  # WCAG 1.4.11
PREFIX = "[x]"


# =================================================================================================
# Phụ trợ
# =================================================================================================


def settle(qtbot, ms: int = 30) -> None:
    QApplication.processEvents()
    qtbot.wait(ms)


def activate(qtbot, window: QWidget) -> None:
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    window.raise_()
    qtbot.waitUntil(window.isActiveWindow, timeout=3000)
    QApplication.processEvents()


def plain(text: str) -> str:
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


def tooltip_text(text: str) -> str:
    """Chữ của tooltip, chấp nhận cả dạng `widgets.safe_tooltip` (`<qt>…</qt>`, đã thoát HTML)."""
    if text.startswith("<qt>") and text.endswith("</qt>"):
        text = html.unescape(text[4:-5])
    return text


def contrast(a, b) -> float:
    """Tỉ lệ tương phản WCAG 2.x, tính độc lập với `theme.contrast_ratio`."""

    def lum(c) -> float:
        def ch(v: float) -> float:
            v /= 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

        return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def pos(widget: QWidget, root: QWidget) -> QPoint:
    return widget.mapTo(root, QPoint(0, 0))


def mode_buttons(root: QWidget) -> list[QRadioButton]:
    """Ba nút của `SegmentedControl` chế độ màu, trái sang phải."""
    from teleport.ui.widgets import SegmentedControl

    controls = [c for c in root.findChildren(SegmentedControl) if c.isVisibleTo(root)]
    assert len(controls) == 1, f"cần đúng một SegmentedControl, thấy {len(controls)}"
    buttons = [b for b in controls[0].findChildren(QRadioButton) if b.isVisibleTo(root)]
    assert len(buttons) == 3, f"SegmentedControl cần ba QRadioButton, thấy {len(buttons)}"
    return sorted(buttons, key=lambda b: pos(b, root).x())


def checked_mode(root: QWidget) -> object:
    buttons = mode_buttons(root)
    checked = [i for i, b in enumerate(buttons) if b.isChecked()]
    assert len(checked) == 1, f"cần đúng một chế độ được chọn: {checked}"
    return MODE_VALUES[checked[0]]


def swatches(root: QWidget) -> list[QAbstractButton]:
    """Các ô màu nhấn (mô tả trợ năng “Màu nhấn”), theo thứ tự hiển thị."""
    found = [
        b
        for b in root.findChildren(QAbstractButton)
        if b.accessibleDescription().endswith(ACCENT_DESCRIPTION) and b.isVisibleTo(root)
    ]
    return sorted(found, key=lambda b: (pos(b, root).y(), pos(b, root).x()))


def swatch(root: QWidget, accent: str) -> QAbstractButton:
    return swatches(root)[ACCENT_IDS.index(accent)]


def checked_accent(root: QWidget) -> str:
    checked = [ACCENT_IDS[i] for i, b in enumerate(swatches(root)) if b.isChecked()]
    assert len(checked) == 1, f"cần đúng một ô màu được chọn: {checked}"
    return checked[0]


def labels(root: QWidget) -> list[QLabel]:
    return [w for w in root.findChildren(QLabel) if w.isVisibleTo(root) and w.text()]


def label_with(root: QWidget, text: str) -> QLabel:
    found = [w for w in labels(root) if plain(w.text()) == text]
    assert found, f"không thấy nhãn {text!r}: {[w.text() for w in labels(root)]}"
    return found[0]


def all_text(root: QWidget) -> str:
    return "\n".join(plain(w.text()) for w in labels(root))


class Spy:
    NAMES = ("theme_selected", "accent_selected", "language_selected")

    def __init__(self, widget) -> None:
        self.log: list[tuple] = []
        for name in self.NAMES:
            getattr(widget, name).connect(lambda *a, _n=name: self.log.append((_n, *a)))

    def of(self, name: str) -> list:
        return [e[1] for e in self.log if e[0] == name]

    def clear(self) -> None:
        self.log.clear()


class PrefixTranslator(QTranslator):
    def isEmpty(self):
        return False

    def translate(self, context, source_text, disambiguation=None, n=-1):
        if not source_text:
            return source_text
        return PREFIX + source_text


@pytest.fixture
def sec(qtbot):
    """`AppearanceSection` đứng riêng, đã nạp (theo HĐH, màu mặc định, ngôn ngữ theo HĐH), đang hiện."""
    from teleport.ui.appearance import AppearanceSection

    w = AppearanceSection()
    qtbot.addWidget(w)
    w.resize(640, 460)
    w.set_loaded(True)
    w.set_theme(None)
    w.set_accent(None)
    w.set_language(None, ("vi",))
    w.show()
    qtbot.waitExposed(w)
    settle(qtbot)
    return SimpleNamespace(w=w, spy=Spy(w), qtbot=qtbot)


# =================================================================================================
# Bố cục, câu chữ
# =================================================================================================


def test_three_rows_mode_accent_language_top_to_bottom(sec):
    w = sec.w
    ys = [pos(label_with(w, t), w).y() for t in (MODE_LABEL, ACCENT_LABEL, LANGUAGE_LABEL)]
    assert ys == sorted(ys) and len(set(ys)) == 3, f"thứ tự hàng: chế độ, màu nhấn, ngôn ngữ ({ys})"


def test_texts_match_spec_section_3(sec):
    text = all_text(sec.w)
    for want in (
        MODE_LABEL,
        MODE_HINT,
        ACCENT_LABEL,
        ACCENT_HINT,
        CURRENT.format(name="Bạc hà"),
        LANGUAGE_LABEL,
        LANGUAGE_HINT,
    ):
        assert want in text, f"thiếu câu {want!r} trong:\n{text}"
    assert [plain(b.text()) for b in mode_buttons(sec.w)] == list(MODE_OPTIONS)
    assert sec.w.language_combo.itemText(0) == LANGUAGE_SYSTEM
    assert sec.w.language_combo.accessibleName() == LANGUAGE_LABEL


def test_segmented_control_segments_first_middle_last(sec):
    assert [b.property("segment") for b in mode_buttons(sec.w)] == ["first", "middle", "last"]
    assert all(b.autoExclusive() for b in mode_buttons(sec.w))


# =================================================================================================
# Chế độ màu
# =================================================================================================


def test_mode_click_emits_none_light_dark(sec):
    buttons = mode_buttons(sec.w)
    buttons[2].click()
    buttons[1].click()
    buttons[0].click()
    assert sec.spy.of("theme_selected") == ["dark", "light", None]


@pytest.mark.parametrize(("value", "index"), [(None, 0), ("light", 1), ("dark", 2)])
def test_set_theme_checks_button_without_signal(sec, value, index):
    sec.w.set_theme("dark" if value is None else None)  # đổi khỏi giá trị cần kiểm trước
    sec.w.set_theme(value)
    assert checked_mode(sec.w) == MODE_VALUES[index]
    assert sec.spy.log == []


def test_mode_arrow_keys_move_and_emit(sec):
    activate(sec.qtbot, sec.w)
    buttons = mode_buttons(sec.w)
    buttons[0].setFocus(Qt.FocusReason.TabFocusReason)
    settle(sec.qtbot)
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Right)
    assert checked_mode(sec.w) == "light"
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Right)
    assert checked_mode(sec.w) == "dark"
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Left)
    assert checked_mode(sec.w) == "light"
    assert QApplication.focusWidget() is buttons[1], "mũi tên đưa tiêu điểm theo lựa chọn"
    assert sec.spy.of("theme_selected") == ["light", "dark", "light"]


# =================================================================================================
# Màu nhấn
# =================================================================================================


def test_six_swatches_in_accents_order_with_names(sec):
    from teleport.ui import theme

    assert theme.ACCENTS == ACCENT_IDS, "thứ tự hiển thị ghim theo spec 2.2"
    found = swatches(sec.w)
    assert len(found) == 6
    assert [b.accessibleName() for b in found] == list(ACCENT_NAMES)
    assert [b.accessibleName() for b in found] == [theme.accent_name(a) for a in theme.ACCENTS]
    assert [tooltip_text(b.toolTip()) for b in found] == list(ACCENT_NAMES), "tooltip = tên màu"
    assert all(b.isCheckable() for b in found)
    assert all(b.autoExclusive() for b in found)


def test_swatch_hit_area_at_least_24px(sec):
    for b in swatches(sec.w):
        assert b.width() >= MIN_HIT and b.height() >= MIN_HIT, (b.accessibleName(), b.size())


@pytest.mark.parametrize("dark", [False, True], ids=["sang", "toi"])
def test_swatch_color_contrast_3_to_1_with_background(sec, ui_theme, dark):
    """Màu tô ô (lấy mẫu từ ảnh vẽ thật) ≥ 3:1 với nền ngay ngoài vòng tròn, mọi bộ × sáng/tối."""
    from teleport.ui import theme

    t = ui_theme(dark)
    w = sec.w
    for accent in ACCENT_IDS:
        # Ô đang chọn có dấu tích ở giữa: chọn bộ khác để lấy mẫu phần tô của ô `accent`.
        w.set_accent("ocean" if accent == "mint" else "mint")
        settle(sec.qtbot)
        b = swatch(w, accent)
        image = w.grab().toImage()
        ratio = image.devicePixelRatio() or 1.0
        top_left = pos(b, w)
        center = top_left + QPoint(b.width() // 2, b.height() // 2)

        def at(p: QPoint, *, _image=image, _ratio=ratio):
            return _image.pixelColor(int(p.x() * _ratio), int(p.y() * _ratio))

        fill = at(center - QPoint(8, 0))
        background = at(top_left + QPoint(1, 1))
        want = theme.accent_swatch(accent, dark)
        assert (
            max(
                abs(fill.red() - want.red()), abs(fill.green() - want.green()), abs(fill.blue() - want.blue())
            )
            <= 8
        ), f"{accent}: ô tô {fill.name()}, cần accent_swatch {want.name()}"
        got = contrast(fill, background)
        assert got >= MIN_NON_TEXT, (
            f"{accent}/{'tối' if dark else 'sáng'}: {got:.2f}:1 trên {background.name()}"
        )
        for name in ("surface_raised", "surface"):
            bg = getattr(t, name)
            assert contrast(want, bg) >= MIN_NON_TEXT, f"{accent} trên {name}"


def test_clicking_mint_emits_none_other_emits_id(sec):
    w = sec.w
    w.set_accent("ocean")
    swatch(w, "mint").click()
    swatch(w, "violet").click()
    swatch(w, "graphite").click()
    assert sec.spy.of("accent_selected") == [None, "violet", "graphite"]
    assert checked_accent(w) == "graphite"


def test_accent_arrow_keys_move_and_select(sec):
    activate(sec.qtbot, sec.w)
    w = sec.w
    swatch(w, "mint").setFocus(Qt.FocusReason.TabFocusReason)
    settle(sec.qtbot)
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Right)
    assert checked_accent(w) == "ocean"
    assert QApplication.focusWidget() is swatch(w, "ocean")
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Right)
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Left)
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Left)
    assert checked_accent(w) == "mint"
    assert sec.spy.of("accent_selected") == ["ocean", "violet", "ocean", None]


@pytest.mark.parametrize(
    "value",
    ["lạ", "khong_co", "MINT", "../mint", "x" * 300],
    ids=["dau", "khong-co", "hoa", "duong-dan", "dai"],
)
def test_unknown_accent_shows_default(sec, value):
    w = sec.w
    w.set_accent("rose")
    w.set_accent(value)
    settle(sec.qtbot)
    assert checked_accent(w) == "mint"
    assert CURRENT.format(name="Bạc hà") in all_text(w)
    assert sec.spy.log == []


def test_current_label_follows_selection(sec):
    w = sec.w
    w.set_accent("amber")
    settle(sec.qtbot)
    assert CURRENT.format(name="Hổ phách") in all_text(w)
    swatch(w, "graphite").click()
    settle(sec.qtbot)
    text = all_text(w)
    assert CURRENT.format(name="Than chì") in text
    assert CURRENT.format(name="Hổ phách") not in text


def test_set_accent_checks_swatch_without_signal(sec):
    for accent in ACCENT_IDS:
        sec.w.set_accent(accent)
        assert checked_accent(sec.w) == accent
    sec.w.set_accent(None)
    assert checked_accent(sec.w) == "mint"
    assert sec.spy.log == []


# =================================================================================================
# Ngôn ngữ, nạp, dịch lại
# =================================================================================================


def test_language_combo_emits_language_selected(sec):
    combo = sec.w.language_combo
    sec.w.set_language(None, ("vi",))
    index = combo.findData("vi")
    assert index > 0, "có mục Tiếng Việt sau “Theo hệ điều hành”"
    combo.setCurrentIndex(index)
    combo.activated.emit(index)
    combo.setCurrentIndex(0)
    combo.activated.emit(0)
    assert sec.spy.of("language_selected") == ["vi", None]


def test_set_star_emit_nothing(sec):
    w = sec.w
    w.set_theme("dark")
    w.set_accent("violet")
    w.set_language("vi", ("vi",))
    w.set_loaded(False)
    w.set_loaded(True)
    settle(sec.qtbot)
    assert sec.spy.log == []
    assert checked_mode(w) == "dark"
    assert checked_accent(w) == "violet"
    assert w.language_combo.currentData() == "vi"


def test_set_loaded_false_disables_every_control(sec):
    w = sec.w
    controls = [*mode_buttons(w), *swatches(w), w.language_combo]
    w.set_loaded(False)
    assert not any(c.isEnabled() for c in controls), "chưa nạp: ba hàng đều tắt"
    w.set_loaded(True)
    assert all(c.isEnabled() for c in controls)


def test_language_change_retranslates_keeps_selection(sec):
    w = sec.w
    w.set_theme("dark")
    w.set_accent("ocean")
    w.set_language("vi", ("vi",))
    translator = PrefixTranslator()
    QCoreApplication.installTranslator(translator)
    try:
        sec.qtbot.waitUntil(
            lambda: any(plain(x.text()).startswith(PREFIX + MODE_LABEL) for x in labels(w)), timeout=3000
        )
        assert checked_mode(w) == "dark"
        assert checked_accent(w) == "ocean"
        assert w.language_combo.currentData() == "vi"
        assert plain(mode_buttons(w)[0].text()) == PREFIX + MODE_OPTIONS[0]
        assert swatch(w, "ocean").accessibleName() == PREFIX + "Đại dương", (
            "tên màu dịch lại (ngữ cảnh Theme)"
        )
        assert any(PREFIX + "Đang dùng" in plain(x.text()) for x in labels(w))
    finally:
        QCoreApplication.removeTranslator(translator)
    sec.qtbot.waitUntil(lambda: any(plain(x.text()) == MODE_LABEL for x in labels(w)), timeout=3000)
    assert checked_accent(w) == "ocean"
    assert CURRENT.format(name="Đại dương") in all_text(w)
    assert sec.spy.log == [], "dịch lại không phát signal"


def test_unknown_saved_accent_then_mint_click_emits_none(qtbot) -> None:
    """Mã lạ đúng dạng đã lưu hiện ô mặc định; bấm ô đó vẫn phát None để xóa mã lạ (soát D6a-2)."""
    from teleport.ui.appearance import AppearanceSection

    section = AppearanceSection()
    qtbot.addWidget(section)
    section.set_accent("teal")
    emitted: list[object] = []
    section.accent_selected.connect(emitted.append)
    section.accent_picker.swatch("mint").click()
    assert emitted == [None]
    section.accent_picker.swatch("mint").click()
    assert emitted == [None]
