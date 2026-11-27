"""Kiểm thử bảng thông tin bên phải luồng chat (`teleport.ui.device_panel`, `ChatView.info_panel`)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from teleport.app.devices import DeviceView

PEER_ID = "a71c04d2" + "e" * 24


def _device(*, name: str = "Laptop-Minh", online: bool = True, os_name: str = "windows") -> DeviceView:
    return DeviceView(
        device_id=PEER_ID,
        name=name,
        paired=True,
        online=online,
        connected=online,
        addresses=("192.168.1.42",) if online else (),
        port=47474 if online else 0,
        os=os_name,
    )


def test_device_info_panel_displays_device_accordion_and_signals(qtbot) -> None:
    from teleport.ui.device_panel import DeviceInfoPanel
    from teleport.ui.widgets import Presence

    panel = DeviceInfoPanel()
    qtbot.addWidget(panel)
    panel.show()

    panel.set_device("Laptop-Minh", "Đang kết nối", Presence.ONLINE, key=PEER_ID, os_name="windows")
    assert panel.name_label.text() == "Laptop-Minh"
    assert panel.name_label.textFormat() == Qt.TextFormat.PlainText
    assert panel.status_label.text() == "Đang kết nối"
    assert PEER_ID[:8] in panel.meta_label.text()
    assert "Windows" in panel.meta_label.text()

    # Hai nhóm đều mở sẵn và thu gọn/mở rộng được
    assert panel.prefs_section.expanded
    assert panel.security_section.expanded
    panel.security_section.header.click()
    QApplication.processEvents()
    assert not panel.security_section.expanded
    assert not panel.security_section.body.isVisible()
    panel.security_section.header.click()
    QApplication.processEvents()
    assert panel.security_section.expanded
    assert panel.security_section.body.isVisible()

    # Phát các signal thao tác (không trùng lặp với ChatComposer / chân trang)
    calls: list[object] = []
    panel.search_requested.connect(lambda: calls.append("search"))
    panel.retry_connect_requested.connect(lambda: calls.append("retry"))
    panel.device_settings_requested.connect(lambda: calls.append("settings"))
    panel.connect_ip_requested.connect(lambda: calls.append("connect_ip"))
    panel.clear_history_requested.connect(lambda: calls.append("clear"))
    panel.unpair_requested.connect(lambda: calls.append("unpair"))
    panel.clipboard_toggled.connect(lambda on: calls.append(("clip", on)))
    panel.auto_accept_toggled.connect(lambda on: calls.append(("auto", on)))

    panel.quick_search_action.button.click()
    panel.quick_retry_action.button.click()
    panel.quick_settings_action.button.click()
    panel.connect_ip_row.click()
    panel.clear_history_row.click()
    panel.unpair_row.click()
    panel.clipboard_switch.click()
    panel.auto_accept_switch.click()

    assert calls == [
        "search",
        "retry",
        "settings",
        "connect_ip",
        "clear",
        "unpair",
        ("clip", True),
        ("auto", True),
    ]


def test_chat_view_right_panel_responsive_sync_and_toggle(qtbot) -> None:
    from teleport.ui.chat import ChatView
    from teleport.ui.radar import DeviceListModel

    model = DeviceListModel()
    model.reset([_device()])
    view = ChatView(model)
    qtbot.addWidget(view)

    # Mặc định tắt quick_actions -> info_panel ẩn dù rộng
    view.resize(900, 620)
    view.show()
    view.set_device(PEER_ID)
    QApplication.processEvents()
    assert view.info_panel.isHidden()

    # Bật quick_actions + cửa sổ đủ rộng (>= 780 px) -> tự hiện cột phải & ẩn nút trùng ở header
    view.set_quick_actions_available(True)
    QApplication.processEvents()
    assert not view.info_panel.isHidden()
    assert view.header.info_button.isChecked()
    assert not view.header.info_button.isHidden()
    assert view.header.clipboard_button.isHidden()
    assert view.header.auto_accept_button.isHidden()
    assert view.header.search_button.isHidden()
    assert view.header.more_button.isHidden()
    assert view.info_panel.name_label.text() == "Laptop-Minh"

    # Đồng bộ 2 chiều giữa ChatHeader và DeviceInfoPanel
    clip_events: list[tuple[str, bool]] = []
    unpair_events: list[str] = []
    view.header_clipboard_toggled.connect(lambda d, on: clip_events.append((d, on)))
    view.unpair_requested.connect(unpair_events.append)

    view.info_panel.clipboard_switch.click()
    assert clip_events == [(PEER_ID, True)]
    assert view.header.clipboard_button.isChecked()

    view.info_panel.unpair_row.click()
    assert unpair_events == [PEER_ID]

    # Bấm nút info ở header để thu gọn cột phải -> các nút thừa vẫn luôn ẩn
    view.header.info_button.click()
    QApplication.processEvents()
    assert view.info_panel.isHidden()
    assert not view.header.info_button.isChecked()
    assert view.header.clipboard_button.isHidden()
    assert view.header.auto_accept_button.isHidden()
    assert view.header.search_button.isHidden()
    assert view.header.more_button.isHidden()

    # Thu nhỏ dưới ngưỡng tự động rồi bấm mở lại ở kích thước vừa (>= 660 px)
    view.resize(700, 620)
    QApplication.processEvents()
    assert view.info_panel.isHidden()
    view.header.info_button.click()
    QApplication.processEvents()
    assert not view.info_panel.isHidden()
    assert view.header.clipboard_button.isHidden()

    # Rất hẹp (< 660 px): luôn tự ẩn để bảo vệ luồng chat
    view.resize(640, 620)
    QApplication.processEvents()
    assert view.info_panel.isHidden()
    assert view.header.clipboard_button.isHidden()
