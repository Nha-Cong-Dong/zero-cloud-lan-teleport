# Quy trình phát triển

## Nhánh
- `main` luôn chạy được và được bảo vệ: chỉ merge qua Pull Request khi CI xanh.
- Mỗi việc làm trên một nhánh riêng: `feat/<tên>`, `fix/<tên>`, `docs/<tên>`…

## Commit
Theo [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: thêm khám phá thiết bị qua mDNS
fix: chặn tên file chứa ../ khi nhận
docs: cập nhật hướng dẫn tường lửa
test: thêm test nối lại khi rớt mạng
```

Thay đổi nào người dùng nhận thấy được thì ghi thêm vào mục `[Unreleased]` của [CHANGELOG.md](CHANGELOG.md).

## Trước khi mở Pull Request

```powershell
ruff check .
ruff format .
mypy src
pytest
```

CI ([.github/workflows/ci.yml](../.github/workflows/ci.yml)) chạy `ruff check .`, `ruff format --check .`, `mypy src` và `pytest` trên Windows, macOS, Linux × Python 3.12 và 3.14. Vì vậy hãy chạy `ruff format .` trước khi push, để bước kiểm tra định dạng không báo lỗi.

Mức kiểm tra kiểu của từng gói nằm ở `[tool.mypy]` trong `pyproject.toml`. Lỗi kiểu thì sửa chú thích; chỉ dùng `# type: ignore[<mã lỗi>]` kèm lý do khi không còn cách nào khác.

Mạng hoặc máy CI chặn multicast thì đặt biến môi trường `TELEPORT_SKIP_MDNS=1` để bỏ qua các test mDNS thật (`tests/net/test_discovery.py`, `tests/app/test_node_mdns.py`).

## Phát hành
1. Chuyển mục `[Unreleased]` trong `CHANGELOG.md` thành `[X.Y.Z] - YYYY-MM-DD`.
2. Cập nhật `__version__` trong `src/teleport/__init__.py`. Đây là nguồn duy nhất của số phiên bản; `pyproject.toml` tự đọc từ đó.
3. Gắn tag `vX.Y.Z` lên `main` rồi push tag.

Đánh số theo [SemVer](https://semver.org/lang/vi/). Phiên bản giao thức mạng đánh số riêng, độc lập với phiên bản app.

## Thay đổi giao thức
Mọi thay đổi định dạng trên dây phải cập nhật [protocol.md](protocol.md) trong cùng Pull Request. Thay đổi không tương thích thì tăng số phiên bản giao thức.

## Giấy phép
Dự án theo giấy phép [GPL-3.0-or-later](../LICENSE). Gửi Pull Request nghĩa là bạn đồng ý đóng góp của mình được phát hành theo cùng giấy phép đó. Chỉ thêm thư viện có giấy phép tương thích GPLv3 (MIT, BSD, Apache-2.0, LGPL).

## Bảo mật
Báo lỗ hổng: xem [SECURITY.md](SECURITY.md).

Mọi dữ liệu nhận từ máy khác đều coi là không tin cậy: kiểm tra kiểu, độ dài và giá trị của từng trường trước khi dùng. Tên file nhận được phải được làm sạch trước khi ghi ra đĩa.
