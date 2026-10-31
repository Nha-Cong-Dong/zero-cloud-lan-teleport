"""Lịch sử truyền bền: `<config_dir>/history.json` (mvp F5; docs/architecture.md mục 4 `history.json`).

Mục, mã hóa và giải mã ở `teleport.core.history`; ở đây chỉ đọc/ghi file trong thư mục cấu hình
(`load`, `save`: API đồng bộ, chặn, gọi qua `DiskExecutor`) và `HistoryStore`, bảng trong bộ nhớ
của `TransferManager` kèm việc gộp ghi. `load`, `save`, `_read_limited` được gọi qua tên cấp
module lúc dùng, nên test thay được bằng monkeypatch.

`HistoryStore` (gọi trên event loop; chỉ `TransferManager` tạo):

- **Nạp** (`load`, gọi một lần trong `TransferManager.restore`): chưa có file → rỗng, không tạo
  file. File hỏng (JSON sai, `version` khác 1, `entries` sai, vượt trần) → rỗng, WARNING, lần
  ghi sau ghi đè (không giữ bản sao; kể cả file của bản mới hơn có `version` khác 1). Không đọc
  được (OSError khác `FileNotFoundError`, không phải file thường, quá hạn) → rỗng, WARNING, và
  **không ghi trong cả phiên**, trừ khi người dùng `clear`.
- **Không ghi trước khi nạp xong:** chưa nạp xong (hay `load` không bao giờ được gọi) thì không
  ghi, trừ khi có `clear`. Mục `record` trước lúc nạp xong được gộp lên đầu kết quả nạp.
- **Xóa:** `clear()` (toàn bộ) cấp quyền ghi kể cả khi file không đọc được lúc nạp (ý người
  dùng). `clear(device_id)` chỉ bỏ mục của máy đó và **không** cấp quyền ghi: file không đọc được
  thì chỉ xóa trong bộ nhớ (WARNING), vì ghi đè sẽ mất lịch sử của các máy khác.
- **Gộp ghi** (như bia mộ ở `app/transfers.py`): `record` / `clear` đánh dấu và hẹn một lần ghi
  (`call_soon`, hay `call_later` cho đủ `HISTORY_MIN_INTERVAL` từ lần ghi trước). Lần ghi chụp
  tuple các mục bất biến, mã hóa và ghi trong thread. Tối đa một việc đĩa trên `history.json`
  tại một thời điểm, kể cả việc nạp: lần ghi chờ việc trước (và việc nạp) `settled()` rồi mới
  giao, vì `atomic.write_atomic` dùng tên `.tmp` cố định.
- **Đóng:** `flush` (trong `TransferManager.close`) xả ảnh chụp cuối trong hạn, không bao giờ ném
  gì ngoài CancelledError; `stop` (trong `abort_now`) chỉ hủy hẹn và task ghi.

Ca đã chấp nhận: lượt tạm dừng không bao giờ được khôi phục không có mục; lượt nhận đã ghi
`done_at` mà tạm dừng trước khi kết thúc thành `RestoredDone` lúc khởi động lại và cũng không có
mục bên nhận.

Log (mvp N9): chỉ loại lỗi và `errno`; không bao giờ tên file, đường dẫn hay nội dung file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from teleport import atomic
from teleport.app.diskio import DiskExecutor, DiskJob
from teleport.core.history import (
    MAX_HISTORY_FILE,
    HistoryEntry,
    HistoryError,
    add_entry,
    decode_history,
    encode_history,
    remove_device,
    trim_history,
)

HISTORY_NAME = "history.json"
HISTORY_MIN_INTERVAL = 1.0  # giây tối thiểu giữa hai lần ghi `history.json`

_TASK_NAME = "teleport.node"

# Mở để đọc: không theo symlink (POSIX), FIFO cài sẵn không làm treo thread đĩa.
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Loaded:
    """Kết quả của `load`. `writable` False: file có mà không đọc được, không được ghi đè."""

    entries: list[HistoryEntry]
    writable: bool


def load(config_dir: Path) -> Loaded:
    """(thread) Đọc `history.json`; không bao giờ ném lỗi (trừ khi bị ngắt).

    Chưa có file → rỗng, `writable`. Không phải file thường hay lỗi đọc → WARNING (chỉ loại
    lỗi), rỗng, không `writable`. Dài quá `MAX_HISTORY_FILE` (đọc tối đa trần + 1 byte) hay hỏng
    cả file → WARNING, rỗng, `writable` (lần ghi sau ghi đè). Mục hỏng bị bỏ riêng.
    """
    path = Path(config_dir) / HISTORY_NAME
    try:
        data = _read_limited(path)
    except FileNotFoundError:
        return Loaded([], True)
    except Exception as e:  # OSError, HistoryError (không phải file thường)…: không chặn app
        log.warning("không đọc được %s (%s), lịch sử rỗng và không ghi đè", HISTORY_NAME, _label(e))
        return Loaded([], False)
    if len(data) > MAX_HISTORY_FILE:
        log.warning("%s dài quá %d byte, lịch sử rỗng", HISTORY_NAME, MAX_HISTORY_FILE)
        return Loaded([], True)
    try:
        return Loaded(decode_history(data), True)
    except Exception as e:  # HistoryError; phòng thủ: mọi lỗi giải mã khác
        log.warning("%s hỏng (%s), lịch sử rỗng", HISTORY_NAME, type(e).__name__)
        return Loaded([], True)


def save(config_dir: Path, entries: Sequence[HistoryEntry]) -> None:
    """(thread) Mã hóa và ghi `history.json` nguyên tử (`atomic.write_atomic`).

    Vượt trần thì bỏ mục cũ nhất (`encode_history`). OSError bay ra nguyên trạng; phần tử không
    phải HistoryEntry → HistoryError.
    """
    atomic.write_atomic(Path(config_dir) / HISTORY_NAME, encode_history(entries))


def _read_limited(path: Path) -> bytes:
    """Đọc tối đa `MAX_HISTORY_FILE` + 1 byte của một file thường; không phải file thường → HistoryError."""
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise HistoryError("history.json không phải file thường")
    fd = os.open(path, _READ_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HistoryError("history.json không phải file thường")
        chunks: list[bytes] = []
        remaining = MAX_HISTORY_FILE + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _label(e: BaseException) -> str:
    """Loại lỗi kèm `errno` nếu có; không bao giờ `str(e)` (có thể mang đường dẫn, mvp N9)."""
    number = getattr(e, "errno", None)
    return type(e).__name__ if number is None else f"{type(e).__name__} errno={number}"


class _JobDropped(Exception):
    """Việc đĩa bị `DiskExecutor.shutdown()` bỏ trước khi chạy (không phải task này bị hủy)."""


async def _job_result[T](job: DiskJob[T]) -> T:
    """`job.result(None)`; việc bị bỏ khi shutdown → `_JobDropped` thay cho CancelledError.

    Phân biệt bằng trạng thái của task đang chờ (`cancelling()`), như `app/transfers.py`.
    """
    try:
        return await job.result(None)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if (task is not None and task.cancelling()) or job.started:
            raise
        raise _JobDropped("việc đĩa bị bỏ khi đóng") from None


class HistoryStore:
    """Bảng lịch sử trong bộ nhớ và việc ghi `history.json` (docstring module). Gọi trên event loop.

    Chỉ `TransferManager` tạo.
    """

    def __init__(self, *, executor: DiskExecutor, config_dir: Path) -> None:
        self._executor = executor
        self._config_dir = Path(config_dir)
        self._loop = asyncio.get_running_loop()
        self._entries: tuple[HistoryEntry, ...] = ()
        self._load_called = False
        self._loaded = False  # `load` đã xong (thành công hay không)
        self._writable = False  # được ghi `history.json` (sau khi nạp xong, hay sau `clear`)
        self._cleared = False  # `clear` đã chạy: được ghi kể cả khi chưa nạp xong
        self._load_discarded = False  # `clear` lúc đang nạp: bỏ kết quả nạp
        self._load_job: DiskJob[Loaded] | None = None
        self._dirty = False  # bảng đổi từ ảnh chụp cuối
        self._scheduled = False  # đã hẹn `_snapshot`
        self._handle: asyncio.Handle | None = None  # `call_soon` / `call_later` đang hẹn
        self._task: asyncio.Task[None] | None = None  # lần ghi đang chạy (tới khi việc `settled`)
        self._job: DiskJob[None] | None = None  # việc đĩa của lần ghi gần nhất
        self._last_write: float | None = None  # `loop.time()` lúc lần ghi trước xong
        self._stopped = False  # đang xả lúc đóng hay đã hủy: không hẹn ghi nữa

    def entries(self, device_id: str | None = None) -> tuple[HistoryEntry, ...]:
        """Mọi mục (hay chỉ mục của `device_id`), mới nhất trước (bản bất biến).

        `device_id` không được kiểm ở đây (`TransferManager.history` kiểm): sai dạng thì rỗng.
        """
        if device_id is None:
            return self._entries
        return tuple(e for e in self._entries if e.device_id == device_id)

    def record(self, entry: HistoryEntry) -> None:
        """Thêm mục của một lượt vừa kết thúc (đồng bộ, không ném, không gọi engine); hẹn ghi."""
        try:
            self._entries = add_entry(self._entries, entry)
        except Exception as e:  # HistoryError: entry sai kiểu
            log.warning("không thêm được mục lịch sử: %s", type(e).__name__)
            return
        self._dirty = True
        self._schedule()

    def clear(self, device_id: str | None = None) -> None:
        """Xóa lịch sử: toàn bộ (`device_id` None) hay chỉ của một máy; hẹn ghi.

        Toàn bộ: rỗng ngay trong bộ nhớ, bỏ kết quả nạp đang chờ, hẹn ghi file rỗng; ghi được kể
        cả khi file không đọc được lúc nạp (ý người dùng). Một máy: bỏ mục của máy đó trong bộ
        nhớ, **không** cấp quyền ghi (file không đọc được thì chỉ xóa trong bộ nhớ, WARNING);
        không có mục nào của máy đó thì không hẹn ghi; chỉ gọi sau khi nạp xong (assert; `Node`
        không có đường nào khác: `restore` chờ nạp xong). Sau `flush` / `stop` chỉ xóa trong bộ
        nhớ (WARNING). `device_id` sai dạng → HistoryError.
        """
        if device_id is not None:
            self._clear_device(device_id)
            return
        self._entries = ()
        self._load_discarded = True
        self._cleared = True
        self._writable = True
        self._dirty = True
        if self._stopped:
            log.warning("không ghi được %s: node đã đóng, chỉ xóa lịch sử trong bộ nhớ", HISTORY_NAME)
            return
        self._schedule()

    def _clear_device(self, device_id: str) -> None:
        assert self._loaded, "xóa theo máy chỉ sau khi nạp xong (`Node` gọi sau `restore`)"
        remaining = remove_device(self._entries, device_id)
        if len(remaining) == len(self._entries):
            return  # không có mục nào của máy đó: không hẹn ghi
        self._entries = remaining
        self._dirty = True
        log.info("đã xóa lịch sử với %s", device_id[:8])
        if self._stopped:
            log.warning("có thể không ghi được %s: node đã đóng, chỉ xóa lịch sử trong bộ nhớ", HISTORY_NAME)
            return
        if self._loaded and not self._writable:
            log.warning("%s không đọc được lúc nạp: chỉ xóa lịch sử trong bộ nhớ, không ghi đè", HISTORY_NAME)
            return
        self._schedule()

    async def load(self, timeout: float) -> None:
        """Nạp `history.json` qua executor (gọi một lần; lần hai → RuntimeError).

        Hạn `timeout` bao cả lúc chờ bắt đầu; quá hạn hay lỗi → rỗng, không ghi (WARNING). Mục
        `record` trước khi nạp xong được gộp lên đầu. Ngoài RuntimeError chỉ ném CancelledError.
        """
        if self._load_called:
            raise RuntimeError("HistoryStore.load() chỉ gọi được một lần")
        self._load_called = True
        try:
            job = self._executor.submit(load, self._config_dir)
        except RuntimeError:  # executor đã shutdown
            self._finish_load(Loaded([], False))
            return
        self._load_job = job
        try:
            async with asyncio.timeout(timeout):
                loaded = await _job_result(job)
        except asyncio.CancelledError:
            job.cancel()
            raise
        except Exception as e:  # gồm TimeoutError của `asyncio.timeout`, `_JobDropped`
            job.cancel()
            log.warning("không đọc được %s (%s), lịch sử rỗng và không ghi đè", HISTORY_NAME, _label(e))
            loaded = Loaded([], False)
        self._finish_load(loaded)

    def _finish_load(self, loaded: Loaded) -> None:
        """Áp kết quả nạp (đồng bộ): gộp mục đã ghi trước khi nạp xong lên đầu; hẹn ghi nếu cần."""
        self._loaded = True
        if self._load_discarded:
            return  # `clear` đã chạy: bỏ kết quả nạp, vẫn ghi được
        pending = self._entries
        try:
            merged = trim_history(loaded.entries)
            for entry in reversed(pending):  # cũ trước, để mục mới nhất ở đầu
                merged = add_entry(merged, entry)
            writable = loaded.writable is True
        except Exception as e:  # phòng thủ: kết quả nạp sai kiểu (seam test) → như không đọc được
            log.warning("kết quả nạp %s sai (%s), lịch sử rỗng", HISTORY_NAME, type(e).__name__)
            merged, writable = pending, False
        self._writable = writable
        self._entries = merged
        if self._dirty:
            self._schedule()

    async def flush(self, timeout: float) -> None:
        """Ghi ảnh chụp cuối, toàn bộ trong `timeout` (như xả bia mộ lúc `close`).

        Chờ lần ghi đang chạy; còn thay đổi chưa ghi (và được ghi) thì chụp và ghi ngay. Quá hạn:
        việc chưa chạy bị bỏ, việc đang chạy để nguyên trong thread, **không** ghi lần hai,
        WARNING. Không bao giờ ném gì ngoài CancelledError.
        """
        self._stop_scheduling()
        try:
            async with asyncio.timeout(timeout):
                task = self._task
                if task is not None:
                    await asyncio.wait({task})  # không hủy task khi hết hạn
                if self._task is None and self._dirty:
                    self._snapshot(flush=True)
                    task = self._task
                    if task is not None:
                        await asyncio.wait({task})
        except TimeoutError:
            self._abandon()
            log.warning("ghi %s chưa xong sau %g s khi đóng", HISTORY_NAME, timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # phòng thủ: `gather` của `TransferManager.close` không được hỏng
            self._abandon()
            log.warning("không xả được %s: %s", HISTORY_NAME, _label(e))

    def stop(self) -> None:
        """Abort: hủy lần hẹn và task ghi; việc đã ở trong thread vẫn chạy tới cùng."""
        self._stop_scheduling()
        task = self._task
        if task is not None:
            task.cancel()

    # ---- Nội bộ --------------------------------------------------------------------------

    @property
    def _can_write(self) -> bool:
        return self._writable and (self._loaded or self._cleared)

    def _stop_scheduling(self) -> None:
        self._stopped = True
        self._scheduled = False
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.cancel()

    def _abandon(self) -> None:
        """Hết hạn xả: bỏ việc chưa chạy, thôi chờ task ghi (không có lần ghi nào sau đó)."""
        job = self._job
        if job is not None:
            job.cancel()  # chỉ có tác dụng khi việc chưa bắt đầu
        task = self._task
        if task is not None:
            task.cancel()

    def _schedule(self) -> None:
        """Hẹn chụp bảng nếu được ghi, chưa hẹn và không có lần ghi đang chạy: ngay (`call_soon`),
        hay lúc đủ `HISTORY_MIN_INTERVAL` từ lần ghi trước (`call_later`)."""
        if self._scheduled or self._task is not None or self._stopped or not self._can_write:
            return
        self._scheduled = True
        last = self._last_write
        delay = 0.0 if last is None else last + HISTORY_MIN_INTERVAL - self._loop.time()
        if delay > 0:
            self._handle = self._loop.call_later(delay, self._snapshot)
        else:
            self._handle = self._loop.call_soon(self._snapshot)

    def _snapshot(self, *, flush: bool = False) -> None:
        """Chụp tuple các mục (đồng bộ) rồi tạo task ghi."""
        self._scheduled = False
        self._handle = None
        if self._task is not None or (self._stopped and not flush) or not self._can_write:
            return
        self._dirty = False
        self._task = asyncio.create_task(self._write(self._entries), name=f"{_TASK_NAME}.history")

    async def _write(self, items: tuple[HistoryEntry, ...]) -> None:
        """Một lần ghi `history.json` (mã hóa trong thread). Chờ việc nạp và việc ghi `settled`
        trước khi cho lần ghi sau (tên `.tmp` cố định); còn thay đổi thì hẹn lần sau.

        Ghi lỗi: đánh dấu lại là chưa ghi (`_dirty`) nhưng không tự hẹn thử lại, để không lặp
        lỗi mãi; lần đổi sau hay `flush` lúc đóng thử lại (mỗi lần `flush` đúng một lần)."""
        failed = False
        try:
            load_job = self._load_job
            if load_job is not None:
                await load_job.settled()
            try:
                job = self._executor.submit(save, self._config_dir, items)
            except RuntimeError:  # executor đã shutdown
                log.warning("không ghi được %s: node đã đóng", HISTORY_NAME)
                return
            self._job = job
            try:
                await _job_result(job)
            except _JobDropped:
                return
            except Exception as e:
                log.warning("không ghi được %s: %s", HISTORY_NAME, _label(e))
                failed = True
            await job.settled()
            self._last_write = self._loop.time()
        finally:
            if self._task is asyncio.current_task():
                self._task = None
        if failed and not self._dirty:
            # Ảnh chụp này chưa lên đĩa: chờ lần đổi sau hay `flush`, không hẹn ngay.
            self._dirty = True
            return
        if self._dirty:
            self._schedule()
