"""Kiểm thử tính năng mDNS Stealth Mode (core discovery)."""

from __future__ import annotations

from teleport.core.discovery import (
    TxtRecord,
    build_txt,
    parse_txt,
)


def test_build_txt_normal() -> None:
    device_id = "0123456789abcdef0123456789abcdef"
    txt_dict = build_txt(
        device_id=device_id,
        name="Laptop của Tùng",
        os="windows",
        stealth=False,
    )
    raw_properties = {k.encode("ascii"): v.encode("utf-8") for k, v in txt_dict.items()}
    parsed = parse_txt(raw_properties)
    assert isinstance(parsed, TxtRecord)
    assert parsed.name == "Laptop của Tùng"
    assert parsed.device_id == device_id
    assert parsed.os == "windows"


def test_build_txt_stealth() -> None:
    device_id = "0123456789abcdef0123456789abcdef"
    txt_dict = build_txt(
        device_id=device_id,
        name="Laptop của Tùng",
        os="windows",
        stealth=True,
    )
    raw_properties = {k.encode("ascii"): v.encode("utf-8") for k, v in txt_dict.items()}
    parsed = parse_txt(raw_properties)
    assert isinstance(parsed, TxtRecord)
    # Tên phát sóng bị ẩn danh thành Teleport-<prefix>
    assert parsed.name == f"Teleport-{device_id[:6]}"
    assert parsed.device_id == device_id
    assert parsed.os == "windows"
