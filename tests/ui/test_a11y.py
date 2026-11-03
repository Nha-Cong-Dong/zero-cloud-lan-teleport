"""Trợ năng kiểm được bằng máy (mvp N12, spec E1 mục 2.3): A1–A7 trên mọi cảnh `SCENES` và hộp `DIALOGS`.

Viết từ spec, không từ code. Cảnh và hộp dựng ở `tests/ui_scenes.py`, ở chuỗi nguồn (tiếng Việt). Cửa sổ
chính hiện ở 980×660 (cỡ mặc định của ảnh duyệt); hộp ở cỡ của chính nó (`adjustSize`).

- **A1. Tên trợ năng.** Mọi widget đang hiện, đang bật, có `TabFocus` (và `focusProxy` của widget như
  vậy) có tên trợ năng không rỗng: `QAccessible.queryAccessibleInterface(w).text(Name).strip()`.
- **A2. Vai trò.** Cùng tập: `role()` không phải `NoRole` hay `Client`.
- **A3. Tới được bằng bàn phím.** Mọi widget tương tác đang hiện và bật (`QAbstractButton`, `QLineEdit`,
  `QAbstractSpinBox`, `QComboBox`, `QAbstractItemView`, `QAbstractSlider`, `QTextEdit`/`QPlainTextEdit`
  không chỉ đọc; tiêu đề bảng chỉ tính khi bấm được) có `TabFocus` (chính nó, hay widget cha nhận Tab rồi
  chuyển tiêu điểm cho nó qua `focusProxy`), trừ `MOUSE_ONLY` (mỗi mục: lý do + test chứng minh có đường
  bàn phím khác). Ô sửa bên trong spin box/combo không xét riêng ở A1/A2: Qt trình bày control chủ.
- **A4. Chuỗi Tab.** Đi `nextInFocusChain()` từ cửa sổ: quay về điểm đầu; mọi widget của A3 là một điểm
  dừng (đang hiện, bật, `TabFocus`, không `focusProxy`, cùng cửa sổ); mọi điểm dừng thấy được (bề ngang và
  bề cao > 0, cả các widget cha tới cửa sổ).
- **A5. Tiêu điểm thấy được** (`slow`). Mỗi điểm dừng: ảnh vùng widget + 3 px khi có và khi không có tiêu
  điểm (cửa sổ active) phải khác nhau. Đo với giao diện thật của app (`theme.apply`, sáng và tối). Tiêu
  điểm tạo như bàn phím thật (spec E1 mục 2.6): `setFocus(TabFocusReason)` rồi bật
  `WA_KeyboardFocusChange` của cửa sổ, đúng trạng thái sau phím Tab thật (style chỉ vẽ khung khi cờ này
  bật; đối chứng so ảnh với phím Tab thật `QTest.keyClick`). Điểm dừng chuyển tiêu điểm cho con (luồng
  chat) đo ở widget thật sự nhận tiêu điểm, trong vùng của điểm dừng. Miễn: nút mặc định của hộp thoại
  (E1f, `A5_DEFAULT_BUTTON_REASON`). Đối chứng: widget không có dấu tiêu điểm bị bắt; miễn E1f không lan
  sang nút khác. `offscreen` không active được → skip; bằng chứng là lần chạy với
  `QT_QPA_PLATFORM=windows` (spec E1 mục 5).
- **A6. Màu là thế giới đóng.** Giá trị property `tone`/`surface` gặp trong cảnh thuộc tập có luật trong
  stylesheet của `ui/theme.py` (sáng và tối). Quét tĩnh `ui/**/*.py` trừ `theme.py`: không `#rrggbb`
  (`#rrggbbaa`), `rgb(`/`rgba(` trong chuỗi, `QColor(<số>…)`, `Qt.GlobalColor.<màu>` hay `Qt.<tên màu>`;
  miễn `transparent` (xóa nền pixmap, không phải màu hiện ra).
- **A7. Tắt hiệu ứng.** Quét tĩnh: `QPropertyAnimation`, `QVariantAnimation`, `QTimeLine`, `QMovie`,
  `QGraphicsOpacityEffect` chỉ có trong `ui/motion.py`. Với `real_motion` và
  `motion.animations_enabled` → False: dựng, hiện mọi cảnh/hộp, chạy vòng sự kiện → `running_count() == 0`
  (đối chứng: hiệu ứng bật thì cảnh radar có hoạt ảnh chạy).

Quét tĩnh đọc `ast` (chú thích, docstring không tính). Mô tả widget lỗi: `Lớp(objectName) trong <cha có
objectName gần nhất>`.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QAccessible, QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractScrollArea,
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QHeaderView,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollBar,
    QTextEdit,
    QWidget,
)
from ui_scenes import DIALOGS, SCENES

WINDOW_SIZE = (980, 660)
FOCUS_MARGIN = 3  # px quanh widget khi so ảnh có/không tiêu điểm (A5)
TARGETS = [("scene", name) for name in SCENES] + [("dialog", name) for name in DIALOGS]
TARGET_IDS = [f"{kind}-{name}" for kind, name in TARGETS]
INTERACTIVE = (
    QAbstractButton,
    QLineEdit,
    QAbstractSpinBox,
    QComboBox,
    QAbstractItemView,
    QAbstractSlider,
    QTextEdit,
    QPlainTextEdit,
)
ANIMATION_CLASSES = {
    "QPropertyAnimation",
    "QVariantAnimation",
    "QTimeLine",
    "QMovie",
    "QGraphicsOpacityEffect",
}


# =================================================================================================
# “Chỉ chuột, có đường bàn phím khác” (A3)
# =================================================================================================


def _in_chat_thread(w: QWidget) -> bool:
    """Control lười trong bong bóng chat: luồng là một điểm dừng Tab, bong bóng hiện hành mang nút của nó
    (roving tabindex, spec B3 9.10)."""
    from teleport.ui.chat import ChatThread

    parent = w.parentWidget()
    while parent is not None:
        if isinstance(parent, ChatThread):
            return True
        parent = parent.parentWidget()
    return False


def _roving_member(w: QWidget) -> bool:
    """Nút chưa chọn của nhóm roving (`widgets.SegmentedControl`, `appearance.AccentPicker`): Tab chỉ dừng ở
    nút đang chọn, mũi tên đi giữa các nút. Chỉ miễn khi trong nhóm đúng là có một nút nhận Tab."""
    from teleport.ui.appearance import AccentPicker
    from teleport.ui.widgets import SegmentedControl

    group = w.parentWidget()
    if not isinstance(w, QAbstractButton) or not isinstance(group, SegmentedControl | AccentPicker):
        return False
    members = [b for b in group.findChildren(QAbstractButton) if b.isVisible()]
    return sum(1 for b in members if tab_focus(b)) == 1


def _scroll_bar(w: QWidget) -> bool:
    """Thanh cuộn của vùng cuộn: Qt để `NoFocus`; bàn phím cuộn qua chính vùng cuộn hay một điểm dừng Tab
    bên trong (phím trang/đầu/cuối mà widget có tiêu điểm bỏ qua thì lên tới `QAbstractScrollArea`).
    Chứng minh trên mọi cảnh/hộp: `test_a3_overflowing_scroll_areas_scroll_by_keyboard`."""
    return isinstance(w, QScrollBar)


# (lý do, test chứng minh có đường bàn phím khác, hàm nhận dạng)
MOUSE_ONLY = (
    (
        "control lười trong bong bóng chat (roving tabindex)",
        "tests/ui/test_chat.py::test_f2_lazy_widgets_are_click_or_no_focus_threads_tab_only, "
        "tests/ui/test_home_layout.py::test_l12_chat_thread_is_one_tab_stop_before_send_and_open_button",
        _in_chat_thread,
    ),
    (
        "nút chưa chọn của nhóm roving (chế độ màu, màu nhấn)",
        "tests/ui/test_widgets.py::test_segmented_control_arrows_select_and_tab_stops_on_checked, "
        "tests/ui/test_appearance.py::test_mode_arrow_keys_move_and_emit, "
        "tests/ui/test_appearance.py::test_accent_arrow_keys_move_and_select",
        _roving_member,
    ),
    (
        "thanh cuộn của vùng cuộn",
        "tests/ui/test_a11y.py::test_a3_overflowing_scroll_areas_scroll_by_keyboard, "
        "tests/ui/test_a11y.py::test_a3_scroll_proof_fails_without_keyboard_path",
        _scroll_bar,
    ),
)


def _mouse_only(w: QWidget) -> bool:
    return any(match(w) for _reason, _proof, match in MOUSE_ONLY)


# =================================================================================================
# Hàm phụ trợ
# =================================================================================================


def settle(rounds: int = 3) -> None:
    for _ in range(rounds):
        QApplication.processEvents()


def describe(w: QWidget) -> str:
    name = f"{type(w).__name__}({w.objectName()})"
    parent = w.parentWidget()
    while parent is not None and not parent.objectName():
        parent = parent.parentWidget()
    return f"{name} trong {parent.objectName()}" if parent is not None else name


def build(qtbot, kind: str, name: str, *, on_screen: bool = False) -> QWidget:
    """Dựng và hiện cảnh/hộp; `on_screen` False thì không lên màn hình (offscreen hay Windows đều được)."""
    root = (SCENES if kind == "scene" else DIALOGS)[name](qtbot)
    if not on_screen:
        root.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    if kind == "scene":
        root.resize(*WINDOW_SIZE)
    root.show()
    settle()
    if kind == "dialog":
        root.adjustSize()
        settle()
    return root


def widgets(root: QWidget) -> list[QWidget]:
    return [root, *root.findChildren(QWidget)]


def tab_focus(w: QWidget) -> bool:
    return bool(w.focusPolicy() & Qt.FocusPolicy.TabFocus)


def final_proxy(w: QWidget) -> QWidget:
    seen = {id(w)}
    while w.focusProxy() is not None and id(w.focusProxy()) not in seen:
        w = w.focusProxy()
        seen.add(id(w))
    return w


def live(w: QWidget) -> bool:
    return w.isVisible() and w.isEnabled()


def focusable_set(root: QWidget) -> list[QWidget]:
    """Tập của A1/A2: widget đang hiện, bật, có `TabFocus`, và `focusProxy` (cuối) của chúng."""
    out: list[QWidget] = []
    seen: set[int] = set()
    for w in widgets(root):
        if not (live(w) and tab_focus(w)):
            continue
        for target in (w, final_proxy(w)):
            if internal_editor(target):
                continue  # control chủ (cũng trong tập) mang tên và vai
            if id(target) not in seen:
                seen.add(id(target))
                out.append(target)
    return out


def is_interactive(w: QWidget) -> bool:
    """Widget tương tác của A3; tiêu đề bảng không bấm được (`sectionsClickable()` False) không tính."""
    if not isinstance(w, INTERACTIVE):
        return False
    if isinstance(w, QHeaderView) and not w.sectionsClickable():
        return False
    return not (isinstance(w, QTextEdit | QPlainTextEdit) and w.isReadOnly())


def internal_editor(w: QWidget) -> bool:
    """Ô sửa bên trong `QAbstractSpinBox` / `QComboBox`: Qt trình bày cả control là một đối tượng trợ năng
    (sự kiện tiêu điểm phát cho chính spin box/combo), nên tên và vai đọc ở control chủ."""
    owner = w.parentWidget()
    return isinstance(owner, QAbstractSpinBox | QComboBox) and owner.lineEdit() is w


def keyboard_reachable(w: QWidget, root: QWidget) -> bool:
    """`w` có `TabFocus`, hay một widget cha (tới `root`) có `TabFocus` và chuyển tiêu điểm cho `w`."""
    if tab_focus(w):
        return True
    parent = w.parentWidget()
    while parent is not None:
        if tab_focus(parent) and final_proxy(parent) is w:
            return True
        if parent is root:
            break
        parent = parent.parentWidget()
    return False


def interactive_widgets(root: QWidget) -> list[QWidget]:
    """Tập của A3 (đã trừ `MOUSE_ONLY`)."""
    return [w for w in widgets(root) if is_interactive(w) and live(w) and not _mouse_only(w)]


def is_tab_stop(w: QWidget, root: QWidget) -> bool:
    return live(w) and tab_focus(w) and w.focusProxy() is None and w.window() is root


def tab_chain(root: QWidget) -> tuple[list[QWidget], bool]:
    """Điểm dừng Tab theo `nextInFocusChain()` từ `root`; (điểm dừng, đã quay về `root`)."""
    limit = 2 * len(widgets(root)) + 10
    stops: list[QWidget] = []
    w = root.nextInFocusChain()
    for _ in range(limit):
        if w is root:
            return stops, True
        if is_tab_stop(w, root) and all(w is not s for s in stops):
            stops.append(w)
        w = w.nextInFocusChain()
    return stops, False


def seen_on_screen(w: QWidget, root: QWidget) -> bool:
    node = w
    while node is not None:
        if node.width() <= 0 or node.height() <= 0:
            return False
        if node is root:
            return True
        node = node.parentWidget()
    return True


def accessible(w: QWidget):
    return QAccessible.queryAccessibleInterface(w)


# =================================================================================================
# A1, A2
# =================================================================================================


@pytest.mark.parametrize(("kind", "name"), TARGETS, ids=TARGET_IDS)
def test_a1_focusable_widgets_have_accessible_name(qtbot, kind, name):
    root = build(qtbot, kind, name)
    targets = focusable_set(root)
    assert targets, "cảnh không có widget nhận Tab nào: cảnh dựng sai?"
    bad = []
    for w in targets:
        iface = accessible(w)
        if iface is None:
            bad.append(f"{describe(w)}: không có giao diện trợ năng")
        elif not iface.text(QAccessible.Text.Name).strip():
            bad.append(describe(w))
    assert bad == []


@pytest.mark.parametrize(("kind", "name"), TARGETS, ids=TARGET_IDS)
def test_a2_focusable_widgets_have_a_real_role(qtbot, kind, name):
    root = build(qtbot, kind, name)
    bad = []
    for w in focusable_set(root):
        iface = accessible(w)
        role = iface.role() if iface is not None else None
        if role in (None, QAccessible.Role.NoRole, QAccessible.Role.Client):
            bad.append(f"{describe(w)}: {role}")
    assert bad == []


def test_a1_a2_checks_catch_unnamed_and_roleless_widgets(qtbot):
    """Đối chứng: nút không chữ, không tên → A1 bắt; `QWidget` trơn nhận Tab → vai `Client`, A2 bắt."""
    from PySide6.QtWidgets import QPushButton, QVBoxLayout

    root = QWidget()
    qtbot.addWidget(root)
    layout = QVBoxLayout(root)
    unnamed = QPushButton()
    plain = QWidget()
    plain.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    plain.setMinimumSize(10, 10)
    layout.addWidget(unnamed)
    layout.addWidget(plain)
    root.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    root.show()
    settle()
    assert unnamed in focusable_set(root) and plain in focusable_set(root)
    assert not accessible(unnamed).text(QAccessible.Text.Name).strip()
    assert accessible(plain).role() in (QAccessible.Role.Client, QAccessible.Role.NoRole)


# =================================================================================================
# A3, A4
# =================================================================================================


@pytest.mark.parametrize(("kind", "name"), TARGETS, ids=TARGET_IDS)
def test_a3_interactive_widgets_are_reachable_by_keyboard(qtbot, kind, name):
    root = build(qtbot, kind, name)
    bad = [describe(w) for w in interactive_widgets(root) if not keyboard_reachable(w, root)]
    assert bad == []


@pytest.mark.parametrize(("kind", "name"), TARGETS, ids=TARGET_IDS)
def test_a4_tab_chain_closes_and_visits_every_interactive_widget(qtbot, kind, name):
    root = build(qtbot, kind, name)
    stops, closed = tab_chain(root)
    assert closed, "chuỗi tiêu điểm không quay về cửa sổ"
    assert stops, "không có điểm dừng Tab nào"
    missing = []
    for w in interactive_widgets(root):
        if not keyboard_reachable(w, root):
            continue  # A3 đã báo
        target = final_proxy(w) if tab_focus(w) else w
        if all(target is not s for s in stops):
            missing.append(describe(w))
    hidden = [describe(s) for s in stops if not seen_on_screen(s, root)]
    assert (missing, hidden) == ([], [])


# ---- A3: thanh cuộn (mục miễn thứ ba của MOUSE_ONLY) ------------------------------------------

_SCROLL_PRESSES = 40  # lần bấm tối đa mỗi phím để đi hết một vùng cuộn


def _press_until(w: QWidget, keys: tuple[Qt.Key, ...], bar: QScrollBar, goal: int) -> bool:
    """Bấm lần lượt từng phím trong `keys` (mỗi phím tới khi thanh cuộn đứng yên, tối đa `_SCROLL_PRESSES`
    lần) vào `w`; True nếu thanh cuộn tới `goal`. Gửi thẳng vào `w` như khi `w` có tiêu điểm: phím `w` bỏ
    qua thì đi lên widget cha (tới vùng cuộn)."""
    for key in keys:
        for _ in range(_SCROLL_PRESSES):
            if bar.value() == goal:
                return True
            before = bar.value()
            QTest.keyClick(w, key)
            settle(1)
            if bar.value() == before:
                break
    return bar.value() == goal


def scroll_keyboard_gaps(root: QWidget) -> list[str]:
    """Vùng cuộn đang hiện có nội dung tràn (thanh cuộn dọc `maximum() > 0`) mà không có đường bàn phím:
    không điểm dừng Tab nào (chính vùng cuộn hay widget bên trong) đưa được thanh cuộn tới cuối rồi về đầu
    bằng PageDown/End và PageUp/Home. Vùng cuộn trong luồng chat thuộc mục miễn roving (có test riêng).
    Cuộn ngang tràn thì báo luôn (không có phím chuẩn để chứng minh)."""
    gaps: list[str] = []
    for area in root.findChildren(QAbstractScrollArea):
        if not area.isVisible() or _in_chat_thread(area.verticalScrollBar()):
            continue
        if area.horizontalScrollBar().maximum() > 0:
            gaps.append(f"{describe(area)}: cuộn ngang {area.horizontalScrollBar().maximum()} px")
        bar = area.verticalScrollBar()
        if bar.maximum() <= 0:
            continue
        stops = [w for w in [area, *area.findChildren(QWidget)] if is_tab_stop(w, root)]
        start = bar.value()
        ok = False
        for w in stops:
            bar.setValue(bar.minimum())
            settle(1)
            down = _press_until(w, (Qt.Key.Key_PageDown, Qt.Key.Key_End), bar, bar.maximum())
            up = _press_until(w, (Qt.Key.Key_PageUp, Qt.Key.Key_Home), bar, bar.minimum())
            if down and up:
                ok = True
                break
        bar.setValue(start)
        if not ok:
            gaps.append(f"{describe(area)}: {len(stops)} điểm dừng Tab bên trong, không cuộn hết bằng phím")
    return gaps


@pytest.mark.parametrize(("kind", "name"), TARGETS, ids=TARGET_IDS)
def test_a3_overflowing_scroll_areas_scroll_by_keyboard(qtbot, kind, name):
    """Chứng minh cho mục miễn “thanh cuộn” của A3: thanh cuộn `NoFocus`, nhưng mọi vùng cuộn tràn cuộn hết
    được bằng bàn phím từ một điểm dừng Tab (ở 980×660, `settings_advanced` và `welcome_*` có tràn)."""
    root = build(qtbot, kind, name)
    assert scroll_keyboard_gaps(root) == []


def test_a3_scroll_proof_fails_without_keyboard_path(qtbot):
    """Đối chứng: vùng cuộn tràn chỉ có nhãn (không điểm dừng Tab) → báo; thêm một nút bên trong → hết."""
    from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea, QVBoxLayout

    root = QWidget()
    qtbot.addWidget(root)
    area = QScrollArea(root)
    area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    area.setWidgetResizable(True)
    inner = QWidget()
    column = QVBoxLayout(inner)
    for i in range(30):
        column.addWidget(QLabel(f"dòng {i}"))
    area.setWidget(inner)
    QVBoxLayout(root).addWidget(area)
    root.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    root.resize(300, 200)
    root.show()
    settle()
    assert area.verticalScrollBar().maximum() > 0
    assert len(scroll_keyboard_gaps(root)) == 1
    column.insertWidget(0, QPushButton("Nút"))
    settle()
    assert scroll_keyboard_gaps(root) == []


def test_a3_a4_checks_catch_unreachable_button(qtbot):
    """Đối chứng: nút `NoFocus` → A3 bắt; nút trong khung cao 0 → A4 báo điểm dừng không thấy được."""
    from PySide6.QtWidgets import QPushButton, QVBoxLayout

    root = QWidget()
    qtbot.addWidget(root)
    layout = QVBoxLayout(root)
    mouse_only = QPushButton("Chuột")
    mouse_only.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    box = QWidget()
    box.setFixedHeight(0)
    hidden = QPushButton("Ẩn", box)
    layout.addWidget(mouse_only)
    layout.addWidget(box)
    root.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    root.show()
    settle()
    assert not keyboard_reachable(mouse_only, root)
    stops, closed = tab_chain(root)
    assert closed and hidden in stops and not seen_on_screen(hidden, root)


# =================================================================================================
# A5
# =================================================================================================


_OFFSCREEN_SKIP = (
    "offscreen không có cửa sổ active nên không vẽ khung tiêu điểm; bằng chứng là lần chạy với "
    "QT_QPA_PLATFORM=windows (spec E1 mục 5)"
)


def show_active(qtbot, root: QWidget) -> None:
    """Đưa `root` (đã hiện trên màn hình) lên làm cửa sổ active; không được thì skip có lý do."""
    root.activateWindow()
    root.raise_()
    try:
        qtbot.waitUntil(root.isActiveWindow, timeout=3000)
    except Exception:
        pytest.skip("cửa sổ không active được (hệ điều hành chặn giành tiêu điểm)")


def keyboard_focus_mode(root: QWidget) -> bool:
    """Cửa sổ đang ở chế độ “tiêu điểm đến từ bàn phím” (`WA_KeyboardFocusChange`): style (vd. windows11)
    chỉ vẽ khung tiêu điểm khi cờ này bật."""
    return root.testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange)


def press_tab(root: QWidget) -> None:
    """Bấm phím Tab thật (`QTest.keyClick`) vào widget đang có tiêu điểm của `root` (không có thì chính
    `root`), như người dùng."""
    target = QApplication.focusWidget()
    if target is None or target.window() is not root:
        target = root
    QTest.keyClick(target, Qt.Key.Key_Tab)
    settle()


def keyboard_focus(w: QWidget) -> None:
    """Đưa tiêu điểm tới `w` và để cửa sổ ở đúng trạng thái sau một lần bấm Tab thật: Qt chỉ bật
    `WA_KeyboardFocusChange` khi đang xử lý phím Tab, còn `setFocus(...)` ngoài phím Tab thì tắt cờ đó
    (vì vậy cách đo cũ chỉ bằng `setFocus` không thấy khung tiêu điểm của style windows11). Bật lại cờ rồi
    vẽ lại `w`. `test_a5_check_catches_widget_without_focus_mark` chứng minh ảnh thu được giống hệt ảnh
    sau phím Tab thật (spec E1 mục 2.6). Không đi cả chuỗi bằng phím Tab vì vùng cuộn tự cuộn theo tiêu
    điểm, làm ảnh khác đi mà không do dấu tiêu điểm."""
    root = w.window()
    w.setFocus(Qt.FocusReason.TabFocusReason)
    root.setAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange, True)
    for part in (w, *w.findChildren(QWidget)):
        part.update()
    settle()


def focus_area(w: QWidget, root: QWidget) -> QRect:
    top_left = w.mapTo(root, QPoint(0, 0))
    return QRect(top_left, w.size()).adjusted(-FOCUS_MARGIN, -FOCUS_MARGIN, FOCUS_MARGIN, FOCUS_MARGIN)


A5_DEFAULT_BUTTON_REASON = (
    "E1f: nút mặc định của hộp thoại (`QPushButton.isDefault()`): Fusion tô nút mặc định giống nút có "
    "tiêu điểm, nên ảnh có/không tiêu điểm trùng nhau. Qt chuyển “mặc định” theo tiêu điểm giữa các nút "
    "nên nút được tô luôn là nút sẽ nhận Enter; việc sau E1f: vòng tiêu điểm riêng cho nút mặc định"
)


def a5_exempt(w: QWidget) -> bool:
    """Miễn A5 (`A5_DEFAULT_BUTTON_REASON`): chỉ nút đang là nút mặc định trong một `QDialog` lúc tới lượt
    đo. Các nút khác của cùng hộp vẫn được đo."""
    from PySide6.QtWidgets import QDialog

    return isinstance(w, QPushButton) and w.isDefault() and isinstance(w.window(), QDialog)


def focus_visibility_gaps(root: QWidget, stops: list[QWidget]) -> list[str]:
    """Điểm dừng mà ảnh vùng widget + `FOCUS_MARGIN` px khi có tiêu điểm đến từ bàn phím
    (`keyboard_focus`) giống hệt ảnh khi không có tiêu điểm (`clearFocus`).

    Điểm dừng chuyển tiêu điểm vào con của nó lúc nhận (vd. `_ThreadStack` của luồng chat chuyển cho
    bong bóng hiện hành, roving) được tính là nhận tiêu điểm; dấu phải thấy trong vùng của điểm dừng.
    Điểm dừng đã ẩn/tắt vì một lần đặt tiêu điểm trước đó trong cùng cảnh (vd. nút “↓ Mới” ẩn khi luồng
    nhận tiêu điểm và cuộn xuống cuối) bị bỏ qua: chuỗi Tab trên cảnh mới dựng là việc của A4. Nút mặc
    định của hộp thoại được miễn (`a5_exempt`)."""
    bad: list[str] = []
    for w in stops:
        if not live(w) or a5_exempt(w):
            continue
        keyboard_focus(w)
        holder = QApplication.focusWidget()
        if holder is None or not (holder is w or w.isAncestorOf(holder)):
            bad.append(f"{describe(w)}: không nhận tiêu điểm")
            continue
        assert keyboard_focus_mode(root)
        area = focus_area(w, root)
        focused = root.grab(area).toImage()
        holder.clearFocus()
        settle()
        unfocused = root.grab(area).toImage()
        if focused == unfocused:
            bad.append(describe(w))
    return bad


@pytest.mark.slow
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize(("kind", "name"), TARGETS, ids=TARGET_IDS)
def test_a5_focus_is_visible_on_every_tab_stop(qtbot, ui_theme, kind, name, dark):
    """Với giao diện thật của app (`theme.apply`: style Fusion + stylesheet, sáng và tối), không phải style
    trần của nền: dấu tiêu điểm của app nằm ở stylesheet (`theme.py`)."""
    if QGuiApplication.platformName() == "offscreen":
        pytest.skip(_OFFSCREEN_SKIP)
    ui_theme(dark)
    root = build(qtbot, kind, name, on_screen=True)
    show_active(qtbot, root)
    stops, _closed = tab_chain(root)
    gaps = focus_visibility_gaps(root, stops)
    assert gaps == [], "không thấy dấu tiêu điểm:\n" + "\n".join(gaps)


class _NoFocusMark(QWidget):
    """Widget nhận Tab mà tự vẽ một màu đặc, không bao giờ vẽ dấu tiêu điểm (stylesheet không với tới vì
    `paintEvent` riêng không gọi style)."""

    def paintEvent(self, event) -> None:
        from PySide6.QtGui import QColor, QPainter

        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(128, 128, 128))
        painter.end()


@pytest.mark.slow
def test_a5_check_catches_widget_without_focus_mark(qtbot, ui_theme):
    """Đối chứng của A5: (1) ảnh nút khi `keyboard_focus` giống hệt ảnh khi tới nút bằng phím Tab thật, và
    khác ảnh không tiêu điểm; (2) cùng cách đo, widget không có dấu tiêu điểm → báo, nút chuẩn → không."""
    from PySide6.QtWidgets import QVBoxLayout

    if QGuiApplication.platformName() == "offscreen":
        pytest.skip(_OFFSCREEN_SKIP)
    ui_theme(False)  # như A5: giao diện thật của app
    root = QWidget()
    qtbot.addWidget(root)
    layout = QVBoxLayout(root)
    layout.setContentsMargins(12, 12, 12, 12)
    button = QPushButton("Nút")
    blank = _NoFocusMark()
    blank.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    blank.setFixedSize(60, 24)
    layout.addWidget(button)
    layout.addWidget(blank)
    root.show()
    settle()
    show_active(qtbot, root)
    area = focus_area(button, root)

    blank.setFocus()
    settle()
    press_tab(root)  # Tab thật: blank → (quay vòng) nút
    assert QApplication.focusWidget() is button
    assert keyboard_focus_mode(root)
    by_tab_key = root.grab(area).toImage()
    button.clearFocus()
    settle()
    unfocused = root.grab(area).toImage()
    keyboard_focus(button)
    emulated = root.grab(area).toImage()
    assert by_tab_key != unfocused, "style không vẽ dấu tiêu điểm cho nút chuẩn: đối chứng vô nghĩa"
    assert emulated == by_tab_key

    assert focus_visibility_gaps(root, [button, blank]) == [describe(blank)]


@pytest.mark.slow
def test_a5_default_button_exemption_is_narrow(qtbot, ui_theme):
    """Miễn E1f chỉ cho nút mặc định của hộp thoại: nút còn lại của cùng hộp vẫn được đo (và có dấu tiêu
    điểm), widget không dấu tiêu điểm trong hộp vẫn bị bắt; nút mặc định ngoài `QDialog` không được miễn."""
    from PySide6.QtWidgets import QDialog, QVBoxLayout

    if QGuiApplication.platformName() == "offscreen":
        pytest.skip(_OFFSCREEN_SKIP)
    ui_theme(False)
    dialog = QDialog()
    qtbot.addWidget(dialog)
    layout = QVBoxLayout(dialog)
    other = QPushButton("Khác")
    default = QPushButton("Mặc định")
    blank = _NoFocusMark()
    blank.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    blank.setFixedSize(60, 24)
    for w in (other, default, blank):
        layout.addWidget(w)
    default.setDefault(True)
    dialog.show()
    settle()
    show_active(qtbot, dialog)
    assert a5_exempt(default) and not a5_exempt(other)
    assert focus_visibility_gaps(dialog, [default, other, blank]) == [describe(blank)]

    plain = QWidget()
    qtbot.addWidget(plain)
    lone = QPushButton("Mặc định", plain)
    lone.setDefault(True)
    assert not a5_exempt(lone)


# =================================================================================================
# A6
# =================================================================================================


def _ui_dir() -> Path:
    spec = importlib.util.find_spec("teleport.ui")
    assert spec is not None and spec.origin is not None
    return Path(spec.origin).resolve().parent


def ruled_values(prop: str) -> set[str]:
    """Giá trị của property `prop` có luật trong stylesheet sáng và tối của `ui/theme.py`."""
    from teleport.ui import theme

    css = theme.stylesheet(False) + theme.stylesheet(True)
    return set(re.findall(rf'\[{re.escape(prop)}="([^"]+)"\]', css))


def test_a6_theme_has_rules_for_tone_and_surface():
    """Đối chứng cho test dưới: bộ đọc luật thấy tập giá trị của cả hai property (không rỗng)."""
    from teleport.ui import theme

    assert {"muted", "danger"} <= ruled_values(theme.TONE_PROPERTY)
    assert {"raised"} <= ruled_values(theme.SURFACE_PROPERTY)


@pytest.mark.parametrize(("kind", "name"), TARGETS, ids=TARGET_IDS)
def test_a6_tone_and_surface_values_have_theme_rules(qtbot, kind, name):
    from teleport.ui import theme

    root = build(qtbot, kind, name)
    allowed = {p: ruled_values(p) for p in (theme.TONE_PROPERTY, theme.SURFACE_PROPERTY)}
    bad = []
    for w in widgets(root):
        for prop, values in allowed.items():
            value = w.property(prop)
            if value is not None and str(value) not in values:
                bad.append(f"{describe(w)}: {prop}={value!r}")
    assert bad == []


_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\b")
_RGB = re.compile(r"\brgba?\s*\(")
# Miễn (rủi ro mục 6: miễn mới phải có lý do): `transparent` chỉ xóa nền pixmap trước khi vẽ, không phải màu
# hiện ra nên không có token tương ứng.
_NOT_A_COLOR = {"transparent"}


def _docstring_ids(tree: ast.AST) -> set[int]:
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }


def _global_color_names() -> set[str]:
    return {member.name for member in Qt.GlobalColor}


def hard_colors(where: str, tree: ast.AST) -> list[str]:
    """Mã màu cứng trong code (không tính docstring, chú thích)."""
    docs = _docstring_ids(tree)
    colors = _global_color_names()
    bad = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if _HEX_COLOR.search(node.value) or _RGB.search(node.value):
                bad.append(f"{where}:{line}: chuỗi {node.value[:50]!r}")
        elif isinstance(node, ast.Call):
            func = node.func
            fname = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            if (
                fname == "QColor"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, int | float)
            ):
                bad.append(f"{where}:{line}: QColor({node.args[0].value}, …)")
        elif isinstance(node, ast.Attribute):
            if node.attr in _NOT_A_COLOR:
                continue
            if isinstance(node.value, ast.Attribute) and node.value.attr == "GlobalColor":
                bad.append(f"{where}:{line}: GlobalColor.{node.attr}")
            elif isinstance(node.value, ast.Name) and node.value.id == "Qt" and node.attr in colors:
                bad.append(f"{where}:{line}: Qt.{node.attr}")
    return bad


def test_a6_hard_color_scan_finds_each_form():
    """Đối chứng cho test dưới: bắt đủ năm dạng, bỏ qua docstring và chú thích."""
    code = (
        '"""Nền #FFFFFF trong docstring không tính."""\n'
        "from PySide6.QtCore import Qt\n"
        "from PySide6.QtGui import QColor\n"
        "# chú thích #123456 không tính\n"
        'a = "color: #1a2b3c;"\n'
        'b = "background: rgba(0, 0, 0, 40)"\n'
        "c = QColor(255, 0, 0)\n"
        "d = QColor(Qt.GlobalColor.red)\n"
        "e = QColor(Qt.white)\n"
        "f = QColor(theme_value)\n"
        "pixmap.fill(Qt.GlobalColor.transparent)\n"
    )
    bad = hard_colors("x.py", ast.parse(code))
    assert sorted({int(b.split(":")[1]) for b in bad}) == [5, 6, 7, 8, 9], bad


