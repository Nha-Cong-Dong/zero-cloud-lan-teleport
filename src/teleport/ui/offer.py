"""Hộp hỏi nhận file (mvp mục 7.3, bản giao diện tối thiểu).

Mỗi `OfferReceived` chưa tự nhận mở một `QDialog` riêng (một hộp cho mỗi `TransferKey`): không có
cha, không chặn (`NonModal`, `show()` rồi đưa lên trước, không `exec()`). Bố cục kiểu AirDrop /
LocalSend, theo mvp mục 7.3 bỏ “Đổi…” và “Luôn nhận”:

- đầu hộp, chữ lớn và đậm (đặt bằng font, không HTML): câu M6 đã duyệt ““X” muốn gửi n file (cỡ)
  tới máy này.” (tên máy gửi và tổng cỡ);
- danh sách file gọn: tối đa `LIST_MAX` dòng “tên  cỡ”, rồi “… và k file khác” nếu còn (chỉ dựng
  chữ cho `LIST_MAX` tên đầu, nên `offer` 1000 file vẫn nhanh);
- “Lưu vào: …” chữ nhỏ;
- nút “Từ chối” và “Nhận” căn phải; “Nhận” là nút chính (chữ đậm, tô màu nhấn qua property
  `theme.PRIMARY_PROPERTY`). Hộp rộng tối thiểu `theme.DIALOG_MIN_WIDTH`, như hộp ghép đôi.

Mọi nhãn `Qt.PlainText`; chữ máy kia không vào tooltip. Tên máy gửi qua `texts.peer_label`
(`display_name`; không có tên thì 8 ký tự đầu của mã máy), tên file qua `texts.file_label`
(`display_filename`, 48 cột, cắt giữa giữ đuôi). `offer_text` trả toàn bộ chữ đó (chữ thuần).

Bấm “Nhận” / “Từ chối” phát `decided(key, accept)`; Esc hay đóng hộp bằng nút X cũng là từ chối.
Nút mặc định (Enter) là “Từ chối”: nhận file phải là một lựa chọn có chủ ý (nút được tô màu không
phải nút mặc định). Có `TransferFinished`
cùng `key` (ví dụ máy gửi hủy) thì hộp tự đóng, không phát gì. Đổi ngôn ngữ dịch lại chữ đang hiện.
Đổi thư mục nhận (`OfferPrompts.set_receive_dir`, từ nút “Đổi…” của cửa sổ chính) cập nhật dòng
“Lưu vào” của cả các hộp đang mở.

Chuỗi qua `QCoreApplication.translate("Offer", ...)` lúc dùng.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from teleport.app.transfers import OfferReceived, TransferFinished, TransferUpdate
from teleport.core.display import format_size
from teleport.core.transfer import TransferKey
from teleport.ui import texts, theme
from teleport.ui.send import printable
from teleport.ui.texts import fill as _fill

LIST_MAX = 10  # số dòng tên file tối đa (mvp mục 7.3)
_MIN_WIDTH = theme.DIALOG_MIN_WIDTH


def offer_parts(offer: OfferReceived, receive_dir: Path | None) -> tuple[str, list[str], str | None]:
    """(câu đầu, các dòng danh sách file, dòng “Lưu vào” hay None) của hộp hỏi, chữ thuần."""
    t = QCoreApplication.translate
    peer = texts.peer_label(offer.device_name, offer.key.device_id)
    count = len(offer.files)
    head = _fill(
        t("Offer", "“{name}” muốn gửi %n file ({size}) tới máy này.", None, count),
        name=peer,
        size=format_size(max(0, offer.total_bytes)),
    )
    lines = [
        f"{texts.file_label(name)}  {format_size(max(0, entry.size))}"
        for name, entry in zip(offer.names[:LIST_MAX], offer.files[:LIST_MAX], strict=False)
    ]
    rest = count - min(count, LIST_MAX)
    if rest > 0:
        lines.append(t("Offer", "… và %n file khác", None, rest))
    save = None
    if receive_dir is not None:
        save = _fill(t("Offer", "Lưu vào: {path}"), path=printable(str(receive_dir)))
    return head, lines, save


def offer_text(offer: OfferReceived, receive_dir: Path | None) -> str:
    """Toàn bộ chữ của hộp hỏi (chữ thuần, nhiều dòng)."""
    head, lines, save = offer_parts(offer, receive_dir)
    out = [head, "", *lines]
    if save is not None:
        out += ["", save]
    return "\n".join(out)


def _plain_label(*, wrap: bool) -> QLabel:
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    label.setWordWrap(wrap)
    return label


class _OfferBox(QDialog):
    """Hộp hỏi; gọi `on_language_change` khi nhận `LanguageChange`."""

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
        box = _OfferBox(self.retranslate)
        self.box = box
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.setMinimumWidth(_MIN_WIDTH)

        self.head_label = _plain_label(wrap=True)
        head_font = self.head_label.font()
        head_font.setBold(True)
        if head_font.pointSizeF() > 0:
            head_font.setPointSizeF(head_font.pointSizeF() * 1.25)
        self.head_label.setFont(head_font)
        self.files_label = _plain_label(wrap=False)
        self.save_label = _plain_label(wrap=True)
        small = self.save_label.font()
        if small.pointSizeF() > 0:
            small.setPointSizeF(small.pointSizeF() * 0.9)
        self.save_label.setFont(small)
        self.save_label.setForegroundRole(QPalette.ColorRole.PlaceholderText)  # chữ phụ, vẫn ≥ 4,5:1

        self.reject_button = QPushButton()
        self.accept_button = QPushButton()
        primary = self.accept_button.font()
        primary.setBold(True)  # nút chính; Enter vẫn là “Từ chối”
        self.accept_button.setFont(primary)
        self.accept_button.setProperty(theme.PRIMARY_PROPERTY, True)  # tô màu nhấn, không phải nút mặc định
        self.accept_button.setAutoDefault(False)
        self.reject_button.setAutoDefault(True)
        self.reject_button.setDefault(True)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.reject_button)
        buttons.addWidget(self.accept_button)
        layout = QVBoxLayout(box)
        layout.setSpacing(10)
        layout.addWidget(self.head_label)
        layout.addWidget(self.files_label)
        layout.addWidget(self.save_label)
        layout.addSpacing(6)
        layout.addLayout(buttons)
        self.retranslate()
        self.reject_button.setFocus()  # Tab tới “Nhận”; Enter / Space trên nút đang có focus

    def retranslate(self) -> None:
        t = QCoreApplication.translate
        head, lines, save = offer_parts(self.offer, self.receive_dir)
        title = t("Offer", "Có file gửi tới")
        self.box.setWindowTitle(title)
        self.box.setAccessibleName(title)
        self.box.setAccessibleDescription(head)  # trình đọc màn hình đọc câu đầu khi hộp mở
        self.head_label.setText(head)
        self.files_label.setText("\n".join(lines))
        self.save_label.setText(save or "")
        self.save_label.setVisible(save is not None)
        accept = t("Offer", "Nhận")
        reject = t("Offer", "Từ chối")
        self.accept_button.setText(accept)
        self.accept_button.setAccessibleName(accept)
        self.reject_button.setText(reject)
        self.reject_button.setAccessibleName(reject)


class OfferPrompts(QObject):
    """Quản lý các hộp hỏi nhận file đang mở, mỗi `TransferKey` một hộp."""

    decided = Signal(object, bool)  # TransferKey, accept
    opened = Signal(object)  # TransferKey: vừa mở một hộp mới

    def __init__(self, receive_dir: Path | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._receive_dir = receive_dir
        self._prompts: dict[TransferKey, _Prompt] = {}

    def set_receive_dir(self, receive_dir: Path | None) -> None:
        """Thư mục lưu hiện ở dòng “Lưu vào” của các hộp mở sau lần gọi này và của các hộp đang mở."""
        self._receive_dir = receive_dir
        for prompt in self._prompts.values():
            prompt.receive_dir = receive_dir
            prompt.retranslate()

    def handle(self, update: TransferUpdate) -> None:
        """Nhận mọi `TransferUpdate`: mở hộp cho `offer` cần hỏi, đóng hộp khi lượt kết thúc."""
        if isinstance(update, OfferReceived):
            if not update.auto_accepted:
                self._open(update)
        elif isinstance(update, TransferFinished):
            self._dismiss(update.info.key)

    def box(self, key: TransferKey) -> QDialog | None:
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
        prompt.accept_button.clicked.connect(lambda: self._clicked(prompt, True))
        prompt.reject_button.clicked.connect(lambda: self._clicked(prompt, False))
        prompt.box.finished.connect(lambda _result: self._finished(prompt))
        box: QWidget = prompt.box
        box.show()
        box.raise_()
        box.activateWindow()
        self.opened.emit(offer.key)

    def _clicked(self, prompt: _Prompt, accept: bool) -> None:
        if prompt.resolved:
            return
        prompt.resolved = True
        self.decided.emit(prompt.offer.key, accept)
        if accept:
            prompt.box.accept()
        else:
            prompt.box.reject()

    def _finished(self, prompt: _Prompt) -> None:
        if self._prompts.get(prompt.offer.key) is prompt:
            del self._prompts[prompt.offer.key]
        if not prompt.resolved:  # Esc hay nút X: từ chối
            prompt.resolved = True
            self.decided.emit(prompt.offer.key, False)

    def _dismiss(self, key: TransferKey) -> None:
        prompt = self._prompts.pop(key, None)
        if prompt is None:
            return
        prompt.resolved = True
        prompt.box.close()
