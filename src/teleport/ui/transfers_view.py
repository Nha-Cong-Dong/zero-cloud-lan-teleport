"""Danh sách lượt truyền trong phiên (mvp F4, F5, mục 7.3 “Tiến độ”, mục 8); không lưu ra đĩa.

`TransfersView` nhận mọi `TransferUpdate` qua `handle` (7 loại) và ảnh chụp `transfers()` lúc
`started` qua `load_snapshot`; mỗi `TransferKey` một thẻ (`TransferRow`), thêm theo thứ tự tới.
Ảnh chụp gộp với thẻ đã có (`TransferResumed` có thể tới trước `started`, spec-hackathon mục 9.7):
thẻ đã có giữ nguyên, không nhân đôi.

Thẻ (kiểu AirDrop / LocalSend): dòng 1 tên file (hay “n file”) kèm cỡ và máy kia (“· tới X” /
“· từ X”), nút “Mở thư mục” / “Hủy”; dòng 2 thanh tiến độ thang 0–1000 (`shown * 1000 // total`,
không tràn int32 với file tới 2⁴², spec-hackathon D8); dòng 3 biểu tượng của style (không tải gì)
+ nhãn trạng thái ngắn (`status_label`: “Đang gửi”, “Đang chờ”, “Xong ✓”, “Lỗi”…) + dòng phụ chữ
nhỏ (`detail_label`): tiến độ “42,0% · 85,2 MB/s · còn 1 phút”, câu chờ cụ thể (“Đang chờ X trả
lời…”), “Đã kiểm tra toàn vẹn” khi xong, hay câu mvp mục 8 (chuyện gì xảy ra + làm gì tiếp) khi
lỗi. Thông tin không chỉ nằm ở màu hay biểu tượng: luôn có chữ (mvp N12); màu theo palette
(`ui/theme.py`), không mã màu cứng.

**Tiến độ nói đúng sự thật** (spec-hackathon PR-1): mỗi thẻ giữ `shown_bytes`, chỉ tăng, ≤ tổng:
`TransferProgress.shown_bytes`, còn ảnh chụp / `TransferStatusChanged` / `TransferResumed` góp
`done_bytes + unsynced_bytes`. Thanh và % đều theo nó, nên thẻ chưa kết thúc không bao giờ lùi (nối
lại xóa khối chưa sync, sang file mới); xong (`DONE`) thì thanh đầy. % làm tròn xuống và chặn ở
99,9% tới khi xong. Dòng phụ theo trạng thái: TRANSFERRING “x% · tốc độ · còn …” (tốc độ 0 thì bỏ
phần “/s”, chỉ “đang tính thời gian còn lại”), nhưng `idle_seconds` ≥ `STALL_TEXT_AFTER` thì câu
đứng yên: bên nhận “x% · Đang chờ dữ liệu…”, bên gửi “x% · Đang chờ X phản hồi…” (bên gửi không có
VERIFYING, máy nhận đọc lại file lớn thì thẻ gửi đứng yên); app vẫn phát tiến độ khi đứng yên, nên
câu tốc độ cũ không đọng lại. RECONNECTING “x% · Đang chờ X nối lại…”, không tốc độ, không thời gian
còn lại; VERIFYING (bên nhận đọc lại / đổi tên) “Đang kiểm tra video.mp4…” như CLI (mvp mục 8), tiến
độ tới muộn không ghi đè. Mỗi nhịp tiến độ chỉ `setText` dòng phụ; lúc đổi loại câu (đứng yên ↔ tốc
độ) mới vẽ lại cả trạng thái và accessibleDescription.

Câu tường lửa (`DataUnreachable`) và câu lượt nhận được khôi phục (`TransferResumed`) giữ tới khi dữ
liệu thật sự chảy: `shown_bytes` của thẻ tăng hay lượt về TRANSFERRING. Tiến độ RECONNECTING đứng yên
(app phát mỗi nhịp) không xóa chúng. `TransferStatusChanged` cũng vậy: engine phát `DataUnreachable`
trước `StatusChanged(RECONNECTING)` của cùng lần thất bại, nên trạng thái mới RECONNECTING giữ câu
tường lửa (trạng thái khác thì bỏ); câu lượt khôi phục giữ qua RECONNECTING / WAITING / QUEUED, chỉ
bỏ khi trạng thái mới là TRANSFERRING / VERIFYING.

Nút “Hủy” phát `cancel_requested(key)`; “Mở thư mục” (bên nhận, khi đã xong và có `FileDone.path`)
mở thư mục cha bằng seam `open_url`, không `subprocess`. `TransferProgress` chỉ `setValue` /
`setText` tại chỗ, không dựng lại widget. Thẻ đã kết thúc không đổi nữa (bỏ sự kiện tới muộn).
`TransferFinished` với `SUSPENDED` (chỉ khi host dừng) bị bỏ qua: thẻ giữ như cũ, host mới thì
`clear()`.

Chữ do máy kia đặt (tên máy, tên file) qua `texts.peer_label` / `texts.file_label`; mọi nhãn đặt
`Qt.PlainText`, chữ máy kia không vào tooltip (chỉ vào accessibleName/Description cho trình đọc
màn hình). Tên máy lấy từ `set_device_names` (`devices()`), không có thì tên trong
`OfferReceived`, không có nữa thì 8 ký tự đầu mã máy. Chuỗi qua
`QCoreApplication.translate("Transfers", ...)` lúc dùng; đổi ngôn ngữ vẽ lại chữ mọi thẻ.

**Thông báo** (`show_notice(text, action_text=None, on_action=None)`): một dải chữ thuần ở đầu
danh sách (trên các thẻ) kèm nút “Đóng”, và khi có thì một nút hành động (“Ghép đôi lại”, “Hủy ghép
đôi”; bấm thì ẩn dải rồi gọi `on_action`). Bố cục hai hàng (cột lượt hẹp, câu không bị ép thành
cột chữ): hàng trên biểu tượng thông tin 16 px của style + câu chiếm hết bề ngang, xuống dòng tự
nhiên khi dài; hàng dưới các nút căn phải. Thứ tự Tab: câu → nút hành động → “Đóng”. Thông báo mới
thay thông báo cũ (kể cả nút), không hộp thoại chặn. Dùng cho lỗi khi chọn gửi (mvp mục 8, các
dòng “Chọn gửi…”, “Giao diện: gửi tới…”) và gợi ý kéo thả. Chữ có thể chứa tên máy kia (đã qua
`peer_label`): chỉ vào nhãn PlainText và accessibleDescription, không vào tooltip. Seam test:
`notice_text()`, `notice_action_text()`.

Seam cho test (biến cấp module, đọc lúc gọi): `open_url`.
"""

