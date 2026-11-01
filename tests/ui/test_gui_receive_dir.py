"""Đổi thư mục nhận từ cửa sổ chính, node thật trên 127.0.0.1 (spec-b1b mục 5, 6.5, 11.1, 11.2).

Viết từ spec, không từ code. A là GUI (`teleport.ui.app.main` trong tiến trình, fixture `gui` với
`keep_receive_dir=True`: app dùng `ReceiveDirChoice.SAVED`, chưa lưu gì thì `paths.default_receive_dir()`
đã vá thành `tmp_path / "nhan-gui"`, không bao giờ là Downloads thật). B (khi cần) là `Node` thật của
fixture `peer_node`, hai bên tin nhau sẵn (`trust_each_other`).

Đường vào: `GuiApp` là cha của khay (`run.tray.parent()`); `GuiApp.receive_dir_controller` là
`ReceiveDirController`; dải thông báo là `GuiApp.transfers.view` (`notice_text`, `notice_action`).
Hộp chọn thư mục đi qua seam `teleport.ui.receive_dir.dialog_factory`: một `QFileDialog` con không hiện
gì; test chọn thư mục bằng cách phát `fileSelected` rồi `finished` như hộp thật khi bấm chọn.
Chờ bằng điều kiện có hạn (`gui_wait`), không `sleep` cứng.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

pytestmark = pytest.mark.integration

LOCAL = "127.0.0.1"
KiB = 1024
MiB = 1024 * 1024

# ---- Câu của spec-b1b (2.8, 11.9) ----------------------------------------------------------------
SAVE_PREFIX = "Lưu vào: "
READY = "Sẵn sàng nhận"
NOT_RECEIVING = "Không nhận file"
NOT_RECEIVING_BAR = "Không nhận file: không mở được {path}"
BUSY_TEXT = "Chưa đổi được thư mục nhận vì đang nhận file. Hãy đổi lại sau khi các lượt nhận xong."
UNAVAILABLE_TEXT = "Không dùng được thư mục {path}. Hãy chọn thư mục khác mà bạn có quyền ghi."
STARTED_UNAVAILABLE_TEXT = (
    "Teleport đang không nhận file vì không mở được thư mục nhận {path}. Hãy kiểm tra ổ đĩa và quyền ghi "
    "rồi chọn lại thư mục đó, hoặc chọn thư mục khác."
)
TRAY_TEXT = "Teleport đang không nhận file vì không mở được thư mục nhận. Mở Teleport để chọn thư mục khác."
CHOOSE_RECEIVE_DIR = "Chọn thư mục nhận"  # nút của dải lúc started
CHOOSE_OTHER = "Chọn thư mục khác"  # nút của dải khi UNAVAILABLE


# =================================================================================================
# Hàm phụ trợ
# =================================================================================================


def same(a, b) -> bool:
    if a is None or b is None:
        return False
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def shown(path) -> str:
    from teleport.ui.send import printable

    return printable(str(path))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def saved_receive_dir(config: Path) -> str | None:
    from teleport.app.settings import Settings

    return Settings.load(config).receive_dir


class Dialogs:
    """Seam `receive_dir.dialog_factory`: `QFileDialog` con, `open()` không hiện gì."""

    def __init__(self) -> None:
        self.made: list = []

    def factory(self, parent=None, *args, **kwargs):
        from PySide6.QtWidgets import QFileDialog

        made = self.made

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


def app_of(run):
    app = run.tray.parent()
    assert app is not None and hasattr(app, "receive_dir_controller"), (
        "GuiApp phải có receive_dir_controller (spec-b1b 2.7)"
    )
    return app


def wait_running(run, gui_wait):
    """Kịch bản con: chờ node chạy và cửa sổ ở RUNNING (thư mục hỏng không được thành màn lỗi)."""
    from teleport.ui.main_window import ViewState

    yield gui_wait(
        lambda: run.windows and run.window.view_state in (ViewState.RUNNING, ViewState.ERROR),
        20,
        "cửa sổ RUNNING (hay ERROR)",
    )
    assert run.window.view_state is ViewState.RUNNING, "thư mục nhận hỏng không được làm app dừng ở màn lỗi"
    yield gui_wait(lambda: run.host.running, 5, "node chạy")


def select(dialog, path: Path) -> None:
    """Như hộp thật khi bấm chọn: `fileSelected` rồi `finished`."""
    dialog.fileSelected.emit(str(path))
    dialog.finished.emit(1)


def pick_with_button(run, dialogs, path: Path) -> None:
    before = len(dialogs.made)
    run.window.save_bar.change_button.click()
    assert len(dialogs.made) == before + 1, "bấm Đổi… phải mở một hộp chọn thư mục"
    dialog = dialogs.made[-1]
    assert dialog.opened == 1
    select(dialog, path)


def pick_with_notice_action(app, dialogs, path: Path) -> None:
    before = len(dialogs.made)
    action = app.transfers.view.notice_action
    assert action.isVisibleTo(app.transfers.view)
    action.click()
    assert len(dialogs.made) == before + 1, "nút của dải thông báo phải mở hộp chọn thư mục"
    select(dialogs.made[-1], path)


def a_port(run, gui_wait):
    got: list = []
    run.host.call(lambda node: node.port, got.append)
    yield gui_wait(lambda: got, 5, "cổng của A")
    assert isinstance(got[0], int), got[0]
    return got[0]


def connect_b(run, peer, gui_wait, a_id: str):
    port = yield from a_port(run, gui_wait)
    peer.soon(peer.node.connect_hint, a_id, [LOCAL], port)
    yield gui_wait(lambda: peer.connected_to(a_id), 10, "B nối tới A")


def a_finished(updates: list, transfer_id: str):
    for u in updates:
        if (
            type(u).__name__ == "TransferFinished"
            and u.info.key.transfer_id == transfer_id
            and str(u.result) != "suspended"
        ):
            return u
    return None


def offer_box_text(box) -> str:
    from PySide6.QtWidgets import QLabel

    return " ".join(w.text() for w in box.findChildren(QLabel) if w.isVisibleTo(box))


@pytest.fixture
def pair(config_dir, trust_each_other, tmp_path):
    """Hai thư mục cấu hình tin nhau: (a, b, a_id, src)."""

    def make():
        a, b = config_dir("A"), config_dir("B")
        id_a, _ = trust_each_other(a, b, name_a="May-A", name_b="May-B")
        src = tmp_path / "nguon"
        src.mkdir(exist_ok=True)
        return a, b, id_a.device_id, src

    return make


# =================================================================================================
# R1: đổi xong, file mới vào thư mục mới, settings.json lưu gốc thật
# =================================================================================================


def test_change_receive_dir_then_received_file_lands_in_new_dir_and_settings_saved(
    gui, gui_wait, pair, peer_node, dialogs, tmp_path, ui_helpers
):
    a, b, a_id, src = pair()
    peer = peer_node(b)
    f = src / "bao-cao.bin"
    f.write_bytes(os.urandom(300 * KiB))
    want = sha256_of(f)
    old_dir = tmp_path / "nhan-gui"
    new_dir = tmp_path / "nhan-moi"
    new_dir.mkdir()
    updates: list = []
    seen: dict = {}

    def script(run):
        yield from wait_running(run, gui_wait)
        run.host.transfer_event.connect(updates.append)
        app = app_of(run)
        c = app.receive_dir_controller
        seen["start"] = (c.receive_dir, c.unavailable, run.window.receive_dir)
        pick_with_button(run, dialogs, new_dir)
        seen["pending"] = (c.pending, c.changing, run.window.save_bar.full_text)
        yield gui_wait(lambda: not c.changing, 10, "đổi thư mục xong")
        seen["after"] = (c.receive_dir, c.unavailable, run.window.receive_dir)
        seen["bar"] = run.window.save_bar.full_text
        seen["notice"] = app.transfers.view.notice_text() or None
        seen["settings"] = saved_receive_dir(a)

        yield from connect_b(run, peer, gui_wait, a_id)
        fut = peer.submit(peer.node.transfers.send(a_id, [f]))
        ctl = app.transfers
        yield gui_wait(lambda: ctl.offers.keys(), 10, "hộp hỏi nhận file")
        [key] = ctl.offers.keys()
        box = ctl.offers.box(key)
        seen["box"] = offer_box_text(box)
        [accept] = ui_helpers.buttons_with_text(box, ui_helpers.M6_ACCEPT)
        accept.click()
        yield gui_wait(lambda: a_finished(updates, key.transfer_id), 30, "A nhận xong")
        seen["result"] = str(a_finished(updates, key.transfer_id).result)
        yield gui_wait(fut.done, 10, "send() của B trả về")
        run.tray.quit_requested.emit()

    run = gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert run.exec_calls == []
    rd, unavailable, window_rd = seen["start"]
    assert same(rd, old_dir) and unavailable is None and same(window_rd, old_dir), seen["start"]
    pending, changing, bar = seen["pending"]
    assert pending == new_dir and changing, seen["pending"]
    assert bar == f"Đang đổi sang {shown(new_dir)}…", bar
    rd, unavailable, window_rd = seen["after"]
    assert same(rd, new_dir) and unavailable is None and same(window_rd, new_dir), seen["after"]
    assert seen["bar"] == SAVE_PREFIX + shown(rd)
    assert seen["notice"] is None
    assert seen["settings"] is not None and os.path.isabs(seen["settings"])
    assert seen["settings"] == str(rd), "settings.json lưu gốc thật mà node trả về"
    assert same(seen["settings"], new_dir)
    assert shown(rd) in seen["box"], "hộp hỏi nhận file phải hiện thư mục mới"
    assert seen["result"] == "done"
    assert sha256_of(new_dir / f.name) == want
    assert not (old_dir / f.name).exists(), "file mới không được vào thư mục cũ"


# =================================================================================================
# R2: lần mở sau dùng thư mục đã đổi
# =================================================================================================


def test_changed_receive_dir_survives_restart(gui, gui_wait, config_dir, dialogs, tmp_path):
    a = config_dir("A")
    new_dir = tmp_path / "nhan-moi"
    new_dir.mkdir()
    seen: dict = {}

    def first(run):
        yield from wait_running(run, gui_wait)
        c = app_of(run).receive_dir_controller
        pick_with_button(run, dialogs, new_dir)
        yield gui_wait(lambda: not c.changing, 10, "đổi thư mục xong")
        seen["first"] = c.receive_dir
        run.tray.quit_requested.emit()

    def second(run):
        yield from wait_running(run, gui_wait)
        c = app_of(run).receive_dir_controller
        seen["second"] = (c.receive_dir, c.unavailable, run.window.receive_dir, run.host.receive_dir)
        seen["bar"] = run.window.save_bar.full_text
        seen["notice"] = app_of(run).transfers.view.notice_text() or None
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], first, keep_receive_dir=True)
    assert same(seen["first"], new_dir)
    gui(["--config-dir", str(a)], second, keep_receive_dir=True)
    rd, unavailable, window_rd, host_rd = seen["second"]
    assert same(rd, new_dir) and same(window_rd, new_dir) and same(host_rd, new_dir), seen["second"]
    assert unavailable is None
    assert seen["bar"] == SAVE_PREFIX + shown(window_rd)
    assert seen["notice"] is None


# =================================================================================================
# R3: thư mục đã lưu hỏng → vẫn chạy, không nhận file; đổi thì hết
# =================================================================================================


def _save_broken_dir(config: Path, tmp_path: Path) -> Path:
    from teleport.app.settings import Settings

    broken = tmp_path / "da-luu-la-file"
    broken.write_bytes(b"khong phai thu muc")
    Settings(receive_dir=str(broken)).save(config)
    return broken


def test_gui_saved_dir_is_a_file_runs_not_receiving_and_change_fixes_it(
    gui, gui_wait, config_dir, dialogs, tmp_path
):
    from teleport.ui.widgets import BadgeTone

    a = config_dir("A")
    broken = _save_broken_dir(a, tmp_path)
    good = tmp_path / "nhan-tot"
    good.mkdir()
    seen: dict = {}

    def script(run):
        yield from wait_running(run, gui_wait)
        app = app_of(run)
        c = app.receive_dir_controller
        w = run.window
        view = app.transfers.view
        yield gui_wait(lambda: view.notice_text() != "", 5, "dải thông báo thư mục hỏng")
        seen["host"] = (run.host.receive_dir, run.host.receive_dir_error)
        seen["ctl"] = (c.receive_dir, c.unavailable)
        seen["badge"] = (w.badge.text, w.badge.tone)
        seen["bar"] = (
            w.save_bar.isVisibleTo(w),
            w.save_bar.full_text,
            w.save_bar.open_button.isVisibleTo(w),
            w.save_bar.change_button.isVisibleTo(w),
        )
        seen["notice"] = (view.notice_text(), view.notice_action_text())
        seen["tray_before"] = [m for _, m in run.messages]
        pick_with_notice_action(app, dialogs, good)
        yield gui_wait(lambda: not c.changing and c.receive_dir is not None, 10, "đổi sang thư mục tốt")
        seen["after_ctl"] = (c.receive_dir, c.unavailable)
        seen["after_badge"] = (w.badge.text, w.badge.tone)
        seen["after_bar"] = (w.save_bar.full_text, w.save_bar.open_button.isVisibleTo(w))
        seen["after_notice"] = view.notice_text() or None
        run.tray.quit_requested.emit()

    run = gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert run.exec_calls == []
    host_rd, host_err = seen["host"]
    assert host_rd is None and same(host_err, broken), seen["host"]
    rd, unavailable = seen["ctl"]
    assert rd is None and same(unavailable, broken), seen["ctl"]
    assert seen["badge"] == (NOT_RECEIVING, BadgeTone.WARNING)
    bar_visible, bar_text, open_visible, change_visible = seen["bar"]
    assert bar_visible and change_visible and not open_visible, seen["bar"]
    assert bar_text == NOT_RECEIVING_BAR.format(path=shown(unavailable))
    notice, action = seen["notice"]
    assert notice == STARTED_UNAVAILABLE_TEXT.format(path=shown(unavailable))
    assert action == CHOOSE_RECEIVE_DIR
    assert TRAY_TEXT not in seen["tray_before"], "cửa sổ đang hiện: không cần bóng thoại khay"
    rd, unavailable = seen["after_ctl"]
    assert same(rd, good) and unavailable is None
    assert seen["after_badge"] == (READY, BadgeTone.SUCCESS)
    assert seen["after_bar"] == (SAVE_PREFIX + shown(rd), True)
    assert seen["after_notice"] is None, "đổi xong thì gỡ dải “không nhận file” (spec 11.1)"
    assert broken.read_bytes() == b"khong phai thu muc"
    assert same(saved_receive_dir(a), good)


def test_success_hides_unavailable_notice(gui, gui_wait, config_dir, dialogs, tmp_path):
    """Spec-b1b 11.1: đổi thẳng bằng nút Đổi… (không qua nút của dải) cũng gỡ dải lúc started."""
    a = config_dir("A")
    _save_broken_dir(a, tmp_path)
    good = tmp_path / "nhan-tot"
    good.mkdir()
    seen: dict = {}

    def script(run):
        yield from wait_running(run, gui_wait)
        app = app_of(run)
        c = app.receive_dir_controller
        view = app.transfers.view
        yield gui_wait(lambda: view.notice_text() != "", 5, "dải thông báo thư mục hỏng")
        seen["before"] = view.notice_text() or None
        pick_with_button(run, dialogs, good)
        yield gui_wait(lambda: not c.changing and c.receive_dir is not None, 10, "đổi xong")
        seen["after"] = view.notice_text() or None
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert seen["before"] is not None and seen["before"].startswith("Teleport đang không nhận file")
    assert seen["after"] is None


def test_success_hides_previous_change_error_notice(gui, gui_wait, config_dir, dialogs, tmp_path):
    a = config_dir("A")
    not_a_dir = tmp_path / "chon-nham-file"
    not_a_dir.write_bytes(b"x")
    good = tmp_path / "nhan-tot"
    good.mkdir()
    seen: dict = {}

    def script(run):
        yield from wait_running(run, gui_wait)
        app = app_of(run)
        c = app.receive_dir_controller
        view = app.transfers.view
        seen["start_notice"] = view.notice_text() or None
        pick_with_button(run, dialogs, not_a_dir)
        yield gui_wait(lambda: not c.changing and view.notice_text() != "", 10, "báo lỗi đổi")
        seen["error"] = (view.notice_text(), view.notice_action_text(), c.receive_dir)
        pick_with_notice_action(app, dialogs, good)
        yield gui_wait(lambda: not c.changing and same(c.receive_dir, good), 10, "đổi sang thư mục tốt")
        seen["after"] = view.notice_text() or None
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert seen["start_notice"] is None
    text, action, rd = seen["error"]
    assert text == UNAVAILABLE_TEXT.format(path=shown(not_a_dir))
    assert action == CHOOSE_OTHER
    assert same(rd, tmp_path / "nhan-gui"), "lỗi: thư mục nhận giữ nguyên"
    assert seen["after"] is None
    assert not_a_dir.read_bytes() == b"x"


def test_success_does_not_hide_unrelated_notice(gui, gui_wait, config_dir, dialogs, tmp_path):
    """Spec-b1b 11.1: chỉ gỡ dải khi nó đang hiện đúng câu về thư mục nhận."""
    a = config_dir("A")
    good = tmp_path / "nhan-tot"
    good.mkdir()
    other = "Một thông báo khác về lượt gửi."
    seen: dict = {}

    def script(run):
        yield from wait_running(run, gui_wait)
        app = app_of(run)
        c = app.receive_dir_controller
        app.transfers.view.show_notice(other)
        pick_with_button(run, dialogs, good)
        yield gui_wait(lambda: not c.changing and same(c.receive_dir, good), 10, "đổi xong")
        seen["after"] = app.transfers.view.notice_text() or None
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert seen["after"] == other


def test_saved_dir_broken_in_background_shows_tray_message(gui, gui_wait, config_dir, tmp_path):
    """Spec-b1b 2.7: chạy nền (cửa sổ không hiện) thì thêm bóng thoại khay."""
    a = config_dir("A")
    _save_broken_dir(a, tmp_path)
    seen: dict = {}

    def script(run):
        yield from wait_running(run, gui_wait)
        yield gui_wait(lambda: any(m == TRAY_TEXT for _, m in run.messages), 5, "bóng thoại khay")
        seen["visible"] = run.window.isVisible()
        seen["notice"] = app_of(run).transfers.view.notice_text() or None
        run.tray.quit_requested.emit()

    run = gui(["--config-dir", str(a), "--background"], script, keep_receive_dir=True)
    assert seen["visible"] is False
    assert seen["notice"] is not None and seen["notice"].startswith("Teleport đang không nhận file")
    assert [m for _, m in run.messages].count(TRAY_TEXT) == 1


# =================================================================================================
# R4: đổi khi đang nhận → BUSY
# =================================================================================================


def test_change_while_receiving_shows_busy_notice(
    gui, gui_wait, pair, peer_node, dialogs, tmp_path, ui_helpers
):
    a, b, a_id, src = pair()
    peer = peer_node(b)
    f = src / "lon.bin"
    f.write_bytes(os.urandom(4 * MiB))
    old_dir = tmp_path / "nhan-gui"
    new_dir = tmp_path / "nhan-moi"
    new_dir.mkdir()
    updates: list = []
    seen: dict = {}

    async def slow_sender() -> None:
        peer.node.transfers.rate_limit = 128 * KiB  # ~32 s cho 4 MiB: còn đang nhận khi đổi

    def receiving(tid: str) -> bool:
        return any(
            type(u).__name__ == "TransferProgress" and u.info.key.transfer_id == tid and u.shown_bytes > 0
            for u in updates
        )

    def script(run):
        yield from wait_running(run, gui_wait)
        run.host.transfer_event.connect(updates.append)
        app = app_of(run)
        c = app.receive_dir_controller
        view = app.transfers.view
        peer.run(slow_sender(), 5)
        yield from connect_b(run, peer, gui_wait, a_id)
        peer.submit(peer.node.transfers.send(a_id, [f]))
        ctl = app.transfers
        yield gui_wait(lambda: ctl.offers.keys(), 10, "hộp hỏi nhận file")
        [key] = ctl.offers.keys()
        [accept] = ui_helpers.buttons_with_text(ctl.offers.box(key), ui_helpers.M6_ACCEPT)
        accept.click()
        yield gui_wait(lambda: receiving(key.transfer_id), 15, "A đang nhận")
        pick_with_button(run, dialogs, new_dir)
        yield gui_wait(lambda: not c.changing, 10, "đổi thư mục kết thúc")
        seen["notice"] = view.notice_text() or None
        seen["ctl"] = (c.receive_dir, c.pending)
        seen["bar"] = run.window.save_bar.full_text
        seen["finished_before"] = a_finished(updates, key.transfer_id) is not None
        ctl.cancel(key)
        yield gui_wait(lambda: a_finished(updates, key.transfer_id), 15, "lượt bị hủy kết thúc")
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert seen["finished_before"] is False, "lượt phải còn đang nhận lúc đổi (test không hợp lệ)"
    assert seen["notice"] == BUSY_TEXT
    rd, pending = seen["ctl"]
    assert same(rd, old_dir) and pending is None, seen["ctl"]
    assert seen["bar"] == SAVE_PREFIX + shown(rd)
    assert saved_receive_dir(a) in (None, str(rd)), "BUSY: không lưu thư mục mới"
    assert not any(new_dir.iterdir()), "BUSY: không ghi gì vào thư mục mới"
