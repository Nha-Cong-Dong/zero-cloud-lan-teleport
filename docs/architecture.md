# Kiến trúc

Tài liệu này là **nguồn duy nhất** về trạng thái từng module. Trạng thái từng giai đoạn nằm ở [ke_hoach.md](ke_hoach.md) mục 5.1; thiết kế kỹ thuật tổng thể ở [ke_hoach.md](ke_hoach.md) mục 3.

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
   paths.py, lock.py — thư mục cấu hình theo hệ điều hành, khóa file; tầng nào cũng dùng được (trừ core)
```

**Quy tắc**, được `tests/test_architecture.py` kiểm tra tự động:
1. Mỗi tầng chỉ import các tầng **bên dưới**. `ui` và `cli` không import lẫn nhau.
2. `core` là **sans-IO**: không import `asyncio`, `socket`, `ssl`, `threading`, `zeroconf`, `PySide6`, `teleport.paths`, `teleport.lock`, hay bất kỳ tầng nào khác.
3. Mọi module/gói ở cấp `teleport.*` phải thuộc một tầng, hoặc là tiện ích dùng chung (`paths`, `lock`) không import tầng nào. Tạo gói mới thì phải xếp nó vào sơ đồ.

### Vì sao chia như vậy

- **core sans-IO:** logic khó nhất là chọn khối nào gửi lại, xử lý `resume`, lượt đọc lại cuối, quyết định hủy khi file nguồn đổi. Phần này được viết thành **máy trạng thái thuần**: nhận sự kiện (message tới, khối tới), trả về hành động (gửi message, ghi khối, báo lỗi). Khi đó test ngẫu nhiên hàng nghìn kịch bản ngắt kết nối bằng `hypothesis` chạy trong vài mili giây, không cần socket.
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

✅ đã có · ⏳ đang làm (giai đoạn hiện tại) · ⬜ chưa làm

| Tầng | Module | Trách nhiệm | Trạng thái |
|---|---|---|---|
| — | `paths.py` | Thư mục cấu hình theo hệ điều hành; `TELEPORT_HOME` ghi đè | ✅ |
| — | `lock.py` | Khóa file của hệ điều hành: `node.lock` (một node cho mỗi thư mục cấu hình), `identity.lock` (khi tạo danh tính) | ✅ |
| core | `framing.py` | Mã hóa/giải mã khung `[uint32 độ dài][JSON]`; giới hạn 16 MiB, riêng kết nối ghép đôi 16 KiB | ✅ |
| core | `messages.py` | Kiểu và kiểm tra hợp lệ từng message (`hello` có `os`, `pair_*`) | ✅ |
| core | `pairing_code.py` | Cam kết nonce và mã ghép đôi 6 số từ hai fingerprint + hai nonce | ✅ |
| core | `pairing.py` | Máy trạng thái ghép đôi sans-IO: thứ tự `pair_*`, hết giờ, lý do hủy, chống làm phiền | ⏳ |
| core | `blocks.py` | Khung khối nhị phân kèm SHA-256, chia khối | ⬜ |
| core | `merkle.py`, `bitfield.py` | Cây băm SHA-256; bảng khối đã nhận và đã xác minh | ⬜ |
| core | `transfer.py` | Máy trạng thái gửi/nhận, nối lại, gửi lại khối lỗi | ⬜ |
| trust | `identity.py` | Khóa ECDSA P-256, chứng chỉ tự ký, fingerprint; kiểm tra chứng chỉ của máy khác (`PeerCert`); danh tính hỏng → `IdentityError`, không tự tạo lại | ✅ |
| trust | `store.py` | `trusted.json`: thiết bị đã ghép đôi | ✅ |
| net | `tls.py` | `SSLContext` TLS 1.3; server một cổng (`CERT_OPTIONAL` + thiết bị tin cậy làm trust anchor); client so toàn bộ fingerprint | ✅ |
| net | `stream.py` | Đọc/ghi message trên `asyncio` stream | ✅ |
| net | `discovery.py` | Quảng bá & tìm `_teleport._tcp.local.` (zeroconf) | ⏳ |
| net | `connection.py` | Listener một cổng; phân loại kết nối ghép đôi / điều khiển / dữ liệu; trao `hello`; giới hạn, thời gian chờ, keepalive; thay `SSLContext` khi danh sách tin cậy đổi | ⏳ |
| app | `node.py` | Ghép các thành phần lại: lắng nghe, khám phá, giữ một kết nối điều khiển tới mỗi thiết bị tin cậy (chống trùng, nối lại), điều phối | ⏳ |
| app | `pairing.py` | Dịch vụ ghép đôi: sinh nonce, chạy máy trạng thái `core.pairing` trên kết nối thật, lưu thiết bị tin cậy, hủy ghép đôi | ⏳ |
| app | `transfers.py`, `clipboard.py` | Quản lý lượt truyền, đồng bộ clipboard | ⬜ |
| cli | `cli.py`, `__main__.py` | Lệnh `teleport` / `python -m teleport` | ✅ `identity` · ⏳ `list`, `listen`, `pair`, `unpair` |
| ui | `ui/` | Radar, lượt truyền, ghép đôi, cài đặt, khay hệ thống | ⬜ |

Module chỉ được tạo khi có code thật, không tạo file rỗng trước.

## 4. Dữ liệu trên đĩa

| Đường dẫn | Nội dung |
|---|---|
| `<config_dir>/device.crt`, `device.key` | Danh tính thiết bị, tạo ở lần chạy đầu (chi tiết bên dưới) ✅ |
| `<config_dir>/trusted.json` | Chứng chỉ + tên các thiết bị đã ghép đôi ✅ |
| `<config_dir>/node.lock` | Khóa: chỉ một node mỗi thư mục cấu hình ✅ |
| `<config_dir>/identity.lock` | Khóa trong lúc tạo danh tính, để nhiều tiến trình chạy lần đầu cùng lúc nhận cùng một danh tính ✅ |
| `<config_dir>/settings.json` | Cài đặt người dùng, gồm cổng dự phòng khi 47474 bận ([protocol.md](protocol.md) mục 2.1), và cả cài đặt theo từng thiết bị tin cậy (“Hỏi trước khi nhận”, “Đồng bộ clipboard”), theo `device_id`. Không đưa vào `trusted.json`: file đó bỏ trường lạ khi ghi và bị bản cũ từ chối nếu tăng `version` ⬜ |
| `<config_dir>/history.json` | 100 lượt truyền gần nhất (mvp F5), ghi nguyên tử ⬜ |
| `<thư mục nhận>/.teleport/<transfer_id>/transfer.json` | Trạng thái mức lượt truyền, còn sau khi khởi động lại: manifest của `offer`, thiết bị gửi (`device_id`), đã chấp nhận, file hiện tại, các file đã `verified`; ghi nguyên tử; xóa 24 giờ sau khi file cuối xong ([protocol.md](protocol.md) mục 6.3) ⬜ |
| `<thư mục nhận>/.teleport/<transfer_id>/<file_index>.part` | Dữ liệu đang nhận, ghi đúng vị trí từng khối ⬜ |
| `<thư mục nhận>/.teleport/<transfer_id>/<file_index>.state` | Trạng thái nối lại: manifest, bitfield, hash các khối đã đánh dấu; cứ 16 khối: `fsync` `.part` rồi mới ghi state nguyên tử ([protocol.md](protocol.md) mục 4.3) ⬜ |

`config_dir` mặc định: Windows `%APPDATA%\Teleport`, macOS `~/Library/Application Support/Teleport`, Linux `$XDG_CONFIG_HOME/teleport` (hoặc `~/.config/teleport`).

### Danh tính (`device.crt`, `device.key`)

Do `teleport.trust.identity.load_or_create_identity` tạo và đọc.

- **Tạo** (chưa có cả hai file, và chưa có `trusted.json`), trong khi giữ `identity.lock`: ghi `device.key.tmp` và `device.crt.tmp` (quyền `0o600` ngay lúc tạo, `fsync`), rồi đổi tên khóa trước, chứng chỉ sau. Chứng chỉ có hiệu lực từ 2000-01-01 tới 9999-12-31. Còn `device.crt.tmp` mà chưa có `device.crt` (bị ngắt giữa hai bước đổi tên) thì lần chạy sau làm nốt.
- **Đọc:** chứng chỉ phải qua đúng bộ kiểm tra dành cho chứng chỉ máy khác ([protocol.md](protocol.md) mục 5); khóa phải là ECDSA P-256 và khớp chứng chỉ.
- **Lỗi:** chỉ còn một file, file hỏng, khóa không khớp → `IdentityError` kèm hướng dẫn; CLI in thông báo và thoát với mã 1. **Không bao giờ** tự tạo danh tính mới khi đã có một phần, vì làm vậy sẽ âm thầm xóa mọi ghép đôi.

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
- `TrustStore.add` thay mục cũ trùng `device_id` hoặc `fingerprint`. Tầng `app` không bao giờ gọi nó với `device_id` đã tin cậy mà fingerprint khác: trường hợp đó bị chặn từ lúc ghép đôi ([protocol.md](protocol.md) mục 5.1, 7).

### `node.lock`

Do `teleport.lock.NodeLock` giữ (tiện ích dùng chung như `paths.py`, vì `trust` cũng cần khóa này). Khóa là khóa của hệ điều hành trên file (Windows: `msvcrt.locking` byte 0; Linux/macOS: `flock`), không phải sự tồn tại của file:

- Lấy khóa không chờ. Đã có node khác giữ (kể cả trong cùng tiến trình) → `NodeLockedError` (“Teleport đang chạy với thư mục cấu hình này”), lệnh CLI thoát với mã ≠ 0.
- Tiến trình chết, kể cả bị kill, thì hệ điều hành tự nhả khóa: không có khóa treo phải xóa tay.
- File không chứa gì (không ghi PID) và được giữ lại sau khi nhả khóa.

## 5. Bất đồng bộ

Toàn bộ mạng chạy trên **một event loop `asyncio`**; giao diện Qt dùng chung event loop đó qua `qasync`. Việc chặn (ghi đĩa lớn, băm) được đẩy sang thread bằng `asyncio.to_thread`, để event loop không bao giờ bị đứng.

## 6. Kiểm thử

- `tests/` chia thư mục theo tầng: `core/`, `trust/`, `net/`… Test ở thư mục gốc `tests/` dành cho các tiện ích dùng chung (`paths`, `lock`), `cli` và quy tắc phân tầng.
- Test cần socket thật được đánh dấu `@pytest.mark.integration`. Chạy nhanh phần còn lại bằng `pytest -m "not integration"`.

Giao thức trên dây: xem [protocol.md](protocol.md).
