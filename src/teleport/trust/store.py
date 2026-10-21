"""Kho thiết bị đã ghép đôi: `<config_dir>/trusted.json`.

Định dạng file ở docs/architecture.md mục 4. Mỗi mục lưu chứng chỉ PEM của máy
kia; listener mTLS nạp các chứng chỉ này làm trust anchor (`cert_pems()`).

File được ghi nguyên tử bằng `teleport.atomic.write_atomic` (`trusted.json.tmp` →
fsync → `os.replace`), nên người đọc không bao giờ thấy file ghi dở. Khi đọc, mọi
mục được kiểm tra lại từ đầu; chỉ một chỗ sai là cả file bị từ chối
(`TrustStoreError`). File hỏng **không bao giờ** bị tự xóa hay tạo lại: người dùng
phải tự quyết định.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path

from teleport.atomic import write_atomic
from teleport.core.messages import MessageError, validate_device_id, validate_device_name
from teleport.paths import TRUSTED_FILE
from teleport.trust.identity import InvalidCertificate, PeerCert, peer_cert_from_pem

STORE_VERSION = 1


class TrustStoreError(Exception):
    """`trusted.json` hỏng, có phiên bản mới hơn, hoặc không ghi được."""


@dataclass(frozen=True)
class TrustedDevice:
    device_id: str
    name: str
    fingerprint: str  # 64 ký tự hex thường
    cert_pem: str  # PEM đã chuẩn hóa
    paired_at: datetime.datetime  # UTC, làm tròn xuống giây


class TrustStore:
    """Danh sách thiết bị đã ghép đôi, đồng bộ với `trusted.json`.

    Tạo bằng `TrustStore.load(config_dir)`. `add`/`remove` ghi xuống đĩa ngay; trạng
    thái trong bộ nhớ chỉ đổi khi ghi thành công.
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

        Tên sai hoặc `peer` không khớp chứng chỉ của nó → ValueError. Không ghi được
        → TrustStoreError.
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
        device = TrustedDevice(
            device_id=peer.device_id,
            name=name,
            fingerprint=peer.fingerprint,
            cert_pem=peer.pem,
            paired_at=now.astimezone(datetime.UTC).replace(microsecond=0),
        )
        devices = {
            k: d
            for k, d in self._devices.items()
            if d.device_id != device.device_id and d.fingerprint != device.fingerprint
        }
        devices[device.device_id] = device
        self._save(devices)
        return device

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
        data = {
            "version": STORE_VERSION,
            "devices": [
                {
                    "device_id": d.device_id,
                    "name": d.name,
                    "fingerprint": d.fingerprint,
                    "cert": d.cert_pem,
                    "paired_at": _format_time(d.paired_at),
                }
                for d in ordered
            ],
        }
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        try:
            write_atomic(self.path, text.encode("utf-8"))
        except OSError as e:
            raise TrustStoreError(f"không ghi được {self.path}: {e}") from e
        self._devices = dict(devices)


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
            device = _parse_device(entry)
        except TrustStoreError as e:
            raise TrustStoreError(f"mục {index}: {e}") from e
        if device.device_id in devices:
            raise TrustStoreError(f"mục {index}: trùng device_id {device.device_id}")
        if device.fingerprint in fingerprints:
            raise TrustStoreError(f"mục {index}: trùng fingerprint")
        devices[device.device_id] = device
        fingerprints.add(device.fingerprint)
    return devices


def _parse_device(entry: object) -> TrustedDevice:
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
    )
