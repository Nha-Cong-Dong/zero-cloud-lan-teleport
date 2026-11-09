"""mvp N7: Độ tin cậy - 100 lượt truyền liên tiếp (file nhỏ lớn lẫn).

Tiêu chí N7:
1. Không crash, 100 lượt đều hoàn thành thành công (TransferResult.DONE), toàn vẹn dữ liệu 100%.
2. Không sót file tạm: mọi file tạm `.part` đều được dọn sạch, chỉ giữ bia mộ `transfer.json`.
3. Không rò bộ nhớ: bộ nhớ không phình liên tục theo số lượt truyền (đo bằng tracemalloc).
"""

from __future__ import annotations

import hashlib
import os
import tracemalloc
from pathlib import Path

import pytest

from teleport.app.transfers import TransferResult

MiB = 1024 * 1024


def make_test_file(directory: Path, name: str, size: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    with open(p, "wb") as f:
        left = size
        while left:
            chunk = os.urandom(min(left, 256 * 1024))
            f.write(chunk)
            left -= len(chunk)
    return p


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(256 * 1024):
            h.update(chunk)
    return h.hexdigest()


@pytest.mark.slow
@pytest.mark.integration
async def test_n7_soak_100_consecutive_transfers(transfer_nodes, tmp_path):
    """100 lượt truyền liên tiếp giữa hai node trên loopback (mvp N7)."""
    net = await transfer_nodes()
    tracemalloc.start()

    # Chuẩn bị trước một số kích cỡ file đa dạng (nhỏ, trung bình, lớn hơn 1 khối)
    file_specs = [
        ("tiny.txt", 1024),  # 1 KiB
        ("doc.pdf", 64 * 1024),  # 64 KiB
        ("photo.jpg", 512 * 1024),  # 512 KiB
        ("clip.mp4", 1 * MiB + 100),  # > 1 khối (1 MiB)
        ("archive.zip", 2 * MiB + 5000),  # > 2 khối
    ]
    src_files = [make_test_file(net.src, name, sz) for name, sz in file_specs]
    src_hashes = {f.name: sha256_of(f) for f in src_files}

    # Đo bộ nhớ mốc sau khi khởi tạo và warm-up
    mem_samples = []

    TOTAL_TRANSFERS = 100

    for i in range(TOTAL_TRANSFERS):
        # Xen kẽ các kiểu gửi: 1 file đơn, hoặc nhóm nhiều file
        mode = i % 4
        if mode == 0:
            # Gửi 1 file nhỏ
            batch = [src_files[0]]
        elif mode == 1:
            # Gửi 1 file vừa
            batch = [src_files[1]]
        elif mode == 2:
            # Gửi file lớn hơn 1 khối Merkle
            batch = [src_files[3]]
        else:
            # Gửi cùng lúc 2 file
            batch = [src_files[0], src_files[2]]

        key = await net.B.transfers.send(net.A.id, batch)
        fin_b = await net.B.trec.wait_finished(key.transfer_id, timeout=10.0)
        fin_a = await net.A.trec.wait_finished(key.transfer_id, timeout=10.0)

        # 1. Không crash, kết quả DONE
        assert fin_b.result is TransferResult.DONE, f"Lượt {i} bên gửi không thành công: {fin_b}"
        assert fin_a.result is TransferResult.DONE, f"Lượt {i} bên nhận không thành công: {fin_a}"

        # Kiểm tra file tạm .part đã bị xóa khỏi thư mục .teleport
        temp_dir = net.receive_dir / ".teleport" / key.transfer_id
        if temp_dir.exists():
            remaining_parts = [p.name for p in temp_dir.glob("*.part*")]
            assert not remaining_parts, f"Lượt {i} còn sót file tạm: {remaining_parts}"

        # Lấy mẫu bộ nhớ mỗi 10 lượt
        if (i + 1) % 10 == 0:
            current, _peak = tracemalloc.get_traced_memory()
            mem_samples.append(current)

    tracemalloc.stop()

    # 2. Toàn vẹn dữ liệu: các file nhận được phải có nội dung khớp hash nguồn
    for p in net.receive_dir.iterdir():
        if p.name == ".teleport":
            continue
        # Tìm hash tương ứng
        matched = False
        p_hash = sha256_of(p)
        for orig_hash in src_hashes.values():
            if p_hash == orig_hash:
                matched = True
                break
        assert matched, f"File nhận được {p.name} không khớp bất kỳ file nguồn nào"

    # 3. Kiểm tra rò rỉ bộ nhớ:
    # Bộ nhớ ở các lượt 20, 30, ... 100 phải tương đối phẳng (không tăng tuyến tính theo số lượt)
    mem_10 = mem_samples[0]
    mem_100 = mem_samples[-1]
    # Mức chênh lệch bộ nhớ giữa lượt 10 và 100 không được vượt quá 10 MiB
    growth_bytes = mem_100 - mem_10
    assert growth_bytes < 10 * MiB, (
        f"Nghi vấn rò rỉ bộ nhớ: tăng {growth_bytes / 1024 / 1024:.2f} MiB sau 90 lượt truyền"
    )
