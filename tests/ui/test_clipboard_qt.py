"""Backend clipboard Qt `teleport.ui.clipboard_qt.QtClipboardBackend` (spec D5 mục 3.1, ca test #7, mục 9).

Viết từ spec, không từ code. Backend dựng trên thread Qt (thread chính của pytest-qt); `start`, `read`,
`write` chạy trên **một event loop asyncio ở thread khác**, như loop của node thật (`NodeHost`). Test chờ
Future của loop bằng `qtbot.waitUntil` để vòng sự kiện Qt vẫn chạy (backend chuyển việc sang Qt bằng
signal `QueuedConnection`, kết quả về loop bằng `call_soon_threadsafe`).

Phủ:
- Đọc: văn bản sau `setText` (ưu tiên văn bản khi có cả ảnh); rỗng / định dạng lạ → EMPTY hay UNSUPPORTED;
  văn bản dài đúng 2 × `MAX_TEXT_BYTES` → OK, thêm 1 → TOO_LARGE; ảnh → PNG + `image_fingerprint` theo pixel
  RGBA8888 (tính độc lập từ byte pixel test tự dựng); ảnh cạnh > `MAX_IMAGE_SIDE` → TOO_LARGE.
- Nhạy cảm qua `QMimeData.setData`: tên trần và dạng `application/x-qt-windows-mime;value="…"`; định dạng
  “có mặt” → SENSITIVE; DWORD 0 → SENSITIVE, DWORD 1 → đọc bình thường (ca đối chứng: chỉ khác giá trị
  DWORD), DWORD < 4 byte → SENSITIVE.
- Ghi: văn bản → clipboard có đúng chữ, `fingerprints` chứa `text_fingerprint`; ảnh PNG (test tự mã hóa
  bằng `zlib`, không qua Qt) → OK, `image_size`, fingerprint đúng pixel; mã hóa PNG khác nhau (bộ lọc dòng,
  mức nén, RGB/RGBA, chunk phụ) cùng pixel → cùng fingerprint; PNG đọc từ clipboard ghi lại → cùng
  fingerprint. Header 50000 × 50000 → REJECTED trong < 1 s và **không** gọi `QImageReader.read` /
  `QImage.fromData` / `loadFromData`; 8193 × 1 → REJECTED, 8192 × 1 → OK; PNG hỏng / không phải PNG /
  thiếu IDAT → REJECTED; REJECTED không đổi clipboard.
- `on_change`: không gọi trước `start` và sau `stop`; gọi khi clipboard đổi sau `start`.
- 9.3 điểm 7: QObject của backend đã bị xóa → `read`/`write` trả FAILED, không ném, không treo.
- Sửa sau soat-code / tan-cong: ghi văn bản thử lại `WRITE_RETRIES` lần; việc pool quá hạn chặn việc ảnh
  mới và không `setImage`; `stop()` không `clear()`, việc đang xếp tự thoát; `convertTo` tại chỗ; đọc vọng
  sau khi ghi ảnh không mã hóa PNG.
- Kiểm sơ bộ (zlib theo khối) trước khi mã hóa PNG: ảnh nhiễu lớn → TOO_LARGE nhanh, không gọi
  `QImageWriter.write`; ảnh dễ nén vẫn OK; kiểm cờ hủy giữa các khối.

Giả định (spec không nêu tên): dựng bằng `QtClipboardBackend()` không đối số bắt buộc.
"""

from __future__ import annotations

import asyncio
import struct
import threading
import time
import zlib
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QByteArray, QMimeData, QObject, Signal
from PySide6.QtGui import QGuiApplication, QImage

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
WAIT_MS = 10_000


# =================================================================================================
# PNG tự mã hóa (không qua Qt): ca kiểm độc lập với bộ mã hóa của backend
# =================================================================================================


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _ihdr(width: int, height: int, color_type: int = 6) -> bytes:
    return _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0))


def make_pixels(width: int, height: int, *, opaque: bool = True) -> bytes:
    """Byte pixel RGBA8888 liền hàng (không đệm), mẫu khác nhau từng điểm."""
    out = bytearray()
    for y in range(height):
        for x in range(width):
            out += bytes(((x * 37 + y * 11) & 0xFF, (x * 5 + y * 71) & 0xFF, (x * y * 13 + 7) & 0xFF))
            out.append(255 if opaque else (x * 29 + y * 3) & 0xFF)
    return bytes(out)


