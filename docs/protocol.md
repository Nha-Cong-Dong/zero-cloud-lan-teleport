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
- Không kiểm tra theo CA hay hostname. Thay vào đó, sau khi bắt tay, mỗi bên so fingerprint chứng chỉ của bên kia với giá trị đã ghim.
- 📝 Xác thực hai chiều: server nạp chứng chỉ của các thiết bị tin cậy làm CA và yêu cầu client gửi chứng chỉ (mTLS). Lần ghép đôi đầu tiên được xác thực ở tầng ứng dụng bằng mã 6 số (mục 5).

## 3. Kênh điều khiển ✅ (khung) · 📝 (message)

Mỗi message:

```
┌──────────────────────┬────────────────────────────────┐
│ độ dài: uint32 (BE)  │ JSON object, mã hóa UTF-8      │
└──────────────────────┴────────────────────────────────┘
```

- Độ dài tối đa: **16 MiB**. Header lớn hơn thì đóng kết nối ngay, không đọc thân.
- Thân phải là một **JSON object**. Mảng, số hay chuỗi ở cấp ngoài cùng đều bị coi là lỗi.
- Đóng kết nối đúng ở ranh giới giữa hai message là kết thúc bình thường. Đóng giữa chừng một message là lỗi.
- Mọi message có trường `"type"`. Trường không biết thì bỏ qua (để mở rộng về sau); thiếu trường bắt buộc thì là lỗi.

### Các loại message 📝

| `type` | Hướng | Trường |
|---|---|---|
| `hello` | ↔ | `protocol` (int), `device_id`, `name`, `app_version` |
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

1. A kết nối tới B (TLS, chưa ghim). Mỗi bên đọc fingerprint của bên kia.
2. Cả hai tính `mã = int(SHA-256(min(fp_A, fp_B) ‖ max(fp_A, fp_B))[:4]) mod 10⁶`, hiển thị 6 chữ số.
3. Người dùng xác nhận mã trùng nhau trên **cả hai** máy.
4. Mỗi bên lưu fingerprint, `device_id` và tên của bên kia vào `trusted.json`.

## 6. Nối lại 📝

- Bên nhận lưu `<tên>.part.state` gồm: manifest từ `offer`, bitfield các khối đã xác minh, hash của các khối đó.
- Kết nối lại → bên nhận gửi `resume` cho từng file dở dang → bên gửi chỉ gửi các khối có bit = 0.
- File nguồn đổi `size` hoặc `mtime` so với manifest → bên gửi gửi `cancel`.
- Merkle root được tính từ danh sách hash các khối (lá) theo cây nhị phân. Lẻ nút thì nút cuối được đẩy thẳng lên tầng trên.
