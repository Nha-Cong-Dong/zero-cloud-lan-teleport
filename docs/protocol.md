# Giao thức Teleport

**Phiên bản giao thức: 1 (dự thảo).** Phiên bản giao thức đánh số riêng, độc lập với phiên bản app. Trước `v1.0.0` của app, giao thức có thể thay đổi không tương thích.

Trạng thái từng phần: ✅ đã cài đặt · 📝 dự thảo, chưa cài đặt.

## 1. Danh tính thiết bị ✅

- Mỗi thiết bị có một khóa **ECDSA P-256** và một **chứng chỉ X.509 tự ký** (chữ ký SHA-256, hiệu lực 10 năm).
- `CN` của chứng chỉ là `teleport-<device_id>`, trong đó `device_id` là 32 ký tự hex (UUID4).
- **Fingerprint** = SHA-256 của chứng chỉ ở dạng DER, viết dưới dạng 64 ký tự hex. Đây là định danh duy nhất được tin cậy; tên thiết bị chỉ để hiển thị.
- **Pin ngắn** = 8 byte đầu của fingerprint, hiển thị dạng `AB:CD:EF:…`.

## 2. Tầng vận chuyển ✅

- TCP + **TLS 1.3**. Phiên bản thấp hơn bị từ chối.
- **Client kiểm tra server:** không dùng CA hay hostname. Sau khi bắt tay, client so fingerprint chứng chỉ server với giá trị đã ghim.
- **Server kiểm tra client (mTLS) ✅:** server nạp chứng chỉ của các thiết bị tin cậy làm trust anchor, bật `CERT_REQUIRED` và kiểm tra X.509 nghiêm ngặt. Client lạ, hoặc client không gửi chứng chỉ, bị từ chối ngay trong lúc bắt tay. Kiểm chứng bởi `tests/net/test_tls.py` (OpenSSL 3.0).
- Với TLS 1.3, client bị từ chối vẫn thấy kết nối mở thành công; lỗi chỉ lộ ra ở lần đọc đầu tiên. Vì vậy client chỉ coi là đã kết nối khi **đọc được `hello` của server**.
- 📝 Kết nối ghép đôi (khi hai bên chưa biết nhau) dùng một listener riêng không yêu cầu chứng chỉ client. Client ghép đôi (bên khởi xướng) **không gửi chứng chỉ** trong bắt tay TLS; nó gửi chứng chỉ PEM của mình trong message `pair_request`. Danh tính được xác thực ở tầng ứng dụng bằng mã 6 số (mục 5).

## 3. Kênh điều khiển ✅ (khung) · ✅/📝 (message)

Mỗi message:

```
┌──────────────────────┬────────────────────────────────┐
│ độ dài: uint32 (BE)  │ JSON object, mã hóa UTF-8      │
└──────────────────────┴────────────────────────────────┘
```

- Độ dài tối đa: **16 MiB**. Header lớn hơn thì đóng kết nối ngay, không đọc thân.
- Thân phải là một **JSON object**. Mảng, số hay chuỗi ở cấp ngoài cùng đều bị coi là lỗi.
- JSON có số nguyên quá dài (hơn 4300 chữ số) hoặc lồng quá sâu cũng là lỗi khung.
- Đóng kết nối đúng ở ranh giới giữa hai message là kết thúc bình thường. Đóng giữa chừng một message là lỗi.
- Mọi message có trường `"type"`. Trường không biết thì bỏ qua (để mở rộng về sau); thiếu trường bắt buộc thì là lỗi.

### Các loại message

`hello` và các message `pair_*` đã có kiểu và kiểm tra hợp lệ (`teleport.core.messages`) ✅; chưa được gửi trên mạng 📝. Các message còn lại là dự thảo 📝.

