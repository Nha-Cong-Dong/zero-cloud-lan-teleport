import base64
import dataclasses
import re
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.trust.identity import (
    InvalidCertificate,
    PeerCert,
    fingerprint_of_der,
    format_fingerprint,
    load_or_create_identity,
    peer_cert_from_der,
    peer_cert_from_pem,
)

MAX_PEM = 8192


def pem_of(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def der_of(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


def test_creates_identity_files(tmp_path):
    identity = load_or_create_identity(tmp_path / "cfg")
    assert identity.cert_path.exists()
    assert identity.key_path.exists()
    assert re.fullmatch(r"[0-9a-f]{64}", identity.fingerprint)
    assert re.fullmatch(r"[0-9a-f]{32}", identity.device_id)


def test_reload_returns_same_identity(tmp_path):
    first = load_or_create_identity(tmp_path)
    second = load_or_create_identity(tmp_path)
    assert first == second


def test_each_device_gets_its_own_identity(tmp_path):
    a = load_or_create_identity(tmp_path / "a")
    b = load_or_create_identity(tmp_path / "b")
    assert a.fingerprint != b.fingerprint
    assert a.device_id != b.device_id


def test_format_fingerprint():
    assert format_fingerprint("abcd12") == "AB:CD:12"


def test_short_fingerprint_is_first_8_bytes(tmp_path):
    identity = load_or_create_identity(tmp_path)
    short = identity.short_fingerprint
    assert len(short) == 23  # 8 cặp hex + 7 dấu ":"
    assert short.replace(":", "").lower() == identity.fingerprint[:16]


def test_cert_pem_matches_fingerprint(tmp_path):
    identity = load_or_create_identity(tmp_path)
    pem = identity.cert_pem()
    assert pem.startswith("-----BEGIN CERTIFICATE-----")
    assert fingerprint_of_der(ssl.PEM_cert_to_DER_cert(pem)) == identity.fingerprint


# --- PeerCert: chứng chỉ hợp lệ ----------------------------------------------------------------


@pytest.fixture
def identity(tmp_path):
    return load_or_create_identity(tmp_path / "peer")


def test_invalid_certificate_is_value_error():
    assert issubclass(InvalidCertificate, ValueError)


def test_peer_cert_from_pem_of_own_identity(identity):
    peer = peer_cert_from_pem(identity.cert_pem())
    assert isinstance(peer, PeerCert)
    assert peer.device_id == identity.device_id
    assert peer.fingerprint == identity.fingerprint
    assert peer.pem == identity.cert_pem()  # bất biến của phụ lục điểm 6


def test_peer_cert_from_der_of_own_identity(identity):
    der = ssl.PEM_cert_to_DER_cert(identity.cert_pem())
    peer = peer_cert_from_der(der)
    assert peer == peer_cert_from_pem(identity.cert_pem())
    assert peer.pem == identity.cert_pem()
    assert peer.fingerprint == fingerprint_of_der(der)


def test_peer_cert_is_frozen(identity):
    peer = peer_cert_from_pem(identity.cert_pem())
    with pytest.raises(dataclasses.FrozenInstanceError):
        peer.device_id = "0" * 32  # type: ignore[misc]


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [("\n\n  ", "  \n"), ("\t", ""), ("", "\r\n\r\n"), (" \r\n", "\n\t \n")],
    ids=["nl-space", "tab", "crlf", "mixed"],
)
def test_pem_with_surrounding_whitespace_is_valid_and_normalized(identity, prefix, suffix):
    peer = peer_cert_from_pem(prefix + identity.cert_pem() + suffix)
    assert peer.device_id == identity.device_id
    assert peer.fingerprint == identity.fingerprint
    assert peer.pem == identity.cert_pem()


def test_self_made_teleport_cert_is_valid(make_cert):
    # Đối chứng: cert tự tạo đúng chuẩn thì phải hợp lệ (để các ca xấu bên dưới có ý nghĩa).
    cert = make_cert(cn="teleport-" + "ab" * 16)
    peer = peer_cert_from_pem(pem_of(cert))
    assert peer.device_id == "ab" * 16
    assert peer.fingerprint == fingerprint_of_der(der_of(cert))
    assert peer.pem == pem_of(cert)
    assert peer_cert_from_der(der_of(cert)) == peer


def test_basic_constraints_ca_false_is_accepted(make_cert):
    cert = make_cert(ca=False)
    assert peer_cert_from_pem(pem_of(cert)).fingerprint == fingerprint_of_der(der_of(cert))


def test_pem_exactly_at_limit_is_valid(identity):
    pem = identity.cert_pem()
    padded = pem + " " * (MAX_PEM - len(pem))
    assert len(padded) == MAX_PEM
    assert peer_cert_from_pem(padded).pem == pem


def test_pem_one_over_limit_is_rejected(identity):
    pem = identity.cert_pem()
    padded = pem + " " * (MAX_PEM + 1 - len(pem))
    assert len(padded) == MAX_PEM + 1
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(padded)


# --- PeerCert: chứng chỉ xấu (tự tạo bằng cryptography) ----------------------------------------


@pytest.mark.parametrize(
    "cn",
    [
        "foo",
        "teleport-",
        "teleport-" + "AB" * 16,
        "teleport-" + "a" * 31,
        "teleport-" + "a" * 33,
        "teleport-" + "g" * 32,
        "Teleport-" + "a" * 32,
        "teleport_" + "a" * 32,
        "a" * 32,
        "xteleport-" + "a" * 32,
        "teleport-" + "a" * 32 + "\n",
        "teleport-" + "０" * 10 + "a" * 22,  # 32 code point, có chữ số toàn chiều rộng
    ],
    ids=repr,
)
def test_wrong_common_name_rejected(make_cert, cn):
    cert = make_cert(cn=cn)
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(pem_of(cert))
    with pytest.raises(InvalidCertificate):
        peer_cert_from_der(der_of(cert))


def test_two_common_names_rejected(make_cert):
    cert = make_cert(cn="teleport-" + "a" * 32, extra_cn="teleport-" + "b" * 32)
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(pem_of(cert))


def test_rsa_key_rejected(make_cert, rsa_key):
    cert = make_cert(key=rsa_key)
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(pem_of(cert))
    with pytest.raises(InvalidCertificate):
        peer_cert_from_der(der_of(cert))


@pytest.mark.parametrize("hash_alg", [hashes.SHA256(), hashes.SHA384()], ids=["sha256", "sha384"])
def test_p384_key_rejected(make_cert, hash_alg):
    cert = make_cert(key=ec.generate_private_key(ec.SECP384R1()), hash_alg=hash_alg)
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(pem_of(cert))


@pytest.mark.parametrize("hash_alg", [hashes.SHA384(), hashes.SHA512(), hashes.SHA224()], ids=repr)
def test_p256_with_non_sha256_signature_rejected(make_cert, hash_alg):
    cert = make_cert(hash_alg=hash_alg)
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(pem_of(cert))


NETSCAPE_CERT_TYPE = x509.ObjectIdentifier("2.16.840.1.113730.1.1")


def key_usage(**flags) -> x509.KeyUsage:
    fields = dict.fromkeys(
        [
            "digital_signature",
            "content_commitment",
            "key_encipherment",
            "data_encipherment",
            "key_agreement",
            "key_cert_sign",
            "crl_sign",
            "encipher_only",
            "decipher_only",
        ],
        False,
    )
    fields.update(flags)
    return x509.KeyUsage(**fields)


@pytest.mark.parametrize(
    ("ext", "critical"),
    [
        (key_usage(key_cert_sign=True), True),
        (key_usage(key_cert_sign=True), False),
        (key_usage(digital_signature=True, key_cert_sign=True), True),
        (key_usage(crl_sign=True), True),
        (key_usage(digital_signature=True, key_agreement=True, crl_sign=True), False),
        (key_usage(key_cert_sign=True, crl_sign=True), True),
        # Netscape Cert Type (bit sslCA = 0x04): BIT STRING 03 02 02 04.
        (x509.UnrecognizedExtension(NETSCAPE_CERT_TYPE, b"\x03\x02\x02\x04"), False),
        # Kể cả khi chỉ khai sslClient (0x80): cấm cả extension, không cần biết bit nào.
        (x509.UnrecognizedExtension(NETSCAPE_CERT_TYPE, b"\x03\x02\x07\x80"), False),
    ],
    ids=[
        "ku-keyCertSign-critical",
        "ku-keyCertSign-noncritical",
        "ku-ds+keyCertSign",
        "ku-cRLSign",
        "ku-ds+ka+cRLSign",
        "ku-keyCertSign+cRLSign",
        "netscape-sslCA",
        "netscape-sslClient",
    ],
)
def test_ca_like_extensions_rejected(make_cert, ext, critical):
    cert = make_cert(extensions=[(ext, critical)])
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(pem_of(cert))
    with pytest.raises(InvalidCertificate):
        peer_cert_from_der(der_of(cert))


@pytest.mark.parametrize(
    "ku",
    [
        key_usage(digital_signature=True),
        key_usage(digital_signature=True, key_agreement=True),
        key_usage(digital_signature=True, key_agreement=True, content_commitment=True),
        key_usage(digital_signature=True, key_agreement=True, key_encipherment=True, data_encipherment=True),
    ],
    ids=["ds", "ds+ka", "ds+ka+nr", "ds+ka+ke+de"],
)
def test_key_usage_without_cert_or_crl_sign_is_accepted(make_cert, ku):
    cert = make_cert(extensions=[(ku, True)])
    peer = peer_cert_from_pem(pem_of(cert))
    assert peer.fingerprint == fingerprint_of_der(der_of(cert))
    assert peer_cert_from_der(der_of(cert)) == peer


def test_key_usage_with_basic_constraints_ca_false_is_accepted(make_cert):
    cert = make_cert(ca=False, extensions=[(key_usage(digital_signature=True, key_agreement=True), True)])
    assert peer_cert_from_pem(pem_of(cert)).pem == pem_of(cert)


def test_own_identity_cert_still_valid_after_extension_rules(identity):
    assert peer_cert_from_pem(identity.cert_pem()).fingerprint == identity.fingerprint


def test_ca_true_rejected(make_cert):
    cert = make_cert(ca=True)
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(pem_of(cert))
    with pytest.raises(InvalidCertificate):
        peer_cert_from_der(der_of(cert))


def test_signed_by_other_key_rejected(make_cert):
    cert = make_cert(signing_key=ec.generate_private_key(ec.SECP256R1()))
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(pem_of(cert))
    with pytest.raises(InvalidCertificate):
        peer_cert_from_der(der_of(cert))


def test_issuer_different_from_subject_rejected(make_cert):
    cert = make_cert(issuer_cn="teleport-" + "c" * 32)
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(pem_of(cert))


def test_two_concatenated_certs_rejected(identity, make_cert):
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(identity.cert_pem() + pem_of(make_cert()))
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(identity.cert_pem() * 2)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda p: "rác\n" + p,
        lambda p: "junk\n" + p,
        lambda p: p + "junk",
        lambda p: p + "\n# comment\n",
        lambda p: "Subject: CN=teleport\n" + p,
        lambda p: p + "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
    ],
    ids=[
        "non-ascii-prefix",
        "ascii-prefix",
        "suffix",
        "comment-suffix",
        "openssl-text-prefix",
        "extra-block",
    ],
)
def test_garbage_outside_pem_block_rejected(identity, mangle):
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(mangle(identity.cert_pem()))


