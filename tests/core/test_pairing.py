"""Test máy trạng thái ghép đôi `teleport.core.pairing` (kế hoạch PR 1, mục 2.3–2.8).

Viết từ đặc tả (kế hoạch + docs/protocol.md §5), không từ code. Chứng chỉ ở đây là PEM giả:
`core` coi PEM là chuỗi mờ, chỉ cần bắt đầu bằng CERT_PEM_HEADER và không quá MAX_CERT_PEM_LEN.
"""

from __future__ import annotations

import ast
import hashlib
import math
from pathlib import Path

import hypothesis
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

import teleport.core.pairing as pairing_mod
from teleport.core.messages import (
    CERT_PEM_HEADER,
    MAX_CERT_PEM_LEN,
    AbortReason,
    Hello,
    PairAbort,
    PairChallenge,
    PairConfirm,
    PairRequest,
    PairReveal,
)
from teleport.core.pairing import (
    CONFIRM_GRACE,
    CONFIRM_TIMEOUT,
    MESSAGE_TIMEOUT,
    BadMessage,
    Cancel,
    Close,
    ConnectionLost,
    DeviceCert,
    InitiatorSession,
    MessageReceived,
    Outcome,
    PairingUsageError,
    ResponderSession,
    Role,
    SavePeer,
    SendMessage,
    ShowCode,
    Start,
    State,
    Tick,
    UserDecision,
)
from teleport.core.pairing_code import NONCE_SIZE, make_commitment, pairing_code

# --- Dữ liệu dựng sẵn ---------------------------------------------------------------------------


def fake_pem(tag: str) -> str:
    return f"{CERT_PEM_HEADER}\nZmFrZS1{tag}\n-----END CERTIFICATE-----\n"


def make_cert(tag: str, *, device_id: str | None = None, fingerprint: str | None = None) -> DeviceCert:
    return DeviceCert(
        device_id=device_id
        if device_id is not None
        else hashlib.sha256(b"id:" + tag.encode()).hexdigest()[:32],
        fingerprint=fingerprint if fingerprint is not None else hashlib.sha256(tag.encode()).hexdigest(),
        pem=fake_pem(tag),
    )


def make_hello(device_id: str, name: str) -> Hello:
    return Hello(protocol=1, device_id=device_id, name=name, app_version="0.1.0", os="windows")


CERT_A = make_cert("A")  # bên khởi xướng
CERT_B = make_cert("B")  # bên trả lời
CERT_C = make_cert("C")  # máy thứ ba / kẻ giả mạo
HELLO_A = make_hello(CERT_A.device_id, "Máy của An")
HELLO_B = make_hello(CERT_B.device_id, "Laptop Bình")
N_A = bytes(range(NONCE_SIZE))
N_B = bytes(range(NONCE_SIZE, 2 * NONCE_SIZE))
N_OTHER = bytes([0xEE] * NONCE_SIZE)
REQUEST_A = PairRequest(cert=CERT_A.pem, commitment=make_commitment(CERT_A.fingerprint, N_A))
CODE_AB = pairing_code(CERT_A.fingerprint, CERT_B.fingerprint, N_A, N_B)

T0 = 100.0  # thời điểm Start
SHOWN_AT = T0 + 1  # thời điểm hiện mã trong các builder bên dưới

ABORT_INVALID = SendMessage(PairAbort("invalid"))
ABORT_TIMEOUT = SendMessage(PairAbort("timeout"))
ABORT_USER = SendMessage(PairAbort("user"))
ABORT_BUSY = SendMessage(PairAbort("busy"))

PEER_ABORT_MAP = {
    "user": Outcome.PEER_REJECTED,
    "busy": Outcome.PEER_BUSY,
    "timeout": Outcome.PEER_TIMEOUT,
    "invalid": Outcome.PEER_INVALID,
}


def make_initiator(
    *,
    local: DeviceCert = CERT_A,
    peer: DeviceCert = CERT_B,
    peer_hello: Hello = HELLO_B,
    nonce: bytes = N_A,
    known_fingerprint: str | None = None,
) -> InitiatorSession:
    return InitiatorSession(
        local=local, peer=peer, peer_hello=peer_hello, nonce=nonce, known_fingerprint=known_fingerprint
    )


def make_responder(
    *,
    local: DeviceCert = CERT_B,
    peer: DeviceCert | None = CERT_A,
    peer_hello: Hello = HELLO_A,
    request: PairRequest = REQUEST_A,
    nonce: bytes = N_B,
    known_fingerprint: str | None = None,
) -> ResponderSession:
    return ResponderSession(
        local=local,
        peer=peer,
        peer_hello=peer_hello,
        request=request,
        nonce=nonce,
        known_fingerprint=known_fingerprint,
    )


def assert_closed(actions: list, outcome: Outcome, peer_reason: str | None = None) -> None:
    """`Close` là phần tử cuối, đúng một lần, đúng outcome và peer_reason (detail tự do)."""
    assert actions, "mong đợi có Close nhưng danh sách rỗng"
    last = actions[-1]
    assert isinstance(last, Close), f"phần tử cuối không phải Close: {actions!r}"
    assert last.outcome == outcome
    assert last.peer_reason == peer_reason
    assert sum(isinstance(a, Close) for a in actions) == 1


def assert_done(session, outcome: Outcome) -> None:
    assert session.state == State.DONE
    assert session.outcome == outcome
    assert session.deadline is None


# Builder: trả về (phiên, thời điểm của lời gọi cuối) ở từng trạng thái đang chạy.


def init_await():
    s = make_initiator()
    s.handle(Start(), T0)
    return s, T0


def init_confirming():
    s, _ = init_await()
    s.handle(MessageReceived(PairChallenge(N_B.hex())), SHOWN_AT)
    return s, SHOWN_AT


def init_local_confirmed():
    s, _ = init_confirming()
    s.handle(UserDecision(True), SHOWN_AT + 5)
    return s, SHOWN_AT + 5


def init_peer_confirmed():
    s, _ = init_confirming()
    s.handle(MessageReceived(PairConfirm()), SHOWN_AT + 5)
    return s, SHOWN_AT + 5


def resp_await():
    s = make_responder()
    s.handle(Start(), T0)
    return s, T0


def resp_confirming():
    s, _ = resp_await()
    s.handle(MessageReceived(PairReveal(N_A.hex())), SHOWN_AT)
    return s, SHOWN_AT


def resp_local_confirmed():
    s, _ = resp_confirming()
    s.handle(UserDecision(True), SHOWN_AT + 5)
    return s, SHOWN_AT + 5


def resp_peer_confirmed():
    s, _ = resp_confirming()
    s.handle(MessageReceived(PairConfirm()), SHOWN_AT + 5)
    return s, SHOWN_AT + 5


ACTIVE_BUILDERS = {
    "init-await_challenge": init_await,
    "init-confirming": init_confirming,
    "init-local_confirmed": init_local_confirmed,
    "init-peer_confirmed": init_peer_confirmed,
    "resp-await_reveal": resp_await,
    "resp-confirming": resp_confirming,
    "resp-local_confirmed": resp_local_confirmed,
    "resp-peer_confirmed": resp_peer_confirmed,
}
# deadline mong đợi ở từng trạng thái (mục 2.5–2.7).
EXPECTED_DEADLINE = {
    "init-await_challenge": T0 + 10,
    "init-confirming": SHOWN_AT + 60,
    "init-local_confirmed": SHOWN_AT + 70,
    "init-peer_confirmed": SHOWN_AT + 60,
    "resp-await_reveal": T0 + 10,
    "resp-confirming": SHOWN_AT + 60,
    "resp-local_confirmed": SHOWN_AT + 70,
    "resp-peer_confirmed": SHOWN_AT + 60,
}

