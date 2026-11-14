"""Hộp thoại Chế độ khách qua Web QR (Web Guest Mode Dialog) cho PySide6/Qt (v1.2.0).

Hiển thị mã QR trực quan, liên kết kết nối mạng nội bộ và tiến độ tải tệp với điện thoại.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from teleport.app.web_guest import WebGuestManager
from teleport.core.display import format_size
from teleport.paths import default_receive_dir

if TYPE_CHECKING:
    from teleport.app.node import Node
    from teleport.app.web_guest import GuestSessionInfo
    from teleport.ui.bridge import NodeHost

log = logging.getLogger(__name__)


def qr_matrix_to_pixmap(matrix: list[list[bool]], module_size: int = 6, border: int = 4) -> QPixmap:
    """Chuyển đổi ma trận QR boolean sang QPixmap để hiển thị trong giao diện Qt."""
    size = len(matrix)
    full_size = (size + 2 * border) * module_size
    img = QImage(full_size, full_size, QImage.Format.Format_RGB32)
    img.fill(QColor(255, 255, 255))

    painter = QPainter(img)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(0, 0, 0)))

    for r in range(size):
        for c in range(size):
            if matrix[r][c]:
                painter.drawRect(
                    (c + border) * module_size,
                    (r + border) * module_size,
                    module_size,
                    module_size,
                )
    painter.end()
    return QPixmap.fromImage(img)


class WebGuestDialog(QDialog):
    """Hộp thoại hiển thị mã QR và điều phối phiên chế độ khách cho điện thoại."""

    upload_progress = Signal(str, int, int)  # name, current, total
    upload_completed = Signal(str, int, str)  # name, size, path
    session_started = Signal(object)  # GuestSessionInfo
    session_failed = Signal(str)  # error text

    def __init__(
        self,
        node: Node | None = None,
        host: NodeHost | None = None,
        initial_files: list[Path] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.node = node
        self.host = host
        self.initial_files = list(initial_files or [])
        self._session_info: GuestSessionInfo | None = None

        self.setWindowTitle(QCoreApplication.translate("WebGuest", "Chuyển tệp với điện thoại"))
        self.setMinimumWidth(440)
        self.setModal(True)

        self._build_ui()

        self.upload_progress.connect(self.update_progress)
        self.upload_completed.connect(self._on_upload_completed_ui)
        self.session_started.connect(self.set_session_info)
        self.session_failed.connect(self._on_session_failed)

        if self.host is not None:
            self._start_host_session()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        # Tiêu đề & hướng dẫn
        title = QLabel(
            QCoreApplication.translate("WebGuest", "Quét mã QR để chuyển tệp"),
            self,
        )
        title_font = QFont()
        title_font.setPointSize(13)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        self.subtitle = QLabel(
            QCoreApplication.translate(
                "WebGuest",
                "Mở máy ảnh điện thoại quét mã bên dưới hoặc truy cập liên kết trong trình duyệt.",
            ),
            self,
        )
        self.subtitle.setWordWrap(True)
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setStyleSheet("color: #64748b;")
        layout.addWidget(self.subtitle)

        # Khung chứa ảnh mã QR
        self.qr_card = QFrame(self)
        self.qr_card.setFrameShape(QFrame.Shape.StyledPanel)
        self.qr_card.setStyleSheet(
            "background-color: white; border: 1px solid #e2e8f0; border-radius: 12px; padding: 12px;"
        )
        qr_layout = QVBoxLayout(self.qr_card)
        qr_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.qr_label = QLabel(self.qr_card)
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr_layout.addWidget(self.qr_label)
        layout.addWidget(self.qr_card, alignment=Qt.AlignmentFlag.AlignCenter)

        # Hàng địa chỉ URL & nút Chép
        url_layout = QHBoxLayout()
        self.url_label = QLabel(self)
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url_label.setStyleSheet(
            "background: rgba(0,0,0,0.05); padding: 8px 12px; border-radius: 8px; font-family: monospace;"
        )
        url_layout.addWidget(self.url_label, 1)

        self.copy_btn = QPushButton(QCoreApplication.translate("WebGuest", "Sao chép"), self)
        self.copy_btn.clicked.connect(self._copy_url)
        url_layout.addWidget(self.copy_btn)
        layout.addLayout(url_layout)

        # Thanh tiến độ khi nhận file
        self.progress_container = QWidget(self)
        prog_layout = QVBoxLayout(self.progress_container)
        prog_layout.setContentsMargins(0, 0, 0, 0)
        prog_layout.setSpacing(4)

        self.progress_label = QLabel(self.progress_container)
        self.progress_label.setStyleSheet("font-size: 12px; color: #64748b;")
        self.progress_bar = QProgressBar(self.progress_container)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)

        prog_layout.addWidget(self.progress_label)
        prog_layout.addWidget(self.progress_bar)
        self.progress_container.setVisible(False)
        layout.addWidget(self.progress_container)

        # Danh sách tệp chia sẻ sẵn
        self.shared_info_label = QLabel(self)
        self.shared_info_label.setStyleSheet("font-size: 12px; color: #475569;")
        layout.addWidget(self.shared_info_label)

        # Hàng nút thao tác ở dưới
        bottom_layout = QHBoxLayout()
        self.add_file_btn = QPushButton(
            QCoreApplication.translate("WebGuest", "+ Thêm tệp chia sẻ..."),
            self,
        )
        self.add_file_btn.clicked.connect(self._choose_and_add_file)
        bottom_layout.addWidget(self.add_file_btn)

        bottom_layout.addStretch(1)

        self.close_btn = QPushButton(QCoreApplication.translate("WebGuest", "Đóng"), self)
        self.close_btn.clicked.connect(self.accept)
        bottom_layout.addWidget(self.close_btn)
        layout.addLayout(bottom_layout)

    def _start_host_session(self) -> None:
        if self.host is None:
            return

        def on_prog(name: str, cur: int, tot: int) -> None:
            self.upload_progress.emit(name, cur, tot)

        def on_comp(name: str, size: int, path: Path) -> None:
            self.upload_completed.emit(name, size, str(path))

        async def _async_start(node: Node) -> GuestSessionInfo:
            await node.stop_guest_session()
            recv_dir = node.transfers.receive_dir
            if recv_dir is None:
                recv_dir = default_receive_dir()
            node._guest_manager = WebGuestManager(
                receive_dir=recv_dir,
                device_name=node.name,
                inactivity_timeout=600.0,
                on_upload_progress=on_prog,
                on_upload_complete=on_comp,
            )
            return await node._guest_manager.start_session(
                shared_files=self.initial_files,
            )

        def _on_done(result: GuestSessionInfo | BaseException) -> None:
            if isinstance(result, BaseException):
                self.session_failed.emit(str(result))
            else:
                self.session_started.emit(result)

        self.host.submit(_async_start, _on_done)

    def set_session_info(self, info: GuestSessionInfo) -> None:
        """Cập nhật giao diện khi phiên khách đã khởi động thành công."""
        self._session_info = info
        pixmap = qr_matrix_to_pixmap(info.qr_matrix, module_size=6, border=3)
        self.qr_label.setPixmap(pixmap)
        self.url_label.setText(info.url)
        self._update_shared_count()

    def _on_session_failed(self, err: str) -> None:
        self.subtitle.setText(
            QCoreApplication.translate("WebGuest", "Không thể khởi động phiên khách: ") + err
        )
        self.subtitle.setStyleSheet("color: #ef4444;")

    def _update_shared_count(self) -> None:
        count = len(self.initial_files)
        if count == 0:
            self.shared_info_label.setText(
                QCoreApplication.translate("WebGuest", "Chưa có tệp nào chia sẻ sẵn cho điện thoại.")
            )
        else:
            self.shared_info_label.setText(
                QCoreApplication.translate("WebGuest", f"Đang chia sẻ sẵn {count} tệp cho điện thoại.")
            )

    def _copy_url(self) -> None:
        if self._session_info:
            cb = QGuiApplication.clipboard()
            if cb:
                cb.setText(self._session_info.url)
            self.copy_btn.setText(QCoreApplication.translate("WebGuest", "✓ Đã chép"))

    def _choose_and_add_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            QCoreApplication.translate("WebGuest", "Chọn tệp chia sẻ cho điện thoại"),
        )
        for p_str in paths:
            p = Path(p_str)
            if p.is_file() and p not in self.initial_files:
                self.initial_files.append(p)
                if self.host is not None:

                    def _add(node: Node, file_path: Path = p) -> None:
                        if node._guest_manager:
                            node._guest_manager.add_file_to_share(file_path)

                    self.host.call(_add)
                elif self.node is not None and self.node._guest_manager is not None:
                    self.node._guest_manager.add_file_to_share(p)
        self._update_shared_count()

    def update_progress(self, filename: str, current: int, total: int) -> None:
        """Được gọi khi điện thoại đang tải lên một tệp."""
        self.progress_container.setVisible(True)
        percent = int((current / total) * 100) if total > 0 else 0
        self.progress_bar.setValue(percent)
        size_info = f"({format_size(current)}/{format_size(total)})"
        self.progress_label.setText(f"Đang nhận {filename}: {percent}% {size_info}")

    def _on_upload_completed_ui(self, filename: str, size: int, _path: str) -> None:
        self.progress_container.setVisible(True)
        self.progress_bar.setValue(100)
        self.progress_label.setText(f"✓ Đã nhận thành công: {filename} ({format_size(size)})")

    def closeEvent(self, event: object) -> None:
        if self.host is not None:

            async def _async_stop(node: Node) -> None:
                await node.stop_guest_session()

            self.host.submit(_async_stop)
        super().closeEvent(event)  # type: ignore[arg-type]
