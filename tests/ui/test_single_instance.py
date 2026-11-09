"""Một bản mỗi thư mục cấu hình: `SingleInstance`, `server_name`, lệnh IPC `show` và `send`
(spec-p4-3 D3, D4, D5, mục 6.4 và mục 10; spec-f5 mục 2.1, 2.4, 5 điểm 2, 8, 9 và mục 9 D1, D2, D13).

`gui.lock` bảo đảm một bản; bản giữ khóa tạo `gui.key` (bí mật 32 byte) trước khi `listen`, và tên
server có khóa đó (D1). Bản thứ hai đọc `gui.key`, nối `QLocalSocket` gửi một lệnh (`b"show\\n"`, hay
lệnh `send` của `ui.ipc`). Server đọc **cả lệnh** (trần `ui.ipc.MAX_BYTES`) trong hạn 1 s tính từ lúc
nhận kết nối, đúng luật thì phát `show_requested` / `send_requested(tuple[Path, ...])`, sai thì bỏ cả
lệnh, luôn đóng socket; tối đa 4 kết nối cùng lúc, mỗi kết nối một hạn riêng. Socket cục bộ (named pipe
/ Unix socket), không TCP. Log không bao giờ chứa đường dẫn (N9).
"""

from __future__ import annotations

import getpass
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtNetwork import QLocalSocket

SHOW = b"show\n"


KEY = bytes(range(32))  # khóa cố định cho các test tính tên


def expected_name(config_dir: Path, key: bytes = KEY, user: str | None = None) -> str:
    """Tên server theo spec-f5 D1: `"teleport-" + sha256(user \\0 norm \\0 key)[:24]`, tính độc lập."""
    if user is None:
        user = getpass.getuser()
    norm = os.path.normcase(os.path.realpath(config_dir))
    return "teleport-" + hashlib.sha256(f"{user}\0{norm}\0".encode() + key).hexdigest()[:24]


def legacy_name(config_dir: Path) -> str:
    """Công thức cũ, không khóa (trước spec-f5 D1)."""
    norm = os.path.normcase(os.path.realpath(config_dir))
    return "teleport-" + hashlib.sha256(f"{getpass.getuser()}\0{norm}".encode()).hexdigest()[:24]


@pytest.fixture
def instances(qtbot):
    """`acquire(config_dir, wait=…)`; mọi bản đã lấy được đóng khi test xong."""
    got: list = []

    def acquire(config_dir: Path, wait: float = 3.0, message: bytes | None = None):
        from teleport.ui.app import SingleInstance

        if message is None:
            inst = SingleInstance.acquire(config_dir, wait=wait)
        else:
            inst = SingleInstance.acquire(config_dir, wait=wait, message=message)
        if inst is not None:
            got.append(inst)
        return inst

    yield acquire
    for inst in got:
        inst.close()


def raw_client(qtbot, name: str, payload: bytes | None) -> QLocalSocket:
    sock = QLocalSocket()
    sock.connectToServer(name)
    assert sock.waitForConnected(2000), sock.errorString()
    if payload:
        sock.write(payload)
        sock.flush()
        sock.waitForBytesWritten(1000)
    return sock


def key_of(config_dir: Path) -> bytes:
    """`gui.key` mà bản giữ `gui.lock` đã tạo (D1)."""
    return (config_dir / "gui.key").read_bytes()


def closed(sock: QLocalSocket) -> bool:
    return sock.state() == QLocalSocket.LocalSocketState.UnconnectedState


# --- server_name ------------------------------------------------------------------------------------


def test_server_name_matches_spec_formula(tmp_path):
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    d.mkdir()
    assert server_name(d, KEY) == expected_name(d, KEY)


def test_server_name_is_stable_and_hides_path(tmp_path):
    from teleport.ui.app import server_name

    d = tmp_path / "Thu-Muc-Cau-Hinh"
    d.mkdir()
    name = server_name(d, KEY)
    assert name == server_name(Path(str(d)), KEY)
    assert name == server_name(Path(str(d) + os.sep), KEY), "dấu phân cách cuối không đổi tên"
    assert name == server_name(d / "con" / "..", KEY), "đường dẫn chưa chuẩn hóa cho cùng tên"
    assert len(name) == len("teleport-") + 24
    assert name.startswith("teleport-")
    assert all(c in "0123456789abcdef" for c in name[len("teleport-") :])
    assert "Thu-Muc" not in name and str(tmp_path) not in name


def test_server_name_relative_path_equals_absolute(tmp_path, monkeypatch):
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.chdir(tmp_path)
    assert server_name(Path("cfg"), KEY) == server_name(d, KEY)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows không phân biệt hoa thường trong đường dẫn")
def test_server_name_ignores_case_on_windows(tmp_path):
    from teleport.ui.app import server_name

    d = tmp_path / "Cfg"
    d.mkdir()
    assert server_name(Path(str(d).upper()), KEY) == server_name(Path(str(d).lower()), KEY)
    assert server_name(Path(str(d).replace("\\", "/")), KEY) == server_name(d, KEY)


def test_server_name_differs_per_config_dir_and_per_user(tmp_path, monkeypatch):
    from teleport.ui import app as ui_app

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert ui_app.server_name(a, KEY) != ui_app.server_name(b, KEY)
    monkeypatch.setattr(getpass, "getuser", lambda: "nguoi-khac")
    assert ui_app.server_name(a, KEY) == expected_name(a, KEY, "nguoi-khac")


