"""Các nhóm lệnh mở rộng của CLI (`transfers`, `guest`, `receipt`, `routing`, `watch`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from teleport import __version__
from teleport.app import node as app_node
from teleport.app.devices import AmbiguousDevice
from teleport.app.ipc import IpcClientError, IpcProtocolError
from teleport.app.node import (
    AmbiguousTransfer,
    ConfigDirError,
    DeviceNotFound,
    PendingTransfer,
    TransferNotFound,
    cancel_pending_offline,
    find_trusted,
    list_pending_offline,
)
from teleport.app.settings import Settings
from teleport.app.storage import PathTooLongError, UnsafeReceiveDirError
from teleport.cli.messages import (
    _NODE_LOCKED_HINT,
    _config,
    _config_dir_error,
    _name_label,
    _Output,
    _trusted_lookup_error,
    _utc_now,
)
from teleport.core.display import format_size
from teleport.core.routing import (
    MAX_ROUTING_RULES,
    RoutingRule,
    parse_patterns,
    routing_rule_to_dict,
)
from teleport.core.watch import MAX_WATCH_PORTALS, WatchPortalRule, watch_portal_to_dict
from teleport.lock import NodeLock, NodeLockedError
from teleport.trust.identity import IdentityError
from teleport.trust.store import TrustStoreError

_TRANSFER_QUERY_RE = re.compile(r"^[0-9a-fA-F]{4,32}$")
_TRANSFER_STATE_LABELS = {
    "queued": "đang chờ",
    "waiting": "chờ xác nhận",
    "transferring": "đang truyền",
    "verifying": "đang kiểm tra",
    "reconnecting": "đang nối lại",
    "pending": "đang tạm dừng",
}


def _cli_syntax_error(args: argparse.Namespace, message: str) -> int:
    parser = getattr(args, "_parser", None)
    if parser is not None:
        parser.error(message)
    print(f"Lỗi: {message}", file=sys.stderr, flush=True)
    return 2


def _dirs_match(first: Path, second: str) -> bool:
    if not second:
        return False
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def _pending_to_dict(item: PendingTransfer) -> dict[str, Any]:
    return {
        "direction": "receive",
        "device_id": item.device_id,
        "device_name": item.device_name,
        "transfer_id": item.transfer_id,
        "state": "pending",
        "files_total": item.files_total,
        "files_done": item.files_done,
        "bytes_done": item.bytes_done,
        "total_bytes": item.total_bytes,
    }


def _render_transfers_list(output: _Output, items: list[dict[str, Any]]) -> None:
    if output.json:
        print(json.dumps({"time": _utc_now(), "transfers": items}, ensure_ascii=False), flush=True)
        return
    if not items:
        print("Không có lượt truyền nào đang dở.", flush=True)
        return
    for item in items:
        direction = "Nhận từ" if item.get("direction") == "receive" else "Gửi tới"
        name = _name_label(str(item.get("device_name") or item.get("device_id", "")[:8]))
        tid = str(item.get("transfer_id", ""))[:8]
        files_done = int(item.get("files_done", 0))
        files_total = int(item.get("files_total", 0))
        bytes_done = format_size(int(item.get("bytes_done", 0)))
        total_bytes = format_size(int(item.get("total_bytes", 0)))
        state = _TRANSFER_STATE_LABELS.get(str(item.get("state", "")), str(item.get("state", "")))
        print(
            f"- {tid} | {direction} {name} | {files_done}/{files_total} tệp "
            f"({bytes_done} / {total_bytes}) | {state}",
            flush=True,
        )


def _render_transfers_cancelled(output: _Output, cancelled: list[dict[str, Any]]) -> None:
    if output.json:
        print(json.dumps({"time": _utc_now(), "cancelled": cancelled}, ensure_ascii=False), flush=True)
        return
    if not cancelled:
        print("Không có lượt truyền nào đang dở.", flush=True)
        return
    for item in cancelled:
        direction = "nhận từ" if item.get("direction") == "receive" else "gửi tới"
        name = _name_label(str(item.get("device_name") or item.get("device_id", "")[:8]))
        tid = str(item.get("transfer_id", ""))[:8]
        print(f"Đã hủy lượt {tid} ({direction} {name}).", flush=True)


async def _transfers_list_ipc(
    directory: Path, recv_dir: Path | None
) -> tuple[bool, list[dict[str, Any]] | None, str]:
    import teleport.cli as _cli

    ipc_client = await _cli.try_connect_ipc(directory)
    if ipc_client is None:
        return False, None, ""
    try:
        res = await ipc_client.list_transfers()
        if recv_dir is not None and not _dirs_match(recv_dir, str(res.get("receive_dir", ""))):
            return True, None, "không thể dùng --dir khác với thư mục nhận của Teleport đang chạy"
        return True, list(res.get("transfers", [])), ""
    finally:
        await ipc_client.close()


async def _transfers_cancel_ipc(
    directory: Path,
    recv_dir: Path | None,
    query: str | None,
    cancel_all: bool,
) -> tuple[bool, dict[str, Any] | None, str]:
    import teleport.cli as _cli

    ipc_client = await _cli.try_connect_ipc(directory)
    if ipc_client is None:
        return False, None, ""
    try:
        if recv_dir is not None:
            info_res = await ipc_client.list_transfers()
            if not _dirs_match(recv_dir, str(info_res.get("receive_dir", ""))):
                return True, None, "không thể dùng --dir khác với thư mục nhận của Teleport đang chạy"
        res = await ipc_client.cancel_transfer(query, cancel_all=cancel_all)
        return True, res, ""
    finally:
        await ipc_client.close()


def cmd_transfers_list(args: argparse.Namespace) -> int:
    output = _Output(sys.stdout, sys.stderr, bool(getattr(args, "json", False)))
    directory = _config(args)
    raw_dir = getattr(args, "dir", None)
    if raw_dir is not None and not raw_dir.strip():
        return _cli_syntax_error(args, "--dir không được rỗng")
    recv_dir = Path(raw_dir).expanduser() if raw_dir is not None else None

    try:
        with asyncio.Runner() as runner:
            connected, ipc_items, syntax_err = runner.run(_transfers_list_ipc(directory, recv_dir))
    except (TimeoutError, IpcProtocolError, IpcClientError, OSError, json.JSONDecodeError) as e:
        output.error("ipc_error", f"Không thể giao tiếp với Teleport đang chạy: {e}")
        return 1
    if connected:
        if syntax_err:
            return _cli_syntax_error(args, syntax_err)
        _render_transfers_list(output, ipc_items or [])
        return 0

    try:
        pending = list_pending_offline(directory, receive_dir=recv_dir)
    except ConfigDirError as e:
        _config_dir_error(output, e.path, e.error)
        return 1
    except OSError as e:
        _config_dir_error(output, directory, e)
        return 1
    except NodeLockedError:
        output.error("node_locked", "Teleport đang chạy với thư mục cấu hình này.", extra=[_NODE_LOCKED_HINT])
        return 1
    except IdentityError as e:
        output.error("identity_error", str(e))
        return 1
    except TrustStoreError as e:
        output.error("trust_store_error", str(e))
        return 1
    except (UnsafeReceiveDirError, PathTooLongError) as e:
        output.error("receive_dir_error", str(e))
        return 1

    _render_transfers_list(output, [_pending_to_dict(item) for item in pending])
    return 0


def cmd_transfers_cancel(args: argparse.Namespace) -> int:
    output = _Output(sys.stdout, sys.stderr, bool(getattr(args, "json", False)))
    raw_query = getattr(args, "transfer", None)
    cancel_all = bool(getattr(args, "cancel_all", False))
    if (raw_query is None and not cancel_all) or (raw_query is not None and cancel_all):
        return _cli_syntax_error(args, "cần chỉ định <mã lượt> hoặc --all (không dùng cả hai)")
    query: str | None = None
    if raw_query is not None:
        query = raw_query.strip()
        if not _TRANSFER_QUERY_RE.fullmatch(query):
            return _cli_syntax_error(args, "mã lượt phải gồm ít nhất 4 ký tự hex (0-9, a-f)")

    directory = _config(args)
    raw_dir = getattr(args, "dir", None)
    if raw_dir is not None and not raw_dir.strip():
        return _cli_syntax_error(args, "--dir không được rỗng")
    recv_dir = Path(raw_dir).expanduser() if raw_dir is not None else None

    try:
        with asyncio.Runner() as runner:
            connected, res, syntax_err = runner.run(
                _transfers_cancel_ipc(directory, recv_dir, query, cancel_all)
            )
    except (TimeoutError, IpcProtocolError, IpcClientError, OSError, json.JSONDecodeError) as e:
        output.error("ipc_error", f"Không thể giao tiếp với Teleport đang chạy: {e}")
        return 1
    if connected:
        if syntax_err:
            return _cli_syntax_error(args, syntax_err)
        assert res is not None
        if "error" in res:
            err = str(res["error"])
            if err == "transfer_not_found":
                output.error("transfer_not_found", f"Không tìm thấy lượt truyền nào khớp {query!r}.")
                return 1
            if err == "ambiguous_transfer":
                output.error(
                    "ambiguous_transfer",
                    f"Có nhiều lượt truyền khớp {query!r}; hãy nhập thêm ký tự.",
                )
                return 1
            if err == "invalid_target":
                return _cli_syntax_error(args, "mã lượt phải gồm ít nhất 4 ký tự hex (0-9, a-f)")
            output.error(err, str(res.get("detail") or err))
            return 1
        _render_transfers_cancelled(output, list(res.get("cancelled", [])))
        return 0

    try:
        cancelled_items = cancel_pending_offline(
            directory, query, cancel_all=cancel_all, receive_dir=recv_dir
        )
    except ConfigDirError as e:
        _config_dir_error(output, e.path, e.error)
        return 1
    except OSError as e:
        _config_dir_error(output, directory, e)
        return 1
    except NodeLockedError:
        output.error("node_locked", "Teleport đang chạy với thư mục cấu hình này.", extra=[_NODE_LOCKED_HINT])
        return 1
    except IdentityError as e:
        output.error("identity_error", str(e))
        return 1
    except TrustStoreError as e:
        output.error("trust_store_error", str(e))
        return 1
    except (UnsafeReceiveDirError, PathTooLongError) as e:
        output.error("receive_dir_error", str(e))
        return 1
    except TransferNotFound:
        output.error("transfer_not_found", f"Không tìm thấy lượt truyền nào khớp {query!r}.")
        return 1
    except AmbiguousTransfer:
        output.error(
            "ambiguous_transfer",
            f"Có nhiều lượt truyền khớp {query!r}; hãy nhập thêm ký tự.",
        )
        return 1

    cancelled = [
        {
            "direction": "receive",
            "device_id": item.device_id,
            "device_name": item.device_name,
            "transfer_id": item.transfer_id,
        }
        for item in cancelled_items
    ]
    _render_transfers_cancelled(output, cancelled)
    return 0


# ---- Chế độ khách (Web Guest) --------------------------------------------------------------


async def run_guest(args: argparse.Namespace, stdout: Any = sys.stdout, stderr: Any = sys.stderr) -> int:
    from teleport.app.web_guest import WebGuestManager, make_wifi_qr
    from teleport.core.display import format_size
    from teleport.net.web_guest import GuestScope
    from teleport.paths import default_receive_dir

    recv_dir = Path(args.receive_dir).expanduser().resolve() if args.receive_dir else default_receive_dir()

    shared_paths: list[Path] = []
    if args.files:
        for f in args.files:
            p = Path(f).expanduser().resolve()
            if not (p.is_file() or p.is_dir()):
                print(f"Lỗi: Không tìm thấy tệp hoặc không phải tệp/thư mục hợp lệ: {f}", file=stderr)
                return 2
            shared_paths.append(p)

    wifi_payload: str | None = None
    wifi_ascii: str | None = None
    wifi_ssid = getattr(args, "wifi_ssid", None)
    if wifi_ssid:
        try:
            wifi_payload, _, wifi_ascii, _ = make_wifi_qr(
                wifi_ssid,
                getattr(args, "wifi_password", "") or "",
                security=getattr(args, "wifi_security", "WPA"),
            )
        except ValueError as e:
            print(f"Lỗi cấu hình Wi-Fi: {e}", file=stderr)
            return 2

    scope = GuestScope(getattr(args, "scope", "both"))
    one_shot = bool(getattr(args, "one_shot", False))
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_upload_start(name: str, total: int) -> None:
        if not args.json:
            total_str = f" ({format_size(total)})" if total > 0 else ""
            print(f"\n[Điện thoại tải lên] Bắt đầu nhận: {name}{total_str}...", file=stdout, flush=True)

    def on_upload_complete(name: str, size: int, path: Path) -> None:
        if args.json:
            data = {"event": "uploaded", "filename": name, "size": size, "path": str(path)}
            print(json.dumps(data, ensure_ascii=False), file=stdout, flush=True)
        else:
            print(
                f"\n[Điện thoại tải lên] Hoàn tất nhận: {name} ({format_size(size)})\n-> Đã lưu tại: {path}",
                file=stdout,
                flush=True,
            )

    def on_text_received(text: str) -> None:
        if args.json:
            data = {"event": "guest_text", "text": text}
            print(json.dumps(data, ensure_ascii=False), file=stdout, flush=True)
        else:
            print(f"\n[Điện thoại gửi chữ] {text}", file=stdout, flush=True)

    def on_session_timeout() -> None:
        if one_shot:
            loop.call_later(0.25, stop_event.set)
        else:
            stop_event.set()

    manager = WebGuestManager(
        receive_dir=recv_dir,
        inactivity_timeout=args.timeout,
        on_upload_start=on_upload_start,
        on_upload_complete=on_upload_complete,
        on_session_timeout=on_session_timeout,
        on_text_received=on_text_received,
    )

    try:
        info = await manager.start_session(
            shared_files=shared_paths,
            preferred_ip=args.ip,
            port=args.port,
            scope=scope,
            one_shot=one_shot,
        )
    except Exception as e:
        print(f"Lỗi khởi động máy chủ Web khách: {e}", file=stderr)
        return 1

    if args.json:
        data: dict[str, Any] = {
            "event": "guest_started",
            "url": info.url,
            "token": info.token,
            "ip": info.local_ip,
            "port": info.port,
            "files": [p.name for p in shared_paths],
            "receive_dir": str(recv_dir),
            "scope": info.scope.value,
            "one_shot": info.one_shot,
        }
        if wifi_payload is not None:
            data["wifi_qr"] = wifi_payload
        print(json.dumps(data, ensure_ascii=False), file=stdout, flush=True)
    else:
        print("=" * 64, file=stdout)
        print(f"ZCL Teleport - Chế độ khách qua Web QR (v{__version__})", file=stdout)
        print("=" * 64, file=stdout)
        if wifi_ascii is not None and wifi_ssid:
            print(f"\n1. Quét mã QR sau để nối Wi-Fi ({wifi_ssid}):", file=stdout)
            print(wifi_ascii, file=stdout)
            print("2. Sau đó quét mã QR dưới để mở trang truyền tệp:", file=stdout)
        print(info.qr_ascii, file=stdout)
        print(f"\nLiên kết kết nối: {info.url}", file=stdout)
        print(f"Thư mục lưu tệp nhận: {recv_dir}", file=stdout)
        scope_text = {
            GuestScope.BOTH: "Gửi và nhận hai chiều",
            GuestScope.RECEIVE_ONLY: "Chỉ nhận tệp từ điện thoại",
            GuestScope.SEND_ONLY: "Chỉ gửi tệp sang điện thoại",
        }[info.scope]
        mode_suffix = " · Chỉ dùng 1 lượt" if info.one_shot else ""
        print(f"Quyền phiên: {scope_text}{mode_suffix}", file=stdout)
        if shared_paths:
            print(f"Các tệp chia sẻ sẵn ({len(shared_paths)}):", file=stdout)
            for p in shared_paths:
                size_val = p.stat().st_size if p.is_file() else 0
                label = f"{p.name}/" if p.is_dir() else f"{p.name} ({format_size(size_val)})"
                print(f"  - {label}", file=stdout)
        print("\nDùng camera điện thoại quét mã QR hoặc truy cập liên kết trên.", file=stdout)
        print("Không cần cài đặt thêm phần mềm trên điện thoại.", file=stdout)
        print("(Nhấn Ctrl+C để kết thúc phiên)", file=stdout)
        print("=" * 64, file=stdout, flush=True)

    try:
        await stop_event.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await manager.stop_session()
        if not args.json:
            print("\nĐã đóng phiên kết nối chế độ khách.", file=stdout, flush=True)

    return 0


def cmd_guest(args: argparse.Namespace) -> int:
    with asyncio.Runner() as runner:
        try:
            return runner.run(run_guest(args))
        except KeyboardInterrupt:
            return 0


# ---- Biên lai truyền tệp (teleport receipt) ------------------------------------------------


def cmd_receipt_verify(args: argparse.Namespace) -> int:
    from teleport.app.receipts import known_trusted_fingerprints, verify_receipt
    from teleport.core.display import format_size
    from teleport.core.resume_state import format_time

    receipt_path = Path(args.path).expanduser()
    raw_dir = getattr(args, "dir", None)
    if raw_dir is not None and not str(raw_dir).strip():
        return _cli_syntax_error(args, "--dir không được rỗng")
    files_dir = Path(raw_dir).expanduser() if raw_dir is not None else None
    trusted_fps = known_trusted_fingerprints(_config(args))

    result = verify_receipt(
        receipt_path,
        files_dir=files_dir,
        trusted_fingerprints=trusted_fps,
    )
    receipt = result.receipt

    if getattr(args, "json", False):
        data: dict[str, Any] = {
            "event": "receipt_verified",
            "valid": result.valid,
            "signature_ok": result.signature_ok,
            "trusted_signer": result.trusted_signer,
            "files_checked": result.files_checked,
            "files_ok": result.files_ok,
            "errors": list(result.errors),
        }
        if receipt is not None:
            payload = receipt.payload
            data.update(
                {
                    "schema": payload.schema,
                    "transfer_id": payload.transfer_id,
                    "direction": payload.direction.value,
                    "completed_at": format_time(payload.completed_at),
                    "signer_device_id": payload.signer_device_id,
                    "signer_fingerprint": payload.signer_fingerprint,
                    "peer_device_id": payload.peer_device_id,
                    "peer_name": payload.peer_name,
                    "total_bytes": payload.total_bytes,
                    "file_count": len(payload.files),
                }
            )
        print(json.dumps(data, ensure_ascii=False))
        return 0 if result.valid else 1

    status_tag = "HỢP LỆ" if result.valid else "KHÔNG HỢP LỆ"
    print(f"[{status_tag}] {receipt_path}")
    if receipt is not None:
        p = receipt.payload
        print(f"  Lượt truyền : {p.transfer_id} ({p.direction.value})")
        print(f"  Hoàn tất lúc: {format_time(p.completed_at)}")
        print(f"  Máy ký      : {p.signer_device_id[:8]} ({p.signer_fingerprint[:16]})")
        print(f"  Máy đối tác : {p.peer_name} ({p.peer_device_id[:8]})")
        print(f"  Dung lượng  : {format_size(p.total_bytes)} ({len(p.files)} tệp)")
        if result.trusted_signer is True:
            print("  Tin cậy     : máy ký thuộc danh sách tin cậy")
        elif result.trusted_signer is False:
            print("  Tin cậy     : máy ký chưa ghép đôi với máy này")
        if files_dir is not None:
            print(f"  Kiểm tra tệp: {result.files_ok}/{result.files_checked} tệp khớp")
    for err in result.errors:
        print(f"  Lỗi: {err}", file=sys.stderr)
    return 0 if result.valid else 1


def cmd_receipt_list(args: argparse.Namespace) -> int:
    from teleport.app.receipts import list_receipts, load_receipt
    from teleport.core.display import format_size
    from teleport.core.receipts import ReceiptError
    from teleport.core.resume_state import format_time

    directory = _config(args)
    paths = list_receipts(directory)
    items: list[dict[str, Any]] = []
    for p in paths:
        item: dict[str, Any] = {"path": str(p), "filename": p.name}
        try:
            rec = load_receipt(p)
        except (OSError, ReceiptError) as e:
            item["error"] = type(e).__name__
        else:
            pl = rec.payload
            item.update(
                {
                    "transfer_id": pl.transfer_id,
                    "direction": pl.direction.value,
                    "completed_at": format_time(pl.completed_at),
                    "peer_device_id": pl.peer_device_id,
                    "peer_name": pl.peer_name,
                    "total_bytes": pl.total_bytes,
                    "file_count": len(pl.files),
                }
            )
        items.append(item)

    if getattr(args, "json", False):
        print(json.dumps({"receipts": items}, ensure_ascii=False))
        return 0

    if not items:
        print("Chưa có biên lai truyền tệp nào được lưu.")
        return 0
    for item in items:
        if "error" in item:
            print(f"{item['filename']}  [lỗi đọc: {item['error']}]")
        else:
            print(
                f"{item['transfer_id'][:8]}  {item['direction']:7s}  {item['completed_at']}  "
                f"{item['peer_name']}  {format_size(item['total_bytes'])}  ({item['file_count']} tệp)  "
                f"-> {item['path']}"
            )
    return 0


# ---- Quy tắc phân loại thư mục nhận (`routing`) & Cổng đẩy tự động (`watch`) ---------------


def _collect_cli_patterns(raw_patterns: Sequence[str] | None) -> tuple[str, ...]:
    if not raw_patterns:
        return ()
    joined = ",".join(raw_patterns)
    return parse_patterns(joined)


def _remove_by_prefix[T: (RoutingRule, WatchPortalRule)](
    items: tuple[T, ...], query: str
) -> tuple[tuple[T, ...], T | None, list[T]]:
    exact = [item for item in items if item.id == query]
    if len(exact) == 1:
        chosen = exact[0]
        return tuple(item for item in items if item is not chosen), chosen, []
    matches = [item for item in items if item.id.startswith(query)]
    if len(matches) == 1:
        chosen = matches[0]
        return tuple(item for item in items if item is not chosen), chosen, []
    return items, None, matches


def cmd_routing_list(args: argparse.Namespace) -> int:
    directory = _config(args)
    settings = Settings.load(directory)
    items = [routing_rule_to_dict(r) for r in settings.routing_rules]
    if getattr(args, "json", False):
        print(json.dumps({"routing_rules": items}, ensure_ascii=False))
        return 0
    if not settings.routing_rules:
        print("Chưa có quy tắc phân loại thư mục nhận nào.")
        return 0
    for r in settings.routing_rules:
        pats = ", ".join(r.patterns) if r.patterns else "*"
        src = r.device_id[:8] if r.device_id else "mọi máy"
        state = "bật" if r.enabled else "tắt"
        print(f"{r.id}  {r.name}  [{pats}]  ({src}) -> {r.destination}  [{state}]")
    return 0


def cmd_routing_add(args: argparse.Namespace) -> int:
    output = _Output(sys.stdout, sys.stderr, bool(getattr(args, "json", False)))
    directory = _config(args)
    from_query = getattr(args, "from_device", None)
    device_id: str | None = None
    if from_query is not None:
        try:
            dev = find_trusted(directory, from_query)
        except ConfigDirError as e:
            _config_dir_error(output, e.path, e.error)
            return 1
        except TrustStoreError as e:
            output.error("trust_store_error", str(e))
            return 1
        except (DeviceNotFound, AmbiguousDevice) as e:
            _trusted_lookup_error(output, from_query, e)
            return 1
        device_id = dev.device_id

    try:
        patterns = _collect_cli_patterns(getattr(args, "pattern", None))
        dest = os.path.abspath(os.path.expanduser(args.dest))
        rule = RoutingRule(
            id=secrets.token_hex(4),
            name=args.name,
            destination=dest,
            device_id=device_id,
            patterns=patterns,
        )
    except (ValueError, TypeError) as e:
        output.error("invalid_argument", str(e))
        return 2

    try:
        directory.mkdir(parents=True, exist_ok=True)
        lock = NodeLock(directory)
        lock.acquire(timeout=app_node.LOCK_TIMEOUT)
    except OSError as e:
        _config_dir_error(output, directory, e)
        return 1
    except NodeLockedError:
        output.error("node_locked", "Teleport đang chạy với thư mục cấu hình này.", extra=[_NODE_LOCKED_HINT])
        return 1
    try:
        settings = Settings.load(directory)
        if len(settings.routing_rules) >= MAX_ROUTING_RULES:
            output.error("limit_reached", f"Tối đa {MAX_ROUTING_RULES} quy tắc phân loại.")
            return 1
        updated = replace(settings, routing_rules=(*settings.routing_rules, rule))
        updated.save(directory)
    except OSError as e:
        _config_dir_error(output, directory, e)
        return 1
    finally:
        lock.release()

    if output.json:
        print(json.dumps({"added": routing_rule_to_dict(rule)}, ensure_ascii=False))
    else:
        print(f"Đã thêm quy tắc {rule.id}: “{rule.name}” -> {rule.destination}")
    return 0


def cmd_routing_remove(args: argparse.Namespace) -> int:
    output = _Output(sys.stdout, sys.stderr, bool(getattr(args, "json", False)))
    directory = _config(args)
    if not directory.exists():
        output.error("not_found", f"Không tìm thấy quy tắc “{args.rule_id}”.")
        return 1
    lock = NodeLock(directory)
    try:
        lock.acquire(timeout=app_node.LOCK_TIMEOUT)
    except OSError as e:
        _config_dir_error(output, directory, e)
        return 1
    except NodeLockedError:
        output.error("node_locked", "Teleport đang chạy với thư mục cấu hình này.", extra=[_NODE_LOCKED_HINT])
        return 1
    try:
        settings = Settings.load(directory)
        remaining, removed, matches = _remove_by_prefix(settings.routing_rules, args.rule_id)
        if removed is None:
            if len(matches) > 1:
                output.error("ambiguous", f"“{args.rule_id}” khớp nhiều quy tắc; hãy nhập mã đầy đủ.")
            else:
                output.error("not_found", f"Không tìm thấy quy tắc “{args.rule_id}”.")
            return 1
        replace(settings, routing_rules=remaining).save(directory)
    finally:
        lock.release()

    if output.json:
        print(json.dumps({"removed": routing_rule_to_dict(removed)}, ensure_ascii=False))
    else:
        print(f"Đã xóa quy tắc {removed.id}: “{removed.name}”.")
    return 0


def cmd_watch_list(args: argparse.Namespace) -> int:
    directory = _config(args)
    settings = Settings.load(directory)
    items = [watch_portal_to_dict(w) for w in settings.watch_portals]
    if getattr(args, "json", False):
        print(json.dumps({"watch_portals": items}, ensure_ascii=False))
        return 0
    if not settings.watch_portals:
        print("Chưa có cổng đẩy tự động nào.")
        return 0
    for w in settings.watch_portals:
        pats = ", ".join(w.patterns) if w.patterns else "*"
        rec_note = ", đệ quy" if w.recursive else ""
        state = "bật" if w.enabled else "tắt"
        print(f"{w.id}  {w.name}  {w.source_dir}  [{pats}{rec_note}] -> {w.device_id[:8]}  [{state}]")
    return 0


def cmd_watch_add(args: argparse.Namespace) -> int:
    output = _Output(sys.stdout, sys.stderr, bool(getattr(args, "json", False)))
    directory = _config(args)
    try:
        dev = find_trusted(directory, args.to)
    except ConfigDirError as e:
        _config_dir_error(output, e.path, e.error)
        return 1
    except TrustStoreError as e:
        output.error("trust_store_error", str(e))
        return 1
    except (DeviceNotFound, AmbiguousDevice) as e:
        _trusted_lookup_error(output, args.to, e)
        return 1

    try:
        patterns = _collect_cli_patterns(getattr(args, "pattern", None))
        source_dir = os.path.abspath(os.path.expanduser(args.dir))
        rule = WatchPortalRule(
            id=secrets.token_hex(4),
            name=args.name,
            source_dir=source_dir,
            device_id=dev.device_id,
            patterns=patterns,
            recursive=bool(getattr(args, "recursive", False)),
        )
    except (ValueError, TypeError) as e:
        output.error("invalid_argument", str(e))
        return 2

    try:
        directory.mkdir(parents=True, exist_ok=True)
        lock = NodeLock(directory)
        lock.acquire(timeout=app_node.LOCK_TIMEOUT)
    except OSError as e:
        _config_dir_error(output, directory, e)
        return 1
    except NodeLockedError:
        output.error("node_locked", "Teleport đang chạy với thư mục cấu hình này.", extra=[_NODE_LOCKED_HINT])
        return 1
    try:
        settings = Settings.load(directory)
        if len(settings.watch_portals) >= MAX_WATCH_PORTALS:
            output.error("limit_reached", f"Tối đa {MAX_WATCH_PORTALS} cổng đẩy tự động.")
            return 1
        updated = replace(settings, watch_portals=(*settings.watch_portals, rule))
        updated.save(directory)
    except OSError as e:
        _config_dir_error(output, directory, e)
        return 1
    finally:
        lock.release()

    if output.json:
        print(json.dumps({"added": watch_portal_to_dict(rule)}, ensure_ascii=False))
    else:
        print(f"Đã thêm cổng đẩy {rule.id}: {rule.source_dir} -> {_name_label(dev.name)}")
    return 0


def cmd_watch_remove(args: argparse.Namespace) -> int:
    output = _Output(sys.stdout, sys.stderr, bool(getattr(args, "json", False)))
    directory = _config(args)
    if not directory.exists():
        output.error("not_found", f"Không tìm thấy cổng đẩy “{args.portal_id}”.")
        return 1
    lock = NodeLock(directory)
    try:
        lock.acquire(timeout=app_node.LOCK_TIMEOUT)
    except OSError as e:
        _config_dir_error(output, directory, e)
        return 1
    except NodeLockedError:
        output.error("node_locked", "Teleport đang chạy với thư mục cấu hình này.", extra=[_NODE_LOCKED_HINT])
        return 1
    try:
        settings = Settings.load(directory)
        remaining, removed, matches = _remove_by_prefix(settings.watch_portals, args.portal_id)
        if removed is None:
            if len(matches) > 1:
                output.error("ambiguous", f"“{args.portal_id}” khớp nhiều cổng đẩy; hãy nhập mã đầy đủ.")
            else:
                output.error("not_found", f"Không tìm thấy cổng đẩy “{args.portal_id}”.")
            return 1
        replace(settings, watch_portals=remaining).save(directory)
    finally:
        lock.release()

    if output.json:
        print(json.dumps({"removed": watch_portal_to_dict(removed)}, ensure_ascii=False))
    else:
        print(f"Đã xóa cổng đẩy {removed.id}: “{removed.name}”.")
    return 0
