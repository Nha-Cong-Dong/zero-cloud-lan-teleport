"""Radar của GUI (spec-hackathon mục 2 PR1 “Xong khi” 1–9, mục 9.1, 9.5, 9.9, 9.10, mục 10).

Test viết theo spec, không theo code. Câu chữ của mvp mục 7.1 / mục 8 được đọc lại từ `docs/mvp.md`
(không chép tay), để tài liệu và giao diện không lệch nhau. Ngưỡng N1 cũng đọc từ `docs/mvp.md`.

Hai lớp test:

- Widget radar đứng riêng (`teleport.ui.radar`): nạp ảnh chụp `devices()` và `NodeEvent` giả qua ba
  hàm `load`, `feed`, `reset` ở đầu file (spec không quy định tên phương thức nạp dữ liệu; chỉ quy
  định model theo `device_id`, ảnh chụp lúc `started`, cập nhật theo `NodeEvent`, host mới thì xóa).
  Không mở socket.
- Đường đi thật qua `teleport.ui.app.main` (fixture `gui`) hoặc `GuiApp` (mDNS): ảnh chụp lúc
  `started`, nối `node_event`, “Thử lại”, màn “Bắt đầu” (mục 10 A1), log ra file (Q7, mục 9.10).
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import logging
import logging.handlers
import os
import re
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

integration = pytest.mark.integration
slow = pytest.mark.slow

LOCAL = "127.0.0.1"
_MVP = Path(__file__).resolve().parents[2] / "docs" / "mvp.md"

# mvp mục 7.1 (spec PR1 “Câu”): nhãn trạng thái ghép đôi của một hàng.
PAIRED_LABEL = "(đã ghép)"
UNPAIRED_LABEL = "(chưa ghép)"
IP_BUTTON = "Nhập IP thủ công"
UNPAIR_ACTION = "Hủy ghép đôi…"  # spec D11
CONNECTED_WORD = "đã kết nối"  # spec PR1: câu mới, trạng thái “đã kết nối”
START_BUTTON = "Bắt đầu"  # spec mục 10 A1
# Spec PR-2 radar-click “Câu chữ” (UI-14) và “Nút theo hàng đang chọn” (UI-06).
RADAR_HINT = "Bấm vào một thiết bị để ghép đôi hoặc gửi file, hoặc kéo file thả vào."
LIST_DESCRIPTION = (
    "Tab hoặc phím mũi tên để chọn thiết bị, Enter để ghép đôi hoặc gửi file, phím Menu để mở các thao tác."
)
SEND_BUTTON = "Gửi file…"
PAIR_BUTTON = "Ghép đôi…"


# =================================================================================================
# Đọc docs/mvp.md
# =================================================================================================


def _mvp_text() -> str:
    return _MVP.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start : nxt if nxt != -1 else len(text)]


def mvp_message(situation_prefix: str) -> str:
    """Câu thông báo (cột 2, câu đầu trong “…”) của hàng bảng mvp mục 8 có cột 1 bắt đầu bằng
    `situation_prefix`; bỏ dấu `*` nhấn mạnh. Không thấy → AssertionError (tài liệu đổi thì test phải
    được cập nhật, không lặng lẽ so với câu cũ)."""
    table = _section(_mvp_text(), "## 8.")
    rows = [line for line in table.splitlines() if line.startswith("| " + situation_prefix)]
    assert len(rows) == 1, f"mvp mục 8: cần đúng một hàng bắt đầu bằng {situation_prefix!r}, thấy {len(rows)}"
    cells = rows[0].split(" | ")
    m = re.search(r"“(.*?)”", cells[1])
    assert m, f"mvp mục 8: hàng {situation_prefix!r} không có câu trong “…”"
    return m.group(1)


def fill(template: str, **values: str) -> str:
    """Thay `*PC-Nha*` / `*x*` … bằng giá trị thật, bỏ các dấu `*` còn lại."""
    for key, value in values.items():
        template = template.replace(f"*{key}*", value)
    return template.replace("*", "")


def n1_limits() -> tuple[float, float]:
    """(xuất hiện < a giây, thoát app thì biến mất < b giây) của hàng `| N1 |` trong docs/mvp.md."""
    m = re.search(
        r"^\| N1 \|[^\n]*?xuất hiện < (\d+) giây; thoát app thì biến mất < (\d+) giây",
        _mvp_text(),
        re.MULTILINE,
    )
    assert m, "không tìm thấy ngưỡng N1 trong hàng | N1 | của docs/mvp.md"
    return float(m.group(1)), float(m.group(2))


def f1_name_notice() -> str:
    """Câu đầu của dòng cảnh báo tên trong F1 (“Tên này hiện với mọi máy …”)."""
    f1 = _section(_mvp_text(), "### F1.")
    m = re.search(r"“(Tên này hiện[^”]*?\.)", f1)
    assert m, "mvp F1: không thấy câu “Tên này hiện …”"
    return m.group(1)


# Hàng mvp mục 8 cho mỗi ConnectResult ≠ OK (spec PR1 “Câu” và tiêu chí 4).
CONNECT_RESULT_ROWS = {
    "unreachable": "Thấy nhưng không kết nối được",
    "rejected": "Máy kia từ chối máy này",
    "identity_mismatch": "Danh tính thiết bị thay đổi",
    "version_mismatch": "Khác phiên bản giao thức",
}
IP_ERROR_ROW = "CLI: `--address` của `send` sai"
EMPTY_ROW = "Không thấy thiết bị nào"


# =================================================================================================
# Truy cập radar (spec chỉ quy định module, signal và model theo `device_id`)
# =================================================================================================

RADAR_SIGNALS = ("pair_requested", "manual_address_entered", "send_files_requested", "unpair_requested")


def radar_class():
    """Lớp widget radar của `teleport.ui.radar`: lớp QWidget có đủ bốn signal của spec."""
    from PySide6.QtWidgets import QWidget

    from teleport.ui import radar as mod

    found = [
        cls
        for _, cls in inspect.getmembers(mod, inspect.isclass)
        if cls.__module__ == mod.__name__
        and issubclass(cls, QWidget)
        and all(hasattr(cls, s) for s in RADAR_SIGNALS)
    ]
    assert len(found) == 1, f"teleport.ui.radar cần đúng một widget có {RADAR_SIGNALS}, thấy {found}"
    return found[0]


def make_radar():
    return radar_class()()


def load(radar, views) -> None:
    """Nạp ảnh chụp `Node.devices()` (lúc `started`)."""
    radar.model.reset(tuple(views))


def feed(radar, event) -> None:
    """Một `NodeEvent`."""
    radar.model.apply(event)


def reset(radar) -> None:
    """Host mới (“Thử lại”): xóa model."""
    radar.model.clear()


def find_radar(root):
    found = root.findChildren(radar_class())
    assert len(found) == 1, f"cần đúng một radar trong {root!r}, thấy {len(found)}"
    return found[0]


def device_view_widget(radar):
    from PySide6.QtWidgets import QAbstractItemView

    views = [v for v in radar.findChildren(QAbstractItemView) if v.model() is not None]
    assert len(views) == 1, f"radar cần đúng một danh sách thiết bị, thấy {len(views)}"
    return views[0]


_TEXT_ROLES: tuple = ()


def _roles():
    global _TEXT_ROLES
    if not _TEXT_ROLES:
        from PySide6.QtCore import Qt

        r = Qt.ItemDataRole
        base = [r.DisplayRole, r.ToolTipRole, r.StatusTipRole, r.WhatsThisRole, r.AccessibleTextRole]
        base.append(r.AccessibleDescriptionRole)
        _TEXT_ROLES = tuple(base) + tuple(int(r.UserRole) + i for i in range(32))
    return _TEXT_ROLES


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def row_texts(radar) -> list[str]:
    """Chữ của từng hàng (mọi vai trò dữ liệu kiểu chuỗi, gộp)."""
    view = device_view_widget(radar)
    model = view.model()
    out = []
    for r in range(model.rowCount()):
        idx = model.index(r, 0)
        parts = []
        for role in _roles():
            data = model.data(idx, role)
            if isinstance(data, str) and data:
                parts.append(data)
        out.append(_norm(" ".join(parts)))
    return out


def accessible_row_text(radar, row: int) -> str:
    from PySide6.QtCore import Qt

    model = device_view_widget(radar).model()
    idx = model.index(row, 0)
    acc = model.data(idx, Qt.ItemDataRole.AccessibleTextRole)
    return _norm(
        acc if isinstance(acc, str) and acc else str(model.data(idx, Qt.ItemDataRole.DisplayRole) or "")
    )


def rows_with(radar, shown_name: str) -> list[int]:
    return [i for i, t in enumerate(row_texts(radar)) if shown_name in t]


def the_row(radar, shown_name: str) -> int:
    rows = rows_with(radar, shown_name)
    assert len(rows) == 1, f"cần đúng một hàng cho {shown_name!r}, thấy {rows}: {row_texts(radar)}"
    return rows[0]


def radar_text(radar, all_text) -> str:
    """Mọi chữ người dùng thấy trên radar: widget đang hiện + chữ các hàng."""
    return _norm("\n".join([all_text(radar), *row_texts(radar)]))


def select_row(radar, row: int) -> None:
    view = device_view_widget(radar)
    view.setCurrentIndex(view.model().index(row, 0))


def device_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:32]


def view(
    label: str,
    *,
    name: str | None = None,
    paired: bool = False,
    online: bool = True,
    connected: bool = False,
    port: int = 47001,
):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id(label),
        name=label if name is None else name,
        os="windows" if online else None,
        paired=paired,
        online=online,
        connected=connected,
        addresses=("192.0.2.7",) if online else (),
        port=port if online else None,
    )


def shown(name: str) -> str:
    from teleport.core.display import display_name

    return display_name(name)


class Spy:
    """Ghi lại mọi lần phát bốn signal của radar."""

    def __init__(self, radar) -> None:
        self.calls: dict[str, list[tuple]] = {s: [] for s in RADAR_SIGNALS}
        for s in RADAR_SIGNALS:
            getattr(radar, s).connect(lambda *a, _s=s: self.calls[_s].append(a))


# =================================================================================================
# Fixture
# =================================================================================================


@pytest.fixture
def radar(qtbot):
    w = make_radar()
    qtbot.addWidget(w)
    w.resize(480, 400)
    w.show()
    return w


@pytest.fixture
def menus(monkeypatch):
    """Bắt menu chuột phải: `QMenu.exec`/`popup` không chặn. `open(radar, row, pick)` mở menu của hàng
    bằng phím Menu (nền offscreen không tự đổi phím Menu thành sự kiện menu như Windows, nên khi
    `keyClick` không mở được thì gửi đúng sự kiện hệ điều hành gửi: `QContextMenuEvent(Keyboard)` tới
    widget đang có tiêu điểm). `pick(actions)` chọn một mục (như người dùng bấm) hoặc None. Trả danh
    sách `(chữ, bật?)` của các mục."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMenu

    state: dict = {"opened": None, "pick": None, "picked": False}

    def visible_actions(actions):
        return [a for a in actions if not a.isSeparator() and a.isVisible()]

    def choose(actions):
        pick = state["pick"]
        chosen = pick(visible_actions(actions)) if pick else None
        state["picked"] = True
        if chosen is not None:
            chosen.trigger()
        return chosen

    def fake_exec(first=None, *args, **kwargs):
        actions = first.actions() if isinstance(first, QMenu) else list(first or [])
        state["opened"] = actions
        return choose(actions)

    def fake_popup(self, *args, **kwargs):
        state["opened"] = self.actions()

    monkeypatch.setattr(QMenu, "exec", fake_exec)
    if hasattr(QMenu, "exec_"):
        monkeypatch.setattr(QMenu, "exec_", fake_exec)
    monkeypatch.setattr(QMenu, "popup", fake_popup)

    class Menus:
        def open(self, radar, row: int, pick=None):
            state.update(opened=None, pick=pick, picked=False)
            lst = device_view_widget(radar)
            lst.setFocus(Qt.FocusReason.OtherFocusReason)
            select_row(radar, row)
            QApplication.processEvents()
            QTest.keyClick(QApplication.focusWidget() or lst, Qt.Key.Key_Menu)
            QApplication.processEvents()
            if state["opened"] is None:
                target = QApplication.focusWidget() or lst
                pos = lst.visualRect(lst.model().index(row, 0)).center()
                if target is not lst:
                    pos = target.mapFrom(lst.viewport(), pos)
                event = QContextMenuEvent(QContextMenuEvent.Reason.Keyboard, pos, target.mapToGlobal(pos))
                QApplication.sendEvent(target, event)
                QApplication.processEvents()
            actions = state["opened"]
            assert actions is not None, "phím Menu trên một hàng không mở menu"
            if not state["picked"]:
                choose(actions)
            QApplication.processEvents()
            return [(a.text().replace("&", ""), a.isEnabled()) for a in visible_actions(actions)]

    return Menus()


