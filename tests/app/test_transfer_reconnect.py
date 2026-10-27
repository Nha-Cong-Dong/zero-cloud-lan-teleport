"""Nối lại sau khi mạng “tắt” lâu hơn hạn keepalive (spec-p3 mục 2.3, P3-0; mvp F6, N4).

Viết từ spec, không theo code: hai node thật trên 127.0.0.1 (`transfer_nodes`, A nhận, B gửi), mọi
kết nối (điều khiển và dữ liệu) đi qua proxy TCP `tcp_proxy` (`tests/conftest.py`). Ở 85 % payload,
proxy **chặn** (ngừng chuyển byte, không đóng gì) lâu hơn hạn keepalive, nên hai node tự thấy mất kết
nối điều khiển; sau đó proxy cắt RST mọi kết nối (hoặc cho đi tiếp) và mạng có lại. Lượt phải hoàn
tất, file khớp nguồn, và byte payload bên gửi đã giao cho kết nối dữ liệu (đếm qua seam
`Connection.write_block`) trừ cỡ file không vượt ngưỡng mvp N4 (đọc từ `docs/mvp.md`).

Hằng: `fast` rút ngắn các hạn khác; `RECONNECT_GRACE` đặt lại giá trị thật (24 giờ), để lượt không thể
qua được nhờ một hạn ngắn; keepalive rút ngắn bằng cách thay `teleport.app.node.ControlChannel` (hằng
keepalive là giá trị mặc định của tham số nên vá hằng không có tác dụng, `architecture.md` §6).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from teleport.app import node as node_mod
from teleport.app.node import DeviceDisconnected
from teleport.core import transfer as core_transfer
from teleport.net import connection

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

ROOT = Path(__file__).resolve().parents[2]
LOCAL = "127.0.0.1"
MiB = 1024 * 1024

# Giá trị thật của hạn chờ nối lại, đọc lúc import (trước khi `fast` vá).
REAL_GRACE = core_transfer.RECONNECT_GRACE

# Keepalive rút ngắn cho test: ping mỗi PING s, không nhận byte nào DEAD s → mất kết nối.
PING = 0.25
DEAD = 1.5
OUTAGE = 2 * DEAD + 0.5  # thời gian chặn, lâu hơn hạn keepalive
CUT_AT = 0.85  # vị trí gián đoạn (mvp N4: “ở 85 %”)
# Cỡ file > ngưỡng N4 / (1 − … ): gửi lại từ đầu ở 85 % (≈ 82 MiB) sẽ vượt ngưỡng 64 MiB.
SIZE = 96 * MiB


def n4_slack_bytes() -> int:
    """Phần cộng thêm của mvp N4 (“gửi lại ≤ phần chưa nhận + X MiB”), đọc từ bảng yêu cầu."""
    text = (ROOT / "docs" / "mvp.md").read_text(encoding="utf-8")
    m = re.search(r"^\| N4 \|[^\n]*?phần chưa nhận \+ (\d+) MiB", text, re.MULTILINE)
    assert m, "không tìm thấy ngưỡng N4 trong docs/mvp.md"
    return int(m.group(1)) * MiB


N4_SLACK = n4_slack_bytes()


class ShortKeepaliveChannel(connection.ControlChannel):
    """`ControlChannel` với keepalive ngắn (node gọi `ControlChannel(conn)` qua tên của module)."""

    def __init__(self, conn, *, ping_interval: float = PING, dead_after: float = DEAD) -> None:
        super().__init__(conn, ping_interval=ping_interval, dead_after=dead_after)


async def wait_until(predicate, timeout: float = 5.0, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.01)


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


@pytest.fixture
def T():
    from teleport.app import transfers

    return transfers


@pytest.fixture
def payload_meter(monkeypatch):
    """Đếm byte payload bên gửi giao cho kết nối dữ liệu, qua seam `Connection.write_block`.

    Đếm cả payload của mỗi lần gọi ngay khi gọi (kể cả lần ghi bị cắt giữa chừng hay thất bại), nên
    số đếm là cận trên của byte thật sự đã đi: kiểm N4 theo số này là kiểm chặt hơn. `hooks`: hàm
    `(meter)` gọi đồng bộ sau mỗi lần cộng.
    """
    meter = SimpleNamespace(sent=0, calls=0, hooks=[])
    real = connection.Connection.write_block

    async def write_block(self, index, sha256, payload, *args, **kwargs):
        meter.sent += memoryview(payload).nbytes
        meter.calls += 1
        for hook in list(meter.hooks):
            hook(meter)
        return await real(self, index, sha256, payload, *args, **kwargs)

    monkeypatch.setattr(connection.Connection, "write_block", write_block)
    return meter


@pytest.mark.parametrize("end", ["rst", "resume"])
@pytest.mark.parametrize("client", ["sender", "receiver"])
async def test_outage_longer_than_keepalive_then_network_back_finishes_within_n4(
    T, transfer_nodes, tcp_proxy, payload_meter, monkeypatch, client, end
):
    """Chặn ở 85 % lâu hơn hạn keepalive → hai bên tự thấy mất kết nối điều khiển, lượt sang “Đang nối
    lại…” và không hết hạn; proxy cắt RST (`end="rst"`) hoặc cho đi tiếp (`"resume"`) → bên mở kết nối
    nối lại qua proxy, lượt DONE, file khớp, `payload gửi − cỡ file` ≤ ngưỡng mvp N4."""
    R = T.TransferResult
    monkeypatch.setattr(core_transfer, "RECONNECT_GRACE", REAL_GRACE)
    monkeypatch.setattr(node_mod, "ControlChannel", ShortKeepaliveChannel)
    net = await transfer_nodes(client=client, connect=False)
    A, B = net.A, net.B
    opener, other = (B, A) if client == "sender" else (A, B)
    proxy = tcp_proxy(other.port)
    opener.connect_hint(other.id, [LOCAL], proxy.port)
    await wait_until(
        lambda: other.id in opener.connected_devices() and opener.id in other.connected_devices(),
        5,
        "chưa nối được kết nối điều khiển qua proxy",
    )
    src = make_file(net.src, "lon.bin", SIZE)

    cut = SimpleNamespace(at=None, sent=0, tid=None, disc=None, reconnecting=None)

    def snapshot() -> tuple[dict[str, int], dict[str, int]]:
        disc = {
            "A": len(A.rec.events_of(DeviceDisconnected, B.id)),
            "B": len(B.rec.events_of(DeviceDisconnected, A.id)),
        }
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

    # Chặn lâu hơn keepalive: proxy không đóng gì, hai node tự thấy mất kết nối điều khiển (lần mất
    # kết nối mới, sau lúc chặn).
    for me, node, peer in (("A", A, B), ("B", B, A)):
        n = cut.disc[me] + 1
        await node.rec.wait_event(DeviceDisconnected, peer.id, n=n, timeout=DEAD + 5)
        idx = [
            i
            for i, e in enumerate(node.rec.events)
            if isinstance(e, DeviceDisconnected) and e.device.device_id == peer.id
        ]
        assert node.rec.event_times[idx[n - 1]] >= cut.at, f"{me}: mất kết nối trước lúc chặn"
    assert proxy.resets == 0
    await asyncio.sleep(max(0.0, cut.at + OUTAGE - time.monotonic()))
    assert B.trec.finished(tid) is None, "bên gửi đã kết thúc lượt trong lúc mất mạng"
    assert A.trec.finished(tid) is None, "bên nhận đã kết thúc lượt trong lúc mất mạng"
    if end == "rst":
        proxy.rst()
    proxy.resume()

    fin_b = await B.trec.wait_finished(tid, 90)
    fin_a = await A.trec.wait_finished(tid, 90)
    assert fin_b.result == R.DONE, f"bên gửi: {fin_b.result} {fin_b.reason}"
    assert fin_a.result == R.DONE, f"bên nhận: {fin_a.result} {fin_a.reason}"
    assert sha256_of(net.receive_dir / "lon.bin") == sha256_of(src)
    # Lần chặn này (không phải một lần trước đó) đưa lượt sang “Đang nối lại…” ở cả hai bên.
    _, after = snapshot()
    assert after["A"] > cut.reconnecting["A"], (A.trec.statuses(tid), cut.reconnecting)
    assert after["B"] > cut.reconnecting["B"], (B.trec.statuses(tid), cut.reconnecting)
    excess = payload_meter.sent - SIZE
    assert excess >= 0
    assert excess <= N4_SLACK, (
        f"gửi thừa {excess / MiB:.1f} MiB (ngưỡng mvp N4: {N4_SLACK // MiB} MiB; "
        f"lúc chặn đã gửi {cut.sent / MiB:.1f} MiB, {payload_meter.calls} lần write_block)"
    )


def test_n4_threshold_is_read_from_mvp():
    """Ngưỡng dùng ở trên đọc đúng từ bảng N4 của docs/mvp.md (không chép số vào test)."""
    text = (ROOT / "docs" / "mvp.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| N4 |"))
    assert f"+ {N4_SLACK // MiB} MiB" in row
    assert CUT_AT * SIZE - N4_SLACK > 0, "file thử phải đủ lớn để gửi lại từ đầu thì vượt ngưỡng N4"
