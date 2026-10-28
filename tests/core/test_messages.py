"""Test `teleport.core.messages` theo đặc tả PR1 (kế hoạch §2.3, §2.5 và phụ lục điểm 1–5, 7, 18).

PR2 thêm `control_open`, `ping`, `pong` (docs/protocol.md §3.2–3.3, kế hoạch PR2 mục Test).
PR-2 (kết nối) thêm bảng loại kết nối: `ConnectionKind`, `allowed_types`, `opening_kind`,
`check_message_kind` (đặc tả PR-2 mục 2.1, test M1–M5 ở cuối file).
"""

import dataclasses
import enum
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
    MAX_SAFE_INTEGER,
    PROTOCOL_VERSION,
    AbortReason,
    ConnectionKind,
    ControlOpen,
    Hello,
    MessageError,
    OsName,
    PairAbort,
    PairCommit,
    PairConfirm,
    PairNonce,
    PairRequest,
    PairReveal,
    Ping,
    Pong,
    UnknownMessageType,
    WrongConnectionKind,
    allowed_types,
    check_message_kind,
    opening_kind,
    parse_message,
    to_dict,
    validate_device_id,
    validate_device_name,
    validate_os,
)

DEVICE_ID = "3f2a" + "0123456789abcdef" * 1 + "0" * 12
HEX64 = "0123456789abcdef" * 4
HEX64_B = "fedcba9876543210" * 4
SIG128 = "0123456789abcdef" * 8  # chữ ký r ‖ s: 64 byte = 128 ký tự hex thường
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
        "pair_request": {"type": "pair_request", "cert": CERT},
        "pair_commit": {"type": "pair_commit", "commitment": HEX64},
        "pair_nonce": {"type": "pair_nonce", "nonce": HEX64_B, "signature": SIG128},
        "pair_reveal": {"type": "pair_reveal", "nonce": HEX64},
        "pair_confirm": {"type": "pair_confirm"},
        "pair_abort": {"type": "pair_abort", "reason": "user"},
        "control_open": {"type": "control_open"},
        "ping": {"type": "ping", "t": 123456},
        "pong": {"type": "pong", "t": 123456},
    }


VALID_MESSAGES = [
    Hello(protocol=1, device_id=DEVICE_ID, name="PC-Nha", app_version="0.1.0", os="windows"),
    Hello(protocol=7, device_id=DEVICE_ID, name="Máy của Minh 🚀", app_version="1.2.3-dev+abc", os="windows"),
    PairRequest(cert=CERT),
    PairCommit(commitment=HEX64),
    PairNonce(nonce=HEX64_B, signature=SIG128),
    PairReveal(nonce=HEX64),
    PairConfirm(),
    PairAbort(reason="user"),
    PairAbort(reason="new_reason"),
    ControlOpen(),
    Ping(t=0),
    Ping(t=2**53 - 1),
    Pong(t=5),
]

