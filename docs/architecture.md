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
2. `core` là **sans-IO**: không import `asyncio`, `socket`, `ssl`, `threading`, `zeroconf`, `selectors`, `ifaddr`, `PySide6`, `teleport.paths`, `teleport.lock`, `teleport.atomic`, hay bất kỳ tầng nào khác.
3. Mọi module/gói ở cấp `teleport.*` phải thuộc một tầng, hoặc là tiện ích dùng chung (`paths`, `lock`, `atomic`) không import tầng nào. Tạo gói mới thì phải xếp nó vào sơ đồ, và vào `LAYERS` (hoặc `SHARED`) trong `tests/test_architecture.py`.

### Vì sao chia như vậy

- **core sans-IO:** logic khó nhất là chọn khối nào gửi lại, xử lý `resume`, lượt đọc lại cuối, quyết định hủy khi file nguồn đổi. Phần này được viết thành **máy trạng thái thuần**: nhận sự kiện (message tới, khối tới), trả về hành động (gửi message, ghi khối, báo lỗi). Khi đó test ngẫu nhiên hàng nghìn kịch bản ngắt kết nối bằng `hypothesis` chạy trong vài mili giây, không cần socket.
- **trust tách khỏi net:** danh tính và danh sách thiết bị tin cậy là mật mã cộng lưu trữ, không phải mạng. Tách ra thì giữ chúng nhỏ và dễ kiểm toán.
- **net chỉ là đường ống:** `net` đọc bytes, đưa cho `core` xử lý, rồi thực hiện hành động `core` trả về. Không có quyết định nghiệp vụ nào trong `net`.
- **app là nơi điều phối duy nhất:** CLI và giao diện gọi cùng các dịch vụ, nên không viết logic hai lần. `app` chỉ trả mã kết quả (ví dụ `PairingOutcome`); câu chữ cho người dùng nằm ở `cli.py` và sau này ở `ui`.
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
| core | `framing.py` | Mã hóa/giải mã khung `[uint32 độ dài][JSON]`; giới hạn 1 MiB, riêng kết nối ghép đôi 16 KiB | ✅ |
| core | `messages.py` | Kiểu và kiểm tra hợp lệ từng message, nơi duy nhất kiểm từng trường của một message do máy khác gửi (`hello` có `os`, `pair_*` gồm `pair_nonce.signature`, `control_open`, `ping`/`pong`; truyền file: `data_open`, `offer` với `FileEntry`, `accept`, `reject` có trường kèm theo lý do, `resume`, `complete`, `verified`, `cancel`; chỉ những gì kiểm được từ riêng message, phần cần `offer` để máy trạng thái truyền file kiểm); bảng loại kết nối (`ConnectionKind`, `allowed_types`, `opening_kind`: `data_open` có chứng chỉ client → dữ liệu) | ✅ |
| core | `keepalive.py` | Keepalive sans-IO: khi nào gửi ping, khi nào coi là mất kết nối ([protocol.md](protocol.md) mục 2.3) | ✅ |
| core | `pairing_code.py` | Cam kết nonce và mã ghép đôi 6 số từ hai fingerprint + hai nonce; payload chứng minh giữ khóa mà bên khởi xướng ký (`proof_payload`) | ✅ |
| core | `pairing.py` | Máy trạng thái ghép đôi sans-IO: thứ tự `pair_*`, hết giờ, lý do hủy (`InitiatorSession`, `ResponderSession`); bên khởi xướng ký (`ProofSigner`) và bên trả lời kiểm chữ ký trong `pair_nonce` (`ProofVerifier`) trước khi hiện mã, hai hàm do `app` đưa vào từ `trust`; chống làm phiền: một phiên tại một thời điểm, chờ tăng dần theo IP, giới hạn số mã hiện ra, hai bên cùng chủ động (`PairingGate`) | ✅ |
| core | `display.py` | Hiện tên do máy kia tự đặt (`display_name`): ký tự trông như khoảng trắng thành `·`, quá dài thì cắt và thêm `…`; độ rộng tối đa và luật bố cục ở [mvp.md](mvp.md) mục 7.2 | ✅ |
| core | `filenames.py` | Tên file trong `offer` ([ke_hoach.md](ke_hoach.md) mục 3.3): bên nhận làm sạch, biến đổi chứ không từ chối (`sanitize_filename`: chỉ giữ phần tên cuối, bỏ ổ đĩa, ký tự cấm, ký tự điều khiển/vô hình/đổi hướng chữ, NFC, ≤ 255 byte giữ đuôi, đổi tên thiết bị Windows và `.teleport`); đánh số khi trùng (`numbered`, `dedupe_offer_names`, khóa so trùng `dedupe_key`, không phân biệt hoa thường); cắt theo ngân sách byte mà kết quả vẫn ổn định qua bộ làm sạch (`fit_filename`); tên dự phòng ASCII (`fallback_filename`); tên thư mục tạm `TEMP_DIR_NAME`; bên gửi kiểm và cắt tên (`prepare_outgoing_name`, `OutgoingNameError`). Giới hạn độ dài cả đường dẫn và lúc nào dùng tên dự phòng: `teleport.app.storage` (mục 4 “Thư mục nhận”) | ✅ |
| core | `discovery.py` | Khám phá sans-IO: dựng/kiểm bản ghi TXT, cắt tên ≤ 200 byte, lọc địa chỉ, nhận ra card ảo, chọn địa chỉ quảng bá; máy trạng thái `DiscoveryTracker` (thiết bị thêm/sửa/xóa, kiểm còn sống và probe thưa dần cho thiết bị đã biến mất, ưu tiên thiết bị đã ghép đôi, danh sách tràn khi đã đủ 256; protocol mục 8); phát hiện mạng đổi / máy thức dậy (`NetworkWatch`) | ✅ |
| core | `links.py` | Máy trạng thái giữ kết nối điều khiển (sans-IO): chống trùng §6.1 (giữ kết nối mới thì báo `LinkSwitched`, không báo người dùng), lịch nối lại và chặn §6.2, giới hạn tốc độ, báo lỗi một lần; địa chỉ người dùng đưa (`hint`, gộp với địa chỉ mDNS; chỉ có `hint` thì chỉ thử khi người dùng thao tác hay có sự kiện, không theo lịch); giữ địa chỉ của thiết bị còn lượt truyền dở khi mDNS báo biến mất (`hold`) | ✅ |
| core | `admission.py` | Cửa nhận kết nối vào của cổng chung (sans-IO, `AdmissionGate`): số kết nối đang mở đầu và tốc độ bắt tay theo IP và tổng (token bucket; hoàn token khi bắt tay xong bằng chứng chỉ còn tin cậy); giới hạn ẩn danh; pool ưu tiên cho IP đã xác thực mTLS (có hạn, bỏ khi hủy ghép đôi); ngân sách lỗi và hạ ưu tiên (`finish`, `priority_strikes`); bảng IP có trần, kể cả bảng IP bị hạ ([protocol.md](protocol.md) mục 2.3) | ✅ |
| core | `blocks.py` | Khung khối nhị phân 41 byte kèm SHA-256 (`BLOCK_HEADER`, `pack_header`, `parse_header` → `BlockHeader`, `check_block` theo kích thước file; sai → `BlockError`); chia khối (`block_count`, `block_length`); trần kích thước file ([protocol.md](protocol.md) mục 4.2) | ✅ |
| core | `merkle.py` | Merkle root từ hash các khối nối liền trong một `bytearray` ([protocol.md](protocol.md) mục 4.4, có test vector) | ✅ |
| core | `bitfield.py` | `Bitfield`: bảng khối bên nhận đã có, bit cao nhất trước, base64 chuẩn chặt trên dây; sai độ dài hay bit thừa ≠ 0 → `ValueError` ([protocol.md](protocol.md) mục 3.3) | ✅ |
| core | `resume_state.py` | Nhật ký nối lại `.state` (sans-IO): mã hóa header và các bản ghi `MARK`/`CLEAR`/`CLEAR_ALL`/`TARGET`/`RESERVED`, đọc lại nhật ký theo từng đoạn bytes (dừng ở bản ghi hỏng đầu tiên, báo vị trí cần cắt), dựng bitfield + hash, quyết định khi nào gộp và cách phục hồi tên đích (mục 4); đọc/ghi file ở `app` | ⬜ |
| core | `transfer.py` | Máy trạng thái gửi/nhận sans-IO (`TransferEngine`): hàng `offer` có trần; đồng bộ lại ([protocol.md](protocol.md) mục 6.3); `seq` và kết nối dữ liệu (mục 4.1); `fsync` theo lô trước khi bật bit; khối trùng và thứ tự khối; đọc lại và Merkle; bia mộ / `reject` / lượt đã xong có trần mỗi thiết bị ([protocol.md](protocol.md) mục 6.3); chờ nối lại; `check_offer`, `mtime_ms`; lỗi đĩa kèm nguyên nhân (`storage_failed(detail=)`: `DISK`, `TEMP_EXISTS`). Giai đoạn 2: trạng thái chỉ trong RAM | ✅ |
| core | `ratelimit.py` | TokenBucket giới hạn tốc độ gửi theo đoạn payload, burst một đoạn ([protocol.md](protocol.md) mục 4.2) | ✅ |
| trust | `identity.py` | Khóa ECDSA P-256, chứng chỉ tự ký, fingerprint; kiểm tra chứng chỉ của máy khác (`PeerCert`; mọi lỗi phân tích → `InvalidCertificate`); danh tính hỏng → `IdentityError`, không tự tạo lại; ký chứng minh giữ khóa khi ghép đôi (`load_pairing_signer` → `PairingSigner`) và kiểm chữ ký của máy kia (`verify_pairing_proof`, không bao giờ ném) | ✅ |
| trust | `store.py` | `trusted.json`: thiết bị đã ghép đôi | ✅ |
| net | `tls.py` | `SSLContext` TLS 1.3; server một cổng (`CERT_OPTIONAL` + thiết bị tin cậy làm trust anchor); client kết nối điều khiển và kết nối dữ liệu `CERT_REQUIRED` với đúng chứng chỉ đã lưu của máy kia (`trusted_client_context`), client ghép đôi ẩn danh `CERT_NONE` (`client_context`); cả hai chiều `VERIFY_X509_STRICT` + `NO_CHECK_TIME`; so toàn bộ fingerprint | ✅ |
| net | `stream.py` | Đọc/ghi message trên `asyncio` stream | ✅ |
| net | `discovery.py` | Quảng bá & tìm `_teleport._tcp.local.` (zeroconf, `MdnsDiscovery`): đăng ký/cập nhật/goodbye, duyệt và resolve theo `core.discovery`, đặt tập thiết bị ưu tiên (`set_priority`), đọc card mạng (ifaddr) và địa chỉ nguồn ưu tiên | ✅ |
| net | `connection.py` | Listener một cổng (TCP trần + `start_tls` theo context hiện tại); trao `hello`; phân loại ghép đôi / điều khiển / dữ liệu (kiểm lại `is_trusted` cả khi nhận `data_open`; handler của kết nối dữ liệu kết thúc mà chưa đóng → cắt); thực thi quyết định của `core.admission` (từ chối = reset trước TLS; hoàn token chỉ khi chứng chỉ client còn tin cậy; báo kết nối điều khiển vào/ra để cấp ưu tiên, kết nối dữ liệu không cấp; `refusals`); thời gian chờ; `Connection`, `ControlChannel` (keepalive; `pong` gửi ở task riêng, gộp `ping` dồn); `open_pairing` / `open_control` thử song song mọi địa chỉ (lỗi kiểm chứng chỉ server trong bắt tay → `IdentityMismatch`). Kết nối dữ liệu: `open_data` (một địa chỉ, một lần thử); `write_raw` / `write_block` (đoạn 64 KiB, móc `before_chunk`, kiểm máy kia đã đóng trước mỗi đoạn); `read_raw` / `read_block` (header kiểm trước payload bằng hàm thuần `core.blocks.check_block` với `size` do `app` đưa vào, đếm khối sai ở `core.transfer`; hạn chờ đọc → `IdleTimeout`); `abort`; `close` của kết nối dữ liệu tối đa hạn `drain()` | ✅ |
| app | `settings.py` | `settings.json`: cổng dự phòng; giữ trường lạ; file hỏng → mặc định + `.bak` trước khi ghi đè | ✅ |
| app | `storage.py` | Lưu file nhận an toàn, API đồng bộ gọi qua thread (mục 4 “Thư mục nhận”): `ReceiveDir` (thư mục `.teleport` tạo khi cần, `0o700` trên Linux/macOS, ẩn trên Windows; từ chối symlink/junction trong cây `.teleport`; `transfer_id` kiểm bằng `core.messages.validate_transfer_id`; `<transfer_id>` đã có → lỗi; giới hạn độ dài đường dẫn); `volume_info` (hệ file FAT → trần kích thước file; dung lượng trống); `TransferTemp`/`PartFile` (`write_at` không tạo lỗ, `sync` có `F_FULLFSYNC`, `reread` trả hash từng khối và cắt `.part` thừa); `finalize` (đổi tên không ghi đè, mục 4); `remove_temp` (chỉ xóa file của mình) | ✅ (chưa có `.state`, Giai đoạn 3) |
| app | `sources.py` | Đọc file nguồn của bên gửi, API đồng bộ gọi qua `DiskExecutor`: `prepare_sources` (kiểm file thường, kích thước, tên gửi đi, quyền đọc; dựng `FileEntry`; lỗi → `SourceError` có `SourceErrorKind`), `SourceReader` (đọc và băm từng khối, file đổi → `SourceChanged`), `check_source` (cho `CheckSource`) | ✅ (dùng từ P2-6b) |
| app | `diskio.py` | `DiskExecutor`: số thread đĩa cố định, hạn tính từ lúc thread bắt đầu việc (`DiskTimeout`, việc vẫn chạy và vẫn tính `busy`), `DiskJob` (`cancel` việc chưa chạy, `settled` chờ việc thật sự xong), `busy`/`wait_idle` đếm tới khi loop đã nhận kết quả, `shutdown` không chờ thread (mục 5) | ✅ (dùng từ P2-6b) |
| app | `devices.py` | Đọc địa chỉ nhập tay (`ip[:cổng]`); tìm thiết bị tin cậy theo tên hoặc tiền tố `device_id` (mvp F12); `DeviceView`/`DeviceRegistry` (gộp mDNS + `trusted.json` + trạng thái kết nối), `resolve_device` | ✅ |
| app | `pairing.py` | Dịch vụ ghép đôi (`PairingService`): sinh nonce, ký/kiểm chữ ký chứng minh giữ khóa qua `trust`, chạy `core.pairing` trên `Connection`, `PairingGate`, lưu thiết bị (ghép đôi lại ở cả hai vai cập nhật tên; gửi `pair_confirm` lỗi thì không lưu), nhường phiên khi hai bên cùng chủ động; không hiện được mã thì hủy phiên; báo cho giao diện số mã vừa hiện, tên đã lưu (`PairingPrompt.known_name`) và phiên đã hiện mã chưa (`PairingFinished.code_shown`); `pair_with` bị hủy sau khi hai bên đã xác nhận thì trả kết quả thật thay vì ném `CancelledError` | ✅ |
| app | `node.py` | Ghép các thành phần lại (`Node`): giữ `node.lock`, lắng nghe, khám phá, kết nối điều khiển tới thiết bị tin cậy, điều phối | ✅ lắng nghe, cổng dự phòng, ghép đôi, hủy ghép đôi đóng kết nối ngay; mDNS (quảng bá, duyệt, ưu tiên thiết bị đã ghép đôi, quảng bá lại khi mạng đổi/thức dậy), tự giữ kết nối điều khiển (`core.links`; chỉ một thiết bị: `auto_connect_to`; tới địa chỉ người dùng đưa: `connect_hint`; đổi kết nối không phát sự kiện), sự kiện `NodeEvent`, `scan_devices`; kết nối dữ liệu vào bị cắt ngay (chưa có `app/transfers.py`) |
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
| `<thư mục nhận>/.teleport/<transfer_id>/transfer.json` | Trạng thái mức lượt truyền, còn sau khi khởi động lại: manifest của `offer` (chỉ nằm ở đây; `.state` chỉ đối chiếu), thiết bị gửi (`device_id`), đã chấp nhận, file hiện tại, các file đã `verified`; ghi nguyên tử; xóa 24 giờ sau khi file cuối xong ([protocol.md](protocol.md) mục 6.3) ⬜ |
| `<thư mục nhận>/.teleport/<transfer_id>/<file_index>.part` | Dữ liệu đang nhận, ghi đúng vị trí từng khối ✅ (Giai đoạn 2, `teleport.app.storage`; chi tiết bên dưới) |
| `<thư mục nhận>/.teleport/<transfer_id>/<file_index>.state` | Trạng thái nối lại của file đó, dạng nhật ký chỉ ghi thêm. Quy tắc bền vững: [protocol.md](protocol.md) mục 4.3; định dạng: bên dưới ⬜ |

