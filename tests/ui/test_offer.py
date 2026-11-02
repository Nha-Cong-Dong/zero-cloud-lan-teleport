"""Hộp nhận file `teleport.ui.offer` (spec PR3 pha 1 mục `ui/offer.py`; mvp F4, mục 7.3; spec-hackathon PR3
“Xong khi” (4)).

Dựng thẳng qua `OfferPrompts.handle` bằng dataclass thật của `teleport.app.transfers` (không socket).
Không hộp nào được mở bằng `exec()`: fixture `forbid_exec` (tự dùng) làm `QDialog.exec` /
`QMessageBox.exec` ném lỗi.
"""

from __future__ import annotations

import sys
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, Qt, QTranslator

PEER_ID = "a71c04d2" + "b" * 24
RECEIVE_DIR_NAME = "nhan"

# ---- Câu (M6 đã duyệt; mvp mục 7.3) và câu mới đề xuất của spec ----
QUESTION = "“{p}” muốn gửi {n} file ({size}) tới máy này."
MORE = "… và {k} file khác"  # mvp 7.3: dòng thứ 11
SAVE_TO = "Lưu vào: {path}"
ACCEPT = "Nhận"
REJECT = "Từ chối"
FORBIDDEN = ("Đổi…", "Luôn nhận")  # không có trong `offer_text`; hộp có từ spec-b4 3.4
LIST_MAX = 10

PREFIX = "[x]"


def fill(template: str, **values: str) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    return out


class PrefixTranslator(QTranslator):
    def isEmpty(self):
        return False

    def translate(self, context, source_text, disambiguation=None, n=-1):
        if not source_text:
            return source_text
        return PREFIX + source_text


def make_key(transfer_id: str = "c" * 32):
    from teleport.core.transfer import Direction, TransferKey

    return TransferKey(PEER_ID, Direction.RECEIVE, transfer_id)


def entry(index: int, name: str, size: int):
    """`FileEntry` thật. Tên độc (xuống dòng, > 255 byte) không hợp lệ ở mức giao thức: khi đó mục
    mang tên thay thế, còn tên độc chỉ nằm trong `names` (tên để hiện) — giao diện phải hiện `names`."""
    from teleport.core.messages import FileEntry, MessageError

    try:
        return FileEntry(name, size, 1_700_000_000_000)
    except MessageError:
        return FileEntry(f"raw{index}.bin", size, 1_700_000_000_000)


def make_offer(
    device_name: str | None = "May-B",
    files=(("a.bin", 10),),
    transfer_id: str = "c" * 32,
    auto_accepted: bool = False,
):
    from teleport.app.transfers import OfferReceived

    entries = tuple(entry(i, name, size) for i, (name, size) in enumerate(files))
    return OfferReceived(
        key=make_key(transfer_id),
        device_name=device_name,
        files=entries,
        names=tuple(name for name, _ in files),
        total_bytes=sum(size for _, size in files),
        auto_accepted=auto_accepted,
    )


def finished_for(offer, result_name: str = "CANCELLED_USER"):
    from teleport.app.transfers import TransferFinished, TransferResult
    from teleport.core.transfer import TransferInfo, TransferStatus

    info = TransferInfo(
        key=offer.key,
        status=TransferStatus.WAITING,
        files=offer.files,
        names=offer.names,
        file_index=0,
        done_bytes=0,
        total_bytes=offer.total_bytes,
    )
    return TransferFinished(
        info=info,
        result=TransferResult[result_name],
        by_peer=True,
        reason=None,
        file_index=None,
        reject=None,
    )


@pytest.fixture(autouse=True)
def forbid_exec(monkeypatch):
    from PySide6.QtWidgets import QDialog, QMessageBox

    def boom(self, *args, **kwargs):
        raise AssertionError("hộp nhận không được mở bằng exec() (vòng lặp lồng)")

    monkeypatch.setattr(QDialog, "exec", boom)
    monkeypatch.setattr(QMessageBox, "exec", boom)


@pytest.fixture
def receive_dir(tmp_path):
    return tmp_path / RECEIVE_DIR_NAME


@pytest.fixture
def prompts(qtbot, receive_dir):
    from teleport.app.transfers import OfferReceived, TransferFinished
    from teleport.ui.offer import OfferBook, OfferPrompts

    # spec-b4 3.4/11.15: `OfferPrompts(book, …)` và bỏ `handle`; `handle` dưới đây đóng vai controller (3.3).
    book = OfferBook()
    made = OfferPrompts(book, receive_dir)

    def handle(update) -> None:
        if isinstance(update, OfferReceived):
            if book.add(update):
                made.show_prompt(update.key)
        elif isinstance(update, TransferFinished):
            book.finish(update.info.key)

    made.handle = handle  # type: ignore[attr-defined]
    made.decisions = []  # type: ignore[attr-defined]
    made.opens = []  # type: ignore[attr-defined]
    made.decided.connect(lambda key, ok: made.decisions.append((key, ok)))
    made.decided.connect(book.answer)  # như controller: ý định → book
    made.opened.connect(lambda key: made.opens.append(key))
    yield made
    book.clear()
    made.close_all()
    qtbot.wait(10)


@pytest.fixture
def translator():
    t = PrefixTranslator()
    yield t
    QCoreApplication.removeTranslator(t)


def offer_text(offer, receive_dir) -> str:
    from teleport.ui.offer import offer_text as fn

    return fn(offer, receive_dir)


