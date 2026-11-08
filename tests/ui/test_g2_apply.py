"""Áp `AnimatedButton` vào các màn và `PairingDialog` chờ máy kia (spec-g2 mục 4.3, test 7.3; mục 12
H4, H7, H8 thắng).

Viết từ spec, không từ code. Mặc định chế độ tức thì (autouse `motion_instant`).

1. Kiểu nút theo bảng 4.3 (chính/phụ); `chat_bubbles.OfferPanel.accept_button` không đổi (vẫn
   `QPushButton` thường mang `PRIMARY_PROPERTY`); luật QSS nút chính còn.
2. `PairingDialog` qua `PairingController` + host giả: bấm “Trùng khớp” → đúng một `decided(sid, True)`,
   hộp ở lại, nút thành “Đang ghép đôi…” (chạy), “Không trùng” thành “Đóng”; bấm lại / Space / `click()`
   C++ không quyết định lần hai; Esc / X / “Đóng” / Enter chỉ đóng hộp; `pairing_finished`, hết hạn,
   `set_host(None)`, `close_all` đóng hộp; trước khi bấm, Esc vẫn là `decided(False)`; English.
3. Hộp nhận file và hộp kết quả ghép đôi: Enter vẫn là nút an toàn / nút mặc định như cũ; default /
   escape như cũ.

Host giả định nghĩa tại chỗ (không import từ `test_pairing.py`).
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication, QDialog, QMessageBox, QPushButton

PEER_ID = "3f2a9c1e" + "ab" * 12
NOW = 1000.0
WAIT_MS = 3000

ACCEPT_VI = "Trùng khớp"
REJECT_VI = "Không trùng"
WAIT_VI = "Đang ghép đôi…"
WAIT_DESC_VI = "Đã xác nhận mã. Đang chờ máy kia xác nhận."
CLOSE_VI = "Đóng"
WAIT_EN = "Pairing…"
WAIT_DESC_EN = "Code confirmed. Waiting for the other device to confirm."
CLOSE_EN = "Close"


def plain(text: str) -> str:
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


def is_animated(button) -> bool:
    from teleport.ui.buttons import AnimatedButton

    return isinstance(button, AnimatedButton)


def assert_primary(button, name: str) -> None:
    from teleport.ui import theme

    assert is_animated(button), f"{name} phải là AnimatedButton, thấy {type(button).__name__}"
    assert button.primary is True, f"{name} phải là nút chính"
    assert bool(button.property(theme.PRIMARY_PROPERTY)), f"{name} phải mang PRIMARY_PROPERTY"


def assert_secondary(button, name: str) -> None:
    from teleport.ui import theme

    assert is_animated(button), f"{name} phải là AnimatedButton, thấy {type(button).__name__}"
    assert button.primary is False, f"{name} phải là nút phụ"
    assert not button.property(theme.PRIMARY_PROPERTY), f"{name} không mang PRIMARY_PROPERTY"


def closed(widget) -> bool:
    import shiboken6

    return widget is None or not shiboken6.isValid(widget) or not widget.isVisible()


# =================================================================================================
# Dữ liệu
# =================================================================================================


def make_prompt(*, sid: int = 7, role: str = "responder", expires_at: float = NOW + 60.0):
    from teleport.app.pairing import PairingPrompt
    from teleport.core.pairing import Role

    return PairingPrompt(
        session_id=sid,
        role=Role(role),
        address="192.168.1.5",
        peer_name="MacBook",
        peer_device_id=PEER_ID,
        code="905617",
        expires_at=expires_at,
        recent_codes=1,
        known_name=None,
    )


def make_finished(outcome: str, *, sid: int | None = 7, role: str = "initiator", code_shown: bool = False):
    from teleport.app.pairing import PairingFinished, PairingOutcome
    from teleport.core.pairing import Role

    return PairingFinished(
        session_id=sid,
        role=Role(role),
        outcome=PairingOutcome(outcome),
        address="192.168.1.5",
        port=47474,
        peer_name="MacBook",
        peer_device_id=PEER_ID,
        peer_reason=None,
        device=None,
        known_name=None,
        code_shown=code_shown,
    )


def make_offer(transfer_id: str = "d" * 32):
    from teleport.app.transfers import OfferReceived
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import Direction, TransferKey

    files = (FileEntry("video.mp4", 1000, 1_700_000_000_000),)
    return OfferReceived(
        key=TransferKey(PEER_ID, Direction.RECEIVE, transfer_id),
        device_name="PC-Nha",
        files=files,
        names=("video.mp4",),
        total_bytes=1000,
        auto_accepted=False,
    )


# =================================================================================================
# Host giả
# =================================================================================================


class FakePairing:
    def __init__(self) -> None:
        self.decisions: list[tuple[int, bool]] = []

    def decide(self, session_id: int, accepted: bool) -> bool:
        self.decisions.append((session_id, accepted))
        return True

    async def pair_with(self, addresses, port):
        return None


class FakeNode:
    def __init__(self) -> None:
        self.pairing = FakePairing()

    def devices(self):
        return ()

    def connect_hint(self, device_id, addresses, port) -> None:
        return None


class FakeHost(QObject):
    """Signal như `NodeHost`; `call` chỉ ghi lại, `decisions()` chạy các call rồi đọc quyết định."""

    pairing_prompt = Signal(object)
    pairing_finished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.node = FakeNode()
        self.calls: list = []
        self.submits: list = []

    def call(self, fn, on_done=None) -> None:
        self.calls.append([fn, on_done, False])

    def submit(self, fn, on_done=None):
        self.submits.append([fn, on_done, False])
        return SimpleNamespace(done=False, cancel=lambda: None)

    def decisions(self) -> list[tuple[int, bool]]:
        for entry in self.calls:
            if entry[2]:
                continue
            entry[2] = True
            try:
                result = entry[0](self.node)
            except Exception as e:
                result = e
            if entry[1] is not None:
                entry[1](result)
        return list(self.node.pairing.decisions)


class FakeClock:
    def __init__(self, value: float = NOW) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.fixture(autouse=True)
def no_exec(monkeypatch):
    calls: list[str] = []

    def fake(self, *a, **k):
        calls.append(type(self).__name__)
        return 0

    monkeypatch.setattr(QDialog, "exec", fake)
    monkeypatch.setattr(QMessageBox, "exec", fake)
    yield calls
    assert not calls, f"hộp mở bằng exec(): {calls}"


@pytest.fixture
def ctl(qtbot):
    from teleport.ui.pairing import PairingController

    made: list = []

    def make(**kw):
        host = FakeHost()
        clock = kw.pop("clock", FakeClock())
        kw.setdefault("notify", lambda title, text: None)
        c = PairingController(clock=clock, **kw)
        c.set_host(host)
        ns = SimpleNamespace(c=c, host=host, clock=clock)
        made.append(ns)
        return ns

    yield make

    for ns in made:
        with contextlib.suppress(Exception):
            ns.c.close_all()
        with contextlib.suppress(Exception):
            ns.c.set_host(None)
    qtbot.wait(10)


def open_prompt(qtbot, ns, prompt):
    ns.host.pairing_prompt.emit(prompt)
    qtbot.waitUntil(lambda: ns.c.dialog(prompt.session_id) is not None, timeout=WAIT_MS)
    dlg = ns.c.dialog(prompt.session_id)
    qtbot.waitUntil(dlg.isVisible, timeout=WAIT_MS)
    QApplication.processEvents()
    return dlg


def activate(qtbot, window) -> None:
    window.activateWindow()
    window.raise_()
    qtbot.waitUntil(window.isActiveWindow, timeout=WAIT_MS)


def confirm(qtbot, ns, sid: int = 7):
    """Mở hộp của phiên `sid`, bấm “Trùng khớp” bằng chuột; trả hộp."""
    dlg = open_prompt(qtbot, ns, make_prompt(sid=sid))
    accept = dlg.accept_button
    QTest.mouseClick(accept, Qt.MouseButton.LeftButton, pos=accept.rect().center())
    QApplication.processEvents()
    return dlg


# =================================================================================================
# 7.3.1 Kiểu nút theo bảng 4.3
# =================================================================================================


def test_start_panel_buttons_are_primary_animated(qtbot):
    from teleport.ui.home import StartPanel

    panel = StartPanel()
    qtbot.addWidget(panel)
    assert_primary(panel.start_button, "start_button")
    assert_primary(panel.retry_button, "retry_button")
    assert panel.start_button.objectName() == "startButton"
    assert panel.retry_button.objectName() == "retryButton"
    assert panel.start_button.isDefault(), "“Bắt đầu” vẫn là nút mặc định"


def test_discover_address_submit_is_primary_animated(qtbot):
    from teleport.ui.discover import DiscoverPage
    from teleport.ui.radar import DeviceListModel

    model = DeviceListModel()
    page = DiscoverPage(model)
    model.setParent(page)
    qtbot.addWidget(page)
    assert_primary(page.address_submit, "address_submit")
    assert page.address_submit.objectName() == "discoverAddressSubmit"
    assert not page.address_submit.isEnabled(), "chưa nhập địa chỉ: vẫn tắt như cũ"


def test_empty_state_action_button_is_primary_animated(qtbot):
    from teleport.ui.widgets import EmptyState

    empty = EmptyState()
    qtbot.addWidget(empty)
    empty.set_content("desktop", "Ghép đôi máy đầu tiên", "Mô tả", action="Tìm máy mới")
    assert_primary(empty.action_button, "EmptyState.action_button")
    seen: list[int] = []
    empty.action_triggered.connect(lambda: seen.append(1))
    empty.action_button.click()
    assert seen == [1]


def test_offer_box_buttons_types_default_and_bold_accept(qtbot):
    from teleport.ui.offer import OfferBook, OfferPrompts

    book = OfferBook()
    prompts = OfferPrompts(book, Path("nhan"))
    item = make_offer()
    assert book.add(item)
    box = prompts.preview(item.key)
    assert box is not None
    qtbot.addWidget(box)
    accept = box.findChild(QPushButton, "offerAccept")
    reject = box.findChild(QPushButton, "offerReject")
    assert accept is not None and reject is not None
    assert_primary(accept, "offerAccept")
    assert_secondary(reject, "offerReject")
    assert accept.font().bold() or accept.font().weight() >= QFont.Weight.DemiBold, "“Nhận” giữ chữ đậm"
    assert reject.isDefault() and reject.autoDefault()
    assert not accept.isDefault() and not accept.autoDefault()


def test_pairing_dialog_buttons_types_and_default(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt())
    accept = dlg.findChild(QAbstractButton, "pairingAccept")
    reject = dlg.findChild(QAbstractButton, "pairingReject")
    assert accept is dlg.accept_button and reject is dlg.reject_button
    assert_primary(accept, "pairingAccept")
    assert_secondary(reject, "pairingReject")
    assert reject.isDefault() and not accept.isDefault() and not accept.autoDefault()
    assert plain(accept.text()) == ACCEPT_VI and plain(reject.text()) == REJECT_VI


@pytest.mark.parametrize("code_shown", [False, True], ids=["chua-hien-ma", "da-hien-ma"])
def test_pairing_result_box_buttons_types_roles_default_escape(qtbot, ctl, code_shown):
    """H7: nút “Thử lại”/“Đóng” là AnimatedButton chính/phụ; objectName, vai, default, escape như cũ."""
    ns = ctl()
    ns.host.pairing_finished.emit(make_finished("unreachable", code_shown=code_shown))
    qtbot.waitUntil(lambda: ns.c.result_box is not None and ns.c.result_box.isVisible(), timeout=WAIT_MS)
    box = ns.c.result_box
    retry = box.findChild(QAbstractButton, "pairingRetryButton")
    close = box.findChild(QAbstractButton, "pairingCloseButton")
    assert retry is not None and close is not None
    assert retry in box.buttons() and close in box.buttons()
    assert_primary(retry, "pairingRetryButton")
    assert_secondary(close, "pairingCloseButton")
    assert box.buttonRole(retry) == QMessageBox.ButtonRole.AcceptRole
    assert box.buttonRole(close) == QMessageBox.ButtonRole.RejectRole
    assert box.escapeButton() is close
    assert box.defaultButton() is (close if code_shown else retry)
    assert retry.parent() is not None
    from teleport.ui import texts

    assert plain(retry.text()) == texts.retry()
    assert plain(close.text()) == CLOSE_VI


def test_pairing_result_box_buttons_translated(qtbot, ctl):
    from teleport.ui import i18n

    ns = ctl()
    ns.host.pairing_finished.emit(make_finished("unreachable"))
    qtbot.waitUntil(lambda: ns.c.result_box is not None, timeout=WAIT_MS)
    i18n.set_language("en")
    QApplication.processEvents()
    box = ns.c.result_box
    close = box.findChild(QAbstractButton, "pairingCloseButton")
    assert plain(close.text()) == CLOSE_EN
    assert is_animated(close)


def test_chat_offer_panel_accept_is_not_converted(qtbot):
    from teleport.ui import theme
    from teleport.ui.chat_bubbles import OfferPanel

    panel = OfferPanel()
    qtbot.addWidget(panel)
    assert isinstance(panel.accept_button, QPushButton)
    assert not is_animated(panel.accept_button), "G4 mới chuyển nút trong chat_bubbles"
    assert bool(panel.accept_button.property(theme.PRIMARY_PROPERTY))


@pytest.mark.parametrize("dark", [False, True], ids=["sang", "toi"])
def test_primary_qss_rules_still_present(dark):
    from teleport.ui import theme

    css = theme.stylesheet(dark)
    colors = theme.primary_colors(dark)
    prop = theme.PRIMARY_PROPERTY

    def rule(state: str) -> str:
        m = re.search(r"QPushButton\[" + prop + r'="true"\]' + re.escape(state) + r"\s*\{([^}]*)\}", css)
        assert m, f"thiếu luật QPushButton[{prop}]{state}"
        return m.group(1).lower()

    assert colors["normal"][1].name().lower() in rule("")
    assert colors["hover"][1].name().lower() in rule(":hover")
    assert colors["pressed"][1].name().lower() in rule(":pressed")
    assert theme.focus_ring_color(dark).name().lower() in rule(":focus")
    assert colors["disabled"][1].name().lower() in rule(":disabled")
    assert f"{theme.PRIMARY_BORDER_WIDTH}px solid" in rule("")


def test_wait_texts_source_strings():
    from teleport.ui import pairing

    assert pairing.pairing_wait_text() == WAIT_VI
    assert pairing.pairing_wait_description() == WAIT_DESC_VI


def test_wait_texts_english():
    from teleport.ui import i18n, pairing

    i18n.set_language("en")
    assert pairing.pairing_wait_text() == WAIT_EN
    assert pairing.pairing_wait_description() == WAIT_DESC_EN


# =================================================================================================
# 7.3.2 PairingDialog chờ máy kia sau “Trùng khớp”
# =================================================================================================


def test_confirm_decides_once_and_dialog_waits(qtbot, ctl):
    ns = ctl()
    dlg = confirm(qtbot, ns, sid=31)
    assert ns.host.decisions() == [(31, True)]
    assert not closed(dlg), "hộp ở lại chờ máy kia"
    accept, reject = dlg.accept_button, dlg.reject_button
    assert accept.running is True
    assert plain(accept.text()) == WAIT_VI
    assert accept.accessibleName() == WAIT_VI
    assert accept.accessibleDescription() == WAIT_DESC_VI
    assert plain(reject.text()) == CLOSE_VI
    assert reject.accessibleName() == CLOSE_VI
    assert reject.isDefault(), "“Đóng” vẫn là nút mặc định"
    assert not accept.isDefault()


def test_confirm_with_space_keeps_focus_on_accept(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(sid=32))
    activate(qtbot, dlg)
    accept = dlg.accept_button
    accept.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(accept.hasFocus, timeout=WAIT_MS)
    QTest.keyClick(accept, Qt.Key.Key_Space)
    QApplication.processEvents()
    assert ns.host.decisions() == [(32, True)]
    assert accept.running is True
    assert accept.hasFocus(), "tiêu điểm ở “Trùng khớp”"
    assert not closed(dlg)


def test_second_press_space_and_cpp_click_do_not_decide_again(qtbot, ctl):
    ns = ctl()
    dlg = confirm(qtbot, ns, sid=33)
    accept = dlg.accept_button
    center = accept.rect().center()
    QTest.mouseClick(accept, Qt.MouseButton.LeftButton, pos=center)
    activate(qtbot, dlg)
    accept.setFocus()
    QTest.keyClick(accept, Qt.Key.Key_Space)
    accept.click()
    # `click()` C++ (hành động “Press” của trình đọc màn hình) không chặn được ở nút: hộp phải bỏ qua.
    QAbstractButton.click(accept)
    QApplication.processEvents()
    assert ns.host.decisions() == [(33, True)]
    assert len(ns.host.calls) == 1, "chỉ một lần gọi decide"
    assert not closed(dlg)
    assert accept.running is True


@pytest.mark.parametrize("how", ["esc", "x", "dong", "enter", "return-tren-nut-chay"])
def test_after_confirm_closing_only_closes(qtbot, ctl, how):
    ns = ctl()
    dlg = confirm(qtbot, ns, sid=34)
    if how == "return-tren-nut-chay":
        # Return gửi thẳng tới nút đang chạy: đi lên hộp, bấm nút mặc định “Đóng” (soat-code G2).
        activate(qtbot, dlg)
        qtbot.keyClick(dlg.accept_button, Qt.Key.Key_Return)
    elif how == "esc":
        QTest.keyClick(dlg, Qt.Key.Key_Escape)
    elif how == "x":
        dlg.close()
    elif how == "dong":
        dlg.reject_button.click()
    else:
        activate(qtbot, dlg)
        QTest.keyClick(dlg, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: closed(dlg), timeout=WAIT_MS)
    qtbot.wait(20)
    assert ns.host.decisions() == [(34, True)], "đóng hộp sau “Trùng khớp” không đổi quyết định"
    assert ns.c.dialog(34) is None


def test_before_confirm_escape_still_rejects(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(sid=35))
    QTest.keyClick(dlg, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: closed(dlg), timeout=WAIT_MS)
    assert ns.host.decisions() == [(35, False)]


def test_after_confirm_pairing_finished_closes(qtbot, ctl):
    ns = ctl()
    dlg = confirm(qtbot, ns, sid=36)
    ns.host.pairing_finished.emit(make_finished("peer_rejected", sid=36, role="responder", code_shown=True))
    qtbot.waitUntil(lambda: closed(dlg), timeout=WAIT_MS)
    assert ns.host.decisions() == [(36, True)]


def test_after_confirm_expiry_closes(qtbot, ctl):
    """H4: hộp vẫn đóng ở `expires_at` sau “Trùng khớp”."""
    clock = FakeClock(NOW)
    ns = ctl(clock=clock)
    dlg = open_prompt(qtbot, ns, make_prompt(sid=37, expires_at=NOW + 0.4))
    dlg.accept_button.click()
    QApplication.processEvents()
    assert not closed(dlg)
    qtbot.waitUntil(lambda: closed(dlg), timeout=WAIT_MS)
    assert ns.host.decisions() == [(37, True)]


@pytest.mark.parametrize("how", ["set_host_none", "close_all"])
def test_after_confirm_host_change_closes(qtbot, ctl, how):
    ns = ctl()
    dlg = confirm(qtbot, ns, sid=38)
    if how == "set_host_none":
        ns.c.set_host(None)
    else:
        ns.c.close_all()
    qtbot.waitUntil(lambda: closed(dlg), timeout=WAIT_MS)
    assert ns.host.decisions() == [(38, True)]


def test_after_confirm_countdown_keeps_running(qtbot, ctl):
    clock = FakeClock(NOW)
    ns = ctl(clock=clock)
    dlg = open_prompt(qtbot, ns, make_prompt(sid=39, expires_at=NOW + 60))
    dlg.accept_button.click()
    QApplication.processEvents()
    before = dlg.countdown_label.text()
    clock.value = NOW + 20
    qtbot.waitUntil(lambda: dlg.countdown_label.text() != before, timeout=WAIT_MS)
    assert not closed(dlg)


def test_accept_called_from_code_keeps_old_behaviour(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(sid=40))
    dlg.accept()
    qtbot.waitUntil(lambda: closed(dlg), timeout=WAIT_MS)
    assert ns.host.decisions() == [(40, True)]


def test_confirm_in_english(qtbot, ctl):
    from teleport.ui import i18n

    i18n.set_language("en")
    ns = ctl()
    dlg = confirm(qtbot, ns, sid=41)
    assert plain(dlg.accept_button.text()) == WAIT_EN
    assert dlg.accept_button.accessibleName() == WAIT_EN
    assert dlg.accept_button.accessibleDescription() == WAIT_DESC_EN
    assert plain(dlg.reject_button.text()) == CLOSE_EN
    assert dlg.reject_button.accessibleName() == CLOSE_EN


def test_language_change_after_confirm_retranslates_waiting_state(qtbot, ctl):
    from teleport.ui import i18n

    ns = ctl()
    dlg = confirm(qtbot, ns, sid=42)
    i18n.set_language("en")
    QApplication.processEvents()
    assert dlg.accept_button.running is True
    assert plain(dlg.accept_button.text()) == WAIT_EN
    assert dlg.accept_button.accessibleName() == WAIT_EN
    assert dlg.accept_button.accessibleDescription() == WAIT_DESC_EN
    assert plain(dlg.reject_button.text()) == CLOSE_EN
    assert ns.host.decisions() == [(42, True)]


def test_confirm_in_real_motion_spins_and_stops_when_closed(qtbot, ctl, real_motion):
    ns = ctl()
    dlg = confirm(qtbot, ns, sid=43)
    accept = dlg.accept_button
    qtbot.waitUntil(accept.spinner_active, timeout=WAIT_MS)
    dlg.reject_button.click()
    qtbot.waitUntil(lambda: closed(dlg), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


# =================================================================================================
# 7.3.3 Enter vẫn là nút an toàn
# =================================================================================================


@pytest.fixture
def offer_box(qtbot):
    from teleport.ui.offer import OfferBook, OfferPrompts

    book = OfferBook()
    prompts = OfferPrompts(book, Path("nhan"))
    decisions: list = []
    prompts.decided.connect(lambda key, ok: decisions.append((key, ok)))
    item = make_offer()
    assert book.add(item)
    assert prompts.show_prompt(item.key)
    box = prompts.box(item.key)
    assert box is not None
    qtbot.waitUntil(box.isVisible, timeout=WAIT_MS)
    yield SimpleNamespace(box=box, key=item.key, decisions=decisions, prompts=prompts, book=book)
    book.clear()
    prompts.close_all()
    qtbot.wait(10)


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape])
def test_offer_box_enter_and_escape_reject(qtbot, offer_box, key):
    box = offer_box.box
    activate(qtbot, box)
    QTest.keyClick(box, key)
    QApplication.processEvents()
    assert offer_box.decisions == [(offer_box.key, False)], "Enter/Esc là “Từ chối”"


def test_offer_box_accept_click_accepts(qtbot, offer_box):
    accept = offer_box.box.findChild(QPushButton, "offerAccept")
    QTest.mouseClick(accept, Qt.MouseButton.LeftButton, pos=accept.rect().center())
    QApplication.processEvents()
    assert offer_box.decisions == [(offer_box.key, True)]


def test_pairing_dialog_enter_before_confirm_rejects(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(sid=50))
    activate(qtbot, dlg)
    QTest.keyClick(dlg, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: closed(dlg), timeout=WAIT_MS)
    assert ns.host.decisions() == [(50, False)]


def test_pairing_result_box_escape_is_close(qtbot, ctl):
    ns = ctl()
    ns.host.pairing_finished.emit(make_finished("unreachable"))
    qtbot.waitUntil(lambda: ns.c.result_box is not None and ns.c.result_box.isVisible(), timeout=WAIT_MS)
    box = ns.c.result_box
    QTest.keyClick(box, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: closed(box), timeout=WAIT_MS)
    qtbot.wait(20)
    assert ns.host.submits == [], "Esc = Đóng: không thử lại"
