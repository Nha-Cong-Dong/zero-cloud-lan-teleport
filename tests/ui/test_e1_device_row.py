"""Hàng thiết bị đã ghép đôi của Cài đặt (`TrustedDeviceRow`) tự chọn bố cục theo bề ngang (spec E1 mục 2.1,
việc 3).

Viết từ spec, không từ code. Kiểu bố cục đọc từ **hình học** của các control so với cột chữ (tên + “HĐH ·
trạng thái”), không đọc thuộc tính bên trong:

- **ONE_LINE**: ba control (“Hỏi trước khi nhận”, “Đồng bộ clipboard”, “Hủy ghép đôi…”) cùng một hàng, cạnh
  cột chữ (chồng lên khoảng cao của cột chữ, nằm bên phải mép trái của nó); cột tên ≥
  `widgets.LABEL_MIN_WIDTH`.
- **BELOW**: ba control cùng một hàng, dưới cột chữ, control đầu thẳng mép trái cột chữ (thụt bằng avatar +
  khoảng cách).
- **STACKED**: mỗi control một dòng theo thứ tự Hỏi → Đồng bộ → Hủy ghép đôi, dưới cột chữ, cùng thẳng mép
  trái cột chữ, không giãn ngang (bề ngang = `sizeHint`).

Kiểm: quét bề ngang 400–1400 px ở cả tiếng Việt và English: không control nào bị cắt
(`sizeHint().width() ≤ width()`), mọi control nằm trong hàng, `minimumSizeHint` của hàng không vượt bề
ngang (không làm vùng cuộn của Cài đặt cuộn ngang), kiểu là một trong ba kiểu trên, rộng hơn thì kiểu
không “phức tạp” hơn (ONE_LINE < BELOW < STACKED), BELOW chỉ khi ba control vừa một hàng; kiểu chỉ phụ
thuộc bề ngang (cùng hàng kéo lên rồi kéo xuống cho cùng kết quả với hàng mới dựng) và ổn định (thêm
vòng sự kiện không đổi hình học). 1400 px tiếng Việt → ONE_LINE; cửa sổ 720×460 English (mục “Thiết bị”
thật) → STACKED khi ba control không vừa một hàng dưới tên (xem “Font”). Đổi kiểu không dựng lại widget:
cùng đối tượng, tiêu điểm giữ nguyên, chuỗi Tab trong hàng luôn Hỏi → Đồng bộ → Hủy ghép đôi. Đổi ngôn
ngữ lúc chạy đổi kiểu và đổi lại quay về kiểu cũ; đổi font (`FontChange`) cũng tính lại kiểu (không
cắt chữ với font to). Quét không cắt/không tràn chạy thêm với theme thật của app (sáng, tối), vì viền khung
của stylesheet lấy bớt bề ngang.

Font: trên `offscreen`, khi một test nào đó trong cùng tiến trình đã nạp font điểm nhấn (`fonts.install`,
ví dụ `test_translations.py::test_assets_ok_*`, `test_snapshots.py`), font thay thế của chữ thân đổi theo và
không trở lại sau `fonts._reset()` (đo được: “Ask before receiving” 264 px → 141 px). Số đo của spec 2.1
lấy khi chưa nạp. Nên điểm cố định “720×460 English → STACKED” được kiểm theo luật “kiểu đầu tiên vừa”:
ba control không vừa một hàng dưới tên (lề thụt + tổng `sizeHint` > bề ngang hàng, đúng trường hợp spec
đo) thì phải STACKED; vừa thì BELOW hay STACKED đều theo luật. Test cuối file quét lại sau khi nạp font
(như lúc app chạy), chỉ kiểm không cắt chữ, không tràn.
"""

from __future__ import annotations

import hashlib
from enum import IntEnum

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton, QVBoxLayout, QWidget
from ui_scenes import SCENES

WIDTHS = list(range(400, 1401, 10))
CONTROL_NAMES = ("askBeforeReceive", "syncClipboard", "unpairButton")
TOL = 1  # px, làm tròn của bố cục


class Kind(IntEnum):
    ONE_LINE = 0
    BELOW = 1
    STACKED = 2