EXPECTED_TYPE = {
    Hello: "hello",
    PairRequest: "pair_request",
    PairCommit: "pair_commit",
    PairNonce: "pair_nonce",
    PairReveal: "pair_reveal",
    PairConfirm: "pair_confirm",
    PairAbort: "pair_abort",
    ControlOpen: "control_open",
    Ping: "ping",
    Pong: "pong",
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


@pytest.mark.parametrize(
    ("msg", "field"),
    [
        (PairCommit(commitment=HEX64), "commitment"),
        (PairNonce(nonce=HEX64, signature=SIG128), "nonce"),
        (PairNonce(nonce=HEX64, signature=SIG128), "signature"),
        (PairReveal(nonce=HEX64), "nonce"),
        (PairRequest(cert=CERT), "cert"),
    ],
    ids=["PairCommit", "PairNonce", "PairNonce-signature", "PairReveal", "PairRequest"],
)
def test_messages_are_frozen(msg, field):
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(msg, field, HEX64_B)


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


# `pair_challenge` thuộc luồng ghép đôi cũ (bên khởi xướng cam kết trước), nay đã bỏ.
@pytest.mark.parametrize(
    "type_name", ["foo", "new_message", "hello2", "pair_requests", "pair_challenge", "pair_commitment"]
)
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
        ("pair_commit", "commitment", None),
        ("pair_commit", "commitment", 0),
        ("pair_commit", "commitment", bytes.fromhex(HEX64)),
        ("pair_commit", "commitment", [HEX64]),
        ("pair_nonce", "nonce", None),
        ("pair_nonce", "nonce", 12345),
        ("pair_nonce", "nonce", bytes.fromhex(HEX64)),
        ("pair_nonce", "nonce", True),
        ("pair_reveal", "nonce", None),
        ("pair_reveal", "nonce", [HEX64]),
        ("pair_reveal", "nonce", bytes.fromhex(HEX64)),
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
    assert parse_message({"type": "pair_request", "cert": cert}).cert == cert


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
        parse_message({"type": "pair_request", "cert": cert})


def test_cert_only_checks_shape_not_content():
    # Phụ lục điểm 7: tầng message chỉ kiểm dạng, không phân tích chứng chỉ.
    cert = CERT_HEADER + "đây không phải base64"
    assert PairRequest(cert=cert).cert == cert


# --- pair_request chỉ còn cert (bên trả lời cam kết trước, docs/protocol.md §5) ----------------


def test_pair_request_has_only_cert_field():
    assert [f.name for f in dataclasses.fields(PairRequest)] == ["cert"]


def test_pair_request_to_dict_has_no_commitment():
    assert to_dict(PairRequest(cert=CERT)) == {"type": "pair_request", "cert": CERT}


@pytest.mark.parametrize("value", [HEX64, "không phải hex", None, 0, HEX64.upper()], ids=repr)
def test_pair_request_ignores_legacy_commitment_field(value):
    # `commitment` của luồng cũ giờ là trường lạ: bị bỏ qua, kể cả khi giá trị sai dạng.
    msg = parse_message({"type": "pair_request", "cert": CERT, "commitment": value})
    assert msg == PairRequest(cert=CERT)
    assert "commitment" not in to_dict(msg)


def test_pair_challenge_is_gone():
    import teleport.core.messages as messages_mod

    assert not hasattr(messages_mod, "PairChallenge")
    with pytest.raises(UnknownMessageType):
        parse_message({"type": "pair_challenge", "nonce": HEX64})


def test_pair_request_constructor_rejects_commitment_argument():
    with pytest.raises(TypeError):
        PairRequest(cert=CERT, commitment=HEX64)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("cls", "fields"),
    [(PairCommit, ["commitment"]), (PairNonce, ["nonce", "signature"]), (PairReveal, ["nonce"])],
)
def test_new_pairing_messages_have_exactly_their_hex_fields(cls, fields):
    # pair_nonce mang thêm chữ ký của bên khởi xướng (protocol 3.2, 5.2).
    assert [f.name for f in dataclasses.fields(cls)] == fields


def test_pair_commit_nonce_reveal_wire_format():
    assert to_dict(PairCommit(commitment=HEX64)) == {"type": "pair_commit", "commitment": HEX64}
    assert to_dict(PairNonce(nonce=HEX64, signature=SIG128)) == {
        "type": "pair_nonce",
        "nonce": HEX64,
        "signature": SIG128,
    }
    assert to_dict(PairReveal(nonce=HEX64)) == {"type": "pair_reveal", "nonce": HEX64}
    assert parse_message({"type": "pair_commit", "commitment": HEX64}) == PairCommit(commitment=HEX64)
    assert parse_message({"type": "pair_nonce", "nonce": HEX64, "signature": SIG128}) == PairNonce(
        nonce=HEX64, signature=SIG128
    )


def test_pairing_messages_use_their_own_field_names():
    # `pair_commit` mang `commitment`; `pair_nonce`/`pair_reveal` mang `nonce`. Tên khác → thiếu trường.
    for wire in (
        {"type": "pair_commit", "nonce": HEX64},
        {"type": "pair_nonce", "commitment": HEX64, "signature": SIG128},
        {"type": "pair_reveal", "commitment": HEX64},
    ):
        with pytest.raises(MessageError):
            parse_message(wire)


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
@pytest.mark.parametrize(
    ("type_name", "field"),
    [("pair_commit", "commitment"), ("pair_nonce", "nonce"), ("pair_reveal", "nonce")],
)
def test_bad_hex64_field_rejected(type_name, field, value):
    # Các trường khác đúng (pair_nonce có chữ ký hợp lệ): lỗi chỉ do trường đang thử.
    with pytest.raises(MessageError) as exc:
        parse_message({**valid_dicts()[type_name], field: value})
    assert not isinstance(exc.value, UnknownMessageType)