from __future__ import annotations

import math
import ntpath
import sys
from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from teleport.app.transfers import (
    DataUnreachable,
    Direction,
    FileDone,
    OfferReceived,
    TransferFinished,
    TransferInfo,
    TransferKey,
    TransferProgress,
    TransferResult,
    TransferResumed,
    TransferStatus,
    TransferStatusChanged,
    TransferUpdate,
)
from teleport.core.display import format_duration, format_size
from teleport.ui import texts
from teleport.ui.send import printable
from teleport.ui.texts import fill as _fill

# Seam: mở thư mục bằng ứng dụng mặc định của hệ điều hành (test thay bằng hàm ghi lại URL).
open_url = QDesktopServices.openUrl

BAR_MAX = 1000  # thang của thanh tiến độ
STALL_TEXT_AFTER = 5.0  # giây không có byte mới (`idle_seconds`) thì dòng phụ nói “Đang chờ dữ liệu…”

_REJECTED = frozenset(
    {
        TransferResult.REJECTED_USER,
        TransferResult.REJECTED_NO_SPACE,
        TransferResult.REJECTED_FILE_TOO_LARGE,
        TransferResult.REJECTED_BUSY,
        TransferResult.REJECTED_OTHER,
    }
)
# Bên nhận: máy này tự từ chối (CLI chỉ báo qua JSON); thẻ chỉ có nhãn ngắn “Đã từ chối”.
_RECEIVE_SILENT = frozenset(
    {
        TransferResult.REJECTED_BUSY,
        TransferResult.REJECTED_OTHER,
        TransferResult.REJECTED_NOT_RECEIVING,
    }
)
_WAITING_STATUSES = frozenset({TransferStatus.QUEUED, TransferStatus.WAITING})
# Trạng thái mà `TransferStatusChanged` tới là dữ liệu đã chảy lại: bỏ câu lượt khôi phục.
_FLOWING_STATUSES = frozenset({TransferStatus.TRANSFERRING, TransferStatus.VERIFYING})


