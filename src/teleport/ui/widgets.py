"""Thành phần dùng chung của giao diện (spec A3): nút icon, badge trạng thái, avatar, bong bóng chat,
thanh thông báo, màn rỗng, đầu trang, công tắc bật/tắt và hàng cài đặt (spec D2), nhóm chọn một
(spec D6) và `safe_tooltip`.

Chỉ có những gì các màn dùng tới. Không có `Card` (đã có `QFrame[surface="card"]` của `ui/theme.py`).

**Quy ước chung**
- Chữ của widget là `Qt.PlainText`; tooltip dựng từ chữ của bên gọi luôn qua `safe_tooltip`. Chữ của
  máy khác do bên gọi đưa qua `texts.peer_label` / `texts.file_label` trước; widget không lọc thêm
  nhưng không bao giờ hiện dạng rich text.
- Chữ đặt sẵn trong widget đi qua `QCoreApplication.translate("Widgets", …)` gọi lúc dùng (viết
  nguyên lời gọi để `pyside6-lupdate` trích được). Mỗi widget có `retranslate()`, gọi khi nhận
  `LanguageChange`.
- **Màu**: nền bong bóng, avatar, chấm, `InfoBar`, vòng tròn của `EmptyState` và đường kẻ của
  `PageHeader` tự vẽ bằng `QPainter`, màu lấy từ `theme.tokens_for(self)` lúc vẽ (không thêm luật
  stylesheet cho `QFrame`/`QWidget` chung, và `bubble_path`/`paint_avatar` dùng lại được trong
  delegate). Viền, chữ, tiêu điểm của `IconButton` dùng luật `QToolButton[iconButton="true"]` của
  `theme.stylesheet`; nền hover/ấn tự vẽ để chuyển dần (dưới). Màu chữ dùng `QLabel[tone=…]` có sẵn.
  Không widget nào gọi `setPalette`.
- **Chuyển động** (spec G2, `ui/motion.py`): `IconButton` đổi nền hover/ấn, `ToggleSwitch` trượt núm,
  `SegmentedControl` trượt khối chọn; chấm của `DeviceAvatar` và `StatusBadge` chuyển màu mờ dần (spec
  G5). Tắt hiệu ứng hay chế độ tức thì: nhảy thẳng. Trạng thái
  (`isChecked()`, chữ “Bật/Tắt”, `value()`, tiêu điểm, trạng thái trợ năng) đổi **ngay**, chỉ phần vẽ
  chạy theo; rảnh thì không hoạt ảnh nào chạy (mvp N3).
- **Đổi sáng/tối**: widget tự vẽ bắt `PaletteChange` và `ApplicationPaletteChange`, tô lại icon (dựng
  lại pixmap qua `ui/icons.py`) và gọi `update()`.

**Token theo thành phần**
- `IconButton`: icon 20 px màu `text`, chế độ Disabled `text_muted`; cỡ `ICON_BUTTON_SIZE` (vùng chạm
  ≥ 24 px, WCAG 2.5.8). `accessibleName` = nhãn, tooltip = nhãn (kèm phím tắt nếu có). Hover/ấn: như
  `buttons.AnimatedButton` (`buttons.ButtonLevels`, `hover_level()` / `press_level()`),
  `QStyleOptionToolButton` bỏ cờ hover/ấn, nền vẽ **trước** `CC_ToolButton`: chữ nhật bo
  `RADIUS_CONTROL` phủ cả nút, màu `blend(selected trong suốt, selected, h)` rồi `blend(…, border, p)`
  (đích bằng màu `:hover` / `:pressed` của QSS). Không có trạng thái đang chạy, không lún. Áp cho mọi
  `IconButton`.
- `StatusBadge`: chấm 8 px + chữ CAPTION đậm. SUCCESS chấm `success`, chữ `tone=success`; BUSY chấm
  `accent_strong`, chữ `tone=accent` (`accent_text`); WARNING `warning` / `tone=warning`; MUTED vòng
  rỗng 2 px `offline`, chữ `tone=muted`. Trạng thái luôn có chữ (mvp N12); chữ rỗng thì ẩn badge.
  Đổi tone khi badge đang hiện: chữ và tên trợ năng đổi ngay, chấm mờ chéo từ kiểu cũ sang kiểu mới
  (`motion.FAST`, `dot_progress()` ∈ [0, 1]); xong thì vẽ đúng đường tĩnh.
- `paint_avatar` / `DeviceAvatar`: hình tròn nền `surface_raised`, viền 1 px `border`, icon
  `text_muted`; máy này (`emphasis`) nền `accent`, icon `accent_text`. Ngoại tuyến thì icon mờ
  (opacity 0,6). Chấm góc dưới phải có vành 2 px màu nền phía sau (`ring`): ONLINE `success`, BUSY
  `accent_strong`, OFFLINE vòng 2 px `offline` ruột màu `ring`, NONE không chấm. Chấm lệch ra ngoài
  hình tròn 1 px khi `rect` còn chỗ; `DeviceAvatar` cỡ đúng bằng đường kính nên chấm nằm sát góc.
  Chấm không mang thông tin một mình: bên gọi đưa trạng thái vào tên của hàng/khối.
  **Chuyển màu chấm** (spec G5 mục 3.3): `paint_avatar(..., previous, progress)` nội suy mô hình
  `_dot_style` (màu ruột, độ “rỗng” của OFFLINE, alpha cả chấm kể cả vành; NONE mượn màu bên kia với
  alpha 0) và độ mờ icon; `previous` None, trùng `presence` hay `progress ≥ 1` thì vẽ đúng hình tĩnh
  (trùng từng điểm ảnh). Chỉ đổi màu, không đổi hình hay cỡ. `DeviceAvatar.set_presence(p, animate=True)`
  chạy `motion.FAST` khi giá trị đổi và avatar đang hiện (đổi tiếp giữa chừng: chạy lại từ đích cũ,
  có thể giật nhẹ ≤ 120 ms, chấp nhận), còn lại nhảy thẳng; `presence_progress()`. Callback đưa vào
  `motion` chỉ giữ tham chiếu yếu tới widget.
- `ChatBubble`: nền `bubble_out` (gửi đi, không viền) / `bubble_in` (nhận vào, viền 1 px `border`);
  ATTENTION viền 2 px `accent_strong`, DANGER viền 2 px `danger`. Hình theo `bubble_path`: bo
  `RADIUS_BUBBLE`, góc dưới phía người gửi bo `RADIUS_BUBBLE_TAIL`. `BubbleRow` đặt bong bóng sát
  phải (OUT) hay sát trái (IN), rộng tối đa `BUBBLE_MAX_RATIO` bề ngang hàng. Móc vẽ chuyển động
  `motion_paint()` (mặc định `(None, 0.0)`, lớp con ở `ui.chat_bubbles` ghi đè): màu phủ lên nền
  (viền vẽ lại trên), và độ dày thêm của viền nhấn vẽ thành vòng thứ hai thụt vào trong.
- `InfoBar`: nền/viền và icon theo mức: INFO `accent_subtle`/`accent_strong`, SUCCESS
  `success_bg`/`success`, WARNING `warning_bg`/`warning`, ERROR `danger_bg`/`danger`; chữ `text`;
  bo `RADIUS_CARD`. Nút hành động nằm ở hàng dưới, thẳng cột chữ (luật PR-1). `accessibleName` bắt
  đầu bằng tên mức (“Lỗi: …”), nên mức không chỉ nói bằng màu; `show_message` gửi sự kiện trợ năng
  Alert (cố gắng tốt nhất: có HĐH không chuyển tới trình đọc màn hình). Chữ là `texts.Text`: đổi
  ngôn ngữ thì câu dựng bằng hàm được dịch lại, câu là chuỗi dịch sẵn bị ẩn (trừ khi bên gọi đã hiện
  lại nó bằng ngôn ngữ mới trước khi thanh nhận `LanguageChange`). Mở/đóng theo chiều cao
  (`motion.HeightAnimator`, `NORMAL`; cha không hiện hay thanh là cửa sổ cấp cao thì tức thì; spec G3).
  Trạng thái logic là `is_open`, đổi ngay khi gọi (bên ngoài đọc `is_open`, không đọc `isHidden()`:
  lúc đang thu về thanh vẫn hiện). Đóng (`clear()`, nút ✕) mà tiêu điểm đang trong thanh thì tiêu điểm
  sang widget kế tiếp trong chuỗi Tab của cùng cửa sổ (hiện, bật, nhận Tab), không có thì bỏ tiêu
  điểm; rồi mới thu về; ✕ phát `dismissed` sau cùng.
- `EmptyState`: vòng tròn 56 px nền `accent_subtle`, icon `accent_text`; icon có cảnh
  (`illustrations.scene_for_icon`) thì thay vòng tròn bằng `illustration` (`illustrations.Illustration`,
  trang trí, cùng cột, cùng khoảng cách tới tiêu đề); tiêu đề SECTION (font điểm nhấn), mô tả
  `tone=muted`, nút chính (`buttons.AnimatedButton`, `primary=True`). Khối rộng tối đa 360 px, căn giữa.
- `PageHeader`: nút “Quay lại” (có chữ, tooltip kèm phím BACK của `ui.shortcuts`), tiêu đề SECTION,
  đường kẻ dưới 1 px `border`. Phím Esc do cửa sổ chính bắt (`main_window`), không nằm ở đây.
- `ToggleSwitch` (spec D2 mục 1.5): `QAbstractButton` checkable, `StrongFocus`, rãnh 40×20 tự vẽ và chữ
  “Bật”/“Tắt” bên phải (trạng thái không chỉ nói bằng màu hay vị trí núm, mvp N12). Tắt: nền
  `surface_raised`, viền 1 px `border_strong`, núm `text_muted`. Bật: nền `accent_strong`, núm
  `surface_raised`. Tiêu điểm: vòng 2 px `theme.focus_ring_color` quanh rãnh. Tắt hẳn (disabled): rãnh
  và núm mờ 0,5, chữ màu chữ bị tắt của bảng màu. Space (của `QAbstractButton`) và Enter đảo trạng
  thái. Núm trượt (`knob_position()` ∈ [0, 1], `motion.NORMAL`): `checkStateSet` (gọi trong mọi
  `setChecked`, kể cả khi đang chặn signal) chạy hoạt ảnh khi công tắc đang hiện và không chặn signal,
  còn lại nhảy thẳng; giữa chừng nền rãnh `blend(surface_raised, accent_strong, k)`, viền
  `border_strong` mờ dần theo `1 − k`, núm `blend(text_muted, surface_raised, k)`; hai đầu đúng hình
  tĩnh. Chữ “Bật/Tắt” và trạng thái đổi ngay; vòng tiêu điểm tĩnh. `border_strong` (viền khi tắt)
  và `accent_strong` (nền khi bật) ≥ 3:1 với `surface` ở cả hai chế độ (`TOGGLE_CONTRAST_PAIRS`). Tên
  và mô tả trợ năng do bên gọi đặt (nhãn và gợi ý của hàng); trạng thái bật/tắt tới trình đọc màn hình
  qua trạng thái “checked” của nút.
- `SettingsRow` (spec D2; dùng ở `ui/settings.py` và `ui/appearance.py`): `QFrame[surface="raised"]`,
  nhãn đậm vừa, gợi ý `tone=muted` xuống dòng, các dòng phụ (`add_line`), control bên phải; hàng hẹp
  tới mức cột chữ còn dưới `LABEL_MIN_WIDTH` thì control xuống hàng dưới (cửa sổ nhỏ nhất không cuộn
  ngang). Control mang tên trợ năng = nhãn hàng và mô tả = gợi ý, trừ khi control có property
  `keepAccessible` (bên gọi tự đặt tên cho từng phần).
- `RovingGroup` (spec D6, mvp N12): nhóm nút chọn một kiểu nút radio. Tab chỉ dừng ở nút đang chọn
  (nút khác `ClickFocus`; chưa nút nào chọn thì nút đầu nhận Tab); mũi tên trái/phải (lên/xuống) đi
  tới nút kế và chọn luôn (`click()`, nên bên dùng nhận `clicked` như khi bấm chuột), Home/End tới
  đầu/cuối, vòng quanh hai đầu; bố cục phải-sang-trái thì đảo trái/phải.
- `SegmentedControl` (spec D6 mục 2.5): khung `QFrame[segmented="true"]` (nền `surface_raised`, viền
  1 px `border_strong`, bo `RADIUS_CONTROL`) chứa các `QRadioButton` mang `segment="first|middle|last"`
  trong một `RovingGroup`. Luật `theme.stylesheet`: nút chưa chọn nền trong suốt (lộ `surface_raised`),
  chữ `text`, hover nền `selected`; đã chọn chữ `accent_text` trên **khối chọn** do khung vẽ trong
  `paintEvent` (nút con vẽ đè): nền `accent`, viền 2 px `accent_strong`, bo `RADIUS_CONTROL − 2`; khung
  tắt thì nền `selected`, viền `border`. Rect đích là `geometry()` của nút đang chọn, đọc lúc vẽ
  (`indicator_rect()`). Đổi lựa chọn khi đang hiện: khối trượt từ rect đang vẽ tới đích
  (`motion.NORMAL`), nút mới chọn mang `SEGMENT_SLIDING_PROPERTY` (chữ `text`) tới khi trượt xong, bị
  thay, ẩn hay nhảy thẳng; chưa hiện hay tắt hiệu ứng: nhảy thẳng. `value()`, `checked_button()`, tiêu
  điểm và `value_selected` không chờ khối;
  tiêu điểm viền `focus_ring_color` và chữ gạch chân (như nút chính: ở chế độ tối viền tiêu điểm chỉ
  khác viền nhấn ≈ 1,7:1); ô tròn của radio rộng 0. Viền 2 px luôn có (trong suốt khi thường) nên nút
  không đổi cỡ. Lựa chọn đọc được bằng nền và chữ, không chỉ bằng màu viền; trình đọc màn hình đọc vai
  trò radio và trạng thái “checked”. `value_selected` chỉ phát khi người dùng chọn giá trị khác giá
  trị đang có; `set_value` không phát. Cặp tương phản phải đạt với mọi màu nhấn × sáng/tối:
  `SEGMENT_CONTRAST_PAIRS`.
"""

