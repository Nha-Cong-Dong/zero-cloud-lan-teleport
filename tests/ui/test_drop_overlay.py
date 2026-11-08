"""Lớp phủ thả file `teleport.ui.drop_overlay.DropOverlay` (spec G5 mục 3.1, test 7.1; mục 13 thắng).

Viết từ spec, không từ code. Mặc định chế độ tức thì (autouse `motion_instant`); ca hoạt ảnh thật xin
`real_motion`, hiện cửa sổ cha và `waitExposed` (13.21), chỉ kiểm giá trị cuối bằng `qtbot.waitUntil`.

1. Tức thì: `show_text` → `shown`, `opacity()==1`, `icon_offset()==0`, `text()`, không viền chạy, không
   hoạt ảnh nào; `dismiss()` → ẩn, `text()==""`.
2. Hoạt ảnh thật: mờ vào + nảy về 0; viền chạy rồi dừng ở trần `MAX_LOOP_MS`; `show_text` lần hai khi
   đang `shown` không chạy lại viền; `dismiss()` → `text()==""` ngay, rồi ẩn hẳn (mvp N3).
3. Ẩn / thu nhỏ cửa sổ → viền dừng.
4. Hệ điều hành tắt hiệu ứng → như tức thì (mvp N12).
5. `dismiss()` rồi `show_text()` khi đang mờ ra → cuối cùng vẫn hiện.
6. Ảnh: chữ vẽ giống `QLabel` cùng font/tone/cỡ; có điểm ảnh `accent_subtle` ở huy hiệu (2 chế độ × 2
   màu nhấn).
8. `ChatView`: kéo vào → `drop_overlay.shown`; rời, thả → không `shown`.

Thêm: cấu trúc (objectName, cờ chuột, hằng, không `QGraphicsEffect`, chỉ import `theme`, `motion`, `icons`
và PySide6), ẩn không tự phát (13.11).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QPoint, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QImage,
    QRegion,
)
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

WAIT_MS = 5000
TEXT = "Thả để gửi 3 file tới PC-Nha"
PEER_ID = "a71c04d2" + "e" * 24
PEER = "PC-Nha"
DROP_TO = "Thả để gửi {n} file tới {name}"
MODES = pytest.mark.parametrize("dark", [False, True], ids=["sang", "toi"])
ACCENTS = pytest.mark.parametrize("accent", ["mint", "rose"])


# =================================================================================================
# Hàm phụ trợ
# =================================================================================================


def settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


def make_overlay(qtbot, *, show: bool = True, size=(480, 320)):
    from teleport.ui.drop_overlay import DropOverlay

    host = QWidget()
    qtbot.addWidget(host)
    host.resize(*size)
    overlay = DropOverlay(host)
    overlay.setGeometry(host.rect())
    if show:
        host.show()
        qtbot.waitExposed(host)
        settle()
    return host, overlay


def close(a: QColor, b: QColor, tol: int) -> bool:
    return (
        abs(a.red() - b.red()) <= tol
        and abs(a.green() - b.green()) <= tol
        and abs(a.blue() - b.blue()) <= tol
    )


def filled(width: int, height: int, color: QColor) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(1.0)
    image.fill(color)
    return image


def badge_rect(overlay) -> QRect:
    """Vòng huy hiệu theo bố cục 3.1: căn giữa ngang, đáy cách đỉnh `label` một khoảng `SPACING[2]`."""
    from teleport.ui import theme
    from teleport.ui.drop_overlay import _BADGE

    label = overlay.label.geometry()
    bottom = label.top() - theme.SPACING[2]
    cx = overlay.width() // 2
    return QRect(cx - _BADGE // 2, bottom - _BADGE, _BADGE, _BADGE)


# =================================================================================================
# Cấu trúc
# =================================================================================================


def test_overlay_parts_flags_and_constants(qtbot):
    from teleport.ui import motion
    from teleport.ui.drop_overlay import _BADGE, _DASH, _DASH_PERIOD_MS, _HOP_PX

    _host, ov = make_overlay(qtbot, show=False)
    assert ov.objectName() == "chatDropOverlay"
    assert ov.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert ov.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert isinstance(ov.label, QLabel)
    assert ov.label.textFormat() == Qt.TextFormat.PlainText
    assert ov.label.wordWrap()
    assert ov.label.alignment() & Qt.AlignmentFlag.AlignHCenter
    assert _BADGE == 56
    assert _HOP_PX == 6
    assert motion.MAX_SHIFT_PX >= _HOP_PX, "nảy không quá MAX_SHIFT_PX"
    assert tuple(_DASH) == (4.0, 2.0)
    assert _DASH_PERIOD_MS == 1200
    assert ov.graphicsEffect() is None and ov.label.graphicsEffect() is None, "không dùng QGraphicsEffect"


def test_overlay_starts_hidden_and_not_shown(qtbot):
    _host, ov = make_overlay(qtbot)
    assert ov.isHidden()
    assert ov.shown is False
    assert ov.text() == ""
    assert ov.dash_running() is False


def test_drop_overlay_module_imports_only_theme_motion_icons_and_pyside():
    """Spec mục 2: `drop_overlay.py` chỉ import `theme`, `motion`, `icons` và PySide6."""
    from teleport.ui import drop_overlay

    tree = ast.parse(Path(drop_overlay.__file__).read_text(encoding="utf-8"))
    bad: list[str] = []
    allowed_ui = {"theme", "motion", "icons"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in {"teleport"} and alias.name.removeprefix("teleport.ui.") not in allowed_ui:
                    bad.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:  # import tương đối: from . import theme / from .motion import Loop
                names = [module] if module else [a.name for a in node.names]
                bad += [n for n in names if n.split(".")[0] not in allowed_ui]
            elif module.startswith("teleport"):
                if module == "teleport.ui":
                    bad += [a.name for a in node.names if a.name not in allowed_ui]
                elif module.removeprefix("teleport.ui.").split(".")[0] not in allowed_ui:
                    bad.append(module)
            elif module.startswith("PySide6"):
                for alias in node.names:
                    assert "QGraphics" not in alias.name, f"không dùng {alias.name}"
    assert not bad, f"drop_overlay import ngoài phạm vi: {bad}"


# =================================================================================================
# 7.1.1 Tức thì
# =================================================================================================


def test_instant_show_text_jumps_to_final_state(qtbot):
    from teleport.ui import motion

    _host, ov = make_overlay(qtbot)
    ov.show_text("x")
    assert ov.shown is True
    assert ov.isVisible()
    assert ov.opacity() == 1.0
    assert ov.icon_offset() == 0.0
    assert ov.text() == "x"
    assert ov.label.text() == "x"
    assert "x" in (ov.accessibleName(), ov.label.accessibleName()), "tên trợ năng = chữ"
    assert ov.dash_running() is False, "chế độ tức thì: Loop không chạy"
    assert motion.running_count() == 0


def test_instant_dismiss_hides_and_clears_text(qtbot):
    from teleport.ui import motion

    _host, ov = make_overlay(qtbot)
    ov.show_text("x")
    ov.dismiss()
    assert ov.shown is False
    assert ov.isHidden()
    assert ov.text() == ""
    assert ov.opacity() == 0.0
    assert motion.running_count() == 0


def test_dismiss_when_not_shown_does_nothing(qtbot):
    from teleport.ui import motion

    _host, ov = make_overlay(qtbot)
    ov.dismiss()
    assert ov.isHidden() and ov.shown is False and ov.text() == ""
    assert motion.running_count() == 0


def test_show_text_again_only_changes_text(qtbot):
    _host, ov = make_overlay(qtbot)
    ov.show_text("x")
    ov.show_text("y")
    assert ov.shown and ov.isVisible()
    assert ov.text() == "y" and ov.label.text() == "y"
    assert ov.opacity() == 1.0 and ov.icon_offset() == 0.0


def test_hiding_parent_window_dismisses_and_it_stays_hidden_when_window_returns(qtbot):
    """13.11: Hide không tự phát (cha ẩn) → `_shown = False` rồi `hide()`; hiện lại cửa sổ không tự hiện."""
    host, ov = make_overlay(qtbot)
    ov.show_text("x")
    host.hide()
    settle()
    assert ov.shown is False
    assert ov.text() == ""
    host.show()
    qtbot.waitExposed(host)
    settle()
    assert ov.isHidden(), "lớp phủ không tự hiện lại khi cửa sổ hiện lại"
    ov.show_text("z")
    assert ov.shown and ov.isVisible() and ov.opacity() == 1.0


# =================================================================================================
# 7.1.2 Hoạt ảnh thật
# =================================================================================================


def test_real_motion_fade_in_bounce_and_dash_stop_at_cap(qtbot, real_motion, monkeypatch):
    motion = real_motion
    monkeypatch.setattr(motion, "MAX_LOOP_MS", 200)  # Loop đọc trần lúc start()
    _host, ov = make_overlay(qtbot)
    ov.show_text("x")
    assert ov.shown is True
    assert ov.text() == "x"
    assert ov.dash_running() is True, "viền nét đứt chạy khi hiện"
    qtbot.waitUntil(lambda: ov.opacity() == 1.0 and abs(ov.icon_offset()) < 0.01, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: not ov.dash_running(), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: motion.running_count() == 0, timeout=WAIT_MS)
    assert ov.icon_offset() == 0.0, "hết nảy: lệch đúng 0"

    ov.show_text("y")  # đang `shown`: chỉ đổi chữ, không chạy lại viền
    assert ov.text() == "y"
    assert ov.dash_running() is False
    assert motion.running_count() == 0

    ov.dismiss()
    assert ov.text() == "", "text() rỗng ngay khi dismiss dù hình còn mờ ra"
    assert ov.shown is False
    qtbot.waitUntil(ov.isHidden, timeout=WAIT_MS)
    assert ov.opacity() == 0.0
    qtbot.waitUntil(lambda: motion.running_count() == 0, timeout=WAIT_MS)


def test_real_motion_dismiss_stops_dash_and_leaves_no_animation(qtbot, real_motion):
    motion = real_motion
    host, ov = make_overlay(qtbot)
    ov.show_text("x")
    assert ov.dash_running() is True
    ov.dismiss()
    assert ov.dash_running() is False, "dismiss dừng viền ngay"
    qtbot.waitUntil(ov.isHidden, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: motion.running_count() == 0, timeout=WAIT_MS)
    assert host.isVisible()


def test_real_motion_icon_offset_stays_within_hop(qtbot, real_motion):
    """Nảy ≤ `_HOP_PX` lên và chưa tới 1 px xuống (OutBack 1.2, 13.2); cuối cùng lệch đúng 0.

    Chỉ kiểm cận trên/dưới của mọi giá trị đọc được, không kiểm một giá trị giữa chừng cụ thể.
    """
    from teleport.ui.drop_overlay import _HOP_PX

    seen: list[float] = []
    _host, ov = make_overlay(qtbot)
    ov.show_text("x")

    def settled() -> bool:
        seen.append(ov.icon_offset())
        return ov.opacity() == 1.0 and ov.icon_offset() == 0.0 and real_motion.running_count() <= 1

    qtbot.waitUntil(settled, timeout=WAIT_MS)
    assert all(-_HOP_PX - 1e-6 <= v < 1.0 for v in seen), seen
    ov.dismiss()


# =================================================================================================
# 7.1.3 Ẩn, thu nhỏ
# =================================================================================================


def test_real_motion_hiding_window_stops_dash(qtbot, real_motion):
    host, ov = make_overlay(qtbot)
    ov.show_text("x")
    assert ov.dash_running() is True
    host.hide()
    qtbot.waitUntil(lambda: not ov.dash_running(), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_real_motion_minimizing_window_stops_dash(qtbot, real_motion):
    host, ov = make_overlay(qtbot)
    ov.show_text("x")
    assert ov.dash_running() is True
    host.setWindowState(host.windowState() | Qt.WindowState.WindowMinimized)
    assert host.isMinimized()
    qtbot.waitUntil(lambda: not ov.dash_running(), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_real_motion_dash_does_not_restart_by_itself_after_window_returns(qtbot, real_motion):
    host, ov = make_overlay(qtbot)
    ov.show_text("x")
    host.hide()
    qtbot.waitUntil(lambda: not ov.dash_running(), timeout=WAIT_MS)
    host.show()
    qtbot.waitExposed(host)
    qtbot.wait(50)
    assert ov.dash_running() is False


# =================================================================================================
# 7.1.4 Hệ điều hành tắt hiệu ứng
# =================================================================================================


def test_animations_disabled_behaves_like_instant(qtbot, real_motion, monkeypatch):
    motion = real_motion
    monkeypatch.setattr(motion, "animations_enabled", lambda: False)
    _host, ov = make_overlay(qtbot)
    ov.show_text("x")
    assert ov.shown and ov.isVisible()
    assert ov.opacity() == 1.0
    assert ov.icon_offset() == 0.0
    assert ov.text() == "x"
    assert ov.dash_running() is False
    assert motion.running_count() == 0
    ov.dismiss()
    assert ov.isHidden(), "tắt hiệu ứng: dismiss ẩn ngay"
    assert ov.text() == "" and ov.opacity() == 0.0
    assert motion.running_count() == 0


# =================================================================================================
# 7.1.5 Kéo vào lại khi đang mờ ra
# =================================================================================================


def test_real_motion_show_again_during_fade_out_stays_visible(qtbot, real_motion):
    motion = real_motion
    _host, ov = make_overlay(qtbot)
    ov.show_text("x")
    qtbot.waitUntil(lambda: ov.opacity() == 1.0, timeout=WAIT_MS)
    ov.dismiss()
    ov.show_text("y")
    assert ov.shown is True
    assert ov.text() == "y"
    qtbot.waitUntil(lambda: ov.opacity() == 1.0, timeout=WAIT_MS)
    qtbot.wait(300)  # quá FAST: `on_finished` cũ (hide) không được chạy muộn
    assert ov.isVisible(), "không bị hide() muộn"
    assert ov.shown is True
    assert ov.opacity() == 1.0
    assert ov.text() == "y"
    ov.dismiss()  # dọn viền đang chạy
    qtbot.waitUntil(lambda: motion.running_count() == 0, timeout=WAIT_MS)


# =================================================================================================
# 7.1.6 Ảnh
# =================================================================================================


@MODES
@ACCENTS
def test_overlay_text_renders_like_plain_qlabel_and_badge_uses_accent_subtle(
    qtbot, ui_theme, monkeypatch, dark, accent
):
    from teleport.ui import motion, theme

    monkeypatch.setattr(theme, "_accent", accent)
    tok = ui_theme(dark)
    host, ov = make_overlay(qtbot)
    ov.show_text(TEXT)
    settle()
    motion.finish_all()
    assert ov.opacity() == 1.0

    render_flags = QWidget.RenderFlag.DrawChildren
    shot = filled(ov.width(), ov.height(), tok.surface)
    ov.render(shot, QPoint(0, 0), QRegion(), render_flags)

    # QLabel đối chứng: cùng font, tone, cỡ, căn lề, xuống dòng; vẽ lên cùng nền.
    box = QWidget()
    qtbot.addWidget(box)
    box.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    ref = QLabel(box)
    ref.setTextFormat(Qt.TextFormat.PlainText)
    ref.setWordWrap(True)
    ref.setAlignment(ov.label.alignment())
    ref.setProperty(theme.TONE_PROPERTY, "accent")
    ref.setFont(ov.label.font())
    ref.setText(TEXT)
    label_rect = ov.label.geometry()
    ref.setGeometry(0, 0, label_rect.width(), label_rect.height())
    box.resize(label_rect.size())
    box.show()
    settle()
    ref_img = filled(label_rect.width(), label_rect.height(), tok.surface)
    ref.render(ref_img, QPoint(0, 0), QRegion(), render_flags)

    # Vùng có chữ trong ảnh đối chứng (khác nền), nới 2 px.
    xs, ys = [], []
    for y in range(ref_img.height()):
        for x in range(ref_img.width()):
            if not close(ref_img.pixelColor(x, y), tok.surface, 2):
                xs.append(x)
                ys.append(y)
    assert xs, "tiền đề: QLabel đối chứng có chữ"
    assert any(close(ref_img.pixelColor(x, y), tok.accent_text, 24) for x, y in zip(xs, ys, strict=True)), (
        "tiền đề: chữ đối chứng màu accent_text"
    )
    left, right = max(0, min(xs) - 2), min(ref_img.width() - 1, max(xs) + 2)
    top, bottom = max(0, min(ys) - 2), min(ref_img.height() - 1, max(ys) + 2)
    total = mismatched = 0
    for y in range(top, bottom + 1):
        for x in range(left, right + 1):
            total += 1
            a = shot.pixelColor(label_rect.left() + x, label_rect.top() + y)
            b = ref_img.pixelColor(x, y)
            if not close(a, b, 2):
                mismatched += 1
    assert mismatched <= total * 0.01, f"chữ lớp phủ lệch QLabel: {mismatched}/{total} điểm ảnh"

    badge = badge_rect(ov)
    found = False
    for y in range(badge.top(), badge.bottom() + 1):
        for x in range(badge.left(), badge.right() + 1):
            if (
                0 <= x < shot.width()
                and 0 <= y < shot.height()
                and close(shot.pixelColor(x, y), tok.accent_subtle, 3)
            ):
                found = True
                break
        if found:
            break
    assert found, "huy hiệu: vòng nền accent_subtle"
    assert host.isVisible()


# =================================================================================================
# 7.1.8 ChatView
# =================================================================================================


def dview(device_id: str = PEER_ID, name: str = PEER, *, online: bool = True):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id,
        name=name,
        os="windows",
        paired=True,
        online=online,
        connected=online,
        addresses=("192.168.1.20",) if online else (),
        port=47474 if online else None,
    )


def peer(name: str, device_id: str = PEER_ID) -> str:
    from teleport.ui.texts import peer_label

    return peer_label(name, device_id)


def mime(*paths: Path) -> QMimeData:
    m = QMimeData()
    m.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return m


_COPY = Qt.DropAction.CopyAction
_LEFT = Qt.MouseButton.LeftButton
_NOMOD = Qt.KeyboardModifier.NoModifier


def drag_enter(target, data) -> bool:
    ev = QDragEnterEvent(target.rect().center(), _COPY, data, _LEFT, _NOMOD)
    ev.setAccepted(False)
    QApplication.sendEvent(target, ev)
    return ev.isAccepted()


def drag_move(target, data) -> bool:
    ev = QDragMoveEvent(target.rect().center(), _COPY, data, _LEFT, _NOMOD)
    ev.setAccepted(False)
    QApplication.sendEvent(target, ev)
    return ev.isAccepted()


def drop(target, data) -> None:
    drag_enter(target, data)
    drag_move(target, data)
    QApplication.sendEvent(target, QDropEvent(QPointF(target.rect().center()), _COPY, data, _LEFT, _NOMOD))


def drag_leave(target) -> None:
    QApplication.sendEvent(target, QDragLeaveEvent())


@pytest.fixture
def chat(qtbot):
    from teleport.ui.chat import ChatView
    from teleport.ui.radar import DeviceListModel

    host = QWidget()
    qtbot.addWidget(host)
    devices = DeviceListModel(host)
    devices.reset([dview()])
    view = ChatView(devices, host)
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(view)
    host.resize(720, 460)
    view.set_device_names({PEER_ID: PEER})
    view.set_connected(PEER_ID, True)
    host.show()
    qtbot.waitExposed(host)
    settle()
    view.set_device(PEER_ID)
    settle()
    yield view  # giữ `host` sống tới hết test (qtbot chỉ giữ yếu)
    del host


def test_chat_view_uses_drop_overlay_module(chat):
    from teleport.ui import chat as chat_module
    from teleport.ui.drop_overlay import DropOverlay

    assert isinstance(chat.drop_overlay, DropOverlay)
    assert chat.drop_overlay.objectName() == "chatDropOverlay"
    assert not hasattr(chat_module, "_DropOverlay"), "lớp `_DropOverlay` cũ đã xóa khỏi chat.py"


def test_chat_drag_enter_shows_overlay_and_leave_dismisses(chat, tmp_path):
    ov = chat.drop_overlay
    data = mime(tmp_path / "a.txt", tmp_path / "b.txt")
    assert drag_enter(chat, data)
    drag_move(chat, data)
    assert ov.shown is True
    assert ov.text() == DROP_TO.format(n=2, name=peer(PEER))
    assert ov.geometry() == chat.threads.geometry()
    drag_leave(chat)
    assert ov.shown is False
    assert ov.text() == ""


def test_chat_drop_dismisses_overlay(chat, tmp_path):
    ov = chat.drop_overlay
    drop(chat, mime(tmp_path / "a.txt"))
    assert ov.shown is False
    assert ov.text() == ""


def test_chat_preview_drop_none_or_no_device_dismisses(chat):
    ov = chat.drop_overlay
    chat.preview_drop(3)
    assert ov.shown and ov.text() == DROP_TO.format(n=3, name=peer(PEER))
    chat.preview_drop(None)
    assert ov.shown is False
    chat.set_device(None)
    chat.preview_drop(3)
    assert ov.shown is False


def test_chat_real_motion_leave_clears_text_now_and_hides_later(chat, tmp_path, qtbot, real_motion):
    ov = chat.drop_overlay
    data = mime(tmp_path / "a.txt")
    assert drag_enter(chat, data)
    assert ov.shown is True
    drag_leave(chat)
    assert ov.text() == ""
    qtbot.waitUntil(ov.isHidden, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)