@pytest.mark.parametrize("value", BAD_HEX64, ids=repr)
@pytest.mark.parametrize(
    ("cls", "field"), [(PairCommit, "commitment"), (PairNonce, "nonce"), (PairReveal, "nonce")]
)
def test_bad_hex64_rejected_when_constructing(cls, field, value):
    extra = {"signature": SIG128} if cls is PairNonce else {}
    with pytest.raises(MessageError):
        cls(**{field: value, **extra})


@pytest.mark.parametrize("value", ["0" * 64, "f" * 64, HEX64, HEX64_B])
def test_good_hex64_accepted(value):
    assert parse_message({"type": "pair_commit", "commitment": value}).commitment == value
    assert parse_message({"type": "pair_nonce", "nonce": value, "signature": SIG128}).nonce == value
    assert parse_message({"type": "pair_reveal", "nonce": value}).nonce == value


# --- pair_nonce.signature (protocol 3.3: đúng 128 ký tự [0-9a-f], re.fullmatch ASCII) ----------

BAD_SIG128 = [
    "",
    SIG128[:126],
    SIG128[:127],
    SIG128 + "0",
    SIG128 + "00",
    SIG128[:64],  # 64 ký tự: độ dài của nonce, không phải của chữ ký
    SIG128.upper(),
    "A" + SIG128[1:],
    "g" + SIG128[1:],
    SIG128[:127] + "\n",
    SIG128 + "\n",
    " " + SIG128[1:],
    SIG128[:127] + " ",
    "０" * 128,  # chữ số toàn chiều rộng
    "٠" * 128,  # chữ số Ả Rập-Ấn
    "0x" + SIG128[2:],
    ":".join([SIG128[i : i + 2] for i in range(0, 128, 2)])[:128],
]
NON_STRING_SIG = [None, 0, 12345, True, bytes(64), bytes.fromhex(SIG128), [SIG128], {"r": 1}, 1.5]


@pytest.mark.parametrize("value", BAD_SIG128, ids=repr)
def test_pair_nonce_bad_signature_rejected_on_parse(value):
    with pytest.raises(MessageError) as exc:
        parse_message({**valid_dicts()["pair_nonce"], "signature": value})
    assert not isinstance(exc.value, UnknownMessageType)


@pytest.mark.parametrize("value", BAD_SIG128, ids=repr)
def test_pair_nonce_bad_signature_rejected_when_constructing(value):
    with pytest.raises(MessageError):
        PairNonce(nonce=HEX64, signature=value)


@pytest.mark.parametrize("value", NON_STRING_SIG, ids=repr)
def test_pair_nonce_non_string_signature_rejected(value):
    with pytest.raises(MessageError):
        parse_message({**valid_dicts()["pair_nonce"], "signature": value})
    with pytest.raises(MessageError):
        PairNonce(nonce=HEX64, signature=value)


def test_pair_nonce_missing_signature_rejected():
    wire = valid_dicts()["pair_nonce"]
    del wire["signature"]
    with pytest.raises(MessageError) as exc:
        parse_message(wire)
    assert not isinstance(exc.value, UnknownMessageType)


def test_pair_nonce_constructor_requires_signature():
    with pytest.raises(TypeError):
        PairNonce(nonce=HEX64)  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["0" * 128, "f" * 128, SIG128, "ab" * 64, HEX64 + HEX64_B])
def test_pair_nonce_good_signature_accepted_and_roundtrips(value):
    msg = parse_message({"type": "pair_nonce", "nonce": HEX64, "signature": value})
    assert msg == PairNonce(nonce=HEX64, signature=value)
    assert msg.signature == value
    assert type(msg.signature) is str
    assert to_dict(msg) == {"type": "pair_nonce", "nonce": HEX64, "signature": value}
    assert parse_message(to_dict(msg)) == msg