from __future__ import annotations

import contextlib
import html
import weakref
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Any

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QAccessible,
    QAccessibleEvent,
    QColor,
    QFont,
    QFontMetrics,
    QHideEvent,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QStyle,
    QStyleOptionToolButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from teleport.ui import i18n, icons, motion, shortcuts, texts, theme
from teleport.ui.buttons import AnimatedButton, ButtonLevels, strip_interaction_state
from teleport.ui.illustrations import Illustration, scene_for_icon
from teleport.ui.texts import Text
from teleport.ui.theme import Tokens

ICON_BUTTON_SIZE = 32  # px, cạnh của nút chỉ có icon (vùng chạm ≥ 24, WCAG 2.5.8)
_ICON_SIZE = 20  # px, icon của nút và của InfoBar
_BADGE_DOT = 8  # px, chấm của StatusBadge
_EMPTY_BADGE = 56  # px, vòng tròn icon của EmptyState
_EMPTY_MAX_WIDTH = 360  # px, khối nội dung của EmptyState
BUBBLE_MAX_RATIO = 0.62
_OFFLINE_ICON_OPACITY = 0.6
_TEXT_FLAGS = Qt.TextInteractionFlag.TextSelectableByMouse  # như nhãn của `main_window`
_PALETTE_EVENTS = (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange)


def safe_tooltip(text: str) -> str:
    """Tooltip hiện đúng nguyên chữ `text`: ép rich text, thoát `<` `>` `&`, giữ xuống dòng.

    Tooltip của Qt tự đoán rich text, nên mọi tooltip chứa chữ của máy khác phải qua hàm này.
    """
    return "<qt>" + html.escape(text).replace("\n", "<br>") + "</qt>"


