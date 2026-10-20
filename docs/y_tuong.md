# Ý tưởng — Zero-Cloud LAN Teleport

> **Vai trò:** ý tưởng và tầm nhìn sản phẩm — vì sao làm, cho ai, trải nghiệm mong muốn, cách tiếp cận ở mức cao, các nguyên tắc dùng để ra quyết định.
> **Không chứa:** con số yêu cầu, tiêu chí chấp nhận → [mvp.md](mvp.md). Thiết kế kỹ thuật, lộ trình → [ke_hoach.md](ke_hoach.md). Thuật ngữ và bản đồ tài liệu → [README.md](README.md).
> **Bản đầu tiên** (WebRTC, điện thoại ↔ laptop) đã được điều chỉnh; những gì đã đổi xem mục 9.
> **Cập nhật:** 2026-10-20

---

## 1. Một câu tóm tắt

**Teleport** — *cổng dịch chuyển file và clipboard trong mạng nội bộ*: mở app trên hai máy tính dùng chung Wi-Fi, máy kia tự hiện trên radar, kéo file hàng chục GB thả vào là xong. Không cloud, không tài khoản, rớt mạng không phải gửi lại từ đầu.

---

## 2. Nỗi đau thực tế

Hai máy tính nằm cách nhau 20cm, dùng chung một cục Wi-Fi, mà chuyển một file 2GB (video, bản build, bộ dữ liệu) hay một đoạn văn bản, một ảnh chụp màn hình sang máy kia vẫn rất ức chế:

* **Ứng dụng nhắn tin (Zalo/Messenger):** ảnh bị nén mờ, file lớn bị chặn, hoặc phải đăng nhập tài khoản cá nhân lên máy lạ, tiềm ẩn rủi ro bảo mật.
* **Lưu trữ đám mây (Google Drive):** upload lên máy chủ ở xa rồi download về, tốn gấp đôi băng thông và thời gian dù hai máy nằm sát nhau.
* **USB / ổ cứng rời:** phải có sẵn, cắm, chép, rút; dễ quên, dễ mất.
* **Chia sẻ thư mục Windows (SMB):** nhanh nhưng cấu hình quyền phức tạp, rớt mạng là gửi lại từ đầu.

Người chịu nỗi đau này nhiều nhất là người làm việc với nhiều máy tính hoặc ngồi cạnh đồng nghiệp: lập trình viên, designer, người dựng video, văn phòng nhỏ, gia đình. Chân dung chi tiết: [mvp.md](mvp.md) mục 2.

---

## 3. Giá trị cốt lõi

| Giá trị | Ý nghĩa với người dùng |
|---|---|
| **Zero-Friction** — không rào cản | Không tài khoản, không gõ IP. Cài xong, máy kia tự hiện ra. |
| **Zero-Cloud** — thuần cục bộ | Dữ liệu chỉ đi trực tiếp giữa hai máy trong mạng nội bộ, được mã hóa. App không gửi gì ra Internet. |
| **Zero-RAM-Overflow** — không tràn bộ nhớ | File hàng chục GB đi thẳng từ ổ đĩa máy gửi sang ổ đĩa máy nhận; bộ nhớ không phình theo kích thước file. |
| **Không sợ rớt mạng** | Wi-Fi chập chờn hay gập laptop giữa chừng thì tự nối lại và chỉ gửi phần còn thiếu; mọi khối đều được kiểm tra toàn vẹn. |

---

## 4. Trải nghiệm mong muốn

