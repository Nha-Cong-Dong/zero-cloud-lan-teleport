"""Khung GUI chạy bằng `teleport.ui.app.main` trong tiến trình: khay, cửa sổ, M2, màn “Bắt đầu”, thoát
(spec-p4-3 mục 4, D6, D8, D9, mục 6.5, 6.6 và mục 10).

Mỗi test chạy `main([...])` qua fixture `gui` (tests/ui/conftest.py): kịch bản chạy trong event loop Qt,
node bị ép nghe 127.0.0.1 cổng 0, không mDNS. Test có node chạy thật mở socket nên gắn `integration`;
test chỉ có màn “Bắt đầu” (không node) thì không.
"""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("PySide6")

integration = pytest.mark.integration


def tray_actions(tray):
    return [a for a in tray.actions() if not a.isSeparator()]


def host_started(run) -> bool:
    return bool(run.hosts) and run.hosts[-1].running


def quit_via_tray(run, plain):
    [quit_action] = [a for a in tray_actions(run.tray) if plain(a.text()) == "Thoát"]
    quit_action.trigger()


def exit_budget() -> float:
    """Tiêu chí thoát của mục 10: ≤ `close_time_limit() + 10` s."""
    from teleport.app import node as app_node

    return app_node.close_time_limit() + 10


# --- Khay (mục 6.5) ------------------------------------------------------------------------------------


def test_tray_has_four_items_in_order_with_enabled_state(gui, gui_wait, config_dir, ui_helpers):
    d = config_dir("A", identity=False)  # màn “Bắt đầu”: không node, không mạng
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.trays and run.windows, 10, "có Tray và MainWindow")
        actions = tray_actions(run.tray)
        seen["texts"] = [ui_helpers.plain(a.text()) for a in actions]
        seen["enabled"] = [a.isEnabled() for a in actions]
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    assert seen["texts"] == list(ui_helpers.M5)
    assert seen["enabled"] == [True, False, False, True], "Mở/Thoát bật; Gửi file…/Tạm dừng nhận tắt"
    assert run.code == 0


def test_tray_actions_emit_open_and_quit_signals(gui, gui_wait, config_dir, ui_helpers):
    d = config_dir("A", identity=False)
    got: list[str] = []

    def script(run):
        yield gui_wait(lambda: run.trays, 10, "có Tray")
        tray = run.tray
        tray.open_requested.connect(lambda: got.append("open"))
        tray.quit_requested.connect(lambda: got.append("quit"))
        actions = tray_actions(tray)
        actions[0].trigger()
        yield gui_wait(lambda: "open" in got, 2, "open_requested")
        actions[3].trigger()

    run = gui(["--config-dir", str(d)], script)
    assert got == ["open", "quit"]
    assert run.code == 0


@integration
def test_tray_open_shows_and_activates_hidden_window(gui, gui_wait, config_dir):
    d = config_dir("A")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.windows and host_started(run), 15, "node chạy")
        win = run.window
        assert win.isVisible()
        win.close()
        yield gui_wait(lambda: not win.isVisible(), 2, "cửa sổ ẩn khi đóng")
        before = run.activations
        tray_actions(run.tray)[0].trigger()
        yield gui_wait(lambda: win.isVisible(), 2, "Mở Teleport hiện lại cửa sổ")
        seen["activated"] = run.activations > before or win.isActiveWindow()
        seen["minimized"] = win.isMinimized()
        run.tray.quit_requested.emit()

    run = gui(["--config-dir", str(d)], script)
    assert seen["activated"], "Mở Teleport phải kích hoạt cửa sổ (activateWindow)"
    assert not seen["minimized"]
    assert run.code == 0


@integration
def test_tray_quit_stops_node_and_main_returns_0_in_time(gui, gui_wait, config_dir, ui_helpers):
    d = config_dir("A")

    def script(run):
        yield gui_wait(lambda: host_started(run), 15, "node chạy")
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    assert run.code == 0
    assert run.stop_calls >= 1, "Thoát phải gọi NodeHost.stop()"
    assert run.quit_at is not None and run.returned_at - run.quit_at <= exit_budget()
    assert not run.host.running
    assert ui_helpers.lock_is_free(d, "node.lock")
    assert ui_helpers.lock_is_free(d, "gui.lock"), "SingleInstance.close() phải nhả gui.lock khi thoát"


# --- Cửa sổ (mục 6.6) ---------------------------------------------------------------------------------


