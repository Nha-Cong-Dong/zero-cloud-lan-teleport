"""Kiểm thử tự động cho hộp thoại Chế độ khách (teleport.ui.web_guest_dialog)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teleport.app.web_guest import GuestSessionInfo
from teleport.core.qr import encode_qr, render_ascii, render_svg
from teleport.net.web_guest import GuestScope
from teleport.ui.web_guest_dialog import WebGuestDialog, qr_matrix_to_pixmap


def test_qr_matrix_to_pixmap(qapp: object) -> None:
    matrix = encode_qr("http://192.168.1.5:8080")
    pixmap = qr_matrix_to_pixmap(matrix, module_size=4, border=2)
    assert not pixmap.isNull()
    assert pixmap.width() > 0
    assert pixmap.height() == pixmap.width()


def test_web_guest_dialog_ui(qtbot: pytest.FixtureRequest) -> None:
    mock_node = MagicMock()
    mock_node.guest_session_info = None

    dialog = WebGuestDialog(node=mock_node)
    dialog.show()

    url = "http://192.168.1.15:54321/?token=abc123xyz"
    matrix = encode_qr(url)
    info = GuestSessionInfo(
        token="abc123xyz",
        port=54321,
        local_ip="192.168.1.15",
        url=url,
        qr_matrix=matrix,
        qr_ascii=render_ascii(matrix),
        qr_svg=render_svg(matrix),
    )

    dialog.set_session_info(info)

    assert dialog.url_label.text() == url
    assert not dialog.qr_label.pixmap().isNull()

    # Thử copy URL
    dialog._copy_url()
    assert "Đã chép" in dialog.copy_btn.text()

    # Thử cập nhật tiến độ
    dialog.update_progress("photo.jpg", 500, 1000)
    assert dialog.progress_container.isVisible()
    assert dialog.progress_bar.value() == 50
    assert "photo.jpg" in dialog.progress_label.text()

    # Thử sự kiện hoàn tất nhận
    dialog.upload_completed.emit("photo.jpg", 1000, "/tmp/photo.jpg")
    assert dialog.progress_bar.value() == 100
    assert "Đã nhận thành công" in dialog.progress_label.text()

    dialog.accept()


def test_web_guest_dialog_with_host(qtbot: pytest.FixtureRequest) -> None:
    mock_host = MagicMock()
    mock_host.running = True

    dialog = WebGuestDialog(host=mock_host)
    assert mock_host.submit.called

    dialog.session_failed.emit("Cổng mạng bị chiếm")
    assert "Cổng mạng bị chiếm" in dialog.subtitle.text()

    dialog.close()


def test_web_guest_dialog_scope_text_and_wifi_qr(qtbot: pytest.FixtureRequest) -> None:
    mock_node = MagicMock()
    mock_node._guest_manager = MagicMock()
    mock_host = MagicMock()
    mock_host.running = True

    # Phần scope & one-shot khởi động lại phiên khi đổi lựa chọn
    dialog = WebGuestDialog(host=mock_host)
    qtbot.addWidget(dialog)
    dialog.show()
    initial_calls = mock_host.submit.call_count
    assert initial_calls >= 1

    idx = dialog.scope_combo.findData(GuestScope.SEND_ONLY)
    assert idx >= 0
    dialog.scope_combo.setCurrentIndex(idx)
    dialog.one_shot_check.setChecked(True)
    assert mock_host.submit.call_count >= initial_calls + 2

    # Gửi chữ qua host.call và nhận chữ từ signal text_received
    dialog.text_input.setText("https://local.test")
    dialog.send_text_btn.click()
    assert mock_host.call.called
    assert dialog.text_input.text() == ""

    dialog.text_received.emit("Ghi chú từ điện thoại")
    assert dialog.received_text_container.isVisible()
    assert "Ghi chú từ điện thoại" in dialog.received_text_label.text()

    # Bảng Wi-Fi QR nội tuyến
    assert not dialog.wifi_panel.isVisible()
    dialog.wifi_qr_btn.click()
    assert dialog.wifi_panel.isVisible()
    dialog.wifi_ssid_input.setText("Teleport-WiFi")
    dialog.wifi_password_input.setText("12345678")
    dialog.wifi_generate_btn.click()
    assert not dialog.wifi_qr_label.pixmap().isNull()

    dialog.close()

    # Test node trực tiếp không qua host
    direct = WebGuestDialog(node=mock_node)
    qtbot.addWidget(direct)
    direct.text_input.setText("Gửi trực tiếp")
    direct.send_text_btn.click()
    mock_node._guest_manager.send_text.assert_called_once_with("Gửi trực tiếp")
    direct.close()