# =================================================================================================
# Tiêu chí 9 (phần radar): API của module
# =================================================================================================


def test_radar_module_exposes_widget_with_the_four_spec_signals():
    cls = radar_class()
    for s in RADAR_SIGNALS:
        assert hasattr(cls, s), s


def test_radar_is_listed_in_architecture_ui_row():
    """Spec PR1 “File”: architecture mục 3 dòng `ui` có `radar`."""
    text = (Path(__file__).resolve().parents[2] / "docs" / "architecture.md").read_text(encoding="utf-8")
    sec = _section(text, "## 3.")
    assert "radar" in sec, "architecture mục 3 chưa nhắc tới ui/radar.py"


# =================================================================================================
# Tiêu chí 1: hàng đúng trạng thái, tên độc qua display_name
# =================================================================================================


def test_rows_show_paired_unpaired_online_and_connected_states(radar):
    a = view("May-A", paired=True, online=True, connected=True)
    b = view("May-B", paired=True, online=False)
    c = view("May-C", paired=False, online=True)
    load(radar, [a, b, c])
    texts = row_texts(radar)
    assert len(texts) == 3, texts
    ta, tb, tc = (texts[the_row(radar, n)] for n in ("May-A", "May-B", "May-C"))
    assert PAIRED_LABEL in ta and PAIRED_LABEL in tb
    assert UNPAIRED_LABEL in tc and PAIRED_LABEL not in tc
    assert CONNECTED_WORD in ta.lower()
    assert CONNECTED_WORD not in tb.lower() and CONNECTED_WORD not in tc.lower()
    # Trạng thái nói bằng chữ (mvp N12, F2): máy đã ghép đang thấy và không thấy phải khác nhau.
    b_online = view("May-B", paired=True, online=True)
    feed(radar, _ev("DeviceOnline", b_online))
    tb_online = row_texts(radar)[the_row(radar, "May-B")]
    assert tb_online.replace("May-B", "") != tb.replace("May-B", ""), (tb, tb_online)


def _ev(kind: str, v, **kw):
    from teleport.app import node as app_node

    cls = getattr(app_node, kind)
    if kind == "DeviceConnected":
        kw.setdefault("outgoing", True)
    return cls(v, **kw)


def test_device_online_event_adds_unpaired_row_and_offline_removes_it(radar):
    load(radar, [])
    x = view("May-X")
    feed(radar, _ev("DeviceOnline", x))
    assert rows_with(radar, "May-X")
    assert UNPAIRED_LABEL in row_texts(radar)[the_row(radar, "May-X")]
    feed(radar, _ev("DeviceOffline", view("May-X", online=False)))
    assert rows_with(radar, "May-X") == [], "máy chưa ghép đã rời mạng thì không còn trên radar"


def test_paired_device_going_offline_stays_listed(radar):
    p = view("May-P", paired=True, online=True, connected=True)
    load(radar, [p])
    feed(radar, _ev("DeviceDisconnected", view("May-P", paired=True, online=True)))
    assert CONNECTED_WORD not in row_texts(radar)[the_row(radar, "May-P")].lower()
    feed(radar, _ev("DeviceOffline", view("May-P", paired=True, online=False)))
    assert PAIRED_LABEL in row_texts(radar)[the_row(radar, "May-P")]


def test_repeated_events_keep_one_row_per_device_id(radar):
    load(radar, [view("May-R")])
    for _ in range(3):
        feed(radar, _ev("DeviceOnline", view("May-R")))
    # Đổi tên (mDNS gửi tên mới) vẫn là cùng thiết bị, cùng một hàng.
    feed(radar, _ev("DeviceOnline", view("May-R", name="May-R-moi")))
    assert len(row_texts(radar)) == 1, row_texts(radar)
    assert rows_with(radar, "May-R-moi")


def test_connected_event_marks_row_connected(radar):
    load(radar, [view("May-K", paired=True)])
    feed(radar, _ev("DeviceConnected", view("May-K", paired=True, connected=True)))
    assert CONNECTED_WORD in row_texts(radar)[the_row(radar, "May-K")].lower()


@pytest.mark.parametrize(
    "toxic",
    [
        "PC-A" + chr(0x3000) * 12 + "382 104",  # khoảng trắng giả đẩy “mã” ra xa
        "PC-B" + "\u2800" * 10 + "Laptop-Minh",  # chữ nổi trống
        "N" * 64,  # dài hơn DISPLAY_NAME_WIDTH
        "\u202eMay-Nguoc",  # RLO: display_name thay bằng `·`, tên thô không được hiện
    ],
)
def test_toxic_peer_names_are_shown_through_display_name(radar, toxic):
    from teleport.core.display import display_name

    load(radar, [view("doc", name=toxic)])
    texts = row_texts(radar)
    assert len(texts) == 1
    expected = _norm(display_name(toxic))
    assert expected in texts[0], (expected, texts[0])
    if display_name(toxic) != toxic:
        assert toxic not in texts[0], "tên thô (chưa qua display_name) không được hiện"


@pytest.mark.parametrize(
    "html_name",
    [
        "<b>X</b>",
        "<font size=7>PC</font>",
        '<img src="file:///C:/x.png">',
        '<a href="https://evil.example/">PC-Nha</a>',
        "<qt>382 104</qt>",
    ],
)
@pytest.mark.parametrize("paired", [False, True])
def test_html_peer_name_is_never_rendered_as_rich_text(radar, html_name, paired):
    """Tên máy kia là dữ liệu không tin cậy (protocol mục 3.3): chuỗi HTML hợp lệ theo
    `validate_device_name` không được thành rich text ở bất kỳ chữ nào của hàng (Qt tự hiểu chuỗi là
    rich text khi `mightBeRichText`, ví dụ tooltip). Mỗi vai trò: không có, hoặc chữ thuần, hoặc đã
    escape (không còn thẻ thô)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import Qt as GuiQt

    from teleport.core.links import ConnectResult
    from teleport.core.messages import validate_device_name

    validate_device_name(html_name)  # tiền đề: tên này đi qua được kiểm tra của lõi
    v = view("html", name=html_name, paired=paired)
    load(radar, [v])
    if paired:  # thêm dòng lỗi có chứa tên máy
        feed(radar, _ev("ConnectFailed", v, result=ConnectResult.REJECTED))
    from PySide6.QtWidgets import QStyledItemDelegate

    lst = device_view_widget(radar)
    model = lst.model()
    idx = model.index(0, 0)
    roles = {"ToolTipRole": Qt.ItemDataRole.ToolTipRole}  # QToolTip hiện rich text khi mightBeRichText
    # DisplayRole: `QStyledItemDelegate` gốc luôn vẽ chữ thuần, nên chuỗi thô là đúng (escape thì người
    # dùng thấy “&lt;b&gt;”). Delegate tự viết (có thể vẽ HTML) thì DisplayRole cũng phải theo luật trên.
    if type(lst.itemDelegate()) is not QStyledItemDelegate:
        roles["DisplayRole"] = Qt.ItemDataRole.DisplayRole
    # AccessibleTextRole: trình đọc màn hình nhận chữ thuần, Qt không dựng rich text từ nó; escape thì
    # trình đọc đọc “&lt;”. Chỉ kiểm nó đọc đúng tên thật.
    acc = model.data(idx, Qt.ItemDataRole.AccessibleTextRole)
    assert isinstance(acc, str) and shown(html_name) in acc, acc
    for role_name, role in roles.items():
        data = model.data(idx, role)
        if data is None or data == "":
            continue
        assert isinstance(data, str), (role_name, data)
        if GuiQt.mightBeRichText(data):
            assert shown(html_name) not in data, f"{role_name}: tên HTML thô trong chuỗi rich text: {data!r}"


# =================================================================================================
# Tiêu chí 2: trống → câu mvp mục 8; nút IP luôn hiện
# =================================================================================================


def test_empty_radar_shows_no_device_message_and_ip_button(radar, ui_helpers):
    load(radar, [])
    expected = _norm(mvp_message(EMPTY_ROW))
    assert expected in radar_text(radar, ui_helpers.all_text)
    assert ui_helpers.buttons_with_text(radar, IP_BUTTON), "nút “Nhập IP thủ công” phải hiện khi trống"


def test_ip_button_stays_visible_when_devices_are_listed(radar, ui_helpers):
    load(radar, [view("May-A", paired=True), view("May-B")])
    assert ui_helpers.buttons_with_text(radar, IP_BUTTON), "nút “Nhập IP thủ công” luôn có (F2)"
    assert _norm(mvp_message(EMPTY_ROW)) not in radar_text(radar, ui_helpers.all_text), (
        "đã thấy thiết bị thì không còn câu “Chưa thấy thiết bị nào”"
    )


def test_ip_button_visible_before_any_snapshot(radar, ui_helpers):
    assert ui_helpers.buttons_with_text(radar, IP_BUTTON)


# =================================================================================================
# Tiêu chí 3: hộp nhập IP
# =================================================================================================


def _open_ip_dialog(radar, ui_helpers):
    """Bấm “Nhập IP thủ công” → một ô nhập đang hiện (hộp riêng hay khung ngay trong radar: spec không
    quy định, chỉ cấm `exec()`)."""
    from PySide6.QtWidgets import QApplication, QLineEdit

    [button] = ui_helpers.buttons_with_text(radar, IP_BUTTON)
    button.click()
    QApplication.processEvents()
    edits = [
        e
        for w in QApplication.topLevelWidgets()
        if w.isVisible()
        for e in w.findChildren(QLineEdit)
        if e.isVisibleTo(w)
    ]
    assert len(edits) == 1, f"bấm “{IP_BUTTON}” phải mở một hộp có một ô nhập, thấy {len(edits)} ô"
    return edits[0]


def _submit_ip(radar, ui_helpers, text: str):
    """Gõ `text` rồi Enter; trả ô nhập."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    edit = _open_ip_dialog(radar, ui_helpers)
    edit.setFocus()
    edit.setText(text)
    QTest.keyClick(edit, Qt.Key.Key_Return)
    QApplication.processEvents()
    return edit


def _visible_text(ui_helpers) -> str:
    from PySide6.QtWidgets import QApplication

    return _norm("\n".join(ui_helpers.all_text(w) for w in QApplication.topLevelWidgets() if w.isVisible()))


