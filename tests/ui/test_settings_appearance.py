"""Mục “Giao diện” gắn vào trang Cài đặt và `SettingsController` (spec D6 mục 2.5, test 5.5; mục 12 ý 14
và mục 13 ý 1, 3–6 thắng các mục trên khi khác).

Viết từ spec, không từ code.

Trang (`SettingsPage`): sáu mục theo thứ tự Chung · Giao diện · Quyền riêng tư · Thiết bị tin cậy · Truyền
file · Nâng cao (`Section.APPEARANCE` thứ 2, icon `color` cỡ 20); ô ngôn ngữ rời mục Chung sang mục Giao
diện nhưng `page.language_combo` / `page.language_selected` vẫn dùng được; trang chuyển tiếp
`theme_selected`, `accent_selected`; `set_values` đặt chế độ/màu nhấn từ ảnh chụp, `set_loaded` bật/tắt
cả ba hàng; vừa 720×460 không cuộn ngang; Tab: `nav` → nút chế độ đang chọn → ô màu đang chọn →
`language_combo` (mục 13 ý 5).

Controller: `SettingsSnapshot.theme/accent`; chọn chế độ/màu → `node.set_ui_preferences(theme=…)` /
`(accent=…)` qua `host.call` (thread node), **xong** mới `theme.set_preference`; `SettingsSaveError` →
control trả về giá trị đã xác nhận, không áp chủ đề, có câu SETTINGS; ảnh chụp đầu tiên đồng bộ chủ đề
đang áp với giá trị đã lưu.

Host giả theo mẫu `test_settings_controller.py` (chép lại ở đây vì không import chéo file test).
"""

from __future__ import annotations

import contextlib
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QObject, QPoint, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication, QRadioButton, QScrollArea, QWidget

NAV = ("Chung", "Giao diện", "Quyền riêng tư", "Thiết bị tin cậy", "Truyền file", "Nâng cao")
SECTION_NAMES = ["GENERAL", "APPEARANCE", "PRIVACY", "DEVICES", "TRANSFER", "ADVANCED"]
ACCENT_IDS = ("mint", "ocean", "violet", "rose", "amber", "graphite")
MODE_VALUES = (None, "light", "dark")
ACCENT_DESCRIPTION = "Màu nhấn"
SETTINGS_ERROR = (
    "Chưa lưu được cài đặt vì không ghi được vào thư mục cấu hình của Teleport. Hãy kiểm tra quyền ghi "
    "rồi thử lại."
)


# =================================================================================================
# Phụ trợ
# =================================================================================================


def settle(qtbot, ms: int = 30) -> None:
    QApplication.processEvents()
    qtbot.wait(ms)


def activate(qtbot, window: QWidget) -> None:
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    window.raise_()
    qtbot.waitUntil(window.isActiveWindow, timeout=3000)
    QApplication.processEvents()


def pos(widget: QWidget, root: QWidget) -> QPoint:
    return widget.mapTo(root, QPoint(0, 0))


def appearance(page):
    from teleport.ui.appearance import AppearanceSection

    found = page.findChildren(AppearanceSection)
    assert len(found) == 1, f"trang cần đúng một AppearanceSection, thấy {len(found)}"
    return found[0]


def mode_buttons(root: QWidget) -> list[QRadioButton]:
    from teleport.ui.widgets import SegmentedControl

    controls = root.findChildren(SegmentedControl)
    assert len(controls) == 1, f"cần đúng một SegmentedControl, thấy {len(controls)}"
    buttons = controls[0].findChildren(QRadioButton)
    assert len(buttons) == 3
    return sorted(buttons, key=lambda b: pos(b, controls[0]).x())


def swatches(root: QWidget) -> list[QAbstractButton]:
    buttons = root.findChildren(QAbstractButton)
    found = [b for b in buttons if b.accessibleDescription().endswith(ACCENT_DESCRIPTION)]
    assert len(found) == 6, f"cần 6 ô màu, thấy {len(found)}"
    parent = found[0].parentWidget()
    return sorted(found, key=lambda b: (pos(b, parent).y(), pos(b, parent).x()))


def swatch(root: QWidget, accent: str) -> QAbstractButton:
    return swatches(root)[ACCENT_IDS.index(accent)]


def checked_mode(root: QWidget) -> object:
    checked = [MODE_VALUES[i] for i, b in enumerate(mode_buttons(root)) if b.isChecked()]
    assert len(checked) == 1, checked
    return checked[0]


def checked_accent(root: QWidget) -> str:
    checked = [ACCENT_IDS[i] for i, b in enumerate(swatches(root)) if b.isChecked()]
    assert len(checked) == 1, checked
    return checked[0]


