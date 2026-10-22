import asyncio
import contextlib
import io
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from teleport import __version__
from teleport.cli import build_parser, main
from teleport.lock import NodeLock, NodeLockedError
from teleport.paths import ENV_HOME
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_command_is_required(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_identity_is_created_once_and_stays_stable(tmp_path, capsys):
    assert main(["--config-dir", str(tmp_path), "identity"]) == 0
    first = capsys.readouterr().out
    assert main(["--config-dir", str(tmp_path), "identity"]) == 0
    assert capsys.readouterr().out == first
    assert "Fingerprint:" in first


def test_identity_uses_teleport_home(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ENV_HOME, str(tmp_path))
    assert main(["identity"]) == 0
    assert str(tmp_path) in capsys.readouterr().out
    assert (tmp_path / "device.crt").exists()


def test_corrupt_identity_exits_1_with_message_not_traceback(tmp_path, capsys):
    assert main(["--config-dir", str(tmp_path), "identity"]) == 0
    (tmp_path / "device.crt").write_bytes(b"garbage")
    assert main(["--config-dir", str(tmp_path), "identity"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Lỗi: Danh tính thiết bị")
    assert "Traceback" not in err


# =================================================================================================
# PR-3: `listen`, `pair`, `unpair` (đặc tả PR-3 mục 2.6, 3.4, 3.7 và 4.7, CL1–CL15).
#
# Viết theo đặc tả: thông báo so nguyên văn với mục 3.4 / mvp mục 8; mã thoát 0/1/2; `--json`
# in JSON Lines (lệnh chạy lâu) hoặc một object (`unpair`). Các tên mới của `teleport.cli`
# (`run_listen`, `run_pair`, `StdinReader`) được import trong từng test để các test cũ ở trên vẫn
# chạy khi chúng chưa có.
# =================================================================================================

LOCAL = "127.0.0.1"
TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
MVP_NOT_DONE = "Ghép đôi với {x} chưa xong. Nếu mã trên hai máy khác nhau, có thể có người đang giả mạo."
LOCKED_UNPAIR = "Lỗi: Teleport đang chạy với thư mục cấu hình này; hãy dừng `teleport listen` trước."
BAD_ADDRESS = (  # PR-4 mục 3.10 (đổi theo mục 4.8)
    "“{target}” không phải địa chỉ IPv4 hợp lệ. Hãy nhập địa chỉ IP, ví dụ 192.168.1.5 hoặc "
    "192.168.1.5:47474, hoặc tên / mã thiết bị như `teleport list` hiện."
)
NODE_OPTS = ["--host", LOCAL, "--port", "0"]


def run_main(argv: list[str]) -> int:
    """`main(argv)`; lỗi argparse (SystemExit) cũng đổi thành mã thoát."""
    try:
        return main(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1


def run_main_guarded(argv: list[str], timeout: float = 15.0) -> int:
    """Như `run_main` nhưng trong thread có hạn chót: lệnh lẽ ra phải thoát ngay mà treo → test đỏ."""
    result: list[int] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            result.append(run_main(argv))
        except BaseException as e:
            errors.append(e)

    t = threading.Thread(target=target, daemon=True, name="test-cli-main")
    t.start()
    t.join(timeout)
    assert not t.is_alive(), f"lệnh {argv} không thoát sau {timeout} s"
    if errors:
        raise errors[0]
    return result[0]


def lock_is_free(directory) -> bool:
    lock = NodeLock(directory)
    try:
        lock.acquire()
    except NodeLockedError:
        return False
    lock.release()
    return True


@pytest.fixture
def trusted_cfg(tmp_path):
    """Thư mục cấu hình có danh tính và 3 thiết bị tin cậy: "B", "pc", "PC" (hai tên sau trùng nhau)."""
    d = tmp_path / "cfg"
    load_or_create_identity(d)
    store = TrustStore.load(d)
    ids = {}
    for label, name in (("B", "B"), ("C", "pc"), ("D", "PC")):
        ident = load_or_create_identity(tmp_path / f"peer-{label}")
        store.add(peer_cert_from_pem(ident.cert_pem()), name)
        ids[label] = ident.device_id
    return d, ids


# --- CL1–CL4: unpair (trong tiến trình, không socket) ---------------------------------------------


def test_cl1_unpair_by_name(trusted_cfg, capsys):
    d, ids = trusted_cfg
    assert run_main(["--config-dir", str(d), "unpair", "B"]) == 0
    out = capsys.readouterr().out
    assert f"Đã hủy ghép đôi với B ({ids['B'][:8]})." in out
    assert TrustStore.load(d).get(ids["B"]) is None
    assert len(TrustStore.load(d).devices()) == 2
    assert lock_is_free(d)


def test_cl1_unpair_json_prints_one_object(trusted_cfg, capsys):
    d, ids = trusted_cfg
    assert run_main(["--config-dir", str(d), "unpair", ids["B"][:6], "--json"]) == 0
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1, captured.out
    obj = json.loads(lines[0])
    assert obj["removed"] is True
    assert obj["device_id"] == ids["B"]
    assert obj["name"] == "B"
    assert TIME_RE.match(obj["time"]), obj["time"]
    assert captured.err == ""


def test_cl2_unpair_not_found_lists_devices(trusted_cfg, capsys):
    d, ids = trusted_cfg
    before = (d / "trusted.json").read_bytes()
    assert run_main(["--config-dir", str(d), "unpair", "khong-co"]) == 1
    err = capsys.readouterr().err
    assert "Lỗi: Không có thiết bị tin cậy nào khớp “khong-co”." in err
    assert "Các thiết bị tin cậy:" in err
    for label, name in (("B", "B"), ("C", "pc"), ("D", "PC")):
        assert re.search(rf"^  {ids[label][:8]}  {name}$", err, re.M), err
    assert (d / "trusted.json").read_bytes() == before


def test_cl2_unpair_not_found_with_empty_store(tmp_path, capsys):
    d = tmp_path / "cfg"
    load_or_create_identity(d)
    assert run_main(["--config-dir", str(d), "unpair", "B"]) == 1
    assert "(chưa ghép đôi với thiết bị nào)" in capsys.readouterr().err


def test_cl2_unpair_not_found_json(trusted_cfg, capsys):
    d, ids = trusted_cfg
    assert run_main(["--config-dir", str(d), "unpair", "khong-co", "--json"]) == 1
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1, captured.out
    obj = json.loads(lines[0])
    assert obj["error"] == "not_found"
    assert isinstance(obj["message"], str) and obj["message"]
    assert TIME_RE.match(obj["time"])
    assert {c["device_id"] for c in obj["candidates"]} == set(ids.values())
    assert all(set(c) == {"device_id", "name"} for c in obj["candidates"])
    assert captured.err == ""


def test_cl3_unpair_ambiguous_lists_full_ids(trusted_cfg, capsys):
    d, ids = trusted_cfg
    before = (d / "trusted.json").read_bytes()
    assert run_main(["--config-dir", str(d), "unpair", "Pc"]) == 1
    err = capsys.readouterr().err
    assert "Lỗi: “Pc” khớp nhiều thiết bị; hãy nhập mã thiết bị dài hơn:" in err
    assert re.search(rf"^  {ids['C']}  pc$", err, re.M), err
    assert re.search(rf"^  {ids['D']}  PC$", err, re.M), err
    assert ids["B"] not in err
    assert (d / "trusted.json").read_bytes() == before


def test_cl3_unpair_ambiguous_json(trusted_cfg, capsys):
    d, ids = trusted_cfg
    assert run_main(["--config-dir", str(d), "unpair", "pc", "--json"]) == 1
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["error"] == "ambiguous"
    assert sorted(c["device_id"] for c in obj["candidates"]) == sorted([ids["C"], ids["D"]])


def test_cl4_unpair_while_node_lock_held(trusted_cfg, capsys, monkeypatch):
    from teleport.app import node as node_mod

    monkeypatch.setattr(node_mod, "LOCK_TIMEOUT", 0.2)
    d, _ids = trusted_cfg
    before = (d / "trusted.json").read_bytes()
    holder = NodeLock(d)
    holder.acquire()
    try:
        t0 = time.monotonic()
        assert run_main(["--config-dir", str(d), "unpair", "B"]) == 1
        assert time.monotonic() - t0 < 2.0
        assert LOCKED_UNPAIR in capsys.readouterr().err
        assert run_main(["--config-dir", str(d), "unpair", "B", "--json"]) == 1
        obj = json.loads(capsys.readouterr().out.strip())
        assert obj["error"] == "node_locked"
    finally:
        holder.release()
    assert (d / "trusted.json").read_bytes() == before


def test_unpair_corrupt_store_exits_1(tmp_path, capsys):
    d = tmp_path / "cfg"
    load_or_create_identity(d)
    (d / "trusted.json").write_bytes(b"rac")
    assert run_main(["--config-dir", str(d), "unpair", "B"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Lỗi: ")
    assert "Traceback" not in err
    assert (d / "trusted.json").read_bytes() == b"rac"


# --- Sửa F6/F7: thư mục cấu hình không dùng được ----------------------------------------------------

CONFIG_DIR_ERROR = "Không dùng được thư mục cấu hình"
PORT_ERROR = "Không mở được cổng để lắng nghe"


@pytest.fixture
def config_is_file(tmp_path):
    """`--config-dir` trỏ vào một file thường (không phải thư mục)."""
    f = tmp_path / "cfg-la-file"
    f.write_bytes(b"khong phai thu muc")
    return f


@pytest.mark.parametrize("command", ["listen", "pair"])
def test_node_command_with_config_dir_that_is_a_file_exits_1(config_is_file, capsys, command):
    """F6: lỗi thư mục cấu hình không bị báo nhầm là lỗi cổng; mã 1, không traceback."""
    target = [f"{LOCAL}:47474"] if command == "pair" else []
    argv = ["--config-dir", str(config_is_file), command, *target, "--name", "B", *NODE_OPTS]
    assert run_main_guarded(argv) == 1
    captured = capsys.readouterr()
    assert f"Lỗi: {CONFIG_DIR_ERROR} {config_is_file}" in captured.err, captured.err
    assert PORT_ERROR not in captured.err
    assert "Traceback" not in captured.err
    assert config_is_file.read_bytes() == b"khong phai thu muc"


def test_listen_with_config_dir_that_is_a_file_json(config_is_file, capsys):
    argv = ["--config-dir", str(config_is_file), "listen", "--json", "--name", "B", *NODE_OPTS]
    assert run_main_guarded(argv) == 1
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    assert len(events) == 1, captured.out
    obj = events[0]
    assert obj["event"] == "error"
    assert isinstance(obj["error"], str) and obj["error"] != "port_unavailable"
    assert CONFIG_DIR_ERROR in obj["message"]
    assert TIME_RE.match(obj["time"])
    assert "Traceback" not in captured.err


@pytest.mark.integration
def test_listen_on_busy_port_still_reports_port_error(tmp_path, capsys):
    """F6: lỗi mở cổng vẫn là câu về cổng (mục 3.7), không phải câu về thư mục cấu hình."""
    s = socket.socket()
    try:
        s.bind((LOCAL, 0))
        s.listen(1)
        busy = s.getsockname()[1]
        opts = ["--name", "B", "--host", LOCAL, "--port", str(busy)]
        argv = ["--config-dir", str(tmp_path / "B"), "listen", *opts]
        assert run_main_guarded(argv) == 1
    finally:
        s.close()
    err = capsys.readouterr().err
    assert f"Lỗi: {PORT_ERROR}" in err, err
    assert CONFIG_DIR_ERROR not in err
    assert "Traceback" not in err
    assert lock_is_free(tmp_path / "B")


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_unpair_with_config_dir_that_is_a_file_exits_1(config_is_file, capsys, json_mode):
    """F6: `unpair` bắt OSError → mã 1, không traceback; `--json` → một object `error`."""
    argv = ["--config-dir", str(config_is_file), "unpair", "B", *(["--json"] if json_mode else [])]
    assert run_main_guarded(argv) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    if json_mode:
        obj = json.loads(captured.out.strip())
        assert isinstance(obj["error"], str) and obj["error"]
        assert isinstance(obj["message"], str) and obj["message"]
        assert captured.err == ""
    else:
        assert captured.err.startswith("Lỗi: "), captured.err
    assert config_is_file.read_bytes() == b"khong phai thu muc"


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_unpair_with_missing_config_dir_creates_nothing(tmp_path, capsys, json_mode):
    """F7: thư mục cấu hình chưa có → “không tìm thấy”, mã 1; không tạo thư mục hay `node.lock`."""
    d = tmp_path / "chua-co"
    argv = ["--config-dir", str(d), "unpair", "B", *(["--json"] if json_mode else [])]
    assert run_main_guarded(argv) == 1
    captured = capsys.readouterr()
    if json_mode:
        obj = json.loads(captured.out.strip())
        assert obj["error"] == "not_found"
        assert obj["candidates"] == []
    else:
        assert "Lỗi: Không có thiết bị tin cậy nào khớp “B”." in captured.err
        assert "(chưa ghép đôi với thiết bị nào)" in captured.err
    assert not d.exists()


# --- CL5, CL6: lỗi cú pháp → mã 2, không chạy node -------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "1.2.3.4:0",
        "::1",
        "1.2.3.4:65536",
        "0.0.0.0",
        "192.168.1",
        "1.2.3.4:080",
        "12.5",
        "fe80::1",
        "[::1]:80",
    ],
)
def test_cl5_pair_bad_address_exits_2_without_side_effects(tmp_path, capsys, address):
    d = tmp_path / "cfg"
    assert run_main_guarded(["--config-dir", str(d), "pair", address, *NODE_OPTS]) == 2
    assert BAD_ADDRESS.format(target=address) in capsys.readouterr().err
    assert not (d / "node.lock").exists()
    assert not (d / "settings.json").exists()
    assert not (d / "device.crt").exists()


def test_cl5_pair_bad_address_json(tmp_path, capsys):
    d = tmp_path / "cfg"
    assert run_main_guarded(["--config-dir", str(d), "pair", "1.2.3.4:0", "--json", *NODE_OPTS]) == 2
    captured = capsys.readouterr()
    obj = json.loads(captured.out.strip())
    assert obj["error"] == "invalid_address"
    assert captured.err == ""


@pytest.mark.parametrize("name", ["  x", "a\u202eb", "x" * 65])
def test_cl6_listen_bad_name_exits_2(tmp_path, capsys, name):
    d = tmp_path / "cfg"
    assert run_main_guarded(["--config-dir", str(d), "listen", "--name", name, *NODE_OPTS]) == 2
    assert "Tên thiết bị không hợp lệ" in capsys.readouterr().err
    assert not (d / "node.lock").exists()


@pytest.mark.parametrize(
    "extra",
    [
        ["--host", "localhost", "--port", "0"],
        ["--host", LOCAL, "--port", "70000"],
        ["--host", LOCAL, "--port", "x"],
    ],
)
def test_listen_bad_hidden_options_exit_2(tmp_path, extra):
    d = tmp_path / "cfg"
    assert run_main_guarded(["--config-dir", str(d), "listen", *extra]) == 2
    assert not (d / "node.lock").exists()


def test_hidden_options_are_not_in_help(capsys):
    for cmd in ("listen", "pair"):
        with pytest.raises(SystemExit):
            main([cmd, "--help"])
        out = capsys.readouterr().out
        assert "--host" not in out and "--port" not in out
        assert "--json" in out and "--name" in out


# --- Trong tiến trình, có socket (CL7–CL12) --------------------------------------------------------


@pytest.fixture
def fast_cli(monkeypatch):
    """Như `fast` của tests/app (mục 4.1): rút ngắn hằng thời gian trước khi tạo Node."""
    from teleport.app import node as app_node
    from teleport.app import pairing as app_pairing
    from teleport.core import pairing as core_pairing
    from teleport.net import connection

    values = {
        core_pairing: {
            "MESSAGE_TIMEOUT": 1.0,
            "NONCE_TIMEOUT": 0.5,
            "CONFIRM_TIMEOUT": 2.0,
            "CONFIRM_GRACE": 0.5,
            "BACKOFF_BASE": 0.5,
            "BACKOFF_MAX": 2.0,
        },
        connection: {"OPENING_TIMEOUT": 2.0, "CLOSE_TIMEOUT": 0.5, "DRAIN_TIMEOUT": 1.0},
        app_pairing: {
            "SEND_TIMEOUT": 1.0,
            "BUSY_REPLY_TIMEOUT": 0.5,
            "ABORT_ON_CANCEL_TIMEOUT": 0.5,
            "CLOSE_SESSIONS_TIMEOUT": 1.0,
        },
        app_node: {"LOCK_TIMEOUT": 0.5},
    }
    for module, names in values.items():
        for name, value in names.items():
            monkeypatch.setattr(module, name, value)


@pytest.fixture
async def cli_no_leaks():
    """Không còn task nào của test hay của Teleport sau khi test xong."""
    before = asyncio.all_tasks()
    yield
    await asyncio.sleep(0.05)
    me = asyncio.current_task()
    left = [t for t in asyncio.all_tasks() if t is not me and t not in before and not t.done()]
    assert not left, [t.get_name() for t in left]


class CliRun:
    """Một lần chạy `run_listen` / `run_pair` trong tiến trình, với `Lines` và đầu ra riêng."""

    def __init__(self, coro_fn, argv: list[str]) -> None:
        self.args = build_parser().parse_args(argv)
        self.lines: asyncio.Queue = asyncio.Queue()
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.task = asyncio.create_task(coro_fn(self.args, self.lines, self.out, self.err), name="test.cli")

    def say(self, text: str | None, ts: float | None = None) -> None:
        # Hợp đồng `Lines` (sửa F2): mốc đọc là `time.perf_counter()`.
        self.lines.put_nowait((time.perf_counter() if ts is None else ts, text))

    def text(self) -> str:
        return self.out.getvalue() + self.err.getvalue()

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.out.getvalue().splitlines() if line.strip()]

    def events_of(self, name: str) -> list[dict]:
        return [e for e in self.events() if e.get("event") == name]

    async def wait_event(self, name: str, n: int = 1, timeout: float = 8.0) -> dict:
        deadline = time.monotonic() + timeout
        while len(self.events_of(name)) < n:
            if self.task.done():
                raise AssertionError(f"lệnh đã dừng trước khi có {name}: {self.text()!r}")
            if time.monotonic() > deadline:
                raise AssertionError(f"hết {timeout} s chưa có {name}: {self.text()!r}")
            await asyncio.sleep(0.01)
        return self.events_of(name)[n - 1]

    async def wait_text(self, needle: str, n: int = 1, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while self.text().count(needle) < n:
            if self.task.done():
                raise AssertionError(f"lệnh đã dừng mà chưa in {needle!r}: {self.text()!r}")
            if time.monotonic() > deadline:
                raise AssertionError(f"hết {timeout} s chưa in {needle!r}: {self.text()!r}")
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        if not self.task.done():
            self.task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(self.task, 15)


@pytest.fixture
async def cli_runs():
    """`start("listen"|"pair", argv)` → `CliRun`; mọi lần chạy được dừng khi test xong."""
    from teleport.cli import run_listen, run_pair

    runs: list[CliRun] = []

    def start(kind: str, argv: list[str]) -> CliRun:
        run = CliRun(run_listen if kind == "listen" else run_pair, argv)
        runs.append(run)
        return run

    yield start

    for run in runs:
        await run.stop()


def listen_argv(d, *, json_mode: bool = True, name: str = "B") -> list[str]:
    return ["--config-dir", str(d), "listen", *(["--json"] if json_mode else []), "--name", name, *NODE_OPTS]


def pair_argv(d, port: int, *, json_mode: bool = True, name: str = "A") -> list[str]:
    mode = ["--json"] if json_mode else []
    return ["--config-dir", str(d), "pair", f"{LOCAL}:{port}", *mode, "--name", name, *NODE_OPTS]


async def listen_port_text(run: CliRun) -> int:
    await run.wait_text("Nhấn Ctrl+C để dừng.")
    m = re.search(r"Cổng:\s+(\d+)", run.text())
    assert m, run.text()
    return int(m.group(1))


socket_test = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cli_no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
]


def with_marks(marks):
    def deco(fn):
        for mark in reversed(marks):
            fn = mark(fn)
        return fn

    return deco


@with_marks(socket_test)
async def test_cl7_listen_and_pair_json_end_to_end(tmp_path, fast_cli, cli_runs):
    listen = cli_runs("listen", listen_argv(tmp_path / "B"))
    ev = await listen.wait_event("listening")
    assert TIME_RE.match(ev["time"])
    assert ev["name"] == "B"
    assert re.fullmatch(r"[0-9a-f]{32}", ev["device_id"])
    assert isinstance(ev["port"], int) and ev["port"] > 0
    assert ev["addresses"] == [LOCAL]

    pair = cli_runs("pair", pair_argv(tmp_path / "A", ev["port"]))
    pb = await listen.wait_event("pair_prompt")
    pa = await pair.wait_event("pair_prompt")
    assert re.fullmatch(r"[0-9]{6}", pa["code"]) and pa["code"] == pb["code"]
    for p, role, peer in ((pa, "initiator", "B"), (pb, "responder", "A")):
        assert p["role"] == role
        assert p["peer_name"] == peer
        assert re.fullmatch(r"[0-9a-f]{32}", p["peer_device_id"])
        assert p["address"] == LOCAL
        assert isinstance(p["expires_in"], float) and 0 < p["expires_in"] <= 2.0
        assert TIME_RE.match(p["time"])
        assert "session_id" in p
    assert pa["peer_device_id"] == ev["device_id"]

    pair.say("c")
    listen.say("c")
    assert await asyncio.wait_for(pair.task, 10) == 0
    rb = await listen.wait_event("pair_result")
    ra = pair.events_of("pair_result")[0]
    for r, peer in ((ra, "B"), (rb, "A")):
        assert r["outcome"] == "paired"
        assert r["message"] == f"Đã ghép đôi với {peer}."
        assert r["peer_name"] == peer
        assert TIME_RE.match(r["time"])
        assert {"session_id", "role", "peer_device_id", "address"} <= set(r)
    assert TrustStore.load(tmp_path / "A").get(ev["device_id"]) is not None
    assert TrustStore.load(tmp_path / "B").get(pb["peer_device_id"]) is not None

    listen.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(listen.task, 10)
    assert lock_is_free(tmp_path / "B")
    assert lock_is_free(tmp_path / "A")
    assert listen.err.getvalue() == "" and pair.err.getvalue() == ""


@with_marks(socket_test)
async def test_cl8_text_mode_reject_messages(tmp_path, fast_cli, cli_runs):
    listen = cli_runs("listen", listen_argv(tmp_path / "B", json_mode=False))
    port = await listen_port_text(listen)
    head = listen.text()
    assert "Teleport đang chờ ghép đôi." in head
    assert re.search(r"Tên:\s+B$", head, re.M)
    assert re.search(r"Mã máy:\s+[0-9a-f]{8}$", head, re.M)
    assert re.search(r"Địa chỉ:\s+127\.0\.0\.1$", head, re.M)
    assert f"Trên máy kia, chạy: teleport pair 127.0.0.1:{port}" in head

    pair = cli_runs("pair", pair_argv(tmp_path / "A", port, json_mode=False))
    await pair.wait_text("Trùng khớp? [c/k]:")
    await listen.wait_text("Trùng khớp? [c/k]:")
    assert f"Đang kết nối tới 127.0.0.1:{port}…" in pair.text()
    assert "Ghép đôi với “B” (127.0.0.1, mã máy " in pair.text()
    assert "Yêu cầu ghép đôi từ “A” (127.0.0.1, mã máy " in listen.text()
    codes_a = re.findall(r"màn hình của B:\s+(\d{3} \d{3})", pair.text())
    codes_b = re.findall(r"màn hình của A:\s+(\d{3} \d{3})", listen.text())
    assert codes_a and codes_a == codes_b

    pair.say("k")
    assert await asyncio.wait_for(pair.task, 10) == 1
    assert MVP_NOT_DONE.format(x="B") in pair.text()
    await listen.wait_text(MVP_NOT_DONE.format(x="A"))
    await listen.wait_text("Tiếp tục chờ ghép đôi…")


@with_marks(socket_test)
async def test_listen_cancelled_while_asking_does_not_say_keep_waiting(tmp_path, fast_cli, cli_runs):
    """Sửa F9: Ctrl+C (hủy task) khi `listen` đang hỏi → in “Đã hủy ghép đôi.” nhưng không in
    “Tiếp tục chờ ghép đôi…” (node đang đóng, không chờ tiếp)."""
    listen = cli_runs("listen", listen_argv(tmp_path / "B", json_mode=False))
    port = await listen_port_text(listen)
    pair = cli_runs("pair", pair_argv(tmp_path / "A", port))
    await listen.wait_text("Trùng khớp? [c/k]:")
    await pair.wait_event("pair_prompt")

    listen.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(listen.task, 10)
    after_prompt = listen.text().split("Trùng khớp? [c/k]:", 1)[1]
    assert "Đã hủy ghép đôi." in after_prompt
    assert "Tiếp tục chờ ghép đôi" not in listen.text(), after_prompt
    assert lock_is_free(tmp_path / "B")
    assert await asyncio.wait_for(pair.task, 10) == 1
    assert pair.events_of("pair_result")[0]["outcome"] == "peer_rejected"


@with_marks(socket_test)
async def test_cl9_line_typed_before_prompt_is_ignored(tmp_path, fast_cli, cli_runs):
    listen = cli_runs("listen", listen_argv(tmp_path / "B"))
    ev = await listen.wait_event("listening")
    t_before = time.perf_counter()
    listen.say("c", ts=t_before)  # gõ sẵn trước khi có lời hỏi
    await asyncio.sleep(0.05)

    pair = cli_runs("pair", pair_argv(tmp_path / "A", ev["port"]))
    await listen.wait_event("pair_prompt")
    await pair.wait_event("pair_prompt")
    listen.say("c", ts=t_before)  # dòng đọc trước lúc hiện mã nhưng tới hàng đợi muộn
    await asyncio.sleep(0.1)
    pair.say("c")
    await asyncio.sleep(0.1)
    assert listen.events_of("pair_result") == [], "dòng gõ trước lúc hiện mã đã được dùng để xác nhận"
    listen.say("k")
    assert await asyncio.wait_for(pair.task, 10) == 1
    rb = await listen.wait_event("pair_result")
    assert rb["outcome"] == "user_rejected"
    assert TrustStore.load(tmp_path / "B").devices() == []
    assert pair.events_of("pair_result")[0]["outcome"] == "peer_rejected"


@with_marks(socket_test)
@pytest.mark.parametrize("before", [0.001, 0.0002], ids=["1ms", "0.2ms"])
async def test_line_read_just_before_prompt_is_ignored(tmp_path, fast_cli, cli_runs, monkeypatch, before):
    """Sửa F2: dòng đọc **trước** lúc hiện mã dù chỉ 1 ms (hoặc ít hơn) cũng bị bỏ.

    Trên Python 3.12 Windows `time.monotonic()` chỉ chính xác 15,6 ms nên dòng như vậy từng được
    nhận. Hợp đồng mới: mốc đọc và mốc hiện mã đều là `time.perf_counter()`, so bằng `<`. Test
    ghi `perf_counter()` ngay **trước** khi lời hỏi được giao cho CLI, nên mốc hiện mã ≥ mốc đó.
    """
    from teleport import cli

    called_at: dict[str, float] = {}  # vai → perf_counter() ngay trước khi CLI nhận lời hỏi
    real_on_prompt = cli._Prompter.on_prompt

    def on_prompt(self, prompt):
        called_at[prompt.role.value] = time.perf_counter()
        return real_on_prompt(self, prompt)

    monkeypatch.setattr(cli._Prompter, "on_prompt", on_prompt)

    listen = cli_runs("listen", listen_argv(tmp_path / "B"))
    ev = await listen.wait_event("listening")
    pair = cli_runs("pair", pair_argv(tmp_path / "A", ev["port"]))
    await listen.wait_event("pair_prompt")
    await pair.wait_event("pair_prompt")
    assert set(called_at) == {"initiator", "responder"}
    listen.say("c", ts=called_at["responder"] - before)  # `listen` là bên trả lời
    pair.say("c")
    await asyncio.sleep(0.3)
    assert listen.events_of("pair_result") == [], "dòng đọc trước lúc hiện mã đã được dùng để xác nhận"
    assert TrustStore.load(tmp_path / "B").devices() == []

    listen.say("k")  # dòng đọc sau lúc hiện mã vẫn được nhận
    assert await asyncio.wait_for(pair.task, 10) == 1
    assert (await listen.wait_event("pair_result"))["outcome"] == "user_rejected"


@with_marks(socket_test)
async def test_cl10_answer_after_prompt_expired_does_nothing(tmp_path, fast_cli, cli_runs):
    listen = cli_runs("listen", listen_argv(tmp_path / "B"))
    ev = await listen.wait_event("listening")
    pair = cli_runs("pair", pair_argv(tmp_path / "A", ev["port"]))
    await pair.wait_event("pair_prompt")
    await listen.wait_event("pair_prompt")
    pair.say("c")
    assert await asyncio.wait_for(pair.task, 10) == 1
    rb = await listen.wait_event("pair_result", timeout=2.0 + 0.5 + 2.5)
    assert rb["outcome"] == "timeout"
    assert rb["message"] == MVP_NOT_DONE.format(x="A")

    n_events = len(listen.events())
    listen.say("c")
    await asyncio.sleep(0.3)
    assert len(listen.events()) == n_events
    assert not listen.task.done()
    assert TrustStore.load(tmp_path / "B").devices() == []
    assert listen.err.getvalue() == ""


@with_marks(socket_test)
async def test_cl11_stdin_eof_rejects_immediately_and_later(tmp_path, fast_cli, cli_runs):
    listen = cli_runs("listen", listen_argv(tmp_path / "B"))
    ev = await listen.wait_event("listening")
    pair = cli_runs("pair", pair_argv(tmp_path / "A", ev["port"]))
    await listen.wait_event("pair_prompt")
    await pair.wait_event("pair_prompt")
    t0 = time.monotonic()
    listen.say(None)
    rb = await listen.wait_event("pair_result", timeout=1.0)
    assert time.monotonic() - t0 < 1.0
    assert rb["outcome"] == "user_rejected"
    assert await asyncio.wait_for(pair.task, 10) == 1

    # Lời hỏi sau cũng bị từ chối ngay (stdin đã đóng). Chờ hết thời gian chờ 0,5 s của IP trước.
    await asyncio.sleep(0.7)
    pair2 = cli_runs("pair", pair_argv(tmp_path / "A", ev["port"]))
    await listen.wait_event("pair_prompt", n=2)
    t1 = time.monotonic()
    rb2 = await listen.wait_event("pair_result", n=2, timeout=1.0)
    assert time.monotonic() - t1 < 1.0
    assert rb2["outcome"] == "user_rejected"
    assert await asyncio.wait_for(pair2.task, 10) == 1


@with_marks(socket_test)
async def test_cl12_invalid_answer_prints_hint(tmp_path, fast_cli, cli_runs):
    hint = "Hãy gõ c (trùng) hoặc k (không trùng)."
    listen = cli_runs("listen", listen_argv(tmp_path / "B", json_mode=False))
    port = await listen_port_text(listen)
    pair = cli_runs("pair", pair_argv(tmp_path / "A", port))
    await listen.wait_text("Trùng khớp? [c/k]:")
    await pair.wait_event("pair_prompt")
    listen.say("x")
    await listen.wait_text(hint)
    listen.say("có")  # chỉ nhận câu trả lời ASCII
    await listen.wait_text(hint, n=2)
    pair.say("c")
    listen.say("  YES ")
    assert await asyncio.wait_for(pair.task, 10) == 0
    await listen.wait_text("Đã ghép đôi với A.")


@with_marks(socket_test)
async def test_cl12_json_mode_invalid_answer_is_silent(tmp_path, fast_cli, cli_runs):
    listen = cli_runs("listen", listen_argv(tmp_path / "B"))
    ev = await listen.wait_event("listening")
    pair = cli_runs("pair", pair_argv(tmp_path / "A", ev["port"]))
    await listen.wait_event("pair_prompt")
    await pair.wait_event("pair_prompt")
    listen.say("x")
    await asyncio.sleep(0.2)
    assert "Hãy gõ" not in listen.text()
    for line in listen.out.getvalue().splitlines():
        json.loads(line)  # chỉ có JSON Lines trên stdout
    listen.say("No")
    assert await asyncio.wait_for(pair.task, 10) == 1
    assert (await listen.wait_event("pair_result"))["outcome"] == "user_rejected"


@with_marks(socket_test)
async def test_pair_unreachable_message(tmp_path, fast_cli, cli_runs):
    s = socket.socket()
    s.bind((LOCAL, 0))
    port = s.getsockname()[1]
    s.close()
    pair = cli_runs("pair", pair_argv(tmp_path / "A", port, json_mode=False))
    assert await asyncio.wait_for(pair.task, 10) == 1
    assert (
        f"Không kết nối được tới 127.0.0.1:{port}. Hãy kiểm tra Teleport đang chạy trên máy đó, "
        "hai máy dùng chung một mạng, và tường lửa cho phép Teleport."
    ) in pair.text()


@with_marks(socket_test)
async def test_pair_peer_busy_message(tmp_path, fast_cli, cli_runs):
    """`peer_busy` (mục 3.4): máy kia đang ghép đôi với máy khác → câu mvp 8 + gợi ý `teleport listen`."""
    listen = cli_runs("listen", listen_argv(tmp_path / "B"))
    ev = await listen.wait_event("listening")
    first = cli_runs("pair", pair_argv(tmp_path / "A", ev["port"]))
    await listen.wait_event("pair_prompt")
    await first.wait_event("pair_prompt")
    second = cli_runs("pair", pair_argv(tmp_path / "C", ev["port"], json_mode=False, name="C"))
    assert await asyncio.wait_for(second.task, 10) == 1
    text = second.text()
    assert (
        "B chưa sẵn sàng ghép đôi: máy đó đang ghép đôi với máy khác, chưa mở chế độ chờ ghép đôi, "
        "hoặc cần chờ một lát sau lần thử trước. Hãy thử lại sau."
    ) in text
    assert "Trên máy đó, hãy chạy: `teleport listen`" in text
    first.say("k")
    assert await asyncio.wait_for(first.task, 10) == 1


@with_marks(socket_test)
async def test_pair_with_self_message(tmp_path, fast_cli, cli_runs):
    """`self` (mục 3.4): `pair` tới chính cổng node của nó."""
    s = socket.socket()
    s.bind((LOCAL, 0))
    port = s.getsockname()[1]
    s.close()
    argv = ["--config-dir", str(tmp_path / "A"), "pair", f"{LOCAL}:{port}", "--name", "A"]
    pair = cli_runs("pair", [*argv, "--host", LOCAL, "--port", str(port)])
    assert await asyncio.wait_for(pair.task, 10) == 1
    assert "Đó là chính máy này. Hãy nhập địa chỉ của máy kia." in pair.text()


# --- Subprocess (CL13–CL15) ----------------------------------------------------------------------------


class Proc:
    """`python -m teleport …` thật; stdout đọc theo dòng trong thread; luôn bị dừng khi xong."""

    def __init__(self, argv: list[str]) -> None:
        env = dict(os.environ)
        env.pop(ENV_HOME, None)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.p = subprocess.Popen(
            [sys.executable, "-m", "teleport", *argv],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=flags,
            env=env,
        )
        self.lines: queue.Queue = queue.Queue()
        self.out: list[str] = []
        self.err = bytearray()
        self._t_out = threading.Thread(target=self._read_out, daemon=True)
        self._t_err = threading.Thread(target=self._read_err, daemon=True)
        self._t_out.start()
        self._t_err.start()

    def _read_out(self) -> None:
        with contextlib.suppress(Exception):
            for raw in self.p.stdout:
                self.lines.put(raw.decode("utf-8", "replace").rstrip("\r\n"))

    def _read_err(self) -> None:
        with contextlib.suppress(Exception):
            for raw in self.p.stderr:
                self.err += raw

    def err_text(self) -> str:
        return self.err.decode("utf-8", "replace")

    def wait_json(self, event: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"hết {timeout} s chưa có {event}: {self.out!r} {self.err_text()}")
            try:
                line = self.lines.get(timeout=min(left, 0.5))
            except queue.Empty:
                if self.p.poll() is not None and self.lines.empty():
                    detail = f"{self.out!r} {self.err_text()}"
                    raise AssertionError(
                        f"tiến trình đã thoát ({self.p.returncode}) trước {event}: {detail}"
                    ) from None
                continue
            self.out.append(line)
            with contextlib.suppress(ValueError):
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("event") == event:
                    return obj

    def write(self, text: str) -> None:
        self.p.stdin.write(text.encode("utf-8"))
        self.p.stdin.flush()

    def interrupt(self) -> None:
        if sys.platform == "win32":
            os.kill(self.p.pid, signal.CTRL_BREAK_EVENT)
        else:
            self.p.send_signal(signal.SIGINT)

    def finish(self, timeout: float = 30.0) -> int:
        code = self.p.wait(timeout)
        self._t_out.join(5)
        self._t_err.join(5)
        while not self.lines.empty():
            self.out.append(self.lines.get())
        return code

    def kill(self) -> None:
        if self.p.poll() is None:
            self.p.kill()
            with contextlib.suppress(Exception):
                self.p.wait(10)
        for stream in (self.p.stdin, self.p.stdout, self.p.stderr):
            with contextlib.suppress(Exception):
                stream.close()
        self._t_out.join(5)
        self._t_err.join(5)


@pytest.fixture
def procs():
    started: list[Proc] = []

    def start(argv: list[str]) -> Proc:
        proc = Proc(argv)
        started.append(proc)
        return proc

    yield start

    for proc in started:
        proc.kill()


def listen_cmd(d) -> list[str]:
    return ["--config-dir", str(d), "listen", "--json", "--name", "B", *NODE_OPTS]


def pair_cmd(d, port: int, *, json_mode: bool = True) -> list[str]:
    mode = ["--json"] if json_mode else []
    return ["--config-dir", str(d), "pair", f"{LOCAL}:{port}", *mode, "--name", "A", *NODE_OPTS]


@pytest.mark.integration
def test_cl13_subprocess_end_to_end(tmp_path, procs):
    listen = procs(listen_cmd(tmp_path / "B"))
    ev = listen.wait_json("listening")
    pair = procs(pair_cmd(tmp_path / "A", ev["port"]))
    pa = pair.wait_json("pair_prompt")
    pb = listen.wait_json("pair_prompt")
    assert pa["code"] == pb["code"]
    pair.write("c\n")
    listen.write("c\n")
    assert pair.finish(30) == 0, pair.err_text()
    assert listen.wait_json("pair_result")["outcome"] == "paired"
    assert TrustStore.load(tmp_path / "A").get(ev["device_id"]) is not None
    assert (tmp_path / "B" / "trusted.json").exists()
    assert len(TrustStore.load(tmp_path / "B").devices()) == 1
    listen.interrupt()
    assert listen.finish(30) == 0


@pytest.mark.integration
def test_cl14_interrupt_stops_listen_cleanly(tmp_path, procs):
    d = tmp_path / "B"
    listen = procs(listen_cmd(d))
    listen.wait_json("listening")
    time.sleep(0.3)  # để thread đọc stdin đã chặn trong os.read
    listen.interrupt()
    code = listen.finish(30)
    err = listen.err_text()
    assert code == 0, err
    last = json.loads([line for line in listen.out if line.strip()][-1])
    assert last["event"] == "stopped"
    assert TIME_RE.match(last["time"])
    lock = NodeLock(d)
    lock.acquire(timeout=2)
    lock.release()
    assert "Fatal Python error" not in err
    assert "Traceback" not in err


@pytest.mark.integration
def test_cl14_interrupt_while_prompt_is_open(tmp_path, procs):
    """3.9 mục 2: Ctrl+C khi đang hỏi → listen thoát 0, máy kia nhận pair_abort{user}."""
    listen = procs(listen_cmd(tmp_path / "B"))
    ev = listen.wait_json("listening")
    pair = procs(pair_cmd(tmp_path / "A", ev["port"]))
    pair.wait_json("pair_prompt")
    listen.wait_json("pair_prompt")
    listen.interrupt()
    assert listen.finish(30) == 0
    result = pair.wait_json("pair_result")
    assert result["outcome"] == "peer_rejected"
    assert pair.finish(30) == 1
    err = listen.err_text()
    assert "Fatal Python error" not in err and "Traceback" not in err


@pytest.mark.integration
def test_cl14_interrupt_pair_while_waiting(tmp_path, procs):
    """`pair` bị Ctrl+C khi đang chờ người dùng → in “Đã hủy ghép đôi.”, mã 1; máy kia PEER_REJECTED."""
    listen = procs(listen_cmd(tmp_path / "B"))
    ev = listen.wait_json("listening")
    pair = procs(pair_cmd(tmp_path / "A", ev["port"], json_mode=False))
    listen.wait_json("pair_prompt")
    time.sleep(0.3)
    pair.interrupt()
    assert pair.finish(30) == 1
    text = "\n".join(pair.out) + pair.err_text()
    assert "Đã hủy ghép đôi." in text
    assert "Traceback" not in text and "Fatal Python error" not in text
    assert listen.wait_json("pair_result")["outcome"] == "peer_rejected"


@pytest.mark.integration
def test_cl15_unpair_while_listen_runs(tmp_path, procs, capsys, monkeypatch):
    from teleport.app import node as node_mod

    monkeypatch.setattr(node_mod, "LOCK_TIMEOUT", 0.2)
    d = tmp_path / "B"
    listen = procs(listen_cmd(d))
    listen.wait_json("listening")
    assert run_main(["--config-dir", str(d), "unpair", "B"]) == 1
    assert LOCKED_UNPAIR in capsys.readouterr().err
    listen.interrupt()
    assert listen.finish(30) == 0


# --- StdinReader (mục 2.6, 0 điểm 12) -------------------------------------------------------------------


async def test_stdin_reader_without_stdin_sends_eof(monkeypatch):
    from teleport.cli import StdinReader

    monkeypatch.setattr(sys, "stdin", None)
    lines: asyncio.Queue = asyncio.Queue()
    StdinReader(asyncio.get_running_loop(), lines).start()
    ts, line = await asyncio.wait_for(lines.get(), 2)
    assert line is None
    assert isinstance(ts, float)


def _stdin_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "teleport-stdin"]


async def test_stdin_reader_reads_lines_from_pipe(monkeypatch):
    from teleport.cli import StdinReader

    r, w = os.pipe()
    fake_stdin = os.fdopen(r, "rb", buffering=0)
    try:
        monkeypatch.setattr(sys, "stdin", fake_stdin)
        lines: asyncio.Queue = asyncio.Queue()
        t0 = time.perf_counter()
        StdinReader(asyncio.get_running_loop(), lines).start()
        threads = _stdin_threads()
        assert threads and all(t.daemon for t in threads)
        os.write(w, b"c\nx y\n\xff\n" + "không\n".encode())
        os.close(w)
        w = None
        got = []
        while True:
            ts, line = await asyncio.wait_for(lines.get(), 3)
            got.append((ts, line))
            if line is None:
                break
        assert [line for _, line in got] == ["c", "x y", "\ufffd", "không", None]
        stamps = [ts for ts, _ in got]
        # Mốc là `time.perf_counter()` lúc đọc (sửa F2): nằm giữa lúc bắt đầu và bây giờ.
        assert stamps == sorted(stamps) and stamps[0] >= t0
        assert stamps[-1] <= time.perf_counter()
        for t in threads:
            t.join(2)
            assert not t.is_alive()
    finally:
        if w is not None:
            os.close(w)
        fake_stdin.close()


async def test_stdin_reader_queue_is_bounded(monkeypatch):
    from teleport.cli import StdinReader

    r, w = os.pipe()
    fake_stdin = os.fdopen(r, "rb", buffering=0)
    try:
        monkeypatch.setattr(sys, "stdin", fake_stdin)
        lines: asyncio.Queue = asyncio.Queue()
        StdinReader(asyncio.get_running_loop(), lines).start()
        threads = _stdin_threads()
        os.write(w, b"".join(f"{i}\n".encode() for i in range(300)))
        os.close(w)
        w = None
        deadline = time.monotonic() + 5
        while any(t.is_alive() for t in threads) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.05)
        assert lines.qsize() <= 101
        assert lines.get_nowait()[1] == "0"
    finally:
        if w is not None:
            os.close(w)
        fake_stdin.close()


# =================================================================================================
# PR-4: `identity` (cổng, thư mục là file), `list`, `pair <tên>`, `listen` với mDNS
# (đặc tả PR-4 mục 2.7, 3.10, 4.8: CL16–CL30). `MdnsDiscovery` được thay bằng bản giả: fixture của
# tests/app/conftest.py không dùng được ở đây (mỗi thư mục một conftest), nên bản giả gọn được chép lại.
# =================================================================================================

MVP_NO_DEVICES = (
    "Chưa thấy thiết bị nào. Hãy mở Teleport trên máy kia và kiểm tra hai máy dùng chung một mạng Wi-Fi."
)
MVP_REINSTALL = "Có một {x} mới chưa ghép đôi. Nếu máy đó vừa cài lại Teleport, hãy ghép đôi lại."
MVP_SEEN_UNREACHABLE = (
    "Thấy {x} nhưng không kết nối được. Mạng Wi-Fi này có thể đang chặn các thiết bị nói chuyện với nhau "
    "(hay gặp ở quán cà phê, khách sạn)."
)
HOTSPOT_HINT = (
    "Hãy thử dùng điểm phát Wi-Fi từ điện thoại, và kiểm tra tường lửa trên máy kia đã cho phép Teleport."
)
MVP_REJECTED = "{x} từ chối kết nối từ máy này. Có thể máy đó đã hủy ghép đôi."
MVP_IDENTITY = (
    "{x} đang dùng một danh tính khác với lúc ghép đôi. Có thể có người đang giả mạo máy này. "
    "Kết nối đã bị chặn."
)
MDNS_OFF = (
    "Không tìm được thiết bị theo tên vì khám phá mDNS đang tắt. "
    "Hãy nhập địa chỉ IP: teleport pair <ip[:cổng]>"
)
MDNS_HINT = "Nếu mạng chặn mDNS, hãy nhập địa chỉ IP: teleport pair <ip[:cổng]>"
LISTEN_DISCOVERY = "Máy khác trong mạng thấy máy này qua `teleport list` và tự kết nối nếu đã ghép đôi."
MDNS_OPTS = ["--mdns-interface", LOCAL]


def new_identity(directory):
    return load_or_create_identity(directory)


def discovered(device_id: str, name: str, port: int, *, addresses=(LOCAL,), os: str = "windows"):
    from teleport.core.discovery import DiscoveredDevice
    from teleport.core.messages import PROTOCOL_VERSION

    return DiscoveredDevice(
        device_id=device_id, name=name, os=os, proto=PROTOCOL_VERSION, port=port, addresses=tuple(addresses)
    )


class CliFakeMdnsControl:
    def __init__(self) -> None:
        self.instances: list = []
        self.fail_start: BaseException | None = None
        self.fail_advertise: BaseException | None = None
        self.seed: dict = {}

    @property
    def last(self):
        assert self.instances, "chưa có MdnsDiscovery nào được tạo"
        return self.instances[-1]


class CliFakeMdns:
    """Bản giả `MdnsDiscovery` (như `FakeMdns` của tests/app): không socket; thiết bị trong `seed`
    được báo `DeviceAdded` ngay sau `start_browsing`."""

    def __init__(self, ctl: CliFakeMdnsControl, **kwargs) -> None:
        self.ctl = ctl
        self.kwargs = kwargs
        self.calls: list[str] = []
        self.advertised: list[dict] = []
        self.closing = False
        self.on_event = None
        self._devices: dict = {}
        ctl.instances.append(self)

    async def start(self) -> None:
        self.calls.append("start")
        if self.ctl.fail_start is not None:
            raise self.ctl.fail_start

    async def close(self) -> None:
        self.calls.append("close")
        self.closing = True

    async def advertise(self, **kw) -> None:
        self.calls.append("advertise")
        if self.ctl.fail_advertise is not None:
            raise self.ctl.fail_advertise
        self.advertised.append(kw)

    async def stop_advertising(self) -> None:
        self.calls.append("stop_advertising")

    async def start_browsing(self, *, self_device_id: str, on_event, max_services: int = 256) -> None:
        from teleport.core.discovery import DeviceAdded

        self.calls.append("start_browsing")
        self.on_event = on_event
        loop = asyncio.get_running_loop()
        for d in self.ctl.seed.values():
            if d.device_id != self_device_id:
                loop.call_soon(self.emit, DeviceAdded(d))

    def set_priority(self, device_ids) -> None:
        self.calls.append("set_priority")
        list(device_ids)

    @property
    def devices(self):
        return dict(self._devices)

    def emit(self, event) -> None:
        from teleport.core.discovery import DeviceRemoved

        if self.closing:
            return
        if isinstance(event, DeviceRemoved):
            self._devices.pop(event.device.device_id, None)
        else:
            self._devices[event.device.device_id] = event.device
        if self.on_event is not None:
            self.on_event(event)


@pytest.fixture
def cli_mdns(monkeypatch):
    """Thay `teleport.app.node.MdnsDiscovery`; địa chỉ card mạng cố định (không khởi động lại mDNS)."""
    from teleport.app import node as node_mod
    from teleport.net import discovery as net_discovery

    ctl = CliFakeMdnsControl()
    monkeypatch.setattr(node_mod, "MdnsDiscovery", lambda **kw: CliFakeMdns(ctl, **kw))
    monkeypatch.setattr(net_discovery, "local_ipv4_addresses", lambda: ("192.0.2.5",))
    return ctl


@pytest.fixture
def open_control_calls(monkeypatch):
    """Đếm lời gọi `teleport.app.node.open_control` (node tự mở kết nối điều khiển)."""
    from teleport.app import node as node_mod

    calls: list = []
    real = node_mod.open_control

    async def counting(*args, **kwargs):
        calls.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(node_mod, "open_control", counting)
    return calls


@pytest.fixture
def fast_links(fast_cli, monkeypatch):
    """`fast_cli` + hằng của `teleport.core.links` và `NETWORK_CHECK_INTERVAL` (như `fast` của tests/app)."""
    from teleport.app import node as node_mod
    from teleport.core import links

    for name, value in {
        "RETRY_BASE": 0.2,
        "RETRY_MAX": 1.0,
        "EVENT_RETRY_GAP": 0.2,
        "REPLACE_AFTER": 2.0,
    }.items():
        monkeypatch.setattr(links, name, value)
    monkeypatch.setattr(node_mod, "NETWORK_CHECK_INTERVAL", 0.2)


@pytest.fixture
def browse_fast(monkeypatch):
    from teleport import cli

    monkeypatch.setattr(cli, "_PAIR_BROWSE_TIMEOUT", 0.3)


async def run_list_capture(argv: list[str]) -> tuple[int, str, str]:
    from teleport.cli import run_list

    out, err = io.StringIO(), io.StringIO()
    code = await asyncio.wait_for(run_list(build_parser().parse_args(argv), out, err), 15)
    return code, out.getvalue(), err.getvalue()


def list_argv(d, *extra: str) -> list[str]:
    return ["--config-dir", str(d), "list", *MDNS_OPTS, "--timeout", "0.3", *extra]


def name_pair_argv(d, target: str, *, json_mode: bool = False, name: str = "A") -> list[str]:
    mode = ["--json"] if json_mode else []
    return ["--config-dir", str(d), "pair", target, *mode, "--name", name, *NODE_OPTS, *MDNS_OPTS]


def mdns_listen_argv(d, *, json_mode: bool = True, name: str = "A") -> list[str]:
    mode = ["--json"] if json_mode else []
    return ["--config-dir", str(d), "listen", *mode, "--name", name, *NODE_OPTS, *MDNS_OPTS]


# --- CL16, CL17: identity ------------------------------------------------------------------------------


def test_cl16_identity_with_config_dir_that_is_a_file(config_is_file, capsys):
    assert run_main_guarded(["--config-dir", str(config_is_file), "identity"]) == 1
    captured = capsys.readouterr()
    assert f"Lỗi: {CONFIG_DIR_ERROR} {config_is_file}" in captured.err, captured.err
    assert "Traceback" not in captured.err + captured.out
    assert config_is_file.read_bytes() == b"khong phai thu muc"


def test_cl17_identity_shows_port(tmp_path, capsys):
    d = tmp_path / "cfg"
    assert run_main(["--config-dir", str(d), "identity"]) == 0
    assert re.search(r"^Cổng:\s+47474$", capsys.readouterr().out, re.M)
    from teleport.app.settings import Settings

    Settings(port=47480).save(d)
    assert run_main(["--config-dir", str(d), "identity"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"^Cổng:\s+47480$", out, re.M), out
    assert "Fingerprint:" in out


# --- CL18: tham số của list ----------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "abc", "61", "nan", "inf", "-1", "60.5"])
def test_cl18_list_bad_timeout_exits_2(tmp_path, value):
    d = tmp_path / "cfg"
    assert run_main_guarded(["--config-dir", str(d), "list", "--timeout", value]) == 2
    assert not d.exists() or not (d / "node.lock").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["list", "--mdns-interface", "localhost"],
        ["list", "--mdns-interface", "1.2.3"],
        ["listen", "--mdns-interface", "::1", *NODE_OPTS],
        ["listen", "--advertise-address", "192.168.1", *NODE_OPTS],
        ["pair", "B", "--mdns-interface", "x", *NODE_OPTS],
    ],
)
def test_hidden_mdns_options_must_be_ipv4(tmp_path, argv):
    d = tmp_path / "cfg"
    assert run_main_guarded(["--config-dir", str(d), *argv]) == 2
    assert not (d / "node.lock").exists()


