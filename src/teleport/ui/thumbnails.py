"""Nạp ảnh thu nhỏ cho bong bóng ảnh trên thread nền, giới hạn bộ nhớ đệm (v1.7.0 Đợt 1).

Chỉ giải mã định dạng ảnh an toàn (`THUMB_EXTENSIONS`), bỏ qua file quá lớn (`THUMB_MAX_FILE_BYTES`),
đọc theo kích thước thu gọn ngay trong `QImageReader` (trên `QThreadPool` tối đa 2 luồng) thành
`QImage`, rồi mới chuyển thành `QPixmap` trên luồng Qt chính và giữ tối đa `THUMB_CACHE_LIMIT`
mục gần nhất theo LRU.
"""

from __future__ import annotations

import contextlib
import os
import weakref
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QImageReader, QKeyEvent, QPixmap, QResizeEvent
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from shiboken6 import isValid

from teleport.ui import motion, texts, theme
from teleport.ui.buttons import AnimatedButton

THUMB_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico"})
THUMB_MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MiB: tránh tốn RAM với ảnh RAW/PNG khổng lồ
THUMB_CACHE_LIMIT = 32
THUMB_DEFAULT_SIZE = 72

_enabled: bool = True  # seam cho test hoặc tắt nạp ảnh thu nhỏ


def is_thumbnailable(path: Path | str | None) -> bool:
    """True nếu đường dẫn mang đuôi ảnh có thể tạo ảnh thu nhỏ."""
    if not path:
        return False
    suffix = Path(os.fspath(path)).suffix.lower()
    return suffix in THUMB_EXTENSIONS


