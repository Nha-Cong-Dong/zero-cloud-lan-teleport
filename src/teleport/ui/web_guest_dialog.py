"""Hộp thoại Chế độ khách qua Web QR (Web Guest Mode Dialog) cho PySide6/Qt (v1.2.0).

Hiển thị mã QR trực quan, liên kết kết nối mạng nội bộ và tiến độ tải tệp với điện thoại.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtGui import QBrush, QFont, QGuiApplication, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from teleport.app.web_guest import WebGuestManager
from teleport.core.display import format_size
from teleport.core.qr import encode_wifi_qr
from teleport.net.web_guest import GuestScope
from teleport.paths import default_receive_dir
from teleport.ui.theme import tokens

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
    tok = tokens(dark=False)
    img.fill(tok.surface_raised)

    painter = QPainter(img)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(tok.text))

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
    session_ended = Signal()
    text_received = Signal(str)  # văn bản điện thoại gửi lên

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
        self._last_received_text: str = ""
        self._stopped_host = False

        self.setWindowTitle(QCoreApplication.translate("WebGuest", "Chuyển tệp với điện thoại"))
        self.setMinimumWidth(460)
        self.setModal(True)

        self._build_ui()

        self.upload_progress.connect(self.update_progress)
        self.upload_completed.connect(self._on_upload_completed_ui)
        self.session_started.connect(self.set_session_info)
        self.session_failed.connect(self._on_session_failed)
        self.session_ended.connect(self._on_session_ended)
        self.text_received.connect(self._on_text_received)
        self.finished.connect(self._stop_host_session)

        if self.host is not None:
            self._start_host_session()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
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
        self.subtitle.setProperty("tone", "muted")
        layout.addWidget(self.subtitle)

        # Tùy chọn quyền phiên và chế độ 1 lượt
        opts_layout = QHBoxLayout()
        scope_label = QLabel(QCoreApplication.translate("WebGuest", "Quyền phiên:"), self)
        scope_label.setProperty("tone", "muted")
        opts_layout.addWidget(scope_label)

        self.scope_combo = QComboBox(self)
        self.scope_combo.setAccessibleName(QCoreApplication.translate("WebGuest", "Quyền phiên khách"))
        self.scope_combo.addItem(QCoreApplication.translate("WebGuest", "Hai chiều"), GuestScope.BOTH)
        self.scope_combo.addItem(
            QCoreApplication.translate("WebGuest", "Chỉ nhận từ điện thoại"),
            GuestScope.RECEIVE_ONLY,
        )
        self.scope_combo.addItem(
            QCoreApplication.translate("WebGuest", "Chỉ gửi sang điện thoại"),
            GuestScope.SEND_ONLY,
        )
        self.scope_combo.currentIndexChanged.connect(self._on_options_changed)
        opts_layout.addWidget(self.scope_combo, 1)

        self.one_shot_check = QCheckBox(
            QCoreApplication.translate("WebGuest", "Chỉ dùng 1 lượt"),
            self,
        )
        self.one_shot_check.toggled.connect(self._on_options_changed)
        opts_layout.addWidget(self.one_shot_check)
        layout.addLayout(opts_layout)

        # Khung chứa ảnh mã QR
        self.qr_card = QFrame(self)
        self.qr_card.setFrameShape(QFrame.Shape.StyledPanel)
        self.qr_card.setProperty("surface", "card")
        qr_layout = QVBoxLayout(self.qr_card)
        qr_layout.setContentsMargins(12, 12, 12, 12)
        qr_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.qr_label = QLabel(self.qr_card)
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr_layout.addWidget(self.qr_label)
        layout.addWidget(self.qr_card, alignment=Qt.AlignmentFlag.AlignCenter)

        # Hàng địa chỉ URL & nút Chép
        url_layout = QHBoxLayout()
        self.url_label = QLabel(self)
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.url_label.setProperty("surface", "sidebar")
        url_font = self.url_label.font()
        url_font.setFamilies(["monospace", "Consolas", "Courier New"])
        self.url_label.setFont(url_font)
        url_layout.addWidget(self.url_label, 1)

        self.copy_btn = QPushButton(QCoreApplication.translate("WebGuest", "Sao chép"), self)
        self.copy_btn.clicked.connect(self._copy_url)
        url_layout.addWidget(self.copy_btn)
        layout.addLayout(url_layout)

        # Gửi / nhận nhanh văn bản hai chiều
        text_layout = QHBoxLayout()
        self.text_input = QLineEdit(self)
        self.text_input.setAccessibleName(
            QCoreApplication.translate("WebGuest", "Văn bản gửi sang điện thoại")
        )
        self.text_input.setPlaceholderText(
            QCoreApplication.translate("WebGuest", "Nhập ghi chú hoặc liên kết để gửi sang điện thoại...")
        )
        self.text_input.returnPressed.connect(self._send_text)
        text_layout.addWidget(self.text_input, 1)

        self.send_text_btn = QPushButton(QCoreApplication.translate("WebGuest", "Gửi chữ"), self)
        self.send_text_btn.clicked.connect(self._send_text)
        text_layout.addWidget(self.send_text_btn)
        layout.addLayout(text_layout)

        self.received_text_container = QWidget(self)
        recv_text_layout = QHBoxLayout(self.received_text_container)
        recv_text_layout.setContentsMargins(0, 0, 0, 0)
        self.received_text_label = QLabel(self.received_text_container)
        self.received_text_label.setWordWrap(True)
        self.received_text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        recv_text_layout.addWidget(self.received_text_label, 1)

        self.copy_text_btn = QPushButton(
            QCoreApplication.translate("WebGuest", "Chép chữ"),
            self.received_text_container,
        )
        self.copy_text_btn.clicked.connect(self._copy_received_text)
        recv_text_layout.addWidget(self.copy_text_btn)
        self.received_text_container.setVisible(False)
        layout.addWidget(self.received_text_container)

        # Thanh tiến độ khi nhận file
        self.progress_container = QWidget(self)
        prog_layout = QVBoxLayout(self.progress_container)
        prog_layout.setContentsMargins(0, 0, 0, 0)
        prog_layout.setSpacing(4)

        self.progress_label = QLabel(self.progress_container)
        self.progress_label.setProperty("tone", "muted")
        self.progress_bar = QProgressBar(self.progress_container)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)

        prog_layout.addWidget(self.progress_label)
        prog_layout.addWidget(self.progress_bar)
        self.progress_container.setVisible(False)
        layout.addWidget(self.progress_container)

        # Khung tạo mã QR Wi-Fi nội tuyến (không dùng hộp thoại chặn)
        self.wifi_panel = QFrame(self)
        self.wifi_panel.setFrameShape(QFrame.Shape.StyledPanel)
        self.wifi_panel.setProperty("surface", "card")
        wifi_layout = QVBoxLayout(self.wifi_panel)
        wifi_layout.setContentsMargins(12, 12, 12, 12)
        wifi_layout.setSpacing(8)

        wifi_row = QHBoxLayout()
        self.wifi_ssid_input = QLineEdit(self.wifi_panel)
        self.wifi_ssid_input.setAccessibleName(QCoreApplication.translate("WebGuest", "Tên mạng Wi-Fi"))
        self.wifi_ssid_input.setPlaceholderText(
            QCoreApplication.translate("WebGuest", "Tên mạng Wi-Fi (SSID)")
        )
        wifi_row.addWidget(self.wifi_ssid_input, 1)

        self.wifi_password_input = QLineEdit(self.wifi_panel)
        self.wifi_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.wifi_password_input.setAccessibleName(QCoreApplication.translate("WebGuest", "Mật khẩu Wi-Fi"))
        self.wifi_password_input.setPlaceholderText(QCoreApplication.translate("WebGuest", "Mật khẩu Wi-Fi"))
        wifi_row.addWidget(self.wifi_password_input, 1)

        self.wifi_generate_btn = QPushButton(
            QCoreApplication.translate("WebGuest", "Tạo QR"),
            self.wifi_panel,
        )
        self.wifi_generate_btn.clicked.connect(self._generate_wifi_qr)
        wifi_row.addWidget(self.wifi_generate_btn)
        wifi_layout.addLayout(wifi_row)

        self.wifi_qr_label = QLabel(self.wifi_panel)
        self.wifi_qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.wifi_qr_label.setWordWrap(True)
        wifi_layout.addWidget(self.wifi_qr_label)
        self.wifi_panel.setVisible(False)
        layout.addWidget(self.wifi_panel)

        # Danh sách tệp chia sẻ sẵn
        self.shared_info_label = QLabel(self)
        self.shared_info_label.setProperty("tone", "muted")
        layout.addWidget(self.shared_info_label)

        # Hàng nút thao tác ở dưới
        bottom_layout = QHBoxLayout()
        self.add_file_btn = QPushButton(
            QCoreApplication.translate("WebGuest", "+ Thêm tệp chia sẻ..."),
            self,
        )
        self.add_file_btn.clicked.connect(self._choose_and_add_file)
        bottom_layout.addWidget(self.add_file_btn)

        self.wifi_qr_btn = QPushButton(
            QCoreApplication.translate("WebGuest", "QR Wi-Fi..."),
            self,
        )
        self.wifi_qr_btn.setCheckable(True)
        self.wifi_qr_btn.toggled.connect(self._toggle_wifi_panel)
        bottom_layout.addWidget(self.wifi_qr_btn)

        bottom_layout.addStretch(1)

        self.close_btn = QPushButton(QCoreApplication.translate("WebGuest", "Đóng"), self)
        self.close_btn.clicked.connect(self.accept)
        bottom_layout.addWidget(self.close_btn)
        layout.addLayout(bottom_layout)

    def _selected_scope(self) -> GuestScope:
        data = self.scope_combo.currentData()
        if isinstance(data, GuestScope):
            return data
        return GuestScope.BOTH

    def _on_options_changed(self) -> None:
        if self.host is not None:
            self._start_host_session()

    def _start_host_session(self) -> None:
        if self.host is None:
            return
        self._stopped_host = False
        scope = self._selected_scope()
        one_shot = self.one_shot_check.isChecked()

        def on_prog(name: str, cur: int, tot: int) -> None:
            self.upload_progress.emit(name, cur, tot)

        def on_comp(name: str, size: int, path: Path) -> None:
            self.upload_completed.emit(name, size, str(path))

        def on_text(text: str) -> None:
            self.text_received.emit(text)

        def on_timeout() -> None:
            self.session_ended.emit()

        async def _async_start(node: Node) -> GuestSessionInfo:
            await node.stop_guest_session()
            recv_dir = node.transfers.receive_dir
            if recv_dir is None:
                recv_dir = default_receive_dir()
            if getattr(node, "create_subfolder", True):
                from teleport.app.transfers import make_transfer_subfolder_name

                sub_name = make_transfer_subfolder_name(QCoreApplication.translate("WebGuest", "Điện thoại"))
                recv_dir = recv_dir / sub_name
                recv_dir.mkdir(parents=True, exist_ok=True)
            node._guest_manager = WebGuestManager(
                receive_dir=recv_dir,
                device_name=node.name,
                inactivity_timeout=600.0,
                on_upload_progress=on_prog,
                on_upload_complete=on_comp,
                on_session_timeout=on_timeout,
                on_text_received=on_text,
            )
            return await node._guest_manager.start_session(
                shared_files=self.initial_files,
                scope=scope,
                one_shot=one_shot,
            )

        def _on_done(result: GuestSessionInfo | BaseException) -> None:
            if isinstance(result, BaseException):
                self.session_failed.emit(str(result))
            else:
                self.session_started.emit(result)

        self.host.submit(_async_start, _on_done)

    def _stop_host_session(self) -> None:
        if self.host is None or self._stopped_host:
            return
        self._stopped_host = True

        async def _async_stop(node: Node) -> None:
            await node.stop_guest_session()

        self.host.submit(_async_stop)

    def set_session_info(self, info: GuestSessionInfo) -> None:
        """Cập nhật giao diện khi phiên khách đã khởi động thành công."""
        self._session_info = info
        pixmap = qr_matrix_to_pixmap(info.qr_matrix, module_size=6, border=3)
        self.qr_label.setPixmap(pixmap)
        self.url_label.setText(info.url)
        self.copy_btn.setText(QCoreApplication.translate("WebGuest", "Sao chép"))
        self.subtitle.setText(
            QCoreApplication.translate(
                "WebGuest",
                "Mở máy ảnh điện thoại quét mã bên dưới hoặc truy cập liên kết trong trình duyệt.",
            )
        )
        self.subtitle.setProperty("tone", "muted")
        self.subtitle.style().unpolish(self.subtitle)
        self.subtitle.style().polish(self.subtitle)
        self._update_shared_count()

    def _on_session_failed(self, err: str) -> None:
        self.subtitle.setText(
            QCoreApplication.translate("WebGuest", "Không thể khởi động phiên khách: ") + err
        )
        self.subtitle.setProperty("tone", "danger")
        self.subtitle.style().unpolish(self.subtitle)
        self.subtitle.style().polish(self.subtitle)

    def _on_session_ended(self) -> None:
        self.subtitle.setText(QCoreApplication.translate("WebGuest", "Phiên khách đã kết thúc hoặc hết hạn."))
        self.subtitle.setProperty("tone", "muted")
        self.subtitle.style().unpolish(self.subtitle)
        self.subtitle.style().polish(self.subtitle)

    def _update_shared_count(self) -> None:
        count = len(self.initial_files)
        if count == 0:
            self.shared_info_label.setText(
                QCoreApplication.translate("WebGuest", "Chưa có tệp nào chia sẻ sẵn cho điện thoại.")
            )
        else:
            self.shared_info_label.setText(
                QCoreApplication.translate("WebGuest", "Đang chia sẻ sẵn %n tệp cho điện thoại.", None, count)
            )

    def _copy_url(self) -> None:
        if self._session_info:
            cb = QGuiApplication.clipboard()
            if cb:
                cb.setText(self._session_info.url)
            self.copy_btn.setText(QCoreApplication.translate("WebGuest", "✓ Đã chép"))

    def _send_text(self) -> None:
        text = self.text_input.text().strip()
        if not text:
            return
        if self.host is not None:

            def _send(node: Node, msg: str = text) -> None:
                if node._guest_manager:
                    node._guest_manager.send_text(msg)

            self.host.call(_send)
            self.text_input.clear()
        elif self.node is not None and self.node._guest_manager is not None:
            self.node._guest_manager.send_text(text)
            self.text_input.clear()

    def _on_text_received(self, text: str) -> None:
        self._last_received_text = text
        self.received_text_label.setText(
            QCoreApplication.translate("WebGuest", "Từ điện thoại: {text}").format(text=text)
        )
        self.copy_text_btn.setText(QCoreApplication.translate("WebGuest", "Chép chữ"))
        self.received_text_container.setVisible(True)

    def _copy_received_text(self) -> None:
        if not self._last_received_text:
            return
        cb = QGuiApplication.clipboard()
        if cb:
            cb.setText(self._last_received_text)
        self.copy_text_btn.setText(QCoreApplication.translate("WebGuest", "✓ Đã chép"))

    def _toggle_wifi_panel(self, checked: bool) -> None:
        self.wifi_panel.setVisible(checked)

    def _generate_wifi_qr(self) -> None:
        ssid = self.wifi_ssid_input.text().strip()
        password = self.wifi_password_input.text()
        security = "WPA" if password else "nopass"
        try:
            matrix = encode_wifi_qr(ssid, password, security=security)
        except ValueError:
            self.wifi_qr_label.setPixmap(QPixmap())
            self.wifi_qr_label.setProperty("tone", "danger")
            self.wifi_qr_label.style().unpolish(self.wifi_qr_label)
            self.wifi_qr_label.style().polish(self.wifi_qr_label)
            self.wifi_qr_label.setText(
                QCoreApplication.translate("WebGuest", "Tên mạng hoặc mật khẩu Wi-Fi không hợp lệ.")
            )
            return
        self.wifi_qr_label.setProperty("tone", None)
        self.wifi_qr_label.style().unpolish(self.wifi_qr_label)
        self.wifi_qr_label.style().polish(self.wifi_qr_label)
        self.wifi_qr_label.setText("")
        self.wifi_qr_label.setPixmap(qr_matrix_to_pixmap(matrix, module_size=4, border=2))

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
        msg = QCoreApplication.translate("WebGuest", "Đang nhận {filename}: {percent}% {size_info}").format(
            filename=filename, percent=percent, size_info=size_info
        )
        self.progress_label.setText(msg)

    def _on_upload_completed_ui(self, filename: str, size: int, _path: str) -> None:
        self.progress_container.setVisible(True)
        self.progress_bar.setValue(100)
        msg = QCoreApplication.translate("WebGuest", "✓ Đã nhận thành công: {filename} ({size_str})").format(
            filename=filename, size_str=format_size(size)
        )
        self.progress_label.setText(msg)

    def closeEvent(self, event: object) -> None:
        self._stop_host_session()
        super().closeEvent(event)  # type: ignore[arg-type]
