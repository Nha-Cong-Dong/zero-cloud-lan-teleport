"""Ghép đôi trên giao diện (PR2 `feat(ui)`, P4-5h): `teleport.ui.pairing`.

Viết từ đặc tả, không từ code: spec-hackathon mục 2 PR2 “Xong khi” (1)–(6), mục 9.1–9.6, mục 10;
hợp đồng API spec-pr2-api.md; mvp mục 7.2 (bố cục hộp mã) và mục 8 (câu kết quả).

Ba nhóm:

(a) hàm chữ và `PairingDialog`: tên độc (`PC-A:   382 104`, BiDi, zalgo, HTML, xuống dòng, ký tự trống)
    không bao giờ đứng trước hay cùng nhãn với mã; mọi nhãn PlainText; tooltip không chứa tên máy kia.
(b) `PairingController` với host giả (QObject có `pairing_prompt`, `pairing_finished`, ghi lại
    `call`/`submit`; `fn` chạy trên một Node giả khi test muốn).
(c) integration: NodeHost GUI thật + Node B thật trên 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtWidgets import QAbstractButton, QApplication, QDialog, QLabel, QMessageBox, QWidget

integration = pytest.mark.integration

LOCAL = "127.0.0.1"
PEER_ID = "3f2a9c1e" + "ab" * 12
OTHER_ID = "77aa55cc" + "cd" * 12
NOW = 1000.0

TITLE = "Ghép đôi"
ACCEPT = "Trùng khớp"
REJECT = "Không trùng"
CANCEL = "Hủy"
UNPAIR_TITLE = "Hủy ghép đôi"
UNPAIR_YES = "Hủy ghép đôi"
UNPAIR_NO = "Không"

HEADER_RESPONDER = "Có yêu cầu ghép đôi. Hãy so mã dưới đây với mã trên màn hình máy kia."
HEADER_INITIATOR = "Hãy so mã dưới đây với mã trên màn hình máy kia."
WARNING = (
    "Cảnh báo: máy này đã hiện {n} mã ghép đôi trong 1 phút qua. Nếu bạn không tự ghép đôi nhiều lần "
    "như vậy, có thể có người đang thử giả mạo: hãy bấm “Không trùng”."
)
LINE_PEER = "Máy kia tự xưng là “{name}” (mã máy {id8}, {address})."
LINE_KNOWN = "Máy này đã ghép đôi với mã máy {id8} trước đây, tên đã lưu: “{known}”."
LINE_RENAME = "Ghép đôi xong, tên sẽ đổi thành “{name}”."
PROGRESS = "Đang ghép đôi với “{name}”…"
GONE = "Không thấy “{name}” trong mạng nữa. Hãy thử lại."
UNPAIR_CONFIRM = (
    "Hủy ghép đôi với “{name}”? Hai máy sẽ không gửi file cho nhau được nữa cho tới khi ghép đôi lại."
)
UNPAIRED = "Đã hủy ghép đôi với “{name}”."
UNPAIR_FAILED = "Không hủy ghép đôi được với “{name}”. Hãy thử lại."
NOTIFICATION = ("ZCL Teleport", "Có yêu cầu ghép đôi")

# Câu kết quả (hợp đồng mục 3, mvp mục 8). {x}: tên máy kia; {k}: tên đã lưu; {t}: ip:port.
OUT_PAIRED = "Đã ghép đôi với “{x}”."
OUT_NOT_DONE = "Ghép đôi với “{x}” chưa xong. Nếu mã trên hai máy khác nhau, có thể có người đang giả mạo."
OUT_EARLY = "Ghép đôi với “{x}” chưa xong vì phiên dừng trước khi hiện mã. Hãy thử lại."
OUT_CONFLICT_R = (
    "Một máy chưa xác minh tự xưng là “{k}” nhưng dùng danh tính khác với lúc ghép đôi. Có thể có người "
    "đang giả mạo máy đó. Yêu cầu ghép đôi đã bị chặn."
)
OUT_CONFLICT_I = (
    "“{k}” đang dùng một danh tính khác với lúc ghép đôi. Có thể có người đang giả mạo máy này. "
    "Kết nối đã bị chặn."
)
OUT_CONFLICT_HINT = (
    "Muốn ghép đôi lại thì hãy hủy ghép đôi mục cũ trước (chuột phải vào thiết bị → Hủy ghép đôi…)."
)
OUT_NOT_READY = "“{x}” đang bận hoặc vừa có một lần ghép đôi không thành. Hãy chờ một lát rồi thử lại."
OUT_UNREACHABLE = (
    "Không kết nối được tới {t}. Hãy kiểm tra Teleport đang chạy trên máy đó, hai máy dùng chung một "
    "mạng, và tường lửa cho phép Teleport."
)
OUT_MISMATCH = (
    "Thiết bị ở {t} gửi danh tính không hợp lệ nên kết nối đã bị chặn. Có thể có người đang giả mạo."
)
OUT_VERSION = "“{x}” đang dùng phiên bản Teleport khác. Hãy cập nhật cả hai máy lên cùng phiên bản."
OUT_SELF = "Đó là chính máy này. Hãy nhập địa chỉ của máy kia."
OUT_BUSY = "Máy này đang có một phiên ghép đôi khác. Hãy thử lại sau."
OUT_SAVE_FAILED = (
    "Đã xác nhận ghép đôi với “{x}” nhưng không lưu được danh sách thiết bị đã ghép đôi. Hãy kiểm tra "
    "quyền ghi thư mục cấu hình rồi ghép đôi lại."
)
OUT_PROMPT_FAILED = "Không hiện được mã ghép đôi trên máy này nên đã hủy ghép đôi. Hãy thử lại."

# Tên độc do máy kia tự đặt (hợp đồng mục 2; mvp mục 7.2).
EVIL_NAMES = {
    "ma_gia": "PC-A:   382 104",
    "bidi": "PC-A‮401 283",
    "zalgo": "P̷̴̛C̶̡̢-̸̧À̵́̂̃",
    "html_b": "<b>html</b>",
    "html_img": "<img src=x>",
    "xuong_dong": "PC-A\n382 104",
    "ky_tu_trong": "PCㅤAㅤ382ㅤ104",
    "rat_dai": "M" * 40 + "382 104",
}


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def fill(template: str, **values: str) -> str:
    """Thay `{tên}` một lượt (giá trị chứa `{…}` không bị thay tiếp)."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def label(name: str | None, device_id: str = PEER_ID) -> str:
    from teleport.ui.texts import peer_label

    return peer_label(name, device_id)


# =================================================================================================
# Dựng dữ liệu
# =================================================================================================


def make_prompt(
    *,
    sid: int = 7,
    role: str = "responder",
    name: str = "MacBook",
    known: str | None = None,
    code: str = "905617",
    recent: int = 1,
    expires_at: float = NOW + 60.0,
    address: str = "192.168.1.5",
    device_id: str = PEER_ID,
):
    from teleport.app.pairing import PairingPrompt
    from teleport.core.pairing import Role

    return PairingPrompt(
        session_id=sid,
        role=Role(role),
        address=address,
        peer_name=name,
        peer_device_id=device_id,
        code=code,
        expires_at=expires_at,
        recent_codes=recent,
        known_name=known,
    )


def make_device(name: str = "May-B", device_id: str = PEER_ID):
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
    device_id: str | None = PEER_ID,
    known: str | None = None,
    code_shown: bool = False,
    device=None,
):
    from teleport.app.pairing import PairingFinished, PairingOutcome
    from teleport.core.pairing import Role

    outcome = PairingOutcome(outcome)
    if outcome is PairingOutcome.PAIRED and device is None:
        device = make_device(name or "May-B", device_id or PEER_ID)
    return PairingFinished(
        session_id=sid,
        role=Role(role),
        outcome=outcome,
        address=address,
        port=port,
        peer_name=name,
        peer_device_id=device_id,
        peer_reason=None,
        device=device,
        known_name=known,
        code_shown=code_shown,
    )


def make_view(
    *,
    name: str = "May-B",
    device_id: str = PEER_ID,
    paired: bool = False,
    online: bool = True,
    connected: bool = False,
    addresses: tuple[str, ...] = ("10.0.0.1",),
    port: int | None = 47474,
):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id,
        name=name,
        os="windows",
        paired=paired,
        online=online,
        connected=connected,
        addresses=addresses,
        port=port,
    )


_FAILURES = (
    "user_rejected",
    "timeout",
    "peer_rejected",
    "peer_timeout",
    "protocol_error",
    "peer_invalid",
    "peer_aborted",
    "connection_lost",
)


