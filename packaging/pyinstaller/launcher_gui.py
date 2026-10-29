"""Điểm vào của bản GUI đóng gói bằng PyInstaller: chạy `teleport.ui.app:main`.

Như `launcher.py` của bản CLI: PyInstaller cần một file script làm điểm vào, file này chỉ gọi đúng
điểm vào `teleport-gui` trong `[project.gui-scripts]` của `pyproject.toml`, không thêm logic.
"""

import sys

from teleport.ui.app import main

if __name__ == "__main__":
    sys.exit(main())
