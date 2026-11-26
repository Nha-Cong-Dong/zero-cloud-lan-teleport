"""Dịch vụ theo dõi thư mục đẩy tự động một chiều (Watch Portal, tầng app).

Chỉ mở tác vụ quét định kỳ khi có ít nhất một quy tắc `enabled=True` (không có quy tắc →
0% CPU khi rảnh, mvp N3). Không theo dõi symlink/junction. Nếu thiết bị đích chưa kết nối
khi tệp đã ổn định, đường dẫn được giữ lại trong hàng đệm và tự đẩy ngay khi `on_peer_connected`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import stat
from collections import deque
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from teleport.app.diskio import DiskExecutor
from teleport.app.sources import SourceError, SourceErrorKind
from teleport.core.watch import STABILITY_SECONDS, WatchPortalRule, WatchTracker, is_ignored_watch_name

log = logging.getLogger(__name__)

SCAN_INTERVAL = 1.5  # giây giữa hai lần quét thư mục (test vá được)
MAX_WATCH_BATCH = 64  # số tệp tối đa mỗi lần gọi `send_files`
_SCAN_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class WatchPortalEvent:
    """Thông báo kết quả một đợt đẩy tự động từ Watch Portal."""

    rule_id: str
    device_id: str
    paths: tuple[Path, ...]
    transfer_id: str | None
    error: str | None = None


def scan_watch_dir(source_dir: Path, recursive: bool) -> dict[str, tuple[int, int]]:
    """Quét thư mục `source_dir` trả ánh xạ `rel_path -> (size, mtime_ns)` (không theo symlink)."""
    try:
        if source_dir.is_symlink() or not source_dir.is_dir():
            return {}
    except OSError:
        return {}
    entries: dict[str, tuple[int, int]] = {}
    if not recursive:
        try:
            with os.scandir(source_dir) as it:
                for item in it:
                    if is_ignored_watch_name(item.name):
                        continue
                    try:
                        if item.is_symlink():
                            continue
                        st = item.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISREG(st.st_mode):
                        entries[item.name] = (st.st_size, st.st_mtime_ns)
        except OSError:
            return {}
        return entries

    for root, dirs, files in os.walk(source_dir, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not is_ignored_watch_name(d) and not Path(root, d).is_symlink())
        files.sort()
        for fname in files:
            if is_ignored_watch_name(fname):
                continue
            fpath = Path(root, fname)
            try:
                if fpath.is_symlink():
                    continue
                st = os.lstat(fpath)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            rel = os.path.relpath(fpath, source_dir).replace("\\", "/")
            entries[rel] = (st.st_size, st.st_mtime_ns)
    return entries


class WatchPortalService:
    """Quản lý vòng lặp quét thư mục và hàng chờ đẩy tự động sang thiết bị tin cậy."""

    def __init__(
        self,
        *,
        executor: DiskExecutor,
        is_trusted: Callable[[str], bool],
        is_connected: Callable[[str], bool],
        send_files: Callable[[str, Sequence[Path]], Coroutine[Any, Any, Any]],
        on_event: Callable[[WatchPortalEvent], None] | None = None,
        stability: float | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._executor = executor
        self._is_trusted = is_trusted
        self._is_connected = is_connected
        self._send_files = send_files
        self._on_event = on_event
        self._stability = stability
        self._rules: tuple[WatchPortalRule, ...] = ()
        self._tracker = WatchTracker()
        self._pending: dict[tuple[str, str], deque[Path]] = {}
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._closed = False
        self._started = False

    def start(self) -> None:
        """Cho phép vòng quét hoạt động khi có quy tắc bật."""
        if self._closed:
            return
        self._started = True
        self._sync_task()

    def set_rules(self, rules: Sequence[WatchPortalRule]) -> None:
        """Cập nhật danh sách quy tắc; bật/tắt tác vụ quét tương ứng."""
        self._rules = tuple(rules)
        self._tracker.set_rules(self._rules)
        valid_keys = {(r.id, r.device_id) for r in self._rules if r.enabled}
        for key in [k for k in self._pending if k not in valid_keys]:
            del self._pending[key]
        self._sync_task()
        self._wake.set()

    def on_peer_connected(self, device_id: str) -> None:
        """Đẩy ngay các tệp đã sẵn sàng đang chờ `device_id` khi kết nối điều khiển lên."""
        if self._closed or not self._started:
            return
        if any(dev == device_id and q for (_, dev), q in self._pending.items()):
            self._wake.set()

    async def scan_once(self) -> None:
        """Chạy một vòng quét đồng bộ mọi quy tắc đang bật và đẩy lô sẵn sàng (hữu ích cho test)."""
        if self._closed:
            return
        stability = STABILITY_SECONDS if self._stability is None else self._stability
        for rule in list(self._rules):
            if not rule.enabled or self._closed:
                continue
            source_dir = Path(rule.source_dir)
            try:
                job = self._executor.submit(scan_watch_dir, source_dir, rule.recursive)
            except RuntimeError:
                return
            try:
                entries = await job.result(_SCAN_TIMEOUT)
            except asyncio.CancelledError:
                job.cancel()
                raise
            except Exception:
                await job.settled()
                continue
            if self._closed:
                return
            ready = self._tracker.scan(rule.id, entries, self._loop.time(), stability=stability)
            if ready:
                q = self._pending.setdefault((rule.id, rule.device_id), deque())
                for rel in ready:
                    q.append(source_dir / Path(*rel.split("/")))
        await self._flush_ready()

    async def close(self) -> None:
        """Dừng sạch tác vụ quét và giải phóng hàng đệm."""
        self._closed = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._pending.clear()

    def _sync_task(self) -> None:
        need = self._started and not self._closed and any(r.enabled for r in self._rules)
        if need and self._task is None:
            self._task = asyncio.create_task(self._run(), name="teleport.watch-portal")
        elif not need and self._task is not None:
            task, self._task = self._task, None
            task.cancel()

    async def _run(self) -> None:
        try:
            while not self._closed and any(r.enabled for r in self._rules):
                await self.scan_once()
                if self._closed or not any(r.enabled for r in self._rules):
                    break
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(SCAN_INTERVAL):
                        await self._wake.wait()
        except asyncio.CancelledError:
            return

    async def _flush_ready(self) -> None:
        for rule in list(self._rules):
            if not rule.enabled or self._closed:
                continue
            key = (rule.id, rule.device_id)
            q = self._pending.get(key)
            if not q or not self._is_trusted(rule.device_id) or not self._is_connected(rule.device_id):
                continue
            while q and not self._closed:
                batch: list[Path] = []
                while q and len(batch) < MAX_WATCH_BATCH:
                    cand = q.popleft()
                    batch.append(cand)
                if not batch:
                    break
                try:
                    result_key = await self._send_files(rule.device_id, batch)
                except SourceError as e:
                    if e.kind is SourceErrorKind.NOT_FOUND:
                        continue
                    self._emit(WatchPortalEvent(rule.id, rule.device_id, tuple(batch), None, e.kind.value))
                    continue
                except Exception as e:
                    log.debug("watch portal đẩy tới %s lỗi: %s", rule.device_id[:8], type(e).__name__)
                    for path in reversed(batch):
                        q.appendleft(path)
                    break
                tid = getattr(result_key, "transfer_id", None)
                self._emit(WatchPortalEvent(rule.id, rule.device_id, tuple(batch), tid))
            if key in self._pending and not self._pending[key]:
                del self._pending[key]

    def _emit(self, event: WatchPortalEvent) -> None:
        if self._closed or self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:
            log.exception("on_watch_event raise")


__all__ = [
    "MAX_WATCH_BATCH",
    "SCAN_INTERVAL",
    "WatchPortalEvent",
    "WatchPortalService",
    "scan_watch_dir",
]
