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


@pytest.mark.slow
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


# =================================================================================================
# PR-4: `DeviceView`, `DeviceRegistry`, `resolve_device` (đặc tả PR-4 mục 2.4, 4.4: DR1–DR5, RD1–RD4).
# Tên mới được import trong từng test, để các test PR-3 ở trên vẫn chạy khi chúng chưa có.
# RD4 = mọi test `resolve_trusted` ở trên chạy không sửa.
# =================================================================================================

OWN_ID = "8" * 32
ID_D = "d00d" + "2" * 28


def seen_dev(device_id: str, name: str, *, os: str = "windows", port: int = 47474, addresses=("192.0.2.10",)):
    from teleport.core.discovery import DiscoveredDevice
    from teleport.core.messages import PROTOCOL_VERSION

    return DiscoveredDevice(
        device_id=device_id, name=name, os=os, proto=PROTOCOL_VERSION, port=port, addresses=tuple(addresses)
    )


def registry(*trusted: TrustedDevice):
    from teleport.app.devices import DeviceRegistry

    r = DeviceRegistry(OWN_ID)
    r.set_trusted(list(trusted))
    return r


def view(device_id: str, name: str, *, paired: bool = True, online: bool = True, os: str | None = "windows"):
    from teleport.app.devices import DeviceView

    return DeviceView(
        device_id=device_id,
        name=name,
        os=os if online else None,
        paired=paired,
        online=online,
        connected=False,
        addresses=("192.0.2.10",) if online else (),
        port=47474 if online else None,
    )


def test_device_view_is_frozen():
    import dataclasses

    v = view(ID_A, "A")
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.name = "B"  # type: ignore[misc]


def test_dr1_paired_offline_device_view():
    from teleport.app.devices import DeviceView

    r = registry(dev(ID_A, "PC Nhà"))
    assert r.get(ID_A) == DeviceView(
        device_id=ID_A,
        name="PC Nhà",
        os=None,
        paired=True,
        online=False,
        connected=False,
        addresses=(),
        port=None,
    )
    assert r.views() == (r.get(ID_A),)


def test_dr1_seen_paired_device_keeps_trusted_name():
    r = registry(dev(ID_A, "PC Nhà"))
    r.seen(seen_dev(ID_A, "Ten-gia-mao", os="linux", port=50000, addresses=("192.0.2.1", "192.0.2.2")))
    v = r.get(ID_A)
    assert v.name == "PC Nhà"  # tên mDNS không đổi tên thiết bị đã ghép đôi
    assert v.paired is True and v.online is True and v.connected is False
    assert v.os == "linux"
    assert v.addresses == ("192.0.2.1", "192.0.2.2")
    assert v.port == 50000


def test_dr1_seen_unpaired_device_uses_txt_name():
    r = registry()
    r.seen(seen_dev(ID_B, "Laptop", os="macos"))
    v = r.get(ID_B)
    assert v.name == "Laptop" and v.paired is False and v.online is True and v.os == "macos"
    assert v.port == 47474 and v.addresses == ("192.0.2.10",)


def test_dr1_seen_update_replaces_address_and_port():
    r = registry()
    r.seen(seen_dev(ID_B, "Laptop", port=1000))
    r.seen(seen_dev(ID_B, "Laptop 2", port=2000, addresses=("192.0.2.99",)))
    v = r.get(ID_B)
    assert (v.name, v.port, v.addresses) == ("Laptop 2", 2000, ("192.0.2.99",))
    assert len(r.views()) == 1


def test_dr2_gone_removes_unpaired_and_marks_paired_offline():
    r = registry(dev(ID_A, "A"))
    r.seen(seen_dev(ID_A, "A"))
    r.seen(seen_dev(ID_B, "B"))
    r.gone(ID_B)
    assert r.get(ID_B) is None
    r.gone(ID_A)
    v = r.get(ID_A)
    assert (v.online, v.os, v.addresses, v.port, v.paired) == (False, None, (), None, True)
    r.gone(ID_C)  # lạ: không lỗi
    assert [x.device_id for x in r.views()] == [ID_A]


def test_dr2_own_device_never_has_a_view():
    from teleport.app.devices import DeviceRegistry

    r = DeviceRegistry(OWN_ID)
    r.seen(seen_dev(OWN_ID, "Máy này"))
    assert r.get(OWN_ID) is None
    r.set_trusted([dev(OWN_ID, "Máy này")])
    assert r.get(OWN_ID) is None
    assert r.views() == ()


def test_dr3_untrusting_seen_device_keeps_view_as_unpaired():
    r = registry(dev(ID_A, "Tên lưu"), dev(ID_B, "B"))
    r.seen(seen_dev(ID_A, "Tên TXT"))
    r.set_connected(ID_A, True)
    assert r.get(ID_A).connected is True
    r.set_trusted([dev(ID_B, "B")])
    v = r.get(ID_A)
    assert (v.paired, v.connected, v.name, v.online) == (False, False, "Tên TXT", True)


def test_dr3_untrusting_unseen_device_removes_view():
    r = registry(dev(ID_A, "A"), dev(ID_B, "B"))
    r.set_trusted([dev(ID_B, "B")])
    assert r.get(ID_A) is None
    assert [v.device_id for v in r.views()] == [ID_B]


def test_dr3_trusting_seen_device_uses_trusted_name():
    r = registry()
    r.seen(seen_dev(ID_A, "Tên TXT"))
    r.set_trusted([dev(ID_A, "Tên lưu")])
    v = r.get(ID_A)
    assert (v.paired, v.name, v.online) == (True, "Tên lưu", True)


