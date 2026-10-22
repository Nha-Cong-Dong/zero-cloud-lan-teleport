"""Test `teleport.app.node` theo đặc tả PR-3 mục 2.5, 3.5, 3.6, 3.8 và 4.6 (N1–N15, NU1–NU4).

Integration: Node thật trên 127.0.0.1 (fixture `node_factory`, hằng thời gian rút ngắn bằng `fast`).
Unit (không socket): `device_os`, `default_device_name`, `unpair_offline`, kiểm tham số hàm tạo.
"""

import asyncio
import contextlib
import json
import logging
import os
import platform
import time

import pytest

import teleport
from teleport import atomic
from teleport.app import node as node_mod
from teleport.app.devices import AmbiguousDevice, DeviceNotFound
from teleport.app.node import Node, default_device_name, device_os, unpair_offline
from teleport.app.settings import BACKUP_FILE, SETTINGS_FILE, Settings
from teleport.lock import NodeLock, NodeLockedError
from teleport.net.connection import CloseReason, Rejected, open_control
from teleport.trust.identity import IdentityError, load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore, TrustStoreError

pytestmark = [
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
integration = pytest.mark.integration

LOCAL = "127.0.0.1"
OS_NAMES = {"windows", "macos", "linux"}


async def wait_until(predicate, timeout: float = 2.0, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.01)


def lock_is_free(directory) -> bool:
    lock = NodeLock(directory)
    try:
        lock.acquire()
    except NodeLockedError:
        return False
    lock.release()
    return True


def test_constants_match_spec():
    assert node_mod.LOCK_TIMEOUT == 2.0
    assert node_mod.UNPAIR_CLOSE_TIMEOUT == 1.0
    assert node_mod.DEFAULT_PORT == 47474


# --- N1, N2, N13: vòng đời ------------------------------------------------------------------------


@integration
async def test_n1_lifecycle(node_factory, tmp_path, fast):
    A = await node_factory("A")
    assert isinstance(A.port, int) and A.port > 0
    assert A.name == "A"
    assert A.hello.name == "A"
    assert A.hello.os in OS_NAMES
    assert A.hello.device_id == A.identity.device_id
    assert A.hello.app_version == teleport.__version__
    assert A.pairing.accepting is False
    assert A.connected_devices() == frozenset()

    second = Node(A.dir, name="A2", host=LOCAL, port=0)
    t0 = time.monotonic()
    with pytest.raises(NodeLockedError):
        await second.start()
    waited = time.monotonic() - t0
    assert 0.8 * fast.LOCK_TIMEOUT <= waited <= fast.LOCK_TIMEOUT + 1, waited
    await second.close()  # sau khi start thất bại: không làm gì
    with pytest.raises(RuntimeError):
        await second.start()  # start sau khi thất bại → RuntimeError

    with pytest.raises(RuntimeError):
        await A.start()  # start lần hai
    await A.close()
    assert lock_is_free(A.dir)
    await A.close()  # gọi lần hai không lỗi
    with pytest.raises(RuntimeError):
        await A.start()  # start sau close

    async with Node(tmp_path / "ctx", name="Ctx", host=LOCAL, port=0) as n:
        assert n.port > 0
        assert not lock_is_free(tmp_path / "ctx")
    assert lock_is_free(tmp_path / "ctx")


@integration
async def test_concurrent_close_calls(node_factory):
    A = await node_factory("A")
    await asyncio.wait_for(asyncio.gather(A.close(), A.close(), A.close()), 10)
    assert lock_is_free(A.dir)


async def test_properties_before_start_raise(tmp_path):
    node = Node(tmp_path / "A", name="A", host=LOCAL, port=0)
    for prop in ("identity", "store", "hello", "pairing", "name"):
        with pytest.raises(RuntimeError):
            getattr(node, prop)
    assert node.port is None
    with pytest.raises(RuntimeError):
        await node.unpair("0" * 32)  # N9: gọi trước start
    await node.close()  # N13: close trước start không lỗi
    await node.close()
    assert not (tmp_path / "A" / "node.lock").exists() or lock_is_free(tmp_path / "A")


@integration
async def test_n2_corrupt_identity_releases_lock(tmp_path, fast):
    d = tmp_path / "A"
    load_or_create_identity(d)
    (d / "device.crt").write_bytes(b"rac")
    node = Node(d, name="A", host=LOCAL, port=0)
    with pytest.raises(IdentityError):
        await node.start()
    assert lock_is_free(d)
    await node.close()  # N13
    with pytest.raises(RuntimeError):
        await node.start()


@integration
async def test_n2_corrupt_trust_store_releases_lock(tmp_path, fast):
    d = tmp_path / "A"
    load_or_create_identity(d)
    (d / "trusted.json").write_bytes(b"{khong phai json")
    node = Node(d, name="A", host=LOCAL, port=0)
    with pytest.raises(TrustStoreError):
        await node.start()
    assert lock_is_free(d)
    assert (d / "trusted.json").read_bytes() == b"{khong phai json"
    await node.close()


# --- N3: cổng ---------------------------------------------------------------------------------------


@integration
async def test_n3a_saved_free_port_is_used_and_file_unchanged(node_factory, tmp_path, app_helpers):
    d = tmp_path / "A"
    port = app_helpers.free_port()
    Settings(port=port).save(d)
    raw = (d / SETTINGS_FILE).read_bytes()
    A = await node_factory("A", port=None)
    assert A.port == port
    assert (d / SETTINGS_FILE).read_bytes() == raw


@integration
async def test_n3b_saved_port_busy_falls_back_and_saves(node_factory, tmp_path, app_helpers, caplog):
    caplog.set_level(logging.DEBUG)
    d = tmp_path / "A"
    with app_helpers.busy_port() as busy:
        Settings(port=busy, extra={"khac": [1]}).save(d)
        A = await node_factory("A", port=None)
        assert A.port != busy and A.port > 0
    saved = json.loads((d / SETTINGS_FILE).read_text(encoding="utf-8"))
    assert saved["port"] == A.port
    assert saved["khac"] == [1]  # trường lạ được giữ
    assert any(r.levelno == logging.WARNING and r.name.startswith("teleport.app") for r in caplog.records)
    assert not (d / BACKUP_FILE).exists()


@integration
async def test_n3c_default_port_busy_falls_back_and_saves(node_factory, tmp_path, app_helpers, monkeypatch):
    d = tmp_path / "A"
    with app_helpers.busy_port() as busy:
        monkeypatch.setattr(node_mod, "DEFAULT_PORT", busy)
        A = await node_factory("A", port=None)
        assert A.port != busy
    assert Settings.load(d).port == A.port


@integration
async def test_n3_default_port_free_is_used_and_not_saved(node_factory, tmp_path, app_helpers, monkeypatch):
    """Mục 0, điểm 3: chỉ ghi settings.json khi đã phải dùng cổng dự phòng."""
    port = app_helpers.free_port()
    monkeypatch.setattr(node_mod, "DEFAULT_PORT", port)
    A = await node_factory("A", port=None)
    assert A.port == port
    assert not (tmp_path / "A" / SETTINGS_FILE).exists()


@integration
async def test_n3d_explicit_port_zero_never_writes_settings(node_factory, tmp_path):
    A = await node_factory("A", port=0)
    assert A.port > 0
    assert not (tmp_path / "A" / SETTINGS_FILE).exists()


@integration
async def test_n3e_explicit_busy_port_raises_and_releases_lock(tmp_path, app_helpers, fast):
    d = tmp_path / "A"
    with app_helpers.busy_port() as busy:
        node = Node(d, name="A", host=LOCAL, port=busy)
        with pytest.raises(OSError):
            await node.start()
    assert lock_is_free(d)
    assert not (d / SETTINGS_FILE).exists()
    await node.close()


@integration
async def test_n3f_corrupt_settings_and_busy_port_creates_backup(
    node_factory, tmp_path, app_helpers, monkeypatch
):
    d = tmp_path / "A"
    d.mkdir()
    raw = b'{"port": "khong phai so", "x": ['
    (d / SETTINGS_FILE).write_bytes(raw)
    with app_helpers.busy_port() as busy:
        monkeypatch.setattr(node_mod, "DEFAULT_PORT", busy)
        A = await node_factory("A", port=None)
    assert (d / BACKUP_FILE).read_bytes() == raw
    assert Settings.load(d).port == A.port


@integration
async def test_settings_save_failure_does_not_stop_node(
    node_factory, tmp_path, app_helpers, monkeypatch, caplog
):
    """3.5 bước 3: OSError khi lưu cổng → WARNING, node vẫn chạy."""
    caplog.set_level(logging.DEBUG)
    from teleport.app import settings as settings_mod

    def failing(path, content):
        raise PermissionError(13, "giả lập không ghi được")

    monkeypatch.setattr(atomic, "write_atomic", failing)
    if hasattr(settings_mod, "write_atomic"):
        monkeypatch.setattr(settings_mod, "write_atomic", failing)
    with app_helpers.busy_port() as busy:
        monkeypatch.setattr(node_mod, "DEFAULT_PORT", busy)
        A = await node_factory("A", port=None)
    assert A.port > 0
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@integration
@pytest.mark.parametrize("depth", [1500, 2000, 3000, 5000])
async def test_deeply_nested_settings_and_busy_port_still_start(
    node_factory, tmp_path, app_helpers, monkeypatch, caplog, depth
):
    """Sửa F8 (review PR-3): `settings.json` lồng rất sâu + cổng ưu tiên bận → node vẫn chạy.

    Trên Python 3.12, `json` đọc được ~2000 tầng nhưng ghi lại thì `RecursionError`, làm
    `Node.start` hỏng khi lưu cổng dự phòng. Đặc tả cho hai cách sửa (bắt lỗi khi lưu, hoặc coi
    file quá sâu là hỏng); test chỉ đòi kết quả: start thành công trên cổng dự phòng, có WARNING.
    (Trên 3.14 `json` không lỗi ở các độ sâu này, nên test chỉ bắt được lỗi cũ trên 3.12.)
    """
    caplog.set_level(logging.DEBUG)
    d = tmp_path / "A"
    d.mkdir()
    nested = "[" * depth + "]" * depth
    (d / SETTINGS_FILE).write_text('{"version": 1, "x": ' + nested + "}", encoding="utf-8")
    with app_helpers.busy_port() as busy:
        monkeypatch.setattr(node_mod, "DEFAULT_PORT", busy)
        A = await node_factory("A", port=None)
        assert A.port != busy and A.port > 0
    assert any(r.levelno == logging.WARNING and r.name.startswith("teleport.app") for r in caplog.records)
    assert not lock_is_free(d)  # node đang chạy giữ khóa


@pytest.mark.parametrize("port", [-1, 65536, True, False, "47474", 1.5])
def test_constructor_rejects_bad_port(tmp_path, port):
    with pytest.raises(ValueError):
        Node(tmp_path / "A", name="A", port=port)
    assert not (tmp_path / "A").exists()


@pytest.mark.parametrize("host", ["localhost", "::1", "", "127.0.0.01"])
async def test_bad_host_is_rejected_at_start(tmp_path, fast, host):
    """`host` kiểm như `Listener.start`, lúc `start` → ValueError; khóa được nhả."""
    node = Node(tmp_path / "A", name="A", host=host, port=0)
    with pytest.raises(ValueError):
        await node.start()
    assert lock_is_free(tmp_path / "A")
    await node.close()


# --- N4: tên ----------------------------------------------------------------------------------------


@integration
async def test_n4_vietnamese_name(node_factory):
    A = await node_factory("A", name="PC Nhà")
    assert A.hello.name == "PC Nhà"
    assert A.name == "PC Nhà"


@pytest.mark.parametrize("name", ["  x", "x ", "a‮b", "", "x" * 65, "a\nb", "a\x00b"])
def test_n4_invalid_name_raises_in_constructor(tmp_path, name):
    with pytest.raises(ValueError):
        Node(tmp_path / "A", name=name, host=LOCAL, port=0)
    assert not (tmp_path / "A").exists()


def test_n4_name_of_64_chars_is_valid(tmp_path):
    Node(tmp_path / "A", name="x" * 64, host=LOCAL, port=0)


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("MacBook-Pro.local", "MacBook-Pro"),
        ("MacBook-Pro.LOCAL", "MacBook-Pro"),
        ("  pc-nha  ", "pc-nha"),
        ("DESKTOP-ABC123", "DESKTOP-ABC123"),
    ],
)
def test_default_device_name_uses_hostname(tmp_path, monkeypatch, hostname, expected):
    identity = load_or_create_identity(tmp_path / "id")
    monkeypatch.setattr(platform, "node", lambda: hostname)
    assert default_device_name(identity) == expected


