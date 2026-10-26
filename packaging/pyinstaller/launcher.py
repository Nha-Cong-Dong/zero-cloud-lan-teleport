"""Điểm vào của bản CLI đóng gói bằng PyInstaller: chạy `teleport.cli:main`.

PyInstaller cần một file script làm điểm vào, không nhận trực tiếp `module:hàm` như
`[project.scripts]` trong `pyproject.toml`. File này chỉ gọi đúng điểm vào đó, không thêm logic.
"""

import sys

from teleport.cli import main

if __name__ == "__main__":
    sys.exit(main())