def bar_value(done: int, total: int) -> int:
    """Giá trị thanh tiến độ 0…`BAR_MAX`; `total` ≤ 0 → 0."""
    if total <= 0:
        return 0
    return min(BAR_MAX, max(0, done) * BAR_MAX // total)


def percent_text(shown: int, total: int) -> str:
    """“42,0%”: một chữ số thập phân, làm tròn xuống, tối đa 99,9% (chỉ thẻ đã xong mới đầy)."""
    tenths = min(bar_value(shown, total), BAR_MAX - 1)
    return f"{tenths // 10},{tenths % 10}%"


def _file_at(info: TransferInfo, index: int | None) -> str:
    """Tên file thứ `index` (None: `info.file_index`), kẹp trong khoảng hợp lệ, qua `file_label`."""
    if not info.names:
        return "?"
    i = info.file_index if index is None else index
    return texts.file_label(info.names[min(max(i, 0), len(info.names) - 1)])


def _drive_label(receive_dir: Path | None) -> str:
    """Cách gọi ổ chứa thư mục nhận (mvp mục 8), như `cli._drive_label`; không chạm đĩa."""
    t = QCoreApplication.translate
    if receive_dir is None:
        return t("Transfers", "ổ lưu file")
    text = str(receive_dir)
    if sys.platform == "win32":
        if text.startswith("\\\\?\\UNC\\"):
            text = "\\\\" + text[len("\\\\?\\UNC\\") :]
        elif text.startswith("\\\\?\\"):
            text = text[len("\\\\?\\") :]
        drive = ntpath.splitdrive(text)[0]
        if drive.startswith(("\\\\", "//")):
            return _fill(t("Transfers", "ổ mạng {drive}"), drive=printable(drive))
        if drive:
            return _fill(t("Transfers", "ổ {drive}"), drive=printable(drive.upper()))
    return _fill(t("Transfers", "ổ chứa {path}"), path=printable(text))


def _dir_label(receive_dir: Path | None) -> str:
    if receive_dir is None:
        return QCoreApplication.translate("Transfers", "thư mục nhận")
    return printable(str(receive_dir))


class Kind(StrEnum):
    """Loại trạng thái của một thẻ: chọn biểu tượng (luôn kèm chữ, không chỉ bằng màu, mvp N12)."""

    ACTIVE = "active"  # đang gửi / nhận / kiểm tra
    WAITING = "waiting"
    RECONNECTING = "reconnecting"
    DONE = "done"
    CANCELLED = "cancelled"  # đã hủy, đã từ chối
    ERROR = "error"


def short_status(direction: Direction, status: TransferStatus) -> str:
    """Nhãn trạng thái ngắn của lượt chưa kết thúc (mvp F5): một từ hay cụm ngắn."""
    t = QCoreApplication.translate
    if status is TransferStatus.TRANSFERRING:
        return t("Transfers", "Đang gửi") if direction is Direction.SEND else t("Transfers", "Đang nhận")
    if status is TransferStatus.VERIFYING:
        return t("Transfers", "Đang kiểm tra")
    if status is TransferStatus.RECONNECTING:
        return t("Transfers", "Đang nối lại…")
    return t("Transfers", "Đang chờ")  # QUEUED, WAITING


def status_kind(status: TransferStatus) -> Kind:
    if status in (TransferStatus.TRANSFERRING, TransferStatus.VERIFYING):
        return Kind.ACTIVE
    if status is TransferStatus.RECONNECTING:
        return Kind.RECONNECTING
    return Kind.WAITING


def waiting_detail(
    direction: Direction, status: TransferStatus, peer: str, *, connected: bool = True
) -> str | None:
    """Dòng phụ cụ thể khi lượt đang chờ (mvp F5, mục 8); không chờ → None. `peer` đã qua `peer_label`."""
    t = QCoreApplication.translate
    send = direction is Direction.SEND
    if status is TransferStatus.QUEUED:
        if send:
            return _fill(t("Transfers", "Đang chờ {name} nhận xong lượt trước…"), name=peer)
        return t("Transfers", "Đang xếp hàng")
    if status is TransferStatus.WAITING:
        if not send:
            return t("Transfers", "Đang chờ bạn trả lời")
        if not connected:
            return _fill(t("Transfers", "Đang chờ kết nối tới {name}…"), name=peer)
        return _fill(t("Transfers", "Đang chờ {name} trả lời…"), name=peer)
    return None


_CANCELLED_RESULTS = frozenset(
    {
        TransferResult.CANCELLED_USER,
        TransferResult.CANCELLED_OTHER,
        TransferResult.UNKNOWN_TRANSFER,
        TransferResult.UNPAIRED,
    }
)


def result_kind(result: TransferResult) -> Kind:
    if result is TransferResult.DONE:
        return Kind.DONE
    if result in _CANCELLED_RESULTS or result in _REJECTED or result is TransferResult.REJECTED_NOT_RECEIVING:
        return Kind.CANCELLED
    return Kind.ERROR


def result_status(result: TransferResult) -> str:
    """Nhãn trạng thái ngắn của lượt đã kết thúc."""
    t = QCoreApplication.translate
    if result is TransferResult.DONE:
        return t("Transfers", "Xong ✓")
    if result in _REJECTED or result is TransferResult.REJECTED_NOT_RECEIVING:
        return t("Transfers", "Đã từ chối")
    if result in _CANCELLED_RESULTS:
        return t("Transfers", "Đã hủy")
    return t("Transfers", "Lỗi")


def reconnecting_text(percent: str, peer: str) -> str:
    """Dòng phụ khi RECONNECTING: “42,0% · Đang chờ X nối lại…”; không tốc độ, không thời gian còn lại."""
    return _fill(
        QCoreApplication.translate("Transfers", "{percent} · Đang chờ {name} nối lại…"),
        percent=percent,
        name=peer,
    )


def stalled_text(percent: str, direction: Direction, peer: str) -> str:
    """Dòng phụ khi đang truyền mà đã `STALL_TEXT_AFTER` giây không có byte mới (mvp mục 8).

    Bên nhận: “Đang chờ dữ liệu…”. Bên gửi không có VERIFYING, nên lúc máy nhận đọc lại file lớn
    thẻ gửi đứng yên: nói đang chờ máy kia phản hồi. `peer` đã qua `peer_label`.
    """
    t = QCoreApplication.translate
    if direction is Direction.SEND:
        return _fill(t("Transfers", "{percent} · Đang chờ {name} phản hồi…"), percent=percent, name=peer)
    return _fill(t("Transfers", "{percent} · Đang chờ dữ liệu…"), percent=percent)


def verifying_text(info: TransferInfo | None) -> str:
    """Dòng phụ khi VERIFYING (bên nhận đọc lại / đổi tên file): “Đang kiểm tra video.mp4…” (mvp mục 8)."""
    if info is None or not info.names:
        return ""
    return _fill(QCoreApplication.translate("Transfers", "Đang kiểm tra {file}…"), file=_file_at(info, None))


def resumed_text(peer: str) -> str:
    """Lượt nhận dở được khôi phục khi khởi động, đang chờ máy gửi nối lại."""
    return _fill(QCoreApplication.translate("Transfers", "Đang chờ {name} nối lại…"), name=peer)


def unreachable_text(peer: str) -> str:
    """mvp mục 8: không mở được kết nối truyền dữ liệu (`DataUnreachable`)."""
    return _fill(
        QCoreApplication.translate(
            "Transfers",
            "Không mở được kết nối truyền dữ liệu tới {name}; tường lửa trên máy đó có thể đang chặn "
            "Teleport. Teleport vẫn đang thử lại.",
        ),
        name=peer,
    )


def done_text() -> str:
    """Dòng phụ của thẻ đã xong."""
    return QCoreApplication.translate("Transfers", "Đã kiểm tra toàn vẹn")


def result_message(finished: TransferFinished, peer: str, receive_dir: Path | None = None) -> str:
    """Dòng phụ của thẻ đã kết thúc: câu mvp mục 8, port từ `cli.transfer_result_message`.

    `peer` đã qua `peer_label`. `DONE` → `done_text()`. Máy này tự từ chối (bên nhận) → "" (nhãn
    ngắn “Đã từ chối” đã đủ). `receive_dir`: thư mục nhận (câu lỗi ghi đĩa, câu từ chối vì ổ);
    None → “thư mục nhận” / “ổ lưu file”.
    """
    if finished.result is TransferResult.DONE:
        return done_text()
    if finished.info.key.direction is Direction.SEND:
        return _send_result(finished, peer)
    return _receive_result(finished, peer, receive_dir)


def _send_result(finished: TransferFinished, p: str) -> str:
    t = QCoreApplication.translate
    info, result, reject = finished.info, finished.result, finished.reject
    if (
        result is TransferResult.REJECTED_NO_SPACE
        and reject is not None
        and reject.needed is not None
        and reject.available is not None
    ):
        return _fill(
            t(
                "Transfers",
                "{name} không đủ chỗ trống để nhận: cần {needed}, ổ lưu file trên máy đó còn {available}.",
            ),
            name=p,
            needed=format_size(reject.needed),
            available=format_size(reject.available),
        )
    if result is TransferResult.REJECTED_FILE_TOO_LARGE and reject is not None:
        index, max_size = reject.file_index, reject.max_size
        if index is not None and max_size is not None and 0 <= index < len(info.files):
            return _fill(
                t(
                    "Transfers",
                    "{name} không nhận được {file} ({size}): ổ lưu file trên máy đó chỉ chứa được "
                    "file nhỏ hơn {limit}.",
                ),
                name=p,
                file=_file_at(info, index),
                size=format_size(info.files[index].size),
                limit=format_size(max_size + 1, trim=True),
            )
    if result is TransferResult.REJECTED_NOT_RECEIVING:
        return _fill(
            t(
                "Transfers",
                "{name} chưa bật nhận file. Trên máy đó hãy chạy “teleport receive” rồi gửi lại.",
            ),
            name=p,
        )
    if result in _REJECTED:
        return _fill(t("Transfers", "{name} đã từ chối nhận file."), name=p)
    if result is TransferResult.CANCELLED_USER:
        if finished.by_peer:
            return _fill(
                t(
                    "Transfers",
                    "{name} đã dừng nhận giữa chừng. Các file đã gửi xong vẫn nằm trên máy đó.",
                ),
                name=p,
            )
        return _fill(
            t("Transfers", "Đã hủy lượt gửi tới {name}. Các file đã gửi xong vẫn nằm trên máy đó."),
            name=p,
        )
    file = _file_at(info, finished.file_index)
    if result is TransferResult.SOURCE_CHANGED:
        return _fill(
            t("Transfers", "{file} đã bị thay đổi trên máy gửi nên không thể gửi tiếp. Hãy gửi lại."),
            file=file,
        )
    if result is TransferResult.EXPIRED:
        return _fill(
            t("Transfers", "Mất kết nối với {name} quá lâu nên lượt gửi đã dừng. Hãy gửi lại."), name=p
        )
    if result is TransferResult.SOURCE_READ_FAILED:
        return _fill(
            t(
                "Transfers",
                "Không đọc được {file} trên máy này nữa (file có thể đang bị chương trình khác khóa, "
                "hoặc nằm trên ổ vừa bị rút ra). Đã dừng gửi tới {name}.",
            ),
            file=file,
            name=p,
        )
    if result is TransferResult.PEER_ERROR:
        return _fill(
            t(
                "Transfers",
                "{name} gặp lỗi khi nhận nên đã dừng lượt gửi. Hãy xem thông báo trên máy đó rồi gửi lại.",
            ),
            name=p,
        )
    if result is TransferResult.PROTOCOL_ERROR:
        return _protocol_error(p)
    return _stopped(p)  # UNKNOWN_TRANSFER, CANCELLED_OTHER, UNPAIRED, CORRUPT…


def _protocol_error(p: str) -> str:
    return _fill(
        QCoreApplication.translate(
            "Transfers",
            "Lượt gửi với {name} đã dừng vì máy đó gửi dữ liệu không hợp lệ. "
            "Hãy cập nhật Teleport trên cả hai máy rồi thử lại.",
        ),
        name=p,
    )


def _stopped(p: str) -> str:
    return _fill(QCoreApplication.translate("Transfers", "{name} đã dừng lượt gửi."), name=p)


def _kept() -> str:
    return QCoreApplication.translate("Transfers", "Các file đã nhận xong vẫn giữ nguyên; hãy nhờ gửi lại.")


def _receive_result(finished: TransferFinished, s: str, receive_dir: Path | None) -> str:
    t = QCoreApplication.translate
    info, result, reject = finished.info, finished.result, finished.reject
    n = len(info.files)
    if (result is TransferResult.REJECTED_USER and not finished.by_peer) or result in _RECEIVE_SILENT:
        return ""
    if (
        result is TransferResult.REJECTED_NO_SPACE
        and reject is not None
        and reject.needed is not None
        and reject.available is not None
    ):
        return _fill(
            t(
                "Transfers",
                "Đã từ chối %n file từ {name}: không đủ chỗ trống, cần {needed}, {drive} còn {available}.",
                None,
                n,
            ),
            name=s,
            needed=format_size(reject.needed),
            drive=_drive_label(receive_dir),
            available=format_size(reject.available),
        )
    if result is TransferResult.REJECTED_FILE_TOO_LARGE and reject is not None:
        index, max_size = reject.file_index, reject.max_size
        if index is not None and max_size is not None and 0 <= index < n:
            return _fill(
                t(
                    "Transfers",
                    "Đã từ chối %n file từ {name}: {drive} chỉ chứa được file nhỏ hơn {limit}, "
                    "mà {file} nặng {size}.",
                    None,
                    n,
                ),
                name=s,
                drive=_drive_label(receive_dir),
                limit=format_size(max_size + 1, trim=True),
                file=_file_at(info, index),
                size=format_size(info.files[index].size),
            )
    if result is TransferResult.CANCELLED_USER:
        if not finished.by_peer:
            return _fill(t("Transfers", "Đã hủy nhận từ {name}. File đã nhận xong vẫn giữ nguyên."), name=s)
        if info.status in (TransferStatus.WAITING, TransferStatus.QUEUED):
            return _fill(t("Transfers", "{name} đã hủy lượt gửi."), name=s)
        return _fill(
            t(
                "Transfers",
                "{name} đã hủy lượt gửi. Các file chưa nhận xong đã được xóa; "
                "file đã nhận xong vẫn giữ nguyên.",
            ),
            name=s,
        )
    file = _file_at(info, finished.file_index)
    if result is TransferResult.SOURCE_CHANGED:
        return _fill(
            t("Transfers", "{file} đã bị thay đổi trên {name} nên không nhận tiếp được. {kept}"),
            file=file,
            name=s,
            kept=_kept(),
        )
    if result is TransferResult.EXPIRED:
        return _fill(
            t(
                "Transfers",
                "Mất kết nối với {name} quá lâu nên đã dừng nhận. "
                "Các file chưa nhận xong đã được xóa; file đã nhận xong vẫn giữ nguyên.",
            ),
            name=s,
        )
    if result is TransferResult.DISK_FAILED:
        return _fill(
            t(
                "Transfers",
                "Không lưu được {file} vào {dir} vì ổ đĩa báo lỗi khi ghi "
                "(ổ có thể đã đầy, bị rút ra hoặc bị hỏng). Đã dừng nhận từ {name}; "
                "các file đã nhận xong vẫn giữ nguyên.",
            ),
            file=file,
            dir=_dir_label(receive_dir),
            name=s,
        )
    if result is TransferResult.RECEIVE_DIR_MISSING:
        return _fill(
            t(
                "Transfers",
                "Không nhận được lượt gửi từ {name} vì thư mục nhận {dir} không còn hoặc không dùng "
                "được, và Teleport không tạo lại được (có thể thư mục chứa nó đã bị xóa, hoặc ổ đã bị "
                "rút ra). "
                "Hãy kiểm tra lại rồi nhờ gửi lại.",
            ),
            name=s,
            dir=_dir_label(receive_dir),
        )
    if result is TransferResult.TEMP_EXISTS:
        return _fill(
            t(
                "Transfers",
                "Không nhận được lượt gửi từ {name} vì thư mục tạm của lượt này đã có sẵn mà Teleport "
                "không dùng tiếp được. Hãy nhờ gửi lại.",
            ),
            name=s,
        )
    if result is TransferResult.CORRUPT:
        return _fill(
            t(
                "Transfers",
                "Dữ liệu nhận từ {name} bị hỏng nhiều lần liên tiếp nên đã dừng nhận {file}. {kept}",
            ),
            name=s,
            file=file,
            kept=_kept(),
        )
    if result is TransferResult.PEER_ERROR:
        return _fill(
            t("Transfers", "{name} đã dừng gửi {file} vì gặp lỗi trên máy đó. {kept}"),
            name=s,
            file=file,
            kept=_kept(),
        )
    if result is TransferResult.UNKNOWN_TRANSFER:
        return _fill(t("Transfers", "{name} đã hủy lượt gửi {file}. Hãy nhờ gửi lại."), name=s, file=file)
    if result is TransferResult.PROTOCOL_ERROR:
        return _protocol_error(s)
    return _stopped(s)  # CANCELLED_OTHER, UNPAIRED, REJECTED_* do máy gửi báo…


def progress_text(update: TransferProgress, shown: int | None = None) -> str:
    """Dòng phụ khi đang truyền: “42,0% · 85,2 MB/s · còn 1 phút 2 giây”.

    % theo `shown` (None: `update.shown_bytes`) qua `percent_text`. Chưa đo được tốc độ (`eta`
    None) thì “đang tính thời gian còn lại”; tốc độ 0 thì bỏ hẳn phần “/s” (không hiện “0 byte/s”).
    """
    info = update.info
    percent = percent_text(update.shown_bytes if shown is None else shown, info.total_bytes)
    speed = update.bytes_per_second
    speed_bytes = int(speed) if math.isfinite(speed) and speed > 0 else 0
    eta = update.eta
    if eta is None or not math.isfinite(eta) or speed_bytes == 0:
        remaining = QCoreApplication.translate("Transfers", "đang tính thời gian còn lại")
    else:
        remaining = _fill(QCoreApplication.translate("Transfers", "còn {time}"), time=format_duration(eta))
    if speed_bytes == 0:
        return " · ".join((percent, remaining))
    return " · ".join((percent, f"{format_size(speed_bytes)}/s", remaining))


_ICONS: dict[Kind, QStyle.StandardPixmap | None] = {
    Kind.ACTIVE: None,
    Kind.WAITING: None,
    Kind.RECONNECTING: QStyle.StandardPixmap.SP_BrowserReload,
    Kind.DONE: None,  # chữ “Xong ✓” đã có dấu
    Kind.CANCELLED: QStyle.StandardPixmap.SP_DialogCancelButton,
    Kind.ERROR: QStyle.StandardPixmap.SP_MessageBoxWarning,
}
_ICON_SIZE = 16


def _plain_label() -> QLabel:
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    return label


class TransferRow(QFrame):
    """Thẻ của một lượt: dòng 1 tên file (hoặc “n file”) và máy kia, dòng 2 thanh tiến độ, dòng 3
    biểu tượng + nhãn trạng thái ngắn + dòng phụ chữ nhỏ (tiến độ, câu chờ, câu lỗi mục 8).

    Thuộc tính cho test: `key`, `progress_bar`, `title_label`, `status_label` (nhãn ngắn),
    `detail_label` (dòng phụ), `icon_label`, `cancel_button`, `open_button`, `finished`, `kind`.
    """

    def __init__(self, key: TransferKey, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.finished = False
        self.kind = Kind.WAITING
        # Trạng thái để vẽ lại chữ (đổi ngôn ngữ, đổi tên máy).
        self.status = TransferStatus.WAITING
        self.info: TransferInfo | None = None
        self.file_count = 0
        self.total_bytes = 0
        self.first_name: str | None = None  # tên file đầu (chưa qua `file_label`)
        self.offer_name: str | None = None  # tên máy trong `OfferReceived` (dự phòng)
        self.resumed = False
        self.unreachable = False
        self.progress: TransferProgress | None = None
        self.shown_bytes = 0  # byte hiển thị (thanh, %), chỉ tăng
        self.stalled_shown = False  # lần vẽ đầy đủ cuối là câu đứng yên (để biết lúc đổi loại câu)
        self.result: TransferFinished | None = None
        self.last_path: Path | None = None  # `FileDone.path` cuối (bên nhận)

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.AlternateBase)  # rãnh thanh tiến độ (Base) vẫn thấy

        self.title_label = _plain_label()
        self.title_label.setWordWrap(True)  # tên file + tên máy dài: xuống dòng, không đẩy nút ra ngoài
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        title_font = self.title_label.font()
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.cancel_button = QPushButton()
        self.open_button = QPushButton()
        self.open_button.setVisible(False)
        for button in (self.cancel_button, self.open_button):
            button.setAutoDefault(False)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, BAR_MAX)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(8)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(_ICON_SIZE, _ICON_SIZE)
        self.status_label = _plain_label()
        status_font = self.status_label.font()
        status_font.setBold(True)
        self.status_label.setFont(status_font)
        self.detail_label = _plain_label()
        self.detail_label.setWordWrap(True)
        small = self.detail_label.font()
        if small.pointSizeF() > 0:
            small.setPointSizeF(small.pointSizeF() * 0.9)
        self.detail_label.setFont(small)
        self.detail_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)  # chữ phụ, vẫn ≥ 4,5:1
        self.detail_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        top = QHBoxLayout()
        top.addWidget(self.title_label, 1)
        top.addWidget(self.open_button)
        top.addWidget(self.cancel_button)
        bottom = QHBoxLayout()
        bottom.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)
        bottom.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignTop)
        bottom.addWidget(self.detail_label, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.progress_bar)
        layout.addLayout(bottom)

    def set_info(self, info: TransferInfo) -> None:
        self.info = info
        self.status = info.status
        self.file_count = len(info.files)
        self.total_bytes = info.total_bytes
        self.first_name = info.names[0] if info.names else None

    def raise_shown(self, shown: int, total: int) -> None:
        """Nâng `shown_bytes` lên `shown` (kẹp trong 0…`total`); không bao giờ hạ, thanh không lùi."""
        self.shown_bytes = max(self.shown_bytes, min(max(0, shown), max(0, total)))
        value = bar_value(self.shown_bytes, total)
        if value > self.progress_bar.value():
            self.progress_bar.setValue(value)

    def set_kind(self, kind: Kind) -> None:
        """Đổi biểu tượng theo loại trạng thái (chỉ khi đổi, để cập nhật tiến độ vẫn rẻ)."""
        if kind is self.kind and self.icon_label.property("teleport_icon_set"):
            return
        self.kind = kind
        self.icon_label.setProperty("teleport_icon_set", True)
        pixmap = _ICONS[kind]
        if pixmap is None:
            self.icon_label.clear()
        else:
            self.icon_label.setPixmap(self.style().standardIcon(pixmap).pixmap(_ICON_SIZE, _ICON_SIZE))


