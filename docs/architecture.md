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
   paths.py, lock.py, atomic.py — thư mục cấu hình theo hệ điều hành, khóa file, ghi file nguyên tử;
   tầng nào cũng dùng được (trừ core)
```

**Quy tắc**, được `tests/test_architecture.py` kiểm tra tự động:
1. Mỗi tầng chỉ import các tầng **bên dưới**. `ui` và `cli` không import lẫn nhau.
2. `core` là **sans-IO**: không import `asyncio`, `socket`, `ssl`, `threading`, `zeroconf`, `ifaddr`, `PySide6`, `teleport.paths`, `teleport.lock`, hay bất kỳ tầng nào khác.
3. Mọi module/gói ở cấp `teleport.*` phải thuộc một tầng, hoặc là tiện ích dùng chung (`paths`, `lock`, `atomic`) không import tầng nào. Tạo gói mới thì phải xếp nó vào sơ đồ.

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

`teleport unpair` sửa `trusted.json` khi không có node nào chạy, và giữ `node.lock` trong lúc sửa. Đang có `teleport listen` thì báo phải dừng nó trước (tới v0.3).

`teleport list` chỉ duyệt mDNS trong vài giây: không giữ `node.lock` và không quảng bá máy này. `teleport pair <tên>` chạy một node chỉ duyệt, không quảng bá và không tự giữ kết nối.

Chạy nhiều node trên cùng một máy để phát triển/test: cho mỗi node một thư mục cấu hình riêng bằng `TELEPORT_HOME` hoặc `--config-dir`.

## 3. Bản đồ module

✅ đã có · ⏳ đang làm (giai đoạn hiện tại) · ⬜ chưa làm

| Tầng | Module | Trách nhiệm | Trạng thái |
|---|---|---|---|
| — | `paths.py` | Thư mục cấu hình theo hệ điều hành; `TELEPORT_HOME` ghi đè | ✅ |
| — | `lock.py` | Khóa file của hệ điều hành: `node.lock` (một node cho mỗi thư mục cấu hình), `identity.lock` (khi tạo danh tính) | ✅ |
| — | `atomic.py` | Ghi file nguyên tử (`.tmp` → fsync → `os.replace`, thử lại khi bị khóa); dùng cho `trusted.json`, `settings.json` | ✅ |
| core | `framing.py` | Mã hóa/giải mã khung `[uint32 độ dài][JSON]`; giới hạn 16 MiB, riêng kết nối ghép đôi 16 KiB | ✅ |
| core | `messages.py` | Kiểu và kiểm tra hợp lệ từng message (`hello` có `os`, `pair_*` gồm `pair_nonce.signature`, `control_open`, `ping`/`pong`); bảng loại kết nối (`ConnectionKind`, `allowed_types`, `opening_kind`) | ✅ |
| core | `keepalive.py` | Keepalive sans-IO: khi nào gửi ping, khi nào coi là mất kết nối ([protocol.md](protocol.md) mục 2.3) | ✅ |
| core | `pairing_code.py` | Cam kết nonce và mã ghép đôi 6 số từ hai fingerprint + hai nonce; payload chứng minh giữ khóa mà bên khởi xướng ký (`proof_payload`) | ✅ |
| core | `pairing.py` | Máy trạng thái ghép đôi sans-IO: thứ tự `pair_*`, hết giờ, lý do hủy (`InitiatorSession`, `ResponderSession`); bên khởi xướng ký (`ProofSigner`) và bên trả lời kiểm chữ ký trong `pair_nonce` (`ProofVerifier`) trước khi hiện mã, hai hàm do `app` đưa vào từ `trust`; chống làm phiền: một phiên tại một thời điểm, chờ tăng dần theo IP, giới hạn số mã hiện ra, hai bên cùng chủ động (`PairingGate`) | ✅ |
| core | `display.py` | Hiện tên do máy kia tự đặt (`display_name`): ký tự trông như khoảng trắng thành `·`, quá dài thì cắt và thêm `…`; độ rộng tối đa và luật bố cục ở [mvp.md](mvp.md) mục 7.2 | ✅ |
| core | `discovery.py` | Khám phá sans-IO: dựng/kiểm bản ghi TXT, cắt tên ≤ 200 byte, lọc địa chỉ, nhận ra card ảo, chọn địa chỉ quảng bá; máy trạng thái `DiscoveryTracker` (thiết bị thêm/sửa/xóa, kiểm còn sống và probe thưa dần cho thiết bị đã biến mất, ưu tiên thiết bị đã ghép đôi, danh sách tràn khi đã đủ 256; protocol mục 8); phát hiện mạng đổi / máy thức dậy (`NetworkWatch`) | ✅ |
| core | `links.py` | Máy trạng thái giữ kết nối điều khiển (sans-IO): chống trùng §6.1, lịch nối lại và chặn §6.2, giới hạn tốc độ, báo lỗi một lần | ✅ |
| core | `blocks.py` | Khung khối nhị phân kèm SHA-256, chia khối | ⬜ |
| core | `merkle.py`, `bitfield.py` | Cây băm SHA-256; bảng khối đã nhận và đã xác minh | ⬜ |
| core | `transfer.py` | Máy trạng thái gửi/nhận, nối lại, gửi lại khối lỗi | ⬜ |
| trust | `identity.py` | Khóa ECDSA P-256, chứng chỉ tự ký, fingerprint; kiểm tra chứng chỉ của máy khác (`PeerCert`; mọi lỗi phân tích → `InvalidCertificate`); danh tính hỏng → `IdentityError`, không tự tạo lại; ký chứng minh giữ khóa khi ghép đôi (`load_pairing_signer` → `PairingSigner`) và kiểm chữ ký của máy kia (`verify_pairing_proof`, không bao giờ ném) | ✅ |
| trust | `store.py` | `trusted.json`: thiết bị đã ghép đôi | ✅ |
| net | `tls.py` | `SSLContext` TLS 1.3; server một cổng (`CERT_OPTIONAL` + thiết bị tin cậy làm trust anchor); client so toàn bộ fingerprint | ✅ |
| net | `stream.py` | Đọc/ghi message trên `asyncio` stream | ✅ |
| net | `discovery.py` | Quảng bá & tìm `_teleport._tcp.local.` (zeroconf, `MdnsDiscovery`): đăng ký/cập nhật/goodbye, duyệt và resolve theo `core.discovery`, đặt tập thiết bị ưu tiên (`set_priority`), đọc card mạng (ifaddr) và địa chỉ nguồn ưu tiên | ✅ |
| net | `connection.py` | Listener một cổng (TCP trần + `start_tls` theo context hiện tại); trao `hello`; phân loại ghép đôi / điều khiển; giới hạn đang mở đầu 4/IP · 64 và ẩn danh 1/IP · 8; thời gian chờ; `Connection`, `ControlChannel` (keepalive); `open_pairing` / `open_control` thử song song mọi địa chỉ | ✅ (kết nối dữ liệu ⬜, Giai đoạn 2) |
| app | `settings.py` | `settings.json`: cổng dự phòng; giữ trường lạ; file hỏng → mặc định + `.bak` trước khi ghi đè | ✅ |
| app | `devices.py` | Đọc địa chỉ nhập tay (`ip[:cổng]`); tìm thiết bị tin cậy theo tên hoặc tiền tố `device_id` (mvp F12); `DeviceView`/`DeviceRegistry` (gộp mDNS + `trusted.json` + trạng thái kết nối), `resolve_device` | ✅ |
| app | `pairing.py` | Dịch vụ ghép đôi (`PairingService`): sinh nonce, ký/kiểm chữ ký chứng minh giữ khóa qua `trust`, chạy `core.pairing` trên `Connection`, `PairingGate`, lưu thiết bị (ghép đôi lại ở cả hai vai cập nhật tên; gửi `pair_confirm` lỗi thì không lưu), nhường phiên khi hai bên cùng chủ động; không hiện được mã thì hủy phiên; báo cho giao diện số mã vừa hiện, tên đã lưu (`PairingPrompt.known_name`) và phiên đã hiện mã chưa (`PairingFinished.code_shown`); `pair_with` bị hủy sau khi hai bên đã xác nhận thì trả kết quả thật thay vì ném `CancelledError` | ✅ |
| app | `node.py` | Ghép các thành phần lại (`Node`): giữ `node.lock`, lắng nghe, khám phá, kết nối điều khiển tới thiết bị tin cậy, điều phối | ✅ lắng nghe, cổng dự phòng, ghép đôi, hủy ghép đôi đóng kết nối ngay; mDNS (quảng bá, duyệt, ưu tiên thiết bị đã ghép đôi, quảng bá lại khi mạng đổi/thức dậy), tự giữ kết nối điều khiển (`core.links`), sự kiện `NodeEvent`, `scan_devices` |
| app | `transfers.py`, `clipboard.py` | Quản lý lượt truyền, đồng bộ clipboard | ⬜ |
| cli | `cli.py`, `__main__.py` | Lệnh `teleport` / `python -m teleport` | ✅ `identity`, `list`, `listen`, `pair <tên \| tiền tố \| ip[:cổng]>`, `unpair` |
| ui | `ui/` | Radar, lượt truyền, ghép đôi, cài đặt, khay hệ thống | ⬜ |

Module chỉ được tạo khi có code thật, không tạo file rỗng trước.

## 4. Dữ liệu trên đĩa

| Đường dẫn | Nội dung |
|---|---|
| `<config_dir>/device.crt`, `device.key` | Danh tính thiết bị, tạo ở lần chạy đầu (chi tiết bên dưới) ✅ |
| `<config_dir>/trusted.json` | Chứng chỉ + tên các thiết bị đã ghép đôi ✅ |
| `<config_dir>/node.lock` | Khóa: chỉ một node mỗi thư mục cấu hình ✅ |
| `<config_dir>/identity.lock` | Khóa trong lúc tạo danh tính, để nhiều tiến trình chạy lần đầu cùng lúc nhận cùng một danh tính ✅ |
| `<config_dir>/settings.json` | Cài đặt người dùng: cổng dự phòng khi 47474 bận ([protocol.md](protocol.md) mục 2.1) ✅; sau này cả cài đặt theo từng thiết bị tin cậy (“Hỏi trước khi nhận”, “Đồng bộ clipboard”), theo `device_id` ⬜. Không đưa vào `trusted.json`: file đó bỏ trường lạ khi ghi và bị bản cũ từ chối nếu tăng `version`. Chi tiết bên dưới |
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

- **Ghi:** UTF-8, `indent=2`, giữ nguyên ký tự Unicode; các mục sắp theo tên (không phân biệt hoa thường) rồi `device_id`; `paired_at` theo UTC, dạng `YYYY-MM-DDTHH:MM:SSZ`. Ghi nguyên tử bằng `teleport.atomic.write_atomic`: `trusted.json.tmp` → fsync → `os.replace` (tối đa 5 lần thử, cách nhau 50 ms, nếu file đích đang bị tiến trình khác mở). Trên Linux/macOS quyền file là `0o600` và thư mục cha được fsync sau khi đổi tên. Thư mục cấu hình được tạo nếu chưa có. Trường lạ không được giữ khi ghi lại.
- **Đọc:** chấp nhận BOM UTF-8. Chưa có file → kho rỗng, không tạo file. Mỗi mục được kiểm lại: phân tích lại chứng chỉ (như khi ghép đôi, xem [protocol.md](protocol.md) mục 5), `fingerprint` = SHA-256 DER, `device_id` khớp CN, `name` hợp lệ, `paired_at` là ISO 8601 có múi giờ. Trường lạ bị bỏ qua.
- **Lỗi:** JSON hỏng, thiếu/sai trường, `version` khác số nguyên 1 (lớn hơn là bản mới hơn), trùng `device_id` hoặc trùng `fingerprint` → **cả file** bị từ chối (`TrustStoreError`). File hỏng **không bao giờ** bị tự xóa, sửa hay tạo lại.
- `TrustStore.add` thay mục cũ trùng `device_id` hoặc `fingerprint`. Tầng `app` không bao giờ gọi nó với `device_id` đã tin cậy mà fingerprint khác: trường hợp đó bị chặn từ lúc ghép đôi ([protocol.md](protocol.md) mục 5.1, 7).

### `settings.json`

Do `teleport.app.settings.Settings` đọc/ghi.

```json
{
  "version": 1,
  "port": 47475
}
```

- **Ghi:** chỉ khi phải dùng cổng dự phòng (47474 hay cổng đã lưu không mở được → hệ điều hành chọn cổng khác). UTF-8, `indent=2`, ghi nguyên tử bằng `teleport.atomic.write_atomic`. Trường lạ được giữ nguyên, kể cả `version` lớn hơn bản app hỗ trợ (không coi là lỗi), để bản cũ không xóa dữ liệu của bản mới.
- **Đọc:** chấp nhận BOM UTF-8. Chưa có file → mặc định, không tạo file. Không bao giờ chặn app: file hỏng (> 1 MiB, không phải JSON UTF-8, không phải object) → ghi cảnh báo vào log (chỉ loại lỗi, không ghi nội dung) và dùng mặc định; `port` hay `version` sai → cảnh báo, bỏ trường đó.
- **File hỏng:** trước khi ghi đè, bản gốc được chép nguyên byte sang `settings.json.bak` (ghi đè `.bak` cũ). Không chép được thì không ghi file chính.

### `node.lock`

Do `teleport.lock.NodeLock` giữ (tiện ích dùng chung như `paths.py`, vì `trust` cũng cần khóa này). Khóa là khóa của hệ điều hành trên file (Windows: `msvcrt.locking` byte 0; Linux/macOS: `flock`), không phải sự tồn tại của file:

- Lấy khóa không chờ. Đã có node khác giữ (kể cả trong cùng tiến trình) → `NodeLockedError` (“Teleport đang chạy với thư mục cấu hình này”), lệnh CLI thoát với mã ≠ 0.
- Tiến trình chết, kể cả bị kill, thì hệ điều hành tự nhả khóa: không có khóa treo phải xóa tay.
- File không chứa gì (không ghi PID) và được giữ lại sau khi nhả khóa.

## 5. Bất đồng bộ

Toàn bộ mạng chạy trên **một event loop `asyncio`**; giao diện Qt dùng chung event loop đó qua `qasync`. Việc chặn (ghi đĩa lớn, băm) được đẩy sang thread bằng `asyncio.to_thread`, để event loop không bao giờ bị đứng.

Ngoại lệ có chủ đích: ghi `trusted.json` và `settings.json` chạy đồng bộ trên event loop. File nhỏ (thường < 50 ms, xấu nhất ~250 ms khi phải thử lại `os.replace`), và nhờ vậy trạng thái trong RAM đổi cùng lúc với file, không có `await` xen giữa (ví dụ `Node.unpair`: xóa khỏi kho, đổi danh sách tin cậy của cổng và lấy kết nối của máy đó ra khỏi bảng trong cùng một bước).

## 6. Kiểm thử

- `tests/` chia thư mục theo tầng: `core/`, `trust/`, `net/`… Test ở thư mục gốc `tests/` dành cho các tiện ích dùng chung (`paths`, `lock`, `atomic`), `cli` và quy tắc phân tầng.
- Test cần socket thật được đánh dấu `@pytest.mark.integration`. Chạy nhanh phần còn lại bằng `pytest -m "not integration"`.

Giao thức trên dây: xem [protocol.md](protocol.md).
