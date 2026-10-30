"""Một bản mỗi thư mục cấu hình: `SingleInstance`, `server_name`, giao thức IPC `show`
(spec-p4-3 D3, D4, D5, mục 6.4 và mục 10).

`gui.lock` bảo đảm một bản; bản thứ hai nối `QLocalSocket` gửi đúng `b"show\\n"`. Server đọc tối đa 16
byte trong 1 s, khớp thì phát `show_requested`, khác thì bỏ qua, luôn đóng socket; tối đa 4 kết nối
cùng lúc, mỗi kết nối một hạn 1 s riêng. Socket cục bộ (named pipe / Unix socket), không TCP.
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


def expected_name(config_dir: Path, user: str | None = None) -> str:
    """Tên server theo D4 + mục 10, tính độc lập với code."""
    if user is None:
        user = getpass.getuser()
    norm = os.path.normcase(os.path.realpath(config_dir))
    return "teleport-" + hashlib.sha256(f"{user}\0{norm}".encode()).hexdigest()[:24]


@pytest.fixture
def instances(qtbot):
    """`acquire(config_dir, wait=…)`; mọi bản đã lấy được đóng khi test xong."""
    got: list = []

    def acquire(config_dir: Path, wait: float = 3.0):
        from teleport.ui.app import SingleInstance

        inst = SingleInstance.acquire(config_dir, wait=wait)
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


def closed(sock: QLocalSocket) -> bool:
    return sock.state() == QLocalSocket.LocalSocketState.UnconnectedState


# --- server_name ------------------------------------------------------------------------------------


def test_server_name_matches_spec_formula(tmp_path):
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    d.mkdir()
    assert server_name(d) == expected_name(d)


def test_server_name_is_stable_and_hides_path(tmp_path):
    from teleport.ui.app import server_name

    d = tmp_path / "Thu-Muc-Cau-Hinh"
    d.mkdir()
    name = server_name(d)
    assert name == server_name(Path(str(d)))
    assert name == server_name(Path(str(d) + os.sep)), "dấu phân cách cuối không đổi tên"
    assert name == server_name(d / "con" / ".."), "đường dẫn chưa chuẩn hóa cho cùng tên"
    assert len(name) == len("teleport-") + 24
    assert name.startswith("teleport-")
    assert all(c in "0123456789abcdef" for c in name[len("teleport-") :])
    assert "Thu-Muc" not in name and str(tmp_path) not in name


def test_server_name_relative_path_equals_absolute(tmp_path, monkeypatch):
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    d.mkdir()
    monkeypatch.chdir(tmp_path)
    assert server_name(Path("cfg")) == server_name(d)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows không phân biệt hoa thường trong đường dẫn")
def test_server_name_ignores_case_on_windows(tmp_path):
    from teleport.ui.app import server_name

    d = tmp_path / "Cfg"
    d.mkdir()
    assert server_name(Path(str(d).upper())) == server_name(Path(str(d).lower()))
    assert server_name(Path(str(d).replace("\\", "/"))) == server_name(d)


def test_server_name_differs_per_config_dir_and_per_user(tmp_path, monkeypatch):
    from teleport.ui import app as ui_app

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert ui_app.server_name(a) != ui_app.server_name(b)
    monkeypatch.setattr(getpass, "getuser", lambda: "nguoi-khac")
    assert ui_app.server_name(a) == expected_name(a, "nguoi-khac")


def test_server_name_when_getuser_fails_uses_empty_user(tmp_path, monkeypatch):
    from teleport.ui.app import server_name

    def broken():
        raise OSError("không có tên người dùng")

    monkeypatch.setattr(getpass, "getuser", broken)
    d = tmp_path / "cfg"
    d.mkdir()
    assert server_name(d) == expected_name(d, "")


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
        sock = raw_client(qtbot, server_name(d), payload)
    # Server luôn đóng socket (hết 1 s hoặc đọc đủ 16 byte).
    qtbot.waitUntil(lambda: closed(sock), timeout=3_000)
    sock.abort()


@pytest.mark.slow
def test_silent_client_is_dropped_after_about_1s(qtbot, instances, tmp_path):
    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    first = instances(d)
    assert first is not None
    t0 = time.monotonic()
    sock = raw_client(qtbot, server_name(d), None)
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
    name = server_name(d)
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
    sock = raw_client(qtbot, server_name(d), SHOW)
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

    from teleport.ui.app import server_name

    d = tmp_path / "cfg"
    d.mkdir()
    squatter = QLocalServer()
    assert squatter.listen(server_name(d)), squatter.errorString()
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
