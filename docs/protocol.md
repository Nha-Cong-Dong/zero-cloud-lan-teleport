# Giao thức Teleport

**Phiên bản giao thức: 1 (dự thảo).** Phiên bản giao thức đánh số riêng, độc lập với phiên bản app. Quy tắc tăng số: mục 9.

Trạng thái từng phần: ✅ đã cài đặt · 📝 dự thảo, chưa cài đặt.

## 1. Danh tính thiết bị ✅

- Mỗi thiết bị có một khóa **ECDSA P-256** và một **chứng chỉ X.509 tự ký** (chữ ký ECDSA-SHA256), hiệu lực từ `2000-01-01T00:00:00Z` tới `9999-12-31T23:59:59Z` (“không hết hạn”, RFC 5280 mục 4.1.2.5), nên không ngày nào phụ thuộc đồng hồ của máy tạo danh tính. Khi bắt tay, cả hai chiều bỏ kiểm ngày hiệu lực (`NO_CHECK_TIME`, mục 2.1) ✅, nên đồng hồ sai của máy nào cũng không làm hỏng kết nối.
- `CN` của chứng chỉ là `teleport-<device_id>`, trong đó `device_id` là 32 ký tự hex (UUID4).
- **Fingerprint** = SHA-256 của chứng chỉ ở dạng DER, viết dưới dạng 64 ký tự hex thường. Đây là định danh duy nhất được tin cậy; tên thiết bị chỉ để hiển thị.
- **Chữ ký bằng khóa danh tính** (ngoài chữ ký tự ký của chứng chỉ): mọi payload được ký phải bắt đầu bằng một **nhãn riêng** dạng `teleport-…-vN` (ví dụ `teleport-initiator-proof-v1`, mục 5.2), mỗi mục đích một nhãn, để chữ ký làm cho việc này không bao giờ dùng lại được cho việc khác.
- **Pin ngắn** = 8 byte đầu của fingerprint, dạng `AB:CD:EF:…`. Chỉ để hiển thị (so bằng mắt); máy không bao giờ xác thực bằng pin ngắn.
- **Danh tính gắn với thư mục cấu hình, không gắn với máy.** Xóa thư mục cấu hình (gỡ cài đặt không giữ cài đặt, hoặc cài lại sạch) sinh `device_id` và fingerprint mới: với các máy khác, đó là một **thiết bị mới**, chưa ghép đôi (mục 7).
- File danh tính thiếu một nửa, hỏng, khóa không khớp chứng chỉ, hoặc mất danh tính mà vẫn còn `trusted.json` → app báo lỗi và dừng, **không bao giờ** tự tạo danh tính mới (làm vậy sẽ âm thầm làm hỏng mọi ghép đôi). Cách tạo và lưu: [architecture.md](architecture.md) mục 4.

## 2. Tầng vận chuyển

### 2.1. Một cổng, ba loại kết nối

- TCP + **TLS 1.3** ✅. Phiên bản thấp hơn bị từ chối.
- Mỗi node lắng nghe trên **một cổng TCP**, mặc định **47474** (listener ✅ `teleport.net.connection`). Cổng mặc định (hoặc cổng đã lưu) không mở được thì node để hệ điều hành chọn một cổng trống và lưu vào `settings.json` để lần chạy sau dùng lại ✅ (`teleport.app.node`); cổng đó hiện trong `teleport identity` / `teleport list` ✅, và cổng thật luôn được quảng bá qua mDNS (mục 8) ✅ (`teleport.app.node`). Nhập IP thủ công nhận `ip` (dùng cổng mặc định) hoặc `ip:cổng` ✅ (`teleport pair`).
- Node nhận **TCP trần** trên `0.0.0.0` rồi bắt đầu TLS cho từng kết nối bằng `SSLContext` **hiện tại** ✅, nên đổi danh sách tin cậy không phải mở lại cổng (mục 7).
- **Địa chỉ:** v1.0 chỉ dùng IPv4. Node lắng nghe trên mọi địa chỉ IPv4 (`0.0.0.0`) và quảng bá qua mDNS các địa chỉ IPv4 của máy (tối đa 16, mục 8; chưa lọc theo trạng thái bật/tắt của card), bỏ loopback, link-local `169.254.0.0/16` và các card ảo nhận ra được ✅. Card ảo nhận ra theo tên card (tiền tố như `docker`, `veth`, `vEthernet`, `tun`, `wg`, `utun`, hoặc chứa `virtual`, `hyper-v`, `wsl`, `vpn`…; danh sách đầy đủ ở `teleport.core.discovery`). Hai ngoại lệ: luôn giữ **địa chỉ nguồn ưu tiên** dù card đó trông như card ảo — đó là địa chỉ nguồn hệ điều hành chọn cho tuyến tới một địa chỉ ngoài LAN (thường là tuyến mặc định), vì máy ra ngoài qua chính card đó (ví dụ Hyper-V External vSwitch); không xác định được hoặc là loopback thì không có địa chỉ ưu tiên (LAN không có gateway: chỉ còn các địa chỉ qua bộ lọc card ảo, sắp tăng dần); và nếu mọi địa chỉ đều nằm trên card ảo thì quảng bá tất cả thay vì không quảng bá gì. Bên gửi đặt địa chỉ ưu tiên đầu danh sách của mình, nhưng thứ tự này không có ý nghĩa trên dây: bên nhận sắp lại tăng dần (mục 8). Client thử **song song** mọi địa chỉ được quảng bá của một thiết bị (kết nối ghép đôi và điều khiển; kết nối dữ liệu: mục 4.1) và giữ kết nối thành công đầu tiên, nên một địa chỉ chết không làm chậm việc kết nối (mvp F2, N1). Mỗi lần thử chỉ đi tới khi đã đọc và kiểm `hello` của server; message mở đầu (bảng bên dưới) chỉ được gửi trên kết nối thắng, sau khi các lần thử khác đã bị đóng, nên server không nhận kết nối điều khiển thừa ✅.
- Server dùng `CERT_OPTIONAL` ✅ và nạp chứng chỉ của mọi thiết bị tin cậy làm trust anchor, kèm kiểm tra X.509 nghiêm ngặt (`teleport.net.tls.server_context(..., allow_anonymous=True)`):
  - Client **gửi chứng chỉ** → chứng chỉ phải nằm trong danh sách tin cậy, nếu không bắt tay thất bại (kể cả khi danh sách rỗng). Kiểm chứng bởi `tests/net/test_tls.py`.
  - Client **không gửi chứng chỉ** → kết nối ẩn danh, chỉ được dùng để ghép đôi.
- Client của kết nối điều khiển dùng `CERT_REQUIRED` với **đúng chứng chỉ đã lưu** của thiết bị đó làm trust anchor duy nhất, không CA hệ thống, không kiểm hostname ✅ (`teleport.net.tls.trusted_client_context`). Trong TLS 1.3, client chỉ gửi chứng chỉ của mình sau khi đã kiểm xong chứng chỉ server, nên server đưa chứng chỉ khác thì bắt tay hỏng ở client **trước khi** chứng chỉ client rời máy (mục 2.2, 8). Client của kết nối ghép đôi không gửi chứng chỉ và không kiểm chứng chỉ server trong bắt tay (`CERT_NONE`; xác thực bằng mã 6 số, mục 5).
- Cả hai chiều bật kiểm tra X.509 nghiêm ngặt (`VERIFY_X509_STRICT`) và **bỏ kiểm ngày hiệu lực** (`X509_V_FLAG_NO_CHECK_TIME`) ✅: mỗi bên ghim đúng chứng chỉ đã lưu, nên ngày hiệu lực không thêm gì về an toàn, còn đồng hồ sai (ví dụ máy hết pin CMOS) thì không được làm hỏng kết nối hay bị báo thành giả mạo.
- Ngay sau bắt tay, hai bên gửi `hello`. Rồi **client khai báo loại kết nối bằng message đầu tiên của nó**. Trước khi đọc được message này, server không gửi gì ngoài `hello`. Sai loại hoặc quá giờ → đóng. Thứ tự ✅:
  - Server gửi `hello` ngay sau bắt tay (trừ kết nối ẩn danh vượt giới hạn ✅ và kết nối ẩn danh tới máy đang ẩn 📝, mục 2.3), rồi đọc `hello` của client, rồi đọc message mở đầu. Cả hai được đọc với giới hạn 16 KiB, trong **cùng một hạn 10 s cho cả chuỗi** (tính từ lúc nhận TCP, gồm cả bắt tay TLS), không gia hạn khi có byte tới.
  - Client của kết nối điều khiển kiểm chứng chỉ server ngay trong bắt tay (gạch trên), rồi còn so toàn bộ fingerprint của server **trước khi gửi bất kỳ message nào** (kể cả `hello`) ✅.

| Loại kết nối | Client gửi chứng chỉ? | Message đầu tiên của client sau `hello` | Dùng cho |
|---|---|---|---|
| **Ghép đôi** | Không | `pair_request` | `pair_*` (mục 5) |
| **Điều khiển** | Có, đã tin cậy | `control_open` | Mọi message ở mục 3 trừ `pair_*`, `control_open`, `data_open` |
| **Dữ liệu** | Có, đã tin cậy | `data_open` | Khung khối nhị phân (mục 4) |

### 2.2. Xác thực hai chiều

- **Client kiểm tra server:** không dùng CA hệ thống hay hostname. Kết nối điều khiển dùng `CERT_REQUIRED` với chứng chỉ đã lưu trong `trusted.json` làm trust anchor duy nhất ✅ (mục 2.1): server đưa chứng chỉ khác thì bắt tay hỏng ở client (`ssl.SSLCertVerificationError`) → `IdentityMismatch` ✅. Sau khi bắt tay, client còn so **toàn bộ** fingerprint chứng chỉ server với fingerprint đã lưu (`verify_fingerprint`) ✅, và kiểm `CN` khớp `hello.device_id` của server ✅. Không khớp → đóng kết nối, báo người dùng (mvp mục 8, “Danh tính thiết bị thay đổi”), và không tự thử lại theo lịch (mục 6.2).
- Máy kia đưa đúng chứng chỉ công khai của thiết bị tin cậy nhưng không có khóa riêng: bắt tay hỏng với một lỗi SSL khác (không ký được CertificateVerify) → “không kết nối được” (`Unreachable`) ✅. Kẻ đó không thu được gì: chứng chỉ client chưa được gửi.
- **Server kiểm tra client:** OpenSSL đã kiểm chứng chỉ trong lúc bắt tay; server còn kiểm `CN` khớp `hello.device_id` của client ✅.
- **Không có session ticket** ✅ (`num_tickets = 0`): kết nối dùng lại session sẽ bỏ qua bước kiểm chứng chỉ client, nên máy vừa bị hủy ghép đôi có thể vào lại bằng session cũ. Mỗi kết nối đều bắt tay đầy đủ với danh sách tin cậy hiện tại.
- Ghép đôi (kết nối ẩn danh): client lấy chứng chỉ server từ bắt tay TLS; server nhận chứng chỉ client trong `pair_request`, và client chứng minh giữ khóa riêng của chứng chỉ đó bằng chữ ký trong `pair_nonce` (mục 5.1, 5.2).
- **Lưu ý TLS 1.3:** client bị từ chối vẫn thấy kết nối mở thành công; lỗi chỉ lộ ra ở lần đọc đầu tiên. Vì vậy client chỉ coi là đã kết nối khi **đọc được `hello` của server**, trên **mọi** loại kết nối ✅. Nhận EOF, lỗi SSL hoặc reset trước `hello` → máy kia từ chối máy này. “Bị từ chối” chỉ tính khi lỗi tới **sau khi bắt tay TLS xong**; lỗi trong lúc TCP/TLS là “không kết nối được” (ngoại lệ duy nhất ✅: lỗi kiểm chứng chỉ server với chứng chỉ đã lưu trên kết nối điều khiển → danh tính không khớp, gạch đầu), vì server đóng ngay các kết nối vượt giới hạn trước cả TLS (mục 2.3), và nếu coi đó là bị từ chối thì một máy làm lũ kết nối sẽ khiến node ngừng tự nối lại (mục 6.2). Trên thực tế client thường chỉ thấy EOF, không thấy loại cảnh báo TLS, nên **không phân biệt được lý do** (bị hủy ghép đôi, ghép đôi một phía…); thông báo cho người dùng phải chung chung (mục 7). Trên kết nối ghép đôi, bị từ chối hiện cho người dùng thành “chưa sẵn sàng ghép đôi” (mục 5.3; mvp mục 8) ✅.

### 2.3. Giới hạn và thời gian chờ

Phần ở tầng mạng đã cài ở `teleport.net.connection` ✅, với luật nhận/từ chối kết nối mới ở `teleport.core.admission` ✅ (các giới hạn ở gạch “Nhận kết nối mới” và giới hạn kết nối ẩn danh là biến của module đó); những chỗ đánh 📝 (kết nối dữ liệu, các luật do node áp) chưa cài.

