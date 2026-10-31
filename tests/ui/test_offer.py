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
FORBIDDEN = ("Đổi…", "Luôn nhận")  # Q6: bỏ cả hai
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
    from teleport.ui.offer import OfferPrompts

    made = OfferPrompts(receive_dir)
    made.decisions = []  # type: ignore[attr-defined]
    made.opens = []  # type: ignore[attr-defined]
    made.decided.connect(lambda key, ok: made.decisions.append((key, ok)))
    made.opened.connect(lambda key: made.opens.append(key))
    yield made
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


def test_box_shows_all_section_7_3_fields_without_change_or_always(prompts, receive_dir, ui_helpers):
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
        assert word not in shown
    from PySide6.QtWidgets import QCheckBox

    assert not [c for c in box.findChildren(QCheckBox) if c.isVisibleTo(box)]


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
