"""Cài đặt của node: `<config_dir>/settings.json`.

Định dạng file và luật từng trường ở docs/architecture.md mục 4. Chỉ ghi các trường khác
mặc định; mọi khóa lạ (cấp ngoài cùng và trong từng mục thiết bị) được giữ nguyên khi ghi lại,
để bản cũ không xóa dữ liệu của bản mới. File hỏng không chặn app: dùng giá trị mặc định, và
bản gốc được chép sang `settings.json.bak` trước khi bị ghi đè. Trường sai kiểu hay sai giá trị
chỉ làm mất đúng trường đó (WARNING nêu tên trường).

Cài đặt theo thiết bị (`devices`, khóa là `device_id`) chỉ có hiệu lực với thiết bị đang tin cậy:
`Node` trả mặc định cho thiết bị khác và bỏ mục của chúng mỗi lần ghi (`pruned`); `last_conversation`
cũng vậy.

Nhớ trạng thái cửa sổ (`window`, `last_conversation`, `tray_hint_shown`) do giao diện ghi. `window`
(`WindowGeometry`) là một object được ghi đè nguyên khối: khóa lạ bên trong không được giữ, và một
khóa sai làm mất cả `window`.

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
MAX_WINDOW_COORD = 100_000  # |x|, |y| tối đa của `WindowGeometry` (pixel logic)
MAX_WINDOW_SIZE = 100_000  # rộng/cao tối đa của `WindowGeometry`
MAX_SCREEN_NAME_LEN = 256  # ký tự; `QScreen.name()`
MIN_DPR = 0.25  # tỉ lệ điểm ảnh (`devicePixelRatio`) hợp lệ của `WindowGeometry`
MAX_DPR = 16.0
RADAR_VIEWS = ("radar", "list")
THEMES = ("light", "dark")  # chế độ màu ép; None = theo hệ điều hành
# Mã màu nhấn: chỉ kiểm dạng (`app` không biết bảng màu của `ui`); mã lạ đúng dạng được giữ nguyên.
ACCENT_RE = re.compile(r"[a-z][a-z0-9_]{0,31}", re.ASCII)

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
    "theme",
    "accent",
    "window",
    "last_conversation",
    "tray_hint_shown",
    "devices",
)
_DEVICE_FIELDS = ("ask_before_receive", "clipboard")
_WINDOW_REQUIRED = ("x", "y", "width", "height")
_WINDOW_OPTIONAL = ("maximized", "screen", "dpr")

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


def _check_theme(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("theme phải là None hoặc chuỗi")
    if value not in THEMES:
        raise ValueError(f"theme phải là None hoặc một trong {THEMES}")
    return value


def _check_accent(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("accent phải là None hoặc chuỗi")
    if not ACCENT_RE.fullmatch(value):
        raise ValueError("accent phải có dạng chữ thường, số, `_`, bắt đầu bằng chữ, ≤ 32 ký tự")
    return value


def _check_last_conversation(value: object) -> str | None:
    # MessageError là ValueError.
    return None if value is None else validate_device_id(value)


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
    "theme": _check_theme,
    "accent": _check_accent,
    "last_conversation": _check_last_conversation,
    "tray_hint_shown": lambda v: _check_bool("tray_hint_shown", v),
}
# Trường bool: `null` trong file không hợp lệ (khác các trường có thể là None).
_BOOL_FIELDS = frozenset({"visible", "autostart", "first_run_done", "tray_hint_shown"})


@dataclass(frozen=True, slots=True)
class WindowGeometry:
    """Hình học cửa sổ chính đã lưu (pixel logic). Sai kiểu → TypeError, sai giá trị → ValueError.

    `x`, `y`, `width`, `height` là hình học thường (không phóng to); `maximized` là cờ phóng to
    (trạng thái thu nhỏ không được lưu); `screen` là `QScreen.name()` của màn hình chứa cửa sổ,
    `dpr` là tỉ lệ điểm ảnh của nó (None: không biết; số nguyên được đổi sang float).
    """

    x: int
    y: int
    width: int
    height: int
    maximized: bool = False
    screen: str | None = None
    dpr: float | None = None

    def __post_init__(self) -> None:
        for name in ("x", "y"):
            value = getattr(self, name)
            if type(value) is not int:  # bool bị từ chối
                raise TypeError(f"{name} phải là số nguyên")
            if not -MAX_WINDOW_COORD <= value <= MAX_WINDOW_COORD:
                raise ValueError(f"{name} phải trong ±{MAX_WINDOW_COORD}")
        for name in ("width", "height"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} phải là số nguyên")
            if not 1 <= value <= MAX_WINDOW_SIZE:
                raise ValueError(f"{name} phải trong 1–{MAX_WINDOW_SIZE}")
        _check_bool("maximized", self.maximized)
        if self.screen is not None:
            if not isinstance(self.screen, str):
                raise TypeError("screen phải là None hoặc chuỗi")
            if not 1 <= len(self.screen) <= MAX_SCREEN_NAME_LEN or "\x00" in self.screen:
                raise ValueError(f"screen phải dài 1–{MAX_SCREEN_NAME_LEN} ký tự, không có NUL")
        if self.dpr is not None:
            if type(self.dpr) not in (int, float):  # bool bị từ chối
                raise TypeError("dpr phải là None hoặc số")
            # Chỉ so sánh, không đổi sang float trước (kể cả `math.isfinite`): số nguyên khổng lồ đổi
            # sang float là OverflowError. NaN và ±inf đều không qua phép so sánh khoảng.
            if not MIN_DPR <= self.dpr <= MAX_DPR:
                raise ValueError(f"dpr phải là số hữu hạn trong {MIN_DPR}–{MAX_DPR}")
            object.__setattr__(self, "dpr", float(self.dpr))

    def to_dict(self) -> dict[str, Any]:
        """Dạng ghi ra file: luôn có `x, y, width, height, maximized`; `screen`, `dpr` khi khác None."""
        data: dict[str, Any] = {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "maximized": self.maximized,
        }
        if self.screen is not None:
            data["screen"] = self.screen
        if self.dpr is not None:
            data["dpr"] = self.dpr
        return data

    @classmethod
    def from_dict(cls, value: object) -> WindowGeometry:
        """Đọc `window` từ file; khóa lạ bị bỏ qua (không giữ).

        Không phải object, thiếu một trong `x/y/width/height` hay một trường sai → TypeError /
        ValueError (thông điệp không chứa giá trị).
        """
        if not isinstance(value, dict):
            raise TypeError("window phải là object")
        if any(key not in value for key in _WINDOW_REQUIRED):
            raise ValueError("window thiếu trường bắt buộc")
        fields = {key: value[key] for key in (*_WINDOW_REQUIRED, *_WINDOW_OPTIONAL) if key in value}
        return cls(**fields)


@dataclass(frozen=True, slots=True)
class DeviceSettings:
    """Cài đặt của một thiết bị tin cậy. `extra` giữ khóa lạ của mục (ghi lại nguyên trạng)."""

    ask_before_receive: bool = True  # False = "Luôn nhận" từ thiết bị này
    clipboard: bool = False  # “Đồng bộ clipboard” với thiết bị này (`app.clipboard`)
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
    theme: str | None = None  # None = theo hệ điều hành; ngoài ra một trong THEMES
    accent: str | None = None  # None = màu nhấn mặc định; mã lạ đúng dạng: `ui` hiện mặc định
    window: WindowGeometry | None = None  # None = giao diện dùng cỡ mặc định
    last_conversation: str | None = None  # `device_id` của cuộc trò chuyện mở gần nhất
    tray_hint_shown: bool = False  # True = đã báo “vẫn chạy ở khay” (chỉ báo một lần)
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
        if self.window is not None and not isinstance(self.window, WindowGeometry):
            raise TypeError("window phải là None hoặc WindowGeometry")
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
        """Bản sao bỏ mục `devices` và `last_conversation` của thiết bị không có trong `trusted_ids`.

        Không có gì để bỏ: trả `self`.
        """
        keep = set(trusted_ids)
        stale_conversation = self.last_conversation is not None and self.last_conversation not in keep
        if not stale_conversation and all(device_id in keep for device_id in self.devices):
            return self
        return replace(
            self,
            devices={k: v for k, v in self.devices.items() if k in keep},
            last_conversation=None if stale_conversation else self.last_conversation,
        )

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
        for name in (
            "name",
            "receive_dir",
            "rate_limit",
            "temp_retention_days",
            "language",
            "radar_view",
            "theme",
            "accent",
            "last_conversation",
        ):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        if not self.visible:
            data["visible"] = False
        if self.autostart:
            data["autostart"] = True
        if self.first_run_done:
            data["first_run_done"] = True
        if self.window is not None:
            data["window"] = self.window.to_dict()
        if self.tray_hint_shown:
            data["tray_hint_shown"] = True
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
        elif key == "window":
            if value is None:
                continue  # `null` = mặc định
            try:
                values["window"] = WindowGeometry.from_dict(value)
            except (ValueError, TypeError):
                problems.append("trường window không hợp lệ, bỏ qua")
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