def expected_outcome(result, target: str | None = None) -> str | None:
    """Câu kết quả theo bảng của hợp đồng (mục 3). Outcome lạ → KeyError (thiếu câu)."""
    o = result.outcome.value
    role = result.role.value
    did = result.peer_device_id or PEER_ID
    if result.port is not None:
        t = f"{result.address}:{result.port}"
    elif target is not None:
        t = target
    else:
        t = result.address
    name = result.peer_name or result.known_name
    x = label(name, did) if name else t
    k = label(result.known_name, did) if result.known_name else x
    if o == "paired":
        return fill(OUT_PAIRED, x=label(result.device.name, result.device.device_id))
    if o in _FAILURES:
        if result.code_shown:
            return fill(OUT_NOT_DONE, x=x)
        return fill(OUT_EARLY, x=x) if role == "initiator" else None
    table = {
        "identity_conflict": None,
        "peer_busy": fill(OUT_NOT_READY, x=x),
        "rejected": fill(OUT_NOT_READY, x=x),
        "unreachable": fill(OUT_UNREACHABLE, t=t),
        "identity_mismatch": fill(OUT_MISMATCH, t=t),
        "version_mismatch": fill(OUT_VERSION, x=x),
        "self": OUT_SELF,
        "busy": OUT_BUSY,
        "save_failed": fill(OUT_SAVE_FAILED, x=x),
        "prompt_failed": OUT_PROMPT_FAILED,
        "cancelled": None,
    }
    if o == "identity_conflict":
        return fill(OUT_CONFLICT_R if role == "responder" else OUT_CONFLICT_I, k=k)
    return table[o]


# =================================================================================================
# Host giả, Node giả
# =================================================================================================


class FakeJob:
    def __init__(self) -> None:
        self.cancel_calls = 0
        self.done = False

    def cancel(self) -> None:
        self.cancel_calls += 1


class FakePairing:
    def __init__(self, node: FakeNode) -> None:
        self.node = node
        self.decisions: list[tuple[int, bool]] = []
        self.pair_calls: list[tuple[list[str], int]] = []
        self.pair_result = None

    def decide(self, session_id: int, accepted: bool) -> bool:
        self.node.log.append(("decide", session_id, accepted))
        self.decisions.append((session_id, accepted))
        return True

    async def pair_with(self, addresses, port):
        self.node.log.append(("pair_with", list(addresses), port))
        self.pair_calls.append((list(addresses), port))
        return self.pair_result


class FakeNode:
    """Node giả: chỉ những gì `PairingController` được phép đụng (hợp đồng mục 2)."""

    def __init__(self) -> None:
        self.log: list[tuple] = []
        self.views: tuple = ()
        self.pairing = FakePairing(self)
        self.hint_error: BaseException | None = None
        self.unpair_error: BaseException | None = None
        self.unpair_clear_history: list[bool] = []  # spec D2 1.7: cờ `clear_history` của mỗi lần unpair

    def devices(self):
        self.log.append(("devices",))
        return self.views

    def connect_hint(self, device_id, addresses, port) -> None:
        self.log.append(("connect_hint", device_id, list(addresses), port))
        if self.hint_error is not None:
            raise self.hint_error

    async def unpair(self, device_id, *, clear_history=False):
        self.log.append(("unpair", device_id))
        self.unpair_clear_history.append(clear_history)
        if self.unpair_error is not None:
            raise self.unpair_error
        return make_device(device_id=device_id)


class Pending:
    def __init__(self, fn, on_done, job=None) -> None:
        self.fn = fn
        self.on_done = on_done
        self.job = job
        self.ran = False


class FakeHost(QObject):
    """Host giả: signal như `NodeHost`, `call`/`submit` chỉ ghi lại; test tự chạy khi muốn."""

    pairing_prompt = Signal(object)
    pairing_finished = Signal(object)

    def __init__(self, node: FakeNode) -> None:
        super().__init__()
        self.node = node
        self.calls: list[Pending] = []
        self.submits: list[Pending] = []

    def call(self, fn, on_done=None) -> None:
        self.calls.append(Pending(fn, on_done))

    def submit(self, fn, on_done=None):
        job = FakeJob()
        self.submits.append(Pending(fn, on_done, job))
        return job

    # ---- Phía test ---------------------------------------------------------------------------

    def run_calls(self) -> list:
        """Chạy mọi `call` chưa chạy (kể cả call sinh ra trong lúc chạy), theo thứ tự."""
        out = []
        while True:
            pending = [p for p in self.calls if not p.ran]
            if not pending:
                return out
            for p in pending:
                p.ran = True
                try:
                    result = p.fn(self.node)
                except Exception as e:
                    result = e
                out.append(result)
                if p.on_done is not None:
                    p.on_done(result)

    def run_submit(self, p: Pending, *, deliver: bool = True):
        """Chạy coroutine của một `submit` trên loop riêng; trả kết quả hay lỗi."""
        p.ran = True
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(p.fn(self.node))
        except Exception as e:
            result = e
        finally:
            loop.close()
        if deliver:
            self.deliver(p, result)
        return result

    def deliver(self, p: Pending, result) -> None:
        p.ran = True
        if p.job is not None:
            p.job.done = True
        if p.on_done is not None:
            p.on_done(result)

    def decisions(self) -> list[tuple[int, bool]]:
        self.run_calls()
        return list(self.node.pairing.decisions)


class FakeClock:
    def __init__(self, value: float = NOW) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class Recorder:
    def __init__(self, c) -> None:
        self.devices: list = []
        self.messages: list[str] = []
        self.paired: list[str] = []
        self.finished: list = []
        self.opened: list[int] = []
        c.devices_changed.connect(self.devices.append)
        c.message.connect(self.messages.append)
        c.paired.connect(self.paired.append)
        c.finished.connect(self.finished.append)
        c.prompt_opened.connect(self.opened.append)


def closed(widget) -> bool:
    """Hộp đã đóng: bị xóa (WA_DeleteOnClose) hoặc không còn hiện."""
    import shiboken6

    return widget is None or not shiboken6.isValid(widget) or not widget.isVisible()


def find_button(root: QWidget, name: str) -> QAbstractButton:
    btn = root.findChild(QAbstractButton, name)
    assert btn is not None, f"thiếu nút objectName={name!r}"
    return btn


def texts_of(w: QWidget) -> list[str]:
    """Chữ hiện trên một hộp bất kể kiểu (QMessageBox hay QDialog tự dựng)."""
    out = [w.text()] if isinstance(w, QMessageBox) and w.text() else []
    out += [lb.text() for lb in w.findChildren(QLabel) if lb.isVisibleTo(w) and lb.text()]
    return out


def all_plain(w: QWidget) -> bool:
    if isinstance(w, QMessageBox) and w.textFormat() != Qt.TextFormat.PlainText:
        return False
    return all(lb.textFormat() == Qt.TextFormat.PlainText for lb in w.findChildren(QLabel) if lb.text())


def button_with_text(w: QWidget, text: str) -> QAbstractButton:
    found = [
        b for b in w.findChildren(QAbstractButton) if b.text().replace("&", "") == text and b.isVisibleTo(w)
    ]
    assert len(found) == 1, (
        f"phải có đúng một nút “{text}”: {[b.text() for b in w.findChildren(QAbstractButton)]}"
    )
    return found[0]


@pytest.fixture(autouse=True)
def no_exec(monkeypatch):
    """Không hộp nào được mở bằng `exec()` (bẫy: chặn vòng sự kiện, hợp đồng mục 2)."""
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
    """`ctl(**kw) -> SimpleNamespace(c, host, node, clock, rec, notes)`: controller nối với host giả."""
    from teleport.ui.pairing import PairingController

    made: list = []

    def make(**kw):
        node = FakeNode()
        host = FakeHost(node)
        clock = kw.pop("clock", FakeClock())
        notes: list[tuple[str, str]] = []
        kw.setdefault("notify", lambda title, text: notes.append((title, text)))
        c = PairingController(clock=clock, **kw)
        c.set_host(host)
        ns = SimpleNamespace(c=c, host=host, node=node, clock=clock, rec=Recorder(c), notes=notes)
        made.append(ns)
        return ns

    yield make

    for ns in made:
        with contextlib.suppress(Exception):
            ns.c.close_all()
        with contextlib.suppress(Exception):
            ns.c.set_host(None)
    qtbot.wait(10)


def open_prompt(qtbot, ns, prompt):
    ns.host.pairing_prompt.emit(prompt)
    qtbot.waitUntil(lambda: ns.c.dialog(prompt.session_id) is not None, timeout=2_000)
    dlg = ns.c.dialog(prompt.session_id)
    qtbot.waitUntil(dlg.isVisible, timeout=2_000)
    QApplication.processEvents()
    return dlg


def top_of(widget: QWidget, root: QWidget) -> int:
    return widget.mapTo(root, QPoint(0, 0)).y()


def visible_labels(root: QWidget) -> list[QLabel]:
    labels = [w for w in root.findChildren(QLabel) if w.isVisibleTo(root)]
    return sorted(labels, key=lambda w: (top_of(w, root), w.mapTo(root, QPoint(0, 0)).x()))


def font_size(w: QWidget) -> float:
    f = w.font()
    return f.pointSizeF() if f.pointSizeF() > 0 else float(f.pixelSize())


# =================================================================================================
# (a) Hàm chữ
# =================================================================================================


def test_prompt_header_by_role():
    from teleport.core.pairing import Role
    from teleport.ui.pairing import prompt_header

    assert prompt_header(Role.RESPONDER) == HEADER_RESPONDER
    assert prompt_header(Role.INITIATOR) == HEADER_INITIATOR


def test_code_text_is_only_the_formatted_code():
    from teleport.ui.pairing import code_text

    assert code_text(make_prompt(code="048213", name="PC-A:   382 104")) == "048 213"


