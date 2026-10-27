"""`teleport receive` bị giết hay dừng có trật tự giữa lượt, chạy lại → lượt hoàn tất (P3-3b, việc V4).

Viết từ đặc tả, không từ code: `specs/spec-p3-3b.md` mục 4 (V4), 5 (“giết tiến trình nhận lúc nhận và lúc
đọc lại → mở lại → hoàn tất”; “đóng trật tự giữa lượt → `.part`, `.state`, `transfer.json` còn → mở lại →
hoàn tất”); `specs/spec-p3.md` mục P3-3b; protocol §6.2 (“Tắt app nhận”); mvp F6, F7.

Mỗi node là một tiến trình con `python -m teleport` (fixture `procs`, `tests/conftest.py`; tự giết khi test
xong). `send` nối tới máy nhận qua proxy TCP (`tcp_proxy`) để khi `receive` chạy lại trên cổng mới chỉ cần
đổi đích của proxy (`TcpProxy.target`, đọc lúc nối); `send` tự nối lại theo lịch thật của nó. Giết bằng
`Popen.kill()` (Windows: TerminateProcess; POSIX: SIGKILL). Ca “lúc đọc lại”: một `sitecustomize.py` trong
thư mục riêng (chỉ nạp khi có biến môi trường của test) làm `PartFile.reread` của tiến trình nhận báo một
dấu lên stderr rồi treo, để test giết đúng lúc đang đọc lại.
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from teleport.lock import NodeLock
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore

pytestmark = [pytest.mark.integration]

LOCAL = "127.0.0.1"
SENDER = "Laptop-Minh"
RECEIVER = "PC-Nha"
MiB = 1024 * 1024
HANG_MARK = "TEST-REREAD-HANG"
HANG_ENV = "TELEPORT_TEST_HANG_REREAD"

SITECUSTOMIZE = f'''
import os, sys, time
if os.environ.get("{HANG_ENV}"):
    from teleport.app import storage as _storage

    _real = _storage.PartFile.reread

    def _reread(self, *, stop=None):
        sys.stderr.write("{HANG_MARK}\\n")
        sys.stderr.flush()
        time.sleep(600)
        return _real(self, stop=stop)

    _storage.PartFile.reread = _reread
'''


# --- Hàm phụ trợ ---------------------------------------------------------------------------------


@pytest.fixture
def pair_dirs(tmp_path):
    """S (gửi) và R (nhận) tin nhau: S lưu R là “PC-Nha”, R lưu S là “Laptop-Minh”."""
    s_dir, r_dir = tmp_path / "S", tmp_path / "R"
    ident_s, ident_r = load_or_create_identity(s_dir), load_or_create_identity(r_dir)
    TrustStore.load(s_dir).add(peer_cert_from_pem(ident_r.cert_pem()), RECEIVER)
    TrustStore.load(r_dir).add(peer_cert_from_pem(ident_s.cert_pem()), SENDER)
    return SimpleNamespace(S=s_dir, R=r_dir, recv=tmp_path / "nhan", src=tmp_path / "nguon", tmp=tmp_path)


def receive_cmd(net) -> list[str]:
    return [
        "--config-dir",
        str(net.R),
        "receive",
        "--json",
        "--yes",
        "--dir",
        str(net.recv),
        "--name",
        RECEIVER,
        "--host",
        LOCAL,
        "--port",
        "0",
    ]


def send_cmd(net, files, port: int, *, extra=()) -> list[str]:
    return [
        "--config-dir",
        str(net.S),
        "send",
        *(str(f) for f in files),
        "--to",
        RECEIVER,
        "--address",
        f"{LOCAL}:{port}",
        "--json",
        "--name",
        SENDER,
        "--host",
        LOCAL,
        *extra,
    ]


def make_file(directory: Path, name: str, size: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(os.urandom(size))
    return path


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def wait_status(proc, status: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        ev = proc.wait_json("status", max(0.1, deadline - time.monotonic()))
        if ev["status"] == status:
            return ev


def poll(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        time.sleep(0.02)


def wait_lock_free(directory: Path) -> None:
    """Khóa của tiến trình bị giết được nhả không đồng bộ trên Windows: chờ có hạn (architecture §4)."""
    lock = NodeLock(directory)
    lock.acquire(timeout=10)
    lock.release()


def state_size(net, tid: str) -> int:
    try:
        return (net.recv / ".teleport" / tid / "0.state").stat().st_size
    except OSError:
        return 0


def statuses_seen(proc) -> list[str]:
    """Mọi `status` JSON tiến trình đã in tới giờ (chuyển hết dòng đang chờ sang `proc.out`)."""
    while not proc.lines.empty():
        proc.out.append(proc.lines.get())
    return [e["status"] for e in proc.events() if e.get("event") == "status"]


def assert_clean_exit(proc) -> None:
    err = proc.err_text()
    assert "Traceback" not in err and "Fatal Python error" not in err, err


def finish_send_and_receive(net, send, recv2, src: Path) -> None:
    """`send` nối lại tới `receive` mới, lượt xong ở cả hai bên, file khớp nguồn; rồi dừng `receive`."""
    assert send.finish(180) == 0, f"{send.out!r} {send.err_text()}"
    results = [e for e in send.events() if e["event"] == "result"]
    assert [r["result"] for r in results] == ["done"], results
    res = recv2.wait_json("result", 60)
    assert res["result"] == "done", res
    assert sha256_of(net.recv / src.name) == sha256_of(src)
    recv2.interrupt()
    assert recv2.finish(30) == 0
    assert_clean_exit(send)
    assert_clean_exit(recv2)


# --- Test -----------------------------------------------------------------------------------------


def test_killed_receive_mid_transfer_then_restarted_finishes(pair_dirs, procs, tcp_proxy):
    net = pair_dirs
    recv = procs(receive_cmd(net))
    proxy = tcp_proxy(recv.wait_json("receiving")["port"])
    src = make_file(net.src, "lon.bin", 24 * MiB)
    send = procs(send_cmd(net, [src], proxy.port, extra=["--limit-rate", "4M"]))
    tid = wait_status(recv, "transferring")["transfer_id"]
    # Có ít nhất một `MARK` bền (header `.state` 41 byte): một phần đã nhận sống qua lần giết.
    poll(lambda: state_size(net, tid) > 41, 30, "chưa có MARK nào trong .state")
    recv.kill()
    tdir = net.recv / ".teleport" / tid
    assert {"transfer.json", "0.part", "0.state"} <= set(os.listdir(tdir))
    wait_lock_free(net.R)

    recv2 = procs(receive_cmd(net))
    port2 = recv2.wait_json("receiving")["port"]
    proxy.target = (LOCAL, port2)
    finish_send_and_receive(net, send, recv2, src)


def test_killed_receive_during_reread_then_restarted_finishes(pair_dirs, procs, tcp_proxy, tmp_path):
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    pythonpath = os.pathsep.join(p for p in (str(hook), os.environ.get("PYTHONPATH", "")) if p)
    net = pair_dirs
    recv = procs(receive_cmd(net), env={HANG_ENV: "1", "PYTHONPATH": pythonpath})
    proxy = tcp_proxy(recv.wait_json("receiving")["port"])
    src = make_file(net.src, "f.bin", 6 * MiB + 9)
    send = procs(send_cmd(net, [src], proxy.port))
    tid = wait_status(recv, "transferring")["transfer_id"]
    poll(lambda: HANG_MARK in recv.err_text(), 60, "tiến trình nhận chưa tới bước đọc lại")
    recv.kill()
    tdir = net.recv / ".teleport" / tid
    assert {"transfer.json", "0.part", "0.state"} <= set(os.listdir(tdir))
    assert not (net.recv / src.name).exists()
    wait_lock_free(net.R)

    recv2 = procs(receive_cmd(net))
    port2 = recv2.wait_json("receiving")["port"]
    proxy.target = (LOCAL, port2)
    finish_send_and_receive(net, send, recv2, src)


def test_ctrl_c_receive_mid_transfer_suspends_then_restarted_finishes(pair_dirs, procs, tcp_proxy):
    """Ctrl+C khi đã có `MARK` bền mà lượt chưa sang đọc lại (spec-p3-3b mục 8.4 F2): file dài hơn hẳn
    `SYNC_EVERY` khối, để `MARK` đầu tới giữa lượt chứ không phải lúc đã đủ file."""
    from teleport.core.blocks import BLOCK_SIZE
    from teleport.core.transfer import SYNC_EVERY

    net = pair_dirs
    recv = procs(receive_cmd(net))
    proxy = tcp_proxy(recv.wait_json("receiving")["port"])
    src = make_file(net.src, "lon.bin", (2 * SYNC_EVERY + 8) * BLOCK_SIZE)
    send = procs(send_cmd(net, [src], proxy.port, extra=["--limit-rate", "4M"]))
    tid = wait_status(recv, "transferring")["transfer_id"]
    poll(lambda: state_size(net, tid) > 41, 30, "chưa có MARK nào trong .state")
    assert "verifying" not in statuses_seen(recv), "lượt đã sang đọc lại trước khi Ctrl+C"
    recv.interrupt()
    assert recv.finish(30) == 0, recv.err_text()
    results = [e for e in recv.events() if e["event"] == "result"]
    assert [(r["result"], r["by_peer"]) for r in results] == [("suspended", False)], results
    assert recv.events()[-1]["event"] == "stopped"
    tdir = net.recv / ".teleport" / tid
    left = set(os.listdir(tdir))
    assert {"transfer.json", "0.part", "0.state"} <= left, (left, recv.out, recv.err_text())
    assert send.p.poll() is None, "send phải còn chờ nối lại"
    assert_clean_exit(recv)

    recv2 = procs(receive_cmd(net))
    port2 = recv2.wait_json("receiving")["port"]
    proxy.target = (LOCAL, port2)
    finish_send_and_receive(net, send, recv2, src)
