"""`teleport.ui.errorlog`: lỗi không bắt được vào `teleport.log` mà không có nội dung lỗi (mvp N9).

Mỗi lỗi chỉ được ghi tên loại và traceback (`traceback.format_tb`), không bao giờ `str(e)`: thông điệp
lỗi có thể chứa đường dẫn, tên file của người dùng hay dữ liệu máy khác gửi tới.

Lưu ý khi viết test ở đây:

- Chuỗi bí mật nằm trong hằng ở mức module; dòng `raise` chỉ nhắc **tên** hằng. Traceback có in dòng
  mã nguồn, nên nếu viết chuỗi bí mật ngay trong dòng `raise` thì nó vào log hợp lệ và test sai.
- Hook được cài **trong thân test** (fixture `hooks`), không ở fixture setup: pytest-qt vá
  `sys.excepthook` trong pha call, và test slot Qt dùng `qt_no_exception_capture` để lỗi đi tới hook
  của `errorlog` thật.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import io
import logging
import ssl
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

integration = pytest.mark.integration

SECRET = "C:/Users/x/bi-mat-tai-lieu-7c1e.txt"
SECRET_2 = "noi-dung-clipboard-bi-mat-42ab"
SECRET_3 = "/home/u/thu-muc-rieng-9d0f/anh.png"
SHORT_SECRET = "bi-mat-ngan-31"  # đủ ngắn để reprlib của asyncio không cắt
ERRORLOG_LOGGER = "teleport.ui.errorlog"


class TeleportTestError(Exception):
    """Loại lỗi không dựng sẵn: tên ghi ra phải có module."""


def _is_errorlog_hook(hook) -> bool:
    return getattr(hook, "__module__", None) == ERRORLOG_LOGGER


def _error_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if " ERROR " in line]


def _assert_no_secret(text: str, *secrets: str) -> None:
    for s in secrets or (SECRET, SECRET_2, SECRET_3, SHORT_SECRET):
        assert s not in text, f"log chứa nội dung lỗi {s!r}:\n{text}"


# =================================================================================================
# Fixture
# =================================================================================================


@pytest.fixture
def file_log(tmp_path):
    """File log gắn bằng `_attach_file_log(tmp_path)` như app thật; gỡ và đóng khi test xong."""
    from teleport.ui import app as ui_app

    handler = ui_app._attach_file_log(tmp_path)
    assert handler is not None, "không gắn được file log"
    path = tmp_path / ui_app.LOG_DIR / ui_app.LOG_FILE

    def read() -> str:
        handler.flush()
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    try:
        yield SimpleNamespace(path=path, handler=handler, read=read)
    finally:
        ui_app._detach_file_log(handler)


@pytest.fixture
def hooks():
    """`install() -> restore`: cài hook của `errorlog`; khi test xong luôn khôi phục hook cũ."""
    from teleport.ui import errorlog

    saved_sys, saved_thread = sys.excepthook, threading.excepthook
    restores: list = []

    def install():
        restore = errorlog.install_excepthooks()
        restores.append(restore)
        return restore

    yield install

    for restore in reversed(restores):
        with contextlib.suppress(Exception):
            restore()
    if _is_errorlog_hook(sys.excepthook):
        sys.excepthook = saved_sys
    if _is_errorlog_hook(threading.excepthook):
        threading.excepthook = saved_thread


@pytest.fixture
def own_logger():
    """Logger riêng của test (lan lên gốc, nơi có file log); gỡ handler thêm vào khi xong."""
    logger = logging.getLogger("teleport.ui.test_errorlog")
    added: list[logging.Handler] = []

    def add(handler: logging.Handler) -> None:
        logger.addHandler(handler)
        added.append(handler)

    yield SimpleNamespace(logger=logger, add=add)
    for h in added:
        logger.removeHandler(h)
        h.close()


# =================================================================================================
# 1. Slot Qt ném lỗi → ERROR trong teleport.log, không có nội dung
# =================================================================================================


def _make_emitter():
    from PySide6.QtCore import QObject, Signal

    class Emitter(QObject):
        fired = Signal()

    return Emitter()


def slot_that_leaks_path() -> None:
    raise ValueError(SECRET)


@pytest.mark.qt_no_exception_capture
def test_uncaught_error_in_qt_slot_logged_as_type_and_traceback_without_message(qtbot, file_log, hooks):
    from PySide6.QtCore import Qt

    calls: list[str] = []

    def slot() -> None:
        calls.append("slot")
        slot_that_leaks_path()

    emitter = _make_emitter()
    emitter.fired.connect(slot, Qt.ConnectionType.QueuedConnection)  # chạy trong vòng sự kiện Qt

    hooks()
    assert _is_errorlog_hook(sys.excepthook), "hook của errorlog không phải sys.excepthook lúc phát signal"
    emitter.fired.emit()
    qtbot.waitUntil(lambda: bool(calls) and "ValueError" in file_log.read(), timeout=5000)

    text = file_log.read()
    errors = _error_lines(text)
    assert any("ValueError" in line for line in errors), f"không có dòng ERROR có ValueError:\n{text}"
    assert any(ERRORLOG_LOGGER in line for line in errors), "dòng ERROR không đến từ hook của errorlog"
    assert "slot_that_leaks_path" in text, "thiếu traceback (tên hàm slot)"
    assert "test_errorlog.py" in text, "thiếu traceback (file nguồn)"
    _assert_no_secret(text)


# =================================================================================================
# 2. Lỗi trong threading.Thread
# =================================================================================================


def worker_that_leaks_clipboard() -> None:
    raise TeleportTestError(SECRET_2)


def test_uncaught_error_in_thread_logs_thread_name_and_type_without_message(file_log, hooks):
    hooks()
    t = threading.Thread(target=worker_that_leaks_clipboard, name="tl-worker-kiem-thu")
    t.start()
    t.join(10)
    assert not t.is_alive()

    text = file_log.read()
    errors = _error_lines(text)
    expected_type = f"{__name__}.TeleportTestError"
    assert any("tl-worker-kiem-thu" in line for line in errors), f"thiếu tên thread:\n{text}"
    assert expected_type in text, f"thiếu tên loại lỗi đầy đủ {expected_type!r}:\n{text}"
    assert "worker_that_leaks_clipboard" in text, "thiếu traceback"
    _assert_no_secret(text)


def worker_exits() -> None:
    raise SystemExit(SECRET)


def test_system_exit_in_thread_is_ignored(file_log, hooks):
    hooks()
    t = threading.Thread(target=worker_exits, name="tl-thoat")
    t.start()
    t.join(10)
    text = file_log.read()
    assert _error_lines(text) == [], text
    _assert_no_secret(text)


def test_sys_hook_called_directly_logs_without_message(file_log, hooks):
    """Lỗi không bắt được ở thread chính (Python gọi `sys.excepthook`)."""
    hooks()
    try:
        slot_that_leaks_path()
    except ValueError as e:
        sys.excepthook(type(e), e, e.__traceback__)
    text = file_log.read()
    assert any("ValueError" in line for line in _error_lines(text)), text
    assert "slot_that_leaks_path" in text
    _assert_no_secret(text)


# =================================================================================================
# 3. log.exception và RedactingFormatter
# =================================================================================================


def open_private_file(path: Path) -> None:
    path.read_bytes()


def test_log_exception_through_file_handler_has_no_message(file_log, own_logger, tmp_path):
    secret_path = tmp_path / "bi-mat-khong-ton-tai-5e2d.bin"
    try:
        open_private_file(secret_path)
    except OSError:
        own_logger.logger.exception("không đọc được file")
    text = file_log.read()
    assert "FileNotFoundError" in text, text
    assert "open_private_file" in text, "thiếu traceback"
    assert "bi-mat-khong-ton-tai-5e2d" not in text, text


def raise_chained() -> None:
    try:
        raise OSError(SECRET_2)
    except OSError as inner:
        raise RuntimeError(SECRET) from inner


def raise_during_handling() -> None:
    try:
        raise KeyError(SECRET_3)
    except KeyError:
        raise TeleportTestError(SECRET_2)  # noqa: B904 - kiểm chuỗi __context__


def test_explicit_cause_chain_has_both_types_and_no_message(file_log, own_logger):
    try:
        raise_chained()
    except RuntimeError:
        own_logger.logger.exception("chuỗi lỗi")
    text = file_log.read()
    assert "RuntimeError" in text and "OSError" in text, text
    assert text.index("OSError") < text.rindex("RuntimeError"), "lỗi gốc phải in trước như Python"
    _assert_no_secret(text)


def test_implicit_context_chain_has_both_types_and_no_message(file_log, own_logger):
    try:
        raise_during_handling()
    except TeleportTestError:
        own_logger.logger.exception("chuỗi lỗi ngầm")
    text = file_log.read()
    assert "KeyError" in text and f"{__name__}.TeleportTestError" in text, text
    _assert_no_secret(text)


def test_file_has_no_message_when_another_handler_formats_record_first(file_log, own_logger):
    """Handler khác (Formatter thường) định dạng trước → `record.exc_text` chứa đủ nội dung; file
    log vẫn không được dùng bản lưu đó."""
    plain = io.StringIO()
    other = logging.StreamHandler(plain)
    other.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    own_logger.add(other)  # handler của logger con chạy trước handler của logger gốc

    try:
        raise_chained()
    except RuntimeError:
        own_logger.logger.exception("định dạng trước")

    assert SECRET in plain.getvalue(), "đối chứng: Formatter thường có ghi nội dung lỗi"
    text = file_log.read()
    assert "RuntimeError" in text and "OSError" in text, text
    _assert_no_secret(text)


def test_redacting_formatter_ignores_and_restores_cached_exc_text():
    from teleport.ui.errorlog import RedactingFormatter

    try:
        slot_that_leaks_path()
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", None, sys.exc_info())
    cached = f"Traceback ...\nValueError: {SECRET}"
    record.exc_text = cached

    out = RedactingFormatter("%(levelname)s %(message)s").format(record)

    assert SECRET not in out, out
    assert "ValueError" in out and "slot_that_leaks_path" in out, out
    assert record.exc_text == cached, "phải trả lại exc_text cũ cho handler sau"


def test_redacting_formatter_without_exception_formats_message_only():
    from teleport.ui.errorlog import RedactingFormatter

    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "chỉ có lời %s", ("nhắn",), None)
    assert RedactingFormatter("%(levelname)s %(message)s").format(record) == "WARNING chỉ có lời nhắn"


def test_attach_file_log_uses_redacting_formatter(file_log):
    from teleport.ui.errorlog import RedactingFormatter

    assert isinstance(file_log.handler.formatter, RedactingFormatter)


# =================================================================================================
# 4. Handler lỗi của loop asyncio
# =================================================================================================


def callback_that_leaks(arg: str) -> None:
    raise TeleportTestError(arg)


async def task_that_leaks() -> None:
    raise ValueError(SECRET_3)


def _run_loop_with_handler(body) -> list[dict]:
    """Chạy `body(loop)` trên loop mới có `loop_exception_handler`; trả các context đã nhận."""
    from teleport.ui.errorlog import loop_exception_handler

    contexts: list[dict] = []

    def handler(loop, context):
        contexts.append(dict(context))
        loop_exception_handler(loop, context)

    loop = asyncio.new_event_loop()
    try:
        loop.set_exception_handler(handler)
        body(loop)
    finally:
        loop.close()
    return contexts


def test_loop_handler_callback_error_has_no_message_or_repr(file_log):
    def body(loop):
        loop.set_debug(True)  # chế độ debug: asyncio đưa repr tham số của callback vào `message`
        loop.slow_callback_duration = 3600  # không để cảnh báo “callback chậm” (có repr) chen vào log
        loop.call_soon(callback_that_leaks, SHORT_SECRET)
        loop.call_soon(loop.stop)
        loop.run_forever()

    contexts = _run_loop_with_handler(body)
    assert contexts and SHORT_SECRET in contexts[0]["message"], "đối chứng: message của asyncio có tham số"
    text = file_log.read()
    assert any(f"{__name__}.TeleportTestError" in line for line in _error_lines(text)), text
    assert "callback_that_leaks" in text, "thiếu traceback"
    _assert_no_secret(text, SHORT_SECRET)


def test_loop_handler_unretrieved_task_exception_has_no_message(file_log):
    def body(loop):
        task = loop.create_task(task_that_leaks())
        loop.run_until_complete(asyncio.wait([task]))
        del task
        gc.collect()

    contexts = _run_loop_with_handler(body)
    assert contexts, "đối chứng: asyncio không báo task bị bỏ lỗi"
    text = file_log.read()
    assert "ValueError" in text, text
    assert "task_that_leaks" in text, "thiếu traceback"
    _assert_no_secret(text)


def test_loop_handler_direct_context_with_exception(file_log):
    from teleport.ui.errorlog import loop_exception_handler

    try:
        callback_that_leaks(SECRET_2)
    except TeleportTestError as e:
        exc = e
    loop = asyncio.new_event_loop()
    try:
        loop_exception_handler(loop, {"message": f"lỗi với {SECRET}", "exception": exc, "future": SECRET_3})
    finally:
        loop.close()
    text = file_log.read()
    assert any(f"{__name__}.TeleportTestError" in line for line in _error_lines(text)), text
    _assert_no_secret(text)


def test_loop_handler_context_without_exception_logs_error_without_message(file_log):
    from teleport.ui.errorlog import loop_exception_handler

    loop = asyncio.new_event_loop()
    try:
        loop_exception_handler(loop, {"message": SECRET, "handle": SECRET_2})
    finally:
        loop.close()
    text = file_log.read()
    assert _error_lines(text), "phải vẫn có một dòng ERROR"
    _assert_no_secret(text)


# =================================================================================================
# 5. Khôi phục hook, KeyboardInterrupt
# =================================================================================================


def test_restore_puts_back_previous_hooks(monkeypatch, hooks):
    def old_sys(*a):
        pass

    def old_thread(args):
        pass

    monkeypatch.setattr(sys, "excepthook", old_sys)
    monkeypatch.setattr(threading, "excepthook", old_thread)
    restore = hooks()
    assert _is_errorlog_hook(sys.excepthook) and _is_errorlog_hook(threading.excepthook)
    restore()
    assert sys.excepthook is old_sys
    assert threading.excepthook is old_thread
    restore()  # gọi lần hai không đổi gì
    assert sys.excepthook is old_sys and threading.excepthook is old_thread


def test_restore_keeps_hooks_installed_later_by_someone_else(monkeypatch, hooks):
    monkeypatch.setattr(sys, "excepthook", lambda *a: None)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)
    restore = hooks()

    def newer_sys(*a):
        pass

    def newer_thread(args):
        pass

    sys.excepthook = newer_sys
    threading.excepthook = newer_thread
    restore()
    assert sys.excepthook is newer_sys
    assert threading.excepthook is newer_thread


def test_keyboard_interrupt_goes_to_previous_hook_and_is_not_logged(monkeypatch, file_log, hooks):
    seen: list = []
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: seen.append((t, e)))
    hooks()
    exc = KeyboardInterrupt(SECRET)
    sys.excepthook(KeyboardInterrupt, exc, None)
    assert seen == [(KeyboardInterrupt, exc)]
    assert _error_lines(file_log.read()) == []


def test_ordinary_error_does_not_reach_previous_hook(monkeypatch, file_log, hooks):
    """Hook cũ (mặc định in đủ `str(e)` ra stderr) không được gọi cho lỗi thường."""
    seen: list = []
    monkeypatch.setattr(sys, "excepthook", lambda t, e, tb: seen.append(t))
    hooks()
    exc = ValueError(SECRET)
    sys.excepthook(ValueError, exc, None)
    assert seen == []
    assert any("ValueError" in line for line in _error_lines(file_log.read()))


# =================================================================================================
# error_name
# =================================================================================================


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        (ValueError, "ValueError"),
        (KeyboardInterrupt, "KeyboardInterrupt"),
        (ssl.SSLError, "ssl.SSLError"),
        (TeleportTestError, f"{__name__}.TeleportTestError"),
    ],
)
def test_error_name(kind, name):
    from teleport.ui.errorlog import error_name

    assert error_name(kind) == name


def test_error_name_uses_qualname_for_nested_class():
    from teleport.ui.errorlog import error_name

    class Inner(Exception):
        pass

    assert error_name(Inner) == f"{__name__}.{Inner.__qualname__}"
    assert "<locals>" in error_name(Inner)


# =================================================================================================
# Nối dây: NodeHost và main()
# =================================================================================================


@integration
def test_node_host_loop_errors_go_through_redacting_handler(
    qtbot, file_log, node_hosts, node_options, config_dir
):
    host = node_hosts(node_options(config_dir("A")))
    with qtbot.waitSignal(host.started, timeout=20000):
        host.start()

    def schedule(node):
        asyncio.get_running_loop().call_soon(callback_that_leaks, SECRET)

    host.call(schedule)
    qtbot.waitUntil(lambda: "TeleportTestError" in file_log.read(), timeout=10000)
    text = file_log.read()
    assert "callback_that_leaks" in text
    _assert_no_secret(text)


@integration
@pytest.mark.qt_no_exception_capture
def test_main_installs_hooks_and_restores_them_on_exit(gui, gui_wait, config_dir, ui_helpers, tmp_path):
    from PySide6.QtCore import QTimer

    a = config_dir("A")
    log_file = a / "logs" / "teleport.log"
    before = sys.excepthook
    seen: dict = {}

    def read() -> str:
        for h in logging.getLogger().handlers:
            with contextlib.suppress(Exception):
                h.flush()
        return log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""

    def script(run):
        yield gui_wait(lambda: bool(run.hosts) and run.hosts[-1].running, 15, "node chạy")
        seen["hook"] = sys.excepthook
        QTimer.singleShot(0, slot_that_leaks_path)
        yield gui_wait(lambda: "slot_that_leaks_path" in read(), 10, "lỗi slot vào teleport.log")
        [quit_action] = [
            x for x in run.tray.actions() if not x.isSeparator() and ui_helpers.plain(x.text()) == "Thoát"
        ]
        quit_action.trigger()

    run = gui(["--config-dir", str(a)], script)
    assert run.code == 0
    assert _is_errorlog_hook(seen["hook"]), "main không cài sys.excepthook của errorlog"
    assert sys.excepthook is before, "main không khôi phục sys.excepthook khi trả về"
    text = log_file.read_text(encoding="utf-8", errors="replace")
    assert any("ValueError" in line for line in _error_lines(text)), text
    _assert_no_secret(text)


@integration
def test_error_escaping_main_is_logged_before_hooks_are_removed(qapp, config_dir, monkeypatch):
    """Lỗi lan ra khỏi `main` (vd. lúc dựng `GuiApp`) vẫn vào `teleport.log`, không kèm nội dung."""
    from teleport.ui import app as ui_app

    a = config_dir("A")
    before = sys.excepthook

    def broken_init(self, *args, **kwargs):
        raise TeleportTestError(SECRET)

    monkeypatch.setattr(ui_app.GuiApp, "__init__", broken_init)
    with pytest.raises(TeleportTestError):
        ui_app.main(["--config-dir", str(a)])
    assert sys.excepthook is before
    text = (a / ui_app.LOG_DIR / ui_app.LOG_FILE).read_text(encoding="utf-8", errors="replace")
    assert any("TeleportTestError" in line for line in _error_lines(text)), text
    assert "broken_init" in text
    _assert_no_secret(text)
