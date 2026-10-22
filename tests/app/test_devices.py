"""Test `teleport.app.devices` theo đặc tả PR-3 mục 2.3 và 4.4 (DV1–DV7), mvp F12.

Hàm thuần: `parse_manual_address` (chỉ IPv4 viết chuẩn, cổng ASCII 1–65535) và `resolve_trusted`
(khớp nguyên tên theo NFC + casefold, hoặc tiền tố mã ≥ 4 ký tự hex; nhiều kết quả thì không đoán).
"""

import datetime
import unicodedata

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.app.devices import (
    MIN_ID_PREFIX,
    AmbiguousDevice,
    DeviceLookupError,
    DeviceNotFound,
    parse_manual_address,
    resolve_trusted,
)
from teleport.net.connection import DEFAULT_PORT
from teleport.trust.store import TrustedDevice

PAIRED_AT = datetime.datetime(2026, 10, 20, 8, 0, 0, tzinfo=datetime.UTC)


def dev(device_id: str, name: str) -> TrustedDevice:
    """Mục giả (hàm thuần không đọc chứng chỉ)."""
    return TrustedDevice(
        device_id=device_id,
        name=name,
        fingerprint=device_id * 2,
        cert_pem="-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n",
        paired_at=PAIRED_AT,
    )


def sort_key(d: TrustedDevice) -> tuple[str, str]:
    return (unicodedata.normalize("NFC", d.name).casefold(), d.device_id)


ID_A = "a1b2c3d4" + "0" * 24
ID_B = "b7e1" + "f" * 28
ID_C = "c0ffee00" + "1" * 24


def test_constants():
    assert MIN_ID_PREFIX == 4
    assert DEFAULT_PORT == 47474
    assert issubclass(DeviceNotFound, DeviceLookupError)
    assert issubclass(AmbiguousDevice, DeviceLookupError)


# --- DV1 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("192.168.1.5", ("192.168.1.5", 47474)),
        ("192.168.1.5:8080", ("192.168.1.5", 8080)),
        (" 10.0.0.2:1 ", ("10.0.0.2", 1)),
        ("\t10.0.0.2\t", ("10.0.0.2", 47474)),
        ("127.0.0.1:65535", ("127.0.0.1", 65535)),
        ("127.0.0.1", ("127.0.0.1", 47474)),
        ("1.2.3.4:47474", ("1.2.3.4", 47474)),
    ],
)
def test_parse_manual_address_accepts(text, expected):
    """DV1."""
    result = parse_manual_address(text)
    assert result == expected
    assert type(result[0]) is str
    assert type(result[1]) is int


def test_parse_manual_address_uses_default_port_argument():
    """DV1: `default_port` được dùng khi thiếu cổng, không đè cổng đã nhập."""
    assert parse_manual_address("192.168.1.5", default_port=5000) == ("192.168.1.5", 5000)
    assert parse_manual_address("192.168.1.5:80", default_port=5000) == ("192.168.1.5", 80)


# --- DV2 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "localhost",
        "pc-nha",
        "192.168.1",
        "192.168.1.256",
        "01.2.3.4",
        "1.2.3.04",
        "1.2.3.4:",
        "1.2.3.4:0",
        ":80",
        "1.2.3.4:65536",
        "1.2.3.4:99999",
        "1.2.3.4:080",
        "1.2.3.4:+80",
        "1.2.3.4:-80",
        "1.2.3.4:８０",
        "１.2.3.4",
        "1.2.3.4:80:90",
        "[::1]:80",
        "::1",
        "0.0.0.0",
        "0.0.0.0:80",
        "224.0.0.251",
        "239.255.255.250:1900",
        "255.255.255.255",
        "1.2.3.4 :80",
        "1.2.3.4: 80",
        "1.2.3.4\n5",
        " 1.2.3.4",
        "1.2.3.4/24",
        "1.2.3.4:80a",
        "1.2.3.4:" + "1" * 200,
        "1" * 10_000,
    ],
)
def test_parse_manual_address_rejects(text):
    """DV2."""
    with pytest.raises(ValueError):
        parse_manual_address(text)


@pytest.mark.parametrize("value", [None, b"1.2.3.4", 1234, ["1.2.3.4"]])
def test_parse_manual_address_rejects_non_str_with_value_error(value):
    """DV2: không phải `str` → ValueError (không phải TypeError)."""
    with pytest.raises(ValueError):
        parse_manual_address(value)


