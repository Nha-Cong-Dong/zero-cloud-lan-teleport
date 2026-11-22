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


@pytest.mark.asyncio
async def test_cli_guest_scope_one_shot_wifi_and_text(tmp_path: Path) -> None:
    recv_dir = tmp_path / "recv"
    parser = build_parser()
    args = parser.parse_args(
        [
            "guest",
            "--dir",
            str(recv_dir),
            "--ip",
            "127.0.0.1",
            "--scope",
            "receive_only",
            "--one-shot",
            "--wifi-ssid",
            "MyLAN",
            "--wifi-password",
            "Secret99",
            "--json",
        ]
    )

    stdout = io.StringIO()
    stderr = io.StringIO()
    guest_task = asyncio.create_task(run_guest(args, stdout=stdout, stderr=stderr))

    for _ in range(50):
        await asyncio.sleep(0.05)
        if "guest_started" in stdout.getvalue():
            break

    lines = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    start_event = lines[0]
    assert start_event["scope"] == "receive_only"
    assert start_event["one_shot"] is True
    assert start_event["wifi_qr"] == "WIFI:T:WPA;S:MyLAN;P:Secret99;;"

    async with SimpleHttpClient() as client:
        # Gửi chữ từ điện thoại -> guest_text JSON event
        txt_resp = await client.post(
            f"http://127.0.0.1:{start_event['port']}/api/text",
            params={"token": start_event["token"]},
            content=b"Hello CLI",
            headers={"Content-Type": "text/plain"},
        )
        assert txt_resp.status_code == 200

        # Tải 1 file -> one-shot tự kết thúc run_guest
        up_resp = await client.post(
            f"http://127.0.0.1:{start_event['port']}/api/upload",
            params={"name": "only.txt", "token": start_event["token"]},
            content=b"ONCE",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert up_resp.status_code == 200

    exit_code = await asyncio.wait_for(guest_task, timeout=3.0)
    assert exit_code == 0
    all_events = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    assert any(ev.get("event") == "guest_text" and ev.get("text") == "Hello CLI" for ev in all_events)
    assert (recv_dir / "only.txt").read_bytes() == b"ONCE"


def test_cli_guest_invalid_wifi_qr(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(["guest", "--wifi-ssid", "Cafe", "--wifi-security", "WPA"])
    stderr = io.StringIO()
    stdout = io.StringIO()
    exit_code = asyncio.run(run_guest(args, stdout=stdout, stderr=stderr))
    assert exit_code == 2
    assert "Lỗi cấu hình Wi-Fi" in stderr.getvalue()
