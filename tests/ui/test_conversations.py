"""Danh sách cuộc trò chuyện ở cột trái (spec B2 mục 2.4–2.6, 2.9, 2.10; mục 12 thắng): T-M, T-V, T-K.

Viết từ spec, không từ code.

- T-M: `ConversationModel` (lọc, sắp, `set_activity`, role) và ba hàm chữ thuần `status_line`,
  `activity_text`, `time_text`. Dữ liệu hoạt động dựng thẳng bằng các dataclass của `ui.activity`.
- T-V: `ConversationList` đứng riêng (bấm, phím, menu, Tab, giữ lựa chọn, tên độc, màu). Màu đọc bằng
  `grab()` của `MainWindow` (cột trái có nền `sidebar` thật), sáng + tối.
- T-K: cửa sổ thu gọn (< 820 px) với `MainWindow` thật: `SIDEBAR_COMPACT_WIDTH = 80` (12.5).

Seam giờ: `teleport.ui.conversations._wall_now`. Trình tự bấm đúp `DCLICK_WAYS` dời từ `test_radar.py`.
"""

from __future__ import annotations

import collections
import datetime
import hashlib
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QItemSelectionModel, QPoint, QRect, Qt, QTranslator
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QPushButton,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# ---- Chữ của spec (2.4, 2.9, 2.10) ------------------------------------------------------------------
LIST_NAME = "Máy đã ghép đôi"
# Spec B3 mục 6: Enter mở cuộc trò chuyện (thay “Enter để gửi file” của B2).
LIST_DESCRIPTION = (
    "Mũi tên để chọn máy, Tab để đi qua từng máy, Enter để mở cuộc trò chuyện, phím Menu để mở thao tác."
)
ADD_DEVICE = "Ghép đôi máy mới"
SEND_FILES = "Gửi file…"
UNPAIR = "Hủy ghép đôi…"
MENU_NAME = "Thao tác với {name}"
ONLINE = "Trực tuyến"
OFFLINE = "Ngoại tuyến"
THIS_MACHINE = "Máy này"
SEP = " · "

SIDEBAR_WIDTH = 288
SIDEBAR_COMPACT_WIDTH = 80  # spec 12.5 (không phải 72)
COMPACT_BELOW = 820
MIN_ROW_HEIGHT = 56
ICON_BUTTON_SIZE = 32

NOW = datetime.datetime(2026, 10, 31, 10, 45).timestamp()  # giờ địa phương


# =================================================================================================
# Dữ liệu giả
# =================================================================================================


def device_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:32]


def dview(
    label: str = "PC-Nha",
    *,
    name: str | None = None,
    paired: bool = True,
    online: bool = True,
    connected: bool = False,
    os: str | None = "windows",
    did: str | None = None,
):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=did or device_id(label),
        name=label if name is None else name,
        os=os,
        paired=paired,
        online=online,
        connected=connected and paired,
        addresses=("192.0.2.7",) if online else (),
        port=47474 if online else None,
    )


def tkey(dev: str, direction: str = "send", tid: str = "a" * 32):
    from teleport.app.transfers import Direction, TransferKey

    return TransferKey(device_id=dev, direction=Direction(direction), transfer_id=tid)


def active(
    dev: str,
    status: str = "transferring",
    *,
    direction: str = "send",
    shown: int = 620,
    total: int = 1000,
    started: float = NOW - 60,
    tid: str = "a" * 32,
    count: int = 1,
    first: str | None = "video.mp4",
):
    from teleport.app.transfers import TransferStatus
    from teleport.ui.activity import ActiveTransfer

    return ActiveTransfer(
        key=tkey(dev, direction, tid),
        status=TransferStatus(status),
        file_count=count,
        first_name=first,
        shown_bytes=shown,
        total_bytes=total,
        started_at=started,
    )


def last(
    result: str | None = "done",
    *,
    direction: str = "send",
    count: int = 1,
    first: str | None = "bao-cao.pdf",
    at: float = NOW - 3600,
):
    from teleport.app.transfers import Direction, TransferResult
    from teleport.ui.activity import FinishedTransfer

    return FinishedTransfer(
        direction=Direction(direction),
        result=None if result is None else TransferResult(result),
        file_count=count,
        first_name=first,
        at=at,
    )


def activity(*actives, last_=None):
    from teleport.ui.activity import DeviceActivity

    return DeviceActivity(active=tuple(actives), last=last_)


def peer(name: str, did: str) -> str:
    from teleport.ui import texts

    return texts.peer_label(name, did)


def shout(text: str) -> str:
    return "".join(
        p if re.fullmatch(r"\{\w+\}|%n", p) else p.upper() for p in re.split(r"(\{\w+\}|%n)", text)
    )


class ShoutTranslator(QTranslator):
    def isEmpty(self):
        return False

    def translate(self, context, source_text, disambiguation=None, n=-1):
        return shout(source_text) if source_text else source_text


def settle(qtbot=None, ms: int = 0) -> None:
    for _ in range(3):
        QApplication.processEvents()
    if qtbot is not None and ms:
        qtbot.wait(ms)


class Conv:
    """Một `ConversationList` đứng riêng trong khung có nút trước/sau (để Tab ra vào)."""

    def __init__(self, qtbot) -> None:
        from teleport.ui.conversations import ConversationList
        from teleport.ui.radar import DeviceListModel

        self.host = QWidget()
        qtbot.addWidget(self.host)
        self.devices = DeviceListModel(self.host)
        self.before = QPushButton("Trước", self.host)
        self.widget = ConversationList(self.devices, self.host)
        self.after = QPushButton("Sau", self.host)
        lay = QVBoxLayout(self.host)
        lay.addWidget(self.before)
        lay.addWidget(self.widget)
        lay.addWidget(self.after)
        QWidget.setTabOrder(self.before, self.widget.first_focus_widget())
        QWidget.setTabOrder(self.widget.last_focus_widget(), self.after)
        self.host.resize(320, 700)
        self.picks: list[str] = []
        self.opens: list[str] = []
        self.sends: list[tuple[str, list[Path]]] = []
        self.unpairs: list[str] = []
        self.currents: list = []
        self.adds = 0
        self.hints: list[str] = []
        w = self.widget
        w.pick_files_requested.connect(self.picks.append)
        w.open_requested.connect(self.opens.append)  # spec B3 mục 6
        w.send_files_requested.connect(lambda i, p: self.sends.append((i, [Path(x) for x in p])))
        w.unpair_requested.connect(self.unpairs.append)
        w.current_changed.connect(self.currents.append)
        w.add_device_requested.connect(self._add)
        w.drop_hint_requested.connect(self.hints.append)

    def _add(self) -> None:
        self.adds += 1

    @property
    def model(self):
        return self.widget.model

    @property
    def lst(self):
        return self.widget.list_view

    def show(self, qtbot) -> None:
        self.host.show()
        qtbot.waitExposed(self.host)
        self.host.activateWindow()
        settle()

    def row_pos(self, row: int) -> QPoint:
        return self.lst.visualRect(self.model.index(row, 0)).center()

    def ids(self) -> list[str]:
        return [v.device_id for v in self.model.devices()]

    def selected_ids(self) -> list[str]:
        from teleport.ui.conversations import DEVICE_ID_ROLE

        return [i.data(DEVICE_ID_ROLE) for i in self.lst.selectionModel().selectedIndexes()]