def test_list_help_hides_mdns_options(capsys):
    with pytest.raises(SystemExit):
        main(["list", "--help"])
    out = capsys.readouterr().out
    assert "--timeout" in out and "--json" in out
    assert "--mdns-interface" not in out
    with pytest.raises(SystemExit):
        main(["listen", "--help"])
    out = capsys.readouterr().out
    assert "--mdns-interface" not in out and "--advertise-address" not in out


# --- CL19–CL22: list (FakeMdns, không socket TCP) ------------------------------------------------------


@pytest.fixture
def list_cfg(tmp_path):
    """Máy này + B (đã ghép đôi, thấy), C (chưa ghép đôi, thấy), D (đã ghép đôi, không thấy)."""
    d = tmp_path / "cfg"
    me = load_or_create_identity(d)
    peers = {k: load_or_create_identity(tmp_path / f"peer-{k}") for k in "BCD"}
    store = TrustStore.load(d)
    store.add(peer_cert_from_pem(peers["B"].cert_pem()), "B")
    store.add(peer_cert_from_pem(peers["D"].cert_pem()), "D")
    seed = {
        peers["B"].device_id: discovered(
            peers["B"].device_id, "B-ten-txt", 47474, addresses=("192.0.2.10", "192.0.2.11")
        ),
        peers["C"].device_id: discovered(
            peers["C"].device_id, "C", 50000, addresses=("192.0.2.20",), os="macos"
        ),
    }
    return d, me, peers, seed


