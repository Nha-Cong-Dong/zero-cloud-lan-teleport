"""Màn lần chạy đầu trong app thật (mvp F1; spec D1 Q1–Q8, mục 6, 7; mục 14 thắng).

Viết từ spec, không từ code. `teleport.ui.app.main` chạy trong tiến trình (harness `gui`). Chưa có
`device.crt`: cửa sổ hiện form lần chạy đầu (tên thiết bị, thư mục nhận, khởi động cùng Windows).
Chưa bấm “Bắt đầu” thì không chạy node, không lấy `node.lock`, không tạo danh tính, không quảng bá.

- Mọi test chốt trạng thái hỗ trợ tự khởi động (spec 14.2): `autostart_on` vá `autostart._frozen` →
  True; `autostart_off` vá `autostart._executable` → `tmp_path/"python.exe"` (không có `pythonw.exe`
  cạnh nó) và `_frozen` → False. Registry luôn là `fake_registry` (`tests/conftest.py`).
- Tên gợi ý chốt bằng seam `teleport.app.node._hostname` (14.12).
- Test kiểm `receive_dir` trong `settings.json` hay lỗi RECEIVE_DIR gọi `gui(..., keep_receive_dir=True)`
  (14.1). Hộp chọn thư mục đi qua seam `teleport.ui.receive_dir.dialog_factory`.
- Đường vào `GuiApp`: cha của khay (`run.tray.parent()`); form: `run.window.first_run`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

integration = pytest.mark.integration

START_BUTTON = "Bắt đầu"
HOSTNAME = "Laptop-Minh"
PLACEHOLDER = "Ví dụ: Laptop-Minh"
UNAVAILABLE_TEXT = "Không dùng được thư mục {path}. Hãy chọn thư mục khác mà bạn có quyền ghi."
RESERVED_TEXT = (
    "Không dùng được thư mục {path} để nhận file vì Teleport dùng nó cho việc riêng. Hãy chọn thư mục khác."
)
AUTOSTART_FAILED = (
    "Chưa bật được khởi động cùng Windows vì Windows không cho ghi cài đặt khởi động. "
    "Bạn có thể bật lại trong Cài đặt."
)
OPEN_SETTINGS = "Mở Cài đặt"
_MVP = Path(__file__).resolve().parents[2] / "docs" / "mvp.md"


def f1_notice() -> str:
    """Câu dưới ô tên thiết bị, nguyên văn mvp F1, đọc từ tài liệu."""
    text = _MVP.read_text(encoding="utf-8")
    start = text.index("### F1.")
    f1 = text[start : text.index("\n### ", start + 1)]
    m = re.search(r"Ngay dưới ô tên thiết bị có dòng:\s*“([^”]+)”", f1)
    assert m, "mvp F1: không thấy dòng “Ngay dưới ô tên thiết bị có dòng: “…””"
    return m.group(1)


def shown(path) -> str:
    from teleport.ui.send import printable

    return printable(str(path))


def same(a, b) -> bool:
    if a is None or b is None:
        return False
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def settings_json(config: Path) -> dict:
    path = config / "settings.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def first_run_marked(config: Path) -> bool:
    try:
        return settings_json(config).get("first_run_done") is True
    except (OSError, ValueError):
        return False  # đang ghi dở (ghi nguyên tử, nhưng phòng hờ)


def host_started(run) -> bool:
    return bool(run.hosts) and run.hosts[-1].running


def quit_via_tray(run, plain):
    [quit_action] = [a for a in run.tray.actions() if not a.isSeparator() and plain(a.text()) == "Thoát"]
    quit_action.trigger()


def app_of(run):
    app = run.tray.parent()
    assert app is not None and hasattr(app, "receive_dir_controller"), "cha của khay phải là GuiApp"
    return app


def welcome(run) -> bool:
    from teleport.ui.main_window import ViewState

    return bool(run.windows) and run.window.view_state is ViewState.WELCOME


def widgets(run):
    """(ô tên, nút Đổi…, nhãn lỗi thư mục, hàng tự khởi động, công tắc, nút Bắt đầu) của form."""
    from PySide6.QtWidgets import QAbstractButton, QLabel, QLineEdit, QWidget

    w = run.window

    def get(cls, name):
        found = w.findChild(cls, name)
        assert found is not None, f"không có {name} (spec D1 mục 4)"
        return found

    return (
        get(QLineEdit, "firstRunNameEdit"),
        get(QAbstractButton, "firstRunDirButton"),
        get(QLabel, "firstRunDirError"),
        get(QWidget, "firstRunAutostartRow"),
        get(QAbstractButton, "firstRunAutostartSwitch"),
        get(QAbstractButton, "startButton"),
    )


def send_text(widget, text: str) -> None:
    """Gõ từng ký tự bằng `QKeyEvent` mang chữ (phát `textEdited` như bàn phím thật).

    Không dùng `QTest.keyClicks`: với ký tự ngoài ASCII (“à”, “Đ”) PySide6 6.12 làm tiến trình chết
    (0xC0000409), đã thử.
    """
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    for ch in text:
        for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.sendEvent(
                widget, QKeyEvent(kind, Qt.Key.Key_unknown, Qt.KeyboardModifier.NoModifier, ch)
            )


def type_name(edit, text: str) -> None:
    """Gõ như người dùng (`textEdited`)."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    edit.setFocus()
    edit.selectAll()
    QTest.keyClick(edit, Qt.Key.Key_Backspace)
    send_text(edit, text)