@pytest.mark.parametrize("key", sorted(EVIL_NAMES))
def test_code_text_never_contains_peer_name(key):
    from teleport.ui.pairing import code_text

    name = EVIL_NAMES[key]
    assert code_text(make_prompt(code="905617", name=name, known=name)) == "905 617"


def test_code_warning_reads_warn_recent_codes_at_use(monkeypatch):
    from teleport.core import pairing as core_pairing
    from teleport.ui.pairing import code_warning

    monkeypatch.setattr(core_pairing, "WARN_RECENT_CODES", 5)
    assert code_warning(make_prompt(role="responder", recent=4)) is None
    assert code_warning(make_prompt(role="responder", recent=5)) == fill(WARNING, n="5")
    assert code_warning(make_prompt(role="responder", recent=6)) == fill(WARNING, n="6")


def test_code_warning_at_threshold_for_responder():
    from teleport.core.pairing import WARN_RECENT_CODES
    from teleport.ui.pairing import code_warning

    assert code_warning(make_prompt(role="responder", recent=WARN_RECENT_CODES - 1)) is None
    n = WARN_RECENT_CODES
    assert code_warning(make_prompt(role="responder", recent=n)) == fill(WARNING, n=str(n))


def test_code_warning_never_for_initiator():
    from teleport.ui.pairing import code_warning

    assert code_warning(make_prompt(role="initiator", recent=50)) is None


def test_peer_lines_without_known_name():
    from teleport.ui.pairing import peer_lines

    p = make_prompt(name="MacBook Pro", known=None)
    assert peer_lines(p) == [fill(LINE_PEER, name="MacBook Pro", id8=PEER_ID[:8], address="192.168.1.5")]


def test_peer_lines_known_name_same_has_no_rename_line():
    from teleport.ui.pairing import peer_lines

    p = make_prompt(name="MacBook", known="MacBook")
    assert peer_lines(p) == [
        fill(LINE_PEER, name="MacBook", id8=PEER_ID[:8], address="192.168.1.5"),
        fill(LINE_KNOWN, id8=PEER_ID[:8], known="MacBook"),
    ]


def test_peer_lines_known_name_differs_adds_rename_line():
    from teleport.ui.pairing import peer_lines

    p = make_prompt(name="MacBook Pro", known="MacBook")
    assert peer_lines(p) == [
        fill(LINE_PEER, name="MacBook Pro", id8=PEER_ID[:8], address="192.168.1.5"),
        fill(LINE_KNOWN, id8=PEER_ID[:8], known="MacBook"),
        fill(LINE_RENAME, name="MacBook Pro"),
    ]


@pytest.mark.parametrize("key", sorted(EVIL_NAMES))
def test_peer_lines_use_peer_label_for_names(key):
    from teleport.ui.pairing import peer_lines

    name = EVIL_NAMES[key]
    known = "Cuㅤ" + name
    lines = peer_lines(make_prompt(name=name, known=known))
    assert lines == [
        fill(LINE_PEER, name=label(name), id8=PEER_ID[:8], address="192.168.1.5"),
        fill(LINE_KNOWN, id8=PEER_ID[:8], known=label(known)),
        fill(LINE_RENAME, name=label(name)),
    ]
    for line in lines:
        assert "\n" not in line, "tên có xuống dòng không được tách dòng (peer_label đổi thành U+FFFD)"
        assert "ㅤ" not in line, "ký tự trống U+3164 phải hiện thành ·"


def test_peer_lines_fill_values_in_one_pass():
    """Tên chứa `{address}`… không bị thay tiếp (chèn một lượt như `texts._fill`)."""
    from teleport.ui.pairing import peer_lines

    name = "{name}{known}{id8}{address}{n}"
    lines = peer_lines(make_prompt(name=name, known=None))
    assert lines == [fill(LINE_PEER, name=name, id8=PEER_ID[:8], address="192.168.1.5")]


def test_long_name_is_cut_to_32_columns_in_peer_lines():
    from teleport.core.display import text_width
    from teleport.ui.pairing import peer_lines

    name = "N" * 60
    line = peer_lines(make_prompt(name=name))[0]
    shown = line.split("“", 1)[1].split("”", 1)[0]
    assert shown.endswith("…") and text_width(shown) <= 32


def test_small_texts():
    from teleport.ui.pairing import pairing_progress_text, prompt_notification, unpair_confirm_text

    assert pairing_progress_text("May-B") == fill(PROGRESS, name="May-B")
    assert pairing_progress_text("192.168.1.5:47474") == fill(PROGRESS, name="192.168.1.5:47474")
    assert unpair_confirm_text("May-B") == fill(UNPAIR_CONFIRM, name="May-B")
    assert prompt_notification() == NOTIFICATION


def _outcome_cases():
    from teleport.app.pairing import PairingOutcome

    cases = []
    for outcome in PairingOutcome:
        for role in ("initiator", "responder"):
            for shown in (False, True):
                cases.append(pytest.param(outcome.value, role, shown, id=f"{outcome.value}-{role}-{shown}"))
    return cases


def _none_expected(outcome: str, role: str, shown: bool) -> bool:
    return outcome == "cancelled" or (outcome in _FAILURES and role == "responder" and not shown)


@pytest.mark.parametrize(("outcome", "role", "shown"), _outcome_cases())
def test_outcome_text_for_every_outcome_role_and_code_shown(outcome, role, shown):
    """Xong khi (3): mỗi `PairingOutcome` có câu; None chỉ ở các ca hợp đồng mục 3 nói None."""
    from teleport.ui.pairing import outcome_text

    port = 47474 if role == "initiator" else None
    result = make_finished(outcome, role=role, port=port, code_shown=shown, known="MacBook cu")
    text = outcome_text(result)
    if _none_expected(outcome, role, shown):
        assert text is None
        return
    assert text is not None, "outcome này phải có câu"
    expected = expected_outcome(result)
    if outcome == "identity_conflict" and role == "initiator":
        assert text.startswith(expected), text
        rest = text[len(expected) :]
        assert rest.strip() == OUT_CONFLICT_HINT and "\n" in rest, "gợi ý hủy ghép đôi nằm ở dòng riêng"
    else:
        assert text == expected
    assert "teleport listen" not in text and "`" not in text, "câu GUI không gợi ý lệnh CLI"


def test_outcome_text_uses_target_when_port_unknown():
    """Phiên thay thế ở vai trả lời có `port None`: {target} lấy ip:port UI tự giữ."""
    from teleport.ui.pairing import outcome_text

    r = make_finished("unreachable", role="responder", port=None, name=None, device_id=None)
    assert outcome_text(r, target="192.168.1.5:5555") == fill(OUT_UNREACHABLE, t="192.168.1.5:5555")
    assert outcome_text(r) == fill(OUT_UNREACHABLE, t="192.168.1.5")
    r2 = make_finished("identity_mismatch", port=47474, name=None, device_id=None)
    assert outcome_text(r2) == fill(OUT_MISMATCH, t="192.168.1.5:47474")


def test_outcome_text_name_fallback_order():
    """X = peer_name → tên đã biết → target → address."""
    from teleport.ui.pairing import outcome_text

    r = make_finished("peer_busy", name="Moi", known="Cu")
    assert outcome_text(r) == fill(OUT_NOT_READY, x="Moi")
    r = make_finished("peer_busy", name=None, known="Cu")
    assert outcome_text(r) == fill(OUT_NOT_READY, x="Cu")
    r = make_finished("peer_busy", name=None, known=None, device_id=None, port=None)
    assert outcome_text(r, target="192.168.1.5:5555") == fill(OUT_NOT_READY, x="192.168.1.5:5555")
    assert outcome_text(r) == fill(OUT_NOT_READY, x="192.168.1.5")


@pytest.mark.parametrize("key", sorted(EVIL_NAMES))
def test_outcome_text_names_go_through_peer_label(key):
    from teleport.ui.pairing import outcome_text

    name = EVIL_NAMES[key]
    r = make_finished("timeout", name=name, code_shown=True)
    assert outcome_text(r) == fill(OUT_NOT_DONE, x=label(name))
    r = make_finished("paired", device=make_device(name))
    assert outcome_text(r) == fill(OUT_PAIRED, x=label(name))


# =================================================================================================
# (a) PairingDialog (mở qua controller, không phụ thuộc chữ ký hàm dựng)
# =================================================================================================


