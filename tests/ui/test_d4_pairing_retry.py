"""Nút “Thử lại” của hộp kết quả ghép đôi (spec D4 mục 5.1, mục 12 B1–B3; mục 14.4 và 14.9 thắng).

Viết từ spec, không từ code. `PairingController` với host giả (như `test_pairing.py`: QObject có
`pairing_prompt` / `pairing_finished`, ghi lại `call` / `submit`; test tự chạy coroutine trên node giả
và tự trả `on_done`). Thứ tự như `NodeHost` thật: `pairing_finished` tới trước `on_done` của `submit`.

Luật được kiểm:

- Có nút khi outcome thuộc tập thử lại được (tám outcome “chưa xong”, PEER_BUSY, REJECTED, UNREACHABLE,
  PROMPT_FAILED) **và** việc ghép là của máy này: (a) câu hiện từ `on_done` của job (cả nhánh phiên thay
  thế), (b) `pairing_finished` khớp job đang chạy, (c) `pairing_finished` ở vai INITIATOR. Vai trả lời
  không qua (a)/(b): không nút. ValueError (tham số sai): không nút (14.9). “Không thấy … nữa”: không nút.
- Nút: [Thử lại] (`pairingRetryButton`, AcceptRole, `theme.PRIMARY_PROPERTY`, `texts.retry()`) và [Đóng]
  (RejectRole, Esc). Không retry → giữ OK. Mặc định “Thử lại”, riêng đã hiện mã → “Đóng”.
- Bấm Thử lại: thử lại chạy **sau khi** hộp đóng (`result_box` đã là None), dùng host hiện tại; có ip/port
  → `pair_address`; còn lại → `device_lookup(id)` → `pair_device`; None hay không ghép được (không vì
  bận) → `device_gone_text`; bận → câu bận.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import sys

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QAbstractButton, QDialog, QMessageBox

PEER_ID = "3f2a9c1e" + "ab" * 12
OTHER_ID = "77aa55cc" + "cd" * 12

RETRY_NAME = "pairingRetryButton"
RETRY_VI = "Thử lại"
CLOSE_VI = "Đóng"
CLOSE_EN = "Close"
OUT_BUSY = "Máy này đang có một phiên ghép đôi khác. Hãy thử lại sau."
GONE = "Không thấy “{name}” trong mạng nữa. Hãy thử lại."

RETRYABLE = (
    "user_rejected",
    "timeout",
    "peer_rejected",
    "peer_timeout",
    "protocol_error",
    "peer_invalid",
    "peer_aborted",
    "connection_lost",
    "peer_busy",
    "rejected",
    "unreachable",
    "prompt_failed",
)
NOT_RETRYABLE = (
    "identity_mismatch",
    "identity_conflict",
    "version_mismatch",
    "self",
    "busy",
    "save_failed",
)


# =================================================================================================
# Dữ liệu giả
# =================================================================================================


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
    address: str = "10.0.0.1",
    port: int | None = 5555,
    name: str | None = "MacBook",
    device_id: str | None = PEER_ID,
    code_shown: bool = False,
):
    from teleport.app.pairing import PairingFinished, PairingOutcome
    from teleport.core.pairing import Role

    outcome = PairingOutcome(outcome)
    device = make_device(name or "May-B", device_id or PEER_ID) if outcome is PairingOutcome.PAIRED else None
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
        known_name=None,
        code_shown=code_shown,
    )


def make_view(
    *,
    name: str = "May-B",
    device_id: str = PEER_ID,
    online: bool = True,
    addresses: tuple[str, ...] = ("10.0.0.1",),
    port: int | None = 47474,
):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id,
        name=name,
        os="windows",
        paired=False,
        online=online,
        connected=False,
        addresses=addresses if online else (),
        port=port if online else None,
    )


class FakeJob:
    def __init__(self) -> None:
        self.cancel_calls = 0
        self.done = False

    def cancel(self) -> None:
        self.cancel_calls += 1


class FakePairing:
    def __init__(self) -> None:
        self.pair_calls: list[tuple[list[str], int]] = []

    async def pair_with(self, addresses, port):
        self.pair_calls.append((list(addresses), port))
        return None


class FakeNode:
    def __init__(self) -> None:
        self.views: tuple = ()
        self.pairing = FakePairing()

    def devices(self):
        return self.views

    def connect_hint(self, device_id, addresses, port) -> None:
        pass


class Pending:
    def __init__(self, fn, on_done, job) -> None:
        self.fn = fn
        self.on_done = on_done
        self.job = job


class FakeHost(QObject):
    pairing_prompt = Signal(object)
    pairing_finished = Signal(object)

    def __init__(self, node: FakeNode, on_submit=None) -> None:
        super().__init__()
        self.node = node
        self.calls: list = []
        self.submits: list[Pending] = []
        self.on_submit = on_submit
        self.running = True  # như `NodeHost.running`: node đã chạy

    def call(self, fn, on_done=None) -> None:
        self.calls.append((fn, on_done))

    def submit(self, fn, on_done=None):
        if self.on_submit is not None:
            self.on_submit()
        job = FakeJob()
        self.submits.append(Pending(fn, on_done, job))
        return job

    def run(self, p: Pending):
        """Chạy coroutine của `p` trên loop riêng (không trả `on_done`)."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(p.fn(self.node))
        finally:
            loop.close()

    def deliver(self, p: Pending, result) -> None:
        p.job.done = True
        if p.on_done is not None:
            p.on_done(result)


