"""Nhớ trạng thái ở `GuiApp` (`specs/spec-f1.md` D8, D9, R1, R2, R15, R19; tiêu chí xong 5–7).

Viết từ spec, không từ code. Hai cách dựng:

- `GuiApp(qapp, cfg, saved=Settings(...))` dựng thẳng (fixture `make_app`), không node thật: một `FakeHost`
  gán vào `app.host` ghi lại mọi `call(fn, on_done)` và `stop()` theo thứ tự. Lần lưu được nhận ra bằng
  cách chạy `fn` trên một node giả ghi lại `set_ui_preferences(**kw)`. Test tự trả kết quả về `on_done`
  (None = thành công theo R2, hay một Exception = lỗi ghi).
- `main()` trong tiến trình (fixture `gui`), node thật trên 127.0.0.1 → `integration`: Thoát lưu xong
  trước khi node đóng, mở lại thấy đúng hình học / cuộc trò chuyện, câu khay một lần qua hai lần mở app.

Giả định API (theo spec): `GuiApp(..., saved: Settings | None = None)`; `GuiApp._on_commit_data(manager)`
(đã có); `placement.restore` được gọi qua thuộc tính module (`placement.restore(...)`) để vá được;
`ui_app._saved_settings(config_dir)`.
"""

from __future__ import annotations

import contextlib
import json
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QRect, Qt, Signal
from PySide6.QtGui import QGuiApplication

from teleport.app.settings import Settings, WindowGeometry

integration = pytest.mark.integration

PEER_ID = "a71c04d2" + "e" * 24
OTHER_ID = "0f0f0f0f" + "1" * 24


# =================================================================================================
# Đồ giả
# =================================================================================================


class ProbeNode:
    """Node giả: ghi lại `set_ui_preferences(**kw)`; trả `result` hoặc ném `error`."""

    def __init__(self, result=None, error: BaseException | None = None) -> None:
        self.saves: list[dict] = []
        self.result = result
        self.error = error

    def set_ui_preferences(self, **kw):
        self.saves.append(kw)
        if self.error is not None:
            raise self.error
        return self.result


class FakeHost(QObject):
    """`NodeHost` tối thiểu cho `GuiApp`: `running`, `is_stopped`, `call`, `stop`, `join`, `stopped`."""

    stopped = Signal()

    def __init__(self, window=None, *, running: bool = True) -> None:
        super().__init__()
        self.running = running
        self.is_stopped = False
        self.calls: list[tuple] = []  # (fn, on_done)
        self.events: list[str] = []  # "call" / "stop", theo thứ tự
        self.window_visible_at_call: list[bool] = []
        self._window = window
        self.node_name = "May-A"

    def call(self, fn, on_done=None) -> None:
        self.calls.append((fn, on_done))
        self.events.append("call")
        if self._window is not None:
            self.window_visible_at_call.append(self._window.isVisible())

    def submit(self, fn, on_done=None):
        return SimpleNamespace(cancel=lambda: None, done=False)

    def stop(self) -> None:
        self.events.append("stop")
        self.running = False
        self.is_stopped = True  # không phát `stopped`: test không muốn `QApplication.quit()`

    def join(self, timeout: float) -> bool:
        return True

    # ---- tiện ích của test -------------------------------------------------------------------

    def saves(self) -> list[dict]:
        """kwargs của mọi lần gọi `set_ui_preferences` mà app đã gửi (chạy `fn` trên node thăm dò)."""
        out: list[dict] = []
        for fn, _ in self.calls:
            probe = ProbeNode()
            with contextlib.suppress(Exception):
                fn(probe)
            out.extend(probe.saves)
        return out

    def finish(self, result=None, error: BaseException | None = None) -> None:
        """Chạy các lời gọi đang chờ trên node giả rồi trả kết quả về `on_done` (như thread Qt)."""
        pending, self.calls_done = self.calls, getattr(self, "calls_done", [])
        self.calls = []
        for fn, on_done in pending:
            node = ProbeNode(result, error)
            try:
                value = fn(node)
            except Exception as e:
                value = e
            self.calls_done.append((fn, on_done))
            if on_done is not None:
                on_done(value)