def test_pair_nonce_unknown_fields_still_ignored():
    # protocol 3.1: trường lạ bỏ qua; không lẫn với `signature`.
    wire = {"type": "pair_nonce", "nonce": HEX64, "signature": SIG128, "sig": "x", "proof": 1}
    msg = parse_message(wire)
    assert msg == PairNonce(nonce=HEX64, signature=SIG128)
    assert set(to_dict(msg)) == {"type", "nonce", "signature"}


def test_pair_nonce_signature_and_nonce_are_not_interchangeable():
    # Đổi chỗ hai trường: nonce 128 ký tự và chữ ký 64 ký tự đều sai.
    with pytest.raises(MessageError):
        parse_message({"type": "pair_nonce", "nonce": SIG128, "signature": HEX64})


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=140))
def test_property_signature_accepts_only_128_lowercase_hex(value):
    import re as _re

    good = _re.fullmatch(r"[0-9a-f]{128}", value, flags=_re.ASCII) is not None
    try:
        msg = parse_message({"type": "pair_nonce", "nonce": HEX64, "signature": value})
    except MessageError:
        assert not good, f"chữ ký hợp lệ bị từ chối: {value!r}"
        return
    assert good, f"chữ ký sai dạng được nhận: {value!r}"
    assert parse_message(to_dict(msg)) == msg


@settings(max_examples=200, deadline=None)
@given(st.binary(min_size=64, max_size=64), st.binary(min_size=32, max_size=32))
def test_property_pair_nonce_roundtrip(sig, nonce):
    msg = PairNonce(nonce=nonce.hex(), signature=sig.hex())
    assert parse_message(to_dict(msg)) == msg
    assert parse_message(decode_body(encode_message(to_dict(msg))[4:])) == msg


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


# --- validate_os (PR3 mục 2.1: cùng quy tắc với `hello.os`, dùng lại cho TXT `os`) ---------------

VALID_OS = ["windows", "macos", "linux", "freebsd", "x", "a" * 16, "os_2", "0", "_", "win11"]
INVALID_OS = [
    "",
    "Windows",
    "WINDOWS",
    "a" * 17,
    "mac os",
    "linux\n",
    "\nlinux",
    " linux",
    "ｗｉｎ",  # "win" toàn chiều rộng
    "win-11",
    "win.11",
    "macos​",
    "linuх",  # chữ x Kirin
    "٣",  # chữ số Ả Rập: `\d` nhận, ASCII thì không
    None,
    1,
    b"windows",
    ["windows"],
]


@pytest.mark.parametrize("value", VALID_OS)
def test_validate_os_returns_valid_value_unchanged(value):
    result = validate_os(value)
    assert result == value
    assert isinstance(result, str)


@pytest.mark.parametrize("value", INVALID_OS, ids=repr)
def test_validate_os_rejects_invalid_value(value):
    with pytest.raises(MessageError):
        validate_os(value)


def test_validate_os_accepts_os_name_enum():
    assert validate_os(OsName.LINUX) == "linux"


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=20) | st.text(alphabet="abcxyz019_ABC-\n ", max_size=18))
def test_property_validate_os_agrees_with_hello_os(value):
    expected_ok = 1 <= len(value) <= 16 and all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in value)
    try:
        assert validate_os(value) == value
    except MessageError:
        assert not expected_ok, f"os hợp lệ bị từ chối: {value!r}"
    else:
        assert expected_ok, f"os sai đặc tả lại được chấp nhận: {value!r}"
    # Hello dùng cùng quy tắc: kết quả nhận/từ chối phải trùng.
    try:
        parse_message({**valid_dicts()["hello"], "os": value})
    except MessageError:
        assert not expected_ok
    else:
        assert expected_ok


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
        lambda: PairRequest(cert="không phải PEM"),
        lambda: PairRequest(cert=_cert_of_length(8193)),
        lambda: PairCommit(commitment=HEX64.upper()),
        lambda: PairCommit(commitment=bytes(32)),
        lambda: PairNonce(nonce="x", signature=SIG128),
        lambda: PairNonce(nonce=bytes(32), signature=SIG128),
        lambda: PairNonce(nonce=HEX64, signature=SIG128.upper()),
        lambda: PairNonce(nonce=HEX64, signature=bytes(64)),
        lambda: PairReveal(nonce=HEX64[:62]),
        lambda: PairAbort(reason="USER"),
        lambda: PairAbort(reason=""),
        lambda: PairAbort(reason=None),
    ],
)
def test_constructing_dataclass_with_bad_value_raises(factory):
    with pytest.raises(MessageError):
        factory()