| | Kết nối ghép đôi | Kết nối điều khiển | Kết nối dữ liệu |
|---|---|---|---|
| Bắt tay TLS + `hello` + message mở đầu | 10 s ✅ | 10 s ✅ | 10 s 📝 |
| Kích thước message tối đa | **16 KiB** (`MAX_PAIRING_MESSAGE_SIZE`) ✅ | **1 MiB** (`MAX_MESSAGE_SIZE`) ✅; `hello` và `control_open` 16 KiB ✅ | 16 KiB cho `hello`/`data_open`; khung khối theo mục 4 📝 |
| Sau khi mở đầu | Bên trả lời chờ `pair_nonce` **5 s** kể từ khi gửi `pair_commit` ✅ (bên dưới); chờ mỗi message khác 10 s; riêng lúc chờ người dùng xác nhận: 60 s, cộng 10 s chờ `pair_confirm` của máy kia (bên dưới) | Keepalive (bên dưới) ✅; một đoạn gửi không xong (`drain()`) trong 30 s → đóng ✅ | Bên nhận file: không nhận được byte nào trong 30 s → đóng. Bên gửi file: `drain()` không xong trong 30 s, hoặc mất kết nối điều khiển với máy đó → đóng 📝 |
| Số kết nối đồng thời | 1 phiên ghép đôi ✅ (`PairingGate`, nối vào node ở `teleport.app.pairing`); yêu cầu bị từ chối nhận `pair_abort{busy}` rồi bị đóng, tổng cộng tối đa 1 s ✅; phiên kết thúc mà không ghép đôi được cũng gửi `pair_abort` (nếu có) rồi đóng trong tổng cộng tối đa 1 s, nên máy kia không đọc cũng không giữ được phiên lâu hơn ✅; kết nối ẩn danh chưa vào phiên: 1 mỗi IP, 8 tổng cộng ✅ (mục 5.3) | 1 cho mỗi thiết bị tin cậy (mục 6.1) ✅ (`teleport.core.links`, `teleport.app.node`) | 1 cho mỗi lượt truyền (file gửi lần lượt); mỗi chiều giữa hai máy chỉ chạy 1 lượt truyền, lượt khác xếp hàng 📝 |

- **Nhận kết nối mới** ✅ (`AdmissionGate`): mỗi kết nối TCP được xét ngay khi nhận, trước mọi bước khác; bị từ chối thì đóng ngay (TCP reset), trước cả bắt tay TLS. Có hai **pool**:
  - **Pool chung** cho IP lạ: kết nối đang mở đầu (từ lúc nhận TCP tới khi xác định loại) tối đa **4 mỗi IP, 64 tổng**; tốc độ bắt tay mới **1/s mỗi IP (burst 2)** và **4/s tổng (burst 4)**.
  - **Pool ưu tiên** cho IP đã chứng minh bằng mTLS: đang mở đầu tối đa **4 mỗi IP, 16 tổng**; tốc độ **2/s mỗi IP (burst 4)** và **4/s tổng (burst 4)**. Một IP được ưu tiên khi có kết nối điều khiển **vào** từ IP đó đã qua kiểm `is_trusted` (lúc nhận `control_open`), hoặc khi node **tự mở** kết nối điều khiển ra tới IP đó thành công (chứng chỉ server đã được kiểm trong bắt tay, mục 2.1); và giữ ưu tiên khi còn kết nối như vậy, hoặc chưa quá **24 h** từ lúc kết nối cuối đóng. Thời gian này đo bằng đồng hồ monotonic, nên trên Linux/macOS thời gian máy ngủ không được tính. Địa chỉ mDNS **không bao giờ** cấp ưu tiên (mDNS không xác thực, mục 8). Hủy ghép đôi thì thiết bị đó thôi giữ ưu tiên cho mọi IP ngay, kể cả khi kết nối của nó còn sống; IP về pool chung ngay nếu không còn thiết bị tin cậy nào khác giữ ưu tiên cho nó (nhiều máy sau một NAT).
  - **Ngân sách lỗi và hạ ưu tiên** ✅: một kết nối đã vào pool ưu tiên mà thôi mở đầu khi **chưa** bắt tay xong bằng chứng chỉ client còn tin cậy (TCP trần, bắt tay hỏng, hết giờ mở đầu, ẩn danh, client bỏ dở) là một **lần lỗi** của IP đó. Lần lỗi được tính **lúc kết nối thôi ở trạng thái mở đầu**: khi đã xác định loại (ví dụ đã gửi `pair_request`), hoặc khi kết thúc trước đó (đóng, reset, hết hạn mở đầu 10 s); không phải lúc nhận. Mỗi IP ưu tiên được tha **4 lần lỗi** (`PRIORITY_STRIKES`; ngân sách đầy khi IP được ưu tiên, hồi **1 lần mỗi 600 s** (`STRIKE_REFILL`), và một kết nối điều khiển của IP đó, vào hay ra, **đóng sau khi đã sống ít nhất 60 s** (`STRIKE_CREDIT_AFTER`) thì cộng lại 1, không vượt 4). Bắt tay tin cậy **không** cộng lại: kẻ relay (gạch dưới) cắt kết nối là thiết bị tin cậy nối lại qua nó sau 1 s (mục 6.2), nên nếu mỗi lần bắt tay đều cộng thì kẻ đó tự nạp lại được mãi. Kết nối điều khiển đã bị bỏ khỏi bảng (hủy ghép đôi, hạ ưu tiên) thì lúc đóng không cộng. Lần lỗi khi ngân sách đã hết thì IP **mất ưu tiên 24 h** (`PRIORITY_TTL`, tính từ lúc tính lần lỗi đó): kết nối mới từ IP đó vào pool chung. Việc hạ **thắng** mọi thứ khác: kết nối điều khiển còn sống từ IP đó không giữ được ưu tiên (bị bỏ khỏi bảng như khi hủy ghép đôi), và kết nối điều khiển mới (vào hay ra) trong 24 h đó không cấp lại; hết 24 h thì chỉ một kết nối điều khiển mới mới cấp lại, với ngân sách lỗi đầy. Kết nối lỗi từ IP đã mất ưu tiên (hủy ghép đôi, hết hạn, đã bị hạ) không tính gì thêm.
    - Lý do hạ: mTLS chứng minh **thiết bị**, không chứng minh **IP**. Máy lạ ở IP Y quảng bá mDNS giả (địa chỉ Y) hoặc tự kết nối tới cổng, rồi chuyển tiếp nguyên byte (relay) tới một thiết bị tin cậy thật: bắt tay qua được, nên Y được ưu tiên; nếu không có luật này, nó dùng ưu tiên đó để làm lũ TCP trần cạn ngân sách tổng của pool ưu tiên (dùng chung mọi IP ưu tiên) và chặn thiết bị tin cậy thật ở IP khác suốt 24 h. Với luật này, Y chỉ giữ được tới 4 chỗ mở đầu của pool ưu tiên (trong 16) khoảng 20 s, tức hai lượt kết nối treo tới hạn mở đầu 10 s (lượt đầu tiêu hết 4 lần lỗi lúc hết hạn, lượt sau bị hạ khi kết nối đầu tiên của nó kết thúc), và vét được ngân sách tổng của pool ưu tiên cỡ hai burst, rồi bị hạ. Với **một** IP, Y chỉ được cộng lại khi một kết nối điều khiển qua relay của nó sống đủ 60 s rồi đóng, tức tối đa khoảng 1 lần lỗi mỗi phút cho mỗi thiết bị tin cậy mà nó đè được bản ghi mDNS. Ngân sách tính theo IP, nên kẻ có **nhiều** IP xoay vòng relay vẫn chặn được pool ưu tiên (giới hạn M3 bên dưới).
    - Lý do tha: thiết bị tin cậy mở `open_control` song song tới mọi địa chỉ của **máy này** và hủy các lần thử thua giữa chừng bắt tay (mục 2.1). Máy này có n địa chỉ thì mỗi lần thiết bị đó nối lại để tới n − 1 lần thử thua kết thúc “không tin cậy” ở máy này, và các lần lỗi đó tính vào **IP nguồn** của thiết bị nối vào; hạ ngay thì chính thiết bị tin cậy sẽ bị hạ oan. Cụ thể: máy này có **2 địa chỉ** thì thiết bị nối vào tốn 1 lần lỗi mỗi lần nối và được cộng lại 1 khi kết nối đó sống ≥ 60 s rồi đóng, nên không bị hạ vì lần thử thua; máy này có **3 địa chỉ** thì nó tốn 2 lần mỗi lần nối khi IP của nó đã được ưu tiên, nên chịu được **2 lần nối liền nhau** (3 nếu lần đầu IP chưa được ưu tiên: lần thử thua của lần đó nằm ở pool chung, không tốn gì), sau đó khoảng 1 lần nối mỗi 10 phút (phần hồi theo thời gian), cộng 1 lần nữa mỗi khi một kết nối sống ≥ 60 s rồi đóng. Bị hạ thì thiết bị chỉ quay về pool chung, như máy chưa từng được ưu tiên.
    - Các trường hợp khác tốn lần lỗi của một IP ưu tiên: node máy kia tắt giữa lúc đang mở kết nối ra tới máy này; ghép đôi lại từ IP ưu tiên (kết nối ghép đôi là ẩn danh); một thiết bị đã bị máy này hủy ghép đôi (một phía) mà còn nối tới từ cùng IP NAT với một thiết bị tin cậy khác (mỗi lần nối của nó bắt tay hỏng). Ngân sách hồi theo đồng hồ monotonic, nên trên Linux/macOS thời gian máy ngủ không làm hồi ngân sách.
    - Bảng IP bị hạ tối đa **1024** IP (`MAX_DEMOTED_IPS`; đầy thì bỏ IP hết hạn sớm nhất), riêng với bảng ưu tiên, không bị hủy ghép đôi hay việc dọn bảng ưu tiên xóa mất.
  - IP ưu tiên chỉ dùng pool ưu tiên (đầy thì không rơi về pool chung), nên máy lạ làm đầy pool chung (bắt tay rác, hay giữ kết nối mở dở) không chặn được thiết bị đã ghép đôi nối lại.
  - Thứ tự xét: chọn pool → số kết nối đang mở đầu (theo IP, rồi tổng) → tốc độ theo IP → tốc độ tổng. Chỉ trừ ngân sách tốc độ khi qua mọi bước, nên một IP đập liên tục không làm cạn ngân sách chung. Bắt tay xong với chứng chỉ client còn tin cậy (context bắt tay có thể đã cũ, nên server kiểm lại `is_trusted` ngay sau bắt tay) thì **được hoàn** phần ngân sách đã trừ: chỉ bắt tay hỏng hoặc ẩn danh mới tiêu ngân sách, nên thiết bị tin cậy mở nhiều kết nối liên tiếp không bị giới hạn tốc độ chặn. Máy không có khóa riêng của một chứng chỉ tin cậy không bắt tay xong, nên không bao giờ được hoàn.
  - Bảng theo IP có trần bộ nhớ: tối đa 4096 IP cho pool chung (chỉ thêm IP khi nhận kết nối của nó; bỏ IP đã nạp đầy ngân sách mà không có kết nối đang mở đầu; đầy thì bỏ IP dùng lâu nhất), 256 IP ưu tiên (đầy thì bỏ IP đã hết ưu tiên, rồi IP không còn kết nối điều khiển sống và có kết nối đóng lâu nhất), 1024 IP bị hạ ưu tiên. Việc **dọn chỗ khi bảng đầy** không bao giờ bỏ IP đang có kết nối mở dở hoặc kết nối điều khiển sống; bảng ưu tiên đầy mà IP nào cũng còn kết nối như vậy thì IP mới không được ưu tiên. Hủy ghép đôi và hạ ưu tiên thì khác: chúng bỏ ưu tiên dù kết nối còn sống (các gạch trên).
  - Kết nối đã xác định loại không còn tính vào số kết nối đang mở đầu.