def encode_png(
    width: int,
    height: int,
    rgba: bytes,
    *,
    color_type: int = 6,
    row_filter: int = 0,
    level: int = 6,
    extra: bytes = b"",
) -> bytes:
    """PNG 8 bit từ pixel RGBA liền hàng. `color_type` 6 (RGBA) hay 2 (RGB, cần ảnh đục);
    `row_filter` 0 (None) hay 1 (Sub); `extra`: chunk phụ chèn trước IDAT."""
    bpp = 4 if color_type == 6 else 3
    raw = bytearray()
    for y in range(height):
        row = rgba[y * width * 4 : (y + 1) * width * 4]
        if color_type == 2:
            row = b"".join(row[i : i + 3] for i in range(0, len(row), 4))
        if row_filter == 1:
            sub = bytearray(row)
            for i in range(len(row) - 1, bpp - 1, -1):
                sub[i] = (row[i] - row[i - bpp]) & 0xFF
            raw += b"\x01" + bytes(sub)
        else:
            raw += b"\x00" + bytes(row)
    return (
        PNG_SIGNATURE
        + _ihdr(width, height, color_type)
        + extra
        + _chunk(b"IDAT", zlib.compress(bytes(raw), level))
        + _chunk(b"IEND", b"")
    )


def header_only_png(width: int, height: int) -> bytes:
    """PNG hợp lệ ở header (IHDR khai cỡ `width` × `height`) mà IDAT gần như rỗng: bom giải mã."""
    return (
        PNG_SIGNATURE
        + _ihdr(width, height)
        + _chunk(b"IDAT", zlib.compress(b"\0" * 64))
        + _chunk(b"IEND", b"")
    )


def fingerprint_of(width: int, height: int, rgba: bytes) -> str:
    from teleport.core.clipboard import image_fingerprint

    return image_fingerprint(width, height, "RGBA8888", rgba)