@pytest.mark.parametrize("key", sorted(EVIL_NAMES))
@pytest.mark.parametrize("role", ["responder", "initiator"])
def test_dialog_code_stands_alone_before_any_peer_text(qtbot, ctl, key, role):
    """Xong khi (1), mvp 7.2: tên độc không đứng trước / cùng nhãn với mã; nhãn mã chỉ có mã."""
    from teleport.ui.pairing import code_text, peer_lines, prompt_header

    ns = ctl()
    name = EVIL_NAMES[key]
    p = make_prompt(role=role, name=name, known=name + "x", code="905617", recent=1)
    dlg = open_prompt(qtbot, ns, p)

    assert dlg.windowTitle() == TITLE
    code = dlg.findChild(QLabel, "pairingCode")
    assert code is not None and code.isVisibleTo(dlg)
    assert code.text() == code_text(p) == "905 617"
    code_top = top_of(code, dlg)
    code_bottom = code_top + code.height()

    labels = visible_labels(dlg)
    others = [lb for lb in labels if lb is not code]
    same_row = [
        lb for lb in others if top_of(lb, dlg) < code_bottom and top_of(lb, dlg) + lb.height() > code_top
    ]
    assert not [lb.text() for lb in same_row if lb.text()], "có chữ cùng hàng với mã"
    above = [lb for lb in others if top_of(lb, dlg) + lb.height() <= code_top]
    for lb in above:
        assert lb.text() in ("", prompt_header(p.role)), f"chữ đứng trước mã: {lb.text()!r}"
    assert any(lb.text() == prompt_header(p.role) for lb in above), "thiếu dòng 1 phía trên mã"

    peers = [lb for lb in labels if lb.objectName() == "pairingPeer"]
    assert peers, "thiếu nhãn pairingPeer"
    assert all(top_of(lb, dlg) >= code_bottom for lb in peers)
    assert "\n".join(lb.text() for lb in peers) == "\n".join(peer_lines(p))
    for lb in labels:
        if lb is code:
            continue
        assert "905 617" not in lb.text() and "905617" not in lb.text(), "mã chỉ ở nhãn pairingCode"
    assert font_size(code) > max(font_size(lb) for lb in labels if lb is not code), "mã là chữ lớn"


@pytest.mark.parametrize("key", sorted(EVIL_NAMES))
def test_dialog_labels_are_plain_text_and_tooltips_have_no_peer_name(qtbot, ctl, key):
    ns = ctl()
    name = EVIL_NAMES[key]
    p = make_prompt(name=name, known="<i>cu</i>" + name)
    dlg = open_prompt(qtbot, ns, p)
    for lb in dlg.findChildren(QLabel):
        assert lb.textFormat() == Qt.TextFormat.PlainText, f"nhãn {lb.objectName()!r} không PlainText"
    pieces = {name, label(name), "<i>cu</i>", "MacBook"}
    pieces |= {part for part in re.split(r"\s+", name) if len(part) >= 3}
    for w in [dlg, *dlg.findChildren(QWidget)]:
        for attr in ("toolTip", "whatsThis", "statusTip"):
            value = getattr(w, attr)()
            for piece in pieces:
                assert piece not in value, f"{attr} của {w.objectName()!r} chứa chữ máy kia: {value!r}"
        for attr in ("accessibleName", "accessibleDescription"):
            value = getattr(w, attr)()
            if label(name) != name:
                assert name not in value, f"{attr} chứa tên thô chưa qua peer_label: {value!r}"
            if "ㅤ" in name:
                assert "ㅤ" not in value, f"{attr} chứa ký tự trống chưa qua peer_label"


def test_dialog_window_properties(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt())
    assert isinstance(dlg, QDialog)
    assert dlg.parent() is None
    assert dlg.windowModality() == Qt.WindowModality.NonModal
    assert dlg.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    assert dlg.session_id == 7
    assert dlg.windowTitle() == TITLE


def test_dialog_buttons_default_is_reject(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt())
    reject = find_button(dlg, "pairingReject")
    accept = find_button(dlg, "pairingAccept")
    assert reject.text().replace("&", "") == REJECT
    assert accept.text().replace("&", "") == ACCEPT
    assert reject.isDefault(), "nút mặc định phải là “Không trùng”"
    assert not accept.isDefault()
    assert not accept.autoDefault(), "“Trùng khớp” không được autoDefault"
    assert top_of(reject, dlg) > top_of(dlg.findChild(QLabel, "pairingCode"), dlg)


def test_dialog_warning_right_below_code_for_responder(qtbot, ctl):
    from teleport.core.pairing import WARN_RECENT_CODES

    ns = ctl()
    n = WARN_RECENT_CODES
    dlg = open_prompt(qtbot, ns, make_prompt(role="responder", recent=n, known="MacBook"))
    warn = dlg.findChild(QLabel, "pairingWarning")
    assert warn is not None and warn.isVisibleTo(dlg)
    assert warn.text() == fill(WARNING, n=str(n))
    assert warn.textFormat() == Qt.TextFormat.PlainText
    code_y = top_of(dlg.findChild(QLabel, "pairingCode"), dlg)
    peers = [lb for lb in dlg.findChildren(QLabel) if lb.objectName() == "pairingPeer"]
    warn_y = top_of(warn, dlg)
    assert code_y < warn_y < min(top_of(lb, dlg) for lb in peers), "cảnh báo nằm ngay dưới mã"
    between = [
        lb for lb in visible_labels(dlg) if code_y < top_of(lb, dlg) < warn_y and lb.text() and lb is not warn
    ]
    assert not between, f"có chữ chen giữa mã và cảnh báo: {[lb.text() for lb in between]}"


def test_dialog_no_warning_below_threshold(qtbot, ctl):
    from teleport.core.pairing import WARN_RECENT_CODES

    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(role="responder", recent=WARN_RECENT_CODES - 1))
    warn = dlg.findChild(QLabel, "pairingWarning")
    assert warn is None or not warn.isVisibleTo(dlg) or not warn.text()


def test_dialog_no_warning_for_initiator(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(role="initiator", recent=99))
    warn = dlg.findChild(QLabel, "pairingWarning")
    assert warn is None or not warn.isVisibleTo(dlg) or not warn.text()


def test_dialog_shows_known_and_new_name(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(name="MacBook Pro", known="MacBook"))
    text = "\n".join(lb.text() for lb in visible_labels(dlg) if lb.objectName() == "pairingPeer")
    assert fill(LINE_KNOWN, id8=PEER_ID[:8], known="MacBook") in text
    assert fill(LINE_RENAME, name="MacBook Pro") in text


# =================================================================================================
# (b) PairingController: hộp mã
# =================================================================================================


def test_prompt_opens_one_dialog_per_session(qtbot, ctl):
    ns = ctl()
    p = make_prompt(sid=3)
    open_prompt(qtbot, ns, p)
    ns.host.pairing_prompt.emit(p)
    qtbot.wait(50)
    assert len(ns.c.dialogs()) == 1
    assert ns.rec.opened == [3]


@pytest.mark.parametrize("role", ["responder", "initiator"])
def test_prompt_opens_dialog_for_both_roles(qtbot, ctl, role):
    """9.3: hộp hiện ở cả hai vai."""
    from teleport.ui.pairing import prompt_header

    ns = ctl()
    p = make_prompt(role=role)
    dlg = open_prompt(qtbot, ns, p)
    assert any(lb.text() == prompt_header(p.role) for lb in dlg.findChildren(QLabel))


def test_dialog_closes_at_expiry_without_decide(qtbot, ctl):
    """9.3: hết `expires_at` chỉ đóng hộp, KHÔNG decide (lõi tự TIMEOUT)."""
    clock = FakeClock(NOW)
    ns = ctl(clock=clock)
    dlg = open_prompt(qtbot, ns, make_prompt(expires_at=NOW + 0.3))
    qtbot.waitUntil(lambda: closed(dlg), timeout=3_000)
    qtbot.wait(50)
    assert ns.c.dialog(7) is None
    assert ns.host.decisions() == []


def test_dialog_stays_open_before_expiry(qtbot, ctl):
    ns = ctl(clock=FakeClock(NOW))
    dlg = open_prompt(qtbot, ns, make_prompt(expires_at=NOW + 60))
    qtbot.wait(400)
    assert not closed(dlg)
    assert ns.host.decisions() == []


def test_already_expired_prompt_never_decides(qtbot, ctl):
    ns = ctl(clock=FakeClock(NOW))
    ns.host.pairing_prompt.emit(make_prompt(expires_at=NOW - 1))
    qtbot.wait(300)
    d = ns.c.dialog(7)
    assert d is None or closed(d)
    assert ns.host.decisions() == []


def test_escape_decides_false_once(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(sid=11))
    qtbot.keyClick(dlg, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: closed(dlg), timeout=2_000)
    assert ns.host.decisions() == [(11, False)]


def test_close_button_x_decides_false_once(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(sid=12))
    dlg.close()
    qtbot.wait(20)
    assert ns.host.decisions() == [(12, False)]


def test_reject_button_decides_false_once_even_if_closed_after(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(sid=13))
    find_button(dlg, "pairingReject").click()
    qtbot.wait(20)
    if not closed(dlg):
        dlg.close()
        qtbot.wait(20)
    assert ns.host.decisions() == [(13, False)]


def test_enter_key_is_reject(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(sid=14))
    qtbot.keyClick(dlg, Qt.Key.Key_Return)
    qtbot.wait(20)
    assert ns.host.decisions() == [(14, False)]


def test_accept_button_decides_true_once(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(sid=15))
    find_button(dlg, "pairingAccept").click()
    qtbot.wait(20)
    if not closed(dlg):
        dlg.close()
        qtbot.keyClick(dlg, Qt.Key.Key_Escape)
        qtbot.wait(20)
    assert ns.host.decisions() == [(15, True)]


def test_decide_goes_through_host_call_not_submit(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(sid=16))
    find_button(dlg, "pairingAccept").click()
    qtbot.wait(20)
    assert len(ns.host.calls) == 1 and not ns.host.submits
    assert ns.node.pairing.decisions == [], "Node chỉ được chạm bên trong fn của call"
    ns.host.run_calls()
    assert ns.node.pairing.decisions == [(16, True)]