def _decode_scaled(path: Path, pixel_size: int) -> QImage | None:
    """Giải mã ảnh thu gọn ngay ở mức `QImageReader` trên thread nền (trả None nếu lỗi/quá lớn)."""
    try:
        stat = path.stat()
    except OSError:
        return None
    if stat.st_size <= 0 or stat.st_size > THUMB_MAX_FILE_BYTES:
        return None
    reader = QImageReader(os.fspath(path))
    reader.setAutoTransform(True)
    raw = reader.size()
    target = QSize(pixel_size, pixel_size)
    if raw.isValid() and (raw.width() > pixel_size or raw.height() > pixel_size):
        scaled = raw.scaled(target, Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(scaled)
    image = reader.read()
    if image.isNull():
        return None
    if image.width() > pixel_size or image.height() > pixel_size:
        image = image.scaled(
            pixel_size,
            pixel_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return image


class _ThumbTask(QRunnable):
    def __init__(
        self,
        bridge: _ThumbBridge,
        path: Path,
        size: int,
        dpr: float,
    ) -> None:
        super().__init__()
        self._bridge_ref = weakref.ref(bridge)
        self._path = path
        self._size = size
        self._dpr = dpr

    def run(self) -> None:
        bridge = self._bridge_ref()
        if bridge is None or not isValid(bridge) or bridge.closing:
            return
        pixel_size = max(16, round(self._size * max(1.0, self._dpr)))
        try:
            image = _decode_scaled(self._path, pixel_size)
        except Exception:
            image = None
        bridge = self._bridge_ref()
        if bridge is None or not isValid(bridge) or bridge.closing:
            return
        with contextlib.suppress(RuntimeError):
            bridge.decoded.emit(os.fspath(self._path), self._size, self._dpr, image)


class _ThumbBridge(QObject):
    decoded = Signal(str, int, float, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.closing = False


def _mark_bridge_closing(bridge_ref: weakref.ref[_ThumbBridge]) -> None:
    bridge = bridge_ref()
    if bridge is not None and isValid(bridge):
        bridge.closing = True


class ThumbnailLoader(QObject):
    """Bộ nạp ảnh thu nhỏ bất đồng bộ dùng chung cho luồng chat."""

    def __init__(self, parent: QObject | None = None, *, limit: int = THUMB_CACHE_LIMIT) -> None:
        super().__init__(parent)
        self._limit = max(1, int(limit))
        self._cache: OrderedDict[tuple[str, int, float], QPixmap] = OrderedDict()
        self._pending: dict[tuple[str, int, float], list[Callable[[QPixmap | None], None]]] = {}
        self._bridge = _ThumbBridge(self)
        self._bridge.decoded.connect(self._on_decoded)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        bridge_ref = weakref.ref(self._bridge)
        self.destroyed.connect(lambda _obj=None, r=bridge_ref: _mark_bridge_closing(r))

    def cached(self, path: Path, size: int = THUMB_DEFAULT_SIZE, *, dpr: float = 1.0) -> QPixmap | None:
        """Lấy từ đệm nếu đã có (và đưa lên đầu LRU); không kích hoạt nạp nền."""
        key = (os.fspath(path), size, round(max(1.0, dpr), 2))
        pix = self._cache.get(key)
        if pix is not None:
            self._cache.move_to_end(key)
        return pix

    def request(
        self,
        path: Path,
        callback: Callable[[QPixmap | None], None],
        *,
        size: int = THUMB_DEFAULT_SIZE,
        dpr: float = 1.0,
    ) -> None:
        """Yêu cầu ảnh thu nhỏ; gọi `callback(QPixmap)` ngay nếu có sẵn trong đệm, ngược lại nạp nền."""
        if self._bridge.closing or not _enabled or not path.is_absolute() or not is_thumbnailable(path):
            return
        norm_dpr = round(max(1.0, dpr), 2)
        key = (os.fspath(path), size, norm_dpr)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            callback(hit)
            return
        waiters = self._pending.get(key)
        if waiters is not None:
            waiters.append(callback)
            return
        self._pending[key] = [callback]
        self._pool.start(_ThumbTask(self._bridge, path, size, norm_dpr))

    def _on_decoded(self, path_str: str, size: int, dpr: float, image: object) -> None:
        key = (path_str, size, dpr)
        waiters = self._pending.pop(key, None)
        if self._bridge.closing or waiters is None:
            return
        pixmap: QPixmap | None = None
        if isinstance(image, QImage) and not image.isNull():
            pixmap = QPixmap.fromImage(image)
            pixmap.setDevicePixelRatio(dpr)
            self._cache[key] = pixmap
            self._cache.move_to_end(key)
            while len(self._cache) > self._limit:
                self._cache.popitem(last=False)
        for cb in waiters:
            cb(pixmap)

    def clear_cache(self) -> None:
        """Xóa toàn bộ ảnh thu nhỏ đang giữ trong bộ nhớ."""
        self._cache.clear()
        self._pending.clear()

    def shutdown(self) -> None:
        """Hủy mọi yêu cầu chờ và đợi thread pool kết thúc gọn khi đóng ứng dụng."""
        self._bridge.closing = True
        self._pool.clear()
        self._pool.waitForDone(500)
        self._pending.clear()
        self._cache.clear()


class QuickPreviewDialog(QDialog):
    """Cửa sổ xem trước ảnh nhanh không chặn (bấm ảnh thu nhỏ trong bong bóng chat)."""

    def __init__(
        self,
        path: Path,
        parent: QWidget | None = None,
        *,
        on_open: Callable[[Path], None] | None = None,
    ) -> None:
        super().__init__(parent)
        motion.dim_parent_on_show(self)
        self.setObjectName("quickPreviewDialog")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._path = path
        self._on_open = on_open
        self._source = self._load_source(path)

        t = QCoreApplication.translate
        self.setWindowTitle(texts.file_label(path.name))
        self.setMinimumSize(420, 320)
        self.resize(640, 480)

        self.image_label = QLabel(self)
        self.image_label.setObjectName("quickPreviewImage")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setProperty(theme.SURFACE_PROPERTY, "raised")
        self.image_label.setMinimumSize(200, 180)
        if self._source.isNull():
            self.image_label.setText(t("Chat", "Không xem trước được ảnh."))
            self.image_label.setProperty(theme.TONE_PROPERTY, "muted")

        self.name_label = QLabel(texts.file_label(path.name), self)
        self.name_label.setObjectName("quickPreviewName")
        self.name_label.setTextFormat(Qt.TextFormat.PlainText)
        self.name_label.setProperty(theme.TONE_PROPERTY, "muted")

        self.open_button = AnimatedButton(t("Chat", "Mở file"), self)
        self.open_button.setObjectName("quickPreviewOpenBtn")
        self.open_button.setVisible(on_open is not None)
        self.open_button.clicked.connect(self._handle_open)

        self.close_button = AnimatedButton(t("Chat", "Đóng"), self, primary=True)
        self.close_button.setObjectName("quickPreviewCloseBtn")
        self.close_button.clicked.connect(self.close)

        footer = QHBoxLayout()
        footer.setSpacing(theme.SPACING[2])
        footer.addWidget(self.name_label, 1)
        footer.addWidget(self.open_button)
        footer.addWidget(self.close_button)

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.SPACING[3], theme.SPACING[3], theme.SPACING[3], theme.SPACING[3])
        root.setSpacing(theme.SPACING[2])
        root.addWidget(self.image_label, 1)
        root.addLayout(footer)
        self._update_pixmap()

    @staticmethod
    def _load_source(path: Path) -> QPixmap:
        reader = QImageReader(os.fspath(path))
        reader.setAutoTransform(True)
        img = reader.read()
        if img.isNull():
            return QPixmap()
        return QPixmap.fromImage(img)

    def _handle_open(self) -> None:
        if self._on_open is not None:
            self._on_open(self._path)

    def _update_pixmap(self) -> None:
        if self._source.isNull():
            return
        size = self.image_label.contentsRect().size()
        if size.width() <= 0 or size.height() <= 0:
            size = QSize(580, 360)
        scaled = self._source.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_pixmap()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)