@integration
def test_close_with_tray_hides_window_node_keeps_running_and_m4_once(gui, gui_wait, config_dir, ui_helpers):
    d = config_dir("A")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.windows and host_started(run), 15, "node chạy")
        win = run.window
        win.close()
        yield gui_wait(lambda: not win.isVisible(), 2, "cửa sổ ẩn")
        yield gui_wait(lambda: run.messages, 2, "bóng thoại M4")
        seen["after_first"] = list(run.messages)
        seen["running"] = run.host.running
        seen["stop_calls"] = run.stop_calls
        win.show()
        yield
        win.close()
        yield gui_wait(lambda: not win.isVisible(), 2, "cửa sổ ẩn lần hai")
        for _ in range(10):
            yield
        seen["after_second"] = list(run.messages)
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    assert seen["running"], "đóng cửa sổ khi có khay: node vẫn chạy (vẫn nhận file)"
    assert seen["stop_calls"] == 0
    assert any(ui_helpers.M4 in body or ui_helpers.M4 in title for title, body in seen["after_first"])
    assert len(seen["after_second"]) == len(seen["after_first"]), "M4 chỉ hiện lần đầu trong phiên"
    assert run.code == 0


@integration
def test_close_without_tray_quits(gui, gui_wait, config_dir, ui_helpers):
    d = config_dir("A")

    def script(run):
        yield gui_wait(lambda: run.windows and host_started(run), 15, "node chạy")
        run.window.close()

    run = gui(["--config-dir", str(d)], script, tray_available=False)
    assert run.code == 0
    assert run.stop_calls >= 1
    assert run.returned_at - run.quit_at <= exit_budget()
    assert ui_helpers.lock_is_free(d, "node.lock")


@integration
def test_background_with_tray_starts_hidden(gui, gui_wait, config_dir, ui_helpers):
    d = config_dir("A")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.windows and host_started(run), 15, "node chạy")
        for _ in range(5):
            yield
        seen["visible"] = run.window.isVisible()
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d), "--background"], script)
    assert seen["visible"] is False
    assert run.code == 0


@integration
def test_background_without_tray_still_shows_window(gui, gui_wait, config_dir):
    d = config_dir("A")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.windows and host_started(run), 15, "node chạy")
        yield gui_wait(lambda: run.window.isVisible(), 3, "cửa sổ hiện dù --background vì không có khay")
        seen["ok"] = True
        run.window.close()

    run = gui(["--config-dir", str(d), "--background"], script, tray_available=False)
    assert seen.get("ok")
    assert run.code == 0


@integration
def test_running_window_shows_device_name_and_receive_dir(gui, gui_wait, config_dir, tmp_path, ui_helpers):
    from teleport.app.node import default_device_name
    from teleport.core.display import display_name
    from teleport.trust.identity import load_or_create_identity

    d = config_dir("A")
    name = display_name(default_device_name(load_or_create_identity(d)))
    receive_dir = tmp_path / "nhan-gui"
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.windows and host_started(run), 15, "node chạy")
        yield gui_wait(lambda: name in ui_helpers.all_text(run.window), 3, "tên máy hiện trên cửa sổ")
        seen["text"] = ui_helpers.all_text(run.window)
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    text = seen["text"]
    shown = {str(receive_dir), os.path.normpath(receive_dir), receive_dir.as_posix()}
    assert any(s in text for s in shown), f"thư mục nhận {receive_dir} không hiện: {text!r}"
    assert run.code == 0


@integration
def test_locked_shows_m2_even_in_background_then_retry_runs(gui, gui_wait, config_dir, ui_helpers, fast_lock):
    """Mục 4: CLI giữ `node.lock` → `start_failed("locked")` → M2 + Thử lại, hiện cửa sổ kể cả
    `--background`; nhả khóa rồi bấm Thử lại → `NodeHost` mới chạy."""
    from teleport.lock import NodeLock

    d = config_dir("A")
    holder = NodeLock(d)
    holder.acquire()
    seen: dict = {}

    def script(run):
        yield gui_wait(
            lambda: run.windows and ui_helpers.M2_HEAD in ui_helpers.all_text(run.window), 15, "câu M2"
        )
        seen["text"] = ui_helpers.all_text(run.window)
        yield gui_wait(lambda: run.window.isVisible(), 3, "cửa sổ hiện dù --background")
        holder.release()
        [retry] = ui_helpers.buttons_with_text(run.window, ui_helpers.RETRY)
        retry.click()
        yield gui_wait(lambda: len(run.hosts) >= 2 and host_started(run), 15, "node chạy sau Thử lại")
        seen["m2_gone"] = ui_helpers.M2_HEAD not in ui_helpers.all_text(run.window)
        quit_via_tray(run, ui_helpers.plain)

    try:
        run = gui(["--config-dir", str(d), "--background"], script)
    finally:
        holder.release()
    assert ui_helpers.M2_HINT in seen["text"]
    assert "`" not in seen["text"], "câu GUI không dùng backtick quanh tên lệnh"
    assert seen["m2_gone"], "chạy rồi thì không còn câu M2"
    assert len(run.hosts) == 2, "Thử lại dựng một NodeHost mới"
    assert run.code == 0