@pytest.mark.parametrize("hostname", ["", "   ", "x" * 100, "a\x00b", ".local", "a‮b"])
def test_default_device_name_falls_back_to_stable_name(tmp_path, monkeypatch, hostname):
    identity = load_or_create_identity(tmp_path / "id")
    monkeypatch.setattr(platform, "node", lambda: hostname)
    expected = "Teleport-" + identity.device_id[:4]
    assert default_device_name(identity) == expected
    assert default_device_name(identity) == expected  # ổn định, không ngẫu nhiên


@integration
async def test_n4_node_without_name_uses_hostname(node_factory, monkeypatch):
    monkeypatch.setattr(platform, "node", lambda: "MacBook-Pro.local")
    A = await node_factory("A", hostname=True)
    assert A.hello.name == "MacBook-Pro"


@integration
async def test_n4_node_with_bad_hostname_uses_fallback(node_factory, monkeypatch):
    monkeypatch.setattr(platform, "node", lambda: "x" * 100)
    A = await node_factory("A", hostname=True)
    assert A.hello.name == "Teleport-" + A.id[:4]


# --- N6, N7, N11: kết nối điều khiển ------------------------------------------------------------------


@integration
async def test_n6_control_connection_registered_and_removed(node_factory, trust_each_other, control_client):
    trust_each_other("A", "B")
    A = await node_factory("A")
    B = await node_factory("B")
    cc = await control_client(B, A)
    await wait_until(lambda: B.id in A.connected_devices(), 1.0, "A chưa ghi nhận B")
    assert A.connected_devices() == frozenset({B.id})
    await wait_until(lambda: cc.pongs() >= 2, 3, "chưa trao đủ ping/pong")

    await cc.conn.close()  # B đóng
    await wait_until(lambda: B.id not in A.connected_devices(), 1.0, "A chưa xóa B")
    assert A.connected_devices() == frozenset()


