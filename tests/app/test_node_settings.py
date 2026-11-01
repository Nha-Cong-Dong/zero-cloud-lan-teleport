"""C1 (= P4-0): cài đặt lưu được, đổi lúc chạy, theo thiết bị, tạm dừng nhận, thư mục nhận chung.

Viết theo đặc tả, không theo code: `specs/spec-dot-c.md` mục 1.2.3 (`Node`), 1.2.4 (`TransferManager`),
1.3 (bất biến), 1.5 (tiêu chí xong) và mục 11 (thắng mục 1 khi khác: H1, H2, M1, L1, L2).
C1c (= P4-0b, spec mục 4.3, 4.6): `offer` chưa nhận (kiểm chỗ trống, chờ hỏi, xếp hàng) không còn làm
thư mục nhận “bận”; đổi thư mục thì engine kiểm lại chúng trên thư mục mới (`recheck_space`), kết quả
kiểm trên thư mục cũ bị bỏ (`check_id`), và job `volume_info` của thư mục cũ được chờ trước khi đóng nó.

Quy ước như `test_transfers.py`: hai node thật trên 127.0.0.1 (`transfer_nodes`, **A nhận, B gửi**),
hằng thời gian rút ngắn bằng `fast`. Chờ theo điều kiện (`wait_until`), không ngủ cố định trừ khi kiểm
“không có gì xảy ra thêm”. `settings.json` đọc thẳng từ đĩa để kiểm “ghi file trước, đổi RAM sau”.

Giả định về API (spec chưa nói đủ cụ thể):
- `ReceiveDirChoice`, `ReceiveDirBusy`, `ReceiveDirUnavailable`, `SettingsSaveError`: từ `teleport.app.node`.
- Tạm dừng nhận gọi qua `node.transfers.pause_receiving(...)` / `receiving_paused` (spec đặt ở
  `TransferManager`); `busy_devices()` / `receive_dir_busy()` cũng ở `node.transfers`.
- Khóa JSON của `settings.json` trùng tên trường của `Settings`.
- Lỗi ghi `settings.json` giả bằng cách vá `Settings.save` ở lớp (ném `OSError`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import wait_until

from teleport.app import node as node_mod
from teleport.app import settings as settings_mod
from teleport.app import storage
from teleport.app import transfers as transfers_mod
from teleport.app.node import DeviceConnected, DeviceDisconnected, MdnsOptions
from teleport.core import transfer as core_transfer
from teleport.core.messages import MessageError
from teleport.net import connection

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

LOCAL = "127.0.0.1"
MiB = 1024 * 1024
SLOW_RATE = 4 * MiB
REAL_GRACE = core_transfer.RECONNECT_GRACE  # đọc lúc import, trước khi `fast` vá
SIZE = 5 * MiB + 3  # lượt nhận dở dựng thẳng trên đĩa: 6 khối, khối cuối ngắn
MIN_RATE = transfers_mod.MIN_RATE_LIMIT


# --- Hàm phụ trợ ---------------------------------------------------------------------------------


def make_file(directory: Path, name: str, size: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(os.urandom(size))
    return path


def write_settings(config_dir: Path, data: dict) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def on_disk(config_dir: Path) -> dict:
    """Nội dung `settings.json` (chưa có file → `{}`)."""
    path = config_dir / "settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def same_path(a, b) -> bool:
    return os.path.normcase(os.path.realpath(os.fspath(a))) == os.path.normcase(
        os.path.realpath(os.fspath(b))
    )


def recv_key(net, tid: str):
    return core_transfer.TransferKey(net.B.id, core_transfer.Direction.RECEIVE, tid)


async def wait_status(node, key, status: str, timeout: float = 15.0) -> None:
    def ok() -> bool:
        info = node.transfers.get(key)
        return info is not None and str(info.status) == status

    await wait_until(ok, timeout, f"lượt {key.transfer_id[:8]} chưa ở trạng thái {status}")


async def wait_not_busy(node, timeout: float = 10.0) -> None:
    await wait_until(lambda: not node.transfers.receive_dir_busy(), timeout, "thư mục nhận vẫn bận")


async def send_and_finish(net, name: str = "f.bin", size: int = 1000):
    """B gửi một file cho A (A tự nhận); trả (key, đường dẫn nguồn); chờ DONE hai phía."""
    src = make_file(net.src, name, size)
    key = await net.B.transfers.send(net.A.id, [src])
    fin_b = await net.B.trec.wait_finished(key.transfer_id, 30)
    fin_a = await net.A.trec.wait_finished(key.transfer_id, 30)
    assert str(fin_b.result) == "done" and str(fin_a.result) == "done", (fin_a, fin_b)
    return key, src


def failing_save(monkeypatch) -> SimpleNamespace:
    """`Settings.save` ném OSError tới khi `state.fail = False`; `state.calls` đếm số lần gọi."""
    real = settings_mod.Settings.save
    state = SimpleNamespace(fail=True, calls=0)

    def save(self, config_dir):
        state.calls += 1
        if state.fail:
            raise OSError(28, "giả lập không ghi được settings.json")
        return real(self, config_dir)

    monkeypatch.setattr(settings_mod.Settings, "save", save)
    return state


def build_running(root: Path, record_for, h, tid: str, *, sender: str, receiver: str, age: float) -> Path:
    """Lượt nhận dở trên đĩa (như test_transfer_restore): `transfer.json`, `0.part` có khối 0, lùi mtime."""
    data = random.Random(tid).randbytes(SIZE)
    with storage.ReceiveDir.open(root) as rd:
        temp = rd.create_temp(tid, record_for(tid, [SIZE], receiver=receiver, sender=sender))
        part = temp.create_part(0, SIZE)
        part.commit(h.write_blocks(part, data, (0,)))
        rd.close_temp(temp)
    d = root / ".teleport" / tid
    t = time.time() - age
    for p in d.iterdir():
        os.utime(p, (t, t))
    os.utime(d, (t, t))
    return d


class Gate:
    """Chặn một thread đĩa: `wait()` báo `hit` rồi chờ `release` (≤ 30 s)."""

    def __init__(self) -> None:
        self.hit = threading.Event()
        self.release = threading.Event()

    def wait(self) -> None:
        self.hit.set()
        if not self.release.wait(30):
            raise AssertionError("cổng chặn của test không được mở")


class RdSpy:
    """Ghi lại `ReceiveDir` mở/đóng (an toàn giữa thread) và cho chặn `open` của một đường dẫn."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.opened: list[storage.ReceiveDir] = []
        self.events: list[tuple] = []  # ("close", rd) | ("close_temp", rd, tid) | mốc tự thêm
        self.holds: dict[str, Gate] = {}

    @staticmethod
    def _key(path) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    def hold(self, path) -> Gate:
        gate = Gate()
        self.holds[self._key(path)] = gate
        return gate

    def add(self, *event) -> None:
        with self.lock:
            self.events.append(event)

    def of_root(self, path) -> list[storage.ReceiveDir]:
        with self.lock:
            return [rd for rd in self.opened if same_path(rd.root, path)]

    def index(self, *event) -> int | None:
        with self.lock:
            for i, e in enumerate(self.events):
                if len(e) == len(event) and all(a is b or a == b for a, b in zip(e, event, strict=True)):
                    return i
        return None

    def install(self, monkeypatch) -> None:
        spy = self
        real_open = storage.ReceiveDir.open
        real_close = storage.ReceiveDir.close
        real_close_temp = storage.ReceiveDir.close_temp

        def open_(cls, root):
            gate = spy.holds.get(spy._key(root))
            if gate is not None:
                gate.wait()
            rd = real_open(root)
            with spy.lock:
                spy.opened.append(rd)
            return rd

        def close(rd):
            if not rd.closed:
                spy.add("close", rd)
            return real_close(rd)

        def close_temp(rd, temp):
            spy.add("close_temp", rd, temp.transfer_id)
            return real_close_temp(rd, temp)

        monkeypatch.setattr(storage.ReceiveDir, "open", classmethod(open_))
        monkeypatch.setattr(storage.ReceiveDir, "close", close)
        monkeypatch.setattr(storage.ReceiveDir, "close_temp", close_temp)


# --- Fixture -------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def roomy_close(monkeypatch):
    """Node đóng trong tiến trình test: hạn chờ hàng đĩa rộng hơn (máy CI chậm)."""
    monkeypatch.setattr(transfers_mod, "CLOSE_DISK_WAIT", 10.0)


@pytest.fixture
def rd_spy(monkeypatch):
    spy = RdSpy()
    spy.install(monkeypatch)
    return spy


@pytest.fixture
def cleanup_spy(monkeypatch):
    """Mọi lần `ReceiveDir.cleanup` chạy xong: `(root, retention)`."""
    calls: list[SimpleNamespace] = []
    real = storage.ReceiveDir.cleanup

    def cleanup(self, wall_now, *, own_device_id, skip, retention):
        n = real(self, wall_now, own_device_id=own_device_id, skip=skip, retention=retention)
        calls.append(SimpleNamespace(root=self.root, retention=retention, removed=n))
        return n

    monkeypatch.setattr(storage.ReceiveDir, "cleanup", cleanup)
    return calls


@pytest.fixture
def receiver(node_factory, app_helpers):
    """`await receiver(label, **node_kwargs)` → `AppNode` có `trec` (một node nhận, không nối ai)."""

    async def make(label: str = "A", *, factory: dict | None = None, **node_kwargs):
        trec = app_helpers.TransferRecorder(label)
        node_kwargs.setdefault("on_transfer_event", trec.on_update)
        n = await node_factory(label, node_kwargs=node_kwargs, **(factory or {}))
        n.trec = trec
        return n

    return make


# =================================================================================================
# Tên máy
# =================================================================================================


async def test_name_from_settings_when_no_arg(node_factory, tmp_path, fake_mdns):
    write_settings(tmp_path / "A", {"version": 1, "name": "Ten-Da-Luu"})
    A = await node_factory("A", hostname=True)  # name=None
    assert A.name == "Ten-Da-Luu"
    assert A.hello.name == "Ten-Da-Luu"
    assert A.settings.name == "Ten-Da-Luu"
    B = await node_factory("B", hostname=True)
    assert B.name == node_mod.default_device_name(B.identity)
    await A.close()
    result = await node_mod.scan_devices(tmp_path / "A", timeout=0.1, mdns=MdnsOptions(advertise=False))
    assert result.name == "Ten-Da-Luu"


