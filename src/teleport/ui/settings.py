"""Trang “Cài đặt” (`Page.SETTINGS`, mvp F10, mục 7.4, N9, N12; spec D2).

Hai phần, theo mẫu `ui.discover`:

- **`SettingsPage`** (view, objectName `settingsPage`): không bao giờ gọi node; người dùng đổi gì thì
  phát signal, còn `set_*` đặt giá trị mà **không** phát signal. Bố cục: `PageHeader("Cài đặt")`; cột
  trái `nav` (`_NavList`, một `QListWidget` objectName `settingsNav`, rộng `NAV_WIDTH`, icon cỡ 20 +
  chữ) chọn mục; bên phải một `info_bar` cho cả trang rồi `sections` (`QStackedWidget`, mỗi
  `Section` một `QScrollArea` không viền, lề `SECTION_MARGIN`, khoảng hàng `ROW_SPACING`). Mỗi
  hàng là một `SettingsRow`.
- **`SettingsController`** (`GuiApp.settings`): nối signal của trang với node qua `NodeHost.call`
  (thread node), registry tự khởi động (`app.autostart`, thread Qt), hộp xác nhận và hộp lưu file
  (mở bằng `open()`, không `exec()`), việc xuất log (thread riêng).

**Các mục** (`Section`, thứ tự cố định): Chung (tên thiết bị, thư mục nhận, tự tạo thư mục con, khởi động
cùng Windows) · Giao diện (`ui.appearance.AppearanceSection`, spec D6: chế độ màu, màu nhấn, ngôn
ngữ) · Quyền riêng tư (cho máy chưa ghép đôi thấy máy này, chế độ ẩn hoàn toàn `stealth`, xóa toàn
bộ lịch sử) · Thiết bị tin cậy (mỗi máy đã ghép đôi một `TrustedDeviceRow`: “Hỏi trước khi nhận”,
“Đồng bộ clipboard” (objectName `syncClipboard`, mặc định tắt; clip chỉ đi khi cả hai máy cùng bật,
mvp F8), “Hủy ghép đôi…”; không máy nào thì `EmptyState` + “Tìm máy mới”) · Truyền file (giới hạn tốc
độ gửi, ngưỡng dọn phần nhận dở, quản lý các lượt nhận đang chờ hoặc dở dang) · Nâng cao (xuất log,
bảng “Phím tắt” chỉ để xem, phiên bản, kiểm tra cập nhật thủ công chỉ khi bấm nút theo mvp N9, giấy
phép, “Thông báo bên thứ ba” khi có file `THIRD-PARTY-NOTICES.txt` cạnh `sys.executable`). Hàng thư mục
nhận: chữ theo ba trạng thái của `ui.receive_dir` (đang đổi sang / thư mục nhận / thư mục đã lưu hỏng),
“Mở” (chỉ khi có thư mục nhận) và “Đổi…” (ẩn tới `set_change_available(True)`; `ui.app` bật và nối vào
`ReceiveDirController.choose`).

**Hàng** (`widgets.SettingsRow`, `QFrame[surface="raised"]`, bo `RADIUS_CARD`): nhãn đậm vừa, gợi ý
màu dịu xuống dòng, control bên phải; hàng hẹp tới mức cột chữ còn dưới `widgets.LABEL_MIN_WIDTH` thì control
xuống hàng dưới (cửa sổ `MIN_SIZE` không cuộn ngang). Control mang tên trợ năng = nhãn hàng và mô tả =
gợi ý. Hàng thiết bị tin cậy có bố cục riêng (xem **Danh sách thiết bị**).

**Số** (spec D2 mục 9.1, không viết cứng): ngưỡng dọn tạm mặc định = `storage.TEMP_RETENTION / 86400`
(đọc lúc dùng), khoảng `settings.MIN_RETENTION_DAYS`–`MAX_RETENTION_DAYS`; tốc độ hiện theo MB/s
(MB = `MB` = 1 048 576 byte, một số lẻ, 0,1–100 000; dấu thập phân theo ngôn ngữ giao diện), gửi
`round(MB/s × MB)` kẹp trong `app.transfers.MIN_RATE_LIMIT`–`settings.MAX_RATE_LIMIT`. Giá trị bằng
mặc định → gửi None. Hậu tố của ô ngày theo số (English “1 day” / “2 days”). Hai ô số chỉ phát khi
**chốt** (spec D2 mục 9.4): Enter hay rời ô (`editingFinished`), hoặc `SPIN_COMMIT_MS`
không đổi sau khi bấm mũi tên; không phát khi bằng giá trị đã biết.

**Ô tên** (`_NameField`): Enter / rời ô thì kiểm (`core.messages.validate_device_name` sau `strip`);
sai thì hiện lỗi ngay dưới ô (`name_error`, màu `danger` kèm chữ), giữ chữ, không phát; đúng và khác tên
đã lưu thì phát `name_submitted`. Esc khi chữ khác tên đã lưu: ô nhận `ShortcutOverride` rồi trả tên
cũ; trùng tên đã lưu thì Esc tới cửa sổ (Quay lại).

**Danh sách thiết bị**: đọc `radar.DeviceListModel` dùng chung (`MainWindow.devices`), theo signal
của model như `DiscoverPage._sync`; chỉ máy `paired`, sắp NFC(tên).casefold() rồi `device_id`; máy
ngoại tuyến vẫn hiện; hàng dùng lại theo `device_id`. Hàng đang có tiêu điểm bị bỏ → tiêu điểm sang
hàng kế, không có thì hàng trước, không còn thì nút của màn rỗng. Tên qua `texts.peer_label`,
chữ thuần, tooltip qua `widgets.safe_tooltip`; tên trợ năng của hàng “{tên}, {HĐH}, {trạng thái}”.
Bố cục hàng (`TrustedDeviceRow`) chọn kiểu đầu tiên vừa bề ngang thật, không theo ngưỡng cứng: một dòng
`[avatar tên/chi tiết | Hỏi… | Đồng bộ… | Hủy ghép đôi…]` khi cột tên còn ≥ `widgets.LABEL_MIN_WIDTH`;
ô và nút xuống một dòng dưới tên, thụt thẳng cột chữ, khi tổng `sizeHint` của chúng vừa; không thì xếp
dọc dưới tên, mỗi control một dòng, căn trái. Nhờ vậy chữ dài của ngôn ngữ khác (English) không làm
cửa sổ `MIN_SIZE` cuộn ngang hay cắt chữ; thứ tự Tab không đổi theo kiểu.

**Bàn phím** (mvp N12): Tab: Quay lại → `nav` → thanh thông báo (khi hiện) → control của mục đang mở
(mục Giao diện: trong mỗi nhóm chỉ phần tử đang chọn nhận Tab, mũi tên đi giữa các phần tử) → hàng
thiết bị → nút màn rỗng; mục Nâng cao: “Xuất log…” → bảng Phím tắt → “Thông báo bên thứ ba”. Lên/Xuống
trong `nav` đổi mục; trang hiện thì tiêu điểm vào `nav` (`setFocusProxy`). Chuyển động (spec G3): chỉ
vạch nhấn của `nav` (`_NavList`: pill `accent_strong` rộng 3 px, cao 16 px ở mép trái mục được chọn,
`motion.SlidingRect`) trượt tới mục mới khi đổi mục lúc cột đang hiện; vùng `sections` đổi tức thì.
Hộp xác nhận xóa lịch sử mờ dần khi hiện (`motion.fade_in_on_show`).
`show_section(section, device_id, focus=…)`: mục Thiết bị tin cậy với `device_id` → tiêu điểm vào ô
“Hỏi trước khi nhận” của máy đó, hay ô “Đồng bộ clipboard” khi `focus="clipboard"` và trang đã có ảnh
chụp cài đặt (gợi ý khi dán không phải file ở luồng chat, qua `ui.app`).

**Bảng Phím tắt** (`shortcuts_table`, `_ShortcutTable`, `QTableWidget` objectName `shortcutsTable`,
nằm dưới gợi ý của hàng `shortcutsRow`): hai cột “Thao tác” | “Phím” từ `ui.shortcuts.table_rows()`
(không chép danh sách phím ra đây), không sửa được, chọn một hàng, không tô xen kẽ, không tiêu đề
dọc, không thanh cuộn: cột 0 giãn và xuống dòng (không cắt chữ), cột 1 vừa nội dung, cao vừa đúng số
hàng (tính lại khi đổi bề ngang, font, kiểu, ngôn ngữ). Bánh xe chuột trên bảng cuộn mục chứa nó.
Tab không đi giữa các ô (`tabKeyNavigation` tắt): một điểm dừng Tab, mũi tên đổi hàng. Trợ năng: tên
“Phím tắt”, mô tả là gợi ý của hàng; ô cột 0 đọc “{mô tả}: {phím}”, ô cột 1 đọc phím. Đổi ngôn ngữ thì
dựng lại chữ, giữ hàng đang chọn.

**Controller** (bảng ở spec D2 mục 1.6, 9): lưu trước, áp sau; lỗi thì trả control về giá trị đã xác
nhận gần nhất của trường đó, nhưng chỉ khi đó là lời gọi mới nhất của trường (`_seq`); kết quả của host
cũ hay về sau `close_all` bị bỏ (`_generation`). `SettingsSaveError` → câu SETTINGS; `MessageError` của
tên → lỗi ngay dưới ô (không trả chữ về); `KeyError` của “Hỏi trước khi nhận” và “Đồng bộ clipboard”
(`node.set_clipboard`) → im lặng, đọc lại;
`RuntimeError` (node không chạy) / `CancelledError` → im lặng (DEBUG); lỗi khác → câu OTHER, log ERROR chỉ
tên loại lỗi (mvp N9). Ẩn/hiện: `node.set_visible(v)` và `host.apply_pairing_policy(node)` trong **cùng
một** `call` (hợp đồng ở docstring `ui.bridge`). Ngôn ngữ: lưu xong mới `i18n.apply_preference`; chế độ
màu và màu nhấn: lưu xong (`set_ui_preferences(theme=…)` / `(accent=…)` trên thread node) mới
`theme.set_preference` trên thread Qt. Mỗi ảnh chụp cài đặt khác lựa chọn đang áp thì cũng áp theo ảnh
chụp.

**Tự khởi động** (registry thắng, spec D2 mục 1.2): lúc `started` và mỗi lần trang hiện, đọc
`autostart.state` trên thread Qt; khác `settings.autostart` thì chỉ ghi `set_ui_preferences(autostart=…)`
(không bao giờ tự ghi registry). Bấm công tắc: ghi registry trước (lỗi → trả công tắc, báo, không lưu),
rồi lưu cài đặt (lỗi chỉ WARNING), rồi đọc lại trạng thái.

**Xuất log**: hộp lưu (`dialog_factory`, seam cấp module đọc lúc gọi) mặc định
`teleport-log-YYYYMMDD-HHMMSS.zip` trong thư mục Tài liệu; chọn xong thì `export_fn` (seam, mặc định
`app.logexport.export_logs`) chạy trong `ThreadPoolExecutor(1, "teleport-logexport")`, ngoài thread Qt
và loop node; kết quả về qua signal `QueuedConnection` của một `QObject` sống cùng controller. Worker chỉ
giữ `Path`/`str`. Rủi ro chấp nhận: xuất đúng lúc file log xoay vòng có thể thiếu vài dòng.

**Xóa toàn bộ lịch sử**: hộp xác nhận (mặc định và Esc là “Không”) → `node.transfers.clear_history()`;
báo “Đã xóa…” khi lời gọi trả về (việc ghi `history.json` lỗi chỉ WARNING ở `app`), phát
`history_cleared(None)`.

Câu chữ ở ngữ cảnh `"Settings"`, gọi `QCoreApplication.translate` lúc dùng; `retranslate()` khi nhận
`LanguageChange` (giữ lựa chọn đang có).
"""

from __future__ import annotations

import functools
import itertools
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QLocale,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtCore import (
    QStandardPaths as QStandardPaths,
)
from PySide6.QtGui import QHideEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

