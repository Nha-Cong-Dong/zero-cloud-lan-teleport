"""Thành phần con, câu chữ và ảnh chụp cài đặt dùng cho `teleport.ui.settings`."""

from __future__ import annotations

import contextlib
import sys
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QRect,
    QRectF,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QFontMetrics,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QShowEvent,
    QValidator,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QBoxLayout,
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from teleport.app import autostart, storage
from teleport.app import settings as app_settings
from teleport.app import transfers as app_transfers
from teleport.app.devices import DeviceView
from teleport.app.node import Node
from teleport.app.settings import DeviceSettings
from teleport.core.routing import RoutingRule
from teleport.core.transfer import Direction, TransferStatus
from teleport.core.watch import WatchPortalRule
from teleport.ui import motion, texts, theme
from teleport.ui.appearance import section_title as appearance_title
from teleport.ui.send import printable
from teleport.ui.widgets import LABEL_MIN_WIDTH, DeviceAvatar, Presence, safe_tooltip

NAV_WIDTH = 224  # px, cột chọn mục
CONTENT_MAX_WIDTH = 860  # px, độ rộng tối đa khối nội dung (tránh dàn trải ở màn hình rộng)
SECTION_MARGIN = theme.SPACING[4]  # 24 px, lề nội dung của mỗi mục
ROW_SPACING = theme.SPACING[2]  # 12 px, khoảng giữa hai hàng
SPIN_COMMIT_MS = 600  # ms không đổi sau khi bấm mũi tên thì chốt giá trị ô số
MB = 1024 * 1024  # byte của “MB” trong ô tốc độ
RATE_MIN_MB = 0.1
RATE_MAX_MB = 100_000.0
RATE_DEFAULT_MB = 10.0  # số hiện sẵn khi chưa giới hạn
NAME_MAX_INPUT = 256  # ký tự tối đa gõ được vào ô tên (luật thật: `validate_device_name`)
NOTICES_FILE = "THIRD-PARTY-NOTICES.txt"
EXPORT_THREAD = "teleport-logexport"
UPDATE_THREAD = "teleport-update"
_NAV_ICON = 20  # px (`shield_checkmark` có cỡ 20)
_NAV_ROW_HEIGHT = 44  # px
_NAV_PILL_WIDTH = 3  # px, vạch nhấn của mục được chọn
_NAV_PILL_HEIGHT = 20  # px
_NAV_PILL_RADIUS = 1.5  # px
_NAV_PILL_INSET = 4  # px, từ mép item (nằm trên viền trong suốt 2 px)
_AVATAR = 36  # px
_ACTIONS_INDENT = _AVATAR + theme.SPACING[2]  # px, lề trái của ô và nút dưới tên (thẳng cột chữ)
_PALETTE_EVENTS = (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange)
_FIT_PASSES = 4  # số lượt tối đa tính lại chiều cao bảng Phím tắt tới khi đứng yên
_TABLE_ROW_PADDING = 6  # px, trên và dưới chữ mỗi hàng của bảng Phím tắt
_TABLE_CELL_PADDING = 8  # px, hai bên chữ mỗi ô của bảng Phím tắt


class Section(StrEnum):
    """Các mục của trang, đúng thứ tự trong `nav`."""

    GENERAL = "general"
    APPEARANCE = "appearance"
    PRIVACY = "privacy"
    DEVICES = "devices"
    TRANSFER = "transfer"
    ADVANCED = "advanced"


SECTION_ICONS = {
    Section.GENERAL: "settings",
    Section.APPEARANCE: "color",
    Section.PRIVACY: "eye_off",
    Section.DEVICES: "shield_checkmark",
    Section.TRANSFER: "arrow_swap",
    Section.ADVANCED: "wrench",
}

FILE_TYPE_PRESETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("images", ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif", "*.heic")),
    ("videos", ("*.mp4", "*.mov", "*.mkv", "*.avi", "*.webm")),
    ("documents", ("*.pdf", "*.doc", "*.docx", "*.xls", "*.xlsx", "*.ppt", "*.pptx", "*.txt")),
    ("archives", ("*.zip", "*.7z", "*.rar", "*.tar", "*.gz")),
    ("audio", ("*.mp3", "*.flac", "*.wav", "*.m4a", "*.ogg")),
    ("all", ()),
)


def _preset_label(key: str) -> str:
    t = QCoreApplication.translate
    return {
        "images": t("Settings", "Ảnh"),
        "videos": t("Settings", "Video"),
        "documents": t("Settings", "Tài liệu"),
        "archives": t("Settings", "Nén"),
        "audio": t("Settings", "Nhạc"),
        "all": t("Settings", "Mọi file"),
    }[key]


# ---- Câu chữ -----------------------------------------------------------------------------------


def page_title() -> str:
    return QCoreApplication.translate("Settings", "Cài đặt")