async def test_name_arg_not_persisted(node_factory, tmp_path):
    A = await node_factory("A", name="Tham-So")
    assert A.name == "Tham-So"
    assert A.settings.name is None
    await A.close()
    assert "name" not in on_disk(tmp_path / "A")


async def test_save_args_persists_name_before_first_advertise(node_factory, tmp_path, fake_mdns, monkeypatch):
    """Lần chạy đầu (D1): `save_args=True` lưu tên trước khi dựng `hello`, lưu thư mục nhận (gốc thật)
    sau khi mở được, cả hai trước lần quảng bá đầu tiên."""
    fake_factory = node_mod.MdnsDiscovery
    seen: list[dict] = []

    def factory(**kwargs):
        m = fake_factory(**kwargs)
        real_advertise = m.advertise

        async def advertise(**kw):
            seen.append({"name": kw.get("name"), "file": on_disk(tmp_path / "A")})
            await real_advertise(**kw)

        m.advertise = advertise
        return m

    monkeypatch.setattr(node_mod, "MdnsDiscovery", factory)
    recv = tmp_path / "nhan"
    A = await node_factory(
        "A",
        name="Ten-Moi",
        mdns=MdnsOptions(),
        node_kwargs={"save_args": True, "receive_dir": recv, "auto_accept": True},
    )
    assert seen, "node chưa quảng bá"
    first = seen[0]
    assert first["name"] == "Ten-Moi"
    assert first["file"].get("name") == "Ten-Moi"
    assert same_path(first["file"].get("receive_dir"), recv)
    assert first["file"]["receive_dir"] == str(A.transfers.receive_dir)  # L2: gốc thật
    assert A.settings.name == "Ten-Moi"


async def test_save_args_write_failure_fails_start_without_advertising(
    node_factory, tmp_path, fake_mdns, monkeypatch, app_helpers
):
    failing_save(monkeypatch)
    with pytest.raises(node_mod.SettingsSaveError):
        await node_factory("A", name="Ten-Moi", mdns=MdnsOptions(), node_kwargs={"save_args": True})
    assert all(not m.advertised for m in fake_mdns.instances)
    assert app_helpers.lock_is_free(tmp_path / "A")


async def test_set_name_new_hello_on_control_pairing_data(
    transfer_nodes, node_factory, raw_pairing_client, monkeypatch
):
    """`set_name`: trả tên đang dùng, lưu, rồi trong cùng khối đồng bộ đổi `hello` cho kết nối mới
    (Listener: ghép đôi/điều khiển vào; PairingService: ghép đôi khởi xướng; kết nối điều khiển ra;
    TransferManager: kết nối dữ liệu ra). Kết nối đang mở giữ `hello` cũ."""
    seen_data: list[tuple[object, str]] = []
    real_serve = transfers_mod.TransferManager.serve_data

    async def serve_data(self, conn):
        seen_data.append((self, conn.peer.hello.name))
        return await real_serve(self, conn)

    monkeypatch.setattr(transfers_mod.TransferManager, "serve_data", serve_data)
    net = await transfer_nodes(client="receiver")  # A mở điều khiển và dữ liệu tới B
    A, B = net.A, net.B
    old_hello = A.hello
    assert A.set_name("A-Moi") == "A-Moi"
    assert A.name == "A-Moi" and A.hello.name == "A-Moi"
    assert A.hello.device_id == old_hello.device_id and A.hello.os == old_hello.os
    assert on_disk(A.dir)["name"] == "A-Moi"
    assert B.control_connection(A.id).peer.hello.name == "A", "kết nối đang mở phải giữ hello cũ"

    peer = await raw_pairing_client(A.port)
    assert peer.server_hello["name"] == "A-Moi"

    # Kết nối điều khiển ra (A mở lại sau khi bị cắt) mang tên mới.
    # Chỉ có hint (không mDNS, không lượt nào giữ `hold`): mất kết nối thì đưa hint lại để A mở lại.
    A.control_connection(B.id).abort()
    await A.rec.wait_event(DeviceDisconnected, B.id, timeout=5)
    A.connect_hint(B.id, [LOCAL], B.port)
    await A.rec.wait_event(DeviceConnected, B.id, n=2, timeout=10)
    await wait_until(
        lambda: (c := B.control_connection(A.id)) is not None and c.peer.hello.name == "A-Moi",
        10,
        "B chưa thấy tên mới trên kết nối điều khiển mới",
    )

    # Kết nối dữ liệu (A mở tới B) mang tên mới.
    await send_and_finish(net)
    assert [name for tm, name in seen_data if tm is B.transfers] == ["A-Moi"]

    # Ghép đôi khởi xướng từ A mang tên mới.
    C = await node_factory("C", accept_pairing=True, answer=False)
    result = await asyncio.wait_for(A.pairing.pair_with([LOCAL], C.port), 15)
    assert C.rec.prompts and C.rec.prompts[0].peer_name == "A-Moi"
    assert str(result.outcome) != "paired"


async def test_set_name_same_name_only_saves(node_factory, tmp_path):
    A = await node_factory("A", name="Giu-Ten")
    before = A.hello
    assert A.set_name("Giu-Ten") == "Giu-Ten"
    assert A.hello is before, "trùng tên đang dùng thì không dựng hello mới"
    assert on_disk(tmp_path / "A")["name"] == "Giu-Ten"


@pytest.mark.parametrize("bad", ["", " dau", "x" * 65, 5, None])
async def test_set_name_rejects_invalid_name(node_factory, tmp_path, bad):
    A = await node_factory("A", name="Ten-Cu")
    with pytest.raises((MessageError, TypeError, ValueError)):
        A.set_name(bad)
    assert A.name == "Ten-Cu"
    assert "name" not in on_disk(tmp_path / "A")


async def test_set_name_save_failure_changes_nothing(
    node_factory, raw_pairing_client, fake_mdns, monkeypatch
):
    A = await node_factory("A", name="Ten-Cu", mdns=MdnsOptions())
    m = fake_mdns.of(A)
    await wait_until(lambda: len(m.advertised) >= 1, 5, "chưa quảng bá lần đầu")
    n_adv = len(m.advertised)
    before = A.hello
    failing_save(monkeypatch)
    with pytest.raises(node_mod.SettingsSaveError) as info:
        A.set_name("Ten-Moi")
    assert isinstance(info.value.__cause__, OSError)
    assert A.name == "Ten-Cu" and A.hello is before
    assert A.settings.name is None
    peer = await raw_pairing_client(A.port)
    assert peer.server_hello["name"] == "Ten-Cu"
    await asyncio.sleep(0.3)
    assert len(m.advertised) == n_adv
    assert all(a["name"] == "Ten-Cu" for a in m.advertised)


async def test_set_name_readvertises_with_new_name(node_factory, fake_mdns):
    A = await node_factory("A", name="Ten-Cu", mdns=MdnsOptions())
    m = fake_mdns.of(A)
    await wait_until(lambda: len(m.advertised) >= 1, 5, "chưa quảng bá lần đầu")
    A.set_name("Ten-Moi")
    await wait_until(
        lambda: any(a["name"] == "Ten-Moi" for a in m.advertised), 3, "chưa quảng bá lại tên mới trong 3 s"
    )


@pytest.mark.slow
@pytest.mark.mdns
async def test_set_name_txt_updated_within_3s(node_factory):
    """mDNS thật trên loopback: máy kia thấy tên mới (TXT) trong 3 s sau `set_name`."""

    def opts(n: int) -> MdnsOptions:
        return MdnsOptions(
            interfaces=(LOCAL,),
            advertise_addresses=(f"192.0.2.{n}",),
            probe_interval=1.0,
            resolve_timeout=0.5,
            probe_tick=0.2,
        )

    A = await node_factory("A", mdns=opts(31))
    B = await node_factory("B", name="B-Cu", mdns=opts(32))
    await A.rec.wait_event(node_mod.DeviceOnline, B.id, timeout=10)
    t0 = time.monotonic()
    B.set_name("B-Moi")

    def renamed() -> bool:
        return any(v.device_id == B.id and v.name == "B-Moi" for v in A.devices())

    await wait_until(renamed, 3.0, "A chưa thấy tên mới của B qua mDNS")
    assert time.monotonic() - t0 <= 3.0


async def test_setters_need_running_node(node_factory, tmp_path):
    A = await node_factory("A", start=False, node_kwargs={"auto_accept": True})
    with pytest.raises(RuntimeError):
        A.set_name("X")
    with pytest.raises(RuntimeError):
        A.set_rate_limit(None)
    with pytest.raises(RuntimeError):
        await A.set_receive_dir(tmp_path / "nhan")
    with pytest.raises(RuntimeError):
        _ = A.settings
    B = await node_factory("B")
    await B.close()
    for call in (
        lambda: B.set_name("X"),
        lambda: B.set_rate_limit(None),
        lambda: B.set_temp_retention_days(3),
        lambda: B.set_ui_preferences(autostart=True),
    ):
        with pytest.raises(RuntimeError):
            call()
    assert on_disk(tmp_path / "B") == {}


# =================================================================================================
# Thư mục nhận
# =================================================================================================


async def test_receive_dir_saved_sentinel_uses_settings_then_default(receiver, tmp_path, monkeypatch):
    from teleport import paths

    saved = tmp_path / "da-luu"
    write_settings(tmp_path / "A", {"version": 1, "receive_dir": str(saved)})
    A = await receiver("A", receive_dir=node_mod.ReceiveDirChoice.SAVED, auto_accept=True)
    assert same_path(A.transfers.receive_dir, saved)
    assert A.transfers.receiving is True
    assert A.receive_dir_error is None

    default = tmp_path / "mac-dinh" / "Teleport"
    monkeypatch.setattr(paths, "default_receive_dir", lambda: default)
    if hasattr(node_mod, "default_receive_dir"):
        monkeypatch.setattr(node_mod, "default_receive_dir", lambda: default)
    B = await receiver("B", receive_dir=node_mod.ReceiveDirChoice.SAVED, auto_accept=True)
    assert same_path(B.transfers.receive_dir, default)
    assert "receive_dir" not in on_disk(tmp_path / "B"), "thư mục mặc định không được lưu"


def test_saved_sentinel_without_way_to_ask_is_value_error(tmp_path):
    with pytest.raises(ValueError):
        node_mod.Node(tmp_path / "A", receive_dir=node_mod.ReceiveDirChoice.SAVED, auto_accept=False)