@integration
async def test_n7_untrusted_device_is_rejected(node_factory):
    A = await node_factory("A")
    B = await node_factory("B")
    with pytest.raises(Rejected):
        await open_control([LOCAL], A.port, B.identity, B.hello, A.identity.fingerprint)
    assert A.connected_devices() == frozenset()


@integration
async def test_n11_second_connection_replaces_first(node_factory, trust_each_other, control_client):
    trust_each_other("A", "B")
    A = await node_factory("A")
    B = await node_factory("B")
    c1 = await control_client(B, A)
    await wait_until(lambda: B.id in A.connected_devices(), 1.0)
    c2 = await control_client(B, A)
    await wait_until(lambda: c1.task.done(), 1.0, "kết nối cũ chưa bị đóng")
    await asyncio.sleep(0.2)
    # Kết nối cũ kết thúc không được xóa mục của kết nối mới.
    assert A.connected_devices() == frozenset({B.id})
    n = c2.pongs()
    await wait_until(lambda: c2.pongs() >= n + 2, 3, "kết nối mới không trao ping/pong")
    assert not c2.task.done()
    await c2.conn.close()
    await wait_until(lambda: B.id not in A.connected_devices(), 1.0)


@integration
async def test_many_control_connections_keep_at_most_one(node_factory, trust_each_other, control_client):
    """3.8: tối đa một kết nối điều khiển được ghi nhận cho mỗi device_id."""
    trust_each_other("A", "B")
    A = await node_factory("A")
    B = await node_factory("B")
    clients = [await control_client(B, A, ping_interval=5.0, dead_after=10.0) for _ in range(5)]
    await wait_until(lambda: sum(not c.task.done() for c in clients) == 1, 2, "còn nhiều hơn 1 kết nối sống")
    assert not clients[-1].task.done()
    assert A.connected_devices() == frozenset({B.id})


