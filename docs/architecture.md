# Kiến trúc

Tài liệu này là **nguồn duy nhất** về trạng thái từng module. README chỉ nêu tầm nhìn và lộ trình phiên bản.

## 1. Phân tầng

```
        ┌──────────────────────┐   ┌──────────────────────┐
        │   ui  (PySide6)      │   │   cli                │   lớp mỏng: hiển thị, nhận lệnh
        └──────────┬───────────┘   └──────────┬───────────┘
                   └────────────┬─────────────┘
                                ▼
        ┌─────────────────────────────────────────────────┐
        │   app — dịch vụ ứng dụng                          │   Node, DeviceRegistry, PairingService,
        │                                                 │   TransferManager, ClipboardSync
        └────────────────────────┬────────────────────────┘
                                 ▼
        ┌─────────────────────────────────────────────────┐
        │   net — I/O mạng                                 │   TLS, đọc/ghi message, khám phá mDNS,
        │                                                 │   mở/nhận kết nối
        └────────────────────────┬────────────────────────┘
                                 ▼
        ┌─────────────────────────────────────────────────┐
        │   trust — niềm tin                               │   danh tính máy này, kho thiết bị tin cậy
        └────────────────────────┬────────────────────────┘
                                 ▼
        ┌─────────────────────────────────────────────────┐
        │   core — giao thức thuần (sans-IO)               │   framing, message, Merkle, bitfield,
        │                                                 │   máy trạng thái truyền file, mã ghép đôi
        └─────────────────────────────────────────────────┘
   paths.py — thư mục cấu hình theo hệ điều hành; tầng nào cũng dùng được (trừ core)
```

**Quy tắc**, được `tests/test_architecture.py` kiểm tra tự động:
1. Mỗi tầng chỉ import các tầng **bên dưới**. `ui` và `cli` không import lẫn nhau.
2. `core` là **sans-IO**: không import `asyncio`, `socket`, `ssl`, `threading`, `zeroconf`, `PySide6`, `teleport.paths`, hay bất kỳ tầng nào khác.
3. Mọi module/gói ở cấp `teleport.*` phải thuộc một tầng. Tạo gói mới thì phải xếp nó vào sơ đồ.

### Vì sao chia như vậy

- **core sans-IO:** logic khó nhất là chọn khối nào gửi lại, xử lý `resume`/`corrupt`, quyết định hủy khi file nguồn đổi. Phần này được viết thành **máy trạng thái thuần**: nhận sự kiện (message tới, khối tới), trả về hành động (gửi message, ghi khối, báo lỗi). Khi đó test ngẫu nhiên hàng nghìn kịch bản ngắt kết nối bằng `hypothesis` chạy trong vài mili giây, không cần socket.
- **trust tách khỏi net:** danh tính và danh sách thiết bị tin cậy là mật mã cộng lưu trữ, không phải mạng. Tách ra thì giữ chúng nhỏ và dễ kiểm toán.
- **net chỉ là đường ống:** `net` đọc bytes, đưa cho `core` xử lý, rồi thực hiện hành động `core` trả về. Không có quyết định nghiệp vụ nào trong `net`.
- **app là nơi điều phối duy nhất:** CLI và giao diện gọi cùng các dịch vụ, nên không viết logic hai lần.
- **clipboard là tính năng, không phải tầng:** `ClipboardSync` thuộc `app`. Nó đọc/ghi clipboard qua một interface `ClipboardBackend`; `ui` cung cấp bản dùng Qt, test dùng bản giả. Nhờ vậy CLI không phải kéo theo Qt.

## 2. Mô hình tiến trình

Mỗi thư mục cấu hình (tức mỗi danh tính) chỉ có **một node** chạy tại một thời điểm.

| Giai đoạn | Cách làm |
|---|---|
| `v0.1`–`v0.2` (chỉ có CLI) | Mỗi lệnh CLI tự chạy một node. Node giữ **file khóa** `<config_dir>/node.lock`; tiến trình thứ hai cùng thư mục cấu hình sẽ báo “Teleport đang chạy” rồi thoát. |
| Từ `v0.3` (có giao diện) | Giao diện chạy node nền ở khay hệ thống. CLI phát hiện node đang chạy thì gửi lệnh cho nó qua **IPC cục bộ** (socket trên `127.0.0.1` kèm token trong thư mục cấu hình), không tự chạy node thứ hai. |

Chạy nhiều node trên cùng một máy để phát triển/test: cho mỗi node một thư mục cấu hình riêng bằng `TELEPORT_HOME` hoặc `--config-dir`.

## 3. Bản đồ module

✅ đã có · ⏳ giai đoạn tiếp theo · ⬜ chưa làm

