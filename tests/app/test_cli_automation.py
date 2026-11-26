"""Kiểm thử lệnh CLI tự động hóa (`teleport routing`, `teleport watch` và `--no-delta`)."""

from __future__ import annotations

import json
from pathlib import Path

from teleport.cli import build_parser, main
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore


def test_cli_send_receive_no_delta_flag() -> None:
    parser = build_parser()
    args_send = parser.parse_args(["send", "a.bin", "--to", "PeerB", "--no-delta"])
    assert args_send.no_delta is True

    args_recv = parser.parse_args(["receive", "--no-delta"])
    assert args_recv.no_delta is True


def test_cli_routing_list_add_remove(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    load_or_create_identity(cfg)
    dest = tmp_path / "routed-videos"
    dest.mkdir()

    # Rỗng ban đầu
    assert main(["--config-dir", str(cfg), "routing", "list", "--json"]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["routing_rules"] == []

    # Thêm quy tắc
    assert (
        main(
            [
                "--config-dir",
                str(cfg),
                "routing",
                "add",
                "--name",
                "Videos",
                "--dest",
                str(dest),
                "--pattern",
                "*.mp4,*.mkv",
                "--json",
            ]
        )
        == 0
    )
    added = json.loads(capsys.readouterr().out)
    rule_id = added["added"]["id"]
    assert added["added"]["name"] == "Videos"
    assert added["added"]["patterns"] == ["*.mp4", "*.mkv"]

    # Liệt kê
    assert main(["--config-dir", str(cfg), "routing", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed["routing_rules"]) == 1

    # Xóa theo tiền tố mã
    assert main(["--config-dir", str(cfg), "routing", "remove", rule_id[:6], "--json"]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed"]["id"] == rule_id

    assert main(["--config-dir", str(cfg), "routing", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["routing_rules"] == []


def test_cli_watch_list_add_remove(tmp_path: Path, capsys) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    load_or_create_identity(cfg)
    peer_cfg = tmp_path / "peer"
    peer_cfg.mkdir()
    peer_id = load_or_create_identity(peer_cfg)
    TrustStore.load(cfg).add(peer_cert_from_pem(peer_id.cert_pem()), "May-Dich")

    portal_src = tmp_path / "drop-folder"
    portal_src.mkdir()

    assert (
        main(
            [
                "--config-dir",
                str(cfg),
                "watch",
                "add",
                "--name",
                "Outbox Drop",
                "--dir",
                str(portal_src),
                "--to",
                "May-Dich",
                "--pattern",
                "*.zip",
                "--recursive",
                "--json",
            ]
        )
        == 0
    )
    added = json.loads(capsys.readouterr().out)
    portal_id = added["added"]["id"]
    assert added["added"]["device_id"] == peer_id.device_id
    assert added["added"]["recursive"] is True
    assert added["added"]["patterns"] == ["*.zip"]

    assert main(["--config-dir", str(cfg), "watch", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed["watch_portals"]) == 1

    assert main(["--config-dir", str(cfg), "watch", "remove", portal_id[:6], "--json"]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed"]["id"] == portal_id

    assert main(["--config-dir", str(cfg), "watch", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["watch_portals"] == []
