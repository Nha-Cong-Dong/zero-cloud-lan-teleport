# Kế hoạch dự án — Zero-Cloud LAN Teleport v1.0

> **Vai trò:** kế hoạch kỹ thuật — làm thế nào, bằng công nghệ gì, theo thứ tự nào, đang tới đâu.
> **Nguồn duy nhất cho:** các quyết định đã chốt, thiết kế kỹ thuật, lộ trình và **trạng thái giai đoạn**, quy trình, kiểm thử, rủi ro kỹ thuật, việc cần chốt.
> **Không chứa:** phạm vi, tính năng, tiêu chí chấp nhận và con số chất lượng → [mvp.md](mvp.md) (tham chiếu theo mã F1–F12, N1–N12). Ý tưởng, nguyên tắc và tầm nhìn → [y_tuong.md](y_tuong.md). Thuật ngữ, quy ước và bản đồ tài liệu → [README.md](README.md).
> **Bản chính thức về kỹ thuật:** đặc tả giao thức [protocol.md](protocol.md) và trạng thái từng module [architecture.md](architecture.md); khi lệch nhau thì hai file đó đúng.
> **Cập nhật:** 2026-10-20

---

## 1. Các quyết định đã chốt

| Câu hỏi | Quyết định |
|---|---|
| Loại dự án | **Sản phẩm phát hành cho người dùng thật**: người dùng phổ thông tải bộ cài về là dùng được; phát hành theo phiên bản; có CI, kiểm thử tự động, tài liệu người dùng, chính sách bảo mật và quyền riêng tư |
| Nền tảng v1.0 | **Desktop ↔ desktop**: Windows là chính; macOS và Linux được CI build/test và có bộ cài |
| Ngôn ngữ chính | **Python 3.12+** cho toàn bộ ứng dụng |
| Ngôn ngữ bổ trợ | **C/C++** và **Java** cho công cụ phụ trợ (mục 4.2), không nằm trên đường găng của v1.0 |
| Giấy phép | Chưa quyết → repo để **Private**; chỉ dùng thư viện MIT/BSD/Apache/LGPL cho phần chạy/đóng gói (công cụ chỉ dùng khi phát triển được phép khác, ví dụ `hypothesis` MPL-2.0); **phải chốt trước khi công khai** |
| Nguồn lực | Làm một mình |
| Thời hạn | Không có hạn cố định → kiểm soát bằng các bản phát hành (mục 5), mốc tham khảo ~15 tuần tới v1.0 |
| Ngôn ngữ giao diện | Tiếng Việt và English |

---

## 2. Khác biệt so với ý tưởng ban đầu

**Python có làm được không? Có.** Những phần nặng (mã hóa TLS, băm SHA-256, ghi đĩa) đều chạy bằng code C bên trong Python (OpenSSL, `hashlib`), nên tốc độ vẫn đạt mức của mạng Wi-Fi/LAN thông thường. Đổi lại cần điều chỉnh vài chỗ so với bản ý tưởng đầu tiên; [y_tuong.md](y_tuong.md) hiện hành đã theo các điều chỉnh này:

| Hạng mục | Ý tưởng ban đầu | Kế hoạch | Lý do |
|---|---|---|---|
| Kênh truyền | WebRTC DataChannel | **TCP + TLS 1.3** (`asyncio` + `ssl` có sẵn) | Trong LAN không cần NAT traversal. Thư viện WebRTC (`aiortc`) và QUIC (`aioquic`) cho Python viết bằng Python thuần nên chậm; TCP + TLS dùng OpenSSL nên nhanh và dễ bảo trì hơn nhiều. |
| Backpressure | Theo dõi bộ đệm gửi | `await writer.drain()` + hàng đợi có giới hạn | Cơ chế có sẵn của `asyncio`: bộ đệm vượt ngưỡng thì `drain()` tự chờ. Kết hợp với cửa sổ TCP, đây chính là backpressure đầu-cuối. |
| Mã hóa | DTLS / AES-GCM | **TLS 1.3** + ghim chứng chỉ sau ghép đôi | TLS 1.3 đã dùng AES-GCM / ChaCha20 bên trong. Rủi ro thật là MITM lúc ghép đôi → xử lý bằng mã xác minh 6 số có nonce cam kết. |
| Gói tin | Header 57 byte mỗi mảnh, kèm hash | Message điều khiển (JSON) + **khung dữ liệu nhị phân** gọn | Hash gửi theo lô trên kênh điều khiển; TCP/TLS đã chống hỏng dữ liệu trên đường truyền. |
| Kích thước mảnh | 64KB hoặc 256KB | **Khối 1 MiB** | Khối lớn giảm số hash, kích thước bitfield và chi phí Python mỗi khối; RAM vẫn chỉ giữ ~10 khối. |
| Giao diện | Web / app nhẹ | **PySide6 (Qt)** | Kéo thả file cho ra đường dẫn thật (đọc thẳng từ đĩa), vẽ radar dễ, `QClipboard` theo dõi clipboard sẵn, giao diện native trên cả 3 hệ điều hành. |
| Nền tảng | Laptop ↔ điện thoại hoặc laptop ↔ laptop | **Desktop ↔ desktop** ở v1.0; điện thoại để sau v1.0 | Làm một mình; app điện thoại là một sản phẩm riêng, dùng lại cùng giao thức. |
| Ghép đôi | Chỉ nhắc cho clipboard | **Bắt buộc ghép đôi** cho cả file lẫn clipboard; có dự phòng nhập IP thủ công | Chống giả mạo thiết bị và chống máy lạ gửi rác trên Wi-Fi chung. |
| RAM | < 100MB tuyệt đối | **RAM tăng thêm khi truyền < 50MB và không tăng theo kích thước file** (mvp N3) | Riêng Python + Qt đã chiếm khoảng 60–90MB lúc khởi động. Điều cần đảm bảo là bộ nhớ không phình ra khi truyền file 10GB. |