active_builders = pytest.mark.parametrize("build", list(ACTIVE_BUILDERS.values()), ids=list(ACTIVE_BUILDERS))


def done_initiator_paired():
    s, now = init_local_confirmed()
    s.handle(MessageReceived(PairConfirm()), now + 1)
    return s, now + 1


def done_initiator_cancelled():
    s, now = init_await()
    s.handle(Cancel(), now)
    return s, now


def done_responder_rejected():
    s, now = resp_confirming()
    s.handle(UserDecision(False), now)
    return s, now


def done_initiator_idle_cancel():
    s = make_initiator()
    s.handle(Cancel(), T0)
    return s, T0


DONE_BUILDERS = {
    "init-paired": done_initiator_paired,
    "init-cancelled": done_initiator_cancelled,
    "resp-user_rejected": done_responder_rejected,
    "init-cancel_at_idle": done_initiator_idle_cancel,
}
done_builders = pytest.mark.parametrize("build", list(DONE_BUILDERS.values()), ids=list(DONE_BUILDERS))


# Mọi loại sự kiện (trừ Start) để thử ở các trạng thái.
ALL_NON_START_EVENTS = [
    Tick(),
    UserDecision(True),
    UserDecision(False),
    BadMessage("khung hỏng"),
    ConnectionLost(),
    Cancel(),
    Cancel(AbortReason.BUSY),
    MessageReceived(PairConfirm()),
    MessageReceived(PairAbort("user")),
    MessageReceived(PairChallenge(N_B.hex())),
    MessageReceived(PairReveal(N_A.hex())),
    MessageReceived(HELLO_A),
]


# --- Hằng số, kiểu ------------------------------------------------------------------------------


def test_timeout_constants_match_protocol():
    assert MESSAGE_TIMEOUT == 10.0
    assert CONFIRM_TIMEOUT == 60.0
    assert CONFIRM_GRACE == 10.0


def test_enum_values():
    assert Role.INITIATOR == "initiator"
    assert Role.RESPONDER == "responder"
    assert {s.value for s in State} == {"idle", "await_challenge", "await_reveal", "confirming", "done"}
    assert {o.value for o in Outcome} == {
        "paired",
        "user_rejected",
        "timeout",
        "protocol_error",
        "identity_conflict",
        "peer_rejected",
        "peer_busy",
        "peer_timeout",
        "peer_invalid",
        "peer_aborted",
        "connection_lost",
        "cancelled",
    }


def test_pairing_usage_error_is_runtime_error():
    assert issubclass(PairingUsageError, RuntimeError)


def test_close_defaults():
    c = Close(Outcome.TIMEOUT)
    assert c.peer_reason is None
    assert c.detail == ""


