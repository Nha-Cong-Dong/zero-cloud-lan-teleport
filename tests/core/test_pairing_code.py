"""Test `teleport.core.pairing_code`: tính lại độc lập bằng `hashlib` theo công thức kế hoạch §2.3."""

import hashlib
import re
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from teleport.core.pairing_code import (
    CODE_DIGITS,
    NONCE_SIZE,
    check_commitment,
    format_code,
    make_commitment,
    pairing_code,
)

# --- Tham chiếu độc lập (không chép từ code) ---------------------------------------------------


def ref_commitment(fp_initiator: str, nonce: bytes) -> str:
    return hashlib.sha256(b"teleport-commit-v1" + bytes.fromhex(fp_initiator) + nonce).hexdigest()


def ref_code(fp_initiator: str, fp_responder: str, n_a: bytes, n_b: bytes) -> str:
    digest = hashlib.sha256(
        b"teleport-pair-v1" + bytes.fromhex(fp_initiator) + bytes.fromhex(fp_responder) + n_a + n_b
    ).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


# --- Vector cố định ----------------------------------------------------------------------------
# fp = SHA-256 của các chuỗi ASCII cho dễ tái tạo; giá trị kỳ vọng tính tay một lần bằng hashlib.
FP_A = hashlib.sha256(b"initiator").hexdigest()
FP_B = hashlib.sha256(b"responder").hexdigest()
FP_C = hashlib.sha256(b"other").hexdigest()
N_A = bytes(range(32))
N_B = bytes(range(32, 64))
N_B_LEADING_ZERO = bytes.fromhex("f506" + "00" * 30)

VECTOR_FP_A = "59eb3cb130074f7b1ddb99c8fe14f27e1dbca735bb3da82e1ff0305340a32419"
VECTOR_FP_B = "6dcde155f1f900e157d25bb5c24b1a54d71ea35b78661ed9b1a8be3532c3d4f5"
VECTOR_COMMITMENT = "d55c4dad2e3d3ee9d9d81205366d8b8521b5c3390379d41a5f6e5ae50986fc02"
VECTOR_CODE = "536981"


def flip(b: bytes, i: int = 0) -> bytes:
    out = bytearray(b)
    out[i] ^= 1
    return bytes(out)


def test_constants():
    assert NONCE_SIZE == 32
    assert CODE_DIGITS == 6


def test_fixed_vector_inputs_are_what_we_think():
    assert FP_A == VECTOR_FP_A
    assert FP_B == VECTOR_FP_B
    assert ref_commitment(FP_A, N_A) == VECTOR_COMMITMENT
    assert ref_code(FP_A, FP_B, N_A, N_B) == VECTOR_CODE


def test_commitment_fixed_vector():
    assert make_commitment(FP_A, N_A) == VECTOR_COMMITMENT


def test_pairing_code_fixed_vector():
    assert pairing_code(FP_A, FP_B, N_A, N_B) == VECTOR_CODE


def test_pairing_code_keeps_leading_zeros():
    code = pairing_code(FP_A, FP_B, N_A, N_B_LEADING_ZERO)
    assert code == ref_code(FP_A, FP_B, N_A, N_B_LEADING_ZERO) == "000083"


def test_commitment_is_64_lowercase_hex():
    assert re.fullmatch(r"[0-9a-f]{64}", make_commitment(FP_A, N_A))


@pytest.mark.parametrize(
    ("label", "args"),
    [
        ("fp_khởi_xướng", (FP_C, FP_B, N_A, N_B)),
        ("fp_trả_lời", (FP_A, FP_C, N_A, N_B)),
        ("n_A byte đầu", (FP_A, FP_B, flip(N_A), N_B)),
        ("n_A byte cuối", (FP_A, FP_B, flip(N_A, 31), N_B)),
        ("n_B byte đầu", (FP_A, FP_B, N_A, flip(N_B))),
        ("n_B byte cuối", (FP_A, FP_B, N_A, flip(N_B, 31))),
        ("đổi vai trò", (FP_B, FP_A, N_A, N_B)),
        ("đổi nonce", (FP_A, FP_B, N_B, N_A)),
        ("đổi cả hai", (FP_B, FP_A, N_B, N_A)),
    ],
)
def test_changing_any_input_or_role_changes_code(label, args):
    code = pairing_code(*args)
    assert code != VECTOR_CODE, label
    assert code == ref_code(*args)