# --- N8–N10: hủy ghép đôi ---------------------------------------------------------------------------


@integration
async def test_n8_unpair_closes_connection_fast(node_factory, trust_each_other, control_client):
    trust_each_other("A", "B")
    trust_each_other("A", "C", name_b="C")
    A = await node_factory("A")
    B = await node_factory("B")
    C = await node_factory("C")
    cb = await control_client(B, A)
    cc = await control_client(C, A)
    await wait_until(lambda: A.connected_devices() == frozenset({B.id, C.id}), 1.0)

    t0 = time.monotonic()
    device = await A.unpair(B.id)
    assert device.device_id == B.id
    assert device.name == "B"
    assert device.fingerprint == B.identity.fingerprint
    assert B.id not in A.connected_devices()
    await wait_until(lambda: cb.task.done(), max(0.0, 1.0 - (time.monotonic() - t0)), "B chưa thấy EOF")
    assert time.monotonic() - t0 < 1.0
    assert cb.task.result() in (CloseReason.PEER_CLOSED, CloseReason.CONNECTION_LOST)

    # C không bị ảnh hưởng.
    assert C.id in A.connected_devices()
    n = cc.pongs()
    await wait_until(lambda: cc.pongs() >= n + 2, 3, "kết nối của C ngừng trao ping/pong")

    assert A.store.get(B.id) is None
    assert TrustStore.load(A.dir).get(B.id) is None
    assert TrustStore.load(A.dir).get(C.id) is not None
    with pytest.raises(Rejected):
        await open_control([LOCAL], A.port, B.identity, B.hello, A.identity.fingerprint)
    assert B.id not in A.connected_devices()