def test_pairing_module_does_not_import_time_or_secrets():
    # core sans-IO: thời gian và nonce do app truyền vào (kế hoạch 2.1, tiêu chí xong).
    tree = ast.parse(Path(pairing_mod.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Import tương đối trong teleport/core: quy về tên tuyệt đối.
                base = ["teleport", "core"][: max(0, 3 - node.level)]
                if node.module:
                    imported.add(".".join([*base, node.module]))
                else:
                    imported |= {".".join([*base, alias.name]) for alias in node.names}
            elif node.module:
                imported.add(node.module)
    roots = {name.split(".")[0] for name in imported}
    forbidden = {"time", "secrets", "random", "os", "asyncio", "socket"}
    assert not roots & forbidden, f"core/pairing.py không được import: {roots & forbidden}"
    teleport_imports = {name for name in imported if name.split(".")[0] == "teleport"}
    assert teleport_imports <= {"teleport.core.messages", "teleport.core.pairing_code"}


# --- DeviceCert ---------------------------------------------------------------------------------


def test_device_cert_accepts_valid_values():
    c = DeviceCert(device_id="0" * 32, fingerprint="a" * 64, pem=fake_pem("x"))
    assert c.device_id == "0" * 32
    assert c.fingerprint == "a" * 64


def test_device_cert_accepts_pem_exactly_at_limit():
    pem = CERT_PEM_HEADER + "A" * (MAX_CERT_PEM_LEN - len(CERT_PEM_HEADER))
    assert len(pem) == MAX_CERT_PEM_LEN
    DeviceCert(device_id="0" * 32, fingerprint="a" * 64, pem=pem)


@pytest.mark.parametrize(
    "device_id", ["", "0" * 31, "0" * 33, "A" * 32, "g" * 32, "0" * 31 + "\n", 123, None, b"0" * 32]
)
def test_device_cert_rejects_bad_device_id(device_id):
    with pytest.raises(ValueError):
        DeviceCert(device_id=device_id, fingerprint="a" * 64, pem=fake_pem("x"))


@pytest.mark.parametrize(
    "fingerprint", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 63 + "\n", None, b"a" * 64, 0]
)
def test_device_cert_rejects_bad_fingerprint(fingerprint):
    with pytest.raises(ValueError):
        DeviceCert(device_id="0" * 32, fingerprint=fingerprint, pem=fake_pem("x"))


@pytest.mark.parametrize(
    "pem",
    [
        "",
        "-----BEGIN PRIVATE KEY-----\nabc\n",
        " " + fake_pem("x"),
        CERT_PEM_HEADER + "A" * (MAX_CERT_PEM_LEN - len(CERT_PEM_HEADER) + 1),
        fake_pem("x").encode(),
        None,
    ],
    ids=["empty", "wrong-header", "leading-space", "too-long-by-1", "bytes", "none"],
)
def test_device_cert_rejects_bad_pem(pem):
    with pytest.raises(ValueError):
        DeviceCert(device_id="0" * 32, fingerprint="a" * 64, pem=pem)


# --- Sự kiện ------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 0, "yes", None, 1.0])
def test_user_decision_requires_real_bool(value):
    with pytest.raises(ValueError):
        UserDecision(value)


def test_cancel_defaults_to_user():
    assert Cancel().reason == "user"


@pytest.mark.parametrize("reason", ["user", "busy", AbortReason.USER, AbortReason.BUSY])
def test_cancel_accepts_user_and_busy(reason):
    assert Cancel(reason).reason == reason


@pytest.mark.parametrize("reason", ["timeout", "invalid", "x", "", "USER", None, AbortReason.TIMEOUT])
def test_cancel_rejects_other_reasons(reason):
    with pytest.raises(ValueError):
        Cancel(reason)


# --- Kiểm tra tham số khởi tạo (mục 2.3) --------------------------------------------------------

BAD_NONCES = [b"", N_A[:-1], N_A + b"\x00", bytearray(N_A), N_A.hex(), None, list(N_A)]
BAD_KNOWN_FP = ["", "a" * 63, "a" * 65, "A" * 64, "a" * 63 + "\n", b"a" * 64, 0]
BAD_HELLOS = [None, {"type": "hello"}, PairChallenge(N_B.hex()), "hello"]
BAD_CERTS = [None, "cert", CERT_A.pem, (CERT_A.device_id, CERT_A.fingerprint, CERT_A.pem)]


@pytest.mark.parametrize("nonce", BAD_NONCES, ids=repr)
def test_initiator_rejects_bad_nonce(nonce):
    with pytest.raises(ValueError):
        make_initiator(nonce=nonce)


@pytest.mark.parametrize("nonce", BAD_NONCES, ids=repr)
def test_responder_rejects_bad_nonce(nonce):
    with pytest.raises(ValueError):
        make_responder(nonce=nonce)


@pytest.mark.parametrize("known", BAD_KNOWN_FP, ids=repr)
def test_initiator_rejects_bad_known_fingerprint(known):
    with pytest.raises(ValueError):
        make_initiator(known_fingerprint=known)


@pytest.mark.parametrize("known", BAD_KNOWN_FP, ids=repr)
def test_responder_rejects_bad_known_fingerprint(known):
    with pytest.raises(ValueError):
        make_responder(known_fingerprint=known)


@pytest.mark.parametrize("hello", BAD_HELLOS, ids=repr)
def test_initiator_rejects_non_hello_peer_hello(hello):
    with pytest.raises(ValueError):
        make_initiator(peer_hello=hello)


@pytest.mark.parametrize("hello", BAD_HELLOS, ids=repr)
def test_responder_rejects_non_hello_peer_hello(hello):
    with pytest.raises(ValueError):
        make_responder(peer_hello=hello)


@pytest.mark.parametrize("request_", [None, PairChallenge(N_B.hex()), {"type": "pair_request"}], ids=repr)
def test_responder_rejects_non_pair_request(request_):
    with pytest.raises(ValueError):
        make_responder(request=request_)


@pytest.mark.parametrize("cert", BAD_CERTS, ids=repr)
def test_initiator_rejects_non_device_cert_local(cert):
    with pytest.raises(ValueError):
        make_initiator(local=cert)


@pytest.mark.parametrize("cert", BAD_CERTS, ids=repr)
def test_initiator_rejects_non_device_cert_peer(cert):
    # Bên khởi xướng luôn có chứng chỉ của B từ bắt tay TLS: None cũng sai.
    with pytest.raises(ValueError):
        make_initiator(peer=cert)


@pytest.mark.parametrize("cert", BAD_CERTS, ids=repr)
def test_responder_rejects_non_device_cert_local(cert):
    with pytest.raises(ValueError):
        make_responder(local=cert)


@pytest.mark.parametrize("cert", BAD_CERTS[1:], ids=repr)
def test_responder_rejects_non_device_cert_peer(cert):
    with pytest.raises(ValueError):
        make_responder(peer=cert)


def test_responder_accepts_none_peer():
    s = make_responder(peer=None)
    assert s.state == State.IDLE


def test_sessions_are_keyword_only():
    with pytest.raises(TypeError):
        InitiatorSession(CERT_A, CERT_B, HELLO_B, N_A)  # type: ignore[misc]
    with pytest.raises(TypeError):
        ResponderSession(CERT_B, CERT_A, HELLO_A, REQUEST_A, N_B)  # type: ignore[misc]


def test_initial_properties():
    for s, role in ((make_initiator(), Role.INITIATOR), (make_responder(), Role.RESPONDER)):
        assert s.role == role
        assert s.state == State.IDLE
        assert s.deadline is None
        assert s.code is None
        assert s.outcome is None
        assert s.local_confirmed is False
        assert s.peer_confirmed is False


# --- handle: kiểu sự kiện và thời gian ---------------------------------------------------------

NOT_EVENTS = [None, "start", Start, PairConfirm(), ShowCode("123456"), Close(Outcome.CANCELLED), 0]


@pytest.mark.parametrize("event", NOT_EVENTS, ids=repr)
def test_handle_rejects_non_event_at_idle(event):
    with pytest.raises(TypeError):
        make_initiator().handle(event, T0)


@pytest.mark.parametrize("event", NOT_EVENTS, ids=repr)
def test_handle_rejects_non_event_when_active(event):
    s, now = resp_confirming()
    with pytest.raises(TypeError):
        s.handle(event, now)


NOT_MESSAGES = [None, "pair_confirm", {"type": "pair_confirm"}, Tick(), PairConfirm, 0]


@pytest.mark.parametrize("payload", NOT_MESSAGES, ids=repr)
def test_message_received_with_non_message_is_type_error(payload):
    # Chấp nhận raise lúc tạo MessageReceived hoặc lúc handle: cả hai đều nằm trong khối.
    s, now = resp_confirming()
    with pytest.raises(TypeError):
        s.handle(MessageReceived(payload), now)


@pytest.mark.parametrize("bad_now", [True, False, "1", None, b"1"], ids=repr)
def test_handle_rejects_non_number_now_at_idle(bad_now):
    with pytest.raises(TypeError):
        make_initiator().handle(Start(), bad_now)


@pytest.mark.parametrize("bad_now", [True, "1", None], ids=repr)
@active_builders
def test_handle_rejects_non_number_now_when_active(build, bad_now):
    s, _ = build()
    with pytest.raises(TypeError):
        s.handle(Tick(), bad_now)


@pytest.mark.parametrize("bad_now", [math.nan, math.inf, -math.inf])
def test_handle_rejects_non_finite_now_at_first_call(bad_now):
    with pytest.raises(ValueError):
        make_initiator().handle(Start(), bad_now)


@pytest.mark.parametrize("bad_now", [math.nan, math.inf, -math.inf])
@active_builders
def test_handle_rejects_non_finite_now_when_active(build, bad_now):
    s, _ = build()
    with pytest.raises(ValueError):
        s.handle(Tick(), bad_now)


@active_builders
def test_handle_rejects_time_going_backwards(build):
    s, now = build()
    with pytest.raises(ValueError):
        s.handle(Tick(), now - 0.001)


@active_builders
def test_handle_accepts_same_now_twice(build):
    s, now = build()
    assert s.handle(Tick(), now) == []


@pytest.mark.parametrize("bad_now", [math.nan, math.inf, T0 - 1])
@done_builders
def test_handle_validates_now_even_when_done(build, bad_now):
    # "Thời gian now ... truyền vào mọi lời gọi": kể cả ở DONE.
    s, _ = build()
    with pytest.raises(ValueError):
        s.handle(Tick(), bad_now)


def test_handle_rejects_backwards_time_from_idle_start():
    s = make_responder()
    s.handle(Start(), 5.0)
    with pytest.raises(ValueError):
        s.handle(Tick(), 4.0)


# --- R1: IDLE -----------------------------------------------------------------------------------


@pytest.mark.parametrize("factory", [make_initiator, make_responder, lambda: make_responder(peer=None)])
@pytest.mark.parametrize("cancel", [Cancel(), Cancel(AbortReason.BUSY)], ids=["user", "busy"])
def test_idle_cancel_closes_without_sending(factory, cancel):
    # Câu 5: Cancel(BUSY) khi phiên khởi xướng còn IDLE → chỉ đóng, không gửi gì.
    s = factory()
    actions = s.handle(cancel, T0)
    assert len(actions) == 1
    assert_closed(actions, Outcome.CANCELLED)
    assert_done(s, Outcome.CANCELLED)


@pytest.mark.parametrize("factory", [make_initiator, make_responder])
def test_idle_connection_lost_closes(factory):
    s = factory()
    actions = s.handle(ConnectionLost(), T0)
    assert len(actions) == 1
    assert_closed(actions, Outcome.CONNECTION_LOST)
    assert_done(s, Outcome.CONNECTION_LOST)


IDLE_FORBIDDEN = [
    Tick(),
    UserDecision(True),
    UserDecision(False),
    BadMessage(),
    MessageReceived(PairConfirm()),
    MessageReceived(PairAbort("user")),
    MessageReceived(PairChallenge(N_B.hex())),
    MessageReceived(PairReveal(N_A.hex())),
]


@pytest.mark.parametrize("factory", [make_initiator, make_responder])
@pytest.mark.parametrize("event", IDLE_FORBIDDEN, ids=repr)
def test_idle_other_events_are_usage_errors(factory, event):
    with pytest.raises(PairingUsageError):
        factory().handle(event, T0)


# --- R2: DONE -----------------------------------------------------------------------------------


@done_builders
@pytest.mark.parametrize("event", ALL_NON_START_EVENTS, ids=repr)
def test_done_ignores_every_event(build, event):
    s, now = build()
    outcome = s.outcome
    assert s.handle(event, now + 1000) == []
    assert_done(s, outcome)


@done_builders
def test_done_start_is_usage_error(build):
    s, now = build()
    with pytest.raises(PairingUsageError):
        s.handle(Start(), now)


def test_start_twice_is_usage_error():
    s, now = init_await()
    # Start khi đang chạy không nằm trong bảng; theo R1/R2 nó chỉ hợp lệ ở IDLE.
    # Đọc hợp lý nhất: đang chạy mà Start lại là lỗi lập trình của app.
    with pytest.raises(PairingUsageError):
        s.handle(Start(), now)


# --- Bảng 2.5: bên khởi xướng ------------------------------------------------------------------


def test_initiator_start_sends_pair_request_with_commitment():
    s = make_initiator()
    actions = s.handle(Start(), T0)
    assert actions == [
        SendMessage(PairRequest(cert=CERT_A.pem, commitment=make_commitment(CERT_A.fingerprint, N_A)))
    ]
    assert s.state == State.AWAIT_CHALLENGE
    assert s.deadline == T0 + 10
    assert s.code is None
    assert s.outcome is None


def test_initiator_start_closes_silently_when_peer_cert_cn_differs_from_hello():
    # Câu 1: lỗi trước khi gửi pair_request → chỉ đóng, KHÔNG gửi gì.
    s = make_initiator(peer_hello=make_hello(CERT_C.device_id, "Kẻ giả"))
    actions = s.handle(Start(), T0)
    assert len(actions) == 1
    assert_closed(actions, Outcome.PROTOCOL_ERROR)
    assert_done(s, Outcome.PROTOCOL_ERROR)


def test_initiator_start_rejects_pairing_with_same_fingerprint():
    peer = make_cert("B", fingerprint=CERT_A.fingerprint)
    s = make_initiator(peer=peer, peer_hello=make_hello(peer.device_id, "Gương"))
    actions = s.handle(Start(), T0)
    assert len(actions) == 1
    assert_closed(actions, Outcome.PROTOCOL_ERROR)


def test_initiator_start_rejects_pairing_with_same_device_id():
    peer = make_cert("B", device_id=CERT_A.device_id)
    s = make_initiator(peer=peer, peer_hello=make_hello(CERT_A.device_id, "Gương"))
    actions = s.handle(Start(), T0)
    assert len(actions) == 1
    assert_closed(actions, Outcome.PROTOCOL_ERROR)


def test_initiator_start_identity_conflict_before_showing_code():
    s = make_initiator(known_fingerprint=CERT_C.fingerprint)
    actions = s.handle(Start(), T0)
    assert len(actions) == 1
    assert_closed(actions, Outcome.IDENTITY_CONFLICT)
    assert_done(s, Outcome.IDENTITY_CONFLICT)
    assert s.code is None


def test_initiator_start_cn_mismatch_takes_priority_over_identity_conflict():
    # Bảng xét theo thứ tự: CN ≠ hello đứng trước trùng danh tính.
    s = make_initiator(
        peer_hello=make_hello(CERT_C.device_id, "Kẻ giả"), known_fingerprint=CERT_C.fingerprint
    )
    actions = s.handle(Start(), T0)
    assert len(actions) == 1
    assert_closed(actions, Outcome.PROTOCOL_ERROR)


def test_initiator_start_same_known_fingerprint_is_allowed():
    # Ghép đôi lại sau ghép đôi một phía: cùng device_id, cùng fingerprint → được.
    s = make_initiator(known_fingerprint=CERT_B.fingerprint)
    actions = s.handle(Start(), T0)
    assert actions == [SendMessage(REQUEST_A)]
    assert s.state == State.AWAIT_CHALLENGE


def test_initiator_challenge_reveals_nonce_and_shows_code():
    s, _ = init_await()
    actions = s.handle(MessageReceived(PairChallenge(N_B.hex())), T0 + 3)
    assert actions == [SendMessage(PairReveal(N_A.hex())), ShowCode(CODE_AB)]
    assert s.state == State.CONFIRMING
    assert s.code == CODE_AB
    assert s.deadline == T0 + 3 + 60
    assert s.local_confirmed is False
    assert s.peer_confirmed is False


def test_initiator_code_uses_initiator_fingerprint_first():
    s, _ = init_await()
    actions = s.handle(MessageReceived(PairChallenge(N_B.hex())), T0 + 1)
    code = actions[1].code
    assert code == pairing_code(CERT_A.fingerprint, CERT_B.fingerprint, N_A, N_B)
    assert len(code) == 6
    assert code.isascii()
    assert code.isdigit()


def test_initiator_times_out_after_10s_without_challenge():
    s, _ = init_await()
    assert s.handle(Tick(), T0 + 9.999) == []
    actions = s.handle(Tick(), T0 + 10)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)
    assert_done(s, Outcome.TIMEOUT)