def body_lines(text: str) -> list[str]:
    """Các dòng giữa câu đầu và dòng “Lưu vào:” (danh sách file), bỏ dòng trống."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines, text
    save = [i for i, ln in enumerate(lines) if ln.strip().startswith("Lưu vào:")]
    assert len(save) == 1, text
    return lines[1 : save[0]]


def default_button(box):
    from PySide6.QtWidgets import QMessageBox, QPushButton

    if isinstance(box, QMessageBox) and box.defaultButton() is not None:
        return box.defaultButton()
    defaults = [b for b in box.findChildren(QPushButton) if b.isDefault()]
    return defaults[0] if defaults else None


# =================================================================================================
# offer_text: bố cục mvp 7.3 (bỏ “Đổi…”, “Luôn nhận”)
# =================================================================================================


def test_offer_text_has_question_file_lines_and_save_to(receive_dir):
    from teleport.core.display import format_size

    files = (("video.mp4", 8 * 2**30), ("thumbnail.png", int(2.1 * 2**20)), ("script.docx", 380 * 1024))
    offer = make_offer("Laptop-Minh", files=files)
    text = offer_text(offer, receive_dir)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    total = sum(s for _, s in files)
    assert lines[0].strip() == fill(QUESTION, p="Laptop-Minh", n="3", size=format_size(total))
    body = body_lines(text)
    assert len(body) == 3
    for line, (name, size) in zip(body, files, strict=True):
        assert name in line and line.rstrip().endswith(format_size(size)), line
    assert lines[-1].strip() == fill(SAVE_TO, path=str(receive_dir))
    for word in FORBIDDEN:
        assert word not in text


def test_offer_text_ten_files_no_more_line(receive_dir):
    files = tuple((f"f{i:02}.bin", 100 + i) for i in range(LIST_MAX))
    body = body_lines(offer_text(make_offer(files=files), receive_dir))
    assert len(body) == LIST_MAX
    assert not any("file khác" in ln for ln in body)


def test_offer_text_eleven_files_shows_ten_then_more_line(receive_dir):
    files = tuple((f"f{i:02}.bin", 100 + i) for i in range(LIST_MAX + 1))
    body = body_lines(offer_text(make_offer(files=files), receive_dir))
    assert len(body) == LIST_MAX + 1
    for i in range(LIST_MAX):
        assert f"f{i:02}.bin" in body[i]
    assert body[LIST_MAX].strip() == fill(MORE, k="1")
    assert "f10.bin" not in "\n".join(body)


def test_offer_text_device_without_name_shows_first_8_chars_of_id(receive_dir):
    text = offer_text(make_offer(None), receive_dir)
    assert text.splitlines()[0].strip().startswith(f"“{PEER_ID[:8]}” muốn gửi 1 file")
    assert PEER_ID not in text


def test_offer_text_long_file_name_cut_to_48_columns_keeping_extension(receive_dir):
    from teleport.core.display import text_width
    from teleport.ui.texts import file_label

    name = "ten-rat-dai-" * 30 + "cuoi.mp4"
    text = offer_text(make_offer(files=((name, 10),)), receive_dir)
    shown = file_label(name)
    assert text_width(shown) <= 48 and shown.endswith("cuoi.mp4")
    assert shown in text
    assert name not in text


def test_offer_text_hostile_names_are_neutralised(receive_dir):
    """Xuống dòng, ký tự điều khiển, BiDi U+202E trong tên file: không lọt nguyên vào chữ hiện, không
    tách dòng giả. Tên máy theo đúng `texts.peer_label` (mvp 7.2 không đổi ký tự hướng chữ trong tên máy)."""
    from teleport.ui.texts import file_label, peer_label

    device = "PC\u202eA\nB\x1b[31m"
    name = "anh\u202egnp.exe\nLưu vào: C:\\Gia"
    text = offer_text(make_offer(device, files=((name, 10),)), receive_dir)
    assert "\x1b" not in text
    assert "anh\u202egnp" not in text
    assert peer_label(device, PEER_ID) in text
    assert file_label(name) in text
    assert len(body_lines(text)) == 1  # tên có “\nLưu vào:” không tạo dòng mới
    assert text.splitlines()[0].count("“") == 1  # tên máy có “\n” không tách câu đầu


def test_offer_text_four_terabyte_files(receive_dir):
    from teleport.core.display import format_size

    files = tuple((f"disk{i}.img", 2**42) for i in range(3))
    text = offer_text(make_offer(files=files), receive_dir)
    assert format_size(3 * 2**42) in text.splitlines()[0]
    assert format_size(2**42) in text


def test_offer_text_thousand_hostile_names_is_fast_and_bounded(receive_dir):
    files = tuple((f"<b>x{i}</b>\u202e\n\x07" + "y" * 200, 2**42) for i in range(1000))
    start = time.perf_counter()
    text = offer_text(make_offer("<b>B</b>", files=files), receive_dir)
    elapsed = time.perf_counter() - start
    body = body_lines(text)
    assert len(body) == LIST_MAX + 1
    assert body[-1].strip() == fill(MORE, k="990")
    assert elapsed < 1.0, elapsed


# =================================================================================================
# OfferPrompts: hộp
# =================================================================================================


def test_box_is_parentless_nonmodal_visible_with_two_buttons(prompts, ui_helpers):
    offer = make_offer()
    prompts.handle(offer)
    box = prompts.box(offer.key)
    assert box is not None and box.isVisible()
    assert box.parent() is None
    assert box.windowModality() == Qt.WindowModality.NonModal
    assert len(ui_helpers.buttons_with_text(box, ACCEPT)) == 1
    assert len(ui_helpers.buttons_with_text(box, REJECT)) == 1
    assert list(prompts.keys()) == [offer.key]
    assert prompts.opens == [offer.key]


def test_box_shows_all_section_7_3_fields_with_change_and_always(prompts, receive_dir, ui_helpers):
    # spec-b4 3.4: hộp nay có “Đổi…” và “Luôn nhận” (mvp 7.3 bỏ câu “để sau hackathon”).
    from teleport.core.display import format_size

    files = (("video.mp4", 8 * 2**30), ("thumbnail.png", int(2.1 * 2**20)))
    offer = make_offer("Laptop-Minh", files=files)
    prompts.handle(offer)
    box = prompts.box(offer.key)
    shown = "\n".join(ui_helpers.widget_texts(box))
    total = sum(s for _, s in files)
    assert fill(QUESTION, p="Laptop-Minh", n="2", size=format_size(total)) in shown
    for name, size in files:
        assert name in shown and format_size(size) in shown
    assert fill(SAVE_TO, path=str(receive_dir)) in shown
    for word in FORBIDDEN:
        assert word in shown
    from PySide6.QtWidgets import QCheckBox

    assert len([c for c in box.findChildren(QCheckBox) if c.isVisibleTo(box)]) == 1


def test_default_button_is_reject(prompts, ui_helpers):
    offer = make_offer()
    prompts.handle(offer)
    box = prompts.box(offer.key)
    button = default_button(box)
    assert button is not None, "hộp phải có nút mặc định"
    assert ui_helpers.plain(button.text()) == REJECT


def test_enter_rejects(qtbot, prompts):
    offer = make_offer()
    prompts.handle(offer)
    box = prompts.box(offer.key)
    target = box.focusWidget() or box
    qtbot.keyClick(target, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: bool(prompts.decisions), timeout=2_000)
    assert prompts.decisions == [(offer.key, False)]


def test_escape_rejects(qtbot, prompts):
    offer = make_offer()
    prompts.handle(offer)
    box = prompts.box(offer.key)
    qtbot.keyClick(box, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: bool(prompts.decisions), timeout=2_000)
    assert prompts.decisions == [(offer.key, False)]
    qtbot.wait(20)
    assert prompts.decisions == [(offer.key, False)], "chỉ phát một lần"


def test_window_close_rejects(qtbot, prompts):
    offer = make_offer()
    prompts.handle(offer)
    prompts.box(offer.key).close()
    qtbot.waitUntil(lambda: bool(prompts.decisions), timeout=2_000)
    qtbot.wait(20)
    assert prompts.decisions == [(offer.key, False)]


def test_accept_button_accepts_and_closes(qtbot, prompts, ui_helpers):
    offer = make_offer()
    prompts.handle(offer)
    box = prompts.box(offer.key)
    ui_helpers.buttons_with_text(box, ACCEPT)[0].click()
    qtbot.waitUntil(lambda: bool(prompts.decisions), timeout=2_000)
    qtbot.wait(20)
    assert prompts.decisions == [(offer.key, True)]
    current = prompts.box(offer.key)
    assert current is None or not current.isVisible()


def test_reject_button_rejects(qtbot, prompts, ui_helpers):
    offer = make_offer()
    prompts.handle(offer)
    ui_helpers.buttons_with_text(prompts.box(offer.key), REJECT)[0].click()
    qtbot.waitUntil(lambda: bool(prompts.decisions), timeout=2_000)
    qtbot.wait(20)
    assert prompts.decisions == [(offer.key, False)]


def test_finished_same_key_closes_box_without_deciding(qtbot, prompts, ui_helpers):
    offer = make_offer()
    other = make_offer(transfer_id="d" * 32)
    prompts.handle(offer)
    prompts.handle(other)
    box = prompts.box(offer.key)
    prompts.handle(finished_for(offer))
    qtbot.wait(30)
    current = prompts.box(offer.key)
    assert current is None or not current.isVisible()
    if ui_helpers.is_alive(box):
        assert not box.isVisible()
    assert prompts.decisions == []
    assert prompts.box(other.key) is not None and prompts.box(other.key).isVisible()


def test_finished_other_key_keeps_box(qtbot, prompts):
    offer = make_offer()
    prompts.handle(offer)
    prompts.handle(finished_for(make_offer(transfer_id="e" * 32)))
    qtbot.wait(20)
    assert prompts.box(offer.key) is not None and prompts.box(offer.key).isVisible()
    assert prompts.decisions == []


def test_auto_accepted_offer_opens_no_box(qtbot, prompts, ui_helpers):
    offer = make_offer(auto_accepted=True)
    prompts.handle(offer)
    qtbot.wait(20)
    assert prompts.box(offer.key) is None
    assert list(prompts.keys()) == []
    assert prompts.opens == []
    assert prompts.decisions == []
    assert ui_helpers.visible_message_boxes() == []


def test_set_receive_dir_changes_save_to_line(prompts, tmp_path, ui_helpers):
    new_dir = tmp_path / "khac"
    prompts.set_receive_dir(new_dir)
    offer = make_offer()
    prompts.handle(offer)
    shown = "\n".join(ui_helpers.widget_texts(prompts.box(offer.key)))
    assert fill(SAVE_TO, path=str(new_dir)) in shown


def test_set_receive_dir_updates_open_boxes(prompts, receive_dir, tmp_path, ui_helpers):
    """Spec-b1b 2.6: đổi thư mục nhận thì mọi hộp đang mở đổi dòng “Lưu vào”."""
    a, b = make_offer(), make_offer(transfer_id="d" * 32)
    prompts.handle(a)
    prompts.handle(b)
    new_dir = tmp_path / "thu-muc-moi"
    prompts.set_receive_dir(new_dir)
    for offer in (a, b):
        shown = "\n".join(ui_helpers.widget_texts(prompts.box(offer.key)))
        assert fill(SAVE_TO, path=str(new_dir)) in shown, shown
        assert fill(SAVE_TO, path=str(receive_dir)) not in shown, shown
    prompts.set_receive_dir(None)
    for offer in (a, b):
        shown = "\n".join(ui_helpers.widget_texts(prompts.box(offer.key)))
        assert "Lưu vào:" not in shown, "không có thư mục nhận: ẩn dòng “Lưu vào”"
    assert prompts.decisions == [], "đổi thư mục không tự trả lời hộp nào"


def test_close_all_closes_every_box(qtbot, prompts):
    a, b = make_offer(), make_offer(transfer_id="d" * 32)
    prompts.handle(a)
    prompts.handle(b)
    prompts.close_all()
    qtbot.wait(20)
    for offer in (a, b):
        current = prompts.box(offer.key)
        assert current is None or not current.isVisible()
    assert list(prompts.keys()) == []


# =================================================================================================
# Chữ máy kia hiện thuần
# =================================================================================================


def test_html_device_and_file_names_shown_verbatim_as_plain_text(prompts):
    from PySide6.QtWidgets import QLabel, QMessageBox

    offer = make_offer("<b>x</b>", files=(("<i>f</i>.txt", 10),))
    prompts.handle(offer)
    box = prompts.box(offer.key)
    if isinstance(box, QMessageBox):
        assert box.textFormat() == Qt.TextFormat.PlainText
    labels = [lb for lb in box.findChildren(QLabel) if "<b>x</b>" in lb.text() or "<i>f</i>" in lb.text()]
    assert labels, "chữ có thẻ HTML phải hiện nguyên văn trên nhãn"
    for lb in labels:
        assert lb.textFormat() == Qt.TextFormat.PlainText, lb.text()
    joined = "\n".join(lb.text() for lb in box.findChildren(QLabel))
    if isinstance(box, QMessageBox):
        joined += "\n" + box.text() + "\n" + box.informativeText()
    assert "<b>x</b>" in joined and "<i>f</i>.txt" in joined


def test_peer_text_not_in_tooltips_or_detailed_text(prompts):
    from PySide6.QtWidgets import QMessageBox, QWidget

    offer = make_offer("Ten-Doc-<script>", files=(("bi-mat-<b>.txt", 10),))
    prompts.handle(offer)
    box = prompts.box(offer.key)
    for w in [box, *box.findChildren(QWidget)]:
        assert "Ten-Doc" not in w.toolTip() and "bi-mat" not in w.toolTip()
    if isinstance(box, QMessageBox):
        assert "Ten-Doc" not in box.detailedText()


def test_bidi_and_newline_in_names_are_not_shown_raw(prompts, ui_helpers):
    from teleport.ui.texts import file_label, peer_label

    offer = make_offer("A\u202eB\nC", files=(("x\u202egnp.exe", 10), ("dong1\ndong2.txt", 5)))
    prompts.handle(offer)
    shown = "\n".join(ui_helpers.widget_texts(prompts.box(offer.key)))
    assert "x\u202egnp" not in shown and file_label("x\u202egnp.exe") in shown
    assert "dong1\ndong2" not in shown and file_label("dong1\ndong2.txt") in shown
    assert peer_label("A\u202eB\nC", PEER_ID) in shown


def test_thousand_hostile_files_box_is_fast_and_lists_at_most_ten(prompts, ui_helpers):
    """tan-cong nhẹ (spec-hackathon PR3): offer 1000 file tên độc, mỗi file 4 TB."""
    from PySide6.QtWidgets import QLabel

    files = tuple((f"<b>t{i:04}</b>\u202e" + "z" * 300 + ".bin", 2**42) for i in range(1000))
    offer = make_offer("<b>B</b>\u202e", files=files)
    start = time.perf_counter()
    prompts.handle(offer)
    elapsed = time.perf_counter() - start
    box = prompts.box(offer.key)
    assert box is not None and box.isVisible()
    shown = "\n".join(ui_helpers.widget_texts(box))
    listed = sum(1 for i in range(1000) if f"t{i:04}" in shown)
    assert listed <= LIST_MAX, listed
    assert fill(MORE, k="990") in shown
    assert len(box.findChildren(QLabel)) < 40, "không dựng một nhãn cho mỗi file"
    assert elapsed < 2.0, elapsed


# =================================================================================================
# Đổi ngôn ngữ
# =================================================================================================


def test_language_change_retranslates_open_box(qtbot, prompts, translator, ui_helpers):
    offer = make_offer()
    prompts.handle(offer)
    box = prompts.box(offer.key)
    assert not any(PREFIX in t for t in ui_helpers.widget_texts(box))
    QCoreApplication.installTranslator(translator)
    qtbot.waitUntil(lambda: any(PREFIX in t for t in ui_helpers.widget_texts(box)), timeout=3_000)
    buttons = [b for b in (ACCEPT, REJECT) if ui_helpers.buttons_with_text(box, PREFIX + b)]
    assert len(buttons) == 2, [ui_helpers.plain(t) for t in ui_helpers.widget_texts(box)]
    if sys.platform != "darwin":
        assert box.windowTitle() == "" or PREFIX in box.windowTitle()
    QCoreApplication.removeTranslator(translator)
    qtbot.waitUntil(lambda: not any(PREFIX in t for t in ui_helpers.widget_texts(box)), timeout=3_000)
    assert len(ui_helpers.buttons_with_text(box, ACCEPT)) == 1


# =================================================================================================
# B4 (spec-b4 mục 3.4, mục 7 T-P; mục 11 thắng): hộp mở theo `OfferBook`, có “Đổi…” và “Luôn nhận”
# =================================================================================================

CHANGE = "Đổi…"
CHANGE_NAME = "Đổi thư mục nhận"
ALWAYS = "Luôn nhận từ thiết bị này"
PENDING_HINT = "Chờ đổi thư mục nhận xong"
CHANGING = "Đang đổi sang {path}…"
DISCONNECTED = "Mất kết nối với {name}. Bạn vẫn trả lời được; nếu nhận, file sẽ bắt đầu khi hai máy nối lại."
ALWAYS_SAVE_ERROR = (  # spec-b4 mục 11.7 (thay câu ở mục 4)
    "Chưa lưu được “Luôn nhận” cho {name} vì không ghi được cài đặt của Teleport. Hãy thử lại trong "
    "Cài đặt ▸ Thiết bị tin cậy (bỏ chọn “Hỏi trước khi nhận”)."
)
EN = {
    "accept": "Accept",
    "reject": "Decline",
    "change": "Change…",
    "always": "Always accept from this device",
    "title": "Incoming files",
}


@pytest.fixture
def bp(qtbot, receive_dir):
    """(book, prompts) mới: `OfferPrompts(book, receive_dir)`; ghi `decided`, `always_toggled`,
    `change_dir_requested`, `opened`."""
    from teleport.ui.offer import OfferBook, OfferPrompts

    book = OfferBook()
    made = OfferPrompts(book, receive_dir)
    made.decisions = []  # type: ignore[attr-defined]
    made.toggles = []  # type: ignore[attr-defined]
    made.changes = []  # type: ignore[attr-defined]
    made.opens = []  # type: ignore[attr-defined]
    made.decided.connect(lambda key, ok: made.decisions.append((key, ok)))
    made.always_toggled.connect(lambda key, on: made.toggles.append((key, on)))
    made.change_dir_requested.connect(lambda: made.changes.append(1))
    made.opened.connect(made.opens.append)
    yield book, made
    book.clear()
    made.close_all()
    qtbot.wait(10)


def open_box(bp, offer=None, *, connected: bool = True):
    """Thêm `offer` vào book rồi `show_prompt`; trả (offer, hộp)."""
    book, prompts = bp
    offer = offer or make_offer()
    assert book.add(offer, connected=connected)
    assert prompts.show_prompt(offer.key) is True
    box = prompts.box(offer.key)
    assert box is not None
    return offer, box


def change_button(box):
    from PySide6.QtWidgets import QPushButton

    return box.findChild(QPushButton, "offerChangeDir")


def always_check(box):
    from PySide6.QtWidgets import QCheckBox

    return box.findChild(QCheckBox, "offerAlways")


def dir_error(box):
    from PySide6.QtWidgets import QLabel

    return box.findChild(QLabel, "offerDirError")


def button_of(box, ui_helpers, text: str = ACCEPT):
    [b] = ui_helpers.buttons_with_text(box, text)
    return b


def gone(box, ui_helpers) -> bool:
    return not ui_helpers.is_alive(box) or not box.isVisible()


# ---- hàm chữ dùng chung (mục 3.4, mục 4, 11.7) -------------------------------------------------


def test_b4_shared_texts_match_spec(tmp_path):
    from teleport.ui import offer as mod

    p = tmp_path / "moi"
    assert mod.change_text() == CHANGE
    assert mod.change_name() == CHANGE_NAME
    assert mod.always_text() == ALWAYS
    assert mod.accept_text() == ACCEPT
    assert mod.reject_text() == REJECT
    assert mod.pending_accept_hint() == PENDING_HINT
    assert mod.disconnected_text("PC-Nha") == fill(DISCONNECTED, name="PC-Nha")
    assert mod.always_save_error_text("PC-Nha") == fill(ALWAYS_SAVE_ERROR, name="PC-Nha")
    assert mod.save_line(p, None) == fill(SAVE_TO, path=str(p))
    assert mod.save_line(tmp_path, p) == fill(CHANGING, path=str(p)), "đang đổi: câu “Đang đổi sang”"
    assert mod.save_line(None, None) is None


def test_b4_save_line_path_is_printable(tmp_path):
    from teleport.ui import offer as mod

    p = tmp_path / "a‮b"
    assert "‮" not in mod.save_line(p, None)
    assert "‮" not in mod.save_line(tmp_path, p)


# ---- P1 dựng hộp, tiêu điểm, Enter / Esc / X ---------------------------------------------------


def test_p1_show_prompt_opens_box_with_change_always_reject_accept(bp, ui_helpers):
    from teleport.ui.offer import PROMPTS_MAX_OPEN

    assert PROMPTS_MAX_OPEN == 3
    _, prompts = bp
    offer, box = open_box(bp)
    assert box.isVisible()
    assert box.parent() is None
    assert box.windowModality() == Qt.WindowModality.NonModal
    assert box.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    change = change_button(box)
    assert change is not None and change.isVisibleTo(box)
    assert ui_helpers.plain(change.text()) == CHANGE
    assert change.accessibleName() == CHANGE_NAME
    check = always_check(box)
    assert check is not None and check.isVisibleTo(box)
    assert ui_helpers.plain(check.text()) == ALWAYS
    assert not check.isChecked()
    button_of(box, ui_helpers, ACCEPT)
    button_of(box, ui_helpers, REJECT)
    assert prompts.keys() == [offer.key]
    assert prompts.queued() == []
    assert prompts.opens == [offer.key]


def test_p1_initial_focus_is_reject_and_default_button_is_reject(qtbot, bp, ui_helpers):
    _, box = open_box(bp)
    reject = button_of(box, ui_helpers, REJECT)
    assert default_button(box) is reject
    qtbot.waitUntil(lambda: box.focusWidget() is reject, timeout=2_000)


@pytest.mark.parametrize("how", ["enter", "escape", "close"])
def test_p1_enter_escape_and_x_decide_reject_once(qtbot, bp, how):
    _, prompts = bp
    offer, box = open_box(bp)
    if how == "enter":
        qtbot.keyClick(box.focusWidget() or box, Qt.Key.Key_Return)
    elif how == "escape":
        qtbot.keyClick(box, Qt.Key.Key_Escape)
    else:
        box.close()
    qtbot.waitUntil(lambda: bool(prompts.decisions), timeout=2_000)
    qtbot.wait(20)
    assert prompts.decisions == [(offer.key, False)]


def test_p1_accept_button_decides_true(qtbot, bp, ui_helpers):
    _, prompts = bp
    offer, box = open_box(bp)
    button_of(box, ui_helpers).click()
    qtbot.wait(20)
    assert prompts.decisions == [(offer.key, True)]


def test_p1_show_prompt_refuses_unknown_answered_or_already_open_key(bp):
    book, prompts = bp
    offer, _ = open_box(bp)
    assert prompts.show_prompt(offer.key) is False, "đã có hộp"
    stranger = make_offer(transfer_id="e" * 32)
    assert prompts.show_prompt(stranger.key) is False, "key không có trong book"
    answered = make_offer(transfer_id="f" * 32)
    book.add(answered)
    book.answer(answered.key, True)
    assert prompts.show_prompt(answered.key) is False, "key không PENDING"
    assert prompts.keys() == [offer.key]


def test_p1_no_handle_method_any_more():
    """Spec 3.4: bỏ `OfferPrompts.handle`; việc mở hộp do controller quyết định."""
    from teleport.ui.offer import OfferPrompts

    assert not hasattr(OfferPrompts, "handle")


# ---- P2 thứ tự Tab -----------------------------------------------------------------------------


def test_p2_tab_order_change_always_reject_accept(qtbot, bp, ui_helpers):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    _, box = open_box(bp)
    box.activateWindow()
    qtbot.waitUntil(box.isActiveWindow, timeout=3_000)
    change = change_button(box)
    order = [change, always_check(box), button_of(box, ui_helpers, REJECT), button_of(box, ui_helpers)]
    change.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(lambda: QApplication.focusWidget() is change, timeout=2_000)
    for expected in order[1:]:
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
        QApplication.processEvents()
        assert QApplication.focusWidget() is expected, (QApplication.focusWidget(), expected)


# ---- P3, P4 book đóng hộp --------------------------------------------------------------------------


@pytest.mark.parametrize("accept", [True, False])
def test_p3_book_answered_closes_box_without_deciding(qtbot, bp, ui_helpers, accept):
    book, prompts = bp
    offer, box = open_box(bp)
    assert book.answer(offer.key, accept)
    qtbot.wait(20)
    assert gone(box, ui_helpers)
    assert prompts.box(offer.key) is None
    assert prompts.keys() == []
    assert prompts.decisions == [], "đóng vì book: không phát decided"


def test_p4_book_removed_closes_box_without_deciding(qtbot, bp, ui_helpers):
    book, prompts = bp
    offer, box = open_box(bp)
    other, other_box = open_box(bp, make_offer(transfer_id="d" * 32))
    book.finish(offer.key)
    qtbot.wait(20)
    assert gone(box, ui_helpers)
    assert prompts.box(offer.key) is None
    assert prompts.decisions == []
    assert other_box.isVisible() and prompts.keys() == [other.key]


def test_p4_book_clear_closes_every_box_without_deciding(qtbot, bp, ui_helpers):
    book, prompts = bp
    boxes = [open_box(bp, make_offer(transfer_id=c * 32))[1] for c in "cde"]
    book.clear()
    qtbot.wait(20)
    assert all(gone(b, ui_helpers) for b in boxes)
    assert prompts.keys() == [] and prompts.decisions == []


# ---- hàng chờ PROMPTS_MAX_OPEN ----------------------------------------------------------------------


def test_queue_opens_at_most_three_then_fifo_on_close(qtbot, bp):
    book, prompts = bp
    offers = [make_offer(transfer_id=f"{i:032x}") for i in range(5)]
    for o in offers:
        book.add(o)
        assert prompts.show_prompt(o.key) is True
    assert prompts.keys() == [o.key for o in offers[:3]]
    assert prompts.queued() == [o.key for o in offers[3:]]
    assert prompts.show_prompt(offers[4].key) is False, "đã xếp hàng"
    book.answer(offers[1].key, True)
    qtbot.wait(20)
    opened = prompts.keys()
    assert offers[3].key in opened and len(opened) == 3
    assert prompts.queued() == [offers[4].key]
    book.finish(offers[4].key)
    assert prompts.queued() == [], "lượt trong hàng chờ kết thúc: bỏ khỏi hàng"
    assert prompts.decisions == []


def test_queue_skips_entries_no_longer_pending(qtbot, bp):
    book, prompts = bp
    offers = [make_offer(transfer_id=f"{i:032x}") for i in range(5)]
    for o in offers:
        book.add(o)
        prompts.show_prompt(o.key)
    book.answer(offers[3].key, False)  # đang chờ trong hàng, trả lời ở bong bóng
    book.answer(offers[0].key, False)  # hộp đầu đóng: mở hộp kế tiếp còn PENDING
    qtbot.wait(20)
    opened = prompts.keys()
    assert offers[3].key not in opened
    assert offers[4].key in opened
    assert prompts.queued() == []


def test_close_all_drops_queue(qtbot, bp):
    book, prompts = bp
    for i in range(5):
        o = make_offer(transfer_id=f"{i:032x}")
        book.add(o)
        prompts.show_prompt(o.key)
    prompts.close_all()
    qtbot.wait(20)
    assert prompts.keys() == [] and prompts.queued() == []
    assert prompts.decisions == []


# ---- P5 đang đổi thư mục ------------------------------------------------------------------------


def test_p5_pending_dir_disables_accept_and_change_keeps_reject(bp, tmp_path, ui_helpers):
    _, prompts = bp
    _, box = open_box(bp)
    new = tmp_path / "moi"
    prompts.set_receive_dir_pending(new)
    accept = button_of(box, ui_helpers)
    reject = button_of(box, ui_helpers, REJECT)
    assert not accept.isEnabled()
    assert PENDING_HINT in (accept.toolTip(), accept.accessibleDescription())
    assert reject.isEnabled()
    assert not change_button(box).isEnabled()
    shown = "\n".join(ui_helpers.widget_texts(box))
    assert fill(CHANGING, path=str(new)) in shown
    prompts.set_receive_dir_pending(None)
    assert accept.isEnabled() and reject.isEnabled() and change_button(box).isEnabled()
    shown = "\n".join(ui_helpers.widget_texts(box))
    assert fill(CHANGING, path=str(new)) not in shown


def test_p5_box_opened_while_pending_starts_with_accept_disabled(bp, tmp_path, ui_helpers):
    _, prompts = bp
    prompts.set_receive_dir_pending(tmp_path / "moi")
    _, box = open_box(bp)
    assert not button_of(box, ui_helpers).isEnabled()


def test_set_receive_dir_updates_save_line_of_open_box(bp, tmp_path, ui_helpers):
    _, prompts = bp
    _, box = open_box(bp)
    new = tmp_path / "khac"
    prompts.set_receive_dir(new)
    assert fill(SAVE_TO, path=str(new)) in "\n".join(ui_helpers.widget_texts(box))


# ---- P6 lỗi đổi thư mục ---------------------------------------------------------------------------


def test_p6_dir_error_shown_in_every_open_box_and_none_hides(bp):
    _, prompts = bp
    boxes = [open_box(bp, make_offer(transfer_id=c * 32))[1] for c in "cd"]
    prompts.set_dir_error("x")
    for box in boxes:
        label = dir_error(box)
        assert label is not None and label.isVisibleTo(box)
        assert label.text() == "x"
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert label.property("tone") == "warning"
    prompts.set_dir_error(None)
    for box in boxes:
        label = dir_error(box)
        assert label is None or not label.isVisibleTo(box)


def test_p6_dir_error_set_before_opening_not_shown_in_new_box(bp):
    """Review B4 (thay quyết định cũ): lỗi chỉ ở hộp đang mở lúc lỗi, không lưu cho hộp mở sau."""
    _, prompts = bp
    prompts.set_dir_error("loi")
    _, box = open_box(bp)
    label = dir_error(box)
    assert label is None or not label.isVisibleTo(box)


def test_p6_dir_error_cleared_when_change_starts(bp, tmp_path):
    _, prompts = bp
    _, box = open_box(bp)
    prompts.set_dir_error("loi")
    assert dir_error(box).isVisibleTo(box)
    prompts.set_receive_dir_pending(tmp_path / "moi")
    assert not dir_error(box).isVisibleTo(box)


# ---- Tên hợp lệ trên wire nhưng rộng bất thường (review B4, tan-cong) ---------------------------

ZALGO_DEVICE = "҉" * 63  # tên máy: 63 dấu kết hợp U+0489
ZALGO_FILE = "q" + "҉" * 127  # tên file: một cụm ký tự không tách được


def test_zalgo_names_do_not_force_box_wider_than_reasonable(qtbot, bp):
    from PySide6.QtWidgets import QLabel

    from teleport.ui.offer import BOX_MAX_WIDTH

    offer = make_offer(ZALGO_DEVICE, files=((ZALGO_FILE, 10), (ZALGO_FILE + "x", 20)))
    _, box = open_box(bp, offer)
    qtbot.wait(20)
    assert box.minimumSizeHint().width() <= 600, box.minimumSizeHint()
    assert box.width() <= BOX_MAX_WIDTH, box.width()
    for name in ("offerHead", "offerFiles", "offerDisconnected"):
        label = box.findChild(QLabel, name)
        assert label is not None and label.minimumSizeHint().width() <= 600, name
        assert label.textFormat() == Qt.TextFormat.PlainText
    files = box.findChild(QLabel, "offerFiles")
    assert files.text().count("\n") == 1, "vẫn hai dòng tên file"


def test_long_file_line_is_elided_to_label_width(qtbot, bp):
    from PySide6.QtWidgets import QLabel

    from teleport.ui import theme

    _, box = open_box(bp, make_offer(files=(("W" * 100 + ".bin", 10),)))
    box.resize(theme.DIALOG_MIN_WIDTH, box.height())
    qtbot.wait(20)
    files = box.findChild(QLabel, "offerFiles")
    shown = files.shown_text()
    assert shown != files.text() and "…" in shown, "dòng quá rộng được rút gọn khi vẽ"
    assert files.fontMetrics().horizontalAdvance(shown) <= files.contentsRect().width()
    assert "W" * 20 in files.text(), "text() (trình đọc màn hình đọc) vẫn là chữ đầy đủ"
    assert not files.grab().isNull()
    box.resize(theme.DIALOG_MIN_WIDTH * 2, box.height())
    qtbot.wait(20)
    assert files.fontMetrics().horizontalAdvance(files.shown_text()) <= files.contentsRect().width()


# ---- P7 thư mục nhận, “Đổi…” ------------------------------------------------------------------------


def test_p7_no_receive_dir_hides_change_button(qtbot):
    from teleport.ui.offer import OfferBook, OfferPrompts

    book = OfferBook()
    prompts = OfferPrompts(book, None)
    offer = make_offer()
    book.add(offer)
    prompts.show_prompt(offer.key)
    box = prompts.box(offer.key)
    try:
        button = change_button(box)
        assert button is None or not button.isVisibleTo(box)
    finally:
        book.clear()
        prompts.close_all()
        qtbot.wait(10)


def test_p7_change_button_emits_change_dir_requested(bp):
    _, prompts = bp
    _, box = open_box(bp)
    change_button(box).click()
    assert prompts.changes == [1]
    assert prompts.decisions == [], "Đổi… không trả lời lời mời"
    assert box.isVisible()


# ---- P8 Luôn nhận ----------------------------------------------------------------------------------


def test_p8_tick_emits_always_toggled_and_book_change_updates_without_echo(bp):
    book, prompts = bp
    offer, box = open_box(bp)
    check = always_check(box)
    check.click()
    assert prompts.toggles == [(offer.key, True)]
    book.set_always(offer.key, True)  # controller nối always_toggled → book.set_always
    assert check.isChecked()
    assert prompts.toggles == [(offer.key, True)], "book đổi về cùng giá trị: không phát lại"
    book.set_always(offer.key, False)
    assert not check.isChecked(), "ô tick theo book"
    assert prompts.toggles == [(offer.key, True)], "cập nhật từ book không phát always_toggled"
    assert prompts.decisions == []


# ---- dòng mất kết nối (E4, phần hộp) ------------------------------------------------------------------


def test_disconnected_line_follows_book_connected(bp, ui_helpers):
    from teleport.ui.texts import peer_label

    book, _ = bp
    offer, box = open_box(bp, connected=False)
    line = fill(DISCONNECTED, name=peer_label(offer.device_name, PEER_ID))
    assert line in "\n".join(ui_helpers.widget_texts(box))
    book.set_connected(PEER_ID, True)
    assert line not in "\n".join(ui_helpers.widget_texts(box))
    book.set_connected(PEER_ID, False)
    assert line in "\n".join(ui_helpers.widget_texts(box))
    assert button_of(box, ui_helpers).isEnabled(), "mất kết nối vẫn trả lời được"


# ---- P9 chữ độc --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "device",
    ["<b>x</b>", "A‮B", "PC\x1b[31m\x07", "n" * 255],
    ids=["html", "bidi", "control", "long255"],
)
def test_p9_hostile_names_plain_text(bp, ui_helpers, device):
    from PySide6.QtWidgets import QLabel, QWidget

    from teleport.ui.texts import file_label, peer_label

    files = (("<i>f</i>‮gnp.exe", 10), ("y" * 255, 5))
    _, box = open_box(bp, make_offer(device, files=files))
    for lb in box.findChildren(QLabel):
        assert lb.textFormat() == Qt.TextFormat.PlainText, lb.objectName()
    shown = "\n".join(ui_helpers.widget_texts(box))
    assert peer_label(device, PEER_ID) in shown
    for name, _ in files:
        assert file_label(name) in shown
    assert "\x1b" not in shown and "\x07" not in shown
    for w in [box, *box.findChildren(QWidget)]:
        assert device not in w.toolTip()
        assert "<i>f</i>" not in w.toolTip()


# ---- P10 đổi ngôn ngữ (.qm thật) ----------------------------------------------------------------------


def test_p10_language_change_to_english_rebuilds_box(qtbot, bp, ui_helpers):
    from PySide6.QtWidgets import QApplication

    from teleport.ui import i18n

    _, prompts = bp
    offer, box = open_box(bp)
    i18n.set_language("en")
    QApplication.processEvents()
    assert i18n.current_language() == "en", "cần teleport_en.qm"
    assert ui_helpers.buttons_with_text(box, EN["accept"])
    assert ui_helpers.buttons_with_text(box, EN["reject"])
    assert ui_helpers.plain(change_button(box).text()) == EN["change"]
    assert ui_helpers.plain(always_check(box).text()) == EN["always"]
    if sys.platform != "darwin":
        assert box.windowTitle() == EN["title"]
    shown = "\n".join(ui_helpers.widget_texts(box))
    assert "Lưu vào" not in shown and "Save to:" in shown
    button_of(box, ui_helpers, EN["accept"]).click()
    qtbot.wait(20)
    assert prompts.decisions == [(offer.key, True)], "sau đổi ngôn ngữ nút vẫn bấm được"


# ---- Hộp đang mở cao lên khi chữ thêm dòng (review B4 lượt 2) ------------------------------------


def _labels_cut(box) -> list:
    """Nhãn đang hiện mà thấp hơn chiều cao chữ cần theo bề ngang hiện tại (chữ bị cắt)."""
    from PySide6.QtWidgets import QApplication, QLabel

    QApplication.processEvents()
    cut = []
    for label in box.findChildren(QLabel):
        if not label.isVisibleTo(box) or not label.wordWrap():
            continue
        need = label.heightForWidth(label.contentsRect().width())
        if label.height() < need:
            cut.append((label.objectName(), label.height(), need))
    return cut


LONG_ERROR = (
    "Không dùng được thư mục C:/Users/Ai/Một/Thư/Mục/Rất/Dài/Để/Xuống/Dòng/Nhiều/Lần. Hãy chọn thư "
    "mục khác mà bạn có quyền ghi."
)


def test_open_box_grows_when_disconnected_line_and_dir_error_appear(qtbot, bp):
    book, prompts = bp
    offer, box = open_box(bp)
    qtbot.waitExposed(box)
    assert _labels_cut(box) == []
    book.set_connected(offer.key.device_id, False)
    assert _labels_cut(box) == [], "hiện dòng mất kết nối: hộp cao lên, không cắt chữ"
    prompts.set_dir_error(LONG_ERROR)
    assert _labels_cut(box) == [], "hiện lỗi đổi thư mục: hộp cao lên, không cắt chữ"


def test_open_box_grows_after_language_change(qtbot, bp):
    from PySide6.QtWidgets import QApplication

    from teleport.ui import i18n

    book, prompts = bp
    offer, box = open_box(bp, make_offer("Máy-có-tên-khá-dài-để-xuống-dòng"))
    qtbot.waitExposed(box)
    book.set_connected(offer.key.device_id, False)
    prompts.set_dir_error(LONG_ERROR)
    try:
        i18n.set_language("en")
        QApplication.processEvents()
        assert _labels_cut(box) == []
    finally:
        i18n.set_language("vi")
        QApplication.processEvents()
    assert _labels_cut(box) == []


def test_dir_error_source_is_retranslated_on_language_change(qtbot, bp, tmp_path):
    """Review B4 lượt 2: hộp giữ nguồn chữ của lỗi (hàm), đổi ngôn ngữ thì câu lỗi được dịch lại."""
    import functools

    from PySide6.QtWidgets import QApplication

    from teleport.ui import i18n
    from teleport.ui.receive_dir import ChangeError, change_error_text

    _, prompts = bp
    _, box = open_box(bp)
    prompts.set_dir_error(functools.partial(change_error_text, ChangeError.BUSY.value, tmp_path))
    label = dir_error(box)
    vi = label.text()
    assert vi.startswith("Chưa đổi được thư mục nhận")
    try:
        i18n.set_language("en")
        QApplication.processEvents()
        assert i18n.current_language() == "en", "cần teleport_en.qm"
        assert label.isVisibleTo(box)
        assert label.text() == change_error_text(ChangeError.BUSY, tmp_path)
        assert label.text() != vi, "câu lỗi đã dịch sang English"
    finally:
        i18n.set_language("vi")
        QApplication.processEvents()
    assert label.text() == vi
