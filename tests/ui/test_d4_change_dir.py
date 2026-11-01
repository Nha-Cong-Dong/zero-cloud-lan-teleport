"""Nút “Đổi thư mục nhận” trên bong bóng nhận bị lỗi (spec D4 mục 5.2, mục 12 B4; 14.5, 14.13 thắng).

Viết từ spec, không từ code. `ChatView` đứng riêng (như `test_chat_bubbles.py`), sự kiện lượt đẩy bằng
`ChatView.handle`. Ca nối vào app (một hộp chọn thư mục dù bấm hai lần, đổi xong thì nút ẩn) chạy `main()`
trong tiến trình với node thật (`integration`), hộp chọn thư mục qua seam `receive_dir.dialog_factory`.

Luật: nút hiện khi chiều RECEIVE, kết thúc với REJECTED_NO_SPACE / REJECTED_FILE_TOO_LARGE / DISK_FAILED /
RECEIVE_DIR_MISSING (14.13), là bong bóng của phiên (không phải `HistoryBubble`), và
`finished_receive_dir` (thư mục nhận lúc lượt kết thúc) khác None và bằng thư mục hiện tại (14.5). Câu kết
quả của bong bóng đã kết thúc nêu thư mục lúc kết thúc, không phải thư mục hiện tại (14.5).
"""

from __future__ import annotations

import datetime

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

PEER_ID = "a71c04d2" + "e" * 24
OTHER_ID = "0f0f0f0f" + "1" * 24
PEER = "PC-Nha"
MiB = 2**20

CHANGE_VI = "Đổi thư mục nhận"
CHANGE_EN = "Change receive folder"

WITH_BUTTON = ("rejected_no_space", "rejected_file_too_large", "disk_failed", "receive_dir_missing")
WITHOUT_BUTTON = (
    "done",
    "cancelled_user",
    "rejected_user",
    "expired",
    "source_changed",
    "protocol_error",
    "temp_exists",
)


def key(direction: str = "receive", transfer_id: str = "c" * 32, device_id: str = PEER_ID):
    from teleport.core.transfer import Direction, TransferKey

    return TransferKey(device_id, Direction(direction), transfer_id)


def info(k, status: str = "transferring"):
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import TransferInfo, TransferStatus

    return TransferInfo(
        key=k,
        status=TransferStatus(status),
        files=(FileEntry("video.mp4", 10 * MiB, 1_700_000_000_000),),
        names=("video.mp4",),
        file_index=0,
        done_bytes=0,
        total_bytes=10 * MiB,
    )


def finished(k, result: str):
    from teleport.app.transfers import TransferFinished, TransferResult

    return TransferFinished(
        info=info(k),
        result=TransferResult(result),
        by_peer=False,
        reason=None,
        file_index=0,
        reject=None,
    )


def dview(device_id: str, name: str):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id,
        name=name,
        os="windows",
        paired=True,
        online=True,
        connected=True,
        addresses=("192.0.2.7",),
        port=47474,
    )


def settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


def shown(view, bubble) -> bool:
    button = getattr(bubble, "change_dir_button", None)
    return button is not None and button.isVisibleTo(view)


@pytest.fixture
def view(qtbot, tmp_path):
    from teleport.ui.chat import ChatView
    from teleport.ui.radar import DeviceListModel

    devices = DeviceListModel()
    devices.reset([dview(PEER_ID, PEER), dview(OTHER_ID, "Laptop")])
    v = ChatView(devices)
    v._test_devices = devices  # type: ignore[attr-defined]  # giữ model sống cùng view
    qtbot.addWidget(v)
    v.set_device_names({PEER_ID: PEER, OTHER_ID: "Laptop"})
    v.set_device(PEER_ID)
    v.resize(720, 560)
    v.show()
    qtbot.waitExposed(v)
    v.requests = []  # type: ignore[attr-defined]
    v.change_receive_dir_requested.connect(lambda: v.requests.append(1))
    return v


@pytest.fixture
def dirs(tmp_path):
    a, b = tmp_path / "nhan-a", tmp_path / "nhan-b"
    a.mkdir()
    b.mkdir()
    return a, b


# =================================================================================================
# Khi nào có nút
# =================================================================================================


@pytest.mark.parametrize("result", WITH_BUTTON)
def test_receive_failure_shows_change_dir_button(view, dirs, result):
    rd, _ = dirs
    view.set_receive_dir(rd)
    k = key("receive")
    view.handle(finished(k, result))
    settle()
    bubble = view.row(k)
    assert bubble is not None
    assert bubble.finished_receive_dir == rd
    assert shown(view, bubble)
    button = bubble.change_dir_button
    assert button.text().replace("&", "") == CHANGE_VI
    assert button.accessibleName(), "nút có tên trợ năng"
    assert "video.mp4" in button.accessibleDescription(), "mô tả trợ năng là tiêu đề lượt (14.13)"


