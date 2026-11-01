"""Kho thiết bị đã ghép đôi: `<config_dir>/trusted.json`.

Định dạng file ở docs/architecture.md mục 4. Mỗi mục lưu chứng chỉ PEM của máy
kia; listener mTLS nạp các chứng chỉ này làm trust anchor (`cert_pems()`).

File được ghi nguyên tử bằng `teleport.atomic.write_atomic` (`trusted.json.tmp` →
fsync → `os.replace`), nên người đọc không bao giờ thấy file ghi dở. Khi đọc, mọi
mục được kiểm tra lại từ đầu; chỉ một chỗ sai là cả file bị từ chối
(`TrustStoreError`). Ngoại lệ duy nhất: trường tùy chọn `last_address` (địa chỉ của kết nối
điều khiển ra gần nhất, chỉ là gợi ý để nối lại) sai thì chỉ bỏ riêng trường đó (WARNING có
chỉ số mục, không in giá trị); lần ghi sau ghi lại theo RAM nên tự làm sạch. File hỏng
**không bao giờ** bị tự xóa hay tạo lại: người dùng phải tự quyết định.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from teleport.atomic import write_atomic
from teleport.core.links import check_endpoint
from teleport.core.messages import MessageError, validate_device_id, validate_device_name
from teleport.paths import TRUSTED_FILE
from teleport.trust.identity import InvalidCertificate, PeerCert, peer_cert_from_pem

STORE_VERSION = 1

log = logging.getLogger(__name__)


class TrustStoreError(Exception):
    """`trusted.json` hỏng, có phiên bản mới hơn, hoặc không ghi được."""


@dataclass(frozen=True)
class TrustedDevice:
    device_id: str
    name: str
    fingerprint: str  # 64 ký tự hex thường
    cert_pem: str  # PEM đã chuẩn hóa
    paired_at: datetime.datetime  # UTC, làm tròn xuống giây
    last_address: tuple[str, int] | None = None  # (IPv4, cổng) của kết nối điều khiển ra gần nhất


class TrustStore:
    """Danh sách thiết bị đã ghép đôi, đồng bộ với `trusted.json`.

    Tạo bằng `TrustStore.load(config_dir)`. `add`/`rename`/`remove`/`set_last_address` ghi xuống
    đĩa ngay; trạng thái trong bộ nhớ chỉ đổi khi ghi thành công.
    """

    def __init__(self, path: Path, *, _devices: dict[str, TrustedDevice]) -> None:
        """Nội bộ: chỉ dùng `TrustStore.load()`. `_devices` phải là dữ liệu đã kiểm tra."""
        self.path = path
        self._devices = dict(_devices)  # device_id → TrustedDevice

    @classmethod
    def load(cls, config_dir: Path) -> TrustStore:
        """Đọc `trusted.json` trong `config_dir`. Chưa có file → kho rỗng (không tạo file)."""
        path = config_dir / TRUSTED_FILE
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return cls(path, _devices={})
        except OSError as e:
            raise TrustStoreError(f"không đọc được {path}: {e}") from e
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, RecursionError) as e:
            raise TrustStoreError(f"{path} không phải JSON UTF-8 hợp lệ") from e
        try:
            devices = _parse_store(data)
        except TrustStoreError as e:
            raise TrustStoreError(f"{path}: {e}") from e
        return cls(path, _devices=devices)

    def devices(self) -> list[TrustedDevice]:
        """Mọi thiết bị, sắp theo tên (không phân biệt hoa thường) rồi theo device_id."""
        return sorted(self._devices.values(), key=lambda d: (d.name.casefold(), d.device_id))

    def get(self, device_id: str) -> TrustedDevice | None:
        if not isinstance(device_id, str):
            return None
        return self._devices.get(device_id)

    def by_fingerprint(self, fingerprint: str) -> TrustedDevice | None:
        for device in self._devices.values():
            if device.fingerprint == fingerprint:
                return device
        return None

    def find_by_name(self, name: str) -> list[TrustedDevice]:
        """Các thiết bị có tên trùng nguyên `name`, không phân biệt hoa thường."""
        if not isinstance(name, str):
            return []
        key = name.casefold()
        return [d for d in self.devices() if d.name.casefold() == key]

    def is_trusted(self, fingerprint: str) -> bool:
        return self.by_fingerprint(fingerprint) is not None

    def cert_pems(self) -> list[str]:
        """Chứng chỉ PEM của mọi thiết bị: đưa thẳng vào `server_context(identity, ...)`."""
        return [d.cert_pem for d in self.devices()]

    def add(self, peer: PeerCert, name: str, *, now: datetime.datetime | None = None) -> TrustedDevice:
        """Lưu thiết bị vừa ghép đôi, thay mục cũ trùng `device_id` hoặc `fingerprint`.

        Mục cũ cùng `device_id` và cùng `fingerprint` (ghép đôi lại cùng danh tính) thì giữ
        `last_address` của nó; còn lại `last_address` là None. Tên sai hoặc `peer` không khớp
        chứng chỉ của nó → ValueError. Không ghi được → TrustStoreError.
        """
        validate_device_name(name)
        # PeerCert có thể được tạo bằng tay: kiểm lại để không ghi ra một file mà
        # lần đọc sau sẽ từ chối.
        if peer_cert_from_pem(peer.pem) != peer:
            raise ValueError("device_id/fingerprint không khớp với chứng chỉ")
        if now is None:
            now = datetime.datetime.now(datetime.UTC)
        elif now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now phải có múi giờ")
        old = self._devices.get(peer.device_id)
        keep_address = old is not None and old.fingerprint == peer.fingerprint
        device = TrustedDevice(
            device_id=peer.device_id,
            name=name,
            fingerprint=peer.fingerprint,
            cert_pem=peer.pem,
            paired_at=now.astimezone(datetime.UTC).replace(microsecond=0),
            last_address=old.last_address if old is not None and keep_address else None,
        )
        devices = {
            k: d
            for k, d in self._devices.items()
            if d.device_id != device.device_id and d.fingerprint != device.fingerprint
        }
        devices[device.device_id] = device
        self._save(devices)
        return device

    def rename(self, device_id: str, name: str) -> TrustedDevice:
        """Đổi tên đã lưu của một thiết bị và ghi xuống đĩa; giữ chứng chỉ, fingerprint, `paired_at`.

        Tên sai → MessageError; không có `device_id` → KeyError; không ghi được →
        TrustStoreError (RAM không đổi). Tên trùng nguyên tên đang lưu → trả mục cũ, không ghi.
        Tên trùng tên một thiết bị khác vẫn được (người dùng phân biệt bằng mã thiết bị).
        """
        validate_device_name(name)
        device = self.get(device_id)
        if device is None:
            raise KeyError(device_id)
        if device.name == name:
            return device
        renamed = dataclasses.replace(device, name=name)
        devices = dict(self._devices)
        devices[device.device_id] = renamed
        self._save(devices)
        return renamed

    def set_last_address(self, device_id: str, ip: str, port: int) -> TrustedDevice:
        """Ghi địa chỉ (đã quan sát, đã xác thực) của thiết bị; giữ mọi trường khác.

        Sai (`check_endpoint`) → ValueError; không có `device_id` → KeyError; không ghi được →
        TrustStoreError (RAM không đổi). Trùng giá trị đang lưu → trả mục cũ, không ghi.
        """
        address = check_endpoint(ip, port)
        device = self.get(device_id)
        if device is None:
            raise KeyError(device_id)
        if device.last_address == address:
            return device
        updated = dataclasses.replace(device, last_address=address)
        devices = dict(self._devices)
        devices[device.device_id] = updated
        self._save(devices)
        return updated

    def remove(self, device_id: str) -> TrustedDevice:
        """Xóa một thiết bị và ghi xuống đĩa. Không có → KeyError."""
        device = self.get(device_id)
        if device is None:
            raise KeyError(device_id)
        devices = {k: d for k, d in self._devices.items() if k != device_id}
        self._save(devices)
        return device

    def _save(self, devices: dict[str, TrustedDevice]) -> None:
        ordered = sorted(devices.values(), key=lambda d: (d.name.casefold(), d.device_id))
        data = {"version": STORE_VERSION, "devices": [_device_entry(d) for d in ordered]}
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        try:
            write_atomic(self.path, text.encode("utf-8"))
        except OSError as e:
            raise TrustStoreError(f"không ghi được {self.path}: {e}") from e
        self._devices = dict(devices)


def _device_entry(d: TrustedDevice) -> dict[str, object]:
    """Một mục của `trusted.json`; `last_address` chỉ có khóa khi khác None (không ghi `null`)."""
    entry: dict[str, object] = {
        "device_id": d.device_id,
        "name": d.name,
        "fingerprint": d.fingerprint,
        "cert": d.cert_pem,
        "paired_at": _format_time(d.paired_at),
    }
    if d.last_address is not None:
        entry["last_address"] = {"ip": d.last_address[0], "port": d.last_address[1]}
    return entry


def _format_time(moment: datetime.datetime) -> str:
    """Thời điểm UTC dạng `YYYY-MM-DDTHH:MM:SSZ`."""
    return moment.astimezone(datetime.UTC).isoformat(timespec="seconds").removesuffix("+00:00") + "Z"


def _parse_store(data: object) -> dict[str, TrustedDevice]:
    if not isinstance(data, dict):
        raise TrustStoreError("nội dung phải là một JSON object")
    version = data.get("version")
    if type(version) is not int or version < 1:
        raise TrustStoreError("thiếu hoặc sai trường version")
    if version > STORE_VERSION:
        raise TrustStoreError(f"phiên bản {version} mới hơn bản app này hỗ trợ ({STORE_VERSION})")
    entries = data.get("devices")
    if not isinstance(entries, list):
        raise TrustStoreError("trường devices phải là một danh sách")

    devices: dict[str, TrustedDevice] = {}
    fingerprints: set[str] = set()
    for index, entry in enumerate(entries):
        try:
            device = _parse_device(entry, index)
        except TrustStoreError as e:
            raise TrustStoreError(f"mục {index}: {e}") from e
        if device.device_id in devices:
            raise TrustStoreError(f"mục {index}: trùng device_id {device.device_id}")
        if device.fingerprint in fingerprints:
            raise TrustStoreError(f"mục {index}: trùng fingerprint")
        devices[device.device_id] = device
        fingerprints.add(device.fingerprint)
    return devices


def _parse_last_address(value: object, index: int) -> tuple[str, int] | None:
    """Trường tùy chọn `last_address`: thiếu / `null` → None; sai → None kèm WARNING (không in giá trị).

    Khoan dung riêng cho trường này: nó chỉ là gợi ý để nối lại, nên một giá trị hỏng không làm cả
    file bị từ chối. Khóa lạ bên trong object bị bỏ qua.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        try:
            return check_endpoint(value.get("ip"), value.get("port"))
        except ValueError:
            pass
    log.warning("trusted.json: mục %d: bỏ last_address không hợp lệ", index)
    return None


