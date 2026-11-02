"""Nối dây lời mời nhận file trong `TransfersController` (spec B4 mục 3.3, 3.6, mục 7 T-W và ca biên
E1–E10; mục 11 thắng khi khác).

Viết từ spec, không từ code. Phần không socket: `MainWindow`, `Tray`, `ChatView`, `OfferPrompts` thật
(offscreen), `NodeHost` giả ghi lại `call(fn, on_done)`; test tự chạy `fn` trên một node giả ghi nhật ký
(`decide`, `set_ask_before_receive`, `cancel`) để biết controller định gọi gì. Trạng thái active của cửa
sổ đi qua seam cấp module `ui.transfers.default_window_active` (spec 11.3), vá bằng `monkeypatch`.

E7, E9 là integration (GUI thật + `Node` B thật trên 127.0.0.1, fixture `gui`).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton, QWidget

A_ID = "a71c04d2" + "e" * 24
B_ID = "0f0f0f0f" + "1" * 24
A_NAME = "PC-Nha"
B_NAME = "Laptop-Minh"
LOCAL = "127.0.0.1"
KiB = 1024

ACCEPT = "Nhận"
REJECT = "Từ chối"
CANCELLED = "Đã hủy"
SAVED = "Đã lưu"
DISCONNECTED = "Mất kết nối với {name}. Bạn vẫn trả lời được; nếu nhận, file sẽ bắt đầu khi hai máy nối lại."
CHANGING = "Đang đổi sang {path}…"


# =================================================================================================
# Dữ liệu giả (định nghĩa tại chỗ: không import conftest)
# =================================================================================================


def settle(qtbot=None, ms: int = 0) -> None:
    for _ in range(3):
        QApplication.processEvents()
    if qtbot is not None and ms:
        qtbot.wait(ms)


def tid(i: int) -> str:
    return f"{i:032x}"


def tkey(i: int = 1, device_id: str = A_ID, direction: str = "receive"):
    from teleport.core.transfer import Direction, TransferKey

    return TransferKey(device_id, Direction(direction), tid(i))


def dview(device_id: str = A_ID, name: str = A_NAME, *, connected: bool = True):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id,
        name=name,
        os="windows",
        paired=True,
        online=True,
        connected=connected,
        addresses=("192.0.2.7",),
        port=47474,
    )


def offer(key, *, name: str | None = None, names=("video.mp4",), auto: bool = False):
    from teleport.app.transfers import OfferReceived
    from teleport.core.messages import FileEntry

    if name is None:
        name = A_NAME if key.device_id == A_ID else B_NAME
    files = tuple(FileEntry(f"f{i}.bin", 1000, 0) for i in range(len(names)))
    return OfferReceived(
        key=key,
        device_name=name,
        files=files,
        names=tuple(names),
        total_bytes=1000 * len(names),
        auto_accepted=auto,
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


class FakeTransfers:
    def __init__(self, log: list) -> None:
        self.log = log
        self.gone: set = set()  # key mà `get` trả None (lượt đã kết thúc trên node)
        self.running: set = set()  # key đã qua pha hỏi người dùng (`get` báo TRANSFERRING)
        self.decide_error: BaseException | None = None

    def get(self, key):
        from teleport.core.transfer import TransferStatus

        if key in self.gone:
            return None
        status = TransferStatus.TRANSFERRING if key in self.running else TransferStatus.WAITING
        return SimpleNamespace(key=key, status=status)

    def decide(self, key, accept):
        if self.decide_error is not None:
            raise self.decide_error
        self.log.append(("decide", key, accept))

    def cancel(self, key):
        self.log.append(("cancel", key))

    def send(self, device_id, paths):
        self.log.append(("send", device_id))

    def transfers(self):
        return ()

    def history(self, device_id=None):
        return ()


class FakeNode:
    def __init__(self) -> None:
        self.log: list = []
        self.transfers = FakeTransfers(self.log)
        self.ask_error: BaseException | None = None

    def set_ask_before_receive(self, device_id, ask):
        if self.ask_error is not None:
            raise self.ask_error
        self.log.append(("ask", device_id, ask))

    def devices(self):
        return ()

    def decides(self) -> list:
        return [e for e in self.log if e[0] == "decide"]


class FakeHost:
    def __init__(self, receive_dir: Path) -> None:
        from teleport.ui.bridge import NodeOptions

        self.options = NodeOptions(config_dir=receive_dir.parent / "cfg", receive_dir=receive_dir, mdns=None)
        self.calls: list = []
        self.submits: list = []
        self.running = True

    def call(self, fn, on_done=None) -> None:
        self.calls.append((fn, on_done))

    def submit(self, fn, on_done=None) -> None:
        self.submits.append((fn, on_done))

    def run_calls(self, node: FakeNode) -> int:
        """Chạy mọi `call` đang chờ trên `node`, trả kết quả về `on_done` (như NodeHost); trả số lời gọi."""
        pending, self.calls = self.calls, []
        for fn, on_done in pending:
            try:
                result = fn(node)
            except Exception as e:
                result = e
            if on_done is not None:
                on_done(result)
        return len(pending)


def panel_of(bubble):
    import shiboken6

    if bubble is None or not shiboken6.isValid(bubble):
        return None
    for w in bubble.findChildren(QWidget, "offerPanel"):
        if shiboken6.isValid(w) and not w.isHidden():
            return w
    return None


def alive(obj) -> bool:
    import shiboken6

    return obj is not None and shiboken6.isValid(obj)


def box_open(ctl, key) -> bool:
    box = ctl.offers.box(key)
    return alive(box) and box.isVisible()


def box_texts(box) -> str:
    return "\n".join(w.text() for w in box.findChildren(QLabel) if w.isVisibleTo(box) and w.text())


def box_button(box, text: str):
    found = [
        b for b in box.findChildren(QPushButton) if b.text().replace("&", "") == text and b.isVisibleTo(box)
    ]
    assert len(found) == 1, [b.text() for b in box.findChildren(QPushButton)]
    return found[0]


def peer(name: str, device_id: str) -> str:
    from teleport.ui.texts import peer_label

    return peer_label(name, device_id)


# =================================================================================================
# Fixture
# =================================================================================================


@pytest.fixture
def env(qtbot, monkeypatch, tmp_path):
    """Cửa sổ chính đang RUNNING, hiện, trang HOME, mở luồng của A (rỗng nên đang ở cuối); cửa sổ
    “active” theo seam; app active (không bóng thoại offer)."""
    from teleport.ui import transfers as mod
    from teleport.ui.main_window import MainWindow
    from teleport.ui.tray import Tray

    messages: list[str] = []
    monkeypatch.setattr(Tray, "show_message", lambda self, text, folder=None: messages.append(text))
    flags = SimpleNamespace(window_active=True, app_active=True)
    monkeypatch.setattr(mod, "default_window_active", lambda: flags.window_active)
    window = MainWindow()
    qtbot.addWidget(window)
    tray = Tray()
    ctl = mod.TransfersController(window, tray, parent=None, app_active=lambda: flags.app_active)
    receive_dir = tmp_path / "nhan"
    host = FakeHost(receive_dir)
    ctl.attach(host)
    window.show_running("May-A", receive_dir)
    window.devices.reset([dview(A_ID, A_NAME), dview(B_ID, B_NAME)])
    window.resize(980, 660)
    window.show()
    qtbot.waitExposed(window)
    window.conversations.select_device(A_ID)
    settle(qtbot, 20)
    node = FakeNode()
    saved: list[str] = []
    ctl.always_accept_saved.connect(saved.append)
    changes: list[int] = []
    ctl.change_receive_dir_requested.connect(lambda: changes.append(1))
    ns = SimpleNamespace(
        mod=mod,
        window=window,
        tray=tray,
        ctl=ctl,
        host=host,
        node=node,
        flags=flags,
        messages=messages,
        saved=saved,
        changes=changes,
        receive_dir=receive_dir,
        tmp=tmp_path,
    )
    assert ctl.view.device_id() == A_ID, "tiền đề: luồng A đang mở"
    yield ns
    ctl.close_all()
    tray.dispose()
    settle()


def push(env, update) -> None:
    env.ctl.on_transfer(update)
    settle()


# =================================================================================================
# W1–W6: bong bóng hay hộp
# =================================================================================================


def test_w1_offer_in_open_thread_shows_panel_only_no_box_no_tray(env):
    assert env.ctl.offer_in_view(A_ID) is True
    k = tkey(1)
    push(env, offer(k))
    assert env.ctl.offers.keys() == [] and env.ctl.offers.queued() == []
    assert panel_of(env.ctl.view.row(k)) is not None
    assert env.messages == []
    from teleport.ui.offer import OfferPhase

    assert env.ctl.offer_book.entry(k).phase is OfferPhase.PENDING


def test_w2_offer_from_other_device_opens_box_and_panel(env):
    assert env.ctl.offer_in_view(B_ID) is False
    k = tkey(1, B_ID)
    push(env, offer(k))
    assert env.ctl.offers.keys() == [k]
    assert box_open(env.ctl, k)
    assert panel_of(env.ctl.view.row(k)) is not None, "luồng máy khác vẫn có panel"


def test_w3_window_not_active_opens_box(env):
    env.flags.window_active = False
    assert env.ctl.offer_in_view(A_ID) is False
    k = tkey(1)
    push(env, offer(k))
    assert env.ctl.offers.keys() == [k]


def test_w3_app_not_active_shows_tray_balloon(env):
    from teleport.ui import texts

    env.flags.window_active = False
    env.flags.app_active = False
    push(env, offer(tkey(1)))
    assert env.messages == [texts.offer_waiting()]


def test_w4_hidden_window_opens_box(env, qtbot):
    env.window.hide()
    settle(qtbot, 10)
    assert env.ctl.offer_in_view(A_ID) is False
    k = tkey(1)
    push(env, offer(k))
    assert env.ctl.offers.keys() == [k]


def test_w4b_minimizing_opens_box_for_offer_shown_only_as_bubble(env, qtbot):
    """Spec 11.4: thu nhỏ (WindowStateChange) → `on_window_hidden` qua event filter trên cửa sổ."""
    k = tkey(1)
    push(env, offer(k))
    assert env.ctl.offers.keys() == []
    env.window.showMinimized()
    qtbot.waitUntil(lambda: env.ctl.offers.keys() == [k], timeout=2000)
    assert env.ctl.offer_in_view(A_ID) is False


def test_w4b_hiding_window_opens_box_for_offer_shown_only_as_bubble(env, qtbot):
    k = tkey(1)
    push(env, offer(k))
    assert env.ctl.offers.keys() == []
    env.window.hide()
    qtbot.waitUntil(lambda: env.ctl.offers.keys() == [k], timeout=2000)


def test_w5_settings_page_opens_box(env, qtbot):
    from teleport.ui.main_window import Page

    if not env.window.pages.has_page(Page.SETTINGS):
        env.window.add_page(Page.SETTINGS, QWidget())
    assert env.window.open_page(Page.SETTINGS)
    settle(qtbot, 10)
    assert env.ctl.offer_in_view(A_ID) is False
    k = tkey(1)
    push(env, offer(k))
    assert env.ctl.offers.keys() == [k]


def test_w6_thread_scrolled_up_opens_box(env, qtbot):
    view = env.ctl.view
    for i in range(30):
        view.handle(finished(tkey(500 + i, direction="send"), "done"))
    settle(qtbot, 50)
    bar = view.thread_for(A_ID).scroll_area.verticalScrollBar()
    assert bar.maximum() > 0, "cần đủ bong bóng để cuộn"
    bar.setValue(0)
    settle()
    assert not view.is_pinned(A_ID)
    assert env.ctl.offer_in_view(A_ID) is False
    k = tkey(1)
    push(env, offer(k))
    assert env.ctl.offers.keys() == [k]


def test_auto_accepted_offer_neither_box_nor_book(env):
    k = tkey(1, B_ID)
    push(env, offer(k, auto=True))
    assert env.ctl.offers.keys() == []
    assert env.ctl.offer_book.entry(k) is None
    assert panel_of(env.ctl.view.row(k)) is None


# =================================================================================================
# W7–W8: chống trả lời hai lần
# =================================================================================================


def test_w7_panel_accept_then_box_reject_same_tick_sends_one_decide_true(env):
    k = tkey(1, B_ID)
    push(env, offer(k))
    box = env.ctl.offers.box(k)
    panel = panel_of(env.ctl.view.row(k))
    assert alive(box) and panel is not None
    panel.accept_button.click()
    env.ctl.offers.decided.emit(k, False)  # không processEvents ở giữa
    env.host.run_calls(env.node)
    assert env.node.decides() == [("decide", k, True)]
    assert not box_open(env.ctl, k)
    assert env.ctl.offers.box(k) is None


def test_w8_box_escape_then_panel_accept_sends_one_decide_false(env, qtbot):
    k = tkey(1, B_ID)
    push(env, offer(k))
    box = env.ctl.offers.box(k)
    accept = panel_of(env.ctl.view.row(k)).accept_button
    qtbot.keyClick(box, Qt.Key.Key_Escape)
    if alive(accept):
        accept.click()
    env.ctl.view.offer_answer_requested.emit(k, True)  # thêm một ý định muộn từ bong bóng
    settle(qtbot, 20)
    env.host.run_calls(env.node)
    assert env.node.decides() == [("decide", k, False)]
    assert not box_open(env.ctl, k)


def test_decide_alias_goes_through_book_once(env):
    k = tkey(1, B_ID)
    push(env, offer(k))
    env.ctl.decide(k, True)
    env.ctl.decide(k, False)
    env.ctl.answer(k, False)
    env.host.run_calls(env.node)
    assert env.node.decides() == [("decide", k, True)]


def test_decide_without_offer_sends_nothing(env):
    env.ctl.decide(tkey(9, B_ID), True)
    assert env.host.calls == []


# =================================================================================================
# W9–W11: Luôn nhận
# =================================================================================================


def test_w9_accept_with_always_runs_decide_then_set_ask_in_one_call(env):
    k = tkey(1, B_ID)
    push(env, offer(k))
    box = env.ctl.offers.box(k)
    box.findChild(QCheckBox, "offerAlways").click()
    env.host.calls.clear()
    box_button(box, ACCEPT).click()
    assert len(env.host.calls) == 1, "một host.call duy nhất"
    env.host.run_calls(env.node)
    assert env.node.log == [("decide", k, True), ("ask", B_ID, False)]


def test_w9_tick_in_panel_counts_too(env):
    k = tkey(1)
    push(env, offer(k))
    panel = panel_of(env.ctl.view.row(k))
    panel.always_check.click()
    panel.accept_button.click()
    env.host.run_calls(env.node)
    assert env.node.log == [("decide", k, True), ("ask", A_ID, False)]


def test_w9_reject_with_always_only_decides(env):
    k = tkey(1, B_ID)
    push(env, offer(k))
    box = env.ctl.offers.box(k)
    box.findChild(QCheckBox, "offerAlways").click()
    box_button(box, REJECT).click()
    env.host.run_calls(env.node)
    assert env.node.log == [("decide", k, False)]
    assert env.saved == []


def test_w9_accept_without_always_only_decides(env):
    k = tkey(1, B_ID)
    push(env, offer(k))
    box_button(env.ctl.offers.box(k), ACCEPT).click()
    env.host.run_calls(env.node)
    assert env.node.log == [("decide", k, True)]


def test_w10_settings_save_error_shows_warning_notice(env):
    from teleport.app.settings import SettingsSaveError
    from teleport.ui.offer import always_save_error_text
    from teleport.ui.widgets import InfoLevel

    env.node.ask_error = SettingsSaveError("disk full")
    k = tkey(1, B_ID)
    push(env, offer(k))
    box = env.ctl.offers.box(k)
    box.findChild(QCheckBox, "offerAlways").click()
    box_button(box, ACCEPT).click()
    env.host.run_calls(env.node)
    assert env.node.decides() == [("decide", k, True)]
    assert env.ctl.view.notice_text() == always_save_error_text(peer(B_NAME, B_ID))
    assert env.ctl.view.info_bar.level is InfoLevel.WARNING
    assert env.saved == []


@pytest.mark.parametrize("error", [KeyError("x"), RuntimeError("node đang đóng")], ids=["key", "runtime"])
def test_w10_key_or_runtime_error_is_silent(env, error):
    env.node.ask_error = error
    k = tkey(1, B_ID)
    push(env, offer(k))
    box = env.ctl.offers.box(k)
    box.findChild(QCheckBox, "offerAlways").click()
    box_button(box, ACCEPT).click()
    env.host.run_calls(env.node)
    assert env.ctl.view.notice_text() == ""
    assert env.saved == []


def test_decide_error_skips_set_ask(env):
    """Spec 11.7: `decide` ném lỗi → không gọi `set_ask_before_receive`; không thông báo “Luôn nhận”."""
    env.node.transfers.decide_error = ValueError("sai pha")
    k = tkey(1, B_ID)
    push(env, offer(k))
    box = env.ctl.offers.box(k)
    box.findChild(QCheckBox, "offerAlways").click()
    box_button(box, ACCEPT).click()
    env.host.run_calls(env.node)
    assert [e for e in env.node.log if e[0] == "ask"] == []
    assert env.saved == []


def test_transfer_gone_on_node_skips_decide_and_set_ask(env):
    """Spec 11.7: `node.transfers.get(key) is None` → không decide, không lưu “Luôn nhận”."""
    k = tkey(1, B_ID)
    push(env, offer(k))
    env.node.transfers.gone.add(k)
    box = env.ctl.offers.box(k)
    box.findChild(QCheckBox, "offerAlways").click()
    box_button(box, ACCEPT).click()
    env.host.run_calls(env.node)
    assert env.node.log == []
    assert env.saved == []


def test_transfer_past_asking_phase_decides_but_does_not_save_always(env):
    """Review B4: lượt đã qua pha hỏi người dùng trên node (`get` không còn WAITING) lúc lời gọi chạy
    → vẫn `decide` (engine tự bỏ qua) nhưng không lưu “Luôn nhận”, không phát `always_accept_saved`."""
    k = tkey(1, B_ID)
    push(env, offer(k))
    env.node.transfers.running.add(k)
    box = env.ctl.offers.box(k)
    box.findChild(QCheckBox, "offerAlways").click()
    box_button(box, ACCEPT).click()
    env.host.run_calls(env.node)
    assert env.node.log == [("decide", k, True)]
    assert env.saved == []
    assert env.ctl.view.notice_text() == ""


def test_w11_always_accept_saved_emitted_on_success(env):
    k = tkey(1, B_ID)
    push(env, offer(k))
    box = env.ctl.offers.box(k)
    box.findChild(QCheckBox, "offerAlways").click()
    box_button(box, ACCEPT).click()
    env.host.run_calls(env.node)
    assert env.saved == [B_ID]


def test_result_of_old_host_is_ignored(env):
    from teleport.app.settings import SettingsSaveError

    env.node.ask_error = SettingsSaveError("x")
    k = tkey(1, B_ID)
    push(env, offer(k))
    box = env.ctl.offers.box(k)
    box.findChild(QCheckBox, "offerAlways").click()
    box_button(box, ACCEPT).click()
    old = env.host
    env.ctl.attach(FakeHost(env.tmp / "nhan2"))
    old.run_calls(env.node)
    assert env.ctl.view.notice_text() == ""
    assert env.saved == []


def test_answer_without_host_sends_nothing_and_does_not_crash(env):
    from teleport.ui.offer import OfferPhase

    env.ctl.attach(None)
    k = tkey(1, B_ID)
    push(env, offer(k))
    env.ctl.answer(k, True)
    entry = env.ctl.offer_book.entry(k)
    assert entry is not None and entry.phase is OfferPhase.ANSWERED


# =================================================================================================
# W12–W14
# =================================================================================================


def test_w12_attach_new_host_closes_boxes_removes_panels_and_rows(env):
    kb, ka = tkey(1, B_ID), tkey(2)
    push(env, offer(kb))
    push(env, offer(ka))
    box = env.ctl.offers.box(kb)
    old = env.host
    new = FakeHost(env.tmp / "nhan2")
    env.ctl.attach(new)
    settle()
    assert not (alive(box) and box.isVisible())
    assert env.ctl.offers.keys() == [] and env.ctl.offers.queued() == []
    assert env.ctl.view.rows() == []
    assert env.ctl.offer_book.entry(kb) is None and env.ctl.offer_book.entry(ka) is None
    env.ctl.decide(kb, True)
    env.ctl.answer(ka, True)
    old.run_calls(env.node)
    new.run_calls(env.node)
    assert env.node.decides() == [], "không quyết định gì khi đổi host"


def test_close_all_clears_book_and_boxes_without_deciding(env):
    k = tkey(1, B_ID)
    push(env, offer(k))
    env.ctl.close_all()
    settle()
    assert env.ctl.offers.keys() == []
    assert env.ctl.offer_book.entry(k) is None
    env.host.run_calls(env.node)
    assert env.node.decides() == []


def test_w13_window_hidden_opens_box_for_pending_bubble_only(env):
    pending, answered = tkey(1), tkey(2)
    push(env, offer(pending))
    push(env, offer(answered))
    assert env.ctl.offers.keys() == []
    panel_of(env.ctl.view.row(answered)).reject_button.click()
    env.ctl.on_window_hidden()
    settle()
    assert env.ctl.offers.keys() == [pending]


def test_w13_window_hidden_twice_does_not_duplicate(env):
    k = tkey(1)
    push(env, offer(k))
    env.ctl.on_window_hidden()
    env.ctl.on_window_hidden()
    assert env.ctl.offers.keys() == [k]
    assert env.ctl.offers.queued() == []


def test_w14_cancel_on_pending_offer_never_cancels_transfer(env):
    """Spec 11.6: `cancel_requested` không phát được cho lời mời PENDING; `cancel(key)` PENDING → từ chối."""
    k = tkey(1)
    push(env, offer(k))
    bubble = env.ctl.view.row(k)
    bubble.cancel_button.click()
    env.host.run_calls(env.node)
    assert [e for e in env.node.log if e[0] == "cancel"] == []
    env.ctl.cancel(k)
    env.host.run_calls(env.node)
    assert env.node.log == [("decide", k, False)], "PENDING: cancel là từ chối qua book"


def test_w14_cancel_after_accept_really_cancels(env):
    k = tkey(1)
    push(env, offer(k))
    env.ctl.answer(k, True)
    env.host.run_calls(env.node)
    env.ctl.cancel(k)
    env.host.run_calls(env.node)
    assert env.node.log == [("decide", k, True), ("cancel", k)]


# =================================================================================================
# Ca biên E
# =================================================================================================


def test_e1_offer_expires_while_box_open(env):
    k = tkey(1, B_ID)
    push(env, offer(k))
    bubble = env.ctl.view.row(k)
    push(env, finished(k, "expired"))
    assert not box_open(env.ctl, k)
    assert panel_of(bubble) is None or not panel_of(bubble).isVisibleTo(env.ctl.view)
    env.ctl.answer(k, True)
    env.ctl.decide(k, True)
    assert env.host.calls == []


def test_e1_finished_closes_box_before_bubble_renders(env):
    """Spec 3.3 bước 2a (11.13): `book.finish` chạy trước `view.handle` của `TransferFinished`."""
    k = tkey(1, B_ID)
    push(env, offer(k))
    seen: list = []
    env.ctl.offer_book.removed.connect(lambda key: seen.append(env.ctl.view.row(key).finished))
    push(env, finished(k, "cancelled_other", by_peer=True))
    assert seen == [False]


def test_e2_cancel_right_after_accept(env):
    k = tkey(1, B_ID)
    push(env, offer(k))
    box_button(env.ctl.offers.box(k), ACCEPT).click()
    push(env, finished(k, "cancelled_other", by_peer=True))
    env.host.run_calls(env.node)
    assert not box_open(env.ctl, k)
    assert CANCELLED in env.ctl.view.row(k).status_label.text()
    assert env.node.decides() == [("decide", k, True)]


def test_e3_twenty_offers_while_hidden_three_boxes_rest_queued(env, qtbot):
    ids = [f"{i:08x}" + "c" * 24 for i in range(20)]
    env.window.devices.reset([dview(i, f"May-{n:02}") for n, i in enumerate(ids)])
    env.window.hide()
    settle(qtbot, 10)
    keys = [tkey(100 + n, i) for n, i in enumerate(ids)]
    for n, k in enumerate(keys):
        push(env, offer(k, name=f"May-{n:02}"))
    ctl = env.ctl
    assert ctl.offers.keys() == keys[:3]
    assert ctl.offers.queued() == keys[3:]
    assert all(panel_of(ctl.view.row(k)) is not None for k in keys), "20 panel"
    box_button(ctl.offers.box(keys[0]), ACCEPT).click()
    settle(qtbot, 20)
    assert ctl.offers.keys() == keys[1:4], "hộp kế tiếp trong hàng mở (FIFO)"
    assert ctl.offers.queued() == keys[4:]
    push(env, finished(keys[10], "cancelled_other", by_peer=True))
    assert keys[10] not in ctl.offers.queued()
    assert len(ctl.offers.queued()) == 15
    for k in keys[1:4]:
        box_button(ctl.offers.box(k), REJECT).click()
        ctl.offers.decided.emit(k, True)  # bấm thêm, muộn: không được gửi lần hai
    settle(qtbot, 20)
    env.host.run_calls(env.node)
    counts: dict = {}
    for _, k, _a in env.node.decides():
        counts[k] = counts.get(k, 0) + 1
    assert counts and max(counts.values()) == 1, counts
    assert len(ctl.offers.keys()) <= 3


def test_e4_disconnect_shows_line_in_box_and_panel_accept_still_sends(env):
    from teleport.app.node import DeviceConnected, DeviceDisconnected

    k = tkey(1, B_ID)
    push(env, offer(k))
    box = env.ctl.offers.box(k)
    panel = panel_of(env.ctl.view.row(k))
    line = DISCONNECTED.format(name=peer(B_NAME, B_ID))
    assert line not in box_texts(box)
    env.ctl.on_node_event(DeviceDisconnected(device=dview(B_ID, B_NAME, connected=False)))
    settle()
    assert line in box_texts(box)
    assert (
        not panel.note_label.isHidden() and panel.note_label.text() == line
    )  # luồng B không mở: xét isHidden
    env.ctl.on_node_event(DeviceConnected(device=dview(B_ID, B_NAME), outgoing=False))
    settle()
    assert line not in box_texts(box)
    assert panel.note_label.isHidden()
    env.ctl.on_node_event(DeviceDisconnected(device=dview(B_ID, B_NAME, connected=False)))
    box_button(box, ACCEPT).click()
    env.host.run_calls(env.node)
    assert env.node.decides() == [("decide", k, True)]


def test_e4_offer_arriving_while_disconnected_starts_with_line(env):
    k = tkey(1, B_ID)
    env.window.devices.reset([dview(A_ID, A_NAME), dview(B_ID, B_NAME, connected=False)])
    push(env, offer(k))
    assert env.ctl.offer_book.entry(k).connected is False
    assert DISCONNECTED.format(name=peer(B_NAME, B_ID)) in box_texts(env.ctl.offers.box(k))


def test_e5_language_change_with_box_panel_and_pairing_dialog(env, qtbot):
    from teleport.app.pairing import PairingPrompt
    from teleport.core.pairing import Role
    from teleport.ui import i18n
    from teleport.ui.pairing import PairingDialog
    from teleport.ui.widgets import BubbleEmphasis

    k = tkey(1, B_ID)
    push(env, offer(k))
    env.ctl.set_receive_dir_pending(env.tmp / "moi")
    box = env.ctl.offers.box(k)
    panel = panel_of(env.ctl.view.row(k))
    prompt = PairingPrompt(
        session_id=3,
        role=Role("responder"),
        address="192.168.1.5",
        peer_name="MacBook",
        peer_device_id=B_ID,
        code="905617",
        expires_at=1042.3,
        recent_codes=1,
        known_name=None,
    )
    dialog = PairingDialog(prompt, clock=lambda: 1000.0)
    qtbot.addWidget(dialog)
    dialog.show()
    i18n.set_language("en")
    settle(qtbot, 10)
    assert i18n.current_language() == "en", "cần teleport_en.qm"
    assert not box_button(box, "Accept").isEnabled(), "đang đổi thư mục: Nhận vẫn tắt sau đổi ngôn ngữ"
    assert box_button(box, "Decline").isEnabled()
    assert panel.accept_button.text().replace("&", "") == "Accept"
    assert not panel.accept_button.isEnabled()
    assert env.ctl.view.row(k).emphasis is BubbleEmphasis.ATTENTION
    countdown = dialog.findChild(QLabel, "pairingCountdownText")
    assert countdown is not None and countdown.text() == "43 seconds left to confirm"


def test_e6_change_dir_from_box_and_panel_then_pending_success_and_busy(env):
    from teleport.ui.send import printable

    k = tkey(1, B_ID)
    push(env, offer(k))
    box = env.ctl.offers.box(k)
    panel = panel_of(env.ctl.view.row(k))
    box.findChild(QPushButton, "offerChangeDir").click()
    panel.change_button.click()
    assert env.changes == [1, 1]
    new = env.tmp / "moi"
    env.ctl.set_receive_dir_pending(new)
    assert not box_button(box, ACCEPT).isEnabled()
    assert not panel.accept_button.isEnabled()
    assert CHANGING.format(path=printable(str(new))) in box_texts(box)
    env.ctl.set_receive_dir_pending(None)
    env.ctl.set_receive_dir(new)
    assert box_button(box, ACCEPT).isEnabled() and panel.accept_button.isEnabled()
    save = f"Lưu vào: {printable(str(new))}"
    assert save in box_texts(box)
    assert panel.save_label.text() == save
    busy = "Chưa đổi được thư mục nhận vì đang nhận file. Hãy đổi lại sau khi các lượt nhận xong."
    env.ctl.show_receive_dir_error(busy)
    err = box.findChild(QLabel, "offerDirError")
    assert err is not None and err.isVisibleTo(box) and err.text() == busy
    env.ctl.show_receive_dir_error(None)
    assert not err.isVisibleTo(box)
    assert env.host.calls == [], "đổi thư mục không trả lời lời mời"


def test_e6_dir_error_not_carried_to_box_opened_later(env):
    """Review B4: lỗi đổi thư mục chỉ hiện ở hộp đang mở lúc lỗi; lời mời tới sau mở hộp sạch.
    (`ReceiveDirController` phát `state_changed` trước `failed`, nên không có lần gỡ nào sau lỗi.)"""
    busy = "Chưa đổi được thư mục nhận vì đang nhận file. Hãy đổi lại sau khi các lượt nhận xong."
    first = tkey(1, B_ID)
    push(env, offer(first))
    old_box = env.ctl.offers.box(first)
    env.ctl.show_receive_dir_error(busy)
    old_err = old_box.findChild(QLabel, "offerDirError")
    assert old_err is not None and old_err.isVisibleTo(old_box) and old_err.text() == busy
    later = tkey(2, B_ID)
    push(env, offer(later))
    new_box = env.ctl.offers.box(later)
    assert alive(new_box) and new_box is not old_box
    new_err = new_box.findChild(QLabel, "offerDirError")
    assert new_err is None or not new_err.isVisibleTo(new_box)
    assert busy not in box_texts(new_box)
    assert old_err.isVisibleTo(old_box), "hộp đang mở lúc lỗi vẫn giữ lỗi"


def test_e6_dir_error_cleared_when_next_change_starts(env):
    busy = "Chưa đổi được thư mục nhận vì đang nhận file. Hãy đổi lại sau khi các lượt nhận xong."
    k = tkey(1, B_ID)
    push(env, offer(k))
    box = env.ctl.offers.box(k)
    env.ctl.show_receive_dir_error(busy)
    err = box.findChild(QLabel, "offerDirError")
    assert err.isVisibleTo(box)
    env.ctl.set_receive_dir_pending(env.tmp / "lan-sau")
    assert not err.isVisibleTo(box), "lần đổi kế tiếp bắt đầu: gỡ lỗi cũ"


def test_e8_hide_then_show_keeps_box_open(env, qtbot):
    k = tkey(1, B_ID)
    push(env, offer(k))
    env.window.hide()
    settle(qtbot, 10)
    env.window.show()
    settle(qtbot, 10)
    assert box_open(env.ctl, k)
    assert env.ctl.offers.keys() == [k]


def test_e10_duplicate_offer_same_key_one_box_one_panel(env):
    k = tkey(1, B_ID)
    opened: list = []
    env.ctl.offers.opened.connect(opened.append)
    push(env, offer(k))
    push(env, offer(k))
    assert env.ctl.offers.keys() == [k]
    assert opened == [k]
    rows = [r for r in env.ctl.view.rows() if r.key == k]
    assert len(rows) == 1
    assert len([w for w in rows[0].findChildren(QWidget, "offerPanel") if alive(w) and not w.isHidden()]) == 1


def test_e10_duplicate_offer_after_answer_does_not_reopen(env):
    k = tkey(1, B_ID)
    push(env, offer(k))
    box_button(env.ctl.offers.box(k), ACCEPT).click()
    settle()
    push(env, offer(k))
    assert env.ctl.offers.keys() == []
    assert panel_of(env.ctl.view.row(k)) is None
    env.host.run_calls(env.node)
    assert env.node.decides() == [("decide", k, True)]


# =================================================================================================
# E7, E9 integration: GUI A + Node B thật
# =================================================================================================


def _controller(run):
    app = run.tray.parent()
    assert app is not None and hasattr(app, "transfers")
    return app


def _a_port(run, gui_wait):
    got: list = []
    run.host.call(lambda node: node.port, got.append)
    yield gui_wait(lambda: got, 5, "cổng của A")
    return got[0]


def _connect_b(run, p, gui_wait, a_id: str):
    port = yield from _a_port(run, gui_wait)
    p.soon(p.node.connect_hint, a_id, [LOCAL], port)
    yield gui_wait(lambda: p.connected_to(a_id), 10, "B nối tới A")


def _a_finished(updates: list, transfer_id: str):
    for u in updates:
        if (
            type(u).__name__ == "TransferFinished"
            and u.info.key.transfer_id == transfer_id
            and str(u.result) != "suspended"
        ):
            return u
    return None


class _Dialogs:
    """Seam `receive_dir.dialog_factory`: `QFileDialog` con, `open()` không hiện gì."""

    def __init__(self) -> None:
        self.made: list = []

    def factory(self, parent=None, *args, **kwargs):
        from PySide6.QtWidgets import QFileDialog

        made = self.made

        class FakeDialog(QFileDialog):
            def __init__(self) -> None:
                super().__init__(parent)
                made.append(self)

            def open(self, *a, **k):
                pass

            def exec(self, *a, **k):
                raise AssertionError("hộp chọn thư mục không được exec()")

        return FakeDialog()


@pytest.fixture
def pair(config_dir, trust_each_other, tmp_path):
    def make():
        a, b = config_dir("A"), config_dir("B")
        id_a, id_b = trust_each_other(a, b, name_a="May-A", name_b="May-B")
        src = tmp_path / "nguon"
        src.mkdir(exist_ok=True)
        return a, b, id_a.device_id, id_b.device_id, src

    return make


@pytest.mark.integration
def test_e7_change_receive_dir_while_offer_waits_then_accept_lands_in_new_dir(
    gui, gui_wait, pair, peer_node, monkeypatch, tmp_path
):
    from teleport.ui import receive_dir as rd_mod
    from teleport.ui import transfers as mod

    monkeypatch.setattr(mod, "default_window_active", lambda: False)  # hộp luôn bật
    dialogs = _Dialogs()
    monkeypatch.setattr(rd_mod, "dialog_factory", dialogs.factory)
    a, b, a_id, _, src = pair()
    p = peer_node(b)
    f = src / "bao-cao.bin"
    data = os.urandom(200 * KiB)
    f.write_bytes(data)
    old_dir = tmp_path / "nhan-gui"
    new_dir = tmp_path / "nhan-moi"
    new_dir.mkdir()
    updates: list = []
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.hosts and run.host.running, 20, "node A chạy")
        run.host.transfer_event.connect(updates.append)
        app = _controller(run)
        ctl = app.transfers
        c = app.receive_dir_controller
        yield from _connect_b(run, p, gui_wait, a_id)
        p.submit(p.node.transfers.send(a_id, [f]))
        yield gui_wait(lambda: ctl.offers.keys(), 10, "hộp hỏi nhận file")
        [key] = ctl.offers.keys()
        box = ctl.offers.box(key)
        box.findChild(QPushButton, "offerChangeDir").click()
        assert dialogs.made, "Đổi… trong hộp phải mở hộp chọn thư mục"
        dialog = dialogs.made[-1]
        dialog.fileSelected.emit(str(new_dir))
        dialog.finished.emit(1)
        if c.changing:
            seen["accept_while_pending"] = box_button(box, ACCEPT).isEnabled()
        yield gui_wait(lambda: not c.changing, 10, "đổi thư mục xong")
        seen["box_alive"] = box_open(ctl, key)
        box = ctl.offers.box(key)
        box_button(box, ACCEPT).click()
        yield gui_wait(lambda: _a_finished(updates, key.transfer_id), 30, "A nhận xong")
        seen["result"] = str(_a_finished(updates, key.transfer_id).result)
        run.tray.quit_requested.emit()

    run = gui(["--config-dir", str(a)], script, keep_receive_dir=True)
    assert run.exec_calls == []
    assert seen.get("accept_while_pending", False) is False, "đang đổi thư mục: Nhận phải tắt"
    assert seen["box_alive"], "đổi thư mục không đóng hộp hỏi"
    assert seen["result"] == "done"
    assert (new_dir / f.name).read_bytes() == data
    assert not (old_dir / f.name).exists()


@pytest.mark.integration
def test_e9_receiver_viewing_sender_thread_answers_in_bubble_only(
    gui, gui_wait, pair, peer_node, monkeypatch, tmp_path
):
    from teleport.ui import transfers as mod

    monkeypatch.setattr(mod, "default_window_active", lambda: True)  # spec 11.3: E9 vá True
    a, b, a_id, b_id, src = pair()
    p = peer_node(b)
    f = src / "anh.bin"
    data = os.urandom(100 * KiB)
    f.write_bytes(data)
    updates: list = []
    seen: dict = {}

    def script(run):
        from teleport.ui.main_window import ViewState

        yield gui_wait(lambda: run.hosts and run.host.running, 20, "node A chạy")
        yield gui_wait(lambda: run.window.view_state is ViewState.RUNNING, 10, "cửa sổ RUNNING")
        run.host.transfer_event.connect(updates.append)
        ctl = _controller(run).transfers
        yield from _connect_b(run, p, gui_wait, a_id)
        yield gui_wait(lambda: run.window.conversations.model.row_of(b_id) >= 0, 10, "B trong danh sách")
        run.window.show()
        run.window.open_conversation(b_id)
        yield gui_wait(lambda: ctl.view.device_id() == b_id, 5, "luồng của B mở")
        yield gui_wait(lambda: ctl.offer_in_view(b_id), 5, "đang nhìn cuối luồng của B")
        p.submit(p.node.transfers.send(a_id, [f]))
        yield gui_wait(
            lambda: any(panel_of(r) is not None for r in ctl.view.rows()), 10, "bong bóng lời mời có panel"
        )
        [row] = [r for r in ctl.view.rows() if panel_of(r) is not None]
        seen["boxes"] = (list(ctl.offers.keys()), list(ctl.offers.queued()))
        panel_of(row).accept_button.click()
        tid_ = row.key.transfer_id
        yield gui_wait(lambda: _a_finished(updates, tid_), 30, "A nhận xong")
        yield gui_wait(lambda: row.finished, 5, "bong bóng kết thúc")
        seen["result"] = str(_a_finished(updates, tid_).result)
        seen["status"] = row.status_label.text()
        run.tray.quit_requested.emit()

    run = gui(["--config-dir", str(a)], script)
    assert run.exec_calls == []
    assert seen["boxes"] == ([], []), "đang nhìn đúng luồng: chỉ bong bóng, không hộp"
    assert seen["result"] == "done"
    assert SAVED in seen["status"], seen["status"]
    assert (tmp_path / "nhan-gui" / f.name).read_bytes() == data


def test_snapshot_on_started_sets_connected_of_pending_offers(env):
    """Spec 3.3: ảnh chụp `_on_snapshot` gọi `book.set_connected` cho từng máy."""
    k = tkey(1, B_ID)
    push(env, offer(k))
    entry = env.ctl.offer_book.entry(k)
    assert entry.connected is True
    node = FakeNode()
    node.devices = lambda: (dview(A_ID, A_NAME), dview(B_ID, B_NAME, connected=False))  # type: ignore[method-assign]
    env.ctl.on_started(env.host)
    env.host.run_calls(node)
    settle()
    assert env.ctl.offer_book.entry(k).connected is False
    assert DISCONNECTED.format(name=peer(B_NAME, B_ID)) in box_texts(env.ctl.offers.box(k))