def test_initiator_challenge_at_deadline_is_timeout_not_code():
    s, _ = init_await()
    actions = s.handle(MessageReceived(PairChallenge(N_B.hex())), T0 + 10)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)
    assert s.code is None


# --- Bảng 2.6: bên trả lời ---------------------------------------------------------------------


def test_responder_start_sends_challenge():
    s = make_responder()
    actions = s.handle(Start(), T0)
    assert actions == [SendMessage(PairChallenge(N_B.hex()))]
    assert s.state == State.AWAIT_REVEAL
    assert s.deadline == T0 + 10


def test_responder_start_with_unparsable_cert_aborts_invalid():
    s = make_responder(peer=None)
    actions = s.handle(Start(), T0)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)
    assert_done(s, Outcome.PROTOCOL_ERROR)


def test_responder_start_with_none_peer_and_known_fp_is_protocol_error():
    # peer None đứng đầu bảng, trước kiểm tra trùng danh tính.
    s = make_responder(peer=None, known_fingerprint=CERT_C.fingerprint)
    actions = s.handle(Start(), T0)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)


def test_responder_start_cn_differs_from_hello_aborts_invalid():
    s = make_responder(peer_hello=make_hello(CERT_C.device_id, "Kẻ giả"))
    actions = s.handle(Start(), T0)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)


def test_responder_start_rejects_same_fingerprint():
    peer = make_cert("A", fingerprint=CERT_B.fingerprint)
    s = make_responder(
        peer=peer,
        peer_hello=make_hello(peer.device_id, "Gương"),
        request=PairRequest(cert=peer.pem, commitment=make_commitment(peer.fingerprint, N_A)),
    )
    actions = s.handle(Start(), T0)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)


def test_responder_start_rejects_same_device_id():
    peer = make_cert("A", device_id=CERT_B.device_id)
    s = make_responder(
        peer=peer,
        peer_hello=make_hello(CERT_B.device_id, "Gương"),
        request=PairRequest(cert=peer.pem, commitment=make_commitment(peer.fingerprint, N_A)),
    )
    actions = s.handle(Start(), T0)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)


def test_responder_start_identity_conflict_aborts_invalid_before_code():
    s = make_responder(known_fingerprint=CERT_C.fingerprint)
    actions = s.handle(Start(), T0)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.IDENTITY_CONFLICT)
    assert_done(s, Outcome.IDENTITY_CONFLICT)
    assert s.code is None


def test_responder_start_same_known_fingerprint_is_allowed():
    s = make_responder(known_fingerprint=CERT_A.fingerprint)
    assert s.handle(Start(), T0) == [SendMessage(PairChallenge(N_B.hex()))]