def section_index(name: str) -> int:
    from teleport.ui.settings import Section

    return list(Section).index(Section[name])


def open_section(page, name: str) -> QWidget:
    from teleport.ui.settings import Section

    page.show_section(Section[name])
    QApplication.processEvents()
    assert page.sections.currentIndex() == section_index(name)
    return page.sections.currentWidget()


def press_tab(times: int = 1) -> list:
    seen = []
    for _ in range(times):
        focus = QApplication.focusWidget()
        assert focus is not None, "không widget nào có tiêu điểm"
        QTest.keyClick(focus, Qt.Key.Key_Tab)
        seen.append(QApplication.focusWidget())
    return seen


def snapshot(**kw):
    from teleport.ui.settings import SettingsSnapshot

    values = {
        "name": "May-A",
        "visible": True,
        "rate_limit": None,
        "temp_retention_days": None,
        "language": None,
        "autostart": False,
        "theme": None,
        "accent": None,
        "devices": {},
    }
    values.update(kw)
    return SettingsSnapshot(**values)


class PageSpy:
    NAMES = ("theme_selected", "accent_selected", "language_selected")

    def __init__(self, page) -> None:
        self.log: list[tuple] = []
        for name in self.NAMES:
            getattr(page, name).connect(lambda *a, _n=name: self.log.append((_n, *a)))

    def of(self, name: str) -> list:
        return [e[1] for e in self.log if e[0] == name]


@pytest.fixture(autouse=True)
def autostart_supported(monkeypatch):
    from teleport.app import autostart

    monkeypatch.setattr(autostart, "supported", lambda: True)


@pytest.fixture
def ui(qtbot):
    """`SettingsPage` đứng riêng 980x660, đã nạp giá trị mặc định, đang hiện."""
    from teleport.app.autostart import AutostartState
    from teleport.ui.radar import DeviceListModel
    from teleport.ui.settings import SettingsPage

    model = DeviceListModel()
    page = SettingsPage(model)
    model.setParent(page)
    qtbot.addWidget(page)
    page.resize(980, 660)
    page.show()
    qtbot.waitExposed(page)
    page.set_loaded(True)
    page.set_values(snapshot())
    page.set_autostart(AutostartState.OFF)
    settle(qtbot)
    return SimpleNamespace(page=page, model=model, qtbot=qtbot, spy=PageSpy(page))


# =================================================================================================
# Trang
# =================================================================================================


def test_nav_six_sections_appearance_second_with_color_icon(qtbot, monkeypatch):
    from teleport.ui import icons
    from teleport.ui.radar import DeviceListModel
    from teleport.ui.settings import Section, SettingsPage

    requested: list[tuple[str, int]] = []
    real_pixmap, real_icon = icons.pixmap, icons.icon

    def spy_pixmap(name, size, *a, **k):
        requested.append((name, size))
        return real_pixmap(name, size, *a, **k)

    def spy_icon(name, size, *a, **k):
        requested.append((name, size))
        return real_icon(name, size, *a, **k)

    monkeypatch.setattr(icons, "pixmap", spy_pixmap)
    monkeypatch.setattr(icons, "icon", spy_icon)
    model = DeviceListModel()
    page = SettingsPage(model)
    model.setParent(page)
    qtbot.addWidget(page)
    page.show()
    settle(qtbot)

    assert [s.name for s in Section] == SECTION_NAMES
    assert Section.APPEARANCE.value == "appearance"
    assert page.nav.count() == 6 and page.sections.count() == 6
    assert [page.nav.item(i).text() for i in range(6)] == list(NAV)
    assert not page.nav.item(1).icon().isNull()
    assert ("color", 20) in requested, "icon `color` cỡ 20 cho mục Giao diện"
    from PySide6.QtGui import QColor

    black = QColor("#000000")
    assert not real_pixmap("color", 20, black).isNull(), "icon `color` có thật trong `ui/icons.py`"


def test_language_row_moved_from_general_to_appearance(ui):
    page = ui.page
    general = open_section(page, "GENERAL")
    assert not general.isAncestorOf(page.language_combo), "ô ngôn ngữ không còn ở mục Chung"
    assert not page.language_combo.isVisibleTo(page)
    area = open_section(page, "APPEARANCE")
    settle(ui.qtbot)
    assert area.isAncestorOf(page.language_combo)
    assert page.language_combo.isVisibleTo(page)
    assert page.language_combo is appearance(page).language_combo


