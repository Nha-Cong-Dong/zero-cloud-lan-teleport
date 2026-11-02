"""Clipboard thật của hệ điều hành cho `app.clipboard.ClipboardSync`: `QtClipboardBackend` (mvp F8).

Đây là chỗ duy nhất của Teleport chạm `QClipboard`, và chỉ trên thread Qt. Hợp đồng `ClipboardBackend`
(gọi trên loop của node, `on_change` từ mọi thread, mọi lỗi coi như `FAILED`) ở docstring
`app.clipboard`.

**Thread** (docs/architecture.md mục 5):
- `QtClipboardBackend` dựng trên thread Qt (`ui.app` dựng một cái cho mỗi `NodeHost`, qua
  `NodeOptions.clipboard`, và gọi `dispose()` khi host đó phát `stopped`), nối `QClipboard.dataChanged`
  ngay trong `__init__`. Slot chỉ gọi `on_change` khi đã `start` và chưa `stop` (cờ có khóa); `stop()`
  chỉ đổi cờ, không chờ gì. Không dùng `QThreadPool.clear()` (PySide6 không nhả callable Python đã
  xếp): việc pool chưa chạy tự thoát khi tới lượt.
- `read()` / `write()` chạy trên loop của node: tạo Future, chuyển việc sang thread Qt qua signal nội
  bộ `_request` nối kiểu `QueuedConnection` (mẫu `NodeHost._relay`); kết quả về bằng
  `loop.call_soon_threadsafe`, chỉ đặt khi Future chưa xong (bị hủy vì quá hạn thì bỏ); loop đã đóng
  (`RuntimeError`) thì bỏ. QObject đã bị xóa (`RuntimeError` khi phát signal) → `FAILED`. Sau `stop()`
  mọi việc mới trả `FAILED`.
- Future xong (có kết quả, hay bị hủy vì quá hạn) đặt một `threading.Event` (`add_done_callback`):
  mỗi bước trên thread Qt và trong pool kiểm cờ này trước khi làm (và giữa các bước giải mã / mã hóa),
  đã đặt thì thoát, nhả dữ liệu; ghi ảnh đã quá hạn thì không `setImage`. Pool còn việc quá hạn chưa
  thoát thì việc ảnh mới trả `FAILED` ngay, không xếp thêm (ảnh 8192² chiếm 256 MiB mỗi bản).
- Giải mã / mã hóa PNG và tính fingerprint ảnh chạy trong `QThreadPool` riêng, `maxThreadCount = 1`;
  ngoài thread Qt chỉ có `QImage` (không bao giờ `QPixmap`). Đổi định dạng bằng `QImage.convertTo`
  tại chỗ (không giữ hai bản). `QMimeData` không giữ qua khỏi slot: tên định dạng và giá trị DWORD được
  chép ra ngay trong slot.

**Đọc** (thread Qt): `is_sensitive(formats, giá trị)` → `SENSITIVE` (trình quản lý mật khẩu; Windows
thấy tên định dạng dạng `application/x-qt-windows-mime;value="…"`); văn bản không rỗng (ưu tiên hơn
ảnh) dài hơn 2 × `MAX_TEXT_BYTES` code point → `TOO_LARGE`, còn lại `OK` (fingerprint để rỗng:
`ClipboardSync` tự tính); không có chữ mà có ảnh → `clipboard.image()`, cạnh > `MAX_IMAGE_SIDE` →
`TOO_LARGE` (cạnh ảnh cục bộ chỉ biết sau `clipboard.image()`, việc chuyển DIB 8K trên Windows có thể làm
đứng thread Qt cỡ giây; chưa đo trên Windows thật, số đo nền offscreen ở docs/benchmark.md mục 9.4). Ảnh vẫn
là đúng dữ liệu của lần ghi ảnh gần nhất (cùng `QImage.cacheKey()`: app đang giữ clipboard, đây là vọng của
lần ghi) → `OK` với `ClipContent(PNG, b"", fingerprint đã ghi)`, không mã hóa: `ClipboardSync` chỉ cập nhật
`LoopGuard` và không gửi (hợp đồng ở docstring `teleport.app.clipboard`). Không thì sang pool: đổi về
`RGBA8888`, bỏ đệm hàng nếu có, `image_fingerprint(w, h, "RGBA8888", pixels)` (từ `memoryview` của
`constBits()` khi không có đệm); **kiểm sơ bộ**: nén dòng các hàng pixel bằng
`zlib.compressobj(PRESCREEN_ZLIB_LEVEL)` theo khối `PRESCREEN_CHUNK_BYTES` (kiểm cờ hủy giữa các khối), đầu
ra vượt `PRESCREEN_LIMIT_BYTES` (= 2 × `MAX_PNG_BYTES`, nới để ảnh mà PNG có bộ lọc vừa trần không bị từ
chối nhầm) thì dừng sớm và trả `TOO_LARGE` (ảnh nhiễu khó nén không bao giờ được mã hóa PNG đầy đủ); qua
được thì mã hóa PNG qua `QBuffer` với mức nén mặc định của Qt (hạ mức nén chỉ nhanh hơn ~10 % mà PNG lớn hơn
1,4–3 lần, dễ thành `TOO_LARGE`; phần chậm là libpng xử lý từng hàng, benchmark.md mục 9.4);
PNG > `MAX_PNG_BYTES` → `TOO_LARGE` (kiểm cỡ bộ đệm trước khi chép ra `bytes`).
Không có gì → `EMPTY` (không định dạng nào) / `UNSUPPORTED`.

**Ghi**: văn bản trên thread Qt: `setText`, đọc lại `text()` → `text_fingerprint`; đọc lại rỗng (ứng
dụng khác đang giữ clipboard) thì thử lại tối đa `WRITE_RETRIES` lần, cách `WRITE_RETRY_MS` (`QTimer`,
không chặn), rồi `FAILED`. Ảnh: trong pool `QImageReader` trên `QBuffer`, định dạng ép “png”,
`setAllocationLimit(IMAGE_ALLOCATION_LIMIT_MB)`; đọc `size()` từ header **trước** khi giải mã, không hợp
lệ hay cạnh > `MAX_IMAGE_SIDE` → `REJECTED` (bom giải nén không được giải mã); `read()` rỗng (hỏng,
cụt, vượt trần cấp phát — PNG 16 bit 8192² bị từ chối, chấp nhận) → `REJECTED`; đổi `RGBA8888`, tính
fingerprint; về thread Qt `setImage`, lấy `clipboard.image()`; đọc lại rỗng → `FAILED`; đọc lại cùng
dữ liệu (cùng `cacheKey()`) → fingerprint đọc lại = fingerprint đã ghi, không qua pool lần hai; khác thì
về pool tính fingerprint đọc lại; trả `(fingerprint, fingerprint đọc lại)` + `image_size`. Không polling:
Wayland không báo `dataChanged` khi app không có tiêu điểm, macOS có thể chỉ báo khi app được kích hoạt
(ghi ở docs/ke_hoach.md mục 3.6).

Không ghi gì ra đĩa; log chỉ có tên loại lỗi (mvp N9).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import threading
import zlib
from collections.abc import Callable
from typing import cast

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QClipboard, QGuiApplication, QImage, QImageReader, QImageWriter

from teleport.app.clipboard import (
    ClipContent,
    ClipRead,
    ClipWrite,
    ReadStatus,
    WriteStatus,
)
from teleport.core.clipboard import (
    MAX_IMAGE_SIDE,
    MAX_PNG_BYTES,
    MAX_TEXT_BYTES,
    image_fingerprint,
    is_sensitive,
    text_fingerprint,
)
from teleport.core.messages import ClipKind

log = logging.getLogger(__name__)

PIXEL_FORMAT = "RGBA8888"  # định dạng pixel cố định của fingerprint ảnh (hợp đồng `core.clipboard`)
IMAGE_ALLOCATION_LIMIT_MB = 300  # trần cấp phát của `QImageReader` (8192² RGBA8888 = 256 MiB)
WRITE_RETRIES = 3  # lần thử lại khi đọc lại văn bản vừa ghi ra rỗng
WRITE_RETRY_MS = 50
PRESCREEN_ZLIB_LEVEL = 1  # mức zlib của bước kiểm sơ bộ ảnh trước khi mã hóa PNG
PRESCREEN_CHUNK_BYTES = 4 * 1024 * 1024  # mỗi lần nén bao nhiêu byte pixel (kiểm cờ hủy giữa các khối)
PRESCREEN_LIMIT_BYTES = 2 * MAX_PNG_BYTES  # đầu ra kiểm sơ bộ vượt mức này → `TOO_LARGE`, không mã hóa PNG
DISPOSE_RETRY_MS = 100  # `dispose()`: pool còn việc đang chạy thì hẹn lại sau chừng này

_Result = ClipRead | ClipWrite
_Cancelled = Callable[[], bool]


class _Reply:
    """Trả kết quả về Future trên loop của node (gọi được từ mọi thread, chỉ lần đầu có tác dụng).

    `dead`: Future đã xong (có kết quả, bị hủy vì quá hạn hay node đóng); cờ `threading.Event` đặt bằng
    `add_done_callback` trên loop, đọc được từ mọi thread. `sent`: chính backend đã trả kết quả."""

    __slots__ = ("_dead", "_future", "_loop", "sent")

    def __init__(self, loop: asyncio.AbstractEventLoop, future: asyncio.Future[_Result]) -> None:
        self._loop = loop
        self._future = future
        self._dead = threading.Event()
        self.sent = False
        future.add_done_callback(self._on_done)  # dựng trên loop nên gọi được ở đây

    def _on_done(self, _future: asyncio.Future[_Result]) -> None:
        self._dead.set()

    @property
    def dead(self) -> bool:
        return self._dead.is_set()

    def __call__(self, result: _Result) -> None:
        self.sent = True
        with contextlib.suppress(RuntimeError):  # loop đã đóng: không còn ai chờ
            self._loop.call_soon_threadsafe(self._set, result)

    def _set(self, result: _Result) -> None:
        if not self._future.done():  # đã hủy (quá hạn, node đóng) thì bỏ kết quả
            self._future.set_result(result)


def _rgba(image: QImage) -> QImage:
    """`image` đổi **tại chỗ** sang RGBA8888 (`convertTo`: không giữ thêm một bản; ảnh đang dùng chung
    ngầm thì tự tách). Bên gọi phải là chủ của `image`."""
    if image.format() != QImage.Format.Format_RGBA8888:
        image.convertTo(QImage.Format.Format_RGBA8888)
    return image


def _fingerprint(image: QImage) -> str:
    """Fingerprint theo pixel của `image` đã ở `RGBA8888` (các hàng nối liền, không đệm)."""
    width, height = image.width(), image.height()
    row = 4 * width
    stride = image.bytesPerLine()
    bits = image.constBits()
    view = bits if isinstance(bits, memoryview) else memoryview(bits)
    if stride == row:
        pixels = view[: row * height]
    else:
        pixels = memoryview(b"".join(view[y * stride : y * stride + row] for y in range(height)))
    # `image_fingerprint` chỉ đưa `pixels` vào `hashlib`, nhận được memoryview (không chép 256 MiB).
    return image_fingerprint(width, height, PIXEL_FORMAT, cast(bytes, pixels))


def _never() -> bool:
    return False


def prescreen(image: QImage, cancelled: _Cancelled = _never) -> int | None:
    """Kiểm sơ bộ (pool): số byte nén dòng các hàng pixel của `image` bằng zlib mức
    `PRESCREEN_ZLIB_LEVEL`, theo khối `PRESCREEN_CHUNK_BYTES`; dừng ngay khi vượt
    `PRESCREEN_LIMIT_BYTES` (trả số đã đếm, > trần). `cancelled()` True giữa các khối → None."""
    limit = PRESCREEN_LIMIT_BYTES
    stride = image.bytesPerLine()
    total = stride * image.height()
    bits = image.constBits()
    view = bits if isinstance(bits, memoryview) else memoryview(bits)
    step = max(stride, PRESCREEN_CHUNK_BYTES // max(1, stride) * stride)
    compressor = zlib.compressobj(PRESCREEN_ZLIB_LEVEL)
    out = 0
    for start in range(0, total, step):
        if cancelled():
            return None
        out += len(compressor.compress(view[start : min(total, start + step)]))
        if out > limit:
            return out
    return out + len(compressor.flush())


def encode_image(image: QImage, cancelled: _Cancelled = _never) -> ClipRead:
    """Ảnh clipboard máy này → `ClipRead` (chạy trong pool): RGBA8888 (tại chỗ: bên gọi nhường
    `image`), fingerprint, kiểm sơ bộ (`prescreen`), mã hóa PNG. `cancelled()` True giữa các bước →
    `FAILED` ngay."""
    rgba = _rgba(image)
    if rgba.isNull() or cancelled():
        return ClipRead(ReadStatus.FAILED)
    fingerprint = _fingerprint(rgba)
    if cancelled():
        return ClipRead(ReadStatus.FAILED)
    estimate = prescreen(rgba, cancelled)
    if estimate is None:
        return ClipRead(ReadStatus.FAILED)
    if estimate > PRESCREEN_LIMIT_BYTES:
        return ClipRead(ReadStatus.TOO_LARGE, size=estimate)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QImageWriter(buffer, QByteArray(b"png"))
    if not writer.write(rgba) or cancelled():
        return ClipRead(ReadStatus.FAILED)
    size = buffer.size()
    if size > MAX_PNG_BYTES:  # trước khi chép ra `bytes`: không chép bản quá trần
        return ClipRead(ReadStatus.TOO_LARGE, size=size)
    png = bytes(buffer.data().data())
    buffer.close()
    if not png:
        return ClipRead(ReadStatus.FAILED)
    return ClipRead(ReadStatus.OK, ClipContent(ClipKind.PNG, png, fingerprint), len(png))


def decode_png(png: bytes, cancelled: _Cancelled = _never) -> tuple[QImage, str] | None:
    """PNG nhận được → (ảnh RGBA8888, fingerprint) hay None (`REJECTED`; hay `cancelled()` True giữa các
    bước), chạy trong pool.

    Đọc kích thước ở header **trước** khi giải mã: không hợp lệ hay cạnh > `MAX_IMAGE_SIDE` thì không
    giải mã. Trần cấp phát `IMAGE_ALLOCATION_LIMIT_MB`."""
    buffer = QBuffer()
    buffer.setData(QByteArray(png))
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        return None
    try:
        reader = QImageReader(buffer, QByteArray(b"png"))
        reader.setDecideFormatFromContent(False)
        reader.setAllocationLimit(IMAGE_ALLOCATION_LIMIT_MB)
        size = reader.size()
        width, height = size.width(), size.height()
        if not size.isValid() or not (1 <= width <= MAX_IMAGE_SIDE and 1 <= height <= MAX_IMAGE_SIDE):
            return None
        if cancelled():
            return None
        image = reader.read()
    finally:
        buffer.close()
    if image.isNull() or cancelled():
        return None
    rgba = _rgba(image)
    if rgba.isNull() or rgba.width() != width or rgba.height() != height or cancelled():
        return None
    return rgba, _fingerprint(rgba)


class QtClipboardBackend(QObject):
    """`ClipboardBackend` trên `QClipboard` (xem docstring module). Dựng trên thread Qt.

    `clipboard`: mặc định `QGuiApplication.clipboard()` (test có thể truyền cái khác)."""

    _request = Signal(object)  # hàm không tham số, chạy trên thread Qt

    def __init__(self, parent: QObject | None = None, *, clipboard: QClipboard | None = None) -> None:
        super().__init__(parent)
        self._clipboard = clipboard if clipboard is not None else QGuiApplication.clipboard()
        self._guard = threading.Lock()
        self._on_change: Callable[[], None] | None = None  # đọc/ghi khi giữ `_guard`
        self._stopped = False  # đọc/ghi khi giữ `_guard`
        self._in_pool: dict[object, _Reply] = {}  # việc đã đưa vào pool, chưa xong; giữ `_guard`
        # Ảnh vừa ghi còn trên clipboard: (`QImage.cacheKey()` đọc lại, fingerprint); giữ `_guard`.
        self._echo: tuple[int, str] | None = None
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._request.connect(self._on_request, Qt.ConnectionType.QueuedConnection)
        self._clipboard.dataChanged.connect(self._on_data_changed)

    # ---- Hợp đồng `ClipboardBackend` (loop của node) ---------------------------------------------

    def start(self, on_change: Callable[[], None]) -> None:
        with self._guard:
            if not self._stopped:
                self._on_change = on_change

    def stop(self) -> None:
        """Chỉ đổi cờ (có khóa); việc pool chưa chạy tự thoát khi tới lượt (không `QThreadPool.clear()`)."""
        with self._guard:
            self._stopped = True
            self._on_change = None
            self._echo = None

    async def read(self) -> ClipRead:
        result = await self._run(self._read_on_qt)
        return result if isinstance(result, ClipRead) else ClipRead(ReadStatus.FAILED)

    async def write(self, kind: ClipKind, data: str | bytes) -> ClipWrite:
        if kind == ClipKind.TEXT and isinstance(data, str):
            result = await self._run(lambda reply: self._write_text_on_qt(data, reply, 0))
        elif kind == ClipKind.PNG and isinstance(data, bytes):
            result = await self._run(lambda reply: self._write_image_on_qt(data, reply))
        else:
            return ClipWrite(WriteStatus.REJECTED)
        return result if isinstance(result, ClipWrite) else ClipWrite(WriteStatus.FAILED)

    # ---- Thread Qt: dọn ---------------------------------------------------------------------------

    def dispose(self) -> None:
        """Thread Qt, sau khi node dùng backend này đã dừng: `stop()` rồi `deleteLater()` khi pool không
        còn việc đang chạy (còn thì hẹn lại sau `DISPOSE_RETRY_MS`; không chờ chặn)."""
        self.stop()
        try:
            busy = self._pool.activeThreadCount() > 0
        except RuntimeError:  # đã bị xóa
            return
        if busy:
            QTimer.singleShot(DISPOSE_RETRY_MS, self, self.dispose)
        else:
            self.deleteLater()

    # ---- Nội bộ: chuyển việc giữa các thread ------------------------------------------------------

    @property
    def stopped(self) -> bool:
        with self._guard:
            return self._stopped

    async def _run(self, job: Callable[[_Reply], None]) -> _Result:
        """Loop: chạy `job(reply)` trên thread Qt, chờ kết quả (bên gọi đặt hạn và có thể hủy)."""
        if self.stopped:
            return ClipRead(ReadStatus.FAILED)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_Result] = loop.create_future()
        reply = _Reply(loop, future)
        try:
            self._request.emit(lambda: self._guarded(job, reply))
        except RuntimeError:  # QObject đã bị xóa
            return ClipRead(ReadStatus.FAILED)
        return await future

    def _guarded(self, job: Callable[[_Reply], None], reply: _Reply) -> None:
        """Thread Qt: chạy `job`; bên chờ đã thôi chờ → bỏ; đã `stop` hay lỗi → `FAILED`."""
        if reply.dead:
            return
        if self.stopped:
            reply(ClipRead(ReadStatus.FAILED))
            return
        try:
            job(reply)
        except Exception as e:
            log.warning("clipboard Qt lỗi: %s", type(e).__name__)
            reply(ClipRead(ReadStatus.FAILED))

    def _post(self, fn: Callable[[], None], reply: _Reply) -> None:
        """Từ thread pool: đưa `fn` về thread Qt; QObject đã bị xóa → `FAILED`."""
        try:
            self._request.emit(lambda: self._guarded(lambda _r: fn(), reply))
        except RuntimeError:
            reply(ClipRead(ReadStatus.FAILED))

    def _stale_in_pool(self) -> bool:
        """Còn việc trong pool mà bên chờ đã bỏ (quá hạn) trước khi có kết quả, và chưa thoát."""
        with self._guard:
            return any(r.dead and not r.sent for r in self._in_pool.values())

    def _pool_start(self, fn: Callable[[], None], reply: _Reply) -> None:
        """Thread Qt: chạy `fn` trong pool một luồng; lỗi trong `fn` → `FAILED`.

        Còn việc quá hạn chưa thoát trong pool → `FAILED` ngay, không xếp thêm. Tới lượt mà đã `stop`
        → `FAILED`; bên chờ đã bỏ → thoát ngay (closure, cùng dữ liệu nó giữ, được nhả)."""
        if self._stale_in_pool():
            log.debug("pool clipboard còn việc quá hạn, bỏ việc mới")
            reply(ClipRead(ReadStatus.FAILED))
            return
        token = object()

        def run() -> None:
            try:
                if self.stopped:
                    reply(ClipRead(ReadStatus.FAILED))
                    return
                if reply.dead:
                    return
                fn()
            except Exception as e:
                log.warning("xử lý ảnh clipboard lỗi: %s", type(e).__name__)
                reply(ClipRead(ReadStatus.FAILED))
            finally:
                with self._guard:
                    self._in_pool.pop(token, None)

        with self._guard:
            self._in_pool[token] = reply
        self._pool.start(run)

    @Slot(object)
    def _on_request(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:
            log.warning("clipboard Qt lỗi: %s", type(e).__name__)

    def _on_data_changed(self) -> None:
        with self._guard:
            callback = None if self._stopped else self._on_change
        if callback is None:
            return
        try:
            callback()
        except Exception as e:
            log.warning("báo clipboard đổi lỗi: %s", type(e).__name__)

    # ---- Nội bộ: đọc ------------------------------------------------------------------------------

    def _read_on_qt(self, reply: _Reply) -> None:
        mime = self._clipboard.mimeData()
        if mime is None:
            reply(ClipRead(ReadStatus.EMPTY))
            return
        formats = [str(name) for name in mime.formats()]
        if is_sensitive(formats, lambda name: bytes(mime.data(name).data()) or None):
            reply(ClipRead(ReadStatus.SENSITIVE))
            return
        if mime.hasText():
            text = mime.text()
            if text:
                if len(text) > 2 * MAX_TEXT_BYTES:
                    reply(ClipRead(ReadStatus.TOO_LARGE, size=len(text)))
                else:
                    reply(ClipRead(ReadStatus.OK, ClipContent(ClipKind.TEXT, text, ""), len(text)))
                return
        if mime.hasImage():
            image = self._clipboard.image()
            if image.isNull():
                reply(ClipRead(ReadStatus.UNSUPPORTED))
                return
            width, height = image.width(), image.height()
            if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
                reply(ClipRead(ReadStatus.TOO_LARGE, size=4 * width * height))
                return
            with self._guard:
                echo = self._echo
            if echo is not None and image.cacheKey() == echo[0]:
                # Vọng của lần ghi ảnh gần nhất: không mã hóa PNG (docstring module, “Đọc”).
                reply(ClipRead(ReadStatus.OK, ClipContent(ClipKind.PNG, b"", echo[1])))
                return
            self._pool_start(lambda: reply(encode_image(image, lambda: reply.dead)), reply)
            return
        reply(ClipRead(ReadStatus.EMPTY if not formats else ReadStatus.UNSUPPORTED))

    # ---- Nội bộ: ghi ------------------------------------------------------------------------------

    def _set_echo(self, echo: tuple[int, str] | None) -> None:
        with self._guard:
            self._echo = None if self._stopped else echo

    def _write_text_on_qt(self, text: str, reply: _Reply, attempt: int = 0) -> None:
        self._set_echo(None)
        self._clipboard.setText(text)
        back = self._clipboard.text()
        if back:
            reply(ClipWrite(WriteStatus.OK, (text_fingerprint(back),)))
            return
        if attempt >= WRITE_RETRIES:
            reply(ClipWrite(WriteStatus.FAILED))
            return
        retry = functools.partial(self._write_text_on_qt, text, attempt=attempt + 1)
        QTimer.singleShot(WRITE_RETRY_MS, self, lambda: self._guarded(retry, reply))

    def _write_image_on_qt(self, png: bytes, reply: _Reply) -> None:
        self._pool_start(lambda: self._decode_job(png, reply), reply)

    def _decode_job(self, png: bytes, reply: _Reply) -> None:
        """Pool: giải mã PNG nhận được rồi về thread Qt để ghi."""
        decoded = decode_png(png, lambda: reply.dead)
        if reply.dead:
            return
        if decoded is None:
            reply(ClipWrite(WriteStatus.REJECTED))
            return
        image, fingerprint = decoded
        self._post(lambda: self._set_image_on_qt(image, fingerprint, reply), reply)

    def _set_image_on_qt(self, image: QImage, fingerprint: str, reply: _Reply) -> None:
        """Thread Qt (qua `_guarded`: bên chờ đã bỏ thì không ghi): `setImage`, đọc lại."""
        self._set_echo(None)
        self._clipboard.setImage(image)
        back = self._clipboard.image()
        if back.isNull():
            reply(ClipWrite(WriteStatus.FAILED))
            return
        size = (image.width(), image.height())
        key = back.cacheKey()
        if key == image.cacheKey():  # clipboard giữ đúng ảnh vừa đặt: fingerprint đọc lại = fingerprint
            self._set_echo((key, fingerprint))
            reply(ClipWrite(WriteStatus.OK, (fingerprint,), size))
            return
        # Đặt vọng ngay: việc trong pool có thể không chạy (quá hạn) mà ảnh đã nằm trên clipboard.
        self._set_echo((key, fingerprint))

        def job() -> None:
            result = _written(fingerprint, back, size, lambda: reply.dead)
            if result.status is WriteStatus.OK:
                self._set_echo((key, result.fingerprints[-1]))
            reply(result)

        self._pool_start(job, reply)


def _written(
    fingerprint: str, back: QImage, size: tuple[int, int], cancelled: _Cancelled = _never
) -> ClipWrite:
    """Pool: kết quả ghi ảnh với fingerprint của ảnh đọc lại (cạnh lạ thì chỉ fingerprint đã ghi).

    `back` đổi tại chỗ sang RGBA8888 (bên gọi nhường ảnh); `cancelled()` True → `FAILED`."""
    if cancelled():
        return ClipWrite(WriteStatus.FAILED)
    fingerprints = [fingerprint]
    rgba = _rgba(back)
    if cancelled():
        return ClipWrite(WriteStatus.FAILED)
    if not rgba.isNull() and 1 <= rgba.width() <= MAX_IMAGE_SIDE and 1 <= rgba.height() <= MAX_IMAGE_SIDE:
        again = _fingerprint(rgba)
        if again != fingerprint:
            fingerprints.append(again)
    return ClipWrite(WriteStatus.OK, tuple(fingerprints), size)
