from unittest.mock import MagicMock

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QDialog

from teleport.ui import firewall
from teleport.ui.firewall import (
    FirewallGuideDialog,
    firewall_blocked_notice_text,
    firewall_guide_action_text,
    open_firewall_guide,
)


def test_notice_texts():
    assert "Tường lửa" in firewall_blocked_notice_text()
    assert firewall_guide_action_text() == "Mở hướng dẫn"


def test_dialog_creation(qapp):
    dialog = FirewallGuideDialog()
    try:
        assert dialog.objectName() == "firewallGuideDialog"
        assert "tường lửa" in dialog.windowTitle().lower()
        assert not dialog.network_button.isHidden()
        assert not dialog.firewall_button.isHidden()
        assert not dialog.close_button.isHidden()
    finally:
        dialog.close()


def test_dialog_open_network_action(qapp, monkeypatch):
    opened_urls: list[QUrl] = []
    monkeypatch.setattr(firewall, "open_url", lambda url: opened_urls.append(url))

    dialog = FirewallGuideDialog()
    try:
        dialog.network_button.click()
        assert len(opened_urls) == 1
        assert opened_urls[0].scheme() == "ms-settings"
    finally:
        dialog.close()


def test_dialog_open_firewall_action(qapp, monkeypatch):
    started_args: list[str] = []
    monkeypatch.setattr(firewall, "startfile", lambda arg: started_args.append(arg))

    dialog = FirewallGuideDialog()
    try:
        dialog.firewall_button.click()
        assert started_args == ["firewall.cpl"]
    finally:
        dialog.close()


def test_open_firewall_guide_helper(qapp, monkeypatch):
    monkeypatch.setattr(QDialog, "open", MagicMock())
    dialog = open_firewall_guide()
    assert isinstance(dialog, FirewallGuideDialog)
    dialog.close()


def test_app_on_firewall_checked_shows_and_hides_notice(qapp, monkeypatch):
    from teleport.app.firewall import FirewallIssue
    from teleport.ui import app as app_module
    from teleport.ui.chat import ChatView

    view = MagicMock(spec=ChatView)
    view.notice_text.return_value = "Tường lửa của Windows đang chặn Teleport nhận kết nối."

    app = MagicMock()
    app._quitting = False
    app._firewall_notice = None
    app.transfers = MagicMock()
    app.transfers.view = view
    app.window = MagicMock()

    # Bị tường lửa chặn -> hiện thông báo cảnh báo
    app_module.GuiApp._on_firewall_checked(app, FirewallIssue.BLOCKED)
    assert app.transfers.view.show_notice.called
    assert app._firewall_notice is not None

    # Tình trạng bình thường trở lại -> ẩn thông báo
    app_module.GuiApp._on_firewall_checked(app, FirewallIssue.NONE)
    assert app.transfers.view.hide_notice.called
    assert app._firewall_notice is None


def test_check_firewall_background_thread_dispatches_to_main_thread(qapp, monkeypatch):
    import time

    from PySide6.QtCore import QCoreApplication, QObject

    from teleport.app.firewall import FirewallIssue
    from teleport.ui import app as app_module

    class DummyApp(QObject):
        def __init__(self):
            super().__init__()
            self._quitting = False
            self.called_with = None

        def _on_firewall_checked(self, issue):
            self.called_with = issue

    app = DummyApp()
    monkeypatch.setattr(app_module, "check_firewall_status_fn", lambda: FirewallIssue.BLOCKED)

    app_module.GuiApp._check_firewall(app)

    # Đợi thread nền hoàn tất và QTimer.singleShot chuyển sự kiện về luồng chính
    deadline = time.monotonic() + 2.0
    while app.called_with is None and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)

    assert app.called_with is FirewallIssue.BLOCKED