def test_parse_manual_address_error_message_gives_examples():
    """Thông điệp tiếng Việt kèm ví dụ đúng."""
    with pytest.raises(ValueError) as exc:
        parse_manual_address("pc-nha")
    msg = str(exc.value)
    assert "192.168.1.5" in msg
    assert "192.168.1.5:47474" in msg


# --- DV3 ------------------------------------------------------------------------------------------


def test_resolve_by_name_is_case_insensitive():
    """DV3."""
    a = dev(ID_A, "PC-Nha")
    assert resolve_trusted("pc-nha", [a, dev(ID_B, "Laptop")]) is a
    assert resolve_trusted("  PC-NHA  ", [a]) is a


def test_resolve_by_name_nfd_query_matches_nfc_name():
    """DV3: tên tiếng Việt gõ dạng NFD khớp tên đã lưu dạng NFC (và ngược lại)."""
    nfc = unicodedata.normalize("NFC", "Máy Nhà Ông Đức")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    a = dev(ID_A, nfc)
    assert resolve_trusted(nfd, [a]) is a
    b = dev(ID_B, nfd)
    assert resolve_trusted(nfc, [b]) is b


def test_resolve_name_prefix_does_not_match():
    """DV3: tiền tố của tên (`PC`) không khớp."""
    with pytest.raises(DeviceNotFound):
        resolve_trusted("PC", [dev(ID_A, "PC-Nha")])
    with pytest.raises(DeviceNotFound):
        resolve_trusted("PC-Nh", [dev(ID_A, "PC-Nha")])


# --- DV4 ------------------------------------------------------------------------------------------


def test_resolve_by_id_prefix_of_four():
    """DV4."""
    a, b = dev(ID_A, "A"), dev(ID_B, "B")
    assert resolve_trusted(ID_A[:4], [a, b]) is a
    assert resolve_trusted(ID_B[:MIN_ID_PREFIX], [a, b]) is b


def test_resolve_three_char_prefix_is_not_an_id():
    """DV4: 3 ký tự không tính là mã."""
    with pytest.raises(DeviceNotFound):
        resolve_trusted(ID_A[:3], [dev(ID_A, "A")])


def test_resolve_uppercase_prefix_and_full_id():
    """DV4: tiền tố viết hoa khớp; mã đủ 32 ký tự khớp."""
    a = dev(ID_A, "A")
    assert resolve_trusted(ID_A[:8].upper(), [a]) is a
    assert resolve_trusted(ID_A, [a]) is a
    assert resolve_trusted(ID_A.upper(), [a]) is a


def test_resolve_non_hex_or_non_ascii_prefix_is_not_an_id():
    a = dev(ID_A, "A")
    for q in ["a1b2g", "ａ１ｂ２", "a1b2c3d4" + "0" * 25, "a1 b2"]:
        with pytest.raises(DeviceNotFound):
            resolve_trusted(q, [a])


# --- DV5 ------------------------------------------------------------------------------------------


def test_same_name_different_case_is_ambiguous_with_sorted_candidates():
    """DV5: hai thiết bị cùng tên khác hoa thường → AmbiguousDevice, sắp đúng thứ tự."""
    x = dev(ID_C, "laptop")
    y = dev(ID_A, "LAPTOP")
    z = dev(ID_B, "Khác")
    with pytest.raises(AmbiguousDevice) as exc:
        resolve_trusted("Laptop", [x, z, y])
    err = exc.value
    assert isinstance(err.candidates, tuple)
    assert set(err.candidates) == {x, y}
    assert list(err.candidates) == sorted([x, y], key=sort_key)
    assert err.query == "Laptop"


def test_name_equal_to_id_prefix_of_other_device_is_ambiguous():
    """DV5: tên của thiết bị này là tiền tố mã của thiết bị kia → AmbiguousDevice."""
    named = dev(ID_A, ID_B[:6])
    other = dev(ID_B, "B")
    with pytest.raises(AmbiguousDevice) as exc:
        resolve_trusted(ID_B[:6], [named, other])
    assert set(exc.value.candidates) == {named, other}


def test_name_and_id_match_same_device_is_not_ambiguous():
    """Gộp hai tập và bỏ trùng: cùng một thiết bị khớp cả tên lẫn mã → trả về nó."""
    d = dev(ID_A, ID_A[:8])
    assert resolve_trusted(ID_A[:8], [d]) is d