async def test_saved_dir_unavailable_starts_without_receiving(transfer_nodes, tmp_path, caplog):
    """H2: thư mục đã lưu không mở được → node vẫn chạy (gửi được), không nhận, WARNING,
    `receive_dir_error`; không tự rơi về thư mục mặc định; `settings.json` giữ nguyên."""
    caplog.set_level(logging.WARNING)
    bad = tmp_path / "la-file"
    bad.write_text("x", encoding="utf-8")
    write_settings(tmp_path / "A", {"version": 1, "receive_dir": str(bad)})
    net = await transfer_nodes({"receive_dir": node_mod.ReceiveDirChoice.SAVED})
    A, B = net.A, net.B
    assert isinstance(A.receive_dir_error, node_mod.ReceiveDirUnavailable)
    assert A.transfers.receiving is False
    assert A.transfers.receive_dir is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert bad.read_text(encoding="utf-8") == "x"
    assert on_disk(A.dir)["receive_dir"] == str(bad)
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 100)])
    fin = await B.trec.wait_finished(key.transfer_id, 15)
    assert str(fin.result) == "rejected_not_receiving"
    # Máy này vẫn gửi được (A gửi cho B: B không nhận, nhưng lượt đi tới máy kia).
    key2 = await A.transfers.send(B.id, [make_file(net.src, "g.bin", 100)])
    fin2 = await A.trec.wait_finished(key2.transfer_id, 15)
    assert str(fin2.result) == "rejected_not_receiving" and fin2.by_peer is True


async def test_explicit_dir_unavailable_still_fails_start(node_factory, tmp_path, app_helpers):
    bad = tmp_path / "la-file"
    bad.write_text("x", encoding="utf-8")
    with pytest.raises(node_mod.ReceiveDirUnavailable):
        await node_factory("A", node_kwargs={"receive_dir": bad, "auto_accept": True})
    assert app_helpers.lock_is_free(tmp_path / "A")


async def test_set_receive_dir_new_offer_lands_in_new_dir_and_old_closed(transfer_nodes, rd_spy, tmp_path):
    net = await transfer_nodes()
    A = net.A
    await send_and_finish(net, "truoc.bin")
    [old_rd] = rd_spy.of_root(net.receive_dir)
    await wait_not_busy(A)
    new = tmp_path / "nhan-moi"
    root = await A.set_receive_dir(new)
    assert isinstance(root, Path)
    assert same_path(root, new)
    assert A.transfers.receive_dir == root
    assert on_disk(A.dir)["receive_dir"] == str(root)  # L2
    assert A.settings.receive_dir == str(root)
    _, src = await send_and_finish(net, "sau.bin")
    assert (new / "sau.bin").read_bytes() == src.read_bytes()
    assert not (net.receive_dir / "sau.bin").exists()
    await wait_until(lambda: old_rd.closed, 10, "thư mục nhận cũ chưa được đóng")
    [new_rd] = rd_spy.of_root(new)
    assert not new_rd.closed
    # Windows: thư mục cũ không còn handle nào mở thì đổi tên được.
    moved = tmp_path / "nhan-cu-doi-ten"

    def renamed() -> bool:
        try:
            os.rename(net.receive_dir, moved)
        except OSError:
            return False
        return True

    await wait_until(renamed, 10, "không đổi tên được thư mục nhận cũ (còn handle mở)")


# C1c: "offer-cho-hoi" đã bỏ khỏi đây: `offer` chưa nhận không còn làm bận (spec mục 4.3), xem
# `test_change_dir_with_pending_offer_dialog_then_accept_lands_in_new_dir`.
BUSY_CASES = ["dang-nhan", "noi-lai", "khoi-phuc-cho-noi-lai", "dang-discard"]


@pytest.mark.slow
@pytest.mark.parametrize("case", BUSY_CASES)
async def test_set_receive_dir_busy(
    case,
    transfer_nodes,
    receiver,
    trust_each_other,
    record_for,
    resume_helpers,
    app_helpers,
    tmp_path,
    monkeypatch,
):
    """Còn lượt nhận đã chấp nhận (đang nhận, chờ nối lại, khôi phục), hàng đĩa còn sống hay đang
    `discard` → `ReceiveDirBusy`, không mở thư mục mới, không đổi gì (RAM lẫn `settings.json`)."""
    new = tmp_path / "nhan-moi"
    release = None
    if case in ("dang-nhan", "noi-lai"):
        net = await transfer_nodes({}, {"rate_limit": SLOW_RATE})
        A, B = net.A, net.B
        key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 24 * MiB)])
        akey = recv_key(net, key.transfer_id)
        await wait_status(A, akey, "transferring")
        if case == "noi-lai":

            async def unreachable(*args, **kwargs):
                raise connection.Unreachable("giả lập máy kia không tới được", address=LOCAL)

            monkeypatch.setattr(node_mod, "open_control", unreachable)
            B.control_connection(A.id).abort()
            await wait_status(A, akey, "reconnecting")
        old_root = A.transfers.receive_dir
    else:
        monkeypatch.setattr(
            core_transfer, "RECONNECT_GRACE", REAL_GRACE if case.startswith("khoi") else 600.0
        )
        _, id_c = trust_each_other("A", "C", name_b="C")
        a_id = app_helpers.ensure_identity(tmp_path / "A").device_id
        root = tmp_path / "nhan"
        if case == "khoi-phuc-cho-noi-lai":
            tid = uuid.uuid4().hex
            build_running(root, record_for, resume_helpers, tid, sender=id_c.device_id, receiver=a_id, age=50)
        else:
            gate = Gate()
            real_discard = storage.ReceiveDir.discard_temp

            def discard_temp(rd, transfer_id, **kw):
                gate.wait()
                return real_discard(rd, transfer_id, **kw)

            monkeypatch.setattr(storage.ReceiveDir, "discard_temp", discard_temp)
            release = gate.release.set
            old_tid, new_tid = uuid.uuid4().hex, uuid.uuid4().hex
            build_running(
                root, record_for, resume_helpers, old_tid, sender=id_c.device_id, receiver=a_id, age=7200
            )
            build_running(
                root, record_for, resume_helpers, new_tid, sender=id_c.device_id, receiver=a_id, age=3600
            )
        A = await receiver("A", receive_dir=root, auto_accept=True)
        if case == "khoi-phuc-cho-noi-lai":
            await A.trec.wait_kind("TransferResumed", tid)
            await wait_status(
                A,
                core_transfer.TransferKey(id_c.device_id, core_transfer.Direction.RECEIVE, tid),
                "reconnecting",
            )
        else:
            # Lượt mới nhất hết hạn ngay (tuổi > hạn nối lại) và bị dọn; lượt thừa còn kẹt ở `discard`.
            await A.trec.wait_finished(new_tid, 10)
            await wait_until(
                lambda: not (root / ".teleport" / new_tid).exists(), 10, "lượt hết hạn chưa bị dọn"
            )
            await wait_until(gate.hit.is_set, 10, "discard_temp chưa chạy")
            assert A.transfers.transfers() == ()
        old_root = A.transfers.receive_dir
    try:
        assert A.transfers.receive_dir_busy() is True
        before = on_disk(A.dir)
        with pytest.raises(node_mod.ReceiveDirBusy):
            await A.set_receive_dir(new)
        assert not new.exists(), "bận thì không được mở (tạo) thư mục mới"
        assert A.transfers.receive_dir == old_root
        assert on_disk(A.dir) == before
        assert A.transfers.receiving is True
    finally:
        if release is not None:
            release()
    if case == "dang-discard":
        await wait_not_busy(A)
        root2 = await A.set_receive_dir(new)
        assert same_path(root2, new)


# --- C1c: đổi thư mục khi chỉ còn `offer` chưa nhận ------------------------------------------------


class VolumeSeam:
    """Vá `ReceiveDir.volume_info`: chặn (`hold`) hay đổi kết quả (`set_free`) theo thư mục nhận.

    Mỗi lần gọi xong (kể cả lỗi) ghi mốc `("vi_done", khóa đường dẫn)` vào `spy` nếu có, để so thứ
    tự với `("close", rd)` của `RdSpy`.
    """

    def __init__(self, monkeypatch, spy: RdSpy | None = None) -> None:
        self.spy = spy
        self.holds: dict[str, Gate] = {}
        self.free: dict[str, int] = {}
        self.calls: list[str] = []
        real = storage.ReceiveDir.volume_info
        seam = self

        def volume_info(rd):
            key = RdSpy._key(rd.root)
            seam.calls.append(key)
            gate = seam.holds.get(key)
            if gate is not None:
                gate.wait()
            try:
                info = real(rd)
            finally:
                if seam.spy is not None:
                    seam.spy.add("vi_done", key)
            if key in seam.free:
                return storage.VolumeInfo(
                    fs_type=info.fs_type, max_file_size=info.max_file_size, free_bytes=seam.free[key]
                )
            return info

        monkeypatch.setattr(storage.ReceiveDir, "volume_info", volume_info)

    def hold(self, path) -> Gate:
        gate = Gate()
        self.holds[RdSpy._key(path)] = gate
        return gate

    def set_free(self, path, free: int) -> None:
        self.free[RdSpy._key(path)] = free

    def count(self, path) -> int:
        return self.calls.count(RdSpy._key(path))


async def test_change_dir_with_pending_offer_dialog_then_accept_lands_in_new_dir(
    transfer_nodes, rd_spy, tmp_path, monkeypatch
):
    """Hộp hỏi đang mở (`offer` chưa nhận) → đổi thư mục được, không hỏi lại; chỗ trống được kiểm lại
    trên thư mục mới; bấm Nhận → file nằm trong thư mục mới; thư mục cũ đóng."""
    vol = VolumeSeam(monkeypatch)
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    [old_rd] = rd_spy.of_root(net.receive_dir)
    src = make_file(net.src, "f.bin", 1000)
    key = await B.transfers.send(A.id, [src])
    offer = await A.trec.wait_kind("OfferReceived", key.transfer_id)
    assert A.transfers.receive_dir_busy() is False, "offer chưa nhận không làm bận"
    new = tmp_path / "nhan-moi"
    root = await A.set_receive_dir(new)
    assert same_path(root, new)
    assert on_disk(A.dir)["receive_dir"] == str(root)
    await wait_until(lambda: vol.count(new) >= 1, 10, "chưa kiểm lại chỗ trống trên thư mục mới")
    A.transfers.decide(offer.key, True)
    fin = await A.trec.wait_finished(key.transfer_id, 30)
    assert str(fin.result) == "done", fin
    assert (new / "f.bin").read_bytes() == src.read_bytes()
    assert not (net.receive_dir / "f.bin").exists()
    assert len(A.trec.of("OfferReceived", key.transfer_id)) == 1, "không được hỏi người dùng lần hai"
    await wait_until(lambda: old_rd.closed, 10, "thư mục nhận cũ chưa được đóng")
    [new_rd] = rd_spy.of_root(new)
    assert not new_rd.closed


