"""Chụp ảnh các màn chính ở sáng/tối, 980x660 và 720x460 (spec A3 mục 2.6, S1; spec B1 mục 7).

Trên CI (offscreen) đây là kiểm tra khói: dựng được mọi cảnh ở cả hai chế độ, ảnh đúng cỡ, cửa sổ
không phình quá cỡ đã đặt, ảnh sáng khác ảnh tối, và điểm (4,4) của cảnh có token góc đúng màu token
đó ở chế độ đó (`surface` cho khối căn giữa và trang, `sidebar` cho khung hai cột vì cột trái nằm ở
góc).

Ảnh để duyệt: đặt `TELEPORT_UI_SNAPSHOT_DIR` thì mỗi cảnh ghi
`<cảnh>_<light|dark>_<w>x<h>.png` vào thư mục đó (tạo nếu chưa có); không đặt thì không ghi gì. Chụp
bằng nền thật của Windows để font và hinting giống thật (conftest chỉ `setdefault` nền offscreen,
nên biến môi trường thắng)::

    $env:QT_QPA_PLATFORM="windows"; $env:TELEPORT_UI_SNAPSHOT_DIR="<thư mục ngoài repo>"
    pytest tests/ui/test_snapshots.py

Ảnh không commit vào repo: đính vào PR. Các PR của đợt B thêm cảnh của mình vào `SCENES`.

Cảnh: `welcome`, `starting`, `running` (một địa chỉ, radar rỗng → “Ghép đôi máy đầu tiên”),
`running_full` (ba thiết bị, thẻ đang gửi, thẻ đã nhận xong, một thông báo), `error`, `widgets` (trang
mẫu mọi thành phần của `ui/widgets.py` trong `QScrollArea`), `page` (RUNNING, mở một trang có
`PageHeader`). Tổng 7 cảnh × 2 chế độ × 2 cỡ = 28 ảnh.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

pytestmark = pytest.mark.slow

SIZES = ((980, 660), (720, 460))
SNAPSHOT_ENV = "TELEPORT_UI_SNAPSHOT_DIR"


@pytest.fixture(scope="module", autouse=True)
def accent_font(qapp):
    """Nạp font điểm nhấn như `ui.app` (trước khi dựng cửa sổ); gỡ lại nếu trước đó chưa nạp."""
    from teleport.ui import fonts

    was_installed = fonts._installed
    fonts.install()
    yield
    if not was_installed:
        fonts._reset()


# ---- Cảnh --------------------------------------------------------------------------------------


def _main_window(qtbot):
    from teleport.ui.main_window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    return window


def scene_welcome(qtbot) -> QWidget:
    window = _main_window(qtbot)
    window.show_welcome("Laptop-Minh")
    return window


def scene_starting(qtbot) -> QWidget:
    window = _main_window(qtbot)
    window.show_starting()
    return window


def scene_running(qtbot) -> QWidget:
    window = _main_window(qtbot)
    window.show_running("Laptop-Minh", Path("C:/Users/Minh/Downloads/Teleport"))
    window.set_addresses(["192.168.1.15"], None)
    return window


def _device(label: str, *, paired: bool, online: bool, connected: bool):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=hashlib.sha256(label.encode()).hexdigest()[:32],
        name=label,
        os="windows" if online else None,
        paired=paired,
        online=online,
        connected=connected,
        addresses=("192.168.1.20",) if online else (),
        port=47474 if online else None,
    )


def _transfer_info(
    device_id: str, direction: str, transfer_id: str, status: str, name: str, size: int, done: int
):
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import Direction, TransferInfo, TransferKey, TransferStatus

    return TransferInfo(
        key=TransferKey(device_id, Direction(direction), transfer_id),
        status=TransferStatus(status),
        files=(FileEntry(name, size, 1_700_000_000_000),),
        names=(name,),
        file_index=0,
        done_bytes=done,
        total_bytes=size,
        unsynced_bytes=0,
    )


def scene_running_full(qtbot) -> QWidget:
    """RUNNING đầy đủ: ba thiết bị, thẻ đang gửi, thẻ đã nhận xong, một thông báo (spec B1 mục 7)."""
    from teleport.app.transfers import (
        FileDone,
        TransferFinished,
        TransferProgress,
        TransferResult,
        TransferStatusChanged,
    )
    from teleport.ui.transfers_view import TransfersView

    window = _main_window(qtbot)
    nha = _device("PC-Nha", paired=True, online=True, connected=True)
    lan = _device("MacBook-Lan", paired=True, online=False, connected=False)
    guest = _device("PC-Khach", paired=False, online=True, connected=False)
    window.radar.model.reset([nha, lan, guest])

    view = TransfersView(window.transfers_area)
    window.transfers_area.layout().addWidget(view)
    view.set_device_names({nha.device_id: nha.name, lan.device_id: lan.name})
    size = 18 * 2**30
    sending = _transfer_info(
        nha.device_id, "send", "a" * 32, "transferring", "phim-4k.mkv", size, size * 62 // 100
    )
    view.handle(TransferStatusChanged(sending))
    view.handle(TransferProgress(sending, 94 * 2**20, 60.0, shown_bytes=sending.done_bytes, idle_seconds=0.0))
    small = 340 * 2**20
    received = _transfer_info(lan.device_id, "receive", "b" * 32, "verifying", "anh-cuoi.zip", small, small)
    view.handle(TransferStatusChanged(received))
    folder = Path("C:/Users/Minh/Downloads/Teleport")
    view.handle(FileDone(received.key, 0, "anh-cuoi.zip", folder / "anh-cuoi.zip"))
    view.handle(
        TransferFinished(
            info=received, result=TransferResult.DONE, by_peer=False, reason=None, file_index=0, reject=None
        )
    )
    view.show_notice("Đã ghép đôi với PC-Nha.")
    window.set_transfer_summary(view.has_content(), view.active_count())

    window.show_running("Laptop-Minh", folder)
    window.set_addresses(["192.168.1.15"], None)
    return window


def scene_error(qtbot) -> QWidget:
    window = _main_window(qtbot)
    window.show_error("port", "[WinError 10048] Only one usage of each socket address")
    return window


def _sample_bubble(direction, emphasis, file_name: str, progress: int | None):
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar

    from teleport.ui import widgets

    bubble = widgets.ChatBubble(direction, emphasis=emphasis)
    head = QHBoxLayout()
    name = QLabel(file_name)
    name.setTextFormat(Qt.TextFormat.PlainText)
    head.addWidget(name, 1)
    head.addWidget(widgets.IconButton("pause", "Tạm dừng"))
    head.addWidget(widgets.IconButton("dismiss", "Hủy"))
    bubble.body().addLayout(head)
    if progress is not None:
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(progress)
        bar.setTextVisible(False)
        bar.setFixedHeight(6)
        bubble.body().addWidget(bar)
    detail = QLabel(f"18 GB · {progress}%" if progress is not None else "18 GB")
    detail.setTextFormat(Qt.TextFormat.PlainText)
    detail.setProperty("tone", "muted")
    bubble.body().addWidget(detail)
    bubble.set_summary(file_name)
    return widgets.BubbleRow(bubble)


def scene_widgets(qtbot) -> QWidget:
    from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QScrollArea, QVBoxLayout

    from teleport.ui import widgets

    area = QScrollArea()
    qtbot.addWidget(area)
    area.setWidgetResizable(True)
    page = QWidget()
    page.setObjectName("snapshotWidgets")
    column = QVBoxLayout(page)
    column.setContentsMargins(0, 0, 0, 16)
    column.setSpacing(12)
    column.addWidget(widgets.PageHeader("Thành phần dùng chung"))

    # Hai cột để một khung hình thấy được nhiều thành phần: trái InfoBar + EmptyState, phải avatar,
    # badge và bong bóng.
    inner = QHBoxLayout()
    inner.setContentsMargins(16, 0, 16, 0)
    inner.setSpacing(16)
    left = QVBoxLayout()
    left.setSpacing(12)
    right = QVBoxLayout()
    right.setSpacing(12)
    levels = (
        (widgets.InfoLevel.INFO, "Đã ghép đôi với PC-Nha", "", None),
        (widgets.InfoLevel.SUCCESS, "Đã nhận anh-cuoi.zip", "Lưu vào Downloads\\Teleport", "Mở thư mục"),
        (widgets.InfoLevel.WARNING, "Mạng chặn tìm máy", "Nhập IP thủ công để ghép đôi.", "Nhập IP"),
        (
            widgets.InfoLevel.ERROR,
            "Không mở được cổng 47474",
            "Một ứng dụng khác đang dùng cổng này.",
            "Thử lại",
        ),
    )
    for level, title, text, action in levels:
        bar = widgets.InfoBar()
        left.addWidget(bar)  # có cha trước, để show_message không mở cửa sổ riêng
        bar.show_message(level, title, text, action=action)

    right.addWidget(_sample_bubble(widgets.Direction.OUT, widgets.BubbleEmphasis.NONE, "phim-4k.mkv", 62))
    right.addWidget(_sample_bubble(widgets.Direction.IN, widgets.BubbleEmphasis.NONE, "anh-cuoi.zip", None))
    right.addWidget(
        _sample_bubble(widgets.Direction.IN, widgets.BubbleEmphasis.ATTENTION, "slide.pptx", None)
    )
    right.addWidget(_sample_bubble(widgets.Direction.OUT, widgets.BubbleEmphasis.DANGER, "bao-cao.pdf", 30))

    avatars = QHBoxLayout()
    avatars.setSpacing(16)
    for presence in widgets.Presence:
        avatar = widgets.DeviceAvatar(presence=presence)
        avatar.set_label(str(presence))
        avatars.addWidget(avatar)
    avatars.addWidget(
        widgets.DeviceAvatar(
            diameter=44, kind=widgets.AvatarKind.LAPTOP, presence=widgets.Presence.ONLINE, emphasis=True
        )
    )
    avatars.addStretch(1)
    right.insertLayout(0, avatars)

    badges = QGridLayout()  # 2x2: một hàng bốn badge làm cột phải tràn ngang ở 720x460
    badges.setHorizontalSpacing(16)
    badges.setVerticalSpacing(8)
    samples = (
        (widgets.BadgeTone.SUCCESS, "Sẵn sàng nhận"),
        (widgets.BadgeTone.BUSY, "Đang truyền"),
        (widgets.BadgeTone.WARNING, "Tạm dừng"),
        (widgets.BadgeTone.MUTED, "Đang ẩn"),
    )
    for index, (tone, text) in enumerate(samples):
        badge = widgets.StatusBadge()
        badges.addWidget(badge, index // 2, index % 2)  # có cha trước, như InfoBar
        badge.set_status(tone, text)
    badges.setColumnStretch(2, 1)
    right.insertLayout(1, badges)
    right.addStretch(1)

    empty = widgets.EmptyState()
    empty.set_content(
        "desktop",
        "Ghép đôi máy đầu tiên",
        "Mở Teleport trên máy kia, rồi bấm Tìm máy mới để thấy nó ở đây.",
        action="Tìm máy mới",
    )
    empty.setMinimumHeight(240)
    left.addWidget(empty)
    left.addStretch(1)
    inner.addLayout(left, 1)
    inner.addLayout(right, 1)
    column.addLayout(inner)
    area.setWidget(page)
    return area


def scene_page(qtbot) -> QWidget:
    from PySide6.QtWidgets import QLineEdit, QVBoxLayout

    from teleport.ui import widgets
    from teleport.ui.main_window import Page

    window = scene_running(qtbot)
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    header = widgets.PageHeader("Tìm máy mới")
    address = QLineEdit()
    address.setPlaceholderText("Nhập IP…")
    address.setFixedWidth(180)
    header.add_trailing(address)
    layout.addWidget(header)
    empty = widgets.EmptyState()
    empty.set_content("search", "Chưa thấy máy nào", "Mở Teleport trên máy kia, cùng mạng Wi-Fi hoặc dây.")
    layout.addWidget(empty, 1)
    page.setFocusProxy(header.back_button)
    window.add_page(Page.DISCOVER, page)
    assert window.open_page(Page.DISCOVER)
    return window


SCENES: dict[str, tuple[Callable[..., QWidget], bool, str | None]] = {
    # tên cảnh: (hàm dựng, là cửa sổ chính, token mà điểm (4,4) phải bằng; None: không kiểm góc)
    "welcome": (scene_welcome, True, "surface"),
    "starting": (scene_starting, True, "surface"),
    "running": (scene_running, True, "sidebar"),
    "running_full": (scene_running_full, True, "sidebar"),
    "error": (scene_error, True, "surface"),
    "widgets": (scene_widgets, False, None),
    "page": (scene_page, True, "surface"),
}


# ---- Chụp --------------------------------------------------------------------------------------


def _grab(widget: QWidget, width: int, height: int) -> QImage:
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    widget.resize(width, height)
    widget.show()
    QApplication.processEvents()
    image = widget.grab().toImage()
    widget.hide()
    return image


def _save(image: QImage, scene: str, mode: str, width: int, height: int) -> None:
    folder = os.environ.get(SNAPSHOT_ENV)
    if not folder:
        return
    path = Path(folder)
    path.mkdir(parents=True, exist_ok=True)
    assert image.save(str(path / f"{scene}_{mode}_{width}x{height}.png"))


@pytest.mark.parametrize(("width", "height"), SIZES, ids=[f"{w}x{h}" for w, h in SIZES])
@pytest.mark.parametrize("scene", list(SCENES))
def test_snapshot(qtbot, ui_theme, scene, width, height):
    build, is_window, corner_token = SCENES[scene]
    images: dict[str, QImage] = {}
    for dark, mode in ((False, "light"), (True, "dark")):
        tokens = ui_theme(dark)
        widget = build(qtbot)
        image = _grab(widget, width, height)
        dpr = widget.devicePixelRatioF()
        assert (image.width(), image.height()) == (round(width * dpr), round(height * dpr)), mode
        if is_window:
            assert (widget.width(), widget.height()) == (width, height), (mode, widget.size())
        if corner_token is not None:
            corner = QColor(image.pixel(round(4 * dpr), round(4 * dpr)))
            expected = getattr(tokens, corner_token)
            assert corner.name() == expected.name(), (mode, corner_token, corner.name())
        _save(image, scene, mode, width, height)
        images[mode] = image
    assert images["light"] != images["dark"]
