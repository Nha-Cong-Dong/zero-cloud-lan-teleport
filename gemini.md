# ZCL Teleport - Hướng Dẫn Phát Triển & Trợ Lý AI

Tài liệu hướng dẫn tổng quan và bộ quy chuẩn phát triển cho ZCL Teleport. Mọi trợ lý AI và lập trình viên khi làm việc trong dự án đều phải tuân thủ nghiêm ngặt các nguyên tắc dưới đây.

---

## 1. Thông Tin Dự Án & Bản Quyền

- **Tên dự án:** ZCL Teleport
- **Tác giả:** **Nguyễn Hoàng Tùng** (`@Luffyreals`)
- **Bản quyền:** Copyright (C) 2026 Nguyễn Hoàng Tùng
- **Giấy phép:** GNU General Public License v3.0 or later (GPL-3.0-or-later)
- **Mục đích:** Chuyển file dung lượng lớn và đồng bộ văn bản trực tiếp giữa các máy tính trong cùng mạng nội bộ.

---

## 2. Quy Tắc Giao Tiếp & Diễn Đạt (Bắt Buộc)

1. **Ngôn ngữ bình dân, dễ hiểu:**
   - Tuyệt đối không dùng thuật ngữ chuyên ngành phức tạp khó hiểu khi giao tiếp hoặc viết tài liệu hướng dẫn (như mDNS, WebRTC, TLS 1.3, NVENC, telemetry, subnet, Opus 48kHz...).
   - Luôn sử dụng từ ngữ thuần Việt, diễn đạt trực quan, gần gũi với người dùng thông thường (ví dụ: thay vì "quét mDNS" dùng "tự tìm máy trong mạng", thay vì "TLS 1.3 mutual handshake" dùng "kết nối mã hóa an toàn giữa hai máy", thay vì "chunked resume" dùng "tự động gửi tiếp phần còn thiếu").
2. **Không so sánh hay nhắc tên ứng dụng khác:**
   - Không được đề cập, so sánh hay đối chiếu với bất kỳ ứng dụng bên thứ ba nào (như TeamViewer, AnyDesk, UltraViewer, LocalSend...).
3. **Không dùng từ "hệ sinh thái":**
   - Tuyệt đối tránh sử dụng các từ "hệ sinh thái", "ecosystem" trong toàn bộ tài liệu và mã nguồn.
4. **Không dùng cụm từ tiêu đề dài rườm rà:**
   - Tuyệt đối không dùng cụm từ dài: "Bộ Ứng Dụng Mạng Nội Bộ Zero-Cloud LAN". Sử dụng ngắn gọn: **ZCL Teleport**.
5. **Biểu tượng tối giản (Monochrome Line Vector):**
   - Không sử dụng emoji mặc định màu mè trong văn bản hướng dẫn hoặc giao diện.
   - Chỉ sử dụng vector SVG dạng nét đơn giản (monochrome line, stroke-width từ 1.8 đến 2.0).

---

## 3. Cấu Trúc Mã Nguồn & Thư Mục

Dự án được bố cục chuẩn hóa, tách biệt rõ ràng giữa mã nguồn, kiểm thử, đóng gói và tài liệu:

```
teleport/
├── src/                          # Toàn bộ mã nguồn ứng dụng
│   └── teleport/
│       ├── core/                 # Giao thức thuần túy, thuật toán (không làm I/O)
│       ├── trust/                # Quản lý danh tính, chứng chỉ máy và độ tin cậy
│       ├── net/                  # Kết nối mạng nội bộ và mã hóa truyền dẫn
│       ├── app/                  # Điều phối nghiệp vụ, lưu trữ và luồng truyền
│       ├── ui/                   # Giao diện đồ họa PySide6 / Qt
│       └── cli.py                # Giao diện dòng lệnh
├── docs/                         # Toàn bộ tài liệu dự án
│   ├── README.md                 # Bản đồ hướng dẫn tra cứu tài liệu
│   ├── product/                  # Tài liệu sản phẩm, định hướng & kế hoạch
│   │   ├── y_tuong.md
│   │   ├── mvp.md
│   │   ├── ke_hoach.md
│   │   └── hieu_du_an.md
│   ├── technical/                # Đặc tả kỹ thuật & kiến trúc
│   │   ├── architecture.md
│   │   ├── protocol.md
│   │   └── benchmark.md
│   ├── community/                # Quy chuẩn đóng góp & chính sách
│   │   ├── CONTRIBUTING.md
│   │   ├── SECURITY.md
│   │   └── CHANGELOG.md
│   └── assets/                   # Hình ảnh giao diện thực tế và tài nguyên
├── packaging/                    # Công cụ đóng gói PyInstaller & Inno Setup
├── tests/                        # Hệ thống kiểm thử tự động (pytest)
├── pyproject.toml                # Cấu hình dự án Python
├── LICENSE                       # Giấy phép bản quyền GPL-3.0
└── README.md                     # Trang giới thiệu chính của repository
```