@integration
async def test_unpair_synchronous_part_runs_before_first_await(
    node_factory, trust_each_other, control_client
):
    """2.5/3.6: `store.remove` → đổi context → lấy kết nối ra khỏi bảng chạy liền, không await xen."""
    trust_each_other("A", "B")
    A = await node_factory("A")
    B = await node_factory("B")
    await control_client(B, A)
    await wait_until(lambda: B.id in A.connected_devices(), 1.0)
    task = asyncio.create_task(A.unpair(B.id), name="test.unpair")
    try:
        await asyncio.sleep(0)  # chạy task tới await đầu tiên
        assert A.store.get(B.id) is None
        assert B.id not in A.connected_devices()
    finally:
        await asyncio.wait_for(task, 3)


@integration
async def test_n9_unpair_unknown_id_raises_key_error(node_factory, trust_each_other):
    trust_each_other("A", "B")
    A = await node_factory("A")
    before = (A.dir / "trusted.json").read_bytes()
    with pytest.raises(KeyError):
        await A.unpair("f" * 32)
    assert (A.dir / "trusted.json").read_bytes() == before
    assert len(A.store.devices()) == 1


@integration
async def test_unpair_store_write_failure_changes_nothing(
    node_factory, trust_each_other, control_client, monkeypatch
):
    """3.6 bước 1: TrustStoreError bay ra, không đổi gì (kết nối vẫn còn)."""
    trust_each_other("A", "B")
    A = await node_factory("A")
    B = await node_factory("B")
    cb = await control_client(B, A)
    await wait_until(lambda: B.id in A.connected_devices(), 1.0)
    monkeypatch.setattr(atomic, "REPLACE_DELAY", 0.001)
    real_replace = os.replace

    def locked(src, dst, *args, **kwargs):
        if os.path.basename(os.fspath(dst)) == "trusted.json":
            raise PermissionError(13, "giả lập file bị khóa")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(TrustStoreError):
        await A.unpair(B.id)
    monkeypatch.setattr(os, "replace", real_replace)
    assert A.store.get(B.id) is not None
    assert B.id in A.connected_devices()
    await asyncio.sleep(0.2)
    assert not cb.task.done()


@integration
async def test_n10_unpair_races_with_handshake(
    node_factory, trust_each_other, raw_client, app_helpers, tmp_path
):
    _id_a, id_b = trust_each_other("A", "B")
    A = await node_factory("A")
    peer = await raw_client(A.port, id_b)
    await peer.read_hello()
    peer.send_dict(app_helpers.hello_dict(id_b, "B"))
    await peer.drain()
    await asyncio.sleep(0.1)  # server đã bắt tay xong và đang chờ message mở đầu

    seen: list[bool] = []

    async def watch():
        while True:
            if id_b.device_id in A.connected_devices():
                seen.append(True)
            await asyncio.sleep(0.002)

    watcher = asyncio.create_task(watch(), name="test.watch")
    try:
        await A.unpair(id_b.device_id)
        peer.send_dict({"type": "control_open"})
        await peer.drain()
        await peer.expect_closed(2.0)
        await asyncio.sleep(0.1)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    assert seen == []
    assert id_b.device_id not in A.connected_devices()


