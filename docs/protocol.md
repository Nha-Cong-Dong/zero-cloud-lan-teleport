# Giao thức Teleport

**Phiên bản giao thức: 1 (dự thảo).** Phiên bản giao thức đánh số riêng, độc lập với phiên bản app. Quy tắc tăng số: mục 9.

Trạng thái từng phần: ✅ đã cài đặt · 📝 dự thảo, chưa cài đặt.

## 1. Danh tính thiết bị ✅

- Mỗi thiết bị có một khóa **ECDSA P-256** và một **chứng chỉ X.509 tự ký** (chữ ký ECDSA-SHA256), hiệu lực từ `2000-01-01T00:00:00Z` tới `9999-12-31T23:59:59Z` (“không hết hạn”, RFC 5280 mục 4.1.2.5). OpenSSL kiểm ngày hiệu lực khi bắt tay mTLS, nên không ngày nào được phụ thuộc đồng hồ của máy tạo danh tính.
- `CN` của chứng chỉ là `teleport-<device_id>`, trong đó `device_id` là 32 ký tự hex (UUID4).
- **Fingerprint** = SHA-256 của chứng chỉ ở dạng DER, viết dưới dạng 64 ký tự hex thường. Đây là định danh duy nhất được tin cậy; tên thiết bị chỉ để hiển thị.
- **Pin ngắn** = 8 byte đầu của fingerprint, dạng `AB:CD:EF:…`. Chỉ để hiển thị (so bằng mắt); máy không bao giờ xác thực bằng pin ngắn.
- **Danh tính gắn với thư mục cấu hình, không gắn với máy.** Xóa thư mục cấu hình (gỡ cài đặt không giữ cài đặt, hoặc cài lại sạch) sinh `device_id` và fingerprint mới: với các máy khác, đó là một **thiết bị mới**, chưa ghép đôi (mục 7).
- File danh tính thiếu một nửa, hỏng, khóa không khớp chứng chỉ, hoặc mất danh tính mà vẫn còn `trusted.json` → app báo lỗi và dừng, **không bao giờ** tự tạo danh tính mới (làm vậy sẽ âm thầm làm hỏng mọi ghép đôi). Cách tạo và lưu: [architecture.md](architecture.md) mục 4.

## 2. Tầng vận chuyển

### 2.1. Một cổng, ba loại kết nối

- TCP + **TLS 1.3** ✅. Phiên bản thấp hơn bị từ chối.
- Mỗi node lắng nghe trên **một cổng TCP**, mặc định **47474** (listener ✅ `teleport.net.connection`). Cổng mặc định (hoặc cổng đã lưu) không mở được thì node để hệ điều hành chọn một cổng trống và lưu vào `settings.json` để lần chạy sau dùng lại ✅ (`teleport.app.node`); cổng đó hiện trong `teleport identity` / `teleport list` ✅, và cổng thật luôn được quảng bá qua mDNS (mục 8) ✅ (`teleport.app.node`). Nhập IP thủ công nhận `ip` (dùng cổng mặc định) hoặc `ip:cổng` ✅ (`teleport pair`).
- Node nhận **TCP trần** trên `0.0.0.0` rồi bắt đầu TLS cho từng kết nối bằng `SSLContext` **hiện tại** ✅, nên đổi danh sách tin cậy không phải mở lại cổng (mục 7).
- **Địa chỉ:** v1.0 chỉ dùng IPv4. Node lắng nghe trên mọi địa chỉ IPv4 (`0.0.0.0`) và quảng bá qua mDNS các địa chỉ IPv4 của máy (tối đa 16, mục 8; chưa lọc theo trạng thái bật/tắt của card), bỏ loopback, link-local `169.254.0.0/16` và các card ảo nhận ra được ✅. Card ảo nhận ra theo tên card (tiền tố như `docker`, `veth`, `vEthernet`, `tun`, `wg`, `utun`, hoặc chứa `virtual`, `hyper-v`, `wsl`, `vpn`…; danh sách đầy đủ ở `teleport.core.discovery`). Hai ngoại lệ: luôn giữ **địa chỉ nguồn ưu tiên** dù card đó trông như card ảo — đó là địa chỉ nguồn hệ điều hành chọn cho tuyến tới nhóm multicast mDNS, hoặc, nếu địa chỉ đó là loopback (Windows thường chọn `127.0.0.1` cho tuyến multicast), địa chỉ nguồn của tuyến mặc định; địa chỉ này đứng đầu danh sách quảng bá; và nếu mọi địa chỉ đều nằm trên card ảo thì quảng bá tất cả thay vì không quảng bá gì. Client thử **song song** mọi địa chỉ được quảng bá của một thiết bị và giữ kết nối thành công đầu tiên, nên một địa chỉ chết không làm chậm việc kết nối (mvp F2, N1). Mỗi lần thử chỉ đi tới khi đã đọc và kiểm `hello` của server; message mở đầu (bảng bên dưới) chỉ được gửi trên kết nối thắng, sau khi các lần thử khác đã bị đóng, nên server không nhận kết nối điều khiển thừa ✅.
- Server dùng `CERT_OPTIONAL` ✅ và nạp chứng chỉ của mọi thiết bị tin cậy làm trust anchor, kèm kiểm tra X.509 nghiêm ngặt (`teleport.net.tls.server_context(..., allow_anonymous=True)`):
  - Client **gửi chứng chỉ** → chứng chỉ phải nằm trong danh sách tin cậy, nếu không bắt tay thất bại (kể cả khi danh sách rỗng). Kiểm chứng bởi `tests/net/test_tls.py`.
  - Client **không gửi chứng chỉ** → kết nối ẩn danh, chỉ được dùng để ghép đôi.