---

## 3. Thiết kế kỹ thuật

```
┌────────────────────────── App Teleport (mỗi máy) ──────────────────────────┐
│  ui (PySide6)   radar, lượt truyền, ghép đôi, khay  │  cli   lệnh `teleport` │
│                 ▲ Qt signals (qasync: chung một event loop)                 │
│  app      Node · PairingService · TransferManager · ClipboardSync          │
│  net      tls (TLS 1.3 + mTLS) · stream · discovery (zeroconf) · connection │
│  trust    identity (khóa + chứng chỉ tự ký) · store (thiết bị tin cậy)     │
│  core     framing · messages · pairing_code · blocks · merkle · bitfield · │
│           transfer (máy trạng thái gửi/nhận, nối lại) — sans-IO            │
└─────────────────────────────────────────────────────────────────────────────┘
               ▲  TLS 1.3, mTLS: chỉ thiết bị đã ghép đôi  ▼
                          App Teleport trên máy khác
```

- Phân tầng `core ← trust ← net ← app ← ui / cli`, được kiểm tra tự động trong repo. Logic truyền file và nối lại nằm ở `core` dưới dạng máy trạng thái thuần (sự kiện vào, hành động ra); `net` chỉ đọc/ghi socket.
- Mô hình tiến trình: mỗi thư mục cấu hình một node. Tới `v0.2`, mỗi lần chạy CLI là một node. Từ `v0.3`, giao diện giữ node và CLI nói chuyện với nó qua IPC cục bộ.
- Mô tả chính thức và trạng thái từng module: [architecture.md](architecture.md).

### 3.1. Khám phá & ghép đôi
*(mvp F2, F3)*
- Thư viện `zeroconf` quảng bá dịch vụ `_teleport._tcp.local.` với thuộc tính: `id`, `name`, `port`, `fp` (fingerprint rút gọn), `ver`. Dự phòng: nhập IP thủ công.
- **Danh tính:** mỗi máy có khóa ECDSA P-256 và chứng chỉ X.509 tự ký; fingerprint = SHA-256 của chứng chỉ (DER). Tên thiết bị chỉ để hiển thị.
- **Ghép đôi lần đầu** (kiểu *numeric comparison* của Bluetooth): bên khởi xướng gửi chứng chỉ kèm **cam kết** cho nonce của mình, bên trả lời gửi nonce, bên khởi xướng mới lộ nonce. Mã 6 số tính từ hai fingerprint **và hai nonce**. Nếu mã chỉ tính từ hai fingerprint, kẻ đứng giữa có thể sinh khóa offline tới khi ra mã trùng; có nonce cam kết thì mỗi lần thử chỉ trúng với xác suất 10⁻⁶. Công thức, luồng message `pair_*` và test vector: `protocol.md` mục 5.
- Người dùng thấy mã trùng thì bấm “Xác nhận” **ở cả hai máy** (hết giờ 60 giây) → mỗi máy lưu chứng chỉ và fingerprint của máy kia vào `trusted.json`. Những lần sau, TLS chỉ chấp nhận chứng chỉ đã lưu. Có thể **hủy ghép đôi** bất cứ lúc nào.
- **Xác thực hai chiều:** module `ssl` của Python không cho tự viết hàm kiểm tra chứng chỉ phía server. Phương án: nạp chứng chỉ của các thiết bị tin cậy làm trust anchor cho `SSLContext` của server (mTLS); vì vậy chứng chỉ có dấu hiệu CA bị từ chối. Riêng lần ghép đôi đầu tiên dùng một **listener ghép đôi riêng** không yêu cầu chứng chỉ client, và xác thực ở tầng ứng dụng bằng mã 6 số.
- Lỗi thường gặp cần phát hiện và báo rõ (mvp mục 8): Windows Firewall chặn cổng; Wi-Fi bật **cô lập thiết bị** (thấy nhau qua mDNS nhưng không kết nối được).

