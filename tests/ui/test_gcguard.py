"""GC vòng tham chiếu chỉ chạy trên thread Qt (`teleport.ui.gcguard.GcGuard`).

Gốc lỗi: GC của CPython chạy ở thread nào đang cấp phát, nên đối tượng PySide trong vòng tham chiếu
có thể bị hủy ở thread node / thread đĩa (segfault trên CI macOS). Fixture autouse `qt_thread_gc`
(conftest) đã bật một guard cho mọi test ui; các test ở đây tắt nó trước khi cần quan sát guard
đang thử một mình.
"""

from __future__ import annotations

import gc
import threading
import weakref
from contextlib import contextmanager

import pytest

pytest.importorskip("PySide6")


class _Cycle:
    """Đối tượng tự trỏ vào mình: chỉ GC vòng tham chiếu thu gom được."""

    def __init__(self) -> None:
        self.me = self


def _young_threshold() -> int:
    return max(gc.get_threshold()[0], 700)


@contextmanager
def _gc_starts():
    """Ghi tên thread của mọi lần GC bắt đầu trong khối `with`."""
    names: list[str] = []

    def callback(phase, info):
        if phase == "start":
            names.append(threading.current_thread().name)

    gc.callbacks.append(callback)
    try:
        yield names
    finally:
        gc.callbacks.remove(callback)


# ---- Hàm thuần --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "threshold", "want"),
    [
        ((10, 0, 0), (2000, 10, 10), None),
        ((2000, 0, 0), (2000, 10, 10), None),
        ((2001, 0, 0), (2000, 10, 10), 0),
        ((2001, 11, 0), (2000, 10, 10), 1),
        ((2001, 11, 11), (2000, 10, 10), 2),
        ((2001, 3, 11), (2000, 10, 10), 0),
        ((2001, 11, 50), (2000, 10, 0), 1),  # 3.14: ngưỡng thế hệ 2 có thể là 0
        ((99999, 99, 99), (0, 10, 10), None),  # set_threshold(0): không thu gom theo đếm
    ],
)
def test_generation_to_collect(count, threshold, want):
    from teleport.ui.gcguard import generation_to_collect

    assert generation_to_collect(count, threshold) == want


def test_gc_api_shape_is_what_guard_expects():
    """Bản Python đang chạy (3.12 / 3.14) vẫn trả bộ ba số: guard không rơi vào nhánh dự phòng."""
    from teleport.ui import gcguard

    assert gcguard._triple(gc.get_count()) is not None
    assert gcguard._triple(gc.get_threshold()) is not None


# ---- Bật/tắt, lồng nhau ------------------------------------------------------------------------


def test_start_disables_and_stop_restores_enabled(qt_thread_gc):
    from teleport.ui.gcguard import GcGuard

    qt_thread_gc.stop()
    assert gc.isenabled()
    guard = GcGuard()
    guard.start()
    assert not gc.isenabled()
    assert guard.active
    guard.stop()
    assert gc.isenabled()
    assert not guard.active
    guard.stop()  # lần hai không làm gì
    assert gc.isenabled()


def test_stop_keeps_gc_disabled_when_it_was_disabled(qt_thread_gc):
    from teleport.ui.gcguard import GcGuard

    qt_thread_gc.stop()
    gc.disable()
    try:
        guard = GcGuard()
        guard.start()
        guard.stop()
        assert not gc.isenabled()
    finally:
        gc.enable()


def test_nested_guards_restore_in_order(qt_thread_gc):
    from teleport.ui.gcguard import GcGuard

    qt_thread_gc.stop()
    outer, inner = GcGuard(), GcGuard()
    outer.start()
    inner.start()
    inner.stop()
    assert not gc.isenabled(), "guard trong dừng thì guard ngoài vẫn giữ GC tắt"
    outer.stop()
    assert gc.isenabled()


def test_stop_collects_once_on_calling_thread(qt_thread_gc):
    from teleport.ui.gcguard import GcGuard

    qt_thread_gc.stop()
    guard = GcGuard()
    guard.start()
    seen: list[str] = []
    obj = _Cycle()
    weakref.finalize(obj, lambda: seen.append(threading.current_thread().name))
    del obj
    assert seen == []
    guard.stop()
    assert seen == [threading.current_thread().name]


# ---- Hẹn giờ thu gom trên thread Qt ------------------------------------------------------------


