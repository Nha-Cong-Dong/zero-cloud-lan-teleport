"""SSLContext cho TLS 1.3 với chứng chỉ tự ký: ghim fingerprint và mTLS.

Chứng chỉ tự ký không được CA nào ký, nên không kiểm tra được theo kiểu HTTPS
thông thường. Có hai cơ chế thay thế:

- **Client kiểm tra server:** tắt kiểm tra CA rồi tự so fingerprint chứng chỉ của
  server với giá trị đã ghim (`peer_fingerprint` + `verify_pin`).
- **Server kiểm tra client (mTLS):** nạp chứng chỉ của các thiết bị tin cậy làm
  "CA". Mỗi chứng chỉ tự ký là trust anchor của chính nó, nên client lạ bị từ
  chối ngay trong lúc bắt tay.

Lưu ý TLS 1.3: client bị từ chối vẫn thấy `open_connection()` thành công; lỗi
chỉ lộ ra ở lần đọc đầu tiên (EOF hoặc lỗi SSL). Vì vậy client phải đọc được
`hello` của server rồi mới coi là đã kết nối.
"""

from __future__ import annotations

import asyncio
import hmac
import ssl
from collections.abc import Iterable

from teleport.trust.identity import Identity, fingerprint_of_der

# Pin 8 byte (16 ký tự hex): kẻ tấn công phải tìm một chứng chỉ khác trùng 64 bit
# đầu SHA-256. Dùng cho nhập tay; ghép đôi trong app dùng mã 6 số (protocol.md mục 5).
MIN_PIN_HEX = 16


class FingerprintMismatch(Exception):
    pass


def server_context(identity: Identity, trusted_certs: Iterable[str] | None = None) -> ssl.SSLContext:
    """Context phía server.

    `trusted_certs=None`: không yêu cầu chứng chỉ client (chỉ dùng cho bước ghép
    đôi, khi chưa biết bên kia). Có giá trị (kể cả rỗng): bắt buộc mTLS, chỉ chấp
    nhận client có chứng chỉ nằm trong danh sách PEM này.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(identity.cert_path, identity.key_path)
    if trusted_certs is not None:
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.verify_flags |= ssl.VERIFY_X509_STRICT
        if cadata := "".join(trusted_certs):
            ctx.load_verify_locations(cadata=cadata)
    return ctx


def client_context(identity: Identity | None = None) -> ssl.SSLContext:
    """Context phía client. Truyền `identity` để gửi chứng chỉ của mình cho server (mTLS)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # tự kiểm tra bằng verify_pin() sau khi bắt tay
    if identity is not None:
        ctx.load_cert_chain(identity.cert_path, identity.key_path)
    return ctx


def peer_fingerprint(writer: asyncio.StreamWriter) -> str:
    ssl_obj: ssl.SSLObject | None = writer.get_extra_info("ssl_object")
    if ssl_obj is None:
        raise ValueError("kết nối không dùng TLS")
    der = ssl_obj.getpeercert(binary_form=True)
    if der is None:
        raise ValueError("bên kia không gửi chứng chỉ")
    return fingerprint_of_der(der)


def normalize_pin(pin: str) -> str:
    return pin.replace(":", "").replace(" ", "").lower()


def verify_pin(actual_fp: str, pin: str) -> None:
    """Raise FingerprintMismatch nếu `pin` không phải tiền tố của `actual_fp`.

    `pin` có thể viết hoa/thường, có hoặc không có dấu ":".
    """
    pin = normalize_pin(pin)
    if not all(c in "0123456789abcdef" for c in pin):
        raise ValueError("pin chỉ được chứa ký tự hex (0-9, a-f) và dấu ':'")
    if len(pin) < MIN_PIN_HEX:
        raise ValueError(f"pin quá ngắn: cần ít nhất {MIN_PIN_HEX} ký tự hex")
    # compare_digest so sánh trong thời gian không đổi, không lộ vị trí ký tự sai.
    if not hmac.compare_digest(actual_fp[: len(pin)], pin):
        raise FingerprintMismatch("fingerprint của máy bên kia KHÔNG khớp với pin")