@pytest.fixture
def fake_tray_icon(monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon

    from teleport.ui import tray

    shown: dict[int, bool] = {}
    messages: list[tuple[str, str]] = []
    monkeypatch.setattr(tray, "is_available", lambda: True)
    monkeypatch.setattr(QSystemTrayIcon, "show", lambda self: shown.__setitem__(id(self), True))
    monkeypatch.setattr(QSystemTrayIcon, "hide", lambda self: shown.__setitem__(id(self), False))
    monkeypatch.setattr(QSystemTrayIcon, "setVisible", lambda self, v: shown.__setitem__(id(self), bool(v)))
    monkeypatch.setattr(QSystemTrayIcon, "isVisible", lambda self: shown.get(id(self), False))
    monkeypatch.setattr(
        QSystemTrayIcon, "showMessage", lambda self, *a, **k: messages.append((str(a[0]), str(a[1])))
    )
    return SimpleNamespace(messages=messages)


@pytest.fixture
def no_exec(monkeypatch):
    from PySide6.QtWidgets import QDialog, QMessageBox

    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a, **k: 0)
    monkeypatch.setattr(QDialog, "exec", lambda self, *a, **k: 0)


@pytest.fixture
def make_app(qapp, config_dir, fake_tray_icon, no_exec):
    """`make_app(saved=None, *, identity=False, background=False) -> GuiApp` (không gọi `start()`)."""
    from teleport.ui.app import GuiApp

    made: list = []

    def make(saved: Settings | None = None, *, identity: bool = False, background: bool = False, label="A"):
        kw = {} if saved is None else {"saved": saved}
        controller = GuiApp(qapp, config_dir(label, identity=identity), background=background, **kw)
        controller.tray.show()
        made.append(controller)
        return controller

    yield make

    for controller in made:
        with contextlib.suppress(Exception):
            controller.shutdown()
        with contextlib.suppress(Exception):
            controller.window.hide()
            controller.window.deleteLater()
    qapp.processEvents()


def m4_count(fake_tray_icon, ui_helpers) -> int:
    return sum(1 for _t, body in fake_tray_icon.messages if body == ui_helpers.M4)


def show(qtbot, w) -> None:
    w.show()
    qtbot.waitUntil(w.isVisible, timeout=2000)
    qtbot.wait(10)


def hide_to_tray(qtbot, w) -> None:
    w.close()  # có khay: ẩn + `hidden_to_tray`
    qtbot.waitUntil(lambda: not w.isVisible(), timeout=2000)


def rect_inside() -> QRect:
    g = QGuiApplication.primaryScreen().availableGeometry()
    if g.width() < 780 or g.height() < 600:
        pytest.skip("màn hình (offscreen) quá nhỏ")
    return QRect(g.x() + 30, g.y() + 60, 740, 500)


def dview(device_id: str, name: str):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id,
        name=name,
        os="windows",
        paired=True,
        online=True,
        connected=False,
        addresses=("192.0.2.7",),
        port=47474,
    )


# =================================================================================================
# D8 / R1: `GuiApp(saved=...)` mặc định, áp hình học ở đầu `start()` (R15)
# =================================================================================================


def test_gui_app_without_saved_still_constructs(make_app):
    app = make_app()
    assert app.window is not None


def test_start_restores_geometry_before_showing(make_app, qtbot):
    r = rect_inside()
    app = make_app(Settings(window=WindowGeometry(r.x(), r.y(), r.width(), r.height())))
    app.start()  # chưa có danh tính: màn lần chạy đầu, cửa sổ hiện
    qtbot.waitUntil(app.window.isVisible, timeout=2000)
    assert app.window.geometry() == r


def test_start_restores_maximized(make_app, qtbot):
    r = rect_inside()
    app = make_app(Settings(window=WindowGeometry(r.x(), r.y(), r.width(), r.height(), maximized=True)))
    app.start()
    qtbot.waitUntil(app.window.isVisible, timeout=2000)
    assert app.window.windowState() & Qt.WindowState.WindowMaximized
    assert app.window.normalGeometry() == r


