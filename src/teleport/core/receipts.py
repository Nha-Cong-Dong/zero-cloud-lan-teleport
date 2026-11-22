"""Biên lai truyền tệp có chữ ký số (`teleport-receipt-v1`, sans-IO).

Định dạng bất biến, xác thực độc lập hoàn toàn ở tầng lõi không chạm đĩa hay mạng:
mỗi lượt truyền hoàn tất tạo được một biên lai JSON chuẩn hóa mang Merkle root từng tệp
và chữ ký ECDSA P-256 trên payload chuẩn (`canonical_payload_bytes`) bằng khóa danh tính
đã ghép đôi của thiết bị ký.
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from teleport.core.messages import (
    MAX_CERT_PEM_LEN,
    MAX_OFFER_FILES,
    MAX_SAFE_INTEGER,
    MessageError,
    validate_device_id,
    validate_device_name,
    validate_rel_path,
    validate_transfer_id,
)
from teleport.core.resume_state import StateError, check_time, format_time, parse_time
from teleport.core.transfer import Direction

RECEIPT_SCHEMA = "teleport-receipt-v1"
MAX_RECEIPT_FILE = 4 * 1024 * 1024  # 4 MiB
MAX_RECEIPT_FILES = MAX_OFFER_FILES
DIGEST_HEX_LEN = 64
SIGNATURE_HEX_LEN = 128

_HEX_64_RE = re.compile(r"[0-9a-f]{64}")
_HEX_128_RE = re.compile(r"[0-9a-f]{128}")
_DIRECTIONS = frozenset(d.value for d in Direction)


class ReceiptError(ValueError):
    """Tham số không hợp lệ hoặc tệp biên lai bị hỏng / vượt trần."""


@dataclass(frozen=True, slots=True)
class ReceiptFile:
    """Thông tin một tệp đã hoàn tất và xác minh Merkle trong biên lai."""

    name: str
    size: int
    merkle_root: str


@dataclass(frozen=True, slots=True)
class ReceiptPayload:
    """Phần dữ liệu chuẩn hóa được ký trong biên lai truyền tệp."""

    schema: str
    transfer_id: str
    direction: Direction
    completed_at: datetime.datetime
    signer_device_id: str
    signer_fingerprint: str
    peer_device_id: str
    peer_name: str
    total_bytes: int
    files: tuple[ReceiptFile, ...]


@dataclass(frozen=True, slots=True)
class TransferReceipt:
    """Biên lai hoàn chỉnh gồm payload, chứng chỉ PEM của máy ký và chữ ký hex."""

    payload: ReceiptPayload
    signer_cert_pem: str
    signature_hex: str


def _check_hex(value: object, field: str, length: int) -> str:
    pattern = _HEX_64_RE if length == DIGEST_HEX_LEN else _HEX_128_RE
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ReceiptError(f"{field} phải là đúng {length} ký tự hex thường")
    return value if type(value) is str else str.__str__(value)


def _check_int(value: object, field: str, lo: int, hi: int) -> int:
    if type(value) is not int or not lo <= value <= hi:
        raise ReceiptError(f"{field} phải là số nguyên {lo}…{hi}")
    return value


def _check_direction(value: object) -> Direction:
    if isinstance(value, Direction):
        return value
    if isinstance(value, str) and value in _DIRECTIONS:
        return Direction(value)
    raise ReceiptError("direction phải là 'send' hoặc 'receive'")


def _check_file(item: object) -> ReceiptFile:
    name: object
    size: object
    root: object
    if isinstance(item, ReceiptFile):
        name, size, root = item.name, item.size, item.merkle_root
    elif isinstance(item, dict):
        name = item.get("name")
        size = item.get("size")
        root = item.get("merkle_root")
    else:
        raise ReceiptError("mục trong files phải là ReceiptFile hoặc object JSON")

    try:
        validated_name = validate_rel_path(name)
    except MessageError as e:
        raise ReceiptError(f"files[].name không hợp lệ: {e}") from None
    if not validated_name or "\\" in validated_name:
        raise ReceiptError("files[].name phải là đường dẫn tương đối khác rỗng dùng '/'")
    validated_size = _check_int(size, "files[].size", 0, MAX_SAFE_INTEGER)
    validated_root = _check_hex(root, "files[].merkle_root", DIGEST_HEX_LEN)
    return ReceiptFile(name=validated_name, size=validated_size, merkle_root=validated_root)


def _validate_payload(payload: ReceiptPayload) -> ReceiptPayload:
    if not isinstance(payload, ReceiptPayload):
        raise ReceiptError("payload phải là ReceiptPayload")
    if payload.schema != RECEIPT_SCHEMA:
        raise ReceiptError(f"schema phải là {RECEIPT_SCHEMA!r}")
    try:
        transfer_id = validate_transfer_id(payload.transfer_id)
        signer_id = validate_device_id(payload.signer_device_id)
        peer_id = validate_device_id(payload.peer_device_id)
        peer_name = validate_device_name(payload.peer_name)
    except MessageError as e:
        raise ReceiptError(str(e)) from None

    direction = _check_direction(payload.direction)
    try:
        completed_at = check_time(payload.completed_at, "completed_at")
    except StateError as e:
        raise ReceiptError(str(e)) from None

    signer_fp = _check_hex(payload.signer_fingerprint, "signer_fingerprint", DIGEST_HEX_LEN)

    if not isinstance(payload.files, Sequence) or isinstance(payload.files, str | bytes):
        raise ReceiptError("files phải là dãy ReceiptFile")
    if len(payload.files) > MAX_RECEIPT_FILES:
        raise ReceiptError(f"files vượt quá {MAX_RECEIPT_FILES} mục")
    files = tuple(_check_file(f) for f in payload.files)

    total_bytes = _check_int(payload.total_bytes, "total_bytes", 0, MAX_SAFE_INTEGER)
    expected_total = sum(f.size for f in files)
    if total_bytes != expected_total:
        raise ReceiptError("total_bytes không khớp tổng kích thước các tệp")

    return ReceiptPayload(
        schema=RECEIPT_SCHEMA,
        transfer_id=transfer_id,
        direction=direction,
        completed_at=completed_at,
        signer_device_id=signer_id,
        signer_fingerprint=signer_fp,
        peer_device_id=peer_id,
        peer_name=peer_name,
        total_bytes=total_bytes,
        files=files,
    )


def _payload_dict(payload: ReceiptPayload) -> dict[str, Any]:
    checked = _validate_payload(payload)
    return {
        "schema": checked.schema,
        "transfer_id": checked.transfer_id,
        "direction": checked.direction.value,
        "completed_at": format_time(checked.completed_at),
        "signer_device_id": checked.signer_device_id,
        "signer_fingerprint": checked.signer_fingerprint,
        "peer_device_id": checked.peer_device_id,
        "peer_name": checked.peer_name,
        "total_bytes": checked.total_bytes,
        "files": [{"name": f.name, "size": f.size, "merkle_root": f.merkle_root} for f in checked.files],
    }


def _check_cert_pem(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_CERT_PEM_LEN or not value.isascii():
        raise ReceiptError(f"signer_cert_pem phải là chuỗi ASCII 1–{MAX_CERT_PEM_LEN} ký tự")
    return value if type(value) is str else str.__str__(value)


def canonical_payload_bytes(payload: ReceiptPayload) -> bytes:
    """Chuỗi byte chuẩn hóa dùng làm đầu vào cho chữ ký ECDSA P-256."""
    body = json.dumps(_payload_dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"{RECEIPT_SCHEMA}\n{body}".encode()


def encode_receipt(receipt: TransferReceipt) -> bytes:
    """Mã hóa biên lai hoàn chỉnh thành JSON UTF-8 định dạng rõ ràng (<= MAX_RECEIPT_FILE)."""
    if not isinstance(receipt, TransferReceipt):
        raise ReceiptError("receipt phải là TransferReceipt")
    payload_obj = _payload_dict(receipt.payload)
    cert_pem = _check_cert_pem(receipt.signer_cert_pem)
    signature_hex = _check_hex(receipt.signature_hex, "signature_hex", SIGNATURE_HEX_LEN)

    doc: dict[str, Any] = {
        **payload_obj,
        "signer_cert_pem": cert_pem,
        "signature_hex": signature_hex,
    }
    encoded = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode()
    if len(encoded) > MAX_RECEIPT_FILE:
        raise ReceiptError(f"biên lai vượt quá giới hạn {MAX_RECEIPT_FILE} byte")
    return encoded


def _reject_constant(name: str) -> object:
    raise ReceiptError(f"JSON biên lai không được chứa {name}")


def decode_receipt(data: object) -> TransferReceipt:
    """Giải mã và kiểm tra chặt chẽ mọi trường của biên lai từ bytes JSON."""
    if not isinstance(data, bytes | bytearray | memoryview):
        raise ReceiptError("data phải là bytes")
    with memoryview(data) as view:
        if view.nbytes > MAX_RECEIPT_FILE:
            raise ReceiptError(f"biên lai dài quá {MAX_RECEIPT_FILE} byte")
    raw = bytes(data)
    try:
        doc = json.loads(raw.decode("utf-8-sig"), parse_constant=_reject_constant)
    except ReceiptError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise ReceiptError("không phân tích được JSON biên lai") from e
    if not isinstance(doc, dict):
        raise ReceiptError("biên lai JSON phải là object")

    schema = doc.get("schema")
    if schema != RECEIPT_SCHEMA:
        raise ReceiptError(f"schema biên lai không được hỗ trợ: {schema!r}")

    raw_files = doc.get("files")
    if not isinstance(raw_files, list):
        raise ReceiptError("files phải là mảng JSON")
    if len(raw_files) > MAX_RECEIPT_FILES:
        raise ReceiptError(f"files vượt quá {MAX_RECEIPT_FILES} mục")

    try:
        completed_at = parse_time(doc.get("completed_at"), "completed_at")
    except StateError as e:
        raise ReceiptError(str(e)) from None

    payload = ReceiptPayload(
        schema=schema,
        transfer_id=doc.get("transfer_id"),  # type: ignore[arg-type]
        direction=_check_direction(doc.get("direction")),
        completed_at=completed_at,
        signer_device_id=doc.get("signer_device_id"),  # type: ignore[arg-type]
        signer_fingerprint=doc.get("signer_fingerprint"),  # type: ignore[arg-type]
        peer_device_id=doc.get("peer_device_id"),  # type: ignore[arg-type]
        peer_name=doc.get("peer_name"),  # type: ignore[arg-type]
        total_bytes=doc.get("total_bytes"),  # type: ignore[arg-type]
        files=tuple(_check_file(item) for item in raw_files),
    )
    checked_payload = _validate_payload(payload)
    cert_pem = _check_cert_pem(doc.get("signer_cert_pem"))
    signature_hex = _check_hex(doc.get("signature_hex"), "signature_hex", SIGNATURE_HEX_LEN)
    return TransferReceipt(payload=checked_payload, signer_cert_pem=cert_pem, signature_hex=signature_hex)