def test_a6_no_hard_coded_colors_outside_theme():
    root = _ui_dir()
    bad: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "theme.py" and path.parent == root:
            continue
        bad += hard_colors(path.relative_to(root).as_posix(), ast.parse(path.read_bytes()))
    assert bad == []


# =================================================================================================
# A7
# =================================================================================================


def animation_uses(where: str, tree: ast.AST) -> list[str]:
    bad = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Name):
            names = [node.id]
        elif isinstance(node, ast.Attribute):
            names = [node.attr]
        elif isinstance(node, ast.ImportFrom):
            names = [alias.name for alias in node.names]
        names = [n for n in names if n in ANIMATION_CLASSES]
        bad += [f"{where}:{getattr(node, 'lineno', 0)}: {n}" for n in names]
    return bad


def test_a7_animation_scan_finds_import_and_use():
    code = (
        '"""QPropertyAnimation trong docstring không tính."""\n'
        "from PySide6.QtCore import QPropertyAnimation\n"
        "from PySide6 import QtCore\n"
        "a = QtCore.QVariantAnimation()\n"
        "b = QPropertyAnimation()\n"
    )
    bad = animation_uses("x.py", ast.parse(code))
    assert sorted({int(b.split(":")[1]) for b in bad}) == [2, 4, 5], bad


def test_a7_animation_classes_only_in_motion():
    root = _ui_dir()
    bad: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "motion.py" and path.parent == root:
            continue
        bad += animation_uses(path.relative_to(root).as_posix(), ast.parse(path.read_bytes()))
    assert bad == []


def test_a7_control_radar_animates_when_effects_are_on(qtbot, real_motion):
    """Đối chứng cho test dưới: hiệu ứng bật thì cảnh radar có hoạt ảnh/`Loop` đang chạy."""
    build(qtbot, "scene", "discover")
    settle()
    assert real_motion.running_count() > 0


@pytest.mark.parametrize(("kind", "name"), TARGETS, ids=TARGET_IDS)
def test_a7_no_animation_runs_when_os_disables_effects(qtbot, real_motion, monkeypatch, kind, name):
    monkeypatch.setattr(real_motion, "animations_enabled", lambda: False)
    build(qtbot, kind, name)
    settle(5)
    assert real_motion.running_count() == 0