def test_page_forwards_language_theme_accent_signals(ui):
    page = ui.page
    open_section(page, "APPEARANCE")
    settle(ui.qtbot)
    combo = page.language_combo
    page.set_language(None, ("vi",))
    index = combo.findData("vi")
    combo.setCurrentIndex(index)
    combo.activated.emit(index)
    mode_buttons(page)[2].click()
    swatch(page, "violet").click()
    swatch(page, "mint").click()
    assert ui.spy.of("language_selected") == ["vi"]
    assert ui.spy.of("theme_selected") == ["dark"]
    assert ui.spy.of("accent_selected") == ["violet", None]


def test_set_values_sets_theme_and_accent_without_signal(ui):
    page = ui.page
    page.set_values(snapshot(theme="dark", accent="rose"))
    settle(ui.qtbot)
    assert checked_mode(page) == "dark"
    assert checked_accent(page) == "rose"
    page.set_values(snapshot(theme="light", accent="khong_co"))
    assert checked_mode(page) == "light"
    assert checked_accent(page) == "mint", "mã lạ hiện màu mặc định"
    assert ui.spy.log == []


def test_set_loaded_toggles_appearance_rows(ui):
    page = ui.page
    controls = [*mode_buttons(page), *swatches(page), page.language_combo]
    page.set_loaded(False)
    assert not any(c.isEnabled() for c in controls)
    page.set_loaded(True)
    assert all(c.isEnabled() for c in controls)


def test_appearance_fits_720x460_without_horizontal_scroll(ui):
    page = ui.page
    page.resize(720, 460)
    settle(ui.qtbot, 60)
    w = open_section(page, "APPEARANCE")
    settle(ui.qtbot, 60)
    areas = [w] if isinstance(w, QScrollArea) else w.findChildren(QScrollArea)
    assert areas, "mục Giao diện là một QScrollArea"
    for area in areas:
        bar = area.horizontalScrollBar()
        assert bar.maximum() == 0 or not bar.isVisible(), "cuộn ngang ở 720x460"
    viewport = areas[0].viewport()
    for c in [*mode_buttons(page), *swatches(page), page.language_combo]:
        left = c.mapTo(viewport, QPoint(0, 0)).x()
        assert left >= 0 and left + c.width() <= viewport.width(), (
            f"{type(c).__name__} {c.accessibleName()!r} tràn ngang ở 720x460"
        )


def test_tab_order_nav_then_checked_mode_checked_swatch_language(ui):
    page = ui.page
    page.set_values(snapshot(theme="light", accent="amber"))
    activate(ui.qtbot, page)
    open_section(page, "APPEARANCE")
    page.nav.setFocus(Qt.FocusReason.TabFocusReason)
    seen = press_tab(3)
    assert seen[0] is mode_buttons(page)[1], "Tab vào nút chế độ đang chọn (Sáng)"
    assert seen[1] is swatch(page, "amber"), "Tab vào ô màu đang chọn"
    assert seen[2] is page.language_combo


# =================================================================================================
# Controller: node giả, host giả
# =================================================================================================


class FakeNode:
    """Node giả: `set_ui_preferences` ghi vào `trace` dùng chung; `fail(op, lỗi)` cho lần gọi tới ném."""

    def __init__(self, trace: list) -> None:
        self.trace = trace
        self.log: list[tuple] = []
        self._fail: dict[str, list] = {}
        self.name = "May-A"
        self.visible = True
        self.language: str | None = None
        self.theme: str | None = None
        self.accent: str | None = None
        self.threads: list[str] = []

    def fail(self, op: str, *errors) -> None:
        self._fail.setdefault(op, []).extend(errors)

    def _enter(self, op: str, *args) -> None:
        self.log.append((op, *args))
        self.threads.append(threading.current_thread().name)
        queue = self._fail.get(op)
        if queue:
            error = queue.pop(0)
            if error is not None:
                raise error

    def snapshot(self):
        self._enter("read_snapshot")
        return snapshot(language=self.language, theme=self.theme, accent=self.accent, name=self.name)

    def set_ui_preferences(self, **kw) -> None:
        self.trace.append(("node", dict(kw)))
        self._enter("set_ui_preferences", kw)
        for key in ("language", "theme", "accent"):
            if key in kw:
                setattr(self, key, kw[key])


class Pending:
    def __init__(self, fn, on_done) -> None:
        self.fn = fn
        self.on_done = on_done
        self.ran = False