- Kết nối ẩn danh vượt 1 mỗi IP hoặc 8 tổng (dùng chung cho cả hai pool) bị đóng ngay sau bắt tay TLS, không nhận `hello` ✅. Kết nối ẩn danh thôi tính khi đã gửi `pair_request` (lúc đó node quyết định ngay có nhận phiên hay không, mục 5.3) hoặc khi đóng.
- 📝 **Máy đang ẩn** (công tắc “Cho máy chưa ghép đôi thấy máy này” đang tắt, mvp F10) mà không ở chế độ chờ ghép đôi (mvp F3, mục 5.3): mọi kết nối ẩn danh bị đóng ngay sau bắt tay TLS như ở gạch trên, không gửi `hello`, nên máy chưa ghép đôi không đọc được `name`, `app_version` hay `os` của máy này (chứng chỉ server thì vẫn lộ trong bắt tay, mục 8). Kết nối có chứng chỉ client đã tin cậy không bị ảnh hưởng. Đang ở chế độ chờ ghép đôi thì kết nối ẩn danh đi như thường, để máy kia ghép đôi được bằng IP nhập tay. Cũng không cắt kết nối ẩn danh đến từ một địa chỉ mà node đang có phiên khởi xướng ghép đôi tới, để luật hai bên cùng chủ động (mục 5.3) vẫn chạy.
- Từ chối trong lúc mở đầu luôn là đóng ngay (TCP reset), không đóng gọn, để máy không đọc dữ liệu không giữ được socket.
- Header vượt giới hạn → đóng kết nối ngay, không đọc thân (`read_message(reader, max_size)`, `Connection.receive`) ✅.
- **Giới hạn đã biết** của kết nối điều khiển: thân message được giải mã JSON ngay trên event loop, nên một thiết bị đã ghép đôi gửi liên tục message 1 MiB hợp lệ vẫn làm loop bận. Chấp nhận được: chỉ thiết bị tin cậy mới mở được kết nối điều khiển, và trần 1 MiB giữ mỗi lần giải mã ở mức ngắn.
- **Bắt tay TLS tốn CPU** trên event loop: OpenSSL phân tích chuỗi chứng chỉ client ngay trên loop, với cận mặc định khoảng 100 KiB mỗi chuỗi, và máy lạ không cần ghép đôi vẫn gửi được chuỗi chứng chỉ rác gần cận đó ở mọi lần bắt tay. Số kết nối đang mở đầu một mình không đủ giữ loop nhanh; giới hạn **tốc độ** bắt tay của hai pool (gạch “Nhận kết nối mới”) chặn tổng CPU dành cho bắt tay ✅. Đo đa tiến trình trên `127.0.0.1` (thiết bị tin cậy, các lần đo `ping`/`open_control` và đo RSS ở **tiến trình riêng**, mỗi vai ghim một lõi CPU; kẻ tấn công giữ đủ số kết nối đồng thời tới giới hạn, kết nối nào xong thì mở lại ngay; chuỗi rác khoảng 90 KiB; mỗi ca chạy 3 lần, lấy lần tệ nhất). **T1′** = trước giới hạn tốc độ (commit `f71bb9f`); **T2** = sau giới hạn tốc độ, pool ưu tiên và ngân sách lỗi:

  | Ca (64 kết nối đồng thời, riêng 1 IP là 4) | loop p99, T1′ → T2 | RTT `ping` thiết bị tin cậy, lâu nhất, T1′ → T2 | `open_control` hợp lệ, lâu nhất, T1′ → T2 |
  |---|---|---|---|
  | nền (không lũ) | 7,8 → 13,0 ms | 3,0 → 3,2 ms | 0,02 → 0,01 s |
  | lũ 1 IP, chuỗi rác | 181 → 36 ms | 141 → 73 ms | 0,23 → 0,05 s |
  | lũ 16 IP, chuỗi rác | 142 → 73 ms | 224 → 90 ms | 2,9 → 0,10 s |
  | lũ 16 IP, lá RSA-8192 | 14 → 16 ms | 5 → 127 ms | 0,02 → 0,05 s |

  Với T2, cả 12 lần chạy đều dưới ngưỡng chấp nhận của lần đo này (không phải yêu cầu mvp: loop p99 < 100 ms, RTT < 1 s và không đứt, `open_control` từ IP ưu tiên < 3 s và không thất bại); CPU của máy bị lũ ở hai ca chuỗi rác giảm từ khoảng 21 s xuống 8–10 s trên 22 s lũ, vì kết nối bị từ chối trước `start_tls` rất rẻ. Lần đo T1 trước đó (commit `8220fdb`) chạy thiết bị tin cậy và các lần đo **chung event loop** với bên tấn công, nên số RTT và `open_control` của nó lẫn cả độ nghẽn của chính bên tấn công; T1′/T2 tách tiến trình để loại lỗi này.
  - Biện pháp ✅: giới hạn tốc độ bắt tay mới theo IP **và** một ngân sách chung cho mọi IP, ở `teleport.core.admission`, áp trước `start_tls`; cộng pool ưu tiên cho IP đã xác thực mTLS, có ngân sách lỗi và hạ ưu tiên (gạch “Nhận kết nối mới”). Kết nối điều khiển đang sống không bị giới hạn tốc độ, và node bị lũ vẫn tự mở kết nối ra (mục 6.1); kết nối ra không đi qua giới hạn của chính máy đó.
  - **Giới hạn còn lại:** thiết bị tin cậy ở một IP chưa được ưu tiên (IP mới do DHCP, đã quá 24 h, kết nối đầu tiên ngay sau ghép đôi, hay máy kia có nhiều địa chỉ: máy này nối ra tới một địa chỉ của nó, còn kết nối vào của nó có thể đi từ IP nguồn khác) phải chen pool chung cho tới khi một kết nối điều khiển (vào, hoặc ra từ máy bị lũ) thành công; nếu nó còn đang ẩn, máy bị lũ không thấy nó qua mDNS để tự nối ra. Lũ từ chính một IP ưu tiên (ví dụ IP của thiết bị tin cậy vừa được DHCP cấp cho máy khác trong 24 h, hay máy lạ đã lấy ưu tiên qua relay) chỉ được 4–5 lần lỗi (giữ tới 4 chỗ mở đầu ưu tiên khoảng 20 s), rồi IP đó bị hạ về pool chung (gạch “Ngân sách lỗi và hạ ưu tiên”). Lũ TCP trần (không TLS) vẫn tốn CPU cho việc nhận rồi reset từng kết nối; chống lũ ở mức hệ điều hành nằm ngoài phạm vi. Nhiều máy sau cùng một IP (NAT) dùng chung ngân sách của IP đó.
  - **M1, cùng IP:** giới hạn tính theo IP, nên kẻ tấn công ở **đúng IP** của một thiết bị tin cậy (cùng NAT, cùng máy) vét được ngân sách theo IP đó: `open_control` mới của thiết bị tin cậy ấy bị từ chối, còn kết nối đang sống không đứt. Với luật hạ ưu tiên, IP đó còn bị đưa về pool chung. Đã đo: từ 9/10 tới mọi lần (5/5, 10/10) `open_control` mới thất bại khi có lũ từ cùng IP.
  - **M3, relay xoay vòng nhiều IP:** ngân sách lỗi tính theo IP và IP mới được ưu tiên có ngân sách đầy, nên kẻ lạ trong LAN có nhiều IP, đè được bản ghi mDNS của một thiết bị tin cậy và ép nó nối lại qua từng IP relay mới (mỗi lần khoảng 1–2 s, mục 6.2) giữ được gần kín 16 chỗ mở đầu của pool ưu tiên: mô phỏng ở mức `AdmissionGate` với mặc định thật, cứ 5 s một IP mới thì khoảng 94 % lần nhận của một thiết bị tin cậy ở IP ưu tiên khác bị từ chối (2 s: khoảng 94 %; 10 s: khoảng 15 %). Kết nối đang sống không bị ảnh hưởng. Hướng sửa sau: [ke_hoach.md](ke_hoach.md) mục 8.
  - **M2, hàng đợi accept:** lũ TCP trần rất nhanh có thể làm đầy hàng đợi accept của hệ điều hành trước khi `AdmissionGate` kịp xét; việc này nằm ngoài phạm vi (dựa vào bảo vệ của hệ điều hành, tường lửa). Đã đo: ở đỉnh lũ, từ 1/10 tới gần như mọi lần `open_control` mới (kể cả từ IP ưu tiên) thất bại (`Unreachable`), tùy tốc độ lũ và máy; kết nối đang sống không đứt.
- `read_message` không tự đặt thời gian chờ; tầng gọi nó bọc trong `asyncio.timeout`.
- **Xác nhận ghép đôi** ✅ (`teleport.core.pairing`): mỗi máy tính 60 s từ lúc hiện mã; người dùng máy này phải quyết trong 60 s đó. Máy đã gửi `pair_confirm` mà chưa nhận `pair_confirm` của máy kia thì chờ thêm 10 s (`CONFIRM_GRACE`, tới 70 s kể từ lúc hiện mã), vì người dùng máy kia có thể bấm sát giây thứ 60 và message còn phải đi qua mạng. Hết giờ → `pair_abort{timeout}`; quyết định tới sau hạn chót vẫn là hết giờ.
- **Chờ `pair_nonce`** ✅ (`teleport.core.pairing.NONCE_TIMEOUT`): hạn của bên trả lời ngắn hơn các bước khác vì đây là lúc máy lạ giữ được phiên ghép đôi duy nhất mà chưa làm hiện mã nào (mục 5.3). Hạn này vẫn chịu được khoảng 3 lần mất gói liên tiếp: TCP gửi lại sau ít nhất 200–300 ms rồi nhân đôi (0,3 → 0,6 → 1,2 s), Wi-Fi tiết kiệm điện thêm 100–300 ms. Hết hạn → `pair_abort{timeout}`, tính là hết giờ trong chờ tăng dần theo IP (mục 5.3).
- **Keepalive** trên kết nối điều khiển ✅ (`teleport.core.keepalive`, `teleport.net.connection.ControlChannel`): bên nào không gửi gì trong 10 s (mọi message đều tính, kể cả `pong`) thì gửi `ping`; không nhận được byte nào trong 30 s → coi là mất kết nối (mục 6.2). Tính theo **byte**: đọc được một phần message cũng là “nhận”, nên message lớn đi chậm trên mạng yếu không bị coi là mất kết nối. Nhận `ping` thì bắt buộc trả `pong` mang `t` của `ping` **mới nhất chưa trả**: `ping` dồn lại trong lúc `pong` trước chưa gửi được thì gộp thành một `pong` (mỗi kết nối có tối đa một `pong` đang chờ gửi), nên máy gửi lũ `ping` mà không đọc không làm bên kia phình bộ nhớ. `pong` không cần trả lời.
- **Gửi không tiến triển** trên kết nối điều khiển ✅: message được ghi theo đoạn 64 KiB; một đoạn không gửi xong (`drain()`) trong 30 s → đóng kết nối. Nếu không, máy kia chỉ gửi mà không bao giờ đọc sẽ làm bên gửi treo mãi, vì keepalive vẫn thấy có byte tới.

## 3. Kênh điều khiển

### 3.1. Khung ✅

Mỗi message:

```
┌──────────────────────┬────────────────────────────────┐
│ độ dài: uint32 (BE)  │ JSON object, mã hóa UTF-8      │
└──────────────────────┴────────────────────────────────┘
```

- Độ dài tối đa theo loại kết nối (mục 2.3). Header lớn hơn thì đóng kết nối ngay, không đọc thân.
- Thân phải là một **JSON object**. Mảng, số hay chuỗi ở cấp ngoài cùng đều bị coi là lỗi.
- Thân mã hóa đúng **UTF-8, không BOM**; UTF-16, UTF-32, BOM UTF-8 hay chuỗi byte UTF-8 sai đều là lỗi khung ✅.
- JSON có số nguyên quá dài (hơn 4300 chữ số) hoặc lồng sâu hơn **64 cấp** (object ngoài cùng tính là cấp 1) cũng là lỗi khung.
- Đóng kết nối đúng ở ranh giới giữa hai message là kết thúc bình thường. Đóng giữa chừng một message là lỗi.
- Mọi message có trường `"type"`. Trường không biết thì bỏ qua (để mở rộng về sau); thiếu trường bắt buộc thì là lỗi.

### 3.2. Các loại message

`hello`, các message `pair_*`, `control_open`, `ping` và `pong` đã có kiểu và kiểm tra hợp lệ (`teleport.core.messages`) ✅. `hello`, `control_open`, `ping` và `pong` đã được gửi trên mạng (`teleport.net.connection`) ✅; `pair_*` trên mạng ✅ (`teleport.app.pairing`). Các message còn lại là dự thảo 📝.

| `type` | Hướng | Kết nối | Trường |
|---|---|---|---|
| `hello` | ↔ | mọi loại | `protocol` (int ≥ 1), `device_id`, `name`, `app_version`, `os` |
| `pair_request` | khởi xướng → trả lời | ghép đôi | `cert` (PEM) |
| `pair_commit` | trả lời → khởi xướng | ghép đôi | `commitment` (64 hex) — cam kết cho n_B |
| `pair_nonce` | khởi xướng → trả lời | ghép đôi | `nonce` (64 hex = 32 byte) — n_A; `signature` (128 hex = 64 byte) — chữ ký chứng minh giữ khóa (mục 5.2) |
| `pair_reveal` | trả lời → khởi xướng | ghép đôi | `nonce` (64 hex = 32 byte) — n_B đã cam kết |
| `pair_confirm` | ↔ | ghép đôi | (không có trường) |
| `pair_abort` | ↔ | ghép đôi | `reason` |
| `control_open` | client → server | điều khiển | (không có trường) — mở đầu kết nối điều khiển |
| `data_open` | client → server | dữ liệu | `transfer_id`, `file_index`, `resume_seq` — mở đầu kết nối dữ liệu phục vụ `resume` có `seq` này (mục 4.1) |
| `offer` | gửi → nhận | điều khiển | `transfer_id` (32 hex thường, UUID4), `block_size` (= 1 048 576), `files`: `[{name, size, mtime}]` (1–1000 file) |
| `accept` | nhận → gửi | điều khiển | `transfer_id` (nhận **tất cả** file trong `offer`). Ngay sau đó bên nhận gửi `resume` cho file đầu tiên |
| `reject` | nhận → gửi | điều khiển | `transfer_id`, `reason`: `user` · `no_space` · `busy`; với `no_space` thêm `needed`, `available` (byte) |
| `resume` | nhận → gửi | điều khiển | `transfer_id`, `file_index`, `seq` (số nguyên tăng dần trên mỗi kết nối điều khiển, dùng chung cho mọi lượt truyền, bắt đầu từ 1), `bitfield` (base64) — các khối bên nhận **đã có**; gửi lúc nào cũng được (mục 4.1, 4.4, 6.3) |
| `complete` | gửi → nhận | điều khiển | `transfer_id`, `file_index`, `merkle_root` (64 hex); gửi lại được (mục 6.3) |
| `verified` | nhận → gửi | điều khiển | `transfer_id`, `file_index`; gửi lại được (mục 6.3) |
| `cancel` | ↔ | điều khiển | `transfer_id`, `reason`: `user` · `source_changed` · `unknown_transfer` · `error` |
| `clipboard` | ↔ | điều khiển | `kind`: `text` · `image/png`, `sha256`, `data` (văn bản UTF-8 hoặc base64). Cả văn bản lẫn ảnh đều chia thành nhiều mảnh, mỗi message sau mã hóa ≤ 768 KiB để luôn vừa trần 1 MiB (mục 2.3); cách chia và tổng dung lượng tối đa chốt ở Giai đoạn 5 |
| `ping` / `pong` | ↔ | điều khiển | `t` (số nguyên, mili giây, 0 … 2⁵³ − 1) |

Hai bên gửi `hello` ngay sau khi bắt tay TLS, trên mọi loại kết nối (ngoại lệ: mục 2.3). Khác `protocol` thì đóng kết nối và báo người dùng cập nhật app.

### 3.3. Quy tắc trường ✅

Mọi message đến từ máy khác đều không được tin: sai một trường là cả message bị từ chối.