def test_server_name_when_getuser_fails_uses_empty_user(tmp_path, monkeypatch):
    from teleport.ui.app import server_name

    def broken():
        raise OSError("không có tên người dùng")

    monkeypatch.setattr(getpass, "getuser", broken)
    d = tmp_path / "cfg"
    d.mkdir()
    assert server_name(d, KEY) == expected_name(d, KEY, "")


# --- Một bản ----------------------------------------------------------------------------------------


def test_second_instance_same_dir_returns_none_and_first_gets_show(qtbot, instances, tmp_path):
    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    assert (d / "gui.lock").exists()
    with qtbot.waitSignal(first.show_requested, timeout=5_000):
        second = instances(d, wait=3.0)
        assert second is None


def test_second_instance_does_not_take_node_lock(qtbot, instances, tmp_path, ui_helpers):
    """D3: GUI giữ `gui.lock`, không giữ trước `node.lock` (để `Node.start()` cùng tiến trình lấy được)."""
    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    assert ui_helpers.lock_is_free(d, "node.lock")
    assert not ui_helpers.lock_is_free(d, "gui.lock")


def test_different_config_dirs_are_independent(qtbot, instances, tmp_path):
    a = instances(tmp_path / "a")
    b = instances(tmp_path / "b")
    assert a is not None and b is not None
    with qtbot.assertNotEmitted(a.show_requested, wait=300):
        assert instances(tmp_path / "b", wait=1.0) is None


def test_close_releases_gui_lock_and_dir_can_be_acquired_again(qtbot, tmp_path, ui_helpers):
    from teleport.ui.app import SingleInstance

    d = tmp_path / "cfg"
    first = SingleInstance.acquire(d)
    assert first is not None
    first.close()
    assert ui_helpers.lock_is_free(d, "gui.lock")
    again = SingleInstance.acquire(d, wait=1.0)
    try:
        assert again is not None
    finally:
        if again is not None:
            again.close()


