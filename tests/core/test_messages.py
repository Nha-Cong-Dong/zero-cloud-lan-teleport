"""Test `teleport.core.messages` theo đặc tả PR1 (kế hoạch §2.3, §2.5 và phụ lục điểm 1–5, 7, 18)."""

import dataclasses
import json
import unicodedata

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.core.framing import FramingError, decode_body, encode_message
from teleport.core.messages import (
    MAX_APP_VERSION_LEN,
    MAX_CERT_PEM_LEN,
    MAX_NAME_LEN,
    PROTOCOL_VERSION,
    AbortReason,
    Hello,
    MessageError,
    OsName,
    PairAbort,
    PairChallenge,
    PairConfirm,
    PairRequest,
    PairReveal,
    UnknownMessageType,
    parse_message,
    to_dict,
    validate_device_id,
    validate_device_name,
)

DEVICE_ID = "3f2a" + "0123456789abcdef" * 1 + "0" * 12
HEX64 = "0123456789abcdef" * 4
HEX64_B = "fedcba9876543210" * 4
CERT_HEADER = "-----BEGIN CERTIFICATE-----"
CERT = CERT_HEADER + "\nMIIBfake\n-----END CERTIFICATE-----\n"

assert len(DEVICE_ID) == 32


def valid_dicts() -> dict[str, dict]:
    """Một message hợp lệ (dạng dict trên dây) cho mỗi `type`."""
    return {
        "hello": {
            "type": "hello",
            "protocol": 1,
            "device_id": DEVICE_ID,
            "name": "PC-Nha",
            "app_version": "0.1.0",
            "os": "windows",
        },
        "pair_request": {"type": "pair_request", "cert": CERT, "commitment": HEX64},
        "pair_challenge": {"type": "pair_challenge", "nonce": HEX64},
        "pair_reveal": {"type": "pair_reveal", "nonce": HEX64_B},
        "pair_confirm": {"type": "pair_confirm"},
        "pair_abort": {"type": "pair_abort", "reason": "user"},
    }


VALID_MESSAGES = [
    Hello(protocol=1, device_id=DEVICE_ID, name="PC-Nha", app_version="0.1.0", os="windows"),
    Hello(protocol=7, device_id=DEVICE_ID, name="Máy của Minh 🚀", app_version="1.2.3-dev+abc", os="windows"),
    PairRequest(cert=CERT, commitment=HEX64),
    PairChallenge(nonce=HEX64),
    PairReveal(nonce=HEX64_B),
    PairConfirm(),
    PairAbort(reason="user"),
    PairAbort(reason="new_reason"),
]

EXPECTED_TYPE = {
    Hello: "hello",
    PairRequest: "pair_request",
    PairChallenge: "pair_challenge",
    PairReveal: "pair_reveal",
    PairConfirm: "pair_confirm",
    PairAbort: "pair_abort",
}

# Ký tự cấm trong name/app_version ngoài nhóm Cc/Cs (phụ lục điểm 2).
FORBIDDEN_EXTRA = {
    0x2028,
    0x2029,
    0x200B,
    0xFEFF,
    0x2060,
    0x200E,
    0x200F,
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
}


def reference_text_ok(value: object, max_len: int) -> bool:
    """Luật name/app_version viết lại độc lập từ đặc tả, dùng làm oracle cho property test."""
    if not isinstance(value, str):
        return False
    if not 1 <= len(value) <= max_len or value != value.strip():
        return False
    return all(unicodedata.category(c) not in ("Cc", "Cs") and ord(c) not in FORBIDDEN_EXTRA for c in value)


# --- Hằng số -----------------------------------------------------------------------------------


def test_constants_match_spec():
    assert PROTOCOL_VERSION == 1
    assert MAX_NAME_LEN == 64
    assert MAX_APP_VERSION_LEN == 32
    assert MAX_CERT_PEM_LEN == 8192


def test_exception_hierarchy():
    assert issubclass(MessageError, ValueError)
    assert issubclass(UnknownMessageType, MessageError)


def test_abort_reason_values():
    assert {r.value for r in AbortReason} == {"user", "busy", "timeout", "invalid"}
    assert AbortReason.USER == "user"


# --- Round-trip --------------------------------------------------------------------------------


