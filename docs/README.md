# Tài Liệu ZCL Teleport

Thư mục này chứa toàn bộ tài liệu hướng dẫn, kế hoạch, đặc tả kỹ thuật và quy trình phát triển của ZCL Teleport. Mọi tài liệu được phân chia theo từng nhóm thư mục chuyên biệt bên dưới.

---

## 1. Bản Đồ Tài Liệu

### Nhóm 1: Sản Phẩm & Kế Hoạch (`docs/product/`)
Đọc theo thứ tự: **y_tuong -> mvp -> ke_hoach** (từ mục tiêu đến yêu cầu và lộ trình thực hiện).

| Tài liệu | Nội dung chính | Khi nào cập nhật |
|---|---|---|
| [y_tuong.md](product/y_tuong.md) | Mục đích ra đời, trải nghiệm mong muốn, nguyên tắc thiết kế cốt lõi | Khi điều chỉnh định hướng sản phẩm |
| [mvp.md](product/mvp.md) | Danh sách tính năng, tiêu chí hoàn thành, các giới hạn kỹ thuật | Khi bổ sung tính năng hoặc tiêu chuẩn chất lượng |
| [ke_hoach.md](product/ke_hoach.md) | Lộ trình từng giai đoạn, tiến độ thực tế, kế hoạch kiểm thử thực tế | Khi hoàn thành giai đoạn hoặc cập nhật thiết kế |
| [hieu_du_an.md](product/hieu_du_an.md) | Giải thích cách thức hoạt động qua ví dụ trực quan, dễ hiểu | Khi quy trình kết nối hoặc truyền file thay đổi |

### Nhóm 2: Kỹ Thuật & Cấu Trúc (`docs/technical/`)

| Tài liệu | Nội dung chính | Khi nào cập nhật |
|---|---|---|
| [architecture.md](technical/architecture.md) | Cấu trúc phân tầng mã nguồn, mô hình tiến trình, trạng thái các module | Khi hoàn thiện hoặc thêm module mới |
| [protocol.md](technical/protocol.md) | Đặc tả định dạng dữ liệu truyền trực tiếp giữa hai máy tính | Khi có thay đổi định dạng gói tin |
| [benchmark.md](technical/benchmark.md) | Kết quả đo đạc thực tế về tốc độ truyền, bộ nhớ tiêu thụ, thời gian phản hồi | Sau mỗi lần chạy kiểm tra thực tế |

### Nhóm 3: Cộng Đồng & Quy Trình (`docs/community/`)

| Tài liệu | Nội dung chính | Khi nào cập nhật |
|---|---|---|
| [CONTRIBUTING.md](community/CONTRIBUTING.md) | Hướng dẫn cài đặt môi trường, quy tắc đóng góp, quy trình phát hành | Khi thay đổi quy trình phát triển |
| [SECURITY.md](community/SECURITY.md) | Chính sách an toàn thông tin và cách báo cáo lỗi bảo mật | Khi cập nhật chính sách an toàn |
| [CHANGELOG.md](community/CHANGELOG.md) | Nhật ký thay đổi qua từng phiên bản | Sau mỗi bản phát hành mới |

### Nhóm 4: Hình Ảnh & Tài Nguyên (`docs/assets/`)
Chứa hình ảnh giao diện thực tế, biểu đồ quy trình và biểu tượng ứng dụng.

---

## 2. Nguyên Tắc Đồng Bộ Tài Liệu

- **Nguồn thông tin duy nhất:** Mỗi quy định hoặc thông số chỉ được ghi chi tiết tại một tài liệu duy nhất; các tài liệu khác chỉ dẫn chiếu tới mã tương ứng.
- **Dễ hiểu và nhất quán:** Mọi tài liệu ưu tiên câu chữ mạch lạc, dễ hiểu, tránh các thuật ngữ chuyên ngành phức tạp.
- **Trực tiếp và minh bạch:** Khi cập nhật tính năng mới, tài liệu liên quan phải được cập nhật đồng thời trong cùng một đợt thay đổi.
- **Quy chuẩn mã nguồn:** Mã nguồn luôn tuân thủ cấu trúc phân tầng được định nghĩa tại [architecture.md](technical/architecture.md).

---

## 3. Bảng Giải Nghĩa Thuật Ngữ

| Khái niệm | Ý nghĩa đơn giản |
|---|---|
| **Khối dữ liệu (Block)** | Phần nhỏ được chia ra từ file lớn (mỗi khối 1 MiB) để gửi tuần tự và kiểm tra tính toàn vẹn. |
| **Lượt truyền** | Một phiên gửi một hoặc nhiều file giữa hai máy tính. |
| **Ghép đôi** | Bước xác nhận mã 6 chữ số giữa hai máy tính để thiết lập độ tin cậy. |
| **Máy tin cậy** | Máy tính đã được ghép đôi thành công, lưu trong danh sách máy quen thuộc. |
| **Nhận tiếp (Resume)** | Khả năng tự động truyền tiếp phần dữ liệu còn thiếu khi kết nối bị ngắt mà không cần gửi lại từ đầu. |
| **Chế độ ẩn / hiện** | Tùy chọn cho phép hoặc không cho phép các máy chưa ghép đôi nhìn thấy máy của bạn trong mạng. |
| **Mạng nội bộ** | Mạng kết nối các máy tính tại cùng một không gian (như cùng cắm dây mạng hoặc chung một điểm phát Wi-Fi). |

---

*Tác giả dự án: Nguyễn Hoàng Tùng (@Luffyreals)*