def retry_button(box) -> QAbstractButton | None:
    found = [b for b in box.findChildren(QAbstractButton, RETRY_NAME) if not b.isHidden()]
    return found[0] if found else None


def text_button(box, text: str) -> QAbstractButton:
    found = [b for b in box.buttons() if b.text().replace("&", "") == text and not b.isHidden()]
    assert len(found) == 1, [b.text() for b in box.buttons()]
    return found[0]


def closed(widget) -> bool:
    import shiboken6

    return widget is None or not shiboken6.isValid(widget) or not widget.isVisible()


@pytest.fixture(autouse=True)
def no_exec(monkeypatch):
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
    """`ctl(device_lookup=None) -> ns(c, host, node, messages, lookups, at_submit)`."""
    from teleport.ui.pairing import PairingController

    made: list = []

    def make(device_lookup=None):
        node = FakeNode()
        lookups: list[str] = []
        at_submit: list = []
        ns_holder: dict = {}

        def on_submit():
            c = ns_holder.get("c")
            if c is not None:
                at_submit.append(c.result_box)

        host = FakeHost(node, on_submit)
        kwargs = {}
        if device_lookup is not None:

            def lookup(device_id):
                lookups.append(device_id)
                return device_lookup(device_id)

            kwargs["device_lookup"] = lookup
        c = PairingController(notify=lambda *_: None, **kwargs)
        ns_holder["c"] = c
        c.set_host(host)
        messages: list[str] = []
        c.message.connect(messages.append)
        from types import SimpleNamespace

        ns = SimpleNamespace(
            c=c, host=host, node=node, messages=messages, lookups=lookups, at_submit=at_submit
        )
        made.append(ns)
        return ns

    yield make
    for ns in made:
        with contextlib.suppress(Exception):
            ns.c.close_all()
    qtbot.wait(10)


def fail_by_ip(qtbot, ns, outcome: str, *, code_shown: bool = False, role: str = "initiator"):
    """Ghép bằng IP 10.0.0.1:5555 rồi kết thúc bằng `outcome` (sự kiện trước, `on_done` sau)."""
    assert ns.c.pair_address("10.0.0.1", 5555) is True
    p = ns.host.submits[-1]
    port = 5555 if role == "initiator" else None
    r = make_finished(outcome, role=role, address="10.0.0.1", port=port, code_shown=code_shown)
    ns.host.pairing_finished.emit(r)
    ns.host.deliver(p, r)
    qtbot.wait(20)
    return r


def fail_by_device(qtbot, ns, outcome: str, view, *, code_shown: bool = False):
    ns.node.views = (view,)
    assert ns.c.pair_device(view) is True
    p = ns.host.submits[-1]
    r = make_finished(
        outcome,
        address=view.addresses[0],
        port=view.port,
        name=view.name,
        device_id=view.device_id,
        code_shown=code_shown,
    )
    ns.host.pairing_finished.emit(r)
    ns.host.deliver(p, r)
    qtbot.wait(20)
    return r


def click_retry(qtbot, ns) -> None:
    box = ns.c.result_box
    assert box is not None and box.isVisible()
    button = retry_button(box)
    assert button is not None, "thiếu nút Thử lại"
    button.click()
    qtbot.wait(30)


# =================================================================================================
# B1: ai có nút
# =================================================================================================