`config_dir` mặc định: Windows `%APPDATA%\Teleport`, macOS `~/Library/Application Support/Teleport`, Linux `$XDG_CONFIG_HOME/teleport` (hoặc `~/.config/teleport`).

### Danh tính (`device.crt`, `device.key`)

Do `teleport.trust.identity.load_or_create_identity` tạo và đọc.

- **Tạo** (chưa có cả hai file, và `trusted.json` chưa có hoặc có danh sách thiết bị rỗng; `trusted.json` không đọc được hay hỏng thì coi như còn ghép đôi → `IdentityError`), trong khi giữ `identity.lock`: ghi `device.key.tmp` và `device.crt.tmp` (quyền `0o600` ngay lúc tạo, `fsync`), rồi đổi tên khóa trước, chứng chỉ sau. Chứng chỉ có hiệu lực từ 2000-01-01 tới 9999-12-31. Còn `device.crt.tmp` mà chưa có `device.crt` (bị ngắt giữa hai bước đổi tên) thì lần chạy sau làm nốt.
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

- Mặc định lấy khóa không chờ. Đã có node khác giữ (kể cả trong cùng tiến trình) → `NodeLockedError` (“Teleport đang chạy với thư mục cấu hình này”), lệnh CLI thoát với mã ≠ 0.
- Tiến trình chết, kể cả bị kill, thì hệ điều hành tự nhả khóa: không có khóa treo phải xóa tay. Nhưng Windows nhả khóa của tiến trình bị kill **không đồng bộ**, nên chỗ nào bên giữ khóa có thể vừa chết (node khởi động lại sau khi sập) phải lấy khóa có chờ: `acquire(timeout=...)`, như `Node.start` với `LOCK_TIMEOUT`.
- File không chứa gì (không ghi PID) và được giữ lại sau khi nhả khóa.

