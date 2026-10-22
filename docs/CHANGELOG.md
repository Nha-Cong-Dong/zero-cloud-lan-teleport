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
- `teleport.core.messages`: kiểu và kiểm tra hợp lệ các message `hello`, `pair_request`, `pair_commit`, `pair_nonce`, `pair_reveal`, `pair_confirm`, `pair_abort`.
- `teleport.core.pairing_code`: mã ghép đôi 6 số tính từ hai fingerprint và hai nonce; bên trả lời cam kết nonce trước khi thấy nonce của bên kia.
- `teleport.trust.identity`: `PeerCert`, `peer_cert_from_pem` và `peer_cert_from_der` để kiểm tra chứng chỉ của máy khác.
- `teleport.trust.store`: kho thiết bị đã ghép đôi `trusted.json`, ghi nguyên tử.
- `teleport.lock`: `NodeLock` giữ `node.lock`, bảo đảm một node cho mỗi thư mục cấu hình (Windows và POSIX). Là tiện ích dùng chung như `paths`, vì `trust` cũng dùng nó khi tạo danh tính. `acquire(timeout=...)` chờ khóa được nhả (Windows nhả khóa của tiến trình bị kill không đồng bộ).
- Phụ thuộc phát triển `hypothesis` (MPL-2.0) cho test thuộc tính.
- `hello` có thêm trường `os` (`windows`, `macos`, `linux`) để hiện biểu tượng hệ điều hành (kiểm bằng `teleport.core.messages.validate_os`, dùng chung với TXT mDNS).
- `teleport.net.tls.verify_fingerprint`: client so **toàn bộ** fingerprint của server.
- Giới hạn message 16 KiB cho kết nối ghép đôi (`MAX_PAIRING_MESSAGE_SIZE`, `read_message(..., max_size)`).
- `teleport.core.pairing`: máy trạng thái ghép đôi sans-IO cho bên khởi xướng (`InitiatorSession`) và bên trả lời (`ResponderSession`): thứ tự message `pair_*`, hết giờ (10 s mỗi message, 60 s chờ người dùng xác nhận), lý do hủy, không ghép đôi đè danh tính khác; `PairingGate` chống làm phiền: một phiên ghép đôi tại một thời điểm, chờ tăng dần theo IP (10 s → 10 phút), phân xử khi hai bên cùng chủ động ghép đôi.
- `teleport.core.messages`: kiểu và kiểm tra hợp lệ các message kết nối điều khiển `control_open`, `ping`, `pong`; hằng số `MAX_SAFE_INTEGER` (2⁵³ − 1).
- Khám phá thiết bị trong LAN qua mDNS (`_teleport._tcp.local.`, `teleport.core.discovery` và `teleport.net.discovery`): thiết bị hiện ra khi mở app, biến mất khi tắt app hoặc mất mạng đột ngột (thiết kế để đạt mvp N1, sẽ đo trên 2 máy thật); thiết bị đã ghép đôi luôn được theo dõi, kể cả khi danh sách đã đầy (protocol mục 8).
- Phụ thuộc chạy `zeroconf` (LGPL-2.1-or-later, `>=0.148,<0.152`) và `ifaddr` (MIT).
- `teleport.net.connection`: node lắng nghe trên một cổng (mặc định 47474), nhận kết nối ghép đôi và điều khiển, giới hạn kết nối và thời gian chờ chống lũ, keepalive `ping`/`pong`, đổi danh sách tin cậy có hiệu lực ngay mà không mở lại cổng; client thử song song mọi địa chỉ.
- `teleport.core.keepalive`: keepalive sans-IO (khi nào gửi `ping`, khi nào coi là mất kết nối).
- `teleport.core.messages`: bảng loại kết nối `ConnectionKind` / `allowed_types` / `opening_kind`.
- Ghép đôi hai máy qua mạng bằng dòng lệnh: `teleport listen` (chờ ghép đôi, hiện mã 6 số, hỏi xác nhận `[c/k]`), `teleport pair <ip[:cổng]>`, `teleport unpair <thiết bị>` (tên hoặc tiền tố mã thiết bị, khi không có node nào chạy); `--json` (JSON Lines cho `listen` / `pair`); mã thoát 0 (thành công), 1 (lỗi lúc chạy), 2 (lệnh sai).
- `teleport.app`: `Node` (giữ `node.lock`, lắng nghe, nhận kết nối điều khiển, tối đa một cho mỗi thiết bị), `PairingService` (luồng ghép đôi trên mạng, chống làm phiền, nhường phiên khi hai bên cùng chủ động), cài đặt (`settings.json`), tìm thiết bị theo tên / tiền tố mã.
- Cổng 47474 bận thì hệ điều hành chọn cổng khác, lưu vào `settings.json` để lần sau dùng lại.
- Hủy ghép đôi khi node đang chạy đóng ngay kết nối đang mở với máy bị hủy.
- `teleport.atomic`: ghi file nguyên tử dùng chung (`write_atomic`).
- `teleport list` (`--timeout`, `--json`): liệt kê các máy Teleport trong mạng (tên, mã, hệ điều hành, đã/chưa ghép đôi, địa chỉ), không giữ `node.lock` và không quảng bá máy này; gợi ý ghép đôi lại khi một máy đã ghép đôi có vẻ vừa cài lại.
- `teleport pair <tên | tiền tố mã>`: tìm máy qua mDNS rồi ghép đôi, không phải gõ IP; trùng tên thì liệt kê để người dùng gõ cụ thể hơn.
- `teleport listen` quảng bá qua mDNS, tự kết nối và giữ kết nối với các thiết bị đã ghép đôi, in sự kiện thiết bị (thấy, rời mạng) và kết nối (được, mất, bị từ chối, danh tính đổi).
- Tự nối lại kết nối điều khiển theo lịch 1, 2, 4… ≤ 30 s; chống trùng khi hai máy gọi nhau cùng lúc (`teleport.core.links`).
- Quảng bá lại mDNS khi mạng đổi hoặc máy thức dậy.
- `teleport identity` hiện cổng của máy này.