def select(dialog, path: Path) -> None:
    """Như hộp thật khi bấm chọn: `fileSelected` rồi `finished`."""
    dialog.fileSelected.emit(str(path))
    dialog.finished.emit(1)


class Dialogs:
    """Seam `receive_dir.dialog_factory`: `QFileDialog` con, `open()` không hiện gì."""

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


def pick_dir(run, dialogs, path: Path) -> None:
    _, dir_button, *_ = widgets(run)
    before = len(dialogs.made)
    dir_button.click()
    assert len(dialogs.made) == before + 1, "bấm [Đổi…] ở lần chạy đầu phải mở một hộp chọn thư mục"
    assert dialogs.made[-1].opened == 1
    select(dialogs.made[-1], path)


# =================================================================================================
# Fixture cục bộ
# =================================================================================================


@pytest.fixture
def dialogs(monkeypatch):
    from teleport.ui import receive_dir as mod

    d = Dialogs()
    monkeypatch.setattr(mod, "dialog_factory", d.factory)
    return d


@pytest.fixture
def hostname(monkeypatch):
    from teleport.app import node as app_node

    monkeypatch.setattr(app_node, "_hostname", lambda: HOSTNAME)
    return HOSTNAME


@pytest.fixture
def autostart_on(monkeypatch, fake_registry):
    """Tự khởi động được hỗ trợ trên mọi hệ điều hành (spec 7, 14.2)."""
    from teleport.app import autostart

    monkeypatch.setattr(autostart, "_frozen", lambda: True)
    assert autostart.supported()
    return fake_registry


@pytest.fixture
def autostart_off(monkeypatch, tmp_path, fake_registry):
    """Không hỗ trợ: bản mã nguồn không có `pythonw.exe` cạnh `sys.executable` (spec 14.2)."""
    from teleport.app import autostart

    monkeypatch.setattr(autostart, "_frozen", lambda: False)
    monkeypatch.setattr(autostart, "_executable", lambda: str(tmp_path / "khong-co" / "python.exe"))
    assert not autostart.supported()
    return fake_registry


def run_key(fake_registry):
    from teleport.app import autostart

    return fake_registry.values.get((autostart.RUN_KEY, autostart.VALUE_NAME))


# =================================================================================================
# Màn lần chạy đầu: không node trước khi bấm
# =================================================================================================