def test_r15_restore_is_first_in_start_even_in_background(make_app, monkeypatch):
    """R15 + D9: `placement.restore` chạy ở dòng đầu `start()`, trước `tray.show()` và `start_node()`,
    cả khi `--background` (cửa sổ vẫn ẩn nhưng đã có hình học đã lưu)."""
    from teleport.ui import placement

    r = rect_inside()
    saved = WindowGeometry(r.x(), r.y(), r.width(), r.height())
    app = make_app(Settings(window=saved), identity=True, background=True)
    order: list[str] = []
    real_restore = placement.restore

    def restore(window, value):
        order.append("restore")
        assert value == saved
        real_restore(window, value)

    monkeypatch.setattr(placement, "restore", restore)
    real_tray_show = app.tray.show
    monkeypatch.setattr(app.tray, "show", lambda: (order.append("tray.show"), real_tray_show())[1])

    def fake_start_node():
        order.append("start_node")
        app.window.show_starting()  # như node đang khởi động: `--background` giữ cửa sổ ẩn

    monkeypatch.setattr(app, "start_node", fake_start_node)
    app.start()
    assert order[0] == "restore", order
    assert order.index("restore") < order.index("tray.show") < order.index("start_node")
    assert not app.window.isVisible(), "--background: cửa sổ vẫn ẩn"
    assert app.window.geometry() == r


def test_start_with_no_saved_window_keeps_default_geometry(make_app, qtbot):
    a = make_app(label="A")
    default = a.window.geometry().size()
    a.start()
    qtbot.waitUntil(a.window.isVisible, timeout=2000)
    assert a.window.geometry().size() == default


# =================================================================================================
# D9: bóng thoại khay một lần + lưu khi ẩn
# =================================================================================================


def test_first_hide_shows_hint_once_and_saves_hint_and_window(make_app, qtbot, fake_tray_icon, ui_helpers):
    app = make_app()
    host = FakeHost(app.window)
    app.host = host
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    assert m4_count(fake_tray_icon, ui_helpers) == 1
    [save] = host.saves()
    assert save.get("tray_hint_shown") is True
    assert isinstance(save.get("window"), WindowGeometry)
    g = app.window.geometry()
    w = save["window"]
    assert (w.x, w.y, w.width, w.height, w.maximized) == (g.x(), g.y(), g.width(), g.height(), False)
    assert "last_conversation" not in save, "chưa chọn máy nào: không gửi"
    host.finish(None)  # R2: None là thành công

    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    qtbot.wait(20)
    assert m4_count(fake_tray_icon, ui_helpers) == 1, "câu khay chỉ một lần"
    assert host.calls == [], "ẩn lần hai không có gì đổi: không gọi lưu"


def test_hint_saved_with_false_result_counts_as_success(make_app, qtbot):
    """R2: `on_done` coi mọi kết quả không phải BaseException (kể cả False) là thành công."""
    app = make_app()
    host = FakeHost(app.window)
    app.host = host
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    host.finish(False)
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    qtbot.wait(20)
    assert host.calls == []


def test_window_moved_then_hidden_again_saves_only_window(make_app, qtbot):
    app = make_app()
    host = FakeHost(app.window)
    app.host = host
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    host.finish(None)
    r = rect_inside()
    app.window.setGeometry(r.adjusted(5, 5, 5, 5))
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    [save] = host.saves()
    assert set(save) == {"window"}, "chỉ gửi trường khác mốc"
    g = app.window.geometry()
    assert (save["window"].x, save["window"].y) == (g.x(), g.y())


def test_saved_tray_hint_shown_means_no_bubble_and_not_sent(make_app, qtbot, fake_tray_icon, ui_helpers):
    app = make_app(Settings(tray_hint_shown=True))
    host = FakeHost(app.window)
    app.host = host
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    qtbot.wait(20)
    assert m4_count(fake_tray_icon, ui_helpers) == 0
    assert fake_tray_icon.messages == [], "không bóng thoại nào (kể cả câu tạm dừng)"
    for save in host.saves():
        assert "tray_hint_shown" not in save, "mốc đã True: không gửi lại"


def test_saved_values_equal_to_current_are_not_sent(make_app, qtbot):
    """Mốc = giá trị đọc lúc đầu: cửa sổ ở đúng hình đã lưu, hint đã báo → ẩn không gọi lưu."""
    from teleport.ui.placement import capture

    probe = make_app(label="P")
    probe.window.setGeometry(rect_inside())  # trong vùng khả dụng (đã trừ thanh tiêu đề, R9)
    show(qtbot, probe.window)
    current = capture(probe.window)
    assert current is not None
    probe.window.hide()

    app = make_app(Settings(window=current, tray_hint_shown=True), label="B")
    host = FakeHost(app.window)
    app.host = host
    app.start()
    qtbot.waitUntil(app.window.isVisible, timeout=2000)
    assert capture(app.window) == current, "restore → show → capture không trôi (R13)"
    hide_to_tray(qtbot, app.window)
    qtbot.wait(20)
    assert host.saves() == []


