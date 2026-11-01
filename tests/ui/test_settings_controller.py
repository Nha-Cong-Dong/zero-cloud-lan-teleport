"""`SettingsController` (spec D2 mục 1.2 “Registry thắng”, 1.6 Controller, 1.9; test 4.3; mục 9 thắng).

Viết từ spec, không từ code. Controller nối một `SettingsPage` thật (model thật) với host giả: `call` /
`submit` chỉ ghi lại, test tự chạy trên một node giả và tự giao kết quả, nên thứ tự kết quả (kể cả kết quả
của host cũ) do test quyết định. Registry là `FakeRegistry` của fixture tự động (`tests/conftest.py`).

Luật kiểm:
- Lưu trước, áp sau; lỗi → trả control về giá trị đã xác nhận gần nhất; chỉ lời gọi mới nhất của một
  trường được trả control về; kết quả của host cũ / sau `attach` bị bỏ.
- Bảng lỗi 1.6: `SettingsSaveError` → trả về + InfoBar câu SETTINGS; `MessageError` (tên) → lỗi tại ô,
  giữ chữ; `KeyError` (hỏi trước khi nhận) → im lặng, đọc lại ảnh chụp; `RuntimeError` / `CancelledError`
  → im lặng; khác → trả về + InfoBar câu OTHER, log chỉ tên loại.
- Ẩn/hiện: `node.set_visible(v)` rồi áp luật ghép đôi trong **một** `host.call`.
- Ngôn ngữ: lưu xong mới `i18n.apply_preference`.
- Tự khởi động: registry trước (thread Qt); lỗi → trả công tắc, báo, không lưu; ổn → lưu
  `set_ui_preferences(autostart=…)`, lưu lỗi chỉ WARNING. Đối chiếu lúc `started` / trang hiện: chỉ ghi
  `settings.json`, không bao giờ ghi registry.
- Xóa toàn bộ lịch sử: hộp `open()` (không `exec`), mặc định “Không”, Esc = “Không”; đồng ý →
  `node.transfers.clear_history()` → SUCCESS, `history_cleared(None)` (9.9).
- Xuất log: hộp lưu `open()`; `export_fn(log_files(), dest)` chạy trong thread “teleport-logexport”.

Giả định / seam (giao-dien khớp): `settings.read_snapshot` là biến module đọc lúc gọi (test thay bằng hàm
đọc node giả); `settings.dialog_factory(parent)` dựng hộp lưu kiểu `QFileDialog` (test dùng lớp con không
native, chấp nhận bằng `selectFile` + `accept()` như người dùng); `open_url` nhận `QUrl` (hay đường dẫn);
host giả có `apply_pairing_policy(node)` như `NodeHost`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QAbstractButton, QApplication, QDialog, QFileDialog, QMessageBox

# ---- Câu chữ (spec D2 1.9) ------------------------------------------------------------------------
SETTINGS_ERROR = (
    "Chưa lưu được cài đặt vì không ghi được vào thư mục cấu hình của Teleport. Hãy kiểm tra quyền ghi "
    "rồi thử lại."
)
OTHER_ERROR = "Chưa lưu được cài đặt vì một lỗi không mong đợi. Hãy thử lại."
SNAPSHOT_ERROR = "Không đọc được cài đặt hiện tại."
RETRY = "Thử lại"
NAME_INVALID = "Tên thiết bị cần từ 1 đến 64 ký tự và không có ký tự điều khiển."
AUTOSTART_WRITE_ERROR = (
    "Chưa đổi được việc khởi động cùng Windows vì Windows không cho ghi cài đặt khởi động. Hãy thử lại."
)
CLEAR_TITLE = "Xóa toàn bộ lịch sử"
CLEAR_QUESTION = (
    "Xóa lịch sử truyền file với mọi máy, kể cả máy đã hủy ghép đôi? File đã nhận vẫn còn nguyên trong "
    "thư mục nhận."
)
CLEAR_YES = "Xóa lịch sử"
CLEAR_NO = "Không"
CLEARED = "Đã xóa toàn bộ lịch sử truyền file."
CLEAR_FAILED = "Chưa xóa được lịch sử truyền file. Hãy thử lại."
SAVE_TITLE = "Lưu file log"
SAVE_FILTER = "File zip (*.zip)"
EXPORT_SAVED = "Đã lưu log vào {path}."
OPEN_FOLDER = "Mở thư mục"
EXPORT_WRITE_ERROR = "Không lưu được file log vào {path}. Hãy chọn thư mục khác mà bạn có quyền ghi."
EXPORT_OTHER_ERROR = "Không xuất được log vì một lỗi không mong đợi. Hãy thử lại."

MB = 1048576
SECRET = "bí-mật-C:\\Users\\Nguyễn\\thư mục"


# =================================================================================================
# Node giả, host giả
# =================================================================================================


def view(name: str, *, paired: bool = True, online: bool = True):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=hashlib.sha256(name.encode()).hexdigest()[:32],
        name=name,
        os="windows",
        paired=paired,
        online=online,
        connected=False,
        addresses=("192.168.1.9",) if online else (),
        port=47474 if online else None,
    )


class FakeTransfers:
    def __init__(self, node: FakeNode) -> None:
        self.node = node

    def clear_history(self, device_id=None) -> None:
        self.node._enter("clear_history", device_id)


class FakeNode:
    """Node giả: các setter của spec 1.6 ghi lại lời gọi; `fail(op, *lỗi)` cho các lần gọi tới ném lỗi
    theo thứ tự (`None` = lần đó thành công)."""

    def __init__(self) -> None:
        self.log: list[tuple] = []
        self._fail: dict[str, list] = {}
        self.name = "May-A"
        self.visible = True
        self._accept_pairing = True
        self.rate_limit: int | None = None
        self.retention: int | None = None
        self.language: str | None = None
        self.autostart = False
        self.devices_settings: dict = {}
        self.transfers = FakeTransfers(self)
        self.threads: set[str] = set()

    def fail(self, op: str, *errors) -> None:
        self._fail.setdefault(op, []).extend(errors)

    def _enter(self, op: str, *args) -> None:
        self.log.append((op, *args))
        self.threads.add(threading.current_thread().name)
        queue = self._fail.get(op)
        if queue:
            error = queue.pop(0)
            if error is not None:
                raise error

    @property
    def accept_pairing(self) -> bool:
        return self._accept_pairing

    @accept_pairing.setter
    def accept_pairing(self, value: bool) -> None:
        self.log.append(("accept_pairing", value))
        self._accept_pairing = value

    def snapshot(self):
        from teleport.ui.settings import SettingsSnapshot

        self._enter("read_snapshot")
        return SettingsSnapshot(
            name=self.name,
            visible=self.visible,
            rate_limit=self.rate_limit,
            temp_retention_days=self.retention,
            language=self.language,
            autostart=self.autostart,
            devices=dict(self.devices_settings),
        )

    def set_name(self, name: str) -> str:
        self._enter("set_name", name)
        self.name = name.strip()
        return self.name

    def set_visible(self, visible: bool) -> None:
        self._enter("set_visible", visible)
        self.visible = visible

    def set_ui_preferences(self, **kw) -> None:
        self._enter("set_ui_preferences", kw)
        if "language" in kw:
            self.language = kw["language"]
        if "autostart" in kw:
            self.autostart = kw["autostart"]

    def set_rate_limit(self, value) -> None:
        self._enter("set_rate_limit", value)
        self.rate_limit = value

    def set_temp_retention_days(self, days) -> None:
        self._enter("set_temp_retention_days", days)
        self.retention = days

    def set_ask_before_receive(self, device_id: str, ask: bool) -> None:
        from teleport.app.settings import DeviceSettings

        self._enter("set_ask_before_receive", device_id, ask)
        self.devices_settings[device_id] = DeviceSettings(ask_before_receive=ask)

    def device_settings(self, device_id: str):
        from teleport.app.settings import DeviceSettings

        self._enter("device_settings", device_id)
        return self.devices_settings.get(device_id, DeviceSettings())


class Pending:
    def __init__(self, fn, on_done) -> None:
        self.fn = fn
        self.on_done = on_done
        self.ran = False


class FakeJob:
    def __init__(self) -> None:
        self.done = False

    def cancel(self) -> None:
        pass


class FakeHost(QObject):
    """Host giả: `call`/`submit` chỉ ghi lại; `run_calls()` chạy chúng trên node giả theo thứ tự."""

    started = Signal()
    stopped = Signal()

    def __init__(self, node: FakeNode) -> None:
        super().__init__()
        self.node = node
        self.calls: list[Pending] = []
        self.submits: list[Pending] = []
        self.wait_pairing_open = False
        self.running = True
        self.node_name = node.name

    def call(self, fn, on_done=None) -> None:
        self.calls.append(Pending(fn, on_done))

    def submit(self, fn, on_done=None):
        self.submits.append(Pending(fn, on_done))
        return FakeJob()

    def apply_pairing_policy(self, node) -> None:
        node.accept_pairing = bool(node.visible or self.wait_pairing_open)

    def set_node_name(self, name: str) -> None:
        self.node_name = name

    def pending(self) -> list[Pending]:
        return [p for p in self.calls if not p.ran]

    def run_one(self, p: Pending, *, deliver: bool = True):
        p.ran = True
        try:
            result = p.fn(self.node)
        except Exception as e:
            result = e
        if deliver and p.on_done is not None:
            p.on_done(result)
        return result

    def run_calls(self) -> list:
        out = []
        while True:
            pending = self.pending()
            if not pending:
                return out
            for p in pending:
                out.append(self.run_one(p))


# =================================================================================================
# Phụ trợ
# =================================================================================================


def settle(qtbot, ms: int = 20) -> None:
    QApplication.processEvents()
    qtbot.wait(ms)


def plain(text: str) -> str:
    return text.replace("&", "")


def notice(page) -> tuple[object, str] | None:
    """(mức, chữ) của `info_bar` khi đang hiện, không thì None."""
    bar = page.info_bar
    if bar.isHidden():
        return None
    return bar.level, " ".join(t for t in (bar.title, bar.text) if t)


def boxes() -> list[QMessageBox]:
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()]


def button(box, text: str) -> QAbstractButton:
    found = [b for b in box.findChildren(QAbstractButton) if plain(b.text()) == text and b.isVisibleTo(box)]
    assert len(found) == 1, (
        f"cần đúng một nút “{text}”: {[b.text() for b in box.findChildren(QAbstractButton)]}"
    )
    return found[0]


def url_path(arg) -> Path:
    if hasattr(arg, "toLocalFile"):
        return Path(arg.toLocalFile())
    return Path(str(arg))


class SaveDialog(QFileDialog):
    """`dialog_factory` giả: QFileDialog thật, không native; `open()` chỉ ghi lại (không hiện)."""

    made: ClassVar[list[SaveDialog]] = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        self.opened = 0
        SaveDialog.made.append(self)

    def open(self, *args) -> None:
        self.opened += 1

    def choose(self, path: Path) -> None:
        """Như người dùng gõ tên file rồi bấm Lưu."""
        self.selectFile(str(path))
        self.accept()


@pytest.fixture(autouse=True)
def no_exec(monkeypatch):
    """Không hộp nào được mở bằng `exec()`."""
    calls: list[str] = []

    def fake(self, *a, **k):
        calls.append(type(self).__name__)
        return 0

    monkeypatch.setattr(QDialog, "exec", fake)
    monkeypatch.setattr(QMessageBox, "exec", fake)
    yield calls
    assert not calls, f"hộp mở bằng exec(): {calls}"


@pytest.fixture
def env(qtbot, tmp_path, monkeypatch, fake_registry):
    """Trang + controller + host giả đã `attach`. `start()` chạy `on_started` và giao ảnh chụp."""
    from teleport.app import autostart
    from teleport.ui import i18n
    from teleport.ui import settings as ui_settings
    from teleport.ui.radar import DeviceListModel
    from teleport.ui.settings import SettingsController, SettingsPage

    exe = tmp_path / "app" / "Teleport.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(autostart, "_frozen", lambda: True)
    monkeypatch.setattr(autostart, "_executable", lambda: str(exe))
    monkeypatch.setattr(ui_settings, "read_snapshot", lambda node: node.snapshot())
    SaveDialog.made = []
    monkeypatch.setattr(ui_settings, "dialog_factory", SaveDialog)
    applied: list = []
    monkeypatch.setattr(i18n, "apply_preference", applied.append)

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    logs = [cfg / "logs" / "teleport.log.1", cfg / "logs" / "teleport.log"]
    model = DeviceListModel()
    page = SettingsPage(model)
    model.setParent(page)
    qtbot.addWidget(page)
    page.resize(980, 660)
    page.show()
    qtbot.waitExposed(page)
    urls: list = []
    c = SettingsController(
        page, cfg, message_parent=lambda: page, open_url=urls.append, log_files=lambda: logs
    )
    node = FakeNode()
    host = FakeHost(node)
    rec = SimpleNamespace(names=[], visibility=[], cleared=[])
    c.name_changed.connect(rec.names.append)
    c.visibility_changed.connect(rec.visibility.append)
    c.history_cleared.connect(rec.cleared.append)
    c.attach(host)

    def start() -> None:
        c.on_started(host)
        host.run_calls()
        settle(qtbot)

    ns = SimpleNamespace(
        page=page,
        model=model,
        c=c,
        host=host,
        node=node,
        start=start,
        cfg=cfg,
        logs=logs,
        urls=urls,
        rec=rec,
        applied=applied,
        registry=fake_registry,
        qtbot=qtbot,
    )
    yield ns
    with contextlib.suppress(Exception):
        c.shutdown()
    for box in boxes():
        box.close()
    settle(qtbot)


def ops(node: FakeNode, name: str) -> list[tuple]:
    return [e for e in node.log if e[0] == name]


# =================================================================================================
# Vòng đời
# =================================================================================================


def test_on_started_reads_snapshot_enables_controls(env):
    env.node.name = "May-Ten"
    env.node.visible = False
    env.node.rate_limit = 4 * MB
    assert not env.page.name_edit.isEnabled(), "attach → set_loaded(False)"
    env.start()
    assert ops(env.node, "read_snapshot"), "on_started đọc ảnh chụp bằng host.call"
    assert env.page.name_edit.isEnabled()
    assert env.page.name_edit.text() == "May-Ten"
    assert not env.page.visible_switch.isChecked()
    assert env.page.rate_checkbox.isChecked()
    assert env.page.rate_spin.value() == pytest.approx(4.0)


def test_on_started_other_host_is_ignored(env):
    other = FakeHost(FakeNode())
    env.c.on_started(other)
    assert other.calls == [] and env.host.calls == []


def test_old_host_results_ignored(env):
    env.c.on_started(env.host)
    [old] = env.host.pending()
    env.node.name = "Cu"
    new = FakeHost(FakeNode())
    env.c.attach(new)
    env.host.run_one(old)  # kết quả của host cũ tới muộn
    settle(env.qtbot)
    assert env.page.name_edit.text() != "Cu"
    assert not env.page.name_edit.isEnabled()
    env.c.on_started(env.host)
    assert env.host.pending() == [], "on_started của host cũ bị bỏ"


def test_snapshot_read_error_shows_retry(env):
    from teleport.ui.widgets import InfoLevel

    env.node.fail("read_snapshot", OSError(5, SECRET))
    env.start()
    level, text = notice(env.page)
    assert level is InfoLevel.ERROR and SNAPSHOT_ERROR in text
    retry = [b for b in env.page.info_bar.findChildren(QAbstractButton) if plain(b.text()) == RETRY]
    assert retry, "có nút Thử lại"
    retry[0].click()
    settle(env.qtbot)
    env.host.run_calls()
    settle(env.qtbot)
    assert len(ops(env.node, "read_snapshot")) == 2
    assert env.page.name_edit.isEnabled()


def test_attach_resets_disables_closes_dialogs(env):
    env.start()
    env.page.clear_history_requested.emit()
    env.page.export_logs_requested.emit()
    settle(env.qtbot)
    assert boxes(), "hộp xác nhận đang mở"
    assert SaveDialog.made and SaveDialog.made[-1].opened == 1
    env.c.attach(FakeHost(FakeNode()))
    settle(env.qtbot)
    assert boxes() == []
    assert not env.page.name_edit.isEnabled()
    dlg = SaveDialog.made[-1]
    import shiboken6

    assert not shiboken6.isValid(dlg) or not dlg.isVisible()


def test_page_show_refreshes_snapshot(env):
    env.start()
    before = len(ops(env.node, "read_snapshot"))
    env.page.hide()
    env.page.show()
    settle(env.qtbot)
    env.host.run_calls()
    assert len(ops(env.node, "read_snapshot")) == before + 1


# =================================================================================================
# Tên, ẩn/hiện
# =================================================================================================


def test_name_success_emits_name_changed(env):
    env.start()
    env.page.name_edit.setText("May-Moi")
    env.page.name_submitted.emit("May-Moi")
    assert env.rec.names == [], "chưa lưu thì chưa báo"
    env.host.run_calls()
    settle(env.qtbot)
    assert ops(env.node, "set_name") == [("set_name", "May-Moi")]
    assert env.rec.names == ["May-Moi"]
    assert env.page.name_edit.text() == "May-Moi"


def test_name_message_error_inline_not_reverted(env):
    from teleport.core.messages import MessageError

    env.start()
    env.page.name_edit.setText("Ten-Sai")
    env.node.fail("set_name", MessageError("tên sai"))
    env.page.name_submitted.emit("Ten-Sai")
    env.host.run_calls()
    settle(env.qtbot)
    assert env.page.name_error.isVisibleTo(env.page)
    assert env.page.name_error.text() == NAME_INVALID
    assert env.page.name_edit.text() == "Ten-Sai", "giữ chữ, không trả về"
    assert env.rec.names == []
    assert notice(env.page) is None


def test_visible_and_accept_pairing_in_same_call(env):
    env.start()
    for v in (False, True):
        env.page.visible_switch.setChecked(v)
        env.page.visible_toggled.emit(v)
        pending = env.host.pending()
        assert len(pending) == 1, "đổi ẩn/hiện và áp luật ghép đôi: MỘT host.call"
        env.node.log.clear()
        env.host.run_one(pending[0])
        settle(env.qtbot)
        assert env.node.log == [("set_visible", v), ("accept_pairing", v)]
    assert env.rec.visibility == [False, True]


def test_visible_keeps_wait_pairing_open(env):
    """9.3: host đang “chờ ghép đôi” → ẩn qua Cài đặt vẫn nhận ghép đôi."""
    env.start()
    env.host.wait_pairing_open = True
    env.page.visible_switch.setChecked(False)
    env.page.visible_toggled.emit(False)
    env.host.run_calls()
    assert env.node.visible is False and env.node.accept_pairing is True


# =================================================================================================
# Bảng lỗi
# =================================================================================================


def _user_change(env, field: str):
    """Thay đổi của người dùng cho `field`: đặt control như người dùng rồi phát signal. Trả (op, hàm đọc
    giá trị đang hiện, giá trị đã xác nhận)."""
    page = env.page
    if field == "name":
        page.name_edit.setText("May-Moi")
        page.name_submitted.emit("May-Moi")
        return "set_name", lambda: page.name_edit.text(), "May-A"
    if field == "visible":
        page.visible_switch.setChecked(False)
        page.visible_toggled.emit(False)
        return "set_visible", lambda: page.visible_switch.isChecked(), True
    if field == "language":
        combo = page.language_combo
        combo.setCurrentIndex(combo.findData("vi"))
        page.language_selected.emit("vi")
        return "set_ui_preferences", lambda: combo.currentData(), None
    if field == "rate":
        page.rate_checkbox.setChecked(True)
        page.rate_spin.setValue(9)
        page.rate_limit_changed.emit(9 * MB)
        return "set_rate_limit", lambda: page.rate_checkbox.isChecked(), False
    if field == "retention":
        page.retention_spin.setValue(30)
        page.retention_changed.emit(30)
        return "set_temp_retention_days", lambda: page.retention_spin.value(), _default_days()
    if field == "ask":
        from teleport.ui.settings import TrustedDeviceRow

        b = env.b
        [row] = [r for r in page.findChildren(TrustedDeviceRow) if r.device_id == b.device_id]
        checkbox = row.ask_checkbox
        checkbox.setChecked(False)
        page.ask_toggled.emit(b.device_id, False)
        return "set_ask_before_receive", lambda: checkbox.isChecked(), True
    raise AssertionError(field)


def _default_days() -> int:
    from teleport.app import storage

    return int(storage.TEMP_RETENTION / 86400)


def _start_with_device(env) -> None:
    env.b = view("May-B")
    env.model.reset([env.b])
    env.start()


FIELDS = ["name", "visible", "language", "rate", "retention", "ask"]


@pytest.mark.parametrize("field", FIELDS)
def test_settings_save_error_reverts_and_notice(env, field):
    from teleport.app.settings import SettingsSaveError
    from teleport.ui.widgets import InfoLevel

    _start_with_device(env)
    op, current, confirmed = _user_change(env, field)
    env.node.fail(op, SettingsSaveError("không ghi được"))
    env.host.run_calls()
    settle(env.qtbot)
    assert current() == confirmed, f"{field}: trả control về giá trị đã xác nhận"
    assert notice(env.page) == (InfoLevel.ERROR, SETTINGS_ERROR)
    assert env.applied == [], "ngôn ngữ lưu lỗi: không áp"
    assert env.rec.names == [] and env.rec.visibility == []


@pytest.mark.parametrize("field", FIELDS)
def test_runtime_error_when_not_running_silent(env, field):
    _start_with_device(env)
    op, _current, _confirmed = _user_change(env, field)
    env.node.fail(op, RuntimeError("node chưa chạy"))
    env.host.run_calls()
    settle(env.qtbot)
    assert notice(env.page) is None


def test_cancelled_error_silent(env):
    env.start()
    env.page.visible_toggled.emit(False)
    [p] = env.host.pending()
    p.ran = True
    p.on_done(asyncio.CancelledError())  # như `NodeHost` khi việc bị hủy
    settle(env.qtbot)
    assert notice(env.page) is None


@pytest.mark.parametrize("field", ["name", "visible", "rate", "retention"])
def test_unexpected_error_other_text_logs_type_only(env, field, caplog):
    from teleport.ui.widgets import InfoLevel

    _start_with_device(env)
    caplog.set_level(logging.DEBUG)
    op, current, confirmed = _user_change(env, field)
    env.node.fail(op, ZeroDivisionError(SECRET))
    env.host.run_calls()
    settle(env.qtbot)
    assert current() == confirmed
    assert notice(env.page) == (InfoLevel.ERROR, OTHER_ERROR)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "lỗi không mong đợi: log ERROR"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ZeroDivisionError" in text
    assert SECRET not in text and "Nguyễn" not in text
    assert not any(r.exc_info for r in errors), "log chỉ tên loại lỗi, không traceback kèm thông điệp"


def test_rapid_toggles_only_latest_failure_reverts(env):
    from teleport.app.settings import SettingsSaveError

    env.start()
    sw = env.page.visible_switch
    # Lần 1 (ẩn) lỗi, lần 2 (hiện) ổn: lỗi của lần cũ không kéo công tắc về.
    sw.setChecked(False)
    env.page.visible_toggled.emit(False)
    sw.setChecked(True)
    env.page.visible_toggled.emit(True)
    env.node.fail("set_visible", SettingsSaveError("x"), None)
    env.host.run_calls()
    settle(env.qtbot)
    assert sw.isChecked() is True
    # Lần 1 (ẩn) ổn, lần 2 (hiện) lỗi: trả về giá trị lần 1 đã xác nhận.
    sw.setChecked(False)
    env.page.visible_toggled.emit(False)
    sw.setChecked(True)
    env.page.visible_toggled.emit(True)
    env.node.fail("set_visible", None, SettingsSaveError("x"))
    env.host.run_calls()
    settle(env.qtbot)
    assert sw.isChecked() is False


def test_ask_keyerror_silent_refresh(env):
    _start_with_device(env)
    before = len(ops(env.node, "read_snapshot"))
    _user_change(env, "ask")
    env.node.fail("set_ask_before_receive", KeyError(env.b.device_id))
    env.host.run_calls()
    settle(env.qtbot)
    assert notice(env.page) is None
    assert len(ops(env.node, "read_snapshot")) > before or ops(env.node, "device_settings"), (
        "KeyError: đọc lại cài đặt"
    )


def test_ask_success_saves_flag(env):
    _start_with_device(env)
    _user_change(env, "ask")
    env.host.run_calls()
    assert ops(env.node, "set_ask_before_receive") == [("set_ask_before_receive", env.b.device_id, False)]
    assert notice(env.page) is None


def test_rate_and_retention_values_reach_node(env):
    env.start()
    env.page.rate_limit_changed.emit(3 * MB)
    env.page.rate_limit_changed.emit(None)
    env.page.retention_changed.emit(30)
    env.page.retention_changed.emit(None)
    env.host.run_calls()
    assert ops(env.node, "set_rate_limit") == [("set_rate_limit", 3 * MB), ("set_rate_limit", None)]
    assert ops(env.node, "set_temp_retention_days") == [
        ("set_temp_retention_days", 30),
        ("set_temp_retention_days", None),
    ]


# =================================================================================================
# Ngôn ngữ
# =================================================================================================


def test_language_applied_only_after_save(env):
    env.start()
    env.page.language_selected.emit("vi")
    assert env.applied == [], "lưu trước, áp sau"
    env.host.run_calls()
    settle(env.qtbot)
    assert ops(env.node, "set_ui_preferences") == [("set_ui_preferences", {"language": "vi"})]
    assert env.applied == ["vi"]


def test_language_save_failure_keeps_language(env):
    from teleport.app.settings import SettingsSaveError

    env.start()
    env.node.fail("set_ui_preferences", SettingsSaveError("x"))
    env.page.language_selected.emit("vi")
    env.host.run_calls()
    settle(env.qtbot)
    assert env.applied == []
    assert env.page.language_combo.currentData() is None


def test_language_system_choice_saves_none(env):
    env.node.language = "vi"
    env.start()
    env.page.language_selected.emit(None)
    env.host.run_calls()
    settle(env.qtbot)
    assert ops(env.node, "set_ui_preferences") == [("set_ui_preferences", {"language": None})]
    assert env.applied == [None]


# =================================================================================================
# Tự khởi động
# =================================================================================================


def _run_value(env):
    from teleport.app import autostart

    return env.registry.values.get((autostart.RUN_KEY, autostart.VALUE_NAME))


def test_autostart_on_registry_then_preference(env):
    from teleport.app import autostart

    env.start()
    env.node.log.clear()
    env.page.autostart_switch.setChecked(True)
    env.page.autostart_toggled.emit(True)
    assert _run_value(env) is not None, "registry ghi ngay trên thread Qt, trước khi lưu"
    assert autostart.state(env.cfg) is autostart.AutostartState.ON
    env.host.run_calls()
    settle(env.qtbot)
    assert ops(env.node, "set_ui_preferences") == [("set_ui_preferences", {"autostart": True})]
    assert env.page.autostart_switch.isChecked()
    assert notice(env.page) is None
    env.page.autostart_switch.setChecked(False)
    env.page.autostart_toggled.emit(False)
    assert _run_value(env) is None
    env.host.run_calls()
    assert ops(env.node, "set_ui_preferences")[-1] == ("set_ui_preferences", {"autostart": False})


def test_autostart_registry_error_reverts_and_no_save(env, caplog):
    from teleport.ui.widgets import InfoLevel

    env.start()
    env.node.log.clear()
    caplog.set_level(logging.DEBUG)
    env.registry.errors["set"] = PermissionError(5, SECRET)
    env.page.autostart_switch.setChecked(True)
    env.page.autostart_toggled.emit(True)
    env.host.run_calls()
    settle(env.qtbot)
    assert not env.page.autostart_switch.isChecked(), "trả công tắc"
    assert ops(env.node, "set_ui_preferences") == [], "registry lỗi: không lưu"
    assert notice(env.page) == (InfoLevel.ERROR, AUTOSTART_WRITE_ERROR)
    assert SECRET not in caplog.text


def test_autostart_preference_save_error_only_warning(env, caplog):
    from teleport.app import autostart
    from teleport.app.settings import SettingsSaveError

    env.start()
    caplog.set_level(logging.DEBUG)
    env.node.fail("set_ui_preferences", SettingsSaveError("x"))
    env.page.autostart_switch.setChecked(True)
    env.page.autostart_toggled.emit(True)
    env.host.run_calls()
    settle(env.qtbot)
    assert autostart.state(env.cfg) is autostart.AutostartState.ON, "registry thắng: không gỡ"
    assert env.page.autostart_switch.isChecked()
    assert notice(env.page) is None or notice(env.page)[1] != SETTINGS_ERROR
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


@pytest.mark.parametrize(("saved", "registry_on"), [(False, True), (True, False)])
def test_reconcile_registry_wins_without_registry_write(env, saved, registry_on):
    from teleport.app import autostart

    if registry_on:
        autostart.enable(env.cfg)
    env.registry.calls.clear()
    env.node.autostart = saved
    env.start()
    assert env.registry.writes() == [], "đối chiếu không bao giờ ghi registry"
    assert ops(env.node, "set_ui_preferences") == [("set_ui_preferences", {"autostart": registry_on})]
    assert env.page.autostart_switch.isChecked() is registry_on


def test_reconcile_nothing_when_equal(env):
    from teleport.app import autostart

    autostart.enable(env.cfg)
    env.node.autostart = True
    env.start()
    assert ops(env.node, "set_ui_preferences") == []


def test_autostart_read_error_shows_hint_without_save(env):
    env.registry.errors["get"] = OSError(5, SECRET)
    env.node.autostart = True
    env.start()
    assert ops(env.node, "set_ui_preferences") == [], "đọc registry lỗi: không đối chiếu"
    assert not env.page.autostart_switch.isChecked()


# =================================================================================================
# Xóa toàn bộ lịch sử
# =================================================================================================


def _open_clear_box(env) -> QMessageBox:
    env.page.clear_history_requested.emit()
    settle(env.qtbot)
    found = boxes()
    assert len(found) == 1, "hộp xác nhận mở bằng open(), không chặn"
    return found[0]


def test_clear_history_box_nonblocking_default_no_escape_no(env, no_exec):
    env.start()
    box = _open_clear_box(env)
    assert no_exec == []
    assert box.windowModality() != Qt.WindowModality.ApplicationModal or box.isVisible()
    assert CLEAR_QUESTION in " ".join((box.text(), box.informativeText()))
    import sys

    if sys.platform != "darwin":
        assert box.windowTitle() == CLEAR_TITLE
    button(box, CLEAR_YES)
    no = button(box, CLEAR_NO)
    assert box.defaultButton() is no
    assert box.escapeButton() is no
    no.click()
    settle(env.qtbot)
    env.host.run_calls()
    assert ops(env.node, "clear_history") == []
    assert env.rec.cleared == []


def test_clear_history_escape_does_nothing(env):
    from PySide6.QtTest import QTest

    env.start()
    box = _open_clear_box(env)
    QTest.keyClick(box, Qt.Key.Key_Escape)
    settle(env.qtbot)
    env.host.run_calls()
    assert ops(env.node, "clear_history") == []


def test_clear_history_yes_calls_and_emits_history_cleared(env):
    from teleport.ui.widgets import InfoLevel

    env.start()
    box = _open_clear_box(env)
    button(box, CLEAR_YES).click()
    settle(env.qtbot)
    env.host.run_calls()
    settle(env.qtbot)
    assert ops(env.node, "clear_history") == [("clear_history", None)], "xóa toàn bộ, không theo máy"
    assert env.rec.cleared == [None]
    assert notice(env.page) == (InfoLevel.SUCCESS, CLEARED)


def test_clear_history_failure_notice(env):
    from teleport.ui.widgets import InfoLevel

    env.start()
    env.node.fail("clear_history", OSError(5, SECRET))
    box = _open_clear_box(env)
    button(box, CLEAR_YES).click()
    settle(env.qtbot)
    env.host.run_calls()
    settle(env.qtbot)
    assert env.rec.cleared == []
    assert notice(env.page) == (InfoLevel.ERROR, CLEAR_FAILED)


# =================================================================================================
# Xuất log
# =================================================================================================


@pytest.fixture
def export_spy(monkeypatch):
    """`settings.export_fn` giả: ghi (files, dest, thread) rồi trả `dest` (hay ném `error`)."""
    from teleport.ui import settings as ui_settings

    spy = SimpleNamespace(calls=[], error=None, gate=None)

    def fake(files, dest):
        spy.calls.append((list(files), Path(dest), threading.current_thread().name))
        if spy.gate is not None:
            spy.gate.wait(10)
        if spy.error is not None:
            raise spy.error
        return Path(dest)

    monkeypatch.setattr(ui_settings, "export_fn", fake)
    return spy


def _choose_dest(env, dest: Path) -> SaveDialog:
    env.page.export_logs_requested.emit()
    settle(env.qtbot)
    assert SaveDialog.made, "dialog_factory phải được gọi"
    dlg = SaveDialog.made[-1]
    dlg.choose(dest)
    settle(env.qtbot)
    return dlg


def test_export_dialog_open_not_exec_save_mode_default_name(env, no_exec, export_spy):
    from PySide6.QtCore import QStandardPaths

    env.start()
    env.page.export_logs_requested.emit()
    settle(env.qtbot)
    [dlg] = SaveDialog.made
    assert dlg.opened == 1 and no_exec == []
    assert dlg.acceptMode() == QFileDialog.AcceptMode.AcceptSave
    assert dlg.windowTitle() == SAVE_TITLE
    assert SAVE_FILTER in dlg.nameFilters()
    [selected] = dlg.selectedFiles()
    assert re.fullmatch(r"teleport-log-\d{8}-\d{6}\.zip", Path(selected).name), selected
    docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    expected = Path(docs) if docs and Path(docs).is_dir() else Path.home()
    assert Path(selected).parent == expected
    env.page.export_logs_requested.emit()  # bấm lần nữa khi hộp đang mở: không dựng hộp thứ hai
    settle(env.qtbot)
    assert len(SaveDialog.made) == 1
    assert export_spy.calls == []


def test_export_runs_off_qt_and_node_threads(env, export_spy, tmp_path):
    env.start()
    dest = tmp_path / "out" / "log.zip"
    dest.parent.mkdir()
    _choose_dest(env, dest)
    env.qtbot.waitUntil(lambda: bool(export_spy.calls), timeout=5000)
    _files, got_dest, thread = export_spy.calls[0]
    assert got_dest == dest
    assert thread != threading.main_thread().name
    assert thread.startswith("teleport-logexport")
    assert thread not in env.node.threads


def test_export_success_notice_opens_parent_folder(env, export_spy, tmp_path):
    from teleport.ui.send import printable
    from teleport.ui.widgets import InfoLevel

    env.start()
    dest = tmp_path / "Thư mục log" / "log.zip"
    dest.parent.mkdir()
    _choose_dest(env, dest)
    env.qtbot.waitUntil(lambda: notice(env.page) is not None, timeout=5000)
    level, text = notice(env.page)
    assert level is InfoLevel.SUCCESS
    assert EXPORT_SAVED.format(path=printable(str(dest))) in text
    assert env.page.export_button.isEnabled()
    [action] = [b for b in env.page.info_bar.findChildren(QAbstractButton) if plain(b.text()) == OPEN_FOLDER]
    action.click()
    settle(env.qtbot)
    assert [url_path(u) for u in env.urls] == [dest.parent]


def test_export_button_disabled_while_running(env, export_spy, tmp_path):
    env.start()
    export_spy.gate = threading.Event()
    dest = tmp_path / "log.zip"
    _choose_dest(env, dest)
    env.qtbot.waitUntil(lambda: bool(export_spy.calls), timeout=5000)
    assert not env.page.export_button.isEnabled()
    export_spy.gate.set()
    env.qtbot.waitUntil(lambda: env.page.export_button.isEnabled(), timeout=5000)


@pytest.mark.parametrize("kind", ["oserror", "unexpected"])
def test_export_oserror_and_unexpected_texts(env, export_spy, tmp_path, kind, caplog):
    from teleport.ui.send import printable
    from teleport.ui.widgets import InfoLevel

    env.start()
    caplog.set_level(logging.DEBUG)
    export_spy.error = PermissionError(13, SECRET) if kind == "oserror" else ValueError(SECRET)
    dest = tmp_path / "khóa" / "log.zip"
    dest.parent.mkdir()
    _choose_dest(env, dest)
    env.qtbot.waitUntil(lambda: notice(env.page) is not None, timeout=5000)
    level, text = notice(env.page)
    assert level is InfoLevel.ERROR
    if kind == "oserror":
        assert EXPORT_WRITE_ERROR.format(path=printable(str(dest))) in text
    else:
        assert EXPORT_OTHER_ERROR in text
    assert SECRET not in caplog.text
    assert env.page.export_button.isEnabled()


def test_export_result_after_close_all_ignored(env, export_spy, tmp_path):
    env.start()
    export_spy.gate = threading.Event()
    _choose_dest(env, tmp_path / "log.zip")
    env.qtbot.waitUntil(lambda: bool(export_spy.calls), timeout=5000)
    env.c.close_all()
    export_spy.gate.set()
    settle(env.qtbot, 300)
    assert notice(env.page) is None, "kết quả xuất log sau close_all bị bỏ"


def test_export_log_files_order_rotated_first(env, export_spy, tmp_path):
    env.start()
    _choose_dest(env, tmp_path / "log.zip")
    env.qtbot.waitUntil(lambda: bool(export_spy.calls), timeout=5000)
    files, _dest, _thread = export_spy.calls[0]
    assert [Path(f) for f in files] == env.logs


def test_export_cancelled_dialog_does_nothing(env, export_spy):
    env.start()
    env.page.export_logs_requested.emit()
    settle(env.qtbot)
    SaveDialog.made[-1].reject()
    settle(env.qtbot, 100)
    assert export_spy.calls == []
    assert notice(env.page) is None


def test_shutdown_does_not_wait_for_running_export(env, export_spy, tmp_path):
    import time

    env.start()
    export_spy.gate = threading.Event()
    _choose_dest(env, tmp_path / "log.zip")
    env.qtbot.waitUntil(lambda: bool(export_spy.calls), timeout=5000)
    t0 = time.monotonic()
    env.c.shutdown()
    assert time.monotonic() - t0 < 1.0, "shutdown không chờ việc xuất log đang chạy"
    export_spy.gate.set()


def test_logs_never_contain_paths_or_names(env, export_spy, tmp_path, caplog):
    from teleport.app.settings import SettingsSaveError

    caplog.set_level(logging.DEBUG)
    env.node.name = "Máy-Riêng-Tư"
    _start_with_device(env)
    env.node.fail("set_name", SettingsSaveError(SECRET))
    env.page.name_submitted.emit("Tên-Bí-Mật")
    env.node.fail("set_visible", ZeroDivisionError(SECRET))
    env.page.visible_toggled.emit(False)
    env.host.run_calls()
    env.registry.errors["set"] = PermissionError(5, SECRET)
    env.page.autostart_toggled.emit(True)
    export_spy.error = PermissionError(13, SECRET)
    dest = tmp_path / "Nguyễn" / "log.zip"
    dest.parent.mkdir()
    _choose_dest(env, dest)
    env.qtbot.waitUntil(lambda: bool(export_spy.calls), timeout=5000)
    settle(env.qtbot, 200)
    text = caplog.text
    for needle in (SECRET, "Nguyễn", "Tên-Bí-Mật", "Máy-Riêng-Tư", str(tmp_path), "May-B"):
        assert needle not in text, f"log lộ {needle!r}"


def test_default_export_dir_falls_back_to_home_when_documents_missing(monkeypatch, tmp_path):
    """Documents chưa được tạo (máy Linux mới): hộp xuất log mở ở thư mục nhà."""
    from teleport.ui import settings as settings_mod

    missing = tmp_path / "Documents"
    monkeypatch.setattr(settings_mod.QStandardPaths, "writableLocation", lambda _loc: str(missing))
    assert settings_mod.default_export_dir() == str(Path.home())
    missing.mkdir()
    assert settings_mod.default_export_dir() == str(missing)
