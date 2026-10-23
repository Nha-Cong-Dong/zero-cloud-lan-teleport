"""Test chữ ký chứng minh giữ khóa khi ghép đôi (đặc tả 9.5, phần V2 trust).

`PairingSigner.sign(payload)` ký bằng khóa danh tính: ECDSA P-256 + SHA-256, mã hóa r ‖ s (mỗi số
32 byte big-endian, IEEE P1363) = 64 byte. `verify_pairing_proof(peer, payload, signature)` kiểm
bằng khóa công khai trong chứng chỉ máy kia và **không bao giờ ném**: mọi thứ sai → False.
`load_pairing_signer(identity)` đọc khóa, kiểm P-256 và khớp chứng chỉ; sai → IdentityError.

Ký và kiểm được làm lại độc lập bằng `cryptography` để không xác nhận lại chính code.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.trust.identity import (
    CERT_FILE,
    KEY_FILE,
    IdentityError,
    PeerCert,
    load_or_create_identity,
    peer_cert_from_pem,
)

# Bậc của nhóm P-256 (SEC 2): r, s hợp lệ nằm trong 1 … N-1.
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
PAYLOAD = (
    b"teleport-initiator-proof-v1"
    + hashlib.sha256(b"A").digest()
    + hashlib.sha256(b"B").digest()
    + bytes(range(32))
    + hashlib.sha256(b"C").digest()
)
assert len(PAYLOAD) == 155


def _api():
    """Import trễ: khi API mới chưa có, chỉ các test dùng tới nó hỏng."""
    from teleport.trust.identity import PairingSigner, load_pairing_signer, verify_pairing_proof

    return PairingSigner, load_pairing_signer, verify_pairing_proof


def load_pairing_signer(identity):
    return _api()[1](identity)


def verify_pairing_proof(peer, payload, signature):
    return _api()[2](peer, payload, signature)


def private_key_of(identity) -> ec.EllipticCurvePrivateKey:
    return serialization.load_pem_private_key(identity.key_path.read_bytes(), password=None)


def p1363(r: int, s: int) -> bytes:
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def split(sig: bytes) -> tuple[int, int]:
    return int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")


def ref_sign(identity, payload: bytes, algorithm: hashes.HashAlgorithm | None = None) -> bytes:
    """Chữ ký tham chiếu theo đặc tả (r ‖ s), ký trực tiếp bằng `cryptography`."""
    der = private_key_of(identity).sign(payload, ec.ECDSA(algorithm or hashes.SHA256()))
    return p1363(*decode_dss_signature(der))


def real_fingerprint(cert: x509.Certificate) -> str:
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def flip_bit(data: bytes, bit: int) -> bytes:
    out = bytearray(data)
    out[bit // 8] ^= 1 << (bit % 8)
    return bytes(out)


@pytest.fixture
def ident(tmp_path):
    return load_or_create_identity(tmp_path / "a")


@pytest.fixture
def other(tmp_path):
    return load_or_create_identity(tmp_path / "b")


@pytest.fixture
def peer(ident) -> PeerCert:
    return peer_cert_from_pem(ident.cert_pem())


# --- Đường đúng -----------------------------------------------------------------------------------


def test_proof_signature_size_comes_from_core():
    from teleport.core.pairing_code import PROOF_SIGNATURE_SIZE

    assert PROOF_SIGNATURE_SIZE == 64


def test_signer_produces_64_byte_p1363_signature_verifiable_independently(ident):
    from teleport.core.pairing_code import PROOF_SIGNATURE_SIZE

    PairingSigner, _, _ = _api()
    signer = load_pairing_signer(ident)
    assert isinstance(signer, PairingSigner)
    sig = signer.sign(PAYLOAD)
    assert type(sig) is bytes
    assert len(sig) == PROOF_SIGNATURE_SIZE == 64
    r, s = split(sig)
    assert 1 <= r < N and 1 <= s < N
    # Kiểm độc lập: ECDSA P-256 + SHA-256 trên đúng payload, bằng khóa công khai trong chứng chỉ.
    cert = x509.load_pem_x509_certificate(ident.cert_pem().encode("ascii"))
    cert.public_key().verify(encode_dss_signature(r, s), PAYLOAD, ec.ECDSA(hashes.SHA256()))


def test_sign_then_verify_is_true(ident, peer):
    sig = load_pairing_signer(ident).sign(PAYLOAD)
    assert verify_pairing_proof(peer, PAYLOAD, sig) is True


def test_verify_accepts_independent_reference_signature(ident, peer):
    assert verify_pairing_proof(peer, PAYLOAD, ref_sign(ident, PAYLOAD)) is True


def test_signer_can_sign_many_payloads(ident, peer):
    signer = load_pairing_signer(ident)
    for i in range(5):
        payload = PAYLOAD[:-1] + bytes([i])
        assert verify_pairing_proof(peer, payload, signer.sign(payload)) is True


def test_high_s_signature_is_still_accepted(ident, peer):
    # Đặc tả: không chặn high-s. (r, n − s) là chữ ký hợp lệ khác của cùng payload.
    r, s = split(ref_sign(ident, PAYLOAD))
    high = p1363(r, N - s)
    assert verify_pairing_proof(peer, PAYLOAD, high) is True
    assert verify_pairing_proof(peer, PAYLOAD, p1363(r, s)) is True


# --- Chữ ký / payload / khóa sai → False --------------------------------------------------------


@pytest.mark.parametrize("bit", [0, 7, 8, 27 * 8, 100 * 8 + 3, 154 * 8 + 7])
def test_flipping_one_payload_bit_fails(ident, peer, bit):
    sig = load_pairing_signer(ident).sign(PAYLOAD)
    assert verify_pairing_proof(peer, flip_bit(PAYLOAD, bit), sig) is False


@pytest.mark.parametrize("bit", [0, 7, 255, 256, 300, 511])
def test_flipping_one_signature_bit_fails(ident, peer, bit):
    sig = load_pairing_signer(ident).sign(PAYLOAD)
    assert verify_pairing_proof(peer, PAYLOAD, flip_bit(sig, bit)) is False


def test_signature_of_other_device_fails(other, peer):
    # Máy lạ có chứng chỉ công khai của máy kia nhưng chỉ ký được bằng khóa của chính nó.
    assert verify_pairing_proof(peer, PAYLOAD, load_pairing_signer(other).sign(PAYLOAD)) is False


def test_swapped_r_and_s_fails(ident, peer):
    r, s = split(ref_sign(ident, PAYLOAD))
    assert verify_pairing_proof(peer, PAYLOAD, p1363(s, r)) is False


def test_signature_with_other_hash_fails(ident, peer):
    assert verify_pairing_proof(peer, PAYLOAD, ref_sign(ident, PAYLOAD, hashes.SHA384())) is False


def test_der_encoded_signature_is_not_accepted(ident, peer):
    der = private_key_of(ident).sign(PAYLOAD, ec.ECDSA(hashes.SHA256()))
    assert verify_pairing_proof(peer, PAYLOAD, der) is False


def test_empty_payload_signature_does_not_verify_other_payload(ident, peer):
    sig = load_pairing_signer(ident).sign(b"")
    assert verify_pairing_proof(peer, b"", sig) is True
    assert verify_pairing_proof(peer, PAYLOAD, sig) is False


@pytest.mark.parametrize(
    "which",
    ["r=0", "s=0", "r=n", "s=n", "r=n+1", "s=2^256-1", "both=0", "both=n"],
)
def test_out_of_range_r_or_s_is_false_without_raising(ident, peer, which):
    r, s = split(ref_sign(ident, PAYLOAD))
    r, s = {
        "r=0": (0, s),
        "s=0": (r, 0),
        "r=n": (N, s),
        "s=n": (r, N),
        "r=n+1": (N + 1, s),
        "s=2^256-1": (r, 2**256 - 1),
        "both=0": (0, 0),
        "both=n": (N, N),
    }[which]
    assert verify_pairing_proof(peer, PAYLOAD, p1363(r, s)) is False


@pytest.mark.parametrize("size", [0, 1, 32, 63, 65, 72, 128])
def test_wrong_signature_length_is_false(ident, peer, size):
    sig = load_pairing_signer(ident).sign(PAYLOAD)
    bad = (sig * 3)[:size]
    assert verify_pairing_proof(peer, PAYLOAD, bad) is False


@pytest.mark.parametrize("kind", ["hex-str", "none", "int", "list", "latin1-str"])
def test_signature_of_wrong_type_is_false(ident, peer, kind):
    sig = load_pairing_signer(ident).sign(PAYLOAD)
    bad = {
        "hex-str": sig.hex(),
        "none": None,
        "int": int.from_bytes(sig, "big"),
        "list": list(sig),
        "latin1-str": sig.decode("latin-1"),
    }[kind]
    assert verify_pairing_proof(peer, PAYLOAD, bad) is False


@pytest.mark.parametrize("kind", ["str", "none", "int", "list"])
def test_payload_of_wrong_type_is_false(ident, peer, kind):
    sig = load_pairing_signer(ident).sign(PAYLOAD)
    bad = {"str": PAYLOAD.decode("latin-1"), "none": None, "int": 1, "list": list(PAYLOAD)}[kind]
    assert verify_pairing_proof(peer, bad, sig) is False


@pytest.mark.parametrize("kind", ["none", "pem-str", "identity", "dict", "der-bytes"])
def test_peer_of_wrong_type_is_false(ident, kind):
    sig = load_pairing_signer(ident).sign(PAYLOAD)
    pem = ident.cert_pem()
    bad = {
        "none": None,
        "pem-str": pem,
        "identity": ident,
        "dict": {"pem": pem},
        "der-bytes": x509.load_pem_x509_certificate(pem.encode("ascii")).public_bytes(
            serialization.Encoding.DER
        ),
    }[kind]
    assert verify_pairing_proof(bad, PAYLOAD, sig) is False


@pytest.mark.parametrize(
    "pem",
    [
        "",
        "rác",
        "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
        "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
    ],
    ids=["empty", "garbage", "bad-base64-cert", "private-key-header"],
)
def test_broken_peer_cert_is_false_without_raising(ident, peer, pem):
    sig = load_pairing_signer(ident).sign(PAYLOAD)
    broken = dataclasses.replace(peer, pem=pem)
    assert verify_pairing_proof(broken, PAYLOAD, sig) is False


def test_peer_cert_with_non_p256_key_is_false(make_cert):
    # PeerCert dựng tay (peer_cert_from_pem đã chặn P-384): verify vẫn phải tự từ chối khóa lạ.
    key = ec.generate_private_key(ec.SECP384R1())
    cert = make_cert(key=key, hash_alg=hashes.SHA384())
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    # Fingerprint thật, để verify đi tới bước kiểm đường cong chứ không dừng ở bước so fingerprint.
    fake_peer = PeerCert(device_id="0" * 32, fingerprint=real_fingerprint(cert), pem=pem)
    der = key.sign(PAYLOAD, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    # P-384 cho r, s 48 byte: thử cả dạng 64 byte bị cắt lẫn dạng đầy đủ 96 byte.
    full = r.to_bytes(48, "big") + s.to_bytes(48, "big")
    assert verify_pairing_proof(fake_peer, PAYLOAD, full) is False
    assert verify_pairing_proof(fake_peer, PAYLOAD, full[:64]) is False


def test_peer_cert_with_rsa_key_is_false(make_cert, rsa_key):
    cert = make_cert(key=rsa_key)
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    fake_peer = PeerCert(device_id="0" * 32, fingerprint=real_fingerprint(cert), pem=pem)
    assert verify_pairing_proof(fake_peer, PAYLOAD, bytes(64)) is False
    assert verify_pairing_proof(fake_peer, PAYLOAD, b"\x01" * 64) is False


def test_peer_cert_whose_fingerprint_does_not_match_pem_is_false(ident, other):
    # PeerCert tự mâu thuẫn: pem của máy này, fingerprint của máy khác; chữ ký đúng vẫn bị từ chối.
    sig = load_pairing_signer(ident).sign(PAYLOAD)
    mixed = PeerCert(device_id=ident.device_id, fingerprint=other.fingerprint, pem=ident.cert_pem())
    assert verify_pairing_proof(mixed, PAYLOAD, sig) is False


class Weird(Exception):
    """Lớp lỗi lạ, kế thừa thẳng từ Exception (như UnsupportedGeneralNameType của cryptography)."""


class EvilPeer(PeerCert):
    """Đúng kiểu PeerCert, nhưng đọc `pem` thì ném một lỗi lạ."""

    @property
    def pem(self):  # type: ignore[override]
        raise Weird("lỗi lạ khi đọc chứng chỉ")


def test_verify_swallows_unexpected_exceptions(ident, peer):
    """Mọi `Exception` bên trong (kể cả lớp lạ) → False, không bay ra."""
    sig = load_pairing_signer(ident).sign(PAYLOAD)
    evil = object.__new__(EvilPeer)
    object.__setattr__(evil, "device_id", peer.device_id)
    object.__setattr__(evil, "fingerprint", peer.fingerprint)
    assert isinstance(evil, PeerCert)
    assert verify_pairing_proof(evil, PAYLOAD, sig) is False


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    payload=st.binary(max_size=200),
    signature=st.binary(max_size=80) | st.text(max_size=80) | st.none() | st.integers(),
)
def test_fuzz_verify_never_raises_and_is_false_for_junk(peer, payload, signature):
    result = verify_pairing_proof(peer, payload, signature)
    assert result is False


# --- load_pairing_signer ------------------------------------------------------------------------


def test_load_signer_key_not_matching_cert_is_identity_error(ident, other):
    ident.key_path.write_bytes(other.key_path.read_bytes())
    with pytest.raises(IdentityError):
        load_pairing_signer(ident)


def test_load_signer_with_identity_pointing_to_foreign_key_is_identity_error(ident, other):
    mixed = dataclasses.replace(ident, key_path=other.key_path)
    with pytest.raises(IdentityError):
        load_pairing_signer(mixed)


def test_load_signer_when_cert_changed_since_identity_was_loaded_is_identity_error(ident):
    # Cặp khóa/chứng chỉ trên đĩa vẫn khớp nhau, nhưng không còn là danh tính đã nạp: không ký.
    stale = dataclasses.replace(ident, fingerprint="a" * 64)
    with pytest.raises(IdentityError):
        load_pairing_signer(stale)


@pytest.mark.parametrize(
    "content",
    [b"", b"garbage", b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n", b"\xff\xfe\x00"],
    ids=["empty", "garbage", "bad-pem", "binary"],
)
def test_load_signer_with_corrupt_key_file_is_identity_error(ident, content):
    ident.key_path.write_bytes(content)
    with pytest.raises(IdentityError):
        load_pairing_signer(ident)


def test_load_signer_with_missing_key_file_is_identity_error(ident):
    ident.key_path.unlink()
    with pytest.raises(IdentityError):
        load_pairing_signer(ident)


def test_load_signer_with_non_p256_key_is_identity_error(ident):
    other_key = ec.generate_private_key(ec.SECP384R1())
    ident.key_path.write_bytes(
        other_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    with pytest.raises(IdentityError):
        load_pairing_signer(ident)


def test_load_signer_with_rsa_key_is_identity_error(ident, rsa_key):
    ident.key_path.write_bytes(
        rsa_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    with pytest.raises(IdentityError):
        load_pairing_signer(ident)


@pytest.mark.parametrize("content", [b"", b"garbage"], ids=["empty", "garbage"])
def test_load_signer_with_corrupt_cert_file_is_identity_error(ident, content):
    ident.cert_path.write_bytes(content)
    with pytest.raises(IdentityError):
        load_pairing_signer(ident)


def test_identity_files_are_the_ones_load_pairing_signer_reads(ident):
    # Khóa đọc từ đúng file của danh tính (device.key / device.crt).
    assert ident.key_path.name == KEY_FILE
    assert ident.cert_path.name == CERT_FILE


def test_signer_repr_does_not_leak_private_key(ident):
    signer = load_pairing_signer(ident)
    key = private_key_of(ident)
    pem = ident.key_path.read_text(encoding="ascii")
    body = "".join(line for line in pem.splitlines() if not line.startswith("-----"))
    secret = key.private_numbers().private_value
    for text in (repr(signer), str(signer)):
        assert "PRIVATE KEY" not in text
        assert body[:40] not in text
        assert str(secret) not in text
        assert f"{secret:x}" not in text
        assert f"{secret:064x}" not in text


def test_signature_cannot_be_reused_across_payloads(ident, peer):
    # Một chữ ký đúng cho payload này không qua được với payload của phiên khác (n_A / C_B mới).
    signer = load_pairing_signer(ident)
    sig = signer.sign(PAYLOAD)
    other_session = PAYLOAD[:91] + bytes(32) + PAYLOAD[123:]
    assert verify_pairing_proof(peer, other_session, sig) is False


def test_verify_independent_check_agrees_with_library(ident, peer):
    """Đối chứng cho bộ tham chiếu ở trên: `cryptography` từ chối chữ ký bị sửa."""
    r, s = split(ref_sign(ident, PAYLOAD))
    cert = x509.load_pem_x509_certificate(ident.cert_pem().encode("ascii"))
    with pytest.raises(InvalidSignature):
        cert.public_key().verify(encode_dss_signature(r, (s + 1) % N), PAYLOAD, ec.ECDSA(hashes.SHA256()))