@pytest.mark.parametrize("msg", VALID_MESSAGES, ids=lambda m: type(m).__name__)
def test_roundtrip_to_dict_then_parse(msg):
    d = to_dict(msg)
    assert d["type"] == EXPECTED_TYPE[type(msg)]
    assert parse_message(d) == msg


@pytest.mark.parametrize("msg", VALID_MESSAGES, ids=lambda m: type(m).__name__)
def test_roundtrip_through_framing(msg):
    frame = encode_message(to_dict(msg))
    assert parse_message(decode_body(frame[4:])) == msg


@pytest.mark.parametrize("type_name", list(valid_dicts()))
def test_wire_dict_parses_and_serializes_back_exactly(type_name):
    wire = valid_dicts()[type_name]
    msg = parse_message(wire)
    assert to_dict(msg) == wire


def test_parse_returns_expected_dataclass():
    assert parse_message(valid_dicts()["hello"]) == Hello(
        protocol=1, device_id=DEVICE_ID, name="PC-Nha", app_version="0.1.0", os="windows"
    )
    assert parse_message({"type": "pair_confirm"}) == PairConfirm()
    assert to_dict(PairConfirm()) == {"type": "pair_confirm"}


@pytest.mark.parametrize("type_name", list(valid_dicts()))
def test_unknown_fields_are_ignored(type_name):
    wire = valid_dicts()[type_name]
    noisy = {**wire, "extra": 1, "z": {"nested": [1, 2]}, "TYPE": "khác"}
    assert parse_message(noisy) == parse_message(wire)
    assert "extra" not in to_dict(parse_message(noisy))


def test_parse_does_not_mutate_input():
    wire = {**valid_dicts()["hello"], "extra": True}
    snapshot = dict(wire)
    parse_message(wire)
    assert wire == snapshot


def test_messages_are_frozen():
    msg = PairChallenge(nonce=HEX64)
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.nonce = HEX64_B  # type: ignore[misc]


# --- Khung message: không phải dict, thiếu type, type sai --------------------------------------


@pytest.mark.parametrize("obj", [None, [], ["hello"], "hello", 1, 1.5, True, b"{}", ({"type": "hello"},)])
def test_non_dict_raises_message_error_not_unknown_type(obj):
    with pytest.raises(MessageError) as exc:
        parse_message(obj)
    assert not isinstance(exc.value, UnknownMessageType)


@pytest.mark.parametrize(
    "obj",
    [
        {},
        {"protocol": 1},
        {"type": None},
        {"type": 1},
        {"type": True},
        {"type": ["hello"]},
        {"type": {"x": "hello"}},
        {"type": b"hello"},
    ],
)
def test_missing_or_non_string_type_is_message_error_not_unknown(obj):
    with pytest.raises(MessageError) as exc:
        parse_message(obj)
    assert not isinstance(exc.value, UnknownMessageType)


@pytest.mark.parametrize("type_name", ["foo", "new_message", "hello2", "pair_requests"])
def test_unknown_type_string_raises_unknown_message_type(type_name):
    with pytest.raises(UnknownMessageType):
        parse_message({"type": type_name})


@pytest.mark.parametrize("type_name", ["HELLO", "Hello", " hello", "hello ", ""])
def test_type_is_case_and_space_sensitive(type_name):
    with pytest.raises(MessageError):
        parse_message({**valid_dicts()["hello"], "type": type_name})


# --- Thiếu trường / sai kiểu -------------------------------------------------------------------


def _missing_field_cases():
    for type_name, wire in valid_dicts().items():
        for field in wire:
            if field != "type":
                yield pytest.param(type_name, field, id=f"{type_name}-{field}")


@pytest.mark.parametrize(("type_name", "field"), list(_missing_field_cases()))
def test_missing_field_raises(type_name, field):
    wire = valid_dicts()[type_name]
    del wire[field]
    with pytest.raises(MessageError):
        parse_message(wire)