async def test_change_dir_accept_while_rechecking_applied_after_check(
    transfer_nodes, rd_spy, tmp_path, monkeypatch
):
    """Bấm Nhận lúc lần kiểm trên thư mục mới chưa xong: chỉ được ghi nhận (chưa nhận gì), nhận ngay
    khi kết quả đủ chỗ về; file vào thư mục mới."""
    vol = VolumeSeam(monkeypatch)
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    src = make_file(net.src, "f.bin", 1000)
    key = await B.transfers.send(A.id, [src])
    offer = await A.trec.wait_kind("OfferReceived", key.transfer_id)
    new = tmp_path / "nhan-moi"
    gate = vol.hold(new)
    try:
        await A.set_receive_dir(new)
        await wait_until(gate.hit.is_set, 10, "chưa kiểm lại chỗ trống trên thư mục mới")
        A.transfers.decide(offer.key, True)
        await asyncio.sleep(0.3)
        assert A.trec.finished(key.transfer_id) is None
        assert "transferring" not in A.trec.statuses(key.transfer_id)
        assert not (new / ".teleport" / key.transfer_id).exists(), "chưa có kết quả kiểm thì chưa nhận"
    finally:
        gate.release.set()
    fin = await A.trec.wait_finished(key.transfer_id, 30)
    assert str(fin.result) == "done", fin
    assert (new / "f.bin").read_bytes() == src.read_bytes()
    assert len(A.trec.of("OfferReceived", key.transfer_id)) == 1


async def test_change_dir_with_pending_offer_new_dir_full_rejects(transfer_nodes, tmp_path, monkeypatch):
    """Thư mục mới không đủ chỗ cho `offer` đang hỏi → từ chối `no_space` như lần kiểm đầu (hộp hỏi tự
    đóng nhờ `TransferFinished`), máy gửi thấy `rejected_no_space`; Nhận bấm muộn không làm gì."""
    vol = VolumeSeam(monkeypatch)
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 1000)])
    offer = await A.trec.wait_kind("OfferReceived", key.transfer_id)
    new = tmp_path / "nhan-moi"
    vol.set_free(new, 10)
    await A.set_receive_dir(new)
    fin_a = await A.trec.wait_finished(key.transfer_id, 15)
    fin_b = await B.trec.wait_finished(key.transfer_id, 15)
    assert str(fin_a.result) == "rejected_no_space", fin_a
    assert str(fin_b.result) == "rejected_no_space" and fin_b.by_peer is True, fin_b
    A.transfers.decide(offer.key, True)
    await asyncio.sleep(0.2)
    assert not (new / ".teleport" / key.transfer_id).exists()
    assert len(A.trec.of("OfferReceived", key.transfer_id)) == 1


async def test_replace_dir_inside_event_after_deferred_accept_still_rechecks(
    transfer_nodes, tmp_path, monkeypatch
):
    """Trong `on_transfer_event` của `OfferReceived` (đang thực hiện hành động: lời gọi engine bị hoãn),
    gọi `decide(True)` rồi `replace_receive_dir(rd mới)`. Lần kiểm lại phải chạy **trước** `decide` đã
    hoãn, nên Nhận chỉ được ghi nhận; thư mục mới thiếu chỗ → lượt bị `reject{no_space}`, không tạo
    thư mục tạm ở đâu cả."""
    vol = VolumeSeam(monkeypatch)
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    new = tmp_path / "nhan-moi"
    vol.set_free(new, 10)
    new_rd = storage.ReceiveDir.open(new)
    errors: list[BaseException] = []
    fired: list[object] = []

    def hook(update) -> None:
        if type(update).__name__ != "OfferReceived" or fired:
            return
        fired.append(update)
        try:
            A.transfers.decide(update.key, True)
            A.transfers.replace_receive_dir(new_rd)
        except BaseException as e:  # báo ở luồng test, không nuốt
            errors.append(e)

    A.trec.hooks.append(hook)
    try:
        key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 1000)])
        fin_a = await A.trec.wait_finished(key.transfer_id, 15)
        fin_b = await B.trec.wait_finished(key.transfer_id, 15)
    finally:
        if errors and not new_rd.closed:
            new_rd.close()
    assert errors == [], errors
    assert same_path(A.transfers.receive_dir, new)
    assert str(fin_a.result) == "rejected_no_space", fin_a
    assert str(fin_b.result) == "rejected_no_space" and fin_b.by_peer is True, fin_b
    assert not (new / ".teleport" / key.transfer_id).exists(), "không được tạo thư mục tạm ở thư mục mới"
    assert not (net.receive_dir / ".teleport" / key.transfer_id).exists()
    assert len(A.trec.of("OfferReceived", key.transfer_id)) == 1


@pytest.mark.parametrize("case", ["chua-tao-tam", "dang-nhan"])
async def test_change_dir_with_accepted_receive_busy(case, transfer_nodes, tmp_path, monkeypatch):
    """Người dùng đã bấm Nhận (kể cả khi thư mục tạm chưa tạo xong) → bận: `ReceiveDirBusy`, không mở
    thư mục mới, không đổi gì."""
    release = None
    if case == "chua-tao-tam":
        gate = Gate()
        real_create = storage.ReceiveDir.create_temp

        def create_temp(rd, transfer_id, record):
            gate.wait()
            return real_create(rd, transfer_id, record)

        monkeypatch.setattr(storage.ReceiveDir, "create_temp", create_temp)
        release = gate.release.set
    net = await transfer_nodes({"auto_accept": False}, {"rate_limit": SLOW_RATE})
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 24 * MiB)])
    offer = await A.trec.wait_kind("OfferReceived", key.transfer_id)
    A.transfers.decide(offer.key, True)
    new = tmp_path / "nhan-moi"
    try:
        if case == "chua-tao-tam":
            await wait_until(gate.hit.is_set, 10, "create_temp chưa chạy")
        else:
            await wait_status(A, recv_key(net, key.transfer_id), "transferring")
        assert A.transfers.receive_dir_busy() is True
        before = on_disk(A.dir)
        with pytest.raises(node_mod.ReceiveDirBusy):
            await A.set_receive_dir(new)
        assert not new.exists(), "bận thì không được mở (tạo) thư mục mới"
        assert same_path(A.transfers.receive_dir, net.receive_dir)
        assert on_disk(A.dir) == before
    finally:
        if release is not None:
            release()
    A.transfers.cancel(recv_key(net, key.transfer_id))
    await A.trec.wait_finished(key.transfer_id, 30)


async def test_old_volume_info_job_does_not_close_new_dir(transfer_nodes, rd_spy, tmp_path, monkeypatch):
    """`volume_info` của thư mục cũ còn chạy lúc đổi (offer đang kiểm chỗ trống, chưa làm bận):
    thư mục cũ chỉ đóng sau khi job đó xong, thư mục mới không bị đóng; kết quả của nó (giả là hết chỗ)
    về **trước** kết quả kiểm trên thư mục mới mà vẫn bị bỏ (`check_id` cũ); offer được hỏi đúng một
    lần theo kết quả của thư mục mới."""
    vol = VolumeSeam(monkeypatch, rd_spy)
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    [old_rd] = rd_spy.of_root(net.receive_dir)
    old_gate = vol.hold(net.receive_dir)
    vol.set_free(net.receive_dir, 0)  # nếu kết quả cũ được dùng, offer bị từ chối `no_space`
    new = tmp_path / "nhan-moi"
    new_gate = vol.hold(new)
    src = make_file(net.src, "f.bin", 1000)
    key = await B.transfers.send(A.id, [src])
    try:
        await wait_until(old_gate.hit.is_set, 10, "chưa kiểm chỗ trống trên thư mục cũ")
        assert A.trec.of("OfferReceived", key.transfer_id) == []
        assert A.transfers.receive_dir_busy() is False, "offer đang kiểm chỗ trống không làm bận"
        root = await A.set_receive_dir(new)
        assert same_path(root, new)
        await wait_until(new_gate.hit.is_set, 10, "chưa kiểm lại chỗ trống trên thư mục mới")
        await asyncio.sleep(0.2)
        assert not old_rd.closed, "job volume_info của thư mục cũ chưa xong: chưa được đóng"
        [new_rd] = rd_spy.of_root(new)
        assert not new_rd.closed

        old_gate.release.set()  # kết quả cũ (hết chỗ) về trước, lúc offer vẫn đang chờ kết quả mới
        await wait_until(lambda: old_rd.closed, 10, "thư mục nhận cũ chưa được đóng sau khi job xong")
        old_key = RdSpy._key(net.receive_dir)
        assert rd_spy.index("vi_done", old_key) < rd_spy.index("close", old_rd), rd_spy.events
        await asyncio.sleep(0.3)  # cho kết quả cũ kịp tới engine
        assert A.trec.finished(key.transfer_id) is None, "kết quả kiểm cũ (hết chỗ) phải bị bỏ"
        assert A.trec.of("OfferReceived", key.transfer_id) == []
        assert not new_rd.closed
    finally:
        old_gate.release.set()
        new_gate.release.set()
    offer = await A.trec.wait_kind("OfferReceived", key.transfer_id)
    A.transfers.decide(offer.key, True)
    fin = await A.trec.wait_finished(key.transfer_id, 30)
    assert str(fin.result) == "done", fin
    assert (new / "f.bin").read_bytes() == src.read_bytes()
    assert len(A.trec.of("OfferReceived", key.transfer_id)) == 1
    assert not new_rd.closed


