"""Kiểm thử tự động cho lệnh CLI `teleport guest` (Chế độ khách qua Web QR)."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from teleport.cli import build_parser, run_guest
from tests.test_http_client import SimpleHttpClient


def test_cli_guest_help() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["guest", "--help"])
    assert exc.value.code == 0


def test_cli_guest_missing_file(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(["guest", str(tmp_path / "not_found.txt")])
    stderr = io.StringIO()
    stdout = io.StringIO()

    exit_code = asyncio.run(run_guest(args, stdout=stdout, stderr=stderr))
    assert exit_code == 2
    assert "Không tìm thấy tệp" in stderr.getvalue()


@pytest.mark.asyncio
async def test_cli_guest_json_run_and_upload(tmp_path: Path) -> None:
    recv_dir = tmp_path / "recv"
    sample_file = tmp_path / "share_me.txt"
    sample_file.write_bytes(b"HELLO_FROM_PC")

    parser = build_parser()
    args = parser.parse_args(
        [
            "guest",
            str(sample_file),
            "--dir",
            str(recv_dir),
            "--ip",
            "127.0.0.1",
            "--json",
        ]
    )

    stdout = io.StringIO()
    stderr = io.StringIO()

    guest_task = asyncio.create_task(run_guest(args, stdout=stdout, stderr=stderr))

    # Đợi máy chủ khởi động và xuất JSON
    for _ in range(50):
        await asyncio.sleep(0.1)
        val = stdout.getvalue()
        if "guest_started" in val:
            break

    output_lines = stdout.getvalue().strip().split("\n")
    start_event = json.loads(output_lines[0])
    assert start_event["event"] == "guest_started"
    url = start_event["url"]
    token = start_event["token"]
    assert "127.0.0.1" in url

    # Giả lập điện thoại tải file lên máy tính
    async with SimpleHttpClient() as client:
        upload_resp = await client.post(
            f"http://127.0.0.1:{start_event['port']}/api/upload",
            params={"name": "phone_photo.png", "token": token},
            content=b"PNG_DATA_FROM_PHONE",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert upload_resp.status_code == 200

    # Hủy task để kết thúc phiên
    guest_task.cancel()
    exit_code = await guest_task
    assert exit_code == 0

    # Kiểm tra file đã được lưu trong recv_dir
    saved_file = recv_dir / "phone_photo.png"
    assert saved_file.is_file()
    assert saved_file.read_bytes() == b"PNG_DATA_FROM_PHONE"
