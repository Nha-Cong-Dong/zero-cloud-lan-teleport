"""Bộ mã hóa mã QR thuần túy (sans-IO) cho ZCL Teleport (v1.2.0).

Triển khai tiêu chuẩn QR Code Model 2 (ISO/IEC 18004) bằng Python thuần túy,
không phụ thuộc bất kỳ thư viện bên thứ ba nào.

Hỗ trợ:
- Chế độ Byte (8-bit) cho chuỗi URL hoặc văn bản bất kỳ.
- Mức sửa lỗi L (7%) và M (15%).
- Tự động chọn phiên bản nhỏ nhất phù hợp (phiên bản 1 đến 10).
- Xuất ma trận boolean (True = đen, False = trắng).
- Xuất chuỗi hiển thị dòng lệnh (Unicode half-block / full-block) với viền tĩnh.
- Xuất định dạng vector SVG nét đơn tối giản (monochrome).
"""

from __future__ import annotations

from collections.abc import Sequence

# Bảng Galois Field GF(256) với số nguyên thủy 0x11d (285)
_EXP = [0] * 512
_LOG = [0] * 256

_val = 1
for _i in range(255):
    _EXP[_i] = _val
    _EXP[_i + 255] = _val
    _LOG[_val] = _i
    _val = (_val << 1) ^ (0x11D if (_val & 0x80) else 0)


def _gf_mul(x: int, y: int) -> int:
    if x == 0 or y == 0:
        return 0
    return _EXP[_LOG[x] + _LOG[y]]


def _rs_poly(degree: int) -> list[int]:
    """Sinh đa thức cực tiểu cho mã sửa lỗi Reed-Solomon."""
    g = [1]
    for i in range(degree):
        term = [1, _EXP[i]]
        # Nhân g với term
        new_g = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            new_g[j] ^= _gf_mul(c, term[0])
            new_g[j + 1] ^= _gf_mul(c, term[1])
        g = new_g
    return g


def _rs_encode(data: Sequence[int], n_ecc: int) -> list[int]:
    """Tạo các byte sửa lỗi Reed-Solomon."""
    gen = _rs_poly(n_ecc)
    msg = list(data) + [0] * n_ecc
    for i in range(len(data)):
        lead = msg[i]
        if lead != 0:
            for j, c in enumerate(gen):
                msg[i + j] ^= _gf_mul(lead, c)
    return msg[len(data) :]


# Cấu hình các phiên bản QR (1 đến 10):
# Định dạng: (total_codewords, data_L, ecc_per_block_L, blocks_L, data_M, ecc_per_block_M, blocks_M)
_VERSION_SPECS: dict[int, tuple[int, int, int, int, int, int, int]] = {
    1: (26, 19, 7, 1, 16, 10, 1),
    2: (44, 34, 10, 1, 28, 16, 1),
    3: (70, 55, 15, 1, 44, 26, 1),
    4: (100, 80, 20, 1, 64, 18, 2),
    5: (134, 108, 26, 1, 86, 24, 2),
    6: (172, 136, 18, 2, 108, 16, 4),
    7: (196, 156, 20, 2, 124, 18, 4),
    8: (242, 194, 24, 2, 154, 22, 4),
    9: (292, 232, 30, 2, 182, 22, 5),
    10: (346, 274, 18, 4, 216, 26, 5),
}

# Vị trí các điểm căn chỉnh (Alignment pattern coordinates) cho phiên bản 1..10
_ALIGNMENT_LOCATIONS: dict[int, list[int]] = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}