- Ngay sau bắt tay, hai bên gửi `hello`. Rồi **client khai báo loại kết nối bằng message đầu tiên của nó**. Trước khi đọc được message này, server không gửi gì ngoài `hello`. Sai loại hoặc quá giờ → đóng. Thứ tự ✅:
  - Server gửi `hello` ngay sau bắt tay, rồi đọc `hello` của client, rồi đọc message mở đầu. Cả hai được đọc với giới hạn 16 KiB, trong **cùng một hạn 10 s cho cả chuỗi** (tính từ lúc nhận TCP, gồm cả bắt tay TLS), không gia hạn khi có byte tới.
  - Client của kết nối điều khiển so fingerprint của server **trước khi gửi bất kỳ byte nào** (kể cả `hello`).

| Loại kết nối | Client gửi chứng chỉ? | Message đầu tiên của client sau `hello` | Dùng cho |
|---|---|---|---|
| **Ghép đôi** | Không | `pair_request` | `pair_*` (mục 5) |
| **Điều khiển** | Có, đã tin cậy | `control_open` | Mọi message ở mục 3 trừ `pair_*`, `control_open`, `data_open` |
| **Dữ liệu** | Có, đã tin cậy | `data_open` | Khung khối nhị phân (mục 4) |

### 2.2. Xác thực hai chiều

- **Client kiểm tra server:** không dùng CA hay hostname (`CERT_NONE`). Sau khi bắt tay, client so **toàn bộ** fingerprint chứng chỉ server với fingerprint đã lưu trong `trusted.json` (`verify_fingerprint`) ✅, và kiểm `CN` khớp `hello.device_id` của server ✅. Không khớp → đóng kết nối, báo người dùng (mvp mục 8, “Danh tính thiết bị thay đổi”).
- **Server kiểm tra client:** OpenSSL đã kiểm chứng chỉ trong lúc bắt tay; server còn kiểm `CN` khớp `hello.device_id` của client ✅.
- **Không có session ticket** ✅ (`num_tickets = 0`): kết nối dùng lại session sẽ bỏ qua bước kiểm chứng chỉ client, nên máy vừa bị hủy ghép đôi có thể vào lại bằng session cũ. Mỗi kết nối đều bắt tay đầy đủ với danh sách tin cậy hiện tại.
- Ghép đôi (kết nối ẩn danh): client lấy chứng chỉ server từ bắt tay TLS; server nhận chứng chỉ client trong `pair_request` (mục 5).
- **Lưu ý TLS 1.3:** client bị từ chối vẫn thấy kết nối mở thành công; lỗi chỉ lộ ra ở lần đọc đầu tiên. Vì vậy client chỉ coi là đã kết nối khi **đọc được `hello` của server**, trên **mọi** loại kết nối ✅. Nhận EOF, lỗi SSL hoặc reset trước `hello` → máy kia từ chối máy này. “Bị từ chối” chỉ tính khi lỗi tới **sau khi bắt tay TLS xong**; lỗi trong lúc TCP/TLS là “không kết nối được”, vì server đóng ngay các kết nối vượt giới hạn trước cả TLS (mục 2.3), và nếu coi đó là bị từ chối thì một máy làm lũ kết nối sẽ khiến node ngừng tự nối lại (mục 6.2). Trên thực tế client thường chỉ thấy EOF, không thấy loại cảnh báo TLS, nên **không phân biệt được lý do** (bị hủy ghép đôi, ghép đôi một phía…); thông báo cho người dùng phải chung chung (mục 7).

### 2.3. Giới hạn và thời gian chờ

Phần ở tầng mạng đã cài ở `teleport.net.connection` ✅; những chỗ đánh 📝 (kết nối dữ liệu, các luật do node áp) chưa cài.

| | Kết nối ghép đôi | Kết nối điều khiển | Kết nối dữ liệu |
|---|---|---|---|
| Bắt tay TLS + `hello` + message mở đầu | 10 s ✅ | 10 s ✅ | 10 s 📝 |
| Kích thước message tối đa | **16 KiB** (`MAX_PAIRING_MESSAGE_SIZE`) ✅ | 16 MiB (`MAX_MESSAGE_SIZE`) ✅; `hello` và `control_open` 16 KiB ✅ | 16 KiB cho `hello`/`data_open`; khung khối theo mục 4 📝 |
| Sau khi mở đầu | Chờ mỗi message 10 s; riêng lúc chờ người dùng xác nhận: 60 s, cộng 10 s chờ `pair_confirm` của máy kia (bên dưới) | Keepalive (bên dưới) ✅; một đoạn gửi không xong (`drain()`) trong 30 s → đóng ✅ | Bên nhận file: không nhận được byte nào trong 30 s → đóng. Bên gửi file: `drain()` không xong trong 30 s, hoặc mất kết nối điều khiển với máy đó → đóng 📝 |
| Số kết nối đồng thời | 1 phiên ghép đôi ✅ (`PairingGate`, nối vào node ở `teleport.app.pairing`); yêu cầu bị từ chối nhận `pair_abort{busy}` rồi bị đóng, tổng cộng tối đa 1 s ✅; phiên kết thúc mà không ghép đôi được cũng gửi `pair_abort` (nếu có) rồi đóng trong tổng cộng tối đa 1 s, nên máy kia không đọc cũng không giữ được phiên lâu hơn ✅; kết nối ẩn danh chưa vào phiên: 1 mỗi IP, 8 tổng cộng ✅ (mục 5.3) | 1 cho mỗi thiết bị tin cậy (mục 6.1) ✅ (`teleport.core.links`, `teleport.app.node`) | 1 cho mỗi lượt truyền (file gửi lần lượt); mỗi chiều giữa hai máy chỉ chạy 1 lượt truyền, lượt khác xếp hàng 📝 |

