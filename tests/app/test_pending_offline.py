"""Kiểm thử `list_pending_offline` và `cancel_pending_offline` (`teleport.app.node`, PR 2.2)."""

from __future__ import annotations

import datetime
import os
import uuid
from pathlib import Path

import pytest
from support import wait_until

from teleport.app import storage, tombstones
from teleport.app.transfers import TransferProgress, TransferResult
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.resume_state import TombstoneRecord, encode_transfer_record
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore

pytestmark = [
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

LOCAL = "127.0.0.1"
MiB = 1024 * 1024


def _offline_api():
    from teleport.app.node import cancel_pending_offline, list_pending_offline

    return list_pending_offline, cancel_pending_offline


def _make_file(directory: Path, name: str, size: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(os.urandom(size))
    return path


def _setup_mixed_temps(cfg_dir: Path, recv_dir: Path, resume_helpers) -> dict[str, str]:
    """Tạo hỗn hợp các thư mục tạm RUNNING, DONE, ORPHAN, BROKEN, OTHER_NODE."""
    ident_recv = load_or_create_identity(cfg_dir)
    ident_send = load_or_create_identity(cfg_dir.parent / "sender_ident")
    ident_untrusted = load_or_create_identity(cfg_dir.parent / "untrusted_ident")
    ident_other_recv = load_or_create_identity(cfg_dir.parent / "other_recv_ident")

    TrustStore.load(cfg_dir).add(peer_cert_from_pem(ident_send.cert_pem()), "TrustedSender")

    tids = {
        "running_1": "aaaa1111" + uuid.uuid4().hex[8:],
        "running_2": "bbbb2222" + uuid.uuid4().hex[8:],
        "done": "cccc3333" + uuid.uuid4().hex[8:],
        "orphan_empty": "dddd4444" + uuid.uuid4().hex[8:],
        "orphan_untrusted": "eeee5555" + uuid.uuid4().hex[8:],
        "broken": "ffff6666" + uuid.uuid4().hex[8:],
        "other_node": "12347777" + uuid.uuid4().hex[8:],
    }

    recv_dir.mkdir(parents=True, exist_ok=True)
    rd = storage.ReceiveDir.open(recv_dir)
    try:
        for key in ("running_1", "running_2"):
            tid = tids[key]
            rec = resume_helpers.record_for(
                tid,
                [2 * BLOCK_SIZE],
                receiver=ident_recv.device_id,
                sender=ident_send.device_id,
            )
            temp = rd.create_temp(tid, rec)
            try:
                part = temp.create_part(0, 2 * BLOCK_SIZE)
                part.close()
            finally:
                rd.close_temp(temp)

        untrusted_rec = resume_helpers.record_for(
            tids["orphan_untrusted"],
            [BLOCK_SIZE],
            receiver=ident_recv.device_id,
            sender=ident_untrusted.device_id,
        )
        rd.close_temp(rd.create_temp(tids["orphan_untrusted"], untrusted_rec))

        other_rec = resume_helpers.record_for(
            tids["other_node"],
            [BLOCK_SIZE],
            receiver=ident_other_recv.device_id,
            sender=ident_send.device_id,
        )
        rd.close_temp(rd.create_temp(tids["other_node"], other_rec))
    finally:
        rd.close()

    # DONE (transfer.json có done_at)
    done_rec = resume_helpers.record_for(
        tids["done"],
        [BLOCK_SIZE],
        verified=1,
        receiver=ident_recv.device_id,
        sender=ident_send.device_id,
    )
    done_dir = recv_dir / ".teleport" / tids["done"]
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / "transfer.json").write_bytes(encode_transfer_record(done_rec))

    # ORPHAN (không có transfer.json) và BROKEN (transfer.json hỏng)
    (recv_dir / ".teleport" / tids["orphan_empty"]).mkdir(parents=True, exist_ok=True)
    broken_dir = recv_dir / ".teleport" / tids["broken"]
    broken_dir.mkdir(parents=True, exist_ok=True)
    (broken_dir / "transfer.json").write_bytes(b"{not valid json")

    tids["sender_id"] = ident_send.device_id
    return tids


def test_offline_does_not_create_config_dir_identity_or_teleport_dir(tmp_path: Path):
    """Liệt kê và hủy offline không tạo `config_dir`, file danh tính, hay `.teleport` mới."""
    list_pending_offline, cancel_pending_offline = _offline_api()
    missing_cfg = tmp_path / "missing_cfg"
    missing_recv = tmp_path / "missing_recv"

    assert list_pending_offline(missing_cfg, receive_dir=missing_recv) == []
    assert cancel_pending_offline(missing_cfg, cancel_all=True, receive_dir=missing_recv) == []
    assert not missing_cfg.exists()
    assert not missing_recv.exists()

    # config_dir tồn tại nhưng rỗng (chưa có danh tính)
    empty_cfg = tmp_path / "empty_cfg"
    empty_cfg.mkdir()
    recv_no_teleport = tmp_path / "recv_no_teleport"
    recv_no_teleport.mkdir()

    assert list_pending_offline(empty_cfg, receive_dir=recv_no_teleport) == []
    assert cancel_pending_offline(empty_cfg, cancel_all=True, receive_dir=recv_no_teleport) == []
    assert not (empty_cfg / "device.crt").exists()
    assert not (empty_cfg / "device.key").exists()
    assert not (empty_cfg / "settings.json").exists()
    assert not (recv_no_teleport / ".teleport").exists()

    # config_dir có danh tính nhưng receive_dir chưa có `.teleport`
    cfg_with_ident = tmp_path / "cfg_with_ident"
    load_or_create_identity(cfg_with_ident)
    assert list_pending_offline(cfg_with_ident, receive_dir=recv_no_teleport) == []
    assert cancel_pending_offline(cfg_with_ident, cancel_all=True, receive_dir=recv_no_teleport) == []
    assert not (recv_no_teleport / ".teleport").exists()


def test_list_pending_offline_only_returns_running_temps(tmp_path: Path, resume_helpers):
    """`list_pending_offline` chỉ trả về các lượt `TempKind.RUNNING`."""
    list_pending_offline, _ = _offline_api()
    cfg_dir = tmp_path / "cfg"
    recv_dir = tmp_path / "recv"
    tids = _setup_mixed_temps(cfg_dir, recv_dir, resume_helpers)

    pending = list_pending_offline(cfg_dir, receive_dir=recv_dir)
    found_ids = {p.transfer_id for p in pending}
    assert found_ids == {tids["running_1"], tids["running_2"]}


def test_cancel_pending_offline_filters_running_records_tombstone_and_dedupes(tmp_path: Path, resume_helpers):
    """Hủy offline chỉ xóa `RUNNING`, ghi bia mộ `(receive, user, unsent=True)` và khử trùng."""
    _, cancel_pending_offline = _offline_api()
    cfg_dir = tmp_path / "cfg"
    recv_dir = tmp_path / "recv"
    tids = _setup_mixed_temps(cfg_dir, recv_dir, resume_helpers)

    old_at = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
    old_tid = "0000aaaa" + uuid.uuid4().hex[8:]
    old_record = TombstoneRecord(
        device_id=tids["sender_id"],
        direction="receive",
        transfer_id=old_tid,
        reason="user",
        unsent=False,
        at=old_at,
    )
    # Kèm thêm một bản ghi sẵn cho running_1 để kiểm tra khử trùng
    dup_record = TombstoneRecord(
        device_id=tids["sender_id"],
        direction="receive",
        transfer_id=tids["running_1"],
        reason="expired",
        unsent=False,
        at=old_at,
    )
    tombstones.save(cfg_dir, [old_record, dup_record])

    canceled = cancel_pending_offline(cfg_dir, query=tids["running_1"][:8], receive_dir=recv_dir)
    assert [p.transfer_id for p in canceled] == [tids["running_1"]]

    # Thư mục tạm running_1 bị xóa; các thư mục khác (running_2, done, orphan, broken, other_node) còn nguyên
    assert not (recv_dir / ".teleport" / tids["running_1"]).exists()
    for keep_key in ("running_2", "done", "orphan_empty", "orphan_untrusted", "broken", "other_node"):
        assert (recv_dir / ".teleport" / tids[keep_key]).exists(), f"{keep_key} không được phép bị xóa"

    saved = tombstones.load(cfg_dir)
    by_tid = [r for r in saved if r.transfer_id == tids["running_1"]]
    assert len(by_tid) == 1, "Bia mộ bị trùng sau khi hủy offline"
    rec = by_tid[0]
    assert rec.direction == "receive"
    assert rec.reason == "user"
    assert rec.unsent is True
    assert rec.device_id == tids["sender_id"]

    # Bia mộ cũ không bị mất
    assert any(r.transfer_id == old_tid for r in saved)

    # Hủy nốt lượt RUNNING còn lại bằng cancel_all=True
    canceled_rest = cancel_pending_offline(cfg_dir, cancel_all=True, receive_dir=recv_dir)
    assert [p.transfer_id for p in canceled_rest] == [tids["running_2"]]
    assert not (recv_dir / ".teleport" / tids["running_2"]).exists()
    for keep_key in ("done", "orphan_empty", "orphan_untrusted", "broken", "other_node"):
        assert (recv_dir / ".teleport" / tids[keep_key]).exists()


@pytest.mark.integration
async def test_offline_cancel_then_restart_sends_cancel_user_and_frees_promised_bytes(
    transfer_nodes, node_factory, tmp_path: Path, monkeypatch
):
    """Sau khi hủy offline và khởi động lại: máy gửi nhận `cancel{user}`, `promised_bytes` được giải phóng."""
    net = await transfer_nodes(
        receiver_opts={"receive_dir": tmp_path / "nhan-A", "auto_accept": True},
        sender_opts={"rate_limit": 256 * 1024},
    )
    file_1 = _make_file(net.src, "big1.bin", 2 * MiB)
    key_1 = await net.B.transfers.send(net.A.id, [file_1])

    await wait_until(
        lambda: len(net.A.trec.of(TransferProgress, key_1.transfer_id)) >= 1,
        15,
        "A chưa bắt đầu nhận lượt 1",
    )

    # Tắt A có trật tự để tạo lượt nhận dở trên đĩa
    await net.A.node.close()
    assert (net.receive_dir / ".teleport" / key_1.transfer_id).is_dir()

    # Hủy offline trên thư mục cấu hình của A
    _, cancel_pending_offline = _offline_api()
    canceled = cancel_pending_offline(net.A.dir, cancel_all=True, receive_dir=net.receive_dir)
    assert [p.transfer_id for p in canceled] == [key_1.transfer_id]
    assert not (net.receive_dir / ".teleport" / key_1.transfer_id).exists()

    # Ép dung lượng trống chỉ đủ cho file mới (1 MiB), không đủ nếu lượt cũ (2 MiB) vẫn giữ chỗ
    real_volume_info = storage.volume_info

    def tight_volume_info(path: Path) -> storage.VolumeInfo:
        info = real_volume_info(path)
        return storage.VolumeInfo(
            fs_type=info.fs_type,
            max_file_size=info.max_file_size,
            free_bytes=1 * MiB + 128 * 1024,
        )

    monkeypatch.setattr(storage, "volume_info", tight_volume_info)

    # Khởi động lại A trên cùng thư mục cấu hình và nối lại từ B
    a2 = await node_factory("A", node_kwargs={"receive_dir": net.receive_dir, "auto_accept": True})
    try:
        assert a2.node._transfers.promised_bytes() == 0
        net.B.connect_hint(a2.id, [LOCAL], a2.port)

        fin_b = await net.B.trec.wait_finished(key_1.transfer_id, timeout=15)
        assert fin_b.result == TransferResult.CANCELLED_USER
        assert fin_b.reason == "user"
        assert fin_b.by_peer is True

        # Lượt mới 1 MiB phải được chấp nhận và hoàn tất vì promised_bytes của lượt cũ đã giải phóng
        file_2 = _make_file(net.src, "new_ok.bin", 1 * MiB)
        key_2 = await net.B.transfers.send(a2.id, [file_2])
        fin_2 = await net.B.trec.wait_finished(key_2.transfer_id, timeout=15)
        assert fin_2.result == TransferResult.DONE
    finally:
        await a2.node.close()