def test_responder_correct_reveal_shows_code():
    s, _ = resp_await()
    actions = s.handle(MessageReceived(PairReveal(N_A.hex())), T0 + 2)
    assert actions == [ShowCode(CODE_AB)]
    assert s.state == State.CONFIRMING
    assert s.code == CODE_AB
    assert s.deadline == T0 + 2 + 60


def test_both_roles_compute_same_code():
    a, _ = init_confirming()
    b, _ = resp_confirming()
    assert a.code == b.code == CODE_AB


def test_responder_wrong_reveal_is_invalid():
    s, _ = resp_await()
    actions = s.handle(MessageReceived(PairReveal(N_OTHER.hex())), T0 + 1)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)
    assert s.code is None


def test_responder_commitment_from_other_fingerprint_is_invalid():
    # Cam kết làm với fingerprint khác chứng chỉ trong pair_request → sai.
    req = PairRequest(cert=CERT_A.pem, commitment=make_commitment(CERT_C.fingerprint, N_A))
    s = make_responder(request=req)
    s.handle(Start(), T0)
    actions = s.handle(MessageReceived(PairReveal(N_A.hex())), T0 + 1)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)


def test_responder_times_out_after_10s_without_reveal():
    s, _ = resp_await()
    assert s.handle(Tick(), T0 + 9.999) == []
    actions = s.handle(Tick(), T0 + 10)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)


def test_responder_reveal_at_deadline_is_timeout():
    s, _ = resp_await()
    actions = s.handle(MessageReceived(PairReveal(N_A.hex())), T0 + 10)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)
    assert s.code is None


# --- R4: message không nằm trong bảng của trạng thái -------------------------------------------

OTHER_REQUEST = PairRequest(cert=CERT_C.pem, commitment=make_commitment(CERT_C.fingerprint, N_OTHER))
OUT_OF_ORDER = {
    init_await: [HELLO_B, REQUEST_A, PairReveal(N_A.hex()), PairConfirm()],
    resp_await: [HELLO_A, OTHER_REQUEST, PairChallenge(N_B.hex()), PairConfirm()],
    init_confirming: [HELLO_B, REQUEST_A, PairChallenge(N_B.hex()), PairReveal(N_A.hex())],
    init_local_confirmed: [HELLO_B, PairChallenge(N_B.hex()), PairReveal(N_A.hex())],
    init_peer_confirmed: [HELLO_B, PairChallenge(N_B.hex()), PairConfirm()],  # PairConfirm lần hai
    resp_confirming: [HELLO_A, OTHER_REQUEST, PairChallenge(N_B.hex()), PairReveal(N_A.hex())],
    resp_local_confirmed: [HELLO_A, PairReveal(N_A.hex()), PairChallenge(N_B.hex())],
    resp_peer_confirmed: [HELLO_A, PairReveal(N_A.hex()), PairConfirm()],  # PairConfirm lần hai
}
OUT_OF_ORDER_CASES = [
    pytest.param(build, msg, id=f"{build.__name__}-{type(msg).__name__}")
    for build, msgs in OUT_OF_ORDER.items()
    for msg in msgs
]


@pytest.mark.parametrize(("build", "msg"), OUT_OF_ORDER_CASES)
def test_out_of_order_message_aborts_invalid(build, msg):
    s, now = build()
    actions = s.handle(MessageReceived(msg), now + 1)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)
    assert_done(s, Outcome.PROTOCOL_ERROR)
    assert not any(isinstance(a, SavePeer) for a in actions)


@active_builders
def test_bad_message_aborts_invalid(build):
    s, now = build()
    actions = s.handle(BadMessage("JSON sai"), now + 1)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)


# --- R3: nhận pair_abort ------------------------------------------------------------------------


@active_builders
@pytest.mark.parametrize("reason", ["user", "busy", "timeout", "invalid", "from_newer_version"])
def test_peer_abort_maps_reason_and_does_not_reply(build, reason):
    s, now = build()
    actions = s.handle(MessageReceived(PairAbort(reason)), now + 1)
    expected = PEER_ABORT_MAP.get(reason, Outcome.PEER_ABORTED)
    assert len(actions) == 1, "nhận pair_abort thì không trả lời"
    assert_closed(actions, expected, peer_reason=reason)
    assert_done(s, expected)


# --- R5, R6, R7 ---------------------------------------------------------------------------------


@active_builders
def test_connection_lost_closes_without_sending(build):
    s, now = build()
    actions = s.handle(ConnectionLost(), now + 1)
    assert len(actions) == 1
    assert_closed(actions, Outcome.CONNECTION_LOST)
    assert_done(s, Outcome.CONNECTION_LOST)


@active_builders
@pytest.mark.parametrize("reason", ["user", "busy"])
def test_cancel_sends_abort_with_reason(build, reason):
    s, now = build()
    actions = s.handle(Cancel(reason), now + 1)
    assert actions[:-1] == [SendMessage(PairAbort(reason))]
    assert_closed(actions, Outcome.CANCELLED)
    assert_done(s, Outcome.CANCELLED)


@pytest.mark.parametrize("name", list(ACTIVE_BUILDERS))
def test_deadline_property_per_state(name):
    s, _ = ACTIVE_BUILDERS[name]()
    assert s.deadline == EXPECTED_DEADLINE[name]


@active_builders
def test_tick_before_deadline_does_nothing(build):
    s, _ = build()
    state, deadline = s.state, s.deadline
    assert s.handle(Tick(), deadline - 0.001) == []
    assert s.state == state
    assert s.deadline == deadline


@pytest.mark.parametrize("build", [init_await, resp_await, init_local_confirmed, resp_local_confirmed])
@pytest.mark.parametrize("accepted", [True, False])
def test_user_decision_outside_prompt_is_ignored(build, accepted):
    s, now = build()
    state, deadline, local = s.state, s.deadline, s.local_confirmed
    assert s.handle(UserDecision(accepted), now + 1) == []
    assert s.state == state
    assert s.deadline == deadline
    assert s.local_confirmed == local


# --- R0: hết giờ xét trước mọi sự kiện ---------------------------------------------------------

R0_EVENTS = [
    Tick(),
    UserDecision(True),
    UserDecision(False),
    BadMessage(),
    ConnectionLost(),
    Cancel(),
    Cancel(AbortReason.BUSY),
    MessageReceived(PairConfirm()),
    MessageReceived(PairAbort("user")),
    MessageReceived(PairChallenge(N_B.hex())),
    MessageReceived(PairReveal(N_A.hex())),
    MessageReceived(HELLO_A),
]


@active_builders
@pytest.mark.parametrize("event", R0_EVENTS, ids=repr)
@pytest.mark.parametrize("late", [0.0, 0.1, 1000.0])
def test_any_event_at_or_after_deadline_is_timeout(build, event, late):
    s, _ = build()
    actions = s.handle(event, s.deadline + late)
    assert actions[:-1] == [ABORT_TIMEOUT], "hết giờ xét trước, sự kiện bị bỏ"
    assert_closed(actions, Outcome.TIMEOUT)
    assert_done(s, Outcome.TIMEOUT)
    assert not any(isinstance(a, SavePeer) for a in actions)


# --- Bảng 2.7: CONFIRMING ----------------------------------------------------------------------

ROLE_CONFIRMING = {
    "initiator": (init_confirming, CERT_B, HELLO_B.name),
    "responder": (resp_confirming, CERT_A, HELLO_A.name),
}
by_role = pytest.mark.parametrize("role", list(ROLE_CONFIRMING))