@pytest.mark.parametrize(
    ("type_name", "field", "value"),
    [
        ("hello", "protocol", "1"),
        ("hello", "protocol", True),
        ("hello", "protocol", False),
        ("hello", "protocol", 0),
        ("hello", "protocol", -1),
        ("hello", "protocol", 1.0),
        ("hello", "protocol", 1.5),
        ("hello", "protocol", None),
        ("hello", "protocol", [1]),
        ("hello", "name", 5),
        ("hello", "name", None),
        ("hello", "name", ["PC"]),
        ("hello", "name", b"PC"),
        ("hello", "device_id", 123),
        ("hello", "device_id", None),
        ("hello", "device_id", DEVICE_ID.encode()),
        ("hello", "app_version", 1),
        ("hello", "app_version", None),
        ("hello", "os", None),
        ("hello", "os", 1),
        ("hello", "os", ["windows"]),
        ("pair_request", "cert", None),
        ("pair_request", "cert", CERT.encode()),
        ("pair_request", "cert", [CERT]),
        ("pair_request", "commitment", None),
        ("pair_request", "commitment", 0),
        ("pair_request", "commitment", bytes.fromhex(HEX64)),
        ("pair_challenge", "nonce", None),
        ("pair_challenge", "nonce", 12345),
        ("pair_challenge", "nonce", bytes.fromhex(HEX64)),
        ("pair_reveal", "nonce", None),
        ("pair_reveal", "nonce", [HEX64]),
        ("pair_abort", "reason", None),
        ("pair_abort", "reason", 1),
        ("pair_abort", "reason", ["user"]),
        ("pair_abort", "reason", True),
    ],
)
def test_wrong_field_type_or_value_raises(type_name, field, value):
    wire = {**valid_dicts()[type_name], field: value}
    with pytest.raises(MessageError):
        parse_message(wire)


@pytest.mark.parametrize("protocol", [1, 2, 99, 2**40])
def test_protocol_accepts_any_int_at_least_one(protocol):
    msg = parse_message({**valid_dicts()["hello"], "protocol": protocol})
    assert msg.protocol == protocol
    assert type(msg.protocol) is int


# --- device_id ---------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [DEVICE_ID, "0" * 32, "f" * 32, "0123456789abcdef" * 2])
def test_valid_device_id_returned_unchanged(value):
    assert validate_device_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        DEVICE_ID.upper(),
        "A" + DEVICE_ID[1:],
        DEVICE_ID[:31],
        DEVICE_ID + "0",
        "g" + DEVICE_ID[1:],
        "-" + DEVICE_ID[1:],
        "",
        DEVICE_ID[:31] + "\n",
        DEVICE_ID + "\n",
        " " + DEVICE_ID[1:],
        "０" * 32,  # chữ số toàn chiều rộng
        "٠" * 32,  # chữ số Ả Rập (\d khớp, [0-9] không)
        "teleport-" + DEVICE_ID,
        None,
        123,
        DEVICE_ID.encode(),
    ],
)
def test_invalid_device_id_rejected(value):
    with pytest.raises(MessageError):
        validate_device_id(value)


def test_invalid_device_id_in_hello_rejected():
    with pytest.raises(MessageError):
        parse_message({**valid_dicts()["hello"], "device_id": DEVICE_ID.upper()})


# --- name / app_version ------------------------------------------------------------------------

INVALID_NAMES = [
    "",
    " A",
    "A ",
    "\tA",
    "A\n",
    "\u00a0A",  # NBSP đầu
    "A\u3000",  # khoảng trắng ý tự cuối
    "x" * 65,
    "🚀" * 65,  # 65 code point
    "\x1b[31m",
    "a\x00b",
    "a\nb",
    "a\rb",
    "a\x7fb",
    "a\x85b",  # Cc C1
    "a\u202eb",
    "a\u202ab",
    "a\u202bb",
    "a\u202cb",
    "a\u202db",
    "a\u200eb",
    "a\u200fb",
    "a\u2066b",
    "a\u2067b",
    "a\u2068b",
    "a\u2069b",
    "a\u2028b",
    "a\u2029b",
    "a\u200bb",
    "a\ufeffb",
    "a\u2060b",
    "a\ud800b",  # surrogate lẻ (Cs)
    "\udfff",
    None,
    5,
    b"PC",
    ["PC"],
]

VALID_NAMES = [
    "A",
    "Máy của Minh",
    "x" * 64,
    "🚀",
    "🚀" * 64,  # 64 code point (128 đơn vị UTF-16)
    "👨\u200d👩\u200d👧",  # ZWJ (Cf) phải hợp lệ
    "PC Nhà  2",
    "名前のパソコン",
    "a\u00adb",  # soft hyphen (Cf) không bị cấm
    "Máy",  # NFD giữ nguyên
]


