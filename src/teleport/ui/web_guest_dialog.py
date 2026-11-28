"""Hộp thoại Chế độ khách qua Web QR (Web Guest Mode Dialog) cho PySide6/Qt (v1.2.0).

Hiển thị mã QR trực quan, liên kết kết nối mạng nội bộ và tiến độ tải tệp với điện thoại.
"""

from __future__ import annotations

import contextlib
import functools
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, QEvent, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QDesktopServices,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QGuiApplication,
    QImage,
    QPainter,
    QPixmap,
)
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
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from teleport.app.web_guest import GuestSessionInfo, WebGuestManager
from teleport.core.display import format_size
from teleport.core.qr import encode_wifi_qr
from teleport.net.web_guest import GuestScope
from teleport.paths import default_receive_dir
from teleport.ui import motion, shell, sounds, texts, theme
from teleport.ui.buttons import AnimatedButton, CopiedFeedback
from teleport.ui.chat_bubbles import BubbleBar
from teleport.ui.drop_overlay import DropOverlay
from teleport.ui.illustrations import Illustration, Scene
from teleport.ui.sounds import SoundCue
from teleport.ui.theme import tokens
from teleport.ui.widgets import (
    BadgeTone,
    InfoBar,
    InfoLevel,
    PageHeader,
    StatusBadge,
    ToggleSwitch,
    safe_tooltip,
)

if TYPE_CHECKING:
    from teleport.app.node import Node
    from teleport.ui.bridge import NodeHost

log = logging.getLogger(__name__)
file_dialog_factory: Callable[[QWidget | None], QFileDialog] = QFileDialog


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
        motion.dim_parent_on_show(self)
        self.node = node
        self.host = host
        self.initial_files = list(initial_files or [])
        self._session_info: GuestSessionInfo | None = None
        self._last_received_text: str = ""
        self._stopped_host = False

        self.setWindowTitle(QCoreApplication.translate("WebGuest", "Chuyển tệp với điện thoại"))
        self.setMinimumWidth(520)
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
        title.setFont(theme.font_for(theme.TextStyle.TITLE, title.font()))
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


_CONTENT_MAX_WIDTH = 720


def _plain_label(text: str = "", parent: QWidget | None = None, *, object_name: str = "") -> QLabel:
    label = QLabel(text, parent)
    if object_name:
        label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _safe_path_size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    return 0


