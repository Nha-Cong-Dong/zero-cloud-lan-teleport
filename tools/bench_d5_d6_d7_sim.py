"""Đo giả lập D5 (`mvp N4`), D6 (`mvp N6`) và D7 (`mvp N10`) trên một máy.

Thay cho việc bắt buộc phải có máy macOS/Linux thật hoặc Wi-Fi rời khi đo định kỳ:
- D5 (`mvp N4`): Qua proxy TCP cục bộ (`SO_RCVBUF`/`SO_SNDBUF` 64 KiB), cắt kết nối bằng
  TCP RST giữa lượt truyền 64 MiB (tại ~70% lưu lượng), chờ 0,4s rồi nối lại, kiểm tra
  SHA-256 khớp và lượng gửi thừa <= 64 MiB (ngưỡng mvp N4).
- D6 (`mvp N6`): Qua proxy TCP mô phỏng Wi-Fi 5 (RTT 4 ms, 30 MB/s), đo độ trễ đồng bộ
  clipboard qua `Node` + `FakeClipboard` (1 KiB văn bản, 1 MiB văn bản <= 500 ms,
  9,5 MiB PNG <= 2000 ms).
- D7 (`mvp N10`): Kiểm tra bộ vector tương thích đa nền tảng Windows/macOS/Linux (chuẩn
  hóa tên file NFD/NFC, tên thiết bị cấm trên Windows, chặn path traversal, `BLOCK_HEADER`
  41 byte chuẩn mạng, `Bitfield` MSB-first, `MerkleStream` O(log2 N), `StateReplay`).

Ví dụ:
    python tools/bench_d5_d6_d7_sim.py
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import random
import socket
import statistics
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from teleport.app.clipboard import CLIP_DEBOUNCE, ClipContent, FakeClipboard  # noqa: E402
from teleport.app.node import Node  # noqa: E402
from teleport.app.transfers import TransferFinished, TransferResult, TransferUpdate  # noqa: E402
from teleport.core.bitfield import Bitfield  # noqa: E402
from teleport.core.blocks import BLOCK_HEADER, BLOCK_SIZE, check_block, pack_header, parse_header  # noqa: E402
from teleport.core.clipboard import MAX_PNG_BYTES, MAX_TEXT_BYTES, PNG_SIGNATURE  # noqa: E402
from teleport.core.filenames import FALLBACK_NAME, sanitize_filename, sanitize_rel_path  # noqa: E402
from teleport.core.merkle import MerkleStream, merkle_root  # noqa: E402
from teleport.core.resume_state import (  # noqa: E402
    StateReplay,
    encode_header,
    encode_mark,
    encode_reserved,
    encode_target,
    max_state_length,
)
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem  # noqa: E402
from teleport.trust.store import TrustStore  # noqa: E402

MIB = 1024 * 1024


def setup_mutual_trust(home_a: Path, name_a: str, home_b: Path, name_b: str) -> tuple[str, str]:
    home_a.mkdir(parents=True, exist_ok=True)
    home_b.mkdir(parents=True, exist_ok=True)
    ident_a = load_or_create_identity(home_a)
    ident_b = load_or_create_identity(home_b)
    TrustStore.load(home_a).add(peer_cert_from_pem(ident_b.cert_pem()), name_b)
    TrustStore.load(home_b).add(peer_cert_from_pem(ident_a.cert_pem()), name_a)
    return ident_a.device_id, ident_b.device_id


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(MIB):
            h.update(chunk)
    return h.hexdigest()


class _ConnPair:
    def __init__(self, c_writer: asyncio.StreamWriter, s_writer: asyncio.StreamWriter) -> None:
        self.c_writer = c_writer
        self.s_writer = s_writer


class SimTcpProxy:
    """Proxy TCP cục bộ có đếm byte, chèn độ trễ/giới hạn băng thông và cắt TCP RST."""

    def __init__(
        self,
        target_port: int,
        *,
        buf_size: int = 65536,
        one_way_delay_s: float = 0.0,
        rate_bytes_per_sec: float = 0.0,
    ) -> None:
        self.target_host = "127.0.0.1"
        self.target_port = target_port
        self.buf_size = buf_size
        self.one_way_delay_s = one_way_delay_s
        self.rate_bytes_per_sec = rate_bytes_per_sec
        self.c2s_bytes = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._active: set[_ConnPair] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._rst_threshold: int | None = None
        self._rst_fired = asyncio.Event()
        self._triggered = False

    def arm_rst_at(self, byte_threshold: int) -> asyncio.Event:
        """Đặt bẫy tự cắt RST ngay khi tổng `c2s_bytes` chạm ngưỡng."""
        self._rst_threshold = byte_threshold
        self._triggered = False
        self._rst_fired.clear()
        return self._rst_fired

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0)
        sock = self._server.sockets[0]
        self.port = int(sock.getsockname()[1])

    def _set_buf_opts(self, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is not None and self.buf_size > 0:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.buf_size)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.buf_size)
            except OSError:
                pass

    @staticmethod
    def _force_rst(writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        try:
            writer.transport.abort()
        except Exception:
            with contextlib.suppress(Exception):
                writer.close()

    def rst_active(self) -> None:
        """Cắt toàn bộ kết nối đang sống bằng TCP RST (giữ server lắng nghe cho lần nối lại)."""
        pairs = list(self._active)
        self._active.clear()
        for pair in pairs:
            self._force_rst(pair.c_writer)
            self._force_rst(pair.s_writer)

    async def _handle_client(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter) -> None:
        self._set_buf_opts(c_writer)
        try:
            s_reader, s_writer = await asyncio.open_connection(self.target_host, self.target_port)
        except OSError:
            c_writer.close()
            return
        self._set_buf_opts(s_writer)
        pair = _ConnPair(c_writer, s_writer)
        self._active.add(pair)

        t1 = asyncio.create_task(self._pump(c_reader, s_writer, count_c2s=True))
        t2 = asyncio.create_task(self._pump(s_reader, c_writer, count_c2s=False))
        self._tasks.update((t1, t2))
        try:
            await asyncio.gather(t1, t2, return_exceptions=True)
        finally:
            self._tasks.discard(t1)
            self._tasks.discard(t2)
            self._active.discard(pair)
            for w in (c_writer, s_writer):
                with contextlib.suppress(Exception):
                    w.close()

    async def _pump(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        count_c2s: bool,
    ) -> None:
        loop = asyncio.get_running_loop()
        chunk_read = min(65536, max(4096, self.buf_size or 65536))
        has_shaping = self.rate_bytes_per_sec > 0 or self.one_way_delay_s > 0
        if not has_shaping:
            try:
                while True:
                    data = await reader.read(chunk_read)
                    if not data:
                        break
                    if count_c2s:
                        self.c2s_bytes += len(data)
                        if (
                            self._rst_threshold is not None
                            and not self._triggered
                            and self.c2s_bytes >= self._rst_threshold
                        ):
                            self._triggered = True
                            self._rst_fired.set()
                            self.rst_active()
                            break
                    writer.write(data)
                    await writer.drain()
            except (OSError, asyncio.CancelledError, RuntimeError):
                pass
            return

        # Đường truyền có băng thông R và độ trễ lan truyền d được mô hình hóa dạng ống dẫn (pipelined):
        # nhiều gói tin nối đuôi nhau trên đường truyền thay vì cộng dồn RTT vào từng khúc đọc TCP.
        queue: asyncio.Queue[tuple[float, bytes] | None] = asyncio.Queue(maxsize=256)

        async def _ingest() -> None:
            ser_done = loop.time()
            try:
                while True:
                    data = await reader.read(chunk_read)
                    if not data:
                        break
                    if count_c2s:
                        self.c2s_bytes += len(data)
                        if (
                            self._rst_threshold is not None
                            and not self._triggered
                            and self.c2s_bytes >= self._rst_threshold
                        ):
                            self._triggered = True
                            self._rst_fired.set()
                            self.rst_active()
                            break
                    now = loop.time()
                    if self.rate_bytes_per_sec > 0:
                        ser_done = max(now, ser_done) + len(data) / self.rate_bytes_per_sec
                        arrive_at = ser_done + self.one_way_delay_s
                    else:
                        arrive_at = now + self.one_way_delay_s
                    await queue.put((arrive_at, data))
            except (OSError, asyncio.CancelledError, RuntimeError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    await queue.put(None)

        async def _deliver() -> None:
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    arrive_at, data = item
                    rem = arrive_at - loop.time()
                    if rem > 0.001:
                        await asyncio.sleep(rem)
                    writer.write(data)
                    # Xả ngay các gói đã tới hạn nằm kế trong hàng đợi trước khi drain
                    while not queue.empty():
                        nxt = queue.get_nowait()
                        if nxt is None:
                            await writer.drain()
                            return
                        n_arrive, n_data = nxt
                        if n_arrive > loop.time() + 0.001:
                            # Đợi nốt gói kế rồi mới ghi
                            await writer.drain()
                            await asyncio.sleep(n_arrive - loop.time())
                        writer.write(n_data)
                    await writer.drain()
            except (OSError, asyncio.CancelledError, RuntimeError):
                pass

        await asyncio.gather(_ingest(), _deliver(), return_exceptions=True)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
        self.rst_active()
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


async def run_d5_resume_bench(work_root: Path) -> bool:
    """D5 (`mvp N4`): truyền 64 MiB qua proxy TCP, cắt RST tại ~70%, nối lại 3 lượt."""
    print("\n==================================================")
    print("D5 (mvp N4): GIẢ LẬP RỚT MẠNG TCP RST & NỐI LẠI (3 LƯỢT x 64 MiB)")
    print("==================================================")
    file_size = 64 * MIB
    src_file = work_root / "d5_64mib.bin"
    rng = random.Random(20261118)
    src_hash = hashlib.sha256()
    with src_file.open("wb") as f:
        rem = file_size
        while rem > 0:
            n = min(rem, MIB)
            buf = rng.randbytes(n)
            f.write(buf)
            src_hash.update(buf)
            rem -= n
    expected_sha = src_hash.hexdigest()
    trigger_bytes = int(file_size * 0.70)

    excess_list: list[float] = []
    elapsed_list: list[float] = []
    all_ok = True

    for run_idx in range(1, 4):
        run_dir = work_root / f"d5_run_{run_idx}"
        recv_home = run_dir / "rh"
        send_home = run_dir / "sh"
        recv_dir = run_dir / "dst"
        recv_dir.mkdir(parents=True, exist_ok=True)
        recv_id, send_id = setup_mutual_trust(recv_home, "bench-recv", send_home, "bench-send")
        _ = send_id

        send_finished: asyncio.Future[TransferFinished] = asyncio.get_running_loop().create_future()
        recv_finished: asyncio.Future[TransferFinished] = asyncio.get_running_loop().create_future()

        def _on_send(ev: TransferUpdate, fut: asyncio.Future[TransferFinished] = send_finished) -> None:
            if isinstance(ev, TransferFinished) and not fut.done():
                fut.set_result(ev)

        def _on_recv(ev: TransferUpdate, fut: asyncio.Future[TransferFinished] = recv_finished) -> None:
            if isinstance(ev, TransferFinished) and not fut.done():
                fut.set_result(ev)

        node_r = Node(
            recv_home,
            name="bench-recv",
            host="127.0.0.1",
            port=0,
            mdns=None,
            auto_connect=False,
            receive_dir=recv_dir,
            auto_accept=True,
            on_transfer_event=_on_recv,
        )
        node_s = Node(
            send_home,
            name="bench-send",
            host="127.0.0.1",
            port=0,
            mdns=None,
            auto_connect=False,
            receive_dir=None,
            auto_accept=False,
            on_transfer_event=_on_send,
        )
        await node_r.start()
        assert node_r.port is not None
        proxy = SimTcpProxy(node_r.port, buf_size=65536)
        await proxy.start()
        await node_s.start()

        try:
            conn_res = await node_s.connect_address(recv_id, "127.0.0.1", proxy.port)
            assert str(conn_res.value) == "ok" or conn_res.name == "OK", conn_res
            rst_event = proxy.arm_rst_at(trigger_bytes)

            t0 = time.perf_counter()
            await node_s.transfers.send(recv_id, [src_file])
            await asyncio.wait_for(rst_event.wait(), timeout=60.0)
            await asyncio.sleep(0.4)
            node_s.connect_hint(recv_id, ["127.0.0.1"], proxy.port)

            fin_s, fin_r = await asyncio.wait_for(asyncio.gather(send_finished, recv_finished), timeout=60.0)
            t1 = time.perf_counter()

            received_files = [
                p
                for p in recv_dir.rglob(src_file.name)
                if p.is_file() and ".teleport" not in p.relative_to(recv_dir).parts
            ]
            sha_ok = len(received_files) == 1 and sha256_file(received_files[0]) == expected_sha
            excess_mib = max(0.0, (fin_s.payload_sent - file_size) / MIB)
            elapsed_s = t1 - t0
            run_ok = (
                fin_s.result is TransferResult.DONE
                and fin_r.result is TransferResult.DONE
                and sha_ok
                and excess_mib <= 64.0
            )
            if not run_ok:
                all_ok = False
            excess_list.append(excess_mib)
            elapsed_list.append(elapsed_s)
            print(
                f"  Lượt {run_idx}: result=DONE | excess={excess_mib:.2f} MiB "
                f"(payload_sent={fin_s.payload_sent:,} B) | time={elapsed_s:.2f} s | "
                f"sha={'OK' if sha_ok else 'FAIL'}"
            )
        finally:
            await node_s.close()
            await node_r.close()
            await proxy.close()

    med_excess = statistics.median(excess_list)
    max_excess = max(excess_list)
    med_time = statistics.median(elapsed_list)
    print(
        f"  -> Trung vị gửi thừa: {med_excess:.2f} MiB (max {max_excess:.2f} MiB, ngưỡng N4 <= 64,00 MiB) | "
        f"Trung vị thời gian: {med_time:.2f} s | Kết luận: {'ĐẠT' if all_ok else 'KHÔNG ĐẠT'}"
    )
    return all_ok


def make_test_png(target_bytes: int, seed_tag: bytes) -> bytes:
    """Dựng luồng byte có chữ ký PNG + IHDR 2048x1536 hợp lệ đúng dung lượng yêu cầu."""
    ihdr_data = struct.pack("!IIBBBBB", 2048, 1536, 8, 2, 0, 0, 0)
    ihdr_crc = struct.pack("!I", zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF)
    ihdr = struct.pack("!I", len(ihdr_data)) + b"IHDR" + ihdr_data + ihdr_crc
    iend_crc = struct.pack("!I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    iend = struct.pack("!I", 0) + b"IEND" + iend_crc

    overhead = len(PNG_SIGNATURE) + len(ihdr) + 12 + len(iend)
    payload_len = max(len(seed_tag) + 16, target_bytes - overhead)
    idat_body = bytearray(seed_tag + b"\x00" * (payload_len - len(seed_tag)))
    idat_crc = struct.pack("!I", zlib.crc32(b"IDAT" + idat_body) & 0xFFFFFFFF)
    idat = struct.pack("!I", len(idat_body)) + b"IDAT" + bytes(idat_body) + idat_crc
    png = PNG_SIGNATURE + ihdr + idat + iend
    assert len(png) <= MAX_PNG_BYTES
    return png


async def run_d6_clipboard_bench(work_root: Path) -> bool:
    """D6 (`mvp N6`): đo độ trễ đồng bộ clipboard qua proxy giả lập Wi-Fi 5 (RTT 4 ms, 30 MB/s)."""
    print("\n==================================================")
    print(
        "D6 (mvp N6): ĐỒNG BỘ CLIPBOARD GIẢ LẬP WI-FI 5 "
        f"(RTT 4 ms, 30 MB/s, debounce {CLIP_DEBOUNCE * 1000:.0f} ms)"
    )
    print("==================================================")
    run_dir = work_root / "d6_sim"
    home_a = run_dir / "ha"
    home_b = run_dir / "hb"
    id_a, id_b = setup_mutual_trust(home_a, "clip-a", home_b, "clip-b")

    cb_a = FakeClipboard()
    cb_b = FakeClipboard()

    node_b = Node(
        home_b,
        name="clip-b",
        host="127.0.0.1",
        port=0,
        mdns=None,
        auto_connect=False,
        receive_dir=None,
        clipboard=cb_b,
    )
    node_a = Node(
        home_a,
        name="clip-a",
        host="127.0.0.1",
        port=0,
        mdns=None,
        auto_connect=False,
        receive_dir=None,
        clipboard=cb_a,
    )

    await node_b.start()
    assert node_b.port is not None
    # Proxy 2 ms một chiều (4 ms RTT) và băng thông 30 MB/s
    proxy = SimTcpProxy(node_b.port, buf_size=65536, one_way_delay_s=0.002, rate_bytes_per_sec=30_000_000)
    await proxy.start()
    await node_a.start()

    try:
        node_a.set_clipboard(id_b, True)
        node_b.set_clipboard(id_a, True)
        conn_res = await node_a.connect_address(id_b, "127.0.0.1", proxy.port)
        assert conn_res.name == "OK", conn_res
        # Chờ kênh điều khiển sẵn sàng cả hai phía
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if id_a in node_b.connected_devices() and id_b in node_a.connected_devices():
                break
            await asyncio.sleep(0.02)

        async def _wait_write(expected_count: int, timeout_s: float = 10.0) -> tuple[float, ClipContent]:
            end_at = time.monotonic() + timeout_s
            while len(cb_b.writes) < expected_count:
                if time.monotonic() >= end_at:
                    raise TimeoutError(f"Quá hạn chờ clipboard #{expected_count}")
                await asyncio.sleep(0.005)
            return cb_b.writes[expected_count - 1]

        all_ok = True
        count_before = len(cb_b.writes)

        # Ca 1: 5 lượt 1 KiB văn bản
        lat_1k: list[float] = []
        for i in range(5):
            tag = f"[1KiB-run-{i}] "
            txt = (tag + ("ZCL Teleport Wi-Fi 5 Tiếng Việt\n" * 40))[:1024]
            t_set = time.monotonic()
            cb_a.set_text(txt)
            count_before += 1
            t_write, content = await _wait_write(count_before)
            assert content.data == txt
            lat_1k.append((t_write - t_set) * 1000.0)
            await asyncio.sleep(0.25)

        # Ca 2: 5 lượt 1 MiB văn bản (<= 500 ms)
        lat_1m: list[float] = []
        base_pad = ("Dong bo van ban dung luong lon trong mang noi bo!\n" * 21000).encode("utf-8")
        for i in range(5):
            hdr = f"[1MiB-run-{i:02d}]".encode()
            txt = (hdr + base_pad)[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
            t_set = time.monotonic()
            cb_a.set_text(txt)
            count_before += 1
            t_write, content = await _wait_write(count_before)
            assert content.data == txt
            lat_1m.append((t_write - t_set) * 1000.0)
            await asyncio.sleep(0.25)

        # Ca 3: 3 lượt ~9.5 MiB ảnh PNG (<= 2000 ms)
        lat_png: list[float] = []
        target_png_bytes = int(9.5 * MIB)
        for i in range(3):
            png_bytes = make_test_png(target_png_bytes, f"png-run-{i:02d}".encode())
            t_set = time.monotonic()
            cb_a.set_image(png_bytes)
            count_before += 1
            t_write, content = await _wait_write(count_before, timeout_s=15.0)
            assert content.data == png_bytes
            lat_png.append((t_write - t_set) * 1000.0)
            await asyncio.sleep(0.30)

        med_1k, max_1k = statistics.median(lat_1k), max(lat_1k)
        med_1m, max_1m = statistics.median(lat_1m), max(lat_1m)
        med_png, max_png = statistics.median(lat_png), max(lat_png)
        ok_1m = max_1m < 500.0
        ok_png = max_png < 2000.0
        all_ok = ok_1m and ok_png
        print(f"  1 KiB văn bản (5 lượt):  trung vị {med_1k:.1f} ms | max {max_1k:.1f} ms")
        print(
            f"  1 MiB văn bản (5 lượt):  trung vị {med_1m:.1f} ms | max {max_1m:.1f} ms "
            f"(ngưỡng N6 < 500 ms) -> {'ĐẠT' if ok_1m else 'KHÔNG ĐẠT'}"
        )
        print(
            f"  9,5 MiB PNG   (3 lượt):  trung vị {med_png:.1f} ms | max {max_png:.1f} ms "
            f"(ngưỡng N6 < 2000 ms) -> {'ĐẠT' if ok_png else 'KHÔNG ĐẠT'}"
        )
        return all_ok
    finally:
        await node_a.close()
        await node_b.close()
        await proxy.close()


def run_d7_cross_platform_vectors() -> bool:
    """D7 (`mvp N10`): bộ test vector tương thích dây & hệ file Windows / macOS / Linux."""
    print("\n==================================================")
    print("D7 (mvp N10): KIỂM TRA VECTOR TƯƠNG THÍCH ĐA NỀN TẢNG (WIN / MAC / LINUX)")
    print("==================================================")
    checks: list[tuple[str, bool]] = []

    # 1. Làm sạch tên file đa nền tảng (macOS NFD -> NFC, tên cấm Windows, Linux control/traversal)
    nfd_name = "Ba\u0301o ca\u0301o Ta\u0300i chi\u0301nh e\u0301.pdf"
    nfc_expected = "Báo cáo Tài chính é.pdf"
    c1 = sanitize_filename(nfd_name) == nfc_expected
    checks.append(("macOS NFD -> NFC", c1))

    con_name = sanitize_filename("CON.txt")
    nul_name = sanitize_filename("nul")
    trail_name = sanitize_filename("draft.   ")
    c2 = con_name != "CON.txt" and nul_name.lower() != "nul" and not trail_name.endswith((".", " "))
    checks.append(("Tên cấm Windows & dấu chấm/cách cuối", c2))

    rel_trav = sanitize_rel_path("../../etc/passwd")
    rel_win = sanitize_rel_path("folderA\\subB\\Ba\u0301o ca\u0301o.txt")
    rel_empty = sanitize_rel_path("..\\../")
    c3 = (
        rel_trav == "etc/passwd"
        and ".." not in rel_trav.split("/")
        and rel_win == "folderA/subB/Báo cáo.txt"
        and rel_empty == FALLBACK_NAME
    )
    checks.append(("Đường dẫn tương đối & chặn path traversal", c3))

    # 2. Header khối 41 byte chuẩn mạng (!BII32s)
    digest = hashlib.sha256(b"teleport-cross-platform-block").digest()
    hdr = pack_header(7, 131072, digest)
    parsed = parse_header(hdr)
    check_block(parsed, 7 * BLOCK_SIZE + 131072)
    c4 = (
        len(hdr) == BLOCK_HEADER.size == 41
        and parsed.index == 7
        and parsed.length == 131072
        and parsed.sha256 == digest
    )
    checks.append(("Header khối 41 byte network-endian", c4))

    # 3. Bitfield MSB-first
    bf = Bitfield(10)
    for idx in (0, 3, 7, 9):
        bf.set(idx)
    raw = bf.to_bytes()
    b64 = bf.to_base64()
    bf2 = Bitfield.from_base64(b64, 10)
    # Bit 0=0x80, bit 3=0x10, bit 7=0x01 -> 0x91; bit 9 (index 1 byte 1) = 0x40
    c5 = raw == bytes([0x91, 0x40]) and bf2 == bf and list(bf2.missing()) == [1, 2, 4, 5, 6, 8]
    checks.append(("Bitfield MSB-first & base64 roundtrip", c5))

    # 4. MerkleStream O(log2 N) khớp merkle_root chuẩn
    leaves = b"".join(hashlib.sha256(f"blk-{i}".encode()).digest() for i in range(137))
    stream = MerkleStream()
    stream.update(leaves[: 64 * 32])
    stream.update(leaves[64 * 32 :])
    c6 = stream.root() == merkle_root(leaves) and stream.stack_depth <= 8 and stream.count == 137
    checks.append(("MerkleStream O(log2 N) khớp merkle_root", c6))

    # 5. StateReplay vòng khép kín (header + TARGET + RESERVED + MARK)
    tid = "0123456789abcdef0123456789abcdef"
    file_sz = 3 * BLOCK_SIZE + 512
    n_blk = 4
    log_bytes = bytearray()
    log_bytes += encode_header(tid, 0, file_sz, BLOCK_SIZE)
    log_bytes += encode_target("mac_to_win.bin")
    log_bytes += encode_reserved("mac_to_win.bin")
    log_bytes += encode_mark([(0, digest), (2, digest)])
    replay = StateReplay(n_blk, max_length=max_state_length(n_blk))
    replay.feed(bytes(log_bytes))
    res = replay.finish()
    c7 = (
        res.target == "mac_to_win.bin"
        and res.reserved is True
        and res.bits.count() == 2
        and (0 in res.bits)
        and (2 in res.bits)
        and not res.truncated
    )
    checks.append(("StateReplay nhật ký khôi phục bền", c7))

    for label, ok in checks:
        print(f"  [{'ĐẠT' if ok else 'LỖI'}] {label}")

    all_ok = all(ok for _, ok in checks)
    passed_count = sum(ok for _, ok in checks)
    print(f"  -> Kết luận D7 vector: {'ĐẠT' if all_ok else 'KHÔNG ĐẠT'} ({passed_count}/{len(checks)})")
    return all_ok


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tpd567_") as tmp:
        work_root = Path(tmp)
        d5_ok = asyncio.run(run_d5_resume_bench(work_root))
        d6_ok = asyncio.run(run_d6_clipboard_bench(work_root))
        d7_ok = run_d7_cross_platform_vectors()
    print("\n==================================================")
    print(
        f"TỔNG KẾT GIẢ LẬP: D5={'ĐẠT' if d5_ok else 'LỖI'} | "
        f"D6={'ĐẠT' if d6_ok else 'LỖI'} | D7={'ĐẠT' if d7_ok else 'LỖI'}"
    )
    print("==================================================")
    return 0 if (d5_ok and d6_ok and d7_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