def _parse_device(entry: object, index: int) -> TrustedDevice:
    if not isinstance(entry, dict):
        raise TrustStoreError("mỗi mục phải là một JSON object")
    values = {}
    for key in ("device_id", "name", "fingerprint", "cert", "paired_at"):
        value = entry.get(key)
        if not isinstance(value, str):
            raise TrustStoreError(f"thiếu hoặc sai kiểu trường {key}")
        values[key] = value
    try:
        validate_device_id(values["device_id"])
        validate_device_name(values["name"])
    except MessageError as e:
        raise TrustStoreError(str(e)) from e
    try:
        peer = peer_cert_from_pem(values["cert"])
    except InvalidCertificate as e:
        raise TrustStoreError(f"chứng chỉ không hợp lệ: {e}") from e
    if peer.device_id != values["device_id"]:
        raise TrustStoreError("device_id không khớp CN của chứng chỉ")
    if peer.fingerprint != values["fingerprint"]:
        raise TrustStoreError("fingerprint không khớp chứng chỉ")
    try:
        paired_at = datetime.datetime.fromisoformat(values["paired_at"])
        if paired_at.tzinfo is None or paired_at.utcoffset() is None:
            raise TrustStoreError("paired_at phải có múi giờ")
        paired_at = paired_at.astimezone(datetime.UTC).replace(microsecond=0)
    except (ValueError, OverflowError) as e:
        raise TrustStoreError("paired_at không phải thời điểm ISO 8601 hợp lệ") from e
    return TrustedDevice(
        device_id=peer.device_id,
        name=values["name"],
        fingerprint=peer.fingerprint,
        cert_pem=peer.pem,
        paired_at=paired_at,
        last_address=_parse_last_address(entry.get("last_address"), index),
    )
