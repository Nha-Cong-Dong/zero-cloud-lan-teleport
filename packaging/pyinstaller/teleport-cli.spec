# -*- mode: python ; coding: utf-8 -*-
# Spec PyInstaller cho bản CLI Teleport (build thử, Windows). Lệnh build (chạy từ gốc repo) ở mục
# "Build thử bản CLI (Windows)" của docs/CONTRIBUTING.md; workflow: .github/workflows/build-cli.yml.
# Kết quả: dist/teleport/teleport.exe, thư viện trong dist/teleport/_internal/, cùng LICENSE,
# THIRD-PARTY-NOTICES.txt, SOURCE.txt cạnh teleport.exe.
#
# Giấy phép (ke_hoach mục 1, Giấy phép; Giai đoạn 6 ở mục 5.2):
# - Chỉ dùng --onedir (COLLECT), không --onefile, để người dùng thay được thư viện LGPL.
# - zeroconf (LGPL-2.1-or-later) đóng dạng file riêng trong _internal/zeroconf/ (module_collection_mode
#   "py": file .py nguyên gốc; module Cython vẫn là .pyd riêng), không gộp vào kho PYZ trong exe.
# - third_party.check kiểm giấy phép mọi file thật sự đóng vào và dựng sẵn nội dung file giấy phép
#   NGAY SAU Analysis/PYZ, trước EXE/COLLECT. Kiểm hỏng thì spec xóa bản build cũ dist/teleport (nếu
#   có, vì --clean không dọn distpath) rồi dừng, nên dist/ không còn teleport.exe thiếu giấy phép.
#   third_party.write_outputs chỉ ghi các file đó sau COLLECT.
import importlib.util
import sys
from pathlib import Path

from packaging.version import Version
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32 import versioninfo as vi

SPEC_DIR = Path(SPECPATH).resolve()
REPO_DIR = SPEC_DIR.parents[1]
SRC_DIR = REPO_DIR / "src"

sys.path.insert(0, str(SPEC_DIR))
import third_party  # noqa: E402  (nằm cạnh file spec)

# Phiên bản chỉ lấy từ teleport.__version__ (đọc thẳng từ src/, không phụ thuộc gói đã cài).
_init_spec = importlib.util.spec_from_file_location("_teleport_version", SRC_DIR / "teleport" / "__init__.py")
_init_mod = importlib.util.module_from_spec(_init_spec)
_init_spec.loader.exec_module(_init_mod)
APP_VERSION = _init_mod.__version__
_release = (tuple(Version(APP_VERSION).release) + (0, 0, 0, 0))[:4]

# Module Cython của zeroconf (.pyd) import lẫn nhau từ mã máy nên PyInstaller không dò được;
# gom mọi module con cho chắc.
hiddenimports = collect_submodules("zeroconf")

a = Analysis(
    [str(SPEC_DIR / "launcher.py")],
    pathex=[str(SRC_DIR)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Không cần lúc chạy: giao diện Tk, bộ test của Python, công cụ dev cài cùng môi trường build.
    excludes=[
        "tkinter",
        "_tkinter",
        "turtle",
        "turtledemo",
        "idlelib",
        "test",
        "unittest",
        "pydoc_data",
        "pytest",
        "_pytest",
        "hypothesis",
        "mypy",
        "setuptools",
        "pip",
    ],
    module_collection_mode={"zeroconf": "py"},
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# Kiểm giấy phép trước khi tạo teleport.exe: mọi file sẽ vào bản build, kể cả module khởi động mà PYZ
# đưa thêm (pyz.dependencies). Lỗi thì build dừng ở đây với LicenseAuditError.
try:
    license_audit = third_party.check(
        [*a.scripts, *a.pure, *a.binaries, *a.datas, *pyz.dependencies],
        repo_dir=REPO_DIR,
        app_version=APP_VERSION,
    )
except Exception:
    # Chỉ xóa đúng DISTPATH/teleport (remove_stale_dist từ chối mọi đường dẫn khác).
    third_party.remove_stale_dist(Path(DISTPATH), third_party.DIST_NAME)
    raise

version_info = vi.VSVersionInfo(
    ffi=vi.FixedFileInfo(filevers=_release, prodvers=_release),
    kids=[
        vi.StringFileInfo(
            [
                vi.StringTable(
                    "040904B0",
                    [
                        vi.StringStruct("FileDescription", "Teleport CLI - chuyển file trong mạng LAN"),
                        vi.StringStruct("FileVersion", APP_VERSION),
                        vi.StringStruct("InternalName", "teleport"),
                        vi.StringStruct("LegalCopyright", "GPL-3.0-or-later"),
                        vi.StringStruct("OriginalFilename", "teleport.exe"),
                        vi.StringStruct("ProductName", "Zero-Cloud LAN Teleport"),
                        vi.StringStruct("ProductVersion", APP_VERSION),
                    ],
                )
            ]
        ),
        vi.VarFileInfo([vi.VarStruct("Translation", [0x0409, 1200])]),
    ],
)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=third_party.DIST_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX hay bị antivirus báo nhầm
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=third_party.DIST_NAME,
)

third_party.write_outputs(
    license_audit,
    dist_dir=Path(DISTPATH) / third_party.DIST_NAME,
    repo_dir=REPO_DIR,
)