def _plain_label(parent: QWidget | None = None) -> QLabel:
    label = QLabel(parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _demibold(font: QFont) -> QFont:
    result = QFont(font)
    result.setWeight(QFont.Weight.DemiBold)
    return result


def _repolish(widget: QWidget) -> None:
    """Áp lại stylesheet sau khi đổi dynamic property của widget đã polish."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _set_shown(widget: QWidget, shown: bool) -> None:
    """Ẩn/hiện `widget` theo nội dung mà không bao giờ biến widget chưa có cha thành cửa sổ riêng.

    Có cha (hoặc đã hiện): `setVisible(shown)`. Chưa có cha và chưa hiện: hiện thì chỉ bỏ cờ “ẩn tường
    minh” (không gọi `show`), nên widget hiện theo cha khi được đặt vào layout của một widget sắp hiện,
    hoặc khi bên gọi tự `show()`; ẩn thì `setVisible(False)` (không mở cửa sổ nào).
    """
    if shown and widget.parentWidget() is None and not widget.isVisible():
        widget.setAttribute(Qt.WidgetAttribute.WA_WState_ExplicitShowHide, False)
        return
    widget.setVisible(shown)


def _weak(method: Callable[..., Any]) -> Callable[..., None]:
    """Bọc phương thức của một QObject bằng tham chiếu yếu (xem sổ đăng ký ở `ui/motion.py`)."""
    ref = weakref.WeakMethod(method)

    def call(*args: Any) -> None:
        bound = ref()
        if bound is not None and isValid(getattr(bound, "__self__", None)):
            bound(*args)

    return call


def _unit(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(0.0, min(1.0, number))


def _check_label(label: str) -> None:
    if not label.strip():
        raise ValueError("IconButton cần nhãn không rỗng")


# ---- IconButton --------------------------------------------------------------------------------


class IconButton(QToolButton):
    """Nút icon có tên cho trình đọc màn hình và tooltip bắt buộc (xem docstring module)."""

    def __init__(
        self,
        icon_name: str,
        label: str,
        parent: QWidget | None = None,
        *,
        shortcut: QKeySequence | None = None,
        show_text: bool = False,
    ) -> None:
        # Kiểm trước `super().__init__`: ném lỗi sau đó thì nút C++ nằm lại trong `parent` như widget mồ côi.
        icons.file_name(icon_name, _ICON_SIZE)  # tên lạ → KeyError ngay khi dựng
        _check_label(label)
        super().__init__(parent)
        self._icon_name = icon_name
        self._label = ""
        self._shortcut = QKeySequence(shortcut) if shortcut is not None else None
        self.setProperty(theme.ICON_BUTTON_PROPERTY, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAutoRaise(True)
        self.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        if show_text:
            self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.setFixedHeight(ICON_BUTTON_SIZE)
        else:
            self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            self.setFixedSize(ICON_BUTTON_SIZE, ICON_BUTTON_SIZE)
        self._show_text = show_text
        self._levels = ButtonLevels(self)
        self.set_label(label)
        self._refresh_icon()

    def hover_level(self) -> float:
        return self._levels.hover

    def press_level(self) -> float:
        return self._levels.press

    @property
    def icon_name(self) -> str:
        return self._icon_name

    @property
    def label(self) -> str:
        return self._label

    def set_label(self, label: str) -> None:
        """Đổi nhãn (tên cho trình đọc màn hình, tooltip, chữ khi `show_text`); rỗng → ValueError."""
        _check_label(label)
        self._label = label
        self.setAccessibleName(label)
        if self._show_text:
            self.setText(label)
        self._update_tooltip()

    def set_icon_name(self, name: str) -> None:
        """Đổi icon (ví dụ ⏸ ↔ ▶); tên lạ → KeyError."""
        icons.file_name(name, _ICON_SIZE)
        self._icon_name = name
        self._refresh_icon()

    def retranslate(self) -> None:
        """Nhãn do bên gọi dịch (`set_label`); ở đây chỉ dựng lại tooltip (tên phím có thể đổi)."""
        self._update_tooltip()

    def _update_tooltip(self) -> None:
        text = self._label
        if self._shortcut is not None and not self._shortcut.isEmpty():
            text = f"{text} ({self._shortcut.toString(QKeySequence.SequenceFormat.NativeText)})"
        self.setToolTip(safe_tooltip(text))

    def _refresh_icon(self) -> None:
        t = theme.tokens_for(self)
        self.setIcon(icons.icon(self._icon_name, _ICON_SIZE, t.text, t.text_muted))

    def event(self, event: QEvent) -> bool:
        levels = self.__dict__.get("_levels")  # None khi đang dựng hay đang hủy
        if levels is not None:
            levels.on_event(event)
        return super().event(event)

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._levels.reset()

    def paintEvent(self, event: QPaintEvent) -> None:
        opt = QStyleOptionToolButton()
        self.initStyleOption(opt)
        opt.state = strip_interaction_state(opt.state)
        painter = QPainter(self)
        try:
            h, p = self._levels.hover, self._levels.press
            if (h > 0.0 or p > 0.0) and self.isEnabled():
                t = theme.tokens_for(self)
                clear = QColor(t.selected)
                clear.setAlpha(0)
                color = theme.blend(theme.blend(clear, t.selected, h), t.border, p)
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawRoundedRect(QRectF(self.rect()), theme.RADIUS_CONTROL, theme.RADIUS_CONTROL)
                painter.restore()
            self.style().drawComplexControl(QStyle.ComplexControl.CC_ToolButton, opt, painter, self)
        finally:
            painter.end()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if "_show_text" not in self.__dict__:  # đang dựng, hay đang hủy (GC đã dọn thuộc tính Python)
            return
        if event.type() in _PALETTE_EVENTS:
            self._refresh_icon()
            self.update()
        elif event.type() == QEvent.Type.LanguageChange:
            self.retranslate()


# ---- StatusBadge -------------------------------------------------------------------------------


class BadgeTone(StrEnum):
    SUCCESS = "success"
    BUSY = "busy"
    WARNING = "warning"
    MUTED = "muted"


# tone của badge → `TONE_PROPERTY` của nhãn
_BADGE_LABEL_TONE = {
    BadgeTone.SUCCESS: "success",
    BadgeTone.BUSY: "accent",
    BadgeTone.WARNING: "warning",
    BadgeTone.MUTED: "muted",
}


class _BadgeDot(QWidget):
    """Chấm 8 px của `StatusBadge`."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.tone = BadgeTone.MUTED
        self.previous: BadgeTone | None = None  # kiểu cũ khi đang mờ chéo
        self.progress = 1.0
        self.setFixedSize(_BADGE_DOT, _BADGE_DOT)

    def paintEvent(self, event: QPaintEvent) -> None:
        t = theme.tokens_for(self)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            previous, m = self.previous, self.progress
            if previous is None or previous is self.tone or m >= 1.0:
                self._paint_tone(painter, self.tone, t)  # đường tĩnh
            else:
                m = max(0.0, m)
                painter.setOpacity(1.0 - m)
                self._paint_tone(painter, previous, t)
                painter.setOpacity(m)
                self._paint_tone(painter, self.tone, t)
        finally:
            painter.end()

    @staticmethod
    def _paint_tone(painter: QPainter, tone: BadgeTone, t: Tokens) -> None:
        rect = QRectF(0, 0, _BADGE_DOT, _BADGE_DOT)
        if tone is BadgeTone.MUTED:
            painter.setPen(QPen(t.offline, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(rect.adjusted(1, 1, -1, -1))
        else:
            color = {
                BadgeTone.SUCCESS: t.success,
                BadgeTone.BUSY: t.accent_strong,
                BadgeTone.WARNING: t.warning,
            }[tone]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(rect)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in _PALETTE_EVENTS:
            self.update()


class StatusBadge(QWidget):
    """Trạng thái bằng chấm và chữ (“Sẵn sàng nhận”, “Đang truyền”…); chữ rỗng thì ẩn."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tone = BadgeTone.MUTED
        self._text = ""
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACING[0])
        self.dot = _BadgeDot(self)
        self.label = _plain_label(self)
        self.label.setFont(_demibold(theme.font_for(theme.TextStyle.CAPTION, self.label.font())))
        self.label.setProperty(theme.TONE_PROPERTY, _BADGE_LABEL_TONE[self._tone])
        layout.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.label, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)
        self.setVisible(False)

    @property
    def tone(self) -> BadgeTone:
        return self._tone

    @property
    def text(self) -> str:
        return self._text

    def dot_progress(self) -> float:
        """Tiến độ mờ chéo của chấm (1 khi đứng yên)."""
        return self.dot.progress

    def set_status(self, tone: BadgeTone, text: str) -> None:
        """Đặt trạng thái; `text` rỗng thì ẩn cả badge, có chữ thì hiện lại.

        Badge chưa có cha thì không tự mở thành cửa sổ riêng (`_set_shown`): đặt vào layout trước rồi
        mới gọi, hoặc tự `show()`. Tone đổi khi badge đang hiện: chấm mờ chéo (`motion.FAST`).
        """
        new_tone = BadgeTone(tone)
        old_tone = self._tone
        visible = self.isVisible()  # trước `_set_shown`
        self._tone = new_tone
        self._text = text
        self.dot.tone = new_tone
        if new_tone is not old_tone:
            if visible:
                self.dot.previous = old_tone
                self.dot.progress = 0.0
                token = motion.FAST
            else:
                token = motion.INSTANT
            motion.animate_value(
                self,
                self.dot.progress,
                1.0,
                token,
                _weak(self._set_dot_progress),
                key="tone",
                on_finished=_weak(self._end_dot_fade),
            )
        self.dot.update()
        self.label.setText(text)
        self.label.setProperty(theme.TONE_PROPERTY, _BADGE_LABEL_TONE[self._tone])
        _repolish(self.label)
        self.setAccessibleName(text)
        _set_shown(self, bool(text))

    def retranslate(self) -> None:
        """Chữ do bên gọi dịch (`set_status`); không có chữ đặt sẵn."""

    def _set_dot_progress(self, value: Any) -> None:
        self.dot.progress = _unit(value)
        self.dot.update()

    def _end_dot_fade(self) -> None:
        self.dot.previous = None
        self.dot.progress = 1.0
        self.dot.update()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()


# ---- Avatar ------------------------------------------------------------------------------------


class Presence(StrEnum):
    ONLINE = "online"
    BUSY = "busy"
    OFFLINE = "offline"
    NONE = "none"


class AvatarKind(StrEnum):
    """Loại máy; giá trị trùng tên icon trong `icons.NAMES`."""

    DESKTOP = "desktop"
    LAPTOP = "laptop"


_RING_TOKENS = frozenset({"surface", "sidebar", "surface_raised", "selected"})


def _dot_style(presence: Presence, tokens: Tokens) -> tuple[QColor, float, float]:
    """(màu ruột, độ rỗng, alpha) của chấm. NONE: alpha 0, màu ruột do bên chuyển tới/từ quyết định."""
    if presence is Presence.ONLINE:
        return QColor(tokens.success), 0.0, 1.0
    if presence is Presence.BUSY:
        return QColor(tokens.accent_strong), 0.0, 1.0
    if presence is Presence.OFFLINE:
        return QColor(tokens.offline), 1.0, 1.0
    return QColor(tokens.offline), 0.0, 0.0


def _icon_opacity(presence: Presence) -> float:
    return _OFFLINE_ICON_OPACITY if presence is Presence.OFFLINE else 1.0


def paint_avatar(
    painter: QPainter,
    rect: QRectF,
    *,
    kind: AvatarKind,
    presence: Presence,
    emphasis: bool,
    ring: QColor,
    tokens: Tokens,
    dpr: float,
    previous: Presence | None = None,
    progress: float = 1.0,
) -> None:
    """Vẽ avatar (hình tròn, icon loại máy, chấm trạng thái) vào `rect`; dùng chung cho delegate.

    Hình tròn đường kính `min(rect.width, rect.height)`, căn giữa `rect`. `ring` là màu nền nằm sau
    avatar (vành của chấm). `previous` / `progress`: đang chuyển màu chấm từ `previous` (xem docstring
    module); mặc định vẽ hình tĩnh. Lưu và khôi phục trạng thái của `painter`.
    """
    d = min(rect.width(), rect.height())
    if d <= 0:
        return
    if previous is not None and Presence(previous) is not presence and progress < 1.0:
        _paint_avatar_fading(
            painter,
            rect,
            d,
            kind,
            Presence(previous),
            presence,
            max(0.0, progress),
            emphasis,
            ring,
            tokens,
            dpr,
        )
        return
    circle = QRectF(rect.center().x() - d / 2, rect.center().y() - d / 2, d, d)
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if emphasis:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(tokens.accent)
            painter.drawEllipse(circle)
            icon_color = tokens.accent_text
        else:
            painter.setPen(QPen(tokens.border, 1))
            painter.setBrush(tokens.surface_raised)
            painter.drawEllipse(circle.adjusted(0.5, 0.5, -0.5, -0.5))
            icon_color = tokens.text_muted

        size = 24 if d >= 44 else 20
        pm = icons.pixmap(str(AvatarKind(kind)), size, icon_color, dpr)
        if not pm.isNull():
            painter.save()
            if presence is Presence.OFFLINE:
                painter.setOpacity(_OFFLINE_ICON_OPACITY)
            center = circle.center()
            painter.drawPixmap(QPointF(center.x() - size / 2, center.y() - size / 2), pm)
            painter.restore()

        if presence is not Presence.NONE:
            dot = max(9, round(d * 0.29))
            # Lệch ra ngoài hình tròn 1 px như mockup, nhưng không ra khỏi `rect` (sẽ bị cắt).
            right = min(circle.right() + 1, rect.right())
            bottom = min(circle.bottom() + 1, rect.bottom())
            outer = QRectF(right - dot, bottom - dot, dot, dot)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ring)
            painter.drawEllipse(outer)
            inner = outer.adjusted(2, 2, -2, -2)
            if presence is Presence.OFFLINE:
                painter.setBrush(tokens.offline)
                painter.drawEllipse(inner)
                painter.setBrush(ring)
                painter.drawEllipse(inner.adjusted(2, 2, -2, -2))
            else:
                painter.setBrush(tokens.success if presence is Presence.ONLINE else tokens.accent_strong)
                painter.drawEllipse(inner)
    finally:
        painter.restore()


def _paint_avatar_fading(
    painter: QPainter,
    rect: QRectF,
    d: float,
    kind: AvatarKind,
    previous: Presence,
    presence: Presence,
    m: float,
    emphasis: bool,
    ring: QColor,
    tokens: Tokens,
    dpr: float,
) -> None:
    """`paint_avatar` giữa lúc chuyển chấm `previous` → `presence` ở mức `m` ∈ [0, 1)."""
    circle = QRectF(rect.center().x() - d / 2, rect.center().y() - d / 2, d, d)
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if emphasis:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(tokens.accent)
            painter.drawEllipse(circle)
            icon_color = tokens.accent_text
        else:
            painter.setPen(QPen(tokens.border, 1))
            painter.setBrush(tokens.surface_raised)
            painter.drawEllipse(circle.adjusted(0.5, 0.5, -0.5, -0.5))
            icon_color = tokens.text_muted

        size = 24 if d >= 44 else 20
        pm = icons.pixmap(str(AvatarKind(kind)), size, icon_color, dpr)
        if not pm.isNull():
            painter.save()
            start, end = _icon_opacity(previous), _icon_opacity(presence)
            icon_opacity = start + (end - start) * m
            if icon_opacity < 1.0:
                painter.setOpacity(icon_opacity)
            center = circle.center()
            painter.drawPixmap(QPointF(center.x() - size / 2, center.y() - size / 2), pm)
            painter.restore()

        fill_prev, hole_prev, alpha_prev = _dot_style(previous, tokens)
        fill_cur, hole_cur, alpha_cur = _dot_style(presence, tokens)
        if previous is Presence.NONE:
            fill_prev = fill_cur
        if presence is Presence.NONE:
            fill_cur = fill_prev
        alpha = alpha_prev + (alpha_cur - alpha_prev) * m
        if alpha <= 0.0:
            return
        hole = hole_prev + (hole_cur - hole_prev) * m
        dot = max(9, round(d * 0.29))
        right = min(circle.right() + 1, rect.right())
        bottom = min(circle.bottom() + 1, rect.bottom())
        outer = QRectF(right - dot, bottom - dot, dot, dot)
        painter.setOpacity(painter.opacity() * alpha)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ring)
        painter.drawEllipse(outer)
        inner = outer.adjusted(2, 2, -2, -2)
        painter.setBrush(theme.blend(fill_prev, fill_cur, m))
        painter.drawEllipse(inner)
        if hole > 0.0:
            hole_color = QColor(ring)
            hole_color.setAlphaF(ring.alphaF() * hole)
            painter.setBrush(hole_color)
            painter.drawEllipse(inner.adjusted(2, 2, -2, -2))
    finally:
        painter.restore()


class DeviceAvatar(QWidget):
    """Avatar tròn cỡ cố định; chỉ để trang trí trừ khi bên gọi đặt tên (`set_label`)."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        diameter: int = 38,
        kind: AvatarKind = AvatarKind.DESKTOP,
        presence: Presence = Presence.NONE,
        emphasis: bool = False,
        ring: str = "surface",
    ) -> None:
        if diameter < 24:
            raise ValueError(f"đường kính avatar phải ≥ 24: {diameter}")
        self._check_ring(ring)
        super().__init__(parent)
        self._diameter = diameter
        self._kind = AvatarKind(kind)
        self._presence = Presence(presence)
        self._previous: Presence | None = None  # chấm cũ khi đang chuyển màu
        self._progress = 1.0
        self._emphasis = emphasis
        self._ring = ring
        self.setFixedSize(diameter, diameter)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    @staticmethod
    def _check_ring(ring: str) -> None:
        if ring not in _RING_TOKENS:
            raise ValueError(f"ring phải là một trong {sorted(_RING_TOKENS)}: {ring!r}")

    @property
    def diameter(self) -> int:
        return self._diameter

    @property
    def kind(self) -> AvatarKind:
        return self._kind

    @property
    def presence(self) -> Presence:
        return self._presence

    @property
    def emphasis(self) -> bool:
        return self._emphasis

    @property
    def ring(self) -> str:
        return self._ring

    def set_kind(self, kind: AvatarKind) -> None:
        self._kind = AvatarKind(kind)
        self.update()

    def set_presence(self, presence: Presence, *, animate: bool = True) -> None:
        """Đổi chấm; khác giá trị cũ, `animate` và avatar đang hiện thì chuyển màu (`motion.FAST`)."""
        new = Presence(presence)
        old = self._presence
        if new is old:
            return
        self._presence = new
        if animate and self.isVisible():
            self._previous = old
            self._progress = 0.0
            token = motion.FAST
        else:
            token = motion.INSTANT
        motion.animate_value(
            self,
            self._progress,
            1.0,
            token,
            _weak(self._set_progress),
            key="presence",
            on_finished=_weak(self._end_fade),
        )
        self.update()

    def presence_progress(self) -> float:
        """Tiến độ chuyển màu chấm (1 khi đứng yên)."""
        return self._progress

    def _set_progress(self, value: Any) -> None:
        self._progress = _unit(value)
        self.update()

    def _end_fade(self) -> None:
        self._previous = None
        self._progress = 1.0
        self.update()

    def set_emphasis(self, emphasis: bool) -> None:
        self._emphasis = emphasis
        self.update()

    def set_ring(self, ring: str) -> None:
        self._check_ring(ring)
        self._ring = ring
        self.update()

    def set_label(self, text: str) -> None:
        """Tên cho trình đọc màn hình và tooltip (chữ thuần)."""
        self.setAccessibleName(text)
        self.setToolTip(safe_tooltip(text) if text else "")

    def retranslate(self) -> None:
        """Không có chữ đặt sẵn."""

    def paintEvent(self, event: QPaintEvent) -> None:
        t = theme.tokens_for(self)
        painter = QPainter(self)
        try:
            paint_avatar(
                painter,
                QRectF(self.rect()),
                kind=self._kind,
                presence=self._presence,
                emphasis=self._emphasis,
                ring=getattr(t, self._ring),
                tokens=t,
                dpr=self.devicePixelRatioF(),
                previous=self._previous,
                progress=self._progress,
            )
        finally:
            painter.end()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in _PALETTE_EVENTS:
            self.update()
        elif event.type() == QEvent.Type.LanguageChange:
            self.retranslate()


# ---- Bong bóng chat ----------------------------------------------------------------------------


class Direction(StrEnum):
    OUT = "out"
    IN = "in"


class BubbleEmphasis(StrEnum):
    NONE = "none"
    ATTENTION = "attention"  # lời mời cần trả lời
    DANGER = "danger"  # lỗi / đã hủy


def bubble_path(rect: QRectF, direction: Direction) -> QPainterPath:
    """Hình bong bóng: chữ nhật bo `RADIUS_BUBBLE`, góc dưới phía người gửi bo `RADIUS_BUBBLE_TAIL`
    (OUT: góc dưới phải, IN: góc dưới trái)."""
    big, tail = float(theme.RADIUS_BUBBLE), float(theme.RADIUS_BUBBLE_TAIL)
    limit = min(rect.width(), rect.height()) / 2
    big, tail = min(big, limit), min(tail, limit)
    top_left = top_right = big
    bottom_right = tail if direction is Direction.OUT else big
    bottom_left = tail if direction is Direction.IN else big
    x, y, r, b = rect.left(), rect.top(), rect.right(), rect.bottom()
    path = QPainterPath()
    path.moveTo(x + top_left, y)
    path.lineTo(r - top_right, y)
    path.arcTo(QRectF(r - 2 * top_right, y, 2 * top_right, 2 * top_right), 90, -90)
    path.lineTo(r, b - bottom_right)
    path.arcTo(QRectF(r - 2 * bottom_right, b - 2 * bottom_right, 2 * bottom_right, 2 * bottom_right), 0, -90)
    path.lineTo(x + bottom_left, b)
    path.arcTo(QRectF(x, b - 2 * bottom_left, 2 * bottom_left, 2 * bottom_left), 270, -90)
    path.lineTo(x, y + top_left)
    path.arcTo(QRectF(x, y, 2 * top_left, 2 * top_left), 180, -90)
    path.closeSubpath()
    return path


class ChatBubble(QFrame):
    """Bong bóng của luồng chat; nội dung đặt qua `body()` (xem docstring module)."""

    def __init__(
        self,
        direction: Direction,
        parent: QWidget | None = None,
        *,
        emphasis: BubbleEmphasis = BubbleEmphasis.NONE,
    ) -> None:
        super().__init__(parent)
        self._direction = Direction(direction)
        self._emphasis = BubbleEmphasis(emphasis)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._body = QVBoxLayout(self)
        self._body.setContentsMargins(theme.SPACING[2], theme.SPACING[1], theme.SPACING[2], theme.SPACING[1])
        self._body.setSpacing(theme.SPACING[0])
        self.retranslate()

    @property
    def direction(self) -> Direction:
        return self._direction

    @property
    def emphasis(self) -> BubbleEmphasis:
        return self._emphasis

    def set_emphasis(self, emphasis: BubbleEmphasis) -> None:
        self._emphasis = BubbleEmphasis(emphasis)
        self.update()

    def body(self) -> QVBoxLayout:
        """Layout nội dung của bong bóng (lề 12/8/12/8, giãn dòng 4)."""
        return self._body

    def set_summary(self, text: str) -> None:
        """Câu tóm tắt cho trình đọc màn hình (ví dụ “Đang gửi phim-4k.mkv, 62%”)."""
        self.setAccessibleName(text)

    def motion_paint(self) -> tuple[QColor | None, float]:
        """Móc vẽ chuyển động, đọc lúc vẽ: (màu phủ lên nền hay None, độ dày thêm px của viền nhấn).
        Mặc định `(None, 0.0)`: vẽ y như không có chuyển động (lớp con ở `ui.chat_bubbles` ghi đè)."""
        return None, 0.0

    def retranslate(self) -> None:
        if self._direction is Direction.OUT:
            self.setAccessibleDescription(QCoreApplication.translate("Widgets", "Gửi đi"))
        else:
            self.setAccessibleDescription(QCoreApplication.translate("Widgets", "Nhận vào"))

    def paintEvent(self, event: QPaintEvent) -> None:
        t = theme.tokens_for(self)
        pen_color: QColor | None
        if self._emphasis is BubbleEmphasis.ATTENTION:
            pen_color, width = t.accent_strong, 2
        elif self._emphasis is BubbleEmphasis.DANGER:
            pen_color, width = t.danger, 2
        elif self._direction is Direction.IN:
            pen_color, width = t.border, 1
        else:
            pen_color, width = None, 0
        inset = width / 2 if width else 0.5
        path = bubble_path(QRectF(self.rect()).adjusted(inset, inset, -inset, -inset), self._direction)
        tint, extra = self.motion_paint()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(t.bubble_out if self._direction is Direction.OUT else t.bubble_in)
            painter.setPen(QPen(pen_color, width) if pen_color is not None else Qt.PenStyle.NoPen)
            painter.drawPath(path)
            if tint is not None:  # phủ màu lên nền rồi vẽ lại viền cho viền giữ nguyên màu
                painter.fillPath(path, tint)
                if pen_color is not None:
                    painter.strokePath(path, QPen(pen_color, width))
            if extra > 0 and pen_color is not None and self._emphasis is not BubbleEmphasis.NONE:
                # Phần dày thêm là vòng thứ hai thụt vào trong: nền và viền gốc không “thở”.
                ring = width + extra / 2
                inner = bubble_path(QRectF(self.rect()).adjusted(ring, ring, -ring, -ring), self._direction)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(pen_color, extra))
                painter.drawPath(inner)
        finally:
            painter.end()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in _PALETTE_EVENTS:
            self.update()
        elif event.type() == QEvent.Type.LanguageChange:
            self.retranslate()


class BubbleRow(QWidget):
    """Một hàng của luồng: bong bóng sát phải (OUT) hay sát trái (IN), rộng tối đa 62% hàng."""

    def __init__(self, bubble: ChatBubble, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bubble = bubble
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        if bubble.direction is Direction.OUT:
            layout.addStretch(1)
            layout.addWidget(bubble)
        else:
            layout.addWidget(bubble)
            layout.addStretch(1)

    @property
    def bubble(self) -> ChatBubble:
        return self._bubble

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._bubble.setMaximumWidth(max(1, round(event.size().width() * BUBBLE_MAX_RATIO)))
        super().resizeEvent(event)


# ---- InfoBar -----------------------------------------------------------------------------------


class InfoLevel(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


_INFO_ICONS = {
    InfoLevel.INFO: "info",
    InfoLevel.SUCCESS: "checkmark_circle",
    InfoLevel.WARNING: "warning",
    InfoLevel.ERROR: "error_circle",
}


def _info_colors(t: Tokens, level: InfoLevel) -> tuple[QColor, QColor]:
    """(nền, viền và icon) của mức `level`."""
    return {
        InfoLevel.INFO: (t.accent_subtle, t.accent_strong),
        InfoLevel.SUCCESS: (t.success_bg, t.success),
        InfoLevel.WARNING: (t.warning_bg, t.warning),
        InfoLevel.ERROR: (t.danger_bg, t.danger),
    }[level]


def _level_name(level: InfoLevel) -> str:
    if level is InfoLevel.SUCCESS:
        return QCoreApplication.translate("Widgets", "Thành công")
    if level is InfoLevel.WARNING:
        return QCoreApplication.translate("Widgets", "Cảnh báo")
    if level is InfoLevel.ERROR:
        return QCoreApplication.translate("Widgets", "Lỗi")
    return QCoreApplication.translate("Widgets", "Thông tin")


class InfoBar(QFrame):
    """Thanh thông báo bốn mức; ẩn cho tới `show_message` (xem docstring module)."""

    action_triggered = Signal()
    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._level = InfoLevel.INFO
        self._title = ""
        self._text = ""
        self._sources: tuple[Text, Text, Text | None] = ("", "", None)
        self._language = ""  # ngôn ngữ lúc hiện câu đang có
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(_ICON_SIZE, _ICON_SIZE)
        self.title_label = _plain_label(self)
        self.title_label.setFont(_demibold(self.title_label.font()))
        self.text_label = _plain_label(self)
        for label in (self.title_label, self.text_label):
            label.setWordWrap(True)
            label.setTextInteractionFlags(_TEXT_FLAGS)
            label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.action_button = QPushButton(self)
        self.action_button.clicked.connect(self.action_triggered)
        self.close_button = IconButton(
            "dismiss", QCoreApplication.translate("Widgets", "Đóng thông báo"), self
        )
        self.close_button.clicked.connect(self._on_close)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(2)
        text_column.addWidget(self.title_label)
        text_column.addWidget(self.text_label)
        grid = QGridLayout(self)
        grid.setContentsMargins(theme.SPACING[2], theme.SPACING[1], theme.SPACING[1], theme.SPACING[1])
        grid.setHorizontalSpacing(theme.SPACING[1] + 2)
        grid.setVerticalSpacing(theme.SPACING[1])
        grid.addWidget(self.icon_label, 0, 0, Qt.AlignmentFlag.AlignTop)
        grid.addLayout(text_column, 0, 1)
        grid.addWidget(self.close_button, 0, 2, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.action_button, 1, 1, Qt.AlignmentFlag.AlignLeft)
        grid.setColumnStretch(1, 1)

        self.action_button.setVisible(False)
        self.setVisible(False)
        self._open = False  # trạng thái logic (`is_open`), đổi ngay
        self._height = motion.HeightAnimator(self)
        self._refresh_icon()
        self.retranslate()

    @property
    def is_open(self) -> bool:
        """Thanh đang mở (đổi ngay khi gọi `show_message` / `clear`, không chờ hoạt ảnh)."""
        return self._open

    @property
    def level(self) -> InfoLevel:
        return self._level

    @property
    def title(self) -> str:
        return self._title

    @property
    def text(self) -> str:
        return self._text

    def show_message(
        self,
        level: InfoLevel,
        title: Text,
        text: Text = "",
        *,
        action: Text | None = None,
        closable: bool = True,
    ) -> None:
        """Hiện thông báo; `action` là chữ của nút ở hàng dưới (None: không có nút).

        Thanh chưa có cha thì không tự mở thành cửa sổ riêng (`_set_shown`): đặt vào layout trước rồi
        mới gọi, hoặc tự `show()`.
        """
        self._level = InfoLevel(level)
        self._sources = (title, text, action)
        self._language = i18n.current_language()
        self._apply_sources()
        self.action_button.setVisible(action is not None)
        self.close_button.setVisible(closable)
        self._refresh_icon()
        self.retranslate()
        self._open = True
        parent = self.parentWidget()
        if parent is None:
            _set_shown(self, True)
        else:
            token = motion.NORMAL if parent.isVisible() else motion.INSTANT
            if self.isHidden() and self._height.expanded:  # bị ẩn từ ngoài: mở lại hẳn
                self._height.expand(token)
            else:  # đang mở và đứng yên: chỉ đổi nội dung
                self._height.set_expanded(True, token)
        self.update()
        # Cố gắng tốt nhất: có HĐH không chuyển Alert tới trình đọc màn hình; tên mức vẫn ở accessibleName.
        with contextlib.suppress(Exception):
            QAccessible.updateAccessibility(QAccessibleEvent(self, QAccessible.Event.Alert))

    def clear(self) -> None:
        """Đóng thanh (`is_open` False ngay), không phát `dismissed`."""
        self._close()

    def retranslate(self) -> None:
        self.close_button.set_label(QCoreApplication.translate("Widgets", "Đóng thông báo"))
        self.setAccessibleName(f"{_level_name(self._level)}: {self._title}")
        self.setAccessibleDescription(self._text)

    def _apply_sources(self) -> None:
        title, text, action = self._sources
        self._title, self._text = texts.resolve(title), texts.resolve(text)
        self.title_label.setText(self._title)
        self.text_label.setText(self._text)
        self.text_label.setVisible(bool(self._text))
        self.action_button.setText(texts.resolve(action) if action is not None else "")

    def _retranslate_message(self) -> None:
        """Sau `LanguageChange`: câu dựng bằng hàm thì dịch lại; câu dịch sẵn của ngôn ngữ cũ thì ẩn."""
        language = i18n.current_language()
        if not self.is_open or self._language == language:
            return
        if all(callable(source) or not source for source in self._sources):
            self._language = language
            self._apply_sources()
            self.retranslate()
        else:
            self.clear()

    def _on_close(self) -> None:
        self._close()
        self.dismissed.emit()

    def _close(self) -> None:
        self._open = False
        self._move_focus_out()
        if self.parentWidget() is None:  # mở bằng `_set_shown`, không qua animator: đóng tức thì
            self._height.collapse(motion.INSTANT)
            return
        token = motion.INSTANT if self.isWindow() or not self.isVisible() else motion.NORMAL
        self._height.set_expanded(False, token)  # đang thu dở thì không chạy lại

    def _move_focus_out(self) -> None:
        """Tiêu điểm đang trong thanh → widget kế tiếp ngoài thanh trong chuỗi Tab của cùng cửa sổ (hiện,
        bật, nhận Tab); không có thì bỏ tiêu điểm. Thanh đang thu về vẫn hiện nên phải dời trước."""
        focus = self.window().focusWidget()
        if focus is None or not (focus is self or self.isAncestorOf(focus)):
            return
        window = self.window()
        tab = Qt.FocusPolicy.TabFocus.value
        candidate = focus.nextInFocusChain()
        seen = 0
        while candidate is not None and candidate is not focus and seen < 10_000:
            seen += 1
            if (
                candidate is not self
                and not self.isAncestorOf(candidate)
                and candidate.window() is window
                and candidate.isVisible()
                and candidate.isEnabled()
                and candidate.focusPolicy().value & tab
            ):
                candidate.setFocus(Qt.FocusReason.TabFocusReason)
                return
            candidate = candidate.nextInFocusChain()
        focus.clearFocus()

    def _refresh_icon(self) -> None:
        t = theme.tokens_for(self)
        color = _info_colors(t, self._level)[1]
        self.icon_label.setPixmap(
            icons.pixmap(_INFO_ICONS[self._level], _ICON_SIZE, color, self.devicePixelRatioF())
        )

    def paintEvent(self, event: QPaintEvent) -> None:
        background, edge = _info_colors(theme.tokens_for(self), self._level)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(edge, 1))
            painter.setBrush(background)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.drawRoundedRect(rect, theme.RADIUS_CARD, theme.RADIUS_CARD)
        finally:
            painter.end()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        # Phòng thủ: đang dựng, hay đang hủy mà GC đã dọn thuộc tính Python trong khi đối tượng C++ còn
        # chờ `deleteLater` và vẫn nhận sự kiện (từng gặp khi `HeightAnimator._widget` giữ mạnh thanh,
        # tạo vòng tham chiếu; nay animator chỉ giữ tham chiếu yếu): bỏ qua.
        if "_height" not in self.__dict__:
            return
        if event.type() in (*_PALETTE_EVENTS, QEvent.Type.DevicePixelRatioChange):
            self._refresh_icon()
            self.update()
        elif event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
            self._retranslate_message()


# ---- EmptyState --------------------------------------------------------------------------------


class _IconBadge(QWidget):
    """Vòng tròn 56 px nền `accent_subtle`, icon `accent_text` ở giữa."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.icon_name = "info"
        self.setFixedSize(_EMPTY_BADGE, _EMPTY_BADGE)

    def icon_size(self) -> int:
        return 24 if 24 in icons.SIZES[self.icon_name] else 20

    def paintEvent(self, event: QPaintEvent) -> None:
        t = theme.tokens_for(self)
        size = self.icon_size()
        pm = icons.pixmap(self.icon_name, size, t.accent_text, self.devicePixelRatioF())
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(t.accent_subtle)
            painter.drawEllipse(QRectF(0, 0, _EMPTY_BADGE, _EMPTY_BADGE))
            if not pm.isNull():
                offset = (_EMPTY_BADGE - size) / 2
                painter.drawPixmap(QPointF(offset, offset), pm)
        finally:
            painter.end()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in _PALETTE_EVENTS:
            self.update()


class EmptyState(QWidget):
    """Màn rỗng căn giữa: icon, tiêu đề, mô tả, nút chính (xem docstring module)."""

    action_triggered = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = ""
        self.content = QWidget(self)
        self.content.setObjectName("emptyStateContent")
        # Căn giữa bằng stretch hai bên, không dùng cờ căn lề: có cờ căn lề thì Qt đặt khối theo sizeHint
        # và bỏ qua heightForWidth của nhãn xuống dòng, khoảng cách bị ép và chữ bị cắt.
        self.content.setMaximumWidth(_EMPTY_MAX_WIDTH)
        self.content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.icon_badge = _IconBadge(self.content)
        self.illustration = Illustration(parent=self.content)  # trang trí, ẩn tới khi icon có cảnh
        self.illustration.setVisible(False)
        self.title_label = _plain_label(self.content)
        self.title_label.setFont(theme.font_for(theme.TextStyle.SECTION, self.title_label.font()))
        self.text_label = _plain_label(self.content)
        self.text_label.setProperty(theme.TONE_PROPERTY, "muted")
        for label in (self.title_label, self.text_label):
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            label.setTextInteractionFlags(_TEXT_FLAGS)
            label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.action_button = AnimatedButton("", self.content, primary=True)
        self.action_button.setObjectName("emptyActionButton")
        self.action_button.setProperty(theme.PRIMARY_PROPERTY, True)
        self.action_button.clicked.connect(self.action_triggered)

        column = QVBoxLayout(self.content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(self.icon_badge, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(self.illustration, 0, Qt.AlignmentFlag.AlignHCenter)  # chỉ một trong hai hiện
        column.addSpacing(theme.SPACING[3])
        column.addWidget(self.title_label)
        column.addSpacing(theme.SPACING[1])
        column.addWidget(self.text_label)
        self._button_gap = theme.SPACING[3]
        column.addSpacing(self._button_gap)
        column.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignHCenter)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        row.addWidget(self.content, 1000)  # lấy hết chỗ tới 360 px, phần thừa chia đều hai stretch
        row.addStretch(1)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)

        self.text_label.setVisible(False)
        self.action_button.setVisible(False)
        self.setFocusProxy(self.focus_target())

    def set_content(self, icon_name: str, title: str, text: str = "", *, action: str | None = None) -> None:
        """Đặt nội dung; `action` là chữ của nút chính (None: không có nút); icon lạ → KeyError.

        Icon có cảnh minh họa (`illustrations.scene_for_icon`) thì hiện minh họa thay vòng tròn icon."""
        icons.SIZES[icon_name]  # tên lạ → KeyError
        self.icon_badge.icon_name = icon_name
        self.icon_badge.update()
        scene = scene_for_icon(icon_name)
        if scene is not None:
            self.illustration.set_scene(scene)  # đặt cảnh trước khi hiện: hiện lần đầu mới chạy
        self.icon_badge.setVisible(scene is None)
        self.illustration.setVisible(scene is not None)
        self._title = title
        self.title_label.setText(title)
        self.text_label.setText(text)
        self.text_label.setVisible(bool(text))
        self.action_button.setText(action or "")
        self.action_button.setVisible(action is not None)
        self.setAccessibleName(title)
        self.setFocusProxy(self.focus_target())

    def focus_target(self) -> QWidget:
        """Nút chính nếu đang có, không thì tiêu đề."""
        return self.action_button if not self.action_button.isHidden() else self.title_label

    def retranslate(self) -> None:
        """Chữ do bên gọi dịch (`set_content`); không có chữ đặt sẵn."""

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()


# ---- PageHeader --------------------------------------------------------------------------------


class PageHeader(QWidget):
    """Đầu trang: nút “Quay lại”, tiêu đề, chỗ cho widget bên phải; đường kẻ dưới."""

    back_requested = Signal()

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.back_button = IconButton(
            "chevron_left",
            QCoreApplication.translate("Widgets", "Quay lại"),
            self,
            shortcut=shortcuts.sequence(shortcuts.Shortcut.BACK),
            show_text=True,
        )
        self.back_button.clicked.connect(self.back_requested)
        self.title_label = _plain_label(self)
        self.title_label.setFont(theme.font_for(theme.TextStyle.SECTION, self.title_label.font()))
        self.title_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # tên trang đã đọc qua tên của trang
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(
            theme.SPACING[3], theme.SPACING[2], theme.SPACING[3], theme.SPACING[2]
        )
        self._layout.setSpacing(theme.SPACING[2])
        self._layout.addWidget(self.back_button)
        self._layout.addWidget(self.title_label, 1)
        self.set_title(title)

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)
        self.title_label.setAccessibleName(title)

    def add_trailing(self, widget: QWidget) -> None:
        """Thêm `widget` vào cuối hàng, bên phải tiêu đề (ví dụ ô “Nhập IP…”)."""
        self._layout.addWidget(widget)

    def retranslate(self) -> None:
        self.back_button.set_label(QCoreApplication.translate("Widgets", "Quay lại"))

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.fillRect(0, self.height() - 1, self.width(), 1, theme.tokens_for(self).border)
        finally:
            painter.end()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in _PALETTE_EVENTS:
            self.update()
        elif event.type() == QEvent.Type.LanguageChange:
            self.retranslate()


# ---- ToggleSwitch ------------------------------------------------------------------------------

TOGGLE_TRACK = QSize(40, 20)  # px, rãnh của công tắc
_TOGGLE_KNOB_MARGIN = 3  # px, từ mép rãnh tới núm
_TOGGLE_TEXT_GAP = 8  # px, từ rãnh tới chữ “Bật”/“Tắt”
_TOGGLE_RING = 2  # px, vòng tiêu điểm (ngoài rãnh, trong vùng của widget)
_TOGGLE_DISABLED_OPACITY = 0.5
# (token, token nền) phải ≥ 3:1 (WCAG 1.4.11) ở cả hai chế độ.
TOGGLE_CONTRAST_PAIRS = (("border_strong", "surface"), ("accent_strong", "surface"))
_PLAIN_KEYS = (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier)


def toggle_state_text(checked: bool) -> str:
    """Chữ trạng thái cạnh công tắc (“Bật” / “Tắt”)."""
    if checked:
        return QCoreApplication.translate("Widgets", "Bật")
    return QCoreApplication.translate("Widgets", "Tắt")


class ToggleSwitch(QAbstractButton):
    """Công tắc bật/tắt tự vẽ có chữ trạng thái (xem docstring module)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._knob = 1.0 if self.isChecked() else 0.0
        self._knob_target = self._knob
        self.toggled.connect(self._sync_knob)
        self.retranslate()

    def knob_position(self) -> float:
        """Vị trí núm đang vẽ: 0 = tắt (trái), 1 = bật (phải)."""
        return self._knob

    def checkStateSet(self) -> None:
        # Gọi trong `setChecked` và `click()` từ code, kể cả khi đang chặn signal (`toggled` không phát).
        # Bấm bằng chuột hay Space thì Qt đi qua `QAbstractButtonPrivate::click` (đặt `blockRefresh`) và
        # bỏ qua hàm này (đã thử: PySide6 6.12), nên `toggled` cũng gọi `_sync_knob`.
        super().checkStateSet()
        self._sync_knob()

    def _sync_knob(self, *_args: object) -> None:
        """Chạy núm tới vị trí của `isChecked()`; đích không đổi thì không làm gì."""
        target = 1.0 if self.isChecked() else 0.0
        animated = self.isVisible() and not self.signalsBlocked()
        if target == self._knob_target and (animated or self._knob == target):
            return
        self._knob_target = target
        token = motion.NORMAL if animated else motion.INSTANT
        motion.animate_value(self, self._knob, target, token, self._set_knob, key="knob")
        self.update()

    def _set_knob(self, value: object) -> None:
        try:
            knob = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            knob = 1.0 if self.isChecked() else 0.0
        self._knob = max(0.0, min(1.0, knob))
        self.update()

    def state_text(self) -> str:
        """Chữ đang hiện cạnh rãnh."""
        return toggle_state_text(self.isChecked())

    def _text_width(self) -> int:
        metrics = QFontMetrics(self.font())
        return max(
            metrics.horizontalAdvance(toggle_state_text(True)),
            metrics.horizontalAdvance(toggle_state_text(False)),
        )

    def sizeHint(self) -> QSize:
        height = max(TOGGLE_TRACK.height() + 2 * _TOGGLE_RING, QFontMetrics(self.font()).height())
        width = 2 * _TOGGLE_RING + TOGGLE_TRACK.width() + _TOGGLE_TEXT_GAP + self._text_width()
        return QSize(width, height)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def hitButton(self, pos: QPoint) -> bool:
        return self.rect().contains(pos)

    def track_rect(self) -> QRectF:
        """Rãnh trong widget: sát trái (chừa vòng tiêu điểm), căn giữa theo chiều dọc."""
        top = (self.height() - TOGGLE_TRACK.height()) / 2
        return QRectF(_TOGGLE_RING, top, TOGGLE_TRACK.width(), TOGGLE_TRACK.height())

    def retranslate(self) -> None:
        """Chữ trạng thái đổi theo ngôn ngữ; tên và mô tả trợ năng do bên gọi đặt."""
        self.updateGeometry()
        self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() in _PLAIN_KEYS
            and not event.isAutoRepeat()
        ):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        t = theme.tokens_for(self)
        dark = theme.is_dark_palette(QApplication.palette())
        track = self.track_rect()
        radius = track.height() / 2
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            if self.hasFocus():
                half = _TOGGLE_RING / 2
                ring = track.adjusted(-half, -half, half, half)
                painter.setPen(QPen(theme.focus_ring_color(dark), _TOGGLE_RING))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(ring, radius + half, radius + half)
            painter.save()
            if not self.isEnabled():
                painter.setOpacity(_TOGGLE_DISABLED_OPACITY)
            k = self._knob
            if k >= 1.0:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(t.accent_strong)
                painter.drawRoundedRect(track, radius, radius)
            elif k <= 0.0:
                painter.setPen(QPen(t.border_strong, 1))
                painter.setBrush(t.surface_raised)
                painter.drawRoundedRect(track.adjusted(0.5, 0.5, -0.5, -0.5), radius - 0.5, radius - 0.5)
            else:  # đang trượt: nền chuyển dần, viền mờ dần
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.blend(t.surface_raised, t.accent_strong, k))
                painter.drawRoundedRect(track, radius, radius)
                edge = QColor(t.border_strong)
                edge.setAlphaF(edge.alphaF() * (1.0 - k))
                painter.setPen(QPen(edge, 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(track.adjusted(0.5, 0.5, -0.5, -0.5), radius - 0.5, radius - 0.5)
            knob_color = theme.blend(t.text_muted, t.surface_raised, k)
            d = track.height() - 2 * _TOGGLE_KNOB_MARGIN
            left = track.left() + _TOGGLE_KNOB_MARGIN
            x = left + (track.right() - _TOGGLE_KNOB_MARGIN - d - left) * k
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(knob_color)
            painter.drawEllipse(QRectF(x, track.top() + _TOGGLE_KNOB_MARGIN, d, d))
            painter.restore()
            group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
            painter.setPen(QApplication.palette().color(group, QPalette.ColorRole.WindowText))
            left = track.right() + _TOGGLE_TEXT_GAP
            text_rect = QRectF(left, 0, max(0.0, self.width() - left), self.height())
            painter.drawText(
                text_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self.state_text(),
            )
        finally:
            painter.end()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in _PALETTE_EVENTS:
            self.update()
        elif event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        elif event.type() == QEvent.Type.FontChange:
            self.updateGeometry()


# ---- SettingsRow -------------------------------------------------------------------------------

LABEL_MIN_WIDTH = 240  # px: cột chữ của hàng hẹp hơn thì control xuống hàng dưới


def _row_label(parent: QWidget, object_name: str) -> QLabel:
    label = _plain_label(parent)
    if object_name:
        label.setObjectName(object_name)
    label.setWordWrap(True)
    label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return label


class SettingsRow(QFrame):
    """Một hàng cài đặt: nhãn, gợi ý, các dòng phụ, control bên phải (hẹp thì xuống dưới)."""

    def __init__(self, parent: QWidget | None = None, *, object_name: str = "") -> None:
        super().__init__(parent)
        if object_name:
            self.setObjectName(object_name)
        self.setProperty(theme.SURFACE_PROPERTY, "raised")
        # Không cao hơn cần: QBoxLayout chia chỗ thừa cho hàng có nhãn xuống dòng (heightForWidth).
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.label = _row_label(self, f"{object_name}Label" if object_name else "")
        self.label.setFont(_demibold(self.label.font()))
        self.hint_label = _row_label(self, f"{object_name}Hint" if object_name else "")
        self.hint_label.setProperty(theme.TONE_PROPERTY, "muted")
        self.hint_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.hint_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._control: QWidget | None = None
        self._narrow: bool | None = None

        self._text_column = QVBoxLayout()
        self._text_column.setContentsMargins(0, 0, 0, 0)
        self._text_column.setSpacing(2)
        self._text_column.addWidget(self.label)
        self._text_column.addWidget(self.hint_label)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(theme.SPACING[3], theme.SPACING[2], theme.SPACING[3], theme.SPACING[2])
        self._grid.setHorizontalSpacing(theme.SPACING[3])
        self._grid.setVerticalSpacing(theme.SPACING[1])
        self._grid.addLayout(self._text_column, 0, 0)
        self._grid.setColumnStretch(0, 1)
        self.hint_label.setVisible(False)

    @property
    def control(self) -> QWidget | None:
        return self._control

    @property
    def narrow(self) -> bool:
        """Control đang nằm dưới cột chữ."""
        return bool(self._narrow)

    def set_text(self, label: str, hint: str = "") -> None:
        """Nhãn và gợi ý; control (nếu có) mang tên trợ năng = nhãn, mô tả = gợi ý."""
        self.label.setText(label)
        self.hint_label.setText(hint)
        self.hint_label.setVisible(bool(hint))
        self.setAccessibleName(label)
        control = self._control
        if control is not None and not control.property("keepAccessible"):
            control.setAccessibleName(label)
            control.setAccessibleDescription(hint)

    def add_line(self, widget: QWidget) -> None:
        """Thêm một dòng dưới gợi ý (dòng phụ, lỗi), trong cột chữ."""
        self._text_column.addWidget(widget)

    def set_control(self, widget: QWidget) -> None:
        self._control = widget
        self._narrow = None
        self._place_control()

    def _control_width(self) -> int:
        control = self._control
        if control is None:
            return 0
        hint = control.sizeHint().width()
        return max(hint, control.minimumWidth())

    def _place_control(self) -> None:
        control = self._control
        if control is None:
            return
        margins = self._grid.contentsMargins()
        inner = self.width() - margins.left() - margins.right()
        narrow = inner - self._control_width() - self._grid.horizontalSpacing() < LABEL_MIN_WIDTH
        if narrow == self._narrow:
            return
        self._narrow = narrow
        self._grid.removeWidget(control)
        if narrow:
            self._grid.addWidget(control, 1, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        else:
            self._grid.addWidget(control, 0, 1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._place_control()


# ---- RovingGroup, SegmentedControl -------------------------------------------------------------

_PREV_KEYS = (Qt.Key.Key_Left, Qt.Key.Key_Up)
_NEXT_KEYS = (Qt.Key.Key_Right, Qt.Key.Key_Down)
# (token chữ/viền, token nền, ngưỡng) của `SegmentedControl`, phải đạt với mọi màu nhấn × sáng/tối:
# chữ nút thường trên nền khung và nền hover; chữ nút chọn trên nền nhấn; viền khung trên nền cửa sổ và
# nền nổi (WCAG 1.4.11); viền tiêu điểm (`text` = `focus_ring_color`) trên nền nhấn.
SEGMENT_CONTRAST_PAIRS: tuple[tuple[str, str, float], ...] = (
    ("text", "surface_raised", theme.MIN_CONTRAST),
    ("text", "selected", theme.MIN_CONTRAST),
    ("accent_text", "accent", theme.MIN_CONTRAST),
    ("border_strong", "surface", 3.0),
    ("border_strong", "surface_raised", 3.0),
    ("text", "accent", 3.0),
)


class RovingGroup(QObject):
    """Nhóm nút chọn một: Tab vào nút đang chọn, mũi tên đi và chọn (xem docstring module)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._buttons: list[QAbstractButton] = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

    @property
    def group(self) -> QButtonGroup:
        return self._group

    def buttons(self) -> list[QAbstractButton]:
        return list(self._buttons)

    def add(self, button: QAbstractButton) -> None:
        button.setCheckable(True)
        self._group.addButton(button, len(self._buttons))
        self._buttons.append(button)
        button.installEventFilter(self)
        button.toggled.connect(lambda _checked: self.sync_focus())
        self.sync_focus()

    def checked_index(self) -> int:
        """Vị trí nút đang chọn, -1 khi chưa nút nào."""
        return self._group.checkedId()

    def set_checked(self, index: int) -> None:
        """Chọn nút `index` (không phát `clicked`); ngoài khoảng thì bỏ chọn cả nhóm."""
        if 0 <= index < len(self._buttons):
            self._buttons[index].setChecked(True)
        else:
            self._group.setExclusive(False)
            for button in self._buttons:
                button.setChecked(False)
            self._group.setExclusive(True)
        self.sync_focus()

    def sync_focus(self) -> None:
        """Chỉ nút đang chọn (không có thì nút đầu) nhận Tab."""
        current = max(0, self.checked_index())
        for index, button in enumerate(self._buttons):
            policy = Qt.FocusPolicy.StrongFocus if index == current else Qt.FocusPolicy.ClickFocus
            if button.focusPolicy() != policy:
                button.setFocusPolicy(policy)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and isinstance(watched, QAbstractButton)
            and watched in self._buttons
            and event.modifiers() in _PLAIN_KEYS
        ):
            target = self._target(self._buttons.index(watched), event.key(), watched)
            if target is not None:
                self._move_to(target, event.key())
                return True
        return False

    def _target(self, index: int, key: int, button: QAbstractButton) -> int | None:
        count = len(self._buttons)
        if key == Qt.Key.Key_Home:
            return 0
        if key == Qt.Key.Key_End:
            return count - 1
        step = -1 if key in _PREV_KEYS else 1 if key in _NEXT_KEYS else 0
        if step == 0:
            return None
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right) and button.isRightToLeft():
            step = -step
        return (index + step) % count

    def _move_to(self, index: int, key: int) -> None:
        button = self._buttons[index]
        if not button.isEnabled():
            return
        backwards = key in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_Home)
        reason = Qt.FocusReason.BacktabFocusReason if backwards else Qt.FocusReason.TabFocusReason
        if not button.isChecked():
            button.click()
        button.setFocus(reason)


class SegmentedControl(QFrame):
    """Nhóm nút chọn một nằm liền nhau (xem docstring module). Chữ do bên gọi đặt (`set_texts`)."""

    value_selected = Signal(object)

    def __init__(self, values: Sequence[object], parent: QWidget | None = None) -> None:
        if not values:
            raise ValueError("SegmentedControl cần ít nhất một giá trị")
        super().__init__(parent)
        self.setProperty(theme.SEGMENTED_PROPERTY, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._values = tuple(values)
        self._value: object = self._values[0]
        self._roving = RovingGroup(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        last = len(self._values) - 1
        for index in range(len(self._values)):
            button = QRadioButton(self)
            position = "first" if index == 0 else "last" if index == last else "middle"
            button.setProperty(theme.SEGMENT_PROPERTY, position)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            layout.addWidget(button)
            self._roving.add(button)
        self._roving.group.idClicked.connect(self._on_clicked)
        self._indicator_index = -1  # nút mà khối chọn đang hướng tới
        self._slide_from: QRectF | None = None  # rect lúc bắt đầu trượt (None: không trượt)
        self._slide_t = 1.0
        self._sliding_button: QRadioButton | None = None  # nút mang SEGMENT_SLIDING_PROPERTY
        self._roving.group.idToggled.connect(self._on_id_toggled)
        self.set_value(self._values[0])

    def values(self) -> tuple[object, ...]:
        return self._values

    def buttons(self) -> list[QRadioButton]:
        return [b for b in self._roving.buttons() if isinstance(b, QRadioButton)]

    def button(self, value: object) -> QRadioButton:
        """Nút của `value`; giá trị lạ → `ValueError`."""
        return self.buttons()[self._values.index(value)]

    def value(self) -> object:
        return self._value

    def checked_button(self) -> QRadioButton | None:
        index = self._roving.checked_index()
        return self.buttons()[index] if index >= 0 else None

    def set_value(self, value: object) -> None:
        """Chọn `value` (không phát `value_selected`); giá trị lạ → nút đầu."""
        index = self._values.index(value) if value in self._values else 0
        self._value = self._values[index]
        self._roving.set_checked(index)

    def set_texts(self, texts: Sequence[str]) -> None:
        """Chữ của từng nút theo thứ tự giá trị; tên trợ năng của nút = chữ."""
        for button, text in zip(self.buttons(), texts, strict=True):
            button.setText(text)
            button.setAccessibleName(text)

    def _on_clicked(self, index: int) -> None:
        value = self._values[index]
        if value == self._value:
            return
        self._value = value
        self.value_selected.emit(value)

    # ---- Khối chọn -----------------------------------------------------------------------------

    def indicator_rect(self) -> QRectF:
        """Rect khối chọn đang vẽ (tọa độ của khung); chưa chọn nút nào → rỗng."""
        index = self._indicator_index
        buttons = self.buttons()
        if not 0 <= index < len(buttons):
            return QRectF()
        target = QRectF(buttons[index].geometry())
        start = self._slide_from
        if start is None:
            return target
        t = self._slide_t
        return QRectF(
            start.x() + (target.x() - start.x()) * t,
            start.y() + (target.y() - start.y()) * t,
            start.width() + (target.width() - start.width()) * t,
            start.height() + (target.height() - start.height()) * t,
        )

    def _on_id_toggled(self, _index: int, _checked: bool) -> None:
        new = self._roving.checked_index()
        if new == self._indicator_index:
            return
        start = self.indicator_rect()
        old = self._indicator_index
        self._indicator_index = new
        self._clear_sliding()
        if old < 0 or new < 0 or not self.isVisible() or not motion.should_animate() or start.isEmpty():
            self._jump_indicator()
            return
        self._slide_from = start
        self._slide_t = 0.0
        if self.isEnabled():
            button = self.buttons()[new]
            button.setProperty(theme.SEGMENT_SLIDING_PROPERTY, True)
            _repolish(button)
            self._sliding_button = button
        motion.animate_value(
            self, 0.0, 1.0, motion.NORMAL, self._set_slide, key="indicator", on_finished=self._end_slide
        )
        self.update()

    def _set_slide(self, value: object) -> None:
        try:
            t = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            t = 1.0
        self._slide_t = max(0.0, min(1.0, t))
        self.update()

    def _jump_indicator(self) -> None:
        """Khối về thẳng nút đang chọn (dừng hoạt ảnh đang chạy, gỡ property trượt)."""
        motion.animate_value(
            self, 1.0, 1.0, motion.INSTANT, self._set_slide, key="indicator", on_finished=self._end_slide
        )

    def _end_slide(self) -> None:
        self._slide_from = None
        self._slide_t = 1.0
        self._clear_sliding()
        self.update()

    def _clear_sliding(self) -> None:
        button = self._sliding_button
        self._sliding_button = None
        if button is not None and isValid(button):
            button.setProperty(theme.SEGMENT_SLIDING_PROPERTY, None)
            _repolish(button)

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        if self._slide_from is not None or self._sliding_button is not None:
            self._jump_indicator()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        rect = self.indicator_rect()
        if rect.isEmpty():
            return
        t = theme.tokens_for(self)
        fill, edge = (t.accent, t.accent_strong) if self.isEnabled() else (t.selected, t.border)
        radius = max(0, theme.RADIUS_CONTROL - 2)
        width = 2  # = viền của nút `segment` trong `theme.stylesheet`
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(edge)
            painter.drawRoundedRect(rect, radius, radius)
            painter.setBrush(fill)
            inner = rect.adjusted(width, width, -width, -width)
            painter.drawRoundedRect(inner, max(0, radius - width), max(0, radius - width))
        finally:
            painter.end()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (*_PALETTE_EVENTS, QEvent.Type.EnabledChange):
            self.update()
