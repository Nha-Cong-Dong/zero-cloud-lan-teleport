"""Câu chữ dùng chung của giao diện: lỗi khởi động (E1–E6, M2), M4, M5 trong spec P4-3.

M3 (máy chưa có danh tính) đã bỏ: thay bằng màn “Bắt đầu” của `ui.main_window` (spec hackathon
mục 10 A1). M6 (hộp hỏi nhận file) nằm ở `ui.offer`. Câu chữ riêng của một màn hình nằm ở module
của màn hình đó (ví dụ `ui.radar`, `ui.transfers_view`).

Chuỗi nguồn tiếng Việt, mọi chuỗi qua `QCoreApplication.translate` với ngữ cảnh `Texts`, gọi lúc
dùng (không lúc import) để đổi ngôn ngữ có hiệu lực ngay. Viết nguyên lời gọi `translate(...)` ở
từng chỗ để `pyside6-lupdate` trích được chuỗi.

Chèn giá trị bằng `fill` (một lượt, chỉ thay `{tên}` có trong bản dịch): bản dịch có ngoặc nhọn
lạ không làm hỏng câu, và chữ do máy kia đặt chứa `{size}` không bị thay tiếp. Chữ từ máy khác
đi qua `peer_label` / `file_label` (`core.display`) trước khi chèn.

Tên lệnh CLI trong câu của GUI nằm trong ngoặc kép “…” (không dùng backtick như ở CLI).

`APP_NAME` là tên hiển thị của sản phẩm (tiêu đề cửa sổ, chữ cạnh logo, khay, tiêu đề bóng thoại, hộp
lỗi khởi động, tên đăng ký cho AppUserModelID ở `ui.app`): tên riêng nên không dịch. Trong câu chữ,
“Teleport” vẫn là cách gọi tắt.
"""

from __future__ import annotations

import re
import unicodedata

from PySide6.QtCore import QCoreApplication

from teleport.core.display import display_filename, display_name

APP_NAME = "ZCL Teleport"  # tên ngắn của “Zero-Cloud LAN Teleport”

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_REPLACEMENT = "�"  # U+FFFD thay ký tự điều khiển, như `cli._printable`