class TransfersView(QWidget):
    """Danh sách lượt truyền của phiên, mỗi `TransferKey` một thẻ."""

    cancel_requested = Signal(object)  # TransferKey

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[TransferKey, TransferRow] = {}
        self._names: dict[str, str | None] = {}
        self._connected: dict[str, bool] = {}
        self._receive_dir: Path | None = None

        # Dải thông báo ở đầu danh sách (ẩn khi không có).
        self.notice_frame = QFrame()
        self.notice_frame.setObjectName("transfersNotice")
        self.notice_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self.notice_frame.setAutoFillBackground(True)
        self.notice_frame.setBackgroundRole(QPalette.ColorRole.AlternateBase)
        self.notice_label = _plain_label()
        self.notice_label.setObjectName("transfersNoticeText")
        self.notice_label.setWordWrap(True)
        # Đọc được bằng bàn phím và trình đọc màn hình (Tab tới câu, rồi tới nút “Đóng”).
        self.notice_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByKeyboard | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.notice_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.notice_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        # Nút hành động của thông báo (“Ghép đôi lại”, “Hủy ghép đôi”…); chỉ hiện khi có.
        self.notice_action = QPushButton()
        self.notice_action.setObjectName("transfersNoticeAction")
        self.notice_action.setAutoDefault(False)
        self.notice_action.setVisible(False)
        self.notice_action.clicked.connect(self._notice_action_clicked)
        self._notice_callback: Callable[[], None] | None = None
        self.notice_close = QPushButton()
        self.notice_close.setObjectName("transfersNoticeClose")
        self.notice_close.setAutoDefault(False)
        self.notice_close.clicked.connect(self.hide_notice)
        # Biểu tượng thông tin của style (trang trí: câu đã đủ nghĩa, không nhận tiêu điểm).
        self.notice_icon = QLabel()
        self.notice_icon.setObjectName("transfersNoticeIcon")
        self.notice_icon.setFixedSize(_ICON_SIZE, _ICON_SIZE)
        self._set_notice_icon()
        # Hàng trên: biểu tượng + câu chiếm hết bề ngang; hàng dưới: nút căn phải. Cột lượt hẹp,
        # nút nằm cạnh câu thì câu bị ép thành cột chữ.
        text_row = QHBoxLayout()
        text_row.addWidget(self.notice_icon, 0, Qt.AlignmentFlag.AlignTop)
        text_row.addWidget(self.notice_label, 1)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.notice_action)
        button_row.addWidget(self.notice_close)
        notice_layout = QVBoxLayout(self.notice_frame)
        notice_layout.addLayout(text_row)
        notice_layout.addLayout(button_row)
        self.notice_frame.setVisible(False)
        QWidget.setTabOrder(self.notice_label, self.notice_action)
        QWidget.setTabOrder(self.notice_action, self.notice_close)

        self.empty_label = _plain_label()
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)

        self._container = QWidget()
        self._list_layout = QVBoxLayout(self._container)
        self._list_layout.addWidget(self.empty_label)
        self._list_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # thẻ co theo bề ngang
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self._container)
        self._scroll = scroll
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.notice_frame)
        layout.addWidget(scroll)
        self.retranslate()

    # ---- API -----------------------------------------------------------------------------------

    def rows(self) -> list[TransferRow]:
        return list(self._rows.values())

    def row(self, key: TransferKey) -> TransferRow | None:
        return self._rows.get(key)

    def show_notice(
        self,
        text: str,
        action_text: str | None = None,
        on_action: Callable[[], None] | None = None,
    ) -> None:
        """Hiện `text` (chữ thuần) ở dải thông báo, thay thông báo cũ; chuỗi rỗng → ẩn.

        `action_text` và `on_action` cùng có → thêm một nút hành động (trước nút “Đóng”); bấm thì
        ẩn thông báo rồi gọi `on_action`. Thiếu một trong hai → không có nút.
        """
        if not text:
            self.hide_notice()
            return
        self.notice_label.setText(text)
        self.notice_label.setAccessibleDescription(text)
        self.notice_frame.setAccessibleDescription(text)
        if action_text and on_action is not None:
            self._notice_callback = on_action
            self.notice_action.setText(action_text)
            self.notice_action.setAccessibleName(action_text)
            self.notice_action.setAccessibleDescription(text)
            self.notice_action.setVisible(True)
        else:
            self._clear_notice_action()
        self.notice_frame.setVisible(True)

    def hide_notice(self) -> None:
        """Ẩn dải thông báo (nút “Đóng”). Đang có tiêu điểm thì Qt tự chuyển sang widget kế tiếp."""
        self.notice_frame.setVisible(False)
        self.notice_label.clear()
        self.notice_label.setAccessibleDescription("")
        self.notice_frame.setAccessibleDescription("")
        self._clear_notice_action()

    def notice_text(self) -> str | None:
        """Chữ của thông báo đang hiện; không có → None (test)."""
        if self.notice_frame.isHidden() or not self.notice_label.text():
            return None
        return self.notice_label.text()

    def notice_action_text(self) -> str | None:
        """Chữ của nút hành động của thông báo đang hiện; không có → None (test)."""
        if self.notice_text() is None or self.notice_action.isHidden():
            return None
        return self.notice_action.text()

    def set_device_names(self, names: Mapping[str, str | None]) -> None:
        """Đặt lại bảng tên máy (device_id → tên từ `devices()`) và vẽ lại chữ các thẻ."""
        self._names = dict(names)
        for row in self._rows.values():
            self._render_text(row)

    def set_connected(self, device_id: str, connected: bool) -> None:
        """Thiết bị có kết nối điều khiển hay không (dòng phụ của lượt gửi đang chờ trả lời)."""
        self._connected[device_id] = connected
        for row in self._rows.values():
            if row.key.device_id == device_id and not row.finished:
                self._render_status(row)

    def set_receive_dir(self, receive_dir: Path | None) -> None:
        """Thư mục nhận, cho câu lỗi ghi đĩa / từ chối vì ổ của bên nhận."""
        self._receive_dir = receive_dir

    def load_snapshot(self, infos: Iterable[TransferInfo]) -> None:
        """Ảnh chụp `transfers()`: thêm thẻ cho lượt chưa có, thẻ đã có giữ nguyên."""
        for info in infos:
            if info.key in self._rows:
                continue
            row = self._add_row(info.key)
            row.set_info(info)
            row.raise_shown(info.done_bytes + info.unsynced_bytes, info.total_bytes)
            self._render_text(row)

    def remove(self, key: TransferKey) -> None:
        """Gỡ thẻ của `key` (không có → bỏ qua). Sự kiện tới sau sẽ dựng lại thẻ: bên gọi tự lọc."""
        row = self._rows.pop(key, None)
        if row is None:
            return
        self._list_layout.removeWidget(row)
        row.setParent(None)
        row.deleteLater()
        self._update_empty()

    def clear(self) -> None:
        """Xóa mọi thẻ, thông báo và trạng thái kết nối đã biết (host mới, “Thử lại”)."""
        self.hide_notice()
        self._connected.clear()
        rows = list(self._rows.values())
        self._rows.clear()
        for row in rows:
            self._list_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._update_empty()

    def handle(self, update: TransferUpdate) -> None:
        """Nhận một `TransferUpdate` của node (đã về thread Qt)."""
        if isinstance(update, TransferProgress):
            self._on_progress(update)
        elif isinstance(update, TransferStatusChanged):
            row = self._ensure(update.info.key)
            if row.finished:
                return
            row.set_info(update.info)
            row.raise_shown(update.info.done_bytes + update.info.unsynced_bytes, update.info.total_bytes)
            # Engine báo `DataUnreachable` TRƯỚC `StatusChanged(RECONNECTING)` của cùng lần thất bại
            # (`_finish`: hành động, rồi báo cáo, rồi trạng thái): RECONNECTING không xóa câu tường
            # lửa. Câu lượt khôi phục giữ qua RECONNECTING / WAITING, chỉ mất khi dữ liệu chảy lại.
            status = update.info.status
            if status is not TransferStatus.RECONNECTING:
                row.unreachable = False
            if status in _FLOWING_STATUSES:
                row.resumed = False
            self._render_text(row)
        elif isinstance(update, OfferReceived):
            row = self._ensure(update.key)
            if row.finished:
                return
            row.offer_name = update.device_name
            if row.info is None:
                row.status = TransferStatus.WAITING
                row.file_count = len(update.files)
                row.total_bytes = update.total_bytes
                row.first_name = update.names[0] if update.names else None
            self._render_text(row)
        elif isinstance(update, FileDone):
            existing = self._rows.get(update.key)
            if existing is not None and not existing.finished and update.path is not None:
                existing.last_path = update.path
        elif isinstance(update, TransferFinished):
            self._on_finished(update)
        elif isinstance(update, TransferResumed):
            row = self._ensure(update.info.key)
            if row.finished:
                return
            row.set_info(update.info)
            row.resumed = True
            row.raise_shown(update.info.done_bytes + update.info.unsynced_bytes, update.info.total_bytes)
            self._render_text(row)
        elif isinstance(update, DataUnreachable):
            for row in self._rows.values():
                if row.key.device_id == update.device_id and not row.finished:
                    row.unreachable = True
                    self._render_status(row)

    # ---- Bên trong -----------------------------------------------------------------------------

    def _clear_notice_action(self) -> None:
        self._notice_callback = None
        self.notice_action.setVisible(False)
        self.notice_action.setText("")
        self.notice_action.setAccessibleName("")
        self.notice_action.setAccessibleDescription("")

    def _notice_action_clicked(self) -> None:
        callback = self._notice_callback
        self.hide_notice()
        if callback is not None:
            callback()

    def _ensure(self, key: TransferKey) -> TransferRow:
        row = self._rows.get(key)
        return row if row is not None else self._add_row(key)

    def _add_row(self, key: TransferKey) -> TransferRow:
        row = TransferRow(key, self._container)
        row.cancel_button.clicked.connect(lambda: self._cancel_clicked(row))
        row.open_button.clicked.connect(lambda: self._open_clicked(row))
        self._rows[key] = row
        # Trước khoảng giãn cuối danh sách: thẻ mới nằm dưới cùng.
        self._list_layout.insertWidget(self._list_layout.count() - 1, row)
        row.show()  # hiện ngay (layout chỉ hẹn hiện), để nút “Mở thư mục” hiện được trong cùng lượt
        self._update_empty()
        return row

    def _on_progress(self, update: TransferProgress) -> None:
        info = update.info
        row = self._rows.get(info.key)
        if row is None:
            row = self._add_row(info.key)
            row.set_info(info)
            self._render_text(row)
        if row.finished:
            return
        row.info = info
        row.progress = update
        old_shown = row.shown_bytes
        row.raise_shown(update.shown_bytes, info.total_bytes)
        # App phát tiến độ mỗi nhịp cả khi RECONNECTING đứng yên: chỉ khi dữ liệu thật sự chảy (byte
        # hiển thị tăng, hay đã về TRANSFERRING) mới bỏ câu tường lửa / câu lượt khôi phục.
        flowing = row.shown_bytes > old_shown or info.status is TransferStatus.TRANSFERRING
        cleared = flowing and (row.resumed or row.unreachable)
        if cleared:
            row.resumed = False
            row.unreachable = False
        if cleared or info.status is not row.status:
            row.status = info.status
            self._render_status(row)
        elif row.status not in _WAITING_STATUSES and not (row.resumed or row.unreachable):
            if self._is_stalled(row) is not row.stalled_shown:
                self._render_status(row)  # đổi loại câu (đứng yên ↔ tốc độ): cập nhật cả trình đọc màn hình
            else:
                row.detail_label.setText(self._active_detail(row))

    def _on_finished(self, update: TransferFinished) -> None:
        if update.result is TransferResult.SUSPENDED:
            return  # chỉ khi host dừng: giữ thẻ như cũ
        row = self._ensure(update.info.key)
        if row.finished:
            return
        row.set_info(update.info)
        row.finished = True
        row.result = update
        if update.result is TransferResult.DONE:
            row.progress_bar.setValue(BAR_MAX)
        row.cancel_button.setEnabled(False)
        row.cancel_button.setVisible(False)
        row.open_button.setVisible(
            update.result is TransferResult.DONE
            and row.key.direction is Direction.RECEIVE
            and row.last_path is not None
        )
        self._render_text(row)

    def _cancel_clicked(self, row: TransferRow) -> None:
        if not row.finished and self._rows.get(row.key) is row:
            self.cancel_requested.emit(row.key)

    def _open_clicked(self, row: TransferRow) -> None:
        if row.last_path is None:
            return
        open_url(QUrl.fromLocalFile(str(row.last_path.parent)))

    def _peer(self, row: TransferRow) -> str:
        device_id = row.key.device_id
        name = self._names.get(device_id) or row.offer_name
        return texts.peer_label(name, device_id)

    def _title(self, row: TransferRow) -> str:
        """Dòng 1: “video.mp4 (8,0 GB) · tới PC-Nha” / “3 file (8,4 GB) · từ Laptop-Minh”."""
        t = QCoreApplication.translate
        peer = self._peer(row)
        size = format_size(max(0, row.total_bytes))
        if row.file_count == 1 and row.first_name is not None:
            what = f"{texts.file_label(row.first_name)} ({size})"
        else:
            what = _fill(t("Transfers", "%n file ({size})", None, row.file_count), size=size)
        if row.key.direction is Direction.SEND:
            return _fill(t("Transfers", "{what} · tới {name}"), what=what, name=peer)
        return _fill(t("Transfers", "{what} · từ {name}"), what=what, name=peer)

    def _status(self, row: TransferRow) -> tuple[Kind, str, str]:
        """(loại, nhãn ngắn, dòng phụ) của thẻ."""
        peer = self._peer(row)
        if row.result is not None:
            result = row.result.result
            return (
                result_kind(result),
                result_status(result),
                result_message(row.result, peer, self._receive_dir),
            )
        direction = row.key.direction
        if row.unreachable:
            return (
                Kind.RECONNECTING,
                short_status(direction, TransferStatus.RECONNECTING),
                unreachable_text(peer),
            )
        if row.resumed:
            return Kind.WAITING, short_status(direction, TransferStatus.WAITING), resumed_text(peer)
        connected = self._connected.get(row.key.device_id, True)
        waiting = waiting_detail(direction, row.status, peer, connected=connected)
        detail = waiting if waiting is not None else self._active_detail(row, peer)
        return status_kind(row.status), short_status(direction, row.status), detail

    def _active_detail(self, row: TransferRow, peer: str | None = None) -> str:
        """Dòng phụ của thẻ TRANSFERRING / VERIFYING / RECONNECTING (xem docstring module)."""
        status = row.status
        if status is TransferStatus.VERIFYING:
            return verifying_text(row.info)
        percent = percent_text(row.shown_bytes, row.total_bytes)
        if status is TransferStatus.RECONNECTING:
            return reconnecting_text(percent, self._peer(row) if peer is None else peer)
        progress = row.progress
        if progress is None:
            return ""
        if self._is_stalled(row):
            return stalled_text(percent, row.key.direction, self._peer(row) if peer is None else peer)
        return progress_text(progress, row.shown_bytes)

    @staticmethod
    def _is_stalled(row: TransferRow) -> bool:
        """Thẻ TRANSFERRING đã `STALL_TEXT_AFTER` giây không có byte mới (dòng phụ là câu đứng yên)."""
        progress = row.progress
        return (
            row.status is TransferStatus.TRANSFERRING
            and progress is not None
            and progress.idle_seconds >= STALL_TEXT_AFTER
        )

    def _render_status(self, row: TransferRow) -> None:
        row.stalled_shown = self._is_stalled(row)
        kind, status, detail = self._status(row)
        row.set_kind(kind)
        row.status_label.setText(status)
        row.detail_label.setText(detail)
        # Trình đọc màn hình: thẻ đọc tiêu đề, rồi trạng thái và dòng phụ.
        row.setAccessibleDescription(f"{status}. {detail}" if detail else status)
        row.progress_bar.setAccessibleDescription(status)

    def _render_text(self, row: TransferRow) -> None:
        t = QCoreApplication.translate
        title = self._title(row)
        row.title_label.setText(title)
        row.setAccessibleName(title)
        row.progress_bar.setAccessibleName(t("Transfers", "Tiến độ"))
        cancel, open_text = t("Transfers", "Hủy"), t("Transfers", "Mở thư mục")
        for button, text in ((row.cancel_button, cancel), (row.open_button, open_text)):
            button.setText(text)
            button.setAccessibleName(text)
            button.setAccessibleDescription(title)  # nút nào của thẻ nào (chỉ cho trình đọc màn hình)
        self._render_status(row)

    def _update_empty(self) -> None:
        self.empty_label.setVisible(not self._rows)

    def retranslate(self) -> None:
        t = QCoreApplication.translate
        self.empty_label.setText(t("Transfers", "Kéo file vào một thiết bị để gửi"))
        self.setAccessibleName(t("Transfers", "Lượt truyền"))
        notice = t("Transfers", "Thông báo")
        self.notice_frame.setAccessibleName(notice)
        self.notice_label.setAccessibleName(notice)
        close = t("Transfers", "Đóng")
        self.notice_close.setText(close)
        self.notice_close.setAccessibleName(close)
        for row in self._rows.values():
            self._render_text(row)
        self._update_empty()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        elif event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            self._set_notice_icon()
            for row in self._rows.values():  # biểu tượng của style mới (sáng/tối)
                row.icon_label.setProperty("teleport_icon_set", False)
                row.set_kind(row.kind)

    def _set_notice_icon(self) -> None:
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
        self.notice_icon.setPixmap(icon.pixmap(_ICON_SIZE, _ICON_SIZE))