@pytest.fixture
def conv(qtbot, monkeypatch):
    from teleport.ui import conversations

    monkeypatch.setattr(conversations, "_wall_now", lambda: NOW)
    return Conv(qtbot)


@pytest.fixture
def menus(monkeypatch):
    """Không mở menu thật: ghi lại `QMenu` được `popup`/`exec`."""
    from PySide6.QtWidgets import QMenu

    opened: list = []

    def fake(self, *args, **kwargs):
        opened.append(self)
        return None

    monkeypatch.setattr(QMenu, "popup", fake)
    monkeypatch.setattr(QMenu, "exec", fake)
    return opened


def menu_items(menu) -> list[str]:
    return [a.text().replace("&", "") for a in menu.actions() if not a.isSeparator() and a.isVisible()]


def menu_action(menu, text: str):
    [a] = [a for a in menu.actions() if a.text().replace("&", "") == text]
    return a


# =================================================================================================
# T-M4 `status_line`
# =================================================================================================

A = device_id("PC-Nha")


def _status(view, act=None, failure=None):
    from teleport.ui.activity import EMPTY_ACTIVITY
    from teleport.ui.conversations import status_line

    return status_line(view, act if act is not None else EMPTY_ACTIVITY, failure)


@pytest.mark.parametrize(
    ("status", "direction", "shown", "total", "expected"),
    [
        ("transferring", "send", 620, 1000, "Đang gửi · 62%"),
        ("transferring", "receive", 620, 1000, "Đang nhận · 62%"),
        ("transferring", "send", 0, 0, "Đang gửi"),
        ("transferring", "send", 1000, 1000, "Đang gửi · 99%"),
        ("transferring", "send", 999, 1000, "Đang gửi · 99%"),
        ("transferring", "send", 1, 1000, "Đang gửi · 0%"),
        ("verifying", "receive", 1000, 1000, "Đang kiểm tra"),
        ("reconnecting", "send", 620, 1000, "Đang nối lại · 62%"),
        ("reconnecting", "send", 0, 0, "Đang nối lại"),
        ("waiting", "receive", 0, 1000, "Đang chờ bạn trả lời"),
        ("waiting", "send", 0, 1000, "Đang chờ trả lời"),
        ("queued", "send", 0, 1000, "Đang chờ"),
    ],
)
def test_status_line_single_busy_transfer(status, direction, shown, total, expected):
    from teleport.ui.conversations import StatusTone
    from teleport.ui.widgets import Presence

    view = dview(online=False, os="linux")
    act = activity(
        active(view.device_id, status, direction=direction, shown=shown, total=total), last_=last()
    )
    presence, tone, text = _status(view, act)
    assert presence is Presence.BUSY
    assert tone is StatusTone.BUSY
    assert text == expected, "máy bận: không nối hoạt động gần nhất hay HĐH"


@pytest.mark.parametrize("n", [2, 3])
def test_status_line_many_busy_transfers(n):
    from teleport.ui.conversations import StatusTone
    from teleport.ui.widgets import Presence

    view = dview()
    acts = [active(view.device_id, tid=f"{i:032x}", started=NOW - i) for i in range(n)]
    presence, tone, text = _status(view, activity(*acts))
    assert (presence, tone) == (Presence.BUSY, StatusTone.BUSY)
    assert text == f"Đang truyền {n} lượt"


def test_status_line_busy_wins_over_connect_failure():
    from teleport.core.links import ConnectResult
    from teleport.ui.widgets import Presence

    view = dview()
    presence, _tone, text = _status(view, activity(active(view.device_id)), ConnectResult.REJECTED)
    assert presence is Presence.BUSY
    assert text == "Đang gửi · 62%"


@pytest.mark.parametrize(
    ("result", "tone", "label"),
    [
        ("rejected", "DANGER", "Bị từ chối kết nối"),
        ("identity_mismatch", "DANGER", "Danh tính đã đổi"),
        ("version_mismatch", "DANGER", "Khác phiên bản"),
        ("unreachable", "WARNING", "Không kết nối được"),
    ],
)
@pytest.mark.parametrize("online", [True, False])
def test_status_line_connect_failure(result, tone, label, online):
    from teleport.core.links import ConnectResult
    from teleport.ui.conversations import StatusTone
    from teleport.ui.widgets import Presence

    view = dview(online=online)
    presence, got_tone, text = _status(view, None, ConnectResult(result))
    assert presence is (Presence.ONLINE if online else Presence.OFFLINE)
    assert got_tone is StatusTone[tone]
    assert text.split(SEP)[0] == label, "chữ trạng thái luôn đứng đầu"


@pytest.mark.parametrize(
    ("online", "connected", "word"), [(True, False, ONLINE), (False, True, ONLINE), (False, False, OFFLINE)]
)
def test_status_line_presence_with_last_activity(online, connected, word):
    from teleport.ui.conversations import StatusTone
    from teleport.ui.widgets import Presence

    view = dview(online=online, connected=connected)
    presence, tone, text = _status(view, activity(last_=last("done", first="bao-cao.pdf")))
    assert presence is (Presence.ONLINE if word == ONLINE else Presence.OFFLINE)
    assert tone is StatusTone.NORMAL
    assert text == f"{word} · Đã gửi bao-cao.pdf"


@pytest.mark.parametrize(
    ("os_value", "suffix"),
    [("windows", " · Windows"), ("macos", " · macOS"), ("linux", " · Linux"), (None, ""), ("beos", "")],
)
def test_status_line_without_activity_shows_known_os(os_value, suffix):
    view = dview(os=os_value)
    assert _status(view)[2] == ONLINE + suffix
    off = dview(online=False, os=os_value)
    assert _status(off)[2] == OFFLINE + suffix


# =================================================================================================
# T-M5 `activity_text`
# =================================================================================================

_REJECTED = [
    "rejected_user",
    "rejected_no_space",
    "rejected_file_too_large",
    "rejected_busy",
    "rejected_other",
    "rejected_not_receiving",
]
_CANCELLED = ["cancelled_user", "cancelled_other", "unknown_transfer", "unpaired"]
_FAILED = [
    "source_changed",
    "expired",
    "disk_failed",
    "temp_exists",
    "receive_dir_missing",
    "corrupt",
    "protocol_error",
    "source_read_failed",
    "peer_error",
    None,
]


def _activity_cases():
    cases = [("done", "send", "Đã gửi {what}"), ("done", "receive", "Đã nhận {what}")]
    cases += [(r, "send", "Bị từ chối · {what}") for r in _REJECTED]
    cases += [(r, "receive", "Đã từ chối · {what}") for r in _REJECTED]
    cases += [(r, d, "Đã hủy · {what}") for r in _CANCELLED for d in ("send", "receive")]
    cases += [(r, "send", "Lỗi khi gửi · {what}") for r in _FAILED]
    cases += [(r, "receive", "Lỗi khi nhận · {what}") for r in _FAILED]
    return cases


@pytest.mark.parametrize(("result", "direction", "template"), _activity_cases())
def test_activity_text_table(result, direction, template):
    from teleport.ui.conversations import activity_text

    one = activity_text(last(result, direction=direction, count=1, first="bao-cao.pdf"))
    assert one == template.replace("{what}", "bao-cao.pdf")
    many = activity_text(last(result, direction=direction, count=3, first="bao-cao.pdf"))
    assert many == template.replace("{what}", "3 file")