def section_title(section: Section) -> str:
    t = QCoreApplication.translate
    return {
        Section.GENERAL: t("Settings", "Chung"),
        Section.APPEARANCE: appearance_title(),
        Section.PRIVACY: t("Settings", "Quyền riêng tư"),
        Section.DEVICES: t("Settings", "Thiết bị tin cậy"),
        Section.TRANSFER: t("Settings", "Truyền file"),
        Section.ADVANCED: t("Settings", "Nâng cao"),
    }[Section(section)]


def name_hint(visible: bool) -> str:
    """Gợi ý dưới ô tên: máy đang hiện → nguyên câu mvp F1; đang ẩn → chỉ máy đã ghép đôi thấy."""
    if visible:
        return texts.device_name_notice()
    return QCoreApplication.translate("Settings", "Máy này đang ẩn nên chỉ máy đã ghép đôi thấy tên này.")


def name_empty_text() -> str:
    return QCoreApplication.translate("Settings", "Hãy nhập tên thiết bị.")


def name_invalid_text() -> str:
    return QCoreApplication.translate(
        "Settings", "Tên thiết bị cần từ 1 đến 64 ký tự và không có ký tự điều khiển."
    )


def autostart_state_hint(state: autostart.AutostartState | None) -> str:
    """Câu thêm của hàng khởi động theo trạng thái registry; None = đọc lỗi."""
    t = QCoreApplication.translate
    if state is None:
        return t("Settings", "Không đọc được cài đặt khởi động của Windows.")
    if state is autostart.AutostartState.BLOCKED:
        return t(
            "Settings",
            "Windows đang tắt Teleport trong danh sách ứng dụng khởi động. Bật công tắc này để cho phép lại.",
        )
    if state is autostart.AutostartState.OTHER:
        return t(
            "Settings",
            "Teleport đang được đặt khởi động cùng Windows từ một thư mục cài hoặc thư mục cấu hình "
            "khác. Bật công tắc để dùng bản này.",
        )
    return ""


def autostart_write_error_text() -> str:
    return QCoreApplication.translate(
        "Settings",
        "Chưa đổi được việc khởi động cùng Windows vì Windows không cho ghi cài đặt khởi động. Hãy thử lại.",
    )


def hidden_text() -> str:
    return QCoreApplication.translate(
        "Settings",
        "Đang ẩn: máy chưa ghép đôi không thấy máy này và không gửi được yêu cầu ghép đôi tới. "
        "Muốn ghép đôi với máy mới, hãy mở Tìm máy mới trên máy này rồi bấm vào máy kia.",
    )


def settings_error_text() -> str:
    """Lưu cài đặt lỗi (`SettingsSaveError`)."""
    return QCoreApplication.translate(
        "Settings",
        "Chưa lưu được cài đặt vì không ghi được vào thư mục cấu hình của Teleport. "
        "Hãy kiểm tra quyền ghi rồi thử lại.",
    )


def other_error_text() -> str:
    return QCoreApplication.translate(
        "Settings", "Chưa lưu được cài đặt vì một lỗi không mong đợi. Hãy thử lại."
    )


def snapshot_error_text() -> str:
    return QCoreApplication.translate("Settings", "Không đọc được cài đặt hiện tại.")


def history_cleared_text() -> str:
    return QCoreApplication.translate("Settings", "Đã xóa toàn bộ lịch sử truyền file.")


def history_clear_failed_text() -> str:
    return QCoreApplication.translate("Settings", "Chưa xóa được lịch sử truyền file. Hãy thử lại.")


def clear_history_title() -> str:
    return QCoreApplication.translate("Settings", "Xóa toàn bộ lịch sử")


def clear_history_question() -> str:
    return QCoreApplication.translate(
        "Settings",
        "Xóa lịch sử truyền file với mọi máy, kể cả máy đã hủy ghép đôi? "
        "File đã nhận vẫn còn nguyên trong thư mục nhận.",
    )


def export_saved_text(path: Path) -> str:
    return texts.fill(
        QCoreApplication.translate("Settings", "Đã lưu log vào {path}."), path=printable(str(path))
    )


def export_write_error_text(path: Path) -> str:
    return texts.fill(
        QCoreApplication.translate(
            "Settings",
            "Không lưu được file log vào {path}. Hãy chọn thư mục khác mà bạn có quyền ghi.",
        ),
        path=printable(str(path)),
    )


def export_other_error_text() -> str:
    return QCoreApplication.translate(
        "Settings", "Không xuất được log vì một lỗi không mong đợi. Hãy thử lại."
    )


def device_status_text(view: DeviceView) -> str:
    t = QCoreApplication.translate
    if view.connected:
        return t("Settings", "đã kết nối")
    if view.online:
        return t("Settings", "đang trong mạng")
    return t("Settings", "không thấy trong mạng")


