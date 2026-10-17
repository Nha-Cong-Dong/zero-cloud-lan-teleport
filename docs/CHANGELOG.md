# Changelog

Mọi thay đổi đáng chú ý của dự án được ghi ở đây.
Định dạng theo [Keep a Changelog](https://keepachangelog.com/vi/1.1.0/), phiên bản theo [SemVer](https://semver.org/lang/vi/).

## [Unreleased]

### Added
- Khung dự án Python, CI chạy ruff + pytest trên Windows, macOS, Linux.
- `teleport.core.framing`: khung message điều khiển `[4 byte độ dài][JSON]`.
- `teleport.net.identity`: danh tính thiết bị gồm khóa ECDSA P-256, chứng chỉ tự ký và fingerprint SHA-256.
- `teleport.net.tls`: TLS 1.3 với ghim fingerprint.
- Lệnh `teleport` (và `python -m teleport`), lệnh con `teleport identity`.
- Thư mục cấu hình chuẩn theo hệ điều hành; biến môi trường `TELEPORT_HOME` để ghi đè.
- Tài liệu `docs/architecture.md`, `docs/protocol.md` (dự thảo giao thức phiên bản 1), `docs/SECURITY.md`; toàn bộ tài liệu gom vào `docs/`.
