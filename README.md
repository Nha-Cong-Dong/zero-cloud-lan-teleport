<div align="center">

# 🛰️ Teleport

### Zero-Cloud LAN Teleport

**Chuyển file nặng hàng chục GB và đồng bộ clipboard giữa các máy tính trong cùng mạng Wi-Fi/LAN.**
**Không cloud · Không tài khoản · Rớt mạng vẫn gửi tiếp.**

[![CI](https://github.com/Nha-Cong-Dong/zero-cloud-lan-teleport/actions/workflows/ci.yml/badge.svg)](https://github.com/Nha-Cong-Dong/zero-cloud-lan-teleport/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)
![Nền tảng](https://img.shields.io/badge/n%E1%BB%81n%20t%E1%BA%A3ng-Windows%20%7C%20macOS%20%7C%20Linux-555)
![Giấy phép](https://img.shields.io/badge/gi%E1%BA%A5y%20ph%C3%A9p-GPL--3.0-blue)
![Trạng thái](https://img.shields.io/badge/tr%E1%BA%A1ng%20th%C3%A1i-%C4%91ang%20ph%C3%A1t%20tri%E1%BB%83n-orange)

</div>

> [!NOTE]
> **Teleport đang được phát triển và chưa có bản phát hành.**
> - **Đã có:** danh tính thiết bị (chứng chỉ tự ký), kênh TLS 1.3 với xác thực hai chiều giữa các thiết bị tin cậy, khung message điều khiển, khám phá thiết bị qua mDNS, ghép đôi qua mạng bằng mã 6 số có nonce cam kết, kho thiết bị tin cậy, tự giữ kết nối điều khiển tới thiết bị đã ghép đôi, khóa một node mỗi thư mục cấu hình, các lệnh `teleport identity` / `list` / `listen` / `pair` / `unpair`.
> - **Tiếp theo:** gửi file lớn qua dòng lệnh (`v0.1.0-alpha`). Thời gian tìm thấy nhau (mvp N1) đã đạt khi đo trên máy ảo ([docs/benchmark.md](docs/benchmark.md)).
> - **Chưa có:** gửi file, nối lại, clipboard, giao diện.
>
> Các tính năng bên dưới là **mục tiêu của v1.0**. Tiến độ xem ở [Lộ trình](#lộ-trình); lịch sử thay đổi ở [docs/CHANGELOG.md](docs/CHANGELOG.md).

## Mục lục

- [Vì sao có Teleport?](#vì-sao-có-teleport)
- [Nguyên tắc sản phẩm](#nguyên-tắc-sản-phẩm)
- [Tính năng](#tính-năng)
- [Trải nghiệm sử dụng](#trải-nghiệm-sử-dụng)
- [Cách hoạt động](#cách-hoạt-động)
- [Bảo mật & quyền riêng tư](#bảo-mật--quyền-riêng-tư)
- [Yêu cầu hệ thống & mạng](#yêu-cầu-hệ-thống--mạng)
- [Lộ trình](#lộ-trình)
- [Dành cho nhà phát triển](#dành-cho-nhà-phát-triển)
- [Kiến trúc](#kiến-trúc)
- [Tài liệu](#tài-liệu)
- [Câu hỏi thường gặp](#câu-hỏi-thường-gặp)
- [Đóng góp · Bảo mật · Giấy phép](#đóng-góp--bảo-mật--giấy-phép)

---

## Vì sao có Teleport?

Hai máy tính đặt cạnh nhau, dùng chung một cục Wi-Fi, vậy mà muốn chuyển một file 5GB vẫn phải đi đường vòng:

| Cách thường dùng | Vấn đề |
|---|---|
| **Zalo / Messenger** | Ảnh bị nén, file lớn bị chặn, phải đăng nhập tài khoản cá nhân lên máy lạ |
| **Google Drive** | Tải lên rồi tải về: tốn gấp đôi thời gian và băng thông, dù hai máy cách nhau 20cm |
| **USB** | Phải có USB, cắm, copy, rút, cắm sang máy kia |
| **Chia sẻ thư mục Windows** | Cấu hình quyền phức tạp; rớt mạng là phải copy lại từ đầu |

**Teleport gửi file thẳng từ máy này sang máy kia** qua mạng nội bộ, với tốc độ tối đa của mạng. Dữ liệu không đi qua bất kỳ máy chủ nào.

### Giá trị cốt lõi

| | Giá trị | Nghĩa là |
|---|---|---|
| 🚪 | **Zero-Friction** | Không tài khoản, không gõ IP. Cài xong, máy kia tự hiện ra |
| ☁️ | **Zero-Cloud** | Dữ liệu chỉ đi trong mạng nội bộ, được mã hóa. App không gửi gì ra Internet |
| 🧠 | **Zero-RAM-Overflow** | File 50GB vẫn đi thẳng từ ổ đĩa máy gửi sang ổ đĩa máy nhận; RAM không tăng theo kích thước file |
| 🔁 | **Không sợ rớt mạng** | Wi-Fi chập chờn hay gập laptop giữa chừng thì tự nối lại và chỉ gửi phần còn thiếu; mọi khối đều được kiểm tra toàn vẹn |

## Nguyên tắc sản phẩm

Mọi quyết định đều dựa trên sáu nguyên tắc: **riêng tư tuyệt đối**, **an toàn theo mặc định**, **không bao giờ mất hay hỏng dữ liệu**, **nói ngôn ngữ người dùng**, **lõi trước, giao diện sau**, **làm ít mà chắc**. Giải thích từng nguyên tắc: [docs/y_tuong.md](docs/y_tuong.md) mục 6.

## Tính năng

| | Tính năng | Mô tả |
|---|---|---|
| 📡 | **Radar tự tìm thiết bị** | Mở app là thấy các máy khác đang chạy Teleport trong cùng mạng. Mạng chặn tự tìm thì vẫn nhập IP được |
| 🤝 | **Ghép đôi một lần** | Lần đầu, hai máy hiện cùng một mã 6 số để bạn xác nhận. Các lần sau tự nhận ra nhau |
| 📦 | **Gửi file cực lớn** | Kéo thả một hoặc nhiều file vào thiết bị trên radar. Người nhận được hỏi trước khi nhận (tắt được cho từng thiết bị tin cậy) |
| 🔁 | **Tự nối lại** | Rớt Wi-Fi ở 85% thì khi có mạng lại chỉ gửi tiếp phần còn thiếu, kể cả khi đã tắt app nhận hay khởi động lại máy nhận |
| 🧩 | **Kiểm tra toàn vẹn** | Mỗi khối 1 MiB được kiểm tra bằng SHA-256; cả file được đối chiếu bằng cây băm Merkle. Khối hỏng được gửi lại tự động |
| 📋 | **Đồng bộ clipboard** | Ctrl+C văn bản hoặc ảnh chụp màn hình trên máy này, Ctrl+V trên máy kia. Bật riêng cho từng thiết bị, mặc định tắt |
| 🔒 | **Bảo mật** | TLS 1.3; chỉ thiết bị đã ghép đôi mới gửi được file hay clipboard, theo cả hai chiều |
| 🕶️ | **Riêng tư** | Không tài khoản, không telemetry, không máy chủ |
| 🖥️ | **Chạy nền** | Thu xuống khay hệ thống, luôn sẵn sàng nhận |
| 🌐 | **Hai ngôn ngữ** | Tiếng Việt và English |
| ⌨️ | **Dòng lệnh** | Lệnh `teleport` cho người dùng nâng cao và viết script |

## Trải nghiệm sử dụng

```
1. Mở Teleport trên hai máy  ──►  máy kia hiện trên radar sau vài giây
2. Kéo video.mp4 (8 GB) thả vào “PC-Nha”
3. Lần đầu: hai máy cùng hiện mã  4 8 2  9 1 3  ──►  bấm “Trùng khớp” ở cả hai máy
4. PC-Nha: “Laptop-Minh muốn gửi video.mp4 (8,0 GB)”  ──►  Nhận
5. Thanh tiến độ, tốc độ, thời gian còn lại  ──►  Wi-Fi rớt?  “Đang nối lại…”  ──►  gửi tiếp phần thiếu
6. “Đã nhận video.mp4 ✔ đã kiểm tra toàn vẹn”  ──►  bấm để mở thư mục
```

## Cách hoạt động

```
   Máy A                                                  Máy B
┌──────────┐  ① Tìm nhau qua mDNS (multicast trong LAN)  ┌──────────┐
│ Teleport │ ◄─────────────────────────────────────────► │ Teleport │
│          │  ② Ghép đôi lần đầu: so mã 6 số             │          │
│          │  ③ TLS 1.3, hai bên xác thực chứng chỉ      │          │
│  file ───┼──► ④ Khối 1 MiB + SHA-256, có backpressure ─┼──► đĩa   │
│          │  ⑤ Rớt mạng → so bitfield → gửi phần thiếu  │          │
└──────────┘                                             └──────────┘
             Không máy chủ trung gian · Không cần Internet
```

1. **Khám phá:** mỗi máy quảng bá dịch vụ `_teleport._tcp.local.` qua mDNS, kèm mã thiết bị, tên, hệ điều hành và phiên bản giao thức. Mọi kết nối đi vào một cổng duy nhất.
2. **Danh tính & ghép đôi:** lần chạy đầu, mỗi máy tự tạo một khóa ECDSA P-256 và chứng chỉ tự ký. Fingerprint SHA-256 của chứng chỉ là định danh của máy. Khi ghép đôi, hai máy trao nhau nonce ngẫu nhiên (máy đang chờ ghép đôi cam kết nonce của mình trước khi thấy nonce bên kia, và hiện mã rồi mới lộ nonce), rồi cùng tính một mã 6 số từ hai fingerprint và hai nonce. Nhờ cam kết, kẻ đứng giữa không thể dò trước khóa cho ra mã trùng, cũng không thể dò mã của máy đang chờ mà máy đó không hiện mã lên. Người dùng xác nhận mã trùng ở cả hai máy thì hai bên lưu chứng chỉ của nhau.
3. **Kết nối:** TLS 1.3, xác thực hai chiều (mTLS) bằng đúng chứng chỉ đã lưu khi ghép đôi. Client chỉ chấp nhận server đưa đúng chứng chỉ đã lưu, ngay trong bắt tay và trước khi gửi chứng chỉ của mình, nên máy giả mạo không nhận được chứng chỉ của máy kết nối tới nó; server chỉ chấp nhận client có chứng chỉ nằm trong danh sách tin cậy. Thiết bị gửi chứng chỉ lạ bị từ chối ngay trong lúc bắt tay; thiết bị không gửi chứng chỉ chỉ được ghép đôi. Máy lạ mở dồn dập kết nối bị giới hạn tốc độ, còn thiết bị đã ghép đôi và vừa kết nối gần đây được ưu tiên nên thường vẫn nối lại được (giới hạn còn lại: [docs/protocol.md](docs/protocol.md) mục 2.3).
4. **Truyền tải:** kênh điều khiển dùng message `[4 byte độ dài][JSON]`; dữ liệu đi trên kênh riêng theo khung nhị phân gọn. Bên gửi tự chờ khi mạng hoặc ổ đĩa bên nhận chậm hơn (backpressure), nên bộ nhớ không phình ra.
5. **Toàn vẹn & nối lại:** mỗi khối mang hash SHA-256 của chính nó; bên nhận băm lại và so ngay, ghi khối xuống đĩa rồi lưu bảng các khối đã xác minh (bitfield). Kết nối lại thì chỉ những khối còn thiếu được gửi tiếp. Cuối cùng bên nhận đọc lại cả file từ đĩa, so từng khối và Merkle root rồi mới đổi sang tên thật.
6. **Clipboard:** gửi văn bản/ảnh tới các thiết bị đã bật đồng bộ; nhớ hash nội dung vừa nhận để không gửi vòng lại.

Đặc tả chi tiết: [docs/protocol.md](docs/protocol.md).

## Bảo mật & quyền riêng tư

| Mối đe dọa | Cách Teleport xử lý |
|---|---|
| Người cùng Wi-Fi nghe lén | Toàn bộ dữ liệu mã hóa TLS 1.3 |
| Kẻ xấu giả làm máy của bạn (MITM) | Ghép đôi bằng mã 6 số có nonce cam kết; sau đó chỉ chấp nhận đúng chứng chỉ đã lưu |
| Thiết bị lạ gửi file rác | Máy chưa ghép đôi không kết nối được; mặc định hỏi trước khi nhận |
| File độc hại cố ghi đè hệ thống (`..\..\Windows\…`) | Làm sạch tên file, chỉ ghi trong thư mục nhận, không bao giờ ghi đè |
| Lộ mật khẩu qua clipboard | Đồng bộ clipboard mặc định tắt; bỏ qua nội dung mà trình quản lý mật khẩu đánh dấu nhạy cảm |
| Thu thập dữ liệu người dùng | Không telemetry, không tài khoản, không máy chủ. Kiểm tra cập nhật là tùy chọn, mặc định tắt |
| Theo dõi thiết bị qua nhiều mạng | Lần chạy đầu nói rõ tên máy hiện với mọi máy trong mạng; có công tắc ẩn máy khỏi máy chưa ghép đôi ([mvp mục 11](docs/mvp.md)) |

Phát hiện lỗ hổng? Xem [docs/SECURITY.md](docs/SECURITY.md).

## Yêu cầu hệ thống & mạng

| | Yêu cầu |
|---|---|
| **Hệ điều hành** | Windows 10 (22H2) / 11 · macOS 12+ · Ubuntu 22.04+ (các hệ điều hành gửi/nhận qua lại được với nhau) |
| **Mạng** | Các máy cùng một mạng Wi-Fi/LAN; không cần Internet |
| **Tường lửa** | Cho phép Teleport trên mạng **Private**. Bộ cài sẽ tự tạo rule |

> [!TIP]
> Wi-Fi ở quán cà phê, khách sạn hay văn phòng thường bật **cô lập thiết bị** (AP isolation): các máy thấy nhau nhưng không kết nối được. Khi đó hãy phát hotspot từ điện thoại và cho hai máy cùng vào.

## Lộ trình

| Phiên bản | Nội dung |
|---|---|
| — | ✅ Nền tảng: khung dự án, CI 3 hệ điều hành, danh tính thiết bị, TLS 1.3 + mTLS |
| — | ✅ Khám phá thiết bị & ghép đôi: mDNS, ghép đôi qua mạng, tự giữ kết nối, lệnh `teleport list` / `listen` / `pair` / `unpair`; đạt mvp N1 |
| `v0.1.0-alpha` | Gửi file lớn qua dòng lệnh giữa hai máy đã ghép đôi |
| `v0.2.0-alpha` | Kiểm tra toàn vẹn, tự nối lại |
| `v0.3.0-beta` | Giao diện radar, khay hệ thống, Tiếng Việt / English |
| `v0.4.0-beta` | Đồng bộ clipboard |
| `v1.0.0` | Bộ cài cho Windows, macOS, Linux; tài liệu người dùng |

**Sau v1.0:** app Android, chế độ khách qua trình duyệt cho điện thoại, gửi tới nhiều thiết bị cùng lúc, gửi và đồng bộ thư mục, app iOS, quảng bá ẩn danh.

Lộ trình chi tiết theo tuần: [docs/ke_hoach.md](docs/ke_hoach.md) mục 5. Trạng thái chi tiết từng module: [docs/architecture.md](docs/architecture.md#3-bản-đồ-module).

## Dành cho nhà phát triển

### Cài đặt

Cần **Python 3.12+**.

```powershell
git clone git@github.com:Nha-Cong-Dong/zero-cloud-lan-teleport.git
cd zero-cloud-lan-teleport
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
```

Nếu PowerShell chặn `Activate.ps1`, chạy `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` một lần.

### Lệnh thường dùng

| Lệnh | Tác dụng |
|---|---|
| `pytest` | Chạy toàn bộ test |
| `pytest -m "not integration"` | Bỏ qua test mở socket thật (nhanh hơn) |
| `pytest tests/net/test_tls.py::test_verify_fingerprint_accepts_equal` | Chạy một test |
| `ruff check .` · `ruff format .` | Kiểm tra và định dạng code (CI chạy `ruff check`, `ruff format --check`, `mypy src`, `pytest`) |
| `mypy src` | Kiểm tra kiểu |
| `teleport identity` | Tạo / xem danh tính thiết bị của máy này (hoặc `python -m teleport identity`) |
| `teleport list` | Liệt kê các máy thấy được qua mDNS trong vài giây |
| `teleport listen` | Chờ ghép đôi: hiện mã 6 số, hỏi xác nhận |
| `teleport pair <tên \| tiền tố mã \| ip[:cổng]>` | Ghép đôi với một máy |
| `teleport unpair <thiết bị>` | Hủy ghép đôi (khi không có `teleport listen` nào đang chạy) |
| `teleport receive [--dir <thư mục>] [--yes]` | Chờ nhận file từ các máy đã ghép đôi, hỏi `[c/k]` mỗi lượt (`--yes`: tự nhận); mặc định lưu vào `Teleport` trong thư mục Tải về |
| `teleport send <file...> --to <thiết bị> [--address ip[:cổng]] [--limit-rate 20M]` | Gửi file tới một máy đã ghép đôi (máy đó chạy `teleport receive`); Ctrl+C để hủy |
| `teleport --version` | Xem phiên bản |

### Chạy nhiều node trên một máy

Mỗi node cần một danh tính riêng. Hãy cho mỗi node một thư mục cấu hình bằng `TELEPORT_HOME` hoặc `--config-dir`:

```powershell
$env:TELEPORT_HOME = ".teleport-a"; teleport identity
$env:TELEPORT_HOME = ".teleport-b"; teleport identity
```

Thư mục cấu hình mặc định: Windows `%APPDATA%\Teleport`, macOS `~/Library/Application Support/Teleport`, Linux `~/.config/teleport`.

## Kiến trúc

```
   ui (PySide6)  ·  cli          lớp mỏng: hiển thị, nhận lệnh
        │
       app                       dịch vụ: Node, ghép đôi, quản lý lượt truyền, clipboard
        │
       net                       I/O mạng: TLS + mTLS, đọc/ghi message, mDNS, kết nối
        │
      trust                      danh tính thiết bị, kho thiết bị tin cậy
        │
      core                       giao thức thuần (sans-IO): framing, message, Merkle,
                                 máy trạng thái truyền file và nối lại
```

- **Tầng dưới không bao giờ import tầng trên**; `ui` và `cli` không import lẫn nhau.
- **`core` không làm I/O**: không socket, không asyncio, không Qt. Logic khó nhất (chọn khối gửi lại, nối lại) được viết thành máy trạng thái thuần, nên test được hàng nghìn kịch bản ngắt mạng ngẫu nhiên trong vài mili giây.
- Các quy tắc trên được **kiểm tra tự động** bởi `tests/test_architecture.py`.
- Mỗi thư mục cấu hình chỉ có **một node** chạy tại một thời điểm.

```
src/teleport/
  core/  trust/  net/  app/  # các tầng đã có code
  ui/                        # sẽ được thêm khi có code thật
  cli.py  __main__.py        # lệnh `teleport`
  paths.py  lock.py  atomic.py  # thư mục cấu hình, khóa file, ghi file nguyên tử
tests/                       # chia theo tầng: core/, trust/, net/, app/ + test phân tầng
docs/                      # toàn bộ tài liệu
```

Chi tiết: [docs/architecture.md](docs/architecture.md).

## Tài liệu

Mục lục, thuật ngữ và quy tắc đồng bộ tài liệu: [docs/README.md](docs/README.md).

| Tài liệu | Nội dung |
|---|---|
| [docs/y_tuong.md](docs/y_tuong.md) | Vì sao làm: nỗi đau, giá trị cốt lõi, nguyên tắc sản phẩm, tầm nhìn |
| [docs/mvp.md](docs/mvp.md) | Làm gì: phạm vi v1.0, tính năng F1–F12 kèm tiêu chí chấp nhận, yêu cầu N1–N12 |
| [docs/ke_hoach.md](docs/ke_hoach.md) | Làm thế nào, khi nào: thiết kế kỹ thuật, lộ trình và trạng thái, kiểm thử, rủi ro |
| [docs/architecture.md](docs/architecture.md) | Phân tầng, mô hình tiến trình, bản đồ module và trạng thái, dữ liệu trên đĩa |
| [docs/protocol.md](docs/protocol.md) | Đặc tả giao thức trên dây (phiên bản 1, dự thảo) |
| [docs/benchmark.md](docs/benchmark.md) | Số đo thực tế của các yêu cầu chất lượng |
| [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) | Nhánh, commit, kiểm tra trước Pull Request, phát hành |
| [docs/SECURITY.md](docs/SECURITY.md) | Cách báo lỗ hổng bảo mật |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | Lịch sử thay đổi |

## Câu hỏi thường gặp

<details>
<summary><b>Có cần Internet không?</b></summary>

Không. Teleport chỉ cần các máy cùng một mạng Wi-Fi/LAN. Bạn có thể rút dây Internet của router mà việc gửi file vẫn chạy.
</details>

<details>
<summary><b>Có giới hạn dung lượng file không?</b></summary>

Mỗi file tối đa 4 TB; ổ nhận FAT32 (USB cũ) chỉ chứa được file nhỏ hơn 4 GB ([mvp F4](docs/mvp.md)). Ngoài ra chỉ giới hạn bởi chỗ trống của ổ nhận. Dữ liệu được ghi thẳng ra đĩa theo từng khối, nên file 50GB cũng không làm tăng RAM.
</details>

<details>
<summary><b>Có cần cài Teleport trên cả hai máy không?</b></summary>

Có, ở v1.0. Chế độ khách qua trình duyệt (cho điện thoại không cài app) nằm trong lộ trình sau v1.0.
</details>

<details>
<summary><b>Hai máy cùng Wi-Fi mà không thấy nhau?</b></summary>

Thường gặp ở Wi-Fi công cộng bật cô lập thiết bị, hoặc khi tường lửa chặn. Hãy kiểm tra tường lửa đã cho phép Teleport trên mạng Private, thử nhập IP thủ công, hoặc dùng hotspot điện thoại.
</details>

<details>
<summary><b>Nếu tôi tắt máy giữa chừng thì sao?</b></summary>

Phần đã nhận vẫn nằm an toàn trên đĩa. Nếu máy **nhận** tắt hoặc khởi động lại, mở lại Teleport trên máy đó thì lượt truyền tự tiếp tục từ chỗ còn thiếu, miễn là Teleport trên máy gửi vẫn chạy. Nếu máy **gửi** tắt Teleport (hoặc tắt máy), lượt gửi bị hủy: máy nhận báo và dọn file tạm, bạn chỉ cần gửi lại. Nếu file gốc đã bị sửa trong lúc đó, Teleport sẽ báo và yêu cầu gửi lại.
</details>

<details>
<summary><b>Khác gì LocalSend, KDE Connect hay Nearby Share?</b></summary>

Teleport tập trung vào **file cực lớn và đường truyền chập chờn**: nối lại theo từng khối kể cả sau khi tắt app nhận, kiểm tra toàn vẹn bằng cây Merkle. Thêm vào đó là đồng bộ clipboard giữa máy tính với nhau và xác thực hai chiều bằng chứng chỉ.
</details>

## Đóng góp · Bảo mật · Giấy phép

- **Đóng góp:** quy trình nhánh, commit (Conventional Commits) và phát hành ở [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md).
- **Bảo mật:** báo lỗ hổng theo [docs/SECURITY.md](docs/SECURITY.md), **không** mở issue công khai.
- **Giấy phép:** [GPL-3.0-or-later](LICENSE). Bạn được dùng, sửa và phân phối lại Teleport; bản phân phối đã sửa đổi phải mở mã theo cùng giấy phép.