async def test_cl19_list_text(list_cfg, cli_mdns):
    from teleport.app.node import default_device_name

    d, me, peers, seed = list_cfg
    cli_mdns.seed = seed
    code, out, err = await run_list_capture(list_argv(d))
    assert code == 0, err
    b8, c8, d8 = (peers[k].device_id[:8] for k in "BCD")
    lines = out.splitlines()
    assert f"Máy này: {default_device_name(me)} ({me.device_id[:8]}), cổng 47474" in lines, out
    i = lines.index("Thiết bị trong mạng:")
    assert lines[i + 1] == f"  B  {b8}  Windows  đã ghép đôi  192.0.2.10:47474, 192.0.2.11:47474", out
    assert lines[i + 2] == f"  C  {c8}  macOS  chưa ghép đôi  192.0.2.20:50000", out
    j = lines.index("Thiết bị đã ghép đôi nhưng không thấy trong mạng:")
    assert j > i + 2
    assert lines[j + 1] == f"  D  {d8}", out
    assert "B-ten-txt" not in out, "tên mDNS không được thay tên đã ghép đôi"
    m = cli_mdns.last
    assert "advertise" not in m.calls, "list không được quảng bá"
    assert m.closing
    assert not (d / "node.lock").exists(), "list không được đụng node.lock"
    assert lock_is_free(d)


