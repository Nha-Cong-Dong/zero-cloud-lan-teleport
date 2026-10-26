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

## Build thử bản CLI (Windows)
Bản CLI được đóng gói thử bằng PyInstaller để sớm phát hiện lỗi đóng gói và antivirus. Đây chưa phải bản phát hành: không gắn tag, không tạo GitHub Release. File build nằm trong `packaging/pyinstaller/`: `teleport-cli.spec` (cấu hình build), `third_party.py` (kiểm giấy phép, sinh file giấy phép) và `requirements.txt` (công cụ build, ghim phiên bản).

Chạy từ gốc repo, trên cây làm việc đã commit hết:

```powershell
pip install -e ".[dev]" -r packaging/pyinstaller/requirements.txt
$env:PYTHONUTF8 = "1"   # như workflow
pyinstaller --noconfirm --clean --distpath dist --workpath build packaging/pyinstaller/teleport-cli.spec
```

Bản build theo chế độ `--onedir`, không `--onefile` (lý do: [ke_hoach.md](ke_hoach.md) mục 5.2, Giai đoạn 6). Kết quả nằm trong `dist/teleport/`: `teleport.exe`, thư viện trong `_internal/`, cùng `LICENSE`, `THIRD-PARTY-NOTICES.txt` (giấy phép các thành phần đóng vào) và `SOURCE.txt` (commit dùng để build, nơi lấy mã nguồn). Build từ cây làm việc còn thay đổi chưa commit thì `SOURCE.txt` ghi cảnh báo, và bản build đó không được phát hành. `build/` và `dist/` đã có trong `.gitignore`.

`third_party.py` kiểm giấy phép mọi gói thật sự đóng vào và **làm build thất bại** (`LicenseAuditError`) khi gặp gói hay giấy phép chưa duyệt, file không rõ thuộc gói nào, hoặc thành phần trong SBOM của gói có giấy phép chưa duyệt. Khi đó đừng lách: đối chiếu luật giấy phép ở [ke_hoach.md](ke_hoach.md) mục 1 (Giấy phép) và mục 4.1, hợp lệ thì mới sửa `APPROVED` trong `third_party.py` (cách làm: docstring của file). Riêng Universal CRT mà PyInstaller tìm thấy trên `PATH` (ví dụ trong thư mục `bin` của JDK) bị spec bỏ khỏi bản build trước bước kiểm, log build ghi số file đã bỏ (lý do: [ke_hoach.md](ke_hoach.md) mục 1, Giấy phép).

Chạy test khói trên exe vừa build (marker `frozen`, [architecture.md](architecture.md) mục 6). Nâng bản công cụ build trong `requirements.txt` thì build lại rồi chạy lại bước này.

```powershell
$env:TELEPORT_FROZEN_EXE = "$PWD\dist\teleport\teleport.exe"
pytest -m frozen
```

Build trên GitHub: workflow `build-cli` ([.github/workflows/build-cli.yml](../.github/workflows/build-cli.yml)) chỉ chạy tay (`workflow_dispatch`). Vào tab Actions → “Build CLI (thử)” → Run workflow, chọn nhánh `main` (hoặc `gh workflow run build-cli.yml --ref main`); nút chạy tay chỉ hiện khi file workflow đã có trên nhánh mặc định. Workflow build bằng đúng lệnh trên, chạy `pytest -m frozen` trên exe, rồi tải lên artifact `teleport-cli-<phiên bản>-<sha7>-windows`. Artifact là bản tạm, tự xóa sau thời hạn `retention-days` của workflow. File zip tải về chứa thẳng `teleport.exe`, `_internal\` và ba file giấy phép, không có thư mục bọc ngoài, nên hãy giải nén vào một thư mục riêng (ví dụ `C:\tp\teleport\`).

## Phát hành
1. Chuyển mục `[Unreleased]` trong `CHANGELOG.md` thành `[X.Y.Z] - YYYY-MM-DD`.
2. Cập nhật `__version__` trong `src/teleport/__init__.py`. Đây là nguồn duy nhất của số phiên bản; `pyproject.toml` tự đọc từ đó.
3. Gắn tag `vX.Y.Z` lên `main` rồi push tag.

Đánh số theo [SemVer](https://semver.org/lang/vi/). Phiên bản giao thức mạng đánh số riêng, độc lập với phiên bản app.

## Thay đổi giao thức
Mọi thay đổi định dạng trên dây phải cập nhật [protocol.md](protocol.md) trong cùng Pull Request. Thay đổi không tương thích thì tăng số phiên bản giao thức.

## Giấy phép
Dự án theo giấy phép [GPL-3.0-or-later](../LICENSE). Gửi Pull Request nghĩa là bạn đồng ý đóng góp của mình được phát hành theo cùng giấy phép đó. Chỉ thêm thư viện có giấy phép tương thích GPLv3 (danh sách giấy phép được chấp nhận: [ke_hoach.md](ke_hoach.md) mục 1 (Giấy phép) và mục 4.1).

## Bảo mật
Báo lỗ hổng: xem [SECURITY.md](SECURITY.md).

Mọi dữ liệu nhận từ máy khác đều coi là không tin cậy: kiểm tra kiểu, độ dài và giá trị của từng trường trước khi dùng. Tên file nhận được phải được làm sạch trước khi ghi ra đĩa.