class FakeHost(QObject):
    started = Signal()
    stopped = Signal()

    def __init__(self, node: FakeNode, ctx: dict) -> None:
        super().__init__()
        self.node = node
        self.ctx = ctx  # ctx["in_node"]: đang chạy hàm của `call` (giả thread node)
        self.calls: list[Pending] = []
        self.wait_pairing_open = False
        self.running = True

    def call(self, fn, on_done=None) -> None:
        self.calls.append(Pending(fn, on_done))

    def submit(self, fn, on_done=None):
        self.calls.append(Pending(fn, on_done))
        return SimpleNamespace(done=False, cancel=lambda: None)

    def apply_pairing_policy(self, node) -> None:
        pass

    def set_node_name(self, name: str) -> None:
        pass

    def run_calls(self) -> None:
        while True:
            pending = [p for p in self.calls if not p.ran]
            if not pending:
                return
            for p in pending:
                p.ran = True
                self.ctx["in_node"] = True
                try:
                    result = p.fn(self.node)
                except Exception as e:
                    result = e
                finally:
                    self.ctx["in_node"] = False
                if p.on_done is not None:
                    p.on_done(result)


@pytest.fixture
def env(qtbot, tmp_path, monkeypatch, fake_registry):
    from teleport.app import autostart
    from teleport.ui import i18n, theme
    from teleport.ui import settings as ui_settings
    from teleport.ui.radar import DeviceListModel
    from teleport.ui.settings import SettingsController, SettingsPage

    exe = tmp_path / "app" / "Teleport.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(autostart, "_frozen", lambda: True)
    monkeypatch.setattr(autostart, "_executable", lambda: str(exe))
    monkeypatch.setattr(ui_settings, "read_snapshot", lambda node: node.snapshot())
    monkeypatch.setattr(i18n, "apply_preference", lambda code: None)

    trace: list = []
    ctx = {"in_node": False}
    real_set_preference = theme.set_preference

    def spy_set_preference(app, mode, accent):
        if ctx["in_node"]:
            trace.append(("apply-tren-thread-node",))
        trace.append(("apply", theme.Mode(mode), theme.accent_from_setting(accent)))
        return real_set_preference(app, mode, accent)

    monkeypatch.setattr(theme, "set_preference", spy_set_preference)

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    model = DeviceListModel()
    page = SettingsPage(model)
    model.setParent(page)
    qtbot.addWidget(page)
    page.resize(980, 660)
    page.show()
    qtbot.waitExposed(page)
    c = SettingsController(page, cfg, message_parent=lambda: page, open_url=lambda _u: None)
    node = FakeNode(trace)
    host = FakeHost(node, ctx)
    c.attach(host)

    def start() -> None:
        c.on_started(host)
        host.run_calls()
        settle(qtbot)
        page.show_section(ui_settings.Section.APPEARANCE)
        settle(qtbot)

    ns = SimpleNamespace(page=page, c=c, host=host, node=node, start=start, trace=trace, qtbot=qtbot)
    yield ns
    with contextlib.suppress(Exception):
        c.shutdown()


def node_writes(trace: list) -> list:
    return [e[1] for e in trace if e[0] == "node"]


def applies(trace: list) -> list:
    return [e[1:] for e in trace if e[0] == "apply"]


def notice_text(page) -> str | None:
    bar = page.info_bar
    if bar.isHidden():
        return None
    return " ".join(t for t in (bar.title, bar.text) if t)


# =================================================================================================
# Controller
# =================================================================================================


def test_read_snapshot_reads_theme_and_accent_from_node_settings():
    from teleport.app.settings import Settings
    from teleport.ui import settings as ui_settings

    node = SimpleNamespace(
        name="May-A",
        visible=True,
        settings=Settings(theme="dark", accent="ocean"),
        devices=lambda: [],
        device_settings=lambda _id: None,
    )
    snap = ui_settings.read_snapshot(node)
    assert (snap.theme, snap.accent) == ("dark", "ocean")


@pytest.mark.parametrize(("index", "value", "mode"), [(2, "dark", "DARK"), (1, "light", "LIGHT")])
def test_theme_saved_on_node_then_applied(env, index, value, mode):
    from teleport.ui import theme

    env.start()
    env.trace.clear()
    mode_buttons(env.page)[index].click()
    settle(env.qtbot)
    assert applies(env.trace) == [], "chưa lưu xong thì chưa áp"
    assert theme.current_mode() is theme.Mode.SYSTEM
    env.host.run_calls()
    settle(env.qtbot)
    assert env.trace == [
        ("node", {"theme": value}),
        ("apply", theme.Mode[mode], theme.DEFAULT_ACCENT),
    ], "lưu trên node trước, áp sau; chỉ đổi trường theme"
    assert theme.current_mode() is theme.Mode[mode]


def test_theme_system_choice_saves_none(env):
    from teleport.ui import theme

    env.node.theme = "dark"
    env.start()
    env.trace.clear()
    mode_buttons(env.page)[0].click()
    env.host.run_calls()
    settle(env.qtbot)
    assert node_writes(env.trace) == [{"theme": None}]
    assert applies(env.trace)[-1][0] is theme.Mode.SYSTEM


