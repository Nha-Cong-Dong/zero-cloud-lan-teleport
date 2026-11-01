"""Icon SVG đóng gói kèm app (Fluent UI System Icons, MIT), vẽ bằng `QSvgRenderer` và tô một màu.

Đây là module **duy nhất** import `PySide6.QtSvg` (`tests/test_architecture.py`). Không dùng
`QIcon("….svg")` hay `QIcon(Path)`: engine icon SVG của Qt là plugin `iconengines`, không đóng vào
bản exe; mọi icon dựng từ pixmap của module này.

**Tài sản**: `teleport/ui/assets/icons/<tên>_<cỡ>_regular.svg`, đọc qua `importlib.resources`
(chạy được cả từ wheel lẫn bản PyInstaller), giấy phép MIT ở `assets/icons/LICENSE`. Không tải gì từ
mạng. Bộ tên lấy theo spec A2 (42 tên, 47 file) cộng `color` (bảng màu, mục Giao diện của Cài đặt,
spec D6): mọi tên có cỡ 20; `desktop`, `laptop`,
`cloud_off`, `shield_checkmark`, `arrow_sync` có thêm cỡ 24. Tên nào cũng có đúng tên trong bộ
Fluent ở commit đã ghim (`packaging/pyinstaller/third_party.py`, `SOURCE_ASSETS`), không phải đổi
sang tên gần nhất.

**Tô màu** (`pixmap`): SVG vẽ vào pixmap trong suốt cỡ `round(size * dpr)` px, rồi phủ màu bằng
`CompositionMode_SourceIn` (giữ alpha của nét, thay màu), cuối cùng `setDevicePixelRatio(dpr)`. Màu
truyền vào lấy từ token của `ui/theme.py` (`theme.tokens(dark)`), không ghi cứng mã màu ở màn nào.

**Đệm**: pixmap đã tô được giữ theo khóa `(tên, cỡ, color.rgba(), dpr)`, tối đa `CACHE_LIMIT` mục,
bỏ mục dùng lâu nhất khi đầy; nội dung SVG đọc một lần mỗi file. Chỉ dùng trên thread Qt.

**Lỗi**: tên hay cỡ không có trong `SIZES` → `KeyError` (lỗi lập trình). File thiếu hay SVG hỏng (bản
đóng gói lỗi) → ghi WARNING (không kèm nội dung), trả pixmap rỗng, không đệm; giao diện vẫn chạy,
còn `--selftest` bắt được (`ui/app.py`).

`render_svg(data, size, dpr)` vẽ một SVG bất kỳ đủ màu, không tô và không đệm: dùng cho logo của dự án
(`ui/logo.py`), để `QtSvg` vẫn chỉ bị import ở đây.

`icon(name, size, color, disabled)` dựng `QIcon` từ pixmap ở dpr 1, 2 và dpr lớn nhất của các màn
hình đang có, cho chế độ Normal và (nếu có `disabled`) Disabled. Không có `disabled` thì Qt tự làm mờ
ảnh Normal cho chế độ Disabled; giao diện nên truyền màu chữ bị tắt (`text_muted`) để đủ tương phản.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from importlib import resources

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

log = logging.getLogger(__name__)

_SIZES_20 = (
    "search",
    "settings",
    "add",
    "chevron_left",
    "more_horizontal",
    "checkmark_circle",
    "error_circle",
    "warning",
    "info",
    "plug_disconnected",
    "eye_off",
    "pause_circle",
    "pause",
    "play",
    "dismiss",
    "arrow_clockwise",
    "arrow_down",
    "arrow_upload",
    "arrow_download",
    "attach",
    "folder",
    "folder_open",
    "clipboard_paste",
    "copy",
    "delete",
    "link_dismiss",
    "checkmark",
    "document",
    "document_pdf",
    "image",
    "video",
    "music_note_2",
    "folder_zip",
    "globe",
    "lock_closed",
    "arrow_swap",
    "wrench",
    "color",
)
_SIZES_20_24 = ("desktop", "laptop", "cloud_off", "shield_checkmark", "arrow_sync")

SIZES: dict[str, tuple[int, ...]] = {
    **{name: (20,) for name in _SIZES_20},
    **{name: (20, 24) for name in _SIZES_20_24},
}
"""Các cỡ (px logic) có file SVG của mỗi tên."""

NAMES: frozenset[str] = frozenset(SIZES)
"""Mọi tên icon có trong `assets/icons/`."""

CACHE_LIMIT = 512
"""Số pixmap đã tô giữ trong đệm tối đa."""

_DPR_SET = (1.0, 2.0)

_pixmaps: OrderedDict[tuple[str, int, int, float], QPixmap] = OrderedDict()
_svg_data: dict[tuple[str, int], bytes] = {}


def file_name(name: str, size: int) -> str:
    """Tên file SVG của icon `name` cỡ `size`; tên hay cỡ không có → `KeyError`."""
    if size not in SIZES[name]:
        raise KeyError(f"{name}@{size}")
    return f"{name}_{size}_regular.svg"


def _read_svg(name: str, size: int) -> bytes | None:
    key = (name, size)
    data = _svg_data.get(key)
    if data is None:
        resource = resources.files("teleport.ui").joinpath("assets", "icons", file_name(name, size))
        try:
            data = resource.read_bytes()
        except OSError as e:
            log.warning("không đọc được icon %s cỡ %d: %s", name, size, type(e).__name__)
            return None
        _svg_data[key] = data
    return data


def render_svg(data: bytes, size: int, dpr: float = 1.0) -> QPixmap:
    """Vẽ SVG `data` đủ màu (không tô lại) vào pixmap trong suốt cỡ `size` px logic, tỉ lệ `dpr`.

    SVG hỏng → pixmap rỗng; nơi gọi tự ghi log. Không đệm (dùng cho logo, `ui/logo.py`).
    """
    if not dpr > 0:
        raise ValueError(f"dpr phải > 0: {dpr}")
    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        return QPixmap()
    side = max(1, round(size * dpr))
    result = QPixmap(side, side)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(0, 0, side, side))
    finally:
        painter.end()
    result.setDevicePixelRatio(dpr)
    return result


def pixmap(name: str, size: int, color: QColor, dpr: float = 1.0) -> QPixmap:
    """Icon `name` cỡ `size` px logic, tô `color`, cho màn hình tỉ lệ `dpr` (xem docstring module).

    Trả bản sao (Qt chia sẻ ngầm dữ liệu ảnh) để nơi gọi sửa pixmap không làm hỏng mục trong đệm.
    """
    file_name(name, size)  # tên/cỡ không có → KeyError, kể cả khi đã có trong đệm
    if not dpr > 0:
        raise ValueError(f"dpr phải > 0: {dpr}")
    key = (name, size, color.rgba(), float(dpr))
    cached = _pixmaps.get(key)
    if cached is not None:
        _pixmaps.move_to_end(key)
        return QPixmap(cached)
    data = _read_svg(name, size)
    if data is None:
        return QPixmap()
    result = render_svg(data, size, dpr)
    if result.isNull():
        log.warning("icon %s cỡ %d không phải SVG hợp lệ", name, size)
        return result
    painter = QPainter(result)
    try:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(result.rect(), color)
    finally:
        painter.end()
    _pixmaps[key] = result
    if len(_pixmaps) > CACHE_LIMIT:
        _pixmaps.popitem(last=False)
    return QPixmap(result)


def _ratios() -> list[float]:
    """dpr 1, 2 và dpr lớn nhất của các màn hình đang có (nếu khác 1, 2)."""
    ratios = list(_DPR_SET)
    screens = QGuiApplication.screens()
    if screens:
        top = max(screen.devicePixelRatio() for screen in screens)
        if top > 0 and top not in ratios:
            ratios.append(float(top))
    return ratios


def icon(name: str, size: int, color: QColor, disabled: QColor | None = None) -> QIcon:
    """`QIcon` dựng từ `pixmap` ở nhiều dpr (Normal; Disabled nếu có `disabled`)."""
    result = QIcon()
    for dpr in _ratios():
        normal = pixmap(name, size, color, dpr)
        if not normal.isNull():
            result.addPixmap(normal, QIcon.Mode.Normal)
        if disabled is not None:
            off = pixmap(name, size, disabled, dpr)
            if not off.isNull():
                result.addPixmap(off, QIcon.Mode.Disabled)
    return result


def _clear_cache() -> None:
    """Xóa đệm pixmap và nội dung SVG (cho test)."""
    _pixmaps.clear()
    _svg_data.clear()