@pytest.mark.parametrize("result", WITHOUT_BUTTON)
def test_other_receive_results_have_no_button(view, dirs, result):
    rd, _ = dirs
    view.set_receive_dir(rd)
    k = key("receive")
    view.handle(finished(k, result))
    settle()
    assert not shown(view, view.row(k))


@pytest.mark.parametrize("result", WITH_BUTTON)
def test_send_side_never_has_change_dir_button(view, dirs, result):
    rd, _ = dirs
    view.set_receive_dir(rd)
    k = key("send")
    view.handle(finished(k, result))
    settle()
    assert not shown(view, view.row(k))


def test_unfinished_receive_has_no_button(view, dirs):
    from teleport.app.transfers import TransferStatusChanged

    rd, _ = dirs
    view.set_receive_dir(rd)
    k = key("receive")
    view.handle(TransferStatusChanged(info(k)))
    settle()
    assert not shown(view, view.row(k))


def test_no_receive_dir_at_finish_means_no_button(view):
    """14.5: `finished_receive_dir` None → không nút (chưa biết thư mục nào đã hỏng)."""
    k = key("receive")
    view.handle(finished(k, "disk_failed"))
    settle()
    bubble = view.row(k)
    assert bubble.finished_receive_dir is None
    assert not shown(view, bubble)


def test_history_bubble_never_has_change_dir_button(view, qtbot, dirs):
    from teleport.core.history import HistoryEntry
    from teleport.ui.chat_bubbles import HistoryBubble

    rd, _ = dirs
    view.set_receive_dir(rd)
    entry = HistoryEntry(
        direction="receive",
        device_id=PEER_ID,
        device_name=PEER,
        transfer_id="d" * 32,
        file_count=1,
        total_bytes=10 * MiB,
        done_bytes=0,
        names=("video.mp4",),
        result="disk_failed",
        finished_at=datetime.datetime(2026, 10, 30, 9, 0, tzinfo=datetime.UTC),
        saved_path=None,
    )
    hb = HistoryBubble(entry, receive_dir=rd)
    qtbot.addWidget(hb)
    hb.show()
    button = getattr(hb, "change_dir_button", None)
    assert button is None or not button.isVisibleTo(hb)
    texts = [b.text().replace("&", "") for b in hb.findChildren(type(hb.open_button)) if b.isVisibleTo(hb)]
    assert CHANGE_VI not in texts


# =================================================================================================
# 14.5: thư mục lúc kết thúc
# =================================================================================================


def test_button_hides_after_receive_dir_changed_and_returns_when_back(view, dirs):
    a, b = dirs
    view.set_receive_dir(a)
    k = key("receive")
    view.handle(finished(k, "rejected_no_space"))
    settle()
    bubble = view.row(k)
    assert shown(view, bubble)
    view.set_receive_dir(b)
    settle()
    assert not shown(view, bubble), "đã đổi sang thư mục khác: nút ẩn"
    assert bubble.finished_receive_dir == a
    view.set_receive_dir(a)
    settle()
    assert shown(view, bubble), "nút hiện khi thư mục hiện tại bằng thư mục lúc kết thúc"


def test_set_receive_dir_reaches_every_session_bubble(view, dirs):
    """`ChatView.set_receive_dir` gọi `set_current_receive_dir` cho mọi bong bóng của phiên, kể cả ở
    luồng không đang mở."""
    a, b = dirs
    view.set_receive_dir(a)
    k1 = key("receive", "1" * 32, PEER_ID)
    k2 = key("receive", "2" * 32, OTHER_ID)
    view.handle(finished(k1, "disk_failed"))
    view.handle(finished(k2, "receive_dir_missing"))
    view.set_receive_dir(b)
    settle()
    for k in (k1, k2):
        assert not view.row(k).change_dir_button.isVisibleTo(view.row(k))
    view.set_receive_dir(a)
    settle()
    assert view.row(k2).change_dir_button.isVisibleTo(view.row(k2)), "luồng đang không mở cũng cập nhật"


def test_set_current_receive_dir_on_bubble_directly(view, dirs):
    a, b = dirs
    view.set_receive_dir(a)
    k = key("receive")
    view.handle(finished(k, "disk_failed"))
    bubble = view.row(k)
    bubble.set_current_receive_dir(b)
    assert bubble.change_dir_button.isHidden()
    bubble.set_current_receive_dir(a)
    assert not bubble.change_dir_button.isHidden()