### Changed
- `docs/protocol.md` (dự thảo, chưa phát hành): mọi kết nối đi vào **một cổng** (mặc định 47474) với `CERT_OPTIONAL`; ba loại kết nối ghép đôi / điều khiển / dữ liệu, client khai báo loại bằng `pair_request` / `control_open` / `data_open` ngay sau `hello`; kết nối dữ liệu đi cùng chiều với kết nối điều khiển (qua được tường lửa một chiều); giới hạn kích thước, thời gian chờ, keepalive; chống làm phiền khi ghép đôi (chờ tăng dần theo IP); không ghép đôi đè danh tính khác; một kết nối điều khiển cho mỗi cặp máy; mDNS TTL 60 s; hủy ghép đôi có hiệu lực ngay; quy tắc tăng số phiên bản giao thức từ `v0.1.0-alpha`.
- `docs/protocol.md` mục 4 (dự thảo): hash SHA-256 của mỗi khối nằm trong header khung khối, thay cho `block_hashes` theo lô 64 khối trên kênh điều khiển; `fsync` mỗi 16 khối; `resume{seq}` là cơ chế gửi lại duy nhất (bỏ `corrupt`), mỗi kết nối dữ liệu phục vụ đúng một `resume`; mọi message của lượt truyền gửi lại được và hai bên đồng bộ lại sau mỗi kết nối điều khiển mới; mỗi chiều giữa hai máy chạy một lượt truyền; bên nhận đọc lại toàn bộ file từ đĩa trước khi đổi tên (phát hiện byte bị sửa lúc tạm dừng); đổi tên không ghi đè trên mọi hệ file; file tạm nằm trong `.teleport/<transfer_id>/`.
- `docs/protocol.md` (dự thảo): IPv4, thử song song mọi địa chỉ được quảng bá, cổng dự phòng lưu lại; tên instance mDNS là `device_id`, TXT `name` ≤ 200 byte; quy tắc trường `ping`/`pong`/`seq`; `seq` có phạm vi một kết nối điều khiển; trạng thái mức lượt truyền `transfer.json`; chính sách nối lại khi không có lượt truyền và khi bị từ chối; `teleport pair` nhận `ip[:cổng]`.
- `docs/mvp.md` mục 8: thêm 4 thông báo (máy kia chưa sẵn sàng ghép đôi; nhập IP mà không kết nối được; danh tính không hợp lệ khi ghép đôi; ghép đôi với chính máy này).
- `teleport.net.tls.server_context(identity, trusted_certs, *, allow_anonymous=False)`: bỏ chế độ `trusted_certs=None` (không xác thực client); `allow_anonymous=True` cho cổng chung.
- `docs/protocol.md` mục 5 (dự thảo, chưa phát hành): mã ghép đôi dùng nonce cam kết thay vì chỉ hai fingerprint; bên khởi xướng gửi chứng chỉ trong `pair_request` thay vì trong bắt tay TLS.
- `docs/protocol.md` mục 5 (dự thảo, chưa phát hành): **bên trả lời cam kết nonce trước** (kiểu Bluetooth numeric comparison). Luồng mới `pair_request{cert}` → `pair_commit{commitment}` → `pair_nonce{nonce = n_A}` → bên trả lời hiện mã rồi mới gửi `pair_reveal{nonce = n_B}` → bên khởi xướng kiểm cam kết rồi hiện mã; `pair_request` bỏ trường `commitment`, bỏ message `pair_challenge`; cam kết tính trên fingerprint của bên trả lời và n_B (test vector mục 5.4 đổi dòng `commitment`, mã giữ nguyên).
- `docs/protocol.md` mục 2.3 (dự thảo, chưa phát hành): máy đã gửi `pair_confirm` chờ thêm 10 s (`CONFIRM_GRACE`, tới 70 s kể từ lúc hiện mã) để nhận `pair_confirm` của máy kia; quyết định tới sau hạn chót vẫn là hết giờ.
- `docs/protocol.md` mục 5.1 (dự thảo): bên khởi xướng phát hiện lỗi trước khi gửi `pair_request` (CN không khớp `hello`, tự ghép với chính mình, trùng danh tính) thì chỉ đóng kết nối, không gửi gì; `hello` thứ hai, message có `type` chưa biết và `pair_confirm` nhận lần hai trên kết nối ghép đôi đều là `pair_abort{invalid}`.
- `docs/protocol.md` mục 5.3 (dự thảo): chi tiết chờ tăng dần theo IP: chỉ tính phiên mà máy này là bên trả lời; mất kết nối và trùng danh tính tính là thất bại, phiên do node tự hủy thì không; `pair_abort{busy}` lúc đang chờ không kéo dài thời gian chờ; quên IP sau 10 phút yên lặng, theo dõi tối đa 1024 IP, bộ đếm chỉ nằm trong bộ nhớ. Luật nhường khi hai bên cùng chủ động: bên có `device_id` lớn hơn hủy phiên khởi xướng của mình và trả lời phiên của bên kia, bên nhỏ hơn trả `pair_abort{busy}`; luật này xét trước việc node có đang nhận ghép đôi không và trước thời gian chờ của IP.
- `docs/protocol.md` mục 3.3 (dự thảo): trường `t` của `ping`/`pong` có cận trên 2⁵³ − 1 và không nhận `true`/`false` hay số thực; quy tắc trả `pong` chuyển về mục 2.3 (keepalive).
- `docs/protocol.md` mục 8 (dự thảo): SRV trỏ tới `teleport-<device_id>.local.`; quy tắc khóa TXT (chữ thường, khóa lạ bỏ qua, `id` phải bằng nhãn instance, `proto` ≤ 2⁵³ − 1); bên nhận lọc địa chỉ (chỉ IPv4 dùng được, tối đa 16); tối đa 256 thiết bị theo dõi; kiểm còn sống bằng resolve lại theo chu kỳ (zeroconf giữ bản ghi PTR ≥ 1125 s), cần 2 lần thất bại liên tiếp mới xóa thiết bị, bản ghi chưa từng hợp lệ bị bỏ sau 2 lần; resolve lần đầu được ưu tiên nhưng luôn dành chỗ cho kiểm còn sống; thiết bị đã ghép đôi không tính vào giới hạn 256; dịch vụ đến khi đã đủ vào danh sách chờ (≤ 1024) và được nhận khi có chỗ; thiết bị đã biến mất được kiểm thưa dần (tối đa 240 s); ba kiểu từ chối dịch vụ đã biết qua mDNS. Mục 2.1: cách bỏ card mạng ảo khi quảng bá địa chỉ; địa chỉ nguồn ưu tiên là nguồn của tuyến multicast, hoặc của tuyến mặc định nếu tuyến multicast cho loopback.
- `docs/protocol.md` (dự thảo): giới hạn kết nối đang mở đầu 4 mỗi IP, 64 tổng (mục 2.3); keepalive tính theo byte; gửi không tiến triển 30 s trên kết nối điều khiển thì đóng; lỗi giao thức trên kết nối điều khiển thì đóng (mục 3.3); “bị từ chối” chỉ tính sau khi bắt tay TLS xong (mục 2.2, 6.2); đổi danh sách tin cậy bằng context mới cho từng kết nối thay vì mở lại listener (mục 7).
- README: thêm giá trị “Không sợ rớt mạng” và mục “Nguyên tắc sản phẩm”; mô tả ghép đôi theo nonce cam kết; cập nhật hiện trạng và lộ trình.
- Tài liệu sản phẩm và kế hoạch đưa vào repo: `docs/y_tuong.md` (ý tưởng, nguyên tắc, tầm nhìn), `docs/mvp.md` (phạm vi, tính năng F1–F12, yêu cầu N1–N12), `docs/ke_hoach.md` (thiết kế, lộ trình, trạng thái). `docs/README.md` thành mục lục chung kèm thuật ngữ và quy tắc đồng bộ.

