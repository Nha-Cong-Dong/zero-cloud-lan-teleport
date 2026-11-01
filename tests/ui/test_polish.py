"""PR-7 “Giao diện trông như một app” (spec-pr7: B3, B4, B6, B7).

Viết từ spec, không từ code. Mọi widget dựng riêng (không `main()`, không socket): `TransfersView`,
`MainWindow`, `DeviceListModel`, `PairingController` với host giả, `OfferPrompts`,
`PairingDialog`, `PairingProgressDialog`; nối dây `ui/app.py` kiểm bằng `GuiApp` không chạy node.

Nhóm:

- B3: thẻ mới lên đầu (`rows()`, vị trí y, thứ tự Tab); dòng phụ “Đã lưu thành …” khi tên lưu khác
  tên gốc; thanh tiến độ ẩn khi kết thúc khác DONE; tỉ lệ radar : lượt = 1:1; câu trống căn giữa.
- B4: ghép đôi xong / hủy ghép đôi xong → `notice`, không hộp; lỗi vẫn hộp; hộp lỗi đang mở thì đóng.
- B6: câu chữ radar trống, UNREACHABLE, PEER_BUSY / REJECTED.
- B7: `default_size`, tiêu đề ẩn khi RUNNING, không nhãn nào chọn chữ bằng bàn phím, tiêu điểm khi
  RUNNING, thứ tự của `DeviceListModel` (cả property test), icon/font của hàng, nút chính, Enter an
  toàn, tương phản, bề rộng hộp mã. Spec B2 gỡ `RadarView`: các ca radar trống / thanh bận / nút “Nhập
  IP thủ công” bỏ; ca kéo thả khi hàng dời chỗ chuyển sang `test_conversations_drop.py` (T-D4).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import re
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PySide6.QtCore import QObject, QPoint, QSize, Qt, Signal
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox

PEER_ID = "3f2a9c1e" + "ab" * 12
TRANSFER_PEER_ID = "a71c04d2" + "e" * 24
PEER = "PC-Nha"
NOW = 1000.0

# ---- Câu chữ của spec ----------------------------------------------------------------------------
DONE_DETAIL = "Đã kiểm tra toàn vẹn"
SAVED_AS = "Đã lưu thành {name} · Đã kiểm tra toàn vẹn"
SAVED_MANY = "Đã lưu {n} file với tên mới · Đã kiểm tra toàn vẹn"
NO_DEVICES = (
    "Chưa thấy thiết bị nào. Hãy mở Teleport trên máy kia, kiểm tra hai máy dùng chung một mạng Wi-Fi "
    "và tường lửa của Windows cho phép Teleport."
)
UNREACHABLE_OLD = (
    "Thấy {name} nhưng không kết nối được. Mạng Wi-Fi này có thể đang chặn các thiết bị nói chuyện với "
    "nhau (hay gặp ở quán cà phê, khách sạn)."
)
HOTSPOT_HINT = "Hãy thử dùng điểm phát Wi-Fi từ điện thoại."
NOT_READY = "{name} đang bận hoặc vừa có một lần ghép đôi không thành. Hãy chờ một lát rồi thử lại."
OUT_PAIRED = "Đã ghép đôi với “{x}”."
UNPAIRED = "Đã hủy ghép đôi với “{name}”."


def fill(template: str, **values: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), template)


# =================================================================================================
# Dữ liệu lượt truyền
# =================================================================================================


def tkey(direction: str = "receive", transfer_id: str = "c" * 32, device_id: str = TRANSFER_PEER_ID):
    from teleport.core.transfer import Direction, TransferKey

    return TransferKey(device_id, Direction(direction), transfer_id)


def tinfo(k, status: str = "transferring", files=(("video.mp4", 8 * 2**20),), done: int = 0):
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import TransferInfo, TransferStatus

    entries = tuple(FileEntry(name, size, 1_700_000_000_000) for name, size in files)
    return TransferInfo(
        key=k,
        status=TransferStatus(status),
        files=entries,
        names=tuple(name for name, _ in files),
        file_index=0,
        done_bytes=done,
        total_bytes=sum(s for _, s in files),
        unsynced_bytes=0,
    )


def status_changed(k, status: str = "transferring", **kw):
    from teleport.app.transfers import TransferStatusChanged

    return TransferStatusChanged(tinfo(k, status, **kw))


def progress(k, done: int, files=(("video.mp4", 8 * 2**20),)):
    from teleport.app.transfers import TransferProgress

    return TransferProgress(tinfo(k, files=files, done=done), 2**20, 5.0, shown_bytes=done, idle_seconds=0.0)


def finished(k, result: str, files=(("video.mp4", 8 * 2**20),), *, by_peer: bool = False):
    from teleport.app.transfers import TransferFinished, TransferResult

    return TransferFinished(
        info=tinfo(k, "verifying" if result == "done" else "transferring", files=files),
        result=TransferResult(result),
        by_peer=by_peer,
        reason=None,
        file_index=0,
        reject=None,
    )


def file_done(k, index: int, name: str, path: Path | None):
    from teleport.app.transfers import FileDone

    return FileDone(key=k, file_index=index, name=name, path=path)


def received(k, index: int, path: Path):
    """`FileDone` bên nhận như app thật phát: `name` ĐÃ là tên lưu cuối (== `path.name`, xem
    `core.transfer.FileVerified`); tên gốc chỉ nằm ở `TransferInfo.names` của lượt."""
    return file_done(k, index, path.name, path)


def offer_received(k, files=(("video.mp4", 8 * 2**20),)):
    from teleport.app.transfers import OfferReceived
    from teleport.core.messages import FileEntry

    entries = tuple(FileEntry(name, size, 1_700_000_000_000) for name, size in files)
    return OfferReceived(
        key=k,
        device_name=PEER,
        files=entries,
        names=tuple(n for n, _ in files),
        total_bytes=sum(s for _, s in files),
        auto_accepted=False,
    )


# =================================================================================================
# Dữ liệu ghép đôi, host giả
# =================================================================================================


def make_prompt(*, sid: int = 7, role: str = "responder", name: str = "MacBook"):
    from teleport.app.pairing import PairingPrompt
    from teleport.core.pairing import Role

    return PairingPrompt(
        session_id=sid,
        role=Role(role),
        address="192.168.1.5",
        peer_name=name,
        peer_device_id=PEER_ID,
        code="905617",
        expires_at=NOW + 60.0,
        recent_codes=1,
        known_name=None,
    )


def make_trusted(name: str = "May-B", device_id: str = PEER_ID):
    from teleport.trust.store import TrustedDevice

    return TrustedDevice(
        device_id=device_id,
        name=name,
        fingerprint="0" * 64,
        cert_pem="-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n",
        paired_at=datetime.datetime(2026, 10, 29, tzinfo=datetime.UTC),
    )


def make_finished(
    outcome: str,
    *,
    sid: int | None = 7,
    role: str = "initiator",
    address: str = "192.168.1.5",
    port: int | None = 47474,
    name: str | None = "MacBook",
    code_shown: bool = True,
):
    from teleport.app.pairing import PairingFinished, PairingOutcome
    from teleport.core.pairing import Role

    o = PairingOutcome(outcome)
    device = make_trusted(name or "May-B") if o is PairingOutcome.PAIRED else None
    return PairingFinished(
        session_id=sid,
        role=Role(role),
        outcome=o,
        address=address,
        port=port,
        peer_name=name,
        peer_device_id=PEER_ID,
        peer_reason=None,
        device=device,
        known_name=None,
        code_shown=code_shown,
    )


def peer_label(name: str | None, device_id: str = PEER_ID) -> str:
    from teleport.ui.texts import peer_label as pl

    return pl(name, device_id)


class FakePairing:
    async def pair_with(self, addresses, port):
        return None

    def decide(self, session_id, accepted) -> bool:
        return True


class FakeNode:
    def __init__(self) -> None:
        self.views: tuple = ()
        self.pairing = FakePairing()
        self.unpair_error: BaseException | None = None

    def devices(self):
        return self.views

    def connect_hint(self, device_id, addresses, port) -> None:
        pass

    async def unpair(self, device_id, *, clear_history=False):  # D2: hộp hủy ghép đôi truyền clear_history
        if self.unpair_error is not None:
            raise self.unpair_error
        return make_trusted(device_id=device_id)


class FakeHost(QObject):
    """Host giả như `NodeHost`: `call` / `submit` ghi lại, test tự chạy."""

    pairing_prompt = Signal(object)
    pairing_finished = Signal(object)

    def __init__(self, node: FakeNode) -> None:
        super().__init__()
        self.node = node
        self.calls: list = []
        self.submits: list = []

    def call(self, fn, on_done=None) -> None:
        self.calls.append((fn, on_done))

    def submit(self, fn, on_done=None):
        job = SimpleNamespace(done=False, cancel=lambda: None)
        self.submits.append((fn, on_done))
        return job

    def run_last_submit(self):
        fn, on_done = self.submits.pop()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(fn(self.node))
        except Exception as e:
            result = e
        finally:
            loop.close()
        if on_done is not None:
            on_done(result)
        return result


def visible_boxes() -> list:
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()]


def closed(widget) -> bool:
    import shiboken6

    return widget is None or not shiboken6.isValid(widget) or not widget.isVisible()


@pytest.fixture(autouse=True)
def no_exec(monkeypatch):
    """Không hộp nào được mở bằng `exec()` (chặn vòng sự kiện)."""
    calls: list[str] = []

    def fake(self, *a, **k):
        calls.append(type(self).__name__)
        return 0

    monkeypatch.setattr(QDialog, "exec", fake)
    monkeypatch.setattr(QMessageBox, "exec", fake)
    yield calls
    assert not calls, f"hộp mở bằng exec(): {calls}"


@pytest.fixture
def ctl(qtbot):
    """Controller ghép đôi nối với host giả; ghi lại `message` và `notice`."""
    from teleport.ui.pairing import PairingController

    made: list = []

    def make(*, visible: bool = True):
        node = FakeNode()
        host = FakeHost(node)
        notes: list[tuple[str, str]] = []
        c = PairingController(
            clock=lambda: NOW,
            notify=lambda title, text: notes.append((title, text)),
            window_visible=lambda: visible,
        )
        c.set_host(host)
        ns = SimpleNamespace(c=c, host=host, node=node, messages=[], notices=[], notes=notes)
        c.message.connect(ns.messages.append)
        c.notice.connect(ns.notices.append)
        made.append(ns)
        return ns

    yield make

    for ns in made:
        with contextlib.suppress(Exception):
            ns.c.close_all()
    for box in visible_boxes():
        with contextlib.suppress(Exception):
            box.close()
    qtbot.wait(10)


@pytest.fixture
def view(qtbot):
    from teleport.ui.transfers_view import TransfersView

    v = TransfersView()
    qtbot.addWidget(v)
    v.set_device_names({TRANSFER_PEER_ID: PEER})
    v.resize(420, 560)
    v.show()
    qtbot.waitExposed(v)
    return v


@pytest.fixture
def window(qtbot):
    from teleport.ui.main_window import MainWindow

    w = MainWindow()
    qtbot.addWidget(w)
    return w


@pytest.fixture
def restore_style(qapp):
    palette = qapp.palette()
    sheet = qapp.styleSheet()
    yield
    qapp.setStyleSheet(sheet)
    qapp.setPalette(palette)


# ---- Radar ---------------------------------------------------------------------------------------


def device_id(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:32]


def dview(label: str, *, name: str | None = None, paired=False, online=True, connected=False):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id(label),
        name=label if name is None else name,
        os="windows" if online else None,
        paired=paired,
        online=online,
        connected=connected,
        addresses=("192.0.2.7",) if online else (),
        port=47001 if online else None,
    )


def order(model) -> list[str]:
    return [v.name for v in model.devices()]


def spec_key(v) -> tuple:
    return (not v.connected, not v.paired, unicodedata.normalize("NFC", v.name).casefold(), v.device_id)


# =================================================================================================
# B3.1 — Thẻ mới lên đầu
# =================================================================================================


def three_rows(view):
    keys = [tkey("send", c * 32) for c in "abc"]
    for k in keys:
        view.handle(status_changed(k))
    QApplication.processEvents()
    return [view.row(k) for k in keys]


def test_rows_lists_newest_card_first(view):
    r1, r2, r3 = three_rows(view)
    assert view.rows() == [r3, r2, r1]


def test_newest_card_is_drawn_on_top(view, qtbot):
    r1, r2, r3 = three_rows(view)
    qtbot.waitUntil(lambda: r3.isVisible() and r1.isVisible(), timeout=1000)
    ys = [r.mapTo(view, QPoint(0, 0)).y() for r in (r3, r2, r1)]
    assert ys[0] < ys[1] < ys[2], f"thẻ mới phải nằm trên thẻ cũ: y = {ys}"


def test_card_added_via_progress_or_offer_also_goes_on_top(view):
    """Mọi đường tạo thẻ (tiến độ, offer) đều chèn lên đầu, không chỉ `StatusChanged`."""
    first = tkey("send", "a" * 32)
    view.handle(status_changed(first))
    second = tkey("receive", "b" * 32)
    view.handle(progress(second, 100))
    third = tkey("receive", "c" * 32)
    view.handle(offer_received(third))
    assert view.rows() == [view.row(third), view.row(second), view.row(first)]


def test_tab_walks_cards_in_display_order(view, qtbot):
    from PySide6.QtTest import QTest

    r1, r2, r3 = three_rows(view)
    view.activateWindow()
    qtbot.waitUntil(lambda: r3.cancel_button.isVisible(), timeout=1000)
    cancels = {r.cancel_button: r for r in (r1, r2, r3)}
    r3.cancel_button.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(lambda: QApplication.focusWidget() is r3.cancel_button, timeout=1000)
    seen = [r3]
    for _ in range(10):
        QTest.keyClick(QApplication.focusWidget() or view, Qt.Key.Key_Tab)
        row = cancels.get(QApplication.focusWidget())
        if row is not None and row not in seen:
            seen.append(row)
    assert seen == [r3, r2, r1], "Tab phải đi qua thẻ theo thứ tự hiển thị (trên xuống)"


# =================================================================================================
# B3.2 — Tên lưu khác tên gốc
# =================================================================================================


def run_receive(view, files, saved: list[str], tmp_path: Path):
    """Một lượt nhận: `files` (tên gốc trong `info.names`, cỡ), `saved[i]` là tên file thật trên đĩa
    (`FileDone.name == FileDone.path.name == saved[i]`); kết thúc DONE."""
    k = tkey("receive")
    view.handle(status_changed(k, files=files))
    assert len(files) == len(saved)
    for i, real in enumerate(saved):
        view.handle(received(k, i, tmp_path / "nhan" / real))
    view.handle(finished(k, "done", files=files))
    return view.row(k)


def test_one_renamed_file_shows_saved_name(view, tmp_path):
    row = run_receive(view, (("video.mp4", 100),), ["video (1).mp4"], tmp_path)
    assert row.detail_label.text() == fill(SAVED_AS, name="video (1).mp4")


def test_one_renamed_among_several_shows_that_saved_name(view, tmp_path):
    from teleport.ui import texts

    files = (("a.bin", 10), ("b.bin", 10), ("c.bin", 10))
    row = run_receive(view, files, ["a.bin", "b (2).bin", "c.bin"], tmp_path)
    assert row.detail_label.text() == fill(SAVED_AS, name=texts.file_label("b (2).bin"))


def test_long_saved_name_goes_through_file_label(view, tmp_path):
    from teleport.ui import texts

    long_name = "bao-cao-" + "x" * 120 + ".pdf"
    saved = "bao-cao-" + "x" * 120 + " (1).pdf"
    row = run_receive(view, ((long_name, 10),), [saved], tmp_path)
    assert row.detail_label.text() == fill(SAVED_AS, name=texts.file_label(saved))
    assert texts.file_label(saved) != saved, "tiền đề: tên dài phải bị rút gọn"


@pytest.mark.parametrize("renamed", [2, 3])
def test_several_renamed_files_show_plural_count(view, tmp_path, renamed):
    files = tuple((f"f{i}.bin", 10) for i in range(3))
    saved = [f"f{i} (1).bin" if i < renamed else f"f{i}.bin" for i in range(3)]
    row = run_receive(view, files, saved, tmp_path)
    assert row.detail_label.text() == fill(SAVED_MANY, n=str(renamed))


def test_no_renamed_file_keeps_plain_verified_text(view, tmp_path):
    files = (("a.bin", 10), ("b.bin", 10))
    row = run_receive(view, files, ["a.bin", "b.bin"], tmp_path)
    assert row.detail_label.text() == DONE_DETAIL


def test_sender_done_text_unchanged(view):
    k = tkey("send")
    files = (("a.bin", 10), ("b.bin", 10))
    view.handle(status_changed(k, files=files))
    view.handle(file_done(k, 0, "a.bin", None))
    view.handle(file_done(k, 1, "b.bin", None))
    view.handle(finished(k, "done", files=files))
    assert view.row(k).detail_label.text() == DONE_DETAIL


def test_renamed_sentence_only_when_done(view, tmp_path):
    """Lượt nhận đổi tên một file rồi bị hủy: không có câu “Đã lưu thành …”."""
    k = tkey("receive")
    files = (("a.bin", 10), ("b.bin", 10))
    view.handle(status_changed(k, files=files))
    view.handle(received(k, 0, tmp_path / "a (1).bin"))
    view.handle(finished(k, "cancelled_user", files=files, by_peer=True))
    assert "Đã lưu thành" not in view.row(k).detail_label.text()


# =================================================================================================
# B3.3 — Thanh tiến độ khi kết thúc
# =================================================================================================


def _results_not_done():
    from teleport.app.transfers import TransferResult

    return [r.value for r in TransferResult if r not in (TransferResult.DONE, TransferResult.SUSPENDED)]


@pytest.mark.parametrize("direction", ["send", "receive"])
@pytest.mark.parametrize("result", _results_not_done())
def test_progress_bar_hidden_when_finished_not_done(view, direction, result):
    k = tkey(direction)
    view.handle(progress(k, 4 * 2**20))
    row = view.row(k)
    assert row.progress_bar.isVisibleTo(row), "tiền đề: lượt đang chạy có thanh"
    view.handle(finished(k, result))
    assert not row.progress_bar.isVisibleTo(row), f"{result}: thanh tiến độ phải ẩn"


@pytest.mark.parametrize("direction", ["send", "receive"])
def test_progress_bar_full_and_visible_when_done(view, direction):
    k = tkey(direction)
    view.handle(progress(k, 2**20))
    view.handle(finished(k, "done"))
    bar = view.row(k).progress_bar
    assert bar.isVisibleTo(view.row(k))
    assert bar.value() == bar.maximum()


def test_suspended_keeps_progress_bar(view):
    k = tkey("receive")
    view.handle(progress(k, 4 * 2**20))
    row = view.row(k)
    before = row.progress_bar.value()
    assert before > 0
    view.handle(finished(k, "suspended"))
    assert row.progress_bar.isVisibleTo(row)
    assert row.progress_bar.value() == before


# =================================================================================================
# B3.4, B3.5 — Tỉ lệ cột, câu trống căn giữa
# =================================================================================================


# B3.4 (radar : lượt = 1:1) đã bỏ: spec B1 (mục 8a) đổi sang cột trái cố định 288/248 px; bề ngang
# mới kiểm ở `test_home_layout.py` (L2).


def test_transfers_empty_label_centered_both_ways(view, qtbot):
    from PySide6.QtWidgets import QScrollArea

    label = view.empty_label
    assert label.isVisibleTo(view)
    assert label.alignment() & Qt.AlignmentFlag.AlignHCenter
    [scroll] = view.findChildren(QScrollArea)
    viewport = scroll.viewport()
    qtbot.waitUntil(lambda: viewport.height() > 200, timeout=1000)
    QApplication.processEvents()
    center = label.mapTo(viewport, label.rect().center())
    assert abs(center.y() - viewport.height() / 2) < viewport.height() * 0.1, (
        f"câu trống phải căn giữa dọc: tâm y={center.y()}, vùng cuộn cao {viewport.height()}"
    )
    assert abs(center.x() - viewport.width() / 2) < viewport.width() * 0.1


# =================================================================================================
# B4 — Ghép đôi xong → dải thông báo
# =================================================================================================


@pytest.mark.parametrize("role", ["initiator", "responder"])
def test_paired_emits_notice_and_opens_no_box(ctl, role):
    ns = ctl()
    before = len(visible_boxes())
    result = make_finished("paired", role=role, name="May-B")
    ns.host.pairing_finished.emit(result)
    QApplication.processEvents()
    text = fill(OUT_PAIRED, x=peer_label("May-B"))
    assert ns.notices == [text]
    assert ns.messages == [text], "`message` vẫn phát như cũ"
    assert ns.c.result_box is None
    assert len(visible_boxes()) == before, "ghép đôi xong không được mở QMessageBox"


def test_paired_after_own_pairing_job_closes_progress_and_uses_notice(ctl):
    ns = ctl()
    assert ns.c.pair_address("192.168.1.5", 47474)
    assert ns.c.progress_box is not None
    ns.host.pairing_finished.emit(make_finished("paired", name="May-B"))
    QApplication.processEvents()
    assert ns.c.progress_box is None
    assert ns.notices == [fill(OUT_PAIRED, x=peer_label("May-B"))]
    assert ns.c.result_box is None
    assert not visible_boxes()


def test_unpaired_emits_notice_and_opens_no_box(ctl):
    ns = ctl()
    ns.c.request_unpair(PEER_ID, "May-B")
    confirm = ns.c.unpair_box
    assert confirm is not None
    [yes] = [b for b in confirm.buttons() if b.text().replace("&", "") == "Hủy ghép đôi"]
    yes.click()
    QApplication.processEvents()
    assert ns.host.submits, "đồng ý thì phải gửi việc hủy ghép đôi"
    ns.host.run_last_submit()
    QApplication.processEvents()
    text = fill(UNPAIRED, name=peer_label("May-B"))
    assert ns.notices == [text]
    assert ns.messages == [text]
    assert ns.c.result_box is None
    assert not visible_boxes()


def test_unpair_of_already_unpaired_device_emits_notice(ctl):
    """Nhánh KeyError (đã bị hủy trước đó) cũng là thành công: dải thông báo, không hộp."""
    ns = ctl()
    ns.node.unpair_error = KeyError(PEER_ID)
    ns.c.unpair(PEER_ID)
    ns.host.run_last_submit()
    QApplication.processEvents()
    assert len(ns.notices) == 1 and ns.notices[0].startswith("Đã hủy ghép đôi với")
    assert ns.c.result_box is None
    assert not visible_boxes()


def test_unpair_failure_still_uses_result_box(ctl):
    ns = ctl()
    ns.node.unpair_error = OSError("hỏng đĩa")
    ns.c.unpair(PEER_ID)
    ns.host.run_last_submit()
    QApplication.processEvents()
    assert ns.notices == []
    assert ns.c.result_box is not None and ns.c.result_box.isVisible()


@pytest.mark.parametrize("outcome", ["peer_busy", "unreachable", "identity_mismatch", "user_rejected"])
def test_pairing_errors_still_use_result_box(ctl, outcome):
    ns = ctl()
    ns.host.pairing_finished.emit(make_finished(outcome))
    QApplication.processEvents()
    assert ns.notices == []
    box = ns.c.result_box
    assert box is not None and box.isVisible()
    assert ns.messages and box.text() == ns.messages[-1]


def test_success_closes_open_error_box_then_emits_notice(ctl, qtbot):
    ns = ctl()
    ns.host.pairing_finished.emit(make_finished("unreachable", sid=1))
    QApplication.processEvents()
    box = ns.c.result_box
    assert box is not None and box.isVisible()
    ns.host.pairing_finished.emit(make_finished("paired", sid=2, name="May-B"))
    qtbot.waitUntil(lambda: closed(box), timeout=1000)
    assert ns.c.result_box is None
    assert ns.notices == [fill(OUT_PAIRED, x=peer_label("May-B"))]
    assert not visible_boxes()


def _unpair_ok(ns) -> str:
    ns.c.unpair(PEER_ID)
    ns.host.run_last_submit()
    QApplication.processEvents()
    return fill(UNPAIRED, name=peer_label(None, PEER_ID))


def _paired_ok(ns) -> str:
    ns.host.pairing_finished.emit(make_finished("paired", role="responder", name="May-B"))
    QApplication.processEvents()
    return fill(OUT_PAIRED, x=peer_label("May-B"))


@pytest.mark.parametrize("success", [_paired_ok, _unpair_ok], ids=["paired", "unpaired"])
def test_success_while_window_hidden_notifies_tray_once(ctl, success):
    """Cửa sổ ẩn (khay): kết quả thành công vẫn tới người dùng qua bóng thoại, đúng một lần, không hộp."""
    ns = ctl(visible=False)
    text = success(ns)
    assert len(ns.notes) == 1, f"notify phải được gọi đúng một lần: {ns.notes}"
    assert ns.notes[0][1] == text
    assert ns.notices == [text]
    assert ns.c.result_box is None
    assert not visible_boxes()


@pytest.mark.parametrize("success", [_paired_ok, _unpair_ok], ids=["paired", "unpaired"])
def test_success_while_window_visible_does_not_notify(ctl, success):
    ns = ctl(visible=True)
    text = success(ns)
    assert ns.notes == []
    assert ns.notices == [text]


def test_gui_app_routes_pairing_notice_to_transfers_view(qapp, tmp_path):
    from teleport.ui import app as ui_app

    gui = ui_app.GuiApp(qapp, tmp_path)
    try:
        gui.pairing.notice.emit("Đã ghép đôi với “May-B”.")
        assert gui.transfers.view.notice_text() == "Đã ghép đôi với “May-B”."
    finally:
        gui.tray.dispose()
        gui.window.deleteLater()


# =================================================================================================
# B6 — Câu chữ
# =================================================================================================


def test_no_devices_text_mentions_firewall():
    from teleport.ui.radar import no_devices_text

    assert no_devices_text() == NO_DEVICES
    assert "tường lửa" in no_devices_text()


def test_unreachable_text_keeps_old_sentence_and_adds_hotspot_hint():
    from teleport.core.links import ConnectResult
    from teleport.ui.radar import connect_failed_text

    v = dview("May-X", paired=True)
    text = connect_failed_text(v, ConnectResult.UNREACHABLE)
    assert fill(UNREACHABLE_OLD, name=peer_label("May-X", v.device_id)) in text
    assert "điểm phát Wi-Fi" in text
    assert text.rstrip().endswith(HOTSPOT_HINT)


@pytest.mark.parametrize("outcome", ["peer_busy", "rejected"])
@pytest.mark.parametrize(("name", "port"), [("MacBook", 47474), (None, 47474), (None, None)])
def test_busy_or_rejected_text_promises_no_seconds(outcome, name, port):
    from teleport.ui.pairing import outcome_text

    result = make_finished(outcome, name=name, port=port, code_shown=False)
    text = outcome_text(result)
    assert text is not None
    if name is not None:
        shown = f"“{peer_label(name)}”"
        assert text == fill(NOT_READY, name=shown)
    rest = text
    for token in (f"“{peer_label(name)}”" if name else "", "192.168.1.5:47474", "192.168.1.5"):
        if token:
            rest = rest.replace(token, "")
    for banned in ("ẩn", "chế độ chờ", "giây"):
        assert banned not in rest, f"câu không được chứa “{banned}”: {text!r}"
    assert not re.search(r"\d", rest), f"câu không được hứa con số: {text!r}"
    assert rest.endswith("Hãy chờ một lát rồi thử lại.")


# =================================================================================================
# B7.1 — Cỡ cửa sổ
# =================================================================================================


@pytest.mark.parametrize(
    ("available", "expected"),
    [
        ((1920, 1080), (980, 660)),
        ((980, 660), (980, 660)),
        ((1366, 600), (980, 600)),
        ((900, 2000), (900, 660)),
        ((800, 600), (800, 600)),
        ((640, 400), (640, 400)),
    ],
)
def test_default_size_clamps_to_available(available, expected):
    from teleport.ui.main_window import default_size

    assert default_size(QSize(*available)) == QSize(*expected)


def test_default_size_constant():
    from teleport.ui.main_window import DEFAULT_SIZE

    assert QSize(980, 660) == DEFAULT_SIZE


def test_main_window_starts_at_default_size_of_primary_screen(window):
    from PySide6.QtGui import QGuiApplication

    from teleport.ui.main_window import default_size

    assert window.minimumSize() == QSize(720, 460)
    screen = QGuiApplication.primaryScreen()
    assert screen is not None
    expected = default_size(screen.availableGeometry().size()).expandedTo(QSize(720, 460))
    assert window.size() == expected


def test_main_window_without_screen_uses_default_size(qtbot, monkeypatch):
    from PySide6.QtGui import QGuiApplication

    from teleport.ui.main_window import DEFAULT_SIZE, MainWindow

    monkeypatch.setattr(QGuiApplication, "primaryScreen", staticmethod(lambda: None))
    w = MainWindow()
    qtbot.addWidget(w)
    assert w.size() == DEFAULT_SIZE


# =================================================================================================
# B7.2 — Tiêu đề lớn ẩn khi RUNNING
# =================================================================================================


def test_title_hidden_only_when_running(window, tmp_path):
    window.show_starting()
    assert window.title_label.isVisibleTo(window)
    window.show_welcome("May-A")
    assert window.title_label.isVisibleTo(window)
    window.show_running("May-A", tmp_path)
    assert not window.title_label.isVisibleTo(window)
    window.show_error("other", "")
    assert window.title_label.isVisibleTo(window)
    window.show_running("May-A", tmp_path)
    assert not window.title_label.isVisibleTo(window)
    window.show_starting()
    assert window.title_label.isVisibleTo(window)


# =================================================================================================
# B7.3 — Không con trỏ chữ trong nhãn; tiêu điểm khi RUNNING
# =================================================================================================


def keyboard_selectable(root) -> list[str]:
    bad = []
    for lb in root.findChildren(QLabel):
        if lb.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByKeyboard:
            bad.append(f"{lb.objectName() or type(lb.parent()).__name__}: {lb.text()[:40]!r}")
    return bad


def full_window(window, tmp_path):
    """Cửa sổ RUNNING có nhiều máy ở cột trái, danh sách lượt có thẻ và thông báo (spec B2 12.26: không còn
    khung nhập IP trong cột trái)."""
    from teleport.ui.transfers_view import TransfersView

    tv = TransfersView(window.transfers_area)
    window.transfers_area.layout().addWidget(tv)
    tv.set_device_names({TRANSFER_PEER_ID: PEER})
    tv.handle(status_changed(tkey("send", "a" * 32)))
    k = tkey("receive", "b" * 32)
    tv.handle(status_changed(k))
    tv.handle(received(k, 0, tmp_path / "video (1).mp4"))
    tv.handle(finished(k, "done"))
    tv.handle(finished(tkey("send", "a" * 32), "rejected_user", by_peer=True))
    tv.show_notice("Một thông báo.", "Làm gì đó", lambda: None)
    window.devices.reset([dview("May-X", paired=True, connected=True), dview("May-Y")])
    window.show_running("May-A", tmp_path)
    return tv


@pytest.mark.parametrize("state", ["welcome", "starting", "running", "error"])
def test_no_label_in_main_window_is_keyboard_selectable(window, tmp_path, state):
    full_window(window, tmp_path)
    {
        "welcome": lambda: window.show_welcome("May-A"),
        "starting": window.show_starting,
        "running": lambda: window.show_running("May-A", tmp_path),
        "error": lambda: window.show_error("other", "chi tiết"),
    }[state]()
    assert keyboard_selectable(window) == []


def test_offer_box_and_pairing_dialogs_have_no_keyboard_selectable_label(qtbot):
    from teleport.ui.offer import OfferPrompts
    from teleport.ui.pairing import PairingDialog, PairingProgressDialog

    prompts = OfferPrompts(Path("nhan"))
    k = tkey("receive")
    prompts.handle(offer_received(k))
    box = prompts.box(k)
    assert box is not None
    dlg = PairingDialog(make_prompt())
    progress_dlg = PairingProgressDialog("May-B")
    try:
        for root in (box, dlg, progress_dlg):
            assert keyboard_selectable(root) == [], type(root).__name__
    finally:
        prompts.close_all()
        dlg.dismiss()
        progress_dlg.close()


def test_text_labels_stay_mouse_selectable_and_tabbable(window, tmp_path):
    tv = full_window(window, tmp_path)
    labels = [window.status_label, window.detail_label, window.hint_label]
    labels.append(tv.notice_label)
    for lb in labels:
        flags = lb.textInteractionFlags()
        assert flags & Qt.TextInteractionFlag.TextSelectableByMouse, lb.objectName()
        assert lb.focusPolicy() == Qt.FocusPolicy.StrongFocus, lb.objectName()
        assert lb.accessibleName(), lb.objectName()


def show_and_run(window, qtbot, tmp_path):
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    window.show_running("May-A", tmp_path)
    QApplication.processEvents()


def test_running_focus_goes_to_device_list_when_devices(window, qtbot, tmp_path):
    window.devices.reset([dview("May-X", paired=True)])
    show_and_run(window, qtbot, tmp_path)
    qtbot.waitUntil(lambda: window.focusWidget() is window.conversations.list_view, timeout=1000)
    assert window.focusWidget() is not window.status_label


def test_running_focus_goes_to_add_device_button_when_no_paired_device(window, qtbot, tmp_path):
    """Spec B2 2.8: không có máy đã ghép đôi → “＋ Ghép đôi máy mới” (thay nút “Nhập IP thủ công”)."""
    show_and_run(window, qtbot, tmp_path)
    add = window.conversations.add_button
    qtbot.waitUntil(lambda: window.focusWidget() is add, timeout=1000)


# =================================================================================================
# B7.4 — Sắp xếp radar
# =================================================================================================


def test_radar_orders_connected_then_paired_then_name(qtbot):
    from teleport.ui.radar import DeviceListModel

    m = DeviceListModel()
    m.reset(
        [
            dview("alpha"),
            dview("Zulu", paired=True, connected=True),
            dview("bravo", paired=True),
            dview("Alpha2", paired=True, connected=True),
            dview("Charlie"),
        ]
    )
    assert order(m) == ["Alpha2", "Zulu", "bravo", "alpha", "Charlie"]


def test_radar_order_follows_connect_and_disconnect(qtbot):
    from teleport.app.node import DeviceConnected, DeviceDisconnected
    from teleport.ui.radar import DeviceListModel

    m = DeviceListModel()
    m.reset([dview("Alpha", paired=True), dview("Bravo", paired=True, connected=True), dview("Zulu")])
    assert order(m) == ["Bravo", "Alpha", "Zulu"]
    m.apply(DeviceDisconnected(dview("Bravo", paired=True)))
    assert order(m) == ["Alpha", "Bravo", "Zulu"]
    m.apply(DeviceConnected(dview("Bravo", paired=True, connected=True), outgoing=True))
    assert order(m) == ["Bravo", "Alpha", "Zulu"]
    m.apply(DeviceConnected(dview("Alpha", paired=True, connected=True), outgoing=False))
    assert order(m) == ["Alpha", "Bravo", "Zulu"]


def test_radar_order_on_row_insert_and_pairing_change(qtbot):
    from teleport.app.node import DeviceConnected, DeviceOnline
    from teleport.ui.radar import DeviceListModel

    m = DeviceListModel()
    m.reset([dview("Bravo", paired=True, connected=True), dview("Alpha", paired=True), dview("Zulu")])
    m.apply(DeviceOnline(dview("Aaron")))  # chèn từng hàng: chưa ghép đôi → sau máy đã ghép
    assert order(m) == ["Bravo", "Alpha", "Aaron", "Zulu"]
    m.apply(DeviceConnected(dview("Yankee", paired=True, connected=True), outgoing=True))
    assert order(m) == ["Bravo", "Yankee", "Alpha", "Aaron", "Zulu"]
    m.apply(DeviceOnline(dview("Zulu", paired=True)))  # vừa ghép đôi
    assert order(m) == ["Bravo", "Yankee", "Alpha", "Zulu", "Aaron"]


_NAMES = ["alpha", "Alpha", "bravo", "Ä", "zulu"]
_EVENT = st.tuples(
    st.sampled_from(["online", "offline", "connected", "disconnected", "failed"]),
    st.integers(0, 3),  # thiết bị
    st.sampled_from(_NAMES),
    st.booleans(),  # paired
)


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(_EVENT, max_size=25), st.lists(st.tuples(st.integers(0, 3), st.booleans()), max_size=4))
def test_radar_model_stays_sorted_under_any_event_sequence(qapp, events, snapshot):
    """Property: sau mọi chuỗi `NodeEvent` (thêm, bỏ, đổi tên, nối, ngắt, ghép), hàng luôn theo
    (chưa nối, chưa ghép, tên casefold, device_id), mỗi device_id một hàng."""
    from teleport.app.devices import DeviceView
    from teleport.app.node import (
        ConnectFailed,
        DeviceConnected,
        DeviceDisconnected,
        DeviceOffline,
        DeviceOnline,
    )
    from teleport.core.links import ConnectResult
    from teleport.ui.radar import DeviceListModel

    def make(i: int, name: str, paired: bool, online: bool, connected: bool) -> DeviceView:
        return DeviceView(
            device_id=f"{i:02d}" * 16,
            name=name,
            os=None,
            paired=paired,
            online=online,
            connected=connected and paired,
            addresses=("192.0.2.1",) if online else (),
            port=47001 if online else None,
        )

    m = DeviceListModel()
    m.reset([make(i, _NAMES[i], paired, True, paired) for i, paired in snapshot])
    for kind, i, name, paired in events:
        if kind == "online":
            ev = DeviceOnline(make(i, name, paired, True, False))
        elif kind == "offline":
            ev = DeviceOffline(make(i, name, paired, False, False))
        elif kind == "connected":
            ev = DeviceConnected(make(i, name, True, True, True), outgoing=True)
        elif kind == "disconnected":
            ev = DeviceDisconnected(make(i, name, True, True, False))
        else:
            ev = ConnectFailed(make(i, name, True, True, False), ConnectResult.UNREACHABLE)
        m.apply(ev)
        rows = list(m.devices())
        assert rows == sorted(rows, key=spec_key), [spec_key(v) for v in rows]
        assert len({v.device_id for v in rows}) == len(rows) == m.rowCount()


# =================================================================================================
# B7.5 — Icon và chữ đậm của hàng
# =================================================================================================


def test_every_row_has_device_icon(qtbot):
    from PySide6.QtGui import QIcon

    from teleport.ui.radar import DeviceListModel

    m = DeviceListModel()
    m.reset([dview("A"), dview("B", paired=True), dview("C", paired=True, connected=True)])
    for r in range(m.rowCount()):
        icon = m.data(m.index(r, 0), Qt.ItemDataRole.DecorationRole)
        assert isinstance(icon, QIcon) and not icon.isNull(), f"hàng {r} thiếu icon máy"


def test_paired_rows_have_bold_font(qtbot):
    from PySide6.QtGui import QFont

    from teleport.ui.radar import DeviceListModel

    m = DeviceListModel()
    m.reset(
        [
            dview("B", paired=True),
            dview("C", paired=True, connected=True),
            dview("D", paired=True, online=False),
        ]
    )
    for r in range(m.rowCount()):
        font = m.data(m.index(r, 0), Qt.ItemDataRole.FontRole)
        assert isinstance(font, QFont) and font.bold(), f"máy đã ghép đôi (hàng {r}) phải có tên đậm"


def test_rows_keep_accessible_text_and_tooltip(qtbot):
    from teleport.ui.radar import DeviceListModel

    m = DeviceListModel()
    m.reset([dview("May-X", paired=True)])
    idx = m.index(0, 0)
    assert "May-X" in str(m.data(idx, Qt.ItemDataRole.AccessibleTextRole))
    assert "May-X" in str(m.data(idx, Qt.ItemDataRole.ToolTipRole))


# =================================================================================================
# B7.7 — Nút chính, Enter an toàn, tương phản
# =================================================================================================


def is_primary(button) -> bool:
    from teleport.ui import theme

    return button.property(theme.PRIMARY_PROPERTY) is True


def test_primary_property_name():
    from teleport.ui import theme

    assert theme.PRIMARY_PROPERTY == "primary"


def test_primary_buttons_carry_property(window, qtbot):
    from teleport.ui.offer import OfferPrompts
    from teleport.ui.pairing import PairingDialog

    assert is_primary(window.start_button)
    assert is_primary(window.discover.address_submit)  # spec B2: ô nhập IP chỉ còn ở “Tìm máy mới”

    prompts = OfferPrompts(Path("nhan"))
    k = tkey("receive")
    prompts.handle(offer_received(k))
    dlg = PairingDialog(make_prompt())
    try:
        accept, reject = offer_buttons(prompts.box(k))
        assert is_primary(accept), "“Nhận” là nút chính"
        assert not is_primary(reject), "“Từ chối” không tô màu nhấn"
        assert is_primary(dlg.accept_button), "“Trùng khớp” là nút chính"
        assert not is_primary(dlg.reject_button)
    finally:
        prompts.close_all()
        dlg.dismiss()


def offer_buttons(box):
    from PySide6.QtWidgets import QPushButton

    by_text = {b.text().replace("&", ""): b for b in box.findChildren(QPushButton)}
    return by_text["Nhận"], by_text["Từ chối"]


def test_enter_stays_safe_in_offer_box_and_code_dialog(qtbot, qapp, restore_style):
    from teleport.ui import theme
    from teleport.ui.offer import OfferPrompts
    from teleport.ui.pairing import PairingDialog

    theme.apply(qapp)
    prompts = OfferPrompts(Path("nhan"))
    k = tkey("receive")
    prompts.handle(offer_received(k))
    dlg = PairingDialog(make_prompt())
    try:
        accept, reject = offer_buttons(prompts.box(k))
        assert reject.isDefault()
        assert not accept.isDefault() and not accept.autoDefault()
        assert dlg.reject_button.isDefault()
        assert not dlg.accept_button.isDefault()
    finally:
        prompts.close_all()
        dlg.dismiss()


def _css(qapp) -> str:
    return re.sub(r"\s+", " ", qapp.styleSheet())


_PRIMARY_SEL = r'QPushButton\[primary=(?:"true"|true|\'true\')\]'


def test_theme_stylesheet_paints_primary_buttons(qapp, restore_style):
    from teleport.ui import theme

    theme.apply(qapp)
    css = _css(qapp)
    assert re.search(_PRIMARY_SEL, css), 'thiếu luật QPushButton[primary="true"]'
    text, background = theme.primary_colors(theme.is_dark(qapp.styleHints().colorScheme()))["normal"]
    assert f"background-color: {background.name()}" in css, "nền nút chính là token accent"
    assert f"color: {text.name()}" in css, "chữ nút chính là token accent_text"
    assert re.search(_PRIMARY_SEL + r":disabled", css), "thiếu trạng thái :disabled"
    assert re.search(_PRIMARY_SEL + r":(?:hover|pressed)", css), "thiếu :hover / :pressed"
    assert re.search(_PRIMARY_SEL + r":focus", css), "thiếu :focus thấy rõ"


@pytest.mark.parametrize("dark", [False, True])
def test_highlight_pair_meets_min_contrast(dark):
    from PySide6.QtGui import QPalette

    from teleport.ui import theme

    pal = theme.palette(dark)
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        fg = pal.color(group, QPalette.ColorRole.HighlightedText)
        bg = pal.color(group, QPalette.ColorRole.Highlight)
        assert theme.contrast_ratio(fg, bg) >= theme.MIN_CONTRAST


def _dominant(image) -> tuple[int, int, int]:
    from collections import Counter

    counts: Counter = Counter()
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            c = image.pixelColor(x, y)
            counts[(c.red(), c.green(), c.blue())] += 1
    return counts.most_common(1)[0][0]


def _distance(a, b) -> int:
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def test_primary_button_drawn_with_highlight_and_others_not(window, qapp, qtbot, restore_style):
    from teleport.ui import theme

    theme.apply(qapp)
    window.show_welcome("May-A")
    window.show()
    qtbot.waitExposed(window)
    QApplication.processEvents()
    hl = theme.primary_colors(theme.is_dark(qapp.styleHints().colorScheme()))["normal"][1]
    highlight = (hl.red(), hl.green(), hl.blue())
    start = _dominant(window.start_button.grab().toImage())
    assert _distance(start, highlight) <= 24, f"nút chính phải tô màu nhấn {highlight}, thấy {start}"
    window.show_running("May-A", None)
    QApplication.processEvents()
    from teleport.ui.main_window import Page

    # Spec B2 12.26: nút thường thay `radar.address_button` là `discover.view_button` (trang “Tìm máy mới”).
    assert window.open_page(Page.DISCOVER)
    QApplication.processEvents()
    ordinary = window.discover.view_button
    assert ordinary.isVisible() and not ordinary.size().isEmpty()
    assert not is_primary(ordinary)
    drawn = _dominant(ordinary.grab().toImage())
    assert _distance(drawn, highlight) > 24, "nút thường không được tô màu nhấn"


def test_stylesheet_does_not_resize_ordinary_buttons(qapp, qtbot, restore_style):
    from PySide6.QtWidgets import QPushButton

    from teleport.ui import theme

    theme.apply(qapp)
    qapp.setStyleSheet("")
    plain = QPushButton("Hủy")
    qtbot.addWidget(plain)
    before = plain.sizeHint()
    theme.apply(qapp)
    after_btn = QPushButton("Hủy")
    qtbot.addWidget(after_btn)
    after = after_btn.sizeHint()
    assert abs(after.height() - before.height()) <= 4
    assert abs(after.width() - before.width()) <= 10


# =================================================================================================
# B7.8 — Hộp mã rộng, nút căn phải
# =================================================================================================


def test_code_and_progress_dialogs_share_offer_min_width(qtbot):
    from teleport.ui.offer import OfferPrompts
    from teleport.ui.pairing import PairingDialog, PairingProgressDialog

    prompts = OfferPrompts(Path("nhan"))
    k = tkey("receive")
    prompts.handle(offer_received(k))
    dlg = PairingDialog(make_prompt())
    prog = PairingProgressDialog("May-B")
    try:
        widths = {
            "offer": prompts.box(k).minimumWidth(),
            "code": dlg.minimumWidth(),
            "progress": prog.minimumWidth(),
        }
        assert all(w >= 420 for w in widths.values()), widths
        assert len(set(widths.values())) == 1, f"ba hộp phải dùng chung một bề rộng tối thiểu: {widths}"
    finally:
        prompts.close_all()
        dlg.dismiss()
        prog.close()


def test_code_dialog_buttons_aligned_right_reject_then_accept(qtbot):
    from teleport.ui.pairing import PairingDialog

    dlg = PairingDialog(make_prompt())
    try:
        dlg.show()
        qtbot.waitExposed(dlg)
        dlg.resize(700, dlg.height())  # rộng hơn cỡ tự nhiên: khoảng giãn phải nằm bên trái hai nút
        QApplication.processEvents()
        rej, acc = dlg.reject_button.geometry(), dlg.accept_button.geometry()
        assert rej.right() < acc.left(), "“Không trùng” đứng trước “Trùng khớp”"
        assert acc.left() - rej.right() < 24, "hai nút đứng sát nhau (khoảng giãn ở bên trái)"
        assert dlg.width() - acc.right() < 32, "nút căn phải"
    finally:
        dlg.dismiss()


# =================================================================================================
# Nút chính cao bằng nút thường cạnh nó (soat-code vòng 1)
# =================================================================================================


def test_primary_and_neighbour_buttons_have_same_height(qapp, qtbot, restore_style):
    from teleport.ui import theme
    from teleport.ui.offer import OfferPrompts
    from teleport.ui.pairing import PairingDialog

    theme.apply(qapp)
    prompts = OfferPrompts(Path("nhan"))
    k = tkey("receive")
    prompts.handle(offer_received(k))
    dlg = PairingDialog(make_prompt())
    try:
        accept, reject = offer_buttons(prompts.box(k))
        pairs = {
            "hộp nhận file": (accept, reject),
            "hộp mã": (dlg.accept_button, dlg.reject_button),
        }
        for where, (primary, plain) in pairs.items():
            assert is_primary(primary) and not is_primary(plain)
            hp, hn = primary.sizeHint().height(), plain.sizeHint().height()
            assert abs(hp - hn) <= 1, f"{where}: nút chính cao {hp}, nút thường {hn}"
    finally:
        prompts.close_all()
        dlg.dismiss()


# =================================================================================================
# Ghép với app thật: nhận file trùng tên (soat-code vòng 1)
# =================================================================================================


@pytest.mark.integration
def test_real_receive_of_duplicate_name_shows_saved_as(
    gui, gui_wait, config_dir, trust_each_other, peer_node, tmp_path, ui_helpers
):
    """A (GUI thật) đã có `a.txt` trong thư mục nhận; B gửi `a.txt` → file lưu thành `a (1).txt` và
    thẻ bên nhận ghi “Đã lưu thành a (1).txt · Đã kiểm tra toàn vẹn”."""
    import os

    from PySide6.QtWidgets import QDialog

    from teleport.trust.identity import load_or_create_identity
    from teleport.ui.transfers_view import TransfersView

    a, b = config_dir("A"), config_dir("B")
    trust_each_other(a, b)
    a_id = load_or_create_identity(a).device_id
    receive_dir = tmp_path / "nhan-gui"  # thư mục nhận mà fixture `gui` ép cho node A
    receive_dir.mkdir(parents=True, exist_ok=True)
    (receive_dir / "a.txt").write_bytes(b"canh gac")
    src = tmp_path / "nguon"
    src.mkdir()
    f = src / "a.txt"
    f.write_bytes(os.urandom(50_000))
    peer = peer_node(b)
    want = fill(SAVED_AS, name="a (1).txt")
    seen: dict = {}

    def offer_box():
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible() and ui_helpers.buttons_with_text(w, "Nhận"):
                return w
        return None

    def card_details(run) -> list[str]:
        out = []
        for tv in run.window.findChildren(TransfersView):
            out += [r.detail_label.text() for r in tv.rows()]
        return out

    def script(run):
        yield gui_wait(lambda: run.hosts and run.host.running, 15, "node A chạy")
        got: list = []
        run.host.call(lambda node: node.port, got.append)
        yield gui_wait(lambda: got, 5, "cổng của A")
        peer.soon(peer.node.connect_hint, a_id, ["127.0.0.1"], got[0])
        yield gui_wait(lambda: peer.connected_to(a_id), 10, "B nối tới A")
        fut = peer.submit(peer.node.transfers.send(a_id, [f]))
        yield gui_wait(offer_box, 10, "hộp hỏi mở")
        ui_helpers.buttons_with_text(offer_box(), "Nhận")[0].click()
        yield gui_wait(fut.done, 10, "send() trả khóa")
        key = fut.result()
        yield gui_wait(lambda: peer.finished(key.transfer_id), 30, "B thấy lượt kết thúc")
        yield gui_wait(lambda: want in card_details(run), 10, "thẻ bên nhận có câu “Đã lưu thành …”")
        seen["details"] = card_details(run)
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], script)
    assert (receive_dir / "a.txt").read_bytes() == b"canh gac"
    assert (receive_dir / "a (1).txt").read_bytes() == f.read_bytes()
    assert want in seen["details"]
