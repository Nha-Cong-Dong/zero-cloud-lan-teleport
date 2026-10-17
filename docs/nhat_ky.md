# Nhật ký dự án

Mỗi tuần 3–5 dòng: đã làm gì, vướng gì, tuần sau làm gì. Dùng làm tư liệu cho `CHANGELOG.md` và tài liệu dự án.

## Tuần 1 — Giai đoạn 0 (bắt đầu 17/10/2026)

**Đã làm**
- Dựng project Python (`pyproject.toml`, `src/`, `tests/`), virtualenv, pytest + pytest-asyncio.
- `framing.py`: khung `[4 byte độ dài][JSON]`, có giới hạn 16 MiB và xử lý kết nối bị cắt giữa chừng.
- `identity.py`: tạo khóa ECDSA P-256 và chứng chỉ tự ký, fingerprint SHA-256.
- `tls.py`: TLS 1.3, ghim fingerprint (pin 8 byte).
- Bài tập `examples/chat_tls.py`: chat 1-1 qua TLS. 25 test pass; đã chạy thử 2 tiến trình trên localhost (pin đúng thì chat được, pin sai thì bị từ chối).

**Vướng / ghi chú**
- `input()` chặn event loop → đọc bàn phím trong thread riêng, đẩy vào `asyncio.Queue`.
- Máy có nhiều card mạng ảo nên in ra nhiều IP; phải chọn IP Wi-Fi thật.
- Python `ssl` không cho tự viết hàm kiểm tra chứng chỉ phía server → ở Giai đoạn 1 cần tìm cách xác thực **hai chiều** (máy chủ cũng kiểm tra máy khách).

**Tuần sau**
- [ ] Chạy chat giữa 2 máy thật trong LAN.
- [ ] Bắt đầu Giai đoạn 1: `discovery.py` với `zeroconf`.