def pending_status_text(status: str) -> str:
    t = QCoreApplication.translate
    if status == TransferStatus.QUEUED:
        return t("Settings", "đang xếp hàng")
    if status == TransferStatus.WAITING:
        return t("Settings", "đang chờ xác nhận")
    if status == TransferStatus.TRANSFERRING:
        return t("Settings", "đang nhận")
    if status == TransferStatus.VERIFYING:
        return t("Settings", "đang kiểm tra")
    if status == TransferStatus.RECONNECTING:
        return t("Settings", "đang nối lại")
    return t("Settings", "đang chờ")


def cancel_pending_title() -> str:
    return QCoreApplication.translate("Settings", "Hủy lượt nhận đang chờ")


def cancel_pending_question() -> str:
    return QCoreApplication.translate(
        "Settings",
        "Hủy lượt nhận này và xóa phần đã tải dở khỏi máy?",
    )


def cancel_all_pending_title() -> str:
    return QCoreApplication.translate("Settings", "Hủy tất cả lượt nhận dở")


def cancel_all_pending_question() -> str:
    return QCoreApplication.translate(
        "Settings",
        "Hủy toàn bộ lượt nhận đang chờ hoặc dở dang và xóa các phần đã tải dở khỏi máy?",
    )


def pending_cancelled_text() -> str:
    return QCoreApplication.translate("Settings", "Đã hủy lượt nhận và dọn phần tải dở.")


def pending_all_cancelled_text() -> str:
    return QCoreApplication.translate("Settings", "Đã hủy toàn bộ lượt nhận đang chờ.")


def pending_cancel_failed_text() -> str:
    return QCoreApplication.translate("Settings", "Chưa hủy được lượt nhận. Hãy thử lại.")


def install_update_title() -> str:
    return QCoreApplication.translate("Settings", "Cài đặt bản cập nhật")


def install_update_question(version: str) -> str:
    return texts.fill(
        QCoreApplication.translate(
            "Settings",
            "Đã tải xong bản {version}. Đóng Teleport và chạy trình cài đặt ngay bây giờ?",
        ),
        version=version,
    )


def update_launch_failed_text() -> str:
    return QCoreApplication.translate(
        "Settings", "Không thể mở bộ cài cập nhật. Hãy thử tải lại từ trang phát hành."
    )


def default_export_dir() -> str:
    """Thư mục mở sẵn của hộp “Xuất log…”: Documents nếu có thật, không thì thư mục nhà.

    Qt vẫn trả đường dẫn Documents khi thư mục đó chưa được tạo (máy Linux mới), và
    `QFileDialog.setDirectory` bỏ qua thư mục không tồn tại.
    """
    docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    if docs and Path(docs).is_dir():
        return docs
    return str(Path.home())


def default_export_name(now: float | None = None) -> str:
    """`teleport-log-YYYYMMDD-HHMMSS.zip` theo giờ máy."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() if now is None else now))
    return f"teleport-log-{stamp}.zip"


def notices_path() -> Path | None:
    """`THIRD-PARTY-NOTICES.txt` cạnh `sys.executable` nếu có (bản đóng gói), không thì None."""
    try:
        path = Path(sys.executable).resolve().parent / NOTICES_FILE
        return path if path.is_file() else None
    except (OSError, ValueError):
        return None


# ---- Số ----------------------------------------------------------------------------------------


def default_retention_days() -> int:
    """Ngưỡng dọn tạm mặc định (ngày), đọc `storage.TEMP_RETENTION` lúc gọi."""
    return max(1, round(storage.TEMP_RETENTION / 86400))


def rate_to_bytes(mb_per_s: float) -> int:
    """MB/s của ô → byte/giây gửi cho node, kẹp trong khoảng node nhận."""
    value = round(mb_per_s * MB)
    return max(app_transfers.MIN_RATE_LIMIT, min(app_settings.MAX_RATE_LIMIT, value))


def bytes_to_rate(value: int) -> float:
    """byte/giây → MB/s hiện trên ô (một số lẻ, kẹp trong khoảng của ô)."""
    return max(RATE_MIN_MB, min(RATE_MAX_MB, round(value / MB, 1)))


def _nfc_key(view: DeviceView) -> tuple[str, str]:
    return (unicodedata.normalize("NFC", view.name or "").casefold(), view.device_id)


def trusted_devices(views: Sequence[DeviceView]) -> list[DeviceView]:
    """Máy đã ghép đôi (kể cả ngoại tuyến), sắp NFC(tên).casefold() rồi `device_id`."""
    return sorted((v for v in views if isinstance(v, DeviceView) and v.paired), key=_nfc_key)


# ---- Thành phần --------------------------------------------------------------------------------


def _plain(parent: QWidget | None = None, *, object_name: str = "", wrap: bool = True) -> QLabel:
    label = QLabel(parent)
    if object_name:
        label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return label


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class _ElidedLabel(QLabel):
    """Nhãn một dòng tự cắt chữ theo bề ngang (giữa hay cuối); chữ đầy đủ ở `full_text`."""

    def __init__(self, mode: Qt.TextElideMode, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mode = mode
        self._full = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    @property
    def full_text(self) -> str:
        return self._full

    def set_full_text(self, text: str) -> None:
        self._full = text
        self._elide()

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def _elide(self) -> None:
        width = self.contentsRect().width()
        text = self._full
        if text and width > 0:
            text = QFontMetrics(self.font()).elidedText(text, self._mode, width)
        if self.text() != text:
            self.setText(text)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self._elide()


class _NameField(QLineEdit):
    """Ô tên thiết bị: Esc trả tên đã lưu khi chữ khác tên đó; trùng thì để Esc cho cửa sổ."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.saved = ""
        self.setMaxLength(NAME_MAX_INPUT)

    @staticmethod
    def _is_plain_escape(event: QEvent) -> bool:
        return (
            isinstance(event, QKeyEvent)
            and event.key() == Qt.Key.Key_Escape
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        )

    def event(self, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.ShortcutOverride
            and self._is_plain_escape(event)
            and self.text() != self.saved
        ):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._is_plain_escape(event) and self.text() != self.saved:
            self.setText(self.saved)
            self.reverted.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    reverted = Signal()


