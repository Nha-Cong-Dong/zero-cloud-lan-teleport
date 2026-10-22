# MVP — Zero-Cloud LAN Teleport v1.0

> **Vai trò:** đặc tả sản phẩm — giải quyết vấn đề gì, cho ai, gồm những gì, trông ra sao, đạt chuẩn nào thì được phát hành.
> **Nguồn duy nhất cho:** phạm vi MVP, tính năng và tiêu chí chấp nhận (F1–F12), yêu cầu phi chức năng (N1–N12), thông báo lỗi, tiêu chí phát hành.
> **Không chứa:** thiết kế kỹ thuật, lộ trình và tiến độ → [ke_hoach.md](ke_hoach.md). Ý tưởng, nguyên tắc và tầm nhìn → [y_tuong.md](y_tuong.md). Thuật ngữ, quy ước và bản đồ tài liệu → [README.md](README.md).
> **Cập nhật:** 2026-10-20

---

## 1. Tóm tắt một trang

**Teleport** là ứng dụng desktop giúp chuyển file nặng hàng chục GB và đồng bộ clipboard giữa các máy tính **trong cùng mạng Wi-Fi/LAN**. Dữ liệu đi thẳng từ máy này sang máy kia, **không qua cloud, không cần tài khoản**.

| | |
|---|---|
| **Cho ai** | Người làm việc với nhiều máy tính hoặc ngồi cạnh đồng nghiệp: lập trình viên, designer, dựng video, văn phòng nhỏ, gia đình |
| **Vấn đề** | Chuyển file giữa hai máy cách nhau 20cm vẫn phải qua Zalo (nén ảnh, giới hạn dung lượng), Google Drive (upload rồi download, tốn gấp đôi thời gian), USB (phải tìm, cắm, rút) hoặc chia sẻ thư mục Windows (cấu hình phức tạp) |
| **Giải pháp** | Mở app → máy kia tự hiện trên radar → kéo file thả vào → xong |
| **Khác biệt** | Rớt mạng giữa chừng vẫn **tự nối lại và chỉ gửi phần còn thiếu**; kiểm tra toàn vẹn từng khối; đồng bộ clipboard; không tài khoản, không telemetry |
| **Nền tảng** | Windows 10/11 (chính), macOS 12+, Ubuntu 22.04+ |
| **Mô hình** | Ứng dụng cài trên máy, hoạt động hoàn toàn offline trong mạng nội bộ |

---

## 2. Vấn đề & người dùng mục tiêu

### 2.1. Chân dung người dùng

| Người dùng | Bối cảnh | Nhu cầu chính | Hiện đang làm |
|---|---|---|---|
| **Minh — lập trình viên** | Laptop công ty + PC ở nhà, hay chuyển bản build 2–5GB, dump database | Nhanh, không giới hạn dung lượng, không phải đăng nhập tài khoản cá nhân lên máy công ty | USB hoặc Google Drive |
| **Lan — dựng video** | Quay xong copy footage 30–80GB từ laptop sang máy dựng | Ổn định với file cực lớn, rớt Wi-Fi không phải gửi lại từ đầu | Ổ cứng rời |
| **Văn phòng 5 người** | Gửi tài liệu, ảnh chụp màn hình qua lại cả ngày | Gửi nhanh cho người ngồi cạnh, copy đoạn văn từ máy này dán sang máy kia | Zalo, email cho chính mình |

### 2.2. Vì sao giải pháp hiện tại chưa ổn

| Cách | Tốc độ | File lớn | Riêng tư | Dễ dùng | Rớt mạng |
|---|---|---|---|---|---|
| Zalo / Messenger | Phụ thuộc Internet | ❌ giới hạn, nén ảnh | ❌ qua máy chủ, phải đăng nhập | ✅ | Gửi lại từ đầu |
| Google Drive | Upload + download ≈ gấp đôi | ⚠️ chậm | ⚠️ lưu trên cloud | ✅ | Tùy |
| USB | Nhanh | ✅ | ✅ | ⚠️ phải có USB, cắm rút | — |
| Chia sẻ thư mục Windows (SMB) | Nhanh | ✅ | ⚠️ cấu hình quyền phức tạp | ❌ | Gửi lại từ đầu |
| **Teleport** | **Tối đa tốc độ LAN** | **✅ hàng chục GB** | **✅ mã hóa, không cloud** | **✅ kéo thả** | **✅ chỉ gửi phần thiếu** |

---

## 3. Giá trị cốt lõi

Ý nghĩa từng giá trị: [y_tuong.md](y_tuong.md) mục 3. MVP đo từng giá trị bằng các tính năng và yêu cầu sau:

| Giá trị | Tính năng | Yêu cầu đo được |
|---|---|---|
| **Zero-Friction** | F1, F2, F3 | N1, N11, mục 9.1 |
| **Zero-Cloud** | F8, F10 | N8, N9 |
| **Zero-RAM-Overflow** | F4 | N3 |
| **Không sợ rớt mạng** | F6, F7 | N4, N5 |

