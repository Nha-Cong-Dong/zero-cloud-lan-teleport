"""Danh tính thiết bị: một khóa ECDSA P-256 và chứng chỉ tự ký.

Fingerprint = SHA-256 của chứng chỉ ở dạng DER. Đây là "số căn cước" của máy:
ghép đôi thực chất là hai máy lưu chứng chỉ (và fingerprint) của nhau.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import string
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, SignatureAlgorithmOID

CERT_FILE = "device.crt"
KEY_FILE = "device.key"
CN_PREFIX = "teleport-"

# Giới hạn độ dài PEM của chứng chỉ nhận từ máy khác (bằng MAX_CERT_PEM_LEN ở core.messages).
MAX_PEM_LEN = 8192
PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
PEM_END = "-----END CERTIFICATE-----"

# Extension cũ của Netscape đánh dấu loại chứng chỉ (có bit "SSL CA"); OpenSSL vẫn đọc nó.
_NETSCAPE_CERT_TYPE = x509.ObjectIdentifier("2.16.840.1.113730.1.1")

_CN_RE = re.compile(re.escape(CN_PREFIX) + r"[0-9a-f]{32}")


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

    def cert_pem(self) -> str:
        """Chứng chỉ dạng PEM: thứ được trao cho thiết bị khác khi ghép đôi."""
        return self.cert_path.read_text(encoding="ascii")


def fingerprint_of_der(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def format_fingerprint(hex_fp: str) -> str:
    hex_fp = hex_fp.upper()
    return ":".join(hex_fp[i : i + 2] for i in range(0, len(hex_fp), 2))


class InvalidCertificate(ValueError):
    """Chứng chỉ của máy khác không hợp lệ hoặc không phải chứng chỉ thiết bị Teleport."""


@dataclass(frozen=True)
class PeerCert:
    """Chứng chỉ của một máy khác, đã được kiểm tra."""

    device_id: str
    fingerprint: str  # 64 ký tự hex thường
    pem: str  # PEM đã chuẩn hóa (serialize lại từ chứng chỉ đã phân tích)


def peer_cert_from_pem(pem: str) -> PeerCert:
    """Phân tích và kiểm tra chứng chỉ PEM nhận từ máy khác (ví dụ trong `pair_request`).

    Chỉ nhận chuỗi ASCII ≤ 8192 ký tự chứa đúng một khối CERTIFICATE; ngoài khối
    chỉ được có khoảng trắng. Sai bất kỳ điều kiện nào → InvalidCertificate.
    """
    if not isinstance(pem, str):
        raise InvalidCertificate("chứng chỉ PEM phải là chuỗi")
    if len(pem) > MAX_PEM_LEN:
        raise InvalidCertificate(f"chứng chỉ PEM dài quá {MAX_PEM_LEN} ký tự")
    if not pem.isascii():
        raise InvalidCertificate("chứng chỉ PEM chỉ được chứa ký tự ASCII")
    body = pem.strip(string.whitespace)
    if not (
        body.startswith(PEM_BEGIN)
        and body.endswith(PEM_END)
        and body.count("-----BEGIN") == 1
        and body.count("-----END") == 1
    ):
        raise InvalidCertificate("cần đúng một khối CERTIFICATE, ngoài khối chỉ được có khoảng trắng")
    try:
        cert = x509.load_pem_x509_certificate(body.encode("ascii"))
    except ValueError as e:
        raise InvalidCertificate("không phân tích được chứng chỉ PEM") from e
    return _peer_cert(cert)


def peer_cert_from_der(der: bytes) -> PeerCert:
    """Như `peer_cert_from_pem`, cho chứng chỉ DER (ví dụ lấy từ bắt tay TLS)."""
    if not isinstance(der, bytes):
        raise InvalidCertificate("chứng chỉ DER phải là bytes")
    if len(der) > MAX_PEM_LEN:
        raise InvalidCertificate(f"chứng chỉ DER dài quá {MAX_PEM_LEN} byte")
    try:
        cert = x509.load_der_x509_certificate(der)
    except ValueError as e:
        raise InvalidCertificate("không phân tích được chứng chỉ DER") from e
    return _peer_cert(cert)


def _reject_ca_markers(cert: x509.Certificate) -> None:
    """Từ chối mọi dấu hiệu chứng chỉ CA.

    Chứng chỉ tin cậy được nạp làm trust anchor cho mTLS. OpenSSL coi một chứng chỉ
    là CA không chỉ qua BasicConstraints CA=true mà cả qua KeyUsage (keyCertSign,
    cRLSign) hay Netscape Cert Type; nhận chúng thì chứng chỉ đó có thể ký ra chứng
    chỉ giả cho thiết bị khác. Chứng chỉ do `_create_identity` tạo không có extension.
    """
    extensions = cert.extensions
    try:
        constraints = extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        pass
    else:
        if constraints.ca:
            raise InvalidCertificate("không nhận chứng chỉ CA (BasicConstraints CA=true)")
    try:
        usage = extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        pass
    else:
        if usage.key_cert_sign or usage.crl_sign:
            raise InvalidCertificate("không nhận chứng chỉ CA (KeyUsage keyCertSign/cRLSign)")
    try:
        extensions.get_extension_for_oid(_NETSCAPE_CERT_TYPE)
    except x509.ExtensionNotFound:
        pass
    else:
        raise InvalidCertificate("không nhận chứng chỉ có extension Netscape Cert Type")


def _peer_cert(cert: x509.Certificate) -> PeerCert:
    """Kiểm tra chứng chỉ đúng dạng một thiết bị Teleport (như `_create_identity` tạo ra)."""
    try:
        if cert.version != x509.Version.v3:
            raise InvalidCertificate("chứng chỉ phải là X.509 v3")
        cns = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if len(cns) != 1:
            raise InvalidCertificate("subject phải có đúng một CN")
        cn = cns[0].value
        if not isinstance(cn, str) or not _CN_RE.fullmatch(cn):
            raise InvalidCertificate("CN phải có dạng teleport-<32 ký tự hex thường>")
        if cert.issuer != cert.subject:
            raise InvalidCertificate("chứng chỉ phải tự ký (issuer trùng subject)")
        if cert.signature_algorithm_oid != SignatureAlgorithmOID.ECDSA_WITH_SHA256:
            raise InvalidCertificate("chữ ký phải là ECDSA-SHA256")
        key = cert.public_key()
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise InvalidCertificate("khóa phải là ECDSA P-256")
        _reject_ca_markers(cert)
        cert.verify_directly_issued_by(cert)
        der = cert.public_bytes(serialization.Encoding.DER)
        pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    except InvalidCertificate:
        raise
    except (
        ValueError,
        TypeError,
        InvalidSignature,
        UnsupportedAlgorithm,
        x509.InvalidVersion,
        x509.DuplicateExtension,
    ) as e:
        raise InvalidCertificate("chứng chỉ không hợp lệ hoặc chữ ký tự ký sai") from e
    if len(pem) > MAX_PEM_LEN:
        raise InvalidCertificate(f"chứng chỉ PEM dài quá {MAX_PEM_LEN} ký tự")
    return PeerCert(device_id=cn.removeprefix(CN_PREFIX), fingerprint=fingerprint_of_der(der), pem=pem)


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