### 3.2. Truyền file & backpressure
*(mvp F4, F5, N3)*
- **Khối:** 1 MiB, là đơn vị băm, nối lại và ghi bitfield. File 10GB ≈ 10.240 khối, bitfield ≈ 1,3KB.
- **Bên gửi:**
  ```python
  while block := f.read(BLOCK_SIZE):  # chỉ giữ 1 khối trong RAM
      h = hashlib.sha256(block).digest()  # gom vào lô block_hashes (mục 3.4)
      writer.write(frame_header(idx, len(block)) + block)
      await writer.drain()  # ← backpressure: chờ khi bộ đệm đầy
  ```
- **Bên nhận:** đọc từng khung → băm khối và giữ lại hash vừa tính → đẩy vào `asyncio.Queue(maxsize=8)` → luồng ghi đĩa (`asyncio.to_thread`) ghi vào `<tên>.part` đúng vị trí `idx × BLOCK_SIZE`. Ổ đĩa chậm → hàng đợi đầy → ngừng đọc socket → TCP báo bên gửi chậm lại. Kết quả: RAM tối đa khoảng 8–10 khối, bất kể file lớn cỡ nào.
- Hash của bên gửi tới sau theo lô, nên việc **đối chiếu** diễn ra khi lô `block_hashes` tương ứng tới (mục 3.4), không phải ngay lúc đọc khung.
- **Giới hạn tốc độ gửi** (tùy chọn trong cài đặt): bên gửi chờ thêm giữa các khối theo kiểu token bucket.
- **Hoàn tất:** so khớp Merkle root → `fsync` → đổi tên `.part` → tên thật.

### 3.3. An toàn khi nhận
*(mvp F4, N8 — bắt buộc, không cắt khi trễ tiến độ)*
- Làm sạch tên file trong `offer`: loại bỏ `..`, đường dẫn tuyệt đối, ký tự cấm và tên thiết bị đặc biệt của Windows (`CON`, `NUL`…), để chặn ghi ra ngoài thư mục nhận (path traversal).
- Kiểm tra dung lượng trống trước khi nhận.
- Trùng tên thì tự đặt `ten (1).ext`, không bao giờ ghi đè.
- Luôn hỏi người nhận chấp nhận, trừ khi họ đã tắt “Hỏi trước khi nhận” cho thiết bị đó.
- Mọi message nhận từ máy khác đều không được tin: sai một trường là cả message bị từ chối; có giới hạn kích thước và fuzz test.

### 3.4. Toàn vẹn & nối lại
*(mvp F6, F7, N4, N5)*
- Bên gửi băm từng khối **trong khi gửi**, gửi `block_hashes` theo lô **64 khối** trên kênh điều khiển, và `complete{merkle_root}` ở cuối. Nhờ vậy không phải băm cả file 10GB trước khi bắt đầu.
- Bên nhận băm lại khối khi nhận; khi lô `block_hashes` tới thì đối chiếu: khớp và đã ghi xuống đĩa → bật bit trong bitfield (bitfield chỉ chứa khối **đã xác minh**); sai → báo trong `corrupt` để gửi lại.
- Sau mỗi lô được xác minh, lưu `<tên>.part.state` (JSON: manifest, bitfield dạng base64, các hash đã xác minh) bằng ghi nguyên tử.
- **Nối lại:** mất kết nối → tự thử lại (1s, 2s, 4s… tối đa 30s một lần, trong tối đa 24 giờ) → bên nhận gửi `resume{bitfield}` → bên gửi chỉ gửi những khối có bit = 0.
- **Giới hạn dữ liệu gửi lại:** khối đã nhận nhưng chưa kịp xác minh (lô hash chưa tới) có bit = 0 nên sẽ được gửi lại. Vì vậy lượng gửi lại ≤ **phần chưa nhận + 64 khối (64 MiB)**, cả khi rớt mạng lẫn khi tắt hẳn app nhận. Với file 10GB rớt ở 85%: ≤ 1,5GB + 64 MiB. Muốn giới hạn chặt hơn thì giảm cỡ lô hoặc lưu cả hash bên nhận tự tính (phải sửa `protocol.md` mục 6).
- File nguồn thay đổi (kích thước/mtime khác manifest) → bên gửi gửi `cancel`, báo người dùng.
- File `.part` bị bỏ dở quá 7 ngày thì tự dọn (chỉnh được trong cài đặt).
- **Merkle root:** tính từ danh sách hash các khối (lá) theo cây nhị phân; lẻ nút thì nút cuối được đẩy thẳng lên tầng trên.

### 3.5. Giao thức (tóm tắt)
Đặc tả chính thức: [protocol.md](protocol.md), có **số phiên bản** riêng, độc lập với phiên bản app (hiện là phiên bản 1, dự thảo). Bảng dưới chỉ là tóm tắt; khi lệch nhau thì `protocol.md` đúng. Mọi thay đổi định dạng trên dây phải sửa `protocol.md` trong cùng PR.

