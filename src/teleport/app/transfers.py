"""Quản lý lượt truyền file của một node: chạy `core.transfer.TransferEngine` trên kết nối thật.

`TransferManager` (chỉ `Node` tạo, lấy qua `node.transfers`) thực thi hành động của **một**
`TransferEngine` cho cả node theo hợp đồng ở docstring `core/transfer.py`; mọi chính sách (kiểm
`data_open`, `seq`, lịch thử lại, nhịp `fsync`, luật khối sai, bia mộ, chờ nối lại) nằm ở engine.
Ở đây chỉ có I/O:

- **Gọi engine** (`_call`): đọc `now = loop.time()` ngay lúc gọi (kể cả lời gọi bị hoãn, kết quả
  giao sau, `call_soon` của tự nhận), rồi thực hiện danh sách hành động đồng bộ, đúng thứ tự, không
  `await` xen giữa. **Không gọi engine lồng nhau:** lời gọi phát sinh trong lúc đang thực hiện (kể
  cả từ callback `on_transfer_event`) xếp vào hàng `_deferred`, chạy sau khi danh sách hiện tại
  xong. Engine ném lỗi thì chỉ ghi log; một hành động ném lỗi thì ghi log rồi vẫn chạy các hành
  động sau.
- **Kết nối điều khiển:** `Node` báo `control_up` / `control_lost` theo `LinkUp`, `LinkSwitched`,
  `LinkDown`, và giao message truyền file (`on_message`). `SendMessage` đi qua hàng gửi FIFO của
  kết nối hiện tại (`_Outbox`), có trần `CONTROL_SEND_QUEUE_MAX` message: tràn thì cắt kết nối đó
  (máy kia gửi liên tục mà không đọc), thành mất kết nối điều khiển và chờ nối lại như thường.
- **Kết nối dữ liệu:** `OpenData` mở bằng `open_data` (một địa chỉ, một lần thử); phía server,
  `serve_data` báo `incoming_data` trước `await` đầu tiên rồi giữ handler tới khi kết nối xong.
  Mỗi `SendBlocks` / `ReceiveBlocks` là một task; task kết thúc thì cắt kết nối (trừ lần đóng gọn
  khi bên gửi đã ghi hết khối). Bên nhận ngừng đọc khung khi đã có `WRITE_QUEUE_BLOCKS` khối chờ
  ghi (protocol §4.3; cận gửi lại của mvp N4).
- **Đĩa:** mọi thao tác chạm hệ file (`app.storage`, `app.sources`) đi qua `DiskExecutor` bằng
  `submit` (không `run`, để không mất handle khi bị hủy); mỗi lượt nhận có một hàng FIFO đĩa
  (`_DiskLane`) với luật `RemoveTemp` của hợp đồng (mục 5). Việc quá hạn vẫn chạy trong thread:
  kết quả tới muộn được dọn sau `DiskJob.settled()` (thư mục tạm tạo muộn bị xóa, file nguồn mở
  muộn bị đóng). Băm payload nhận dùng `asyncio.to_thread` (chỉ tốn CPU).
- **Giữ địa chỉ:** thiết bị còn lượt chưa kết thúc được `hold` ở `core.links` (nối lại theo lịch
  kể cả khi mDNS báo máy đi, protocol §6.1, §6.2).
- **Giới hạn tốc độ:** một `TokenBucket` chung cho mọi lượt gửi, áp theo từng đoạn 64 KiB
  (`core.ratelimit`); khi có giới hạn, tối đa `MAX_LIMITED_SENDS` lượt gửi chạy cùng lúc để mỗi
  lần chờ một đoạn ngắn hơn hạn chờ đọc của bên nhận.

`preview_sources(paths)` kiểm trước các file sẽ gửi mà không cần node (cùng lỗi như `send`), để
CLI báo lỗi file trước khi lấy `node.lock` hay đụng mạng.

Sự kiện cho giao diện / CLI: một callback `on_transfer_event` (`TransferUpdate`), gọi đồng bộ trên
loop theo thứ tự engine báo; vẫn phát trong lúc `close()`, hết phát khi `close()` trả về. Kết quả
cuối của mỗi lượt là `TransferResult` (mã, không có câu chữ: câu chữ ở `cli.py` / `ui`).

Log (mvp N9): không bao giờ ghi tên hay đường dẫn file; chỉ `transfer_id[:8]`, `file_index`,
`device_id[:8]`, loại lỗi và `errno` (`OSError` có thể mang đường dẫn trong `str(e)`, nên không
ghi `str(e)` hay traceback của nó).

Seam cho test (biến cấp module, đọc lúc dùng): các hằng dưới đây, `open_data`, `uuid4`; module
`sources` được gọi qua thuộc tính (`sources.prepare_sources`, `sources.SourceReader`,
`sources.check_source`).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import logging
import os
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from teleport.app import sources
from teleport.app.diskio import DiskExecutor, DiskJob, DiskTimeout
from teleport.app.sources import SourceChanged, SourceError, SourceErrorKind, SourceFile, SourceReader
from teleport.app.storage import PartFile, ReceiveDir, RereadStopped, TempExistsError, TransferTemp
from teleport.core import ratelimit
from teleport.core.blocks import DIGEST_SIZE, BlockError
from teleport.core.merkle import merkle_root
from teleport.core.messages import (
    Accept,
    Cancel,
    CancelReason,
    Complete,
    DataOpen,
    FileEntry,
    Hello,
    Offer,
    Reject,
    RejectReason,
    Resume,
    Verified,
    validate_device_id,
)
from teleport.core.ratelimit import TokenBucket
from teleport.core.transfer import (
    AskUser,
    CheckSource,
    CheckSpace,
    CloseData,
    CreateTemp,
    DataUnreachable,
    Direction,
    DiskOp,
    FailureDetail,
    FileVerified,
    Finalize,
    OpenData,
    OpenPart,
    Outcome,
    ReceiveBlocks,
    RemoveTemp,
    Report,
    Reread,
    SendBlocks,
    SendMessage,
    StatusChanged,
    SyncPart,
    TransferAction,
    TransferEnded,
    TransferEngine,
    TransferEvent,
    TransferIdInUse,
    TransferInfo,
    TransferKey,
    TransferMessage,
    TransferStatus,
    WriteBlock,
)
from teleport.net.connection import (
    Connection,
    ConnectionLostError,
    IdentityMismatch,
    VersionMismatch,
    open_data,
)
from teleport.trust.identity import Identity
from teleport.trust.store import TrustStore

__all__ = [
    "MIN_RATE_LIMIT",
    "DataUnreachable",
    "Direction",
    "FileDone",
    "FileEntry",
    "OfferReceived",
    "SourceError",
    "SourceErrorKind",
    "SourceFile",
    "TransferFinished",
    "TransferInfo",
    "TransferKey",
    "TransferManager",
    "TransferProgress",
    "TransferResult",
    "TransferStatus",
    "TransferStatusChanged",
    "TransferUpdate",
    "preview_sources",
]

# architecture §5 (bảng số của truyền file). Đọc lúc dùng: test vá bằng monkeypatch.
DISK_OP_TIMEOUT = 60.0  # một thao tác đĩa (tạo thư mục tạm, tạo `.part`, ghi khối, đọc khối, finalize phụ)
DISK_SYNC_TIMEOUT = 120.0  # `fsync` `.part`, `finalize`, phần cố định của lượt đọc lại
REREAD_BLOCK_TIMEOUT = 1.0  # đọc lại: cộng thêm mỗi khối
CONTROL_SEND_QUEUE_MAX = 4096  # message chờ gửi trên một kết nối điều khiển; tràn → cắt kết nối đó
CANCEL_FLUSH_TIMEOUT = 0.5  # `Node.close`: chờ đẩy hết `cancel` ra kết nối điều khiển
CLOSE_DISK_WAIT = 2.0  # `Node.close`: chờ các hàng đĩa xong `RemoveTemp`
MAX_LIMITED_SENDS = 8  # có giới hạn tốc độ: số `SendBlocks` chạy cùng lúc
MIN_RATE_LIMIT = ratelimit.CHUNK_SIZE  # byte/giây: mỗi lần chờ một đoạn ≤ 1 s
PROGRESS_INTERVAL = 1.0  # giây giữa hai lần báo tiến độ
SPEED_WINDOW = 5.0  # tốc độ tính trên các mẫu trong 5 s gần nhất
WRITE_QUEUE_BLOCKS = 8  # protocol §4.3: khối đã nhận chờ ghi đĩa tối đa

_TASK_NAME = "teleport.node"

_TRANSFER_MESSAGE_TYPES = (Offer, Accept, Reject, Resume, Complete, Verified, Cancel)

# Lời gọi engine bị hoãn (phương thức, đối số, đối số từ khóa); `now` thêm lúc thật sự gọi.
_Deferred = tuple[Callable[..., list[TransferAction]], tuple[Any, ...], dict[str, Any]]

log = logging.getLogger(__name__)


# ---- Kết quả và sự kiện cho giao diện ------------------------------------------------------


class TransferResult(StrEnum):
    """Kết quả cuối của một lượt (`TransferFinished.result`)."""

    DONE = "done"
    REJECTED_USER = "rejected_user"
    REJECTED_NO_SPACE = "rejected_no_space"
    REJECTED_FILE_TOO_LARGE = "rejected_file_too_large"
    REJECTED_BUSY = "rejected_busy"
    REJECTED_NOT_RECEIVING = "rejected_not_receiving"
    REJECTED_OTHER = "rejected_other"
    UNPAIRED = "unpaired"  # máy này hủy ghép đôi khi lượt chưa kết thúc
    CANCELLED_USER = "cancelled_user"
    SOURCE_CHANGED = "source_changed"
    EXPIRED = "expired"  # mất kết nối điều khiển quá `RECONNECT_GRACE`
    DISK_FAILED = "disk_failed"
    TEMP_EXISTS = "temp_exists"  # thư mục tạm của lượt còn sót lại từ lần trước
    CORRUPT = "corrupt"  # khối sai quá trần, hoặc đọc lại / Merkle sai quá trần
    PROTOCOL_ERROR = "protocol_error"
    SOURCE_READ_FAILED = "source_read_failed"
    PEER_ERROR = "peer_error"  # máy kia gửi `cancel{error}`
    UNKNOWN_TRANSFER = "unknown_transfer"  # máy kia không biết lượt này (ví dụ đã khởi động lại)
    CANCELLED_OTHER = "cancelled_other"


@dataclass(frozen=True, slots=True)
class OfferReceived:
    """Có `offer` cần trả lời (`decide`), hoặc đã tự nhận (`auto_accepted`)."""

    key: TransferKey
    device_name: str | None  # tên trong trusted.json, chưa qua `display_name`
    files: tuple[FileEntry, ...]
    names: tuple[str, ...]  # đã làm sạch và khử trùng trong `offer`
    total_bytes: int
    auto_accepted: bool


@dataclass(frozen=True, slots=True)
class TransferStatusChanged:
    info: TransferInfo


@dataclass(frozen=True, slots=True)
class TransferProgress:
    """Tiến độ, mỗi `PROGRESS_INTERVAL` khi `done_bytes` đổi. `eta` (giây) None khi chưa đo được tốc độ."""

    info: TransferInfo
    bytes_per_second: float
    eta: float | None


@dataclass(frozen=True, slots=True)
class FileDone:
    """Một file đã xong. `path`: bên nhận là đường dẫn file cuối; bên gửi là None."""

    key: TransferKey
    file_index: int
    name: str
    path: Path | None


@dataclass(frozen=True, slots=True)
class TransferFinished:
    """Lượt kết thúc (đúng một lần mỗi lượt); `info` là bản chụp cuối."""

    info: TransferInfo
    result: TransferResult
    by_peer: bool
    reason: str | None
    file_index: int | None
    reject: Reject | None


TransferUpdate = (
    OfferReceived | TransferStatusChanged | TransferProgress | FileDone | TransferFinished | DataUnreachable
)

_REJECT_RESULTS: dict[str, TransferResult] = {
    RejectReason.USER: TransferResult.REJECTED_USER,
    RejectReason.NO_SPACE: TransferResult.REJECTED_NO_SPACE,
    RejectReason.FILE_TOO_LARGE: TransferResult.REJECTED_FILE_TOO_LARGE,
    RejectReason.BUSY: TransferResult.REJECTED_BUSY,
    RejectReason.NOT_RECEIVING: TransferResult.REJECTED_NOT_RECEIVING,
}

_ERROR_RESULTS: dict[FailureDetail | None, TransferResult] = {
    FailureDetail.DISK: TransferResult.DISK_FAILED,
    FailureDetail.TEMP_EXISTS: TransferResult.TEMP_EXISTS,
    FailureDetail.CORRUPT_BLOCKS: TransferResult.CORRUPT,
    FailureDetail.VERIFY_FAILED: TransferResult.CORRUPT,
    FailureDetail.PROTOCOL: TransferResult.PROTOCOL_ERROR,
    FailureDetail.SOURCE_READ: TransferResult.SOURCE_READ_FAILED,
}


def _result_of(ended: TransferEnded, unpaired: bool) -> TransferResult:
    """`TransferEnded` → `TransferResult`. Lượt bị hủy vì hủy ghép đôi xét trước mọi thứ khác."""
    if unpaired:
        return TransferResult.UNPAIRED
    if ended.outcome is Outcome.DONE:
        return TransferResult.DONE
    reason = ended.reason or ""
    if ended.outcome is Outcome.REJECTED:
        return _REJECT_RESULTS.get(reason, TransferResult.REJECTED_OTHER)
    if reason == CancelReason.USER:
        return TransferResult.CANCELLED_USER
    if reason == CancelReason.SOURCE_CHANGED:
        return TransferResult.SOURCE_CHANGED
    if reason == CancelReason.EXPIRED:
        return TransferResult.EXPIRED
    if reason == CancelReason.ERROR:
        if ended.by_peer:
            return TransferResult.PEER_ERROR
        return _ERROR_RESULTS.get(ended.detail, TransferResult.CANCELLED_OTHER)
    if reason == CancelReason.UNKNOWN_TRANSFER and ended.by_peer:
        return TransferResult.UNKNOWN_TRANSFER
    return TransferResult.CANCELLED_OTHER


async def preview_sources(paths: Sequence[str | os.PathLike[str]]) -> list[SourceFile]:
    """Kiểm các file sẽ gửi như `TransferManager.send` (`sources.prepare_sources`), không cần node.

    Chạy trên một `DiskExecutor` một thread dùng một lần (thread daemon), không hạn (ổ mạng có
    thể treo: bên gọi hủy được). Lỗi như `send`: `SourceError`, ValueError (0 hoặc quá
    `MAX_OFFER_FILES` file, một file chọn hai lần), TypeError. Bị hủy → bỏ việc nếu chưa chạy;
    thread đang chạy thì chạy nốt rồi tự thoát. Không giữ handle nào sau khi trả về.
    """
    executor = DiskExecutor(1, name="teleport.preview")
    try:
        job = executor.submit(sources.prepare_sources, paths)
        try:
            return await job.result(None)
        except asyncio.CancelledError:
            job.cancel()
            raise
    finally:
        executor.shutdown()


# ---- Việc đĩa --------------------------------------------------------------------------------


class _JobDropped(Exception):
    """Việc đĩa bị `DiskExecutor.shutdown()` bỏ trước khi chạy (không phải task này bị hủy)."""


async def _job_result[T](job: DiskJob[T], timeout: float | None) -> T:
    """`job.result(timeout)`; việc bị bỏ khi shutdown → `_JobDropped` thay cho CancelledError.

    Phân biệt bằng trạng thái của task đang chờ (`cancelling()`), không dựa vào CancelledError.
    """
    try:
        return await job.result(timeout)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if (task is not None and task.cancelling()) or job.started:
            raise
        raise _JobDropped("việc đĩa bị bỏ khi đóng") from None


async def _late_result[T](job: DiskJob[T]) -> T | None:
    """Kết quả của một việc đã `done` (kể cả việc đã quá hạn); lỗi hay bị bỏ → None."""
    try:
        return await job.result(None)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        return None
    except Exception:
        return None


def _sha256(data: bytearray) -> bytes:
    return hashlib.sha256(data).digest()


def _reread_compare(
    part: PartFile, leaves: bytearray, stop: threading.Event
) -> tuple[list[int], bytes | None]:
    """(thread) Đọc lại `.part`, so hash từng khối với `leaves` → `(bad, root)` cho `reread_done`.

    `leaves` là bytearray của engine: chỉ đọc (engine không sửa nó lúc VERIFYING).
    """
    digests = part.reread(stop=stop)
    if digests == leaves:
        return [], merkle_root(leaves)
    n = len(leaves) // DIGEST_SIZE
    have, want = memoryview(digests), memoryview(leaves)
    bad = [
        i
        for i in range(n)
        if have[i * DIGEST_SIZE : (i + 1) * DIGEST_SIZE] != want[i * DIGEST_SIZE : (i + 1) * DIGEST_SIZE]
    ]
    if not bad:  # phòng thủ: độ dài khác nhau mà mọi khối so được đều khớp
        bad = list(range(n))
    return bad, None


def _error_label(e: BaseException) -> str:
    """Loại lỗi cho log, kèm `errno` nếu có; không bao giờ `str(e)` (có thể mang đường dẫn, mvp N9)."""
    number = getattr(e, "errno", None)
    return type(e).__name__ if number is None else f"{type(e).__name__} errno={number}"


_DISK_OPS: dict[type, DiskOp] = {
    CreateTemp: DiskOp.CREATE_TEMP,
    OpenPart: DiskOp.OPEN_PART,
    WriteBlock: DiskOp.WRITE,
    SyncPart: DiskOp.SYNC,
    Reread: DiskOp.REREAD,
    Finalize: DiskOp.FINALIZE,
}

_LaneAction = CreateTemp | OpenPart | WriteBlock | SyncPart | Reread | Finalize


@dataclass(eq=False)
class _DiskLane:
    """Hàng FIFO đĩa của một lượt nhận (hợp đồng `core/transfer.py` mục 5)."""

    device_id: str
    transfer_id: str
    ops: deque[tuple[_LaneAction, bytearray | None]] = field(default_factory=deque)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    stop: threading.Event = field(default_factory=threading.Event)  # dừng `Reread` đang chạy
    temp: TransferTemp | None = None
    parts: dict[int, PartFile] = field(default_factory=dict)
    pending_writes: int = 0  # `WriteBlock` đang giữ payload, chưa ghi xong (kể cả đang chạy)
    failed: bool = False
    closing: bool = False  # `RemoveTemp` hay lượt đã kết thúc: chỉ còn dọn
    jobs: set[DiskJob[Any]] = field(default_factory=set)  # việc đã giao mà chưa `done`
    create_job: DiskJob[TransferTemp] | None = None
    waiters: set[asyncio.Event] = field(default_factory=set)  # bên đọc khung chờ chỗ trong hàng ghi


# ---- Kết nối ---------------------------------------------------------------------------------


@dataclass(eq=False)
class _DataSlot:
    """Một kết nối dữ liệu engine đang theo dõi (`data_id`)."""

    data_id: int
    device_id: str
    conn: Connection
    task: asyncio.Task[None] | None = None  # `SendBlocks` / `ReceiveBlocks`
    closed: bool = False
    engine_closed: bool = False  # engine đã phát `CloseData` (đã quên kết nối): không báo lại
    done: asyncio.Event = field(default_factory=asyncio.Event)
    wake: asyncio.Event = field(default_factory=asyncio.Event)  # bên đọc khung đang chờ chỗ ghi


class _Outbox:
    """Hàng gửi FIFO có trần của một kết nối điều khiển, một task gửi.

    Trần chỉ xét khi đang có message gửi dở (máy kia chưa đọc): loạt message của một lời gọi engine
    xếp vào lúc hàng đang rảnh (ví dụ `offer` của mọi lượt khi đồng bộ lại, protocol §6.3) không bị
    tính là tràn, nên số lượt dở với một thiết bị lớn hơn trần không làm kết nối mới bị cắt lặp mãi.
    """

    def __init__(self, device_id: str, conn: Connection) -> None:
        self._device_id = device_id
        self._conn = conn
        self._queue: deque[TransferMessage] = deque()
        self._wake = asyncio.Event()
        self.idle = asyncio.Event()  # hàng rỗng và không có message đang gửi
        self.idle.set()
        self.closed = False
        self._sending = False  # task gửi đang có một message gửi dở
        self._task = asyncio.create_task(self._run(), name=f"{_TASK_NAME}.transfer-outbox {device_id[:8]}")

    @property
    def task(self) -> asyncio.Task[None]:
        return self._task

    def put(self, msg: TransferMessage) -> None:
        if self.closed:
            return
        if self._sending and len(self._queue) >= CONTROL_SEND_QUEUE_MAX:
            # Máy kia gửi liên tục mà không đọc: hàng phình mãi. Cắt kết nối → mất kết nối điều
            # khiển, nối lại và đồng bộ lại như thường (architecture §5).
            log.warning(
                "hàng gửi tới %s quá %d message, cắt kết nối điều khiển",
                self._device_id[:8],
                CONTROL_SEND_QUEUE_MAX,
            )
            self._conn.abort()
            self.close()
            return
        self._queue.append(msg)
        self.idle.clear()
        self._wake.set()

    def close(self) -> None:
        """Bỏ hàng, dừng task gửi (đổi hay mất kết nối điều khiển, hoặc node đóng)."""
        self.closed = True
        self._queue.clear()
        self.idle.set()
        self._task.cancel()

    async def _run(self) -> None:
        try:
            while True:
                while not self._queue:
                    self.idle.set()
                    self._wake.clear()
                    await self._wake.wait()
                msg = self._queue.popleft()
                self._sending = True
                try:
                    await self._conn.send(msg)
                except ConnectionLostError:
                    return
                except ValueError:
                    log.exception("không gửi được message %s tới %s", type(msg).__name__, self._device_id[:8])
                finally:
                    self._sending = False
        finally:
            self.closed = True
            self._queue.clear()
            self.idle.set()


@dataclass(eq=False)
class _ReadJobs:
    """Việc đĩa của một `SendBlocks` chưa chắc đã xong: mở file nguồn, khối đang chờ, khối đọc trước."""

    open: DiskJob[SourceReader] | None = None
    current: DiskJob[tuple[bytearray, bytes]] | None = None
    ahead: DiskJob[tuple[bytearray, bytes]] | None = None


@dataclass(eq=False)
class _Control:
    control_id: int
    conn: Connection
    outbox: _Outbox


# ---- TransferManager -------------------------------------------------------------------------


class TransferManager:
    """Lượt truyền file của một node. Chỉ `Node` tạo; xem docstring module."""

    def __init__(
        self,
        *,
        identity: Identity,
        hello: Hello,
        store: TrustStore,
        executor: DiskExecutor,
        receive_dir: ReceiveDir | None,
        auto_accept: bool,
        rate_limit: int | None,
        on_event: Callable[[TransferUpdate], None] | None,
        new_id: Callable[[], int],
        set_hold: Callable[[str, bool], None],
    ) -> None:
        """Gọi trong event loop. `new_id` cấp `data_id`; `set_hold(device_id, held)` → `LinkTracker.hold`."""
        self._identity = identity
        self._hello = hello
        self._store = store
        self._executor = executor
        self._receive_dir = receive_dir
        self._on_event = on_event
        self._new_id = new_id
        self._set_hold = set_hold
        self._loop = asyncio.get_running_loop()
        self._engine = TransferEngine(identity.device_id, receiving=receive_dir is not None)
        self._auto_accept = False
        self.auto_accept = auto_accept
        self._rate_limit: int | None = None
        self._bucket: TokenBucket | None = None
        self.rate_limit = rate_limit
        self._send_slots = asyncio.Semaphore(MAX_LIMITED_SENDS)

        self._dispatching = False
        self._deferred: deque[_Deferred] = deque()
        self._controls: dict[str, _Control] = {}
        self._data: dict[int, _DataSlot] = {}
        self._lanes: dict[tuple[str, str], _DiskLane] = {}
        self._sources: dict[tuple[str, str], list[SourceFile]] = {}
        self._final_paths: dict[tuple[str, str, int], Path] = {}
        self._payloads: dict[tuple[str, str, int, int], bytearray] = {}
        self._unpaired: set[TransferKey] = set()
        self._held: set[str] = set()
        self._hold_dirty = False
        self._tasks: set[asyncio.Task[Any]] = set()  # mạng, kiểm chỗ trống / nguồn: hủy khi đóng
        self._disk_tasks: set[asyncio.Task[Any]] = set()  # hàng đĩa, dọn file nguồn: chờ có hạn khi đóng
        self._timer_wake = asyncio.Event()
        self._timer_deadline: float | None = None
        self._timer_task: asyncio.Task[None] | None = None
        self._progress_wake = asyncio.Event()
        self._progress_task: asyncio.Task[None] | None = None
        self._samples: dict[TransferKey, deque[tuple[float, int]]] = {}
        self._reported_done: dict[TransferKey, int] = {}
        self._closing = False  # begin_close() đã chạy: lời gọi công khai không làm gì
        self._closed = False  # close() xong: không phát sự kiện nữa

    def start(self) -> None:
        """Bật bộ hẹn giờ của engine và task tiến độ."""
        if self._timer_task is None:
            self._timer_task = asyncio.create_task(self._timer(), name=f"{_TASK_NAME}.transfer-timer")
            self._progress_task = asyncio.create_task(
                self._progress(), name=f"{_TASK_NAME}.transfer-progress"
            )

    # ---- API công khai ---------------------------------------------------------------------

    @property
    def receiving(self) -> bool:
        """Máy này có nhận `offer` mới không (có thư mục nhận và node chưa đóng)."""
        return self._engine.receiving

    @property
    def receive_dir(self) -> Path | None:
        """Thư mục nhận (đường dẫn tuyệt đối); None nếu node không nhận file."""
        return None if self._receive_dir is None else self._receive_dir.root

    @property
    def auto_accept(self) -> bool:
        """Tự nhận mọi `offer` (`receive --yes`); đổi lúc chạy áp cho `offer` hỏi sau đó."""
        return self._auto_accept

    @auto_accept.setter
    def auto_accept(self, value: bool) -> None:
        if type(value) is not bool:
            raise ValueError("auto_accept phải là bool")
        self._auto_accept = value

    @property
    def rate_limit(self) -> int | None:
        """Giới hạn tốc độ gửi chung (byte/giây); None: không giới hạn. Sai → ValueError."""
        return self._rate_limit

    @rate_limit.setter
    def rate_limit(self, value: int | None) -> None:
        if value is not None and (type(value) is not int or value < MIN_RATE_LIMIT):
            raise ValueError(f"rate_limit phải là None hoặc số nguyên ≥ {MIN_RATE_LIMIT} (byte/giây)")
        self._rate_limit = value
        self._bucket = None if value is None else TokenBucket(value, ratelimit.CHUNK_SIZE)

    async def send(self, device_id: str, paths: Sequence[str | os.PathLike[str]]) -> TransferKey:
        """Gửi các file `paths` tới thiết bị tin cậy `device_id`; trả khóa của lượt.

        Ném lỗi trước khi gửi gì: node chưa chạy hay đang đóng → RuntimeError; không tin cậy →
        KeyError; `device_id` sai hay là máy này, 0 hoặc quá `MAX_OFFER_FILES` file, một file chọn
        hai lần → ValueError; `paths` không phải dãy đường dẫn → TypeError; file không gửi được →
        `SourceError`. Sự kiện của lượt có thể tới trước khi hàm trả về.
        """
        self._check_send_target(device_id)
        job = self._executor.submit(sources.prepare_sources, paths)
        try:
            files = await _job_result(job, None)
        except _JobDropped:
            raise RuntimeError("node đang đóng") from None
        except asyncio.CancelledError:
            job.cancel()
            raise
        # Kiểm lại sau await, cùng khối đồng bộ với `engine.send` (node có thể đang đóng, máy kia
        # có thể vừa bị hủy ghép đôi).
        self._check_send_target(device_id)
        if self._dispatching:
            raise RuntimeError("send() không được gọi trong lúc đang thực hiện hành động của engine")
        entries = [f.entry for f in files]
        while True:
            transfer_id = uuid4().hex
            try:
                actions = self._engine.send(device_id, transfer_id, entries, self._loop.time())
            except TransferIdInUse:
                continue
            break
        key = TransferKey(device_id, Direction.SEND, transfer_id)
        self._sources[(device_id, transfer_id)] = files
        self._dispatch_now(actions)
        self._drain_deferred()
        return key

    def decide(self, key: TransferKey, accept: bool) -> None:
        """Trả lời `OfferReceived`. Sai kiểu → TypeError; lượt không biết hay sai pha → bỏ qua."""
        if not isinstance(key, TransferKey):
            raise TypeError("key phải là TransferKey")
        if type(accept) is not bool:
            raise TypeError("accept phải là bool")
        if self._closing or key.direction is not Direction.RECEIVE or self._engine.info(key) is None:
            return
        self._call(self._engine.decide, key.device_id, key.transfer_id, accept)

    def cancel(self, key: TransferKey) -> None:
        """Hủy lượt `key` (ở trạng thái nào cũng được). Sai kiểu → TypeError; không biết → bỏ qua."""
        if not isinstance(key, TransferKey):
            raise TypeError("key phải là TransferKey")
        if self._closing or self._engine.info(key) is None:
            return
        self._call(self._engine.cancel, key)

    def cancel_all(self) -> None:
        """Hủy mọi lượt chưa kết thúc."""
        if not self._closing:
            self._cancel_every()

    def get(self, key: TransferKey) -> TransferInfo | None:
        """Bản chụp lượt `key`; None nếu không biết hoặc đã kết thúc."""
        return self._engine.info(key)

    def transfers(self) -> tuple[TransferInfo, ...]:
        """Mọi lượt chưa kết thúc, theo thứ tự tạo."""
        return self._engine.transfers()

    # ---- Cho Node ---------------------------------------------------------------------------

    def control_up(self, device_id: str, control_id: int, conn: Connection, *, client: bool) -> None:
        """Kết nối điều khiển hiện tại của thiết bị là `conn` (`LinkUp` hoặc `LinkSwitched`)."""
        old = self._controls.pop(device_id, None)
        if old is not None:
            old.outbox.close()
        outbox = _Outbox(device_id, conn)
        self._tasks.add(outbox.task)
        outbox.task.add_done_callback(self._tasks.discard)
        self._controls[device_id] = _Control(control_id, conn, outbox)
        self._call(self._engine.control_up, device_id, control_id, client)

    def control_lost(self, device_id: str, control_id: int) -> None:
        """Kết nối điều khiển `control_id` đã mất (`LinkDown`)."""
        ctl = self._controls.get(device_id)
        if ctl is not None and ctl.control_id == control_id:
            del self._controls[device_id]
            ctl.outbox.close()
        self._call(self._engine.control_lost, device_id, control_id)

    def on_message(self, device_id: str, control_id: int, msg: object) -> None:
        """Message tới trên kết nối điều khiển `control_id`; loại không phải truyền file bị bỏ qua."""
        if isinstance(msg, _TRANSFER_MESSAGE_TYPES):
            self._call(self._engine.message, device_id, control_id, msg)

    def forget_device(self, device_id: str) -> None:
        """Hủy ghép đôi (đồng bộ, trong `Node.unpair`): hủy mọi lượt của thiết bị, kết quả `UNPAIRED`."""
        for info in self._engine.transfers():
            if info.key.device_id == device_id:
                # Thêm trước lời gọi: `TransferEnded` được báo ngay trong lời gọi đó.
                self._unpaired.add(info.key)
                if not self._call(self._engine.cancel, info.key):
                    self._unpaired.discard(info.key)

    async def serve_data(self, conn: Connection) -> None:
        """Kết nối dữ liệu vào (đã qua kiểm tin cậy): báo engine trước `await` đầu, giữ tới khi xong."""
        opening = conn.opening
        if self._closing or conn.peer.cert is None or not isinstance(opening, DataOpen):
            conn.abort()
            return
        device_id = conn.peer.cert.device_id
        slot = _DataSlot(self._new_id(), device_id, conn)
        self._data[slot.data_id] = slot
        if not self._call(self._engine.incoming_data, device_id, slot.data_id, opening):
            self._finish_slot(slot, abort=True)
            return
        try:
            await slot.done.wait()
        finally:
            if not slot.closed and slot.task is None:
                # Bị hủy (cổng đóng) khi engine còn giữ kết nối chờ `resume`: báo engine quên nó.
                self._finish_slot(slot, abort=True)
                if not slot.engine_closed:
                    self._call(self._engine.data_closed, device_id, slot.data_id, False)

    def begin_close(self) -> None:
        """Bước đầu của `Node.close` (đồng bộ): ngừng nhận `offer`, từ chối `send` mới, hủy mọi lượt."""
        if self._closing:
            return
        self._closing = True
        self._engine.receiving = False
        self._cancel_every()

    async def flush_control(self, timeout: float) -> None:
        """Chờ tối đa `timeout` giây cho mọi hàng gửi điều khiển trống (đẩy hết các `cancel`)."""
        outboxes = [c.outbox for c in self._controls.values() if not c.outbox.closed]
        if not outboxes:
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                for outbox in outboxes:
                    await outbox.idle.wait()

    async def close(self) -> None:
        """Hủy task mạng, chờ các hàng đĩa dọn xong tối đa `CLOSE_DISK_WAIT`, đóng thư mục nhận.

        `ReceiveDir.close()` chỉ chạy khi không còn thread đĩa nào bận; không thì bỏ qua (WARNING).
        Gọi lại được. Bị hủy giữa chừng → bên gọi dùng `abort_now()`.
        """
        if self._closed:
            return
        self.begin_close()
        for ctl in self._controls.values():
            ctl.outbox.close()
        tasks = [t for t in (self._timer_task, self._progress_task, *self._tasks) if t is not None]
        while tasks:
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks)
            # Task bị hủy có thể còn báo engine lúc kết thúc và sinh task mới: hủy nốt.
            tasks = [t for t in self._tasks if not t.done()]
        for lane in list(self._lanes.values()):
            self._close_lane(lane)
        disk = set(self._disk_tasks)
        if disk:
            _, pending = await asyncio.wait(disk, timeout=CLOSE_DISK_WAIT)
            for task in pending:
                task.cancel()
            if pending:
                log.warning("còn %d việc dọn đĩa chưa xong khi đóng", len(pending))
                await asyncio.wait(pending)
        self._shutdown_disk()
        self._closed = True

    def abort_now(self) -> None:
        """`Node.close` bị hủy: hủy mọi task, không chờ đĩa; không đóng thư mục nhận khi còn thread."""
        self._closing = True
        self._closed = True
        for ctl in self._controls.values():
            ctl.outbox.close()
        for task in (self._timer_task, self._progress_task, *self._tasks, *self._disk_tasks):
            if task is not None:
                task.cancel()
        for slot in list(self._data.values()):
            self._finish_slot(slot, abort=True)
        self._shutdown_disk()

    def _shutdown_disk(self) -> None:
        # shutdown() trước: việc chưa chạy bị bỏ, không thread nào nhận việc mới; sau đó busy == 0
        # nghĩa là không còn thread nào dùng ReceiveDir.
        self._executor.shutdown()
        rd = self._receive_dir
        if rd is None or rd.closed:
            return
        if self._executor.busy == 0:
            rd.close()
        else:
            log.warning("còn %d thao tác đĩa đang chạy: không đóng thư mục nhận", self._executor.busy)

    # ---- Gọi engine -------------------------------------------------------------------------

    def _call(self, method: Callable[..., list[TransferAction]], *args: Any, **kwargs: Any) -> bool:
        """Gọi engine với `now` đọc lúc gọi rồi thực hiện hành động; đang thực hiện thì hoãn.

        Trả False khi engine ném lỗi (đã ghi log); lời gọi bị hoãn trả True.
        """
        if self._dispatching:
            self._deferred.append((method, args, kwargs))
            return True
        ok = self._run(method, args, kwargs)
        self._drain_deferred()
        return ok

    def _drain_deferred(self) -> None:
        while self._deferred and not self._dispatching:
            method, args, kwargs = self._deferred.popleft()
            self._run(method, args, kwargs)

    def _run(
        self, method: Callable[..., list[TransferAction]], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> bool:
        try:
            actions = method(*args, self._loop.time(), **kwargs)
        except Exception:
            log.exception("TransferEngine.%s ném lỗi", getattr(method, "__name__", "?"))
            return False
        self._dispatch_now(actions)
        return True

    def _dispatch_now(self, actions: list[TransferAction]) -> None:
        """Thực hiện danh sách hành động đồng bộ, rồi cập nhật `hold` và bộ hẹn giờ."""
        self._dispatching = True
        try:
            for action in actions:
                try:
                    self._execute(action)
                except Exception:
                    log.exception("thực hiện %s lỗi", type(action).__name__)
        finally:
            self._dispatching = False
        self._update_holds()
        self._wake_timer()

    def _execute(self, action: TransferAction) -> None:
        if isinstance(action, SendMessage):
            ctl = self._controls.get(action.device_id)
            if ctl is not None and ctl.control_id == action.control_id:
                ctl.outbox.put(action.message)
        elif isinstance(action, WriteBlock):
            self._enqueue_write(action)
        elif isinstance(action, SyncPart | OpenPart | Reread | Finalize):
            self._enqueue_disk(action)
        elif isinstance(action, Report):
            self._report(action.event)
        elif isinstance(action, CloseData):
            slot = self._data.get(action.data_id)
            if slot is not None:
                slot.engine_closed = True
                self._finish_slot(slot, abort=True)
        elif isinstance(action, SendBlocks | ReceiveBlocks):
            self._start_data_task(action)
        elif isinstance(action, OpenData):
            self._open_data(action)
        elif isinstance(action, CreateTemp):
            self._create_lane(action)
        elif isinstance(action, RemoveTemp):
            lane = self._lanes.get((action.device_id, action.transfer_id))
            if lane is not None:
                self._close_lane(lane)
        elif isinstance(action, CheckSpace):
            self._spawn(self._check_space(action), f"transfer-check {action.transfer_id[:8]}")
        elif isinstance(action, CheckSource):
            self._spawn(self._check_source(action), f"transfer-check {action.transfer_id[:8]}")
        elif isinstance(action, AskUser):
            self._ask_user(action)

    def _cancel_every(self) -> None:
        for info in self._engine.transfers():
            self._call(self._engine.cancel, info.key)

    def _check_send_target(self, device_id: str) -> None:
        if self._closing:
            raise RuntimeError("node đang đóng")
        validate_device_id(device_id)
        if device_id == self._identity.device_id:
            raise ValueError("không gửi file cho chính máy này")
        if self._store.get(device_id) is None:
            raise KeyError(device_id)

    def _spawn(self, coro: Any, name: str, *, disk: bool = False) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=f"{_TASK_NAME}.{name}")
        tasks = self._disk_tasks if disk else self._tasks
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    def _update_holds(self) -> None:
        """Thiết bị còn lượt chưa kết thúc ↔ `hold` ở `core.links` (chỉ tính lại khi tập lượt đổi)."""
        if not self._hold_dirty:
            return
        self._hold_dirty = False
        active = {info.key.device_id for info in self._engine.transfers()}
        changes = [(d, True) for d in sorted(active - self._held)]
        changes += [(d, False) for d in sorted(self._held - active)]
        for device_id, held in changes:
            if held:
                self._held.add(device_id)
            else:
                self._held.discard(device_id)
            try:
                self._set_hold(device_id, held)
            except Exception:
                log.exception("không đổi được hold của %s", device_id[:8])

    def _wake_timer(self) -> None:
        wake = self._engine.next_wakeup()
        if wake is not None and (self._timer_deadline is None or wake < self._timer_deadline):
            self._timer_wake.set()

    async def _timer(self) -> None:
        """Gọi `engine.tick` lúc `next_wakeup()`."""
        while True:
            wake = self._engine.next_wakeup()
            self._timer_deadline = wake
            if wake is None:
                await self._timer_wake.wait()
            else:
                delay = wake - self._loop.time()
                if delay > 0:
                    with contextlib.suppress(TimeoutError):
                        async with asyncio.timeout(delay):
                            await self._timer_wake.wait()
            self._timer_wake.clear()
            due = self._engine.next_wakeup()
            if due is not None and due <= self._loop.time():
                self._call(self._engine.tick)

    # ---- Sự kiện ----------------------------------------------------------------------------

    def _emit(self, update: TransferUpdate) -> None:
        if self._closed or self._on_event is None:
            return
        try:
            self._on_event(update)
        except Exception:
            log.exception("on_transfer_event raise")

    def _report(self, event: TransferEvent) -> None:
        if isinstance(event, StatusChanged):
            self._hold_dirty = True
            self._progress_wake.set()
            info = self._engine.info(event.key)
            if info is not None:
                self._emit(TransferStatusChanged(info))
        elif isinstance(event, FileVerified):
            key = event.key
            path = None
            if key.direction is Direction.RECEIVE:
                path = self._final_paths.get((key.device_id, key.transfer_id, event.file_index))
            self._emit(FileDone(key, event.file_index, event.name, path))
        elif isinstance(event, TransferEnded):
            self._hold_dirty = True
            result = _result_of(event, event.key in self._unpaired)
            self._forget_transfer(event.key)
            ended = TransferFinished(
                event.info, result, event.by_peer, event.reason, event.file_index, event.reject
            )
            self._emit(ended)
        elif isinstance(event, DataUnreachable):
            self._emit(event)

    def _forget_transfer(self, key: TransferKey) -> None:
        """Lượt đã kết thúc: gỡ hàng đĩa, file nguồn, đường dẫn file cuối, dấu hủy ghép đôi."""
        pair = (key.device_id, key.transfer_id)
        self._unpaired.discard(key)
        self._samples.pop(key, None)
        self._reported_done.pop(key, None)
        if key.direction is Direction.SEND:
            self._sources.pop(pair, None)
            return
        lane = self._lanes.get(pair)
        if lane is not None:
            self._close_lane(lane)
        for path_key in [k for k in self._final_paths if k[:2] == pair]:
            del self._final_paths[path_key]

    def _ask_user(self, a: AskUser) -> None:
        key = TransferKey(a.device_id, Direction.RECEIVE, a.transfer_id)
        device = self._store.get(a.device_id)
        auto = self._auto_accept and not self._closing
        total = sum(entry.size for entry in a.offer.files)
        name = None if device is None else device.name
        self._emit(OfferReceived(key, name, a.offer.files, a.names, total, auto))
        if auto:
            self._loop.call_soon(self._auto_decide, a.device_id, a.transfer_id)

    def _auto_decide(self, device_id: str, transfer_id: str) -> None:
        if not self._closing:
            self._call(self._engine.decide, device_id, transfer_id, True)

    async def _progress(self) -> None:
        """Mỗi `PROGRESS_INTERVAL`: `TransferProgress` cho lượt có `done_bytes` đổi."""
        while True:
            if not self._engine.transfers():
                self._progress_wake.clear()
                await self._progress_wake.wait()
            await asyncio.sleep(PROGRESS_INTERVAL)
            now = self._loop.time()
            live: set[TransferKey] = set()
            for info in self._engine.transfers():
                key = info.key
                live.add(key)
                samples = self._samples.setdefault(key, deque())
                samples.append((now, info.done_bytes))
                while samples[0][0] < now - SPEED_WINDOW:
                    samples.popleft()
                if self._reported_done.get(key) == info.done_bytes:
                    continue
                self._reported_done[key] = info.done_bytes
                first_t, first_done = samples[0]
                span = now - first_t
                speed = max(0.0, (info.done_bytes - first_done) / span) if span > 0 else 0.0
                eta = (info.total_bytes - info.done_bytes) / speed if speed > 0 else None
                self._emit(TransferProgress(info, speed, eta))
            for key in [k for k in self._samples if k not in live]:
                del self._samples[key]
                self._reported_done.pop(key, None)

    # ---- Kết nối dữ liệu --------------------------------------------------------------------

    def _finish_slot(self, slot: _DataSlot, *, abort: bool) -> None:
        """Gỡ kết nối dữ liệu khỏi bảng, cắt nó (trừ khi đã đóng gọn), đánh thức bên chờ."""
        slot.closed = True
        if self._data.get(slot.data_id) is slot:
            del self._data[slot.data_id]
        if abort:
            slot.conn.abort()
        slot.wake.set()
        slot.done.set()

    def _open_data(self, a: OpenData) -> None:
        ctl = self._controls.get(a.device_id)
        device = self._store.get(a.device_id)
        if self._closing or ctl is None or ctl.control_id != a.control_id or device is None:
            # Engine không có hạn chờ cho `OpenData`: luôn báo kết quả (giao sau).
            self._call(self._engine.outgoing_data_failed, a.device_id, a.control_id, a.message)
            return
        peer = ctl.conn.peer
        self._spawn(
            self._open_data_task(a, peer.address, peer.port, device.cert_pem),
            f"transfer-open {a.message.transfer_id[:8]}",
        )

    async def _open_data_task(self, a: OpenData, address: str, port: int, cert_pem: str) -> None:
        device_id = a.device_id
        try:
            conn = await open_data(address, port, self._identity, self._hello, cert_pem, a.message)
        except (IdentityMismatch, VersionMismatch) as e:
            # Đúng địa chỉ:cổng của một kết nối điều khiển đã kiểm chứng chỉ mà danh tính hay phiên
            # bản khác: không phải tường lửa. Đóng kết nối điều khiển để lần nối lại báo đúng lỗi.
            log.warning(
                "kết nối dữ liệu tới %s: %s, đóng kết nối điều khiển", device_id[:8], type(e).__name__
            )
            ctl = self._controls.get(device_id)
            if ctl is not None and ctl.control_id == a.control_id:
                ctl.conn.abort()
            self._call(self._engine.outgoing_data_failed, device_id, a.control_id, a.message)
            return
        except (ValueError, TypeError):
            log.exception("lỗi khi mở kết nối dữ liệu tới %s", device_id[:8])
            self._call(self._engine.outgoing_data_failed, device_id, a.control_id, a.message)
            return
        except Exception as e:
            log.debug("không mở được kết nối dữ liệu tới %s: %s", device_id[:8], type(e).__name__)
            self._call(self._engine.outgoing_data_failed, device_id, a.control_id, a.message)
            return
        if self._closing:
            conn.abort()
            self._call(self._engine.outgoing_data_failed, device_id, a.control_id, a.message)
            return
        slot = _DataSlot(self._new_id(), device_id, conn)
        self._data[slot.data_id] = slot
        self._call(self._engine.outgoing_data, device_id, a.control_id, slot.data_id, a.message)
        if slot.task is None and not slot.closed:
            # Engine ném lỗi (đã ghi log) hoặc không giữ kết nối: không để nó treo.
            self._finish_slot(slot, abort=True)

    def _start_data_task(self, a: SendBlocks | ReceiveBlocks) -> None:
        slot = self._data.get(a.data_id)
        if slot is not None and slot.task is not None:
            log.error("kết nối dữ liệu %d đã có task", a.data_id)
            return
        if slot is None or slot.closed or self._closing:
            if slot is not None:
                self._finish_slot(slot, abort=True)
            self._call(self._engine.data_closed, a.device_id, a.data_id, False)
            return
        if isinstance(a, SendBlocks):
            slot.task = self._spawn(self._send_blocks(slot, a), f"transfer-send {a.transfer_id[:8]}")
        else:
            slot.task = self._spawn(self._receive_blocks(slot, a), f"transfer-recv {a.transfer_id[:8]}")

    async def _before_chunk(self, n: int) -> None:
        """Móc giới hạn tốc độ của `write_block` (công thức ở docstring `core/ratelimit.py`)."""
        bucket = self._bucket
        if bucket is None:
            return
        delay = bucket.delay(n, self._loop.time())
        if delay > 0:
            await asyncio.sleep(delay)
        bucket.take(n, self._loop.time())

    async def _acquire_send_slot(self, slot: _DataSlot) -> bool:
        """Có giới hạn tốc độ: chờ một chỗ trong `MAX_LIMITED_SENDS` (dừng chờ khi kết nối bị đóng)."""
        if self._bucket is None:
            return False
        while not slot.closed:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(0.5):
                    await self._send_slots.acquire()
                    return True
        return False

    async def _send_blocks(self, slot: _DataSlot, a: SendBlocks) -> None:
        """`SendBlocks` (hợp đồng mục 3). Kết thúc: cắt kết nối (trừ khi đã đóng gọn), báo engine."""
        device_id, data_id = a.device_id, a.data_id
        jobs = _ReadJobs()
        limited = False
        graceful = False
        result: tuple[str, bool] | None = None  # ("closed" | "source", cờ)
        try:
            limited = await self._acquire_send_slot(slot)
            if not slot.closed:
                graceful = await self._send_file(slot, a, jobs)
                if graceful:
                    result = ("closed", True)
        except ConnectionLostError:
            result = ("closed", False)
        except SourceChanged:
            result = ("source", True)
        except (OSError, DiskTimeout, _JobDropped) as e:
            label = _error_label(e)
            log.info("đọc file nguồn của lượt %s, file %d lỗi: %s", a.transfer_id[:8], a.file_index, label)
            result = ("source", False)
        except Exception:
            log.exception("lỗi khi gửi khối của lượt %s, file %d", a.transfer_id[:8], a.file_index)
            result = ("source", False)
        finally:
            if limited:
                self._send_slots.release()
            if jobs.open is not None:
                # File nguồn chỉ đóng sau khi mọi việc mở/đọc đã thật sự xong (`SourceReader` không
                # thread-safe; việc quá hạn vẫn chạy trong thread).
                pending = [j for j in (jobs.open, jobs.current, jobs.ahead) if j is not None]
                self._spawn(self._close_reader(jobs.open, pending), "transfer-source-close", disk=True)
            self._finish_slot(slot, abort=not graceful)
        if result is None or slot.engine_closed:
            return
        kind, flag = result
        if kind == "closed":
            self._call(self._engine.data_closed, device_id, data_id, flag)
        else:
            self._call(self._engine.source_failed, device_id, a.transfer_id, a.file_index, flag)

    async def _send_file(self, slot: _DataSlot, a: SendBlocks, jobs: _ReadJobs) -> bool:
        """Ghi khung cho mọi khối thiếu, đọc trước một khối trong lúc ghi khối hiện tại.

        True: đã ghi hết rồi đóng gọn; False: dừng vì engine đóng kết nối. Lỗi bay ra.
        """
        device_id, data_id = a.device_id, a.data_id
        files = self._sources.get((device_id, a.transfer_id))
        if files is None or not 0 <= a.file_index < len(files):
            raise RuntimeError("không có file nguồn của lượt")
        source = files[a.file_index]
        jobs.open = self._executor.submit(sources.SourceReader.open, source.path, source.entry)
        reader = await _job_result(jobs.open, DISK_OP_TIMEOUT)
        missing = a.have.missing()
        index = next(missing, None)
        if index is not None:
            jobs.ahead = self._executor.submit(reader.read_block, index)
        while index is not None and jobs.ahead is not None:
            jobs.current, jobs.ahead = jobs.ahead, None
            payload, digest = await _job_result(jobs.current, DISK_OP_TIMEOUT)
            following = next(missing, None)
            if following is not None:
                jobs.ahead = self._executor.submit(reader.read_block, following)
            if slot.closed:
                return False
            if not self._call(self._engine.block_read, device_id, data_id, index, digest):
                raise RuntimeError("engine từ chối block_read")
            if slot.closed:  # kết quả có `CloseData` của kết nối này: không ghi khung
                return False
            await slot.conn.write_block(index, digest, payload, before_chunk=self._before_chunk)
            del payload
            index = following
        await slot.conn.close()  # hết khối: đóng gọn, bên nhận đọc hết phần đuôi
        return True

    async def _close_reader(self, open_job: DiskJob[SourceReader], jobs: list[DiskJob[Any]]) -> None:
        """Đóng file nguồn sau khi mọi việc đọc đã thật sự xong (kể cả việc mở/đọc đã quá hạn)."""
        for job in jobs:
            await job.settled()
        reader = await _late_result(open_job)
        if reader is None:
            return
        try:
            job = self._executor.submit(reader.close)
        except RuntimeError:  # executor đã shutdown; mọi việc đọc đã xong ở trên
            reader.close()
            return
        await job.settled()
        if not job.started:  # bị shutdown bỏ trước khi chạy
            reader.close()

    async def _wait_write_room(self, slot: _DataSlot, pair: tuple[str, str]) -> None:
        """Chờ hàng ghi của lượt có chỗ (< `WRITE_QUEUE_BLOCKS`); thức cả khi kết nối / hàng đĩa đóng."""
        while not slot.closed:
            lane = self._lanes.get(pair)
            if lane is None or lane.failed or lane.closing or lane.pending_writes < WRITE_QUEUE_BLOCKS:
                return
            slot.wake.clear()
            lane.waiters.add(slot.wake)
            try:
                await slot.wake.wait()
            finally:
                lane.waiters.discard(slot.wake)

    async def _receive_blocks(self, slot: _DataSlot, a: ReceiveBlocks) -> None:
        """`ReceiveBlocks` (hợp đồng mục 4): đọc khung, so hash ngoài loop, báo engine."""
        device_id, data_id = a.device_id, a.data_id
        pair = (device_id, a.transfer_id)
        conn = slot.conn
        result: str | None = None  # "closed" | "bad"
        try:
            while not slot.closed:
                await self._wait_write_room(slot, pair)
                if slot.closed:
                    break
                try:
                    frame = await conn.read_block(size=a.size)
                except BlockError:
                    result = "bad"
                    break
                if frame is None:
                    result = "closed"
                    break
                header, payload = frame
                digest = await asyncio.to_thread(_sha256, payload)
                if slot.closed:
                    break
                if digest != header.sha256:
                    result = "bad"
                    break
                key = (device_id, a.transfer_id, a.file_index, header.index)
                self._payloads[key] = payload
                try:
                    self._call(self._engine.block_received, device_id, data_id, header.index, digest)
                finally:
                    # Engine không phát `WriteBlock` cho khối trùng, sai thứ tự hay kết nối đã quên:
                    # payload chỉ còn sống trong thao tác `WriteBlock` của hàng đĩa.
                    self._payloads.pop(key, None)
                del frame, payload
        except ConnectionLostError:
            result = "closed"
        except Exception:
            log.exception("lỗi khi nhận khối của lượt %s, file %d", a.transfer_id[:8], a.file_index)
            result = "closed"
        finally:
            self._finish_slot(slot, abort=True)
        if result is None or slot.engine_closed:
            return
        if result == "bad":
            self._call(self._engine.block_bad, device_id, data_id)
        else:
            self._call(self._engine.data_closed, device_id, data_id, False)

    # ---- Kiểm chỗ trống / file nguồn ---------------------------------------------------------

    async def _check_space(self, a: CheckSpace) -> None:
        free: int | None = None
        max_size: int | None = None
        rd = self._receive_dir
        if rd is not None and not self._closing:
            job = None
            try:
                job = self._executor.submit(rd.volume_info)
                info = await _job_result(job, DISK_OP_TIMEOUT)
            except asyncio.CancelledError:
                if job is not None:
                    job.cancel()
                raise
            except Exception as e:  # gồm DiskTimeout, `_JobDropped`, RuntimeError sau shutdown
                log.info("không đọc được dung lượng thư mục nhận: %s", _error_label(e))
            else:
                free, max_size = info.free_bytes, info.max_file_size
        self._call(self._engine.space_checked, a.device_id, a.transfer_id, free, max_size)

    async def _check_source(self, a: CheckSource) -> None:
        files = self._sources.get((a.device_id, a.transfer_id))
        if files is None or not 0 <= a.file_index < len(files) or self._closing:
            self._call(self._engine.source_failed, a.device_id, a.transfer_id, a.file_index, False)
            return
        job = None
        try:
            job = self._executor.submit(sources.check_source, files[a.file_index].path, a.leaves)
            size, mtime, root = await _job_result(job, DISK_OP_TIMEOUT)
        except asyncio.CancelledError:
            if job is not None:
                job.cancel()
            raise
        except SourceChanged:
            self._call(self._engine.source_failed, a.device_id, a.transfer_id, a.file_index, True)
            return
        except Exception as e:
            label = _error_label(e)
            log.info("kiểm file nguồn của lượt %s, file %d lỗi: %s", a.transfer_id[:8], a.file_index, label)
            self._call(self._engine.source_failed, a.device_id, a.transfer_id, a.file_index, False)
            return
        self._call(self._engine.source_checked, a.device_id, a.transfer_id, a.file_index, size, mtime, root)

    # ---- Hàng đĩa ---------------------------------------------------------------------------

    def _create_lane(self, a: CreateTemp) -> None:
        pair = (a.device_id, a.transfer_id)
        if pair in self._lanes:
            log.error("lượt %s đã có hàng đĩa", a.transfer_id[:8])
            self._call(self._engine.storage_failed, a.device_id, a.transfer_id, DiskOp.CREATE_TEMP)
            return
        lane = _DiskLane(a.device_id, a.transfer_id)
        self._lanes[pair] = lane
        if self._receive_dir is None or self._closing:
            # Không tạo được: hàng đĩa ở trạng thái lỗi, các thao tác sau của lượt bị bỏ im lặng
            # tới khi lượt kết thúc (engine nhận `storage_failed` đúng một lần).
            self._fail_lane(lane, DiskOp.CREATE_TEMP)
        else:
            lane.ops.append((a, None))
        self._spawn(self._run_lane(lane), f"transfer-disk {a.transfer_id[:8]}", disk=True)

    def _lane_for(self, action: _LaneAction) -> _DiskLane | None:
        lane = self._lanes.get((action.device_id, action.transfer_id))
        if lane is None:
            log.error("không có hàng đĩa cho %s của lượt %s", type(action).__name__, action.transfer_id[:8])
            op = _DISK_OPS[type(action)]
            self._call(self._engine.storage_failed, action.device_id, action.transfer_id, op)
        return lane

    def _enqueue_write(self, a: WriteBlock) -> None:
        payload = self._payloads.pop((a.device_id, a.transfer_id, a.file_index, a.index), None)
        lane = self._lane_for(a)
        if lane is None or lane.failed or lane.closing:
            return
        if payload is None:
            log.error("thiếu payload của khối %d, lượt %s", a.index, a.transfer_id[:8])
            self._fail_lane(lane, DiskOp.WRITE)
            return
        lane.ops.append((a, payload))
        lane.pending_writes += 1
        lane.wake.set()

    def _enqueue_disk(self, action: OpenPart | SyncPart | Reread | Finalize) -> None:
        lane = self._lane_for(action)
        if lane is None or lane.failed or lane.closing:
            return
        lane.ops.append((action, None))
        lane.wake.set()

    @staticmethod
    def _notify_lane(lane: _DiskLane) -> None:
        for waiter in list(lane.waiters):
            waiter.set()

    @staticmethod
    def _drop_ops(lane: _DiskLane) -> None:
        """Bỏ mọi thao tác chưa bắt đầu, trả lại chỗ trong hàng ghi."""
        dropped = sum(isinstance(action, WriteBlock) for action, _ in lane.ops)
        lane.ops.clear()
        lane.pending_writes -= dropped

    def _close_lane(self, lane: _DiskLane) -> None:
        """`RemoveTemp` hay lượt đã kết thúc: bỏ thao tác chưa chạy, dừng `Reread`, gỡ hàng.

        Task của hàng chờ thao tác đang chạy xong rồi mới xóa thư mục tạm (`_cleanup_lane`).
        """
        if self._lanes.get((lane.device_id, lane.transfer_id)) is lane:
            del self._lanes[(lane.device_id, lane.transfer_id)]
        if lane.closing:
            return
        lane.closing = True
        lane.stop.set()
        self._drop_ops(lane)
        self._notify_lane(lane)
        lane.wake.set()

    def _fail_lane(self, lane: _DiskLane, op: DiskOp, detail: FailureDetail | None = None) -> None:
        """Thao tác lỗi: bỏ các thao tác còn lại của lượt (trừ dọn), báo `storage_failed`."""
        lane.failed = True
        self._drop_ops(lane)
        self._notify_lane(lane)
        if not lane.closing:
            self._call(self._engine.storage_failed, lane.device_id, lane.transfer_id, op, detail=detail)

    async def _run_lane(self, lane: _DiskLane) -> None:
        while not lane.closing:
            if not lane.ops:
                lane.wake.clear()
                await lane.wake.wait()
                continue
            action, payload = lane.ops.popleft()
            try:
                if not lane.failed:
                    await self._lane_op(lane, action, payload)
            finally:
                del payload
                if isinstance(action, WriteBlock):
                    lane.pending_writes -= 1
                    self._notify_lane(lane)
        await self._cleanup_lane(lane)

    async def _lane_job[R](self, lane: _DiskLane, job: DiskJob[R], timeout: float) -> R:
        """Chờ một việc của hàng; việc chưa xong (quá hạn) ở lại `lane.jobs` để dọn chờ nó."""
        lane.jobs.add(job)
        try:
            return await _job_result(job, timeout)
        finally:
            if job.done:
                lane.jobs.discard(job)

    async def _lane_op(self, lane: _DiskLane, action: _LaneAction, payload: bytearray | None) -> None:
        device_id, transfer_id = lane.device_id, lane.transfer_id
        rd = self._receive_dir
        assert rd is not None
        submit = self._executor.submit
        try:
            if isinstance(action, CreateTemp):
                lane.create_job = submit(rd.create_temp, transfer_id)
                lane.temp = await self._lane_job(lane, lane.create_job, DISK_OP_TIMEOUT)
            elif isinstance(action, OpenPart):
                if lane.temp is None:
                    raise ValueError("chưa có thư mục tạm")
                job = submit(lane.temp.create_part, action.file_index, action.size)
                lane.parts[action.file_index] = await self._lane_job(lane, job, DISK_OP_TIMEOUT)
            elif isinstance(action, WriteBlock):
                part = self._part(lane, action.file_index)
                await self._lane_job(lane, submit(part.write_at, action.index, payload), DISK_OP_TIMEOUT)
            elif isinstance(action, SyncPart):
                part = self._part(lane, action.file_index)
                await self._lane_job(lane, submit(part.sync), DISK_SYNC_TIMEOUT)
                if not lane.closing:
                    self._call(self._engine.synced, device_id, transfer_id, action.file_index, action.token)
            elif isinstance(action, Reread):
                part = self._part(lane, action.file_index)
                n = len(action.leaves) // DIGEST_SIZE
                check = submit(_reread_compare, part, action.leaves, lane.stop)
                bad, root = await self._lane_job(lane, check, DISK_SYNC_TIMEOUT + n * REREAD_BLOCK_TIMEOUT)
                if not lane.closing:
                    self._call(self._engine.reread_done, device_id, transfer_id, action.file_index, bad, root)
            else:
                part = self._part(lane, action.file_index)
                finalize = functools.partial(rd.finalize, part, action.name, mtime_ms=action.mtime)
                path = await self._lane_job(lane, submit(finalize), DISK_SYNC_TIMEOUT)
                # `Finalize` đã bắt đầu thì chạy tới cùng (hợp đồng mục 5); lượt đã kết thúc giữa
                # chừng thì file vẫn mang tên thật, chỉ không báo nữa.
                if not lane.closing:
                    self._final_paths[(device_id, transfer_id, action.file_index)] = path
                    self._call(self._engine.finalized, device_id, transfer_id, action.file_index, path.name)
        except RereadStopped:
            return
        except TempExistsError:
            log.info("thư mục tạm của lượt %s đã có sẵn (sót lại từ lần trước)", transfer_id[:8])
            self._fail_lane(lane, DiskOp.CREATE_TEMP, FailureDetail.TEMP_EXISTS)
        except Exception as e:
            op = _DISK_OPS[type(action)]
            index = getattr(action, "file_index", None)
            if isinstance(e, ValueError | TypeError):
                log.exception("lỗi khi %s của lượt %s, file %s", op.value, transfer_id[:8], index)
            else:
                label = _error_label(e)
                log.warning("%s của lượt %s, file %s lỗi: %s", op.value, transfer_id[:8], index, label)
            self._fail_lane(lane, op)

    @staticmethod
    def _part(lane: _DiskLane, file_index: int) -> PartFile:
        part = lane.parts.get(file_index)
        if part is None:
            raise ValueError(f"chưa có .part của file {file_index}")
        return part

    async def _cleanup_lane(self, lane: _DiskLane) -> None:
        """Chờ mọi việc của hàng thật sự xong, rồi xóa thư mục tạm của chính lượt này (nếu có)."""
        lane.stop.set()
        self._drop_ops(lane)
        self._notify_lane(lane)
        for job in list(lane.jobs):
            await job.settled()
        lane.jobs.clear()
        temp = lane.temp
        if temp is None and lane.create_job is not None:
            # `create_temp` quá hạn rồi xong muộn: thư mục đó là của lượt này, xóa nó.
            temp = await _late_result(lane.create_job)
        lane.temp = None
        lane.parts.clear()
        rd = self._receive_dir
        if temp is None or rd is None:
            return
        # `remove_temp` đóng mọi `.part` của lượt (kể cả `.part` tạo muộn) rồi mới xóa.
        try:
            job = self._executor.submit(rd.remove_temp, temp)
        except RuntimeError:
            return  # executor đã shutdown (node đóng quá hạn): để lại thư mục tạm
        await job.settled()
        if not job.started:
            return  # bị shutdown bỏ trước khi chạy (node đóng quá hạn): để lại thư mục tạm
        try:
            await job.result(None)
        except Exception as e:
            log.debug("xóa thư mục tạm của lượt %s lỗi: %s", lane.transfer_id[:8], _error_label(e))
