"""Ghép đôi và hủy ghép đôi trên giao diện (mvp F3, mục 7.2, mục 8; spec hackathon PR2).

Ba phần:

- **Hàm chữ** (`prompt_header`, `code_text`, `code_warning`, `peer_lines`, `outcome_text`, …):
  thuần, gọi lúc dùng, mọi chuỗi qua `QCoreApplication.translate("Pairing", …)` (chuỗi nguồn
  tiếng Việt), chèn giá trị một lượt bằng `texts.fill`. Chữ do máy kia đặt (tên) luôn qua
  `texts.peer_label` (`display_name`: ≤ 32 cột, ký tự trống thành `·`, ký tự điều khiển thành
  U+FFFD) và nằm trong “…”.
- **`PairingDialog`**: hộp mvp mục 7.2, theo đúng bố cục chống giả mã: tiêu đề cửa sổ chỉ
  “Ghép đôi”; dòng đầu cố định theo vai; một nhãn chữ lớn CHỈ có mã; cảnh báo nhiều mã ngay dưới
  mã; rồi mới tới tên máy kia. Không cha, `NonModal`, `WA_DeleteOnClose`, mở bằng `show()` +
  `raise_()` + `activateWindow()`, không bao giờ `exec()`. Nút mặc định (Enter) và Esc là
  “Không trùng”; đóng bằng nút X cũng là “Không trùng”. Mỗi hộp phát `decided` tối đa một lần;
  `dismiss()` đóng hộp mà không phát (hết hạn, đã có kết quả, thoát). Mã thật to, căn giữa;
  dòng hướng dẫn và tên máy kia màu dịu theo vai `PlaceholderText` của bảng màu (`ui.theme`, đủ
  tương phản mvp N12); cảnh báo nhiều mã có biểu tượng cảnh báo, chữ đậm và nền riêng (không chỉ
  dựa vào màu). Chữ chỉ cho trình đọc màn hình nằm ở accessibleName/Description.
- **`PairingProgressDialog`**: hộp “Đang ghép đôi với …” có thanh bận và nút “Hủy”.
- **`PairingController`**: điều phối trên thread Qt. Host là bất cứ gì có signal
  `pairing_prompt`, `pairing_finished` và `call` / `submit` như `bridge.NodeHost` (test dùng host
  giả). `Node` chỉ được chạm bên trong hàm đưa cho `call` / `submit`, chạy trên thread node.

Hành vi chính của `PairingController`:

- `pairing_finished` là nguồn kết quả duy nhất ở cả hai vai (spec D4). Kết quả `on_done` của
  `submit(pair_with)` chỉ dùng để xóa trạng thái việc đang chạy, bắt lỗi tham số / host dừng, và
  phát `job_done(device_id, paired)` đúng một lần khi việc chủ động kết thúc (trừ khi đổi host);
  `paired` chỉ đúng khi PAIRED với chính thiết bị được nhắm (`ui.app` dùng cho “ghép rồi gửi”).
- Hết `expires_at` chỉ đóng hộp, KHÔNG `decide(False)`: lõi tự kết thúc phiên (TIMEOUT) và báo
  qua `pairing_finished`. `expires_at` tính theo `loop.time()` của asyncio, tức
  `time.monotonic()`; đồng hồ tiêm được qua `clock`.
- Ghép bằng IP (`pair_address`) khớp kết quả theo `result.address == ip` hoặc mã máy, không theo
  `session_id`: khi hai bên cùng chủ động, `pair_with` trả kết quả của phiên thay thế ở vai trả
  lời (`port` None). PAIRED thì MỘT `call` vừa `connect_hint(device_id, [ip], port)` vừa đọc lại
  `devices()` (không có NodeEvent khi kho tin cậy đổi).
- Một hộp kết quả dùng lại: câu mới thay câu cũ (`setText`), không mở hộp thứ hai, nên máy lạ dội
  yêu cầu không làm dội hộp. Phiên ở vai trả lời dừng trước khi hiện mã thì không báo gì.
- `set_host` / `close_all`: đóng mọi hộp mà không quyết định; sự kiện và `on_done` của host cũ
  tới sau bị bỏ (mỗi lần đổi host tăng một số thế hệ, các callback mang theo số thế hệ cũ).

Bẫy:

- `QLabel` và `QMessageBox` mặc định `AutoText`: chữ có thẻ HTML sẽ bị hiểu là rich text. Mọi
  nhãn, hộp ở đây đặt `Qt.PlainText`. Tooltip và whatsThis luôn là rich text: không bao giờ đặt
  chữ do máy kia đặt vào đó.
- `QDialog.closeEvent` gọi `reject()`, nên đóng hộp bằng code cũng đi qua `done()`: `dismiss()`
  đánh dấu đã xử lý TRƯỚC khi `close()`, không thì đóng vì hết hạn thành `decide(False)`.
- Không `exec()` / `open()` hộp nào: `open()` đổi sang `WindowModal`; mọi hộp ở đây `NonModal`
  và mở bằng `show()`.
- Lời hỏi tới khi cửa sổ chính đang ẩn: Windows chặn cửa sổ giành focus, nên báo thêm bằng bóng
  thoại khay (`notify`).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from teleport.app.devices import DeviceView
from teleport.app.node import Node
from teleport.app.pairing import PairingFinished, PairingOutcome, PairingPrompt
from teleport.core import pairing as core_pairing  # WARN_RECENT_CODES đọc lúc dùng
from teleport.core.pairing import Role
from teleport.core.pairing_code import format_code
from teleport.ui import texts

log = logging.getLogger(__name__)

_CODE_FONT_SCALE = 2.6  # cỡ chữ của mã so với cỡ chữ thường
_MAX_TIMER_MS = 24 * 3600 * 1000  # QTimer nhận int32 mili giây; hạn thật chỉ vài chục giây

# Phiên chưa xong vì người dùng hay máy kia (mvp mục 8, hai dòng “chưa xong”).
_UNFINISHED = frozenset(
    {
        PairingOutcome.USER_REJECTED,
        PairingOutcome.TIMEOUT,
        PairingOutcome.PEER_REJECTED,
        PairingOutcome.PEER_TIMEOUT,
        PairingOutcome.PROTOCOL_ERROR,
        PairingOutcome.PEER_INVALID,
        PairingOutcome.PEER_ABORTED,
        PairingOutcome.CONNECTION_LOST,
    }
)


# =================================================================================================
# Hàm chữ
# =================================================================================================


def _quoted(name: str) -> str:
    """Tên đã qua `peer_label`, trong ngoặc “…” (một lượt `fill`, tên chứa `{x}` không bị thay)."""
    return texts.fill(QCoreApplication.translate("Pairing", "“{name}”"), name=name)


def title() -> str:
    """Tiêu đề hộp mã và hộp kết quả: chỉ “Ghép đôi”, không chứa tên máy kia (mvp mục 7.2)."""
    return QCoreApplication.translate("Pairing", "Ghép đôi")


def prompt_header(role: Role) -> str:
    """Dòng 1 của hộp mã, theo vai (bên trả lời là máy nhận yêu cầu)."""
    if role is Role.RESPONDER:
        return QCoreApplication.translate(
            "Pairing", "Có yêu cầu ghép đôi. Hãy so mã dưới đây với mã trên màn hình máy kia."
        )
    return QCoreApplication.translate("Pairing", "Hãy so mã dưới đây với mã trên màn hình máy kia.")


def code_text(prompt: PairingPrompt) -> str:
    """Chữ của nhãn mã: chỉ có mã, ví dụ `382 104`."""
    return format_code(prompt.code)


def code_label_name() -> str:
    """Tên cho trình đọc màn hình của nhãn mã (nhãn cố định của dòng mã ở mvp mục 7.2)."""
    return QCoreApplication.translate("Pairing", "Mã ghép đôi")


def code_warning(prompt: PairingPrompt) -> str | None:
    """Cảnh báo nhiều mã (mvp mục 8, bản giao diện); chỉ ở vai trả lời, từ `WARN_RECENT_CODES` mã."""
    if prompt.role is not Role.RESPONDER or prompt.recent_codes < core_pairing.WARN_RECENT_CODES:
        return None
    return QCoreApplication.translate(
        "Pairing",
        "Cảnh báo: máy này đã hiện %n mã ghép đôi trong 1 phút qua. Nếu bạn không tự ghép đôi nhiều "
        "lần như vậy, có thể có người đang thử giả mạo: hãy bấm “Không trùng”.",
        None,
        prompt.recent_codes,
    )


def peer_lines(prompt: PairingPrompt) -> list[str]:
    """Dòng 4–6 của bảng mvp mục 7.2: tên máy kia tự xưng, tên đã lưu, tên mới."""
    short_id = prompt.peer_device_id[:8]
    name = texts.peer_label(prompt.peer_name, prompt.peer_device_id)
    lines = [
        texts.fill(
            QCoreApplication.translate("Pairing", "Máy kia tự xưng là “{name}” (mã máy {id}, {address})."),
            name=name,
            id=short_id,
            address=prompt.address,
        )
    ]
    if prompt.known_name is not None:
        known = texts.peer_label(prompt.known_name, prompt.peer_device_id)
        lines.append(
            texts.fill(
                QCoreApplication.translate(
                    "Pairing", "Máy này đã ghép đôi với mã máy {id} trước đây, tên đã lưu: “{known}”."
                ),
                id=short_id,
                known=known,
            )
        )
        # Ghép đôi lại cập nhật tên theo hello ở cả hai vai (protocol mục 5.5).
        if prompt.known_name != prompt.peer_name:
            lines.append(
                texts.fill(
                    QCoreApplication.translate("Pairing", "Ghép đôi xong, tên sẽ đổi thành “{name}”."),
                    name=name,
                )
            )
    return lines


def _result_target(result: PairingFinished, target: str | None) -> str:
    """`ip:port` khi biết cổng (của kết quả, hay cái UI tự giữ), không thì địa chỉ."""
    if result.port is not None:
        return f"{result.address}:{result.port}"
    if target is not None:
        return target
    return result.address


def _result_name(result: PairingFinished, target: str | None) -> str:
    """X của mvp mục 8: tên máy kia tự xưng → tên đã lưu → `target` → địa chỉ (chưa có ngoặc)."""
    device_id = result.peer_device_id or ""
    if result.peer_name:
        return texts.peer_label(result.peer_name, device_id)
    if result.known_name:
        return texts.peer_label(result.known_name, device_id)
    if target is not None:
        return target
    return result.address


def outcome_text(result: PairingFinished, *, target: str | None = None) -> str | None:
    """Câu mvp mục 8 cho kết quả ghép đôi; None khi không cần báo.

    `target`: “ip:port” giao diện tự giữ khi ghép bằng IP (dùng khi `result.port` là None, ví dụ
    phiên thay thế ở vai trả lời). Không báo: CANCELLED (người dùng tự hủy hay app đang thoát), và
    phiên ở vai trả lời dừng trước khi hiện mã (mvp: máy nhận “không cần làm gì”; máy lạ không
    dội được hộp).
    """
    outcome = result.outcome
    where = _result_target(result, target)
    x = _quoted(_result_name(result, target))
    responder = result.role is Role.RESPONDER
    tr = QCoreApplication.translate
    if outcome is PairingOutcome.PAIRED:
        if result.device is not None:
            x = _quoted(texts.peer_label(result.device.name, result.device.device_id))
        return texts.fill(tr("Pairing", "Đã ghép đôi với {name}."), name=x)
    if outcome in _UNFINISHED:
        if result.code_shown:
            return texts.fill(
                tr(
                    "Pairing",
                    "Ghép đôi với {name} chưa xong. Nếu mã trên hai máy khác nhau, "
                    "có thể có người đang giả mạo.",
                ),
                name=x,
            )
        if responder:
            return None
        return texts.fill(
            tr("Pairing", "Ghép đôi với {name} chưa xong vì phiên dừng trước khi hiện mã. Hãy thử lại."),
            name=x,
        )
    if outcome is PairingOutcome.IDENTITY_CONFLICT:
        known = (
            _quoted(texts.peer_label(result.known_name, result.peer_device_id or ""))
            if result.known_name
            else x
        )
        if responder:
            return texts.fill(
                tr(
                    "Pairing",
                    "Một máy chưa xác minh tự xưng là {known} nhưng dùng danh tính khác với lúc ghép đôi. "
                    "Có thể có người đang giả mạo máy đó. Yêu cầu ghép đôi đã bị chặn.",
                ),
                known=known,
            )
        return "\n".join(
            [
                texts.fill(
                    tr(
                        "Pairing",
                        "{known} đang dùng một danh tính khác với lúc ghép đôi. "
                        "Có thể có người đang giả mạo máy này. Kết nối đã bị chặn.",
                    ),
                    known=known,
                ),
                tr(
                    "Pairing",
                    "Muốn ghép đôi lại thì hãy hủy ghép đôi mục cũ trước "
                    "(chuột phải vào thiết bị → Hủy ghép đôi…).",
                ),
            ]
        )
    if outcome in (PairingOutcome.PEER_BUSY, PairingOutcome.REJECTED):
        return texts.fill(
            tr(
                "Pairing",
                "{name} chưa sẵn sàng ghép đôi: máy đó đang ẩn, đang ghép đôi với máy khác, "
                "chưa mở chế độ chờ ghép đôi, hoặc cần chờ một lát sau lần thử trước. Hãy thử lại sau.",
            ),
            name=x,
        )
    if outcome is PairingOutcome.UNREACHABLE:
        return _unreachable_text(where)
    if outcome is PairingOutcome.IDENTITY_MISMATCH:
        return texts.fill(
            tr(
                "Pairing",
                "Thiết bị ở {target} gửi danh tính không hợp lệ nên kết nối đã bị chặn. "
                "Có thể có người đang giả mạo.",
            ),
            target=where,
        )
    if outcome is PairingOutcome.VERSION_MISMATCH:
        return texts.fill(
            tr(
                "Pairing",
                "{name} đang dùng phiên bản Teleport khác. Hãy cập nhật cả hai máy lên cùng phiên bản.",
            ),
            name=x,
        )
    if outcome is PairingOutcome.SELF:
        return tr("Pairing", "Đó là chính máy này. Hãy nhập địa chỉ của máy kia.")
    if outcome is PairingOutcome.BUSY:
        return busy_text()
    if outcome is PairingOutcome.SAVE_FAILED:
        return texts.fill(
            tr(
                "Pairing",
                "Đã xác nhận ghép đôi với {name} nhưng không lưu được danh sách thiết bị đã ghép đôi. "
                "Hãy kiểm tra quyền ghi thư mục cấu hình rồi ghép đôi lại.",
            ),
            name=x,
        )
    if outcome is PairingOutcome.PROMPT_FAILED:
        return tr("Pairing", "Không hiện được mã ghép đôi trên máy này nên đã hủy ghép đôi. Hãy thử lại.")
    return None  # CANCELLED


def _unreachable_text(target: str) -> str:
    return texts.fill(
        QCoreApplication.translate(
            "Pairing",
            "Không kết nối được tới {target}. Hãy kiểm tra Teleport đang chạy trên máy đó, "
            "hai máy dùng chung một mạng, và tường lửa cho phép Teleport.",
        ),
        target=target,
    )


def busy_text() -> str:
    """Câu BUSY (mvp mục 8): máy này đang có một phiên ghép đôi khác."""
    return QCoreApplication.translate("Pairing", "Máy này đang có một phiên ghép đôi khác. Hãy thử lại sau.")


def pairing_progress_text(name: str) -> str:
    """Hộp tiến độ khi máy này chủ động ghép đôi; `name` đã qua `peer_label` hoặc là “ip:port”."""
    return texts.fill(QCoreApplication.translate("Pairing", "Đang ghép đôi với {name}…"), name=_quoted(name))


def cancel_button_text() -> str:
    """Nút “Hủy” của hộp tiến độ."""
    return QCoreApplication.translate("Pairing", "Hủy")


def device_gone_text(name: str) -> str:
    """Thiết bị không còn online lúc bắt đầu ghép đôi (radar có thể giữ IP cũ)."""
    return texts.fill(
        QCoreApplication.translate("Pairing", "Không thấy {name} trong mạng nữa. Hãy thử lại."),
        name=_quoted(name),
    )


def repair_offline_text(name: str) -> str:
    """“Ghép đôi lại” khi radar không thấy thiết bị (mDNS bị chặn): mời nhập IP; `name` qua `peer_label`."""
    return texts.fill(
        QCoreApplication.translate(
            "Pairing",
            "Không thấy {name} trong mạng. Nếu mạng chặn việc tìm thiết bị, "
            "hãy nhập địa chỉ IP của máy đó để ghép đôi lại.",
        ),
        name=_quoted(name),
    )


def reject_button_text() -> str:
    return QCoreApplication.translate("Pairing", "Không trùng")


def accept_button_text() -> str:
    return QCoreApplication.translate("Pairing", "Trùng khớp")


def unpair_title() -> str:
    """Tiêu đề hộp xác nhận hủy ghép đôi."""
    return QCoreApplication.translate("Pairing", "Hủy ghép đôi")


def unpair_confirm_text(name: str) -> str:
    """Câu hỏi xác nhận hủy ghép đôi; `name` đã qua `peer_label`."""
    return texts.fill(
        QCoreApplication.translate(
            "Pairing",
            "Hủy ghép đôi với {name}? Hai máy sẽ không gửi file cho nhau được nữa cho tới khi ghép đôi lại.",
        ),
        name=_quoted(name),
    )


def unpair_accept_text() -> str:
    """Nút đồng ý hủy ghép đôi."""
    return QCoreApplication.translate("Pairing", "Hủy ghép đôi")


def unpair_decline_text() -> str:
    """Nút không hủy (mặc định)."""
    return QCoreApplication.translate("Pairing", "Không")


def unpaired_text(name: str) -> str:
    return texts.fill(
        QCoreApplication.translate("Pairing", "Đã hủy ghép đôi với {name}."), name=_quoted(name)
    )


def unpair_failed_text(name: str) -> str:
    return texts.fill(
        QCoreApplication.translate("Pairing", "Không hủy ghép đôi được với {name}. Hãy thử lại."),
        name=_quoted(name),
    )


def prompt_notification() -> tuple[str, str]:
    """(tiêu đề, chữ) bóng thoại khay khi có lời hỏi ghép đôi mà cửa sổ chính đang ẩn."""
    return (
        QCoreApplication.translate("Pairing", "Teleport"),
        QCoreApplication.translate("Pairing", "Có yêu cầu ghép đôi"),
    )


# =================================================================================================
# Hộp mã (mvp mục 7.2)
# =================================================================================================


def _plain_label(object_name: str) -> QLabel:
    label = QLabel()
    label.setObjectName(object_name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class PairingDialog(QDialog):
    """Hộp so mã của một phiên ghép đôi; `decided(session_id, accepted)` phát tối đa một lần."""

    decided = Signal(int, bool)

    def __init__(self, prompt: PairingPrompt) -> None:
        super().__init__(None)
        self._prompt = prompt
        self._resolved = False  # đã quyết định hoặc đã đóng không quyết định
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        # Dòng hướng dẫn và dòng tên máy kia: màu dịu (vai PlaceholderText của theme, vẫn đủ tương
        # phản mvp N12), căn giữa như mã.
        self._header = _plain_label("pairingHeader")
        self._header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._header.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self._code = _plain_label("pairingCode")
        self._code.setWordWrap(False)
        self._code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self._code.font()
        base = font.pointSizeF() if font.pointSizeF() > 0 else 10.0
        font.setPointSizeF(base * _CODE_FONT_SCALE)
        font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, base * 0.3)
        self._code.setFont(font)
        # Cảnh báo nhiều mã: biểu tượng cảnh báo + chữ đậm trên nền khác màu (vai của theme),
        # không chỉ dựa vào màu; luôn ngay dưới mã.
        self._warning_box = QFrame()
        self._warning_box.setObjectName("pairingWarningBox")
        self._warning_box.setFrameShape(QFrame.Shape.StyledPanel)
        self._warning_box.setBackgroundRole(QPalette.ColorRole.AlternateBase)
        self._warning_box.setAutoFillBackground(True)
        icon = _plain_label("pairingWarningIcon")
        icon.setPixmap(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning).pixmap(QSize(24, 24))
        )
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._warning = _plain_label("pairingWarning")
        warning_font = self._warning.font()
        warning_font.setBold(True)
        self._warning.setFont(warning_font)
        warning_row = QHBoxLayout(self._warning_box)
        warning_row.addWidget(icon)
        warning_row.addWidget(self._warning, 1)
        self._peer = _plain_label("pairingPeer")
        self._peer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._peer.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.reject_button = QPushButton()
        self.reject_button.setObjectName("pairingReject")
        self.accept_button = QPushButton()
        self.accept_button.setObjectName("pairingAccept")
        # Enter là “Không trùng”: đồng ý phải là một lựa chọn có chủ ý.
        self.reject_button.setAutoDefault(True)
        self.reject_button.setDefault(True)
        self.accept_button.setAutoDefault(False)
        self.accept_button.setDefault(False)
        self.reject_button.clicked.connect(self.reject)
        self.accept_button.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addWidget(self.reject_button)
        buttons.addStretch(1)
        buttons.addWidget(self.accept_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self._header)
        layout.addSpacing(8)
        layout.addWidget(self._code)
        layout.addWidget(self._warning_box)
        layout.addSpacing(8)
        layout.addWidget(self._peer)
        layout.addSpacing(8)
        layout.addLayout(buttons)
        self.setTabOrder(self.reject_button, self.accept_button)
        self.reject_button.setFocus()
        self.retranslate()

    @property
    def session_id(self) -> int:
        return self._prompt.session_id

    @property
    def prompt(self) -> PairingPrompt:
        return self._prompt

    @property
    def resolved(self) -> bool:
        return self._resolved

    def retranslate(self) -> None:
        """Đặt lại mọi chữ (lúc dựng và khi đổi ngôn ngữ)."""
        prompt = self._prompt
        self.setWindowTitle(title())
        self.setAccessibleName(title())
        self._header.setText(prompt_header(prompt.role))
        self._code.setText(code_text(prompt))
        self._code.setAccessibleName(code_label_name())
        self._code.setAccessibleDescription(code_text(prompt))
        warning = code_warning(prompt)
        self._warning.setText(warning or "")
        self._warning.setVisible(warning is not None)
        self._warning_box.setVisible(warning is not None)
        self._peer.setText("\n".join(peer_lines(prompt)))
        self.reject_button.setText(reject_button_text())
        self.accept_button.setText(accept_button_text())

    def dismiss(self) -> None:
        """Đóng hộp mà không quyết định (hết hạn, đã có kết quả, thoát)."""
        self._resolved = True
        self.close()

    def done(self, result: int) -> None:
        if not self._resolved:
            self._resolved = True
            accepted = result in (QDialog.DialogCode.Accepted, QDialog.DialogCode.Accepted.value)
            self.decided.emit(self.session_id, accepted)
        super().done(result)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()


class PairingProgressDialog(QDialog):
    """Hộp “Đang ghép đôi với …”: chữ thuần, thanh bận (chỉ báo sống) và nút “Hủy”.

    Không cha, `NonModal`, tự xóa khi đóng. Esc, nút X và “Hủy” đều là `reject()`.
    """

    def __init__(self, name: str) -> None:
        super().__init__(None)
        self._name = name
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.label = _plain_label("pairingProgressText")
        self.bar = QProgressBar()
        self.bar.setObjectName("pairingProgressBar")
        self.bar.setRange(0, 0)  # dạng bận: chưa biết còn bao lâu
        self.bar.setTextVisible(False)
        self.cancel_button = QPushButton()
        self.cancel_button.setObjectName("pairingCancel")
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.label)
        layout.addWidget(self.bar)
        layout.addLayout(row)
        self.setMinimumWidth(320)
        self.retranslate()

    def text(self) -> str:
        """Câu đang hiện (giống `QMessageBox.text()`, cho test)."""
        return self.label.text()

    def retranslate(self) -> None:
        self.setWindowTitle(title())
        self.setAccessibleName(title())
        text = pairing_progress_text(self._name)
        self.label.setText(text)
        self.bar.setAccessibleName(text)
        self.cancel_button.setText(cancel_button_text())

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()


# =================================================================================================
# Điều phối
# =================================================================================================


class _Job(Protocol):
    """Phần `bridge.SubmitJob` mà controller dùng."""

    @property
    def done(self) -> bool: ...

    def cancel(self) -> None: ...


@dataclass
class _Pairing:
    """Một lần “ghép đôi chủ động” đang chạy (máy này khởi xướng)."""

    generation: int
    label: str  # tên đã qua peer_label, hoặc “ip:port”
    device_id: str | None  # ghép từ radar
    addresses: tuple[str, ...]  # địa chỉ radar đang giữ (để khớp kết quả)
    ip: str | None = None  # ghép bằng IP
    port: int | None = None
    job: _Job | None = None
    cancelled: bool = False
    hinted: bool = False  # đã gọi `connect_hint` tới `ip:port` sau PAIRED

    @property
    def target(self) -> str | None:
        return None if self.ip is None else f"{self.ip}:{self.port}"

    def matches(self, result: PairingFinished) -> bool:
        if self.ip is not None and result.address == self.ip:
            return True
        if self.device_id is not None and result.peer_device_id == self.device_id:
            return True
        return result.address in self.addresses


def _new_box(parent: QWidget | None, on_finished: Callable[[QMessageBox], None]) -> QMessageBox:
    """`QMessageBox` không chặn, chữ thuần, tự xóa khi đóng."""
    box = QMessageBox(parent)
    box.setWindowModality(Qt.WindowModality.NonModal)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.finished.connect(lambda _result: on_finished(box))
    return box


def _show(widget: QWidget) -> None:
    widget.show()
    widget.raise_()
    widget.activateWindow()


class PairingController(QObject):
    """Ghép đôi / hủy ghép đôi trên thread Qt: hộp mã, hộp tiến độ, hộp kết quả, hộp xác nhận."""

    devices_changed = Signal(object)  # tuple[DeviceView, ...] đọc lại sau PAIRED / unpair
    message = Signal(str)  # mọi câu kết quả vừa hiện
    paired = Signal(str)  # device_id, sau PAIRED (cả hai vai)
    finished = Signal(object)  # PairingFinished, mọi kết quả (cả hai vai)
    prompt_opened = Signal(int)  # session_id vừa mở hộp
    # Việc ghép chủ động (`pair_device` / `pair_address`) kết thúc, mọi đường (kể cả lỗi, hủy):
    # device_id của thiết bị trên radar (None nếu ghép bằng IP), PAIRED hay không.
    job_done = Signal(object, bool)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        notify: Callable[[str, str], None] | None = None,
        window_visible: Callable[[], bool] = lambda: True,
        message_parent: Callable[[], QWidget | None] = lambda: None,
    ) -> None:
        super().__init__(parent)
        self._clock = clock
        self._notify = notify
        self._window_visible = window_visible
        self._message_parent = message_parent
        self._host: Any = None
        self._generation = 0
        self._connections: list[tuple[Any, Callable[..., None]]] = []
        self._dialogs: dict[int, PairingDialog] = {}
        self._pairing: _Pairing | None = None
        self._progress_box: PairingProgressDialog | None = None
        self._progress_closing = False  # đóng hộp tiến độ bằng code, không phải người dùng hủy
        self._result_box: QMessageBox | None = None
        self._unpair_box: QMessageBox | None = None
        self._unpair_names: dict[str, str] = {}  # device_id → tên đã qua peer_label

    # ---- Thuộc tính --------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        """Có một lần ghép đôi chủ động đang chạy."""
        return self._pairing is not None

    @property
    def progress_box(self) -> PairingProgressDialog | None:
        return self._progress_box

    @property
    def result_box(self) -> QMessageBox | None:
        return self._result_box

    @property
    def unpair_box(self) -> QMessageBox | None:
        return self._unpair_box

    def dialog(self, session_id: int) -> PairingDialog | None:
        return self._dialogs.get(session_id)

    def dialogs(self) -> list[PairingDialog]:
        return list(self._dialogs.values())

    # ---- Host --------------------------------------------------------------------------------

    def set_host(self, host: Any) -> None:
        """Đổi host: ngắt host cũ, đóng mọi hộp, quên việc đang chạy; sự kiện host cũ bị bỏ."""
        for signal, slot in self._connections:
            with contextlib.suppress(RuntimeError, TypeError):  # host cũ đã bị xóa hay đã ngắt
                signal.disconnect(slot)
        self._connections = []
        self._generation += 1
        self._pairing = None
        self._close_boxes(include_result=False)
        self._host = host
        if host is None:
            return
        generation = self._generation
        on_prompt = functools.partial(self._on_prompt, generation)
        on_finished = functools.partial(self._on_finished, generation)
        host.pairing_prompt.connect(on_prompt)
        host.pairing_finished.connect(on_finished)
        self._connections = [(host.pairing_prompt, on_prompt), (host.pairing_finished, on_finished)]

    def close_all(self) -> None:
        """Thoát: đóng mọi hộp, không quyết định; bỏ host, nên sự kiện và `on_done` tới sau bị bỏ."""
        self.set_host(None)
        if self._result_box is not None:
            box, self._result_box = self._result_box, None
            box.close()

    def _current(self, generation: int) -> bool:
        return generation == self._generation and self._host is not None

    # ---- Ghép đôi chủ động -------------------------------------------------------------------

    def pair_device(self, view: DeviceView) -> bool:
        """Ghép đôi với thiết bị trên radar; False nếu không ghép (offline, đang bận, không có host).

        Đang bận thì luôn báo câu “đang bận”, kể cả khi thiết bị offline: `ui.app` dựa vào điều đó.
        """
        if self._host is None:
            return False
        if self._pairing is not None:
            self._show_result(busy_text())
            return False
        if not view.online or not view.addresses or view.port is None:
            return False
        label = texts.peer_label(view.name, view.device_id)
        pairing = _Pairing(self._generation, label, view.device_id, tuple(view.addresses))
        device_id = view.device_id

        async def run(node: Node) -> PairingFinished | None:
            # Radar có thể giữ địa chỉ cũ: đọc lại bản mới nhất trên thread node.
            fresh = next((v for v in node.devices() if v.device_id == device_id), None)
            if fresh is None or not fresh.online or not fresh.addresses or fresh.port is None:
                return None
            return await node.pairing.pair_with(list(fresh.addresses), fresh.port)

        self._start(pairing, run)
        return True

    def pair_address(self, ip: str, port: int) -> bool:
        """Ghép đôi với `ip:port` người dùng nhập; False nếu đang bận hay không có host."""
        if self._host is None:
            return False
        if self._pairing is not None:
            self._show_result(busy_text())
            return False
        pairing = _Pairing(self._generation, f"{ip}:{port}", None, (), ip=ip, port=port)
        self._start(pairing, lambda node: node.pairing.pair_with([ip], port))
        return True

    def cancel_pairing(self) -> None:
        """Nút “Hủy” của hộp tiến độ: hủy việc đang chạy (kết quả CANCELLED không báo)."""
        pairing = self._pairing
        if pairing is not None and not pairing.cancelled:
            pairing.cancelled = True
            if pairing.job is not None:
                pairing.job.cancel()
        self._close_progress()

    def _start(self, pairing: _Pairing, fn: Callable[[Node], Any]) -> None:
        self._pairing = pairing
        self._open_progress(pairing.label)
        pairing.job = self._host.submit(fn, functools.partial(self._on_pair_done, pairing))

    def _on_pair_done(self, pairing: _Pairing, result: object) -> None:
        if pairing is not self._pairing or not self._current(pairing.generation):
            return
        self._pairing = None
        self._close_progress()
        self.job_done.emit(
            pairing.device_id,
            isinstance(result, PairingFinished)
            and result.outcome is PairingOutcome.PAIRED
            # Địa chỉ cũ của radar có thể là của máy khác: chỉ tính khi ghép đúng thiết bị được nhắm.
            and (pairing.device_id is None or result.peer_device_id == pairing.device_id),
        )
        if result is None:
            self._show_result(device_gone_text(pairing.label))
        elif isinstance(result, ValueError):
            log.warning("ghép đôi: tham số không hợp lệ (%s)", type(result).__name__)
            self._show_result(_unreachable_text(pairing.target or pairing.label))
        elif isinstance(result, RuntimeError):
            log.info("ghép đôi: node chưa chạy hoặc đang đóng")
        elif isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            log.error("ghép đôi lỗi: %s", type(result).__name__)
        elif (
            isinstance(result, PairingFinished)
            and result.outcome is PairingOutcome.PAIRED
            and pairing.ip is not None
            and pairing.port is not None
            and not pairing.hinted
            and result.peer_device_id is not None
        ):
            # Phiên thay thế từ một địa chỉ khác `ip` (máy kia nhiều card mạng): `pairing_finished`
            # không khớp được theo địa chỉ, nhưng kết quả của `pair_with` là của job này (9.2).
            self._connect_hint(pairing.generation, result.peer_device_id, (pairing.ip, pairing.port))
        elif (
            isinstance(result, PairingFinished)
            and result.role is Role.RESPONDER
            and outcome_text(result) is None
            and result.outcome is not PairingOutcome.CANCELLED
        ):
            # `pair_with` trả kết quả của phiên thay thế (hai bên cùng chủ động) mà phiên đó dừng
            # trước khi hiện mã: người dùng đã bấm ghép đôi nên vẫn báo, bằng câu của vai khởi xướng.
            replaced = dataclasses.replace(result, role=Role.INITIATOR)
            self._show_result(outcome_text(replaced, target=pairing.target) or "")
        # Còn lại (PairingFinished): đã báo qua `pairing_finished` (tới trước on_done).

    def _connect_hint(self, generation: int, device_id: str, hint: tuple[str, int]) -> None:
        def run(node: Node) -> None:
            node.connect_hint(device_id, [hint[0]], hint[1])

        if self._current(generation):
            self._host.call(run, self._log_error("connect_hint"))

    def _open_progress(self, label: str) -> None:
        self._close_progress()
        box = PairingProgressDialog(label)
        box.finished.connect(lambda _result: self._progress_finished(box))
        self._progress_box = box
        _show(box)

    def _close_progress(self) -> None:
        box = self._progress_box
        if box is None:
            return
        self._progress_box = None
        self._progress_closing = True
        try:
            box.close()
        finally:
            self._progress_closing = False

    def _progress_finished(self, box: PairingProgressDialog) -> None:
        if box is not self._progress_box:
            return  # đóng bằng code (đã gỡ trước khi close)
        self._progress_box = None
        if not self._progress_closing:
            self.cancel_pairing()  # người dùng bấm “Hủy”, Esc hay nút X

    # ---- Sự kiện từ node ---------------------------------------------------------------------

    def _on_prompt(self, generation: int, prompt: PairingPrompt) -> None:
        if not self._current(generation) or prompt.session_id in self._dialogs:
            return
        try:
            dialog = PairingDialog(prompt)
        except Exception:
            # Lõi tự kết thúc phiên khi hết hạn; không có gì để quyết định ở đây.
            log.exception("không mở được hộp ghép đôi của phiên %d", prompt.session_id)
            return
        host = self._host
        sid = prompt.session_id
        self._dialogs[sid] = dialog
        dialog.decided.connect(lambda session_id, ok: self._decide(generation, host, session_id, ok))
        dialog.finished.connect(lambda _result: self._dialog_closed(sid, dialog))
        timer = QTimer(dialog)
        timer.setSingleShot(True)
        timer.timeout.connect(dialog.dismiss)  # hết hạn: chỉ đóng, lõi tự báo TIMEOUT
        remaining = prompt.expires_at - self._clock()
        timer.start(int(min(max(remaining, 0.0) * 1000, _MAX_TIMER_MS)))
        self._close_progress()
        if not self._window_visible() and self._notify is not None:
            try:
                self._notify(*prompt_notification())
            except Exception:
                log.exception("không hiện được bóng thoại ghép đôi")
        _show(dialog)
        self.prompt_opened.emit(sid)

    def _decide(self, generation: int, host: Any, session_id: int, accepted: bool) -> None:
        if not self._current(generation) or host is not self._host:
            return
        host.call(lambda node: node.pairing.decide(session_id, accepted), self._log_error("decide"))

    def _dialog_closed(self, session_id: int, dialog: PairingDialog) -> None:
        if self._dialogs.get(session_id) is dialog:
            del self._dialogs[session_id]

    def _on_finished(self, generation: int, result: PairingFinished) -> None:
        if not self._current(generation):
            return
        if result.session_id is not None:
            dialog = self._dialogs.pop(result.session_id, None)
            if dialog is not None:
                dialog.dismiss()
        pairing = self._pairing
        # Phiên trả lời dừng trước khi hiện mã: `peer_device_id` do máy kia tự khai, chưa kiểm, nên
        # không coi là của job; nếu đúng là job bị nhường thì `_on_pair_done` báo thay.
        early_responder = (
            result.role is Role.RESPONDER
            and not result.code_shown
            and result.outcome is not PairingOutcome.PAIRED  # PAIRED: máy kia đã chứng minh giữ khóa
        )
        matched = pairing is not None and not early_responder and pairing.matches(result)
        if matched:
            self._close_progress()
        self.finished.emit(result)
        target = pairing.target if (pairing is not None and matched) else None
        text = outcome_text(result, target=target)
        if text is not None:
            self._show_result(text, ok=result.outcome is PairingOutcome.PAIRED)
        if result.outcome is PairingOutcome.PAIRED:
            device_id = result.device.device_id if result.device is not None else result.peer_device_id
            if device_id is not None:
                hint: tuple[str, int] | None = None
                if pairing is not None and matched and pairing.ip is not None and pairing.port is not None:
                    hint = (pairing.ip, pairing.port)
                    pairing.hinted = True
                self._after_paired(generation, device_id, hint)

    def _after_paired(self, generation: int, device_id: str, hint: tuple[str, int] | None) -> None:
        """MỘT `call`: `connect_hint` (ghép bằng IP) rồi đọc lại `devices()`; xong phát `paired`."""

        def refresh(node: Node) -> object:
            if hint is not None:
                try:
                    node.connect_hint(device_id, [hint[0]], hint[1])
                except (KeyError, ValueError) as e:
                    log.warning("ghép đôi: không gợi ý được địa chỉ (%s)", type(e).__name__)
            return node.devices()

        def done(result: object) -> None:
            if not self._current(generation):
                return
            if isinstance(result, tuple):
                self.devices_changed.emit(result)
            elif isinstance(result, BaseException):
                log.warning("ghép đôi: không đọc lại được danh sách thiết bị (%s)", type(result).__name__)
            self.paired.emit(device_id)

        self._host.call(refresh, done)

    # ---- Hủy ghép đôi ------------------------------------------------------------------------

    def request_unpair(self, device_id: str, name: str | None) -> None:
        """Mở hộp xác nhận hủy ghép đôi (nút mặc định là “Không”)."""
        label = texts.peer_label(name, device_id)
        self._unpair_names[device_id] = label
        self._close_unpair_box()
        box = _new_box(self._message_parent(), self._unpair_finished)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(unpair_title())
        box.setAccessibleName(unpair_title())
        box.setText(unpair_confirm_text(label))
        accept = box.addButton(unpair_accept_text(), QMessageBox.ButtonRole.DestructiveRole)
        decline = box.addButton(unpair_decline_text(), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(decline)
        box.setEscapeButton(decline)
        generation = self._generation

        def clicked(button: object) -> None:
            if button is accept and self._current(generation):
                self.unpair(device_id)

        box.buttonClicked.connect(clicked)
        self._unpair_box = box
        _show(box)

    def unpair(self, device_id: str) -> None:
        """Hủy ghép đôi ngay, không hỏi; xong thì đọc lại danh sách thiết bị."""
        host = self._host
        if host is None:
            return
        generation = self._generation
        label = self._unpair_names.get(device_id) or texts.peer_label(None, device_id)

        async def run(node: Node) -> object:
            await node.unpair(device_id)
            return node.devices()

        def done(result: object) -> None:
            if not self._current(generation):
                return
            if isinstance(result, tuple):
                self.devices_changed.emit(result)
                self._show_result(unpaired_text(label), ok=True)
            elif isinstance(result, KeyError):
                # Đã bị hủy trước đó: vẫn đọc lại danh sách để radar đúng.
                self._host.call(lambda node: node.devices(), self._devices_done(generation))
                self._show_result(unpaired_text(label), ok=True)
            elif isinstance(result, Exception):
                log.error("hủy ghép đôi lỗi: %s", type(result).__name__)
                self._show_result(unpair_failed_text(label))
            # CancelledError: host đang dừng, không báo.

        host.submit(run, done)

    def _devices_done(self, generation: int) -> Callable[[object], None]:
        def done(result: object) -> None:
            if self._current(generation) and isinstance(result, tuple):
                self.devices_changed.emit(result)

        return done

    def _unpair_finished(self, box: QMessageBox) -> None:
        if box is self._unpair_box:
            self._unpair_box = None

    def _close_unpair_box(self) -> None:
        box = self._unpair_box
        if box is not None:
            self._unpair_box = None
            box.close()

    # ---- Hộp kết quả và dọn dẹp --------------------------------------------------------------

    def _show_result(self, text: str, *, ok: bool = False) -> None:
        """Hiện câu trong hộp kết quả dùng lại (câu mới thay câu cũ) và phát `message`."""
        box = self._result_box
        fresh = box is None or not box.isVisible()
        if box is None:
            box = _new_box(self._message_parent(), self._result_finished)
            box.setWindowTitle(title())
            box.setAccessibleName(title())
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            self._result_box = box
        box.setIcon(QMessageBox.Icon.Information if ok else QMessageBox.Icon.Warning)
        box.setText(text)
        if fresh:
            _show(box)  # hộp đang mở thì chỉ thay chữ, không giành focus lại (máy lạ dội kết quả)
        self.message.emit(text)

    def _result_finished(self, box: QMessageBox) -> None:
        if box is self._result_box:
            self._result_box = None

    def _close_boxes(self, *, include_result: bool) -> None:
        for dialog in list(self._dialogs.values()):
            dialog.dismiss()
        self._dialogs.clear()
        self._close_progress()
        self._close_unpair_box()
        if include_result and self._result_box is not None:
            box, self._result_box = self._result_box, None
            box.close()

    @staticmethod
    def _log_error(what: str) -> Callable[[object], None]:
        def done(result: object) -> None:
            if isinstance(result, BaseException):
                log.info("ghép đôi: %s không chạy được (%s)", what, type(result).__name__)

        return done
