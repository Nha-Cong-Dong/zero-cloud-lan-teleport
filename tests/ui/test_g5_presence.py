"""Chấm trạng thái chuyển màu mờ dần (spec G5 mục 3.3, test 7.3; mục 13 thắng).

Viết từ spec, không từ code. Mặc định chế độ tức thì (autouse `motion_instant`); ca hoạt ảnh thật xin
`real_motion` và hiện cửa sổ trước; chỉ kiểm giá trị cuối (`qtbot.waitUntil`), riêng “có hoạt ảnh ngay
sau” đếm bằng `running_count()` như spec 7.3.3.

1. `paint_avatar` không truyền `previous`, `progress=1` hay `previous` trùng: ảnh trùng từng điểm ảnh với
   cách gọi cũ; `progress=0` với `previous` khác: trông như `previous` (công thức lerp của 3.3).
2. `DeviceAvatar.set_presence`: tức thì → `presence_progress()==1`; thật → chờ 1 rồi hết hoạt ảnh;
   `animate=False`, widget ẩn, hệ điều hành tắt hiệu ứng → 1 ngay; tâm chấm cuối ≈ `success` (ONLINE) /
   ruột `ring` (OFFLINE).
3. `ChatHeader.set_device(..., key=…)`: cùng key → có hoạt ảnh; khác key hay None → nhảy thẳng;
   `ChatView` truyền `key=device_id`. `StatusBadge`: chữ và tên trợ năng đổi ngay, `dot_progress()` về 1;
   badge đang ẩn thì không hoạt ảnh (13.21).
4. Cột trái: ONLINE → OFFLINE qua model → `presence_fade` có `previous == ONLINE` rồi về xong;
   `modelReset` không hoạt ảnh; sắp lại do lên mạng (`layoutChanged`) vẫn có hoạt ảnh trên đúng hàng;
   ẩn view kết thúc ngay (13.18); cuối cùng `running_count()==0`.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget

WAIT_MS = 5000
MODES = pytest.mark.parametrize("dark", [False, True], ids=["sang", "toi"])


def settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


def close(a: QColor, b: QColor, tol: int) -> bool:
    return (
        abs(a.red() - b.red()) <= tol
        and abs(a.green() - b.green()) <= tol
        and abs(a.blue() - b.blue()) <= tol
    )


@pytest.fixture
def shown(qtbot):
    """`shown(size)`: một cửa sổ cha đã hiện (`waitExposed`), giữ sống tới hết test (qtbot chỉ giữ yếu)."""
    alive: list[QWidget] = []

    def make(size=(320, 120)) -> QWidget:
        host = QWidget()
        qtbot.addWidget(host)
        alive.append(host)
        host.resize(*size)
        host.show()
        qtbot.waitExposed(host)
        settle()
        return host

    yield make
    alive.clear()


# =================================================================================================
# 7.3.1 paint_avatar
# =================================================================================================


def _render(tokens, presence, *, emphasis=False, size=44, **extra) -> QImage:
    from teleport.ui.widgets import AvatarKind, paint_avatar

    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(1.0)
    image.fill(tokens.sidebar)
    painter = QPainter(image)
    try:
        paint_avatar(
            painter,
            QRectF(2, 2, size - 4, size - 4),
            kind=AvatarKind.DESKTOP,
            presence=presence,
            emphasis=emphasis,
            ring=tokens.sidebar,
            tokens=tokens,
            dpr=1.0,
            **extra,
        )
    finally:
        painter.end()
    return image


def _near(a: QImage, b: QImage, tol: int = 3, share: float = 0.99) -> bool:
    assert a.size() == b.size()
    total = a.width() * a.height()
    good = sum(
        1
        for y in range(a.height())
        for x in range(a.width())
        if close(a.pixelColor(x, y), b.pixelColor(x, y), tol)
    )
    return good >= total * share


@MODES
@pytest.mark.parametrize("emphasis", [False, True], ids=["thuong", "nhan"])
def test_paint_avatar_new_arguments_default_to_old_image(qapp, dark, emphasis):
    from teleport.ui import theme
    from teleport.ui.widgets import Presence

    t = theme.tokens(dark)
    for presence in Presence:
        old = _render(t, presence, emphasis=emphasis)
        assert _render(t, presence, emphasis=emphasis, previous=None) == old, presence
        assert _render(t, presence, emphasis=emphasis, progress=1.0) == old, presence
        assert _render(t, presence, emphasis=emphasis, previous=None, progress=0.3) == old, presence
        assert _render(t, presence, emphasis=emphasis, previous=presence, progress=0.3) == old, presence
        for other in Presence:
            assert _render(t, presence, emphasis=emphasis, previous=other, progress=1.0) == old, (
                presence,
                other,
            )
            assert _render(t, presence, emphasis=emphasis, previous=other, progress=2.0) == old, (
                presence,
                other,
            )


@MODES
@pytest.mark.parametrize(
    ("previous", "presence"),
    [("offline", "online"), ("none", "online"), ("online", "busy"), ("busy", "offline"), ("online", "none")],
)
def test_paint_avatar_progress_zero_looks_like_previous(qapp, dark, previous, presence):
    """m = 0: fill, ruột, alpha và độ mờ icon đều của `previous` (công thức 3.3)."""
    from teleport.ui import theme
    from teleport.ui.widgets import Presence

    t = theme.tokens(dark)
    prev, cur = Presence(previous), Presence(presence)
    start = _render(t, cur, previous=prev, progress=0.0)
    assert _near(start, _render(t, prev)), f"{prev}→{cur} ở progress 0 phải trông như {prev}"
    assert start != _render(t, cur), "progress 0 chưa phải trạng thái mới"


# =================================================================================================
# 7.3.2 DeviceAvatar
# =================================================================================================


def _avatar(host, presence):
    from teleport.ui.widgets import DeviceAvatar

    avatar = DeviceAvatar(host, presence=presence)  # 38 px, ring "surface"
    QHBoxLayout(host).addWidget(avatar)
    avatar.show()
    settle()
    return avatar


def _dot_center(avatar) -> QColor:
    """Tâm chấm: chấm `max(9, round(d·0.29))` px ở góc dưới phải của hình tròn (không ra ngoài rect)."""
    from teleport.ui import motion

    motion.finish_all()
    d = avatar.diameter
    dot = max(9, round(d * 0.29))
    center = d - dot / 2
    image = avatar.grab().toImage()
    dpr = image.devicePixelRatio()
    return image.pixelColor(round(center * dpr), round(center * dpr))


def test_instant_set_presence_progress_is_one(shown):
    from teleport.ui import motion
    from teleport.ui.widgets import Presence

    avatar = _avatar(shown(), Presence.OFFLINE)
    assert avatar.presence_progress() == 1.0
    avatar.set_presence(Presence.ONLINE)
    assert avatar.presence is Presence.ONLINE
    assert avatar.presence_progress() == 1.0
    assert motion.running_count() == 0


def test_real_motion_set_presence_animates_then_settles(shown, qtbot, real_motion):
    from teleport.ui.widgets import Presence

    avatar = _avatar(shown(), Presence.OFFLINE)
    avatar.set_presence(Presence.ONLINE)
    assert avatar.presence is Presence.ONLINE, "giá trị presence đổi ngay"
    assert real_motion.running_count() >= 1, "đang hiện: có hoạt ảnh chuyển màu"
    qtbot.waitUntil(lambda: avatar.presence_progress() == 1.0, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_real_motion_same_presence_does_not_animate(shown, real_motion):
    from teleport.ui.widgets import Presence

    avatar = _avatar(shown(), Presence.ONLINE)
    avatar.set_presence(Presence.ONLINE)
    assert avatar.presence_progress() == 1.0
    assert real_motion.running_count() == 0


def test_real_motion_animate_false_jumps(shown, real_motion):
    from teleport.ui.widgets import Presence

    avatar = _avatar(shown(), Presence.OFFLINE)
    avatar.set_presence(Presence.ONLINE, animate=False)
    assert avatar.presence_progress() == 1.0
    assert real_motion.running_count() == 0


def test_real_motion_hidden_avatar_jumps(qtbot, real_motion):
    from teleport.ui.widgets import DeviceAvatar, Presence

    avatar = DeviceAvatar(presence=Presence.OFFLINE)  # chưa hiện
    qtbot.addWidget(avatar)
    avatar.set_presence(Presence.ONLINE)
    assert avatar.presence_progress() == 1.0
    assert real_motion.running_count() == 0


def test_animations_disabled_avatar_jumps(shown, real_motion, monkeypatch):
    from teleport.ui.widgets import Presence

    monkeypatch.setattr(real_motion, "animations_enabled", lambda: False)
    avatar = _avatar(shown(), Presence.OFFLINE)
    avatar.set_presence(Presence.ONLINE)
    assert avatar.presence_progress() == 1.0
    assert real_motion.running_count() == 0


def test_real_motion_second_change_midway_ends_on_last_target(shown, qtbot, real_motion):
    from teleport.ui.widgets import Presence

    avatar = _avatar(shown(), Presence.OFFLINE)
    avatar.set_presence(Presence.ONLINE)
    avatar.set_presence(Presence.BUSY)
    assert avatar.presence is Presence.BUSY
    qtbot.waitUntil(lambda: avatar.presence_progress() == 1.0, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


@MODES
def test_real_motion_dot_center_final_colours(shown, qtbot, ui_theme, real_motion, dark):
    from teleport.ui.widgets import Presence

    tok = ui_theme(dark)
    avatar = _avatar(shown(), Presence.OFFLINE)
    avatar.set_presence(Presence.ONLINE)
    qtbot.waitUntil(lambda: avatar.presence_progress() == 1.0, timeout=WAIT_MS)
    got = _dot_center(avatar)
    assert close(got, tok.success, 8), f"ONLINE: tâm chấm success, thấy {got.name()}"
    avatar.set_presence(Presence.OFFLINE)
    qtbot.waitUntil(lambda: avatar.presence_progress() == 1.0, timeout=WAIT_MS)
    got = _dot_center(avatar)
    assert close(got, tok.surface, 8), f"OFFLINE: tâm chấm là ruột màu ring (surface), thấy {got.name()}"


# =================================================================================================
# 7.3.3 ChatHeader, StatusBadge
# =================================================================================================


def _header(host):
    from teleport.ui.chat import ChatHeader

    header = ChatHeader(host)
    QVBoxLayout(host).addWidget(header)
    header.show()
    settle()
    return header


def test_real_motion_header_same_key_animates(shown, qtbot, real_motion):
    from teleport.ui.widgets import Presence

    header = _header(shown((480, 120)))
    header.set_device("PC-Nha", "Ngoại tuyến", Presence.OFFLINE, key="k1")
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)
    header.set_device("PC-Nha", "Trực tuyến", Presence.ONLINE, key="k1")
    assert real_motion.running_count() >= 1, "cùng máy đổi trạng thái: chấm chuyển màu"
    qtbot.waitUntil(lambda: header.avatar.presence_progress() == 1.0, timeout=WAIT_MS)
    assert header.avatar.presence is Presence.ONLINE
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_real_motion_header_other_key_or_none_jumps(shown, qtbot, real_motion):
    from teleport.ui.widgets import Presence

    header = _header(shown((480, 120)))
    header.set_device("PC-Nha", "Trực tuyến", Presence.ONLINE, key="k1")
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)
    header.set_device("Laptop", "Ngoại tuyến", Presence.OFFLINE, key="k2")
    assert header.avatar.presence_progress() == 1.0, "đổi máy: nhảy thẳng"
    assert real_motion.running_count() == 0
    header.set_device("Laptop", "Trực tuyến", Presence.ONLINE, key=None)
    assert header.avatar.presence_progress() == 1.0, "key None: nhảy thẳng"
    header.set_device("Laptop", "Ngoại tuyến", Presence.OFFLINE)
    assert header.avatar.presence_progress() == 1.0, "không truyền key: nhảy thẳng"
    assert real_motion.running_count() == 0


PEER_ID = "a71c04d2" + "e" * 24
OTHER_ID = "0f0f0f0f" + "1" * 24


def _chat_dview(device_id: str, name: str, *, online: bool):
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


def test_real_motion_chat_view_header_animates_same_device_and_jumps_on_switch(qtbot, real_motion):
    from teleport.app.node import DeviceOffline
    from teleport.ui.chat import ChatView
    from teleport.ui.radar import DeviceListModel
    from teleport.ui.widgets import Presence

    host = QWidget()
    qtbot.addWidget(host)
    devices = DeviceListModel(host)
    devices.reset([_chat_dview(PEER_ID, "PC-Nha", online=True), _chat_dview(OTHER_ID, "Laptop", online=True)])
    view = ChatView(devices, host)
    QVBoxLayout(host).addWidget(view)
    host.resize(720, 460)
    view.set_device_names({PEER_ID: "PC-Nha", OTHER_ID: "Laptop"})
    host.show()
    qtbot.waitExposed(host)
    view.set_device(PEER_ID)
    settle()
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)
    avatar = view.header.avatar
    assert avatar.presence is Presence.ONLINE

    devices.apply(DeviceOffline(_chat_dview(PEER_ID, "PC-Nha", online=False)))
    assert avatar.presence is Presence.OFFLINE
    assert avatar.presence_progress() < 1.0, "cùng máy (key = device_id): chấm chuyển màu"
    qtbot.waitUntil(lambda: avatar.presence_progress() == 1.0, timeout=WAIT_MS)

    view.set_device(OTHER_ID)
    assert avatar.presence is Presence.ONLINE
    assert avatar.presence_progress() == 1.0, "đổi sang máy khác: nhảy thẳng"
    view.set_device(None)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def _badge(host):
    from teleport.ui.widgets import StatusBadge

    badge = StatusBadge(host)
    QHBoxLayout(host).addWidget(badge)
    return badge


def test_instant_badge_tone_change_text_now_progress_one(shown):
    from teleport.ui import motion
    from teleport.ui.widgets import BadgeTone

    badge = _badge(shown())
    badge.set_status(BadgeTone.SUCCESS, "Sẵn sàng nhận")
    badge.set_status(BadgeTone.MUTED, "Đang ẩn")
    assert badge.label.text() == "Đang ẩn"
    assert badge.accessibleName() == "Đang ẩn"
    assert badge.tone is BadgeTone.MUTED
    assert badge.dot_progress() == 1.0
    assert motion.running_count() == 0


def test_real_motion_badge_tone_change_text_now_dot_settles(shown, qtbot, real_motion):
    from teleport.ui.widgets import BadgeTone

    badge = _badge(shown())
    badge.set_status(BadgeTone.SUCCESS, "Sẵn sàng nhận")
    settle()
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)
    badge.set_status(BadgeTone.WARNING, "Tạm dừng")
    assert badge.label.text() == "Tạm dừng", "chữ đổi ngay"
    assert badge.accessibleName() == "Tạm dừng", "tên trợ năng đổi ngay"
    assert real_motion.running_count() >= 1, "badge đang hiện, đổi tone: chấm chuyển màu"
    qtbot.waitUntil(lambda: badge.dot_progress() == 1.0, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_real_motion_badge_same_tone_or_hidden_badge_does_not_animate(shown, real_motion):
    from teleport.ui.widgets import BadgeTone

    badge = _badge(shown())
    assert badge.isHidden(), "tiền đề: badge chưa có chữ thì ẩn"
    badge.set_status(BadgeTone.SUCCESS, "Sẵn sàng nhận")  # đang ẩn trước lời gọi → không hoạt ảnh
    assert badge.dot_progress() == 1.0
    assert real_motion.running_count() == 0
    badge.set_status(BadgeTone.SUCCESS, "Vẫn sẵn sàng")  # tone không đổi
    assert badge.dot_progress() == 1.0
    assert real_motion.running_count() == 0


def test_animations_disabled_badge_jumps(shown, real_motion, monkeypatch):
    from teleport.ui.widgets import BadgeTone

    monkeypatch.setattr(real_motion, "animations_enabled", lambda: False)
    badge = _badge(shown())
    badge.set_status(BadgeTone.SUCCESS, "Sẵn sàng nhận")
    settle()
    badge.set_status(BadgeTone.BUSY, "Đang truyền")
    assert badge.dot_progress() == 1.0
    assert real_motion.running_count() == 0


# =================================================================================================
# 7.3.4 Cột trái
# =================================================================================================


def device_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:32]


def dview(label: str, *, online: bool = True):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id(label),
        name=label,
        os="windows",
        paired=True,
        online=online,
        connected=False,
        addresses=("192.0.2.7",) if online else (),
        port=47001 if online else None,
    )


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

    def fade(self, did: str):
        return self.lst.presence_fade(self.model.index(self.model.row_of(did), 0))


def _done(fade) -> bool:
    previous, progress = fade
    return previous is None or progress == 1.0


@pytest.fixture
def conv(qtbot):
    c = Conv(qtbot)
    c.host.show()
    qtbot.waitExposed(c.host)
    return c


def test_instant_presence_fade_is_done(conv):
    from teleport.app.node import DeviceOffline
    from teleport.ui import motion

    x = dview("May-X")
    conv.devices.reset([x])
    settle()
    conv.devices.apply(DeviceOffline(dview("May-X", online=False)))
    assert _done(conv.fade(x.device_id))
    assert motion.running_count() == 0


def test_real_motion_online_to_offline_fades_then_settles(conv, qtbot, real_motion):
    from teleport.app.node import DeviceOffline
    from teleport.ui.widgets import Presence

    x = dview("May-X")
    conv.devices.reset([x])
    settle()
    assert _done(conv.fade(x.device_id))
    conv.devices.apply(DeviceOffline(dview("May-X", online=False)))
    previous, _progress = conv.fade(x.device_id)
    assert previous is Presence.ONLINE, "chấm chuyển từ ONLINE"
    qtbot.waitUntil(lambda: _done(conv.fade(x.device_id)), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_real_motion_model_reset_does_not_animate(conv, real_motion):
    x = dview("May-X")
    conv.devices.reset([x])
    settle()
    conv.devices.reset([dview("May-X", online=False)])
    settle()
    assert _done(conv.fade(x.device_id)), "modelReset (host mới): ghi lại, không hoạt ảnh"
    assert real_motion.running_count() == 0


def test_real_motion_reorder_on_coming_online_animates_right_row(conv, qtbot, real_motion):
    """B lên mạng thì lên trên A (proxy sắp lại: `layoutChanged`); chấm của B chuyển, A không."""
    from teleport.app.node import DeviceOnline
    from teleport.ui.widgets import Presence

    a, b = dview("May-A", online=False), dview("May-B", online=False)
    conv.devices.reset([a, b])
    settle()
    assert conv.model.row_of(a.device_id) < conv.model.row_of(b.device_id), "tiền đề: A trên B"
    conv.devices.apply(DeviceOnline(dview("May-B", online=True)))
    assert conv.model.row_of(b.device_id) < conv.model.row_of(a.device_id), "tiền đề: B lên đầu"
    previous, _progress = conv.fade(b.device_id)
    assert previous is Presence.OFFLINE, "hàng B (đã dời chỗ) có hoạt ảnh từ OFFLINE"
    assert _done(conv.fade(a.device_id)), "hàng A không đổi presence: không hoạt ảnh"
    qtbot.waitUntil(lambda: _done(conv.fade(b.device_id)), timeout=WAIT_MS)
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_real_motion_hiding_view_ends_presence_fades_at_once(conv, real_motion):
    from teleport.app.node import DeviceOffline

    x = dview("May-X")
    conv.devices.reset([x])
    settle()
    conv.devices.apply(DeviceOffline(dview("May-X", online=False)))
    conv.host.hide()
    assert _done(conv.fade(x.device_id))
    assert real_motion.running_count() == 0


def test_real_motion_hidden_view_presence_change_does_not_animate(qtbot, real_motion):
    from teleport.app.node import DeviceOffline

    c = Conv(qtbot)  # không hiện
    x = dview("May-X")
    c.devices.reset([x])
    c.devices.apply(DeviceOffline(dview("May-X", online=False)))
    assert _done(c.fade(x.device_id))
    assert real_motion.running_count() == 0


def test_animations_disabled_column_presence_jumps(conv, real_motion, monkeypatch):
    from teleport.app.node import DeviceOffline

    monkeypatch.setattr(real_motion, "animations_enabled", lambda: False)
    x = dview("May-X")
    conv.devices.reset([x])
    settle()
    conv.devices.apply(DeviceOffline(dview("May-X", online=False)))
    assert _done(conv.fade(x.device_id))
    assert real_motion.running_count() == 0


def test_real_motion_row_removed_mid_fade_leaves_no_animation(conv, qtbot, real_motion):
    """13.7: `rowsRemoved` xóa id khỏi `_presence_seen`/`_presence_fade`, huỷ hoạt ảnh bằng `INSTANT`."""
    from teleport.app.node import DeviceOffline, DeviceOnline

    x, y = dview("May-X"), dview("May-Y")
    conv.devices.reset([x, y])
    settle()
    conv.devices.apply(DeviceOffline(dview("May-X", online=False)))
    conv.devices.apply(DeviceOnline(dataclasses.replace(dview("May-X", online=False), paired=False)))
    settle()
    assert conv.model.row_of(x.device_id) < 0, "tiền đề: hàng X đã khỏi danh sách"
    qtbot.waitUntil(lambda: real_motion.running_count() == 0, timeout=WAIT_MS)


def test_presence_fade_of_invalid_index(conv):
    from PySide6.QtCore import QModelIndex

    assert _done(conv.lst.presence_fade(QModelIndex()))