def test_start_screen_shows_f1_notice_and_name_without_starting_node(
    gui, gui_wait, config_dir, ui_helpers, hostname, autostart_on
):
    from PySide6.QtWidgets import QLabel

    d = config_dir("A", identity=False)
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.windows and run.window.isVisible(), 10, "cửa sổ hiện")
        yield gui_wait(lambda: welcome(run), 5, "màn lần chạy đầu (WELCOME)")
        for _ in range(10):
            yield
        edit, _dir_button, _dir_error, row, switch, _start = widgets(run)
        seen["name"] = edit.text()
        seen["hint"] = run.window.findChild(QLabel, "firstRunNameHint").text()
        seen["text"] = ui_helpers.all_text(run.window)
        seen["buttons"] = len(ui_helpers.buttons_with_text(run.window, START_BUTTON))
        seen["row"] = row.isVisibleTo(run.window)
        seen["switch"] = switch.isChecked()
        seen["node_lock_free"] = ui_helpers.lock_is_free(d, "node.lock")
        seen["hosts"] = len(run.hosts)
        seen["writes"] = list(autostart_on.writes())
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    assert seen["name"] == HOSTNAME, "ô tên điền sẵn tên gợi ý (hostname)"
    assert seen["hint"] == f1_notice(), "câu F1 nguyên văn ngay dưới ô tên"
    assert seen["buttons"] == 1
    assert "Máy này chưa được thiết lập Teleport." not in seen["text"], "M3 đã được thay"
    assert seen["row"] and seen["switch"] is True, "OFF → công tắc bật sẵn (Q7)"
    assert run.start_calls == 0 and seen["hosts"] == 0, "chưa bấm “Bắt đầu” thì không chạy node"
    assert seen["node_lock_free"]
    assert not (d / "device.crt").exists(), "chưa bấm “Bắt đầu” thì không tạo danh tính"
    assert not (d / "settings.json").exists(), "chưa bấm thì không ghi gì"
    assert seen["writes"] == [], "chưa bấm thì không ghi registry"
    assert run.code == 0


def test_start_screen_show_request_does_not_start_node(gui, gui_wait, config_dir, ui_helpers, autostart_off):
    """Mở Teleport lần nữa (`show`) khi đang ở màn lần chạy đầu: chỉ hiện cửa sổ, vẫn chưa chạy node."""
    from teleport.ui.app import SingleInstance

    d = config_dir("A", identity=False)
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "màn lần chạy đầu")
        seen["second"] = SingleInstance.acquire(d, wait=2.0)
        for _ in range(15):
            yield
        seen["hosts"] = len(run.hosts)
        seen["still_welcome"] = welcome(run)
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    assert seen["second"] is None
    assert seen["hosts"] == 0 and run.start_calls == 0
    assert seen["still_welcome"]
    assert not (d / "device.crt").exists()


def test_background_without_identity_shows_first_run_window(
    gui, gui_wait, config_dir, ui_helpers, autostart_on
):
    """Q4: `--background` mà chưa có danh tính → vẫn hiện cửa sổ với form và hiện khay; không node."""
    d = config_dir("A", identity=False)
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.windows and run.window.isVisible(), 10, "cửa sổ hiện dù --background")
        yield gui_wait(lambda: welcome(run), 5, "WELCOME")
        for _ in range(10):
            yield
        seen["visible"] = run.window.isVisible()
        seen["tray"] = run.tray.visible
        seen["hosts"] = len(run.hosts)
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d), "--background"], script)
    assert seen["visible"]
    assert seen["tray"], "khay vẫn hiện"
    assert seen["hosts"] == 0 and run.start_calls == 0
    assert not (d / "device.crt").exists()
    assert autostart_on.writes() == []


def test_first_run_hostname_fallback_leaves_name_empty(
    gui, gui_wait, config_dir, ui_helpers, monkeypatch, autostart_off
):
    """14.12: hostname rỗng → gợi ý rỗng, placeholder; không bao giờ hiện `Teleport-????`."""
    from teleport.app import node as app_node

    monkeypatch.setattr(app_node, "_hostname", lambda: "")
    d = config_dir("A", identity=False)
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        edit, *_rest, start = widgets(run)
        seen["name"] = edit.text()
        seen["placeholder"] = edit.placeholderText()
        seen["text"] = ui_helpers.all_text(run.window)
        start.click()  # tên rỗng: ở lại WELCOME
        for _ in range(5):
            yield
        seen["after"] = welcome(run)
        seen["hosts"] = len(run.hosts)
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script)
    assert seen["name"] == ""
    assert seen["placeholder"] == PLACEHOLDER
    assert "Teleport-" not in seen["text"]
    assert seen["after"] and seen["hosts"] == 0, "tên rỗng: không chạy node"


def test_first_run_autostart_row_hidden_when_unsupported(
    gui, gui_wait, config_dir, ui_helpers, autostart_off
):
    d = config_dir("A", identity=False)
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        _edit, _dir, _err, row, switch, _start = widgets(run)
        seen["row"] = row.isVisibleTo(run.window)
        seen["switch"] = switch.isVisibleTo(run.window)
        seen["choice"] = run.window.first_run.validate()
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script)
    assert seen["row"] is False and seen["switch"] is False
    assert seen["choice"] is not None and seen["choice"].autostart is None
    assert autostart_off.writes() == []