### Fixed
- `teleport --config-dir <file> identity` in traceback; nay báo “Không dùng được thư mục cấu hình” và thoát mã 1.
- Dừng node đúng lúc đang ghép đôi có thể để phiên ghép đôi chạy tiếp sau khi đã nhả `node.lock`.
- Hai máy cùng ghép đôi với nhau khi máy kia có nhiều địa chỉ: luật nhường nay xét mọi địa chỉ của máy đích.
- Hủy khởi động node đúng lúc vừa mở cổng có thể để cổng vẫn nhận kết nối tới hết tiến trình dù đã nhả `node.lock`.

### Removed
- `teleport.net.tls.verify_pin`, `normalize_pin`, `MIN_PIN_HEX`: không tính năng nào xác thực bằng tiền tố fingerprint; pin ngắn chỉ còn để hiển thị.

### Security
- Ghép đôi qua CLI chỉ lưu thiết bị khi người dùng xác nhận `[c/k]` ở cả hai máy sau khi mã đã hiện (dòng gõ sẵn trước lúc hiện mã bị bỏ); hủy ghép đôi đóng ngay kết nối đang mở với máy đó; mỗi thiết bị tin cậy chỉ giữ một kết nối điều khiển.
- Danh tính thiếu một nửa, hỏng hoặc khóa không khớp chứng chỉ báo `IdentityError` (CLI thoát mã 1, không in traceback) thay vì âm thầm tạo danh tính mới làm mất mọi ghép đôi.
- Khóa riêng được tạo với quyền `0o600` ngay từ đầu (trước đây có một khoảnh khắc người khác đọc được trên Linux/macOS); danh tính được tạo qua file tạm và đổi tên không ghi đè.
- Chứng chỉ thiết bị có hiệu lực từ 2000-01-01 tới 9999-12-31: không ngày nào phụ thuộc đồng hồ của máy tạo, nên máy lệch đồng hồ vẫn bắt tay mTLS được và danh tính không hết hạn.
- Tạo danh tính trong khi giữ `identity.lock`: nhiều tiến trình chạy lần đầu cùng lúc nhận cùng một danh tính. Mất danh tính mà `trusted.json` còn thiết bị (hoặc không đọc được) thì báo lỗi thay vì tạo mới; danh sách rỗng thì được tạo mới.
- Server TLS không cấp session ticket, để máy đã bị hủy ghép đôi không dùng lại session cũ mà vào được.
- Cổng của node chống lũ kết nối và slowloris: tối đa 4 kết nối đang mở đầu mỗi IP (64 tổng), kết nối ẩn danh chờ ghép đôi 1 mỗi IP (8 tổng), hạn 10 s cho cả chuỗi bắt tay + `hello` + message mở đầu; kết nối điều khiển chỉ được nhận khi chứng chỉ còn trong danh sách tin cậy tại lúc nhận `control_open`.
- `teleport.trust.identity`: chứng chỉ từ máy khác có trường version sai (ví dụ “v6”) bị từ chối bằng `InvalidCertificate`, thay vì làm văng lỗi `InvalidVersion` không kiểm soát (với `cryptography` bản mới, lỗi này bật ra ngay lúc đọc chứng chỉ).
- Từ chối mọi chứng chỉ có dấu hiệu CA (BasicConstraints CA=true, KeyUsage keyCertSign/cRLSign, Netscape Cert Type), vì chứng chỉ tin cậy được dùng làm trust anchor cho mTLS.
- Tên thiết bị và phiên bản app nhận từ máy khác không được chứa ký tự điều khiển, ký tự vô hình hay ký tự đổi hướng chữ, để không giả mạo được tên hiển thị.
- Mã ghép đôi có nonce cam kết: kẻ đứng giữa không thể sinh khóa offline cho tới khi trùng mã.
- Kẻ đứng giữa không còn dò được mã của máy đang chờ ghép đôi mà máy đó không hiện mã: trước đây nó đóng vai bên khởi xướng, tính được mã trước khi phải lộ nonce rồi bỏ phiên và thử lại; nay bên trả lời cam kết trước và hiện mã trước khi lộ nonce của mình.
- `teleport.core.framing`: JSON có số nguyên quá dài hoặc lồng quá sâu báo `FramingError` thay vì lỗi không kiểm soát.
- `trusted.json` hỏng thì cả file bị từ chối, và không bao giờ bị tự xóa hay ghi đè.
- `teleport.core.pairing` chỉ lưu thiết bị khi máy này đã gửi **và** đã nhận `pair_confirm`; cam kết nonce sai, chứng chỉ không khớp `hello` hay tự ghép với chính mình đều hủy phiên trước khi hiện mã.
- Khám phá mDNS chịu được bản ghi độc hại trong LAN: bản ghi sai bị bỏ qua mà không làm dừng khám phá; mỗi thiết bị có tối đa một lần resolve đang chạy (chặn lũ gói xuất hiện/biến mất làm tăng số tác vụ); tối đa 256 thiết bị (cộng thiết bị đã ghép đôi), 1024 dịch vụ chờ, 16 địa chỉ, 16 resolve đồng thời; thiết bị đã ghép đôi luôn được theo dõi và được resolve trước hàng chờ, nên lũ bản ghi giả chỉ làm nó hiện chậm khoảng một lần hết giờ resolve (vài giây). mDNS không xác thực: danh tính thiết bị chỉ được tin qua fingerprint TLS.