class _CommitSpin:
    """Chốt giá trị của ô số (spec D2 mục 9.4): `editingFinished`, hay `SPIN_COMMIT_MS` sau mũi tên."""

    def __init__(self, spin: QSpinBox | QDoubleSpinBox, commit: Callable[[], None]) -> None:
        spin.setKeyboardTracking(False)  # gõ phím không phát `valueChanged` tới khi chốt
        self._timer = QTimer(spin)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(commit)
        spin.valueChanged.connect(lambda _v: self._timer.start(SPIN_COMMIT_MS))
        self._commit = commit
        spin.editingFinished.connect(self._finished)

    def _finished(self) -> None:
        self._timer.stop()
        self._commit()

    def stop(self) -> None:
        self._timer.stop()


class _DecimalSpin(QDoubleSpinBox):
    """Ô số lẻ nhận cả “,” lẫn “.” làm dấu thập phân, không có dấu nhóm hàng nghìn.

    `QDoubleSpinBox` theo locale lặng lẽ bỏ dấu nhóm: giao diện English gõ “2,5” thành 25. Ở đây cả
    hai dấu đổi thành dấu thập phân của locale trước khi kiểm, nên “2,5” và “2.5” đều là 2,5, còn
    “1,000” (quá số chữ số lẻ) bị từ chối.
    """

    def _normalized(self, text: str) -> str:
        point = self.locale().decimalPoint()
        return text.replace(",", point).replace(".", point)

    def validate(self, text: str, pos: int) -> tuple[QValidator.State, str, int]:
        return super().validate(self._normalized(text), pos)

    def valueFromText(self, text: str) -> float:
        return super().valueFromText(self._normalized(text))


class _RowShape(StrEnum):
    """Kiểu bố cục của `TrustedDeviceRow`, từ rộng tới hẹp (docstring module, **Danh sách thiết bị**)."""

    ONE_LINE = "one_line"  # [đầu hàng | ô, ô, nút] một dòng
    BELOW = "below"  # ô, ô, nút một dòng dưới tên
    STACKED = "stacked"  # ô, ô, nút mỗi cái một dòng dưới tên