def test_dr4_set_connected_only_for_paired():
    r = registry(dev(ID_A, "A"))
    r.seen(seen_dev(ID_B, "B"))
    before = r.views()
    r.set_connected(ID_B, True)
    r.set_connected(ID_C, True)  # không có view
    assert r.views() == before
    assert r.get(ID_C) is None
    r.set_connected(ID_A, True)
    assert r.get(ID_A).connected is True  # đang offline vẫn có kết nối được (mDNS chỉ là gợi ý)
    r.set_connected(ID_A, False)
    assert r.get(ID_A).connected is False


def test_dr4_views_sorted_by_folded_name_then_id():
    r = registry(dev(ID_C, "b"), dev(ID_A, "B"))
    r.seen(seen_dev(ID_B, "a"))
    r.seen(seen_dev(ID_D, unicodedata.normalize("NFD", "Ä")))
    names = [(v.name, v.device_id) for v in r.views()]
    expected = sorted(names, key=lambda x: (unicodedata.normalize("NFC", x[0]).casefold(), x[1]))
    assert names == expected
    assert [v.device_id for v in r.views()][:3] == [ID_B, ID_A, ID_C]


def test_dr5_clear_seen_returns_seen_ids_and_marks_all_offline():
    r = registry(dev(ID_A, "A"), dev(ID_C, "C"))
    r.seen(seen_dev(ID_A, "A"))
    r.seen(seen_dev(ID_B, "B"))
    ids = r.clear_seen()
    assert isinstance(ids, tuple)
    assert set(ids) == {ID_A, ID_B}
    assert not any(v.online for v in r.views())
    assert r.get(ID_B) is None  # chưa ghép đôi → mất view
    assert {v.device_id for v in r.views()} == {ID_A, ID_C}
    assert r.clear_seen() == ()


def test_registry_never_changes_trusted_list():
    """Dữ liệu mDNS chỉ là gợi ý: `seen` không biến thiết bị lạ thành đã ghép đôi."""
    r = registry(dev(ID_A, "A"))
    r.seen(seen_dev(ID_B, "A"))  # cùng tên với thiết bị đã ghép đôi
    assert r.get(ID_B).paired is False
    assert r.get(ID_A).paired is True and r.get(ID_A).online is False


# --- RD1–RD3 ------------------------------------------------------------------------------------------


def test_rd1_resolve_device_by_name_prefix_and_full_id():
    from teleport.app.devices import resolve_device

    views = [view(ID_A, "PC Nhà"), view(ID_B, "Laptop", paired=False), view(ID_C, "Văn phòng", online=False)]
    assert resolve_device("pc nhà", views) is views[0]
    assert resolve_device(unicodedata.normalize("NFD", "PC NHÀ"), views) is views[0]
    assert resolve_device(ID_B[:4], views) is views[1]
    assert resolve_device(ID_B[:4].upper(), views) is views[1]
    assert resolve_device(ID_A, views) is views[0]
    assert resolve_device("văn phòng", views) is views[2]  # khớp cả view offline
    assert resolve_device(f"  {ID_C[:6]} ", views) is views[2]


def test_rd2_same_name_paired_and_unpaired_is_ambiguous():
    from teleport.app.devices import resolve_device

    views = [view(ID_C, "pc"), view(ID_A, "PC", paired=False), view(ID_B, "other")]
    with pytest.raises(AmbiguousDevice) as exc:
        resolve_device("Pc", views)
    assert exc.value.query == "Pc"
    assert [c.device_id for c in exc.value.candidates] == [ID_A, ID_C]


def test_rd2_not_found_lists_given_views():
    from teleport.app.devices import resolve_device

    views = [view(ID_B, "b"), view(ID_A, "a", paired=False)]
    with pytest.raises(DeviceNotFound) as exc:
        resolve_device("khong-co", views)
    assert exc.value.query == "khong-co"
    assert [v.device_id for v in exc.value.known] == [ID_A, ID_B]
    with pytest.raises(DeviceNotFound):
        resolve_device("", [])
    with pytest.raises(DeviceNotFound):
        resolve_device(ID_A[:3], views)  # < 4 ký tự: không phải mã


def test_rd_shared_exceptions_still_work_with_trusted_devices():
    a, b = dev(ID_A, "x"), dev(ID_B, "X")
    exc = AmbiguousDevice("x", [b, a])
    assert [c.device_id for c in exc.candidates] == [ID_A, ID_B]
    nf = DeviceNotFound("q", [b, a])
    assert [c.device_id for c in nf.known] == [ID_A, ID_B]
    assert DeviceNotFound("q").known == ()


@st.composite
def views_and_query(draw):
    devices, q = draw(devices_and_query())
    out = []
    for d in devices:
        out.append(view(d.device_id, d.name, paired=draw(st.booleans()), online=draw(st.booleans())))
    return out, q


@pytest.mark.slow
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(views_and_query())
def test_rd3_resolve_device_matches_definition(case):
    from teleport.app.devices import resolve_device

    views, query = case
    expected = oracle(query, views)
    if len(expected) == 1:
        assert resolve_device(query, views) is expected[0]
    elif not expected:
        with pytest.raises(DeviceNotFound) as exc:
            resolve_device(query, views)
        assert exc.value.query == query
    else:
        with pytest.raises(AmbiguousDevice) as exc:
            resolve_device(query, views)
        assert list(exc.value.candidates) == sorted(expected, key=sort_key)