def test_role_order_is_fixed_not_sorted():
    # Thứ tự theo vai trò (khởi xướng trước), không sắp xếp fingerprint.
    assert pairing_code(FP_A, FP_B, N_A, N_B) == "536981"
    assert pairing_code(FP_B, FP_A, N_A, N_B) == "021724"


def test_check_commitment_true_when_matching():
    assert check_commitment(FP_A, N_A, VECTOR_COMMITMENT) is True


def test_check_commitment_false_with_other_nonce():
    assert check_commitment(FP_A, flip(N_A), VECTOR_COMMITMENT) is False
    assert check_commitment(FP_A, flip(N_A, 31), VECTOR_COMMITMENT) is False


def test_check_commitment_false_with_other_fingerprint():
    assert check_commitment(FP_B, N_A, VECTOR_COMMITMENT) is False


def test_check_commitment_false_with_other_commitment():
    other = make_commitment(FP_A, N_B)
    assert check_commitment(FP_A, N_A, other) is False
    tampered = VECTOR_COMMITMENT[:-1] + ("0" if VECTOR_COMMITMENT[-1] != "0" else "1")
    assert check_commitment(FP_A, N_A, tampered) is False


def test_commitment_domain_separated_from_code():
    # Cùng fp và nonce nhưng tiền tố khác: commitment không được trùng digest của mã.
    digest_hex = hashlib.sha256(b"teleport-pair-v1" + bytes.fromhex(FP_A) + N_A).hexdigest()
    assert make_commitment(FP_A, N_A) != digest_hex


# --- ValueError --------------------------------------------------------------------------------

BAD_FPS = [
    "",
    FP_A.upper(),
    "A" + FP_A[1:],
    FP_A[:63],
    FP_A + "0",
    "g" + FP_A[1:],
    FP_A[:63] + "\n",
    FP_A + "\n",
    " " + FP_A[1:],
    "０" * 64,
    "٠" * 64,
    "AB:" + FP_A[3:],
]
BAD_NONCES = [b"", bytes(31), bytes(33), bytes(64), b"\x00" * 16]


@pytest.mark.parametrize("fp", BAD_FPS, ids=repr)
def test_bad_fingerprint_raises_value_error(fp):
    with pytest.raises(ValueError):
        make_commitment(fp, N_A)
    with pytest.raises(ValueError):
        check_commitment(fp, N_A, VECTOR_COMMITMENT)
    with pytest.raises(ValueError):
        pairing_code(fp, FP_B, N_A, N_B)
    with pytest.raises(ValueError):
        pairing_code(FP_A, fp, N_A, N_B)


def test_same_fingerprints_raise_value_error():
    with pytest.raises(ValueError):
        pairing_code(FP_A, FP_A, N_A, N_B)


@pytest.mark.parametrize("nonce", BAD_NONCES, ids=lambda n: f"{len(n)}B")
def test_bad_nonce_size_raises_value_error(nonce):
    with pytest.raises(ValueError):
        make_commitment(FP_A, nonce)
    with pytest.raises(ValueError):
        check_commitment(FP_A, nonce, VECTOR_COMMITMENT)
    with pytest.raises(ValueError):
        pairing_code(FP_A, FP_B, nonce, N_B)
    with pytest.raises(ValueError):
        pairing_code(FP_A, FP_B, N_A, nonce)


def test_equal_nonces_are_allowed():
    # Đặc tả chỉ cấm hai fp giống nhau, không cấm hai nonce trùng.
    assert pairing_code(FP_A, FP_B, N_A, N_A) == ref_code(FP_A, FP_B, N_A, N_A)


# --- format_code -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [("012345", "012 345"), ("048213", "048 213"), ("000000", "000 000"), ("999999", "999 999")],
)
def test_format_code(code, expected):
    assert format_code(code) == expected


@pytest.mark.parametrize(
    "code",
    [
        "",
        "12345",
        "1234567",
        "12a456",
        "012 345",
        " 012345",
        "012345 ",
        "012345\n",
        "０４８２１３",
        "٠١٢٣٤٥",
        "-12345",
        "+12345",
        "1_2345",
    ],
    ids=repr,
)
def test_format_code_rejects_non_six_ascii_digits(code):
    with pytest.raises(ValueError):
        format_code(code)


# --- Thuộc tính (hypothesis) -------------------------------------------------------------------

fingerprints = st.binary(min_size=32, max_size=32).map(bytes.hex)
nonces = st.binary(min_size=32, max_size=32)