@pytest.mark.slow
def test_second_process_sends_show_and_exits_0(qtbot, instances, tmp_path):
    """Bản thứ hai là tiến trình thật (`python -m teleport.ui --config-dir D`): gửi `show`, trả 0, không
    mở cửa sổ thứ hai. Thư mục không có `device.crt`: lỡ bản hai không nhận ra bản một thì cũng không chạy
    node."""
    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.pop("TELEPORT_HOME", None)
    shows: list[float] = []
    first.show_requested.connect(lambda: shows.append(time.monotonic()))
    proc = subprocess.Popen(
        [sys.executable, "-m", "teleport.ui", "--config-dir", str(d)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        # Chờ bằng event loop Qt: server của bản một phải nhận kết nối trong lúc chờ.
        qtbot.waitUntil(lambda: proc.poll() is not None, timeout=30_000)
        _out, err = proc.communicate(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10)
    assert proc.returncode == 0, err.decode("utf-8", "replace")
    qtbot.waitUntil(lambda: bool(shows), timeout=5_000)
    assert len(shows) == 1


# --- Server IPC: dữ liệu từ tiến trình khác không tin cậy --------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize(
    "payload",
    [b"hello\n", b"SHOW\n", b"show", b"show\r\n", b"x" * 17, b"x" * 4096, b"\x00\xff" * 8],
    ids=["other", "upper", "no-newline", "crlf", "17-bytes", "4k", "binary"],
)
def test_server_ignores_garbage_and_closes(qtbot, instances, tmp_path, payload):
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    with qtbot.assertNotEmitted(first.show_requested, wait=1_500):
        sock = raw_client(qtbot, server_name(d, key_of(d)), payload)
    # Server luôn đóng socket (hết hạn đọc, hoặc đã quyết định bỏ lệnh).
    qtbot.waitUntil(lambda: closed(sock), timeout=3_000)
    sock.abort()


@pytest.mark.slow
def test_silent_client_is_dropped_after_about_1s(qtbot, instances, tmp_path):
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    t0 = time.monotonic()
    sock = raw_client(qtbot, server_name(d, key_of(d)), None)
    qtbot.waitUntil(lambda: closed(sock), timeout=4_000)
    elapsed = time.monotonic() - t0
    assert elapsed < 3.5, f"server giữ kết nối im lặng {elapsed:.1f} s (hạn 1 s)"
    sock.abort()


@pytest.mark.slow
def test_valid_show_still_works_while_silent_clients_are_open(qtbot, instances, tmp_path):
    """Tối đa 4 kết nối cùng lúc: kẻ mở nhiều kết nối im lặng không làm server treo; `show` thật vẫn tới
    (ngay, hoặc sau khi các kết nối im lặng hết hạn 1 s)."""
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    name = server_name(d, key_of(d))
    silent = []
    for _ in range(10):
        s = QLocalSocket()
        s.connectToServer(name)
        silent.append(s)
    qtbot.wait(50)
    try:
        with qtbot.waitSignal(first.show_requested, timeout=6_000):
            deadline = time.monotonic() + 5
            while True:
                sock = QLocalSocket()
                sock.connectToServer(name)
                if sock.waitForConnected(500):
                    sock.write(SHOW)
                    sock.flush()
                    sock.waitForBytesWritten(500)
                    break
                assert time.monotonic() < deadline, "không nối được tới server sau 5 s"
                qtbot.wait(100)
    finally:
        for s in silent:
            s.abort()


@pytest.mark.slow
def test_show_via_raw_socket_emits_once(qtbot, instances, tmp_path):
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    count: list[int] = []
    first.show_requested.connect(lambda: count.append(1))
    sock = raw_client(qtbot, server_name(d, key_of(d)), SHOW)
    qtbot.waitUntil(lambda: bool(count), timeout=3_000)
    qtbot.waitUntil(lambda: closed(sock), timeout=3_000)
    qtbot.wait(200)
    assert len(count) == 1
    sock.abort()


# --- Thứ tự: gui.lock trước, socket sau (soat-code vòng 1) --------------------------------------------


def test_lock_is_tried_before_socket_so_squatted_name_cannot_block(qtbot, instances, tmp_path):
    """Ai đó dựng sẵn server cùng tên mà không giữ `gui.lock` (pipe/socket còn sót, hay người dùng khác
    giành tên): bản mới vẫn lấy được `gui.lock` và trở thành bản chính, không nối vào server giả."""
    from PySide6.QtNetwork import QLocalServer

    d = tmp_path / "cfg"
    d.mkdir()
    squatter = QLocalServer()
    assert squatter.listen(legacy_name(d)), squatter.errorString()
    hits: list[int] = []
    squatter.newConnection.connect(lambda: hits.append(1))
    try:
        inst = instances(d, wait=1.0)
        qtbot.wait(200)
        assert inst is not None, "server giả không giữ gui.lock không được làm bản mới tưởng đã có bản khác"
        assert hits == [] and not squatter.hasPendingConnections(), "lấy được gui.lock thì không nối socket"
    finally:
        squatter.close()


@pytest.mark.slow
def test_stop_listening_then_second_acquire_times_out_not_swallowed(qtbot, instances, tmp_path):
    """Bản một đang thoát (server đã đóng, `gui.lock` còn giữ): bản hai không được coi là đã gửi `show`
    (None) mà phải hết hạn (TimeoutError → `main` trả 1)."""
    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    first.stop_listening()
    shows: list[int] = []
    first.show_requested.connect(lambda: shows.append(1))
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        instances(d, wait=1.0)
    assert time.monotonic() - t0 < 5
    assert shows == []


# =================================================================================================
# spec-f5 D1: bí mật `gui.key` trong tên server
# =================================================================================================

REPO = Path(__file__).resolve().parents[2]
SEND_HEAD = "Không mở được hộp gửi file."  # spec-f5 mục 2.6, `send_args_head`
BAD_PATH_TAIL = " không phải đường dẫn đầy đủ tới một file. Hãy chọn file trong File Explorer rồi gửi lại."
TOO_LARGE_HINT = "Bạn chọn quá nhiều file một lần. Hãy chọn ít file hơn rồi gửi lại."
# Đường dẫn sai trên hệ điều hành đang chạy mà `prepare_send` không nối với cwd (có ổ / có gốc).
BAD_ARG = "C:abc.txt" if os.name == "nt" else "/a/../x.txt"


def file_path(tmp_path: Path, name: str) -> str:
    """Đường dẫn tuyệt đối hợp lệ (file không cần có thật: `ipc` không chạm đĩa)."""
    return str(tmp_path / "files" / name)


def send_payload(*paths: str) -> bytes:
    return b"send\n" + b"".join(p.encode("utf-8") + b"\n" for p in paths) + b"\n"


def host_long_path(n_bytes: int, idx: int) -> str:
    sep = "\\" if os.name == "nt" else "/"
    head = "C:\\" if os.name == "nt" else "/"
    body_len = n_bytes - len(head)
    body = (f"d{idx:05d}" + sep + ("a" * 199 + sep) * (body_len // 200 + 2))[:body_len]
    if body.endswith(sep):
        body = body[:-1] + "b"
    return head + body


class Seen:
    """Ghi mọi `show_requested` / `send_requested` của một bản."""

    def __init__(self, inst) -> None:
        self.shows = 0
        self.sends: list = []
        inst.show_requested.connect(self._show)
        inst.send_requested.connect(self.sends.append)

    def _show(self) -> None:
        self.shows += 1

    @property
    def nothing(self) -> bool:
        return self.shows == 0 and self.sends == []


def test_holder_creates_32_byte_key_and_listens_on_keyed_name(qtbot, instances, tmp_path):
    d = tmp_path / "cfg"
    inst = instances(d)
    assert inst is not None and inst.listening
    key = key_of(d)
    assert len(key) == 32
    assert inst.name == expected_name(d, key), "tên server phải theo công thức D1 với khóa trong gui.key"


def test_keys_are_random_per_config_dir(qtbot, instances, tmp_path):
    a, b = instances(tmp_path / "a"), instances(tmp_path / "b")
    assert a is not None and b is not None
    assert key_of(tmp_path / "a") != key_of(tmp_path / "b")


def test_server_name_depends_on_key(tmp_path):
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    d.mkdir()
    other = bytes(32)
    assert server_name(d, KEY) != server_name(d, other)
    assert server_name(d, other) == expected_name(d, other)
    assert server_name(d, KEY) != legacy_name(d)


def test_key_is_new_on_each_acquire(qtbot, tmp_path):
    """D15 (sửa D1): mỗi lần lấy được `gui.lock` là một khóa mới, nên tên server đổi mỗi lần chạy."""
    from teleport.ui.app import SingleInstance

    d = tmp_path / "cfg"
    first = SingleInstance.acquire(d)
    assert first is not None
    key, name = key_of(d), first.name
    first.close()
    again = SingleInstance.acquire(d, wait=1.0)
    try:
        assert again is not None and again.listening
        assert len(key_of(d)) == 32
        assert key_of(d) != key, "D15: khóa mới mỗi lần lấy gui.lock"
        assert again.name != name
        assert again.name == expected_name(d, key_of(d))
    finally:
        if again is not None:
            again.close()


class _ScriptedKeys:
    """Thay `secrets.token_bytes`: trả lần lượt các khóa cho trước rồi khóa ngẫu nhiên thật."""

    def __init__(self, keys: list[bytes]) -> None:
        import secrets

        self.keys = list(keys)
        self.real = secrets.token_bytes
        self.calls = 0

    def __call__(self, n: int | None = None) -> bytes:
        self.calls += 1
        if self.keys:
            key = self.keys.pop(0)
            assert n in (None, len(key))
            return key
        return self.real(n)


class _Squatter:
    """`QLocalServer` của kẻ chiếm sẵn một tên; ghi các kết nối tới và byte nhận được."""

    def __init__(self, name: str) -> None:
        from PySide6.QtNetwork import QLocalServer

        self.server = QLocalServer()
        assert self.server.listen(name), self.server.errorString()
        self.socks: list = []
        self.data = bytearray()
        self.server.newConnection.connect(self._accept)

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            self.socks.append(sock)
            sock.readyRead.connect(lambda s=sock: self.data.extend(bytes(s.readAll().data())))

    def close(self) -> None:
        for sock in self.socks:
            sock.abort()
        self.server.close()


SQUAT_KEY = bytes([7]) * 32


def test_squatted_name_makes_holder_pick_another_key(qtbot, instances, tmp_path, monkeypatch):
    """D15: trước `listen`, nối thử tên dự kiến; có kẻ chiếm sẵn thì tạo khóa mới và thử lại. Lần nối thử
    không gửi byte nào cho kẻ chiếm."""
    import secrets

    d = tmp_path / "cfg"
    d.mkdir()
    squatter = _Squatter(expected_name(d, SQUAT_KEY))
    keys = _ScriptedKeys([SQUAT_KEY])
    monkeypatch.setattr(secrets, "token_bytes", keys)
    try:
        inst = instances(d)
        assert inst is not None
        qtbot.wait(100)
        assert keys.calls >= 2, "tên bị chiếm thì phải tạo khóa khác"
        key = key_of(d)
        assert key != SQUAT_KEY and len(key) == 32
        assert inst.listening and inst.name == expected_name(d, key)
        assert bytes(squatter.data) == b"", "nối thử không được gửi lệnh nào cho kẻ chiếm"
        # Bản thứ hai nối tới đúng bản thật, không tới kẻ chiếm.
        seen = Seen(inst)
        assert instances(d, wait=3.0) is None
        qtbot.waitUntil(lambda: seen.shows == 1, timeout=3_000)
        assert bytes(squatter.data) == b""
    finally:
        squatter.close()


@pytest.mark.slow
def test_always_squatted_name_gives_up_with_warning_and_no_server(
    qtbot, instances, tmp_path, monkeypatch, caplog
):
    """D15: thử vài lần vẫn bị chiếm → WARNING, không mở server (vẫn giữ `gui.lock`)."""
    import logging
    import secrets

    caplog.set_level(logging.WARNING)
    d = tmp_path / "cfg"
    d.mkdir()
    squatter = _Squatter(expected_name(d, SQUAT_KEY))
    monkeypatch.setattr(secrets, "token_bytes", lambda n=None: SQUAT_KEY)
    try:
        inst = instances(d, wait=2.0)
        assert inst is not None, "vẫn là bản chính (giữ gui.lock)"
        assert not inst.listening
        assert any(r.levelno == logging.WARNING and r.name.startswith("teleport") for r in caplog.records)
        assert bytes(squatter.data) == b""
    finally:
        squatter.close()


@pytest.mark.parametrize(
    "bad", [b"", b"x" * 5, b"y" * 31, b"z" * 33, b"\0" * 64], ids=["0", "5", "31", "33", "64"]
)
def test_holder_replaces_key_of_wrong_length(qtbot, instances, tmp_path, bad):
    d = tmp_path / "cfg"
    d.mkdir()
    (d / "gui.key").write_bytes(bad)
    inst = instances(d)
    assert inst is not None
    key = key_of(d)
    assert len(key) == 32 and key != bad
    assert inst.name == expected_name(d, key)


# =================================================================================================
# spec-f5 D16: client chỉ thành công khi server đóng kết nối
# =================================================================================================


class _MuteServer:
    """Giả một bản đang chạy giữ `gui.lock` mà không bao giờ đóng kết nối (đọc rồi im)."""

    def __init__(self, d: Path) -> None:
        from teleport.lock import NodeLock

        d.mkdir(parents=True, exist_ok=True)
        self.lock = NodeLock(d, "gui.lock")
        self.lock.acquire()
        (d / "gui.key").write_bytes(KEY)
        self.squatter = _Squatter(expected_name(d, KEY))

    def close(self) -> None:
        self.squatter.close()
        self.lock.release()


@pytest.mark.slow
def test_client_times_out_when_server_never_closes(qtbot, tmp_path):
    """D16: nối được và ghi được nhưng server không đóng → không coi là đã gửi; hết `wait` → TimeoutError."""
    from teleport.ui.app import SingleInstance

    d = tmp_path / "cfg"
    mute = _MuteServer(d)
    try:
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            SingleInstance.acquire(d, wait=1.0)
        assert time.monotonic() - t0 < 10
        assert mute.squatter.socks, "client phải đã nối tới server"
        assert bytes(mute.squatter.data).startswith(b"show\n")
    finally:
        mute.close()


def test_ack_timeout_constant_is_longer_than_read_timeout():
    from teleport.ui import app

    assert app.IPC_ACK_TIMEOUT_MS > app.IPC_READ_TIMEOUT_MS


@pytest.mark.slow
def test_main_shows_not_responding_when_server_never_closes(gui, tmp_path, ui_helpers):
    """D16: `main` hiện hộp `not_responding` và trả 1 (như TimeoutError), không thoát 0 im lặng."""
    d = tmp_path / "cfg"
    mute = _MuteServer(d)

    def script(run):
        return
        yield

    try:
        run = gui(
            ["--config-dir", str(d), "--send", file_path(tmp_path, "a.txt")],
            script,
            require_script_done=False,
        )
    finally:
        mute.close()
    assert run.code == 1
    assert any(ui_helpers.QUITTING in t for t in run.exec_texts), run.exec_texts


@pytest.mark.slow
def test_open_close_flood_does_not_lose_real_command(qtbot, instances, tmp_path):
    """D16 (tan-cong `attack_backlog`): 400 kết nối mở rồi đóng ngay không giữ chỗ; lệnh thật gửi sau đó
    tới trong vài giây."""
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    name = server_name(d, key_of(d))
    for i in range(400):
        sock = QLocalSocket()
        sock.connectToServer(name)
        sock.waitForConnected(50)
        sock.abort()
        sock.deleteLater()
        if i % 20 == 0:
            qtbot.wait(1)
    t0 = time.monotonic()
    assert instances(d, wait=10.0) is None
    qtbot.waitUntil(lambda: seen.shows == 1, timeout=5_000)
    elapsed = time.monotonic() - t0
    assert elapsed < 4.0, f"lệnh thật tới sau {elapsed:.1f} s"
    assert seen.sends == []


@pytest.mark.slow
def test_closed_connection_frees_slot_immediately(qtbot, instances, tmp_path):
    """D16: kết nối đã đóng thì server drop ngay; 4 kết nối đóng liền rồi lệnh thật vẫn tới nhanh, không
    phải chờ trọn hạn đọc của 4 chỗ."""
    from teleport.ui.app import IPC_READ_TIMEOUT_MS, server_name

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    name = server_name(d, key_of(d))
    for _ in range(4):
        sock = QLocalSocket()
        sock.connectToServer(name)
        assert sock.waitForConnected(1000)
        sock.write(b"sh")
        sock.flush()
        sock.disconnectFromServer()
        sock.abort()
    qtbot.wait(50)
    t0 = time.monotonic()
    assert instances(d, wait=5.0) is None
    qtbot.waitUntil(lambda: seen.shows == 1, timeout=5_000)
    assert time.monotonic() - t0 < IPC_READ_TIMEOUT_MS / 1000, "chỗ của kết nối đã đóng phải được trả ngay"


@pytest.mark.skipif(sys.platform == "win32", reason="quyền POSIX")
def test_key_file_is_private_on_posix(qtbot, instances, tmp_path):
    d = tmp_path / "cfg"
    assert instances(d) is not None
    assert (d / "gui.key").stat().st_mode & 0o777 == 0o600


@pytest.mark.slow
@pytest.mark.parametrize("damage", ["missing", "short", "long"])
def test_client_without_valid_key_does_not_connect_and_times_out(qtbot, instances, tmp_path, damage):
    """D1: client không đọc được khóa hợp lệ thì coi như chưa nối được, thử lại tới hạn `wait`."""
    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    key_file = d / "gui.key"
    if damage == "missing":
        key_file.unlink()
    else:
        key_file.write_bytes(b"k" * (16 if damage == "short" else 48))
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        instances(d, wait=1.0)
    assert time.monotonic() - t0 < 5
    qtbot.wait(200)
    assert seen.nothing


def test_client_uses_keyed_name_not_legacy_name(qtbot, instances, tmp_path):
    """Kẻ dựng server theo công thức cũ (không khóa) không nhận được lệnh nào."""
    from PySide6.QtNetwork import QLocalServer

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    squatter = QLocalServer()
    assert squatter.listen(legacy_name(d)), squatter.errorString()
    hits: list[int] = []
    squatter.newConnection.connect(lambda: hits.append(1))
    try:
        with qtbot.waitSignal(first.show_requested, timeout=5_000):
            assert instances(d, wait=3.0) is None
        qtbot.wait(100)
        assert hits == [] and not squatter.hasPendingConnections()
    finally:
        squatter.close()


# =================================================================================================
# spec-f5: lệnh `send` qua `SingleInstance`
# =================================================================================================


def test_acquire_with_send_message_emits_send_requested_with_paths(qtbot, instances, tmp_path):
    from teleport.ui import ipc

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    a, b = file_path(tmp_path, "b.txt"), file_path(tmp_path, "a ảnh.jpg")
    assert instances(d, wait=3.0, message=ipc.encode_send([Path(a), Path(b)])) is None
    qtbot.waitUntil(lambda: bool(seen.sends), timeout=3_000)
    qtbot.wait(100)
    assert seen.sends == [(Path(a), Path(b))], "đúng thứ tự, đúng một lần"
    assert isinstance(seen.sends[0], tuple) and all(isinstance(p, Path) for p in seen.sends[0])
    assert seen.shows == 0


def test_acquire_default_message_is_still_show(qtbot, instances, tmp_path):
    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    assert instances(d, wait=3.0) is None
    qtbot.waitUntil(lambda: seen.shows == 1, timeout=3_000)
    assert seen.sends == []


@pytest.mark.slow
def test_raw_send_emits_once_and_server_closes(qtbot, instances, tmp_path):
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    a = file_path(tmp_path, "x.txt")
    sock = raw_client(qtbot, server_name(d, key_of(d)), send_payload(a))
    qtbot.waitUntil(lambda: bool(seen.sends), timeout=3_000)
    qtbot.waitUntil(lambda: closed(sock), timeout=3_000)
    qtbot.wait(200)
    assert seen.sends == [(Path(a),)] and seen.shows == 0
    sock.abort()


def _write_unwaited(name: str, payload: bytes) -> QLocalSocket:
    """Nối và ghi mà không chặn chờ (server cùng tiến trình cần vòng sự kiện để đọc lệnh lớn)."""
    sock = QLocalSocket()
    sock.connectToServer(name)
    assert sock.waitForConnected(2000), sock.errorString()
    sock.write(payload)
    sock.flush()
    return sock


@pytest.mark.slow
def test_send_of_exactly_max_bytes_is_accepted(qtbot, instances, tmp_path):
    from teleport.ui import ipc
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    paths = [host_long_path(4000, i) for i in range(32)] + [host_long_path(3033, 99)]
    payload = send_payload(*paths)
    assert len(payload) == ipc.MAX_BYTES
    sock = _write_unwaited(server_name(d, key_of(d)), payload)
    try:
        qtbot.waitUntil(lambda: bool(seen.sends), timeout=5_000)
        assert seen.sends == [tuple(Path(p) for p in paths)]
    finally:
        sock.abort()


@pytest.mark.slow
def test_send_one_byte_over_max_bytes_is_dropped(qtbot, instances, tmp_path):
    from teleport.ui import ipc
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    paths = [host_long_path(4000, i) for i in range(32)] + [host_long_path(3034, 99)]
    payload = send_payload(*paths)
    assert len(payload) == ipc.MAX_BYTES + 1
    sock = _write_unwaited(server_name(d, key_of(d)), payload)
    try:
        qtbot.waitUntil(lambda: closed(sock), timeout=4_000)
        qtbot.wait(300)
        assert seen.nothing
    finally:
        sock.abort()


def _bad_payloads(tmp_path: Path) -> dict[str, bytes]:
    a = file_path(tmp_path, "a.txt").encode("utf-8")
    sep = b"\\" if os.name == "nt" else b"/"
    return {
        "no-terminator": b"send\n" + a + b"\n",
        "no-paths": b"send\n\n",
        "three-lf": b"send\n\n\n",
        "relative": b"send\nrel.txt\n\n",
        "dotdot": b"send\n" + a + sep + b".." + sep + b"x\n\n",
        "trailing": b"send\n" + a + b"\n\nX",
        "blank-between": b"send\n" + a + b"\n\n" + a + b"2\n\n",
        "bad-utf8": b"send\n" + a + b"\xff\n\n",
        "overlong": b"send\n" + a + b"\xc0\xaf\n\n",
        "nul": b"send\n" + a + b"\x00b\n\n",
        "bom": b"\xef\xbb\xbfsend\n" + a + b"\n\n",
        "crlf": b"send\r\n" + a + b"\r\n\r\n",
        "upper": b"SEND\n" + a + b"\n\n",
        "space": b"send \n" + a + b"\n\n",
        "show-then-send": b"show\nsend\n" + a + b"\n\n",
        "dup": b"send\n" + a + b"\n" + a + b"\n\n",
        "unc-device": b"send\n\\\\.\\PhysicalDrive0\n\n",
        "long-line": b"send\n" + b"a" * 16384,
        "junk-17": b"x" * 17,
    }


BAD_IDS = list(_bad_payloads(Path("/t")).keys())


@pytest.mark.slow
@pytest.mark.parametrize("case", BAD_IDS)
def test_bad_send_payload_emits_nothing_and_is_closed_within_deadline(qtbot, instances, tmp_path, case):
    """Tiêu chí 8: payload xấu (mục 8) → không signal nào, kết nối đóng trong `IPC_READ_TIMEOUT_MS` + dung
    sai."""
    from teleport.ui.app import IPC_READ_TIMEOUT_MS, server_name

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    t0 = time.monotonic()
    sock = raw_client(qtbot, server_name(d, key_of(d)), _bad_payloads(tmp_path)[case])
    qtbot.waitUntil(lambda: closed(sock), timeout=IPC_READ_TIMEOUT_MS + 3_000)
    elapsed = time.monotonic() - t0
    assert elapsed < IPC_READ_TIMEOUT_MS / 1000 + 2.0, f"server giữ kết nối {elapsed:.1f} s"
    qtbot.wait(200)
    assert seen.nothing, (case, seen.shows, seen.sends)
    sock.abort()


@pytest.mark.slow
def test_byte_drip_is_cut_by_whole_command_deadline(qtbot, instances, tmp_path):
    """Hạn 1 s tính cho **cả lệnh** từ lúc nhận kết nối: gửi từng byte cách 100 ms thì bị cắt ở ~1 s, dù
    mỗi byte tới trước hạn của byte trước."""
    from teleport.ui.app import IPC_READ_TIMEOUT_MS, server_name

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    payload = send_payload(file_path(tmp_path, "nho-giot.txt"))
    sock = QLocalSocket()
    sock.connectToServer(server_name(d, key_of(d)))
    assert sock.waitForConnected(2000)
    t0 = time.monotonic()
    try:
        for i in range(len(payload)):
            if closed(sock):
                break
            sock.write(payload[i : i + 1])
            sock.flush()
            qtbot.wait(100)
            assert time.monotonic() - t0 < IPC_READ_TIMEOUT_MS / 1000 + 2.5, (
                "server không cắt kết nối nhỏ giọt"
            )
        qtbot.waitUntil(lambda: closed(sock), timeout=3_000)
        elapsed = time.monotonic() - t0
        assert elapsed < IPC_READ_TIMEOUT_MS / 1000 + 1.5, f"cắt sau {elapsed:.1f} s"
        assert seen.nothing
    finally:
        sock.abort()


@pytest.mark.slow
def test_logs_never_contain_paths_and_drop_reason_is_debug(qtbot, instances, tmp_path, caplog):
    """N9 (mục 2.4): bỏ lệnh chỉ ghi DEBUG kèm mã lý do; không bao giờ ghi đường dẫn hay byte nhận được."""
    import logging

    from teleport.ui.app import server_name

    caplog.set_level(logging.DEBUG)
    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    secret = "BiMat-QZX7"
    a, b = file_path(tmp_path, f"{secret}-1.txt"), file_path(tmp_path, f"{secret}-2.txt")
    payloads = [
        send_payload(a, b),  # hợp lệ
        send_payload(a, a),  # dup
        f"send\n{secret}.txt\n\n".encode(),  # path
        send_payload(a) + secret.encode(),  # trailing
    ]
    name = server_name(d, key_of(d))
    for payload in payloads:
        sock = raw_client(qtbot, name, payload)
        qtbot.waitUntil(lambda s=sock: closed(s), timeout=3_000)
        sock.abort()
    qtbot.wait(200)
    assert seen.sends == [(Path(a), Path(b))]
    ours = [r for r in caplog.records if r.name.startswith("teleport")]
    text = "\n".join(r.getMessage() for r in ours)
    assert secret not in text and str(tmp_path) not in text, text
    assert not [r for r in ours if r.levelno >= logging.WARNING], "bỏ lệnh chỉ ghi DEBUG"
    debug = "\n".join(r.getMessage() for r in ours if r.levelno == logging.DEBUG)
    for reason in ("dup", "path", "trailing"):
        assert reason in debug, f"thiếu mã lý do {reason!r} trong log DEBUG: {debug!r}"


@pytest.mark.slow
def test_stop_listening_then_send_times_out(qtbot, instances, tmp_path):
    """D10: bản một đang thoát → bản hai gửi `send` cũng hết hạn (TimeoutError), không coi là đã gửi."""
    from teleport.ui import ipc

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    first.stop_listening()
    seen = Seen(first)
    with pytest.raises(TimeoutError):
        instances(d, wait=1.0, message=ipc.encode_send([Path(file_path(tmp_path, "a.txt"))]))
    assert seen.nothing


# =================================================================================================
# spec-f5 mục 5 điểm 2: tiến trình thứ hai `--send`
# =================================================================================================


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.pop("TELEPORT_HOME", None)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO / "src"), env.get("PYTHONPATH", "")) if p)
    return env