Hai bên gửi `hello` ngay sau bắt tay TLS; khác phiên bản giao thức thì đóng kết nối kèm thông báo “hãy cập nhật app”. Mỗi cặp máy đã ghép đôi dùng **2 loại kết nối mTLS**; riêng lúc ghép đôi dùng kết nối tới listener ghép đôi (mục 3.1):
- **Kết nối điều khiển:** message `[4 byte độ dài][JSON UTF-8]`, tối đa 16 MiB. JSON dễ đọc và dễ debug; message điều khiển ít và nhỏ nên không ảnh hưởng tốc độ.
- **Kết nối dữ liệu** (mở cho mỗi file / mỗi lần nối lại): header mở đầu `transfer_id` (16 byte UUID) + `file_index` (UInt32), sau đó là các khung khối.

| Message điều khiển (`type`) | Hướng | Nội dung |
|---|---|---|
| `hello` | ↔ | phiên bản giao thức, `device_id`, tên thiết bị, phiên bản app |
| `pair_request` / `pair_challenge` / `pair_reveal` / `pair_confirm` / `pair_abort` | ↔ | ghép đôi: chứng chỉ, cam kết, hai nonce, xác nhận, lý do hủy — chỉ trên kết nối ghép đôi |
| `offer` | gửi → nhận | `transfer_id`, danh sách file (tên, kích thước, mtime), `block_size` |
| `accept` / `reject` | nhận → gửi | file được chấp nhận / lý do từ chối (`user`, `no_space`, `busy`) |
| `resume` | nhận → gửi | `transfer_id`, `file_index`, bitfield |
| `block_hashes` | gửi → nhận | `transfer_id`, `file_index`, `start_block`, danh sách hash (hex) |
| `complete` | gửi → nhận | `transfer_id`, `file_index`, `merkle_root` |
| `verified` / `corrupt` | nhận → gửi | kết quả, các khối lỗi |
| `cancel` | ↔ | `transfer_id` |
| `clipboard` | ↔ | `kind` (`text` / `image/png`), `sha256`, dữ liệu (văn bản hoặc base64, ≤ 10MB) |
| `ping` / `pong` | ↔ | giữ kết nối |

**Khung dữ liệu nhị phân** (rút gọn từ bảng gói tin trong bản ý tưởng ban đầu), đóng gói bằng `struct.pack("!BII", ...)`:

| Trường | Byte | Kiểu | Ghi chú |
|---|---|---|---|
| `type` | 1 | UInt8 | `0x02` = khối dữ liệu |
| `block_index` | 4 | UInt32 (BE) | Hỗ trợ tới 4 tỷ khối × 1 MiB |
| `length` | 4 | UInt32 (BE) | ≤ 1 MiB; khối cuối có thể ngắn hơn |
| `payload` | biến thiên | bytes | Dữ liệu thô |

### 3.6. Đồng bộ clipboard
*(mvp F8, N6)*
- Dùng `QClipboard.dataChanged` (văn bản + ảnh). Nếu signal không đáng tin trên một hệ điều hành thì chuyển sang polling 500ms. Lõi làm việc qua giao diện `ClipboardBackend`; bản Qt do tầng `ui` cung cấp.
- **Chống vòng lặp:** nhớ hash nội dung vừa nhận; không gửi lại nội dung trùng hash.
- Bật/tắt theo từng thiết bị đã ghép đôi, **mặc định tắt** (clipboard có thể chứa mật khẩu).
- Bỏ qua nội dung được trình quản lý mật khẩu đánh dấu nhạy cảm (`ExcludeClipboardContentFromMonitorProcessing` trên Windows, `org.nspasteboard.ConcealedType` trên macOS).

### 3.7. Quyền riêng tư & “Zero-Cloud”
*(mvp N9)*
- Không telemetry, không tài khoản, không gọi bất kỳ máy chủ nào ngoài LAN.
- Kiểm tra bản cập nhật (đọc GitHub Releases) là **tùy chọn, mặc định tắt**, và là kết nối Internet duy nhất app có thể tạo ra.
- Log chỉ lưu trên máy, không ghi nội dung file hay clipboard. Có nút “Xuất log” để người dùng tự gửi khi báo lỗi.

---

## 4. Công nghệ

### 4.1. Thư viện & công cụ

| Mục đích | Thư viện / công cụ | Giấy phép |
|---|---|---|
| Quản lý môi trường & gói | `venv` + `pip` (có thể chuyển sang `uv`) | — |
| Mạng bất đồng bộ, TLS | `asyncio`, `ssl` (có sẵn) | PSF |
| mDNS | `zeroconf` | LGPL |
| Tạo chứng chỉ | `cryptography` | Apache/BSD |
| Giao diện | `PySide6` + `qasync` | LGPL / BSD |
| Đo RAM | `psutil` | BSD |
| Kiểm thử | `pytest`, `pytest-asyncio`, `hypothesis` | MIT / Apache / MPL |
| Lint & format | `ruff` | MIT |
| Kiểm tra kiểu | `mypy` (từ Giai đoạn 2) | MIT |
| CI / phát hành | GitHub Actions | — |
| Đóng gói | `PyInstaller` → Inno Setup (Windows), `.dmg` (macOS), AppImage (Linux) | GPL có ngoại lệ / tùy công cụ |

