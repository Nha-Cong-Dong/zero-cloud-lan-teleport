"""Giữ kết nối điều khiển tới các thiết bị tin cậy (sans-IO): chống trùng, nối lại, chặn.

Đặc tả ở docs/protocol.md mục 6.1 và 6.2. Module không mở socket, không xem đồng hồ:
tầng `app` đưa sự kiện (mDNS thấy/mất thiết bị, danh sách tin cậy đổi, kết quả một
lần mở kết nối, kết nối vào, kết nối mất, mạng đổi, hẹn giờ) cùng thời gian monotonic
`now` vào `LinkTracker`, rồi thực hiện các hành động nó trả về:

- `Connect`: mở kết nối điều khiển (luôn kiểm với chứng chỉ đã lưu trong `trusted.json`),
  rồi báo kết quả qua `connect_finished`;
- `CancelConnect`: hủy lần mở đang chạy (không báo `connect_finished` cho lần đó);
- `CloseConnection`: đóng một kết nối;
- `Notify`: báo người dùng (`LinkUp`, `LinkDown`, `LinkFailed`).

Luật chính:

- Tối đa một kết nối điều khiển cho mỗi thiết bị. Hai kết nối cùng sống: kết nối cũ
  đã mở ≥ `REPLACE_AFTER` giây, hoặc hai kết nối do cùng một bên mở → giữ kết nối mới;
  còn lại (hai bên gọi nhau cùng lúc) giữ kết nối do bên có `device_id` nhỏ hơn mở.
  Đổi kết nối không báo người dùng.
- Nối lại theo lịch 1, 2, 4… ≤ 30 s. Máy kia từ chối, danh tính không khớp hay khác
  phiên bản giao thức thì “chặn”: không thử lại theo lịch, chỉ khi có sự kiện mDNS
  hoặc mạng đổi (vẫn theo lịch tính từ lần thử trước), hoặc người dùng thao tác.
- Mọi lần thử do sự kiện cách lần trước ≥ `EVENT_RETRY_GAP` (kể cả khi bị chặn: không
  sớm hơn `max(EVENT_RETRY_GAP, lịch)`), nên kẻ giả mạo mDNS chỉ làm node thử với tốc
  độ có trần. Lịch 1, 2, 4… là lịch theo thời gian, không bị giới hạn này.
- Mỗi loại lỗi chỉ báo một lần cho tới khi kết nối được hoặc người dùng thao tác;
  “không kết nối được” chỉ báo sau `UNREACHABLE_REPORT_AFTER` lần thất bại liên tiếp.
- mDNS chỉ là gợi ý địa chỉ: thiết bị biến mất khỏi mDNS thì ngừng thử lại, nhưng
  kết nối đang sống không bị đóng.
"""

from __future__ import annotations

import ipaddress
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from teleport.core.messages import MessageError, validate_device_id

# Thời gian tính bằng giây; đọc lúc dùng để test rút ngắn được bằng monkeypatch.
# protocol §6.1: kết nối cũ đã mở ≥ 15 s thì kết nối mới thay. Phải lớn hơn hạn mở kết nối
# (net OPENING_TIMEOUT, protocol §2.3) cộng độ lệch đo giữa hai bên: một lần mở chậm của hai
# bên gọi nhau cùng lúc nếu bị coi là “nối lại” ở một bên thì hai bên đóng hai kết nối khác
# nhau, mất cả hai, và lặp lại mãi nếu lần mở chậm lặp lại.
REPLACE_AFTER = 15.0
RETRY_BASE = 1.0  # protocol §6.2: lịch 1, 2, 4… giây
RETRY_MAX = 30.0  # … tối đa 30 s
EVENT_RETRY_GAP = 2.0  # sự kiện mDNS/mạng: hai lần thử tới cùng máy cách nhau ≥ 2 s
UNREACHABLE_REPORT_AFTER = 3  # báo "không kết nối được" sau 3 lần thất bại liên tiếp

