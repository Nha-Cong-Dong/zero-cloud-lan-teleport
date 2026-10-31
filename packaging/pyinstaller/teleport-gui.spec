# -*- mode: python ; coding: utf-8 -*-
# Spec PyInstaller cho bản GUI Teleport (build thử dạng zip, Windows). Lệnh build (chạy từ gốc repo) ở
# mục "Build thử bản GUI (Windows)" của docs/CONTRIBUTING.md; workflow: .github/workflows/build-gui.yml.
# Kết quả: dist/teleport-gui/Teleport.exe (windowed), thư viện trong dist/teleport-gui/_internal/, cùng
# LICENSE, THIRD-PARTY-NOTICES.txt, SOURCE.txt (third_party.write_outputs) và DOC-TRUOC.txt (hướng dẫn
# chạy cho người dùng, chép từ packaging/pyinstaller/ ở cuối file này) cạnh Teleport.exe. Thư mục tên
# teleport-gui, không phải Teleport: Windows không phân biệt hoa thường nên "Teleport" trùng thư mục
# dist/teleport của bản CLI.
#
# Icon của Teleport.exe: teleport.ico cạnh file này, sinh bằng make_icon.py từ đúng hàm vẽ icon của app
# (teleport.ui.tray._draw, tức logo SVG nhúng trong ui/logo.py), cùng giấy phép GPL-3.0-or-later của
# dự án, không dùng tài sản ngoài. Nó vào exe dưới dạng tài nguyên Windows (RT_ICON), không phải file trong _internal/.
#
# Giấy phép (ke_hoach mục 1, Giấy phép; Giai đoạn 6 ở mục 5.2): như teleport-cli.spec.
# - Chỉ --onedir (COLLECT): Qt/PySide6 (LGPL-3.0) và zeroconf (LGPL-2.1) là file riêng trong _internal/
#   mà người dùng thay được; zeroconf, PySide6, shiboken6 đóng dạng file .py/.pyd riêng
#   (module_collection_mode "py"), không gộp vào kho PYZ trong Teleport.exe.
# - Chỉ đóng phần Qt mà ui dùng (QtCore, QtGui, QtWidgets, QtNetwork cho QLocalServer, QtSvg cho
#   QSvgRenderer của ui/icons.py), plugin nền tảng qwindows và style; bỏ QtQml/QtQuick/QtPdf/
#   QtSvgWidgets…, opengl32sw.dll, bản dịch của Qt (ui không nạp bản dịch nào của Qt) và plugin không
#   dùng (QT_UNUSED dưới đây), trước khi kiểm giấy phép.
# - Tài sản của ui (src/teleport/ui/assets/: icon Fluent UI System Icons, MIT; font Bricolage Grotesque,
#   OFL-1.1) đóng nguyên dạng làm file dữ liệu riêng, kèm LICENSE/OFL.txt của chúng; third_party.check
#   xếp chúng theo SOURCE_ASSETS và dừng build nếu có file lạ trong assets/.
# - third_party.check kiểm giấy phép mọi file thật sự đóng vào (kể cả thành phần lồng trong Qt,
#   QT_EMBEDDED) trước EXE/COLLECT; hỏng thì xóa dist/teleport-gui cũ rồi dừng.
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

DIST_NAME = third_party.GUI_DIST_NAME
EXE_NAME = "Teleport"

# Phiên bản chỉ lấy từ teleport.__version__ (đọc thẳng từ src/, không phụ thuộc gói đã cài).
_init_spec = importlib.util.spec_from_file_location("_teleport_version", SRC_DIR / "teleport" / "__init__.py")
_init_mod = importlib.util.module_from_spec(_init_spec)
_init_spec.loader.exec_module(_init_mod)
APP_VERSION = _init_mod.__version__
_release = (tuple(Version(APP_VERSION).release) + (0, 0, 0, 0))[:4]

# Module Cython của zeroconf import lẫn nhau từ mã máy nên PyInstaller không dò được (như bản CLI).
hiddenimports = collect_submodules("zeroconf")