- **`protocol`**: số nguyên JSON ≥ 1. Không nhận `true`/`false`, không nhận số thực (kể cả `1.0`), không nhận chuỗi.
- **`device_id`**: đúng 32 ký tự hex thường `[0-9a-f]`.
- **`name`** (1–64 code point) và **`app_version`** (1–32 code point):
  - không có khoảng trắng ở đầu hoặc cuối (kể cả khoảng trắng Unicode như NBSP, U+3000);
  - không chứa ký tự loại `Cc` (điều khiển), `Cs` (surrogate lẻ), U+2028, U+2029, U+200B, U+FEFF, U+2060, hay ký tự điều khiển hướng chữ U+200E, U+200F, U+202A–U+202E, U+2066–U+2069;
  - được phép: chữ có dấu, emoji, kể cả emoji ghép bằng ZWJ U+200D. Giá trị giữ nguyên, không chuẩn hóa NFC.
  - `name` do máy kia tự đặt và **chưa được xác thực**: trên kết nối ghép đôi (ẩn danh), máy nào cũng gửi được tên bất kỳ đúng luật trên, kể cả chữ số, dấu `:`, khoảng trắng ở giữa tên hay ký tự trông như khoảng trắng (U+2800, U+3164…). Tên chỉ để hiển thị, không bao giờ dùng để quyết định tin cậy. Lời hỏi ghép đôi hiện tên sau mã, trong ngoặc, có giới hạn độ rộng: [mvp.md](mvp.md) mục 7.2.
- **`os`**: 1–16 ký tự `[a-z0-9_]`. Giá trị chuẩn: `windows`, `macos`, `linux`. Giá trị lạ đúng dạng vẫn được nhận và hiển thị như hệ điều hành không rõ.
- **`cert`**: chuỗi ≤ 8192 ký tự, bắt đầu đúng bằng `-----BEGIN CERTIFICATE-----`. Bên nhận còn phân tích và kiểm tra chứng chỉ (mục 5).
- **`commitment`**, **`nonce`**: đúng 64 ký tự hex thường (32 byte).
- **`signature`** (`pair_nonce`): đúng 128 ký tự hex thường `[0-9a-f]` (64 byte). Thiếu trường thì lỗi như mọi trường bắt buộc. Bên nhận còn kiểm chữ ký (mục 5.1, 5.2).
- **`reason`** (`pair_abort`): 1–32 ký tự `[a-z_]`. Giá trị chuẩn: `user` (người dùng từ chối), `busy` (đang có phiên ghép đôi khác, node không nhận ghép đôi lúc này, hoặc không hiện được mã), `timeout` (hết giờ), `invalid` (message, chứng chỉ, cam kết hoặc chữ ký sai). Giá trị lạ đúng dạng vẫn được nhận, để bản mới thêm lý do được.
- Kiểm tra hex, chữ số, `os` và `reason` chỉ dùng lớp ký tự ASCII trên **toàn bộ** chuỗi: `"user\n"` hay chữ số toàn chiều rộng đều sai.
- `type` là chuỗi nhưng chưa biết thì bên nhận phân biệt được với message hỏng (để bản cũ bỏ qua message mới).
- Message chỉ hợp lệ trên loại kết nối ghi ở bảng mục 3.2; sai loại kết nối là lỗi giao thức ✅ (`teleport.core.messages.allowed_types`).
- Trên kết nối điều khiển ✅: `type` chưa biết thì bỏ qua; khung sai, message sai trường, hoặc message sai loại kết nối → đóng kết nối điều khiển (nối lại theo mục 6.2).
- **`t`** (`ping`/`pong`): số nguyên JSON 0 … 2⁵³ − 1; không nhận `true`/`false` hay số thực (kể cả `1.0`, `1e3`). Mili giây theo đồng hồ của bên gửi `ping` (chỉ để đo độ trễ, không so giữa hai máy). Trả lời `ping`: mục 2.3.
- 📝 **`seq`**, **`resume_seq`**: số nguyên JSON 1 … 2⁵³ − 1; không nhận `true`/`false` hay số thực. Phạm vi của `seq`: mục 4.1.
- 📝 Quy tắc cho message truyền file (chốt khi cài đặt ở Giai đoạn 2): `transfer_id` đúng 32 hex thường; `file_index` < số file trong `offer`; `size` và `mtime` là số nguyên ≥ 0, `size` ≤ 2⁴² (4 TiB; bên gửi chặn file lớn hơn ngay lúc chọn, và `resume` của file lớn nhất có bitfield 512 KiB nên vẫn vừa trần 1 MiB); `name` dài ≤ 255 byte UTF-8, không chứa ký tự loại `Cc`, và qua bộ làm sạch tên file (mục 4.4); `bitfield` dài đúng ⌈số khối / 8⌉ byte, các bit thừa ở cuối bằng 0. Bên gửi tự cắt tên dài hơn (giữ đuôi file); tên có ký tự điều khiển thì bên gửi từ chối và báo lỗi trên máy mình, không gửi gì. Nhờ vậy `offer` 1000 file luôn vừa trần 1 MiB. Bên nhận vẫn kiểm đủ các luật trên, vì dữ liệu từ máy khác không được tin.

## 4. Kênh dữ liệu 📝

### 4.1. Mở kết nối

- Mỗi lượt truyền có tối đa **một kết nối dữ liệu** tại một thời điểm; các file được gửi lần lượt. Kết nối dữ liệu mới cho cùng lượt truyền thay kết nối cũ (kết nối cũ bị đóng).
- Kết nối dữ liệu được mở **theo cùng chiều với kết nối điều khiển hiện tại**: bên đang là client của kết nối điều khiển mở kết nối dữ liệu. Chiều đó đã đi qua được tường lửa, nên một máy chỉ cho phép kết nối ra (ví dụ Windows đang ở hồ sơ mạng Public) vẫn gửi và nhận file được.
- Kết nối dữ liệu chỉ mở tới **đúng địa chỉ (IP và cổng) của kết nối điều khiển hiện tại**, một lần thử mỗi kết nối, không thử song song mọi địa chỉ như `open_control`: lần thử thua bị hủy giữa bắt tay tính là một lần lỗi của IP đó ở bên kia (mục 2.3, “Ngân sách lỗi và hạ ưu tiên”).
- Sau bắt tay TLS và trao `hello`, client gửi `data_open{transfer_id, file_index, resume_seq}`. Từ đó **bên gửi file** (dù là client hay server của kết nối này) ghi các khung khối; bên nhận file không gửi gì trên kết nối này, mọi phản hồi đi trên kết nối điều khiển.
- **Mỗi kết nối dữ liệu phục vụ đúng một `resume`.** Bên nhận là bên duy nhất quyết định cần khối nào: sau `accept` nó gửi `resume` cho file đầu tiên, sau `verified` một file nó gửi `resume` cho file kế tiếp (bitfield toàn 0), và gửi `resume` mỗi khi cần khối gửi lại (mục 4.2, 4.4, 6.3). Mỗi `resume` có `seq` mới; client của kết nối điều khiển mở một kết nối dữ liệu với `data_open{resume_seq = seq}`:
  - client là bên nhận: gửi `resume` rồi mở kết nối dữ liệu ngay;
  - client là bên gửi: mở kết nối dữ liệu khi nhận được `resume`.
  - `resume` có đủ mọi bit thì **không** mở kết nối dữ liệu: nó chỉ để kéo `complete` về (mục 6.3).
  - Bên gửi gửi xong các khối được yêu cầu thì đóng gọn kết nối dữ liệu.
- **Phạm vi của `seq` là một kết nối điều khiển:** bên nhận đánh `seq` lại từ 1 trên mỗi kết nối điều khiển mới, và bên gửi quên mọi `seq` cũ khi kết nối điều khiển đổi. Vì vậy `seq` không phải lưu xuống đĩa, và bên nhận vừa khởi động lại không bao giờ bị coi là đang gửi `seq` cũ.
- Hai kết nối không có thứ tự với nhau, nên `data_open` có thể tới trước `resume` tương ứng. Bên gửi chỉ gửi khối sau khi đã có, trên kết nối điều khiển hiện tại, `resume` với đúng `seq` đó và đúng `transfer_id`/`file_index` (chờ tối đa 10 s, quá thì đóng kết nối dữ liệu). `resume_seq` nhỏ hơn `seq` **lớn nhất** mà bên gửi đã nhận trên kết nối điều khiển hiện tại → kết nối cũ, đóng. Không nhét bitfield vào `data_open`, vì file 100 GiB có bitfield 12,5 KiB, mã hóa base64 thành khoảng 16,7 KiB, vượt giới hạn 16 KiB.
- Không mở được kết nối dữ liệu trong 10 s trong khi kết nối điều khiển vẫn sống → thử lại theo lịch ở mục 6.2; sau 3 lần thất bại liên tiếp thì báo lỗi tường lửa (mvp mục 8) và giữ lượt truyền ở trạng thái “Đang nối lại…”.

### 4.2. Khung khối

```
┌──────────────┬────────────────────┬──────────────────┬───────────────────┬──────────────┐
│ type: uint8  │ block_index: uint32│ length: uint32   │ sha256: 32 byte   │ payload      │
│ 0x02 = khối  │ (BE)               │ (BE)             │ SHA-256(payload)  │ dữ liệu thô  │
└──────────────┴────────────────────┴──────────────────┴───────────────────┴──────────────┘
```

Đóng gói bằng `struct.pack("!BII32s", ...)` (41 byte header).

- Kích thước khối cố định **1 MiB** (`block_size` trong `offer`). Khối `i` nằm ở vị trí `i × block_size` trong file. File `size` byte có `⌈size / block_size⌉` khối; file rỗng có 0 khối.
- `length` phải **đúng bằng** độ dài của khối đó suy ra từ `size` (mọi khối 1 MiB, khối cuối có thể ngắn hơn). `type` lạ, `block_index` ngoài phạm vi hay `length` sai → đóng kết nối dữ liệu.
- Bên gửi đọc khối, băm, rồi gửi hash **trong header của chính khối đó**: chỉ giữ một khối trong RAM, không phải đọc file hai lần, và hash không bao giờ tới sau dữ liệu. Bên gửi giữ hash các khối đã gửi trong RAM (32 byte mỗi khối, 320 KiB cho file 10 GiB) để tính Merkle root.
- Giới hạn tốc độ gửi được áp theo từng đoạn 64 KiB của payload, không phải theo cả khối, để dòng byte không ngưng quá lâu (thời gian chờ 30 s ở mục 2.3).
- Bên nhận băm lại payload và so với `sha256` trong header ngay khi nhận. Không khớp → bỏ khối, đóng kết nối dữ liệu, gửi `resume` (khối đó vẫn có bit = 0 nên được gửi lại). Khớp → giữ hash để ghi vào trạng thái nối lại ở lần đánh dấu kế tiếp (mục 4.3) và chuyển khối cho luồng ghi đĩa.
- Bên gửi gửi các khối còn thiếu theo thứ tự tăng dần.

### 4.3. Ghi đĩa và trạng thái nối lại

- Luồng ghi đĩa nhận khối qua hàng đợi có giới hạn **8 khối**. Hàng đợi đầy → bên nhận ngừng đọc socket → TCP làm bên gửi chậm lại (backpressure).
- **Trạng thái nối lại** của mỗi file là một **nhật ký chỉ ghi thêm** (`.state`): không bao giờ ghi lại toàn bộ trạng thái, chỉ nối thêm bản ghi vào cuối. Bitfield và hash các khối được dựng lại bằng cách đọc nhật ký từ đầu. Manifest chỉ nằm ở `transfer.json`; header của nhật ký chỉ để đối chiếu. Định dạng byte, cách đọc lại và gộp nhật ký: [architecture.md](architecture.md) mục 4.
- **Mỗi 16 khối** (và khi đã nhận đủ khối của file): `fsync` file `.part` → nối một bản ghi đánh dấu các khối đó, kèm hash của từng khối (từ header khung), vào `.state` → `fsync` `.state`. Một khối chỉ được tính là đã có (bit bật) khi đã khớp hash, đã nằm bền trong `.part`, **và** bản ghi đánh dấu nó đã nằm bền trong `.state`. Mất điện ở bước nào thì trạng thái cũng không đánh dấu khối chưa có trên đĩa: bản ghi ghi dở bị bỏ khi đọc lại, nên mất tối đa một lần đánh dấu (≤ 16 khối), và các khối đó đã nằm trong giới hạn gửi lại bên dưới.
- Bản ghi đánh dấu chỉ được nối sau một lần `fsync` `.part` **thành công** bao gồm các khối đó. Xử lý khi `fsync` hay ghi lỗi: [architecture.md](architecture.md) mục 4.
- Trong lúc `fsync`, luồng ghi tạm dừng, nên số khối đã nhận mà chưa được đánh dấu luôn ≤ 16 + 8 + 1 (khối đang đọc).
- **Giới hạn gửi lại (mvp N4).** “Phần chưa nhận” tính theo bitfield của bên nhận lúc mất kết nối. Phần gửi thừa gồm các khối đã nhận mà chưa đánh dấu (≤ 25 MiB) cộng dữ liệu còn trong bộ đệm socket hai bên. Bên nhận đặt `SO_RCVBUF` ≤ 8 MiB và bên gửi đặt `SO_SNDBUF` ≤ 8 MiB, nên tổng ≤ 41 MiB, dưới ngưỡng của mvp N4.
- `.state` hỏng ngay từ header, hoặc header không khớp manifest trong `transfer.json` → bên nhận coi như chưa có khối nào và gửi `resume` với bitfield toàn 0 ([architecture.md](architecture.md) mục 4).

### 4.4. Hoàn tất và kiểm tra toàn vẹn