---

## 4. Quy Chuẩn Kiến Trúc Mã Nguồn

Tuân thủ nguyên tắc phân tầng bất di bất dịch:
$$\text{core} \leftarrow \text{trust} \leftarrow \text{net} \leftarrow \text{app} \leftarrow \text{ui} \text{ / } \text{cli}$$

- Tầng dưới tuyệt đối **không** import tầng trên.
- Tầng `core` là logic giao thức thuần, hoàn toàn không thao tác với thời gian hệ thống, bộ sinh số ngẫu nhiên hay I/O (không đọc ghi đĩa, không mở mạng), giúp kiểm thử nhanh và ổn định tuyệt đối.
- Mọi vi phạm kiến trúc phân tầng sẽ bị kiểm tra chặn lại ngay lập tức tại `tests/test_architecture.py`.

---

## 5. Đội Ngũ Subagents & Quy Trình Làm Việc

Các trợ lý AI chuyên biệt được cấu hình trong thư mục `.agents/` (đồng bộ cùng `.claude/agents/`) gồm hai nhóm:

### 5.1. Nhóm Chuyên Gia Miền (Domain Specialists)

1. **`lan-protocol-specialist`** (`.agents/lan-protocol-specialist.md`):
   - Chuyên trách logic kết nối trực tiếp hai máy tính, tìm kiếm tự động trong mạng nội bộ, chia nhỏ khối truyền tải và cơ chế tiếp tục khi đứt kết nối.
2. **`desktop-ui-designer`** (`.agents/desktop-ui-designer.md`):
   - Chuyên trách giao diện máy tính với PySide6 / Qt, biểu tượng nét tối giản SVG monochrome, chế độ sáng tối, trợ năng và bố cục trực quan.
3. **`packaging-engineer`** (`.agents/packaging-engineer.md`):
   - Chuyên trách đóng gói bộ cài đặt Windows (Inno Setup), file chạy độc lập (PyInstaller), kiểm tra tính tương thích bản quyền phụ thuộc (GPL/LGPL).
4. **`qa-tester`** (`.agents/qa-tester.md`):
   - Chuyên trách kiểm thử tự động (pytest, hypothesis), bảo đảm quy tắc phân tầng kiến trúc, độ tin cậy và không bao giờ để lỗi hồi quy xảy ra.

### 5.2. Nhóm Quy Trình Kỹ Thuật (Pipeline Agents)

| Agent | Vai trò | Sửa file? |
|---|---|---|
| **`truong-nhom`** | Lập kế hoạch thiết kế, chia việc, đặt tiêu chí xong; cuối cùng ra phán quyết ĐẠT / CHƯA ĐẠT | Không |
| **`hoi-lai`** | Phản biện kế hoạch trước khi code: tìm chỗ mơ hồ, mâu thuẫn kiến trúc/giao thức, rủi ro bảo mật | Không |
| **`lap-trinh`** | Thực hiện việc được giao trong `src/` (kèm cập nhật `protocol.md`, `architecture.md`), chạy ruff + mypy + pytest | Có (`src/`) |
| **`viet-test`** | Viết test trong `tests/` từ đặc tả (chạy song song với lập trình); không bao giờ sửa `src/` | Có (`tests/`) |
| **`soat-code`** | Soát `git diff` về tính đúng đắn, bảo mật, phân tầng, giao thức và tài liệu | Không |
| **`tan-cong`** | Viết và chạy script tấn công trên `127.0.0.1` trong `scratch/` để tái hiện lỗi | Chỉ `scratch/` |
| **`do-dac`** | Đo thực tế `mvp N1`–`N12` và ghi kết quả vào `docs/technical/benchmark.md` | Chỉ `benchmark.md` & `scratch/` |
| **`soat-tai-lieu`** | Soát `docs/`, `README.md`, `gemini.md`, `.agents/` theo luật "mỗi sự thật một chỗ" và quy tắc diễn đạt | Không |
| **`giao-dien`** | Lập trình tầng `ui` (PySide6/Qt), đa ngôn ngữ (F11) và trợ năng (N12) | Có (`src/teleport/ui/`, `tests/ui/`) |
| **`dong-goi`** | Xây dựng bộ cài, workflow phát hành và kiểm tra giấy phép phụ thuộc | Có (`packaging/`, `.github/workflows/`) |

---

*Cập nhật lần cuối: 2026-11-10 · Tác giả: Nguyễn Hoàng Tùng (@Luffyreals)*