---

## 4. Phạm vi MVP

| ✅ Có trong MVP (v1.0) | ❌ Không có trong MVP |
|---|---|
| Ứng dụng desktop Windows / macOS / Linux | Ứng dụng điện thoại Android / iOS |
| Tự khám phá thiết bị qua mDNS + nhập IP thủ công | Truyền qua Internet / khác mạng |
| Ghép đôi bằng mã 6 số | Tài khoản, đăng nhập |
| Gửi một hoặc nhiều file (kéo thả hoặc chọn file) | Gửi cả thư mục, đồng bộ thư mục |
| Hỏi người nhận chấp nhận / từ chối | Gửi một lúc tới nhiều thiết bị |
| Tự nối lại sau rớt mạng, kể cả khi đã tắt app | Chế độ khách qua trình duyệt |
| Kiểm tra toàn vẹn bằng SHA-256 Merkle | Nén dữ liệu khi truyền |
| Đồng bộ clipboard văn bản và ảnh | Đồng bộ clipboard dạng file |
| Khay hệ thống, chạy nền, khởi động cùng máy | Tự động cập nhật ngầm |
| Giao diện Tiếng Việt + English | Ngôn ngữ khác |
| Dòng lệnh (CLI) cho người dùng nâng cao | Plugin / API cho bên thứ ba |

Thứ tự làm các mục “không có” sau v1.0: [ke_hoach.md](ke_hoach.md) mục 5.3. Nếu trễ tiến độ, một số mục “có” được hoãn theo thứ tự ở mục 14.

---

## 5. Tính năng & tiêu chí chấp nhận

Mỗi tính năng có các user story (US) và tiêu chí chấp nhận (AC). Một tính năng chỉ được coi là xong khi **mọi AC đều đạt** và có test tự động tương ứng (trừ AC về giao diện thì kiểm thủ công). Giai đoạn làm từng tính năng: [ke_hoach.md](ke_hoach.md) mục 5.

### F1. Cài đặt & lần chạy đầu
- **US-01:** Là người dùng mới, tôi muốn cài app và dùng được ngay mà không phải cấu hình gì.
- **AC:**
  - Bộ cài Windows ≤ 80MB, cài xong không cần khởi động lại máy; bộ cài tự tạo rule tường lửa cho mạng Private.
  - Lần chạy đầu chỉ hỏi 2 thứ: **tên thiết bị** (mặc định = tên máy) và **thư mục nhận** (mặc định = `Downloads/Teleport`), kèm câu hỏi có khởi động cùng hệ điều hành không.
  - Từ lúc mở bộ cài tới lúc gửi xong file đầu tiên mất < 2 phút với người chưa từng dùng.

### F2. Radar khám phá thiết bị
- **US-02:** Tôi muốn thấy các máy đang mở Teleport trong cùng mạng mà không phải gõ IP.
- **US-03:** Khi mạng chặn mDNS, tôi vẫn muốn kết nối bằng cách nhập IP.
- **AC:**
  - Thiết bị mới xuất hiện trên radar trong < 3 giây; thiết bị thoát app biến mất trong < 10 giây; thiết bị mất mạng đột ngột hoặc ngủ biến mất trong < 2 phút.
  - Mỗi thiết bị hiển thị tên, biểu tượng hệ điều hành, trạng thái (đã ghép đôi / chưa ghép đôi / đang bận).
  - Nút “Nhập IP thủ công” luôn có trên màn hình radar.
  - Thấy thiết bị qua mDNS mà không kết nối được → báo lỗi “Wi-Fi chặn thiết bị” (mục 8).

