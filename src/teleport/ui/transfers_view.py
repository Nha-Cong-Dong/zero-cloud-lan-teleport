"""Câu chữ của lượt truyền (mvp F5, mục 7.3 “Tiến độ”, mục 8): hàm thuần, không widget.

Từ spec B3, danh sách lượt truyền là luồng bong bóng theo máy (`ui.chat`, `ui.chat_bubbles`); module
này chỉ còn phần chữ dùng chung để câu chữ không đổi: thang thanh tiến độ (`BAR_MAX`, `bar_value`),
phần trăm (`percent_text`: một chữ số thập phân, làm tròn xuống, tối đa 99,9% tới khi xong), loại
trạng thái (`Kind`, `status_kind`, `result_kind`), nhãn ngắn (`short_status`, `result_status`), dòng
phụ khi đang truyền (`progress_text`: “42,0% · 85,2 MB/s · còn 1 phút”, tốc độ 0 thì bỏ phần “/s”,
chưa đo được thì “đang tính thời gian còn lại”), khi chờ (`waiting_detail`), khi đứng yên
(`stalled_text`, sau `STALL_TEXT_AFTER` giây không có byte mới), khi nối lại (`reconnecting_text`),
khi kiểm tra (`verifying_text`), lượt khôi phục (`resumed_text`), tường lửa (`unreachable_text`), khi
xong (`done_text`, `saved_as_text`) và câu mvp mục 8 khi kết thúc (`result_message`, port từ
`cli.transfer_result_message`; máy này tự từ chối thì chuỗi rỗng). Luật dùng các câu này (tiến độ
không lùi, khi nào đổi câu) nằm ở docstring `ui.chat_bubbles`.

Chữ do máy kia đặt (tên máy, tên file) do bên gọi đưa qua `texts.peer_label` / `texts.file_label`
(`_file_at` tự gọi `file_label`); đường dẫn trên máy này qua `send.printable`. Chuỗi qua
`QCoreApplication.translate("Transfers", ...)` lúc gọi (giữ ngữ cảnh cũ để bản dịch không đổi).
"""

from __future__ import annotations

import math
import ntpath
import sys
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from teleport.app.transfers import (
    Direction,
    TransferFinished,
    TransferInfo,
    TransferProgress,
    TransferResult,
    TransferStatus,
)
from teleport.core.display import format_duration, format_size
from teleport.ui import texts
from teleport.ui.send import printable
from teleport.ui.texts import fill as _fill

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


def saved_as_text(saved_names: list[str]) -> str:
    """Dòng phụ của thẻ nhận đã xong khi có file được lưu khác tên gốc; rỗng → `done_text()`.

    Một file: “Đã lưu thành {tên lưu} · Đã kiểm tra toàn vẹn” (tên qua `texts.file_label`); từ hai
    file: “Đã lưu n file với tên mới · Đã kiểm tra toàn vẹn”.
    """
    t = QCoreApplication.translate
    if not saved_names:
        return done_text()
    if len(saved_names) == 1:
        return _fill(
            t("Transfers", "Đã lưu thành {file} · Đã kiểm tra toàn vẹn"),
            file=texts.file_label(saved_names[0]),
        )
    return t("Transfers", "Đã lưu %n file với tên mới · Đã kiểm tra toàn vẹn", None, len(saved_names))


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
