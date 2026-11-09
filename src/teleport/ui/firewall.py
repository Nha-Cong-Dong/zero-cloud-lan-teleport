"""Giao diện hướng dẫn khắc phục khi tường lửa chặn Teleport (mvp mục 8).

Cung cấp hộp thoại `FirewallGuideDialog` giải thích dễ hiểu cho người dùng cách kiểm tra mạng
và cấu hình tường lửa Windows khi kết nối bị chặn, cùng các nút thao tác mở nhanh cài đặt hệ thống.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from PySide6.QtCore import QCoreApplication, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from teleport.ui import theme

log = logging.getLogger(__name__)

# Seam cấp module để kiểm thử tự động mà không cần mở cài đặt thật
open_url: Callable[[QUrl], object] = QDesktopServices.openUrl
startfile: Callable[..., object] = os.startfile if hasattr(os, "startfile") else lambda *a, **k: None


def firewall_blocked_notice_text() -> str:
    """mvp mục 8: câu thông báo tường lửa chặn Teleport nhận kết nối."""
    return QCoreApplication.translate("Firewall", "Tường lửa của Windows đang chặn Teleport nhận kết nối.")


def firewall_guide_action_text() -> str:
    """Nút bấm mở hướng dẫn trên thanh thông báo."""
    return QCoreApplication.translate("Firewall", "Mở hướng dẫn")


def dialog_title() -> str:
    return QCoreApplication.translate("Firewall", "Hướng dẫn cấu hình tường lửa")


def header_text() -> str:
    return QCoreApplication.translate(
        "Firewall",
        "Tường lửa của Windows hoặc mạng công cộng có thể đang chặn các máy tính khác kết nối "
        "tới Teleport trên máy này.",
    )


def step1_title() -> str:
    return QCoreApplication.translate("Firewall", "1. Đổi mạng sang Mạng riêng tư (Private):")


def step1_body() -> str:
    return QCoreApplication.translate(
        "Firewall",
        "Vào Cài đặt Windows → Mạng & Internet → Wi-Fi (hoặc Ethernet) → chọn mạng đang kết nối "
        "→ chuyển sang Mạng riêng tư.",
    )


def step2_title() -> str:
    return QCoreApplication.translate("Firewall", "2. Cho phép Teleport qua Tường lửa:")


def step2_body() -> str:
    return QCoreApplication.translate(
        "Firewall",
        "Mở Cài đặt Tường lửa Windows → Cho phép ứng dụng qua tường lửa → tìm Teleport và đánh dấu "
        "Cho phép trên mạng Riêng tư.",
    )


def open_network_btn_text() -> str:
    return QCoreApplication.translate("Firewall", "Mở Cài đặt mạng")


def open_firewall_btn_text() -> str:
    return QCoreApplication.translate("Firewall", "Mở Tường lửa")


def close_btn_text() -> str:
    return QCoreApplication.translate("Firewall", "Đóng")


def _plain_label(object_name: str, parent: QWidget) -> QLabel:
    label = QLabel(parent)
    label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class FirewallGuideDialog(QDialog):
    """Hộp thoại hướng dẫn người dùng khắc phục khi tường lửa chặn Teleport."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("firewallGuideDialog")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self.header_label = _plain_label("firewallHeaderLabel", self)
        self.step1_title_label = _plain_label("firewallStep1TitleLabel", self)
        self.step1_title_label.setStyleSheet("font-weight: bold;")
        self.step1_body_label = _plain_label("firewallStep1BodyLabel", self)
        self.step1_body_label.setProperty(theme.TONE_PROPERTY, "muted")

        self.step2_title_label = _plain_label("firewallStep2TitleLabel", self)
        self.step2_title_label.setStyleSheet("font-weight: bold;")
        self.step2_body_label = _plain_label("firewallStep2BodyLabel", self)
        self.step2_body_label.setProperty(theme.TONE_PROPERTY, "muted")

        self.network_button = QPushButton(self)
        self.network_button.setObjectName("firewallOpenNetworkButton")
        self.network_button.clicked.connect(self._on_open_network)

        self.firewall_button = QPushButton(self)
        self.firewall_button.setObjectName("firewallOpenFirewallButton")
        self.firewall_button.clicked.connect(self._on_open_firewall)

        self.close_button = QPushButton(self)
        self.close_button.setObjectName("firewallCloseButton")
        self.close_button.setProperty(theme.PRIMARY_PROPERTY, True)
        self.close_button.setDefault(True)
        self.close_button.clicked.connect(self.accept)

        btn_layout = QHBoxLayout()
        btn_layout.addWidget(self.network_button)
        btn_layout.addWidget(self.firewall_button)
        btn_layout.addStretch(1)
        btn_layout.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.setSpacing(theme.SPACING[2])
        layout.addWidget(self.header_label)
        layout.addSpacing(theme.SPACING[1])
        layout.addWidget(self.step1_title_label)
        layout.addWidget(self.step1_body_label)
        layout.addSpacing(theme.SPACING[1])
        layout.addWidget(self.step2_title_label)
        layout.addWidget(self.step2_body_label)
        layout.addSpacing(theme.SPACING[3])
        layout.addLayout(btn_layout)

        for first, second in (
            (self.network_button, self.firewall_button),
            (self.firewall_button, self.close_button),
        ):
            QWidget.setTabOrder(first, second)

        self.setMinimumWidth(theme.DIALOG_MIN_WIDTH + 80)
        self.retranslate()

    def retranslate(self) -> None:
        self.setWindowTitle(dialog_title())
        self.header_label.setText(header_text())
        self.step1_title_label.setText(step1_title())
        self.step1_body_label.setText(step1_body())
        self.step2_title_label.setText(step2_title())
        self.step2_body_label.setText(step2_body())
        self.network_button.setText(open_network_btn_text())
        self.firewall_button.setText(open_firewall_btn_text())
        self.close_button.setText(close_btn_text())

    def _on_open_network(self) -> None:
        """Mở cài đặt mạng của hệ điều hành."""
        try:
            open_url(QUrl("ms-settings:network"))
        except Exception:
            log.warning("khong the mo cai dat mang he dieu hanh", exc_info=True)

    def _on_open_firewall(self) -> None:
        """Mở bảng điều khiển tường lửa Windows."""
        try:
            startfile("firewall.cpl")
        except Exception:
            log.warning("khong the mo bang dieu khien tuong lua", exc_info=True)


def open_firewall_guide(parent: QWidget | None = None) -> FirewallGuideDialog:
    """Tạo và hiển thị hộp thoại hướng dẫn cấu hình tường lửa."""
    dialog = FirewallGuideDialog(parent)
    dialog.open()
    return dialog