### F3. Ghép đôi
- **US-04:** Lần đầu gửi tới một máy, tôi muốn chắc chắn mình đang gửi đúng máy, không phải kẻ giả mạo.
- **US-05:** Tôi muốn hủy ghép đôi một thiết bị không còn tin tưởng.
- **AC:**
  - Bấm vào thiết bị chưa ghép đôi → hai máy cùng hiện **mã 6 số**; người dùng xác nhận trùng khớp ở **cả hai máy** thì mới ghép đôi xong. Không xác nhận trong 60 giây thì phiên ghép đôi tự hủy.
  - Kẻ đứng giữa không thể chuẩn bị trước để ra mã trùng: mỗi lần thử giả mạo chỉ có xác suất trúng 1/1.000.000.
  - Sau khi ghép đôi, hai máy tự nhận ra nhau sau khi khởi động lại, không hỏi lại.
  - Máy chưa ghép đôi không thể gửi file hay clipboard tới máy này, theo cả hai chiều.
  - Chứng chỉ của một thiết bị đã ghép đôi bị thay đổi → chặn kết nối và cảnh báo (mục 8).
  - Có danh sách “Thiết bị tin cậy” trong cài đặt, có nút hủy ghép đôi. Hủy ghép đôi có hiệu lực ngay: kết nối đang mở với máy đó bị đóng; máy đó thấy thông báo “từ chối kết nối từ máy này” (mục 8).
  - Máy đã ghép đôi cài lại Teleport hiện như một thiết bị mới chưa ghép đôi; app gợi ý ghép đôi lại và đề nghị xóa mục cũ trùng tên (mục 8).
  - Hạn chế máy lạ làm phiền: chỉ một phiên ghép đôi tại một thời điểm; sau mỗi lần từ chối hay hết giờ, máy đó phải chờ lâu dần (10 giây, 20 giây… tối đa 10 phút) mới gửi được yêu cầu mới. Đây là mức tốt nhất có thể trong LAN, không chống được máy cố tình đổi địa chỉ liên tục; ghép đôi vẫn an toàn vì cần xác nhận mã ở cả hai máy.
  - Máy đang chờ ghép đôi hiện tối đa **5 mã mỗi phút** (gộp mọi máy gửi yêu cầu); quá thì yêu cầu mới nhận “chưa sẵn sàng ghép đôi” (mục 8). Từ mã thứ **3** trong 1 phút, máy đó cảnh báo có thể có người đang thử giả mạo (mục 8). Nhờ vậy kẻ đứng giữa chỉ thử được vài mã trong lúc người dùng chờ xác nhận.
  - Không ghép đôi đè lên một thiết bị tin cậy có cùng mã thiết bị nhưng danh tính khác; phải hủy ghép đôi mục cũ trước, và khi đó cài đặt theo thiết bị cũ cũng bị xóa.

### F4. Gửi file
- **US-06:** Tôi muốn kéo một hoặc nhiều file thả vào thiết bị trên radar để gửi.
- **US-07:** Là người nhận, tôi muốn biết ai gửi gì và được quyền từ chối.
- **AC:**
  - Kéo thả, hoặc bấm chuột phải vào thiết bị → “Gửi file…”, đều được; hỗ trợ chọn nhiều file.
  - Máy nhận hiện hộp thoại: tên người gửi, danh sách file, tổng dung lượng, nút **Nhận** / **Từ chối**. Có tùy chọn “Luôn nhận từ thiết bị này” (tương đương tắt “Hỏi trước khi nhận” của thiết bị đó trong cài đặt).
  - Không đủ dung lượng trống → máy nhận tự từ chối ngay, máy gửi được báo cần bao nhiêu và còn bao nhiêu.
  - Trùng tên file → tự đặt `ten (1).ext`, không bao giờ ghi đè.
  - Tên file chứa `..`, đường dẫn tuyệt đối, ký tự cấm hay tên thiết bị đặc biệt của Windows (`CON`, `NUL`…) → được làm sạch, file luôn nằm trong thư mục nhận.
  - File chỉ xuất hiện với tên thật **sau khi** đã kiểm tra toàn vẹn xong; trong lúc nhận, dữ liệu nằm trong thư mục tạm ẩn `.teleport` bên trong thư mục nhận.

### F5. Theo dõi & quản lý lượt truyền
- **US-08:** Tôi muốn biết đang gửi tới đâu, còn bao lâu, và hủy được khi cần.
- **AC:**
  - Mỗi lượt truyền hiển thị: % hoàn thành, tốc độ hiện tại, thời gian còn lại, trạng thái (đang xếp hàng / đang chờ chấp nhận / đang gửi / đang nối lại / đang kiểm tra / xong / lỗi / đã hủy). Mỗi chiều giữa hai máy chạy một lượt truyền tại một thời điểm; lượt khác xếp hàng.
  - Nút **Hủy** ở cả hai bên; hủy thì máy nhận xóa file tạm.
  - Nhận xong có thông báo hệ thống; bấm vào thông báo mở thư mục chứa file.
  - Lịch sử 100 lượt truyền gần nhất, lưu trên máy, xóa được.

### F6. Tự nối lại khi rớt mạng
- **US-09:** Wi-Fi chập chờn hoặc tôi gập laptop giữa chừng, tôi không muốn gửi lại từ đầu.
- **AC:**
  - Mất kết nối → trạng thái chuyển “Đang nối lại…”, tự thử lại liên tục (1s, 2s, 4s… tối đa 30s một lần) trong 24 giờ.
  - Có mạng lại → tiếp tục từ khối còn thiếu. Lượng dữ liệu gửi lại ≤ phần chưa nhận + 64 MiB.
  - Tắt hẳn app nhận (hoặc khởi động lại máy) rồi mở lại → vẫn nối lại được, cùng giới hạn gửi lại như trên.
  - File nguồn bị sửa trong lúc gián đoạn hoặc trong lúc gửi → hủy lượt truyền, báo rõ lý do.
  - Tắt app **gửi** thì lượt truyền không nối lại được: máy nhận báo “máy gửi đã hủy” và dọn file tạm.
  - File tạm bỏ dở quá 7 ngày được tự dọn (chỉnh được trong cài đặt).