@by_role
def test_local_confirm_first_sends_confirm_and_extends_deadline(role):
    build, _, _ = ROLE_CONFIRMING[role]
    s, _ = build()
    actions = s.handle(UserDecision(True), SHOWN_AT + 30)
    assert actions == [SendMessage(PairConfirm())]
    assert s.local_confirmed is True
    assert s.peer_confirmed is False
    assert s.state == State.CONFIRMING
    # Thêm CONFIRM_GRACE kể từ lúc hiện mã, không kể từ lúc bấm.
    assert s.deadline == SHOWN_AT + 60 + 10


@by_role
def test_local_then_peer_confirm_saves_peer_and_pairs(role):
    build, peer, name = ROLE_CONFIRMING[role]
    s, _ = build()
    s.handle(UserDecision(True), SHOWN_AT + 30)
    actions = s.handle(MessageReceived(PairConfirm()), SHOWN_AT + 31)
    assert actions[:-1] == [SavePeer(peer, name)]
    assert_closed(actions, Outcome.PAIRED)
    assert_done(s, Outcome.PAIRED)


@by_role
def test_peer_confirm_first_waits_and_keeps_deadline(role):
    build, _, _ = ROLE_CONFIRMING[role]
    s, _ = build()
    assert s.handle(MessageReceived(PairConfirm()), SHOWN_AT + 20) == []
    assert s.peer_confirmed is True
    assert s.local_confirmed is False
    assert s.state == State.CONFIRMING
    assert s.deadline == SHOWN_AT + 60


@by_role
def test_peer_then_local_confirm_sends_confirm_saves_and_pairs(role):
    build, peer, name = ROLE_CONFIRMING[role]
    s, _ = build()
    s.handle(MessageReceived(PairConfirm()), SHOWN_AT + 20)
    actions = s.handle(UserDecision(True), SHOWN_AT + 40)
    assert actions[:-1] == [SendMessage(PairConfirm()), SavePeer(peer, name)]
    assert_closed(actions, Outcome.PAIRED)
    assert_done(s, Outcome.PAIRED)


@by_role
def test_user_rejects_sends_abort_user(role):
    build, _, _ = ROLE_CONFIRMING[role]
    s, _ = build()
    actions = s.handle(UserDecision(False), SHOWN_AT + 5)
    assert actions[:-1] == [ABORT_USER]
    assert_closed(actions, Outcome.USER_REJECTED)
    assert_done(s, Outcome.USER_REJECTED)


@by_role
def test_user_rejects_after_peer_confirmed_does_not_save(role):
    build, _, _ = ROLE_CONFIRMING[role]
    s, _ = build()
    s.handle(MessageReceived(PairConfirm()), SHOWN_AT + 1)
    actions = s.handle(UserDecision(False), SHOWN_AT + 2)
    assert actions[:-1] == [ABORT_USER]
    assert_closed(actions, Outcome.USER_REJECTED)


@by_role
@pytest.mark.parametrize("accepted", [True, False])
def test_second_user_decision_after_confirm_is_ignored(role, accepted):
    build, _, _ = ROLE_CONFIRMING[role]
    s, _ = build()
    s.handle(UserDecision(True), SHOWN_AT + 1)
    assert s.handle(UserDecision(accepted), SHOWN_AT + 2) == []
    assert s.local_confirmed is True
    assert s.state == State.CONFIRMING


@by_role
def test_60s_without_user_decision_times_out(role):
    build, _, _ = ROLE_CONFIRMING[role]
    s, _ = build()
    assert s.handle(Tick(), SHOWN_AT + 59.999) == []
    actions = s.handle(Tick(), SHOWN_AT + 60)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)


@by_role
def test_peer_confirmed_does_not_extend_user_deadline(role):
    build, _, _ = ROLE_CONFIRMING[role]
    s, _ = build()
    s.handle(MessageReceived(PairConfirm()), SHOWN_AT + 10)
    actions = s.handle(UserDecision(True), SHOWN_AT + 60)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)


@by_role
def test_user_confirm_just_after_60s_is_timeout(role):
    build, _, _ = ROLE_CONFIRMING[role]
    s, _ = build()
    actions = s.handle(UserDecision(True), SHOWN_AT + 60.1)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)
    assert s.local_confirmed is False


@by_role
def test_grace_period_accepts_late_peer_confirm(role):
    build, peer, name = ROLE_CONFIRMING[role]
    s, _ = build()
    s.handle(UserDecision(True), SHOWN_AT + 55)
    assert s.handle(Tick(), SHOWN_AT + 65) == []
    actions = s.handle(MessageReceived(PairConfirm()), SHOWN_AT + 69.9)
    assert actions[:-1] == [SavePeer(peer, name)]
    assert_closed(actions, Outcome.PAIRED)


@by_role
def test_grace_period_ends_at_70s(role):
    build, _, _ = ROLE_CONFIRMING[role]
    s, _ = build()
    s.handle(UserDecision(True), SHOWN_AT + 1)
    assert s.handle(Tick(), SHOWN_AT + 69.999) == []
    actions = s.handle(MessageReceived(PairConfirm()), SHOWN_AT + 70)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)
    assert not any(isinstance(a, SavePeer) for a in actions)


@by_role
def test_cancel_after_local_confirm_sends_abort_user(role):
    # "muốn hủy thì app gửi Cancel(USER)".
    build, _, _ = ROLE_CONFIRMING[role]
    s, _ = build()
    s.handle(UserDecision(True), SHOWN_AT + 1)
    actions = s.handle(Cancel(), SHOWN_AT + 2)
    assert actions[:-1] == [ABORT_USER]
    assert_closed(actions, Outcome.CANCELLED)


@by_role
def test_connection_lost_between_confirms_does_not_save(role):
    build, _, _ = ROLE_CONFIRMING[role]
    s, _ = build()
    s.handle(UserDecision(True), SHOWN_AT + 1)
    actions = s.handle(ConnectionLost(), SHOWN_AT + 2)
    assert len(actions) == 1
    assert_closed(actions, Outcome.CONNECTION_LOST)


# --- Bảng 2.8: lý do hủy gửi đi -----------------------------------------------------------------


def _reject(s, now):
    return s.handle(UserDecision(False), now + 1)


def _cancel_user(s, now):
    return s.handle(Cancel(AbortReason.USER), now + 1)


def _cancel_busy(s, now):
    return s.handle(Cancel(AbortReason.BUSY), now + 1)


def _wait_out(s, now):
    return s.handle(Tick(), s.deadline)


def _bad(s, now):
    return s.handle(BadMessage(), now + 1)


ABORT_REASON_CASES = [
    pytest.param(init_confirming, _reject, "user", id="user-decision-false"),
    pytest.param(resp_await, _cancel_user, "user", id="cancel-user"),
    pytest.param(init_await, _cancel_busy, "busy", id="initiator-yields-busy"),
    pytest.param(init_await, _wait_out, "timeout", id="no-challenge-10s"),
    pytest.param(resp_await, _wait_out, "timeout", id="no-reveal-10s"),
    pytest.param(resp_confirming, _wait_out, "timeout", id="confirm-60s"),
    pytest.param(init_local_confirmed, _wait_out, "timeout", id="confirm-grace-70s"),
    pytest.param(init_confirming, _bad, "invalid", id="bad-message"),
]


@pytest.mark.parametrize(("build", "trigger", "reason"), ABORT_REASON_CASES)
def test_abort_reason_table(build, trigger, reason):
    s, now = build()
    actions = trigger(s, now)
    sent = [a.message for a in actions if isinstance(a, SendMessage)]
    assert sent == [PairAbort(reason)]
    assert isinstance(actions[-1], Close)


