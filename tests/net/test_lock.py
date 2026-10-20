"""Test `teleport.net.lock` theo kế hoạch §2.5 và phụ lục điểm 14."""

import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from teleport.net.lock import LOCK_FILE, NodeLock, NodeLockedError

CHILD_TIMEOUT = 30

# Tiến trình con giữ khóa: in "ready" khi đã giữ, rồi chờ lệnh trên stdin.
#   "exit"  → thoát bằng os._exit (không release, không finalizer: HĐH phải tự nhả)
#   "sleep" → ngủ mãi (để bị kill)
CHILD_HOLDER = textwrap.dedent(
    """
    import os, sys, time
    from pathlib import Path
    from teleport.net.lock import NodeLock
    lock = NodeLock(Path(sys.argv[1]))
    lock.acquire()
    print("ready", flush=True)
    cmd = sys.stdin.readline().strip()
    if cmd == "sleep":
        time.sleep(3600)
    os._exit(0)
    """
)

# Tiến trình con thử lấy khóa một lần rồi in kết quả.
CHILD_TRY = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from teleport.net.lock import NodeLock, NodeLockedError
    lock = NodeLock(Path(sys.argv[1]))
    try:
        lock.acquire()
    except NodeLockedError:
        print("locked", flush=True)
    else:
        print("acquired", flush=True)
        lock.release()
    """
)


def _readline(proc: subprocess.Popen, timeout: float = CHILD_TIMEOUT) -> str:
    result: list[str] = []
    t = threading.Thread(target=lambda: result.append(proc.stdout.readline()), daemon=True)
    t.start()
    t.join(timeout)
    if not result:
        proc.kill()
        raise AssertionError(f"tiến trình con không trả lời sau {timeout}s; stderr: {proc.stderr.read()}")
    return result[0].strip()


def _spawn(script: str, cfg: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, str(cfg)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.fixture
def holder(tmp_path):
    """Tiến trình con đang giữ khóa của `tmp_path / 'cfg'`."""
    cfg = tmp_path / "cfg"
    proc = _spawn(CHILD_HOLDER, cfg)
    try:
        line = _readline(proc)
        assert line == "ready", f"con không lấy được khóa: {line!r} / {proc.stderr.read()}"
        yield cfg, proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(CHILD_TIMEOUT)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


# --- Cơ bản ------------------------------------------------------------------------------------


def test_lock_file_name():
    assert LOCK_FILE == "node.lock"


def test_acquire_creates_dir_and_file(tmp_path):
    cfg = tmp_path / "a" / "b" / "cfg"
    lock = NodeLock(cfg)
    assert lock.path == cfg / "node.lock"
    assert lock.held is False
    assert not cfg.exists()
    lock.acquire()
    try:
        assert lock.held is True
        assert cfg.is_dir()
        assert lock.path.is_file()
    finally:
        lock.release()
    assert lock.held is False


def test_constructor_does_not_touch_disk(tmp_path):
    cfg = tmp_path / "cfg"
    NodeLock(cfg)
    assert not cfg.exists()


def test_second_lock_same_dir_is_rejected_until_release(tmp_path):
    first = NodeLock(tmp_path)
    second = NodeLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(NodeLockedError, match="đang chạy"):
            second.acquire()
        assert second.held is False
        assert first.held is True
        # Thử lại vẫn bị từ chối; lần thất bại trước không làm hỏng khóa đang giữ.
        with pytest.raises(NodeLockedError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    assert second.held is True
    second.release()


def test_lock_with_different_path_spelling_still_conflicts(tmp_path):
    (tmp_path / "sub").mkdir()
    first = NodeLock(tmp_path)
    second = NodeLock(tmp_path / "sub" / "..")
    with first, pytest.raises(NodeLockedError):
        second.acquire()


def test_context_manager_acquires_and_releases(tmp_path):
    lock = NodeLock(tmp_path)
    with lock as entered:
        assert entered is lock
        assert lock.held is True
        with pytest.raises(NodeLockedError):
            NodeLock(tmp_path).acquire()
    assert lock.held is False
    with NodeLock(tmp_path) as again:
        assert again.held is True


def test_context_manager_releases_on_exception(tmp_path):
    lock = NodeLock(tmp_path)
    with pytest.raises(ZeroDivisionError), lock:
        1 / 0  # noqa: B018
    assert lock.held is False
    other = NodeLock(tmp_path)
    other.acquire()
    other.release()


def test_context_manager_when_already_locked_raises_on_enter(tmp_path):
    with NodeLock(tmp_path):
        entered = False
        with pytest.raises(NodeLockedError), NodeLock(tmp_path):
            entered = True
        assert entered is False


def test_release_many_times_is_fine(tmp_path):
    lock = NodeLock(tmp_path)
    lock.release()  # chưa acquire
    lock.acquire()
    lock.release()
    lock.release()
    lock.release()
    assert lock.held is False


def test_release_does_not_delete_file(tmp_path):
    lock = NodeLock(tmp_path)
    lock.acquire()
    lock.release()
    assert lock.path.exists()


def test_existing_lock_file_does_not_block(tmp_path):
    # File node.lock còn sót (ví dụ sau khi tiến trình chết) không có nghĩa là đang bị giữ.
    (tmp_path / LOCK_FILE).write_bytes(b"")
    with NodeLock(tmp_path) as lock:
        assert lock.held is True


def test_acquire_twice_same_object_raises_runtime_error(tmp_path):
    lock = NodeLock(tmp_path)
    lock.acquire()
    try:
        with pytest.raises(RuntimeError):
            lock.acquire()
        assert lock.held is True
        with pytest.raises(NodeLockedError):
            NodeLock(tmp_path).acquire()
    finally:
        lock.release()


def test_reacquire_after_release_same_object(tmp_path):
    lock = NodeLock(tmp_path)
    for _ in range(3):
        lock.acquire()
        assert lock.held is True
        lock.release()
        assert lock.held is False


def test_two_dirs_are_independent(tmp_path):
    a = NodeLock(tmp_path / "a")
    b = NodeLock(tmp_path / "b")
    with a, b:
        assert a.held and b.held


def test_threads_racing_in_same_process_only_one_wins(tmp_path):
    # Hai NodeLock trong cùng tiến trình cũng loại trừ nhau, kể cả khi tranh nhau từ nhiều luồng.
    locks = [NodeLock(tmp_path) for _ in range(8)]
    barrier = threading.Barrier(len(locks))
    results: list[str] = []
    guard = threading.Lock()

    def worker(lock: NodeLock) -> None:
        barrier.wait()
        try:
            lock.acquire()
            outcome = "won"
        except NodeLockedError:
            outcome = "locked"
        except Exception as e:
            outcome = f"lỗi khác: {e!r}"
        with guard:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(lk,)) for lk in locks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(CHILD_TIMEOUT)
    try:
        assert sorted(results) == ["locked"] * 7 + ["won"]
        assert sum(lk.held for lk in locks) == 1
    finally:
        for lk in locks:
            lk.release()


def test_does_not_write_pid(tmp_path):
    with NodeLock(tmp_path) as lock:
        pass
    assert str(os.getpid()).encode() not in lock.path.read_bytes()


def test_no_file_handle_leaked_after_contention(tmp_path):
    # Trên Windows file còn handle mở thì không xóa được → phát hiện rò fd.
    first = NodeLock(tmp_path)
    first.acquire()
    for _ in range(5):
        with pytest.raises(NodeLockedError):
            NodeLock(tmp_path).acquire()
    first.release()
    os.remove(first.path)
    assert not first.path.exists()


def test_other_os_error_is_not_node_locked_error(tmp_path):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    lock = NodeLock(not_a_dir)
    with pytest.raises(OSError) as exc:
        lock.acquire()
    assert not isinstance(exc.value, NodeLockedError)
    assert lock.held is False


def test_node_locked_error_message_says_teleport_is_running(tmp_path):
    with NodeLock(tmp_path), pytest.raises(NodeLockedError) as exc:
        NodeLock(tmp_path).acquire()
    assert "Teleport" in str(exc.value)
    assert "đang chạy" in str(exc.value)


# --- Nhiều tiến trình --------------------------------------------------------------------------


def test_child_holding_lock_blocks_parent_then_exit_releases(holder):
    cfg, proc = holder
    lock = NodeLock(cfg)
    with pytest.raises(NodeLockedError):
        lock.acquire()
    proc.stdin.write("exit\n")
    proc.stdin.flush()
    assert proc.wait(CHILD_TIMEOUT) == 0
    lock.acquire()  # HĐH tự nhả khi tiến trình thoát (con không gọi release)
    assert lock.held is True
    lock.release()


def test_killed_child_releases_lock(holder):
    cfg, proc = holder
    proc.stdin.write("sleep\n")
    proc.stdin.flush()
    with pytest.raises(NodeLockedError):
        NodeLock(cfg).acquire()
    proc.kill()
    proc.wait(CHILD_TIMEOUT)
    with NodeLock(cfg) as lock:
        assert lock.held is True


def test_parent_holding_lock_blocks_child(tmp_path):
    cfg = tmp_path / "cfg"
    with NodeLock(cfg):
        out = subprocess.run(
            [sys.executable, "-c", CHILD_TRY, str(cfg)],
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT,
        )
        assert out.stdout.strip() == "locked", out.stderr
    out = subprocess.run(
        [sys.executable, "-c", CHILD_TRY, str(cfg)],
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT,
    )
    assert out.stdout.strip() == "acquired", out.stderr


# --- Riêng từng HĐH ----------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="flock chỉ có trên POSIX")
def test_posix_uses_flock_so_raw_flock_conflicts(tmp_path):
    import fcntl

    with NodeLock(tmp_path) as lock:
        fd = os.open(lock.path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    fd = os.open(tmp_path / LOCK_FILE, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(NodeLockedError):
            NodeLock(tmp_path).acquire()
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name != "posix", reason="flock chỉ có trên POSIX")
def test_posix_child_flock_conflicts(tmp_path):
    script = textwrap.dedent(
        """
        import fcntl, os, sys
        fd = os.open(sys.argv[1], os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("locked")
        else:
            print("acquired")
        """
    )
    with NodeLock(tmp_path) as lock:
        out = subprocess.run(
            [sys.executable, "-c", script, str(lock.path)],
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT,
        )
    assert out.stdout.strip() == "locked", out.stderr


@pytest.mark.skipif(os.name != "nt", reason="msvcrt.locking chỉ có trên Windows")
def test_windows_locks_byte_zero(tmp_path):
    import msvcrt

    with NodeLock(tmp_path) as lock:
        fd = os.open(lock.path, os.O_RDWR)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            with pytest.raises(OSError):
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        finally:
            os.close(fd)

    # Ngược lại: ai đó giữ byte 0 bằng msvcrt → NodeLock phải báo NodeLockedError.
    fd = os.open(tmp_path / LOCK_FILE, os.O_RDWR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        try:
            with pytest.raises(NodeLockedError):
                NodeLock(tmp_path).acquire()
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)
    with NodeLock(tmp_path) as lock:
        assert lock.held is True


@pytest.mark.skipif(os.name != "nt", reason="chỉ Windows")
def test_windows_reacquire_many_times_does_not_leak_or_drift(tmp_path):
    # Phụ lục điểm 14: seek(0) trước cả LK_NBLCK lẫn LK_UNLCK; nếu quên, lần unlock/lock sau lệch byte.
    lock = NodeLock(tmp_path)
    for _ in range(20):
        lock.acquire()
        with pytest.raises(NodeLockedError):
            NodeLock(tmp_path).acquire()
        lock.release()
        with NodeLock(tmp_path):
            pass
    os.remove(tmp_path / LOCK_FILE)