1. Bên gửi gửi xong mọi khối của file ít nhất một lần thì kiểm lại `size` và `mtime` của file nguồn; khác manifest → `cancel{source_changed}`. Không đổi → gửi `complete{merkle_root}` trên kết nối điều khiển. Bên gửi gửi lại `complete` mỗi khi nhận `resume` có đủ mọi bit của file đó (mục 6.3).
2. Bên nhận chờ tới khi **có `complete` và mọi bit đều bật**. Hai kết nối không có thứ tự với nhau, nên `complete` có thể tới trước khối cuối. Kết nối dữ liệu đóng mà file còn khối có bit = 0 → bên nhận gửi `resume`, bên gửi gửi tiếp các khối đó (mục 4.1).
3. **Lượt đọc lại:** bên nhận đọc lại toàn bộ `.part` **từ đĩa**, băm từng khối và so với hash đã lưu; `.part` dài hơn `size` (bị nối thêm) thì cắt về đúng `size` rồi `fsync`. Bước này bắt được mọi thay đổi trên `.part` sau khi khối được nhận (ví dụ bị sửa trong lúc tạm dừng, mvp F7). Khối sai → xóa bit (nối bản ghi xóa đánh dấu các khối đó vào `.state`), gửi `resume` với bitfield mới; bên gửi gửi lại đúng các khối đó; quay lại bước 2.
4. Mọi khối khớp → tính Merkle root từ danh sách hash; khác `merkle_root` của bên gửi (trạng thái nối lại bị hỏng) → xóa toàn bộ bitfield (nối bản ghi xóa mọi đánh dấu vào `.state`) và gửi `resume` với bitfield toàn 0 để nhận lại cả file.
5. Một file qua bước 3 hoặc 4 mà vẫn sai **quá 3 lần** (ví dụ ổ đĩa hỏng) → `cancel{error}`, báo người dùng.
6. Khớp → **đổi tên không ghi đè** sang tên thật → ghi “file đã xong” vào trạng thái mức lượt truyền (mục 6.3) → xóa trạng thái của file → gửi `verified`. Thứ tự đầy đủ, kể cả các lần `fsync` file và thư mục: [architecture.md](architecture.md) mục 4.
   - Đổi tên không ghi đè chạy được trên mọi hệ file, kể cả FAT/exFAT và ổ mạng. Chọn tên đích (đã có thì thử ` (1)`, ` (2)`…), **nối bản ghi ý định mang tên đó vào `.state` và `fsync`** rồi mới tạo file đích rỗng bằng `O_CREAT | O_EXCL` để giữ chỗ; tạo được thì **nối bản ghi đã giữ chỗ cho tên đó và `fsync`**; sau đó `os.replace` file tạm đè lên **chính file giữ chỗ đó**.
   - `os.replace` gặp `PermissionError` (Windows: antivirus hay trình lập chỉ mục đang mở file giữ chỗ) → thử lại tối đa 5 lần, cách nhau 50 ms.
   - Bị ngắt giữa chừng → lần sau, bên nhận chỉ dùng lại một file mang tên đích khi `.state` có bản ghi đã giữ chỗ cho đúng tên đó; file nào `.state` không chứng minh được là của mình thì không bao giờ bị xóa hay ghi đè. Bảng phục hồi: [architecture.md](architecture.md) mục 4.
   - Không dùng `os.rename` trần trên POSIX, vì nó âm thầm ghi đè file đích. Cũng không dùng `os.link`, vì FAT/exFAT không hỗ trợ.

**Merkle root:** lá là hash SHA-256 của từng khối theo thứ tự; nút cha = SHA-256(con trái ‖ con phải); tầng có số nút lẻ thì nút cuối được đẩy thẳng lên tầng trên; root là nút còn lại cuối cùng. File rỗng (0 khối): root = SHA-256 của chuỗi rỗng.

**Tên file nhận được** được làm sạch trước khi dùng (chi tiết: [ke_hoach.md](ke_hoach.md) mục 3.3). Hai file trùng tên trong cùng `offer`, hoặc trùng với file đã có (không phân biệt hoa thường), đều được đặt tên ` (1)`, ` (2)`…

## 5. Ghép đôi

Ghép đôi theo kiểu *numeric comparison* của Bluetooth: hai bên trao nonce ngẫu nhiên, bên **trả lời cam kết** nonce của mình trước khi thấy nonce bên kia, rồi cả hai hiện cùng một mã 6 số cho người dùng so. Nếu mã chỉ tính từ hai fingerprint, kẻ đứng giữa có thể sinh khóa offline tới khi ra mã trùng; với nonce cam kết, mỗi lần thử chỉ thành công với xác suất 10⁻⁶, và mỗi lần thử đều làm một máy hiện mã.

Vì sao bên trả lời cam kết: nó là máy đang chờ ghép đôi, ai trong LAN cũng gọi tới được. Ở bản dự thảo cũ bên khởi xướng cam kết, nên kẻ đứng giữa đóng vai bên khởi xướng nhận được n_B trước khi phải lộ nonce của mình và tính trước được mã mà bên trả lời sắp hiện. Mã không như ý thì nó bỏ phiên và thử lại; bên trả lời không hiện mã nên người dùng không thấy gì (chỉ còn chờ tăng dần ở mục 5.3 làm chậm). Nay bên khởi xướng chỉ lộ n_A sau khi đã nhận cam kết, còn bên trả lời hiện mã trước rồi mới lộ n_B, nên không bên nào biết mã trước khi đã bị ràng buộc vào nonce của mình.

Công thức đã cài đặt ở `teleport.core.pairing_code` ✅; máy trạng thái ghép đôi và chống làm phiền ở `teleport.core.pairing` ✅ (thứ tự message, hết giờ, lý do hủy, mục 5.1 và 5.3); trao đổi trên mạng ✅ (`teleport.app.pairing`).

### 5.1. Luồng

A là bên khởi xướng, B là bên trả lời.

```
A (khởi xướng)                              B (trả lời)
TLS tới cổng của B: A không gửi chứng chỉ (kết nối ghép đôi)
hello ───────────────────────────────────►
      ◄─────────────────────────────────── hello
pair_request{cert}               ────────►  B: kiểm cert, CN khớp hello.device_id của A
      ◄─────────────────────────────────── pair_commit{commitment = C_B}  (hoặc pair_abort{busy})
pair_nonce{nonce = n_A,          ────────►  B: kiểm chữ ký bằng khóa trong cert của A;
           signature}                       sai → pair_abort{invalid}, không hiện mã.
                                            Đúng → tính mã, HIỆN MÃ, rồi mới gửi n_B
      ◄─────────────────────────────────── pair_reveal{nonce = n_B}
A: kiểm C_B bằng fp_B (từ bắt tay TLS); sai → pair_abort{invalid}, không hiện mã.
   Đúng → tính mã và hiện mã.
Cả hai hỏi người dùng (hết giờ 60 s → pair_abort{timeout})
pair_confirm ◄──────────────────────────► pair_confirm   (từ chối → pair_abort{user})
Mỗi bên: đã GỬI pair_confirm VÀ đã NHẬN pair_confirm của bên kia → lưu trusted.json, đóng.
```

Năm bất biến ✅ (`teleport.core.pairing`):

1. A chỉ ký và gửi n_A (`pair_nonce`) sau khi đã nhận `pair_commit`.
2. B chỉ gửi n_B (`pair_reveal`) sau khi đã nhận n_A, và mã đã hiện trong cùng bước đó: n_B không bao giờ rời B khi B chưa hiện mã. Không hiện được mã (giao diện lỗi) → B gửi `pair_abort{busy}`, không gửi n_B; A không hiện được mã → `pair_abort{user}` (trừ khi phiên đã hết giờ: khi đó là `timeout` và tính như hết giờ).
3. A chỉ hiện mã khi cam kết của B đúng (kiểm bằng fingerprint của B lấy từ bắt tay TLS và n_B).
4. A không bao giờ tự thử lại một phiên thất bại: mỗi phiên khởi xướng là một thao tác của người dùng. Phiên khởi xướng bị nhường (hai bên cùng chủ động, mục 5.3) thì máy đó chuyển sang vai trả lời, và vai này vẫn cam kết trước.
5. B chỉ hiện mã (và chỉ gửi `pair_reveal`) sau khi chữ ký trong `pair_nonce` đã hợp lệ. Chữ ký sai → `pair_abort{invalid}`, tính là thất bại trong chờ tăng dần theo IP (mục 5.3), như mọi message sai.

- n_A, n_B: mỗi nonce 32 byte ngẫu nhiên, sinh mới cho mỗi lần ghép đôi.
- Chứng chỉ của B: A lấy từ bắt tay TLS (đã được TLS xác thực là B giữ khóa riêng). A kiểm CN chứng chỉ đó khớp `hello.device_id` của B. Lập luận “TLS đã xác thực B” dựa vào các điều kiện **phía client (A)**: TLS 1.3 tối thiểu (`teleport.net.tls`); A không bao giờ dùng lại session (không truyền `session=`, nên không có PSK/resumption bỏ qua bước ký của server); fp_B lấy từ `getpeercert(binary_form=True)` của **chính kết nối đó** (`teleport.net.connection`); OpenSSL ≥ 1.1.1 vẫn kiểm `CertificateVerify` của server khi client dùng `CERT_NONE`. (`num_tickets = 0` ở mục 2.2 là cấu hình server, không phải điều kiện ở đây.)
- **A phát hiện lỗi trước khi gửi `pair_request`** (chứng chỉ của B không hợp lệ, CN không khớp `hello`, tự ghép với chính mình, trùng danh tính bên dưới): A chỉ đóng kết nối, không gửi gì. B thấy EOF trước message mở đầu thì không tạo phiên và không tính vào thời gian chờ của IP đó (mục 5.3).
- Chứng chỉ của A: B lấy từ `pair_request.cert`. Chứng chỉ không bí mật (ai bắt tay TLS với một máy cũng lấy được chứng chỉ của máy đó), nên B không tin nó chỉ vì nó đúng dạng: A **chứng minh giữ khóa riêng** bằng cách ký payload ở mục 5.2 bằng khóa danh tính và gửi chữ ký trong `pair_nonce`. Payload gắn nhãn riêng, fingerprint của cả hai bên, n_A và cam kết C_B mà B vừa gửi trong **phiên này**, nên chữ ký không dùng lại được ở phiên khác hay với máy khác. Chỉ A ký, chỉ B kiểm (B đã được TLS chứng minh, gạch trên). B kiểm chữ ký bằng khóa công khai trong `pair_request.cert` **trước khi hiện mã** (bất biến 5). Vì vậy một máy lạ M gửi chứng chỉ công khai của một máy V khác mà không có khóa của V thì bị dừng ở `pair_nonce`: B không hiện mã, không lưu gì, và IP của M bị tính chờ tăng dần (mục 5.3). Về sau, mTLS buộc A chứng minh giữ khóa riêng ở mỗi kết nối.
- Rủi ro còn lại: lập luận ở hai gạch trên dựa vào các điều kiện phía client đã nêu; vào việc B chỉ chạy đúng một phiên tại một thời điểm và sinh n_B mới (nên C_B mới) cho mỗi phiên; còn lộ khóa riêng của một máy nằm ngoài mô hình đe dọa. Không dùng channel binding của TLS: `tls-exporter` (RFC 9266) không có trong `ssl` của Python 3.12–3.14, còn `tls-unique` không định nghĩa cho TLS 1.3; cũng không cần, vì payload đã gắn fp_B và C_B mới mỗi phiên.
- Chứng chỉ nhận được phải: phân tích được; ≤ 8192 ký tự PEM, chỉ ASCII, đúng một khối `CERTIFICATE`; X.509 v3; subject có đúng một CN dạng `teleport-<32 hex thường>`; issuer trùng subject và chữ ký tự ký hợp lệ; khóa ECDSA P-256; chữ ký ECDSA-SHA256; không có dấu hiệu chứng chỉ CA: từ chối `BasicConstraints CA=true`, `KeyUsage` có `keyCertSign` hoặc `cRLSign`, và mọi chứng chỉ có extension Netscape Cert Type (OID `2.16.840.1.113730.1.1`). Lý do: chứng chỉ tin cậy được nạp làm trust anchor cho mTLS, nên không được phép ký ra chứng chỉ khác. Ngày hiệu lực không được kiểm khi ghép đôi (chứng chỉ của Teleport không hết hạn, mục 1). Mọi lỗi khi phân tích hay kiểm chứng chỉ, kể cả kiểu dữ liệu mà thư viện không hỗ trợ (ví dụ SAN kiểu x400Address hay EDIPartyName), đều tính là chứng chỉ không hợp lệ: B gửi `pair_abort{invalid}` (tính là thất bại trong chờ tăng dần theo IP, mục 5.3); A chỉ đóng kết nối (gạch “A phát hiện lỗi…” ở trên) và báo danh tính không hợp lệ (mvp mục 8).
- **Không ghép đôi đè danh tính:** `device_id` của bên kia đã có trong `trusted.json` với fingerprint **khác** → mỗi bên tự hủy phiên trước khi hiện mã (B gửi `pair_abort{invalid}`; A chỉ đóng, như ở trên), và báo người dùng (mục 7). Muốn ghép đôi lại thì phải hủy ghép đôi mục cũ trước. Cùng `device_id` và cùng fingerprint (ghép đôi lại sau ghép đôi một phía) thì được, và lưu theo mục 5.5.
- Message sai thứ tự, message không phải `pair_*` (kể cả `hello` thứ hai; message có `type` chưa biết cũng không được bỏ qua trên kết nối ghép đôi), `pair_confirm` nhận lần hai, hay message lỗi → `pair_abort{invalid}` và đóng. Nhận `pair_abort` thì đóng, không trả lời.
- **Hiện mã:** `hello.name` do máy kia tự đặt và chưa được xác thực (mục 3.3), nên kẻ đứng giữa đặt được tên trông như một mã. Vì vậy lời hỏi hiện mã trước mọi chữ do máy kia đặt, theo bố cục ở [mvp.md](mvp.md) mục 7.2.
- **“Đã gửi” `pair_confirm`** nghĩa là đã ghi xong message vào kết nối. Gửi lỗi (mất kết nối, quá hạn gửi) thì máy đó không lưu gì, kể cả khi đã nhận `pair_confirm` của máy kia, và phiên kết thúc là mất kết nối (với bên trả lời, tính là thất bại trong chờ tăng dần, mục 5.3).
- Rớt mạng giữa hai `pair_confirm` có thể để lại ghép đôi một phía. Phía còn tin sẽ bị từ chối ở lần kết nối sau (mục 7); ghép đôi lại sẽ sửa.