def fill(template: str, **values: str) -> str:
    """Thay `{tên}` trong `template` bằng `values[tên]` trong một lượt; tên lạ giữ nguyên."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def _printable(text: str) -> str:
    return "".join(_REPLACEMENT if unicodedata.category(ch) == "Cc" else ch for ch in text)


def peer_label(name: str | None, device_id: str) -> str:
    """Tên máy kia để hiện (không ngoặc): `display_name`; không có tên thì 8 ký tự đầu của mã."""
    return display_name(_printable(name)) if name else _printable(device_id[:8])


def file_label(name: str) -> str:
    """Tên file để hiện: ký tự điều khiển thành U+FFFD, rồi `display_filename`."""
    return display_filename(_printable(name))


# ---- Lỗi khởi động node --------------------------------------------------------------------


def start_error(code: str, detail: str = "") -> tuple[str, list[str]]:
    """(câu chính, các câu gợi ý) cho mã `bridge.StartError`; mã lạ dùng câu của OTHER.

    `detail` theo `bridge.classify_start_error`: CONFIG_DIR là `<đường dẫn>\\n<lỗi>`, RECEIVE_DIR
    là đường dẫn, PORT là lỗi của hệ điều hành.

    E4 (RECEIVE_DIR) chỉ còn cho `NodeOptions.receive_dir` là đường dẫn tường minh (test): giao diện
    chạy node với thư mục đã lưu, hỏng thì node vẫn chạy mà không nhận file (`ui.receive_dir`).
    """
    t = QCoreApplication.translate
    if code == "locked":  # M2
        return (
            t(
                "Texts",
                "Teleport đang chạy ở nơi khác với thư mục cấu hình này "
                "(ví dụ lệnh “teleport receive” trong cửa sổ dòng lệnh).",
            ),
            [t("Texts", "Hãy dừng nó rồi bấm Thử lại.")],
        )
    if code == "config_dir":  # E1
        path, _, error = detail.partition("\n")
        if error:
            head = fill(
                t("Texts", "Không dùng được thư mục cấu hình {path} ({error})."), path=path, error=error
            )
        else:
            head = fill(t("Texts", "Không dùng được thư mục cấu hình {path}."), path=path)
        return head, [t("Texts", "Hãy kiểm tra quyền ghi vào thư mục đó rồi bấm Thử lại.")]
    if code == "identity":  # E2: câu mvp mục 8 cho danh tính hỏng
        return (
            t(
                "Texts",
                "Không đọc được danh tính của máy này. "
                "Teleport sẽ không tự tạo danh tính mới để khỏi mất các ghép đôi.",
            ),
            [t("Texts", "Hãy chạy “teleport identity” trong cửa sổ dòng lệnh để xem chi tiết.")],
        )
    if code == "trust_store":  # E3
        return (
            t(
                "Texts",
                "Không đọc được danh sách thiết bị đã ghép đôi. "
                "Teleport sẽ không tự xóa nó để khỏi mất các ghép đôi.",
            ),
            [t("Texts", "Hãy chạy “teleport receive” trong cửa sổ dòng lệnh để xem chi tiết.")],
        )
    if code == "receive_dir":  # E4
        return (
            fill(t("Texts", "Không dùng được thư mục nhận {path}."), path=detail),
            [t("Texts", "Hãy kiểm tra ổ đĩa và quyền ghi rồi bấm Thử lại.")],
        )
    if code == "port":  # E5
        return (
            fill(t("Texts", "Không mở được cổng để nhận kết nối ({error})."), error=detail),
            [t("Texts", "Hãy đóng ứng dụng đang dùng cổng đó rồi bấm Thử lại.")],
        )
    return (  # E6
        t("Texts", "Teleport không khởi động được vì một lỗi không mong đợi."),
        [
            t(
                "Texts",
                "Hãy bấm Thử lại; nếu vẫn lỗi, hãy chạy “teleport receive” "
                "trong cửa sổ dòng lệnh để xem chi tiết.",
            )
        ],
    )


def config_dir_unusable_at_launch(detail: str) -> tuple[str, list[str]]:
    """E1 khi chưa có cửa sổ (không lấy được `gui.lock`): hộp chỉ có nút OK nên không nói "Thử lại"."""
    head, _ = start_error("config_dir", detail)
    t = QCoreApplication.translate
    return head, [t("Texts", "Hãy kiểm tra quyền ghi vào thư mục đó rồi mở lại Teleport.")]


def not_responding() -> tuple[str, list[str]]:
    """Bản Teleport khác giữ `gui.lock` mà không trả lời lệnh `show` (thường là đang tắt)."""
    t = QCoreApplication.translate
    return (
        t("Texts", "Teleport đang tắt hoặc chưa phản hồi."),
        [t("Texts", "Hãy thử lại sau giây lát.")],
    )


# ---- Cửa sổ, khay --------------------------------------------------------------------------


def still_running() -> str:
    """M4: bóng thoại khi đóng cửa sổ lần đầu trong phiên."""
    return QCoreApplication.translate(
        "Texts",
        "Teleport vẫn chạy ở khay hệ thống và vẫn nhận file. Chọn Thoát trong menu khay để tắt hẳn.",
    )


def menu_open() -> str:
    return QCoreApplication.translate("Texts", "Mở Teleport")


def menu_send() -> str:
    return QCoreApplication.translate("Texts", "Gửi file…")


def menu_pause() -> str:
    return QCoreApplication.translate("Texts", "Tạm dừng nhận")


def menu_quit() -> str:
    return QCoreApplication.translate("Texts", "Thoát")


def retry() -> str:
    """Nút “Thử lại” (phím tắt F5 ở cửa sổ chính)."""
    return QCoreApplication.translate("Texts", "Thử lại")


# ---- Nhận file -----------------------------------------------------------------------------


def offer_waiting() -> str:
    """Bóng thoại của khay khi có `offer` đang chờ trả lời."""
    return QCoreApplication.translate("Texts", "Có file đang chờ bạn nhận.")
