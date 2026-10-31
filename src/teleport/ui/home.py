"""Thành phần riêng của trang HOME trong cửa sổ chính (spec B1): `StartPanel`, `MachineCard`, `SaveBar`.

Chỉ ghép từ `ui/widgets.py` và `ui/theme.py` (cùng `icons`, `texts`, `send`, `tray`,
`core.display`); không import `main_window`. Mỗi lớp **tạo** mọi nhãn và nút của mình; `MainWindow`
gán tên tắt (`status_label`, `address_label`, `copy_button`…), nối signal và giữ trạng thái. Chữ đặt
sẵn đi qua `QCoreApplication.translate("Home", …)` gọi lúc dùng; chỉ `MainWindow.retranslate()` gọi
`retranslate()` của các lớp này (chúng không tự bắt `LanguageChange`).

**Bố cục**
- `StartPanel` (`startView`, màn Bắt đầu / Đang khởi động / Lỗi): `start_scroll` (không khung, không
  nhận tiêu điểm, thanh cuộn ngang khi cần cho đường dẫn dài không dấu cách) bọc một widget căn giữa
  hai chiều bằng stretch; trong đó `card` (`startCard`, `QFrame[surface="card"]`, rộng tối đa
  `START_PANEL_MAX_WIDTH`, lề trong 32, khoảng cách 12). Từ trên xuống: hàng logo 40 px +
  `title_label`; hàng `status_icon` 20 px + `status_label` (SECTION); `detail_label`; `hint_label`;
  `starting_bar` (bận, 160x4, căn trái); hàng nút căn phải `retry_button`, `start_button` (cả hai là
  nút chính, mỗi lúc chỉ hiện một). Không có chữ đặt sẵn ngoài tên của `starting_bar`.
- `MachineCard` (`machineCard`, chân cột trái): avatar máy này | cột chữ: “Máy này” (CAPTION, muted),
  `name_label` (SECTION), hàng `address_label` (CAPTION, muted) + `copy_button`, `badge`. Lề
  16/12/12/12.
- `SaveBar` (`saveBar`, chân “Lưu vào” toàn chiều ngang): icon ↓ + `save_label` (cắt giữa theo bề
  ngang còn lại) + nút “Mở thư mục”. Lề 16/6/12/6.

**Token**: `MachineCard` trong suốt (thấy nền `sidebar` của cột trái), đường kẻ trên 1 px `border`
tự vẽ; avatar nền `accent`, icon `accent_text`, vành `sidebar`. `SaveBar` tự vẽ nền `sidebar` và
đường kẻ trên `border`; icon `text_muted`. Chữ phụ dùng `QLabel[tone="muted"]` (`text_muted` trên
`sidebar` ≥ 4,5:1, `tests/ui/test_tokens.py`). Màu lấy bằng `theme.tokens_for` lúc vẽ; đổi sáng/tối
thì vẽ lại và tô lại icon. Không thêm luật stylesheet, không `setPalette`.

**Trợ năng (mvp N12)**: mọi nhãn là `Qt.PlainText`, không `TextSelectableByKeyboard`. `name_label` và
`address_label` nhận tiêu điểm bằng Tab (chọn bằng chuột được); `name_label` tên “Máy này”, mô tả là
tên + chữ huy hiệu. Huy hiệu luôn có chữ (“Sẵn sàng nhận”, “Đang truyền n lượt”). Tooltip dựng từ
địa chỉ hay đường dẫn đi qua `widgets.safe_tooltip`. Chép địa chỉ: clipboard, tooltip “Đã chép địa
chỉ …” 2 giây và sự kiện trợ năng Alert (cố gắng tốt nhất).
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QRect, Qt, Signal
from PySide6.QtGui import (
    QAccessible,
    QAccessibleEvent,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPaintEvent,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from teleport.core.display import display_name
from teleport.ui import icons, texts, theme
from teleport.ui.send import printable
from teleport.ui.tray import make_icon
from teleport.ui.widgets import (
    AvatarKind,
    BadgeTone,
    DeviceAvatar,
    IconButton,
    Presence,
    StatusBadge,
    safe_tooltip,
)

# Chọn bằng chuột để chép; không `TextSelectableByKeyboard`: Tab tới nhãn sẽ hiện con trỏ chữ nhấp nháy.
_TEXT_FLAGS = Qt.TextInteractionFlag.TextSelectableByMouse
START_PANEL_MAX_WIDTH = 460  # px, khối căn giữa của màn Bắt đầu / Lỗi
_STATUS_ICON = 20  # px
_LOGO_SIZE = 40  # px, logo trong khối căn giữa
_STARTING_BAR = (160, 4)  # px
_COPIED_TOOLTIP_MS = 2000
_PALETTE_EVENTS = (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange)


def _label(object_name: str, *, size_delta: int = 0, bold: bool = False) -> QLabel:
    """Nhãn chữ thuần, xuống dòng, chọn bằng chuột, nhận tiêu điểm bằng Tab (trình đọc màn hình đọc được)."""
    label = QLabel()
    label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(_TEXT_FLAGS)
    label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    if size_delta or bold:
        font = QFont(label.font())
        if size_delta:
            font.setPointSizeF(font.pointSizeF() + size_delta)
        font.setBold(bold)
        label.setFont(font)
    return label


def _static_label(object_name: str, parent: QWidget | None = None) -> QLabel:
    """Nhãn chữ thuần không nhận tiêu điểm, không chọn chữ (chú thích, logo, icon)."""
    label = QLabel(parent)
    label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return label


# ---- StartPanel --------------------------------------------------------------------------------


class StartPanel(QWidget):
    """Khối căn giữa của màn Bắt đầu / Đang khởi động / Lỗi (chỉ bố cục; chữ do `MainWindow` đặt)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("startView")
        self.start_scroll = QScrollArea(self)
        self.start_scroll.setObjectName("startScroll")
        self.start_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.start_scroll.setWidgetResizable(True)
        self.start_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.start_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        center = QWidget()
        center.setObjectName("startCenter")
        self.card = QFrame(center)
        self.card.setObjectName("startCard")
        self.card.setProperty(theme.SURFACE_PROPERTY, "card")
        self.card.setMaximumWidth(START_PANEL_MAX_WIDTH)
        self.card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self.logo_label = _static_label("startLogo", self.card)
        self.logo_label.setFixedSize(_LOGO_SIZE, _LOGO_SIZE)
        self.logo_label.setPixmap(make_icon().pixmap(_LOGO_SIZE))
        self.title_label = _label("titleLabel")
        self.title_label.setWordWrap(False)
        self.title_label.setFont(theme.font_for(theme.TextStyle.TITLE, self.title_label.font()))
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.title_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # chỉ là tên app; tiêu đề cửa sổ đã có

        self.status_icon = _static_label("statusIcon", self.card)
        self.status_icon.setFixedSize(_STATUS_ICON, _STATUS_ICON)
        self.status_label = _label("statusLabel")
        self.status_label.setFont(theme.font_for(theme.TextStyle.SECTION, self.status_label.font()))
        self.detail_label = _label("detailLabel")
        self.hint_label = _label("hintLabel")

        self.starting_bar = QProgressBar(self.card)
        self.starting_bar.setObjectName("startingBar")
        self.starting_bar.setRange(0, 0)
        self.starting_bar.setTextVisible(False)
        self.starting_bar.setFixedSize(*_STARTING_BAR)
        self.starting_bar.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.retry_button = QPushButton(self.card)
        self.retry_button.setObjectName("retryButton")
        self.retry_button.setProperty(theme.PRIMARY_PROPERTY, True)  # màn Lỗi: hành động chính
        self.start_button = QPushButton(self.card)
        self.start_button.setObjectName("startButton")
        self.start_button.setDefault(True)
        self.start_button.setProperty(theme.PRIMARY_PROPERTY, True)

        column = QVBoxLayout(self.card)
        column.setContentsMargins(*(theme.SPACING[5],) * 4)
        column.setSpacing(theme.SPACING[2])
        brand = QHBoxLayout()
        brand.setSpacing(theme.SPACING[2])
        brand.addWidget(self.logo_label, 0, Qt.AlignmentFlag.AlignVCenter)
        brand.addWidget(self.title_label, 1, Qt.AlignmentFlag.AlignVCenter)
        column.addLayout(brand)
        status_row = QHBoxLayout()
        status_row.setSpacing(theme.SPACING[1])
        status_row.addWidget(self.status_icon, 0, Qt.AlignmentFlag.AlignTop)
        status_row.addWidget(self.status_label, 1)
        column.addLayout(status_row)
        column.addWidget(self.detail_label)
        column.addWidget(self.hint_label)
        column.addWidget(self.starting_bar, 0, Qt.AlignmentFlag.AlignLeft)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.retry_button)
        buttons.addWidget(self.start_button)
        column.addLayout(buttons)

        # Căn giữa bằng stretch, không bằng cờ căn lề: có cờ thì Qt đặt khối theo sizeHint và bỏ qua
        # heightForWidth của nhãn xuống dòng (như `widgets.EmptyState`).
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        row.addWidget(self.card, 1000)
        row.addStretch(1)
        outer = QVBoxLayout(center)
        outer.setContentsMargins(*(theme.SPACING[4],) * 4)
        outer.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)
        self.start_scroll.setWidget(center)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.start_scroll)

        QWidget.setTabOrder(self.status_label, self.detail_label)
        QWidget.setTabOrder(self.detail_label, self.hint_label)
        QWidget.setTabOrder(self.hint_label, self.retry_button)
        QWidget.setTabOrder(self.retry_button, self.start_button)
        self.refresh_icon()
        self.status_icon.setVisible(False)
        self.retranslate()

    def set_error_icon(self, shown: bool) -> None:
        """Hiện/ẩn icon lỗi (màu `danger`, tô lại theo chế độ hiện tại)."""
        self.refresh_icon()
        self.status_icon.setVisible(shown)

    def refresh_icon(self) -> None:
        """Dựng lại pixmap của `status_icon` theo token hiện tại (sau khi đổi sáng/tối)."""
        color = theme.tokens_for(self).danger
        self.status_icon.setPixmap(
            icons.pixmap("error_circle", _STATUS_ICON, color, self.status_icon.devicePixelRatioF())
        )

    def retranslate(self) -> None:
        self.starting_bar.setAccessibleName(QCoreApplication.translate("Home", "Đang khởi động"))