| # | Trải nghiệm | Người dùng thấy gì | Đặc tả |
|---|---|---|---|
| 1 | **Radar khám phá** | Mở app, các máy đang mở Teleport trong cùng mạng tự hiện thành chấm trên radar. Mạng chặn tự tìm thì vẫn nhập IP được. | mvp F2 |
| 2 | **Ghép đôi một lần** | Lần đầu gửi tới máy nào, hai màn hình cùng hiện một mã 6 số; thấy trùng thì bấm xác nhận ở cả hai máy. Từ đó hai máy tự tin nhau. | mvp F3 |
| 3 | **Kéo thả file cực lớn** | Kéo một hoặc nhiều file thả vào chấm trên radar; máy kia được hỏi Nhận / Từ chối; thanh tiến độ, tốc độ, thời gian còn lại. | mvp F4, F5 |
| 4 | **Tự nối lại** | Rớt Wi-Fi thì hiện “Đang nối lại…”, có mạng là chạy tiếp từ chỗ dừng, kể cả khi đã tắt app. File chỉ hiện ra khi đã kiểm tra khớp với bản gốc. | mvp F6, F7 |
| 5 | **Clipboard chung** | Ctrl+C đoạn văn hoặc ảnh chụp màn hình trên máy này, Ctrl+V ngay trên máy kia. Bật riêng cho từng máy tin cậy. | mvp F8 |
| 6 | **Luôn sẵn sàng** | App nằm dưới khay hệ thống, chạy nền, vẫn nhận file và clipboard. Người dùng nâng cao gửi được bằng dòng lệnh. | mvp F9, F12 |

Luồng chi tiết và wireframe: [mvp.md](mvp.md) mục 6–7.

---

## 5. Cách tiếp cận ở mức cao

Hệ thống gồm 4 khối việc, đều chạy độc lập với Internet:

```
+-------------------------------------------------------------------------+
|              GIAO DIỆN: RADAR · LƯỢT TRUYỀN · KHAY · CLIPBOARD          |
|            (kéo thả file hàng chục GB / Ctrl+C -> Ctrl+V)               |
+-------------------------------------------------------------------------+
                                     |
+-------------------------------------------------------------------------+
|  1. KHÁM PHÁ & GHÉP ĐÔI            |  3. TOÀN VẸN & NỐI LẠI             |
|  - mDNS / ZeroConf tự tìm nhau     |  - SHA-256 từng khối + Merkle root |
|  - Nhập IP thủ công khi bị chặn    |  - Bitfield khối đã nhận           |
|  - Mã 6 số xác nhận ở hai máy      |  - Chỉ gửi lại khối thiếu / lỗi    |
+-------------------------------------------------------------------------+
                                     |
+-------------------------------------------------------------------------+
|  2. TRUYỀN TẢI & KIỂM SOÁT LUỒNG                                        |
|  - Kết nối trực tiếp TCP trong mạng nội bộ                              |
|  - Chia file thành khối cố định, đọc/ghi thẳng giữa hai ổ đĩa           |
|  - Backpressure: bên nhận ghi chậm thì bên gửi tự chậm lại             |
+-------------------------------------------------------------------------+
                                     |
+-------------------------------------------------------------------------+
|  4. BẢO MẬT                                                             |
|  - TLS 1.3 cho mọi dữ liệu, kể cả clipboard                             |
|  - Ghim chứng chỉ thiết bị sau ghép đôi; máy lạ không kết nối được     |
+-------------------------------------------------------------------------+
```

1. **Khám phá & ghép đôi.** Mỗi máy tự quảng bá sự có mặt của mình trong mạng nội bộ bằng mDNS, không cần máy chủ trung tâm. Lần đầu nói chuyện, hai máy ghép đôi bằng mã 6 số do người dùng so bằng mắt; mã được thiết kế để kẻ đứng giữa không thể chuẩn bị trước cho trùng.
2. **Truyền tải & kiểm soát luồng.** Không bao giờ đọc cả file vào bộ nhớ: file được cắt thành các khối, bộ nhớ chỉ giữ vài khối cùng lúc. Khi mạng hoặc ổ đĩa bên nhận chậm hơn, bên gửi tự động chờ, nên bộ nhớ không phình ra dù file lớn cỡ nào.
3. **Toàn vẹn & nối lại.** Mỗi khối có mã băm SHA-256, cả file có một Merkle root. Bên nhận ghi lại khối nào đã nhận và đã kiểm tra; khi nối lại, hai bên so bảng này và chỉ gửi phần còn thiếu hoặc bị lỗi. File đang nhận mang đuôi `.part` và chỉ đổi sang tên thật khi đã khớp hoàn toàn.
4. **Bảo mật.** Mọi dữ liệu, cả file lẫn clipboard, đều được mã hóa. Sau khi ghép đôi, mỗi máy chỉ chấp nhận đúng chứng chỉ đã ghim của máy kia, nên ngồi chung Wi-Fi công cộng cũng không ai đọc trộm hay giả mạo được.