# =================================================================================================
# Hàm phụ trợ
# =================================================================================================


def settle(rounds: int = 4) -> None:
    for _ in range(rounds):
        QApplication.processEvents()


def view(name: str = "PC-Nha", *, connected: bool = True, os_name: str = "windows"):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=hashlib.sha256(name.encode()).hexdigest()[:32],
        name=name,
        os=os_name,
        paired=True,
        online=True,
        connected=connected,
        addresses=("192.168.1.20",),
        port=47474,
    )


class Host:
    """Một `TrustedDeviceRow` trong khung có bề ngang cố định (không lề), hiện nhưng không lên màn hình."""

    def __init__(self, qtbot, width: int) -> None:
        from teleport.ui.settings import TrustedDeviceRow

        self.frame = QWidget()
        qtbot.addWidget(self.frame)
        layout = QVBoxLayout(self.frame)
        layout.setContentsMargins(0, 0, 0, 0)
        self.row = TrustedDeviceRow(view(), self.frame)
        layout.addWidget(self.row)
        layout.addStretch(1)
        self.frame.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.frame.setFixedWidth(width)
        self.frame.resize(width, 600)
        self.frame.show()
        settle()

    def set_width(self, width: int) -> None:
        self.frame.setFixedWidth(width)
        self.frame.resize(width, 600)
        settle()


def controls(row) -> list:
    found = [
        row.findChild(QCheckBox, CONTROL_NAMES[0]),
        row.findChild(QCheckBox, CONTROL_NAMES[1]),
        row.findChild(QPushButton, CONTROL_NAMES[2]),
    ]
    assert all(c is not None for c in found), "thiếu control (tên đối tượng không được đổi, spec 2.1)"
    return found


def rect_in(widget, row) -> QRect:
    return QRect(widget.mapTo(row, QPoint(0, 0)), widget.size())


def text_block(row) -> QRect:
    name = row.findChild(QLabel, "trustedDeviceName")
    detail = row.findChild(QLabel, "trustedDeviceDetail")
    assert name is not None and detail is not None
    block = rect_in(name, row)
    if detail.isVisible():
        block = block.united(rect_in(detail, row))
    return block


def kind_of(row) -> Kind | None:
    """Kiểu bố cục đọc từ hình học (docstring module); None nếu không khớp kiểu nào."""
    text = text_block(row)
    rs = [rect_in(c, row) for c in controls(row)]
    same_line = all(abs(r.center().y() - rs[0].center().y()) <= 2 for r in rs) and all(
        rs[i].right() < rs[i + 1].left() for i in range(2)
    )
    aligned = all(abs(r.left() - text.left()) <= TOL for r in rs)
    below = rs[0].top() >= text.bottom() - TOL
    if same_line and rs[0].top() < text.bottom() and rs[0].left() > text.left():
        return Kind.ONE_LINE
    if same_line and below and abs(rs[0].left() - text.left()) <= TOL:
        return Kind.BELOW
    stacked = all(rs[i].bottom() < rs[i + 1].top() for i in range(2))
    if stacked and below and aligned:
        return Kind.STACKED
    return None


def problems(row) -> list[str]:
    """Lỗi tràn/cắt của hàng ở bề ngang hiện tại."""
    out = []
    width = row.width()
    for c in controls(row):
        if c.sizeHint().width() > c.width():
            out.append(f"{c.objectName()} cần {c.sizeHint().width()} px, có {c.width()} px")
        r = rect_in(c, row)
        if r.left() < 0 or r.right() >= width + TOL:
            out.append(f"{c.objectName()} ra ngoài hàng: x {r.left()}…{r.right()}, hàng rộng {width}")
    if row.minimumSizeHint().width() > width:
        out.append(f"minimumSizeHint của hàng {row.minimumSizeHint().width()} px > {width} px")
    return out


def geometry(row) -> list[tuple[int, int, int, int]]:
    return [rect_in(c, row).getRect() for c in controls(row)]