def test_finished_same_session_closes_dialog_without_decide(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(sid=21, role="responder"))
    ns.host.pairing_finished.emit(
        make_finished("peer_rejected", sid=21, role="responder", port=None, code_shown=True)
    )
    qtbot.waitUntil(lambda: closed(dlg), timeout=2_000)
    qtbot.wait(20)
    assert ns.host.decisions() == []
    assert [r.session_id for r in ns.rec.finished] == [21]


def test_finished_other_session_keeps_dialog(qtbot, ctl):
    ns = ctl()
    dlg = open_prompt(qtbot, ns, make_prompt(sid=22))
    ns.host.pairing_finished.emit(make_finished("timeout", sid=99, role="responder", port=None))
    qtbot.wait(50)
    assert not closed(dlg)


def test_hidden_window_notifies_tray(qtbot, ctl):
    ns = ctl(window_visible=lambda: False)
    open_prompt(qtbot, ns, make_prompt())
    assert ns.notes == [NOTIFICATION]


def test_visible_window_does_not_notify(qtbot, ctl):
    ns = ctl(window_visible=lambda: True)
    open_prompt(qtbot, ns, make_prompt())
    assert ns.notes == []


# =================================================================================================
# (b) PairingController: kết quả
# =================================================================================================


@pytest.mark.parametrize(("outcome", "role", "shown"), _outcome_cases())
def test_finished_shows_outcome_text_in_result_box(qtbot, ctl, outcome, role, shown):
    from teleport.ui.pairing import outcome_text

    ns = ctl()
    port = 47474 if role == "initiator" else None
    r = make_finished(outcome, role=role, port=port, code_shown=shown, known="Cu", sid=40)
    ns.host.pairing_finished.emit(r)
    qtbot.wait(20)
    ns.host.run_calls()
    qtbot.wait(20)
    text = outcome_text(r)
    if text is None:
        assert ns.rec.messages == []
        box = ns.c.result_box
        assert box is None or not box.isVisible()
    elif outcome == "paired":
        # PR-7 B4: thành công không mở hộp, chỉ phát `notice` (dải thông báo) và `message`.
        assert ns.rec.messages == [text]
        assert ns.c.result_box is None
    else:
        assert ns.rec.messages == [text]
        box = ns.c.result_box
        assert box is not None and box.isVisible()
        assert text in texts_of(box)
        assert all_plain(box)
        assert box.windowModality() == Qt.WindowModality.NonModal
        if sys.platform != "darwin":
            assert box.windowTitle() == TITLE
    assert ns.rec.finished == [r]


def test_flood_of_failed_results_reuses_one_result_box(qtbot, ctl, ui_helpers):
    """Dội 50 `pairing_finished` lỗi → chỉ một hộp kết quả (chống dội)."""
    ns = ctl()
    for i in range(50):
        ns.host.pairing_finished.emit(
            make_finished("timeout", sid=100 + i, role="responder", port=None, code_shown=True, name=f"M{i}")
        )
    qtbot.wait(50)
    boxes = [
        w
        for w in QApplication.topLevelWidgets()
        if w.isVisible() and any("chưa xong" in t for t in texts_of(w))
    ]
    assert len(boxes) == 1, [texts_of(b) for b in boxes]
    assert boxes[0] is ns.c.result_box
    assert fill(OUT_NOT_DONE, x="M49") in texts_of(ns.c.result_box)


def test_paired_from_incoming_reads_devices_in_one_call(qtbot, ctl):
    """9.6: PAIRED → một `call` đọc lại `devices()` → `devices_changed`, rồi `paired`."""
    ns = ctl()
    ns.node.views = (make_view(paired=True),)
    ns.host.pairing_finished.emit(make_finished("paired", sid=5, role="responder", port=None, name="May-B"))
    qtbot.wait(20)
    assert len(ns.host.calls) == 1 and not ns.host.submits
    ns.host.run_calls()
    qtbot.wait(20)
    assert ns.node.log == [("devices",)], "không ghép bằng IP thì không connect_hint"
    assert ns.rec.devices == [ns.node.views]
    assert ns.rec.paired == [PEER_ID]
    assert ns.rec.messages == [fill(OUT_PAIRED, x="May-B")]


# =================================================================================================
# (b) PairingController: ghép đôi chủ động
# =================================================================================================


@pytest.mark.parametrize(
    "view",
    [
        pytest.param(dict(online=False, addresses=(), port=None), id="offline"),
        pytest.param(dict(online=True, addresses=(), port=47474), id="khong-dia-chi"),
        pytest.param(dict(online=True, addresses=("10.0.0.1",), port=None), id="khong-cong"),
        pytest.param(dict(online=False, addresses=("10.0.0.1",), port=47474), id="offline-co-dia-chi"),
    ],
)
def test_pair_device_refuses_offline_or_without_address(qtbot, ctl, view):
    """9.1: chỉ ghép khi online và có địa chỉ, cổng; không thì False, không làm gì."""
    ns = ctl()
    assert ns.c.pair_device(make_view(**view)) is False
    qtbot.wait(20)
    assert not ns.host.submits and not ns.host.calls
    assert not ns.c.busy


def test_pair_device_without_host_returns_false(qtbot, ctl):
    ns = ctl()
    ns.c.set_host(None)
    assert ns.c.pair_device(make_view()) is False
    assert ns.c.pair_address("10.0.0.1", 47474) is False
    assert not ns.host.submits


def test_pair_device_uses_fresh_addresses_from_devices(qtbot, ctl):
    """9.6: radar có thể giữ IP cũ; coroutine đọc lại `devices()` rồi `pair_with` địa chỉ mới."""
    ns = ctl()
    ns.node.views = (make_view(addresses=("10.0.0.2", "10.0.0.3"), port=5000),)
    assert ns.c.pair_device(make_view(addresses=("10.0.0.1",), port=47474)) is True
    assert ns.c.busy
    assert len(ns.host.submits) == 1
    assert ns.node.log == [], "Node chỉ được chạm bên trong fn"
    ns.host.run_submit(ns.host.submits[0], deliver=False)
    assert ns.node.pairing.pair_calls == [(["10.0.0.2", "10.0.0.3"], 5000)]


@pytest.mark.parametrize("gone", ["offline", "missing"])
def test_pair_device_gone_from_network_shows_text(qtbot, ctl, gone):
    ns = ctl()
    if gone == "offline":
        ns.node.views = (make_view(online=False, addresses=(), port=None),)
    assert ns.c.pair_device(make_view(name="May-B")) is True
    ns.host.run_submit(ns.host.submits[0])
    qtbot.wait(20)
    assert ns.node.pairing.pair_calls == []
    assert ns.rec.messages == [fill(GONE, name="May-B")]
    assert not ns.c.busy
    box = ns.c.progress_box
    assert box is None or not box.isVisible()


def test_pair_device_shows_progress_box_with_cancel(qtbot, ctl):
    ns = ctl()
    ns.node.views = (make_view(),)
    assert ns.c.pair_device(make_view(name="<b>May-B</b>")) is True
    qtbot.wait(20)
    box = ns.c.progress_box
    assert box is not None and box.isVisible()
    assert fill(PROGRESS, name="<b>May-B</b>") in texts_of(box)
    assert all_plain(box), "chữ máy kia đặt không được hiểu là rich text"
    assert box.parent() is None
    assert box.windowModality() == Qt.WindowModality.NonModal
    button_with_text(box, CANCEL).click()
    qtbot.wait(20)
    assert ns.host.submits[0].job.cancel_calls == 1


def test_cancel_pairing_cancels_job_and_cancelled_shows_nothing(qtbot, ctl):
    ns = ctl()
    ns.node.views = (make_view(),)
    ns.c.pair_device(make_view())
    ns.c.cancel_pairing()
    p = ns.host.submits[0]
    assert p.job.cancel_calls == 1
    ns.host.pairing_finished.emit(make_finished("cancelled", sid=1, address="10.0.0.1"))
    ns.host.deliver(p, asyncio.CancelledError())
    qtbot.wait(20)
    assert not ns.c.busy
    assert ns.rec.messages == []
    box = ns.c.progress_box
    assert box is None or not box.isVisible()


def test_second_pair_while_busy_shows_busy_text(qtbot, ctl):
    ns = ctl()
    ns.node.views = (make_view(),)
    assert ns.c.pair_device(make_view()) is True
    assert ns.c.pair_device(make_view(device_id=OTHER_ID, name="May-C")) is False
    assert ns.c.pair_address("10.0.0.9", 47474) is False
    qtbot.wait(20)
    assert len(ns.host.submits) == 1
    assert ns.rec.messages and ns.rec.messages[-1] == OUT_BUSY


def test_can_pair_again_after_job_done(qtbot, ctl):
    ns = ctl()
    assert ns.c.pair_address("10.0.0.1", 47474) is True
    p = ns.host.submits[0]
    r = make_finished("unreachable", address="10.0.0.1", port=47474, name=None, device_id=None)
    ns.host.run_submit(p, deliver=False)
    ns.host.pairing_finished.emit(r)
    ns.host.deliver(p, r)
    qtbot.wait(20)
    assert not ns.c.busy
    assert ns.rec.messages == [fill(OUT_UNREACHABLE, t="10.0.0.1:47474")]
    assert ns.c.pair_address("10.0.0.1", 47474) is True