# ---- MachineCard -------------------------------------------------------------------------------


class MachineCard(QFrame):
    """Chân cột trái: máy này (tên, địa chỉ, nút chép, huy hiệu trạng thái)."""

    address_copied = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("machineCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._name: str | None = None
        self._active = 0
        self._copy_value: str | None = None

        self.avatar = DeviceAvatar(
            self,
            diameter=38,
            kind=AvatarKind.DESKTOP,
            presence=Presence.NONE,
            emphasis=True,
            ring="sidebar",
        )
        self.avatar.setObjectName("machineAvatar")
        self.caption_label = _static_label("machineCaption", self)
        self.caption_label.setFont(theme.font_for(theme.TextStyle.CAPTION, self.caption_label.font()))
        self.caption_label.setProperty(theme.TONE_PROPERTY, "muted")
        self.name_label = _label("machineName")
        self.name_label.setFont(theme.font_for(theme.TextStyle.SECTION, self.name_label.font()))
        self.address_label = _label("addressLabel")
        self.address_label.setFont(theme.font_for(theme.TextStyle.CAPTION, self.address_label.font()))
        self.address_label.setProperty(theme.TONE_PROPERTY, "muted")
        self.copy_button = IconButton("copy", QCoreApplication.translate("Home", "Chép địa chỉ"), self)
        self.copy_button.setObjectName("copyAddressButton")
        self.copy_button.clicked.connect(self._copy)
        self.copy_button.setVisible(False)
        self.badge = StatusBadge(self)
        self.badge.setObjectName("machineBadge")

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(2)
        text_column.addWidget(self.caption_label)
        text_column.addWidget(self.name_label)
        address_row = QHBoxLayout()
        address_row.setContentsMargins(0, 0, 0, 0)
        address_row.setSpacing(theme.SPACING[0])
        address_row.addWidget(self.address_label, 1)
        address_row.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignTop)
        text_column.addLayout(address_row)
        text_column.addWidget(self.badge)
        self.text_column = text_column
        self.address_row = address_row

        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.SPACING[3], theme.SPACING[2], theme.SPACING[2], theme.SPACING[2])
        layout.setSpacing(theme.SPACING[2])
        layout.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(text_column, 1)

        QWidget.setTabOrder(self.name_label, self.address_label)
        QWidget.setTabOrder(self.address_label, self.copy_button)
        self.retranslate()

    @property
    def copy_value(self) -> str | None:
        return self._copy_value

    @property
    def active(self) -> int:
        return self._active

    def set_name(self, name: str | None) -> None:
        """Tên máy này (chưa qua `display_name`); None thì ẩn dòng tên."""
        self._name = name
        self._update_name()

    def set_activity(self, active: int) -> None:
        """Số lượt chưa kết thúc: 0 → “Sẵn sàng nhận”, n ≥ 1 → “Đang truyền n lượt”."""
        if active < 0:
            raise ValueError(f"số lượt phải ≥ 0: {active}")
        self._active = active
        self._update_badge()

    def set_copy_value(self, value: str | None) -> None:
        """Chuỗi mà “Chép địa chỉ” chép; None thì ẩn nút."""
        self._copy_value = value or None
        self.copy_button.setVisible(self._copy_value is not None)

    def retranslate(self) -> None:
        t = QCoreApplication.translate
        caption = t("Home", "Máy này")
        self.caption_label.setText(caption)
        self.name_label.setAccessibleName(caption)
        self.copy_button.set_label(t("Home", "Chép địa chỉ"))
        self._update_badge()

    # ---- Bên trong -----------------------------------------------------------------------------

    def _update_name(self) -> None:
        shown = display_name(printable(self._name)) if self._name else ""
        self.name_label.setText(shown)
        self.name_label.setVisible(bool(shown))
        self._update_description()

    def _update_badge(self) -> None:
        if self._active > 0:
            text = QCoreApplication.translate("Home", "Đang truyền %n lượt", None, self._active)
            self.badge.set_status(BadgeTone.BUSY, text)
        else:
            self.badge.set_status(BadgeTone.SUCCESS, QCoreApplication.translate("Home", "Sẵn sàng nhận"))
        self._update_description()

    def _update_description(self) -> None:
        parts = [part for part in (self.name_label.text(), self.badge.text) if part]
        self.name_label.setAccessibleDescription(". ".join(parts))

    def _copy(self) -> None:
        value = self._copy_value
        if value is None:
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(value)
        message = texts.fill(QCoreApplication.translate("Home", "Đã chép địa chỉ {address}"), address=value)
        button = self.copy_button
        QToolTip.showText(
            button.mapToGlobal(QPoint(0, button.height())),
            safe_tooltip(message),
            button,
            QRect(),
            _COPIED_TOOLTIP_MS,
        )
        # Cố gắng tốt nhất: có HĐH không chuyển Alert tới trình đọc màn hình.
        with contextlib.suppress(Exception):
            QAccessible.updateAccessibility(QAccessibleEvent(button, QAccessible.Event.Alert))
        self.address_copied.emit(value)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.fillRect(0, 0, self.width(), 1, theme.tokens_for(self).border)
        finally:
            painter.end()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in _PALETTE_EVENTS:
            self.update()


