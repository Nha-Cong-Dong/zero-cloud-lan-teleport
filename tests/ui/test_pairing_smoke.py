"""Test khói của `teleport.ui.pairing` và phần ghép đôi của `bridge.NodeHost` (spec hackathon PR2).

Bộ test chính ở `test_pairing.py` / `test_bridge_pairing.py`; ở đây chỉ kiểm nhanh các đường chính:
bố cục hộp mã, quyết định một lần, hết hạn không quyết định, hộp kết quả dùng lại, và một lần ghép
đôi thật giữa hai `NodeHost` trên 127.0.0.1.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QLabel

from teleport.app.pairing import PairingFinished, PairingOutcome, PairingPrompt
from teleport.core.pairing import Role

POISON = "PC-A:   382 104 <b>x</b>"


def _prompt(**kw) -> PairingPrompt:
    values = {
        "session_id": 7,
        "role": Role.RESPONDER,
        "address": "192.168.1.5",
        "peer_name": POISON,
        "peer_device_id": "3f2a9c1e" + "0" * 24,
        "code": "482913",
        "expires_at": 1_000.0,
        "recent_codes": 1,
        "known_name": None,
    }
    values.update(kw)
    return PairingPrompt(**values)


def _finished(outcome: PairingOutcome, **kw) -> PairingFinished:
    values = {
        "session_id": 7,
        "role": Role.INITIATOR,
        "outcome": outcome,
        "address": "192.168.1.5",
        "port": 47474,
        "peer_name": "MacBook",
        "peer_device_id": "3f2a9c1e" + "0" * 24,
        "peer_reason": None,
        "device": None,
        "known_name": None,
        "code_shown": True,
    }
    values.update(kw)
    return PairingFinished(**values)


class FakeHost(QObject):
    pairing_prompt = Signal(object)
    pairing_finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []
        self.submits: list = []

    def call(self, fn, on_done=None) -> None:
        self.calls.append((fn, on_done))

    def submit(self, fn, on_done=None):
        self.submits.append((fn, on_done))
        return _Job()


class _Job:
    done = False

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakePairing:
    def __init__(self) -> None:
        self.decisions: list = []

    def decide(self, sid, ok):
        self.decisions.append((sid, ok))
        return True


class FakeNode:
    def __init__(self) -> None:
        self.pairing = FakePairing()


def _run_calls(host: FakeHost, node: FakeNode) -> None:
    for fn, on_done in host.calls:
        result = fn(node)
        if on_done is not None:
            on_done(result)
    host.calls.clear()


def test_dialog_layout_code_alone_and_plain_text(qtbot):
    from teleport.ui.pairing import PairingDialog

    dialog = PairingDialog(_prompt(recent_codes=3, known_name="MacBook"))
    qtbot.addWidget(dialog)
    assert dialog.windowTitle() == "Ghép đôi"
    code = dialog.findChild(QLabel, "pairingCode")
    assert code.text() == "482 913"
    warning = dialog.findChild(QLabel, "pairingWarning")
    assert not warning.isHidden() and "3 mã ghép đôi" in warning.text()
    peer = dialog.findChild(QLabel, "pairingPeer")
    assert "Máy kia tự xưng là “PC-A:" in peer.text()
    assert "tên đã lưu: “MacBook”" in peer.text()
    for label in dialog.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText
    assert dialog.findChild(object, "pairingReject").isDefault()
    assert not dialog.findChild(object, "pairingAccept").autoDefault()


def test_prompt_esc_decides_false_once_and_expiry_does_not_decide(qtbot):
    from teleport.ui.pairing import PairingController

    now = [995.0]
    controller = PairingController(clock=lambda: now[0])
    host, node = FakeHost(), FakeNode()
    controller.set_host(host)
    host.pairing_prompt.emit(_prompt(session_id=1))
    dialog = controller.dialog(1)
    assert dialog is not None
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    _run_calls(host, node)
    assert node.pairing.decisions == [(1, False)]
    assert controller.dialog(1) is None

    host.pairing_prompt.emit(_prompt(session_id=2, expires_at=995.05))
    assert controller.dialog(2) is not None
    qtbot.waitUntil(lambda: controller.dialog(2) is None, timeout=2_000)
    _run_calls(host, node)
    assert node.pairing.decisions == [(1, False)]
    controller.close_all()


def test_finished_closes_dialog_and_reuses_result_box(qtbot):
    from teleport.ui.pairing import PairingController

    controller = PairingController()
    host = FakeHost()
    controller.set_host(host)
    messages: list[str] = []
    controller.message.connect(messages.append)
    host.pairing_prompt.emit(_prompt(session_id=3))
    host.pairing_finished.emit(_finished(PairingOutcome.TIMEOUT, session_id=3, role=Role.RESPONDER))
    assert controller.dialog(3) is None
    assert host.calls == []  # đóng vì có kết quả: không decide
    first = controller.result_box
    host.pairing_finished.emit(_finished(PairingOutcome.PEER_BUSY, session_id=None, peer_name=None))
    assert controller.result_box is first
    assert first.textFormat() == Qt.TextFormat.PlainText
    assert messages[-1].startswith("“192.168.1.5” chưa sẵn sàng ghép đôi")  # không có tên: địa chỉ
    controller.close_all()


def test_every_outcome_has_text_or_is_silent():
    from teleport.ui.pairing import outcome_text

    for outcome in PairingOutcome:
        text = outcome_text(_finished(outcome))
        assert (text is None) == (outcome is PairingOutcome.CANCELLED), outcome
    silent = _finished(PairingOutcome.CONNECTION_LOST, role=Role.RESPONDER, code_shown=False)
    assert outcome_text(silent) is None


def test_pair_address_cancel_and_stale_host(qtbot):
    from teleport.ui.pairing import PairingController

    controller = PairingController()
    host = FakeHost()
    controller.set_host(host)
    assert controller.pair_address("192.168.1.5", 47474)
    assert controller.busy and controller.progress_box is not None
    assert controller.progress_box.text() == "Đang ghép đôi với “192.168.1.5:47474”…"
    assert controller.progress_box.bar.maximum() == 0  # thanh bận
    assert controller.progress_box.windowModality() == Qt.WindowModality.NonModal
    assert not controller.pair_address("192.168.1.6", 47474)  # bận → câu BUSY
    controller.cancel_pairing()
    assert controller.progress_box is None
    _fn, on_done = host.submits[0]
    controller.set_host(FakeHost())
    on_done(RuntimeError("cũ"))  # on_done của host cũ: bỏ qua
    assert not controller.busy
    controller.close_all()


@pytest.mark.integration
def test_submit_job_cancel(qtbot, config_dir, node_options, node_hosts):
    import asyncio

    host = node_hosts(node_options(config_dir("A")))
    got: list = []
    early = host.submit(_sleep, got.append)  # node chưa chạy: RuntimeError, vẫn có job
    qtbot.waitUntil(lambda: early.done, timeout=2_000)
    assert isinstance(got.pop(), RuntimeError)
    early.cancel()  # đã xong: không làm gì
    with qtbot.waitSignal(host.started, timeout=15_000):
        host.start()
    job = host.submit(_sleep, got.append)
    assert not job.done
    job.cancel()
    job.cancel()
    qtbot.waitUntil(lambda: job.done, timeout=5_000)
    assert len(got) == 1 and isinstance(got[0], asyncio.CancelledError)


async def _sleep(node):
    import asyncio

    await asyncio.sleep(30)


@pytest.mark.integration
def test_two_gui_hosts_pair_by_address(qtbot, config_dir, node_options, node_hosts):
    from teleport.ui.pairing import PairingController

    hosts = [node_hosts(node_options(config_dir(label))) for label in ("A", "B")]
    for host in hosts:
        with qtbot.waitSignal(host.started, timeout=15_000):
            host.start()
    ports: list = []
    hosts[1].call(lambda n: n.port, ports.append)
    qtbot.waitUntil(lambda: bool(ports), timeout=5_000)

    controllers = [PairingController() for _ in hosts]
    results: list[list] = [[], []]
    for host, controller, got in zip(hosts, controllers, results, strict=True):
        controller.set_host(host)
        controller.prompt_opened.connect(lambda sid, c=controller: c.dialog(sid).accept_button.click())
        controller.finished.connect(got.append)
    changed: list = []
    controllers[0].devices_changed.connect(changed.append)

    assert controllers[0].pair_address("127.0.0.1", ports[0])
    qtbot.waitUntil(lambda: bool(results[0]) and bool(results[1]), timeout=20_000)
    assert results[0][0].outcome is PairingOutcome.PAIRED
    assert results[1][0].outcome is PairingOutcome.PAIRED
    qtbot.waitUntil(lambda: bool(changed), timeout=5_000)
    assert any(view.paired for view in changed[0])
    qtbot.waitUntil(lambda: not controllers[0].busy, timeout=5_000)
    for controller in controllers:
        controller.close_all()