- **Kết nối đang mở đầu** (từ lúc nhận TCP tới khi xác định loại): tối đa **4 mỗi IP, 64 tổng**; quá thì đóng ngay, trước cả bắt tay TLS ✅. Kết nối đã xác định loại không còn tính vào giới hạn này.
- Kết nối ẩn danh vượt 1 mỗi IP hoặc 8 tổng bị đóng ngay sau bắt tay TLS, không nhận `hello` ✅. Kết nối ẩn danh thôi tính khi đã gửi `pair_request` (lúc đó node quyết định ngay có nhận phiên hay không, mục 5.3) hoặc khi đóng.
- Từ chối trong lúc mở đầu luôn là đóng ngay (TCP reset), không đóng gọn, để máy không đọc dữ liệu không giữ được socket.
- Header vượt giới hạn → đóng kết nối ngay, không đọc thân (`read_message(reader, max_size)`) ✅.
- `read_message` không tự đặt thời gian chờ; tầng gọi nó bọc trong `asyncio.timeout`.
- **Xác nhận ghép đôi** ✅ (`teleport.core.pairing`): mỗi máy tính 60 s từ lúc hiện mã; người dùng máy này phải quyết trong 60 s đó. Máy đã gửi `pair_confirm` mà chưa nhận `pair_confirm` của máy kia thì chờ thêm 10 s (`CONFIRM_GRACE`, tới 70 s kể từ lúc hiện mã), vì người dùng máy kia có thể bấm sát giây thứ 60 và message còn phải đi qua mạng. Hết giờ → `pair_abort{timeout}`; quyết định tới sau hạn chót vẫn là hết giờ.
- **Keepalive** trên kết nối điều khiển ✅ (`teleport.core.keepalive`, `teleport.net.connection.ControlChannel`): bên nào không gửi gì trong 10 s (mọi message đều tính, kể cả `pong`) thì gửi `ping`; không nhận được byte nào trong 30 s → coi là mất kết nối (mục 6.2). Tính theo **byte**: đọc được một phần message cũng là “nhận”, nên message lớn đi chậm trên mạng yếu không bị coi là mất kết nối. Nhận `ping` thì bắt buộc trả `pong` với đúng `t` đó; `pong` không cần trả lời.
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
- JSON có số nguyên quá dài (hơn 4300 chữ số) hoặc lồng sâu hơn **64 cấp** (object ngoài cùng tính là cấp 1) cũng là lỗi khung.
- Đóng kết nối đúng ở ranh giới giữa hai message là kết thúc bình thường. Đóng giữa chừng một message là lỗi.
- Mọi message có trường `"type"`. Trường không biết thì bỏ qua (để mở rộng về sau); thiếu trường bắt buộc thì là lỗi.

### 3.2. Các loại message

`hello`, các message `pair_*`, `control_open`, `ping` và `pong` đã có kiểu và kiểm tra hợp lệ (`teleport.core.messages`) ✅. `hello`, `control_open`, `ping` và `pong` đã được gửi trên mạng (`teleport.net.connection`) ✅; `pair_*` trên mạng ✅ (`teleport.app.pairing`). Các message còn lại là dự thảo 📝.

| `type` | Hướng | Kết nối | Trường |
|---|---|---|---|
| `hello` | ↔ | mọi loại | `protocol` (int ≥ 1), `device_id`, `name`, `app_version`, `os` |
| `pair_request` | khởi xướng → trả lời | ghép đôi | `cert` (PEM), `commitment` (64 hex) |
| `pair_challenge` | trả lời → khởi xướng | ghép đôi | `nonce` (64 hex = 32 byte) |
| `pair_reveal` | khởi xướng → trả lời | ghép đôi | `nonce` (64 hex = 32 byte) |
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
| `clipboard` | ↔ | điều khiển | `kind`: `text` · `image/png`, `sha256`, `data` (văn bản UTF-8 hoặc base64; dữ liệu gốc trước base64 ≤ 10 MiB) |
| `ping` / `pong` | ↔ | điều khiển | `t` (số nguyên, mili giây, 0 … 2⁵³ − 1) |

Hai bên gửi `hello` ngay sau khi bắt tay TLS, trên mọi loại kết nối. Khác `protocol` thì đóng kết nối và báo người dùng cập nhật app.

### 3.3. Quy tắc trường ✅

Mọi message đến từ máy khác đều không được tin: sai một trường là cả message bị từ chối.

- **`protocol`**: số nguyên JSON ≥ 1. Không nhận `true`/`false`, không nhận số thực (kể cả `1.0`), không nhận chuỗi.
- **`device_id`**: đúng 32 ký tự hex thường `[0-9a-f]`.
- **`name`** (1–64 code point) và **`app_version`** (1–32 code point):
  - không có khoảng trắng ở đầu hoặc cuối (kể cả khoảng trắng Unicode như NBSP, U+3000);
  - không chứa ký tự loại `Cc` (điều khiển), `Cs` (surrogate lẻ), U+2028, U+2029, U+200B, U+FEFF, U+2060, hay ký tự điều khiển hướng chữ U+200E, U+200F, U+202A–U+202E, U+2066–U+2069;
  - được phép: chữ có dấu, emoji, kể cả emoji ghép bằng ZWJ U+200D. Giá trị giữ nguyên, không chuẩn hóa NFC.
