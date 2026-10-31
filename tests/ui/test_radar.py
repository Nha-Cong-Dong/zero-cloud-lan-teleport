"""Model và chữ của radar (spec-hackathon mục 2 PR1 “Xong khi” 1–9, mục 9.1, 9.5, 9.9, 9.10, mục 10).

Spec B2 (mục 2.1, 8(a), 12.31) gỡ widget `RadarView`: `teleport.ui.radar` chỉ còn `DeviceListModel` và chữ
dùng chung. Test của widget cũ (nhập IP: `test_discover.py`; bấm, bấm đúp, Enter, menu, Tab, giữ lựa chọn:
`test_conversations.py`; kéo thả: `test_conversations_drop.py`) đã dời hoặc bỏ. File này giữ test của model,
chữ và bốn test tích hợp (đọc `gui.window.devices`).

Test viết theo spec, không theo code. Câu chữ của mvp mục 7.1 / mục 8 được đọc lại từ `docs/mvp.md`
(không chép tay), để tài liệu và giao diện không lệch nhau. Ngưỡng N1 cũng đọc từ `docs/mvp.md`.

Hai lớp test:

- `DeviceListModel` đứng riêng (`teleport.ui.radar`): nạp ảnh chụp `devices()` và `NodeEvent` giả qua
  ba hàm `load`, `feed`, `reset` ở đầu file (model theo `device_id`, ảnh chụp lúc `started`, cập nhật
  theo `NodeEvent`, host mới thì xóa). Không mở socket.
- Đường đi thật qua `teleport.ui.app.main` (fixture `gui`) hoặc `GuiApp` (mDNS): ảnh chụp lúc
  `started`, nối `node_event`, “Thử lại”, log ra file (Q7, mục 9.10); model là `window.devices`.
"""

from __future__ import annotations

import contextlib
import hashlib
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
CONNECTED_WORD = "đã kết nối"  # spec PR1: câu mới, trạng thái “đã kết nối”


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


# Hàng mvp mục 8 cho mỗi ConnectResult ≠ OK (spec PR1 “Câu” và tiêu chí 4).
CONNECT_RESULT_ROWS = {
    "unreachable": "Thấy nhưng không kết nối được",
    "rejected": "Máy kia từ chối máy này",
    "identity_mismatch": "Danh tính thiết bị thay đổi",
    "version_mismatch": "Khác phiên bản giao thức",
}


# =================================================================================================
# Truy cập model (spec B2 12.31: helper đọc thẳng `DeviceListModel`)
# =================================================================================================


def make_model():
    from teleport.ui.radar import DeviceListModel

    return DeviceListModel()


def load(radar, views) -> None:
    """Nạp ảnh chụp `Node.devices()` (lúc `started`)."""
    radar.reset(tuple(views))


def feed(radar, event) -> None:
    """Một `NodeEvent`."""
    radar.apply(event)


def reset(radar) -> None:
    """Host mới (“Thử lại”): xóa model."""
    radar.clear()


def find_radar(root):
    """Model thiết bị của cửa sổ (`MainWindow.devices`, spec B2 mục 2.1)."""
    from teleport.ui.radar import DeviceListModel

    model = root.devices
    assert isinstance(model, DeviceListModel)
    return model


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
    """Chữ của từng hàng của model (mọi vai trò dữ liệu kiểu chuỗi, gộp)."""
    out = []
    for r in range(radar.rowCount()):
        idx = radar.index(r, 0)
        parts = []
        for role in _roles():
            data = radar.data(idx, role)
            if isinstance(data, str) and data:
                parts.append(data)
        out.append(_norm(" ".join(parts)))
    return out


def rows_with(radar, shown_name: str) -> list[int]:
    return [i for i, t in enumerate(row_texts(radar)) if shown_name in t]


def the_row(radar, shown_name: str) -> int:
    rows = rows_with(radar, shown_name)
    assert len(rows) == 1, f"cần đúng một hàng cho {shown_name!r}, thấy {rows}: {row_texts(radar)}"
    return rows[0]


def radar_text(radar) -> str:
    """Mọi chữ của các hàng model (câu lỗi kết nối nằm trong chữ hàng)."""
    return _norm("\n".join(row_texts(radar)))


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


# =================================================================================================
# Fixture
# =================================================================================================


