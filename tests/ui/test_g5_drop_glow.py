"""Hàng cột trái sáng lên khi kéo file qua (spec G5 mục 3.2, test 7.2; mục 13 thắng).

Viết từ spec, không từ code. Mặc định chế độ tức thì (autouse `motion_instant`); ca hoạt ảnh thật xin
`real_motion` và hiện cửa sổ trước; chỉ kiểm giá trị cuối (`qtbot.waitUntil`).

1. Tức thì: `preview_drop(id, 3)` → `drop_glow(index) == 1`; đổi hàng → cũ 0, mới 1; bỏ → mọi 0; không
   hoạt ảnh nào.
2. Hoạt ảnh thật: lên 1 rồi về 0; cuối cùng `running_count() == 0`; ẩn view → mọi mức 0 ngay; view không
   hiện → gán ngay; hệ điều hành tắt hiệu ứng → gán ngay (mvp N12).
3. Ảnh (tức thì): hàng không chọn đang kéo qua có nền ≈ `blend(sidebar, accent_subtle, _DROP_GLOW_MAX)`
   (sai số 2), cả dạng thu gọn; hàng chọn giữ `selected`. Hai chế độ × hai màu nhấn.
4. Câu “Thả để gửi n file”, viền và `drop_target()` đổi ngay cả khi hoạt ảnh thật.
5. Tương phản: mọi cặp trong `DROP_GLOW_CONTRAST_PAIRS` đạt ngưỡng ở mọi màu nhấn × sáng/tối; bộ cặp
   chứa đúng các cặp spec 13.14 đòi.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QPoint, QRect, Qt, QUrl
from PySide6.QtGui import QColor, QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

WAIT_MS = 5000
MODES = pytest.mark.parametrize("dark", [False, True], ids=["sang", "toi"])
ACCENTS = pytest.mark.parametrize("accent", ["mint", "rose"])


def device_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:32]


def dview(label: str, *, online: bool = True):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id(label),
        name=label,
        os="windows" if online else None,
        paired=True,
        online=online,
        connected=False,
        addresses=("192.0.2.7",) if online else (),
        port=47001 if online else None,
    )


def settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


def mime(*paths: Path) -> QMimeData:
    m = QMimeData()
    m.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return m


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

    @property
    def lst(self):
        return self.widget.list_view

    @property
    def model(self):
        return self.widget.model

    def index(self, did: str):
        return self.model.index(self.model.row_of(did), 0)

    def glow(self, did: str) -> float:
        return self.lst.drop_glow(self.index(did))

    def pos(self, did: str) -> QPoint:
        return self.lst.visualRect(self.index(did)).center()


@pytest.fixture
def conv(qtbot):
    c = Conv(qtbot)
    c.host.show()
    qtbot.waitExposed(c.host)
    return c


# =================================================================================================
# 7.2.1 Tức thì
# =================================================================================================


def test_instant_glow_follows_drop_row(conv):
    from teleport.ui import motion

    x, y = dview("May-X"), dview("May-Y")
    conv.devices.reset([x, y])
    assert conv.glow(x.device_id) == 0.0 and conv.glow(y.device_id) == 0.0
    conv.lst.preview_drop(x.device_id, 3)
    assert conv.glow(x.device_id) == 1.0
    assert conv.glow(y.device_id) == 0.0
    conv.lst.preview_drop(y.device_id, 3)
    assert conv.glow(x.device_id) == 0.0
    assert conv.glow(y.device_id) == 1.0
    conv.lst.preview_drop(None, 0)
    assert conv.glow(x.device_id) == 0.0 and conv.glow(y.device_id) == 0.0
    assert motion.running_count() == 0


def test_glow_of_invalid_index_is_zero(conv):
    from PySide6.QtCore import QModelIndex

    conv.devices.reset([dview("May-X")])
    assert conv.lst.drop_glow(QModelIndex()) == 0.0


def test_real_drag_over_row_glows_and_leave_clears(conv, tmp_path):
    x = dview("May-X")
    conv.devices.reset([x])
    vp = conv.lst.viewport()
    data = mime(tmp_path / "a.txt")
    pos = conv.pos(x.device_id)
    assert drag_enter(vp, pos, data)
    assert drag_move(vp, pos, data)
    assert conv.glow(x.device_id) == 1.0
    drag_leave(vp)
    assert conv.glow(x.device_id) == 0.0


# =================================================================================================
# 7.2.2 Hoạt ảnh thật
# =================================================================================================


def test_real_motion_glow_rises_then_falls_and_leaves_no_animation(conv, qtbot, real_motion):
    x, y = dview("May-X"), dview("May-Y")
    conv.devices.reset([x, y])
    conv.lst.preview_drop(x.device_id, 3)
    qtbot.waitUntil(lambda: conv.glow(x.device_id) == 1.0, timeout=WAIT_MS)
    conv.lst.preview_drop(y.device_id, 3)
    qtbot.waitUntil(lambda: conv.glow(x.device_id) == 0.0 and conv.glow(y.device_id) == 1.0, timeout=WAIT_MS)
    conv.lst.preview_drop(None, 0)
    qtbot.waitUntil(lambda: conv.glow(y.device_id) == 0.0, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_real_motion_hiding_view_drops_every_glow_at_once(conv, qtbot, real_motion):
    x, y = dview("May-X"), dview("May-Y")
    conv.devices.reset([x, y])
    conv.lst.preview_drop(x.device_id, 3)
    conv.lst.preview_drop(y.device_id, 3)
    conv.host.hide()
    assert conv.glow(x.device_id) == 0.0
    assert conv.glow(y.device_id) == 0.0
    assert real_motion.running_count() == 0, "ẩn view: hoạt ảnh sáng hàng kết thúc ngay (13.18)"
    qtbot.wait(300)  # quá FAST: không hoạt ảnh nào còn chạy ngầm ghi lại mức sáng
    assert conv.glow(x.device_id) == 0.0
    assert conv.glow(y.device_id) == 0.0


def test_real_motion_view_not_shown_sets_glow_at_once(qtbot, real_motion):
    c = Conv(qtbot)  # không hiện
    x = dview("May-X")
    c.devices.reset([x])
    c.lst.preview_drop(x.device_id, 3)
    assert c.glow(x.device_id) == 1.0
    assert real_motion.running_count() == 0
    c.lst.preview_drop(None, 0)
    assert c.glow(x.device_id) == 0.0
    assert real_motion.running_count() == 0


def test_animations_disabled_glow_jumps(conv, real_motion, monkeypatch):
    monkeypatch.setattr(real_motion, "animations_enabled", lambda: False)
    x = dview("May-X")
    conv.devices.reset([x])
    conv.lst.preview_drop(x.device_id, 3)
    assert conv.glow(x.device_id) == 1.0
    assert real_motion.running_count() == 0
    conv.lst.preview_drop(None, 0)
    assert conv.glow(x.device_id) == 0.0
    assert real_motion.running_count() == 0


def test_real_motion_row_removed_while_glowing_leaves_no_animation(conv, qtbot, real_motion):
    """13.7: `rowsRemoved` xóa id mất khỏi `_drop_glow`, huỷ hoạt ảnh bằng `INSTANT`."""
    from teleport.app.node import DeviceOnline

    x, y = dview("May-X"), dview("May-Y")
    conv.devices.reset([x, y])
    conv.lst.preview_drop(x.device_id, 3)
    unpaired = dataclasses.replace(x, paired=False)
    conv.devices.apply(DeviceOnline(unpaired))  # bỏ ghép đôi → proxy lọc ra → rowsRemoved
    settle()
    assert conv.model.row_of(x.device_id) < 0, "tiền đề: hàng đã khỏi danh sách"
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


# =================================================================================================
# 7.2.4 Câu, viền, drop_target đổi ngay
# =================================================================================================


def test_real_motion_drop_target_and_count_change_at_once(conv, real_motion, tmp_path):
    x, y = dview("May-X"), dview("May-Y")
    conv.devices.reset([x, y])
    vp = conv.lst.viewport()
    data = mime(tmp_path / "a", tmp_path / "b", tmp_path / "c")
    assert drag_enter(vp, conv.pos(x.device_id), data)
    assert drag_move(vp, conv.pos(x.device_id), data)
    assert conv.lst.drop_target() == x.device_id
    assert conv.lst.drop_count() == 3
    assert drag_move(vp, conv.pos(y.device_id), data)
    assert conv.lst.drop_target() == y.device_id
    drag_leave(vp)
    assert conv.lst.drop_target() is None


# =================================================================================================
# Ảnh (MainWindow, như D5 của test_conversations_drop.py)
# =================================================================================================


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


def _pixel(image, point: QPoint) -> QColor:
    dpr = image.devicePixelRatio()
    return image.pixelColor(round(point.x() * dpr), round(point.y() * dpr))


def _row_rect(window, did: str) -> QRect:
    lst = window.conversations.list_view
    m = window.conversations.model
    rect = lst.visualRect(m.index(m.row_of(did), 0))
    return QRect(lst.viewport().mapTo(window, rect.topLeft()), rect.size())


def _quiet_point(rect: QRect) -> QPoint:
    """Điểm trong nền hàng (dạng đầy đủ) không có chữ, avatar hay viền: sát mép phải, giữa hàng.

    Chữ dừng ở `right - _PADDING` (8 px); viền thả 2 px nằm ở 1–3 px từ mép.
    """
    return QPoint(rect.right() - 5, rect.center().y())


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
@ACCENTS
def test_glowing_row_background_is_blend_of_sidebar_and_accent_subtle(
    window, qtbot, ui_theme, monkeypatch, dark, accent
):
    from teleport.ui import conversations, theme

    monkeypatch.setattr(theme, "_accent", accent)
    tok = ui_theme(dark)
    sel, other = dview("A-Sel"), dview("B-Other")
    window.devices.reset([sel, other])
    window.show()
    qtbot.waitExposed(window)
    assert window.conversations.select_device(sel.device_id)
    lst = window.conversations.list_view
    settle()

    point = _quiet_point(_row_rect(window, other.device_id))
    before = _pixel(window.grab().toImage(), point)
    assert _close(before, tok.sidebar, 2), f"tiền đề: hàng không chọn nền sidebar, thấy {before.name()}"

    lst.preview_drop(other.device_id, 3)
    settle()
    image = window.grab().toImage()
    expected = theme.blend(tok.sidebar, tok.accent_subtle, conversations._DROP_GLOW_MAX)
    got = _pixel(image, point)
    assert _close(got, expected, 2), f"nền sáng: mong {expected.name()}, thấy {got.name()}"
    sel_point = _quiet_point(_row_rect(window, sel.device_id))
    assert _close(_pixel(image, sel_point), tok.selected, 2), "hàng chọn không bị phủ khi kéo qua hàng khác"

    lst.preview_drop(sel.device_id, 3)
    settle()
    image = window.grab().toImage()
    got_sel = _pixel(image, sel_point)
    assert _close(got_sel, tok.selected, 2), f"hàng chọn giữ selected khi kéo qua, thấy {got_sel.name()}"
    assert _close(_pixel(image, point), tok.sidebar, 2), "hàng cũ về sidebar"

    lst.preview_drop(None, 0)
    settle()
    assert _close(_pixel(window.grab().toImage(), sel_point), tok.selected, 2)


@MODES
def test_glow_applies_in_compact_form(window, qtbot, ui_theme, dark):
    from teleport.ui import conversations, theme

    tok = ui_theme(dark)
    sel, other = dview("A-Sel"), dview("B-Other")
    window.devices.reset([sel, other])
    window.show()
    qtbot.waitExposed(window)
    assert window.conversations.select_device(sel.device_id)
    window.conversations.set_compact(True)
    settle()
    lst = window.conversations.list_view
    expected = theme.blend(tok.sidebar, tok.accent_subtle, conversations._DROP_GLOW_MAX)
    rect = _row_rect(window, other.device_id)
    cell = QRect(
        rect.center().x() - conversations.COMPACT_CELL // 2,
        rect.center().y() - conversations.COMPACT_CELL // 2,
        conversations.COMPACT_CELL,
        conversations.COMPACT_CELL,
    )
    assert not _has(window.grab().toImage(), cell, expected, 2), "tiền đề: chưa kéo thì chưa có màu sáng"
    lst.preview_drop(other.device_id, 3)
    settle()
    assert _has(window.grab().toImage(), cell, expected, 2), "dạng thu gọn: ô avatar cũng sáng lên"


@MODES
def test_real_motion_drop_line_and_border_drawn_at_once(window, qtbot, ui_theme, real_motion, dark):
    """Câu “Thả để gửi n file” (accent_text) và viền nét đứt (accent_strong) có ngay, không chờ nền."""
    tok = ui_theme(dark)
    sel, x = dview("A-Sel"), dview("May-X")
    window.devices.reset([sel, x])
    window.show()
    qtbot.waitExposed(window)
    assert window.conversations.select_device(sel.device_id)  # hàng kéo qua không phải hàng chọn
    settle()
    lst = window.conversations.list_view
    r = _row_rect(window, x.device_id)
    text_block = QRect(r.left() + 60, r.top() + r.height() // 2, r.width() - 70, r.height() // 2 - 2)
    edge = QRect(r.left() + 14, r.top(), r.width() - 28, 4)
    lst.preview_drop(x.device_id, 2)
    assert lst.drop_target() == x.device_id
    image = window.grab().toImage()  # không finish_all: chữ và viền không phụ thuộc hoạt ảnh
    assert _has(image, text_block, tok.accent_text, 16), "câu thả hiện ngay"
    assert _has(image, edge, tok.accent_strong, 16), "viền thả hiện ngay"


# =================================================================================================
# 7.2.5 Tương phản
# =================================================================================================


def test_drop_glow_contrast_pairs_cover_spec():
    from teleport.ui import conversations

    pairs = dict(conversations.DROP_GLOW_CONTRAST_PAIRS)
    for name, minimum in (("text", 4.5), ("accent_text", 4.5), ("text_muted", 4.5)):
        assert name in pairs, f"thiếu cặp {name}"
        assert pairs[name] >= minimum, f"{name}: ngưỡng {pairs[name]} < {minimum}"
    for name, minimum in (("warning", 3.0), ("danger", 3.0)):
        assert name in pairs, f"thiếu cặp {name} (13.14)"
        assert pairs[name] >= minimum
    assert 0.0 < conversations._DROP_GLOW_MAX <= 1.0


@pytest.mark.parametrize("dark", [False, True], ids=["sang", "toi"])
def test_drop_glow_contrast_holds_for_every_accent(qapp, dark):
    from teleport.ui import conversations, theme

    failures = []
    for accent in theme.ACCENTS:
        t = theme.tokens(dark, accent)
        bg = theme.blend(t.sidebar, t.accent_subtle, conversations._DROP_GLOW_MAX)
        for name, minimum in conversations.DROP_GLOW_CONTRAST_PAIRS:
            ratio = theme.contrast_ratio(getattr(t, name), bg)
            if ratio < minimum:
                failures.append(f"{accent}/{name}: {ratio:.2f} < {minimum}")
    assert not failures, failures
