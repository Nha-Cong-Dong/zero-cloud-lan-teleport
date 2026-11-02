"""Nhớ trạng thái cửa sổ ở `Node` (`specs/spec-f1.md` D4, D5, R2).

Viết từ spec, không từ code.

- D5: `set_ui_preferences(window=, last_conversation=, tray_hint_shown=)` trả `bool`: True khi đã ghi
  file; bản mới (`replace` rồi `.pruned(trusted)`) bằng bản đang có → False, **không ghi** (cho mọi
  trường, kể cả trường cũ; so sánh bỏ qua `extra`, `load_failed`). `last_conversation` của máy không tin
  cậy bị `pruned` bỏ, không lỗi. Sai luật → ValueError/TypeError, không ghi.
- D4: `Node.unpair` và `unpair_offline` ghi khi file có mục `devices` của máy đó **hoặc**
  `last_conversation` là máy đó; lỗi ghi chỉ WARNING.

“Không ghi” được kiểm bằng một spy trên `Settings.save` (đếm số lần gọi) và bằng byte của file.

Node thật trên 127.0.0.1 (`node_factory`) → `integration`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from teleport.app import node as node_mod
from teleport.app import settings as settings_mod

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

STRANGER = "5" * 32  # máy không tin cậy


def wg(**kw):
    values = {"x": 120, "y": 90, "width": 1100, "height": 720}
    values.update(kw)
    return settings_mod.WindowGeometry(**values)


def write_settings(config_dir: Path, data: dict) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def on_disk(config_dir: Path) -> dict:
    path = config_dir / "settings.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def raw(config_dir: Path) -> bytes | None:
    path = config_dir / "settings.json"
    return path.read_bytes() if path.exists() else None


@pytest.fixture
def save_spy(monkeypatch) -> SimpleNamespace:
    """Đếm `Settings.save`; `state.fail = True` → ném OSError (không ghi)."""
    real = settings_mod.Settings.save
    state = SimpleNamespace(calls=0, fail=False)

    def save(self, config_dir):
        state.calls += 1
        if state.fail:
            raise OSError(28, "giả lập không ghi được settings.json")
        return real(self, config_dir)

    monkeypatch.setattr(settings_mod.Settings, "save", save)
    return state


@pytest.fixture
async def node_with_peers(node_factory, trust_each_other):
    """Node A đang chạy, tin B và C (B, C không chạy). Trả (A, id_b, id_c)."""

    async def make(**kw):
        _, id_b = trust_each_other("A", "B")
        _, id_c = trust_each_other("A", "C", name_b="C")
        A = await node_factory("A", **kw)
        return A, id_b.device_id, id_c.device_id

    return make


# =================================================================================================
# D5: ba tham số mới, trả bool
# =================================================================================================


async def test_set_new_fields_returns_true_and_writes(node_with_peers, save_spy):
    A, b, _ = await node_with_peers()
    g = wg(maximized=True, screen="DP-1", dpr=1.25)
    assert A.set_ui_preferences(window=g, last_conversation=b, tray_hint_shown=True) is True
    assert save_spy.calls == 1
    s = A.settings
    assert (s.window, s.last_conversation, s.tray_hint_shown) == (g, b, True)
    data = on_disk(A.dir)
    assert data["window"] == {
        "x": 120,
        "y": 90,
        "width": 1100,
        "height": 720,
        "maximized": True,
        "screen": "DP-1",
        "dpr": 1.25,
    }
    assert data["last_conversation"] == b
    assert data["tray_hint_shown"] is True


async def test_same_values_again_returns_false_and_does_not_write(node_with_peers, save_spy):
    A, b, _ = await node_with_peers()
    g = wg()
    assert A.set_ui_preferences(window=g, last_conversation=b, tray_hint_shown=True) is True
    before = raw(A.dir)
    calls = save_spy.calls
    # Bản mới bằng bản cũ (một `WindowGeometry` khác đối tượng nhưng bằng giá trị).
    assert A.set_ui_preferences(window=wg(), last_conversation=b, tray_hint_shown=True) is False
    assert A.set_ui_preferences(window=g) is False
    assert A.set_ui_preferences(tray_hint_shown=True) is False
    assert save_spy.calls == calls, "không đổi thì không được gọi Settings.save"
    assert raw(A.dir) == before


async def test_no_arguments_returns_false_and_does_not_write(node_with_peers, save_spy):
    A, _, _ = await node_with_peers()
    calls = save_spy.calls
    assert A.set_ui_preferences() is False
    assert save_spy.calls == calls


async def test_unchanged_old_fields_return_false_without_write(node_with_peers, save_spy):
    """Mặc định 6: bỏ qua lần ghi không đổi cho mọi trường, kể cả trường cũ."""
    A, _, _ = await node_with_peers()
    assert A.set_ui_preferences(theme="dark", accent="ocean", language="vi") is True
    calls = save_spy.calls
    before = raw(A.dir)
    assert A.set_ui_preferences(theme="dark") is False
    assert A.set_ui_preferences(accent="ocean", language="vi") is False
    assert A.set_ui_preferences(autostart=False, first_run_done=False, radar_view=None) is False
    assert save_spy.calls == calls
    assert raw(A.dir) == before


async def test_one_changed_field_among_unchanged_ones_writes(node_with_peers, save_spy):
    A, b, c = await node_with_peers()
    A.set_ui_preferences(theme="dark", last_conversation=b)
    calls = save_spy.calls
    assert A.set_ui_preferences(theme="dark", last_conversation=c) is True
    assert save_spy.calls == calls + 1
    assert on_disk(A.dir)["last_conversation"] == c


async def test_comparison_ignores_extra(node_with_peers, save_spy, tmp_path):
    """Khóa lạ trong file (`extra`) không làm “khác”: giá trị như cũ → không ghi, khóa lạ vẫn còn."""
    write_settings(tmp_path / "A", {"version": 1, "theme": "dark", "tuong_lai": {"giu": 1}})
    A, _, _ = await node_with_peers()
    before = raw(A.dir)
    calls = save_spy.calls
    assert A.set_ui_preferences(theme="dark") is False
    assert save_spy.calls == calls
    assert raw(A.dir) == before
    assert A.set_ui_preferences(tray_hint_shown=True) is True
    data = on_disk(A.dir)
    assert data["tuong_lai"] == {"giu": 1} and data["tray_hint_shown"] is True


async def test_clearing_window_and_last_conversation_with_none(node_with_peers):
    A, b, _ = await node_with_peers()
    A.set_ui_preferences(window=wg(), last_conversation=b)
    assert A.set_ui_preferences(window=None) is True
    data = on_disk(A.dir)
    assert "window" not in data and data["last_conversation"] == b
    assert A.set_ui_preferences(last_conversation=None) is True
    assert "last_conversation" not in on_disk(A.dir)
    assert (A.settings.window, A.settings.last_conversation) == (None, None)
    assert A.set_ui_preferences(window=None, last_conversation=None) is False


async def test_tray_hint_shown_false_after_true_writes_and_removes_key(node_with_peers):
    A, _, _ = await node_with_peers()
    A.set_ui_preferences(tray_hint_shown=True)
    assert A.set_ui_preferences(tray_hint_shown=False) is True
    assert "tray_hint_shown" not in on_disk(A.dir)


async def test_untrusted_last_conversation_is_pruned_silently(node_with_peers, save_spy):
    """D5: máy không tin cậy → `pruned` bỏ, không lỗi; không còn gì đổi → False, không ghi."""
    A, _, _ = await node_with_peers()
    calls = save_spy.calls
    assert A.set_ui_preferences(last_conversation=STRANGER) is False
    assert save_spy.calls == calls
    assert A.settings.last_conversation is None
    assert "last_conversation" not in on_disk(A.dir)


async def test_untrusted_last_conversation_with_other_change_writes_without_it(node_with_peers):
    A, _, _ = await node_with_peers()
    assert A.set_ui_preferences(last_conversation=STRANGER, window=wg()) is True
    data = on_disk(A.dir)
    assert "last_conversation" not in data and data["window"]["width"] == 1100
    assert A.settings.last_conversation is None


async def test_untrusted_last_conversation_replaces_trusted_one_clears_it(node_with_peers):
    """Truyền một máy không tin cậy là “đổi” sang None sau `pruned`: bản cũ (máy tin cậy) bị bỏ."""
    A, b, _ = await node_with_peers()
    A.set_ui_preferences(last_conversation=b)
    assert A.set_ui_preferences(last_conversation=STRANGER) is True
    assert A.settings.last_conversation is None
    assert "last_conversation" not in on_disk(A.dir)


async def test_saved_last_conversation_of_untrusted_ignored_and_pruned_on_write(node_with_peers, tmp_path):
    write_settings(tmp_path / "A", {"version": 1, "last_conversation": STRANGER})
    A, _, _ = await node_with_peers()
    assert A.set_ui_preferences(theme="light") is True
    assert "last_conversation" not in on_disk(A.dir)
    assert A.settings.last_conversation is None


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"window": {"x": 1, "y": 2, "width": 3, "height": 4}}, TypeError),
        ({"window": (1, 2, 3, 4)}, TypeError),
        ({"last_conversation": "A" * 32}, ValueError),
        ({"last_conversation": "a" * 31}, ValueError),
        ({"last_conversation": ""}, ValueError),
        ({"tray_hint_shown": None}, TypeError),
        ({"tray_hint_shown": 1}, TypeError),
        ({"tray_hint_shown": True, "theme": "system"}, ValueError),  # một sai thì không lưu gì
        ({"window": "x", "theme": "dark"}, TypeError),
    ],
    ids=lambda v: repr(v)[:30],
)
async def test_invalid_new_fields_raise_and_write_nothing(node_with_peers, save_spy, kwargs, error):
    A, _, _ = await node_with_peers()
    A.set_ui_preferences(theme="light")
    before_disk = raw(A.dir)
    before = A.settings
    calls = save_spy.calls
    with pytest.raises(error):
        A.set_ui_preferences(**kwargs)
    assert raw(A.dir) == before_disk
    assert A.settings == before
    assert A.settings.tray_hint_shown is False and A.settings.window is None
    assert save_spy.calls == calls


async def test_save_error_raises_and_changes_nothing(node_with_peers, save_spy):
    A, _, _ = await node_with_peers()
    save_spy.fail = True
    with pytest.raises(node_mod.SettingsSaveError):
        A.set_ui_preferences(window=wg(), tray_hint_shown=True)
    assert (A.settings.window, A.settings.tray_hint_shown) == (None, False)
    save_spy.fail = False
    assert A.set_ui_preferences(window=wg(), tray_hint_shown=True) is True
    assert A.settings.window == wg()


async def test_new_fields_survive_restart(node_factory, trust_each_other, tmp_path):
    _, id_b = trust_each_other("A", "B")
    A = await node_factory("A")
    g = wg(maximized=True, screen="HDMI-1", dpr=2.0)
    A.set_ui_preferences(window=g, last_conversation=id_b.device_id, tray_hint_shown=True)
    await A.close()
    s = settings_mod.Settings.load(tmp_path / "A")
    assert (s.window, s.last_conversation, s.tray_hint_shown) == (g, id_b.device_id, True)


# =================================================================================================
# D4: hủy ghép đôi bỏ `last_conversation` của máy đó
# =================================================================================================


async def test_unpair_removes_last_conversation_of_that_device(node_with_peers):
    A, b, _ = await node_with_peers()
    A.set_ui_preferences(last_conversation=b, window=wg(), tray_hint_shown=True)
    assert on_disk(A.dir)["last_conversation"] == b
    await A.unpair(b)
    data = on_disk(A.dir)
    assert "last_conversation" not in data
    assert data["window"]["width"] == 1100 and data["tray_hint_shown"] is True
    assert A.settings.last_conversation is None


async def test_unpair_other_device_keeps_last_conversation(node_with_peers, save_spy):
    """Máy bị hủy không có mục `devices` và không phải `last_conversation` → không ghi gì."""
    A, b, c = await node_with_peers()
    A.set_ui_preferences(last_conversation=b)
    before = raw(A.dir)
    calls = save_spy.calls
    await A.unpair(c)
    assert on_disk(A.dir)["last_conversation"] == b
    assert A.settings.last_conversation == b
    assert save_spy.calls == calls
    assert raw(A.dir) == before


async def test_unpair_removes_both_device_entry_and_last_conversation(node_with_peers):
    A, b, _ = await node_with_peers()
    A.set_ask_before_receive(b, False)
    A.set_ui_preferences(last_conversation=b)
    await A.unpair(b)
    data = on_disk(A.dir)
    assert b not in data.get("devices", {})
    assert "last_conversation" not in data


async def test_unpair_write_failure_warns_and_still_unpairs(node_with_peers, save_spy, caplog):
    A, b, _ = await node_with_peers()
    A.set_ui_preferences(last_conversation=b)
    save_spy.fail = True
    caplog.set_level(logging.WARNING)
    device = await A.unpair(b)
    assert device.device_id == b
    assert A.store.get(b) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    save_spy.fail = False
    A.set_ui_preferences(theme="dark")
    assert "last_conversation" not in on_disk(A.dir), "lần ghi sau phải bỏ (prune)"


async def test_unpair_offline_removes_last_conversation(trust_each_other, tmp_path):
    _, id_b = trust_each_other("A", "B")
    write_settings(
        tmp_path / "A",
        {
            "version": 1,
            "name": "Giu-Ten",
            "last_conversation": id_b.device_id,
            "window": {"x": 1, "y": 2, "width": 300, "height": 400, "maximized": False},
            "tray_hint_shown": True,
        },
    )
    device = node_mod.unpair_offline(tmp_path / "A", "B", lock_timeout=2.0)
    assert device.device_id == id_b.device_id
    data = on_disk(tmp_path / "A")
    assert "last_conversation" not in data
    assert data["name"] == "Giu-Ten" and data["tray_hint_shown"] is True
    assert data["window"] == {"x": 1, "y": 2, "width": 300, "height": 400, "maximized": False}


async def test_unpair_offline_other_device_keeps_file_untouched(trust_each_other, tmp_path, save_spy):
    _, id_b = trust_each_other("A", "B")
    trust_each_other("A", "C", name_b="C")
    write_settings(tmp_path / "A", {"version": 1, "last_conversation": id_b.device_id})
    before = raw(tmp_path / "A")
    node_mod.unpair_offline(tmp_path / "A", "C", lock_timeout=2.0)
    assert raw(tmp_path / "A") == before
    assert save_spy.calls == 0


async def test_unpair_offline_last_conversation_write_failure_warns(
    trust_each_other, tmp_path, save_spy, caplog
):
    from teleport.trust.store import TrustStore

    caplog.set_level(logging.WARNING)
    _, id_b = trust_each_other("A", "B")
    write_settings(tmp_path / "A", {"version": 1, "last_conversation": id_b.device_id})
    save_spy.fail = True
    node_mod.unpair_offline(tmp_path / "A", "B", lock_timeout=2.0)
    assert save_spy.calls == 1
    assert TrustStore.load(tmp_path / "A").get(id_b.device_id) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)