### 5.2. Công thức

`‖` là nối byte. Fingerprint (64 ký tự hex) được đổi sang 32 byte trước khi băm. Thứ tự cố định theo vai trò (khởi xướng trước), **không** sắp xếp.

```
commitment = hex(SHA-256(b"teleport-commit-v1" ‖ fp_trả_lời ‖ n_B))
digest     = SHA-256(b"teleport-pair-v1" ‖ fp_khởi_xướng ‖ fp_trả_lời ‖ n_A ‖ n_B)
mã         = int.from_bytes(digest[0:4], "big") mod 1 000 000, viết đủ 6 chữ số (có số 0 đầu)
payload    = b"teleport-initiator-proof-v1" ‖ fp_khởi_xướng ‖ fp_trả_lời ‖ n_A ‖ C_B   (155 byte)
signature  = hex(ECDSA-P256-SHA256(khóa danh tính của bên khởi xướng, payload)), dạng r ‖ s
```

Mã hiển thị thành hai nhóm 3 chữ số, ví dụ `048 213`. Hai fingerprint giống nhau (một máy tự ghép với chính nó) thì không tính mã.

Chữ ký chứng minh giữ khóa (`pair_nonce.signature`, mục 5.1) ✅ (`teleport.core.pairing_code.proof_payload`, `teleport.trust.identity`):

- C_B là `commitment` trong `pair_commit` của phiên này, đổi từ hex sang 32 byte. Bên khởi xướng lấy fp_trả_lời từ bắt tay TLS của chính kết nối này; bên trả lời dùng fingerprint của chính nó và fingerprint của `pair_request.cert`.
- ECDSA trên đường cong P-256 với SHA-256, bằng khóa danh tính (mục 1). Chữ ký mã hóa **r ‖ s, mỗi số 32 byte big-endian** (IEEE P1363), tổng 64 byte, trong JSON là 128 ký tự hex thường. Không dùng DER.
- Bên kiểm: độ dài khác 64 byte, r hoặc s ngoài 1 … n − 1 (n là bậc của nhóm P-256), hay chữ ký không khớp → chữ ký sai. Không chặn chữ ký có s lớn (high-s): chữ ký không dùng làm định danh, nên việc một chữ ký có hai dạng hợp lệ không gây hại.

### 5.3. Chống làm phiền

Kết nối ghép đôi không cần chứng chỉ, nên bất kỳ máy nào trong mạng cũng mở được. Bên trả lời:

- Chỉ chạy **một phiên ghép đôi** tại một thời điểm; yêu cầu khác trong lúc đó nhận `pair_abort{busy}`. Kết nối ẩn danh đã bắt tay xong mà chưa gửi `pair_request`: tối đa 1 mỗi IP và 8 tổng cộng; quá thì đóng ngay sau bắt tay, không gửi `hello` (mục 2.3). Trong lúc bắt tay, kết nối thuộc giới hạn kết nối đang mở đầu **và giới hạn tốc độ bắt tay** của pool mà IP đó thuộc về (thường là pool chung; IP ưu tiên thì dùng pool ưu tiên và tốn một lần lỗi, mục 2.3) ✅; bắt tay ẩn danh không được hoàn ngân sách. Vì vậy khi cổng đang bị lũ bắt tay từ nhiều IP, ghép đôi có thể thất bại: bên khởi xướng bị reset trước TLS và thấy câu “không kết nối được” hiện có (mvp mục 8), rồi thử lại sau. Thời gian chờ ở mục 2.3.
- **Chờ tăng dần theo từng địa chỉ IP:** một phiên từ một IP kết thúc mà không ghép đôi được (người dùng từ chối, hết giờ, `invalid`, trùng danh tính, mất kết nối, bên kia tự hủy) → IP đó nhận `pair_abort{busy}` trong 10 s, rồi 20 s, 40 s… tối đa 10 phút; ghép đôi thành công thì xóa đếm. IP khác không bị ảnh hưởng. Chi tiết ✅ (`teleport.core.pairing.PairingGate`):
  - Chỉ tính phiên mà máy này là **bên trả lời**. Phiên do node tự hủy (tắt node, Ctrl+C, nhường khi hai bên cùng chủ động, không hiện được mã) không tính (hủy sau khi đã hết giờ thì tính là hết giờ).
  - `pair_abort{busy}` gửi trong lúc IP đang bị chờ **không** kéo dài thời gian chờ.
  - IP yên lặng 10 phút kể từ khi hết thời gian chờ thì được quên (đếm lại từ đầu). Theo dõi tối đa 1024 IP; quá thì quên IP có thời gian chờ hết sớm nhất. Bộ đếm chỉ nằm trong bộ nhớ, mất khi khởi động lại node.
- **Giới hạn số mã hiện ra** ✅ (`PairingGate.code_shown`): bên trả lời đếm số mã đã hiện trong một cửa sổ trượt, gộp mọi IP (đổi IP không lách được). Đủ số mã tối đa thì mọi yêu cầu mới nhận `pair_abort{busy}` tới khi mã cũ nhất ra khỏi cửa sổ; việc này không ghi gì vào bộ đếm theo IP. Đếm cả mã mà giao diện không hiện được. Luật hai bên cùng chủ động (gạch dưới) xét trước nên không bị chặn, nhưng mã của nó vẫn được đếm. Từ một số mã nhất định trong cửa sổ, giao diện cảnh báo người dùng có thể có người đang thử giả mạo. Con số (cửa sổ, số mã tối đa, ngưỡng cảnh báo) ở mvp F3. Bộ đếm chỉ nằm trong bộ nhớ. Đổi lại, trong lúc kẻ tấn công còn làm đầy giới hạn thì yêu cầu thật cũng nhận `busy`. Đổi được IP thì kẻ đó kéo dài được việc này, nhưng chờ tăng dần theo IP (gạch trên) khiến về lâu dài mỗi IP chỉ còn làm hiện khoảng một mã mỗi 10 phút, nên muốn chặn liên tục phải dùng nhiều IP. Rủi ro này không xấu hơn việc một máy giữ phiên duy nhất cỡ 70 s (gạch “Giới hạn của các biện pháp trên”): chỉ gây phiền, không ghép đôi được.
- **Hai bên cùng chủ động ghép đôi với nhau** (máy này đang có phiên khởi xướng tới **một trong các địa chỉ** máy này đang thử cho máy đó, tức các địa chỉ mDNS quảng bá hoặc IP nhập tay, và yêu cầu tới từ chính địa chỉ đó): bên có `device_id` **lớn hơn** hủy phiên khởi xướng của mình và trả lời phiên của bên kia; bên nhỏ hơn trả `pair_abort{busy}`. Luật này xét trước việc node có đang nhận ghép đôi không (gạch cuối) và trước thời gian chờ của IP. Phiên khởi xướng bị nhường gửi `pair_abort{busy}` nếu đã gửi `pair_request`, chưa gửi thì chỉ đóng. `device_id` trên kết nối ẩn danh chưa được xác thực, nên cần IP khớp; kẻ giả mạo vẫn có thể làm phiên tự hủy (chỉ gây phiền, không ghép đôi được). Bên nhường trả kết quả của phiên trả lời thay thế cho người dùng (lệnh `teleport pair` vẫn có đúng một kết quả). Giới hạn: máy kia gửi yêu cầu từ một địa chỉ không nằm trong danh sách đó (ví dụ địa chỉ của card ảo bị lọc khỏi bản quảng bá) thì luật này không áp được, và hai bên cùng nhận `busy` rồi thử lại.
- **Giới hạn của các biện pháp trên:** đây là chống làm phiền ở mức tốt nhất có thể, không chống được tấn công từ chối dịch vụ trong LAN. Một máy đổi IP liên tục (IPv6 tạm thời, xin DHCP mới) vẫn có thể làm hiện hộp thoại nhiều lần (tới giới hạn số mã hiện ra) hoặc giữ phiên ghép đôi. Kẻ đứng giữa làm vậy cũng thử được nhiều mã trong một lần ghép đôi: mỗi phiên trả lời chỉ trúng với xác suất 10⁻⁶ (mục 5.1), và giới hạn số mã hiện ra chặn số phiên đó. Kẻ đứng giữa biết mã của A ngay khi nhận n_A, rồi A còn chờ tới khoảng 80 s (10 s chờ `pair_reveal` + 60 s chờ người dùng + 10 s chờ `pair_confirm` của máy kia, mục 2.3), trải trên tối đa hai cửa sổ đếm mã; nên mỗi lần A chờ xác nhận, kẻ đó thử được tối đa gấp đôi giới hạn ở mvp F3, tức xác suất trúng cỡ 10·10⁻⁶. Mỗi phiên đều làm máy này hiện một mã mới, và từ ngưỡng cảnh báo (mvp F3) máy này cảnh báo người dùng, nên người dùng thấy được dấu hiệu bất thường. Một máy ẩn danh gửi `pair_request` rồi không gửi `pair_nonce` chỉ giữ được phiên duy nhất tới hạn chờ `pair_nonce` cộng thời gian đóng (mục 2.3), và mỗi lần như vậy làm IP đó dính chờ tăng dần (gạch trên), nên muốn chặn liên tục phải đổi IP liên tục. Đi tiếp tới lúc hiện mã thì giữ được lâu hơn (chờ người dùng xác nhận, mục 2.3; gửi và đọc thật chậm thì cỡ 70 s hoặc hơn chút), nhưng mỗi lần như vậy bị đếm vào giới hạn số mã hiện ra và người dùng thấy cảnh báo (gạch trên). Trong lúc phiên bị giữ, mọi yêu cầu khác nhận `pair_abort{busy}`. Không có giới hạn chung (gộp mọi IP) cho phiên chưa hiện mã: kẻ tấn công đổi IP đốt hết hạn mức đó dễ hơn người dùng thật, nên người bị từ chối lại là người dùng thật; phiên chưa hiện mã cũng không cho đoán mã, nên giới hạn đó không thêm an toàn. Việc này chỉ ảnh hưởng khi node đang nhận yêu cầu ghép đôi (gạch cuối; với CLI là trong lúc `teleport listen` đang chạy). Ghép đôi vẫn an toàn vì cần người dùng xác nhận mã ở cả hai máy.
- Khi nào node nhận yêu cầu ghép đôi: giao diện nhận theo mvp F3 📝 (máy đang hiện: luôn nhận; máy đang ẩn: chỉ khi màn hình “Chờ ghép đôi” đang mở); CLI chỉ nhận khi đang chạy `teleport listen` ✅. Ngoài các trường hợp đó → `pair_abort{busy}`; riêng máy đang ẩn thì kết nối ẩn danh bị cắt ngay sau bắt tay và không tới được `pair_abort`, trừ khi luật hai bên cùng chủ động áp dụng (mục 2.3). Bên khởi xướng hiện cả `busy` lẫn bị từ chối trước `hello` (mục 2.2) thành cùng một câu “chưa sẵn sàng ghép đôi” (mvp mục 8) ✅. Đóng màn hình “Chờ ghép đôi” hay chuyển sang ẩn không hủy phiên đã nhận (kể cả phiên chưa hiện mã), chỉ từ chối yêu cầu mới (mvp F3) 📝.

### 5.4. Test vector

| Đại lượng | Giá trị (hex) |
|---|---|
| `fp_khởi_xướng` | `5ee5afc087437e85ae8430c657e73fe6fd7c87fe5064b77eb6537a133ee3b290` |
| `fp_trả_lời` | `2846c2e746ec2e9bb0dbcfad4873a0e81d82f256f89fb6b0e2bca91890b498f3` |
| `n_A` | `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` |
| `n_B` | `202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f` |
| `commitment` (cam kết của bên trả lời cho n_B) | `a3fd58883a0ae9e63ebc321d164eeaf743173032ba03c44aefc333753e314e69` |
| `digest` | `4a58a291a77f5de3163a28b02a8ca6c465aac3805851f1f3f2477c87232d3e64` |
| **mã** | `322769` (hiển thị `322 769`) |
| `payload` (chứng minh giữ khóa, 155 byte; C_B = `commitment` ở trên) | `74656c65706f72742d696e69746961746f722d70726f6f662d76315ee5afc087437e85ae8430c657e73fe6fd7c87fe5064b77eb6537a133ee3b2902846c2e746ec2e9bb0dbcfad4873a0e81d82f256f89fb6b0e2bca91890b498f3000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1fa3fd58883a0ae9e63ebc321d164eeaf743173032ba03c44aefc333753e314e69` |

Hai fingerprint ở trên là SHA-256 của chuỗi ASCII `teleport test vector A` và `teleport test vector B`. Đổi vai trò (đảo hai fingerprint) cho mã `488828`; đảo hai nonce cho mã `682735`.

ECDSA dùng số ngẫu nhiên k, nên chữ ký không có test vector cố định; ví dụ dưới chỉ để test bên **kiểm** chữ ký. Khóa test không gắn với hai fingerprint ở trên (chúng không phải fingerprint của chứng chỉ thật):

