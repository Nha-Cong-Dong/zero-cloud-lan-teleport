#!/usr/bin/env bash
# Script đóng gói ZCL Teleport dạng AppImage trên Linux.
# Yêu cầu: Python 3.12+, pip, virtualenv, và appimagetool.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BUILD_DIR="${REPO_DIR}/build/appimage"
APP_DIR="${BUILD_DIR}/AppDir"

echo "[*] Gốc dự án: ${REPO_DIR}"
echo "[*] Thư mục dựng: ${BUILD_DIR}"

rm -rf "${BUILD_DIR}"
mkdir -p "${APP_DIR}/usr/bin"
mkdir -p "${APP_DIR}/usr/lib"
mkdir -p "${APP_DIR}/usr/share/applications"
mkdir -p "${APP_DIR}/usr/share/icons/hicolor/scalable/apps"
mkdir -p "${APP_DIR}/usr/share/metainfo"

# 1. Cài đặt và build PyInstaller bản onedir nếu chưa có
echo "[*] Đang chuẩn bị bản chạy từ PyInstaller..."
cd "${REPO_DIR}"
python -m pip install -e ".[gui]"

# Tạo bản build tạm thời vào AppDir/usr/lib/teleport
pyinstaller --noconfirm --clean \
    --name "teleport-gui" \
    --onedir \
    --distpath "${APP_DIR}/usr/lib" \
    --workpath "${BUILD_DIR}/pyi-work" \
    packaging/pyinstaller/launcher_gui.py

# Liên kết file chạy vào usr/bin
ln -sf "../lib/teleport-gui/teleport-gui" "${APP_DIR}/usr/bin/teleport-gui"

# 2. Chép tài nguyên XDG
echo "[*] Đang chép tệp desktop, metainfo và biểu tượng..."
cp "${SCRIPT_DIR}/io.github.nhacongdong.teleport.desktop" "${APP_DIR}/usr/share/applications/"
cp "${SCRIPT_DIR}/io.github.nhacongdong.teleport.metainfo.xml" "${APP_DIR}/usr/share/metainfo/"
cp "${SCRIPT_DIR}/io.github.nhacongdong.teleport.desktop" "${APP_DIR}/"

# Tạo icon SVG từ logo
python -c '
from teleport.ui import logo
with open("'${APP_DIR}'/usr/share/icons/hicolor/scalable/apps/teleport.svg", "wb") as f:
    f.write(logo.SVG)
'
cp "${APP_DIR}/usr/share/icons/hicolor/scalable/apps/teleport.svg" "${APP_DIR}/teleport.svg"

# 3. Tạo AppRun
cat << 'EOF' > "${APP_DIR}/AppRun"
#!/bin/sh
HERE="$(dirname "$(readlink -f "${0}")")"
export PATH="${HERE}/usr/bin:${PATH}"
export LD_LIBRARY_PATH="${HERE}/usr/lib:${HERE}/usr/lib/teleport-gui/_internal:${LD_LIBRARY_PATH}"
export XDG_DATA_DIRS="${HERE}/usr/share:${XDG_DATA_DIRS}"
exec "${HERE}/usr/bin/teleport-gui" "$@"
EOF
chmod +x "${APP_DIR}/AppRun"

# 4. Tải appimagetool nếu chưa có và đóng gói
APPIMAGETOOL="${BUILD_DIR}/appimagetool"
if [ ! -f "${APPIMAGETOOL}" ]; then
    echo "[*] Đang tải appimagetool..."
    curl -fsSL -o "${APPIMAGETOOL}" "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x "${APPIMAGETOOL}"
fi

OUTPUT_NAME="${REPO_DIR}/dist/Teleport-x86_64.AppImage"
mkdir -p "${REPO_DIR}/dist"

echo "[*] Đang đóng gói ${OUTPUT_NAME}..."
ARCH=x86_64 "${APPIMAGETOOL}" "${APP_DIR}" "${OUTPUT_NAME}"

echo "[+] Hoàn tất! File AppImage đã được tạo tại: ${OUTPUT_NAME}"
