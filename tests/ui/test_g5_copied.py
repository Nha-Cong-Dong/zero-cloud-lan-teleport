"""Phản hồi “Đã chép” trên nút chép địa chỉ (spec G5 mục 3.4, test 7.4; mục 13 thắng).

Viết từ spec, không từ code. `COPIED_FEEDBACK_MS` đọc lúc `trigger` nên test vá nhỏ lại; “Đã chép” không
phải hoạt ảnh: vẫn chạy ở chế độ tức thì và khi hệ điều hành tắt hiệu ứng (mvp N12).

1. `CopiedFeedback`: `trigger` → `active`, phát `True` một lần; hết hẹn → `False`; `trigger` hai lần hẹn
   lại từ đầu; nút ẩn → `cancel` ngay; `cancel` khi không active không phát; mặc định 1500 ms; hết hẹn
   không còn timer (mvp N3); nút bị hủy giữa chừng không lỗi.
2. `WaitPairingPage`: bấm “Chép” → clipboard, “Đã chép” + icon + tên trợ năng “Đã chép địa chỉ …”; hết
   hẹn → “Chép”, không icon, tên cũ; cỡ nút không đổi; English “Copied” → “Copy”; rời trang → về “Chép”.
3. `MachineCard`: bấm → `checkmark`, “Đã chép địa chỉ …”, `address_copied` như cũ; hết hẹn → `copy`,
   “Chép địa chỉ”; `set_copy_value(None)` hay giá trị khác khi đang active → hết active.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget

WAIT_MS = 5000
SHORT_MS = 300  # COPIED_FEEDBACK_MS vá nhỏ

COPY_VI = "Chép"
COPIED_VI = "Đã chép"
COPY_EN = "Copy"
COPIED_EN = "Copied"
COPY_NAME_VI = "Chép địa chỉ {address}"
COPIED_NAME_VI = "Đã chép địa chỉ {address}"
COPY_NAME_EN = "Copy address {address}"
COPIED_NAME_EN = "Copied address {address}"
HOME_COPY_VI = "Chép địa chỉ"


def plain(text: str) -> str:
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


def settle(qtbot=None, ms: int = 0) -> None:
    for _ in range(3):
        QApplication.processEvents()
    if qtbot is not None and ms:
        qtbot.wait(ms)


def active_timers(*objects) -> list[QTimer]:
    out: list[QTimer] = []
    for obj in objects:
        out += [t for t in obj.findChildren(QTimer) if t.isActive()]
    return out


@pytest.fixture
def short_feedback(monkeypatch):
    from teleport.ui import buttons

    monkeypatch.setattr(buttons, "COPIED_FEEDBACK_MS", SHORT_MS)
    return SHORT_MS


@pytest.fixture
def button(qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    btn = QPushButton("Chép", host)
    QVBoxLayout(host).addWidget(btn)
    host.resize(200, 80)
    host.show()
    qtbot.waitExposed(host)
    settle()
    yield btn  # giữ `host` sống tới hết test (qtbot chỉ giữ yếu)
    del host


def make_feedback(btn):
    from teleport.ui.buttons import CopiedFeedback

    fb = CopiedFeedback(btn)
    events: list[bool] = []
    fb.active_changed.connect(events.append)
    return fb, events


# =================================================================================================
# 7.4.1 CopiedFeedback
# =================================================================================================


def test_default_feedback_duration_is_1500_ms():
    from teleport.ui import buttons

    assert buttons.COPIED_FEEDBACK_MS == 1500


def test_feedback_is_child_of_button_and_starts_inactive(button):
    fb, events = make_feedback(button)
    assert fb.parent() is button
    assert fb.active is False
    fb.cancel()
    assert events == [], "cancel khi không active: không phát"


def test_trigger_activates_once_then_expires(button, qtbot, short_feedback):
    from teleport.ui import motion

    fb, events = make_feedback(button)
    fb.trigger()
    assert fb.active is True
    assert events == [True]
    qtbot.waitUntil(lambda: not fb.active, timeout=WAIT_MS)
    assert events == [True, False]
    assert active_timers(fb, button) == [], "hết hẹn: không còn timer (mvp N3)"
    assert motion.running_count() == 0


def test_trigger_reads_duration_at_trigger_time(button, qtbot, monkeypatch):
    from teleport.ui import buttons

    fb, _events = make_feedback(button)  # dựng trước khi vá
    monkeypatch.setattr(buttons, "COPIED_FEEDBACK_MS", 60)
    fb.trigger()
    assert fb.active
    qtbot.waitUntil(lambda: not fb.active, timeout=1000)


def test_second_trigger_restarts_the_countdown(button, qtbot, monkeypatch):
    from teleport.ui import buttons

    monkeypatch.setattr(buttons, "COPIED_FEEDBACK_MS", 1000)
    fb, events = make_feedback(button)
    fb.trigger()
    qtbot.wait(400)
    assert fb.active, "tiền đề: chưa hết hẹn lần đầu"
    fb.trigger()
    qtbot.wait(700)  # 1100 ms sau lần đầu (đã quá hẹn cũ), 700 ms sau lần hai
    assert fb.active, "trigger lần hai hẹn lại từ đầu"
    assert events == [True], "chỉ một lần True"
    qtbot.waitUntil(lambda: not fb.active, timeout=WAIT_MS)
    assert events == [True, False]


def test_hiding_button_cancels_at_once(button, short_feedback):
    fb, events = make_feedback(button)
    fb.trigger()
    button.hide()
    assert fb.active is False
    assert events == [True, False]
    assert active_timers(fb, button) == []


def test_hiding_window_cancels(button, short_feedback):
    fb, events = make_feedback(button)
    fb.trigger()
    button.window().hide()
    settle()
    assert fb.active is False
    assert events == [True, False]


def test_cancel_stops_and_emits_once(button, short_feedback):
    fb, events = make_feedback(button)
    fb.trigger()
    fb.cancel()
    fb.cancel()
    assert fb.active is False
    assert events == [True, False]
    assert active_timers(fb, button) == []


def test_instant_mode_still_shows_feedback_for_the_whole_window(button, qtbot, short_feedback):
    """Chế độ tức thì (autouse) không rút ngắn “Đã chép”: đây là phản hồi trạng thái, không phải hoạt ảnh."""
    fb, _events = make_feedback(button)
    fb.trigger()
    settle(qtbot, 50)
    assert fb.active is True
    qtbot.waitUntil(lambda: not fb.active, timeout=WAIT_MS)


def test_animations_disabled_still_shows_feedback(button, qtbot, short_feedback, real_motion, monkeypatch):
    monkeypatch.setattr(real_motion, "animations_enabled", lambda: False)
    fb, _events = make_feedback(button)
    fb.trigger()
    settle(qtbot, 50)
    assert fb.active is True, "tắt hiệu ứng: “Đã chép” vẫn hiện trong khoảng hẹn (mvp N12)"
    qtbot.waitUntil(lambda: not fb.active, timeout=WAIT_MS)


def test_button_destroyed_while_active_does_not_crash(qtbot, monkeypatch):
    import shiboken6

    from teleport.ui import buttons

    monkeypatch.setattr(buttons, "COPIED_FEEDBACK_MS", 80)
    host = QWidget()
    qtbot.addWidget(host)
    btn = QPushButton("Chép", host)
    host.show()
    qtbot.waitExposed(host)
    fb, _events = make_feedback(btn)
    fb.trigger()
    btn.deleteLater()
    qtbot.waitUntil(lambda: not shiboken6.isValid(btn), timeout=WAIT_MS)
    qtbot.wait(200)  # quá hẹn: không lỗi trong slot (pytest-qt bắt ngoại lệ)


# =================================================================================================
# 7.4.2 WaitPairingPage
# =================================================================================================


@pytest.fixture
def page(qtbot):
    from teleport.ui.wait_pairing import WaitPairingPage

    p = WaitPairingPage()
    qtbot.addWidget(p)
    p.resize(980, 660)
    p.show()
    qtbot.waitExposed(p)
    return p


def _one_copy(page, address: str = "192.168.1.20"):
    page.set_addresses([address], None)
    settle()
    assert len(page.copy_buttons) == 1
    assert len(page.copy_feedbacks) == 1
    return page.copy_buttons[0], page.copy_feedbacks[0]


def test_copied_button_text_function():
    from teleport.ui.wait_pairing import copied_button_text

    assert copied_button_text() == COPIED_VI


def test_copy_feedbacks_parallel_to_copy_buttons(page):
    from teleport.ui.buttons import CopiedFeedback

    page.set_addresses(["192.168.1.20", "10.0.0.5"], None)
    settle()
    assert len(page.copy_feedbacks) == len(page.copy_buttons) == 2
    for button, feedback in zip(page.copy_buttons, page.copy_feedbacks, strict=True):
        assert isinstance(feedback, CopiedFeedback)
        assert feedback.parent() is button
        assert feedback.active is False


def test_wait_pairing_copy_shows_copied_then_returns(page, qtbot, short_feedback):
    address = "192.168.1.20"
    button, feedback = _one_copy(page, address)
    assert plain(button.text()) == COPY_VI
    assert button.icon().isNull()
    assert button.accessibleName() == COPY_NAME_VI.format(address=address)
    size = button.size()

    QGuiApplication.clipboard().setText("không đổi")
    button.click()
    settle()
    assert QGuiApplication.clipboard().text() == address
    assert feedback.active is True
    assert plain(button.text()) == COPIED_VI
    assert not button.icon().isNull(), "dạng active có icon ✓"
    assert button.accessibleName() == COPIED_NAME_VI.format(address=address), "tên trợ năng theo chữ hiện"
    assert COPY_NAME_VI.format(address=address) in button.toolTip(), "tooltip giữ “Chép địa chỉ …”"
    assert button.size() == size, "cỡ nút không đổi khi đổi chữ"

    qtbot.waitUntil(lambda: plain(button.text()) == COPY_VI, timeout=WAIT_MS)
    assert feedback.active is False
    assert button.icon().isNull()
    assert button.accessibleName() == COPY_NAME_VI.format(address=address)
    assert button.size() == size
    assert active_timers(feedback, button) == [], "hết hẹn: không còn timer (mvp N3)"


def test_wait_pairing_copy_keeps_feedback_in_instant_mode(page, qtbot, short_feedback):
    button, feedback = _one_copy(page)
    button.click()
    settle(qtbot, 50)
    assert plain(button.text()) == COPIED_VI, "chế độ tức thì vẫn hiện “Đã chép” trong khoảng hẹn"
    qtbot.waitUntil(lambda: not feedback.active, timeout=WAIT_MS)


def test_wait_pairing_copy_english_while_active(page, qtbot, short_feedback, qapp):
    from teleport.ui import i18n

    address = "192.168.1.20"
    button, feedback = _one_copy(page, address)
    button.click()
    settle()
    assert feedback.active
    i18n.set_language("en")  # autouse `ui_language_reset` trả về chuỗi nguồn
    settle()
    assert plain(button.text()) == COPIED_EN
    assert button.accessibleName() == COPIED_NAME_EN.format(address=address)
    qtbot.waitUntil(lambda: plain(button.text()) == COPY_EN, timeout=WAIT_MS)
    assert button.accessibleName() == COPY_NAME_EN.format(address=address)
    assert button.icon().isNull()


def test_wait_pairing_leaving_page_resets_copy_button(page, qtbot, short_feedback):
    button, feedback = _one_copy(page)
    button.click()
    settle()
    assert feedback.active
    page.hide()
    assert feedback.active is False
    page.show()
    qtbot.waitExposed(page)
    settle()
    assert plain(button.text()) == COPY_VI
    assert button.icon().isNull()


def test_wait_pairing_copy_button_min_size_fits_both_forms(page):
    """B4: `minimumSize` ≥ `sizeHint` của cả hai dạng (đo bằng áp tạm từng dạng)."""
    from teleport.ui.wait_pairing import WaitPairingPage

    address = "192.168.1.20"
    button, _feedback = _one_copy(page, address)
    minimum = button.minimumSize()
    probe = QPushButton()
    probe.setFont(button.font())
    probe.setIconSize(button.iconSize())
    for active in (False, True):
        WaitPairingPage._label_copy_button(probe, address, active)
        hint = probe.sizeHint()
        assert minimum.width() >= hint.width(), (active, minimum, hint)
        assert minimum.height() >= hint.height(), (active, minimum, hint)
    probe.deleteLater()


# =================================================================================================
# 7.4.3 MachineCard
# =================================================================================================


@pytest.fixture
def card(qtbot):
    from teleport.ui.home import MachineCard

    c = MachineCard()
    qtbot.addWidget(c)
    c.resize(480, 160)
    c.set_name("May-A")
    c.set_copy_value("192.168.1.5")
    c.show()
    qtbot.waitExposed(c)
    settle()
    return c


def test_machine_card_copy_feedback_exists(card):
    from teleport.ui.buttons import CopiedFeedback

    assert isinstance(card.copy_feedback, CopiedFeedback)
    assert card.copy_feedback.parent() is card.copy_button
    assert card.copy_button.icon_name == "copy"
    assert card.copy_button.label == HOME_COPY_VI


def test_machine_card_copy_shows_check_then_returns(card, qtbot, short_feedback):
    address = "192.168.1.5"
    copied: list[str] = []
    card.address_copied.connect(copied.append)
    card.copy_button.click()
    settle()
    assert QGuiApplication.clipboard().text() == address
    assert copied == [address], "address_copied phát như cũ"
    assert card.copy_feedback.active is True
    assert card.copy_button.icon_name == "checkmark"
    assert card.copy_button.label == COPIED_NAME_VI.format(address=address)
    assert card.copy_button.accessibleName() == COPIED_NAME_VI.format(address=address)
    qtbot.waitUntil(lambda: card.copy_button.icon_name == "copy", timeout=WAIT_MS)
    assert card.copy_button.label == HOME_COPY_VI
    assert card.copy_feedback.active is False
    assert active_timers(card.copy_feedback, card.copy_button) == []


def test_machine_card_hiding_copy_value_while_active_cancels(card, short_feedback):
    card.copy_button.click()
    settle()
    assert card.copy_feedback.active
    card.set_copy_value(None)
    assert card.copy_feedback.active is False
    assert card.copy_button.icon_name == "copy"
    assert card.copy_button.label == HOME_COPY_VI


def test_machine_card_new_copy_value_while_active_cancels(card, short_feedback):
    card.copy_button.click()
    settle()
    assert card.copy_feedback.active
    card.set_copy_value("10.0.0.9")
    assert card.copy_feedback.active is False, "B5: giá trị khác → cancel"
    assert card.copy_button.icon_name == "copy"
    assert card.copy_button.label == HOME_COPY_VI


def test_machine_card_retranslate_follows_active(card, qtbot, short_feedback):
    from teleport.ui import i18n

    card.copy_button.click()
    settle()
    i18n.set_language("en")
    settle()
    card.retranslate()  # MachineCard không tự bắt LanguageChange: cửa sổ chính gọi (home.py docstring)
    assert card.copy_button.label == COPIED_NAME_EN.format(address="192.168.1.5")
    qtbot.waitUntil(lambda: card.copy_button.icon_name == "copy", timeout=WAIT_MS)
    assert card.copy_button.label == "Copy address"