| Đại lượng | Giá trị (hex) |
|---|---|
| `d` (khóa riêng P-256 test = SHA-256 của chuỗi ASCII `teleport test vector A key`) | `d7482011631edcff51c30d35c66198ebfef2a332250180e7bb0e4638726f3006` |
| `Q` (khóa công khai, dạng không nén `04 ‖ x ‖ y`) | `0439390e2245eec503026763deedb561da31191fc173c7f0cebcc2b1f75c9da044ca2716a45d26999d21f0bc87a313402e594bd79eafbbd911aa29fd6cb27dc287` |
| `signature` (r ‖ s, một chữ ký hợp lệ của `d` trên `payload` ở trên) | `a64c0dcb5ec6ee7dc8acc45acbc3d240684b8e8136f20b87213ed20fdcae2724436c0d36d1bbb51bc79fb260ffcd9096f8066a16ccd506c3a934fcc2129bfa6e` |

### 5.5. Lưu trữ

Mỗi bên lưu `device_id`, tên, fingerprint và chứng chỉ PEM của bên kia vào `trusted.json` (định dạng ở [architecture.md](architecture.md) mục 4). Thay đổi danh sách tin cậy có hiệu lực ngay với kết nối mới (mục 7).

Ghép đôi lại cùng `device_id` và cùng fingerprint ✅ (`teleport.app.pairing`):

- **Cả hai bên** ghi đè mục cũ, cập nhật tên theo `hello` của bên kia và cập nhật `paired_at`. Bên kia đã chứng minh giữ khóa riêng trên chính kết nối đó: bên trả lời qua bắt tay TLS, bên khởi xướng qua chữ ký trong `pair_nonce` (mục 5.1). Máy lạ dùng chứng chỉ công khai của một thiết bị đã tin cậy bị dừng trước khi hiện mã, nên không đổi được tên của thiết bị đó.
- Lời hỏi ghép đôi ở cả hai vai hiện tên đã lưu, và cả tên mới nếu tên sẽ đổi (mvp mục 7.2).

## 6. Kết nối giữa các thiết bị tin cậy & nối lại

### 6.1. Kết nối điều khiển ✅

Cài đặt ở `teleport.core.links` (máy trạng thái) và `teleport.app.node`.

- Mỗi cặp thiết bị tin cậy đang cùng trực tuyến giữ **đúng một kết nối điều khiển**, mở sẵn (clipboard cần độ trễ thấp, mvp N6).
- Cả hai bên đều có thể chủ động kết nối: khi thấy thiết bị tin cậy qua mDNS, khi có việc cần gửi, hoặc khi nối lại. Nhờ vậy vẫn kết nối được khi tường lửa chỉ chặn một chiều; kết nối dữ liệu đi theo chiều của kết nối điều khiển (mục 4.1).
- **Chống trùng:** khi đã có kết nối điều khiển với một thiết bị mà có thêm kết nối mới:
  - Kết nối cũ đã mở được **ít nhất 5 s** → kết nối mới thay kết nối cũ. Trường hợp này là bên kia nối lại sau khi mạng chập chờn, còn kết nối cũ có thể đã chết mà chưa ai biết.
  - Hai kết nối do **cùng một bên** mở → kết nối mới thay kết nối cũ, bất kể cách nhau bao lâu (bên đó chỉ mở kết nối mới khi đã thấy kết nối cũ chết).
  - Hai kết nối do hai bên mở cách nhau **dưới 5 s** (hai bên gọi nhau cùng lúc) → giữ kết nối do bên có `device_id` **nhỏ hơn** mở, đóng kết nối kia.
  - Mỗi bên đo theo đồng hồ của mình: phía server lúc nhận `control_open`, phía client lúc gửi xong `control_open`. Hai bên đo lệch nhau một chút, nên ở sát ngưỡng 5 s có thể cả hai kết nối cùng bị đóng; lần nối lại sau sẽ ổn, và việc đồng bộ lại (mục 6.3) bảo đảm không mất gì.
  - Đổi kết nối không báo người dùng.
- Message đang gửi dở trên kết nối bị đóng có thể mất. Lượt truyền không bị ảnh hưởng vì hai bên đồng bộ lại trên kết nối mới (mục 6.3); clipboard chỉ gửi theo kiểu cố gắng, không gửi lại.

### 6.2. Nối lại

- 📝 Mất kết nối điều khiển (lỗi socket hoặc keepalive hết 30 s) → mọi lượt truyền với máy đó chuyển sang “Đang nối lại…”. Bên nào còn lượt truyền dở dang thì thử kết nối lại theo lịch 1 s, 2 s, 4 s… tối đa 30 s một lần, trong tối đa 24 giờ; địa chỉ lấy lại từ mDNS vì IP có thể đã đổi.
- ✅ **Không có lượt truyền nào** (chỉ giữ kết nối điều khiển, như ở Giai đoạn 1): mất kết nối mà mDNS vẫn thấy máy kia → thử lại theo cùng lịch, cho tới khi mDNS báo máy đó đã đi. mDNS báo máy xuất hiện lại hoặc đổi địa chỉ → thử ngay.
- ✅ **Bị từ chối** (EOF hoặc lỗi SSL trước `hello`, sau khi bắt tay TLS xong, mục 2.2) → không tự thử lại theo lịch; chỉ thử lại khi mDNS báo sự kiện mới về máy đó hoặc khi người dùng thao tác. Nhờ vậy máy đã hủy ghép đôi không bị gọi liên tục. Cũng áp cho danh tính không khớp (`IdentityMismatch`) và khác phiên bản giao thức.
- ✅ **Giới hạn tốc độ:** sự kiện mDNS hay mạng đổi được thử ngay nhưng cách lần thử trước ≥ 2 s; máy đang bị chặn (gạch trên) thì lần thử do sự kiện cách lần thử trước ≥ max(2 s, lịch 1, 2, 4… ≤ 30 s), và bộ đếm không bị xóa khi mDNS báo máy đi rồi đến lại. Giới hạn 2 s chỉ áp cho lần thử do sự kiện; lịch 1, 2, 4… (gạch “Không có lượt truyền nào”) là lịch theo thời gian, nên lần thử theo lịch đầu tiên vẫn là 1 s sau lần thất bại. Mỗi loại lỗi chỉ báo người dùng một lần cho tới khi kết nối được, hoặc người dùng ghép đôi lại; “không kết nối được” chỉ báo sau 3 lần thất bại liên tiếp.
- 📝 Kết nối điều khiển có lại → hai bên đồng bộ lại (mục 6.3); bên gửi chỉ gửi các khối có bit = 0, trên kết nối dữ liệu mới (mục 4.1).
- 📝 Bên gửi kiểm `size` và `mtime` của file nguồn trước khi gửi tiếp; khác manifest → `cancel{source_changed}`.
- 📝 **Tắt app nhận** (hoặc khởi động lại máy): trạng thái nối lại nằm trên đĩa, nên bên nhận gửi `resume` khi mở lại app. Lượng gửi lại vẫn theo mục 4.3.
- 📝 **Tắt app gửi:** v1.0 không lưu lượt truyền của bên gửi xuống đĩa. Bên gửi nhận `resume` cho `transfer_id` không biết → trả `cancel{unknown_transfer}`; bên nhận xóa file tạm và báo người dùng gửi lại.
- 📝 File dở dang không còn được nối lại sau 7 ngày thì bị dọn (chỉnh được trong cài đặt, mvp F6).

### 6.3. Đồng bộ lại sau mỗi kết nối điều khiển mới 📝

Message điều khiển có thể mất khi kết nối đứt hoặc bị thay (mục 6.1). Vì vậy mọi message của lượt truyền đều **gửi lại được, và nhận trùng không gây hại** (nhận diện bằng `transfer_id` + `file_index`). Ngay khi có kết nối điều khiển mới với một máy, cho mọi lượt truyền chưa kết thúc giữa hai máy:

| Bên | Việc làm khi có kết nối mới | Khi nhận message trùng hoặc lạ |
|---|---|---|
| Gửi | Gửi lại `offer` của lượt truyền chưa nhận được `accept`/`reject` | `resume` có đủ mọi bit → gửi lại `complete`. `resume`/`data_open` của lượt truyền đã hủy hoặc không biết → `cancel{unknown_transfer}` |
| Nhận | Gửi `resume` (với `seq` mới) cho file hiện tại của mọi lượt truyền đã chấp nhận | `offer` đã trả lời → gửi lại `accept`/`reject`; `offer` đang chờ người dùng → bỏ qua. `complete` của file đã xong → gửi lại `verified`. `complete`/`offer` của lượt truyền đã hủy → `cancel` |

- Bên nhận giữ trạng thái mức lượt truyền (đã chấp nhận, file hiện tại, các file đã `verified`) trong `transfer.json` trên đĩa ([architecture.md](architecture.md) mục 4) cho tới 24 giờ sau khi file cuối xong, để trả lời `complete` gửi lại cả sau khi khởi động lại. `reject` chỉ cần nhớ trong bộ nhớ: khởi động lại thì `offer` gửi lại sẽ được hỏi lại người dùng. Bên gửi coi lượt truyền kết thúc khi đã nhận `verified` của file cuối.
- `cancel` bị mất được sửa ở lần đồng bộ sau: bên kia gửi lại `offer` hoặc `resume`, bên đã hủy trả `cancel`.
- Nhờ vậy, không message nào bị mất mà làm lượt truyền treo khi hai máy vẫn còn kết nối được với nhau.

## 7. Hủy ghép đôi & danh tính đổi 📝

- **Hủy ghép đôi** chỉ xóa mục ở máy bấm hủy, cùng cài đặt theo thiết bị đó (“Hỏi trước khi nhận”, “Đồng bộ clipboard”), và có hiệu lực ngay. Không có message báo cho máy kia.
  - Listener nhận TCP trần và bắt đầu TLS cho từng kết nối với `SSLContext` **hiện tại** (mục 2.1). Khi danh sách tin cậy đổi, node dựng context mới; từ đó mọi kết nối mới dùng context mới, listener không đóng hay mở lại ✅ (`Listener.update_trusted`).
  - Đóng kết nối đang mở với máy bị hủy (các kết nối khác giữ nguyên) ✅ (`Node.unpair`: xóa khỏi kho, đổi context, đóng kết nối điều khiển của máy đó, không có `await` xen giữa).
  - Khi không có node nào chạy, `teleport unpair` chỉ sửa `trusted.json` ✅.
- Kết nối đã bắt đầu bắt tay bằng context cũ vẫn bắt tay xong với context đó. Vì vậy khi nhận `control_open` (✅) hay `data_open` (📝), node còn kiểm fingerprint của client có trong danh sách tin cậy **hiện tại** không; không có thì đóng.
- **Máy kia từ chối máy này** (đã hủy ghép đôi, hoặc ghép đôi một phía): lần kết nối sau nhận EOF hoặc lỗi SSL trước `hello` (mục 2.2) → báo “*X* từ chối kết nối từ máy này” và gợi ý ghép đôi lại. Không đoán lý do, vì client không phân biệt được. ✅ (`teleport listen` in thông báo)
- **Cùng `device_id`, khác fingerprint** (mDNS, `hello` hay `pair_request` báo `device_id` của một máy tin cậy nhưng chứng chỉ khác): không có lý do hợp lệ, vì danh tính mới luôn có `device_id` mới (mục 1). Chặn kết nối, không cho ghép đôi đè (mục 5.1), cảnh báo có thể có giả mạo. Phần ghép đôi ✅ (từ chối trước khi hiện mã; ở bên trả lời, `pair_request` chưa được xác minh nên chỉ cảnh báo, không gợi ý hủy ghép đôi máy thật, mvp mục 8); phần kết nối điều khiển và mDNS ✅ (`IdentityMismatch` → cảnh báo một lần, không tự thử lại theo lịch). Muốn ghép đôi lại thì người dùng phải hủy ghép đôi mục cũ trước, và khi đó cài đặt theo thiết bị cũ cũng bị xóa, nên máy giả không thừa hưởng “Luôn nhận” hay “Đồng bộ clipboard”.
- **Máy tin cậy xóa thư mục cấu hình** (cài lại sạch): xuất hiện như một thiết bị mới chưa ghép đôi, có thể trùng tên với mục cũ. App gợi ý ghép đôi lại; ghép đôi xong thì đề nghị xóa mục cũ cùng tên (mục cũ không bao giờ kết nối lại được).

## 8. Khám phá (mDNS) ✅

Cài đặt ở `teleport.core.discovery` (bản ghi, lọc, theo dõi thiết bị) và `teleport.net.discovery` (zeroconf). Dùng bởi `teleport.app.node` (`teleport listen`) và `teleport list` / `teleport pair <tên>`.