@settings(max_examples=300)
@given(fingerprints, fingerprints, nonces, nonces)
def test_property_code_matches_reference_and_shape(fp_a, fp_b, n_a, n_b):
    assume(fp_a != fp_b)
    code = pairing_code(fp_a, fp_b, n_a, n_b)
    assert code == ref_code(fp_a, fp_b, n_a, n_b)
    assert re.fullmatch(r"[0-9]{6}", code)
    assert format_code(code) == f"{code[:3]} {code[3:]}"


@settings(max_examples=300)
@given(fingerprints, nonces, nonces)
def test_property_commitment_matches_reference_and_checks(fp, n, other):
    commitment = make_commitment(fp, n)
    assert commitment == ref_commitment(fp, n)
    assert re.fullmatch(r"[0-9a-f]{64}", commitment)
    assert check_commitment(fp, n, commitment) is True
    if other != n:
        assert check_commitment(fp, other, commitment) is False


@settings(max_examples=200)
@given(fingerprints, fingerprints, nonces)
def test_property_commitment_bound_to_initiator_fingerprint(fp, other_fp, n):
    assume(fp != other_fp)
    assert check_commitment(other_fp, n, make_commitment(fp, n)) is False


@given(st.text(alphabet="0123456789", min_size=6, max_size=6))
def test_property_format_code_any_six_digits(code):
    assert format_code(code) == code[:3] + " " + code[3:]


@given(st.text(max_size=10))
def test_property_format_code_rejects_everything_else(code):
    assume(not re.fullmatch(r"[0-9]{6}", code, flags=re.ASCII))
    with pytest.raises(ValueError):
        format_code(code)


@settings(max_examples=200)
@given(st.text(max_size=70))
def test_property_bad_fingerprint_text_raises_value_error(fp):
    assume(not re.fullmatch(r"[0-9a-f]{64}", fp))
    with pytest.raises(ValueError):
        make_commitment(fp, N_A)


# --- Vector trong docs/protocol.md §5 (phụ lục điểm 17) ----------------------------------------

PROTOCOL_MD = Path(__file__).resolve().parents[2] / "docs" / "protocol.md"


def _doc_vector() -> dict[str, str]:
    text = PROTOCOL_MD.read_text(encoding="utf-8")
    section = text[text.index("### Test vector") :]
    rows = dict(re.findall(r"^\|\s*\**`?([^`|*]+?)`?\**\s*\|\s*\**`([0-9a-f]+)`", section, flags=re.M))
    return rows


def test_protocol_md_vector_matches_independent_computation():
    rows = _doc_vector()
    fp_a, fp_b = rows["fp_khởi_xướng"], rows["fp_trả_lời"]
    n_a, n_b = bytes.fromhex(rows["n_A"]), bytes.fromhex(rows["n_B"])
    # Tài liệu nói hai fp là SHA-256 của hai chuỗi ASCII này.
    assert fp_a == hashlib.sha256(b"teleport test vector A").hexdigest()
    assert fp_b == hashlib.sha256(b"teleport test vector B").hexdigest()
    assert rows["commitment"] == ref_commitment(fp_a, n_a)
    digest = hashlib.sha256(
        b"teleport-pair-v1" + bytes.fromhex(fp_a) + bytes.fromhex(fp_b) + n_a + n_b
    ).hexdigest()
    assert rows["digest"] == digest
    assert rows["mã"] == ref_code(fp_a, fp_b, n_a, n_b)
    # Và code cài đặt cho đúng các giá trị đó.
    assert make_commitment(fp_a, n_a) == rows["commitment"]
    assert pairing_code(fp_a, fp_b, n_a, n_b) == rows["mã"]
    assert format_code(rows["mã"]) == rows["mã"][:3] + " " + rows["mã"][3:]


def test_protocol_md_role_and_nonce_swap_codes():
    rows = _doc_vector()
    fp_a, fp_b = rows["fp_khởi_xướng"], rows["fp_trả_lời"]
    n_a, n_b = bytes.fromhex(rows["n_A"]), bytes.fromhex(rows["n_B"])
    text = PROTOCOL_MD.read_text(encoding="utf-8")
    swapped_roles = ref_code(fp_b, fp_a, n_a, n_b)
    swapped_nonces = ref_code(fp_a, fp_b, n_b, n_a)
    assert f"Đổi vai trò (đảo hai fingerprint) cho mã `{swapped_roles}`" in text
    assert f"đảo hai nonce cho mã `{swapped_nonces}`" in text
    assert pairing_code(fp_b, fp_a, n_a, n_b) == swapped_roles
    assert pairing_code(fp_a, fp_b, n_b, n_a) == swapped_nonces