class WebGuestPage(QWidget):
    """Trang Chế độ khách không chặn (`Page.WEB_GUEST`): giữ phiên khi quay về HOME, hỗ trợ
    kéo thả tệp/thư mục chia sẻ, mở nhanh tệp nhận và trao đổi văn bản 2 chiều."""

    upload_progress = Signal(str, int, int)  # name, current, total
    upload_completed = Signal(str, int, str)  # name, size, path
    session_started = Signal(object, object)  # GuestSessionInfo, list[tuple[str, Path, bool]]
    session_failed = Signal(str)  # error text
    session_ended = Signal()
    text_received = Signal(str)
    clients_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("webGuestPage")
        self.setAcceptDrops(True)

        self._host: NodeHost | None = None
        self._session_info: GuestSessionInfo | None = None
        self._session_seq = 0
        self._session_active = False
        self._connected_clients = 0
        self._shared_paths: list[Path] = []
        self._shared_ids: dict[Path, str] = {}
        self._received_entries: list[tuple[str, int, Path]] = []
        self._text_entries: list[tuple[bool, str]] = []
        self._dialogs: list[QFileDialog] = []
        self._progress_state: tuple[str, int, int] | None = None
        self._progress_done: tuple[str, int] | None = None
        self._progress_display = 0.0

        self.header = PageHeader("", self)
        self.info_bar = InfoBar(self)
        self.info_bar.setObjectName("webGuestInfo")

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("webGuestScroll")
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        body_wrap = QWidget()
        body_wrap.setObjectName("webGuestBody")
        outer_center = QHBoxLayout(body_wrap)
        outer_center.setContentsMargins(
            theme.SPACING[3], theme.SPACING[2], theme.SPACING[3], theme.SPACING[3]
        )
        outer_center.addStretch(1)

        content = QWidget(body_wrap)
        content.setMaximumWidth(_CONTENT_MAX_WIDTH)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(theme.SPACING[3])
        outer_center.addWidget(content, 1000)
        outer_center.addStretch(1)
        self.scroll_area.setWidget(body_wrap)

        # ---- Thẻ 1: Phiên kết nối & Mã QR -----------------------------------------------------
        self.session_card = QFrame(content)
        self.session_card.setObjectName("webGuestSessionCard")
        self.session_card.setProperty(theme.SURFACE_PROPERTY, "card")
        sess_layout = QVBoxLayout(self.session_card)
        sess_layout.setContentsMargins(theme.SPACING[3], theme.SPACING[3], theme.SPACING[3], theme.SPACING[3])
        sess_layout.setSpacing(theme.SPACING[2])

        self.subtitle = _plain_label("", self.session_card, object_name="webGuestSubtitle")
        self.subtitle.setProperty(theme.TONE_PROPERTY, "muted")
        self.status_badge = StatusBadge(self.session_card)
        self.status_badge.setObjectName("webGuestStatusBadge")
        sess_layout.addWidget(self.status_badge)
        sess_layout.addWidget(self.subtitle)

        opts_row = QHBoxLayout()
        opts_row.setSpacing(theme.SPACING[2])
        self.scope_label = _plain_label("", self.session_card)
        self.scope_label.setProperty(theme.TONE_PROPERTY, "muted")
        opts_row.addWidget(self.scope_label)

        self.scope_combo = QComboBox(self.session_card)
        self.scope_combo.setObjectName("webGuestScopeCombo")
        self.scope_combo.addItem("", GuestScope.BOTH)
        self.scope_combo.addItem("", GuestScope.RECEIVE_ONLY)
        self.scope_combo.addItem("", GuestScope.SEND_ONLY)
        self.scope_combo.currentIndexChanged.connect(self._on_options_changed)
        opts_row.addWidget(self.scope_combo, 1)

        self.one_shot_label = _plain_label("", self.session_card)
        self.one_shot_label.setProperty(theme.TONE_PROPERTY, "muted")
        self.one_shot_check = ToggleSwitch(self.session_card)
        self.one_shot_check.setObjectName("webGuestOneShotCheck")
        self.one_shot_check.toggled.connect(self._on_options_changed)
        opts_row.addWidget(self.one_shot_label)
        opts_row.addWidget(self.one_shot_check)
        sess_layout.addLayout(opts_row)

        qr_row = QHBoxLayout()
        qr_row.setSpacing(theme.SPACING[3])
        self.qr_frame = QFrame(self.session_card)
        self.qr_frame.setProperty(theme.SURFACE_PROPERTY, "raised")
        qr_frame_box = QVBoxLayout(self.qr_frame)
        qr_frame_box.setContentsMargins(
            theme.SPACING[2], theme.SPACING[2], theme.SPACING[2], theme.SPACING[2]
        )
        self.qr_label = QLabel(self.qr_frame)
        self.qr_label.setObjectName("webGuestQrLabel")
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setFixedSize(180, 180)
        self.idle_illustration = Illustration(Scene.WEB_GUEST, self.qr_frame)
        self.idle_illustration.setObjectName("webGuestIdleIllustration")
        qr_frame_box.addWidget(self.qr_label, 0, Qt.AlignmentFlag.AlignCenter)
        qr_frame_box.addWidget(self.idle_illustration, 0, Qt.AlignmentFlag.AlignCenter)
        qr_row.addWidget(self.qr_frame, 0, Qt.AlignmentFlag.AlignTop)

        qr_right = QVBoxLayout()
        qr_right.setSpacing(theme.SPACING[2])
        self.url_label = _plain_label("", self.session_card, object_name="webGuestUrlLabel")
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        url_font = self.url_label.font()
        url_font.setFamilies(["monospace", "Consolas", "Courier New"])
        self.url_label.setFont(url_font)
        qr_right.addWidget(self.url_label)

        action_btns = QHBoxLayout()
        action_btns.setSpacing(theme.SPACING[1])
        self.copy_btn = AnimatedButton("", self.session_card)
        self.copy_btn.setObjectName("webGuestCopyUrlBtn")
        self.copy_btn.clicked.connect(self._copy_url)
        self.copy_feedback = CopiedFeedback(self.copy_btn)
        self.copy_feedback.active_changed.connect(self._render_copy_button)
        action_btns.addWidget(self.copy_btn)

        self.wifi_qr_btn = AnimatedButton("", self.session_card)
        self.wifi_qr_btn.setObjectName("webGuestWifiBtn")
        self.wifi_qr_btn.setCheckable(True)
        self.wifi_qr_btn.toggled.connect(self._toggle_wifi_panel)
        action_btns.addWidget(self.wifi_qr_btn)

        self.session_toggle_btn = AnimatedButton("", self.session_card)
        self.session_toggle_btn.setObjectName("webGuestSessionToggleBtn")
        self.session_toggle_btn.clicked.connect(self._toggle_session)
        action_btns.addWidget(self.session_toggle_btn)
        action_btns.addStretch(1)
        qr_right.addLayout(action_btns)
        qr_right.addStretch(1)
        qr_row.addLayout(qr_right, 1)
        sess_layout.addLayout(qr_row)

        self.wifi_panel = QFrame(self.session_card)
        self.wifi_panel.setObjectName("webGuestWifiPanel")
        self.wifi_panel.setProperty(theme.SURFACE_PROPERTY, "raised")
        wifi_layout = QVBoxLayout(self.wifi_panel)
        wifi_layout.setContentsMargins(theme.SPACING[2], theme.SPACING[2], theme.SPACING[2], theme.SPACING[2])
        wifi_layout.setSpacing(theme.SPACING[1])
        wifi_row = QHBoxLayout()
        self.wifi_ssid_input = QLineEdit(self.wifi_panel)
        self.wifi_ssid_input.setObjectName("webGuestWifiSsid")
        self.wifi_password_input = QLineEdit(self.wifi_panel)
        self.wifi_password_input.setObjectName("webGuestWifiPassword")
        self.wifi_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.wifi_generate_btn = AnimatedButton("", self.wifi_panel)
        self.wifi_generate_btn.setObjectName("webGuestWifiGenerate")
        self.wifi_generate_btn.clicked.connect(self._generate_wifi_qr)
        wifi_row.addWidget(self.wifi_ssid_input, 1)
        wifi_row.addWidget(self.wifi_password_input, 1)
        wifi_row.addWidget(self.wifi_generate_btn)
        wifi_layout.addLayout(wifi_row)
        self.wifi_qr_label = _plain_label("", self.wifi_panel, object_name="webGuestWifiQr")
        self.wifi_qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wifi_layout.addWidget(self.wifi_qr_label)
        self.wifi_panel.setVisible(False)
        self._wifi_anim = motion.HeightAnimator(self.wifi_panel)
        sess_layout.addWidget(self.wifi_panel)
        col.addWidget(self.session_card)

        # ---- Thẻ 2: Tệp & thư mục chia sẻ cho điện thoại -------------------------------------
        self.shared_card = QFrame(content)
        self.shared_card.setObjectName("webGuestSharedCard")
        self.shared_card.setProperty(theme.SURFACE_PROPERTY, "card")
        shared_layout = QVBoxLayout(self.shared_card)
        shared_layout.setContentsMargins(
            theme.SPACING[3], theme.SPACING[3], theme.SPACING[3], theme.SPACING[3]
        )
        shared_layout.setSpacing(theme.SPACING[2])

        shared_head = QHBoxLayout()
        self.shared_title_label = _plain_label("", self.shared_card)
        shared_font = theme.font_for(theme.TextStyle.SECTION, self.shared_title_label.font())
        shared_font.setBold(True)
        self.shared_title_label.setFont(shared_font)
        shared_head.addWidget(self.shared_title_label, 1)

        self.add_file_btn = AnimatedButton("", self.shared_card, primary=True)
        self.add_file_btn.setObjectName("webGuestAddFileBtn")
        self.add_file_btn.clicked.connect(self._choose_files)
        self.add_folder_btn = AnimatedButton("", self.shared_card)
        self.add_folder_btn.setObjectName("webGuestAddFolderBtn")
        self.add_folder_btn.clicked.connect(self._choose_folder)
        shared_head.addWidget(self.add_file_btn)
        shared_head.addWidget(self.add_folder_btn)
        shared_layout.addLayout(shared_head)

        self.shared_hint_label = _plain_label("", self.shared_card)
        self.shared_hint_label.setProperty(theme.TONE_PROPERTY, "muted")
        shared_layout.addWidget(self.shared_hint_label)

        self.shared_list_widget = QWidget(self.shared_card)
        self.shared_items_layout = QVBoxLayout(self.shared_list_widget)
        self.shared_items_layout.setContentsMargins(0, 0, 0, 0)
        self.shared_items_layout.setSpacing(theme.SPACING[1])
        shared_layout.addWidget(self.shared_list_widget)
        col.addWidget(self.shared_card)

        # ---- Thẻ 3: Tệp nhận từ điện thoại --------------------------------------------------
        self.received_card = QFrame(content)
        self.received_card.setObjectName("webGuestReceivedCard")
        self.received_card.setProperty(theme.SURFACE_PROPERTY, "card")
        recv_layout = QVBoxLayout(self.received_card)
        recv_layout.setContentsMargins(theme.SPACING[3], theme.SPACING[3], theme.SPACING[3], theme.SPACING[3])
        recv_layout.setSpacing(theme.SPACING[2])

        self.received_title_label = _plain_label("", self.received_card)
        self.received_title_label.setFont(shared_font)
        recv_layout.addWidget(self.received_title_label)

        self.progress_container = QWidget(self.received_card)
        prog_box = QVBoxLayout(self.progress_container)
        prog_box.setContentsMargins(0, 0, 0, 0)
        prog_box.setSpacing(4)
        self.progress_label = _plain_label("", self.progress_container)
        self.progress_label.setProperty(theme.TONE_PROPERTY, "muted")
        self.progress_bar = BubbleBar(self.progress_container)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.display_fraction = lambda: self._progress_display
        prog_box.addWidget(self.progress_label)
        prog_box.addWidget(self.progress_bar)
        self.progress_container.setVisible(False)
        recv_layout.addWidget(self.progress_container)

        self.received_empty_label = _plain_label("", self.received_card)
        self.received_empty_label.setProperty(theme.TONE_PROPERTY, "muted")
        recv_layout.addWidget(self.received_empty_label)

        self.received_list_widget = QWidget(self.received_card)
        self.received_items_layout = QVBoxLayout(self.received_list_widget)
        self.received_items_layout.setContentsMargins(0, 0, 0, 0)
        self.received_items_layout.setSpacing(theme.SPACING[1])
        recv_layout.addWidget(self.received_list_widget)
        col.addWidget(self.received_card)

        # ---- Thẻ 4: Văn bản / liên kết 2 chiều ------------------------------------------------
        self.text_card = QFrame(content)
        self.text_card.setObjectName("webGuestTextCard")
        self.text_card.setProperty(theme.SURFACE_PROPERTY, "card")
        txt_layout = QVBoxLayout(self.text_card)
        txt_layout.setContentsMargins(theme.SPACING[3], theme.SPACING[3], theme.SPACING[3], theme.SPACING[3])
        txt_layout.setSpacing(theme.SPACING[2])

        self.text_title_label = _plain_label("", self.text_card)
        self.text_title_label.setFont(shared_font)
        txt_layout.addWidget(self.text_title_label)

        txt_row = QHBoxLayout()
        self.text_input = QLineEdit(self.text_card)
        self.text_input.setObjectName("webGuestTextInput")
        self.text_input.returnPressed.connect(self._send_text)
        self.send_text_btn = AnimatedButton("", self.text_card, primary=True)
        self.send_text_btn.setObjectName("webGuestSendTextBtn")
        self.send_text_btn.clicked.connect(self._send_text)
        txt_row.addWidget(self.text_input, 1)
        txt_row.addWidget(self.send_text_btn)
        txt_layout.addLayout(txt_row)

        self.text_empty_label = _plain_label("", self.text_card)
        self.text_empty_label.setProperty(theme.TONE_PROPERTY, "muted")
        txt_layout.addWidget(self.text_empty_label)

        self.text_list_widget = QWidget(self.text_card)
        self.text_items_layout = QVBoxLayout(self.text_list_widget)
        self.text_items_layout.setContentsMargins(0, 0, 0, 0)
        self.text_items_layout.setSpacing(theme.SPACING[1])
        txt_layout.addWidget(self.text_list_widget)
        col.addWidget(self.text_card)
        col.addStretch(1)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.header)
        root.addWidget(self.info_bar)
        root.addWidget(self.scroll_area, 1)

        self._drop_overlay = DropOverlay(self)

        self.setFocusProxy(self.header.back_button)
        self.upload_progress.connect(self._on_upload_progress)
        self.upload_completed.connect(self._on_upload_completed)
        self.session_started.connect(self._on_session_started)
        self.session_failed.connect(self._on_session_failed)
        self.session_ended.connect(self._on_session_ended)
        self.text_received.connect(self._on_text_received)
        self.clients_changed.connect(self._on_clients_changed)

        self.retranslate()

    # ---- Điều phối vòng đời phiên ---------------------------------------------------------------

    @property
    def session_active(self) -> bool:
        return self._session_active

    @property
    def session_info(self) -> GuestSessionInfo | None:
        return self._session_info

    @property
    def shared_paths(self) -> tuple[Path, ...]:
        return tuple(self._shared_paths)

    @property
    def received_entries(self) -> tuple[tuple[str, int, Path], ...]:
        return tuple(self._received_entries)

    @property
    def text_entries(self) -> tuple[tuple[bool, str], ...]:
        return tuple(self._text_entries)

    def attach_host(self, host: NodeHost | None) -> None:
        """Gắn `NodeHost` mới khi node khởi động lại."""
        if self._host is host:
            return
        self._session_seq += 1
        self._host = host
        self._session_active = False
        self._session_info = None
        self._connected_clients = 0
        self._shared_ids.clear()
        self.qr_label.setPixmap(QPixmap())
        self.url_label.setText("")
        self._sync_session_controls()

    def ensure_started(self, initial_paths: Sequence[Path] = ()) -> None:
        """Bảo đảm phiên đang chạy; thêm `initial_paths` vào danh sách chia sẻ."""
        if initial_paths:
            self.add_shared_paths(initial_paths)
        if not self._session_active and self._host is not None:
            self._start_host_session()

    def add_shared_paths(self, paths: Sequence[Path]) -> int:
        """Thêm các tệp hoặc thư mục hợp lệ vào danh sách chia sẻ; trả số mục mới thêm."""
        added: list[Path] = []
        for raw in paths:
            p = Path(raw)
            if p in self._shared_paths:
                continue
            try:
                ok = p.is_file() or p.is_dir()
            except OSError:
                ok = False
            if not ok:
                continue
            self._shared_paths.append(p)
            added.append(p)
        if not added:
            return 0
        self._rebuild_shared_list()
        if self._session_active and self._host is not None:
            host = self._host
            seq = self._session_seq
            for p in added:

                def _add(node: Node, file_path: Path = p) -> str | None:
                    if node._guest_manager is None:
                        return None
                    return node._guest_manager.add_file_to_share(file_path)

                host.call(
                    _add,
                    functools.partial(self._on_remote_file_added, host, seq, p),
                )
        return len(added)

    def remove_shared_path(self, path: Path) -> None:
        """Gỡ một tệp/thư mục khỏi danh sách chia sẻ (cả máy chủ lẫn UI)."""
        if path not in self._shared_paths:
            return
        self._shared_paths.remove(path)
        fid = self._shared_ids.pop(path, None)
        if fid and self._host is not None and self._session_active:

            def _remove(node: Node, file_id: str = fid) -> None:
                if node._guest_manager is not None:
                    node._guest_manager.remove_shared_file(file_id)

            self._host.call(_remove)
        self._rebuild_shared_list()

    def stop_session(self) -> None:
        """Dừng phiên Web khách trên host hiện tại và cập nhật giao diện."""
        self._session_seq += 1
        was_active = self._session_active
        self._session_active = False
        self._session_info = None
        self._connected_clients = 0
        self._shared_ids.clear()
        self.qr_label.setPixmap(QPixmap())
        self.url_label.setText("")
        self._sync_session_controls()
        host = self._host
        if was_active and host is not None:

            async def _async_stop(node: Node) -> None:
                await node.stop_guest_session()

            with contextlib.suppress(Exception):
                host.submit(_async_stop)

    def close_dialogs(self) -> None:
        """Đóng mọi hộp chọn tệp/thư mục không chặn đang mở."""
        dialogs, self._dialogs = self._dialogs, []
        for dialog in dialogs:
            if isValid(dialog):
                dialog.close()
                dialog.deleteLater()

    # ---- Drag & Drop ---------------------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime = event.mimeData()
        if mime is not None and mime.hasUrls() and any(u.isLocalFile() for u in mime.urls()):
            self._drop_overlay.show_text(
                QCoreApplication.translate("WebGuest", "Thả tệp hoặc thư mục để chia sẻ cho điện thoại")
            )
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._drop_overlay.dismiss()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._drop_overlay.dismiss()
        mime = event.mimeData()
        if mime is not None and mime.hasUrls():
            local_paths = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]
            if local_paths:
                if self.add_shared_paths(local_paths) > 0:
                    sounds.play(SoundCue.DROP_LOCK)
                event.acceptProposedAction()
                return
        super().dropEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()

    # ---- Bên trong -----------------------------------------------------------------------------

    def _selected_scope(self) -> GuestScope:
        data = self.scope_combo.currentData()
        return data if isinstance(data, GuestScope) else GuestScope.BOTH

    def _on_options_changed(self) -> None:
        if self._host is None or not self._session_active:
            return
        t = QCoreApplication.translate
        self.info_bar.show_message(
            InfoLevel.WARNING,
            t("WebGuest", "Đã đổi quyền phiên — mã QR mới đã được tạo, mã cũ không còn hiệu lực."),
        )
        self._start_host_session()

    def _toggle_session(self) -> None:
        if self._session_active:
            self.stop_session()
        else:
            self.info_bar.clear()
            self._start_host_session()

    def _start_host_session(self) -> None:
        host = self._host
        if host is None:
            return
        self._session_seq += 1
        seq = self._session_seq
        scope = self._selected_scope()
        one_shot = self.one_shot_check.isChecked()
        shared_snapshot = list(self._shared_paths)

        def on_prog(name: str, cur: int, tot: int) -> None:
            if isValid(self):
                self.upload_progress.emit(name, cur, tot)

        def on_comp(name: str, size: int, path: Path) -> None:
            if isValid(self):
                self.upload_completed.emit(name, size, str(path))

        def on_text(text: str) -> None:
            if isValid(self):
                self.text_received.emit(text)

        def on_timeout() -> None:
            if isValid(self):
                self.session_ended.emit()

        def on_clients(count: int) -> None:
            if isValid(self):
                self.clients_changed.emit(count)

        async def _async_start(node: Node) -> tuple[GuestSessionInfo, list[tuple[str, Path, bool]]]:
            await node.stop_guest_session()
            recv_dir = node.transfers.receive_dir or default_receive_dir()
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
                on_clients_changed=on_clients,
            )
            info = await node._guest_manager.start_session(
                shared_files=shared_snapshot,
                scope=scope,
                one_shot=one_shot,
            )
            mapped = [
                (fid, path, is_dir)
                for fid, _name, path, _size, is_dir in node._guest_manager.shared_files_snapshot()
            ]
            return info, mapped

        def _on_done(result: tuple[GuestSessionInfo, list[tuple[str, Path, bool]]] | BaseException) -> None:
            if not isValid(self) or seq != self._session_seq or host is not self._host:
                return
            if isinstance(result, BaseException):
                self.session_failed.emit(str(result))
                return
            info, mapped = result
            self.session_started.emit(info, mapped)

        self._connected_clients = 0
        self._session_active = True
        self._sync_session_controls()
        host.submit(_async_start, _on_done)

    def _on_clients_changed(self, count: int) -> None:
        previous = self._connected_clients
        self._connected_clients = max(0, int(count))
        if previous == 0 and self._connected_clients >= 1:
            sounds.play(SoundCue.GUEST_CONNECTED)
        self._sync_session_controls()

    def _on_remote_file_added(self, host: NodeHost, seq: int, path: Path, result: object) -> None:
        if not isValid(self) or seq != self._session_seq or host is not self._host:
            return
        if isinstance(result, str) and result:
            self._shared_ids[path] = result

    def _on_session_started(self, info_obj: object, mapped_obj: object) -> None:
        if not isinstance(info_obj, GuestSessionInfo):
            return
        self._session_active = True
        self._session_info = info_obj
        self._shared_ids.clear()
        if isinstance(mapped_obj, list):
            for entry in mapped_obj:
                if isinstance(entry, tuple) and len(entry) >= 2:
                    fid, p = entry[0], entry[1]
                    if isinstance(fid, str) and isinstance(p, Path):
                        self._shared_ids[p] = fid
        self.qr_label.setPixmap(qr_matrix_to_pixmap(info_obj.qr_matrix, module_size=5, border=2))
        self.url_label.setText(info_obj.url)
        self.copy_feedback.cancel()
        self._sync_session_controls()

    def _on_session_failed(self, err: str) -> None:
        self._session_active = False
        self._session_info = None
        self._connected_clients = 0
        self._sync_session_controls()
        self.info_bar.show_message(
            InfoLevel.ERROR,
            QCoreApplication.translate("WebGuest", "Không thể khởi động phiên khách: ") + err,
        )

    def _on_session_ended(self) -> None:
        self._session_active = False
        self._session_info = None
        self._connected_clients = 0
        self.qr_label.setPixmap(QPixmap())
        self.url_label.setText("")
        self._sync_session_controls()

    def _copy_url(self) -> None:
        if not self._session_info:
            return
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(self._session_info.url)
        self.copy_feedback.trigger()

    def _render_copy_button(self, *_args: object) -> None:
        t = QCoreApplication.translate
        label = t("WebGuest", "Đã chép") if self.copy_feedback.active else t("WebGuest", "Sao chép")
        self.copy_btn.setText(label)
        self.copy_btn.setAccessibleName(label)

    def _toggle_wifi_panel(self, checked: bool) -> None:
        if checked:
            self._wifi_anim.expand()
        else:
            self._wifi_anim.collapse()

    def _generate_wifi_qr(self) -> None:
        ssid = self.wifi_ssid_input.text().strip()
        password = self.wifi_password_input.text()
        security = "WPA" if password else "nopass"
        try:
            matrix = encode_wifi_qr(ssid, password, security=security)
        except ValueError:
            self.wifi_qr_label.setPixmap(QPixmap())
            self.wifi_qr_label.setProperty(theme.TONE_PROPERTY, "danger")
            self.wifi_qr_label.style().unpolish(self.wifi_qr_label)
            self.wifi_qr_label.style().polish(self.wifi_qr_label)
            self.wifi_qr_label.setText(
                QCoreApplication.translate("WebGuest", "Tên mạng hoặc mật khẩu Wi-Fi không hợp lệ.")
            )
            return
        self.wifi_qr_label.setProperty(theme.TONE_PROPERTY, None)
        self.wifi_qr_label.style().unpolish(self.wifi_qr_label)
        self.wifi_qr_label.style().polish(self.wifi_qr_label)
        self.wifi_qr_label.setText("")
        self.wifi_qr_label.setPixmap(qr_matrix_to_pixmap(matrix, module_size=4, border=2))

    def _choose_files(self) -> None:
        t = QCoreApplication.translate
        dialog = file_dialog_factory(self)
        dialog.setObjectName("webGuestPickFilesDialog")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setWindowTitle(t("WebGuest", "Chọn tệp chia sẻ cho điện thoại"))
        dialog.filesSelected.connect(lambda paths: self.add_shared_paths([Path(p) for p in paths]))
        dialog.finished.connect(functools.partial(self._on_dialog_finished, dialog))
        self._dialogs.append(dialog)
        dialog.open()

    def _choose_folder(self) -> None:
        t = QCoreApplication.translate
        dialog = file_dialog_factory(self)
        dialog.setObjectName("webGuestPickFolderDialog")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        dialog.setWindowTitle(t("WebGuest", "Chọn thư mục chia sẻ cho điện thoại"))
        dialog.fileSelected.connect(lambda p: self.add_shared_paths([Path(p)]) if p else None)
        dialog.finished.connect(functools.partial(self._on_dialog_finished, dialog))
        self._dialogs.append(dialog)
        dialog.open()

    def _on_dialog_finished(self, dialog: QFileDialog, _result: int) -> None:
        if dialog in self._dialogs:
            self._dialogs.remove(dialog)
        if isValid(dialog):
            dialog.deleteLater()

    def _set_progress_display(self, value: object) -> None:
        try:
            frac = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            frac = self.progress_bar.value() / 100.0
        self._progress_display = max(0.0, min(1.0, frac))
        self.progress_bar.update()

    def _on_upload_progress(self, filename: str, current: int, total: int) -> None:
        self._progress_state = (filename, current, total)
        self._progress_done = None
        self._render_progress()

    def _on_upload_completed(self, filename: str, size: int, path_str: str) -> None:
        self._progress_state = None
        self._progress_done = (filename, size)
        self._render_progress()
        sounds.play(SoundCue.TRANSFER_DONE)
        self._received_entries.insert(0, (filename, size, Path(path_str)))
        self._rebuild_received_list()

    def _render_progress(self) -> None:
        t = QCoreApplication.translate
        if self._progress_state is not None:
            filename, current, total = self._progress_state
            percent = int((current / total) * 100) if total > 0 else 0
            self.progress_bar.setValue(percent)
            motion.animate_value(
                self.progress_bar,
                self._progress_display,
                percent / 100.0,
                motion.FAST,
                self._set_progress_display,
                key="web_guest_bar",
            )
            size_info = f"({format_size(current)}/{format_size(total)})"
            self.progress_label.setText(
                texts.fill(
                    t("WebGuest", "Đang nhận {filename}: {percent}% {size_info}"),
                    filename=filename,
                    percent=str(percent),
                    size_info=size_info,
                )
            )
            self.progress_container.setVisible(True)
        elif self._progress_done is not None:
            filename, size = self._progress_done
            self.progress_bar.setValue(100)
            motion.animate_value(
                self.progress_bar,
                self._progress_display,
                1.0,
                motion.FAST,
                self._set_progress_display,
                key="web_guest_bar",
            )
            self.progress_label.setText(
                texts.fill(
                    t("WebGuest", "Đã nhận xong: {filename} ({size_str})"),
                    filename=filename,
                    size_str=format_size(size),
                )
            )
            self.progress_container.setVisible(True)
        else:
            self._progress_display = 0.0
            self.progress_container.setVisible(False)

    def _send_text(self) -> None:
        text = self.text_input.text().strip()
        if not text or self._host is None or not self._session_active:
            return

        def _send(node: Node, msg: str = text) -> None:
            if node._guest_manager is not None:
                node._guest_manager.send_text(msg)

        self._host.call(_send)
        self.text_input.clear()
        self._text_entries.insert(0, (True, text))
        self._rebuild_text_list()

    def _on_text_received(self, text: str) -> None:
        self._text_entries.insert(0, (False, text))
        self._rebuild_text_list()

    # ---- Dựng lại danh sách giao diện ----------------------------------------------------------

    @staticmethod
    def _clear_layout(layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _rebuild_shared_list(self) -> None:
        t = QCoreApplication.translate
        self._clear_layout(self.shared_items_layout)
        count = len(self._shared_paths)
        if count == 0:
            self.shared_hint_label.setText(
                t("WebGuest", "Kéo thả tệp hoặc thư mục vào đây để chia sẻ ngay cho điện thoại.")
            )
            self.shared_list_widget.setVisible(False)
            return
        self.shared_hint_label.setText(t("WebGuest", "Đang chia sẻ sẵn %n tệp cho điện thoại.", None, count))
        self.shared_list_widget.setVisible(True)
        for p in self._shared_paths:
            row = QFrame(self.shared_list_widget)
            row.setObjectName("webGuestSharedRow")
            row.setProperty(theme.SURFACE_PROPERTY, "raised")
            box = QHBoxLayout(row)
            box.setContentsMargins(theme.SPACING[2], theme.SPACING[1], theme.SPACING[2], theme.SPACING[1])
            box.setSpacing(theme.SPACING[2])
            meta = t("WebGuest", "Thư mục") if p.is_dir() else format_size(_safe_path_size(p))
            label = _plain_label(f"{p.name} · {meta}", row)
            label.setToolTip(safe_tooltip(str(p)))
            box.addWidget(label, 1)
            rm_btn = QPushButton(t("WebGuest", "Bỏ"), row)
            rm_btn.setObjectName("webGuestRemoveSharedBtn")
            rm_btn.setAccessibleName(
                texts.fill(t("WebGuest", "Bỏ {name} khỏi danh sách chia sẻ"), name=p.name)
            )
            rm_btn.clicked.connect(functools.partial(self.remove_shared_path, p))
            box.addWidget(rm_btn)
            self.shared_items_layout.addWidget(row)

    def _rebuild_received_list(self) -> None:
        t = QCoreApplication.translate
        self._clear_layout(self.received_items_layout)
        if not self._received_entries:
            self.received_empty_label.setVisible(True)
            self.received_list_widget.setVisible(False)
            return
        self.received_empty_label.setVisible(False)
        self.received_list_widget.setVisible(True)
        for name, size, path in self._received_entries:
            row = QFrame(self.received_list_widget)
            row.setObjectName("webGuestReceivedRow")
            row.setProperty(theme.SURFACE_PROPERTY, "raised")
            box = QHBoxLayout(row)
            box.setContentsMargins(theme.SPACING[2], theme.SPACING[1], theme.SPACING[2], theme.SPACING[1])
            box.setSpacing(theme.SPACING[1])
            label = _plain_label(f"{name} · {format_size(size)}", row)
            label.setToolTip(safe_tooltip(str(path)))
            box.addWidget(label, 1)
            if shell.is_safe_to_open(path):
                open_btn = QPushButton(t("WebGuest", "Mở file"), row)
                open_btn.setObjectName("webGuestOpenReceivedBtn")
                open_btn.clicked.connect(functools.partial(self._open_received, path))
                box.addWidget(open_btn)
            reveal_btn = QPushButton(t("WebGuest", "Hiện trong thư mục"), row)
            reveal_btn.setObjectName("webGuestRevealReceivedBtn")
            reveal_btn.clicked.connect(functools.partial(self._reveal_received, path))
            box.addWidget(reveal_btn)
            self.received_items_layout.addWidget(row)

    @staticmethod
    def _open_received(path: Path) -> None:
        resolved = path if path.is_absolute() else path.resolve()
        shell.open_file_in_os(resolved, resolved.parent, open_url=QDesktopServices.openUrl)

    @staticmethod
    def _reveal_received(path: Path) -> None:
        resolved = path if path.is_absolute() else path.resolve()
        shell.show_in_folder(
            [resolved],
            resolved.parent,
            open_url=QDesktopServices.openUrl,
            folder=resolved.parent,
        )

    def _rebuild_text_list(self) -> None:
        t = QCoreApplication.translate
        self._clear_layout(self.text_items_layout)
        if not self._text_entries:
            self.text_empty_label.setVisible(True)
            self.text_list_widget.setVisible(False)
            return
        self.text_empty_label.setVisible(False)
        self.text_list_widget.setVisible(True)
        for is_host, text in self._text_entries:
            row = QFrame(self.text_list_widget)
            row.setObjectName("webGuestTextRow")
            row.setProperty(theme.SURFACE_PROPERTY, "raised")
            box = QHBoxLayout(row)
            box.setContentsMargins(theme.SPACING[2], theme.SPACING[1], theme.SPACING[2], theme.SPACING[1])
            box.setSpacing(theme.SPACING[2])
            prefix = t("WebGuest", "Máy tính") if is_host else t("WebGuest", "Điện thoại")
            label = _plain_label(f"{prefix}: {text}", row)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            box.addWidget(label, 1)
            copy_btn = QPushButton(t("WebGuest", "Chép chữ"), row)
            copy_btn.setObjectName("webGuestCopyTextItemBtn")
            fb = CopiedFeedback(copy_btn)
            fb.active_changed.connect(
                lambda active, btn=copy_btn: btn.setText(
                    t("WebGuest", "Đã chép") if active else t("WebGuest", "Chép chữ")
                )
            )
            copy_btn.clicked.connect(functools.partial(self._copy_text_entry, text, fb))
            box.addWidget(copy_btn)
            self.text_items_layout.addWidget(row)

    @staticmethod
    def _copy_text_entry(text: str, fb: CopiedFeedback) -> None:
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(text)
        fb.trigger()

    def _sync_session_controls(self) -> None:
        t = QCoreApplication.translate
        has_qr = self._session_info is not None
        self.qr_label.setVisible(has_qr)
        self.idle_illustration.setVisible(not has_qr)
        if self._session_active:
            self.session_toggle_btn.setText(t("WebGuest", "Dừng phiên"))
            self.subtitle.setText(
                t(
                    "WebGuest",
                    "Phiên vẫn giữ chạy ngay cả khi bạn quay lại trang chính. Đưa điện thoại quét mã "
                    "QR hoặc mở liên kết bên dưới.",
                )
            )
            if self._connected_clients > 0:
                self.status_badge.set_status(
                    BadgeTone.SUCCESS,
                    t("WebGuest", "Đã kết nối %n điện thoại", None, self._connected_clients),
                )
            else:
                self.status_badge.set_status(
                    BadgeTone.MUTED,
                    t("WebGuest", "Đang chờ điện thoại quét mã"),
                )
        else:
            self.session_toggle_btn.setText(t("WebGuest", "Bắt đầu phiên mới"))
            self.subtitle.setText(t("WebGuest", "Phiên đã dừng. Bấm Bắt đầu phiên mới để tạo mã QR mới."))
            self.status_badge.set_status(BadgeTone.MUTED, "")
        self.copy_btn.setEnabled(has_qr)
        self.send_text_btn.setEnabled(self._session_active)

    def retranslate(self) -> None:
        t = QCoreApplication.translate
        self.header.retranslate()
        self.header.set_title(t("WebGuest", "Chuyển tệp với điện thoại"))
        self.scope_label.setText(t("WebGuest", "Quyền phiên:"))
        self.scope_combo.setAccessibleName(t("WebGuest", "Quyền phiên khách"))
        with contextlib.ExitStack():
            self.scope_combo.blockSignals(True)
            try:
                self.scope_combo.setItemText(0, t("WebGuest", "Hai chiều"))
                self.scope_combo.setItemText(1, t("WebGuest", "Chỉ nhận từ điện thoại"))
                self.scope_combo.setItemText(2, t("WebGuest", "Chỉ gửi sang điện thoại"))
            finally:
                self.scope_combo.blockSignals(False)
        one_shot_text = t("WebGuest", "Chỉ dùng 1 lượt")
        self.one_shot_label.setText(one_shot_text)
        self.one_shot_check.setAccessibleName(one_shot_text)
        self.one_shot_check.retranslate()
        self._render_copy_button()
        self.wifi_qr_btn.setText(t("WebGuest", "QR Wi-Fi…"))
        self.wifi_ssid_input.setAccessibleName(t("WebGuest", "Tên mạng Wi-Fi"))
        self.wifi_ssid_input.setPlaceholderText(t("WebGuest", "Tên mạng Wi-Fi (SSID)"))
        self.wifi_password_input.setAccessibleName(t("WebGuest", "Mật khẩu Wi-Fi"))
        self.wifi_password_input.setPlaceholderText(t("WebGuest", "Mật khẩu Wi-Fi"))
        self.wifi_generate_btn.setText(t("WebGuest", "Tạo QR"))
        self.shared_title_label.setText(t("WebGuest", "Tệp chia sẻ cho điện thoại"))
        self.add_file_btn.setText(t("WebGuest", "+ Thêm tệp…"))
        self.add_folder_btn.setText(t("WebGuest", "+ Thêm thư mục…"))
        self.received_title_label.setText(t("WebGuest", "Tệp nhận từ điện thoại"))
        self.received_empty_label.setText(t("WebGuest", "Chưa nhận tệp nào trong phiên này."))
        self.text_title_label.setText(t("WebGuest", "Văn bản và liên kết"))
        self.text_input.setAccessibleName(t("WebGuest", "Văn bản gửi sang điện thoại"))
        self.text_input.setPlaceholderText(
            t("WebGuest", "Nhập ghi chú hoặc liên kết để gửi sang điện thoại…")
        )
        self.send_text_btn.setText(t("WebGuest", "Gửi chữ"))
        self.text_empty_label.setText(t("WebGuest", "Chưa có tin nhắn văn bản nào trong phiên này."))
        self._sync_session_controls()
        self._render_progress()
        self._rebuild_shared_list()
        self._rebuild_received_list()
        self._rebuild_text_list()
