"""Hàng “Thư mục nhận” của trang Cài đặt nối với `ReceiveDirController` (spec D2 mục 1.8, test 4.7).

Viết từ spec, không từ code. A là GUI chạy `teleport.ui.app.main` trong tiến trình (fixture `gui` với
`keep_receive_dir=True`: app dùng `ReceiveDirChoice.SAVED`), node thật trên 127.0.0.1.

Hợp đồng kiểm (spec D2 1.8, spec-b1b 2.2 và 11.5):
- `controller.state_changed` → `page.set_receive_dir(c.receive_dir, c.unavailable, c.pending)` và
  `page.set_change_available(True)`: hàng luôn theo đúng trạng thái của controller (bình thường, đang
  đổi, thư mục đã lưu hỏng); nút “Đổi…” hiện.
- `page.change_receive_dir_requested` → `controller.choose(self.window)`: hộp chọn thư mục (seam
  `teleport.ui.receive_dir.dialog_factory`) mở với cha là cửa sổ chính, bằng `open()`.
- `controller.failed` khi trang Cài đặt đang hiện → thêm `page.show_notice(ERROR, text)`; trang không
  hiện thì không thêm.

Đường vào: `GuiApp` là cha của khay (`run.tray.parent()`), `GuiApp.receive_dir_controller` (spec-b1b 2.7),
`GuiApp.settings.page` (spec D2 1.7). Chờ bằng điều kiện có hạn (`gui_wait`), không `sleep` cứng.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

pytestmark = pytest.mark.integration

# ---- Câu của spec (D2 1.9, spec-b1b 2.8 và 11.5) -------------------------------------------------
CHANGING = "Đang đổi sang {path}…"
NOT_RECEIVING_ROW = "Không nhận file: không mở được {path}"
UNAVAILABLE_TEXT = "Không dùng được thư mục {path}. Hãy chọn thư mục khác mà bạn có quyền ghi."
RESERVED_TEXT = (
    "Không dùng được thư mục {path} để nhận file vì Teleport dùng nó cho việc riêng. Hãy chọn thư mục khác."
)


# =================================================================================================
# Phụ trợ
# =================================================================================================


class Dialogs:
    """Seam `receive_dir.dialog_factory`: ghi lại cha được truyền vào; `open()` không hiện gì."""

    def __init__(self) -> None:
        self.made: list = []
        self.parents: list = []

    def factory(self, parent=None, *args, **kwargs):
        from PySide6.QtWidgets import QFileDialog

        made = self.made
        self.parents.append(parent)

        class FakeDialog(QFileDialog):
            def __init__(self) -> None:
                super().__init__(parent)
                self.opened = 0
                made.append(self)

            def open(self, *a, **k):
                self.opened += 1

            def exec(self, *a, **k):
                raise AssertionError("hộp chọn thư mục không được exec()")

        return FakeDialog()


@pytest.fixture
def dialogs(monkeypatch):
    from teleport.ui import receive_dir as mod

    d = Dialogs()
    monkeypatch.setattr(mod, "dialog_factory", d.factory)
    return d


def same(a, b) -> bool:
    if a is None or b is None:
        return False
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def shown(path) -> str:
    from teleport.ui.send import printable

    return printable(str(path))


def app_of(run):
    app = run.tray.parent()
    assert app is not None and hasattr(app, "receive_dir_controller"), "GuiApp phải có receive_dir_controller"
    assert hasattr(app, "settings"), "GuiApp phải có settings (spec D2 1.7)"
    return app


def wait_running(run, gui_wait):
    from teleport.ui.main_window import ViewState

    yield gui_wait(
        lambda: run.windows and run.window.view_state in (ViewState.RUNNING, ViewState.ERROR),
        20,
        "cửa sổ RUNNING (hay ERROR)",
    )
    assert run.window.view_state is ViewState.RUNNING
    yield gui_wait(lambda: run.host.running, 5, "node chạy")


def open_settings(run, gui_wait):
    from teleport.ui.main_window import Page

    window = run.window
    window.settings_button.click()
    yield gui_wait(lambda: window.current_page is Page.SETTINGS, 3, "trang Cài đặt mở")


def go_back(run, gui_wait):
    from teleport.ui.main_window import Page

    window = run.window
    window.open_page(Page.HOME)
    yield gui_wait(lambda: window.current_page is not Page.SETTINGS, 3, "rời trang Cài đặt")


def show_general(page) -> None:
    from PySide6.QtWidgets import QApplication

    from teleport.ui.settings import Section

    page.show_section(Section.GENERAL)
    QApplication.processEvents()


def row_state(page) -> dict:
    """Ảnh chụp hàng Thư mục nhận: chữ đầy đủ, tone, nút Mở / Đổi… có hiện không (trong trang)."""
    from teleport.ui import theme

    label = page.receive_dir_label
    text = label.full_text if hasattr(label, "full_text") else label.text()
    return {
        "text": text,
        "tone": label.property(theme.TONE_PROPERTY),
        "open": page.open_dir_button.isVisibleTo(page),
        "change": page.change_dir_button.isVisibleTo(page),
    }


def notice(page) -> tuple | None:
    """Thông báo đang hiện trên `info_bar` của trang: (mức, chữ) hay None khi thanh ẩn.

    Chữ gộp tiêu đề và dòng dưới (không giả định câu lỗi nằm ở dòng nào của thanh)."""
    bar = page.info_bar
    if not bar.isVisibleTo(page):
        return None
    return bar.level, " ".join(part for part in (bar.title, bar.text) if part)


def select(dialog, path: Path) -> None:
    """Như hộp thật khi bấm chọn: `fileSelected` rồi `finished`."""
    dialog.fileSelected.emit(str(path))
    dialog.finished.emit(1)


def _save_broken_dir(config: Path, tmp_path: Path) -> Path:
    from teleport.app.settings import Settings

    broken = tmp_path / "da-luu-la-file"
    broken.write_bytes(b"khong phai thu muc")
    Settings(receive_dir=str(broken)).save(config)
    return broken


# =================================================================================================
# Hàng theo trạng thái controller
# =================================================================================================


def test_receive_dir_row_follows_controller_state(gui, gui_wait, config_dir, dialogs, tmp_path):
    """Hỏng lúc khởi động → đang đổi → đổi xong: hàng luôn khớp `(receive_dir, unavailable, pending)`."""
    a = config_dir("A")
    broken = _save_broken_dir(a, tmp_path)
    good = tmp_path / "nhan-tot"
    good.mkdir()
    seen: dict = {}

    def script(run):
        yield from wait_running(run, gui_wait)
        app = app_of(run)
        c = app.receive_dir_controller
        page = app.settings.page
        yield gui_wait(lambda: c.unavailable is not None, 5, "controller biết thư mục hỏng")
        yield from open_settings(run, gui_wait)
        show_general(page)
        seen["ctl_broken"] = (c.receive_dir, c.unavailable, c.pending)
        seen["broken"] = row_state(page)
        # Đổi thẳng qua controller: `pending` đặt đồng bộ, hàng phải đổi ngay trong cùng lời gọi.
        assert c.change(good) is True
        seen["ctl_pending"] = c.pending
        seen["pending"] = row_state(page)
        yield gui_wait(lambda: not c.changing and c.receive_dir is not None, 10, "đổi xong")
        seen["ctl_done"] = (c.receive_dir, c.unavailable, c.pending)
        seen["done"] = row_state(page)
        run.tray.quit_requested.emit()

    run = gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert run.exec_calls == []

    rd, unavailable, pending = seen["ctl_broken"]
    assert rd is None and pending is None and same(unavailable, broken), seen["ctl_broken"]
    assert seen["broken"] == {
        "text": NOT_RECEIVING_ROW.format(path=shown(unavailable)),
        "tone": "warning",
        "open": False,
        "change": True,
    }, "thư mục đã lưu hỏng: chữ warning, không có Mở, có Đổi… (set_change_available(True))"

    assert same(seen["ctl_pending"], good)
    pending_row = seen["pending"]
    assert pending_row["text"] == CHANGING.format(path=shown(seen["ctl_pending"])), pending_row
    assert pending_row["change"] is True

    rd, unavailable, pending = seen["ctl_done"]
    assert same(rd, good) and unavailable is None and pending is None, seen["ctl_done"]
    assert seen["done"] == {"text": shown(rd), "tone": "muted", "open": True, "change": True}


def test_receive_dir_row_normal_start_shows_controller_dir(gui, gui_wait, config_dir, dialogs, tmp_path):
    """Khởi động bình thường: hàng hiện đúng `controller.receive_dir`, nút Mở và Đổi… cùng hiện."""
    a = config_dir("A")
    seen: dict = {}

    def script(run):
        yield from wait_running(run, gui_wait)
        app = app_of(run)
        c = app.receive_dir_controller
        page = app.settings.page
        yield gui_wait(lambda: c.receive_dir is not None, 5, "controller có thư mục nhận")
        yield from open_settings(run, gui_wait)
        show_general(page)
        seen["rd"] = c.receive_dir
        seen["row"] = row_state(page)
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert seen["row"] == {"text": shown(seen["rd"]), "tone": "muted", "open": True, "change": True}


# =================================================================================================
# Nút “Đổi…” của Cài đặt
# =================================================================================================


def test_change_button_calls_controller_choose(gui, gui_wait, config_dir, dialogs, tmp_path):
    """Bấm “Đổi…” trên trang Cài đặt → `choose(window)`: một hộp chọn, cha là cửa sổ chính, `open()`;
    chọn thư mục → thư mục nhận đổi và hàng của Cài đặt hiện thư mục mới."""
    a = config_dir("A")
    target = tmp_path / "nhan-moi"
    target.mkdir()
    seen: dict = {}

    def script(run):
        yield from wait_running(run, gui_wait)
        app = app_of(run)
        c = app.receive_dir_controller
        page = app.settings.page
        yield gui_wait(lambda: c.receive_dir is not None, 5, "controller có thư mục nhận")
        yield from open_settings(run, gui_wait)
        show_general(page)
        before = len(dialogs.made)
        page.change_dir_button.click()
        seen["made"] = len(dialogs.made) - before
        seen["parent_is_window"] = bool(dialogs.parents) and dialogs.parents[-1] is run.window
        seen["opened"] = dialogs.made[-1].opened if dialogs.made else 0
        seen["ctl_dialog_is_made"] = bool(dialogs.made) and c.dialog is dialogs.made[-1]
        # Bấm lần nữa khi hộp còn mở: không mở hộp thứ hai (choose đưa hộp cũ lên trước).
        page.change_dir_button.click()
        seen["made_again"] = len(dialogs.made) - before
        select(dialogs.made[-1], target)
        yield gui_wait(lambda: not c.changing and same(c.receive_dir, target), 10, "đổi sang thư mục mới")
        seen["rd"] = c.receive_dir
        seen["row"] = row_state(page)
        seen["page_still_open"] = run.window.current_page.name
        run.tray.quit_requested.emit()

    run = gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert seen["made"] == 1, "bấm Đổi… phải mở đúng một hộp chọn thư mục"
    assert seen["parent_is_window"], "choose(self.window): cha của hộp là cửa sổ chính"
    assert seen["opened"] == 1, "hộp mở bằng open(), không exec()"
    assert seen["ctl_dialog_is_made"], "hộp đi qua ReceiveDirController (controller.dialog)"
    assert seen["made_again"] == 1, "bấm lần hai khi hộp còn mở không được mở hộp thứ hai"
    assert run.exec_calls == []
    assert seen["row"]["text"] == shown(seen["rd"])
    assert seen["page_still_open"] == "SETTINGS"


# =================================================================================================
# `failed` → thông báo trên trang Cài đặt
# =================================================================================================


def test_change_failed_notice_on_settings_page_when_open(gui, gui_wait, config_dir, dialogs, tmp_path):
    """Trang Cài đặt đang hiện: lỗi đổi (RESERVED đồng bộ, UNAVAILABLE qua node) hiện ở `info_bar` với
    mức ERROR và đúng câu B1b. Trang không hiện: lỗi không được ghi vào `info_bar` của Cài đặt."""
    from teleport.ui.widgets import InfoLevel

    a = config_dir("A")
    not_a_dir = tmp_path / "la-file"
    not_a_dir.write_bytes(b"x")
    seen: dict = {}

    def script(run):
        yield from wait_running(run, gui_wait)
        app = app_of(run)
        c = app.receive_dir_controller
        page = app.settings.page
        yield gui_wait(lambda: c.receive_dir is not None, 5, "controller có thư mục nhận")

        # 1. Trang KHÔNG hiện: lỗi RESERVED (thư mục cấu hình) không vào info_bar của Cài đặt.
        seen["closed_change"] = c.change(a)
        seen["closed_notice"] = notice(page)

        # 2. Trang hiện: RESERVED (phát đồng bộ, không qua node).
        yield from open_settings(run, gui_wait)
        show_general(page)
        seen["open_reserved_change"] = c.change(a)
        yield gui_wait(lambda: notice(page) is not None, 3, "info_bar có thông báo RESERVED")
        seen["reserved"] = notice(page)

        # 3. Trang hiện: UNAVAILABLE (node từ chối một file) qua nút Đổi… của Cài đặt.
        page.clear_notice()
        page.change_dir_button.click()
        assert dialogs.made, "bấm Đổi… phải mở hộp chọn"
        select(dialogs.made[-1], not_a_dir)
        yield gui_wait(
            lambda: not c.changing and notice(page) is not None, 10, "info_bar có thông báo UNAVAILABLE"
        )
        seen["unavailable"] = notice(page)
        seen["rd_after"] = c.receive_dir
        run.tray.quit_requested.emit()

    run = gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert run.exec_calls == []
    assert seen["closed_change"] is False
    assert seen["closed_notice"] is None, (
        f"trang đóng mà info_bar của Cài đặt hiện: {seen['closed_notice']!r}"
    )

    assert seen["open_reserved_change"] is False
    assert seen["reserved"] == (InfoLevel.ERROR, RESERVED_TEXT.format(path=shown(a)))
    assert seen["unavailable"] == (InfoLevel.ERROR, UNAVAILABLE_TEXT.format(path=shown(not_a_dir)))
    assert seen["rd_after"] is not None, "đổi hỏng không làm mất thư mục nhận cũ"


def test_change_failed_after_leaving_settings_not_on_page(gui, gui_wait, config_dir, dialogs, tmp_path):
    """Mở Cài đặt rồi quay lại trang chủ: lỗi sau đó không ghi vào `info_bar` của Cài đặt (“khi trang
    đang hiện”), nhưng vẫn hiện ở dải thông báo của cửa sổ chính như B1b."""
    a = config_dir("A")
    seen: dict = {}

    def script(run):
        yield from wait_running(run, gui_wait)
        app = app_of(run)
        c = app.receive_dir_controller
        page = app.settings.page
        yield gui_wait(lambda: c.receive_dir is not None, 5, "controller có thư mục nhận")
        yield from open_settings(run, gui_wait)
        yield from go_back(run, gui_wait)
        page.clear_notice()
        c.change(a)  # RESERVED, phát đồng bộ
        seen["page_bar"] = notice(page)
        seen["main_notice"] = app.transfers.view.notice_text()
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert seen["page_bar"] is None, f"trang đã đóng mà info_bar của Cài đặt hiện: {seen['page_bar']!r}"
    assert seen["main_notice"] == RESERVED_TEXT.format(path=shown(a)), "B1b: lỗi vẫn ở dải cửa sổ chính"