# Bản dịch của giao diện (teleport/ui/i18n/*.qm, đọc qua importlib.resources). Chưa có file nào thì
# không đóng gì; có thì đóng đúng chỗ để i18n tìm thấy.
_QM_DIR = SRC_DIR / "teleport" / "ui" / "i18n"
datas = [(str(qm), "teleport/ui/i18n") for qm in sorted(_QM_DIR.glob("*.qm"))]

# Tài sản của ui (đọc qua importlib.resources: files("teleport.ui") / "assets" / ...). Lấy MỌI file trong
# assets/ (không lọc theo đuôi), để file lạ lọt vào thư mục đó làm third_party.check dừng build thay vì
# bị bỏ qua lặng lẽ; đích giữ nguyên đường dẫn tương đối với src/.
_ASSETS_DIR = SRC_DIR / "teleport" / "ui" / "assets"
datas += [
    (str(f), f.parent.relative_to(SRC_DIR).as_posix())
    for f in sorted(_ASSETS_DIR.rglob("*"))
    if f.is_file() and "__pycache__" not in f.parts
]

# Module Qt ui không dùng. ui chỉ import QtCore, QtGui, QtWidgets, QtNetwork (QLocalServer) và QtSvg
# (chỉ ui/icons.py, cho QSvgRenderer); dùng thêm module Qt nào thì sửa danh sách này và QT_UNUSED dưới
# đây, rồi rà giấy phép file mới (QT_REVIEWED_FILES trong third_party.py).
_QT_UNUSED_MODULES = [
    f"PySide6.{name}"
    for name in (
        "QtConcurrent",
        "QtDBus",
        "QtDesigner",
        "QtHelp",
        "QtLabsStyleKit",
        "QtOpenGL",
        "QtOpenGLWidgets",
        "QtPdf",
        "QtPdfWidgets",
        "QtPrintSupport",
        "QtQml",
        "QtQmlFeatures",
        "QtQuick",
        "QtQuickControls2",
        "QtQuickTest",
        "QtQuickWidgets",
        "QtSql",
        "QtSvgWidgets",
        "QtTest",
        "QtUiTools",
        "QtXml",
    )
]

