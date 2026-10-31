"""Dựng `teleport.ico` (icon của `Teleport.exe`) từ đúng hàm vẽ icon của app, `teleport.ui.tray._draw`.

Icon là logo của dự án (SVG nhúng trong `ui/logo.py`, cùng giấy phép GPL-3.0-or-later của dự án);
không dùng ảnh, font hay tài sản nào bên ngoài. Cửa sổ và khay vẽ icon lúc chạy (`tray.make_icon`);
file .ico này chỉ để Windows Explorer, thanh tác vụ và lối tắt hiện đúng icon của exe (tài nguyên
RT_ICON trong exe, không phải file trong `_internal/`).

`teleport.ico` đã sinh được commit cạnh script này và `teleport-gui.spec` chỉ đọc file đó: build
(trên CI hay tại máy) không phải chạy Qt để vẽ, nên không thêm một chỗ có thể hỏng. Sửa logo
(`ui/logo.py`) hay nâng PySide6 thì chạy lại script này rồi commit file mới; `--check` báo
file đã cũ (mã thoát 1) mà không ghi gì. Chạy từ gốc repo, trong môi trường đã cài extra `gui`:

    python packaging/pyinstaller/make_icon.py           # ghi packaging/pyinstaller/teleport.ico
    python packaging/pyinstaller/make_icon.py --check   # chỉ so, không ghi

Định dạng ICO viết tay bằng `struct` (không thêm dependency): ICONDIR, mỗi cỡ một ICONDIRENTRY,
ảnh là PNG 32 bit RGBA (Windows Vista trở lên đọc được PNG trong ICO ở mọi cỡ).
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

ICON_PATH = Path(__file__).resolve().parent / "teleport.ico"
# Cỡ Windows dùng cho exe: Explorer (16–256), thanh tác vụ, Alt+Tab, theo DPI.
ICO_SIZES = (16, 24, 32, 48, 64, 256)


def _png_images() -> list[tuple[int, bytes]]:
    """Vẽ từng cỡ bằng `tray._draw` (Qt offscreen) và trả (cỡ, PNG)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QGuiApplication

    from teleport.ui import tray

    app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    images = []
    for size in ICO_SIZES:
        pixmap = tray._draw(size)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not pixmap.save(buffer, "PNG"):
            raise RuntimeError(f"không ghi được PNG cỡ {size}")
        buffer.close()
        images.append((size, bytes(data.data())))
    del app
    return images


def build_ico(images: list[tuple[int, bytes]]) -> bytes:
    """Ghép các ảnh PNG vuông thành một file ICO."""
    header = struct.pack("<HHH", 0, 1, len(images))  # reserved, type 1 = icon, số ảnh
    offset = len(header) + 16 * len(images)
    entries, blobs = [], []
    for size, png in images:
        if not 1 <= size <= 256:
            raise ValueError(f"cỡ icon {size} ngoài 1..256")
        dim = 0 if size == 256 else size  # 0 nghĩa là 256
        # rộng, cao, số màu bảng (0), reserved, planes, bit mỗi điểm ảnh, số byte, vị trí
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset))
        blobs.append(png)
        offset += len(png)
    return header + b"".join(entries) + b"".join(blobs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dựng teleport.ico từ teleport.ui.tray._draw.")
    parser.add_argument("--check", action="store_true", help="chỉ so với file đã có, không ghi")
    args = parser.parse_args(argv)
    # Console Windows có thể là cp1252: không có PYTHONUTF8 thì in tiếng Việt sẽ ném UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    data = build_ico(_png_images())
    if args.check:
        if ICON_PATH.is_file() and ICON_PATH.read_bytes() == data:
            print(f"{ICON_PATH.name} khớp với tray._draw")
            return 0
        print(f"{ICON_PATH.name} đã cũ hoặc chưa có: chạy lại make_icon.py rồi commit", file=sys.stderr)
        return 1
    ICON_PATH.write_bytes(data)
    print(f"Đã ghi {ICON_PATH} ({len(data)} byte, cỡ {', '.join(map(str, ICO_SIZES))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