def _close_dialogs(radar) -> None:
    from PySide6.QtWidgets import QApplication, QDialog

    for w in QApplication.topLevelWidgets():
        if isinstance(w, QDialog) and w.isVisible():
            w.reject()
    QApplication.processEvents()


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("192.168.1.5", ("192.168.1.5", None)),
        ("127.0.0.1:5000", ("127.0.0.1", 5000)),
        ("  10.0.0.2  ", ("10.0.0.2", None)),
        ("10.0.0.2:65535", ("10.0.0.2", 65535)),
        ("10.0.0.2:1", ("10.0.0.2", 1)),
    ],
)
def test_valid_manual_address_emits_signal(radar, ui_helpers, typed, expected):
    from teleport.net.connection import DEFAULT_PORT

    spy = Spy(radar)
    edit = _submit_ip(radar, ui_helpers, typed)
    ip, port = expected
    assert spy.calls["manual_address_entered"] == [(ip, DEFAULT_PORT if port is None else port)]
    assert spy.calls["pair_requested"] == []
    assert not edit.isVisible(), "nhập đúng thì hộp đóng"
    _close_dialogs(radar)


@pytest.mark.parametrize(
    "typed",
    [
        "abc",
        "192.168.1.300",
        "192.168.01.5",
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "192.168.1.5:0",
        "192.168.1.5:65536",
        "192.168.1.5:",
        "pc-nha.local",
        "::1",
        "\uff11\uff19\uff12.168.1.5",  # chữ số toàn độ rộng
        "../../etc",
        "C:\\Windows",
    ],
)
def test_invalid_manual_address_shows_mvp_message_and_does_not_emit(radar, ui_helpers, typed):
    from teleport.app.devices import parse_manual_address

    spy = Spy(radar)
    _submit_ip(radar, ui_helpers, typed)
    text = _visible_text(ui_helpers)
    assert spy.calls["manual_address_entered"] == []
    expected = _norm(fill(mvp_message(IP_ERROR_ROW), x=typed.strip()))
    assert expected in text, f"thiếu câu mvp mục 8: {expected!r} trong {text!r}"
    with pytest.raises(ValueError) as e:
        parse_manual_address(typed)
    assert _norm(str(e.value)) not in text, "không hiện str(e) của parse_manual_address (mục 9.9)"
    _close_dialogs(radar)


def test_very_long_manual_address_is_rejected_without_emitting(radar, ui_helpers):
    spy = Spy(radar)
    _submit_ip(radar, ui_helpers, "1" * 10_000)
    assert spy.calls["manual_address_entered"] == []
    tail = _norm(fill(mvp_message(IP_ERROR_ROW), x="").strip())
    assert tail in _visible_text(ui_helpers)
    _close_dialogs(radar)


def test_ip_error_then_valid_retry_emits_once(radar, ui_helpers):
    from teleport.net.connection import DEFAULT_PORT

    spy = Spy(radar)
    _submit_ip(radar, ui_helpers, "abc")
    _close_dialogs(radar)
    _submit_ip(radar, ui_helpers, "192.168.1.9")
    assert spy.calls["manual_address_entered"] == [("192.168.1.9", DEFAULT_PORT)]
    _close_dialogs(radar)


def test_ip_dialog_is_not_modal_exec(radar, ui_helpers, monkeypatch):
    """Rủi ro 3: không `exec()` (hộp dùng `open()`/`show()`)."""
    from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox

    calls = []
    for cls in (QDialog, QInputDialog, QMessageBox):
        monkeypatch.setattr(cls, "exec", lambda self, *a, **k: calls.append(type(self).__name__) or 0)
    for name in ("getText", "getItem", "getInt"):
        monkeypatch.setattr(QInputDialog, name, lambda *a, **k: calls.append("static") or ("", False))
    [button] = ui_helpers.buttons_with_text(radar, IP_BUTTON)
    button.click()
    assert calls == [], f"hộp nhập IP mở bằng exec(): {calls}"
    _close_dialogs(radar)


# =================================================================================================
# Tiêu chí 4: mỗi ConnectResult ≠ OK → câu mvp mục 8 (mục 9.5: nhớ theo thiết bị)
# =================================================================================================


def test_every_failing_connect_result_has_an_mvp_row():
    from teleport.core.links import ConnectResult

    failing = {r.value for r in ConnectResult if r is not ConnectResult.OK}
    assert failing == set(CONNECT_RESULT_ROWS), "ConnectResult mới cần một câu mvp mục 8"
    for row in CONNECT_RESULT_ROWS.values():
        mvp_message(row)


@pytest.mark.parametrize("result", sorted(CONNECT_RESULT_ROWS))
def test_connect_failed_shows_mvp_message_on_the_device(radar, ui_helpers, result):
    from teleport.core.links import ConnectResult

    name = "PC-Nha"
    other = "May-Khac"
    load(radar, [view(name, paired=True), view(other, paired=True)])
    feed(radar, _ev("ConnectFailed", view(name, paired=True), result=ConnectResult(result)))
    select_row(radar, the_row(radar, name))
    expected = _norm(fill(mvp_message(CONNECT_RESULT_ROWS[result]), **{"PC-Nha": name}))
    text = radar_text(radar, ui_helpers.all_text)
    assert expected in text, f"{result}: thiếu {expected!r} trong {text!r}"
    # Lỗi gắn với đúng thiết bị: không có câu mang tên máy kia.
    wrong = _norm(fill(mvp_message(CONNECT_RESULT_ROWS[result]), **{"PC-Nha": other}))
    select_row(radar, the_row(radar, other))
    assert wrong not in radar_text(radar, ui_helpers.all_text)


def test_connect_failure_survives_snapshot_reload_and_clears_on_connected(radar, ui_helpers):
    """Mục 9.5: UI nhớ `ConnectFailed` gần nhất theo thiết bị, không xóa khi nạp lại `devices()` hay khi
    có `DeviceOnline`; xóa khi có `DeviceConnected`."""
    from teleport.core.links import ConnectResult

    name = "PC-Nha"
    p = view(name, paired=True)
    load(radar, [p])
    feed(radar, _ev("ConnectFailed", p, result=ConnectResult.REJECTED))
    expected = _norm(fill(mvp_message(CONNECT_RESULT_ROWS["rejected"]), **{"PC-Nha": name}))

    def text():
        select_row(radar, the_row(radar, name))
        return radar_text(radar, ui_helpers.all_text)

    assert expected in text()
    load(radar, [p])  # nạp lại devices() (sau PAIRED / unpair, mục 9.6)
    assert expected in text(), "nạp lại devices() không được xóa lỗi kết nối đã nhớ"
    feed(radar, _ev("DeviceOnline", p))
    feed(radar, _ev("DeviceDisconnected", p))
    assert expected in text()
    feed(radar, _ev("DeviceConnected", view(name, paired=True, connected=True)))
    assert expected not in text(), "DeviceConnected xóa lỗi đã nhớ"
    assert CONNECTED_WORD in row_texts(radar)[the_row(radar, name)].lower()


def test_latest_connect_failure_wins(radar, ui_helpers):
    from teleport.core.links import ConnectResult

    name = "PC-Nha"
    p = view(name, paired=True)
    load(radar, [p])
    feed(radar, _ev("ConnectFailed", p, result=ConnectResult.UNREACHABLE))
    feed(radar, _ev("ConnectFailed", p, result=ConnectResult.IDENTITY_MISMATCH))
    select_row(radar, the_row(radar, name))
    text = radar_text(radar, ui_helpers.all_text)
    assert _norm(fill(mvp_message(CONNECT_RESULT_ROWS["identity_mismatch"]), **{"PC-Nha": name})) in text
    assert _norm(fill(mvp_message(CONNECT_RESULT_ROWS["unreachable"]), **{"PC-Nha": name})) not in text


def test_reset_clears_rows_and_remembered_failures(radar, ui_helpers):
    """D5 / tiêu chí 8: host mới thì xóa model (kể cả lỗi đã nhớ của host cũ)."""
    from teleport.core.links import ConnectResult

    p = view("PC-Nha", paired=True)
    load(radar, [p, view("May-Z")])
    feed(radar, _ev("ConnectFailed", p, result=ConnectResult.REJECTED))
    reset(radar)
    assert row_texts(radar) == []
    load(radar, [p])
    select_row(radar, the_row(radar, "PC-Nha"))
    expected = _norm(fill(mvp_message(CONNECT_RESULT_ROWS["rejected"]), **{"PC-Nha": "PC-Nha"}))
    assert expected not in radar_text(radar, ui_helpers.all_text)


# =================================================================================================
# Property: chuỗi sự kiện lộn xộn → mỗi device_id đúng một hàng, theo view cuối cùng
# =================================================================================================


def test_model_follows_last_view_per_device_for_any_event_order(qapp):
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    labels = ["May-1", "May-2", "May-3"]
    kinds = ["DeviceOnline", "DeviceOffline", "DeviceConnected", "DeviceDisconnected", "snapshot"]

    step = st.tuples(st.sampled_from(kinds), st.sampled_from(labels), st.booleans())

    @settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(st.lists(step, max_size=25))
    def run(steps):
        w = make_radar()
        try:
            last: dict[str, object] = {}
            load(w, [])
            for kind, label, paired in steps:
                if kind == "DeviceOnline":
                    v = view(label, paired=paired)
                elif kind == "DeviceOffline":
                    v = view(label, paired=paired, online=False)
                elif kind == "DeviceConnected":
                    v = view(label, paired=True, connected=True)
                elif kind == "DeviceDisconnected":
                    v = view(label, paired=True)
                else:
                    snap = [x for x in last.values() if x.paired or x.online]
                    load(w, snap)
                    continue
                last[label] = v
                feed(w, _ev(kind, v))
            texts = row_texts(w)
            expected = {lbl: v for lbl, v in last.items() if v.paired or v.online}
            assert len(texts) == len(expected), (steps, texts)
            for lbl, v in expected.items():
                [t] = [t for t in texts if lbl in t]
                assert (PAIRED_LABEL in t) == v.paired, (steps, t)
                assert (UNPAIRED_LABEL in t) == (not v.paired), (steps, t)
                assert (CONNECTED_WORD in t.lower()) == v.connected, (steps, t)
        finally:
            w.deleteLater()

    run()


# =================================================================================================
# Ghép đôi / hủy ghép từ menu (mục 9.1, D11); bàn phím (tiêu chí 7)
# =================================================================================================


def test_menu_on_unpaired_online_device_requests_pairing_with_its_view(radar, menus):
    x = view("May-X", port=47123)
    load(radar, [x])
    spy = Spy(radar)
    row = the_row(radar, "May-X")
    items = menus.open(radar, row)
    enabled = [i for i, (_, on) in enumerate(items) if on]
    assert enabled, f"menu của máy chưa ghép không có mục nào bật: {items}"
    for i in enabled:
        menus.open(radar, row, pick=lambda acts, _i=i: acts[_i])
    got = spy.calls["pair_requested"]
    assert got, f"không mục nào trong {items} phát pair_requested"
    v = got[-1][0]
    assert (v.device_id, v.addresses, v.port) == (x.device_id, x.addresses, x.port)
    assert spy.calls["unpair_requested"] == []


