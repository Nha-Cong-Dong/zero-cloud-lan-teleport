"""C1b: `hello.os` và tên của máy đã ghép đôi lấy từ kết nối điều khiển (kèm A6).

Viết theo đặc tả, không theo code: `specs/spec-dot-c.md` mục 2.1–2.3, 2.5 và mục 11 (M2, Q6, thắng mục 2
khi khác).

- `DeviceView.os` của máy đã ghép đôi lấy từ `hello.os` của kết nối điều khiển đã qua mTLS, chỉ trong RAM;
  giữ khi máy đó ngoại tuyến, tới khi hủy ghép đôi hay đóng node (Q6).
- Máy đã ghép đôi đổi tên (`Node.set_name` ở máy kia): ở `LinkUp` / `LinkSwitched`, trước
  `DeviceConnected` và trước `TransferManager.control_up`, node ghi `hello.name` vào `trusted.json`
  (`TrustStore.rename`) và phát `DeviceRenamed(device, old_name)`. Trần theo thiết bị
  `PEER_RENAME_MIN_INTERVAL` (đọc lúc dùng); tên bị trần chặn được áp khi hết trần nếu kết nối điều
  khiển hiện tại vẫn đúng chứng chỉ (M2). Lỗi ghi: WARNING, kết nối vẫn chạy. Tên TXT mDNS không bao
  giờ đổi tên đã lưu. Đổi tên không đổi tin cậy (không `Listener.update_trusted`).

Hai node thật trên 127.0.0.1 (`transfer_nodes`: A nhận, B gửi). Kết nối điều khiển chỉ có hint (không
mDNS, không `hold`), nên sau khi cắt, bên mở phải gọi `connect_hint` lại. Hệ điều hành của từng node
được giả bằng cách vá `teleport.app.node.device_os` (A khởi động trước B trong `transfer_nodes`).

Giả định về API (spec chưa nói đủ cụ thể):
- `DeviceRenamed` import từ `teleport.app.node`; `old_name` là tên đã lưu trước khi đổi.
- Lỗi ghi `trusted.json` giả bằng cách vá `teleport.trust.store.write_atomic` (ném `OSError`), số lần
  ghi đếm cũng qua seam đó.
- WARNING ghi qua logger `teleport.app.node`, có `device_id[:8]` và tên lớp lỗi (`TrustStoreError`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import pytest
from support import wait_until

from teleport.app import node as node_mod
from teleport.app import transfers as transfers_mod
from teleport.app.node import (
    DeviceConnected,
    DeviceDisconnected,
    DeviceOffline,
    DeviceOnline,
    DeviceRenamed,
    MdnsOptions,
)
from teleport.core.discovery import DeviceAdded, DeviceRemoved, DeviceUpdated
from teleport.net import connection
from teleport.trust import store as store_mod
from teleport.trust.store import TRUSTED_FILE, TrustStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

LOCAL = "127.0.0.1"
OS_A = "zq_os_a"  # đúng luật protocol §3.3 (1–16 ký tự [a-z0-9_]), không trùng HĐH thật nào
OS_B = "zq_os_b"
OS_OTHER = "zq_os_x"


# --- Fixture, hàm phụ trợ --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def roomy_close(monkeypatch):
    """Đóng node trong tiến trình: nới `CLOSE_DISK_WAIT` để máy chậm không làm test đỏ lúc dọn."""
    monkeypatch.setattr(transfers_mod, "CLOSE_DISK_WAIT", 10.0)


@pytest.fixture
def peer_os(monkeypatch):
    """Node khởi động thứ nhất có `hello.os` = OS_A, thứ hai OS_B, các node sau OS_OTHER."""
    values = iter([OS_A, OS_B])

    def fake_device_os() -> str:
        return next(values, OS_OTHER)

    monkeypatch.setattr(node_mod, "device_os", fake_device_os)


@pytest.fixture
def store_writes(monkeypatch):
    """Ghi lại mọi lần ghi `trusted.json` (đường dẫn), và cho phép giả lỗi ghi theo thư mục.

    `state.paths`: các `Path` đã ghi thành công hay thất bại; `state.fail_dirs`: thư mục cấu hình mà
    lần ghi `trusted.json` ném `OSError`.
    """
    real = store_mod.write_atomic

    class State:
        def __init__(self) -> None:
            self.paths: list[Path] = []
            self.fail_dirs: set[Path] = set()

        def count(self, config_dir: Path) -> int:
            return sum(1 for p in self.paths if same_dir(p.parent, config_dir))

    state = State()

    def spy(path, data, *args, **kwargs):
        p = Path(path)
        state.paths.append(p)
        if p.name == TRUSTED_FILE and any(same_dir(p.parent, d) for d in state.fail_dirs):
            raise PermissionError(13, "giả lập không ghi được trusted.json")
        return real(path, data, *args, **kwargs)

    monkeypatch.setattr(store_mod, "write_atomic", spy)
    return state


def same_dir(a, b) -> bool:
    return os.path.normcase(os.path.realpath(os.fspath(a))) == os.path.normcase(
        os.path.realpath(os.fspath(b))
    )


def opener_of(net):
    """Bên mở kết nối điều khiển (và giữ hint) theo `net.client`."""
    return net.B if net.client == "sender" else net.A


def stored_names(config_dir: Path) -> dict[str, str]:
    """`device_id → name` đọc thẳng từ `trusted.json` trên đĩa."""
    data = json.loads((config_dir / TRUSTED_FILE).read_text(encoding="utf-8"))
    return {d["device_id"]: d["name"] for d in data["devices"]}


async def drop_control(net) -> None:
    """Cắt kết nối điều khiển A–B và chờ cả hai bên báo mất kết nối."""
    A, B = net.A, net.B
    da = len(A.rec.events_of(DeviceDisconnected, B.id))
    db = len(B.rec.events_of(DeviceDisconnected, A.id))
    conn = A.control_connection(B.id)
    assert conn is not None, "A chưa có kết nối điều khiển tới B"
    conn.abort()
    await A.rec.wait_event(DeviceDisconnected, B.id, n=da + 1, timeout=10)
    await B.rec.wait_event(DeviceDisconnected, A.id, n=db + 1, timeout=10)


async def reconnect(net) -> DeviceConnected:
    """Bên mở đưa hint lại; chờ cả hai bên báo kết nối; trả `DeviceConnected` mới nhất của A."""
    A, B = net.A, net.B
    na = len(A.rec.events_of(DeviceConnected, B.id))
    nb = len(B.rec.events_of(DeviceConnected, A.id))
    opener = opener_of(net)
    other = B if opener is A else A
    opener.connect_hint(other.id, [LOCAL], other.port)
    event = await A.rec.wait_event(DeviceConnected, B.id, n=na + 1, timeout=10)
    await B.rec.wait_event(DeviceConnected, A.id, n=nb + 1, timeout=10)
    return event


async def cycle(net) -> DeviceConnected:
    await drop_control(net)
    return await reconnect(net)


def index_of(events: list, event: object) -> int:
    return next(i for i, e in enumerate(events) if e is event)


def renamed_events(node, device_id: str) -> list[DeviceRenamed]:
    return node.rec.events_of(DeviceRenamed, device_id)


def make_file(directory: Path, name: str, size: int) -> Path:
    path = directory / name
    path.write_bytes(bytes(i % 251 for i in range(size)))
    return path


async def send_and_finish(net, name: str = "f.bin", size: int = 1000):
    src = make_file(net.src, name, size)
    key = await net.B.transfers.send(net.A.id, [src])
    fin_b = await net.B.trec.wait_finished(key.transfer_id, 30)
    fin_a = await net.A.trec.wait_finished(key.transfer_id, 30)
    assert str(fin_b.result) == "done" and str(fin_a.result) == "done", (fin_a, fin_b)
    return key


# --- hello.os ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("client", ["sender", "receiver"])
async def test_device_connected_carries_hello_os(transfer_nodes, peer_os, client):
    """Không có mDNS: `os` của view chỉ có thể đến từ `hello` đã xác thực, cả chiều vào lẫn ra."""
    net = await transfer_nodes(client=client)
    A, B = net.A, net.B
    assert (A.hello.os, B.hello.os) == (OS_A, OS_B)

    at_a = A.rec.events_of(DeviceConnected, B.id)[0]
    at_b = B.rec.events_of(DeviceConnected, A.id)[0]
    assert at_a.outgoing is (client == "receiver")
    assert at_b.outgoing is (client == "sender")
    assert at_a.device.os == OS_B
    assert at_b.device.os == OS_A
    assert [v.os for v in A.devices()] == [OS_B]
    assert [v.os for v in B.devices()] == [OS_A]


async def test_hello_os_kept_while_paired_device_offline(transfer_nodes, peer_os, fake_mdns, app_helpers):
    """Q6: `hello.os` thắng mDNS, và còn giữ khi máy đó rời mDNS và mất kết nối điều khiển."""
    net = await transfer_nodes(receiver_opts={"mdns": MdnsOptions(advertise=False)})
    A, B = net.A, net.B
    m = fake_mdns.of(A.node)
    m.emit(DeviceAdded(app_helpers.dev(B, os="linux")))
    online = await A.rec.wait_event(DeviceOnline, B.id)
    assert online.device.os == OS_B, "hello đã xác thực phải thắng HĐH trong TXT mDNS"

    m.emit(DeviceRemoved(app_helpers.dev(B, os="linux")))
    offline = await A.rec.wait_event(DeviceOffline, B.id)
    assert offline.device.os == OS_B
    assert offline.device.online is False

    await drop_control(net)
    gone = A.rec.events_of(DeviceDisconnected, B.id)[-1]
    assert gone.device.os == OS_B
    view = next(v for v in A.devices() if v.device_id == B.id)
    assert (view.os, view.online, view.connected) == (OS_B, False, False)

    await A.unpair(B.id)
    assert all(v.device_id != B.id for v in A.devices())


async def test_os_not_written_to_disk(transfer_nodes, peer_os, store_writes):
    """`os` của máy kia không bao giờ ghi đĩa, kể cả khi `trusted.json` được ghi lại vì đổi tên."""
    net = await transfer_nodes(client="receiver")
    A, B = net.A, net.B
    B.set_name("B-Moi")
    await cycle(net)
    assert A.store.get(B.id).name == "B-Moi"
    assert store_writes.count(A.dir) >= 1, "đổi tên phải ghi lại trusted.json"
    await send_and_finish(net)  # ghi thêm lịch sử, trạng thái truyền…
    await A.close()
    await B.close()

    for config_dir, marker in ((A.dir, OS_B), (B.dir, OS_A)):
        files = [p for p in Path(config_dir).rglob("*") if p.is_file()]
        assert any(p.name == TRUSTED_FILE for p in files)
        leaked = [str(p) for p in files if marker.encode() in p.read_bytes()]
        assert not leaked, f"`os` của máy kia nằm trên đĩa: {leaked}"


# --- Đổi tên ---------------------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("client", ["sender", "receiver"])
async def test_rename_applied_on_incoming_and_outgoing(
    transfer_nodes, peer_os, store_writes, monkeypatch, client
):
    """B đổi tên; kết nối điều khiển mới (vào hay ra ở A) mang `hello.name` mới → A cập nhật
    `trusted.json`, phát `DeviceRenamed` trước `DeviceConnected`, view mang tên và `os` mới."""
    net = await transfer_nodes(client=client)
    A, B = net.A, net.B
    before = A.store.get(B.id)
    assert before.name == "B"

    update_calls: list[object] = []
    real_update = connection.Listener.update_trusted

    def spy_update(self, certs):
        update_calls.append(self)
        return real_update(self, certs)

    monkeypatch.setattr(connection.Listener, "update_trusted", spy_update)
    writes_before = store_writes.count(A.dir)

    assert B.set_name("B-Moi") == "B-Moi"
    connected = await cycle(net)

    renamed = renamed_events(A, B.id)
    assert len(renamed) == 1
    assert renamed[0].old_name == "B"
    assert renamed[0].device.name == "B-Moi"
    assert renamed[0].device.paired is True
    assert index_of(A.rec.events, renamed[0]) < index_of(A.rec.events, connected)
    assert connected.outgoing is (client == "receiver")
    assert connected.device.name == "B-Moi"
    assert connected.device.os == OS_B

    after = A.store.get(B.id)
    assert after.name == "B-Moi"
    assert (after.cert_pem, after.fingerprint, after.paired_at) == (
        before.cert_pem,
        before.fingerprint,
        before.paired_at,
    )
    assert stored_names(A.dir) == {B.id: "B-Moi"}
    assert TrustStore.load(A.dir).get(B.id) == after
    assert store_writes.count(A.dir) == writes_before + 1
    assert [v.name for v in A.devices()] == ["B-Moi"]
    # Đổi tên chỉ đổi tên, không đổi tin cậy.
    assert update_calls == []
    assert A.store.is_trusted(before.fingerprint)
    # B không đổi gì về A.
    assert renamed_events(B, A.id) == []
    assert stored_names(B.dir) == {A.id: "A"}


async def test_offer_received_uses_new_name(transfer_nodes, peer_os, monkeypatch):
    """`offer` tới ngay khi kết nối điều khiển mới lên vẫn mang tên mới: tên đã lưu đổi **trước** khi
    `TransferManager.control_up` được gọi (spec §2.2: `_refresh_peer` trước `_transfer_control_up`)."""
    net = await transfer_nodes(client="sender")  # B mở và giữ hint
    A, B = net.A, net.B
    await drop_control(net)

    names_at_control_up: list[str] = []
    real_control_up = transfers_mod.TransferManager.control_up

    def spy_control_up(self, device_id, *args, **kwargs):
        if self is A.transfers and device_id == B.id:
            names_at_control_up.append(A.store.get(B.id).name)
        return real_control_up(self, device_id, *args, **kwargs)

    monkeypatch.setattr(transfers_mod.TransferManager, "control_up", spy_control_up)
    B.set_name("B-Moi")
    src = make_file(net.src, "f.bin", 2000)
    key = await B.transfers.send(A.id, [src])  # lượt chờ kết nối: `offer` đi ngay khi kết nối lên
    B.connect_hint(A.id, [LOCAL], A.port)
    offer = await A.trec.wait_kind("OfferReceived", key.transfer_id, timeout=15)
    assert offer.device_name == "B-Moi"
    assert A.store.get(B.id).name == "B-Moi"
    assert names_at_control_up, "TransferManager.control_up chưa được gọi cho kết nối mới"
    assert names_at_control_up[0] == "B-Moi", "control_up được gọi trước khi đổi tên đã lưu"
    fin = await A.trec.wait_finished(key.transfer_id, 30)
    assert str(fin.result) == "done"


async def test_no_rename_when_unpaired_during_open(transfer_nodes, peer_os, store_writes, monkeypatch):
    """A hủy ghép đôi B trong lúc `open_control` tới B đang chờ: tên mới của B không được ghi,
    B không "hồi sinh" trong kho, view hay kết nối."""
    net = await transfer_nodes(client="receiver")  # A mở
    A, B = net.A, net.B
    await drop_control(net)
    B.set_name("B-Moi")
    writes_before = store_writes.count(A.dir)

    real_open = node_mod.open_control
    opened = asyncio.Event()
    release = asyncio.Event()

    async def gated_open(*args, **kwargs):
        conn = await real_open(*args, **kwargs)
        opened.set()
        try:
            await release.wait()
        except BaseException:
            conn.abort()
            raise
        return conn

    monkeypatch.setattr(node_mod, "open_control", gated_open)
    nb = len(B.rec.events_of(DeviceDisconnected, A.id))
    A.connect_hint(B.id, [LOCAL], B.port)
    await asyncio.wait_for(opened.wait(), 10)
    await A.unpair(B.id)
    release.set()
    # Kết nối mà A đã mở tới B phải bị đóng (B thấy mất kết nối với A).
    await wait_until(
        lambda: (
            B.control_connection(A.id) is None and len(B.rec.events_of(DeviceDisconnected, A.id)) >= nb + 1
        ),
        10,
        "B vẫn còn kết nối điều khiển với A",
    )
    await asyncio.sleep(0.2)  # cho mọi callback còn lại chạy (kiểm “không có gì xảy ra thêm”)

    assert renamed_events(A, B.id) == []
    assert len(A.rec.events_of(DeviceConnected, B.id)) == 1
    assert A.store.get(B.id) is None
    assert B.id not in A.connected_devices()
    assert A.devices() == ()
    assert B.id not in stored_names(A.dir)
    assert b"B-Moi" not in (A.dir / TRUSTED_FILE).read_bytes()
    assert store_writes.count(A.dir) == writes_before + 1  # chỉ lần ghi của `unpair`


async def test_rename_rate_limited(transfer_nodes, peer_os, store_writes, monkeypatch):
    """Máy đã ghép đôi đổi tên rồi nối lại liên tục: chỉ một lần ghi trong `PEER_RENAME_MIN_INTERVAL`."""
    monkeypatch.setattr(node_mod, "PEER_RENAME_MIN_INTERVAL", 3600.0)
    net = await transfer_nodes(client="receiver")
    A, B = net.A, net.B
    writes_before = store_writes.count(A.dir)

    B.set_name("B-1")
    first = await cycle(net)
    assert first.device.name == "B-1"
    assert A.store.get(B.id).name == "B-1"

    for name in ("B-2", "B-3", "B-4"):
        B.set_name(name)
        connected = await cycle(net)
        assert connected.device.name == "B-1", "tên bị trần chặn không được ghi ngay"

    assert A.store.get(B.id).name == "B-1"
    assert stored_names(A.dir) == {B.id: "B-1"}
    assert store_writes.count(A.dir) == writes_before + 1
    assert [e.device.name for e in renamed_events(A, B.id)] == ["B-1"]
    assert B.id in A.connected_devices(), "trần đổi tên không cắt kết nối"


@pytest.mark.slow
async def test_rename_rate_limited_applied_after_interval(transfer_nodes, peer_os, store_writes, monkeypatch):
    """M2: tên bị trần chặn được nhớ và áp khi hết trần, vì kết nối hiện tại vẫn đúng chứng chỉ."""
    interval = 3.0
    monkeypatch.setattr(node_mod, "PEER_RENAME_MIN_INTERVAL", interval)
    net = await transfer_nodes(client="receiver")
    A, B = net.A, net.B
    writes_before = store_writes.count(A.dir)

    B.set_name("B-1")
    await cycle(net)
    first = renamed_events(A, B.id)
    assert [e.device.name for e in first] == ["B-1"]
    t_first = A.rec.event_times[index_of(A.rec.events, first[0])]

    B.set_name("B-2")
    connected = await cycle(net)
    elapsed = time.monotonic() - t_first
    assert elapsed < interval, f"nối lại mất {elapsed:.2f} s ≥ trần {interval} s: không kiểm được nhánh chờ"
    assert connected.device.name == "B-1"
    assert A.store.get(B.id).name == "B-1"

    await wait_until(
        lambda: A.store.get(B.id).name == "B-2", interval + 5, "tên chờ chưa được áp khi hết trần"
    )
    renamed = renamed_events(A, B.id)
    assert [(e.old_name, e.device.name) for e in renamed] == [("B", "B-1"), ("B-1", "B-2")]
    t_second = A.rec.event_times[index_of(A.rec.events, renamed[1])]
    assert t_second - t_first >= interval - 0.1, "tên chờ bị áp trước khi hết trần"
    assert stored_names(A.dir) == {B.id: "B-2"}
    assert store_writes.count(A.dir) == writes_before + 2
    assert [v.name for v in A.devices()] == ["B-2"]
    assert len(A.rec.events_of(DeviceDisconnected, B.id)) == 2, "áp tên chờ không cắt kết nối"


@pytest.mark.slow
async def test_pending_rename_not_applied_without_control_connection(
    transfer_nodes, peer_os, store_writes, monkeypatch
):
    """M2: hết trần mà không còn kết nối điều khiển → không áp; lần nối sau (đã hết trần) áp ngay."""
    interval = 2.0
    monkeypatch.setattr(node_mod, "PEER_RENAME_MIN_INTERVAL", interval)
    net = await transfer_nodes(client="receiver")
    A, B = net.A, net.B

    B.set_name("B-1")
    await cycle(net)
    t_first = A.rec.event_times[index_of(A.rec.events, renamed_events(A, B.id)[0])]
    B.set_name("B-2")
    await cycle(net)
    assert A.store.get(B.id).name == "B-1"
    await drop_control(net)
    remaining = t_first + interval + 0.5 - time.monotonic()
    if remaining > 0:
        await asyncio.sleep(remaining)  # kiểm “không có gì xảy ra” khi hết trần
    assert A.store.get(B.id).name == "B-1"
    assert len(renamed_events(A, B.id)) == 1

    connected = await reconnect(net)
    renamed = renamed_events(A, B.id)
    assert [(e.old_name, e.device.name) for e in renamed] == [("B", "B-1"), ("B-1", "B-2")]
    assert index_of(A.rec.events, renamed[1]) < index_of(A.rec.events, connected)
    assert connected.device.name == "B-2"


@pytest.mark.slow
async def test_pending_rename_dropped_when_unpaired(transfer_nodes, peer_os, store_writes, monkeypatch):
    """Tên đang chờ hết trần không được ghi sau khi máy đó bị hủy ghép đôi."""
    interval = 2.0
    monkeypatch.setattr(node_mod, "PEER_RENAME_MIN_INTERVAL", interval)
    net = await transfer_nodes(client="receiver")
    A, B = net.A, net.B

    B.set_name("B-1")
    await cycle(net)
    t_first = A.rec.event_times[index_of(A.rec.events, renamed_events(A, B.id)[0])]
    B.set_name("B-2")
    await cycle(net)
    await A.unpair(B.id)
    writes = store_writes.count(A.dir)
    remaining = t_first + interval + 0.5 - time.monotonic()
    if remaining > 0:
        await asyncio.sleep(remaining)
    assert A.store.get(B.id) is None
    assert B.id not in stored_names(A.dir)
    assert store_writes.count(A.dir) == writes
    assert len(renamed_events(A, B.id)) == 1


async def test_store_write_failure_logs_and_connection_survives(
    transfer_nodes, peer_os, store_writes, caplog
):
    """`trusted.json` không ghi được: WARNING (8 ký tự đầu mã máy, loại lỗi), tên cũ giữ nguyên,
    kết nối điều khiển vẫn chạy (truyền file được)."""
    net = await transfer_nodes(client="receiver")
    A, B = net.A, net.B
    before_bytes = (A.dir / TRUSTED_FILE).read_bytes()
    store_writes.fail_dirs.add(A.dir)
    caplog.set_level(logging.WARNING, logger="teleport.app.node")

    B.set_name("B-Moi")
    connected = await cycle(net)

    assert connected.device.name == "B"
    assert renamed_events(A, B.id) == []
    assert A.store.get(B.id).name == "B"
    assert (A.dir / TRUSTED_FILE).read_bytes() == before_bytes
    warnings = [
        r
        for r in caplog.records
        if r.name == "teleport.app.node" and r.levelno == logging.WARNING and B.id[:8] in r.getMessage()
    ]
    assert warnings, "thiếu WARNING khi không ghi được tên mới"
    assert any("TrustStoreError" in r.getMessage() for r in warnings)
    assert not any(B.id in r.getMessage() for r in warnings), "chỉ ghi 8 ký tự đầu của mã máy"

    disconnects = len(A.rec.events_of(DeviceDisconnected, B.id))
    await send_and_finish(net)
    assert B.id in A.connected_devices()
    assert len(A.rec.events_of(DeviceDisconnected, B.id)) == disconnects


async def test_mdns_name_never_renames_paired(transfer_nodes, peer_os, store_writes, fake_mdns, app_helpers):
    """Tên trong TXT mDNS (không xác thực) không bao giờ đổi tên đã lưu của máy đã ghép đôi."""
    net = await transfer_nodes(receiver_opts={"mdns": MdnsOptions(advertise=False)})
    A, B = net.A, net.B
    before_bytes = (A.dir / TRUSTED_FILE).read_bytes()
    writes_before = store_writes.count(A.dir)
    m = fake_mdns.of(A.node)

    first = app_helpers.dev(B, name="Ten-Gia")
    m.emit(DeviceAdded(first))
    online = await A.rec.wait_event(DeviceOnline, B.id)
    assert online.device.name == "B"
    second = app_helpers.dev(B, name="Ten-Gia-2")
    m.emit(DeviceUpdated(second, first))
    await asyncio.sleep(0.2)  # kiểm “không có gì xảy ra”

    assert renamed_events(A, B.id) == []
    assert A.store.get(B.id).name == "B"
    assert [v.name for v in A.devices()] == ["B"]
    assert (A.dir / TRUSTED_FILE).read_bytes() == before_bytes
    assert store_writes.count(A.dir) == writes_before

    # Nối lại sau khi thấy tên TXT lạ: `hello.name` vẫn là "B", nên vẫn không đổi tên.
    connected = await cycle(net)
    assert connected.device.name == "B"
    assert renamed_events(A, B.id) == []
    assert store_writes.count(A.dir) == writes_before


# --- Kiểm fingerprint của kết nối (soat-code C1b, mục 1) --------------------------------------------
# Qua API công khai, kết nối điều khiển luôn đúng chứng chỉ đã ghim, nên nhánh "fingerprint lệch" chỉ
# kiểm được bằng cách gọi thẳng `_refresh_peer` / `_apply_pending_name` với kết nối giả. Mỗi test có
# ca đối chứng (cùng kết nối giả nhưng đúng fingerprint thì đổi tên), để chắc ca "lệch" chỉ sai đúng ở
# fingerprint.


def fake_conn(fingerprint: str, device_id: str, name: str, os_name: str):
    """Thay cho `Connection`: chỉ có `peer.fingerprint` và `peer.hello` (đã kiểm như `net` giao)."""
    from types import SimpleNamespace

    import teleport
    from teleport.core.messages import PROTOCOL_VERSION, Hello

    hello = Hello(PROTOCOL_VERSION, device_id, name, teleport.__version__, os_name)
    return SimpleNamespace(peer=SimpleNamespace(fingerprint=fingerprint, hello=hello))


def other_fingerprint(fingerprint: str) -> str:
    return ("1" if fingerprint[0] == "0" else "0") + fingerprint[1:]


async def test_refresh_peer_ignores_connection_with_other_fingerprint(
    node_factory, trust_each_other, peer_os, store_writes
):
    trust_each_other("A", "B")
    A = await node_factory("A")
    B_id = next(d.device_id for d in A.store.devices())
    saved = A.store.get(B_id)
    writes = store_writes.count(A.dir)

    A.node._refresh_peer(B_id, fake_conn(other_fingerprint(saved.fingerprint), B_id, "B-Moi", OS_OTHER))
    assert A.store.get(B_id) == saved
    assert store_writes.count(A.dir) == writes
    assert renamed_events(A, B_id) == []
    assert [v.os for v in A.devices()] == [None], "`os` của kết nối lệch chứng chỉ không được nhớ"

    # Đối chứng: đúng fingerprint thì nhớ `os` và đổi tên.
    A.node._refresh_peer(B_id, fake_conn(saved.fingerprint, B_id, "B-Moi", OS_OTHER))
    assert A.store.get(B_id).name == "B-Moi"
    assert store_writes.count(A.dir) == writes + 1
    assert [e.device.name for e in renamed_events(A, B_id)] == ["B-Moi"]
    assert [v.os for v in A.devices()] == [OS_OTHER]


async def test_apply_pending_name_ignores_current_connection_with_other_fingerprint(
    transfer_nodes, peer_os, store_writes
):
    net = await transfer_nodes(client="receiver")
    A, B = net.A, net.B
    node = A.node
    conn_id = node._links.connected()[B.id]
    real = node._conns[conn_id]
    saved = A.store.get(B.id)
    writes = store_writes.count(A.dir)

    node._pending_names[B.id] = "B-Cho"
    node._conns[conn_id] = fake_conn(other_fingerprint(saved.fingerprint), B.id, "B-Cho", OS_B)
    try:
        node._apply_pending_name(B.id)
    finally:
        node._conns[conn_id] = real
    assert A.store.get(B.id) == saved
    assert store_writes.count(A.dir) == writes
    assert renamed_events(A, B.id) == []

    # Đối chứng: kết nối hiện tại đúng chứng chỉ thì tên chờ được áp.
    node._pending_names[B.id] = "B-Cho"
    node._apply_pending_name(B.id)
    assert A.store.get(B.id).name == "B-Cho"
    assert store_writes.count(A.dir) == writes + 1
    assert [e.device.name for e in renamed_events(A, B.id)] == ["B-Cho"]


# --- Ghép đôi lại khi đang có tên chờ (soat-code C1b, mục 4) ----------------------------------------


@pytest.mark.slow
async def test_repair_drops_pending_name(transfer_nodes, peer_os, store_writes, monkeypatch):
    """B đổi tên (A ghi), đổi tiếp bị trần chặn (tên chờ), rồi hai máy ghép đôi lại trong lúc kết nối
    cũ còn mở. Khi hết trần, tên đã lưu phải là tên lúc ghép đôi lại, không quay về tên đang chờ."""
    interval = 4.0
    monkeypatch.setattr(node_mod, "PEER_RENAME_MIN_INTERVAL", interval)
    net = await transfer_nodes(
        receiver_opts={"answer": True, "accept_pairing": True},
        sender_opts={"answer": True, "accept_pairing": True},
        client="receiver",
    )
    A, B = net.A, net.B

    B.set_name("B-1")
    await cycle(net)
    t_first = A.rec.event_times[index_of(A.rec.events, renamed_events(A, B.id)[0])]
    B.set_name("B-2")
    await cycle(net)  # kết nối này mang "B-2", bị trần chặn → tên chờ
    assert A.store.get(B.id).name == "B-1"
    conn_before = A.control_connection(B.id)
    connects = len(A.rec.events_of(DeviceConnected, B.id))

    B.set_name("B-3")
    result = await asyncio.wait_for(A.pairing.pair_with([LOCAL], B.port), 15)
    assert str(result.outcome) == "paired", result
    name_after_pairing = A.store.get(B.id).name
    assert name_after_pairing != "B-2"
    assert time.monotonic() - t_first < interval, "ghép đôi lại xong sau khi hết trần: không kiểm được"

    remaining = t_first + interval + 0.5 - time.monotonic()
    if remaining > 0:
        await asyncio.sleep(remaining)  # kiểm “không có gì xảy ra” khi hết trần
    if A.control_connection(B.id) is conn_before:
        assert len(A.rec.events_of(DeviceConnected, B.id)) == connects
    assert A.store.get(B.id).name == name_after_pairing, "tên chờ từ trước khi ghép đôi lại đã đè tên mới"
    assert stored_names(A.dir)[B.id] == name_after_pairing
    assert all(e.device.name != "B-2" for e in renamed_events(A, B.id))
