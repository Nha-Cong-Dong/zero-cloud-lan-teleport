# Hướng Dẫn Sử Dụng ZCL Teleport

Tài liệu hướng dẫn chi tiết cách cài đặt, kết nối và sử dụng ZCL Teleport dành cho người dùng thông thường.

---

## 1. Giới Thiệu Chung

**ZCL Teleport** là công cụ giúp bạn gửi nhận tệp tin dung lượng lớn và chia sẻ nội dung sao chép trực tiếp giữa các máy tính trong cùng một mạng nội bộ.

- **Trực tiếp và riêng tư:** Dữ liệu đi thẳng từ máy gửi sang máy nhận, không lưu trữ qua máy chủ trung gian trên mạng.
- **Không cần tài khoản:** Không yêu cầu đăng ký, đăng nhập hay nhớ mật khẩu.
- **Không ngại mất mạng:** Đang gửi mà mất kết nối, khi có mạng lại ứng dụng sẽ tự động gửi tiếp đúng phần còn thiếu mà không cần bắt đầu lại từ đầu.
- **An toàn cho máy tính:** Tệp tin lớn được chia thành các phần nhỏ để đọc ghi tuần tự, không làm đầy bộ nhớ RAM hay gây đơ máy.

---

## 2. Cài Đặt & Khởi Động Lần Đầu

### 2.1. Tải về và cài đặt

