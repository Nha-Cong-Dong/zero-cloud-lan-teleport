"""Test khói cho bản build PyInstaller của CLI (P2-8b, `specs/plan-p2-8.md` mục 2, việc b2).

Chạy exe thật bằng tiến trình con; đường dẫn exe lấy từ biến môi trường `TELEPORT_FROZEN_EXE`.
Không có biến thì cả module bị bỏ qua, nên `pytest` thường không chạy gì ở đây. Chạy riêng:

    $env:TELEPORT_FROZEN_EXE = "<dist>\\teleport\\teleport.exe"
    pytest tests/packaging -m frozen

Test không chạy node bằng `teleport` trong tiến trình test: mọi node là exe. Chỉ đọc file cấu hình
(`trusted.json`, `device.crt`) bằng `json`/`pathlib`, theo định dạng ở `docs/architecture.md` mục 4.

Node nghe trên 127.0.0.1 (cờ ẩn `--host`), nên không bật mDNS và Windows không hỏi tường lửa; riêng
`list` duyệt mDNS thật trên mọi card mạng (mạng chặn multicast thì chấp nhận lỗi khám phá, chỉ đòi không
có traceback).

Mỗi tiến trình được tạo với `CREATE_NEW_PROCESS_GROUP` (Windows) để gửi được `CTRL_BREAK_EVENT` riêng
cho nó; khi test xong chỉ tiến trình do test tạo bị giết (đúng PID của `Popen`).
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ENV_EXE = "TELEPORT_FROZEN_EXE"
EXE = os.environ.get(ENV_EXE, "").strip()

if not EXE:
    pytest.skip(f"không có {ENV_EXE}: chưa có bản build để kiểm", allow_module_level=True)

# `integration`: mở socket thật trên 127.0.0.1 (quy ước repo); `frozen`: chọn bằng `-m frozen`.
pytestmark = [pytest.mark.frozen, pytest.mark.integration]

LOCAL = "127.0.0.1"
NODE_OPTS = ["--host", LOCAL, "--port", "0"]
RECEIVER = "PC-Nha"
SENDER = "Laptop-Minh"
MiB = 1024 * 1024
DEVICE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

# Dấu hiệu tiến trình chết vì lỗi Python hay lỗi đóng gói (thiếu module, bootloader PyInstaller).
CRASH_MARKERS = (
    "Traceback (most recent call last)",
    "Fatal Python error",
    "ModuleNotFoundError",
    "No module named",
    "ImportError",
    "Failed to execute script",
    "[PYI-",
)


def assert_no_crash(text: str, what: str = "") -> None:
    found = [m for m in CRASH_MARKERS if m in text]
    assert not found, f"{what}: có dấu hiệu lỗi {found}:\n{text}"


def base_env(home: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("TELEPORT_HOME", None)
    env.pop(ENV_EXE, None)
    if home is not None:
        env["TELEPORT_HOME"] = str(home)
    return env


def run_once(argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 60.0):
    """Chạy exe tới khi thoát; trả (mã, stdout, stderr) đã giải mã UTF-8; stdin đóng sẵn."""
    p = subprocess.run(
        [EXE, *argv],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        env=base_env() if env is None else env,
        check=False,
    )
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    assert_no_crash(out + err, " ".join(argv))
    return p.returncode, out, err


class ExeProc:
    """Một exe chạy nền: stdout đọc theo dòng trong thread, stderr gom lại; stdin là pipe."""

    def __init__(self, argv: list[str], *, env: dict[str, str] | None = None) -> None:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        self.argv = argv
        self.p = subprocess.Popen(
            [EXE, *argv],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=flags,
            env=base_env() if env is None else env,
        )
        self.lines: queue.Queue[str] = queue.Queue()
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

    def text(self) -> str:
        return "\n".join(self.out) + "\n" + self.err_text()

    def _next_line(self, deadline: float, what: str) -> str:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"hết giờ chờ {what}: {self.text()}")
            try:
                line = self.lines.get(timeout=min(left, 0.5))
            except queue.Empty:
                # Luồng đọc còn sống thì pipe có thể còn dòng chưa đọc dù exe đã thoát.
                if not self._t_out.is_alive() and self.lines.empty():
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        self.p.wait(timeout=5)
                    raise AssertionError(
                        f"exe đã thoát ({self.p.returncode}) trước {what}: {self.text()}"
                    ) from None
                continue
            self.out.append(line)
            return line

    def wait_json(self, event: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            line = self._next_line(deadline, event)
            with contextlib.suppress(ValueError):
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("event") == event:
                    return obj

    def wait_line(self, pattern: re.Pattern[str], timeout: float = 30.0) -> re.Match[str]:
        deadline = time.monotonic() + timeout
        while True:
            m = pattern.search(self._next_line(deadline, pattern.pattern))
            if m:
                return m

    def wait_status(self, status: str, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            ev = self.wait_json("status", max(0.1, deadline - time.monotonic()))
            if ev.get("status") == status:
                return ev

    def events(self, name: str | None = None) -> list[dict]:
        found = []
        for line in self.out:
            with contextlib.suppress(ValueError):
                obj = json.loads(line)
                if isinstance(obj, dict) and (name is None or obj.get("event") == name):
                    found.append(obj)
        return found

    def write(self, text: str) -> None:
        self.p.stdin.write(text.encode("utf-8"))
        self.p.stdin.flush()

    def interrupt(self) -> None:
        """Ctrl+C như người dùng: Windows `CTRL_BREAK_EVENT` cho nhóm tiến trình riêng, POSIX SIGINT."""
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
        assert_no_crash(self.text(), " ".join(self.argv))
        return code

    def kill(self) -> None:
        if self.p.poll() is None:
            self.p.kill()  # chỉ đúng PID do test tạo
            with contextlib.suppress(Exception):
                self.p.wait(10)
        for stream in (self.p.stdin, self.p.stdout, self.p.stderr):
            with contextlib.suppress(Exception):
                stream.close()
        self._t_out.join(5)
        self._t_err.join(5)


@pytest.fixture(scope="module")
def exe() -> str:
    path = Path(EXE)
    assert path.is_file(), f"{ENV_EXE}={EXE!r} không trỏ tới một file"
    return EXE


@pytest.fixture
def start(exe):
    """`start(argv, *, env=None)` → `ExeProc`; mọi exe do test tạo bị dừng khi test xong."""
    started: list[ExeProc] = []

    def _start(argv: list[str], *, env: dict[str, str] | None = None) -> ExeProc:
        proc = ExeProc(argv, env=env)
        started.append(proc)
        return proc

    yield _start
    for proc in started:
        proc.kill()


def make_random_file(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        left = size
        while left:
            chunk = os.urandom(min(left, 4 * MiB))
            f.write(chunk)
            left -= len(chunk)
    return path


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(4 * MiB):
            h.update(chunk)
    return h.hexdigest()


def trusted_devices(config: Path) -> list[dict]:
    data = json.loads((config / "trusted.json").read_text(encoding="utf-8"))
    return data["devices"]


def parse_time(text: str) -> datetime.datetime:
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.UTC)


# =================================================================================================
# Ghép đôi hai exe qua 127.0.0.1 (dùng chung cho các test truyền file trong module)
# =================================================================================================


class Paired:
    def __init__(self, root: Path) -> None:
        self.R = root / "R"  # máy nhận (chạy `listen`)
        self.S = root / "S"  # máy gửi (chạy `pair`)
        self.rid = ""
        self.sid = ""
        self.listen_result: dict = {}
        self.pair_result: dict = {}
        self.codes: tuple[str, str] = ("", "")


def pair_two_exes(root: Path) -> Paired:
    net = Paired(root)
    procs: list[ExeProc] = []
    try:
        listen = ExeProc(["--config-dir", str(net.R), "listen", "--json", "--name", RECEIVER, *NODE_OPTS])
        procs.append(listen)
        ready = listen.wait_json("listening", 60)
        net.rid = ready["device_id"]
        pair = ExeProc(
            [
                "--config-dir",
                str(net.S),
                "pair",
                f"{LOCAL}:{ready['port']}",
                "--json",
                "--name",
                SENDER,
                *NODE_OPTS,
            ]
        )
        procs.append(pair)
        prompt_s = pair.wait_json("pair_prompt", 60)
        prompt_r = listen.wait_json("pair_prompt", 30)
        net.codes = (prompt_s["code"], prompt_r["code"])
        pair.write("c\n")
        listen.write("c\n")
        assert pair.finish(60) == 0, pair.text()
        [net.pair_result] = pair.events("pair_result")
        net.listen_result = listen.wait_json("pair_result", 30)
        listen.interrupt()
        assert listen.finish(30) == 0, listen.text()
        net.sid = net.listen_result.get("peer_device_id") or ""
    finally:
        for proc in procs:
            proc.kill()
    return net


@pytest.fixture(scope="module")
def paired(exe, tmp_path_factory) -> Paired:
    return pair_two_exes(tmp_path_factory.mktemp("frozen_pair"))


def receive_argv(net: Paired, recv_dir: Path) -> list[str]:
    return [
        "--config-dir",
        str(net.R),
        "receive",
        "--yes",
        "--json",
        "--dir",
        str(recv_dir),
        "--name",
        RECEIVER,
        *NODE_OPTS,
    ]


def send_argv(net: Paired, files: list[Path], port: int, extra: tuple[str, ...] = ()) -> list[str]:
    return [
        "--config-dir",
        str(net.S),
        "send",
        *(str(f) for f in files),
        "--to",
        net.rid[:8],
        "--address",
        f"{LOCAL}:{port}",
        "--json",
        "--name",
        SENDER,
        "--host",
        LOCAL,
        *extra,
    ]


# =================================================================================================
# Các ca
# =================================================================================================


def test_frozen_version_prints_program_name_and_version(exe):
    from teleport import __version__  # chỉ đọc chuỗi phiên bản của mã nguồn đã build

    code, out, err = run_once(["--version"])
    assert code == 0, out + err
    assert out.strip() == f"teleport {__version__}", out + err


UCRT_NAME_RE = re.compile(r"^(?:ucrtbase\.dll|api-ms-win-(?:core|crt)-.*\.dll)$", re.IGNORECASE)


def test_frozen_bundle_has_no_universal_crt(exe):
    """Universal CRT là thành phần của Windows 10+, bản build không được mang bản sao nào."""
    root = Path(exe).parent
    found = sorted(str(p.relative_to(root)) for p in root.rglob("*") if UCRT_NAME_RE.match(p.name))
    assert not found, found


def test_frozen_identity_creates_stable_identity_in_teleport_home(exe, tmp_path):
    home = tmp_path / "home"
    code, out, err = run_once(["identity"], env=base_env(home))
    assert code == 0, out + err
    fields = dict(
        (key.strip(), value.strip()) for key, _, value in (line.partition(":") for line in out.splitlines())
    )
    assert DEVICE_ID_RE.match(fields["Mã thiết bị"]), out
    assert Path(fields["Thư mục"]) == home, out
    assert (home / "device.crt").is_file() and (home / "device.key").is_file()

    code2, out2, err2 = run_once(["identity"], env=base_env(home))
    assert code2 == 0, out2 + err2
    assert out2 == out, "chạy lần hai phải đọc lại đúng danh tính cũ"


def test_frozen_pair_two_exes_over_loopback(paired):
    net = paired
    code_s, code_r = net.codes
    assert re.fullmatch(r"\d{6}", code_s) and code_s == code_r, net.codes
    assert net.pair_result["outcome"] == "paired", net.pair_result
    assert net.listen_result["outcome"] == "paired", net.listen_result
    assert DEVICE_ID_RE.match(net.rid) and DEVICE_ID_RE.match(net.sid), (net.rid, net.sid)
    assert net.pair_result["peer_device_id"] == net.rid, net.pair_result
    [on_s] = trusted_devices(net.S)
    [on_r] = trusted_devices(net.R)
    assert (on_s["device_id"], on_s["name"]) == (net.rid, RECEIVER), on_s
    assert (on_r["device_id"], on_r["name"]) == (net.sid, SENDER), on_r


def test_frozen_send_64_mib_by_address_sha_matches(paired, start, tmp_path):
    net = paired
    recv_dir = tmp_path / "nhan"
    recv = start(receive_argv(net, recv_dir))
    port = recv.wait_json("receiving", 60)["port"]
    f = make_random_file(tmp_path / "src" / "ngau-nhien.bin", 64 * MiB)
    send = start(send_argv(net, [f], port))
    assert send.finish(300) == 0, send.text()
    assert [r["result"] for r in send.events("result")] == ["done"], send.text()
    assert recv.wait_json("result", 60)["result"] == "done", recv.text()
    got = recv_dir / f.name
    assert got.stat().st_size == f.stat().st_size
    assert sha256_of(got) == sha256_of(f)
    recv.interrupt()
    assert recv.finish(30) == 0, recv.text()
    assert recv.events()[-1]["event"] == "stopped", recv.text()


def test_frozen_ctrl_break_on_send_cancels_receiver(paired, start, tmp_path):
    """Ctrl+C ở `send` (giới hạn 1M/s nên còn đang gửi) → send mã 1; bên nhận `result`
    `cancelled_user`, `by_peer: true`; trễ ≤ 2,0 s như P3 (spec-p2-7 mục B)."""
    net = paired
    recv = start(receive_argv(net, tmp_path / "nhan"))
    port = recv.wait_json("receiving", 60)["port"]
    f = make_random_file(tmp_path / "src" / "lon.bin", 32 * MiB)  # ~32 s ở 1 MiB/s
    send = start(send_argv(net, [f], port, extra=("--limit-rate", "1M")))
    recv.wait_status("transferring", 60)
    time.sleep(1.0)
    assert send.p.poll() is None, send.text()
    sent_at = datetime.datetime.now(datetime.UTC)
    send.interrupt()
    res = recv.wait_json("result", 15)
    assert (res["result"], res["by_peer"]) == ("cancelled_user", True), res
    delay = (parse_time(res["time"]) - sent_at).total_seconds()
    print(f"frozen cancel_delay_s={delay:.3f}")
    assert delay <= 2.0, f"bên nhận báo hủy sau {delay:.3f} s"
    assert send.finish(30) == 1, send.text()
    mine = [(r["result"], r["by_peer"]) for r in send.events("result")]
    assert mine == [("cancelled_user", False)], send.text()
    assert not (tmp_path / "nhan" / f.name).exists(), "file bị hủy không được xuất hiện ở thư mục nhận"
    recv.interrupt()
    assert recv.finish(30) == 0, recv.text()


def known_folder_downloads() -> Path:
    """`SHGetKnownFolderPath(FOLDERID_Downloads)` đọc độc lập bằng ctypes (không dùng `teleport.paths`)."""
    import ctypes
    import uuid
    from ctypes import wintypes

    guid_bytes = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}").bytes_le
    guid = (ctypes.c_ubyte * 16).from_buffer_copy(guid_bytes)
    buffer = ctypes.c_wchar_p()
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    fn = shell32.SHGetKnownFolderPath
    fn.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    fn.restype = ctypes.c_long
    hr = fn(ctypes.byref(guid), 0, None, ctypes.byref(buffer))
    try:
        assert hr == 0 and buffer.value, f"SHGetKnownFolderPath lỗi {hr:#x}"
        return Path(buffer.value)
    finally:
        if buffer:
            ole32.CoTaskMemFree(buffer)


SAVE_LINE_RE = re.compile(r"^\s*Lưu vào:\s+(?P<path>.+?)\s*$")


@pytest.mark.skipif(sys.platform != "win32", reason="Known Folder chỉ có trên Windows")
def test_frozen_receive_default_dir_is_teleport_in_known_folder_downloads(start, tmp_path):
    expected = known_folder_downloads() / "Teleport"
    existed = expected.exists()
    home = tmp_path / "home"
    recv = start(["receive", "--yes", "--name", RECEIVER, *NODE_OPTS], env=base_env(home))
    try:
        m = recv.wait_line(SAVE_LINE_RE, 60)
        shown = Path(m["path"])
        assert os.path.normcase(shown) == os.path.normcase(expected), (shown, expected)
        assert (home / "device.crt").is_file(), "TELEPORT_HOME phải được dùng làm thư mục cấu hình"
        time.sleep(0.3)
        recv.interrupt()
        assert recv.finish(30) == 0, recv.text()
        assert "Đã dừng." in recv.text(), recv.text()
    finally:
        recv.kill()
        # Dọn thư mục do test tạo ra (chỉ khi trước đó chưa có và nay vẫn trống; không xóa file nào).
        if not existed:
            with contextlib.suppress(OSError):
                expected.rmdir()


def test_frozen_list_runs_without_crash(start, tmp_path):
    """`list` duyệt mDNS thật vài giây. Mạng/runner chặn multicast thì chấp nhận lỗi khám phá (B8b),
    nhưng không được có traceback hay thiếu module (zeroconf phải được đóng gói đủ)."""
    home = tmp_path / "home"
    code, out, err = run_once(["list", "--json", "--timeout", "3"], env=base_env(home), timeout=60)
    obj = json.loads(out)
    if code == 0:
        assert obj["self"]["device_id"] and DEVICE_ID_RE.match(obj["self"]["device_id"]), obj
        assert isinstance(obj["devices"], list), obj
    else:
        assert code == 1, (code, out, err)
        assert obj.get("error") == "discovery_error", obj
        assert obj["message"].startswith("Không bật được khám phá mDNS"), obj

    code_t, out_t, err_t = run_once(["list", "--timeout", "3"], env=base_env(home), timeout=60)
    assert code_t in (0, 1), (code_t, out_t, err_t)
    if code_t == 1:
        assert "Không bật được khám phá mDNS" in out_t + err_t, out_t + err_t