### Thư mục nhận (`teleport.app.storage`)

Do `teleport.app.storage` quản lý (API đồng bộ, mục 5). Giai đoạn 2 chưa có `.state` và `transfer.json` (bên dưới).

- **Bố cục:** `<thư mục nhận>/.teleport/<transfer_id>/<file_index>.part`; không đặt tên nào khác vào cây này. `.teleport` được tạo ở lần nhận đầu tiên (chưa nhận gì thì không có thư mục ẩn nào), được đặt thuộc tính ẩn trên Windows khi vừa tạo, và không bao giờ bị xóa khi dọn một lượt (Giai đoạn 3 dọn). Trên Linux/macOS, `.teleport` và `<transfer_id>` có quyền `0o700` và phải thuộc euid của tiến trình hoặc chủ của thư mục nhận (ổ USB mount theo uid cố định); quyền khác `0o700` thì `chmod` lại, `chmod` lỗi trên hệ file không hỗ trợ (vfat, exfat, ntfs-3g, cifs) thì bỏ qua. Trên Windows, hai thư mục này được tạo không truyền mode nên kế thừa ACL của thư mục nhận: từ Python 3.12.4, `os.mkdir(path, 0o700)` gắn một DACL được bảo vệ (chỉ SYSTEM, Administrators, OWNER RIGHTS), `.part` kế thừa DACL đó và đổi tên trong cùng ổ giữ nguyên nó, nên file nhận xong sẽ không mang ACL của thư mục nhận (thư mục chia sẻ, thư viện của media server chạy bằng tài khoản khác).
- **Liên kết:** chỉ cấm trong cây `.teleport`; chính thư mục nhận được phép là symlink hay junction, hoặc nằm dưới symlink (Downloads dời sang ổ khác, `/var` → `/private/var` trên macOS). Windows chỉ coi reparse point có bit name-surrogate (symlink, junction) là liên kết; reparse point của OneDrive Files On-Demand được phép. Linux/macOS làm mọi thao tác trong thư mục nhận qua `dir_fd` và `O_NOFOLLOW`, nên một mục bị thay bằng liên kết giữa chừng không làm ghi ra ngoài. `.teleport` có sẵn mà là liên kết, không phải thư mục hay sai chủ → lỗi ngay lúc mở thư mục nhận, trước khi nhận gì.
  - **Giới hạn đã biết trên Windows** (không có `dir_fd`): một tiến trình có quyền ghi vào thư mục nhận mà đua thay `.teleport` hay `<transfer_id>` bằng junction đúng giữa bước kiểm và bước tạo thì có thể làm file tạm của chính lượt này được tạo ở chỗ khác. File của nạn nhân vẫn không bao giờ bị xóa, ghi đè hay sửa (tạo bằng `O_EXCL`, xóa thì so danh tính trước), và `.part` được kiểm lại danh tính trước khi đổi tên.
  - Linux/macOS giữ fd của thư mục nhận suốt lúc nó còn mở, nên chưa tháo được ổ USB khi thư mục nhận còn mở.
