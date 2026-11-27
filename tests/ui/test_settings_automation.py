"""Kiểm thử Đợt 3 cho trang Cài đặt: sửa quy tắc/cổng đẩy, chọn nhanh loại file và tạm dừng tất cả."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLineEdit, QPushButton

from teleport.app.devices import DeviceView
from teleport.core.routing import RoutingRule
from teleport.core.watch import WatchPortalRule
from teleport.ui.radar import DeviceListModel
from teleport.ui.settings import Section, SettingsPage

PEER_ID = "a" * 32


def _make_page(qtbot, tmp_path: Path) -> SettingsPage:
    model = DeviceListModel()
    model.reset(
        [
            DeviceView(
                device_id=PEER_ID,
                name="Laptop",
                os="windows",
                paired=True,
                online=True,
                connected=True,
                addresses=("192.168.1.10",),
                port=47474,
            )
        ]
    )
    page = SettingsPage(model)
    qtbot.addWidget(page)
    page.set_loaded(True)
    page.show()
    return page


def test_edit_routing_rule_with_presets_preserves_id_and_enabled(qtbot, tmp_path: Path) -> None:
    page = _make_page(qtbot, tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    orig = RoutingRule(
        id="keep-id-1", name="Cu", destination=str(dest), device_id=None, patterns=(), enabled=False
    )
    page.set_routing_rules([orig])

    emitted: list[tuple[RoutingRule, ...]] = []
    page.routing_rules_changed.connect(emitted.append)

    edit_btn = page.routing_list.findChild(QPushButton, "routingRuleRowEdit")
    assert edit_btn is not None
    edit_btn.click()

    dlg = page._rule_dialog
    assert isinstance(dlg, QDialog)
    name_edit = dlg.findChild(QLineEdit, "ruleNameEdit")
    pat_edit = dlg.findChild(QLineEdit, "rulePatternsEdit")
    img_preset = dlg.findChild(QPushButton, "rulePreset_images")
    all_preset = dlg.findChild(QPushButton, "rulePreset_all")
    assert name_edit is not None and pat_edit is not None
    assert img_preset is not None and all_preset is not None
    assert name_edit.text() == "Cu"

    img_preset.click()
    assert "*.jpg" in pat_edit.text()
    all_preset.click()
    assert pat_edit.text() == ""
    img_preset.click()
    name_edit.setText("Anh chup")

    buttons = dlg.findChild(QDialogButtonBox)
    assert buttons is not None
    ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok_btn is not None
    ok_btn.click()

    assert len(emitted) == 1
    updated = emitted[0][0]
    assert updated.id == "keep-id-1"
    assert updated.enabled is False
    assert updated.name == "Anh chup"
    assert "*.png" in updated.patterns


def test_watch_portal_edit_and_pause_all_toggle(qtbot, tmp_path: Path) -> None:
    page = _make_page(qtbot, tmp_path)
    src = tmp_path / "watch"
    src.mkdir()
    portal = WatchPortalRule(
        id="wp-1",
        name="Day nhac",
        source_dir=str(src),
        device_id=PEER_ID,
        patterns=("*.mp3",),
        recursive=False,
        enabled=True,
    )
    page.show_section(Section.TRANSFER)
    page.set_watch_portals([portal])
    assert page.watch_pause_button.isVisibleTo(page)

    paused_events: list[bool] = []
    page.watch_portals_paused_toggled.connect(paused_events.append)
    page.watch_pause_button.click()
    assert paused_events == [True]
    assert page.watch_pause_button.isChecked()
    assert "Tiếp tục" in page.watch_pause_button.text()

    page.set_watch_portals_paused(False)
    assert not page.watch_pause_button.isChecked()
    assert "Tạm dừng" in page.watch_pause_button.text()
    assert len(paused_events) == 1

    edit_btn = page.watch_list.findChild(QPushButton, "watchPortalRowEdit")
    assert edit_btn is not None
    edit_btn.click()
    dlg = page._rule_dialog
    assert isinstance(dlg, QDialog)
    name_edit = dlg.findChild(QLineEdit, "ruleNameEdit")
    assert name_edit is not None and name_edit.text() == "Day nhac"
    dlg.reject()
