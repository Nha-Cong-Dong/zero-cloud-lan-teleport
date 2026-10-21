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
BAD_ADDRESS = "`teleport pair` hiện chỉ nhận địa chỉ IP, ví dụ 192.168.1.5 hoặc 192.168.1.5:47474."
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
    "address", ["pc-nha", "1.2.3.4:0", "::1", "1.2.3.4:65536", "0.0.0.0", "192.168.1", "1.2.3.4:080"]
)
def test_cl5_pair_bad_address_exits_2_without_side_effects(tmp_path, capsys, address):
    d = tmp_path / "cfg"
    assert run_main_guarded(["--config-dir", str(d), "pair", address, *NODE_OPTS]) == 2
    assert BAD_ADDRESS in capsys.readouterr().err
    assert not (d / "node.lock").exists()
    assert not (d / "settings.json").exists()
    assert not (d / "device.crt").exists()


def test_cl5_pair_bad_address_json(tmp_path, capsys):
    d = tmp_path / "cfg"
    assert run_main_guarded(["--config-dir", str(d), "pair", "pc-nha", "--json", *NODE_OPTS]) == 2
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