async def test_cl20_list_json_single_object(list_cfg, cli_mdns):
    d, me, peers, seed = list_cfg
    cli_mdns.seed = seed
    code, out, err = await run_list_capture(list_argv(d, "--json"))
    assert code == 0 and err == ""
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, out
    obj = json.loads(lines[0])
    assert TIME_RE.match(obj["time"]), obj["time"]
    assert obj["self"]["device_id"] == me.device_id
    assert obj["self"]["port"] == 47474
    assert isinstance(obj["self"]["name"], str) and obj["self"]["name"]
    devs = {x["device_id"]: x for x in obj["devices"]}
    assert set(devs) == {peers[k].device_id for k in "BCD"}
    keys = {"device_id", "name", "os", "paired", "online", "addresses", "port"}
    assert all(set(x) == keys for x in obj["devices"]), obj["devices"]
    b, c, dd = (devs[peers[k].device_id] for k in "BCD")
    assert b == {
        "device_id": peers["B"].device_id,
        "name": "B",
        "os": "windows",
        "paired": True,
        "online": True,
        "addresses": ["192.0.2.10", "192.0.2.11"],
        "port": 47474,
    }
    assert (c["paired"], c["online"], c["os"], c["port"]) == (False, True, "macos", 50000)
    assert (dd["paired"], dd["online"], dd["os"], dd["addresses"], dd["port"]) == (
        True,
        False,
        None,
        [],
        None,
    )


