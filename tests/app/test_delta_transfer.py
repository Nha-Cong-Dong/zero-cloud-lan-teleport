"""Kiểm thử end-to-end Smart Delta Teleport qua hai `Node` thật trên loopback."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from support import wait_until

from teleport.app.delta import DELTA_INDEX_FILE, DeltaIndexStore, is_safe_regular_file
from teleport.app.transfers import TransferResult
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.delta import delta_lookup_key


def _make_blocks(seeds: list[bytes]) -> bytes:
    parts: list[bytes] = []
    for seed in seeds:
        repeat = (BLOCK_SIZE // len(seed)) + 1
        parts.append((seed * repeat)[:BLOCK_SIZE])
    return b"".join(parts)


@pytest.mark.integration
async def test_smart_delta_level1_zero_payload_and_level2_partial_reuse(
    transfer_nodes, tmp_path: Path
) -> None:
    net = await transfer_nodes()
    net.A.node.set_create_subfolder(False)
    net.A.node.set_delta_sync(True)
    net.B.node.set_delta_sync(True)
    payload_v1 = _make_blocks([b"blk0-", b"blk1-", b"blk2-", b"blk3-", b"blk4-", b"blk5-"])
    src_file = net.src / "dataset.bin"
    src_file.write_bytes(payload_v1)

    # Lần 1: truyền đầy đủ file 6 MiB từ B -> A
    key1 = await net.B.transfers.send(net.A.id, [src_file])
    fin1_a = await net.A.trec.wait_finished(key1.transfer_id)
    fin1_b = await net.B.trec.wait_finished(key1.transfer_id)
    assert fin1_a.result is TransferResult.DONE
    assert fin1_b.payload_sent == len(payload_v1)
    assert (net.receive_dir / "dataset.bin").read_bytes() == payload_v1

    # Kiểm tra `delta_index.json` đã ghi chỉ mục trên máy nhận
    await wait_until(
        lambda: (net.A.dir / DELTA_INDEX_FILE).is_file(),
        5,
        "ghi delta_index.json sau khi nhận",
    )
    store = DeltaIndexStore.load(net.A.dir)
    assert any(e.key == delta_lookup_key("dataset.bin") for e in store.entries())

    # Lần 2 (Mức 1): gửi lại tệp y hệt -> 0 byte dữ liệu qua mạng
    key2 = await net.B.transfers.send(net.A.id, [src_file])
    fin2_a = await net.A.trec.wait_finished(key2.transfer_id)
    fin2_b = await net.B.trec.wait_finished(key2.transfer_id)
    assert fin2_a.result is TransferResult.DONE
    assert fin2_a.delta_reused_bytes == len(payload_v1)
    assert fin2_b.payload_sent == 0

    # Lần 3 (Mức 2): chỉ đổi khối 2 và khối 4 trong 6 khối (tái sử dụng 4 MiB, gửi 2 MiB)
    payload_v2 = _make_blocks([b"blk0-", b"blk1-", b"EDIT2", b"blk3-", b"EDIT4", b"blk5-"])
    src_file.write_bytes(payload_v2)

    key3 = await net.B.transfers.send(net.A.id, [src_file])
    fin3_a = await net.A.trec.wait_finished(key3.transfer_id)
    fin3_b = await net.B.trec.wait_finished(key3.transfer_id)
    assert fin3_a.result is TransferResult.DONE
    assert fin3_a.delta_reused_bytes == 4 * BLOCK_SIZE
    assert fin3_b.payload_sent == 2 * BLOCK_SIZE
    saved_v2 = net.receive_dir / "dataset (2).bin"
    assert saved_v2.read_bytes() == payload_v2
    assert hashlib.sha256(saved_v2.read_bytes()).hexdigest() == hashlib.sha256(payload_v2).hexdigest()

    # Lần 4 (tắt delta_sync): truyền lại toàn bộ không dùng delta
    net.B.node.set_delta_sync(False)
    key4 = await net.B.transfers.send(net.A.id, [src_file])
    fin4_a = await net.A.trec.wait_finished(key4.transfer_id)
    fin4_b = await net.B.trec.wait_finished(key4.transfer_id)
    assert fin4_a.result is TransferResult.DONE
    assert fin4_a.delta_reused_bytes == 0
    assert fin4_b.payload_sent == len(payload_v2)


@pytest.mark.integration
async def test_smart_delta_disabled_setting_sends_full_file(transfer_nodes) -> None:
    net = await transfer_nodes()
    net.A.node.set_create_subfolder(False)
    net.A.node.set_delta_sync(True)
    net.B.node.set_delta_sync(False)
    payload = _make_blocks([b"one-", b"two-"])
    src_file = net.src / "full.bin"
    src_file.write_bytes(payload)

    key1 = await net.B.transfers.send(net.A.id, [src_file])
    assert (await net.A.trec.wait_finished(key1.transfer_id)).result is TransferResult.DONE

    key2 = await net.B.transfers.send(net.A.id, [src_file])
    fin2_a = await net.A.trec.wait_finished(key2.transfer_id)
    fin2_b = await net.B.trec.wait_finished(key2.transfer_id)
    assert fin2_a.result is TransferResult.DONE
    assert fin2_a.delta_reused_bytes == 0
    assert fin2_b.payload_sent == len(payload)
    assert is_safe_regular_file(src_file)