# --- control_open / ping / pong (PR2, protocol.md §3.2–3.3) -----------------------------------

TIMED = [Ping, Pong]
timed_classes = pytest.mark.parametrize("cls", TIMED, ids=lambda c: EXPECTED_TYPE[c])

BAD_T = [
    -1,
    -(2**53),
    True,
    False,
    1.0,
    0.0,
    -0.0,
    1e3,
    float("nan"),
    float("inf"),
    "1",
    "0",
    "",
    None,
    [],
    [1],
    {},
    {"t": 1},
    b"1",
    2**53,
    2**53 + 1,
    10**100,
]


def test_max_safe_integer_is_2_pow_53_minus_1():
    assert MAX_SAFE_INTEGER == 2**53 - 1
    assert type(MAX_SAFE_INTEGER) is int


@timed_classes
@pytest.mark.parametrize("value", BAD_T, ids=repr)
def test_bad_t_in_wire_dict_raises_message_error(cls, value):
    with pytest.raises(MessageError) as exc:
        parse_message({"type": EXPECTED_TYPE[cls], "t": value})
    # type đã biết: lỗi là message hỏng, không phải type lạ.
    assert not isinstance(exc.value, UnknownMessageType)


@timed_classes
@pytest.mark.parametrize("value", [0, 1, 123456, 2**53 - 1])
def test_valid_t_roundtrips(cls, value):
    msg = parse_message({"type": EXPECTED_TYPE[cls], "t": value})
    assert msg == cls(t=value)
    assert msg.t == value
    assert type(msg.t) is int
    assert to_dict(msg) == {"type": EXPECTED_TYPE[cls], "t": value}
    frame = encode_message(to_dict(msg))
    assert parse_message(decode_body(frame[4:])) == msg


@timed_classes
def test_json_negative_zero_is_valid_t_zero(cls):
    # JSON `-0` là số nguyên 0, khác `-0.0` (số thực, bị từ chối).
    msg = parse_message(decode_body(f'{{"type":"{EXPECTED_TYPE[cls]}","t":-0}}'.encode()))
    assert msg == cls(t=0)
    assert type(msg.t) is int


@timed_classes
@pytest.mark.parametrize(
    "literal", ["1e3", "1.0", "0.0", "-0.0", "1E0", "true", "null", '"5"', "9007199254740992"]
)
def test_t_non_integer_or_out_of_range_from_json_raises(cls, literal):
    body = f'{{"type":"{EXPECTED_TYPE[cls]}","t":{literal}}}'.encode()
    with pytest.raises(MessageError):
        parse_message(decode_body(body))


@timed_classes
def test_missing_t_raises(cls):
    with pytest.raises(MessageError) as exc:
        parse_message({"type": EXPECTED_TYPE[cls]})
    assert not isinstance(exc.value, UnknownMessageType)


@timed_classes
def test_unknown_fields_ignored_next_to_t(cls):
    assert parse_message({"type": EXPECTED_TYPE[cls], "t": 7, "x": 1, "T": "khác"}) == cls(t=7)


def test_control_open_ignores_unknown_fields():
    assert parse_message({"type": "control_open", "x": 1}) == ControlOpen()
    assert parse_message({"type": "control_open", "t": "không phải số"}) == ControlOpen()


def test_to_dict_of_new_messages():
    assert to_dict(ControlOpen()) == {"type": "control_open"}
    assert to_dict(Ping(t=7)) == {"type": "ping", "t": 7}
    assert to_dict(Pong(t=9)) == {"type": "pong", "t": 9}


def test_ping_and_pong_with_same_t_are_different_messages():
    assert Ping(t=5) != Pong(t=5)
    assert to_dict(Ping(t=5)) != to_dict(Pong(t=5))
    assert parse_message({"type": "pong", "t": 5}) == Pong(t=5)
    assert type(parse_message({"type": "ping", "t": 5})) is Ping


def test_control_open_instances_are_equal():
    assert ControlOpen() == ControlOpen()
    assert ControlOpen() != PairConfirm()