| `type` | Hướng | Trường |
|---|---|---|
| `hello` | ↔ | `protocol` (int ≥ 1), `device_id`, `name`, `app_version` |
| `pair_request` | khởi xướng → trả lời | `cert` (PEM), `commitment` (64 hex) — chỉ trên kết nối ghép đôi |
| `pair_challenge` | trả lời → khởi xướng | `nonce` (64 hex = 32 byte) — chỉ trên kết nối ghép đôi |
| `pair_reveal` | khởi xướng → trả lời | `nonce` (64 hex = 32 byte) — chỉ trên kết nối ghép đôi |
| `pair_confirm` | ↔ | (không có trường) — chỉ trên kết nối ghép đôi |
| `pair_abort` | ↔ | `reason` — chỉ trên kết nối ghép đôi |
| `offer` | gửi → nhận | `transfer_id` (UUID hex), `block_size`, `files`: `[{name, size, mtime}]` |
| `accept` | nhận → gửi | `transfer_id`, `files`: chỉ số các file được nhận |
| `reject` | nhận → gửi | `transfer_id`, `reason`: `"user"` · `"no_space"` · `"busy"` |
| `resume` | nhận → gửi | `transfer_id`, `file_index`, `bitfield` (base64) |
| `block_hashes` | gửi → nhận | `transfer_id`, `file_index`, `start_block`, `hashes`: danh sách hex SHA-256 |
| `complete` | gửi → nhận | `transfer_id`, `file_index`, `merkle_root` (hex) |
| `verified` | nhận → gửi | `transfer_id`, `file_index` |
| `corrupt` | nhận → gửi | `transfer_id`, `file_index`, `blocks`: danh sách chỉ số khối lỗi |
| `cancel` | ↔ | `transfer_id` |
| `clipboard` | ↔ | `kind`: `"text"` · `"image/png"`, `sha256`, `data` (văn bản hoặc base64, ≤ 10 MB) |
| `ping` / `pong` | ↔ | `t` (timestamp) |

Hai bên gửi `hello` ngay sau khi bắt tay TLS. Khác `protocol` thì đóng kết nối và báo người dùng cập nhật app.

### Quy tắc trường ✅

Mọi message đến từ máy khác đều không được tin: sai một trường là cả message bị từ chối.

- **`protocol`**: số nguyên JSON ≥ 1. Không nhận `true`/`false`, không nhận số thực (kể cả `1.0`), không nhận chuỗi.
- **`device_id`**: đúng 32 ký tự hex thường `[0-9a-f]`.
- **`name`** (1–64 code point) và **`app_version`** (1–32 code point):
  - không có khoảng trắng ở đầu hoặc cuối (kể cả khoảng trắng Unicode như NBSP, U+3000);
  - không chứa ký tự loại `Cc` (điều khiển), `Cs` (surrogate lẻ), U+2028, U+2029, U+200B, U+FEFF, U+2060, hay ký tự điều khiển hướng chữ U+200E, U+200F, U+202A–U+202E, U+2066–U+2069;
  - được phép: chữ có dấu, emoji, kể cả emoji ghép bằng ZWJ U+200D. Giá trị giữ nguyên, không chuẩn hóa NFC.
- **`cert`**: chuỗi ≤ 8192 ký tự, bắt đầu đúng bằng `-----BEGIN CERTIFICATE-----`. Bên nhận còn phân tích và kiểm tra chứng chỉ (mục 5).
- **`commitment`**, **`nonce`**: đúng 64 ký tự hex thường (32 byte).
- **`reason`** (`pair_abort`): 1–32 ký tự `[a-z_]`. Giá trị chuẩn: `user` (người dùng từ chối), `busy` (đang có phiên ghép đôi khác), `timeout` (hết giờ), `invalid` (message hoặc cam kết sai). Giá trị lạ đúng dạng vẫn được nhận, để bản mới thêm lý do được.
- Kiểm tra hex, chữ số và `reason` chỉ dùng lớp ký tự ASCII trên **toàn bộ** chuỗi: `"user\n"` hay chữ số toàn chiều rộng đều sai.
- `type` là chuỗi nhưng chưa biết thì bên nhận phân biệt được với message hỏng (để bản cũ bỏ qua message mới).
- Message `pair_*` chỉ hợp lệ trên kết nối ghép đôi; trên kết nối mTLS thì là lỗi giao thức.

## 4. Kênh dữ liệu 📝

Mỗi file (hoặc mỗi lần nối lại) dùng một kết nối TLS riêng. Mở đầu kết nối:

```
transfer_id: 16 byte (UUID) │ file_index: uint32 (BE)
```

Sau đó là các khung khối:

```
┌──────────────┬────────────────────┬──────────────────┬──────────────┐
│ type: uint8  │ block_index: uint32│ length: uint32   │ payload      │
│ 0x02 = khối  │ (BE)               │ (BE), ≤ 1 MiB    │ dữ liệu thô  │
└──────────────┴────────────────────┴──────────────────┴──────────────┘
```

