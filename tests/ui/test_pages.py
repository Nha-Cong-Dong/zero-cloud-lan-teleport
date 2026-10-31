"""Khung trang của cửa sổ chính: `Page`, `PageStack`, `MainWindow.open_page` / `go_back` / Esc (spec A3,
mục 2.5, test P1–P8 ở mục 6; mục 10 “Áp dụng hoi-lai” ghi đè phần trên).

Viết từ spec, không từ code:

- `PageStack` (objectName `pageStack`) là `centralWidget()`; trang HOME (`home_page`, objectName
  `homePage`) chứa toàn bộ thân cũ. `page_changed(str)` phát đúng một lần mỗi khi trang đổi thật, kể cả
  khi `reset()` đưa về HOME (mục 10, ý 8).
- Lịch sử: về HOME xóa lịch sử; mở trang đã có trong lịch sử thì cắt lịch sử tới ngay trước nó; còn lại
  đẩy (trang hiện tại, widget có tiêu điểm). `back()` trả tiêu điểm về widget đã nhớ nếu nó còn sống,
  còn trong trang đó, đang hiện và đang bật; không thì đặt tiêu điểm vào trang.
- Tiêu điểm được nhớ bằng `window().focusWidget()` (mục 10, ý 7), nên test kích hoạt cửa sổ trước khi
  bấm phím hay kiểm tiêu điểm.

Module `teleport.ui.*` chỉ import bên trong test/fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QVBoxLayout, QWidget

# =================================================================================================
# Hàm phụ trợ
# =================================================================================================


def _page(label: str = "A") -> tuple[QWidget, QPushButton]:
    """Một trang mẫu: một nút; trang đặt `setFocusProxy` tới nút (như spec yêu cầu với trang thật)."""
    page = QWidget()
    lay = QVBoxLayout(page)
    button = QPushButton(f"Nút của trang {label}")
    button.setObjectName(f"button{label}")
    lay.addWidget(button)
    page.setFocusProxy(button)
    return page, button


def _activate(qtbot, window: QWidget) -> None:
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    window.raise_()
    qtbot.waitUntil(window.isActiveWindow, timeout=3000)
    QApplication.processEvents()


def _record(signal) -> list[str]:
    seen: list[str] = []
    signal.connect(lambda value: seen.append(str(value)))
    return seen


def _running(window, tmp_path: Path):
    window.show_running("May-A", tmp_path / "nhan")
    return window


# =================================================================================================
# Fixture
# =================================================================================================


@pytest.fixture
def window(qtbot):
    from teleport.ui.main_window import MainWindow

    w = MainWindow()
    qtbot.addWidget(w)
    return w


@pytest.fixture
def stack(qtbot):
    """`PageStack` đứng riêng trong một cửa sổ, có đủ bốn trang (HOME thêm đầu tiên)."""
    from teleport.ui.main_window import Page, PageStack

    host = QWidget()
    lay = QVBoxLayout(host)
    s = PageStack()
    lay.addWidget(s)
    s.home_widget, s.home_button = _page("Home")
    s.add_page(Page.HOME, s.home_widget)
    s.buttons = {Page.HOME: s.home_button}
    for page, label in ((Page.DISCOVER, "D"), (Page.SETTINGS, "S"), (Page.WAIT_PAIRING, "W")):
        widget, button = _page(label)
        s.add_page(page, widget)
        s.buttons[page] = button
    s.host = host
    qtbot.addWidget(host)
    host.resize(400, 300)
    return s


# =================================================================================================
# Page enum
# =================================================================================================


def test_page_enum_values():
    from teleport.ui.main_window import Page

    assert {p.name: p.value for p in Page} == {
        "HOME": "home",
        "DISCOVER": "discover",
        "SETTINGS": "settings",
        "WAIT_PAIRING": "wait_pairing",
    }


# =================================================================================================
# P1 cấu trúc
# =================================================================================================


def test_central_widget_is_page_stack_with_home_current(window):
    from teleport.ui.main_window import Page, PageStack

    central = window.centralWidget()
    assert isinstance(central, PageStack)
    assert central is window.pages
    assert central.objectName() == "pageStack"
    assert window.pages.current_page is Page.HOME
    assert window.pages.has_page(Page.HOME)
    assert window.home_page.objectName() == "homePage"
    assert window.pages.currentWidget() is window.home_page


def test_home_page_holds_old_body(window):
    home = window.home_page
    for name in (
        "status_label",
        "detail_label",
        "address_label",
        "hint_label",
        "body",
        "conversations",
        "transfers_area",
    ):
        widget = getattr(window, name)
        assert home.isAncestorOf(widget), f"{name} phải nằm trong home_page"


def test_only_home_page_exists_by_default(window):
    from teleport.ui.main_window import Page

    # B5: DISCOVER là trang thật
    assert [p for p in Page if window.pages.has_page(p)] == [Page.HOME, Page.DISCOVER]


# =================================================================================================
# P2 add_page / show_page / has_page
# =================================================================================================


def test_add_page_rejects_duplicate(window):
    from teleport.ui.main_window import Page

    page, _ = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    assert window.pages.has_page(Page.WAIT_PAIRING)
    other, _ = _page("B")
    with pytest.raises(ValueError):
        window.add_page(Page.WAIT_PAIRING, other)
    with pytest.raises(ValueError):
        window.pages.add_page(Page.HOME, other)


def test_show_page_unknown_raises_key_error(window):
    from teleport.ui.main_window import Page

    assert not window.pages.has_page(Page.SETTINGS)
    with pytest.raises(KeyError):
        window.pages.show_page(Page.SETTINGS)
    assert window.pages.current_page is Page.HOME


def test_add_page_names_unnamed_widget_and_keeps_existing_name(window):
    from teleport.ui.main_window import Page

    unnamed, _ = _page()
    named, _ = _page("B")
    named.setObjectName("trangCaiDat")
    window.add_page(Page.WAIT_PAIRING, unnamed)
    window.add_page(Page.SETTINGS, named)
    assert unnamed.objectName() == "wait_pairingPage"
    assert named.objectName() == "trangCaiDat"


def test_add_page_does_not_switch_page(window):
    from teleport.ui.main_window import Page

    seen = _record(window.page_changed)
    page, _ = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    assert window.pages.current_page is Page.HOME
    assert seen == []


# =================================================================================================
# P3 open_page chỉ khi RUNNING
# =================================================================================================


@pytest.mark.parametrize("state", ["fresh", "welcome", "starting", "error"])
def test_open_page_refused_unless_running(window, state):
    from teleport.ui.main_window import Page

    page, _ = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    {
        "fresh": lambda: None,
        "welcome": lambda: window.show_welcome("May-A"),
        "starting": window.show_starting,
        "error": lambda: window.show_error("other", ""),
    }[state]()
    seen = _record(window.page_changed)
    assert window.open_page(Page.WAIT_PAIRING) is False
    assert window.pages.current_page is Page.HOME
    assert seen == []


def test_open_page_when_running_emits_once(window, tmp_path):
    from teleport.ui.main_window import Page, ViewState

    page, _ = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    _running(window, tmp_path)
    assert window.view_state is ViewState.RUNNING
    seen = _record(window.page_changed)
    pages_seen = _record(window.pages.page_changed)
    assert window.open_page(Page.WAIT_PAIRING) is True
    assert window.pages.current_page is Page.WAIT_PAIRING
    assert window.pages.currentWidget() is page
    assert seen == ["wait_pairing"]
    assert pages_seen == ["wait_pairing"]
    window.open_page(Page.WAIT_PAIRING)
    assert seen == ["wait_pairing"], "mở lại cùng trang không phát thêm"


def test_open_page_unknown_page_raises_when_running(window, tmp_path):
    from teleport.ui.main_window import Page

    _running(window, tmp_path)
    with pytest.raises(KeyError):
        window.open_page(Page.SETTINGS)


def test_go_back_delegates_to_page_stack(window, tmp_path):
    from teleport.ui.main_window import Page

    page, _ = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    _running(window, tmp_path)
    assert window.go_back() is False
    window.open_page(Page.WAIT_PAIRING)
    assert window.go_back() is True
    assert window.pages.current_page is Page.HOME
    assert window.go_back() is False


# =================================================================================================
# P4 lịch sử (PageStack đứng riêng)
# =================================================================================================


def test_page_stack_starts_on_first_added_home(stack):
    from teleport.ui.main_window import Page

    assert stack.current_page is Page.HOME
    assert stack.back() is False


def test_history_home_a_b_back_back(stack):
    from teleport.ui.main_window import Page

    seen = _record(stack.page_changed)
    stack.show_page(Page.DISCOVER)
    stack.show_page(Page.SETTINGS)
    assert stack.back() is True
    assert stack.current_page is Page.DISCOVER
    assert stack.back() is True
    assert stack.current_page is Page.HOME
    assert stack.back() is False
    assert stack.current_page is Page.HOME
    assert seen == ["discover", "settings", "discover", "home"]


def test_history_revisit_cuts_loop(stack):
    from teleport.ui.main_window import Page

    stack.show_page(Page.DISCOVER)
    stack.show_page(Page.SETTINGS)
    stack.show_page(Page.DISCOVER)
    assert stack.back() is True
    assert stack.current_page is Page.HOME
    assert stack.back() is False


def test_history_revisit_deeper_page_cuts_to_before_it(stack):
    from teleport.ui.main_window import Page

    stack.show_page(Page.DISCOVER)
    stack.show_page(Page.SETTINGS)
    stack.show_page(Page.WAIT_PAIRING)
    stack.show_page(Page.SETTINGS)
    assert stack.back() is True
    assert stack.current_page is Page.DISCOVER
    assert stack.back() is True
    assert stack.current_page is Page.HOME
    assert stack.back() is False


def test_show_home_clears_history(stack):
    from teleport.ui.main_window import Page

    stack.show_page(Page.DISCOVER)
    stack.show_page(Page.SETTINGS)
    stack.show_page(Page.HOME)
    assert stack.current_page is Page.HOME
    assert stack.back() is False


def test_show_same_page_is_noop(stack):
    from teleport.ui.main_window import Page

    seen = _record(stack.page_changed)
    stack.show_page(Page.HOME)
    assert seen == []
    stack.show_page(Page.DISCOVER)
    stack.show_page(Page.DISCOVER)
    assert seen == ["discover"]
    assert stack.back() is True
    assert stack.current_page is Page.HOME
    assert stack.back() is False, "trang trùng không được đẩy vào lịch sử"


def test_reset_returns_home_clears_history_and_emits_once(stack):
    from teleport.ui.main_window import Page

    stack.show_page(Page.DISCOVER)
    stack.show_page(Page.SETTINGS)
    seen = _record(stack.page_changed)
    stack.reset()
    assert stack.current_page is Page.HOME
    assert seen == ["home"]
    assert stack.back() is False
    stack.reset()
    assert seen == ["home"], "reset khi đã ở HOME không phát thêm"


def test_page_changed_value_is_page_value(stack):
    from teleport.ui.main_window import Page

    seen: list = []
    stack.page_changed.connect(seen.append)
    stack.show_page(Page.WAIT_PAIRING)
    assert seen == ["wait_pairing"]
    assert Page(seen[0]) is Page.WAIT_PAIRING


# =================================================================================================
# P5 Esc
# =================================================================================================


def test_back_action_is_window_esc_shortcut(window):
    from PySide6.QtGui import QAction

    action = window._back_action
    assert isinstance(action, QAction)
    assert action.shortcut() == QKeySequence(Qt.Key.Key_Escape)
    assert action.shortcutContext() == Qt.ShortcutContext.WindowShortcut
    assert action.text().replace("&", "") == "Quay lại"
    assert action.isEnabled() is False


def test_back_action_enabled_only_off_home(window, tmp_path):
    from teleport.ui.main_window import Page

    page, _ = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    _running(window, tmp_path)
    assert not window._back_action.isEnabled()
    window.open_page(Page.WAIT_PAIRING)
    assert window._back_action.isEnabled()
    window.go_back()
    assert not window._back_action.isEnabled()


def test_esc_on_page_goes_back_home(window, qtbot, tmp_path):
    from teleport.ui.main_window import Page

    page, _ = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    _running(window, tmp_path)
    _activate(qtbot, window)
    assert window.open_page(Page.WAIT_PAIRING)
    seen = _record(window.page_changed)
    QTest.keyClick(window, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: window.pages.current_page is Page.HOME, timeout=1000)
    assert seen == ["home"]


def test_esc_on_home_does_nothing(window, qtbot, tmp_path):
    from teleport.ui.main_window import Page

    page, _ = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    _running(window, tmp_path)
    _activate(qtbot, window)
    seen = _record(window.page_changed)
    assert not window._back_action.isEnabled()
    QTest.keyClick(window, Qt.Key.Key_Escape)
    QApplication.processEvents()
    assert seen == []
    assert window.pages.current_page is Page.HOME
    assert window.isVisible(), "Esc ở HOME không được đóng cửa sổ"


def test_esc_in_child_dialog_does_not_go_back(window, qtbot, tmp_path):
    """Hộp thoại (có cha) là cửa sổ riêng: Esc đóng hộp thoại, không quay lại trang (mục 10, ý 3)."""
    from teleport.ui.main_window import Page

    page, _ = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    _running(window, tmp_path)
    _activate(qtbot, window)
    window.open_page(Page.WAIT_PAIRING)
    dialog = QDialog(window)
    dialog.setLayout(QVBoxLayout())
    dialog.layout().addWidget(QPushButton("OK"))
    _activate(qtbot, dialog)
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not dialog.isVisible(), timeout=1000)
    QApplication.processEvents()
    assert window.pages.current_page is Page.WAIT_PAIRING


# =================================================================================================
# P6 tiêu điểm
# =================================================================================================


def test_focus_moves_to_page_proxy_and_returns_on_back(window, qtbot, tmp_path):
    from teleport.ui.main_window import Page

    page, button = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    _running(window, tmp_path)
    _activate(qtbot, window)
    target = window.conversations.add_button
    target.setFocus()
    qtbot.waitUntil(lambda: window.focusWidget() is target, timeout=1000)

    assert window.open_page(Page.WAIT_PAIRING)
    qtbot.waitUntil(lambda: window.focusWidget() is button, timeout=1000)

    assert window.go_back()
    qtbot.waitUntil(lambda: window.focusWidget() is target, timeout=1000)


def test_esc_back_also_restores_focus(window, qtbot, tmp_path):
    from teleport.ui.main_window import Page

    page, button = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    _running(window, tmp_path)
    _activate(qtbot, window)
    target = window.conversations.add_button
    target.setFocus()
    qtbot.waitUntil(lambda: window.focusWidget() is target, timeout=1000)
    window.open_page(Page.WAIT_PAIRING)
    qtbot.waitUntil(lambda: window.focusWidget() is button, timeout=1000)
    QTest.keyClick(button, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: window.focusWidget() is target, timeout=1000)


def _focus_in(page: QWidget, focus: QWidget | None) -> bool:
    return focus is not None and (focus is page or page.isAncestorOf(focus))


def test_back_with_deleted_remembered_widget_focuses_page(stack, qtbot):
    import shiboken6

    from teleport.ui.main_window import Page

    extra = QPushButton("Sẽ bị xóa")
    stack.home_widget.layout().addWidget(extra)
    _activate(qtbot, stack.host)
    extra.setFocus()
    qtbot.waitUntil(lambda: stack.host.focusWidget() is extra, timeout=1000)
    stack.show_page(Page.DISCOVER)
    qtbot.waitUntil(lambda: stack.host.focusWidget() is stack.buttons[Page.DISCOVER], timeout=1000)
    shiboken6.delete(extra)
    assert stack.back() is True
    assert stack.current_page is Page.HOME
    qtbot.waitUntil(lambda: _focus_in(stack.home_widget, stack.host.focusWidget()), timeout=1000)


@pytest.mark.parametrize("how", ["hidden", "disabled", "moved"])
def test_back_with_unusable_remembered_widget_focuses_page(stack, qtbot, how):
    from teleport.ui.main_window import Page

    extra = QPushButton("Không dùng được nữa")
    stack.home_widget.layout().addWidget(extra)
    _activate(qtbot, stack.host)
    extra.setFocus()
    qtbot.waitUntil(lambda: stack.host.focusWidget() is extra, timeout=1000)
    stack.show_page(Page.DISCOVER)
    qtbot.waitUntil(lambda: stack.host.focusWidget() is stack.buttons[Page.DISCOVER], timeout=1000)
    if how == "hidden":
        extra.hide()
    elif how == "disabled":
        extra.setEnabled(False)
    else:  # chuyển ra ngoài khung trang: vẫn sống, hiện, bật, nhưng không còn nằm trong HOME
        stack.host.layout().addWidget(extra)
        extra.show()  # đổi cha thì Qt ẩn widget: hiện lại để chỉ còn điều kiện “nằm trong trang” sai
        assert extra.isVisibleTo(stack.host)
    assert stack.back() is True
    assert stack.current_page is Page.HOME
    qtbot.waitUntil(lambda: _focus_in(stack.home_widget, stack.host.focusWidget()), timeout=1000)
    assert stack.host.focusWidget() is not extra


# =================================================================================================
# P7 show_error / show_welcome / show_starting đưa về HOME
# =================================================================================================

STATES = {
    "error": lambda w: w.show_error("other", ""),
    "welcome": lambda w: w.show_welcome("May-A"),
    "starting": lambda w: w.show_starting(),
}


@pytest.mark.parametrize("state", list(STATES))
def test_leaving_running_resets_to_home(window, qtbot, tmp_path, state):
    from teleport.ui.main_window import Page

    page, _ = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    _running(window, tmp_path)
    _activate(qtbot, window)

    # Mốc: tiêu điểm của `_place_focus` khi chuyển RUNNING → state mà chưa mở trang nào.
    STATES[state](window)
    QApplication.processEvents()
    expected = window.focusWidget()

    _running(window, tmp_path)
    QApplication.processEvents()
    assert window.open_page(Page.WAIT_PAIRING)
    seen = _record(window.page_changed)
    STATES[state](window)
    QApplication.processEvents()
    assert window.pages.current_page is Page.HOME
    assert seen == ["home"], "reset phải phát page_changed (mục 10, ý 8)"
    assert not window._back_action.isEnabled()
    assert window.pages.back() is False, "lịch sử phải rỗng"
    assert window.focusWidget() is expected, (
        f"tiêu điểm {window.focusWidget()!r}, như cũ phải là {expected!r}"
    )


# =================================================================================================
# P8 Tab không vào trang ẩn
# =================================================================================================


def test_tab_cycle_never_reaches_hidden_page(window, qtbot, tmp_path):
    from teleport.ui.main_window import Page

    page, button = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    _running(window, tmp_path)
    _activate(qtbot, window)
    assert not button.isVisibleTo(window)
    # Spec B1 mục 8c: ở RUNNING `status_label` nằm trong màn Bắt đầu (đang ẩn). Spec B2 mục 2.6: cửa sổ
    # mặc định (nền offscreen) hẹp hơn 820 px nên cột trái thu gọn và `name_label` ẩn; bắt đầu từ nút
    # “Tìm máy mới” (luôn hiện ở RUNNING).
    start = window.discover_button
    start.setFocus()
    qtbot.waitUntil(lambda: window.focusWidget() is start, timeout=1000)
    visited = []
    for _ in range(80):
        current = window.focusWidget()
        QTest.keyClick(current, Qt.Key.Key_Tab)
        QApplication.processEvents()
        focus = window.focusWidget()
        visited.append(focus)
        if focus is start:
            break
    assert visited[-1] is start, f"Tab không quay vòng về discover_button sau {len(visited)} bước"
    assert not any(_focus_in(page, w) for w in visited), "Tab vào widget của trang đang ẩn"


# =================================================================================================
# Hồi quy sau soát code: tiêu điểm khi về HOME, open_page bằng chuỗi, show_running không đổi trang
# =================================================================================================


def _real_home_control(window) -> bool:
    """Tiêu điểm nằm ở một control thật trong HOME: không phải chính `home_page`, nhận tiêu điểm,
    đang hiện."""
    focus = window.focusWidget()
    return (
        focus is not None
        and focus is not window.home_page
        and window.home_page.isAncestorOf(focus)
        and focus.focusPolicy() != Qt.FocusPolicy.NoFocus
        and focus.isVisible()
    )


def test_go_back_with_deleted_remembered_widget_focuses_real_home_control(window, qtbot, tmp_path):
    import shiboken6
    from PySide6.QtCore import QCoreApplication, QEvent

    from teleport.ui.main_window import Page

    page, button = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    extra = QPushButton("Sẽ bị xóa")
    window.home_page.layout().addWidget(extra)
    _running(window, tmp_path)
    _activate(qtbot, window)
    extra.setFocus()
    qtbot.waitUntil(lambda: window.focusWidget() is extra, timeout=1000)
    assert window.open_page(Page.WAIT_PAIRING)
    qtbot.waitUntil(lambda: window.focusWidget() is button, timeout=1000)

    extra.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(extra), "widget đã nhớ phải bị xóa thật trước khi go_back"

    assert window.go_back() is True
    assert window.pages.current_page is Page.HOME
    qtbot.waitUntil(lambda: _real_home_control(window), timeout=1000)
    assert window.focusWidget() is not window.home_page


def test_open_home_while_focus_on_home_page_moves_focus_to_real_control(window, qtbot, tmp_path):
    from teleport.ui.main_window import Page

    _running(window, tmp_path)
    _activate(qtbot, window)
    home = window.home_page
    policy = home.focusPolicy()
    # Đưa tiêu điểm vào chính `home_page` (như sau khi `back()` đặt tiêu điểm vào trang), rồi trả lại
    # chính sách cũ để trạng thái đúng như trang thật.
    home.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    home.setFocus()
    qtbot.waitUntil(lambda: window.focusWidget() is home, timeout=1000)
    home.setFocusPolicy(policy)
    if window.focusWidget() is not home:
        pytest.skip("Qt bỏ tiêu điểm khỏi home_page khi đổi focusPolicy: không dựng được ca này")

    assert window.open_page(Page.HOME) is True
    assert window.pages.current_page is Page.HOME
    qtbot.waitUntil(lambda: _real_home_control(window), timeout=1000)


def test_open_page_accepts_string_home_on_welcome(window):
    from teleport.ui.main_window import Page

    window.show_welcome("May-A")
    assert window.open_page("home") is True
    assert window.pages.current_page is Page.HOME


def test_open_page_accepts_string_when_running(window, tmp_path):
    from teleport.ui.main_window import Page

    page, _ = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    _running(window, tmp_path)
    assert window.open_page("wait_pairing") is True
    assert window.pages.current_page is Page.WAIT_PAIRING


def test_show_running_again_keeps_current_page_and_focus(window, qtbot, tmp_path):
    from teleport.ui.main_window import Page

    page, button = _page()
    window.add_page(Page.WAIT_PAIRING, page)
    _running(window, tmp_path)
    _activate(qtbot, window)
    assert window.open_page(Page.WAIT_PAIRING)
    qtbot.waitUntil(lambda: window.focusWidget() is button, timeout=1000)
    seen = _record(window.page_changed)

    window.show_running("May-B", tmp_path / "khac")
    QApplication.processEvents()
    assert window.pages.current_page is Page.WAIT_PAIRING
    assert window.pages.currentWidget() is page
    assert window.focusWidget() is button
    assert seen == []
    assert window.go_back() is True, "lịch sử phải còn nguyên"