Thiết kế chi tiết, giao thức và lựa chọn công nghệ (Python, PySide6, asyncio): [ke_hoach.md](ke_hoach.md) mục 2–4.

---

## 6. Nguyên tắc sản phẩm

Khi phải chọn giữa hai hướng, ưu tiên theo các nguyên tắc sau:

1. **Riêng tư tuyệt đối.** Không telemetry, không tài khoản, không máy chủ. Kết nối Internet duy nhất được phép là kiểm tra cập nhật, và phải do người dùng tự bật.
2. **An toàn theo mặc định.** Mặc định hỏi trước khi nhận; clipboard mặc định tắt; máy chưa ghép đôi không gửi được gì.
3. **Không bao giờ mất hay hỏng dữ liệu.** Không ghi đè file sẵn có, không ghi ra ngoài thư mục nhận, không hiện file chưa kiểm tra xong.
4. **Nói ngôn ngữ người dùng.** Lỗi phải nói chuyện gì xảy ra và nên làm gì, không dùng thuật ngữ kỹ thuật.
5. **Lõi trước, giao diện sau.** Phần truyền tải và nối lại phải chạy đúng qua dòng lệnh, có test, rồi mới gắn giao diện.
6. **Làm ít mà chắc.** Mỗi bản phát hành là một bản dùng được; ý tưởng mới để sau v1.0 thay vì ôm thêm.

---

## 7. Phạm vi & tầm nhìn

**v1.0 (MVP): desktop ↔ desktop** trên Windows, macOS, Linux, trong cùng một mạng nội bộ. Danh sách có / không có trong MVP: [mvp.md](mvp.md) mục 4.

**Sau v1.0**, theo hướng mở rộng tới mọi thiết bị trong nhà và văn phòng:
1. App Android, dùng chung giao thức với bản desktop.
2. Chế độ khách qua trình duyệt cho điện thoại không cài app.
3. Gửi tới nhiều thiết bị cùng lúc, gửi và đồng bộ thư mục.
4. App iOS.

Thứ tự chính thức và các câu hỏi còn mở: [ke_hoach.md](ke_hoach.md) mục 5.3 và mục 9.

---

## 8. Khác biệt so với giải pháp sẵn có

Các ứng dụng gần nhất là **LocalSend**, **KDE Connect** và **Nearby Share**. Teleport không cố làm nhiều hơn, mà tập trung làm tốt hơn ở ba điểm:

- **File cực lớn và rớt mạng:** nối lại theo từng khối, chỉ gửi phần thiếu, kể cả sau khi tắt app; kiểm tra toàn vẹn bằng Merkle root.
- **Clipboard chung** giữa các máy tin cậy, bật riêng từng máy.
- **Không tài khoản, không cloud** mà vẫn chống được giả mạo nhờ ghép đôi bằng mã 6 số.

---

## 9. Ý tưởng đã thay đổi thế nào

Bản ý tưởng đầu tiên đã được điều chỉnh khi lập kế hoạch:

| Bản đầu tiên | Hiện nay | Vì sao |
|---|---|---|
| Laptop ↔ điện thoại là tình huống chính | Desktop ↔ desktop ở v1.0; điện thoại sau v1.0 | Làm một mình; app điện thoại là sản phẩm riêng, dùng lại cùng giao thức |
| Trang web nội bộ hoặc app nhẹ | Ứng dụng desktop cài đặt, có khay hệ thống | Cần đọc/ghi thẳng ổ đĩa, chạy nền và theo dõi clipboard |
| WebRTC DataChannel, DTLS | TCP + TLS 1.3 | Trong mạng nội bộ không cần vượt NAT; nhanh và dễ bảo trì hơn |
| Mảnh 64–256KB, mỗi mảnh kèm hash trong header | Khối lớn hơn, hash gửi theo lô | Ít chi phí mỗi khối, bitfield nhỏ |
| Ghép đôi chỉ nhắc cho clipboard | Bắt buộc ghép đôi cho mọi thứ | Chống giả mạo và chống máy lạ gửi rác |
| RAM < 100MB tuyệt đối | RAM không tăng theo kích thước file | Bản thân giao diện đã chiếm một phần bộ nhớ cố định |

Chi tiết và số liệu: [ke_hoach.md](ke_hoach.md) mục 2.