### 4.2. Vai trò của C/C++ và Java
Giao thức chỉ gồm JSON và khung nhị phân `struct`, không phụ thuộc Python, nên ngôn ngữ khác nói chuyện được với app. Ba hướng bổ trợ, đều **tùy chọn** và làm sau khi phần lõi Python đã chạy:

| Ngôn ngữ | Việc bổ trợ | Khi nào làm | Giá trị |
|---|---|---|---|
| C++ | **Bộ nhận tham chiếu** (`tools/cpp-receiver`): nhận file theo đúng giao thức, viết bằng C++ thuần + OpenSSL | Giai đoạn 6 | Benchmark tham chiếu: biết nút thắt nằm ở mạng hay ở Python; kiểm chứng đặc tả giao thức đủ rõ để triển khai độc lập |
| C/C++ | **Module mở rộng** (`pybind11`) cho đoạn code nóng, nếu profiling chỉ ra | Chỉ khi Giai đoạn 2 đo thấy Python là nút thắt | Tăng tốc có số liệu trước/sau |
| Java | **Client dòng lệnh** (`tools/java-client`) gửi/nhận file bằng cùng giao thức | Sau v1.0 | Bước đệm cho app Android (Android dùng Java/Kotlin) |

Ghi chú: `hashlib` và `ssl` của Python vốn đã chạy bằng C (OpenSSL), nên nhiều khả năng **không cần** module C++. Đo trước rồi mới tối ưu.

### 4.3. Cấu trúc repo
Repo Git: GitHub `Nha-Cong-Dong/zero-cloud-lan-teleport` (private). Mọi tài liệu, kể cả y_tuong, mvp và ke_hoach, nằm trong `docs/`.
```
teleport/                      # ← gốc repo
  README.md  pyproject.toml    # gốc repo chỉ có 2 file này (ngoài .gitignore)
  .claude/CLAUDE.md            # hướng dẫn cho Claude Code
  .claude/agents/              # đội subagent: truong-nhom, hoi-lai, lap-trinh, viet-test, review, tom-tat
  .github/workflows/ci.yml
  docs/                        # mọi tài liệu .md: README (mục lục, thuật ngữ), y_tuong, mvp, ke_hoach,
                               #   architecture, protocol, CONTRIBUTING, SECURITY, CHANGELOG,
                               #   (sau này) benchmark, hướng dẫn người dùng
  src/teleport/
    core/      framing.py messages.py pairing_code.py blocks.py merkle.py bitfield.py transfer.py
    trust/     identity.py store.py
    net/       tls.py stream.py discovery.py connection.py lock.py
    app/       node.py pairing.py transfers.py clipboard.py
    ui/        app.py radar_widget.py transfer_panel.py pairing_dialog.py
    paths.py   cli.py  __main__.py
  tests/       # core/, trust/, net/, … theo tầng
  tools/       # (tùy chọn) cpp-receiver/, java-client/
```
Đây là cấu trúc dự kiến; module chỉ được tạo khi có code thật.

---

## 5. Lộ trình (~15 tuần tới v1.0)

Nguyên tắc: **làm bằng dòng lệnh (CLI) trước, giao diện sau.** Phần lõi có test chạy được thì gắn UI mới nhanh và ít lỗi. Làm một mình và không có hạn cố định thì rủi ro lớn nhất là kéo dài mãi hoặc ôm thêm tính năng, nên mỗi bản phát hành là một **bản chạy được, dùng được**, gắn tag Git và có ghi chú trong `CHANGELOG.md`.

### 5.1. Tổng quan & trạng thái

Đây là **nơi duy nhất ghi trạng thái giai đoạn**. Trạng thái từng module xem [architecture.md](architecture.md).

