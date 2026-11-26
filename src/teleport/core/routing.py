"""Quy tắc phân loại thư mục nhận tự động (Smart Receive Routing, sans-IO).

Không chạm đĩa, không mở mạng. Mỗi quy tắc (`RoutingRule`) ánh xạ tệp nhận được từ một thiết
bị hoặc khớp mẫu tên tệp (ví dụ `*.mp4`, `*.pdf`) sang thư mục đích mong muốn.
"""

from __future__ import annotations

import fnmatch
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from teleport.core.messages import MessageError, validate_device_id

MAX_ROUTING_RULES = 64
MAX_PATTERNS_PER_RULE = 32
MAX_RULE_NAME_LEN = 64
MAX_PATTERN_LEN = 128
MAX_DESTINATION_LEN = 32767

_RULE_ID_RE = re.compile(r"[a-zA-Z0-9_-]{1,32}")
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cs"})
_FORBIDDEN_CHARS = frozenset(
    "\u2028\u2029\u200b\ufeff\u2060\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


def _check_rule_id(value: object) -> str:
    if not isinstance(value, str) or _RULE_ID_RE.fullmatch(value) is None:
        raise ValueError("id quy tắc phải gồm 1–32 ký tự chữ, số, `-` hoặc `_`")
    return value if type(value) is str else str.__str__(value)


def _check_rule_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("tên quy tắc phải là chuỗi")
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= MAX_RULE_NAME_LEN:
        raise ValueError(f"tên quy tắc phải dài 1–{MAX_RULE_NAME_LEN} ký tự")
    for ch in cleaned:
        if ch in _FORBIDDEN_CHARS or unicodedata.category(ch) in _FORBIDDEN_CATEGORIES:
            raise ValueError("tên quy tắc chứa ký tự không được phép")
    return cleaned


def _check_destination(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("destination phải là chuỗi")
    if not value or "\x00" in value or len(value) > MAX_DESTINATION_LEN or not os.path.isabs(value):
        raise ValueError("destination phải là đường dẫn tuyệt đối hợp lệ")
    return value if type(value) is str else str.__str__(value)


def _check_patterns(value: object) -> tuple[str, ...]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise ValueError("patterns phải là dãy chuỗi")
    if len(value) > MAX_PATTERNS_PER_RULE:
        raise ValueError(f"mỗi quy tắc tối đa {MAX_PATTERNS_PER_RULE} mẫu")
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("mỗi mẫu tên tệp phải là chuỗi")
        pat = item.strip()
        if not pat or len(pat) > MAX_PATTERN_LEN:
            raise ValueError(f"mẫu tên tệp phải dài 1–{MAX_PATTERN_LEN} ký tự")
        if "\x00" in pat or "/" in pat or "\\" in pat:
            raise ValueError("mẫu tên tệp không được chứa ký tự phân tách đường dẫn hoặc NUL")
        key = pat.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(pat)
    return tuple(cleaned)


def parse_patterns(text: str) -> tuple[str, ...]:
    """Tách chuỗi người dùng nhập (cách nhau bằng dấu phẩy, chấm phẩy hoặc khoảng trắng) thành mẫu."""
    if not isinstance(text, str):
        raise TypeError("text phải là chuỗi")
    parts = [part.strip() for part in re.split(r"[,;\s]+", text) if part.strip()]
    return _check_patterns(parts)


@dataclass(frozen=True, slots=True)
class RoutingRule:
    """Một quy tắc chuyển hướng thư mục nhận khi tệp hoàn tất kiểm tra toàn vẹn."""

    id: str
    name: str
    destination: str
    device_id: str | None = None
    patterns: tuple[str, ...] = ()
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _check_rule_id(self.id))
        object.__setattr__(self, "name", _check_rule_name(self.name))
        object.__setattr__(self, "destination", _check_destination(self.destination))
        if self.device_id is not None:
            try:
                checked_dev = validate_device_id(self.device_id)
            except MessageError as e:
                raise ValueError(str(e)) from None
            object.__setattr__(self, "device_id", checked_dev)
        object.__setattr__(self, "patterns", _check_patterns(self.patterns))
        if type(self.enabled) is not bool:
            raise ValueError("enabled phải là bool")


def match_routing_rule(
    rules: Sequence[RoutingRule],
    *,
    device_id: str,
    filename: str,
    rel_path: str = "",
) -> RoutingRule | None:
    """Trả về quy tắc đang bật đầu tiên khớp thiết bị nguồn và tên tệp, hoặc None."""
    leaf = rel_path.replace("\\", "/").rsplit("/", 1)[-1] if rel_path else filename
    leaf_cf = leaf.casefold()
    for rule in rules:
        if not rule.enabled:
            continue
        if rule.device_id is not None and rule.device_id != device_id:
            continue
        if not rule.patterns or any(fnmatch.fnmatchcase(leaf_cf, pat.casefold()) for pat in rule.patterns):
            return rule
    return None


def routing_rule_to_dict(rule: RoutingRule) -> dict[str, object]:
    """Chuyển `RoutingRule` thành dict để ghi `settings.json`."""
    data: dict[str, object] = {
        "id": rule.id,
        "name": rule.name,
        "destination": rule.destination,
    }
    if rule.device_id is not None:
        data["device_id"] = rule.device_id
    if rule.patterns:
        data["patterns"] = list(rule.patterns)
    if not rule.enabled:
        data["enabled"] = False
    return data


def routing_rule_from_dict(obj: object) -> RoutingRule:
    """Đọc và kiểm tra `RoutingRule` từ dict JSON."""
    if not isinstance(obj, Mapping):
        raise ValueError("mục routing_rules phải là object")
    rule_id = obj.get("id")
    name = obj.get("name")
    destination = obj.get("destination")
    device_id = obj.get("device_id")
    patterns = obj.get("patterns", ())
    enabled = obj.get("enabled", True)
    return RoutingRule(
        id=rule_id,  # type: ignore[arg-type]
        name=name,  # type: ignore[arg-type]
        destination=destination,  # type: ignore[arg-type]
        device_id=device_id,
        patterns=patterns,
        enabled=enabled,
    )


__all__ = [
    "MAX_PATTERNS_PER_RULE",
    "MAX_PATTERN_LEN",
    "MAX_ROUTING_RULES",
    "MAX_RULE_NAME_LEN",
    "RoutingRule",
    "match_routing_rule",
    "parse_patterns",
    "routing_rule_from_dict",
    "routing_rule_to_dict",
]