@pytest.mark.parametrize("outcome", RETRYABLE)
def test_retryable_outcome_by_ip_has_retry_and_retries_same_address(qtbot, ctl, outcome):
    ns = ctl()
    fail_by_ip(qtbot, ns, outcome)
    box = ns.c.result_box
    assert box is not None and box.isVisible()
    assert retry_button(box) is not None
    assert ns.c.result_retry_available is True
    click_retry(qtbot, ns)
    assert len(ns.host.submits) == 2, "bấm Thử lại phải bắt đầu lại đúng một lần ghép"
    ns.host.run(ns.host.submits[-1])
    assert ns.node.pairing.pair_calls == [(["10.0.0.1"], 5555)]


@pytest.mark.parametrize("outcome", RETRYABLE)
def test_retryable_outcome_by_device_retries_through_device_lookup(qtbot, ctl, outcome):
    view = make_view(name="May-B")
    ns = ctl(device_lookup=lambda device_id: view if device_id == PEER_ID else None)
    fail_by_device(qtbot, ns, outcome, view)
    assert retry_button(ns.c.result_box) is not None
    click_retry(qtbot, ns)
    assert ns.lookups == [PEER_ID]
    assert len(ns.host.submits) == 2
    ns.host.run(ns.host.submits[-1])
    assert ns.node.pairing.pair_calls == [(["10.0.0.1"], 47474)], "pair_device đọc lại địa chỉ mới"


@pytest.mark.parametrize("outcome", NOT_RETRYABLE)
def test_not_retryable_outcome_keeps_ok_only(qtbot, ctl, outcome):
    ns = ctl()
    fail_by_ip(qtbot, ns, outcome)
    box = ns.c.result_box
    assert box is not None and box.isVisible(), "outcome lỗi vẫn có hộp kết quả"
    assert retry_button(box) is None
    assert ns.c.result_retry_available is False
    oks = [b for b in box.buttons() if box.standardButton(b) == QMessageBox.StandardButton.Ok]
    assert len(oks) == 1, "không thử lại được: giữ nút OK"


def test_cancelled_shows_no_box(qtbot, ctl):
    ns = ctl()
    ns.c.pair_address("10.0.0.1", 5555)
    p = ns.host.submits[-1]
    ns.c.cancel_pairing()
    r = make_finished("cancelled")
    ns.host.pairing_finished.emit(r)
    ns.host.deliver(p, asyncio.CancelledError())
    qtbot.wait(20)
    assert closed(ns.c.result_box)


@pytest.mark.parametrize("outcome", ["timeout", "peer_busy", "connection_lost"])
def test_responder_result_not_from_own_job_has_no_retry(qtbot, ctl, outcome):
    """Máy lạ ghép tới máy này (vai trả lời, không có job): không có nút Thử lại."""
    ns = ctl()
    r = make_finished(outcome, role="responder", address="10.9.9.9", port=None, code_shown=True)
    ns.host.pairing_finished.emit(r)
    qtbot.wait(20)
    box = ns.c.result_box
    if box is None or not box.isVisible():
        return  # outcome không báo gì ở vai trả lời
    assert retry_button(box) is None
    assert ns.c.result_retry_available is False


def test_initiator_result_without_job_retries_result_address(qtbot, ctl):
    """14.4 (c): kết quả vai INITIATOR mà không có job đang chạy → nút; đích là `result.address/port`."""
    ns = ctl()
    ns.host.pairing_finished.emit(make_finished("unreachable", address="10.0.0.9", port=4000, name=None))
    qtbot.wait(20)
    assert retry_button(ns.c.result_box) is not None
    click_retry(qtbot, ns)
    assert len(ns.host.submits) == 1
    ns.host.run(ns.host.submits[-1])
    assert ns.node.pairing.pair_calls == [(["10.0.0.9"], 4000)]


def test_matched_responder_result_of_own_job_after_code_has_retry_default_close(qtbot, ctl):
    """14.4 (b): phiên thay thế ở vai trả lời khớp job (theo địa chỉ) và đã hiện mã → có nút, mặc định Đóng;
    thử lại về đúng `ip:port` của job."""
    ns = ctl()
    fail_by_ip(qtbot, ns, "user_rejected", role="responder", code_shown=True)
    box = ns.c.result_box
    retry = retry_button(box)
    assert retry is not None
    assert box.defaultButton() is not retry
    assert box.defaultButton() is text_button(box, CLOSE_VI)
    click_retry(qtbot, ns)
    ns.host.run(ns.host.submits[-1])
    assert ns.node.pairing.pair_calls == [(["10.0.0.1"], 5555)]


