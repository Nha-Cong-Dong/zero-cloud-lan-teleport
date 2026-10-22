"""Cài đặt của node: `<config_dir>/settings.json`.

Định dạng file ở docs/architecture.md mục 4. Hiện chỉ có `port` (cổng dự phòng khi
47474 bận); mọi trường khác được giữ nguyên khi ghi lại, để bản cũ không xóa dữ liệu
của bản mới. File hỏng không chặn app: dùng giá trị mặc định, và bản gốc được chép
sang `settings.json.bak` trước khi bị ghi đè.

Không bao giờ ghi nội dung file vào log (mvp N9): chỉ ghi đường dẫn và loại lỗi.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from teleport.atomic import write_atomic

SETTINGS_FILE = "settings.json"
BACKUP_FILE = "settings.json.bak"
SETTINGS_VERSION = 1
MAX_SETTINGS_SIZE = 1024 * 1024  # lớn hơn → coi là hỏng, không đọc vào RAM

log = logging.getLogger(__name__)


def _valid_port(value: object) -> bool:
    # bool là lớp con của int: bị từ chối.
    return type(value) is int and 1 <= value <= 65535


@dataclass(frozen=True, slots=True)
class Settings:
    """Cài đặt đã đọc. `extra` giữ mọi khóa cấp ngoài cùng khác `port` (kể cả `version`)."""

    port: int | None = None  # None = chưa lưu cổng nào
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.port is not None and not _valid_port(self.port):
            raise ValueError("port phải là None hoặc số nguyên 1–65535")
        if not isinstance(self.extra, Mapping):
            raise ValueError("extra phải là mapping")
        extra = dict(self.extra)
        if any(not isinstance(key, str) for key in extra):
            raise ValueError("khóa của extra phải là chuỗi")
        if "port" in extra:
            raise ValueError('extra không được chứa khóa "port"')
        object.__setattr__(self, "extra", extra)

    @classmethod
    def load(cls, config_dir: Path) -> Settings:
        """Đọc `settings.json`; không bao giờ raise vì nội dung file.

        Chưa có file → mặc định (không tạo file). Lỗi đọc hay file hỏng → log WARNING,
        dùng mặc định. Trường sai (`port`, `version`) → log WARNING, bỏ trường đó.
        """
        path = config_dir / SETTINGS_FILE
        try:
            raw = _read_limited(path)
        except FileNotFoundError:
            return cls()
        except OSError as e:
            log.warning("không đọc được %s (%s), dùng cài đặt mặc định", path, type(e).__name__)
            return cls()
        settings, problems = _parse(raw)
        for problem in problems:
            log.warning("%s: %s", path, problem)
        return settings

    def save(self, config_dir: Path) -> None:
        """Ghi `settings.json` (nguyên tử).

        File đang có mà hỏng (theo đúng luật của `load`) thì chép nguyên byte sang
        `settings.json.bak` trước (ghi đè `.bak` cũ). Lỗi đọc/ghi (`OSError`) bay ra; ghi
        `.bak` thất bại thì không ghi file chính. `extra` không ghi ra JSON được → TypeError.
        """
        path = config_dir / SETTINGS_FILE
        data: dict[str, Any] = {"version": self.extra.get("version", SETTINGS_VERSION)}
        data.update((k, v) for k, v in self.extra.items() if k != "version")
        if self.port is not None:
            data["port"] = self.port
        try:
            text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            content = text.encode("utf-8")
        except UnicodeEncodeError:
            # Chuỗi chứa surrogate lẻ (JSON cho phép "\ud800"): viết dạng escape ASCII.
            content = (json.dumps(data, ensure_ascii=True, indent=2) + "\n").encode("ascii")

        try:
            old = path.read_bytes()
        except FileNotFoundError:
            old = None
        if old is not None and _parse(old)[1]:
            write_atomic(config_dir / BACKUP_FILE, old)
        write_atomic(path, content)


def _read_limited(path: Path) -> bytes:
    """Đọc tối đa MAX_SETTINGS_SIZE + 1 byte (đủ để biết file quá lớn mà không đọc hết)."""
    with path.open("rb") as f:
        return f.read(MAX_SETTINGS_SIZE + 1)


def _parse(raw: bytes) -> tuple[Settings, list[str]]:
    """Phân tích nội dung file; trả cài đặt và danh sách vấn đề (rỗng = file không hỏng).

    Thông điệp vấn đề chỉ nói loại lỗi, không chứa nội dung file.
    """
    if len(raw) > MAX_SETTINGS_SIZE:
        return Settings(), [f"file lớn hơn {MAX_SETTINGS_SIZE} byte, dùng cài đặt mặc định"]
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError) as e:  # gồm UnicodeDecodeError, JSONDecodeError
        return Settings(), [f"không phải JSON UTF-8 hợp lệ ({type(e).__name__}), dùng cài đặt mặc định"]
    if not isinstance(data, dict):
        return Settings(), ["nội dung không phải JSON object, dùng cài đặt mặc định"]

    problems: list[str] = []
    port: int | None = None
    extra: dict[str, Any] = {}
    for key, value in data.items():
        if key == "port":
            if value is None or _valid_port(value):
                port = value
            else:
                problems.append("trường port không hợp lệ, bỏ qua")
        elif key == "version":
            if type(value) is int and value >= 1:
                extra[key] = value  # version lớn hơn bản này hỗ trợ không phải lỗi: giữ nguyên
            else:
                problems.append("trường version không hợp lệ, bỏ qua")
        else:
            extra[key] = value
    return Settings(port=port, extra=extra), problems
