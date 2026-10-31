"""Test `teleport.app.history` (P4-2, việc 6: T10–T18; spec `specs/spec-p4-2.md` mục 2, 3.2, 4, 7, 11;
C3 lịch sử theo từng máy: `specs/spec-dot-c.md` mục 3, 11).

Viết từ đặc tả, không từ code. Không mở socket: `HistoryStore` chạy với `DiskExecutor` thật trên
`tmp_path`. Seam (spec mục 3.2, 11.12): `history.load`, `history.save`, `history._read_limited` và
`history.HISTORY_MIN_INTERVAL` là thuộc tính module, đọc lúc dùng. Việc “treo” trong thread chờ một
`threading.Event`; test luôn nhả nó trước khi xong.

Nội dung file được kiểm độc lập bằng `json` (spec mục 4), không qua `decode_history`.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from teleport.app.diskio import DiskExecutor
from teleport.core.transfer import Direction

pytestmark = [
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
leakcheck = pytest.mark.usefixtures("no_leaks")

UTC = datetime.UTC
DEV = "d" * 32
TIDS = [f"{k:032x}" for k in range(1, 300)]
SECRET = "BI-MAT-NOI-DUNG-7f3a9c"
EMPTY = {"version": 1, "entries": []}


# --- Hàm phụ trợ ---------------------------------------------------------------------------------


def make_entry(k: int, **overrides):
    from teleport.core.history import HistoryEntry

    fields = {
        "direction": Direction.RECEIVE,
        "device_id": DEV,
        "device_name": "PC-Nha",
        "transfer_id": TIDS[k],
        "file_count": 1,
        "total_bytes": 10,
        "done_bytes": 10,
        "names": (f"tep-{k}.bin",),
        "result": "done",
        "finished_at": datetime.datetime(2026, 10, 28, 8, 0, k % 60, tzinfo=UTC),
        "saved_path": f"C:\\nhan\\tep-{k}.bin",
    }
    fields.update(overrides)
    return HistoryEntry(**fields)


def entry_json(k: int) -> dict:
    return {
        "direction": "receive",
        "device_id": DEV,
        "device_name": "PC-Nha",
        "transfer_id": TIDS[k],
        "file_count": 1,
        "total_bytes": 10,
        "done_bytes": 10,
        "names": [f"tep-{k}.bin"],
        "result": "done",
        "finished_at": f"2026-10-28T08:00:{k % 60:02d}Z",
        "saved_path": f"C:\\nhan\\tep-{k}.bin",
    }


def write_file(config: Path, ks: list[int]) -> Path:
    path = config / "history.json"
    path.write_text(json.dumps({"version": 1, "entries": [entry_json(k) for k in ks]}), encoding="utf-8")
    return path


def file_ids(config: Path) -> list[str] | None:
    """`transfer_id` trong `history.json` theo thứ tự (đọc độc lập bằng json); không đọc được → None."""
    try:
        obj = json.loads((config / "history.json").read_bytes())
    except (OSError, ValueError):
        return None
    return [e["transfer_id"] for e in obj["entries"]]


async def wait_until(predicate, timeout: float = 5.0, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.01)


def warnings_of(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("teleport") and r.levelno >= logging.WARNING]


def errors_logged(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


class SaveSpy:
    """Bọc `history.save` (chạy trong thread): ghi lại mỗi lần gọi; `gate` chặn lần gọi `gate_call`;
    `fail` ném lỗi đó thay vì ghi."""

    def __init__(self, real) -> None:
        self.real = real
        self.calls: list[SimpleNamespace] = []
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.gate: threading.Event | None = None
        self.gate_call = 1
        self.hit = threading.Event()
        self.fail: BaseException | None = None

    def __call__(self, config_dir, entries):
        rec = SimpleNamespace(t0=time.monotonic(), t1=None, ids=[e.transfer_id for e in entries])
        with self.lock:
            self.calls.append(rec)
            number = len(self.calls)
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            if self.gate is not None and number == self.gate_call:
                self.hit.set()
                if not self.gate.wait(30):
                    raise AssertionError("cổng chặn của test không được mở")
            if self.fail is not None:
                raise self.fail
            return self.real(config_dir, entries)
        finally:
            with self.lock:
                self.running -= 1
            rec.t1 = time.monotonic()


class LoadGate:
    """Thay `history.load` bằng bản chờ `gate` rồi gọi bản thật."""

    def __init__(self, real) -> None:
        self.real = real
        self.gate = threading.Event()
        self.hit = threading.Event()
        self.calls = 0

    def __call__(self, config_dir):
        self.calls += 1
        self.hit.set()
        if not self.gate.wait(30):
            raise AssertionError("cổng chặn của test không được mở")
        return self.real(config_dir)


@pytest.fixture
def HM():
    """Module `teleport.app.history` (import muộn: chưa có thì chỉ test dùng nó đỏ)."""
    from teleport.app import history

    return history


@pytest.fixture
async def executor():
    ex = DiskExecutor()
    yield ex
    await ex.wait_idle(10)
    ex.shutdown()


@pytest.fixture
def save_spy(HM, monkeypatch):
    spy = SaveSpy(HM.save)
    monkeypatch.setattr(HM, "save", spy)
    yield spy
    if spy.gate is not None:
        spy.gate.set()


@pytest.fixture
def load_gate(HM, monkeypatch):
    gate = LoadGate(HM.load)
    monkeypatch.setattr(HM, "load", gate)
    yield gate
    gate.gate.set()


@pytest.fixture
def config(tmp_path) -> Path:
    d = tmp_path / "cfg"
    d.mkdir()
    return d


async def loaded_store(HM, executor, config, timeout: float = 5.0):
    store = HM.HistoryStore(executor=executor, config_dir=config)
    await store.load(timeout)
    return store


# =================================================================================================
# Hàm của module (đồng bộ, chạy trong thread khi dùng thật)
# =================================================================================================


def test_constants_match_spec(HM):
    assert HM.HISTORY_NAME == "history.json"
    assert HM.HISTORY_MIN_INTERVAL == 1.0


def test_module_load_missing_file_is_empty_writable_and_creates_nothing(HM, config):
    """T10 (hàm module)."""
    got = HM.load(config)
    assert got.entries == []
    assert got.writable is True
    assert list(config.iterdir()) == []


def test_module_save_then_load_roundtrip(HM, config):
    entries = [make_entry(3), make_entry(2), make_entry(1)]
    HM.save(config, entries)
    assert file_ids(config) == [TIDS[3], TIDS[2], TIDS[1]]
    got = HM.load(config)
    assert got.entries == entries and got.writable is True


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"{" + SECRET.encode(), id="khong-phai-json"),
        pytest.param(json.dumps({"version": 2, "entries": [], "x": SECRET}).encode(), id="version-2"),
        pytest.param(json.dumps({"version": 1, "entries": SECRET}).encode(), id="entries-chuoi"),
        pytest.param(SECRET.encode() + b" " * (4 * 1024 * 1024 + 1), id="vuot-tran"),
    ],
)
def test_module_load_corrupt_file_is_empty_but_writable_and_logs_only_error_kind(HM, config, caplog, content):
    """T11 (hàm module): hỏng cả file → rỗng, WARNING, **ghi đè được** (writable True); log không có
    nội dung file hay đường dẫn."""
    caplog.set_level(logging.DEBUG, logger="teleport")
    (config / "history.json").write_bytes(content)
    got = HM.load(config)
    assert got.entries == [] and got.writable is True
    assert warnings_of(caplog), "phải có WARNING khi history.json hỏng"
    for r in caplog.records:
        msg = r.getMessage()
        assert SECRET not in msg
        assert str(config) not in msg


def test_module_load_unreadable_is_empty_and_not_writable(HM, config, monkeypatch, caplog):
    """T12 (hàm module): `_read_limited` ném PermissionError → rỗng, WARNING, writable False."""
    caplog.set_level(logging.WARNING)
    write_file(config, [1])

    def denied(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(HM, "_read_limited", denied)
    got = HM.load(config)
    assert got.entries == [] and got.writable is False
    assert warnings_of(caplog)
    assert all(str(config) not in r.getMessage() for r in caplog.records)


def test_module_load_directory_in_place_of_file_is_not_writable(HM, config):
    (config / "history.json").mkdir()
    got = HM.load(config)
    assert got.entries == [] and got.writable is False


def test_module_load_keeps_good_entries_when_one_is_corrupt(HM, config):
    obj = {"version": 1, "entries": [entry_json(1), {"direction": "x"}, entry_json(2)]}
    (config / "history.json").write_text(json.dumps(obj), encoding="utf-8")
    got = HM.load(config)
    assert [e.transfer_id for e in got.entries] == [TIDS[1], TIDS[2]]
    assert got.writable is True


def test_module_load_accepts_bom(HM, config):
    data = json.dumps({"version": 1, "entries": [entry_json(4)]}).encode()
    (config / "history.json").write_bytes(b"\xef\xbb\xbf" + data)
    assert [e.transfer_id for e in HM.load(config).entries] == [TIDS[4]]


# =================================================================================================
# HistoryStore
# =================================================================================================


@leakcheck
async def test_store_missing_file_is_empty_and_creates_nothing(HM, executor, config, save_spy):
    """T10."""
    store = await loaded_store(HM, executor, config)
    assert store.entries() == ()
    await store.flush(5)
    assert save_spy.calls == []
    assert list(config.iterdir()) == []


@leakcheck
async def test_store_entries_is_an_immutable_tuple_newest_first(HM, executor, config):
    write_file(config, [2, 1])
    store = await loaded_store(HM, executor, config)
    got = store.entries()
    assert isinstance(got, tuple)
    assert [e.transfer_id for e in got] == [TIDS[2], TIDS[1]]
    store.record(make_entry(3))
    assert [e.transfer_id for e in store.entries()] == [TIDS[3], TIDS[2], TIDS[1]]
    assert [e.transfer_id for e in got] == [TIDS[2], TIDS[1]], "bản đã trả không được đổi"
    await store.flush(5)


@leakcheck
async def test_store_corrupt_file_is_overwritten_on_next_record(HM, executor, config, caplog):
    """T11."""
    caplog.set_level(logging.DEBUG, logger="teleport")
    (config / "history.json").write_bytes(b"[[[" + SECRET.encode())
    store = await loaded_store(HM, executor, config)
    assert store.entries() == ()
    assert warnings_of(caplog)
    assert all(SECRET not in r.getMessage() for r in caplog.records)
    store.record(make_entry(1))
    await store.flush(5)
    obj = json.loads((config / "history.json").read_bytes())
    assert obj["version"] == 1
    assert [e["transfer_id"] for e in obj["entries"]] == [TIDS[1]]
    assert SECRET.encode() not in (config / "history.json").read_bytes()
    assert not [p.name for p in config.iterdir() if p.name != "history.json"], "không giữ bản sao"


@leakcheck
async def test_store_unreadable_file_is_never_overwritten_but_clear_writes_empty(
    HM, executor, config, monkeypatch, caplog, save_spy
):
    """T12. Đột biến “ghi khi writable=False” làm test này đỏ."""
    caplog.set_level(logging.WARNING)
    path = write_file(config, [7])
    before = path.read_bytes()

    def denied(p):
        raise PermissionError(13, "Permission denied", str(p))

    monkeypatch.setattr(HM, "_read_limited", denied)
    store = await loaded_store(HM, executor, config)
    assert store.entries() == ()
    assert warnings_of(caplog)
    store.record(make_entry(1))
    store.record(make_entry(2))
    assert [e.transfer_id for e in store.entries()] == [TIDS[2], TIDS[1]], "trong bộ nhớ vẫn ghi nhận"
    await asyncio.sleep(0.2)
    await store.flush(5)
    assert path.read_bytes() == before, "không đọc được thì không được ghi đè trong cả phiên"
    assert save_spy.calls == []
    store.clear()
    assert store.entries() == ()
    await store.flush(5)
    assert json.loads(path.read_bytes()) == EMPTY


@leakcheck
async def test_store_50_records_in_one_block_are_coalesced(HM, executor, config, save_spy):
    """T13: 50 lần `record` trong một khối đồng bộ sau khi nạp → `save` ≤ 2 lần, file đủ 50 mục."""
    store = await loaded_store(HM, executor, config)
    for k in range(50):
        store.record(make_entry(k))
    want = [TIDS[k] for k in reversed(range(50))]
    await wait_until(lambda: file_ids(config) == want, 5, "history.json chưa có đủ 50 mục")
    await asyncio.sleep(0.3)
    assert 1 <= len(save_spy.calls) <= 2, [len(c.ids) for c in save_spy.calls]
    assert save_spy.max_running == 1
    await store.flush(5)
    assert len(save_spy.calls) <= 2


@leakcheck
async def test_store_writes_are_spaced_by_min_interval(HM, executor, config, save_spy, monkeypatch):
    interval = 0.6
    monkeypatch.setattr(HM, "HISTORY_MIN_INTERVAL", interval)
    store = await loaded_store(HM, executor, config)
    store.record(make_entry(1))
    await wait_until(lambda: file_ids(config) == [TIDS[1]], 5, "lần ghi đầu chưa xong")
    store.record(make_entry(2))
    await wait_until(lambda: file_ids(config) == [TIDS[2], TIDS[1]], 5 + interval, "lần ghi sau chưa xong")
    assert len(save_spy.calls) == 2
    gap = save_spy.calls[1].t0 - save_spy.calls[0].t1
    assert gap >= interval - 0.05, f"hai lần ghi cách nhau {gap:.3f} s < {interval} s"
    await store.flush(5)


@leakcheck
async def test_store_second_write_waits_for_running_write_to_settle(
    HM, executor, config, save_spy, monkeypatch
):
    """Một lần ghi tại một thời điểm (`.tmp` tên cố định): lần sau chờ lần trước `settled()`."""
    monkeypatch.setattr(HM, "HISTORY_MIN_INTERVAL", 0.0)
    save_spy.gate = threading.Event()
    store = await loaded_store(HM, executor, config)
    store.record(make_entry(1))
    await wait_until(save_spy.hit.is_set, 5, "chưa có lần ghi nào")
    store.record(make_entry(2))
    await asyncio.sleep(0.5)
    assert len(save_spy.calls) == 1, "không được ghi lần hai khi lần ghi trước chưa xong"
    save_spy.gate.set()
    await wait_until(lambda: file_ids(config) == [TIDS[2], TIDS[1]], 5, "lần ghi sau chưa xong")
    assert len(save_spy.calls) == 2
    assert save_spy.max_running == 1
    assert save_spy.calls[1].t0 >= save_spy.calls[0].t1
    await store.flush(5)


@leakcheck
async def test_store_record_before_load_finishes_is_merged_on_top_and_not_written_early(
    HM, executor, config, save_spy, load_gate
):
    """T14. Đột biến “ghi trước khi nạp xong” làm test này đỏ."""
    write_file(config, [2, 1])
    store = HM.HistoryStore(executor=executor, config_dir=config)
    loading = asyncio.create_task(store.load(10))
    await wait_until(load_gate.hit.is_set, 5, "việc nạp chưa bắt đầu")
    store.record(make_entry(3))
    store.record(make_entry(4))
    await asyncio.sleep(0.4)
    assert save_spy.calls == [], "không được ghi trước khi nạp xong"
    assert file_ids(config) == [TIDS[2], TIDS[1]]
    load_gate.gate.set()
    await asyncio.wait_for(loading, 5)
    assert [e.transfer_id for e in store.entries()] == [TIDS[4], TIDS[3], TIDS[2], TIDS[1]]
    await store.flush(5)
    assert file_ids(config) == [TIDS[4], TIDS[3], TIDS[2], TIDS[1]]


@leakcheck
async def test_store_record_before_load_with_same_key_keeps_the_new_entry(HM, executor, config, load_gate):
    """Mục ghi trước lúc nạp xong trùng khóa với mục trong file: chỉ còn mục mới, ở đầu."""
    write_file(config, [2, 1])
    store = HM.HistoryStore(executor=executor, config_dir=config)
    loading = asyncio.create_task(store.load(10))
    await wait_until(load_gate.hit.is_set, 5, "việc nạp chưa bắt đầu")
    newer = make_entry(1, result="cancelled_user", done_bytes=3)
    store.record(newer)
    load_gate.gate.set()
    await asyncio.wait_for(loading, 5)
    got = store.entries()
    assert [e.transfer_id for e in got] == [TIDS[1], TIDS[2]]
    assert got[0] == newer
    await store.flush(5)


@leakcheck
async def test_store_never_loaded_never_writes(HM, executor, config, save_spy):
    """`load` không được gọi (restore chưa chạy) → không bao giờ ghi, kể cả lúc `flush`."""
    path = write_file(config, [1])
    before = path.read_bytes()
    store = HM.HistoryStore(executor=executor, config_dir=config)
    store.record(make_entry(5))
    assert [e.transfer_id for e in store.entries()] == [TIDS[5]]
    await asyncio.sleep(0.3)
    await store.flush(2)
    assert save_spy.calls == []
    assert path.read_bytes() == before


@leakcheck
async def test_store_clear_without_load_still_writes_empty_file(HM, executor, config):
    path = write_file(config, [1])
    store = HM.HistoryStore(executor=executor, config_dir=config)
    store.clear()
    await store.flush(5)
    assert json.loads(path.read_bytes()) == EMPTY


@leakcheck
async def test_store_clear_while_loading_discards_the_load_result(HM, executor, config, load_gate):
    """T15."""
    path = write_file(config, [2, 1])
    store = HM.HistoryStore(executor=executor, config_dir=config)
    loading = asyncio.create_task(store.load(10))
    await wait_until(load_gate.hit.is_set, 5, "việc nạp chưa bắt đầu")
    store.clear()
    assert store.entries() == ()
    load_gate.gate.set()
    await asyncio.wait_for(loading, 5)
    assert store.entries() == (), "kết quả nạp phải bị bỏ sau clear()"
    await store.flush(5)
    assert json.loads(path.read_bytes()) == EMPTY


@leakcheck
async def test_store_record_after_clear_while_loading_keeps_only_the_new_entry(
    HM, executor, config, load_gate
):
    write_file(config, [2, 1])
    store = HM.HistoryStore(executor=executor, config_dir=config)
    loading = asyncio.create_task(store.load(10))
    await wait_until(load_gate.hit.is_set, 5, "việc nạp chưa bắt đầu")
    store.clear()
    store.record(make_entry(9))
    load_gate.gate.set()
    await asyncio.wait_for(loading, 5)
    assert [e.transfer_id for e in store.entries()] == [TIDS[9]]
    await store.flush(5)
    assert file_ids(config) == [TIDS[9]]


@leakcheck
async def test_store_clear_writes_empty_file_and_keeps_it(HM, executor, config):
    path = write_file(config, [3, 2, 1])
    store = await loaded_store(HM, executor, config)
    assert len(store.entries()) == 3
    store.clear()
    assert store.entries() == ()
    await store.flush(5)
    assert path.exists(), "clear không xóa file"
    assert json.loads(path.read_bytes()) == EMPTY


@leakcheck
async def test_store_flush_writes_pending_last_entry_without_waiting_for_interval(
    HM, executor, config, save_spy, monkeypatch
):
    """T16 (phần 1): lần ghi sau đang chờ `HISTORY_MIN_INTERVAL` (60 s) → `flush` ghi ngay ảnh cuối."""
    monkeypatch.setattr(HM, "HISTORY_MIN_INTERVAL", 60.0)
    store = await loaded_store(HM, executor, config)
    store.record(make_entry(1))
    await wait_until(lambda: file_ids(config) == [TIDS[1]], 5, "lần ghi đầu chưa xong")
    store.record(make_entry(2))
    await asyncio.sleep(0.2)
    assert len(save_spy.calls) == 1
    t0 = time.monotonic()
    await store.flush(5)
    assert time.monotonic() - t0 < 3
    assert file_ids(config) == [TIDS[2], TIDS[1]]
    assert len(save_spy.calls) == 2


@leakcheck
async def test_store_flush_is_bounded_when_save_hangs_and_never_writes_twice(
    HM, executor, config, save_spy, caplog
):
    """T16 (phần 2): `save` treo quá hạn → `flush` trả về ≤ hạn + 0,5 s, WARNING, không ghi lần hai."""
    caplog.set_level(logging.WARNING)
    save_spy.gate = threading.Event()
    store = await loaded_store(HM, executor, config)
    store.record(make_entry(1))
    await wait_until(save_spy.hit.is_set, 5, "chưa có lần ghi nào")
    store.record(make_entry(2))  # còn bẩn khi xả
    limit = 0.5
    t0 = time.monotonic()
    await store.flush(limit)
    elapsed = time.monotonic() - t0
    assert elapsed <= limit + 0.5, f"flush mất {elapsed:.2f} s dù hạn {limit} s"
    assert warnings_of(caplog), "phải có WARNING khi xả quá hạn"
    assert len(save_spy.calls) == 1, "không được ghi lần hai khi lần ghi trước chưa xong (cùng .tmp)"
    store.stop()
    save_spy.gate.set()
    await asyncio.sleep(0.3)
    assert len(save_spy.calls) == 1
    assert save_spy.max_running == 1


@leakcheck
async def test_store_flush_swallows_save_error_with_warning(HM, executor, config, save_spy, caplog):
    """T16 mở rộng (spec 11.7): `save` ném OSError → `flush` không ném, chỉ WARNING; không ERROR."""
    caplog.set_level(logging.WARNING)
    save_spy.fail = OSError(28, "No space left on device")
    store = await loaded_store(HM, executor, config)
    store.record(make_entry(1))
    await store.flush(5)
    await asyncio.sleep(0.1)
    assert save_spy.calls, "phải có lần ghi"
    assert warnings_of(caplog)
    assert errors_logged(caplog) == [], [r.getMessage() for r in errors_logged(caplog)]
    assert all("tep-1.bin" not in r.getMessage() for r in caplog.records)


class FailingSave:
    """Thay `history.save`: `fail_first` lần gọi đầu ném OSError, các lần sau ghi thật."""

    def __init__(self, real, fail_first: int) -> None:
        self.real = real
        self.fail_first = fail_first
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, config_dir, entries):
        with self.lock:
            self.calls += 1
            number = self.calls
        if number <= self.fail_first:
            raise OSError(28, "No space left on device")
        return self.real(config_dir, entries)


@leakcheck
async def test_store_failed_write_of_clear_is_retried_once_by_flush(HM, executor, config, monkeypatch):
    """Ghi lỗi → còn “chưa ghi”, không tự thử lại ngay; `flush` lúc đóng thử lại đúng một lần nên
    `clear` vẫn lên đĩa thành file rỗng hợp lệ."""
    path = write_file(config, [3, 2, 1])
    store = await loaded_store(HM, executor, config)
    monkeypatch.setattr(HM, "HISTORY_MIN_INTERVAL", 0.0)  # thử lại tự hẹn (nếu có) chạy ngay
    spy = FailingSave(HM.save, fail_first=1)
    monkeypatch.setattr(HM, "save", spy)
    store.clear()
    await wait_until(lambda: spy.calls == 1, 5, "chưa có lần ghi nào")
    await asyncio.sleep(0.3)
    assert spy.calls == 1, "ghi lỗi không được tự hẹn thử lại ngay"
    assert file_ids(config) == [TIDS[3], TIDS[2], TIDS[1]]
    await store.flush(5)
    assert spy.calls == 2
    assert json.loads(path.read_bytes()) == EMPTY


@leakcheck
async def test_store_save_failing_forever_does_not_loop_and_flush_is_bounded(
    HM, executor, config, monkeypatch, caplog
):
    """`save` lỗi mãi: không lặp ghi; `flush` thử đúng một lần, trả về trong hạn, chỉ WARNING."""
    caplog.set_level(logging.WARNING)
    store = await loaded_store(HM, executor, config)
    monkeypatch.setattr(HM, "HISTORY_MIN_INTERVAL", 0.0)  # thử lại tự hẹn (nếu có) chạy ngay
    spy = FailingSave(HM.save, fail_first=10**9)
    monkeypatch.setattr(HM, "save", spy)
    store.record(make_entry(1))
    await wait_until(lambda: spy.calls == 1, 5, "chưa có lần ghi nào")
    await asyncio.sleep(0.3)
    assert spy.calls == 1
    limit = 1.0
    t0 = time.monotonic()
    await store.flush(limit)
    assert time.monotonic() - t0 <= limit + 0.5
    assert spy.calls == 2
    await asyncio.sleep(0.3)
    assert spy.calls == 2, "sau flush không được ghi thêm"
    assert not (config / "history.json").exists()
    assert warnings_of(caplog)
    assert errors_logged(caplog) == []


@leakcheck
async def test_store_stop_cancels_pending_write_and_later_records(HM, executor, config, save_spy):
    """T17: `stop()` ngay sau `record` (cùng khối đồng bộ) → không ghi; `record` sau `stop` → không ghi;
    không còn task (`no_leaks`)."""
    store = await loaded_store(HM, executor, config)
    store.record(make_entry(1))
    store.stop()
    store.record(make_entry(2))
    await asyncio.sleep(0.4)
    assert save_spy.calls == []
    assert not (config / "history.json").exists()


@leakcheck
async def test_store_stop_during_running_write_leaves_no_task(HM, executor, config, save_spy, monkeypatch):
    monkeypatch.setattr(HM, "HISTORY_MIN_INTERVAL", 0.0)
    save_spy.gate = threading.Event()
    store = await loaded_store(HM, executor, config)
    store.record(make_entry(1))
    await wait_until(save_spy.hit.is_set, 5, "chưa có lần ghi nào")
    store.record(make_entry(2))
    store.stop()
    save_spy.gate.set()
    await asyncio.sleep(0.4)
    assert len(save_spy.calls) == 1, "sau stop() không được ghi lần nữa"


@leakcheck
async def test_store_load_timeout_is_empty_and_not_writable(
    HM, executor, config, load_gate, save_spy, caplog
):
    """T18: việc nạp quá hạn → rỗng, WARNING, writable=False (không ghi trong cả phiên)."""
    caplog.set_level(logging.WARNING)
    path = write_file(config, [1])
    before = path.read_bytes()
    store = HM.HistoryStore(executor=executor, config_dir=config)
    limit = 0.3
    t0 = time.monotonic()
    await store.load(limit)
    elapsed = time.monotonic() - t0
    assert elapsed <= limit + 0.5, f"load mất {elapsed:.2f} s dù hạn {limit} s"
    assert store.entries() == ()
    assert warnings_of(caplog)
    store.record(make_entry(2))
    await store.flush(1)
    load_gate.gate.set()
    await asyncio.sleep(0.4)
    assert save_spy.calls == []
    assert path.read_bytes() == before


@leakcheck
async def test_store_load_deadline_covers_waiting_to_start(HM, monkeypatch, config, save_spy):
    """Spec 11.9: hạn nạp bao cả lúc chờ bắt đầu; quá hạn thì việc nạp bị bỏ (không bao giờ chạy)."""
    ex = DiskExecutor(1)
    busy = threading.Event()
    started = threading.Event()
    calls: list[Path] = []
    real_load = HM.load

    def counting_load(config_dir):
        calls.append(config_dir)
        return real_load(config_dir)

    monkeypatch.setattr(HM, "load", counting_load)

    def hog():
        started.set()
        busy.wait(30)

    try:
        ex.submit(hog)
        await wait_until(started.is_set, 5, "thread đĩa chưa bận")
        store = HM.HistoryStore(executor=ex, config_dir=config)
        t0 = time.monotonic()
        await store.load(0.3)
        assert time.monotonic() - t0 <= 0.8
        assert store.entries() == ()
    finally:
        busy.set()
    await ex.wait_idle(5)
    await asyncio.sleep(0.1)
    assert calls == [], "việc nạp quá hạn khi chưa bắt đầu phải bị bỏ"
    ex.shutdown()


@leakcheck
async def test_store_record_after_executor_shutdown_warns_and_does_not_raise(HM, config, caplog):
    """Spec 3.2: `submit` ném RuntimeError (executor đã shutdown) → WARNING “node đã đóng”, không ném."""
    caplog.set_level(logging.WARNING)
    ex = DiskExecutor()
    store = HM.HistoryStore(executor=ex, config_dir=config)
    await store.load(5)
    ex.shutdown()
    store.record(make_entry(1))
    await asyncio.sleep(0.2)
    await store.flush(1)
    assert any("node đã đóng" in r.getMessage() for r in warnings_of(caplog)), [
        r.getMessage() for r in warnings_of(caplog)
    ]
    assert errors_logged(caplog) == [], [r.getMessage() for r in errors_logged(caplog)]
    assert [e.transfer_id for e in store.entries()] == [TIDS[1]]


# =================================================================================================
# C3: đọc và xóa theo máy (spec-dot-c mục 3.2, 3.5, 11 sửa 5)
# =================================================================================================

OTHER = "e" * 32


def entry_json_of(k: int, device_id: str) -> dict:
    d = entry_json(k)
    d["device_id"] = device_id
    return d


def file_keys(config: Path) -> list[tuple[str, str]]:
    obj = json.loads((config / "history.json").read_bytes())
    return [(e["device_id"], e["transfer_id"]) for e in obj["entries"]]


@leakcheck
async def test_store_entries_filter_by_device(HM, executor, config):
    obj = {"version": 1, "entries": [entry_json_of(3, OTHER), entry_json(2), entry_json_of(1, OTHER)]}
    (config / "history.json").write_text(json.dumps(obj), encoding="utf-8")
    store = await loaded_store(HM, executor, config)
    assert [e.transfer_id for e in store.entries(OTHER)] == [TIDS[3], TIDS[1]]
    assert [e.transfer_id for e in store.entries(DEV)] == [TIDS[2]]
    assert store.entries("f" * 32) == ()
    assert len(store.entries()) == 3
    await store.flush(5)


@leakcheck
async def test_clear_device_writes_remaining_entries(HM, executor, config, caplog):
    """`clear(id)` bỏ mục của máy đó trong bộ nhớ và trong file; mục của máy khác giữ nguyên thứ tự.
    Log chỉ có 8 ký tự đầu của mã máy (mvp N9)."""
    caplog.set_level(logging.DEBUG, logger="teleport")
    obj = {"version": 1, "entries": [entry_json_of(3, OTHER), entry_json(2), entry_json_of(1, OTHER)]}
    (config / "history.json").write_text(json.dumps(obj), encoding="utf-8")
    store = await loaded_store(HM, executor, config)
    store.clear(OTHER)
    assert [(e.device_id, e.transfer_id) for e in store.entries()] == [(DEV, TIDS[2])]
    store.record(make_entry(4, device_id=OTHER))
    await store.flush(5)
    assert file_keys(config) == [(OTHER, TIDS[4]), (DEV, TIDS[2])], "mục sau lần xóa vẫn giữ"
    assert all(OTHER not in r.getMessage() for r in caplog.records)


@leakcheck
async def test_clear_device_unreadable_file_not_overwritten(
    HM, executor, config, monkeypatch, caplog, save_spy
):
    """File không đọc được lúc nạp: `clear(id)` **không** cấp quyền ghi (ghi đè sẽ mất lịch sử của các
    máy khác) → chỉ xóa trong bộ nhớ, WARNING, file giữ nguyên."""
    caplog.set_level(logging.WARNING)
    path = write_file(config, [7])
    before = path.read_bytes()

    def denied(p):
        raise PermissionError(13, "Permission denied", str(p))

    monkeypatch.setattr(HM, "_read_limited", denied)
    store = await loaded_store(HM, executor, config)
    store.record(make_entry(1))
    store.record(make_entry(2, device_id=OTHER))
    caplog.clear()
    store.clear(DEV)
    assert [(e.device_id, e.transfer_id) for e in store.entries()] == [(OTHER, TIDS[2])]
    assert warnings_of(caplog), "phải có WARNING: chỉ xóa trong bộ nhớ"
    await asyncio.sleep(0.2)
    await store.flush(5)
    assert save_spy.calls == []
    assert path.read_bytes() == before


@leakcheck
async def test_clear_all_still_overwrites_unreadable(HM, executor, config, monkeypatch):
    """Sau `clear(id)` (không ghi), `clear()` toàn bộ vẫn ghi file rỗng như trước (ý người dùng)."""
    path = write_file(config, [7])

    def denied(p):
        raise PermissionError(13, "Permission denied", str(p))

    monkeypatch.setattr(HM, "_read_limited", denied)
    store = await loaded_store(HM, executor, config)
    store.record(make_entry(1, device_id=OTHER))
    store.clear(OTHER)
    store.clear()
    assert store.entries() == ()
    await store.flush(5)
    assert json.loads(path.read_bytes()) == EMPTY


@leakcheck
async def test_clear_device_after_flush_only_clears_memory(HM, executor, config, caplog):
    caplog.set_level(logging.WARNING)
    write_file(config, [1])
    store = await loaded_store(HM, executor, config)
    await store.flush(5)
    caplog.clear()
    store.clear(DEV)
    assert store.entries() == ()
    assert any("node đã đóng" in r.getMessage() for r in warnings_of(caplog))
    await asyncio.sleep(0.2)
    assert file_ids(config) == [TIDS[1]]


@leakcheck
async def test_clear_device_rejects_bad_device_id(HM, executor, config):
    from teleport.core.history import HistoryError

    write_file(config, [1])
    store = await loaded_store(HM, executor, config)
    for bad in ("D" * 32, "d" * 31, 5):
        with pytest.raises(HistoryError):
            store.clear(bad)
    assert [e.transfer_id for e in store.entries()] == [TIDS[1]]
    await store.flush(5)


@leakcheck
async def test_record_while_loading_merges_with_per_device_cap(HM, executor, config, load_gate):
    """L4: file có đủ `MAX_HISTORY_PER_DEVICE` mục của DEV và vài mục của OTHER; hai mục của DEV ghi
    trong lúc nạp thì lên đầu, hai mục cũ nhất của DEV bị bỏ, mục của OTHER giữ đủ và đúng thứ tự."""
    from teleport.core.history import MAX_HISTORY_PER_DEVICE

    assert MAX_HISTORY_PER_DEVICE == 100
    dev_ks = list(range(100, 0, -1))  # k lớn = mới hơn
    raw = [
        entry_json_of(103, OTHER),
        *[entry_json(k) for k in dev_ks[:50]],
        entry_json_of(102, OTHER),
        *[entry_json(k) for k in dev_ks[50:]],
        entry_json_of(101, OTHER),
    ]
    (config / "history.json").write_text(json.dumps({"version": 1, "entries": raw}), encoding="utf-8")
    store = HM.HistoryStore(executor=executor, config_dir=config)
    loading = asyncio.create_task(store.load(10))
    await wait_until(load_gate.hit.is_set, 5, "việc nạp chưa bắt đầu")
    store.record(make_entry(201))
    store.record(make_entry(202))
    load_gate.gate.set()
    await asyncio.wait_for(loading, 5)

    dropped = {(DEV, TIDS[2]), (DEV, TIDS[1])}
    expected = [(DEV, TIDS[202]), (DEV, TIDS[201])] + [
        (d["device_id"], d["transfer_id"]) for d in raw if (d["device_id"], d["transfer_id"]) not in dropped
    ]
    got = [(e.device_id, e.transfer_id) for e in store.entries()]
    assert got == expected
    assert [e.transfer_id for e in store.entries(DEV)] == [
        TIDS[202],
        TIDS[201],
        *[TIDS[k] for k in dev_ks[:-2]],
    ]
    assert [e.transfer_id for e in store.entries(OTHER)] == [TIDS[103], TIDS[102], TIDS[101]]
    await store.flush(5)
    assert file_keys(config) == expected