# =================================================================================================
# D9: lỗi ghi chỉ WARNING rồi thử lại; node chưa chạy thì không gọi
# =================================================================================================


def test_save_error_only_warns_keeps_baseline_and_retries(
    make_app, qtbot, caplog, fake_tray_icon, ui_helpers
):
    from teleport.app.settings import SettingsSaveError

    caplog.set_level(logging.WARNING)
    app = make_app()
    host = FakeHost(app.window)
    app.host = host
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    [first] = host.saves()
    host.finish(error=SettingsSaveError("khong ghi duoc"))
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warns, "lỗi ghi phải có WARNING"
    assert any("SettingsSaveError" in r.getMessage() for r in warns), [r.getMessage() for r in warns]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    [second] = host.saves()
    assert second == first, "giữ mốc: lần sau gửi lại đúng các trường đó"
    assert m4_count(fake_tray_icon, ui_helpers) == 1, "thử lại lưu nhưng không báo lại câu khay"
    host.finish(None)
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    assert host.calls == []


def test_save_os_error_result_also_only_warns(make_app, qtbot, caplog):
    caplog.set_level(logging.WARNING)
    app = make_app()
    host = FakeHost(app.window)
    app.host = host
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    host.finish(error=OSError(28, "day"))
    assert any(r.levelno == logging.WARNING and "OSError" in r.getMessage() for r in caplog.records)


def test_no_host_no_call_then_saved_when_node_runs(make_app, qtbot, fake_tray_icon, ui_helpers):
    app = make_app()
    assert app.host is None
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)  # không lỗi
    assert m4_count(fake_tray_icon, ui_helpers) == 1, "bóng thoại vẫn hiện dù node chưa chạy"
    host = FakeHost(app.window)
    app.host = host
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    [save] = host.saves()
    assert save.get("tray_hint_shown") is True, "đã báo mà chưa lưu được: lần lưu sau mang theo"
    assert m4_count(fake_tray_icon, ui_helpers) == 1


def test_host_not_running_no_call(make_app, qtbot):
    app = make_app()
    host = FakeHost(app.window, running=False)
    app.host = host
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    app._on_commit_data(None)
    assert host.calls == []


# =================================================================================================
# D9: Thoát, commitDataRequest, shutdown
# =================================================================================================


def test_quit_saves_before_window_hidden_and_before_host_stop(make_app, qtbot):
    app = make_app(Settings(tray_hint_shown=True))
    host = FakeHost(app.window)
    app.host = host
    show(qtbot, app.window)
    app.request_quit(confirm=False)
    assert host.events[:2] == ["call", "stop"], host.events
    assert host.window_visible_at_call == [True], "lưu trước `window.hide()`"
    [save] = host.saves()
    assert isinstance(save.get("window"), WindowGeometry)


def test_quit_on_done_after_quitting_still_updates_baseline(make_app, qtbot, caplog):
    """D9: không bỏ `on_done` khi `_quitting`: kết quả về sau Thoát vẫn được xử lý (không lỗi)."""
    caplog.set_level(logging.WARNING)
    app = make_app()
    host = FakeHost(app.window)
    app.host = host
    show(qtbot, app.window)
    app.request_quit(confirm=False)
    host.finish(error=OSError(5, "x"))
    assert any(r.levelno == logging.WARNING and "OSError" in r.getMessage() for r in caplog.records)


def test_commit_data_saves(make_app, qtbot):
    app = make_app()
    host = FakeHost(app.window)
    app.host = host
    show(qtbot, app.window)
    app._on_commit_data(None)
    [save] = host.saves()
    assert isinstance(save.get("window"), WindowGeometry)
    assert "tray_hint_shown" not in save, "chưa báo câu khay: không gửi"


def test_commit_data_twice_without_change_after_success_sends_once(make_app, qtbot):
    app = make_app()
    host = FakeHost(app.window)
    app.host = host
    show(qtbot, app.window)
    app._on_commit_data(None)
    host.finish(None)
    app._on_commit_data(None)
    assert host.calls == []