- **`os`**: 1–16 ký tự `[a-z0-9_]`. Giá trị chuẩn: `windows`, `macos`, `linux`. Giá trị lạ đúng dạng vẫn được nhận và hiển thị như hệ điều hành không rõ.
- **`cert`**: chuỗi ≤ 8192 ký tự, bắt đầu đúng bằng `-----BEGIN CERTIFICATE-----`. Bên nhận còn phân tích và kiểm tra chứng chỉ (mục 5).
- **`commitment`**, **`nonce`**: đúng 64 ký tự hex thường (32 byte).
- **`reason`** (`pair_abort`): 1–32 ký tự `[a-z_]`. Giá trị chuẩn: `user` (người dùng từ chối), `busy` (đang có phiên ghép đôi khác hoặc node không nhận ghép đôi lúc này), `timeout` (hết giờ), `invalid` (message hoặc cam kết sai). Giá trị lạ đúng dạng vẫn được nhận, để bản mới thêm lý do được.
- Kiểm tra hex, chữ số, `os` và `reason` chỉ dùng lớp ký tự ASCII trên **toàn bộ** chuỗi: `"user\n"` hay chữ số toàn chiều rộng đều sai.
- `type` là chuỗi nhưng chưa biết thì bên nhận phân biệt được với message hỏng (để bản cũ bỏ qua message mới).
- Message chỉ hợp lệ trên loại kết nối ghi ở bảng mục 3.2; sai loại kết nối là lỗi giao thức ✅ (`teleport.core.messages.allowed_types`).
- Trên kết nối điều khiển ✅: `type` chưa biết thì bỏ qua; khung sai, message sai trường, hoặc message sai loại kết nối → đóng kết nối điều khiển (nối lại theo mục 6.2).
- **`t`** (`ping`/`pong`): số nguyên JSON 0 … 2⁵³ − 1; không nhận `true`/`false` hay số thực (kể cả `1.0`, `1e3`). Mili giây theo đồng hồ của bên gửi `ping` (chỉ để đo độ trễ, không so giữa hai máy). Trả lời `ping`: mục 2.3.
- 📝 **`seq`**, **`resume_seq`**: số nguyên JSON 1 … 2⁵³ − 1; không nhận `true`/`false` hay số thực. Phạm vi của `seq`: mục 4.1.
- 📝 Quy tắc cho message truyền file (chốt khi cài đặt ở Giai đoạn 2): `transfer_id` đúng 32 hex thường; `file_index` < số file trong `offer`; `size` và `mtime` là số nguyên ≥ 0, `size` ≤ 2⁵²; `name` qua bộ làm sạch tên file (mục 4.4); `bitfield` dài đúng ⌈số khối / 8⌉ byte, các bit thừa ở cuối bằng 0.

## 4. Kênh dữ liệu 📝

### 4.1. Mở kết nối

- Mỗi lượt truyền có tối đa **một kết nối dữ liệu** tại một thời điểm; các file được gửi lần lượt. Kết nối dữ liệu mới cho cùng lượt truyền thay kết nối cũ (kết nối cũ bị đóng).
- Kết nối dữ liệu được mở **theo cùng chiều với kết nối điều khiển hiện tại**: bên đang là client của kết nối điều khiển mở kết nối dữ liệu. Chiều đó đã đi qua được tường lửa, nên một máy chỉ cho phép kết nối ra (ví dụ Windows đang ở hồ sơ mạng Public) vẫn gửi và nhận file được.
- Sau bắt tay TLS và trao `hello`, client gửi `data_open{transfer_id, file_index, resume_seq}`. Từ đó **bên gửi file** (dù là client hay server của kết nối này) ghi các khung khối; bên nhận file không gửi gì trên kết nối này, mọi phản hồi đi trên kết nối điều khiển.
- **Mỗi kết nối dữ liệu phục vụ đúng một `resume`.** Bên nhận là bên duy nhất quyết định cần khối nào: sau `accept` nó gửi `resume` cho file đầu tiên, sau `verified` một file nó gửi `resume` cho file kế tiếp (bitfield toàn 0), và gửi `resume` mỗi khi cần khối gửi lại (mục 4.2, 4.4, 6.3). Mỗi `resume` có `seq` mới; client của kết nối điều khiển mở một kết nối dữ liệu với `data_open{resume_seq = seq}`:
  - client là bên nhận: gửi `resume` rồi mở kết nối dữ liệu ngay;
  - client là bên gửi: mở kết nối dữ liệu khi nhận được `resume`.
  - `resume` có đủ mọi bit thì **không** mở kết nối dữ liệu: nó chỉ để kéo `complete` về (mục 6.3).
  - Bên gửi gửi xong các khối được yêu cầu thì đóng gọn kết nối dữ liệu.
- **Phạm vi của `seq` là một kết nối điều khiển:** bên nhận đánh `seq` lại từ 1 trên mỗi kết nối điều khiển mới, và bên gửi quên mọi `seq` cũ khi kết nối điều khiển đổi. Vì vậy `seq` không phải lưu xuống đĩa, và bên nhận vừa khởi động lại không bao giờ bị coi là đang gửi `seq` cũ.
- Hai kết nối không có thứ tự với nhau, nên `data_open` có thể tới trước `resume` tương ứng. Bên gửi chỉ gửi khối sau khi đã có, trên kết nối điều khiển hiện tại, `resume` với đúng `seq` đó và đúng `transfer_id`/`file_index` (chờ tối đa 10 s, quá thì đóng kết nối dữ liệu). `resume_seq` nhỏ hơn `seq` **lớn nhất** mà bên gửi đã nhận trên kết nối điều khiển hiện tại → kết nối cũ, đóng. Không nhét bitfield vào `data_open`, vì file 100 GB có bitfield khoảng 17 KB, vượt giới hạn 16 KiB.
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
- Bên gửi đọc khối, băm, rồi gửi hash **trong header của chính khối đó**: chỉ giữ một khối trong RAM, không phải đọc file hai lần, và hash không bao giờ tới sau dữ liệu. Bên gửi giữ hash các khối đã gửi trong RAM (32 byte mỗi khối, 320 KB cho file 10 GB) để tính Merkle root.
- Giới hạn tốc độ gửi được áp theo từng đoạn 64 KiB của payload, không phải theo cả khối, để dòng byte không ngưng quá lâu (thời gian chờ 30 s ở mục 2.3).
- Bên nhận băm lại payload và so với `sha256` trong header ngay khi nhận. Không khớp → bỏ khối, đóng kết nối dữ liệu, gửi `resume` (khối đó vẫn có bit = 0 nên được gửi lại). Khớp → lưu hash vào trạng thái nối lại và chuyển khối cho luồng ghi đĩa.
- Bên gửi gửi các khối còn thiếu theo thứ tự tăng dần.