def _run_second(qtbot, args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "teleport.ui", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
        cwd=str(cwd) if cwd is not None else None,
    )
    try:
        qtbot.waitUntil(lambda: proc.poll() is not None, timeout=30_000)
        _out, err = proc.communicate(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=10)
    return proc.returncode, err.decode("utf-8", "replace")


@pytest.mark.slow
def test_second_process_send_exits_0_and_first_gets_paths_in_order(qtbot, instances, tmp_path):
    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    a, b = file_path(tmp_path, "z.mp4"), file_path(tmp_path, "a.pdf")
    code, err = _run_second(qtbot, ["--config-dir", str(d), "--send", a, b])
    assert code == 0, err
    qtbot.waitUntil(lambda: bool(seen.sends), timeout=5_000)
    qtbot.wait(200)
    assert seen.sends == [(Path(a), Path(b))]
    assert seen.shows == 0


@pytest.mark.slow
def test_second_process_relative_send_path_is_joined_with_its_cwd(qtbot, instances, tmp_path):
    """D2: đường dẫn tương đối nối với thư mục hiện tại của tiến trình gọi."""
    d = tmp_path / "cfg"
    work = tmp_path / "noi-lam-viec"
    work.mkdir()
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    code, err = _run_second(qtbot, ["--config-dir", str(d), "--send", "tuong-doi.txt"], cwd=work)
    assert code == 0, err
    qtbot.waitUntil(lambda: bool(seen.sends), timeout=5_000)
    [paths] = seen.sends
    assert len(paths) == 1
    assert os.path.normcase(str(paths[0])) == os.path.normcase(str(work / "tuong-doi.txt"))