_MAX_ADDRESSES = 16  # như net.connection: số địa chỉ tối đa cho một lần mở kết nối
_MAX_DELAY_EXPONENT = 20  # chặn số mũ của lịch thử lại (2**20 · 1 s đã vượt xa RETRY_MAX)
_BROADCAST = ipaddress.IPv4Address("255.255.255.255")


class ConnectResult(StrEnum):
    """Kết quả một lần mở kết nối điều khiển (`app` ánh xạ từ ngoại lệ của `net`)."""

    OK = "ok"
    UNREACHABLE = "unreachable"  # net.Unreachable
    REJECTED = "rejected"  # net.Rejected
    IDENTITY_MISMATCH = "identity_mismatch"  # net.IdentityMismatch
    VERSION_MISMATCH = "version_mismatch"  # net.VersionMismatch


_BLOCKING = frozenset(
    {ConnectResult.REJECTED, ConnectResult.IDENTITY_MISMATCH, ConnectResult.VERSION_MISMATCH}
)


@dataclass(frozen=True, slots=True)
class Connect:
    """Mở kết nối điều khiển tới `addresses:port` (so fingerprint đã lưu), rồi `connect_finished`."""

    device_id: str
    attempt_id: int  # tracker cấp, tăng dần từ 1
    addresses: tuple[str, ...]
    port: int


@dataclass(frozen=True, slots=True)
class CancelConnect:
    """Hủy lần thử đang chạy; KHÔNG gọi `connect_finished` cho lần thử đó."""

    device_id: str
    attempt_id: int


@dataclass(frozen=True, slots=True)
class CloseConnection:
    """Đóng kết nối `conn_id` (lấy ra khỏi bảng ngay, đóng nền)."""

    device_id: str
    conn_id: int


@dataclass(frozen=True, slots=True)
class LinkUp:
    """Thiết bị chuyển từ 0 sang 1 kết nối điều khiển."""

    device_id: str
    conn_id: int
    outgoing: bool  # True: máy này mở


@dataclass(frozen=True, slots=True)
class LinkDown:
    """Thiết bị chuyển từ 1 về 0 kết nối (mất, hoặc thôi tin cậy)."""

    device_id: str
    conn_id: int


@dataclass(frozen=True, slots=True)
class LinkFailed:
    """Báo người dùng; `result` khác OK; mỗi loại tối đa một lần (xem `LinkTracker`)."""

    device_id: str
    result: ConnectResult


LinkEvent = LinkUp | LinkDown | LinkFailed


@dataclass(frozen=True, slots=True)
class Notify:
    """Báo người dùng một sự kiện kết nối."""

    event: LinkEvent


LinkAction = Connect | CancelConnect | CloseConnection | Notify


@dataclass(slots=True)
class _Conn:
    conn_id: int
    since: float  # `now` lúc kết nối thành kết nối hiện tại (đồng hồ của máy này)
    local: bool  # máy này mở


@dataclass(slots=True)
class _Device:
    """Trạng thái của một thiết bị (tin cậy, hoặc chưa tin cậy nhưng đang thấy qua mDNS)."""

    trusted: bool = False
    seen: tuple[tuple[str, ...], int] | None = None  # (địa chỉ, cổng) mDNS gần nhất
    current: _Conn | None = None
    attempt: int | None = None  # attempt_id đang chạy
    dirty: bool = False  # có sự kiện mDNS/mạng trong lúc đang thử
    failures: int = 0  # số lần thất bại liên tiếp
    blocked: bool = False  # máy kia từ chối / danh tính sai / khác phiên bản
    next_at: float | None = None  # lần thử đã hẹn
    last_start: float | None = None  # `now` của lần thử gần nhất
    reported: set[ConnectResult] = field(default_factory=set)  # loại lỗi đã báo

    def eligible(self) -> bool:
        """Đủ điều kiện thử: tin cậy, đang thấy, chưa kết nối, không đang thử."""
        return self.trusted and self.seen is not None and self.current is None and self.attempt is None


