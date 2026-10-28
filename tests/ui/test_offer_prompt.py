"""Hộp hỏi offer dựng thẳng qua `teleport.ui.offer_prompt.OfferPrompts.handle` (không socket).

spec-p4-3 D7, mục 6.3, 6.7 và mục 10: chữ thuần (dữ liệu máy kia gửi không được hiểu là HTML), đổi ngôn
ngữ thì hộp đang mở dịch lại.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, Qt, QTranslator
from PySide6.QtWidgets import QLabel

PREFIX = "[x]"
PEER_ID = "b" * 32


class PrefixTranslator(QTranslator):
    def isEmpty(self):
        # Qt chỉ phát `LanguageChange` khi cài/gỡ một translator không rỗng.
        return False

    def translate(self, context, source_text, disambiguation=None, n=-1):
        if not source_text:
            return source_text
        return PREFIX + source_text


def make_offer(device_name: str | None = "May-B", files=(("a.bin", 10),), transfer_id: str = "c" * 32):
    from teleport.app.transfers import OfferReceived
    from teleport.core.messages import FileEntry
    from teleport.core.transfer import Direction, TransferKey

    key = TransferKey(PEER_ID, Direction.RECEIVE, transfer_id)
    entries = tuple(FileEntry(name, size, 1_700_000_000_000) for name, size in files)
    return OfferReceived(
        key=key,
        device_name=device_name,
        files=entries,
        names=tuple(name for name, _ in files),
        total_bytes=sum(size for _, size in files),
        auto_accepted=False,
    )


@pytest.fixture
def prompts(qtbot, tmp_path):
    from teleport.ui.offer_prompt import OfferPrompts

    made = OfferPrompts(tmp_path / "nhan")
    yield made
    made.close_all()
    qtbot.wait(10)


@pytest.fixture
def translator():
    t = PrefixTranslator()
    yield t
    QCoreApplication.removeTranslator(t)


def box_texts(box, ui_helpers) -> list[str]:
    return ui_helpers.widget_texts(box)


def test_html_in_device_and_file_name_is_shown_verbatim(prompts, ui_helpers):
    """Tên do máy kia đặt có thẻ HTML → hiện nguyên văn, không in đậm/nghiêng (Qt.PlainText)."""
    offer = make_offer("<b>x</b>", files=(("<i>f</i>.txt", 10),))
    prompts.handle(offer)
    box = prompts.box(offer.key)
    assert box is not None and box.isVisible()
    assert box.textFormat() == Qt.TextFormat.PlainText
    assert "<b>x</b>" in box.text()
    assert "<i>f</i>.txt" in box.text()
    labels = [lb for lb in box.findChildren(QLabel) if "<b>x</b>" in lb.text()]
    assert labels, "nhãn của hộp phải mang nguyên văn tên có thẻ HTML"
    for lb in labels:
        assert lb.textFormat() == Qt.TextFormat.PlainText, "nhãn không được hiểu chữ là rich text"


def test_offer_box_is_parentless_nonmodal_with_accept_and_reject(prompts, ui_helpers):
    offer = make_offer()
    prompts.handle(offer)
    box = prompts.box(offer.key)
    assert box.parent() is None
    assert box.windowModality() == Qt.WindowModality.NonModal
    assert len(ui_helpers.buttons_with_text(box, ui_helpers.M6_ACCEPT)) == 1
    assert len(ui_helpers.buttons_with_text(box, ui_helpers.M6_REJECT)) == 1
    assert "“May-B” muốn gửi 1 file (" in box.text()


def test_language_change_retranslates_open_offer_box(qtbot, prompts, translator, ui_helpers):
    """6.7: cài translator khi hộp đang mở → `LanguageChange` → chữ của hộp (câu, nút, tiêu đề) có tiền
    tố; gỡ translator → về chuỗi nguồn."""
    offer = make_offer()
    prompts.handle(offer)
    box = prompts.box(offer.key)
    assert not any(PREFIX in t for t in box_texts(box, ui_helpers))
    QCoreApplication.installTranslator(translator)
    qtbot.waitUntil(lambda: PREFIX in box.text(), timeout=3_000)
    buttons = [ui_helpers.plain(b.text()) for b in box.buttons()]
    assert buttons and all(PREFIX in t for t in buttons), buttons
    assert PREFIX in box.windowTitle()
    QCoreApplication.removeTranslator(translator)
    qtbot.waitUntil(lambda: PREFIX not in box.text(), timeout=3_000)
    assert len(ui_helpers.buttons_with_text(box, ui_helpers.M6_ACCEPT)) == 1


def test_offer_box_opened_after_language_change_is_translated(prompts, translator, ui_helpers):
    QCoreApplication.installTranslator(translator)
    offer = make_offer()
    prompts.handle(offer)
    box = prompts.box(offer.key)
    assert PREFIX in box.text()
    assert all(PREFIX in ui_helpers.plain(b.text()) for b in box.buttons())
