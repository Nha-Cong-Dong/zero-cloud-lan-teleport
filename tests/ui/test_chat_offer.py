"""Bong bóng lời mời nhận file trong luồng chat (spec B4 mục 3.5, mục 7 T-C; mục 11 thắng khi khác).

Viết từ spec, không từ code. `ChatView` đứng riêng giữa một nút “Trước” (để Tab vào/ra), nối với một
`OfferBook` thật bằng `set_offer_book`. Thứ tự gọi như `TransfersController` (spec 3.3): `view.handle`
rồi `book.add`; `TransferFinished` thì `book.finish` trước `view.handle`. Bong bóng không tự gọi
`book.answer`: test đóng vai controller khi cần (nối `offer_answer_requested` → `book.answer`).
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

PEER_ID = "a71c04d2" + "e" * 24
OTHER_ID = "0f0f0f0f" + "1" * 24
PEER = "PC-Nha"
OTHER = "Laptop-Minh"
NOW = datetime.datetime(2026, 10, 31, 10, 40).timestamp()

ACCEPT = "Nhận"
REJECT = "Từ chối"
CHANGE = "Đổi…"
ALWAYS = "Luôn nhận từ thiết bị này"
SUMMARY_HINT = "Lời mời nhận file. Dùng Tab để tới nút Nhận hay Từ chối."
MORE = "… và {k} file khác"
CANCELLED = "Đã hủy"
LIST_MAX = 10
EN = {
    "accept": "Accept",
    "reject": "Decline",
    "change": "Change…",
    "always": "Always accept from this device",
}


# =================================================================================================
# Hàm phụ trợ (định nghĩa tại chỗ: không import conftest)
# =================================================================================================


def settle(qtbot=None, ms: int = 0) -> None:
    for _ in range(3):
        QApplication.processEvents()
    if qtbot is not None and ms:
        qtbot.wait(ms)


def tid(i: int) -> str:
    return f"{i:032x}"


def tkey(i: int = 1, device_id: str = PEER_ID, direction: str = "receive"):
    from teleport.core.transfer import Direction, TransferKey

    return TransferKey(device_id, Direction(direction), tid(i))


def make_offer(key, *, name: str | None = PEER, names=("video.mp4",)):
    from teleport.app.transfers import OfferReceived
    from teleport.core.messages import FileEntry

    files = tuple(FileEntry(f"f{i}.bin", 1000, 1_700_000_000_000) for i in range(len(names)))
    return OfferReceived(
        key=key,
        device_name=name,
        files=files,
        names=tuple(names),
        total_bytes=1000 * len(names),
        auto_accepted=False,
    )


def finished(key, result: str, *, by_peer: bool = False):
    from teleport.app.transfers import TransferFinished, TransferResult
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import TransferInfo, TransferStatus

    info = TransferInfo(
        key=key,
        status=TransferStatus.WAITING,
        files=(FileEntry("video.mp4", 1000, 0),),
        names=("video.mp4",),
        file_index=0,
        done_bytes=0,
        total_bytes=1000,
    )
    return TransferFinished(
        info=info, result=TransferResult(result), by_peer=by_peer, reason=None, file_index=None, reject=None
    )


def status_changed(key, status: str = "transferring"):
    from teleport.app.transfers import TransferStatusChanged
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import TransferInfo, TransferStatus

    return TransferStatusChanged(
        TransferInfo(
            key=key,
            status=TransferStatus(status),
            files=(FileEntry("a.bin", 1000, 0),),
            names=("a.bin",),
            file_index=0,
            done_bytes=0,
            total_bytes=1000,
        )
    )


def dview(device_id: str = PEER_ID, name: str = PEER, *, online: bool = True):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id,
        name=name,
        os="windows",
        paired=True,
        online=online,
        connected=online,
        addresses=("192.168.1.20",) if online else (),
        port=47474 if online else None,
    )


def panel_of(bubble):
    """`OfferPanel` còn sống và chưa bị gỡ của bong bóng, hay None."""
    import shiboken6

    if bubble is None or not shiboken6.isValid(bubble):
        return None
    for w in bubble.findChildren(QWidget, "offerPanel"):
        if shiboken6.isValid(w) and not w.isHidden():
            return w
    return None


def plain(text: str) -> str:
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


class Harness:
    """`ChatView` + `OfferBook` thật; ghi lại signal mới của B4."""

    def __init__(self, qtbot, receive_dir: Path) -> None:
        from teleport.ui.chat import ChatView
        from teleport.ui.offer import OfferBook
        from teleport.ui.radar import DeviceListModel

        self.host = QWidget()
        qtbot.addWidget(self.host)
        self.devices = DeviceListModel(self.host)
        self.devices.reset([dview(), dview(OTHER_ID, OTHER)])
        self.before = QPushButton("Trước", self.host)
        self.view = ChatView(self.devices, self.host)
        lay = QVBoxLayout(self.host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.before)
        lay.addWidget(self.view, 1)
        self.host.resize(720, 560)
        self.book = OfferBook(self.host)
        v = self.view
        v.set_device_names({PEER_ID: PEER, OTHER_ID: OTHER})
        v.set_connected(PEER_ID, True)
        v.set_connected(OTHER_ID, True)
        v.set_receive_dir(receive_dir)
        v.set_offer_book(self.book)
        self.answers: list[tuple] = []
        self.toggles: list[tuple] = []
        self.changes: list[int] = []
        self.cancels: list = []
        v.offer_answer_requested.connect(lambda k, a: self.answers.append((k, a)))
        v.offer_always_toggled.connect(lambda k, on: self.toggles.append((k, on)))
        v.change_receive_dir_requested.connect(lambda: self.changes.append(1))
        v.cancel_requested.connect(self.cancels.append)

    def show(self, qtbot, *, active: bool = False) -> None:
        self.host.show()
        qtbot.waitExposed(self.host)
        if active:
            self.host.activateWindow()
            self.host.raise_()
            qtbot.waitUntil(self.host.isActiveWindow, timeout=3000)
        settle()

    def offer(self, key, **kw):
        """Như controller: dựng bong bóng rồi thêm vào book. Trả bong bóng."""
        o = make_offer(key, **kw)
        self.view.handle(o)
        assert self.book.add(o)
        settle()
        return self.view.row(key)

    def finish(self, key, result: str, *, by_peer: bool = False) -> None:
        self.book.finish(key)
        self.view.handle(finished(key, result, by_peer=by_peer))
        settle()


@pytest.fixture
def clock(monkeypatch):
    from teleport.ui import chat

    monkeypatch.setattr(chat, "_wall_now", lambda: NOW)


@pytest.fixture
def h(qtbot, clock, tmp_path):
    harness = Harness(qtbot, tmp_path / "nhan")
    harness.show(qtbot, active=True)
    harness.view.set_device(PEER_ID)
    settle(qtbot, 10)
    yield harness
    harness.book.clear()
    settle()


# =================================================================================================
# C1 bong bóng có panel
# =================================================================================================


def test_c1_added_offer_gets_panel_attention_and_no_cancel(h):
    from teleport.ui.chat_bubbles import OfferPanel
    from teleport.ui.widgets import BubbleEmphasis

    k = tkey(1)
    b = h.offer(k)
    assert b is not None
    assert b.key == k
    panel = panel_of(b)
    assert isinstance(panel, OfferPanel)
    assert panel.isVisibleTo(h.view)
    assert b.emphasis is BubbleEmphasis.ATTENTION
    assert not b.cancel_button.isVisibleTo(h.view), "lời mời đang chờ: không có nút Hủy"
    assert plain(panel.accept_button.text()) == ACCEPT
    assert plain(panel.reject_button.text()) == REJECT
    assert plain(panel.change_button.text()) == CHANGE
    assert plain(panel.always_check.text()) == ALWAYS
    assert SUMMARY_HINT in b.accessibleName()


def test_c1_accept_is_primary_and_buttons_click_focus_only(h):
    from teleport.ui import theme

    panel = panel_of(h.offer(tkey(1)))
    assert panel.accept_button.property(theme.PRIMARY_PROPERTY)
    for w in (panel.change_button, panel.always_check, panel.reject_button, panel.accept_button):
        assert w.focusPolicy() == Qt.FocusPolicy.ClickFocus, w


def test_c1_book_add_alone_creates_bubble_with_panel(h):
    """Spec 11.2: `ChatView` `_ensure` bong bóng khi `added` (kể cả khi `view.handle` chưa chạy)."""
    k = tkey(2)
    assert h.book.add(make_offer(k))
    settle()
    assert panel_of(h.view.row(k)) is not None


def test_c1_save_line_follows_receive_dir_and_pending(h, tmp_path):
    from teleport.ui.send import printable

    panel = panel_of(h.offer(tkey(1)))
    new = tmp_path / "moi"
    h.view.set_receive_dir(new)
    assert panel.save_label.text() == f"Lưu vào: {printable(str(new))}"
    h.view.set_receive_dir_pending(tmp_path / "dang-doi")
    assert panel.save_label.text() == f"Đang đổi sang {printable(str(tmp_path / 'dang-doi'))}…"
    assert not panel.accept_button.isEnabled(), "đang đổi thư mục: Nhận tắt"
    assert panel.reject_button.isEnabled()
    h.view.set_receive_dir_pending(None)
    assert panel.accept_button.isEnabled()


def test_c1_change_and_tick_emit_view_signals(h):
    k = tkey(1)
    panel = panel_of(h.offer(k))
    panel.change_button.click()
    assert h.changes == [1]
    panel.always_check.click()
    assert h.toggles == [(k, True)]
    assert h.answers == []


# =================================================================================================
# C2 bấm Nhận
# =================================================================================================


def test_c2_accept_disables_both_buttons_before_emitting_then_panel_removed(h, qtbot):
    from teleport.ui.widgets import BubbleEmphasis

    k = tkey(1)
    b = h.offer(k)
    panel = panel_of(b)
    at_emit: list[tuple[bool, bool]] = []
    h.view.offer_answer_requested.connect(
        lambda *_: at_emit.append((panel.accept_button.isEnabled(), panel.reject_button.isEnabled()))
    )
    panel.accept_button.click()
    assert h.answers == [(k, True)]
    assert at_emit == [(False, False)], "hai nút tắt ngay, trước khi phát"
    assert QApplication.focusWidget() is b, "spec 11.8: tiêu điểm về bong bóng"
    assert h.book.answer(k, True)  # controller
    qtbot.waitUntil(lambda: panel_of(b) is None, timeout=2000)
    assert b.cancel_button.isVisibleTo(h.view), "ANSWERED: nút ✕ theo luật B3 (chưa kết thúc → hiện)"
    assert b.emphasis is not BubbleEmphasis.ATTENTION
    assert SUMMARY_HINT not in b.accessibleName()


def test_c2_reject_emits_false(h):
    k = tkey(1)
    panel_of(h.offer(k)).reject_button.click()
    assert h.answers == [(k, False)]


def test_c2_attention_survives_rename_and_reconnect(h):
    """Spec 11.1: `render_status` tính lại emphasis; đổi tên / kết nối không làm mất ATTENTION."""
    from teleport.ui.widgets import BubbleEmphasis

    b = h.offer(tkey(1))
    h.view.set_device_names({PEER_ID: "Ten-Moi", OTHER_ID: OTHER})
    assert b.emphasis is BubbleEmphasis.ATTENTION
    h.view.set_connected(PEER_ID, False)
    assert b.emphasis is BubbleEmphasis.ATTENTION
    h.view.set_connected(PEER_ID, True)
    assert b.emphasis is BubbleEmphasis.ATTENTION
    assert not b.cancel_button.isVisibleTo(h.view)


def test_c2_answered_bubble_then_running_has_no_panel(h, qtbot):
    k = tkey(1)
    b = h.offer(k)
    h.book.answer(k, True)
    h.view.handle(status_changed(k, "transferring"))
    qtbot.waitUntil(lambda: panel_of(b) is None, timeout=2000)
    assert b.cancel_button.isVisibleTo(h.view)


def test_c2_cancel_button_click_with_panel_is_ignored(h):
    """Spec 11.6: `_on_cancel` bỏ qua khi có panel (đường trả lời thứ ba)."""
    k = tkey(1)
    b = h.offer(k)
    b.cancel_button.click()
    assert h.cancels == []


# =================================================================================================
# C3 máy gửi hủy
# =================================================================================================


def test_c3_peer_cancel_removes_panel_and_shows_cancelled(h, qtbot):
    from teleport.ui.widgets import BubbleEmphasis

    k = tkey(1)
    b = h.offer(k)
    h.finish(k, "cancelled_other", by_peer=True)
    qtbot.waitUntil(lambda: panel_of(b) is None, timeout=2000)
    assert CANCELLED in b.status_label.text()
    assert b.emphasis is BubbleEmphasis.NONE
    assert not b.cancel_button.isVisibleTo(h.view), "đã kết thúc: không có ✕"


@pytest.mark.parametrize("result", ["expired", "unpaired", "rejected_no_space"])
def test_c3_any_finish_removes_panel(h, qtbot, result):
    k = tkey(1)
    b = h.offer(k)
    h.finish(k, result)
    qtbot.waitUntil(lambda: panel_of(b) is None, timeout=2000)
    assert h.answers == []


# =================================================================================================
# C4 tiêu điểm, roving Tab
# =================================================================================================


def test_c4_focus_buttons_are_change_tick_reject_accept(h):
    b = h.offer(tkey(1))
    panel = panel_of(b)
    expected = [panel.change_button, panel.always_check, panel.reject_button, panel.accept_button]
    assert panel.focus_widgets() == expected
    assert b.focus_buttons() == expected


def test_c4_tab_from_offer_bubble_walks_panel_then_send_button(h):
    b = h.offer(tkey(1))
    panel = panel_of(b)
    h.before.setFocus(Qt.FocusReason.OtherFocusReason)
    settle()
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
    settle()
    assert QApplication.focusWidget() is b, "Tab vào luồng: bong bóng mới nhất"
    for expected in (panel.change_button, panel.always_check, panel.reject_button, panel.accept_button):
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
        settle()
        assert QApplication.focusWidget() is expected, (QApplication.focusWidget(), expected)
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
    settle()
    assert QApplication.focusWidget() is h.view.composer.send_button


def test_c4_disabled_accept_drops_out_of_focus_list(h, tmp_path):
    """`focus_widgets()`: chỉ widget hiện và bật (spec 3.5)."""
    panel = panel_of(h.offer(tkey(1)))
    h.view.set_receive_dir_pending(tmp_path / "moi")
    assert panel.accept_button not in panel.focus_widgets()
    assert panel.reject_button in panel.focus_widgets()


# =================================================================================================
# C5 danh sách file
# =================================================================================================


def test_c5_many_files_lists_ten_names_then_more(h):
    names = tuple(f"tep-{i:02}.bin" for i in range(12))
    panel = panel_of(h.offer(tkey(1), names=names))
    label = panel.files_label
    assert label.isVisibleTo(h.view)
    assert label.textFormat() == Qt.TextFormat.PlainText
    text = label.text()
    for i in range(LIST_MAX):
        assert f"tep-{i:02}.bin" in text
    assert "tep-10.bin" not in text and "tep-11.bin" not in text
    assert MORE.format(k=2) in text
    assert " · " in text
    assert label.property("tone") == "muted"


def test_c5_ten_files_no_more_line(h):
    names = tuple(f"tep-{i:02}.bin" for i in range(LIST_MAX))
    text = panel_of(h.offer(tkey(1), names=names)).files_label.text()
    assert "file khác" not in text


def test_c5_single_file_has_no_files_label(h):
    panel = panel_of(h.offer(tkey(1), names=("video.mp4",)))
    assert not panel.files_label.isVisibleTo(h.view)


def test_c5_hostile_names_in_panel_go_through_file_label(h):
    from teleport.ui.texts import file_label

    names = ("<b>x</b>‮gnp.exe", "dong1\ndong2.txt", "z" * 255)
    panel = panel_of(h.offer(tkey(1), names=names))
    text = panel.files_label.text()
    for n in names:
        assert file_label(n) in text
    assert "‮" not in text and "dong1\ndong2" not in text
    for w in panel.findChildren(QWidget):
        assert "<b>x</b>" not in w.toolTip()


def test_c5_zalgo_names_keep_bubble_within_thread_width(h, qtbot):
    """Review B4 (tan-cong): tên hợp lệ trên wire nhưng nhiều dấu kết hợp không được ép bong bóng
    rộng hơn luồng chat."""
    zalgo_device = "҉" * 63
    zalgo_file = "q" + "҉" * 127
    h.devices.reset([dview(name=zalgo_device), dview(OTHER_ID, OTHER)])
    h.view.set_device_names({PEER_ID: zalgo_device, OTHER_ID: OTHER})
    h.view.set_connected(PEER_ID, False)  # hiện cả dòng mất kết nối có tên máy
    b = h.offer(tkey(1), name=zalgo_device, names=(zalgo_file, zalgo_file + "x"))
    settle(qtbot, 20)
    panel = panel_of(b)
    assert panel is not None and panel.files_label.isVisibleTo(h.view)
    width = h.view.width()
    assert panel.minimumSizeHint().width() <= width, panel.minimumSizeHint()
    assert b.minimumSizeHint().width() <= width, b.minimumSizeHint()
    assert b.width() <= width, (b.width(), width)


# =================================================================================================
# C6 đổi ngôn ngữ
# =================================================================================================


def test_c6_language_change_with_panel_open(h):
    from teleport.ui import i18n
    from teleport.ui.widgets import BubbleEmphasis

    k = tkey(1)
    b = h.offer(k)
    panel = panel_of(b)
    i18n.set_language("en")
    settle()
    assert i18n.current_language() == "en", "cần teleport_en.qm"
    assert plain(panel.accept_button.text()) == EN["accept"]
    assert plain(panel.reject_button.text()) == EN["reject"]
    assert plain(panel.change_button.text()) == EN["change"]
    assert plain(panel.always_check.text()) == EN["always"]
    assert b.emphasis is BubbleEmphasis.ATTENTION
    panel.accept_button.click()
    assert h.answers == [(k, True)]


def test_c6_language_change_keeps_disabled_state_while_pending_dir(h, tmp_path):
    from teleport.ui import i18n

    panel = panel_of(h.offer(tkey(1)))
    h.view.set_receive_dir_pending(tmp_path / "moi")
    i18n.set_language("en")
    settle()
    assert not panel.accept_button.isEnabled()
    assert panel.reject_button.isEnabled()
    assert "Changing to" in panel.save_label.text()


# =================================================================================================
# C7 luồng máy khác, C8 không cướp tiêu điểm
# =================================================================================================


def test_c7_offer_from_other_device_has_panel_in_its_thread(h, qtbot):
    k = tkey(1, device_id=OTHER_ID)
    b = h.offer(k, name=OTHER)
    assert b is not None and panel_of(b) is not None
    assert h.view.device_id() == PEER_ID
    h.view.set_device(OTHER_ID)
    settle(qtbot, 20)
    assert panel_of(h.view.row(k)) is not None
    assert panel_of(h.view.row(k)).isVisibleTo(h.view)


def test_c8_new_offer_does_not_steal_focus(h):
    h.before.setFocus(Qt.FocusReason.OtherFocusReason)
    settle()
    assert QApplication.focusWidget() is h.before
    h.offer(tkey(1))
    h.offer(tkey(2, device_id=OTHER_ID), name=OTHER)
    settle()
    assert QApplication.focusWidget() is h.before


def test_c8_offer_bubble_counts_as_unfinished_and_survives_trim(h):
    """Luật B3 giữ: lời mời là bong bóng của phiên chưa kết thúc, không bị cắt bởi trần 200."""
    from teleport.ui.chat import MAX_THREAD_BUBBLES

    k = tkey(1)
    h.offer(k)
    for i in range(MAX_THREAD_BUBBLES + 5):
        kk = tkey(1000 + i, direction="send")
        h.view.handle(finished(kk, "done"))
    settle()
    assert panel_of(h.view.row(k)) is not None


# =================================================================================================
# is_pinned
# =================================================================================================


def test_is_pinned_true_for_thread_not_built_yet(h):
    assert h.view.is_pinned("9" * 32) is True


def test_is_pinned_follows_thread_scroll(h, qtbot):
    for i in range(30):
        h.view.handle(finished(tkey(100 + i, direction="send"), "done"))
    settle(qtbot, 50)
    bar = h.view.thread_for(PEER_ID).scroll_area.verticalScrollBar()
    assert bar.maximum() > 0, "cần đủ bong bóng để cuộn"
    qtbot.waitUntil(lambda: bar.value() == bar.maximum(), timeout=2000)
    assert h.view.is_pinned(PEER_ID)
    bar.setValue(0)
    settle()
    assert not h.view.is_pinned(PEER_ID)


def test_panel_labels_not_cut_when_disconnected_line_appears(h, qtbot):
    """Review B4 lượt 2: dòng mất kết nối hiện sau khi bong bóng đã dựng thì bong bóng cao lên."""
    from PySide6.QtWidgets import QLabel

    b = h.offer(tkey(1), names=tuple(f"tep-co-ten-dai-{i}.bin" for i in range(6)))
    panel = panel_of(b)
    h.view.set_connected(PEER_ID, False)
    h.book.set_connected(PEER_ID, False)
    settle(qtbot, 30)
    assert panel.note_label.isVisibleTo(h.view)
    cut = [
        (lb.objectName(), lb.height(), lb.heightForWidth(lb.contentsRect().width()))
        for lb in panel.findChildren(QLabel)
        if lb.isVisibleTo(panel)
        and lb.wordWrap()
        and lb.height() < lb.heightForWidth(lb.contentsRect().width())
    ]
    assert cut == []
