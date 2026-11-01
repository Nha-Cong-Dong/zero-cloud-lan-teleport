"""Huy hiệu “Đang ẩn” trên thẻ máy này (spec D2 mục 1.8, Q6; test 4.7).

Viết từ spec, không từ code. `MachineCard.set_hidden(flag)`; ưu tiên huy hiệu:
“Đang truyền %n lượt” (BUSY) > “Không nhận file” (WARNING) > “Đang ẩn” (MUTED) > “Sẵn sàng nhận” (SUCCESS).
Khi “Đang ẩn”, trạng thái phải đọc được bằng chữ (N12): câu mô tả “Máy chưa ghép đôi không thấy máy này.
Vẫn nhận file từ máy đã ghép đôi.” có trong `accessibleDescription` của huy hiệu hay của dòng tên (chỗ
thẻ máy đã gom mô tả trạng thái, spec-b1b 2.3).

Phần nối (`integration`): `SettingsController.visibility_changed` → huy hiệu (spec D2 1.6 “Đồng bộ trạng
thái”); máy đã lưu `visible: false` thì khi chạy huy hiệu là “Đang ẩn” (suy ra từ 1.8: huy hiệu phản ánh
trạng thái ẩn, không chỉ lần đổi).
"""

from __future__ import annotations

import itertools
import json
import re

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QTranslator

BUSY = "Đang truyền {n} lượt"
NOT_RECEIVING = "Không nhận file"
HIDDEN = "Đang ẩn"
READY = "Sẵn sàng nhận"
HIDDEN_DESCRIPTION = "Máy chưa ghép đôi không thấy máy này. Vẫn nhận file từ máy đã ghép đôi."


def shout(text: str) -> str:
    """Chữ in hoa, giữ nguyên chỗ giữ chỗ `{…}` và `%n`."""
    return "".join(
        p if re.fullmatch(r"\{\w+\}|%n", p) else p.upper() for p in re.split(r"(\{\w+\}|%n)", text)
    )


class ShoutTranslator(QTranslator):
    """Translator giả trả chữ in hoa (như `test_i18n_theme.py`)."""

    def isEmpty(self):
        return False

    def translate(self, context, source_text, disambiguation=None, n=-1):
        return shout(source_text) if source_text else source_text


def descriptions(card) -> str:
    return " | ".join(
        (
            card.badge.accessibleDescription(),
            card.name_label.accessibleDescription(),
            card.accessibleDescription(),
        )
    )


@pytest.fixture
def card(qtbot):
    from teleport.ui.home import MachineCard

    c = MachineCard()
    qtbot.addWidget(c)
    c.set_name("May-A")
    return c


# =================================================================================================
# Ưu tiên huy hiệu
# =================================================================================================


def expected(active: int, unavailable: bool, hidden: bool):
    from teleport.ui.widgets import BadgeTone

    if active > 0:
        return BUSY.format(n=active), BadgeTone.BUSY
    if unavailable:
        return NOT_RECEIVING, BadgeTone.WARNING
    if hidden:
        return HIDDEN, BadgeTone.MUTED
    return READY, BadgeTone.SUCCESS


def test_machine_badge_priority_busy_unavailable_hidden_ready(card):
    """Mọi tổ hợp (lượt, thư mục hỏng, ẩn), theo mọi thứ tự đặt: huy hiệu theo đúng ưu tiên."""
    setters = {
        "active": lambda v: card.set_activity(v),
        "unavailable": lambda v: card.set_receiving_unavailable(v),
        "hidden": lambda v: card.set_hidden(v),
    }
    for active, unavailable, hidden in itertools.product((0, 2), (False, True), (False, True)):
        values = {"active": active, "unavailable": unavailable, "hidden": hidden}
        for order in itertools.permutations(setters):
            # Về trạng thái gốc rồi đặt theo thứ tự `order`: kết quả không phụ thuộc thứ tự gọi.
            card.set_activity(0)
            card.set_receiving_unavailable(False)
            card.set_hidden(False)
            for key in order:
                setters[key](values[key])
            got = (card.badge.text, card.badge.tone)
            assert got == expected(active, unavailable, hidden), (values, order, got)


def test_hidden_badge_muted_with_accessible_description(card):
    from teleport.ui.widgets import BadgeTone

    card.set_hidden(True)
    assert (card.badge.text, card.badge.tone) == (HIDDEN, BadgeTone.MUTED)
    assert HIDDEN_DESCRIPTION in descriptions(card), "trạng thái ẩn phải đọc được bằng chữ (N12)"
    card.set_hidden(False)
    assert (card.badge.text, card.badge.tone) == (READY, BadgeTone.SUCCESS)
    assert HIDDEN_DESCRIPTION not in descriptions(card), "hết ẩn thì bỏ câu mô tả ẩn"