@timed_classes
def test_timed_messages_are_frozen(cls):
    msg = cls(t=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.t = 2  # type: ignore[misc]


class _IntSubclass(enum.IntEnum):
    ONE = 1


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Ping(t=-1),
        lambda: Ping(t=True),
        lambda: Ping(t=False),
        lambda: Ping(t=2**53),
        lambda: Ping(t="1"),
        lambda: Ping(t=None),
        lambda: Pong(t=1.0),
        lambda: Pong(t=2**53),
        lambda: Pong(t=-1),
        lambda: Pong(t=True),
        lambda: Ping(t=_IntSubclass.ONE),  # lớp con của int ngoài bool cũng bị từ chối
    ],
)
def test_constructing_ping_pong_with_bad_t_raises(factory):
    with pytest.raises(MessageError):
        factory()


@settings(max_examples=300, deadline=None)
@given(
    st.integers(min_value=-(2**60), max_value=2**60)
    | st.floats(allow_nan=True, allow_infinity=True)
    | st.booleans()
    | st.none()
    | st.text(max_size=5)
)
def test_property_t_accepted_iff_int_in_safe_range(value):
    expected_ok = type(value) is int and 0 <= value <= MAX_SAFE_INTEGER
    for cls in TIMED:
        try:
            msg = parse_message({"type": EXPECTED_TYPE[cls], "t": value})
        except MessageError:
            assert not expected_ok, f"t hợp lệ bị từ chối: {value!r}"
            continue
        assert expected_ok, f"t sai đặc tả lại được chấp nhận: {value!r}"
        assert msg == cls(t=value)


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
    | st.just(SIG128)
    | st.just(CERT)
    | st.sampled_from(["user", "busy", "PC-Nha", "0.1.0"])
    | st.integers(-3, 3)
    | st.sampled_from([MAX_SAFE_INTEGER, MAX_SAFE_INTEGER + 1, 1.0, 1e3, -0.0])
    | tricky_text
    | json_values
)
near_valid_messages = st.builds(
    lambda type_name, overrides: {**valid_dicts()[type_name], **overrides},
    st.sampled_from(list(valid_dicts())),
    st.dictionaries(
        st.sampled_from(
            [
                "protocol",
                "device_id",
                "name",
                "app_version",
                "cert",
                "commitment",
                "nonce",
                "signature",
                "reason",
                "t",
                "x",
            ]
        ),
        field_values,
        max_size=3,
    ),
)
any_message_input = json_values | near_valid_messages | st.builds(lambda t: {"type": t}, json_values)

MESSAGE_CLASSES = (
    Hello,
    PairRequest,
    PairCommit,
    PairNonce,
    PairReveal,
    PairConfirm,
    PairAbort,
    ControlOpen,
    Ping,
    Pong,
)


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


# --- Bảng loại kết nối (đặc tả PR-2 mục 2.1, test M1–M5) ------------------------------------------

PAIRING_TYPES = frozenset(
    {"pair_request", "pair_commit", "pair_nonce", "pair_reveal", "pair_confirm", "pair_abort"}
)
# GĐ2 P2-1 thêm message truyền file vào kết nối điều khiển (protocol §3.2; test riêng ở
# tests/core/test_transfer_messages.py). GĐ5 P5-0 thêm `clipboard` (test riêng ở
# tests/core/test_clipboard_messages.py).
CONTROL_TYPES = frozenset(
    {"ping", "pong", "offer", "accept", "reject", "resume", "complete", "verified", "cancel", "clipboard"}
)
EXPECTED_ALLOWED = {"pairing": PAIRING_TYPES, "control": CONTROL_TYPES, "data": frozenset()}
KIND_INPUTS = [*ConnectionKind, "pairing", "control", "data"]


def test_connection_kind_values():
    assert issubclass(ConnectionKind, enum.StrEnum)
    assert {k.value for k in ConnectionKind} == {"pairing", "control", "data"}
    assert ConnectionKind.PAIRING == "pairing"
    assert ConnectionKind.CONTROL == "control"
    assert ConnectionKind.DATA == "data"


def test_wrong_connection_kind_is_message_error():
    assert issubclass(WrongConnectionKind, MessageError)
    assert not issubclass(WrongConnectionKind, UnknownMessageType)


