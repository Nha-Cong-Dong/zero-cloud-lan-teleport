"""Test hồi quy cho các phát hiện của soat-code S1 trên trang Cài đặt (spec D2).

1. Tab qua ô tốc độ / ô ngày không được đổi giá trị đã lưu nằm ngoài lưới hiển thị của ô.
2. Node từ chối tên: tên đã lưu trở về tên đã xác nhận, giữ chữ, Enter lần nữa gửi lại.
3. Ảnh chụp xin trước một lời ghi của người dùng không đè control.
4. Ghi registry lỗi: công tắc theo trạng thái đọc lại, không theo trạng thái trước khi ghi.
5. Ẩn/hiện gọi thẳng `host.apply_pairing_policy(node)` trong cùng lời gọi.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QApplication


class _Node:
    def __init__(self) -> None:
        self.name = "May-A"
        self.visible = True
        self.accept_pairing = True
        self.rate_limit: int | None = None
        self.log: list[tuple] = []
        self.name_error: Exception | None = None

    def set_name(self, name: str) -> str:
        self.log.append(("set_name", name))
        if self.name_error is not None:
            error, self.name_error = self.name_error, None
            raise error
        self.name = name
        return name

    def set_visible(self, visible: bool) -> None:
        self.log.append(("set_visible", visible))
        self.visible = visible

    def set_rate_limit(self, value: int | None) -> None:
        self.log.append(("set_rate_limit", value))
        self.rate_limit = value

    def set_ui_preferences(self, **kw: object) -> None:
        self.log.append(("set_ui_preferences", kw))


class _Host(QObject):
    """`call` chỉ ghi lại; `run()` chạy theo thứ tự (như loop của node)."""

    def __init__(self, node: _Node) -> None:
        super().__init__()
        self.node = node
        self.pending: list[tuple] = []
        self.running = True
        self.policy_calls = 0

    def call(self, fn, on_done=None) -> None:
        self.pending.append((fn, on_done))

    def apply_pairing_policy(self, node: _Node) -> None:
        self.policy_calls += 1
        node.accept_pairing = node.visible

    def run(self) -> None:
        while self.pending:
            fn, on_done = self.pending.pop(0)
            try:
                result = fn(self.node)
            except Exception as e:
                result = e
            if on_done is not None:
                on_done(result)


def _snapshot(node: _Node):
    from teleport.ui.settings import SettingsSnapshot

    return SettingsSnapshot(
        name=node.name,
        visible=node.visible,
        rate_limit=node.rate_limit,
        temp_retention_days=None,
        language=None,
        autostart=False,
        devices={},
    )


@pytest.fixture
def page(qtbot):
    from teleport.ui.radar import DeviceListModel
    from teleport.ui.settings import SettingsPage

    model = DeviceListModel()
    widget = SettingsPage(model)
    model.setParent(widget)
    qtbot.addWidget(widget)
    widget.resize(980, 660)
    widget.show()
    qtbot.waitExposed(widget)
    widget.set_loaded(True)
    return widget


@pytest.fixture
def ctl(page, tmp_path, monkeypatch, fake_registry):
    from teleport.ui import settings as ui_settings
    from teleport.ui.settings import SettingsController

    monkeypatch.setattr(ui_settings, "read_snapshot", _snapshot)
    node = _Node()
    host = _Host(node)
    controller = SettingsController(page, tmp_path)
    controller.attach(host)
    controller.on_started(host)
    host.run()
    QApplication.processEvents()
    yield SimpleNamespace(c=controller, host=host, node=node, page=page, registry=fake_registry)
    controller.shutdown()


def _tab_through(qtbot, widget) -> None:
    """Tab vào ô rồi Tab ra (ô số phát `editingFinished` khi mất tiêu điểm)."""
    widget.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.wait(10)
    widget.clearFocus()
    widget.editingFinished.emit()
    qtbot.wait(10)


def test_tab_through_rate_spin_keeps_off_grid_value(page, qtbot):
    from teleport.app import transfers as app_transfers
    from teleport.ui.settings import Section

    emitted: list = []
    page.rate_limit_changed.connect(emitted.append)
    page.set_rate_limit(app_transfers.MIN_RATE_LIMIT)  # 65536 byte: không nằm trên lưới 0,1 MB/s
    page.show_section(Section.TRANSFER)
    _tab_through(qtbot, page.rate_spin)
    assert emitted == []


def test_tab_through_retention_spin_keeps_explicit_default(page, qtbot):
    from teleport.ui.settings import Section, default_retention_days

    emitted: list = []
    page.retention_changed.connect(emitted.append)
    page.set_retention(default_retention_days())  # lưu tường minh số bằng mặc định
    page.show_section(Section.TRANSFER)
    _tab_through(qtbot, page.retention_spin)
    assert emitted == []


def test_rate_spin_real_change_still_emits(page, qtbot):
    from teleport.ui.settings import rate_to_bytes

    emitted: list = []
    page.rate_limit_changed.connect(emitted.append)
    page.set_rate_limit(65536)
    page.rate_spin.setValue(3.0)
    page.rate_spin.editingFinished.emit()
    assert emitted == [rate_to_bytes(3.0)]


def test_rejected_name_reverts_saved_name_and_enter_resends(ctl):
    from teleport.core.messages import MessageError

    page = ctl.page
    submitted: list = []
    page.name_submitted.connect(submitted.append)
    page.name_edit.setText("Ten-Moi")
    ctl.node.name_error = MessageError("sai")
    page.name_edit.editingFinished.emit()
    ctl.host.run()
    assert page.name_error.isVisibleTo(page)
    assert page.name_edit.text() == "Ten-Moi", "giữ chữ"
    assert page.name_edit.saved == "May-A", "tên đã lưu trở về tên đã xác nhận"
    page.name_edit.editingFinished.emit()  # Enter lần nữa
    assert submitted == ["Ten-Moi", "Ten-Moi"]
    ctl.host.run()
    assert ctl.node.name == "Ten-Moi"


def test_snapshot_requested_before_user_write_does_not_override(ctl):
    page = ctl.page
    ctl.c.refresh()  # xin ảnh chụp (visible=True) ...
    page.visible_switch.click()  # ... rồi người dùng ẩn máy trước khi ảnh chụp về
    ctl.host.run()
    QApplication.processEvents()
    assert not page.visible_switch.isChecked()
    assert ctl.node.visible is False


def test_autostart_error_after_partial_write_shows_reread_state(ctl, monkeypatch):
    from teleport.app import autostart

    states = iter([autostart.AutostartState.ON])

    def fail(_config_dir) -> None:
        raise autostart.AutostartError("ghi dở")

    monkeypatch.setattr(autostart, "enable", fail)
    monkeypatch.setattr(autostart, "state", lambda _d: next(states))
    ctl.page.autostart_switch.click()
    assert ctl.page.autostart_switch.isChecked(), "theo trạng thái đọc lại sau lỗi"


def test_visible_toggle_calls_host_policy_in_same_call(ctl):
    ctl.page.visible_switch.click()
    assert len(ctl.host.pending) == 1
    ctl.host.run()
    assert ctl.host.policy_calls == 1
    assert ctl.node.accept_pairing is False
