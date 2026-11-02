"""Nối dây clipboard ở `GuiApp` và `NodeHost` (spec D5 mục 3.2, 9.1 điểm 1; sửa sau soat-code P5-2).

Không node, không socket: `GuiApp` dựng thẳng, host giả gắn vào `GuiApp.host`; ca `clipboard_factory`
dùng `NodeHost` thật mà `start` không mở thread.

Phủ:
- `_on_clipboard_event`: RECEIVED / WRITE_FAILED → `Tray.flash_clipboard` + `ChatView.show_clipboard`;
  TOO_LARGE chỉ khay; SENT không hiện gì; sự kiện của host cũ, lúc đang thoát, hay không phải
  `ClipboardEvent` bị bỏ.
- “Thử lại”: host không chạy → bật lại nút; host chạy → `submit`; kết quả `NOTHING_TO_RETRY` → câu “không
  còn”, kết quả là exception vẫn bật lại nút; kết quả của host cũ bị bỏ.
- `clipboard_factory`: backend vào `NodeOptions.clipboard`, `QtClipboardBackend` bị dọn khi host phát
  `stopped`.
- `NodeHost._clipboard_callback` gọi từ thread khác → `clipboard_event` phát trên thread Qt.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

PEER_ID = "a71c04d2" + "e" * 24


def event(outcome: str, device_id: str | None = PEER_ID, **kw):
    from teleport.app.clipboard import ClipboardEvent, ClipOutcome
    from teleport.core.messages import ClipKind

    values = {"kind": ClipKind.TEXT, "size": 5, "preview": "xin chào"}
    values.update(kw)
    return ClipboardEvent(ClipOutcome(outcome), device_id, **values)


class FakeHost:
    """Host tối thiểu: ghi lại `submit`."""

    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.is_stopped = False
        self.submits: list = []

    def submit(self, fn, on_done=None):
        self.submits.append((fn, on_done))
        return SimpleNamespace(cancel=lambda: None, done=False)

    def stop(self) -> None:
        self.is_stopped = True

    def join(self, timeout: float) -> bool:
        return True


@pytest.fixture
def gapp(qapp, qtbot, config_dir, monkeypatch):
    """`GuiApp` không node; khay “đang chạy”; `flash_clipboard`, `show_clipboard`,
    `finish_clipboard_retry` được ghi lại (vẫn gọi hàm thật)."""
    from teleport.ui.app import GuiApp

    controller = GuiApp(qapp, config_dir("A", identity=False))
    controller.tray.set_running(True)
    rec = SimpleNamespace(flash=[], show=[], finish=[])
    tray, view = controller.tray, controller.transfers.view
    real_flash, real_show, real_finish = (
        tray.flash_clipboard,
        view.show_clipboard,
        view.finish_clipboard_retry,
    )

    def flash(notice):
        rec.flash.append(notice)
        real_flash(notice)

    def show(device_id, **kw):
        rec.show.append((device_id, kw))
        return real_show(device_id, **kw)

    def finish(device_id, *, available):
        rec.finish.append((device_id, available))
        real_finish(device_id, available=available)

    monkeypatch.setattr(tray, "flash_clipboard", flash)
    monkeypatch.setattr(view, "show_clipboard", show)
    monkeypatch.setattr(view, "finish_clipboard_retry", finish)
    host = FakeHost()
    controller.host = host
    yield SimpleNamespace(app=controller, host=host, rec=rec, view=view, tray=tray)
    controller.host = None
    controller._quitting = False
    with contextlib.suppress(Exception):
        controller.shutdown()
    with contextlib.suppress(Exception):
        controller.window.hide()
        controller.window.deleteLater()
    qapp.processEvents()


# ---- Sự kiện ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("outcome", "ok"), [("received", True), ("write_failed", False)])
def test_received_or_write_failed_flashes_tray_and_shows_note(gapp, outcome, ok):
    from teleport.ui import texts
    from teleport.ui.tray import ClipNoticeKind

    gapp.app._on_clipboard_event(gapp.host, event(outcome))
    [notice] = gapp.rec.flash
    assert notice.kind is (ClipNoticeKind.RECEIVED if ok else ClipNoticeKind.WRITE_FAILED)
    assert notice.label == texts.peer_label(None, PEER_ID)
    [(device_id, kw)] = gapp.rec.show
    assert device_id == PEER_ID
    assert kw["ok"] is ok and kw["preview"] == "xin chào" and kw["size"] == 5
    assert gapp.view.clipboard_note(PEER_ID) is not None
    assert gapp.tray.state.clipboard == notice


def test_too_large_only_flashes_tray(gapp):
    from teleport.ui.tray import ClipNoticeKind

    gapp.app._on_clipboard_event(gapp.host, event("too_large", None))
    [notice] = gapp.rec.flash
    assert notice.kind is ClipNoticeKind.TOO_LARGE
    assert gapp.rec.show == []


def test_sent_shows_nothing(gapp):
    gapp.app._on_clipboard_event(gapp.host, event("sent"))
    assert gapp.rec.flash == [] and gapp.rec.show == []


def test_event_from_old_host_is_dropped(gapp):
    gapp.app._on_clipboard_event(FakeHost(), event("received"))
    assert gapp.rec.flash == [] and gapp.rec.show == []


def test_event_while_quitting_is_dropped(gapp):
    gapp.app._quitting = True
    gapp.app._on_clipboard_event(gapp.host, event("received"))
    assert gapp.rec.flash == [] and gapp.rec.show == []


def test_non_event_object_is_dropped(gapp):
    gapp.app._on_clipboard_event(gapp.host, object())
    assert gapp.rec.flash == [] and gapp.rec.show == []


# ---- “Thử lại” ------------------------------------------------------------------------------------


def failed_note(gapp):
    gapp.app._on_clipboard_event(gapp.host, event("write_failed"))
    note = gapp.view.clipboard_note(PEER_ID)
    assert note is not None and note.retry_button.isEnabled()
    return note


def test_retry_without_running_host_reenables_button(gapp):
    note = failed_note(gapp)
    gapp.host.running = False
    note.retry_button.click()
    assert gapp.rec.finish == [(PEER_ID, True)]
    assert gapp.host.submits == []
    assert note.retry_button.isEnabled() and not note.unavailable


def test_retry_without_host_reenables_button(gapp):
    note = failed_note(gapp)
    gapp.app.host = None
    note.retry_button.click()
    assert gapp.rec.finish == [(PEER_ID, True)]
    assert note.retry_button.isEnabled()


def test_retry_submits_job_reading_clipboard_on_node_thread(gapp):
    from teleport.app.clipboard import ClipOutcome

    note = failed_note(gapp)
    note.retry_button.click()
    assert not note.retry_button.isEnabled(), "đang chờ kết quả: nút tắt"
    [(fn, on_done)] = gapp.host.submits
    assert asyncio.run(fn(SimpleNamespace(clipboard=None))) is ClipOutcome.NOTHING_TO_RETRY
    assert on_done is not None


def test_retry_nothing_to_retry_marks_note_unavailable(gapp):
    from teleport.app.clipboard import ClipOutcome

    note = failed_note(gapp)
    note.retry_button.click()
    [(_fn, on_done)] = gapp.host.submits
    on_done(ClipOutcome.NOTHING_TO_RETRY)
    assert gapp.rec.finish == [(PEER_ID, False)]
    assert note.unavailable


def test_retry_exception_result_reenables_button(gapp):
    note = failed_note(gapp)
    note.retry_button.click()
    [(_fn, on_done)] = gapp.host.submits
    on_done(RuntimeError("node đang đóng"))
    assert gapp.rec.finish == [(PEER_ID, True)]
    assert note.retry_button.isEnabled() and not note.unavailable


def test_retry_result_of_old_host_is_dropped(gapp):
    from teleport.app.clipboard import ClipOutcome

    note = failed_note(gapp)
    note.retry_button.click()
    [(_fn, on_done)] = gapp.host.submits
    gapp.app.host = FakeHost()
    on_done(ClipOutcome.NOTHING_TO_RETRY)
    assert gapp.rec.finish == []
    assert not note.unavailable


# ---- clipboard_factory, dọn backend --------------------------------------------------------------


@pytest.fixture
def factory_app(qapp, qtbot, config_dir, node_options, monkeypatch):
    """`GuiApp` có `clipboard_factory`; `NodeHost.start` không mở thread (host ở trạng thái chưa chạy)."""
    from teleport.ui import bridge
    from teleport.ui.app import GuiApp

    monkeypatch.setattr(bridge.NodeHost, "start", lambda self: None)
    made: list = []

    def make(kind):
        def factory(parent):
            from teleport.ui.clipboard_qt import QtClipboardBackend

            backend = QtClipboardBackend(parent) if kind == "qt" else SimpleNamespace(kind="fake")
            made.append((parent, backend))
            return backend

        cfg = config_dir("A", identity=True)
        controller = GuiApp(qapp, cfg, make_options=lambda c: node_options(c), clipboard_factory=factory)
        apps.append(controller)
        return controller

    apps: list = []
    yield SimpleNamespace(make=make, made=made)
    for controller in apps:
        with contextlib.suppress(Exception):
            controller.shutdown()
        with contextlib.suppress(Exception):
            controller.window.hide()
            controller.window.deleteLater()
    qapp.processEvents()


def test_clipboard_factory_backend_goes_into_node_options(factory_app):
    app = factory_app.make("fake")
    app.start_node()
    [(parent, backend)] = factory_app.made
    assert parent is app
    assert app.host is not None and app.host.options.clipboard is backend


def test_qt_backend_disposed_when_its_host_stops(factory_app, qtbot):
    import shiboken6

    app = factory_app.make("qt")
    app.start_node()
    [(_parent, backend)] = factory_app.made
    host = app.host
    assert host.options.clipboard is backend
    with qtbot.waitSignal(backend.destroyed, timeout=3000):
        host.stop()
    assert not shiboken6.isValid(backend)


def test_make_options_without_factory_has_no_clipboard(qapp, config_dir, node_options, monkeypatch):
    from teleport.ui import bridge
    from teleport.ui.app import GuiApp

    monkeypatch.setattr(bridge.NodeHost, "start", lambda self: None)
    app = GuiApp(qapp, config_dir("A", identity=True), make_options=lambda c: node_options(c))
    try:
        app.start_node()
        assert app.host is not None and app.host.options.clipboard is None
    finally:
        with contextlib.suppress(Exception):
            app.shutdown()
        app.window.deleteLater()
        qapp.processEvents()


# ---- NodeHost.clipboard_event -------------------------------------------------------------------


def test_node_host_clipboard_event_emitted_on_qt_thread(qtbot, tmp_path):
    from teleport.ui.bridge import NodeHost, NodeOptions

    host = NodeHost(NodeOptions(config_dir=tmp_path / "cfg", receive_dir=tmp_path / "nhan", mdns=None))
    got: list[tuple[object, int]] = []
    host.clipboard_event.connect(lambda e: got.append((e, threading.get_ident())))
    ev = event("received")
    worker = threading.Thread(target=host._clipboard_callback, args=(ev,))
    worker.start()
    worker.join(5)
    qtbot.waitUntil(lambda: bool(got), timeout=3000)
    assert got == [(ev, threading.main_thread().ident)]
    host.deleteLater()