def test_offline_paired_device_never_requests_pairing(radar, menus):
    """Mục 9.1: thiết bị offline có `addresses=()`, `port=None` → chỉ cho ghép khi online."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    load(radar, [view("May-Off", paired=True, online=False)])
    spy = Spy(radar)
    row = the_row(radar, "May-Off")
    items = menus.open(radar, row)
    for i, (text, on) in enumerate(items):
        if on and text != UNPAIR_ACTION:
            menus.open(radar, row, pick=lambda acts, _i=i: acts[_i])
    lst = device_view_widget(radar)
    select_row(radar, row)
    QTest.keyClick(lst, Qt.Key.Key_Return)
    QTest.mouseDClick(
        lst.viewport(), Qt.MouseButton.LeftButton, pos=lst.visualRect(lst.currentIndex()).center()
    )
    assert spy.calls["pair_requested"] == []


def test_paired_device_menu_has_unpair_that_emits_device_id(radar, menus):
    p = view("May-P", paired=True)
    load(radar, [p])
    spy = Spy(radar)
    row = the_row(radar, "May-P")
    items = menus.open(radar, row)
    assert (UNPAIR_ACTION, True) in items, f"menu máy đã ghép thiếu “{UNPAIR_ACTION}”: {items}"
    menus.open(
        radar, row, pick=lambda acts: next(a for a in acts if a.text().replace("&", "") == UNPAIR_ACTION)
    )
    assert spy.calls["unpair_requested"] == [(p.device_id,)]


def test_unpaired_device_menu_has_no_unpair(radar, menus):
    load(radar, [view("May-U")])
    items = menus.open(radar, the_row(radar, "May-U"))
    assert UNPAIR_ACTION not in [t for t, _ in items]


def test_tab_reaches_device_list_and_ip_button(radar, ui_helpers):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    load(radar, [view("May-1", paired=True), view("May-2"), view("May-3", paired=True, online=False)])
    radar.activateWindow()
    radar.setFocus(Qt.FocusReason.OtherFocusReason)
    lst = device_view_widget(radar)
    [ip] = ui_helpers.buttons_with_text(radar, IP_BUTTON)
    seen = set()
    rows_on_tab = set()
    for _ in range(12):
        QTest.keyClick(QApplication.focusWidget() or radar, Qt.Key.Key_Tab)
        focused = QApplication.focusWidget()
        seen.add(focused)
        if focused is lst and lst.currentIndex().isValid():
            rows_on_tab.add(lst.currentIndex().row())
    assert lst in seen, "Tab không tới danh sách thiết bị"
    assert ip in seen, "Tab không tới nút “Nhập IP thủ công”"
    assert rows_on_tab == {0, 1, 2}, f"Tab phải tới từng thiết bị (tiêu chí 7), tới được hàng {rows_on_tab}"


def test_arrow_keys_visit_every_device(radar):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    load(radar, [view("May-1", paired=True), view("May-2"), view("May-3", paired=True, online=False)])
    lst = device_view_widget(radar)
    lst.setFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(lst, Qt.Key.Key_Home)
    visited = {lst.currentIndex().row()}
    for _ in range(5):
        QTest.keyClick(lst, Qt.Key.Key_Down)
        visited.add(lst.currentIndex().row())
    assert visited == {0, 1, 2}


def test_accessible_names_of_list_rows_and_ip_button(radar, ui_helpers):
    load(radar, [view("May-1", paired=True, connected=True), view("May-2")])
    lst = device_view_widget(radar)
    assert lst.accessibleName(), "danh sách thiết bị cần accessibleName"
    [ip] = ui_helpers.buttons_with_text(radar, IP_BUTTON)
    assert ip.accessibleName() or ui_helpers.plain(ip.text())
    r1, r2 = the_row(radar, "May-1"), the_row(radar, "May-2")
    a1, a2 = accessible_row_text(radar, r1), accessible_row_text(radar, r2)
    assert "May-1" in a1 and PAIRED_LABEL in a1 and CONNECTED_WORD in a1.lower(), a1
    assert "May-2" in a2 and UNPAIRED_LABEL in a2, a2


def test_drop_hint_text_from_mvp_7_1_is_shown(radar, ui_helpers):
    """Câu gợi ý của radar (spec PR-2 radar-click “Câu chữ”, thay câu “Kéo file thả vào một thiết bị” của
    spec PR1)."""
    load(radar, [view("May-1", paired=True)])
    assert RADAR_HINT in radar_text(radar, ui_helpers.all_text)
    assert "Kéo file thả vào một thiết bị" not in radar_text(radar, ui_helpers.all_text), "câu gợi ý cũ"


# =================================================================================================
# Spec PR-2 radar-click: bấm vào thiết bị (COV-03, UX-01), nút theo hàng (UI-06), câu chữ (UI-14)
#
# Chuỗi sự kiện chuột của một lần bấm đúp thật trong Qt: press, release (`clicked`), press/dblclick
# (`doubleClicked`, `activated`), release (`clicked` lần nữa). Mọi test bấm đúp gửi đủ chuỗi đó.
# Kết quả dương được chờ bằng `waitUntil` (radar có thể hoãn xử lý bằng timer); sau đó `_settle` cho
# event loop chạy thêm một chút để bắt các lần phát lặp.
# =================================================================================================


class ClickSpy(Spy):
    """`Spy` + `pick_files_requested`."""

    def __init__(self, radar) -> None:
        super().__init__(radar)
        self.picks: list[str] = []
        radar.pick_files_requested.connect(lambda i: self.picks.append(i))

    @property
    def pairs(self) -> list:
        return [a[0] for a in self.calls["pair_requested"]]


def _settle(qtbot, ms: int = 30) -> None:
    from PySide6.QtWidgets import QApplication

    QApplication.processEvents()
    qtbot.wait(ms)


def _row_pos(lst, row: int):
    return lst.visualRect(lst.model().index(row, 0)).center()


def _empty_pos(lst):
    """Một điểm trong viewport không thuộc hàng nào (dưới hàng cuối)."""
    from PySide6.QtCore import QPoint

    pos = QPoint(10, lst.viewport().height() - 5)
    assert not lst.indexAt(pos).isValid(), "tiền đề: điểm này phải là chỗ trống của danh sách"
    return pos


def _send_mouse(lst, kind, pos, button, buttons, modifiers=None, timestamp=None) -> None:
    """Gửi thẳng một `QMouseEvent` tới viewport (không qua bộ sinh dblclick của QGuiApplication).
    Không cho `timestamp` thì sự kiện tự dựng có `timestamp()` = 0."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    vp = lst.viewport()
    mods = Qt.KeyboardModifier.NoModifier if modifiers is None else modifiers
    ev = QMouseEvent(kind, QPointF(pos), QPointF(vp.mapToGlobal(pos)), button, buttons, mods)
    if timestamp is not None:
        ev.setTimestamp(timestamp)
    QApplication.sendEvent(vp, ev)


DCLICK_WAYS = ("qtest_click_then_dclick", "manual_press_dblclick", "manual_dblclick_without_second_press")


def _double_click(lst, pos, way: str, modifiers=None) -> None:
    """Một lần bấm đúp chuột trái tại `pos`, theo một trong `DCLICK_WAYS`:

    - `qtest_click_then_dclick`: `QTest.mouseClick` rồi `QTest.mouseDClick` (như người thật).
    - `manual_press_dblclick`: press, release, press, dblclick, release (Qt 6 trên mọi nền).
    - `manual_dblclick_without_second_press`: press, release, dblclick, release (kiểu Qt 5 / vài nền).
    """
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    mods = Qt.KeyboardModifier.NoModifier if modifiers is None else modifiers
    if way == "qtest_click_then_dclick":
        QTest.mouseClick(lst.viewport(), left, mods, pos)
        QTest.mouseDClick(lst.viewport(), left, mods, pos)
    else:
        t = QEvent.Type
        seq = [(t.MouseButtonPress, left), (t.MouseButtonRelease, none)]
        if way == "manual_press_dblclick":
            seq.append((t.MouseButtonPress, left))
        seq += [(t.MouseButtonDblClick, left), (t.MouseButtonRelease, none)]
        for kind, buttons in seq:  # nút gây ra sự kiện luôn là nút trái (kể cả release)
            _send_mouse(lst, kind, pos, left, buttons, mods)
    QApplication.processEvents()


def _click(lst, pos, button=None, modifiers=None) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    QTest.mouseClick(
        lst.viewport(),
        Qt.MouseButton.LeftButton if button is None else button,
        Qt.KeyboardModifier.NoModifier if modifiers is None else modifiers,
        pos,
    )
    QApplication.processEvents()


def _selected_rows(lst) -> list[int]:
    return sorted(i.row() for i in lst.selectionModel().selectedIndexes())


def _action_button(radar):
    btn = getattr(radar, "device_action_button", None)
    assert btn is not None, "RadarView cần thuộc tính công khai `device_action_button` (spec UI-06)"
    assert btn.objectName() == "deviceActionButton"
    return btn


def _ip_button(radar, ui_helpers):
    [ip] = ui_helpers.buttons_with_text(radar, IP_BUTTON)
    return ip


@pytest.fixture
def dclick_interval(qapp):
    """Đổi `doubleClickInterval` của ứng dụng (để vượt khoảng bấm đúp mà không phải ngủ lâu); khôi phục
    khi test xong."""
    from PySide6.QtGui import QGuiApplication

    hints = QGuiApplication.styleHints()
    old = hints.mouseDoubleClickInterval()

    def set_interval(ms: int) -> int:
        hints.setMouseDoubleClickInterval(ms)
        return ms

    yield set_interval
    hints.setMouseDoubleClickInterval(old)


# ---- Bấm một lần ----------------------------------------------------------------------------------


def test_single_click_on_unpaired_online_device_requests_pairing_once_and_selects_it(radar, qtbot):
    """Spec Test 1."""
    x, other = view("May-X", port=47123), view("May-Y", paired=True)
    load(radar, [x, other])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    row = the_row(radar, "May-X")
    _click(lst, _row_pos(lst, row))
    qtbot.waitUntil(lambda: bool(spy.pairs), timeout=2000)
    _settle(qtbot)
    assert len(spy.pairs) == 1, spy.pairs
    v = spy.pairs[0]
    assert (v.device_id, v.addresses, v.port) == (x.device_id, x.addresses, x.port)
    assert spy.picks == []
    assert _selected_rows(lst) == [row], "bấm một lần phải chọn hàng"


def test_single_click_on_paired_device_only_selects_and_shows_send_button(radar, qtbot, dclick_interval):
    """Spec Test 3. Chờ quá `doubleClickInterval` (đặt ngắn 100 ms) để chắc radar không hoãn rồi phát."""
    interval = dclick_interval(100)
    p = view("May-P", paired=True)
    load(radar, [p, view("May-U")])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    row = the_row(radar, "May-P")
    _click(lst, _row_pos(lst, row))
    _settle(qtbot, interval + 80)
    assert spy.pairs == [] and spy.picks == []
    assert spy.calls["send_files_requested"] == [] and spy.calls["unpair_requested"] == []
    assert _selected_rows(lst) == [row]
    btn = _action_button(radar)
    assert not btn.isHidden(), "máy đã ghép đôi được chọn → hiện nút “Gửi file…”"
    assert ui_text(btn.text()) == SEND_BUTTON
    btn.click()
    _settle(qtbot)
    assert spy.picks == [p.device_id]
    assert spy.pairs == []


def ui_text(text: str) -> str:
    """Chữ trên nút bỏ dấu tắt `&`."""
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


def test_click_on_empty_space_emits_nothing(radar, qtbot):
    """Spec “Bấm vào chỗ trống của danh sách (không có hàng) không phát gì” (một lần và bấm đúp)."""
    load(radar, [view("May-X"), view("May-P", paired=True)])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    pos = _empty_pos(lst)
    _click(lst, pos)
    for way in DCLICK_WAYS:
        _double_click(lst, pos, way)
    _settle(qtbot)
    assert spy.pairs == [] and spy.picks == []


# ---- Bấm đúp --------------------------------------------------------------------------------------