@pytest.mark.parametrize(
    ("preset", "checked", "hint_state"),
    [
        ("off", True, None),
        ("on", True, None),
        ("other", False, "other"),
        ("blocked", False, "blocked"),
        ("read_error", True, None),
    ],
)
def test_first_run_autostart_initial_state_follows_registry(
    gui, gui_wait, config_dir, ui_helpers, autostart_on, preset, checked, hint_state
):
    """Bảng Q7: OFF/ON/đọc lỗi → bật sẵn, không câu thêm; OTHER/BLOCKED → tắt sẵn kèm câu trạng thái."""
    from PySide6.QtWidgets import QLabel

    from teleport.app import autostart
    from teleport.ui.settings import autostart_state_hint

    d = config_dir("A", identity=False)
    reg = autostart_on
    ours = autostart.launch_command(d)
    if preset == "on":
        reg.values[(autostart.RUN_KEY, autostart.VALUE_NAME)] = (ours, reg.REG_SZ)
    elif preset == "other":
        reg.values[(autostart.RUN_KEY, autostart.VALUE_NAME)] = (
            r'"C:\Khac\Teleport.exe" --background',
            reg.REG_SZ,
        )
    elif preset == "blocked":
        reg.values[(autostart.RUN_KEY, autostart.VALUE_NAME)] = (ours, reg.REG_SZ)
        reg.values[(autostart.STARTUP_APPROVED_KEY, autostart.VALUE_NAME)] = (
            b"\x03" + b"\0" * 11,
            reg.REG_BINARY,
        )
    elif preset == "read_error":
        reg.errors["get"] = OSError("giả lỗi đọc")
    if preset != "read_error":
        expected = autostart.AutostartState(preset)
        assert autostart.state(d) is expected, "dựng sẵn registry sai"
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        _edit, _dir, _err, row, switch, _start = widgets(run)
        state_label = run.window.findChild(QLabel, "firstRunAutostartState")
        seen["row"] = row.isVisibleTo(run.window)
        seen["checked"] = switch.isChecked()
        seen["state_visible"] = state_label is not None and state_label.isVisibleTo(run.window)
        seen["state_text"] = state_label.text() if state_label is not None else None
        reg.errors.pop("get", None)
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script)
    assert seen["row"]
    assert seen["checked"] is checked
    if hint_state is None:
        assert not seen["state_visible"], f"{preset}: không hiện câu trạng thái (kể cả câu đọc lỗi)"
    else:
        assert seen["state_visible"]
        assert seen["state_text"] == autostart_state_hint(autostart.AutostartState(hint_state))
    assert reg.writes() == [], "dựng màn không bao giờ ghi registry"


def test_first_run_change_dir_reserved_shows_error_under_row(
    gui, gui_wait, config_dir, ui_helpers, dialogs, autostart_off, fake_default_receive_dir
):
    d = config_dir("A", identity=False)
    reserved = d / "con"
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        seen["before"] = run.window.first_run.receive_dir
        pick_dir(run, dialogs, reserved)
        _edit, _dir_button, dir_error, *_ = widgets(run)
        yield gui_wait(lambda: dir_error.isVisibleTo(run.window), 5, "lỗi dưới hàng thư mục")
        seen["error"] = dir_error.text()
        seen["after"] = run.window.first_run.receive_dir
        seen["notice"] = app_of(run).transfers.view.notice_text()
        seen["hosts"] = len(run.hosts)
        seen["welcome"] = welcome(run)
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script)
    assert seen["error"] == RESERVED_TEXT.format(path=shown(reserved))
    assert seen["after"] == seen["before"], "thư mục dành riêng không được nhận vào form"
    assert not seen["notice"], "ở WELCOME không dùng thanh thông báo của luồng chat"
    assert seen["hosts"] == 0 and seen["welcome"]
    assert not (d / "device.crt").exists()


def test_first_run_chosen_dir_updates_form_without_node(
    gui, gui_wait, config_dir, ui_helpers, dialogs, autostart_off, tmp_path
):
    d = config_dir("A", identity=False)
    chosen = tmp_path / "chon-truoc"
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        pick_dir(run, dialogs, chosen)
        yield gui_wait(lambda: run.window.first_run.receive_dir == chosen, 5, "form nhận thư mục vừa chọn")
        seen["choice"] = run.window.first_run.validate()
        seen["hosts"] = len(run.hosts)
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script)
    assert seen["choice"].receive_dir == chosen and seen["choice"].receive_dir_chosen is True
    assert seen["hosts"] == 0
    assert not chosen.exists(), "chọn thư mục không tạo gì trên đĩa"
    assert not (d / "settings.json").exists()