@pytest.mark.parametrize("value", INVALID_NAMES, ids=repr)
def test_invalid_name_rejected(value):
    with pytest.raises(MessageError):
        validate_device_name(value)


@pytest.mark.parametrize("value", INVALID_NAMES, ids=repr)
def test_invalid_name_rejected_in_hello(value):
    with pytest.raises(MessageError):
        parse_message({**valid_dicts()["hello"], "name": value})


@pytest.mark.parametrize("value", VALID_NAMES, ids=repr)
def test_valid_name_returned_unchanged(value):
    result = validate_device_name(value)
    assert result == value
    assert len(result) == len(value)  # không chuẩn hóa NFC


@pytest.mark.parametrize("value", VALID_NAMES, ids=repr)
def test_valid_name_roundtrips_in_hello(value):
    msg = Hello(protocol=1, device_id=DEVICE_ID, name=value, app_version="1", os="windows")
    assert parse_message(to_dict(msg)).name == value


def test_name_is_not_nfc_normalized():
    nfd = unicodedata.normalize("NFD", "Máy của Minh")
    assert nfd != unicodedata.normalize("NFC", nfd)
    assert validate_device_name(nfd) == nfd
    assert parse_message({**valid_dicts()["hello"], "name": nfd}).name == nfd


@pytest.mark.parametrize("value", ["1", "0.1.0", "v" * 32, "1.0-bản thử", "🚀" * 32])
def test_valid_app_version(value):
    assert parse_message({**valid_dicts()["hello"], "app_version": value}).app_version == value


@pytest.mark.parametrize(
    "value",
    ["", " 1.0", "1.0 ", "v" * 33, "🚀" * 33, "1.0\x00", "1.0\u202e", "1\u2028", "\ufeff1.0", "1\ud800"],
    ids=repr,
)
def test_invalid_app_version(value):
    with pytest.raises(MessageError):
        parse_message({**valid_dicts()["hello"], "app_version": value})


# --- cert --------------------------------------------------------------------------------------


def _cert_of_length(n: int) -> str:
    return CERT_HEADER + "A" * (n - len(CERT_HEADER))


def test_cert_at_limit_is_accepted():
    cert = _cert_of_length(MAX_CERT_PEM_LEN)
    assert len(cert) == 8192
    assert parse_message({"type": "pair_request", "cert": cert, "commitment": HEX64}).cert == cert


@pytest.mark.parametrize(
    "cert",
    [
        _cert_of_length(MAX_CERT_PEM_LEN + 1),
        _cert_of_length(100_000),
        "",
        "MIIB...",
        " " + CERT,
        "\n" + CERT,
        "\ufeff" + CERT,
        "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
        "-----begin certificate-----\nAAAA\n",
        "-----BEGIN CERTIFICATE----",
    ],
    ids=lambda c: repr(c[:30]) + f"…len={len(c)}",
)
def test_invalid_cert_rejected(cert):
    with pytest.raises(MessageError):
        parse_message({"type": "pair_request", "cert": cert, "commitment": HEX64})


def test_cert_only_checks_shape_not_content():
    # Phụ lục điểm 7: tầng message chỉ kiểm dạng, không phân tích chứng chỉ.
    cert = CERT_HEADER + "đây không phải base64"
    assert PairRequest(cert=cert, commitment=HEX64).cert == cert


# --- nonce / commitment ------------------------------------------------------------------------

BAD_HEX64 = [
    "",
    HEX64[:63],
    HEX64 + "0",
    HEX64.upper(),
    "A" + HEX64[1:],
    "g" + HEX64[1:],
    HEX64[:63] + "\n",
    HEX64 + "\n",
    " " + HEX64[1:],
    HEX64[:63] + " ",
    "０" * 64,
    "٠" * 64,
    "0x" + HEX64[2:],
]


@pytest.mark.parametrize("value", BAD_HEX64, ids=repr)
@pytest.mark.parametrize(("type_name", "field"), [("pair_challenge", "nonce"), ("pair_reveal", "nonce")])
def test_bad_nonce_rejected(type_name, field, value):
    with pytest.raises(MessageError):
        parse_message({"type": type_name, field: value})


@pytest.mark.parametrize("value", BAD_HEX64, ids=repr)
def test_bad_commitment_rejected(value):
    with pytest.raises(MessageError):
        parse_message({"type": "pair_request", "cert": CERT, "commitment": value})


