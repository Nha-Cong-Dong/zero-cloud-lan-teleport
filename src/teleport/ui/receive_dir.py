"""Đổi thư mục nhận từ cửa sổ chính (spec B1b): `ReceiveDirController` và câu chữ của nó.

Thư mục nhận là **chung** cho mọi thiết bị và được lưu (`Node.set_receive_dir` ghi `settings.json`),
nên lần mở app sau dùng lại nó (giao diện chạy node với `ReceiveDirChoice.SAVED`). Thư mục đã lưu
không mở được lúc khởi động thì node vẫn chạy mà **không nhận file** (`Node.receive_dir_error`):
không tự rơi về thư mục mặc định.

`ReceiveDirController` sống trên thread Qt, dựng trong `ui.app.GuiApp`
(`receive_dir_controller`), và chỉ chạm node qua `NodeHost.submit`.

- `attach(host)` (mỗi host mới): hủy việc đổi đang chạy, đóng hộp chọn, quên mọi giá trị.
- `on_started(host)`: lấy ảnh chụp lúc `started` của host (`NodeHost.receive_dir`,
  `NodeHost.receive_dir_error`). Từ đó, trên thread Qt, nguồn sự thật là controller này.
- `choose(parent)`: hộp chọn thư mục không chặn (`open()`, không `exec()`), như “Gửi file…” (hộp của
  hệ điều hành, không `DontUseNativeDialog`), dựng qua seam cấp module `dialog_factory` (đọc lúc
  gọi). Bắt đầu ở thư mục nhận hiện tại, không có thì thư mục đã lưu hỏng, không nữa thì để Qt chọn.
  Đang mở thì chỉ đưa hộp lên trước.
- `choose_initial(parent, current, config_dir)` (lần chạy đầu, `ui.first_run`; spec D1): cùng hộp
  chọn (cùng `dialog_factory`, cùng tiêu đề) khi **chưa có** node; bắt đầu ở `current`. Có host đang
  chạy hay đang đổi → False, không làm gì; hộp đang mở → đưa lên trước. Chọn xong: thư mục dành riêng
  (`is_reserved`) → `failed(RESERVED, câu)`; còn lại → `initial_selected(Path)`. Không gọi node,
  không I/O, không lưu: `ui.app` truyền thư mục đó cho node lần đầu (`NodeOptions.save_args`). Kết
  quả chỉ phát khi hộp vẫn là hộp hiện tại (hộp đã bị `attach` / `close_all` đóng thì bỏ).
- `change(path)`: kiểm thư mục dành riêng (thư mục cấu hình hay nằm trong nó, hoặc có một phần tên
  `.teleport`; so bằng `os.path.normcase(os.path.abspath(...))`, không I/O) thì từ chối ngay với
  `failed(RESERVED)`, không gọi node. Trùng thư mục nhận hiện tại (so `Path`) thì trả True, không
  gọi node và không lưu gì. Còn lại `pending = path` rồi `host.submit(set_receive_dir)`. Đang đổi
  thì không nhận lần đổi khác (trả False); không có nút hủy.
- Kết quả: thành công → `receive_dir` = gốc thật node trả, `unavailable = None`, phát
  `state_changed`. Lỗi → phát `state_changed` (đã xóa `pending`) **rồi** `failed(mã, câu)`: thứ tự
  này để `ui.app` gỡ dải thông báo cũ khi trạng thái ổn trước khi hiện câu lỗi mới. Bảng lỗi:
  `ReceiveDirBusy` → BUSY; `ReceiveDirUnavailable` → UNAVAILABLE (câu nêu thư mục người dùng
  chọn); `SettingsSaveError` → SETTINGS; `ValueError` → UNAVAILABLE (log WARNING);
  `CancelledError`, hay `RuntimeError` khi host không còn chạy → im lặng (log DEBUG); còn lại (kể
  cả RuntimeError khác) → OTHER (log ERROR). Kết quả của việc thuộc host cũ (sau `attach`) bị bỏ.
- `close_all()` (Thoát): đóng hộp chọn, không hủy việc đang chạy (node tự hủy khi dừng).

Log chỉ ghi tên loại lỗi, không bao giờ ghi đường dẫn (mvp N9).

Câu lỗi hiện ở **một** thanh thông báo của luồng chat (`ui.chat`, qua `ui.app`); thông báo khác tới sau có thể
đè lên nó: chấp nhận được vì chân “Lưu vào” vẫn nói trạng thái và còn nút “Đổi…”. Mọi `{path}` qua
`send.printable` rồi `texts.fill`. Chuỗi qua `QCoreApplication.translate("ReceiveDir", ...)` lúc dùng.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtWidgets import QFileDialog, QWidget

from teleport.app.node import ReceiveDirUnavailable
from teleport.app.settings import SettingsSaveError
from teleport.app.transfers import ReceiveDirBusy
from teleport.ui import texts
from teleport.ui.bridge import NodeHost, SubmitJob
from teleport.ui.send import printable

# Seam: dựng hộp chọn thư mục (test thay bằng hàm trả hộp giả). Nhận cha (cửa sổ chính); đọc lúc gọi.
dialog_factory: Callable[[QWidget | None], QFileDialog] = QFileDialog

log = logging.getLogger(__name__)

_RESERVED_PART = ".teleport"  # thư mục tạm ẩn của Teleport trong thư mục nhận


class ChangeError(StrEnum):
    """Mã lỗi khi đổi thư mục nhận (`ReceiveDirController.failed`)."""

    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    SETTINGS = "settings"
    RESERVED = "reserved"
    OTHER = "other"


def _path_text(path: Path | None) -> str:
    return "" if path is None else printable(str(path))


def change_error_text(code: ChangeError | str, path: Path | None) -> str:
    """Câu lỗi khi đổi thư mục nhận (mvp mục 8). `path`: thư mục người dùng chọn; mã lạ → OTHER."""
    t = QCoreApplication.translate
    if code == ChangeError.BUSY:
        return t(
            "ReceiveDir",
            "Chưa đổi được thư mục nhận vì đang nhận file. Hãy đổi lại sau khi các lượt nhận xong.",
        )
    if code == ChangeError.UNAVAILABLE:
        return texts.fill(
            t("ReceiveDir", "Không dùng được thư mục {path}. Hãy chọn thư mục khác mà bạn có quyền ghi."),
            path=_path_text(path),
        )
    if code == ChangeError.SETTINGS:
        return t(
            "ReceiveDir",
            "Chưa đổi được thư mục nhận vì không ghi được cài đặt của Teleport. "
            "Hãy kiểm tra quyền ghi vào thư mục cấu hình rồi thử lại.",
        )
    if code == ChangeError.RESERVED:
        return texts.fill(
            t(
                "ReceiveDir",
                "Không dùng được thư mục {path} để nhận file vì Teleport dùng nó cho việc riêng. "
                "Hãy chọn thư mục khác.",
            ),
            path=_path_text(path),
        )
    return t("ReceiveDir", "Chưa đổi được thư mục nhận vì một lỗi không mong đợi. Hãy thử lại.")


def unavailable_text(path: Path) -> str:
    """Dải thông báo lúc `started` khi thư mục nhận đã lưu không mở được."""
    return texts.fill(
        QCoreApplication.translate(
            "ReceiveDir",
            "Teleport đang không nhận file vì không mở được thư mục nhận {path}. Hãy kiểm tra ổ đĩa "
            "và quyền ghi rồi chọn lại thư mục đó, hoặc chọn thư mục khác.",
        ),
        path=_path_text(path),
    )


def unavailable_tray_text() -> str:
    """Bóng thoại khay khi chạy nền mà thư mục nhận đã lưu không mở được (không có đường dẫn)."""
    return QCoreApplication.translate(
        "ReceiveDir",
        "Teleport đang không nhận file vì không mở được thư mục nhận. Mở Teleport để chọn thư mục khác.",
    )


def choose_title() -> str:
    """Tiêu đề hộp chọn thư mục nhận."""
    return QCoreApplication.translate("ReceiveDir", "Chọn thư mục nhận file")


def choose_action_text() -> str:
    """Nút của dải thông báo lúc `started` khi thư mục đã lưu hỏng."""
    return QCoreApplication.translate("ReceiveDir", "Chọn thư mục nhận")


def choose_other_text() -> str:
    """Nút của dải thông báo khi thư mục vừa chọn không dùng được (UNAVAILABLE, RESERVED)."""
    return QCoreApplication.translate("ReceiveDir", "Chọn thư mục khác")


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def is_reserved(path: Path, config_dir: Path) -> bool:
    """`path` trùng hay nằm trong `config_dir`, hoặc có một phần tên `.teleport` (không I/O).

    Chỉ so chuỗi: tên ngắn 8.3, junction hay symlink trỏ vào thư mục cấu hình vẫn lọt; chặn chắc
    chắn (so gốc thật) là việc của tầng `app`.
    """
    norm = _norm(path)
    config = _norm(config_dir)
    if norm == config or norm.startswith(config.rstrip(os.sep) + os.sep):
        return True
    reserved = os.path.normcase(_RESERVED_PART)
    return any(part == reserved for part in Path(norm).parts)


class ReceiveDirController(QObject):
    """Thư mục nhận trên thread Qt: ảnh chụp lúc `started`, hộp chọn, việc đổi (xem docstring module)."""

    state_changed = Signal()  # receive_dir / unavailable / pending đổi
    failed = Signal(str, str)  # (mã ChangeError, câu đã dịch)
    initial_selected = Signal(object)  # Path: thư mục chọn ở lần chạy đầu (`choose_initial`)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._host: NodeHost | None = None
        self._receive_dir: Path | None = None
        self._unavailable: Path | None = None
        self._pending: Path | None = None
        self._job: SubmitJob | None = None
        self._token: object | None = None  # việc đổi đang chạy; kết quả mang token khác thì bỏ
        self._dialog: QFileDialog | None = None
        self._failed_path: Path | None = None  # thư mục của lần `failed` gần nhất

    # ---- Trạng thái ----------------------------------------------------------------------------

    @property
    def host(self) -> NodeHost | None:
        return self._host

    @property
    def receive_dir(self) -> Path | None:
        """Thư mục nhận đang dùng (gốc thật); None khi chưa biết hay node không nhận file."""
        return self._receive_dir

    @property
    def failed_path(self) -> Path | None:
        """Thư mục người dùng chọn ở lần `failed` gần nhất (đặt trước khi phát), để bên nhận dựng lại
        câu lỗi theo ngôn ngữ mới bằng `change_error_text(mã, failed_path)`."""
        return self._failed_path

    @property
    def unavailable(self) -> Path | None:
        """Thư mục nhận đã lưu không mở được lúc khởi động (None khi không lỗi hay đã đổi xong)."""
        return self._unavailable

    @property
    def pending(self) -> Path | None:
        """Thư mục đang đổi sang (chờ node trả lời), hay None."""
        return self._pending

    @property
    def changing(self) -> bool:
        """Đang chờ kết quả một lần đổi."""
        return self._token is not None

    @property
    def dialog(self) -> QFileDialog | None:
        """Hộp chọn thư mục đang mở."""
        return self._dialog

    # ---- Vòng đời host -------------------------------------------------------------------------

    def attach(self, host: NodeHost | None) -> None:
        """Dùng `host` mới: hủy việc đổi đang chạy, đóng hộp chọn, quên mọi giá trị."""
        job, self._job, self._token = self._job, None, None
        if job is not None:
            job.cancel()
        self._close_dialog()
        self._host = host
        self._receive_dir = self._unavailable = self._pending = None
        self.state_changed.emit()

    def on_started(self, host: NodeHost) -> None:
        """Node của `host` đã chạy: lấy thư mục nhận và thư mục đã lưu hỏng (ảnh chụp lúc `started`)."""
        if host is not self._host:
            return
        self._receive_dir = host.receive_dir
        self._unavailable = host.receive_dir_error
        self.state_changed.emit()

    def close_all(self) -> None:
        """App đang thoát: đóng hộp chọn; việc đổi đang chạy (nếu có) để node tự hủy khi dừng."""
        self._close_dialog()

    # ---- Chọn, đổi -----------------------------------------------------------------------------

    def choose(self, parent: QWidget | None) -> bool:
        """Mở hộp chọn thư mục (không chặn); False khi chưa có host đang chạy hay đang đổi."""
        host = self._host
        if host is None or not host.running or self.changing:
            return False
        self._open_dialog(parent, self._receive_dir or self._unavailable, self._on_selected)
        return True

    def choose_initial(self, parent: QWidget | None, current: Path | None, config_dir: Path) -> bool:
        """Lần chạy đầu: mở hộp chọn thư mục khi chưa có node (không chặn, không I/O, không lưu).

        False khi đã có host đang chạy hay đang đổi. Chọn xong phát `initial_selected(Path)`, hay
        `failed(RESERVED)` khi là thư mục dành riêng của `config_dir`."""
        host = self._host
        if (host is not None and host.running) or self.changing:
            return False
        self._open_dialog(parent, current, functools.partial(self._on_initial_selected, Path(config_dir)))
        return True

    def _open_dialog(
        self,
        parent: QWidget | None,
        start: Path | None,
        on_selected: Callable[[QFileDialog, str], None],
    ) -> None:
        """Dựng và mở hộp chọn thư mục (`open()`); đang mở thì chỉ đưa hộp lên trước."""
        dialog = self._dialog
        if dialog is not None:
            dialog.raise_()
            dialog.activateWindow()
            return
        dialog = dialog_factory(parent)
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        dialog.setWindowTitle(choose_title())
        if start is not None:
            dialog.setDirectory(str(start))
        dialog.fileSelected.connect(functools.partial(on_selected, dialog))
        dialog.finished.connect(functools.partial(self._on_dialog_finished, dialog))
        self._dialog = dialog
        dialog.open()

    def change(self, path: Path) -> bool:
        """Đổi thư mục nhận sang `path` (không chặn); False khi không có host đang chạy, đang đổi,
        hay `path` là thư mục dành riêng (phát `failed(RESERVED)`). Trùng thư mục hiện tại → True,
        không gọi node, không lưu."""
        host = self._host
        if host is None or not host.running or self.changing:
            return False
        path = Path(path)
        if is_reserved(path, host.options.config_dir):
            self._failed_path = path
            self.failed.emit(ChangeError.RESERVED.value, change_error_text(ChangeError.RESERVED, path))
            return False
        if self._receive_dir is not None and path == self._receive_dir:
            return True
        token = object()
        self._token = token
        self._pending = path
        self.state_changed.emit()
        self._job = host.submit(
            lambda node: node.set_receive_dir(path),
            functools.partial(self._on_done, host, token, path),
        )
        return True

    # ---- Bên trong -----------------------------------------------------------------------------

    def _on_selected(self, dialog: QFileDialog, selected: str) -> None:
        if dialog is not self._dialog:
            return  # hộp cũ (đã bị `attach` / `close_all` đóng) phát muộn: không đổi trên host mới
        if selected:
            self.change(Path(selected))

    def _on_initial_selected(self, config_dir: Path, dialog: QFileDialog, selected: str) -> None:
        if dialog is not self._dialog or not selected:
            return  # hộp cũ (đã bị `attach` / `close_all` đóng) phát muộn, hay không chọn gì
        path = Path(selected)
        if is_reserved(path, config_dir):
            self._failed_path = path
            self.failed.emit(ChangeError.RESERVED.value, change_error_text(ChangeError.RESERVED, path))
            return
        self.initial_selected.emit(path)

    def _on_dialog_finished(self, dialog: QFileDialog, _result: int) -> None:
        if self._dialog is dialog:
            self._dialog = None
        dialog.deleteLater()

    def _close_dialog(self) -> None:
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()

    def _on_done(self, host: NodeHost, token: object, path: Path, result: object) -> None:
        if host is not self._host or token is not self._token:
            return  # việc của host cũ hay đã bị bỏ (`attach`)
        self._token = None
        self._job = None
        self._pending = None
        if isinstance(result, Path):
            self._receive_dir = result
            self._unavailable = None
            self.state_changed.emit()
            return
        code = self._classify(host, result)
        self.state_changed.emit()
        if code is not None:
            self._failed_path = path
            self.failed.emit(code.value, change_error_text(code, path))

    @staticmethod
    def _classify(host: NodeHost, result: object) -> ChangeError | None:
        """Mã lỗi của kết quả; None → im lặng. Chỉ ghi tên loại lỗi (mvp N9)."""
        name = type(result).__name__
        if isinstance(result, asyncio.CancelledError) or (
            isinstance(result, RuntimeError) and not host.running
        ):
            log.debug("đổi thư mục nhận bị bỏ: %s", name)
            return None
        if isinstance(result, ReceiveDirBusy):
            return ChangeError.BUSY
        if isinstance(result, ReceiveDirUnavailable):
            return ChangeError.UNAVAILABLE
        if isinstance(result, SettingsSaveError):
            return ChangeError.SETTINGS
        if isinstance(result, ValueError):
            log.warning("không đổi được thư mục nhận: %s", name)
            return ChangeError.UNAVAILABLE
        log.error("không đổi được thư mục nhận: %s", name)
        return ChangeError.OTHER
