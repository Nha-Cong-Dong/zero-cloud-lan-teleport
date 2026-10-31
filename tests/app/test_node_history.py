"""Lịch sử theo từng máy qua node thật (C3; spec `specs/spec-dot-c.md` mục 3.2, 3.5, 11).

Viết từ đặc tả. Quy ước như `test_transfer_history.py`: fixture `transfer_nodes`, **A nhận**, **B gửi**.
Mục của “máy khác” (không phải B) được viết sẵn vào `history.json` của A trước khi A chạy; nội dung
file được đọc độc lập bằng `json`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
leakcheck = pytest.mark.usefixtures("no_leaks")

OTHER_DEVS = ("1" * 32, "2" * 32, "3" * 32)
CLOSE_WARNINGS = ("việc dọn đĩa chưa xong", "không đóng thư mục nhận")


# --- Hàm phụ trợ ---------------------------------------------------------------------------------


async def wait_until(predicate, timeout: float = 5.0, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.01)


def make_file(directory: Path, name: str, size: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(os.urandom(size))
    return path


def old_entry_json(k: int, device_id: str) -> dict:
    return {
        "direction": "receive",
        "device_id": device_id,
        "device_name": "May-Cu",
        "transfer_id": f"{k + 1:032x}",
        "file_count": 1,
        "total_bytes": 10,
        "done_bytes": 10,
        "names": [f"cu-{k}.bin"],
        "result": "done",
        "finished_at": "2026-01-01T00:00:00Z",
        "saved_path": None,
    }


def preload(tmp_path: Path, entries: list[dict]) -> Path:
    """Viết `history.json` cho A (thư mục cấu hình `tmp_path/"A"`) trước khi A chạy."""
    config = tmp_path / "A"
    config.mkdir(parents=True, exist_ok=True)
    (config / "history.json").write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
    return config


def read_replaced(path: Path, budget: float = 1.0) -> bytes:
    """Đọc file node có thể đang thay nguyên tử (Windows báo PermissionError đúng lúc đó): thử lại."""
    deadline = time.monotonic() + budget
    while True:
        try:
            return path.read_bytes()
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def file_keys(config: Path) -> list[tuple[str, str]]:
    obj = json.loads(read_replaced(config / "history.json"))
    return [(e["device_id"], e["transfer_id"]) for e in obj["entries"]]


def keys(entries) -> list[tuple[str, str]]:
    return [(e.device_id, e.transfer_id) for e in entries]


async def close_cleanly(node, caplog, timeout: float = 45.0) -> None:
    caplog.set_level(logging.WARNING, logger="teleport.app.transfers")
    start = len(caplog.records)
    await asyncio.wait_for(node.close(), timeout)
    bad = [
        r.getMessage()
        for r in caplog.records[start:]
        if r.name == "teleport.app.transfers" and any(w in r.getMessage() for w in CLOSE_WARNINGS)
    ]
    assert not bad, f"đóng node mà hàng đĩa chưa xong: {bad}"


async def done_and_pending(net) -> tuple[str, str]:
    """B gửi A một lượt xong (`done`), rồi một lượt A chưa trả lời (`auto_accept` tắt): trả hai
    `transfer_id`. Lượt thứ hai đang chờ quyết định khi hàm trả về."""
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "xong.bin", 2000)])
    await A.trec.wait_finished(key.transfer_id)
    A.transfers.auto_accept = False
    pending = await B.transfers.send(A.id, [make_file(net.src, "cho.bin", 2000)])
    await A.trec.wait_kind("OfferReceived", pending.transfer_id)
    assert A.trec.finished(pending.transfer_id) is None
    return key.transfer_id, pending.transfer_id


# =================================================================================================


@leakcheck
async def test_history_filter_by_device(transfer_nodes, tmp_path):
    """`history(device_id)` chỉ trả lượt với máy đó (cả máy không còn ghép đôi); `device_id` sai dạng →
    MessageError; máy chưa từng có lượt → rỗng."""
    from teleport.core.messages import MessageError

    old = [old_entry_json(k, OTHER_DEVS[k % 2]) for k in range(6)]
    preload(tmp_path, old)
    net = await transfer_nodes()
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "a.bin", 1000)])
    tid = key.transfer_id
    await A.trec.wait_finished(tid)

    assert keys(A.transfers.history(B.id)) == [(B.id, tid)]
    assert keys(A.transfers.history(OTHER_DEVS[0])) == [(d["device_id"], d["transfer_id"]) for d in old[0::2]]
    assert keys(A.transfers.history(OTHER_DEVS[1])) == [(d["device_id"], d["transfer_id"]) for d in old[1::2]]
    assert A.transfers.history(OTHER_DEVS[2]) == ()
    assert len(A.transfers.history()) == 7
    assert isinstance(A.transfers.history(B.id), tuple)
    for bad in ("Z" * 32, B.id.upper(), B.id[:31], 7, b"0" * 32):
        with pytest.raises(MessageError):
            A.transfers.history(bad)
        with pytest.raises(MessageError):
            A.transfers.clear_history(bad)
    assert len(A.transfers.history()) == 7, "device_id sai không được xóa gì"
    # Bên gửi: lượt nằm ở mục của A. B có thể kết thúc lượt sau A một chút: chờ B.
    await B.trec.wait_finished(tid)
    await wait_until(lambda: B.transfers.history(A.id) != (), 10, "B chưa có mục lịch sử")
    assert keys(B.transfers.history(A.id)) == [(A.id, tid)]


@leakcheck
async def test_clear_history_of_one_device_keeps_the_others(transfer_nodes, tmp_path, caplog):
    old = [old_entry_json(k, OTHER_DEVS[0]) for k in range(3)]
    config_a = preload(tmp_path, old)
    net = await transfer_nodes()
    A, B = net.A, net.B
    key = await B.transfers.send(A.id, [make_file(net.src, "a.bin", 1000)])
    await A.trec.wait_finished(key.transfer_id)
    A.transfers.clear_history(OTHER_DEVS[0])
    assert keys(A.transfers.history()) == [(B.id, key.transfer_id)]
    await close_cleanly(A, caplog)
    assert file_keys(config_a) == [(B.id, key.transfer_id)]


@leakcheck
async def test_unpair_clear_history_removes_unpaired_entries_too(transfer_nodes, tmp_path, caplog):
    """`unpair(..., clear_history=True)`: mục cũ của máy đó **và** mục `unpaired` vừa sinh (lượt đang chờ
    bị hủy trong `forget_device`) đều bị xóa, cả trong bộ nhớ lẫn trong file; mục của máy khác giữ."""
    old = [old_entry_json(k, OTHER_DEVS[0]) for k in range(2)]
    config_a = preload(tmp_path, old)
    net = await transfer_nodes()
    A, B = net.A, net.B
    await done_and_pending(net)
    await A.unpair(B.id, clear_history=True)
    assert A.transfers.history(B.id) == ()
    assert keys(A.transfers.history()) == [(d["device_id"], d["transfer_id"]) for d in old]
    await close_cleanly(A, caplog)
    assert file_keys(config_a) == [(d["device_id"], d["transfer_id"]) for d in old]


@leakcheck
async def test_unpair_default_keeps_history(transfer_nodes, tmp_path, caplog):
    """Mặc định `unpair` giữ lịch sử (Q4, Q5): còn mục `done` cũ và có thêm mục `unpaired` không tên."""
    config_a = tmp_path / "A"
    net = await transfer_nodes()
    A, B = net.A, net.B
    done_tid, pending_tid = await done_and_pending(net)
    await A.unpair(B.id)
    fin = await A.trec.wait_finished(pending_tid)
    assert str(fin.result) == "unpaired"
    got = A.transfers.history(B.id)
    assert [(e.transfer_id, e.result) for e in got] == [(pending_tid, "unpaired"), (done_tid, "done")]
    assert got[0].device_name is None
    await close_cleanly(A, caplog)
    assert file_keys(config_a) == [(B.id, pending_tid), (B.id, done_tid)]


@leakcheck
async def test_old_file_with_100_mixed_entries_loads_unchanged(transfer_nodes, tmp_path, caplog):
    """File của bản cũ (100 mục, nhiều máy) nạp đúng như cũ; thêm một lượt thì không mục cũ nào mất
    (trần nay theo từng máy), và lần ghi giữ nguyên thứ tự."""
    old = [old_entry_json(k, OTHER_DEVS[k % 3]) for k in range(100)]
    config_a = preload(tmp_path, old)
    net = await transfer_nodes()
    A, B = net.A, net.B
    expected_old = [(d["device_id"], d["transfer_id"]) for d in old]
    assert keys(A.transfers.history()) == expected_old
    key = await B.transfers.send(A.id, [make_file(net.src, "moi.bin", 1000)])
    await A.trec.wait_finished(key.transfer_id)
    assert keys(A.transfers.history()) == [(B.id, key.transfer_id), *expected_old]
    await close_cleanly(A, caplog)
    assert file_keys(config_a) == [(B.id, key.transfer_id), *expected_old]