> Vì sao là 64 MiB: khối đã nhận nhưng chưa kịp ghi bền xuống đĩa (máy nhận `fsync` mỗi 16 khối 1 MiB, cộng hàng đợi ghi và bộ đệm mạng) phải gửi lại. Xem [ke_hoach.md](ke_hoach.md) mục 3.4.

### F7. Kiểm tra toàn vẹn
- **US-10:** Tôi muốn chắc chắn file nhận được giống hệt file gốc.
- **AC:**
  - Mỗi khối 1 MiB được kiểm tra SHA-256; khối sai được gửi lại tự động.
  - Cuối lượt truyền, máy nhận **đọc lại toàn bộ file từ đĩa** và so từng khối, rồi so khớp Merkle root; khớp thì mới đổi sang tên thật.
  - Test: sửa 1 byte bất kỳ trong file tạm khi đang tạm dừng → phát hiện và gửi lại đúng khối đó.

### F8. Đồng bộ clipboard
- **US-11:** Tôi muốn copy đoạn văn hoặc ảnh chụp màn hình trên máy này rồi dán ngay trên máy kia.
- **AC:**
  - Bật/tắt riêng cho từng thiết bị đã ghép đôi; **mặc định tắt**.
  - Văn bản tới máy kia trong < 500ms; ảnh ≤ 10MB trong < 2 giây trên Wi-Fi 5.
  - Bật hai chiều cùng lúc không gây vòng lặp vô hạn.
  - Bỏ qua nội dung mà trình quản lý mật khẩu đánh dấu nhạy cảm.
  - Có biểu tượng trên khay báo vừa nhận clipboard từ máy nào.

### F9. Khay hệ thống & chạy nền
- **US-12:** Tôi muốn app luôn sẵn sàng nhận mà không chiếm chỗ trên thanh tác vụ.
- **AC:**
  - Đóng cửa sổ thì app thu xuống khay, vẫn nhận file và clipboard.
  - Menu khay: Mở Teleport, Gửi file…, Tạm dừng nhận, Thoát.
  - Tùy chọn khởi động cùng hệ điều hành (bật nếu người dùng đồng ý ở lần chạy đầu).
  - Khi chạy nền không truyền gì: CPU ~0%, RAM < 150MB.

### F10. Cài đặt
- **AC:** có đủ các mục sau, đổi xong có hiệu lực ngay:
  - Tên thiết bị · Thư mục nhận · Ngôn ngữ · Khởi động cùng hệ điều hành
  - Theo từng thiết bị tin cậy: Hỏi trước khi nhận · Đồng bộ clipboard · Hủy ghép đôi
  - Giới hạn tốc độ gửi · Dọn file tạm sau N ngày (mặc định 7)
  - Kiểm tra cập nhật (**mặc định tắt**) · Xuất log

### F11. Đa ngôn ngữ
- **AC:** Toàn bộ chuỗi giao diện có bản Tiếng Việt và English; mặc định theo ngôn ngữ hệ điều hành; đổi ngôn ngữ không cần khởi động lại.

### F12. Dòng lệnh (CLI)
- **US-13:** Là người dùng nâng cao, tôi muốn gửi file bằng script.
- **AC:**
  - Các lệnh: `teleport list`, `teleport listen` (chờ máy khác ghép đôi), `teleport pair <thiết bị>`, `teleport unpair <thiết bị>`, `teleport send <file...> --to <thiết bị>`, `teleport receive`.
  - `<thiết bị>` là tên hoặc tiền tố `device_id` (ít nhất 4 ký tự, như `teleport list` hiện). Khớp nhiều thiết bị → báo lỗi kèm danh sách để chọn, không tự đoán. Riêng `teleport pair` còn nhận `ip` hoặc `ip:cổng`, để ghép đôi được khi mạng chặn mDNS.
  - Mã thoát khác 0 khi lỗi; có `--json` cho đầu ra máy đọc được.

---

## 6. Luồng người dùng chính

### 6.1. Lần đầu dùng (2 máy, chưa ghép đôi)
```
Máy A: cài & mở app ──► đặt tên “Laptop-Minh” ──► thấy radar trống
Máy B: cài & mở app ──► đặt tên “PC-Nha”
Máy A: “PC-Nha” hiện trên radar (< 3s)
Máy A: kéo video.mp4 (8GB) thả vào “PC-Nha”
       └─ chưa ghép đôi ──► cả hai máy hiện mã 6 số “482 913”
          └─ người dùng xác nhận trùng ở cả hai máy ──► đã ghép đôi
Máy B: hộp thoại “Laptop-Minh muốn gửi video.mp4 (8,0 GB)” ──► Nhận
Máy A + B: thanh tiến độ, tốc độ, thời gian còn lại
Máy B: thông báo “Đã nhận video.mp4” ──► bấm để mở thư mục
```

