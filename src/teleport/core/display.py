"""Hiện tên thiết bị do máy kia tự đặt: thay ký tự trống, đo độ rộng theo cột, rút gọn.

Luật ở docs/mvp.md mục 7.2. Tên trong `hello` chưa được xác thực (docs/protocol.md mục 3.3):
kẻ đứng giữa có thể đặt tên trông như một mã ghép đôi, hoặc chèn ký tự trông như khoảng trắng
để đẩy chữ ra xa. `display_name` làm những ký tự đó lộ ra (thành `·`) và giữ tên trong
`DISPLAY_NAME_WIDTH` cột. Ngoặc “…” và vị trí của tên (luôn sau mã) do người gọi lo.

Độ rộng chỉ là ước lượng cho terminal và giao diện: `east_asian_width` W/F = 2 cột; dấu kết
hợp (Mn, Me) và ZWJ (U+200D) = 0 cột, kể cả khi dấu đó cũng thuộc loại rộng; còn lại 1 cột.
"""

from __future__ import annotations

import unicodedata

DISPLAY_NAME_WIDTH = 32  # mvp mục 7.2: tên dài hơn bị cắt, tổng cả `…` vẫn ≤ 32 cột
BLANK_MARK = "·"  # ·

_ELLIPSIS = "…"  # … (1 cột)
_ZWJ = "\u200d"  # ZWJ, viết bằng escape để mã nguồn không chứa ký tự ẩn
# Ngoài khoảng trắng Unicode (loại Zs): các ký tự hiện ra trống trơn dù không phải khoảng trắng.
_EXTRA_BLANKS = frozenset("\u2800\u3164\u115f\u1160\uffa0")  # chữ nổi trống, các ký tự lấp chỗ Hangul


def _is_blank(ch: str) -> bool:
    """Ký tự trông như khoảng trắng mà không phải dấu cách thường U+0020."""
    return ch in _EXTRA_BLANKS or (ch != " " and unicodedata.category(ch) == "Zs")


def _width(ch: str) -> int:
    """Số cột của một ký tự (xem docstring của module)."""
    if ch == _ZWJ or unicodedata.category(ch) in ("Mn", "Me"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_name(name: str, *, max_width: int = DISPLAY_NAME_WIDTH) -> str:
    """Tên để hiện cho người dùng: ký tự trống thành `·`, dài quá `max_width` cột thì cắt.

    Mọi ký tự loại Zs trừ U+0020, cùng U+2800, U+3164, U+115F, U+1160, U+FFA0, thành
    `BLANK_MARK` (1 cột); độ rộng đo sau bước này. Vượt `max_width` thì giữ phần đầu dài
    nhất sao cho phần đó + `…` ≤ `max_width` cột, bỏ ZWJ thừa ở cuối phần giữ lại.
    Không thêm ngoặc. `name` không phải chuỗi → TypeError; `max_width < 2` → ValueError.
    """
    if not isinstance(name, str):
        raise TypeError("name phải là chuỗi")
    if max_width < 2:
        raise ValueError("max_width phải ≥ 2")
    shown = "".join(BLANK_MARK if _is_blank(ch) else ch for ch in name)
    widths = [_width(ch) for ch in shown]
    if sum(widths) <= max_width:
        return shown
    budget = max_width - 1  # chừa 1 cột cho `…`
    used = 0
    end = 0
    for width in widths:
        if used + width > budget:
            break
        used += width
        end += 1
    return shown[:end].rstrip(_ZWJ) + _ELLIPSIS