class BitBuffer:
    """Bộ đệm ghi từng bit nhị phân."""

    def __init__(self) -> None:
        self.bits: list[int] = []

    def append(self, val: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            self.bits.append((val >> i) & 1)

    def to_bytes(self) -> bytearray:
        res = bytearray()
        for i in range(0, len(self.bits), 8):
            byte = 0
            chunk = self.bits[i : i + 8]
            for bit in chunk:
                byte = (byte << 1) | bit
            if len(chunk) < 8:
                byte <<= 8 - len(chunk)
            res.append(byte)
        return res


def _encode_data(data_bytes: bytes, version: int, level: str) -> bytearray:
    buf = BitBuffer()
    # Chế độ Byte: chỉ báo 0100
    buf.append(0b0100, 4)
    # Độ dài dữ liệu: phiên bản 1..9 dùng 8 bit, 10 dùng 16 bit
    char_count_bits = 16 if version >= 10 else 8
    buf.append(len(data_bytes), char_count_bits)

    # Thêm dữ liệu byte
    for b in data_bytes:
        buf.append(b, 8)

    spec = _VERSION_SPECS[version]
    data_capacity = spec[1] if level == "L" else spec[4]

    # Kết thúc chuỗi bit (terminator 0000 tối đa 4 bit)
    total_data_bits = data_capacity * 8
    rem = total_data_bits - len(buf.bits)
    if rem > 0:
        buf.append(0, min(4, rem))

    # Đệm cho tròn byte (byte padding)
    pad_to_byte = (8 - (len(buf.bits) % 8)) % 8
    if pad_to_byte > 0:
        buf.append(0, pad_to_byte)

    # Đệm xen kẽ 0xEC và 0x11 cho tới khi đủ dung lượng
    raw_bytes = buf.to_bytes()
    pad_bytes = [0xEC, 0x11]
    pad_idx = 0
    while len(raw_bytes) < data_capacity:
        raw_bytes.append(pad_bytes[pad_idx])
        pad_idx = (pad_idx + 1) % 2

    return raw_bytes


def _interleave_blocks(raw_bytes: bytearray, version: int, level: str) -> bytearray:
    spec = _VERSION_SPECS[version]
    if level == "L":
        _, total_data, ecc_per_block, num_blocks, _, _, _ = spec
    else:
        _, _, _, _, total_data, ecc_per_block, num_blocks = spec

    base_len = total_data // num_blocks
    extra = total_data % num_blocks

    data_blocks: list[bytearray] = []
    ecc_blocks: list[list[int]] = []

    offset = 0
    for b in range(num_blocks):
        block_len = base_len + (1 if b >= num_blocks - extra else 0)
        chunk = raw_bytes[offset : offset + block_len]
        offset += block_len
        data_blocks.append(chunk)
        ecc_blocks.append(_rs_encode(chunk, ecc_per_block))

    # Ghép xen kẽ dữ liệu
    result = bytearray()
    max_data_len = max(len(b) for b in data_blocks)
    for i in range(max_data_len):
        for block in data_blocks:
            if i < len(block):
                result.append(block[i])

    # Ghép xen kẽ mã sửa lỗi
    for i in range(ecc_per_block):
        for ecc in ecc_blocks:
            result.append(ecc[i])

    return result


def encode_qr(text_or_bytes: str | bytes, level: str = "L") -> list[list[bool]]:
    """Tạo ma trận mã QR nhị phân từ chuỗi hoặc dữ liệu byte.

    `level`: "L" (sửa lỗi 7%) hoặc "M" (sửa lỗi 15%).
    Trả về ma trận 2 chiều các giá trị boolean: True cho điểm đen, False cho điểm trắng.
    """
    data = text_or_bytes.encode("utf-8") if isinstance(text_or_bytes, str) else bytes(text_or_bytes)

    if level not in ("L", "M"):
        raise ValueError("level phải là 'L' hoặc 'M'")

    # Tìm phiên bản tối thiểu chứa đủ dữ liệu
    target_version: int | None = None
    for v in sorted(_VERSION_SPECS.keys()):
        cap = _VERSION_SPECS[v][1] if level == "L" else _VERSION_SPECS[v][4]
        overhead = 2 if v >= 10 else 1  # 4 bit mode + 8/16 bit count
        if len(data) + overhead <= cap:
            target_version = v
            break

    if target_version is None:
        raise ValueError(f"Dữ liệu ({len(data)} byte) vượt quá giới hạn hỗ trợ phiên bản 10")

    version = target_version
    size = version * 4 + 17
    matrix: list[list[bool | None]] = [[None] * size for _ in range(size)]
    reserved: list[list[bool]] = [[False] * size for _ in range(size)]

    # 1. Vẽ các Finder Patterns (7x7 ở 3 góc)
    def put_finder(top_r: int, top_c: int) -> None:
        for r in range(7):
            for c in range(7):
                is_black = (
                    r == 0
                    or r == 6
                    or c == 0
                    or c == 6
                    or (2 <= r <= 4 and 2 <= c <= 4)
                )
                matrix[top_r + r][top_c + c] = is_black
                reserved[top_r + r][top_c + c] = True

        # Vùng phân cách màu trắng (Separator)
        for r in range(-1, 8):
            for c in range(-1, 8):
                nr, nc = top_r + r, top_c + c
                if 0 <= nr < size and 0 <= nc < size:
                    reserved[nr][nc] = True
                    if matrix[nr][nc] is None:
                        matrix[nr][nc] = False

    put_finder(0, 0)
    put_finder(0, size - 7)
    put_finder(size - 7, 0)

    # 2. Điểm căn chỉnh (Alignment patterns) cho phiên bản >= 2
    align_coords = _ALIGNMENT_LOCATIONS[version]
    for r in align_coords:
        for c in align_coords:
            # Bỏ qua nếu trùng với 3 góc finder
            if (r < 9 and c < 9) or (r < 9 and c > size - 10) or (r > size - 10 and c < 9):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    is_black = dr in (-2, 2) or dc in (-2, 2) or (dr == 0 and dc == 0)
                    matrix[r + dr][c + dc] = is_black
                    reserved[r + dr][c + dc] = True

    # 3. Đường định thời (Timing patterns)
    for i in range(8, size - 8):
        is_black = i % 2 == 0
        if not reserved[6][i]:
            matrix[6][i] = is_black
            reserved[6][i] = True
        if not reserved[i][6]:
            matrix[i][6] = is_black
            reserved[i][6] = True

    # Điểm đen cố định (Dark module)
    matrix[size - 8][8] = True
    reserved[size - 8][8] = True

    # Đặt chỗ thông tin định dạng (Format Information area)
    for i in range(9):
        if i != 6:
            reserved[8][i] = True
            reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True

    # 4. Chuẩn bị dòng bit dữ liệu hoàn chỉnh
    raw_data = _encode_data(data, version, level)
    final_codewords = _interleave_blocks(raw_data, version, level)

    bits: list[int] = []
    for byte in final_codewords:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)

    # Thêm số bit thừa (remainder bits) cho phiên bản 2..6
    remainder_bits = {2: 7, 3: 7, 4: 7, 5: 7, 6: 7}.get(version, 0)
    bits.extend([0] * remainder_bits)

    # 5. Rải bit vào ma trận theo hình zigzag
    bit_idx = 0
    num_bits = len(bits)
    right = size - 1
    up = True

    while right > 0:
        if right == 6:
            right -= 1  # Bỏ qua cột timing pattern dọc
        rows = range(size - 1, -1, -1) if up else range(size)
        for r in rows:
            for c in (right, right - 1):
                if not reserved[r][c]:
                    val = bits[bit_idx] if bit_idx < num_bits else 0
                    matrix[r][c] = bool(val)
                    bit_idx += 1
        up = not up
        right -= 2

    # 6. Áp mặt nạ (Mask 0: (r + c) % 2 == 0) và gán thông tin định dạng
    # Mã BCH cho format info (15 bit): Level L + Mask 0 = 0b01000 -> 0x77c4 sau khi XOR 0x5412
    # Level M + Mask 0 = 0b00000 -> 0x5412 sau khi XOR 0x5412
    fmt_bits_val = 0x77C4 if level == "L" else 0x5412

    def fmt_bit(k: int) -> bool:
        return bool((fmt_bits_val >> k) & 1)

    # Áp mặt nạ 0 lên các ô dữ liệu
    out_matrix: list[list[bool]] = []
    for r in range(size):
        row: list[bool] = []
        for c in range(size):
            cell = matrix[r][c]
            if not reserved[r][c]:
                assert cell is not None
                # Mặt nạ 0: đảo bit nếu (r + c) % 2 == 0
                if (r + c) % 2 == 0:
                    cell = not cell
            elif cell is None:
                cell = False
            row.append(cell)
        out_matrix.append(row)

    # Điền 15 bit định dạng vào ma trận theo tiêu chuẩn ISO/IEC 18004
    # 1. Quanh góc trên trái: bit 14 (MSB) -> bit 0 (LSB)
    out_matrix[8][0] = fmt_bit(14)
    out_matrix[8][1] = fmt_bit(13)
    out_matrix[8][2] = fmt_bit(12)
    out_matrix[8][3] = fmt_bit(11)
    out_matrix[8][4] = fmt_bit(10)
    out_matrix[8][5] = fmt_bit(9)
    out_matrix[8][7] = fmt_bit(8)
    out_matrix[8][8] = fmt_bit(7)
    out_matrix[7][8] = fmt_bit(6)
    out_matrix[5][8] = fmt_bit(5)
    out_matrix[4][8] = fmt_bit(4)
    out_matrix[3][8] = fmt_bit(3)
    out_matrix[2][8] = fmt_bit(2)
    out_matrix[1][8] = fmt_bit(1)
    out_matrix[0][8] = fmt_bit(0)

    # 2. Quanh góc trên phải: bit 0..7
    for i in range(8):
        out_matrix[8][size - 1 - i] = fmt_bit(i)

    # 3. Quanh góc dưới trái: bit 8..14
    for i in range(7):
        out_matrix[size - 7 + i][8] = fmt_bit(8 + i)

    return out_matrix