### 6.2. Rớt mạng giữa chừng
```
Đang gửi 85% ──► Wi-Fi mất
  Hai máy: trạng thái “Đang nối lại…” (không báo lỗi, không mất tiến độ)
Wi-Fi có lại ──► tự kết nối ──► so bitfield ──► gửi tiếp phần còn thiếu
  ──► kiểm tra Merkle root ──► “Đã nhận”
```

### 6.3. Clipboard
```
Cài đặt ▸ Thiết bị tin cậy ▸ PC-Nha ▸ bật “Đồng bộ clipboard”
Máy A: Ctrl+C một đoạn văn ──► (< 0,5s) ──► Máy B: Ctrl+V dán được
```

---

## 7. Màn hình (wireframe)

### 7.1. Cửa sổ chính — Radar
```
┌─ Teleport ──────────────────────────────────────────── _ □ × ┐
│  Laptop-Minh · Wi-Fi “NhaMinh-5G”                ⚙ Cài đặt     │
│ ┌──────────────────────────────────┐ ┌───────────────────────┐ │
│ │            ·    ·    ·           │ │ Đang truyền           │ │
│ │       ·       ╭───╮      ·       │ │ video.mp4  → PC-Nha   │ │
│ │    ● PC-Nha   │ ◉ │              │ │ ███████░░░  72%       │ │
│ │   (đã ghép)   ╰───╯  ○ MacBook   │ │ 58 MB/s · còn 41s  ✕  │ │
│ │       ·        Bạn   (chưa ghép) │ ├───────────────────────┤ │
│ │            ·    ·    ·           │ │ Gần đây               │ │
│ └──────────────────────────────────┘ │ ✔ report.pdf ← PC-Nha │ │
│   Kéo file thả vào một thiết bị       │ ✔ build.zip → PC-Nha  │ │
│   [ Nhập IP thủ công ]                └───────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
```

### 7.2. Ghép đôi
```
┌─ Ghép đôi với “MacBook” ─────────────────┐
│  Kiểm tra mã này trùng với màn hình       │
│  của MacBook:                             │
│                                           │
│              4 8 2   9 1 3                │
│                                           │
│  [ Không trùng ]          [ Trùng khớp ]  │
└───────────────────────────────────────────┘
```

### 7.3. Có file gửi tới
```
┌─ Laptop-Minh muốn gửi 3 file (8,4 GB) ────┐
│   video.mp4        8,0 GB                 │
│   thumbnail.png    2,1 MB                 │
│   script.docx      380 KB                 │
│  Lưu vào: D:\Downloads\Teleport  [Đổi…]   │
│  ☐ Luôn nhận từ thiết bị này              │
│        [ Từ chối ]            [ Nhận ]    │
└───────────────────────────────────────────┘
```