- **Thư mục tạm của một lượt:** `<transfer_id>` đã có → lượt đó lỗi, không đụng nội dung cũ (không biết thư mục đó là của ai; thư mục sót lại để Giai đoạn 3 dọn). `.part` được tạo bằng `O_CREAT | O_EXCL` với quyền `0o666` trừ umask, để file nhận xong dùng được như file tải về thường; dữ liệu đang nhận được che bởi thư mục `0o700` trên Linux/macOS, còn trên Windows bởi ACL của chính thư mục nhận (như file trình duyệt đang tải).
- **Ghi không tạo lỗ:** ghi một khối bắt đầu sau cuối phần đã ghi của `.part` → `WriteGapError`, trước mọi I/O, file không đổi. Lý do: NTFS (khi ghi vượt ValidDataLength) và FAT/exFAT bắt hệ điều hành ghi số 0 cho cả khoảng trống, hàng chục phút với file lớn, trong một thread không hủy được. Đây là lưới an toàn ở tầng lưu; luật thứ tự khối trên dây ở [protocol.md](protocol.md) mục 4.2.
- **Đọc lại** ([protocol.md](protocol.md) mục 4.4, bước 3): đọc tuần tự bằng một bộ đệm 1 khối dùng lại, qua chính fd của `.part`. Khối đọc thiếu (kể cả khối nằm hẳn sau cuối file) có hash 32 byte 0, nên luôn bị coi là sai. `.part` dài hơn `size` thì cắt về `size` rồi `fsync`.
- **Hệ file:** Windows `GetVolumeInformationW`; Linux `/proc/self/mountinfo` (mount point dài nhất chứa thư mục nhận); macOS `statfs` (`f_fstypename`). Tập FAT `FAT_FS_TYPES` (`fat`, `fat12`, `fat16`, `fat32`, `vfat`, `msdos`, `umsdos`) → trần kích thước file của `reject.file_too_large` ([protocol.md](protocol.md) mục 3.3). `exfat`, `fuseblk` (ntfs-3g, exfat-fuse) và hệ file không nhận ra → không giới hạn: nhận nhầm còn hơn từ chối nhầm, và lỗi `EFBIG` khi ghi đi theo lỗi ghi đĩa.
- **Độ dài đường dẫn** ([ke_hoach.md](ke_hoach.md) mục 3.3), tính theo UTF-16 code unit trên Windows, theo byte trên Linux/macOS:
  - Windows: đường dẫn file cuối ≤ `WINDOWS_MAX_PATH` = 259 (MAX_PATH 260 trừ ký tự NUL), luôn theo MAX_PATH kể cả khi máy đã bật long path, không dùng `\\?\`: Explorer và nhiều ứng dụng vẫn không mở được file dài hơn.
  - Thư mục nhận phải chừa `TEMP_PATH_RESERVE` = 55 đơn vị, không thì lỗi ngay lúc mở. Cách tính: thư mục `<thư mục nhận>\.teleport\<transfer_id>` dài thêm 43 phải ≤ 247 = MAX_PATH − 12 − 1 (`CreateDirectoryW` chừa chỗ cho tên 8.3 khi máy chưa bật long path), chặt hơn `.part` dài nhất (thêm 52, phải ≤ 259). Nhờ vậy tên file cuối luôn còn ít nhất 54 đơn vị.
  - Linux/macOS: theo `PC_PATH_MAX` và `PC_NAME_MAX` của thư mục nhận, đo lúc mở.
  - Tên dài hơn chỗ còn lại thì cắt phần trước đuôi file, kể cả khi có hậu tố ` (n)` (`teleport.core.filenames.fit_filename`, `numbered`).
- **Chọn tên:** mỗi file liệt kê thư mục nhận một lần; so trùng bằng NFC + casefold trên mọi hệ điều hành, kể cả hệ file phân biệt hoa thường (tránh hai file chỉ khác hoa thường, gây nhầm và hỏng khi chép sang Windows hay macOS); giữ chỗ bằng `O_CREAT | O_EXCL`.
- **Tên dự phòng:** tạo file giữ chỗ gặp `EILSEQ`, `EINVAL` hay `ENAMETOOLONG` (hệ file từ chối tên) → dùng tên dự phòng (`fallback_filename`): mọi ký tự ngoài ASCII thành `_`, ≤ `FALLBACK_MAX_BYTES` = 64 byte, giữ đuôi ASCII, đánh số như thường; tên dự phòng cũng bị từ chối → lỗi. APFS trả `EILSEQ` cho một số code point nó chưa biết (đã thấy với U+18DF3); ổ vfat mount không UTF-8 trả `EINVAL` với tên ngoài ASCII. Bộ làm sạch không bỏ ký tự loại `Cn` (chưa gán): bảng Unicode của Python và của APFS lệch nhau theo cả hai chiều, nên lọc `Cn` không chặn hết `EILSEQ` mà lại làm mất emoji mới khi máy nhận chạy Python cũ.
- **Thứ tự đổi tên ở Giai đoạn 2:** các bước 2, 3, 5, 6 của “Kết thúc một file” bên dưới (chưa có bước 1, 4, 7, 8; bước 9 do tầng điều phối lượt truyền làm), cộng:
  - trước bước 2: đặt `mtime`, `fsync` `.part` rồi đóng nó;
  - sau bước 3: kiểm lại file giữ chỗ (vẫn là file thường, 0 byte, cùng danh tính); khác đi thì để nguyên nó và chọn tên kế tiếp;
  - ngay trước bước 5: kiểm lại `.part` (file thường, đúng `size` byte, cùng danh tính lúc tạo; Windows: không phải reparse point); khác đi → lỗi, không đổi tên;
  - lỗi sau bước 2 khi đổi tên chưa xong → xóa file giữ chỗ nếu nó vẫn là file thường 0 byte cùng danh tính (gặp `PermissionError` thì thử lại như [protocol.md](protocol.md) mục 4.4), khác đi thì để nguyên; `.part` nằm nguyên trong thư mục tạm. Đổi tên xong rồi thì không dọn gì. Hệ file không có số inode (`st_ino` = 0: FAT, ổ mạng) thì chỉ so loại file và kích thước, và chỉ xóa file giữ chỗ khi `.part` còn trong thư mục tạm: `.part` đã rời đi nghĩa là đổi tên có thể đã xong (lỗi bay ra ngay sau lúc đổi tên thật), và file 0 byte mang tên đích có thể chính là file vừa nhận.
  - Chưa có `.state` nên sập sau bước 2 mà trước bước 5 để lại file 0 byte mang tên thật; lần chạy sau không coi nó là của mình và không đụng tới nó (như khe được chấp nhận ở cuối “Phục hồi tên đích”).
- **`mtime`:** `atime` là giờ hiện tại, `mtime` lấy từ `offer`; đặt trên `.part` trước khi đổi tên (Windows: sau khi đóng handle, vì NTFS có thể đặt lại thời điểm sửa khi đóng handle đã ghi); lỗi, kể cả giá trị làm tràn `time_t` hay FILETIME, thì bỏ qua.
- **Dọn một lượt:** chỉ xóa các `.part` do chính lượt đó tạo mà còn là file thường cùng danh tính lúc tạo (gặp `PermissionError` thì thử lại), rồi `<transfer_id>` khi đã rỗng. Không xóa theo mẫu tên, không đệ quy, không xóa `.teleport`; file lạ trong thư mục tạm được để nguyên.
- **`fsync`:** lỗi `fsync` thư mục bỏ qua (một số hệ file như SMB không hỗ trợ), như `teleport.atomic`; lỗi `fsync` file luôn được báo lên (xử lý: “Lỗi khi đang nhận” ở mục `.state` bên dưới).

### `<file_index>.state` (nhật ký nối lại) ⬜

Sẽ do `teleport.core.resume_state` (sans-IO) mã hóa và đọc lại; `app` đọc/ghi file. Quy tắc bền vững (thứ tự `fsync`, khi nào một khối được tính là đã có): [protocol.md](protocol.md) mục 4.3, 4.4. Nhật ký **chỉ ghi thêm**: không bao giờ ghi lại toàn bộ trạng thái ở mỗi lần đánh dấu (lý do: [ke_hoach.md](ke_hoach.md) mục 3.4).

Mọi số nguyên là số không dấu, big-endian (như khung khối, [protocol.md](protocol.md) mục 4.2). `crc32` là CRC-32 IEEE (`zlib.crc32`) của mọi byte đứng trước nó trong cùng header hay cùng bản ghi.

**Header** (41 byte), ghi một lần bằng `teleport.atomic.write_atomic` khi bắt đầu nhận file:

| Trường | Byte | Ghi chú |
|---|---|---|
| `magic` | 4 | `TPRS` (ASCII) |
| `version` | 1 | Phiên bản định dạng, hiện là 1 |
| `transfer_id` | 16 | 32 hex của `transfer_id` đổi sang byte |
| `file_index` | 4 | |
| `size` | 8 | Kích thước file, byte |
| `block_size` | 4 | |
| `crc32` | 4 | |

Header chỉ để đối chiếu: manifest chỉ nằm ở `transfer.json`. Thiếu byte, sai `magic`, `version` lạ, sai `crc32`, hay một trường khác `transfer.json` → coi như chưa có trạng thái: ghi header mới (đè `.state` cũ bằng `write_atomic`) và gửi `resume` với bitfield toàn 0. Các bản ghi `TARGET`/`RESERVED` cũ bị quên theo, nên không file nào mang tên đích được coi là của lượt truyền này (bảng phục hồi bên dưới).

**Bản ghi**, nối liền nhau sau header:

| Loại | Mã | Bố cục | Khi nào nối |
|---|---|---|---|
| `MARK` | `0x01` | `[u8 loại][u16 n][n × (u32 index ‖ 32 byte sha256)][u32 crc32]`, n ≥ 1 | Mỗi lần đánh dấu (16 khối, hoặc khi đã nhận đủ khối của file): `fsync` `.part` thành công → nối `MARK` → `fsync` `.state` |
| `CLEAR` | `0x02` | `[u8 loại][u16 n][n × u32 index][u32 crc32]`, n ≥ 1 | Lượt đọc lại thấy khối sai ([protocol.md](protocol.md) mục 4.4, bước 3) |
| `CLEAR_ALL` | `0x03` | `[u8 loại][u32 crc32]` | Merkle root sai ([protocol.md](protocol.md) mục 4.4, bước 4) |
| `TARGET` | `0x04` | `[u8 loại][u16 len][len byte tên đích, UTF-8][u32 crc32]` | Đã chọn tên đích: nối `TARGET` → `fsync` `.state` → rồi mới tạo file giữ chỗ bằng `O_CREAT \| O_EXCL` ([protocol.md](protocol.md) mục 4.4, bước 6). Chỉ là ý định, chưa chứng minh file mang tên đó là của mình |
| `RESERVED` | `0x05` | Như `TARGET` | `O_CREAT \| O_EXCL` đã tạo được file giữ chỗ: nối `RESERVED` mang đúng tên đó → `fsync` `.state` |

- `TARGET` và `RESERVED`: 1 ≤ `len` ≤ giới hạn `name` ở [protocol.md](protocol.md) mục 3.3. Bước chọn tên đích bảo đảm tên **kèm hậu tố** ` (n)` vẫn trong giới hạn đó, bằng cách cắt bớt phần tên trước đuôi file. Bộ đọc coi `len` ngoài khoảng này là bản ghi hỏng.
- `CLEAR` và `CLEAR_ALL` không cần `fsync` riêng: mất chúng thì lượt đọc lại cuối hay phép so Merkle root lại bắt đúng lỗi đó, và `fsync` của `MARK` kế tiếp làm chúng bền theo. Cái giá: mất `CLEAR_ALL` thì tốn thêm một lượt đọc lại cả file.

Một `MARK` 16 khối dài 583 byte, tức khoảng 36,4 byte mỗi khối: nhật ký của file 100 GiB chừng 3,6 MiB, tăng tỉ lệ thuận với số khối.

**Lỗi khi đang nhận:**

- Giai đoạn 2 (chưa có `.state`): ghi hay `fsync` `.part` lỗi → bên nhận dừng lượt bằng `cancel{error}`, xóa thư mục tạm, giữ các file đã `verified` (`teleport.core.transfer`) ✅.
- Từ Giai đoạn 3: `fsync` `.part` lỗi → các khối ghi từ lần `fsync` `.part` thành công trước đó coi như chưa nhận: không nối `MARK` cho chúng, kể cả khi `fsync` lần sau thành công (Linux có thể đã bỏ các trang chưa ghi được, rồi không báo lỗi lần nữa). Các khối đó vẫn có bit = 0 nên được gửi lại.
- Ghi hay `fsync` `.state` lỗi → dừng lượt truyền và báo người dùng lỗi ghi đĩa (mvp mục 8 chưa có dòng riêng cho lỗi này). Không bao giờ nối tiếp sau một bản ghi có thể đã cụt: lần tiếp tục đọc lại nhật ký từ đầu và cắt về bản ghi tốt cuối (bên dưới) rồi mới nối.

**Đọc lại** (khi mở lại app): kiểm header, rồi phát lần lượt từng bản ghi từ đầu, bắt đầu từ bitfield toàn 0. Đọc theo từng đoạn, không nạp cả nhật ký vào RAM.

- `MARK`: bật bit và lưu hash của từng index. Index đã có thì bản ghi sau thắng.
- `CLEAR`: tắt bit các index đó. `CLEAR_ALL`: tắt mọi bit.
- `TARGET`: nhớ tên đích và quên mọi `RESERVED` trước đó. `RESERVED`: ghi nhận đã giữ chỗ, chỉ khi tên trùng `TARGET` cuối cùng; khác tên thì bỏ qua.
- Gặp bản ghi đầu tiên bị cụt, sai `crc32`, có loại lạ, `n` = 0, index ≥ số khối của file, `len` ngoài khoảng ở trên, hoặc tên không phải UTF-8 hợp lệ hay đổi đi khi qua bộ làm sạch tên file ([ke_hoach.md](ke_hoach.md) mục 3.3) → dừng ở đó, bỏ phần còn lại: cắt file về cuối bản ghi tốt cuối cùng rồi `fsync`, để bản ghi nối sau không nằm sau rác. Mất điện chỉ làm cụt bản ghi đang ghi dở, nên mất tối đa một `MARK` (≤ 16 khối), vẫn trong giới hạn gửi lại ở [protocol.md](protocol.md) mục 4.3.
- `.state` nằm trên đĩa của chính máy nhận nhưng vẫn được kiểm như dữ liệu không tin cậy, vì chương trình khác cũng ghi được vào thư mục nhận.

**Kết thúc một file**, sau khi lượt đọc lại và Merkle root đều khớp ([protocol.md](protocol.md) mục 4.4, bước 6), theo đúng thứ tự:

1. Nối `TARGET` → `fsync` `.state`.
2. Tạo file giữ chỗ bằng `O_CREAT | O_EXCL`; tên đã có thì chọn tên kế tiếp (` (1)`, ` (2)`…) và quay lại bước 1.
3. `fsync` thư mục nhận, để tên file giữ chỗ bền trên đĩa.
4. Nối `RESERVED` → `fsync` `.state`.
5. `os.replace` `.part` lên file giữ chỗ.
6. `fsync` thư mục nhận.
7. Ghi file đó là đã `verified` vào `transfer.json` (`write_atomic`).
8. Xóa `.state`.
9. Gửi `verified`.

`transfer.json` đã ghi file là `verified` thì thắng mọi thứ khác: lần chạy sau không nhận lại file đó, dù `.state` còn hay đã mất (sập giữa bước 7 và 8 thì chỉ còn việc xóa `.state`).

Ghi chú nền tảng: trên macOS `os.fsync` không xả cache của ổ đĩa, nên các `fsync` ở trên dùng `fcntl.F_FULLFSYNC`, lùi về `os.fsync` khi hệ file không hỗ trợ (ví dụ SMB); Windows không `fsync` được thư mục từ Python, nên bước 3 thay bằng `os.fsync` (FlushFileBuffers) trên handle của chính file giữ chỗ vừa tạo rồi mới đóng handle, còn bước 5 và 6 gộp thành một lần `MoveFileExW(.part, đích, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)`, vẫn thử lại khi `PermissionError` như `os.replace`; mọi handle tới file giữ chỗ phải đóng trước khi đổi tên. Cả ba đã cài ✅ (`teleport.app.storage`, Giai đoạn 2). Vẫn chưa chắc FlushFileBuffers trên file làm bền được mục thư mục trên NTFS và FAT/exFAT.

**Phục hồi tên đích** sau khi đọc lại (khi `transfer.json` chưa ghi file là `verified`). Không bao giờ xóa hay ghi đè một file mà `.state` không chứng minh được là của mình. Xét bảng từ trên xuống và dùng dòng đầu tiên khớp; dòng “đúng `size` byte, không còn `.part`” đứng trước dòng “0 byte” vì thiếu `.part` thì phải xét nó trước (file rỗng có `size` = 0):

| `.state` cho biết | File mang tên đích trên đĩa | Làm gì |
|---|---|---|
| Không có `TARGET` | — | Chọn tên khi tới bước đổi tên, như bình thường |
| `TARGET` không có `RESERVED` khớp | Bất kỳ | Không đụng file mang tên đó; tới bước đổi tên thì chọn tên mới |
| `TARGET` + `RESERVED` | Đúng `size` byte, và không còn `.part` | Đã sập sau `os.replace`: đọc lại file đó, kiểm từng khối theo hash đã lưu; khớp hết thì coi như file đã `verified` và làm tiếp từ bước 6 ở trên |
| `TARGET` + `RESERVED` | 0 byte, và còn `.part` | Dùng lại làm file giữ chỗ; làm lại lượt đọc lại ([protocol.md](protocol.md) mục 4.4, bước 3) rồi mới làm tiếp từ bước 5 (`.part` có thể đã bị sửa trong lúc app tắt) |
| `TARGET` + `RESERVED` | 0 byte, và không còn `.part` | File đó là của máy này: coi như chưa có khối nào (ghi lại header mới, gửi `resume` với bitfield toàn 0) và nhận xong thì dùng lại nó làm file giữ chỗ, không chọn tên mới |
| Mọi trường hợp còn lại (kể cả kiểm ở dòng “đúng `size`” có khối sai) | Bất kỳ | Không đụng file đó, chọn tên mới; còn thiếu `.part` thì coi như chưa có khối nào: ghi lại header mới (để `.state` khớp trạng thái trong RAM) và gửi `resume` với bitfield toàn 0 |

Còn một khe được chấp nhận: sập sau khi `O_CREAT | O_EXCL` tạo file giữ chỗ nhưng trước khi `RESERVED` bền trên đĩa thì file 0 byte mang tên thật bị bỏ lại. Việc dọn file tạm cũ chỉ dọn trong `.teleport/`, nên không đụng file này; nó được để lại cho người dùng.

**Gộp:** kích thước gọn là độ dài của header, cộng `TARGET` cuối cùng (nếu có) rồi `RESERVED` (chỉ khi trùng tên với `TARGET` đó), cộng các `MARK` chứa đúng các khối đang có bit bật (mỗi `MARK` tối đa 65 535 khối). Sau mỗi lần nối và khi đọc lại, nếu `.state` dài hơn 2 × kích thước gọn + 64 KiB thì ghi lại bản gọn bằng `write_atomic`. Nhật ký chỉ dài hơn bản gọn khi có `CLEAR`, `CLEAR_ALL`, khối được đánh dấu lại, hay nhiều `TARGET`/`RESERVED`, nên hiếm khi phải gộp.

- Đóng handle đang nối thêm vào `.state` trước khi gọi `write_atomic` (khi gộp hay khi ghi lại header), vì Windows không `os.replace` được lên file đang mở; xong thì mở lại để nối tiếp.
- Bản gọn ghi `TARGET` trước, rồi mới tới `RESERVED` khớp tên, để đọc lại bản gọn cho đúng kết quả như nhật ký cũ. Bản gọn được sinh từ bitfield và hash đang giữ trong RAM rồi ghi ra theo từng đoạn, không dựng cả bản gọn thành một khối bytes (RAM: [ke_hoach.md](ke_hoach.md) mục 8). `write_atomic` hiện nhận cả nội dung một lần, nên sẽ cần thêm cách ghi theo đoạn.

## 5. Bất đồng bộ

Toàn bộ mạng chạy trên **một event loop `asyncio`**; giao diện Qt dùng chung event loop đó qua `qasync`. Việc chặn (ghi đĩa lớn, băm) được đẩy sang thread bằng `asyncio.to_thread`, để event loop không bao giờ bị đứng.

`teleport.app.storage` là API đồng bộ (chặn), chỉ gọi từ thread; executor và hạn cho mỗi lần ghi hay `fsync` do `app/transfers.py` quyết.

I/O đĩa của truyền file (áp dụng từ P2-6b, khi có `app/transfers.py`): mọi thao tác chạm hệ file (`teleport.app.storage`, `teleport.app.sources`) đi qua một `teleport.app.diskio.DiskExecutor` của node, `DISK_THREADS` = 4 thread daemon, không quá 4 việc cùng lúc. Hạn của một việc tính từ lúc thread bắt đầu nó, không tính lúc xếp hàng; quá hạn thì bên gọi nhận `DiskTimeout` nhưng thread vẫn chạy tới cùng (Python không dừng được thread kẹt trong I/O), nên trước khi đóng fd mà việc đó dùng hay dọn kết quả tới muộn, bên gọi chờ `DiskJob.settled()`. Việc chỉ tốn CPU (SHA-256 payload nhận, `merkle_root`) vẫn dùng `asyncio.to_thread`.

Ngoại lệ có chủ đích: ghi `trusted.json` và `settings.json` chạy đồng bộ trên event loop. File nhỏ (thường < 50 ms, xấu nhất ~250 ms khi phải thử lại `os.replace`), và nhờ vậy trạng thái trong RAM đổi cùng lúc với file, không có `await` xen giữa (ví dụ `Node.unpair`: xóa khỏi kho, đổi danh sách tin cậy của cổng và lấy kết nối của máy đó ra khỏi bảng trong cùng một bước).

## 6. Kiểm thử

- `tests/` chia thư mục theo tầng: `core/`, `trust/`, `net/`… Test ở thư mục gốc `tests/` dành cho các tiện ích dùng chung (`paths`, `lock`, `atomic`), `cli` và quy tắc phân tầng.
- Test cần socket thật được đánh dấu `@pytest.mark.integration`. Chạy nhanh phần còn lại bằng `pytest -m "not integration"`. Mạng chặn multicast: [CONTRIBUTING.md](CONTRIBUTING.md) (`TELEPORT_SKIP_MDNS`).
- Máy trạng thái truyền file (`teleport.core.transfer`): hằng của module là biến cấp module, vá được bằng `monkeypatch`. Nó được kiểm bằng mô phỏng hai `TransferEngine` nối qua kênh giả với thời gian ảo, không socket; bộ mô phỏng theo hợp đồng thực thi ở docstring của module (không gọi engine lồng nhau, hàng FIFO đĩa với luật `RemoveTemp`).
- Mọi test kết nối từ `127.0.0.1`, nên fixture tự động trong `tests/conftest.py` nới **tốc độ và burst** bắt tay của `teleport.core.admission` (không nới giới hạn số kết nối đồng thời). Test cần mặc định thật gắn `@pytest.mark.real_admission_limits`. Node chạy bằng tiến trình con không nhận fixture này, nên luôn chạy với mặc định thật.
- pytest chạy với `asyncio_mode = "auto"` (test `async def` không cần marker) và `--import-mode=importlib` (hai thư mục tầng có file test trùng tên được), cấu hình ở `pyproject.toml`.
- Hằng thời gian chờ của `teleport.net.connection` (kể cả `IDLE_TIMEOUT`: tham số `idle_timeout=None` của `read_*` đọc hằng này lúc gọi), cỡ đoạn `_SEND_CHUNK` / `_READ_CHUNK` và giới hạn của `teleport.core.admission` là biến cấp module, đọc lúc dùng, nên test chỉnh bằng `monkeypatch.setattr`. Riêng `ssl_shutdown_timeout` của transport được đặt lúc mở kết nối từ `CLOSE_TIMEOUT` / `DRAIN_TIMEOUT`, nên test vá hai hằng này **trước** khi tạo `Listener` hay gọi `open_*`. Hằng nào là giá trị mặc định của tham số (keepalive: `ping_interval`, `dead_after`; mDNS: `probe_interval`, `max_services`) thì vá không có tác dụng: test truyền tham số tường minh. `teleport.app.node` gọi `MdnsDiscovery`, `open_control`… qua tên toàn cục của module để test thay được.
- `teleport.app.storage`: các seam là biến cấp module, đọc lúc dùng (`REPLACE_ATTEMPTS`, `REPLACE_DELAY`, `_fs_type_of`, `_MOUNTINFO_PATH`, `_parse_mountinfo`, `_path_limits` (vá trước `ReceiveDir.open`), `_after_reserve` (giả kẻ chen sau khi giữ chỗ), `_move_file_ex` (chỉ Windows), `_full_fsync`); thư viện chuẩn được gọi qua module (`os.open`, `os.replace`, `shutil.disk_usage`, `time.sleep`…), nên test vá được. Nhánh Linux/macOS dùng `dir_fd`. Tạo symlink trên Windows cần quyền: gặp `OSError` thì bỏ qua test; junction tạo bằng `_winapi.CreateJunction`, không cần quyền.
- Seam không socket cho kết nối dữ liệu: test dựng thẳng `Connection(kind=ConnectionKind.DATA, ...)` với `asyncio.StreamReader` thật (tạo **bên trong** loop đang chạy; cấp byte bằng `feed_data` / `feed_eof`) và writer giả có `write`, `drain` (async), `close`, `wait_closed` (async), `transport.abort`, `transport.is_closing` và `get_extra_info` (docstring `Connection.__init__`). Nhờ vậy khung khối, chia đoạn, kiểm đóng và hạn chờ đọc test được bằng `hypothesis` mà không mở socket.
- CLI: test chạy lệnh ngay trong tiến trình qua `run_listen`, `run_pair`, `run_list` (`teleport.cli`). Các cờ ẩn `--host`, `--port`, `--mdns-interface` và `--advertise-address` (chỉ `listen`) chỉ dành cho test (nghe `127.0.0.1`, cổng 0).

Giao thức trên dây: xem [protocol.md](protocol.md).
