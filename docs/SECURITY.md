# Chính sách bảo mật

## Phiên bản được hỗ trợ

Teleport chưa có bản phát hành. Sau `v1.0.0`, bản vá bảo mật được áp dụng cho phiên bản phụ (minor) mới nhất.

## Báo cáo lỗ hổng

**Không mở issue công khai** cho lỗ hổng bảo mật. Hãy báo riêng qua mục **Security → Report a vulnerability** của repo trên GitHub, kèm:

- Mô tả lỗ hổng và tác động.
- Các bước tái hiện (phiên bản app, hệ điều hành, cấu hình mạng).
- Đề xuất cách sửa, nếu có.

Bạn sẽ nhận được phản hồi trong vòng 7 ngày.

## Phạm vi

Đặc biệt quan tâm tới:

- Vượt qua bước ghép đôi hoặc ghim chứng chỉ (giả mạo thiết bị, MITM).
- Ghi file ra ngoài thư mục nhận, ghi đè file có sẵn.
- Làm crash hoặc treo app, hoặc làm RAM/đĩa tăng không giới hạn, bằng dữ liệu gửi từ máy khác.
- Lộ nội dung file hoặc clipboard cho thiết bị chưa ghép đôi.
- Bất kỳ kết nối nào ra Internet mà người dùng không bật.

Thiết kế bảo mật: [protocol.md](protocol.md).