def test_shutdown_saves_before_host_stop(make_app, qtbot):
    app = make_app()
    host = FakeHost(app.window)
    app.host = host
    show(qtbot, app.window)
    app.shutdown()
    assert "call" in host.events and "stop" in host.events
    assert host.events.index("call") < host.events.index("stop"), host.events
    assert host.saves()


def test_window_never_shown_nothing_to_save(make_app):
    """`capture` trả None khi cửa sổ chưa từng hiện (`--background`): không gửi `window`."""
    app = make_app()
    host = FakeHost(app.window)
    app.host = host
    app._on_commit_data(None)
    for save in host.saves():
        assert "window" not in save
    assert host.saves() == []


# =================================================================================================
# D9: cuộc trò chuyện gần nhất
# =================================================================================================


def test_selected_conversation_saved_on_hide(make_app, qtbot):
    app = make_app(Settings(tray_hint_shown=True))
    host = FakeHost(app.window)
    app.host = host
    app.window.devices.reset([dview(PEER_ID, "Zeta"), dview(OTHER_ID, "Alpha")])
    assert app.window.conversations.select_device(OTHER_ID)
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    [save] = host.saves()
    assert save.get("last_conversation") == OTHER_ID
    host.finish(None)

    # Danh sách rỗng (current → None) không xóa lựa chọn đã nhớ: không có gì để lưu lại.
    app.window.devices.clear()
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    assert host.saves() == []


def test_saved_last_conversation_not_resent_when_unchanged(make_app, qtbot):
    app = make_app(Settings(last_conversation=OTHER_ID, tray_hint_shown=True))
    host = FakeHost(app.window)
    app.host = host
    app.window.devices.reset([dview(PEER_ID, "Zeta"), dview(OTHER_ID, "Alpha")])
    app.window.conversations.select_device(OTHER_ID)
    show(qtbot, app.window)
    hide_to_tray(qtbot, app.window)
    for save in host.saves():
        assert "last_conversation" not in save


# =================================================================================================
# R1: `_saved_settings`
# =================================================================================================


def test_saved_settings_returns_new_fields(tmp_path):
    from teleport.ui import app as ui_app

    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "window": {"x": 1, "y": 2, "width": 800, "height": 500, "maximized": True},
                "last_conversation": PEER_ID,
                "tray_hint_shown": True,
            }
        ),
        encoding="utf-8",
    )
    s = ui_app._saved_settings(tmp_path)
    assert s.window == WindowGeometry(1, 2, 800, 500, maximized=True)
    assert s.last_conversation == PEER_ID
    assert s.tray_hint_shown is True


def test_saved_settings_bad_window_keeps_other_fields(tmp_path):
    from teleport.ui import app as ui_app

    (tmp_path / "settings.json").write_text(
        json.dumps({"window": [1, 2], "theme": "dark", "tray_hint_shown": True}), encoding="utf-8"
    )
    s = ui_app._saved_settings(tmp_path)
    assert (s.window, s.theme, s.tray_hint_shown) == (None, "dark", True)


def test_run_passes_saved_settings_to_gui_app(monkeypatch, tmp_path):
    """D8: `_run` đọc file một lần rồi đưa `Settings` đó cho `GuiApp(saved=...)`."""
    import argparse

    from teleport.ui import app as ui_app

    saved = Settings(tray_hint_shown=True, theme="dark")
    calls: list = []

    def fake_saved(config_dir):
        calls.append(config_dir)
        return saved

    seen: dict = {}

    class Stop(Exception):
        pass

    def fake_gui_app(*args, **kwargs):
        seen["saved"] = kwargs.get("saved")
        raise Stop

    class FakeInstance:
        def close(self):
            pass

        def stop_listening(self):
            pass

    monkeypatch.setattr(ui_app, "_saved_settings", fake_saved)
    monkeypatch.setattr(ui_app, "GuiApp", fake_gui_app)
    monkeypatch.setattr(
        ui_app.SingleInstance, "acquire", classmethod(lambda cls, d, wait=3.0: FakeInstance())
    )
    monkeypatch.setattr(ui_app, "_attach_file_log", lambda d: None)
    monkeypatch.setattr(ui_app, "_register_app_user_model_id", lambda d: None)
    monkeypatch.setattr(ui_app.i18n, "install", lambda app, language: None)
    monkeypatch.setattr(ui_app.theme, "set_preference", lambda *a, **k: None)
    from PySide6.QtWidgets import QApplication

    args = argparse.Namespace(background=False, config_dir=str(tmp_path), selftest=False)
    with pytest.raises(Stop):
        ui_app._run(QApplication.instance(), args, tmp_path)
    assert len(calls) == 1, "đọc settings.json đúng một lần"
    assert seen["saved"] is saved