def test_timer_collects_cycles_from_worker_thread_on_qt_thread(qt_thread_gc, qtbot):
    """Rác vòng tạo ở thread khác, thread đó cấp phát vượt ngưỡng: chỉ hẹn giờ (thread Qt) thu gom."""
    from teleport.ui.gcguard import GcGuard

    qt_thread_gc.stop()
    guard = GcGuard(interval_ms=20)
    guard.start()
    finalized: list[str] = []
    held: list = []
    try:
        with _gc_starts() as starts:

            def work() -> None:
                obj = _Cycle()
                weakref.finalize(obj, lambda: finalized.append(threading.current_thread().name))
                del obj
                # Giữ lại tới cuối test: bộ đếm của GC là cấp phát trừ giải phóng, thả ngay thì tụt lại.
                held.append([[] for _ in range(_young_threshold() * 5)])  # GC tự động sẽ chạy ở đây

            worker = threading.Thread(target=work, name="gc-test-worker")
            worker.start()
            worker.join(10)
            qtbot.waitUntil(lambda: bool(finalized), timeout=5000)
        assert guard.collections >= 1
    finally:
        held.clear()
        guard.stop()
    qt_name = threading.current_thread().name
    assert finalized == [qt_name]
    assert starts and set(starts) == {qt_name}, starts


# ---- Tích hợp: GUI thật, node thật, hộp hỏi nhận file -------------------------------------------


@pytest.mark.integration
def test_gui_collects_qt_garbage_only_on_qt_thread(
    qt_thread_gc, gui, gui_wait, config_dir, trust_each_other, peer_node, tmp_path, ui_helpers
):
    """`main` (không có guard của fixture) chạy node A; B gửi file, A từ chối (hộp hỏi đóng thành rác),
    thêm rác Qt trong vòng tham chiếu; B cấp phát vượt ngưỡng trên thread của nó. Mọi lần GC trong lúc
    `main` chạy phải ở thread Qt, và rác Qt được hủy trên thread Qt."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog

    from teleport.trust.identity import load_or_create_identity

    qt_thread_gc.stop()  # chỉ còn guard của `main`: bỏ guard trong `main` thì test này phải đỏ
    assert gc.isenabled()

    a, b = config_dir("A"), config_dir("B")
    trust_each_other(a, b)
    a_id = load_or_create_identity(a).device_id
    peer = peer_node(b)
    src = tmp_path / "nguon"
    src.mkdir()
    f = src / "a.bin"
    f.write_bytes(b"x" * 300_000)
    qt_name = threading.current_thread().name
    finalized: list[str] = []
    burst_done = threading.Event()
    seen: dict = {}

    class CyclicTimer(QTimer):
        def __init__(self) -> None:
            super().__init__()
            self.me = self

    def offer_boxes():
        return [
            w
            for w in QApplication.topLevelWidgets()
            if isinstance(w, QDialog)
            and w.isVisible()
            and ui_helpers.buttons_with_text(w, ui_helpers.M6_ACCEPT)
        ]

    held: list = []

    def burst() -> None:  # chạy trên thread của B; giữ lại để bộ đếm của GC không tụt
        held.append([[] for _ in range(_young_threshold() * 10)])
        burst_done.set()

    def script(run):
        yield gui_wait(lambda: run.hosts and run.host.running, 15, "node A chạy")
        seen["gc_enabled_in_main"] = gc.isenabled()
        with _gc_starts() as starts:
            seen["starts"] = starts
            got: list = []
            run.host.call(lambda node: node.port, got.append)
            yield gui_wait(lambda: got, 5, "cổng của A")
            peer.soon(peer.node.connect_hint, a_id, ["127.0.0.1"], got[0])
            yield gui_wait(lambda: peer.connected_to(a_id), 10, "B nối tới A")
            fut = peer.submit(peer.node.transfers.send(a_id, [f]))
            yield gui_wait(offer_boxes, 10, "hộp hỏi mở")
            [box] = offer_boxes()
            ui_helpers.buttons_with_text(box, ui_helpers.M6_REJECT)[0].click()
            del box
            yield gui_wait(fut.done, 10, "send() trả khóa")
            yield gui_wait(lambda: peer.finished(fut.result().transfer_id), 15, "B thấy lượt kết thúc")
            yield gui_wait(lambda: not offer_boxes(), 3, "hộp hỏi đóng")

            garbage = CyclicTimer()
            weakref.finalize(garbage, lambda: finalized.append(threading.current_thread().name))
            del garbage
            peer.soon(burst)
            yield gui_wait(burst_done.is_set, 10, "B cấp phát xong")
            yield gui_wait(lambda: finalized, 5, "rác Qt được thu gom")
            held.clear()
        run.tray.quit_requested.emit()

    run = gui(["--config-dir", str(a)], script)
    enabled_after_main = gc.isenabled()
    qt_thread_gc.start()  # thread của peer còn sống tới lúc dọn: không để GC chạy ở đó
    assert run.code == 0
    assert enabled_after_main, "main phải khôi phục GC khi trả về"
    assert seen["gc_enabled_in_main"] is False, "main phải tắt GC tự động khi node chạy"
    assert finalized == [qt_name], f"rác Qt bị hủy ngoài thread Qt: {finalized}"
    starts = seen["starts"]
    assert starts, "không có lần GC nào: test không kiểm được gì"
    assert set(starts) == {qt_name}, f"GC chạy ngoài thread Qt: {sorted(set(starts))}"