### 7.4. Cài đặt ▸ Thiết bị tin cậy
```
┌─ Thiết bị tin cậy ──────────────────────────────────────────────────┐
│ PC-Nha    Windows  ☑ Hỏi trước khi nhận  ☑ Clipboard   [ Hủy ghép ] │
│ MacBook   macOS    ☑ Hỏi trước khi nhận  ☐ Clipboard   [ Hủy ghép ] │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 8. Thông báo lỗi & tình huống đặc biệt

Nguyên tắc: nói **chuyện gì xảy ra** và **người dùng nên làm gì**, không dùng thuật ngữ kỹ thuật.

| Tình huống | Thông báo | Hành động gợi ý |
|---|---|---|
| Không thấy thiết bị nào | “Chưa thấy thiết bị nào. Hãy mở Teleport trên máy kia và kiểm tra hai máy dùng chung một mạng Wi-Fi.” | Nút “Nhập IP thủ công” |
| Thấy nhưng không kết nối được (cô lập thiết bị) | “Thấy *PC-Nha* nhưng không kết nối được. Mạng Wi-Fi này có thể đang chặn các thiết bị nói chuyện với nhau (hay gặp ở quán cà phê, khách sạn).” | Gợi ý dùng hotspot điện thoại |
| Tường lửa chặn | “Tường lửa của Windows đang chặn Teleport nhận kết nối.” (máy bị chặn tự phát hiện và hiện; nếu chiều ngược lại vẫn kết nối được thì Teleport vẫn chạy, chỉ hiện gợi ý) | Nút “Mở hướng dẫn” |
| macOS chưa cho quyền mạng cục bộ | “Teleport chưa được phép dùng mạng cục bộ nên không thấy máy khác.” | Nút “Mở Cài đặt hệ thống” |
| Không đủ dung lượng | “Không đủ chỗ trống: cần 12,4 GB, ổ D: còn 8,1 GB.” | Nút “Đổi thư mục nhận” |
| Danh tính thiết bị thay đổi (cùng mã thiết bị, khác chứng chỉ) | “*PC-Nha* đang dùng một danh tính khác với lúc ghép đôi. Có thể có người đang giả mạo máy này. Kết nối đã bị chặn.” | Không cho ghép đôi đè; nút “Hủy ghép đôi *PC-Nha*” (kèm cảnh báo), sau đó mới ghép đôi lại được |
| Máy đã ghép đôi cài lại Teleport | “Có một *PC-Nha* mới chưa ghép đôi. Nếu máy đó vừa cài lại Teleport, hãy ghép đôi lại.” | Nút “Ghép đôi”; ghép xong thì hỏi xóa mục *PC-Nha* cũ |
| Máy kia từ chối máy này | “*PC-Nha* từ chối kết nối từ máy này. Có thể máy đó đã hủy ghép đôi.” | Nút “Ghép đôi lại” |
| Máy kia chưa sẵn sàng ghép đôi | “*PC-Nha* chưa sẵn sàng ghép đôi: máy đó đang ghép đôi với máy khác, chưa mở chế độ chờ ghép đôi, hoặc cần chờ một lát sau lần thử trước. Hãy thử lại sau.” | Nút “Thử lại”; CLI gợi ý `teleport listen` |
| Nhập IP thủ công nhưng không kết nối được | “Không kết nối được tới *192.168.1.5:47474*. Hãy kiểm tra Teleport đang chạy trên máy đó, hai máy dùng chung một mạng, và tường lửa cho phép Teleport.” | Nút “Thử lại” |
| Thiết bị trả lời bằng danh tính không hợp lệ khi ghép đôi | “Thiết bị ở *192.168.1.5:47474* gửi danh tính không hợp lệ nên kết nối đã bị chặn. Có thể có người đang giả mạo.” | — |
| Ghép đôi với chính máy này | “Đó là chính máy này. Hãy nhập địa chỉ của máy kia.” | — |
| Danh tính của chính máy này bị hỏng | “Không đọc được danh tính của máy này. Teleport sẽ không tự tạo danh tính mới để khỏi mất các ghép đôi.” | Nút “Tạo danh tính mới” (cảnh báo phải ghép đôi lại) |
| Mã ghép đôi không trùng / hết giờ | “Ghép đôi với *MacBook* chưa xong. Nếu mã trên hai máy khác nhau, có thể có người đang giả mạo.” | Nút “Thử lại” |
| Máy đang chờ ghép đôi đã hiện từ 3 mã trở lên trong 1 phút | “Cảnh báo: máy này đã hiện *3* mã ghép đôi trong 1 phút qua. Nếu bạn không tự ghép đôi nhiều lần như vậy, có thể có người đang thử giả mạo: hãy từ chối (gõ k).” | Hiện ngay dưới mã; từ chối nếu không tự ghép đôi. Câu trên là bản CLI; giao diện thay “(gõ k)” bằng nút “Từ chối” |
| Không hiện được mã ghép đôi (giao diện lỗi) | “Không hiện được mã ghép đôi trên máy này nên đã hủy ghép đôi. Hãy thử lại.” | Nút “Thử lại” |
| Khác phiên bản giao thức | “*PC-Nha* đang dùng phiên bản Teleport khác. Hãy cập nhật cả hai máy lên cùng phiên bản.” | — |
| File nguồn bị sửa khi đang gián đoạn | “*video.mp4* đã bị thay đổi trên máy gửi nên không thể gửi tiếp. Hãy gửi lại.” | Nút “Gửi lại” |
| Người nhận từ chối | “*PC-Nha* đã từ chối nhận file.” | — |
| Máy gửi đã tắt app khi đang gián đoạn | “*Laptop-Minh* đã hủy lượt gửi *video.mp4*. Hãy nhờ gửi lại.” | — |
| Mất mạng | “Đang nối lại…” (không phải lỗi) | Tự xử lý |

---

## 9. Yêu cầu phi chức năng

Đây là **nguồn duy nhất** cho các con số chất lượng của v1.0. Kế hoạch kỹ thuật và kiểm thử tham chiếu theo mã N1–N12.

| Mã | Nhóm | Yêu cầu | Cách đo |
|---|---|---|---|
| N1 | Khám phá | Thiết bị mới xuất hiện < 3 giây; thoát app thì biến mất < 10 giây; mất mạng đột ngột hoặc ngủ thì biến mất < 2 phút | Log thời điểm mDNS |
| N2 | Hiệu năng | ≥ 70% tốc độ copy qua chia sẻ thư mục Windows (SMB) trên cùng mạng | Benchmark mỗi bản phát hành, công bố ở `benchmark.md` (sẽ có trong `docs/`) |
| N3 | Bộ nhớ | Khi truyền: RSS tăng < 50MB so với lúc rảnh với file 1GB, 5GB, 10GB (đường RAM phẳng). Khi rảnh: RAM < 150MB, CPU ~0% | `psutil` + biểu đồ RSS, mỗi bản phát hành |
| N4 | Nối lại | Rớt mạng hoặc tắt app nhận ở bất kỳ điểm nào → gửi lại ≤ phần chưa nhận + 64 MiB (ở 85%: ≤ 15% + 64 MiB) | Test tự động, đếm byte gửi lại |
| N5 | Toàn vẹn | 100% file nhận khớp Merkle root; sửa 1 byte bất kỳ đều bị phát hiện | Test tự động |
| N6 | Clipboard | Văn bản < 500ms; ảnh ≤ 10MB < 2 giây trên Wi-Fi 5 | Log timestamp hai đầu |
| N7 | Độ tin cậy | 100 lượt truyền liên tiếp (file nhỏ lẫn lớn) không crash, không rò bộ nhớ, không sót file tạm | Test soak tự động |
| N8 | Bảo mật | TLS 1.3 bắt buộc; ngoài bước ghép đôi (có xác nhận mã 6 số), chỉ thiết bị đã ghép đôi mới kết nối được; mọi dữ liệu nhận từ máy khác đều được kiểm tra hợp lệ | Test tự động + fuzz test |
| N9 | Quyền riêng tư | Không telemetry, không tài khoản; không có kết nối Internet nào trừ kiểm tra cập nhật (tùy chọn, mặc định tắt); log không chứa nội dung file/clipboard | Kiểm tra mã + theo dõi kết nối mạng |
| N10 | Tương thích | Windows 10 (22H2) và 11; macOS 12+; Ubuntu 22.04+; các hệ điều hành gửi/nhận qua lại được với nhau | CI + ma trận thử thủ công |
| N11 | Cài đặt | Bộ cài Windows ≤ 80MB, không cần khởi động lại; gỡ cài đặt sạch (tùy chọn giữ cài đặt và thiết bị tin cậy) | Kiểm tra trên máy ảo sạch |
| N12 | Trợ năng | Dùng được hoàn toàn bằng bàn phím; tương phản màu đạt WCAG AA; theo chế độ sáng/tối của hệ điều hành | Kiểm thủ công trước bản beta |

Ngoài ra: CI xanh trên 3 hệ điều hành và mọi tính năng lõi có test tự động.

### 9.1. Thử nghiệm với người dùng thật

App không có telemetry, nên trải nghiệm được đo bằng thử nghiệm trực tiếp với 5–10 người, trên ít nhất 3 kiểu mạng (nhà, văn phòng, Wi-Fi công cộng):

| Chỉ số | Mục tiêu v1.0 |
|---|---|
| Thời gian từ mở bộ cài tới gửi xong file đầu tiên | < 2 phút |
| Tỉ lệ người thử gửi thành công không cần hỗ trợ | ≥ 90% |
| Lỗi nghiêm trọng còn mở khi phát hành | 0 |

---

## 10. Kiến trúc tóm tắt

```
┌──────────── App Teleport (Python 3.12+) ────────────┐
│ Giao diện: PySide6 — radar, lượt truyền, ghép đôi,   │
│            khay hệ thống          │ CLI `teleport`   │
│ Lõi asyncio:                                         │
│   khám phá (mDNS) · danh tính (chứng chỉ tự ký)      │
│   ghép đôi (mã 6 số) · kết nối TLS 1.3 ghim cert     │
│   gửi/nhận theo khối 1 MiB + backpressure            │
│   SHA-256 Merkle + bitfield nối lại · clipboard      │
└──────────────────────────────────────────────────────┘
            ▲ TLS 1.3, chỉ thiết bị đã ghép đôi ▼
                  App Teleport trên máy khác
