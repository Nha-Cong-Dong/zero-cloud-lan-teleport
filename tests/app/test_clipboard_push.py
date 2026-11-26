"""Kiểm thử gửi clipboard thủ công `ClipboardSync.push_now` (`teleport.app.clipboard`)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from support import wait_until

from teleport.app import clipboard as app_clip
from teleport.app.clipboard import ClipPushResult, FakeClipboard, ReadStatus
from teleport.core.clipboard import MAX_TEXT_BYTES, PNG_SIGNATURE
from teleport.core.messages import ClipKind

MINIMAL_PNG = (
    PNG_SIGNATURE
    + b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    + b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _PushBackend(FakeClipboard):
    def __init__(self) -> None:
        super().__init__()
        self.read_calls = 0
        self.read_now_calls = 0
        self.force_echo_empty = False
        self.force_unsupported = False

    async def read(self):
        self.read_calls += 1
        return await self._do_read(ignore_echo=False)

    async def read_now(self):
        self.read_now_calls += 1
        return await self._do_read(ignore_echo=True)

    async def _do_read(self, *, ignore_echo: bool):
        if self.force_unsupported:
            return app_clip.ClipRead(ReadStatus.UNSUPPORTED)
        res = await super().read()
        if not ignore_echo and self.force_echo_empty and res.status is ReadStatus.OK:
            return app_clip.ClipRead(
                ReadStatus.OK,
                app_clip.ClipContent(ClipKind.PNG, b"", res.content.fingerprint),
            )
        return res


async def test_push_now_result_codes_without_network() -> None:
    backend = _PushBackend()
    enabled = True
    events = []
    sync = app_clip.ClipboardSync(
        backend=backend,
        enabled=lambda _dev: enabled,
        paused=lambda: False,
        on_event=events.append,
    )
    sync.start()
    try:
        dev = "b" * 32
        assert await sync.push_now(dev) is ClipPushResult.NOT_CONNECTED

        sent_msgs = []
        fake_conn = SimpleNamespace(
            closed=False,
            send=lambda m: asyncio.sleep(0, result=sent_msgs.append(m)),
        )
        sync.link_up(dev, 1, fake_conn)

        enabled = False
        assert await sync.push_now(dev) is ClipPushResult.NOT_ENABLED
        enabled = True

        backend.clear()
        assert await sync.push_now(dev) is ClipPushResult.EMPTY

        backend.set_text("secret", sensitive=True)
        assert await sync.push_now(dev) is ClipPushResult.SENSITIVE

        backend.set_text("x" * (2 * MAX_TEXT_BYTES + 10))
        assert await sync.push_now(dev) is ClipPushResult.TOO_LARGE

        backend.force_unsupported = True
        assert await sync.push_now(dev) is ClipPushResult.UNSUPPORTED
        backend.force_unsupported = False

        backend.fail_read = True
        assert await sync.push_now(dev) is ClipPushResult.FAILED
        backend.fail_read = False

        # Bắn thủ công bằng read_now() ngay cả khi dữ liệu đã từng gửi (vượt qua LoopGuard)
        before = backend.read_now_calls
        backend.set_text("hello push")
        assert await sync.push_now(dev) is ClipPushResult.SENT
        assert backend.read_now_calls == before + 1
        assert await sync.push_now(dev) is ClipPushResult.SENT
        assert backend.read_now_calls == before + 2
        await wait_until(lambda: len(sent_msgs) >= 2, 5, "hai clip thủ công đi xuống kết nối")
    finally:
        sync.close()
        await sync.drain(2)


@pytest.mark.integration
async def test_push_now_end_to_end_repush_and_disabled_error(node_factory, trust_each_other, monkeypatch):
    monkeypatch.setattr(app_clip, "CLIP_DEBOUNCE", 0.02)
    trust_each_other("A", "B", name_a="A", name_b="B")
    cb_a = _PushBackend()
    cb_b = _PushBackend()
    events_a: list = []
    events_b: list = []
    node_a = await node_factory("A", node_kwargs={"clipboard": cb_a, "on_clipboard_event": events_a.append})
    node_b = await node_factory("B", node_kwargs={"clipboard": cb_b, "on_clipboard_event": events_b.append})

    node_a.connect_hint(node_b.id, ["127.0.0.1"], node_b.port)
    await wait_until(
        lambda: node_b.id in node_a.connected_devices() and node_a.id in node_b.connected_devices(),
        15,
        "hai node nối điều khiển",
    )

    assert await node_a.node.clipboard.push_now(node_b.id) is ClipPushResult.NOT_ENABLED
    node_a.node.set_clipboard(node_b.id, True)
    node_b.node.set_clipboard(node_a.id, True)

    cb_a._content = app_clip.ClipContent(
        ClipKind.TEXT, "manual clip", app_clip.text_fingerprint("manual clip")
    )
    assert await node_a.node.clipboard.push_now(node_b.id) is ClipPushResult.SENT
    await wait_until(lambda: len(cb_b.writes) == 1, 10, "B nhận clipboard từ nút gửi thủ công")
    assert cb_b.writes[0][1].data == "manual clip"

    # Đẩy ảnh ngược từ B về A bằng push_now ngay cả khi B có ảnh vừa ghi trước đó
    cb_b.set_image(MINIMAL_PNG)
    assert await node_b.node.clipboard.push_now(node_a.id) is ClipPushResult.SENT
    await wait_until(lambda: len(cb_a.writes) == 1, 10, "A nhận ảnh từ B")
    assert cb_a.writes[0][1].data == MINIMAL_PNG