@pytest.fixture
def radar(qapp):
    """`DeviceListModel` đứng riêng (spec B2 mục 8(a): fixture `radar` thành model)."""
    model = make_model()
    yield model
    model.deleteLater()


# =================================================================================================
# Tiêu chí 9 (phần radar): tài liệu
# =================================================================================================


def test_radar_is_listed_in_architecture_ui_row():
    """Spec PR1 “File”: architecture mục 3 dòng `ui` có `radar`; spec B2 (12.31, mục 4 việc #10): có cả
    `ui/conversations.py`."""
    text = (Path(__file__).resolve().parents[2] / "docs" / "architecture.md").read_text(encoding="utf-8")
    sec = _section(text, "## 3.")
    assert "radar" in sec, "architecture mục 3 chưa nhắc tới ui/radar.py"
    assert "conversations" in sec, "architecture mục 3 chưa nhắc tới ui/conversations.py"


def test_radar_module_no_longer_has_a_widget():
    """Spec B2 mục 2.1: `RadarView` gỡ hẳn, module chỉ còn model và chữ dùng chung."""
    from teleport.ui import radar as mod

    for name in ("DeviceListModel", "DEVICE_ROLE", "DEVICE_ID_ROLE", "FAILURE_ROLE"):
        assert hasattr(mod, name), name
    for name in ("no_devices_text", "invalid_address_text", "connect_failed_text", "drop_hint_text"):
        assert callable(getattr(mod, name)), name
    assert callable(mod.can_receive_files)
    import inspect

    from PySide6.QtWidgets import QLayout, QWidget

    widgets = [
        name
        for name, cls in inspect.getmembers(mod, inspect.isclass)
        if cls.__module__ == mod.__name__ and issubclass(cls, (QWidget, QLayout))
    ]
    assert widgets == [], f"teleport.ui.radar còn widget/layout: {widgets}"


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
    model = radar
    idx = model.index(0, 0)
    roles = {"ToolTipRole": Qt.ItemDataRole.ToolTipRole}  # QToolTip hiện rich text khi mightBeRichText
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
    expected = _norm(fill(mvp_message(CONNECT_RESULT_ROWS[result]), **{"PC-Nha": name}))
    text = radar_text(radar)
    assert expected in text, f"{result}: thiếu {expected!r} trong {text!r}"
    # Lỗi gắn với đúng thiết bị: không có câu mang tên máy kia.
    wrong = _norm(fill(mvp_message(CONNECT_RESULT_ROWS[result]), **{"PC-Nha": other}))
    assert wrong not in radar_text(radar)


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
        return radar_text(radar)

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
    text = radar_text(radar)
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
    expected = _norm(fill(mvp_message(CONNECT_RESULT_ROWS["rejected"]), **{"PC-Nha": "PC-Nha"}))
    assert expected not in radar_text(radar)


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
        w = make_model()
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
        # Spec B2: máy đã ghép đôi có một hàng ở danh sách cuộc trò chuyện (thay nút IP + radar hiện).
        conv = run.window.conversations
        seen["listed"] = conv.model.row_of(r.devices()[the_row(r, "May-B")].device_id) >= 0
        seen["visible"] = conv.list_view.isVisible()
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(a)], script)
    assert PAIRED_LABEL in seen["row"]
    assert CONNECTED_WORD not in seen["row"].lower(), "B không chạy: không “đã kết nối”"
    assert seen["listed"] and seen["visible"]
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
        seen["empty"] = r.rowCount() == 0
        run.host.node_event.emit(_ev("DeviceOnline", view("May-X")))
        p = view("PC-Nha", paired=True)
        run.host.node_event.emit(_ev("DeviceOnline", p))
        run.host.node_event.emit(_ev("ConnectFailed", p, result=ConnectResult.VERSION_MISMATCH))
        yield gui_wait(lambda: rows_with(r, "May-X") and rows_with(r, "PC-Nha"), 5, "hai hàng mới")
        seen["text"] = radar_text(r)
        seen["x"] = row_texts(r)[the_row(r, "May-X")]
        quit_via_tray(run, ui_helpers.plain)

    run = gui(["--config-dir", str(a)], script)
    assert seen["empty"], "chưa có thiết bị → model rỗng"
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
        seen["text"] = radar_text(r)
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