```

- Kết nối trực tiếp bằng TCP + TLS 1.3 trong mạng nội bộ; không có máy chủ trung gian.
- File được chia thành khối 1 MiB, đọc và ghi thẳng giữa hai ổ đĩa; RAM chỉ giữ vài khối.
- Thiết kế chi tiết: [ke_hoach.md](ke_hoach.md) mục 3. Đặc tả giao thức: [protocol.md](protocol.md).

---

## 11. Bảo mật & quyền riêng tư

| Mối đe dọa | Ví dụ | Biện pháp trong MVP |
|---|---|---|
| Nghe lén trên Wi-Fi chung | Người cùng quán cà phê bắt gói tin | Toàn bộ dữ liệu mã hóa TLS 1.3 |
| Giả mạo thiết bị (MITM) | Kẻ xấu đặt tên máy “PC-Nha” | Ghép đôi bằng mã 6 số có nonce cam kết (kẻ giữa không thể dò trước khóa cho trùng mã); sau đó chỉ chấp nhận đúng chứng chỉ đã ghim |
| Thiết bị lạ gửi rác | Máy lạ spam file | Máy chưa ghép đôi không kết nối được; mặc định luôn hỏi trước khi nhận |
| File độc hại ghi đè hệ thống | Tên file `..\..\Windows\x.dll` | Làm sạch tên, chỉ ghi trong thư mục nhận, không ghi đè |
| Dữ liệu điều khiển độc hại | Message cực lớn hoặc sai định dạng | Giới hạn kích thước, kiểm tra hợp lệ từng trường, fuzz test |
| Lộ mật khẩu qua clipboard | Copy mật khẩu từ trình quản lý mật khẩu | Clipboard mặc định tắt; bỏ qua nội dung đánh dấu nhạy cảm |
| Thu thập dữ liệu người dùng | — | Không telemetry, không tài khoản, không máy chủ |

Báo lỗ hổng bảo mật theo [SECURITY.md](SECURITY.md) (đã có từ Giai đoạn 0; bản hoàn chỉnh cho v1.0).

---

## 12. Tiêu chí phát hành MVP (Definition of Done)

- [ ] Mọi tiêu chí chấp nhận F1–F12 đều đạt.
- [ ] Mọi yêu cầu phi chức năng N1–N12 đều đạt, có số liệu trong `benchmark.md`.
- [ ] Đạt các chỉ số thử nghiệm người dùng ở mục 9.1.
- [ ] CI xanh trên Windows, macOS, Linux.
- [ ] Bộ cài cho 3 hệ điều hành được build tự động khi gắn tag `v1.0.0` (nếu trễ tiến độ, bộ cài macOS/Linux có thể hoãn theo mục 14; khi đó ghi rõ ở đây); bộ cài kèm `LICENSE`, giấy phép các thư viện bên thứ ba và chỉ dẫn lấy mã nguồn.
- [ ] Test soak 100 lượt truyền đạt.
- [ ] Tài liệu người dùng: cài đặt, ghép đôi, khắc phục sự cố (tường lửa, Wi-Fi chặn thiết bị).
- [ ] `SECURITY.md`, chính sách quyền riêng tư, `CHANGELOG.md` cho v1.0.0.
- [x] Đã chốt giấy phép phần mềm: GPL-3.0-or-later.
- [ ] Không còn lỗi nghiêm trọng đang mở.

---

## 13. Kịch bản demo (5 phút)

| Phút | Hành động | Điểm muốn chứng minh |
|---|---|---|
| 0:00 | Mở Teleport trên 2 laptop; máy kia tự hiện trên radar | Zero-Friction, không gõ IP |
| 0:30 | Ghép đôi bằng mã 6 số | Bảo mật dễ hiểu với người thường |
| 1:00 | Kéo file 10GB thả vào máy kia; mở Task Manager cạnh bên | Tốc độ LAN, RAM đứng yên |
| 2:00 | Rút dây/tắt Wi-Fi ở ~80%, đợi 10 giây, bật lại | Tự nối lại, chỉ gửi phần thiếu |
| 3:00 | Nhận xong, hiện “Đã kiểm tra toàn vẹn” | SHA-256 Merkle |
| 3:30 | Ctrl+C một đoạn văn + ảnh chụp màn hình → Ctrl+V trên máy kia | Clipboard đồng bộ |
| 4:00 | Tắt Internet của router, gửi tiếp vẫn chạy | Zero-Cloud thật sự |
| 4:30 | So sánh nhanh với Google Drive (số liệu benchmark) | Lợi thế thời gian |

---

## 14. Rủi ro sản phẩm

Rủi ro kỹ thuật và thực thi: [ke_hoach.md](ke_hoach.md) mục 8.

| Rủi ro | Ảnh hưởng | Hướng xử lý |
|---|---|---|
| Wi-Fi công cộng/văn phòng chặn thiết bị nói chuyện với nhau | Không dùng được ở quán cà phê, khách sạn | Phát hiện và báo rõ; gợi ý hotspot |
| SmartScreen / antivirus cảnh báo app chưa ký; macOS Gatekeeper chặn app chưa ký (macOS 15 phải mở Cài đặt hệ thống mới chạy được) | Người dùng sợ, không cài; khó đạt mục 9.1 (≥ 90% tự cài được) | Ký mã nếu có ngân sách (ke_hoach mục 9); hướng dẫn rõ từng bước |
| Cạnh tranh: LocalSend, KDE Connect, Nearby Share | Khó nổi bật | Tập trung vào nối lại theo khối, file cực lớn, clipboard |
| Trễ tiến độ (làm một mình) | v1.0 kéo dài | Hoãn sang sau v1.0 theo thứ tự: công cụ C++/Java → bộ cài macOS/Linux → clipboard ảnh. Không cắt nối lại/toàn vẹn (F6, F7) và an toàn khi nhận (F4). Hoãn mục nào thì sửa mục 4 và mục 12 tương ứng |

---

## 15. Lộ trình & việc cần chốt

- Lộ trình, các bản phát hành và **trạng thái hiện tại**: [ke_hoach.md](ke_hoach.md) mục 5.
- Các câu hỏi còn mở (tên thương mại, ngân sách ký mã, chế độ khách cho điện thoại, App Store và CLA): [ke_hoach.md](ke_hoach.md) mục 9.