# =================================================================================================
# Bấm “Bắt đầu”: node thật
# =================================================================================================


@integration
def test_pressing_start_runs_node_and_creates_identity(gui, gui_wait, config_dir, ui_helpers, autostart_off):
    from teleport.app.node import default_device_name
    from teleport.trust.identity import load_or_create_identity

    d = config_dir("A", identity=False)
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "màn lần chạy đầu")
        [button] = ui_helpers.buttons_with_text(run.window, START_BUTTON)
        button.click()
        yield gui_wait(lambda: host_started(run), 20, "node chạy sau khi bấm Bắt đầu")
        seen["crt"] = (d / "device.crt").is_file()
        seen["node_name"] = run.host.node_name
        seen["start_left"] = len(ui_helpers.buttons_with_text(run.window, START_BUTTON))
        yield gui_wait(lambda: first_run_marked(d), 10, "first_run_done được ghi")
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    assert seen["crt"], "bấm Bắt đầu → node tự tạo device.crt"
    assert run.start_calls == 1
    assert seen["start_left"] == 0, "node chạy rồi thì không còn nút Bắt đầu"
    expected = default_device_name(load_or_create_identity(d))
    assert seen["node_name"] == expected, "tên = hostname (không sửa ô)"
    assert run.code == 0


@integration
def test_start_screen_skipped_when_identity_exists(gui, gui_wait, config_dir, ui_helpers, autostart_on):
    """Q1: đã có `device.crt` thì không hiện màn lần chạy đầu (node khởi động thẳng như trước)."""
    d = config_dir("A")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.hosts, 10, "NodeHost được dựng")
        seen["buttons"] = len(ui_helpers.buttons_with_text(run.window, START_BUTTON))
        seen["welcome"] = welcome(run)
        yield gui_wait(lambda: host_started(run), 20, "node chạy")
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    assert seen["buttons"] == 0 and not seen["welcome"]
    assert run.start_calls == 1
    assert autostart_on.writes() == [], "không qua lần chạy đầu: không đụng registry"


@integration
def test_existing_identity_without_flag_skips_and_marks_done(
    gui, gui_wait, config_dir, ui_helpers, autostart_off
):
    """Q1, Q2: máy đã có danh tính (CLI, bản trước), `settings.json` không có `first_run_done` → không
    hiện WELCOME; sau `started` file có `"first_run_done": true`."""
    d = config_dir("A")
    assert "first_run_done" not in settings_json(d)
    states: set = set()

    def script(run):
        while not host_started(run):
            if run.windows:
                states.add(run.window.view_state)
            yield
        states.add(run.window.view_state)
        yield gui_wait(lambda: first_run_marked(d), 10, "first_run_done được ghi sau started")
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    from teleport.ui.main_window import ViewState

    assert ViewState.WELCOME not in states, states
    assert settings_json(d).get("first_run_done") is True
    assert run.start_calls == 1


@integration
def test_first_run_saves_edited_name_and_chosen_dir(
    gui, gui_wait, config_dir, ui_helpers, dialogs, hostname, autostart_off, tmp_path
):
    d = config_dir("A", identity=False)
    chosen = tmp_path / "Nhận ở đây"
    chosen.mkdir()
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        edit, *_rest, start = widgets(run)
        type_name(edit, "PC Nhà Mới")
        pick_dir(run, dialogs, chosen)
        yield gui_wait(lambda: run.window.first_run.receive_dir == chosen, 5, "form nhận thư mục")
        start.click()
        yield gui_wait(lambda: host_started(run), 20, "node chạy")
        seen["node_name"] = run.host.node_name
        seen["receive_dir"] = run.host.receive_dir
        yield gui_wait(lambda: first_run_marked(d), 10, "first_run_done")
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script, keep_receive_dir=True)
    data = settings_json(d)
    assert seen["node_name"] == "PC Nhà Mới"
    assert data.get("name") == "PC Nhà Mới", data
    assert same(data.get("receive_dir"), chosen), f"receive_dir phải là gốc thật của {chosen}: {data}"
    assert os.path.isabs(data["receive_dir"])
    assert data.get("first_run_done") is True
    assert same(seen["receive_dir"], chosen)