def test_ambiguous_id_prefix():
    a = dev("abcd" + "0" * 28, "A")
    b = dev("abcd" + "1" * 28, "B")
    with pytest.raises(AmbiguousDevice) as exc:
        resolve_trusted("ABCD", [b, a])
    assert list(exc.value.candidates) == sorted([a, b], key=sort_key)
    assert resolve_trusted("abcd1", [a, b]) is b


# --- DV6 ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["khong-co", "", "   ", "\t", "zzzz"])
def test_not_found_keeps_original_query(query):
    """DV6."""
    with pytest.raises(DeviceNotFound) as exc:
        resolve_trusted(query, [dev(ID_A, "A"), dev(ID_B, "B")])
    assert exc.value.query == query


def test_not_found_on_empty_device_list():
    with pytest.raises(DeviceNotFound):
        resolve_trusted("A", [])


def test_accepts_any_iterable():
    a = dev(ID_A, "A")
    assert resolve_trusted("A", iter([a])) is a
    assert resolve_trusted("a", (d for d in [a])) is a


# --- DV7: property --------------------------------------------------------------------------------


def oracle(query: str, devices: list[TrustedDevice]) -> list[TrustedDevice]:
    """Định nghĩa ở đặc tả mục 2.3, viết lại độc lập."""
    q = query.strip()
    if not q:
        return []
    key = unicodedata.normalize("NFC", q).casefold()
    by_name = [d for d in devices if unicodedata.normalize("NFC", d.name).casefold() == key]
    low = q.lower()
    is_hex = len(q) >= 4 and all(c in "0123456789abcdef" for c in low)
    by_id = [d for d in devices if is_hex and d.device_id.startswith(low)]
    out: list[TrustedDevice] = []
    for d in by_name + by_id:
        if d not in out:
            out.append(d)
    return out


NAME_ALPHABET = "aAbBcCđĐeE01 -éÉ́e"
device_ids = st.text("0123456789abcdef", min_size=32, max_size=32)
names = st.text(NAME_ALPHABET, min_size=1, max_size=6)


@st.composite
def devices_and_query(draw):
    ids = draw(st.lists(device_ids, min_size=0, max_size=6, unique=True))
    # Mã chung tiền tố để có nhiều ca mơ hồ theo mã.
    if ids and draw(st.booleans()):
        base = ids[0][:5]
        ids = [base + i[5:] if draw(st.booleans()) else i for i in ids]
        ids = list(dict.fromkeys(ids))
    devices = [dev(i, draw(names)) for i in ids]
    choice = draw(st.integers(0, 3))
    if choice == 0 and devices:
        d = draw(st.sampled_from(devices))
        n = draw(st.integers(0, 32))
        q = d.device_id[:n]
        if draw(st.booleans()):
            q = q.upper()
    elif choice == 1 and devices:
        d = draw(st.sampled_from(devices))
        q = d.name
        form = draw(st.sampled_from(["NFC", "NFD", "upper", "lower", "same"]))
        if form in ("NFC", "NFD"):
            q = unicodedata.normalize(form, q)
        elif form != "same":
            q = getattr(q, form)()
    elif choice == 2:
        q = draw(st.text("0123456789abcdefABCDEF", max_size=8))
    else:
        q = draw(st.text(max_size=10))
    if draw(st.booleans()):
        q = draw(st.sampled_from([" ", "\t", ""])) + q + draw(st.sampled_from([" ", ""]))
    return devices, q


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(devices_and_query())
def test_resolve_matches_definition(case):
    """DV7: kết quả ⇔ định nghĩa; chỉ raise DeviceNotFound / AmbiguousDevice."""
    devices, query = case
    expected = oracle(query, devices)
    if len(expected) == 1:
        assert resolve_trusted(query, devices) is expected[0]
    elif not expected:
        with pytest.raises(DeviceNotFound) as exc:
            resolve_trusted(query, devices)
        assert exc.value.query == query
    else:
        with pytest.raises(AmbiguousDevice) as exc:
            resolve_trusted(query, devices)
        assert exc.value.query == query
        assert list(exc.value.candidates) == sorted(expected, key=sort_key)


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=40))
def test_parse_manual_address_only_raises_value_error(text):
    """Chuỗi bất kỳ: hoặc ra (ip, cổng) hợp lệ, hoặc ValueError; không lỗi nào khác."""
    import ipaddress

    try:
        ip, port = parse_manual_address(text)
    except ValueError:
        return
    addr = ipaddress.IPv4Address(ip)
    assert str(addr) == ip
    assert not (addr.is_unspecified or addr.is_multicast or ip == "255.255.255.255")
    assert 1 <= port <= 65535
    assert ip in text
