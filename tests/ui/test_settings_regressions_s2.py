"""Test hồi quy cho các phát hiện của soat-code S2 trên trang Cài đặt (spec D2, G2).

1. Lỗi đổi thư mục nhận hiện ở trang Cài đặt được gỡ khi đổi thành công.
2. Lỗi UNAVAILABLE/RESERVED có nút “Chọn thư mục khác” mở hộp chọn; nút của thông báo khác không mở.
3. `set_loaded(False)` không tắt hàng Thư mục nhận.
4. Huy hiệu “Đang ẩn” trở về “Sẵn sàng nhận” khi dựng host mới (trước `started`).

GUI chạy `teleport.ui.app.main` trong tiến trình (fixture `gui`, `keep_receive_dir=True`), node thật trên
127.0.0.1; seam `receive_dir.dialog_factory` thay bằng hộp không hiện.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")


class _Dialogs:
    def __init__(self) -> None:
        self.made: list = []

    def factory(self, parent=None, *args, **kwargs):
        from PySide6.QtWidgets import QFileDialog

        made = self.made

        class FakeDialog(QFileDialog):
            def __init__(self) -> None:
                super().__init__(parent)
                made.append(self)

            def open(self, *a, **k) -> None:
                pass

        return FakeDialog()


@pytest.fixture
def dialogs(monkeypatch):
    from teleport.ui import receive_dir as mod

    d = _Dialogs()
    monkeypatch.setattr(mod, "dialog_factory", d.factory)
    return d


def _select(dialog, path: Path) -> None:
    dialog.fileSelected.emit(str(path))
    dialog.finished.emit(1)


def _visible_notice(page) -> str | None:
    bar = page.info_bar
    return bar.title if bar.isVisibleTo(page) else None


def _app(run):
    return run.tray.parent()


def _start(run, gui_wait):
    from teleport.ui.main_window import Page

    yield gui_wait(lambda: run.windows and run.host is not None and run.host.running, 20, "node chạy")
    app = _app(run)
    yield gui_wait(lambda: app.receive_dir_controller.receive_dir is not None, 5, "có thư mục nhận")
    run.window.open_page(Page.SETTINGS)
    yield gui_wait(lambda: run.window.current_page is Page.SETTINGS, 3, "trang Cài đặt mở")


@pytest.mark.integration
def test_settings_dir_error_cleared_after_successful_change(gui, gui_wait, config_dir, dialogs, tmp_path):
    a = config_dir("A")
    not_a_dir = tmp_path / "la-file"
    not_a_dir.write_bytes(b"x")
    good = tmp_path / "thu-muc-moi"
    good.mkdir()
    seen: dict = {}

    def script(run):
        yield from _start(run, gui_wait)
        app = _app(run)
        page = app.settings.page
        c = app.receive_dir_controller
        page.change_dir_button.click()
        _select(dialogs.made[-1], not_a_dir)
        yield gui_wait(lambda: not c.changing and _visible_notice(page) is not None, 10, "câu lỗi")
        seen["error"] = _visible_notice(page)
        seen["action_shown"] = page.info_bar.action_button.isVisibleTo(page)
        count = len(dialogs.made)
        page.info_bar.action_button.click()  # “Chọn thư mục khác”
        seen["reopened"] = len(dialogs.made) == count + 1
        _select(dialogs.made[-1], good)
        yield gui_wait(lambda: not c.changing and c.receive_dir is not None, 10, "đổi xong")
        yield gui_wait(lambda: _visible_notice(page) is None, 3, "câu lỗi được gỡ")
        seen["after"] = _visible_notice(page)
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert seen["error"] is not None
    assert seen["action_shown"], "lỗi thư mục không dùng được có nút “Chọn thư mục khác”"
    assert seen["reopened"], "nút “Chọn thư mục khác” mở hộp chọn thư mục"
    assert seen["after"] is None


@pytest.mark.integration
def test_other_notice_action_does_not_open_dir_dialog(gui, gui_wait, config_dir, dialogs):
    from teleport.ui.widgets import InfoLevel

    a = config_dir("A")
    seen: dict = {}

    def script(run):
        yield from _start(run, gui_wait)
        app = _app(run)
        calls: list = []
        app.settings.show_notice(
            InfoLevel.SUCCESS, "Đã lưu log.", action=("Mở thư mục", lambda: calls.append(1))
        )
        app.settings.page.info_bar.action_button.click()
        seen["calls"] = calls
        seen["dialogs"] = len(dialogs.made)
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert seen["calls"] == [1]
    assert seen["dialogs"] == 0


def test_set_loaded_false_keeps_receive_dir_row_enabled(qtbot):
    from teleport.ui.radar import DeviceListModel
    from teleport.ui.settings import SettingsPage

    model = DeviceListModel()
    page = SettingsPage(model)
    model.setParent(page)
    qtbot.addWidget(page)
    page.set_receive_dir(Path("C:/Nhan"))
    page.set_change_available(True)
    page.set_loaded(False)
    assert page.open_dir_button.isEnabled()
    assert page.change_dir_button.isEnabled()
    assert not page.name_edit.isEnabled()
    assert not page.language_combo.isEnabled()
    assert not page.autostart_switch.isEnabled()


@pytest.mark.integration
def test_hidden_badge_resets_on_new_host_before_started(gui, gui_wait, config_dir):
    from teleport.ui.main_window import ViewState

    a = config_dir("A")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.windows and run.host is not None and run.host.running, 20, "node chạy")
        app = _app(run)
        card = run.window.machine_card
        card.set_hidden(True)
        seen["hidden_text"] = card.badge.text
        app.host.stop()
        yield gui_wait(lambda: app.host.is_stopped, 15, "node dừng")
        run.window.show_error("other", "")
        app.start_node()  # host mới, chưa `started`
        seen["new_host"] = app.host
        seen["machine_hidden"] = card.machine_hidden
        seen["text"] = card.badge.text
        yield gui_wait(lambda: run.window.view_state is not ViewState.STARTING, 20, "host mới chạy")
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script)
    assert seen["hidden_text"] == "Đang ẩn"
    assert seen["machine_hidden"] is False
    assert seen["text"] == "Sẵn sàng nhận"