def qimage_from(width: int, height: int, rgba: bytes) -> QImage:
    return QImage(rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()


# =================================================================================================
# Loop node giả ở thread riêng
# =================================================================================================


class LoopThread:
    """Một event loop asyncio chạy trong thread riêng, như loop của `NodeHost`."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="test-node-loop", daemon=True)
        self.thread.start()

    def run(self, qtbot, make_coro, timeout_ms: int = WAIT_MS):
        """Chạy coroutine (dựng TRÊN loop) và chờ, vòng sự kiện Qt vẫn chạy."""

        async def wrapper():
            return await make_coro()

        fut = asyncio.run_coroutine_threadsafe(wrapper(), self.loop)
        qtbot.waitUntil(fut.done, timeout=timeout_ms)
        return fut.result()

    def call(self, qtbot, fn):
        """Gọi hàm đồng bộ trên loop và chờ kết quả."""

        async def wrapper():
            return fn()

        return self.run(qtbot, wrapper)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        if not self.thread.is_alive():
            self.loop.close()


@pytest.fixture
def node_loop():
    lt = LoopThread()
    yield lt
    lt.close()


@pytest.fixture
def clip(qapp):
    """Clipboard của tiến trình (offscreen), dọn trước và sau mỗi test."""
    cb = QGuiApplication.clipboard()
    cb.clear()
    yield cb
    cb.clear()


@pytest.fixture
def env(qtbot, qapp, clip, node_loop):
    """Backend dựng trên thread Qt, đã `start` trên loop node; `changes` ghi (tên thread) mỗi `on_change`."""
    from teleport.ui.clipboard_qt import QtClipboardBackend

    backend = QtClipboardBackend()
    changes: list[str] = []
    lock = threading.Lock()

    def on_change() -> None:
        with lock:
            changes.append(threading.current_thread().name)

    node_loop.call(qtbot, lambda: backend.start(on_change))
    ns = SimpleNamespace(backend=backend, clip=clip, loop=node_loop, changes=changes, qtbot=qtbot)

    def read():
        return node_loop.run(qtbot, backend.read)

    def write(kind, data, timeout_ms: int = WAIT_MS):
        return node_loop.run(qtbot, lambda: backend.write(kind, data), timeout_ms)

    ns.read = read
    ns.write = write
    yield ns
    import shiboken6

    if shiboken6.isValid(backend):
        node_loop.call(qtbot, backend.stop)
        backend.deleteLater()
    qapp.processEvents()


def set_mime(clip, *, text: str | None = None, formats: dict[str, bytes] | None = None, image=None) -> None:
    mime = QMimeData()
    if text is not None:
        mime.setText(text)
    if image is not None:
        mime.setImageData(image)
    for name, value in (formats or {}).items():
        mime.setData(name, QByteArray(value))
    clip.setMimeData(mime)


# =================================================================================================
# Đọc văn bản
# =================================================================================================


def test_read_returns_text_set_by_set_text(env):
    from teleport.app.clipboard import ReadStatus
    from teleport.core.messages import ClipKind

    text = "Xin chào thế giới\nDòng hai 🚀"
    env.clip.setText(text)
    result = env.read()
    assert result.status is ReadStatus.OK
    assert result.content is not None
    assert result.content.kind == ClipKind.TEXT
    assert result.content.data == text


def test_read_prefers_text_when_clipboard_has_text_and_image(env):
    from teleport.app.clipboard import ReadStatus
    from teleport.core.messages import ClipKind

    set_mime(env.clip, text="chữ đi kèm ảnh", image=qimage_from(4, 4, make_pixels(4, 4)))
    result = env.read()
    assert result.status is ReadStatus.OK
    assert result.content.kind == ClipKind.TEXT
    assert result.content.data == "chữ đi kèm ảnh"


def test_read_empty_clipboard_is_empty_or_unsupported(env):
    from teleport.app.clipboard import ReadStatus

    env.clip.clear()
    result = env.read()
    assert result.status in (ReadStatus.EMPTY, ReadStatus.UNSUPPORTED)
    assert result.content is None


def test_read_unknown_format_only_is_not_sent(env):
    from teleport.app.clipboard import ReadStatus

    set_mime(env.clip, formats={"application/x-teleport-test": b"\x01\x02\x03"})
    result = env.read()
    assert result.status in (ReadStatus.EMPTY, ReadStatus.UNSUPPORTED)
    assert result.content is None


def test_read_text_exactly_at_limit_is_ok(env):
    from teleport.app.clipboard import ReadStatus
    from teleport.core.clipboard import MAX_TEXT_BYTES

    text = "a" * (2 * MAX_TEXT_BYTES)
    env.clip.setText(text)
    result = env.read()
    assert result.status is ReadStatus.OK, "đúng 2 × MAX_TEXT_BYTES: chưa vượt trần đọc"
    assert result.content.data == text


def test_read_text_one_over_limit_is_too_large(env):
    from teleport.app.clipboard import ReadStatus
    from teleport.core.clipboard import MAX_TEXT_BYTES

    env.clip.setText("a" * (2 * MAX_TEXT_BYTES + 1))
    result = env.read()
    assert result.status is ReadStatus.TOO_LARGE
    assert result.content is None


# =================================================================================================
# Đọc: định dạng nhạy cảm (QMimeData.setData)
# =================================================================================================

QT_WIN = 'application/x-qt-windows-mime;value="{}"'
DWORD_0 = b"\x00\x00\x00\x00"
DWORD_1 = b"\x01\x00\x00\x00"

SENSITIVE_CASES = [
    ("ExcludeClipboardContentFromMonitorProcessing", b"\x01"),
    (QT_WIN.format("ExcludeClipboardContentFromMonitorProcessing"), b""),
    ("org.nspasteboard.ConcealedType", b""),
    ("x-kde-passwordManagerHint", b"secret"),
    ("CanIncludeInClipboardHistory", DWORD_0),
    (QT_WIN.format("CanIncludeInClipboardHistory"), DWORD_0),
    ("CanUploadToCloudClipboard", DWORD_0),
    (QT_WIN.format("CanUploadToCloudClipboard"), DWORD_0),
    (QT_WIN.format("CanIncludeInClipboardHistory"), b"\x01"),  # < 4 byte: không đọc được → nhạy cảm
]


@pytest.mark.parametrize(
    ("fmt", "value"), SENSITIVE_CASES, ids=[f"{f}={v.hex()}" for f, v in SENSITIVE_CASES]
)
def test_read_sensitive_format_is_not_sent(env, fmt, value):
    from teleport.app.clipboard import ReadStatus

    set_mime(env.clip, text="mật-khẩu-123", formats={fmt: value})
    result = env.read()
    assert result.status is ReadStatus.SENSITIVE
    assert result.content is None


ALLOWED_CASES = [
    ("CanIncludeInClipboardHistory", DWORD_1),
    (QT_WIN.format("CanIncludeInClipboardHistory"), DWORD_1),
    ("CanUploadToCloudClipboard", DWORD_1),
    (QT_WIN.format("CanUploadToCloudClipboard"), DWORD_1),
]


@pytest.mark.parametrize(("fmt", "value"), ALLOWED_CASES, ids=[f"{f}={v.hex()}" for f, v in ALLOWED_CASES])
def test_read_dword_one_is_not_sensitive(env, fmt, value):
    """Ca đối chứng: cùng định dạng, chỉ khác DWORD 1 thay 0 → đọc bình thường."""
    from teleport.app.clipboard import ReadStatus

    set_mime(env.clip, text="chữ thường", formats={fmt: value})
    result = env.read()
    assert result.status is ReadStatus.OK
    assert result.content.data == "chữ thường"


def test_read_sensitive_image_is_not_sent(env):
    from teleport.app.clipboard import ReadStatus

    set_mime(
        env.clip,
        image=qimage_from(4, 4, make_pixels(4, 4)),
        formats={QT_WIN.format("ExcludeClipboardContentFromMonitorProcessing"): b""},
    )
    assert env.read().status is ReadStatus.SENSITIVE


# =================================================================================================
# Đọc ảnh
# =================================================================================================


def test_read_image_returns_png_and_pixel_fingerprint(env):
    from teleport.app.clipboard import ReadStatus
    from teleport.core.messages import ClipKind

    w, h = 13, 7  # cạnh lẻ
    pixels = make_pixels(w, h)
    env.clip.setImage(qimage_from(w, h, pixels))
    result = env.read()
    assert result.status is ReadStatus.OK
    content = result.content
    assert content.kind == ClipKind.PNG
    assert isinstance(content.data, bytes) and content.data.startswith(PNG_SIGNATURE)
    assert content.fingerprint == fingerprint_of(w, h, pixels), "fingerprint theo pixel RGBA8888 liền hàng"
    decoded = QImage.fromData(content.data).convertToFormat(QImage.Format.Format_RGBA8888)
    assert (decoded.width(), decoded.height()) == (w, h)
    got = b"".join(bytes(decoded.constScanLine(y))[: w * 4] for y in range(h))
    assert got == pixels, "PNG đọc ra giải mã về đúng pixel"


def test_read_image_side_over_limit_is_too_large(env):
    from teleport.app.clipboard import ReadStatus
    from teleport.core.clipboard import MAX_IMAGE_SIDE

    w = MAX_IMAGE_SIDE + 1
    env.clip.setImage(qimage_from(w, 1, make_pixels(w, 1)))
    result = env.read()
    assert result.status is ReadStatus.TOO_LARGE
    assert result.content is None


# =================================================================================================
# Ghi
# =================================================================================================


def test_write_text_then_clipboard_has_it(env):
    from teleport.app.clipboard import WriteStatus
    from teleport.core.clipboard import text_fingerprint
    from teleport.core.messages import ClipKind

    text = "Từ máy kia\nhai dòng"
    result = env.write(ClipKind.TEXT, text)
    assert result.status is WriteStatus.OK
    assert env.clip.text() == text
    assert text_fingerprint(text) in result.fingerprints
    assert result.image_size is None


def test_write_text_then_read_back_same_text(env):
    from teleport.app.clipboard import ReadStatus, WriteStatus
    from teleport.core.messages import ClipKind

    assert env.write(ClipKind.TEXT, "ghi rồi đọc lại").status is WriteStatus.OK
    back = env.read()
    assert back.status is ReadStatus.OK
    assert back.content.data == "ghi rồi đọc lại"


def test_write_png_sets_image_and_returns_pixel_fingerprint(env):
    from teleport.app.clipboard import WriteStatus
    from teleport.core.messages import ClipKind

    w, h = 9, 5
    pixels = make_pixels(w, h)
    result = env.write(ClipKind.PNG, encode_png(w, h, pixels))
    assert result.status is WriteStatus.OK
    assert result.image_size == (w, h)
    assert result.fingerprints, "trả fingerprint đọc lại"
    assert fingerprint_of(w, h, pixels) in result.fingerprints
    img = env.clip.image()
    assert (img.width(), img.height()) == (w, h)


def test_write_translucent_png_fingerprint_is_rgba_pixels(env):
    """Alpha khác 255: fingerprint vẫn là pixel RGBA8888 (không nhân trước alpha)."""
    from teleport.app.clipboard import WriteStatus
    from teleport.core.messages import ClipKind

    w, h = 6, 4
    pixels = make_pixels(w, h, opaque=False)
    result = env.write(ClipKind.PNG, encode_png(w, h, pixels))
    assert result.status is WriteStatus.OK
    assert result.fingerprints[0] == fingerprint_of(w, h, pixels)


def test_write_png_fingerprint_independent_of_encoding(env):
    from teleport.app.clipboard import WriteStatus
    from teleport.core.messages import ClipKind

    w, h = 11, 6
    pixels = make_pixels(w, h)
    variants = [
        encode_png(w, h, pixels),
        encode_png(w, h, pixels, row_filter=1, level=9),
        encode_png(w, h, pixels, color_type=2, level=0),
        encode_png(w, h, pixels, extra=_chunk(b"tEXt", b"Comment\0mot cai anh")),
    ]
    assert len(set(variants)) == len(variants), "các bản mã hóa phải khác byte"
    first: list[str] = []
    for png in variants:
        result = env.write(ClipKind.PNG, png)
        assert result.status is WriteStatus.OK
        first.append(result.fingerprints[0])
    assert first == [fingerprint_of(w, h, pixels)] * len(variants)


def test_read_png_written_back_keeps_fingerprint(env):
    """Ảnh người dùng sao chép → đọc (mã hóa PNG) → máy kia ghi đúng byte đó → cùng fingerprint."""
    from teleport.app.clipboard import ReadStatus, WriteStatus
    from teleport.core.messages import ClipKind

    w, h = 10, 10
    env.clip.setImage(qimage_from(w, h, make_pixels(w, h)))
    read = env.read()
    assert read.status is ReadStatus.OK
    env.clip.clear()
    written = env.write(ClipKind.PNG, read.content.data)
    assert written.status is WriteStatus.OK
    assert read.content.fingerprint in written.fingerprints
    again = env.read()
    assert again.status is ReadStatus.OK
    assert again.content.fingerprint == read.content.fingerprint


def test_write_png_at_max_side_is_accepted(env):
    from teleport.app.clipboard import WriteStatus
    from teleport.core.clipboard import MAX_IMAGE_SIDE
    from teleport.core.messages import ClipKind

    pixels = make_pixels(MAX_IMAGE_SIDE, 1)
    result = env.write(ClipKind.PNG, encode_png(MAX_IMAGE_SIDE, 1, pixels))
    assert result.status is WriteStatus.OK
    assert result.image_size == (MAX_IMAGE_SIDE, 1)


def test_write_png_one_over_max_side_is_rejected(env):
    from teleport.app.clipboard import WriteStatus
    from teleport.core.clipboard import MAX_IMAGE_SIDE
    from teleport.core.messages import ClipKind

    w = MAX_IMAGE_SIDE + 1
    result = env.write(ClipKind.PNG, encode_png(w, 1, make_pixels(w, 1)))
    assert result.status is WriteStatus.REJECTED


@pytest.fixture
def decode_spy(monkeypatch):
    """Ghi lại mọi lần giải mã ảnh qua API Python của Qt (`QImageReader.read`, `QImage.fromData`,
    `QImage.loadFromData`)."""
    from PySide6.QtGui import QImageReader

    calls: list[str] = []
    real_read = QImageReader.read
    real_from = QImage.fromData
    real_load = QImage.loadFromData

    def read(self, *a):
        calls.append("QImageReader.read")
        return real_read(self, *a)

    def from_data(*a, **k):
        calls.append("QImage.fromData")
        return real_from(*a, **k)

    def load(self, *a, **k):
        calls.append("QImage.loadFromData")
        return real_load(self, *a, **k)

    monkeypatch.setattr(QImageReader, "read", read)
    monkeypatch.setattr(QImage, "fromData", staticmethod(from_data))
    monkeypatch.setattr(QImage, "loadFromData", load)
    return calls


def test_decode_spy_sees_decoding_of_valid_png(env, decode_spy):
    """Ca đối chứng cho ca bom: ảnh hợp lệ thì spy thấy giải mã (spy không câm)."""
    from teleport.app.clipboard import WriteStatus
    from teleport.core.messages import ClipKind

    assert env.write(ClipKind.PNG, encode_png(4, 4, make_pixels(4, 4))).status is WriteStatus.OK
    assert decode_spy, "ghi ảnh hợp lệ phải đi qua một API giải mã mà spy theo dõi"


def test_write_png_bomb_header_rejected_fast_without_decoding(env, decode_spy):
    from teleport.app.clipboard import WriteStatus
    from teleport.core.messages import ClipKind

    env.clip.setText("giữ nguyên")
    env.qtbot.wait(50)
    start = time.monotonic()
    result = env.write(ClipKind.PNG, header_only_png(50_000, 50_000))
    elapsed = time.monotonic() - start
    assert result.status is WriteStatus.REJECTED
    assert elapsed < 1.0, f"từ chối theo header phải nhanh, mất {elapsed:.2f} s"
    assert decode_spy == [], f"không được giải mã ảnh có header 50000 × 50000: {decode_spy}"
    assert env.clip.text() == "giữ nguyên", "REJECTED không đổi clipboard"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"khong phai png",
        PNG_SIGNATURE + b"\x00" * 40,
        PNG_SIGNATURE + _ihdr(4, 4) + _chunk(b"IEND", b""),  # thiếu IDAT
        PNG_SIGNATURE + _ihdr(0, 4) + _chunk(b"IDAT", zlib.compress(b"\0")) + _chunk(b"IEND", b""),
    ],
    ids=["empty", "not-png", "garbage-after-signature", "no-idat", "zero-width"],
)
def test_write_broken_png_rejected_and_clipboard_unchanged(env, data):
    from teleport.app.clipboard import WriteStatus
    from teleport.core.messages import ClipKind

    env.clip.setText("giữ nguyên")
    env.qtbot.wait(50)
    result = env.write(ClipKind.PNG, data)
    assert result.status is WriteStatus.REJECTED
    assert env.clip.text() == "giữ nguyên"


# =================================================================================================
# on_change, vòng đời
# =================================================================================================


def test_on_change_called_after_start_when_clipboard_changes(env):
    before = len(env.changes)
    env.clip.setText("người dùng sao chép")
    env.qtbot.waitUntil(lambda: len(env.changes) > before, timeout=3000)


def test_on_change_not_called_before_start_or_after_stop(qtbot, qapp, clip, node_loop):
    from teleport.ui.clipboard_qt import QtClipboardBackend

    backend = QtClipboardBackend()
    changes: list[int] = []
    clip.setText("trước start")
    qtbot.wait(100)
    node_loop.call(qtbot, lambda: backend.start(lambda: changes.append(1)))
    assert changes == [], "chưa start thì không báo"
    clip.setText("sau start")
    qtbot.waitUntil(lambda: len(changes) >= 1, timeout=3000)
    node_loop.call(qtbot, backend.stop)
    seen = len(changes)
    clip.setText("sau stop")
    qtbot.wait(200)
    assert len(changes) == seen, "sau stop không báo nữa"
    backend.deleteLater()
    qapp.processEvents()


def test_read_and_write_from_node_loop_thread_complete(env):
    """Gọi từ loop ở thread khác thread Qt (như node thật): kết quả về đúng Future của loop."""
    from teleport.app.clipboard import ReadStatus, WriteStatus
    from teleport.core.messages import ClipKind

    assert threading.current_thread() is threading.main_thread()
    for i in range(5):
        assert env.write(ClipKind.TEXT, f"lần {i}").status is WriteStatus.OK
        r = env.read()
        assert r.status is ReadStatus.OK and r.content.data == f"lần {i}"


def test_deleted_backend_returns_failed_not_raise(env):
    """9.3 điểm 7: QObject đã bị xóa → FAILED (không ném, không treo)."""
    import shiboken6

    from teleport.app.clipboard import ReadStatus, WriteStatus
    from teleport.core.messages import ClipKind

    env.clip.setText("còn chữ")
    shiboken6.delete(env.backend)
    assert env.read().status is ReadStatus.FAILED
    assert env.write(ClipKind.TEXT, "x").status is WriteStatus.FAILED


# =================================================================================================
# Sửa sau soat-code / tan-cong P5-2: thử lại khi ghi văn bản, việc quá hạn trong pool, stop(), vọng ảnh
# =================================================================================================


class _BusyClipboard(QObject):
    """Clipboard giả: `text()` trả rỗng `busy` lần đầu (ứng dụng khác đang giữ clipboard)."""

    dataChanged = Signal()

    def __init__(self, busy: int) -> None:
        super().__init__()
        self.busy = busy
        self.sets: list[str] = []
        self._text = ""

    def setText(self, text: str) -> None:
        self.sets.append(text)
        self._text = text

    def text(self) -> str:
        if self.busy > 0:
            self.busy -= 1
            return ""
        return self._text


@pytest.fixture
def busy_env(qtbot, qapp, node_loop, monkeypatch):
    from teleport.ui import clipboard_qt
    from teleport.ui.clipboard_qt import QtClipboardBackend

    monkeypatch.setattr(clipboard_qt, "WRITE_RETRY_MS", 5)
    made: list = []

    def make(busy: int):
        fake = _BusyClipboard(busy)
        backend = QtClipboardBackend(clipboard=fake)
        made.append((fake, backend))
        return fake, backend

    yield make
    for fake, backend in made:
        node_loop.call(qtbot, backend.stop)
        backend.deleteLater()
        fake.deleteLater()
    qapp.processEvents()


def test_write_text_retries_until_read_back_not_empty(busy_env, node_loop, qtbot):
    from teleport.app.clipboard import WriteStatus
    from teleport.core.clipboard import text_fingerprint
    from teleport.core.messages import ClipKind
    from teleport.ui.clipboard_qt import WRITE_RETRIES

    fake, backend = busy_env(WRITE_RETRIES)
    result = node_loop.run(qtbot, lambda: backend.write(ClipKind.TEXT, "giữ chỗ"))
    assert result.status is WriteStatus.OK
    assert result.fingerprints == (text_fingerprint("giữ chỗ"),)
    assert len(fake.sets) == WRITE_RETRIES + 1


def test_write_text_fails_after_retries(busy_env, node_loop, qtbot):
    from teleport.app.clipboard import WriteStatus
    from teleport.core.messages import ClipKind
    from teleport.ui.clipboard_qt import WRITE_RETRIES

    fake, backend = busy_env(WRITE_RETRIES + 1)
    result = node_loop.run(qtbot, lambda: backend.write(ClipKind.TEXT, "không chép được"))
    assert result.status is WriteStatus.FAILED
    assert len(fake.sets) == WRITE_RETRIES + 1, "thử tối đa WRITE_RETRIES lần sau lần đầu"


class _Gate:
    """`decode_png` giả chặn tới khi `go` được đặt; ghi lại `cancelled()` lúc được thả."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.go = threading.Event()
        self.calls = 0
        self.cancelled_after: list[bool] = []

    def decode(self, png, cancelled=lambda: False):
        from teleport.ui import clipboard_qt

        self.calls += 1
        self.entered.set()
        self.go.wait(10)
        self.cancelled_after.append(cancelled())
        image = QImage(4, 4, QImage.Format.Format_RGBA8888)
        image.fill(0)
        return image, clipboard_qt._fingerprint(image)


@pytest.fixture
def gate(monkeypatch):
    from teleport.ui import clipboard_qt

    g = _Gate()
    monkeypatch.setattr(clipboard_qt, "decode_png", g.decode)
    yield g
    g.go.set()


def test_timed_out_image_write_blocks_new_image_jobs_then_is_dropped(env, gate):
    """tan-cong: việc quá hạn còn chạy trong pool → việc ảnh mới FAILED ngay (không chồng chất); việc quá
    hạn thấy cờ hủy, không `setImage`; pool rảnh thì ghi lại được."""
    from teleport.app.clipboard import WriteStatus
    from teleport.core.messages import ClipKind

    png = encode_png(4, 4, make_pixels(4, 4))
    env.clip.setText("giữ nguyên")
    env.qtbot.wait(50)
    with pytest.raises(TimeoutError):
        env.loop.run(env.qtbot, lambda: asyncio.wait_for(env.backend.write(ClipKind.PNG, png), 0.3))
    assert gate.entered.is_set()
    start = time.monotonic()
    assert env.write(ClipKind.PNG, png).status is WriteStatus.FAILED
    assert time.monotonic() - start < 2.0
    assert gate.calls == 1, "không xếp thêm việc khi còn việc quá hạn"
    gate.go.set()
    env.qtbot.waitUntil(lambda: env.backend._pool.activeThreadCount() == 0, timeout=WAIT_MS)
    env.qtbot.wait(50)
    assert gate.cancelled_after == [True]
    assert env.clip.text() == "giữ nguyên", "việc quá hạn không được ghi clipboard"
    assert env.write(ClipKind.PNG, png).status is WriteStatus.OK
    assert gate.calls == 2


def test_stop_lets_queued_pool_job_exit_without_running(env, gate, monkeypatch):
    """`stop()` không gọi `QThreadPool.clear()`; việc đang xếp tự thoát (FAILED) khi tới lượt."""
    from PySide6.QtCore import QThreadPool

    from teleport.app.clipboard import WriteStatus
    from teleport.core.messages import ClipKind

    cleared: list[int] = []
    monkeypatch.setattr(QThreadPool, "clear", lambda self: cleared.append(1))
    png = encode_png(4, 4, make_pixels(4, 4))
    first = asyncio.run_coroutine_threadsafe(env.backend.write(ClipKind.PNG, png), env.loop.loop)
    env.qtbot.waitUntil(gate.entered.is_set, timeout=WAIT_MS)
    second = asyncio.run_coroutine_threadsafe(env.backend.write(ClipKind.PNG, png), env.loop.loop)
    env.qtbot.wait(100)  # việc thứ hai đã vào hàng của pool
    env.loop.call(env.qtbot, env.backend.stop)
    gate.go.set()
    env.qtbot.waitUntil(lambda: first.done() and second.done(), timeout=WAIT_MS)
    assert first.result().status is WriteStatus.FAILED
    assert second.result().status is WriteStatus.FAILED
    assert gate.calls == 1, "việc xếp sau stop() không giải mã"
    assert cleared == []


def test_decode_png_cancelled_does_not_decode(decode_spy):
    from teleport.ui.clipboard_qt import decode_png

    assert decode_png(encode_png(4, 4, make_pixels(4, 4)), lambda: True) is None
    assert decode_spy == []


def test_encode_image_cancelled_is_failed():
    from teleport.app.clipboard import ReadStatus
    from teleport.ui.clipboard_qt import encode_image

    assert encode_image(qimage_from(3, 3, make_pixels(3, 3)), lambda: True).status is ReadStatus.FAILED


def test_rgba_converts_in_place():
    from teleport.ui.clipboard_qt import _rgba

    image = QImage(5, 3, QImage.Format.Format_ARGB32)
    image.fill(0xFF102030)
    assert _rgba(image) is image
    assert image.format() == QImage.Format.Format_RGBA8888


def test_image_write_read_back_same_data_skips_second_pool_job(env, monkeypatch):
    from teleport.app.clipboard import WriteStatus
    from teleport.core.messages import ClipKind
    from teleport.ui import clipboard_qt

    calls: list[int] = []
    real = clipboard_qt._written

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(clipboard_qt, "_written", spy)
    w, h = 7, 5
    pixels = make_pixels(w, h)
    result = env.write(ClipKind.PNG, encode_png(w, h, pixels))
    assert result.status is WriteStatus.OK
    assert result.fingerprints == (fingerprint_of(w, h, pixels),)
    assert calls == [], "clipboard offscreen trả đúng ảnh vừa đặt: không tính lại trong pool"


def test_echo_read_after_image_write_does_not_encode(env, monkeypatch):
    """Đọc ngay sau khi ghi ảnh (vọng `dataChanged`): OK với fingerprint đã ghi, không mã hóa PNG."""
    from teleport.app.clipboard import ReadStatus, WriteStatus
    from teleport.core.messages import ClipKind
    from teleport.ui import clipboard_qt

    encoded: list[int] = []
    real = clipboard_qt.encode_image

    def spy(*a, **k):
        encoded.append(1)
        return real(*a, **k)

    monkeypatch.setattr(clipboard_qt, "encode_image", spy)
    w, h = 6, 6
    pixels = make_pixels(w, h)
    written = env.write(ClipKind.PNG, encode_png(w, h, pixels))
    assert written.status is WriteStatus.OK
    echo = env.read()
    assert echo.status is ReadStatus.OK
    assert echo.content.kind == ClipKind.PNG
    assert echo.content.fingerprint in written.fingerprints
    assert echo.content.data == b""
    assert encoded == []


def test_user_copy_of_same_pixels_after_write_is_encoded(env):
    """Người dùng sao chép ảnh khác (dù cùng pixel) sau lần ghi: đọc mã hóa PNG đầy đủ như thường."""
    from teleport.app.clipboard import ReadStatus, WriteStatus
    from teleport.core.messages import ClipKind

    w, h = 6, 6
    pixels = make_pixels(w, h)
    assert env.write(ClipKind.PNG, encode_png(w, h, pixels)).status is WriteStatus.OK
    env.clip.setImage(qimage_from(w, h, pixels))
    read = env.read()
    assert read.status is ReadStatus.OK
    assert read.content.data.startswith(PNG_SIGNATURE)
    assert read.content.fingerprint == fingerprint_of(w, h, pixels)


def test_text_write_clears_image_echo(env):
    from teleport.app.clipboard import ReadStatus, WriteStatus
    from teleport.core.messages import ClipKind

    w, h = 4, 4
    pixels = make_pixels(w, h)
    assert env.write(ClipKind.PNG, encode_png(w, h, pixels)).status is WriteStatus.OK
    image = env.clip.image()
    assert env.write(ClipKind.TEXT, "chữ").status is WriteStatus.OK
    env.clip.setImage(image)  # cùng dữ liệu ảnh đã ghi, nhưng sau một lần ghi khác
    read = env.read()
    assert read.status is ReadStatus.OK and read.content.data.startswith(PNG_SIGNATURE)


# =================================================================================================
# Kiểm sơ bộ trước khi mã hóa PNG (số đo M4: ảnh nhiễu lớn từng mất hàng chục giây mới biết TOO_LARGE)
# =================================================================================================


def noise_image(width: int, height: int) -> QImage:
    import os

    return qimage_from(width, height, os.urandom(4 * width * height))


@pytest.fixture
def writer_spy(monkeypatch):
    from PySide6.QtGui import QImageWriter

    calls: list[int] = []
    real = QImageWriter.write

    def write(self, *a):
        calls.append(1)
        return real(self, *a)

    monkeypatch.setattr(QImageWriter, "write", write)
    return calls


def test_large_noise_image_too_large_fast_without_png_encoding(writer_spy):
    from teleport.app.clipboard import ReadStatus
    from teleport.ui.clipboard_qt import PRESCREEN_LIMIT_BYTES, encode_image

    image = noise_image(3000, 3000)  # 36 MB pixel, gần như không nén được
    start = time.monotonic()
    result = encode_image(image)
    elapsed = time.monotonic() - start
    assert result.status is ReadStatus.TOO_LARGE
    assert result.content is None and result.size > PRESCREEN_LIMIT_BYTES
    assert writer_spy == [], "ảnh không qua kiểm sơ bộ không được mã hóa PNG"
    assert elapsed < 3.0, f"kiểm sơ bộ phải dừng sớm, mất {elapsed:.2f} s"


def test_noise_over_patched_limit_is_too_large_via_backend_read(env, writer_spy, monkeypatch):
    from teleport.app.clipboard import ReadStatus
    from teleport.ui import clipboard_qt

    monkeypatch.setattr(clipboard_qt, "PRESCREEN_LIMIT_BYTES", 64 * 1024)
    monkeypatch.setattr(clipboard_qt, "PRESCREEN_CHUNK_BYTES", 16 * 1024)
    env.clip.setImage(noise_image(256, 256))  # 256 KiB nhiễu
    result = env.read()
    assert result.status is ReadStatus.TOO_LARGE
    assert writer_spy == []


def test_easy_image_passes_prescreen_and_is_encoded(writer_spy):
    from teleport.app.clipboard import ReadStatus
    from teleport.ui.clipboard_qt import encode_image, prescreen

    w, h = 2048, 2048
    image = QImage(w, h, QImage.Format.Format_RGBA8888)
    image.fill(0xFF3366CC)
    assert prescreen(image) < 1024 * 1024
    result = encode_image(image)
    assert result.status is ReadStatus.OK
    assert result.content.data.startswith(PNG_SIGNATURE)
    assert writer_spy == [1]
    decoded = QImage.fromData(result.content.data)
    assert (decoded.width(), decoded.height()) == (w, h)


def test_prescreen_checks_cancel_between_chunks(monkeypatch):
    from teleport.app.clipboard import ReadStatus
    from teleport.ui import clipboard_qt

    monkeypatch.setattr(clipboard_qt, "PRESCREEN_CHUNK_BYTES", 4096)
    image = noise_image(64, 64)  # 16 KiB → 4 khối
    asked: list[int] = []

    def cancelled() -> bool:
        asked.append(1)
        return len(asked) > 2

    assert clipboard_qt.prescreen(image, cancelled) is None
    assert len(asked) == 3, "dừng ở khối kế tiếp sau khi bị hủy"
    seen: list[int] = []

    def later() -> bool:  # hủy đúng lúc kiểm sơ bộ: encode_image trả FAILED, không mã hóa
        seen.append(1)
        return len(seen) > 3

    assert clipboard_qt.encode_image(noise_image(64, 64), later).status is ReadStatus.FAILED