@pytest.mark.parametrize("kind", KIND_INPUTS, ids=repr)
def test_allowed_types_match_spec_table(kind):
    allowed = allowed_types(kind)
    assert isinstance(allowed, frozenset)
    assert allowed == EXPECTED_ALLOWED[str(kind)]


@pytest.mark.parametrize("kind", list(ConnectionKind))
@pytest.mark.parametrize("type_name", ["hello", "control_open", "data_open"])
def test_opening_and_hello_types_are_never_allowed_after_opening(kind, type_name):
    assert type_name not in allowed_types(kind)


def test_allowed_types_accepts_plain_string():
    assert allowed_types("control") == allowed_types(ConnectionKind.CONTROL)


@pytest.mark.parametrize("bad", ["foo", "PAIRING", "Control", "", " control", "control "], ids=repr)
def test_allowed_types_rejects_unknown_kind(bad):
    with pytest.raises(ValueError):
        allowed_types(bad)


def test_pair_request_without_cert_opens_pairing():
    assert opening_kind(PairRequest(cert=CERT), has_client_cert=False) is ConnectionKind.PAIRING


def test_pair_request_with_cert_is_wrong_kind():
    with pytest.raises(WrongConnectionKind):
        opening_kind(PairRequest(cert=CERT), has_client_cert=True)


def test_control_open_with_cert_opens_control():
    assert opening_kind(ControlOpen(), has_client_cert=True) is ConnectionKind.CONTROL


def test_control_open_without_cert_is_wrong_kind():
    with pytest.raises(WrongConnectionKind):
        opening_kind(ControlOpen(), has_client_cert=False)


NON_OPENING_MESSAGES = [m for m in VALID_MESSAGES if not isinstance(m, (PairRequest, ControlOpen))]


@pytest.mark.parametrize("has_cert", [False, True])
@pytest.mark.parametrize("msg", NON_OPENING_MESSAGES, ids=lambda m: type(m).__name__)
def test_any_other_message_cannot_open_a_connection(msg, has_cert):
    with pytest.raises(WrongConnectionKind):
        opening_kind(msg, has_client_cert=has_cert)


@pytest.mark.parametrize("kind", KIND_INPUTS, ids=repr)
@pytest.mark.parametrize("msg", VALID_MESSAGES, ids=lambda m: type(m).__name__)
def test_check_message_kind_follows_table(kind, msg):
    if msg.TYPE in EXPECTED_ALLOWED[str(kind)]:
        assert check_message_kind(kind, msg) is None
    else:
        with pytest.raises(WrongConnectionKind):
            check_message_kind(kind, msg)


def test_check_message_kind_error_is_catchable_as_message_error():
    with pytest.raises(MessageError):
        check_message_kind(ConnectionKind.CONTROL, PairCommit(commitment=HEX64))


@pytest.mark.parametrize(
    "msg",
    [PairCommit(commitment=HEX64), PairNonce(nonce=HEX64, signature=SIG128), PairReveal(nonce=HEX64)],
    ids=lambda m: type(m).__name__,
)
def test_new_pairing_messages_only_on_pairing_and_cannot_open(msg):
    with pytest.raises(WrongConnectionKind):
        check_message_kind(ConnectionKind.CONTROL, msg)
    assert check_message_kind(ConnectionKind.PAIRING, msg) is None
    for has_cert in (False, True):
        with pytest.raises(WrongConnectionKind):
            opening_kind(msg, has_client_cert=has_cert)


valid_message_values = st.sampled_from(VALID_MESSAGES) | st.builds(
    lambda cls, t: cls(t=t), st.sampled_from([Ping, Pong]), st.integers(0, MAX_SAFE_INTEGER)
)


@settings(max_examples=300, deadline=None)
@given(msg=valid_message_values, kind=st.sampled_from(KIND_INPUTS), has_cert=st.booleans())
def test_property_kind_table_is_consistent(msg, kind, has_cert):
    try:
        check_message_kind(kind, msg)
        passed = True
    except WrongConnectionKind:
        passed = False
    assert passed == (msg.TYPE in allowed_types(kind))
    try:
        opened = opening_kind(msg, has_client_cert=has_cert)
    except WrongConnectionKind:
        return
    assert opened is not ConnectionKind.DATA
    assert opened in (ConnectionKind.PAIRING, ConnectionKind.CONTROL)