@pytest.mark.parametrize("way", DCLICK_WAYS)
def test_double_click_on_unpaired_device_requests_pairing_exactly_once(radar, qtbot, way):
    """Spec Test 2: cả chuỗi bấm đúp chỉ phát đúng một `pair_requested` (lần thứ hai làm
    `PairingController` hiện câu BUSY), không `pick_files_requested`."""
    x = view("May-X")
    load(radar, [x, view("May-P", paired=True)])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    _double_click(lst, _row_pos(lst, the_row(radar, "May-X")), way)
    qtbot.waitUntil(lambda: bool(spy.pairs), timeout=2000)
    _settle(qtbot)
    assert [v.device_id for v in spy.pairs] == [x.device_id], f"{way}: {len(spy.pairs)} lần pair_requested"
    assert spy.picks == []


@pytest.mark.parametrize("online", [True, False])
@pytest.mark.parametrize("way", DCLICK_WAYS)
def test_double_click_on_paired_device_requests_file_pick_exactly_once(radar, qtbot, way, online):
    """Spec Test 4: máy đã ghép (online hay không) → đúng một `pick_files_requested`, không ghép đôi."""
    p = view("May-P", paired=True, online=online)
    load(radar, [p, view("May-U")])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    _double_click(lst, _row_pos(lst, the_row(radar, "May-P")), way)
    qtbot.waitUntil(lambda: bool(spy.picks), timeout=2000)
    _settle(qtbot)
    assert spy.picks == [p.device_id], f"{way}: {spy.picks}"
    assert spy.pairs == []


@pytest.mark.parametrize("way", DCLICK_WAYS)
def test_double_click_does_not_rely_on_qt_activated_for_the_mouse(radar, qtbot, way):
    """Spec: “Không dựa vào `activated` của Qt cho chuột (style khác có thể phát `activated` ngay khi
    bấm một lần → lặp)”. Dùng style có `SH_ItemView_ActivateItemOnSingleClick` = 1 (như vài theme
    Linux/KDE): bấm một lần vẫn một `pair_requested`, bấm đúp vẫn đúng một."""
    from PySide6.QtWidgets import QProxyStyle, QStyle

    class SingleClickActivates(QProxyStyle):
        def styleHint(self, hint, option=None, widget=None, return_data=None):
            if hint == QStyle.StyleHint.SH_ItemView_ActivateItemOnSingleClick:
                return 1
            return super().styleHint(hint, option, widget, return_data)

    x = view("May-X")
    load(radar, [x])
    lst = device_view_widget(radar)
    style = SingleClickActivates()
    style.setParent(radar)  # sống cùng radar
    lst.setStyle(style)
    spy = ClickSpy(radar)
    pos = _row_pos(lst, the_row(radar, "May-X"))
    _double_click(lst, pos, way)
    qtbot.waitUntil(lambda: bool(spy.pairs), timeout=2000)
    _settle(qtbot)
    assert len(spy.pairs) == 1, f"{way}: {len(spy.pairs)} lần pair_requested"
    assert spy.picks == []


def test_single_click_with_single_click_activation_style_emits_once(radar, qtbot):
    from PySide6.QtWidgets import QProxyStyle, QStyle

    class SingleClickActivates(QProxyStyle):
        def styleHint(self, hint, option=None, widget=None, return_data=None):
            if hint == QStyle.StyleHint.SH_ItemView_ActivateItemOnSingleClick:
                return 1
            return super().styleHint(hint, option, widget, return_data)

    load(radar, [view("May-X"), view("May-P", paired=True)])
    lst = device_view_widget(radar)
    style = SingleClickActivates()
    style.setParent(radar)
    lst.setStyle(style)
    spy = ClickSpy(radar)
    _click(lst, _row_pos(lst, the_row(radar, "May-X")))
    qtbot.waitUntil(lambda: bool(spy.pairs), timeout=2000)
    _click(lst, _row_pos(lst, the_row(radar, "May-P")))
    _settle(qtbot)
    assert len(spy.pairs) == 1, spy.pairs
    assert spy.picks == [], "bấm một lần vào máy đã ghép không mở hộp chọn file, kể cả khi style kích hoạt"


def test_click_again_after_double_click_interval_requests_pairing_again(radar, qtbot, dclick_interval):
    """Spec “Chặn lặp”: bấm lại sau `doubleClickInterval` thì phát như thường. Hai lần `QTest.mouseClick`
    là hai chuỗi press/release tách biệt: sau mỗi lần nhả, QTest tự đẩy `timestamp()` của sự kiện kế
    tiếp lên (để hai lần bấm không thành bấm đúp), nên theo dấu thời gian của sự kiện hai lần bấm đã
    cách nhau quá khoảng bấm đúp; test này vì thế xanh cả khi bỏ khoảng chờ. Khoảng chờ thật (khoảng
    bấm đúp đặt 40 ms, chờ 150 ms) chỉ để phủ cả cài đặt đo bằng đồng hồ đơn điệu. Bản kiểm bằng dấu
    thời gian tường minh: `test_clicks_by_event_timestamp_*`, `test_*_after_press_released_off_row`."""
    interval = dclick_interval(40)
    x = view("May-X")
    load(radar, [x])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    pos = _row_pos(lst, the_row(radar, "May-X"))
    _click(lst, pos)
    qtbot.waitUntil(lambda: len(spy.pairs) == 1, timeout=2000)
    _settle(qtbot, interval + 110)
    _click(lst, pos)
    qtbot.waitUntil(lambda: len(spy.pairs) == 2, timeout=2000)
    _settle(qtbot)
    assert [v.device_id for v in spy.pairs] == [x.device_id, x.device_id]


def _ts_events(lst, events) -> None:
    """Gửi chuỗi `(loại, pos, timestamp)` chuột trái tới viewport; loại: press / release / dblclick."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtWidgets import QApplication

    left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    kinds = {
        "press": (QEvent.Type.MouseButtonPress, left),
        "release": (QEvent.Type.MouseButtonRelease, none),
        "dblclick": (QEvent.Type.MouseButtonDblClick, left),
    }
    for name, pos, ts in events:
        kind, buttons = kinds[name]
        _send_mouse(lst, kind, pos, left, buttons, timestamp=ts)
    QApplication.processEvents()


@pytest.mark.parametrize(
    ("first", "gap", "expected"),
    [
        (10_000, "within", 1),  # cách ≤ khoảng bấm đúp: cùng một chuỗi
        (10_000, "beyond", 2),  # cách > khoảng bấm đúp: hai lần bấm
        (2**32 - 100, 600, 2),  # dấu thời gian lùi (quay vòng 32 bit): không coi là cùng chuỗi
    ],
    ids=["within_interval", "beyond_interval", "timestamp_wraps_backwards"],
)
def test_clicks_by_event_timestamp_on_unpaired_device(radar, qtbot, dclick_interval, first, gap, expected):
    """Soát vòng 2: chuỗi bấm được xét theo `QMouseEvent.timestamp()` (không ngủ). Hai lần bấm một lần
    (press/release, không dblclick) cùng một hàng máy chưa ghép, dấu thời gian tường minh."""
    interval = dclick_interval(400)
    x = view("May-X")
    load(radar, [x])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    pos = _row_pos(lst, 0)
    # Tránh đúng biên: spec không nói khoảng tính từ lần nhấn hay lần nhả trước.
    if gap == "within":
        second = first + interval // 2
    elif gap == "beyond":
        second = first + 2 * interval
    else:
        second = gap
    _ts_events(lst, [("press", pos, first), ("release", pos, first + 1)])
    _ts_events(lst, [("press", pos, second), ("release", pos, second + 1)])
    _settle(qtbot)
    assert [v.device_id for v in spy.pairs] == [x.device_id] * expected, (first, second, len(spy.pairs))
    assert spy.picks == []


def test_unpaired_double_click_after_press_released_off_row_pairs_again(radar, qtbot, dclick_interval):
    """Soát vòng 2 (B3): bấm X ở ts 10000 → ghép đôi; một giờ sau nhấn trên X nhưng nhả ngoài mọi hàng
    (không thành bấm), rồi 200 ms sau nhấn + dblclick + nhả trên X → phải ghép đôi lần hai (lần bấm cũ
    đã quá xa, lần nhấn bị kéo ra ngoài không được tính là đã phát)."""
    dclick_interval(400)
    x = view("May-X")
    load(radar, [x])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    pos, off = _row_pos(lst, 0), _empty_pos(lst)
    _ts_events(lst, [("press", pos, 10_000), ("release", pos, 10_050)])
    _settle(qtbot)
    assert len(spy.pairs) == 1
    hour = 3_600_000
    _ts_events(lst, [("press", pos, hour), ("release", off, hour + 50)])
    _settle(qtbot)
    assert len(spy.pairs) == 1, "nhấn rồi nhả ngoài hàng không phải là bấm"
    _ts_events(lst, [("press", pos, hour + 200), ("dblclick", pos, hour + 200), ("release", pos, hour + 250)])
    _settle(qtbot)
    assert [v.device_id for v in spy.pairs] == [x.device_id] * 2
    assert spy.picks == []


def test_paired_double_click_after_press_released_off_row_picks_again(radar, qtbot, dclick_interval):
    """Soát vòng 2 (B3), máy đã ghép: bấm đúp ở ts 10000 → chọn file; một giờ sau nhấn trên hàng, nhả
    ngoài hàng, rồi nhấn + dblclick + nhả trên hàng → chọn file lần hai."""
    dclick_interval(400)
    p = view("May-P", paired=True)
    load(radar, [p])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    pos, off = _row_pos(lst, 0), _empty_pos(lst)
    _ts_events(
        lst,
        [
            ("press", pos, 10_000),
            ("release", pos, 10_050),
            ("press", pos, 10_100),
            ("dblclick", pos, 10_100),
            ("release", pos, 10_150),
        ],
    )
    _settle(qtbot)
    assert spy.picks == [p.device_id]
    hour = 3_600_000
    _ts_events(lst, [("press", pos, hour), ("release", off, hour + 50)])
    _ts_events(lst, [("press", pos, hour + 200), ("dblclick", pos, hour + 200), ("release", pos, hour + 250)])
    _settle(qtbot)
    assert spy.picks == [p.device_id] * 2
    assert spy.pairs == []


def test_double_click_on_one_device_does_not_block_click_on_another(radar, qtbot):
    """Chặn lặp theo thiết bị X: bấm ngay sang máy Y khác vẫn phát cho Y."""
    x, y = view("May-X"), view("May-Y")
    load(radar, [x, y])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    _double_click(lst, _row_pos(lst, the_row(radar, "May-X")), "manual_press_dblclick")
    _click(lst, _row_pos(lst, the_row(radar, "May-Y")))
    qtbot.waitUntil(lambda: len(spy.pairs) >= 2, timeout=2000)
    _settle(qtbot)
    assert [v.device_id for v in spy.pairs] == [x.device_id, y.device_id]


@pytest.mark.parametrize("modifier", ["ControlModifier", "ShiftModifier"])
def test_modifier_clicks_never_emit_twice(radar, qtbot, modifier):
    """Spec: bấm có Ctrl/Shift xử lý như bấm thường hoặc bỏ qua, chỉ cần không phát lặp."""
    from PySide6.QtCore import Qt

    mods = getattr(Qt.KeyboardModifier, modifier)
    load(radar, [view("May-X")])
    lst = device_view_widget(radar)
    pos = _row_pos(lst, the_row(radar, "May-X"))
    spy = ClickSpy(radar)
    _click(lst, pos, modifiers=mods)
    _settle(qtbot)
    assert len(spy.pairs) <= 1 and spy.picks == [], spy.pairs
    spy2 = ClickSpy(radar)
    for way in DCLICK_WAYS:
        before = len(spy2.pairs)
        _double_click(lst, pos, way, modifiers=mods)
        _settle(qtbot, 60)
        assert len(spy2.pairs) - before <= 1, f"{way}: {spy2.pairs}"
    assert spy2.picks == []


# ---- Bàn phím -------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["Key_Return", "Key_Enter"])
def test_enter_on_unpaired_online_device_requests_pairing_once(radar, qtbot, key):
    """Spec Test 5 (máy chưa ghép online)."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    x = view("May-X")
    load(radar, [view("May-P", paired=True), x])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    lst.setFocus(Qt.FocusReason.OtherFocusReason)
    select_row(radar, the_row(radar, "May-X"))
    QTest.keyClick(lst, getattr(Qt.Key, key))
    qtbot.waitUntil(lambda: bool(spy.pairs), timeout=2000)
    _settle(qtbot)
    assert [v.device_id for v in spy.pairs] == [x.device_id]
    assert spy.picks == []


