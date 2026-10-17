"""Bài tập Giai đoạn 0: chat giữa 2 máy trong LAN qua TLS 1.3.

Máy A (chờ kết nối):
    python examples/chat_tls.py serve --name An

Máy B (kết nối tới A, dùng IP và pin mà máy A in ra):
    python examples/chat_tls.py connect 192.168.1.10 --name Binh --pin AB:CD:EF:12:34:56:78:90

Gõ tin nhắn rồi Enter. Gõ /quit (hoặc Ctrl+C) để thoát.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import socket
import sys
import threading
from pathlib import Path

from teleport.core.framing import FramingError, read_message, write_message
from teleport.net.identity import format_fingerprint, load_or_create_identity
from teleport.net.tls import (
    FingerprintMismatch,
    client_context,
    peer_fingerprint,
    server_context,
    verify_pin,
)

DEFAULT_PORT = 47100


def start_stdin_reader() -> asyncio.Queue[str | None]:
    """Đọc bàn phím trong một thread riêng và đẩy từng dòng vào asyncio.Queue.

    `input()` là lệnh chặn (blocking). Gọi thẳng trong coroutine sẽ làm đứng
    toàn bộ event loop, nên không nhận được tin nhắn trong lúc chờ gõ phím.
    Thread là daemon để chương trình thoát được ngay cả khi thread đang chờ.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def worker() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(queue.put_nowait, line.rstrip("\r\n"))
        loop.call_soon_threadsafe(queue.put_nowait, None)  # EOF

    threading.Thread(target=worker, daemon=True).start()
    return queue


async def chat_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    my_name: str,
    lines: asyncio.Queue[str | None],
) -> bool:
    """Chat cho tới khi một bên thoát. Trả về False nếu bắt tay HELLO thất bại."""
    try:
        await write_message(writer, {"type": "hello", "name": my_name})
        hello = await read_message(reader)
    except (ConnectionError, FramingError):
        hello = None
    if hello is None or hello.get("type") != "hello":
        print("Bên kia không gửi HELLO hợp lệ, ngắt kết nối.", flush=True)
        writer.close()
        return False
    peer = str(hello.get("name", "?"))
    print(f"--- Đã kết nối với {peer}. Gõ tin nhắn rồi Enter, /quit để thoát ---", flush=True)

    async def receive_loop() -> None:
        while (msg := await read_message(reader)) is not None:
            if msg.get("type") == "msg":
                print(f"[{peer}] {msg.get('text', '')}", flush=True)
        print(f"--- {peer} đã ngắt kết nối ---", flush=True)

    async def send_loop() -> None:
        while (line := await lines.get()) is not None:
            if line.strip() == "/quit":
                return
            if line.strip():
                await write_message(writer, {"type": "msg", "text": line})

    # Hai việc chạy song song trên cùng một event loop; việc nào xong trước
    # (mình gõ /quit hoặc bên kia ngắt) thì hủy việc còn lại.
    tasks = {asyncio.create_task(receive_loop()), asyncio.create_task(send_loop())}
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        task.result()  # ném lại lỗi nếu có (ví dụ FramingError)

    writer.close()
    with contextlib.suppress(ConnectionError, OSError):
        await writer.wait_closed()
    return True


def lan_addresses() -> list[str]:
    try:
        addrs = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        return []
    return [a for a in addrs if not a.startswith("127.")]


async def serve(args: argparse.Namespace) -> None:
    identity = load_or_create_identity(args.config_dir)
    lines = start_stdin_reader()
    finished = asyncio.Event()
    busy = False

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal busy
        if busy:  # bài tập chỉ chat 1-1
            writer.close()
            return
        busy = True
        print(f"Có kết nối từ {writer.get_extra_info('peername')[0]}", flush=True)
        try:
            ok = await chat_session(reader, writer, args.name, lines)
        except BaseException:
            finished.set()
            raise
        if ok:
            finished.set()  # phiên chat xong thì thoát
        else:
            busy = False  # kết nối hỏng (ví dụ client từ chối pin): tiếp tục chờ

    server = await asyncio.start_server(on_connect, args.host, args.port, ssl=server_context(identity))
    print(f"Đang lắng nghe tại cổng {args.port}")
    for addr in lan_addresses():
        print(f"  IP trong LAN: {addr}")
    print(f"  Pin (đưa cho máy kia): {identity.short_fingerprint}")
    print(f"  Fingerprint đầy đủ:    {format_fingerprint(identity.fingerprint)}", flush=True)
    async with server:
        await finished.wait()


async def connect(args: argparse.Namespace) -> None:
    reader, writer = await asyncio.open_connection(args.host, args.port, ssl=client_context())
    actual = peer_fingerprint(writer)
    if args.pin:
        try:
            verify_pin(actual, args.pin)
        except FingerprintMismatch:
            print("CẢNH BÁO: fingerprint KHÔNG khớp pin! Có thể bị tấn công đứng giữa (MITM).")
            print(f"  Nhận được: {format_fingerprint(actual)}")
            writer.close()
            sys.exit(2)
        print("Fingerprint khớp pin ✔")
    else:
        print("CHƯA XÁC MINH: không có --pin. Hãy so fingerprint này với màn hình máy kia:")
        print(f"  {format_fingerprint(actual)}")
    await chat_session(reader, writer, args.name, start_stdin_reader())


def main() -> None:
    # Ép UTF-8 để tiếng Việt hiển thị đúng cả khi stdin/stdout bị chuyển hướng (pipe).
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Chat TLS trong LAN (bài tập Giai đoạn 0)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="chờ máy khác kết nối tới")
    p_serve.add_argument("--host", default="0.0.0.0", help="địa chỉ lắng nghe (mặc định: mọi card mạng)")
    p_serve.add_argument(
        "--config-dir",
        type=Path,
        default=Path.home() / ".teleport-chat",
        help="nơi lưu chứng chỉ của máy này",
    )

    p_connect = sub.add_parser("connect", help="kết nối tới máy đang serve")
    p_connect.add_argument("host", help="IP của máy đang serve")
    p_connect.add_argument("--pin", help="fingerprint (hoặc 8 byte đầu) mà máy serve in ra")

    for p in (p_serve, p_connect):
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        p.add_argument("--name", default=socket.gethostname())

    args = parser.parse_args()
    try:
        asyncio.run(serve(args) if args.command == "serve" else connect(args))
    except KeyboardInterrupt:
        print("\nĐã thoát.")
    except ConnectionRefusedError:
        print("Không kết nối được: máy kia chưa chạy 'serve', sai IP/cổng, hoặc bị tường lửa chặn.")
        sys.exit(1)


if __name__ == "__main__":
    main()