import teleport
from teleport.app import autostart, logexport, updater
from teleport.app import settings as app_settings
from teleport.app.settings import DeviceSettings
from teleport.app.updater import UpdateInfo
from teleport.core.messages import PROTOCOL_VERSION, MessageError, validate_device_name
from teleport.core.routing import RoutingRule, parse_patterns
from teleport.core.watch import WatchPortalRule
from teleport.ui import i18n, icons, shortcuts, texts, theme
from teleport.ui.appearance import AppearanceSection
from teleport.ui.radar import DeviceListModel
from teleport.ui.send import printable
from teleport.ui.settings_controller import (
    SettingsController as SettingsController,
)
from teleport.ui.settings_controller import (
    _apply_theme as _apply_theme,
)
from teleport.ui.settings_controller import (
    _deliver_check as _deliver_check,
)
from teleport.ui.settings_controller import (
    _deliver_download as _deliver_download,
)
from teleport.ui.settings_controller import (
    _deliver_export as _deliver_export,
)
from teleport.ui.settings_controller import (
    _download_worker as _download_worker,
)
from teleport.ui.settings_controller import (
    _ExportRelay as _ExportRelay,
)
from teleport.ui.settings_controller import (
    _retry_text as _retry_text,
)
from teleport.ui.settings_controller import (
    _translate_cancel_pending_box as _translate_cancel_pending_box,
)
from teleport.ui.settings_controller import (
    _translate_clear_history_box as _translate_clear_history_box,
)
from teleport.ui.settings_controller import (
    _translate_install_update_box as _translate_install_update_box,
)
from teleport.ui.settings_controller import (
    _UpdateRelay as _UpdateRelay,
)
from teleport.ui.settings_controller import (
    _warn as _warn,
)
from teleport.ui.settings_widgets import (
    _ACTIONS_INDENT as _ACTIONS_INDENT,
)
from teleport.ui.settings_widgets import (
    _AVATAR as _AVATAR,
)
from teleport.ui.settings_widgets import (
    _FIT_PASSES as _FIT_PASSES,
)
from teleport.ui.settings_widgets import (
    _NAV_ICON as _NAV_ICON,
)
from teleport.ui.settings_widgets import (
    _NAV_PILL_HEIGHT as _NAV_PILL_HEIGHT,
)
from teleport.ui.settings_widgets import (
    _NAV_PILL_INSET as _NAV_PILL_INSET,
)
from teleport.ui.settings_widgets import (
    _NAV_PILL_RADIUS as _NAV_PILL_RADIUS,
)
from teleport.ui.settings_widgets import (
    _NAV_PILL_WIDTH as _NAV_PILL_WIDTH,
)
from teleport.ui.settings_widgets import (
    _NAV_ROW_HEIGHT as _NAV_ROW_HEIGHT,
)
from teleport.ui.settings_widgets import (
    _PALETTE_EVENTS as _PALETTE_EVENTS,
)
from teleport.ui.settings_widgets import (
    _TABLE_CELL_PADDING as _TABLE_CELL_PADDING,
)
from teleport.ui.settings_widgets import (
    _TABLE_ROW_PADDING as _TABLE_ROW_PADDING,
)
from teleport.ui.settings_widgets import (
    CONTENT_MAX_WIDTH as CONTENT_MAX_WIDTH,
)
from teleport.ui.settings_widgets import (
    EXPORT_THREAD as EXPORT_THREAD,
)
from teleport.ui.settings_widgets import (
    FILE_TYPE_PRESETS as FILE_TYPE_PRESETS,
)
from teleport.ui.settings_widgets import (
    MB as MB,
)
from teleport.ui.settings_widgets import (
    NAME_MAX_INPUT as NAME_MAX_INPUT,
)
from teleport.ui.settings_widgets import (
    NAV_WIDTH as NAV_WIDTH,
)
from teleport.ui.settings_widgets import (
    NOTICES_FILE as NOTICES_FILE,
)
from teleport.ui.settings_widgets import (
    RATE_DEFAULT_MB as RATE_DEFAULT_MB,
)
from teleport.ui.settings_widgets import (
    RATE_MAX_MB as RATE_MAX_MB,
)
from teleport.ui.settings_widgets import (
    RATE_MIN_MB as RATE_MIN_MB,
)
from teleport.ui.settings_widgets import (
    ROW_SPACING as ROW_SPACING,
)
from teleport.ui.settings_widgets import (
    SECTION_ICONS as SECTION_ICONS,
)
from teleport.ui.settings_widgets import (
    SECTION_MARGIN as SECTION_MARGIN,
)
from teleport.ui.settings_widgets import (
    SPIN_COMMIT_MS as SPIN_COMMIT_MS,
)
from teleport.ui.settings_widgets import (
    UPDATE_THREAD as UPDATE_THREAD,
)
from teleport.ui.settings_widgets import (
    PendingReceiveItem as PendingReceiveItem,
)
from teleport.ui.settings_widgets import (
    Section as Section,
)
from teleport.ui.settings_widgets import (
    SettingsSnapshot as SettingsSnapshot,
)
from teleport.ui.settings_widgets import (
    TrustedDeviceRow as TrustedDeviceRow,
)
from teleport.ui.settings_widgets import (
    UpdateUiState as UpdateUiState,
)
from teleport.ui.settings_widgets import (
    _blocked as _blocked,
)
from teleport.ui.settings_widgets import (
    _CommitSpin as _CommitSpin,
)
from teleport.ui.settings_widgets import (
    _DecimalSpin as _DecimalSpin,
)
from teleport.ui.settings_widgets import (
    _ElidedLabel as _ElidedLabel,
)
from teleport.ui.settings_widgets import (
    _NameField as _NameField,
)
from teleport.ui.settings_widgets import (
    _NavList as _NavList,
)
from teleport.ui.settings_widgets import (
    _nfc_key as _nfc_key,
)
from teleport.ui.settings_widgets import (
    _plain as _plain,
)
from teleport.ui.settings_widgets import (
    _preset_label as _preset_label,
)
from teleport.ui.settings_widgets import (
    _read_device_names as _read_device_names,
)
from teleport.ui.settings_widgets import (
    _read_device_settings as _read_device_settings,
)
from teleport.ui.settings_widgets import (
    _repolish as _repolish,
)
from teleport.ui.settings_widgets import (
    _RowShape as _RowShape,
)
from teleport.ui.settings_widgets import (
    _ShortcutTable as _ShortcutTable,
)
from teleport.ui.settings_widgets import (
    autostart_state_hint as autostart_state_hint,
)
from teleport.ui.settings_widgets import (
    autostart_write_error_text as autostart_write_error_text,
)
from teleport.ui.settings_widgets import (
    bytes_to_rate as bytes_to_rate,
)
from teleport.ui.settings_widgets import (
    cancel_all_pending_question as cancel_all_pending_question,
)
from teleport.ui.settings_widgets import (
    cancel_all_pending_title as cancel_all_pending_title,
)
from teleport.ui.settings_widgets import (
    cancel_pending_question as cancel_pending_question,
)
from teleport.ui.settings_widgets import (
    cancel_pending_title as cancel_pending_title,
)
from teleport.ui.settings_widgets import (
    clear_history_question as clear_history_question,
)
from teleport.ui.settings_widgets import (
    clear_history_title as clear_history_title,
)
from teleport.ui.settings_widgets import (
    default_export_dir as default_export_dir,
)
from teleport.ui.settings_widgets import (
    default_export_name as default_export_name,
)
from teleport.ui.settings_widgets import (
    default_retention_days as default_retention_days,
)
from teleport.ui.settings_widgets import (
    device_status_text as device_status_text,
)
from teleport.ui.settings_widgets import (
    export_other_error_text as export_other_error_text,
)
from teleport.ui.settings_widgets import (
    export_saved_text as export_saved_text,
)
from teleport.ui.settings_widgets import (
    export_write_error_text as export_write_error_text,
)
from teleport.ui.settings_widgets import (
    hidden_text as hidden_text,
)
from teleport.ui.settings_widgets import (
    history_clear_failed_text as history_clear_failed_text,
)
from teleport.ui.settings_widgets import (
    history_cleared_text as history_cleared_text,
)
from teleport.ui.settings_widgets import (
    install_update_question as install_update_question,
)
from teleport.ui.settings_widgets import (
    install_update_title as install_update_title,
)
from teleport.ui.settings_widgets import (
    name_empty_text as name_empty_text,
)
from teleport.ui.settings_widgets import (
    name_hint as name_hint,
)
from teleport.ui.settings_widgets import (
    name_invalid_text as name_invalid_text,
)
from teleport.ui.settings_widgets import (
    notices_path as notices_path,
)
from teleport.ui.settings_widgets import (
    other_error_text as other_error_text,
)
from teleport.ui.settings_widgets import (
    page_title as page_title,
)
from teleport.ui.settings_widgets import (
    pending_all_cancelled_text as pending_all_cancelled_text,
)
from teleport.ui.settings_widgets import (
    pending_cancel_failed_text as pending_cancel_failed_text,
)
from teleport.ui.settings_widgets import (
    pending_cancelled_text as pending_cancelled_text,
)
from teleport.ui.settings_widgets import (
    pending_status_text as pending_status_text,
)
from teleport.ui.settings_widgets import (
    rate_to_bytes as rate_to_bytes,
)
from teleport.ui.settings_widgets import (
    read_snapshot as read_snapshot,
)
from teleport.ui.settings_widgets import (
    section_title as section_title,
)
from teleport.ui.settings_widgets import (
    settings_error_text as settings_error_text,
)
from teleport.ui.settings_widgets import (
    snapshot_error_text as snapshot_error_text,
)
from teleport.ui.settings_widgets import (
    trusted_devices as trusted_devices,
)
from teleport.ui.settings_widgets import (
    update_launch_failed_text as update_launch_failed_text,
)
from teleport.ui.widgets import (
    EmptyState,
    InfoBar,
    InfoLevel,
    PageHeader,
    SettingsRow,
    ToggleSwitch,
    safe_tooltip,
)

log = logging.getLogger(__name__)


def _default_auto_install_supported() -> bool:
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


# Seam (đọc lúc gọi): hộp chọn file lưu log, hàm xuất log và cập nhật thủ công.
dialog_factory: Callable[[QWidget | None], QFileDialog] = QFileDialog
export_fn: Callable[[Sequence[Path], Path], Path] = logexport.export_logs
check_update_fn: Callable[..., UpdateInfo | None] = updater.check_for_update
download_update_fn: Callable[..., Path] = updater.download_update_installer
apply_update_fn: Callable[..., bool] = updater.apply_update_installer
auto_install_supported: Callable[[], bool] = _default_auto_install_supported


