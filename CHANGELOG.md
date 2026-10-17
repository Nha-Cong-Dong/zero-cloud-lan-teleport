# Changelog

Mọi thay đổi đáng chú ý của dự án được ghi ở đây.
Định dạng theo [Keep a Changelog](https://keepachangelog.com/vi/1.1.0/), phiên bản theo [SemVer](https://semver.org/lang/vi/).

## [Unreleased]

### Added
- Khung dự án Python (`teleport/`), CI chạy ruff + pytest trên Windows, macOS, Linux.
- `teleport.core.framing`: khung message điều khiển `[4 byte độ dài][JSON]`.
- `teleport.net.identity`: danh tính thiết bị gồm khóa ECDSA P-256, chứng chỉ tự ký và fingerprint SHA-256.
- `teleport.net.tls`: TLS 1.3 với ghim fingerprint.
- Ví dụ `examples/chat_tls.py`: chat 1-1 qua TLS trong LAN.
