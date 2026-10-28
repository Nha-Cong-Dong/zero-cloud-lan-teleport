"""Cửa sổ chính (khung P4-3): trạng thái của node, chưa có radar hay danh sách lượt truyền.

Bốn trạng thái (`ViewState`):

- STARTING: “Đang khởi động…”.
- RUNNING: sẵn sàng nhận file, kèm tên máy này và thư mục nhận.
- ERROR: câu lỗi khởi động (`texts.start_error`) và gợi ý, nút “Thử lại”.
- SETUP: máy chưa có danh tính (M3), nút “Thử lại”.

Mọi chữ là chữ thuần (`Qt.PlainText`), đọc được bằng bàn phím (Tab đi qua từng dòng, có tên cho
trình đọc màn hình). Trạng thái luôn nói bằng chữ, không chỉ bằng màu (mvp N12). Phím tắt: F5 =
Thử lại (khi nút đang hiện), Ctrl+W = đóng cửa sổ, Ctrl+Q = Thoát.

Đóng cửa sổ (`closeEvent`): khay dùng được (`Tray.is_available()`) thì ẩn cửa sổ và phát
`hidden_to_tray` (app vẫn chạy và nhận file); không có khay thì phát `quit_requested`. Cửa sổ
không tự thoát app: `ui.app` quyết định.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QAction, QCloseEvent, QFont, QKeySequence
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget

from teleport.core.display import display_name
from teleport.ui import texts
from teleport.ui.tray import Tray

_TEXT_FLAGS = Qt.TextInteractionFlag.TextSelectableByKeyboard | Qt.TextInteractionFlag.TextSelectableByMouse


class ViewState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"
    SETUP = "setup"


def _label(object_name: str, *, size_delta: int = 0, bold: bool = False) -> QLabel:
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


class MainWindow(QMainWindow):
    """Cửa sổ chính; trạng thái đổi bằng các hàm `show_*`."""

    retry_requested = Signal()
    quit_requested = Signal()
    hidden_to_tray = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("mainWindow")
        self.setMinimumSize(480, 300)
        self._state = ViewState.STARTING
        self._name: str | None = None
        self._receive_dir: Path | None = None
        self._error_code = ""
        self._error_detail = ""

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(10)

        self.title_label = _label("titleLabel", size_delta=6, bold=True)
        self.title_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.title_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # chỉ là tên app; tiêu đề cửa sổ đã có
        self.status_label = _label("statusLabel", size_delta=2, bold=True)
        self.detail_label = _label("detailLabel")
        self.hint_label = _label("hintLabel")
        self.retry_button = QPushButton(central)
        self.retry_button.setObjectName("retryButton")
        self.retry_button.clicked.connect(self.retry_requested)

        layout.addWidget(self.title_label)
        layout.addSpacing(6)
        layout.addWidget(self.status_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.hint_label)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.retry_button)
        layout.addLayout(buttons)
        self.setCentralWidget(central)

        QWidget.setTabOrder(self.status_label, self.detail_label)
        QWidget.setTabOrder(self.detail_label, self.hint_label)
        QWidget.setTabOrder(self.hint_label, self.retry_button)

        self._retry_action = QAction(self)
        self._retry_action.setShortcut(QKeySequence(Qt.Key.Key_F5))
        self._retry_action.triggered.connect(self._retry_shortcut)
        self._close_action = QAction(self)
        self._close_action.setShortcut(QKeySequence("Ctrl+W"))
        self._close_action.triggered.connect(self.close)
        self._quit_action = QAction(self)
        self._quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        self._quit_action.triggered.connect(self.quit_requested)
        for action in (self._retry_action, self._close_action, self._quit_action):
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            self.addAction(action)

        self.retranslate()

    # ---- Trạng thái --------------------------------------------------------------------------

    @property
    def view_state(self) -> ViewState:
        return self._state

    def show_starting(self) -> None:
        self._state = ViewState.STARTING
        self.retranslate()
        self._place_focus()

    def show_running(self, name: str | None, receive_dir: Path | None) -> None:
        self._state = ViewState.RUNNING
        self._name, self._receive_dir = name, receive_dir
        self.retranslate()
        self._place_focus()

    def show_error(self, code: str, detail: str = "") -> None:
        """Lỗi khởi động node: `code` là `bridge.StartError`, `detail` đã làm sạch."""
        self._state = ViewState.ERROR
        self._error_code, self._error_detail = code, detail
        self.retranslate()
        self._place_focus()

    def show_setup_needed(self) -> None:
        self._state = ViewState.SETUP
        self.retranslate()
        self._place_focus()

    def _place_focus(self) -> None:
        """Đặt tiêu điểm bàn phím: nút Thử lại khi có, không thì dòng trạng thái."""
        if self.retry_button.isVisibleTo(self):
            self.retry_button.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.status_label.setFocus(Qt.FocusReason.OtherFocusReason)

    def bring_to_front(self) -> None:
        """Hiện lại cửa sổ (kể cả khi đang thu nhỏ hay ẩn) và đưa lên trước."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    # ---- Chữ ---------------------------------------------------------------------------------

    def retranslate(self) -> None:
        """Đặt lại mọi chữ theo ngôn ngữ hiện tại và trạng thái hiện tại."""
        self.setWindowTitle(self.tr("Teleport"))
        self.title_label.setText(self.tr("Teleport"))
        self.title_label.setAccessibleName(self.tr("Tên ứng dụng"))
        self.retry_button.setText(texts.retry())
        self.retry_button.setAccessibleName(texts.retry())
        self.retry_button.setToolTip(self.tr("Khởi động lại Teleport (F5)"))
        self._retry_action.setText(texts.retry())
        self._close_action.setText(self.tr("Đóng cửa sổ"))
        self._quit_action.setText(texts.menu_quit())

        status, detail, hints = self._content()
        self.status_label.setText(status)
        self.status_label.setAccessibleName(self.tr("Trạng thái"))
        self.status_label.setAccessibleDescription(status)
        self._set_optional(self.detail_label, detail, self.tr("Chi tiết"))
        self._set_optional(self.hint_label, "\n".join(hints), self.tr("Gợi ý"))
        self.retry_button.setVisible(self._state in (ViewState.ERROR, ViewState.SETUP))

    def _content(self) -> tuple[str, str, list[str]]:
        """(dòng trạng thái, dòng chi tiết, các dòng gợi ý) của trạng thái hiện tại."""
        if self._state is ViewState.STARTING:
            return self.tr("Đang khởi động…"), "", []
        if self._state is ViewState.RUNNING:
            lines = []
            if self._name:
                lines.append(self.tr("Tên máy này: {name}").replace("{name}", display_name(self._name)))
            if self._receive_dir is not None:
                lines.append(self.tr("Thư mục nhận: {path}").replace("{path}", str(self._receive_dir)))
            return (
                self.tr("Đang chạy, sẵn sàng nhận file từ các máy đã ghép đôi."),
                "\n".join(lines),
                [self.tr("Đóng cửa sổ này thì Teleport vẫn chạy ở khay hệ thống.")]
                if Tray.is_available()
                else [],
            )
        if self._state is ViewState.SETUP:
            head, hints = texts.setup_needed()
            return self.tr("Chưa thiết lập"), head, hints
        head, hints = texts.start_error(self._error_code, self._error_detail)
        return self.tr("Không khởi động được"), head, hints

    @staticmethod
    def _set_optional(label: QLabel, text: str, accessible_name: str) -> None:
        label.setText(text)
        label.setAccessibleName(accessible_name)
        label.setAccessibleDescription(text)
        label.setVisible(bool(text))

    # ---- Sự kiện -----------------------------------------------------------------------------

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        if Tray.is_available():
            self.hide()
            self.hidden_to_tray.emit()
        else:
            self.quit_requested.emit()

    def _retry_shortcut(self) -> None:
        if self.retry_button.isVisibleTo(self):
            self.retry_requested.emit()
