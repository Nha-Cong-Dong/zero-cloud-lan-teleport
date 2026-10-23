"""Mã ghép đôi 6 số có nonce cam kết (kiểu Bluetooth numeric comparison).

Đặc tả ở docs/protocol.md mục 5. Nếu mã chỉ tính từ hai fingerprint, kẻ đứng giữa
(MITM) có thể sinh khóa offline tới khi ra mã trùng. Ở đây bên trả lời cam kết
nonce n_B *trước* khi biết n_A, và chỉ tiết lộ n_B sau khi đã nhận n_A và đã hiện
mã; mã phụ thuộc cả hai nonce, nên mỗi lần thử MITM chỉ thành công với xác suất
10⁻⁶, và mỗi lần thử đều làm một máy hiện mã cho người dùng thấy.

Bên trả lời là bên cam kết vì nó là máy đang chờ ghép đôi, có thể bị gọi tới nhiều
lần: nếu bên khởi xướng cam kết, kẻ đứng giữa đóng vai bên khởi xướng sẽ tính được
mã của bên trả lời trước khi phải lộ nonce của mình, rồi bỏ phiên mà bên trả lời
không hiện mã.

Bên khởi xướng còn chứng minh giữ khóa riêng của chứng chỉ trong `pair_request`: nó
ký `proof_payload(...)` bằng khóa danh tính và gửi chữ ký trong `pair_nonce` (protocol
mục 5.1, 5.2). Module này chỉ dựng payload; ký và kiểm chữ ký là việc của `trust`.

Module chỉ tính toán (sans-IO); sinh nonce bằng `secrets.token_bytes(NONCE_SIZE)`
là việc của tầng app.
"""

from __future__ import annotations

import hashlib
import hmac
import re

NONCE_SIZE = 32
CODE_DIGITS = 6
# Chữ ký ECDSA P-256 dạng r ‖ s, mỗi số 32 byte big-endian (IEEE P1363).
PROOF_SIGNATURE_SIZE = 64

_COMMIT_LABEL = b"teleport-commit-v1"
_PAIR_LABEL = b"teleport-pair-v1"
# Mọi chữ ký bằng khóa danh tính có nhãn riêng (protocol mục 1), để chữ ký cho việc này
# không bao giờ dùng lại được cho việc khác.
_PROOF_LABEL = b"teleport-initiator-proof-v1"

_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_CODE_RE = re.compile(r"[0-9]{6}")


def _fingerprint_bytes(fp: object, field: str) -> bytes:
    if not isinstance(fp, str) or not _FINGERPRINT_RE.fullmatch(fp):
        raise ValueError(f"{field} phải là đúng 64 ký tự hex thường")
    return bytes.fromhex(fp)


def _check_nonce(nonce: object, field: str) -> bytes:
    if not isinstance(nonce, bytes) or len(nonce) != NONCE_SIZE:
        raise ValueError(f"{field} phải là đúng {NONCE_SIZE} byte")
    return nonce


def make_commitment(fp_responder: str, nonce: bytes) -> str:
    """`SHA-256(b"teleport-commit-v1" ‖ fp_trả_lời ‖ n_B)`, dạng 64 ký tự hex thường."""
    fp = _fingerprint_bytes(fp_responder, "fp_responder")
    n = _check_nonce(nonce, "nonce")
    return hashlib.sha256(_COMMIT_LABEL + fp + n).hexdigest()


def check_commitment(fp_responder: str, nonce: bytes, commitment: str) -> bool:
    """True nếu `commitment` đúng là cam kết của (`fp_responder`, `nonce`).

    `commitment` sai định dạng thì trả False (không raise); fp/nonce sai thì ValueError.
    """
    expected = make_commitment(fp_responder, nonce)
    if not isinstance(commitment, str) or not _FINGERPRINT_RE.fullmatch(commitment):
        return False
    # compare_digest so sánh trong thời gian không đổi.
    return hmac.compare_digest(expected, commitment)


def pairing_code(fp_initiator: str, fp_responder: str, nonce_initiator: bytes, nonce_responder: bytes) -> str:
    """Mã 6 chữ số, ví dụ "048213".

    `digest = SHA-256(b"teleport-pair-v1" ‖ fp_khởi_xướng ‖ fp_trả_lời ‖ n_A ‖ n_B)`,
    mã = 4 byte đầu của digest (big-endian) mod 10⁶. Thứ tự cố định theo vai trò.
    """
    fp_a = _fingerprint_bytes(fp_initiator, "fp_initiator")
    fp_b = _fingerprint_bytes(fp_responder, "fp_responder")
    if fp_a == fp_b:
        raise ValueError("hai fingerprint giống nhau: không thể ghép đôi một máy với chính nó")
    n_a = _check_nonce(nonce_initiator, "nonce_initiator")
    n_b = _check_nonce(nonce_responder, "nonce_responder")
    digest = hashlib.sha256(_PAIR_LABEL + fp_a + fp_b + n_a + n_b).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 10**CODE_DIGITS:0{CODE_DIGITS}d}"


def proof_payload(fp_initiator: str, fp_responder: str, nonce_initiator: bytes, commitment: str) -> bytes:
    """Payload bên khởi xướng ký để chứng minh giữ khóa riêng (155 byte).

    `b"teleport-initiator-proof-v1" ‖ fp_khởi_xướng ‖ fp_trả_lời ‖ n_A ‖ C_B`, với C_B là
    `commitment` trong `pair_commit` của phiên này (đổi hex → 32 byte). Fingerprint hoặc
    `commitment` không phải 64 hex thường, hai fingerprint giống nhau, nonce khác 32 byte
    → ValueError.
    """
    fp_a = _fingerprint_bytes(fp_initiator, "fp_initiator")
    fp_b = _fingerprint_bytes(fp_responder, "fp_responder")
    if fp_a == fp_b:
        raise ValueError("hai fingerprint giống nhau: không thể ghép đôi một máy với chính nó")
    n_a = _check_nonce(nonce_initiator, "nonce_initiator")
    c_b = _fingerprint_bytes(commitment, "commitment")
    return _PROOF_LABEL + fp_a + fp_b + n_a + c_b


def format_code(code: str) -> str:
    """Tách mã thành hai nhóm cho dễ đọc: "048213" → "048 213"."""
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
        raise ValueError(f"mã phải là đúng {CODE_DIGITS} chữ số 0-9")
    return f"{code[:3]} {code[3:]}"
