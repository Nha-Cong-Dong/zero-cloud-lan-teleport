"""Logo của Teleport: SVG của dự án, nhúng thẳng trong mã, vẽ thành pixmap đủ màu.

Logo (con mèo vô cực, mũi tên gửi đi, sóng mạng, nền vuông đen bo góc) do nhóm dự án vẽ, cùng giấy
phép GPL-3.0-or-later với mã. Nó nằm trong mã chứ không trong `ui/assets/`, vì thư mục đó chỉ dành
cho tài sản bên thứ ba có nguồn ghim (`packaging/pyinstaller/third_party.py`, `SOURCE_ASSETS`).

`pixmap(size)` vẽ SVG vào pixmap trong suốt cỡ `size` px qua `icons.render_svg` (module duy nhất import
`QtSvg`), không tô lại màu. Dùng cho icon cửa sổ và khay (`tray.make_icon`), logo ở đầu cột / màn Bắt
đầu, và `packaging/pyinstaller/make_icon.py` sinh `teleport.ico` từ đúng hàm này. Đổi `SVG` thì chạy
lại `make_icon.py` rồi commit file .ico mới. Chỉ dùng trên thread Qt.
"""

from __future__ import annotations

from PySide6.QtGui import QPixmap

from teleport.ui import icons

SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?><svg id="Layer_1" xmlns="http://www.w3.org/2000/svg" v'
    b'iewBox="0 0 1024 1024"><defs><style>.cls-1{fill:#fdaab3;}.cls-2{fill:#f5ece0;}.cls-3{fill:#c'
    b"ea3f3;}.cls-4{fill:#feaab5;}.cls-5{fill:#f6efdf;}.cls-6{fill:#85edca;}</style></defs><path d"
    b'="M167.23,0h688.6s168.18,9.14,168.18,182.24v670.43s-9.99,171.33-182.58,171.33H163.82S0,1001.'
    b'94,0,844.16V168.78S3.92,19.43,167.23,0Z"/><path class="cls-2" d="M503.47,446.44s-76.6-123.57'
    b"-188.6-103.83l-52.6-54.13s-28.05-28.3-41.56,8.35c-.19.52-.38,1.06-.57,1.6-13.28,39.06-39.06,"
    b"140.26-39.06,140.26,0,0-9.4,18.3-14.68,41.11,0,0-3.83,16-4.85,42.81,0,0,1.43,80.83,55.55,124"
    b".06,0,0,20.04,20.09,46.72,33.87,0,0,22.77,12.26,50.51,14.47,0,0,32.28,4.28,39.3,1.4,0,0,46.3"
    b".57,87.57-38.15,0,0,38.89-34.89,40.26-38.64l16.96-20.3,16-21.15,48.6-68.04,44.94-55.83s60.47"
    b"-78.98,148.81-12.6c0,0,64.77,69.02,11.66,141.53,0,0-61.79,77.11-135.57,21.19,0,0-38.04-26.6-"
    b"67.06-71.4l-44.81,59.7,30.21,35.96,20.87,21.57s7.49,8.26,40,27.83c0,0,35.91,25.7,90.72,18.55"
    b",0,0,94.04-5.06,136.26-90.17,0,0,34.47-56.43,20.68-112.6,0,0-6.55-45.15-16.43-53.66l-39.23-1"
    b"42.89s-10.89-41.87-45.96-5.79l-49.36,50.38s-59.74-10.3-114.38,30.55l-25.62,19.96-21.83,22.64"
    b"-107.96,142.51-27.02,29.66s-45.11,55.02-117.96,26.77c0,0-66.47-33.06-58.3-102.02,0,0-.68-72."
    b'19,81.36-95.68,0,0,46.98-9.53,80.68,20.43,0,0,67.74,68.09,60.94,70.47l44.81-60.77Z"/><path c'
    b'lass="cls-4" d="M824.09,206.5s59.32,30.26,56.77,97.4c0,0-8.85,13.36-15.4-4.26,0,0-.17-49.36-'
    b'41.36-73.87,0,0-17.53-14.64-57.7-16.68,0,0-11.91-7.83,0-16.17,0,0,35.06-1.28,57.7,13.57Z"/><'
    b'path class="cls-5" d="M824.68,247.09s26.38,24.68,24.51,53.79c0,0-7.83,18.72-15.83-1.53,0,0-2'
    b'.21-55.15-60.09-55.83,0,0-13.62-3.4-4.26-13.96,0,0,21.11-10.38,55.66,17.53Z"/><path class="c'
    b'ls-1" d="M426.55,407.81s-75.23-71.49-172.94-6.47c0,0-114.38,85.19-26.55,212.6,0,0,2.04,4.85,'
    b"11.06,5.7,0,0,5.45-2.55-2.04-11.74s-70.47-100.51,9.02-180.94c0,0,79.49-88.94,174.47-10.3,0,0"
    b',5.53,2.98,10.3,1.45,0,0,2.04-8.68-3.32-10.3Z"/><ellipse class="cls-6" cx="451.62" cy="438.7'
    b'9" rx="12.98" ry="12.68"/><ellipse class="cls-6" cx="267.92" cy="641.85" rx="12.98" ry="12.6'
    b'8"/><path class="cls-3" d="M506.6,520.45s-63.83,92.94-96.51,111.32c0,0-53.28,40-106.38,20.26'
    b",0,0-13.28,4.77,0,12.26,0,0,66.89,16.68,113.53-23.83,0,0,67.06-60.26,97.36-111.32l15.96,14.0"
    b'2,7.79-54.96-50.55,22.47,18.81,9.79Z"/><polygon class="cls-2" points="841.94 435.28 845.21 4'
    b'42.37 841.94 443.63 841.94 435.28"/></svg>'
)


def pixmap(size: int, dpr: float = 1.0) -> QPixmap:
    """Logo cỡ `size` px logic cho màn hình tỉ lệ `dpr`; SVG hỏng → pixmap rỗng (ghi WARNING)."""
    return icons.render_svg(SVG, size, dpr)