### 4.3. Ghi đĩa và trạng thái nối lại

- Luồng ghi đĩa nhận khối qua hàng đợi có giới hạn **8 khối**. Hàng đợi đầy → bên nhận ngừng đọc socket → TCP làm bên gửi chậm lại (backpressure).
- **Mỗi 16 khối** (và khi đã nhận đủ khối của file): `fsync` file `.part` → bật bit các khối đó trong bitfield → ghi file trạng thái nguyên tử (file tạm, `fsync`, đổi tên). Một bit chỉ bật khi khối đã khớp hash **và** đã nằm bền trên đĩa, nên mất điện ở bước nào thì trạng thái cũng không đánh dấu khối chưa có trên đĩa.
- Trong lúc `fsync`, luồng ghi tạm dừng, nên số khối đã nhận mà chưa được đánh dấu luôn ≤ 16 + 8 + 1 (khối đang đọc).
- **Giới hạn gửi lại (mvp N4).** “Phần chưa nhận” tính theo bitfield của bên nhận lúc mất kết nối. Phần gửi thừa gồm các khối đã nhận mà chưa đánh dấu (≤ 25 MiB) cộng dữ liệu còn trong bộ đệm socket hai bên. Bên nhận đặt `SO_RCVBUF` ≤ 8 MiB và bên gửi đặt `SO_SNDBUF` ≤ 8 MiB, nên tổng ≤ 41 MiB, dưới ngưỡng **64 MiB**.
- Trạng thái nối lại gồm: manifest từ `offer`, bitfield, hash (từ header khung) của mọi khối đã đánh dấu. Vị trí file: [architecture.md](architecture.md) mục 4.

### 4.4. Hoàn tất và kiểm tra toàn vẹn

1. Bên gửi gửi xong mọi khối của file ít nhất một lần thì kiểm lại `size` và `mtime` của file nguồn; khác manifest → `cancel{source_changed}`. Không đổi → gửi `complete{merkle_root}` trên kết nối điều khiển. Bên gửi gửi lại `complete` mỗi khi nhận `resume` có đủ mọi bit của file đó (mục 6.3).
2. Bên nhận chờ tới khi **có `complete` và mọi bit đều bật**. Hai kết nối không có thứ tự với nhau, nên `complete` có thể tới trước khối cuối. Kết nối dữ liệu đóng mà file còn khối có bit = 0 → bên nhận gửi `resume`, bên gửi gửi tiếp các khối đó (mục 4.1).
3. **Lượt đọc lại:** bên nhận đọc lại toàn bộ `.part` **từ đĩa**, băm từng khối và so với hash đã lưu. Bước này bắt được mọi thay đổi trên `.part` sau khi khối được nhận (ví dụ bị sửa trong lúc tạm dừng, mvp F7). Khối sai → xóa bit, ghi trạng thái, gửi `resume` với bitfield mới; bên gửi gửi lại đúng các khối đó; quay lại bước 2.
4. Mọi khối khớp → tính Merkle root từ danh sách hash; khác `merkle_root` của bên gửi (trạng thái nối lại bị hỏng) → xóa toàn bộ bitfield và gửi `resume` với bitfield toàn 0 để nhận lại cả file.
5. Một file qua bước 3 hoặc 4 mà vẫn sai **quá 3 lần** (ví dụ ổ đĩa hỏng) → `cancel{error}`, báo người dùng.
6. Khớp → `fsync` → **đổi tên không ghi đè** sang tên thật → xóa trạng thái của file, chỉ giữ lại bản ghi nhỏ “file đã xong” của lượt truyền (mục 6.3) → gửi `verified`.
   - Đổi tên không ghi đè chạy được trên mọi hệ file, kể cả FAT/exFAT và ổ mạng. Chọn tên đích (đã có thì thử ` (1)`, ` (2)`…), **ghi tên đó vào trạng thái nối lại** rồi mới tạo file đích rỗng bằng `O_CREAT | O_EXCL` để giữ chỗ, sau đó `os.replace` file tạm đè lên **chính file giữ chỗ đó**.
   - `os.replace` gặp `PermissionError` (Windows: antivirus hay trình lập chỉ mục đang mở file giữ chỗ) → thử lại tối đa 5 lần, cách nhau 50 ms.
   - Bị ngắt giữa hai bước → lần sau, trạng thái chỉ ra đúng file giữ chỗ 0 byte đó để dùng lại hoặc xóa; không để lại file 0 byte mang tên thật.
   - Không dùng `os.rename` trần trên POSIX, vì nó âm thầm ghi đè file đích. Cũng không dùng `os.link`, vì FAT/exFAT không hỗ trợ.

**Merkle root:** lá là hash SHA-256 của từng khối theo thứ tự; nút cha = SHA-256(con trái ‖ con phải); tầng có số nút lẻ thì nút cuối được đẩy thẳng lên tầng trên; root là nút còn lại cuối cùng. File rỗng (0 khối): root = SHA-256 của chuỗi rỗng.