def render_ascii(matrix: list[list[bool]], border: int = 2) -> str:
    """Xuất mã QR sang chuỗi ký tự hiển thị dòng lệnh dùng ký tự nửa khối Unicode.

    Hai dòng được gộp vào 1 ký tự (`▀` trên đen dưới trắng, `▄` trên trắng dưới đen, `█` đen cả hai).
    Đảm bảo tỷ lệ khung hình cân đối, trực quan khi quét bằng camera điện thoại.
    """
    size = len(matrix)
    # Tạo vùng đệm viền trắng (quiet zone)
    full_size = size + 2 * border
    grid = [[False] * full_size for _ in range(full_size)]
    for r in range(size):
        for c in range(size):
            grid[r + border][c + border] = matrix[r][c]

    lines: list[str] = []
    # Quét từng cặp dòng
    for r in range(0, full_size, 2):
        line_chars: list[str] = []
        for c in range(full_size):
            top = grid[r][c]
            bottom = grid[r + 1][c] if r + 1 < full_size else False
            # Trong terminal nền đen: ký tự sáng biểu thị module trắng, ký tự tối biểu thị module đen
            # Ở đây sử dụng quy ước module đen là ' ' trên nền đảo ngược hoặc các khối:
            if top and bottom:
                line_chars.append("█")
            elif top and not bottom:
                line_chars.append("▀")
            elif not top and bottom:
                line_chars.append("▄")
            else:
                line_chars.append(" ")
        lines.append("".join(line_chars))
    return "\n".join(lines)


def render_svg(matrix: list[list[bool]], border: int = 4, scale: int = 8) -> str:
    """Xuất mã QR sang định dạng ảnh vector SVG monochrome tối giản."""
    size = len(matrix)
    total_size = (size + 2 * border) * scale
    rects: list[str] = []
    for r in range(size):
        for c in range(size):
            if matrix[r][c]:
                x = (c + border) * scale
                y = (r + border) * scale
                rects.append(f'<rect x="{x}" y="{y}" width="{scale}" height="{scale}" fill="#000000"/>')

    content = "\n  ".join(rects)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_size} {total_size}" '
        f'width="{total_size}" height="{total_size}">\n'
        f'  <rect width="100%" height="100%" fill="#ffffff"/>\n'
        f"  {content}\n"
        f"</svg>"
    )
