# Chính sách bảo mật

## Phiên bản được hỗ trợ

Trước `v1.0.0`, các bản thử nghiệm chỉ để dùng thử và không được vá riêng: lỗ hổng được sửa trong bản phát hành kế tiếp, nên hãy luôn dùng bản mới nhất. Từ `v1.0.0`, bản vá bảo mật được áp dụng cho phiên bản phụ (minor) mới nhất.

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

Mô hình mối đe dọa và biện pháp: [mvp.md](../product/mvp.md) mục 11. Thiết kế bảo mật trên dây: [protocol.md](../technical/protocol.md).

---

## Chính Sách Ký Mã & Toàn Vẹn Phát Hành

*This section establishes the Build Integrity Policy and Privacy Statement for ZCL Teleport.*

### Quy trình đóng gói và xác minh toàn vẹn (Build & Integrity Process)

- Toàn bộ tệp thực thi phát hành chính thức (`teleport.exe`, `Teleport.exe`, bộ cài `teleport-setup-*-x64.exe`, `.AppImage`, `.dmg` và các gói nén phát hành) được biên dịch hoàn toàn tự động từ mã nguồn công khai trên GitHub Actions (`.github/workflows/build-cli.yml`, `.github/workflows/build-gui.yml`, `.github/workflows/build-linux.yml`, `.github/workflows/build-macos.yml`, `.github/workflows/release.yml`).
- Việc ký mã cho bộ cài Windows hiện **được tạm hoãn** (đơn đăng ký chương trình ký mã nguồn mở miễn phí SignPath Foundation đã bị từ chối). Mọi bản phát hành đều đính kèm tệp `SHA256SUMS.txt` sinh tự động trên CI để người dùng kiểm tra tính nguyên vẹn trước khi chạy.
- Chỉ các mã nguồn trên nhánh chính (`main`) đã qua kiểm tra tự động hoặc thẻ phiên bản (`v*`) mới được xuất bản thành bản phát hành chính thức.

### Vai trò chịu trách nhiệm (Team Roles)

- **Tác giả mã nguồn & Người rà soát (Authors & Reviewers):** **Nguyễn Hoàng Tùng** (`@tungkernel`) và các cộng tác viên được chấp nhận qua Pull Request công khai.
- **Người quản lý phát hành (Release Maintainer):** **Nguyễn Hoàng Tùng** (`@tungkernel`), chủ sở hữu kho mã nguồn.

### Tuyên Bố Quyền Riêng Tư (Privacy Statement)

This program will not transfer any information to other networked systems unless specifically requested by the user or the person installing or operating it.

Cụ thể trong **ZCL Teleport**: mọi thao tác truyền tệp và đồng bộ văn bản chỉ diễn ra trực tiếp giữa các thiết bị do chính người dùng ghép đôi trong cùng mạng nội bộ, sử dụng kết nối mã hóa an toàn giữa hai máy. Ứng dụng không kết nối máy chủ đám mây, không thu thập dữ liệu đo lường hay báo cáo sử dụng ngầm.