async def test_old_volume_info_job_timed_out_still_delays_close(
    transfer_nodes, rd_spy, tmp_path, monkeypatch
):
    """Như trên, nhưng lần kiểm trên thư mục cũ đã hết hạn chờ (`DISK_OP_TIMEOUT`) trong khi job
    `volume_info` của nó vẫn chạy trong thread: thư mục cũ vẫn chỉ đóng sau khi job đó `settled()`
    (job được tính vào việc chờ, không chỉ task đang chờ nó)."""
    vol = VolumeSeam(monkeypatch, rd_spy)
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    [old_rd] = rd_spy.of_root(net.receive_dir)
    gate = vol.hold(net.receive_dir)
    src = make_file(net.src, "f.bin", 1000)
    # Hạn ngắn chỉ trong lúc kiểm chỗ trống (job cũ bị chặn, job mới chạy ngay); trả lại trước `decide`.
    monkeypatch.setattr(transfers_mod, "DISK_OP_TIMEOUT", 1.0)
    key = await B.transfers.send(A.id, [src])
    new = tmp_path / "nhan-moi"
    try:
        await wait_until(gate.hit.is_set, 10, "chưa kiểm chỗ trống trên thư mục cũ")
        root = await A.set_receive_dir(new)
        assert same_path(root, new)
        offer = await A.trec.wait_kind("OfferReceived", key.transfer_id)
        await asyncio.sleep(1.5)  # hạn chờ của lần kiểm cũ đã qua; job vẫn kẹt ở cổng
        monkeypatch.setattr(transfers_mod, "DISK_OP_TIMEOUT", 60.0)
        assert not old_rd.closed, "job volume_info cũ còn chạy trong thread: chưa được đóng thư mục cũ"
        [new_rd] = rd_spy.of_root(new)
        assert not new_rd.closed
    finally:
        gate.release.set()
    await wait_until(lambda: old_rd.closed, 10, "thư mục nhận cũ chưa được đóng sau khi job xong")
    old_key = RdSpy._key(net.receive_dir)
    assert rd_spy.index("vi_done", old_key) < rd_spy.index("close", old_rd), rd_spy.events
    assert len(A.trec.of("OfferReceived", key.transfer_id)) == 1
    A.transfers.decide(offer.key, True)
    fin = await A.trec.wait_finished(key.transfer_id, 30)
    assert str(fin.result) == "done", fin
    assert (new / "f.bin").read_bytes() == src.read_bytes()
    assert not new_rd.closed


async def test_set_receive_dir_unavailable_keeps_old(transfer_nodes, tmp_path):
    net = await transfer_nodes()
    A = net.A
    bad = tmp_path / "la-file"
    bad.write_text("x", encoding="utf-8")
    with pytest.raises(node_mod.ReceiveDirUnavailable):
        await A.set_receive_dir(bad)
    assert same_path(A.transfers.receive_dir, net.receive_dir)
    assert "receive_dir" not in on_disk(A.dir)
    assert bad.read_text(encoding="utf-8") == "x"
    _, src = await send_and_finish(net)
    assert (net.receive_dir / "f.bin").read_bytes() == src.read_bytes()


@pytest.mark.parametrize("bad", [5, b"bytes-path", object()])
async def test_set_receive_dir_type_error(transfer_nodes, bad):
    net = await transfer_nodes()
    with pytest.raises(TypeError):
        await net.A.set_receive_dir(bad)
    assert same_path(net.A.transfers.receive_dir, net.receive_dir)


async def test_set_receive_dir_without_way_to_ask_is_value_error(node_factory, tmp_path):
    A = await node_factory("A")  # không thư mục nhận, không tự nhận, không on_transfer_event
    with pytest.raises(ValueError):
        await A.set_receive_dir(tmp_path / "nhan")
    assert not (tmp_path / "nhan").exists()


async def test_offer_during_open_rechecked_on_new_dir(transfer_nodes, rd_spy, tmp_path):
    """C1c (thay cho bản C1 “offer tới lúc đang mở → bận”): `offer` chưa nhận tới trong lúc đang mở
    thư mục mới không làm bận; đổi xong, offer được kiểm lại trên thư mục mới, không hỏi lần hai, và
    nhận vào thư mục mới."""
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    new = tmp_path / "nhan-moi"
    gate = rd_spy.hold(new)
    task = asyncio.create_task(A.set_receive_dir(new))
    try:
        await wait_until(gate.hit.is_set, 10, "set_receive_dir chưa mở thư mục mới")
        src = make_file(net.src, "f.bin", 1000)
        key = await B.transfers.send(A.id, [src])
        offer = await A.trec.wait_kind("OfferReceived", key.transfer_id)
    finally:
        gate.release.set()
    root = await task
    assert same_path(root, new)
    A.transfers.decide(offer.key, True)
    fin = await A.trec.wait_finished(key.transfer_id, 30)
    assert str(fin.result) == "done", fin
    assert (new / "f.bin").read_bytes() == src.read_bytes()
    assert not (net.receive_dir / "f.bin").exists()
    assert len(A.trec.of("OfferReceived", key.transfer_id)) == 1


async def test_accept_during_open_raises_busy_and_new_dir_closed(transfer_nodes, rd_spy, tmp_path):
    """Kiểm lại sau `await`: người dùng bấm Nhận (lượt nhận đã chấp nhận) trong lúc đang mở thư mục
    mới → `ReceiveDirBusy`, thư mục mới được đóng, không đổi gì; lượt đó nhận vào thư mục cũ."""
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    src = make_file(net.src, "f.bin", 1000)
    key = await B.transfers.send(A.id, [src])
    offer = await A.trec.wait_kind("OfferReceived", key.transfer_id)
    new = tmp_path / "nhan-moi"
    gate = rd_spy.hold(new)
    task = asyncio.create_task(A.set_receive_dir(new))
    try:
        await wait_until(gate.hit.is_set, 10, "set_receive_dir chưa mở thư mục mới")
        A.transfers.decide(offer.key, True)
        await wait_until(A.transfers.receive_dir_busy, 10, "lượt đã chấp nhận chưa làm bận")
    finally:
        gate.release.set()
    with pytest.raises(node_mod.ReceiveDirBusy):
        await task
    await wait_until(lambda: all(rd.closed for rd in rd_spy.of_root(new)), 10, "thư mục mới chưa được đóng")
    assert len(rd_spy.of_root(new)) == 1
    assert same_path(A.transfers.receive_dir, net.receive_dir)
    assert "receive_dir" not in on_disk(A.dir)
    fin = await A.trec.wait_finished(key.transfer_id, 30)
    assert str(fin.result) == "done"
    assert (net.receive_dir / "f.bin").read_bytes() == src.read_bytes()


async def test_setter_during_open_not_lost(transfer_nodes, rd_spy, tmp_path):
    """Setter đồng bộ chạy xen trong lúc `await` mở thư mục → vẫn còn trong file (dựng từ `_settings`
    hiện tại, không từ bản chụp trước `await`)."""
    net = await transfer_nodes()
    A = net.A
    new = tmp_path / "nhan-moi"
    gate = rd_spy.hold(new)
    task = asyncio.create_task(A.set_receive_dir(new))
    try:
        await wait_until(gate.hit.is_set, 10, "set_receive_dir chưa mở thư mục mới")
        A.set_rate_limit(2 * MIN_RATE)
        A.set_ui_preferences(language="vi_VN")
    finally:
        gate.release.set()
    root = await task
    data = on_disk(A.dir)
    assert data["rate_limit"] == 2 * MIN_RATE
    assert data["language"] == "vi_VN"
    assert data["receive_dir"] == str(root)
    assert A.settings.rate_limit == 2 * MIN_RATE and A.settings.receive_dir == str(root)


async def test_same_dir_is_noop(transfer_nodes, rd_spy):
    net = await transfer_nodes()
    A = net.A
    [old_rd] = rd_spy.of_root(net.receive_dir)
    await wait_not_busy(A)
    root = await A.set_receive_dir(net.receive_dir)
    assert root == A.transfers.receive_dir
    rds = rd_spy.of_root(net.receive_dir)
    extra = [rd for rd in rds if rd is not old_rd]
    await wait_until(lambda: all(rd.closed for rd in extra), 10, "ReceiveDir mở thêm chưa được đóng")
    assert not old_rd.closed
    assert on_disk(A.dir)["receive_dir"] == str(root), "đường dẫn tường minh chưa lưu thì phải lưu"
    _, src = await send_and_finish(net)
    assert (net.receive_dir / "f.bin").read_bytes() == src.read_bytes()


async def test_cancelled_during_open_changes_nothing(transfer_nodes, rd_spy, tmp_path):
    net = await transfer_nodes()
    A = net.A
    new = tmp_path / "nhan-moi"
    gate = rd_spy.hold(new)
    task = asyncio.create_task(A.set_receive_dir(new))
    try:
        await wait_until(gate.hit.is_set, 10, "set_receive_dir chưa mở thư mục mới")
        task.cancel()
        await asyncio.sleep(0)
    finally:
        gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_until(lambda: len(rd_spy.of_root(new)) == 1, 10, "việc mở thư mục mới chưa xong")
    await wait_until(lambda: rd_spy.of_root(new)[0].closed, 10, "kết quả mở tới muộn chưa được đóng")
    assert same_path(A.transfers.receive_dir, net.receive_dir)
    assert "receive_dir" not in on_disk(A.dir)
    await send_and_finish(net)


async def test_set_receive_dir_after_close_runtime_error(transfer_nodes, tmp_path):
    net = await transfer_nodes()
    await net.A.close()
    with pytest.raises(RuntimeError):
        await net.A.set_receive_dir(tmp_path / "nhan-moi")
    assert not (tmp_path / "nhan-moi").exists()


async def test_close_during_open_runtime_error_and_new_dir_closed(transfer_nodes, rd_spy, tmp_path):
    net = await transfer_nodes()
    A = net.A
    new = tmp_path / "nhan-moi"
    gate = rd_spy.hold(new)
    task = asyncio.create_task(A.set_receive_dir(new))
    await wait_until(gate.hit.is_set, 10, "set_receive_dir chưa mở thư mục mới")
    closing = asyncio.create_task(A.close())
    await asyncio.sleep(0.1)
    gate.release.set()
    with pytest.raises(RuntimeError):
        await task
    await closing
    await wait_until(lambda: all(rd.closed for rd in rd_spy.of_root(new)), 10, "thư mục mới chưa được đóng")
    assert "receive_dir" not in on_disk(A.dir)


async def test_set_receive_dir_save_failure_closes_new_and_keeps_old(
    transfer_nodes, rd_spy, tmp_path, monkeypatch
):
    net = await transfer_nodes()
    A = net.A
    await wait_not_busy(A)
    failing_save(monkeypatch)
    new = tmp_path / "nhan-moi"
    with pytest.raises(node_mod.SettingsSaveError):
        await A.set_receive_dir(new)
    await wait_until(lambda: all(rd.closed for rd in rd_spy.of_root(new)), 10, "thư mục mới chưa được đóng")
    assert same_path(A.transfers.receive_dir, net.receive_dir)
    assert A.settings.receive_dir is None


