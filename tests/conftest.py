"""Fixture chung cho mọi test: nới giới hạn tốc độ bắt tay (`docs/protocol.md` mục 2.3).

`teleport.core.admission` giới hạn **tốc độ** bắt tay theo IP (burst 2, 1/s cho IP lạ). Mọi test chạy
trên 127.0.0.1, nên một test mở vài kết nối liên tiếp sẽ vấp giới hạn đó dù không thử nó. Fixture tự
động dưới đây chỉ nới **tốc độ và burst** (cả pool chung lẫn pool ưu tiên); các giới hạn số kết nối
đồng thời (`MAX_OPENING_*`, `PRIORITY_OPENING_*`, `MAX_ANONYMOUS_*`) giữ nguyên mặc định, test nào
cần thì tự đặt lại.

Test cần mặc định thật (test giới hạn tốc độ, test tích hợp chạy “như sản phẩm”) gắn marker
`real_admission_limits`. Test chạy node bằng tiến trình con không nhận monkeypatch: chúng chạy với
mặc định thật (đã kiểm: mỗi tiến trình `listen` trong `tests/test_cli.py` chỉ nhận một kết nối).
"""

from __future__ import annotations

import pytest

from teleport.core import admission

REAL_LIMITS_MARKER = "real_admission_limits"

# Hằng tốc độ/burst của `teleport.core.admission` (`docs/protocol.md` mục 2.3).
RATE_CONSTANTS = (
    "HANDSHAKE_RATE_PER_IP",
    "HANDSHAKE_BURST_PER_IP",
    "HANDSHAKE_RATE_TOTAL",
    "HANDSHAKE_BURST_TOTAL",
    "PRIORITY_RATE_PER_IP",
    "PRIORITY_BURST_PER_IP",
    "PRIORITY_RATE_TOTAL",
    "PRIORITY_BURST_TOTAL",
)
RELAXED_RATE = 1_000_000.0


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{REAL_LIMITS_MARKER}: không nới tốc độ/burst bắt tay của teleport.core.admission (mặc định thật)",
    )


@pytest.fixture(autouse=True)
def relaxed_admission_rates(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nới tốc độ và burst bắt tay cho test không thử chúng; tắt bằng `@pytest.mark.real_admission_limits`."""
    if request.node.get_closest_marker(REAL_LIMITS_MARKER) is not None:
        return
    # Không `raising=False`: đổi tên hay xóa một hằng thì mọi test đỏ ngay, thay vì lặng lẽ chạy với
    # tốc độ thật.
    for name in RATE_CONSTANTS:
        monkeypatch.setattr(admission, name, RELAXED_RATE)
