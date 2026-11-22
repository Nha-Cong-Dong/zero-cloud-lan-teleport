"""Kiểm thử ký, lưu, xuất và xác minh biên lai truyền tệp (`teleport.app.receipts`)."""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

import pytest
from support import wait_until

from teleport.app import receipts as app_receipts
from teleport.app import transfers as app_transfers
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.merkle import merkle_root
from teleport.core.receipts import ReceiptFile
from teleport.core.transfer import Direction
from teleport.trust.identity import load_or_create_identity

pytestmark = [
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
leakcheck = pytest.mark.usefixtures("no_leaks")


def test_create_save_verify_and_tamper_detection(tmp_path: Path) -> None:
    cfg_a = tmp_path / "cfg_a"
    cfg_a.mkdir()
    id_a = load_or_create_identity(cfg_a)

    cfg_other = tmp_path / "cfg_other"
    cfg_other.mkdir()
    id_other = load_or_create_identity(cfg_other)

    files_dir = tmp_path / "files"
    (files_dir / "nested").mkdir(parents=True)
    f1 = files_dir / "hello.txt"
    f1.write_bytes(b"hello world")
    f2 = files_dir / "nested" / "empty.bin"
    f2.write_bytes(b"")
    big = files_dir / "multi.bin"
    chunk1 = b"A" * BLOCK_SIZE
    chunk2 = b"B" * 123
    big.write_bytes(chunk1 + chunk2)

    import hashlib

    root1 = merkle_root(hashlib.sha256(b"hello world").digest()).hex()
    root2 = merkle_root(b"").hex()
    root3 = merkle_root(hashlib.sha256(chunk1).digest() + hashlib.sha256(chunk2).digest()).hex()

    receipt = app_receipts.create_receipt(
        id_a,
        transfer_id="12" * 16,
        direction=Direction.RECEIVE,
        completed_at=datetime.datetime(2026, 11, 22, 12, 0, 0, tzinfo=datetime.UTC),
        peer_device_id=id_other.device_id,
        peer_name="May-Gui",
        files=(
            ReceiptFile(name="hello.txt", size=11, merkle_root=root1),
            ReceiptFile(name="nested/empty.bin", size=0, merkle_root=root2),
            ReceiptFile(name="multi.bin", size=len(chunk1) + len(chunk2), merkle_root=root3),
        ),
    )
    saved = app_receipts.save_receipt(
        app_receipts.receipt_path_for(cfg_a, "12" * 16, Direction.RECEIVE),
        receipt,
    )
    assert saved.is_file()
    assert app_receipts.list_receipts(cfg_a) == [saved]

    # 1. Xác minh hợp lệ cả chữ ký, danh sách tin cậy lẫn tệp thật
    ok = app_receipts.verify_receipt(
        saved,
        files_dir=files_dir,
        trusted_fingerprints={id_a.fingerprint},
    )
    assert ok.valid is True
    assert ok.signature_ok is True
    assert ok.trusted_signer is True
    assert ok.files_checked == 3
    assert ok.files_ok == 3
    assert ok.errors == ()

    # 2. Máy ký chưa nằm trong tập trusted_fingerprints -> vẫn giữ valid chữ ký nhưng trusted_signer=False
    untrusted = app_receipts.verify_receipt(
        saved,
        files_dir=files_dir,
        trusted_fingerprints={id_other.fingerprint},
    )
    assert untrusted.valid is True
    assert untrusted.signature_ok is True
    assert untrusted.trusted_signer is False

    # 3. Đổi nội dung file trên đĩa -> phát hiện sai kích thước / Merkle
    f1.write_bytes(b"hello worlX")
    bad_file = app_receipts.verify_receipt(saved, files_dir=files_dir)
    assert bad_file.valid is False
    assert bad_file.signature_ok is True
    assert bad_file.files_ok == 2

    # 4. Sửa payload trong JSON biên lai -> chữ ký không hợp lệ
    tampered = tmp_path / "tampered.teleport-receipt.json"
    doc = json.loads(saved.read_text(encoding="utf-8"))
    doc["peer_name"] = "Ke-Gia-Mao"
    tampered.write_text(json.dumps(doc), encoding="utf-8")
    bad_sig = app_receipts.verify_receipt(tampered)
    assert bad_sig.valid is False
    assert bad_sig.signature_ok is False


def test_verify_receipt_rejects_symlink_target(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    ident = load_or_create_identity(cfg)

    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")
    import hashlib

    root = merkle_root(hashlib.sha256(b"secret").digest()).hex()
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    link = files_dir / "secret.txt"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("môi trường không cho tạo symlink")

    receipt = app_receipts.create_receipt(
        ident,
        transfer_id="ab" * 16,
        direction=Direction.SEND,
        completed_at=datetime.datetime(2026, 11, 22, 12, 0, 0, tzinfo=datetime.UTC),
        peer_device_id="cd" * 16,
        peer_name="Peer",
        files=(ReceiptFile(name="secret.txt", size=6, merkle_root=root),),
    )
    path = app_receipts.save_receipt(tmp_path / "sym.teleport-receipt.json", receipt)
    res = app_receipts.verify_receipt(path, files_dir=files_dir)
    assert res.valid is False
    assert res.files_ok == 0
    assert any("vượt ngoài" in err for err in res.errors)


@leakcheck
async def test_two_node_transfer_creates_and_verifies_receipts(transfer_nodes, tmp_path: Path) -> None:
    """Truyền tệp + thư mục con giữa 2 node thật -> cả hai đầu tạo biên lai có chữ ký, xác minh khớp."""
    net = await transfer_nodes()
    A, B = net.A, net.B

    folder = net.src / "bo_anh"
    folder.mkdir()
    f1 = net.src / "bao_cao.txt"
    f1.write_bytes(b"Noi dung bao cao ZCL Teleport")
    f2 = folder / "ghi_chu.md"
    f2.write_bytes(b"# Anh 2026\n")
    empty = folder / "trong.txt"
    empty.write_bytes(b"")

    key = await B.transfers.send(A.id, [f1, folder], allow_dirs=True)
    tid = key.transfer_id

    fin_b = await B.trec.wait_finished(tid, 30)
    fin_a = await A.trec.wait_finished(tid, 30)
    assert fin_b.result is app_transfers.TransferResult.DONE
    assert fin_a.result is app_transfers.TransferResult.DONE

    await wait_until(lambda: B.node.receipt_path(tid, Direction.SEND) is not None, 10)
    await wait_until(lambda: A.node.receipt_path(tid, Direction.RECEIVE) is not None, 10)

    send_receipt_path = B.node.receipt_path(tid, Direction.SEND)
    recv_receipt_path = A.node.receipt_path(tid, Direction.RECEIVE)
    assert send_receipt_path is not None and recv_receipt_path is not None

    first_done = A.trec.of("FileDone", tid)[0]
    assert first_done.path is not None
    saved_root = first_done.path.parent
    assert (saved_root / "bao_cao.txt").is_file()

    # Xuất bản sao qua Node.export_receipt
    exported = tmp_path / "exported" / "copy.teleport-receipt.json"
    out = A.node.export_receipt(tid, Direction.RECEIVE, exported)
    assert out == exported and exported.is_file()

    # Xác minh biên lai nhận trên chính thư mục con đã nhận thật
    res_recv = app_receipts.verify_receipt(
        exported,
        files_dir=saved_root,
        trusted_fingerprints=app_receipts.known_trusted_fingerprints(A.dir),
    )
    assert res_recv.valid is True
    assert res_recv.signature_ok is True
    assert res_recv.trusted_signer is True
    assert res_recv.files_checked == 3
    assert res_recv.files_ok == 3

    # Máy A xác minh biên lai do B ký đối chiếu với thư mục file đã nhận của A
    res_send = app_receipts.verify_receipt(
        send_receipt_path,
        files_dir=saved_root,
        trusted_fingerprints=app_receipts.known_trusted_fingerprints(A.dir),
    )
    assert res_send.valid is True
    assert res_send.signature_ok is True
    assert res_send.trusted_signer is True
    assert res_send.files_checked == 3
    assert res_send.files_ok == 3

    # Nếu tệp nhận bị sửa trên đĩa -> verify_receipt phát hiện ngay
    if sys.platform != "win32" or (saved_root / "bao_cao.txt").is_file():
        (saved_root / "bao_cao.txt").write_bytes(b"Tampered content!")
        res_bad = app_receipts.verify_receipt(recv_receipt_path, files_dir=saved_root)
        assert res_bad.valid is False
        assert res_bad.files_ok == 2