async def test_set_receive_dir_on_node_without_dir_enables_receiving(
    transfer_nodes, cleanup_spy, record_for, resume_helpers, tmp_path, monkeypatch
):
    """M1: node không có thư mục nhận → `set_receive_dir` bật nhận theo luật tạm dừng, hẹn dọn tạm
    trên thư mục mới, không `restore` lượt dở có sẵn trong đó (Q7)."""
    # Hạn nối lại thật: lượt dở 50 s tuổi còn hạn, nên lần dọn không được xóa nó (để kiểm “không restore”).
    monkeypatch.setattr(core_transfer, "RECONNECT_GRACE", REAL_GRACE)
    net = await transfer_nodes({"receive_dir": None, "auto_accept": True})
    A, B = net.A, net.B
    assert A.transfers.receiving is False
    new = tmp_path / "nhan-moi"
    tid = uuid.uuid4().hex
    leftover = build_running(new, record_for, resume_helpers, tid, sender=B.id, receiver=A.id, age=50)

    A.transfers.pause_receiving(True)
    root = await A.set_receive_dir(new)
    assert same_path(root, new)
    assert A.transfers.receiving is False, "đang tạm dừng thì vẫn không nhận"
    A.transfers.pause_receiving(False)
    assert A.transfers.receiving is True

    await wait_until(
        lambda: any(same_path(c.root, new) for c in cleanup_spy), 10, "chưa dọn tạm trên thư mục mới"
    )
    assert A.trec.of("TransferResumed") == []
    assert all(i.key.transfer_id != tid for i in A.transfers.transfers())
    assert leftover.exists()
    src = make_file(net.src, "f.bin", 1000)
    key = await B.transfers.send(A.id, [src])
    fin = await A.trec.wait_finished(key.transfer_id, 30)
    assert str(fin.result) == "done"
    assert (new / "f.bin").read_bytes() == src.read_bytes()


@pytest.mark.slow
async def test_restore_late_handle_closed_on_old_dir_after_change(
    receiver, trust_each_other, app_helpers, record_for, resume_helpers, rd_spy, tmp_path, monkeypatch
):
    """H1: `open_for_resume` của `restore` chậm hơn hạn (bị bỏ, kết quả tới muộn); đổi thư mục trong lúc
    nó còn chạy (không bị coi là bận) → handle tới muộn được đóng bằng `close_temp` của **thư mục cũ**,
    thư mục cũ chỉ đóng sau khi việc đó xong; thư mục mới không bị đụng."""
    monkeypatch.setattr(core_transfer, "RECONNECT_GRACE", REAL_GRACE)
    monkeypatch.setattr(transfers_mod, "DISK_SYNC_TIMEOUT", 0.5)
    _, id_c = trust_each_other("A", "C", name_b="C")
    a_id = app_helpers.ensure_identity(tmp_path / "A").device_id
    old = tmp_path / "nhan-cu"
    tid = uuid.uuid4().hex
    temp_dir = build_running(
        old, record_for, resume_helpers, tid, sender=id_c.device_id, receiver=a_id, age=50
    )

    gate = Gate()
    returned: dict[str, object] = {}
    failed: dict[str, BaseException] = {}  # lỗi trong thread đĩa: báo thẳng thay vì chờ hết hạn
    real_open = storage.ReceiveDir.open_for_resume

    def open_for_resume(rd, scan, *, stop=None):
        if scan.transfer_id != tid:
            return real_open(rd, scan, stop=stop)
        try:
            gate.wait()
            handle = real_open(rd, scan, stop=None)  # bỏ qua `stop`: trả handle thật, tới muộn
            returned[tid] = handle
            return handle
        except BaseException as e:
            failed[tid] = e
            raise
        finally:
            rd_spy.add("open_done", tid)

    monkeypatch.setattr(storage.ReceiveDir, "open_for_resume", open_for_resume)
    # `cleanup` đánh dấu từng lượt `_busy` trong lúc xét; chạy trùng lúc với việc mở lại tới muộn trên
    # thư mục cũ thì việc đó gặp NotResumableError “đang được dùng” (đã thấy trên CI chạy song song).
    # Ca này kiểm handle tới muộn *mở được* thì được đóng đúng chỗ, nên dọn tạm bỏ qua lượt này.
    real_cleanup = storage.ReceiveDir.cleanup

    def cleanup(rd, wall_now, *, own_device_id, skip, retention):
        return real_cleanup(rd, wall_now, own_device_id=own_device_id, skip=skip | {tid}, retention=retention)

    monkeypatch.setattr(storage.ReceiveDir, "cleanup", cleanup)
    new = tmp_path / "nhan-moi"
    try:
        A = await receiver("A", receive_dir=old, auto_accept=True)
        old_rd = rd_spy.of_root(old)[-1]  # cái đầu là của build_running (đã đóng)
        assert A.trec.of("TransferResumed") == []
        assert gate.hit.is_set() and tid not in returned
        root = await A.set_receive_dir(new)
        assert same_path(root, new)
    finally:
        gate.release.set()
    # Hạn bằng hạn của `Gate`: trên CI chạy song song (xdist) 10 s từng không đủ.
    await wait_until(lambda: tid in returned or tid in failed, 30, "việc mở lại treo chưa trả handle")
    assert tid not in failed, f"open_for_resume tới muộn lỗi: {failed[tid]!r}"
    handle = returned[tid]
    try:
        await wait_until(lambda: handle.temp.closed, 10, "handle tới muộn không được đóng")
        await wait_until(lambda: old_rd.closed, 10, "thư mục cũ chưa được đóng")
        build_rd = rd_spy.of_root(old)[0]  # build_running cũng gọi close_temp: bỏ qua
        closes = [e for e in rd_spy.events if e[0] == "close_temp" and e[2] == tid and e[1] is not build_rd]
        assert closes, rd_spy.events
        assert all(e[1] is old_rd for e in closes), "close_temp phải gọi trên thư mục cũ"
        assert rd_spy.index("open_done", tid) < rd_spy.index("close", old_rd), rd_spy.events
        assert rd_spy.index("close_temp", old_rd, tid) < rd_spy.index("close", old_rd), rd_spy.events
        [new_rd] = rd_spy.of_root(new)
        assert not new_rd.closed
        assert temp_dir.exists(), "lượt bị bỏ khi khôi phục phải để nguyên trên đĩa"
    finally:
        if not handle.temp.closed:
            handle.temp._close_quietly()


# =================================================================================================
# Hỏi trước khi nhận (theo thiết bị)
# =================================================================================================


async def connect_c(node_factory, app_helpers, A):
    trec = app_helpers.TransferRecorder("C")
    C = await node_factory("C", node_kwargs={"on_transfer_event": trec.on_update})
    C.trec = trec
    C.connect_hint(A.id, [LOCAL], A.port)
    await wait_until(
        lambda: A.id in C.connected_devices() and C.id in A.connected_devices(), 5, "C chưa nối A"
    )
    return C


async def test_ask_false_auto_accepts_only_that_device(transfer_nodes, node_factory, app_helpers):
    net = await transfer_nodes({"auto_accept": False}, extra_trusted=("C",))
    A, B = net.A, net.B
    C = await connect_c(node_factory, app_helpers, A)
    assert A.set_ask_before_receive(B.id, False) is None
    assert on_disk(A.dir)["devices"] == {B.id: {"ask_before_receive": False}}
    assert A.device_settings(B.id).ask_before_receive is False
    assert A.device_settings(C.id).ask_before_receive is True

    key_b = await B.transfers.send(A.id, [make_file(net.src, "b.bin", 1000)])
    offer_b = await A.trec.wait_kind("OfferReceived", key_b.transfer_id)
    assert offer_b.auto_accepted is True
    assert str((await A.trec.wait_finished(key_b.transfer_id)).result) == "done"

    key_c = await C.transfers.send(A.id, [make_file(net.src, "c.bin", 1000)])
    offer_c = await A.trec.wait_kind("OfferReceived", key_c.transfer_id)
    assert offer_c.auto_accepted is False
    await asyncio.sleep(0.3)
    assert A.trec.finished(key_c.transfer_id) is None
    assert str(A.transfers.get(offer_c.key).status) == "waiting"
    A.transfers.decide(offer_c.key, False)
    await A.trec.wait_finished(key_c.transfer_id)


async def test_set_ask_before_receive_errors_change_nothing(transfer_nodes, monkeypatch):
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    with pytest.raises(KeyError):
        A.set_ask_before_receive(uuid.uuid4().hex, False)  # đúng dạng nhưng không tin cậy
    with pytest.raises(MessageError):
        A.set_ask_before_receive("ABC", False)
    for bad in (0, 1, "false", None):
        with pytest.raises(TypeError):
            A.set_ask_before_receive(B.id, bad)
    assert on_disk(A.dir).get("devices", {}) == {}
    failing_save(monkeypatch)
    with pytest.raises(node_mod.SettingsSaveError):
        A.set_ask_before_receive(B.id, False)
    assert A.device_settings(B.id).ask_before_receive is True
    with pytest.raises(MessageError):
        A.device_settings("ABC")


async def test_yes_accepts_all_even_if_ask_true(transfer_nodes):
    net = await transfer_nodes()  # A: auto_accept=True (= receive --yes)
    A, B = net.A, net.B
    A.set_ask_before_receive(B.id, True)
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 1000)])
    offer = await A.trec.wait_kind("OfferReceived", key.transfer_id)
    assert offer.auto_accepted is True
    assert str((await A.trec.wait_finished(key.transfer_id)).result) == "done"


async def test_policy_change_does_not_decide_pending_offer(transfer_nodes):
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 1000)])
    offer = await A.trec.wait_kind("OfferReceived", key.transfer_id)
    assert offer.auto_accepted is False
    A.set_ask_before_receive(B.id, False)
    await asyncio.sleep(0.5)
    assert A.trec.finished(key.transfer_id) is None
    assert str(A.transfers.get(offer.key).status) == "waiting"
    A.transfers.decide(offer.key, True)
    assert str((await A.trec.wait_finished(key.transfer_id)).result) == "done"


