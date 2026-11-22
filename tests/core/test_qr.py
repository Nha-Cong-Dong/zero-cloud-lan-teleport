"""Kiểm thử tự động cho module tạo mã QR thuần túy (teleport.core.qr)."""

from __future__ import annotations

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
import pytest

from teleport.core.qr import encode_qr, encode_wifi_qr, format_wifi_qr, render_ascii, render_svg


def _decode_qr_matrix(matrix: list[list[bool]]) -> str:
    """Giải mã ma trận QR boolean bằng bộ giải mã OpenCV chuẩn."""
    if cv2 is None or np is None:
        pytest.skip("opencv-python hoặc numpy chưa được cài đặt")
    border = 4
    size = len(matrix)
    scale = 10
    img_size = (size + 2 * border) * scale
    img = np.ones((img_size, img_size), dtype=np.uint8) * 255

    for r in range(size):
        for c in range(size):
            if matrix[r][c]:
                img[
                    (r + border) * scale : (r + border + 1) * scale,
                    (c + border) * scale : (c + border + 1) * scale,
                ] = 0

    detector = cv2.QRCodeDetector()
    decoded, _points, _straight_qrcode = detector.detectAndDecode(img)
    return str(decoded)


class TestQRCore:
    """Bộ kiểm thử đơn vị và tính toàn vẹn cho teleport.core.qr."""

    def test_encode_qr_level_validation(self) -> None:
        """Kiểm tra xác thực tham số mức sửa lỗi."""
        with pytest.raises(ValueError, match="level phải là 'L' hoặc 'M'"):
            encode_qr("test", level="H")

    def test_encode_qr_too_long(self) -> None:
        """Dữ liệu vượt quá dung lượng tối đa của phiên bản 10 phải báo lỗi."""
        huge_data = "A" * 1000
        with pytest.raises(ValueError, match="vượt quá giới hạn hỗ trợ"):
            encode_qr(huge_data)

    def test_render_ascii(self) -> None:
        """Kiểm tra hàm render_ascii xuất chuỗi hợp lệ."""
        matrix = encode_qr("HELLO")
        ascii_art = render_ascii(matrix, border=2)
        assert isinstance(ascii_art, str)
        assert len(ascii_art) > 0
        assert any(c in ascii_art for c in ("█", "▀", "▄", " "))
        lines = ascii_art.split("\n")
        assert len(lines) > 0
        # Mọi dòng phải có cùng độ dài ký tự
        assert all(len(line) == len(lines[0]) for line in lines)

    def test_render_svg(self) -> None:
        """Kiểm tra hàm render_svg xuất ảnh vector SVG monochrome chuẩn."""
        matrix = encode_qr("ZCL Teleport")
        svg_xml = render_svg(matrix, border=4, scale=8)
        assert svg_xml.startswith("<svg")
        assert svg_xml.strip().endswith("</svg>")
        assert 'fill="#ffffff"' in svg_xml
        assert 'fill="#000000"' in svg_xml
        assert "viewBox=" in svg_xml

    @pytest.mark.parametrize(
        "payload",
        [
            "HELLO",
            "1234567890",
            "http://192.168.1.1:47474",
            "http://192.168.1.105:54321/?token=3f7a19c8d20e4b85a11c039fbcd487ef",
            "https://teleport.local/guest?t=abcxyz&peer=pc-nha",
            "Zero-Cloud LAN Teleport - v1.2.0 Web Guest Mode",
        ],
    )
    def test_qr_decode_roundtrip_level_l(self, payload: str) -> None:
        """Kiểm tra mã hóa và giải mã thành công 100% với mức sửa lỗi L."""
        matrix = encode_qr(payload, level="L")
        decoded = _decode_qr_matrix(matrix)
        assert decoded == payload

    @pytest.mark.parametrize(
        "payload",
        [
            "HELLO",
            "http://10.0.0.2:8080/?token=0123456789abcdef",
            "Chế độ khách ZCL Teleport",
        ],
    )
    def test_qr_decode_roundtrip_level_m(self, payload: str) -> None:
        """Kiểm tra mã hóa và giải mã thành công 100% với mức sửa lỗi M."""
        matrix = encode_qr(payload, level="M")
        decoded = _decode_qr_matrix(matrix)
        assert decoded == payload

    def test_format_wifi_qr_escaping_and_variants(self) -> None:
        """Kiểm tra chuẩn WIFI QR và escape ký tự đặc biệt \\ ; , : \"."""
        assert (
            format_wifi_qr(r'Cafe;Wi\Fi,"Net":1', r"pass\;123", security="WPA", hidden=True)
            == r"WIFI:T:WPA;S:Cafe\;Wi\\Fi\,\"Net\"\:1;P:pass\\\;123;H:true;;"
        )
        assert format_wifi_qr("GuestNet", security="nopass") == "WIFI:T:nopass;S:GuestNet;;"
        assert format_wifi_qr("LegacyNet", "12345", security="WEP") == "WIFI:T:WEP;S:LegacyNet;P:12345;;"

    def test_format_wifi_qr_validation(self) -> None:
        """SSID rỗng, security lạ hoặc WPA thiếu mật khẩu phải báo ValueError."""
        with pytest.raises(ValueError, match="ssid"):
            format_wifi_qr("")
        with pytest.raises(ValueError, match="security"):
            format_wifi_qr("Home", "1234", security="WPA3")
        with pytest.raises(ValueError, match="mật khẩu"):
            format_wifi_qr("Home", "", security="WPA")

    def test_encode_wifi_qr_roundtrip(self) -> None:
        """Giải mã ma trận QR Wi-Fi ra đúng chuỗi WIFI:..."""
        payload = format_wifi_qr("ZCL-LAN", "MatKhau@2026")
        matrix = encode_wifi_qr("ZCL-LAN", "MatKhau@2026")
        assert _decode_qr_matrix(matrix) == payload