# =================================================================================================
# Node thật: Thoát lưu trước khi node dừng; mở lại khôi phục (tiêu chí 5–7)
# =================================================================================================


def controller_of(run):
    app = run.tray.parent()
    assert app is not None and hasattr(app, "request_quit")
    return app


def host_running(run) -> bool:
    from teleport.ui.main_window import ViewState

    return bool(run.windows and run.hosts and run.host.running and run.window.view_state is ViewState.RUNNING)


def load(d):
    return Settings.load(d)


@integration
def test_quit_saves_geometry_then_restart_restores_it(gui, gui_wait, config_dir):
    d = config_dir("A")
    r = rect_inside()
    seen: dict = {}

    def first(run):
        yield gui_wait(lambda: host_running(run) and run.window.isVisible(), 20, "node chạy, cửa sổ hiện")
        run.window.setGeometry(r)
        for _ in range(3):
            yield
        seen["geometry"] = run.window.geometry()
        controller_of(run).request_quit(confirm=False)

    run = gui(["--config-dir", str(d)], first)
    assert run.code == 0
    saved = load(d).window
    g = seen["geometry"]
    assert saved is not None, "Thoát phải lưu `window` xong trước khi node đóng"
    assert (saved.x, saved.y, saved.width, saved.height, saved.maximized) == (
        g.x(),
        g.y(),
        g.width(),
        g.height(),
        False,
    )

    def second(run):
        yield gui_wait(lambda: run.windows and run.window.isVisible(), 20, "cửa sổ hiện lần hai")
        seen["restored"] = run.window.geometry()
        yield gui_wait(lambda: host_running(run), 20, "node chạy lần hai")
        controller_of(run).request_quit(confirm=False)

    run = gui(["--config-dir", str(d)], second)
    assert run.code == 0
    assert seen["restored"] == g


@integration
def test_quit_saves_maximized_then_restart_restores_maximized(gui, gui_wait, config_dir):
    d = config_dir("A")
    r = rect_inside()
    seen: dict = {}

    def first(run):
        yield gui_wait(lambda: host_running(run) and run.window.isVisible(), 20, "node chạy")
        run.window.setGeometry(r)
        yield
        run.window.setWindowState(Qt.WindowState.WindowMaximized)
        yield gui_wait(lambda: bool(run.window.windowState() & Qt.WindowState.WindowMaximized), 5, "phóng to")
        seen["normal"] = run.window.normalGeometry()
        controller_of(run).request_quit(confirm=False)

    gui(["--config-dir", str(d)], first)
    saved = load(d).window
    assert saved is not None and saved.maximized is True
    n = seen["normal"]
    assert (saved.x, saved.y, saved.width, saved.height) == (n.x(), n.y(), n.width(), n.height())

    def second(run):
        yield gui_wait(lambda: run.windows and run.window.isVisible(), 20, "cửa sổ hiện")
        seen["state"] = run.window.windowState()
        seen["normal2"] = run.window.normalGeometry()
        yield gui_wait(lambda: host_running(run), 20, "node chạy")
        controller_of(run).request_quit(confirm=False)

    gui(["--config-dir", str(d)], second)
    assert seen["state"] & Qt.WindowState.WindowMaximized
    assert seen["normal2"] == n


@integration
def test_background_start_applies_saved_geometry_while_hidden(gui, gui_wait, config_dir):
    d = config_dir("A")
    r = rect_inside()
    (d / "settings.json").write_text(
        json.dumps(
            {"version": 1, "window": {"x": r.x(), "y": r.y(), "width": r.width(), "height": r.height()}}
        ),
        encoding="utf-8",
    )
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: host_running(run), 20, "node chạy (nền)")
        seen["visible"] = run.window.isVisible()
        seen["geometry"] = run.window.geometry()
        controller_of(run).request_quit(confirm=False)

    run = gui(["--config-dir", str(d), "--background"], script)
    assert run.code == 0
    assert seen["visible"] is False
    assert seen["geometry"] == r
    # Cửa sổ chưa từng hiện → không có gì để lưu đè: `window` đã lưu còn nguyên.
    saved = load(d).window
    assert saved is not None and (saved.x, saved.y, saved.width, saved.height) == (
        r.x(),
        r.y(),
        r.width(),
        r.height(),
    )