@integration
def test_first_run_untouched_name_not_saved(gui, gui_wait, config_dir, ui_helpers, hostname, autostart_off):
    d = config_dir("A", identity=False)
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        *_rest, start = widgets(run)
        start.click()
        yield gui_wait(lambda: host_started(run), 20, "node chạy")
        seen["node_name"] = run.host.node_name
        yield gui_wait(lambda: first_run_marked(d), 10, "first_run_done")
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script, keep_receive_dir=True)
    data = settings_json(d)
    assert seen["node_name"] == HOSTNAME
    assert "name" not in data, f"tên trùng gợi ý: không ghi name (Q5): {data}"
    assert "receive_dir" not in data, f"không chọn thư mục: không ghi receive_dir (Q6, 14.1): {data}"
    assert data.get("first_run_done") is True


@integration
def test_first_run_unusable_dir_returns_to_form(
    gui, gui_wait, config_dir, ui_helpers, dialogs, hostname, autostart_off, tmp_path
):
    """Q6: thư mục người dùng chọn không mở được → về WELCOME (không ERROR), câu UNAVAILABLE dưới hàng
    thư mục, chữ đã gõ còn nguyên; chọn thư mục tốt rồi bấm lại thì chạy được."""
    from teleport.ui.main_window import ViewState

    d = config_dir("A", identity=False)
    bad = tmp_path / "la-file.txt"
    bad.write_bytes(b"khong phai thu muc")
    good = tmp_path / "nhan-tot"
    good.mkdir()
    states: list = []
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        edit, dir_button, dir_error, *_rest, start = widgets(run)
        type_name(edit, "Tên Đã Gõ")
        pick_dir(run, dialogs, bad)
        yield gui_wait(lambda: run.window.first_run.receive_dir == bad, 5, "form nhận đường dẫn")
        start.click()
        yield gui_wait(lambda: run.hosts, 5, "NodeHost được dựng")
        for _ in range(1000):  # ~20 s: ghi lại mọi trạng thái đi qua tới khi về WELCOME có lỗi
            if welcome(run) and dir_error.isVisibleTo(run.window):
                break
            states.append(run.window.view_state)
            yield
        yield gui_wait(
            lambda: welcome(run) and dir_error.isVisibleTo(run.window), 20, "về WELCOME với lỗi thư mục"
        )
        states.append(run.window.view_state)
        seen["error"] = dir_error.text()
        seen["name"] = edit.text()
        seen["focus_target"] = run.window.first_run.focus_target() is dir_button
        seen["visible"] = run.window.isVisible()
        pick_dir(run, dialogs, good)
        yield gui_wait(lambda: not dir_error.isVisibleTo(run.window), 5, "chọn thư mục mới thì ẩn lỗi")
        start.click()
        yield gui_wait(lambda: len(run.hosts) >= 2 and host_started(run), 20, "node chạy lần hai")
        seen["node_name"] = run.host.node_name
        yield gui_wait(lambda: first_run_marked(d), 10, "first_run_done")
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script, keep_receive_dir=True)
    assert ViewState.ERROR not in states, f"lỗi thư mục người dùng chọn không hiện màn ERROR: {states}"
    assert seen["error"] == UNAVAILABLE_TEXT.format(path=shown(bad))
    assert seen["name"] == "Tên Đã Gõ", "giữ chữ đã gõ"
    assert seen["focus_target"], "tiêu điểm ở [Đổi…] khi có lỗi thư mục"
    assert seen["visible"]
    assert seen["node_name"] == "Tên Đã Gõ"
    data = settings_json(d)
    assert data.get("name") == "Tên Đã Gõ"
    assert same(data.get("receive_dir"), good)


@integration
def test_first_run_retry_reuses_choice(gui, gui_wait, config_dir, ui_helpers, hostname, autostart_off):
    """Q8, 14.16: lần đầu lỗi LOCKED → ERROR; nhả khóa, bấm “Thử lại” → node chạy với tên đã gõ."""
    from teleport.lock import NodeLock
    from teleport.ui.main_window import ViewState

    d = config_dir("A", identity=False)
    holder = NodeLock(d)
    holder.acquire()
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        edit, *_rest, start = widgets(run)
        type_name(edit, "Máy Thử Lại")
        start.click()
        yield gui_wait(lambda: run.window.view_state is ViewState.ERROR, 30, "màn ERROR (LOCKED)")
        holder.release()
        [retry] = ui_helpers.buttons_with_text(run.window, ui_helpers.RETRY)
        retry.click()
        yield gui_wait(lambda: len(run.hosts) >= 2 and host_started(run), 30, "node chạy sau Thử lại")
        seen["node_name"] = run.host.node_name
        yield gui_wait(lambda: first_run_marked(d), 10, "first_run_done")
        quit_via_tray(run, ui_helpers.plain)

    try:
        gui(["--config-dir", str(d)], script, timeout=90)
    finally:
        holder.release()
    assert seen["node_name"] == "Máy Thử Lại", "Thử lại dùng lại lựa chọn của lần chạy đầu"
    assert settings_json(d).get("name") == "Máy Thử Lại"