a = Analysis(
    [str(SPEC_DIR / "launcher_gui.py")],
    pathex=[str(SRC_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Như bản CLI, cộng các module Qt không dùng.
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
        "pytestqt",
        "hypothesis",
        "mypy",
        "setuptools",
        "pip",
        *_QT_UNUSED_MODULES,
    ],
    # Thư viện LGPL đóng dạng file .py riêng trong _internal/, không gộp vào kho PYZ trong Teleport.exe.
    module_collection_mode={"zeroconf": "py", "PySide6": "py", "shiboken6": "py"},
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# Phần Qt không dùng mà hook của PyInstaller vẫn gom (theo đường dẫn đích, chữ thường, dấu "/").
# Giữ: Qt6Core/Gui/Widgets/Network/Svg, plugins/platforms/qwindows.dll, plugins/styles/. PNG đọc được
# không cần plugin (có sẵn trong QtGui); icon cửa sổ và khay vẽ bằng mã lúc chạy (tray.make_icon), icon
# SVG của ui vẽ thẳng bằng QSvgRenderer (ui/icons.py, không QIcon(đường dẫn .svg)), icon của
# Teleport.exe là tài nguyên trong exe (teleport.ico), nên không cần plugin imageformats/iconengines
# (kể cả qsvg.dll, qsvgicon.dll).
QT_UNUSED = (
    "pyside6/opengl32sw.dll",  # OpenGL phần mềm (Mesa), ui không dùng OpenGL
    "pyside6/translations/*",  # bản dịch của Qt; ui chỉ nạp teleport_*.qm của mình
    "pyside6/plugins/imageformats/*",
    "pyside6/plugins/iconengines/*",
    "pyside6/plugins/generic/*",
    "pyside6/plugins/platforminputcontexts/*",
    "pyside6/plugins/networkinformation/*",
    "pyside6/plugins/tls/*",  # QLocalServer không dùng TLS của Qt
    # OpenSSL mà hook QtNetwork của PyInstaller tìm trên PATH (ví dụ của Git for Windows) cho plugin
    # TLS của Qt; plugin đó đã bỏ. OpenSSL của Python (libssl-3.dll, libcrypto-3.dll) giữ nguyên.
    "libssl-*-x64.dll",
    "libcrypto-*-x64.dll",
    "pyside6/plugins/platforms/qdirect2d.dll",
    "pyside6/plugins/platforms/qminimal.dll",
    "pyside6/plugins/platforms/qoffscreen.dll",
    "pyside6/qt6pdf*.dll",
    "pyside6/qt6qml*.dll",
    "pyside6/qt6quick*.dll",
    "pyside6/qt6svgwidgets*.dll",
    "pyside6/qt6opengl*.dll",
    "pyside6/qt6virtualkeyboard*.dll",
)
a.binaries, _qt_dropped_bin = third_party.drop_files(a.binaries, QT_UNUSED)
a.datas, _qt_dropped_data = third_party.drop_files(a.datas, QT_UNUSED)
third_party.log(f"Bỏ {len(_qt_dropped_bin) + len(_qt_dropped_data)} file Qt không dùng (QT_UNUSED).")

# Universal CRT: như bản CLI (UCRT_FILES trong third_party.py).
a.binaries, _ucrt_dropped = third_party.drop_ucrt(a.binaries)
if _ucrt_dropped:
    third_party.log(f"Bỏ {len(_ucrt_dropped)} file Universal CRT khỏi bản build: {', '.join(_ucrt_dropped)}")

try:
    license_audit = third_party.check(
        [*a.scripts, *a.pure, *a.binaries, *a.datas, *pyz.dependencies],
        repo_dir=REPO_DIR,
        app_version=APP_VERSION,
        edition=third_party.GUI_EDITION,
    )
except Exception:
    third_party.remove_stale_dist(Path(DISTPATH), DIST_NAME)
    raise

version_info = vi.VSVersionInfo(
    ffi=vi.FixedFileInfo(filevers=_release, prodvers=_release),
    kids=[
        vi.StringFileInfo(
            [
                vi.StringTable(
                    "040904B0",
                    [
                        vi.StringStruct("FileDescription", "Teleport - chuyển file trong mạng LAN"),
                        vi.StringStruct("FileVersion", APP_VERSION),
                        vi.StringStruct("InternalName", EXE_NAME),
                        vi.StringStruct("LegalCopyright", "GPL-3.0-or-later"),
                        vi.StringStruct("OriginalFilename", f"{EXE_NAME}.exe"),
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
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX hay bị antivirus báo nhầm
    console=False,  # windowed: không mở cửa sổ dòng lệnh; sys.stdout/stderr là None (ui/app.py)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
    icon=str(SPEC_DIR / "teleport.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=DIST_NAME,
)

third_party.write_outputs(
    license_audit,
    dist_dir=Path(DISTPATH) / DIST_NAME,
    repo_dir=REPO_DIR,
)

# Hướng dẫn cho người dùng cuối, cạnh Teleport.exe. Nguồn trong repo là UTF-8; read_text đổi mọi kiểu
# xuống dòng (git có thể checkout CRLF) thành "\n". Ghi ra UTF-8 có BOM, xuống dòng CRLF, để Notepad của
# mọi bản Windows 10 đọc đúng tiếng Việt. Nội dung lấy theo paths.py và ui/app.py (thư mục nhận, log):
# đổi những chỗ đó thì sửa cả DOC-TRUOC.txt.
_readme_text = (SPEC_DIR / "DOC-TRUOC.txt").read_text(encoding="utf-8-sig")
(Path(DISTPATH) / DIST_NAME / "DOC-TRUOC.txt").write_text(_readme_text, encoding="utf-8-sig", newline="\r\n")