| Tầng | Module | Trách nhiệm | Trạng thái |
|---|---|---|---|
| — | `paths.py` | Thư mục cấu hình theo hệ điều hành; `TELEPORT_HOME` ghi đè | ✅ |
| core | `framing.py` | Mã hóa/giải mã khung `[uint32 độ dài][JSON]` | ✅ |
| core | `messages.py` | Kiểu và kiểm tra hợp lệ từng message (`hello`, `pair_*`) | ✅ |
| core | `pairing_code.py` | Cam kết nonce và mã ghép đôi 6 số từ hai fingerprint + hai nonce | ✅ |
| core | `blocks.py` | Khung dữ liệu nhị phân, chia khối | ⬜ |
| core | `merkle.py`, `bitfield.py` | Cây băm SHA-256; bảng khối đã nhận | ⬜ |
| core | `transfer.py` | Máy trạng thái gửi/nhận, nối lại, gửi lại khối lỗi | ⬜ |
| trust | `identity.py` | Khóa ECDSA P-256, chứng chỉ tự ký, fingerprint; kiểm tra chứng chỉ của máy khác (`PeerCert`) | ✅ |
| trust | `store.py` | `trusted.json`: thiết bị đã ghép đôi | ✅ |
| net | `tls.py` | `SSLContext` TLS 1.3, ghim fingerprint, mTLS | ✅ |
| net | `stream.py` | Đọc/ghi message trên `asyncio` stream | ✅ |
| net | `discovery.py` | Quảng bá & tìm `_teleport._tcp.local.` (zeroconf) | ⏳ |
| net | `connection.py` | Mở/nhận kết nối, trao đổi `hello` | ⏳ |
| net | `lock.py` | File khóa `node.lock`: một node cho mỗi thư mục cấu hình | ✅ |
| app | `node.py` | Ghép các thành phần lại: lắng nghe, khám phá, điều phối | ⏳ |
| app | `pairing.py`, `transfers.py`, `clipboard.py` | Dịch vụ ghép đôi, quản lý lượt truyền, đồng bộ clipboard | ⬜ |
| cli | `cli.py`, `__main__.py` | Lệnh `teleport` / `python -m teleport` | ✅ (`identity`) |
| ui | `ui/` | Radar, transfer, ghép đôi, khay hệ thống | ⬜ |

Module chỉ được tạo khi có code thật, không tạo file rỗng trước.

## 4. Dữ liệu trên đĩa

| Đường dẫn | Nội dung |
|---|---|
| `<config_dir>/device.crt`, `device.key` | Danh tính thiết bị, tạo ở lần chạy đầu ✅ |
| `<config_dir>/trusted.json` | Chứng chỉ + tên các thiết bị đã ghép đôi ✅ |
| `<config_dir>/node.lock` | Khóa: chỉ một node mỗi thư mục cấu hình ✅ |
| `<config_dir>/settings.json` | Cài đặt người dùng ⬜ |
| `<thư mục nhận>/<tên>.part` | File đang nhận ⬜ |
| `<thư mục nhận>/<tên>.part.state` | Trạng thái nối lại: manifest, bitfield, hash ⬜ |

`config_dir` mặc định: Windows `%APPDATA%\Teleport`, macOS `~/Library/Application Support/Teleport`, Linux `$XDG_CONFIG_HOME/teleport` (hoặc `~/.config/teleport`).

### `trusted.json`

Do `teleport.trust.store` đọc/ghi.

```json
{
  "version": 1,
  "devices": [
    {
      "device_id": "3f2a…(32 ký tự hex thường)",
      "name": "PC-Nha",
      "fingerprint": "ab12…(64 ký tự hex thường)",
      "cert": "-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----\n",
      "paired_at": "2026-10-20T08:15:00Z"
    }
  ]
}
```

- **Ghi:** UTF-8, `indent=2`, giữ nguyên ký tự Unicode; các mục sắp theo tên (không phân biệt hoa thường) rồi `device_id`; `paired_at` theo UTC, dạng `YYYY-MM-DDTHH:MM:SSZ`. Ghi nguyên tử: `trusted.json.tmp` → fsync → `os.replace` (tối đa 5 lần thử, cách nhau 50 ms, nếu file đích đang bị tiến trình khác mở). Trên Linux/macOS quyền file là `0o600` và thư mục cha được fsync sau khi đổi tên. Thư mục cấu hình được tạo nếu chưa có. Trường lạ không được giữ khi ghi lại.
- **Đọc:** chấp nhận BOM UTF-8. Chưa có file → kho rỗng, không tạo file. Mỗi mục được kiểm lại: phân tích lại chứng chỉ (như khi ghép đôi, xem [protocol.md](protocol.md) mục 5), `fingerprint` = SHA-256 DER, `device_id` khớp CN, `name` hợp lệ, `paired_at` là ISO 8601 có múi giờ. Trường lạ bị bỏ qua.
- **Lỗi:** JSON hỏng, thiếu/sai trường, `version` khác số nguyên 1 (lớn hơn là bản mới hơn), trùng `device_id` hoặc trùng `fingerprint` → **cả file** bị từ chối (`TrustStoreError`). File hỏng **không bao giờ** bị tự xóa, sửa hay tạo lại.
- Ghép đôi lại cùng `device_id` hoặc cùng `fingerprint` thì mục cũ bị thay.

### `node.lock`

Do `teleport.net.lock.NodeLock` giữ. Khóa là khóa của hệ điều hành trên file (Windows: `msvcrt.locking` byte 0; Linux/macOS: `flock`), không phải sự tồn tại của file:

- Lấy khóa không chờ. Đã có node khác giữ (kể cả trong cùng tiến trình) → `NodeLockedError` (“Teleport đang chạy với thư mục cấu hình này”), lệnh CLI thoát với mã ≠ 0.
- Tiến trình chết, kể cả bị kill, thì hệ điều hành tự nhả khóa: không có khóa treo phải xóa tay.
- File không chứa gì (không ghi PID) và được giữ lại sau khi nhả khóa.

## 5. Bất đồng bộ

Toàn bộ mạng chạy trên **một event loop `asyncio`**; giao diện Qt dùng chung event loop đó qua `qasync`. Việc chặn (ghi đĩa lớn, băm) được đẩy sang thread bằng `asyncio.to_thread`, để event loop không bao giờ bị đứng.

## 6. Kiểm thử

- `tests/` chia thư mục theo tầng: `core/`, `trust/`, `net/`… Test ở thư mục gốc `tests/` dành cho `paths`, `cli` và quy tắc phân tầng.
- Test cần socket thật được đánh dấu `@pytest.mark.integration`. Chạy nhanh phần còn lại bằng `pytest -m "not integration"`.

Giao thức trên dây: xem [protocol.md](protocol.md).