async def test_cl21_list_nothing_seen(tmp_path, cli_mdns):
    code, out, err = await run_list_capture(list_argv(tmp_path / "cfg"))
    assert code == 0, err
    assert MVP_NO_DEVICES in out
    assert "Thiết bị trong mạng:" not in out
    assert "không thấy trong mạng" not in out


async def test_cl21_list_reinstall_hint(tmp_path, cli_mdns):
    d = tmp_path / "cfg"
    load_or_create_identity(d)
    old = load_or_create_identity(tmp_path / "cu")
    new = load_or_create_identity(tmp_path / "moi")
    TrustStore.load(d).add(peer_cert_from_pem(old.cert_pem()), "PC-Nha")
    cli_mdns.seed = {new.device_id: discovered(new.device_id, "PC-Nha", 47474, addresses=("192.0.2.30",))}
    code, out, err = await run_list_capture(list_argv(d))
    assert code == 0, err
    assert MVP_REINSTALL.format(x="PC-Nha") in out, out
    assert "*" not in out


async def test_cl21_no_reinstall_hint_when_paired_device_is_online(tmp_path, cli_mdns):
    d = tmp_path / "cfg"
    load_or_create_identity(d)
    old = load_or_create_identity(tmp_path / "cu")
    other = load_or_create_identity(tmp_path / "khac")
    TrustStore.load(d).add(peer_cert_from_pem(old.cert_pem()), "PC-Nha")
    cli_mdns.seed = {
        old.device_id: discovered(old.device_id, "PC-Nha", 47474, addresses=("192.0.2.31",)),
        other.device_id: discovered(other.device_id, "Laptop", 47474, addresses=("192.0.2.30",)),
    }
    code, out, _ = await run_list_capture(list_argv(d))
    assert code == 0
    assert "mới chưa ghép đôi" not in out


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
async def test_cl22_list_discovery_error(tmp_path, cli_mdns, json_mode):
    cli_mdns.fail_start = OSError("giả lập không mở được socket mDNS")
    code, out, err = await run_list_capture(list_argv(tmp_path / "cfg", *(["--json"] if json_mode else [])))
    assert code == 1
    assert "Traceback" not in out + err
    if json_mode:
        obj = json.loads(out.strip())
        assert obj["error"] == "discovery_error"
        assert isinstance(obj["message"], str) and obj["message"]
    else:
        assert err.startswith("Lỗi: "), err


@pytest.mark.parametrize("case", ["config_file", "corrupt_store", "corrupt_identity"])
async def test_list_runtime_errors_exit_1(tmp_path, cli_mdns, case):
    expected = {
        "config_file": "config_dir_error",
        "corrupt_store": "trust_store_error",
        "corrupt_identity": "identity_error",
    }[case]
    d = tmp_path / "cfg"
    if case == "config_file":
        d.write_bytes(b"x")
    else:
        load_or_create_identity(d)
        target = d / ("trusted.json" if case == "corrupt_store" else "device.crt")
        target.write_bytes(b"rac")
    code, out, err = await run_list_capture(list_argv(d, "--json"))
    assert code == 1
    assert json.loads(out.strip())["error"] == expected
    code, out, err = await run_list_capture(list_argv(d))
    assert code == 1 and err.startswith("Lỗi: ") and "Traceback" not in err


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("TELEPORT_SKIP_MDNS") == "1", reason="TELEPORT_SKIP_MDNS=1")
def test_list_interrupted_prints_cancelled(tmp_path, procs):
    """`cmd_list`: Ctrl+C → “Đã hủy.”, mã 1, không traceback (mDNS thật chỉ trên loopback).

    Chờ tín hiệu sẵn sàng thay cho ngủ cố định: `list` tạo danh tính (`device.crt`) bên trong vòng lặp
    sự kiện, sau khi đã cài xử lý Ctrl+C/Ctrl+Break, nên lúc đó gửi Ctrl+C là đúng lúc lệnh đang chạy.
    """
    cfg = tmp_path / "cfg"
    proc = procs(["--config-dir", str(cfg), "list", "--timeout", "20", *MDNS_OPTS])
    deadline = time.monotonic() + 30.0
    while not (cfg / "device.crt").exists():
        assert proc.p.poll() is None, f"list đã thoát sớm: {proc.err_text()}"
        assert time.monotonic() < deadline, "hết 30 s mà list chưa tạo danh tính"
        time.sleep(0.05)
    time.sleep(0.2)  # cho lệnh đi tiếp vào phần duyệt mDNS
    assert proc.p.poll() is None, proc.err_text()
    proc.interrupt()
    code = proc.finish(30)
    text = "\n".join(proc.out) + proc.err_text()
    assert code == 1, text
    assert "Đã hủy." in text
    assert "Traceback" not in text