@pytest.mark.parametrize("value", ["0" * 64, "f" * 64, HEX64, HEX64_B])
def test_good_hex64_accepted(value):
    assert parse_message({"type": "pair_challenge", "nonce": value}).nonce == value
    assert parse_message({"type": "pair_reveal", "nonce": value}).nonce == value
    assert parse_message({"type": "pair_request", "cert": CERT, "commitment": value}).commitment == value


# --- reason ------------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["user", "busy", "timeout", "invalid", "new_reason", "a", "a" * 32, "_"])
def test_valid_reason(reason):
    msg = parse_message({"type": "pair_abort", "reason": reason})
    assert msg.reason == reason
    assert type(msg.reason) is str  # phụ lục điểm 4: str thuần, không phải AbortReason


def test_parsed_standard_reason_compares_equal_to_enum():
    msg = parse_message({"type": "pair_abort", "reason": "busy"})
    assert msg.reason == AbortReason.BUSY
    assert type(msg.reason) is str


@pytest.mark.parametrize(
    "reason",
    [
        "",
        "USER",
        "User",
        "a" * 33,
        "user\n",
        "user ",
        " user",
        "user-x",
        "user1",
        "ü",
        "người_dùng",
        "user\x00",
    ],
    ids=repr,
)
def test_invalid_reason(reason):
    with pytest.raises(MessageError):
        parse_message({"type": "pair_abort", "reason": reason})


@pytest.mark.parametrize("value", ["windows", "macos", "linux", "freebsd", "x", "a" * 16, "os_2"])
def test_valid_os(value):
    assert parse_message({**valid_dicts()["hello"], "os": value}).os == value


@pytest.mark.parametrize(
    "value",
    ["", "Windows", "a" * 17, "mac os", "linux\n", "\uff57\uff49\uff4e", "win-11", "macos\u200b"],
    ids=repr,
)
def test_invalid_os(value):
    with pytest.raises(MessageError):
        parse_message({**valid_dicts()["hello"], "os": value})


def test_os_enum_becomes_plain_str():
    msg = Hello(protocol=1, device_id=DEVICE_ID, name="PC", app_version="1", os=OsName.MACOS)
    assert type(msg.os) is str
    assert to_dict(msg)["os"] == "macos"


# --- Tạo tay dataclass với giá trị sai ---------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Hello(protocol=0, device_id=DEVICE_ID, name="PC", app_version="1", os="windows"),
        lambda: Hello(protocol=True, device_id=DEVICE_ID, name="PC", app_version="1", os="windows"),
        lambda: Hello(protocol="1", device_id=DEVICE_ID, name="PC", app_version="1", os="windows"),
        lambda: Hello(protocol=1.0, device_id=DEVICE_ID, name="PC", app_version="1", os="windows"),
        lambda: Hello(protocol=1, device_id=DEVICE_ID.upper(), name="PC", app_version="1", os="windows"),
        lambda: Hello(protocol=1, device_id=DEVICE_ID, name=" PC", app_version="1", os="windows"),
        lambda: Hello(protocol=1, device_id=DEVICE_ID, name="x" * 65, app_version="1", os="windows"),
        lambda: Hello(protocol=1, device_id=DEVICE_ID, name="PC", app_version="", os="windows"),
        lambda: Hello(protocol=1, device_id=DEVICE_ID, name="PC", app_version="v" * 33, os="windows"),
        lambda: Hello(protocol=1, device_id=DEVICE_ID, name="PC", app_version="1", os="Windows"),
        lambda: PairRequest(cert="không phải PEM", commitment=HEX64),
        lambda: PairRequest(cert=_cert_of_length(8193), commitment=HEX64),
        lambda: PairRequest(cert=CERT, commitment=HEX64.upper()),
        lambda: PairChallenge(nonce="x"),
        lambda: PairChallenge(nonce=bytes(32)),
        lambda: PairReveal(nonce=HEX64[:62]),
        lambda: PairAbort(reason="USER"),
        lambda: PairAbort(reason=""),
        lambda: PairAbort(reason=None),
    ],
)
def test_constructing_dataclass_with_bad_value_raises(factory):
    with pytest.raises(MessageError):
        factory()


# --- Fuzz bằng hypothesis (phụ lục điểm 18) ----------------------------------------------------