# --- Thuộc tính (a): hai phiên nối với nhau qua kênh giả ----------------------------------------

hex_id = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)
hex_fp = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
nonces = st.binary(min_size=NONCE_SIZE, max_size=NONCE_SIZE)
names = st.sampled_from(["Máy của An", "Laptop Bình", "PC", "🚀 Rocket", "a" * 64, "Nhà‍Cộng"])


def _deliver(actions, outbox, record):
    for act in actions:
        if isinstance(act, SendMessage):
            outbox.append(act.message)
        else:
            record.append(act)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    data=st.data(),
    id_a=hex_id,
    id_b=hex_id,
    fp_a=hex_fp,
    fp_b=hex_fp,
    n_a=nonces,
    n_b=nonces,
    name_a=names,
    name_b=names,
    repair=st.booleans(),
)
def test_property_two_sessions_pair_in_any_order(
    data, id_a, id_b, fp_a, fp_b, n_a, n_b, name_a, name_b, repair
):
    assume(id_a != id_b and fp_a != fp_b)
    cert_a = DeviceCert(device_id=id_a, fingerprint=fp_a, pem=fake_pem("A"))
    cert_b = DeviceCert(device_id=id_b, fingerprint=fp_b, pem=fake_pem("B"))
    hello_a = make_hello(id_a, name_a)
    hello_b = make_hello(id_b, name_b)

    now = 0.0
    a = InitiatorSession(
        local=cert_a, peer=cert_b, peer_hello=hello_b, nonce=n_a, known_fingerprint=fp_b if repair else None
    )
    to_b: list = []
    to_a: list = []
    rec_a: list = []
    rec_b: list = []
    _deliver(a.handle(Start(), now), to_b, rec_a)
    assert len(to_b) == 1
    assert isinstance(to_b[0], PairRequest)
    request = to_b.pop(0)
    assert request.cert == cert_a.pem
    # net đọc pair_request, gate ACCEPT, app tạo phiên trả lời với chứng chỉ phân tích từ request.cert.
    b = ResponderSession(
        local=cert_b,
        peer=cert_a,
        peer_hello=hello_a,
        request=request,
        nonce=n_b,
        known_fingerprint=fp_a if repair else None,
    )
    _deliver(b.handle(Start(), now), to_a, rec_b)

    user_pending = {"A": True, "B": True}
    sessions = {"A": (a, to_b, rec_a), "B": (b, to_a, rec_b)}
    for _ in range(50):
        choices = []
        if to_a:
            choices.append("deliver_to_A")
        if to_b:
            choices.append("deliver_to_B")
        for side in ("A", "B"):
            s = sessions[side][0]
            if user_pending[side] and s.state == State.CONFIRMING:
                choices.append(f"user_{side}")
        if not choices:
            break
        choice = data.draw(st.sampled_from(choices))
        now += data.draw(st.floats(min_value=0.0, max_value=3.0, allow_nan=False))
        if choice.startswith("user_"):
            side = choice[-1]
            user_pending[side] = False
            s, outbox, rec = sessions[side]
            _deliver(s.handle(UserDecision(True), now), outbox, rec)
        else:
            side = choice[-1]
            inbox = to_a if side == "A" else to_b
            s, outbox, rec = sessions[side]
            _deliver(s.handle(MessageReceived(inbox.pop(0)), now), outbox, rec)

    expected_code = pairing_code(fp_a, fp_b, n_a, n_b)
    assert not to_a
    assert not to_b
    assert rec_a == [ShowCode(expected_code), SavePeer(cert_b, name_b), rec_a[-1]]
    assert rec_b == [ShowCode(expected_code), SavePeer(cert_a, name_a), rec_b[-1]]
    assert_closed(rec_a, Outcome.PAIRED)
    assert_closed(rec_b, Outcome.PAIRED)
    assert_done(a, Outcome.PAIRED)
    assert_done(b, Outcome.PAIRED)
    assert a.code == b.code == expected_code


# --- Thuộc tính (b): chuỗi sự kiện ngẫu nhiên giữ mọi bảo đảm mục 2.4 ---------------------------

ALLOWED_SENDS = {
    Role.INITIATOR: (PairRequest, PairReveal, PairConfirm, PairAbort),
    Role.RESPONDER: (PairChallenge, PairConfirm, PairAbort),
}
SENT_REASONS = {"user", "busy", "timeout", "invalid"}

FUZZ_MESSAGES = [
    HELLO_A,
    HELLO_B,
    REQUEST_A,
    OTHER_REQUEST,
    PairChallenge(N_B.hex()),
    PairChallenge(N_OTHER.hex()),
    PairReveal(N_A.hex()),
    PairReveal(N_A.hex()),
    PairReveal(N_OTHER.hex()),
    PairConfirm(),
    PairConfirm(),
    PairConfirm(),
    PairAbort("user"),
    PairAbort("busy"),
    PairAbort("timeout"),
    PairAbort("invalid"),
    PairAbort("from_newer_version"),
]
message_events = st.sampled_from(FUZZ_MESSAGES).map(MessageReceived)
other_events = st.one_of(
    st.just(BadMessage("khung hỏng")),
    st.just(Tick()),
    st.builds(UserDecision, st.booleans()),
    st.just(ConnectionLost()),
    st.sampled_from(["user", "busy"]).map(Cancel),
)
# Message chiếm phần lớn để thường đi được tới CONFIRMING; UserDecision(True) thêm trọng số.
fuzz_events = st.one_of(
    message_events, message_events, message_events, st.just(UserDecision(True)), other_events
)
deltas = st.one_of(
    st.just(0.0),
    st.floats(min_value=0.0, max_value=3.0, allow_nan=False),
    st.sampled_from([9.99, 10.0, 59.9, 60.0, 70.0]),
)
first_events = st.sampled_from(
    [Start(), Start(), Start(), Start(), Start(), Cancel("user"), Cancel("busy"), ConnectionLost()]
)
VARIANTS = ["normal", "normal", "normal", "known_same", "known_conflict", "cn_mismatch", "self", "peer_none"]


def build_fuzz_session(role: Role, variant: str):
    """Trả về (phiên, chứng chỉ máy kia mong đợi trong SavePeer, tên máy kia)."""
    if role == Role.INITIATOR:
        kw: dict = {}
        if variant == "known_same":
            kw["known_fingerprint"] = CERT_B.fingerprint
        elif variant == "known_conflict":
            kw["known_fingerprint"] = CERT_C.fingerprint
        elif variant == "cn_mismatch":
            kw["peer_hello"] = make_hello(CERT_C.device_id, "Kẻ giả")
        elif variant == "self":
            kw["peer"] = make_cert("B", fingerprint=CERT_A.fingerprint)
            kw["peer_hello"] = make_hello(kw["peer"].device_id, "Gương")
        return make_initiator(**kw), CERT_B, HELLO_B.name
    kw = {}
    if variant == "known_same":
        kw["known_fingerprint"] = CERT_A.fingerprint
    elif variant == "known_conflict":
        kw["known_fingerprint"] = CERT_C.fingerprint
    elif variant == "cn_mismatch":
        kw["peer_hello"] = make_hello(CERT_C.device_id, "Kẻ giả")
    elif variant == "self":
        kw["peer"] = make_cert("A", fingerprint=CERT_B.fingerprint)
        kw["peer_hello"] = make_hello(kw["peer"].device_id, "Gương")
    elif variant == "peer_none":
        kw["peer"] = None
    return make_responder(**kw), CERT_A, HELLO_A.name