def test_accent_saved_on_node_then_applied_keeps_mode(env):
    from teleport.ui import theme

    env.node.theme = "dark"
    env.start()
    assert theme.current_mode() is theme.Mode.DARK
    env.trace.clear()
    swatch(env.page, "violet").click()
    settle(env.qtbot)
    assert applies(env.trace) == []
    env.host.run_calls()
    settle(env.qtbot)
    assert env.trace == [("node", {"accent": "violet"}), ("apply", theme.Mode.DARK, "violet")]
    assert theme.current_accent() == "violet"
    assert theme.current_mode() is theme.Mode.DARK, "đổi màu nhấn giữ chế độ đang chọn"


def test_mint_choice_saves_none_and_applies_default(env):
    from teleport.ui import theme

    env.node.accent = "rose"
    env.start()
    assert theme.current_accent() == "rose"
    env.trace.clear()
    swatch(env.page, "mint").click()
    env.host.run_calls()
    settle(env.qtbot)
    assert node_writes(env.trace) == [{"accent": None}]
    assert applies(env.trace) == [(theme.Mode.SYSTEM, theme.DEFAULT_ACCENT)]


@pytest.mark.parametrize("field", ["theme", "accent"])
def test_save_error_reverts_control_no_apply_notice(env, field):
    from teleport.app.settings import SettingsSaveError
    from teleport.ui import theme

    env.node.theme = "light"
    env.node.accent = "amber"
    env.start()
    before = (theme.current_mode(), theme.current_accent())
    palette = QApplication.palette()
    env.trace.clear()
    env.node.fail("set_ui_preferences", SettingsSaveError("x"))
    if field == "theme":
        mode_buttons(env.page)[2].click()
        assert checked_mode(env.page) == "dark"
    else:
        swatch(env.page, "graphite").click()
        assert checked_accent(env.page) == "graphite"
    env.host.run_calls()
    settle(env.qtbot)
    assert applies(env.trace) == [], "lưu lỗi: không áp chủ đề"
    assert (theme.current_mode(), theme.current_accent()) == before
    assert QApplication.palette() == palette
    assert checked_mode(env.page) == "light", "control trả về giá trị đã xác nhận"
    assert checked_accent(env.page) == "amber"
    assert notice_text(env.page) is not None and SETTINGS_ERROR in notice_text(env.page)


def test_snapshot_shows_and_applies_saved_theme(env):
    from teleport.ui import theme

    env.node.theme = "dark"
    env.node.accent = "ocean"
    env.start()
    assert checked_mode(env.page) == "dark"
    assert checked_accent(env.page) == "ocean"
    assert theme.current_mode() is theme.Mode.DARK
    assert theme.current_accent() == "ocean"


def test_snapshot_equal_to_applied_does_not_reapply(env):
    env.start()  # đã lưu: theo HĐH + mặc định = đang áp
    assert applies(env.trace) == []
    assert node_writes(env.trace) == [], "đọc ảnh chụp không ghi gì"


def test_failed_then_success_applies_only_success(env):
    from teleport.app.settings import SettingsSaveError
    from teleport.ui import theme

    env.start()
    env.trace.clear()
    env.node.fail("set_ui_preferences", SettingsSaveError("x"), None)
    mode_buttons(env.page)[2].click()
    env.host.run_calls()
    settle(env.qtbot)
    mode_buttons(env.page)[1].click()
    env.host.run_calls()
    settle(env.qtbot)
    assert applies(env.trace) == [(theme.Mode.LIGHT, theme.DEFAULT_ACCENT)]
    assert checked_mode(env.page) == "light"


def test_retranslate_page_keeps_theme_and_accent(ui):
    from PySide6.QtCore import QTranslator

    class Prefix(QTranslator):
        def isEmpty(self):
            return False

        def translate(self, context, source_text, disambiguation=None, n=-1):
            return "[x]" + source_text if source_text else source_text

    page = ui.page
    page.set_values(snapshot(theme="dark", accent="violet"))
    translator = Prefix()
    QCoreApplication.installTranslator(translator)
    try:
        ui.qtbot.waitUntil(lambda: page.nav.item(1).text().startswith("[x]"), timeout=3000)
        assert checked_mode(page) == "dark"
        assert checked_accent(page) == "violet"
    finally:
        QCoreApplication.removeTranslator(translator)
    ui.qtbot.waitUntil(lambda: page.nav.item(1).text() == NAV[1], timeout=3000)
    assert ui.spy.log == []