**Tên file nhận được** được làm sạch trước khi dùng (chi tiết: [ke_hoach.md](ke_hoach.md) mục 3.3). Hai file trùng tên trong cùng `offer`, hoặc trùng với file đã có (không phân biệt hoa thường), đều được đặt tên ` (1)`, ` (2)`…

## 5. Ghép đôi

Ghép đôi theo kiểu *numeric comparison* của Bluetooth: hai bên trao nonce ngẫu nhiên, bên khởi xướng **cam kết** nonce của mình trước khi thấy nonce bên kia, rồi cả hai hiện cùng một mã 6 số cho người dùng so. Nếu mã chỉ tính từ hai fingerprint, kẻ đứng giữa có thể sinh khóa offline tới khi ra mã trùng; với nonce cam kết, mỗi lần thử chỉ thành công với xác suất 10⁻⁶. Công thức đã cài đặt ở `teleport.core.pairing_code` ✅; máy trạng thái ghép đôi và chống làm phiền ở `teleport.core.pairing` ✅ (thứ tự message, hết giờ, lý do hủy, mục 5.1 và 5.3); trao đổi trên mạng ✅ (`teleport.app.pairing`).

### 5.1. Luồng

A là bên khởi xướng, B là bên trả lời.

```
A (khởi xướng)                              B (trả lời)
TLS tới cổng của B: A không gửi chứng chỉ (kết nối ghép đôi)
hello ───────────────────────────────────►
      ◄─────────────────────────────────── hello
pair_request{cert, commitment}   ────────►  B: kiểm cert, CN khớp hello.device_id của A
      ◄─────────────────────────────────── pair_challenge{nonce = n_B}  (hoặc pair_abort{busy})
pair_reveal{nonce = n_A}         ────────►  B: kiểm commitment; sai → pair_abort{invalid}
Cả hai tính mã và hỏi người dùng (hết giờ 60 s → pair_abort{timeout})
pair_confirm ◄──────────────────────────► pair_confirm   (từ chối → pair_abort{user})
Mỗi bên: đã GỬI pair_confirm VÀ đã NHẬN pair_confirm của bên kia → lưu trusted.json, đóng.
```

- n_A, n_B: mỗi nonce 32 byte ngẫu nhiên, sinh mới cho mỗi lần ghép đôi.
- Chứng chỉ của B: A lấy từ bắt tay TLS (đã được TLS xác thực là B giữ khóa riêng). A kiểm CN chứng chỉ đó khớp `hello.device_id` của B.
- **A phát hiện lỗi trước khi gửi `pair_request`** (CN không khớp `hello`, tự ghép với chính mình, trùng danh tính bên dưới): A chỉ đóng kết nối, không gửi gì. B thấy EOF trước message mở đầu thì không tạo phiên và không tính vào thời gian chờ của IP đó (mục 5.3).
- Chứng chỉ của A: B lấy từ `pair_request.cert`. Về sau, mTLS buộc A chứng minh giữ khóa riêng ở mỗi kết nối.
- Chứng chỉ nhận được phải: phân tích được; ≤ 8192 ký tự PEM, chỉ ASCII, đúng một khối `CERTIFICATE`; X.509 v3; subject có đúng một CN dạng `teleport-<32 hex thường>`; issuer trùng subject và chữ ký tự ký hợp lệ; khóa ECDSA P-256; chữ ký ECDSA-SHA256; không có dấu hiệu chứng chỉ CA: từ chối `BasicConstraints CA=true`, `KeyUsage` có `keyCertSign` hoặc `cRLSign`, và mọi chứng chỉ có extension Netscape Cert Type (OID `2.16.840.1.113730.1.1`). Lý do: chứng chỉ tin cậy được nạp làm trust anchor cho mTLS, nên không được phép ký ra chứng chỉ khác. Ngày hiệu lực không được kiểm khi ghép đôi (chứng chỉ của Teleport không hết hạn, mục 1).
- **Không ghép đôi đè danh tính:** `device_id` của bên kia đã có trong `trusted.json` với fingerprint **khác** → mỗi bên tự hủy phiên trước khi hiện mã (B gửi `pair_abort{invalid}`; A chỉ đóng, như ở trên), và báo người dùng (mục 7). Muốn ghép đôi lại thì phải hủy ghép đôi mục cũ trước. Cùng `device_id` và cùng fingerprint (ghép đôi lại sau ghép đôi một phía) thì được.
- Message sai thứ tự, message không phải `pair_*` (kể cả `hello` thứ hai; message có `type` chưa biết cũng không được bỏ qua trên kết nối ghép đôi), `pair_confirm` nhận lần hai, hay message lỗi → `pair_abort{invalid}` và đóng. Nhận `pair_abort` thì đóng, không trả lời.
- Rớt mạng giữa hai `pair_confirm` có thể để lại ghép đôi một phía. Phía còn tin sẽ bị từ chối ở lần kết nối sau (mục 7); ghép đôi lại sẽ sửa.

### 5.2. Công thức

`‖` là nối byte. Fingerprint (64 ký tự hex) được đổi sang 32 byte trước khi băm. Thứ tự cố định theo vai trò (khởi xướng trước), **không** sắp xếp.

```
commitment = hex(SHA-256(b"teleport-commit-v1" ‖ fp_khởi_xướng ‖ n_A))
digest     = SHA-256(b"teleport-pair-v1" ‖ fp_khởi_xướng ‖ fp_trả_lời ‖ n_A ‖ n_B)
mã         = int.from_bytes(digest[0:4], "big") mod 1 000 000, viết đủ 6 chữ số (có số 0 đầu)
```