def test_activity_text_single_file_without_name_counts_files():
    """Spec 12.10: `HistoryEntry.names` có thể rỗng."""
    from teleport.ui.conversations import activity_text

    assert activity_text(last("done", count=1, first=None)) == "Đã gửi 1 file"


def test_activity_text_file_name_goes_through_file_label():
    from teleport.ui import texts
    from teleport.ui.conversations import activity_text

    name = "a‮gnp.exe"
    assert activity_text(last("done", first=name)) == "Đã gửi " + texts.file_label(name)


# =================================================================================================
# T-M6 `time_text`
# =================================================================================================


def _ts(*args) -> float:
    return datetime.datetime(*args).timestamp()


@pytest.mark.parametrize(
    ("at", "now", "expected"),
    [
        (_ts(2026, 10, 31, 10, 42), NOW, "10:42"),
        (_ts(2026, 10, 31, 9, 5), NOW, "09:05"),
        (_ts(2026, 10, 31, 0, 0), NOW, "00:00"),
        (_ts(2026, 10, 30, 23, 59), NOW, "Hôm qua"),
        (_ts(2026, 10, 30, 0, 1), NOW, "Hôm qua"),
        (_ts(2026, 10, 28, 9, 0), NOW, "28/10"),
        (_ts(2026, 1, 2, 9, 0), NOW, "02/01"),
        (_ts(2025, 10, 28, 9, 0), NOW, "28/10/2025"),
        (_ts(2025, 12, 31, 22, 0), _ts(2026, 1, 1, 8, 0), "Hôm qua"),
        (_ts(2026, 11, 2, 8, 0), NOW, "02/11"),  # 12.11: ngày sau hôm nay
        (_ts(2027, 1, 5, 8, 0), NOW, "05/01/2027"),
    ],
)
def test_time_text_cases(at, now, expected):
    from teleport.ui.conversations import time_text

    assert time_text(at, now) == expected


@pytest.mark.parametrize("at", [None, 1e20, -1e20, float("inf"), float("nan")])
def test_time_text_none_or_unrepresentable_is_empty(at):
    from teleport.ui.conversations import time_text

    assert time_text(at, NOW) == ""


# =================================================================================================
# T-M1, M2, M3: lọc, sắp, set_activity
# =================================================================================================


def test_model_shows_only_paired_devices(conv):
    a, b = dview("May-A"), dview("May-B", online=False, os=None)
    u = dview("May-U", paired=False)
    conv.devices.reset([a, u, b])
    assert sorted(conv.ids()) == sorted([a.device_id, b.device_id])
    assert conv.model.rowCount() == 2
    assert conv.model.device(u.device_id) is None, "device(): chỉ máy trong danh sách"
    assert conv.model.row_of(u.device_id) == -1
    assert conv.model.device(a.device_id) == a
    conv.devices.reset([dview("May-A", paired=False), u, b])
    assert conv.ids() == [b.device_id], "reset với paired=False → bỏ hàng"


def test_model_source_and_dynamic_sort(conv):
    assert conv.model.sourceModel() is conv.devices
    assert conv.model.dynamicSortFilter()


def test_model_order_busy_then_online_then_offline_then_recent(conv):
    busy_off = dview("Busy-Off", online=False, os=None)
    on_recent = dview("On-Recent")
    on_old = dview("On-Old")
    on_none = dview("On-None")
    conn = dview("Conn", online=False, connected=True)
    off_recent = dview("Off-Recent", online=False, os=None)
    off_none = dview("Off-None", online=False, os=None)
    views = [off_none, on_none, conn, off_recent, on_old, busy_off, on_recent]
    conv.devices.reset(views)
    m = conv.model
    m.set_activity(busy_off.device_id, activity(active(busy_off.device_id, started=NOW - 9000)))
    m.set_activity(on_recent.device_id, activity(last_=last(at=NOW - 10)))
    m.set_activity(on_old.device_id, activity(last_=last(at=NOW - 5000)))
    m.set_activity(conn.device_id, activity(last_=last(at=NOW - 100)))
    m.set_activity(off_recent.device_id, activity(last_=last(at=NOW - 20)))
    expected = [busy_off, on_recent, conn, on_old, on_none, off_recent, off_none]
    assert conv.ids() == [v.device_id for v in expected]


def test_model_ties_sort_by_nfc_casefold_name_then_id(conv):
    import unicodedata

    nfd = unicodedata.normalize("NFD", "Ánh")
    x1 = dview("x1", name="beta", did="1" * 32)
    x2 = dview("x2", name="Alpha", did="2" * 32)
    x3 = dview("x3", name=nfd, did="3" * 32)
    x4 = dview("x4", name="alpha", did="0" * 32)
    conv.devices.reset([x1, x2, x3, x4])
    # “alpha” và “Alpha” bằng nhau sau casefold → theo device_id; tên NFD so như NFC.
    keys = sorted(
        [x1, x2, x3, x4], key=lambda v: (unicodedata.normalize("NFC", v.name).casefold(), v.device_id)
    )
    assert conv.ids() == [v.device_id for v in keys]
    assert conv.ids()[:2] == [x4.device_id, x2.device_id]


def test_conversation_sort_key_shape():
    from teleport.ui.activity import EMPTY_ACTIVITY
    from teleport.ui.conversations import conversation_sort_key

    busy = conversation_sort_key(dview(online=False), activity(active(A)))
    online = conversation_sort_key(dview(), EMPTY_ACTIVITY)
    offline = conversation_sort_key(dview(online=False), activity(last_=last(at=NOW)))
    assert busy < online < offline


class _Signals:
    def __init__(self, model) -> None:
        self.data: list[tuple[int, int]] = []
        self.layout = 0
        self.reset = 0
        model.dataChanged.connect(lambda tl, br, *_: self.data.append((tl.row(), br.row())))
        model.layoutChanged.connect(self._layout)
        model.modelReset.connect(self._reset)

    def _layout(self, *_):
        self.layout += 1

    def _reset(self):
        self.reset += 1


def test_set_activity_percent_change_emits_one_data_changed_for_that_row(conv):
    a, b = dview("May-A"), dview("May-B")
    conv.devices.reset([a, b])
    m = conv.model
    m.set_activity(a.device_id, activity(active(a.device_id, shown=100, started=NOW - 50)))
    m.set_activity(b.device_id, activity(active(b.device_id, shown=100, started=NOW - 40)))
    spy = _Signals(m)
    m.set_activity(a.device_id, activity(active(a.device_id, shown=620, started=NOW - 50)))
    row = m.row_of(a.device_id)
    assert spy.data == [(row, row)]
    assert spy.layout == 0 and spy.reset == 0
    from teleport.ui.conversations import STATUS_ROLE

    assert m.index(row, 0).data(STATUS_ROLE) == "Đang gửi · 62%"


def test_set_activity_busy_change_reorders_with_layout_changed(conv):
    a, b = dview("May-A"), dview("May-B")
    conv.devices.reset([a, b])
    assert conv.ids() == [a.device_id, b.device_id]
    spy = _Signals(conv.model)
    conv.model.set_activity(b.device_id, activity(active(b.device_id)))
    assert spy.layout >= 1
    assert spy.reset == 0
    assert conv.ids() == [b.device_id, a.device_id]


def test_set_activity_without_change_emits_nothing(conv):
    a = dview("May-A")
    conv.devices.reset([a])
    act = activity(active(a.device_id), last_=last())
    conv.model.set_activity(a.device_id, act)
    spy = _Signals(conv.model)
    conv.model.set_activity(a.device_id, activity(active(a.device_id), last_=last()))
    assert spy.data == [] and spy.layout == 0 and spy.reset == 0
    assert conv.model.activity(a.device_id) == act


