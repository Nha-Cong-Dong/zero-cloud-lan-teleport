"""Tầng giao diện PySide6 (`teleport-gui`); chỉ tầng này import Qt (docs/architecture.md mục 1).

Qt chạy ở thread chính; node chạy trong thread riêng với loop asyncio chuẩn và chỉ được chạm tới
qua `bridge.NodeHost` (không bao giờ gọi `Node` từ thread Qt). Mọi chữ người dùng thấy đi qua
`tr()` / `QCoreApplication.translate` (chuỗi nguồn tiếng Việt, `i18n`); chữ do máy khác đặt
(tên máy, tên file) đi qua `core.display` và hiện dạng chữ thuần, không HTML.

Module: `app` (điểm vào, một bản mỗi thư mục cấu hình, ráp các phần), `bridge` (thread node),
`main_window`, `tray`, `offer_prompt` (hộp hỏi nhận file tạm tới P4-6), `texts` (câu chữ dùng
chung), `i18n`, `theme`.
"""