# --- N12: đóng khi đang bận ---------------------------------------------------------------------------


@integration
async def test_n12_close_while_busy(node_factory, trust_each_other, control_client, raw_client):
    trust_each_other("A", "B")
    trust_each_other("A", "C", name_b="C")
    A = await node_factory("A", accept_pairing=True)
    B = await node_factory("B")
    C = await node_factory("C")
    P = await node_factory("P")
    cb = await control_client(B, A)
    cc = await control_client(C, A)
    await wait_until(lambda: A.connected_devices() == frozenset({B.id, C.id}), 1.0)
    raw = await raw_client(A.port, use_tls=False)  # TCP thô đang mở đầu
    task = asyncio.create_task(P.pairing.pair_with([LOCAL], A.port), name="test.pair")
    await P.rec.wait_prompts()
    await A.rec.wait_prompts()

    t0 = time.monotonic()
    await asyncio.wait_for(A.close(), 10)
    assert time.monotonic() - t0 <= 6.0
    assert lock_is_free(A.dir)

    result = await asyncio.wait_for(task, 3)
    assert result.outcome == "peer_rejected"
    assert result.peer_reason == "user"
    assert [str(f.outcome) for f in A.rec.finished] == ["cancelled"]
    await wait_until(lambda: cb.task.done() and cc.task.done(), 2, "client điều khiển chưa thấy EOF")
    await raw.expect_closed(2.0)
    assert A.connected_devices() == frozenset()


# --- Sửa F3: close() bị hủy giữa chừng ----------------------------------------------------------------

_NODE_TASK_PREFIXES = ("teleport.node", "teleport.pairing", "teleport.connection")


def _node_tasks() -> list[str]:
    return [
        t.get_name()
        for t in asyncio.all_tasks()
        if not t.done() and t.get_name().startswith(_NODE_TASK_PREFIXES)
    ]


async def _assert_port_not_accepting(port: int) -> None:
    """Cổng không còn nhận kết nối: TCP bị từ chối, hoặc nếu lọt vào thì bị đóng ngay (≤ 1 s).

    Listener còn sống thì giữ kết nối TCP trần tới hết thời gian mở đầu (2 s với `fast`).
    """
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(LOCAL, port), 5)
    except (OSError, TimeoutError):
        return  # bị từ chối (Windows mất ~2 s mới báo)
    try:
        async with asyncio.timeout(1.0):
            while True:
                try:
                    chunk = await reader.read(1024)
                except (ConnectionError, OSError):
                    return
                if not chunk:
                    return
    except TimeoutError:
        raise AssertionError(f"cổng {port} vẫn nhận và giữ kết nối TCP sau close()") from None
    finally:
        writer.transport.abort()


@integration
@pytest.mark.parametrize("cancel_after", [0.0, 0.05, 0.2])
async def test_close_cancelled_midway_still_stops_port_and_releases_lock(
    node_factory, raw_pairing_client, tmp_path, cancel_after
):
    """Sửa F3 (review PR-3): task `Node.close()` bị hủy khi đang có lời hỏi ghép đôi (vd. Ctrl+Break lần hai).

    Bất biến: sau khi `close()` trả về hoặc raise CancelledError — cổng không còn nhận kết nối,
    `node.lock` lấy lại được, không còn task `teleport.node*` / `teleport.pairing*` /
    `teleport.connection*` sau một nhịp ngắn; `close()` lần hai trả về không lỗi.

    Client ghép đôi thô ngừng đọc sau khi nhận `pair_reveal` (B đang hỏi), nên `close()` phải chờ
    đóng phiên (tới `CLOSE_TIMEOUT`) và lệnh hủy chắc chắn tới khi nó đang chạy dở.
    """
    B = await node_factory("B", accept_pairing=True)
    port = B.port
    raw_id = load_or_create_identity(tmp_path / "raw")
    c = await raw_pairing_client(port)
    await c.begin(raw_id)
    await c.run_to_code(B.identity.fingerprint)
    await B.rec.wait_prompts()
    c.writer.transport.pause_reading()  # không trả lời close_notify: close() phải chờ

    closing = asyncio.create_task(B.close(), name="test.close")
    await asyncio.sleep(cancel_after)
    closing.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await closing

    assert lock_is_free(B.dir), "node.lock vẫn bị giữ sau khi close() bị hủy"
    await _assert_port_not_accepting(port)
    await wait_until(lambda: not _node_tasks(), 1.0, f"còn task của node: {_node_tasks()}")
    c.writer.transport.resume_reading()
    await c.expect_closed(2.0)

    await asyncio.wait_for(B.close(), 5)  # lần hai: không lỗi, không treo
    await _assert_port_not_accepting(port)
    assert lock_is_free(B.dir)
    assert _node_tasks() == []