Mã hiển thị thành hai nhóm 3 chữ số, ví dụ `048 213`. Hai fingerprint giống nhau (một máy tự ghép với chính nó) thì không tính mã.

### 5.3. Chống làm phiền

Kết nối ghép đôi không cần chứng chỉ, nên bất kỳ máy nào trong mạng cũng mở được. Bên trả lời:

- Chỉ chạy **một phiên ghép đôi** tại một thời điểm; yêu cầu khác trong lúc đó nhận `pair_abort{busy}`. Kết nối ẩn danh đã bắt tay xong mà chưa gửi `pair_request`: tối đa 1 mỗi IP và 8 tổng cộng; quá thì đóng ngay sau bắt tay, không gửi `hello` (mục 2.3). Trong lúc bắt tay, kết nối thuộc giới hạn kết nối đang mở đầu (mục 2.3). Thời gian chờ ở mục 2.3.
- **Chờ tăng dần theo từng địa chỉ IP:** một phiên từ một IP kết thúc mà không ghép đôi được (người dùng từ chối, hết giờ, `invalid`, trùng danh tính, mất kết nối, bên kia tự hủy) → IP đó nhận `pair_abort{busy}` trong 10 s, rồi 20 s, 40 s… tối đa 10 phút; ghép đôi thành công thì xóa đếm. IP khác không bị ảnh hưởng. Chi tiết ✅ (`teleport.core.pairing.PairingGate`):
  - Chỉ tính phiên mà máy này là **bên trả lời**. Phiên do node tự hủy (tắt node, Ctrl+C, nhường khi hai bên cùng chủ động) không tính (hủy sau khi đã hết giờ thì tính là hết giờ).
  - `pair_abort{busy}` gửi trong lúc IP đang bị chờ **không** kéo dài thời gian chờ.
  - IP yên lặng 10 phút kể từ khi hết thời gian chờ thì được quên (đếm lại từ đầu). Theo dõi tối đa 1024 IP; quá thì quên IP có thời gian chờ hết sớm nhất. Bộ đếm chỉ nằm trong bộ nhớ, mất khi khởi động lại node.
- **Hai bên cùng chủ động ghép đôi với nhau** (máy này đang có phiên khởi xướng tới **một trong các địa chỉ** máy này đang thử cho máy đó, tức các địa chỉ mDNS quảng bá hoặc IP nhập tay, và yêu cầu tới từ chính địa chỉ đó): bên có `device_id` **lớn hơn** hủy phiên khởi xướng của mình và trả lời phiên của bên kia; bên nhỏ hơn trả `pair_abort{busy}`. Luật này xét trước việc node có đang nhận ghép đôi không (gạch cuối) và trước thời gian chờ của IP. Phiên khởi xướng bị nhường gửi `pair_abort{busy}` nếu đã gửi `pair_request`, chưa gửi thì chỉ đóng. `device_id` trên kết nối ẩn danh chưa được xác thực, nên cần IP khớp; kẻ giả mạo vẫn có thể làm phiên tự hủy (chỉ gây phiền, không ghép đôi được). Bên nhường trả kết quả của phiên trả lời thay thế cho người dùng (lệnh `teleport pair` vẫn có đúng một kết quả). Giới hạn: máy kia gửi yêu cầu từ một địa chỉ không nằm trong danh sách đó (ví dụ địa chỉ của card ảo bị lọc khỏi bản quảng bá) thì luật này không áp được, và hai bên cùng nhận `busy` rồi thử lại.
- **Giới hạn của các biện pháp trên:** đây là chống làm phiền ở mức tốt nhất có thể, không chống được tấn công từ chối dịch vụ trong LAN. Một máy đổi IP liên tục (IPv6 tạm thời, xin DHCP mới) vẫn có thể làm hiện hộp thoại nhiều lần hoặc giữ phiên ghép đôi. Ngay cả một máy ẩn danh duy nhất, gửi và đọc thật chậm (sát hạn ở mỗi bước), cũng giữ được phiên duy nhất tới khoảng 10 s cho mỗi bước trao message cộng 60 s chờ người dùng xác nhận (cỡ 70 s hoặc hơn chút); trong lúc đó mọi yêu cầu khác nhận `pair_abort{busy}`. Việc này chỉ ảnh hưởng khi node đang nhận yêu cầu ghép đôi (gạch cuối; với CLI là trong lúc `teleport listen` đang chạy). Ghép đôi vẫn an toàn vì cần người dùng xác nhận mã ở cả hai máy.
- Khi nào node nhận yêu cầu ghép đôi: giao diện luôn nhận (hiện hộp thoại, mvp F3) 📝; CLI chỉ nhận khi đang chạy `teleport listen` ✅. Ngoài các trường hợp đó → `pair_abort{busy}`.

### 5.4. Test vector

| Đại lượng | Giá trị (hex) |
|---|---|
| `fp_khởi_xướng` | `5ee5afc087437e85ae8430c657e73fe6fd7c87fe5064b77eb6537a133ee3b290` |
| `fp_trả_lời` | `2846c2e746ec2e9bb0dbcfad4873a0e81d82f256f89fb6b0e2bca91890b498f3` |
| `n_A` | `000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f` |
| `n_B` | `202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f` |
| `commitment` | `3e4caf4ec8e56d982e218a573ac0c4322e7d0241022faaf2295ea6887211c65b` |
| `digest` | `4a58a291a77f5de3163a28b02a8ca6c465aac3805851f1f3f2477c87232d3e64` |
| **mã** | `322769` (hiển thị `322 769`) |