def test_yielded_job_stopping_before_code_reported_from_on_done_has_retry(qtbot, ctl):
    """14.4 (a): job bị nhường cho phiên trả lời từ địa chỉ khác rồi dừng trước khi hiện mã: câu báo từ
    `on_done` có nút; đích lấy từ job (`ip:port` đã nhập), không phải địa chỉ của phiên thay thế."""
    ns = ctl()
    ns.c.pair_address("10.0.0.1", 5555)
    p = ns.host.submits[-1]
    r = make_finished("connection_lost", sid=9, role="responder", address="10.0.0.77", port=None)
    ns.host.pairing_finished.emit(r)
    qtbot.wait(20)
    ns.host.deliver(p, r)
    qtbot.wait(20)
    assert ns.messages, "job bị nhường vẫn phải báo"
    assert retry_button(ns.c.result_box) is not None
    click_retry(qtbot, ns)
    ns.host.run(ns.host.submits[-1])
    assert ns.node.pairing.pair_calls == [(["10.0.0.1"], 5555)]


def test_value_error_unreachable_text_has_no_retry(qtbot, ctl):
    """14.9: ValueError (tham số sai) → câu không kết nối được nhưng KHÔNG có nút."""
    ns = ctl()
    ns.c.pair_address("10.0.0.1", 5555)
    ns.host.deliver(ns.host.submits[-1], ValueError("cổng sai"))
    qtbot.wait(20)
    box = ns.c.result_box
    assert box is not None and box.isVisible()
    assert retry_button(box) is None
    assert ns.c.result_retry_available is False


def test_device_gone_at_start_has_no_retry(qtbot, ctl):
    """“Không thấy … trong mạng nữa” (job trả None): không nút."""
    view = make_view(name="May-B")
    ns = ctl(device_lookup=lambda _id: view)
    ns.node.views = ()
    ns.c.pair_device(view)
    p = ns.host.submits[-1]
    ns.host.deliver(p, ns.host.run(p))
    qtbot.wait(20)
    assert ns.messages == [GONE.format(name="May-B")]
    assert retry_button(ns.c.result_box) is None


# =================================================================================================
# Hình thức nút (B2)
# =================================================================================================


def test_retry_and_close_buttons_roles_names_and_style(qtbot, ctl):
    from teleport.ui import texts, theme

    ns = ctl()
    fail_by_ip(qtbot, ns, "unreachable")
    box = ns.c.result_box
    retry = retry_button(box)
    close = text_button(box, CLOSE_VI)
    assert retry.text().replace("&", "") == texts.retry() == RETRY_VI
    assert box.buttonRole(retry) == QMessageBox.ButtonRole.AcceptRole
    assert box.buttonRole(close) == QMessageBox.ButtonRole.RejectRole
    assert bool(retry.property(theme.PRIMARY_PROPERTY)), "Thử lại là nút chính tô màu nhấn"
    assert box.escapeButton() is close
    assert box.defaultButton() is retry, "chưa hiện mã: mặc định Thử lại"
    visible = [b for b in box.buttons() if not b.isHidden()]
    assert sorted(b.text().replace("&", "") for b in visible) == sorted([RETRY_VI, CLOSE_VI])
    assert box.windowModality() == Qt.WindowModality.NonModal


@pytest.mark.parametrize("outcome", ["timeout", "user_rejected", "peer_rejected"])
def test_code_shown_defaults_to_close(qtbot, ctl, outcome):
    ns = ctl()
    fail_by_ip(qtbot, ns, outcome, code_shown=True)
    box = ns.c.result_box
    assert retry_button(box) is not None
    assert box.defaultButton() is text_button(box, CLOSE_VI)


def test_escape_closes_without_retry(qtbot, ctl):
    ns = ctl()
    fail_by_ip(qtbot, ns, "unreachable")
    box = ns.c.result_box
    qtbot.keyClick(box, Qt.Key.Key_Escape)
    qtbot.wait(30)
    assert closed(box)
    assert len(ns.host.submits) == 1, "Esc = Đóng: không thử lại"


