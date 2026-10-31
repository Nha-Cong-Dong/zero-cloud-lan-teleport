"""Nối dây ghép đôi trong `GuiApp` (PR2): signal của trang “Tìm máy mới” và danh sách cuộc trò chuyện →
`PairingController`, `devices_changed` → `window.devices` (spec B2 mục 2.1, 12.28)."""

from __future__ import annotations

from teleport.app.devices import DeviceView
from teleport.ui import app as ui_app
from teleport.ui.pairing import PairingController

DEVICE_ID = "a" * 64


def _view(*, paired: bool = False, name: str = "May-B") -> DeviceView:
    return DeviceView(
        device_id=DEVICE_ID,
        name=name,
        os="windows",
        paired=paired,
        online=True,
        connected=False,
        addresses=("127.0.0.1",),
        port=47474,
    )


def test_discover_and_conversation_signals_reach_pairing_controller(qapp, monkeypatch, tmp_path):
    calls: list[tuple] = []
    monkeypatch.setattr(PairingController, "pair_device", lambda self, v: calls.append(("pair", v)) or True)
    monkeypatch.setattr(
        PairingController, "pair_address", lambda self, ip, port: calls.append(("addr", ip, port)) or True
    )
    monkeypatch.setattr(
        PairingController, "request_unpair", lambda self, i, n: calls.append(("unpair", i, n))
    )
    gui = ui_app.GuiApp(qapp, tmp_path)
    try:
        window = gui.window
        view = _view(paired=True)
        window.devices.reset((view,))
        window.discover.pair_requested.emit(view)
        window.discover.manual_address_entered.emit("192.168.1.5", 47474)
        window.conversations.unpair_requested.emit(DEVICE_ID)
        window.conversations.unpair_requested.emit("b" * 64)  # không có trong danh sách: không có tên
        assert calls == [
            ("pair", view),
            ("addr", "192.168.1.5", 47474),
            ("unpair", DEVICE_ID, "May-B"),
            ("unpair", "b" * 64, None),
        ]
    finally:
        gui.tray.dispose()
        gui.window.deleteLater()


def test_devices_changed_resets_window_devices(qapp, tmp_path):
    gui = ui_app.GuiApp(qapp, tmp_path)
    try:
        gui.pairing.devices_changed.emit((_view(paired=True),))
        shown = gui.window.devices.device(DEVICE_ID)
        assert shown is not None and shown.paired
        gui.pairing.devices_changed.emit(())
        assert gui.window.devices.device(DEVICE_ID) is None
    finally:
        gui.tray.dispose()
        gui.window.deleteLater()


def test_quit_closes_pairing_dialogs(qapp, monkeypatch, tmp_path):
    closed: list[bool] = []
    monkeypatch.setattr(PairingController, "close_all", lambda self: closed.append(True))
    monkeypatch.setattr(qapp, "quit", lambda: None)
    gui = ui_app.GuiApp(qapp, tmp_path)
    try:
        gui.request_quit()
        assert closed == [True]
    finally:
        gui._quit_timer.stop()
        gui.tray.dispose()
        gui.window.deleteLater()
