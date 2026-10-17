# Kiến trúc

## Phân tầng

```
            ┌──────────────┐   ┌──────────┐
            │  ui (PySide6) │   │   cli    │      tầng giao tiếp người dùng
            └──────┬───────┘   └────┬─────┘
                   ▼                ▼
            ┌──────────────────────────────┐
            │          clipboard           │      đồng bộ clipboard
            └──────────────┬───────────────┘
                           ▼
            ┌──────────────────────────────┐
            │             net              │      danh tính, TLS, khám phá,
            │                              │      ghép đôi, gửi / nhận
            └──────────────┬───────────────┘
                           ▼
            ┌──────────────────────────────┐
            │             core             │      giao thức thuần: framing,
            │                              │      message, Merkle, bitfield, state
            └──────────────────────────────┘
     paths.py — thư mục cấu hình theo hệ điều hành, tầng nào cũng dùng được
```

**Quy tắc:** tầng dưới không bao giờ import tầng trên. `core` không biết gì về socket; `net` không biết gì về Qt. Quy tắc này được kiểm tra tự động bởi `tests/test_architecture.py`.

Lợi ích:
- `core` test được hoàn toàn bằng dữ liệu trong bộ nhớ, không cần mạng.
- `net` chạy được không cần giao diện, nên CLI và test tích hợp dùng chung một lõi với app.
- Giao diện chỉ là một lớp mỏng phía trên; thay hoặc bỏ giao diện không ảnh hưởng lõi.

## Bản đồ module

✅ đã có · ⏳ giai đoạn tiếp theo · ⬜ chưa làm

| Module | Trách nhiệm | Trạng thái |
|---|---|---|
| `paths.py` | Thư mục cấu hình theo hệ điều hành; `TELEPORT_HOME` ghi đè | ✅ |
| `cli.py`, `__main__.py` | Lệnh `teleport` / `python -m teleport` | ✅ (`identity`) |
| `core/framing.py` | Khung message điều khiển `[uint32 độ dài][JSON]` | ✅ |
| `core/messages.py` | Định nghĩa và kiểm tra hợp lệ từng loại message | ⬜ |
| `core/protocol.py` | Khung dữ liệu nhị phân, hằng số giao thức | ⬜ |
| `core/merkle.py` | Cây băm SHA-256, Merkle root | ⬜ |
| `core/bitfield.py` | Bảng các khối đã nhận | ⬜ |
| `core/state.py` | Đọc/ghi file `.part.state` để nối lại | ⬜ |
| `net/identity.py` | Khóa ECDSA P-256, chứng chỉ tự ký, fingerprint | ✅ |
| `net/tls.py` | `SSLContext` TLS 1.3, đọc fingerprint, ghim | ✅ |
| `net/discovery.py` | Quảng bá & tìm `_teleport._tcp.local.` (zeroconf) | ⏳ |
| `net/pairing.py` | Mã xác minh 6 số, `trusted.json` | ⏳ |
| `net/connection.py` | Mở/nhận kết nối, `HELLO`, mTLS với thiết bị tin cậy | ⏳ |
| `net/sender.py`, `net/receiver.py` | Gửi/nhận theo khối, backpressure, an toàn khi nhận | ⬜ |
| `clipboard/sync.py` | Theo dõi clipboard, chống vòng lặp | ⬜ |
| `ui/` | Radar, transfer, ghép đôi, khay hệ thống | ⬜ |

Module chỉ được tạo khi có code thật, không tạo file rỗng trước.

## Dữ liệu trên đĩa

| Đường dẫn | Nội dung |
|---|---|
| `<config_dir>/device.crt`, `device.key` | Danh tính thiết bị (tạo ở lần chạy đầu) |
| `<config_dir>/trusted.json` | Fingerprint các thiết bị đã ghép đôi ⏳ |
| `<config_dir>/settings.json` | Cài đặt người dùng ⬜ |
| `<thư mục nhận>/<tên>.part` | File đang nhận ⬜ |
| `<thư mục nhận>/<tên>.part.state` | Trạng thái nối lại: manifest, bitfield, hash ⬜ |

`config_dir` mặc định: Windows `%APPDATA%\Teleport`, macOS `~/Library/Application Support/Teleport`, Linux `$XDG_CONFIG_HOME/teleport` (hoặc `~/.config/teleport`).

## Mô hình bất đồng bộ

Toàn bộ mạng chạy trên **một event loop `asyncio`**. Giao diện Qt sẽ dùng chung event loop đó qua `qasync`. Việc chặn (ghi đĩa lớn, băm) được đẩy sang thread bằng `asyncio.to_thread`, để event loop không bao giờ bị đứng.

Giao thức trên dây: xem [protocol.md](protocol.md).
