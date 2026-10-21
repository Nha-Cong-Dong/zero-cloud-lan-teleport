"""Hiểu thứ người dùng gõ: địa chỉ nhập tay (`ip[:cổng]`) và tên / mã của thiết bị tin cậy.

Hàm thuần, không I/O. Không bao giờ tự đoán (mvp F12): khớp nhiều thiết bị thì báo để
người dùng gõ cụ thể hơn.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Iterable, Sequence

from teleport.net.connection import DEFAULT_PORT
from teleport.trust.store import TrustedDevice

MIN_ID_PREFIX = 4

# Lớp ký tự ASCII và re.fullmatch: `\d` nhận cả chữ số Unicode, `$` khớp cả trước "\n" cuối.
_ADDRESS_RE = re.compile(r"([0-9.]+)(?::([1-9][0-9]{0,4}))?")
_HEX_RE = re.compile(r"[0-9a-f]+")
_ASCII_SPACE = " \t\n\r\x0b\x0c"
_BROADCAST = ipaddress.IPv4Address("255.255.255.255")
_ADDRESS_HINT = "ví dụ 192.168.1.5 hoặc 192.168.1.5:47474"


class DeviceLookupError(Exception):
    """Không xác định được đúng một thiết bị tin cậy từ chuỗi người dùng nhập."""

    query: str


class DeviceNotFound(DeviceLookupError):
    """Không thiết bị tin cậy nào khớp.

    `known`: mọi thiết bị tin cậy lúc tìm (để giao diện liệt kê), sắp như `candidates`.
    """

    def __init__(self, query: str, known: Sequence[TrustedDevice] = ()) -> None:
        super().__init__(f"không có thiết bị tin cậy nào khớp {query!r}")
        self.query = query
        self.known: tuple[TrustedDevice, ...] = tuple(sorted(known, key=_sort_key))


class AmbiguousDevice(DeviceLookupError):
    """Khớp nhiều thiết bị; `candidates` sắp theo (NFC(name).casefold(), device_id)."""

    def __init__(self, query: str, candidates: Sequence[TrustedDevice]) -> None:
        super().__init__(f"{query!r} khớp {len(candidates)} thiết bị")
        self.query = query
        self.candidates: tuple[TrustedDevice, ...] = tuple(sorted(candidates, key=_sort_key))


def _nfc_fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _sort_key(device: TrustedDevice) -> tuple[str, str]:
    return _nfc_fold(device.name), device.device_id


def parse_manual_address(text: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """Đọc `ip` hoặc `ip:cổng` người dùng nhập; trả `(ip, cổng)`.

    `ip` là IPv4 dạng chấm viết chuẩn, không phải 0.0.0.0, multicast hay 255.255.255.255
    (loopback được phép). Không phân giải tên miền, không nhận IPv6. Sai → ValueError
    với thông điệp tiếng Việt kèm ví dụ đúng.
    """
    if not isinstance(text, str):
        raise ValueError(f"địa chỉ phải là chuỗi, {_ADDRESS_HINT}")
    value = text.strip(_ASCII_SPACE)
    match = _ADDRESS_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"địa chỉ không hợp lệ: {text!r}; hãy nhập địa chỉ IP, {_ADDRESS_HINT}")
    ip_text, port_text = match.group(1), match.group(2)
    try:
        ip = ipaddress.IPv4Address(ip_text)
    except ValueError as e:
        raise ValueError(f"địa chỉ IP không hợp lệ: {ip_text!r}; {_ADDRESS_HINT}") from e
    if str(ip) != ip_text:
        raise ValueError(f"địa chỉ IP không hợp lệ: {ip_text!r}; {_ADDRESS_HINT}")
    if ip.is_unspecified or ip.is_multicast or ip == _BROADCAST:
        raise ValueError(f"{ip_text} không phải địa chỉ của một máy; {_ADDRESS_HINT}")
    port = default_port if port_text is None else int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError(f"cổng phải trong 1–65535; {_ADDRESS_HINT}")
    return ip_text, port


def resolve_trusted(query: str, devices: Iterable[TrustedDevice]) -> TrustedDevice:
    """Tìm đúng một thiết bị tin cậy theo tên hoặc tiền tố `device_id`.

    Tên: trùng nguyên tên sau NFC + casefold (không khớp tiền tố tên). Mã: ít nhất
    MIN_ID_PREFIX ký tự hex (không phân biệt hoa thường) là tiền tố của `device_id`.
    Gộp hai cách khớp. Đúng một → trả về; không có (kể cả `query` rỗng) → DeviceNotFound;
    nhiều → AmbiguousDevice. `query` không phải chuỗi → TypeError.
    """
    if not isinstance(query, str):
        raise TypeError("query phải là chuỗi")
    known = list(devices)
    q = query.strip()
    if not q:
        raise DeviceNotFound(query, known)
    name_key = _nfc_fold(q)
    id_prefix = q.lower()
    by_id = len(q) >= MIN_ID_PREFIX and _HEX_RE.fullmatch(id_prefix) is not None
    matches: dict[str, TrustedDevice] = {}
    for device in known:
        if _nfc_fold(device.name) == name_key or (by_id and device.device_id.startswith(id_prefix)):
            matches[device.device_id] = device
    if not matches:
        raise DeviceNotFound(query, known)
    if len(matches) > 1:
        raise AmbiguousDevice(query, list(matches.values()))
    return next(iter(matches.values()))
