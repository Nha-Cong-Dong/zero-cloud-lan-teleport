"""Xuất log thành một file zip để người dùng gửi kèm khi báo lỗi (mvp F10, "Xuất log").

`export_logs` đồng bộ và không đụng node: giao diện chạy nó ngoài loop của node (thread pool) và
tự đưa danh sách file log (`app` không biết hằng `LOG_*` của `ui`). Zip gồm các file log còn tồn
tại, theo đúng thứ tự đưa vào (tên mục là tên file), rồi `info.txt`: phiên bản app, phiên bản giao
thức, hệ điều hành, phiên bản Python. Không có tên máy, `device_id`, `settings.json` hay
`trusted.json`. Mỗi file chỉ đọc tối đa `MAX_EXPORT_FILE` byte **cuối** (lớn hơn thì cắt phần
đầu, ghi chú trong `info.txt`); đọc nhanh rồi đóng ngay, để Windows còn đổi tên được file khi
handler log xoay vòng.

Riêng tư (mvp N9): bản thân các dòng log có thể chứa đường dẫn (có tên người dùng của hệ điều
hành), địa chỉ IP và 8 ký tự đầu của mã thiết bị; giao diện nên nói rõ điều đó trước khi người
dùng gửi file đi.
"""

from __future__ import annotations

import io
import platform
import zipfile
from collections.abc import Sequence
from pathlib import Path

import teleport
from teleport import atomic
from teleport.app.node import device_os
from teleport.core.messages import PROTOCOL_VERSION

MAX_EXPORT_FILE = 8 * 1024 * 1024  # byte đọc tối đa mỗi file log (phần cuối)
INFO_NAME = "info.txt"


def export_logs(log_files: Sequence[Path], dest: Path) -> Path:
    """Ghi nguyên tử (`atomic.write_atomic`) zip các file `log_files` còn tồn tại cùng `info.txt` vào `dest`.

    File không còn (`FileNotFoundError`) bị bỏ qua. `dest` không tuyệt đối → ValueError (không ghi
    gì); lỗi đọc/ghi khác (`OSError`) bay ra, `dest` cũ (nếu có) còn nguyên. Trả `dest`.
    """
    dest = Path(dest)
    if not dest.is_absolute():
        raise ValueError("dest phải là đường dẫn tuyệt đối")
    notes: list[str] = []
    used: set[str] = set()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, source in enumerate(log_files):
            path = Path(source)
            try:
                data, size = _read_tail(path)
            except FileNotFoundError:
                continue
            name = path.name
            if name in used or name == INFO_NAME:
                name = f"{index}-{name}"  # hai file cùng tên (khác thư mục): không ghi đè nhau trong zip
            used.add(name)
            archive.writestr(name, data)
            if size > len(data):
                notes.append(f"{name}: dài {size} byte, chỉ giữ {len(data)} byte cuối")
        archive.writestr(INFO_NAME, _info_text(notes))
    atomic.write_atomic(dest, buffer.getvalue())
    return dest


def _read_tail(path: Path) -> tuple[bytes, int]:
    """(nội dung, cỡ file lúc đọc): tối đa `MAX_EXPORT_FILE` byte cuối của `path`."""
    with path.open("rb") as f:
        size = f.seek(0, io.SEEK_END)
        f.seek(max(0, size - MAX_EXPORT_FILE))
        return f.read(MAX_EXPORT_FILE), size


def _info_text(notes: list[str]) -> bytes:
    lines = [
        f"Teleport {teleport.__version__}",
        f"Giao thức: {PROTOCOL_VERSION}",
        f"Hệ điều hành: {device_os()}",
        f"Python: {platform.python_version()}",
    ]
    if notes:
        lines.append("File bị cắt phần đầu:")
        lines.extend(f"- {note}" for note in notes)
    return ("\n".join(lines) + "\n").encode("utf-8")