def tab_stops_after(start, count: int) -> list:
    """`count` điểm dừng Tab kế tiếp sau `start` theo `nextInFocusChain` (hiện, bật, có `TabFocus`)."""
    out = []
    w = start.nextInFocusChain()
    for _ in range(500):
        if w is start or len(out) == count:
            break
        if (
            w.isVisible()
            and w.isEnabled()
            and w.focusPolicy() & Qt.FocusPolicy.TabFocus
            and w.focusProxy() is None
        ):
            out.append(w)
        w = w.nextInFocusChain()
    return out


def set_language(code: str) -> None:
    from teleport.ui import i18n

    i18n.set_language(code)
    settle()
    assert i18n.current_language() == code, f"không đổi được sang {code} (chưa có teleport_{code}.qm?)"


@pytest.fixture
def sweep(qtbot, request):
    """Kiểu bố cục của hàng mới dựng ở mỗi bề ngang của `WIDTHS`, theo ngôn ngữ: {lang: {width: Kind}}."""
    cache: dict[str, dict[int, Kind | None]] = {}

    def run(lang: str) -> dict[int, Kind | None]:
        if lang not in cache:
            set_language(lang)
            result = {}
            for width in WIDTHS:
                host = Host(qtbot, width)
                result[width] = kind_of(host.row)
                host.frame.close()
            cache[lang] = result
        return cache[lang]

    return run


# =================================================================================================
# Quét bề ngang
# =================================================================================================


@pytest.mark.slow
@pytest.mark.parametrize("lang", ["vi", "en"])
def test_row_never_clips_or_overflows_400_to_1400(qtbot, lang):
    set_language(lang)
    host = Host(qtbot, WIDTHS[0])
    bad = []
    for width in WIDTHS:
        host.set_width(width)
        assert host.row.width() == width
        bad += [f"{width}px: {p}" for p in problems(host.row)]
        if kind_of(host.row) is None:
            bad.append(f"{width}px: bố cục không khớp kiểu nào: {geometry(host.row)}")
    assert bad == []


@pytest.mark.slow
@pytest.mark.parametrize("lang", ["vi", "en"])
def test_wider_row_never_uses_a_more_stacked_kind(sweep, lang):
    kinds = sweep(lang)
    assert None not in kinds.values(), {w: k for w, k in kinds.items() if k is None}
    order = [kinds[w] for w in WIDTHS]
    worse = [(WIDTHS[i], order[i], order[i + 1]) for i in range(len(order) - 1) if order[i + 1] > order[i]]
    assert worse == []


@pytest.mark.slow
@pytest.mark.parametrize("lang", ["vi", "en"])
def test_kind_depends_only_on_width_not_on_history(qtbot, sweep, lang):
    """Không ngưỡng trễ (spec 2.1): cùng một hàng kéo rộng dần rồi hẹp dần cho đúng kiểu của hàng mới dựng."""
    fresh = sweep(lang)
    set_language(lang)
    host = Host(qtbot, WIDTHS[0])
    seen = []
    for width in [*WIDTHS, *reversed(WIDTHS)]:
        host.set_width(width)
        if kind_of(host.row) != fresh[width]:
            seen.append((width, kind_of(host.row), fresh[width]))
    assert seen == []


@pytest.mark.slow
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("lang", ["vi", "en"])
def test_row_never_clips_with_app_theme_400_to_1400(qtbot, ui_theme, lang, dark):
    """Như quét trên nhưng với giao diện thật của app (`theme.apply`: Fusion + stylesheet). Stylesheet thêm
    viền khung (`QFrame[surface="raised"]` có `frameWidth` 1) nên hàng mất 1–2 px so với khi không có
    theme: control không được bị cắt, hàng không tràn, kiểu vẫn khớp, và ở kiểu ONE_LINE cột tên vẫn ≥
    `LABEL_MIN_WIDTH`."""
    from teleport.ui.widgets import LABEL_MIN_WIDTH

    ui_theme(dark)
    set_language(lang)
    host = Host(qtbot, WIDTHS[0])
    bad = []
    one_line = 0
    for width in WIDTHS:
        host.set_width(width)
        assert host.row.width() == width
        bad += [f"{width}px: {p}" for p in problems(host.row)]
        kind = kind_of(host.row)
        if kind is None:
            bad.append(f"{width}px: bố cục không khớp kiểu nào: {geometry(host.row)}")
        elif kind is Kind.ONE_LINE:
            one_line += 1
            name = host.row.findChild(QLabel, "trustedDeviceName")
            if name.width() < LABEL_MIN_WIDTH:
                bad.append(f"{width}px: cột tên ONE_LINE {name.width()} px < {LABEL_MIN_WIDTH} px")
    assert bad == []
    assert one_line, "không bề ngang nào tới một dòng khi có theme"