@pytest.mark.parametrize("online", [True, False])
@pytest.mark.parametrize("key", ["Key_Return", "Key_Enter"])
def test_enter_on_paired_device_requests_file_pick_once(radar, qtbot, key, online):
    """Spec Test 5 (máy đã ghép, online hay không)."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    p = view("May-P", paired=True, online=online)
    load(radar, [p, view("May-U")])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    lst.setFocus(Qt.FocusReason.OtherFocusReason)
    select_row(radar, the_row(radar, "May-P"))
    QTest.keyClick(lst, getattr(Qt.Key, key))
    qtbot.waitUntil(lambda: bool(spy.picks), timeout=2000)
    _settle(qtbot)
    assert spy.picks == [p.device_id]
    assert spy.pairs == []


def test_each_enter_press_emits_once_and_is_not_blocked_by_a_prior_click(radar, qtbot):
    """Spec “Chặn lặp”: Enter không bị chặn bởi cơ chế chặn chuỗi bấm đúp, mỗi lần nhấn phát một lần."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    x = view("May-X")
    load(radar, [x])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    lst.setFocus(Qt.FocusReason.OtherFocusReason)
    _click(lst, _row_pos(lst, the_row(radar, "May-X")))
    qtbot.waitUntil(lambda: len(spy.pairs) == 1, timeout=2000)
    QTest.keyClick(lst, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: len(spy.pairs) == 2, timeout=2000)
    QTest.keyClick(lst, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: len(spy.pairs) == 3, timeout=2000)
    _settle(qtbot)
    assert [v.device_id for v in spy.pairs] == [x.device_id] * 3


# ---- Chuột phải, phím Menu, Shift+F10 ---------------------------------------------------------------


@pytest.mark.parametrize("paired", [False, True])
def test_right_click_on_row_only_opens_menu(radar, qtbot, menus, paired):
    """Spec Test 6: chuột phải không được tính là bấm; sau đó một lần bấm trái vẫn chỉ là bấm một lần
    (không ghép với chuột phải thành bấm đúp)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtWidgets import QApplication

    d = view("May-D", paired=paired)
    load(radar, [d])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    pos = _row_pos(lst, the_row(radar, "May-D"))
    _click(lst, pos, button=Qt.MouseButton.RightButton)
    vp = lst.viewport()
    ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, vp.mapToGlobal(pos))
    QApplication.sendEvent(vp, ev)
    _settle(qtbot)
    assert spy.pairs == [] and spy.picks == [], "chỉ mở menu thì không phát gì"
    assert spy.calls["unpair_requested"] == []
    _click(lst, pos)
    _settle(qtbot, 60)
    if paired:
        assert spy.pairs == [] and spy.picks == []
    else:
        assert len(spy.pairs) == 1 and spy.picks == []


@pytest.mark.parametrize("paired", [False, True])
def test_menu_key_and_shift_f10_only_open_menu(radar, qtbot, menus, paired):
    """Spec Test 6 / bảng “Phím Menu, Shift+F10”: mở menu như cũ, không phát gì."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    load(radar, [view("May-D", paired=paired)])
    spy = ClickSpy(radar)
    row = the_row(radar, "May-D")
    items = menus.open(radar, row)  # phím Menu
    assert items, "phím Menu phải mở menu"
    lst = device_view_widget(radar)
    QTest.keyClick(lst, Qt.Key.Key_F10, Qt.KeyboardModifier.ShiftModifier)
    _settle(qtbot)
    assert spy.pairs == [] and spy.picks == []
    assert spy.calls["unpair_requested"] == []


# ---- Kéo thả ---------------------------------------------------------------------------------------


def test_dragging_files_over_unpaired_row_and_dropping_does_not_request_pairing(radar, qtbot, tmp_path):
    """Spec Test 10: kéo qua hàng chưa ghép rồi thả → chỉ `send_files_requested` như cũ."""
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
    from PySide6.QtWidgets import QApplication

    u, p = view("May-U"), view("May-P", paired=True)
    load(radar, [u, p])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    vp = lst.viewport()
    mime = QMimeData()
    a = tmp_path / "a.txt"
    mime.setUrls([QUrl.fromLocalFile(str(a))])
    copy, left, nomod = Qt.DropAction.CopyAction, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    p_pos = _row_pos(lst, the_row(radar, "May-P"))
    u_pos = _row_pos(lst, the_row(radar, "May-U"))
    QApplication.sendEvent(vp, QDragEnterEvent(p_pos, copy, mime, left, nomod))
    QApplication.sendEvent(vp, QDragMoveEvent(p_pos, copy, mime, left, nomod))
    QApplication.sendEvent(vp, QDragMoveEvent(u_pos, copy, mime, left, nomod))
    QApplication.sendEvent(vp, QDropEvent(QPointF(u_pos), copy, mime, left, nomod))
    _settle(qtbot)
    assert spy.pairs == [] and spy.picks == []
    assert [(i, [Path(x) for x in ps]) for i, ps in spy.calls["send_files_requested"]] == [(u.device_id, [a])]


# ---- Nút theo hàng đang chọn (UI-06) ---------------------------------------------------------------


def test_action_button_hidden_without_a_selected_device(radar, ui_helpers):
    """Spec Test 7: ẩn khi radar trống, sau `reset` có hàng (hàng hiện hành nhưng chưa chọn), sau `clear`."""
    from PySide6.QtCore import QItemSelectionModel

    btn = _action_button(radar)
    load(radar, [])
    assert btn.isHidden(), "radar trống → nút ẩn"
    load(radar, [view("May-X"), view("May-P", paired=True)])
    lst = device_view_widget(radar)
    assert _selected_rows(lst) == [], "tiền đề spec: nạp ảnh chụp không chọn sẵn"
    assert btn.isHidden(), "chưa chọn thiết bị nào → nút ẩn"
    lst.selectionModel().setCurrentIndex(lst.model().index(0, 0), QItemSelectionModel.SelectionFlag.NoUpdate)
    assert btn.isHidden(), "hàng hiện hành mà chưa được chọn → nút vẫn ẩn"
    select_row(radar, the_row(radar, "May-P"))
    assert not btn.isHidden()
    reset(radar)
    assert btn.isHidden(), "sau clear → nút ẩn"
    assert ui_helpers.buttons_with_text(radar, SEND_BUTTON) == []
    assert ui_helpers.buttons_with_text(radar, PAIR_BUTTON) == []


def test_action_button_text_follows_selected_device_and_its_paired_state(radar):
    """Spec Test 7: “Ghép đôi…” ↔ “Gửi file…” khi thiết bị đang chọn đổi `paired` (dataChanged)."""
    x = view("May-X")
    load(radar, [x])
    btn = _action_button(radar)
    select_row(radar, the_row(radar, "May-X"))
    assert not btn.isHidden()
    assert ui_text(btn.text()) == PAIR_BUTTON
    feed(radar, _ev("DeviceConnected", view("May-X", paired=True, connected=True)))  # vừa ghép xong
    assert not btn.isHidden()
    assert ui_text(btn.text()) == SEND_BUTTON
    feed(radar, _ev("DeviceOnline", view("May-X", paired=False)))  # hủy ghép, vẫn trong mạng
    assert not btn.isHidden()
    assert ui_text(btn.text()) == PAIR_BUTTON


def test_action_button_follows_selection_changes(radar):
    load(radar, [view("May-X"), view("May-P", paired=True), view("May-Off", paired=True, online=False)])
    btn = _action_button(radar)
    for name, expected in (("May-P", SEND_BUTTON), ("May-X", PAIR_BUTTON), ("May-Off", SEND_BUTTON)):
        select_row(radar, the_row(radar, name))
        assert not btn.isHidden(), name
        assert ui_text(btn.text()) == expected, name
    device_view_widget(radar).clearSelection()
    assert btn.isHidden(), "bỏ chọn → nút ẩn"


def test_action_button_hides_when_selected_unpaired_device_leaves(radar):
    """Spec Test 7: hàng đang chọn bị bỏ (DeviceOffline máy chưa ghép) → nút ẩn và không hàng nào được
    chọn (không được để Qt tự chọn hàng bên cạnh: nút sẽ trỏ sang máy người dùng không chọn). Máy rời
    mạng nằm giữa hai hàng khác để hàng trên lẫn hàng dưới đều có thể bị chọn nhầm."""
    load(radar, [view("May-A", paired=True), view("May-X"), view("May-Z", paired=True)])
    btn = _action_button(radar)
    select_row(radar, the_row(radar, "May-X"))
    assert not btn.isHidden()
    feed(radar, _ev("DeviceOffline", view("May-X", online=False)))
    assert rows_with(radar, "May-X") == []
    lst = device_view_widget(radar)
    assert _selected_rows(lst) == [], f"không được tự chọn hàng khác: {_selected_rows(lst)}"
    assert btn.isHidden()


def test_action_button_hides_after_clear_of_selected_device(radar):
    load(radar, [view("May-P", paired=True)])
    btn = _action_button(radar)
    select_row(radar, 0)
    assert not btn.isHidden()
    radar.model.clear()
    assert btn.isHidden()


def _selected_ids(radar) -> list[str]:
    lst = device_view_widget(radar)
    return [radar.model.devices()[r].device_id for r in _selected_rows(lst)]


@pytest.mark.parametrize("paired", [False, True])
def test_renaming_selected_device_keeps_it_selected(radar, paired):
    """Soát code: đổi tên thiết bị đang chọn (khóa sắp xếp đổi, hàng nhảy chỗ) → vẫn chọn đúng thiết bị
    đó, nút còn hiện đúng chữ."""
    m = view("May-M", paired=paired)
    # May-Z cùng nhóm (đã ghép / chưa ghép) với May-M: radar sắp máy đã ghép đôi lên trước (PR-7 B7),
    # nên chỉ trong cùng nhóm thì đổi tên mới làm hàng nhảy chỗ.
    load(radar, [view("May-A", paired=True), m, view("May-Z", paired=paired)])
    btn = _action_button(radar)
    select_row(radar, the_row(radar, "May-M"))
    old_row = the_row(radar, "May-M")
    feed(radar, _ev("DeviceOnline", view("May-M", name="Zzz-Moi", paired=paired)))
    assert the_row(radar, "Zzz-Moi") != old_row, "tiền đề: tên mới đổi vị trí hàng"
    assert _selected_ids(radar) == [m.device_id]
    assert not btn.isHidden()
    assert ui_text(btn.text()) == (SEND_BUTTON if paired else PAIR_BUTTON)
    assert "Zzz-Moi" in btn.accessibleDescription()


