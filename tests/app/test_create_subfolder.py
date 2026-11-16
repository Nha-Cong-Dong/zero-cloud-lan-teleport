import datetime
import json
from pathlib import Path

import pytest

from teleport.app.settings import SETTINGS_FILE, Settings
from teleport.app.storage import ReceiveDir
from teleport.app.transfers import make_transfer_subfolder_name
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.messages import FileEntry, Offer
from teleport.core.resume_state import TransferRecord


def test_make_transfer_subfolder_name():
    dt = datetime.datetime(2026, 11, 16, 10, 52)
    name = make_transfer_subfolder_name("Redmi Note 13", timestamp=dt)
    assert name == "Redmi Note 13 (2026-11-16 10-52)"

    # Tên thiết bị None hoặc rỗng
    name_none = make_transfer_subfolder_name(None, timestamp=dt)
    assert name_none == "Thiết bị (2026-11-16 10-52)"

    name_empty = make_transfer_subfolder_name("   ", timestamp=dt)
    assert name_empty == "Thiết bị (2026-11-16 10-52)"

    # Ký tự cấm trên Windows
    name_invalid = make_transfer_subfolder_name('May:Tung/PC*Test?"<>|', timestamp=dt)
    assert "/" not in name_invalid
    assert "\\" not in name_invalid
    assert ":" not in name_invalid
    assert "*" not in name_invalid
    assert "?" not in name_invalid
    assert "<" not in name_invalid
    assert ">" not in name_invalid
    assert "|" not in name_invalid
    assert "2026-11-16 10-52" in name_invalid


def test_settings_create_subfolder(tmp_path: Path):
    s = Settings()
    assert s.create_subfolder is True
    s.save(tmp_path)
    content = json.loads((tmp_path / SETTINGS_FILE).read_text(encoding="utf-8"))
    assert "create_subfolder" not in content

    s2 = Settings(create_subfolder=False)
    assert s2.create_subfolder is False
    s2.save(tmp_path)
    content2 = json.loads((tmp_path / SETTINGS_FILE).read_text(encoding="utf-8"))
    assert content2["create_subfolder"] is False

    loaded = Settings.load(tmp_path)
    assert loaded.create_subfolder is False


@pytest.mark.asyncio
async def test_node_create_subfolder_setting(node_factory):
    n = await node_factory("TestSubfolder")
    assert n.create_subfolder is True
    assert n.transfers.create_subfolder is True

    n.set_create_subfolder(False)
    assert n.create_subfolder is False
    assert n.transfers.create_subfolder is False

    loaded = Settings.load(n.dir)
    assert loaded.create_subfolder is False

    n.set_create_subfolder(True)
    assert n.create_subfolder is True
    assert n.transfers.create_subfolder is True
    await n.close()


def test_finalize_into_subfolder(tmp_path: Path):
    recv_root = tmp_path / "received"
    recv_root.mkdir()

    dt = datetime.datetime(2026, 11, 16, 10, 52)
    subfolder = make_transfer_subfolder_name("Redmi Note 13", timestamp=dt)
    assert subfolder == "Redmi Note 13 (2026-11-16 10-52)"

    offer_files = (FileEntry(name="photo.jpg", size=10, mtime=1000),)
    t_id = "1" * 32
    offer = Offer(transfer_id=t_id, files=offer_files, block_size=BLOCK_SIZE)
    record = TransferRecord(receiver="a" * 32, sender="b" * 32, offer=offer, verified=0, done_at=None)

    with ReceiveDir.open(recv_root) as rd:
        temp = rd.create_temp(t_id, record)
        part0 = temp.create_part(0, 10)
        part0.write_at(0, b"0123456789")

        final_file = rd.finalize(part0, "photo.jpg", rel_path=f"{subfolder}/photo.jpg")
        assert final_file == recv_root / subfolder / "photo.jpg"
        assert final_file.is_file()
        assert final_file.read_bytes() == b"0123456789"