async def test_ask_callable_error_means_ask(transfer_nodes, monkeypatch, caplog):
    """Chính sách hỏi ném lỗi → coi như hỏi (không tự nhận); log chỉ có `device_id[:8]`."""
    caplog.set_level(logging.DEBUG)
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    A.set_ask_before_receive(B.id, False)

    def boom(self, device_id):
        raise RuntimeError("giả lập lỗi đọc chính sách")

    # Không biết Node đọc chính sách qua đường nào: làm hỏng cả hai đường công khai.
    monkeypatch.setattr(node_mod.Node, "device_settings", boom)
    monkeypatch.setattr(settings_mod.Settings, "device", boom)
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 1000)])
    offer = await A.trec.wait_kind("OfferReceived", key.transfer_id)
    assert offer.auto_accepted is False
    for r in caplog.records:
        assert B.id not in r.getMessage()
    A.transfers.decide(offer.key, False)
    await A.trec.wait_finished(key.transfer_id)


# =================================================================================================
# A5: cài đặt theo thiết bị khi hủy ghép đôi / ghép đôi lại
# =================================================================================================


async def test_unpair_removes_device_entry_before_return(transfer_nodes):
    net = await transfer_nodes()
    A, B = net.A, net.B
    A.set_ask_before_receive(B.id, False)
    assert B.id in on_disk(A.dir)["devices"]
    await A.unpair(B.id)
    assert B.id not in on_disk(A.dir).get("devices", {})
    assert B.id not in A.settings.devices
    assert A.device_settings(B.id).ask_before_receive is True


async def test_unpair_settings_write_failure_still_unpairs(transfer_nodes, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    net = await transfer_nodes()
    A, B = net.A, net.B
    A.set_ask_before_receive(B.id, False)
    state = failing_save(monkeypatch)
    device = await A.unpair(B.id)
    assert device.device_id == B.id
    assert A.store.get(B.id) is None
    assert B.id not in A.connected_devices()
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert A.device_settings(B.id).ask_before_receive is True, "thiết bị không tin cậy: mặc định"
    assert B.id in on_disk(A.dir)["devices"]  # chưa ghi được
    state.fail = False
    A.set_rate_limit(2 * MIN_RATE)
    assert B.id not in on_disk(A.dir).get("devices", {}), "lần ghi sau phải bỏ (prune) mục đó"


async def test_unpair_offline_removes_device_entry(trust_each_other, tmp_path):
    _, id_b = trust_each_other("A", "B")
    write_settings(
        tmp_path / "A",
        {"version": 1, "name": "Giu-Ten", "devices": {id_b.device_id: {"ask_before_receive": False}}},
    )
    device = node_mod.unpair_offline(tmp_path / "A", "B", lock_timeout=2.0)
    assert device.device_id == id_b.device_id
    data = on_disk(tmp_path / "A")
    assert id_b.device_id not in data.get("devices", {})
    assert data["name"] == "Giu-Ten"


async def test_unpair_offline_settings_failure_warns_and_still_unpairs(
    trust_each_other, tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.WARNING)
    _, id_b = trust_each_other("A", "B")
    write_settings(tmp_path / "A", {"version": 1, "devices": {id_b.device_id: {"ask_before_receive": False}}})
    failing_save(monkeypatch)
    node_mod.unpair_offline(tmp_path / "A", "B", lock_timeout=2.0)
    from teleport.trust.store import TrustStore

    assert TrustStore.load(tmp_path / "A").get(id_b.device_id) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_repair_resets_device_settings(node_factory):
    B = await node_factory("B", accept_pairing=True, answer=True, node_kwargs={"auto_accept": True})
    A = await node_factory("A", answer=True, node_kwargs={"auto_accept": True})
    assert str((await asyncio.wait_for(A.pairing.pair_with([LOCAL], B.port), 15)).outcome) == "paired"
    await B.rec.wait_finished(1)
    A.set_ask_before_receive(B.id, False)
    B.set_ask_before_receive(A.id, False)
    assert str((await asyncio.wait_for(A.pairing.pair_with([LOCAL], B.port), 15)).outcome) == "paired"
    await B.rec.wait_finished(2)
    assert A.device_settings(B.id).ask_before_receive is True
    assert B.device_settings(A.id).ask_before_receive is True
    assert B.id not in on_disk(A.dir).get("devices", {})
    assert A.id not in on_disk(B.dir).get("devices", {})


async def test_first_pairing_clears_stale_entry_of_untrusted_device(node_factory, app_helpers, tmp_path):
    """Mục cũ trong file của một máy chưa tin cậy (ví dụ còn sót sau khi hủy ghép đôi lúc ghi lỗi)
    không bao giờ có hiệu lực: ghép đôi xong là về mặc định."""
    b_id = app_helpers.ensure_identity(tmp_path / "B").device_id
    write_settings(tmp_path / "A", {"version": 1, "devices": {b_id: {"ask_before_receive": False}}})
    B = await node_factory("B", accept_pairing=True, answer=True)
    A = await node_factory("A", answer=True, node_kwargs={"auto_accept": True})
    assert A.device_settings(b_id).ask_before_receive is True
    assert str((await asyncio.wait_for(A.pairing.pair_with([LOCAL], B.port), 15)).outcome) == "paired"
    assert A.device_settings(b_id).ask_before_receive is True
    assert b_id not in on_disk(A.dir).get("devices", {})


async def test_entry_for_untrusted_ignored_and_pruned(transfer_nodes, app_helpers, tmp_path):
    b_id = app_helpers.ensure_identity(tmp_path / "B").device_id
    stranger = uuid.uuid4().hex
    write_settings(
        tmp_path / "A",
        {
            "version": 1,
            "devices": {stranger: {"ask_before_receive": False}, b_id: {"ask_before_receive": False}},
        },
    )
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    assert A.device_settings(stranger).ask_before_receive is True
    assert A.device_settings(B.id).ask_before_receive is False
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 1000)])
    assert (await A.trec.wait_kind("OfferReceived", key.transfer_id)).auto_accepted is True
    A.set_rate_limit(2 * MIN_RATE)
    assert on_disk(A.dir)["devices"] == {B.id: {"ask_before_receive": False}}


# =================================================================================================
# Tạm dừng nhận
# =================================================================================================


async def test_pause_rejects_new_offer_not_receiving(transfer_nodes):
    net = await transfer_nodes()
    A, B = net.A, net.B
    assert A.transfers.receiving_paused is False
    assert A.transfers.pause_receiving(True) is None
    assert A.transfers.receiving_paused is True
    assert A.transfers.receiving is False
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 1000)])
    fin = await B.trec.wait_finished(key.transfer_id, 15)
    assert str(fin.result) == "rejected_not_receiving" and fin.by_peer is True
    await asyncio.sleep(0.2)
    assert A.trec.of("OfferReceived") == []
    assert "paused" not in json.dumps(on_disk(A.dir))


@pytest.mark.slow
async def test_pause_running_transfer_finishes_across_reconnect(transfer_nodes):
    net = await transfer_nodes(sender_opts={"rate_limit": SLOW_RATE})
    A, B = net.A, net.B
    src = make_file(net.src, "f.bin", 12 * MiB)
    key = await B.transfers.send(A.id, [src])
    await wait_status(A, recv_key(net, key.transfer_id), "transferring")
    A.transfers.pause_receiving(True)
    B.control_connection(A.id).abort()
    await B.rec.wait_event(DeviceDisconnected, A.id, timeout=5)
    await B.rec.wait_event(DeviceConnected, A.id, n=2, timeout=10)
    fin_b = await B.trec.wait_finished(key.transfer_id, 60)
    fin_a = await A.trec.wait_finished(key.transfer_id, 60)
    assert str(fin_a.result) == "done" and str(fin_b.result) == "done"
    assert (net.receive_dir / "f.bin").read_bytes() == src.read_bytes()
    assert A.transfers.receiving_paused is True


async def test_pause_pending_offer_still_decidable(transfer_nodes):
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 1000)])
    offer = await A.trec.wait_kind("OfferReceived", key.transfer_id)
    A.transfers.pause_receiving(True)
    A.transfers.decide(offer.key, True)
    assert str((await A.trec.wait_finished(key.transfer_id)).result) == "done"


async def test_pause_not_persisted_across_restart(receiver, tmp_path):
    A = await receiver("A", receive_dir=tmp_path / "nhan", auto_accept=True)
    A.transfers.pause_receiving(True)
    await A.close()
    A2 = await receiver("A", receive_dir=tmp_path / "nhan", auto_accept=True)
    assert A2.transfers.receiving_paused is False
    assert A2.transfers.receiving is True
    assert on_disk(tmp_path / "A") == {} or set(on_disk(tmp_path / "A")) <= {"version", "port"}


async def test_resume_receiving_accepts_again(transfer_nodes):
    net = await transfer_nodes()
    A = net.A
    A.transfers.pause_receiving(True)
    A.transfers.pause_receiving(False)
    assert A.transfers.receiving_paused is False and A.transfers.receiving is True
    await send_and_finish(net)


async def test_pause_type_checked_and_node_without_dir_stays_not_receiving(transfer_nodes):
    net = await transfer_nodes({"receive_dir": None})
    A = net.A
    A.transfers.pause_receiving(False)
    assert A.transfers.receiving is False, "không có thư mục nhận thì bỏ tạm dừng vẫn không nhận"
    with pytest.raises((TypeError, ValueError)):
        A.transfers.pause_receiving(1)


# =================================================================================================
# Dọn tạm, giới hạn tốc độ, `busy_devices`, tùy chọn giao diện
# =================================================================================================


async def test_retention_change_runs_one_cleanup_with_new_threshold(receiver, cleanup_spy, tmp_path):
    A = await receiver("A", receive_dir=tmp_path / "nhan", auto_accept=True)
    await wait_until(lambda: len(cleanup_spy) >= 1, 10, "chưa dọn tạm lần đầu")
    assert cleanup_spy[0].retention == storage.TEMP_RETENTION
    n0 = len(cleanup_spy)
    assert A.set_temp_retention_days(3) is None
    assert on_disk(A.dir)["temp_retention_days"] == 3
    assert A.settings.temp_retention_days == 3
    await wait_until(lambda: len(cleanup_spy) > n0, 10, "đổi ngưỡng mà chưa dọn lại")
    await asyncio.sleep(0.5)
    assert [c.retention for c in cleanup_spy[n0:]] == [3 * 86400.0]


async def test_retention_none_uses_storage_constant(receiver, cleanup_spy, tmp_path, monkeypatch):
    write_settings(tmp_path / "A", {"version": 1, "temp_retention_days": 5})
    A = await receiver("A", receive_dir=tmp_path / "nhan", auto_accept=True)
    await wait_until(lambda: len(cleanup_spy) >= 1, 10, "chưa dọn tạm lần đầu")
    assert cleanup_spy[0].retention == 5 * 86400.0, "ngưỡng đã lưu phải áp từ lúc khởi động"
    n0 = len(cleanup_spy)
    monkeypatch.setattr(storage, "TEMP_RETENTION", 1234.0)  # đọc lúc dùng
    A.set_temp_retention_days(None)
    assert "temp_retention_days" not in on_disk(A.dir)
    await wait_until(lambda: len(cleanup_spy) > n0, 10, "chưa dọn lại")
    assert cleanup_spy[-1].retention == 1234.0