def test_reset_keeps_selected_device_selected_and_button_shown(radar):
    """Soát code: nạp lại ảnh chụp (`reset`) khi có thiết bị đang chọn → vẫn chọn nó, nút còn hiện."""
    p = view("May-P", paired=True)
    views = [view("May-A"), p, view("May-Z")]
    load(radar, views)
    btn = _action_button(radar)
    select_row(radar, the_row(radar, "May-P"))
    load(radar, [view("May-B"), *views])  # thêm một hàng phía trên để chỉ số hàng đổi
    assert _selected_ids(radar) == [p.device_id]
    assert not btn.isHidden()
    assert ui_text(btn.text()) == SEND_BUTTON


def test_reset_with_only_current_row_keeps_button_hidden(radar):
    """Soát code: `reset` khi chỉ có hàng hiện hành (chưa chọn) → không tự chọn, nút vẫn ẩn."""
    from PySide6.QtCore import QItemSelectionModel

    views = [view("May-A"), view("May-P", paired=True)]
    load(radar, views)
    lst = device_view_widget(radar)
    lst.selectionModel().setCurrentIndex(
        lst.model().index(the_row(radar, "May-P"), 0), QItemSelectionModel.SelectionFlag.NoUpdate
    )
    btn = _action_button(radar)
    assert btn.isHidden()
    load(radar, views)
    assert _selected_rows(lst) == []
    assert btn.isHidden()


@pytest.mark.parametrize("paired", [False, True])
def test_enter_auto_repeat_does_not_emit_again(radar, qtbot, paired):
    """Soát code: giữ Enter sinh các `KeyPress` có `autoRepeat` (Windows: chỉ press; X11: cặp
    release/press, cả hai autoRepeat) → chỉ lần nhấn thật phát."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    d = view("May-D", paired=paired)
    load(radar, [d])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    lst.setFocus(Qt.FocusReason.OtherFocusReason)
    select_row(radar, 0)
    press, release = QEvent.Type.KeyPress, QEvent.Type.KeyRelease
    ret, nomod = Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier
    QApplication.sendEvent(lst, QKeyEvent(press, ret, nomod, "\r", False))
    for _ in range(3):
        QApplication.sendEvent(lst, QKeyEvent(press, ret, nomod, "", True))
    QApplication.sendEvent(lst, QKeyEvent(release, ret, nomod, "", True))
    QApplication.sendEvent(lst, QKeyEvent(press, ret, nomod, "", True))
    QApplication.sendEvent(lst, QKeyEvent(release, ret, nomod, "\r", False))
    _settle(qtbot)
    if paired:
        assert spy.picks == [d.device_id] and spy.pairs == []
    else:
        assert [v.device_id for v in spy.pairs] == [d.device_id] and spy.picks == []


@pytest.mark.parametrize("way", DCLICK_WAYS)
def test_double_click_while_event_loop_is_blocked_emits_pairing_once(radar, qtbot, dclick_interval, way):
    """Soát code: handler của `pair_requested` chặn luồng Qt lâu hơn `doubleClickInterval` (đặt 40 ms,
    handler ngủ 120 ms) → phần còn lại của chuỗi bấm đúp tới “muộn” theo đồng hồ, nhưng vẫn là cùng một
    lần bấm đúp: tổng cộng đúng một `pair_requested`."""
    interval = dclick_interval(40)
    x = view("May-X")
    load(radar, [x])
    spy = ClickSpy(radar)
    radar.pair_requested.connect(lambda _v: time.sleep((interval + 80) / 1000))
    lst = device_view_widget(radar)
    _double_click(lst, _row_pos(lst, 0), way)
    qtbot.waitUntil(lambda: bool(spy.pairs), timeout=2000)
    _settle(qtbot)
    assert [v.device_id for v in spy.pairs] == [x.device_id], f"{way}: {len(spy.pairs)} lần"


@pytest.mark.parametrize("way", ["qtest", "manual"])
def test_fast_triple_click_requests_pairing_once(radar, qtbot, way):
    """Soát code: bấm ba lần liền (click, dblclick, click trong cùng một chuỗi, mỗi khoảng < interval)
    → một `pair_requested`."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    x = view("May-X")
    load(radar, [x])
    spy = ClickSpy(radar)
    lst = device_view_widget(radar)
    pos = _row_pos(lst, 0)
    left, none, nomod = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier
    if way == "qtest":
        QTest.mouseClick(lst.viewport(), left, nomod, pos)
        QTest.mouseDClick(lst.viewport(), left, nomod, pos)
        QTest.mouseClick(lst.viewport(), left, nomod, pos)
    else:
        t = QEvent.Type
        seq = [
            (t.MouseButtonPress, left),
            (t.MouseButtonRelease, none),
            (t.MouseButtonPress, left),
            (t.MouseButtonDblClick, left),
            (t.MouseButtonRelease, none),
            (t.MouseButtonPress, left),
            (t.MouseButtonRelease, none),
        ]
        for kind, buttons in seq:
            _send_mouse(lst, kind, pos, left, buttons)
    QApplication.processEvents()
    qtbot.waitUntil(lambda: bool(spy.pairs), timeout=2000)
    _settle(qtbot)
    assert [v.device_id for v in spy.pairs] == [x.device_id], f"{way}: {len(spy.pairs)} lần"
    assert spy.picks == []


def test_action_button_pair_uses_latest_view_of_selected_device(radar, qtbot):
    """Spec: bấm nút đọc thiết bị được chọn tại lúc bấm (view mới nhất của model)."""
    load(radar, [view("May-X", port=47001)])
    btn = _action_button(radar)
    select_row(radar, the_row(radar, "May-X"))
    newer = view("May-X", port=47999)
    feed(radar, _ev("DeviceOnline", newer))
    spy = ClickSpy(radar)
    assert ui_text(btn.text()) == PAIR_BUTTON
    btn.click()
    _settle(qtbot)
    assert len(spy.pairs) == 1
    assert (spy.pairs[0].device_id, spy.pairs[0].port) == (newer.device_id, 47999)
    assert spy.picks == []


def test_action_button_after_pairing_completes_requests_file_pick(radar, qtbot):
    """Chọn máy chưa ghép, nó vừa ghép xong (dataChanged) → bấm nút là “Gửi file…”, không ghép lại."""
    x = view("May-X")
    load(radar, [x])
    btn = _action_button(radar)
    select_row(radar, 0)
    feed(radar, _ev("DeviceConnected", view("May-X", paired=True, connected=True)))
    spy = ClickSpy(radar)
    btn.click()
    _settle(qtbot)
    assert spy.picks == [x.device_id]
    assert spy.pairs == []


def test_action_button_is_left_of_ip_button_on_the_same_row(radar, ui_helpers, qtbot):
    from PySide6.QtCore import QPoint

    load(radar, [view("May-P", paired=True)])
    select_row(radar, 0)
    btn, ip = _action_button(radar), _ip_button(radar, ui_helpers)
    _settle(qtbot)
    assert btn.isVisible() and ip.isVisible()
    b, i = btn.mapTo(radar, QPoint(0, 0)), ip.mapTo(radar, QPoint(0, 0))
    assert b.x() + btn.width() <= i.x(), "nút hành động nằm bên trái nút “Nhập IP thủ công”"
    b_mid, i_mid = b.y() + btn.height() / 2, i.y() + ip.height() / 2
    assert abs(b_mid - i_mid) < max(btn.height(), ip.height()) / 2, "hai nút cùng một hàng"


def test_action_button_accessibility_names_the_device(radar):
    """Spec N12: `accessibleName` = chữ nút; `accessibleDescription` nêu tên máy qua `texts.peer_label`."""
    from teleport.ui.texts import peer_label

    p, x = view("PC-Nha", paired=True), view("<b>May-X</b>")
    load(radar, [p, x])
    btn = _action_button(radar)
    for v in (p, x):
        select_row(radar, radar.model.row_of(v.device_id))
        assert ui_text(btn.accessibleName()) == ui_text(btn.text())
        assert peer_label(v.name, v.device_id) in btn.accessibleDescription(), btn.accessibleDescription()
    select_row(radar, radar.model.row_of(p.device_id))
    assert "May-X" not in btn.accessibleDescription(), "mô tả phải theo thiết bị đang chọn"


def test_action_button_text_survives_retranslate(radar):
    load(radar, [view("May-X"), view("May-P", paired=True)])
    btn = _action_button(radar)
    select_row(radar, the_row(radar, "May-X"))
    radar.retranslate()
    assert not btn.isHidden() and ui_text(btn.text()) == PAIR_BUTTON
    select_row(radar, the_row(radar, "May-P"))
    radar.retranslate()
    assert not btn.isHidden() and ui_text(btn.text()) == SEND_BUTTON
    device_view_widget(radar).clearSelection()
    radar.retranslate()
    assert btn.isHidden(), "retranslate không được làm hiện nút khi chưa chọn gì"


# ---- Thứ tự Tab, câu chữ ---------------------------------------------------------------------------