class SettingsPage(QWidget):
    """Trang “Cài đặt” (xem docstring module). Không gọi node; `set_*` không phát signal."""

    name_submitted = Signal(str)
    visible_toggled = Signal(bool)
    stealth_toggled = Signal(bool)
    language_selected = Signal(object)  # mã ngôn ngữ hay None (theo hệ điều hành)
    theme_selected = Signal(object)  # None (theo hệ điều hành) | "light" | "dark"
    accent_selected = Signal(object)  # None (mặc định) | mã màu nhấn
    sound_mode_selected = Signal(object)  # None (mặc định full) | "notify" | "off"
    sound_volume_selected = Signal(object)  # None (mặc định 70) | int 0..100
    auto_dismiss_notices_toggled = Signal(bool)
    tray_notifications_toggled = Signal(bool)
    autostart_toggled = Signal(bool)
    create_subfolder_toggled = Signal(bool)
    rate_limit_changed = Signal(object)  # byte/giây hay None
    retention_changed = Signal(object)  # ngày hay None (= mặc định)
    delta_sync_toggled = Signal(bool)
    routing_rules_changed = Signal(object)  # tuple[RoutingRule, ...]
    watch_portals_changed = Signal(object)  # tuple[WatchPortalRule, ...]
    watch_portals_paused_toggled = Signal(bool)
    cancel_pending_requested = Signal(str, str)  # device_id, transfer_id
    cancel_all_pending_requested = Signal()
    ask_toggled = Signal(str, bool)
    clipboard_toggled = Signal(str, bool)  # device_id, bật “Đồng bộ clipboard”
    unpair_requested = Signal(str)
    clear_history_requested = Signal()
    export_logs_requested = Signal()
    verify_receipt_requested = Signal()
    check_update_requested = Signal()
    download_update_requested = Signal()
    open_receive_dir_requested = Signal(object)  # Path
    change_receive_dir_requested = Signal()
    discover_requested = Signal()
    open_notices_requested = Signal(object)  # Path
    notice_action = Signal()

    def __init__(self, model: DeviceListModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self.model = model
        self._loaded = False
        self._visible = True
        self._stealth = False
        self._name = ""
        self._autostart: autostart.AutostartState | None = autostart.AutostartState.OFF
        self._rate: int | None = None  # giá trị đã biết (đặt từ ngoài hay vừa phát)
        self._retention: int | None = None
        self._pending_receives: tuple[PendingReceiveItem, ...] = ()
        self._promised_bytes = 0
        self._pending_rows: list[QFrame] = []
        self._pending_cancel_buttons: list[QPushButton] = []
        self._delta_sync = True
        self._routing_rules: tuple[RoutingRule, ...] = ()
        self._watch_portals: tuple[WatchPortalRule, ...] = ()
        self._watch_portals_paused = False
        self._device_names: dict[str, str] = {}
        self._routing_rows: list[QFrame] = []
        self._routing_controls: list[QWidget] = []
        self._watch_rows: list[QFrame] = []
        self._watch_controls: list[QWidget] = []
        self._rule_dialog: QDialog | None = None
        self._dir_picker: QFileDialog | None = None
        self._device_settings: dict[str, DeviceSettings] = {}
        self._rows: dict[str, TrustedDeviceRow] = {}
        self._order: list[str] = []
        self._receive_dir: Path | None = None
        self._unavailable: Path | None = None
        self._pending: Path | None = None
        self._export_running = False
        self._update_state = UpdateUiState.IDLE
        self._update_info: UpdateInfo | None = None
        self._update_progress: tuple[int, int] | None = None
        self._notices = notices_path()
        self._name_error: str | None = None

        self.header = PageHeader(page_title(), self)
        self.nav = _NavList(self)
        self.nav.setObjectName("settingsNav")
        self.nav.setFixedWidth(NAV_WIDTH)
        self.nav.setFrameShape(QFrame.Shape.NoFrame)
        self.nav.setIconSize(QSize(_NAV_ICON, _NAV_ICON))
        self.nav.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setUniformItemSizes(True)
        for section in Section:
            item = QListWidgetItem(self.nav)
            item.setData(Qt.ItemDataRole.UserRole, section.value)
            item.setSizeHint(QSize(0, _NAV_ROW_HEIGHT))
        self.info_bar = InfoBar(self)
        self.info_bar.setObjectName("settingsInfo")
        self.info_bar.action_triggered.connect(self.notice_action)
        self.sections = QStackedWidget(self)
        self.sections.setObjectName("settingsSections")
        self._section_widgets: dict[Section, QScrollArea] = {}
        self._section_contents: dict[Section, QWidget] = {}
        self._section_titles: dict[Section, QLabel] = {}
        self._section_layouts: dict[Section, QVBoxLayout] = {}
        for section in Section:
            self._add_section(section)

        self._build_general()
        self._build_appearance()
        self._build_privacy()
        self._build_devices()
        self._build_transfer()
        self._build_advanced()

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.info_bar.setMaximumWidth(CONTENT_MAX_WIDTH)
        self.info_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar_row = QHBoxLayout()
        bar_row.setContentsMargins(0, 0, 0, 0)
        bar_row.setSpacing(0)
        bar_row.addStretch(1)
        bar_row.addWidget(self.info_bar, 1000)
        bar_row.addStretch(1)
        bar_box = QVBoxLayout()
        bar_box.setContentsMargins(SECTION_MARGIN, theme.SPACING[2], SECTION_MARGIN, 0)
        bar_box.addLayout(bar_row)
        right.addLayout(bar_box)
        right.addWidget(self.sections, 1)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.nav)
        body.addLayout(right, 1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.header)
        outer.addLayout(body, 1)

        self.nav.currentRowChanged.connect(self._on_nav_row)
        self.nav.setCurrentRow(0)
        self.setFocusProxy(self.nav)
        for signal in (
            model.modelReset,
            model.rowsInserted,
            model.rowsRemoved,
            model.rowsMoved,
            model.dataChanged,
            model.layoutChanged,
        ):
            signal.connect(self._sync_devices)

        self._restyle()
        self.retranslate()
        self._sync_devices()
        self.set_loaded(False)

    # ---- Dựng các mục --------------------------------------------------------------------------

    def _add_section(self, section: Section) -> None:
        scroll = QScrollArea(self.sections)
        scroll.setObjectName(f"settingsSection_{section.value}")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        viewport_host = QWidget()
        viewport_host.setObjectName(f"settingsHost_{section.value}")
        viewport_host.setAutoFillBackground(False)
        content = QWidget(viewport_host)
        content.setObjectName(f"settingsContent_{section.value}")
        content.setMaximumWidth(CONTENT_MAX_WIDTH)
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        content.setAutoFillBackground(False)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(ROW_SPACING)
        title = _plain(content, object_name=f"settingsTitle_{section.value}")
        title.setFont(theme.font_for(theme.TextStyle.TITLE, title.font()))
        layout.addWidget(title)
        layout.addSpacing(theme.SPACING[1])
        host_layout = QHBoxLayout(viewport_host)
        host_layout.setContentsMargins(SECTION_MARGIN, theme.SPACING[3], SECTION_MARGIN, SECTION_MARGIN)
        host_layout.setSpacing(0)
        host_layout.addStretch(1)
        host_layout.addWidget(content, 1000)
        host_layout.addStretch(1)
        scroll.setWidget(viewport_host)
        viewport = scroll.viewport()
        if viewport is not None:
            viewport.setAutoFillBackground(False)
        self.sections.addWidget(scroll)
        self._section_widgets[section] = scroll
        self._section_contents[section] = content
        self._section_titles[section] = title
        self._section_layouts[section] = layout

    def _row(self, section: Section, object_name: str) -> SettingsRow:
        row = SettingsRow(self._section_contents[section], object_name=object_name)
        self._section_layouts[section].addWidget(row)
        return row

    def _build_general(self) -> None:
        self.name_row = self._row(Section.GENERAL, "nameRow")
        self.name_edit = _NameField(self.name_row)
        self.name_edit.setObjectName("deviceNameEdit")
        self.name_edit.setMinimumWidth(240)
        self.name_edit.editingFinished.connect(self._on_name_finished)
        self.name_edit.textEdited.connect(lambda _text: self.set_name_error(None))
        self.name_edit.reverted.connect(lambda: self.set_name_error(None))
        self.name_row.set_control(self.name_edit)
        self.name_error = _plain(self.name_row, object_name="deviceNameError")
        self.name_error.setProperty(theme.TONE_PROPERTY, "danger")
        self.name_error.setVisible(False)
        self.name_row.add_line(self.name_error)

        self.receive_row = self._row(Section.GENERAL, "receiveDirRow")
        self.receive_dir_label = _ElidedLabel(Qt.TextElideMode.ElideMiddle, self.receive_row)
        self.receive_dir_label.setObjectName("receiveDirLabel")
        self.receive_dir_label.setProperty(theme.TONE_PROPERTY, "muted")
        self.receive_row.add_line(self.receive_dir_label)
        buttons = QWidget(self.receive_row)
        buttons_layout = QHBoxLayout(buttons)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(theme.SPACING[1])
        self.open_dir_button = QPushButton(buttons)
        self.open_dir_button.setObjectName("openReceiveDir")
        self.change_dir_button = QPushButton(buttons)
        self.change_dir_button.setObjectName("changeReceiveDir")
        self.change_dir_button.setVisible(False)
        buttons_layout.addWidget(self.open_dir_button)
        buttons_layout.addWidget(self.change_dir_button)
        buttons.setProperty("keepAccessible", True)
        self.receive_row.set_control(buttons)
        self.open_dir_button.clicked.connect(self._on_open_dir)
        self.change_dir_button.clicked.connect(self.change_receive_dir_requested)

        self.create_subfolder_row = self._row(Section.GENERAL, "createSubfolderRow")
        self.create_subfolder_switch = ToggleSwitch(self.create_subfolder_row)
        self.create_subfolder_switch.setObjectName("createSubfolderSwitch")
        self.create_subfolder_switch.setChecked(True)
        self.create_subfolder_switch.clicked.connect(
            lambda checked: self.create_subfolder_toggled.emit(bool(checked))
        )
        self.create_subfolder_row.set_control(self.create_subfolder_switch)

        self.autostart_row = self._row(Section.GENERAL, "autostartRow")
        self.autostart_switch = ToggleSwitch(self.autostart_row)
        self.autostart_switch.setObjectName("autostartSwitch")
        self.autostart_switch.clicked.connect(lambda checked: self.autostart_toggled.emit(bool(checked)))
        self.autostart_row.set_control(self.autostart_switch)
        self.autostart_state_label = _plain(self.autostart_row, object_name="autostartState")
        self.autostart_state_label.setProperty(theme.TONE_PROPERTY, "warning")
        self.autostart_state_label.setVisible(False)
        self.autostart_row.add_line(self.autostart_state_label)
        self._section_layouts[Section.GENERAL].addStretch(1)

    def _build_appearance(self) -> None:
        layout = self._section_layouts[Section.APPEARANCE]
        self.appearance = AppearanceSection(self._section_contents[Section.APPEARANCE])
        self.appearance.theme_selected.connect(self.theme_selected)
        self.appearance.accent_selected.connect(self.accent_selected)
        self.appearance.language_selected.connect(self.language_selected)
        self.appearance.sound_mode_selected.connect(self.sound_mode_selected)
        self.appearance.sound_volume_selected.connect(self.sound_volume_selected)
        self.appearance.auto_dismiss_notices_toggled.connect(self.auto_dismiss_notices_toggled)
        self.appearance.tray_notifications_toggled.connect(self.tray_notifications_toggled)
        layout.addWidget(self.appearance)
        layout.addStretch(1)

    @property
    def language_row(self) -> SettingsRow:
        return self.appearance.language_row

    @property
    def language_combo(self) -> QComboBox:
        return self.appearance.language_combo

    def _build_privacy(self) -> None:
        self.visible_row = self._row(Section.PRIVACY, "visibleRow")
        self.visible_switch = ToggleSwitch(self.visible_row)
        self.visible_switch.setObjectName("visibleSwitch")
        self.visible_switch.setChecked(True)
        self.visible_switch.clicked.connect(self._on_visible_clicked)
        self.visible_row.set_control(self.visible_switch)
        self.hidden_label = _plain(self.visible_row, object_name="hiddenNotice")
        self.hidden_label.setProperty(theme.TONE_PROPERTY, "warning")
        self.hidden_label.setVisible(False)
        self.visible_row.add_line(self.hidden_label)

        self.stealth_row = self._row(Section.PRIVACY, "stealthRow")
        self.stealth_switch = ToggleSwitch(self.stealth_row)
        self.stealth_switch.setObjectName("stealthSwitch")
        self.stealth_switch.setChecked(False)
        self.stealth_switch.clicked.connect(self._on_stealth_clicked)
        self.stealth_row.set_control(self.stealth_switch)

        self.history_row = self._row(Section.PRIVACY, "historyRow")
        self.clear_history_button = QPushButton(self.history_row)
        self.clear_history_button.setObjectName("clearHistoryButton")
        self.clear_history_button.clicked.connect(self.clear_history_requested)
        self.history_row.set_control(self.clear_history_button)
        self._section_layouts[Section.PRIVACY].addStretch(1)

    def _build_devices(self) -> None:
        content = self._section_contents[Section.DEVICES]
        layout = self._section_layouts[Section.DEVICES]
        self.devices_intro = _plain(content, object_name="trustedIntro")
        self.devices_intro.setProperty(theme.TONE_PROPERTY, "muted")
        layout.addWidget(self.devices_intro)
        self.device_list = QWidget(content)
        self.device_list.setObjectName("trustedDeviceList")
        self._device_layout = QVBoxLayout(self.device_list)
        self._device_layout.setContentsMargins(0, 0, 0, 0)
        self._device_layout.setSpacing(ROW_SPACING)
        layout.addWidget(self.device_list)
        self.devices_empty = EmptyState(content)
        self.devices_empty.setObjectName("trustedEmpty")
        self.devices_empty.action_triggered.connect(self.discover_requested)
        layout.addWidget(self.devices_empty)
        layout.addStretch(1)

    def _build_transfer(self) -> None:
        self.rate_row = self._row(Section.TRANSFER, "rateRow")
        box = QWidget(self.rate_row)
        box_layout = QHBoxLayout(box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.setSpacing(theme.SPACING[1])
        self.rate_checkbox = QCheckBox(box)
        self.rate_checkbox.setObjectName("rateCheckbox")
        self.rate_spin = _DecimalSpin(box)
        self.rate_spin.setObjectName("rateSpin")
        self.rate_spin.setDecimals(1)
        self.rate_spin.setRange(RATE_MIN_MB, RATE_MAX_MB)
        self.rate_spin.setSingleStep(1.0)
        self.rate_spin.setValue(RATE_DEFAULT_MB)
        self.rate_spin.setEnabled(False)
        self.rate_spin.setAccelerated(True)
        box_layout.addWidget(self.rate_checkbox)
        box_layout.addWidget(self.rate_spin)
        box.setProperty("keepAccessible", True)
        self.rate_row.set_control(box)
        self.rate_checkbox.clicked.connect(self._on_rate_checkbox)
        self._rate_commit = _CommitSpin(self.rate_spin, self._commit_rate)

        self.retention_row = self._row(Section.TRANSFER, "retentionRow")
        self.retention_spin = QSpinBox(self.retention_row)
        self.retention_spin.setObjectName("retentionSpin")
        self.retention_spin.setRange(app_settings.MIN_RETENTION_DAYS, app_settings.MAX_RETENTION_DAYS)
        self.retention_spin.setValue(default_retention_days())
        self.retention_spin.valueChanged.connect(self._update_retention_suffix)
        self.retention_row.set_control(self.retention_spin)
        self._retention_commit = _CommitSpin(self.retention_spin, self._commit_retention)

        self.pending_row = self._row(Section.TRANSFER, "pendingTransfersRow")
        self.cancel_all_pending_button = QPushButton(self.pending_row)
        self.cancel_all_pending_button.setObjectName("cancelAllPendingButton")
        self.cancel_all_pending_button.setProperty("keepAccessible", True)
        self.cancel_all_pending_button.setVisible(False)
        self.cancel_all_pending_button.clicked.connect(self.cancel_all_pending_requested)
        self.pending_row.set_control(self.cancel_all_pending_button)
        transfer_content = self._section_contents[Section.TRANSFER]
        self.pending_list = QWidget(transfer_content)
        self.pending_list.setObjectName("pendingTransfersList")
        self._pending_layout = QVBoxLayout(self.pending_list)
        self._pending_layout.setContentsMargins(0, 0, 0, 0)
        self._pending_layout.setSpacing(ROW_SPACING)
        self.pending_list.setVisible(False)
        self._section_layouts[Section.TRANSFER].addWidget(self.pending_list)

        self.delta_row = self._row(Section.TRANSFER, "deltaSyncRow")
        self.delta_switch = ToggleSwitch(self.delta_row)
        self.delta_switch.setObjectName("deltaSyncSwitch")
        self.delta_switch.setChecked(True)
        self.delta_switch.clicked.connect(self._on_delta_clicked)
        self.delta_row.set_control(self.delta_switch)

        self.routing_row = self._row(Section.TRANSFER, "routingRulesRow")
        self.add_routing_button = QPushButton(self.routing_row)
        self.add_routing_button.setObjectName("addRoutingRuleButton")
        self.add_routing_button.clicked.connect(self._open_add_routing_dialog)
        self.routing_row.set_control(self.add_routing_button)
        self.routing_list = QWidget(transfer_content)
        self.routing_list.setObjectName("routingRulesList")
        self._routing_layout = QVBoxLayout(self.routing_list)
        self._routing_layout.setContentsMargins(0, 0, 0, 0)
        self._routing_layout.setSpacing(ROW_SPACING)
        self.routing_list.setVisible(False)
        self._section_layouts[Section.TRANSFER].addWidget(self.routing_list)

        self.watch_row = self._row(Section.TRANSFER, "watchPortalsRow")
        watch_controls = QWidget(self.watch_row)
        watch_box = QHBoxLayout(watch_controls)
        watch_box.setContentsMargins(0, 0, 0, 0)
        watch_box.setSpacing(theme.SPACING[1])
        self.watch_pause_button = QPushButton(watch_controls)
        self.watch_pause_button.setObjectName("pauseWatchPortalsButton")
        self.watch_pause_button.setCheckable(True)
        self.watch_pause_button.setVisible(False)
        self.watch_pause_button.clicked.connect(self._on_watch_pause_clicked)
        self.add_watch_button = QPushButton(watch_controls)
        self.add_watch_button.setObjectName("addWatchPortalButton")
        self.add_watch_button.clicked.connect(self._open_add_watch_dialog)
        watch_box.addWidget(self.watch_pause_button)
        watch_box.addWidget(self.add_watch_button)
        watch_controls.setProperty("keepAccessible", True)
        self.watch_row.set_control(watch_controls)
        self.watch_list = QWidget(transfer_content)
        self.watch_list.setObjectName("watchPortalsList")
        self._watch_layout = QVBoxLayout(self.watch_list)
        self._watch_layout.setContentsMargins(0, 0, 0, 0)
        self._watch_layout.setSpacing(ROW_SPACING)
        self.watch_list.setVisible(False)
        self._section_layouts[Section.TRANSFER].addWidget(self.watch_list)

        self._section_layouts[Section.TRANSFER].addStretch(1)

    def _build_advanced(self) -> None:
        self.export_row = self._row(Section.ADVANCED, "exportRow")
        self.export_button = QPushButton(self.export_row)
        self.export_button.setObjectName("exportLogsButton")
        self.export_button.clicked.connect(self.export_logs_requested)
        self.export_row.set_control(self.export_button)

        self.shortcuts_row = self._row(Section.ADVANCED, "shortcutsRow")
        self.shortcuts_table = _ShortcutTable(self.shortcuts_row)
        self.shortcuts_row.add_line(self.shortcuts_table)

        self.version_row = self._row(Section.ADVANCED, "versionRow")

        self.update_row = self._row(Section.ADVANCED, "updateRow")
        update_controls = QWidget(self.update_row)
        update_layout = QHBoxLayout(update_controls)
        update_layout.setContentsMargins(0, 0, 0, 0)
        update_layout.setSpacing(theme.SPACING[1])
        self.check_update_button = QPushButton(update_controls)
        self.check_update_button.setObjectName("checkUpdateButton")
        self.check_update_button.clicked.connect(self.check_update_requested)
        self.download_update_button = QPushButton(update_controls)
        self.download_update_button.setObjectName("downloadUpdateButton")
        self.download_update_button.setProperty(theme.PRIMARY_PROPERTY, True)
        self.download_update_button.setVisible(False)
        self.download_update_button.clicked.connect(self.download_update_requested)
        update_layout.addWidget(self.check_update_button)
        update_layout.addWidget(self.download_update_button)
        update_controls.setProperty("keepAccessible", True)
        self.update_row.set_control(update_controls)
        self.update_status_label = _plain(self.update_row, object_name="updateStatusLabel")
        self.update_status_label.setProperty(theme.TONE_PROPERTY, "muted")
        self.update_status_label.setVisible(False)
        self.update_row.add_line(self.update_status_label)

        self.license_row = self._row(Section.ADVANCED, "licenseRow")
        self.notices_button = QPushButton(self.license_row)
        self.notices_button.setObjectName("noticesButton")
        self.notices_button.clicked.connect(self._on_notices)
        if self._notices is not None:
            self.license_row.set_control(self.notices_button)
        else:
            self.notices_button.setVisible(False)

        self.verify_receipt_row = self._row(Section.ADVANCED, "verifyReceiptRow")
        self.verify_receipt_button = QPushButton(self.verify_receipt_row)
        self.verify_receipt_button.setObjectName("verifyReceiptButton")
        self.verify_receipt_button.clicked.connect(self.verify_receipt_requested)
        self.verify_receipt_row.set_control(self.verify_receipt_button)
        self._section_layouts[Section.ADVANCED].addStretch(1)

    # ---- API -----------------------------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def current_section(self) -> Section:
        item = self.nav.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else Section.GENERAL.value
        return Section(value)

    def device_rows(self) -> list[TrustedDeviceRow]:
        """Hàng thiết bị theo thứ tự đang hiện."""
        return [self._rows[i] for i in self._order]

    def device_row(self, device_id: str) -> TrustedDeviceRow | None:
        return self._rows.get(device_id)

    def device_ids(self) -> list[str]:
        return list(self._order)

    def _ask_of(self, device_id: str) -> bool:
        return self._device_settings.get(device_id, DeviceSettings()).ask_before_receive

    def _clipboard_of(self, device_id: str) -> bool:
        return self._device_settings.get(device_id, DeviceSettings()).clipboard

    def set_loaded(self, loaded: bool) -> None:
        """Đã có ảnh chụp cài đặt: bật các control đọc/ghi node.

        Mục Nâng cao và hàng Thư mục nhận (theo `ReceiveDirController`, không theo ảnh chụp) luôn dùng
        được."""
        self._loaded = bool(loaded)
        for section in (Section.PRIVACY, Section.DEVICES, Section.TRANSFER):
            content = self._section_contents[section]
            content.setEnabled(self._loaded)
        for row in (self.name_row, self.create_subfolder_row, self.autostart_row):
            row.setEnabled(self._loaded)
        self.appearance.set_loaded(self._loaded)

    def set_values(self, s: SettingsSnapshot) -> None:
        """Đặt mọi giá trị từ ảnh chụp (không phát signal)."""
        self.set_name(s.name)
        self.set_visible(s.visible)
        self.set_stealth(s.stealth)
        self.set_rate_limit(s.rate_limit)
        self.set_retention(s.temp_retention_days)
        self.set_pending_receives(s.pending_receives, s.promised_bytes)
        self._device_names = dict(s.device_names)
        self.set_delta_sync(s.delta_sync)
        self.set_routing_rules(s.routing_rules)
        self.set_watch_portals(s.watch_portals)
        self.set_watch_portals_paused(s.watch_portals_paused)
        self.set_language(s.language, i18n.available_languages())
        self.appearance.set_theme(s.theme)
        self.appearance.set_accent(s.accent)
        self.appearance.set_sound_mode(s.sound_mode)
        self.appearance.set_sound_volume(s.sound_volume)
        self.appearance.set_auto_dismiss_notices(s.auto_dismiss_notices)
        self.appearance.set_tray_notifications(s.tray_notifications)
        with _blocked(self.create_subfolder_switch):
            self.create_subfolder_switch.setChecked(bool(s.create_subfolder))
        with _blocked(self.autostart_switch):
            self.autostart_switch.setChecked(bool(s.autostart))
        self.set_device_settings(s.devices)

    def set_name(self, name: str) -> None:
        self._name = name
        self.name_edit.saved = name
        self.name_edit.setText(name)
        self.set_name_error(None)

    def set_name_error(self, text: str | None) -> None:
        self._name_error = text
        self.name_error.setText(text or "")
        self.name_error.setVisible(bool(text))
        description = self.hint_for_name()
        self.name_edit.setAccessibleDescription(f"{text} {description}" if text else description)

    def reject_name(self, confirmed: str, error: str) -> None:
        """Node từ chối tên vừa gửi: tên đã lưu trở về `confirmed`, giữ chữ trong ô, hiện lỗi.

        Enter lần nữa gửi lại (chữ trong ô khác tên đã lưu)."""
        self._name = confirmed
        self.name_edit.saved = confirmed
        self.set_name_error(error)

    def hint_for_name(self) -> str:
        return name_hint(self._visible)

    def set_visible(self, visible: bool) -> None:
        self._visible = bool(visible)
        with _blocked(self.visible_switch):
            self.visible_switch.setChecked(self._visible)
        self._update_visibility_texts()

    def set_stealth(self, enabled: bool) -> None:
        self._stealth = bool(enabled)
        with _blocked(self.stealth_switch):
            self.stealth_switch.setChecked(self._stealth)

    def set_pending_receives(self, items: Sequence[PendingReceiveItem], promised_bytes: int = 0) -> None:
        self._pending_receives = tuple(items)
        self._promised_bytes = max(0, int(promised_bytes))
        self._rebuild_pending_receives()
        self._update_tab_order()

    def set_delta_sync(self, enabled: bool) -> None:
        self._delta_sync = bool(enabled)
        with _blocked(self.delta_switch):
            self.delta_switch.setChecked(self._delta_sync)

    def set_routing_rules(self, rules: Sequence[RoutingRule]) -> None:
        self._routing_rules = tuple(rules)
        self._rebuild_routing_rules()
        self._update_tab_order()

    def set_watch_portals(self, rules: Sequence[WatchPortalRule]) -> None:
        self._watch_portals = tuple(rules)
        self._rebuild_watch_portals()
        self._update_tab_order()

    def set_watch_portals_paused(self, paused: bool) -> None:
        """Đặt trạng thái tạm dừng mọi cổng đẩy (không phát signal)."""
        self._watch_portals_paused = bool(paused)
        with _blocked(self.watch_pause_button):
            self.watch_pause_button.setChecked(self._watch_portals_paused)
        self._rebuild_watch_portals()
        self._update_tab_order()

    def close_dialogs(self) -> None:
        """Đóng hộp thoại tạo quy tắc / chọn thư mục đang mở."""
        picker, self._dir_picker = self._dir_picker, None
        if picker is not None and isValid(picker):
            picker.close()
            picker.deleteLater()
        dlg, self._rule_dialog = self._rule_dialog, None
        if dlg is not None and isValid(dlg):
            dlg.close()
            dlg.deleteLater()

    def set_update_state(
        self,
        state: UpdateUiState,
        *,
        info: UpdateInfo | None = None,
        progress: tuple[int, int] | None = None,
    ) -> None:
        state_changed = state != self._update_state
        self._update_state = UpdateUiState(state)
        self._update_info = info
        self._update_progress = progress
        self._update_update_ui()
        if state_changed:
            self._update_tab_order()

    def set_language(self, code: str | None, available: Sequence[str]) -> None:
        self.appearance.set_language(code, available)

    def set_theme(self, value: str | None) -> None:
        self.appearance.set_theme(value)

    def set_accent(self, value: str | None) -> None:
        self.appearance.set_accent(value)

    def set_sound_mode(self, value: str | None) -> None:
        self.appearance.set_sound_mode(value)

    def set_sound_volume(self, value: int | None) -> None:
        self.appearance.set_sound_volume(value)

    def set_auto_dismiss_notices(self, enabled: bool) -> None:
        self.appearance.set_auto_dismiss_notices(enabled)

    def set_tray_notifications(self, enabled: bool) -> None:
        self.appearance.set_tray_notifications(enabled)

    def set_autostart(self, state: autostart.AutostartState | None) -> None:
        """Trạng thái registry (None: đọc lỗi); UNSUPPORTED hay `autostart.supported()` False → ẩn hàng."""
        self._autostart = None if state is None else autostart.AutostartState(state)
        with _blocked(self.autostart_switch):
            self.autostart_switch.setChecked(self._autostart is autostart.AutostartState.ON)
        self._update_autostart()

    def set_rate_limit(self, value: int | None) -> None:
        self._rate_commit.stop()
        self._rate = value
        with _blocked(self.rate_checkbox), _blocked(self.rate_spin):
            self.rate_checkbox.setChecked(value is not None)
            if value is not None:
                self.rate_spin.setValue(bytes_to_rate(value))
            self.rate_spin.setEnabled(value is not None)

    def set_retention(self, days: int | None) -> None:
        self._retention_commit.stop()
        self._retention = days
        with _blocked(self.retention_spin):
            self.retention_spin.setValue(default_retention_days() if days is None else days)
        self._update_retention_suffix()

    def _update_retention_suffix(self) -> None:
        """Chữ đơn vị quanh số của ô ngày theo giá trị đang có (English “1 day” / “2 days”): dịch câu
        đếm “%n ngày” rồi tách phần trước/sau số làm tiền tố/hậu tố của ô."""
        value = self.retention_spin.value()
        full = QCoreApplication.translate("Settings", "%n ngày", "đơn vị của ô số ngày", value)
        before, found, after = full.partition(str(value))
        self.retention_spin.setPrefix(before if found else "")
        self.retention_spin.setSuffix(after if found else "")

    def set_device_settings(self, mapping: Mapping[str, DeviceSettings]) -> None:
        self._device_settings = dict(mapping)
        for device_id, row in self._rows.items():
            row.set_ask(self._ask_of(device_id))
            row.set_clipboard(self._clipboard_of(device_id))

    def set_receive_dir(
        self, path: Path | None, unavailable: Path | None = None, pending: Path | None = None
    ) -> None:
        self._receive_dir, self._unavailable, self._pending = path, unavailable, pending
        self._update_receive_dir()

    def set_change_available(self, available: bool) -> None:
        """Hiện nút “Đổi…” (sau B1b, khi đã nối `ReceiveDirController`)."""
        self.change_dir_button.setVisible(bool(available))
        self._update_tab_order()

    def set_export_running(self, running: bool) -> None:
        self._export_running = bool(running)
        self.export_button.setEnabled(not self._export_running)
        self._update_export_text()

    def show_section(
        self,
        section: Section,
        device_id: str | None = None,
        *,
        focus: Literal["ask", "clipboard"] = "ask",
    ) -> None:
        """Mở mục `section`; `device_id` (mục Thiết bị tin cậy) → tiêu điểm vào ô “Hỏi trước khi nhận”
        của máy đó, hay ô “Đồng bộ clipboard” khi `focus="clipboard"` và trang đã có ảnh chụp."""
        self.nav.setCurrentRow(list(Section).index(Section(section)))
        if device_id is not None:
            row = self._rows.get(device_id)
            if row is not None:
                use_clipboard = focus == "clipboard" and self._loaded
                target = row.clipboard_checkbox if use_clipboard else row.ask_checkbox
                target.setFocus(Qt.FocusReason.OtherFocusReason)
                self._section_widgets[Section.DEVICES].ensureWidgetVisible(row)

    def show_notice(self, level: InfoLevel, text: texts.Text, *, action: texts.Text | None = None) -> None:
        self.info_bar.show_message(InfoLevel(level), text, action=action)

    def clear_notice(self) -> None:
        self.info_bar.clear()

    def retranslate(self) -> None:
        t = QCoreApplication.translate
        title = page_title()
        self.header.set_title(title)
        self.setAccessibleName(title)
        self.nav.setAccessibleName(t("Settings", "Các mục cài đặt"))
        for index, section in enumerate(Section):
            item = self.nav.item(index)
            if item is not None:
                item.setText(section_title(section))
            self._section_titles[section].setText(section_title(section))
            self._section_widgets[section].setAccessibleName(section_title(section))

        self.name_row.set_text(t("Settings", "Tên thiết bị"), self.hint_for_name())
        self.name_edit.setPlaceholderText(t("Settings", "Tên thiết bị"))
        if self._name_error is not None:
            self.set_name_error(self._name_error)
        self.receive_row.set_text(t("Settings", "Thư mục nhận"))
        self.open_dir_button.setText(t("Settings", "Mở"))
        self.open_dir_button.setAccessibleName(t("Settings", "Mở thư mục nhận"))
        self.change_dir_button.setText(t("Settings", "Đổi…"))
        self.change_dir_button.setAccessibleName(t("Settings", "Đổi thư mục nhận"))
        self.create_subfolder_row.set_text(
            t("Settings", "Tạo thư mục con cho mỗi lần nhận"),
            t("Settings", "Tự động gom tệp vào thư mục riêng theo tên máy gửi và thời gian nhận."),
        )
        self.autostart_row.set_text(
            t("Settings", "Khởi động cùng Windows"),
            t("Settings", "Teleport chạy ở khay hệ thống khi bạn đăng nhập Windows, sẵn sàng nhận file."),
        )
        self.visible_row.set_text(
            t("Settings", "Cho máy chưa ghép đôi thấy máy này"),
            t(
                "Settings",
                "Máy đã ghép đôi tự kết nối được khi ít nhất một trong hai máy đang hiện, "
                "hoặc khi hai máy đã từng kết nối và địa chỉ không đổi.",
            ),
        )
        self.stealth_row.set_text(
            t("Settings", "Chế độ ẩn hoàn toàn trong mạng"),
            t(
                "Settings",
                "Tắt phát tín hiệu tự tìm máy trong mạng nội bộ. Máy đã ghép đôi vẫn kết nối được "
                "khi biết địa chỉ.",
            ),
        )
        self.history_row.set_text(
            t("Settings", "Lịch sử truyền file"),
            t(
                "Settings",
                "Teleport chỉ lưu tên file, cỡ và kết quả của các lượt truyền trên máy này, "
                "không lưu nội dung.",
            ),
        )
        self.clear_history_button.setText(t("Settings", "Xóa toàn bộ lịch sử…"))
        intro = t(
            "Settings",
            "Các máy đã ghép đôi với máy này. Bỏ chọn “Hỏi trước khi nhận” để tự nhận file từ máy đó. "
            "Clipboard chỉ đồng bộ khi cả hai máy cùng bật “Đồng bộ clipboard”; mật khẩu chép từ trình "
            "quản lý mật khẩu không được gửi.",
        )
        self.devices_intro.setText(intro)
        self.device_list.setAccessibleName(section_title(Section.DEVICES))
        empty_action = t("Settings", "Tìm máy mới")
        self.devices_empty.set_content(
            "shield_checkmark",
            t("Settings", "Chưa ghép đôi với máy nào"),
            t("Settings", "Ghép đôi để gửi file qua lại với máy khác trong mạng."),
            action=empty_action,
        )
        self.devices_empty.action_button.setAccessibleName(empty_action)
        self.rate_row.set_text(
            t("Settings", "Giới hạn tốc độ gửi"), t("Settings", "Áp dụng cho mọi lượt gửi từ máy này.")
        )
        self.rate_checkbox.setText(t("Settings", "Giới hạn"))
        self.rate_checkbox.setAccessibleName(t("Settings", "Giới hạn tốc độ gửi"))
        self.rate_spin.setSuffix(t("Settings", " MB/s"))
        self.rate_spin.setAccessibleName(t("Settings", "Tốc độ gửi tối đa (MB/s)"))
        retention_hint = t(
            "Settings",
            "Phần nhận dở không còn lượt nào chờ nối lại, để quá số ngày này, được tự xóa khỏi thư mục nhận.",
        )
        self.retention_row.set_text(t("Settings", "Dọn phần nhận dở sau"), retention_hint)
        self._update_retention_suffix()
        self.delta_row.set_text(
            t("Settings", "Truyền thông minh phần thay đổi (Delta)"),
            t(
                "Settings",
                "Chỉ gửi các khối dữ liệu mới hoặc đã sửa khi máy nhận đã có tệp giống hoặc tương tự.",
            ),
        )
        self.delta_switch.setAccessibleName(t("Settings", "Truyền thông minh phần thay đổi (Delta)"))
        self.add_routing_button.setText(t("Settings", "Thêm quy tắc…"))
        self.add_routing_button.setAccessibleName(t("Settings", "Thêm quy tắc phân loại thư mục nhận"))
        self.add_watch_button.setText(t("Settings", "Thêm cổng đẩy…"))
        self.add_watch_button.setAccessibleName(t("Settings", "Thêm cổng đẩy thư mục tự động"))
        # Dấu thập phân của ô tốc độ theo ngôn ngữ giao diện (“2,5” / “2.5”), không theo hệ điều hành.
        self.rate_spin.setLocale(QLocale(i18n.current_language()))
        self.export_row.set_text(
            t("Settings", "Xuất log"),
            t(
                "Settings",
                "Gói các file log của Teleport thành một file zip để gửi kèm khi báo lỗi. Log không chứa "
                "nội dung file hay clipboard, nhưng có thể có đường dẫn thư mục (kèm tên người dùng của "
                "máy), địa chỉ IP và 8 ký tự đầu của mã thiết bị.",
            ),
        )
        self._update_export_text()
        self.version_row.set_text(
            t("Settings", "Phiên bản"),
            texts.fill(
                t("Settings", "Teleport {version} · giao thức {protocol}"),
                version=teleport.__version__,
                protocol=str(PROTOCOL_VERSION),
            ),
        )
        self._update_update_ui()
        self.license_row.set_text(
            t("Settings", "Giấy phép"), t("Settings", "Phần mềm tự do theo GNU GPL phiên bản 3.")
        )
        self.notices_button.setText(t("Settings", "Thông báo bên thứ ba"))
        self.notices_button.setAccessibleName(t("Settings", "Thông báo bên thứ ba"))
        self.verify_receipt_row.set_text(
            t("Settings", "Biên lai truyền tệp"),
            t("Settings", "Xác minh chữ ký số và tính toàn vẹn của tệp biên lai đã lưu."),
        )
        self.verify_receipt_button.setText(t("Settings", "Kiểm tra biên lai…"))
        self.verify_receipt_button.setAccessibleName(t("Settings", "Kiểm tra biên lai"))
        self._update_shortcuts_table()
        self._update_visibility_texts()
        self._update_autostart()
        self._update_receive_dir()
        self._rebuild_pending_receives()
        self._rebuild_routing_rules()
        self._rebuild_watch_portals()
        self._update_tab_order()
        for row in self._rows.values():
            row.retranslate()

    # ---- Bên trong: cập nhật chữ ---------------------------------------------------------------

    def _rebuild_pending_receives(self) -> None:
        t = QCoreApplication.translate
        for w in self._pending_rows:
            self._pending_layout.removeWidget(w)
            w.hide()
            w.deleteLater()
        self._pending_rows.clear()
        self._pending_cancel_buttons.clear()

        count = len(self._pending_receives)
        if count == 0:
            hint = t("Settings", "Không có lượt nhận đang chờ hoặc dở dang.")
            self.cancel_all_pending_button.setVisible(False)
            self.pending_list.setVisible(False)
        else:
            count_text = t("Settings", "%n lượt nhận đang chờ hoặc dở dang", None, count)
            promised_text = texts.fill(
                t("Settings", "Đang giữ chỗ {size}."), size=texts.format_size(self._promised_bytes)
            )
            hint = f"{count_text} · {promised_text}"
            self.cancel_all_pending_button.setVisible(True)
            self.pending_list.setVisible(True)
            for item in self._pending_receives:
                row = QFrame(self.pending_list)
                row.setObjectName("pendingReceiveRow")
                row.setProperty(theme.SURFACE_PROPERTY, "raised")
                row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
                peer = texts.peer_label(item.device_name, item.device_id)
                files_text = t("Settings", "%n file", None, item.files_total)
                head_text = f"{peer} · {files_text}"
                done_text = texts.format_size(item.done_bytes)
                total_text = texts.format_size(item.total_bytes)
                status_text = pending_status_text(item.status)
                detail_text = f"{done_text} / {total_text} · {status_text}"

                left = QVBoxLayout()
                left.setContentsMargins(0, 0, 0, 0)
                left.setSpacing(2)
                head_label = _ElidedLabel(Qt.TextElideMode.ElideRight, row)
                head_label.setObjectName("pendingReceiveTitle")
                font = head_label.font()
                font.setWeight(font.Weight.DemiBold)
                head_label.setFont(font)
                head_label.set_full_text(head_text)
                detail_label = _plain(row, object_name="pendingReceiveDetail")
                detail_label.setProperty(theme.TONE_PROPERTY, "muted")
                detail_label.setText(detail_text)
                left.addWidget(head_label)
                left.addWidget(detail_label)

                cancel_btn = QPushButton(row)
                cancel_btn.setObjectName("cancelPendingButton")
                cancel_btn.setText(t("Settings", "Hủy"))
                cancel_btn.setAccessibleName(texts.fill(t("Settings", "Hủy lượt nhận từ {name}"), name=peer))
                cancel_btn.setAccessibleDescription(f"{head_text} - {detail_text}")
                cancel_btn.clicked.connect(
                    functools.partial(self.cancel_pending_requested.emit, item.device_id, item.transfer_id)
                )

                box = QHBoxLayout(row)
                box.setContentsMargins(theme.SPACING[3], theme.SPACING[1], theme.SPACING[3], theme.SPACING[1])
                box.setSpacing(theme.SPACING[2])
                box.addLayout(left, 1)
                box.addWidget(cancel_btn, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

                self._pending_layout.addWidget(row)
                self._pending_rows.append(row)
                self._pending_cancel_buttons.append(cancel_btn)

        self.pending_row.set_text(t("Settings", "Lượt nhận đang chờ"), hint)
        self.cancel_all_pending_button.setText(t("Settings", "Hủy tất cả"))
        self.cancel_all_pending_button.setAccessibleName(t("Settings", "Hủy tất cả lượt nhận dở"))
        self.cancel_all_pending_button.setAccessibleDescription(hint)

    def _device_label(self, device_id: str) -> str:
        for view in self.model.devices():
            if view.device_id == device_id:
                return texts.peer_label(view.name, device_id)
        return texts.peer_label(self._device_names.get(device_id, ""), device_id)

    def _paired_choices(self) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        for view in trusted_devices(self.model.devices()):
            seen[view.device_id] = texts.peer_label(view.name, view.device_id)
        for device_id in self._device_settings:
            if device_id not in seen:
                seen[device_id] = texts.peer_label(self._device_names.get(device_id, ""), device_id)
        return sorted(seen.items(), key=lambda pair: (pair[1].casefold(), pair[0]))

    def _clear_rule_cards(
        self,
        layout: QVBoxLayout,
        rows: list[QFrame],
        controls: list[QWidget],
    ) -> None:
        for row in rows:
            layout.removeWidget(row)
            row.hide()
            row.setParent(None)
            row.deleteLater()
        rows.clear()
        controls.clear()

    def _add_rule_card(
        self,
        container: QWidget,
        layout: QVBoxLayout,
        rows: list[QFrame],
        controls: list[QWidget],
        *,
        row_name: str,
        title: str,
        detail: str,
        enabled: bool,
        toggle_label: str,
        remove_label: str,
        on_toggle: Callable[[bool], None],
        on_remove: Callable[[], None],
        edit_label: str | None = None,
        on_edit: Callable[[], None] | None = None,
    ) -> None:
        t = QCoreApplication.translate
        row = QFrame(container)
        row.setObjectName(row_name)
        row.setProperty(theme.SURFACE_PROPERTY, "raised")
        row.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(2)
        head = _ElidedLabel(Qt.TextElideMode.ElideRight, row)
        head.setObjectName(f"{row_name}Title")
        font = head.font()
        font.setWeight(font.Weight.DemiBold)
        head.setFont(font)
        head.set_full_text(title)
        info = _plain(row, object_name=f"{row_name}Detail")
        info.setProperty(theme.TONE_PROPERTY, "muted")
        info.setText(detail)
        left.addWidget(head)
        left.addWidget(info)

        edit_btn: QPushButton | None = None
        if on_edit is not None:
            edit_btn = QPushButton(row)
            edit_btn.setObjectName(f"{row_name}Edit")
            edit_btn.setText(t("Settings", "Sửa"))
            if edit_label:
                edit_btn.setAccessibleName(edit_label)
            edit_btn.clicked.connect(on_edit)

        toggle = ToggleSwitch(row)
        toggle.setObjectName(f"{row_name}Toggle")
        toggle.setChecked(enabled)
        toggle.setAccessibleName(toggle_label)
        toggle.clicked.connect(lambda checked: on_toggle(bool(checked)))

        remove_btn = QPushButton(row)
        remove_btn.setObjectName(f"{row_name}Remove")
        remove_btn.setText(t("Settings", "Xóa"))
        remove_btn.setAccessibleName(remove_label)
        remove_btn.clicked.connect(on_remove)

        box = QHBoxLayout(row)
        box.setContentsMargins(theme.SPACING[3], theme.SPACING[1], theme.SPACING[3], theme.SPACING[1])
        box.setSpacing(theme.SPACING[2])
        box.addLayout(left, 1)
        if edit_btn is not None:
            box.addWidget(edit_btn, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        box.addWidget(toggle, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        box.addWidget(remove_btn, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        layout.addWidget(row)
        rows.append(row)
        if edit_btn is not None:
            controls.append(edit_btn)
        controls.extend((toggle, remove_btn))

    def _rebuild_routing_rules(self) -> None:
        t = QCoreApplication.translate
        self._clear_rule_cards(self._routing_layout, self._routing_rows, self._routing_controls)
        count = len(self._routing_rules)
        if count == 0:
            hint = t(
                "Settings",
                "Chưa có quy tắc nào. File nhận về có thể được tự động đưa vào thư mục riêng theo tên "
                "file hoặc máy gửi.",
            )
            self.routing_list.setVisible(False)
        else:
            hint = t("Settings", "%n quy tắc phân loại thư mục nhận đang cấu hình.", None, count)
            self.routing_list.setVisible(True)
            for idx, rule in enumerate(self._routing_rules):
                peer = (
                    self._device_label(rule.device_id)
                    if rule.device_id is not None
                    else t("Settings", "Mọi máy")
                )
                pats = ", ".join(rule.patterns) if rule.patterns else t("Settings", "Mọi file")
                detail = f"{rule.destination} · {peer} · {pats}"
                self._add_rule_card(
                    self.routing_list,
                    self._routing_layout,
                    self._routing_rows,
                    self._routing_controls,
                    row_name="routingRuleRow",
                    title=rule.name,
                    detail=detail,
                    enabled=rule.enabled,
                    toggle_label=texts.fill(t("Settings", "Bật hoặc tắt quy tắc {name}"), name=rule.name),
                    remove_label=texts.fill(t("Settings", "Xóa quy tắc {name}"), name=rule.name),
                    on_toggle=functools.partial(self._toggle_routing_rule, idx),
                    on_remove=functools.partial(self._remove_routing_rule, idx),
                    edit_label=texts.fill(t("Settings", "Sửa quy tắc {name}"), name=rule.name),
                    on_edit=functools.partial(self._open_edit_routing_dialog, idx),
                )
        self.routing_row.set_text(t("Settings", "Tự phân loại thư mục nhận"), hint)

    def _rebuild_watch_portals(self) -> None:
        t = QCoreApplication.translate
        self._clear_rule_cards(self._watch_layout, self._watch_rows, self._watch_controls)
        count = len(self._watch_portals)
        self.watch_pause_button.setVisible(count > 0)
        pause_text = (
            t("Settings", "Tiếp tục cổng đẩy")
            if self._watch_portals_paused
            else t("Settings", "Tạm dừng tất cả")
        )
        self.watch_pause_button.setText(pause_text)
        self.watch_pause_button.setAccessibleName(pause_text)
        with _blocked(self.watch_pause_button):
            self.watch_pause_button.setChecked(self._watch_portals_paused)
        if count == 0:
            hint = t(
                "Settings",
                "Chưa có cổng đẩy nào. Khi có file mới đặt vào thư mục theo dõi, Teleport tự gửi "
                "sang máy đã chọn.",
            )
            self.watch_list.setVisible(False)
        else:
            base_hint = t("Settings", "%n cổng đẩy tự động đang cấu hình.", None, count)
            hint = (
                f"{base_hint} · {t('Settings', 'Đang tạm dừng mọi cổng đẩy tự động.')}"
                if self._watch_portals_paused
                else base_hint
            )
            self.watch_list.setVisible(True)
            for idx, portal in enumerate(self._watch_portals):
                peer = self._device_label(portal.device_id)
                pats = ", ".join(portal.patterns) if portal.patterns else t("Settings", "Mọi file")
                mode = (
                    t("Settings", "gồm thư mục con") if portal.recursive else t("Settings", "chỉ cấp ngoài")
                )
                detail = f"{portal.source_dir} → {peer} · {pats} · {mode}"
                self._add_rule_card(
                    self.watch_list,
                    self._watch_layout,
                    self._watch_rows,
                    self._watch_controls,
                    row_name="watchPortalRow",
                    title=portal.name,
                    detail=detail,
                    enabled=portal.enabled,
                    toggle_label=texts.fill(t("Settings", "Bật hoặc tắt cổng đẩy {name}"), name=portal.name),
                    remove_label=texts.fill(t("Settings", "Xóa cổng đẩy {name}"), name=portal.name),
                    on_toggle=functools.partial(self._toggle_watch_portal, idx),
                    on_remove=functools.partial(self._remove_watch_portal, idx),
                    edit_label=texts.fill(t("Settings", "Sửa cổng đẩy {name}"), name=portal.name),
                    on_edit=functools.partial(self._open_edit_watch_dialog, idx),
                )
        self.watch_row.set_text(t("Settings", "Cổng đẩy thư mục tự động"), hint)

    def _update_update_ui(self) -> None:
        t = QCoreApplication.translate
        hint = t("Settings", "Chỉ kiểm tra khi bạn bấm nút. Ứng dụng không tự kết nối mạng.")
        self.update_row.set_text(t("Settings", "Cập nhật"), hint)

        state = self._update_state
        info = self._update_info
        progress = self._update_progress
        tone = "muted"
        status_text = ""

        self.check_update_button.setAccessibleName(t("Settings", "Kiểm tra cập nhật"))
        self.check_update_button.setAccessibleDescription(hint)
        self.download_update_button.setAccessibleName(t("Settings", "Tải bản cập nhật"))
        self.download_update_button.setAccessibleDescription(hint)

        if state is UpdateUiState.CHECKING:
            self.check_update_button.setEnabled(False)
            self.check_update_button.setText(t("Settings", "Đang kiểm tra…"))
            self.download_update_button.setVisible(False)
        elif state is UpdateUiState.DOWNLOADING:
            self.check_update_button.setEnabled(False)
            self.check_update_button.setText(t("Settings", "Kiểm tra lại"))
            self.download_update_button.setVisible(True)
            self.download_update_button.setEnabled(False)
            self.download_update_button.setText(t("Settings", "Đang tải…"))
            if progress is not None and progress[1] > 0:
                status_text = texts.fill(
                    t("Settings", "Đang tải bản cập nhật ({done} / {total})…"),
                    done=texts.format_size(progress[0]),
                    total=texts.format_size(progress[1]),
                )
            else:
                status_text = t("Settings", "Đang tải bản cập nhật…")
        elif state is UpdateUiState.UP_TO_DATE:
            self.check_update_button.setEnabled(True)
            self.check_update_button.setText(t("Settings", "Kiểm tra lại"))
            self.download_update_button.setVisible(False)
            status_text = t("Settings", "Bạn đang dùng phiên bản mới nhất.")
        elif state is UpdateUiState.AVAILABLE:
            self.check_update_button.setEnabled(True)
            self.check_update_button.setText(t("Settings", "Kiểm tra lại"))
            self.download_update_button.setVisible(True)
            self.download_update_button.setEnabled(True)
            self.download_update_button.setText(t("Settings", "Tải về"))
            status_text = (
                texts.fill(t("Settings", "Đã có bản {version}."), version=info.version)
                if info is not None
                else t("Settings", "Đã có bản cập nhật mới.")
            )
        elif state is UpdateUiState.READY:
            self.check_update_button.setEnabled(True)
            self.check_update_button.setText(t("Settings", "Kiểm tra lại"))
            self.download_update_button.setVisible(False)
            status_text = t("Settings", "Đã tải xong bộ cài cập nhật.")
        elif state is UpdateUiState.ERROR:
            self.check_update_button.setEnabled(True)
            self.check_update_button.setText(t("Settings", "Thử lại"))
            self.download_update_button.setVisible(False)
            status_text = t("Settings", "Không thể kiểm tra hoặc tải bản cập nhật. Hãy thử lại sau.")
            tone = "warning"
        else:
            self.check_update_button.setEnabled(True)
            self.check_update_button.setText(t("Settings", "Kiểm tra"))
            self.download_update_button.setVisible(False)

        self.update_status_label.setText(status_text)
        self.update_status_label.setVisible(bool(status_text))
        if self.update_status_label.property(theme.TONE_PROPERTY) != tone:
            self.update_status_label.setProperty(theme.TONE_PROPERTY, tone)
            _repolish(self.update_status_label)

    def _update_visibility_texts(self) -> None:
        self.name_row.set_text(QCoreApplication.translate("Settings", "Tên thiết bị"), self.hint_for_name())
        if self._name_error is not None:
            self.set_name_error(self._name_error)
        hidden = hidden_text()
        self.hidden_label.setText(hidden)
        self.hidden_label.setVisible(not self._visible)
        base = QCoreApplication.translate(
            "Settings",
            "Máy đã ghép đôi tự kết nối được khi ít nhất một trong hai máy đang hiện, "
            "hoặc khi hai máy đã từng kết nối và địa chỉ không đổi.",
        )
        self.visible_switch.setAccessibleDescription(base if self._visible else f"{base} {hidden}")

    def _update_autostart(self) -> None:
        state = self._autostart
        try:
            supported = autostart.supported()
        except Exception:
            supported = False
        shown = supported and state is not autostart.AutostartState.UNSUPPORTED
        self.autostart_row.setVisible(shown)
        extra = autostart_state_hint(state)
        self.autostart_state_label.setText(extra)
        self.autostart_state_label.setVisible(bool(extra))
        base = QCoreApplication.translate(
            "Settings", "Teleport chạy ở khay hệ thống khi bạn đăng nhập Windows, sẵn sàng nhận file."
        )
        self.autostart_switch.setAccessibleDescription(f"{base} {extra}" if extra else base)

    def _update_receive_dir(self) -> None:
        t = QCoreApplication.translate
        label = self.receive_dir_label
        tone = "muted"
        if self._pending is not None:
            text = texts.fill(t("Settings", "Đang đổi sang {path}…"), path=printable(str(self._pending)))
        elif self._receive_dir is not None:
            text = printable(str(self._receive_dir))
        elif self._unavailable is not None:
            text = texts.fill(
                t("Settings", "Không nhận file: không mở được {path}"), path=printable(str(self._unavailable))
            )
            tone = "warning"
        else:
            text = ""
        label.set_full_text(text)
        label.setToolTip(safe_tooltip(text) if text else "")
        label.setAccessibleName(t("Settings", "Thư mục nhận"))
        label.setAccessibleDescription(text)
        label.setVisible(bool(text))
        if label.property(theme.TONE_PROPERTY) != tone:
            label.setProperty(theme.TONE_PROPERTY, tone)
            _repolish(label)
        self.open_dir_button.setVisible(self._receive_dir is not None)
        self.open_dir_button.setAccessibleDescription(text)
        self.change_dir_button.setAccessibleDescription(text)

    def _update_shortcuts_table(self) -> None:
        t = QCoreApplication.translate
        label = t("Settings", "Phím tắt")
        hint = t("Settings", "Dùng Teleport không cần chuột. Danh sách chỉ để xem, không đổi được phím.")
        self.shortcuts_row.set_text(label, hint)
        table = self.shortcuts_table
        table.setAccessibleName(label)
        table.setAccessibleDescription(hint)
        table.set_rows(shortcuts.table_rows(), (t("Settings", "Thao tác"), t("Settings", "Phím")))

    def _update_export_text(self) -> None:
        t = QCoreApplication.translate
        text = t("Settings", "Đang xuất…") if self._export_running else t("Settings", "Xuất log…")
        self.export_button.setText(text)
        self.export_button.setAccessibleName(t("Settings", "Xuất log"))

    # ---- Bên trong: thiết bị -------------------------------------------------------------------

    def _sync_devices(self) -> None:
        if "_rows" not in self.__dict__:  # đang hủy
            return
        views = trusted_devices(self.model.devices())
        new_order = [v.device_id for v in views]
        focus = QApplication.focusWidget()
        focused_id = next(
            (i for i, row in self._rows.items() if focus is not None and row.isAncestorOf(focus)), None
        )
        removed = [i for i in self._order if i not in new_order]
        move_focus = focused_id is not None and focused_id in removed
        target_id: str | None = None  # hàng nhận tiêu điểm; None (khi `move_focus`) = nút màn rỗng
        if focused_id is not None and move_focus:
            old = self._order
            position = old.index(focused_id)
            after = [i for i in old[position + 1 :] if i in new_order]
            before = [i for i in old[:position] if i in new_order]
            target_id = after[0] if after else (before[-1] if before else None)
        for device_id in removed:
            row = self._rows.pop(device_id)
            self._device_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        for view in views:
            existing = self._rows.get(view.device_id)
            if existing is None:
                created = TrustedDeviceRow(view, self.device_list)
                created.ask_toggled.connect(self.ask_toggled)
                created.clipboard_toggled.connect(self.clipboard_toggled)
                created.unpair_requested.connect(self.unpair_requested)
                created.set_ask(self._ask_of(view.device_id))
                created.set_clipboard(self._clipboard_of(view.device_id))
                self._rows[view.device_id] = created
            elif existing.view != view:
                existing.set_view(view)
        for index, device_id in enumerate(new_order):
            row = self._rows[device_id]
            if self._device_layout.indexOf(row) != index:
                self._device_layout.removeWidget(row)
                self._device_layout.insertWidget(index, row)
            row.show()
        self._order = new_order
        empty = not new_order
        self.device_list.setVisible(not empty)
        self.devices_intro.setVisible(not empty)
        self.devices_empty.setVisible(empty)
        self._update_tab_order()
        if move_focus:
            widget: QWidget = (
                self._rows[target_id].ask_checkbox
                if target_id is not None
                else self.devices_empty.focus_target()
            )
            widget.setFocus(Qt.FocusReason.OtherFocusReason)

    # ---- Bên trong: thứ tự Tab -----------------------------------------------------------------

    def _update_tab_order(self) -> None:
        bar = self.info_bar
        chain: list[QWidget] = [
            self.header.back_button,
            self.nav,
            bar.title_label,
            bar.text_label,
            bar.action_button,
            bar.close_button,
            self.name_edit,
            self.open_dir_button,
            self.change_dir_button,
            self.create_subfolder_switch,
            self.autostart_switch,
            *self.appearance.focus_widgets(),
            self.visible_switch,
            self.stealth_switch,
            self.clear_history_button,
        ]
        for row in self.device_rows():
            chain.extend(row.focus_widgets())
        chain += [
            self.devices_empty.action_button,
            self.rate_checkbox,
            self.rate_spin,
            self.retention_spin,
            self.cancel_all_pending_button,
            *self._pending_cancel_buttons,
            self.delta_switch,
            self.add_routing_button,
            *self._routing_controls,
            self.watch_pause_button,
            self.add_watch_button,
            *self._watch_controls,
            self.export_button,
            self.shortcuts_table,
            self.check_update_button,
            self.download_update_button,
            self.notices_button,
            self.verify_receipt_button,
        ]
        for first, second in itertools.pairwise(chain):
            QWidget.setTabOrder(first, second)

    # ---- Bên trong: slot -----------------------------------------------------------------------

    def _on_nav_row(self, row: int) -> None:
        if 0 <= row < self.sections.count():
            self.sections.setCurrentIndex(row)

    def _on_name_finished(self) -> None:
        text = self.name_edit.text().strip()
        if text == self._name:
            if self.name_edit.text() != text:
                self.name_edit.setText(text)
            self.set_name_error(None)
            return
        if not text:
            self.set_name_error(name_empty_text())
            return
        try:
            validate_device_name(text)
        except (MessageError, ValueError, TypeError):
            self.set_name_error(name_invalid_text())
            return
        self.set_name_error(None)
        self.name_edit.saved = text  # Esc lúc đang lưu không trả về tên cũ
        self._name = text
        self.name_edit.setText(text)
        self.name_submitted.emit(text)

    def _on_visible_clicked(self, checked: bool) -> None:
        self._visible = bool(checked)
        self._update_visibility_texts()
        self.visible_toggled.emit(self._visible)

    def _on_stealth_clicked(self, checked: bool) -> None:
        self._stealth = bool(checked)
        self.stealth_toggled.emit(self._stealth)

    def _on_delta_clicked(self, checked: bool) -> None:
        self._delta_sync = bool(checked)
        self.delta_sync_toggled.emit(self._delta_sync)

    def _toggle_routing_rule(self, index: int, enabled: bool) -> None:
        if not (0 <= index < len(self._routing_rules)):
            return
        updated = list(self._routing_rules)
        updated[index] = replace(updated[index], enabled=bool(enabled))
        self.set_routing_rules(updated)
        self.routing_rules_changed.emit(self._routing_rules)

    def _remove_routing_rule(self, index: int) -> None:
        if not (0 <= index < len(self._routing_rules)):
            return
        updated = [rule for idx, rule in enumerate(self._routing_rules) if idx != index]
        self.set_routing_rules(updated)
        self.routing_rules_changed.emit(self._routing_rules)

    def _toggle_watch_portal(self, index: int, enabled: bool) -> None:
        if not (0 <= index < len(self._watch_portals)):
            return
        updated = list(self._watch_portals)
        updated[index] = replace(updated[index], enabled=bool(enabled))
        self.set_watch_portals(updated)
        self.watch_portals_changed.emit(self._watch_portals)

    def _remove_watch_portal(self, index: int) -> None:
        if not (0 <= index < len(self._watch_portals)):
            return
        updated = [portal for idx, portal in enumerate(self._watch_portals) if idx != index]
        self.set_watch_portals(updated)
        self.watch_portals_changed.emit(self._watch_portals)

    def _open_dir_picker(self, parent: QDialog, target_edit: QLineEdit, title: str) -> None:
        old, self._dir_picker = self._dir_picker, None
        if old is not None and isValid(old):
            old.close()
            old.deleteLater()
        picker = QFileDialog(parent, title)
        picker.setFileMode(QFileDialog.FileMode.Directory)
        picker.setOption(QFileDialog.Option.ShowDirsOnly, True)
        current = target_edit.text().strip()
        if current and Path(current).is_dir():
            picker.setDirectory(current)
        picker.fileSelected.connect(target_edit.setText)

        def cleanup(_result: int) -> None:
            if self._dir_picker is picker:
                self._dir_picker = None
            if isValid(picker):
                picker.deleteLater()

        picker.finished.connect(cleanup)
        self._dir_picker = picker
        picker.open()

    def _on_watch_pause_clicked(self, checked: bool) -> None:
        self.set_watch_portals_paused(bool(checked))
        self.watch_portals_paused_toggled.emit(self._watch_portals_paused)

    def _open_add_routing_dialog(self) -> None:
        self._open_rule_dialog(is_watch=False)

    def _open_edit_routing_dialog(self, index: int) -> None:
        if 0 <= index < len(self._routing_rules):
            self._open_rule_dialog(is_watch=False, edit_index=index)

    def _open_add_watch_dialog(self) -> None:
        t = QCoreApplication.translate
        if not self._paired_choices():
            self.show_notice(
                InfoLevel.INFO,
                t("Settings", "Hãy ghép đôi với ít nhất một máy trước khi tạo cổng đẩy tự động."),
            )
            return
        self._open_rule_dialog(is_watch=True)

    def _open_edit_watch_dialog(self, index: int) -> None:
        if 0 <= index < len(self._watch_portals):
            self._open_rule_dialog(is_watch=True, edit_index=index)

    def _open_rule_dialog(self, *, is_watch: bool, edit_index: int | None = None) -> None:
        t = QCoreApplication.translate
        self.close_dialogs()
        existing_watch = (
            self._watch_portals[edit_index]
            if is_watch and edit_index is not None and 0 <= edit_index < len(self._watch_portals)
            else None
        )
        existing_routing = (
            self._routing_rules[edit_index]
            if not is_watch and edit_index is not None and 0 <= edit_index < len(self._routing_rules)
            else None
        )
        is_edit = existing_watch is not None or existing_routing is not None

        dlg = QDialog(self)
        dlg.setObjectName("addWatchPortalDialog" if is_watch else "addRoutingRuleDialog")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        if is_edit:
            dlg.setWindowTitle(
                t("Settings", "Sửa cổng đẩy tự động")
                if is_watch
                else t("Settings", "Sửa quy tắc phân loại thư mục nhận")
            )
        else:
            dlg.setWindowTitle(
                t("Settings", "Thêm cổng đẩy tự động")
                if is_watch
                else t("Settings", "Thêm quy tắc phân loại thư mục nhận")
            )
        form = QFormLayout(dlg)
        form.setContentsMargins(theme.SPACING[3], theme.SPACING[3], theme.SPACING[3], theme.SPACING[3])
        form.setSpacing(theme.SPACING[2])

        name_edit = QLineEdit(dlg)
        name_edit.setObjectName("ruleNameEdit")
        name_edit.setPlaceholderText(t("Settings", "Ví dụ: Ảnh chụp, Tài liệu công việc"))
        name_edit.setAccessibleName(t("Settings", "Tên gợi nhớ"))
        form.addRow(t("Settings", "Tên gợi nhớ"), name_edit)

        dir_wrap = QWidget(dlg)
        dir_layout = QHBoxLayout(dir_wrap)
        dir_layout.setContentsMargins(0, 0, 0, 0)
        dir_layout.setSpacing(theme.SPACING[1])
        dir_edit = QLineEdit(dir_wrap)
        dir_edit.setObjectName("rulePathEdit")
        dir_title = t("Settings", "Chọn thư mục theo dõi") if is_watch else t("Settings", "Chọn thư mục đích")
        dir_edit.setAccessibleName(dir_title)
        browse_btn = QPushButton(t("Settings", "Chọn…"), dir_wrap)
        browse_btn.setObjectName("ruleBrowseButton")
        browse_btn.setAccessibleName(dir_title)
        browse_btn.clicked.connect(lambda: self._open_dir_picker(dlg, dir_edit, dir_title))
        dir_layout.addWidget(dir_edit, 1)
        dir_layout.addWidget(browse_btn, 0)
        form.addRow(
            t("Settings", "Thư mục theo dõi") if is_watch else t("Settings", "Thư mục đích"),
            dir_wrap,
        )

        device_combo = QComboBox(dlg)
        device_combo.setObjectName("ruleDeviceCombo")
        device_label = t("Settings", "Máy nhận") if is_watch else t("Settings", "Áp dụng cho máy gửi")
        device_combo.setAccessibleName(device_label)
        if not is_watch:
            device_combo.addItem(t("Settings", "Mọi máy đã ghép đôi"), None)
        for dev_id, label in self._paired_choices():
            device_combo.addItem(label, dev_id)
        form.addRow(device_label, device_combo)

        patterns_edit = QLineEdit(dlg)
        patterns_edit.setObjectName("rulePatternsEdit")
        patterns_edit.setPlaceholderText(t("Settings", "Để trống = mọi file; ví dụ: *.jpg, *.png"))
        patterns_edit.setAccessibleName(t("Settings", "Mẫu tên file"))
        form.addRow(t("Settings", "Mẫu tên file"), patterns_edit)

        presets_wrap = QWidget(dlg)
        presets_wrap.setObjectName("rulePresetsRow")
        presets_grid = QGridLayout(presets_wrap)
        presets_grid.setContentsMargins(0, 0, 0, 0)
        presets_grid.setSpacing(theme.SPACING[1])

        def apply_preset(preset_patterns: tuple[str, ...]) -> None:
            if not preset_patterns:
                patterns_edit.clear()
                return
            try:
                current = list(parse_patterns(patterns_edit.text()))
            except ValueError:
                current = [part.strip() for part in patterns_edit.text().split(",") if part.strip()]
            lower_current = {p.casefold() for p in current}
            if all(p.casefold() in lower_current for p in preset_patterns):
                remove_set = {p.casefold() for p in preset_patterns}
                updated = [p for p in current if p.casefold() not in remove_set]
            else:
                updated = list(current)
                for p in preset_patterns:
                    if p.casefold() not in lower_current:
                        updated.append(p)
                        lower_current.add(p.casefold())
            patterns_edit.setText(", ".join(updated))

        for pos, (key, preset_patterns) in enumerate(FILE_TYPE_PRESETS):
            chip = QPushButton(_preset_label(key), presets_wrap)
            chip.setObjectName(f"rulePreset_{key}")
            chip.clicked.connect(functools.partial(apply_preset, preset_patterns))
            presets_grid.addWidget(chip, pos // 3, pos % 3)
        form.addRow(t("Settings", "Chọn nhanh loại file"), presets_wrap)

        recursive_cb: QCheckBox | None = None
        if is_watch:
            recursive_cb = QCheckBox(t("Settings", "Theo dõi cả thư mục con bên trong"), dlg)
            recursive_cb.setObjectName("ruleRecursiveCheckbox")
            recursive_cb.setAccessibleName(t("Settings", "Theo dõi cả thư mục con bên trong"))
            form.addRow("", recursive_cb)

        if existing_watch is not None:
            name_edit.setText(existing_watch.name)
            dir_edit.setText(existing_watch.source_dir)
            dev_idx = device_combo.findData(existing_watch.device_id)
            if dev_idx < 0:
                device_combo.addItem(self._device_label(existing_watch.device_id), existing_watch.device_id)
                dev_idx = device_combo.count() - 1
            device_combo.setCurrentIndex(dev_idx)
            patterns_edit.setText(", ".join(existing_watch.patterns))
            if recursive_cb is not None:
                recursive_cb.setChecked(existing_watch.recursive)
        elif existing_routing is not None:
            name_edit.setText(existing_routing.name)
            dir_edit.setText(existing_routing.destination)
            dev_idx = device_combo.findData(existing_routing.device_id)
            if dev_idx < 0 and existing_routing.device_id is not None:
                device_combo.addItem(
                    self._device_label(existing_routing.device_id), existing_routing.device_id
                )
                dev_idx = device_combo.count() - 1
            if dev_idx >= 0:
                device_combo.setCurrentIndex(dev_idx)
            patterns_edit.setText(", ".join(existing_routing.patterns))

        error_label = _plain(dlg, object_name="ruleDialogError")
        error_label.setProperty(theme.TONE_PROPERTY, "danger")
        error_label.setVisible(False)
        form.addRow("", error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dlg
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        ok_text = t("Settings", "Lưu") if is_edit else t("Settings", "Thêm")
        if ok_btn is not None:
            ok_btn.setText(ok_text)
            ok_btn.setAccessibleName(ok_text)
        if cancel_btn is not None:
            cancel_btn.setText(t("Settings", "Hủy"))
            cancel_btn.setAccessibleName(t("Settings", "Hủy"))
        form.addRow(buttons)

        def accept_rule() -> None:
            raw_name = name_edit.text().strip()
            raw_dir = dir_edit.text().strip()
            if not raw_name:
                error_label.setText(t("Settings", "Vui lòng nhập tên gợi nhớ."))
                error_label.setVisible(True)
                return
            if not raw_dir or not Path(raw_dir).is_dir():
                error_label.setText(t("Settings", "Vui lòng chọn một thư mục đang tồn tại."))
                error_label.setVisible(True)
                return
            try:
                patterns = parse_patterns(patterns_edit.text())
                chosen_device = device_combo.currentData()
                if is_watch:
                    if not isinstance(chosen_device, str) or not chosen_device:
                        error_label.setText(t("Settings", "Vui lòng chọn máy nhận."))
                        error_label.setVisible(True)
                        return
                    portal = WatchPortalRule(
                        id=existing_watch.id if existing_watch is not None else uuid4().hex,
                        name=raw_name,
                        source_dir=raw_dir,
                        device_id=chosen_device,
                        patterns=patterns,
                        recursive=bool(recursive_cb is not None and recursive_cb.isChecked()),
                        enabled=existing_watch.enabled if existing_watch is not None else True,
                    )
                    if existing_watch is not None and edit_index is not None:
                        updated_list = list(self._watch_portals)
                        updated_list[edit_index] = portal
                        updated_portals = tuple(updated_list)
                    else:
                        updated_portals = (*self._watch_portals, portal)
                    self.set_watch_portals(updated_portals)
                    self.watch_portals_changed.emit(self._watch_portals)
                else:
                    rule = RoutingRule(
                        id=existing_routing.id if existing_routing is not None else uuid4().hex,
                        name=raw_name,
                        destination=raw_dir,
                        device_id=chosen_device if isinstance(chosen_device, str) else None,
                        patterns=patterns,
                        enabled=existing_routing.enabled if existing_routing is not None else True,
                    )
                    if existing_routing is not None and edit_index is not None:
                        updated_r_list = list(self._routing_rules)
                        updated_r_list[edit_index] = rule
                        updated_rules = tuple(updated_r_list)
                    else:
                        updated_rules = (*self._routing_rules, rule)
                    self.set_routing_rules(updated_rules)
                    self.routing_rules_changed.emit(self._routing_rules)
            except ValueError as err:
                error_label.setText(str(err))
                error_label.setVisible(True)
                return
            dlg.accept()

        def finish(_result: int) -> None:
            if self._rule_dialog is dlg:
                self._rule_dialog = None
            if isValid(dlg):
                dlg.deleteLater()

        buttons.accepted.connect(accept_rule)
        buttons.rejected.connect(dlg.reject)
        dlg.finished.connect(finish)
        self._rule_dialog = dlg
        dlg.open()

    def _on_rate_checkbox(self, checked: bool) -> None:
        self.rate_spin.setEnabled(bool(checked))
        self._commit_rate()

    def _current_rate(self) -> int | None:
        if not self.rate_checkbox.isChecked():
            return None
        return rate_to_bytes(self.rate_spin.value())

    def _commit_rate(self) -> None:
        self._rate_commit.stop()
        if (
            self._rate is not None
            and self.rate_checkbox.isChecked()
            and self.rate_spin.value() == bytes_to_rate(self._rate)
        ):
            return  # ô vẫn hiện đúng giá trị đã lưu (có thể lệch lưới 0,1 MB/s): không đổi gì
        value = self._current_rate()
        if value == self._rate:
            return
        self._rate = value
        self.rate_limit_changed.emit(value)

    def _commit_retention(self) -> None:
        self._retention_commit.stop()
        days = self.retention_spin.value()
        shown = default_retention_days() if self._retention is None else self._retention
        if days == shown:
            return  # ô vẫn hiện đúng giá trị đã lưu (kể cả số bằng mặc định lưu tường minh)
        value = None if days == default_retention_days() else days
        if value == self._retention:
            return
        self._retention = value
        self.retention_changed.emit(value)

    def _on_open_dir(self) -> None:
        if self._receive_dir is not None:
            self.open_receive_dir_requested.emit(self._receive_dir)

    def _on_notices(self) -> None:
        if self._notices is not None:
            self.open_notices_requested.emit(self._notices)

    # ---- Bên trong: giao diện ------------------------------------------------------------------

    def _restyle(self) -> None:
        """Màu của cột mục và icon theo token (đổi lại khi đổi sáng/tối)."""
        t = theme.tokens_for(self)
        dark = theme.is_dark_palette(QApplication.palette())
        ring = theme.focus_ring_color(dark).name()
        self.nav.setStyleSheet(
            f"QListWidget#settingsNav {{ background: {t.sidebar.name()}; border: none;"
            f" border-right: 1px solid {t.border.name()}; padding: 12px 10px; outline: 0; }}"
            f" QListWidget#settingsNav::item {{ color: {t.text.name()};"
            f" border-radius: {theme.RADIUS_CARD}px;"
            # Lề trái thêm 2 px bù cho vạch 4 px cũ: chữ và icon không dời. Vạch nhấn do `_NavList` vẽ.
            f" padding: 0px 12px 0px 14px;"
            " border: 2px solid transparent; }"
            f" QListWidget#settingsNav::item:hover {{ background: {t.selected.name()}; }}"
            f" QListWidget#settingsNav::item:selected {{ background: {t.selected.name()};"
            f" color: {t.text.name()}; }}"
            # Tiêu điểm bàn phím: vòng đều 2 px quanh mục đang chọn (vạch nhấn vẽ đè lên).
            f" QListWidget#settingsNav::item:selected:focus {{ border-color: {ring}; }}"
        )
        for index, section in enumerate(Section):
            item = self.nav.item(index)
            if item is not None:
                item.setIcon(icons.icon(SECTION_ICONS[section], _NAV_ICON, t.text, t.text_muted))

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        if not event.spontaneous():  # đổi trang, xuống khay: câu báo cũ không còn đúng
            self.info_bar.clear()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        elif event.type() in _PALETTE_EVENTS and "nav" in self.__dict__:
            self._restyle()
