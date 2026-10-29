"""Kéo thả file và mục “Gửi file…” của radar (spec PR3 pha 2a, mục “`ui/radar.py`” và “Test”).

Viết từ spec, không từ code. Radar đứng riêng (`teleport.ui.radar.RadarView`), nạp thiết bị bằng
`model.reset`. Seam của spec: `drop_files(device_id | None, urls)` (dropEvent gọi nó); thêm hai test
gửi sự kiện kéo thả thật (`QDragEnterEvent`, `QDropEvent`) vào danh sách, để chắc dropEvent thật sự nối
tới `drop_files`. Không socket.

Gợi ý khi thả vào chỗ trống: spec cho chọn “signal `drop_hint_requested` hoặc controller”; test ở đây
nhận cả hai cách (signal phát ra, hoặc câu hiện ngay trên radar). Đường đi qua controller nằm ở
`test_transfers_controller.py::test_drop_on_empty_radar_space_shows_hint`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

DROP_HINT = "Thả file lên một thiết bị để gửi."  # câu mới (spec PR3 pha 2a)
SEND_FILES = "Gửi file…"  # mục menu mới (spec PR3 pha 2a)


def device_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:32]


def view(label: str, *, paired: bool = True, online: bool = True, connected: bool = False):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id(label),
        name=label,
        os="windows" if online else None,
        paired=paired,
        online=online,
        connected=connected and paired,
        addresses=("192.0.2.7",) if online else (),
        port=47001 if online else None,
    )


class Spy:
    def __init__(self, radar) -> None:
        self.sends: list[tuple[str, list[Path]]] = []
        self.picks: list[str] = []
        self.hints: list[tuple] = []
        radar.send_files_requested.connect(lambda i, p: self.sends.append((i, [Path(x) for x in p])))
        if hasattr(radar, "pick_files_requested"):
            radar.pick_files_requested.connect(lambda i: self.picks.append(i))
        if hasattr(radar, "drop_hint_requested"):
            radar.drop_hint_requested.connect(lambda *a: self.hints.append(a))


@pytest.fixture
def radar(qtbot):
    from teleport.ui.radar import RadarView

    w = RadarView()
    qtbot.addWidget(w)
    w.resize(480, 360)
    w.show()
    return w


def urls(*paths: Path):
    from PySide6.QtCore import QUrl

    return [QUrl.fromLocalFile(str(p)) for p in paths]


def hint_shown(radar, spy: Spy, all_text) -> bool:
    return bool(spy.hints) or DROP_HINT in all_text(radar)


# =================================================================================================
# drop_files
# =================================================================================================


def test_drop_files_on_paired_device_emits_device_id_and_both_paths(radar, tmp_path):
    x = view("May-X")
    radar.model.reset([x])
    spy = Spy(radar)
    a, b = tmp_path / "a.txt", tmp_path / "thư mục" / "b c.bin"  # không cần có trên đĩa
    radar.drop_files(x.device_id, urls(a, b))
    assert spy.sends == [(x.device_id, [a, b])]


def test_drop_files_skips_non_local_urls(radar, tmp_path):
    from PySide6.QtCore import QUrl

    x = view("May-X")
    radar.model.reset([x])
    spy = Spy(radar)
    a = tmp_path / "a.txt"
    radar.drop_files(x.device_id, [QUrl("https://example.com/x.zip"), *urls(a), QUrl("ftp://h/y")])
    assert spy.sends == [(x.device_id, [a])]


def test_drop_with_only_non_local_urls_sends_nothing(radar):
    from PySide6.QtCore import QUrl

    x = view("May-X")
    radar.model.reset([x])
    spy = Spy(radar)
    radar.drop_files(x.device_id, [QUrl("https://example.com/x.zip")])
    assert spy.sends == []


def test_drop_on_unpaired_online_device_still_emits(radar, tmp_path):
    """Controller lo “ghép rồi gửi”: radar vẫn phát cho thiết bị chưa ghép đôi đang online."""
    u = view("May-U", paired=False)
    radar.model.reset([u])
    spy = Spy(radar)
    a = tmp_path / "a.txt"
    radar.drop_files(u.device_id, urls(a))
    assert spy.sends == [(u.device_id, [a])]


def test_drop_on_paired_offline_device_still_emits(radar, tmp_path):
    """Mục 10 A2: chưa kết nối mà không bị chặn vẫn gửi (hàng chờ kết nối)."""
    p = view("May-P", online=False)
    radar.model.reset([p])
    spy = Spy(radar)
    a = tmp_path / "a.txt"
    radar.drop_files(p.device_id, urls(a))
    assert spy.sends == [(p.device_id, [a])]


def test_drop_on_unpaired_offline_device_is_ignored(radar, tmp_path):
    u = view("May-U", paired=False, online=False)
    radar.model.reset([u])
    spy = Spy(radar)
    radar.drop_files(u.device_id, urls(tmp_path / "a.txt"))
    assert spy.sends == []


def test_drop_on_unknown_device_sends_nothing(radar, tmp_path):
    radar.model.reset([view("May-X")])
    spy = Spy(radar)
    radar.drop_files(device_id("khong-co"), urls(tmp_path / "a.txt"))
    assert spy.sends == []


def test_drop_on_empty_space_shows_hint_and_sends_nothing(radar, tmp_path, ui_helpers):
    radar.model.reset([view("May-X")])
    spy = Spy(radar)
    radar.drop_files(None, urls(tmp_path / "a.txt"))
    assert spy.sends == []
    assert hint_shown(radar, spy, ui_helpers.all_text), f"thả vào chỗ trống phải gợi ý “{DROP_HINT}”"


# =================================================================================================
# Sự kiện kéo thả thật trên danh sách
# =================================================================================================


def _list_view(radar):
    from PySide6.QtWidgets import QAbstractItemView

    [lst] = [v for v in radar.findChildren(QAbstractItemView) if v.model() is not None]
    return lst


def _mime(*qurls):
    from PySide6.QtCore import QMimeData

    m = QMimeData()
    m.setUrls(list(qurls))
    return m


def _drag_enter(target, pos, mime) -> bool:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QDragEnterEvent
    from PySide6.QtWidgets import QApplication

    ev = QDragEnterEvent(
        pos, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    )
    ev.setAccepted(False)
    QApplication.sendEvent(target, ev)
    return ev.isAccepted()


def test_drag_enter_accepts_local_files_and_refuses_web_links(radar, tmp_path):
    from PySide6.QtCore import QUrl

    x = view("May-X")
    radar.model.reset([x])
    lst = _list_view(radar)
    target = lst.viewport()
    pos = lst.visualRect(lst.model().index(0, 0)).center()
    assert _drag_enter(target, pos, _mime(*urls(tmp_path / "a.txt"))), (
        "file cục bộ phải được nhận khi kéo vào"
    )
    assert not _drag_enter(target, pos, _mime(QUrl("https://example.com/x.zip"))), "chỉ URL web: không nhận"


def test_real_drop_event_on_row_emits_send_files_requested(radar, tmp_path):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDropEvent
    from PySide6.QtWidgets import QApplication

    x, y = view("May-X"), view("May-Y")
    radar.model.reset([x, y])
    spy = Spy(radar)
    lst = _list_view(radar)
    target = lst.viewport()
    row = radar.model.row_of(y.device_id)
    pos = lst.visualRect(lst.model().index(row, 0)).center()
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    mime = _mime(*urls(a, b))
    assert _drag_enter(target, pos, mime)
    ev = QDropEvent(
        QPointF(pos),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(target, ev)
    assert spy.sends == [(y.device_id, [a, b])], "thả lên hàng nào thì gửi tới thiết bị của hàng đó"


def _drag_move(target, pos, mime) -> bool:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QDragMoveEvent
    from PySide6.QtWidgets import QApplication

    ev = QDragMoveEvent(
        pos, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    )
    ev.setAccepted(False)
    QApplication.sendEvent(target, ev)
    return ev.isAccepted()


def test_drag_move_refuses_row_that_cannot_receive_and_highlights_without_selecting(radar, tmp_path):
    """Hàng chưa ghép đôi không trong mạng: không nhận thả. Hàng gửi tới được: tô, không đổi hàng chọn."""
    ok, off = view("May-A"), view("May-Z", paired=False, online=False)
    radar.model.reset([ok, off])
    lst = _list_view(radar)
    target = lst.viewport()
    mime = _mime(*urls(tmp_path / "a.txt"))
    radar.select_device(off.device_id)
    selected_before = lst.currentIndex().row()

    ok_pos = lst.visualRect(lst.model().index(radar.model.row_of(ok.device_id), 0)).center()
    off_pos = lst.visualRect(lst.model().index(radar.model.row_of(off.device_id), 0)).center()
    assert _drag_enter(target, ok_pos, mime)
    assert _drag_move(target, ok_pos, mime), "hàng gửi tới được: nhận thả"
    assert lst.drop_target() == ok.device_id
    assert lst.currentIndex().row() == selected_before, "chỉ kéo qua thì không đổi hàng đang chọn"

    assert not _drag_move(target, off_pos, mime), "thiết bị chưa ghép đôi không trong mạng: không nhận thả"
    assert lst.drop_target() is None
    assert lst.currentIndex().row() == selected_before


def test_drag_leave_clears_drop_highlight(radar, tmp_path):
    from PySide6.QtGui import QDragLeaveEvent
    from PySide6.QtWidgets import QApplication

    x = view("May-X")
    radar.model.reset([x])
    lst = _list_view(radar)
    target = lst.viewport()
    pos = lst.visualRect(lst.model().index(0, 0)).center()
    mime = _mime(*urls(tmp_path / "a.txt"))
    assert _drag_enter(target, pos, mime)
    assert _drag_move(target, pos, mime)
    assert lst.drop_target() == x.device_id
    QApplication.sendEvent(target, QDragLeaveEvent())
    assert lst.drop_target() is None


# =================================================================================================
# Menu “Gửi file…”
# =================================================================================================


def _actions(menu) -> list:
    return [a for a in menu.actions() if not a.isSeparator()]


def _send_action(radar, did: str):
    menu = radar.context_menu_for(did)
    if menu is None:
        return None
    found = [a for a in _actions(menu) if a.text().replace("&", "") == SEND_FILES]
    assert len(found) <= 1
    return found[0] if found else None


def test_paired_device_menu_has_send_files_that_emits_pick_files_requested(radar):
    x = view("May-X")
    radar.model.reset([x])
    spy = Spy(radar)
    action = _send_action(radar, x.device_id)
    assert action is not None, f"menu thiết bị đã ghép đôi cần mục “{SEND_FILES}”"
    assert action.isEnabled()
    action.trigger()
    assert spy.picks == [x.device_id]
    assert spy.sends == []


def test_paired_offline_device_menu_has_send_files(radar):
    p = view("May-P", online=False)
    radar.model.reset([p])
    assert _send_action(radar, p.device_id) is not None


def test_unpaired_online_device_menu_has_send_files(radar):
    u = view("May-U", paired=False)
    radar.model.reset([u])
    spy = Spy(radar)
    action = _send_action(radar, u.device_id)
    assert action is not None, f"thiết bị chưa ghép đôi đang online cũng có “{SEND_FILES}” (ghép rồi gửi)"
    action.trigger()
    assert spy.picks == [u.device_id]


def test_unpaired_offline_device_has_no_send_files(radar):
    u = view("May-U", paired=False, online=False)
    radar.model.reset([u])
    assert _send_action(radar, u.device_id) is None


def test_tray_send_action_stays_disabled():
    """Spec: không thêm “Gửi file…” vào khay ở pha này (Q8 để sau): mục khay vẫn tắt."""
    from teleport.ui.tray import Tray

    tray = Tray()
    try:
        send = tray.actions()[1]
        assert not send.isEnabled()
    finally:
        tray.dispose()