| # | Giai đoạn | Thời lượng | Hết tuần | Bản phát hành | Tính năng (mvp) | Người dùng làm được gì | Trạng thái |
|---|---|---|---|---|---|---|---|
| 0 | Nền tảng, repo, CI | 1 tuần | 1 | — | — | — | ✅ Đã xong |
| 1 | Khám phá & ghép đôi | 2 tuần | 3 | — | F2, F3 (lõi + CLI), F12 một phần | Thấy máy khác và ghép đôi qua CLI | ⏳ Đang làm (nền ghép đôi không mạng đã merge) |
| 2 | Truyền file, backpressure, an toàn khi nhận | 3 tuần | 6 | `v0.1.0-alpha` | F4, F5 (CLI), F12 | Gửi file lớn qua CLI giữa 2 máy đã ghép đôi | ⬜ Chưa làm |
| 3 | Merkle & tự nối lại | 3 tuần | 9 | `v0.2.0-alpha` | F6, F7 | Rớt mạng vẫn tự nối lại, chỉ gửi phần còn thiếu. **Bản CLI đầu tiên đáng cho người dùng kỹ thuật dùng thử** | ⬜ Chưa làm |
| 4 | Giao diện, khay hệ thống, đa ngôn ngữ | 2 tuần | 11 | `v0.3.0-beta` | F2–F5 (giao diện), F9, F10, F11 | Dùng hoàn toàn bằng giao diện: radar, kéo thả, ghép đôi | ⬜ Chưa làm |
| 5 | Đồng bộ clipboard | 1 tuần | 12 | `v0.4.0-beta` | F8 | Đồng bộ clipboard | ⬜ Chưa làm |
| 6 | Hoàn thiện, bộ cài, phát hành | 3 tuần | 15 | `v1.0.0` | F1, toàn bộ N1–N12 | Bộ cài 3 hệ điều hành, tài liệu người dùng, đã thử với người dùng thật | ⬜ Chưa làm |

