"""Kéo thả file lên danh sách cuộc trò chuyện (spec B2 mục 2.5, 2.6, 12.7; T-D1…D6).

Viết từ spec, không từ code; dời và sửa từ `test_radar_drop.py` cũ (bỏ ca “chưa ghép đôi không trong
mạng”: danh sách chỉ có máy đã ghép đôi). Sự kiện kéo thả thật (`QDragEnterEvent`, `QDragMoveEvent`,
`QDropEvent`) gửi tới viewport của `list_view`; seam `drop_files(device_id | None, urls)` và
`preview_drop(device_id | None, count)`. D5 (lỗi soat-code A1 của radar cũ: viền thả trùng nền hàng chọn)
đọc màu bằng `grab()` của `MainWindow`, sáng + tối. Không socket.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QPoint, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import QColor, QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

DROP_HINT = "Thả file lên một thiết bị để gửi."  # mvp mục 8 (`radar.drop_hint_text`)


def device_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:32]


def dview(label: str, *, paired: bool = True, online: bool = True):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id(label),
        name=label,
        os="windows" if online else None,
        paired=paired,
        online=online,
        connected=False,
        addresses=("192.0.2.7",) if online else (),
        port=47001 if online else None,
    )


def urls(*paths: Path) -> list[QUrl]:
    return [QUrl.fromLocalFile(str(p)) for p in paths]


def mime(*qurls) -> QMimeData:
    m = QMimeData()
    m.setUrls(list(qurls))
    return m


def settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


_COPY = Qt.DropAction.CopyAction
_LEFT = Qt.MouseButton.LeftButton
_NOMOD = Qt.KeyboardModifier.NoModifier


def drag_enter(target, pos: QPoint, data) -> bool:
    ev = QDragEnterEvent(pos, _COPY, data, _LEFT, _NOMOD)
    ev.setAccepted(False)
    QApplication.sendEvent(target, ev)
    return ev.isAccepted()


def drag_move(target, pos: QPoint, data) -> bool:
    ev = QDragMoveEvent(pos, _COPY, data, _LEFT, _NOMOD)
    ev.setAccepted(False)
    QApplication.sendEvent(target, ev)
    return ev.isAccepted()


def drop(target, pos: QPoint, data) -> None:
    QApplication.sendEvent(target, QDropEvent(QPointF(pos), _COPY, data, _LEFT, _NOMOD))


def drag_leave(target) -> None:
    QApplication.sendEvent(target, QDragLeaveEvent())


class Conv:
    def __init__(self, qtbot) -> None:
        from teleport.ui.conversations import ConversationList
        from teleport.ui.radar import DeviceListModel

        self.host = QWidget()
        qtbot.addWidget(self.host)
        self.devices = DeviceListModel(self.host)
        self.widget = ConversationList(self.devices, self.host)
        QVBoxLayout(self.host).addWidget(self.widget)
        self.host.resize(320, 600)
        self.sends: list[tuple[str, list[Path]]] = []
        self.hints: list[str] = []
        self.widget.send_files_requested.connect(lambda i, p: self.sends.append((i, [Path(x) for x in p])))
        self.widget.drop_hint_requested.connect(self.hints.append)

    @property
    def lst(self):
        return self.widget.list_view

    @property
    def model(self):
        return self.widget.model

    def pos(self, did: str) -> QPoint:
        return self.lst.visualRect(self.model.index(self.model.row_of(did), 0)).center()

    def selected_ids(self) -> list[str]:
        from teleport.ui.conversations import DEVICE_ID_ROLE

        return [i.data(DEVICE_ID_ROLE) for i in self.lst.selectionModel().selectedIndexes()]


@pytest.fixture
def conv(qtbot):
    c = Conv(qtbot)
    c.host.show()
    qtbot.waitExposed(c.host)
    return c


# =================================================================================================
# D1: thả lên hàng
# =================================================================================================


def test_drop_files_on_row_emits_paths_and_selects_row(conv, tmp_path):
    x, y = dview("May-X"), dview("May-Y")
    conv.devices.reset([x, y])
    a, b = tmp_path / "a.txt", tmp_path / "thư mục" / "b c.bin"  # không cần có trên đĩa
    assert conv.widget.drop_files(y.device_id, urls(a, b))
    assert conv.sends == [(y.device_id, [a, b])]
    assert conv.selected_ids() == [y.device_id]


def test_drop_files_skips_non_local_urls(conv, tmp_path):
    x = dview("May-X")
    conv.devices.reset([x])
    a = tmp_path / "a.txt"
    conv.widget.drop_files(x.device_id, [QUrl("https://example.com/x.zip"), *urls(a), QUrl("ftp://h/y")])
    assert conv.sends == [(x.device_id, [a])]


def test_drop_with_only_non_local_urls_sends_nothing(conv):
    x = dview("May-X")
    conv.devices.reset([x])
    assert not conv.widget.drop_files(x.device_id, [QUrl("https://example.com/x.zip")])
    assert conv.sends == []
    assert conv.hints == []


def test_drop_on_offline_paired_row_still_emits(conv, tmp_path):
    p = dview("May-P", online=False)
    conv.devices.reset([p])
    a = tmp_path / "a.txt"
    assert conv.widget.drop_files(p.device_id, urls(a))
    assert conv.sends == [(p.device_id, [a])]


def test_real_drop_event_on_row_emits_send_files_requested(conv, tmp_path):
    x, y = dview("May-X"), dview("May-Y")
    conv.devices.reset([x, y])
    vp = conv.lst.viewport()
    pos = conv.pos(y.device_id)
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    data = mime(*urls(a, b))
    assert drag_enter(vp, pos, data)
    assert drag_move(vp, pos, data)
    drop(vp, pos, data)
    settle()
    assert conv.sends == [(y.device_id, [a, b])]
    assert conv.selected_ids() == [y.device_id]


def test_drag_enter_accepts_local_files_and_refuses_web_links(conv, tmp_path):
    conv.devices.reset([dview("May-X")])
    vp = conv.lst.viewport()
    pos = conv.pos(device_id("May-X"))
    assert drag_enter(vp, pos, mime(*urls(tmp_path / "a.txt")))
    assert not drag_enter(vp, pos, mime(QUrl("https://example.com/x.zip")))


# =================================================================================================
# D2: thả vào chỗ trống, nút “＋”
# =================================================================================================


def test_drop_files_on_nothing_or_unknown_device_hints(conv, tmp_path):
    from teleport.ui.radar import drop_hint_text

    conv.devices.reset([dview("May-X"), dview("May-U", paired=False)])
    assert not conv.widget.drop_files(None, urls(tmp_path / "a.txt"))
    assert not conv.widget.drop_files(device_id("May-U"), urls(tmp_path / "a.txt"))
    assert not conv.widget.drop_files(device_id("khong-co"), urls(tmp_path / "a.txt"))
    assert conv.sends == []
    assert conv.hints == [drop_hint_text()] * 3
    assert drop_hint_text() == DROP_HINT


def test_real_drop_on_add_button_hints(conv, tmp_path):
    conv.devices.reset([dview("May-X")])
    btn = conv.widget.add_button
    pos = btn.rect().center()
    data = mime(*urls(tmp_path / "a.txt"))
    drag_enter(btn, pos, data)
    drag_move(btn, pos, data)
    drop(btn, pos, data)
    settle()
    assert conv.sends == []
    assert conv.hints == [DROP_HINT]


def test_real_drop_on_empty_space_below_list_hints(conv, tmp_path):
    conv.devices.reset([dview("May-X")])
    w = conv.widget
    btn = w.add_button
    pos = QPoint(w.width() // 2, btn.geometry().bottom() + 20)
    assert pos.y() < w.height(), "tiền đề: có chỗ trống dưới nút “＋”"
    data = mime(*urls(tmp_path / "a.txt"))
    drag_enter(w, pos, data)
    drag_move(w, pos, data)
    drop(w, pos, data)
    settle()
    assert conv.sends == []
    assert conv.hints == [DROP_HINT]


def test_real_drop_on_empty_list_hints(conv, tmp_path):
    w = conv.widget
    pos = w.add_button.geometry().center()
    data = mime(*urls(tmp_path / "a.txt"))
    drag_enter(w, pos, data)
    drop(w, pos, data)
    settle()
    assert conv.hints == [DROP_HINT]


# =================================================================================================
# D3: đang kéo qua hàng
# =================================================================================================


def test_drag_over_row_sets_drop_target_and_count_then_leave_clears(conv, tmp_path):
    x, y = dview("May-X"), dview("May-Y")
    conv.devices.reset([x, y])
    lst, vp = conv.lst, conv.lst.viewport()
    conv.widget.select_device(x.device_id)
    data = mime(*urls(tmp_path / "a", tmp_path / "b", tmp_path / "c"), QUrl("https://example.com/d"))
    pos = conv.pos(y.device_id)
    assert drag_enter(vp, pos, data)
    assert drag_move(vp, pos, data)
    assert lst.drop_target() == y.device_id
    assert lst.drop_count() == 3, "chỉ đếm file cục bộ"
    assert conv.selected_ids() == [x.device_id], "chỉ kéo qua thì không đổi hàng đang chọn"
    drag_leave(vp)
    assert lst.drop_target() is None


def test_preview_drop_seam(conv):
    x = dview("May-X")
    conv.devices.reset([x])
    conv.lst.preview_drop(x.device_id, 4)
    assert conv.lst.drop_target() == x.device_id
    assert conv.lst.drop_count() == 4
    conv.lst.preview_drop(None, 0)
    assert conv.lst.drop_target() is None


# =================================================================================================
# D4: hàng dời chỗ khi đang kéo
# =================================================================================================

NOW = 1_800_000_000.0


def _busy(did: str):
    from teleport.app.transfers import Direction, TransferKey, TransferStatus
    from teleport.ui.activity import ActiveTransfer, DeviceActivity

    a = ActiveTransfer(
        key=TransferKey(did, Direction.SEND, "a" * 32),
        status=TransferStatus.TRANSFERRING,
        file_count=1,
        first_name="x",
        shown_bytes=1,
        total_bytes=10,
        started_at=NOW,
    )
    return DeviceActivity(active=(a,), last=None)


@pytest.mark.parametrize("how", ["activity", "reset", "remove"])
def test_drop_target_follows_row_under_cursor_when_rows_move(conv, tmp_path, how):
    a, b, c = dview("May-A"), dview("May-B"), dview("May-C")
    conv.devices.reset([a, b, c])
    lst, vp = conv.lst, conv.lst.viewport()
    data = mime(*urls(tmp_path / "x"))
    pos = conv.pos(a.device_id)  # hàng 0
    drag_enter(vp, pos, data)
    drag_move(vp, pos, data)
    assert lst.drop_target() == a.device_id
    if how == "activity":
        conv.model.set_activity(c.device_id, _busy(c.device_id))  # C lên đầu
    elif how == "reset":
        conv.devices.reset([dview("May-0"), a, b, c])  # thêm hàng phía trên
    else:
        conv.devices.reset([b, c])
    settle()
    under = lst.indexAt(pos)
    from teleport.ui.conversations import DEVICE_ID_ROLE

    expected = under.data(DEVICE_ID_ROLE) if under.isValid() else None
    assert expected != a.device_id, "tiền đề: hàng dưới con trỏ đã đổi"
    assert lst.drop_target() == expected
    drop(vp, pos, data)
    settle()
    assert conv.sends == [(expected, [tmp_path / "x"])]


# =================================================================================================
# D5: màu viền thả (lỗi cũ A1)
# =================================================================================================

MODES = pytest.mark.parametrize("dark", [False, True], ids=["sang", "toi"])


def _close(a: QColor, b: QColor, tol: int) -> bool:
    return (
        abs(a.red() - b.red()) <= tol
        and abs(a.green() - b.green()) <= tol
        and abs(a.blue() - b.blue()) <= tol
    )


def _has(image, rect: QRect, color: QColor, tol: int = 16) -> bool:
    dpr = image.devicePixelRatio()
    for y in range(round(rect.top() * dpr), round((rect.bottom() + 1) * dpr)):
        for x in range(round(rect.left() * dpr), round((rect.right() + 1) * dpr)):
            if (
                0 <= x < image.width()
                and 0 <= y < image.height()
                and _close(image.pixelColor(x, y), color, tol)
            ):
                return True
    return False


def _row_rect(window, did: str) -> QRect:
    lst = window.conversations.list_view
    m = window.conversations.model
    rect = lst.visualRect(m.index(m.row_of(did), 0))
    return QRect(lst.viewport().mapTo(window, rect.topLeft()), rect.size())


def _edges(rect: QRect) -> list[QRect]:
    """Dải 4 px ở mép trên và mép dưới của hàng (bỏ góc bo), nơi chỉ có nền và viền."""
    return [
        QRect(rect.left() + 14, rect.top(), rect.width() - 28, 4),
        QRect(rect.left() + 14, rect.bottom() - 3, rect.width() - 28, 4),
    ]


@pytest.fixture
def window(qtbot, tmp_path):
    from teleport.ui.main_window import MainWindow

    w = MainWindow()
    qtbot.addWidget(w)
    w.show_running("May-A", tmp_path / "nhan")
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    w.resize(980, 660)
    return w


@MODES
def test_drop_border_on_selected_row_uses_text_not_accent_strong(window, qtbot, ui_theme, dark):
    tok = ui_theme(dark)
    sel, other = dview("A-Sel"), dview("B-Other")
    window.devices.reset([sel, other])
    window.show()
    qtbot.waitExposed(window)
    assert window.conversations.select_device(sel.device_id)
    lst = window.conversations.list_view

    lst.preview_drop(sel.device_id, 3)
    settle()
    image = window.grab().toImage()
    edges = _edges(_row_rect(window, sel.device_id))
    assert any(_has(image, e, tok.text) for e in edges), "viền thả trên hàng đang chọn phải màu text"
    assert not any(_has(image, e, tok.accent_strong, 10) for e in edges), (
        "không dùng accent_strong trên hàng chọn"
    )

    lst.preview_drop(other.device_id, 3)
    settle()
    image = window.grab().toImage()
    edges = _edges(_row_rect(window, other.device_id))
    assert any(_has(image, e, tok.accent_strong) for e in edges), "hàng không chọn: viền accent_strong"
    assert not any(_has(image, e, tok.accent_strong, 10) for e in _edges(_row_rect(window, sel.device_id))), (
        "viền chỉ ở hàng dưới con trỏ"
    )


@MODES
def test_drop_preview_draws_drop_line_in_accent_text(window, qtbot, ui_theme, dark):
    """Dòng phụ thay bằng “Thả để gửi n file” màu `accent_text` (máy không bận: bình thường `text_muted`)."""
    tok = ui_theme(dark)
    x = dview("May-X")
    window.devices.reset([x])
    window.show()
    qtbot.waitExposed(window)
    lst = window.conversations.list_view
    r = _row_rect(window, x.device_id)
    text_block = QRect(r.left() + 60, r.top() + r.height() // 2, r.width() - 70, r.height() // 2 - 2)
    settle()
    assert not _has(window.grab().toImage(), text_block, tok.accent_text, 10)
    lst.preview_drop(x.device_id, 2)
    settle()
    assert _has(window.grab().toImage(), text_block, tok.accent_text, 16)


# =================================================================================================
# D6: kéo không chạm đĩa
# =================================================================================================


def test_dragging_does_not_stat_files(conv, tmp_path, monkeypatch):
    x = dview("May-X")
    conv.devices.reset([x])
    files = [tmp_path / "a.txt", tmp_path / "b.txt"]
    for f in files:
        f.write_bytes(b"x")
    watched = {os.path.normcase(str(f)) for f in files}
    calls: list[str] = []
    real_os_stat = os.stat
    real_path_stat = pathlib.Path.stat

    def os_stat(path, *a, **k):
        if os.path.normcase(os.fspath(path)) in watched:
            calls.append(f"os.stat({path})")
        return real_os_stat(path, *a, **k)

    def path_stat(self, *a, **k):
        if os.path.normcase(str(self)) in watched:
            calls.append(f"Path.stat({self})")
        return real_path_stat(self, *a, **k)

    monkeypatch.setattr(os, "stat", os_stat)
    monkeypatch.setattr(pathlib.Path, "stat", path_stat)
    vp = conv.lst.viewport()
    pos = conv.pos(x.device_id)
    data = mime(*urls(*files))
    drag_enter(vp, pos, data)
    for _ in range(3):
        drag_move(vp, pos, data)
    conv.lst.repaint()
    drag_leave(vp)
    settle()
    assert calls == []
