"""Ghi file nguyên tử; tiện ích dùng chung như `paths` và `lock`.

Dùng cho các file nhỏ trong thư mục cấu hình (`trusted.json`, `settings.json`): người
đọc không bao giờ thấy file ghi dở. Module không import tầng nào, nên lỗi bay ra dưới
dạng `OSError` nguyên trạng; bên gọi tự bọc thành lỗi của mình (ví dụ `TrustStoreError`).
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

# os.replace có thể báo PermissionError nếu tiến trình khác (trình diệt virus, người
# đọc file) đang mở file đích, hay gặp nhất trên Windows. Áp dụng cho mọi HĐH: tối
# đa 5 lần thử, cách nhau 50 ms, rồi mới bỏ cuộc.
REPLACE_ATTEMPTS = 5  # số lần thử os.replace khi gặp PermissionError
REPLACE_DELAY = 0.05  # giây giữa hai lần thử


def write_atomic(path: Path, content: bytes) -> None:
    """Ghi nguyên tử: `<path>.tmp` → flush + fsync → os.replace → fsync thư mục cha (POSIX).

    Tạo thư mục cha nếu chưa có. Quyền 0o600 ngay lúc tạo, fchmod lại trên POSIX (file .tmp
    còn sót từ lần trước có thể mang quyền khác). os.replace gặp PermissionError thì thử lại tối
    đa REPLACE_ATTEMPTS lần, cách nhau REPLACE_DELAY. Lỗi thì xóa .tmp (bỏ qua lỗi khi xóa) rồi
    raise OSError nguyên trạng. File đích hoặc còn nguyên bản cũ, hoặc đã là bản mới; không bao
    giờ ghi dở.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 0o600: chỉ chủ sở hữu đọc/ghi (có tác dụng trên POSIX).
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            if sys.platform != "win32":
                os.fchmod(f.fileno(), 0o600)  # .tmp còn sót từ lần trước có thể mang quyền khác
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        attempts = max(1, REPLACE_ATTEMPTS)
        for attempt in range(attempts):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt >= attempts - 1:
                    raise
                time.sleep(REPLACE_DELAY)
        _fsync_dir(path.parent)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def _fsync_dir(directory: Path) -> None:
    """POSIX: fsync thư mục để việc đổi tên đã ghi xuống đĩa. Windows không hỗ trợ; lỗi bỏ qua."""
    if sys.platform == "win32":
        return
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
