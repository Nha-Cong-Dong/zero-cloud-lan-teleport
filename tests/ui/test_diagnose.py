"""`teleport.ui.diagnose`: thanh chẩn đoán kết nối và hộp “Kết nối bằng IP…” (spec F3b mục 2–3, 7.1;
mục 12 H1–H16 thắng khi khác).

Viết từ spec, không từ code (U1–U16 của mục 7.1). Không socket: `NodeHost` giả ghi lại mọi `call(fn,
on_done)` / `submit(fn, on_done)` (trả `SubmitJob` giả đếm `cancel()`); test tự chạy `fn` trên một node
giả ở lúc nó chọn để biết controller định gọi gì, rồi tự gọi `on_done` (như `NodeHost` làm trên thread
Qt). `ChatView`, `DeviceListModel`, `DiagnoseBar`, `ConnectAddressDialog` là widget thật (offscreen).

Luồng sự kiện như `ui.app`: `devices.apply(event)` rồi `on_node_event(event)`; host mới: `devices.clear()`
→ `attach` → `on_started` → `devices.reset(ảnh chụp)` (H2). Đồng hồ: seam `diagnose._monotonic` (giây,
như `time.monotonic`) vá bằng đồng hồ giả; “hết hạn” = tiến đồng hồ quá `SHOW_DELAY_MS` rồi phát một
signal của model (`refresh` → `dataChanged`), vì H2: mọi signal của model gọi `_reevaluate()`. Một test
dùng `QTimer` thật với `SHOW_DELAY_MS` nhỏ.

Quan sát công khai: thanh (`view.findChild(DiagnoseBar, "chatDiagnoseBar")`), `isVisible()`,
`title_label` / `text_label`, `level`, `action_button` ([Kết nối bằng IP…]) / `secondary_button` ([Thử
lại]); hộp tìm theo objectName (`connectAddressDialog`, `connectAddressEdit`, `connectAddressError`).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

PEER_ID = "a71c04d2" + "e" * 24
OTHER_ID = "0f0f0f0f" + "1" * 24
PEER = "PC-Nha"
OTHER = "Laptop-Minh"
LAST = ("192.0.2.9", 47474)  # cổng mặc định: format_address chỉ IP
LAST_ODD = ("192.0.2.9", 50123)  # cổng khác: ip:cổng
TYPED = "192.0.2.50:5000"

# Câu nguồn của spec mục 3 ({name}, {address} chèn bằng `texts.fill`).
SPEC_TEXTS = {
    "not_seen_title": "Không thấy {name} trong mạng",
    "not_seen_body": (
        "Hãy mở Teleport trên máy đó và kiểm tra hai máy dùng chung một mạng. Nếu mạng chặn việc tìm thiết "
        "bị, hãy kết nối bằng địa chỉ IP của máy đó."
    ),
    "reconnecting_body": (
        "Teleport vẫn đang tự thử kết nối lại tới địa chỉ lần trước ({address}). Nếu máy đó đã đổi mạng hay "
        "đổi địa chỉ, hãy kết nối bằng địa chỉ IP mới."
    ),
    "mdns_down_title": "Máy này chưa tìm được thiết bị nào trong mạng",
    "mdns_down_body": (
        "Tính năng tìm thiết bị của Teleport trên máy này đang không chạy (có thể mạng vừa đổi, hay VPN hoặc "
        "phần mềm bảo mật đang chặn). Teleport sẽ tự bật lại. Trong lúc chờ, hãy kết nối tới {name} bằng "
        "địa chỉ IP."
    ),
    "wrong_address_title": "Địa chỉ IP vừa nhập là của một máy khác, không phải {name}",
    "wrong_address_body": (
        "Hãy xem lại địa chỉ ở dòng “Địa chỉ” trong cửa sổ Teleport trên {name}, rồi kết nối lại."
    ),
    "connecting_title": "Đang kết nối tới {name}…",
    "still_failed_title": "Vẫn chưa kết nối được với {name}",
    "saved_changed_body": (
        "Địa chỉ lần trước ({address}) giờ là của một máy khác. Hãy kết nối bằng địa chỉ IP mới của {name}."
    ),
    "connect_by_ip": "Kết nối bằng IP…",
    "dialog_title": "Kết nối bằng IP",
    "dialog_label": "Địa chỉ IP của {name}",
    "dialog_help": (
        "Xem ở dòng “Địa chỉ” trong cửa sổ Teleport trên máy đó, ví dụ 192.168.1.5 hoặc 192.168.1.5:47474."
    ),
    "dialog_connect": "Kết nối",
    "dialog_cancel": "Hủy",
    "dialog_connecting": "Đang kết nối tới {address}…",
    "not_this_device_text": (
        "Máy ở {address} không phải {name}. Có thể đó là một máy khác đang chạy Teleport; hãy kiểm tra lại "
        "địa chỉ."
    ),
    "connect_error_text": "Không kết nối được vì một lỗi không mong đợi. Hãy thử lại.",
}
# Tham số của từng hàm chữ (theo thứ tự chữ ký trong spec mục 3).
SPEC_ARGS = {
    "not_seen_title": ("name",),
    "not_seen_body": (),
    "reconnecting_body": ("address",),
    "mdns_down_title": (),
    "mdns_down_body": ("name",),
    "wrong_address_title": ("name",),
    "wrong_address_body": ("name",),
    "connecting_title": ("name",),
    "still_failed_title": ("name",),
    "saved_changed_body": ("address", "name"),
    "connect_by_ip": (),
    "dialog_title": (),
    "dialog_label": ("name",),
    "dialog_help": (),
    "dialog_connect": (),
    "dialog_cancel": (),
    "dialog_connecting": ("address",),
    "not_this_device_text": ("address", "name"),
    "connect_error_text": (),
}


# =================================================================================================
# Dữ liệu, node, host giả
# =================================================================================================


def dview(
    device_id: str = PEER_ID,
    name: str = PEER,
    *,
    paired: bool = True,
    online: bool = False,
    connected: bool = False,
    last=None,
    port: int | None = None,
):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id,
        name=name,
        os="windows",
        paired=paired,
        online=online,
        connected=connected,
        addresses=("192.0.2.7",) if online else (),
        port=(port if port is not None else 47474) if online else None,
        last_address=last,
    )


def label(name: str = PEER, device_id: str = PEER_ID) -> str:
    from teleport.ui import texts

    return texts.peer_label(name, device_id)


def drive(result):
    """`fn(node)` có thể trả coroutine (lambda gọi phương thức `async`, hay hàm `async` bọc): chạy hết."""
    if not inspect.isawaitable(result):
        return result

    async def wait():
        return await result

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(wait())
    finally:
        loop.close()


class FakeNode:
    """Node giả: `discovery_active`, ghi lại `retry_connect` / `connect_address` và luồng của mọi truy cập."""

    def __init__(self, discovery_active: bool | None = True) -> None:
        object.__setattr__(self, "threads", [])
        self.discovery_active = discovery_active
        self.retries: list[str] = []
        self.connects: list[tuple[str, str, int]] = []

    def __getattribute__(self, name: str):
        if not name.startswith("_") and name != "threads":
            object.__getattribute__(self, "threads").append(threading.get_ident())
        return object.__getattribute__(self, name)

    async def retry_connect(self, device_id: str):
        self.retries.append(device_id)

    async def connect_address(self, device_id: str, ip: str, port: int):
        self.connects.append((device_id, ip, port))


class FakeJob:
    def __init__(self) -> None:
        self.cancel_calls = 0
        self.done = False

    def cancel(self) -> None:
        self.cancel_calls += 1


class Pending:
    def __init__(self, fn, on_done, job=None) -> None:
        self.fn = fn
        self.on_done = on_done
        self.job = job
        self.answered = False


class FakeHost:
    """Thay `NodeHost`: ghi lại `call` / `submit`; không chạy gì cho tới khi test gọi."""

    def __init__(self) -> None:
        self.calls: list[Pending] = []
        self.submits: list[Pending] = []
        self.running = True
        self.is_stopped = False
        self.node_name = "May-A"
        self.options = SimpleNamespace(receive_dir=None, config_dir=None, mdns=None)

    def call(self, fn, on_done=None) -> None:
        self.calls.append(Pending(fn, on_done))

    def submit(self, fn, on_done=None):
        job = FakeJob()
        self.submits.append(Pending(fn, on_done, job))
        return job

    def pending_calls(self) -> list[Pending]:
        return [c for c in self.calls if not c.answered]

    def answer_calls(self, value, node_factory=None) -> int:
        """Trả lời mọi `call` đang chờ: `value` là `discovery_active` của node giả, hay một ngoại lệ."""
        pending = self.pending_calls()
        for c in pending:
            c.answered = True
            if isinstance(value, BaseException):
                result = value
            else:
                node = node_factory() if node_factory is not None else FakeNode(value)
                if node_factory is not None:
                    node.discovery_active = value
                try:
                    result = drive(c.fn(node))
                except Exception as e:  # như NodeHost: lỗi của fn về on_done
                    result = e
            if c.on_done is not None:
                c.on_done(result)
        return len(pending)

    def run_submit(self, index: int = -1) -> FakeNode:
        node = FakeNode()
        drive(self.submits[index].fn(node))
        return node

    def finish(self, result, index: int = -1) -> None:
        sub = self.submits[index]
        sub.job.done = True
        sub.answered = True
        if sub.on_done is not None:
            sub.on_done(result)


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


_SKIP = object()


# =================================================================================================
# Fixture và hàm phụ trợ
# =================================================================================================


@pytest.fixture
def env(qtbot, monkeypatch):
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    from teleport.ui import diagnose as mod
    from teleport.ui.chat import ChatView
    from teleport.ui.radar import DeviceListModel

    clock = Clock()
    monkeypatch.setattr(mod, "_monotonic", clock)
    window = QWidget()
    qtbot.addWidget(window)
    window.resize(900, 700)
    model = DeviceListModel(window)
    view = ChatView(model, window)
    QVBoxLayout(window).addWidget(view)
    ctl = mod.DiagnoseController(view, model, window)
    window.show()
    bar = view.findChild(mod.DiagnoseBar, "chatDiagnoseBar")
    ns = SimpleNamespace(
        mod=mod,
        clock=clock,
        window=window,
        model=model,
        view=view,
        ctl=ctl,
        bar=bar,
        host=None,
        qtbot=qtbot,
        monkeypatch=monkeypatch,
    )
    yield ns
    with contextlib.suppress(Exception):
        ctl.close_all()
    for d in dialogs(ns):
        with contextlib.suppress(Exception):
            d.close()


def start(env, *views, current: str | None = PEER_ID, discovery=True, host: FakeHost | None = None):
    """Host mới như `ui.app`: clear → attach → on_started → reset → chọn máy; trả lời lần đọc mDNS."""
    host = host if host is not None else FakeHost()
    env.host = host
    env.model.clear()
    env.ctl.attach(host)
    env.ctl.on_started(host)
    env.model.reset(views)
    select(env, current)
    if discovery is not _SKIP:
        host.answer_calls(discovery)
    return host


def select(env, device_id: str | None) -> None:
    """Như `conversations.current_changed`: `view.set_device` rồi `diagnose.set_device`."""
    env.view.set_device(device_id)
    env.ctl.set_device(device_id)


def emit(env, event) -> None:
    env.model.apply(event)
    env.ctl.on_node_event(event)


def elapse(env, seconds: float | None = None) -> None:
    """Tiến đồng hồ giả (mặc định quá `SHOW_DELAY_MS`) rồi phát một signal của model (H2)."""
    if seconds is None:
        seconds = env.mod.SHOW_DELAY_MS / 1000 + 0.01
    env.clock.now += seconds
    env.model.refresh()


def failed(view, result: str, *, from_hint: bool = False):
    from teleport.app.node import ConnectFailed
    from teleport.core.links import ConnectResult

    return ConnectFailed(device=view, result=ConnectResult(result), from_hint=from_hint)


def connected_event(view):
    from teleport.app.node import DeviceConnected

    return DeviceConnected(device=view, outgoing=True)


def offline_event(view):
    from teleport.app.node import DeviceOffline

    return DeviceOffline(device=view)


def disconnected_event(view):
    from teleport.app.node import DeviceDisconnected

    return DeviceDisconnected(device=view)


def shown(env) -> bool:
    return env.bar.isVisible()


def title(env) -> str:
    return env.bar.title_label.text()


def body(env) -> str:
    return env.bar.text_label.text() if env.bar.text_label.isVisibleTo(env.bar) else ""


def retry_visible(env) -> bool:
    return env.bar.secondary_button.isVisibleTo(env.bar)


def ip_visible(env) -> bool:
    return env.bar.action_button.isVisibleTo(env.bar)


def plain(text: str) -> str:
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


def dialogs(env) -> list:
    from PySide6.QtWidgets import QDialog

    try:
        import shiboken6

        alive = shiboken6.isValid
    except ImportError:  # pragma: no cover
        alive = lambda _o: True  # noqa: E731

    return [d for d in env.window.findChildren(QDialog, "connectAddressDialog") if alive(d) and d.isVisible()]


def the_dialog(env):
    found = dialogs(env)
    assert len(found) == 1, f"cần đúng một hộp “Kết nối bằng IP…” đang mở, thấy {len(found)}"
    return found[0]


def dialog_parts(d):
    from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton

    from teleport.ui import diagnose as mod

    edit = d.findChild(QLineEdit, "connectAddressEdit")
    error = d.findChild(QLabel, "connectAddressError")
    buttons = d.findChildren(QPushButton)
    connect = [b for b in buttons if plain(b.text()) == mod.dialog_connect()]
    cancel = [b for b in buttons if plain(b.text()) == mod.dialog_cancel()]
    assert edit is not None, "thiếu ô connectAddressEdit"
    assert error is not None, "thiếu nhãn connectAddressError"
    assert len(connect) == 1 and len(cancel) == 1, "hộp cần đúng một nút “Kết nối” và một nút “Hủy”"
    return SimpleNamespace(edit=edit, error=error, connect=connect[0], cancel=cancel[0])


def shown_labels(parts) -> list[str]:
    """Chữ mọi nhãn đang hiện của hộp (câu “Đang kết nối tới …” nằm ở một nhãn trạng thái của hộp)."""
    from PySide6.QtWidgets import QLabel

    d = parts.edit.window()
    return [w.text() for w in d.findChildren(QLabel) if w.isVisibleTo(d) and w.text()]


def open_dialog(env):
    env.bar.action_button.click()
    env.qtbot.wait(1)
    return the_dialog(env)


def reconnecting(env, *, last=LAST, discovery=True):
    """Máy PEER ngoại tuyến, có địa chỉ đã lưu, mDNS chạy, đã quá hạn → RECONNECTING đang hiện."""
    view = dview(last=last)
    start(env, view, discovery=discovery)
    elapse(env)
    assert shown(env), "RECONNECTING phải hiện sau SHOW_DELAY_MS"
    return view


def next_poll(env, value, timeout: int = 2000) -> None:
    """Chờ lần đọc mDNS kế tiếp (cần `DISCOVERY_POLL_MS` nhỏ, vá trước `start`) rồi trả `value`."""
    env.qtbot.waitUntil(lambda: len(env.host.pending_calls()) == 1, timeout=timeout)
    env.host.answer_calls(value)


def warnings_about(caplog, name: str) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith("teleport") and name in r.getMessage()
    ]


# =================================================================================================
# Hằng, hàm thuần, câu chữ
# =================================================================================================


def test_constants_and_enums_match_spec():
    from teleport.ui import diagnose as mod

    assert mod.SHOW_DELAY_MS == 4000
    assert mod.DISCOVERY_POLL_MS == 5000
    assert {c.name: c.value for c in mod.Cause} == {
        "NONE": "none",
        "BLOCKED": "blocked",
        "WRONG_ADDRESS": "wrong_address",
        "ISOLATED": "isolated",
        "MDNS_DOWN": "mdns_down",
        "RECONNECTING": "reconnecting",
        "NOT_SEEN": "not_seen",
    }
    # H14: overlay là enum riêng, không dùng None làm giá trị.
    assert {"NONE", "BUSY", "TRIED_UNREACHABLE", "TRIED_IDENTITY", "NO_TARGET"} <= {
        o.name for o in mod.Overlay
    }


@pytest.mark.parametrize(
    ("ip", "port", "expected"),
    [
        ("192.168.1.5", 47474, "192.168.1.5"),
        ("192.168.1.5", 47475, "192.168.1.5:47475"),
        ("127.0.0.1", 1, "127.0.0.1:1"),
        ("10.0.0.2", 65535, "10.0.0.2:65535"),
    ],
)
def test_format_address_omits_only_the_default_port(ip, port, expected):
    from teleport.net.connection import DEFAULT_PORT
    from teleport.ui import diagnose as mod

    assert DEFAULT_PORT == 47474
    assert mod.format_address(ip, port) == expected


@pytest.mark.parametrize("fn_name", sorted(SPEC_TEXTS))
def test_text_functions_return_spec_sentences(fn_name):
    from teleport.ui import diagnose as mod
    from teleport.ui import texts

    values = {"name": "PC-Nha", "address": "192.0.2.9:5000"}
    args = [values[a] for a in SPEC_ARGS[fn_name]]
    expected = texts.fill(SPEC_TEXTS[fn_name], **{a: values[a] for a in SPEC_ARGS[fn_name]})
    assert getattr(mod, fn_name)(*args) == expected


def test_pairing_unreachable_text_is_public():
    from teleport.ui import pairing

    text = pairing.unreachable_text("192.0.2.9:5000")
    assert "192.0.2.9:5000" in text


# ---- U1: bảng chân trị diagnose() ----------------------------------------------------------------

_RESULTS = ("unreachable", "rejected", "identity_mismatch", "version_mismatch")
_BLOCKING_FALSE = ("rejected", "identity_mismatch", "version_mismatch")
_BLOCKING_TRUE = ("rejected", "version_mismatch")


def _diag(view, failure, discovery):
    from teleport.core.links import ConnectResult
    from teleport.ui import diagnose as mod

    f = None if failure is None else (ConnectResult(failure[0]), failure[1])
    return mod.diagnose(view, f, discovery)


def _expect(d, cause, level, urgent, can_retry, can_ip, result=None):
    from teleport.core.links import ConnectResult
    from teleport.ui import diagnose as mod
    from teleport.ui.widgets import InfoLevel

    assert isinstance(d, mod.Diagnosis)
    assert d.cause is mod.Cause(cause), d
    if cause == "none":
        return
    assert d.level is InfoLevel(level), d
    assert d.urgent is urgent, d
    assert d.can_retry is can_retry, d
    assert d.can_connect_ip is can_ip, d
    assert d.result == (None if result is None else ConnectResult(result)), d


@pytest.mark.parametrize("discovery", [None, True, False])
@pytest.mark.parametrize("failure", [None, *((r, h) for r in _RESULTS for h in (False, True))])
def test_diagnose_row1_none_when_no_view_unpaired_or_connected(failure, discovery):
    _expect(_diag(None, failure, discovery), "none", None, None, None, None)
    _expect(_diag(dview(paired=False, online=True), failure, discovery), "none", None, None, None, None)
    _expect(_diag(dview(paired=False, last=LAST), failure, discovery), "none", None, None, None, None)
    _expect(_diag(dview(online=True, connected=True), failure, discovery), "none", None, None, None, None)
    _expect(_diag(dview(connected=True, last=LAST), failure, discovery), "none", None, None, None, None)


@pytest.mark.parametrize("discovery", [None, True, False])
@pytest.mark.parametrize("online", [False, True])
@pytest.mark.parametrize("last", [None, LAST])
@pytest.mark.parametrize(
    "failure", [*((r, False) for r in _BLOCKING_FALSE), *((r, True) for r in _BLOCKING_TRUE)]
)
def test_diagnose_row2_blocked(failure, online, last, discovery):
    d = _diag(dview(online=online, last=last), failure, discovery)
    _expect(d, "blocked", "error", True, True, True, result=failure[0])


@pytest.mark.parametrize("discovery", [None, True, False])
@pytest.mark.parametrize("online", [False, True])
@pytest.mark.parametrize("last", [None, LAST])
def test_diagnose_row3_wrong_address_has_no_retry(online, last, discovery):
    d = _diag(dview(online=online, last=last), ("identity_mismatch", True), discovery)
    _expect(d, "wrong_address", "warning", True, False, True)


@pytest.mark.parametrize("discovery", [None, True, False])
@pytest.mark.parametrize("last", [None, LAST])
def test_diagnose_row4_isolated_when_online_and_auto_unreachable(last, discovery):
    d = _diag(dview(online=True, last=last), ("unreachable", False), discovery)
    _expect(d, "isolated", "warning", True, True, True)


@pytest.mark.parametrize("discovery", [None, True, False])
@pytest.mark.parametrize("failure", [None, ("unreachable", True)])
def test_diagnose_row5_online_without_blocking_failure_is_none(failure, discovery):
    # H1: lỗi UNREACHABLE từ địa chỉ vừa nhập (from_hint) không thành ISOLATED.
    _expect(_diag(dview(online=True, last=LAST), failure, discovery), "none", None, None, None, None)


@pytest.mark.parametrize("failure", [None, ("unreachable", False), ("unreachable", True)])
@pytest.mark.parametrize(("last", "can_retry"), [(None, False), (LAST, True)])
def test_diagnose_row6_mdns_down(failure, last, can_retry):
    d = _diag(dview(last=last), failure, False)
    _expect(d, "mdns_down", "warning", False, can_retry, True)


@pytest.mark.parametrize("discovery", [None, True])
@pytest.mark.parametrize("failure", [None, ("unreachable", False), ("unreachable", True)])
def test_diagnose_row7_reconnecting_with_saved_address(failure, discovery):
    d = _diag(dview(last=LAST), failure, discovery)
    _expect(d, "reconnecting", "info", False, True, True)


@pytest.mark.parametrize("discovery", [None, True])
@pytest.mark.parametrize("failure", [None, ("unreachable", False), ("unreachable", True)])
def test_diagnose_row8_not_seen_without_address(failure, discovery):
    d = _diag(dview(), failure, discovery)
    _expect(d, "not_seen", "info", False, False, True)


# =================================================================================================
# U16: vị trí thanh, set_device_banner
# =================================================================================================


def _layout_index_of(layout, widget) -> int:
    """Chỉ số mục của `layout` (cấp trên cùng) chứa `widget` (trực tiếp hay trong layout con)."""

    def contains(item) -> bool:
        if item.widget() is widget:
            return True
        sub = item.layout()
        if sub is not None:
            return any(contains(sub.itemAt(i)) for i in range(sub.count()))
        return False

    for i in range(layout.count()):
        if contains(layout.itemAt(i)):
            return i
    return -1


def test_bar_sits_between_header_and_threads(env):
    from teleport.ui.widgets import InfoBar

    assert env.bar is not None, "ChatView phải có DiagnoseBar objectName chatDiagnoseBar"
    assert isinstance(env.bar, InfoBar)
    assert env.bar is not env.view.info_bar
    layout = env.view.layout()
    header = _layout_index_of(layout, env.view.header)
    bar = _layout_index_of(layout, env.bar)
    threads = _layout_index_of(layout, env.view.threads)
    assert -1 not in (header, bar, threads)
    assert header < bar < threads
    assert not shown(env), "chưa có host: thanh ẩn"


def test_set_device_banner_twice_raises(env):
    from PySide6.QtWidgets import QWidget

    other = QWidget()
    with pytest.raises(RuntimeError):
        env.view.set_device_banner(other, [other])
    other.deleteLater()


# =================================================================================================
# U3, U4, U5: khi nào hiện
# =================================================================================================


def test_offline_device_not_shown_before_delay_then_shown(env):
    start(env, dview(last=LAST))
    assert not shown(env)
    elapse(env, env.mod.SHOW_DELAY_MS / 1000 - 0.05)
    assert not shown(env), "chưa đủ SHOW_DELAY_MS kể từ lúc ngoại tuyến"
    elapse(env, 0.06)
    assert shown(env)


def test_offline_device_shown_by_timer_at_delay(env, monkeypatch):
    # QTimer thật, `_monotonic` thật: hẹn đúng phần còn thiếu.
    monkeypatch.setattr(env.mod, "_monotonic", time.monotonic)
    monkeypatch.setattr(env.mod, "SHOW_DELAY_MS", 300)
    began = time.monotonic()
    start(env, dview(last=LAST))
    assert not shown(env)
    env.qtbot.waitUntil(lambda: shown(env), timeout=3000)
    assert time.monotonic() - began >= 0.25


def test_no_bar_before_started(env):
    host = FakeHost()
    env.ctl.attach(host)
    env.model.reset([dview(last=LAST)])
    select(env, PEER_ID)
    elapse(env)
    emit(env, failed(dview(last=LAST), "rejected"))
    assert not shown(env), "node chưa started: không hiện"


def test_blocked_shows_immediately(env):
    from teleport.ui import radar
    from teleport.ui.widgets import InfoLevel

    view = dview(last=LAST)
    start(env, view)
    emit(env, failed(view, "rejected"))
    assert shown(env)
    assert env.bar.level is InfoLevel.ERROR
    from teleport.core.links import ConnectResult

    assert radar.connect_failed_text(view, ConnectResult.REJECTED) in title(env) + "\n" + body(env)
    assert retry_visible(env) and ip_visible(env)


def test_isolated_shows_immediately_with_title_only(env):
    from teleport.core.links import ConnectResult
    from teleport.ui import radar
    from teleport.ui.widgets import InfoLevel

    view = dview(online=True)
    start(env, view)
    assert not shown(env), "trực tuyến, không lỗi: NONE"
    emit(env, failed(view, "unreachable"))
    assert shown(env)
    assert env.bar.level is InfoLevel.WARNING
    assert title(env) == radar.connect_failed_text(view, ConnectResult.UNREACHABLE)
    assert body(env) == ""
    assert retry_visible(env) and ip_visible(env)


def test_poll_false_shows_mdns_down(env):
    from teleport.ui.widgets import InfoLevel

    start(env, dview(last=LAST), discovery=False)
    elapse(env)
    assert shown(env)
    assert env.bar.level is InfoLevel.WARNING
    assert title(env) == env.mod.mdns_down_title()
    assert body(env) == env.mod.mdns_down_body(label())
    assert retry_visible(env), "MDNS_DOWN có địa chỉ đã lưu: có [Thử lại]"
    assert ip_visible(env)


def test_mdns_down_without_address_has_no_retry(env):
    start(env, dview(), discovery=False)
    elapse(env)
    assert title(env) == env.mod.mdns_down_title()
    assert not retry_visible(env)
    assert ip_visible(env)


@pytest.mark.parametrize("last", [LAST, LAST_ODD])
def test_poll_true_with_address_shows_reconnecting_with_formatted_address(env, last):
    from teleport.ui.widgets import InfoLevel

    reconnecting(env, last=last)
    address = env.mod.format_address(*last)
    assert env.bar.level is InfoLevel.INFO
    assert title(env) == env.mod.not_seen_title(label())
    assert body(env) == env.mod.reconnecting_body(address)
    assert address in body(env)
    assert retry_visible(env) and ip_visible(env)


def test_poll_true_without_address_shows_not_seen(env):
    start(env, dview())
    elapse(env)
    assert shown(env)
    assert title(env) == env.mod.not_seen_title(label())
    assert body(env) == env.mod.not_seen_body()
    assert not retry_visible(env)
    assert ip_visible(env)


def test_unknown_discovery_counts_as_running(env):
    start(env, dview(), discovery=_SKIP)  # lần đọc mDNS chưa về: None
    elapse(env)
    assert shown(env)
    assert title(env) == env.mod.not_seen_title(label())


def test_poll_error_keeps_previous_value(env, monkeypatch):
    monkeypatch.setattr(env.mod, "DISCOVERY_POLL_MS", 20)
    start(env, dview(last=LAST), discovery=False)
    elapse(env)
    assert title(env) == env.mod.mdns_down_title()
    next_poll(env, RuntimeError("node đang dừng"))
    next_poll(env, OSError("lỗi lạ"))
    env.model.refresh()
    assert title(env) == env.mod.mdns_down_title()


def test_device_connected_hides_and_later_offline_shows_after_delay(env):
    view = reconnecting(env)
    on = dview(online=True, connected=True, last=LAST)
    emit(env, connected_event(on))
    assert not shown(env)
    env.clock.now += 60  # lâu sau mới ngoại tuyến lại
    emit(env, disconnected_event(dview(online=True, last=LAST)))
    emit(env, offline_event(view))
    env.host.answer_calls(True)
    assert not shown(env), "vừa ngoại tuyến: chưa hiện (hạn tính từ lúc chuyển sang ngoại tuyến, H5)"
    elapse(env)
    assert shown(env)
    assert title(env) == env.mod.not_seen_title(label())


def test_on_started_restarts_offline_clock(env):
    reconnecting(env)
    # Host mới (“Thử lại” của app): mọi máy coi như mới ngoại tuyến.
    start(env, dview(last=LAST))
    assert not shown(env)
    elapse(env)
    assert shown(env)


def test_device_unpaired_hides(env):
    reconnecting(env)
    emit(env, offline_event(dview(paired=False)))  # không còn ghép đôi: model bỏ hàng
    assert not shown(env)


# =================================================================================================
# U2: luật nhận ConnectFailed
# =================================================================================================


def test_hint_unreachable_does_not_make_isolated(env):
    view = dview(online=True)
    start(env, view)
    emit(env, failed(view, "unreachable", from_hint=True))
    assert not shown(env)


def test_hint_unreachable_after_dialog_result_leaves_bar_unchanged(env):
    reconnecting(env)
    before = (title(env), body(env), env.bar.level)
    d = open_dialog(env)
    parts = dialog_parts(d)
    parts.edit.setText(TYPED)
    parts.connect.click()
    from teleport.core.links import ConnectResult

    env.host.finish(ConnectResult.UNREACHABLE)
    from teleport.ui import pairing

    assert parts.error.text() == pairing.unreachable_text(TYPED)
    emit(env, failed(dview(last=LAST), "unreachable", from_hint=True))  # C4: tới sau
    assert (title(env), body(env), env.bar.level) == before
    assert parts.error.text() == pairing.unreachable_text(TYPED), "hộp không nghe ConnectFailed"


def test_hint_unreachable_does_not_clear_remembered_failure(env):
    from teleport.core.links import ConnectResult
    from teleport.ui import radar

    view = dview(last=LAST)
    start(env, view)
    emit(env, failed(view, "rejected"))
    emit(env, failed(view, "unreachable", from_hint=True))
    assert shown(env)
    assert radar.connect_failed_text(view, ConnectResult.REJECTED) in title(env) + "\n" + body(env)


@pytest.mark.parametrize("result", ["rejected", "version_mismatch"])
def test_hint_blocking_result_shows_blocked(env, result):
    from teleport.core.links import ConnectResult
    from teleport.ui import radar
    from teleport.ui.widgets import InfoLevel

    view = dview(online=True)
    start(env, view)
    emit(env, failed(view, result, from_hint=True))
    assert shown(env)
    assert env.bar.level is InfoLevel.ERROR
    assert radar.connect_failed_text(view, ConnectResult(result)) in title(env) + "\n" + body(env)
    assert retry_visible(env)


def test_hint_identity_mismatch_shows_wrong_address_without_retry(env):
    from teleport.core.links import ConnectResult
    from teleport.ui import radar
    from teleport.ui.widgets import InfoLevel

    view = dview(last=LAST)
    start(env, view)
    emit(env, failed(view, "identity_mismatch", from_hint=True))
    assert shown(env), "WRONG_ADDRESS là urgent"
    assert env.bar.level is InfoLevel.WARNING
    assert title(env) == env.mod.wrong_address_title(label())
    assert body(env) == env.mod.wrong_address_body(label())
    spoof = radar.connect_failed_text(view, ConnectResult.IDENTITY_MISMATCH)
    assert spoof not in title(env) + body(env), "không phải câu “giả mạo”"
    assert not retry_visible(env)
    assert ip_visible(env)


def test_auto_identity_mismatch_is_blocked_spoof_sentence(env):
    from teleport.core.links import ConnectResult
    from teleport.ui import radar

    view = dview(last=LAST)
    start(env, view)
    emit(env, failed(view, "identity_mismatch"))
    assert radar.connect_failed_text(view, ConnectResult.IDENTITY_MISMATCH) in title(env) + "\n" + body(env)


def test_device_connected_clears_failure(env):
    view = dview(online=True)
    start(env, view)
    emit(env, failed(view, "rejected"))
    assert shown(env)
    emit(env, connected_event(dview(online=True, connected=True)))
    assert not shown(env)
    emit(env, disconnected_event(dview(online=True)))
    assert not shown(env), "lỗi đã xóa khi DeviceConnected: trực tuyến không lỗi là NONE"


# =================================================================================================
# U6: [Thử lại]
# =================================================================================================


def test_retry_submits_once_and_shows_busy(env):
    from teleport.ui.widgets import InfoLevel

    reconnecting(env)
    env.bar.secondary_button.click()
    assert len(env.host.submits) == 1
    node = env.host.run_submit()
    assert node.retries == [PEER_ID]
    assert node.connects == []
    assert shown(env)
    assert env.bar.level is InfoLevel.INFO
    assert title(env) == env.mod.connecting_title(label())
    assert not retry_visible(env) and not ip_visible(env)
    assert env.bar.close_button.isVisibleTo(env.bar), "đang chờ vẫn có ✕"


def test_retry_ok_hides_after_device_connected(env):
    from teleport.core.links import ConnectResult

    reconnecting(env)
    env.bar.secondary_button.click()
    emit(env, connected_event(dview(online=True, connected=True, last=LAST)))
    env.host.finish(ConnectResult.OK)
    assert not shown(env)


def test_retry_unreachable_shows_still_failed_with_cause_body(env):
    from teleport.core.links import ConnectResult

    reconnecting(env)
    env.bar.secondary_button.click()
    env.host.finish(ConnectResult.UNREACHABLE)
    assert shown(env)
    assert title(env) == env.mod.still_failed_title(label())
    assert body(env) == env.mod.reconnecting_body(env.mod.format_address(*LAST))
    assert retry_visible(env) and ip_visible(env)
    # Bắt đầu lần thử mới xóa `_tried`: lại là trạng thái chờ.
    env.bar.secondary_button.click()
    assert len(env.host.submits) == 2
    assert title(env) == env.mod.connecting_title(label())


@pytest.mark.parametrize("result", ["unreachable", "rejected"])
def test_retry_busy_on_online_device_keeps_bar_with_connecting_title(env, result):
    # H17: đang chờ, diagnose dùng lỗi chụp lúc bấm (`_before_retry`), nên ISOLATED / BLOCKED không biến mất.
    from teleport.ui.widgets import InfoLevel

    view = dview(online=True)
    start(env, view)
    emit(env, failed(view, result))
    assert shown(env)
    env.bar.secondary_button.click()
    assert shown(env), "đang chờ lần [Thử lại]: thanh vẫn hiện"
    assert title(env) == env.mod.connecting_title(label())
    assert env.bar.level is InfoLevel.INFO


def test_retry_unreachable_on_isolated_uses_its_title_as_body(env):
    # H17: core không phát lại `ConnectFailed(UNREACHABLE)` sau `retry_now` (UNREACHABLE_REPORT_AFTER), nên
    # không có sự kiện nào giữa lúc bấm và `on_done`.
    from teleport.core.links import ConnectResult
    from teleport.ui import radar
    from teleport.ui.widgets import InfoLevel

    view = dview(online=True)
    start(env, view)
    emit(env, failed(view, "unreachable"))
    env.bar.secondary_button.click()
    assert shown(env)
    assert title(env) == env.mod.connecting_title(label())
    env.host.finish(ConnectResult.UNREACHABLE)
    assert shown(env)
    assert title(env) == env.mod.still_failed_title(label())
    # Nguyên nhân vẫn là ISOLATED: thân là tiêu đề của ISOLATED, mức WARNING, có hai nút.
    assert body(env) == radar.connect_failed_text(view, ConnectResult.UNREACHABLE)
    assert env.bar.level is InfoLevel.WARNING
    assert retry_visible(env) and ip_visible(env)
    # Lỗi ISOLATED được đặt lại thật (không chỉ overlay): bấm lần nữa vẫn hiện “đang kết nối”.
    env.bar.secondary_button.click()
    assert len(env.host.submits) == 2
    assert shown(env)
    assert title(env) == env.mod.connecting_title(label())


def test_retry_unreachable_on_offline_device_with_address_is_reconnecting_still_failed(env):
    # H17: máy ngoại tuyến thì không đặt ISOLATED; lỗi đã xóa lúc bấm giữ nguyên là đã xóa.
    from teleport.app.node import DeviceOnline
    from teleport.core.links import ConnectResult
    from teleport.ui import radar

    view = reconnecting(env)
    emit(env, failed(view, "unreachable"))  # lỗi tự động cũ của máy ngoại tuyến
    env.bar.secondary_button.click()
    assert shown(env)
    assert title(env) == env.mod.connecting_title(label())
    env.host.finish(ConnectResult.UNREACHABLE)
    assert shown(env)
    assert title(env) == env.mod.still_failed_title(label())
    # Thân của RECONNECTING (không phải tiêu đề ISOLATED): nguyên nhân vẫn là ngoại tuyến.
    assert body(env) == env.mod.reconnecting_body(env.mod.format_address(*LAST))
    assert body(env) != radar.connect_failed_text(view, ConnectResult.UNREACHABLE)
    assert retry_visible(env) and ip_visible(env)
    # Máy hiện lại trong mạng (chưa kết nối): lỗi đã xóa, không có ISOLATED từ lần thử lúc ngoại tuyến.
    emit(env, DeviceOnline(device=dview(online=True, last=LAST)))
    assert not shown(env), "trực tuyến, không lỗi: NONE"


def test_retry_rejected_with_new_event_on_online_blocked_stays_blocked(env):
    from teleport.core.links import ConnectResult
    from teleport.ui import radar
    from teleport.ui.widgets import InfoLevel

    view = dview(online=True)
    start(env, view)
    emit(env, failed(view, "rejected"))
    env.bar.secondary_button.click()
    assert shown(env)
    assert title(env) == env.mod.connecting_title(label())
    emit(env, failed(view, "rejected"))  # lần thử bị từ chối lại: core phát ConnectFailed mới
    env.host.finish(ConnectResult.REJECTED)
    assert shown(env)
    assert env.bar.level is InfoLevel.ERROR
    assert title(env) != env.mod.still_failed_title(label())
    assert radar.connect_failed_text(view, ConnectResult.REJECTED) in title(env) + "\n" + body(env)
    assert retry_visible(env) and ip_visible(env)


@pytest.mark.parametrize("result", ["unreachable", "rejected"])
def test_retry_by_keyboard_keeps_bar_and_focus_while_busy(env, qtbot, result):
    # H17 / N12: nút [Thử lại] đang giữ tiêu điểm; bấm bằng phím thì thanh không ẩn, tiêu điểm ở lại thanh.
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    view = dview(online=True)
    start(env, view)
    emit(env, failed(view, result))
    assert retry_visible(env)
    env.window.activateWindow()
    env.bar.secondary_button.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(lambda: QApplication.focusWidget() is env.bar.secondary_button, timeout=2000)
    QTest.keyClick(env.bar.secondary_button, Qt.Key.Key_Space)
    assert len(env.host.submits) == 1
    assert shown(env), "đang chờ: thanh không bị ẩn"
    assert title(env) == env.mod.connecting_title(label())
    focus = QApplication.focusWidget()
    assert focus is not None, "tiêu điểm bàn phím không được mất"
    assert focus.isVisible()
    assert env.bar.isAncestorOf(focus), "tiêu điểm ở lại trong thanh chẩn đoán"


def test_retry_identity_mismatch_without_event_says_saved_address_changed(env):
    from teleport.core.links import ConnectResult

    reconnecting(env)
    env.bar.secondary_button.click()
    env.host.finish(ConnectResult.IDENTITY_MISMATCH)
    assert shown(env)
    assert title(env) == env.mod.still_failed_title(label())
    ip, port = LAST
    accepted = {
        env.mod.saved_changed_body(env.mod.format_address(ip, port), label()),
        env.mod.saved_changed_body(f"{ip}:{port}", label()),
    }
    assert body(env) in accepted


@pytest.mark.parametrize("result", ["identity_mismatch", "rejected", "version_mismatch"])
def test_retry_blocking_result_with_event_shows_blocked(env, result):
    from teleport.core.links import ConnectResult
    from teleport.ui import radar
    from teleport.ui.widgets import InfoLevel

    view = reconnecting(env)
    env.bar.secondary_button.click()
    emit(env, failed(view, result))
    env.host.finish(ConnectResult(result))
    assert shown(env)
    assert env.bar.level is InfoLevel.ERROR
    assert title(env) != env.mod.still_failed_title(label())
    assert radar.connect_failed_text(view, ConnectResult(result)) in title(env) + "\n" + body(env)


def test_retry_none_hides_retry_until_cause_changes(env, monkeypatch):
    from teleport.ui.widgets import InfoLevel

    monkeypatch.setattr(env.mod, "DISCOVERY_POLL_MS", 20)
    reconnecting(env)
    env.bar.secondary_button.click()
    env.host.finish(None)
    assert shown(env)
    assert env.bar.level is InfoLevel.INFO
    assert title(env) == env.mod.not_seen_title(label())
    assert body(env) == env.mod.not_seen_body(), "H4: NO_TARGET dùng not_seen_body"
    assert not retry_visible(env)
    assert ip_visible(env)
    # Nguyên nhân đổi (mDNS hỏng) → [Thử lại] trở lại.
    next_poll(env, False)
    assert title(env) == env.mod.mdns_down_title()
    assert retry_visible(env)


def test_retry_key_error_drops_device_state(env):
    reconnecting(env)
    env.bar.secondary_button.click()
    env.host.finish(KeyError(PEER_ID))
    assert not (shown(env) and title(env) == env.mod.connecting_title(label()))
    assert not (shown(env) and title(env) == env.mod.still_failed_title(label()))
    env.model.reset([dview(paired=False)])
    assert not shown(env)


@pytest.mark.parametrize("error", [RuntimeError("đang dừng"), asyncio.CancelledError()])
def test_retry_runtime_error_and_cancel_are_silent(env, caplog, error):
    caplog.set_level(logging.DEBUG)
    reconnecting(env)
    env.bar.secondary_button.click()
    env.host.finish(error)
    assert not warnings_about(caplog, type(error).__name__)
    assert not (shown(env) and title(env) == env.mod.still_failed_title(label()))


def test_retry_other_error_logs_warning_and_reenables_buttons(env, caplog):
    caplog.set_level(logging.DEBUG)
    reconnecting(env)
    env.bar.secondary_button.click()
    env.host.finish(OSError("bí mật 7f3a"))
    assert warnings_about(caplog, "OSError"), "lỗi khác: WARNING kèm tên loại lỗi"
    assert not any("bí mật 7f3a" in r.getMessage() for r in caplog.records)
    assert shown(env)
    assert title(env) != env.mod.connecting_title(label())
    assert retry_visible(env) and ip_visible(env)


@pytest.mark.parametrize("kind", ["unreachable", "rejected"])
def test_retry_other_error_on_online_device_keeps_cause(env, kind):
    # Lần thử không ra kết quả (exception) → lỗi chụp lúc bấm được đặt lại, thanh không biến mất.
    view = dview(online=True)
    start(env, view)
    emit(env, failed(view, kind))
    before = title(env)
    env.bar.secondary_button.click()
    env.host.finish(OSError("x"))
    assert shown(env)
    assert title(env) == before
    assert retry_visible(env) and ip_visible(env)


def test_retry_result_of_old_host_is_ignored(env):
    from teleport.core.links import ConnectResult

    reconnecting(env)
    env.bar.secondary_button.click()
    old = env.host
    old_job = old.submits[-1].job
    reconnecting(env)  # host mới, máy vẫn ngoại tuyến, đã quá hạn
    assert old_job.cancel_calls >= 1, "attach hủy việc đang chạy"
    old.finish(ConnectResult.UNREACHABLE)
    assert title(env) == env.mod.not_seen_title(label())


def test_retry_clears_failure_on_submit(env):
    # H6: bấm [Thử lại] xóa lỗi đang nhớ; ConnectFailed của lần đó sẽ đặt lại.
    view = dview(online=True)
    start(env, view)
    emit(env, failed(view, "rejected"))
    env.bar.secondary_button.click()
    env.host.finish(None)  # không có đích, không có ConnectFailed mới
    assert not shown(env), "lỗi đã xóa lúc bấm: trực tuyến không lỗi là NONE"
    emit(env, failed(view, "rejected"))
    assert shown(env)


# =================================================================================================
# U7: hộp “Kết nối bằng IP…”
# =================================================================================================


def test_dialog_prefills_ip_only_for_default_port(env):
    from PySide6.QtCore import Qt

    reconnecting(env, last=LAST)
    d = open_dialog(env)
    parts = dialog_parts(d)
    assert d.objectName() == "connectAddressDialog"
    assert d.parent() is env.window
    assert d.windowModality() == Qt.WindowModality.WindowModal
    assert d.windowTitle() == env.mod.dialog_title()
    assert parts.edit.text() == LAST[0]
    assert parts.edit.selectedText() == LAST[0], "điền sẵn và chọn hết"
    assert parts.connect.isDefault()


def test_dialog_prefills_ip_and_port_for_other_port(env):
    reconnecting(env, last=LAST_ODD)
    parts = dialog_parts(open_dialog(env))
    assert parts.edit.text() == f"{LAST_ODD[0]}:{LAST_ODD[1]}"


def test_dialog_empty_with_placeholder_without_address(env):
    from PySide6.QtWidgets import QLabel

    start(env, dview())
    elapse(env)
    d = open_dialog(env)
    parts = dialog_parts(d)
    assert parts.edit.text() == ""
    assert parts.edit.placeholderText() == "192.168.1.5"
    texts_shown = [w.text() for w in d.findChildren(QLabel)]
    assert env.mod.dialog_label(label()) in [plain(t) for t in texts_shown]
    assert env.mod.dialog_help() in texts_shown
    buddies = [w for w in d.findChildren(QLabel) if w.buddy() is parts.edit]
    assert buddies, "nhãn của ô là buddy"


def test_dialog_invalid_address_shows_error_without_network(env):
    from teleport.ui import radar

    reconnecting(env)
    parts = dialog_parts(open_dialog(env))
    parts.edit.setText("abc")
    parts.connect.click()
    assert env.host.submits == []
    assert parts.error.text() == radar.invalid_address_text("abc")
    assert parts.error.isVisibleTo(parts.error.window())


def test_dialog_valid_address_submits_connect_address_and_waits(env):
    reconnecting(env)
    d = open_dialog(env)
    parts = dialog_parts(d)
    parts.edit.setText(TYPED)
    parts.connect.click()
    assert len(env.host.submits) == 1
    node = env.host.run_submit()
    assert node.connects == [(PEER_ID, "192.0.2.50", 5000)]
    assert node.retries == []
    assert not parts.edit.isEnabled()
    assert not parts.connect.isEnabled()
    assert env.mod.dialog_connecting(TYPED) in shown_labels(parts)


def test_dialog_enter_key_connects(env):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    reconnecting(env)
    parts = dialog_parts(open_dialog(env))
    parts.edit.setText(TYPED)
    QTest.keyClick(parts.edit, Qt.Key.Key_Return)
    assert len(env.host.submits) == 1


@pytest.mark.parametrize(
    ("last", "port_field", "typed", "expected_port"),
    [
        (LAST_ODD, None, "192.0.2.50", LAST_ODD[1]),  # H3: cổng của địa chỉ đã lưu
        (None, 47999, "192.0.2.50", 47999),  # không có: cổng mDNS
        (None, None, "192.0.2.50", 47474),  # cuối cùng: DEFAULT_PORT
    ],
)
def test_dialog_default_port_order(env, last, port_field, typed, expected_port):
    online = port_field is not None
    view = dview(online=online, last=last, port=port_field)
    start(env, view)
    if online:
        emit(env, failed(view, "unreachable"))  # ISOLATED: thanh hiện cho máy trực tuyến
    else:
        elapse(env)
    parts = dialog_parts(open_dialog(env))
    parts.edit.setText(typed)
    parts.connect.click()
    node = env.host.run_submit()
    assert node.connects == [(PEER_ID, typed, expected_port)]


def _dialog_waiting(env, typed: str = TYPED):
    reconnecting(env)
    d = open_dialog(env)
    parts = dialog_parts(d)
    parts.edit.setText(typed)
    parts.connect.click()
    return d, parts


def test_dialog_ok_closes(env):
    from teleport.core.links import ConnectResult

    _dialog_waiting(env)
    env.host.finish(ConnectResult.OK)
    env.qtbot.wait(1)
    assert dialogs(env) == []


def test_dialog_key_error_closes(env):
    _dialog_waiting(env)
    env.host.finish(KeyError(PEER_ID))
    env.qtbot.wait(1)
    assert dialogs(env) == []


def test_dialog_unreachable_reenables_field(env):
    from teleport.core.links import ConnectResult
    from teleport.ui import pairing

    d, parts = _dialog_waiting(env)
    env.host.finish(ConnectResult.UNREACHABLE)
    assert dialogs(env) == [d]
    assert parts.error.text() == pairing.unreachable_text(TYPED)
    assert parts.edit.isEnabled() and parts.connect.isEnabled()


def test_dialog_address_uses_full_ip_and_port_even_for_default_port(env):
    from teleport.core.links import ConnectResult
    from teleport.ui import pairing

    _, parts = _dialog_waiting(env, typed="192.0.2.50")
    assert env.mod.dialog_connecting("192.0.2.50:47474") in shown_labels(parts)
    env.host.finish(ConnectResult.UNREACHABLE)
    assert parts.error.text() == pairing.unreachable_text("192.0.2.50:47474")


def test_dialog_identity_mismatch_says_not_this_device(env):
    from teleport.core.links import ConnectResult

    _, parts = _dialog_waiting(env)
    env.host.finish(ConnectResult.IDENTITY_MISMATCH)
    assert parts.error.text() == env.mod.not_this_device_text(TYPED, label())
    assert parts.edit.isEnabled()


@pytest.mark.parametrize("result", ["rejected", "version_mismatch"])
def test_dialog_blocking_result_uses_radar_sentence(env, result):
    from teleport.core.links import ConnectResult
    from teleport.ui import radar

    _, parts = _dialog_waiting(env)
    env.host.finish(ConnectResult(result))
    assert parts.error.text() == radar.connect_failed_text(dview(last=LAST), ConnectResult(result))
    assert parts.edit.isEnabled()


def test_dialog_value_error_from_node_says_invalid_address(env):
    from teleport.ui import radar

    _, parts = _dialog_waiting(env)
    env.host.finish(ValueError("máy này"))
    assert parts.error.text() == radar.invalid_address_text(TYPED)


def test_dialog_runtime_error_says_unexpected_error(env, caplog):
    caplog.set_level(logging.DEBUG)
    _, parts = _dialog_waiting(env)
    env.host.finish(RuntimeError("đang dừng"))
    assert parts.error.text() == env.mod.connect_error_text()


def test_dialog_other_error_says_unexpected_error_and_logs(env, caplog):
    caplog.set_level(logging.DEBUG)
    _, parts = _dialog_waiting(env)
    env.host.finish(OSError("bí mật 7f3a"))
    assert parts.error.text() == env.mod.connect_error_text()
    assert warnings_about(caplog, "OSError")
    assert not any("bí mật 7f3a" in r.getMessage() for r in caplog.records)


def test_dialog_cancelled_error_is_silent(env, caplog):
    caplog.set_level(logging.DEBUG)
    _, parts = _dialog_waiting(env)
    env.host.finish(asyncio.CancelledError())
    assert not warnings_about(caplog, "CancelledError")
    assert parts.error.text() != env.mod.connect_error_text()


def test_dialog_cancel_while_waiting_cancels_job_and_closes(env):
    _, parts = _dialog_waiting(env)
    job = env.host.submits[-1].job
    parts.cancel.click()
    env.qtbot.wait(1)
    assert job.cancel_calls >= 1
    assert dialogs(env) == []


def test_dialog_escape_while_waiting_cancels_job_and_closes(env):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    d, _ = _dialog_waiting(env)
    job = env.host.submits[-1].job
    QTest.keyClick(d, Qt.Key.Key_Escape)
    env.qtbot.wait(1)
    assert job.cancel_calls >= 1
    assert dialogs(env) == []


def test_dialog_ignores_connect_failed(env):
    d, parts = _dialog_waiting(env)
    for result in ("unreachable", "identity_mismatch", "rejected"):
        emit(env, failed(dview(last=LAST), result, from_hint=True))
    assert dialogs(env) == [d]
    assert env.mod.dialog_connecting(TYPED) in shown_labels(parts), "hộp chỉ theo giá trị connect_address trả"
    assert not parts.edit.isEnabled()


def test_dialog_only_one_at_a_time(env):
    reconnecting(env)
    d = open_dialog(env)
    env.bar.action_button.click()
    env.qtbot.wait(1)
    assert dialogs(env) == [d]


def test_dialog_keeps_its_device_when_switching(env):
    reconnecting(env)
    env.model.reset([dview(last=LAST), dview(OTHER_ID, OTHER)])
    parts = dialog_parts(open_dialog(env))
    select(env, OTHER_ID)
    parts.edit.setText(TYPED)
    parts.connect.click()
    node = env.host.run_submit()
    assert node.connects == [(PEER_ID, "192.0.2.50", 5000)]


def test_open_dialog_hides_retry_and_busy_still_allows_dialog(env):
    # H11
    reconnecting(env)
    d = open_dialog(env)
    assert not retry_visible(env), "hộp đang mở cho máy đó: ẩn [Thử lại]"
    dialog_parts(d).cancel.click()
    env.qtbot.wait(1)
    assert retry_visible(env)
    env.bar.secondary_button.click()  # đang _busy
    assert title(env) == env.mod.connecting_title(label())
    # Đang chờ thì hai nút ẩn; hộp vẫn mở được qua cùng đường của action (bấm thẳng).
    env.bar.action_triggered.emit()
    env.qtbot.wait(1)
    assert len(dialogs(env)) == 1


# =================================================================================================
# U8: nút ✕
# =================================================================================================


def test_dismiss_hides_until_key_changes_and_connecting_forgets(env, monkeypatch):
    monkeypatch.setattr(env.mod, "DISCOVERY_POLL_MS", 20)
    view = reconnecting(env)
    env.bar.close_button.click()
    assert not shown(env)
    next_poll(env, True)
    elapse(env)
    assert not shown(env), "cùng khóa: không hiện lại"
    # Nguyên nhân đổi → hiện lại.
    next_poll(env, False)
    assert shown(env)
    assert title(env) == env.mod.mdns_down_title()
    # Bấm ✕ lần nữa, rồi kết nối được → quên; ngoại tuyến lại cùng khóa thì hiện.
    env.bar.close_button.click()
    assert not shown(env)
    emit(env, connected_event(dview(online=True, connected=True, last=LAST)))
    emit(env, disconnected_event(dview(online=True, last=LAST)))
    emit(env, offline_event(view))
    next_poll(env, False)
    elapse(env)
    assert shown(env)
    assert title(env) == env.mod.mdns_down_title()


# =================================================================================================
# U9: poll mDNS
# =================================================================================================


def test_poll_reads_discovery_active_one_call_at_a_time(env, monkeypatch):
    monkeypatch.setattr(env.mod, "DISCOVERY_POLL_MS", 20)
    start(env, dview(last=LAST), discovery=_SKIP)
    assert len(env.host.pending_calls()) == 1, "đọc ngay khi on_started / đổi máy"
    assert drive(env.host.calls[0].fn(FakeNode(False))) is False, "fn đọc node.discovery_active"
    env.qtbot.wait(200)
    assert len(env.host.pending_calls()) == 1, "một call một lúc"
    env.host.answer_calls(False)
    elapse(env)
    assert title(env) == env.mod.mdns_down_title()
    next_poll(env, False)  # lần đọc sau vẫn tới


def test_poll_only_while_chat_view_visible(env, monkeypatch):
    monkeypatch.setattr(env.mod, "DISCOVERY_POLL_MS", 20)
    start(env, dview(last=LAST))
    env.view.hide()
    env.host.answer_calls(True)
    env.qtbot.wait(200)
    assert env.host.pending_calls() == [], "ChatView ẩn: nhịp poll bỏ qua"
    env.view.show()
    env.qtbot.waitUntil(lambda: len(env.host.pending_calls()) == 1, timeout=2000)


def test_no_poll_for_online_device(env, monkeypatch):
    monkeypatch.setattr(env.mod, "DISCOVERY_POLL_MS", 20)
    start(env, dview(online=True), discovery=_SKIP)
    env.qtbot.wait(200)
    assert env.host.calls == []


def test_poll_stops_when_switching_to_online_device(env, monkeypatch):
    monkeypatch.setattr(env.mod, "DISCOVERY_POLL_MS", 20)
    start(env, dview(last=LAST), dview(OTHER_ID, OTHER, online=True, connected=True))
    select(env, OTHER_ID)
    env.host.answer_calls(True)
    env.qtbot.wait(200)
    assert env.host.pending_calls() == []


@pytest.mark.parametrize("stop", ["attach", "attach_none", "close_all"])
def test_poll_stops_on_attach_and_close_all(env, monkeypatch, stop):
    monkeypatch.setattr(env.mod, "DISCOVERY_POLL_MS", 20)
    host = start(env, dview(last=LAST))
    env.qtbot.waitUntil(lambda: len(host.pending_calls()) == 1, timeout=2000)
    if stop == "attach":
        env.ctl.attach(FakeHost())
    elif stop == "attach_none":
        env.ctl.attach(None)
    else:
        env.ctl.close_all()
    host.answer_calls(False)  # kết quả muộn bị bỏ
    count = len(host.calls)
    env.qtbot.wait(200)
    assert len(host.calls) == count
    assert not shown(env)


def test_poll_result_of_old_host_is_ignored(env):
    old = start(env, dview(last=LAST), discovery=_SKIP)
    reconnecting(env)  # host mới, mDNS chạy
    old.answer_calls(False)
    env.model.refresh()
    assert title(env) == env.mod.not_seen_title(label())


# =================================================================================================
# U10: đổi máy
# =================================================================================================


def test_bar_follows_selected_device(env):
    reconnecting(env)
    env.model.reset([dview(last=LAST), dview(OTHER_ID, OTHER, online=True, connected=True)])
    select(env, OTHER_ID)
    assert not shown(env)
    select(env, PEER_ID)
    env.host.answer_calls(True)
    assert shown(env)
    assert title(env) == env.mod.not_seen_title(label())
    select(env, None)
    assert not shown(env)


def test_late_retry_result_of_previous_device_not_drawn_on_new_one(env):
    from teleport.core.links import ConnectResult

    start(env, dview(last=LAST), dview(OTHER_ID, OTHER))
    elapse(env)
    env.bar.secondary_button.click()
    select(env, OTHER_ID)
    env.host.answer_calls(True)
    assert title(env) == env.mod.not_seen_title(label(OTHER, OTHER_ID))
    env.host.finish(ConnectResult.UNREACHABLE)
    assert title(env) == env.mod.not_seen_title(label(OTHER, OTHER_ID))
    assert body(env) == env.mod.not_seen_body()


# =================================================================================================
# U11: Tab, trợ năng
# =================================================================================================


def test_focus_chain_order_and_accessible_names(env):
    from teleport.ui import texts

    bar = env.bar
    assert list(bar.focus_chain()) == [
        bar.title_label,
        bar.text_label,
        bar.action_button,
        bar.secondary_button,
        bar.close_button,
    ]
    info = env.view.info_bar
    expected = [
        info.title_label,
        info.text_label,
        info.action_button,
        info.close_button,
        *bar.focus_chain(),
        env.view.empty_state.title_label,
        env.view.threads,
    ]
    order: list = []
    w = info.title_label
    for _ in range(500):
        if w in expected and w not in order:
            order.append(w)
        w = w.nextInFocusChain()
        if w is info.title_label:
            break
    assert order == expected

    reconnecting(env)
    assert plain(bar.action_button.accessibleName()) == env.mod.connect_by_ip()
    assert plain(bar.action_button.text()) == env.mod.connect_by_ip()
    assert plain(bar.secondary_button.accessibleName()) == texts.retry()
    assert plain(bar.secondary_button.text()) == texts.retry()
    for button in (bar.action_button, bar.secondary_button):
        assert button.accessibleDescription() == title(env)


def test_hidden_retry_leaves_tab_chain(env, qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    start(env, dview())
    elapse(env)
    assert not retry_visible(env)
    env.window.activateWindow()
    env.bar.action_button.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(lambda: QApplication.focusWidget() is env.bar.action_button, timeout=2000)
    QTest.keyClick(env.bar.action_button, Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is env.bar.close_button


# =================================================================================================
# U12: show_message chỉ khi khóa đổi
# =================================================================================================


def test_show_message_called_once_for_repeated_polls(env, monkeypatch):
    calls: list = []
    real = env.bar.show_message

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(env.bar, "show_message", spy)
    monkeypatch.setattr(env.mod, "DISCOVERY_POLL_MS", 20)
    reconnecting(env)
    assert len(calls) == 1
    for _ in range(10):
        env.qtbot.waitUntil(lambda: len(env.host.pending_calls()) == 1, timeout=2000)
        env.host.answer_calls(True)
        env.model.refresh()
    assert len(calls) == 1
    # H10: mọi nguồn chữ là hàm (đổi ngôn ngữ dịch lại được).
    _level, title_src, *_rest = calls[0]
    assert callable(title_src)


# =================================================================================================
# U13: đổi ngôn ngữ
# =================================================================================================


def test_language_change_retranslates_bar_and_keeps_it_visible(env, qapp):
    from teleport.ui import i18n, texts

    reconnecting(env)
    vi_title = title(env)
    vi_retry = env.bar.secondary_button.text()
    i18n.set_language("en")
    qapp.processEvents()
    assert i18n.current_language() == "en"
    assert shown(env), "đổi ngôn ngữ không ẩn thanh"
    assert title(env) == env.mod.not_seen_title(label())
    assert body(env) == env.mod.reconnecting_body(env.mod.format_address(*LAST))
    assert plain(env.bar.secondary_button.text()) == plain(texts.retry())
    assert plain(env.bar.action_button.text()) == env.mod.connect_by_ip()
    assert env.bar.secondary_button.accessibleDescription() == title(env)
    assert title(env) != vi_title, "cần bản English cho not_seen_title"
    assert env.bar.secondary_button.text() != vi_retry


def test_language_change_retranslates_dialog(env, qapp):
    from teleport.ui import i18n

    reconnecting(env)
    d = open_dialog(env)
    vi = d.windowTitle()
    i18n.set_language("en")
    qapp.processEvents()
    assert d.windowTitle() == env.mod.dialog_title()
    assert d.windowTitle() != vi
    assert plain(dialog_parts(d).connect.text()) == env.mod.dialog_connect()


# =================================================================================================
# U14: không chạm Node từ luồng Qt
# =================================================================================================


def test_node_only_touched_off_qt_thread(env):
    from concurrent.futures import ThreadPoolExecutor

    from teleport.core.links import ConnectResult

    nodes: list[FakeNode] = []
    pool = ThreadPoolExecutor(1)

    def on_worker(fn, discovery=True):
        def run():
            node = FakeNode(discovery)
            nodes.append(node)
            return drive(fn(node))

        return pool.submit(run).result(5)

    try:
        start(env, dview(last=LAST), discovery=_SKIP)
        for c in env.host.pending_calls():
            c.answered = True
            c.on_done(on_worker(c.fn))
        elapse(env)
        env.bar.secondary_button.click()
        on_worker(env.host.submits[-1].fn)
        env.host.finish(ConnectResult.UNREACHABLE)
        parts = dialog_parts(open_dialog(env))
        parts.edit.setText(TYPED)
        parts.connect.click()
        on_worker(env.host.submits[-1].fn)
        env.host.finish(ConnectResult.UNREACHABLE)
    finally:
        pool.shutdown()
    qt = threading.get_ident()
    touched = [t for n in nodes for t in n.threads]
    assert touched, "fn phải chạm node (đọc discovery_active, gọi retry_connect / connect_address)"
    assert qt not in touched
    assert any(n.retries == [PEER_ID] for n in nodes)
    assert any(n.connects == [(PEER_ID, "192.0.2.50", 5000)] for n in nodes)


# =================================================================================================
# U15: attach host mới
# =================================================================================================


@pytest.mark.parametrize("new_host", [True, False])
def test_attach_hides_bar_closes_dialog_cancels_jobs_and_clears_state(env, new_host):
    reconnecting(env)
    env.bar.secondary_button.click()
    retry_job = env.host.submits[-1].job
    env.bar.action_triggered.emit()  # H11: đang chờ vẫn mở được hộp
    env.qtbot.wait(1)
    parts = dialog_parts(the_dialog(env))
    parts.edit.setText(TYPED)
    parts.connect.click()
    dialog_job = env.host.submits[-1].job
    env.bar.close_button.click()  # ✕ đang nhớ
    if new_host:
        env.ctl.attach(FakeHost())
    else:
        env.ctl.attach(None)
    env.qtbot.wait(1)
    assert not shown(env)
    assert dialogs(env) == []
    assert retry_job.cancel_calls >= 1
    assert dialog_job.cancel_calls >= 1
    # Trạng thái xóa: host mới chạy, cùng máy ngoại tuyến → chờ hạn mới rồi hiện (✕ đã quên).
    start(env, dview(last=LAST))
    assert not shown(env)
    elapse(env)
    assert shown(env)
    assert title(env) == env.mod.not_seen_title(label())


def test_close_all_closes_dialog_and_cancels_job(env):
    _dialog_waiting(env)
    job = env.host.submits[-1].job
    env.ctl.close_all()
    env.qtbot.wait(1)
    assert job.cancel_calls >= 1
    assert dialogs(env) == []


# =================================================================================================
# Nối dây trong TransfersController (spec 2.8)
# =================================================================================================


def test_transfers_controller_wires_diagnose(qtbot, monkeypatch):
    from teleport.ui import diagnose as mod
    from teleport.ui import transfers
    from teleport.ui.main_window import MainWindow
    from teleport.ui.tray import Tray

    monkeypatch.setattr(Tray, "show_message", lambda self, text, folder=None: None)
    window = MainWindow()
    qtbot.addWidget(window)
    tray = Tray()
    ctl = transfers.TransfersController(window, tray, parent=None)
    try:
        assert isinstance(ctl.diagnose, mod.DiagnoseController)
        assert ctl.diagnose.parent() is ctl
        bar = ctl.view.findChild(mod.DiagnoseBar, "chatDiagnoseBar")
        assert bar is not None
        host = FakeHost()
        window.devices.clear()
        ctl.attach(host)
        ctl.on_started(host)
        view = dview(last=LAST)
        window.devices.reset([view])
        assert window.conversations.select_device(PEER_ID)
        event = failed(view, "rejected")
        window.devices.apply(event)
        ctl.on_node_event(event)
        assert bar.isVisibleTo(ctl.view), "on_node_event / current_changed phải tới DiagnoseController"
        ctl.close_all()
    finally:
        tray.dispose()
