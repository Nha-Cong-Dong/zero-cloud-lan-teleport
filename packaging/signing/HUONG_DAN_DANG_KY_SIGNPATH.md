# Hướng Dẫn Đăng Ký Ký Mã Miễn Phí Cho Nguồn Mở (SignPath Foundation)

ZCL Teleport đã chuẩn bị sẵn đầy đủ chính sách bảo mật, cấu hình đóng gói và bước tích hợp CI/CD để tác giả (`@Luffyreals`) nộp hồ sơ nhận chứng chỉ ký mã Windows miễn phí từ **SignPath Foundation** (<https://signpath.org/>).

---

## 1. Những gì đã có sẵn trong mã nguồn

- `docs/community/SECURITY.md`: Chính sách ký mã (Code Signing Policy) công khai danh sách tác giả/người duyệt (`Nguyễn Hoàng Tùng (@Luffyreals)`), tuyên bố không thu thập dữ liệu (Privacy Statement) và quy trình build tự động từ GitHub CI.
- `README.md`: Mục ghi nhận nguồn tài trợ ký mã miễn phí từ SignPath Foundation.
- `packaging/signing/artifact-configuration.xml`: Quy tắc chỉ định chính xác tệp `.exe`, `.dll`, `.pyd` cần ký bên trong bộ cài `teleport-setup-*-x64.exe`, bản zip GUI và bản zip CLI.
- `packaging/signing/sign_windows.py`: Script hỗ trợ ký bằng `signtool.exe` (tự động bỏ qua an toàn khi chạy build thử hoặc khi chưa nạp chứng chỉ PFX).
- `packaging/release/release.py`: Đã hỗ trợ cờ `--signed` (hoặc biến môi trường `TELEPORT_SIGNED=true`) để tự động chuyển nội dung Release Note sang trạng thái **đã ký số**.

---

## 2. Mẫu điền đơn đăng ký gửi SignPath Foundation

Truy cập <https://signpath.org/apply/> bằng tài khoản GitHub của chủ dự án (`@Luffyreals`) và điền các mục theo mẫu dưới đây:

- **Project Name:** `ZCL Teleport`
- **Website / Repository URL:** `https://github.com/Nha-Cong-Dong/zero-cloud-lan-teleport` *(thay bằng đúng URL GitHub công khai của repo nếu đổi đường dẫn)*
- **License:** `GPL-3.0-or-later`
- **Build System:** `GitHub Actions`
- **Code Signing Policy URL:** Liên kết trực tiếp tới mục "Chính Sách Ký Mã (Code Signing Policy)" trong `docs/community/SECURITY.md`.
- **Short Project Description:**
  > ZCL Teleport is a free, open-source (GPL-3.0-or-later) local network file transfer and text synchronization utility built in Python and PySide6. All transfers occur directly between paired devices on the local network with encrypted connections and zero cloud servers, telemetry, or analytics.
- **Role / Organization Details:**
  - Single maintainer / approver: Nguyễn Hoàng Tùng (`@Luffyreals`)
  - CI runs purely from `main` branch and protected version tags (`v*`) via `.github/workflows/build-cli.yml`, `.github/workflows/build-gui.yml`, and `.github/workflows/release.yml`.

---

## 3. Kích hoạt sau khi được SignPath duyệt

Sau khi SignPath Foundation cấp Organization ID, Signing Policy và API Token:

1. Nạp cấu hình `packaging/signing/artifact-configuration.xml` vào Artifact Configuration của Project trên bảng điều khiển SignPath.
2. Vào GitHub Repository → **Settings → Secrets and variables → Actions**, thêm:
   - **Secrets:**
     - `SIGNPATH_ORGANIZATION_ID`
     - `SIGNPATH_API_TOKEN`
   - **Variables:**
     - `TELEPORT_SIGNED` = `true` (chỉ bật biến này sau khi pipeline phát hành đã thực sự xuất ra tệp có chữ ký số).
3. Nếu về sau dự án dùng thêm chứng chỉ ký PFX riêng trong CI, chỉ cần thêm Secrets `WINDOWS_CERT_PFX_BASE64` và `WINDOWS_CERT_PASSWORD`; các workflow `build-cli.yml` và `build-gui.yml` sẽ tự động gọi `packaging/signing/sign_windows.py sign --if-configured ...`.
4. Nếu dùng SignPath Action trực tiếp trong `release.yml`, giữ biến `TELEPORT_SIGNED=true` để ghi chú phát hành tự động loại bỏ cảnh báo bản chưa ký mã.