@pytest.mark.parametrize(
    "mangle",
    [
        lambda p: p + "é",
        lambda p: "\u3000" + p,  # khoảng trắng nhưng không ASCII
        lambda p: p + "\u00a0",
        lambda p: p.replace("\n", "\nđ", 2),
        lambda p: p.replace("CERTIFICATE", "CERTIFICATÉ", 1),
        lambda p: "\ufeff" + p,
        lambda p: p + "\ud800",
    ],
    ids=["e-acute", "ideographic-space", "nbsp", "inside-block", "in-header", "bom", "lone-surrogate"],
)
def test_non_ascii_rejected_as_invalid_certificate(identity, mangle):
    # Phải là InvalidCertificate, không để lọt UnicodeEncodeError.
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(mangle(identity.cert_pem()))


@pytest.mark.parametrize(
    "pem",
    [
        "",
        "   ",
        "rác",
        "-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n",
        "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
        "-----BEGIN CERTIFICATE-----\nMIIBkTCCATegAwIBAgIU\n",
        "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
    ],
    ids=repr,
)
def test_garbage_pem_rejected(pem):
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(pem)


def test_truncated_pem_rejected(identity):
    pem = identity.cert_pem()
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(pem[: len(pem) // 2])
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(pem.replace("-----END CERTIFICATE-----", ""))


def test_private_key_pem_rejected(identity):
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(identity.key_path.read_text(encoding="ascii"))


@pytest.mark.parametrize("der", [b"", b"\x00", b"\x30\x00", b"rac" * 100, bytes(range(256))], ids=repr)
def test_garbage_der_rejected(der):
    with pytest.raises(InvalidCertificate):
        peer_cert_from_der(der)


def test_truncated_der_rejected(identity):
    der = ssl.PEM_cert_to_DER_cert(identity.cert_pem())
    with pytest.raises(InvalidCertificate):
        peer_cert_from_der(der[:-1])
    with pytest.raises(InvalidCertificate):
        peer_cert_from_der(der + b"\x00")


def test_cert_whose_pem_exceeds_limit_rejected_from_pem_and_der(make_cert):
    # Cert hợp lệ mọi mặt nhưng PEM chuẩn hóa dài hơn 8192 ký tự.
    cert = make_cert(san_dns=[f"may-so-{i}.example" for i in range(600)])
    assert len(pem_of(cert)) > MAX_PEM
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(pem_of(cert))
    with pytest.raises(InvalidCertificate):
        peer_cert_from_der(der_of(cert))


def test_large_but_within_limit_cert_accepted(make_cert):
    cert = make_cert(san_dns=[f"h{i}.example" for i in range(300)])
    assert len(pem_of(cert)) <= MAX_PEM
    assert peer_cert_from_der(der_of(cert)).pem == pem_of(cert)


def test_invalid_x509_version_rejected_from_pem_and_der(identity):
    # Trường version của X.509 v3 mã hóa là [0] INTEGER 2. Đổi thành 5 (tức "v6"):
    # `cryptography` ném InvalidVersion (không phải ValueError), có bản ném ngay lúc load.
    der = x509.load_pem_x509_certificate(identity.cert_pem().encode()).public_bytes(
        serialization.Encoding.DER
    )
    v3_field = b"\xa0\x03\x02\x01\x02"
    assert der.count(v3_field) == 1
    bad_der = der.replace(v3_field, b"\xa0\x03\x02\x01\x05")
    b64 = base64.encodebytes(bad_der).decode("ascii")
    bad_pem = f"-----BEGIN CERTIFICATE-----\n{b64}-----END CERTIFICATE-----\n"
    with pytest.raises(InvalidCertificate):
        peer_cert_from_der(bad_der)
    with pytest.raises(InvalidCertificate):
        peer_cert_from_pem(bad_pem)


# --- Fuzz --------------------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=300) | st.binary(max_size=300).map(lambda b: b.decode("latin-1")))
def test_fuzz_peer_cert_from_pem_only_raises_invalid_certificate(text):
    try:
        peer = peer_cert_from_pem(text)
    except InvalidCertificate:
        return
    assert isinstance(peer, PeerCert)


@settings(max_examples=200, deadline=None)
@given(st.binary(max_size=600))
def test_fuzz_peer_cert_from_der_only_raises_invalid_certificate(der):
    try:
        peer = peer_cert_from_der(der)
    except InvalidCertificate:
        return
    assert isinstance(peer, PeerCert)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(data=st.data())
def test_fuzz_mutated_valid_pem_only_raises_invalid_certificate(identity, data):
    pem = identity.cert_pem()
    i = data.draw(st.integers(0, len(pem) - 1))
    ch = data.draw(st.characters())
    mutated = pem[:i] + ch + pem[i + 1 :]
    try:
        peer = peer_cert_from_pem(mutated)
    except InvalidCertificate:
        return
    assert isinstance(peer, PeerCert)
    assert peer.pem.isascii()
