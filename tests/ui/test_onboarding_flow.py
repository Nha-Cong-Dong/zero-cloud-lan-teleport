"""Kiểm thử hành trình 60 giây đầu, thẻ phát hiện máy sống và kéo thả màn rỗng (Đợt 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QWidget


def _device(label: str, *, paired: bool = False, online: bool = True):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=("a1b2c3d4" * 4)[:32] if label == "PC-A" else ("f1e2d3c4" * 4)[:32],
        name=label,
        os="windows",
        paired=paired,
        online=online,
        connected=paired and online,
        addresses=("192.0.2.10",) if online else (),
        port=47474 if online else None,
    )


def test_empty_state_secondary_action_and_extra_widget(qtbot):
    from teleport.ui.widgets import EmptyState

    w = EmptyState()
    qtbot.addWidget(w)
    w.set_content("arrow_swap", "Tiêu đề", "Mô tả", action="Chính")
    assert not w.secondary_button.isVisible()
    w.set_secondary_action("Phụ")
    w.show()
    QApplication.processEvents()
    assert w.secondary_button.isVisible()

    hits: list[str] = []
    w.secondary_action_triggered.connect(lambda: hits.append("sec"))
    w.secondary_button.click()
    assert hits == ["sec"]


def test_nearby_card_appears_when_unpaired_device_online_and_quick_pairs(qtbot, tmp_path):
    from teleport.ui.main_window import MainWindow, Page

    win = MainWindow()
    qtbot.addWidget(win)
    win.add_page(Page.WEB_GUEST, QWidget())
    win.resize(900, 620)
    win.show()
    qtbot.waitExposed(win)
    win.show_running("May-Chinh", tmp_path)
    win.set_transfer_summary(False, 0)
    QApplication.processEvents()

    assert win.empty_state.secondary_button.isVisible()
    assert not win.nearby_card.isVisible()
    assert win.onboarding_card.isVisible()

    # Thấy máy mới trong LAN → nearbyCard mở ra
    peer = _device("PC-A", paired=False, online=True)
    win.devices.reset([peer])
    QApplication.processEvents()
    assert win.nearby_card.isVisible()
    assert "PC-A" in win.nearby_label.text()

    paired_reqs: list[object] = []
    win.quick_pair_requested.connect(paired_reqs.append)
    win.nearby_pair_button.click()
    assert paired_reqs == [peer]

    # Đóng onboardingCard
    dismissed: list[bool] = []
    win.onboarding_dismissed_requested.connect(lambda: dismissed.append(True))
    win.onboarding_close_button.click()
    QApplication.processEvents()
    assert dismissed == [True]
    assert not win.onboarding_card.isVisible()


def test_empty_state_drop_emits_files_for_web_guest(qtbot, tmp_path):
    from teleport.ui.main_window import MainWindow, Page

    win = MainWindow()
    qtbot.addWidget(win)
    win.add_page(Page.WEB_GUEST, QWidget())
    win.resize(900, 620)
    win.show()
    qtbot.waitExposed(win)
    win.show_running("May-Chinh", tmp_path)
    win.set_transfer_summary(False, 0)
    QApplication.processEvents()

    f = tmp_path / "demo.txt"
    f.write_text("hi", encoding="utf-8")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(f))])

    dropped: list[list[Path]] = []
    win.empty_drop_files_requested.connect(dropped.append)

    pos = QPointF(win.empty_state.rect().center())
    enter = QDragEnterEvent(
        pos.toPoint(),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(win.empty_state, enter)
    assert enter.isAccepted()
    assert win._empty_drop_overlay.shown

    drop = QDropEvent(
        pos,
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(win.empty_state, drop)
    assert drop.isAccepted()
    assert not win._empty_drop_overlay.shown
    assert dropped == [[f]]