@integration
def test_double_start_click_runs_one_node(gui, gui_wait, config_dir, ui_helpers, hostname, autostart_off):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    d = config_dir("A", identity=False)

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        edit, *_rest, start = widgets(run)
        start.click()
        start.click()
        QTest.keyClick(edit, Qt.Key.Key_Return)
        yield gui_wait(lambda: host_started(run), 20, "node chạy")
        for _ in range(15):
            yield
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(d)], script)
    assert len(run.hosts) == 1 and run.start_calls == 1, "bấm nhiều lần chỉ chạy một node"


# =================================================================================================
# Khởi động cùng Windows ở lần chạy đầu (Q7, 14.5, 14.6)
# =================================================================================================


@integration
def test_first_run_autostart_enabled_after_started(
    gui, gui_wait, config_dir, ui_helpers, hostname, autostart_on
):
    from teleport.app import autostart
    from teleport.app.settings import Settings

    d = config_dir("A", identity=False)
    reg = autostart_on
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        *_rest, switch, start = widgets(run)
        seen["checked"] = switch.isChecked()
        start.click()
        seen["writes_after_click"] = list(reg.writes())
        while not host_started(run):
            seen["writes_before_started"] = list(reg.writes())
            yield
        yield gui_wait(lambda: run_key(reg) is not None, 5, "giá trị Run được ghi")
        yield gui_wait(lambda: Settings.load(d).autostart, 10, "settings.autostart = True (reconcile D2)")
        yield gui_wait(lambda: first_run_marked(d), 10, "first_run_done")
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script)
    assert seen["checked"] is True
    assert seen["writes_after_click"] == [], "không ghi registry khi bấm Bắt đầu"
    assert seen.get("writes_before_started", []) == [], "chỉ ghi sau started"
    assert run_key(reg) == (autostart.launch_command(d), autostart.REG_SZ)
    assert autostart.state(d) is autostart.AutostartState.ON
    assert settings_json(d).get("autostart") is True


@integration
def test_first_run_autostart_off_does_not_touch_registry(
    gui, gui_wait, config_dir, ui_helpers, hostname, autostart_on
):
    from teleport.app.settings import Settings

    d = config_dir("A", identity=False)
    reg = autostart_on

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        *_rest, switch, start = widgets(run)
        switch.click()
        assert not switch.isChecked()
        start.click()
        yield gui_wait(lambda: host_started(run), 20, "node chạy")
        yield gui_wait(lambda: first_run_marked(d), 10, "first_run_done")
        for _ in range(10):
            yield
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script)
    assert reg.writes() == [], "tắt + trạng thái OFF: không gọi gì"
    assert run_key(reg) is None
    assert Settings.load(d).autostart is False


@integration
def test_first_run_autostart_off_when_on_disables(
    gui, gui_wait, config_dir, ui_helpers, hostname, autostart_on
):
    """Q7: tắt công tắc khi trạng thái lúc `started` là ON → `disable` (xóa giá trị Run của bản này)."""
    from teleport.app import autostart
    from teleport.app.settings import Settings

    d = config_dir("A", identity=False)
    reg = autostart_on
    reg.values[(autostart.RUN_KEY, autostart.VALUE_NAME)] = (autostart.launch_command(d), reg.REG_SZ)
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        *_rest, switch, start = widgets(run)
        seen["checked"] = switch.isChecked()
        switch.click()
        start.click()
        seen["writes_after_click"] = list(reg.writes())
        yield gui_wait(lambda: host_started(run), 20, "node chạy")
        yield gui_wait(lambda: run_key(reg) is None, 5, "giá trị Run bị xóa")
        yield gui_wait(lambda: first_run_marked(d), 10, "first_run_done")
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script)
    assert seen["checked"] is True, "ON → bật sẵn"
    assert seen["writes_after_click"] == []
    assert run_key(reg) is None
    assert Settings.load(d).autostart is False


