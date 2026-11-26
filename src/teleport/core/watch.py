"""Logic theo dõi thư mục đẩy tự động một chiều (Watch Portal, sans-IO).

Không chạm đĩa, không mở mạng. `WatchTracker` giữ trạng thái từng quy tắc `WatchPortalRule`:
- Lần quét đầu sau khi bật: ghi nhận toàn bộ tệp sẵn có (seed) mà không gửi hàng loạt tệp cũ.
- Khi có tệp mới hoặc kích thước / `mtime_ns` thay đổi: đợi ổn định đủ `STABILITY_SECONDS`
  trước khi phát tín hiệu sẵn sàng gửi để tránh gửi tệp đang xuất dở.
- Chỉ đẩy một chiều: xóa tệp khỏi thư mục nguồn chỉ dọn mục khỏi bộ theo dõi, không xóa ở máy kia.
"""

from __future__ import annotations

import fnmatch
import os
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from teleport.core.messages import MessageError, validate_device_id

MAX_WATCH_PORTALS = 32
STABILITY_SECONDS = 2.0
MAX_TRACKED_FILES = 10_000
MAX_PATTERNS_PER_RULE = 32
MAX_RULE_NAME_LEN = 64
MAX_PATTERN_LEN = 128
MAX_SOURCE_DIR_LEN = 32767

IGNORED_TEMP_SUFFIXES = (".part", ".tmp", ".crdownload", ".download", ".partial")
IGNORED_PREFIXES = (".", "~$")