def check_event_sequence(session, role: Role, expected_peer, expected_name, events) -> None:
    now = 0.0
    closed = False
    shown = 0
    saved = 0
    sent_confirm = False
    peer_confirm_accepted = False
    for event, delta in events:
        now += delta
        before_state = session.state
        before_deadline = session.deadline
        out = session.handle(event, now)
        assert isinstance(out, list)
        if closed:
            assert out == [], "sau Close mọi sự kiện phải cho []"
            assert session.state == State.DONE
            continue

        active = before_state not in (State.IDLE, State.DONE)
        timed_out = active and now >= before_deadline
        is_msg = isinstance(event, MessageReceived)

        # Quy tắc chung khi đang chạy.
        if timed_out:
            assert out[:-1] == [ABORT_TIMEOUT]
            assert_closed(out, Outcome.TIMEOUT)
        elif active and is_msg and isinstance(event.message, PairAbort):
            r = event.message.reason
            assert len(out) == 1
            assert_closed(out, PEER_ABORT_MAP.get(r, Outcome.PEER_ABORTED), peer_reason=r)
        elif active and (isinstance(event, BadMessage) or (is_msg and isinstance(event.message, Hello))):
            assert out[:-1] == [ABORT_INVALID]
            assert_closed(out, Outcome.PROTOCOL_ERROR)
        elif active and isinstance(event, ConnectionLost):
            assert len(out) == 1
            assert_closed(out, Outcome.CONNECTION_LOST)
        elif active and isinstance(event, Cancel):
            assert out[:-1] == [SendMessage(PairAbort(event.reason))]
            assert_closed(out, Outcome.CANCELLED)
        elif active and isinstance(event, Tick):
            assert out == []

        close_idx = [i for i, a in enumerate(out) if isinstance(a, Close)]
        assert len(close_idx) <= 1, "Close nhiều lần trong một lời gọi"
        if close_idx:
            assert close_idx[0] == len(out) - 1, "Close phải là phần tử cuối"

        confirm_now = is_msg and isinstance(event.message, PairConfirm) and not timed_out
        if confirm_now and (out == [] or (close_idx and out[-1].outcome == Outcome.PAIRED)):
            peer_confirm_accepted = True

        for i, act in enumerate(out):
            assert isinstance(act, SendMessage | ShowCode | SavePeer | Close)
            if isinstance(act, SendMessage):
                assert isinstance(act.message, ALLOWED_SENDS[role]), f"{role} không được gửi {act.message!r}"
                if isinstance(act.message, PairAbort):
                    assert act.message.reason in SENT_REASONS
                    assert close_idx, "gửi pair_abort mà không đóng phiên"
                if isinstance(act.message, PairConfirm):
                    assert not sent_confirm, "gửi pair_confirm hai lần"
                    sent_confirm = True
            elif isinstance(act, ShowCode):
                shown += 1
                assert shown == 1, "ShowCode nhiều hơn một lần"
                assert len(act.code) == 6
                assert act.code.isascii()
                assert act.code.isdigit()
                assert session.code == act.code
            elif isinstance(act, SavePeer):
                saved += 1
                assert saved == 1
                assert sent_confirm, "SavePeer khi chưa gửi pair_confirm"
                assert peer_confirm_accepted, "SavePeer khi chưa nhận pair_confirm"
                assert i == len(out) - 2
                assert out[-1].outcome == Outcome.PAIRED
                assert act.peer == expected_peer
                assert act.name == expected_name

        if close_idx:
            closed = True
            close = out[-1]
            assert session.state == State.DONE
            assert session.outcome == close.outcome
            assert session.deadline is None
            if close.outcome == Outcome.PAIRED:
                assert saved == 1
                assert shown == 1
            else:
                assert saved == 0
            if close.peer_reason is not None:
                assert is_msg
                assert isinstance(event.message, PairAbort)
                assert event.message.reason == close.peer_reason
        else:
            assert session.state != State.DONE
            assert session.outcome is None
            if session.state != State.IDLE:
                assert session.deadline is not None
                assert session.deadline > now
    # Thống kê để thấy fuzz thật sự đi tới các kết cục (xem bằng --hypothesis-show-statistics).
    hypothesis.event(f"outcome={session.outcome}")


@pytest.mark.parametrize("role", [Role.INITIATOR, Role.RESPONDER])
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    variant=st.sampled_from(VARIANTS),
    first=first_events,
    steps=st.lists(st.tuples(fuzz_events, deltas), max_size=25),
    first_delta=st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
)
def test_property_random_events_keep_guarantees(role, variant, first, steps, first_delta):
    session, peer, name = build_fuzz_session(role, variant)
    check_event_sequence(session, role, peer, name, [(first, first_delta), *steps])


# Biến thể "đường đúng có nhiễu": mỗi bước hoặc là bước kế tiếp của đường đúng, hoặc là sự kiện
# ngẫu nhiên. Chuỗi thuần ngẫu nhiên hiếm khi đi tới PAIRED, nên cần biến thể này để các bảo đảm
# về SavePeer/Close(PAIRED) thật sự được kiểm.
HAPPY = "happy"
small_deltas = st.one_of(st.just(0.0), st.floats(min_value=0.0, max_value=2.0, allow_nan=False))


def happy_path(role: Role, peer_first: bool) -> list:
    first = (
        MessageReceived(PairChallenge(N_B.hex()))
        if role == Role.INITIATOR
        else MessageReceived(PairReveal(N_A.hex()))
    )
    confirms = [UserDecision(True), MessageReceived(PairConfirm())]
    if peer_first:
        confirms.reverse()
    return [first, *confirms]


@pytest.mark.parametrize("role", [Role.INITIATOR, Role.RESPONDER])
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    variant=st.sampled_from(["normal", "normal", "normal", "known_same", "known_conflict"]),
    peer_first=st.booleans(),
    steps=st.lists(
        # Mỗi bước: khoảng 1/8 là nhiễu, còn lại đi tiếp đường đúng.
        st.tuples(st.sampled_from([HAPPY] * 7 + ["noise"]), fuzz_events, small_deltas),
        min_size=3,
        max_size=12,
    ),
)
def test_property_noisy_happy_path_keeps_guarantees(role, variant, peer_first, steps):
    session, peer, name = build_fuzz_session(role, variant)
    path = iter(happy_path(role, peer_first))
    events = [(Start(), 0.0)]
    for kind, noise, delta in steps:
        events.append((next(path, Tick()) if kind == HAPPY else noise, delta))
    check_event_sequence(session, role, peer, name, events)


# --- Thuộc tính (c): MITM tiết lộ nonce khác nonce đã cam kết ----------------------------------


@settings(max_examples=300, deadline=None)
@given(n_a=nonces, n_b=nonces, revealed=nonces, swap_fp=st.booleans(), delay=st.floats(0, 9.99))
def test_property_reveal_not_matching_commitment_is_protocol_error(n_a, n_b, revealed, swap_fp, delay):
    if swap_fp:
        # Kẻ đứng giữa thay chứng chỉ: cam kết làm với fingerprint của nó, nonce tiết lộ đúng.
        commitment = make_commitment(CERT_C.fingerprint, n_a)
        revealed = n_a
    else:
        assume(revealed != n_a)
        commitment = make_commitment(CERT_A.fingerprint, n_a)
    s = make_responder(request=PairRequest(cert=CERT_A.pem, commitment=commitment), nonce=n_b)
    assert s.handle(Start(), T0) == [SendMessage(PairChallenge(n_b.hex()))]
    actions = s.handle(MessageReceived(PairReveal(revealed.hex())), T0 + delay)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)
    assert not any(isinstance(a, ShowCode) for a in actions)
    assert s.code is None
    assert_done(s, Outcome.PROTOCOL_ERROR)