def test_hidden_description_dropped_when_higher_priority_badge_shown(card):
    """Huy hiệu khác thắng (đang truyền / không nhận file): không còn câu mô tả “Đang ẩn” lệch chữ."""
    from teleport.ui.widgets import BadgeTone

    card.set_hidden(True)
    card.set_activity(1)
    assert card.badge.tone == BadgeTone.BUSY
    assert HIDDEN_DESCRIPTION not in card.badge.accessibleDescription()
    card.set_activity(0)
    assert (card.badge.text, card.badge.tone) == (HIDDEN, BadgeTone.MUTED)
    assert HIDDEN_DESCRIPTION in descriptions(card)


# =================================================================================================
# Đổi ngôn ngữ
# =================================================================================================


def test_hidden_badge_language_change(qtbot, tmp_path):
    """LanguageChange (cửa sổ chính đang RUNNING) dịch lại chữ “Đang ẩn” và câu mô tả, giữ tone MUTED;
    gỡ translator thì về chữ nguồn. Đi qua `MainWindow` như app thật: thẻ máy là `window.machine_card`."""
    from teleport.ui.main_window import MainWindow
    from teleport.ui.widgets import BadgeTone

    window = MainWindow()
    qtbot.addWidget(window)
    rd = tmp_path / "nhan"
    rd.mkdir()
    window.show_running("May-A", rd)
    window.resize(980, 660)
    window.show()
    card = window.machine_card
    card.set_hidden(True)
    assert (card.badge.text, card.badge.tone) == (HIDDEN, BadgeTone.MUTED)
    translator = ShoutTranslator()
    QCoreApplication.installTranslator(translator)
    try:
        qtbot.waitUntil(lambda: card.badge.text == shout(HIDDEN), timeout=3000)
        assert card.badge.tone == BadgeTone.MUTED
        qtbot.waitUntil(lambda: shout(HIDDEN_DESCRIPTION) in descriptions(card), timeout=3000)
        # Đổi trạng thái khi đang ở ngôn ngữ mới: chữ mới cũng đã dịch.
        card.set_hidden(False)
        assert card.badge.text == shout(READY)
        card.set_hidden(True)
        assert card.badge.text == shout(HIDDEN)
    finally:
        QCoreApplication.removeTranslator(translator)
    qtbot.waitUntil(lambda: card.badge.text == HIDDEN, timeout=3000)
    assert card.badge.tone == BadgeTone.MUTED
    qtbot.waitUntil(lambda: HIDDEN_DESCRIPTION in descriptions(card), timeout=3000)
    assert shout(HIDDEN_DESCRIPTION) not in descriptions(card)


# =================================================================================================
# Nối vào app (node thật trên 127.0.0.1)
# =================================================================================================


def _started(run) -> bool:
    return bool(run.hosts) and run.hosts[-1].running


def _loaded(run) -> bool:
    app = run.tray.parent() if run.trays else None
    return app is not None and app.settings.page.name_edit.isEnabled()


@pytest.mark.integration
def test_hidden_badge_follows_visibility_from_settings(gui, gui_wait, config_dir, ui_helpers):
    """Đã lưu `visible: false` → chạy lên là “Đang ẩn”; bật hiện trong Cài đặt → “Sẵn sàng nhận”;
    tắt lại → “Đang ẩn”."""
    from PySide6.QtWidgets import QApplication

    from teleport.ui.settings import Section

    d = config_dir("A")
    (d / "settings.json").write_text(json.dumps({"visible": False}), encoding="utf-8")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: _started(run) and _loaded(run), 15, "node chạy, trang Cài đặt nạp xong")
        window = run.window
        page = run.tray.parent().settings.page
        yield gui_wait(lambda: window.badge.text == HIDDEN, 5, "huy hiệu Đang ẩn lúc khởi động")
        seen["start"] = (window.badge.text, window.badge.tone)
        window.settings_button.click()
        page.show_section(Section.PRIVACY)
        QApplication.processEvents()
        page.visible_switch.click()  # hiện
        yield gui_wait(lambda: window.badge.text == READY, 5, "huy hiệu Sẵn sàng nhận sau khi hiện")
        seen["shown"] = (window.badge.text, window.badge.tone)
        page.visible_switch.click()  # ẩn lại
        yield gui_wait(lambda: window.badge.text == HIDDEN, 5, "huy hiệu Đang ẩn sau khi ẩn lại")
        seen["hidden"] = (window.badge.text, window.badge.tone)
        [action] = [
            a for a in run.tray.actions() if not a.isSeparator() and ui_helpers.plain(a.text()) == "Thoát"
        ]
        action.trigger()

    run = gui(["--config-dir", str(d)], script)
    from teleport.ui.widgets import BadgeTone

    assert seen["start"] == (HIDDEN, BadgeTone.MUTED)
    assert seen["shown"] == (READY, BadgeTone.SUCCESS)
    assert seen["hidden"] == (HIDDEN, BadgeTone.MUTED)
    assert run.code == 0
