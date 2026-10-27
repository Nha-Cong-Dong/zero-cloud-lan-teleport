"""Test `scan_temps` và `cleanup` của `teleport.app.storage` (P3-3a, việc V5).

Viết từ đặc tả, không từ code: `specs/spec-p3-3a.md` mục 2.2 (`scan_temps`, `TempScan`, `TempKind`,
“hoạt động cuối”, `cleanup`, `TEMP_RETENTION`), mục 4 (V5 “Xong khi”), mục 8.3 (`_busy`, B3 mtime
`.part` tương lai, lọc trước khi mở file), 8.4 (B2: “của máy này” theo `receiver`); `specs/spec-p3.md`
mục 2.2 (bố cục, regex, `transfer.json`, giờ tường), H5, M1, Q3; architecture mục 4.

Ngưỡng dọn (từ “hoạt động cuối”): lượt đang chạy của máy này → `core.transfer.RECONNECT_GRACE`; lượt đã
xong của máy này → `core.transfer.REMEMBER_TTL`; còn lại (node khác, mồ côi, hỏng) → `retention`. Mọi ca
cách ngưỡng ±60 s để không phụ thuộc so sánh `>` hay `≥` ở đúng biên.

Không mở socket.
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path

import pytest

from teleport.core import resume_state as rs
from teleport.core import transfer as core_transfer

WINDOWS = sys.platform == "win32"
DAY = 24 * 3600.0
RETENTION = 7 * DAY
OTHER = "c" * 32  # máy nhận khác dùng chung thư mục nhận


def tid(k: int) -> str:
    return f"{k:032x}"


@pytest.fixture(scope="module")
def storage():
    return importlib.import_module("teleport.app.storage")


@pytest.fixture
def h(resume_helpers):
    return resume_helpers


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "recv"
    r.mkdir()
    return r


@pytest.fixture
def now():
    return float(int(time.time()))


def make_symlink(target, link, *, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"không tạo được symlink: {e}")


def link_dir(kind: str, target, link) -> None:
    if kind == "junction":
        if not WINDOWS:
            pytest.skip("junction chỉ có trên Windows")
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        make_symlink(target, link, is_dir=True)


def set_age(path: Path, now: float, age: float) -> None:
    t = now - age
    os.utime(path, (t, t))


def build(
    root: Path, transfer_id: str, now: float, *, age: float, record=None, raw=None, files=("0.part",)
) -> Path:
    """`.teleport/<transfer_id>/` với `transfer.json` (`record` hay `raw`) và các file `files`; mọi file
    và chính thư mục có mtime = `now − age`."""
    d = root / ".teleport" / transfer_id
    d.mkdir(parents=True)
    if record is not None:
        (d / "transfer.json").write_bytes(rs.encode_transfer_record(record))
    if raw is not None:
        (d / "transfer.json").write_bytes(raw)
    for name in files:
        (d / name).write_bytes(b"du lieu")
    for p in d.iterdir():
        set_age(p, now, age)
    set_age(d, now, age)
    return d


def running(record_for, h, transfer_id, **kw):
    return record_for(transfer_id, [100, 100], receiver=kw.pop("receiver", h.RECEIVER), **kw)


def done(record_for, h, transfer_id, **kw):
    return record_for(transfer_id, [100], verified=1, receiver=kw.pop("receiver", h.RECEIVER), **kw)


def do_cleanup(storage, root, now, h, *, skip=frozenset(), retention=RETENTION):
    with storage.ReceiveDir.open(root) as rd:
        return rd.cleanup(now, own_device_id=h.RECEIVER, skip=frozenset(skip), retention=retention)


# ---------------------------------------------------------------------------------------------
# `scan_temps`: các loại
# ---------------------------------------------------------------------------------------------


def test_scan_without_teleport_dir_is_empty(storage, root, h):
    with storage.ReceiveDir.open(root) as rd:
        assert rd.scan_temps(h.RECEIVER, {h.SENDER}) == []
    assert not (root / ".teleport").exists()


def test_scan_classifies_every_kind_without_changing_disk(storage, root, record_for, h, now, tmp_path):
    rec_running = running(record_for, h, tid(1))
    rec_done = done(record_for, h, tid(2))
    rec_other = running(record_for, h, tid(3), receiver=OTHER)
    rec_untrusted = running(record_for, h, tid(5), sender="b" * 32)
    build(root, tid(1), now, age=60, record=rec_running)
    build(root, tid(2), now, age=60, record=rec_done, files=())
    build(root, tid(3), now, age=60, record=rec_other)
    build(root, tid(4), now, age=60)  # không có transfer.json
    build(root, tid(5), now, age=60, record=rec_untrusted)
    build(root, tid(6), now, age=60, raw=b"{khong phai json")
    build(root, tid(7), now, age=60, raw=b" " * (rs.MAX_TRANSFER_RECORD + 1))
    build(root, tid(8), now, age=60, raw=rs.encode_transfer_record(running(record_for, h, tid(9))))  # id khác
    d10 = build(root, tid(10), now, age=60, files=())
    (d10 / "transfer.json").mkdir()
    # Không phải lượt: tên sai, file thường mang tên hợp lệ.
    (root / ".teleport" / "khong-phai-id").mkdir()
    (root / ".teleport" / tid(11).upper()).mkdir()
    (root / ".teleport" / tid(12)).write_bytes(b"file thuong")
    before = h.snapshot(tmp_path)
    with storage.ReceiveDir.open(root) as rd:
        scans = rd.scan_temps(h.RECEIVER, {h.SENDER})
    assert h.snapshot(tmp_path) == before
    kinds = {s.transfer_id: s.kind for s in scans}
    K = storage.TempKind
    assert kinds == {
        tid(1): K.RUNNING,
        tid(2): K.DONE,
        tid(3): K.OTHER_NODE,
        tid(4): K.ORPHAN,
        tid(5): K.ORPHAN,
        tid(6): K.BROKEN,
        tid(7): K.BROKEN,
        tid(8): K.BROKEN,
        tid(10): K.BROKEN,
    }
    by_id = {s.transfer_id: s for s in scans}
    assert by_id[tid(1)].record == rec_running
    assert by_id[tid(2)].record == rec_done
    assert by_id[tid(3)].record == rec_other
    assert by_id[tid(4)].record is None
    for k in (6, 7, 8, 10):
        assert by_id[tid(k)].record is None


def test_scan_treats_symlinked_transfer_json_as_broken(storage, root, record_for, h, now, tmp_path):
    outside = tmp_path / "ngoai.json"
    outside.write_bytes(rs.encode_transfer_record(running(record_for, h, tid(1))))
    d = build(root, tid(1), now, age=60)
    make_symlink(outside, d / "transfer.json")
    with storage.ReceiveDir.open(root) as rd:
        [scan] = rd.scan_temps(h.RECEIVER, {h.SENDER})
    assert scan.kind == storage.TempKind.BROKEN and scan.record is None


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_scan_ignores_linked_transfer_dirs(storage, root, record_for, h, now, tmp_path, kind):
    victim = tmp_path / "nan-nhan"
    victim.mkdir()
    (victim / "transfer.json").write_bytes(rs.encode_transfer_record(running(record_for, h, tid(1))))
    (root / ".teleport").mkdir()
    link_dir(kind, victim, root / ".teleport" / tid(1))
    with storage.ReceiveDir.open(root) as rd:
        assert rd.scan_temps(h.RECEIVER, {h.SENDER}) == []


def test_scan_last_activity_for_running_uses_only_current_files(storage, root, record_for, h, now):
    rec = running(record_for, h, tid(1), verified=1)
    d = build(root, tid(1), now, age=5000, record=rec, files=("1.part", "1.state", "0.state", "0.part"))
    set_age(d / "1.state", now, 3000)
    set_age(d / "0.state", now, 10)  # file đã verified: không tính
    set_age(d / "0.part", now, 10)
    with storage.ReceiveDir.open(root) as rd:
        [scan] = rd.scan_temps(h.RECEIVER, {h.SENDER})
    assert scan.kind == storage.TempKind.RUNNING
    assert scan.last_activity == pytest.approx(now - 3000, abs=2.5)


def test_scan_last_activity_of_orphan_is_newest_layout_file(storage, root, h, now):
    d = build(root, tid(1), now, age=5000, files=("0.part", "3.state", "ghi-chu.txt"))
    set_age(d / "3.state", now, 1000)
    set_age(d / "ghi-chu.txt", now, 1)  # không khớp regex: không tính
    with storage.ReceiveDir.open(root) as rd:
        [scan] = rd.scan_temps(h.RECEIVER, {h.SENDER})
    assert scan.kind == storage.TempKind.ORPHAN
    assert scan.last_activity == pytest.approx(now - 1000, abs=2.5)


def test_scan_last_activity_of_empty_dir_is_dir_mtime(storage, root, h, now):
    build(root, tid(1), now, age=4321, files=())
    with storage.ReceiveDir.open(root) as rd:
        [scan] = rd.scan_temps(h.RECEIVER, {h.SENDER})
    assert scan.last_activity == pytest.approx(now - 4321, abs=2.5)


# ---------------------------------------------------------------------------------------------
# `cleanup`: ba ngưỡng
# ---------------------------------------------------------------------------------------------


def test_cleanup_running_of_this_node_uses_reconnect_grace(storage, root, record_for, h, now):
    grace = core_transfer.RECONNECT_GRACE
    young = build(root, tid(1), now, age=grace - 60, record=running(record_for, h, tid(1)))
    old = build(root, tid(2), now, age=grace + 60, record=running(record_for, h, tid(2)))
    assert do_cleanup(storage, root, now, h) == 1
    assert young.is_dir() and not old.exists()
    assert (root / ".teleport").is_dir()


def test_cleanup_running_threshold_is_read_at_call_time(storage, root, record_for, h, now, monkeypatch):
    monkeypatch.setattr(core_transfer, "RECONNECT_GRACE", 100.0)
    d = build(root, tid(1), now, age=200, record=running(record_for, h, tid(1)))
    assert do_cleanup(storage, root, now, h) == 1
    assert not d.exists()


def test_cleanup_running_with_untrusted_sender_is_still_this_nodes(storage, root, record_for, h, now):
    # B2: “của máy này” xếp theo `receiver`, không theo `sender` còn tin cậy.
    grace = core_transfer.RECONNECT_GRACE
    d = build(root, tid(1), now, age=grace + 60, record=running(record_for, h, tid(1), sender="b" * 32))
    assert do_cleanup(storage, root, now, h) == 1
    assert not d.exists()


def test_cleanup_done_of_this_node_uses_remember_ttl(storage, root, record_for, h, now):
    ttl = core_transfer.REMEMBER_TTL
    assert ttl > core_transfer.RECONNECT_GRACE + 120
    young = build(
        root,
        tid(1),
        now,
        age=core_transfer.RECONNECT_GRACE + 60,
        record=done(record_for, h, tid(1)),
        files=(),
    )
    old = build(root, tid(2), now, age=ttl + 60, record=done(record_for, h, tid(2)), files=())
    assert do_cleanup(storage, root, now, h) == 1
    assert young.is_dir() and not old.exists()


def test_cleanup_done_threshold_is_read_at_call_time(storage, root, record_for, h, now, monkeypatch):
    monkeypatch.setattr(core_transfer, "REMEMBER_TTL", 100.0)
    d = build(root, tid(1), now, age=200, record=done(record_for, h, tid(1)), files=())
    assert do_cleanup(storage, root, now, h) == 1
    assert not d.exists()


@pytest.mark.parametrize("kind", ["node-khac", "mo-coi", "hong", "rong"])
def test_cleanup_others_use_retention(storage, root, record_for, h, now, kind):
    ttl = core_transfer.REMEMBER_TTL

    def make(k: int, age: float) -> Path:
        t = tid(k)
        if kind == "node-khac":
            return build(root, t, now, age=age, record=running(record_for, h, t, receiver=OTHER))
        if kind == "mo-coi":
            return build(root, t, now, age=age, files=("0.part", "0.state"))
        if kind == "hong":
            return build(root, t, now, age=age, raw=b"\x00\xff khong phai json")
        return build(root, t, now, age=age, files=())

    young = make(1, ttl + 60)  # quá hạn của lượt của máy này, nhưng chưa tới `retention`
    old = make(2, RETENTION + 60)
    assert do_cleanup(storage, root, now, h, retention=RETENTION) == 1
    assert young.is_dir() and not old.exists()


def test_cleanup_returns_number_of_removed_dirs(storage, root, record_for, h, now):
    for k in range(1, 6):
        build(root, tid(k), now, age=RETENTION + 60)
    keep = build(root, tid(9), now, age=60)
    assert do_cleanup(storage, root, now, h) == 5
    assert sorted(os.listdir(root / ".teleport")) == [keep.name]
    assert do_cleanup(storage, root, now, h) == 0


def test_cleanup_without_teleport_dir_returns_zero_and_creates_nothing(storage, root, h, now):
    assert do_cleanup(storage, root, now, h) == 0
    assert not (root / ".teleport").exists()


def test_cleanup_never_removes_teleport_dir(storage, root, h, now):
    build(root, tid(1), now, age=RETENTION + 60)
    assert do_cleanup(storage, root, now, h) == 1
    assert (root / ".teleport").is_dir() and os.listdir(root / ".teleport") == []


# ---------------------------------------------------------------------------------------------
# `cleanup`: bỏ qua `skip`, temp đang mở; mtime tương lai
# ---------------------------------------------------------------------------------------------


def test_cleanup_respects_skip(storage, root, h, now, tmp_path):
    kept = build(root, tid(1), now, age=RETENTION + 60)
    gone = build(root, tid(2), now, age=RETENTION + 60)
    before = h.snapshot(kept)
    assert do_cleanup(storage, root, now, h, skip={tid(1)}) == 1
    assert h.snapshot(kept) == before and not gone.exists()


def test_cleanup_skips_temp_open_in_this_receive_dir(storage, root, record_for, h, now):
    with storage.ReceiveDir.open(root) as rd:
        temp = rd.create_temp(tid(1), record_for(tid(1), [5]))
        part = temp.create_part(0, 5)
        part.write_at(0, b"12345")
        far = now + 10 * RETENTION
        assert rd.cleanup(far, own_device_id=h.RECEIVER, skip=frozenset(), retention=1.0) == 0
        assert sorted(os.listdir(temp.path)) == ["0.part", "0.state", "transfer.json"]
        rd.close_temp(temp)
        # Đã đóng (tạm dừng) thì không còn được che: dọn theo ngưỡng như mọi thư mục khác.
        assert rd.cleanup(far, own_device_id=h.RECEIVER, skip=frozenset(), retention=1.0) == 1
        assert not temp.path.exists()


@pytest.mark.parametrize("name", ["transfer.json", "0.state"])
def test_cleanup_keeps_dir_with_future_record_or_state_mtime(storage, root, record_for, h, now, name):
    d = build(
        root,
        tid(1),
        now,
        age=RETENTION + 60,
        record=running(record_for, h, tid(1)),
        files=("0.part", "0.state"),
    )
    set_age(d / name, now, -3600)  # mtime 1 giờ sau `wall_now`
    before = h.snapshot(d)
    assert do_cleanup(storage, root, now, h) == 0
    assert h.snapshot(d) == before


def test_cleanup_ignores_future_part_mtime(storage, root, record_for, h, now):
    # B3: mtime của `.part` lớn hơn `wall_now` bị bỏ qua khi tính hoạt động cuối.
    d = build(
        root,
        tid(1),
        now,
        age=RETENTION + 60,
        record=running(record_for, h, tid(1)),
        files=("0.part", "0.state"),
    )
    set_age(d / "0.part", now, -3600)
    assert do_cleanup(storage, root, now, h) == 1
    assert not d.exists()


def test_cleanup_keeps_empty_dir_with_future_mtime(storage, root, h, now):
    d = build(root, tid(1), now, age=-3600, files=())
    assert do_cleanup(storage, root, now, h) == 0
    assert d.is_dir()


# ---------------------------------------------------------------------------------------------
# `cleanup`: chỉ file thường khớp regex, không đệ quy, không theo liên kết
# ---------------------------------------------------------------------------------------------


def test_cleanup_deletes_only_layout_files_and_keeps_foreign_ones(storage, root, h, now):
    names = [
        "transfer.json",
        "0.part",
        "0.state",
        "999.part",
        "transfer.json.0123456789abcdef.tmp",
        "12.state.fedcba9876543210.tmp",
    ]
    foreign = ["ghi-chu.txt", "1000.part", "01.state", "0.part.0123456789abcdef.tmp", "TRANSFER.JSON.bak"]
    d = build(root, tid(1), now, age=RETENTION + 60, raw=b"hong", files=names[1:] + foreign)
    assert do_cleanup(storage, root, now, h) == 0  # còn file lạ: thư mục chưa xóa hết
    assert sorted(os.listdir(d)) == sorted(foreign)
    for name in foreign:
        assert (d / name).read_bytes() == b"du lieu"


def test_cleanup_keeps_subdirectories_and_their_content(storage, root, h, now):
    d = build(root, tid(1), now, age=RETENTION + 60)
    sub = d / "con"
    sub.mkdir()
    (sub / "0.part").write_bytes(b"trong thu muc con")
    set_age(sub, now, RETENTION + 60)
    set_age(d, now, RETENTION + 60)
    assert do_cleanup(storage, root, now, h) == 0
    assert (sub / "0.part").read_bytes() == b"trong thu muc con"
    assert not (d / "0.part").exists()


def test_cleanup_does_not_follow_file_symlink_inside_transfer_dir(storage, root, h, now, tmp_path):
    victim = tmp_path / "nan-nhan.bin"
    victim.write_bytes(b"cua nan nhan")
    d = build(root, tid(1), now, age=RETENTION + 60, files=())
    make_symlink(victim, d / "0.part")
    set_age(d, now, RETENTION + 60)
    assert do_cleanup(storage, root, now, h) == 0
    assert victim.read_bytes() == b"cua nan nhan"
    assert os.path.islink(d / "0.part")


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_cleanup_does_not_follow_linked_transfer_dir(storage, root, h, now, tmp_path, kind):
    victim = tmp_path / "nan-nhan"
    victim.mkdir()
    for name in ("transfer.json", "0.part", "0.state"):
        (victim / name).write_bytes(b"cua nan nhan")
        set_age(victim / name, now, RETENTION + 60)
    (root / ".teleport").mkdir()
    link = root / ".teleport" / tid(1)
    link_dir(kind, victim, link)
    before = h.snapshot(tmp_path)
    assert do_cleanup(storage, root, now, h) == 0
    assert h.snapshot(tmp_path) == before


def test_cleanup_ignores_entries_with_invalid_names(storage, root, h, now):
    odd = root / ".teleport" / "khong-phai-id"
    odd.mkdir(parents=True)
    (odd / "0.part").write_bytes(b"x")
    set_age(odd / "0.part", now, RETENTION + 60)
    upper = root / ".teleport" / tid(0xABCDEF).upper()  # có chữ hoa: không phải transfer_id hợp lệ
    upper.mkdir()
    set_age(upper, now, RETENTION + 60)
    stray = root / ".teleport" / tid(2)
    stray.write_bytes(b"file thuong mang ten hop le")
    set_age(stray, now, RETENTION + 60)
    assert do_cleanup(storage, root, now, h) == 0
    assert (
        (odd / "0.part").exists() and upper.is_dir() and stray.read_bytes() == b"file thuong mang ten hop le"
    )


def test_temp_retention_constant_is_seven_days(storage):
    assert storage.TEMP_RETENTION == 7 * 24 * 3600.0


# ---------------------------------------------------------------------------------------------
# `discard_temp` (P3-3b, spec-p3-3b mục 2.1, Q6): dọn một lượt RUNNING thừa, bỏ ngưỡng thời gian
# ---------------------------------------------------------------------------------------------


def do_discard(storage, root, transfer_id, h, *, own=None):
    with storage.ReceiveDir.open(root) as rd:
        return rd.discard_temp(transfer_id, own_device_id=h.RECEIVER if own is None else own)


def test_discard_temp_removes_fresh_running_dir_of_this_node(storage, root, record_for, h, now):
    """Lượt RUNNING của máy này vừa hoạt động (còn xa ngưỡng nối lại) vẫn bị xóa: bỏ ngưỡng thời gian."""
    d = build(
        root, tid(1), now, age=0, record=running(record_for, h, tid(1)), files=("0.part", "0.state", "1.part")
    )
    assert do_discard(storage, root, tid(1), h) is True
    assert not d.exists()
    assert (root / ".teleport").is_dir()


@pytest.mark.parametrize("kind", ["other_node", "done", "broken", "orphan"])
def test_discard_temp_refuses_anything_but_running_of_this_node(storage, root, record_for, h, now, kind):
    record = {
        "other_node": running(record_for, h, tid(1), receiver=OTHER),
        "done": done(record_for, h, tid(1)),
        "broken": None,
        "orphan": None,
    }[kind]
    raw = b"{hong" if kind == "broken" else None
    d = build(root, tid(1), now, age=10 * RETENTION, record=record, raw=raw, files=("0.part", "0.state"))
    before = h.snapshot(d)
    assert do_discard(storage, root, tid(1), h) is False
    assert h.snapshot(d) == before


def test_discard_temp_of_missing_dir_is_false(storage, root, h):
    (root / ".teleport").mkdir()
    assert do_discard(storage, root, tid(9), h) is False


def test_discard_temp_refuses_temp_open_in_same_receive_dir(storage, root, record_for, h):
    with storage.ReceiveDir.open(root) as rd:
        temp = rd.create_temp(tid(1), record_for(tid(1), [5], receiver=h.RECEIVER))
        part = temp.create_part(0, 5)
        part.write_at(0, b"12345")
        before = h.snapshot(temp.path)
        assert rd.discard_temp(tid(1), own_device_id=h.RECEIVER) is False
        assert h.snapshot(temp.path) == before
        rd.close_temp(temp)


def test_discard_temp_deletes_only_layout_names_and_keeps_foreign(storage, root, record_for, h, now):
    names = ["0.part", "0.state", "transfer.json.0123456789abcdef.tmp"]
    foreign = ["ghi-chu.txt", "1000.part"]
    d = build(root, tid(1), now, age=0, record=running(record_for, h, tid(1)), files=names + foreign)
    do_discard(storage, root, tid(1), h)
    assert sorted(os.listdir(d)) == sorted(foreign)
    for name in foreign:
        assert (d / name).read_bytes() == b"du lieu"


def test_discard_temp_does_not_follow_part_symlink(storage, root, record_for, h, now, tmp_path):
    victim = tmp_path / "nan-nhan.bin"
    victim.write_bytes(b"cua nan nhan")
    d = build(root, tid(1), now, age=0, record=running(record_for, h, tid(1)), files=("0.state",))
    make_symlink(victim, d / "0.part")
    do_discard(storage, root, tid(1), h)
    assert victim.read_bytes() == b"cua nan nhan"