def test_runtime_error_from_stopped_host_clears_job(qtbot, ctl):
    ns = ctl()
    ns.c.pair_address("10.0.0.1", 47474)
    ns.host.deliver(ns.host.submits[0], RuntimeError("node chưa chạy hoặc đang đóng"))
    qtbot.wait(20)
    assert not ns.c.busy
    box = ns.c.progress_box
    assert box is None or not box.isVisible()


def test_progress_box_closes_when_code_dialog_opens(qtbot, ctl):
    ns = ctl()
    ns.c.pair_address("10.0.0.1", 47474)
    qtbot.wait(20)
    box = ns.c.progress_box
    assert box is not None and box.isVisible()
    assert fill(PROGRESS, name="10.0.0.1:47474") in texts_of(box)
    open_prompt(qtbot, ns, make_prompt(role="initiator", address="10.0.0.1"))
    qtbot.wait(20)
    assert closed(box)


def test_pair_address_calls_pair_with_single_ip(qtbot, ctl):
    ns = ctl()
    assert ns.c.pair_address("10.0.0.1", 47474) is True
    ns.host.run_submit(ns.host.submits[0], deliver=False)
    assert ns.node.pairing.pair_calls == [(["10.0.0.1"], 47474)]


@pytest.mark.parametrize("replacement", [False, True], ids=["khoi-xuong", "phien-thay-the"])
def test_paired_by_ip_connect_hint_then_devices_in_one_call(qtbot, ctl, replacement):
    """9.2, D10: PAIRED khi ghép bằng IP → MỘT call: connect_hint(id, [ip], port) rồi devices()."""
    ns = ctl()
    ns.node.views = (make_view(paired=True),)
    assert ns.c.pair_address("10.0.0.1", 5555) is True
    p = ns.host.submits[0]
    if replacement:  # hai bên cùng chủ động: kết quả ở vai trả lời, port None, session_id khác
        r = make_finished("paired", sid=99, role="responder", address="10.0.0.1", port=None, name="May-B")
    else:
        r = make_finished("paired", sid=1, role="initiator", address="10.0.0.1", port=5555, name="May-B")
    ns.host.pairing_finished.emit(r)
    ns.host.deliver(p, r)
    qtbot.wait(20)
    assert len(ns.host.calls) == 1
    ns.host.run_calls()
    qtbot.wait(20)
    assert ns.node.log == [("connect_hint", PEER_ID, ["10.0.0.1"], 5555), ("devices",)]
    assert ns.rec.devices == [ns.node.views]
    assert ns.rec.paired == [PEER_ID]
    assert fill(OUT_PAIRED, x="May-B") in ns.rec.messages


def test_paired_from_other_address_no_connect_hint(qtbot, ctl):
    ns = ctl()
    ns.c.pair_address("10.0.0.1", 5555)
    ns.host.pairing_finished.emit(
        make_finished("paired", sid=8, role="responder", address="10.0.0.77", port=None, device_id=OTHER_ID)
    )
    qtbot.wait(20)
    ns.host.run_calls()
    assert ("connect_hint", OTHER_ID, ["10.0.0.77"], 5555) not in ns.node.log
    assert not any(e[0] == "connect_hint" for e in ns.node.log)
    assert ns.node.log.count(("devices",)) == 1


@pytest.mark.parametrize("error", [KeyError(PEER_ID), ValueError("x")], ids=["KeyError", "ValueError"])
def test_connect_hint_error_still_reads_devices(qtbot, ctl, error):
    ns = ctl()
    ns.node.hint_error = error
    ns.node.views = (make_view(paired=True),)
    ns.c.pair_address("10.0.0.1", 5555)
    r = make_finished("paired", address="10.0.0.1", port=5555, name="May-B")
    ns.host.pairing_finished.emit(r)
    ns.host.deliver(ns.host.submits[0], r)
    qtbot.wait(20)
    ns.host.run_calls()
    qtbot.wait(20)
    assert ("devices",) in ns.node.log
    assert ns.rec.devices == [ns.node.views]
    assert ns.rec.paired == [PEER_ID]


# =================================================================================================
# (b) Hủy ghép đôi
# =================================================================================================


def test_request_unpair_opens_confirm_with_default_no(qtbot, ctl):
    ns = ctl()
    ns.c.request_unpair(PEER_ID, "<b>May-B</b>")
    qtbot.wait(20)
    box = ns.c.unpair_box
    assert box is not None and box.isVisible()
    assert box.windowModality() == Qt.WindowModality.NonModal
    assert all_plain(box)
    assert fill(UNPAIR_CONFIRM, name="<b>May-B</b>") in texts_of(box)
    if sys.platform != "darwin":
        assert box.windowTitle() == UNPAIR_TITLE
    button_with_text(box, UNPAIR_YES)
    no = button_with_text(box, UNPAIR_NO)
    assert no.isDefault(), "nút mặc định phải là “Không”"
    assert not ns.host.submits and not ns.host.calls


def test_request_unpair_no_does_nothing(qtbot, ctl):
    ns = ctl()
    ns.c.request_unpair(PEER_ID, "May-B")
    qtbot.wait(20)
    button_with_text(ns.c.unpair_box, UNPAIR_NO).click()
    qtbot.wait(20)
    assert not ns.host.submits and not ns.host.calls


def test_unpair_confirmed_unpairs_and_reads_devices_in_one_submit(qtbot, ctl):
    """D11, 9.6: đồng ý → MỘT submit `await unpair(id)` rồi `devices()` → devices_changed."""
    ns = ctl()
    ns.node.views = (make_view(paired=False),)
    ns.c.request_unpair(PEER_ID, "May-B")
    qtbot.wait(20)
    button_with_text(ns.c.unpair_box, UNPAIR_YES).click()
    qtbot.wait(20)
    assert len(ns.host.submits) == 1 and not ns.host.calls
    ns.host.run_submit(ns.host.submits[0])
    qtbot.wait(20)
    assert ns.node.log == [("unpair", PEER_ID), ("devices",)]
    assert ns.rec.devices == [ns.node.views]
    assert ns.rec.messages == [fill(UNPAIRED, name="May-B")]


def test_unpair_key_error_still_reads_devices(qtbot, ctl):
    ns = ctl()
    ns.node.unpair_error = KeyError(PEER_ID)
    ns.node.views = ()
    ns.c.request_unpair(PEER_ID, "May-B")
    qtbot.wait(20)
    button_with_text(ns.c.unpair_box, UNPAIR_YES).click()
    qtbot.wait(20)
    ns.host.run_submit(ns.host.submits[0])
    qtbot.wait(20)
    ns.host.run_calls()
    qtbot.wait(20)
    assert ("devices",) in ns.node.log
    assert ns.rec.devices == [()]
    assert fill(UNPAIR_FAILED, name="May-B") not in ns.rec.messages


def test_unpair_other_error_shows_failure_text(qtbot, ctl):
    from teleport.trust.store import TrustStoreError

    ns = ctl()
    ns.node.unpair_error = TrustStoreError("không ghi được")
    ns.c.request_unpair(PEER_ID, "May-B")
    qtbot.wait(20)
    button_with_text(ns.c.unpair_box, UNPAIR_YES).click()
    qtbot.wait(20)
    ns.host.run_submit(ns.host.submits[0])
    qtbot.wait(20)
    assert ns.rec.messages == [fill(UNPAIR_FAILED, name="May-B")]


def test_unpair_direct_runs_one_submit(qtbot, ctl):
    ns = ctl()
    ns.c.unpair(PEER_ID)
    qtbot.wait(20)
    assert len(ns.host.submits) == 1
    ns.host.run_submit(ns.host.submits[0])
    qtbot.wait(20)
    assert ns.node.log == [("unpair", PEER_ID), ("devices",)]
    assert len(ns.rec.devices) == 1


UNPAIR_CLEAR_HISTORY = "Xóa cả lịch sử truyền file với máy này"  # spec D2 1.9


def test_unpair_box_has_clear_history_checkbox_checked(qtbot, ctl):
    """Spec D2 1.7, 1.9: hộp hủy ghép đôi có ô “Xóa cả lịch sử…” (QMessageBox.setCheckBox), bật sẵn."""
    ns = ctl()
    ns.c.request_unpair(PEER_ID, "May-B")
    qtbot.wait(20)
    box = ns.c.unpair_box
    check = box.checkBox()
    assert check is not None, "hộp hủy ghép đôi phải có ô xóa lịch sử"
    assert check.isChecked()
    assert check.text().replace("&", "") == UNPAIR_CLEAR_HISTORY
    assert check.isVisibleTo(box)


@pytest.mark.parametrize("clear", [True, False])
def test_unpair_passes_clear_history_flag(qtbot, ctl, clear):
    ns = ctl()
    cleared: list = []
    ns.c.history_cleared.connect(cleared.append)
    ns.node.views = (make_view(paired=False),)
    ns.c.request_unpair(PEER_ID, "May-B")
    qtbot.wait(20)
    box = ns.c.unpair_box
    box.checkBox().setChecked(clear)
    button_with_text(box, UNPAIR_YES).click()
    qtbot.wait(20)
    [pending] = ns.host.submits
    ns.host.run_submit(pending)
    qtbot.wait(20)
    assert ns.node.unpair_clear_history == [clear]
    assert cleared == ([PEER_ID] if clear else []), "history_cleared(device_id) chỉ khi có xóa (9.9)"