def test_clear_activity_forgets_everything(conv):
    from teleport.ui.activity import EMPTY_ACTIVITY

    a, b = dview("May-A"), dview("May-B")
    conv.devices.reset([a, b])
    conv.model.set_activity(b.device_id, activity(active(b.device_id)))
    assert conv.ids()[0] == b.device_id
    conv.model.clear_activity()
    assert conv.model.activity(b.device_id) == EMPTY_ACTIVITY
    assert conv.ids() == [a.device_id, b.device_id]


# =================================================================================================
# T-M7 role, T-M8 đổi ngôn ngữ
# =================================================================================================


def test_roles_of_a_row(conv):
    from teleport.ui import widgets
    from teleport.ui.conversations import (
        DEVICE_ID_ROLE,
        DEVICE_ROLE,
        PRESENCE_ROLE,
        STATUS_ROLE,
        TIME_ROLE,
        TONE_ROLE,
        StatusTone,
    )

    a = dview("PC-Nha")
    conv.devices.reset([a])
    conv.model.set_activity(a.device_id, activity(last_=last(at=NOW - 180)))
    idx = conv.model.index(0, 0)
    status = "Trực tuyến · Đã gửi bao-cao.pdf"
    when = datetime.datetime.fromtimestamp(NOW - 180).strftime("%H:%M")
    assert idx.data(Qt.ItemDataRole.DisplayRole) == peer("PC-Nha", a.device_id)
    assert idx.data(DEVICE_ROLE) == a
    assert idx.data(DEVICE_ID_ROLE) == a.device_id
    assert idx.data(PRESENCE_ROLE) == widgets.Presence.ONLINE  # StrEnum qua QVariant thành str
    assert idx.data(TONE_ROLE) == StatusTone.NORMAL
    assert idx.data(STATUS_ROLE) == status
    assert idx.data(TIME_ROLE) == when
    assert idx.data(Qt.ItemDataRole.AccessibleTextRole) == f"PC-Nha. {status}. {when}"
    description = idx.data(Qt.ItemDataRole.AccessibleDescriptionRole) or ""
    assert "Windows" in description
    tip = idx.data(Qt.ItemDataRole.ToolTipRole)
    assert tip.startswith("<qt>")


def test_accessible_text_without_time_has_no_trailing_part(conv):
    a = dview("PC-Nha")
    conv.devices.reset([a])
    assert conv.model.index(0, 0).data(Qt.ItemDataRole.AccessibleTextRole) == "PC-Nha. Trực tuyến · Windows"


def test_hostile_name_goes_through_peer_label_and_safe_tooltip(conv):
    from teleport.ui.conversations import STATUS_ROLE

    hostile = dview("x", name="<b>x</b>\x07")
    conv.devices.reset([hostile])
    idx = conv.model.index(0, 0)
    shown = peer("<b>x</b>\x07", hostile.device_id)
    assert idx.data(Qt.ItemDataRole.DisplayRole) == shown
    assert "\x07" not in shown
    tip = idx.data(Qt.ItemDataRole.ToolTipRole)
    assert "<b>x</b>" not in tip
    assert "&lt;b&gt;x&lt;/b&gt;" in tip
    assert idx.data(Qt.ItemDataRole.AccessibleTextRole).startswith(shown + ". ")
    assert idx.data(STATUS_ROLE).startswith(ONLINE)


def test_accessible_description_has_full_failure_sentence_and_count(conv):
    from teleport.app.node import ConnectFailed
    from teleport.core.links import ConnectResult
    from teleport.ui.radar import connect_failed_text

    a = dview("PC-Nha")
    conv.devices.reset([a])
    conv.devices.apply(ConnectFailed(device=a, result=ConnectResult.REJECTED))
    idx = conv.model.index(conv.model.row_of(a.device_id), 0)
    description = idx.data(Qt.ItemDataRole.AccessibleDescriptionRole)
    assert connect_failed_text(a, ConnectResult.REJECTED) in description
    from teleport.ui.conversations import FAILURE_ROLE

    assert idx.data(FAILURE_ROLE) == ConnectResult.REJECTED  # StrEnum qua QVariant thành str
    conv.model.set_activity(
        a.device_id, activity(*[active(a.device_id, tid=f"{i:032x}", started=NOW - i) for i in range(3)])
    )
    description = conv.model.index(conv.model.row_of(a.device_id), 0).data(
        Qt.ItemDataRole.AccessibleDescriptionRole
    )
    assert "3" in description


@pytest.mark.parametrize(
    ("result", "label", "tone"),
    [
        ("rejected", "Bị từ chối kết nối", "DANGER"),
        ("identity_mismatch", "Danh tính đã đổi", "DANGER"),
        ("version_mismatch", "Khác phiên bản", "DANGER"),
        ("unreachable", "Không kết nối được", "WARNING"),
    ],
)
def test_row_status_shows_connect_failure_from_device_model(conv, result, label, tone):
    """Lỗi kết nối đi từ `DeviceListModel` (FAILURE_ROLE của nguồn) tới dòng phụ của hàng."""
    from teleport.app.node import ConnectFailed
    from teleport.core.links import ConnectResult
    from teleport.ui.conversations import STATUS_ROLE, TONE_ROLE, StatusTone

    a = dview("PC-Nha")
    conv.devices.reset([a])
    conv.devices.apply(ConnectFailed(device=a, result=ConnectResult(result)))
    idx = conv.model.index(conv.model.row_of(a.device_id), 0)
    assert idx.data(STATUS_ROLE).split(SEP)[0] == label
    assert idx.data(TONE_ROLE) == StatusTone[tone]


def test_retranslate_rebuilds_status_text(conv, qapp):
    from teleport.ui.conversations import STATUS_ROLE

    a = dview("PC-Nha")
    conv.devices.reset([a])
    spy = _Signals(conv.model)
    translator = ShoutTranslator()
    qapp.installTranslator(translator)
    try:
        settle()
        status = conv.model.index(0, 0).data(STATUS_ROLE)
        assert status.startswith(shout(ONLINE)), status
        assert spy.data or spy.layout or spy.reset, "đổi ngôn ngữ phải báo view vẽ lại"
        assert conv.widget.add_button.text().replace("&", "") == shout(ADD_DEVICE)
        assert conv.lst.accessibleName() == shout(LIST_NAME)
    finally:
        qapp.removeTranslator(translator)
        settle()
    assert conv.model.index(0, 0).data(STATUS_ROLE).startswith(ONLINE)


# =================================================================================================
# T-V: cấu trúc
# =================================================================================================


def test_list_structure_and_names(conv):
    from PySide6.QtWidgets import QAbstractItemView, QFrame

    w, lst = conv.widget, conv.lst
    assert w.objectName() == "conversations"
    assert lst.objectName() == "conversationList"
    assert w.first_focus_widget() is lst
    assert w.last_focus_widget() is w.add_button
    assert w.add_button.objectName() == "addDeviceButton"
    assert w.add_button.text().replace("&", "") == ADD_DEVICE
    assert lst.accessibleName() == LIST_NAME
    assert lst.accessibleDescription() == LIST_DESCRIPTION
    assert lst.selectionMode() is QAbstractItemView.SelectionMode.SingleSelection
    assert lst.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert lst.verticalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert lst.horizontalScrollBarPolicy() is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert lst.frameShape() is QFrame.Shape.NoFrame
    assert lst.spacing() == 0, "12.3: khe giữa hàng nằm trong sizeHint của delegate"
    assert not lst.tabKeyNavigation(), "12.7"
    assert lst.uniformItemSizes()
    assert lst.model() is w.model
    assert lst.itemDelegate() is w.delegate


