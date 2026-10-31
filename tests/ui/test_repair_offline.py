"""Nút “Ghép đôi lại” khi không thấy máy kia trong mạng (spec-fix-relink, Lỗi 2: D8–D11, U1–U4; đích đổi
theo spec B2 mục 2.7 và 12.30, W11).

Viết từ spec, không từ code. Trước đây nút nối thẳng vào `pair_device`, mà `pair_device` trả False im
lặng khi thiết bị offline / không có địa chỉ (máy ghép bằng IP, mDNS bị chặn) → bấm không có gì xảy ra.
Đặc tả mới:

- không bận, có host, `pair_device` không bắt đầu được → mở trang “Tìm máy mới” (DISCOVER), câu mới có
  tên thiết bị (`ui.pairing.repair_offline_text`) hiện ở thanh thông báo của trang (`info_bar`, mức INFO),
  ô nhập IP (`discover.address_edit`) có tiêu điểm, không điền sẵn IP (D8–D10; B2 2.7, W11);
- thiết bị online có địa chỉ → ghép đôi như cũ (hộp tiến độ);
- đang có lượt ghép khác → câu “đang bận” (`busy_text`), vẫn ở HOME (D11);
- không có host → không làm gì, không lỗi (D11).

`GuiApp` dựng trực tiếp (như `test_pair_then_send.py`), `PairingController` và `TransfersController`
dùng chung một host giả; `GuiApp.host` được gán host giả đó (điều kiện “có host” của D8 sau hoi-lai).
Tiêu điểm: cửa sổ hiện + `activateWindow`, rồi `QApplication.focusWidget()`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal

from teleport.app.devices import DeviceView

B_ID = "b" * 64
B_NAME = "May-B"
LOCAL = "127.0.0.1"
REPAIR = "Ghép đôi lại"  # nút của mvp mục 8 “Máy kia từ chối máy này”


def _view(*, online: bool, addresses: tuple[str, ...], port: int | None) -> DeviceView:
    return DeviceView(
        device_id=B_ID,
        name=B_NAME,
        os="windows",
        paired=True,
        online=online,
        connected=False,
        addresses=addresses,
        port=port,
    )


def offline_view() -> DeviceView:
    return _view(online=False, addresses=(), port=None)


def online_view() -> DeviceView:
    return _view(online=True, addresses=(LOCAL,), port=47474)


class _Job:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeHost(QObject):
    """Phần `NodeHost` mà hai controller dùng; không chạy gì, chỉ ghi lại `call` / `submit`."""

    pairing_prompt = Signal(object)
    pairing_finished = Signal(object)

    def __init__(self, receive_dir: Path) -> None:
        super().__init__()
        self.options = SimpleNamespace(receive_dir=receive_dir)
        self.calls: list = []
        self.submits: list = []

    def call(self, fn, on_done=None) -> None:
        self.calls.append((fn, on_done))

    def submit(self, fn, on_done=None):
        job = _Job()
        self.submits.append((fn, on_done, job))
        return job


def _make_app(qapp, tmp_path: Path, *, with_host: bool):
    from teleport.ui import app as ui_app

    gui_app = ui_app.GuiApp(qapp, tmp_path / "cfg")
    host = None
    if with_host:
        host = FakeHost(tmp_path / "nhan")
        gui_app.transfers.attach(host)
        gui_app.pairing.set_host(host)
        gui_app.host = host  # điều kiện “có host” của slot là `GuiApp.host is not None`
    window = gui_app.window
    window.show_running("May-A", tmp_path / "nhan")
    window.resize(900, 600)
    window.show()
    window.activateWindow()
    qapp.processEvents()
    return gui_app, host


def _dispose(qapp, gui_app) -> None:
    gui_app.pairing.close_all()
    gui_app.transfers.close_all()
    gui_app.tray.dispose()
    gui_app.window.hide()
    gui_app.window.deleteLater()
    qapp.processEvents()


@pytest.fixture
def env(qapp, tmp_path):
    gui_app, host = _make_app(qapp, tmp_path, with_host=True)
    yield SimpleNamespace(app=gui_app, host=host, window=gui_app.window, tmp=tmp_path)
    _dispose(qapp, gui_app)


@pytest.fixture
def env_no_host(qapp, tmp_path):
    gui_app, _ = _make_app(qapp, tmp_path, with_host=False)
    yield SimpleNamespace(app=gui_app, host=None, window=gui_app.window, tmp=tmp_path)
    _dispose(qapp, gui_app)


def _notice(env) -> str:
    return env.app.transfers.view.notice_text() or ""


def _rejected_notice_button(env, view: DeviceView):
    """Thiết bị đã ghép bị REJECTED → thả file → dải thông báo có nút “Ghép đôi lại”; trả nút đó."""
    from PySide6.QtWidgets import QPushButton

    from teleport.app.node import ConnectFailed
    from teleport.core.links import ConnectResult

    env.window.devices.reset((view,))
    env.window.devices.apply(ConnectFailed(view, ConnectResult.REJECTED))
    env.app.transfers.request_send(B_ID, [env.tmp / "nguon" / "a.bin"])
    tview = env.app.transfers.view
    assert tview.notice_action_text() == REPAIR, (
        f"dải thông báo REJECTED phải có nút “{REPAIR}”, thấy {tview.notice_action_text()!r}"
    )
    found = [
        b
        for b in tview.findChildren(QPushButton)
        if b.text() == REPAIR and b.isVisibleTo(tview) and not b.isHidden()
    ]
    assert len(found) == 1, f"cần đúng một nút “{REPAIR}”, thấy {len(found)}"
    return found[0]


def _at_home(env) -> bool:
    from teleport.ui.main_window import Page

    return env.window.current_page is Page.HOME


def _pair_jobs(env) -> list:
    return list(env.host.submits) if env.host is not None else []


# =================================================================================================
# U1 (W11): offline, không địa chỉ → trang “Tìm máy mới”, câu ở thanh thông báo, tiêu điểm ô nhập IP
# =================================================================================================


def test_u1_repair_offline_device_opens_discover_with_notice_and_focused_ip_entry(env, qtbot):
    from PySide6.QtWidgets import QApplication

    from teleport.ui import pairing as pairing_mod
    from teleport.ui.main_window import Page
    from teleport.ui.widgets import InfoLevel

    window = env.window
    assert window.current_page is Page.HOME
    button = _rejected_notice_button(env, offline_view())
    button.click()
    QApplication.processEvents()

    # Không bắt đầu lượt ghép nào.
    assert not env.app.pairing.busy
    assert env.app.pairing.progress_box is None
    assert _pair_jobs(env) == []

    # Trang “Tìm máy mới”, câu mới có tên thiết bị và mời nhập IP ở thanh thông báo (D8, D9; B2 2.7).
    assert window.current_page is Page.DISCOVER
    bar = window.discover.info_bar
    expected = pairing_mod.repair_offline_text(B_NAME)
    assert B_NAME in expected and "IP" in expected
    assert bar.isVisibleTo(window), "12.16: mở trang trước rồi mới hiện thanh thông báo"
    assert expected in (bar.title, bar.text), (bar.title, bar.text)
    assert bar.level is InfoLevel.INFO

    # Ô nhập IP rỗng (D10) và có tiêu điểm.
    edit = window.discover.address_edit
    assert edit.text() == "", "không điền sẵn IP (D10)"
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is edit, timeout=2000)


# =================================================================================================
# U2: online có địa chỉ → ghép đôi như cũ
# =================================================================================================


def test_u2_repair_online_device_starts_pairing_as_before(env):
    from PySide6.QtWidgets import QApplication

    button = _rejected_notice_button(env, online_view())
    button.click()
    QApplication.processEvents()
    assert env.app.pairing.busy, "thiết bị online có địa chỉ: phải bắt đầu ghép đôi"
    box = env.app.pairing.progress_box
    assert box is not None and box.isVisible()
    assert B_NAME in box.text()
    assert len(_pair_jobs(env)) == 1
    assert _at_home(env), "đang ghép đôi thì không mở trang nhập IP"
    assert "IP" not in _notice(env), "đang ghép đôi thì không mời nhập IP"


# =================================================================================================
# U3: đang có lượt ghép khác → câu “đang bận”, không mở khung nhập IP
# =================================================================================================


@pytest.mark.parametrize("make_view", [offline_view, online_view], ids=["offline", "online"])
def test_u3_repair_while_busy_shows_busy_text_and_keeps_ip_entry_closed(env, make_view):
    from PySide6.QtWidgets import QApplication

    from teleport.ui import pairing as pairing_mod

    view = make_view()
    button = _rejected_notice_button(env, view)
    assert env.app.pairing.pair_address("192.168.1.9", 47474), "lượt ghép khác (nhập IP) đang chạy"
    assert env.app.pairing.busy
    jobs_before = len(_pair_jobs(env))
    messages: list[str] = []
    env.app.pairing.message.connect(messages.append)
    button.click()
    QApplication.processEvents()

    busy = pairing_mod.busy_text()
    box = env.app.pairing.result_box
    shown = (box.text() if box is not None and box.isVisible() else "", *messages, _notice(env))
    assert busy in shown, f"đang bận phải hiện câu “đang bận”, thấy {shown!r}"
    assert len(_pair_jobs(env)) == jobs_before, "không bắt đầu lượt ghép thứ hai"
    assert _at_home(env), "đang bận thì không mở trang nhập IP"
    assert "IP" not in _notice(env), "đang bận thì không mời nhập IP"


# =================================================================================================
# U4: không có host → không làm gì
# =================================================================================================


@pytest.mark.parametrize("make_view", [offline_view, online_view], ids=["offline", "online"])
def test_u4_repair_without_host_does_nothing(env_no_host, make_view, ui_helpers):
    from PySide6.QtWidgets import QApplication

    env = env_no_host
    view = make_view()
    env.window.devices.reset((view,))
    before = _notice(env)
    env.app.transfers.repair_requested.emit(view)  # không lỗi
    QApplication.processEvents()
    assert not env.app.pairing.busy
    assert env.app.pairing.progress_box is None
    box = env.app.pairing.result_box
    assert box is None or not box.isVisible()
    assert ui_helpers.visible_message_boxes() == []
    assert _at_home(env)
    assert _notice(env) == before


# =================================================================================================
# “Ghép rồi gửi” khi đang bận với thiết bị offline: cũng không được im lặng
# =================================================================================================


def test_pair_then_send_while_busy_with_offline_device_shows_busy_text(env):
    import dataclasses

    from PySide6.QtWidgets import QApplication

    from teleport.ui import pairing as pairing_mod

    view = dataclasses.replace(offline_view(), paired=False)
    assert env.app.pairing.pair_address("192.168.1.9", 47474), "lượt ghép khác (nhập IP) đang chạy"
    jobs_before = len(_pair_jobs(env))
    messages: list[str] = []
    env.app.pairing.message.connect(messages.append)
    env.app.transfers.pair_needed.emit(view)
    QApplication.processEvents()

    box = env.app.pairing.result_box
    shown = (box.text() if box is not None and box.isVisible() else "", *messages, _notice(env))
    assert pairing_mod.busy_text() in shown, f"đang bận phải hiện câu “đang bận”, thấy {shown!r}"
    assert len(_pair_jobs(env)) == jobs_before, "không bắt đầu lượt ghép thứ hai"
    assert env.app.transfers.pending is None, "không ghép được thì bỏ việc chờ gửi"
