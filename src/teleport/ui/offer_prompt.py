"""Hộp hỏi nhận file tạm thời (spec P4-3 D7 và mục 10); P4-6 thay bằng hộp thoại nhận đầy đủ.

Mỗi `OfferReceived` chưa tự nhận mở một `QMessageBox` riêng (một hộp cho mỗi `TransferKey`):
không có cha, không chặn (`NonModal`, `show()` rồi đưa lên trước, không `exec()`), chữ thuần
(`Qt.PlainText`, không HTML). Câu chính là M6, rồi tối đa `LIST_MAX` tên file, “và k file khác”,
và thư mục lưu. Tên máy gửi qua `texts.peer_label` (`display_name`; không có tên thì 8 ký tự đầu
của mã máy), tên file qua `texts.file_label` (`display_filename`).

Bấm “Nhận” / “Từ chối” phát `decided(key, accept)`; Esc hay đóng hộp bằng nút X cũng là từ chối.
Nút mặc định (Enter) là “Từ chối”: nhận file phải là một lựa chọn có chủ ý. Có `TransferFinished`
cùng `key` (`update.info.key`, ví dụ máy gửi hủy) thì hộp tự đóng, không phát gì.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QAbstractButton, QMessageBox, QPushButton, QWidget

from teleport.app.transfers import OfferReceived, TransferFinished, TransferUpdate
from teleport.core.display import format_size
from teleport.core.transfer import TransferKey
from teleport.ui import texts

LIST_MAX = 5  # số tên file liệt kê tối đa


def offer_text(offer: OfferReceived, receive_dir: Path | None) -> str:
    """Toàn bộ chữ của hộp hỏi (chữ thuần, nhiều dòng)."""
    peer = texts.peer_label(offer.device_name, offer.key.device_id)
    lines = [texts.offer_question(peer, len(offer.files), offer.total_bytes), ""]
    for name, entry in zip(offer.names[:LIST_MAX], offer.files[:LIST_MAX], strict=False):
        lines.append(f"• {texts.file_label(name)}  ({format_size(entry.size)})")
    rest = len(offer.files) - min(len(offer.files), LIST_MAX)
    if rest > 0:
        lines.append(texts.offer_more(rest))
    if receive_dir is not None:
        lines += ["", texts.offer_save_to(str(receive_dir))]
    return "\n".join(lines)


class _OfferBox(QMessageBox):
    """`QMessageBox` gọi `on_language_change` khi nhận `LanguageChange`."""

    def __init__(self, on_language_change: Callable[[], None]) -> None:
        super().__init__()
        self._on_language_change = on_language_change

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self._on_language_change()


class _Prompt:
    """Một hộp hỏi đang mở."""

    def __init__(self, offer: OfferReceived, receive_dir: Path | None) -> None:
        self.offer = offer
        self.receive_dir = receive_dir
        self.resolved = False  # đã có quyết định hoặc đã đóng vì lượt kết thúc
        self.box = _OfferBox(self.retranslate)
        self.box.setWindowModality(Qt.WindowModality.NonModal)
        self.box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.box.setTextFormat(Qt.TextFormat.PlainText)
        self.box.setIcon(QMessageBox.Icon.Question)
        self.accept_button: QPushButton = self.box.addButton("", QMessageBox.ButtonRole.AcceptRole)
        self.reject_button: QPushButton = self.box.addButton("", QMessageBox.ButtonRole.RejectRole)
        self.box.setDefaultButton(self.reject_button)
        self.box.setEscapeButton(self.reject_button)
        self.retranslate()

    def retranslate(self) -> None:
        title = texts.offer_title()
        self.box.setWindowTitle(title)
        self.box.setText(offer_text(self.offer, self.receive_dir))
        self.box.setAccessibleName(title)
        self.accept_button.setText(texts.offer_accept())
        self.reject_button.setText(texts.offer_reject())


class OfferPrompts(QObject):
    """Quản lý các hộp hỏi nhận file đang mở, mỗi `TransferKey` một hộp."""

    decided = Signal(object, bool)  # TransferKey, accept
    opened = Signal(object)  # TransferKey: vừa mở một hộp mới

    def __init__(self, receive_dir: Path | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._receive_dir = receive_dir
        self._prompts: dict[TransferKey, _Prompt] = {}

    def set_receive_dir(self, receive_dir: Path | None) -> None:
        self._receive_dir = receive_dir

    def handle(self, update: TransferUpdate) -> None:
        """Nhận mọi `TransferUpdate`: mở hộp cho `offer` cần hỏi, đóng hộp khi lượt kết thúc."""
        if isinstance(update, OfferReceived):
            if not update.auto_accepted:
                self._open(update)
        elif isinstance(update, TransferFinished):
            self._dismiss(update.info.key)

    def box(self, key: TransferKey) -> QMessageBox | None:
        """Hộp đang mở cho `key` (test, trợ năng)."""
        prompt = self._prompts.get(key)
        return None if prompt is None else prompt.box

    def keys(self) -> list[TransferKey]:
        return list(self._prompts)

    def close_all(self) -> None:
        """Đóng mọi hộp mà không quyết định (app đang thoát: node tự từ chối khi đóng)."""
        for key in list(self._prompts):
            self._dismiss(key)

    def _open(self, offer: OfferReceived) -> None:
        if offer.key in self._prompts:
            return
        prompt = _Prompt(offer, self._receive_dir)
        self._prompts[offer.key] = prompt
        prompt.box.buttonClicked.connect(lambda button: self._clicked(prompt, button))
        prompt.box.finished.connect(lambda _result: self._finished(prompt))
        box: QWidget = prompt.box
        box.show()
        box.raise_()
        box.activateWindow()
        self.opened.emit(offer.key)

    def _clicked(self, prompt: _Prompt, button: QAbstractButton) -> None:
        if prompt.resolved:
            return
        prompt.resolved = True
        self.decided.emit(prompt.offer.key, button is prompt.accept_button)

    def _finished(self, prompt: _Prompt) -> None:
        if self._prompts.get(prompt.offer.key) is prompt:
            del self._prompts[prompt.offer.key]
        if not prompt.resolved:  # đóng bằng nút X: từ chối
            prompt.resolved = True
            self.decided.emit(prompt.offer.key, False)

    def _dismiss(self, key: TransferKey) -> None:
        prompt = self._prompts.pop(key, None)
        if prompt is None:
            return
        prompt.resolved = True
        prompt.box.close()
