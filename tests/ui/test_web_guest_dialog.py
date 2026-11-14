"""Kiểm thử tự động cho hộp thoại Chế độ khách (teleport.ui.web_guest_dialog)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teleport.app.web_guest import GuestSessionInfo
from teleport.core.qr import encode_qr, render_ascii, render_svg
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