# --- CL23–CL26: pair theo tên ------------------------------------------------------------------------------


@with_marks(socket_test)
async def test_cl23_pair_by_name_then_by_prefix(
    tmp_path, fast_links, cli_mdns, browse_fast, open_control_calls, cli_runs
):
    listen = cli_runs("listen", listen_argv(tmp_path / "B"))  # B: 127.0.0.1, không mDNS
    ev = await listen.wait_event("listening")
    b_id, port = ev["device_id"], ev["port"]
    cli_mdns.seed = {b_id: discovered(b_id, "B", port)}

    pair = cli_runs("pair", name_pair_argv(tmp_path / "A", "B"))
    await pair.wait_text("Trùng khớp? [c/k]:")
    await listen.wait_event("pair_prompt")
    text = pair.text()
    assert "Đang tìm “B” trong mạng…" in text
    assert f"Đang kết nối tới “B” ({b_id[:8]}) tại 127.0.0.1:{port}…" in text
    pair.say("c")
    listen.say("c")
    assert await asyncio.wait_for(pair.task, 10) == 0, pair.text()
    assert TrustStore.load(tmp_path / "A").get(b_id) is not None
    assert cli_mdns.instances and all(m.advertised == [] for m in cli_mdns.instances), (
        "pair <tên> không quảng bá"
    )
    assert all("advertise" not in m.calls for m in cli_mdns.instances)

    n = len(cli_mdns.instances)
    pair2 = cli_runs("pair", name_pair_argv(tmp_path / "A", b_id[:6], json_mode=True))
    p2 = await pair2.wait_event("pair_prompt")
    assert p2["peer_device_id"] == b_id
    await listen.wait_event("pair_prompt", n=2)
    pair2.say("c")
    listen.say("c")
    assert await asyncio.wait_for(pair2.task, 10) == 0, pair2.text()
    assert pair2.events_of("pair_result")[0]["outcome"] == "paired"
    assert len(cli_mdns.instances) == n + 1
    assert open_control_calls == [], "pair <tên> không được tự mở kết nối điều khiển"


@with_marks(socket_test)
async def test_cl24_pair_by_name_not_found(tmp_path, fast_links, cli_mdns, browse_fast, cli_runs):
    c = load_or_create_identity(tmp_path / "peer-C")
    cli_mdns.seed = {c.device_id: discovered(c.device_id, "C", 47474, addresses=("192.0.2.20",))}
    pair = cli_runs("pair", name_pair_argv(tmp_path / "A", "khong-co"))
    assert await asyncio.wait_for(pair.task, 10) == 1
    text = pair.text()
    assert "Lỗi: Không thấy thiết bị nào khớp “khong-co” trong mạng." in text, text
    assert "Thiết bị đang thấy:" in text
    assert re.search(rf"^  {c.device_id[:8]}  C$", text, re.M), text
    assert MDNS_HINT in text

    pair = cli_runs("pair", name_pair_argv(tmp_path / "A", "khong-co", json_mode=True))
    assert await asyncio.wait_for(pair.task, 10) == 1
    [obj] = pair.events_of("error")
    assert obj["error"] == "not_found"
    assert obj["candidates"] == [{"device_id": c.device_id, "name": "C"}]


@with_marks(socket_test)
async def test_cl24_pair_by_name_nothing_seen(tmp_path, fast_links, cli_mdns, browse_fast, cli_runs):
    pair = cli_runs("pair", name_pair_argv(tmp_path / "A", "B"))
    assert await asyncio.wait_for(pair.task, 10) == 1
    text = pair.text()
    assert "(chưa thấy thiết bị nào)" in text
    assert MDNS_HINT in text


@with_marks(socket_test)
async def test_cl24_pair_by_name_ambiguous(tmp_path, fast_links, cli_mdns, browse_fast, cli_runs):
    x = load_or_create_identity(tmp_path / "peer-X")
    y = load_or_create_identity(tmp_path / "peer-Y")
    cli_mdns.seed = {
        x.device_id: discovered(x.device_id, "pc", 47474, addresses=("192.0.2.21",)),
        y.device_id: discovered(y.device_id, "PC", 47474, addresses=("192.0.2.22",)),
    }
    pair = cli_runs("pair", name_pair_argv(tmp_path / "A", "Pc"))
    assert await asyncio.wait_for(pair.task, 10) == 1
    text = pair.text()
    assert "Lỗi: “Pc” khớp nhiều thiết bị; hãy nhập mã thiết bị dài hơn:" in text, text
    assert re.search(rf"^  {x.device_id}  pc$", text, re.M), text
    assert re.search(rf"^  {y.device_id}  PC$", text, re.M), text

    pair = cli_runs("pair", name_pair_argv(tmp_path / "A", "pc", json_mode=True))
    assert await asyncio.wait_for(pair.task, 10) == 1
    [obj] = pair.events_of("error")
    assert obj["error"] == "ambiguous"
    assert sorted(c["device_id"] for c in obj["candidates"]) == sorted([x.device_id, y.device_id])


@with_marks(socket_test)
async def test_cl24_pair_by_name_paired_but_offline(tmp_path, fast_links, cli_mdns, browse_fast, cli_runs):
    d = tmp_path / "A"
    load_or_create_identity(d)
    dd = load_or_create_identity(tmp_path / "peer-D")
    TrustStore.load(d).add(peer_cert_from_pem(dd.cert_pem()), "D")
    pair = cli_runs("pair", name_pair_argv(d, "D"))
    assert await asyncio.wait_for(pair.task, 10) == 1
    text = pair.text()
    assert f"Lỗi: “D” ({dd.device_id[:8]}) không có trong mạng lúc này. {MDNS_HINT}" in text, text

    pair = cli_runs("pair", name_pair_argv(d, "D", json_mode=True))
    assert await asyncio.wait_for(pair.task, 10) == 1
    assert pair.events_of("error")[0]["error"] == "offline"


@with_marks(socket_test)
async def test_cl25_seen_but_unreachable(tmp_path, fast_links, cli_mdns, browse_fast, cli_runs):
    b = load_or_create_identity(tmp_path / "peer-B")
    s = socket.socket()
    s.bind((LOCAL, 0))
    closed_port = s.getsockname()[1]
    s.close()
    cli_mdns.seed = {b.device_id: discovered(b.device_id, "B", closed_port)}
    pair = cli_runs("pair", name_pair_argv(tmp_path / "A", "B"))
    assert await asyncio.wait_for(pair.task, 15) == 1
    text = pair.text()
    assert MVP_SEEN_UNREACHABLE.format(x="B") in text, text
    assert HOTSPOT_HINT in text


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_cl26_pair_by_name_without_mdns(tmp_path, capsys, json_mode):
    d = tmp_path / "cfg"
    argv = ["--config-dir", str(d), "pair", "B", *(["--json"] if json_mode else []), *NODE_OPTS]
    assert run_main_guarded(argv) == 1
    captured = capsys.readouterr()
    if json_mode:
        obj = json.loads(captured.out.strip())
        assert obj["error"] == "discovery_error"
        assert obj["message"] == MDNS_OFF
    else:
        assert MDNS_OFF in captured.err, captured.err
    assert not (d / "node.lock").exists()


@with_marks(socket_test)
async def test_pair_by_name_when_mdns_cannot_start(tmp_path, fast_links, cli_mdns, cli_runs):
    cli_mdns.fail_start = OSError("giả lập")
    pair = cli_runs("pair", name_pair_argv(tmp_path / "A", "B"))
    assert await asyncio.wait_for(pair.task, 10) == 1
    text = pair.text()
    assert "Không tìm được thiết bị theo tên vì không bật được khám phá mDNS (" in text, text
    assert "teleport pair <ip[:cổng]>" in text
    pair = cli_runs("pair", name_pair_argv(tmp_path / "A", "B", json_mode=True))
    assert await asyncio.wait_for(pair.task, 10) == 1
    assert pair.events_of("error")[0]["error"] == "discovery_error"


def test_pair_by_name_bad_name_option_exits_2(tmp_path):
    d = tmp_path / "cfg"
    argv = ["--config-dir", str(d), "pair", "B", "--name", "  x", *NODE_OPTS, *MDNS_OPTS]
    assert run_main_guarded(argv) == 2
    assert not (d / "node.lock").exists()


@with_marks(socket_test)
async def test_pair_by_ip_does_not_use_mdns(tmp_path, fast_links, cli_mdns, cli_runs):
    """Chốt thêm 8: `pair <ip>` không bật mDNS, kể cả khi có `--mdns-interface`."""
    s = socket.socket()
    s.bind((LOCAL, 0))
    port = s.getsockname()[1]
    s.close()
    argv = [
        "--config-dir",
        str(tmp_path / "A"),
        "pair",
        f"{LOCAL}:{port}",
        "--name",
        "A",
        *NODE_OPTS,
        *MDNS_OPTS,
    ]
    pair = cli_runs("pair", argv)
    assert await asyncio.wait_for(pair.task, 15) == 1
    assert cli_mdns.instances == []


@pytest.fixture
def fake_open_pairing(monkeypatch):
    """Thay `open_pairing` của `teleport.app.pairing`: ghi (địa chỉ, cổng) rồi báo không kết nối được.

    Nhờ vậy `pair 1.2.3.4…` không gửi gói nào ra mạng thật."""
    from teleport.app import pairing as app_pairing
    from teleport.net.connection import Unreachable

    calls: list[tuple[list[str], int]] = []

    async def fake(addresses, port, hello):
        calls.append((list(addresses), port))
        raise Unreachable("giả lập: không kết nối được", address=next(iter(addresses)))

    monkeypatch.setattr(app_pairing, "open_pairing", fake)
    return calls


@with_marks(socket_test)
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("1.2.3.4\xa0:47474", (["1.2.3.4"], 47474)),
        ("1.2.3.4\xa0", (["1.2.3.4"], 47474)),
        ("\xa01.2.3.4:50000\xa0", (["1.2.3.4"], 50000)),
        ("\N{EM SPACE}1.2.3.4\t", (["1.2.3.4"], 47474)),
    ],
    ids=["nbsp-before-port", "nbsp-trailing", "nbsp-both-ends", "unicode-space"],
)
async def test_pair_target_with_unicode_whitespace_is_an_address(
    tmp_path, fast_links, cli_mdns, fake_open_pairing, cli_runs, target, expected
):
    """Sửa F6: dán địa chỉ từ web (NBSP, khoảng trắng Unicode) vẫn được hiểu là địa chỉ, không duyệt mDNS."""
    pair = cli_runs("pair", name_pair_argv(tmp_path / "A", target))
    t0 = time.monotonic()
    assert await asyncio.wait_for(pair.task, 15) == 1, pair.text()
    assert fake_open_pairing == [expected], pair.text()
    assert cli_mdns.instances == [], "đích là địa chỉ: không được bật mDNS để tìm theo tên"
    assert "Đang tìm" not in pair.text()
    assert time.monotonic() - t0 < 3.0


@pytest.mark.parametrize(
    "target", ["", "   ", "\xa0", " \t\N{IDEOGRAPHIC SPACE} "], ids=["empty", "spaces", "nbsp", "mixed"]
)
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_pair_empty_target_exits_2_immediately(tmp_path, cli_mdns, target, json_mode):
    """Sửa F6: `pair ""` / chỉ khoảng trắng → lỗi cú pháp, mã 2 ngay (không duyệt mDNS 3 s,
    không chạy node)."""
    d = tmp_path / "cfg"
    argv = [
        "--config-dir",
        str(d),
        "pair",
        target,
        *(["--json"] if json_mode else []),
        *NODE_OPTS,
        *MDNS_OPTS,
    ]
    t0 = time.monotonic()
    assert run_main_guarded(argv) == 2
    assert time.monotonic() - t0 < 1.0
    assert cli_mdns.instances == []
    assert not (d / "node.lock").exists()