# ---- SaveBar -----------------------------------------------------------------------------------


class SaveBar(QFrame):
    """Chân “Lưu vào: …” toàn chiều ngang cửa sổ, kèm nút “Mở thư mục”."""

    open_requested = Signal(object)  # Path

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("saveBar")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._receive_dir: Path | None = None
        self._full_text = ""

        self.icon_label = _static_label("saveIcon", self)
        self.icon_label.setFixedSize(_STATUS_ICON, _STATUS_ICON)
        self.save_label = _static_label("saveLabel", self)
        self.save_label.setFont(theme.font_for(theme.TextStyle.CAPTION, self.save_label.font()))
        self.save_label.setProperty(theme.TONE_PROPERTY, "muted")
        self.save_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.save_label.installEventFilter(self)  # cắt lại chữ mỗi khi nhãn đổi bề ngang
        self.open_button = IconButton(
            "folder_open", QCoreApplication.translate("Home", "Mở thư mục"), self, show_text=True
        )
        self.open_button.setObjectName("openReceiveDirButton")
        self.open_button.clicked.connect(self._open)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.SPACING[3], 6, theme.SPACING[2], 6)
        layout.setSpacing(theme.SPACING[1])
        layout.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.save_label, 1)
        layout.addWidget(self.open_button)
        self._refresh_icon()
        self.setVisible(False)
        self.retranslate()

    @property
    def receive_dir(self) -> Path | None:
        return self._receive_dir

    @property
    def full_text(self) -> str:
        """Chữ đầy đủ “Lưu vào: …” (trước khi cắt giữa)."""
        return self._full_text

    def set_receive_dir(self, path: Path | None) -> None:
        """Thư mục nhận đang dùng; None thì ẩn cả thanh."""
        self._receive_dir = path
        self._update_text()
        self.setVisible(path is not None)

    def retranslate(self) -> None:
        t = QCoreApplication.translate
        self.save_label.setAccessibleName(t("Home", "Thư mục nhận"))
        self.open_button.set_label(t("Home", "Mở thư mục"))
        self._update_text()

    # ---- Bên trong -----------------------------------------------------------------------------

    def _update_text(self) -> None:
        if self._receive_dir is None:
            full = ""
        else:
            full = texts.fill(
                QCoreApplication.translate("Home", "Lưu vào: {path}"), path=printable(str(self._receive_dir))
            )
        self._full_text = full
        self.save_label.setToolTip(safe_tooltip(full) if full else "")
        self.save_label.setAccessibleDescription(full)
        self.open_button.setAccessibleDescription(full)
        self._elide()

    def _elide(self) -> None:
        width = self.save_label.contentsRect().width()
        text = self._full_text
        if text and width > 0:
            text = QFontMetrics(self.save_label.font()).elidedText(text, Qt.TextElideMode.ElideMiddle, width)
        if self.save_label.text() != text:
            self.save_label.setText(text)

    def _open(self) -> None:
        if self._receive_dir is not None:
            self.open_requested.emit(self._receive_dir)

    def _refresh_icon(self) -> None:
        color = theme.tokens_for(self).text_muted
        self.icon_label.setPixmap(
            icons.pixmap("arrow_download", _STATUS_ICON, color, self.icon_label.devicePixelRatioF())
        )

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.save_label and event.type() == QEvent.Type.Resize:
            self._elide()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()

    def paintEvent(self, event: QPaintEvent) -> None:
        t = theme.tokens_for(self)
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), t.sidebar)
            painter.fillRect(0, 0, self.width(), 1, t.border)
        finally:
            painter.end()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in _PALETTE_EVENTS:
            self._refresh_icon()
            self.update()