@pytest.mark.parametrize("lang", ["vi", "en"])
def test_one_line_keeps_name_column_at_least_label_min_width(qtbot, lang):
    from teleport.ui.widgets import LABEL_MIN_WIDTH

    set_language(lang)
    host = Host(qtbot, WIDTHS[0])
    checked = 0
    for width in WIDTHS:
        host.set_width(width)
        if kind_of(host.row) is Kind.ONE_LINE:
            checked += 1
            name = host.row.findChild(QLabel, "trustedDeviceName")
            assert name.width() >= LABEL_MIN_WIDTH, (width, name.width())
    assert checked, "không bề ngang nào tới một dòng"


@pytest.mark.parametrize("lang", ["vi", "en"])
def test_below_kind_only_when_three_controls_fit_one_line(qtbot, lang):
    """Kiểu BELOW: tổng `sizeHint` ba control cộng lề thụt không vượt bề ngang hàng (spec 2.1 ý 2)."""
    set_language(lang)
    host = Host(qtbot, WIDTHS[0])
    for width in WIDTHS:
        host.set_width(width)
        if kind_of(host.row) is Kind.BELOW:
            indent = text_block(host.row).left()
            need = indent + sum(c.sizeHint().width() for c in controls(host.row))
            assert need <= width, (width, need)


def test_stacked_controls_are_left_aligned_and_not_stretched(qtbot):
    set_language("en")
    host = Host(qtbot, WIDTHS[0])
    for width in range(WIDTHS[0], 300, -10):  # hẹp dần tới khi xếp dọc (font điểm nhấn làm chữ hẹp hơn)
        host.set_width(width)
        if kind_of(host.row) is Kind.STACKED:
            break
    assert kind_of(host.row) is Kind.STACKED, "English hẹp tới 310 px vẫn không xếp dọc"
    assert problems(host.row) == []
    for c in controls(host.row):
        assert c.width() == c.sizeHint().width(), (c.objectName(), c.width(), c.sizeHint().width())


def test_layout_is_stable_after_more_events(qtbot):
    """Không vòng lặp đổi kiểu: thêm nhiều vòng sự kiện, hình học không đổi, ở mọi bề ngang của lượt quét."""
    set_language("en")
    host = Host(qtbot, WIDTHS[0])
    moved = []
    for width in WIDTHS[::5]:
        host.set_width(width)
        before = geometry(host.row)
        settle(10)
        if geometry(host.row) != before:
            moved.append(width)
    assert moved == []


# =================================================================================================
# Điểm cố định của spec
# =================================================================================================


def test_vietnamese_1400px_is_one_line(qtbot):
    set_language("vi")
    host = Host(qtbot, 1400)
    assert kind_of(host.row) is Kind.ONE_LINE, geometry(host.row)


def test_english_settings_devices_at_720x460_stacks_every_row(qtbot, qapp):
    """Mục “Thiết bị” thật của Cài đặt ở cửa sổ nhỏ nhất, English: không cắt chữ; ba control không vừa một
    hàng dưới tên thì xếp dọc (xem “Font” ở docstring module)."""
    from teleport.ui.settings import TrustedDeviceRow

    set_language("en")
    window = SCENES["settings_devices"](qtbot)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.resize(720, 460)
    window.show()
    settle()
    rows = [r for r in window.findChildren(TrustedDeviceRow) if r.isVisible()]
    assert rows, "cảnh settings_devices phải có hàng thiết bị"
    assert [p for r in rows for p in problems(r)] == []
    for r in rows:
        need = text_block(r).left() + sum(c.sizeHint().width() for c in controls(r))
        expected = {Kind.STACKED} if need > r.width() else {Kind.BELOW, Kind.STACKED}
        assert kind_of(r) in expected, (r.width(), need, geometry(r))


