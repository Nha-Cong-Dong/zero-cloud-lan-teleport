"""Bộ điều phối `SettingsController` cho trang Cài đặt (`teleport.ui.settings`)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QWidget
from shiboken6 import isValid

from teleport.app import autostart, receipts, updater
from teleport.app.node import Node
from teleport.app.settings import DeviceSettings, SettingsSaveError
from teleport.app.updater import UpdateInfo
from teleport.core.messages import MessageError
from teleport.core.transfer import Direction, TransferKey
from teleport.ui import i18n, motion, sounds, texts, theme, widgets
from teleport.ui import tray as ui_tray
from teleport.ui.settings_widgets import (
    EXPORT_THREAD,
    UPDATE_THREAD,
    SettingsSnapshot,
    UpdateUiState,
    _read_device_settings,
    autostart_write_error_text,
    cancel_all_pending_question,
    cancel_all_pending_title,
    cancel_pending_question,
    cancel_pending_title,
    clear_history_question,
    clear_history_title,
    default_export_dir,
    default_export_name,
    export_other_error_text,
    export_saved_text,
    export_write_error_text,
    history_clear_failed_text,
    history_cleared_text,
    install_update_question,
    install_update_title,
    name_invalid_text,
    other_error_text,
    pending_all_cancelled_text,
    pending_cancel_failed_text,
    pending_cancelled_text,
    settings_error_text,
    snapshot_error_text,
    update_launch_failed_text,
)
from teleport.ui.widgets import InfoLevel

if TYPE_CHECKING:
    from teleport.ui.settings import SettingsPage

log = logging.getLogger(__name__)

SUPPORT_URL = "https://github.com/sponsors/tungkernel"


class _ExportRelay(QObject):
    """Mang kết quả xuất log từ thread xuất về thread Qt (signal `QueuedConnection`)."""

    finished = Signal(object)  # (generation, dest: Path, result: Path | BaseException)


def _deliver_export(
    relay: _ExportRelay, generation: int, dest: Path, future: concurrent.futures.Future[Path]
) -> None:
    """Thread xuất (hay Qt nếu đã xong): đọc kết quả rồi phát về thread Qt; app đang tắt thì bỏ."""
    try:
        result: Path | BaseException = future.result()
    except BaseException as e:  # CancelledError khi `shutdown(cancel_futures=True)`
        result = e
    with contextlib.suppress(RuntimeError):  # QObject đã bị xóa
        relay.finished.emit((generation, dest, result))


class _UpdateRelay(QObject):
    """Mang kết quả kiểm tra và tải bản cập nhật từ thread nền về thread Qt (`QueuedConnection`)."""

    check_finished = Signal(object)  # (generation, result: UpdateInfo | None | BaseException)
    download_progress = Signal(object)  # (generation, done: int, total: int)
    download_finished = Signal(object)  # (generation, info: UpdateInfo, result: Path | BaseException)


def _deliver_check(
    relay: _UpdateRelay, generation: int, future: concurrent.futures.Future[UpdateInfo | None]
) -> None:
    try:
        result: UpdateInfo | BaseException | None = future.result()
    except BaseException as e:
        result = e
    with contextlib.suppress(RuntimeError):
        relay.check_finished.emit((generation, result))


def _download_worker(
    relay: _UpdateRelay,
    generation: int,
    info: UpdateInfo,
    dl_fn: Callable[..., Path],
) -> Path:
    last_emit = 0.0

    def progress(done: int, total: int) -> None:
        nonlocal last_emit
        now = time.monotonic()
        if total <= 0 or done >= total or (now - last_emit) >= 0.08:
            last_emit = now
            with contextlib.suppress(RuntimeError):
                relay.download_progress.emit((generation, int(done), int(total)))

    path = dl_fn(info, progress_cb=progress)
    if info.expected_sha256 and not updater.verify_file_sha256(path, info.expected_sha256):
        with contextlib.suppress(OSError):
            path.unlink()
        raise ValueError("checksum mismatch")
    return path


def _deliver_download(
    relay: _UpdateRelay,
    generation: int,
    info: UpdateInfo,
    future: concurrent.futures.Future[Path],
) -> None:
    try:
        result: Path | BaseException = future.result()
    except BaseException as e:
        result = e
    with contextlib.suppress(RuntimeError):
        relay.download_finished.emit((generation, info, result))


class SettingsController(QObject):
    """Nối `SettingsPage` với node, registry tự khởi động, hộp thoại và việc xuất log."""

    name_changed = Signal(str)
    visibility_changed = Signal(bool)
    history_cleared = Signal(object)  # None = toàn bộ (spec D2 mục 9.9)
    device_settings_changed = Signal(dict)  # dict[str, DeviceSettings]

    def __init__(
        self,
        page: SettingsPage,
        config_dir: Path,
        parent: QObject | None = None,
        *,
        message_parent: Callable[[], QWidget | None] = lambda: None,
        open_url: Callable[[QUrl], object] = QDesktopServices.openUrl,
        log_files: Callable[[], Sequence[Path]] = lambda: (),
    ) -> None:
        super().__init__(parent)
        self.page = page
        self._config_dir = Path(config_dir)
        self._message_parent = message_parent
        self._open_url = open_url
        self._log_files = log_files
        self._host: Any = None
        self._generation = 0
        self._seq: dict[str, int] = {}
        self._writes = 0  # số lời ghi đã gửi tới node (bỏ ảnh chụp xin trước một lời ghi)
        self._confirmed: dict[str, Any] = {}
        self._devices: dict[str, DeviceSettings] = {}
        self._saved_autostart: bool | None = None
        self._notice_action: Callable[[], None] | None = None
        self._confirm_box: QMessageBox | None = None
        self._cancel_pending_box: QMessageBox | None = None
        self._cancel_pending_target: tuple[str, str] | Literal["all"] | None = None
        self._install_update_box: QMessageBox | None = None
        self._install_version = ""
        self._dialog: QFileDialog | None = None
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._update_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._export_running = False
        self._update_busy = False
        self._update_info: UpdateInfo | None = None
        self._downloaded_installer: tuple[Path, str | None] | None = None
        self._relay = _ExportRelay(self)
        self._relay.finished.connect(self._on_export_finished, Qt.ConnectionType.QueuedConnection)
        self._update_relay = _UpdateRelay(self)
        self._update_relay.check_finished.connect(self._on_check_finished, Qt.ConnectionType.QueuedConnection)
        self._update_relay.download_progress.connect(
            self._on_download_progress, Qt.ConnectionType.QueuedConnection
        )
        self._update_relay.download_finished.connect(
            self._on_download_finished, Qt.ConnectionType.QueuedConnection
        )

        page.name_submitted.connect(self._on_name)
        page.visible_toggled.connect(self._on_visible)
        page.stealth_toggled.connect(self._on_stealth)
        page.language_selected.connect(self._on_language)
        page.theme_selected.connect(self._on_theme)
        page.accent_selected.connect(self._on_accent)
        page.sound_mode_selected.connect(self._on_sound_mode)
        page.sound_volume_selected.connect(self._on_sound_volume)
        page.auto_dismiss_notices_toggled.connect(self._on_auto_dismiss_notices)
        page.tray_notifications_toggled.connect(self._on_tray_notifications)
        page.autostart_toggled.connect(self._on_autostart)
        page.create_subfolder_toggled.connect(self._on_create_subfolder)
        page.rate_limit_changed.connect(self._on_rate)
        page.retention_changed.connect(self._on_retention)
        page.delta_sync_toggled.connect(self._on_delta_sync)
        page.routing_rules_changed.connect(self._on_routing_rules)
        page.watch_portals_changed.connect(self._on_watch_portals)
        page.watch_portals_paused_toggled.connect(self._on_watch_portals_paused)
        page.cancel_pending_requested.connect(self.request_cancel_pending)
        page.cancel_all_pending_requested.connect(self.request_cancel_all_pending)
        page.ask_toggled.connect(self._on_ask)
        page.clipboard_toggled.connect(self._on_clipboard)
        page.clear_history_requested.connect(self.request_clear_history)
        page.export_logs_requested.connect(self.request_export)
        page.verify_receipt_requested.connect(self.request_verify_receipt)
        page.check_update_requested.connect(self.request_check_update)
        page.download_update_requested.connect(self.request_download_update)
        page.open_notices_requested.connect(self._on_open_notices)
        page.open_support_requested.connect(self._on_open_support)
        page.notice_action.connect(self._on_notice_action)
        model = page.model
        for signal in (model.modelReset, model.rowsInserted, model.dataChanged):
            signal.connect(self._on_model_changed)
        page.installEventFilter(self)

    # ---- Thuộc tính ----------------------------------------------------------------------------

    @property
    def host(self) -> Any:
        return self._host

    @property
    def confirm_box(self) -> QMessageBox | None:
        """Hộp xác nhận “Xóa toàn bộ lịch sử” đang mở."""
        return self._confirm_box

    @property
    def cancel_pending_box(self) -> QMessageBox | None:
        """Hộp xác nhận hủy lượt nhận đang chờ đang mở."""
        return self._cancel_pending_box

    @property
    def install_update_box(self) -> QMessageBox | None:
        """Hộp xác nhận chạy bộ cài cập nhật đang mở."""
        return self._install_update_box

    @property
    def dialog(self) -> QFileDialog | None:
        """Hộp lưu file log đang mở."""
        return self._dialog

    @property
    def export_running(self) -> bool:
        return self._export_running

    # ---- Vòng đời ------------------------------------------------------------------------------

    def attach(self, host: Any) -> None:
        """Host mới (lần đầu, “Thử lại”): trang chờ ảnh chụp mới, đóng mọi hộp."""
        self.close_all()
        self._host = host
        self._seq.clear()
        self._confirmed.clear()
        self._devices = {}
        self._saved_autostart = None
        self.page.set_loaded(False)

    def on_started(self, host: Any) -> None:
        if host is not self._host or host is None:
            return
        self._read_snapshot()

    def refresh(self) -> None:
        """Đọc lại ảnh chụp (trang vừa hiện)."""
        if self._host is not None:
            self._read_snapshot()

    def close_all(self) -> None:
        """Đóng hộp xác nhận và hộp lưu file; kết quả xuất log / cập nhật đang chạy về sau bị bỏ."""
        self._generation += 1
        box, self._confirm_box = self._confirm_box, None
        if box is not None:
            box.close()
            box.deleteLater()
        pending_box, self._cancel_pending_box = self._cancel_pending_box, None
        self._cancel_pending_target = None
        if pending_box is not None:
            pending_box.close()
            pending_box.deleteLater()
        install_box, self._install_update_box = self._install_update_box, None
        self._downloaded_installer = None
        if install_box is not None:
            install_box.close()
            install_box.deleteLater()
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
        self.page.close_dialogs()
        if self._export_running:
            self._export_running = False
            self.page.set_export_running(False)
        if self._update_busy:
            self._update_busy = False
            self.page.set_update_state(UpdateUiState.IDLE)

    def shutdown(self) -> None:
        """Thoát: bỏ việc xuất log và kiểm tra cập nhật chưa chạy, không chờ việc đang chạy."""
        self.close_all()
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        update_executor, self._update_executor = self._update_executor, None
        if update_executor is not None:
            update_executor.shutdown(wait=False, cancel_futures=True)

    # ---- Ảnh chụp ------------------------------------------------------------------------------

    def _read_snapshot(self) -> None:
        host = self._host
        generation = self._generation
        writes = self._writes
        from teleport.ui import settings as _settings_mod

        host.call(
            _settings_mod.read_snapshot,
            functools.partial(self._on_snapshot, generation, host, writes),
        )

    def _on_snapshot(self, generation: int, host: Any, writes: int, result: object) -> None:
        if generation != self._generation or host is not self._host:
            return
        if writes != self._writes and not isinstance(result, BaseException):
            # Người dùng đã gửi lời ghi sau khi xin ảnh chụp này: ảnh chụp cũ hơn lời ghi đó, đặt vào
            # trang sẽ đè giá trị vừa chọn. Đọc lại (chạy sau lời ghi trên loop của node).
            self._read_snapshot()
            return
        if isinstance(result, BaseException):
            if isinstance(result, (RuntimeError, asyncio.CancelledError)):
                log.debug("không đọc được cài đặt: node không chạy")
                return
            log.error("không đọc được cài đặt: %s", type(result).__name__)
            self._show_notice(InfoLevel.ERROR, snapshot_error_text, action=(_retry_text, self.refresh))
            return
        if not isinstance(result, SettingsSnapshot):
            return
        self._confirmed.update(
            name=result.name,
            visible=result.visible,
            stealth=result.stealth,
            language=result.language,
            rate=result.rate_limit,
            retention=result.temp_retention_days,
            theme=result.theme,
            accent=result.accent,
            sound_mode=result.sound_mode,
            sound_volume=result.sound_volume,
            auto_dismiss_notices=result.auto_dismiss_notices,
            tray_notifications=result.tray_notifications,
            delta_sync=result.delta_sync,
            routing_rules=result.routing_rules,
            watch_portals=result.watch_portals,
            watch_portals_paused=result.watch_portals_paused,
        )
        self._devices = dict(result.devices)
        self._saved_autostart = result.autostart
        self.page.set_values(result)
        self.page.set_loaded(True)
        self.device_settings_changed.emit(dict(self._devices))
        self._sync_theme(theme.mode_from_setting(result.theme), result.accent)
        sounds.set_preference(result.sound_mode, sounds.volume_from_setting(result.sound_volume))
        widgets.set_auto_dismiss_enabled(result.auto_dismiss_notices)
        ui_tray.set_notifications_enabled(result.tray_notifications)
        if self.page.info_bar.title == snapshot_error_text():  # đọc lại được: câu lỗi cũ không còn đúng
            self.page.clear_notice()
        self._reconcile_autostart()

    def _reconcile_autostart(self) -> None:
        """Registry thắng: khác `settings.autostart` thì chỉ ghi cài đặt, không bao giờ ghi registry."""
        state = self._read_autostart()
        self.page.set_autostart(state)
        saved = self._saved_autostart
        if state is None or state is autostart.AutostartState.UNSUPPORTED or saved is None:
            return
        on = state is autostart.AutostartState.ON
        if saved == on or self._host is None:
            return
        self._saved_autostart = on
        self._host.call(lambda node: node.set_ui_preferences(autostart=on), _warn("lưu cài đặt khởi động"))

    def _read_autostart(self) -> autostart.AutostartState | None:
        try:
            return autostart.state(self._config_dir)
        except autostart.AutostartError as e:
            log.warning("không đọc được cài đặt khởi động: %s", type(e).__name__)
            return None
        except Exception as e:
            log.error("đọc cài đặt khởi động lỗi: %s", type(e).__name__)
            return None

    def _on_model_changed(self, *_args: object) -> None:
        """Máy mới được ghép đôi khi trang đang mở: đọc cài đặt theo thiết bị của nó."""
        host = self._host
        if host is None or not self.page.loaded:
            return
        if all(device_id in self._devices for device_id in self.page.device_ids()):
            return
        generation = self._generation
        host.call(_read_device_settings, functools.partial(self._on_device_settings, generation, host))

    def _on_device_settings(self, generation: int, host: Any, result: object) -> None:
        if generation != self._generation or host is not self._host:
            return
        if isinstance(result, BaseException):
            log.debug("không đọc được cài đặt theo thiết bị: %s", type(result).__name__)
            return
        if isinstance(result, dict):
            self._devices = dict(result)
            self.page.set_device_settings(self._devices)
            self.device_settings_changed.emit(dict(self._devices))

    # ---- Ý định của người dùng -----------------------------------------------------------------

    def _call(
        self,
        field: str,
        fn: Callable[[Node], object],
        revert: Callable[[Any], None],
        on_ok: Callable[[object], None] | None = None,
        *,
        value: object = None,
        on_message_error: Callable[[], None] | None = None,
        on_key_error: Callable[[], None] | None = None,
    ) -> None:
        host = self._host
        if host is None:
            revert(self._confirmed.get(field))
            return
        seq = self._seq.get(field, 0) + 1
        self._seq[field] = seq
        self._writes += 1
        generation = self._generation

        def done(result: object) -> None:
            if generation != self._generation or host is not self._host:
                return
            latest = self._seq.get(field) == seq
            if not isinstance(result, BaseException):
                self._confirmed[field] = value
                if on_ok is not None:
                    on_ok(result)
                return
            if isinstance(result, (RuntimeError, asyncio.CancelledError)):
                log.debug("không lưu được cài đặt %s: node không chạy", field)
                if latest:
                    revert(self._confirmed.get(field))
                return
            if isinstance(result, KeyError) and on_key_error is not None:
                on_key_error()
                return
            if isinstance(result, MessageError) and on_message_error is not None:
                on_message_error()
                return
            if latest:
                revert(self._confirmed.get(field))
            if isinstance(result, SettingsSaveError):
                log.warning("không lưu được cài đặt %s: %s", field, type(result).__name__)
                self._show_notice(InfoLevel.ERROR, settings_error_text())
            else:
                log.error("lưu cài đặt %s lỗi: %s", field, type(result).__name__)
                self._show_notice(InfoLevel.ERROR, other_error_text())

        host.call(fn, done)

    def _on_name(self, name: str) -> None:
        def ok(result: object) -> None:
            final = result if isinstance(result, str) else name
            self._confirmed["name"] = final
            self.page.set_name(final)
            self.name_changed.emit(final)

        self._call(
            "name",
            lambda node: node.set_name(name),
            lambda old: self.page.set_name(old if isinstance(old, str) else ""),
            ok,
            value=name,
            on_message_error=lambda: self.page.reject_name(
                str(self._confirmed.get("name") or ""), name_invalid_text()
            ),
        )

    def _on_visible(self, visible: bool) -> None:
        host = self._host

        def run(node: Node) -> None:
            node.set_visible(visible)
            host.apply_pairing_policy(node)  # cùng một khối đồng bộ với `set_visible` (docstring `ui.bridge`)

        self._call(
            "visible",
            run,
            lambda old: self.page.set_visible(True if old is None else bool(old)),
            lambda _r: self.visibility_changed.emit(visible),
            value=visible,
        )

    def _on_stealth(self, stealth: bool) -> None:
        self._call(
            "stealth",
            lambda node: node.set_stealth(stealth),
            lambda old: self.page.set_stealth(False if old is None else bool(old)),
            value=stealth,
        )

    def _on_language(self, code: object) -> None:
        language = code if isinstance(code, str) else None

        def ok(_result: object) -> None:
            i18n.apply_preference(language)

        self._call(
            "language",
            lambda node: node.set_ui_preferences(language=language),
            lambda old: self.page.set_language(
                old if isinstance(old, str) else None, i18n.available_languages()
            ),
            ok,
            value=language,
        )

    def _on_theme(self, value: object) -> None:
        saved = theme.mode_from_setting(value if isinstance(value, str) else None).setting_value

        def ok(_result: object) -> None:
            _apply_theme(theme.mode_from_setting(saved), theme.current_accent())

        self._call(
            "theme",
            lambda node: node.set_ui_preferences(theme=saved),
            lambda old: self.page.set_theme(old if isinstance(old, str) else None),
            ok,
            value=saved,
        )

    def _on_accent(self, value: object) -> None:
        accent = value if isinstance(value, str) else None

        def ok(_result: object) -> None:
            _apply_theme(theme.current_mode(), accent)

        self._call(
            "accent",
            lambda node: node.set_ui_preferences(accent=accent),
            lambda old: self.page.set_accent(old if isinstance(old, str) else None),
            ok,
            value=accent,
        )

    @staticmethod
    def _sync_theme(mode: theme.Mode, accent: str | None) -> None:
        """Ảnh chụp khác lựa chọn đang áp (mỗi ảnh chụp; chỉ áp khi khác): áp theo ảnh chụp."""
        if mode is not theme.current_mode() or theme.accent_from_setting(accent) != theme.current_accent():
            _apply_theme(mode, accent)

    def _on_sound_mode(self, value: object) -> None:
        saved = value if isinstance(value, str) else None

        def ok(_result: object) -> None:
            sounds.set_preference(saved, sounds.current_volume())

        self._call(
            "sound_mode",
            lambda node: node.set_ui_preferences(sound_mode=saved),
            lambda old: self.page.set_sound_mode(old if isinstance(old, str) else None),
            ok,
            value=saved,
        )

    def _on_sound_volume(self, value: object) -> None:
        saved = value if type(value) is int else None

        def ok(_result: object) -> None:
            sounds.set_preference(sounds.current_mode(), sounds.volume_from_setting(saved))

        self._call(
            "sound_volume",
            lambda node: node.set_ui_preferences(sound_volume=saved),
            lambda old: self.page.set_sound_volume(old if type(old) is int else None),
            ok,
            value=saved,
        )

    def _on_auto_dismiss_notices(self, enabled: bool) -> None:
        saved = bool(enabled)

        def ok(_result: object) -> None:
            widgets.set_auto_dismiss_enabled(saved)

        self._call(
            "auto_dismiss_notices",
            lambda node: node.set_ui_preferences(auto_dismiss_notices=saved),
            lambda old: self.page.set_auto_dismiss_notices(True if old is None else bool(old)),
            ok,
            value=saved,
        )

    def _on_tray_notifications(self, enabled: bool) -> None:
        saved = bool(enabled)

        def ok(_result: object) -> None:
            ui_tray.set_notifications_enabled(saved)

        self._call(
            "tray_notifications",
            lambda node: node.set_ui_preferences(tray_notifications=saved),
            lambda old: self.page.set_tray_notifications(True if old is None else bool(old)),
            ok,
            value=saved,
        )

    def _on_rate(self, value: object) -> None:
        rate = value if isinstance(value, int) else None
        self._call(
            "rate",
            lambda node: node.set_rate_limit(rate),
            lambda old: self.page.set_rate_limit(old if isinstance(old, int) else None),
            value=rate,
        )

    def _on_retention(self, value: object) -> None:
        days = value if isinstance(value, int) else None
        self._call(
            "retention",
            lambda node: node.set_temp_retention_days(days),
            lambda old: self.page.set_retention(old if isinstance(old, int) else None),
            value=days,
        )

    def _on_ask(self, device_id: str, ask: bool) -> None:
        field = f"ask:{device_id}"
        self._confirmed.setdefault(field, self._devices.get(device_id, DeviceSettings()).ask_before_receive)

        def revert(old: object) -> None:
            row = self.page.device_row(device_id)
            if row is not None:
                row.set_ask(True if old is None else bool(old))

        def ok(_result: object) -> None:
            current = self._devices.get(device_id, DeviceSettings())
            self._devices[device_id] = replace(current, ask_before_receive=ask)
            self.device_settings_changed.emit(dict(self._devices))

        self._call(
            field,
            lambda node: node.set_ask_before_receive(device_id, ask),
            revert,
            ok,
            value=ask,
            on_key_error=self.refresh,
        )

    def _on_clipboard(self, device_id: str, enabled: bool) -> None:
        """“Đồng bộ clipboard” của một máy: như `_on_ask`, với `Node.set_clipboard`."""
        field = f"clipboard:{device_id}"
        self._confirmed.setdefault(field, self._devices.get(device_id, DeviceSettings()).clipboard)

        def revert(old: object) -> None:
            row = self.page.device_row(device_id)
            if row is not None:
                row.set_clipboard(False if old is None else bool(old))

        def ok(_result: object) -> None:
            current = self._devices.get(device_id, DeviceSettings())
            self._devices[device_id] = replace(current, clipboard=enabled)
            self.device_settings_changed.emit(dict(self._devices))

        self._call(
            field,
            lambda node: node.set_clipboard(device_id, enabled),
            revert,
            ok,
            value=enabled,
            on_key_error=self.refresh,
        )

    def _on_autostart(self, enabled: bool) -> None:
        """Ghi registry trước (thread Qt); lỗi → trả công tắc, báo, không lưu cài đặt."""
        try:
            if enabled:
                autostart.enable(self._config_dir)
            else:
                autostart.disable(self._config_dir)
        except autostart.AutostartError as e:
            log.warning("không ghi được cài đặt khởi động: %s", type(e).__name__)
            self.page.set_autostart(self._read_autostart())  # lỗi có thể tới sau khi đã ghi một phần
            self._show_notice(InfoLevel.ERROR, autostart_write_error_text())
            return
        except Exception as e:
            log.error("ghi cài đặt khởi động lỗi: %s", type(e).__name__)
            self.page.set_autostart(self._read_autostart())  # lỗi có thể tới sau khi đã ghi một phần
            self._show_notice(InfoLevel.ERROR, other_error_text())
            return
        self._saved_autostart = enabled
        host = self._host
        if host is not None:
            host.call(lambda node: node.set_ui_preferences(autostart=enabled), _warn("lưu cài đặt khởi động"))
        self.page.set_autostart(self._read_autostart())

    def _on_create_subfolder(self, enabled: bool) -> None:
        host = self._host
        if host is not None:
            host.call(lambda node: node.set_create_subfolder(enabled), _warn("lưu cài đặt tạo thư mục con"))

    def _on_delta_sync(self, enabled: bool) -> None:
        self._call(
            "delta_sync",
            lambda node: node.set_delta_sync(enabled),
            lambda old: self.page.set_delta_sync(True if old is None else bool(old)),
            value=bool(enabled),
        )

    def _on_routing_rules(self, rules: object) -> None:
        next_rules = tuple(rules) if isinstance(rules, (tuple, list)) else ()

        def ok(result: object) -> None:
            if isinstance(result, tuple):
                self._confirmed["routing_rules"] = result
                self.page.set_routing_rules(result)

        self._call(
            "routing_rules",
            lambda node: node.set_routing_rules(next_rules),
            lambda old: self.page.set_routing_rules(old if isinstance(old, tuple) else ()),
            ok,
            value=next_rules,
        )

    def _on_watch_portals(self, rules: object) -> None:
        next_rules = tuple(rules) if isinstance(rules, (tuple, list)) else ()

        def ok(result: object) -> None:
            if isinstance(result, tuple):
                self._confirmed["watch_portals"] = result
                self.page.set_watch_portals(result)

        self._call(
            "watch_portals",
            lambda node: node.set_watch_portals(next_rules),
            lambda old: self.page.set_watch_portals(old if isinstance(old, tuple) else ()),
            ok,
            value=next_rules,
        )

    def _on_watch_portals_paused(self, paused: bool) -> None:
        self._call(
            "watch_portals_paused",
            lambda node: node.set_watch_portals_paused(bool(paused)),
            lambda old: self.page.set_watch_portals_paused(False if old is None else bool(old)),
            value=bool(paused),
        )

    # ---- Xóa toàn bộ lịch sử -------------------------------------------------------------------

    def request_clear_history(self) -> None:
        """Hộp xác nhận không chặn (`open()`); “Không” là nút mặc định và Esc."""
        box = self._confirm_box
        if box is not None:
            box.raise_()
            box.activateWindow()
            return
        box = QMessageBox(self._message_parent())
        box.setObjectName("clearHistoryBox")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        accept = box.addButton("", QMessageBox.ButtonRole.DestructiveRole)
        decline = box.addButton("", QMessageBox.ButtonRole.RejectRole)
        _translate_clear_history_box(box)
        box.installEventFilter(self)  # đổi ngôn ngữ lúc hộp đang mở
        box.setDefaultButton(decline)
        box.setEscapeButton(decline)
        generation = self._generation
        box.finished.connect(lambda _r: self._on_confirm_finished(box, accept, generation))
        self._confirm_box = box
        motion.fade_in_on_show(box)
        box.open()

    def _on_confirm_finished(self, box: QMessageBox, accept: object, generation: int) -> None:
        if self._confirm_box is box:
            self._confirm_box = None
        clicked = box.clickedButton()
        box.deleteLater()
        if clicked is not accept or generation != self._generation:
            return
        host = self._host
        if host is None:
            return

        def done(result: object) -> None:
            if generation != self._generation or host is not self._host:
                return
            if isinstance(result, BaseException):
                if isinstance(result, (RuntimeError, asyncio.CancelledError)):
                    log.debug("không xóa được lịch sử: node không chạy")
                    return
                log.error("xóa lịch sử lỗi: %s", type(result).__name__)
                self._show_notice(InfoLevel.ERROR, history_clear_failed_text())
                return
            self._show_notice(InfoLevel.SUCCESS, history_cleared_text())
            self.history_cleared.emit(None)

        host.call(lambda node: node.transfers.clear_history(), done)

    # ---- Hủy lượt nhận đang chờ ----------------------------------------------------------------

    def request_cancel_pending(self, device_id: str, transfer_id: str) -> None:
        """Mở hộp xác nhận không chặn để hủy một lượt nhận đang chờ / dở dang."""
        self._open_cancel_pending_box((device_id, transfer_id))

    def request_cancel_all_pending(self) -> None:
        """Mở hộp xác nhận không chặn để hủy mọi lượt nhận đang chờ / dở dang."""
        self._open_cancel_pending_box("all")

    def _open_cancel_pending_box(self, target: tuple[str, str] | Literal["all"]) -> None:
        box = self._cancel_pending_box
        if box is not None:
            if self._cancel_pending_target == target:
                box.raise_()
                box.activateWindow()
                return
            self._cancel_pending_box = None
            box.close()
            box.deleteLater()
        box = QMessageBox(self._message_parent())
        box.setObjectName("cancelPendingBox")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        accept = box.addButton("", QMessageBox.ButtonRole.DestructiveRole)
        decline = box.addButton("", QMessageBox.ButtonRole.RejectRole)
        self._cancel_pending_target = target
        _translate_cancel_pending_box(box, target == "all")
        box.installEventFilter(self)
        box.setDefaultButton(decline)
        box.setEscapeButton(decline)
        generation = self._generation
        box.finished.connect(lambda _r: self._on_cancel_pending_finished(box, accept, target, generation))
        self._cancel_pending_box = box
        motion.fade_in_on_show(box)
        box.open()

    def _on_cancel_pending_finished(
        self,
        box: QMessageBox,
        accept: object,
        target: tuple[str, str] | Literal["all"],
        generation: int,
    ) -> None:
        if self._cancel_pending_box is box:
            self._cancel_pending_box = None
            self._cancel_pending_target = None
        clicked = box.clickedButton()
        box.deleteLater()
        if clicked is not accept or generation != self._generation:
            return
        host = self._host
        if host is None:
            return

        def done(result: object) -> None:
            if generation != self._generation or host is not self._host:
                return
            if isinstance(result, BaseException):
                if isinstance(result, (RuntimeError, asyncio.CancelledError)):
                    log.debug("không hủy được lượt nhận: node không chạy")
                    return
                log.error("hủy lượt nhận lỗi: %s", type(result).__name__)
                self._show_notice(InfoLevel.ERROR, pending_cancel_failed_text())
                return
            if target == "all":
                self._show_notice(InfoLevel.SUCCESS, pending_all_cancelled_text())
            elif result:
                self._show_notice(InfoLevel.SUCCESS, pending_cancelled_text())
            self.refresh()

        if target == "all":

            async def cancel_all(node: Node) -> int:
                keys = [
                    info.key for info in node.transfers.transfers() if info.key.direction is Direction.RECEIVE
                ]
                cancelled = 0
                for key in keys:
                    if await node.transfers.cancel_transfer(key, wait_disk=True):
                        cancelled += 1
                return cancelled

            host.submit(cancel_all, done)
        else:
            device_id, transfer_id = target
            key = TransferKey(device_id, Direction.RECEIVE, transfer_id)
            host.submit(lambda node: node.transfers.cancel_transfer(key, wait_disk=True), done)

    # ---- Kiểm tra & tải bản cập nhật thủ công --------------------------------------------------

    def request_check_update(self) -> None:
        """Kiểm tra bản cập nhật khi người dùng bấm nút (mvp N9: chạy trên thread nền)."""
        if self._update_busy:
            return
        if self._update_executor is None:
            self._update_executor = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix=UPDATE_THREAD)
        from teleport.ui import settings as _settings_mod

        fn = _settings_mod.check_update_fn
        generation = self._generation
        try:
            future = self._update_executor.submit(fn)
        except RuntimeError as e:
            log.debug("không kiểm tra được cập nhật: %s", type(e).__name__)
            return
        self._update_busy = True
        self._update_info = None
        self.page.set_update_state(UpdateUiState.CHECKING)
        future.add_done_callback(functools.partial(_deliver_check, self._update_relay, generation))

    def _on_check_finished(self, payload: object) -> None:
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        generation, result = payload
        if generation != self._generation:
            return
        self._update_busy = False
        if isinstance(result, concurrent.futures.CancelledError):
            return
        if result is None or isinstance(result, BaseException):
            if isinstance(result, BaseException):
                log.warning("kiểm tra cập nhật lỗi: %s", type(result).__name__)
            self.page.set_update_state(UpdateUiState.ERROR)
            return
        if isinstance(result, UpdateInfo):
            if result.is_newer:
                self._update_info = result
                self.page.set_update_state(UpdateUiState.AVAILABLE, info=result)
            else:
                self._update_info = None
                self.page.set_update_state(UpdateUiState.UP_TO_DATE, info=result)
            return
        self.page.set_update_state(UpdateUiState.ERROR)

    def request_download_update(self) -> None:
        """Tải bộ cài cập nhật hoặc mở trang phát hành khi người dùng bấm Tải về."""
        info = self._update_info
        if info is None or self._update_busy:
            return
        from teleport.ui import settings as _settings_mod

        if _settings_mod.auto_install_supported() and info.download_url:
            if self._update_executor is None:
                self._update_executor = concurrent.futures.ThreadPoolExecutor(
                    1, thread_name_prefix=UPDATE_THREAD
                )
            dl_fn = _settings_mod.download_update_fn
            generation = self._generation
            try:
                future = self._update_executor.submit(
                    _download_worker, self._update_relay, generation, info, dl_fn
                )
            except RuntimeError as e:
                log.debug("không tải được cập nhật: %s", type(e).__name__)
                return
            self._update_busy = True
            self.page.set_update_state(UpdateUiState.DOWNLOADING, info=info)
            future.add_done_callback(
                functools.partial(_deliver_download, self._update_relay, generation, info)
            )
            return
        url = info.release_url.strip()
        if not url.startswith("https://") or self._open_url(QUrl(url)) is False:
            log.warning("không mở được trang phát hành cập nhật")
            self._show_notice(InfoLevel.ERROR, update_launch_failed_text())

    def _on_download_progress(self, payload: object) -> None:
        if not isinstance(payload, tuple) or len(payload) != 3:
            return
        generation, done, total = payload
        if generation != self._generation or not self._update_busy:
            return
        self.page.set_update_state(UpdateUiState.DOWNLOADING, info=self._update_info, progress=(done, total))

    def _on_download_finished(self, payload: object) -> None:
        if not isinstance(payload, tuple) or len(payload) != 3:
            return
        generation, info, result = payload
        if generation != self._generation:
            return
        self._update_busy = False
        if isinstance(result, Path):
            self._downloaded_installer = (result, info.expected_sha256)
            self.page.set_update_state(UpdateUiState.READY, info=info)
            self._open_install_update_box(info)
        elif isinstance(result, concurrent.futures.CancelledError):
            log.debug("tải cập nhật bị hủy")
        else:
            log.warning("tải cập nhật lỗi: %s", type(result).__name__)
            self.page.set_update_state(UpdateUiState.ERROR, info=info)
            self._show_notice(InfoLevel.ERROR, update_launch_failed_text())

    def _open_install_update_box(self, info: UpdateInfo) -> None:
        box = self._install_update_box
        if box is not None:
            box.raise_()
            box.activateWindow()
            return
        box = QMessageBox(self._message_parent())
        box.setObjectName("installUpdateBox")
        box.setIcon(QMessageBox.Icon.Information)
        box.setTextFormat(Qt.TextFormat.PlainText)
        accept = box.addButton("", QMessageBox.ButtonRole.AcceptRole)
        decline = box.addButton("", QMessageBox.ButtonRole.RejectRole)
        self._install_version = info.version
        _translate_install_update_box(box, info.version)
        box.installEventFilter(self)
        box.setDefaultButton(decline)
        box.setEscapeButton(decline)
        generation = self._generation
        box.finished.connect(lambda _r: self._on_install_update_finished(box, accept, generation))
        self._install_update_box = box
        motion.fade_in_on_show(box)
        box.open()

    def _on_install_update_finished(self, box: QMessageBox, accept: object, generation: int) -> None:
        if self._install_update_box is box:
            self._install_update_box = None
        clicked = box.clickedButton()
        box.deleteLater()
        pair, self._downloaded_installer = self._downloaded_installer, None
        if clicked is not accept or generation != self._generation or pair is None:
            return
        installer_path, expected_sha256 = pair
        from teleport.ui import settings as _settings_mod

        fn = _settings_mod.apply_update_fn
        try:
            ok = bool(fn(installer_path, expected_sha256=expected_sha256))
        except Exception as e:
            log.warning("chưa chạy được bộ cài cập nhật: %s", type(e).__name__)
            ok = False
        if not ok:
            self._show_notice(InfoLevel.ERROR, update_launch_failed_text())

    # ---- Xuất log ------------------------------------------------------------------------------

    def request_export(self) -> None:
        """Hộp lưu file (không chặn); chọn xong thì xuất ngoài thread Qt."""
        if self._export_running:
            return
        dialog = self._dialog
        if dialog is not None:
            dialog.raise_()
            dialog.activateWindow()
            return
        from teleport.ui import settings as _settings_mod

        dialog = _settings_mod.dialog_factory(self._message_parent())
        dialog.setObjectName("exportLogsDialog")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setWindowTitle(QCoreApplication.translate("Settings", "Lưu file log"))
        dialog.setNameFilter(QCoreApplication.translate("Settings", "File zip (*.zip)"))
        dialog.setDefaultSuffix("zip")
        dialog.setDirectory(default_export_dir())
        dialog.selectFile(default_export_name())
        generation = self._generation
        dialog.fileSelected.connect(lambda path: self._on_export_path(generation, path))
        dialog.finished.connect(lambda _r, d=dialog: self._dialog_finished(d))
        self._dialog = dialog
        dialog.open()

    def _dialog_finished(self, dialog: QFileDialog) -> None:
        if self._dialog is dialog:
            self._dialog = None
        dialog.deleteLater()

    def _on_export_path(self, generation: int, path: str) -> None:
        if generation != self._generation or not path or self._export_running:
            return
        dest = Path(path)
        files = [Path(p) for p in self._log_files()]
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix=EXPORT_THREAD)
        from teleport.ui import settings as _settings_mod

        fn = _settings_mod.export_fn  # seam: đọc lúc gọi
        try:
            future = self._executor.submit(fn, files, dest)
        except RuntimeError as e:  # executor đã đóng (đang thoát)
            log.debug("không xuất được log: %s", type(e).__name__)
            return
        self._export_running = True
        self.page.set_export_running(True)
        future.add_done_callback(functools.partial(_deliver_export, self._relay, self._generation, dest))

    def _on_export_finished(self, payload: object) -> None:
        if not isinstance(payload, tuple) or len(payload) != 3:
            return
        generation, dest, result = payload
        if generation != self._generation:
            return
        self._export_running = False
        self.page.set_export_running(False)
        if isinstance(result, Path):
            folder = result.parent
            self._show_notice(
                InfoLevel.SUCCESS,
                export_saved_text(result),
                action=(
                    QCoreApplication.translate("Settings", "Mở thư mục"),
                    lambda: self._open_folder(folder),
                ),
            )
        elif isinstance(result, concurrent.futures.CancelledError):
            log.debug("xuất log bị hủy")
        elif isinstance(result, OSError):
            log.warning("không lưu được file log: %s", type(result).__name__)
            self._show_notice(InfoLevel.ERROR, export_write_error_text(dest))
        else:
            log.error("xuất log lỗi: %s", type(result).__name__)
            self._show_notice(InfoLevel.ERROR, export_other_error_text())

    def _open_folder(self, folder: Path) -> None:
        if self._open_url(QUrl.fromLocalFile(str(folder))) is False:
            log.warning("không mở được thư mục chứa file log")

    def _on_open_notices(self, path: object) -> None:
        if isinstance(path, Path) and self._open_url(QUrl.fromLocalFile(str(path))) is False:
            log.warning("không mở được file thông báo bên thứ ba")

    def _on_open_support(self) -> None:
        if self._open_url(QUrl(SUPPORT_URL)) is False:
            log.warning("không mở được trang ủng hộ dự án")

    # ---- Biên lai truyền tệp -------------------------------------------------------------------

    def request_verify_receipt(self) -> None:
        """Hộp chọn tệp biên lai (không chặn) rồi xác minh chữ ký số và hiển thị kết quả."""
        dialog = self._dialog
        if dialog is not None:
            dialog.raise_()
            dialog.activateWindow()
            return
        from teleport.ui import settings as _settings_mod

        dialog = _settings_mod.dialog_factory(self._message_parent())
        dialog.setObjectName("verifyReceiptDialog")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setWindowTitle(QCoreApplication.translate("Settings", "Chọn tệp biên lai"))
        dialog.setNameFilter(
            QCoreApplication.translate("Settings", "Tệp biên lai (*.teleport-receipt.json *.json)")
        )
        r_dir = receipts.receipts_dir(self._config_dir)
        dialog.setDirectory(str(r_dir) if r_dir.is_dir() else default_export_dir())
        generation = self._generation
        dialog.fileSelected.connect(lambda path: self._on_verify_receipt_path(generation, path))
        dialog.finished.connect(lambda _r, d=dialog: self._dialog_finished(d))
        self._dialog = dialog
        dialog.open()

    def _on_verify_receipt_path(self, generation: int, path: str) -> None:
        if generation != self._generation or not path:
            return
        receipt_file = Path(path)
        trusted_fps = receipts.known_trusted_fingerprints(self._config_dir)
        result = receipts.verify_receipt(receipt_file, trusted_fingerprints=trusted_fps)
        if result.valid and result.receipt is not None:
            payload = result.receipt.payload
            peer = texts.peer_label(payload.peer_name, payload.peer_device_id)
            size_text = texts.format_size(payload.total_bytes)
            if result.trusted_signer is False:
                self._show_notice(
                    InfoLevel.WARNING,
                    texts.fill(
                        QCoreApplication.translate(
                            "Settings",
                            "Chữ ký biên lai hợp lệ nhưng máy ký chưa ghép đôi ({peer}, {size}).",
                        ),
                        peer=peer,
                        size=size_text,
                    ),
                )
            else:
                self._show_notice(
                    InfoLevel.SUCCESS,
                    texts.fill(
                        QCoreApplication.translate("Settings", "Biên lai hợp lệ: {peer} ({size})."),
                        peer=peer,
                        size=size_text,
                    ),
                )
            return
        self._show_notice(
            InfoLevel.ERROR,
            QCoreApplication.translate("Settings", "Biên lai không hợp lệ hoặc đã bị sửa đổi."),
        )

    # ---- Thông báo -----------------------------------------------------------------------------

    def notice_text(self) -> str | None:
        """Câu đang hiện ở thanh thông báo của trang, hay None khi thanh ẩn."""
        bar = self.page.info_bar
        return bar.title if bar.is_open else None

    def show_notice(
        self,
        level: InfoLevel,
        text: texts.Text,
        *,
        action: tuple[texts.Text, Callable[[], None]] | None = None,
    ) -> None:
        """Hiện câu ở thanh thông báo của trang; `action` = (chữ nút, hàm) chỉ gắn với câu này
        (thông báo kế tiếp thay hay xóa nó). `ui.app` dùng cho lỗi đổi thư mục nhận."""
        self._show_notice(level, text, action=action)

    def _show_notice(
        self,
        level: InfoLevel,
        text: texts.Text,
        *,
        action: tuple[texts.Text, Callable[[], None]] | None = None,
    ) -> None:
        self._notice_action = action[1] if action is not None else None
        self.page.show_notice(level, text, action=action[0] if action is not None else None)

    def _on_notice_action(self) -> None:
        action, self._notice_action = self._notice_action, None
        self.page.clear_notice()
        if action is not None:
            action()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.LanguageChange and isinstance(watched, QMessageBox):
            if watched is self._confirm_box:
                _translate_clear_history_box(watched)
                return False
            if watched is self._cancel_pending_box and self._cancel_pending_target is not None:
                _translate_cancel_pending_box(watched, self._cancel_pending_target == "all")
                return False
            if watched is self._install_update_box:
                _translate_install_update_box(watched, self._install_version)
                return False
        if (
            watched is self.page
            and event.type() == QEvent.Type.Show
            and not event.spontaneous()
            and isValid(self)
        ):
            self.refresh()
        return False


def _translate_clear_history_box(box: QMessageBox) -> None:
    """Chữ của hộp xác nhận “Xóa toàn bộ lịch sử” theo ngôn ngữ hiện tại (dựng và `LanguageChange`)."""
    title = clear_history_title()
    box.setWindowTitle(title)
    box.setAccessibleName(title)
    box.setText(clear_history_question())
    for button in box.buttons():
        role = box.buttonRole(button)
        if role == QMessageBox.ButtonRole.DestructiveRole:
            button.setText(QCoreApplication.translate("Settings", "Xóa lịch sử"))
        elif role == QMessageBox.ButtonRole.RejectRole:
            button.setText(QCoreApplication.translate("Settings", "Không"))


def _translate_cancel_pending_box(box: QMessageBox, cancel_all: bool) -> None:
    """Chữ hộp xác nhận hủy lượt nhận đang chờ theo ngôn ngữ hiện tại."""
    title = cancel_all_pending_title() if cancel_all else cancel_pending_title()
    box.setWindowTitle(title)
    box.setAccessibleName(title)
    box.setText(cancel_all_pending_question() if cancel_all else cancel_pending_question())
    for button in box.buttons():
        role = box.buttonRole(button)
        if role == QMessageBox.ButtonRole.DestructiveRole:
            button.setText(
                QCoreApplication.translate("Settings", "Hủy tất cả")
                if cancel_all
                else QCoreApplication.translate("Settings", "Hủy lượt nhận")
            )
        elif role == QMessageBox.ButtonRole.RejectRole:
            button.setText(QCoreApplication.translate("Settings", "Không"))


def _translate_install_update_box(box: QMessageBox, version: str) -> None:
    """Chữ hộp xác nhận chạy trình cài đặt bản cập nhật theo ngôn ngữ hiện tại."""
    title = install_update_title()
    box.setWindowTitle(title)
    box.setAccessibleName(title)
    box.setText(install_update_question(version))
    for button in box.buttons():
        role = box.buttonRole(button)
        if role == QMessageBox.ButtonRole.AcceptRole:
            button.setText(QCoreApplication.translate("Settings", "Cài đặt ngay"))
        elif role == QMessageBox.ButtonRole.RejectRole:
            button.setText(QCoreApplication.translate("Settings", "Để sau"))


def _apply_theme(mode: theme.Mode, accent: str | None) -> None:
    """Thread Qt: áp chế độ màu và màu nhấn cho ứng dụng (`theme.set_preference`)."""
    app = QApplication.instance()
    if isinstance(app, QApplication):
        theme.set_preference(app, mode, accent)


def _retry_text() -> str:
    return texts.retry()


def _warn(what: str) -> Callable[[object], None]:
    def done(result: object) -> None:
        if isinstance(result, BaseException) and not isinstance(
            result, (RuntimeError, asyncio.CancelledError)
        ):
            log.warning("không %s được: %s", what, type(result).__name__)

    return done
