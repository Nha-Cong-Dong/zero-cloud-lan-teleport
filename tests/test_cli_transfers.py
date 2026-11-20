"""Kiểm thử lệnh CLI `teleport transfers list` và `teleport transfers cancel` (PR 2.2)."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from teleport.app import storage, tombstones
from teleport.app.ipc import IpcProtocolError
from teleport.app.node import Node
from teleport.cli import main
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.messages import FileEntry, Offer
from teleport.core.resume_state import TransferRecord
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore

pytestmark = [
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]


def _seed_offline_running_temp(cfg_dir: Path, recv_dir: Path, tid: str) -> tuple[str, str]:
    """Tạo một lượt nhận dở hợp lệ (`TempKind.RUNNING`) không dùng fixture `tests/app`."""
    ident_recv = load_or_create_identity(cfg_dir)
    ident_send = load_or_create_identity(cfg_dir.parent / f"sender_{tid[:8]}")
    TrustStore.load(cfg_dir).add(peer_cert_from_pem(ident_send.cert_pem()), "Sender")

    offer = Offer(
        transfer_id=tid,
        block_size=BLOCK_SIZE,
        files=(FileEntry("sample.bin", 2 * BLOCK_SIZE, 1_700_000_000_000),),
    )
    rec = TransferRecord(
        receiver=ident_recv.device_id,
        sender=ident_send.device_id,
        offer=offer,
        verified=0,
        done_at=None,
    )
    recv_dir.mkdir(parents=True, exist_ok=True)
    rd = storage.ReceiveDir.open(recv_dir)
    try:
        rd.close_temp(rd.create_temp(tid, rec))
    finally:
        rd.close()
    return ident_recv.device_id, ident_send.device_id


def _exit_code_of_main(argv: list[str]) -> int:
    try:
        return main(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1


# -------------------------------------------------------------------------------------------------
# Kiểm tra tham số CLI (mã lỗi 2)
# -------------------------------------------------------------------------------------------------


def test_cli_transfers_cancel_requires_id_or_all(tmp_path: Path):
    """Thiếu cả `<id>` lẫn `--all` trả về mã lỗi `2`."""
    code = _exit_code_of_main(["--config-dir", str(tmp_path), "transfers", "cancel"])
    assert code == 2


def test_cli_transfers_cancel_rejects_both_id_and_all(tmp_path: Path):
    """Truyền cả `<id>` và `--all` trả về mã lỗi `2`."""
    code = _exit_code_of_main(["--config-dir", str(tmp_path), "transfers", "cancel", "abcd1234", "--all"])
    assert code == 2


@pytest.mark.parametrize("bad_id", ["", "a", "ab", "abc", "zzzz", "12g4"])
def test_cli_transfers_cancel_rejects_short_or_non_hex_id(tmp_path: Path, bad_id: str):
    """`<id>` ngắn hơn 4 ký tự hex hoặc không phải hex trả về mã lỗi `2`."""
    code = _exit_code_of_main(["--config-dir", str(tmp_path), "transfers", "cancel", bad_id])
    assert code == 2


def test_cli_transfers_list_rejects_empty_dir(tmp_path: Path):
    """`teleport transfers list --dir ""` trả về mã lỗi cú pháp `2`."""
    code = _exit_code_of_main(["--config-dir", str(tmp_path), "transfers", "list", "--dir", ""])
    assert code == 2


def test_cli_transfers_cancel_rejects_empty_dir(tmp_path: Path):
    """`teleport transfers cancel --all --dir ""` trả về mã lỗi cú pháp `2`."""
    code = _exit_code_of_main(["--config-dir", str(tmp_path), "transfers", "cancel", "--all", "--dir", ""])
    assert code == 2


# -------------------------------------------------------------------------------------------------
# Chế độ offline (node tắt)
# -------------------------------------------------------------------------------------------------


def test_cli_transfers_list_offline_text_and_json(tmp_path: Path, capsys):
    """`teleport transfers list` offline hoạt động cả text và `--json` hợp lệ."""
    cfg_dir = tmp_path / "cfg"
    recv_dir = tmp_path / "recv"
    tid = "deadbeef" + uuid.uuid4().hex[8:]
    _seed_offline_running_temp(cfg_dir, recv_dir, tid)

    code_text = main(["--config-dir", str(cfg_dir), "transfers", "list", "--dir", str(recv_dir)])
    assert code_text == 0
    out_text = capsys.readouterr().out
    assert tid[:8] in out_text or tid in out_text

    code_json = main(["--config-dir", str(cfg_dir), "transfers", "list", "--dir", str(recv_dir), "--json"])
    assert code_json == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    items = payload.get("transfers", [])
    assert len(items) == 1
    assert items[0]["transfer_id"] == tid


def test_cli_transfers_cancel_all_when_empty_exits_0_offline(tmp_path: Path, capsys):
    """`teleport transfers cancel --all` khi không có lượt nào trả mã `0` (text và `--json`)."""
    cfg_dir = tmp_path / "cfg"
    recv_dir = tmp_path / "recv"
    recv_dir.mkdir()

    assert main(["--config-dir", str(cfg_dir), "transfers", "cancel", "--all", "--dir", str(recv_dir)]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--config-dir",
                str(cfg_dir),
                "transfers",
                "cancel",
                "--all",
                "--dir",
                str(recv_dir),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    assert not cfg_dir.exists()


def test_cli_transfers_cancel_by_prefix_offline_json(tmp_path: Path, capsys):
    """`teleport transfers cancel <id> --dir ... --json` offline xóa tạm và ghi bia mộ."""
    cfg_dir = tmp_path / "cfg"
    recv_dir = tmp_path / "recv"
    tid = "1234abcd" + uuid.uuid4().hex[8:]
    _seed_offline_running_temp(cfg_dir, recv_dir, tid)

    code = main(
        [
            "--config-dir",
            str(cfg_dir),
            "transfers",
            "cancel",
            tid[:6],
            "--dir",
            str(recv_dir),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    assert not (recv_dir / ".teleport" / tid).exists()

    saved = tombstones.load(cfg_dir)
    assert any(
        r.transfer_id == tid and r.direction == "receive" and r.reason == "user" and r.unsent is True
        for r in saved
    )


def test_cli_transfers_cancel_missing_id_offline_fails_cleanly(tmp_path: Path, capsys):
    """Hủy `<id>` không tồn tại trả về mã lỗi khác 0 và không in traceback."""
    cfg_dir = tmp_path / "cfg"
    recv_dir = tmp_path / "recv"
    load_or_create_identity(cfg_dir)
    recv_dir.mkdir()

    code = main(["--config-dir", str(cfg_dir), "transfers", "cancel", "9999aaaa", "--dir", str(recv_dir)])
    assert code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err


# -------------------------------------------------------------------------------------------------
# Chế độ IPC (daemon đang chạy)
# -------------------------------------------------------------------------------------------------


@pytest.mark.integration
async def test_cli_transfers_ipc_list_cancel_and_dir_mismatch(tmp_path: Path, capsys):
    """Chế độ IPC: liệt kê, hủy, `--all` khi rỗng, `--json` hợp lệ và báo lỗi `2` khi `--dir` khác."""
    cfg_dir = tmp_path / "cfg"
    recv_dir = tmp_path / "recv"
    other_dir = tmp_path / "other_recv"
    other_dir.mkdir()

    tid = "faceb00c" + uuid.uuid4().hex[8:]
    _seed_offline_running_temp(cfg_dir, recv_dir, tid)

    node = Node(
        cfg_dir,
        name="DaemonNode",
        host="127.0.0.1",
        port=0,
        accept_pairing=False,
        auto_connect=False,
        receive_dir=recv_dir,
        auto_accept=True,
        enable_ipc=True,
    )
    await node.start()
    try:
        # 1. --dir khác receive_dir của daemon -> mã lỗi 2
        code_bad_list = await asyncio.to_thread(
            _exit_code_of_main,
            ["--config-dir", str(cfg_dir), "transfers", "list", "--dir", str(other_dir)],
        )
        assert code_bad_list == 2
        capsys.readouterr()

        code_bad_cancel = await asyncio.to_thread(
            _exit_code_of_main,
            ["--config-dir", str(cfg_dir), "transfers", "cancel", "--all", "--dir", str(other_dir)],
        )
        assert code_bad_cancel == 2
        capsys.readouterr()

        # 2. `transfers list --dir <recv_dir> --json` (khớp receive_dir) -> 0 & JSON hợp lệ
        code_list = await asyncio.to_thread(
            main,
            ["--config-dir", str(cfg_dir), "transfers", "list", "--dir", str(recv_dir), "--json"],
        )
        assert code_list == 0
        list_data = json.loads(capsys.readouterr().out)
        assert any(t["transfer_id"] == tid for t in list_data.get("transfers", []))

        # 3. `transfers cancel <id> --json` -> 0, xóa tạm, promised_bytes == 0
        code_cancel = await asyncio.to_thread(
            main,
            ["--config-dir", str(cfg_dir), "transfers", "cancel", tid[:8], "--json"],
        )
        assert code_cancel == 0
        cancel_data = json.loads(capsys.readouterr().out)
        assert isinstance(cancel_data, dict)
        assert node._transfers.promised_bytes() == 0
        assert not (recv_dir / ".teleport" / tid).exists()

        # 4. `transfers cancel --all` khi không còn lượt nào -> 0
        code_empty_all = await asyncio.to_thread(
            main,
            ["--config-dir", str(cfg_dir), "transfers", "cancel", "--all", "--json"],
        )
        assert code_empty_all == 0
        empty_data = json.loads(capsys.readouterr().out)
        assert isinstance(empty_data, dict)
    finally:
        await node.close()


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: IpcProtocolError("Server dong ket noi bat ngo"),
        lambda: TimeoutError("IPC timeout"),
    ],
)
def test_cli_transfers_list_handles_ipc_disconnect_and_timeout_cleanly(
    tmp_path: Path, monkeypatch, capsys, exc_factory
):
    """`cmd_transfers_list` trả mã `1` và không in traceback khi IPC ngắt kết nối hoặc `TimeoutError`."""

    class _FailingIpcClient:
        async def list_transfers(self):
            raise exc_factory()

        async def close(self) -> None:
            return None

    async def _fake_try_connect_ipc(_cfg_dir):
        return _FailingIpcClient()

    monkeypatch.setattr("teleport.cli.try_connect_ipc", _fake_try_connect_ipc)

    code = main(["--config-dir", str(tmp_path), "transfers", "list"])
    assert code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert captured.err.strip()


@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: IpcProtocolError("Server dong ket noi bat ngo"),
        lambda: TimeoutError("IPC timeout"),
    ],
)
def test_cli_transfers_cancel_handles_ipc_disconnect_and_timeout_cleanly(
    tmp_path: Path, monkeypatch, capsys, exc_factory
):
    """`cmd_transfers_cancel` trả mã `1` và không in traceback khi IPC ngắt kết nối hoặc `TimeoutError`."""

    class _FailingIpcClient:
        async def list_transfers(self):
            raise exc_factory()

        async def cancel_transfer(self, target=None, *, cancel_all: bool = False):
            raise exc_factory()

        async def close(self) -> None:
            return None

    async def _fake_try_connect_ipc(_cfg_dir):
        return _FailingIpcClient()

    monkeypatch.setattr("teleport.cli.try_connect_ipc", _fake_try_connect_ipc)

    code = main(["--config-dir", str(tmp_path), "transfers", "cancel", "--all"])
    assert code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert captured.err.strip()