def test_list_height_is_fixed_to_its_rows(conv, qtbot):
    conv.devices.reset([dview(f"May-{i}") for i in range(5)])
    conv.show(qtbot)
    lst = conv.lst
    expected = 5 * conv.widget.delegate.row_height() + 2 * lst.frameWidth()
    assert lst.height() == expected
    assert lst.verticalScrollBar().maximum() == 0
    conv.devices.reset([dview(f"May-{i}") for i in range(2)])
    settle()
    assert lst.height() == 2 * conv.widget.delegate.row_height() + 2 * lst.frameWidth()


def test_row_height_has_minimum(conv):
    from teleport.ui import conversations

    assert conversations.MIN_ROW_HEIGHT == MIN_ROW_HEIGHT
    assert conv.widget.delegate.row_height() >= MIN_ROW_HEIGHT


# =================================================================================================
# T-V1 bấm, bấm đúp, Enter
# =================================================================================================

DCLICK_WAYS = ("qtest_click_then_dclick", "manual_press_dblclick", "manual_dblclick_without_second_press")


def _send_mouse(lst, kind, pos, button, buttons) -> None:
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent

    vp = lst.viewport()
    ev = QMouseEvent(
        kind, QPointF(pos), QPointF(vp.mapToGlobal(pos)), button, buttons, Qt.KeyboardModifier.NoModifier
    )
    QApplication.sendEvent(vp, ev)


def _double_click(lst, pos, way: str) -> None:
    left, none = Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton
    if way == "qtest_click_then_dclick":
        QTest.mouseClick(lst.viewport(), left, Qt.KeyboardModifier.NoModifier, pos)
        QTest.mouseDClick(lst.viewport(), left, Qt.KeyboardModifier.NoModifier, pos)
    else:
        t = QEvent.Type
        seq = [(t.MouseButtonPress, left), (t.MouseButtonRelease, none)]
        if way == "manual_press_dblclick":
            seq.append((t.MouseButtonPress, left))
        seq += [(t.MouseButtonDblClick, left), (t.MouseButtonRelease, none)]
        for kind, buttons in seq:
            _send_mouse(lst, kind, pos, left, buttons)
    QApplication.processEvents()


def test_single_click_selects_and_reports_current_once(conv, qtbot):
    a, b = dview("May-A"), dview("May-B")
    conv.devices.reset([a, b])
    conv.show(qtbot)
    conv.currents.clear()
    row = conv.model.row_of(b.device_id)
    QTest.mouseClick(
        conv.lst.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, conv.row_pos(row)
    )
    settle(qtbot, 60)
    assert conv.selected_ids() == [b.device_id]
    assert conv.widget.current_device() == b
    assert conv.currents == [b.device_id]
    assert conv.picks == []


@pytest.mark.parametrize("way", DCLICK_WAYS)
def test_double_click_opens_conversation_exactly_once(conv, qtbot, way):
    # Spec B3 mục 6: bấm đúp phát `open_requested` thay `pick_files_requested`.
    a, b = dview("May-A"), dview("May-B", online=False)
    conv.devices.reset([a, b])
    conv.show(qtbot)
    _double_click(conv.lst, conv.row_pos(conv.model.row_of(b.device_id)), way)
    qtbot.waitUntil(lambda: bool(conv.opens), timeout=2000)
    settle(qtbot, 40)
    assert conv.opens == [b.device_id], f"{way}: {conv.opens}"
    assert conv.picks == []
    assert conv.selected_ids() == [b.device_id]
    assert conv.unpairs == [] and conv.sends == []


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_enter_opens_conversation_once(conv, qtbot, key):
    # Spec B3 mục 6: Enter phát `open_requested` thay `pick_files_requested`.
    a, b = dview("May-A"), dview("May-B")
    conv.devices.reset([a, b])
    conv.show(qtbot)
    conv.widget.select_device(b.device_id)
    conv.lst.setFocus(Qt.FocusReason.OtherFocusReason)
    QTest.keyClick(conv.lst, key)
    settle(qtbot, 30)
    assert conv.opens == [b.device_id]
    QTest.keyClick(conv.lst, key)
    settle(qtbot, 30)
    assert conv.opens == [b.device_id] * 2, "mỗi lần nhấn thật một lần"
    assert conv.picks == []


def test_enter_auto_repeat_does_not_emit_again(conv, qtbot):
    d = dview("May-D")
    conv.devices.reset([d])
    conv.show(qtbot)
    conv.widget.select_device(d.device_id)
    lst = conv.lst
    lst.setFocus(Qt.FocusReason.OtherFocusReason)
    press, release = QEvent.Type.KeyPress, QEvent.Type.KeyRelease
    ret, nomod = Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier
    QApplication.sendEvent(lst, QKeyEvent(press, ret, nomod, "\r", False))
    for _ in range(3):
        QApplication.sendEvent(lst, QKeyEvent(press, ret, nomod, "", True))
    QApplication.sendEvent(lst, QKeyEvent(release, ret, nomod, "", True))
    QApplication.sendEvent(lst, QKeyEvent(press, ret, nomod, "", True))
    QApplication.sendEvent(lst, QKeyEvent(release, ret, nomod, "\r", False))
    settle(qtbot, 30)
    assert conv.opens == [d.device_id]  # spec B3 mục 6: Enter → `open_requested`
    assert conv.picks == []


# =================================================================================================
# T-V2 menu
# =================================================================================================


def test_context_menu_items_and_accessible_name(conv):
    a = dview("PC-Nha")
    conv.devices.reset([a])
    menu = conv.widget.context_menu_for(a.device_id)
    assert menu is not None
    assert menu_items(menu) == [SEND_FILES, UNPAIR]
    assert menu.accessibleName() == MENU_NAME.replace("{name}", peer("PC-Nha", a.device_id))
    menu_action(menu, SEND_FILES).trigger()
    assert conv.picks == [a.device_id] and conv.unpairs == []
    menu = conv.widget.context_menu_for(a.device_id)
    menu_action(menu, UNPAIR).trigger()
    assert conv.unpairs == [a.device_id] and conv.picks == [a.device_id]


def test_context_menu_for_unknown_or_unpaired_device_is_none(conv):
    u = dview("May-U", paired=False)
    conv.devices.reset([dview("May-A"), u])
    assert conv.widget.context_menu_for(u.device_id) is None
    assert conv.widget.context_menu_for("f" * 32) is None


