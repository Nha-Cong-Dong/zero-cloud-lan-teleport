"""Tạo, lưu và xác minh biên lai truyền tệp có chữ ký số ở tầng ứng dụng (`teleport-receipt-v1`)."""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import os
import stat
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from teleport import atomic
from teleport.core.blocks import BLOCK_SIZE
from teleport.core.merkle import MerkleStream
from teleport.core.messages import MessageError, validate_transfer_id
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
from teleport.paths import TRUSTED_FILE
from teleport.trust.identity import (
    CERT_FILE,
    Identity,
    InvalidCertificate,
    peer_cert_from_pem,
    sign_payload,
    verify_signature,
)
from teleport.trust.store import TrustStore, TrustStoreError

RECEIPTS_DIR = "receipts"
RECEIPT_EXT = ".teleport-receipt.json"

_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


@dataclass(frozen=True, slots=True)
class ReceiptVerification:
    """Kết quả xác minh chữ ký và (tùy chọn) tính toàn vẹn tệp của một biên lai."""

    valid: bool
    receipt: TransferReceipt | None
    signature_ok: bool
    trusted_signer: bool | None
    files_checked: int
    files_ok: int
    errors: tuple[str, ...]


def receipts_dir(config_dir: Path) -> Path:
    """Thư mục lưu biên lai trong `<config_dir>`."""
    return Path(config_dir) / RECEIPTS_DIR


def receipt_filename(transfer_id: str, direction: Direction) -> str:
    """Tên tệp biên lai chuẩn hóa theo `transfer_id` và `direction`."""
    try:
        tid = validate_transfer_id(transfer_id)
    except MessageError as e:
        raise ReceiptError(str(e)) from None
    dir_val = Direction(direction).value
    return f"{tid}_{dir_val}{RECEIPT_EXT}"


def receipt_path_for(config_dir: Path, transfer_id: str, direction: Direction) -> Path:
    """Đường dẫn đầy đủ của tệp biên lai cho một lượt truyền."""
    return receipts_dir(config_dir) / receipt_filename(transfer_id, direction)


def list_receipts(config_dir: Path) -> list[Path]:
    """Danh sách tệp biên lai trong `<config_dir>/receipts`, mới nhất đứng trước."""
    folder = receipts_dir(config_dir)
    if not folder.is_dir():
        return []
    found: list[tuple[float, str, Path]] = []
    try:
        for entry in folder.iterdir():
            if not entry.name.endswith(RECEIPT_EXT):
                continue
            try:
                st = entry.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            found.append((st.st_mtime, entry.name, entry))
    except OSError:
        return []
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in found]


