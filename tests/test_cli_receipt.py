"""Kiểm thử lệnh CLI `teleport receipt list` và `teleport receipt verify`."""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path

from teleport.app import receipts as app_receipts
from teleport.cli import main
from teleport.core.merkle import merkle_root
from teleport.core.receipts import ReceiptFile
from teleport.core.transfer import Direction
from teleport.trust.identity import load_or_create_identity


def test_cli_receipt_list_and_verify(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    ident = load_or_create_identity(cfg)

    files_dir = tmp_path / "files"
    files_dir.mkdir()
    sample = files_dir / "note.txt"
    sample.write_bytes(b"teleport receipt cli")
    root = merkle_root(hashlib.sha256(b"teleport receipt cli").digest()).hex()

    receipt = app_receipts.create_receipt(
        ident,
        transfer_id="fe" * 16,
        direction=Direction.SEND,
        completed_at=datetime.datetime(2026, 11, 22, 9, 0, 0, tzinfo=datetime.UTC),
        peer_device_id="01" * 16,
        peer_name="May-Khach",
        files=(ReceiptFile(name="note.txt", size=20, merkle_root=root),),
    )
    saved = app_receipts.save_receipt(
        app_receipts.receipt_path_for(cfg, "fe" * 16, Direction.SEND),
        receipt,
    )

    # 1. `teleport receipt list --json`
    assert main(["--config-dir", str(cfg), "receipt", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed["receipts"]) == 1
    assert listed["receipts"][0]["transfer_id"] == "fe" * 16

    # 2. `teleport receipt verify --dir ... --json` hợp lệ -> mã thoát 0
    assert (
        main(
            [
                "--config-dir",
                str(cfg),
                "receipt",
                "verify",
                str(saved),
                "--dir",
                str(files_dir),
                "--json",
            ]
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["valid"] is True
    assert verified["signature_ok"] is True
    assert verified["trusted_signer"] is True
    assert verified["files_ok"] == 1

    # 3. Tệp bị sửa -> mã thoát 1
    sample.write_bytes(b"changed")
    assert (
        main(
            [
                "--config-dir",
                str(cfg),
                "receipt",
                "verify",
                str(saved),
                "--dir",
                str(files_dir),
                "--json",
            ]
        )
        == 1
    )
    failed = json.loads(capsys.readouterr().out)
    assert failed["valid"] is False
    assert failed["signature_ok"] is True
    assert failed["files_ok"] == 0