@pytest.mark.parametrize("how", ["right_click", "menu_key", "shift_f10"])
def test_menu_opens_by_mouse_menu_key_and_shift_f10(conv, qtbot, menus, how):
    from PySide6.QtGui import QContextMenuEvent

    a, b = dview("May-A"), dview("May-B")
    conv.devices.reset([a, b])
    conv.show(qtbot)
    row = conv.model.row_of(b.device_id)
    lst = conv.lst
    if how == "right_click":
        pos = conv.row_pos(row)
        QTest.mouseClick(lst.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pos)
        ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, lst.viewport().mapToGlobal(pos))
        QApplication.sendEvent(lst.viewport(), ev)
    else:
        conv.widget.select_device(b.device_id)
        lst.setFocus(Qt.FocusReason.OtherFocusReason)
        if how == "menu_key":
            QTest.keyClick(lst, Qt.Key.Key_Menu)
        else:
            QTest.keyClick(lst, Qt.Key.Key_F10, Qt.KeyboardModifier.ShiftModifier)
    settle(qtbot, 30)
    assert menus, f"{how}: không mở menu"
    menu = conv.widget.last_menu
    assert menu is not None
    assert menu_items(menu) == [SEND_FILES, UNPAIR]
    assert menu.accessibleName() == MENU_NAME.replace("{name}", "May-B")
    assert conv.picks == [] and conv.unpairs == [], "chỉ mở menu thì không phát gì"


# =================================================================================================
# T-V3 Tab / mũi tên
# =================================================================================================


def test_tab_and_shift_tab_visit_each_row_then_add_button(conv, qtbot):
    views = [dview(f"May-{i}") for i in range(4)]
    conv.devices.reset(views)
    conv.show(qtbot)
    qtbot.waitUntil(conv.host.isActiveWindow, timeout=3000)
    conv.before.setFocus(Qt.FocusReason.OtherFocusReason)
    settle()
    rows = []
    for _ in range(4):
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
        settle()
        assert QApplication.focusWidget() is conv.lst
        rows.append(conv.lst.currentIndex().row())
    assert rows == [0, 1, 2, 3]
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
    settle()
    assert QApplication.focusWidget() is conv.widget.add_button
    back = []
    for _ in range(4):
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
        settle()
        assert QApplication.focusWidget() is conv.lst
        back.append(conv.lst.currentIndex().row())
    assert back == [3, 2, 1, 0]
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    settle()
    assert QApplication.focusWidget() is conv.before


def test_tab_through_rows_keeps_selected_device(conv, qtbot):
    """Tab chỉ dời hàng hiện hành; máy được chọn (cuộc trò chuyện đang mở) không đổi (soat-code B2)."""
    views = [dview(f"May-{i}") for i in range(3)]
    conv.devices.reset(views)
    conv.show(qtbot)
    qtbot.waitUntil(conv.host.isActiveWindow, timeout=3000)
    chosen = conv.ids()[2]
    assert conv.widget.select_device(chosen)
    conv.currents.clear()
    conv.before.setFocus(Qt.FocusReason.OtherFocusReason)
    settle()
    for _ in range(3):
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
        settle()
        assert conv.selected_ids() == [chosen]
    assert conv.currents == []
    assert conv.widget.current_device().device_id == chosen


def test_arrow_keys_visit_every_row(conv, qtbot):
    conv.devices.reset([dview(f"May-{i}") for i in range(4)])
    conv.show(qtbot)
    conv.lst.setFocus(Qt.FocusReason.OtherFocusReason)
    conv.lst.setCurrentIndex(conv.model.index(0, 0))
    seen = [conv.lst.currentIndex().row()]
    for _ in range(3):
        QTest.keyClick(conv.lst, Qt.Key.Key_Down)
        seen.append(conv.lst.currentIndex().row())
    assert seen == [0, 1, 2, 3]


# =================================================================================================
# T-V4 giữ lựa chọn
# =================================================================================================


def test_reset_keeps_selected_device(conv):
    a, b, c = dview("May-B"), dview("May-C"), dview("May-D")
    conv.devices.reset([a, b, c])
    assert conv.widget.select_device(c.device_id)
    conv.currents.clear()
    conv.devices.reset([dview("May-A"), a, b, c])  # thêm một hàng phía trên
    assert conv.selected_ids() == [c.device_id]
    assert conv.widget.current_device() == c
    assert conv.currents == [], "không đổi thật thì không phát current_changed"


def test_reorder_by_activity_keeps_selected_device(conv):
    a, b, c = dview("May-A"), dview("May-B"), dview("May-C")
    conv.devices.reset([a, b, c])
    conv.widget.select_device(b.device_id)
    conv.currents.clear()
    conv.model.set_activity(c.device_id, activity(active(c.device_id)))
    assert conv.ids()[0] == c.device_id
    assert conv.selected_ids() == [b.device_id]
    assert conv.lst.currentIndex().data(Qt.ItemDataRole.DisplayRole) == "May-B"
    assert conv.currents == []


def test_rename_keeps_selected_device(conv):
    from teleport.app.node import DeviceOnline

    a, m, z = dview("May-A"), dview("May-M"), dview("May-Z")
    conv.devices.reset([a, m, z])
    conv.widget.select_device(m.device_id)
    conv.devices.apply(DeviceOnline(device=dview("May-M", name="Zzz-Moi")))
    assert conv.ids()[-1] == m.device_id, "tiền đề: đổi tên làm hàng dời chỗ"
    assert conv.selected_ids() == [m.device_id]


# Spec B3 mục 6 (đổi luật B2 V4 / mục 2.6): luôn có một cuộc trò chuyện được chọn khi danh sách có hàng.


def test_removing_selected_row_selects_another(conv):
    a, b, c = dview("May-A"), dview("May-B"), dview("May-C")
    conv.devices.reset([a, b, c])
    conv.widget.select_device(b.device_id)
    conv.currents.clear()
    conv.devices.reset([a, c])
    selected = conv.selected_ids()
    assert len(selected) == 1 and selected[0] in (a.device_id, c.device_id)
    assert conv.widget.current_device().device_id == selected[0]
    assert conv.currents and conv.currents[-1] == selected[0]


def test_unpaired_selected_row_selects_remaining_row(conv):
    a, b = dview("May-A"), dview("May-B")
    conv.devices.reset([a, b])
    conv.widget.select_device(b.device_id)
    conv.currents.clear()
    conv.devices.reset([a, dview("May-B", paired=False)])
    assert conv.selected_ids() == [a.device_id]
    assert conv.currents and conv.currents[-1] == a.device_id


def test_rows_without_selection_select_row_zero(conv):
    conv.devices.reset([dview("May-A"), dview("May-B")])
    settle()
    assert conv.lst.currentIndex().row() == 0
    first = conv.model.index(0, 0).data(Qt.ItemDataRole.DisplayRole)
    assert conv.selected_ids() == [device_id(first)]
    assert conv.widget.current_device().device_id == device_id(first)
    assert conv.currents == [device_id(first)]


def test_select_device_unknown_returns_false(conv):
    conv.devices.reset([dview("May-A"), dview("May-U", paired=False)])
    assert not conv.widget.select_device("f" * 32)
    assert not conv.widget.select_device(device_id("May-U"))
    # Máy lạ / chưa ghép đôi không đổi lựa chọn; hàng duy nhất đã tự được chọn.
    assert conv.widget.current_device().device_id == device_id("May-A")


def test_current_device_is_the_selected_one_not_just_current_row(conv):
    a, b = dview("May-A"), dview("May-B")
    conv.devices.reset([a, b])
    assert conv.widget.current_device() == a  # hàng 0 tự được chọn
    conv.lst.selectionModel().setCurrentIndex(
        conv.model.index(1, 0), QItemSelectionModel.SelectionFlag.NoUpdate
    )
    assert conv.widget.current_device() == a, "dời hàng hiện hành (Tab) không đổi máy được chọn"
    conv.widget.select_device(b.device_id)
    assert conv.widget.current_device() == b


# =================================================================================================
# T-V5 tên độc
# =================================================================================================