# --- N14, N15 ----------------------------------------------------------------------------------------


@integration
async def test_n14_accept_pairing_can_change_while_running(node_factory):
    A = await node_factory("A", accept_pairing=False, answer=True)
    B = await node_factory("B", answer=True)
    first = await asyncio.wait_for(B.pairing.pair_with([LOCAL], A.port), 10)
    assert first.outcome == "peer_busy"
    assert A.accept_pairing is False

    A.accept_pairing = True
    assert A.accept_pairing is True and A.pairing.accepting is True
    second = await asyncio.wait_for(B.pairing.pair_with([LOCAL], A.port), 10)
    assert second.outcome == "paired"

    A.accept_pairing = False
    third = await asyncio.wait_for(B.pairing.pair_with([LOCAL], A.port), 10)
    assert third.outcome == "peer_busy"
    assert len(A.rec.prompts) == 1


@integration
async def test_n15_local_addresses_for_specific_host(node_factory):
    A = await node_factory("A")
    assert await A.local_addresses() == ("127.0.0.1",)


async def test_local_addresses_any_host_uses_discovery_and_survives_errors(tmp_path, monkeypatch, caplog):
    """2.5: `host == 0.0.0.0` → `net.discovery.local_ipv4_addresses` qua thread; lỗi → WARNING, `()`.

    Không start node (bind 0.0.0.0 mở hộp thoại tường lửa Windows). Nếu bản cài đặt đòi node đã
    chạy thì bỏ qua: đặc tả không nói rõ (xem báo cáo).
    """
    from teleport.net import discovery

    caplog.set_level(logging.DEBUG)
    node = Node(tmp_path / "A", name="A", host="0.0.0.0", port=0)
    monkeypatch.setattr(discovery, "local_ipv4_addresses", lambda: ("192.168.1.5", "10.0.0.2"))
    try:
        assert await node.local_addresses() == ("192.168.1.5", "10.0.0.2")
    except RuntimeError:
        pytest.skip("local_addresses() đòi node đã chạy")

    def boom():
        raise OSError("giả lập lỗi đọc card mạng")

    monkeypatch.setattr(discovery, "local_ipv4_addresses", boom)
    assert await node.local_addresses() == ()
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# --- NU1–NU4: unit ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        ("Windows", "windows"),
        ("Darwin", "macos"),
        ("Linux", "linux"),
        ("FreeBSD", "freebsd"),
        ("", "unknown"),
        ("Foo Bar!", "foobar"),
        ("A" * 40, "a" * 16),
        ("!!!", "unknown"),
        ("SunOS_5", "sunos_5"),
    ],
)
def test_nu1_device_os(monkeypatch, system, expected):
    monkeypatch.setattr(platform, "system", lambda: system)
    assert device_os() == expected


@pytest.fixture
def offline_dir(tmp_path):
    """Thư mục cấu hình có danh tính và 3 thiết bị tin cậy: B, C, D (tên "pc" và "PC" trùng nhau)."""
    d = tmp_path / "A"
    load_or_create_identity(d)
    store = TrustStore.load(d)
    ids = {}
    for label, name in (("B", "B"), ("C", "pc"), ("D", "PC")):
        ident = load_or_create_identity(tmp_path / f"peer-{label}")
        store.add(peer_cert_from_pem(ident.cert_pem()), name)
        ids[label] = ident.device_id
    return d, ids


def test_nu2_unpair_offline_by_name_and_prefix(offline_dir):
    d, ids = offline_dir
    removed = unpair_offline(d, "b")
    assert removed.device_id == ids["B"] and removed.name == "B"
    assert TrustStore.load(d).get(ids["B"]) is None
    assert lock_is_free(d)

    removed = unpair_offline(d, ids["C"][:6].upper())
    assert removed.device_id == ids["C"]
    remaining = {x.device_id for x in TrustStore.load(d).devices()}
    assert remaining == {ids["D"]}
    assert lock_is_free(d)