def test_close_button_does_not_retry(qtbot, ctl):
    ns = ctl()
    fail_by_ip(qtbot, ns, "peer_busy")
    box = ns.c.result_box
    text_button(box, CLOSE_VI).click()
    qtbot.wait(30)
    assert closed(box)
    assert len(ns.host.submits) == 1


def test_close_button_is_translated(qtbot, ctl):
    from teleport.ui import i18n

    i18n.set_language("en")
    ns = ctl()
    fail_by_ip(qtbot, ns, "unreachable")
    box = ns.c.result_box
    names = [b.text().replace("&", "") for b in box.buttons() if not b.isHidden()]
    assert CLOSE_EN in names
    assert retry_button(box).text().replace("&", "") != RETRY_VI


# =================================================================================================
# B3: hộp dùng lại, máy không còn, bận; 14.9 thứ tự
# =================================================================================================


def test_reused_box_with_not_retryable_text_loses_retry_button(qtbot, ctl):
    ns = ctl()
    fail_by_ip(qtbot, ns, "unreachable")
    box = ns.c.result_box
    assert retry_button(box) is not None
    ns.host.pairing_finished.emit(make_finished("self", sid=None))
    qtbot.wait(20)
    assert ns.c.result_box is box, "hộp kết quả dùng lại"
    assert retry_button(box) is None
    assert ns.c.result_retry_available is False
    oks = [b for b in box.buttons() if box.standardButton(b) == QMessageBox.StandardButton.Ok]
    assert len(oks) == 1


def test_reused_box_with_retryable_text_gains_retry_button(qtbot, ctl):
    ns = ctl()
    ns.host.pairing_finished.emit(make_finished("self", sid=None))
    qtbot.wait(20)
    box = ns.c.result_box
    assert retry_button(box) is None
    fail_by_ip(qtbot, ns, "peer_busy")
    assert ns.c.result_box is box
    assert retry_button(box) is not None
    assert ns.c.result_retry_available is True


def test_retry_when_device_lookup_finds_nothing_says_device_gone(qtbot, ctl):
    view = make_view(name="May-B")
    found: dict = {"view": view}
    ns = ctl(device_lookup=lambda device_id: found["view"])
    fail_by_device(qtbot, ns, "peer_busy", view)
    found["view"] = None
    click_retry(qtbot, ns)
    assert len(ns.host.submits) == 1
    assert ns.messages[-1] == GONE.format(name="May-B")
    assert retry_button(ns.c.result_box) is None, "câu “không thấy … nữa” không có nút"


def test_retry_when_device_now_offline_says_device_gone(qtbot, ctl):
    view = make_view(name="May-B")
    found: dict = {"view": view}
    ns = ctl(device_lookup=lambda device_id: found["view"])
    fail_by_device(qtbot, ns, "timeout", view)
    found["view"] = make_view(name="May-B", online=False)
    click_retry(qtbot, ns)
    assert len(ns.host.submits) == 1
    assert ns.messages[-1] == GONE.format(name="May-B")


def test_default_device_lookup_finds_nothing(qtbot, ctl):
    """Tham số `device_lookup` mặc định trả None: ghép theo máy mà không truyền lookup → “không thấy”."""
    ns = ctl()
    view = make_view(name="May-B")
    fail_by_device(qtbot, ns, "timeout", view)
    click_retry(qtbot, ns)
    assert len(ns.host.submits) == 1
    assert ns.messages[-1] == GONE.format(name="May-B")


def test_retry_while_another_pairing_runs_says_busy(qtbot, ctl):
    ns = ctl()
    fail_by_ip(qtbot, ns, "unreachable")
    box = ns.c.result_box
    assert ns.c.pair_address("10.0.0.2", 6000) is True  # việc khác bắt đầu khi hộp còn mở
    button = retry_button(box)
    button.click()
    qtbot.wait(30)
    assert len(ns.host.submits) == 2, "đang bận: không bắt đầu thêm"
    assert ns.messages[-1] == OUT_BUSY


def test_retry_runs_after_box_closed_not_inside_click(qtbot, ctl):
    """14.9: lúc `submit` của lần thử lại chạy, hộp kết quả cũ đã đóng (`result_box` là None)."""
    ns = ctl()
    fail_by_ip(qtbot, ns, "unreachable")
    old = ns.c.result_box
    ns.at_submit.clear()
    click_retry(qtbot, ns)
    assert len(ns.at_submit) == 1
    assert ns.at_submit[0] is None, "thử lại chạy lồng trong slot của nút (hộp cũ còn là result_box)"
    assert closed(old)


