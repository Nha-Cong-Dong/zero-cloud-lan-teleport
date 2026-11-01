"""Nối danh sách cuộc trò chuyện vào cửa sổ, `TransfersController` và `GuiApp` (spec B2 mục 2.1, 2.6–2.9,
12.2, 12.4, 12.9, 12.16; T-W1…W12, kể cả W2b, W7b).

Viết từ spec, không từ code. Không socket: `NodeHost` giả ghi lại `call` / `submit`, test tự chạy `fn`
trên node giả rồi gọi `on_done` (như `test_transfers_controller.py`). `GuiApp` dựng trực tiếp với host
giả (như `test_repair_offline.py`). W11 (“Ghép đôi lại” khi máy không trong mạng) nằm ở
`test_repair_offline.py` (spec 12.30: giữ file, đổi đích). W12 chạy `teleport-gui --selftest` ở tiến
trình con (`slow`).

Cửa sổ cần tiêu điểm/Tab thì hiện không có `WA_DontShowOnScreen` (cửa sổ cờ đó không thành active trên
nền `offscreen`, xem `test_home_layout.py`).
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QRect, Qt, QUrl, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

NOW = datetime.datetime(2026, 10, 31, 10, 45).timestamp()
PEER = "PC-Nha"
OTHER = "Laptop-Minh"
DROP_HINT = "Thả file lên một thiết bị để gửi."
OPEN_DIR = "Mở thư mục"


def device_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:32]


PEER_ID = device_id(PEER)
OTHER_ID = device_id(OTHER)


def dview(label: str = PEER, *, paired: bool = True, online: bool = True, connected: bool = False):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id(label),
        name=label,
        os="windows" if online else None,
        paired=paired,
        online=online,
        connected=connected and paired,
        addresses=("192.0.2.7",) if online else (),
        port=47474 if online else None,
    )


def tkey(direction: str = "send", dev: str = PEER_ID, tid: str | None = None):
    from teleport.core.transfer import Direction, TransferKey

    return TransferKey(dev, Direction(direction), tid or uuid.uuid4().hex)


def tinfo(key, *, status: str = "transferring", names=("video.mp4",), size: int = 1000, done: int = 0):
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import TransferInfo, TransferStatus

    return TransferInfo(
        key=key,
        status=TransferStatus(status),
        files=tuple(FileEntry(n, size, 0) for n in names),
        names=tuple(names),
        file_index=0,
        done_bytes=done,
        total_bytes=size * len(names),
    )


def status_changed(key, status: str = "transferring", done: int = 0):
    from teleport.app.transfers import TransferStatusChanged

    return TransferStatusChanged(tinfo(key, status=status, done=done))


def finished(key, result: str = "done"):
    from teleport.app.transfers import TransferFinished, TransferResult

    return TransferFinished(
        info=tinfo(key, status="verifying", done=1000),
        result=TransferResult(result),
        by_peer=False,
        reason=None,
        file_index=None,
        reject=None,
    )


def hentry(dev: str, *, direction: str = "receive", result: str = "done", names=("bao-cao.pdf",)):
    from teleport.core.history import HistoryEntry

    return HistoryEntry(
        direction=direction,
        device_id=dev,
        device_name="X",
        transfer_id=uuid.uuid4().hex,
        file_count=len(names) or 1,
        total_bytes=10,
        done_bytes=10 if result == "done" else 0,
        names=tuple(names),
        result=result,
        finished_at=datetime.datetime(2026, 10, 31, 2, 15, tzinfo=datetime.UTC),
        saved_path=None,
    )


def settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


# =================================================================================================
# Host / node giả
# =================================================================================================


class FakeTransfers:
    def __init__(self, infos=(), history=()) -> None:
        self.infos = tuple(infos)
        self._history = history
        self.sent: list = []
        self.cancelled: list = []

    def transfers(self):
        return self.infos

    def history(self, device_id=None):
        if isinstance(self._history, BaseException):
            raise self._history
        return tuple(self._history)

    def send(self, device_id, paths):
        self.sent.append((device_id, [Path(p) for p in paths]))
        return SimpleNamespace()

    def cancel(self, key):
        self.cancelled.append(key)

    def decide(self, key, accept):
        pass


class FakeNode:
    def __init__(self, devices=(), infos=(), history=()) -> None:
        self._devices = tuple(devices)
        self.transfers = FakeTransfers(infos, history)

    def devices(self):
        return self._devices


class _Job:
    def cancel(self) -> None:
        pass


class FakeHost(QObject):
    pairing_prompt = Signal(object)
    pairing_finished = Signal(object)

    def __init__(self, receive_dir: Path) -> None:
        super().__init__()
        from teleport.ui.bridge import NodeOptions

        self.options = NodeOptions(config_dir=receive_dir.parent / "cfg", receive_dir=receive_dir, mdns=None)
        self.calls: list = []
        self.submits: list = []

    def call(self, fn, on_done=None) -> None:
        self.calls.append((fn, on_done))

    def submit(self, fn, on_done=None):
        self.submits.append((fn, on_done))
        return _Job()

    def run_calls(self, node: FakeNode) -> None:
        pending, self.calls = self.calls, []
        for fn, on_done in pending:
            try:
                result = fn(node)
            except Exception as e:
                result = e
            if on_done is not None:
                on_done(result)

    def sent(self) -> list:
        node = FakeNode()
        for fn, _ in self.submits:
            fn(node)
        return node.transfers.sent


# =================================================================================================
# Fixture
# =================================================================================================


@pytest.fixture
def clock(monkeypatch):
    from teleport.ui import conversations, transfers

    monkeypatch.setattr(conversations, "_wall_now", lambda: NOW)
    monkeypatch.setattr(transfers, "_wall_now", lambda: NOW)


@pytest.fixture
def window(qtbot, clock):
    from teleport.ui.main_window import MainWindow

    w = MainWindow()
    qtbot.addWidget(w)
    return w


@pytest.fixture
def env(qtbot, monkeypatch, tmp_path, clock):
    from teleport.ui import transfers as mod
    from teleport.ui.main_window import MainWindow
    from teleport.ui.tray import Tray

    monkeypatch.setattr(Tray, "show_message", lambda self, text, folder=None: None)
    window = MainWindow()
    qtbot.addWidget(window)
    tray = Tray()
    ctl = mod.TransfersController(window, tray, parent=None)
    host = FakeHost(tmp_path / "nhan")
    ctl.attach(host)
    ns = SimpleNamespace(mod=mod, window=window, tray=tray, ctl=ctl, host=host, tmp=tmp_path)
    yield ns
    ctl.close_all()
    tray.dispose()


def show_window(window, qtbot, width: int = 980, height: int = 660, *, active: bool = False) -> None:
    if not active:
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.resize(width, height)
    window.show()
    qtbot.waitExposed(window)
    if active:
        window.activateWindow()
        window.raise_()
        qtbot.waitUntil(window.isActiveWindow, timeout=3000)
    settle()


def running(window, tmp_path: Path) -> None:
    window.show_running("May-A", tmp_path / "nhan")
    settle()


def status_of(window, did: str) -> str:
    from teleport.ui.conversations import STATUS_ROLE

    m = window.conversations.model
    row = m.row_of(did)
    assert row >= 0, "máy không có trong danh sách"
    return m.index(row, 0).data(STATUS_ROLE)


# =================================================================================================
# W1: một model dùng chung
# =================================================================================================


def test_w1_discover_and_conversations_share_window_devices(window):
    from teleport.ui.radar import DeviceListModel

    assert isinstance(window.devices, DeviceListModel)
    assert window.discover.model is window.devices
    assert window.conversations.model.sourceModel() is window.devices
    assert not hasattr(window, "radar"), "widget radar cũ đã gỡ (spec 2.1)"


def test_w1_window_has_conversations_and_compact_machine(window):
    from teleport.ui.conversations import ConversationList

    assert isinstance(window.conversations, ConversationList)
    assert window.sidebar_scroll.widget().isAncestorOf(window.conversations)
    assert window.compact_machine.objectName() == "compactMachine"


# =================================================================================================
# W2: tiêu điểm khi vào RUNNING
# =================================================================================================


@pytest.mark.parametrize(
    ("views", "target"),
    [
        ([dview(PEER), dview(OTHER, online=False)], "list"),
        ([], "add"),
        ([dview(PEER, paired=False), dview(OTHER, paired=False)], "add"),  # W2b (12.4)
    ],
    ids=["co-may", "rong", "chi-chua-ghep"],
)
def test_w2_place_focus_on_running(window, qtbot, tmp_path, views, target):
    show_window(window, qtbot, active=True)
    window.devices.reset(views)
    running(window, tmp_path)
    conv = window.conversations
    expected = conv.list_view if target == "list" else conv.add_button
    qtbot.waitUntil(lambda: QApplication.focusWidget() is expected, timeout=2000)
    if target == "list":
        assert conv.list_view.currentIndex().isValid(), "tiêu điểm vào hàng hiện hành"


# =================================================================================================
# W3: thứ tự Tab (spec 2.9)
# =================================================================================================


def tab_cycle(window, start, key=Qt.Key.Key_Tab, steps: int = 150) -> tuple[list, bool]:
    start.setFocus(Qt.FocusReason.OtherFocusReason)
    QApplication.processEvents()
    assert window.focusWidget() is start
    order = [start]
    for _ in range(steps):
        current = window.focusWidget()
        if key == Qt.Key.Key_Backtab:
            QTest.keyClick(current, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        else:
            QTest.keyClick(current, key)
        QApplication.processEvents()
        focus = window.focusWidget()
        if focus is start:
            return order, True
        if focus is not None and all(focus is not w for w in order):
            order.append(focus)
    return order, False


def index_of(order: list, widget) -> int:
    for i, w in enumerate(order):
        if w is widget:
            return i
    raise AssertionError(f"{widget.objectName() or type(widget).__name__} không có trong chuỗi Tab")


def _fill(env) -> None:
    w = env.window
    running(w, env.tmp)
    w.set_addresses(["192.0.2.5"], None)
    w.devices.reset([dview(PEER, connected=True), dview(OTHER)])
    env.ctl.on_transfer(status_changed(tkey("send")))
    settle()


def test_w3_tab_order_full_sidebar(env, qtbot):
    """Spec 2.9. Chưa có trang SETTINGS nên `settings_button` ẩn và bị bỏ qua (B1 L6)."""
    _fill(env)
    w = env.window
    show_window(w, qtbot, 980, 660, active=True)
    order, closed = tab_cycle(w, w.discover_button)
    assert closed, f"Tab không quay vòng sau {len(order)} widget"
    conv = w.conversations
    chain = [
        w.discover_button,
        conv.list_view,
        conv.add_button,
        w.name_label,
        w.address_label,
        w.copy_button,
    ]
    idx = [index_of(order, x) for x in chain]
    assert idx == sorted(idx), [type(x).__name__ for x in chain]
    in_area = [i for i, x in enumerate(order) if w.transfers_area.isAncestorOf(x)]
    assert in_area, "Tab không vào được thẻ lượt truyền"
    i_open = index_of(order, w.save_bar.open_button)
    assert all(idx[-1] < i < i_open for i in in_area)
    # B1b: “Đổi…” (`change_button`) ngay sau “Mở thư mục”, cuối chuỗi.
    assert i_open == len(order) - 2, f"“{OPEN_DIR}” phải ngay trước “Đổi…”"
    assert index_of(order, w.save_bar.change_button) == len(order) - 1, "“Đổi…” phải cuối chuỗi"
    assert all(x is not w.compact_machine for x in order), "compact_machine ẩn thì bị bỏ qua"


def test_w3_tab_order_compact_sidebar(env, qtbot):
    _fill(env)
    w = env.window
    show_window(w, qtbot, 720, 660, active=True)
    assert w.sidebar_compact
    order, closed = tab_cycle(w, w.discover_button)
    assert closed
    conv = w.conversations
    chain = [w.discover_button, conv.list_view, conv.add_button, w.compact_machine]
    idx = [index_of(order, x) for x in chain]
    assert idx == sorted(idx)
    for hidden in (w.name_label, w.address_label, w.copy_button):
        assert all(x is not hidden for x in order), "widget ẩn bị Tab ghé"
    assert index_of(order, w.save_bar.open_button) == len(order) - 2
    assert index_of(order, w.save_bar.change_button) == len(order) - 1


def test_w3_shift_tab_reverses_outside_rows(env, qtbot):
    _fill(env)
    w = env.window
    show_window(w, qtbot, 980, 660, active=True)
    forward, closed = tab_cycle(w, w.discover_button)
    assert closed
    backward, closed = tab_cycle(w, w.discover_button, Qt.Key.Key_Backtab)
    assert closed
    assert [forward[0], *reversed(forward[1:])] == backward


def test_w3_list_accessible_name(window):
    lst = window.conversations.list_view
    assert lst.accessibleName() == "Máy đã ghép đôi"
    assert lst.accessibleDescription() == (  # spec B3 mục 6
        "Mũi tên để chọn máy, Tab để đi qua từng máy, Enter để mở cuộc trò chuyện, phím Menu để mở thao tác."
    )


# =================================================================================================
# W4, W5: mở cuộc trò chuyện, mở ô nhập IP
# =================================================================================================


def test_w4_open_conversation_from_discover(window, qtbot, tmp_path):
    from teleport.ui.main_window import Page

    window.devices.reset([dview(PEER), dview(OTHER)])
    running(window, tmp_path)
    show_window(window, qtbot, active=True)
    assert window.open_page(Page.DISCOVER)
    assert window.open_conversation(OTHER_ID)
    settle()
    assert window.current_page is Page.HOME
    assert window.conversations.current_device().device_id == OTHER_ID
    qtbot.waitUntil(lambda: QApplication.focusWidget() is window.conversations.list_view, timeout=2000)


def test_w4_open_conversation_unknown_id_stays(window, qtbot, tmp_path):
    from teleport.ui.main_window import Page

    window.devices.reset([dview(PEER), dview(OTHER, paired=False)])
    running(window, tmp_path)
    show_window(window, qtbot)
    assert window.open_page(Page.DISCOVER)
    assert not window.open_conversation("f" * 32)
    assert not window.open_conversation(OTHER_ID), "máy chưa ghép đôi không có cuộc trò chuyện"
    assert window.current_page is Page.DISCOVER
    # Spec B3 mục 6: máy đã ghép đôi duy nhất tự được chọn; id lạ / máy chưa ghép đôi không đổi nó.
    assert window.conversations.current_device().device_id == PEER_ID


def test_w5_open_address_entry_shows_notice_and_focuses_address_edit(window, qtbot, tmp_path):
    from teleport.ui.main_window import Page
    from teleport.ui.widgets import InfoLevel

    running(window, tmp_path)
    show_window(window, qtbot, active=True)
    window.discover.address_edit.setText("192.0.2")
    assert window.open_address_entry("x")
    settle()
    assert window.current_page is Page.DISCOVER
    bar = window.discover.info_bar
    assert bar.isVisibleTo(window), "12.16: open_page trước show_message (showEvent xóa thanh)"
    assert "x" in (bar.title, bar.text)
    assert bar.level is InfoLevel.INFO
    edit = window.discover.address_edit
    qtbot.waitUntil(lambda: QApplication.focusWidget() is edit, timeout=2000)
    assert edit.selectedText() == edit.text(), "selectAll()"


def test_w5_open_address_entry_without_notice(window, qtbot, tmp_path):
    from teleport.ui.main_window import Page

    running(window, tmp_path)
    show_window(window, qtbot, active=True)
    assert window.open_address_entry()
    settle()
    assert window.current_page is Page.DISCOVER
    assert not window.discover.info_bar.isVisibleTo(window)


def test_w5_open_address_entry_outside_running_is_refused(window):
    from teleport.ui.main_window import Page

    window.show_welcome("May-A")
    assert not window.open_address_entry("x")
    assert window.current_page is Page.HOME


# =================================================================================================
# W6: bàn phím xuống hàng bị che
# =================================================================================================


def test_w6_keyboard_to_hidden_row_scrolls_sidebar(window, qtbot, tmp_path):
    window.devices.reset([dview(f"May-{i:02d}") for i in range(12)])
    running(window, tmp_path)
    show_window(window, qtbot, 720, 460, active=True)
    conv = window.conversations
    lst = conv.list_view
    assert lst.verticalScrollBar().maximum() == 0, "12.3: không thanh cuộn dọc riêng"
    lst.setFocus(Qt.FocusReason.OtherFocusReason)
    lst.setCurrentIndex(lst.model().index(0, 0))
    vp = window.sidebar_scroll.viewport()
    for _ in range(11):
        QTest.keyClick(lst, Qt.Key.Key_Down)
        settle()
        cur = lst.currentIndex()
        rect = lst.visualRect(cur)
        top_left = lst.viewport().mapTo(vp, rect.topLeft())
        on_screen = QRect(top_left, rect.size())
        # Chỉ xét chiều dọc (chiều ngang: K3 ở `test_conversations.py`).
        assert on_screen.top() >= 0 and on_screen.bottom() < vp.height(), (
            f"hàng {cur.row()} ngoài vùng thấy: {on_screen} / {vp.rect()}"
        )
    assert lst.currentIndex().row() == 11


# =================================================================================================
# W7: hoạt động từ TransfersController
# =================================================================================================


def test_w7_transfer_updates_reach_status_line(env):
    w = env.window
    w.devices.reset([dview(PEER, connected=True), dview(OTHER)])
    key = tkey("send")
    env.ctl.on_transfer(status_changed(key, done=620))
    assert status_of(w, PEER_ID) == "Đang gửi · 62%"
    assert w.conversations.model.devices()[0].device_id == PEER_ID, "máy đang truyền lên đầu"
    env.ctl.on_transfer(finished(key))
    assert status_of(w, PEER_ID) == "Trực tuyến · Đã gửi video.mp4"


def test_w7_attach_none_clears_activity(env):
    w = env.window
    w.devices.reset([dview(PEER, connected=True)])
    env.ctl.on_transfer(status_changed(tkey("receive")))
    assert status_of(w, PEER_ID).startswith("Đang nhận")
    env.ctl.attach(None)
    assert status_of(w, PEER_ID) == "Trực tuyến · Windows"


def test_w7_suspended_keeps_busy(env):
    w = env.window
    w.devices.reset([dview(PEER, connected=True)])
    key = tkey("receive")
    env.ctl.on_transfer(status_changed(key, done=300))
    env.ctl.on_transfer(finished(key, "suspended"))
    assert status_of(w, PEER_ID) == "Đang nhận · 30%"


def test_w7b_dropped_new_send_finishing_clears_busy(env):
    """12.2: lượt gửi mới bị gỡ thẻ vì REJECTED, rồi `TransferFinished(CANCELLED_USER)` → hết bận."""
    from teleport.app.node import ConnectFailed
    from teleport.core.links import ConnectResult

    w = env.window
    view = dview(PEER, connected=False)
    w.devices.reset([view])
    key = tkey("send")
    env.ctl.on_transfer(status_changed(key, "waiting"))
    assert env.ctl.view.row(key) is not None
    assert status_of(w, PEER_ID) == "Đang chờ trả lời"
    env.ctl.on_node_event(ConnectFailed(view, ConnectResult.REJECTED))
    assert env.ctl.view.row(key) is None, "tiền đề: thẻ lượt mới bị gỡ"
    env.ctl.on_transfer(status_changed(key, "reconnecting"))  # sự kiện khác của khóa đã gỡ: bỏ
    env.ctl.on_transfer(finished(key, "cancelled_user"))
    text = status_of(w, PEER_ID)
    assert not text.startswith("Đang"), text
    assert text.endswith(" · Đã hủy · video.mp4"), text


def test_w7_snapshot_at_started_marks_busy(env):
    w = env.window
    w.devices.reset([dview(PEER), dview(OTHER)])
    env.ctl.on_started(env.host)
    infos = (tinfo(tkey("send", OTHER_ID), done=500),)
    env.host.run_calls(FakeNode(devices=(dview(PEER), dview(OTHER)), infos=infos))
    assert status_of(w, OTHER_ID) == "Đang gửi · 50%"
    assert w.conversations.model.devices()[0].device_id == OTHER_ID


# =================================================================================================
# W8: lịch sử lúc started
# =================================================================================================


def test_w8_history_gives_last_activity(env):
    w = env.window
    w.devices.reset([dview(PEER), dview(OTHER)])
    env.ctl.on_started(env.host)
    node = FakeNode(
        devices=(dview(PEER), dview(OTHER)),
        history=(hentry(OTHER_ID), hentry(OTHER_ID, result="cancelled_user")),
    )
    env.host.run_calls(node)
    assert status_of(w, OTHER_ID) == "Trực tuyến · Đã nhận bao-cao.pdf"
    assert status_of(w, PEER_ID) == "Trực tuyến · Windows"
    assert w.conversations.model.devices()[0].device_id == OTHER_ID, "có hoạt động gần nhất → lên trước"


def test_w8_history_from_old_host_is_ignored(env, tmp_path):
    w = env.window
    w.devices.reset([dview(OTHER)])
    old = env.host
    env.ctl.on_started(old)
    env.ctl.attach(FakeHost(tmp_path / "nhan2"))
    old.run_calls(FakeNode(devices=(dview(OTHER),), history=(hentry(OTHER_ID),)))
    assert status_of(w, OTHER_ID) == "Trực tuyến · Windows"


def test_w8_history_after_close_all_is_ignored(env):
    w = env.window
    w.devices.reset([dview(OTHER)])
    env.ctl.on_started(env.host)
    env.ctl.close_all()
    env.host.run_calls(FakeNode(devices=(dview(OTHER),), history=(hentry(OTHER_ID),)))
    assert status_of(w, OTHER_ID) == "Trực tuyến · Windows"


def test_w8_history_error_logs_type_name_only(env, caplog):
    w = env.window
    w.devices.reset([dview(OTHER)])
    env.ctl.on_started(env.host)
    secret = "C:/bi-mat/duong-dan"
    with caplog.at_level(logging.WARNING):
        env.host.run_calls(FakeNode(devices=(dview(OTHER),), history=PermissionError(secret)))
    assert status_of(w, OTHER_ID) == "Trực tuyến · Windows"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("PermissionError" in r.getMessage() for r in warnings), [r.getMessage() for r in warnings]
    assert all(secret not in r.getMessage() for r in caplog.records), "không chép nội dung lỗi (mvp N9)"


def test_w8_history_does_not_override_session_activity(env):
    """12.9: mục lịch sử chỉ dùng khi máy chưa có `last` trong phiên."""
    w = env.window
    w.devices.reset([dview(OTHER)])
    env.ctl.on_started(env.host)
    key = tkey("send", OTHER_ID)
    env.ctl.on_transfer(status_changed(key))
    env.ctl.on_transfer(finished(key, "cancelled_user"))
    env.host.run_calls(FakeNode(devices=(dview(OTHER),), history=(hentry(OTHER_ID),)))
    assert status_of(w, OTHER_ID) == "Trực tuyến · Đã hủy · video.mp4"


# =================================================================================================
# W9: signal của danh sách → controller
# =================================================================================================


def test_w9_send_files_requested_goes_to_request_send(env):
    env.window.devices.reset([dview(PEER, connected=True)])
    paths = [env.tmp / "nguon" / "a.bin", env.tmp / "nguon" / "b.bin"]
    env.window.conversations.send_files_requested.emit(PEER_ID, paths)
    assert env.host.sent() == [(PEER_ID, paths)]


def test_w9_pick_files_requested_opens_file_dialog(env, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    made: list = []

    class FakeDialog(QFileDialog):
        def __init__(self, *a, **k) -> None:
            super().__init__()
            made.append(self)

        def open(self, *a, **k):
            pass

        def exec(self, *a, **k):
            return 0

    monkeypatch.setattr(env.mod, "file_dialog_factory", lambda *a, **k: FakeDialog())
    env.window.devices.reset([dview(PEER, connected=True)])
    env.window.conversations.pick_files_requested.emit(PEER_ID)
    assert len(made) == 1
    p = env.tmp / "nguon" / "a.bin"
    made[0].filesSelected.emit([str(p)])
    assert env.host.sent() == [(PEER_ID, [p])]


def test_w9_drop_hint_goes_to_notice(env):
    env.window.devices.reset([dview(PEER)])
    env.window.conversations.drop_files(None, [QUrl.fromLocalFile(str(env.tmp / "a.txt"))])
    assert env.ctl.view.notice_text() == DROP_HINT
    assert env.host.submits == []


def test_w9_active_sends_reads_window_devices(env):
    env.window.devices.reset([dview(PEER, connected=True)])
    env.ctl.on_transfer(status_changed(tkey("send")))
    from teleport.ui import texts

    assert env.ctl.active_sends() == [texts.peer_label(PEER, PEER_ID)]


# =================================================================================================
# W10: GuiApp
# =================================================================================================


@pytest.fixture
def gui_app(qapp, tmp_path, clock):
    from teleport.ui import app as ui_app

    app = ui_app.GuiApp(qapp, tmp_path / "cfg")
    host = FakeHost(tmp_path / "nhan")
    app.transfers.attach(host)
    app.pairing.set_host(host)
    app.host = host
    w = app.window
    w.show_running("May-A", tmp_path / "nhan")
    w.resize(980, 660)
    w.show()
    w.activateWindow()
    settle()
    yield app
    app.pairing.close_all()
    app.transfers.close_all()
    app.tray.dispose()
    w.hide()
    w.deleteLater()
    settle()


def test_w10_open_device_requested_opens_conversation(gui_app):
    from teleport.ui.main_window import Page

    w = gui_app.window
    w.devices.reset([dview(PEER), dview(OTHER)])
    assert w.open_page(Page.DISCOVER)
    w.discover.open_device_requested.emit(OTHER_ID)
    settle()
    assert w.current_page is Page.HOME
    assert w.conversations.current_device().device_id == OTHER_ID


def test_w10_unpair_menu_goes_to_pairing_request_unpair(gui_app, monkeypatch):
    calls: list = []
    monkeypatch.setattr(gui_app.pairing, "request_unpair", lambda *a, **k: calls.append((a, k)))
    w = gui_app.window
    w.devices.reset([dview(PEER)])
    menu = w.conversations.context_menu_for(PEER_ID)
    [action] = [a for a in menu.actions() if a.text().replace("&", "") == "Hủy ghép đôi…"]
    action.trigger()
    settle()
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert PEER_ID in args or kwargs.get("device_id") == PEER_ID
    assert PEER in args or kwargs.get("name") == PEER


def test_w10_node_events_and_devices_changed_update_window_devices(gui_app):
    from teleport.app.node import DeviceOnline

    w = gui_app.window
    gui_app._on_node_event(gui_app.host, DeviceOnline(dview(PEER)))
    assert w.devices.device(PEER_ID) is not None
    assert w.conversations.model.row_of(PEER_ID) >= 0


# =================================================================================================
# W12: selftest
# =================================================================================================


@pytest.mark.slow
def test_w12_selftest_returns_zero(tmp_path):
    import teleport

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["TELEPORT_HOME"] = str(tmp_path / "home")
    src = str(Path(teleport.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([src, env.get("PYTHONPATH", "")])
    code = "from teleport.ui.app import main\nraise SystemExit(main(['--selftest']))\n"
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=120)
    assert p.returncode == 0, (p.returncode, p.stderr.decode("utf-8", "replace"))