def test_unpair_failure_does_not_emit_history_cleared(qtbot, ctl):
    from teleport.trust.store import TrustStoreError

    ns = ctl()
    cleared: list = []
    ns.c.history_cleared.connect(cleared.append)
    ns.node.unpair_error = TrustStoreError("không ghi được")
    ns.c.unpair(PEER_ID, clear_history=True)
    qtbot.wait(20)
    ns.host.run_submit(ns.host.submits[0])
    qtbot.wait(20)
    assert cleared == []


def test_unpair_direct_defaults_to_keep_history(qtbot, ctl):
    ns = ctl()
    ns.c.unpair(PEER_ID)
    qtbot.wait(20)
    ns.host.run_submit(ns.host.submits[0])
    assert ns.node.unpair_clear_history == [False], "`unpair(device_id)` mặc định clear_history=False (1.7)"


def test_unpair_box_default_button_still_no(qtbot, ctl):
    ns = ctl()
    ns.c.request_unpair(PEER_ID, "May-B")
    qtbot.wait(20)
    box = ns.c.unpair_box
    no = button_with_text(box, UNPAIR_NO)
    assert box.defaultButton() is no
    assert box.escapeButton() is no
    assert not ns.host.submits and not ns.host.calls, "chưa bấm gì thì không hủy ghép đôi"


# =================================================================================================
# (b) set_host / close_all
# =================================================================================================


def _open_everything(qtbot, ns):
    ns.node.views = (make_view(),)
    dlg = open_prompt(qtbot, ns, make_prompt(sid=61))
    ns.c.pair_address("10.0.0.1", 47474)
    ns.c.request_unpair(OTHER_ID, "May-C")
    qtbot.wait(20)
    boxes = [ns.c.progress_box, ns.c.unpair_box]
    assert all(b is not None and b.isVisible() for b in boxes)
    return dlg, boxes


def test_close_all_closes_everything_without_decide(qtbot, ctl):
    ns = ctl()
    dlg, boxes = _open_everything(qtbot, ns)
    ns.c.close_all()
    qtbot.wait(30)
    assert closed(dlg)
    assert all(closed(b) for b in boxes)
    assert ns.c.dialogs() == []
    assert ns.host.decisions() == []
    assert not ns.host.submits[1:], "đóng hộp xác nhận không được hủy ghép đôi"


@pytest.mark.parametrize("new_host", [True, False], ids=["host-moi", "None"])
def test_set_host_closes_everything_and_ignores_old_host(qtbot, ctl, new_host):
    ns = ctl()
    dlg, boxes = _open_everything(qtbot, ns)
    old = ns.host
    pending = old.submits[0]
    if new_host:
        replacement = FakeHost(FakeNode())
        ns.c.set_host(replacement)
    else:
        ns.c.set_host(None)
    qtbot.wait(30)
    assert closed(dlg) and all(closed(b) for b in boxes)
    assert ns.c.dialogs() == []
    assert not ns.c.busy
    messages_before = list(ns.rec.messages)
    # Sự kiện, on_done của host cũ tới sau: bị bỏ.
    old.pairing_prompt.emit(make_prompt(sid=62))
    old.pairing_finished.emit(make_finished("timeout", sid=63, code_shown=True))
    old.deliver(pending, make_finished("unreachable", address="10.0.0.1", port=47474))
    qtbot.wait(50)
    assert ns.c.dialog(62) is None and ns.c.dialogs() == []
    assert ns.rec.finished == []
    assert ns.rec.messages == messages_before
    assert old.decisions() == []
    if new_host:
        assert ns.c.pair_address("10.0.0.1", 47474) is True
        assert len(replacement.submits) == 1


# =================================================================================================
# (c) Integration: NodeHost GUI thật + Node B thật trên 127.0.0.1
# =================================================================================================


class PairPeer:
    """Máy B: `Node` thật nhận ghép đôi, tự quyết mỗi lời hỏi bằng `answer` (None: không quyết)."""

    def __init__(self, config: Path, answer: bool | None = True, name: str = "May-B") -> None:
        self.config = config
        self.answer = answer
        self.name = name
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="test-pair-peer", daemon=True)
        self.node = None
        self.prompts: list = []
        self.results: list = []
        self.events: list = []
        self._lock = threading.Lock()

    def _prompt(self, prompt) -> None:
        with self._lock:
            self.prompts.append(prompt)
        if self.answer is not None:
            self.loop.call_soon(self.node.pairing.decide, prompt.session_id, self.answer)

    def _finished(self, result) -> None:
        with self._lock:
            self.results.append(result)

    def _event(self, event) -> None:
        with self._lock:
            self.events.append(event)

    def start(self) -> PairPeer:
        from teleport.app.node import Node

        self.thread.start()

        async def go():
            self.node = Node(
                self.config,
                name=self.name,
                host=LOCAL,
                port=0,
                accept_pairing=True,
                on_pairing_prompt=self._prompt,
                on_pairing_finished=self._finished,
                on_event=self._event,
            )
            await self.node.start()

        self.run(go(), 20)
        return self

    def run(self, coro, timeout: float = 10.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, fn, timeout: float = 5.0):
        async def go():
            return fn(self.node)

        return self.run(go(), timeout)

    @property
    def device_id(self) -> str:
        return self.node.identity.device_id

    @property
    def port(self) -> int:
        return self.call(lambda n: n.port)

    def snapshot(self, what: str) -> list:
        with self._lock:
            return list(getattr(self, what))

    def close(self) -> None:
        if self.node is not None:
            with contextlib.suppress(Exception):
                self.run(asyncio.wait_for(self.node.close(), 30), 35)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        if not self.thread.is_alive():
            self.loop.close()


@pytest.fixture
def pair_peer():
    made: list[PairPeer] = []

    def make(config: Path, **kw) -> PairPeer:
        peer = PairPeer(config, **kw)
        made.append(peer)
        return peer.start()

    yield make
    for peer in reversed(made):
        peer.close()


def host_call(qtbot, host, fn, timeout_ms: int = 5_000):
    box: dict = {}
    host.call(fn, lambda r: box.setdefault("r", r))
    qtbot.waitUntil(lambda: "r" in box, timeout=timeout_ms)
    if isinstance(box["r"], BaseException):
        raise box["r"]
    return box["r"]


@pytest.fixture
def gui_a(qtbot, config_dir, node_options, node_hosts):
    """NodeHost GUI A (đã chạy) + PairingController nối vào nó; ghi node_event."""
    from teleport.ui.pairing import PairingController

    host = node_hosts(node_options(config_dir("A")))
    c = PairingController()
    c.set_host(host)
    rec = Recorder(c)
    events: list = []
    host.node_event.connect(events.append)
    host.start()
    qtbot.waitUntil(lambda: host.running, timeout=15_000)
    ns = SimpleNamespace(
        host=host,
        c=c,
        rec=rec,
        events=events,
        port=host_call(qtbot, host, lambda n: n.port),
        device_id=host_call(qtbot, host, lambda n: n.identity.device_id),
    )
    yield ns
    c.close_all()
    c.set_host(None)


def wait_dialog(qtbot, c, timeout_ms: int = 10_000):
    qtbot.waitUntil(lambda: bool(c.dialogs()), timeout=timeout_ms)
    dlg = c.dialogs()[0]
    qtbot.waitUntil(dlg.isVisible, timeout=2_000)
    return dlg


def outcomes(rec) -> list[str]:
    return [r.outcome.value for r in rec.finished]


@integration
def test_gui_pairs_with_node_by_ip_then_connects(qtbot, gui_a, config_dir, pair_peer):
    """Xong khi (4), (5): GUI A → Node B qua 127.0.0.1:port: cả hai PAIRED, radar A “đã ghép”,
    rồi `DeviceConnected` (connect_hint)."""
    from teleport.app.node import DeviceConnected

    b = pair_peer(config_dir("B", identity=True), answer=True)
    assert gui_a.c.pair_address(LOCAL, b.port) is True
    dlg = wait_dialog(qtbot, gui_a.c)
    qtbot.waitUntil(lambda: bool(b.snapshot("prompts")), timeout=5_000)
    code_b = b.snapshot("prompts")[0].code
    assert dlg.findChild(QLabel, "pairingCode").text() == f"{code_b[:3]} {code_b[3:]}", "hai máy cùng mã"
    find_button(dlg, "pairingAccept").click()
    qtbot.waitUntil(lambda: "paired" in outcomes(gui_a.rec), timeout=10_000)
    qtbot.waitUntil(lambda: any(r.outcome.value == "paired" for r in b.snapshot("results")), timeout=10_000)
    qtbot.waitUntil(lambda: bool(gui_a.rec.devices), timeout=5_000)
    views = gui_a.rec.devices[-1]
    assert any(v.device_id == b.device_id and v.paired for v in views)
    assert gui_a.rec.paired == [b.device_id]
    qtbot.waitUntil(
        lambda: any(
            isinstance(e, DeviceConnected) and e.device.device_id == b.device_id for e in gui_a.events
        ),
        timeout=10_000,
    )
    assert not gui_a.c.busy