def test_retry_without_host_does_nothing(qtbot, ctl):
    ns = ctl()
    fail_by_ip(qtbot, ns, "unreachable")
    box = ns.c.result_box
    ns.c.set_host(None)
    qtbot.wait(10)
    if closed(box):
        return  # đổi host đóng luôn hộp kết quả: không còn gì để bấm
    before = list(ns.messages)
    retry_button(box).click()
    qtbot.wait(30)
    assert len(ns.host.submits) == 1
    assert ns.messages == before


def test_retry_uses_current_host(qtbot, ctl):
    ns = ctl()
    fail_by_ip(qtbot, ns, "unreachable")
    box = ns.c.result_box
    node2 = FakeNode()
    host2 = FakeHost(node2)
    ns.c.set_host(host2)
    qtbot.wait(10)
    if closed(box):
        pytest.skip("đổi host đóng hộp kết quả: không còn nút để bấm (hành vi chấp nhận được)")
    retry_button(box).click()
    qtbot.wait(30)
    assert len(ns.host.submits) == 1, "host cũ không được dùng"
    assert len(host2.submits) == 1


def test_retry_pairing_still_compares_full_code(qtbot, ctl):
    """Lần thử mới vẫn là một lần ghép đầy đủ: mã hiện lại trong hộp mã khi node báo `pairing_prompt`."""
    import time as _time

    from teleport.app.pairing import PairingPrompt
    from teleport.core.pairing import Role

    ns = ctl()
    fail_by_ip(qtbot, ns, "timeout", code_shown=True)
    click_retry(qtbot, ns)
    prompt = PairingPrompt(
        session_id=11,
        role=Role("initiator"),
        address="10.0.0.1",
        peer_name="MacBook",
        peer_device_id=PEER_ID,
        code="905617",
        expires_at=_time.monotonic() + 60.0,
        recent_codes=1,
        known_name=None,
    )
    ns.host.pairing_prompt.emit(prompt)
    qtbot.waitUntil(lambda: ns.c.dialog(11) is not None, timeout=2000)


# =================================================================================================
# GuiApp truyền `device_lookup`
# =================================================================================================


def test_gui_app_passes_device_lookup_from_window_devices(qapp, qtbot, config_dir, monkeypatch):
    from teleport.ui import app as ui_app
    from teleport.ui import tray

    monkeypatch.setattr(tray, "is_available", lambda: False)
    captured: dict = {}
    real = ui_app.PairingController

    class Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ui_app, "PairingController", Spy)
    gapp = ui_app.GuiApp(qapp, config_dir("A", identity=False))
    try:
        lookup = captured.get("device_lookup")
        assert callable(lookup), "GuiApp phải truyền device_lookup"
        view = make_view(name="PC-Nha", device_id=OTHER_ID)
        gapp.window.devices.reset([view])
        got = lookup(OTHER_ID)
        assert got is not None and got.device_id == OTHER_ID
        assert lookup("f" * 32) is None
    finally:
        with contextlib.suppress(Exception):
            gapp.shutdown()
        gapp.window.deleteLater()
        qapp.processEvents()


def test_result_box_with_retry_keeps_title_and_plain_text(qtbot, ctl):
    """Tiêu đề hộp kết quả vẫn “Ghép đôi” khi có nút Thử lại (macOS bỏ tiêu đề QMessageBox)."""
    ns = ctl()
    fail_by_ip(qtbot, ns, "unreachable")
    box = ns.c.result_box
    if sys.platform != "darwin":
        assert box.windowTitle() == "Ghép đôi"
    assert box.textFormat() == Qt.TextFormat.PlainText


def test_retry_while_host_not_running_does_nothing(qtbot, ctl):
    """Hộp của host cũ, host mới còn khởi động: bấm “Thử lại” không bắt đầu ghép đôi."""
    ns = ctl()
    fail_by_ip(qtbot, ns, "unreachable")
    box = ns.c.result_box
    host2 = FakeHost(FakeNode())
    host2.running = False
    ns.c.set_host(host2)
    qtbot.wait(10)
    if closed(box):
        pytest.skip("đổi host đóng hộp kết quả: không còn nút để bấm")
    retry_button(box).click()
    qtbot.wait(30)
    assert len(ns.host.submits) == 1
    assert host2.submits == []