@integration
@pytest.mark.parametrize("preset", ["blocked", "other"])
def test_first_run_autostart_off_keeps_blocked_and_other(
    gui, gui_wait, config_dir, ui_helpers, hostname, autostart_on, preset
):
    """14.6: BLOCKED (và OTHER) với công tắc tắt sẵn → không gọi gì; không xóa lựa chọn trong Windows
    hay giá trị Run của bản khác."""
    from teleport.app import autostart

    d = config_dir("A", identity=False)
    reg = autostart_on
    if preset == "blocked":
        reg.values[(autostart.RUN_KEY, autostart.VALUE_NAME)] = (autostart.launch_command(d), reg.REG_SZ)
        reg.values[(autostart.STARTUP_APPROVED_KEY, autostart.VALUE_NAME)] = (
            b"\x03" + b"\0" * 11,
            reg.REG_BINARY,
        )
    else:
        reg.values[(autostart.RUN_KEY, autostart.VALUE_NAME)] = (
            r'"C:\Khac\Teleport.exe" --background',
            reg.REG_SZ,
        )
    before = dict(reg.values)
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        *_rest, switch, start = widgets(run)
        seen["checked"] = switch.isChecked()
        start.click()
        yield gui_wait(lambda: host_started(run), 20, "node chạy")
        yield gui_wait(lambda: first_run_marked(d), 10, "first_run_done")
        for _ in range(10):
            yield
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script)
    assert seen["checked"] is False
    assert reg.writes() == [], f"{preset} + tắt: không ghi/xóa registry"
    assert reg.values == before


@integration
def test_first_run_autostart_write_error_shows_notice(
    gui, gui_wait, config_dir, ui_helpers, hostname, autostart_on
):
    from teleport.ui.main_window import Page

    d = config_dir("A", identity=False)
    reg = autostart_on
    reg.errors["set"] = OSError("giả Windows từ chối")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        *_rest, start = widgets(run)
        start.click()
        yield gui_wait(lambda: host_started(run), 20, "node vẫn chạy dù bật tự khởi động lỗi")
        view = app_of(run).transfers.view
        yield gui_wait(lambda: bool(view.notice_text()), 5, "dải thông báo tự khởi động lỗi")
        seen["notice"] = view.notice_text()
        seen["action"] = view.notice_action_text()
        view.notice_action.click()
        yield gui_wait(lambda: run.window.current_page is Page.SETTINGS, 5, "nút mở trang Cài đặt")
        seen["running"] = run.host.running
        yield gui_wait(lambda: first_run_marked(d), 10, "first_run_done vẫn được ghi")
        quit_via_tray(run, ui_helpers.plain)

    gui(["--config-dir", str(d)], script)
    assert seen["notice"] == AUTOSTART_FAILED
    assert seen["action"] == OPEN_SETTINGS
    assert seen["running"]
    assert run_key(reg) is None


@integration
def test_first_run_autostart_write_error_logs_only_error_type(
    gui, gui_wait, config_dir, ui_helpers, hostname, autostart_on, caplog, tmp_path
):
    """Q7, mvp N9: lỗi bật tự khởi động chỉ log tên loại lỗi, không đường dẫn thư mục cấu hình."""
    import logging

    d = config_dir("BI-MAT-cfg", identity=False)
    autostart_on.errors["set"] = OSError("BI-MAT-loi")

    def script(run):
        yield gui_wait(lambda: welcome(run), 10, "WELCOME")
        *_rest, start = widgets(run)
        start.click()
        yield gui_wait(lambda: host_started(run), 20, "node chạy")
        yield gui_wait(lambda: bool(app_of(run).transfers.view.notice_text()), 5, "dải thông báo")
        quit_via_tray(run, ui_helpers.plain)

    with caplog.at_level(logging.DEBUG, logger="teleport.ui"):
        gui(["--config-dir", str(d)], script)
    ui_records = [r for r in caplog.records if r.name.startswith("teleport.ui")]
    assert any(r.levelno >= logging.WARNING for r in ui_records), "phải có WARNING khi bật lỗi"
    for r in ui_records:
        msg = r.getMessage()
        assert "BI-MAT" not in msg, f"log lộ đường dẫn/chi tiết lỗi: {msg!r}"