@pytest.mark.slow
def test_second_process_empty_send_acts_like_show(qtbot, instances, tmp_path):
    """D2: `--send` không có đường dẫn chạy như mở bình thường (lệnh `show`)."""
    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    code, err = _run_second(qtbot, ["--config-dir", str(d), "--send"])
    assert code == 0, err
    qtbot.waitUntil(lambda: seen.shows == 1, timeout=5_000)
    assert seen.sends == []


@pytest.mark.slow
def test_selftest_ignores_send(tmp_path, qtbot):
    """Mục 2.4: có `--selftest` thì bỏ qua `--send` (kể cả đường dẫn sai)."""
    code, err = _run_second(qtbot, ["--selftest", "--config-dir", str(tmp_path / "cfg"), "--send", BAD_ARG])
    assert code == 0, err
    assert not (tmp_path / "cfg" / "gui.lock").exists()


# =================================================================================================
# spec-f5 mục 5 điểm 9: tham số sai → hộp lỗi, EXIT_USAGE, không khóa, không nối server
# =================================================================================================


def _noop(run):
    return
    yield


def _error_text(run) -> str:
    texts = [t for t in run.exec_texts if SEND_HEAD in t]
    assert len(texts) == 1, run.exec_texts
    return texts[0]


def test_bad_send_argument_shows_error_exits_usage_and_takes_no_lock(gui, tmp_path):
    from teleport.ui.app import EXIT_USAGE

    d = tmp_path / "cfg"
    d.mkdir()
    run = gui(
        ["--config-dir", str(d), "--send", file_path(tmp_path, "ok.txt"), BAD_ARG, "/thu/../hai"],
        _noop,
        require_script_done=False,
    )
    assert run.code == EXIT_USAGE == 2
    assert run.windows == [] and run.hosts == []
    assert BAD_ARG + BAD_PATH_TAIL in _error_text(run), "câu lỗi nêu đường dẫn sai đầu tiên"
    assert not (d / "gui.lock").exists(), "không lấy gui.lock khi tham số sai"
    assert not (d / "gui.key").exists()