@integration
def test_node_pairs_with_gui_accept(qtbot, gui_a, config_dir, pair_peer):
    """Xong khi (4): B → GUI A: hộp mở (vai trả lời), “Trùng khớp” → PAIRED cả hai."""
    from teleport.ui.pairing import prompt_header

    b = pair_peer(config_dir("B"), answer=True)
    fut = b.submit(b.node.pairing.pair_with([LOCAL], gui_a.port))
    dlg = wait_dialog(qtbot, gui_a.c)
    assert any(lb.text() == prompt_header(_role("responder")) for lb in dlg.findChildren(QLabel))
    find_button(dlg, "pairingAccept").click()
    qtbot.waitUntil(lambda: "paired" in outcomes(gui_a.rec), timeout=10_000)
    assert fut.result(10).outcome.value == "paired"
    qtbot.waitUntil(lambda: bool(gui_a.rec.devices), timeout=5_000)
    assert any(v.device_id == b.device_id and v.paired for v in gui_a.rec.devices[-1])
    qtbot.waitUntil(lambda: closed(dlg), timeout=5_000)


@integration
def test_node_pairs_with_gui_reject(qtbot, gui_a, config_dir, pair_peer):
    """Xong khi (4): B → GUI A, A bấm “Không trùng” → A USER_REJECTED, B PEER_REJECTED."""
    b = pair_peer(config_dir("B"), answer=True)
    fut = b.submit(b.node.pairing.pair_with([LOCAL], gui_a.port))
    dlg = wait_dialog(qtbot, gui_a.c)
    find_button(dlg, "pairingReject").click()
    qtbot.waitUntil(lambda: bool(gui_a.rec.finished), timeout=10_000)
    assert outcomes(gui_a.rec) == ["user_rejected"]
    assert fut.result(10).outcome.value == "peer_rejected"
    trusted = host_call(qtbot, gui_a.host, lambda n: [d.device_id for d in n.store.devices()])
    assert b.device_id not in trusted
    assert gui_a.rec.messages == [fill(OUT_NOT_DONE, x="May-B")]


@integration
def test_unpair_from_gui_blocks_peer_reconnect(
    qtbot, config_dir, node_options, node_hosts, pair_peer, trust_each_other
):
    """Xong khi (6) sửa theo 9.5: A hủy ghép B → radar A không còn B “đã ghép”; B nối lại → nhận
    `ConnectFailed(REJECTED)`.

    B nối lại bằng `connect_hint` (không có mDNS ở test nên B không tự biết địa chỉ A sau khi rớt).
    Phần “A không có OfferReceived” không kiểm ở đây: thuộc PR3 (gửi/nhận trên GUI).
    """
    from teleport.app.node import ConnectFailed
    from teleport.core.links import ConnectResult
    from teleport.ui.pairing import PairingController

    da, db = config_dir("A"), config_dir("B")
    trust_each_other(da, db)
    host = node_hosts(node_options(da))
    c = PairingController()
    c.set_host(host)
    rec = Recorder(c)
    host.start()
    qtbot.waitUntil(lambda: host.running, timeout=15_000)
    try:
        a_id = host_call(qtbot, host, lambda n: n.identity.device_id)
        a_port = host_call(qtbot, host, lambda n: n.port)
        b = pair_peer(db, answer=True)
        b.call(lambda n: n.connect_hint(a_id, [LOCAL], a_port))
        qtbot.waitUntil(lambda: a_id in b.call(lambda n: n.connected_devices()), timeout=10_000)

        c.unpair(b.device_id)
        qtbot.waitUntil(lambda: bool(rec.devices), timeout=10_000)
        assert not any(v.device_id == b.device_id and v.paired for v in rec.devices[-1])

        b.call(lambda n: n.connect_hint(a_id, [LOCAL], a_port))
        qtbot.waitUntil(
            lambda: any(
                isinstance(e, ConnectFailed) and e.result is ConnectResult.REJECTED
                for e in b.snapshot("events")
            ),
            timeout=10_000,
        )
        assert b.device_id not in host_call(qtbot, host, lambda n: n.connected_devices())
    finally:
        c.close_all()
        c.set_host(None)


def _role(value: str):
    from teleport.core.pairing import Role

    return Role(value)


def test_fake_host_helpers_are_consistent():
    """Kiểm chính host giả: `run_calls` chạy cả call sinh ra trong lúc chạy, đúng thứ tự."""
    node = FakeNode()
    host = FakeHost(node)
    seen: list = []
    host.call(lambda n: n.devices(), lambda r: host.call(lambda n: "x", seen.append))
    host.run_calls()
    assert seen == ["x"]
    assert node.log == [("devices",)]


# =================================================================================================
# Sửa sau soat-code vòng 1
# =================================================================================================


def test_events_after_close_all_are_ignored(qtbot, ctl):
    """Thoát: lời hỏi / kết quả tới muộn (đã nằm trong hàng đợi Qt) không mở hộp nào."""
    ns = ctl(window_visible=lambda: False)
    ns.c.close_all()
    ns.host.pairing_prompt.emit(make_prompt(sid=3))
    ns.host.pairing_finished.emit(make_finished("user_rejected", sid=4, code_shown=True))
    qtbot.wait(20)
    assert ns.c.dialogs() == []
    assert ns.c.result_box is None
    assert ns.notes == []
    assert ns.rec.messages == []
    assert ns.c.pair_address("10.0.0.1", 5555) is False


def test_yielded_job_stopping_before_code_still_reports(qtbot, ctl):
    """Job bị nhường cho phiên trả lời rồi dừng trước khi hiện mã: người dùng vẫn thấy câu."""
    ns = ctl()
    ns.c.pair_address("10.0.0.1", 5555)
    p = ns.host.submits[0]
    # Phiên thay thế có thể tới từ một IP khác IP đã nhập (máy kia nhiều card mạng).
    r = make_finished(
        "connection_lost", sid=9, role="responder", address="10.0.0.77", port=None, code_shown=False
    )
    ns.host.pairing_finished.emit(r)
    qtbot.wait(20)
    assert ns.rec.messages == []
    ns.host.deliver(p, r)  # `pair_with` trả kết quả của phiên thay thế
    qtbot.wait(20)
    assert ns.rec.messages == [fill(OUT_EARLY, x="MacBook")]
    assert ns.c.progress_box is None


def test_spoofed_early_responder_result_does_not_touch_running_job(qtbot, ctl):
    """Máy lạ tự khai device_id của thiết bị đang ghép rồi bỏ phiên: job và hộp tiến độ giữ nguyên."""
    ns = ctl()
    ns.node.views = (make_view(),)
    assert ns.c.pair_device(make_view()) is True
    ns.host.pairing_finished.emit(
        make_finished("connection_lost", sid=9, role="responder", address="10.9.9.9", port=None)
    )
    qtbot.wait(20)
    assert ns.rec.messages == []
    assert ns.c.progress_box is not None
    assert ns.c.busy


def test_stranger_responder_stopping_before_code_stays_silent(qtbot, ctl):
    ns = ctl()
    ns.host.pairing_finished.emit(
        make_finished("connection_lost", sid=9, role="responder", address="10.0.0.9", port=None)
    )
    qtbot.wait(20)
    assert ns.rec.messages == []
    assert ns.c.result_box is None


def test_paired_replacement_from_other_address_hints_from_pair_with_result(qtbot, ctl):
    """9.2: phiên thay thế đến từ IP khác IP đã nhập → connect_hint theo kết quả của `pair_with`."""
    ns = ctl()
    ns.c.pair_address("10.0.0.1", 5555)
    p = ns.host.submits[0]
    r = make_finished("paired", sid=8, role="responder", address="10.0.0.77", port=None, name="May-B")
    ns.host.pairing_finished.emit(r)
    ns.host.deliver(p, r)
    qtbot.wait(20)
    ns.host.run_calls()
    hints = [e for e in ns.node.log if e[0] == "connect_hint"]
    assert hints == [("connect_hint", PEER_ID, ["10.0.0.1"], 5555)]


def test_paired_matched_by_address_hints_only_once(qtbot, ctl):
    ns = ctl()
    ns.c.pair_address("10.0.0.1", 5555)
    p = ns.host.submits[0]
    r = make_finished("paired", sid=1, address="10.0.0.1", port=5555, name="May-B")
    ns.host.pairing_finished.emit(r)
    ns.host.deliver(p, r)
    qtbot.wait(20)
    ns.host.run_calls()
    assert [e[0] for e in ns.node.log].count("connect_hint") == 1


def test_result_box_does_not_steal_focus_again_while_open(qtbot, ctl, monkeypatch):
    from PySide6.QtWidgets import QWidget as _QWidget

    ns = ctl()
    ns.host.pairing_finished.emit(make_finished("self", sid=None))
    qtbot.wait(20)
    box = ns.c.result_box
    assert box is not None and box.isVisible()
    raised: list = []
    monkeypatch.setattr(_QWidget, "activateWindow", lambda self: raised.append(self))
    for _ in range(5):
        ns.host.pairing_finished.emit(make_finished("identity_conflict", sid=None, role="responder"))
    qtbot.wait(20)
    assert ns.c.result_box is box
    assert raised == []