Tải bản phát hành mới nhất từ trang dự án:
- **Bản cài đặt tự động (`teleport-setup-x64.exe`):** Bấm đúp vào tệp và làm theo các bước hướng dẫn trên màn hình. Bản cài đặt sẽ tự động tạo biểu tượng trên màn hình chính và cấu hình cho phép kết nối trong mạng.
- **Bản chạy ngay dạng nén (`teleport-gui-windows-x64.zip`):** Giải nén vào một thư mục trên máy (ví dụ `C:\Teleport\`) và mở trực tiếp tệp `Teleport.exe`.

### 2.2. Xử lý cảnh báo mở lần đầu trên Windows

- **Nếu màn hình hiện thông báo bảo vệ của Windows:** Bấm vào dòng chữ **Thêm thông tin (More info)**, sau đó bấm nút **Vẫn chạy (Run anyway)**.
- **Nếu hộp thoại tường lửa hỏi quyền truy cập:** Hãy tích chọn **Mạng riêng tư (Private networks)** và bấm **Cho phép truy cập (Allow access)**.

---

## 3. Tìm Máy & Ghép Đôi Thiết Bị

Để hai máy tính có thể gửi nhận dữ liệu với nhau, bạn chỉ cần thực hiện ghép đôi một lần duy nhất.

```
   Máy A                                                  Máy B
┌──────────┐                                           ┌──────────┐
│ Teleport │  1. Tự động nhận diện nhau trong mạng     │ Teleport │
│          │ ◄───────────────────────────────────────► │          │
│          │  2. Đối chiếu mã 6 số hiển thị màn hình   │          │
│          │ ◄───────────────────────────────────────► │          │
└──────────┘  3. Bấm xác nhận -> Trở thành máy tin cậy  └──────────┘
```

### Các bước thực hiện:

1. Mở ZCL Teleport trên cả hai máy tính (cùng bắt chung một điểm phát Wi-Fi hoặc cùng cắm dây mạng).
2. Tại máy gửi, bấm nút **Tìm máy mới** (biểu tượng kính lúp). Các máy tính đang mở ứng dụng trong mạng sẽ tự động xuất hiện trên màn hình radar.
3. Bấm vào tên máy bạn muốn kết nối.
4. Màn hình của cả hai máy sẽ cùng hiển thị một **mã xác nhận gồm 6 chữ số**.
5. Nhìn màn hình của hai máy: nếu 6 chữ số trùng khớp hoàn toàn, bấm nút **Trùng khớp** ở cả hai bên.
6. Sau khi xác nhận xong, hai máy đã trở thành máy tin cậy của nhau và sẽ luôn tự động nhận ra nhau trong những lần sử dụng sau.

> **Trường hợp mạng chặn tự tìm máy:** Nếu điểm phát sóng chặn tính năng tự tìm nhau, bạn chỉ cần bấm nút **Nhập địa chỉ thủ công**, gõ địa chỉ hiển thị trên máy bên kia (ví dụ `192.168.1.15`) để kết nối trực tiếp.

---

## 4. Gửi & Nhận Tệp Tin Dung Lượng Lớn

### 4.1. Cách gửi tệp tin

Có 3 cách đơn giản để gửi:
- **Kéo thả:** Kéo một hoặc nhiều tệp từ cửa sổ thư mục của bạn và thả trực tiếp vào tên máy nhận ở danh sách bên trái hoặc vào khung gửi.
- **Bấm nút gửi:** Chọn máy nhận trong danh sách, bấm nút **Gửi file** (hoặc nhấn phím tắt `Ctrl + O`) rồi chọn tệp cần gửi.
- **Chuột phải:** Nhấn chuột phải vào tên máy nhận và chọn **Gửi file...**.

### 4.2. Cách nhận tệp tin

- Khi có người gửi dữ liệu tới, một hộp thoại thông báo sẽ xuất hiện trên màn hình máy nhận kèm tên người gửi, tên các tệp tin và dung lượng tệp.
- Bấm **Nhận** để đồng ý tải về, hoặc bấm **Từ chối** nếu không muốn nhận.
- Mặc định, tệp tin nhận về sẽ được lưu an toàn trong thư mục `Teleport` tại mục `Tải về (Downloads)` của máy tính. Bạn có thể đổi thư mục lưu này trong phần Cài đặt.

### 4.3. Theo dõi tiến trình truyền

- Thanh tiến trình hiển thị rõ tỉ lệ phần trăm đã nhận, tốc độ truyền tải thực tế và thời gian ước tính còn lại.
- **Tự động gửi tiếp khi mất mạng:** Nếu vô tình bị vấp dây mạng, mất sóng Wi-Fi hoặc máy bị tắt đột ngột, quá trình truyền sẽ tạm dừng. Ngay khi có mạng trở lại, ứng dụng sẽ **tự động gửi tiếp đúng phần còn thiếu** mà không cần gửi lại từ đầu.
- Khi truyền xong, ứng dụng sẽ kiểm tra lại tính toàn vẹn của tệp tin để đảm bảo nội dung tệp nhận được khớp từng phần với tệp gốc trước khi mở cho bạn sử dụng.

---

## 5. Đồng Bộ Nội Dung Sao Chép (Clipboard)

ZCL Teleport hỗ trợ tính năng chia sẻ nội dung sao chép giữa các máy tính đã ghép đôi:
- **Văn bản:** Sao chép một đoạn văn bản ở máy này, bạn có thể dán ngay sang máy kia.
- **Hình ảnh:** Chụp ảnh màn hình ở máy này, bạn có thể dán ngay vào trình soạn thảo ở máy kia.

> **Lưu ý bảo mật:** Để bảo vệ tối đa quyền riêng tư của bạn, tính năng đồng bộ nội dung sao chép **mặc định được tắt**. Bạn chỉ bật lên trong phần Cài đặt khi có nhu cầu sử dụng. Ứng dụng cũng được thiết kế tự động phát hiện và bỏ qua các nội dung nhạy cảm từ các trình quản lý mật khẩu.

---

## 6. Xử Lý Các Sự Cố Thường Gặp

### 6.1. Hai máy cùng mạng nhưng không tìm thấy nhau

1. **Kiểm tra trạng thái mở ứng dụng:** Đảm bảo cả hai máy đều đang bật ZCL Teleport.
2. **Kiểm tra loại mạng của Windows (Mạng riêng tư vs Mạng công cộng):**
   - Windows mặc định có thể đặt mạng Wi-Fi ở chế độ Công cộng (Public), chặn các máy tính tìm thấy nhau.
   - Khi phát hiện tình trạng này, ứng dụng sẽ hiện một thanh cảnh báo màu vàng ở đầu màn hình kèm nút **Mở hướng dẫn**.
   - Bấm vào nút **Mở hướng dẫn**, sau đó bấm **Mở cài đặt mạng** để chuyển mạng đang dùng sang chế độ **Mạng riêng tư (Private)**.
3. **Nhập địa chỉ trực tiếp:** Nếu điểm phát sóng (như ở quán cà phê, khách sạn) cố tình cô lập các máy tính, hãy bấm **Nhập địa chỉ thủ công** để kết nối thẳng tới địa chỉ của máy cần gửi.

### 6.2. Ứng dụng báo không đủ chỗ trống trên ổ đĩa

- Trước khi bắt đầu nhận tệp, ZCL Teleport luôn kiểm tra dung lượng còn trống trên ổ đĩa lưu của máy nhận.
- Nếu ổ đĩa của bạn còn trống ít hơn dung lượng tệp gửi tới, ứng dụng sẽ tự động từ chối và thông báo rõ ràng để tránh làm đầy ổ cứng của bạn.
- Bạn chỉ cần dọn dẹp ổ đĩa hoặc vào Cài đặt chọn một thư mục lưu khác có nhiều dung lượng trống hơn.

### 6.3. Muốn tắt hẳn ứng dụng

- Khi bạn bấm nút đóng cửa sổ (dấu X), ứng dụng sẽ tự động thu nhỏ xuống thanh tác vụ ở góc dưới màn hình để luôn sẵn sàng nhận tệp khi có người gửi tới.
- Để tắt hoàn toàn ứng dụng, hãy bấm chuột phải vào biểu tượng ứng dụng ở khay hệ thống (cạnh đồng hồ) và chọn **Thoát**.

---

*Tài liệu được cập nhật ngày: 2026-11-09 · Tác giả: Nguyễn Hoàng Tùng (@Luffyreals)*