def _read_limited(path: Path) -> bytes:
    """Đọc tối đa `MAX_RECEIPT_FILE + 1` byte của file thường, không theo symlink."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise ReceiptError("Tệp biên lai không phải file thường")
    fd = os.open(path, _READ_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ReceiptError("Tệp biên lai không phải file thường")
        chunks: list[bytes] = []
        remaining = MAX_RECEIPT_FILE + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > MAX_RECEIPT_FILE:
        raise ReceiptError(f"Tệp biên lai dài quá {MAX_RECEIPT_FILE} byte")
    return data


def _hash_file_merkle(path: Path) -> tuple[int, str]:
    """Đọc tệp thường theo khối 1 MiB (O(log N) RAM), trả `(size, merkle_root_hex)`."""
    fd = os.open(path, _READ_FLAGS)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ReceiptError("Mục kiểm tra không phải file thường")
        stream = MerkleStream()
        total = 0
        sha256 = hashlib.sha256
        while True:
            chunk = bytearray()
            while len(chunk) < BLOCK_SIZE:
                part = os.read(fd, BLOCK_SIZE - len(chunk))
                if not part:
                    break
                chunk.extend(part)
            if not chunk:
                break
            total += len(chunk)
            stream.feed(sha256(chunk).digest())
        return total, stream.root().hex()
    finally:
        os.close(fd)


def create_receipt(
    identity: Identity,
    *,
    transfer_id: str,
    direction: Direction,
    completed_at: datetime.datetime,
    peer_device_id: str,
    peer_name: str,
    files: Sequence[ReceiptFile],
) -> TransferReceipt:
    """Dựng và ký biên lai bằng khóa ECDSA P-256 của thiết bị hiện tại."""
    file_tuple = tuple(files)
    payload = ReceiptPayload(
        schema=RECEIPT_SCHEMA,
        transfer_id=transfer_id,
        direction=direction,
        completed_at=completed_at,
        signer_device_id=identity.device_id,
        signer_fingerprint=identity.fingerprint,
        peer_device_id=peer_device_id,
        peer_name=peer_name,
        total_bytes=sum(f.size for f in file_tuple),
        files=file_tuple,
    )
    signature = sign_payload(identity, canonical_payload_bytes(payload))
    return TransferReceipt(
        payload=payload,
        signer_cert_pem=identity.cert_pem(),
        signature_hex=signature.hex(),
    )


def save_receipt(path: Path, receipt: TransferReceipt) -> Path:
    """Ghi nguyên tử biên lai ra đĩa (`atomic.write_atomic`)."""
    encoded = encode_receipt(receipt)
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_atomic(dest, encoded)
    return dest


def load_receipt(path: Path) -> TransferReceipt:
    """Đọc và giải mã an toàn một tệp biên lai."""
    data = _read_limited(Path(path))
    return decode_receipt(data)


def _has_symlink_component(root: Path, candidate: Path) -> bool:
    rel = candidate.relative_to(root)
    cur = root
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            return True
    return False


def verify_receipt(
    path: Path,
    *,
    files_dir: Path | None = None,
    trusted_fingerprints: Collection[str] | None = None,
) -> ReceiptVerification:
    """Xác minh chữ ký biên lai và tùy chọn đối chiếu tệp thật trong `files_dir`."""
    errors: list[str] = []
    try:
        receipt = load_receipt(path)
    except (OSError, ReceiptError) as e:
        msg = str(e) if isinstance(e, ReceiptError) else f"Không đọc được tệp biên lai ({type(e).__name__})"
        return ReceiptVerification(
            valid=False,
            receipt=None,
            signature_ok=False,
            trusted_signer=None,
            files_checked=0,
            files_ok=0,
            errors=(msg,),
        )

    payload = receipt.payload
    signature_ok = False
    trusted_signer: bool | None = None

    try:
        peer = peer_cert_from_pem(receipt.signer_cert_pem)
    except InvalidCertificate as e:
        errors.append(f"Chứng chỉ máy ký không hợp lệ: {e}")
        peer = None

    if peer is not None:
        if peer.device_id != payload.signer_device_id:
            errors.append("signer_device_id không khớp chứng chỉ máy ký")
        if peer.fingerprint != payload.signer_fingerprint:
            errors.append("signer_fingerprint không khớp chứng chỉ máy ký")

        try:
            canonical = canonical_payload_bytes(payload)
            sig_bytes = bytes.fromhex(receipt.signature_hex)
            signature_ok = verify_signature(peer, canonical, sig_bytes)
        except (ReceiptError, ValueError):
            signature_ok = False

        if not signature_ok:
            errors.append("Chữ ký số của biên lai không hợp lệ")
        elif trusted_fingerprints is not None:
            trusted_signer = peer.fingerprint in trusted_fingerprints

    files_checked = 0
    files_ok = 0
    if files_dir is not None:
        base_dir = Path(files_dir).resolve()
        if not base_dir.is_dir():
            errors.append("Thư mục tệp cần kiểm tra không tồn tại")
        else:
            for item in payload.files:
                files_checked += 1
                candidate = base_dir.joinpath(*item.name.split("/"))
                try:
                    resolved = candidate.resolve()
                except OSError:
                    errors.append(f"Không truy cập được tệp: {item.name}")
                    continue
                if not resolved.is_relative_to(base_dir) or _has_symlink_component(base_dir, candidate):
                    errors.append(f"Đường dẫn tệp vượt ngoài thư mục gốc: {item.name}")
                    continue
                if not resolved.exists():
                    errors.append(f"Thiếu tệp: {item.name}")
                    continue
                try:
                    actual_size, actual_root = _hash_file_merkle(resolved)
                except (OSError, ReceiptError):
                    errors.append(f"Không đọc được nội dung tệp: {item.name}")
                    continue
                if actual_size != item.size:
                    errors.append(f"Sai kích thước tệp: {item.name}")
                    continue
                if actual_root != item.merkle_root:
                    errors.append(f"Sai mã kiểm tra Merkle: {item.name}")
                    continue
                files_ok += 1

    valid = signature_ok and not errors and (files_dir is None or files_ok == len(payload.files))
    return ReceiptVerification(
        valid=valid,
        receipt=receipt,
        signature_ok=signature_ok,
        trusted_signer=trusted_signer,
        files_checked=files_checked,
        files_ok=files_ok,
        errors=tuple(errors),
    )


def known_trusted_fingerprints(config_dir: Path) -> set[str] | None:
    """Thu thập fingerprint của máy này và danh sách thiết bị tin cậy (không tạo file mới)."""
    cfg = Path(config_dir)
    if not cfg.is_dir():
        return None
    cert_path = cfg / CERT_FILE
    trusted_path = cfg / TRUSTED_FILE
    if not cert_path.is_file() and not trusted_path.is_file():
        return None

    fps: set[str] = set()
    if cert_path.is_file():
        with contextlib.suppress(OSError, UnicodeDecodeError, InvalidCertificate):
            fps.add(peer_cert_from_pem(cert_path.read_text(encoding="ascii")).fingerprint)
    if trusted_path.is_file():
        with contextlib.suppress(TrustStoreError):
            for dev in TrustStore.load(cfg).devices():
                fps.add(dev.fingerprint)
    return fps
