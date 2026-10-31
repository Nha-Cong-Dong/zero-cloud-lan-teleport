"""Hoạt động theo máy cho danh sách cuộc trò chuyện (spec B2 mục 2.3): lượt đang chạy và lượt đã kết
thúc gần nhất của mỗi `device_id`.

Module thuần, **không import PySide6** (test kiểm): `ui.transfers` gọi trên thread Qt, `ui.conversations`
đọc kết quả; B3 dùng lại. Không gọi node.

**Nguồn**
- Lượt đang chạy: các `TransferUpdate` mà `TransfersController.on_transfer` nhận (`handle`), cộng ảnh
  chụp `Node.transfers.transfers()` lúc node `started` (`load_snapshot`). “Đang bận” (`busy`,
  `busy_devices`) là máy còn ít nhất một lượt chưa kết thúc ở đây; cùng nguồn với
  `TransferManager.busy_devices()` nên **tương đương** trong điều kiện thường, có thể lệch trong cửa
  sổ ngắn: lượt `TransferFinished(SUSPENDED)` vẫn được giữ (như `TransfersView`: lượt nhận tạm dừng
  sẽ tiếp ở lần chạy sau), và lượt bị gỡ thẻ của `ui.transfers` chỉ hết khi `TransferFinished` tới.
  Không polling node (mvp N3).
- Lượt đã kết thúc gần nhất (`DeviceActivity.last`): trong phiên là `TransferFinished` (khác
  SUSPENDED), mốc là giờ tường lúc nhận (`wall_now` do bên gọi đưa, giây epoch). Lúc `started` bên
  gọi đọc `history()` trên thread node và rút gọn bằng `latest_by_device` (mục đầu, tức mới nhất,
  của mỗi máy), rồi `load_history`: mục lịch sử chỉ dùng khi máy **chưa có `last` nào trong phiên**
  (không so giờ: giờ của máy có thể đã bị chỉnh). `result` lạ trong `history.json` → None.

**Luật của `handle`** (trả `device_id` khi hoạt động của máy đó đổi, không thì None)
- `TransferStatusChanged`, `TransferResumed`: thêm hay cập nhật lượt đang chạy; `shown_bytes` =
  max(cũ, `done_bytes + unsynced_bytes`). `TransferProgress`: như vậy với max(cũ, `shown_bytes`).
  `shown_bytes` không bao giờ giảm và luôn ≤ `total_bytes`. `started_at` là lúc thấy lần đầu.
- `OfferReceived`: thêm lượt chiều nhận ở WAITING (số file, tổng byte, tên đầu) nếu chưa có.
- `TransferFinished`: SUSPENDED → None, không đổi gì; khác → bỏ khỏi đang chạy, đặt `last`.
- `FileDone`, `DataUnreachable`, loại lạ → None.
- Sự kiện muộn của lượt đã kết thúc (kể cả trong `load_snapshot`) không dựng lại lượt: nhớ
  `FINISHED_MEMORY` khóa đã kết thúc gần nhất (cũ nhất bị quên trước). `clear()` (host mới) quên hết.

Thứ tự `DeviceActivity.active`: mới nhất trước theo (`started_at`, `transfer_id`), không phụ thuộc
thứ tự sự kiện tới.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from teleport.app.transfers import (
    Direction,
    OfferReceived,
    TransferFinished,
    TransferInfo,
    TransferKey,
    TransferProgress,
    TransferResult,
    TransferResumed,
    TransferStatus,
    TransferStatusChanged,
    TransferUpdate,
)
from teleport.core.history import HistoryEntry

FINISHED_MEMORY = 1024  # số khóa đã kết thúc gần nhất được nhớ (chặn sự kiện muộn dựng lại lượt)


@dataclass(frozen=True, slots=True)
class ActiveTransfer:
    """Một lượt chưa kết thúc của một máy."""

    key: TransferKey
    status: TransferStatus
    file_count: int
    first_name: str | None
    shown_bytes: int
    total_bytes: int
    started_at: float  # giờ tường (giây epoch) lúc thấy lần đầu


@dataclass(frozen=True, slots=True)
class FinishedTransfer:
    """Lượt đã kết thúc gần nhất của một máy (trong phiên, hay từ lịch sử)."""

    direction: Direction
    result: TransferResult | None  # None: giá trị lạ trong history.json
    file_count: int
    first_name: str | None
    at: float  # giờ tường (giây epoch)


@dataclass(frozen=True, slots=True)
class DeviceActivity:
    """Hoạt động của một máy: các lượt đang chạy (mới nhất trước) và lượt đã kết thúc gần nhất."""

    active: tuple[ActiveTransfer, ...]
    last: FinishedTransfer | None

    @property
    def busy(self) -> bool:
        return bool(self.active)

    @property
    def at(self) -> float | None:
        """Mốc hoạt động gần nhất: max(`started_at` của lượt đang chạy, `last.at`); không có → None."""
        times = [a.started_at for a in self.active]
        if self.last is not None:
            times.append(self.last.at)
        return max(times) if times else None


EMPTY_ACTIVITY = DeviceActivity(active=(), last=None)


def _first(names: tuple[str, ...]) -> str | None:
    return names[0] if names else None


def latest_by_device(entries: Iterable[HistoryEntry]) -> dict[str, FinishedTransfer]:
    """Mục đầu (mới nhất, theo thứ tự của `history()`) của mỗi `device_id`.

    `at` = `finished_at.timestamp()`; `result` lạ → None.
    """
    latest: dict[str, FinishedTransfer] = {}
    for entry in entries:
        if entry.device_id in latest:
            continue
        try:
            result: TransferResult | None = TransferResult(entry.result)
        except ValueError:
            result = None
        latest[entry.device_id] = FinishedTransfer(
            direction=Direction(entry.direction),
            result=result,
            file_count=entry.file_count,
            first_name=_first(entry.names),
            at=entry.finished_at.timestamp(),
        )
    return latest


def _shown(old: int, new: int, total: int) -> int:
    """`shown_bytes` mới: không giảm, trong [0, total]."""
    return max(0, min(total, max(old, new)))


class ActivityTracker:
    """Hoạt động theo máy, cập nhật từ sự kiện lượt truyền (xem docstring module)."""

    def __init__(self) -> None:
        self._active: dict[str, dict[TransferKey, ActiveTransfer]] = {}
        self._last: dict[str, FinishedTransfer] = {}
        self._session_last: set[str] = set()  # máy đã có `last` từ `TransferFinished` trong phiên
        self._finished: OrderedDict[TransferKey, None] = OrderedDict()

    def clear(self) -> None:
        """Quên mọi thứ (host mới)."""
        self._active.clear()
        self._last.clear()
        self._session_last.clear()
        self._finished.clear()

    def get(self, device_id: str) -> DeviceActivity:
        active = self._active.get(device_id)
        last = self._last.get(device_id)
        if not active and last is None:
            return EMPTY_ACTIVITY
        ordered = sorted(
            (active or {}).values(), key=lambda a: (a.started_at, a.key.transfer_id), reverse=True
        )
        return DeviceActivity(active=tuple(ordered), last=last)

    def busy_devices(self) -> frozenset[str]:
        return frozenset(device_id for device_id, active in self._active.items() if active)

    def load_snapshot(self, infos: Iterable[TransferInfo], wall_now: float) -> frozenset[str]:
        """Ảnh chụp `transfers()` lúc `started`; trả các `device_id` đổi."""
        changed: set[str] = set()
        for info in infos:
            if isinstance(info, TransferInfo) and self._upsert(
                info, info.done_bytes + info.unsynced_bytes, wall_now
            ):
                changed.add(info.key.device_id)
        return frozenset(changed)

    def load_history(self, latest: Mapping[str, FinishedTransfer]) -> frozenset[str]:
        """Lượt đã kết thúc gần nhất từ lịch sử (`latest_by_device`); trả các `device_id` đổi."""
        changed: set[str] = set()
        for device_id, finished in latest.items():
            if device_id in self._session_last or self._last.get(device_id) == finished:
                continue
            self._last[device_id] = finished
            changed.add(device_id)
        return frozenset(changed)

    def handle(self, update: TransferUpdate, wall_now: float) -> str | None:
        """Cập nhật theo một `TransferUpdate`; trả `device_id` khi hoạt động của máy đó đổi."""
        if isinstance(update, (TransferStatusChanged, TransferResumed)):
            info = update.info
            changed = self._upsert(info, info.done_bytes + info.unsynced_bytes, wall_now)
            return info.key.device_id if changed else None
        if isinstance(update, TransferProgress):
            changed = self._upsert(update.info, update.shown_bytes, wall_now)
            return update.info.key.device_id if changed else None
        if isinstance(update, OfferReceived):
            return self._offer(update, wall_now)
        if isinstance(update, TransferFinished):
            return self._finish(update, wall_now)
        return None

    # ---- Bên trong -----------------------------------------------------------------------------

    def _upsert(self, info: TransferInfo, shown: int, wall_now: float) -> bool:
        key = info.key
        if key in self._finished:
            return False
        active = self._active.setdefault(key.device_id, {})
        old = active.get(key)
        new = ActiveTransfer(
            key=key,
            status=info.status,
            file_count=len(info.files),
            first_name=_first(info.names),
            shown_bytes=_shown(0 if old is None else old.shown_bytes, shown, info.total_bytes),
            total_bytes=info.total_bytes,
            started_at=wall_now if old is None else old.started_at,
        )
        if new == old:
            return False
        active[key] = new
        return True

    def _offer(self, update: OfferReceived, wall_now: float) -> str | None:
        key = update.key
        if key in self._finished:
            return None
        active = self._active.setdefault(key.device_id, {})
        if key in active:
            return None
        active[key] = ActiveTransfer(
            key=key,
            status=TransferStatus.WAITING,
            file_count=len(update.files),
            first_name=_first(update.names),
            shown_bytes=0,
            total_bytes=max(0, update.total_bytes),
            started_at=wall_now,
        )
        return key.device_id

    def _finish(self, update: TransferFinished, wall_now: float) -> str | None:
        if update.result is TransferResult.SUSPENDED:
            return None
        info = update.info
        key = info.key
        self._finished[key] = None
        self._finished.move_to_end(key)
        while len(self._finished) > FINISHED_MEMORY:
            self._finished.popitem(last=False)
        active = self._active.get(key.device_id)
        if active is not None:
            active.pop(key, None)
            if not active:
                del self._active[key.device_id]
        self._last[key.device_id] = FinishedTransfer(
            direction=key.direction,
            result=update.result,
            file_count=len(info.files),
            first_name=_first(info.names),
            at=wall_now,
        )
        self._session_last.add(key.device_id)
        return key.device_id