Phiên bản app đánh số theo [SemVer](https://semver.org/lang/vi/); phiên bản giao thức đánh số riêng (mục 3.5). Tiến độ được ghi lại qua commit, GitHub Issues/Milestones và mục `[Unreleased]` của `CHANGELOG.md`.

### 5.2. Chi tiết từng giai đoạn

#### Giai đoạn 0 — Nền tảng, repo, CI ✅
- Đã có nền C/C++/Java nên chỉ cần tập trung vào phần riêng của Python: `async`/`await` và `asyncio` (khác mô hình thread của Java), `struct` (tương tự `struct` trong C / `ByteBuffer` trong Java), module `ssl`, `pytest`.
- Bài tập khởi động: chat TCP giữa 2 máy trong LAN qua TLS.
- Dựng `pyproject.toml`, cấu trúc thư mục, repo GitHub, CI chạy `ruff` + `pytest` trên Windows/macOS/Linux.
- **Xong khi:** 2 máy chat được với nhau qua TLS; CI xanh trên cả 3 hệ điều hành.

#### Giai đoạn 1 — Khám phá & ghép đôi ⏳
- ✅ mTLS với chứng chỉ tự ký được kiểm chứng (`net/tls.py`).
- ✅ Nền ghép đôi không mạng (PR #1): `core/messages.py` (`hello`, `pair_*`), `core/pairing_code.py` (nonce cam kết), `trust/identity.py` (kiểm tra chứng chỉ máy khác), `trust/store.py` (`trusted.json`), `net/lock.py` (một node mỗi thư mục cấu hình).
- `net/discovery.py` (zeroconf), `net/connection.py` (trao đổi `hello`), listener ghép đôi.
- `app/node.py` + `app/pairing.py`: ghép các thành phần, luồng ghép đôi, hủy ghép đôi.
- CLI: `teleport list`, `teleport listen`, `teleport pair <tên>`, `teleport unpair <tên>`.
- Cập nhật `protocol.md` (mục 5 đã có công thức và test vector; phần trao đổi trên mạng chưa cài đặt).
- **Xong khi:** đạt N1 trên 2 máy thật; ghép đôi xong thì tự nhận ra nhau sau khi khởi động lại; máy chưa ghép đôi không kết nối được theo cả hai chiều.

#### Giai đoạn 2 — Truyền file, backpressure, an toàn khi nhận
- `core/blocks.py`: khung dữ liệu nhị phân; `core/messages.py`: thêm các message truyền file, kiểm tra hợp lệ mọi trường.
- `core/transfer.py`: máy trạng thái gửi/nhận (`offer`/`accept`, nhiều file, hủy), không I/O.
- `app/transfers.py`: chạy máy trạng thái trên kết nối thật, đọc/ghi `.part` theo khối.
- An toàn khi nhận (mục 3.3); hủy ở cả hai bên (máy nhận xóa `.part`); giới hạn tốc độ gửi.
- CLI: `teleport send <file...> --to <tên>` kèm tiến độ và tốc độ, `teleport receive`; mã thoát khác 0 khi lỗi; `--json`.
- Build thử CLI bằng PyInstaller để sớm phát hiện vấn đề đóng gói / antivirus.
- **Xong khi:** truyền file 10GB thành công; đạt N3 (biểu đồ RSS phẳng, ổ đích chậm mà RAM không tăng); test path traversal (`../../x`, `C:\Windows\x`, `CON`) đều bị chặn. → **`v0.1.0-alpha`**.

#### Giai đoạn 3 — Merkle & tự nối lại
- `core/merkle.py`, `core/bitfield.py`; mở rộng `core/transfer.py` với `block_hashes`, `complete`, `resume`, `verified`/`corrupt`; lưu `.part.state` ở `app/transfers.py`.
- Tự kết nối lại với backoff; phát hiện file nguồn thay đổi; dọn `.part` cũ.
- **Xong khi:** đạt mọi AC của F6, F7 và N4, N5, cụ thể: (a) tắt Wi-Fi ở 85% → bật lại → hoàn tất, gửi lại ≤ 15% + 64 MiB; (b) sửa 1 byte trong `.part` → phát hiện và gửi lại đúng 1 khối; (c) tắt hẳn app nhận rồi mở lại → vẫn nối lại được; (d) test `hypothesis` ngắt kết nối ngẫu nhiên → file cuối luôn đúng. → **`v0.2.0-alpha`**.

#### Giai đoạn 4 — Giao diện, khay hệ thống, đa ngôn ngữ
- Cửa sổ chính theo wireframe mvp mục 7: radar có vòng quét động, mỗi thiết bị là một chấm có tên, biểu tượng hệ điều hành và trạng thái; nút “Nhập IP thủ công”.
- Kéo thả file vào chấm (hoặc chuột phải → “Gửi file…”); hộp thoại chấp nhận ở máy nhận; danh sách lượt truyền với tiến độ/tốc độ/thời gian còn lại/trạng thái và nút hủy; thông báo hệ thống khi nhận xong; lịch sử 100 lượt.
- Hộp thoại ghép đôi; màn hình cài đặt đủ các mục F10.
- Các thông báo lỗi theo mvp mục 8.
- Khay hệ thống, chạy nền (F9); chuyển node sang tiến trình giao diện, CLI nói chuyện qua IPC cục bộ.
- Tiếng Việt và English bằng Qt Linguist (F11); dùng được bằng bàn phím, theo chế độ sáng/tối (N12).
- Đo RAM khi rảnh (N3) ngay ở bản này.
- **Xong khi:** dùng trọn vẹn không cần dòng lệnh; đạt AC giao diện của F2–F5, F9–F11. → **`v0.3.0-beta`**.

#### Giai đoạn 5 — Đồng bộ clipboard
- Văn bản và ảnh chụp màn hình; chống vòng lặp; công tắc theo thiết bị; bỏ qua nội dung nhạy cảm; biểu tượng khay báo vừa nhận (mục 3.6).
- **Xong khi:** đạt mọi AC của F8 và N6. → **`v0.4.0-beta`**.

#### Giai đoạn 6 — Hoàn thiện & phát hành v1.0
- Bộ cài: Inno Setup (Windows, tự tạo rule tường lửa cho mạng Private), `.dmg` (macOS), AppImage (Linux), build tự động bằng GitHub Actions khi gắn tag; lần chạy đầu theo F1; đạt N11.
- Ký mã (code signing) cho Windows để giảm cảnh báo SmartScreen. Chi phí chứng chỉ cần cân nhắc; chưa có thì ghi rõ hướng dẫn “More info → Run anyway”.
- Benchmark công bố ở `docs/benchmark.md` (file mới): tốc độ (so với SMB và Google Drive), RAM theo thời gian, thời gian phát hiện, lượng dữ liệu gửi lại khi nối lại. (Tùy chọn) so sánh với bộ nhận C++.
- Tài liệu người dùng: cài đặt, ghép đôi, khắc phục sự cố (tường lửa, cô lập Wi-Fi). `SECURITY.md`, chính sách quyền riêng tư.
- Thử nghiệm với người dùng thật (mvp mục 9.1), sửa lỗi; test soak (N7).
- Chốt giấy phép trước khi công khai repo.
- **Xong khi:** đạt toàn bộ tiêu chí phát hành ở mvp mục 12. → **`v1.0.0`**.

### 5.3. Sau v1.0
1. App Android (Java/Kotlin, dùng cùng giao thức): mDNS cần `MulticastLock`, clipboard qua share-sheet.
2. Chế độ khách qua trình duyệt cho điện thoại không cài app (thứ tự với mục 1 chưa chốt, xem mục 9).
3. Gửi tới nhiều thiết bị cùng lúc, gửi/đồng bộ thư mục.
4. App iOS.

Ý tưởng mới trong lúc làm v1.0 ghi vào GitHub Issues với nhãn `after-1.0`.

---

## 6. Quy trình phát triển
Bản chính thức: [CONTRIBUTING.md](CONTRIBUTING.md). Tóm tắt:
- **Nhánh:** `main` luôn chạy được và được bảo vệ (chỉ merge qua Pull Request khi CI xanh). Mỗi việc làm trên nhánh riêng `feat/...`, `fix/...`.
- **Commit:** theo [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `test:`, `chore:`), giúp viết `CHANGELOG.md` dễ dàng.
- **CI** (`.github/workflows/ci.yml`): `ruff check`, `ruff format --check`, `pytest` trên Windows/macOS/Linux × Python 3.12/3.14 (bản thấp nhất và mới nhất được hỗ trợ). Từ Giai đoạn 2 thêm `mypy`.
- **Đội subagent** trong `.claude/agents/`: lập kế hoạch → làm rõ → lập trình + viết test song song → review → chốt → tóm tắt. Chỉ commit và push khi người dùng đồng ý.
- **Phát hành:** gắn tag `vX.Y.Z` → GitHub Actions build bộ cài và tạo GitHub Release kèm ghi chú từ `CHANGELOG.md`.
- **Theo dõi công việc:** GitHub Issues + Milestones tương ứng từng bản phát hành ở mục 5.1.

---

## 7. Kiểm thử

| Loại | Nội dung | Phục vụ |
|---|---|---|
| Unit test | Đóng/mở khung, message, mã ghép đôi (test vector), Merkle root (so với kết quả tính tay trên file nhỏ), bitfield, đọc/ghi state, làm sạch tên file | F3, F4, F7 |
| Tích hợp | 2 node trên cùng máy qua `127.0.0.1` với 2 thư mục cấu hình khác nhau; proxy TCP nhỏ tự viết để cắt kết nối giữa chừng hoặc làm chậm đường truyền | F4–F6, N4 |
| Property test (`hypothesis`) | Ngắt tại vị trí ngẫu nhiên, nối lại nhiều lần → hash file cuối luôn khớp. Fuzz bộ đọc message bằng dữ liệu ngẫu nhiên | N5, N8 |
| Benchmark | Tốc độ so với SMB, RSS theo thời gian, thời gian phát hiện, byte gửi lại | N1–N4 |
| Soak test | 100 lượt truyền liên tiếp, kiểm tra không rò bộ nhớ, không còn file tạm | N7 |
| Thủ công trước mỗi bản beta/1.0 | 2 máy thật trên Wi-Fi nhà, văn phòng, quán cà phê; ma trận Windows 10/11, macOS, Ubuntu; giao diện, trợ năng | N10, N12, AC giao diện |

Test mở socket thật được đánh dấu `@pytest.mark.integration`.

---

## 8. Rủi ro kỹ thuật & thực thi

Rủi ro về sản phẩm và thị trường (cô lập Wi-Fi, SmartScreen, cạnh tranh, thứ tự cắt giảm khi trễ): mvp mục 14.

| Rủi ro | Giảm thiểu |
|---|---|
| Mới làm quen `asyncio` | Giai đoạn 0 dành riêng để học; làm CLI trước UI |
| Trộn Qt với asyncio phức tạp | Dùng `qasync`; lõi không phụ thuộc Qt nên test riêng được |
| Python chậm hơn mong đợi (N2) | Đo sớm ở Giai đoạn 2; tăng kích thước khối hoặc bộ đệm socket; module C++ nếu thật sự cần |
| Python + Qt khó đạt RAM rảnh < 150MB (N3) | Đo sớm từ `v0.3.0-beta`; tối ưu, hoặc nới yêu cầu có lý do và sửa N3 trong mvp.md |
| Bộ cài PyInstaller nặng (N11) hoặc bị antivirus báo nhầm | Build thử từ Giai đoạn 2; gửi mẫu cho các hãng antivirus nếu bị báo nhầm |
| Windows Firewall chặn | Bộ cài tạo rule; app phát hiện và hướng dẫn khi bị chặn |
| Lỗ hổng bảo mật (path traversal, dữ liệu độc hại từ máy khác) | Kiểm tra hợp lệ mọi trường nhận được; fuzz test; `SECURITY.md` để nhận báo lỗi |
| Làm một mình, không có hạn → kéo dài, ôm thêm tính năng | Bám các bản phát hành ở mục 5.1; ý tưởng mới ghi Issues nhãn `after-1.0`; nếu trễ thì cắt theo thứ tự ở mvp mục 14 |
| Chỉ có 1 máy để thử | Chạy 2 node trên cùng máy qua `127.0.0.1` cho phát triển hằng ngày; mượn máy thứ 2 hoặc dùng máy ảo (VirtualBox, mạng bridged) cho test LAN thật |

---

## 9. Việc cần chốt

Đây là danh sách câu hỏi mở duy nhất của dự án.

1. **Giấy phép** (trước khi công khai repo): MIT/Apache-2.0 nếu muốn cộng đồng dùng rộng rãi; GPL-3.0 nếu muốn bản phái sinh cũng phải mở mã; đóng nếu định thương mại hóa.
   Công khai repo thì cả `docs/y_tuong.md`, `docs/mvp.md`, `docs/ke_hoach.md` cũng công khai: rà lại nội dung không muốn chia sẻ (ngân sách, kế hoạch thương mại) trước khi đổi sang Public.
2. **Tên thương mại** và biểu tượng app (hiện dùng tên tạm “Teleport”). Cần kiểm tra trùng tên với sản phẩm khác.
3. **Ngân sách ký mã** cho Windows/macOS.
4. **Chế độ khách cho điện thoại** (qua trình duyệt) có làm ngay sau v1.0, trước app Android, hay không.