def _delay(failures: int) -> float:
    """Lịch thử lại: 1, 2, 4, 8, 16, 30, 30… giây (n ≥ 1)."""
    exponent = min(max(failures, 1) - 1, _MAX_DELAY_EXPONENT)
    return min(RETRY_BASE * 2**exponent, RETRY_MAX)


def _check_id_number(name: str, value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} phải là số nguyên ≥ 1")
    return value


def _check_port(port: object) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port phải là số nguyên 1–65535")
    return port


def _check_addresses(addresses: object) -> tuple[str, ...]:
    """1–16 IPv4 dạng chấm viết chuẩn, không phải 0.0.0.0, multicast hay broadcast; bỏ trùng."""
    if isinstance(addresses, str | bytes) or not isinstance(addresses, Sequence):
        raise ValueError("addresses phải là một dãy địa chỉ, không phải một chuỗi")
    items = list(addresses)
    if not 1 <= len(items) <= _MAX_ADDRESSES:
        raise ValueError(f"addresses phải có 1–{_MAX_ADDRESSES} địa chỉ")
    for value in items:
        if not isinstance(value, str):
            raise ValueError("địa chỉ phải là chuỗi IPv4 dạng chấm")
        try:
            ip = ipaddress.IPv4Address(value)
        except ValueError as e:
            raise ValueError(f"địa chỉ phải là IPv4 dạng chấm: {value!r}") from e
        if str(ip) != value:
            raise ValueError(f"địa chỉ phải là IPv4 dạng chấm: {value!r}")
        if ip.is_unspecified or ip.is_multicast or ip == _BROADCAST:
            raise ValueError(f"{value} không phải địa chỉ của một máy")
    return tuple(dict.fromkeys(items))


