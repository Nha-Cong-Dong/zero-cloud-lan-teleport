"""Danh tính thiết bị: một khóa ECDSA P-256 và chứng chỉ tự ký.

Fingerprint = SHA-256 của chứng chỉ ở dạng DER. Đây là "số căn cước" của máy:
ghép đôi ở Giai đoạn 1 thực chất là hai máy lưu fingerprint của nhau.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CERT_FILE = "device.crt"
KEY_FILE = "device.key"
CN_PREFIX = "teleport-"


@dataclass(frozen=True)
class Identity:
    device_id: str
    cert_path: Path
    key_path: Path
    fingerprint: str  # 64 ký tự hex thường

    @property
    def short_fingerprint(self) -> str:
        """8 byte đầu, dạng "AB:CD:...": đủ ngắn để đọc và so bằng mắt."""
        return format_fingerprint(self.fingerprint[:16])


def fingerprint_of_der(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def format_fingerprint(hex_fp: str) -> str:
    hex_fp = hex_fp.upper()
    return ":".join(hex_fp[i : i + 2] for i in range(0, len(hex_fp), 2))


def load_or_create_identity(config_dir: Path) -> Identity:
    """Đọc danh tính trong `config_dir`, hoặc tạo mới nếu chưa có."""
    config_dir.mkdir(parents=True, exist_ok=True)
    cert_path = config_dir / CERT_FILE
    key_path = config_dir / KEY_FILE
    if not (cert_path.exists() and key_path.exists()):
        _create_identity(cert_path, key_path)

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    der = cert.public_bytes(serialization.Encoding.DER)
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    return Identity(
        device_id=str(cn).removeprefix(CN_PREFIX),
        cert_path=cert_path,
        key_path=key_path,
        fingerprint=fingerprint_of_der(der),
    )


def _create_identity(cert_path: Path, key_path: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CN_PREFIX + uuid.uuid4().hex)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)  # tự ký: người cấp chính là chủ thể
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )

    # Khóa riêng lưu dạng chưa mã hóa cho đơn giản; trên Linux/macOS chỉ chủ sở hữu đọc được.
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    if os.name == "posix":
        key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
