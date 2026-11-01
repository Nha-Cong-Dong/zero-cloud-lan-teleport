"""Biểu tượng ở khay hệ thống (mvp F9) và menu của nó.

Menu giữ bốn mục, theo thứ tự (M5): “Mở Teleport”, “Gửi file…”, “Tạm dừng nhận”, “Thoát”
(`actions()` trả cả bốn). Ở bản hackathon menu chỉ **hiện** hai mục “Mở Teleport” và “Thoát”:
“Gửi file…” và “Tạm dừng nhận” vừa ẩn vừa tắt tới khi P4-6 / P4-0 làm xong. Bấm vào biểu tượng
(một lần hoặc hai lần) cũng như chọn “Mở Teleport”.

Bóng thoại (`show_message(text, folder=None)`): chỉ hiện khi có khay và biểu tượng đang hiện; khi
thật sự hiện thì nhớ `folder` của bóng thoại đó (None cũng nhớ). Người dùng bấm vào bóng thoại
(`QSystemTrayIcon.messageClicked`) → phát `message_clicked(folder)` với thư mục của bóng thoại gần
nhất (`Path` hay None); `ui.app` quyết định mở thư mục hay đưa cửa sổ lên trước. Bóng thoại mang logo
(`make_icon`), không dùng icon chuẩn “i” của hệ thống.

`is_available()` (hàm của module, `Tray.is_available` gọi lại nó) là seam cho test: hệ thống
không có khay (một số môi trường Linux, `offscreen`) thì đóng cửa sổ là thoát app. Mọi chỗ
trong `ui` hỏi qua `Tray.is_available()`, nên vá hàm module hay vá thuộc tính lớp đều có tác dụng.

Biểu tượng là logo của dự án (`ui/logo.py`, SVG nhúng trong mã) vẽ ở nhiều cỡ (`make_icon`), không tải gì.
Menu nhận `LanguageChange` (qua event filter) thì gọi `retranslate()`.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from teleport.ui import logo, texts

_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def is_available() -> bool:
    """Hệ thống có khay để đặt biểu tượng không."""
    return QSystemTrayIcon.isSystemTrayAvailable()


def _draw(size: int) -> QPixmap:
    """Logo của dự án cỡ `size` px (`packaging/pyinstaller/make_icon.py` cũng dựng `teleport.ico` từ đây)."""
    return logo.pixmap(size)


def make_icon() -> QIcon:
    """Biểu tượng của app (cửa sổ và khay), vẽ ở nhiều cỡ."""
    icon = QIcon()
    for size in _ICON_SIZES:
        icon.addPixmap(_draw(size))
    return icon


class Tray(QObject):
    """Biểu tượng khay và menu (bốn mục, hiện hai). Chỉ hiện khi gọi `show()` và hệ thống có khay."""

    open_requested = Signal()
    quit_requested = Signal()
    message_clicked = Signal(object)  # Path | None: thư mục của bóng thoại gần nhất

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._message_folder: Path | None = None
        self._menu = QMenu()
        self._menu.installEventFilter(self)
        self._open = QAction(self._menu)
        self._send = QAction(self._menu)
        self._pause = QAction(self._menu)
        self._quit = QAction(self._menu)
        self._send.setEnabled(False)  # P4-6
        self._send.setVisible(False)
        self._pause.setEnabled(False)  # P4-0
        self._pause.setVisible(False)
        self._open.triggered.connect(self.open_requested)
        self._quit.triggered.connect(self.quit_requested)
        self._menu.addAction(self._open)
        self._menu.addAction(self._send)
        self._menu.addAction(self._pause)
        self._menu.addSeparator()
        self._menu.addAction(self._quit)
        self._menu.setDefaultAction(self._open)
        self._icon = QSystemTrayIcon(make_icon(), self)
        self._icon.setContextMenu(self._menu)
        self._icon.activated.connect(self._on_activated)
        self._icon.messageClicked.connect(self._on_message_clicked)
        self.retranslate()

    @staticmethod
    def is_available() -> bool:
        """Seam: gọi `tray.is_available()` của module lúc dùng."""
        return is_available()

    def actions(self) -> list[QAction]:
        """Bốn mục của menu theo thứ tự (không kể dòng kẻ)."""
        return [self._open, self._send, self._pause, self._quit]

    @property
    def menu(self) -> QMenu:
        return self._menu

    def retranslate(self) -> None:
        self._open.setText(texts.menu_open())
        self._send.setText(texts.menu_send())
        self._pause.setText(texts.menu_pause())
        self._quit.setText(texts.menu_quit())
        self._icon.setToolTip(texts.APP_NAME)
        self._menu.setAccessibleName(QCoreApplication.translate("Tray", "Menu của Teleport"))

    def show(self) -> None:
        """Hiện biểu tượng nếu hệ thống có khay."""
        if Tray.is_available():
            self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    @property
    def visible(self) -> bool:
        return self._icon.isVisible()

    def show_message(self, text: str, folder: Path | None = None) -> None:
        """Bóng thoại từ biểu tượng khay (không làm gì nếu không có khay hay biểu tượng đang ẩn).

        `folder`: thư mục mở khi người dùng bấm vào bóng thoại; chỉ nhớ khi bóng thoại thật sự hiện.
        """
        if Tray.is_available() and self._icon.isVisible():
            self._message_folder = folder
            self._icon.showMessage(texts.APP_NAME, text, self._icon.icon())  # logo, không icon “i”

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._menu and event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        return super().eventFilter(watched, event)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_requested.emit()

    def _on_message_clicked(self) -> None:
        self.message_clicked.emit(self._message_folder)

    def dispose(self) -> None:
        """Ẩn biểu tượng và xóa menu (menu không có cha Qt)."""
        self._icon.hide()
        self._menu.deleteLater()