- Kích thước khối mặc định **1 MiB**. Khối cuối có thể ngắn hơn.
- Khối `i` nằm ở vị trí `i × block_size` trong file.
- Bên nhận băm SHA-256 từng khối và so với `block_hashes`. Khối sai được báo trong `corrupt` và gửi lại.

## 5. Ghép đôi 📝

Ghép đôi theo kiểu *numeric comparison* của Bluetooth: hai bên trao nonce ngẫu nhiên, bên khởi xướng **cam kết** nonce của mình trước khi thấy nonce bên kia, rồi cả hai hiện cùng một mã 6 số cho người dùng so. Nếu mã chỉ tính từ hai fingerprint, kẻ đứng giữa có thể sinh khóa offline tới khi ra mã trùng; với nonce cam kết, mỗi lần thử chỉ thành công với xác suất 10⁻⁶. Công thức đã cài đặt ở `teleport.core.pairing_code` ✅; trao đổi trên mạng chưa cài đặt 📝.

### Luồng

A là bên khởi xướng (`teleport pair`), B là bên trả lời (`teleport listen`).

```
A (khởi xướng)                              B (trả lời)
TLS tới cổng ghép đôi: A không gửi chứng chỉ; B không yêu cầu chứng chỉ client
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
- Chứng chỉ của A: B lấy từ `pair_request.cert`. Về sau, mTLS buộc A chứng minh giữ khóa riêng ở mỗi kết nối.
- Chứng chỉ nhận được phải: phân tích được; ≤ 8192 ký tự PEM, chỉ ASCII, đúng một khối `CERTIFICATE`; X.509 v3; subject có đúng một CN dạng `teleport-<32 hex thường>`; issuer trùng subject và chữ ký tự ký hợp lệ; khóa ECDSA P-256; chữ ký ECDSA-SHA256; không có dấu hiệu chứng chỉ CA: từ chối `BasicConstraints CA=true`, `KeyUsage` có `keyCertSign` hoặc `cRLSign`, và mọi chứng chỉ có extension Netscape Cert Type (OID `2.16.840.1.113730.1.1`). Lý do: chứng chỉ tin cậy được nạp làm trust anchor cho mTLS, nên không được phép ký ra chứng chỉ khác. Ngày hiệu lực chưa được kiểm.
- Rớt mạng giữa hai `pair_confirm` có thể để lại ghép đôi một phía; ghép đôi lại sẽ ghi đè.

### Công thức

`‖` là nối byte. Fingerprint (64 ký tự hex) được đổi sang 32 byte trước khi băm. Thứ tự cố định theo vai trò (khởi xướng trước), **không** sắp xếp.

```
commitment = hex(SHA-256(b"teleport-commit-v1" ‖ fp_khởi_xướng ‖ n_A))
digest     = SHA-256(b"teleport-pair-v1" ‖ fp_khởi_xướng ‖ fp_trả_lời ‖ n_A ‖ n_B)
mã         = int.from_bytes(digest[0:4], "big") mod 1 000 000, viết đủ 6 chữ số (có số 0 đầu)
```

Mã hiển thị thành hai nhóm 3 chữ số, ví dụ `048 213`. Hai fingerprint giống nhau (một máy tự ghép với chính nó) thì không tính mã.

### Test vector

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

### Lưu trữ

Mỗi bên lưu `device_id`, tên, fingerprint và chứng chỉ PEM của bên kia vào `trusted.json` (định dạng ở [architecture.md](architecture.md) mục 4). Ghép đôi lại cùng `device_id` hoặc cùng fingerprint thì mục cũ bị thay.

## 6. Nối lại 📝

- Bên nhận lưu `<tên>.part.state` gồm: manifest từ `offer`, bitfield các khối đã xác minh, hash của các khối đó.
- Kết nối lại → bên nhận gửi `resume` cho từng file dở dang → bên gửi chỉ gửi các khối có bit = 0.
- File nguồn đổi `size` hoặc `mtime` so với manifest → bên gửi gửi `cancel`.
- Merkle root được tính từ danh sách hash các khối (lá) theo cây nhị phân. Lẻ nút thì nút cuối được đẩy thẳng lên tầng trên.