# --- Sửa G2, G3 (review PR-4 vòng 2): đích có khoảng trắng bên trong; Ctrl+C khi `pair` ------------
#
# G2: bỏ khoảng trắng hai đầu; còn khoảng trắng bên trong thì chỉ là địa chỉ khi bỏ hết khoảng trắng
# ra được IPv4[:cổng] HỢP LỆ, ngược lại là tên (tìm theo tên, không phải lỗi cú pháp mã 2).
# Không có khoảng trắng bên trong thì như cũ (trông như địa chỉ mà sai → mã 2: CL5).

G2_ADDRESSES = [
    ("1.2.3.4\xa0:47474", (["1.2.3.4"], 47474)),
    ("1.2.3. 4", (["1.2.3.4"], 47474)),
    ("192.168.1.5\N{IDEOGRAPHIC SPACE}", (["192.168.1.5"], 47474)),
    ("1 . 2 . 3 . 4 : 50000", (["1.2.3.4"], 50000)),
]
G2_NAMES = [
    "PC Nha",
    "a 1",
    "dead beef",
    "1 2",
    "Laptop 2.0",
    "Phòng 2:30",
    "12 . 5",
    "2024 . 1",
    "Cafe 2:1",
    "Bed 1:2",
    "Dad 10:30",
    "Cafe: 2",
    "A: B",
]


@pytest.fixture
def browse_instant(monkeypatch):
    from teleport import cli

    monkeypatch.setattr(cli, "_PAIR_BROWSE_TIMEOUT", 0.05)


@with_marks(socket_test)
@pytest.mark.parametrize(("target", "expected"), G2_ADDRESSES, ids=repr)
async def test_g2_target_with_inner_space_that_compacts_to_valid_ipv4_is_an_address(
    tmp_path, fast_links, cli_mdns, fake_open_pairing, cli_runs, target, expected
):
    pair = cli_runs("pair", name_pair_argv(tmp_path / "A", target, json_mode=True))
    assert await asyncio.wait_for(pair.task, 15) == 1, pair.text()
    assert fake_open_pairing == [expected], pair.text()
    assert cli_mdns.instances == [], "đích là địa chỉ: không được bật mDNS để tìm theo tên"
    assert [e["error"] for e in pair.events_of("error")] == []


@with_marks(socket_test)
@pytest.mark.parametrize("name", G2_NAMES, ids=repr)
async def test_g2_name_with_inner_space_is_searched_by_name(
    tmp_path, fast_links, cli_mdns, fake_open_pairing, browse_instant, cli_runs, name
):
    """Tên thiết bị hợp lệ có khoảng trắng bên trong không bị coi là địa chỉ sai (mã 2)."""
    pair = cli_runs("pair", name_pair_argv(tmp_path / "A", f"\xa0{name} ", json_mode=True))
    code = await asyncio.wait_for(pair.task, 15)
    [err] = pair.events_of("error")
    assert err["error"] == "not_found", pair.text()
    assert f"“{name}”" in err["message"], "tìm theo tên đã bỏ khoảng trắng hai đầu"
    assert code == 1
    assert cli_mdns.instances, "tìm theo tên phải bật mDNS"
    assert fake_open_pairing == []


@with_marks(socket_test)
@pytest.mark.parametrize("name", ["Cafe 2:1", "Laptop 2.0", "12 . 5"], ids=repr)
async def test_g2_name_with_inner_space_reaches_the_device_seen_under_that_name(
    tmp_path, fast_links, cli_mdns, fake_open_pairing, browse_instant, cli_runs, name
):
    c = load_or_create_identity(tmp_path / "peer-C")
    cli_mdns.seed = {c.device_id: discovered(c.device_id, name, 47474, addresses=("192.0.2.30",))}
    pair = cli_runs("pair", name_pair_argv(tmp_path / "A", name, json_mode=True))
    assert await asyncio.wait_for(pair.task, 15) == 1, pair.text()
    assert fake_open_pairing == [(["192.0.2.30"], 47474)], pair.text()


def _sigint_soon() -> None:
    """Ctrl+C thật trong tiến trình: SIGINT tới handler mà `asyncio.Runner` của lệnh đã cài."""
    asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGINT)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("target", "port"),
    [
        ("1.2.3.4\xa0:47474", 47474),
        ("\xa01.2.3.4:50000 ", 50000),
        ("1.2.3. 4", 47474),
    ],
    ids=repr,
)
def test_g3_ctrl_c_during_pair_by_address_reports_normalized_address(
    tmp_path, capsys, fast_cli, monkeypatch, target, port
):
    """G3: Ctrl+C khi `pair <địa chỉ> --json` đang kết nối → `pair_result.address` là IPv4 đã chuẩn
    hóa, không kèm cổng (như `pair_result` của đường thường, `PairingFinished.address`), không phải
    chuỗi thô người dùng gõ (còn NBSP, khoảng trắng)."""
    from teleport.app import pairing as app_pairing

    calls: list = []

    async def hang(addresses, port, hello):
        calls.append((list(addresses), port))
        _sigint_soon()
        await asyncio.Event().wait()

    monkeypatch.setattr(app_pairing, "open_pairing", hang)
    d = tmp_path / "A"
    argv = ["--config-dir", str(d), "pair", target, "--json", "--name", "A", *NODE_OPTS]
    assert run_main(argv) == 1
    out = capsys.readouterr().out
    assert calls == [(["1.2.3.4"], port)], f"lệnh chưa tới bước kết nối đúng địa chỉ: {out!r}"
    events = [json.loads(line) for line in out.splitlines() if line.strip()]
    [result] = [e for e in events if e.get("event") == "pair_result"]
    assert result["outcome"] == "cancelled"
    assert result["address"] == "1.2.3.4"
    assert lock_is_free(d)


@pytest.mark.integration
def test_g3_ctrl_c_during_pair_by_name_reports_stripped_name(
    tmp_path, capsys, fast_links, cli_mdns, monkeypatch
):
    """G3: Ctrl+C lúc đang tìm theo tên → `pair_result.address` là tên đã bỏ khoảng trắng hai đầu."""
    from teleport import cli
    from teleport.app import node as node_mod

    monkeypatch.setattr(cli, "_PAIR_BROWSE_TIMEOUT", 10.0)

    def factory(**kwargs):
        m = CliFakeMdns(cli_mdns, **kwargs)
        real = m.start_browsing

        async def start_browsing(**kw):
            await real(**kw)
            _sigint_soon()

        m.start_browsing = start_browsing
        return m

    monkeypatch.setattr(node_mod, "MdnsDiscovery", factory)
    d = tmp_path / "A"
    t0 = time.monotonic()
    assert run_main(name_pair_argv(d, "\xa0 PC Nha\N{IDEOGRAPHIC SPACE}", json_mode=True)) == 1
    assert time.monotonic() - t0 < 5.0, "Ctrl+C phải dừng ngay, không chờ hết thời gian duyệt"
    out = capsys.readouterr().out
    events = [json.loads(line) for line in out.splitlines() if line.strip()]
    [result] = [e for e in events if e.get("event") == "pair_result"]
    assert result["outcome"] == "cancelled"
    assert result["address"] == "PC Nha"
    assert lock_is_free(d)


# --- CL27–CL30: listen với mDNS ----------------------------------------------------------------------


async def start_plain_node(directory, name: str):
    from teleport.app.node import Node

    node = Node(directory, name=name, host=LOCAL, port=0)
    await node.start()
    return node


def trust_dirs(a, b, name_a: str = "A", name_b: str = "B"):
    ia, ib = load_or_create_identity(a), load_or_create_identity(b)
    TrustStore.load(a).add(peer_cert_from_pem(ib.cert_pem()), name_b)
    TrustStore.load(b).add(peer_cert_from_pem(ia.cert_pem()), name_a)
    return ia, ib


@with_marks(socket_test)
async def test_cl27_listen_text_with_discovery(tmp_path, fast_links, cli_mdns, cli_runs):
    from teleport.core.discovery import DeviceAdded, DeviceRemoved

    _ia, ib = trust_dirs(tmp_path / "A", tmp_path / "B")
    listen = cli_runs("listen", mdns_listen_argv(tmp_path / "A", json_mode=False))
    await listen.wait_text("Nhấn Ctrl+C để dừng.")
    head = listen.text()
    assert LISTEN_DISCOVERY in head
    assert head.index(LISTEN_DISCOVERY) < head.index("Nhấn Ctrl+C để dừng.")
    m = cli_mdns.last
    assert m.advertised, "listen phải quảng bá"
    x = load_or_create_identity(tmp_path / "peer-X")
    m.emit(DeviceAdded(discovered(x.device_id, "X", 47474, addresses=("192.0.2.30",))))
    await listen.wait_text(f"Thấy “X” ({x.device_id[:8]}, Windows, chưa ghép đôi) tại 192.0.2.30:47474.")
    m.emit(DeviceRemoved(discovered(x.device_id, "X", 47474, addresses=("192.0.2.30",))))
    await listen.wait_text(f"“X” ({x.device_id[:8]}) đã rời mạng.")

    b = await start_plain_node(tmp_path / "B", "B")
    try:
        m.emit(DeviceAdded(discovered(ib.device_id, "B", b.port)))
        await listen.wait_text(f"Thấy “B” ({ib.device_id[:8]}, Windows, đã ghép đôi) tại 127.0.0.1:{b.port}.")
        await listen.wait_text(f"Đã kết nối với “B” ({ib.device_id[:8]}).")
    finally:
        await b.close()
    await listen.wait_text(f"Mất kết nối với “B” ({ib.device_id[:8]}). Đang nối lại…")


@with_marks(socket_test)
async def test_cl27_listen_json_with_discovery(tmp_path, fast_links, cli_mdns, cli_runs):
    from teleport.core.discovery import DeviceAdded, DeviceRemoved

    _ia, ib = trust_dirs(tmp_path / "A", tmp_path / "B")
    listen = cli_runs("listen", mdns_listen_argv(tmp_path / "A"))
    ev = await listen.wait_event("listening")
    assert ev["discovery"] is True
    assert ev["discovery_error"] is None
    m = cli_mdns.last
    x = load_or_create_identity(tmp_path / "peer-X")
    m.emit(DeviceAdded(discovered(x.device_id, "X", 47474, addresses=("192.0.2.30",), os="linux")))
    on = await listen.wait_event("device_online")
    assert {"device_id", "name", "os", "paired", "addresses", "port", "time"} <= set(on)
    assert (on["device_id"], on["name"], on["os"], on["paired"]) == (x.device_id, "X", "linux", False)
    assert (on["addresses"], on["port"]) == (["192.0.2.30"], 47474)
    assert TIME_RE.match(on["time"])

    b = await start_plain_node(tmp_path / "B", "B")
    try:
        m.emit(DeviceAdded(discovered(ib.device_id, "B", b.port)))
        conn = await listen.wait_event("device_connected")
        assert (conn["device_id"], conn["name"], conn["outgoing"]) == (ib.device_id, "B", True)
        m.emit(DeviceRemoved(discovered(ib.device_id, "B", b.port)))
        off = await listen.wait_event("device_offline")
        assert (off["device_id"], off["name"]) == (ib.device_id, "B")
        await asyncio.sleep(0.3)
        assert listen.events_of("device_disconnected") == [], "mất mDNS không đóng kết nối đang sống"
    finally:
        await b.close()
    disc = await listen.wait_event("device_disconnected")
    assert (disc["device_id"], disc["name"]) == (ib.device_id, "B")
    for line in listen.out.getvalue().splitlines():
        json.loads(line)


@with_marks(socket_test)
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
async def test_cl28_listen_advertise_failure_warns_and_keeps_running(
    tmp_path, fast_links, cli_mdns, cli_runs, json_mode
):
    from teleport.net.discovery import DiscoveryError

    cli_mdns.fail_advertise = DiscoveryError("không có địa chỉ để quảng bá")
    listen = cli_runs("listen", mdns_listen_argv(tmp_path / "A", json_mode=json_mode))
    if json_mode:
        ev = await listen.wait_event("listening")
        assert ev["discovery"] is True
        assert isinstance(ev["discovery_error"], str) and ev["discovery_error"]
    else:
        await listen.wait_text("Nhấn Ctrl+C để dừng.")
        warning = (
            "Cảnh báo: chưa quảng bá được qua mDNS (không có địa chỉ để quảng bá); "
            "máy khác vẫn ghép đôi được bằng IP."
        )
        assert warning in listen.text(), listen.text()
        assert LISTEN_DISCOVERY not in listen.text()
    await asyncio.sleep(0.2)
    assert not listen.task.done()