@integration
def test_tray_hint_only_once_across_two_launches(gui, gui_wait, config_dir, ui_helpers):
    d = config_dir("A")
    seen: dict = {}

    def first(run):
        yield gui_wait(lambda: host_running(run) and run.window.isVisible(), 20, "node chạy")
        run.window.close()
        yield gui_wait(lambda: run.messages, 3, "bóng thoại khay")
        yield gui_wait(lambda: load(d).tray_hint_shown, 5, "tray_hint_shown được lưu khi ẩn")
        seen["first"] = list(run.messages)
        controller_of(run).request_quit(confirm=False)

    gui(["--config-dir", str(d)], first)
    assert any(body == ui_helpers.M4 for _t, body in seen["first"])
    assert json.loads((d / "settings.json").read_text(encoding="utf-8"))["tray_hint_shown"] is True

    def second(run):
        yield gui_wait(lambda: host_running(run) and run.window.isVisible(), 20, "node chạy lần hai")
        run.window.close()
        yield gui_wait(lambda: not run.window.isVisible(), 3, "cửa sổ ẩn")
        for _ in range(15):
            yield
        seen["second"] = list(run.messages)
        controller_of(run).request_quit(confirm=False)

    gui(["--config-dir", str(d)], second)
    assert not any(body == ui_helpers.M4 for _t, body in seen["second"]), "đã báo ở lần mở trước"


@integration
def test_last_conversation_saved_on_quit_and_restored(gui, gui_wait, config_dir, trust_each_other, tmp_path):
    d = config_dir("A")
    trust_each_other(d, tmp_path / "B", name_b="May-B")
    trust_each_other(d, tmp_path / "C", name_b="May-C")
    seen: dict = {}

    def rows(run) -> list[str]:
        return [v.device_id for v in run.window.conversations.model.devices()]

    def first(run):
        yield gui_wait(lambda: host_running(run) and len(rows(run)) == 2, 20, "node chạy, hai máy")
        order = rows(run)
        seen["order"] = order
        target = order[1]  # không phải hàng 0: phân biệt với lựa chọn mặc định
        seen["target"] = target
        assert run.window.conversations.select_device(target)
        yield
        controller_of(run).request_quit(confirm=False)

    gui(["--config-dir", str(d)], first)
    assert load(d).last_conversation == seen["target"]

    def second(run):
        yield gui_wait(lambda: host_running(run) and len(rows(run)) == 2, 20, "node chạy lần hai")
        yield gui_wait(
            lambda: (
                (cur := run.window.conversations.current_device()) is not None
                and cur.device_id == seen["target"]
            ),
            5,
            "cuộc trò chuyện gần nhất được chọn lại",
        )
        seen["page"] = run.window.current_page
        controller_of(run).request_quit(confirm=False)

    gui(["--config-dir", str(d)], second)
    from teleport.ui.main_window import Page

    assert seen["page"] is Page.HOME, "khôi phục chỉ chọn máy, không đổi trang"


@integration
def test_last_conversation_of_unpaired_device_ignored(gui, gui_wait, config_dir, trust_each_other, tmp_path):
    """Máy đã lưu không còn trong danh sách → hàng 0 giữ nguyên, không lỗi."""
    d = config_dir("A")
    trust_each_other(d, tmp_path / "B", name_b="May-B")
    (d / "settings.json").write_text(
        json.dumps({"version": 1, "last_conversation": "9" * 32}), encoding="utf-8"
    )
    seen: dict = {}

    def script(run):
        yield gui_wait(
            lambda: host_running(run) and len(run.window.conversations.model.devices()) == 1, 20, "node chạy"
        )
        for _ in range(5):
            yield
        cur = run.window.conversations.current_device()
        seen["current"] = None if cur is None else cur.device_id
        seen["row0"] = run.window.conversations.model.devices()[0].device_id
        controller_of(run).request_quit(confirm=False)

    run = gui(["--config-dir", str(d)], script)
    assert run.code == 0
    assert seen["current"] in (None, seen["row0"])