_RULE_ID_RE = re.compile(r"[a-zA-Z0-9_-]{1,32}")
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cs"})
_FORBIDDEN_CHARS = frozenset(
    "\u2028\u2029\u200b\ufeff\u2060\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


def is_ignored_watch_name(name: str) -> bool:
    """True nếu tên thành phần là tệp ẩn, tệp khóa hoặc tệp tạm đang tải/ghi dở."""
    if not name or any(name.startswith(prefix) for prefix in IGNORED_PREFIXES):
        return True
    lower = name.casefold()
    return any(lower.endswith(suffix) for suffix in IGNORED_TEMP_SUFFIXES)


def _check_rule_id(value: object) -> str:
    if not isinstance(value, str) or _RULE_ID_RE.fullmatch(value) is None:
        raise ValueError("id cổng đẩy phải gồm 1–32 ký tự chữ, số, `-` hoặc `_`")
    return value if type(value) is str else str.__str__(value)


def _check_rule_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("tên cổng đẩy phải là chuỗi")
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= MAX_RULE_NAME_LEN:
        raise ValueError(f"tên cổng đẩy phải dài 1–{MAX_RULE_NAME_LEN} ký tự")
    for ch in cleaned:
        if ch in _FORBIDDEN_CHARS or unicodedata.category(ch) in _FORBIDDEN_CATEGORIES:
            raise ValueError("tên cổng đẩy chứa ký tự không được phép")
    return cleaned


def _check_source_dir(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("source_dir phải là chuỗi")
    if not value or "\x00" in value or len(value) > MAX_SOURCE_DIR_LEN or not os.path.isabs(value):
        raise ValueError("source_dir phải là đường dẫn tuyệt đối hợp lệ")
    return value if type(value) is str else str.__str__(value)


def _check_patterns(value: object) -> tuple[str, ...]:
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise ValueError("patterns phải là dãy chuỗi")
    if len(value) > MAX_PATTERNS_PER_RULE:
        raise ValueError(f"mỗi cổng đẩy tối đa {MAX_PATTERNS_PER_RULE} mẫu")
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


@dataclass(frozen=True, slots=True)
class WatchPortalRule:
    """Một quy tắc theo dõi thư mục và đẩy tự động tới một thiết bị tin cậy."""

    id: str
    name: str
    source_dir: str
    device_id: str
    patterns: tuple[str, ...] = ()
    recursive: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _check_rule_id(self.id))
        object.__setattr__(self, "name", _check_rule_name(self.name))
        object.__setattr__(self, "source_dir", _check_source_dir(self.source_dir))
        try:
            checked_dev = validate_device_id(self.device_id)
        except MessageError as e:
            raise ValueError(str(e)) from None
        object.__setattr__(self, "device_id", checked_dev)
        object.__setattr__(self, "patterns", _check_patterns(self.patterns))
        if type(self.recursive) is not bool:
            raise ValueError("recursive phải là bool")
        if type(self.enabled) is not bool:
            raise ValueError("enabled phải là bool")


def watch_portal_to_dict(rule: WatchPortalRule) -> dict[str, object]:
    """Chuyển `WatchPortalRule` thành dict để ghi `settings.json`."""
    data: dict[str, object] = {
        "id": rule.id,
        "name": rule.name,
        "source_dir": rule.source_dir,
        "device_id": rule.device_id,
    }
    if rule.patterns:
        data["patterns"] = list(rule.patterns)
    if rule.recursive:
        data["recursive"] = True
    if not rule.enabled:
        data["enabled"] = False
    return data


def watch_portal_from_dict(obj: object) -> WatchPortalRule:
    """Đọc và kiểm tra `WatchPortalRule` từ dict JSON."""
    if not isinstance(obj, Mapping):
        raise ValueError("mục watch_portals phải là object")
    return WatchPortalRule(
        id=obj.get("id"),  # type: ignore[arg-type]
        name=obj.get("name"),  # type: ignore[arg-type]
        source_dir=obj.get("source_dir"),  # type: ignore[arg-type]
        device_id=obj.get("device_id"),  # type: ignore[arg-type]
        patterns=obj.get("patterns", ()),
        recursive=obj.get("recursive", False),
        enabled=obj.get("enabled", True),
    )


@dataclass(slots=True)
class _RuleState:
    rule: WatchPortalRule
    seeded: bool = False
    pending: dict[str, tuple[tuple[int, int], float]] = field(default_factory=dict)
    sent: OrderedDict[str, tuple[int, int]] = field(default_factory=OrderedDict)


class WatchTracker:
    """Máy trạng thái thuần cho các cổng đẩy tự động (sans-IO)."""

    def __init__(self, rules: Sequence[WatchPortalRule] = ()) -> None:
        self._states: dict[str, _RuleState] = {}
        self.set_rules(rules)

    def set_rules(self, rules: Sequence[WatchPortalRule]) -> None:
        """Cập nhật danh sách quy tắc; giữ trạng thái với quy tắc không đổi cấu hình."""
        next_states: dict[str, _RuleState] = {}
        for rule in rules:
            if not rule.enabled:
                continue
            prev = self._states.get(rule.id)
            if (
                prev is not None
                and prev.rule.source_dir == rule.source_dir
                and prev.rule.device_id == rule.device_id
                and prev.rule.patterns == rule.patterns
                and prev.rule.recursive == rule.recursive
            ):
                prev.rule = rule
                next_states[rule.id] = prev
            else:
                next_states[rule.id] = _RuleState(rule=rule)
        self._states = next_states

    @staticmethod
    def matches_rule(rule: WatchPortalRule, rel_path: str) -> bool:
        """True nếu `rel_path` hợp lệ (không phải tên tạm/ẩn) và khớp bộ lọc mẫu của `rule`."""
        parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
        if not parts or any(is_ignored_watch_name(part) for part in parts):
            return False
        if not rule.patterns:
            return True
        leaf_cf = parts[-1].casefold()
        return any(fnmatch.fnmatchcase(leaf_cf, pat.casefold()) for pat in rule.patterns)

    def scan(
        self,
        rule_id: str,
        entries: Mapping[str, tuple[int, int]],
        now: float,
        *,
        stability: float = STABILITY_SECONDS,
    ) -> list[str]:
        """Xử lý ảnh chụp thư mục ở thời điểm `now`; trả các đường dẫn đã ổn định để gửi."""
        state = self._states.get(rule_id)
        if state is None:
            return []
        rule = state.rule
        filtered: dict[str, tuple[int, int]] = {}
        for raw_rel, sig in entries.items():
            rel = raw_rel.replace("\\", "/").strip("/")
            size, mtime_ns = sig
            if size <= 0 or mtime_ns < 0:
                continue
            if self.matches_rule(rule, rel):
                filtered[rel] = (size, mtime_ns)

        if not state.seeded:
            state.seeded = True
            for rel in sorted(filtered):
                state.sent[rel] = filtered[rel]
                while len(state.sent) > MAX_TRACKED_FILES:
                    state.sent.popitem(last=False)
            state.pending.clear()
            return []

        for gone in [k for k in state.pending if k not in filtered]:
            del state.pending[gone]
        for gone in [k for k in state.sent if k not in filtered]:
            del state.sent[gone]

        ready: list[str] = []
        for rel in sorted(filtered):
            sig = filtered[rel]
            if state.sent.get(rel) == sig:
                state.pending.pop(rel, None)
                continue
            prev = state.pending.get(rel)
            if prev is None or prev[0] != sig:
                state.pending[rel] = (sig, now)
                continue
            if now - prev[1] >= stability:
                ready.append(rel)
                del state.pending[rel]
                state.sent.pop(rel, None)
                state.sent[rel] = sig
                while len(state.sent) > MAX_TRACKED_FILES:
                    state.sent.popitem(last=False)
        return ready


__all__ = [
    "IGNORED_PREFIXES",
    "IGNORED_TEMP_SUFFIXES",
    "MAX_TRACKED_FILES",
    "MAX_WATCH_PORTALS",
    "STABILITY_SECONDS",
    "WatchPortalRule",
    "WatchTracker",
    "is_ignored_watch_name",
    "watch_portal_from_dict",
    "watch_portal_to_dict",
]
