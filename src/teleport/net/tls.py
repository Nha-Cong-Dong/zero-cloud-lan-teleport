"""SSLContext cho TLS 1.3 với chứng chỉ tự ký: ghim fingerprint và mTLS.

Chứng chỉ tự ký không được CA nào ký, nên không kiểm tra được theo kiểu HTTPS
thông thường. Có hai cơ chế thay thế:

- **Client kiểm tra server:** tắt kiểm tra CA rồi tự so fingerprint chứng chỉ của
  server với fingerprint đã lưu khi ghép đôi (`peer_fingerprint` + `verify_fingerprint`).
- **Server kiểm tra client (mTLS):** nạp chứng chỉ của các thiết bị tin cậy làm
  "CA". Mỗi chứng chỉ tự ký là trust anchor của chính nó, nên client gửi chứng
  chỉ lạ bị từ chối ngay trong lúc bắt tay.

Node chỉ mở **một cổng** (docs/protocol.md mục 2): `allow_anonymous=True` cho phép
client không gửi chứng chỉ; kết nối đó chỉ được dùng để ghép đôi.

Lưu ý TLS 1.3: client bị từ chối vẫn thấy `open_connection()` thành công; lỗi
chỉ lộ ra ở lần đọc đầu tiên (EOF hoặc lỗi SSL). Vì vậy client phải đọc được
`hello` của server rồi mới coi là đã kết nối.
"""

from __future__ import annotations

import asyncio
import hmac
import re
import ssl
from collections.abc import Iterable

from teleport.trust.identity import Identity, fingerprint_of_der

_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")


class FingerprintMismatch(Exception):
    pass


def server_context(
    identity: Identity, trusted_certs: Iterable[str], *, allow_anonymous: bool = False
) -> ssl.SSLContext:
    """Context phía server; `trusted_certs` là chứng chỉ PEM của các thiết bị tin cậy.

    Client gửi chứng chỉ thì chứng chỉ đó phải nằm trong `trusted_certs`, nếu không
    bắt tay thất bại (kể cả khi danh sách rỗng). Client không gửi chứng chỉ chỉ
    được nhận khi `allow_anonymous=True` (`CERT_OPTIONAL`); khi đó
    `peer_fingerprint` của kết nối raise ValueError.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(identity.cert_path, identity.key_path)
    ctx.verify_mode = ssl.CERT_OPTIONAL if allow_anonymous else ssl.CERT_REQUIRED
    ctx.verify_flags |= ssl.VERIFY_X509_STRICT
    # Không cấp session ticket: kết nối dùng lại session sẽ bỏ qua việc kiểm chứng chỉ
    # client, nên máy vừa bị hủy ghép đôi có thể vào lại bằng session cũ. Mỗi kết nối
    # đều bắt tay đầy đủ với danh sách tin cậy hiện tại (docs/protocol.md mục 2.2).
    ctx.num_tickets = 0
    if cadata := "".join(trusted_certs):
        ctx.load_verify_locations(cadata=cadata)
    return ctx


def client_context(identity: Identity | None = None) -> ssl.SSLContext:
    """Context phía client. Truyền `identity` để gửi chứng chỉ của mình cho server (mTLS).

    Không truyền `identity` là kết nối ẩn danh, chỉ dùng để ghép đôi.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # tự kiểm tra bằng verify_fingerprint() sau khi bắt tay
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


def verify_fingerprint(actual_fp: str, expected_fp: str) -> None:
    """Raise FingerprintMismatch nếu hai fingerprint (64 ký tự hex thường) khác nhau.

    Luôn so **toàn bộ** fingerprint, không so tiền tố. Giá trị không đúng dạng (kể cả
    chuỗi không phải ASCII) cũng là không khớp, để bên gọi chỉ phải bắt một loại lỗi.
    """
    for fp in (actual_fp, expected_fp):
        if not isinstance(fp, str) or not _FINGERPRINT_RE.fullmatch(fp):
            raise FingerprintMismatch("fingerprint phải là đúng 64 ký tự hex thường")
    # compare_digest so sánh trong thời gian không đổi, không lộ vị trí ký tự sai.
    if not hmac.compare_digest(actual_fp.encode("ascii"), expected_fp.encode("ascii")):
        raise FingerprintMismatch("fingerprint của máy bên kia KHÔNG khớp với fingerprint đã lưu")
