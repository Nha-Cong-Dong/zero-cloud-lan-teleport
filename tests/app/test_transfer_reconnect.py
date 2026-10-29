"""Nối lại khi mạng rớt giữa lượt (spec-p3 mục 2.3, P3-0; spec-p3-5 V4, Vd, V8 (i); mvp F6, N4).

Viết từ spec, không theo code: hai node thật trên 127.0.0.1 (`transfer_nodes`, A nhận, B gửi), mọi
kết nối (điều khiển và dữ liệu) đi qua proxy TCP `tcp_proxy` (`tests/conftest.py`). Proxy cắt RST,
chặn (ngừng chuyển byte, không đóng gì) rồi RST, hay chặn rồi đi tiếp, ở 85 % lượt (mvp N4: “ở
85 %”) hoặc ở vị trí ngẫu nhiên. Lượt phải hoàn tất, file khớp nguồn, và byte payload bên gửi đã giao
cho kết nối dữ liệu trừ cỡ file không vượt phần cộng thêm của mvp N4 (`n4_extra`, đọc từ
`docs/mvp.md`), đo cả bằng `TransferFinished.payload_sent` lẫn seam `payload_meter`. Mọi test N4 có
tiền đề `byte lúc cắt > n4_extra` (gửi lại từ đầu thì vượt ngưỡng), để test không kiểm rỗng.

Hằng: `fast` rút ngắn các hạn khác (`IDLE_TIMEOUT` 5 s, `DRAIN_TIMEOUT` 10 s qua `transfer_nodes`);
`RECONNECT_GRACE` đặt lại giá trị thật (24 giờ) để lượt không thể qua được nhờ một hạn ngắn;
keepalive rút ngắn bằng `short_keepalive` (thay `teleport.app.node.ControlChannel`). Ba hạn thật
(keepalive, `IDLE_TIMEOUT`, `DRAIN_TIMEOUT`) đều 30 s; thứ tự của chúng không thử ở đây bằng chặn
thật (P3-6 đo), mà bằng các hạn rút ngắn (spec-p3-5 mục 9.2 C1, N2).
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import os
import random
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import wait_until

from teleport.app.node import DeviceDisconnected
from teleport.core import transfer as core_transfer
from teleport.core.blocks import BLOCK_SIZE
from teleport.net import connection

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

LOCAL = "127.0.0.1"
MiB = 1024 * 1024

# Giá trị thật của hạn chờ nối lại, đọc lúc import (trước khi `fast` vá).
REAL_GRACE = core_transfer.RECONNECT_GRACE

CUT_AT = 0.85  # vị trí gián đoạn (mvp N4: “ở 85 %”)
# Cỡ file đủ lớn để gửi lại từ đầu ở 85 % vượt ngưỡng N4 (tiền đề kiểm ở từng test, không chép số).
SIZE = 96 * MiB


def make_file(directory: Path, name: str, size: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
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


async def wait_gone(path: Path, timeout: float = 20.0) -> None:
    """Chờ tới khi thư mục tạm `path` không còn; quá hạn thì báo các file còn lại (để chẩn đoán)."""
    deadline = time.monotonic() + timeout
    while path.exists():
        if time.monotonic() > deadline:
            try:
                left = sorted(os.listdir(path))
            except OSError as e:
                left = [f"<không liệt kê được: {type(e).__name__}>"]
            raise AssertionError(f"hết {timeout} s mà thư mục tạm của lượt chưa bị xóa; còn: {left}")
        await asyncio.sleep(0.05)


def describe(fin) -> str:
    if fin is None:
        return "chưa có TransferFinished"
    return f"result={fin.result} by_peer={fin.by_peer} reason={fin.reason} payload_sent={fin.payload_sent}"


def disconnect_times(node, peer) -> list[float]:
    """Thời điểm (`time.monotonic()`) mọi `DeviceDisconnected` của `node` về `peer`."""
    return [
        t
        for t, e in zip(node.rec.event_times, node.rec.events, strict=True)
        if isinstance(e, DeviceDisconnected) and e.device.device_id == peer.id
    ]


def status_times(node, tid: str, status: str) -> list[float]:
    return [t for t, u in node.trec.timed("TransferStatusChanged", tid) if str(u.info.status) == status]


@pytest.fixture
def T():
    from teleport.app import transfers

    return transfers


@pytest.fixture
def grace_real(monkeypatch):
    """Hạn chờ nối lại thật (24 giờ): lượt không thể kết thúc nhờ hết hạn trong lúc mất mạng."""
    monkeypatch.setattr(core_transfer, "RECONNECT_GRACE", REAL_GRACE)


@pytest.fixture
def via_proxy(transfer_nodes, tcp_proxy):
    """`await via_proxy(client="sender", **transfer_nodes_kw)` → `SimpleNamespace(net, A, B, proxy, opener,
    other, direction)`: hai node nối kết nối điều khiển **qua proxy** đặt trước node bị nối tới.
    `direction`: chiều proxy của payload (`"c2s"` khi bên gửi mở kết nối, `"s2c"` khi bên nhận mở)."""

    async def make(client: str = "sender", **kw) -> SimpleNamespace:
        net = await transfer_nodes(client=client, connect=False, **kw)
        A, B = net.A, net.B
        opener, other = (B, A) if client == "sender" else (A, B)
        proxy = tcp_proxy(other.port)
        opener.connect_hint(other.id, [LOCAL], proxy.port)
        await wait_until(
            lambda: other.id in opener.connected_devices() and opener.id in other.connected_devices(),
            5,
            "chưa nối được kết nối điều khiển qua proxy",
        )
        direction = "c2s" if client == "sender" else "s2c"
        return SimpleNamespace(
            net=net, A=A, B=B, proxy=proxy, opener=opener, other=other, direction=direction
        )

    return make


async def finish_and_check(T, net, A, B, tid: str, src: Path, timeout: float = 90.0):
    """Chờ hai bên kết thúc; khẳng định DONE, file khớp nguồn; trả (fin_a, fin_b)."""
    fin_b = await B.trec.wait_finished(tid, timeout)
    fin_a = await A.trec.wait_finished(tid, timeout)
    assert fin_b.result == T.TransferResult.DONE, f"bên gửi: {describe(fin_b)}"
    assert fin_a.result == T.TransferResult.DONE, f"bên nhận: {describe(fin_a)}"
    assert sha256_of(net.receive_dir / src.name) == sha256_of(src)
    return fin_a, fin_b


def assert_within_n4(fin_b, meter, size: int, n4_extra: int, *, cuts: int = 1, note: str = "") -> None:
    """mvp N4 theo cả `TransferFinished.payload_sent` lẫn seam `payload_meter` (cận trên)."""
    for label, sent in (("payload_sent", fin_b.payload_sent), ("payload_meter", meter.sent)):
        excess = sent - size
        assert excess >= 0, f"{label}: gửi ít hơn cỡ file ({sent} < {size})"
        assert excess <= cuts * n4_extra, (
            f"{label}: gửi thừa {excess / MiB:.1f} MiB qua {cuts} lần cắt "
            f"(ngưỡng mvp N4: {cuts} × {n4_extra // MiB} MiB) {note}"
        )


# =================================================================================================
# P3-0: chặn lâu hơn keepalive rồi RST / đi tiếp (cả hai chiều client)
# =================================================================================================


@pytest.mark.slow
@pytest.mark.parametrize("end", ["rst", "resume"])
@pytest.mark.parametrize("client", ["sender", "receiver"])
async def test_outage_longer_than_keepalive_then_network_back_finishes_within_n4(
    T, via_proxy, payload_meter, short_keepalive, grace_real, n4_extra, client, end
):
    """Chặn ở 85 % lâu hơn hạn keepalive → hai bên tự thấy mất kết nối điều khiển, lượt sang “Đang nối
    lại…” và không hết hạn; proxy cắt RST (`end="rst"`) hoặc cho đi tiếp (`"resume"`) → bên mở kết nối
    nối lại qua proxy, lượt DONE, file khớp, `payload gửi − cỡ file` ≤ ngưỡng mvp N4."""
    ka = short_keepalive()
    outage = 2 * ka.dead_after + 0.5  # lâu hơn hạn keepalive
    p = await via_proxy(client)
    A, B, proxy = p.A, p.B, p.proxy
    src = make_file(p.net.src, "lon.bin", SIZE)

    cut = SimpleNamespace(at=None, sent=0, tid=None, disc=None, reconnecting=None)

    def snapshot() -> tuple[dict[str, int], dict[str, int]]:
        disc = {"A": len(disconnect_times(A, B)), "B": len(disconnect_times(B, A))}
        rec = {
            "A": A.trec.statuses(cut.tid).count("reconnecting"),
            "B": B.trec.statuses(cut.tid).count("reconnecting"),
        }
        return disc, rec

    def block_at_85(meter) -> None:
        if cut.at is None and cut.tid is not None and meter.sent >= CUT_AT * SIZE:
            proxy.block()
            cut.at = time.monotonic()
            cut.sent = meter.sent
            # Ảnh chụp ngay lúc chặn (đồng bộ): một lần mất kết nối có sẵn trước đó (keepalive ngắn
            # trên máy bận) không được tính là bằng chứng của lần chặn này.
            cut.disc, cut.reconnecting = snapshot()

    payload_meter.hooks.append(block_at_85)
    key = await B.transfers.send(A.id, [src])
    tid = cut.tid = key.transfer_id
    await wait_until(lambda: cut.at is not None, 60, "lượt chưa tới 85 %")
    assert cut.sent > n4_extra, "tiền đề N4: lúc cắt phải đã gửi hơn phần cộng thêm của N4"

    # Chặn lâu hơn keepalive: proxy không đóng gì, hai node tự thấy mất kết nối điều khiển (lần mất
    # kết nối mới, sau lúc chặn).
    for me, node, peer in (("A", A, B), ("B", B, A)):
        n = cut.disc[me] + 1
        await node.rec.wait_event(DeviceDisconnected, peer.id, n=n, timeout=ka.dead_after + 5)
        assert disconnect_times(node, peer)[n - 1] >= cut.at, f"{me}: mất kết nối trước lúc chặn"
    assert proxy.resets == 0
    await asyncio.sleep(max(0.0, cut.at + outage - time.monotonic()))
    assert B.trec.finished(tid) is None, "bên gửi đã kết thúc lượt trong lúc mất mạng"
    assert A.trec.finished(tid) is None, "bên nhận đã kết thúc lượt trong lúc mất mạng"
    if end == "rst":
        proxy.rst()
    proxy.resume()

    _, fin_b = await finish_and_check(T, p.net, A, B, tid, src)
    # Lần chặn này (không phải một lần trước đó) đưa lượt sang “Đang nối lại…” ở cả hai bên.
    _, after = snapshot()
    assert after["A"] > cut.reconnecting["A"], (A.trec.statuses(tid), cut.reconnecting)
    assert after["B"] > cut.reconnecting["B"], (B.trec.statuses(tid), cut.reconnecting)
    assert_within_n4(
        fin_b,
        payload_meter,
        SIZE,
        n4_extra,
        note=f"(lúc chặn đã gửi {cut.sent / MiB:.1f} MiB, {payload_meter.calls} lần write_block)",
    )


def test_n4_threshold_is_read_from_mvp(n4_extra):
    """Ngưỡng dùng ở trên đọc đúng từ hàng N4 của docs/mvp.md (không chép số vào test)."""
    text = (Path(__file__).resolve().parents[2] / "docs" / "mvp.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| N4 |"))
    assert f"+ {n4_extra // MiB} MiB" in row
    assert n4_extra % MiB == 0
    assert CUT_AT * SIZE - n4_extra > 0, "file thử phải đủ lớn để gửi lại từ đầu thì vượt ngưỡng N4"


# =================================================================================================
# V4: cắt ở 85 % theo nhiều kiểu, bên gửi mở kết nối (spec-p3-5 V4, mục 9.2 C1, N2)
# =================================================================================================

# kiểu → (dead_after của keepalive rút ngắn, cách kết thúc lần chặn)
V4_CASES = {
    "rst-ngay": (4.0, "rst"),  # RST ngay khi tới 85 %, không chặn
    # Chặn ngắn = 0,5 × min(keepalive 6 s, `IDLE_TIMEOUT` của `fast`): biên rộng tới hạn gần nhất.
    "chan-ngan-rst": (6.0, "rst"),  # chặn ngắn hơn mọi hạn (“10 s”), rồi RST
    "chan-ngan-di-tiep": (6.0, "resume"),  # chặn ngắn hơn mọi hạn rồi đi tiếp: không mất kết nối
    "chan-idle-truoc-keepalive": (3.0, "rst"),  # hạn đọc dữ liệu < keepalive (“60 s”, C1 (i))
    "chan-rat-dai-rst": (1.5, "rst"),  # chặn tới khi ≥ 2 lần nối lại thất bại, rồi RST (C1 (ii))
}


@pytest.mark.slow
@pytest.mark.parametrize("case", list(V4_CASES))
async def test_cut_at_85_percent_sender_as_client_finishes_within_n4(
    T, via_proxy, payload_meter, short_keepalive, grace_real, n4_extra, monkeypatch, case
):
    """Proxy tự chặn khi chiều payload đã chuyển 85 % cỡ file (`block_at`), rồi theo kiểu: RST ngay; chặn
    ngắn hơn mọi hạn rồi RST / đi tiếp; chặn tới khi hạn đọc dữ liệu cắt kết nối dữ liệu trước keepalive
    rồi RST; chặn tới khi bên gửi thử nối lại thất bại ≥ 2 lần rồi RST. Mọi kiểu: DONE, file khớp,
    `payload gửi − cỡ file ≤ n4_extra` (tiền đề: lúc cắt đã gửi > `n4_extra`). Kiểu có RST: lượt sang
    “Đang nối lại…” sau lúc cắt ở cả hai bên; đi tiếp: không bên nào mất kết nối điều khiển."""
    dead_after, end = V4_CASES[case]
    ka = short_keepalive(dead_after=dead_after)
    idle_events: list[tuple[float, str]] = []
    if case == "chan-idle-truoc-keepalive":
        # Hạn đọc của kết nối dữ liệu ngắn hơn keepalive: kết nối dữ liệu bị cắt vì `IdleTimeout` trước
        # khi kết nối điều khiển bị coi là chết (thứ tự của “60 s” khi các hạn thật khác nhau).
        monkeypatch.setattr(connection, "IDLE_TIMEOUT", 0.5 * ka.dead_after)
        real_idle_error = connection.Connection._idle_error

        def idle_error(self, idle, timeout):
            err = real_idle_error(self, idle, timeout)
            idle_events.append((time.monotonic(), type(err).__name__))
            return err

        monkeypatch.setattr(connection.Connection, "_idle_error", idle_error)
    # Chặn “ngắn” = nửa hạn ngắn nhất đang có hiệu lực (keepalive, hạn đọc dữ liệu); `DRAIN_TIMEOUT`
    # (10 s qua `transfer_nodes`) còn dài hơn.
    short_block = 0.5 * min(ka.dead_after, connection.IDLE_TIMEOUT)
    assert short_block < connection.DRAIN_TIMEOUT

    p = await via_proxy("sender")
    A, B, proxy = p.A, p.B, p.proxy
    src = make_file(p.net.src, "lon.bin", SIZE)
    proxy.block_at(p.direction, int(CUT_AT * SIZE))
    key = await B.transfers.send(A.id, [src])
    tid = key.transfer_id
    await wait_until(lambda: proxy.fired, 60, "lượt chưa tới 85 %")
    cut_at = proxy.fired_at
    cut_sent = payload_meter.sent
    disc_before = {"A": len(disconnect_times(A, B)), "B": len(disconnect_times(B, A))}
    assert proxy.fired_forwarded[p.direction] > n4_extra and cut_sent > n4_extra, (
        "tiền đề N4: lúc cắt phải đã gửi hơn phần cộng thêm của N4"
    )
    accepted_at_cut = proxy.accepted

    if case == "rst-ngay":
        pass
    elif case in ("chan-ngan-rst", "chan-ngan-di-tiep"):
        await asyncio.sleep(short_block)
        # Chặn ngắn hơn mọi hạn: chưa bên nào thấy mất kết nối điều khiển.
        assert len(disconnect_times(A, B)) == disc_before["A"]
        assert len(disconnect_times(B, A)) == disc_before["B"]
    elif case == "chan-idle-truoc-keepalive":
        await A.rec.wait_event(DeviceDisconnected, B.id, n=disc_before["A"] + 1, timeout=ka.dead_after + 5)
        await B.rec.wait_event(DeviceDisconnected, A.id, n=disc_before["B"] + 1, timeout=ka.dead_after + 5)
        idle_after_cut = [t for t, name in idle_events if t >= cut_at and name == "IdleTimeout"]
        assert idle_after_cut, f"không có IdleTimeout nào sau lúc chặn: {idle_events}"
        lost_at = min(t for t in disconnect_times(A, B) if t >= cut_at)
        assert idle_after_cut[0] < lost_at, "kết nối dữ liệu phải bị cắt vì hạn đọc trước keepalive"
    elif case == "chan-rat-dai-rst":
        await A.rec.wait_event(DeviceDisconnected, B.id, n=disc_before["A"] + 1, timeout=ka.dead_after + 5)
        await B.rec.wait_event(DeviceDisconnected, A.id, n=disc_before["B"] + 1, timeout=ka.dead_after + 5)
        # Bên gửi thử nối lại; proxy nhận TCP nhưng không nối tới A, nên mỗi lần thử thất bại.
        await wait_until(lambda: proxy.accepted >= accepted_at_cut + 2, 30, "chưa có 2 lần thử nối lại")
        assert A.trec.finished(tid) is None and B.trec.finished(tid) is None
    assert proxy.blocked or case == "rst-ngay"
    if end == "rst":
        proxy.rst()
    proxy.resume()

    _, fin_b = await finish_and_check(T, p.net, A, B, tid, src)
    if end == "rst":
        assert proxy.resets >= 1
        for node in (A, B):
            assert [t for t in status_times(node, tid, "reconnecting") if t >= cut_at], (
                f"{node.label}: không có “Đang nối lại…” sau lúc cắt: {node.trec.statuses(tid)}"
            )
    else:
        assert proxy.resets == 0
        assert [t for t in disconnect_times(A, B) if t >= cut_at] == []
        assert [t for t in disconnect_times(B, A) if t >= cut_at] == []
    assert_within_n4(fin_b, payload_meter, SIZE, n4_extra, note=f"(lúc cắt đã gửi {cut_sent / MiB:.1f} MiB)")


# =================================================================================================
# Vd: proxy cắt ở vị trí ngẫu nhiên, nhiều lần (spec-p3-5 mục 9.2 C5)
# =================================================================================================


def cut_offsets(seed: int, n4_extra: int) -> list[int]:
    """2–3 vị trí (byte trong file) ngẫu nhiên trong (n4_extra, SIZE), tăng dần, cách nhau ≥ 4 MiB."""
    rnd = random.Random(seed)
    k = rnd.choice((2, 3))
    lo, hi = n4_extra + MiB, SIZE - 2 * MiB
    while True:
        offsets = sorted(rnd.randrange(lo, hi) for _ in range(k))
        if all(b - a >= 4 * MiB for a, b in itertools.pairwise(offsets)):
            return offsets


@pytest.mark.slow
@pytest.mark.parametrize("seed", [3, 11, 2024])
async def test_rst_at_random_offsets_several_times_finishes_within_k_times_n4(
    T, via_proxy, payload_meter, grace_real, n4_extra, seed
):
    """Mỗi lần cắt: khi bên gửi bắt đầu ghi khối có vị trí ≥ vị trí cắt kế tiếp, proxy RST mọi kết nối.
    Vị trí đo theo chỉ số khối (không theo tổng byte đã gửi), nên một bên gửi lại từ đầu phải đi lại tới
    vị trí đó, và mỗi lần như thế gửi thừa > `n4_extra`."""
    offsets = cut_offsets(seed, n4_extra)
    assert offsets[0] > n4_extra, "tiền đề N4: có lần cắt ở byte > n4_extra"
    p = await via_proxy("sender")
    A, B, proxy = p.A, p.B, p.proxy
    src = make_file(p.net.src, "lon.bin", SIZE)
    cuts: list[float] = []
    run = SimpleNamespace(tid=None)

    def cut_when_reached(meter) -> None:
        if len(cuts) >= len(offsets) or run.tid is None:
            return
        # Lần cắt kế tiếp chỉ tính sau khi lần trước đã đưa bên gửi sang “Đang nối lại…”: ngay sau `rst()`
        # bên gửi còn có thể gọi vài `write_block` trên kết nối đã bị cắt.
        if cuts and not [t for t in status_times(B, run.tid, "reconnecting") if t >= cuts[-1]]:
            return
        if meter.log[-1] * BLOCK_SIZE >= offsets[len(cuts)]:
            cuts.append(time.monotonic())
            proxy.rst()

    payload_meter.hooks.append(cut_when_reached)
    key = await B.transfers.send(A.id, [src])
    tid = run.tid = key.transfer_id
    _, fin_b = await finish_and_check(T, p.net, A, B, tid, src)
    assert len(cuts) == len(offsets), f"chỉ cắt {len(cuts)}/{len(offsets)} lần"
    assert proxy.resets >= len(offsets)
    # Mỗi lần cắt đưa lượt sang “Đang nối lại…” mới ở bên gửi (giữa lần cắt đó và lần cắt kế tiếp).
    reconnecting = status_times(B, tid, "reconnecting")
    bounds = [*cuts, float("inf")]
    for i in range(len(cuts)):
        assert any(bounds[i] <= t < bounds[i + 1] for t in reconnecting), (
            f"lần cắt {i + 1}: không có “Đang nối lại…” mới ({B.trec.statuses(tid)})"
        )
    assert_within_n4(
        fin_b,
        payload_meter,
        SIZE,
        n4_extra,
        cuts=len(offsets),
        note=f"(vị trí cắt {[round(o / MiB, 1) for o in offsets]} MiB)",
    )


# =================================================================================================
# V8 (i): nguồn đổi trong lúc proxy chặn ở 85 % (mvp F6 AC4; spec-p3-5 mục 9.2 C3)
# =================================================================================================


def change_source(path: Path, how: str) -> None:
    st = os.stat(path)
    if how == "them-byte":
        with open(path, "ab") as f:
            f.write(b"\x00")
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # chỉ cỡ đổi, mtime giữ nguyên
    else:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    after = os.stat(path)
    if how == "them-byte":
        assert after.st_size == st.st_size + 1 and after.st_mtime_ns == st.st_mtime_ns
    else:
        assert after.st_size == st.st_size and after.st_mtime_ns != st.st_mtime_ns


async def wait_meter_still(meter, quiet: float = 0.5, timeout: float = 10.0) -> int:
    """Chờ tới khi `meter.sent` không đổi trong `quiet` giây; trả giá trị đó."""
    deadline = time.monotonic() + timeout
    last, since = meter.sent, time.monotonic()
    while time.monotonic() - since < quiet:
        assert time.monotonic() < deadline, "bên gửi vẫn giao payload trong lúc mạng bị chặn"
        await asyncio.sleep(0.05)
        if meter.sent != last:
            last, since = meter.sent, time.monotonic()
    return last


@pytest.mark.slow
@pytest.mark.parametrize("how", ["them-byte", "chi-mtime"])
async def test_source_changed_while_network_blocked_cancels_both_sides_and_cleans_temp(
    T, via_proxy, payload_meter, grace_real, how
):
    """Proxy chặn ở 85 %; trong lúc chặn file nguồn đổi (thêm 1 byte, hay chỉ mtime); proxy RST → khi
    nối lại, bên gửi phát hiện ngay lúc mở lại nguồn: B `SOURCE_CHANGED` (`by_peer=False`), A
    `SOURCE_CHANGED` (`by_peer=True`), không gửi thêm payload nào sau lúc đổi, thư mục tạm của A bị xóa,
    không có file mang tên thật."""
    size = 16 * MiB + 5
    p = await via_proxy("sender")
    A, B, proxy = p.A, p.B, p.proxy
    src = make_file(p.net.src, "doi.bin", size)
    proxy.block_at(p.direction, int(CUT_AT * size))
    key = await B.transfers.send(A.id, [src])
    tid = key.transfer_id
    await wait_until(lambda: proxy.fired, 60, "lượt chưa tới 85 %")
    await wait_meter_still(payload_meter)
    assert proxy.blocked
    before_change = payload_meter.sent
    change_source(src, how)
    assert proxy.blocked and payload_meter.sent == before_change, "tiền đề: nguồn đổi lúc mạng đang chặn"
    proxy.rst()
    proxy.resume()

    fin_b = await B.trec.wait_finished(tid, 30)
    fin_a = await A.trec.wait_finished(tid, 30)
    assert (fin_b.result, fin_b.by_peer) == (T.TransferResult.SOURCE_CHANGED, False), describe(fin_b)
    assert (fin_a.result, fin_a.by_peer) == (T.TransferResult.SOURCE_CHANGED, True), describe(fin_a)
    assert payload_meter.sent == before_change, (
        f"bên gửi giao thêm {(payload_meter.sent - before_change) / MiB:.1f} MiB sau khi nguồn đổi"
    )
    temp = p.net.receive_dir / ".teleport" / tid
    await wait_gone(temp)
    assert not (p.net.receive_dir / "doi.bin").exists(), "có file mang tên thật dù nguồn đã đổi"
