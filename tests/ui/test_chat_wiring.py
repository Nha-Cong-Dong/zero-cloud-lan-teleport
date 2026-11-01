"""Nối `TransfersController` ↔ `ChatView` (spec B3 mục 5 “Luồng dữ liệu”, “Lịch sử”; 9.1, 9.7, 9.17, 9b.22):
T-W (W1–W6), T-HW (HW1–HW3).

Viết từ spec, không từ code. Không socket (trừ W5, HW3 gắn `integration`): `NodeHost` giả ghi lại mọi
`call(fn, on_done)` / `submit(fn, on_done)`; test tự chạy `fn` trên node giả và tự gọi `on_done` (như
`NodeHost` làm trên thread Qt), như `test_transfers_controller.py`. Cửa sổ, khay, `ChatView` là widget thật.

Seam: `teleport.ui.chat._wall_now` (9.13). `ctl._sources` là tên spec đặt (mục 5, 9.1); test đọc nó chỉ ở
ca trần và ca gỡ mục, nơi không có hành vi nhìn thấy được.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel

PEER_ID = "a71c04d2" + "e" * 24
OTHER_ID = "0f0f0f0f" + "1" * 24
PEER = "PC-Nha"
OTHER = "Laptop-Minh"
UTC = datetime.UTC
PREPARING = "Đang chuẩn bị…"  # 9b.22
SENT = "Đã gửi"
SAVED = "Đã lưu"
RESEND = "Gửi lại"
HISTORY_FAILED = "Không đọc được lịch sử với máy này."
NOW = datetime.datetime(2026, 10, 31, 10, 40).timestamp()


# =================================================================================================
# Dữ liệu giả
# =================================================================================================


def tid(i: int) -> str:
    return f"{i:032x}"


def dview(device_id: str = PEER_ID, name: str = PEER, *, connected: bool = True, online: bool = True):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id,
        name=name,
        os="windows",
        paired=True,
        online=online,
        connected=connected,
        addresses=("192.0.2.7",) if online else (),
        port=47474 if online else None,
    )


def tkey(direction: str = "send", i: int = 1, device_id: str = PEER_ID):
    from teleport.core.transfer import Direction, TransferKey

    return TransferKey(device_id, Direction(direction), tid(i))


def tinfo(key, status: str = "waiting", names=("video.mp4",)):
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import TransferInfo, TransferStatus

    return TransferInfo(
        key=key,
        status=TransferStatus(status),
        files=tuple(FileEntry(n, 1000, 0) for n in names),
        names=tuple(names),
        file_index=0,
        done_bytes=0,
        total_bytes=1000 * len(names),
    )


def changed(key, status: str = "waiting"):
    from teleport.app.transfers import TransferStatusChanged

    return TransferStatusChanged(tinfo(key, status))


def finished(key, result: str = "done"):
    from teleport.app.transfers import TransferFinished, TransferResult

    return TransferFinished(
        info=tinfo(key, "transferring"),
        result=TransferResult(result),
        by_peer=False,
        reason=None,
        file_index=None,
        reject=None,
    )


def hentry(i: int, device_id: str = PEER_ID, direction: str = "send", result: str = "done"):
    from teleport.core.history import HistoryEntry

    return HistoryEntry(
        direction=direction,
        device_id=device_id,
        device_name=None,
        transfer_id=tid(i),
        file_count=1,
        total_bytes=1000,
        done_bytes=1000 if result == "done" else 0,
        names=("video.mp4",),
        result=result,
        finished_at=datetime.datetime(2026, 10, 30, 9, i % 60, tzinfo=UTC),
        saved_path=None,
    )


class FakeTransfers:
    def __init__(self, entries=(), history_error: BaseException | None = None) -> None:
        self.sent: list[tuple[str, list[Path]]] = []
        self.cancelled: list = []
        self.history_args: list = []
        self.entries = tuple(entries)
        self.history_error = history_error

    def send(self, device_id, paths):
        self.sent.append((device_id, [Path(p) for p in paths]))
        return SimpleNamespace(kind="coroutine giả")

    def cancel(self, key):
        self.cancelled.append(key)

    def decide(self, key, accept):
        pass

    def transfers(self):
        return ()

    def history(self, device_id=None):
        self.history_args.append(device_id)
        if self.history_error is not None:
            raise self.history_error
        return tuple(e for e in self.entries if device_id is None or e.device_id == device_id)


class FakeNode:
    def __init__(self, devices=(), entries=(), history_error=None) -> None:
        self._devices = tuple(devices)
        self.transfers = FakeTransfers(entries, history_error)
        self.name = "May-A"
        self.port = 47474

    def devices(self):
        return self._devices


class FakeHost:
    def __init__(self, receive_dir: Path) -> None:
        from teleport.ui.bridge import NodeOptions

        self.options = NodeOptions(config_dir=receive_dir.parent / "cfg", receive_dir=receive_dir, mdns=None)
        self.calls: list = []
        self.submits: list = []
        self.running = True
        self.is_stopped = False
        self.node_name = "May-A"

    def call(self, fn, on_done=None) -> None:
        self.calls.append((fn, on_done))

    def submit(self, fn, on_done=None) -> None:
        self.submits.append((fn, on_done))

    def run_calls(self, node: FakeNode) -> None:
        pending, self.calls = self.calls, []
        for fn, on_done in pending:
            try:
                result = fn(node)
            except Exception as e:
                result = e
            if on_done is not None:
                on_done(result)

    def sent(self) -> list[tuple[str, list[Path]]]:
        node = FakeNode()
        for fn, _ in self.submits:
            fn(node)
        return node.transfers.sent

    def finish_submit(self, index: int, result) -> None:
        """Trả kết quả của `submit` thứ `index` (TransferKey hay lỗi) về controller."""
        _, on_done = self.submits[index]
        assert on_done is not None, "submit gửi file phải có on_done"
        on_done(result)


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def settle(qtbot=None, ms: int = 0) -> None:
    for _ in range(3):
        QApplication.processEvents()
    if qtbot is not None and ms:
        qtbot.wait(ms)


def plain(text: str) -> str:
    return text.replace("&", "")


@pytest.fixture
def env(qtbot, monkeypatch, tmp_path):
    from teleport.ui import chat
    from teleport.ui import transfers as mod
    from teleport.ui.main_window import MainWindow
    from teleport.ui.tray import Tray

    monkeypatch.setattr(chat, "_wall_now", Clock(NOW))
    monkeypatch.setattr(Tray, "show_message", lambda self, text, folder=None: None)
    window = MainWindow()
    qtbot.addWidget(window)
    tray = Tray()
    ctl = mod.TransfersController(window, tray, parent=None)
    host = FakeHost(tmp_path / "nhan")
    ctl.attach(host)
    window.resize(980, 660)
    window.show_running("May-A", tmp_path / "nhan")
    window.show()  # ChatView hiện thật (hẹn giờ debounce, showEvent chạy như trong app)
    qtbot.waitExposed(window)
    ns = SimpleNamespace(
        mod=mod,
        window=window,
        tray=tray,
        ctl=ctl,
        view=ctl.view,
        host=host,
        model=window.devices,
        tmp=tmp_path,
    )
    yield ns
    ctl.close_all()
    tray.dispose()


def paths(env, n: int = 2, tag: str = "tep") -> list[Path]:
    return [env.tmp / "nguon" / f"{tag}-{i}.bin" for i in range(n)]


def thread_bubbles(env, device_id: str = PEER_ID) -> list:
    thread = env.view.thread_for(device_id)
    return [] if thread is None else list(thread.bubbles())


def preparing_labels(env, device_id: str | None = PEER_ID) -> list:
    root = env.view if device_id is None else env.view.thread_for(device_id)
    if root is None:
        return []
    return [lb for lb in root.findChildren(QLabel) if lb.text() == PREPARING and lb.isVisibleTo(env.view)]


def bubble_of(widget):
    from teleport.ui.widgets import ChatBubble

    w = widget
    while w is not None and not isinstance(w, ChatBubble):
        w = w.parentWidget()
    return w


def resend_shown(env, key) -> bool:
    b = env.view.row(key)
    return b is not None and b.resend_button.isVisibleTo(env.view)


def connect_failed(env, result: str, view=None) -> None:
    from teleport.app.node import ConnectFailed
    from teleport.core.links import ConnectResult

    event = ConnectFailed(view or dview(connected=False), ConnectResult(result))
    env.model.apply(event)
    env.ctl.on_node_event(event)


# =================================================================================================
# W1 lượt gửi → bong bóng OUT trong luồng máy đó
# =================================================================================================


def test_w1_send_update_creates_out_bubble_in_that_device_thread(env):
    from teleport.ui.chat import ChatView
    from teleport.ui.widgets import Direction as Side

    assert isinstance(env.ctl.view, ChatView)
    assert env.window.transfers_area.isAncestorOf(env.view)
    env.model.reset([dview(), dview(OTHER_ID, OTHER)])
    k = tkey("send", 1, OTHER_ID)
    env.ctl.on_transfer(changed(k, "transferring"))
    [b] = thread_bubbles(env, OTHER_ID)
    assert b.key == k and b.direction is Side.OUT
    assert thread_bubbles(env, PEER_ID) == []


def test_w1_transfers_area_has_no_margins(env):
    """9.10: `area_layout.setContentsMargins(0, 0, 0, 0)`."""
    m = env.window.transfers_area.layout().contentsMargins()
    assert (m.left(), m.top(), m.right(), m.bottom()) == (0, 0, 0, 0)


def test_w1_cancel_in_bubble_reaches_node(env):
    k = tkey("receive")
    env.ctl.on_transfer(changed(k, "transferring"))
    env.view.row(k).cancel_button.click()
    node = FakeNode()
    env.host.run_calls(node)
    assert node.transfers.cancelled == [k]


def test_w1_view_device_set_once_at_wiring_and_follows_selection(env):
    """9.17 + mục 5: controller đặt `view.set_device(<máy đang chọn>)`; đổi chọn ở cột trái đổi luồng."""
    env.model.reset([dview(), dview(OTHER_ID, OTHER)])
    settle()
    assert env.view.device_id() == env.window.conversations.current_device().device_id
    env.window.conversations.select_device(OTHER_ID)
    settle()
    assert env.view.device_id() == OTHER_ID


def test_w1_drop_hint_from_view_shows_notice(env):
    from teleport.ui.radar import drop_hint_text

    env.view.drop_hint_requested.emit(drop_hint_text())
    assert env.view.notice_text() == drop_hint_text()


def test_w1_files_dropped_on_view_are_sent(env):
    env.model.reset([dview()])
    ps = paths(env)
    env.view.send_files_requested.emit(PEER_ID, ps)
    assert env.host.sent() == [(PEER_ID, ps)]


# =================================================================================================
# W2 Gửi lại
# =================================================================================================


@pytest.mark.parametrize("order", ["done_then_finished", "finished_then_done"])
def test_w2_failed_send_resend_submits_same_paths_once(env, order):
    env.model.reset([dview()])
    ps = paths(env, 3)
    env.ctl.request_send(PEER_ID, ps)
    assert len(env.host.submits) == 1
    k = tkey("send", 1)
    env.ctl.on_transfer(changed(k, "waiting"))
    if order == "done_then_finished":
        env.host.finish_submit(0, k)
        assert not resend_shown(env, k), "chưa kết thúc: không có “Gửi lại”"
        env.ctl.on_transfer(finished(k, "source_changed"))
    else:
        env.ctl.on_transfer(finished(k, "source_changed"))
        env.host.finish_submit(0, k)
    assert resend_shown(env, k)
    b = env.view.row(k)
    assert plain(b.resend_button.text()) == RESEND
    b.resend_button.click()
    assert len(env.host.submits) == 2
    assert env.host.sent() == [(PEER_ID, ps), (PEER_ID, ps)]
    assert not resend_shown(env, k), "bấm được một lần"


@pytest.mark.parametrize(
    ("result", "expected"), [("done", False), ("unpaired", False), ("cancelled_user", True)]
)
def test_w2_resend_only_for_results_other_than_done_and_unpaired(env, result, expected):
    env.model.reset([dview()])
    env.ctl.request_send(PEER_ID, paths(env))
    k = tkey("send", 1)
    env.ctl.on_transfer(changed(k, "transferring"))
    env.host.finish_submit(0, k)
    env.ctl.on_transfer(finished(k, result))
    assert resend_shown(env, k) is expected


@pytest.mark.parametrize("result", ["done", "unpaired"])
def test_w2_done_or_unpaired_forgets_paths(env, result):
    env.model.reset([dview()])
    env.ctl.request_send(PEER_ID, paths(env))
    k = tkey("send", 1)
    env.ctl.on_transfer(changed(k, "transferring"))
    env.host.finish_submit(0, k)
    env.ctl.on_transfer(finished(k, result))
    assert k not in env.ctl._sources


@pytest.mark.parametrize("result", ["done", "unpaired"])
def test_w2_send_done_after_done_or_unpaired_remembers_nothing(env, result):
    """`send` trả về sau khi lượt đã xong DONE / UNPAIRED: không nhớ đường dẫn (lượt đó không có nút)."""
    env.model.reset([dview()])
    env.ctl.request_send(PEER_ID, paths(env))
    k = tkey("send", 1)
    env.ctl.on_transfer(changed(k, "transferring"))
    env.ctl.on_transfer(finished(k, result))
    env.host.finish_submit(0, k)
    assert k not in env.ctl._sources
    assert not resend_shown(env, k)


def test_w2_send_done_for_unknown_key_remembers_nothing(env):
    """9.1: `_on_send_done` bỏ qua khóa không còn bong bóng."""
    env.model.reset([dview()])
    env.ctl.request_send(PEER_ID, paths(env))
    k = tkey("send", 1)
    env.host.finish_submit(0, k)
    assert k not in env.ctl._sources
    env.ctl.on_transfer(finished(k, "expired"))
    assert not resend_shown(env, k)


def test_w2_sources_capped_at_256_entries(env):
    env.model.reset([dview()])
    for i in range(257):
        env.ctl.request_send(PEER_ID, [env.tmp / "nguon" / f"f{i}.bin"])
        k = tkey("send", 10_000 + i)
        env.ctl.on_transfer(changed(k, "transferring"))
        env.host.finish_submit(i, k)
    assert len(env.ctl._sources) <= 256
    assert tkey("send", 10_000) not in env.ctl._sources, "bỏ mục cũ nhất"
    assert tkey("send", 10_256) in env.ctl._sources


def test_w2_sources_capped_at_4096_paths(env):
    from teleport.core.messages import MAX_OFFER_FILES

    env.model.reset([dview()])
    n = 5
    for i in range(n):
        ps = paths(env, MAX_OFFER_FILES, tag=f"lo{i}")
        env.ctl.request_send(PEER_ID, ps)
        k = tkey("send", 20_000 + i)
        env.ctl.on_transfer(changed(k, "transferring"))
        env.host.finish_submit(i, k)
    total = sum(len(v) for v in env.ctl._sources.values())
    assert total <= 4096
    assert tkey("send", 20_000 + n - 1) in env.ctl._sources, "mục mới nhất phải còn"


# =================================================================================================
# W3 lượt bị gỡ vì bị chặn
# =================================================================================================


def test_w3_blocked_new_send_has_no_bubble_no_resend_and_device_not_busy(env):
    from teleport.ui.widgets import InfoLevel

    env.model.reset([dview(connected=False)])
    env.ctl.request_send(PEER_ID, paths(env))
    k = tkey("send", 1)
    env.ctl.on_transfer(changed(k, "waiting"))
    assert env.view.row(k) is not None
    connect_failed(env, "rejected")
    assert env.view.row(k) is None
    assert all(getattr(b, "key", None) != k for b in thread_bubbles(env))
    assert env.view.info_bar.level is InfoLevel.WARNING, "9.7: bị chặn → WARNING"
    env.host.finish_submit(0, k)
    env.ctl.on_transfer(finished(k, "cancelled_user"))
    assert env.view.row(k) is None
    assert all(getattr(b, "key", None) != k for b in thread_bubbles(env))
    assert k not in env.ctl._sources
    assert preparing_labels(env) == []
    assert not env.window.conversations.model.activity(PEER_ID).busy, "máy hết bận"


def test_w3_send_error_notice_is_error_level(env):
    from teleport.ui import send
    from teleport.ui.texts import peer_label
    from teleport.ui.widgets import InfoLevel

    env.model.reset([dview()])
    env.ctl.request_send(PEER_ID, paths(env))
    err = OSError("x")
    env.host.finish_submit(0, err)
    assert env.view.notice_text() == send.send_error_message(err, peer_label(PEER, PEER_ID))
    assert env.view.info_bar.level is InfoLevel.ERROR


# =================================================================================================
# W4 attach xóa mọi luồng và `_sources`
# =================================================================================================


def test_w4_attach_clears_threads_and_sources(env):
    env.model.reset([dview(), dview(OTHER_ID, OTHER)])
    env.ctl.request_send(PEER_ID, paths(env))
    k = tkey("send", 1)
    env.ctl.on_transfer(changed(k, "transferring"))
    env.host.finish_submit(0, k)
    env.ctl.on_transfer(changed(tkey("receive", 2, OTHER_ID), "transferring"))
    new_host = FakeHost(env.tmp / "nhan2")
    env.ctl.attach(new_host)
    assert env.view.rows() == []
    assert thread_bubbles(env, PEER_ID) == [] and thread_bubbles(env, OTHER_ID) == []
    assert not env.ctl._sources
    env.ctl.on_transfer(finished(k, "expired"))
    assert not resend_shown(env, k), "đường dẫn của host cũ đã quên"


# =================================================================================================
# W6 bong bóng “Đang chuẩn bị…” (9b.22)
# =================================================================================================


def test_w6_valid_request_shows_pending_bubble_immediately_not_in_rows(env):
    from teleport.ui import theme
    from teleport.ui.widgets import Direction as Side
    from teleport.ui.widgets import IconButton

    env.model.reset([dview()])
    env.ctl.request_send(PEER_ID, paths(env, 2))
    labels = preparing_labels(env)
    assert len(labels) == 1, "bong bóng tạm hiện ngay, trước khi host trả"
    assert labels[0].property(theme.TONE_PROPERTY) == "muted"
    bubble = bubble_of(labels[0])
    assert bubble is not None and bubble.direction is Side.OUT
    texts = [lb.text() for lb in bubble.findChildren(QLabel)] + [
        lb.accessibleName() for lb in bubble.findChildren(QLabel)
    ]
    assert "2 file" in texts, texts
    buttons = [
        b for b in bubble.findChildren(IconButton) if b.isVisibleTo(bubble) and b.icon_name == "dismiss"
    ]
    assert buttons == [], "bong bóng tạm không có nút ✕"
    assert env.view.rows() == [] and env.view.active_count() == 0


def test_w6_pending_bubble_single_file_shows_file_name(env):
    from teleport.ui.texts import file_label

    env.model.reset([dview()])
    p = env.tmp / "nguon" / "bao-cao.pdf"
    env.ctl.request_send(PEER_ID, [p])
    [label] = preparing_labels(env)
    bubble = bubble_of(label)
    names = [lb.text() for lb in bubble.findChildren(QLabel)] + [
        lb.accessibleName() for lb in bubble.findChildren(QLabel)
    ]
    assert file_label("bao-cao.pdf") in names


def test_w6_host_returns_key_removes_pending_keeps_real_bubble(env):
    env.model.reset([dview()])
    env.ctl.request_send(PEER_ID, paths(env))
    k = tkey("send", 1)
    env.ctl.on_transfer(changed(k, "waiting"))  # bong bóng thật có thể tới trước
    assert len(preparing_labels(env)) == 1
    env.host.finish_submit(0, k)
    assert preparing_labels(env) == []
    assert env.view.row(k) is not None
    assert [b.key for b in thread_bubbles(env)] == [k], "bong bóng thật đứng đúng chỗ, không còn bản tạm"


def test_w6_host_error_removes_pending_and_shows_notice(env):
    env.model.reset([dview()])
    env.ctl.request_send(PEER_ID, paths(env))
    env.host.finish_submit(0, OSError("x"))
    assert preparing_labels(env) == []
    assert env.view.notice_text()


def test_w6_host_runtime_error_removes_pending_silently(env):
    env.model.reset([dview()])
    env.ctl.request_send(PEER_ID, paths(env))
    env.host.finish_submit(0, RuntimeError("node đang dừng"))
    assert preparing_labels(env) == []


def test_w6_attach_and_clear_remove_pending(env):
    env.model.reset([dview()])
    env.ctl.request_send(PEER_ID, paths(env))
    assert preparing_labels(env)
    env.ctl.attach(FakeHost(env.tmp / "nhan2"))
    assert preparing_labels(env, None) == []
    env.ctl.request_send(PEER_ID, paths(env))
    assert preparing_labels(env)
    env.view.clear()
    assert preparing_labels(env, None) == []


def test_w6_old_host_result_still_removes_its_pending(env):
    """Host cũ trả sau `attach`: không để lại bong bóng tạm (đã gỡ ở `attach`), không lỗi."""
    env.model.reset([dview()])
    env.ctl.request_send(PEER_ID, paths(env))
    old = env.host
    env.ctl.attach(FakeHost(env.tmp / "nhan2"))
    old.finish_submit(0, tkey("send", 1))
    assert preparing_labels(env, None) == []
    assert env.view.notice_text() == ""


@pytest.mark.parametrize("case", ["duplicate", "blocked"])
def test_w6_rejected_request_shows_no_pending(env, case):
    if case == "duplicate":
        env.model.reset([dview()])
        a = env.tmp / "nguon" / "a.bin"
        env.ctl.request_send(PEER_ID, [a, a])
    else:
        env.model.reset([dview(connected=False)])
        connect_failed(env, "rejected")
        env.ctl.request_send(PEER_ID, paths(env))
    assert env.host.submits == []
    assert preparing_labels(env, None) == []


def test_w6_view_add_and_remove_pending_api(env):
    env.model.reset([dview()])
    t1 = env.view.add_pending(PEER_ID, paths(env))
    t2 = env.view.add_pending(PEER_ID, paths(env, 1))
    assert isinstance(t1, int) and isinstance(t2, int) and t2 > t1
    assert len(preparing_labels(env)) == 2
    env.view.remove_pending(t1)
    assert len(preparing_labels(env)) == 1
    env.view.remove_pending(t1)  # gỡ hai lần vô hại
    env.view.remove_pending(t2)
    assert preparing_labels(env) == []


def test_w6_pending_makes_thread_non_empty(env, qtbot):
    from teleport.ui.chat import HISTORY_DEBOUNCE_MS
    from teleport.ui.widgets import EmptyState

    env.model.reset([dview()])
    qtbot.wait(HISTORY_DEBOUNCE_MS * 3)  # LOADING (node chưa started: không ai nạp)
    env.view.load_history(PEER_ID, [])
    [es] = env.view.findChildren(EmptyState)
    assert es.isVisibleTo(env.view)
    env.ctl.request_send(PEER_ID, paths(env))
    assert not es.isVisibleTo(env.view), "bong bóng tạm làm luồng không rỗng"


# =================================================================================================
# HW1 on_started đọc lịch sử cho máy đang mở
# =================================================================================================


def _history_calls(env, node: FakeNode) -> list:
    env.host.run_calls(node)
    return [a for a in node.transfers.history_args if a is not None]


def test_hw1_history_waits_for_started_then_reads_open_device_once(env, qtbot):
    from teleport.ui.chat import HISTORY_DEBOUNCE_MS, HistoryState
    from teleport.ui.chat_bubbles import HistoryBubble

    env.model.reset([dview()])
    qtbot.wait(HISTORY_DEBOUNCE_MS * 3)
    assert env.view.device_id() == PEER_ID
    early = FakeNode([dview()], [hentry(1), hentry(2)])
    assert _history_calls(env, early) == [], "chưa started: không đọc lịch sử"
    env.ctl.on_started(env.host)
    node = FakeNode([dview()], [hentry(1), hentry(2)])
    assert _history_calls(env, node) == [PEER_ID]
    qtbot.wait(HISTORY_DEBOUNCE_MS * 3)
    assert _history_calls(env, FakeNode([dview()])) == [], "không đọc hai lần"
    thread = env.view.thread_for(PEER_ID)
    assert thread.history_state is HistoryState.LOADED
    assert len([b for b in thread.bubbles() if isinstance(b, HistoryBubble)]) == 2


def test_hw1_after_started_each_open_reads_history(env, qtbot):
    from teleport.ui.chat import HISTORY_DEBOUNCE_MS

    env.model.reset([dview(), dview(OTHER_ID, OTHER)])
    env.ctl.on_started(env.host)
    entries = [hentry(1), hentry(2, OTHER_ID)]
    env.host.run_calls(FakeNode([dview(), dview(OTHER_ID, OTHER)], entries))
    conv = env.window.conversations
    for target in (OTHER_ID, PEER_ID, OTHER_ID):
        conv.select_device(target)
        qtbot.wait(HISTORY_DEBOUNCE_MS * 3)
        node = FakeNode([dview(), dview(OTHER_ID, OTHER)], entries)
        assert _history_calls(env, node) == [target]


# =================================================================================================
# HW2 host cũ / close_all / lỗi
# =================================================================================================


def _started_with_pending_history(env, qtbot):
    from teleport.ui.chat import HISTORY_DEBOUNCE_MS

    env.model.reset([dview(), dview(OTHER_ID, OTHER)])
    env.ctl.on_started(env.host)
    env.host.run_calls(FakeNode([dview(), dview(OTHER_ID, OTHER)]))
    env.window.conversations.select_device(OTHER_ID)
    qtbot.wait(HISTORY_DEBOUNCE_MS * 3)
    assert env.host.calls, "phải có lời gọi history đang chờ"


def _history_bubbles(env, device_id):
    from teleport.ui.chat_bubbles import HistoryBubble

    return [b for b in thread_bubbles(env, device_id) if isinstance(b, HistoryBubble)]


def test_hw2_result_of_old_host_is_dropped(env, qtbot):
    _started_with_pending_history(env, qtbot)
    old = env.host
    env.ctl.attach(FakeHost(env.tmp / "nhan2"))
    old.run_calls(FakeNode([], [hentry(5, OTHER_ID)]))
    assert _history_bubbles(env, OTHER_ID) == []


def test_hw2_result_after_close_all_is_dropped(env, qtbot):
    _started_with_pending_history(env, qtbot)
    env.ctl.close_all()
    env.host.run_calls(FakeNode([], [hentry(5, OTHER_ID)]))
    assert _history_bubbles(env, OTHER_ID) == []


def test_hw2_error_logs_type_name_and_shows_failed_line(env, qtbot, caplog):
    from teleport.ui.chat import HistoryState

    _started_with_pending_history(env, qtbot)
    with caplog.at_level(logging.WARNING):
        env.host.run_calls(FakeNode([], history_error=RuntimeError("bi-mat-xyz")))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("RuntimeError" in r.getMessage() for r in warnings), [r.getMessage() for r in warnings]
    assert all("bi-mat-xyz" not in r.getMessage() for r in caplog.records), "chỉ ghi tên loại lỗi"
    thread = env.view.thread_for(OTHER_ID)
    assert thread.history_state is HistoryState.FAILED
    assert any(lb.text() == HISTORY_FAILED for lb in thread.findChildren(QLabel) if lb.isVisibleTo(thread))


# =================================================================================================
# W5, HW3 (integration): hai node thật trên 127.0.0.1
# =================================================================================================

LOCAL = "127.0.0.1"


class Peer:
    """Máy B: `Node` thật trong thread riêng, tự nhận."""

    def __init__(self, config: Path, receive_dir: Path) -> None:
        self.config = config
        self.receive_dir = receive_dir
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="test-peer-b3", daemon=True)
        self.node = None

    def start(self) -> Peer:
        from teleport.app.node import Node

        self.thread.start()

        async def go():
            self.node = Node(
                self.config, name="May-B", host=LOCAL, port=0, receive_dir=self.receive_dir, auto_accept=True
            )
            await self.node.start()

        self.run(go(), 20)
        return self

    def run(self, coro, timeout: float = 10.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def soon(self, fn, *args) -> None:
        self.loop.call_soon_threadsafe(fn, *args)

    def connected_to(self, device_id: str) -> bool:
        async def check() -> bool:
            return device_id in self.node.connected_devices()

        return self.run(check(), 5)

    def send(self, device_id: str, *ps: Path):
        return asyncio.run_coroutine_threadsafe(self.node.transfers.send(device_id, list(ps)), self.loop)

    def close(self) -> None:
        if self.node is not None:
            with contextlib.suppress(Exception):
                self.run(asyncio.wait_for(self.node.close(), 30), 35)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        if not self.thread.is_alive():
            self.loop.close()


@pytest.fixture
def peer_b(tmp_path):
    made: list[Peer] = []

    def make(config: Path) -> Peer:
        p = Peer(config, tmp_path / "nhan-B")
        made.append(p)
        return p.start()

    yield make
    for p in reversed(made):
        p.close()


def _controller(run):
    app = run.tray.parent()
    assert app is not None and hasattr(app, "transfers")
    return app.transfers


def _start(run, gui_wait, a_id, p):
    yield gui_wait(lambda: run.hosts and run.host.running, 15, "node A chạy")
    got: list = []
    run.host.call(lambda node: node.port, got.append)
    yield gui_wait(lambda: got, 5, "cổng của A")
    p.soon(p.node.connect_hint, a_id, [LOCAL], got[0])
    yield gui_wait(lambda: p.connected_to(a_id), 10, "B nối tới A")


def _session_bubble(ctl, direction: str):
    for r in ctl.view.rows():
        if str(r.key.direction) == direction:
            return r
    return None


@pytest.mark.integration
@pytest.mark.usefixtures("offers_as_boxes")  # spec-b4 11.3: test chờ hộp hỏi nhận file
def test_w5_gui_send_and_receive_show_sent_and_saved(
    gui, gui_wait, config_dir, trust_each_other, peer_b, tmp_path, ui_helpers
):
    a, b = config_dir("A"), config_dir("B")
    id_a, id_b = trust_each_other(a, b)
    a_id, b_id = id_a.device_id, id_b.device_id
    p = peer_b(b)
    src = tmp_path / "nguon"
    src.mkdir()
    out_file = src / "di.bin"
    out_file.write_bytes(b"a" * 50_000)
    in_file = src / "den.bin"
    in_file.write_bytes(b"b" * 40_000)
    seen: dict = {}

    def script(run):
        yield from _start(run, gui_wait, a_id, p)
        ctl = _controller(run)
        yield gui_wait(lambda: run.window.devices.device(b_id) is not None, 10, "B trong danh sách")
        ctl.request_send(b_id, [out_file])
        yield gui_wait(
            lambda: (r := _session_bubble(ctl, "send")) is not None and r.finished, 30, "bong bóng gửi xong"
        )
        out = _session_bubble(ctl, "send")
        seen["out"] = (str(out.direction), out.status_label.text(), out.key.device_id)
        p.send(a_id, in_file)
        yield gui_wait(lambda: ctl.offers.keys(), 10, "hộp hỏi nhận file")
        [k] = list(ctl.offers.keys())
        buttons = ui_helpers.buttons_with_text(ctl.offers.box(k), ui_helpers.M6_ACCEPT)
        buttons[0].click()
        yield gui_wait(lambda: (r := ctl.view.row(k)) is not None and r.finished, 30, "bong bóng nhận xong")
        got = ctl.view.row(k)
        seen["in"] = (str(got.direction), got.status_label.text())
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script)
    assert seen["out"] == ("out", SENT, b_id)
    assert seen["in"] == ("in", SAVED)


@pytest.mark.integration
def test_hw3_finished_session_transfer_then_switch_back_gives_one_bubble(
    gui, gui_wait, config_dir, trust_each_other, peer_b, tmp_path
):
    a, b, c = config_dir("A"), config_dir("B"), config_dir("C")
    id_a, id_b = trust_each_other(a, b)
    _, id_c = trust_each_other(a, c, name_b="May-C")
    a_id, b_id, c_id = id_a.device_id, id_b.device_id, id_c.device_id
    p = peer_b(b)
    src = tmp_path / "nguon"
    src.mkdir()
    f = src / "mot.bin"
    f.write_bytes(b"x" * 30_000)
    seen: dict = {}

    def script(run):
        from teleport.ui.chat import HistoryState

        yield from _start(run, gui_wait, a_id, p)
        ctl = _controller(run)
        conv = run.window.conversations
        yield gui_wait(lambda: conv.model.row_of(c_id) >= 0 and conv.model.row_of(b_id) >= 0, 10, "B, C")
        assert conv.select_device(b_id)
        ctl.request_send(b_id, [f])
        yield gui_wait(lambda: (r := _session_bubble(ctl, "send")) is not None and r.finished, 30, "gửi xong")
        k = _session_bubble(ctl, "send").key
        assert conv.select_device(c_id)
        yield gui_wait(lambda: ctl.view.device_id() == c_id, 5, "mở luồng C")
        assert conv.select_device(b_id)
        thread = ctl.view.thread_for(b_id)
        yield gui_wait(lambda: thread.history_state is HistoryState.LOADED, 10, "lịch sử B đã nạp")
        seen["count"] = sum(1 for x in thread.bubbles() if getattr(x, "key", None) == k)
        seen["state"] = thread.history_state
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script)
    assert seen["count"] == 1


@pytest.fixture
def offers_as_boxes(monkeypatch):
    """spec-b4 11.3: đang nhìn đúng luồng của máy gửi thì lời mời chỉ ở bong bóng; test chờ hộp hỏi
    vá `ui.transfers.default_window_active` thành False để hộp luôn bật."""
    from teleport.ui import transfers

    monkeypatch.setattr(transfers, "default_window_active", lambda: False)