class TrustedDeviceRow(QFrame):
    """Một máy đã ghép đôi: avatar, tên, “HĐH · trạng thái”, “Hỏi trước khi nhận”, “Đồng bộ
    clipboard”, “Hủy ghép đôi…”.

    Bố cục tự chọn kiểu đầu tiên vừa bề ngang thật (`_RowShape`): một dòng khi cột tên còn ít nhất
    `widgets.LABEL_MIN_WIDTH`; không thì ô và nút xuống một dòng dưới tên (thụt `_ACTIONS_INDENT`) khi
    tổng `sizeHint` của chúng cộng khoảng cách và lề thụt vừa; không thì xếp dọc, mỗi control một dòng,
    căn trái, không giãn ngang. Tính lại khi đổi cỡ, sau `retranslate` (gồm `LanguageChange`),
    `FontChange`, `StyleChange` và `LayoutRequest` (con polish lại đổi `sizeHint`); chỉ đổi hướng
    của `_box` và `_actions`, không dựng lại widget nên tiêu điểm và thứ tự Tab (Hỏi → Đồng bộ →
    Hủy ghép đôi) giữ nguyên. Bề ngang dành cho nội dung trừ cả viền QFrame lẫn lề `_box`
    (`_chrome_width`). `minimumSizeHint` là bề ngang của kiểu xếp dọc: hàng đang ở kiểu rộng không ép
    vùng cuộn rộng theo, nên co cửa sổ luôn tới `resizeEvent` để đổi kiểu (không kẹt ở cuộn ngang)."""

    ask_toggled = Signal(str, bool)
    clipboard_toggled = Signal(str, bool)
    unpair_requested = Signal(str)

    def __init__(self, view: DeviceView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("trustedDeviceRow")
        self.setProperty(theme.SURFACE_PROPERTY, "raised")
        # Không cao hơn cần: QBoxLayout chia chỗ thừa cho hàng có nhãn xuống dòng (heightForWidth).
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._view = view
        self._shape: _RowShape | None = None
        self.avatar = DeviceAvatar(self, diameter=_AVATAR, ring="surface_raised")
        self.name_label = _ElidedLabel(Qt.TextElideMode.ElideRight, self)
        self.name_label.setObjectName("trustedDeviceName")
        font = self.name_label.font()
        font.setWeight(font.Weight.DemiBold)
        self.name_label.setFont(font)
        self.detail_label = _plain(self, object_name="")  # ngắn; xuống dòng thay vì cắt
        self.detail_label.setObjectName("trustedDeviceDetail")
        self.detail_label.setFont(theme.font_for(theme.TextStyle.CAPTION, self.detail_label.font()))
        self.detail_label.setProperty(theme.TONE_PROPERTY, "muted")
        self.ask_checkbox = QCheckBox(self)
        self.ask_checkbox.setObjectName("askBeforeReceive")
        self.ask_checkbox.setChecked(True)
        self.clipboard_checkbox = QCheckBox(self)
        self.clipboard_checkbox.setObjectName("syncClipboard")
        self.clipboard_checkbox.setChecked(False)
        self.unpair_button = QPushButton(self)
        self.unpair_button.setObjectName("unpairButton")
        self.ask_checkbox.clicked.connect(
            lambda checked: self.ask_toggled.emit(self.device_id, bool(checked))
        )
        self.clipboard_checkbox.clicked.connect(
            lambda checked: self.clipboard_toggled.emit(self.device_id, bool(checked))
        )
        self.unpair_button.clicked.connect(lambda: self.unpair_requested.emit(self.device_id))

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(0)
        text.addWidget(self.name_label)
        text.addWidget(self.detail_label)
        self._head = QHBoxLayout()
        self._head.setContentsMargins(0, 0, 0, 0)
        self._head.setSpacing(theme.SPACING[2])
        self._head.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignVCenter)
        self._head.addLayout(text, 1)
        self._actions = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._actions.setContentsMargins(0, 0, 0, 0)
        self._actions.setSpacing(theme.SPACING[2])
        self._actions.addWidget(self.ask_checkbox)
        self._actions.addWidget(self.clipboard_checkbox)
        self._actions.addWidget(self.unpair_button)
        self._actions.addStretch(0)
        # Kiểu bố cục (`_RowShape`) chỉ đổi hướng của `_box` và `_actions` (`_place_actions`).
        self._box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._box.setContentsMargins(theme.SPACING[3], theme.SPACING[2], theme.SPACING[3], theme.SPACING[2])
        self._box.setSpacing(theme.SPACING[3])
        self._box.addLayout(self._head, 1)
        self._box.addLayout(self._actions, 0)
        self.set_view(view)  # `retranslate` đặt chữ rồi chọn kiểu bố cục

    @property
    def device_id(self) -> str:
        return self._view.device_id

    @property
    def view(self) -> DeviceView:
        return self._view

    def focus_widgets(self) -> list[QWidget]:
        return [self.ask_checkbox, self.clipboard_checkbox, self.unpair_button]

    def set_view(self, view: DeviceView) -> None:
        self._view = view
        self.retranslate()

    def set_ask(self, ask: bool) -> None:
        """Đặt ô “Hỏi trước khi nhận” (không phát `ask_toggled`)."""
        self.ask_checkbox.setChecked(bool(ask))

    def set_clipboard(self, enabled: bool) -> None:
        """Đặt ô “Đồng bộ clipboard” (không phát `clipboard_toggled`)."""
        self.clipboard_checkbox.setChecked(bool(enabled))

    def retranslate(self) -> None:
        view = self._view
        name = texts.peer_label(view.name, view.device_id)
        os_name = texts.os_label(view.os)
        status = device_status_text(view)
        detail = f"{os_name} · {status}" if os_name else status
        self.name_label.set_full_text(name)
        self.detail_label.setText(detail)
        summary = ", ".join(part for part in (name, os_name, status) if part)
        self.setAccessibleName(summary)
        self.setToolTip(safe_tooltip(f"{name}\n{detail}"))
        self.avatar.set_presence(Presence.ONLINE if (view.connected or view.online) else Presence.OFFLINE)
        ask = QCoreApplication.translate("Settings", "Hỏi trước khi nhận")
        self.ask_checkbox.setText(ask)
        self.ask_checkbox.setAccessibleName(
            texts.fill(QCoreApplication.translate("Settings", "{option}: {name}"), option=ask, name=name)
        )
        clipboard = QCoreApplication.translate("Settings", "Đồng bộ clipboard")
        self.clipboard_checkbox.setText(clipboard)
        option = QCoreApplication.translate("Settings", "{option}: {name}")
        self.clipboard_checkbox.setAccessibleName(texts.fill(option, option=clipboard, name=name))
        unpair = QCoreApplication.translate("Settings", "Hủy ghép đôi…")
        self.unpair_button.setText(unpair)
        self.unpair_button.setAccessibleName(
            texts.fill(QCoreApplication.translate("Settings", "{option}: {name}"), option=unpair, name=name)
        )
        self.ask_checkbox.setAccessibleDescription(summary)
        self.clipboard_checkbox.setAccessibleDescription(summary)
        self.unpair_button.setAccessibleDescription(summary)
        self._place_actions()  # chữ mới đổi `sizeHint` của ô và nút

    def _action_hints(self) -> list[int]:
        return [w.sizeHint().width() for w in self.focus_widgets() if not w.isHidden()]

    def _shape_for_width(self, width: int) -> _RowShape:
        """Kiểu đầu tiên vừa bề ngang `width` của hàng (chỉ phụ thuộc `width` và `sizeHint` control).

        Trừ cả viền QFrame (`frameWidth`, 1 px với `surface="raised"` của theme) lẫn lề `_box`: đó là
        chỗ thật `_box` được cấp."""
        inner = width - self._chrome_width()
        hints = self._action_hints()
        actions = sum(hints) + theme.SPACING[2] * max(len(hints) - 1, 0)
        name_column = inner - _AVATAR - self._head.spacing() - theme.SPACING[3] - actions
        if name_column >= LABEL_MIN_WIDTH:
            return _RowShape.ONE_LINE
        if _ACTIONS_INDENT + actions <= inner:
            return _RowShape.BELOW
        return _RowShape.STACKED

    def _place_actions(self) -> None:
        shape = self._shape_for_width(self.width())
        if shape is self._shape:
            return
        self._shape = shape
        if shape is _RowShape.ONE_LINE:
            self._box.setDirection(QBoxLayout.Direction.LeftToRight)
            self._box.setSpacing(theme.SPACING[3])
            self._actions.setContentsMargins(0, 0, 0, 0)
        else:
            self._box.setDirection(QBoxLayout.Direction.TopToBottom)
            self._box.setSpacing(theme.SPACING[1])
            self._actions.setContentsMargins(_ACTIONS_INDENT, 0, 0, 0)
        stacked = shape is _RowShape.STACKED
        self._actions.setDirection(
            QBoxLayout.Direction.TopToBottom if stacked else QBoxLayout.Direction.LeftToRight
        )
        self._actions.setSpacing(theme.SPACING[1] if stacked else theme.SPACING[2])
        # Xếp dọc: căn trái để ô và nút không giãn ngang hết hàng.
        align = Qt.AlignmentFlag.AlignLeft if stacked else Qt.AlignmentFlag(0)
        for widget in self.focus_widgets():
            self._actions.setAlignment(widget, align)
        self.updateGeometry()

    def _chrome_width(self) -> int:
        """Bề ngang hàng không dành cho nội dung `_box`: viền QFrame hai bên cộng lề trái/phải."""
        margins = self._box.contentsMargins()
        return 2 * self.frameWidth() + margins.left() + margins.right()

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        widest = max(self._action_hints(), default=0)
        head = _AVATAR + self._head.spacing() + self.name_label.minimumSizeHint().width()
        width = self._chrome_width() + max(head, _ACTIONS_INDENT + widest)
        return QSize(width, hint.height())

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._place_actions()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        kind = event.type()
        if kind == QEvent.Type.LanguageChange:
            self.retranslate()
        elif kind in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._place_actions()

    def event(self, event: QEvent) -> bool:
        handled = super().event(event)
        # Con polish lại (sau StyleChange/FontChange) gọi `updateGeometry`, gửi `LayoutRequest` tới
        # hàng: `sizeHint` mới có thể đổi kiểu. Không lặp: `_place_actions` chỉ `updateGeometry` khi
        # kiểu đổi, và `updateGeometry` của hàng gửi `LayoutRequest` tới cha, không tới chính nó.
        if event.type() == QEvent.Type.LayoutRequest:
            self._place_actions()
        return handled


