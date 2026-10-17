"""SSLContext cho TLS 1.3 với chứng chỉ tự ký và ghim fingerprint.

Chứng chỉ tự ký không được CA nào ký, nên không kiểm tra được theo kiểu HTTPS
thông thường. Thay vào đó ta tắt kiểm tra CA rồi tự so fingerprint chứng chỉ của
bên kia với giá trị đã biết trước (certificate pinning). Kênh vẫn được mã hóa
đầy đủ bằng TLS 1.3; pinning để chắc chắn đầu bên kia đúng là máy ta muốn nói
chuyện, không phải kẻ đứng giữa (MITM).
"""

from __future__ import annotations

import asyncio
import hmac
import ssl

from .identity import Identity, fingerprint_of_der

# Ghim bằng 8 byte (16 ký tự hex) là đủ cho bài tập: kẻ tấn công phải tìm một
# chứng chỉ khác trùng 64 bit đầu SHA-256. Giai đoạn 1 sẽ thay bằng mã 6 số.
MIN_PIN_HEX = 16


class FingerprintMismatch(Exception):
    pass


def server_context(identity: Identity) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(identity.cert_path, identity.key_path)
    return ctx


def client_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # tự kiểm tra bằng verify_pin() sau khi bắt tay
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
