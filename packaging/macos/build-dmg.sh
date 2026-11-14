#!/usr/bin/env bash
# Script đóng gói ZCL Teleport dạng file .dmg trên macOS.
# Yêu cầu: macOS, Python 3.12+, PySide6, và công cụ hdiutil (có sẵn trên macOS).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BUILD_DIR="${REPO_DIR}/build/macos"
DIST_DIR="${REPO_DIR}/dist"
APP_NAME="Teleport.app"
APP_BUNDLE="${BUILD_DIR}/${APP_NAME}"

echo "[*] Gốc dự án: ${REPO_DIR}"
echo "[*] Thư mục dựng: ${BUILD_DIR}"

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"
mkdir -p "${DIST_DIR}"

# 1. Lấy phiên bản
cd "${REPO_DIR}"
APP_VERSION="$(python -c 'import teleport; print(teleport.__version__)')"
echo "[*] Phiên bản: ${APP_VERSION}"

# 2. Build bằng PyInstaller
echo "[*] Đang đóng gói ứng dụng với PyInstaller..."
python -m pip install -e ".[gui]"
pyinstaller --noconfirm --clean \
    --name "Teleport" \
    --windowed \
    --onedir \
    --collect-data "teleport" \
    --collect-submodules "zeroconf" \
    --distpath "${BUILD_DIR}/pyi-dist" \
    --workpath "${BUILD_DIR}/pyi-work" \
    packaging/pyinstaller/launcher_gui.py

# 3. Tạo cấu trúc .app bundle
echo "[*] Đang tạo cấu trúc ${APP_NAME}..."
mkdir -p "${APP_BUNDLE}/Contents/MacOS"
mkdir -p "${APP_BUNDLE}/Contents/Resources"

# Chép các file thực thi và thư viện
cp -R "${BUILD_DIR}/pyi-dist/Teleport/"* "${APP_BUNDLE}/Contents/MacOS/"

# Sinh file Info.plist
sed "s/@APP_VERSION@/${APP_VERSION}/g" "${SCRIPT_DIR}/Info.plist.in" > "${APP_BUNDLE}/Contents/Info.plist"

# Chép biểu tượng nếu có
if [ -f "${SCRIPT_DIR}/teleport.icns" ]; then
    cp "${SCRIPT_DIR}/teleport.icns" "${APP_BUNDLE}/Contents/Resources/"
fi

# Chép các file giấy phép bắt buộc
cp "${REPO_DIR}/LICENSE" "${APP_BUNDLE}/Contents/Resources/"
if [ -f "${BUILD_DIR}/pyi-dist/Teleport/THIRD-PARTY-NOTICES.txt" ]; then
    cp "${BUILD_DIR}/pyi-dist/Teleport/THIRD-PARTY-NOTICES.txt" "${APP_BUNDLE}/Contents/Resources/"
fi
if [ -f "${BUILD_DIR}/pyi-dist/Teleport/SOURCE.txt" ]; then
    cp "${BUILD_DIR}/pyi-dist/Teleport/SOURCE.txt" "${APP_BUNDLE}/Contents/Resources/"
fi

# 4. Đóng gói file .dmg bằng hdiutil
DMG_NAME="Teleport-${APP_VERSION}-macOS.dmg"
DMG_PATH="${DIST_DIR}/${DMG_NAME}"
DMG_STAGE="${BUILD_DIR}/dmg_stage"

mkdir -p "${DMG_STAGE}"
cp -R "${APP_BUNDLE}" "${DMG_STAGE}/"
ln -sf /Applications "${DMG_STAGE}/Applications"

echo "[*] Đang nén file DMG bằng hdiutil..."
rm -f "${DMG_PATH}"
hdiutil create -volname "ZCL Teleport" -srcfolder "${DMG_STAGE}" -ov -format UDZO "${DMG_PATH}"

echo "[+] Hoàn tất! File DMG đã được tạo tại: ${DMG_PATH}"