# ---- Ảnh chụp cài đặt --------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingReceiveItem:
    """Một lượt nhận đang chờ hoặc dở dang để hiện ở mục Truyền file."""

    transfer_id: str
    device_id: str
    device_name: str
    files_total: int
    done_bytes: int
    total_bytes: int
    status: str


class UpdateUiState(StrEnum):
    """Trạng thái giao diện kiểm tra và tải bản cập nhật thủ công."""

    IDLE = "idle"
    CHECKING = "checking"
    UP_TO_DATE = "up_to_date"
    AVAILABLE = "available"
    DOWNLOADING = "downloading"
    READY = "ready"
    ERROR = "error"


@dataclass(frozen=True)
class SettingsSnapshot:
    """Cài đặt hiện tại đọc trên thread node (`read_snapshot`)."""

    name: str
    visible: bool
    rate_limit: int | None
    temp_retention_days: int | None
    language: str | None
    autostart: bool
    devices: Mapping[str, DeviceSettings]
    create_subfolder: bool = True
    theme: str | None = None  # trường `theme` của settings.json (None = theo hệ điều hành)
    accent: str | None = None  # trường `accent` (None = mặc định; mã lạ giữ nguyên)
    sound_mode: str | None = None  # trường `sound_mode` (None = mặc định full)
    sound_volume: int | None = None  # trường `sound_volume` (None = mặc định 70)
    auto_dismiss_notices: bool = True  # trường `auto_dismiss_notices` (True = tự ẩn thông báo)
    tray_notifications: bool = True  # trường `tray_notifications` (True = hiện bóng thoại khay)
    stealth: bool = False
    pending_receives: tuple[PendingReceiveItem, ...] = ()
    promised_bytes: int = 0
    delta_sync: bool = True
    routing_rules: tuple[RoutingRule, ...] = ()
    watch_portals: tuple[WatchPortalRule, ...] = ()
    watch_portals_paused: bool = False
    device_names: Mapping[str, str] = field(default_factory=dict)


