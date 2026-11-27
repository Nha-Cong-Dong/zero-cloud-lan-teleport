"""Kiểm thử bấm huy hiệu trạng thái trên `MachineCard` để bật/tắt nhanh."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from teleport.ui.home import MachineCard


def test_machine_card_badge_menu_toggles_pause_and_hidden(qtbot) -> None:
    card = MachineCard()
    qtbot.addWidget(card)
    card.set_name("PC-Nha")
    card.set_status_menu_enabled(True)
    card.show()

    paused: list[bool] = []
    hidden: list[bool] = []
    card.pause_toggled.connect(paused.append)
    card.hidden_toggled.connect(hidden.append)

    QTest.mouseRelease(card.badge, Qt.MouseButton.LeftButton)
    menu = card._status_menu
    assert menu is not None
    acts = menu.actions()
    assert len(acts) == 2
    assert acts[0].isCheckable() and not acts[0].isChecked()
    assert acts[1].isCheckable() and not acts[1].isChecked()

    acts[0].trigger()
    acts[1].trigger()
    assert paused == [True]
    assert hidden == [True]
    menu.close()
