"""Thiết bị người dùng thấy, và hiểu thứ người dùng gõ.

- `DeviceRegistry` / `DeviceView`: gộp `trusted.json`, mDNS và trạng thái kết nối.
  Mỗi thiết bị đã ghép đôi hoặc đang thấy qua mDNS có một view. `paired` và tên của
  thiết bị đã ghép đôi chỉ lấy từ `trusted.json`; mDNS chỉ là gợi ý (tên của thiết bị
  chưa ghép đôi, hệ điều hành, địa chỉ, cổng). Hệ điều hành của thiết bị đã ghép đôi lấy
  trước hết từ `hello.os` của kết nối điều khiển đã xác thực (`set_peer_hello`), chỉ giữ
  trong RAM. `last_address` (địa chỉ của kết nối điều khiển ra gần nhất) lấy từ
  `trusted.json`; đổi địa chỉ không sinh sự kiện.
- `parse_manual_address`: địa chỉ nhập tay (`ip[:cổng]`).
- `resolve_trusted` / `resolve_device`: tên hoặc tiền tố mã thiết bị.

Hàm và lớp thuần, không I/O. Không bao giờ tự đoán (mvp F12): khớp nhiều thiết bị thì
báo để người dùng gõ cụ thể hơn.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from teleport.core.discovery import DiscoveredDevice
from teleport.core.links import check_endpoint
from teleport.net.connection import DEFAULT_PORT
from teleport.trust.store import TrustedDevice

MIN_ID_PREFIX = 4

# Lớp ký tự ASCII và re.fullmatch: `\d` nhận cả chữ số Unicode, `$` khớp cả trước "\n" cuối.
_ADDRESS_RE = re.compile(r"([0-9.]+)(?::([1-9][0-9]{0,4}))?")
_HEX_RE = re.compile(r"[0-9a-f]+")
_ASCII_SPACE = " \t\n\r\x0b\x0c"
_ADDRESS_HINT = "ví dụ 192.168.1.5 hoặc 192.168.1.5:47474"


@dataclass(frozen=True, slots=True)
class DeviceView:
    """Một thiết bị như người dùng thấy (đã ghép đôi, hoặc đang thấy qua mDNS)."""

    device_id: str
    name: str  # đã ghép đôi: tên trong trusted.json; chưa: tên TXT mDNS
    # Đã ghép đôi: `hello.os` đã xác thực đã nhớ (giữ cả khi ngoại tuyến), không có thì mDNS;
    # chưa ghép đôi: mDNS. None nếu không có nguồn nào.
    os: str | None
    paired: bool  # CHỈ theo trusted.json
    online: bool  # mDNS đang thấy (không tính kết nối)
    connected: bool  # có kết nối điều khiển (chỉ có thể True khi paired)
    addresses: tuple[str, ...]  # từ mDNS; () nếu không thấy
    port: int | None  # từ mDNS; None nếu không thấy
    # Đã ghép đôi: (IPv4, cổng) của kết nối điều khiển ra gần nhất (`TrustedDevice.last_address`).
    last_address: tuple[str, int] | None = None


class DeviceLookupError(Exception):
    """Không xác định được đúng một thiết bị từ chuỗi người dùng nhập."""

    query: str


class DeviceNotFound(DeviceLookupError):
    """Không thiết bị nào khớp.

    `known`: mọi thiết bị đã xét lúc tìm (để giao diện liệt kê), sắp như `candidates`.
    """

    def __init__(self, query: str, known: Sequence[TrustedDevice | DeviceView] = ()) -> None:
        super().__init__(f"không có thiết bị nào khớp {query!r}")
        self.query = query
        self.known: tuple[TrustedDevice | DeviceView, ...] = tuple(sorted(known, key=_sort_key))


class AmbiguousDevice(DeviceLookupError):
    """Khớp nhiều thiết bị; `candidates` sắp theo (NFC(name).casefold(), device_id)."""

    def __init__(self, query: str, candidates: Sequence[TrustedDevice | DeviceView]) -> None:
        super().__init__(f"{query!r} khớp {len(candidates)} thiết bị")
        self.query = query
        self.candidates: tuple[TrustedDevice | DeviceView, ...] = tuple(sorted(candidates, key=_sort_key))


def _nfc_fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _sort_key(device: TrustedDevice | DeviceView) -> tuple[str, str]:
    return _nfc_fold(device.name), device.device_id


# ---- Gộp mDNS + trusted.json ---------------------------------------------------------------


class DeviceRegistry:
    """Gộp `trusted.json`, mDNS và trạng thái kết nối thành `DeviceView` (thuần, không I/O).

    Có view khi thiết bị đã ghép đôi hoặc đang thấy qua mDNS; máy này không bao giờ có
    view. Dữ liệu mDNS không bao giờ đổi `paired` hay tên của thiết bị đã ghép đôi.
    `os` từ `hello` của thiết bị đã ghép đôi giữ tới khi thiết bị đó thôi tin cậy.
    """

    def __init__(self, own_device_id: str) -> None:
        self._own_id = own_device_id
        self._trusted: dict[str, TrustedDevice] = {}
        self._seen: dict[str, DiscoveredDevice] = {}
        self._connected: set[str] = set()
        self._hello_os: dict[str, str] = {}  # device_id → `hello.os` đã xác thực (chỉ thiết bị tin cậy)

    def set_trusted(self, devices: Iterable[TrustedDevice]) -> None:
        """Thay toàn bộ tập thiết bị đã ghép đôi; thiết bị thôi tin cậy mất kết nối và `os` đã nhớ."""
        self._trusted = {d.device_id: d for d in devices if d.device_id != self._own_id}
        self._connected &= self._trusted.keys()
        self._hello_os = {k: v for k, v in self._hello_os.items() if k in self._trusted}

    def set_peer_hello(self, device_id: str, *, os: str) -> bool:
        """Nhớ `hello.os` của kết nối điều khiển đã xác thực; True nếu view đổi.

        Thiết bị chưa ghép đôi bị bỏ qua (False). `os` phải là giá trị đã qua
        `core.messages.validate_os` (`PeerInfo.hello` của `net` đã kiểm).
        """
        if device_id not in self._trusted:
            return False
        before = self.get(device_id)
        self._hello_os[device_id] = os
        return self.get(device_id) != before

    def seen(self, device: DiscoveredDevice) -> None:
        """mDNS thấy (hoặc thấy lại, đổi) một thiết bị (`DeviceAdded` / `DeviceUpdated`)."""
        if not isinstance(device, DiscoveredDevice):
            raise TypeError("device phải là DiscoveredDevice")
        if device.device_id != self._own_id:
            self._seen[device.device_id] = device

    def gone(self, device_id: str) -> None:
        """mDNS báo thiết bị biến mất (`DeviceRemoved`)."""
        self._seen.pop(device_id, None)

    def clear_seen(self) -> tuple[str, ...]:
        """Đánh dấu mọi thiết bị offline (khởi động lại mDNS); trả các id đã thấy, sắp tăng dần."""
        ids = tuple(sorted(self._seen))
        self._seen.clear()
        return ids

    def set_connected(self, device_id: str, connected: bool) -> None:
        """Có / không có kết nối điều khiển; thiết bị chưa ghép đôi bị bỏ qua."""
        if device_id not in self._trusted:
            return
        if connected:
            self._connected.add(device_id)
        else:
            self._connected.discard(device_id)

    def get(self, device_id: str) -> DeviceView | None:
        """View của thiết bị; None khi chưa ghép đôi và không thấy (hoặc là máy này)."""
        trusted = self._trusted.get(device_id)
        seen = self._seen.get(device_id)
        if trusted is not None:
            name = trusted.name
        elif seen is not None:
            name = seen.name
        else:
            return None
        os_name = self._hello_os.get(device_id) if trusted is not None else None
        if os_name is None and seen is not None:
            os_name = seen.os
        return DeviceView(
            device_id=device_id,
            name=name,
            os=os_name,
            paired=trusted is not None,
            online=seen is not None,
            connected=device_id in self._connected,
            addresses=seen.addresses if seen is not None else (),
            port=seen.port if seen is not None else None,
            last_address=trusted.last_address if trusted is not None else None,
        )

    def views(self) -> tuple[DeviceView, ...]:
        """Mọi view, sắp theo (NFC(name).casefold(), device_id)."""
        views = (self.get(device_id) for device_id in self._trusted.keys() | self._seen.keys())
        return tuple(sorted((v for v in views if v is not None), key=_sort_key))


# ---- Chuỗi người dùng nhập -----------------------------------------------------------------


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
    port = default_port if port_text is None else int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError(f"cổng phải trong 1–65535; {_ADDRESS_HINT}")
    try:
        # Cùng luật với địa chỉ đưa vào `core.links` (`hint`, địa chỉ đã lưu).
        return check_endpoint(ip_text, port)
    except ValueError as e:
        raise ValueError(f"{ip_text} không phải địa chỉ của một máy; {_ADDRESS_HINT}") from e


def _resolve[D: (TrustedDevice, DeviceView)](query: str, devices: Iterable[D]) -> D:
    """Luật chung của `resolve_trusted` và `resolve_device`."""
    if not isinstance(query, str):
        raise TypeError("query phải là chuỗi")
    known = list(devices)
    q = query.strip()
    if not q:
        raise DeviceNotFound(query, known)
    name_key = _nfc_fold(q)
    id_prefix = q.lower()
    by_id = len(q) >= MIN_ID_PREFIX and _HEX_RE.fullmatch(id_prefix) is not None
    matches: dict[str, D] = {}
    for device in known:
        if _nfc_fold(device.name) == name_key or (by_id and device.device_id.startswith(id_prefix)):
            matches[device.device_id] = device
    if not matches:
        raise DeviceNotFound(query, known)
    if len(matches) > 1:
        raise AmbiguousDevice(query, list(matches.values()))
    return next(iter(matches.values()))


def resolve_trusted(query: str, devices: Iterable[TrustedDevice]) -> TrustedDevice:
    """Tìm đúng một thiết bị tin cậy theo tên hoặc tiền tố `device_id`.

    Tên: trùng nguyên tên sau NFC + casefold (không khớp tiền tố tên). Mã: ít nhất
    MIN_ID_PREFIX ký tự hex (không phân biệt hoa thường) là tiền tố của `device_id`.
    Gộp hai cách khớp. Đúng một → trả về; không có (kể cả `query` rỗng) → DeviceNotFound;
    nhiều → AmbiguousDevice. `query` không phải chuỗi → TypeError.
    """
    return _resolve(query, devices)


def resolve_device(query: str, views: Iterable[DeviceView]) -> DeviceView:
    """Như `resolve_trusted`, trên các `DeviceView` (đã ghép đôi hoặc đang thấy qua mDNS).

    Khớp cả view offline: bên gọi tự quyết làm gì với view đó.
    """
    return _resolve(query, views)
