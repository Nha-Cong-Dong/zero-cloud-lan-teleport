"""Biểu tượng ở khay hệ thống (mvp F9) và menu của nó.

Menu có đúng bốn mục, theo thứ tự (M5): “Mở Teleport”, “Gửi file…”, “Tạm dừng nhận”, “Thoát”.
“Gửi file…” và “Tạm dừng nhận” tắt tới khi P4-6 / P4-0 làm xong. Bấm vào biểu tượng (một lần
hoặc hai lần) cũng như chọn “Mở Teleport”.

`is_available()` (hàm của module, `Tray.is_available` gọi lại nó) là seam cho test: hệ thống
không có khay (một số môi trường Linux, `offscreen`) thì đóng cửa sổ là thoát app. Mọi chỗ
trong `ui` hỏi qua `Tray.is_available()`, nên vá hàm module hay vá thuộc tính lớp đều có tác dụng.

Biểu tượng vẽ bằng `QPainter` trong code (`make_icon`), không có file tài nguyên, không tải gì.
Menu nhận `LanguageChange` (qua event filter) thì gọi `retranslate()`.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from teleport.ui import texts

_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
_ICON_BACKGROUND = "#0b5cad"
_ICON_FOREGROUND = "#ffffff"


def is_available() -> bool:
    """Hệ thống có khay để đặt biểu tượng không."""
    return QSystemTrayIcon.isSystemTrayAvailable()


def _draw(size: int) -> QPixmap:
    """Một ô vuông bo góc màu xanh với mũi tên trắng chỉ sang phải (gửi đi)."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = float(size)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_ICON_BACKGROUND))
        painter.drawRoundedRect(QRectF(0, 0, s, s), s * 0.22, s * 0.22)
        pen = QPen(QColor(_ICON_FOREGROUND), max(1.5, s * 0.11))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath(QPointF(s * 0.24, s * 0.5))
        path.lineTo(QPointF(s * 0.74, s * 0.5))
        path.moveTo(QPointF(s * 0.54, s * 0.3))
        path.lineTo(QPointF(s * 0.76, s * 0.5))
        path.lineTo(QPointF(s * 0.54, s * 0.7))
        painter.drawPath(path)
    finally:
        painter.end()
    return pixmap


def make_icon() -> QIcon:
    """Biểu tượng của app (cửa sổ và khay), vẽ ở nhiều cỡ."""
    icon = QIcon()
    for size in _ICON_SIZES:
        icon.addPixmap(_draw(size))
    return icon


class Tray(QObject):
    """Biểu tượng khay và menu bốn mục. Chỉ hiện khi gọi `show()` và hệ thống có khay."""

    open_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._menu = QMenu()
        self._menu.installEventFilter(self)
        self._open = QAction(self._menu)
        self._send = QAction(self._menu)
        self._pause = QAction(self._menu)
        self._quit = QAction(self._menu)
        self._send.setEnabled(False)  # P4-6
        self._pause.setEnabled(False)  # P4-0
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
        self._icon.setToolTip(self.tr("Teleport"))
        self._menu.setAccessibleName(self.tr("Menu của Teleport"))

    def show(self) -> None:
        """Hiện biểu tượng nếu hệ thống có khay."""
        if Tray.is_available():
            self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    @property
    def visible(self) -> bool:
        return self._icon.isVisible()

    def show_message(self, text: str) -> None:
        """Bóng thoại từ biểu tượng khay (không làm gì nếu không có khay hay biểu tượng đang ẩn)."""
        if Tray.is_available() and self._icon.isVisible():
            self._icon.showMessage(self.tr("Teleport"), text, QSystemTrayIcon.MessageIcon.Information)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._menu and event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        return super().eventFilter(watched, event)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_requested.emit()

    def dispose(self) -> None:
        """Ẩn biểu tượng và xóa menu (menu không có cha Qt)."""
        self._icon.hide()
        self._menu.deleteLater()