TOXIC = ["<b>x</b>", "a\x00b\x1b[31mc\x07", "‮exe.gnp", "漢" * 40, "x" * 300, ""]


def test_toxic_names_keep_row_height_and_plain_text(conv, qtbot):
    views = [dview(f"t{i}", name=n) for i, n in enumerate(TOXIC)]
    views.append(dview("normal"))
    conv.devices.reset(views)
    long_file = "a" * 251 + ".txt"
    assert len(long_file) == 255
    first = views[0].device_id
    conv.model.set_activity(first, activity(last_=last(first=long_file)))
    conv.model.set_activity(views[1].device_id, activity(active(views[1].device_id, first=long_file)))
    conv.show(qtbot)
    delegate = conv.widget.delegate
    heights = {
        delegate.sizeHint(QStyleOptionViewItem(), conv.model.index(r, 0)).height()
        for r in range(conv.model.rowCount())
    }
    assert heights == {delegate.row_height()}
    for r in range(conv.model.rowCount()):
        idx = conv.model.index(r, 0)
        did = conv.model.devices()[r].device_id
        view = conv.model.device(did)
        assert idx.data(Qt.ItemDataRole.DisplayRole) == peer(view.name, did)
        tip = idx.data(Qt.ItemDataRole.ToolTipRole)
        assert "<b>" not in tip.replace("<qt>", "")
    assert not conv.host.grab().isNull()


# =================================================================================================
# T-V6 màu (MainWindow, sáng + tối)
# =================================================================================================

MODES = pytest.mark.parametrize("dark", [False, True], ids=["sang", "toi"])


@pytest.fixture
def make_window(qtbot, monkeypatch):
    from teleport.ui import conversations

    monkeypatch.setattr(conversations, "_wall_now", lambda: NOW)

    def make():
        from teleport.ui.main_window import MainWindow

        w = MainWindow()
        qtbot.addWidget(w)
        return w

    return make


@pytest.fixture
def window(make_window):
    return make_window()


def show_window(window, qtbot, width: int = 980, height: int = 660, *, active_: bool = False) -> None:
    if not active_:
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.resize(width, height)
    window.show()
    qtbot.waitExposed(window)
    if active_:
        window.activateWindow()
        window.raise_()
        qtbot.waitUntil(window.isActiveWindow, timeout=3000)
    settle()


def resize_to(window, width: int, height: int) -> None:
    window.resize(width, height)
    settle()


def running(window, tmp_path: Path, name: str = "May-A") -> None:
    window.show_running(name, tmp_path / "nhan")
    settle()


def row_rect_in_window(window, row: int) -> QRect:
    lst = window.conversations.list_view
    rect = lst.visualRect(lst.model().index(row, 0))
    top_left = lst.viewport().mapTo(window, rect.topLeft())
    return QRect(top_left, rect.size())


def _close(a: QColor, b: QColor, tol: int) -> bool:
    return (
        abs(a.red() - b.red()) <= tol
        and abs(a.green() - b.green()) <= tol
        and abs(a.blue() - b.blue()) <= tol
    )


def pixels(image, rect: QRect):
    dpr = image.devicePixelRatio()
    for y in range(round(rect.top() * dpr), round((rect.bottom() + 1) * dpr)):
        for x in range(round(rect.left() * dpr), round((rect.right() + 1) * dpr)):
            if 0 <= x < image.width() and 0 <= y < image.height():
                yield image.pixelColor(x, y)


def has_color(image, rect: QRect, color: QColor, tol: int = 12) -> bool:
    return any(_close(px, color, tol) for px in pixels(image, rect))


def has_blend(image, rect: QRect, bg: QColor, fg: QColor, *, at_least: float = 0.5, tol: int = 10) -> bool:
    """Có điểm ảnh nằm trên đoạn pha nền → `fg`, đậm ít nhất `at_least` (chữ khử răng cưa)."""
    span = [fg.red() - bg.red(), fg.green() - bg.green(), fg.blue() - bg.blue()]
    norm = sum(c * c for c in span)
    for px in pixels(image, rect):
        d = [px.red() - bg.red(), px.green() - bg.green(), px.blue() - bg.blue()]
        t = sum(a * b for a, b in zip(d, span, strict=True)) / norm
        if t < at_least:
            continue
        t = min(t, 1.0)
        if all(abs(d[i] - t * span[i]) <= tol for i in range(3)):
            return True
    return False


def dominant(image, rect: QRect) -> QColor:
    counts = collections.Counter(px.name() for px in pixels(image, rect))
    return QColor(counts.most_common(1)[0][0])


@MODES
def test_row_colors(window, qtbot, ui_theme, tmp_path, dark):
    tok = ui_theme(dark)
    running(window, tmp_path)
    busy, sel, off = dview("A-Busy"), dview("B-Sel"), dview("C-Off", online=False, os=None)
    window.devices.reset([busy, sel, off])
    window.conversations.model.set_activity(busy.device_id, activity(active(busy.device_id)))
    show_window(window, qtbot)
    assert window.conversations.select_device(sel.device_id)
    settle()
    image = window.grab().toImage()
    m = window.conversations.model
    sel_rect = row_rect_in_window(window, m.row_of(sel.device_id))
    busy_rect = row_rect_in_window(window, m.row_of(busy.device_id))
    off_rect = row_rect_in_window(window, m.row_of(off.device_id))
    assert _close(dominant(image, sel_rect), tok.selected, 3), "nền hàng đang chọn = selected"
    assert _close(dominant(image, busy_rect), tok.sidebar, 3), "hàng thường: nền sidebar"
    # Nửa dưới của khối chữ = dòng phụ (tên ở nửa trên). Chữ nhỏ được khử răng cưa: trên Linux
    # (FreeType) có thể không điểm ảnh nào đạt đúng màu chữ, nên tìm điểm pha giữa nền và màu chữ.
    half = busy_rect.height() // 2
    status_block = QRect(busy_rect.left() + 8 + 40 + 12, busy_rect.top() + half, busy_rect.width() - 60, half)
    assert has_blend(image, status_block, tok.sidebar, tok.accent_text), "dòng phụ BUSY màu accent_text"
    avatar = QRect(off_rect.left(), off_rect.top(), 8 + 40 + 4, off_rect.height())
    assert has_color(image, avatar, tok.offline, 16), "chấm ngoại tuyến có vòng offline"


# =================================================================================================
# T-V7 nút “＋ Ghép đôi máy mới”, danh sách rỗng
# =================================================================================================


def test_add_button_requests_add_device(conv, qtbot):
    conv.show(qtbot)
    conv.widget.add_button.click()
    assert conv.adds == 1


def test_empty_list_hides_list_view_but_keeps_add_button(conv, qtbot):
    conv.show(qtbot)
    w = conv.widget
    assert not w.list_view.isVisibleTo(w)
    assert w.add_button.isVisibleTo(w)
    conv.devices.reset([dview("May-U", paired=False)])
    settle()
    assert not w.list_view.isVisibleTo(w), "chỉ máy chưa ghép đôi: vẫn rỗng"
    conv.devices.reset([dview("May-A")])
    settle()
    assert w.list_view.isVisibleTo(w)
    assert w.add_button.isVisibleTo(w)


# =================================================================================================
# T-K thu gọn
# =================================================================================================