@pytest.mark.parametrize("result", ["disk_failed", "receive_dir_missing"])
def test_finished_text_names_dir_at_finish_not_current_dir(view, dirs, result):
    """14.5: câu DISK_FAILED / RECEIVE_DIR_MISSING của bong bóng đã kết thúc nêu thư mục lúc kết thúc."""
    a, b = dirs
    view.set_receive_dir(a)
    k = key("receive")
    view.handle(finished(k, result))
    bubble = view.row(k)
    view.set_receive_dir(b)
    view.set_device_names({PEER_ID: PEER + "-2"})  # buộc vẽ lại chữ mọi bong bóng
    settle()
    detail = bubble.detail_label.text()
    assert str(a) in detail, detail
    assert str(b) not in detail, detail


# =================================================================================================
# Bấm, Tab, ngôn ngữ
# =================================================================================================


def test_click_emits_bubble_and_view_signals_each_time(view, dirs, qtbot):
    a, _ = dirs
    view.set_receive_dir(a)
    k = key("receive")
    view.handle(finished(k, "disk_failed"))
    bubble = view.row(k)
    got: list[int] = []
    bubble.change_dir_clicked.connect(lambda: got.append(1))
    bubble.change_dir_button.click()
    bubble.change_dir_button.click()
    assert got == [1, 1], "không giới hạn số lần bấm"
    assert view.requests == [1, 1]
    assert shown(view, bubble), "bấm xong nút vẫn còn (khác “Gửi lại”)"


def test_change_dir_button_is_in_bubble_tab_chain_after_resend(view, dirs):
    a, _ = dirs
    view.set_receive_dir(a)
    k = key("receive")
    view.handle(finished(k, "rejected_file_too_large"))
    bubble = view.row(k)
    chain = bubble.focus_buttons()
    assert bubble.change_dir_button in chain
    candidates = [bubble.cancel_button, bubble.open_button, bubble.resend_button, bubble.change_dir_button]
    order = [w for w in chain if w in candidates]
    assert order.index(bubble.change_dir_button) == len(order) - 1, "ngay sau resend_button"


def test_change_dir_button_sits_in_button_row_with_open_and_resend(view, dirs):
    a, _ = dirs
    view.set_receive_dir(a)
    k = key("receive")
    view.handle(finished(k, "disk_failed"))
    bubble = view.row(k)
    assert bubble.change_dir_button.parentWidget() is bubble.open_button.parentWidget()
    assert bubble.change_dir_button.parentWidget() is bubble.resend_button.parentWidget()


def test_change_dir_button_retranslates(view, dirs, qapp):
    from teleport.ui import i18n

    a, _ = dirs
    view.set_receive_dir(a)
    k = key("receive")
    view.handle(finished(k, "disk_failed"))
    bubble = view.row(k)
    i18n.set_language("en")
    qapp.processEvents()
    assert bubble.change_dir_button.text().replace("&", "") == CHANGE_EN
    i18n.set_language("vi")
    qapp.processEvents()
    assert bubble.change_dir_button.text().replace("&", "") == CHANGE_VI


# =================================================================================================
# Nối vào app (node thật)
# =================================================================================================


class Dialogs:
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


@pytest.mark.integration
def test_gui_change_dir_button_opens_one_dialog_and_hides_after_change(
    gui, gui_wait, config_dir, monkeypatch, tmp_path
):
    from teleport.ui import receive_dir as rd_mod
    from teleport.ui.main_window import ViewState

    dialogs = Dialogs()
    monkeypatch.setattr(rd_mod, "dialog_factory", dialogs.factory)
    d = config_dir("A")
    new_dir = tmp_path / "nhan-moi"
    new_dir.mkdir()
    seen: dict = {}

    def script(run):
        yield gui_wait(
            lambda: (
                run.windows and run.hosts and run.host.running and run.window.view_state is ViewState.RUNNING
            ),
            20,
            "node chạy",
        )
        app = run.tray.parent()
        c = app.receive_dir_controller
        yield gui_wait(lambda: c.receive_dir is not None, 5, "có thư mục nhận")
        view = app.transfers.view
        k = key("receive", "e" * 32)
        app.transfers.on_transfer(finished(k, "disk_failed"))
        bubble = view.row(k)
        seen["shown"] = bubble.change_dir_button.isVisibleTo(bubble)
        bubble.change_dir_button.click()
        bubble.change_dir_button.click()
        yield
        seen["dialogs"] = len(dialogs.made)
        dialog = dialogs.made[-1]
        dialog.fileSelected.emit(str(new_dir))
        dialog.finished.emit(1)
        yield gui_wait(
            lambda: (
                not c.changing and c.receive_dir is not None and c.receive_dir != bubble.finished_receive_dir
            ),
            10,
            "đổi thư mục xong",
        )
        yield gui_wait(lambda: bubble.change_dir_button.isHidden(), 3, "nút ẩn sau khi đổi")
        run.tray.actions()[3].trigger()

    run = gui(["--config-dir", str(d)], script, keep_receive_dir=True)
    assert run.code == 0
    assert seen["shown"]
    assert seen["dialogs"] == 1, "bấm hai lần: hộp đang mở chỉ được đưa lên trước"
