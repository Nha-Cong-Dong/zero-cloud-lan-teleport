"""Kiểm thử định dạng biên lai truyền tệp sans-IO (`teleport.core.receipts`)."""

from __future__ import annotations

import datetime
import hashlib
import json

import pytest

from teleport.core.receipts import (
    MAX_RECEIPT_FILE,
    RECEIPT_SCHEMA,
    ReceiptError,
    ReceiptFile,
    ReceiptPayload,
    TransferReceipt,
    canonical_payload_bytes,
    decode_receipt,
    encode_receipt,
)
from teleport.core.transfer import Direction


def _sample_payload(**overrides: object) -> ReceiptPayload:
    root_a = hashlib.sha256(b"alpha").hexdigest()
    root_b = hashlib.sha256(b"beta").hexdigest()
    defaults: dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "transfer_id": "a1" * 16,
        "direction": Direction.SEND,
        "completed_at": datetime.datetime(2026, 11, 22, 10, 30, 5, 123456, tzinfo=datetime.UTC),
        "signer_device_id": "b2" * 16,
        "signer_fingerprint": "c3" * 32,
        "peer_device_id": "d4" * 16,
        "peer_name": "May-Nhan",
        "total_bytes": 150,
        "files": (
            ReceiptFile(name="doc.txt", size=100, merkle_root=root_a),
            ReceiptFile(name="sub/img.png", size=50, merkle_root=root_b),
        ),
    }
    defaults.update(overrides)
    return ReceiptPayload(**defaults)  # type: ignore[arg-type]


def _sample_receipt(**payload_overrides: object) -> TransferReceipt:
    payload = _sample_payload(**payload_overrides)
    return TransferReceipt(
        payload=payload,
        signer_cert_pem="-----BEGIN CERTIFICATE-----\nFAKE\n-----END CERTIFICATE-----\n",
        signature_hex="e4" * 64,
    )


def test_receipt_roundtrip_and_canonical_bytes_determinism() -> None:
    p1 = _sample_payload()
    p2 = _sample_payload(
        completed_at=datetime.datetime(
            2026,
            11,
            22,
            17,
            30,
            5,
            999999,
            tzinfo=datetime.timezone(datetime.timedelta(hours=7)),
        )
    )
    assert canonical_payload_bytes(p1) == canonical_payload_bytes(p2)
    assert canonical_payload_bytes(p1).startswith(b"teleport-receipt-v1\n")

    encoded = encode_receipt(_sample_receipt())
    decoded = decode_receipt(encoded)
    assert decoded.payload.schema == RECEIPT_SCHEMA
    assert decoded.payload.transfer_id == "a1" * 16
    assert decoded.payload.direction is Direction.SEND
    assert decoded.payload.completed_at == datetime.datetime(2026, 11, 22, 10, 30, 5, tzinfo=datetime.UTC)
    assert decoded.payload.total_bytes == 150
    assert len(decoded.payload.files) == 2
    assert decoded.signature_hex == "e4" * 64


def test_receipt_rejects_bad_schema_and_tampered_fields() -> None:
    raw = json.loads(encode_receipt(_sample_receipt()).decode("utf-8"))

    for mutator in [
        lambda d: d.__setitem__("schema", "teleport-receipt-v2"),
        lambda d: d.__setitem__("transfer_id", "bad"),
        lambda d: d.__setitem__("direction", "both"),
        lambda d: d.__setitem__("total_bytes", 999),
        lambda d: d.__setitem__("signature_hex", "ff" * 10),
        lambda d: d.__setitem__("signer_fingerprint", "zz" * 32),
        lambda d: d["files"][0].__setitem__("name", "../evil.txt"),
        lambda d: d["files"][0].__setitem__("name", ""),
        lambda d: d["files"][0].__setitem__("size", -1),
        lambda d: d.__setitem__("signer_cert_pem", "tiếng việt"),
    ]:
        doc = json.loads(json.dumps(raw))
        mutator(doc)
        with pytest.raises(ReceiptError):
            decode_receipt(json.dumps(doc).encode())


def test_receipt_size_cap_and_json_constants() -> None:
    with pytest.raises(ReceiptError):
        decode_receipt(b"{" + b" " * MAX_RECEIPT_FILE + b"}")
    with pytest.raises(ReceiptError):
        decode_receipt(b'{"total_bytes": NaN}')