json_scalars = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=True, allow_infinity=True)
    | st.text(max_size=80)
)
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=10), children, max_size=5)
    ),
    max_leaves=25,
)

TRICKY_CHARS = [
    "\x00",
    "\x1b",
    "\x7f",
    "\x85",
    "\u00a0",
    "\u00ad",
    "\u200b",
    "\u200d",
    "\u200e",
    "\u200f",
    "\u2028",
    "\u2029",
    "\u202a",
    "\u202e",
    "\u2060",
    "\u2066",
    "\u2069",
    "\u3000",
    "\ufeff",
    "\ud800",
    "\udfff",
    "０",
    "٠",
    "é",
    "🚀",
    " ",
    "\n",
]
tricky_text = st.lists(
    st.sampled_from(TRICKY_CHARS)
    | st.characters(codec="utf-8")
    | st.sampled_from("0123456789abcdefABCDEF_z"),
    max_size=70,
).map("".join)

field_values = (
    st.just(DEVICE_ID)
    | st.just(HEX64)
    | st.just(CERT)
    | st.sampled_from(["user", "busy", "PC-Nha", "0.1.0"])
    | st.integers(-3, 3)
    | tricky_text
    | json_values
)
near_valid_messages = st.builds(
    lambda type_name, overrides: {**valid_dicts()[type_name], **overrides},
    st.sampled_from(list(valid_dicts())),
    st.dictionaries(
        st.sampled_from(
            ["protocol", "device_id", "name", "app_version", "cert", "commitment", "nonce", "reason", "x"]
        ),
        field_values,
        max_size=3,
    ),
)
any_message_input = json_values | near_valid_messages | st.builds(lambda t: {"type": t}, json_values)

MESSAGE_CLASSES = (Hello, PairRequest, PairChallenge, PairReveal, PairConfirm, PairAbort)


@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(any_message_input)
def test_fuzz_parse_message_only_returns_message_or_message_error(obj):
    try:
        msg = parse_message(obj)
    except MessageError:
        return
    assert isinstance(msg, MESSAGE_CLASSES)
    # Message đã qua kiểm tra thì phải tuần tự hóa lại được và round-trip.
    d = to_dict(msg)
    assert d["type"] == EXPECTED_TYPE[type(msg)]
    assert parse_message(d) == msg
    encode_message(d)


@settings(max_examples=300, deadline=None)
@given(
    st.binary(max_size=300)
    | json_values.map(lambda v: json.dumps(v).encode())
    | near_valid_messages.map(lambda d: json.dumps(d, ensure_ascii=False).encode("utf-8", "surrogatepass"))
)
def test_fuzz_decode_body_then_parse_only_raises_protocol_errors(body):
    try:
        msg = parse_message(decode_body(body))
    except (FramingError, MessageError):
        return
    assert isinstance(msg, MESSAGE_CLASSES)


@settings(max_examples=500, deadline=None)
@given(tricky_text | st.text(max_size=80) | json_scalars)
def test_property_name_validation_matches_reference_rules(value):
    expected_ok = reference_text_ok(value, MAX_NAME_LEN)
    try:
        result = validate_device_name(value)
    except MessageError:
        assert not expected_ok, f"tên hợp lệ theo đặc tả bị từ chối: {value!r}"
        return
    assert expected_ok, f"tên sai đặc tả lại được chấp nhận: {value!r}"
    assert result == value


@settings(max_examples=300, deadline=None)
@given(tricky_text | st.text(max_size=40))
def test_property_app_version_validation_matches_reference_rules(value):
    expected_ok = reference_text_ok(value, MAX_APP_VERSION_LEN)
    wire = {**valid_dicts()["hello"], "app_version": value}
    try:
        msg = parse_message(wire)
    except MessageError:
        assert not expected_ok, f"app_version hợp lệ bị từ chối: {value!r}"
        return
    assert expected_ok, f"app_version sai đặc tả lại được chấp nhận: {value!r}"
    assert msg.app_version == value


@settings(max_examples=300, deadline=None)
@given(tricky_text | st.text(alphabet="0123456789abcdefABCDEF\n ", max_size=66))
def test_property_device_id_is_exactly_32_lowercase_hex(value):
    expected_ok = len(value) == 32 and all(c in "0123456789abcdef" for c in value)
    try:
        assert validate_device_id(value) == value
    except MessageError:
        assert not expected_ok
        return
    assert expected_ok