@with_marks(socket_test)
async def test_cl28_listen_mdns_start_failure_warns(tmp_path, fast_links, cli_mdns, cli_runs):
    cli_mdns.fail_start = OSError("giả lập socket")
    listen = cli_runs("listen", mdns_listen_argv(tmp_path / "A", json_mode=False))
    await listen.wait_text("Nhấn Ctrl+C để dừng.")
    assert "Cảnh báo: chưa quảng bá được qua mDNS (" in listen.text()
    assert not listen.task.done()


@with_marks(socket_test)
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
async def test_cl29_listen_on_loopback_without_mdns_interface(
    tmp_path, fast_cli, cli_mdns, cli_runs, json_mode
):
    listen = cli_runs("listen", listen_argv(tmp_path / "B", json_mode=json_mode))
    if json_mode:
        ev = await listen.wait_event("listening")
        assert ev["discovery"] is False
        assert ev["discovery_error"] is None
    else:
        await listen.wait_text("Nhấn Ctrl+C để dừng.")
        assert LISTEN_DISCOVERY not in listen.text()
        assert "Cảnh báo" not in listen.text()
    assert cli_mdns.instances == []


@with_marks(socket_test)
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
async def test_cl30_listen_reports_rejected(tmp_path, fast_links, cli_mdns, cli_runs, json_mode):
    from teleport.core.discovery import DeviceAdded

    a = load_or_create_identity(tmp_path / "A")
    b_dir = tmp_path / "B"
    ib = load_or_create_identity(b_dir)
    TrustStore.load(tmp_path / "A").add(peer_cert_from_pem(ib.cert_pem()), "B")  # B không tin A
    del a
    listen = cli_runs("listen", mdns_listen_argv(tmp_path / "A", json_mode=json_mode))
    if json_mode:
        await listen.wait_event("listening")
    else:
        await listen.wait_text("Nhấn Ctrl+C để dừng.")
    b = await start_plain_node(b_dir, "B")
    try:
        cli_mdns.last.emit(DeviceAdded(discovered(ib.device_id, "B", b.port)))
        if json_mode:
            ev = await listen.wait_event("connect_failed")
            assert (ev["device_id"], ev["name"], ev["result"]) == (ib.device_id, "B", "rejected")
            assert MVP_REJECTED.format(x="B") in ev["message"]
        else:
            await listen.wait_text(MVP_REJECTED.format(x="B"))
            await listen.wait_text(f"Để ghép đôi lại: `teleport pair {ib.device_id[:8]}`")
        await asyncio.sleep(0.5)
        assert listen.text().count(MVP_REJECTED.format(x="B")) == 1
    finally:
        await b.close()


@with_marks(socket_test)
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
async def test_cl30_listen_reports_identity_mismatch(tmp_path, fast_links, cli_mdns, cli_runs, json_mode):
    from teleport.core.discovery import DeviceAdded

    load_or_create_identity(tmp_path / "A")
    ib = load_or_create_identity(tmp_path / "peer-B")
    TrustStore.load(tmp_path / "A").add(peer_cert_from_pem(ib.cert_pem()), "B")
    listen = cli_runs("listen", mdns_listen_argv(tmp_path / "A", json_mode=json_mode))
    if json_mode:
        await listen.wait_event("listening")
    else:
        await listen.wait_text("Nhấn Ctrl+C để dừng.")
    c = await start_plain_node(tmp_path / "C", "C")  # máy khác trả lời ở “địa chỉ của B”
    try:
        cli_mdns.last.emit(DeviceAdded(discovered(ib.device_id, "B", c.port)))
        if json_mode:
            ev = await listen.wait_event("connect_failed")
            assert (ev["device_id"], ev["name"], ev["result"]) == (ib.device_id, "B", "identity_mismatch")
            assert MVP_IDENTITY.format(x="B") in ev["message"]
        else:
            await listen.wait_text(MVP_IDENTITY.format(x="B"))
            await listen.wait_text(f"teleport unpair {ib.device_id[:8]}")
    finally:
        await c.close()


# =================================================================================================
# L1/L4 (kế hoạch "L1 và L4", mục cli.py): cảnh báo khi máy đang chờ đã hiện nhiều mã, `--json` có
# `recent_codes` + `warning`, câu báo `prompt_failed`. Câu chữ so nguyên văn với kế hoạch.
# =================================================================================================

WARN_CODES = (
    "Cảnh báo: máy này đã hiện {n} mã ghép đôi trong 1 phút qua. Nếu bạn không tự ghép đôi nhiều lần "
    "như vậy, có thể có người đang thử giả mạo: hãy từ chối (gõ k)."
)
MVP_PROMPT_FAILED = "Không hiện được mã ghép đôi trên máy này nên đã hủy ghép đôi. Hãy thử lại."
ASK = "Trùng khớp? [c/k]: "


def _prompt(role: str, recent_codes: int):
    from teleport.app.pairing import PairingPrompt
    from teleport.core.pairing import Role

    return PairingPrompt(
        session_id=7,
        role=Role(role),
        address="192.168.1.5",
        peer_name="A",
        peer_device_id="ab" * 16,
        code="123456",
        expires_at=asyncio.get_running_loop().time() + 60,
        recent_codes=recent_codes,
    )


def _prompter(*, json_mode: bool, listening: bool = True):
    from teleport.cli import _Output, _Prompter

    out, err = io.StringIO(), io.StringIO()
    return _Prompter(_Output(out, err, json_mode), listening=listening), out, err


@pytest.mark.parametrize("n", [0, 1, 2])
async def test_responder_prompt_below_threshold_has_no_warning(n):
    p, out, err = _prompter(json_mode=False)
    p.on_prompt(_prompt("responder", n))
    text = out.getvalue()
    assert "Cảnh báo" not in text and "mã ghép đôi trong 1 phút qua" not in text
    lines = text.split("\n")
    assert lines[-1] == ASK
    assert "123 456" in lines[-2]
    assert err.getvalue() == ""


@pytest.mark.parametrize("n", [3, 5, 64])
async def test_responder_prompt_at_threshold_warns_between_code_and_question(n):
    p, out, err = _prompter(json_mode=False)
    p.on_prompt(_prompt("responder", n))
    lines = out.getvalue().split("\n")
    assert lines[-1] == ASK
    assert lines[-2] == WARN_CODES.format(n=n)
    assert "123 456" in lines[-3]
    assert out.getvalue().count("Cảnh báo") == 1
    assert err.getvalue() == ""


async def test_warning_mentions_exactly_three_codes():
    p, out, _ = _prompter(json_mode=False)
    p.on_prompt(_prompt("responder", 3))
    assert "đã hiện 3 mã ghép đôi trong 1 phút qua" in out.getvalue()


@pytest.mark.parametrize("listening", [False, True], ids=["pair", "listen"])
@pytest.mark.parametrize("n", [0, 3, 10])
async def test_initiator_prompt_never_warns(n, listening):
    p, out, _ = _prompter(json_mode=False, listening=listening)
    p.on_prompt(_prompt("initiator", n))
    text = out.getvalue()
    assert "Cảnh báo" not in text and "mã ghép đôi trong 1 phút qua" not in text
    assert text.endswith(ASK)


@pytest.mark.parametrize(
    ("role", "n", "warn"),
    [
        ("responder", 0, False),
        ("responder", 2, False),
        ("responder", 3, True),
        ("responder", 7, True),
        ("initiator", 0, False),
        ("initiator", 5, False),
    ],
)
async def test_json_pair_prompt_has_recent_codes_and_warning(role, n, warn):
    p, out, err = _prompter(json_mode=True)
    p.on_prompt(_prompt(role, n))
    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert [e["event"] for e in events] == ["pair_prompt"]
    ev = events[0]
    assert type(ev["recent_codes"]) is int and ev["recent_codes"] == n
    assert "warning" in ev
    assert ev["warning"] == (WARN_CODES.format(n=n) if warn else None)
    assert ev["code"] == "123456" and ev["role"] == role
    assert err.getvalue() == ""


@pytest.mark.parametrize("role", ["responder", "initiator"])
def test_outcome_message_prompt_failed(role):
    from teleport.app.pairing import PairingFinished, PairingOutcome
    from teleport.cli import outcome_message
    from teleport.core.pairing import Role

    result = PairingFinished(
        session_id=3,
        role=Role(role),
        outcome=PairingOutcome.PROMPT_FAILED,
        address="192.168.1.5",
        port=None if role == "responder" else 47474,
        peer_name="A",
        peer_device_id="ab" * 16,
        peer_reason=None,
        device=None,
        known_name=None,
    )
    assert outcome_message(result) == [MVP_PROMPT_FAILED]


async def _pair_once(cli_runs, a_dir, port: int, *, json_mode: bool = True) -> CliRun:
    """Một lần `pair` tới `listen` ở `port`, hai bên trả lời “c”; trả lần chạy `pair` (đã xong)."""
    pair = cli_runs("pair", pair_argv(a_dir, port, json_mode=json_mode))
    if json_mode:
        await pair.wait_event("pair_prompt")
    else:
        await pair.wait_text(ASK)
    pair.say("c")
    return pair


@with_marks(socket_test)
async def test_listen_json_recent_codes_and_warning_end_to_end(tmp_path, fast_cli, cli_runs):
    listen = cli_runs("listen", listen_argv(tmp_path / "B"))
    ev = await listen.wait_event("listening")
    for n in (1, 2, 3):
        pair = await _pair_once(cli_runs, tmp_path / "A", ev["port"])
        await listen.wait_event("pair_prompt", n=n)
        listen.say("c")
        assert await asyncio.wait_for(pair.task, 10) == 0
        await listen.wait_event("pair_result", n=n)
        pa = pair.events_of("pair_prompt")[0]
        assert pa["recent_codes"] == 0 and pa["warning"] is None
    prompts = listen.events_of("pair_prompt")
    assert [p["recent_codes"] for p in prompts] == [1, 2, 3]
    assert [p["warning"] for p in prompts] == [None, None, WARN_CODES.format(n=3)]
    assert [r["outcome"] for r in listen.events_of("pair_result")] == ["paired"] * 3
    assert not listen.task.done()  # listen không dừng sau một mã


@with_marks(socket_test)
async def test_listen_text_warns_on_third_code(tmp_path, fast_cli, cli_runs):
    listen = cli_runs("listen", listen_argv(tmp_path / "B", json_mode=False))
    port = await listen_port_text(listen)
    for n in (1, 2, 3):
        pair = await _pair_once(cli_runs, tmp_path / "A", port, json_mode=False)
        await listen.wait_text(ASK, n=n)
        listen.say("c")
        assert await asyncio.wait_for(pair.task, 10) == 0
        await listen.wait_text("Đã ghép đôi với A.", n=n)
        assert "Cảnh báo" not in pair.text()
    text = listen.text()
    assert text.count("mã ghép đôi trong 1 phút qua") == 1
    assert WARN_CODES.format(n=3) + "\n" + ASK in text


@with_marks(socket_test)
async def test_listen_over_code_limit_answers_busy_without_prompt(tmp_path, fast_cli, cli_runs, monkeypatch):
    """L4: quá giới hạn mã thì `listen` không hỏi, không in kết quả, vẫn chạy; `pair` nhận câu
    “chưa sẵn sàng” (mã thoát 1). Giới hạn rút còn 1 mã để test nhanh."""
    from teleport.core import pairing as core_pairing

    monkeypatch.setattr(core_pairing, "MAX_CODES_PER_WINDOW", 1)
    listen = cli_runs("listen", listen_argv(tmp_path / "B"))
    ev = await listen.wait_event("listening")
    first = await _pair_once(cli_runs, tmp_path / "A", ev["port"])
    await listen.wait_event("pair_prompt")
    listen.say("c")
    assert await asyncio.wait_for(first.task, 10) == 0
    await listen.wait_event("pair_result")

    second = cli_runs("pair", pair_argv(tmp_path / "C", ev["port"], json_mode=False, name="C"))
    assert await asyncio.wait_for(second.task, 10) == 1
    assert "B chưa sẵn sàng ghép đôi" in second.text()
    assert ASK not in second.text()
    await asyncio.sleep(0.3)
    assert len(listen.events_of("pair_prompt")) == 1
    assert len(listen.events_of("pair_result")) == 1
    assert not listen.task.done()
