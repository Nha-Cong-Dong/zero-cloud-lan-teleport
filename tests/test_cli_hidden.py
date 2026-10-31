"""CLI khi máy đang ẩn (C2 = P4-1, F10; spec-dot-c mục 5.2 “cli.py (Q14 đã duyệt)”).

Viết theo đặc tả, không theo code:
- `listen` khi ẩn: không quảng bá, vẫn nhận kết nối ẩn danh (vì `accept_pairing=True`), in nguyên văn câu
  của mvp F10.
- `receive` / `send` khi ẩn: không quảng bá, in một dòng cảnh báo. Câu chữ của cảnh báo chưa chốt, nên
  test chỉ kiểm có một dòng nhắc tới “ẩn” mà lần chạy đối chứng (máy đang hiện) không có.

Máy “đang ẩn”: `settings.json` có `"visible": false` trước khi chạy lệnh. mDNS giả `cli_mdns`
(tests/conftest.py); chạy trong tiến trình qua `cli_runs`.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

from teleport.net import tls
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore

LOCAL = "127.0.0.1"
NODE_OPTS = ["--host", LOCAL, "--port", "0"]
MDNS_OPTS = ["--mdns-interface", LOCAL]
READY = "Nhấn Ctrl+C để dừng."
SENDER = "Laptop-Minh"
RECEIVER = "PC-Nha"
# mvp F10, nguyên văn (spec-dot-c mục 5.2).
MVP_F10_HIDDEN = "Máy này đang ẩn nên máy kia không tự thấy. Hãy nhập IP của máy này ở máy kia."
# Câu `listen` in khi máy đang hiện (test_cli.py, CL27): khi ẩn thì câu này sai sự thật.
LISTEN_DISCOVERY = "Máy khác trong mạng thấy máy này qua `teleport list` và tự kết nối nếu đã ghép đôi."

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cli_no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
]


def hide(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(json.dumps({"version": 1, "visible": False}), encoding="utf-8")


def hidden_lines(text: str) -> list[str]:
    """Các dòng nhắc tới chế độ ẩn (chữ “ẩn”, so sau khi chuẩn hóa NFC, không phân biệt hoa thường)."""
    norm = unicodedata.normalize("NFC", text)
    return [line for line in norm.splitlines() if re.search(r"(?<!\w)ẩn(?!\w)", line, re.IGNORECASE)]


def advertised_any(instances) -> bool:
    return any("advertise" in m.calls or m.advertised for m in instances)


def listen_argv(d: Path) -> list[str]:
    return ["--config-dir", str(d), "listen", "--name", "A", *NODE_OPTS, *MDNS_OPTS]


async def listen_port(run) -> int:
    await run.wait_text(READY)
    m = re.search(r"Cổng:\s+(\d+)", run.text())
    assert m, run.text()
    return int(m.group(1))


async def anonymous_hello(port: int) -> dict:
    """Kết nối ẩn danh tới `port`, đọc message đầu tiên (phải là `hello`)."""
    from teleport.core.framing import HEADER, decode_body

    reader, writer = await asyncio.wait_for(asyncio.open_connection(LOCAL, port, ssl=tls.client_context()), 5)
    try:
        header = await asyncio.wait_for(reader.readexactly(HEADER.size), 3)
        (n,) = HEADER.unpack(header)
        body = await asyncio.wait_for(reader.readexactly(n), 3)
        return decode_body(body)
    finally:
        writer.transport.abort()


@pytest.fixture
def pair_dirs(tmp_path):
    """S (gửi) và R (nhận) tin nhau: S lưu R là “PC-Nha”, R lưu S là “Laptop-Minh”."""
    s_dir, r_dir = tmp_path / "S", tmp_path / "R"
    ident_s, ident_r = load_or_create_identity(s_dir), load_or_create_identity(r_dir)
    TrustStore.load(s_dir).add(peer_cert_from_pem(ident_r.cert_pem()), RECEIVER)
    TrustStore.load(r_dir).add(peer_cert_from_pem(ident_s.cert_pem()), SENDER)
    return SimpleNamespace(S=s_dir, R=r_dir, recv=tmp_path / "nhan", src=tmp_path / "nguon")


@pytest.fixture
def transfer_fast(fast, monkeypatch):
    from teleport.net import connection

    monkeypatch.setattr(connection, "DRAIN_TIMEOUT", fast.TRANSFER_DRAIN_TIMEOUT)
    return fast


def receive_argv(net, *, json_mode: bool) -> list[str]:
    return [
        "--config-dir",
        str(net.R),
        "receive",
        "--dir",
        str(net.recv),
        *(["--json"] if json_mode else []),
        "--yes",
        "--name",
        RECEIVER,
        *NODE_OPTS,
        *MDNS_OPTS,
    ]


def send_argv(net, path: Path, port: int) -> list[str]:
    return [
        "--config-dir",
        str(net.S),
        "send",
        str(path),
        "--to",
        RECEIVER,
        "--address",
        f"{LOCAL}:{port}",
        "--name",
        SENDER,
        "--host",
        LOCAL,
        *MDNS_OPTS,
    ]


# --- listen ----------------------------------------------------------------------------------------


async def test_listen_hidden_prints_mvp_f10_sentence_and_does_not_advertise(
    tmp_path, fast_cli, cli_mdns, cli_runs
):
    d = tmp_path / "A"
    hide(d)
    listen = cli_runs("listen", listen_argv(d))
    port = await listen_port(listen)
    assert MVP_F10_HIDDEN in listen.text(), listen.text()
    # Vẫn nhận kết nối ẩn danh (chế độ chờ ghép đôi): máy kia ghép đôi được bằng IP nhập tay.
    hello = await anonymous_hello(port)
    assert hello["type"] == "hello" and hello["name"] == "A"
    await asyncio.sleep(0.5)
    assert cli_mdns.instances, "listen phải vẫn bật mDNS (duyệt) khi ẩn"
    assert any("start_browsing" in m.calls for m in cli_mdns.instances)
    assert not advertised_any(cli_mdns.instances), [m.calls for m in cli_mdns.instances]


async def test_listen_hidden_does_not_claim_others_see_it(tmp_path, fast_cli, cli_mdns, cli_runs):
    """Câu “Máy khác trong mạng thấy máy này qua `teleport list`…” sai khi đang ẩn, nên không được in."""
    d = tmp_path / "A"
    hide(d)
    listen = cli_runs("listen", listen_argv(d))
    await listen.wait_text(READY)
    assert LISTEN_DISCOVERY not in listen.text(), listen.text()


async def test_listen_visible_does_not_print_hidden_sentence(tmp_path, fast_cli, cli_mdns, cli_runs):
    listen = cli_runs("listen", listen_argv(tmp_path / "A"))
    await listen.wait_text(READY)
    assert MVP_F10_HIDDEN not in listen.text()
    assert hidden_lines(listen.text()) == [], listen.text()
    await asyncio.sleep(0.3)
    assert advertised_any(cli_mdns.instances), "đối chứng: máy đang hiện thì listen quảng bá"


# --- receive ---------------------------------------------------------------------------------------


async def test_receive_hidden_warns_and_does_not_advertise(pair_dirs, transfer_fast, cli_mdns, cli_runs):
    net = pair_dirs
    hide(net.R)
    recv = cli_runs("receive", receive_argv(net, json_mode=False))
    await recv.wait_text(READY)
    await asyncio.sleep(0.5)
    assert hidden_lines(recv.text()), f"receive khi ẩn phải in một dòng cảnh báo: {recv.text()!r}"
    assert cli_mdns.instances and any("start_browsing" in m.calls for m in cli_mdns.instances)
    assert not advertised_any(cli_mdns.instances), [m.calls for m in cli_mdns.instances]


async def test_receive_visible_has_no_hidden_warning(pair_dirs, transfer_fast, cli_mdns, cli_runs):
    net = pair_dirs
    recv = cli_runs("receive", receive_argv(net, json_mode=False))
    await recv.wait_text(READY)
    await asyncio.sleep(0.3)
    assert hidden_lines(recv.text()) == [], recv.text()
    assert advertised_any(cli_mdns.instances), "đối chứng: máy đang hiện thì receive quảng bá"


async def test_receive_hidden_still_receives_from_trusted(pair_dirs, transfer_fast, cli_mdns, cli_runs):
    """Máy nhận đang ẩn vẫn nhận file từ máy đã ghép đôi nhập địa chỉ tay (kết nối có chứng chỉ tin cậy)."""
    net = pair_dirs
    hide(net.R)
    recv = cli_runs("receive", receive_argv(net, json_mode=True))
    port = (await recv.wait_event("receiving"))["port"]
    net.src.mkdir(parents=True, exist_ok=True)
    f = net.src / "a.bin"
    f.write_bytes(b"x" * 1000)
    send = cli_runs("send", send_argv(net, f, port))
    assert await send.code(30) == 0, send.text()
    assert (net.recv / "a.bin").read_bytes() == b"x" * 1000


# --- send ------------------------------------------------------------------------------------------


async def _send_once(net, cli_mdns, cli_runs) -> tuple[str, list]:
    recv = cli_runs("receive", receive_argv(net, json_mode=True))
    port = (await recv.wait_event("receiving"))["port"]
    before = len(cli_mdns.instances)
    net.src.mkdir(parents=True, exist_ok=True)
    f = net.src / "a.bin"
    f.write_bytes(b"y" * 1000)
    send = cli_runs("send", send_argv(net, f, port))
    assert await send.code(30) == 0, send.text()
    return send.text(), cli_mdns.instances[before:]


async def test_send_hidden_warns_and_does_not_advertise(pair_dirs, transfer_fast, cli_mdns, cli_runs):
    net = pair_dirs
    hide(net.S)
    text, send_mdns = await _send_once(net, cli_mdns, cli_runs)
    assert hidden_lines(text), f"send khi ẩn phải in một dòng cảnh báo: {text!r}"
    assert not advertised_any(send_mdns), [m.calls for m in send_mdns]


async def test_send_visible_has_no_hidden_warning(pair_dirs, transfer_fast, cli_mdns, cli_runs):
    net = pair_dirs
    text, _ = await _send_once(net, cli_mdns, cli_runs)
    assert hidden_lines(text) == [], text