Hai fingerprint ở trên là SHA-256 của chuỗi ASCII `teleport test vector A` và `teleport test vector B`. Đổi vai trò (đảo hai fingerprint) cho mã `488828`; đảo hai nonce cho mã `682735`.

### 5.5. Lưu trữ

Mỗi bên lưu `device_id`, tên, fingerprint và chứng chỉ PEM của bên kia vào `trusted.json` (định dạng ở [architecture.md](architecture.md) mục 4). Ghép đôi lại cùng `device_id` và cùng fingerprint thì mục cũ được ghi đè (cập nhật tên). Thay đổi danh sách tin cậy có hiệu lực ngay với kết nối mới (mục 7).

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
- **Cùng `device_id`, khác fingerprint** (mDNS, `hello` hay `pair_request` báo `device_id` của một máy tin cậy nhưng chứng chỉ khác): không có lý do hợp lệ, vì danh tính mới luôn có `device_id` mới (mục 1). Chặn kết nối, không cho ghép đôi đè (mục 5.1), cảnh báo có thể có giả mạo. Phần ghép đôi ✅ (từ chối trước khi hiện mã); phần kết nối điều khiển và mDNS ✅ (`IdentityMismatch` → cảnh báo một lần, không tự thử lại theo lịch). Muốn ghép đôi lại thì người dùng phải hủy ghép đôi mục cũ trước, và khi đó cài đặt theo thiết bị cũ cũng bị xóa, nên máy giả không thừa hưởng “Luôn nhận” hay “Đồng bộ clipboard”.
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
  - Cận trên, khi không có hàng đợi resolve: ≈ 60 s (TTL, bản ghi còn trong cache) + 2 × (15 s chu kỳ + 5 s nhịp hẹn giờ + 3 s resolve) ≈ 106 s < 2 phút (mvp N1). Đo trên 2 máy thật ở Giai đoạn 1.
  - Giới hạn đã biết: máy thật mà 2 lần resolve đầu đều mất gói (Wi-Fi xấu) bị bỏ như bản ghi chưa từng hợp lệ, và chỉ hiện lại khi zeroconf nạp lại SRV/TXT/A (máy đó quảng bá lại, hoặc bên duyệt làm mới PTR ở mốc 75 % TTL, có thể tới ~14 phút). Kiểm kịch bản này khi đo N1.
- Bên nhận theo dõi tối đa **256** dịch vụ cùng lúc; dịch vụ của chính máy này bị bỏ qua.
  - **Ưu tiên thiết bị đã ghép đôi:** dịch vụ có `device_id` nằm trong `trusted.json` luôn được theo dõi và **không tính vào 256** (số này bị chặn bởi số thiết bị tin cậy). Một dịch vụ đang theo dõi trở thành tin cậy (vừa ghép đôi) thì nhả một chỗ trong 256, nên mục cũ nhất của danh sách tràn bên dưới được nhận ngay. Thiết bị thôi tin cậy mà đang được theo dõi thì vẫn giữ, nhưng từ đó tính vào 256.
  - **Danh sách tràn:** dịch vụ mới khi đã đủ không bị quên (zeroconf chỉ báo một dịch vụ mới một lần cho mỗi PTR) mà được ghi vào danh sách chờ, tối đa **1024** `device_id`, thứ tự vào trước ra trước, đầy thì bỏ mục cũ nhất. Mỗi khi một dịch vụ bị ngừng theo dõi và có chỗ trống, mục cũ nhất trong danh sách được nhận và resolve ngay. Dịch vụ cũ không bao giờ bị đẩy ra để nhường chỗ. Mục chờ đã cũ (máy đã đi mà không gửi goodbye) được nhận rồi bị bỏ sau 2 lần resolve thất bại như dịch vụ chưa từng hợp lệ.
- ⚠️ **mDNS không xác thực.** Bất kỳ máy nào trong LAN cũng quảng bá được `<device_id>` của máy khác, với tên, địa chỉ, cổng tùy ý. Kết quả khám phá chỉ là gợi ý để biết kết nối tới đâu; danh tính chỉ được tin qua fingerprint chứng chỉ khi bắt tay TLS (mục 2.2, 7). Không bao giờ dùng `name` hay `device_id` từ mDNS để quyết định tin cậy.
  - Vì vậy có ba kiểu từ chối dịch vụ đã biết mà mDNS không chặn được:
    1. **Lũ PTR** lấp đầy danh sách theo dõi (256 dịch vụ và 1024 mục chờ): máy *chưa ghép đôi* mới có thể không hiện. Máy đã ghép đôi vẫn được theo dõi và được resolve trước hàng chờ, nên vẫn hiện sau vài giây.
    2. **Goodbye giả** (bản ghi TTL 0 mạo danh) xóa hoặc ẩn một thiết bị khỏi danh sách.
    3. **Đè bản ghi SRV/A** làm kết nối đi sai địa chỉ hoặc cổng. Kiểm fingerprint khi bắt tay TLS chặn việc mạo danh, nên kẻ tấn công chỉ làm kết nối thất bại.
  - Đường dự phòng cho cả ba là **nhập IP thủ công** (mục 2.1).

## 9. Phiên bản giao thức

- Hai bên so `hello.protocol`: khác nhau thì đóng kết nối và báo cập nhật app (mvp mục 8).
- Phiên bản 1 là dự thảo tới bản phát hành đầu tiên có mạng (`v0.1.0-alpha`). Từ bản đó, **mọi thay đổi không tương thích đều tăng số phiên bản**, kể cả trước `v1.0.0`, để người dùng thử các bản alpha/beta khác nhau nhận được thông báo rõ ràng thay vì lỗi khó hiểu.
- Thêm message hay trường tùy chọn mới mà bản cũ bỏ qua được (mục 3.1, 3.3) thì không phải tăng số.