def test_k1_sidebar_compact_width_threshold(window, qtbot, tmp_path):
    from teleport.ui import main_window

    assert main_window.SIDEBAR_COMPACT_WIDTH == SIDEBAR_COMPACT_WIDTH
    running(window, tmp_path)
    show_window(window, qtbot, 980, 660)
    resize_to(window, COMPACT_BELOW - 1, 660)
    assert window.sidebar.width() == SIDEBAR_COMPACT_WIDTH
    assert window.sidebar_compact
    assert window.conversations.compact
    resize_to(window, COMPACT_BELOW, 660)
    assert window.sidebar.width() == SIDEBAR_WIDTH
    assert not window.sidebar_compact
    assert not window.conversations.compact


def test_k2_compact_hides_brand_and_machine_card_shows_compact_machine(window, qtbot, tmp_path):
    running(window, tmp_path)
    show_window(window, qtbot, 720, 660)
    assert not window.brand_label.isVisibleTo(window)
    assert not window.machine_card.isVisibleTo(window)
    assert window.compact_machine.isVisibleTo(window)
    resize_to(window, 980, 660)
    assert window.brand_label.isVisibleTo(window)
    assert window.machine_card.isVisibleTo(window)
    assert not window.compact_machine.isVisibleTo(window)


def test_k3_twelve_devices_fit_compact_sidebar_without_horizontal_scroll(window, qtbot, tmp_path):
    running(window, tmp_path)
    window.devices.reset([dview(f"May-{i:02d}", online=i % 2 == 0) for i in range(12)])
    show_window(window, qtbot, 720, 460)
    lst = window.conversations.list_view
    for r in range(lst.model().rowCount()):
        assert lst.visualRect(lst.model().index(r, 0)).width() >= 48, r
    viewport = window.sidebar_scroll.viewport().width()
    assert window.conversations.minimumSizeHint().width() <= viewport - 8, "lề 4 + 4 dạng thu gọn"
    assert window.sidebar_scroll.horizontalScrollBar().maximum() == 0
    assert lst.verticalScrollBar().maximum() == 0, "danh sách không có thanh cuộn dọc riêng"


def test_k4_focus_on_hidden_copy_button_moves_to_compact_machine(window, qtbot, tmp_path):
    running(window, tmp_path)
    window.set_addresses(["192.0.2.5"], None)
    show_window(window, qtbot, 980, 660, active_=True)
    window.copy_button.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(lambda: window.focusWidget() is window.copy_button, timeout=1000)
    resize_to(window, 720, 660)
    assert window.focusWidget() is window.compact_machine
    resize_to(window, 980, 660)
    assert window.focusWidget() is window.name_label, "12.15: rời thu gọn → name_label"


def test_k4_focus_on_row_stays_on_row(window, qtbot, tmp_path):
    running(window, tmp_path)
    window.devices.reset([dview("May-A"), dview("May-B")])
    show_window(window, qtbot, 980, 660, active_=True)
    lst = window.conversations.list_view
    lst.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(lambda: window.focusWidget() is lst, timeout=1000)
    resize_to(window, 720, 660)
    assert window.focusWidget() is lst
    resize_to(window, 980, 660)
    assert window.focusWidget() is lst


def _badge_text(window) -> str:
    t = window.machine_card.badge.text
    return t() if callable(t) else t


def test_k5_compact_machine_names_and_copies_address(window, qtbot, tmp_path):
    running(window, tmp_path, name="May-A")
    window.set_addresses(["192.0.2.5"], None)
    show_window(window, qtbot, 720, 660)
    cm = window.compact_machine
    assert cm.objectName() == "compactMachine"
    assert cm.accessibleName() == THIS_MACHINE
    assert cm.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert (cm.width(), cm.height()) == (48, 48)
    description = cm.accessibleDescription()
    assert "May-A" in description
    assert _badge_text(window) in description
    assert "192.0.2.5" in description
    assert cm.toolTip().startswith("<qt>"), "tooltip qua safe_tooltip"
    copied: list[str] = []
    window.machine_card.address_copied.connect(copied.append)
    QGuiApplication.clipboard().setText("cũ")
    cm.click()
    settle()
    assert QGuiApplication.clipboard().text() == "192.0.2.5"
    assert copied == ["192.0.2.5"]


def test_k5_compact_machine_without_address_does_nothing(window, qtbot, tmp_path):
    running(window, tmp_path)
    show_window(window, qtbot, 720, 660)
    copied: list[str] = []
    window.machine_card.address_copied.connect(copied.append)
    QGuiApplication.clipboard().setText("không đổi")
    window.compact_machine.click()
    settle()
    assert QGuiApplication.clipboard().text() == "không đổi"
    assert copied == []


def test_k5_compact_machine_description_follows_transfer_summary(window, qtbot, tmp_path):
    running(window, tmp_path)
    window.set_addresses(["192.0.2.5"], None)
    show_window(window, qtbot, 720, 660)
    before = window.compact_machine.accessibleDescription()
    window.set_transfer_summary(True, 2)
    settle()
    after = window.compact_machine.accessibleDescription()
    assert after != before
    assert _badge_text(window) in after


def test_k6_compact_rows_keep_accessible_text_and_tooltip(window, qtbot, tmp_path):
    running(window, tmp_path)
    a = dview("PC-Nha")
    window.devices.reset([a])
    show_window(window, qtbot, 980, 660)
    idx = window.conversations.model.index(0, 0)
    roles = (
        Qt.ItemDataRole.AccessibleTextRole,
        Qt.ItemDataRole.AccessibleDescriptionRole,
        Qt.ItemDataRole.ToolTipRole,
    )
    full = [idx.data(r) for r in roles]
    resize_to(window, 720, 660)
    assert window.conversations.compact
    idx = window.conversations.model.index(0, 0)
    assert [idx.data(r) for r in roles] == full
    assert "PC-Nha" in full[0] and ONLINE in full[0]


@MODES
def test_k6_busy_row_has_accent_strong_ring_in_compact(window, qtbot, ui_theme, tmp_path, dark):
    tok = ui_theme(dark)
    running(window, tmp_path)
    busy, idle = dview("A-Busy"), dview("B-Idle")
    window.devices.reset([busy, idle])
    window.conversations.model.set_activity(busy.device_id, activity(active(busy.device_id)))
    show_window(window, qtbot, 720, 660)
    assert window.conversations.compact
    image = window.grab().toImage()
    m = window.conversations.model

    def left_of_avatar(row: int) -> QRect:
        r = row_rect_in_window(window, row)
        cx, cy = r.center().x(), r.center().y()
        return QRect(cx - 25, cy - 3, 6, 7)  # vòng 2 px quanh avatar 40 (bán kính ~21–23), phía trái

    assert has_color(image, left_of_avatar(m.row_of(busy.device_id)), tok.accent_strong, 16)
    assert not has_color(image, left_of_avatar(m.row_of(idle.device_id)), tok.accent_strong, 16)


def test_k7_add_button_compact_is_icon_only(window, qtbot, tmp_path):
    running(window, tmp_path)
    show_window(window, qtbot, 720, 660)
    btn = window.conversations.add_button
    assert btn.toolButtonStyle() is Qt.ToolButtonStyle.ToolButtonIconOnly
    assert (btn.width(), btn.height()) == (ICON_BUTTON_SIZE, ICON_BUTTON_SIZE)
    assert btn.accessibleName() == ADD_DEVICE
    resize_to(window, 980, 660)
    assert btn.toolButtonStyle() is Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    assert btn.width() > ICON_BUTTON_SIZE
    assert isinstance(btn, QToolButton)
