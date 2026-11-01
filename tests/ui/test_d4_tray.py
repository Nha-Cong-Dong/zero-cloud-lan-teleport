"""Khay đủ mvp F9 (spec D4 mục 4, mục 12 phần A, mục 14 thắng): phần `ui/tray.py` đứng riêng.

Viết từ spec, không từ code. Không node, không socket: `Tray` dựng thẳng, hàm thuần `tray_badge` /
`tray_tooltip` gọi trực tiếp. Phần nối vào `GuiApp` nằm ở `test_d4_tray_app.py`.

Phủ: A1 (bốn mục, bật/tắt theo `running`), A2 (bảng ưu tiên dấu, chữ tooltip VN/EN, ≤ 100 ký tự kể cả
`active = 999`, mục 14.18), A3 (pixmap 32 px của bốn dấu khác nhau từng đôi, `make_icon()` như cũ, màu
14.2, cỡ 20/40, `setIcon` chỉ khi dấu đổi 14.17, cache dựng lúc gọi lần đầu), A7 phần khay (menu con dựng
lại ở `aboutToShow` của cả hai menu 14.15, `&` → `&&`, hậu tố ngoại tuyến, không máy nào → mục tắt +
“Tìm máy mới…”), A9 (không khay), 14.3 (bóng thoại dùng logo không dấu, không đọc `icon()`), 14.10
(`pause_toggled` nối `triggered`, `set_receiving_paused` không phát), LanguageChange.

Seam: `teleport.ui.tray.is_available`; `QSystemTrayIcon.setIcon` / `showMessage` / `icon` vá bằng
`monkeypatch` (14.17).
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

APP_NAME = "ZCL Teleport"
M5 = ("Mở Teleport", "Gửi file…", "Tạm dừng nhận", "Thoát")

# Bảng tooltip (spec D4 mục 4.2; số nhiều EN theo 14.7).
TIP_ACTIVE_VI = "Đang truyền {n} lượt"
TIP_UNAVAILABLE_VI = "Không nhận file: không mở được thư mục nhận"
TIP_PAUSED_VI = "Đã tạm dừng nhận file mới"
TIP_READY_VI = "Sẵn sàng nhận"
TIP_ACTIVE_EN_ONE = "{n} transfer in progress"
TIP_ACTIVE_EN_MANY = "{n} transfers in progress"
TIP_UNAVAILABLE_EN = "Not receiving files: can't open the receive folder"
TIP_PAUSED_EN = "Paused receiving new files"
TIP_READY_EN = "Ready to receive"
TOOLTIP_BUDGET = 100  # 14.18: chừa chỗ cho F4

NO_PAIRED_VI = "Chưa ghép đôi với máy nào"
NO_PAIRED_EN = "No paired devices"
FIND_VI = "Tìm máy mới…"
FIND_EN = "Find devices…"
OFFLINE_VI = "{name} (ngoại tuyến)"

ID_A = "a71c04d2" + "e" * 24
ID_B = "0f0f0f0f" + "1" * 24
ID_C = "5c5c5c5c" + "2" * 24


def plain(text: str) -> str:
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


def expected_tooltip(running: bool, active: int, paused: bool, unavailable: bool, lang: str = "vi") -> str:
    lines = [APP_NAME]
    if not running:
        return APP_NAME
    if active > 0:
        if lang == "vi":
            lines.append(TIP_ACTIVE_VI.format(n=active))
        else:
            lines.append((TIP_ACTIVE_EN_ONE if active == 1 else TIP_ACTIVE_EN_MANY).format(n=active))
    if unavailable:
        lines.append(TIP_UNAVAILABLE_VI if lang == "vi" else TIP_UNAVAILABLE_EN)
    elif paused:
        lines.append(TIP_PAUSED_VI if lang == "vi" else TIP_PAUSED_EN)
    if len(lines) == 1:
        lines.append(TIP_READY_VI if lang == "vi" else TIP_READY_EN)
    return "\n".join(lines)


def expected_badge(running: bool, active: int, paused: bool, unavailable: bool) -> str:
    """Giá trị `TrayBadge` (chuỗi) theo bảng ưu tiên: UNAVAILABLE > PAUSED > TRANSFERRING > NONE."""
    if not running:
        return "none"
    if unavailable:
        return "unavailable"
    if paused:
        return "paused"
    if active > 0:
        return "transferring"
    return "none"


STATES = list(itertools.product((False, True), (0, 1, 3), (False, True), (False, True)))
STATE_IDS = [f"run{int(r)}-act{a}-pause{int(p)}-unav{int(u)}" for r, a, p, u in STATES]


def tray_icon_of(tray):
    from PySide6.QtWidgets import QSystemTrayIcon

    icon = tray.findChild(QSystemTrayIcon)
    assert icon is not None, "Tray phải giữ một QSystemTrayIcon con"
    return icon


def image(icon, size: int = 32):
    from PySide6.QtGui import QImage

    return icon.pixmap(size, size).toImage().convertToFormat(QImage.Format.Format_ARGB32)


def close_enough(c1, c2, tol: int = 24) -> bool:
    return (
        abs(c1.red() - c2.red()) <= tol
        and abs(c1.green() - c2.green()) <= tol
        and abs(c1.blue() - c2.blue()) <= tol
    )


def count_color(img, color, *, quadrant: bool = True) -> int:
    """Số pixel (đục) gần `color` trong góc dưới phải (nơi đặt dấu) của ảnh."""
    from PySide6.QtGui import QColor

    w, h = img.width(), img.height()
    xs = range(w // 2, w) if quadrant else range(w)
    ys = range(h // 2, h) if quadrant else range(h)
    n = 0
    for y in ys:
        for x in xs:
            c = QColor(img.pixelColor(x, y))
            if c.alpha() > 200 and close_enough(c, color):
                n += 1
    return n


@pytest.fixture
def tray(qapp):
    """`Tray` đứng riêng; hệ thống coi như có khay (`is_available` True), biểu tượng nhớ hiện/ẩn."""
    from teleport.ui.tray import Tray

    made: list = []

    def make():
        t = Tray()
        made.append(t)
        return t

    yield make
    for t in made:
        t.dispose()
    qapp.processEvents()


@pytest.fixture
def has_tray(monkeypatch):
    """Khay “có thật”: `is_available` True, `QSystemTrayIcon` nhớ hiện/ẩn, ghi lại mọi `showMessage`
    (kể cả đối số icon) và mọi lần đọc `QSystemTrayIcon.icon()`."""
    from types import SimpleNamespace

    from PySide6.QtWidgets import QSystemTrayIcon

    from teleport.ui import tray as mod

    shown: dict[int, bool] = {}
    messages: list[tuple] = []
    icon_reads: list[int] = []
    real_icon = QSystemTrayIcon.icon
    monkeypatch.setattr(mod, "is_available", lambda: True)
    monkeypatch.setattr(QSystemTrayIcon, "show", lambda self: shown.__setitem__(id(self), True))
    monkeypatch.setattr(QSystemTrayIcon, "hide", lambda self: shown.__setitem__(id(self), False))
    monkeypatch.setattr(QSystemTrayIcon, "setVisible", lambda self, v: shown.__setitem__(id(self), bool(v)))
    monkeypatch.setattr(QSystemTrayIcon, "isVisible", lambda self: shown.get(id(self), False))

    def show_message(self, *args, **kwargs):
        messages.append(args)

    def icon(self):
        icon_reads.append(1)
        return real_icon(self)

    monkeypatch.setattr(QSystemTrayIcon, "showMessage", show_message)
    monkeypatch.setattr(QSystemTrayIcon, "icon", icon)
    return SimpleNamespace(messages=messages, icon_reads=icon_reads, shown=shown)


def targets(*items):
    from teleport.ui.tray import SendTarget

    return [SendTarget(device_id=i, label=label, online=online) for i, label, online in items]


def show_send_menu(t, *, main: bool = False) -> list:
    """Phát `aboutToShow` (của menu con hay menu chính) rồi trả các mục của menu con (bỏ dòng kẻ)."""
    if main:
        t.menu.aboutToShow.emit()
    else:
        t.send_menu.aboutToShow.emit()
    return [a for a in t.send_menu.actions() if not a.isSeparator()]


# =================================================================================================
# A2: hàm thuần
# =================================================================================================


def test_tray_state_defaults_and_frozen():
    import dataclasses

    from teleport.ui.tray import TrayState

    s = TrayState()
    assert (s.running, s.active, s.paused, s.unavailable) == (False, 0, False, False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.paused = True  # type: ignore[misc]


def test_tray_badge_values():
    from teleport.ui.tray import TrayBadge

    assert [b.value for b in TrayBadge] == ["none", "transferring", "paused", "unavailable"]


@pytest.mark.parametrize(("running", "active", "paused", "unavailable"), STATES, ids=STATE_IDS)
def test_tray_badge_follows_priority_table(running, active, paused, unavailable):
    from teleport.ui.tray import TrayBadge, TrayState, tray_badge

    state = TrayState(running=running, active=active, paused=paused, unavailable=unavailable)
    assert tray_badge(state) is TrayBadge(expected_badge(running, active, paused, unavailable))


@pytest.mark.parametrize(("running", "active", "paused", "unavailable"), STATES, ids=STATE_IDS)
def test_tray_tooltip_vi_follows_table(qapp, running, active, paused, unavailable):
    from teleport.ui.tray import TrayState, tray_tooltip

    state = TrayState(running=running, active=active, paused=paused, unavailable=unavailable)
    assert tray_tooltip(state) == expected_tooltip(running, active, paused, unavailable)


@pytest.mark.parametrize(("running", "active", "paused", "unavailable"), STATES, ids=STATE_IDS)
def test_tray_tooltip_en_follows_table(qapp, running, active, paused, unavailable):
    from teleport.ui import i18n
    from teleport.ui.tray import TrayState, tray_tooltip

    i18n.set_language("en")
    state = TrayState(running=running, active=active, paused=paused, unavailable=unavailable)
    assert tray_tooltip(state) == expected_tooltip(running, active, paused, unavailable, "en")


@pytest.mark.parametrize("lang", ["vi", "en"])
@pytest.mark.parametrize("active", [0, 1, 99, 999, 99_999])
def test_tray_tooltip_within_budget_every_combination(qapp, lang, active):
    """14.18: toàn tooltip ≤ 100 ký tự ở VN và EN, kể cả số lượt rất lớn."""
    from teleport.ui import i18n
    from teleport.ui.tray import TrayState, tray_tooltip

    if lang == "en":
        i18n.set_language("en")
    for running, paused, unavailable in itertools.product((False, True), repeat=3):
        tip = tray_tooltip(TrayState(running=running, active=active, paused=paused, unavailable=unavailable))
        assert len(tip) <= TOOLTIP_BUDGET, (lang, active, paused, unavailable, len(tip), tip)


def test_tray_tooltip_is_translated_at_call_time(qapp):
    """Hàm thuần dịch lúc gọi: cùng trạng thái, đổi ngôn ngữ thì chữ đổi."""
    from teleport.ui import i18n
    from teleport.ui.tray import TrayState, tray_tooltip

    s = TrayState(running=True)
    vi = tray_tooltip(s)
    i18n.set_language("en")
    en = tray_tooltip(s)
    assert vi.endswith(TIP_READY_VI) and en.endswith(TIP_READY_EN)


# =================================================================================================
# A3: biểu tượng
# =================================================================================================


def test_make_icon_without_argument_still_works_and_equals_none_badge(qapp):
    from teleport.ui.tray import TrayBadge, make_icon

    plain_icon = make_icon()
    assert not plain_icon.isNull()
    assert not plain_icon.pixmap(32, 32).isNull()
    assert image(plain_icon) == image(make_icon(TrayBadge.NONE))
    assert image(make_icon(badge=TrayBadge.NONE)) == image(plain_icon)


def test_badge_pixmaps_32_differ_pairwise(qapp):
    from teleport.ui.tray import TrayBadge, make_icon

    images = {b: image(make_icon(b), 32) for b in TrayBadge}
    for b in TrayBadge:
        assert not make_icon(b).pixmap(32, 32).isNull(), b
    for a, b in itertools.combinations(TrayBadge, 2):
        assert images[a] != images[b], f"dấu {a} và {b} vẽ giống nhau ở 32 px"


@pytest.mark.parametrize("size", [16, 20, 24, 32, 40, 48])
def test_badge_pixmaps_differ_from_plain_logo_at_every_tray_size(qapp, size):
    from teleport.ui.tray import TrayBadge, make_icon

    base = image(make_icon(), size)
    for b in (TrayBadge.TRANSFERRING, TrayBadge.PAUSED, TrayBadge.UNAVAILABLE):
        assert image(make_icon(b), size) != base, (b, size)


def test_badged_icons_have_sizes_20_and_40(qapp):
    """14.2: icon có dấu thêm cỡ 20 và 40."""
    from PySide6.QtCore import QSize

    from teleport.ui.tray import TrayBadge, make_icon

    for b in (TrayBadge.TRANSFERRING, TrayBadge.PAUSED, TrayBadge.UNAVAILABLE):
        sizes = make_icon(b).availableSizes()
        assert QSize(20, 20) in sizes and QSize(40, 40) in sizes, (b, sizes)


@pytest.mark.parametrize(
    ("badge", "tokens_needed"),
    [
        ("transferring", ("accent_strong",)),
        ("paused", ("warning", "surface")),
        ("unavailable", ("danger", "surface")),
    ],
)
def test_badge_colors_come_from_fixed_mint_light_tokens(qapp, badge, tokens_needed):
    """14.2: màu từ `theme.tokens(dark=False)` bộ mint mặc định; dấu nằm ở góc dưới phải. Ảnh 40 px để đủ
    pixel đặc. Mỗi màu cần có phải xuất hiện ở góc dấu nhiều hơn hẳn so với logo không dấu."""
    from teleport.ui import theme
    from teleport.ui.tray import TrayBadge, make_icon

    tokens = theme.tokens(False, theme.DEFAULT_ACCENT)
    badged = image(make_icon(TrayBadge(badge)), 40)
    base = image(make_icon(), 40)
    for name in tokens_needed:
        color = getattr(tokens, name)
        got, before = count_color(badged, color), count_color(base, color)
        assert got >= before + 6, (
            f"dấu {badge}: màu {name} ({color.name()}) gần như không có ({got} vs {before})"
        )


def test_paused_and_unavailable_differ_in_color_not_only_shape(qapp):
    """14.2: PAUSED nền `warning`, UNAVAILABLE nền `danger`."""
    from teleport.ui import theme
    from teleport.ui.tray import TrayBadge, make_icon

    tokens = theme.tokens(False, theme.DEFAULT_ACCENT)
    paused = image(make_icon(TrayBadge.PAUSED), 40)
    unavailable = image(make_icon(TrayBadge.UNAVAILABLE), 40)
    assert count_color(paused, tokens.warning) > count_color(unavailable, tokens.warning)
    assert count_color(unavailable, tokens.danger) > count_color(paused, tokens.danger)


def test_badge_icon_is_cached_after_first_build(qapp, monkeypatch):
    """Mỗi dấu dựng một lần rồi cache: lần gọi sau không vẽ lại logo."""
    from teleport.ui import logo
    from teleport.ui.tray import TrayBadge, make_icon

    for b in TrayBadge:
        make_icon(b)
    calls: list[int] = []
    real = logo.pixmap
    monkeypatch.setattr(logo, "pixmap", lambda *a, **k: calls.append(1) or real(*a, **k))
    for b in TrayBadge:
        make_icon(b)
    make_icon()
    assert calls == [], "make_icon gọi lại phải lấy từ cache"


@pytest.mark.slow
def test_importing_tray_builds_no_icon_without_qapplication(tmp_path):
    """14.17: cache dựng lúc gọi lần đầu, không lúc import (import trước khi có QApplication không sập)."""
    import teleport

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    src = str(Path(teleport.__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join([src, env.get("PYTHONPATH", "")])
    code = (
        "import teleport.ui.tray as t\n"
        "from PySide6.QtWidgets import QApplication\n"
        "assert QApplication.instance() is None\n"
        "print('OK')\n"
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=120)
    assert p.returncode == 0 and b"OK" in p.stdout, p.stderr.decode("utf-8", "replace")


def test_set_icon_only_when_badge_changes(qapp, monkeypatch, tray):
    """A3 / 14.17: đếm `QSystemTrayIcon.setIcon` qua `monkeypatch`; mỗi lần dấu đổi đúng một lần."""
    from PySide6.QtWidgets import QSystemTrayIcon

    from teleport.ui.tray import TrayBadge, make_icon

    calls: list = []
    real = QSystemTrayIcon.setIcon

    def spy(self, icon):
        calls.append(icon)
        return real(self, icon)

    monkeypatch.setattr(QSystemTrayIcon, "setIcon", spy)
    t = tray()
    calls.clear()
    t.set_running(True)
    assert calls == [] and t.badge is TrayBadge.NONE
    t.set_activity(1)
    assert len(calls) == 1 and t.badge is TrayBadge.TRANSFERRING
    assert image(calls[-1]) == image(make_icon(TrayBadge.TRANSFERRING))
    t.set_activity(2)
    t.set_activity(5)
    assert len(calls) == 1, "đổi số lượt mà dấu không đổi: không setIcon"
    t.set_receiving_paused(True)
    assert len(calls) == 2 and t.badge is TrayBadge.PAUSED
    t.set_activity(0)
    t.set_receiving_paused(True)
    assert len(calls) == 2
    t.set_receiving_unavailable(True)
    assert len(calls) == 3 and t.badge is TrayBadge.UNAVAILABLE
    assert image(calls[-1]) == image(make_icon(TrayBadge.UNAVAILABLE))
    t.set_receiving_paused(False)
    assert len(calls) == 3
    t.set_receiving_unavailable(False)
    assert len(calls) == 4 and t.badge is TrayBadge.NONE
    assert image(calls[-1]) == image(make_icon())
    t.set_running(False)
    t.set_activity(3)
    assert len(calls) == 4, "node không chạy: luôn NONE"
    assert t.badge is TrayBadge.NONE


# =================================================================================================
# A1: menu, bật/tắt
# =================================================================================================


def test_menu_has_four_actions_in_order_all_visible(tray):
    t = tray()
    actions = t.actions()
    assert len(actions) == 4
    assert [plain(a.text()) for a in actions] == list(M5)
    assert all(a.isVisible() for a in actions), "D4: cả bốn mục đều hiện"
    menu_items = [a for a in t.menu.actions() if not a.isSeparator()]
    assert menu_items == actions, "thứ tự trên menu theo M5, có dòng kẻ trước Thoát"
    raw = t.menu.actions()
    assert raw[-2].isSeparator() and raw[-1] is actions[3]
    assert t.menu.defaultAction() is actions[0]


def test_send_action_is_submenu_of_main_menu(tray):
    t = tray()
    assert t.actions()[1] is t.send_menu.menuAction()
    assert t.send_menu.parent() is t.menu, "14.15: menu con có cha là menu chính"


def test_pause_action_is_checkable_and_unchecked_at_start(tray):
    t = tray()
    pause = t.actions()[2]
    assert pause.isCheckable()
    assert not pause.isChecked()


def test_send_and_pause_enabled_only_while_running(tray):
    t = tray()
    assert [a.isEnabled() for a in t.actions()] == [True, False, False, True]
    t.set_running(True)
    assert [a.isEnabled() for a in t.actions()] == [True, True, True, True]
    t.set_running(False)
    assert [a.isEnabled() for a in t.actions()] == [True, False, False, True]
    assert all(a.isVisible() for a in t.actions())


def test_enabled_state_depends_only_on_running(tray):
    """14.20: dấu tạm dừng / thư mục hỏng / số lượt không đổi việc bật tắt hai mục giữa."""
    t = tray()
    t.set_receiving_paused(True)
    t.set_receiving_unavailable(True)
    t.set_activity(4)
    assert [a.isEnabled() for a in t.actions()[1:3]] == [False, False]
    t.set_running(True)
    assert [a.isEnabled() for a in t.actions()[1:3]] == [True, True]


def test_open_and_quit_still_emit_signals(qtbot, tray):
    t = tray()
    got: list[str] = []
    t.open_requested.connect(lambda: got.append("open"))
    t.quit_requested.connect(lambda: got.append("quit"))
    t.actions()[0].trigger()
    t.actions()[3].trigger()
    assert got == ["open", "quit"]


# =================================================================================================
# State / tooltip của Tray
# =================================================================================================


def test_tray_state_and_tooltip_follow_setters(tray):
    from teleport.ui.tray import TrayBadge, TrayState, tray_tooltip

    t = tray()
    assert t.state == TrayState()
    assert t.tooltip() == APP_NAME
    assert tray_icon_of(t).toolTip() == APP_NAME
    t.set_running(True)
    t.set_activity(2)
    t.set_receiving_paused(True)
    assert t.state == TrayState(running=True, active=2, paused=True, unavailable=False)
    assert t.badge is TrayBadge.PAUSED
    want = expected_tooltip(True, 2, True, False)
    assert t.tooltip() == want == tray_tooltip(t.state)
    assert tray_icon_of(t).toolTip() == want
    t.set_receiving_unavailable(True)
    want = expected_tooltip(True, 2, True, True)
    assert t.tooltip() == want and tray_icon_of(t).toolTip() == want
    assert t.badge is TrayBadge.UNAVAILABLE


def test_set_receiving_paused_sets_check_without_emitting_pause_toggled(qtbot, tray):
    t = tray()
    t.set_running(True)
    got: list = []
    t.pause_toggled.connect(got.append)
    t.set_receiving_paused(True)
    assert t.actions()[2].isChecked()
    t.set_receiving_paused(False)
    assert not t.actions()[2].isChecked()
    assert got == [], "set_receiving_paused không phát pause_toggled (QSignalBlocker)"


def test_pause_action_trigger_emits_pause_toggled_but_does_not_change_state(qtbot, tray):
    """14.10: bấm thì dấu chọn đổi ngay và phát `pause_toggled(bool)`; `state.paused` chỉ đổi khi
    `set_receiving_paused` (node xác nhận)."""
    from teleport.ui.tray import TrayBadge

    t = tray()
    t.set_running(True)
    got: list = []
    t.pause_toggled.connect(got.append)
    pause = t.actions()[2]
    pause.trigger()
    assert got == [True]
    assert pause.isChecked()
    assert t.state.paused is False and t.badge is TrayBadge.NONE
    assert t.tooltip() == expected_tooltip(True, 0, False, False)
    pause.trigger()
    assert got == [True, False]


def test_pause_toggled_follows_triggered_not_toggled(qtbot, tray):
    """14.10: `setChecked` bằng code (không phải người dùng bấm) không phát `pause_toggled`."""
    t = tray()
    t.set_running(True)
    got: list = []
    t.pause_toggled.connect(got.append)
    t.actions()[2].setChecked(True)
    t.actions()[2].setChecked(False)
    assert got == []


# =================================================================================================
# A7 (phần khay): menu con “Gửi file…”
# =================================================================================================


def test_send_menu_reads_provider_on_every_about_to_show(tray):
    t = tray()
    calls: list[int] = []
    current = targets((ID_A, "PC-Nha", True))
    t.set_send_targets_provider(lambda: calls.append(1) or current)
    items = show_send_menu(t)
    assert [plain(a.text()) for a in items] == ["PC-Nha"]
    current = targets((ID_B, "Laptop", True), (ID_A, "PC-Nha", True))
    items = show_send_menu(t)
    assert [plain(a.text()) for a in items] == ["Laptop", "PC-Nha"], "thứ tự theo hàm cung cấp"
    assert len(calls) == 2


def test_send_menu_is_rebuilt_on_main_menu_about_to_show(tray):
    """14.15: dựng lại ở `aboutToShow` của menu chính (Linux SNI có thể không phát cho menu con)."""
    t = tray()
    current = targets((ID_A, "PC-Nha", True))
    t.set_send_targets_provider(lambda: current)
    show_send_menu(t, main=True)
    current = targets((ID_C, "May-Moi", True))
    items = show_send_menu(t, main=True)
    assert [plain(a.text()) for a in items] == ["May-Moi"]


def test_send_menu_escapes_ampersand_in_peer_label(tray):
    t = tray()
    t.set_send_targets_provider(lambda: targets((ID_A, "R&D & Co", True)))
    [item] = show_send_menu(t)
    assert item.text() == "R&&D && Co", "tên do máy kia đặt: & phải thành && (không thành phím tắt)"
    assert plain(item.text()) == "R&D & Co"


def test_send_menu_offline_suffix_and_escape_after_fill(tray):
    from teleport.ui import texts

    t = tray()
    t.set_send_targets_provider(lambda: targets((ID_A, "A&B", False), (ID_B, "On", True)))
    off, on = show_send_menu(t)
    assert off.text() == texts.fill(OFFLINE_VI, name="A&B").replace("&", "&&")
    assert plain(off.text()) == "A&B (ngoại tuyến)"
    assert on.text() == "On"
    assert off.isEnabled(), "máy ngoại tuyến vẫn chọn được (lượt gửi chờ máy đó lên mạng)"


def test_send_menu_selecting_device_emits_send_to_requested(qtbot, tray):
    t = tray()
    t.set_send_targets_provider(lambda: targets((ID_A, "PC-Nha", True), (ID_B, "Laptop", False)))
    got: list[str] = []
    t.send_to_requested.connect(got.append)
    items = show_send_menu(t)
    items[1].trigger()
    items[0].trigger()
    assert got == [ID_B, ID_A]


def test_send_menu_without_paired_devices_shows_disabled_note_and_find(qtbot, tray):
    t = tray()
    t.set_send_targets_provider(lambda: [])
    found: list[int] = []
    sent: list[str] = []
    t.discover_requested.connect(lambda: found.append(1))
    t.send_to_requested.connect(sent.append)
    items = show_send_menu(t)
    assert [plain(a.text()) for a in items] == [NO_PAIRED_VI, FIND_VI]
    note, find = items
    assert not note.isEnabled()
    assert find.isEnabled()
    find.trigger()
    assert found == [1] and sent == []


def test_send_menu_without_provider_is_safe(tray):
    """Chưa tiêm hàm cung cấp: mở menu không lỗi (coi như chưa ghép đôi máy nào)."""
    t = tray()
    items = show_send_menu(t)
    assert NO_PAIRED_VI in [plain(a.text()) for a in items]


def test_send_menu_has_many_devices_without_limit(tray):
    t = tray()
    many = targets(*[(f"{i:032x}", f"May-{i:03d}", True) for i in range(60)])
    t.set_send_targets_provider(lambda: many)
    items = show_send_menu(t)
    assert len(items) == 60


# =================================================================================================
# LanguageChange
# =================================================================================================


def test_language_change_retranslates_menu_tooltip_and_submenu(qapp, tray):
    from teleport.ui import i18n, texts

    t = tray()
    t.set_running(True)
    t.set_receiving_paused(True)
    t.set_send_targets_provider(lambda: [])
    assert t.tooltip() == expected_tooltip(True, 0, True, False)
    i18n.set_language("en")
    qapp.processEvents()
    texts_en = [texts.menu_open(), texts.menu_send(), texts.menu_pause(), texts.menu_quit()]
    assert texts_en != list(M5), "bản dịch EN của menu phải có"
    assert [a.text() for a in t.actions()] == texts_en
    assert t.tooltip() == expected_tooltip(True, 0, True, False, "en")
    assert tray_icon_of(t).toolTip() == t.tooltip()
    items = show_send_menu(t)
    assert [plain(a.text()) for a in items] == [NO_PAIRED_EN, FIND_EN]
    i18n.set_language("vi")
    qapp.processEvents()
    assert [plain(a.text()) for a in t.actions()] == list(M5)
    assert t.tooltip() == expected_tooltip(True, 0, True, False)


def test_offline_suffix_is_translated(qapp, tray):
    from teleport.ui import i18n

    t = tray()
    t.set_send_targets_provider(lambda: targets((ID_A, "PC-Nha", False)))
    i18n.set_language("en")
    qapp.processEvents()
    [item] = show_send_menu(t)
    assert "PC-Nha" in item.text()
    assert "ngoại tuyến" not in item.text()


# =================================================================================================
# A9: không có khay; 14.3 bóng thoại
# =================================================================================================


def test_without_system_tray_setters_do_not_fail_and_show_does_nothing(qapp, monkeypatch):
    from teleport.ui import tray as mod

    monkeypatch.setattr(mod, "is_available", lambda: False)
    t = mod.Tray()
    try:
        t.set_running(True)
        t.set_activity(3)
        t.set_receiving_paused(True)
        t.set_receiving_unavailable(True)
        t.set_send_targets_provider(lambda: [])
        t.set_running(False)
        t.show()
        assert not t.visible
        t.show_message("Đã nhận a.txt.")
        assert len(t.actions()) == 4
    finally:
        t.dispose()


def test_balloon_uses_plain_logo_and_never_reads_current_icon(qapp, has_tray, tray):
    """14.3: bóng thoại luôn dùng `make_icon()` không dấu (cache), không đọc `_icon.icon()`."""
    from teleport.ui.tray import TrayBadge, make_icon

    t = tray()
    t.show()
    t.set_running(True)
    t.set_receiving_paused(True)
    assert t.badge is TrayBadge.PAUSED
    has_tray.icon_reads.clear()
    t.show_message("Có file đang chờ bạn nhận.")
    assert len(has_tray.messages) == 1
    args = has_tray.messages[0]
    assert args[0] == APP_NAME and args[1] == "Có file đang chờ bạn nhận."
    icon = args[2]
    assert image(icon) == image(make_icon()), "bóng thoại mang logo không dấu"
    assert image(icon) != image(make_icon(TrayBadge.PAUSED))
    assert has_tray.icon_reads == [], "không đọc QSystemTrayIcon.icon() (có thể đang mang dấu)"


def test_balloon_message_click_folder_still_works(qapp, has_tray, tray, tmp_path):
    """A9: hành vi bóng thoại cũ vẫn đúng khi có dấu."""
    t = tray()
    t.show()
    t.set_running(True)
    t.set_activity(1)
    got: list = []
    t.message_clicked.connect(got.append)
    t.show_message("Đã nhận a.txt từ PC-Nha.", folder=tmp_path)
    tray_icon_of(t).messageClicked.emit()
    assert got == [tmp_path]