class LinkTracker:
    """Máy trạng thái giữ kết nối điều khiển của một node (sans-IO).

    Mọi phương thức kiểm tham số trước khi đổi trạng thái: sai → MessageError
    (`device_id`), ValueError / TypeError (còn lại), trạng thái giữ nguyên. `now` là
    thời gian monotonic (giây), không được giảm giữa các lời gọi. `device_id` của
    chính máy này bị bỏ qua (trả `[]`).

    Trong một lời gọi, hành động của mỗi thiết bị theo thứ tự `CloseConnection` /
    `CancelConnect`, rồi `Notify`, rồi `Connect`; lời gọi xét nhiều thiết bị duyệt
    theo `device_id` tăng dần.

    Tracker nhớ địa chỉ mDNS của mọi thiết bị đang thấy (cả chưa tin cậy, để thiết bị
    vừa ghép đôi mà đang thấy thì nối được ngay); bên gọi bảo đảm số thiết bị đang
    thấy có giới hạn.
    """

    def __init__(self, own_device_id: str) -> None:
        self._own_id = validate_device_id(own_device_id)
        self._devices: dict[str, _Device] = {}
        self._next_attempt = 1
        self._last_now: float | None = None

    # ---- Truy vấn ------------------------------------------------------------------------

    def connected(self) -> dict[str, int]:
        """Bản sao: device_id → conn_id của kết nối hiện tại."""
        return {d: dev.current.conn_id for d, dev in self._devices.items() if dev.current is not None}

    def attempting(self) -> dict[str, int]:
        """Bản sao: device_id → attempt_id của lần thử đang chạy."""
        return {d: dev.attempt for d, dev in self._devices.items() if dev.attempt is not None}

    def next_wakeup(self) -> float | None:
        """Thời điểm sớm nhất trong các lần thử đã hẹn (gọi `tick` lúc đó); None nếu không có."""
        times = [dev.next_at for dev in self._devices.values() if dev.next_at is not None]
        return min(times) if times else None

    # ---- Sự kiện -------------------------------------------------------------------------

    def trusted_changed(self, device_ids: Iterable[str], now: float) -> list[LinkAction]:
        """Thay toàn bộ tập thiết bị tin cậy (`trusted.json` vừa đổi).

        Thiết bị thôi tin cậy: đóng kết nối (kèm `LinkDown`), hủy lần thử, quên lịch và
        các lỗi đã báo. Thiết bị mới tin cậy: xóa bộ đếm, đang thấy thì thử ngay.
        """
        if isinstance(device_ids, str | bytes):
            raise MessageError("tập tin cậy phải là một tập device_id, không phải một chuỗi")
        ids = {validate_device_id(d) for d in device_ids}
        now = self._check_now(now)
        ids.discard(self._own_id)
        self._last_now = now
        actions: list[LinkAction] = []
        old = {d for d, dev in self._devices.items() if dev.trusted}
        for device_id in sorted(old | ids):
            if device_id in old and device_id not in ids:
                actions.extend(self._untrust(device_id))
            elif device_id in ids and device_id not in old:
                dev = self._devices.setdefault(device_id, _Device())
                dev.trusted = True
                actions.extend(self._user_retry(device_id, dev, now))
        return actions

    def device_seen(
        self, device_id: str, addresses: Sequence[str], port: int, now: float
    ) -> list[LinkAction]:
        """mDNS thấy (hoặc thấy lại, đổi địa chỉ) thiết bị: nhớ địa chỉ, thử nếu đủ điều kiện."""
        validate_device_id(device_id)
        addrs = _check_addresses(addresses)
        port = _check_port(port)
        now = self._check_now(now)
        self._last_now = now
        if device_id == self._own_id:
            return []
        dev = self._devices.setdefault(device_id, _Device())
        dev.seen = (addrs, port)
        if not dev.trusted:
            return []
        return self._event_retry(device_id, dev, now)

    def device_gone(self, device_id: str, now: float) -> list[LinkAction]:
        """mDNS báo thiết bị biến mất: ngừng thử lại; KHÔNG đóng kết nối, KHÔNG hủy lần thử."""
        dev = self._lookup(device_id, now)
        if dev is None:
            return []
        dev.seen = None
        dev.next_at = None
        dev.dirty = False
        if not dev.trusted:
            del self._devices[device_id]
        return []

    def network_changed(self, now: float) -> list[LinkAction]:
        """Mạng đổi hoặc máy vừa thức dậy: thử ngay (theo giới hạn tốc độ) mọi thiết bị chưa nối."""
        now = self._check_now(now)
        self._last_now = now
        actions: list[LinkAction] = []
        for device_id in sorted(self._devices):
            dev = self._devices[device_id]
            if dev.trusted:
                actions.extend(self._event_retry(device_id, dev, now))
        return actions

    def retry_now(self, device_id: str, now: float) -> list[LinkAction]:
        """Người dùng thao tác (ví dụ vừa ghép đôi lại): gỡ chặn, xóa bộ đếm, thử ngay."""
        dev = self._lookup(device_id, now)
        if dev is None or not dev.trusted:
            return []
        return self._user_retry(device_id, dev, now)

    def connect_finished(
        self, device_id: str, attempt_id: int, result: ConnectResult, conn_id: int | None, now: float
    ) -> list[LinkAction]:
        """Kết quả lần thử `attempt_id`; `conn_id` (do `app` cấp) chỉ có khi `OK`."""
        validate_device_id(device_id)
        _check_id_number("attempt_id", attempt_id)
        result = ConnectResult(result)
        if result is ConnectResult.OK:
            if conn_id is None:
                raise ValueError("kết quả OK phải có conn_id")
            self._check_new_conn_id(conn_id)
        elif conn_id is not None:
            raise ValueError("chỉ kết quả OK mới có conn_id")
        now = self._check_now(now)
        self._last_now = now
        if device_id == self._own_id:
            return []
        dev = self._devices.get(device_id)
        if dev is None or dev.attempt != attempt_id:
            # Lần thử cũ, đã hủy, hoặc thiết bị đã thôi tin cậy: kết nối hoàn tất muộn bị đóng.
            return [CloseConnection(device_id, conn_id)] if conn_id is not None else []
        dev.attempt = None
        if result is ConnectResult.OK:
            assert conn_id is not None
            self._reset_failures(dev)
            dev.dirty = False
            if not dev.trusted:
                return [CloseConnection(device_id, conn_id)]
            return self._add_connection(device_id, dev, conn_id, True, now)
        return self._failed(device_id, dev, result, now)

    def incoming_control(self, device_id: str, conn_id: int, now: float) -> list[LinkAction]:
        """Máy kia vừa mở kết nối điều khiển tới máy này (đã qua `control_open`)."""
        validate_device_id(device_id)
        self._check_new_conn_id(conn_id)
        now = self._check_now(now)
        self._last_now = now
        if device_id == self._own_id:
            return []
        dev = self._devices.get(device_id)
        if dev is None or not dev.trusted:
            return [CloseConnection(device_id, conn_id)]  # phòng thủ: Listener đã chặn
        return self._add_connection(device_id, dev, conn_id, False, now)

    def connection_lost(self, device_id: str, conn_id: int, now: float) -> list[LinkAction]:
        """Kết nối `conn_id` đã đóng; là kết nối hiện tại thì báo `LinkDown` và hẹn thử lại."""
        validate_device_id(device_id)
        _check_id_number("conn_id", conn_id)
        now = self._check_now(now)
        self._last_now = now
        if device_id == self._own_id:
            return []
        dev = self._devices.get(device_id)
        if dev is None or dev.current is None or dev.current.conn_id != conn_id:
            return []
        dev.current = None
        if dev.trusted and dev.seen is not None and not dev.blocked:
            dev.next_at = now + _delay(max(dev.failures, 1))
        return [Notify(LinkDown(device_id, conn_id))]

    def tick(self, now: float) -> list[LinkAction]:
        """Hẹn giờ: bắt đầu các lần thử đã tới hạn (`next_wakeup`)."""
        now = self._check_now(now)
        self._last_now = now
        actions: list[LinkAction] = []
        for device_id in sorted(self._devices):
            dev = self._devices[device_id]
            if dev.next_at is not None and dev.next_at <= now:
                dev.next_at = None
                if dev.eligible():
                    actions.append(self._start(device_id, dev, now))
        return actions

    # ---- Nội bộ --------------------------------------------------------------------------

    def _check_now(self, now: object) -> float:
        if isinstance(now, bool) or not isinstance(now, int | float):
            raise TypeError("now phải là số giây (đồng hồ monotonic)")
        if not math.isfinite(now):
            raise ValueError("now phải là số hữu hạn")
        if self._last_now is not None and now < self._last_now:
            raise ValueError("now không được giảm")
        return float(now)

    def _check_new_conn_id(self, conn_id: object) -> int:
        conn_id = _check_id_number("conn_id", conn_id)
        for dev in self._devices.values():
            if dev.current is not None and dev.current.conn_id == conn_id:
                raise ValueError(f"conn_id {conn_id} đang là kết nối hiện tại của một thiết bị")
        return conn_id

    def _lookup(self, device_id: str, now: float) -> _Device | None:
        """Kiểm tham số chung (`device_id`, `now`); trả mục của thiết bị (None: máy này / chưa biết)."""
        validate_device_id(device_id)
        now = self._check_now(now)
        self._last_now = now
        if device_id == self._own_id:
            return None
        return self._devices.get(device_id)

    def _start(self, device_id: str, dev: _Device, now: float) -> Connect:
        assert dev.seen is not None
        attempt_id = self._next_attempt
        self._next_attempt += 1
        dev.attempt = attempt_id
        dev.last_start = now
        dev.next_at = None
        dev.dirty = False
        addresses, port = dev.seen
        return Connect(device_id, attempt_id, addresses, port)

    @staticmethod
    def _reset_failures(dev: _Device) -> None:
        dev.failures = 0
        dev.blocked = False
        dev.reported.clear()
        dev.next_at = None

    def _event_retry(self, device_id: str, dev: _Device, now: float) -> list[LinkAction]:
        """Thử do sự kiện (mDNS, mạng đổi), có giới hạn tốc độ."""
        if dev.eligible():
            if dev.last_start is None:
                earliest = now
            elif dev.blocked:
                # Theo lịch tính từ lần thử trước, nhưng không sớm hơn EVENT_RETRY_GAP.
                earliest = dev.last_start + max(EVENT_RETRY_GAP, _delay(dev.failures))
            else:
                earliest = dev.last_start + EVENT_RETRY_GAP
            if now >= earliest:
                return [self._start(device_id, dev, now)]
            dev.next_at = earliest if dev.next_at is None else min(dev.next_at, earliest)
        elif dev.attempt is not None and dev.seen is not None:
            dev.dirty = True
        return []

    def _user_retry(self, device_id: str, dev: _Device, now: float) -> list[LinkAction]:
        """Thử do người dùng hoặc vì vừa được tin cậy: gỡ chặn, không xét khoảng cách."""
        dev.failures = 0
        dev.blocked = False
        dev.reported.clear()
        if dev.eligible():
            return [self._start(device_id, dev, now)]
        return []

    def _failed(self, device_id: str, dev: _Device, result: ConnectResult, now: float) -> list[LinkAction]:
        """Lần thử hiện tại thất bại (`dev.attempt` đã được xóa)."""
        if dev.current is not None:
            # Máy kia đã nối tới trong lúc thử: không tính là thất bại.
            dev.dirty = False
            return []
        dev.failures += 1
        actions: list[LinkAction] = []
        if result is ConnectResult.UNREACHABLE:
            dev.blocked = False
            if dev.seen is not None:
                dev.next_at = now + _delay(dev.failures)
                if dev.dirty and dev.last_start is not None:
                    dev.next_at = min(dev.next_at, max(now, dev.last_start + EVENT_RETRY_GAP))
                if dev.failures >= UNREACHABLE_REPORT_AFTER and result not in dev.reported:
                    dev.reported.add(result)
                    actions.append(Notify(LinkFailed(device_id, result)))
            dev.dirty = False
            return actions
        assert result in _BLOCKING
        dev.blocked = True
        dev.next_at = None
        if result not in dev.reported:
            dev.reported.add(result)
            actions.append(Notify(LinkFailed(device_id, result)))
        if dev.dirty:
            dev.dirty = False
            actions.extend(self._event_retry(device_id, dev, now))
        return actions

    def _add_connection(
        self, device_id: str, dev: _Device, conn_id: int, local: bool, now: float
    ) -> list[LinkAction]:
        """Một kết nối mới đã sống (ra hoặc vào); áp luật chống trùng nếu đã có kết nối."""
        old = dev.current
        if old is None:
            dev.current = _Conn(conn_id, now, local)
            self._reset_failures(dev)
            return [Notify(LinkUp(device_id, conn_id, local))]
        if now - old.since >= REPLACE_AFTER or old.local == local:
            keep_new = True
        else:
            # Hai bên gọi nhau cùng lúc: giữ kết nối do bên có device_id nhỏ hơn mở.
            keep_local = self._own_id < device_id
            keep_new = local == keep_local
        if keep_new:
            dev.current = _Conn(conn_id, now, local)
            return [CloseConnection(device_id, old.conn_id)]
        return [CloseConnection(device_id, conn_id)]

    def _untrust(self, device_id: str) -> list[LinkAction]:
        """Thiết bị thôi tin cậy: đóng kết nối, hủy lần thử, quên mọi thứ trừ địa chỉ mDNS."""
        dev = self._devices[device_id]
        actions: list[LinkAction] = []
        notify: list[LinkAction] = []
        if dev.current is not None:
            actions.append(CloseConnection(device_id, dev.current.conn_id))
            notify.append(Notify(LinkDown(device_id, dev.current.conn_id)))
        if dev.attempt is not None:
            actions.append(CancelConnect(device_id, dev.attempt))
        if dev.seen is not None:
            self._devices[device_id] = _Device(seen=dev.seen)
        else:
            del self._devices[device_id]
        return actions + notify