def _read_device_settings(node: Node) -> dict[str, DeviceSettings]:
    return {v.device_id: node.device_settings(v.device_id) for v in node.devices() if v.paired}


def _read_device_names(node: Node) -> dict[str, str]:
    devices_fn = getattr(node, "devices", None)
    if not callable(devices_fn):
        return {}
    return {v.device_id: v.name for v in devices_fn() if hasattr(v, "device_id")}


def read_snapshot(node: Node) -> SettingsSnapshot:
    """Thread node: ảnh chụp cài đặt cho trang (tên đang dùng, ẩn/hiện, cài đặt theo thiết bị)."""
    settings = node.settings
    names = _read_device_names(node)
    stealth = bool(getattr(node, "stealth", False))
    transfers_mgr = getattr(node, "transfers", None)
    pending: list[PendingReceiveItem] = []
    transfers_fn = getattr(transfers_mgr, "transfers", None)
    if callable(transfers_fn):
        for info in transfers_fn():
            if info.key.direction is Direction.RECEIVE:
                pending.append(
                    PendingReceiveItem(
                        transfer_id=info.key.transfer_id,
                        device_id=info.key.device_id,
                        device_name=names.get(info.key.device_id) or "",
                        files_total=len(info.files),
                        done_bytes=info.done_bytes,
                        total_bytes=info.total_bytes,
                        status=str(info.status),
                    )
                )
    promised_fn = getattr(transfers_mgr, "promised_bytes", None)
    promised_bytes = max(0, int(promised_fn())) if callable(promised_fn) else 0
    return SettingsSnapshot(
        name=node.name,
        visible=node.visible,
        rate_limit=settings.rate_limit,
        temp_retention_days=settings.temp_retention_days,
        language=settings.language,
        autostart=settings.autostart,
        devices=_read_device_settings(node),
        create_subfolder=settings.create_subfolder,
        theme=settings.theme,
        accent=settings.accent,
        sound_mode=getattr(settings, "sound_mode", None),
        sound_volume=getattr(settings, "sound_volume", None),
        auto_dismiss_notices=bool(getattr(settings, "auto_dismiss_notices", True)),
        tray_notifications=bool(getattr(settings, "tray_notifications", True)),
        stealth=stealth,
        pending_receives=tuple(pending),
        promised_bytes=promised_bytes,
        delta_sync=bool(getattr(settings, "delta_sync", True)),
        routing_rules=tuple(getattr(settings, "routing_rules", ())),
        watch_portals=tuple(getattr(settings, "watch_portals", ())),
        watch_portals_paused=bool(getattr(node, "watch_portals_paused", False)),
        device_names=names,
    )


# ---- Trang -------------------------------------------------------------------------------------