def test_bad_send_argument_does_not_contact_running_instance(gui, instances, qtbot, tmp_path):
    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    run = gui(["--config-dir", str(d), "--send", BAD_ARG], _noop, require_script_done=False)
    qtbot.wait(300)
    assert run.code == 2
    assert seen.nothing, "tham số sai: không gửi show hay send cho bản đang chạy"


def test_bad_send_hint_cuts_long_path_to_120_characters(gui, tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    long_bad = BAD_ARG[:-4] + "đ" * 300 + ".txt"
    run = gui(["--config-dir", str(d), "--send", long_bad], _noop, require_script_done=False)
    assert run.code == 2
    text = _error_text(run)
    assert BAD_PATH_TAIL in text
    shown = text.split(BAD_PATH_TAIL)[0].split("\n")[-1]
    assert 0 < len(shown) <= 120, (len(shown), shown)
    assert long_bad not in text


def test_too_many_send_arguments_hint_matches_selection_error(gui, tmp_path):
    from teleport.core.messages import MAX_OFFER_FILES
    from teleport.ui import send

    d = tmp_path / "cfg"
    d.mkdir()
    args = [file_path(tmp_path, f"f{i:05d}.txt") for i in range(MAX_OFFER_FILES + 1)]
    run = gui(["--config-dir", str(d), "--send", *args], _noop, require_script_done=False)
    assert run.code == 2
    expected = send.selection_error([Path(a) for a in args])
    assert expected and expected in _error_text(run)
    assert not (d / "gui.lock").exists()


def test_too_large_send_command_hint(gui, tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    args = [host_long_path(4000, i) for i in range(40)]  # > 128 KiB, ít hơn trần số file
    run = gui(["--config-dir", str(d), "--send", *args], _noop, require_script_done=False)
    assert run.code == 2
    assert TOO_LARGE_HINT in _error_text(run)
    assert not (d / "gui.lock").exists()


def test_in_process_send_to_running_instance_exits_0(gui, instances, qtbot, tmp_path):
    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    a, b = file_path(tmp_path, "1.txt"), file_path(tmp_path, "2.txt")
    run = gui(["--config-dir", str(d), "--send", a, b, a], _noop, require_script_done=False)
    qtbot.waitUntil(lambda: bool(seen.sends), timeout=3_000)
    assert run.code == 0
    assert run.windows == [], "bản thứ hai không mở cửa sổ"
    assert seen.sends == [(Path(a), Path(b))], "bỏ trùng, giữ thứ tự lần đầu"


def test_in_process_relative_send_uses_cwd(gui, instances, qtbot, tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    work = tmp_path / "cwd"
    work.mkdir()
    first = instances(d)
    assert first is not None
    seen = Seen(first)
    monkeypatch.chdir(work)
    run = gui(["--config-dir", str(d), "--send", "r.txt"], _noop, require_script_done=False)
    qtbot.waitUntil(lambda: bool(seen.sends), timeout=3_000)
    assert run.code == 0
    [paths] = seen.sends
    assert [os.path.normcase(str(p)) for p in paths] == [os.path.normcase(os.path.join(os.getcwd(), "r.txt"))]