- Dịch vụ `_teleport._tcp.local.`; cổng thật nằm trong bản ghi SRV. **Tên instance là `device_id`** (`<device_id>._teleport._tcp.local.`, luôn khác nhau giữa các máy, nên zeroconf không phải tự đổi tên); tên hiển thị chỉ nằm trong TXT. Host trong SRV là `teleport-<device_id>.local.`. Chỉ quảng bá bản ghi A (IPv4, mục 2.1), tối đa **16 địa chỉ**.
- Thuộc tính TXT: `id` (`device_id`), `name`, `os`, `proto` (phiên bản giao thức). Không quảng bá fingerprint: TLS và `trusted.json` đã đủ để nhận ra thiết bị tin cậy.
- Mỗi chuỗi TXT tối đa 255 byte (RFC 6763 mục 6.1). Tên thiết bị hợp lệ có thể dài tới 256 byte UTF-8, nên `name` trong TXT được cắt theo code point cho vừa **≤ 200 byte**, rồi bỏ khoảng trắng và U+200D (ZWJ) ở cuối; tên đầy đủ lấy từ `hello` khi kết nối. Cắt theo code point, không theo grapheme, nên emoji ghép có thể bị cắt mất phần sau.
- Mọi bản ghi nhận được là dữ liệu không tin cậy; một trường sai thì **bỏ qua cả bản ghi** đó (ghi log DEBUG, không báo người dùng):
  - Khóa TXT là chữ thường, so chính xác (`ID` không phải `id`). Khóa trùng thì lấy lần đầu; khóa lạ bị bỏ qua; khóa thiếu `=` (không có giá trị) là sai.
  - `id` kiểm như `device_id` và **phải bằng nhãn của tên instance**; `os` kiểm như `os`; `name` như `name` (mục 3.3), dài ≤ 200 byte UTF-8 và giải mã UTF-8 chặt; `proto` là số nguyên thập phân ASCII 1…2⁵³−1, không có số 0 ở đầu. `proto` khác phiên bản của máy này vẫn hợp lệ (so phiên bản ở `hello`, mục 9).
  - Cổng SRV phải là 1–65535.
  - Địa chỉ: chỉ IPv4; bỏ loopback, link-local, multicast, reserved (`240.0.0.0/4`, `255.255.255.255`) và `0.0.0.0/8`; bỏ trùng, sắp tăng dần, giữ tối đa 16. Không còn địa chỉ nào → bỏ bản ghi.
- Thoát app thì gửi gói “goodbye” để máy khác xóa thiết bị ngay (mvp N1).
- Mọi bản ghi được đăng ký với **TTL 60 s**, thay cho TTL mặc định 75 phút của mDNS (RFC 6762).
- **Kiểm còn sống:** python-zeroconf nâng TTL của bản ghi PTR *nhận được* lên tối thiểu 1125 s (goodbye TTL 0 thì không), nên máy mất mạng đột ngột hoặc đi ngủ không biến mất khi PTR hết hạn. Bên nhận vì vậy **resolve lại SRV/TXT/A mỗi 15 s** cho từng thiết bị (bộ hẹn giờ chạy mỗi 5 s, chờ tối đa 3 s mỗi lần resolve). Khi SRV/TXT/A còn trong cache, resolve trả ngay từ cache; hết hạn thì zeroconf gửi truy vấn mới ngay.
  - Cần **2 lần resolve thất bại liên tiếp** mới xóa thiết bị (chống nhấp nháy khi mất một gói); resolve thành công lần sau → thiết bị hiện lại. Máy trả lời nhưng bản ghi mới bị sai vẫn được coi là còn sống, và giữ bản hợp lệ cũ.
  - Dịch vụ chưa từng có bản ghi hợp lệ mà resolve thất bại (hoặc bản ghi sai) 2 lần liên tiếp thì bị ngừng theo dõi, nhường chỗ trong giới hạn 256 bên dưới.
  - **Probe thưa dần:** thiết bị đã từng hiện nhưng đã bị xóa vì kiểm còn sống thất bại vẫn được theo dõi, nhưng khoảng cách giữa hai lần resolve nhân đôi sau mỗi lần thất bại (15 s, 30 s, 60 s…), tối đa **240 s**. Có bản ghi hợp lệ, hoặc zeroconf báo thấy lại dịch vụ (resolve ngay), thì trở về 15 s. Thiết bị đang hiện luôn được kiểm mỗi 15 s.
    - Hệ quả: thiết bị đã biến mất rồi quay lại **mà không quảng bá lại** (ngủ rồi thức, Wi-Fi nối lại) có thể chỉ hiện lại ở lần probe kế tiếp, tức chậm tối đa 240 s + 5 s nhịp hẹn giờ + 3 s resolve ≈ 248 s. Thiết bị quảng bá lại (zeroconf báo `Updated`) thì được resolve và hiện ngay; vì vậy node quảng bá lại khi card mạng/địa chỉ đổi hoặc khi máy thức dậy ✅. Cách phát hiện (`teleport.core.discovery.NetworkWatch`): mỗi 5 s node đọc địa chỉ quảng bá và hai đồng hồ (monotonic, giờ hệ thống); địa chỉ đổi → khởi động lại mDNS (zeroconf chỉ tham gia nhóm multicast trên các địa chỉ có lúc khởi động); đồng hồ nhảy quá 15 s (khoảng monotonic quá dài, hoặc giờ hệ thống lệch khỏi monotonic) → quảng bá lại.
  - Resolve lần đầu (thiết bị mới hoặc vừa đổi) được ưu tiên hơn kiểm còn sống; tối đa 16 resolve chạy cùng lúc, luôn dành 4 trong 16 chỗ cho kiểm còn sống, và mỗi thiết bị có tối đa một resolve đang chạy.
  - Resolve của **thiết bị đã ghép đôi** được xếp lên **đầu** hàng chờ resolve, trước mọi dịch vụ chưa ghép đôi đang chờ. Nhờ vậy khi có lũ dịch vụ giả, thiết bị đã ghép đôi chỉ chờ một chỗ trống, và vẫn hiện sau khoảng một lần hết giờ resolve (vài giây).
  - Cận trên, khi không có hàng đợi resolve: ≈ 60 s (TTL, bản ghi còn trong cache) + 2 × (15 s chu kỳ + 5 s nhịp hẹn giờ + 3 s resolve) ≈ 106 s, trong thời hạn mvp N1. Số đo thực tế: [benchmark.md](benchmark.md) mục 3.3 (máy ảo bridged; đo lại trên 2 máy thật ở `ke_hoach.md` Giai đoạn 4).
  - Giới hạn đã biết: máy thật mà 2 lần resolve đầu đều mất gói (Wi-Fi xấu) bị bỏ như bản ghi chưa từng hợp lệ, và chỉ hiện lại khi zeroconf nạp lại SRV/TXT/A (máy đó quảng bá lại, hoặc bên duyệt làm mới PTR ở mốc 75 % TTL, có thể tới ~14 phút). Kịch bản này chưa đo ([benchmark.md](benchmark.md) mục 3.4).
- Bên nhận theo dõi tối đa **256** dịch vụ cùng lúc; dịch vụ của chính máy này bị bỏ qua.
  - **Ưu tiên thiết bị đã ghép đôi:** dịch vụ có `device_id` nằm trong `trusted.json` luôn được theo dõi và **không tính vào 256** (số này bị chặn bởi số thiết bị tin cậy). Một dịch vụ đang theo dõi trở thành tin cậy (vừa ghép đôi) thì nhả một chỗ trong 256, nên mục cũ nhất của danh sách tràn bên dưới được nhận ngay. Thiết bị thôi tin cậy mà đang được theo dõi thì vẫn giữ, nhưng từ đó tính vào 256.
  - **Danh sách tràn:** dịch vụ mới khi đã đủ không bị quên (zeroconf chỉ báo một dịch vụ mới một lần cho mỗi PTR) mà được ghi vào danh sách chờ, tối đa **1024** `device_id`, thứ tự vào trước ra trước, đầy thì bỏ mục cũ nhất. Mỗi khi một dịch vụ bị ngừng theo dõi và có chỗ trống, mục cũ nhất trong danh sách được nhận và resolve ngay. Dịch vụ cũ không bao giờ bị đẩy ra để nhường chỗ. Mục chờ đã cũ (máy đã đi mà không gửi goodbye) được nhận rồi bị bỏ sau 2 lần resolve thất bại như dịch vụ chưa từng hợp lệ.
- ⚠️ **mDNS không xác thực.** Bất kỳ máy nào trong LAN cũng quảng bá được `<device_id>` của máy khác, với tên, địa chỉ, cổng tùy ý. Kết quả khám phá chỉ là gợi ý để biết kết nối tới đâu; danh tính chỉ được tin qua fingerprint chứng chỉ khi bắt tay TLS (mục 2.2, 7). Không bao giờ dùng `name` hay `device_id` từ mDNS để quyết định tin cậy.
  - Vì vậy có ba kiểu từ chối dịch vụ đã biết mà mDNS không chặn được:
    1. **Lũ PTR** lấp đầy danh sách theo dõi (256 dịch vụ và 1024 mục chờ): máy *chưa ghép đôi* mới có thể không hiện. Máy đã ghép đôi vẫn được theo dõi và được resolve trước hàng chờ, nên vẫn hiện sau vài giây.
    2. **Goodbye giả** (bản ghi TTL 0 mạo danh) xóa hoặc ẩn một thiết bị khỏi danh sách.
    3. **Đè bản ghi SRV/A** làm kết nối đi sai địa chỉ hoặc cổng. Kiểm chứng chỉ server ngay trong bắt tay chặn việc mạo danh, nên kẻ tấn công chỉ làm kết nối thất bại; nếu nó chuyển tiếp byte tới thiết bị thật thì còn lấy được ưu tiên cho IP của nó (gạch “Máy giả bản ghi mDNS” bên dưới; mục 2.3).
  - Đường dự phòng cho cả ba là **nhập IP thủ công** (mục 2.1).

### Những gì máy này để lộ

Phần này tả hành vi hiện có ✅, trừ chỗ đánh 📝.

- **Người nghe thụ động mDNS** (mọi máy trong cùng mạng, không cần kết nối hay ghép đôi) đọc được `device_id` ở tên instance, ở host trong SRV và ở TXT `id`, cùng `name`, `os`, `proto`, địa chỉ và cổng. `device_id` không đổi khi máy chuyển mạng (nó gắn với thư mục cấu hình, mục 1), nên ai ghi lại bản quảng bá ở một mạng (quán cà phê, khách sạn) nhận ra được cùng thiết bị đó ở mạng khác.
- **Máy kết nối tới cổng** (không cần ghép đôi): bắt tay TLS cho nó chứng chỉ server, tức `device_id` (trong CN) và fingerprint, đều cố định; rồi `hello` của server cho nó `name`, `app_version` và `os` trước khi máy kia được xác thực (mục 2.1, 3.2).
- 📝 **Khi máy đang ẩn** (mvp F10): node không quảng bá (lúc chuyển sang ẩn thì gửi goodbye như khi thoát app, gạch “goodbye” ở trên) nhưng vẫn duyệt mDNS và vẫn tự kết nối tới thiết bị tin cậy mà nó thấy. Node tự kết nối tới địa chỉ thấy qua mDNS (mục 6.1, 6.2), nên hai máy tin cậy cùng ẩn sẽ không tự kết nối được với nhau. Kết nối ẩn danh tới máy đang ẩn: mục 2.3. Chứng chỉ server vẫn lộ cho máy nào kết nối tới cổng, vì nó được gửi trong bắt tay, trước khi biết máy kia là ai.
- **Máy giả bản ghi mDNS của một thiết bị tin cậy** (đè SRV/A, gạch ⚠️ ở trên): khi node tự kết nối ra (kết nối điều khiển, sau này cả kết nối dữ liệu), client dùng `CERT_REQUIRED` với đúng chứng chỉ đã lưu của máy kia làm trust anchor (mục 2.1, 2.2), nên OpenSSL từ chối server sai **trước khi** gửi chứng chỉ client. Máy giả chỉ thấy ClientHello: không thấy chứng chỉ client, không nhận `hello`, nên không đọc được `device_id`, fingerprint hay tên của máy này, kể cả khi máy này đang ẩn (máy đang ẩn vẫn duyệt mDNS và tự kết nối ra). Lỗi đó (`ssl.SSLCertVerificationError`) tính là `IdentityMismatch`, không phải `Unreachable` (ngoại lệ của luật “lỗi trong lúc TLS là không kết nối được” ở mục 2.2), để vẫn có cảnh báo giả mạo (mvp mục 8, “Danh tính thiết bị thay đổi”) và không tự thử lại theo lịch (mục 6.2) ✅. Máy giả không có khóa riêng nên kết nối ra tới nó không thành công, và không cho nó ưu tiên nào (mục 2.3). Nhưng nếu nó **chuyển tiếp nguyên byte** (relay) tới thiết bị tin cậy thật, kết nối ra thành công và IP của máy giả được ưu tiên, vì mTLS chứng minh thiết bị chứ không chứng minh IP; các kết nối không bắt tay tin cậy được từ IP đó tính lần lỗi lúc chúng thôi ở trạng thái mở đầu, và quá ngân sách lỗi thì IP đó mất ưu tiên 24 h; trước đó nó giữ được tới 4 chỗ mở đầu ưu tiên khoảng 20 s (mục 2.3, “Ngân sách lỗi và hạ ưu tiên”). Kẻ có nhiều IP xoay vòng relay thì vẫn chặn được pool ưu tiên (mục 2.3, giới hạn M3).
- Chưa che ở v1.0: `device_id` cố định trong mDNS (khi máy đang hiện) và trong chứng chỉ server (mọi lúc). Quảng bá ẩn danh để sau v1.0 ([ke_hoach.md](ke_hoach.md) mục 5.3); tự ẩn khi Windows ở mạng Public còn đang cân nhắc ([ke_hoach.md](ke_hoach.md) mục 9).

## 9. Phiên bản giao thức

- Hai bên so `hello.protocol`: khác nhau thì đóng kết nối và báo cập nhật app (mvp mục 8).
- Phiên bản 1 là dự thảo tới bản phát hành đầu tiên có mạng (`v0.1.0-alpha`). Từ bản đó, **mọi thay đổi không tương thích đều tăng số phiên bản**, kể cả trước `v1.0.0`, để người dùng thử các bản alpha/beta khác nhau nhận được thông báo rõ ràng thay vì lỗi khó hiểu.
- Thêm message hay trường tùy chọn mới mà bản cũ bỏ qua được (mục 3.1, 3.3) thì không phải tăng số.
