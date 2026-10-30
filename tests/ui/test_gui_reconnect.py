"""GUI nhận tắt giữa lượt rồi mở lại trên cổng mới: máy gửi TỰ nối lại (PR-9, T2; mvp H4, F6).

Viết theo đặc tả, không theo code: protocol §6.2 (“Tắt app nhận”: trạng thái nối lại nằm trên đĩa, đóng
app nhận không gửi `cancel`, máy gửi thấy mất kết nối và **tự** nối lại theo lịch 1, 2, 4… tối đa 30 s
một lần), mvp F6 (tự nối lại sau rớt mạng, kể cả khi đã tắt app nhận).

Khác `test_g_restart_gui_mid_transfer_resumes_row_and_finishes` (`test_gui_transfer.py`), ở đó lần mở
thứ hai test gọi lại `connect_hint` cho máy gửi, nên không kiểm được việc máy gửi tự nối lại. Ở đây
máy gửi B chỉ được đưa địa chỉ **một lần**: cổng của một proxy TCP (`tcp_proxy`) đứng trước GUI nhận A.
GUI A thoát giữa lượt; mở lại thì node có cổng mới (fixture `gui` ép cổng 0), test chỉ chuyển đích của
proxy sang cổng mới (như đổi IP/cổng thật sau khi khởi động lại) rồi chờ: không `connect_hint` lần hai.

Bằng chứng B tự nối lại: B không có mDNS, A không lưu địa chỉ của B nên không tự nối ngược, cổng
mới khác cổng cũ, và proxy (địa chỉ duy nhất B biết) nhận kết nối mới sau khi đổi đích. Bộ đếm
`connect_hint` chỉ là chốt chặn để test không vô tình gọi lại.

Lớp `Peer` và vài hàm phụ trợ chép tối thiểu từ `test_gui_transfer.py` (file test không import lẫn nhau).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

pytestmark = [pytest.mark.slow, pytest.mark.integration]

LOCAL = "127.0.0.1"
MiB = 1024 * 1024
# Lần thử đã hẹn có thể rơi đúng lúc vừa hỏng với khoảng chờ tối đa; cộng phần dư cho bắt tay TLS,
# đồng bộ lại và nhịp dò 20 ms của kịch bản.
HANDSHAKE_MARGIN = 5.0


class Peer:
    """`Node` thật (máy B, máy gửi) trong thread riêng; đếm số lần test gọi `connect_hint`."""

    def __init__(self, config: Path, receive_dir: Path) -> None:
        self.config = config
        self.receive_dir = receive_dir
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="test-peer-b", daemon=True)
        self.node = None
        self.updates: list = []
        self.hint_calls: list[tuple] = []
        self._lock = threading.Lock()

    def _on_update(self, update) -> None:
        with self._lock:
            self.updates.append(update)

    def start(self) -> Peer:
        from teleport.app.node import Node

        self.thread.start()

        async def go():
            self.node = Node(
                self.config,
                name="May-B",
                host=LOCAL,
                port=0,
                receive_dir=self.receive_dir,
                auto_accept=True,
                on_transfer_event=self._on_update,
            )
            await self.node.start()
            real = self.node.connect_hint

            def counted(device_id, addresses, port):
                self.hint_calls.append((device_id, tuple(addresses), port))
                return real(device_id, addresses, port)

            self.node.connect_hint = counted

        self.run(go(), 20)
        return self

    def run(self, coro, timeout: float = 10.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def soon(self, fn, *args) -> None:
        self.loop.call_soon_threadsafe(fn, *args)

    def connected_to(self, device_id: str) -> bool:
        async def check() -> bool:
            return device_id in self.node.connected_devices()

        return self.run(check(), 5)

    def set_rate(self, rate: int | None) -> None:
        async def go() -> None:
            self.node.transfers.rate_limit = rate

        self.run(go(), 5)

    def send(self, device_id: str, *paths: Path):
        return asyncio.run_coroutine_threadsafe(self.node.transfers.send(device_id, list(paths)), self.loop)

    def sent_bytes(self, transfer_id: str) -> int:
        with self._lock:
            return max(
                (
                    u.info.done_bytes
                    for u in self.updates
                    if type(u).__name__ == "TransferProgress" and u.info.key.transfer_id == transfer_id
                ),
                default=0,
            )

    def statuses(self, transfer_id: str) -> list[str]:
        with self._lock:
            return [
                str(u.info.status)
                for u in self.updates
                if type(u).__name__ == "TransferStatusChanged" and u.info.key.transfer_id == transfer_id
            ]

    def finished(self, transfer_id: str):
        with self._lock:
            for u in self.updates:
                if type(u).__name__ == "TransferFinished" and u.info.key.transfer_id == transfer_id:
                    return u
        return None

    def close(self) -> None:
        if self.node is not None:
            with contextlib.suppress(Exception):
                self.run(asyncio.wait_for(self.node.close(), 30), 35)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        if not self.thread.is_alive():
            self.loop.close()


@pytest.fixture
def peer(tmp_path):
    made: list[Peer] = []

    def make(config: Path) -> Peer:
        p = Peer(config, tmp_path / "nhan-B")
        made.append(p)
        return p.start()

    yield make
    for p in reversed(made):
        p.close()


@pytest.fixture
def pair(config_dir, trust_each_other, tmp_path):
    """Hai thư mục cấu hình tin nhau: (a, b, a_id, b_id, src)."""

    def make():
        a, b = config_dir("A"), config_dir("B")
        id_a, id_b = trust_each_other(a, b, name_a="May-A", name_b="May-B")
        src = tmp_path / "nguon"
        src.mkdir(exist_ok=True)
        return a, b, id_a.device_id, id_b.device_id, src

    return make


@pytest.fixture
def fast_progress(monkeypatch):
    from teleport.app import transfers

    monkeypatch.setattr(transfers, "PROGRESS_INTERVAL", 0.2)


def make_file(directory: Path, name: str, size: int) -> Path:
    path = directory / name
    with open(path, "wb") as f:
        left = size
        while left:
            chunk = os.urandom(min(left, 4 * MiB))
            f.write(chunk)
            left -= len(chunk)
    return path


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4 * MiB):
            h.update(chunk)
    return h.hexdigest()


def controller(run):
    app = run.tray.parent()
    assert app is not None and hasattr(app, "transfers"), "GuiApp phải có thuộc tính transfers"
    return app.transfers


def find_row(ctl, transfer_id: str):
    for r in ctl.view.rows():
        if r.key.transfer_id == transfer_id:
            return r
    return None


def row_widgets_of(ctl, transfer_id: str) -> int:
    """Số widget thẻ thật của lượt (không qua `rows()`), để bắt thẻ bị dựng hai lần."""
    from teleport.ui.transfers_view import TransferRow

    return sum(1 for r in ctl.view.findChildren(TransferRow) if r.key.transfer_id == transfer_id)


def a_finished(updates: list, transfer_id: str):
    for u in updates:
        if (
            type(u).__name__ == "TransferFinished"
            and u.info.key.transfer_id == transfer_id
            and str(u.result) != "suspended"
        ):
            return u
    return None


def a_port(run, gui_wait):
    got: list = []
    run.host.call(lambda node: node.port, got.append)
    yield gui_wait(lambda: got, 5, "cổng của A")
    assert isinstance(got[0], int), got[0]
    return got[0]


def start_gui(run, gui_wait, updates: list | None = None):
    yield gui_wait(lambda: run.hosts and run.host.running, 15, "node A chạy")
    if updates is not None:
        run.host.transfer_event.connect(updates.append)


def test_h_restart_gui_peer_reconnects_by_itself_through_proxy(
    gui, gui_wait, pair, peer, tmp_path, tcp_proxy, fast_progress, ui_helpers
):
    from teleport.app.transfers import TransferResult
    from teleport.core.links import RETRY_MAX
    from teleport.ui.transfers_view import result_status

    a, b, a_id, _, src = pair()
    p = peer(b)
    f = make_file(src, "tu-noi-lai.bin", 6 * MiB)
    want = sha256_of(f)
    updates: list = []
    seen: dict = {}
    box: dict = {}

    def first(run):
        yield from start_gui(run, gui_wait)
        port_1 = yield from a_port(run, gui_wait)
        proxy = tcp_proxy(port_1)
        box["proxy"] = proxy
        p.soon(p.node.connect_hint, a_id, [LOCAL], proxy.port)  # lần duy nhất B được đưa địa chỉ
        yield gui_wait(lambda: p.connected_to(a_id), 10, "B nối tới A qua proxy")
        seen["port_1"] = port_1
        ctl = controller(run)
        p.set_rate(1 * MiB)
        p.send(a_id, f)
        yield gui_wait(lambda: ctl.offers.keys(), 10, "hộp hỏi")
        keys = list(ctl.offers.keys())
        assert len(keys) == 1, keys
        buttons = ui_helpers.buttons_with_text(ctl.offers.box(keys[0]), ui_helpers.M6_ACCEPT)
        assert len(buttons) == 1
        buttons[0].click()
        tid = keys[0].transfer_id
        seen["tid"] = tid
        yield gui_wait(
            lambda: find_row(ctl, tid) is not None and p.sent_bytes(tid) >= f.stat().st_size // 5,
            20,
            "B gửi tới 20 %",
        )
        run.tray.quit_requested.emit()  # thoát GUI có trật tự giữa lượt

    gui(["--config-dir", str(a)], first)
    tid = seen["tid"]
    proxy = box["proxy"]
    assert p.finished(tid) is None, "B không được kết thúc lượt chỉ vì A tắt"

    def second(run):
        yield from start_gui(run, gui_wait, updates)
        ctl = controller(run)
        yield gui_wait(lambda: find_row(ctl, tid) is not None, 10, "thẻ của lượt dở")
        seen["rows_after_start"] = row_widgets_of(ctl, tid)
        port_2 = yield from a_port(run, gui_wait)
        seen["port_2"] = port_2
        accepted_before = proxy.accepted
        proxy.target = (LOCAL, port_2)  # “máy nhận đổi cổng”; B không được báo gì
        switched = time.monotonic()
        yield gui_wait(lambda: p.connected_to(a_id), RETRY_MAX + HANDSHAKE_MARGIN, "B tự nối lại tới A")
        seen["reconnect_after"] = time.monotonic() - switched
        seen["accepted_after_switch"] = proxy.accepted - accepted_before
        yield gui_wait(lambda: find_row(ctl, tid).finished, 60, "thẻ kết thúc sau khi B tự nối lại")
        seen["status"] = find_row(ctl, tid).status_label.text()
        seen["rows_at_end"] = row_widgets_of(ctl, tid)
        yield gui_wait(lambda: a_finished(updates, tid), 5, "A thấy TransferFinished")
        seen["a"] = str(a_finished(updates, tid).result)
        yield gui_wait(lambda: p.finished(tid), 15, "B thấy lượt kết thúc")
        seen["b"] = str(p.finished(tid).result)
        seen["done_label"] = result_status(TransferResult.DONE)
        run.tray.quit_requested.emit()

    gui(["--config-dir", str(a)], second, timeout=150)
    assert seen["port_2"] != seen["port_1"], "lần mở thứ hai phải ở cổng khác để kiểm được việc tự nối lại"
    assert len(p.hint_calls) == 1, f"test chỉ được đưa địa chỉ cho B một lần: {p.hint_calls}"
    assert seen["accepted_after_switch"] >= 1, "B phải nối lại qua proxy (địa chỉ duy nhất nó biết)"
    assert seen["reconnect_after"] <= RETRY_MAX + HANDSHAKE_MARGIN, seen["reconnect_after"]
    assert "reconnecting" in p.statuses(tid), f"B phải qua “Đang nối lại…”: {p.statuses(tid)}"
    assert seen["rows_after_start"] == 1 and seen["rows_at_end"] == 1, "một lượt một thẻ"
    assert seen["a"] == TransferResult.DONE and seen["b"] == TransferResult.DONE, seen
    assert seen["done_label"] in seen["status"], (seen["status"], seen["done_label"])
    assert sha256_of(tmp_path / "nhan-gui" / f.name) == want
    print(f"\nB reconnected {seen['reconnect_after']:.1f} s after the receiver restarted on a new port")
