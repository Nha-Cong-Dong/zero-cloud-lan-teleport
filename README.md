# Zero-Cloud LAN Teleport

Chuyển file nặng hàng chục GB và đồng bộ clipboard giữa các máy tính trong cùng mạng Wi-Fi/LAN.
**Không cloud, không tài khoản**, tự nối lại khi rớt mạng.

> **Trạng thái:** đang phát triển, chưa có bản phát hành. Lịch sử thay đổi: [CHANGELOG.md](CHANGELOG.md).

## Tính năng dự kiến cho v1.0
- **Radar:** tự tìm các máy trong cùng mạng, không cần gõ IP.
- **Gửi file lớn:** kéo thả vào thiết bị; ghi thẳng ra đĩa, RAM không tăng theo kích thước file.
- **Tự nối lại:** rớt mạng thì chỉ gửi tiếp phần còn thiếu, kiểm tra toàn vẹn bằng cây băm SHA-256 Merkle.
- **Clipboard:** Ctrl+C trên máy này, Ctrl+V trên máy kia.
- **Bảo mật:** TLS 1.3, ghép đôi bằng mã xác minh 6 số.
- **Riêng tư:** không telemetry, không kết nối ra ngoài mạng LAN.

## Cài đặt môi trường phát triển (Windows, PowerShell)

Cần Python 3.12 trở lên:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

Nếu PowerShell chặn `Activate.ps1`, chạy lệnh này một lần rồi kích hoạt lại:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Quy trình nhánh, commit và phát hành: xem [CONTRIBUTING.md](CONTRIBUTING.md).

## Ví dụ: chat TLS giữa 2 máy

Cả hai máy đều cài đặt như trên, và cùng kết nối một mạng Wi-Fi/LAN.

**Máy A** chờ kết nối:

```powershell
python examples/chat_tls.py serve --name An
```

Máy A in ra:
- **IP trong LAN**: có thể hiện nhiều dòng. Các IP `192.168.56.x` hay của VMware/Hyper-V là card mạng ảo; chọn IP của Wi-Fi/Ethernet thật (xem bằng `ipconfig`).
- **Pin**: 8 byte đầu fingerprint chứng chỉ của máy A.

Lần đầu chạy, Windows Firewall sẽ hỏi cho phép Python. Chọn **Private networks** (mạng riêng).

**Máy B** kết nối tới A:

```powershell
python examples/chat_tls.py connect 192.168.1.7 --name Binh --pin BC:B8:24:87:77:4A:1A:3F
```

Gõ tin nhắn rồi Enter. Gõ `/quit` để thoát.

Muốn thử trên một máy, mở 2 cửa sổ terminal:

```powershell
python examples/chat_tls.py serve --host 127.0.0.1 --config-dir .teleport-a
python examples/chat_tls.py connect 127.0.0.1 --pin <pin máy A in ra>
```

Chạy `connect` với `--pin` sai sẽ thấy cảnh báo MITM và chương trình thoát.

### Ví dụ này minh họa gì

| Khái niệm | Ở đâu | Sẽ dùng cho |
|---|---|---|
| TCP là dòng byte → cần **khung có tiền tố độ dài** | `src/teleport/core/framing.py` | Kênh điều khiển |
| Danh tính máy = **chứng chỉ tự ký**, fingerprint SHA-256 | `src/teleport/net/identity.py` | Ghép đôi |
| **TLS 1.3** + **ghim fingerprint** chống MITM | `src/teleport/net/tls.py` | Mọi kết nối |
| `asyncio`: gửi và nhận song song trên một event loop; `drain()` làm backpressure | `examples/chat_tls.py` | Truyền file |

## Cấu trúc

```
src/teleport/
  core/framing.py   # [4 byte độ dài][JSON]
  net/identity.py   # khóa + chứng chỉ tự ký
  net/tls.py        # SSLContext, đọc fingerprint, kiểm tra pin
examples/chat_tls.py
tests/              # pytest
docs/nhat_ky.md     # nhật ký phát triển hằng tuần
.github/workflows/  # CI
```
