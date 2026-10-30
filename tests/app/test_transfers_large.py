"""Một lượt truyền thật cỡ GiB qua loopback giữa hai node (PR-9, T10).

Viết theo đặc tả, không theo code: mvp F1 (gửi file rất lớn, file nhận đúng từng byte), protocol §4
(khối 1 MiB, đánh số khối, offset = chỉ số khối × cỡ khối), §6.3 (sau DONE thư mục tạm chỉ còn
`transfer.json` có `done_at`). Hai node thật trên 127.0.0.1 (fixture `transfer_nodes`: A nhận, B gửi).

Mục tiêu là lỗi chỉ lộ ra khi file lớn: tràn/cắt offset hay chỉ số khối, khối ghi nhầm chỗ, khối cuối
lẻ. Nội dung file nguồn không toàn số 0: mỗi khối 1 MiB là một khối ngẫu nhiên có seed cố định, đầu
khối đóng dấu số thứ tự khối (8 byte) và cuối khối đóng dấu lại lần nữa, nên khối bị ghi sai chỗ, lặp
hay thiếu đều đổi SHA-256. File dài 1 GiB cộng một đoạn lẻ để khối cuối ngắn hơn cỡ khối.

Giới hạn: 1 GiB chỉ phủ offset tới 2^30. Offset vượt 2^31 (số nguyên 32 bit có dấu) cần file > 2 GiB;
test này không phủ biên đó (chép thêm một tham số cỡ là đủ, nhưng tốn gấp đôi đĩa và thời gian).

File nguồn và file nhận (tổng khoảng 2 GiB) được fixture `big_files` xóa sau khi node đóng, kể cả khi
test hỏng.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import os
import random
import shutil
import struct
import time
from pathlib import Path

import pytest
from support import wait_until

pytestmark = [
    pytest.mark.slow,
    pytest.mark.integration,
    pytest.mark.usefixtures("no_leaks"),
]

MiB = 1024 * 1024
GiB = 1024 * MiB
TAIL = 12_345  # đoạn lẻ: khối cuối ngắn hơn 1 MiB
SIZE = GiB + TAIL
MIN_FREE = 3 * GiB  # nguồn + đích + dư cho thư mục tạm và hệ điều hành
SEED = 20261030
TRANSFER_TIMEOUT = 600.0  # máy chậm (đĩa HDD, quét virus) vẫn đủ; loopback thường chỉ vài chục giây


@pytest.fixture
def T():
    from teleport.app import transfers

    return transfers


@pytest.fixture
def big_files():
    """Dọn file cỡ GiB **sau khi** node đã đóng (Windows không xóa được file đang mở).

    Phải đứng trước `transfer_nodes` trong tham số của test: dựng trước thì tháo sau, lúc các node
    của `node_factory` đã dừng và nhả file nguồn / `.part`. Test đăng ký `(file, thư mục nhận)`.
    Lỗi xóa bị nuốt để không che lỗi gốc của test.
    """
    paths: list[Path] = []
    receive_dirs: list[Path] = []
    yield paths, receive_dirs
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
    # Test hỏng giữa lượt thì `.part` cỡ GiB còn trong thư mục tạm: xóa luôn. Lượt xong thì không
    # còn mảnh nào, bản ghi `transfer.json` để nguyên.
    from teleport.core.filenames import TEMP_DIR_NAME

    for receive_dir in receive_dirs:
        if leftover_temp_files(receive_dir):
            shutil.rmtree(receive_dir / TEMP_DIR_NAME, ignore_errors=True)


def write_pattern_file(path: Path, size: int) -> str:
    """Ghi `size` byte theo mẫu (khối ngẫu nhiên có seed, đóng dấu số khối hai đầu); trả SHA-256 hex.

    SHA tính ngay khi ghi, độc lập với việc đọc lại file, và không giữ cả file trong RAM.
    """
    base = random.Random(SEED).randbytes(MiB)
    h = hashlib.sha256()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        left = size
        for index in itertools.count():
            if left <= 0:
                break
            stamp = struct.pack(">Q", index)
            block = stamp + base[len(stamp) : MiB - len(stamp)] + stamp
            chunk = block[: min(left, MiB)]
            f.write(chunk)
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4 * MiB):
            h.update(chunk)
    return h.hexdigest()


def leftover_temp_files(root: Path) -> list[str]:
    """Mọi file `.part` / `.state` / `.tmp` còn lại dưới `root` (đường dẫn tương đối)."""
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith((".part", ".state", ".tmp")):
                found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return found


async def test_one_gib_local_transfer_sha_matches(T, big_files, transfer_nodes, tmp_path):
    """1 GiB + 12 345 byte, B gửi A qua loopback: hai bên DONE, file nhận đúng kích thước và SHA-256,
    `done_bytes` của mỗi bên không giảm qua các `TransferProgress`, không còn `.part`/`.state`/tạm,
    thư mục tạm của lượt chỉ còn `transfer.json` có `done_at` (protocol §6.3)."""
    if shutil.disk_usage(tmp_path).free < MIN_FREE:
        pytest.skip(f"ổ chứa {tmp_path} còn chưa tới {MIN_FREE // GiB} GiB trống")

    from teleport.core import resume_state as rs
    from teleport.core.filenames import TEMP_DIR_NAME

    net = await transfer_nodes()
    A, B = net.A, net.B
    src = net.src / "lon-1gib.bin"
    dst = net.receive_dir / src.name
    paths, receive_dirs = big_files
    paths += [src, dst]
    receive_dirs.append(net.receive_dir)

    want = write_pattern_file(src, SIZE)
    assert src.stat().st_size == SIZE

    started = time.monotonic()
    key = await B.transfers.send(A.id, [src])
    tid = key.transfer_id
    fin_b = await B.trec.wait_finished(tid, TRANSFER_TIMEOUT)
    fin_a = await A.trec.wait_finished(tid, TRANSFER_TIMEOUT)
    elapsed = time.monotonic() - started

    assert fin_b.result == T.TransferResult.DONE, f"B: {fin_b.result} {fin_b.reason}"
    assert fin_a.result == T.TransferResult.DONE, f"A: {fin_a.result} {fin_a.reason}"
    assert fin_a.info.total_bytes == SIZE and fin_b.info.total_bytes == SIZE
    assert fin_a.info.done_bytes == SIZE and fin_b.info.done_bytes == SIZE

    [done] = A.trec.of("FileDone", tid)
    assert done.path == dst
    assert dst.stat().st_size == SIZE
    assert sha256_of(dst) == want, "file nhận khác file nguồn (khối ghi sai chỗ hay sai offset?)"

    for side in (A, B):
        seq = [u.info.done_bytes for u in side.trec.of("TransferProgress", tid)]
        assert all(x <= y for x, y in itertools.pairwise(seq)), f"{side.label}: done_bytes giảm: {seq}"
        assert all(0 <= x <= SIZE for x in seq), f"{side.label}: done_bytes ra ngoài [0, SIZE]: {seq}"

    temp = net.receive_dir / TEMP_DIR_NAME / tid

    def only_done_record() -> bool:
        try:
            names = os.listdir(temp)
        except FileNotFoundError:
            return False
        if names != ["transfer.json"]:
            return False
        rec = rs.decode_transfer_record((temp / "transfer.json").read_bytes(), dir_name=tid)
        return rec.done_at is not None and rec.verified == len(rec.offer.files)

    # Dọn thư mục tạm là việc nền: chờ có hạn.
    await wait_until(only_done_record, 30, f"thư mục tạm {tid[:8]} chưa chỉ còn transfer.json có done_at")
    # Thư mục nhận chỉ có file đích và thư mục tạm chung; không còn mảnh tạm nào.
    entries = {p.name for p in net.receive_dir.iterdir()}
    assert entries == {dst.name, TEMP_DIR_NAME}, entries
    assert leftover_temp_files(net.receive_dir) == []
    print(f"\n1 GiB loopback: {elapsed:.1f} s ({SIZE / MiB / max(elapsed, 1e-9):.0f} MiB/s)")
