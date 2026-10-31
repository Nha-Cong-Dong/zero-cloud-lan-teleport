"""`teleport.ui.receive_dir.ReceiveDirController` (spec-b1b mục 2.2, 2.8, 6.2; mục 11 thắng khi khác).

Viết từ spec, không từ code. Không socket: `NodeHost` giả ghi lại mọi `submit(fn, on_done)` và trả một
`SubmitJob` giả (đếm `cancel()`); test tự chạy `fn` trên một node giả để biết controller định gọi gì,
rồi tự gọi `on_done` (như `NodeHost` làm trên thread Qt). Hộp chọn thư mục đi qua seam
`receive_dir.dialog_factory` (đọc lúc gọi): một `QFileDialog` con ghi lại `open()`, `exec()`,
`raise_()`, `activateWindow()`, `close()`, `setDirectory()`.

Câu chữ chép nguyên văn từ spec-b1b mục 2.8 (câu “lúc started” theo mục 11.9, câu RESERVED theo
mục 11.5). Mọi `{path}` qua `send.printable` (spec 2.8, dòng cuối).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

# ---- Câu của spec-b1b (2.8, 11.5, 11.9) -----------------------------------------------------------
CHOOSE_TITLE = "Chọn thư mục nhận file"
BUSY_TEXT = "Chưa đổi được thư mục nhận vì đang nhận file. Hãy đổi lại sau khi các lượt nhận xong."
UNAVAILABLE_TEXT = "Không dùng được thư mục {path}. Hãy chọn thư mục khác mà bạn có quyền ghi."
SETTINGS_TEXT = (
    "Chưa đổi được thư mục nhận vì không ghi được cài đặt của Teleport. Hãy kiểm tra quyền ghi vào thư mục "
    "cấu hình rồi thử lại."
)
OTHER_TEXT = "Chưa đổi được thư mục nhận vì một lỗi không mong đợi. Hãy thử lại."
RESERVED_TEXT = (
    "Không dùng được thư mục {path} để nhận file vì Teleport dùng nó cho việc riêng. Hãy chọn thư mục khác."
)
STARTED_UNAVAILABLE_TEXT = (
    "Teleport đang không nhận file vì không mở được thư mục nhận {path}. Hãy kiểm tra ổ đĩa và quyền ghi "
    "rồi chọn lại thư mục đó, hoặc chọn thư mục khác."
)
TRAY_TEXT = "Teleport đang không nhận file vì không mở được thư mục nhận. Mở Teleport để chọn thư mục khác."

SECRET = "BI-MAT-7f3a"  # mẩu tên thư mục không được lọt vào log (mvp N9)


def shown(path: Path) -> str:
    from teleport.ui.send import printable

    return printable(str(path))


def fill(template: str, path: Path) -> str:
    return template.replace("{path}", shown(path))


# =================================================================================================
# Host, node, hộp chọn giả
# =================================================================================================


class FakeJob:
    def __init__(self) -> None:
        self.cancel_calls = 0
        self.done = False

    def cancel(self) -> None:
        self.cancel_calls += 1


class FakeNode:
    def __init__(self) -> None:
        self.requested: list = []

    async def set_receive_dir(self, path):
        self.requested.append(path)
        return Path(path)


class FakeHost:
    """Thay `NodeHost`: `running`, ảnh chụp `receive_dir` / `receive_dir_error`, ghi lại `submit`."""

    def __init__(self, config_dir: Path, *, receive_dir=None, error=None, running: bool = True) -> None:
        from teleport.app.node import ReceiveDirChoice
        from teleport.ui.bridge import NodeOptions

        self.options = NodeOptions(config_dir=config_dir, receive_dir=ReceiveDirChoice.SAVED, mdns=None)
        self.running = running
        self.is_stopped = False
        self.node_name = "May-A"
        self.receive_dir = receive_dir
        self.receive_dir_error = error
        self.submits: list[SimpleNamespace] = []
        self.calls: list = []

    def submit(self, fn, on_done=None):
        job = FakeJob()
        self.submits.append(SimpleNamespace(fn=fn, on_done=on_done, job=job))
        return job

    def call(self, fn, on_done=None) -> None:
        self.calls.append((fn, on_done))

    def requested(self, index: int = -1) -> list:
        """Chạy `fn` của lần `submit` thứ `index` trên một node giả; trả các đường dẫn nó xin đổi sang."""
        node = FakeNode()
        result = self.submits[index].fn(node)
        if inspect.isawaitable(result):

            async def wait():
                return await result

            asyncio.run(wait())
        return node.requested

    def finish(self, result, index: int = -1) -> None:
        sub = self.submits[index]
        sub.job.done = True
        if sub.on_done is not None:
            sub.on_done(result)


class Dialogs:
    """Seam `dialog_factory`: trả một `QFileDialog` con ghi lại lời gọi."""

    def __init__(self) -> None:
        self.made: list = []
        self.parents: list = []

    def factory(self, parent=None, *args, **kwargs):
        from PySide6.QtWidgets import QFileDialog

        made = self.made

        class FakeDialog(QFileDialog):
            def __init__(self) -> None:
                super().__init__(parent)
                self.calls: list[str] = []
                self.directories: list = []
                made.append(self)

            def open(self, *a, **k):
                self.calls.append("open")

            def exec(self, *a, **k):
                self.calls.append("exec")
                return 0

            def exec_(self, *a, **k):
                self.calls.append("exec")
                return 0

            def raise_(self):
                self.calls.append("raise_")

            def activateWindow(self):
                self.calls.append("activateWindow")

            def close(self):
                self.calls.append("close")
                return super().close()

            def setDirectory(self, directory):
                self.directories.append(directory)
                return super().setDirectory(directory)

        self.parents.append(parent)
        return FakeDialog()


def norm(value) -> str:
    """Đường dẫn (str, Path hay QDir) dạng so được."""
    from PySide6.QtCore import QDir

    if isinstance(value, QDir):
        value = value.absolutePath()
    return os.path.normcase(os.path.abspath(str(value)))


# =================================================================================================
# Fixture
# =================================================================================================


@pytest.fixture
def env(qtbot, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QWidget

    from teleport.ui import receive_dir as mod

    dialogs = Dialogs()
    monkeypatch.setattr(mod, "dialog_factory", dialogs.factory)
    parent = QWidget()
    qtbot.addWidget(parent)
    cfg = tmp_path / "cfg"
    old = tmp_path / "nhan-cu"
    new = tmp_path / "nhan-moi"
    for d in (cfg, old, new):
        d.mkdir()
    ctl = mod.ReceiveDirController()
    states: list[int] = []
    failures: list[tuple[str, str]] = []
    ctl.state_changed.connect(lambda: states.append(1))
    ctl.failed.connect(lambda code, text: failures.append((code, text)))
    ns = SimpleNamespace(
        mod=mod,
        ctl=ctl,
        dialogs=dialogs,
        parent=parent,
        cfg=cfg,
        old=old,
        new=new,
        tmp=tmp_path,
        states=states,
        failures=failures,
    )

    def running_host(*, receive_dir=old, error=None) -> FakeHost:
        host = FakeHost(cfg, receive_dir=receive_dir, error=error)
        ctl.attach(host)
        ctl.on_started(host)
        return host

    ns.running_host = running_host
    yield ns
    ctl.close_all()


def start_change(env, path: Path | None = None) -> FakeHost:
    host = env.running_host()
    assert env.ctl.change(path or env.new) is True
    assert len(host.submits) == 1
    return host


# =================================================================================================
# Mã lỗi, câu chữ
# =================================================================================================


def test_change_error_codes_are_the_spec_values():
    from teleport.ui.receive_dir import ChangeError

    assert {e.value for e in ChangeError} == {"busy", "unavailable", "settings", "other", "reserved"}


@pytest.mark.parametrize(
    ("code", "template"),
    [
        ("busy", BUSY_TEXT),
        ("unavailable", UNAVAILABLE_TEXT),
        ("settings", SETTINGS_TEXT),
        ("other", OTHER_TEXT),
        ("reserved", RESERVED_TEXT),
    ],
)
def test_change_error_text_matches_spec(tmp_path, code, template):
    from teleport.ui.receive_dir import ChangeError, change_error_text

    path = tmp_path / "Phim"
    assert change_error_text(ChangeError(code), path) == fill(template, path)


def test_started_and_tray_texts_match_spec(tmp_path):
    from teleport.ui.receive_dir import choose_title, unavailable_text, unavailable_tray_text

    path = tmp_path / "O-ngoai"
    assert unavailable_text(path) == fill(STARTED_UNAVAILABLE_TEXT, path)
    assert unavailable_tray_text() == TRAY_TEXT
    assert choose_title() == CHOOSE_TITLE


def test_hostile_path_is_printable_in_texts(tmp_path):
    from teleport.ui.receive_dir import ChangeError, change_error_text, unavailable_text

    path = tmp_path / "a‮b\nc\x1b<b>x</b>{path}​"
    texts = [unavailable_text(path)] + [
        change_error_text(code, path) for code in (ChangeError.UNAVAILABLE, ChangeError.RESERVED)
    ]
    for text in texts:
        assert shown(path) in text, text
        for bad in ("‮", "\n", "\x1b", "​"):
            assert bad not in text, (bad, text)
    # `{path}` trong tên thư mục không bị thay lần hai (texts.fill một lượt).
    assert texts[0].count("{path}") == 1


def test_hostile_path_is_printable_in_failed_text(env):
    from teleport.app.node import ReceiveDirUnavailable

    hostile = env.tmp / "x‮y\nz"
    host = env.running_host()
    assert env.ctl.change(hostile) is True
    host.finish(ReceiveDirUnavailable(hostile))
    [(code, text)] = env.failures
    assert code == "unavailable"
    assert "‮" not in text and "\n" not in text
    assert shown(hostile) in text


# =================================================================================================
# attach, on_started
# =================================================================================================


def test_on_started_takes_dir_and_error_from_host(env):
    host = FakeHost(env.cfg, receive_dir=None, error=env.old)
    env.ctl.attach(host)
    assert (env.ctl.receive_dir, env.ctl.unavailable, env.ctl.pending) == (None, None, None)
    before = len(env.states)
    env.ctl.on_started(host)
    assert env.ctl.receive_dir is None
    assert env.ctl.unavailable == env.old
    assert env.ctl.pending is None
    assert not env.ctl.changing
    assert len(env.states) > before, "on_started phải phát state_changed"

    ok = FakeHost(env.cfg, receive_dir=env.new, error=None)
    env.ctl.attach(ok)
    env.ctl.on_started(ok)
    assert (env.ctl.receive_dir, env.ctl.unavailable) == (env.new, None)


def test_on_started_ignores_other_host(env):
    mine = FakeHost(env.cfg, receive_dir=env.old)
    other = FakeHost(env.cfg, receive_dir=env.new, error=env.tmp / "hong")
    env.ctl.attach(mine)
    before = len(env.states)
    env.ctl.on_started(other)
    assert (env.ctl.receive_dir, env.ctl.unavailable) == (None, None)
    assert len(env.states) == before, "host khác: không phát state_changed"


def test_attach_emits_state_changed_and_resets(env):
    env.running_host(receive_dir=env.old)
    before = len(env.states)
    env.ctl.attach(None)
    assert (env.ctl.receive_dir, env.ctl.unavailable, env.ctl.pending) == (None, None, None)
    assert len(env.states) > before


# =================================================================================================
# choose: hộp chọn thư mục
# =================================================================================================


def test_choose_opens_directory_dialog_non_blocking(env):
    from PySide6.QtWidgets import QFileDialog

    env.running_host(receive_dir=env.old)
    assert env.ctl.choose(env.parent) is True
    assert len(env.dialogs.made) == 1, "choose phải lấy hộp qua seam dialog_factory"
    dlg = env.dialogs.made[0]
    assert env.dialogs.parents == [env.parent]
    assert dlg.calls.count("open") == 1
    assert "exec" not in dlg.calls, "không exec() (chặn vòng sự kiện)"
    assert dlg.fileMode() == QFileDialog.FileMode.Directory
    assert dlg.testOption(QFileDialog.Option.ShowDirsOnly)
    assert not dlg.testOption(QFileDialog.Option.DontUseNativeDialog), "giữ hộp chọn của hệ điều hành"
    assert dlg.windowTitle() == CHOOSE_TITLE
    assert env.ctl.dialog is dlg
    assert norm(dlg.directory()) == norm(env.old), "hộp mở ở thư mục nhận đang dùng"


def test_choose_starts_in_unavailable_dir_when_no_receive_dir(env):
    env.running_host(receive_dir=None, error=env.new)
    assert env.ctl.choose(env.parent) is True
    dlg = env.dialogs.made[0]
    assert norm(dlg.directory()) == norm(env.new)


def test_choose_twice_raises_existing_dialog(env):
    env.running_host()
    assert env.ctl.choose(env.parent) is True
    assert env.ctl.choose(env.parent) is True
    assert len(env.dialogs.made) == 1, "lần hai không dựng hộp mới"
    dlg = env.dialogs.made[0]
    assert "raise_" in dlg.calls and "activateWindow" in dlg.calls
    assert dlg.calls.count("open") == 1


def test_choose_without_running_host_returns_false(env):
    assert env.ctl.choose(env.parent) is False, "chưa có host"
    stopped = FakeHost(env.cfg, receive_dir=env.old, running=False)
    env.ctl.attach(stopped)
    env.ctl.on_started(stopped)
    assert env.ctl.choose(env.parent) is False, "host chưa chạy"
    assert env.dialogs.made == []


def test_choose_while_changing_returns_false(env):
    start_change(env)
    assert env.ctl.changing
    assert env.ctl.choose(env.parent) is False
    assert env.dialogs.made == []


def test_dialog_finished_clears_reference(env):
    env.running_host()
    env.ctl.choose(env.parent)
    dlg = env.dialogs.made[0]
    dlg.finished.emit(0)
    assert env.ctl.dialog is None
    assert env.ctl.choose(env.parent) is True
    assert len(env.dialogs.made) == 2, "hộp đã đóng: lần sau dựng hộp mới"


def test_selected_dir_submits_set_receive_dir_and_sets_pending(env):
    host = env.running_host()
    env.ctl.choose(env.parent)
    before = len(env.states)
    env.dialogs.made[0].fileSelected.emit(str(env.new))
    assert len(host.submits) == 1
    [requested] = host.requested()
    assert Path(requested) == env.new
    assert env.ctl.pending == env.new
    assert env.ctl.changing
    assert env.ctl.receive_dir == env.old, "chưa xong thì thư mục nhận chưa đổi"
    assert len(env.states) > before


def test_empty_selection_does_nothing(env):
    host = env.running_host()
    env.ctl.choose(env.parent)
    env.dialogs.made[0].fileSelected.emit("")
    assert host.submits == []
    assert env.ctl.pending is None


# =================================================================================================
# change: kết quả
# =================================================================================================


def test_success_updates_receive_dir_clears_unavailable_emits_state_changed(env):
    host = env.running_host(receive_dir=None, error=env.old)
    assert env.ctl.change(env.new) is True
    before = len(env.states)
    real_root = env.tmp / "goc-that"  # node trả gốc thật, có thể khác đường dẫn đã chọn
    host.finish(real_root)
    assert env.ctl.receive_dir == real_root
    assert env.ctl.unavailable is None
    assert env.ctl.pending is None
    assert not env.ctl.changing
    assert len(env.states) > before
    assert env.failures == []


def test_change_while_changing_returns_false(env):
    host = start_change(env)
    assert env.ctl.change(env.tmp / "thu-ba") is False
    assert len(host.submits) == 1
    assert env.ctl.pending == env.new


def test_change_same_dir_does_not_call_node(env):
    host = env.running_host(receive_dir=env.old)
    assert env.ctl.change(env.old) is True
    assert host.submits == []
    assert host.calls == []
    assert env.ctl.pending is None
    assert env.failures == []


def test_change_without_running_host_returns_false(env):
    assert env.ctl.change(env.new) is False
    stopped = FakeHost(env.cfg, receive_dir=env.old, running=False)
    env.ctl.attach(stopped)
    assert env.ctl.change(env.new) is False
    assert stopped.submits == []


def _fail_with(env, error) -> tuple[str, str]:
    host = start_change(env)
    before = len(env.states)
    host.finish(error)
    assert env.ctl.pending is None
    assert not env.ctl.changing
    assert env.ctl.receive_dir == env.old, "lỗi: thư mục nhận không đổi"
    assert len(env.states) > before, "on_done luôn phát state_changed cuối cùng"
    assert len(env.failures) == 1, env.failures
    return env.failures[0]


def test_busy_error_text(env):
    from teleport.app.transfers import ReceiveDirBusy

    assert _fail_with(env, ReceiveDirBusy()) == ("busy", BUSY_TEXT)


def test_unavailable_error_text_names_chosen_path(env):
    from teleport.app.node import ReceiveDirUnavailable

    # Đường dẫn trong lỗi khác đường dẫn đã chọn: câu phải nêu thư mục người dùng chọn.
    code, text = _fail_with(env, ReceiveDirUnavailable(env.tmp / "khac-han"))
    assert code == "unavailable"
    assert text == fill(UNAVAILABLE_TEXT, env.new)


def test_settings_save_error_text(env):
    from teleport.app.settings import SettingsSaveError

    assert _fail_with(env, SettingsSaveError("x")) == ("settings", SETTINGS_TEXT)


def test_value_error_maps_to_unavailable(env, caplog):
    caplog.set_level(logging.DEBUG)
    code, text = _fail_with(env, ValueError("gốc thật quá dài"))
    assert (code, text) == ("unavailable", fill(UNAVAILABLE_TEXT, env.new))
    assert any(r.levelno == logging.WARNING and r.name.startswith("teleport") for r in caplog.records)


@pytest.mark.parametrize("error", [KeyError("x"), OSError(5, "lỗi lạ"), TypeError("t")])
def test_other_error_maps_to_other(env, caplog, error):
    caplog.set_level(logging.DEBUG)
    assert _fail_with(env, error) == ("other", OTHER_TEXT)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR and r.name.startswith("teleport")]
    assert errors, "lỗi lạ phải ghi log ERROR"
    assert any(type(error).__name__ in r.getMessage() for r in errors), "log ghi tên loại lỗi"


def test_runtime_error_while_host_running_maps_to_other(env):
    """Spec-b1b 11.8: RuntimeError chỉ im lặng khi host không còn chạy; còn chạy thì là OTHER."""
    assert _fail_with(env, RuntimeError("lạ")) == ("other", OTHER_TEXT)


@pytest.mark.parametrize("case", ["cancelled", "runtime_after_stop"])
def test_runtime_and_cancelled_errors_are_silent(env, caplog, case):
    caplog.set_level(logging.DEBUG)
    host = start_change(env)
    if case == "cancelled":
        error: BaseException = asyncio.CancelledError()
    else:
        host.running = False  # node đang dừng
        error = RuntimeError("node chưa chạy hoặc đang đóng")
    before = len(env.states)
    host.finish(error)
    assert env.failures == []
    assert env.ctl.pending is None
    assert len(env.states) > before
    loud = [r for r in caplog.records if r.levelno > logging.DEBUG and r.name.startswith("teleport.ui")]
    assert loud == [], [r.getMessage() for r in loud]


# =================================================================================================
# Thư mục dành riêng (spec-b1b 11.5)
# =================================================================================================


def _reserved_cases():
    cases = [
        pytest.param(lambda e: e.cfg, id="config_dir"),
        pytest.param(lambda e: e.cfg / "con", id="inside_config_dir"),
        pytest.param(lambda e: e.tmp / ".teleport", id="dot_teleport"),
        pytest.param(lambda e: e.tmp / "x" / ".teleport" / "y", id="inside_dot_teleport"),
        pytest.param(lambda e: e.cfg / "a" / ".." / "b", id="config_dir_with_dotdot"),
    ]
    if sys.platform == "win32":
        cases.append(pytest.param(lambda e: Path(str(e.cfg).upper()), id="config_dir_other_case"))
    return cases


@pytest.mark.parametrize("make_path", _reserved_cases())
def test_rejects_config_dir_and_dot_teleport(env, make_path):
    host = env.running_host()
    path = make_path(env)
    env.ctl.change(path)
    assert host.submits == [], "thư mục dành riêng: không gọi node"
    assert env.ctl.pending is None
    assert not env.ctl.changing
    [(code, text)] = env.failures
    assert code == "reserved"
    # `{path}` là thư mục đã chọn; với `..` thì chấp nhận cả dạng đã chuẩn hóa (spec không nói).
    assert text in (fill(RESERVED_TEXT, path), fill(RESERVED_TEXT, Path(os.path.abspath(path)))), text
    assert env.ctl.receive_dir == env.old


@pytest.mark.parametrize(
    "make_path",
    [
        pytest.param(lambda e: e.cfg.parent / (e.cfg.name + "-khac"), id="sibling_with_same_prefix"),
        pytest.param(lambda e: e.tmp / ".teleportx", id="name_starting_with_dot_teleport"),
        pytest.param(lambda e: e.cfg.parent, id="parent_of_config_dir"),
    ],
)
def test_similar_but_not_reserved_dirs_are_submitted(env, make_path):
    host = env.running_host()
    path = make_path(env)
    assert env.ctl.change(path) is True
    assert len(host.submits) == 1, f"{path} không phải thư mục dành riêng"
    assert env.failures == []


def test_rejected_from_dialog_selection(env):
    host = env.running_host()
    env.ctl.choose(env.parent)
    env.dialogs.made[0].fileSelected.emit(str(env.cfg))
    assert host.submits == []
    assert [code for code, _ in env.failures] == ["reserved"]


# =================================================================================================
# attach giữa chừng, close_all
# =================================================================================================


def test_attach_new_host_cancels_job_closes_dialog_resets_state(env):
    host = env.running_host(receive_dir=env.old, error=None)
    env.ctl.choose(env.parent)
    dlg = env.dialogs.made[0]
    assert env.ctl.change(env.new) is True
    job = host.submits[0].job
    before = len(env.states)
    env.ctl.attach(FakeHost(env.cfg, receive_dir=env.tmp / "khac"))
    assert job.cancel_calls >= 1, "attach phải hủy SubmitJob đang chạy"
    assert "close" in dlg.calls, "attach phải đóng hộp chọn"
    assert env.ctl.dialog is None
    assert (env.ctl.receive_dir, env.ctl.unavailable, env.ctl.pending) == (None, None, None)
    assert not env.ctl.changing
    assert len(env.states) > before


@pytest.mark.parametrize("result", ["path", "error"])
def test_result_of_old_job_ignored_after_attach(env, result):
    from teleport.app.transfers import ReceiveDirBusy

    old_host = start_change(env)
    new_host = FakeHost(env.cfg, receive_dir=env.tmp / "cua-host-moi")
    env.ctl.attach(new_host)
    env.ctl.on_started(new_host)
    before = len(env.states)
    old_host.finish(env.new if result == "path" else ReceiveDirBusy())
    assert env.ctl.receive_dir == env.tmp / "cua-host-moi"
    assert env.ctl.pending is None
    assert env.failures == []
    assert len(env.states) == before, "kết quả của job cũ: bỏ hẳn"
    assert env.ctl.change(env.new) is True, "job cũ không giữ cờ đang đổi của host mới"


def test_close_all_closes_dialog(env):
    host = env.running_host()
    env.ctl.choose(env.parent)
    dlg = env.dialogs.made[0]
    env.ctl.change(env.new)
    env.ctl.close_all()
    assert "close" in dlg.calls
    assert env.ctl.dialog is None
    assert host.submits[0].job.cancel_calls == 0, "close_all không hủy job"


# =================================================================================================
# Log (mvp N9)
# =================================================================================================


def test_logs_never_contain_path(env, caplog):
    from teleport.app.node import ReceiveDirUnavailable
    from teleport.app.settings import SettingsSaveError
    from teleport.app.transfers import ReceiveDirBusy

    caplog.set_level(logging.DEBUG)
    secret = env.tmp / SECRET
    errors = [
        ReceiveDirBusy(),
        ReceiveDirUnavailable(secret),
        SettingsSaveError(SECRET),
        ValueError(SECRET),
        KeyError(SECRET),
        RuntimeError(SECRET),
        asyncio.CancelledError(),
    ]
    host = env.running_host()
    for error in errors:
        assert env.ctl.change(secret) is True
        host.finish(error)
    env.ctl.change(env.cfg / SECRET)  # thư mục dành riêng
    assert env.failures, "phải có lỗi được báo"
    for record in caplog.records:
        assert SECRET not in record.getMessage(), record.getMessage()
        assert SECRET not in (record.exc_text or ""), record.exc_text
        if record.exc_info and record.exc_info[1] is not None:
            assert SECRET not in str(record.exc_info[1]), "không ghi traceback mang đường dẫn"


# =================================================================================================
# docs/mvp.md mục 8 ghi đúng các câu (spec-b1b 2.10, 11.4)
# =================================================================================================

_MVP = Path(__file__).resolve().parents[2] / "docs" / "mvp.md"


def _mvp_section_8() -> str:
    text = _MVP.read_text(encoding="utf-8")
    start = text.index("## 8.")
    nxt = text.find("\n## ", start + 5)
    return text[start : nxt if nxt != -1 else len(text)]


@pytest.mark.parametrize(
    "template",
    [
        BUSY_TEXT,
        UNAVAILABLE_TEXT,
        SETTINGS_TEXT,
        OTHER_TEXT,
        RESERVED_TEXT,
        STARTED_UNAVAILABLE_TEXT,
        TRAY_TEXT,
        "Chọn thư mục nhận",
        "Chọn thư mục khác",
    ],
    ids=[
        "busy",
        "unavailable",
        "settings",
        "other",
        "reserved",
        "started",
        "tray",
        "btn_choose",
        "btn_other",
    ],
)
def test_change_texts_are_written_in_mvp_section_8(template):
    """Mỗi câu (bỏ chỗ `{path}`, tài liệu viết ví dụ đường dẫn in nghiêng) có trong bảng mvp mục 8."""
    section = _mvp_section_8()
    for part in template.split("{path}"):
        part = part.strip()
        if part:
            assert part in section, f"mvp mục 8 thiếu: {part!r}"
