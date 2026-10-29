"""Màn “Bắt đầu” thay M3 (spec-hackathon mục 10 A1, PR1 tiêu chí 6).

Chưa có `device.crt`: cửa sổ hiện câu F1 (tên hiện với mọi máy trong mạng) và tên sẽ dùng (hostname),
một nút “Bắt đầu”. Chưa bấm thì không chạy node, không lấy `node.lock`, không tạo danh tính, không
quảng bá. Bấm thì node chạy (tự tạo `device.crt`, tên = hostname).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

integration = pytest.mark.integration

START_BUTTON = "Bắt đầu"
_MVP = Path(__file__).resolve().parents[2] / "docs" / "mvp.md"


def f1_name_notice() -> str:
    """Câu đầu của dòng cảnh báo tên trong mvp F1 (“Tên này hiện với mọi máy …”), đọc từ tài liệu."""
    text = _MVP.read_text(encoding="utf-8")
    start = text.index("### F1.")
    f1 = text[start : text.index("\n### ", start + 1)]
    m = re.search(r"“(Tên này hiện[^”]*?\.)", f1)
    assert m, "mvp F1: không thấy câu “Tên này hiện …”"
    return m.group(1)


def expected_name(tmp_path: Path) -> str | None:
    """Tên mặc định (hostname) như `Node.start` sẽ dùng; None nếu hostname không dùng được (khi đó tên
    rơi về `Teleport-<mã>` mà mã chưa có trước khi bấm)."""
    from teleport.app.node import default_device_name
    from teleport.core.display import display_name
    from teleport.trust.identity import load_or_create_identity

    ident = load_or_create_identity(tmp_path / "id-tam")
    name = default_device_name(ident)
    if name == f"Teleport-{ident.device_id[:4]}":
        return None
    return display_name(name)


def host_started(run) -> bool:
    return bool(run.hosts) and run.hosts[-1].running


def quit_via_tray(run, plain):
    [quit_action] = [a for a in run.tray.actions() if not a.isSeparator() and plain(a.text()) == "Thoát"]
    quit_action.trigger()


def test_start_screen_shows_f1_notice_and_name_without_starting_node(
    gui, gui_wait, config_dir, ui_helpers, tmp_path
):
    d = config_dir("A", identity=False)
    name = expected_name(tmp_path)
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.windows and run.window.isVisible(), 10, "cửa sổ hiện")
        yield gui_wait(
            lambda: ui_helpers.buttons_with_text(run.window, START_BUTTON), 5, f"nút “{START_BUTTON}”"
        )
        for _ in range(10):
            yield
        seen["text"] = ui_helpers.all_text(run.window)
        seen["buttons"] = len(ui_helpers.buttons_with_text(run.window, START_BUTTON))
        seen["node_lock_free"] = ui_helpers.lock_is_free(d, "node.lock")
        seen["hosts"] = len(run.hosts)
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    assert ui_helpers.strip_markup(f1_name_notice()) in seen["text"], seen["text"]
    if name is not None:
        assert name in seen["text"], f"tên sẽ dùng {name!r} không hiện: {seen['text']!r}"
    assert seen["buttons"] == 1
    assert "Máy này chưa được thiết lập Teleport." not in seen["text"], "M3 đã được thay bằng màn “Bắt đầu”"
    assert run.start_calls == 0 and seen["hosts"] == 0, "chưa bấm “Bắt đầu” thì không chạy node"
    assert seen["node_lock_free"]
    assert not (d / "device.crt").exists(), "chưa bấm “Bắt đầu” thì không tạo danh tính"
    assert run.code == 0


def test_start_screen_show_request_does_not_start_node(gui, gui_wait, config_dir, ui_helpers):
    """Mở Teleport lần nữa (`show`) khi đang ở màn “Bắt đầu”: chỉ hiện cửa sổ, vẫn chưa chạy node."""
    from teleport.ui.app import SingleInstance

    d = config_dir("A", identity=False)
    seen: dict = {}

    def script(run):
        yield gui_wait(
            lambda: run.windows and ui_helpers.buttons_with_text(run.window, START_BUTTON), 10, "màn Bắt đầu"
        )
        seen["second"] = SingleInstance.acquire(d, wait=2.0)
        for _ in range(15):
            yield
        seen["hosts"] = len(run.hosts)
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    assert seen["second"] is None
    assert seen["hosts"] == 0 and run.start_calls == 0
    assert not (d / "device.crt").exists()


@integration
def test_pressing_start_runs_node_and_creates_identity(gui, gui_wait, config_dir, ui_helpers, tmp_path):
    from teleport.app.node import default_device_name
    from teleport.trust.identity import load_or_create_identity

    d = config_dir("A", identity=False)
    seen: dict = {}

    def script(run):
        yield gui_wait(
            lambda: run.windows and ui_helpers.buttons_with_text(run.window, START_BUTTON), 10, "màn Bắt đầu"
        )
        [button] = ui_helpers.buttons_with_text(run.window, START_BUTTON)
        button.click()
        yield gui_wait(lambda: host_started(run), 20, "node chạy sau khi bấm Bắt đầu")
        seen["crt"] = (d / "device.crt").is_file()
        seen["node_name"] = run.host.node_name
        seen["start_left"] = len(ui_helpers.buttons_with_text(run.window, START_BUTTON))
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    assert seen["crt"], "bấm Bắt đầu → node tự tạo device.crt"
    assert run.start_calls == 1
    assert seen["start_left"] == 0, "node chạy rồi thì không còn nút Bắt đầu"
    assert seen["node_name"] == default_device_name(load_or_create_identity(d)), "tên = hostname (D6)"
    assert run.code == 0


@integration
def test_start_screen_skipped_when_identity_exists(gui, gui_wait, config_dir, ui_helpers):
    """Đã có `device.crt` thì không hiện màn “Bắt đầu” (node khởi động thẳng như trước)."""
    d = config_dir("A")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.hosts, 10, "NodeHost được dựng")
        seen["buttons"] = len(ui_helpers.buttons_with_text(run.window, START_BUTTON))
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    assert seen["buttons"] == 0
    assert run.start_calls == 1
