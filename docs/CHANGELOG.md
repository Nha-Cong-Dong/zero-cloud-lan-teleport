# Changelog

Mọi thay đổi đáng chú ý của dự án được ghi ở đây.
Định dạng theo [Keep a Changelog](https://keepachangelog.com/vi/1.1.0/), phiên bản theo [SemVer](https://semver.org/lang/vi/).

## [Unreleased]

### Added
- Giấy phép GPL-3.0-or-later (`LICENSE`), khai báo trong `pyproject.toml`.
- Khung dự án Python, CI chạy ruff + pytest trên Windows, macOS, Linux.
- `teleport.core.framing`: mã hóa/giải mã khung message điều khiển `[4 byte độ dài][JSON]` (sans-IO).
- `teleport.net.stream`: đọc/ghi message điều khiển trên kết nối asyncio.
- `teleport.trust.identity`: danh tính thiết bị gồm khóa ECDSA P-256, chứng chỉ tự ký và fingerprint SHA-256.
- `teleport.net.tls`: TLS 1.3 với ghim fingerprint phía client và mTLS phía server (chỉ nhận thiết bị tin cậy).
- Lệnh `teleport` (và `python -m teleport`), lệnh con `teleport identity`.
- Thư mục cấu hình chuẩn theo hệ điều hành; biến môi trường `TELEPORT_HOME` để ghi đè.
- Tài liệu `docs/architecture.md`, `docs/protocol.md` (dự thảo giao thức phiên bản 1), `docs/SECURITY.md`; toàn bộ tài liệu gom vào `docs/`.
- Phân tầng `core ← trust ← net ← app ← ui / cli` được kiểm tra tự động; `core` bắt buộc sans-IO.
- `teleport.core.messages`: kiểu và kiểm tra hợp lệ các message `hello`, `pair_request`, `pair_challenge`, `pair_reveal`, `pair_confirm`, `pair_abort`.
- `teleport.core.pairing_code`: mã ghép đôi 6 số tính từ hai fingerprint và hai nonce; bên khởi xướng cam kết nonce trước khi thấy nonce của bên kia.
- `teleport.trust.identity`: `PeerCert`, `peer_cert_from_pem` và `peer_cert_from_der` để kiểm tra chứng chỉ của máy khác.
- `teleport.trust.store`: kho thiết bị đã ghép đôi `trusted.json`, ghi nguyên tử.
- `teleport.net.lock`: `NodeLock` giữ `node.lock`, bảo đảm một node cho mỗi thư mục cấu hình (Windows và POSIX).
- Phụ thuộc phát triển `hypothesis` (MPL-2.0) cho test thuộc tính.

### Changed
- `docs/protocol.md` mục 5 (dự thảo, chưa phát hành): mã ghép đôi dùng nonce cam kết thay vì chỉ hai fingerprint; bên khởi xướng gửi chứng chỉ trong `pair_request` thay vì trong bắt tay TLS.
- `docs/protocol.md` mục 4 và 6 (dự thảo): `block_hashes` gửi theo lô 64 khối; bit trong bitfield chỉ bật khi khối đã ghi xuống đĩa và khớp hash; lượng gửi lại khi nối lại tối đa là phần chưa nhận cộng một lô.
- README: thêm giá trị “Không sợ rớt mạng” và mục “Nguyên tắc sản phẩm”; mô tả ghép đôi theo nonce cam kết; cập nhật hiện trạng và lộ trình.
- `docs/protocol.md` mục 4 và 6 (dự thảo): lô `block_hashes` cố định 64 khối; bên nhận không đọc lô kế tiếp khi lô trước chưa xác minh (hai kết nối không có thứ tự với nhau); `fsync` `.part` trước khi ghi state; dữ liệu `clipboard` gốc ≤ 10 MiB.
- Tài liệu sản phẩm và kế hoạch đưa vào repo: `docs/y_tuong.md` (ý tưởng, nguyên tắc, tầm nhìn), `docs/mvp.md` (phạm vi, tính năng F1–F12, yêu cầu N1–N12), `docs/ke_hoach.md` (thiết kế, lộ trình, trạng thái). `docs/README.md` thành mục lục chung kèm thuật ngữ và quy tắc đồng bộ.

### Security
- `teleport.trust.identity`: chứng chỉ từ máy khác có trường version sai (ví dụ “v6”) bị từ chối bằng `InvalidCertificate`, thay vì làm văng lỗi `InvalidVersion` không kiểm soát (với `cryptography` bản mới, lỗi này bật ra ngay lúc đọc chứng chỉ).
- Từ chối mọi chứng chỉ có dấu hiệu CA (BasicConstraints CA=true, KeyUsage keyCertSign/cRLSign, Netscape Cert Type), vì chứng chỉ tin cậy được dùng làm trust anchor cho mTLS.
- Tên thiết bị và phiên bản app nhận từ máy khác không được chứa ký tự điều khiển, ký tự vô hình hay ký tự đổi hướng chữ, để không giả mạo được tên hiển thị.
- Mã ghép đôi có nonce cam kết: kẻ đứng giữa không thể sinh khóa offline cho tới khi trùng mã.
- `teleport.core.framing`: JSON có số nguyên quá dài hoặc lồng quá sâu báo `FramingError` thay vì lỗi không kiểm soát.
- `trusted.json` hỏng thì cả file bị từ chối, và không bao giờ bị tự xóa hay ghi đè.