def test_tab_from_last_row_goes_to_action_button_then_ip_button(radar, ui_helpers):
    """Spec Test 8 (nút hiện): hàng cuối → Tab → nút hành động → Tab → “Nhập IP thủ công”; Shift+Tab
    quay lại nút hành động."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    load(radar, [view("May-1", paired=True), view("May-2")])
    radar.activateWindow()
    lst = device_view_widget(radar)
    lst.setFocus(Qt.FocusReason.OtherFocusReason)
    select_row(radar, lst.model().rowCount() - 1)
    btn, ip = _action_button(radar), _ip_button(radar, ui_helpers)
    assert not btn.isHidden()
    QTest.keyClick(lst, Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is btn, f"Tab từ hàng cuối tới {QApplication.focusWidget()!r}"
    QTest.keyClick(btn, Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is ip
    QTest.keyClick(ip, Qt.Key.Key_Backtab)
    assert QApplication.focusWidget() is btn


def test_tab_skips_hidden_action_button(radar, ui_helpers):
    """Spec Test 8 (nút ẩn): hàng cuối (hiện hành, chưa chọn) → Tab → thẳng “Nhập IP thủ công”."""
    from PySide6.QtCore import QItemSelectionModel, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    load(radar, [view("May-1", paired=True)])
    radar.activateWindow()
    lst = device_view_widget(radar)
    lst.selectionModel().setCurrentIndex(lst.model().index(0, 0), QItemSelectionModel.SelectionFlag.NoUpdate)
    lst.setFocus(Qt.FocusReason.OtherFocusReason)
    lst.clearSelection()
    btn, ip = _action_button(radar), _ip_button(radar, ui_helpers)
    assert btn.isHidden(), "tiền đề: chưa chọn thiết bị nào"
    QTest.keyClick(lst, Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is ip


def test_device_list_accessible_description_mentions_enter(radar):
    """Spec UI-14: câu mô tả trợ năng của danh sách."""
    assert _norm(device_view_widget(radar).accessibleDescription()) == LIST_DESCRIPTION


# =================================================================================================
# Đường đi thật: main() → GuiApp → NodeHost → radar
# =================================================================================================


def host_started(run) -> bool:
    return bool(run.hosts) and run.hosts[-1].running


def quit_via_tray(run, plain):
    [quit_action] = [a for a in run.tray.actions() if not a.isSeparator() and plain(a.text()) == "Thoát"]
    quit_action.trigger()


def _sync_with_node(run, box: dict):
    """Gọi `devices()` qua host và chờ kết quả: các `call` trước (ảnh chụp lúc `started`) đã về."""
    box.clear()
    run.host.call(lambda n: n.devices(), lambda r: box.setdefault("v", r))


@integration
def test_snapshot_at_started_lists_trusted_device(gui, gui_wait, config_dir, trust_each_other, ui_helpers):
    a, b = config_dir("A"), config_dir("B")
    trust_each_other(a, b, "May-A", "May-B")
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: host_started(run), 15, "node chạy")
        yield gui_wait(lambda: rows_with(find_radar(run.window), "May-B"), 5, "May-B trên radar")
        r = find_radar(run.window)
        seen["row"] = row_texts(r)[the_row(r, "May-B")]
        seen["ip"] = bool(ui_helpers.buttons_with_text(r, IP_BUTTON))
        seen["radar_visible"] = r.isVisible()
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(a)], script)
    assert PAIRED_LABEL in seen["row"]
    assert CONNECTED_WORD not in seen["row"].lower(), "B không chạy: không “đã kết nối”"
    assert seen["ip"] and seen["radar_visible"]
    assert run.code == 0


@integration
def test_node_events_from_host_reach_radar(gui, gui_wait, config_dir, ui_helpers):
    from teleport.core.links import ConnectResult

    a = config_dir("A")
    seen: dict = {}
    box: dict = {}

    def script(run):
        yield gui_wait(lambda: host_started(run), 15, "node chạy")
        _sync_with_node(run, box)
        yield gui_wait(lambda: "v" in box, 5, "devices() trả về")
        r = find_radar(run.window)
        seen["empty"] = _norm(mvp_message(EMPTY_ROW)) in radar_text(r, ui_helpers.all_text)
        run.host.node_event.emit(_ev("DeviceOnline", view("May-X")))
        p = view("PC-Nha", paired=True)
        run.host.node_event.emit(_ev("DeviceOnline", p))
        run.host.node_event.emit(_ev("ConnectFailed", p, result=ConnectResult.VERSION_MISMATCH))
        yield gui_wait(lambda: rows_with(r, "May-X") and rows_with(r, "PC-Nha"), 5, "hai hàng mới")
        select_row(r, the_row(r, "PC-Nha"))
        seen["text"] = radar_text(r, ui_helpers.all_text)
        seen["x"] = row_texts(r)[the_row(r, "May-X")]
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(a)], script)
    assert seen["empty"], "chưa có thiết bị → câu “Chưa thấy thiết bị nào…”"
    assert UNPAIRED_LABEL in seen["x"]
    expected = _norm(fill(mvp_message(CONNECT_RESULT_ROWS["version_mismatch"]), **{"PC-Nha": "PC-Nha"}))
    assert expected in seen["text"]
    assert run.code == 0


@integration
def test_retry_rebuilds_model_and_ignores_old_host(
    gui, gui_wait, config_dir, trust_each_other, ui_helpers, fast_lock
):
    """Tiêu chí 8: “Thử lại” dựng host mới → xóa model, nạp lại từ `devices()` của host mới; sự kiện
    của host cũ bị bỏ (rủi ro 1, mục 10)."""
    from teleport.app.devices import DeviceView
    from teleport.core.links import ConnectResult
    from teleport.lock import NodeLock

    a, b = config_dir("A"), config_dir("B")
    _, id_b = trust_each_other(a, b, "May-A", "May-B")
    b_view = DeviceView(
        device_id=id_b.device_id,
        name="May-B",
        os=None,
        paired=True,
        online=False,
        connected=False,
        addresses=(),
        port=None,
    )
    rejected = _norm(fill(mvp_message(CONNECT_RESULT_ROWS["rejected"]), **{"PC-Nha": "May-B"}))
    holder = NodeLock(a)
    holder.acquire()
    seen: dict = {}

    def script(run):
        yield gui_wait(
            lambda: run.windows and ui_helpers.M2_HEAD in ui_helpers.all_text(run.window), 15, "câu M2"
        )
        old = run.host
        old.node_event.emit(_ev("DeviceOnline", view("May-Cu")))  # sự kiện còn sót của host lỗi
        # Lỗi kết nối nhớ theo thiết bị (mục 9.5) sống qua lần nạp lại devices(); chỉ host mới xóa được.
        old.node_event.emit(_ev("ConnectFailed", b_view, result=ConnectResult.REJECTED))
        holder.release()
        [retry] = ui_helpers.buttons_with_text(run.window, ui_helpers.RETRY)
        retry.click()
        yield gui_wait(lambda: len(run.hosts) >= 2 and host_started(run), 15, "node chạy sau Thử lại")
        yield gui_wait(lambda: rows_with(find_radar(run.window), "May-B"), 5, "May-B trên radar")
        old.node_event.emit(_ev("DeviceOnline", view("May-Ma")))  # host cũ phát sau khi đã thay
        for _ in range(5):
            yield
        r = find_radar(run.window)
        seen["rows"] = row_texts(r)
        select_row(r, the_row(r, "May-B"))
        seen["text"] = radar_text(r, ui_helpers.all_text)
        quit_via_tray(run, ui_helpers.plain)

    try:
        run = gui(["--config-dir", str(a)], script)
    finally:
        holder.release()
    rows = seen["rows"]
    assert any("May-B" in t for t in rows), rows
    assert rejected not in seen["text"], "lỗi kết nối của host cũ còn trên radar: Thử lại phải xóa model"
    assert not any("May-Cu" in t for t in rows), f"model không được làm lại sau Thử lại: {rows}"
    assert not any("May-Ma" in t for t in rows), f"sự kiện của host cũ lọt vào radar: {rows}"
    assert run.code == 0


# =================================================================================================
# Tiêu chí 5: mDNS thật trên 127.0.0.1 (mẫu tests/app/test_node_mdns.py)
# =================================================================================================


def _mdns(n: int):
    from teleport.app.node import MdnsOptions

    return MdnsOptions(
        interfaces=(LOCAL,),
        advertise_addresses=(f"192.0.2.{n}",),
        probe_interval=1.0,
        resolve_timeout=0.5,
        probe_tick=0.2,
    )


class _MdnsPeer:
    """Node B có mDNS, chạy event loop riêng trong một thread."""

    def __init__(self, config: Path, n: int) -> None:
        import asyncio

        self.config, self.n = config, n
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="test-radar-peer", daemon=True)
        self.thread.start()
        self.node = None

    def start_async(self):
        import asyncio

        from teleport.app.node import Node

        async def go():
            self.node = Node(self.config, name="May-B", host=LOCAL, port=0, mdns=_mdns(self.n))
            await self.node.start()

        return asyncio.run_coroutine_threadsafe(go(), self.loop)

    def close_async(self):
        import asyncio

        async def go():
            if self.node is not None:
                await asyncio.wait_for(self.node.close(), 30)

        return asyncio.run_coroutine_threadsafe(go(), self.loop)

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self.close_async().result(35)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        if not self.thread.is_alive():
            self.loop.close()


@integration
@slow
@pytest.mark.skipif(os.environ.get("TELEPORT_SKIP_MDNS") == "1", reason="TELEPORT_SKIP_MDNS=1")
def test_mdns_peer_appears_and_disappears_on_radar_within_n1(
    qtbot, qapp, config_dir, node_options, monkeypatch
):
    from teleport.ui import app as ui_app

    monkeypatch.setattr(
        os, "_exit", lambda code=0: (_ for _ in ()).throw(AssertionError(f"os._exit({code})"))
    )
    appear_limit, vanish_limit = n1_limits()
    a, b = config_dir("A"), config_dir("B")
    controller = ui_app.GuiApp(qapp, a, None, make_options=lambda d: node_options(d, mdns=_mdns(61)))
    peer = _MdnsPeer(b, 62)
    try:
        controller.start()
        qtbot.waitUntil(lambda: controller.host is not None and controller.host.running, timeout=15_000)
        r = find_radar(controller.window)
        t0 = time.monotonic()
        fut = peer.start_async()
        qtbot.waitUntil(lambda: bool(rows_with(r, "May-B")), timeout=int((appear_limit + 5) * 1000))
        appeared = time.monotonic() - t0
        fut.result(10)
        assert UNPAIRED_LABEL in row_texts(r)[the_row(r, "May-B")]
        assert appeared < appear_limit, f"B hiện trên radar sau {appeared:.2f} s (N1: < {appear_limit} s)"

        t1 = time.monotonic()
        closing = peer.close_async()
        qtbot.waitUntil(lambda: not rows_with(r, "May-B"), timeout=int((vanish_limit + 5) * 1000))
        vanished = time.monotonic() - t1
        closing.result(35)
        assert vanished < vanish_limit, (
            f"B đóng mà radar còn hiện sau {vanished:.2f} s (N1: < {vanish_limit} s)"
        )
    finally:
        peer.shutdown()
        host = controller.host
        if host is not None:
            host.stop()
            deadline = time.monotonic() + 30
            while not host.join(0.05) and time.monotonic() < deadline:
                qtbot.wait(20)
        controller.shutdown()
        controller.window.deleteLater()


# =================================================================================================
# Q7 / mục 9.10: log ra `<config_dir>/logs/teleport.log`, chỉ sau SingleInstance.acquire
# =================================================================================================


def _drop_file_handlers(under: Path) -> None:
    for logger in (logging.getLogger(), logging.getLogger("teleport")):
        for h in list(logger.handlers):
            name = getattr(h, "baseFilename", None)
            if name and Path(name).resolve().is_relative_to(under.resolve()):
                logger.removeHandler(h)
                with contextlib.suppress(Exception):
                    h.close()


def _file_handlers(path: Path) -> list:
    out = []
    for logger in (logging.getLogger(), logging.getLogger("teleport")):
        for h in logger.handlers:
            name = getattr(h, "baseFilename", None)
            if name and os.path.normcase(os.path.abspath(name)) == os.path.normcase(os.path.abspath(path)):
                out.append(h)
    return out


@integration
def test_gui_logs_to_rotating_file_in_config_dir(gui, gui_wait, config_dir, ui_helpers, tmp_path):
    a = config_dir("A")
    log_file = a / "logs" / "teleport.log"
    marker = "radar-test-dau-vet-7f3a"
    seen: dict = {}

    def script(run):
        yield gui_wait(lambda: host_started(run), 15, "node chạy")
        logging.getLogger("teleport.ui.test_radar").error(marker)
        handlers = _file_handlers(log_file)
        seen["handlers"] = [
            (type(h), getattr(h, "maxBytes", None), getattr(h, "backupCount", None)) for h in handlers
        ]
        for h in handlers:
            h.flush()
        quit_via_tray(run, ui_helpers.plain)

    try:
        run = gui(["--config-dir", str(a)], script)
        assert run.code == 0
        assert seen["handlers"], f"không có handler ghi vào {log_file}"
        [(kind, max_bytes, backups)] = seen["handlers"]
        assert issubclass(kind, logging.handlers.RotatingFileHandler)
        assert max_bytes == 1024 * 1024, "Q7: mỗi file 1 MiB"
        assert backups is not None and 1 <= backups <= 2, "Q7: xoay 2 × 1 MiB"
        assert marker in log_file.read_text(encoding="utf-8", errors="replace")
    finally:
        _drop_file_handlers(tmp_path)


@slow
def test_no_log_file_written_before_single_instance_acquired(gui, config_dir, tmp_path):
    """Mục 9.10: bản thứ hai (không lấy được `gui.lock`) không ghi vào log của bản đang chạy."""
    from teleport.lock import NodeLock

    a = config_dir("A", identity=False)
    holder = NodeLock(a, "gui.lock")
    holder.acquire()

    def script(run):
        return
        yield

    try:
        run = gui(["--config-dir", str(a)], script, require_script_done=False)
    finally:
        holder.release()
        handlers = _file_handlers(a / "logs" / "teleport.log")
        _drop_file_handlers(tmp_path)
    assert run.code == 1
    assert handlers == [], "gắn handler log file trước khi có SingleInstance"
    log_file = a / "logs" / "teleport.log"
    assert not log_file.exists() or log_file.stat().st_size == 0, log_file.read_text(errors="replace")
