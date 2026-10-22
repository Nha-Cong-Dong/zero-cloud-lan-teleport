"""Fixture dùng chung cho test tầng trust: tự tạo chứng chỉ (tốt và xấu) bằng `cryptography`."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtensionOID, NameOID

CertFactory = Callable[..., x509.Certificate]

# Extension chứa GeneralName mà `cryptography` không hỗ trợ: x400Address ([3]) hoặc EDIPartyName
# ([5]). Đọc `cert.extensions` ném UnsupportedGeneralNameType, lớp lỗi kế thừa thẳng từ Exception
# chứ không từ ValueError (đợt kiểm tra M1). Giá trị là DER thô của extension, dựng tay vì
# `cryptography` không tạo được hai loại tên này.
UNSUPPORTED_GENERAL_NAME_EXTENSIONS: dict[str, tuple[x509.ObjectIdentifier, bytes]] = {
    # Hai mẫu gợi ý trong kế hoạch: SEQUENCE { [3] rỗng } và SEQUENCE { [5] rỗng }.
    "san-x400": (ExtensionOID.SUBJECT_ALTERNATIVE_NAME, b"\x30\x02\xa3\x00"),
    "san-edi-empty": (ExtensionOID.SUBJECT_ALTERNATIVE_NAME, b"\x30\x02\xa5\x00"),
    # EDIPartyName đúng dạng (partyName "ab"): bản này OpenSSL cũng nạp được.
    "san-edi": (ExtensionOID.SUBJECT_ALTERNATIVE_NAME, bytes.fromhex("3008a506a1040c026162")),
    # Tên hợp lệ (dNSName "abcd") đứng trước, tên lạ đứng sau.
    "san-dns-then-x400": (ExtensionOID.SUBJECT_ALTERNATIVE_NAME, b"\x30\x08\x82\x04abcd\xa3\x00"),
    # Cùng loại tên lạ, nằm trong các extension khác cũng chứa GeneralName.
    "ian-x400": (ExtensionOID.ISSUER_ALTERNATIVE_NAME, b"\x30\x02\xa3\x00"),
    "aki-issuer-x400": (ExtensionOID.AUTHORITY_KEY_IDENTIFIER, bytes.fromhex("3007a102a300820101")),
    "name-constraints-x400": (ExtensionOID.NAME_CONSTRAINTS, bytes.fromhex("3006a0043002a300")),
    "crldp-x400": (ExtensionOID.CRL_DISTRIBUTION_POINTS, bytes.fromhex("30083006a004a002a300")),
    "aia-x400": (
        ExtensionOID.AUTHORITY_INFORMATION_ACCESS,
        bytes.fromhex("300e300c06082b06010505073002a300"),
    ),
}


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


@pytest.fixture(params=list(UNSUPPORTED_GENERAL_NAME_EXTENSIONS))
def unsupported_name_ext(request) -> x509.UnrecognizedExtension:
    """Một extension (không critical) chứa GeneralName lạ; test dùng nó chạy lại cho từng mẫu."""
    oid, value = UNSUPPORTED_GENERAL_NAME_EXTENSIONS[request.param]
    return x509.UnrecognizedExtension(oid, value)


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)