class _ShortcutTable(QTableWidget):
    """Bảng phím tắt chỉ để xem (xem docstring module, “Bảng Phím tắt”)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, 2, parent)
        self.setObjectName("shortcutsTable")
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # Tab và bấm chuột
        self.setTabKeyNavigation(False)
        self.setAlternatingRowColors(False)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setShowGrid(False)
        self.setCornerButtonEnabled(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        vertical = self.verticalHeader()
        vertical.setVisible(False)
        vertical.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setHighlightSections(False)
        header.setSectionsClickable(False)
        self._fitting = False
        self._refit = False
        # Cột giãn đổi bề ngang sau khi bảng đổi cỡ (header tự hẹn lại bố cục): tính lại ở vòng lặp sau.
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(0)
        self._fit_timer.timeout.connect(self.fit_height)
        header.sectionResized.connect(lambda *_args: self._fit_timer.start())
        # Lề ngang của ô (chữ không dính mép khung); cột 1 vừa nội dung nên tính cả lề này.
        self.setStyleSheet(
            f"QTableWidget::item {{ padding: 0px {_TABLE_CELL_PADDING}px; }}"
            f"QHeaderView::section {{ padding: 4px {_TABLE_CELL_PADDING + 1}px; }}"
        )

    def set_rows(self, rows: Sequence[tuple[str, str]], headers: tuple[str, str]) -> None:
        """Đặt chữ các hàng (mô tả, phím) và tiêu đề cột; giữ hàng đang chọn."""
        current = self.currentRow()
        self.setHorizontalHeaderLabels(list(headers))
        self.setRowCount(len(rows))
        for row, (description, key) in enumerate(rows):
            for column, text in enumerate((description, key)):
                item = self.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    self.setItem(row, column, item)
                item.setText(text)
                spoken = texts.fill("{description}: {key}", description=description, key=key)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, spoken if column == 0 else key)
                item.setToolTip("")
        if 0 <= current < len(rows) and self.currentRow() != current:
            self.setCurrentCell(current, 0)
        self.fit_height()
        self._fit_timer.start()

    def fit_height(self) -> None:
        """Cao vừa đúng tiêu đề và mọi hàng (không thanh cuộn).

        Đổi chiều cao kéo theo đổi bề ngang cột (và chiều cao hàng xuống dòng): lời gọi tới trong lúc
        đang tính thì tính lại sau, tới khi đứng yên."""
        if self._fitting:
            self._refit = True
            return
        self._fitting = True
        try:
            for _ in range(_FIT_PASSES):
                self._refit = False
                self._fit_rows()
                header = self.horizontalHeader()
                height = max(header.height(), header.sizeHint().height()) if not header.isHidden() else 0
                height += sum(self.rowHeight(row) for row in range(self.rowCount()))
                height += 2 * self.frameWidth()
                if self.height() != height or self.minimumHeight() != height:
                    self.setFixedHeight(height)
                if not self._refit:
                    break
        finally:
            self._fitting = False

    def _fit_rows(self) -> None:
        """Chiều cao mỗi hàng theo chữ xuống dòng ở bề ngang cột hiện tại (không dùng
        `resizeRowsToContents`: tiêu đề dọc đã ẩn vẫn góp cỡ của nó vào hàng)."""
        metrics = QFontMetrics(self.font())
        style = self.style()
        frame = style.pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin, None, self) + 1
        margin = 2 * (frame + _TABLE_CELL_PADDING)
        flags = int(Qt.TextFlag.TextWordWrap)
        header = self.horizontalHeader()
        for row in range(self.rowCount()):
            height = metrics.height()
            for column in range(self.columnCount()):
                item = self.item(row, column)
                if item is None:
                    continue
                width = max(1, header.sectionSize(column) - margin)
                rect = metrics.boundingRect(QRect(0, 0, width, 0), flags, item.text())
                height = max(height, rect.height())
            target = height + 2 * _TABLE_ROW_PADDING
            if self.rowHeight(row) != target:
                self.setRowHeight(row, target)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.fit_height()
        self._fit_timer.start()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._fit_timer.start()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.fit_height()
            self._fit_timer.start()

    def wheelEvent(self, event: QWheelEvent) -> None:
        # Không có gì để cuộn trong bảng: để bánh xe tới vùng cuộn của mục chứa bảng.
        event.ignore()


class _NavList(QListWidget):
    """Cột chọn mục: vẽ vạch nhấn của mục được chọn, trượt khi đổi mục (`selection_indicator`)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.selection_indicator = motion.SlidingRect(self.viewport(), motion.NORMAL)
        self._slide_pending = False
        self.currentRowChanged.connect(self._on_current_row)

    def _on_current_row(self, _row: int) -> None:
        self._slide_pending = self.isVisible()
        self.viewport().update()

    def _pill_rect(self) -> QRectF | None:
        item = self.currentItem()
        if item is None:
            return None
        rect = self.visualItemRect(item)
        if not rect.isValid():
            return None
        if self.isRightToLeft():
            left = rect.right() + 1 - _NAV_PILL_INSET - _NAV_PILL_WIDTH
        else:
            left = rect.left() + _NAV_PILL_INSET
        top = rect.top() + (rect.height() - _NAV_PILL_HEIGHT) / 2
        return QRectF(left, top, _NAV_PILL_WIDTH, _NAV_PILL_HEIGHT)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        indicator = self.selection_indicator
        indicator.move_to(self._pill_rect(), animate=self._slide_pending)
        self._slide_pending = False
        rect = indicator.rect
        if rect is None:
            return
        painter = QPainter(self.viewport())
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(theme.tokens_for(self).accent_strong)
            painter.drawRoundedRect(rect, _NAV_PILL_RADIUS, _NAV_PILL_RADIUS)
        finally:
            painter.end()


class _blocked(contextlib.AbstractContextManager[None]):
    """`with _blocked(w):` chặn signal của `w` trong khối (giá trị đặt từ ngoài không phát signal)."""

    def __init__(self, widget: QObject) -> None:
        self._widget = widget
        self._old = False

    def __enter__(self) -> None:
        self._old = self._widget.blockSignals(True)

    def __exit__(self, *exc: object) -> None:
        self._widget.blockSignals(self._old)
