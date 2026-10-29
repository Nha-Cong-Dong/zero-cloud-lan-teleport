"""Danh sách lượt truyền `teleport.ui.transfers_view.TransfersView` (spec PR3 pha 1, `ui/transfers_view.py`;
mvp F5, F6, mục 7.3 hàng “Tiến độ”, mục 8; spec-hackathon PR3 “Xong khi” (6), (7), (8), 9.7, mục 10 A2).

Đã cập nhật theo chỉnh câu chữ của điều phối (2026-10-29, kiểu AirDrop/LocalSend, thắng spec pha 1):
`status_label` là nhãn ngắn (“Đang gửi”, “Đang nhận”, “Đang chờ”, “Đang nối lại…”, “Đang kiểm tra”,
“Xong ✓”, “Lỗi”, “Đã hủy”); câu chờ cụ thể, câu lỗi mvp mục 8 và tiến độ nằm ở `detail_label`. Nhãn ngắn
kiểm theo ý nghĩa (không so nguyên văn); câu lỗi mục 8 so nguyên văn.

Sự kiện giả dựng bằng dataclass thật của `teleport.app.transfers` / `teleport.core.transfer` (không socket).
`test_section8_sentences_match_mvp_doc` đọc lại mvp.md để hằng mục 8 không lệch tài liệu.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, Qt, QTranslator

PEER_ID = "a71c04d2" + "e" * 24
OTHER_ID = "0f0f0f0f" + "1" * 24
PEER = "PC-Nha"
_MVP = Path(__file__).resolve().parents[2] / "docs" / "mvp.md"

# ---- Nhãn trạng thái ngắn (câu mới, đề xuất; chỉ dùng để kiểm theo ý nghĩa) ----
SHORT_SENDING = "Đang gửi"
SHORT_RECEIVING = "Đang nhận"
SHORT_WAITING = "Đang chờ"
SHORT_RECONNECTING = "Đang nối lại…"  # mvp F6, mục 8
SHORT_VERIFYING = "Đang kiểm tra"
SHORT_DONE = "Xong ✓"
SHORT_FAILED = "Lỗi"
SHORT_CANCELLED = "Đã hủy"

# ---- Câu chờ ở `detail_label` ----
WAIT_QUEUED_SEND = "Đang chờ {p} nhận xong lượt trước…"  # mvp 8
WAIT_ANSWER_SEND = "Đang chờ {p} trả lời…"  # mvp 7.3 (CLI)
WAIT_OFFLINE_SEND = "Đang chờ kết nối tới {p}…"  # mới (mục 10 A2)
WAIT_RESUMED = "Đang chờ {p} nối lại…"  # mới (9.7)
DONE_DETAIL = "Đã kiểm tra toàn vẹn"  # mới
EMPTY_HINT = "Kéo file vào một thiết bị để gửi"  # mới
CANCEL = "Hủy"
OPEN_FOLDER = "Mở thư mục"
ETA_UNKNOWN = "đang tính thời gian còn lại"  # mvp 7.3

# ---- Kết quả (mvp mục 8, nguyên văn; GUI: tên lệnh trong “…”, không backtick) ----
R_REJECTED_SEND = "{p} đã từ chối nhận file."
R_CANCEL_SEND_SELF = "Đã hủy lượt gửi tới {p}. Các file đã gửi xong vẫn nằm trên máy đó."
R_CANCEL_SEND_PEER = "{p} đã dừng nhận giữa chừng. Các file đã gửi xong vẫn nằm trên máy đó."
R_CANCEL_RECEIVE_SELF = "Đã hủy nhận từ {p}. File đã nhận xong vẫn giữ nguyên."
R_CANCEL_RECEIVE_PEER = (
    "{p} đã hủy lượt gửi. Các file chưa nhận xong đã được xóa; file đã nhận xong vẫn giữ nguyên."
)
R_CANCEL_RECEIVE_PEER_ASKING = "{p} đã hủy lượt gửi."
R_EXPIRED_SEND = "Mất kết nối với {p} quá lâu nên lượt gửi đã dừng. Hãy gửi lại."
R_EXPIRED_RECEIVE = (
    "Mất kết nối với {p} quá lâu nên đã dừng nhận. Các file chưa nhận xong đã được xóa; "
    "file đã nhận xong vẫn giữ nguyên."
)
R_SOURCE_CHANGED_SEND = "{f} đã bị thay đổi trên máy gửi nên không thể gửi tiếp. Hãy gửi lại."
R_SOURCE_CHANGED_RECEIVE = (
    "{f} đã bị thay đổi trên {p} nên không nhận tiếp được. Các file đã nhận xong vẫn giữ nguyên; "
    "hãy nhờ gửi lại."
)
R_NOT_RECEIVING_SEND = "{p} chưa bật nhận file. Trên máy đó hãy chạy “teleport receive” rồi gửi lại."
DATA_UNREACHABLE = (
    "Không mở được kết nối truyền dữ liệu tới {p}; tường lửa trên máy đó có thể đang chặn Teleport. "
    "Teleport vẫn đang thử lại."
)

PREFIX = "[x]"


def fill(template: str, **values: str) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    return out


def percent_values(text: str) -> list[float]:
    """Mọi số phần trăm trong chữ (“42%”, “42,0%”) dạng số thực."""
    return [float(m.replace(",", ".")) for m in re.findall(r"(\d+(?:,\d+)?)\s*%", text)]


class PrefixTranslator(QTranslator):
    def isEmpty(self):
        return False

    def translate(self, context, source_text, disambiguation=None, n=-1):
        if not source_text:
            return source_text
        return PREFIX + source_text


# =================================================================================================
# Dựng sự kiện giả
# =================================================================================================


def key(direction: str = "send", transfer_id: str = "c" * 32, device_id: str = PEER_ID):
    from teleport.core.transfer import Direction, TransferKey

    return TransferKey(device_id, Direction(direction), transfer_id)


def entry(index: int, name: str, size: int):
    """`FileEntry` thật. Tên độc không hợp lệ ở mức giao thức thì mục mang tên thay thế; tên độc chỉ
    nằm trong `names` (tên để hiện)."""
    from teleport.core.messages import FileEntry, MessageError

    try:
        return FileEntry(name, size, 1_700_000_000_000)
    except MessageError:
        return FileEntry(f"raw{index}.bin", size, 1_700_000_000_000)


def info(
    k,
    status: str = "transferring",
    files=(("video.mp4", 8 * 2**30),),
    done: int = 0,
    total: int | None = None,
    file_index: int = 0,
):
    from teleport.core.transfer import TransferInfo, TransferStatus

    entries = tuple(entry(i, name, size) for i, (name, size) in enumerate(files))
    return TransferInfo(
        key=k,
        status=TransferStatus(status),
        files=entries,
        names=tuple(name for name, _ in files),
        file_index=file_index,
        done_bytes=done,
        total_bytes=sum(s for _, s in files) if total is None else total,
    )


def status_changed(k, status: str, **kw):
    from teleport.app.transfers import TransferStatusChanged

    return TransferStatusChanged(info(k, status, **kw))


def progress(k, done: int, total: int, bps: float = 85.2 * 2**20, eta: float | None = 62.0, **kw):
    from teleport.app.transfers import TransferProgress

    return TransferProgress(info(k, "transferring", done=done, total=total, **kw), bps, eta)


def finished(k, result: str, *, by_peer: bool = False, status: str = "transferring", **kw):
    from teleport.app.transfers import TransferFinished, TransferResult

    return TransferFinished(
        info=info(k, status, **kw),
        result=TransferResult(result),
        by_peer=by_peer,
        reason=None,
        file_index=0,
        reject=None,
    )


def offer_received(k, device_name: str | None = PEER, files=(("video.mp4", 8 * 2**30),)):
    from teleport.app.transfers import OfferReceived

    entries = tuple(entry(i, name, size) for i, (name, size) in enumerate(files))
    return OfferReceived(
        key=k,
        device_name=device_name,
        files=entries,
        names=tuple(n for n, _ in files),
        total_bytes=sum(s for _, s in files),
        auto_accepted=False,
    )


def resumed(k, **kw):
    from teleport.app.transfers import TransferResumed

    return TransferResumed(info(k, "reconnecting", **kw))


def file_done(k, path: Path | None, index: int = 0, name: str = "video.mp4"):
    from teleport.app.transfers import FileDone

    return FileDone(key=k, file_index=index, name=name, path=path)


def data_unreachable(device_id: str = PEER_ID):
    from teleport.app.transfers import DataUnreachable

    return DataUnreachable(device_id)


def row_texts(row) -> str:
    return "\n".join(
        lb.text()
        for lb in (row.title_label, row.status_label, row.detail_label)
        if lb is not None and lb.text()
    )


def shown(widget, root) -> bool:
    return widget.isVisibleTo(root)


@pytest.fixture
def view(qtbot):
    from teleport.ui.transfers_view import TransfersView

    v = TransfersView()
    qtbot.addWidget(v)
    v.cancels = []  # type: ignore[attr-defined]
    v.cancel_requested.connect(lambda k: v.cancels.append(k))
    v.set_device_names({PEER_ID: PEER})
    v.show()
    return v


@pytest.fixture
def translator():
    t = PrefixTranslator()
    yield t
    QCoreApplication.removeTranslator(t)


# =================================================================================================
# Trạng thái theo TransferStatus × chiều (F5, mục 8)
# =================================================================================================

# (chiều, trạng thái, chữ thường phải có trong nhãn ngắn, câu phải có trong `detail_label` hoặc None)
STATUS_CASES = [
    ("send", "queued", "chờ", WAIT_QUEUED_SEND),
    ("receive", "queued", "chờ", None),
    ("send", "waiting", "chờ", WAIT_ANSWER_SEND),
    ("receive", "waiting", "chờ", None),
    ("send", "transferring", "gửi", None),
    ("receive", "transferring", "nhận", None),
    ("send", "verifying", "kiểm tra", None),
    ("receive", "verifying", "kiểm tra", None),
    ("send", "reconnecting", "nối lại", None),
    ("receive", "reconnecting", "nối lại", None),
]


@pytest.mark.parametrize(("direction", "status", "short_word", "detail"), STATUS_CASES)
def test_status_label_and_detail_per_status_and_direction(view, direction, status, short_word, detail):
    k = key(direction)
    view.set_connected(PEER_ID, True)
    view.handle(status_changed(k, status))
    row = view.row(k)
    assert row is not None
    short = row.status_label.text()
    assert short_word in short.lower(), short
    assert len(short) <= 20, f"nhãn trạng thái phải ngắn: {short!r}"
    if detail is not None:
        assert fill(detail, p=PEER) in row.detail_label.text()


def test_every_transfer_status_is_covered_by_this_table():
    from teleport.core.transfer import Direction, TransferStatus

    covered = {(d, s) for d, s, _, _ in STATUS_CASES}
    assert covered == {(d.value, s.value) for d in Direction for s in TransferStatus}


def test_send_and_receive_transferring_labels_differ(view):
    s, r = key("send"), key("receive", "d" * 32)
    view.handle(status_changed(s, "transferring"))
    view.handle(status_changed(r, "transferring"))
    assert view.row(s).status_label.text() != view.row(r).status_label.text()


def test_send_waiting_while_not_connected(view):
    k = key("send")
    view.set_connected(PEER_ID, False)
    view.handle(status_changed(k, "waiting"))
    assert fill(WAIT_OFFLINE_SEND, p=PEER) in view.row(k).detail_label.text()
    view.set_connected(PEER_ID, True)
    detail = view.row(k).detail_label.text()
    assert fill(WAIT_ANSWER_SEND, p=PEER) in detail
    assert fill(WAIT_OFFLINE_SEND, p=PEER) not in detail
    view.set_connected(PEER_ID, False)
    assert fill(WAIT_OFFLINE_SEND, p=PEER) in view.row(k).detail_label.text()


def test_clear_forgets_connection_state(view):
    """Host mới: trạng thái “chưa kết nối” của host cũ không được dính sang thẻ mới."""
    view.set_connected(PEER_ID, False)
    view.clear()
    k = key("send")
    view.handle(status_changed(k, "waiting"))
    detail = view.row(k).detail_label.text()
    assert fill(WAIT_OFFLINE_SEND, p=PEER) not in detail
    assert fill(WAIT_ANSWER_SEND, p=PEER) in detail


def test_notice_action_button_tab_order_and_hidden_without_action(view):
    calls: list[int] = []
    view.show_notice("Câu có nút.", "Làm gì đó", lambda: calls.append(1))
    assert view.notice_action_text() == "Làm gì đó"
    assert view.notice_action.accessibleName() == "Làm gì đó"
    # Thứ tự Tab: câu → nút hành động → “Đóng”.
    assert view.notice_label.nextInFocusChain() is view.notice_action
    assert view.notice_action.nextInFocusChain() is view.notice_close
    view.notice_action.click()
    assert calls == [1]
    assert view.notice_text() is None
    view.show_notice("Câu không nút.")
    assert view.notice_action_text() is None
    view.show_notice("Câu thiếu hàm.", "Nút", None)
    assert view.notice_action_text() is None


def test_not_connected_does_not_change_transferring_or_receive_rows(view):
    view.set_connected(PEER_ID, False)
    offline = fill(WAIT_OFFLINE_SEND, p=PEER)
    k = key("send")
    view.handle(status_changed(k, "transferring"))
    assert offline not in row_texts(view.row(k))
    r = key("receive", "d" * 32)
    view.handle(status_changed(r, "waiting"))
    assert offline not in row_texts(view.row(r))


def test_status_change_updates_same_row(view):
    k = key("send")
    view.set_connected(PEER_ID, True)
    view.handle(status_changed(k, "waiting"))
    row = view.row(k)
    view.handle(status_changed(k, "transferring"))
    view.handle(status_changed(k, "reconnecting"))
    assert view.row(k) is row
    assert len(view.rows()) == 1
    assert "nối lại" in row.status_label.text().lower()
    assert fill(WAIT_ANSWER_SEND, p=PEER) not in row.detail_label.text(), "câu chờ cũ phải được xóa"


def test_offer_received_adds_receive_row_waiting(view):
    k = key("receive")
    view.handle(offer_received(k))
    view.handle(offer_received(k))
    assert len(view.rows()) == 1
    row = view.row(k)
    assert row.key == k
    assert "chờ" in row.status_label.text().lower()
    assert f"từ {PEER}" in row_texts(row)


# =================================================================================================
# Tiêu đề hàng, tên máy
# =================================================================================================


def test_title_send_many_files_shows_count_and_size(view):
    from teleport.core.display import format_size

    files = (("a.bin", 2**20), ("b.bin", 3 * 2**20), ("c.bin", 5))
    k = key("send")
    view.handle(status_changed(k, "transferring", files=files))
    text = row_texts(view.row(k))
    assert f"tới {PEER}" in text
    assert f"3 file ({format_size(sum(s for _, s in files))})" in text


def test_title_single_file_shows_file_name(view):
    from teleport.ui.texts import file_label

    name = "bao-cao-" * 20 + ".docx"
    k = key("receive")
    view.handle(status_changed(k, "transferring", files=((name, 10),)))
    text = row_texts(view.row(k))
    assert f"từ {PEER}" in text
    assert file_label(name) in text
    assert name not in text


def test_unknown_device_shows_first_8_chars_of_id(qtbot):
    from teleport.ui.transfers_view import TransfersView

    v = TransfersView()
    qtbot.addWidget(v)
    k = key("receive")
    v.handle(status_changed(k, "transferring"))
    text = row_texts(v.row(k))
    assert f"từ {PEER_ID[:8]}" in text
    assert PEER_ID not in text


def test_set_device_names_redraws_existing_rows(view):
    k = key("send")
    view.set_device_names({})
    view.set_connected(PEER_ID, True)
    view.handle(status_changed(k, "waiting"))
    assert f"tới {PEER_ID[:8]}" in row_texts(view.row(k))
    view.set_device_names({PEER_ID: "May-Moi"})
    assert "tới May-Moi" in row_texts(view.row(k))
    assert fill(WAIT_ANSWER_SEND, p="May-Moi") in view.row(k).detail_label.text()
    view.set_device_names({PEER_ID: None})
    assert f"tới {PEER_ID[:8]}" in row_texts(view.row(k))


def test_hostile_device_and_file_names_shown_as_plain_text(view):
    from PySide6.QtWidgets import QLabel, QWidget

    from teleport.ui.texts import file_label, peer_label

    device = "<b>X</b>‮\n"
    name = "<i>f</i>‮\ngnp.exe"
    view.set_device_names({PEER_ID: device})
    k = key("receive")
    view.handle(status_changed(k, "transferring", files=((name, 10),)))
    d = key("receive", "d" * 32)
    view.handle(status_changed(d, "transferring", files=((name, 10),)))
    view.handle(finished(d, "cancelled_user", by_peer=True, files=((name, 10),)))
    text = "\n".join(lb.text() for lb in view.findChildren(QLabel))
    assert peer_label(device, PEER_ID) in text
    assert file_label(name) in text
    assert "<i>f</i>‮" not in text  # BiDi trong tên file thành `·` (mvp 7.3)
    for lb in view.findChildren(QLabel):
        if "<b>" in lb.text() or "<i>" in lb.text():
            assert lb.textFormat() == Qt.TextFormat.PlainText, lb.text()
    for w in [view, *view.findChildren(QWidget)]:
        assert "<b>X" not in w.toolTip() and "<i>f" not in w.toolTip()


def test_all_row_labels_are_plain_text(view):
    k = key("send")
    view.handle(status_changed(k, "transferring"))
    row = view.row(k)
    for lb in (row.title_label, row.status_label, row.detail_label):
        assert lb.textFormat() == Qt.TextFormat.PlainText


# =================================================================================================
# Tiến độ (mvp 7.3 hàng “Tiến độ”; Xong khi (6), (8))
# =================================================================================================


def test_progress_shows_percent_speed_and_eta(view):
    from teleport.core.display import format_duration, format_size

    total = 1000 * 2**20
    done = 420 * 2**20
    bps = 85.2 * 2**20
    k = key("send")
    view.handle(status_changed(k, "transferring"))
    view.handle(progress(k, done, total, bps=bps, eta=62.0))
    row = view.row(k)
    assert row.progress_bar.minimum() == 0 and row.progress_bar.maximum() == 1000
    assert row.progress_bar.value() == 420
    detail = row.detail_label.text()
    assert [int(p) for p in percent_values(detail)] == [42], detail
    assert f"{format_size(int(bps))}/s" in detail
    assert format_size(int(bps)) == "85,2 MB"
    assert f"còn {format_duration(62.0)}" in detail
    assert ETA_UNKNOWN not in detail


def test_progress_without_eta_says_still_estimating(view):
    k = key("receive")
    view.handle(status_changed(k, "transferring"))
    view.handle(progress(k, 10, 1000, bps=0.0, eta=None))
    detail = view.row(k).detail_label.text()
    assert ETA_UNKNOWN in detail
    assert "còn " not in detail.replace(ETA_UNKNOWN, "")


def test_progress_never_shows_100_percent_before_done(view):
    k = key("send")
    view.handle(status_changed(k, "transferring"))
    view.handle(progress(k, 999_999, 1_000_000))
    row = view.row(k)
    assert row.progress_bar.value() == 999
    values = percent_values(row.detail_label.text())
    assert values and all(99 <= v < 100 for v in values), row.detail_label.text()


def test_progress_four_terabytes_half_way_is_500(view):
    k = key("send")
    view.handle(status_changed(k, "transferring", files=(("disk.img", 2**42),)))
    view.handle(progress(k, 2**41, 2**42, files=(("disk.img", 2**42),)))
    row = view.row(k)
    assert row.progress_bar.value() == 500
    assert [int(p) for p in percent_values(row.detail_label.text())] == [50]


def test_progress_total_zero_gives_zero(view):
    k = key("send")
    view.handle(status_changed(k, "transferring", files=(("rong.txt", 0),)))
    view.handle(progress(k, 0, 0, files=(("rong.txt", 0),)))
    assert view.row(k).progress_bar.value() == 0


def test_thousand_progress_updates_in_place_and_fast(view):
    from PySide6.QtWidgets import QWidget

    k = key("send")
    total = 2**42
    view.handle(status_changed(k, "transferring"))
    view.handle(progress(k, 0, total))
    row = view.row(k)
    bar, status, detail = row.progress_bar, row.status_label, row.detail_label
    widgets_before = len(view.findChildren(QWidget))
    updates = [progress(k, total * i // 1000, total, eta=float(1000 - i)) for i in range(1, 1001)]
    start = time.perf_counter()
    for u in updates:
        view.handle(u)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, elapsed
    assert len(view.rows()) == 1
    assert view.row(k) is row
    assert row.progress_bar is bar and row.status_label is status and row.detail_label is detail
    assert len(view.findChildren(QWidget)) == widgets_before
    assert bar.value() == 1000


# =================================================================================================
# Kết thúc (Xong khi (7))
# =================================================================================================

ALL_DIRECTIONS = ("send", "receive")


def test_done_shows_done_label_verified_detail_and_full_bar(view):
    for direction in ALL_DIRECTIONS:
        k = key(direction)
        view.handle(status_changed(k, "transferring"))
        view.handle(finished(k, "done", status="verifying", done=8 * 2**30))
        row = view.row(k)
        assert "xong" in row.status_label.text().lower()
        assert DONE_DETAIL in row.detail_label.text()
        assert row.progress_bar.value() == 1000
        assert row.finished is True


def test_every_result_and_direction_has_a_sentence(qtbot):
    """Mỗi `TransferResult` × chiều ra nhãn ngắn không rỗng; câu chi tiết không backtick (trừ SUSPENDED:
    giữ hàng như cũ). Câu chi tiết bắt buộc trừ khi máy này là bên nhận và lượt bị từ chối (máy này tự
    từ chối: nhãn ngắn “Đã từ chối” đã đủ, như CLI chỉ in “Đã từ chối.”)."""
    from teleport.app.transfers import TransferResult
    from teleport.ui.transfers_view import TransfersView

    v = TransfersView()
    qtbot.addWidget(v)
    v.set_device_names({PEER_ID: PEER})
    missing = []
    for i, result in enumerate(TransferResult):
        if result is TransferResult.SUSPENDED:
            continue
        for direction in ALL_DIRECTIONS:
            for by_peer in (False, True):
                k = key(direction, f"{i:02}{direction[0]}{int(by_peer)}" + "0" * 28)
                v.handle(status_changed(k, "transferring"))
                v.handle(finished(k, result.value, by_peer=by_peer))
                row = v.row(k)
                short, detail = row.status_label.text().strip(), row.detail_label.text().strip()
                self_reject = direction == "receive" and result.value.startswith("rejected_")
                if not short or (not detail and not self_reject) or "`" in detail:
                    missing.append((result.value, direction, by_peer, short, detail))
    assert not missing, missing


def test_failed_and_cancelled_short_labels_differ_from_done(view):
    ok, bad, cancelled = key("send"), key("send", "d" * 32), key("send", "e" * 32)
    for k in (ok, bad, cancelled):
        view.handle(status_changed(k, "transferring"))
    view.handle(finished(ok, "done"))
    view.handle(finished(bad, "disk_failed"))
    view.handle(finished(cancelled, "cancelled_user"))
    labels = {view.row(k).status_label.text() for k in (ok, bad, cancelled)}
    assert len(labels) == 3, labels


@pytest.mark.parametrize(
    ("direction", "result", "by_peer", "status", "template"),
    [
        ("send", "rejected_user", True, "waiting", R_REJECTED_SEND),
        ("send", "cancelled_user", False, "transferring", R_CANCEL_SEND_SELF),
        ("send", "cancelled_user", True, "transferring", R_CANCEL_SEND_PEER),
        ("receive", "cancelled_user", False, "transferring", R_CANCEL_RECEIVE_SELF),
        ("receive", "cancelled_user", True, "transferring", R_CANCEL_RECEIVE_PEER),
        ("receive", "cancelled_user", True, "waiting", R_CANCEL_RECEIVE_PEER_ASKING),
        ("send", "expired", False, "reconnecting", R_EXPIRED_SEND),
        ("receive", "expired", False, "reconnecting", R_EXPIRED_RECEIVE),
        ("send", "source_changed", False, "transferring", R_SOURCE_CHANGED_SEND),
        ("receive", "source_changed", True, "transferring", R_SOURCE_CHANGED_RECEIVE),
        ("send", "rejected_not_receiving", True, "waiting", R_NOT_RECEIVING_SEND),
    ],
)
def test_result_sentences_match_section_8(view, direction, result, by_peer, status, template):
    k = key(direction)
    view.handle(status_changed(k, status))
    view.handle(finished(k, result, by_peer=by_peer, status=status))
    assert view.row(k).detail_label.text() == fill(template, p=PEER, f="video.mp4")


def test_receive_dir_missing_names_the_folder_not_a_disk_error():
    """Thư mục nhận mất mà không tạo lại được: câu nói đúng nguyên nhân, không đổ cho ổ đĩa lỗi."""
    from teleport.ui.transfers_view import result_message

    fin = finished(key("receive"), "receive_dir_missing")
    text = result_message(fin, PEER, Path(r"C:\Users\A\Downloads\Teleport"))
    assert "thư mục nhận" in text and r"C:\Users\A\Downloads\Teleport" in text
    assert PEER in text
    assert "ổ đĩa báo lỗi" not in text


def test_finished_row_hides_or_disables_cancel(view):
    k = key("send")
    view.handle(status_changed(k, "transferring"))
    row = view.row(k)
    assert shown(row.cancel_button, view) and row.cancel_button.isEnabled()
    assert row.finished is False
    view.handle(finished(k, "cancelled_user", by_peer=True))
    assert row.finished is True
    assert not (shown(row.cancel_button, view) and row.cancel_button.isEnabled())


def test_finished_row_ignores_late_updates(view):
    k = key("send")
    view.handle(status_changed(k, "transferring"))
    view.handle(progress(k, 300, 1000))
    view.handle(finished(k, "expired", status="reconnecting"))
    row = view.row(k)
    status, value, detail = row.status_label.text(), row.progress_bar.value(), row.detail_label.text()
    assert detail == fill(R_EXPIRED_SEND, p=PEER)
    view.handle(progress(k, 900, 1000))
    view.handle(status_changed(k, "transferring"))
    view.handle(finished(k, "done"))
    view.handle(data_unreachable())
    assert view.row(k) is row and len(view.rows()) == 1
    assert row.status_label.text() == status
    assert row.progress_bar.value() == value
    assert row.detail_label.text() == detail


def test_suspended_keeps_row_as_it_was(view):
    k = key("receive")
    view.handle(status_changed(k, "transferring"))
    before = (view.row(k).status_label.text(), view.row(k).detail_label.text())
    view.handle(finished(k, "suspended"))
    row = view.row(k)
    assert row is not None
    assert (row.status_label.text(), row.detail_label.text()) == before


# =================================================================================================
# Nút Hủy, Mở thư mục
# =================================================================================================


def test_cancel_button_emits_cancel_requested_with_key(view):
    k = key("receive")
    view.handle(status_changed(k, "transferring"))
    row = view.row(k)
    assert row.cancel_button.text().replace("&", "") == CANCEL
    row.cancel_button.click()
    assert view.cancels == [k]


def test_open_folder_opens_parent_of_last_received_file(view, monkeypatch, tmp_path):
    from PySide6.QtCore import QUrl

    from teleport.ui import transfers_view

    opened: list = []
    monkeypatch.setattr(transfers_view, "open_url", lambda url: opened.append(QUrl(url)) or True)
    folder = tmp_path / "nhan"
    files = (("a.bin", 5), ("b.bin", 5))
    k = key("receive")
    view.handle(status_changed(k, "transferring", files=files))
    row = view.row(k)
    assert not shown(row.open_button, view), "“Mở thư mục” ẩn tới khi nhận xong"
    view.handle(file_done(k, tmp_path / "cu" / "a.bin", 0, "a.bin"))
    view.handle(file_done(k, folder / "b (1).bin", 1, "b.bin"))
    assert not shown(row.open_button, view)
    view.handle(finished(k, "done", status="verifying", files=files))
    assert shown(row.open_button, view)
    assert row.open_button.text().replace("&", "") == OPEN_FOLDER
    row.open_button.click()
    assert len(opened) == 1
    assert opened[0].isLocalFile()
    assert Path(opened[0].toLocalFile()) == folder


def test_open_folder_not_shown_for_send_or_receive_without_file(view):
    s = key("send")
    view.handle(status_changed(s, "transferring"))
    view.handle(file_done(s, None))
    view.handle(finished(s, "done"))
    assert not shown(view.row(s).open_button, view)
    r = key("receive", "d" * 32)
    view.handle(status_changed(r, "transferring"))
    view.handle(finished(r, "done"))
    assert not shown(view.row(r).open_button, view)


def test_open_folder_not_shown_when_receive_fails(view, tmp_path):
    k = key("receive")
    view.handle(status_changed(k, "transferring", files=(("a.bin", 5), ("b.bin", 5))))
    view.handle(file_done(k, tmp_path / "a.bin"))
    view.handle(finished(k, "cancelled_user", by_peer=True))
    assert not shown(view.row(k).open_button, view)


def test_buttons_reachable_by_tab_and_have_accessible_names(view):
    k = key("receive")
    view.handle(status_changed(k, "transferring"))
    row = view.row(k)
    button = row.cancel_button
    assert button.focusPolicy() & Qt.FocusPolicy.TabFocus
    assert (button.accessibleName() or button.text()).replace("&", "")


# =================================================================================================
# Ảnh chụp lúc `started`, khôi phục (9.7)
# =================================================================================================


def test_snapshot_after_resumed_does_not_duplicate(view):
    k = key("receive")
    view.handle(resumed(k, done=4 * 2**30))
    row = view.row(k)
    assert fill(WAIT_RESUMED, p=PEER) in row.detail_label.text()
    other = key("send", "d" * 32)
    view.load_snapshot([info(k, "reconnecting", done=4 * 2**30), info(other, "queued")])
    keys = [r.key for r in view.rows()]
    assert keys == [k, other]
    assert view.row(k) is row
    assert fill(WAIT_RESUMED, p=PEER) in row.detail_label.text()


def test_snapshot_twice_and_resumed_after_snapshot_do_not_duplicate(view):
    a, b = key("receive"), key("send", "d" * 32)
    view.load_snapshot([info(a, "reconnecting"), info(b, "transferring")])
    view.load_snapshot([info(a, "reconnecting"), info(b, "transferring")])
    view.handle(resumed(a))
    assert [r.key for r in view.rows()] == [a, b]


def test_snapshot_rows_show_status_text(view):
    a = key("send")
    view.set_connected(PEER_ID, True)
    view.load_snapshot([info(a, "queued")])
    assert fill(WAIT_QUEUED_SEND, p=PEER) in view.row(a).detail_label.text()


def test_resumed_without_device_name_uses_id_prefix(qtbot):
    from teleport.ui.transfers_view import TransfersView

    v = TransfersView()
    qtbot.addWidget(v)
    k = key("receive")
    v.handle(resumed(k))
    assert fill(WAIT_RESUMED, p=PEER_ID[:8]) in v.row(k).detail_label.text()


# =================================================================================================
# DataUnreachable
# =================================================================================================


def test_data_unreachable_marks_unfinished_rows_of_that_device_only(view):
    view.set_device_names({PEER_ID: PEER, OTHER_ID: "May-Khac"})
    live = key("send")
    done = key("send", "d" * 32)
    other = key("send", "e" * 32, device_id=OTHER_ID)
    view.handle(status_changed(live, "reconnecting"))
    view.handle(status_changed(done, "transferring"))
    view.handle(finished(done, "cancelled_user"))
    view.handle(status_changed(other, "transferring"))
    view.handle(data_unreachable(PEER_ID))
    sentence = fill(DATA_UNREACHABLE, p=PEER)
    assert sentence in view.row(live).detail_label.text()
    assert sentence not in row_texts(view.row(done))
    assert "Không mở được kết nối truyền dữ liệu" not in row_texts(view.row(other))


# =================================================================================================
# Trống, clear()
# =================================================================================================


def _hint_shown(view, ui_helpers) -> bool:
    return any(EMPTY_HINT in t for t in ui_helpers.widget_texts(view))


def test_empty_view_shows_hint(view, ui_helpers):
    assert view.rows() == []
    assert _hint_shown(view, ui_helpers)


def test_hint_hidden_with_rows_and_back_after_clear(view, ui_helpers):
    a, b = key("send"), key("receive", "d" * 32)
    view.handle(status_changed(a, "transferring"))
    view.handle(offer_received(b))
    assert not _hint_shown(view, ui_helpers)
    assert len(view.rows()) == 2
    view.clear()
    assert view.rows() == []
    assert view.row(a) is None and view.row(b) is None
    assert _hint_shown(view, ui_helpers)
    view.handle(status_changed(a, "transferring"))
    assert len(view.rows()) == 1


def test_unknown_key_row_is_none(view):
    assert view.row(key("send", "9" * 32)) is None


# =================================================================================================
# Đổi ngôn ngữ
# =================================================================================================


def test_language_change_retranslates_rows(qtbot, view, translator):
    k = key("send")
    view.handle(status_changed(k, "transferring"))
    row = view.row(k)
    before = row.status_label.text()
    assert PREFIX not in before
    QCoreApplication.installTranslator(translator)
    qtbot.waitUntil(lambda: PREFIX in row.status_label.text(), timeout=3_000)
    assert PREFIX in row.cancel_button.text()
    QCoreApplication.removeTranslator(translator)
    qtbot.waitUntil(lambda: PREFIX not in row.status_label.text(), timeout=3_000)
    assert row.status_label.text() == before


# =================================================================================================
# Câu mục 8 trong file này khớp mvp.md
# =================================================================================================


def test_section8_sentences_match_mvp_doc():
    doc = _MVP.read_text(encoding="utf-8")
    p = {"p": "*PC-Nha*", "f": "*video.mp4*"}
    samples = [
        fill(WAIT_QUEUED_SEND, **p),
        fill(R_REJECTED_SEND, **p),
        fill(R_CANCEL_SEND_SELF, **p),
        fill(R_CANCEL_SEND_PEER, **p),
        fill(R_CANCEL_RECEIVE_SELF, p="*Laptop-Minh*"),
        fill(R_CANCEL_RECEIVE_PEER, p="*Laptop-Minh*"),
        fill(R_CANCEL_RECEIVE_PEER_ASKING, p="*Laptop-Minh*"),
        fill(R_EXPIRED_SEND, **p),
        fill(R_EXPIRED_RECEIVE, p="*Laptop-Minh*"),
        fill(R_SOURCE_CHANGED_SEND, **p),
        fill(R_SOURCE_CHANGED_RECEIVE, p="*Laptop-Minh*", f="*video.mp4*"),
        fill(R_NOT_RECEIVING_SEND, **p).replace("“teleport receive”", "`teleport receive`"),
        fill(DATA_UNREACHABLE, **p),
        SHORT_RECONNECTING,
        ETA_UNKNOWN,
    ]
    for sample in samples:
        assert sample in doc, sample
