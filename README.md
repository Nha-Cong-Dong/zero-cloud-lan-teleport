# Teleport — Zero-Cloud LAN Teleport

**Chuyển file nặng hàng chục GB và đồng bộ clipboard giữa các máy tính trong cùng mạng Wi-Fi/LAN.
Không cloud. Không tài khoản. Rớt mạng vẫn gửi tiếp.**

[![CI](https://github.com/Nha-Cong-Dong/zero-cloud-lan-teleport/actions/workflows/ci.yml/badge.svg)](https://github.com/Nha-Cong-Dong/zero-cloud-lan-teleport/actions/workflows/ci.yml)

> 🚧 **Đang phát triển**, chưa có bản phát hành. Lịch sử thay đổi xem ở [docs/CHANGELOG.md](docs/CHANGELOG.md).

---

## Vì sao có Teleport?

Hai máy tính đặt cạnh nhau, dùng chung một cục Wi-Fi, vậy mà muốn chuyển một file 5GB vẫn phải:

- **Gửi qua Zalo/Messenger:** ảnh bị nén, file lớn bị chặn, phải đăng nhập tài khoản cá nhân lên máy lạ.
- **Tải lên Google Drive rồi tải về:** tốn gấp đôi thời gian và băng thông, dù hai máy chỉ cách nhau 20cm.
- **Đi tìm USB:** cắm, copy, rút, cắm sang máy kia.

Teleport gửi file **thẳng từ máy này sang máy kia** qua mạng nội bộ, với tốc độ tối đa của mạng.

## Tính năng

| | Tính năng | Mô tả |
|---|---|---|
| 📡 | **Radar tự tìm thiết bị** | Mở app là thấy các máy khác trong cùng mạng, không cần gõ IP |
| 📦 | **Gửi file cực lớn** | Kéo thả file vào thiết bị. Dữ liệu được ghi thẳng ra đĩa nên RAM không tăng theo kích thước file |
| 🔁 | **Tự nối lại** | Rớt Wi-Fi ở 85% thì khi có mạng lại chỉ gửi tiếp 15% còn thiếu, kể cả khi đã tắt app |
| 🧩 | **Kiểm tra toàn vẹn** | Mỗi khối 1 MiB được kiểm tra bằng SHA-256; cả file được đối chiếu bằng cây băm Merkle |
| 📋 | **Đồng bộ clipboard** | Ctrl+C văn bản hoặc ảnh chụp màn hình trên máy này, Ctrl+V trên máy kia |
| 🔒 | **Bảo mật** | Mã hóa TLS 1.3; ghép đôi bằng mã xác minh 6 số để chống giả mạo |
| 🕶️ | **Riêng tư** | Không tài khoản, không telemetry, không gửi dữ liệu ra Internet |

**Nền tảng:** Windows 10/11, macOS 12+, Ubuntu 22.04+.

## Cách hoạt động

```
   Máy A                                              Máy B
┌──────────┐  1. Tìm nhau qua mDNS (multicast LAN)  ┌──────────┐
│ Teleport │ ◄────────────────────────────────────► │ Teleport │
│          │  2. Ghép đôi lần đầu: so mã 6 số        │          │
│          │  3. Kết nối TLS 1.3, ghim chứng chỉ    │          │
│  file ───┼──► 4. Gửi theo khối 1 MiB + SHA-256 ──►┼──► đĩa   │
└──────────┘     (backpressure: không tràn RAM)     └──────────┘
          Không máy chủ trung gian · Không Internet
```

- **Khám phá:** mỗi máy quảng bá dịch vụ `_teleport._tcp.local.` qua mDNS.
- **Danh tính:** mỗi máy tự tạo một chứng chỉ riêng. Ghép đôi là bước hai máy xác nhận và lưu lại chứng chỉ của nhau.
- **Truyền tải:** kênh điều khiển dùng message `[4 byte độ dài][JSON]`; kênh dữ liệu dùng khung nhị phân gọn. Bên gửi tự chờ khi mạng hoặc ổ đĩa bên nhận chậm hơn.
- **Nối lại:** bên nhận lưu bảng các khối đã nhận (bitfield). Kết nối lại thì chỉ những khối còn thiếu được gửi tiếp.

## Lộ trình

| Phiên bản | Nội dung | Trạng thái |
|---|---|---|
| — | Khung dự án, CI, mã hóa TLS và danh tính thiết bị | ✅ Xong |
| — | Khám phá thiết bị, ghép đôi | ⏳ Đang làm |
| `v0.1.0-alpha` | Gửi file lớn qua dòng lệnh | |
| `v0.2.0-alpha` | Kiểm tra toàn vẹn, tự nối lại | |
| `v0.3.0-beta` | Giao diện radar, khay hệ thống, Tiếng Việt / English | |
| `v0.4.0-beta` | Đồng bộ clipboard | |
| `v1.0.0` | Bộ cài cho Windows, macOS, Linux | |

## Dành cho nhà phát triển

Cần **Python 3.12+**.

```powershell
git clone git@github.com:Nha-Cong-Dong/zero-cloud-lan-teleport.git
cd zero-cloud-lan-teleport
python -m venv .venv
.\.venv\Scripts\Activate.ps1      # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest
teleport identity                  # tạo / xem danh tính thiết bị của máy này
```

Nếu PowerShell chặn `Activate.ps1`, chạy `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` một lần.

Muốn chạy nhiều node trên cùng một máy, cho mỗi node một thư mục cấu hình riêng bằng biến môi trường `TELEPORT_HOME` hoặc tùy chọn `--config-dir`.

### Cấu trúc mã nguồn

```
src/teleport/
  core/          # giao thức thuần, không phụ thuộc mạng
  net/           # danh tính, TLS, (sắp tới) khám phá, ghép đôi, gửi/nhận
  paths.py       # thư mục cấu hình theo hệ điều hành
  cli.py         # lệnh `teleport`
tests/           # pytest, gồm cả test kiểm tra quy tắc phân tầng
docs/            # toàn bộ tài liệu, mục lục ở docs/README.md
```

Tầng dưới không bao giờ import tầng trên (`core ← net ← clipboard ← ui/cli`). Chi tiết ở [docs/architecture.md](docs/architecture.md).

### Đóng góp

Quy trình nhánh, commit, kiểm tra trước khi mở Pull Request và phát hành: xem [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md).

### Bảo mật

Báo lỗ hổng theo hướng dẫn ở [docs/SECURITY.md](docs/SECURITY.md), không mở issue công khai.

## Giấy phép

Chưa chọn giấy phép. Cho tới khi có giấy phép chính thức, mọi quyền đối với mã nguồn được bảo lưu.
