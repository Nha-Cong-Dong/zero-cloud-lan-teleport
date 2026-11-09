"""Hộp “Gửi n file tới…” khi gửi từ File Explorer (spec-f5 mục 2.1, 2.5, 2.6, mục 5 điểm 1, 3–7, 10 và
mục 9 D3g, D4, D7, D8, D9).

Viết từ spec, không từ code. Ba cách dựng:

- `SendPickerController` trên một `MainWindow` trần (fixture `env`): luật hộp, nút Gửi, bàn phím, trạng
  thái rỗng, chữ thuần, không I/O. Seam `send_picker._arm_ms` (đọc lúc mở hộp) thay hạn chờ 500 ms.
- `GuiApp` dựng thẳng, không node (fixture `gapp`): `_on_send_requested`, `_on_picker_send` (D7), Thoát
  và `shutdown` đóng hộp, “Tìm máy mới”.
- `main()` trong tiến trình (fixture `gui`), node thật trên 127.0.0.1 → `integration`: `--send A B` khi
  chưa có bản nào (hộp mở sau khi node chạy, không `request_send` nào), màn lần chạy đầu giữ yêu cầu,
  `start_failed` bỏ yêu cầu.

Không có máy đã ghép đôi thật trong các ca `gui`: đường bấm Gửi → `request_send` kiểm ở mức `GuiApp`
(spy trên `TransfersController.request_send`).
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QDialog,
    QLabel,
    QPushButton,
    QWidget,
)

integration = pytest.mark.integration

PEER_ID = "a71c04d2" + "e" * 24
OTHER_ID = "0f0f0f0f" + "1" * 24
STRANGER_ID = "9" * 32

# Câu chữ nguyên văn (spec-f5 mục 2.6).
WARNING = "Teleport chỉ gửi khi bạn chọn máy và bấm Gửi. Nếu bạn không tự chọn các file này, hãy bấm Hủy."
EMPTY = "Chưa ghép đôi với máy nào. Hãy ghép đôi một máy rồi gửi lại từ File Explorer."
DISCOVER = "Tìm máy mới"
BUSY = "Đang mở một hộp gửi file. Hãy gửi hoặc hủy các file này trước, rồi gửi tiếp từ File Explorer."
FILES_LABEL = "File sẽ gửi"
DEVICES_LABEL = "Gửi tới máy"
ONLINE = "Trực tuyến"
OFFLINE = "Ngoại tuyến"
NOT_PAIRED_HEAD = "Chưa ghép đôi với "  # send.not_paired_message, mvp mục 8


def P(*parts: str) -> Path:
    root = "C:\\" if os.name == "nt" else "/"
    return Path(root, "Users", "An", *parts)


A = P("Tài liệu", "báo cáo.pdf")
B = P("Ảnh", "video.mp4")


def plain(text: str) -> str:
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


def dview(device_id: str, name: str, *, online: bool = True, paired: bool = True):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id,
        name=name,
        os="windows",
        paired=paired,
        online=online,
        connected=False,
        addresses=("192.0.2.7",) if online else (),
        port=47474 if online else None,
    )


def mod():
    from teleport.ui import send_picker

    return send_picker


def child(root, cls, name: str):
    found = root.findChild(cls, name)
    assert found is not None, f"không có {cls.__name__} objectName={name!r} (spec-f5 mục 2.5)"
    return found


def devices_view(dlg) -> QAbstractItemView:
    return child(dlg, QAbstractItemView, "sendPickerDevices")


def files_view(dlg) -> QAbstractItemView:
    return child(dlg, QAbstractItemView, "sendPickerFiles")


def send_button(dlg) -> QPushButton:
    return child(dlg, QPushButton, "sendPickerSend")


def cancel_button(dlg) -> QPushButton:
    return child(dlg, QPushButton, "sendPickerCancel")


def discover_button(dlg) -> QAbstractButton:
    return child(dlg, QAbstractButton, "sendPickerDiscover")


_ROLES = [
    Qt.ItemDataRole.DisplayRole,
    Qt.ItemDataRole.ToolTipRole,
    Qt.ItemDataRole.AccessibleTextRole,
    Qt.ItemDataRole.AccessibleDescriptionRole,
    *[Qt.ItemDataRole(int(Qt.ItemDataRole.UserRole) + i) for i in range(16)],
]


def row_strings(view: QAbstractItemView, row: int) -> list[str]:
    index = view.model().index(row, 0)
    out = []
    for role in _ROLES:
        value = index.data(role)
        if isinstance(value, str) and value:
            out.append(value)
    return out


def rows(view: QAbstractItemView) -> list[str]:
    model = view.model()
    return [str(model.index(r, 0).data(Qt.ItemDataRole.DisplayRole)) for r in range(model.rowCount())]


def row_of(view: QAbstractItemView, label: str) -> int:
    names = rows(view)
    hits = [i for i, text in enumerate(names) if label in text]
    assert len(hits) == 1, (label, names)
    return hits[0]


def select(dlg, label: str) -> None:
    view = devices_view(dlg)
    index = view.model().index(row_of(view, label), 0)
    view.setFocus()
    view.setCurrentIndex(index)


def has_selection(dlg) -> bool:
    view = devices_view(dlg)
    return bool(view.selectionModel().selectedIndexes())


def activate(qtbot, dlg) -> None:
    """Đưa hộp thành cửa sổ active (offscreen có thể không tự làm: khi đó gửi `WindowActivate`)."""
    qtbot.waitExposed(dlg)
    dlg.raise_()
    dlg.activateWindow()
    with contextlib.suppress(Exception):
        qtbot.waitUntil(dlg.isActiveWindow, timeout=500)
    if not dlg.isActiveWindow():
        QApplication.sendEvent(dlg, QEvent(QEvent.Type.WindowActivate))


def visible_texts(root: QWidget) -> list[str]:
    out = [root.windowTitle()] if root.windowTitle() else []
    for w in root.findChildren(QLabel):
        if w.isVisibleTo(root) and w.text():
            out.append(w.text())
    for w in root.findChildren(QAbstractButton):
        if w.isVisibleTo(root) and w.text():
            out.append(plain(w.text()))
    for w in root.findChildren(QWidget):
        text = getattr(w, "text", None)
        if type(w).__name__ == "InfoBar" and w.isVisibleTo(root) and callable(text) and text():
            out.append(text())
    return out


def is_closed(ctl, dlg) -> bool:
    from shiboken6 import isValid

    return ctl.dialog is None and (not isValid(dlg) or not dlg.isVisible())


# =================================================================================================
# Fixture: controller trên MainWindow trần
# =================================================================================================


@pytest.fixture
def env(qtbot, monkeypatch):
    from teleport.ui.main_window import MainWindow

    sp = mod()
    monkeypatch.setattr(sp, "_arm_ms", 0)
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    ctl = sp.SendPickerController(window)
    confirmed: list = []
    discovers: list = []
    ctl.send_confirmed.connect(lambda device_id, paths: confirmed.append((device_id, paths)))
    ctl.discover_requested.connect(lambda: discovers.append(1))
    ns = SimpleNamespace(window=window, ctl=ctl, confirmed=confirmed, discovers=discovers, qtbot=qtbot)
    yield ns
    ctl.close_all()
    QApplication.processEvents()


def open_picker(env, paths=(A, B), devices=None):
    if devices is None:
        devices = [dview(PEER_ID, "PC-Nha"), dview(OTHER_ID, "Laptop", online=False)]
    env.window.devices.reset(devices)
    env.ctl.set_ready(True)
    assert env.ctl.request(list(paths)) is True
    dlg = env.ctl.dialog
    assert dlg is not None, "node sẵn sàng thì hộp mở ngay"
    activate(env.qtbot, dlg)
    return dlg


def armed_with(env, label: str = "PC-Nha", **kw):
    dlg = open_picker(env, **kw)
    select(dlg, label)
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=2_000)
    return dlg


# =================================================================================================
# Mở hộp, nội dung (mục 2.5, tiêu chí 1)
# =================================================================================================


def test_texts_match_spec():
    sp = mod()
    assert sp.picker_title(2) == "Gửi 2 file tới…"
    assert sp.picker_title(1) == "Gửi 1 file tới…"
    assert sp.files_label() == FILES_LABEL
    assert sp.devices_label() == DEVICES_LABEL
    assert sp.warning_text() == WARNING
    assert sp.empty_text() == EMPTY
    assert sp.discover_text() == DISCOVER
    assert sp.busy_notice() == BUSY
    assert sp.file_row_accessible("a.txt", "C:\\Thu muc") == "a.txt, trong C:\\Thu muc"


def test_presence_texts_d9():
    from teleport.ui import texts

    assert texts.presence_online() == ONLINE
    assert texts.presence_offline() == OFFLINE


def test_ready_request_opens_dialog_with_files_in_order(env):
    from teleport.ui import texts

    dlg = open_picker(env, paths=(B, A))
    assert dlg.objectName() == "sendPickerDialog"
    assert dlg.isVisible()
    assert dlg.windowTitle() == mod().picker_title(2)
    assert dlg.accessibleName() == mod().picker_title(2)
    assert rows(files_view(dlg)) == [texts.file_label(B.name), texts.file_label(A.name)], "đúng thứ tự"
    assert env.confirmed == []


def test_dialog_is_child_of_main_window_and_not_delete_on_close(env):
    dlg = open_picker(env)
    assert dlg.parent() is env.window
    assert not dlg.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    assert isinstance(dlg, QDialog)


def test_labels_and_warning_are_shown(env):
    dlg = open_picker(env)
    shown = visible_texts(dlg)
    for text in (FILES_LABEL, DEVICES_LABEL, WARNING, "Hủy", "Gửi"):
        assert text in shown, (text, shown)


def test_file_rows_show_parent_folder_and_full_path_tooltip(env):
    from teleport.ui import send

    dlg = open_picker(env, paths=(A,))
    strings = row_strings(files_view(dlg), 0)
    assert any(send.printable(str(A.parent)) in s for s in strings if s != str(A)), strings
    tooltip = files_view(dlg).model().index(0, 0).data(Qt.ItemDataRole.ToolTipRole)
    assert tooltip and send.printable(str(A)) in tooltip


def test_file_list_is_read_only_and_at_most_six_rows_high(env):
    many = [P("d", f"f{i}.txt") for i in range(12)]
    dlg = open_picker(env, paths=many)
    view = files_view(dlg)
    assert view.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    row_h = view.sizeHintForRow(0)
    assert row_h > 0
    assert view.height() <= 6 * row_h + 2 * view.frameWidth() + 8, (view.height(), row_h)


def test_only_paired_devices_listed_with_presence_in_words_and_none_preselected(env):
    from teleport.ui import texts

    dlg = open_picker(
        env,
        devices=[
            dview(PEER_ID, "PC-Nha", online=True),
            dview(OTHER_ID, "Laptop", online=False),
            dview(STRANGER_ID, "May-La", online=True, paired=False),
        ],
    )
    view = devices_view(dlg)
    labels = rows(view)
    assert len(labels) == 2
    assert not any("May-La" in label for label in labels), "chỉ máy đã ghép đôi"
    on = row_strings(view, row_of(view, texts.peer_label("PC-Nha", PEER_ID)))
    off = row_strings(view, row_of(view, texts.peer_label("Laptop", OTHER_ID)))
    assert any(ONLINE in s for s in on) and not any(OFFLINE in s for s in on), on
    assert any(OFFLINE in s for s in off), off
    assert not has_selection(dlg), "Q1: không chọn sẵn"
    assert not send_button(dlg).isEnabled()


def test_single_device_is_not_preselected(env):
    dlg = open_picker(env, devices=[dview(PEER_ID, "PC-Nha")])
    assert not has_selection(dlg)
    assert not send_button(dlg).isEnabled()


def test_device_list_updates_live(env):
    dlg = open_picker(env, devices=[dview(PEER_ID, "PC-Nha")])
    env.window.devices.reset([dview(PEER_ID, "PC-Nha"), dview(OTHER_ID, "Laptop")])
    env.qtbot.waitUntil(lambda: len(rows(devices_view(dlg))) == 2, timeout=2_000)


# =================================================================================================
# Nút Gửi: chọn máy + hạn chờ (tiêu chí 3, D4)
# =================================================================================================


def test_send_needs_selection_even_after_arm(env):
    dlg = open_picker(env)
    env.qtbot.wait(100)
    assert not send_button(dlg).isEnabled()
    select(dlg, "PC-Nha")
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=2_000)


@pytest.mark.slow
def test_send_disabled_until_arm_time_after_activation(env, monkeypatch):
    monkeypatch.setattr(mod(), "_arm_ms", 400)
    t0 = time.monotonic()
    dlg = open_picker(env)
    select(dlg, "PC-Nha")
    assert not send_button(dlg).isEnabled(), "đã chọn máy nhưng chưa qua hạn chờ"
    QTest.mouseClick(send_button(dlg), Qt.MouseButton.LeftButton)
    QTest.keyClick(devices_view(dlg), Qt.Key.Key_Return)
    assert env.confirmed == [], "bấm nhầm trong hạn chờ không gửi"
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=3_000)
    assert time.monotonic() - t0 >= 0.35
    assert env.confirmed == []


def test_arm_ms_is_read_when_dialog_opens(env, monkeypatch):
    monkeypatch.setattr(mod(), "_arm_ms", 60_000)
    dlg = open_picker(env)
    monkeypatch.setattr(mod(), "_arm_ms", 0)  # đổi sau khi mở: hộp đang mở giữ hạn cũ
    select(dlg, "PC-Nha")
    env.qtbot.wait(300)
    assert not send_button(dlg).isEnabled()


def test_default_arm_constant_is_500_ms():
    assert mod().SEND_ARM_MS == 500


def test_send_click_emits_once_with_device_and_paths_and_closes(env):
    dlg = armed_with(env)
    QTest.mouseClick(send_button(dlg), Qt.MouseButton.LeftButton)
    env.qtbot.waitUntil(lambda: is_closed(env.ctl, dlg), timeout=2_000)
    assert env.confirmed == [(PEER_ID, (A, B))]
    assert isinstance(env.confirmed[0][1], tuple)
    env.qtbot.wait(100)
    assert len(env.confirmed) == 1


@pytest.mark.parametrize("how", ["cancel", "escape", "close", "reject"])
def test_cancel_escape_and_close_send_nothing(env, how):
    dlg = armed_with(env)
    if how == "cancel":
        QTest.mouseClick(cancel_button(dlg), Qt.MouseButton.LeftButton)
    elif how == "escape":
        QTest.keyClick(devices_view(dlg), Qt.Key.Key_Escape)
    elif how == "close":
        dlg.close()
    else:
        dlg.reject()
    env.qtbot.waitUntil(lambda: is_closed(env.ctl, dlg), timeout=2_000)
    env.qtbot.wait(100)
    assert env.confirmed == []
    assert env.ctl.pending is None


def test_new_request_allowed_after_cancel(env):
    dlg = armed_with(env)
    QTest.mouseClick(cancel_button(dlg), Qt.MouseButton.LeftButton)
    env.qtbot.waitUntil(lambda: is_closed(env.ctl, dlg), timeout=2_000)
    assert env.ctl.request([A]) is True
    assert env.ctl.dialog is not None and env.ctl.dialog is not dlg


# =================================================================================================
# D8: bấm đúp chỉ chọn, Enter gửi một lần (tiêu chí 4)
# =================================================================================================


def test_double_click_only_selects(env):
    dlg = open_picker(env)
    env.qtbot.wait(50)
    view = devices_view(dlg)
    rect = view.visualRect(view.model().index(row_of(view, "PC-Nha"), 0))
    QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    env.qtbot.waitUntil(lambda: has_selection(dlg), timeout=2_000)
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=2_000)
    QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    env.qtbot.wait(200)
    assert env.confirmed == [], "bấm đúp không gửi"
    assert env.ctl.dialog is dlg and dlg.isVisible()


def test_activated_signal_does_not_send(env):
    """D8, D18: `activated` không gửi và không nối vào việc chọn (lựa chọn giữ nguyên)."""
    from teleport.ui import texts

    dlg = armed_with(env)
    view = devices_view(dlg)
    view.activated.emit(view.model().index(row_of(view, "Laptop"), 0))
    env.qtbot.wait(200)
    assert env.confirmed == []
    assert env.ctl.dialog is dlg and dlg.isVisible()
    [index] = view.selectionModel().selectedIndexes()
    assert texts.peer_label("PC-Nha", PEER_ID) in str(index.data(Qt.ItemDataRole.DisplayRole))


def test_enter_on_device_list_sends_once_when_enabled(env):
    dlg = armed_with(env)
    view = devices_view(dlg)
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Return)
    env.qtbot.waitUntil(lambda: is_closed(env.ctl, dlg), timeout=2_000)
    env.qtbot.wait(100)
    assert env.confirmed == [(PEER_ID, (A, B))], "Enter gửi đúng một lần"


def test_enter_without_selection_selects_current_row_and_does_not_send(env):
    """D18: Enter khi chưa chọn máy thì chọn hàng hiện hành, không gửi; Enter lần nữa (Gửi đã bật) mới gửi."""
    dlg = open_picker(env)
    view = devices_view(dlg)
    env.qtbot.wait(100)
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Return)
    env.qtbot.wait(100)
    assert env.confirmed == []
    assert env.ctl.dialog is dlg and dlg.isVisible()
    assert has_selection(dlg), "Enter khi chưa chọn: chọn hàng hiện hành"
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=2_000)
    QTest.keyClick(view, Qt.Key.Key_Return)
    env.qtbot.waitUntil(lambda: is_closed(env.ctl, dlg), timeout=2_000)
    assert len(env.confirmed) == 1


def selected_labels(dlg) -> list[str]:
    view = devices_view(dlg)
    return [str(i.data(Qt.ItemDataRole.DisplayRole)) for i in view.selectionModel().selectedIndexes()]


def test_down_selects_the_only_device(env):
    """D18 (N12): một máy, chưa chọn: Down chọn được máy đó."""
    dlg = open_picker(env, devices=[dview(PEER_ID, "PC-Nha")])
    view = devices_view(dlg)
    view.setFocus()
    env.qtbot.wait(50)
    QTest.keyClick(view, Qt.Key.Key_Down)
    assert len(selected_labels(dlg)) == 1 and "PC-Nha" in selected_labels(dlg)[0]
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=2_000)
    assert env.confirmed == []


@pytest.mark.parametrize(
    "key",
    [Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown],
    ids=["down", "up", "home", "end", "pgup", "pgdn"],
)
def test_first_navigation_key_selects_first_row(env, key):
    """D18: hai máy, chưa chọn: phím di chuyển đầu tiên chọn hàng hiện hành (máy 1) và bị nuốt."""
    dlg = open_picker(env, devices=[dview(PEER_ID, "PC-Nha"), dview(OTHER_ID, "Laptop", online=False)])
    view = devices_view(dlg)
    first = rows(view)[0]
    view.setFocus()
    env.qtbot.wait(50)
    QTest.keyClick(view, key)
    assert selected_labels(dlg) == [first]
    assert env.confirmed == []


def test_second_down_moves_to_second_device(env):
    dlg = open_picker(env, devices=[dview(PEER_ID, "PC-Nha"), dview(OTHER_ID, "Laptop", online=False)])
    view = devices_view(dlg)
    second = rows(view)[1]
    view.setFocus()
    env.qtbot.wait(50)
    QTest.keyClick(view, Qt.Key.Key_Down)
    QTest.keyClick(view, Qt.Key.Key_Down)
    assert selected_labels(dlg) == [second]


def test_send_is_default_button(env):
    dlg = open_picker(env)
    assert send_button(dlg).isDefault() or send_button(dlg).autoDefault()


# =================================================================================================
# Yêu cầu đầu thắng (tiêu chí 5, Q2, D4)
# =================================================================================================


def test_second_request_is_dropped_list_unchanged_and_busy_notice_shown(env):
    from teleport.ui import texts

    dlg = open_picker(env, paths=(A, B))
    assert env.ctl.request([P("khac.txt")]) is False
    assert env.ctl.dialog is dlg
    dialogs = [w for w in env.window.findChildren(QDialog, "sendPickerDialog") if w.isVisible()]
    assert dialogs == [dlg], "không chồng hộp"
    assert rows(files_view(dlg)) == [texts.file_label(A.name), texts.file_label(B.name)]
    env.qtbot.waitUntil(lambda: BUSY in visible_texts(dlg), timeout=2_000)
    assert BUSY in (dlg.accessibleDescription() or ""), "InfoBar vào mô tả trợ năng"


@pytest.mark.slow
def test_busy_request_disarms_send_and_rearms_without_new_activation(env, monkeypatch):
    """D4: hộp bị đưa lên lại vì yêu cầu bận thì tắt Gửi và tính hạn lại (rồi tự bật lại)."""
    monkeypatch.setattr(mod(), "_arm_ms", 300)
    dlg = open_picker(env)
    select(dlg, "PC-Nha")
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=3_000)
    assert env.ctl.request([P("x.txt")]) is False
    assert not send_button(dlg).isEnabled(), "yêu cầu bận: tắt Gửi ngay"
    QTest.mouseClick(send_button(dlg), Qt.MouseButton.LeftButton)
    assert env.confirmed == []
    t0 = time.monotonic()
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=3_000)
    assert time.monotonic() - t0 >= 0.2, "hạn chờ tính lại từ đầu"
    assert has_selection(dlg), "lựa chọn máy giữ nguyên"


@pytest.mark.slow
def test_busy_spam_does_not_keep_send_disabled(env, monkeypatch):
    """D17: `show_busy` tối đa một lần mỗi `BUSY_REPEAT_MS`; yêu cầu bận dồn dập không giữ Gửi tắt mãi."""
    sp = mod()
    monkeypatch.setattr(sp, "_arm_ms", 200)
    monkeypatch.setattr(sp, "BUSY_REPEAT_MS", 2_000, raising=False)
    dlg = open_picker(env)
    select(dlg, "PC-Nha")
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=3_000)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.2:
        assert env.ctl.request([P("spam.txt")]) is False
        env.qtbot.wait(40)
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=500)
    QTest.mouseClick(send_button(dlg), Qt.MouseButton.LeftButton)
    env.qtbot.waitUntil(lambda: is_closed(env.ctl, dlg), timeout=2_000)
    assert env.confirmed == [(PEER_ID, (A, B))]


def test_busy_repeat_constant_is_2000_ms():
    assert mod().BUSY_REPEAT_MS == 2_000


@pytest.mark.slow
def test_busy_after_repeat_interval_disarms_again(env, monkeypatch):
    """D17: qua `BUSY_REPEAT_MS` thì yêu cầu bận lại đưa hộp lên và tính lại hạn chờ."""
    sp = mod()
    monkeypatch.setattr(sp, "_arm_ms", 300)
    monkeypatch.setattr(sp, "BUSY_REPEAT_MS", 200, raising=False)
    dlg = open_picker(env)
    select(dlg, "PC-Nha")
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=3_000)
    env.ctl.request([P("x.txt")])
    assert not send_button(dlg).isEnabled()
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=3_000)
    env.qtbot.wait(250)
    env.ctl.request([P("y.txt")])
    assert not send_button(dlg).isEnabled(), "qua BUSY_REPEAT_MS: yêu cầu bận tắt Gửi lại"


@pytest.mark.slow
def test_reorder_while_selected_disarms_then_rearms(env, monkeypatch):
    """D19: đang chọn máy mà thứ tự hàng đổi (máy khác lên mạng) → tắt Gửi, tính lại hạn chờ; máy đã
    chọn giữ nguyên."""
    monkeypatch.setattr(mod(), "_arm_ms", 300)
    dlg = open_picker(env, devices=[dview(PEER_ID, "PC-Nha"), dview(OTHER_ID, "Laptop", online=False)])
    view = devices_view(dlg)
    before = rows(view)
    select(dlg, "PC-Nha")
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=3_000)
    env.window.devices.reset([dview(PEER_ID, "PC-Nha"), dview(OTHER_ID, "Laptop", online=True)])
    env.qtbot.waitUntil(lambda: rows(view) != before, timeout=2_000)
    assert [r for r in rows(view) if "PC-Nha" in r] == [r for r in before if "PC-Nha" in r]
    assert rows(view).index(next(r for r in rows(view) if "PC-Nha" in r)) != before.index(
        next(r for r in before if "PC-Nha" in r)
    ), "dữ liệu thử phải đổi vị trí của máy đang chọn"
    assert not send_button(dlg).isEnabled(), "thứ tự đổi: tắt Gửi"
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert env.confirmed == []
    t0 = time.monotonic()
    env.qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=3_000)
    assert time.monotonic() - t0 >= 0.2
    assert ["PC-Nha" in label for label in selected_labels(dlg)] == [True]


def test_gapp_does_not_bring_window_again_while_picker_open_or_pending(qtbot, gapp, monkeypatch):
    """D17: hộp đang mở hay có yêu cầu chờ thì `GuiApp` không `bring_to_front` lại."""
    calls: list[int] = []
    real = gapp.window.bring_to_front
    monkeypatch.setattr(gapp.window, "bring_to_front", lambda: (calls.append(1), real())[1])
    gapp._on_send_requested((A,))  # chưa sẵn sàng: giữ chờ
    assert calls == [1]
    gapp._on_send_requested((B,))
    assert calls == [1], "đang có yêu cầu chờ: không đưa cửa sổ lên lại"
    gapp.window.devices.reset([dview(PEER_ID, "PC-Nha")])
    gapp.send_picker.set_ready(True)
    assert gapp.send_picker.dialog is not None
    calls.clear()
    for _ in range(5):
        gapp._on_send_requested((B,))
    # `GuiApp` không tự đưa lên lại; chỉ `show_busy` (tối đa một lần mỗi `BUSY_REPEAT_MS`) được phép.
    assert len(calls) <= 1, f"hộp đang mở: đưa cửa sổ lên {len(calls)} lần cho 5 yêu cầu bận"


def test_hundred_requests_keep_one_dialog_and_same_files(env):
    dlg = open_picker(env)
    for i in range(100):
        assert env.ctl.request([P(f"spam{i}.txt")]) is False
    assert env.ctl.dialog is dlg
    assert len([w for w in env.window.findChildren(QDialog, "sendPickerDialog") if w.isVisible()]) == 1
    assert len(rows(files_view(dlg))) == 2


# =================================================================================================
# Chưa sẵn sàng, set_ready, discard_pending, close_all (tiêu chí 6, Q5)
# =================================================================================================


def test_request_before_ready_is_held_then_opened(env):
    from teleport.ui import texts

    env.window.devices.reset([dview(PEER_ID, "PC-Nha")])
    assert env.ctl.request([A, B]) is True
    assert env.ctl.dialog is None
    assert env.ctl.pending == (A, B)
    env.ctl.set_ready(True)
    dlg = env.ctl.dialog
    assert dlg is not None and dlg.isVisible()
    assert env.ctl.pending is None
    assert rows(files_view(dlg)) == [texts.file_label(A.name), texts.file_label(B.name)]


def test_second_request_while_pending_is_dropped(env):
    assert env.ctl.request([A]) is True
    assert env.ctl.request([B]) is False
    assert env.ctl.pending == (A,)


def test_discard_pending_drops_request_silently(env):
    env.ctl.request([A])
    env.ctl.discard_pending()
    assert env.ctl.pending is None
    env.ctl.set_ready(True)
    assert env.ctl.dialog is None
    assert env.confirmed == []


def test_set_ready_false_closes_open_dialog(env):
    dlg = armed_with(env)
    env.ctl.set_ready(False)
    env.qtbot.waitUntil(lambda: is_closed(env.ctl, dlg), timeout=2_000)
    assert env.confirmed == []
    assert env.ctl.request([A]) is True and env.ctl.dialog is None, "chưa sẵn sàng lại: giữ chờ"
    assert env.ctl.pending == (A,)


def test_close_all_closes_dialog_and_pending_and_is_idempotent(env):
    dlg = open_picker(env)
    env.ctl.close_all()
    env.qtbot.waitUntil(lambda: is_closed(env.ctl, dlg), timeout=2_000)
    env.ctl.close_all()
    env.ctl.set_ready(False)
    env.ctl.request([A])
    env.ctl.close_all()
    assert env.ctl.pending is None and env.ctl.dialog is None
    env.ctl.close_all()
    assert env.confirmed == []


# =================================================================================================
# Máy biến mất, trạng thái rỗng (tiêu chí 7, D4)
# =================================================================================================


def test_selected_device_removed_unselects_and_disables_send(env):
    dlg = armed_with(env)
    env.window.devices.reset([dview(OTHER_ID, "Laptop")])
    env.qtbot.waitUntil(lambda: not send_button(dlg).isEnabled(), timeout=2_000)
    assert not has_selection(dlg)
    QTest.keyClick(devices_view(dlg), Qt.Key.Key_Return)
    env.qtbot.wait(100)
    assert env.confirmed == []


def test_empty_state_shows_text_and_discover_button_with_focus(env):
    dlg = open_picker(env, devices=[dview(STRANGER_ID, "May-La", paired=False)])
    env.qtbot.waitUntil(lambda: EMPTY in visible_texts(dlg), timeout=2_000)
    button = discover_button(dlg)
    assert button.isVisibleTo(dlg) and plain(button.text()) == DISCOVER
    assert not send_button(dlg).isEnabled()
    env.qtbot.waitUntil(lambda: dlg.focusWidget() is button, timeout=2_000)


def test_discover_closes_dialog_and_emits_discover_requested(env):
    dlg = open_picker(env, devices=[])
    QTest.mouseClick(discover_button(dlg), Qt.MouseButton.LeftButton)
    env.qtbot.waitUntil(lambda: is_closed(env.ctl, dlg), timeout=2_000)
    assert env.discovers == [1]
    assert env.confirmed == []


def test_empty_state_switches_to_list_when_a_device_arrives(env):
    dlg = open_picker(env, devices=[])
    env.qtbot.waitUntil(lambda: EMPTY in visible_texts(dlg), timeout=2_000)
    env.window.devices.reset([dview(PEER_ID, "PC-Nha")])
    env.qtbot.waitUntil(lambda: EMPTY not in visible_texts(dlg), timeout=2_000)
    view = devices_view(dlg)
    assert view.isVisibleTo(dlg) and len(rows(view)) == 1
    assert not discover_button(dlg).isVisibleTo(dlg)
    assert not has_selection(dlg)
    env.qtbot.waitUntil(lambda: dlg.focusWidget() is view, timeout=2_000)


# =================================================================================================
# Bàn phím, trợ năng (N12)
# =================================================================================================


def test_initial_focus_is_device_list(env):
    dlg = open_picker(env)
    env.qtbot.waitUntil(lambda: dlg.focusWidget() is devices_view(dlg), timeout=2_000)


def test_tab_order_files_devices_cancel_send(env):
    dlg = armed_with(env)
    order = [files_view(dlg), devices_view(dlg), cancel_button(dlg), send_button(dlg)]
    order[0].setFocus(Qt.FocusReason.TabFocusReason)
    env.qtbot.waitUntil(lambda: QApplication.focusWidget() is order[0], timeout=2_000)
    for nxt in order[1:]:
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
        env.qtbot.waitUntil(lambda n=nxt: QApplication.focusWidget() is n, timeout=1_000)


def test_accessible_description_has_warning(env):
    dlg = open_picker(env)
    assert WARNING in (dlg.accessibleDescription() or "")


def test_file_rows_have_accessible_name_with_folder(env):
    from teleport.ui import send, texts

    dlg = open_picker(env, paths=(A,))
    want = mod().file_row_accessible(texts.file_label(A.name), send.printable(str(A.parent)))
    assert want in row_strings(files_view(dlg), 0)


# =================================================================================================
# Chữ thuần (tiêu chí 10)
# =================================================================================================


def test_malicious_file_and_device_names_are_plain_text(env):
    from teleport.ui import texts

    evil_file = P("<b>dam</b>", "anh\u202egpj.exe")
    hidden = P("dir", "a\u200bb<i>x</i>.txt")
    dlg = open_picker(env, paths=(evil_file, hidden), devices=[dview(PEER_ID, "<b>May</b>\u202eX")])
    env.ctl.request([P("khac.txt")])  # hiện `busy_notice` để nhãn của thanh thông báo cũng có chữ
    env.qtbot.waitUntil(lambda: BUSY in visible_texts(dlg), timeout=2_000)
    for label in dlg.findChildren(QLabel):
        if not label.text():
            continue  # nhãn chỉ có icon
        assert label.textFormat() == Qt.TextFormat.PlainText, (label.objectName(), label.text())
    files = rows(files_view(dlg))
    assert files == [texts.file_label(evil_file.name), texts.file_label(hidden.name)]
    for text in files + rows(devices_view(dlg)):
        assert "\u202e" not in text and "\u200b" not in text, repr(text)
    assert "<b>May</b>" in rows(devices_view(dlg))[0], "thẻ HTML hiện nguyên chữ"


# =================================================================================================
# D3g: không I/O trước khi bấm Gửi
# =================================================================================================


def test_dialog_never_touches_the_paths(env, monkeypatch):
    import pathlib

    marker = "zcl-khong-io-7Q"
    touched: list[str] = []

    def spy(name, real):
        def wrapper(*args, **kwargs):
            if any(marker in str(a) for a in args):
                touched.append(name)
            return real(*args, **kwargs)

        return wrapper

    for name in ("stat", "lstat", "scandir", "listdir", "access", "open"):
        monkeypatch.setattr(os, name, spy(f"os.{name}", getattr(os, name)))
    for name in ("exists", "isfile", "isdir", "getsize", "realpath"):
        monkeypatch.setattr(os.path, name, spy(f"os.path.{name}", getattr(os.path, name)))
    for name in ("stat", "exists", "is_file", "is_dir", "resolve", "open"):
        monkeypatch.setattr(pathlib.Path, name, spy(f"Path.{name}", getattr(pathlib.Path, name)))
    unc = (
        Path("\\\\" + marker + "-may-khong-co\\share\\a.txt") if os.name == "nt" else Path(f"/{marker}/a.txt")
    )
    t0 = time.monotonic()
    dlg = armed_with(env, paths=(unc, P(marker, "b.txt")))
    assert time.monotonic() - t0 < 2.0
    files_view(dlg).setFocus()
    env.qtbot.wait(100)
    assert touched == [], f"hộp chạm đường dẫn trước khi bấm Gửi: {touched}"


# =================================================================================================
# GuiApp không node (D7, nối signal, Thoát)
# =================================================================================================


@pytest.fixture
def gapp(qapp, qtbot, config_dir, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QSystemTrayIcon

    from teleport.ui import tray
    from teleport.ui.app import GuiApp

    monkeypatch.setattr(mod(), "_arm_ms", 0)
    monkeypatch.setattr(tray, "is_available", lambda: True)
    monkeypatch.setattr(QSystemTrayIcon, "show", lambda self: None)
    monkeypatch.setattr(QSystemTrayIcon, "showMessage", lambda self, *a, **k: None)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a, **k: 0)
    monkeypatch.setattr(QDialog, "exec", lambda self, *a, **k: 0)
    controller = GuiApp(qapp, config_dir("A", identity=False))
    sends: list = []
    monkeypatch.setattr(controller.transfers, "request_send", lambda d, p: sends.append((d, tuple(p))))
    controller.test_sends = sends
    yield controller
    with contextlib.suppress(Exception):
        controller.shutdown()
    with contextlib.suppress(Exception):
        controller.window.hide()
        controller.window.deleteLater()
    qapp.processEvents()


def test_gapp_has_send_picker_controller(gapp):
    assert isinstance(gapp.send_picker, mod().SendPickerController)


def test_send_requested_before_ready_is_held_and_window_shown(gapp, qtbot):
    gapp._on_send_requested((A, B))
    qtbot.waitUntil(gapp.window.isVisible, timeout=2_000)
    assert gapp.send_picker.dialog is None
    assert gapp.send_picker.pending == (A, B)


def test_send_requested_when_ready_opens_picker_and_send_goes_through_request_send(gapp, qtbot):
    gapp.window.devices.reset([dview(PEER_ID, "PC-Nha")])
    gapp.send_picker.set_ready(True)
    gapp._on_send_requested((A, B))
    dlg = gapp.send_picker.dialog
    assert dlg is not None
    assert gapp.test_sends == [], "không bao giờ tự gửi"
    activate(qtbot, dlg)
    select(dlg, "PC-Nha")
    qtbot.waitUntil(lambda: send_button(dlg).isEnabled(), timeout=2_000)
    QTest.mouseClick(send_button(dlg), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(gapp.test_sends), timeout=2_000)
    qtbot.wait(100)
    assert gapp.test_sends == [(PEER_ID, (A, B))], "đúng một lần, đúng máy, đúng thứ tự"
    assert gapp.window.conversations.model.devices()[0].device_id == PEER_ID
    assert gapp.window.isVisible()


@pytest.mark.parametrize("others", [True, False], ids=["other-device-left", "no-device-left"])
def test_picker_send_to_device_gone_shows_not_paired_and_does_not_send(gapp, qtbot, tmp_path, others):
    """D7: máy vừa hủy ghép đôi lúc bấm Gửi → câu “Chưa ghép đôi…” hiện (thấy được) ở cửa sổ chính, không
    gửi; kể cả khi không còn máy nào đã ghép đôi."""
    gapp.window.show_running("May-A", tmp_path / "nhan", None)
    gapp.window.devices.reset([dview(OTHER_ID, "Laptop")] if others else [])
    gapp.send_picker.send_confirmed.emit(PEER_ID, (A,))
    qtbot.wait(100)
    assert gapp.test_sends == []
    qtbot.waitUntil(lambda: any(NOT_PAIRED_HEAD in t for t in visible_texts(gapp.window)), timeout=2_000)


def test_send_requested_in_error_state_restarts_node(gapp, qtbot, monkeypatch):
    from teleport.ui.main_window import ViewState

    calls: list[int] = []
    monkeypatch.setattr(gapp, "start_node", lambda: calls.append(1))
    gapp.window.show_error("other", "")
    assert gapp.window.view_state is ViewState.ERROR
    gapp._on_send_requested((A,))
    assert calls == [1]
    assert gapp.send_picker.dialog is None


def test_discover_from_empty_picker_opens_discover_page(gapp, qtbot, tmp_path):
    from teleport.ui.main_window import Page

    gapp.window.show_running("May-A", tmp_path / "nhan", None)
    gapp.window.devices.reset([])
    gapp.send_picker.set_ready(True)
    gapp._on_send_requested((A,))
    dlg = gapp.send_picker.dialog
    assert dlg is not None
    QTest.mouseClick(discover_button(dlg), Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: gapp.window.current_page is Page.DISCOVER, timeout=2_000)
    assert gapp.test_sends == []


def test_shutdown_closes_picker(gapp, qtbot):
    gapp.window.devices.reset([dview(PEER_ID, "PC-Nha")])
    gapp.send_picker.set_ready(True)
    gapp._on_send_requested((A,))
    dlg = gapp.send_picker.dialog
    assert dlg is not None
    gapp.shutdown()
    qtbot.waitUntil(lambda: is_closed(gapp.send_picker, dlg), timeout=2_000)
    assert gapp.test_sends == []


def test_quit_closes_picker_and_ignores_later_requests(gapp, qtbot):
    gapp.window.devices.reset([dview(PEER_ID, "PC-Nha")])
    gapp.send_picker.set_ready(True)
    gapp._on_send_requested((A,))
    dlg = gapp.send_picker.dialog
    assert dlg is not None
    gapp.request_quit(confirm=False)
    qtbot.waitUntil(lambda: is_closed(gapp.send_picker, dlg), timeout=2_000)
    gapp._on_send_requested((B,))
    gapp.open_send_picker((B,))
    assert gapp.send_picker.dialog is None and gapp.send_picker.pending is None
    gapp.send_picker.send_confirmed.emit(PEER_ID, (B,))
    assert gapp.test_sends == []


# =================================================================================================
# main() với node thật (tiêu chí 1, 6)
# =================================================================================================


@pytest.fixture
def request_send_spy(monkeypatch):
    from teleport.ui import transfers

    calls: list = []
    monkeypatch.setattr(
        transfers.TransfersController, "request_send", lambda self, d, p: calls.append((d, p))
    )
    return calls


def find_picker(run):
    if not run.windows:
        return None
    for dlg in run.window.findChildren(QDialog, "sendPickerDialog"):
        if dlg.isVisible():
            return dlg
    return None


def app_of(run):
    app = run.tray.parent()
    assert app is not None and hasattr(app, "send_picker"), "GuiApp là cha của Tray"
    return app


@integration
@pytest.mark.slow
@pytest.mark.parametrize("background", [False, True], ids=["normal", "background"])
def test_send_flag_on_first_launch_opens_picker_after_node_runs(
    gui, gui_wait, config_dir, request_send_spy, monkeypatch, background
):
    """Tiêu chí 1: `--send A B` khi chưa có bản nào chạy → node chạy xong thì hộp mở đúng A, B theo thứ
    tự; không có `request_send` nào. `--send` luôn hiện cửa sổ, kể cả khi có `--background`."""
    from teleport.ui import texts
    from teleport.ui.main_window import ViewState

    monkeypatch.setattr(mod(), "_arm_ms", 0)
    d = config_dir("A")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: find_picker(run) is not None, 20, "hộp gửi mở")
        dlg = find_picker(run)
        seen["state"] = run.window.view_state
        seen["running"] = run.host.running
        seen["files"] = rows(files_view(dlg))
        seen["window"] = run.window.isVisible()
        seen["sends"] = list(request_send_spy)
        QTest.mouseClick(cancel_button(dlg), Qt.MouseButton.LeftButton)
        yield gui_wait(lambda: find_picker(run) is None, 5, "hộp đóng")
        run.tray.actions()[3].trigger()

    argv = ["--config-dir", str(d), "--send", str(A), str(B)]
    if background:
        argv.append("--background")
    run = gui(argv, script)
    assert run.code == 0
    assert seen["state"] is ViewState.RUNNING and seen["running"]
    assert seen["files"] == [texts.file_label(A.name), texts.file_label(B.name)]
    assert seen["window"], "--send luôn hiện cửa sổ"
    assert seen["sends"] == [] and request_send_spy == []


@integration
@pytest.mark.slow
def test_first_run_screen_holds_request_until_started(
    gui, gui_wait, config_dir, request_send_spy, monkeypatch
):
    """Tiêu chí 6, Q5: màn lần chạy đầu giữ yêu cầu; bấm “Bắt đầu”, node chạy xong thì hộp mở."""
    from teleport.ui.main_window import ViewState

    monkeypatch.setattr(mod(), "_arm_ms", 0)
    d = config_dir("A", identity=False)
    seen: dict = {}

    def script(run):
        yield gui_wait(
            lambda: run.windows and run.window.view_state is ViewState.WELCOME, 15, "màn lần chạy đầu"
        )
        for _ in range(15):
            yield
        seen["early"] = find_picker(run)
        seen["pending"] = app_of(run).send_picker.pending
        child(run.window, QAbstractButton, "startButton").click()
        yield gui_wait(lambda: find_picker(run) is not None, 30, "hộp gửi mở sau khi node chạy")
        seen["state"] = run.window.view_state
        QTest.mouseClick(cancel_button(find_picker(run)), Qt.MouseButton.LeftButton)
        yield gui_wait(lambda: find_picker(run) is None, 5, "hộp đóng")
        run.tray.actions()[3].trigger()

    run = gui(["--config-dir", str(d), "--send", str(A)], script)
    assert run.code == 0
    assert seen["early"] is None, "chưa có node: không mở hộp"
    assert seen["pending"] == (A,)
    assert seen["state"] is ViewState.RUNNING
    assert request_send_spy == []


@integration
@pytest.mark.slow
def test_start_failed_discards_pending_request(gui, gui_wait, config_dir, fast_lock, monkeypatch):
    """Tiêu chí 6: `start_failed` bỏ yêu cầu đang chờ; chạy lại thành công cũng không mở hộp."""
    from teleport.lock import NodeLock
    from teleport.ui.main_window import ViewState

    monkeypatch.setattr(mod(), "_arm_ms", 0)
    d = config_dir("A")
    holder = NodeLock(d, "node.lock")
    holder.acquire()
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: run.windows and run.window.view_state is ViewState.ERROR, 15, "màn lỗi")
        seen["error_picker"] = find_picker(run)
        seen["pending"] = app_of(run).send_picker.pending
        holder.release()
        run.window.retry_requested.emit()
        yield gui_wait(
            lambda: len(run.hosts) >= 2 and run.host.running and run.window.view_state is ViewState.RUNNING,
            20,
            "host mới chạy",
        )
        for _ in range(25):
            yield
        seen["after"] = find_picker(run)
        run.tray.actions()[3].trigger()

    try:
        run = gui(["--config-dir", str(d), "--send", str(A)], script)
    finally:
        with contextlib.suppress(Exception):
            holder.release()
    assert run.code == 0
    assert seen["error_picker"] is None
    assert seen["pending"] is None, "start_failed bỏ yêu cầu chờ"
    assert seen["after"] is None
