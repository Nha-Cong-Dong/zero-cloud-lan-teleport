"""`Node`: lỗi đọc `settings.json` lúc `start` không làm mất cài đặt khi setter ghi lại (soát code C1, mục 2).

`Settings.load` gặp OSError (không phải "chưa có file") trả mặc định có `load_failed`; lần ghi đầu của
node chép file cũ sang `.bak` trước, các lần ghi sau không chép nữa (file lúc đó là của node).
"""

from __future__ import annotations

import json

import pytest

from teleport.app import settings as settings_mod

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_leaks")]


async def test_read_error_at_start_backs_up_before_first_write_only(node_factory, tmp_path, monkeypatch):
    d = tmp_path / "A"
    d.mkdir()
    raw = json.dumps({"version": 1, "name": "PC-Nha", "rate_limit": 1048576}).encode("utf-8")
    (d / settings_mod.SETTINGS_FILE).write_bytes(raw)
    real = settings_mod._read_limited

    def boom(path):
        raise PermissionError(13, "giả lập file đang bị khóa")

    monkeypatch.setattr(settings_mod, "_read_limited", boom)
    A = await node_factory("A", port=0)
    monkeypatch.setattr(settings_mod, "_read_limited", real)
    assert A.settings.name is None

    A.set_ui_preferences(language="vi")
    assert (d / settings_mod.BACKUP_FILE).read_bytes() == raw, "file cũ phải được giữ ở .bak"
    assert not A.settings.load_failed

    A.set_ui_preferences(autostart=True)
    assert (d / settings_mod.BACKUP_FILE).read_bytes() == raw, "lần ghi sau không được đè .bak"
    saved = json.loads((d / settings_mod.SETTINGS_FILE).read_text(encoding="utf-8"))
    assert saved == {"version": 1, "language": "vi", "autostart": True}
