"""Kiểm thử an toàn đĩa và tích hợp Smart Receive Routing (`teleport.app.routing`)."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

from teleport.app import routing as app_routing
from teleport.app.transfers import TransferResult
from teleport.core.routing import RoutingRule


def test_deliver_to_routed_dir_non_overwrite_and_subfolder(tmp_path: Path) -> None:
    src_dir = tmp_path / "recv"
    dest_dir = tmp_path / "sorted"
    src_dir.mkdir()
    dest_dir.mkdir()

    existing = dest_dir / "sub" / "doc.txt"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"keep-me")

    f1 = src_dir / "doc.txt"
    f1.write_bytes(b"first-incoming")
    out1 = app_routing.deliver_to_routed_dir(
        f1, dest_dir, name="doc.txt", rel_path="sub/doc.txt", mtime_ms=1700000000000, is_dir=False
    )
    assert out1 == dest_dir / "sub" / "doc (1).txt"
    assert existing.read_bytes() == b"keep-me"
    assert out1.read_bytes() == b"first-incoming"
    assert not f1.exists()


def test_deliver_to_routed_dir_exdev_stream_fallback(tmp_path: Path, monkeypatch) -> None:
    src_dir = tmp_path / "recv"
    dest_dir = tmp_path / "other-vol"
    src_dir.mkdir()
    dest_dir.mkdir()
    f = src_dir / "big.bin"
    payload = b"ABCD" * 25_000
    f.write_bytes(payload)

    real_replace = os.replace

    def fake_replace(a, b):
        if Path(a) == f:
            raise OSError(errno.EXDEV, "cross-device")
        return real_replace(a, b)

    monkeypatch.setattr(os, "replace", fake_replace)
    out = app_routing.deliver_to_routed_dir(
        f, dest_dir, name="big.bin", rel_path="", mtime_ms=0, is_dir=False
    )
    assert out == dest_dir / "big.bin"
    assert out.read_bytes() == payload
    assert not f.exists()


def test_deliver_to_routed_dir_rejects_symlink_destination(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("tạo symlink trên Windows cần đặc quyền riêng")
    src = tmp_path / "f.txt"
    src.write_bytes(b"abc")
    real_outside = tmp_path / "outside"
    real_outside.mkdir()
    dest = tmp_path / "link-dest"
    dest.symlink_to(real_outside)

    with pytest.raises(OSError):
        app_routing.deliver_to_routed_dir(src, dest, name="f.txt", rel_path="", mtime_ms=0, is_dir=False)
    assert src.exists()


@pytest.mark.integration
async def test_node_receive_routes_matching_file_and_prunes_empty_subfolder(
    transfer_nodes, tmp_path: Path
) -> None:
    routed_pdfs = tmp_path / "PDFs"
    net = await transfer_nodes()
    net.A.node.set_create_subfolder(True)
    net.A.node.set_routing_rules(
        (
            RoutingRule(
                id="pdf-rule",
                name="PDF",
                destination=str(routed_pdfs),
                patterns=("*.pdf",),
            ),
        )
    )
    pdf = net.src / "invoice.pdf"
    txt = net.src / "notes.txt"
    pdf.write_bytes(b"%PDF-1.4-content")
    txt.write_bytes(b"plain text")

    key = await net.B.transfers.send(net.A.id, [pdf, txt])
    fin = await net.A.trec.wait_finished(key.transfer_id)
    assert fin.result is TransferResult.DONE

    routed = routed_pdfs / "invoice.pdf"
    assert routed.read_bytes() == b"%PDF-1.4-content"
    plain_matches = list(net.receive_dir.rglob("notes.txt"))
    assert len(plain_matches) == 1
    assert plain_matches[0].read_bytes() == b"plain text"
    assert list(net.receive_dir.rglob("invoice.pdf")) == []