@pytest.mark.parametrize("bad", [0, 366, -1, True, 1.5, "3"])
async def test_retention_invalid_changes_nothing(receiver, cleanup_spy, tmp_path, bad):
    A = await receiver("A", receive_dir=tmp_path / "nhan", auto_accept=True)
    await wait_until(lambda: len(cleanup_spy) >= 1, 10, "chưa dọn tạm lần đầu")
    n0 = len(cleanup_spy)
    with pytest.raises((ValueError, TypeError)):
        A.set_temp_retention_days(bad)
    await asyncio.sleep(0.3)
    assert len(cleanup_spy) == n0
    assert "temp_retention_days" not in on_disk(A.dir)


async def test_retention_on_node_without_dir_only_saves(node_factory, tmp_path):
    A = await node_factory("A")
    A.set_temp_retention_days(10)
    assert on_disk(tmp_path / "A")["temp_retention_days"] == 10


async def test_rate_limit_persisted_and_applied_cli_arg_not_persisted(node_factory, tmp_path, monkeypatch):
    write_settings(tmp_path / "A", {"version": 1, "rate_limit": 2 * MIN_RATE})
    write_settings(tmp_path / "B", {"version": 1, "rate_limit": 2 * MIN_RATE})
    A = await node_factory("A")  # rate_limit=None → theo cài đặt
    B = await node_factory("B", node_kwargs={"rate_limit": 3 * MIN_RATE})  # đối số của lần chạy này
    assert A.transfers.rate_limit == 2 * MIN_RATE
    assert B.transfers.rate_limit == 3 * MIN_RATE
    assert on_disk(B.dir)["rate_limit"] == 2 * MIN_RATE

    assert A.set_rate_limit(4 * MIN_RATE) is None
    assert A.transfers.rate_limit == 4 * MIN_RATE
    assert on_disk(A.dir)["rate_limit"] == 4 * MIN_RATE
    A.set_rate_limit(None)
    assert A.transfers.rate_limit is None
    assert "rate_limit" not in on_disk(A.dir)
    for bad in (MIN_RATE - 1, 0, True, 1.5, "65536", 2**53):
        with pytest.raises(ValueError):
            A.set_rate_limit(bad)
    assert A.transfers.rate_limit is None
    failing_save(monkeypatch)
    with pytest.raises(node_mod.SettingsSaveError):
        A.set_rate_limit(5 * MIN_RATE)
    assert A.transfers.rate_limit is None
    await B.close()
    assert on_disk(tmp_path / "B")["rate_limit"] == 2 * MIN_RATE


async def test_busy_devices_both_directions(transfer_nodes):
    net = await transfer_nodes({"auto_accept": False})
    A, B = net.A, net.B
    assert A.transfers.busy_devices() == frozenset() and B.transfers.busy_devices() == frozenset()
    key = await B.transfers.send(A.id, [make_file(net.src, "f.bin", 1000)])
    offer = await A.trec.wait_kind("OfferReceived", key.transfer_id)
    assert A.transfers.busy_devices() == frozenset({B.id})
    assert B.transfers.busy_devices() == frozenset({A.id})
    A.transfers.decide(offer.key, False)
    await wait_until(
        lambda: not A.transfers.busy_devices() and not B.transfers.busy_devices(),
        10,
        "busy_devices chưa rỗng sau khi lượt kết thúc",
    )


async def test_ui_preferences_validate_and_persist(node_factory, tmp_path, monkeypatch):
    A = await node_factory("A")
    assert (
        A.set_ui_preferences(language="vi_VN", autostart=True, first_run_done=True, radar_view="list") is None
    )
    s = A.settings
    assert (s.language, s.autostart, s.first_run_done, s.radar_view) == ("vi_VN", True, True, "list")
    data = on_disk(A.dir)
    assert (data["language"], data["autostart"], data["first_run_done"], data["radar_view"]) == (
        "vi_VN",
        True,
        True,
        "list",
    )
    A.set_ui_preferences(autostart=False)  # chỉ đổi khóa được truyền
    data = on_disk(A.dir)
    assert "autostart" not in data and data["language"] == "vi_VN" and data["radar_view"] == "list"
    A.set_ui_preferences(language=None, radar_view=None)
    data = on_disk(A.dir)
    assert "language" not in data and "radar_view" not in data and data["first_run_done"] is True

    for kwargs in (
        {"language": "vi-VN"},
        {"language": "vi_VN!"},
        {"radar_view": "grid"},
        {"autostart": 1},
        {"first_run_done": "yes"},
        {"language": "en", "radar_view": "grid"},  # một sai thì không lưu gì
    ):
        before = on_disk(A.dir)
        with pytest.raises((ValueError, TypeError)):
            A.set_ui_preferences(**kwargs)
        assert on_disk(A.dir) == before
        assert A.settings.language is None
    with pytest.raises(TypeError):
        A.set_ui_preferences(visible=False)  # không phải tùy chọn giao diện (C2)
    failing_save(monkeypatch)
    with pytest.raises(node_mod.SettingsSaveError):
        A.set_ui_preferences(language="en")
    assert A.settings.language is None


async def test_settings_property_is_immutable_snapshot(node_factory):
    import dataclasses

    A = await node_factory("A")
    s = A.settings
    assert isinstance(s, settings_mod.Settings)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.name = "X"  # type: ignore[misc]
    A.set_ui_preferences(autostart=True)
    assert s.autostart is False and A.settings.autostart is True


async def test_setters_do_not_touch_port_and_extra(node_factory, tmp_path):
    write_settings(tmp_path / "A", {"version": 1, "port": 47999, "tuong_lai": {"giu": 1}})
    A = await node_factory("A")  # port=0 tường minh: không dùng, không ghi cổng
    A.set_ui_preferences(first_run_done=True)
    data = on_disk(A.dir)
    assert data["port"] == 47999 and data["tuong_lai"] == {"giu": 1} and data["first_run_done"] is True


# =================================================================================================
# D6: `set_ui_preferences(..., theme=, accent=)` (spec-d6 mục 2.1, 5.1; mục 12 ý 9)
# =================================================================================================


async def test_set_ui_preferences_theme_accent_saved(node_factory):
    A = await node_factory("A")
    assert (A.settings.theme, A.settings.accent) == (None, None)
    assert A.set_ui_preferences(theme="dark", accent="ocean") is None
    assert (A.settings.theme, A.settings.accent) == ("dark", "ocean")
    data = on_disk(A.dir)
    assert (data["theme"], data["accent"]) == ("dark", "ocean")

    A.set_ui_preferences(theme="light")  # chỉ đổi khóa được truyền
    data = on_disk(A.dir)
    assert (data["theme"], data["accent"]) == ("light", "ocean")
    assert A.settings.accent == "ocean"

    A.set_ui_preferences(language="vi", theme=None)  # None = theo hệ điều hành: không ghi khóa
    data = on_disk(A.dir)
    assert "theme" not in data and data["accent"] == "ocean" and data["language"] == "vi"
    A.set_ui_preferences(accent=None)
    data = on_disk(A.dir)
    assert "accent" not in data and "theme" not in data
    assert (A.settings.theme, A.settings.accent) == (None, None)


async def test_set_ui_preferences_theme_keeps_unknown_accent(node_factory, tmp_path):
    """Mã màu nhấn đúng dạng nhưng bản này không biết (ví dụ do bản sau ghi): node giữ nguyên khi đổi
    trường khác, và cũng lưu được nếu được truyền vào (`app` chỉ kiểm dạng)."""
    write_settings(tmp_path / "A", {"version": 1, "accent": "tuong_lai_9"})
    A = await node_factory("A")
    assert A.settings.accent == "tuong_lai_9"
    A.set_ui_preferences(theme="light")
    data = on_disk(A.dir)
    assert (data["theme"], data["accent"]) == ("light", "tuong_lai_9")
    assert not (A.dir / "settings.json.bak").exists()
    A.set_ui_preferences(accent="khac_nua")
    assert on_disk(A.dir)["accent"] == "khac_nua"
    assert A.settings.accent == "khac_nua"


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"theme": "system"}, ValueError),
        ({"theme": "Dark"}, ValueError),
        ({"theme": ""}, ValueError),
        ({"theme": 1}, TypeError),
        ({"theme": True}, TypeError),
        ({"accent": "Ocean"}, ValueError),
        ({"accent": "ocean-blue"}, ValueError),
        ({"accent": "a" * 33}, ValueError),
        ({"accent": "ocean\n"}, ValueError),
        ({"accent": 5}, TypeError),
        ({"language": "en", "theme": "system"}, ValueError),  # một sai thì không lưu gì
        ({"theme": "dark", "accent": "Rose"}, ValueError),
        ({"autostart": True, "accent": 7}, TypeError),
    ],
    ids=lambda v: repr(v)[:30],
)
async def test_set_ui_preferences_invalid_theme_raises_no_write(node_factory, kwargs, error):
    A = await node_factory("A")
    A.set_ui_preferences(theme="light", accent="mint")
    before_disk = on_disk(A.dir)
    before = A.settings
    with pytest.raises(error):
        A.set_ui_preferences(**kwargs)
    assert on_disk(A.dir) == before_disk
    assert A.settings == before
    assert (A.settings.theme, A.settings.accent, A.settings.language) == ("light", "mint", None)


async def test_set_ui_preferences_save_error_no_change(node_factory, monkeypatch):
    A = await node_factory("A")
    A.set_ui_preferences(theme="dark", accent="violet")
    before_disk = on_disk(A.dir)
    state = failing_save(monkeypatch)
    with pytest.raises(node_mod.SettingsSaveError) as info:
        A.set_ui_preferences(theme="light", accent="rose")
    assert isinstance(info.value.__cause__, OSError)
    assert state.calls >= 1
    assert (A.settings.theme, A.settings.accent) == ("dark", "violet")
    assert on_disk(A.dir) == before_disk
    state.fail = False
    A.set_ui_preferences(theme="light", accent="rose")
    assert (A.settings.theme, A.settings.accent) == ("light", "rose")
