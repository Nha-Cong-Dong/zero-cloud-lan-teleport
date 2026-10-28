"""Test đường đọc lại sau khi sập của `teleport.app.storage` (P3-3a, việc V4).

Viết từ đặc tả, không từ code: `specs/spec-p3-3a.md` mục 2.2 (`open_for_resume` pha 1 chỉ đọc, pha 2
sửa đĩa, bảng theo `recover_target`; `reread` file đích; `drop_target`; `finalize` theo `mode`), mục 4
(V4 “Xong khi”), mục 8.3 (header hỏng/mất → `rewrite_header`; `name_records` khởi tạo từ đọc lại);
`specs/spec-p3.md` mục 2.2 (trần `.state`), H4, H5; architecture mục 4 (“Kết thúc một file”, “Phục hồi
tên đích”, đọc lại `.state`).

Kỹ thuật “sập” (spec-p3-3a mục 4): trong seam của bước cần xét, chép cả cây thư mục nhận
(`shutil.copytree`, giữ mtime) rồi để lượt gốc chạy tiếp; bản chép là đĩa sau khi sập ở đúng điểm đó và
được mở bằng một `ReceiveDir` mới. Không giả sập bằng exception (nhánh dọn `_discard_reserve` sẽ chạy).

Mỗi kịch bản có hai file canh gác của người khác trong thư mục nhận (`a.txt` trùng tên đích, nên tên
đích thật là `a (1).txt`, và `canh.txt`): chúng không bao giờ được đổi byte.

Không mở socket.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import random
import threading
from pathlib import Path

import pytest

from teleport.core import resume_state as rs
from teleport.core.blocks import BLOCK_SIZE

BS = BLOCK_SIZE
ID = "0123456789abcdef0123456789abcdef"
SIZE = 2 * BS + 7  # 3 khối, khối cuối ngắn
NAME = "a.txt"
TARGET = "a (1).txt"  # `a.txt` là file canh gác có sẵn
SENTINELS = {"a.txt": b"file cua nguoi dung " * 50, "canh.txt": b"canh gac " * 30}


@pytest.fixture(scope="module")
def storage():
    return importlib.import_module("teleport.app.storage")


@pytest.fixture
def h(resume_helpers):
    return resume_helpers


def payload(size: int, seed: int = 1) -> bytes:
    return random.Random(seed).randbytes(size)


def bit_set(bits, n: int) -> set[int]:
    return {i for i in range(n) if i in bits}


def leaves_of(leaves, n: int) -> list[bytes]:
    return [bytes(leaves[32 * i : 32 * i + 32]) for i in range(n)]


def replay_file(path: Path, n: int):
    r = rs.StateReplay(n, max_length=rs.max_state_length(n))
    r.feed(path.read_bytes())
    return r.finish()


def check_sentinels(root: Path) -> None:
    for name, content in SENTINELS.items():
        assert (root / name).read_bytes() == content, f"file canh gác {name} bị đổi"


def running_scan(storage, rd, h, tid: str = ID):
    found = [s for s in rd.scan_temps(h.RECEIVER, {h.SENDER}) if s.transfer_id == tid]
    assert len(found) == 1, found
    assert found[0].kind == storage.TempKind.RUNNING, found[0]
    return found[0]


def finalize_with_crash(storage, tmp_path, record_for, h, seam, *, sizes=(SIZE,), when=None):
    """Nhận đủ các file của `sizes` rồi `finalize` lần lượt; lần đầu seam `seam` chạy (và `when(*args)`
    đúng) thì chép cây thư mục nhận sang `tmp_path/"sap"`. Trả (thư mục bản chép, dữ liệu từng file)."""
    root = tmp_path / "recv"
    crash = tmp_path / "sap"
    datas = [payload(size, seed=i + 1) for i, size in enumerate(sizes)]
    taken: list[tuple] = []

    def hook(*args):
        if not taken and (when is None or when(*args)):
            taken.append(args)
            h.crash_copy(root, crash)

    with storage.ReceiveDir.open(root) as rd:
        for name, content in SENTINELS.items():
            (rd.root / name).write_bytes(content)
        temp = rd.create_temp(ID, record_for(ID, list(sizes)))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(storage, seam, hook)
            for i, data in enumerate(datas):
                part = temp.create_part(i, len(data))
                part.commit(h.write_blocks(part, data))
                rd.finalize(part, NAME)
        assert taken, f"seam {seam} không chạy"
    return crash, datas


def resume_from(storage, root: Path, h, **kwargs):
    rd = storage.ReceiveDir.open(root)
    try:
        scan = running_scan(storage, rd, h)
        handle = rd.open_for_resume(scan, **kwargs)
    except BaseException:
        rd.close()
        raise
    return rd, scan, handle


# ---------------------------------------------------------------------------------------------
# Sập ở từng bước của `finalize` → đúng dòng bảng phục hồi
# ---------------------------------------------------------------------------------------------


def test_crash_after_target_resumes_as_new_name_and_never_uses_that_name(storage, tmp_path, record_for, h):
    crash, (data,) = finalize_with_crash(storage, tmp_path, record_for, h, "_after_target")
    # Sau khi sập, người khác tạo file 0 byte đúng tên trong TARGET: không có RESERVED → không phải của mình.
    (crash / TARGET).write_bytes(b"")
    rd, scan, handle = resume_from(storage, crash, h)
    with rd:
        assert handle.mode == storage.ResumeMode.NORMAL
        assert handle.part.mode == storage.ResumeMode.NORMAL
        assert handle.file_index == 0 and handle.record == scan.record
        assert handle.part_blocks == 3
        assert bit_set(handle.bits, 3) == {0, 1, 2}
        assert leaves_of(handle.leaves, 3) == h.block_hashes(data)
        assert handle.last_activity == scan.last_activity
        final = rd.finalize(handle.part, NAME)
        assert final not in (crash / NAME, crash / TARGET)
        assert final.read_bytes() == data
        assert (crash / TARGET).read_bytes() == b""
    check_sentinels(crash)


def test_crash_after_reservation_before_reserved_leaves_placeholder_alone(storage, tmp_path, record_for, h):
    crash, (data,) = finalize_with_crash(storage, tmp_path, record_for, h, "_after_reserve")
    assert (crash / TARGET).read_bytes() == b""  # khe đã chấp nhận: file giữ chỗ 0 byte sót lại
    rd, _, handle = resume_from(storage, crash, h)
    with rd:
        assert handle.mode == storage.ResumeMode.NORMAL
        assert bit_set(handle.bits, 3) == {0, 1, 2}
        final = rd.finalize(handle.part, NAME)
        assert final != crash / TARGET
        assert final.read_bytes() == data
    assert (crash / TARGET).read_bytes() == b""
    check_sentinels(crash)


def test_crash_after_reserved_reuses_placeholder(storage, tmp_path, record_for, h):
    crash, (data,) = finalize_with_crash(storage, tmp_path, record_for, h, "_after_reserved")
    rd, _, handle = resume_from(storage, crash, h)
    with rd:
        assert handle.mode == storage.ResumeMode.REUSE_PLACEHOLDER
        assert handle.part.mode == storage.ResumeMode.REUSE_PLACEHOLDER
        assert bit_set(handle.bits, 3) == {0, 1, 2}
        assert handle.part.log.name_records == 2  # khởi tạo từ ReplayResult (mục 8.3)
        final = rd.finalize(handle.part, "ten-khac.bin")  # tên lấy từ TARGET đã lưu
        assert final == crash / TARGET
        assert final.read_bytes() == data
        rec = rs.decode_transfer_record(
            (crash / ".teleport" / ID / "transfer.json").read_bytes(), dir_name=ID
        )
        assert rec.verified == 1
    check_sentinels(crash)


@pytest.mark.parametrize("seam", ["_after_replace", "_before_atomic_replace"])
def test_crash_after_replace_verifies_target_and_finishes_from_step_6(storage, tmp_path, record_for, h, seam):
    when = (lambda path, name, *a: name == "transfer.json") if seam == "_before_atomic_replace" else None
    crash, (data,) = finalize_with_crash(storage, tmp_path, record_for, h, seam, when=when)
    tdir = crash / ".teleport" / ID
    assert not (tdir / "0.part").exists()
    target = crash / TARGET
    before = (target.read_bytes(), os.stat(target).st_mtime_ns)
    rd, _, handle = resume_from(storage, crash, h)
    with rd:
        assert handle.mode == storage.ResumeMode.VERIFY_TARGET
        assert handle.part_blocks == 3
        assert bit_set(handle.bits, 3) == {0, 1, 2}
        assert [n for n in os.listdir(tdir) if n.endswith(".tmp")] == []  # `.tmp` sót bị xóa ở pha 2
        # Không băm lúc khởi động; đọc lại file đích qua `reread`, không cắt, không ghi.
        digests = bytes(handle.part.reread())
        assert digests == b"".join(h.block_hashes(data))
        assert (target.read_bytes(), os.stat(target).st_mtime_ns) == before
        for call in (
            lambda: handle.part.write_at(0, data[:BS]),
            lambda: handle.part.sync(),
            lambda: handle.part.commit([]),
        ):
            with pytest.raises(ValueError):
                call()
        final = rd.finalize(handle.part, "bo-qua.bin")
        assert final == target
        assert final.read_bytes() == data
        assert not (tdir / "0.state").exists()
        rec = rs.decode_transfer_record((tdir / "transfer.json").read_bytes(), dir_name=ID)
        assert rec.verified == 1 and rec.done_at is not None
    check_sentinels(crash)


def test_crash_after_record_of_last_file_is_done_not_resumable(storage, tmp_path, record_for, h):
    crash, _ = finalize_with_crash(storage, tmp_path, record_for, h, "_after_record")
    with storage.ReceiveDir.open(crash) as rd:
        [scan] = [s for s in rd.scan_temps(h.RECEIVER, {h.SENDER}) if s.transfer_id == ID]
        assert scan.kind == storage.TempKind.DONE
        before = h.snapshot(crash)
        with pytest.raises(ValueError):
            rd.open_for_resume(scan)
        assert h.snapshot(crash) == before
    check_sentinels(crash)


def test_crash_after_record_of_first_file_deletes_its_state_and_resumes_next(
    storage, tmp_path, record_for, h
):
    crash, datas = finalize_with_crash(
        storage, tmp_path, record_for, h, "_after_record", sizes=(SIZE, BS + 1)
    )
    tdir = crash / ".teleport" / ID
    assert (tdir / "0.state").exists()  # bước 8 chưa chạy
    rd, scan, handle = resume_from(storage, crash, h)
    with rd:
        assert scan.record.verified == 1
        assert handle.file_index == 1
        assert not (tdir / "0.state").exists(), "`.state` của file đã verified phải bị xóa"
        assert handle.mode == storage.ResumeMode.NORMAL
        assert bit_set(handle.bits, 2) == set()
        assert (tdir / "1.state").read_bytes() == rs.encode_header(ID, 1, BS + 1)
        handle.part.commit(h.write_blocks(handle.part, datas[1]))
        final = rd.finalize(handle.part, "b.bin")
        assert final.read_bytes() == datas[1]
    assert (crash / TARGET).read_bytes() == datas[0]
    check_sentinels(crash)


# ---------------------------------------------------------------------------------------------
# Các dòng còn lại của bảng (dựng từ bản chép)
# ---------------------------------------------------------------------------------------------


def test_wrong_target_is_left_alone_and_received_again_under_new_name(storage, tmp_path, record_for, h):
    crash, (data,) = finalize_with_crash(storage, tmp_path, record_for, h, "_after_replace")
    target = crash / TARGET
    corrupted = bytearray(target.read_bytes())
    corrupted[BS + 3] ^= 0xFF  # cùng kích thước, khối 1 sai
    target.write_bytes(bytes(corrupted))
    rd, _, handle = resume_from(storage, crash, h)
    tdir = crash / ".teleport" / ID
    with rd:
        assert handle.mode == storage.ResumeMode.VERIFY_TARGET  # không băm lúc khởi động (H4)
        digests = bytes(handle.part.reread())
        bad = [
            i for i in range(3) if digests[32 * i : 32 * i + 32] != bytes(handle.leaves[32 * i : 32 * i + 32])
        ]
        assert bad == [1]
        handle.part.drop_target()
        assert handle.part.mode == storage.ResumeMode.NORMAL
        assert os.path.getsize(tdir / "0.part") == 0
        assert (tdir / "0.state").read_bytes() == rs.encode_header(ID, 0, SIZE)
        assert target.read_bytes() == bytes(corrupted)
        handle.part.commit(h.write_blocks(handle.part, data))
        final = rd.finalize(handle.part, NAME)
        assert final not in (target, crash / NAME)
        assert final.read_bytes() == data
    assert target.read_bytes() == bytes(corrupted), "file đích sai phải để nguyên"
    check_sentinels(crash)


def test_drop_target_outside_verify_target_raises_without_touching_disk(storage, tmp_path, record_for, h):
    root, tdir, _ = _interrupted(storage, tmp_path, record_for, h)
    rd, _, handle = resume_from(storage, root, h)
    with rd:
        assert handle.mode == storage.ResumeMode.NORMAL
        before = h.snapshot(tdir)
        with pytest.raises(ValueError):
            handle.part.drop_target()
        assert h.snapshot(tdir) == before


def test_verify_target_whose_file_changed_before_finalize_raises(storage, tmp_path, record_for, h):
    crash, _ = finalize_with_crash(storage, tmp_path, record_for, h, "_after_replace")
    rd, _, handle = resume_from(storage, crash, h)
    with rd:
        with open(crash / TARGET, "ab") as f:
            f.write(b"them")
        changed = (crash / TARGET).read_bytes()
        with pytest.raises(storage.StorageError):
            rd.finalize(handle.part, NAME)
    assert (crash / TARGET).read_bytes() == changed


def test_verify_target_with_missing_bits_restarts(storage, tmp_path, record_for, h):
    crash, _ = finalize_with_crash(storage, tmp_path, record_for, h, "_after_replace")
    state = crash / ".teleport" / ID / "0.state"
    with open(state, "ab") as f:
        f.write(rs.encode_clear([1]))
    target_before = (crash / TARGET).read_bytes()
    rd, _, handle = resume_from(storage, crash, h)
    tdir = crash / ".teleport" / ID
    with rd:
        assert handle.mode == storage.ResumeMode.NORMAL
        assert bit_set(handle.bits, 3) == set()
        assert os.path.getsize(tdir / "0.part") == 0
        assert state.read_bytes() == rs.encode_header(ID, 0, SIZE), "TARGET/RESERVED cũ phải bị quên"
    assert (crash / TARGET).read_bytes() == target_before


def test_reserved_placeholder_without_part_restarts_reusing_it(storage, tmp_path, record_for, h):
    crash, (data,) = finalize_with_crash(storage, tmp_path, record_for, h, "_after_reserved")
    tdir = crash / ".teleport" / ID
    os.unlink(tdir / "0.part")
    rd, _, handle = resume_from(storage, crash, h)
    with rd:
        assert handle.mode == storage.ResumeMode.REUSE_PLACEHOLDER  # RESTART_REUSE
        assert bit_set(handle.bits, 3) == set()
        assert os.path.getsize(tdir / "0.part") == 0
        assert (tdir / "0.state").read_bytes() == (
            rs.encode_header(ID, 0, SIZE) + rs.encode_target(TARGET) + rs.encode_reserved(TARGET)
        )
        handle.part.commit(h.write_blocks(handle.part, data))
        final = rd.finalize(handle.part, "khac.bin")
        assert final == crash / TARGET
        assert final.read_bytes() == data
    check_sentinels(crash)


def test_reserved_name_now_holding_other_data_without_part_restarts_new_name(
    storage, tmp_path, record_for, h
):
    crash, (data,) = finalize_with_crash(storage, tmp_path, record_for, h, "_after_reserved")
    tdir = crash / ".teleport" / ID
    os.unlink(tdir / "0.part")
    (crash / TARGET).write_bytes(b"cua nguoi khac")
    rd, _, handle = resume_from(storage, crash, h)
    with rd:
        assert handle.mode == storage.ResumeMode.NORMAL  # RESTART
        assert bit_set(handle.bits, 3) == set()
        assert (tdir / "0.state").read_bytes() == rs.encode_header(ID, 0, SIZE)
        handle.part.commit(h.write_blocks(handle.part, data))
        final = rd.finalize(handle.part, NAME)
        assert final != crash / TARGET and final.read_bytes() == data
    assert (crash / TARGET).read_bytes() == b"cua nguoi khac"
    check_sentinels(crash)


def test_target_without_reserved_and_without_part_restarts(storage, tmp_path, record_for, h):
    crash, _ = finalize_with_crash(storage, tmp_path, record_for, h, "_after_target")
    tdir = crash / ".teleport" / ID
    os.unlink(tdir / "0.part")
    rd, _, handle = resume_from(storage, crash, h)
    with rd:
        assert handle.mode == storage.ResumeMode.NORMAL
        assert bit_set(handle.bits, 3) == set()
        assert os.path.getsize(tdir / "0.part") == 0
        assert (tdir / "0.state").read_bytes() == rs.encode_header(ID, 0, SIZE)


def test_reuse_placeholder_changed_before_finalize_falls_back_to_new_name(storage, tmp_path, record_for, h):
    crash, (data,) = finalize_with_crash(storage, tmp_path, record_for, h, "_after_reserved")
    rd, _, handle = resume_from(storage, crash, h)
    with rd:
        assert handle.mode == storage.ResumeMode.REUSE_PLACEHOLDER
        (crash / TARGET).write_bytes(b"ke chen ghi vao")
        final = rd.finalize(handle.part, NAME)
        assert final != crash / TARGET and final.read_bytes() == data
    assert (crash / TARGET).read_bytes() == b"ke chen ghi vao"
    check_sentinels(crash)


# ---------------------------------------------------------------------------------------------
# Header `.state` hỏng / mất (mục 8.3): luôn `rewrite_header()` trước khi nối gì
# ---------------------------------------------------------------------------------------------


def _interrupted(storage, tmp_path, record_for, h, *, size=SIZE, blocks=(0, 1)):
    """Lượt nhận dở: đã nhận và đánh dấu `blocks`, rồi app tắt (đóng handle, không xóa)."""
    root = tmp_path / "recv"
    data = payload(size)
    with storage.ReceiveDir.open(root) as rd:
        temp = rd.create_temp(ID, record_for(ID, [size]))
        part = temp.create_part(0, size)
        part.commit(h.write_blocks(part, data, blocks))
        rd.close_temp(temp)
    return root, root / ".teleport" / ID, data


@pytest.mark.parametrize("damage", ["mat", "sai-magic", "sai-crc", "khac-size", "cut-header"])
def test_broken_or_missing_header_is_rewritten_before_anything_else(storage, tmp_path, record_for, h, damage):
    root, tdir, data = _interrupted(storage, tmp_path, record_for, h)
    state = tdir / "0.state"
    raw = bytearray(state.read_bytes())
    if damage == "mat":
        state.unlink()
    elif damage == "sai-magic":
        raw[0] ^= 0x20
        state.write_bytes(bytes(raw))
    elif damage == "sai-crc":
        raw[40] ^= 0x01
        state.write_bytes(bytes(raw))
    elif damage == "khac-size":
        state.write_bytes(rs.encode_header(ID, 0, SIZE - 1) + bytes(raw[41:]))
    else:
        state.write_bytes(bytes(raw[:20]))
    rd, _, handle = resume_from(storage, root, h)
    with rd:
        assert handle.mode == storage.ResumeMode.NORMAL
        assert bit_set(handle.bits, 3) == set()
        assert state.read_bytes() == rs.encode_header(ID, 0, SIZE)
        handle.part.commit(h.write_blocks(handle.part, data))
        r = replay_file(state, 3)
        assert bit_set(r.bits, 3) == {0, 1, 2} and not r.truncated


# ---------------------------------------------------------------------------------------------
# Pha 2: cắt rác, tắt bit sau cuối `.part`, xóa `.tmp` sót
# ---------------------------------------------------------------------------------------------


def test_garbage_after_last_good_record_is_cut(storage, tmp_path, record_for, h):
    root, tdir, data = _interrupted(storage, tmp_path, record_for, h)
    state = tdir / "0.state"
    good = state.read_bytes()
    with open(state, "ab") as f:
        f.write(b"\x01\x00\x05rac rac rac")  # MARK cụt
    rd, _, handle = resume_from(storage, root, h)
    with rd:
        assert bit_set(handle.bits, 3) == {0, 1}
        assert state.read_bytes() == good
        handle.part.commit(h.write_blocks(handle.part, data, [2]))
        r = replay_file(state, 3)
        assert bit_set(r.bits, 3) == {0, 1, 2} and not r.truncated


def test_bytes_beyond_read_ceiling_are_cut_and_log_compacted(storage, tmp_path, record_for, h):
    root, tdir, data = _interrupted(storage, tmp_path, record_for, h, size=100, blocks=(0,))
    state = tdir / "0.state"
    mark = rs.encode_mark([(0, hashlib.sha256(data).digest())])
    cap = rs.max_state_length(1)
    body = mark * ((cap - 41) // len(mark) + 50)  # vượt trần đọc bằng các bản ghi hợp lệ
    state.write_bytes(rs.encode_header(ID, 0, 100) + body)
    assert os.path.getsize(state) > cap
    rd, _, handle = resume_from(storage, root, h)
    with rd:
        assert bit_set(handle.bits, 1) == {0}
        assert os.path.getsize(state) <= cap
        assert state.read_bytes() == rs.encode_header(ID, 0, 100) + mark  # gọn: cắt rồi gộp


def test_bits_beyond_end_of_part_are_cleared_and_stay_cleared(storage, tmp_path, record_for, h):
    size = 3 * BS + 7
    root, tdir, data = _interrupted(storage, tmp_path, record_for, h, size=size, blocks=(0, 1, 2, 3))
    os.truncate(tdir / "0.part", BS + 5)  # chỉ khối 0 nằm trọn trong `.part`
    rd, _, handle = resume_from(storage, root, h)
    with rd:
        assert handle.part_blocks == 1
        assert bit_set(handle.bits, 4) == {0}
        assert leaves_of(handle.leaves, 4)[1:] == [bytes(32)] * 3
        # `_end` = cỡ thật `.part`: ghi khối 1 được, khối 2 tạo lỗ.
        with pytest.raises(storage.WriteGapError):
            handle.part.write_at(2, data[2 * BS : 3 * BS])
        handle.part.write_at(1, data[BS : 2 * BS])
        rd.close_temp(handle.temp)
    assert bit_set(replay_file(tdir / "0.state", 4).bits, 4) == {0}
    rd, _, again = resume_from(storage, root, h)  # đọc lại lần nữa: bit vẫn tắt
    with rd:
        assert bit_set(again.bits, 4) == {0}


def test_part_longer_than_size_counts_every_block(storage, tmp_path, record_for, h):
    root, tdir, _data = _interrupted(storage, tmp_path, record_for, h, blocks=(0, 1, 2))
    with open(tdir / "0.part", "ab") as f:
        f.write(b"thua")
    rd, _, handle = resume_from(storage, root, h)
    with rd:
        assert handle.part_blocks == 3
        assert bit_set(handle.bits, 3) == {0, 1, 2}


def test_leftover_layout_tmp_files_are_removed_others_kept(storage, tmp_path, record_for, h):
    root, tdir, _ = _interrupted(storage, tmp_path, record_for, h)
    for name in ("transfer.json.0123456789abcdef.tmp", "0.state.fedcba9876543210.tmp"):
        (tdir / name).write_bytes(b"sot")
    (tdir / "khac.tmp").write_bytes(b"cua ai do")
    rd, _, _handle = resume_from(storage, root, h)
    with rd:
        assert sorted(os.listdir(tdir)) == ["0.part", "0.state", "khac.tmp", "transfer.json"]


# ---------------------------------------------------------------------------------------------
# H5 và pha 1 chỉ đọc: từ chối thì cây không đổi
# ---------------------------------------------------------------------------------------------


def make_symlink(target, link, *, is_dir: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"không tạo được symlink: {e}")


@pytest.mark.parametrize(
    "damage", ["state-symlink", "part-symlink", "part-la-thu-muc", "state-la-thu-muc", "record-doi-sau-scan"]
)
def test_unsafe_or_changed_files_refuse_resume_without_touching_disk(
    storage, tmp_path, record_for, h, damage
):
    root, tdir, _ = _interrupted(storage, tmp_path, record_for, h)
    victim = tmp_path / "nan-nhan.bin"
    victim.write_bytes(b"cua nan nhan" * 100)
    with storage.ReceiveDir.open(root) as rd:
        scan = running_scan(storage, rd, h)
        if damage == "state-symlink":
            os.unlink(tdir / "0.state")
            make_symlink(victim, tdir / "0.state")
        elif damage == "part-symlink":
            os.unlink(tdir / "0.part")
            make_symlink(victim, tdir / "0.part")
        elif damage == "part-la-thu-muc":
            os.unlink(tdir / "0.part")
            (tdir / "0.part").mkdir()
        elif damage == "state-la-thu-muc":
            os.unlink(tdir / "0.state")
            (tdir / "0.state").mkdir()
        else:
            other = record_for(ID, [SIZE], sender="b" * 32)
            (tdir / "transfer.json").write_bytes(rs.encode_transfer_record(other))
        (tdir / "0.state.0123456789abcdef.tmp").write_bytes(b"sot")  # pha 2 sẽ xóa nếu chạy tới
        before = h.snapshot(tmp_path)
        with pytest.raises(storage.NotResumableError):
            rd.open_for_resume(scan)
        assert h.snapshot(tmp_path) == before


def test_open_for_resume_rejects_scan_that_is_not_running(storage, tmp_path, record_for, h):
    root, _tdir, _ = _interrupted(storage, tmp_path, record_for, h)
    with storage.ReceiveDir.open(root) as rd:
        [scan] = rd.scan_temps(h.RECEIVER, set())  # máy gửi không còn tin cậy → ORPHAN
        assert scan.kind == storage.TempKind.ORPHAN
        before = h.snapshot(tmp_path)
        with pytest.raises(ValueError):
            rd.open_for_resume(scan)
        assert h.snapshot(tmp_path) == before


def test_stop_before_repair_raises_reread_stopped_and_leaves_disk(storage, tmp_path, record_for, h):
    root, tdir, _ = _interrupted(storage, tmp_path, record_for, h)
    os.unlink(tdir / "0.part")  # pha 2 sẽ tạo `.part` mới và ghi lại header
    (tdir / "transfer.json.0123456789abcdef.tmp").write_bytes(b"sot")
    with open(tdir / "0.state", "ab") as f:
        f.write(b"\xffrac")
    stop = threading.Event()
    stop.set()
    with storage.ReceiveDir.open(root) as rd:
        scan = running_scan(storage, rd, h)
        before = h.snapshot(tmp_path)
        with pytest.raises(storage.RereadStopped):
            rd.open_for_resume(scan, stop=stop)
        assert h.snapshot(tmp_path) == before
        handle = rd.open_for_resume(scan)  # không dừng thì làm được
        assert handle.mode == storage.ResumeMode.NORMAL


def test_resumed_temp_is_a_normal_open_temp(storage, tmp_path, record_for, h):
    root, tdir, data = _interrupted(storage, tmp_path, record_for, h)
    rd, scan, handle = resume_from(storage, root, h)
    with rd:
        assert handle.temp.transfer_id == ID and handle.temp.path == tdir
        assert handle.temp.record == scan.record
        assert not handle.temp.closed
        handle.part.commit(h.write_blocks(handle.part, data, [2]))
        assert rd.remove_temp(handle.temp) is True
        assert not tdir.exists()


# ---------------------------------------------------------------------------------------------
# Lỗi soat-code (trung bình) sau P3-3a: `RESERVED` cũ phải bị quên; thứ tự header `.state` → `.part`
# ---------------------------------------------------------------------------------------------


def effective_reserved(h, state: Path) -> str | None:
    """Tên mà `.state` (đọc bằng `parse_state` độc lập) còn coi là file giữ chỗ của mình: `TARGET` cuối có
    `RESERVED` cùng tên đứng sau (architecture mục 4, “Đọc lại”). Không có `.state` → None."""
    if not state.exists():
        return None
    _, records = h.parse_state(state.read_bytes())
    target: str | None = None
    reserved = False
    for rec in records:
        if rec[0] == "target":
            target, reserved = rec[1], False
        elif rec[0] == "reserved" and rec[1] == target:
            reserved = True
    return target if reserved else None


def test_new_name_with_part_forgets_stale_reserved(storage, tmp_path, record_for, h):
    """Sập sau bước 4; trên đĩa sau sập, ứng dụng khác ghi 25 byte vào X (tên đích, không còn là file
    giữ chỗ) → mở lại ra `NORMAL` (`NEW_NAME`, còn `.part`) và `.state` không còn `RESERVED X` có hiệu lực.
    Sập lần nữa ngay sau `open_for_resume`, rồi X bị ghi lại về 0 byte (`O_TRUNC`) → lần mở sau không được
    coi X là file giữ chỗ, `finalize` không ghi đè X."""
    crash, (data,) = finalize_with_crash(storage, tmp_path, record_for, h, "_after_reserved")
    x = crash / TARGET
    state = crash / ".teleport" / ID / "0.state"
    assert effective_reserved(h, state) == TARGET  # đúng điểm sập: RESERVED X đã bền
    x.write_bytes(b"du lieu ung dung khac 25b")
    assert os.path.getsize(x) == 25
    crash2 = tmp_path / "sap-2"
    rd, _, handle = resume_from(storage, crash, h)
    with rd:
        assert handle.mode == storage.ResumeMode.NORMAL
        assert effective_reserved(h, state) is None, "RESERVED cũ của X phải bị quên khi ra NEW_NAME"
        h.crash_copy(crash, crash2)  # sập lần nữa trước bước 1 của finalize
    assert x.read_bytes() == b"du lieu ung dung khac 25b"

    x2 = crash2 / TARGET
    with open(x2, "wb"):  # ứng dụng kia mở X bằng O_TRUNC: X tạm là file 0 byte
        pass
    rd, _, handle = resume_from(storage, crash2, h)
    with rd:
        assert handle.mode != storage.ResumeMode.REUSE_PLACEHOLDER
        final = rd.finalize(handle.part, NAME)
        assert final != x2
        assert final.read_bytes() == data
    assert x2.read_bytes() == b"", "file của ứng dụng khác bị ghi đè"
    check_sentinels(crash2)


def install_crash_points(storage, monkeypatch, h, src: Path, base: Path) -> list[tuple[str, Path]]:
    """Chép cây `src` (sập) ở mọi điểm quanh hai bước “ghi lại header `.state`” và “tạo `.part` mới”:
    trước/sau `ResumeLog.rewrite_header`, ngay trước khi thay `.state` (`_before_atomic_replace`), và
    trước/sau lần tạo `0.part` bằng `O_EXCL`. Trả (nhãn, thư mục bản chép) theo thứ tự xảy ra."""
    copies: list[tuple[str, Path]] = []

    def snap(label: str) -> None:
        dest = base / f"{len(copies):02d}"
        h.crash_copy(src, dest)
        copies.append((label, dest))

    real_rewrite = storage.ResumeLog.rewrite_header

    def rewrite_header(self, *args, **kwargs):
        snap("truoc-rewrite_header")
        result = real_rewrite(self, *args, **kwargs)
        snap("sau-rewrite_header")
        return result

    real_before = storage._before_atomic_replace

    def before_replace(directory, name, *args):
        if str(name).endswith(".state"):
            snap("truoc-thay-.state")
        return real_before(directory, name, *args)

    real_open = os.open

    def spy_open(path, flags, *args, **kwargs):
        is_part = (
            not isinstance(path, int)
            and flags & os.O_EXCL
            and os.path.basename(os.fsdecode(path)) == "0.part"
        )
        if is_part:
            snap("truoc-tao-.part")
        fd = real_open(path, flags, *args, **kwargs)
        if is_part:
            snap("sau-tao-.part")
        return fd

    monkeypatch.setattr(storage.ResumeLog, "rewrite_header", rewrite_header)
    monkeypatch.setattr(storage, "_before_atomic_replace", before_replace)
    monkeypatch.setattr(os, "open", spy_open)
    return copies


def check_crash_copy(storage, h, label: str, root: Path, x_bytes: bytes, data: bytes) -> None:
    """Đĩa sau khi sập ở `label`. Khi X (tên đích) không phải file giữ chỗ 0 byte của mình (`x_bytes`
    khác rỗng): không được còn `RESERVED X` có hiệu lực cạnh một `.part`; nếu đã có `.part` thì cắt X về
    0 byte (ứng dụng khác ghi lại bằng `O_TRUNC`) rồi mở lại vẫn không được coi X là file giữ chỗ.
    Nhận tiếp tới xong: file đích không đổi byte (trừ khi được dùng lại hợp lệ), file nhận đúng nguồn."""
    x = root / TARGET
    tdir = root / ".teleport" / ID
    foreign = bool(x_bytes)
    part_exists = (tdir / "0.part").exists()
    if foreign:
        assert not (part_exists and effective_reserved(h, tdir / "0.state") == TARGET), (
            f"{label}: `.part` mới nằm cạnh RESERVED X cũ"
        )
        if part_exists:
            with open(x, "wb"):
                pass
            x_bytes = b""
    rd, _, handle = resume_from(storage, root, h)
    with rd:
        if foreign:
            assert handle.mode != storage.ResumeMode.REUSE_PLACEHOLDER, label
        if handle.mode == storage.ResumeMode.VERIFY_TARGET:
            assert x.read_bytes() == x_bytes, label
            return  # chưa tới `drop_target`: lượt đọc lại file đích sẽ bắt lỗi như lần trước
        assert os.path.getsize(tdir / "0.part") == 0, label
        handle.part.commit(h.write_blocks(handle.part, data))
        reuse = handle.mode == storage.ResumeMode.REUSE_PLACEHOLDER
        final = rd.finalize(handle.part, NAME)
        assert final.read_bytes() == data, label
    if reuse:
        assert final == x, label
    else:
        assert final != x, label
        assert x.read_bytes() == x_bytes, f"{label}: file đích bị đổi"
    check_sentinels(root)


def test_drop_target_writes_state_header_before_new_part(storage, tmp_path, record_for, h, monkeypatch):
    crash, (data,) = finalize_with_crash(storage, tmp_path, record_for, h, "_after_replace")
    other = payload(SIZE, seed=99)  # cùng cỡ, khác nội dung: file đích không còn là của mình
    (crash / TARGET).write_bytes(other)
    rd, _, handle = resume_from(storage, crash, h)
    with rd:
        assert handle.mode == storage.ResumeMode.VERIFY_TARGET
        copies = install_crash_points(storage, monkeypatch, h, crash, tmp_path / "giua")
        handle.part.drop_target()
        monkeypatch.undo()
    labels = [label for label, _ in copies]
    assert "truoc-tao-.part" in labels and "sau-tao-.part" in labels, labels
    assert "truoc-thay-.state" in labels or "sau-rewrite_header" in labels, labels
    for label, root in copies:
        check_crash_copy(storage, h, label, root, other, data)


@pytest.mark.parametrize("case", ["restart", "restart_reuse"])
def test_open_for_resume_restart_writes_state_header_before_new_part(
    storage, tmp_path, record_for, h, monkeypatch, case
):
    crash, (data,) = finalize_with_crash(storage, tmp_path, record_for, h, "_after_reserved")
    os.unlink(crash / ".teleport" / ID / "0.part")
    x_bytes = b"cua nguoi khac" if case == "restart" else b""
    (crash / TARGET).write_bytes(x_bytes)
    with storage.ReceiveDir.open(crash) as rd:
        scan = running_scan(storage, rd, h)
        copies = install_crash_points(storage, monkeypatch, h, crash, tmp_path / "giua")
        handle = rd.open_for_resume(scan)
        monkeypatch.undo()
        expected = storage.ResumeMode.NORMAL if case == "restart" else storage.ResumeMode.REUSE_PLACEHOLDER
        assert handle.mode == expected
    labels = [label for label, _ in copies]
    assert "truoc-tao-.part" in labels and "sau-tao-.part" in labels, labels
    for label, root in copies:
        check_crash_copy(storage, h, label, root, x_bytes, data)


# ---------------------------------------------------------------------------------------------
# V5 (spec-p3 “Sau phán quyết P3-3a”, spec-p3-3b mục 4): `drop_target` lỗi `fstat` sau khi đã tạo
# `.part` mới → `.part` rỗng sót lại; `open_for_resume` và `cleanup` phải xử lý được
# ---------------------------------------------------------------------------------------------


def _fail_fstat_of_new_part(storage, mp) -> dict:
    """Lần `fstat` đầu tiên trên fd của `0.part` vừa tạo bằng `O_EXCL` ném `OSError(EIO)`."""
    import errno

    real_open, real_fstat = storage.os.open, storage.os.fstat
    seen: dict = {"fd": None, "failed": 0}

    def fake_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if seen["fd"] is None and os.fspath(path).endswith("0.part") and flags & os.O_EXCL:
            seen["fd"] = fd
        return fd

    def fake_fstat(fd, *args, **kwargs):
        if fd == seen["fd"] and not seen["failed"]:
            seen["failed"] += 1
            raise OSError(errno.EIO, "fstat giả lỗi")
        return real_fstat(fd, *args, **kwargs)

    mp.setattr(storage.os, "open", fake_open)
    mp.setattr(storage.os, "fstat", fake_fstat)
    return seen


def _drop_target_fails_after_part_created(storage, tmp_path, record_for, h, *, end: str = "close"):
    """File đích sai ở dòng “đúng `size`” (`VERIFY_TARGET`), `drop_target` lỗi `fstat` ngay sau khi tạo
    `.part`. Rồi app đóng lượt: `end="close"` → `close_temp` (tạm dừng), `"remove"` → `remove_temp` (lượt
    lỗi `storage_failed(REREAD)` bị dọn). Trả (thư mục nhận, thư mục tạm, dữ liệu nguồn, nội dung file đích
    sai); `ReceiveDir` đã đóng."""
    crash, (data,) = finalize_with_crash(storage, tmp_path, record_for, h, "_after_replace")
    target = crash / TARGET
    wrong = payload(SIZE, seed=77)
    target.write_bytes(wrong)
    tdir = crash / ".teleport" / ID
    rd, _, handle = resume_from(storage, crash, h)
    with rd:
        assert handle.mode == storage.ResumeMode.VERIFY_TARGET
        with pytest.MonkeyPatch.context() as mp:
            seen = _fail_fstat_of_new_part(storage, mp)
            with pytest.raises(OSError):
                handle.part.drop_target()
        assert seen["failed"] == 1, "fstat của .part mới không được gọi"
        # `.part` rỗng sót lại cạnh header `.state` đã bền (quên TARGET + RESERVED); file đích không đổi.
        assert os.path.getsize(tdir / "0.part") == 0
        assert (tdir / "0.state").read_bytes() == rs.encode_header(ID, 0, SIZE)
        assert target.read_bytes() == wrong
        if end == "close":
            rd.close_temp(handle.temp)
        else:
            rd.remove_temp(handle.temp)
    return crash, tdir, data, wrong


def test_v5_part_left_by_failed_drop_target_is_resumed_as_new_part(storage, tmp_path, record_for, h):
    crash, _, data, wrong = _drop_target_fails_after_part_created(storage, tmp_path, record_for, h)
    rd, _, handle = resume_from(storage, crash, h)
    with rd:
        assert handle.mode == storage.ResumeMode.NORMAL
        assert handle.part_blocks == 0
        assert bit_set(handle.bits, 3) == set()
        handle.part.commit(h.write_blocks(handle.part, data))
        final = rd.finalize(handle.part, NAME)
        assert final not in (crash / TARGET, crash / NAME)
        assert final.read_bytes() == data
    assert (crash / TARGET).read_bytes() == wrong, "file đích sai phải để nguyên"
    check_sentinels(crash)


def test_v5_part_left_by_failed_drop_target_is_removed_by_cleanup_after_grace(
    storage, tmp_path, record_for, h
):
    import time

    from teleport.core import transfer as core_transfer

    crash, tdir, _, wrong = _drop_target_fails_after_part_created(storage, tmp_path, record_for, h)
    now = time.time()
    with storage.ReceiveDir.open(crash) as rd:
        kw = {"own_device_id": h.RECEIVER, "skip": frozenset(), "retention": storage.TEMP_RETENTION}
        assert rd.cleanup(now, **kw) == 0  # còn trong hạn nối lại
        assert tdir.is_dir()
        assert rd.cleanup(now + core_transfer.RECONNECT_GRACE + 3600, **kw) == 1
    assert not tdir.exists()
    assert (crash / ".teleport").is_dir()
    assert (crash / TARGET).read_bytes() == wrong
    check_sentinels(crash)


def test_v5_part_left_by_failed_drop_target_survives_remove_temp_then_cleanup_removes_it(
    storage, tmp_path, record_for, h
):
    """App dọn lượt lỗi bằng `remove_temp`: `.part` chưa rõ danh tính có thể bị để lại; phần còn lại chỉ là
    tên của bố cục và lần dọn tạm sau `TEMP_RETENTION` xóa hết; không đụng file đích hay file canh gác."""
    import time

    crash, tdir, _, wrong = _drop_target_fails_after_part_created(
        storage, tmp_path, record_for, h, end="remove"
    )
    if tdir.exists():
        assert all(h.LAYOUT_RE.fullmatch(n) for n in os.listdir(tdir)), os.listdir(tdir)
        with storage.ReceiveDir.open(crash) as rd:
            later = time.time() + storage.TEMP_RETENTION + 3600
            kw = {"own_device_id": h.RECEIVER, "skip": frozenset(), "retention": storage.TEMP_RETENTION}
            assert rd.cleanup(later, **kw) == 1
    assert not tdir.exists()
    assert (crash / TARGET).read_bytes() == wrong
    check_sentinels(crash)
