"""Fixture dùng chung cho test tầng trust: tự tạo chứng chỉ (tốt và xấu) bằng `cryptography`."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

CertFactory = Callable[..., x509.Certificate]


def random_device_id() -> str:
    return uuid.uuid4().hex


def build_cert(
    *,
    cn: str | None = None,
    extra_cn: str | None = None,
    key=None,
    signing_key=None,
    issuer_cn: str | None = None,
    hash_alg: hashes.HashAlgorithm | None = None,
    ca: bool | None = None,
    san_dns: list[str] | None = None,
    extensions: list[tuple[x509.ExtensionType, bool]] | None = None,
) -> x509.Certificate:
    """Tạo một chứng chỉ; mặc định giống hệt cert của Teleport (P-256, ECDSA-SHA256, tự ký).

    Mỗi tham số cho phép làm hỏng đúng một khía cạnh để kiểm tra việc từ chối.
    """
    if cn is None:
        cn = "teleport-" + random_device_id()
    if key is None:
        key = ec.generate_private_key(ec.SECP256R1())
    if signing_key is None:
        signing_key = key
    if hash_alg is None:
        hash_alg = hashes.SHA256()

    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, cn)]
    if extra_cn is not None:
        attrs.append(x509.NameAttribute(NameOID.COMMON_NAME, extra_cn))
    subject = x509.Name(attrs)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)]) if issuer_cn else subject

    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
    )
    if ca is not None:
        builder = builder.add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    if san_dns:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(d) for d in san_dns]), critical=False
        )
    for ext, critical in extensions or []:
        builder = builder.add_extension(ext, critical=critical)
    return builder.sign(signing_key, hash_alg)


@pytest.fixture
def make_cert() -> CertFactory:
    return build_cert


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)
