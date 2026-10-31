"""Cài đặt của node: `<config_dir>/settings.json`.

Định dạng file và luật từng trường ở docs/architecture.md mục 4. Chỉ ghi các trường khác
mặc định; mọi khóa lạ (cấp ngoài cùng và trong từng mục thiết bị) được giữ nguyên khi ghi lại,
để bản cũ không xóa dữ liệu của bản mới. File hỏng không chặn app: dùng giá trị mặc định, và
bản gốc được chép sang `settings.json.bak` trước khi bị ghi đè. Trường sai kiểu hay sai giá trị
chỉ làm mất đúng trường đó (WARNING nêu tên trường).

Cài đặt theo thiết bị (`devices`, khóa là `device_id`) chỉ có hiệu lực với thiết bị đang tin cậy:
`Node` trả mặc định cho thiết bị khác và bỏ mục của chúng mỗi lần ghi (`pruned`).

Không bao giờ ghi nội dung file vào log (mvp N9): chỉ ghi đường dẫn, tên trường và loại lỗi.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from teleport.atomic import write_atomic
from teleport.core import ratelimit
from teleport.core.messages import validate_device_id, validate_device_name

SETTINGS_FILE = "settings.json"
BACKUP_FILE = "settings.json.bak"
SETTINGS_VERSION = 1
MAX_SETTINGS_SIZE = 1024 * 1024  # lớn hơn → coi là hỏng, không đọc vào RAM
MIN_RATE_LIMIT = ratelimit.CHUNK_SIZE  # = `app.transfers.MIN_RATE_LIMIT`
MAX_RATE_LIMIT = 2**53 - 1  # số nguyên lớn nhất JSON giữ chính xác
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 365
MAX_RECEIVE_DIR_LEN = 32767  # ký tự; trần đường dẫn dài của Windows
RADAR_VIEWS = ("radar", "list")

_LANGUAGE_RE = re.compile(r"[a-z]{2,3}(_[A-Z]{2})?", re.ASCII)  # như `ui/i18n.py`: `vi`, `vi_VN`

# Khóa cấp ngoài cùng do `Settings` quản lý (không được nằm trong `extra`).
_FIELDS = (
    "port",
    "name",
    "receive_dir",
    "visible",
    "rate_limit",
    "temp_retention_days",
    "language",
    "autostart",
    "first_run_done",
    "radar_view",
    "devices",
)
_DEVICE_FIELDS = ("ask_before_receive", "clipboard")

log = logging.getLogger(__name__)


class SettingsSaveError(Exception):
    """Không ghi được `settings.json`; `__cause__` là OSError, ValueError, TypeError hoặc RecursionError."""


def _valid_port(value: object) -> bool:
    # bool là lớp con của int: bị từ chối.
    return type(value) is int and 1 <= value <= 65535


def _check_extra(extra: object, reserved: Iterable[str]) -> dict[str, Any]:
    if not isinstance(extra, Mapping):
        raise ValueError("extra phải là mapping")
    copy = dict(extra)
    if any(not isinstance(key, str) for key in copy):
        raise ValueError("khóa của extra phải là chuỗi")
    for key in reserved:
        if key in copy:
            raise ValueError(f'extra không được chứa khóa "{key}"')
    return copy


# ---- Kiểm từng trường (dùng chung cho constructor và `load`) ---------------------------------
# Mỗi hàm: giá trị hợp lệ → trả lại nó; sai → ValueError/TypeError. None luôn hợp lệ (= mặc định).


def _check_name(value: object) -> str | None:
    return None if value is None else validate_device_name(value)


def _check_receive_dir(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("receive_dir phải là chuỗi")
    if "\x00" in value or len(value) > MAX_RECEIVE_DIR_LEN or not os.path.isabs(value):
        raise ValueError(
            f"receive_dir phải là đường dẫn tuyệt đối, không có NUL, ≤ {MAX_RECEIVE_DIR_LEN} ký tự"
        )
    return value


def _check_bool(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} phải là bool")
    return value


def _check_rate_limit(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError("rate_limit phải là None hoặc số nguyên")
    if not MIN_RATE_LIMIT <= value <= MAX_RATE_LIMIT:
        raise ValueError(f"rate_limit phải trong {MIN_RATE_LIMIT}–{MAX_RATE_LIMIT} byte/giây")
    return value


def _check_retention(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError("temp_retention_days phải là None hoặc số nguyên")
    if not MIN_RETENTION_DAYS <= value <= MAX_RETENTION_DAYS:
        raise ValueError(f"temp_retention_days phải trong {MIN_RETENTION_DAYS}–{MAX_RETENTION_DAYS}")
    return value


def _check_language(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("language phải là None hoặc chuỗi")
    if not _LANGUAGE_RE.fullmatch(value):
        raise ValueError("language phải có dạng `vi` hoặc `vi_VN`")
    return value


def _check_radar_view(value: object) -> str | None:
    if value is None:
        return None
    if value not in RADAR_VIEWS or not isinstance(value, str):
        raise ValueError(f"radar_view phải là None hoặc một trong {RADAR_VIEWS}")
    return value


_CHECKS: dict[str, Any] = {
    "name": _check_name,
    "receive_dir": _check_receive_dir,
    "visible": lambda v: _check_bool("visible", v),
    "rate_limit": _check_rate_limit,
    "temp_retention_days": _check_retention,
    "language": _check_language,
    "autostart": lambda v: _check_bool("autostart", v),
    "first_run_done": lambda v: _check_bool("first_run_done", v),
    "radar_view": _check_radar_view,
}
# Trường bool: `null` trong file không hợp lệ (khác các trường có thể là None).
_BOOL_FIELDS = frozenset({"visible", "autostart", "first_run_done"})


@dataclass(frozen=True, slots=True)
class DeviceSettings:
    """Cài đặt của một thiết bị tin cậy. `extra` giữ khóa lạ của mục (ghi lại nguyên trạng)."""

    ask_before_receive: bool = True  # False = "Luôn nhận" từ thiết bị này
    clipboard: bool = False  # đồng bộ clipboard (chưa dùng: chỉ lưu)
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        _check_bool("ask_before_receive", self.ask_before_receive)
        _check_bool("clipboard", self.clipboard)
        object.__setattr__(self, "extra", _check_extra(self.extra, _DEVICE_FIELDS))

    def is_default(self) -> bool:
        """Không có gì để ghi: mọi trường mặc định, không có khóa lạ."""
        return self.ask_before_receive and not self.clipboard and not self.extra


_DEFAULT_DEVICE = DeviceSettings()


@dataclass(frozen=True, slots=True)
class Settings:
    """Cài đặt đã đọc (bất biến). Sai kiểu → TypeError, sai giá trị → ValueError (luật: architecture §4).

    `extra` giữ mọi khóa cấp ngoài cùng không phải trường ở đây (kể cả `version`).
    """

    port: int | None = None  # None = chưa lưu cổng nào
    extra: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)
    name: str | None = None  # None = hostname (`default_device_name`)
    receive_dir: str | None = None  # None = `paths.default_receive_dir()`
    visible: bool = True
    rate_limit: int | None = None  # byte/giây; None = không giới hạn
    temp_retention_days: int | None = None  # None = `storage.TEMP_RETENTION` (đọc lúc dùng)
    language: str | None = None  # None = theo hệ điều hành
    autostart: bool = False
    first_run_done: bool = False
    radar_view: str | None = None  # None = giao diện tự chọn
    devices: Mapping[str, DeviceSettings] = field(default_factory=dict)
    # `load` không đọc được file (OSError khác "chưa có file"): bản này là mặc định chứ không phải nội
    # dung file, nên `save` chép nguyên file đang có sang `.bak` trước khi ghi đè. Không ghi ra file.
    load_failed: bool = field(default=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.port is not None and not _valid_port(self.port):
            raise ValueError("port phải là None hoặc số nguyên 1–65535")
        for name, check in _CHECKS.items():
            check(getattr(self, name))
        object.__setattr__(self, "extra", _check_extra(self.extra, _FIELDS))
        if not isinstance(self.devices, Mapping):
            raise TypeError("devices phải là mapping device_id → DeviceSettings")
        devices: dict[str, DeviceSettings] = {}
        for device_id, value in self.devices.items():
            validate_device_id(device_id)
            if not isinstance(value, DeviceSettings):
                raise TypeError("giá trị của devices phải là DeviceSettings")
            devices[device_id] = value
        object.__setattr__(self, "devices", MappingProxyType(devices))

    # ---- Hàm thuần --------------------------------------------------------------------------

    def device(self, device_id: str) -> DeviceSettings:
        """Cài đặt của thiết bị `device_id`; chưa có mục thì mặc định. Không kiểm tin cậy."""
        return self.devices.get(device_id, _DEFAULT_DEVICE)

    def with_device(self, device_id: str, value: DeviceSettings | None) -> Settings:
        """Bản sao với mục của `device_id` thay bằng `value` (`None`: xóa mục). Mã sai → MessageError."""
        validate_device_id(device_id)
        devices = dict(self.devices)
        if value is None:
            devices.pop(device_id, None)
        else:
            devices[device_id] = value
        return replace(self, devices=devices)

    def pruned(self, trusted_ids: Iterable[str]) -> Settings:
        """Bản sao bỏ mục của thiết bị không có trong `trusted_ids` (không có gì để bỏ: trả `self`)."""
        keep = set(trusted_ids)
        if all(device_id in keep for device_id in self.devices):
            return self
        return replace(self, devices={k: v for k, v in self.devices.items() if k in keep})

    # ---- Đọc / ghi --------------------------------------------------------------------------

    @classmethod
    def load(cls, config_dir: Path) -> Settings:
        """Đọc `settings.json`; không bao giờ raise vì nội dung file.

        Chưa có file → mặc định (không tạo file). Lỗi đọc hay file hỏng → log WARNING,
        dùng mặc định. Trường sai → log WARNING (chỉ tên trường), bỏ trường đó; lần `save` sau
        chép file sang `.bak`.
        """
        path = config_dir / SETTINGS_FILE
        try:
            raw = _read_limited(path)
        except FileNotFoundError:
            return cls()
        except OSError as e:
            log.warning("không đọc được %s (%s), dùng cài đặt mặc định", path, type(e).__name__)
            return cls(load_failed=True)
        settings, problems = _parse(raw)
        for problem in problems:
            log.warning("%s: %s", path, problem)
        return settings

    def save(self, config_dir: Path) -> None:
        """Ghi `settings.json` (nguyên tử); chỉ ghi trường khác mặc định, giữ khóa lạ.

        File đang có mà hỏng (theo đúng luật của `load`), hoặc bản này do `load` trả khi không
        đọc được file (`load_failed`), thì chép nguyên byte sang `settings.json.bak` trước (ghi
        đè `.bak` cũ); vẫn không đọc được file thì OSError, không ghi gì. Lỗi đọc/ghi (`OSError`) bay ra; ghi
        `.bak` thất bại thì không ghi file chính. `extra` không ghi ra JSON được → TypeError
        (hay ValueError, RecursionError).
        """
        path = config_dir / SETTINGS_FILE
        data: dict[str, Any] = {"version": self.extra.get("version", SETTINGS_VERSION)}
        data.update((k, v) for k, v in self.extra.items() if k != "version")
        if self.port is not None:
            data["port"] = self.port
        for name in ("name", "receive_dir", "rate_limit", "temp_retention_days", "language", "radar_view"):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        if not self.visible:
            data["visible"] = False
        if self.autostart:
            data["autostart"] = True
        if self.first_run_done:
            data["first_run_done"] = True
        devices = {k: _device_dict(v) for k, v in self.devices.items() if not v.is_default()}
        if devices:
            data["devices"] = devices
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
        if old is not None and (self.load_failed or _parse(old)[1]):
            # Hỏng, hoặc bản này nạp lỗi đọc (là mặc định, không phải nội dung file): giữ bản gốc.
            write_atomic(config_dir / BACKUP_FILE, old)
        write_atomic(path, content)


def _device_dict(value: DeviceSettings) -> dict[str, Any]:
    data = dict(value.extra)
    if not value.ask_before_receive:
        data["ask_before_receive"] = False
    if value.clipboard:
        data["clipboard"] = True
    return data


def _read_limited(path: Path) -> bytes:
    """Đọc tối đa MAX_SETTINGS_SIZE + 1 byte (đủ để biết file quá lớn mà không đọc hết)."""
    with path.open("rb") as f:
        return f.read(MAX_SETTINGS_SIZE + 1)


def _parse(raw: bytes) -> tuple[Settings, list[str]]:
    """Phân tích nội dung file; trả cài đặt và danh sách vấn đề (rỗng = file không hỏng).

    Thông điệp vấn đề chỉ nói loại lỗi và tên trường, không chứa nội dung file.
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
    values: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for key, value in data.items():
        if key == "port":
            if value is None or _valid_port(value):
                values["port"] = value
            else:
                problems.append("trường port không hợp lệ, bỏ qua")
        elif key == "version":
            if type(value) is int and value >= 1:
                extra[key] = value  # version lớn hơn bản này hỗ trợ không phải lỗi: giữ nguyên
            else:
                problems.append("trường version không hợp lệ, bỏ qua")
        elif key == "devices":
            if value is not None:  # `null` = không có mục nào
                values["devices"] = _parse_devices(value, problems)
        elif key in _CHECKS:
            if value is None and key not in _BOOL_FIELDS:
                continue  # `null` = mặc định
            try:
                values[key] = _CHECKS[key](value)
            except (ValueError, TypeError):
                problems.append(f"trường {key} không hợp lệ, bỏ qua")
        else:
            extra[key] = value
    return Settings(extra=extra, **values), problems


def _parse_devices(value: object, problems: list[str]) -> dict[str, DeviceSettings]:
    """`devices` trong file → mục hợp lệ; mục sai bị bỏ, trường sai về mặc định (mỗi lỗi một vấn đề)."""
    if not isinstance(value, dict):
        problems.append("trường devices không phải object, bỏ qua")
        return {}
    devices: dict[str, DeviceSettings] = {}
    for device_id, entry in value.items():
        try:
            validate_device_id(device_id)
        except ValueError:
            problems.append("trường devices có khóa không phải device_id, bỏ mục đó")
            continue
        if not isinstance(entry, dict):
            problems.append("trường devices có mục không phải object, bỏ mục đó")
            continue
        fields: dict[str, bool] = {}
        extra: dict[str, Any] = {}
        for key, item in entry.items():
            if key in _DEVICE_FIELDS:
                if type(item) is bool:
                    fields[key] = item
                else:
                    problems.append(f"trường devices.{key} không hợp lệ, dùng mặc định")
            else:
                extra[key] = item
        devices[device_id] = DeviceSettings(extra=extra, **fields)
    return devices