def test_nu3_unpair_offline_while_node_lock_held(offline_dir):
    d, _ids = offline_dir
    before = (d / "trusted.json").read_bytes()
    holder = NodeLock(d)
    holder.acquire()
    try:
        t0 = time.monotonic()
        with pytest.raises(NodeLockedError):
            unpair_offline(d, "B", lock_timeout=0.2)
        waited = time.monotonic() - t0
    finally:
        holder.release()
    assert 0.15 <= waited <= 1.5, waited
    assert (d / "trusted.json").read_bytes() == before


def test_unpair_offline_reads_lock_timeout_at_call_time(offline_dir, monkeypatch):
    """`lock_timeout=None` → `LOCK_TIMEOUT` của module, đọc lúc gọi (patch được)."""
    d, _ = offline_dir
    monkeypatch.setattr(node_mod, "LOCK_TIMEOUT", 0.2)
    holder = NodeLock(d)
    holder.acquire()
    try:
        t0 = time.monotonic()
        with pytest.raises(NodeLockedError):
            unpair_offline(d, "B")
        waited = time.monotonic() - t0
    finally:
        holder.release()
    assert 0.15 <= waited <= 1.5, waited


def test_nu4_unpair_offline_not_found_and_ambiguous(offline_dir):
    d, ids = offline_dir
    before = (d / "trusted.json").read_bytes()
    with pytest.raises(DeviceNotFound) as exc:
        unpair_offline(d, "khong-co")
    assert exc.value.query == "khong-co"
    assert lock_is_free(d)
    with pytest.raises(AmbiguousDevice) as exc2:
        unpair_offline(d, "Pc")
    assert {c.device_id for c in exc2.value.candidates} == {ids["C"], ids["D"]}
    assert lock_is_free(d)
    assert (d / "trusted.json").read_bytes() == before


def test_unpair_offline_corrupt_store_raises_and_releases_lock(tmp_path):
    d = tmp_path / "A"
    load_or_create_identity(d)
    (d / "trusted.json").write_bytes(b"rac")
    with pytest.raises(TrustStoreError):
        unpair_offline(d, "B")
    assert lock_is_free(d)
    assert (d / "trusted.json").read_bytes() == b"rac"


def test_unpair_offline_without_trust_file_is_not_found(tmp_path):
    d = tmp_path / "A"
    load_or_create_identity(d)
    with pytest.raises(DeviceNotFound):
        unpair_offline(d, "B")
    assert lock_is_free(d)


@pytest.mark.parametrize("nested", [False, True], ids=["thu-muc", "thu-muc-cha-cung-chua-co"])
def test_unpair_offline_missing_config_dir_creates_nothing(tmp_path, nested):
    """Sửa F7: thư mục cấu hình chưa có → `DeviceNotFound(query, ())` ngay; không tạo thư mục
    hay `node.lock`."""
    d = tmp_path / "cha" / "A" if nested else tmp_path / "A"
    with pytest.raises(DeviceNotFound) as exc:
        unpair_offline(d, "B")
    assert exc.value.query == "B"
    assert tuple(exc.value.known) == ()
    assert not d.exists()
    assert not (tmp_path / "cha").exists()


@integration
async def test_n3_any_oserror_on_preferred_port_falls_back(node_factory, tmp_path, app_helpers, monkeypatch):
    """Mục 0 điểm 3: mọi OSError ở cổng ưu tiên (vd. WSAEACCES của dải bị giữ chỗ) → dự phòng."""
    from teleport.net import connection

    preferred = app_helpers.free_port()
    monkeypatch.setattr(node_mod, "DEFAULT_PORT", preferred)
    real_start = connection.Listener.start

    async def start(self, host="0.0.0.0", port=47474):
        if port == preferred:
            raise PermissionError(13, "giả lập WSAEACCES: cổng nằm trong dải bị giữ chỗ")
        return await real_start(self, host, port)

    monkeypatch.setattr(connection.Listener, "start", start)
    A = await node_factory("A", port=None)
    assert A.port != preferred and A.port > 0
    assert Settings.load(tmp_path / "A").port == A.port
