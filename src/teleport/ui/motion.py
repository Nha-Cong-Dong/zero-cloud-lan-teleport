"""Chuyển động của giao diện: có được chạy hoạt ảnh trang trí hay không (mvp N12, WCAG 2.2.2).

`animations_enabled()` đọc cài đặt hiệu ứng của hệ điều hành **mỗi lần gọi** (người dùng đổi lúc app
đang chạy thì lần chạy hoạt ảnh sau theo ngay), không nhớ kết quả:

- Windows: `SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION)` qua `ctypes` (“Hiện hiệu ứng động
  trong Windows” của Cài đặt › Trợ năng › Hiệu ứng hình ảnh). Tắt → False. Gọi lỗi → True (hoạt ảnh
  của Teleport đều ngắn, tự dừng, chỉ để trang trí; thiếu cài đặt thì không đáng chặn).
- macOS, Linux: luôn True. Giới hạn đã biết: chưa đọc “Giảm chuyển động” của macOS
  (`NSWorkspace.accessibilityDisplayShouldReduceMotion`) hay `gtk-enable-animations` của GNOME (để
  E1). Mọi hoạt ảnh vì vậy phải tự dừng sau ≤ 5 s và dừng khi cửa sổ ẩn hay thu nhỏ.

Bên dùng gọi `motion.animations_enabled()` qua tên module, lúc cần (test vá được). Seam test của
Windows: `_query_client_area_animation()` (cấp module, đọc lúc gọi).
"""

from __future__ import annotations

import sys

SPI_GETCLIENTAREAANIMATION = 0x1042


def _query_client_area_animation() -> bool:
    """Giá trị `SPI_GETCLIENTAREAANIMATION` của Windows; gọi lỗi → OSError. Ngoài Windows → True."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        value = wintypes.BOOL(True)
        if not ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(value), 0
        ):
            raise OSError("SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION) lỗi")
        return bool(value.value)
    return True


def animations_enabled() -> bool:
    """True nếu được chạy hoạt ảnh trang trí (xem docstring module); không bao giờ ném lỗi."""
    if sys.platform != "win32":
        return True
    try:
        return bool(_query_client_area_animation())
    except Exception:
        return True