def test_start_button_is_keyboard_reachable_and_named(gui, gui_wait, config_dir, ui_helpers):
    """N12 (một phần): nút “Bắt đầu” có tên cho trình đọc màn hình và đi tới được bằng Tab. Màn “Bắt đầu”
    (đủ câu chữ, bấm thì chạy node, `show_requested`): tests/ui/test_start_screen.py."""
    from PySide6.QtCore import Qt

    d = config_dir("A", identity=False)
    seen: dict = {}

    def script(run):
        yield gui_wait(
            lambda: run.windows and ui_helpers.buttons_with_text(run.window, ui_helpers.START), 10, "nút"
        )
        [start] = ui_helpers.buttons_with_text(run.window, ui_helpers.START)
        seen["name"] = start.accessibleName() or ui_helpers.plain(start.text())
        seen["tab"] = bool(start.focusPolicy() & Qt.FocusPolicy.TabFocus)
        seen["win_name"] = run.window.accessibleName() or run.window.windowTitle()
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script)
    assert seen["name"]
    assert seen["tab"]
    assert seen["win_name"]


# --- Đo thời gian thoát khi close treo, qua main ----------------------------------------------------------


@integration
def test_quit_with_hanging_close_returns_within_budget(gui, gui_wait, config_dir, monkeypatch, ui_helpers):
    """D8: `close()` treo → vẫn thoát trong `close_time_limit() + 10` s (mục 10), `main` trả 0, không
    `os._exit`."""
    import asyncio

    from teleport.app import node as app_node

    monkeypatch.setattr(app_node, "close_time_limit", lambda: 0.5)
    real_close = app_node.Node.close

    async def hanging_close(self):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.wait_for(real_close(self), 15)
            raise

    monkeypatch.setattr(app_node.Node, "close", hanging_close)
    d = config_dir("A")

    def script(run):
        yield gui_wait(lambda: host_started(run), 15, "node chạy")
        quit_via_tray(run, ui_helpers.plain)

    t0 = time.monotonic()
    run = gui(["--config-dir", str(d)], script)
    assert run.code == 0
    assert run.returned_at - run.quit_at <= 0.5 + 10
    assert time.monotonic() - t0 < 60


# --- Mở lần nữa trong lúc đang thoát (soat-code vòng 1, (b) và (c)) ----------------------------------------


@pytest.mark.slow
@integration
def test_acquire_during_quit_is_not_swallowed(gui, gui_wait, config_dir):
    """Ngay sau “Thoát” (`request_quit`), server IPC đã đóng mà `gui.lock` còn giữ: bản mở sau không được
    trả None (tưởng đã `show` vào app đang tắt) mà phải hết hạn."""
    from teleport.ui.app import SingleInstance

    d = config_dir("A")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: host_started(run), 15, "node chạy")
        run.tray.quit_requested.emit()
        try:
            seen["second"] = SingleInstance.acquire(d, wait=1.0)
        except TimeoutError:
            seen["second"] = "timeout"

    run = gui(["--config-dir", str(d)], script)
    assert seen["second"] == "timeout", f"bản hai lúc bản một đang thoát: {seen['second']!r}"
    assert run.code == 0


@pytest.mark.slow
def test_main_returns_1_with_message_when_holder_does_not_answer(gui, config_dir, ui_helpers):
    """`gui.lock` bị giữ mà không có server trả lời (bản kia đang tắt hay treo) → `main` báo “Teleport đang
    tắt hoặc chưa phản hồi.” và trả 1 (không phải 0 như khi đã gửi được `show`), không dựng cửa sổ."""
    from teleport.lock import NodeLock

    d = config_dir("A", identity=False)
    holder = NodeLock(d, "gui.lock")
    holder.acquire()

    def script(run):
        return
        yield

    try:
        run = gui(["--config-dir", str(d)], script, require_script_done=False)
    finally:
        holder.release()
    assert run.code == 1
    assert run.windows == [] and run.hosts == []
    assert any(ui_helpers.QUITTING in t for t in run.exec_texts), run.exec_texts