# =================================================================================================
# Đổi kiểu không dựng lại widget
# =================================================================================================


def _width_for(sweep, lang: str, kind: Kind) -> int:
    widths = [w for w, k in sweep(lang).items() if k is kind]
    assert widths, f"{lang}: không có bề ngang nào cho kiểu {kind.name} trong {WIDTHS[0]}–{WIDTHS[-1]} px"
    return widths[len(widths) // 2]


@pytest.mark.slow
@pytest.mark.parametrize("lang", ["vi", "en"])
def test_tab_order_inside_row_is_ask_sync_unpair_in_every_kind(qtbot, sweep, lang):
    kinds = {k for k in sweep(lang).values() if k is not None}
    set_language(lang)
    for kind in sorted(kinds):
        host = Host(qtbot, _width_for(sweep, lang, kind))
        assert kind_of(host.row) is kind
        ask, sync, unpair = controls(host.row)
        assert tab_stops_after(ask, 2) == [sync, unpair], kind.name


@pytest.mark.slow
def test_kind_change_keeps_same_widgets_and_focus(qtbot, sweep):
    """Đổi kiểu bằng đổi hướng layout, không dựng lại: cùng đối tượng, tiêu điểm vẫn ở control đang giữ."""
    one_line = _width_for(sweep, "vi", Kind.ONE_LINE)
    narrow = WIDTHS[0]
    set_language("vi")
    host = Host(qtbot, one_line)
    before = controls(host.row)
    sync = before[1]
    sync.setFocus(Qt.FocusReason.OtherFocusReason)
    settle()
    assert host.frame.focusWidget() is sync
    host.set_width(narrow)
    assert kind_of(host.row) is not Kind.ONE_LINE
    assert controls(host.row) == before
    assert host.frame.focusWidget() is sync
    host.set_width(one_line)
    assert kind_of(host.row) is Kind.ONE_LINE
    assert host.frame.focusWidget() is sync


# =================================================================================================
# Tính lại kiểu khi đổi ngôn ngữ, font
# =================================================================================================


@pytest.mark.slow
def test_live_language_switch_changes_kind_and_back(qtbot, sweep):
    vi, en = sweep("vi"), sweep("en")
    differ = [w for w in WIDTHS if vi[w] != en[w] and None not in (vi[w], en[w])]
    assert differ, "không có bề ngang nào mà English đổi kiểu so với tiếng Việt: không kiểm được đổi lúc chạy"
    width = differ[len(differ) // 2]
    set_language("vi")
    host = Host(qtbot, width)
    assert kind_of(host.row) is vi[width]
    set_language("en")
    assert kind_of(host.row) is en[width], f"{width}px: đổi sang English không tính lại kiểu"
    assert problems(host.row) == []
    set_language("vi")
    assert kind_of(host.row) is vi[width], f"{width}px: đổi lại tiếng Việt không quay về kiểu cũ"


def test_font_change_recomputes_kind_without_clipping(qtbot, sweep):
    """`FontChange`: font to gấp đôi ở bề ngang vốn một dòng → không control nào bị cắt hay ra ngoài hàng."""
    width = _width_for(sweep, "vi", Kind.ONE_LINE)
    set_language("vi")
    host = Host(qtbot, width)
    font = host.row.font()
    font.setPointSizeF(font.pointSizeF() * 2)
    host.row.setFont(font)
    settle()
    assert problems(host.row) == []
    assert kind_of(host.row) is not None


@pytest.mark.slow
@pytest.mark.parametrize("lang", ["vi", "en"])
def test_row_never_clips_with_accent_font_installed(qtbot, lang):
    """Như test trên, với font điểm nhấn đã nạp như lúc app chạy (`ui.app` gọi `fonts.install`)."""
    from teleport.ui import fonts

    fonts.install()
    try:
        set_language(lang)
        host = Host(qtbot, WIDTHS[0])
        bad = []
        for width in WIDTHS:
            host.set_width(width)
            bad += [f"{width}px: {p}" for p in problems(host.row)]
            if kind_of(host.row) is None:
                bad.append(f"{width}px: bố cục không khớp kiểu nào: {geometry(host.row)}")
        assert bad == []
    finally:
        fonts._reset()
