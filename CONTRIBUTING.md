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
pytest
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) chạy đúng 3 lệnh trên, trên Windows, macOS và Linux.

## Phát hành
1. Chuyển mục `[Unreleased]` trong `CHANGELOG.md` thành `[X.Y.Z] - YYYY-MM-DD`.
2. Cập nhật `version` trong `pyproject.toml` và `__version__` trong `src/teleport/__init__.py`.
3. Gắn tag `vX.Y.Z` lên `main` rồi push tag.

Đánh số theo [SemVer](https://semver.org/lang/vi/). Phiên bản giao thức mạng đánh số riêng, độc lập với phiên bản app.

## Bảo mật
Mọi dữ liệu nhận từ máy khác đều coi là không tin cậy: kiểm tra kiểu, độ dài và giá trị của từng trường trước khi dùng. Tên file nhận được phải được làm sạch trước khi ghi ra đĩa.
