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
    """mvp mục 7.1: “Kéo file thả vào một thiết bị” (spec PR1 “Câu”)."""
    load(radar, [view("May-1", paired=True)])
    assert "Kéo file thả vào một thiết bị" in radar_text(radar, ui_helpers.all_text)


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
