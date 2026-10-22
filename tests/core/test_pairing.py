"""Test máy trạng thái ghép đôi `teleport.core.pairing` (kế hoạch PR 1, mục 2.3–2.8).

Viết từ đặc tả (kế hoạch + docs/protocol.md §5), không từ code. Chứng chỉ ở đây là PEM giả:
`core` coi PEM là chuỗi mờ, chỉ cần bắt đầu bằng CERT_PEM_HEADER và không quá MAX_CERT_PEM_LEN.

Luồng (bên trả lời cam kết trước, kiểu Bluetooth), A = khởi xướng, B = trả lời:
    A → B  pair_request{cert}
    B → A  pair_commit{commitment = C_B}
    A → B  pair_nonce{nonce = n_A}
    B: hiện mã, rồi gửi pair_reveal{nonce = n_B}
    A: kiểm C_B bằng fp_B; sai → pair_abort{invalid}, không hiện mã; đúng → hiện mã.
"""

from __future__ import annotations

import ast
import hashlib
import json
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
    ControlOpen,
    Hello,
    PairAbort,
    PairCommit,
    PairConfirm,
    PairNonce,
    PairRequest,
    PairReveal,
    Ping,
    Pong,
    to_dict,
)
from teleport.core.pairing import (
    CONFIRM_GRACE,
    CONFIRM_TIMEOUT,
    MESSAGE_TIMEOUT,
    NONCE_TIMEOUT,
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
from teleport.core.pairing_code import NONCE_SIZE, check_commitment, make_commitment, pairing_code

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
REQUEST_A = PairRequest(cert=CERT_A.pem)
COMMIT_B = PairCommit(make_commitment(CERT_B.fingerprint, N_B))
NONCE_A = PairNonce(N_A.hex())
REVEAL_B = PairReveal(N_B.hex())
CODE_AB = pairing_code(CERT_A.fingerprint, CERT_B.fingerprint, N_A, N_B)

T0 = 100.0  # thời điểm Start
COMMIT_AT = T0 + 0.5  # thời điểm A nhận pair_commit trong các builder bên dưới
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
    nonce: bytes = N_B,
    known_fingerprint: str | None = None,
) -> ResponderSession:
    return ResponderSession(
        local=local,
        peer=peer,
        peer_hello=peer_hello,
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


def sent_text(actions: list) -> str:
    """Toàn bộ nội dung các message sẽ gửi đi, dạng JSON trên dây (để tìm nonce bị lộ)."""
    return json.dumps([to_dict(a.message) for a in actions if isinstance(a, SendMessage)])


# Builder: trả về (phiên, thời điểm của lời gọi cuối) ở từng trạng thái đang chạy.


def init_await_commit():
    s = make_initiator()
    s.handle(Start(), T0)
    return s, T0


def init_await_reveal():
    s, _ = init_await_commit()
    s.handle(MessageReceived(COMMIT_B), COMMIT_AT)
    return s, COMMIT_AT


def init_confirming():
    s, _ = init_await_reveal()
    s.handle(MessageReceived(REVEAL_B), SHOWN_AT)
    return s, SHOWN_AT


def init_local_confirmed():
    s, _ = init_confirming()
    s.handle(UserDecision(True), SHOWN_AT + 5)
    return s, SHOWN_AT + 5


def init_peer_confirmed():
    s, _ = init_confirming()
    s.handle(MessageReceived(PairConfirm()), SHOWN_AT + 5)
    return s, SHOWN_AT + 5


def resp_await_nonce():
    s = make_responder()
    s.handle(Start(), T0)
    return s, T0


def resp_confirming():
    s, _ = resp_await_nonce()
    s.handle(MessageReceived(NONCE_A), SHOWN_AT)
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
    "init-await_commit": init_await_commit,
    "init-await_reveal": init_await_reveal,
    "init-confirming": init_confirming,
    "init-local_confirmed": init_local_confirmed,
    "init-peer_confirmed": init_peer_confirmed,
    "resp-await_nonce": resp_await_nonce,
    "resp-confirming": resp_confirming,
    "resp-local_confirmed": resp_local_confirmed,
    "resp-peer_confirmed": resp_peer_confirmed,
}
# Trạng thái và deadline mong đợi ở từng builder (mục 2.5–2.7).
EXPECTED_STATE = {
    "init-await_commit": State.AWAIT_COMMIT,
    "init-await_reveal": State.AWAIT_REVEAL,
    "init-confirming": State.CONFIRMING,
    "init-local_confirmed": State.CONFIRMING,
    "init-peer_confirmed": State.CONFIRMING,
    "resp-await_nonce": State.AWAIT_NONCE,
    "resp-confirming": State.CONFIRMING,
    "resp-local_confirmed": State.CONFIRMING,
    "resp-peer_confirmed": State.CONFIRMING,
}
EXPECTED_DEADLINE = {
    "init-await_commit": T0 + 10,
    "init-await_reveal": COMMIT_AT + 10,
    "init-confirming": SHOWN_AT + 60,
    "init-local_confirmed": SHOWN_AT + 70,
    "init-peer_confirmed": SHOWN_AT + 60,
    "resp-await_nonce": T0 + NONCE_TIMEOUT,  # hạn riêng chờ pair_nonce, ngắn hơn MESSAGE_TIMEOUT
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
    s, now = init_await_commit()
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


def done_initiator_bad_reveal():
    s, now = init_await_reveal()
    s.handle(MessageReceived(PairReveal(N_OTHER.hex())), now + 1)
    return s, now + 1


DONE_BUILDERS = {
    "init-paired": done_initiator_paired,
    "init-cancelled": done_initiator_cancelled,
    "resp-user_rejected": done_responder_rejected,
    "init-cancel_at_idle": done_initiator_idle_cancel,
    "init-bad_reveal": done_initiator_bad_reveal,
}
done_builders = pytest.mark.parametrize("build", list(DONE_BUILDERS.values()), ids=list(DONE_BUILDERS))


PAIRING_FLOW_MESSAGES = [REQUEST_A, COMMIT_B, NONCE_A, REVEAL_B]

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
    *(MessageReceived(m) for m in PAIRING_FLOW_MESSAGES),
    MessageReceived(HELLO_A),
]


# --- Hằng số, kiểu ------------------------------------------------------------------------------


def test_timeout_constants_match_protocol():
    assert MESSAGE_TIMEOUT == 10.0
    assert NONCE_TIMEOUT == 5.0
    assert NONCE_TIMEOUT < MESSAGE_TIMEOUT
    assert CONFIRM_TIMEOUT == 60.0
    assert CONFIRM_GRACE == 10.0


def test_enum_values():
    assert Role.INITIATOR == "initiator"
    assert Role.RESPONDER == "responder"
    assert {s.value for s in State} == {
        "idle",
        "await_commit",
        "await_nonce",
        "await_reveal",
        "confirming",
        "done",
    }
    assert State.AWAIT_COMMIT == "await_commit"
    assert State.AWAIT_NONCE == "await_nonce"
    assert State.AWAIT_REVEAL == "await_reveal"
    assert not hasattr(State, "AWAIT_CHALLENGE")
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
BAD_HELLOS = [None, {"type": "hello"}, COMMIT_B, "hello"]
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


def test_responder_no_longer_takes_request():
    # pair_request không còn cam kết nên phiên trả lời không cần nó: `request=` là tham số lạ.
    with pytest.raises(TypeError):
        ResponderSession(  # type: ignore[call-arg]
            local=CERT_B, peer=CERT_A, peer_hello=HELLO_A, request=REQUEST_A, nonce=N_B
        )


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
        ResponderSession(CERT_B, CERT_A, HELLO_A, N_B)  # type: ignore[misc]


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
    *(MessageReceived(m) for m in PAIRING_FLOW_MESSAGES),
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
    s, now = init_await_commit()
    # Start chỉ hợp lệ ở IDLE (R1/R2): đang chạy mà Start lại là lỗi lập trình của app.
    with pytest.raises(PairingUsageError):
        s.handle(Start(), now)


# --- Bảng 2.5: bên khởi xướng ------------------------------------------------------------------


def test_initiator_start_sends_pair_request_with_cert_only():
    s = make_initiator()
    actions = s.handle(Start(), T0)
    assert actions == [SendMessage(PairRequest(cert=CERT_A.pem))]
    # Trên dây chỉ có cert: không nonce, không commitment.
    assert to_dict(actions[0].message) == {"type": "pair_request", "cert": CERT_A.pem}
    assert s.state == State.AWAIT_COMMIT
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
    assert s.state == State.AWAIT_COMMIT


def test_initiator_commit_sends_nonce_and_waits_for_reveal():
    s, _ = init_await_commit()
    actions = s.handle(MessageReceived(COMMIT_B), T0 + 3)
    assert actions == [SendMessage(PairNonce(N_A.hex()))]
    assert s.state == State.AWAIT_REVEAL
    assert s.deadline == T0 + 3 + 10
    assert s.code is None, "chưa có n_B thì chưa có mã"


def test_initiator_sends_nonce_for_any_well_formed_commitment():
    # A không kiểm được cam kết lúc nhận (chưa có n_B): commitment nào đúng dạng cũng cho đi tiếp.
    s, _ = init_await_commit()
    actions = s.handle(MessageReceived(PairCommit("0" * 64)), T0 + 1)
    assert actions == [SendMessage(NONCE_A)]
    assert s.state == State.AWAIT_REVEAL


def test_initiator_matching_reveal_shows_code():
    s, _ = init_await_reveal()
    actions = s.handle(MessageReceived(REVEAL_B), COMMIT_AT + 2)
    assert actions == [ShowCode(CODE_AB)]
    assert s.state == State.CONFIRMING
    assert s.code == CODE_AB
    assert s.deadline == COMMIT_AT + 2 + 60
    assert s.local_confirmed is False
    assert s.peer_confirmed is False


def test_initiator_code_uses_initiator_fingerprint_first():
    s, _ = init_await_reveal()
    actions = s.handle(MessageReceived(REVEAL_B), COMMIT_AT + 1)
    code = actions[0].code
    assert code == pairing_code(CERT_A.fingerprint, CERT_B.fingerprint, N_A, N_B)
    assert len(code) == 6
    assert code.isascii()
    assert code.isdigit()


@pytest.mark.parametrize(
    ("label", "commitment", "revealed"),
    [
        ("n_B khác cam kết", make_commitment(CERT_B.fingerprint, N_B), N_OTHER),
        ("n_B = n_A", make_commitment(CERT_B.fingerprint, N_B), N_A),
        ("cam kết bằng fp khởi xướng", make_commitment(CERT_A.fingerprint, N_B), N_B),
        ("cam kết bằng fp máy thứ ba", make_commitment(CERT_C.fingerprint, N_B), N_B),
        ("cam kết rác", "0" * 64, N_B),
    ],
)
def test_initiator_reveal_not_matching_commitment_aborts_without_code(label, commitment, revealed):
    s, _ = init_await_commit()
    s.handle(MessageReceived(PairCommit(commitment)), T0 + 1)
    actions = s.handle(MessageReceived(PairReveal(revealed.hex())), T0 + 2)
    assert actions[:-1] == [ABORT_INVALID], label
    assert_closed(actions, Outcome.PROTOCOL_ERROR)
    assert not any(isinstance(a, ShowCode) for a in actions)
    assert s.code is None
    assert_done(s, Outcome.PROTOCOL_ERROR)


def test_initiator_times_out_after_10s_without_commit():
    s, _ = init_await_commit()
    assert s.handle(Tick(), T0 + 9.999) == []
    actions = s.handle(Tick(), T0 + 10)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)
    assert_done(s, Outcome.TIMEOUT)


def test_initiator_times_out_10s_after_commit_without_reveal():
    s, _ = init_await_reveal()
    # Hạn chót tính lại từ lúc nhận pair_commit, không phải từ Start.
    assert s.handle(Tick(), T0 + 10) == []
    assert s.handle(Tick(), COMMIT_AT + 9.999) == []
    actions = s.handle(Tick(), COMMIT_AT + 10)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)
    assert s.code is None


def test_initiator_commit_at_deadline_is_timeout_and_nonce_not_sent():
    s, _ = init_await_commit()
    actions = s.handle(MessageReceived(COMMIT_B), T0 + 10)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)
    assert N_A.hex() not in sent_text(actions)


def test_initiator_reveal_at_deadline_is_timeout_not_code():
    s, _ = init_await_reveal()
    actions = s.handle(MessageReceived(REVEAL_B), COMMIT_AT + 10)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)
    assert s.code is None


# --- Bảng 2.6: bên trả lời ---------------------------------------------------------------------


def test_responder_start_sends_commitment_of_own_fingerprint_and_nonce():
    s = make_responder()
    actions = s.handle(Start(), T0)
    assert actions == [SendMessage(PairCommit(make_commitment(CERT_B.fingerprint, N_B)))]
    assert to_dict(actions[0].message) == {
        "type": "pair_commit",
        "commitment": make_commitment(CERT_B.fingerprint, N_B),
    }
    # Cam kết gắn với fp bên trả lời, không phải fp bên khởi xướng; n_B chưa lộ.
    assert actions[0].message.commitment != make_commitment(CERT_A.fingerprint, N_B)
    assert N_B.hex() not in sent_text(actions)
    assert s.state == State.AWAIT_NONCE
    assert s.deadline == T0 + NONCE_TIMEOUT
    assert s.code is None


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
    s = make_responder(peer=peer, peer_hello=make_hello(peer.device_id, "Gương"))
    actions = s.handle(Start(), T0)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)


def test_responder_start_rejects_same_device_id():
    peer = make_cert("A", device_id=CERT_B.device_id)
    s = make_responder(peer=peer, peer_hello=make_hello(CERT_B.device_id, "Gương"))
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
    assert s.handle(Start(), T0) == [SendMessage(COMMIT_B)]


def test_responder_nonce_shows_code_then_reveals():
    s, _ = resp_await_nonce()
    actions = s.handle(MessageReceived(NONCE_A), T0 + 2)
    # Đúng thứ tự: hiện mã TRƯỚC, rồi mới tiết lộ n_B.
    assert actions == [ShowCode(CODE_AB), SendMessage(PairReveal(N_B.hex()))]
    assert s.state == State.CONFIRMING
    assert s.code == CODE_AB
    assert s.deadline == T0 + 2 + 60


def test_responder_accepts_any_well_formed_n_a():
    # B không kiểm được n_A; mã đổi theo n_A, người dùng so mã sẽ bắt được kẻ đứng giữa.
    s, _ = resp_await_nonce()
    actions = s.handle(MessageReceived(PairNonce(N_OTHER.hex())), T0 + 1)
    expected = pairing_code(CERT_A.fingerprint, CERT_B.fingerprint, N_OTHER, N_B)
    assert actions == [ShowCode(expected), SendMessage(REVEAL_B)]


def test_both_roles_compute_same_code():
    a, _ = init_confirming()
    b, _ = resp_confirming()
    assert a.code == b.code == CODE_AB


def test_responder_times_out_after_nonce_timeout_without_nonce():
    s, _ = resp_await_nonce()
    assert s.handle(Tick(), T0 + NONCE_TIMEOUT - 0.001) == []
    actions = s.handle(Tick(), T0 + NONCE_TIMEOUT)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)
    assert N_B.hex() not in sent_text(actions)


def test_responder_nonce_at_deadline_is_timeout_and_n_b_stays_secret():
    s, _ = resp_await_nonce()
    actions = s.handle(MessageReceived(NONCE_A), T0 + NONCE_TIMEOUT)
    assert actions[:-1] == [ABORT_TIMEOUT]
    assert_closed(actions, Outcome.TIMEOUT)
    assert s.code is None
    assert not any(isinstance(a, ShowCode) for a in actions)
    assert N_B.hex() not in sent_text(actions)


# --- Hạn riêng NONCE_TIMEOUT: kẻ lạ nhận pair_commit rồi im lặng không giữ gate lâu -------------
#
# Bên trả lời chờ pair_nonce tối đa NONCE_TIMEOUT kể từ Start (lúc gửi pair_commit), ngắn hơn
# MESSAGE_TIMEOUT (10 s) vẫn áp cho bên khởi xướng. Hạn tính từ mốc cố định; sự kiện không đi tiếp
# luồng không gia hạn. Hằng số là biến module đọc lúc dùng (monkeypatch được).

EPS = 0.001


def test_responder_nonce_just_before_nonce_timeout_shows_code_then_reveals():
    s, _ = resp_await_nonce()
    assert s.deadline == T0 + NONCE_TIMEOUT
    assert s.handle(Tick(), T0 + NONCE_TIMEOUT - EPS) == []
    actions = s.handle(MessageReceived(NONCE_A), T0 + NONCE_TIMEOUT - EPS)
    assert actions == [ShowCode(CODE_AB), SendMessage(REVEAL_B)]
    assert s.state == State.CONFIRMING


def test_responder_tick_at_nonce_timeout_sends_abort_timeout_and_closes():
    s, _ = resp_await_nonce()
    actions = s.handle(Tick(), T0 + NONCE_TIMEOUT)
    # Đúng hai hành động: SendMessage(pair_abort{timeout}) rồi Close(TIMEOUT) (detail tự do).
    assert len(actions) == 2
    assert actions[0] == ABORT_TIMEOUT
    assert_closed(actions, Outcome.TIMEOUT)
    assert_done(s, Outcome.TIMEOUT)
    assert s.code is None
    assert not any(isinstance(a, ShowCode) for a in actions)
    assert N_B.hex() not in sent_text(actions)


def test_responder_nonce_wait_is_not_message_timeout():
    # Hồi quy lỗi cũ: trước đây kẻ lạ giữ slot tới MESSAGE_TIMEOUT. Ở T0 + NONCE_TIMEOUT phiên
    # phải đã hết giờ, không còn chờ tới T0 + MESSAGE_TIMEOUT.
    s, _ = resp_await_nonce()
    assert s.deadline < T0 + MESSAGE_TIMEOUT
    out = s.handle(Tick(), T0 + NONCE_TIMEOUT)
    assert_closed(out, Outcome.TIMEOUT)


def test_responder_ignored_events_do_not_extend_nonce_deadline():
    # Sự kiện không đi tiếp luồng (Tick, UserDecision ngoài lời hỏi) không dời mốc.
    s, _ = resp_await_nonce()
    for t in (T0 + 1, T0 + 2.5, T0 + NONCE_TIMEOUT - EPS):
        assert s.handle(Tick(), t) == []
        assert s.handle(UserDecision(True), t) == []
        assert s.deadline == T0 + NONCE_TIMEOUT
    assert_closed(s.handle(Tick(), T0 + NONCE_TIMEOUT), Outcome.TIMEOUT)


def test_initiator_await_commit_still_uses_message_timeout():
    s, _ = init_await_commit()
    assert s.deadline == T0 + MESSAGE_TIMEOUT
    assert s.handle(Tick(), T0 + NONCE_TIMEOUT) == []
    assert s.state == State.AWAIT_COMMIT
    assert s.handle(Tick(), T0 + MESSAGE_TIMEOUT - EPS) == []
    assert_closed(s.handle(Tick(), T0 + MESSAGE_TIMEOUT), Outcome.TIMEOUT)


def test_initiator_await_reveal_still_uses_message_timeout():
    s, _ = init_await_reveal()
    assert s.deadline == COMMIT_AT + MESSAGE_TIMEOUT
    assert s.handle(Tick(), COMMIT_AT + NONCE_TIMEOUT) == []
    assert s.state == State.AWAIT_REVEAL
    actions = s.handle(MessageReceived(REVEAL_B), COMMIT_AT + MESSAGE_TIMEOUT - EPS)
    assert actions == [ShowCode(CODE_AB)]


def test_nonce_timeout_is_read_at_use_time(monkeypatch):
    monkeypatch.setattr(pairing_mod, "NONCE_TIMEOUT", 2.0)
    s = make_responder()
    s.handle(Start(), T0)
    assert s.deadline == T0 + 2.0
    assert s.handle(Tick(), T0 + 2.0 - EPS) == []
    assert_closed(s.handle(Tick(), T0 + 2.0), Outcome.TIMEOUT)


def test_patching_nonce_timeout_does_not_touch_initiator_deadlines(monkeypatch):
    monkeypatch.setattr(pairing_mod, "NONCE_TIMEOUT", 2.0)
    s, _ = init_await_commit()
    assert s.deadline == T0 + MESSAGE_TIMEOUT
    s.handle(MessageReceived(COMMIT_B), COMMIT_AT)
    assert s.deadline == COMMIT_AT + MESSAGE_TIMEOUT


def test_patching_message_timeout_does_not_touch_responder_nonce_deadline(monkeypatch):
    monkeypatch.setattr(pairing_mod, "MESSAGE_TIMEOUT", 1.0)
    s, _ = resp_await_nonce()
    assert s.deadline == T0 + NONCE_TIMEOUT


# --- R4: message không nằm trong bảng của trạng thái -------------------------------------------

OTHER_REQUEST = PairRequest(cert=CERT_C.pem)
OTHER_COMMIT = PairCommit(make_commitment(CERT_B.fingerprint, N_OTHER))
OUT_OF_ORDER = {
    # A nhận pair_reveal trước pair_commit, hoặc pair_nonce (chỉ A được gửi).
    init_await_commit: [HELLO_B, REQUEST_A, NONCE_A, REVEAL_B, PairConfirm()],
    # pair_commit lần hai, pair_nonce, pair_request giữa phiên.
    init_await_reveal: [HELLO_B, REQUEST_A, COMMIT_B, OTHER_COMMIT, NONCE_A, PairConfirm()],
    init_confirming: [HELLO_B, REQUEST_A, COMMIT_B, NONCE_A, REVEAL_B],
    init_local_confirmed: [HELLO_B, COMMIT_B, NONCE_A, REVEAL_B],
    init_peer_confirmed: [HELLO_B, COMMIT_B, REVEAL_B, PairConfirm()],  # PairConfirm lần hai
    # B nhận pair_commit/pair_reveal (chỉ B được gửi), pair_request giữa phiên.
    resp_await_nonce: [HELLO_A, REQUEST_A, OTHER_REQUEST, COMMIT_B, REVEAL_B, PairConfirm()],
    resp_confirming: [HELLO_A, OTHER_REQUEST, COMMIT_B, NONCE_A, REVEAL_B],  # NONCE_A lần hai
    resp_local_confirmed: [HELLO_A, NONCE_A, COMMIT_B, REVEAL_B],
    resp_peer_confirmed: [HELLO_A, NONCE_A, PairConfirm()],  # PairConfirm lần hai
}
OUT_OF_ORDER_CASES = [
    pytest.param(build, msg, id=f"{build.__name__}-{type(msg).__name__}-{i}")
    for build, msgs in OUT_OF_ORDER.items()
    for i, msg in enumerate(msgs)
]


@pytest.mark.parametrize(("build", "msg"), OUT_OF_ORDER_CASES)
def test_out_of_order_message_aborts_invalid(build, msg):
    s, now = build()
    actions = s.handle(MessageReceived(msg), now + 1)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)
    assert_done(s, Outcome.PROTOCOL_ERROR)
    assert not any(isinstance(a, SavePeer | ShowCode) for a in actions)


def test_responder_second_nonce_does_not_reveal_again():
    s, now = resp_confirming()
    actions = s.handle(MessageReceived(PairNonce(N_OTHER.hex())), now + 1)
    assert actions[:-1] == [ABORT_INVALID]
    assert not any(isinstance(a, SendMessage) and isinstance(a.message, PairReveal) for a in actions)


def test_initiator_reveal_before_commit_never_shows_code():
    s, _ = init_await_commit()
    actions = s.handle(MessageReceived(REVEAL_B), T0 + 1)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)
    assert s.code is None
    assert N_A.hex() not in sent_text(actions)


@active_builders
def test_bad_message_aborts_invalid(build):
    s, now = build()
    actions = s.handle(BadMessage("JSON sai"), now + 1)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)


# Message của kết nối điều khiển (protocol.md §2.1, §3.2) không thuộc kết nối ghép đôi:
# message không phải pair_* giữa phiên → pair_abort{invalid} (kế hoạch PR2).
CONTROL_MESSAGES = [ControlOpen(), Ping(t=1), Pong(t=1)]


@active_builders
@pytest.mark.parametrize("msg", CONTROL_MESSAGES, ids=lambda m: type(m).__name__)
def test_control_channel_message_during_pairing_aborts_invalid(build, msg):
    s, now = build()
    actions = s.handle(MessageReceived(msg), now + 1)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)
    assert_done(s, Outcome.PROTOCOL_ERROR)


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
def test_state_and_deadline_per_builder(name):
    s, _ = ACTIVE_BUILDERS[name]()
    assert s.state == EXPECTED_STATE[name]
    assert s.deadline == EXPECTED_DEADLINE[name]


@active_builders
def test_tick_before_deadline_does_nothing(build):
    s, _ = build()
    state, deadline = s.state, s.deadline
    assert s.handle(Tick(), deadline - 0.001) == []
    assert s.state == state
    assert s.deadline == deadline


@pytest.mark.parametrize(
    "build",
    [init_await_commit, init_await_reveal, resp_await_nonce, init_local_confirmed, resp_local_confirmed],
)
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
    *(MessageReceived(m) for m in PAIRING_FLOW_MESSAGES),
    MessageReceived(HELLO_A),
]


@active_builders
@pytest.mark.parametrize("event", R0_EVENTS, ids=repr)
@pytest.mark.parametrize("late", [0.0, 0.1, 1000.0])
def test_any_event_at_or_after_deadline_is_timeout(build, event, late):
    s, _ = build()
    code_before = s.code
    actions = s.handle(event, s.deadline + late)
    assert actions[:-1] == [ABORT_TIMEOUT], "hết giờ xét trước, sự kiện bị bỏ"
    assert_closed(actions, Outcome.TIMEOUT)
    assert_done(s, Outcome.TIMEOUT)
    assert not any(isinstance(a, SavePeer | ShowCode) for a in actions)
    assert s.code == code_before


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
    assert actions[:-1] == [SavePeer(peer, name, keep_known_name=False)]
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
    assert actions[:-1] == [SendMessage(PairConfirm()), SavePeer(peer, name, keep_known_name=False)]
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
    assert actions[:-1] == [SavePeer(peer, name, keep_known_name=False)]
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


# --- M5: ghép đôi lại ở vai trả lời không đổi tên đã lưu (`SavePeer.keep_known_name`) ------------
#
# Bên trả lời nhận chứng chỉ trong `pair_request` mà chưa có bằng chứng bên kia giữ khóa riêng, nên
# một máy lạ gửi được chứng chỉ công khai của một máy đã tin cậy. Ghép đôi lại ở vai này giữ tên đã
# lưu; vai khởi xướng (đã kiểm khóa qua bắt tay TLS) và thiết bị mới thì dùng tên trong hello.


def test_m5_save_peer_keep_known_name_is_required():
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(SavePeer)}
    assert {"peer", "name", "keep_known_name"} <= set(fields)
    keep = fields["keep_known_name"]
    no_default = keep.default is dataclasses.MISSING and keep.default_factory is dataclasses.MISSING
    assert no_default, "keep_known_name là trường bắt buộc"
    with pytest.raises(TypeError):
        SavePeer(CERT_A, HELLO_A.name)  # type: ignore[call-arg]


def _confirm_both(role: Role, *, known: bool, peer_first: bool) -> list:
    """Đi đường đúng tới khi hai bên xác nhận; trả lô hành động cuối (có SavePeer, Close)."""
    if role == Role.INITIATOR:
        s = make_initiator(known_fingerprint=CERT_B.fingerprint if known else None)
        s.handle(Start(), T0)
        s.handle(MessageReceived(COMMIT_B), COMMIT_AT)
        assert s.handle(MessageReceived(REVEAL_B), SHOWN_AT) == [ShowCode(CODE_AB)]
    else:
        s = make_responder(known_fingerprint=CERT_A.fingerprint if known else None)
        s.handle(Start(), T0)
        assert s.handle(MessageReceived(NONCE_A), SHOWN_AT)[0] == ShowCode(CODE_AB)
    if peer_first:
        assert s.handle(MessageReceived(PairConfirm()), SHOWN_AT + 1) == []
        return s.handle(UserDecision(True), SHOWN_AT + 2)
    assert s.handle(UserDecision(True), SHOWN_AT + 1) == [SendMessage(PairConfirm())]
    return s.handle(MessageReceived(PairConfirm()), SHOWN_AT + 2)


M5_CASES = [
    pytest.param(Role.RESPONDER, True, True, id="responder-ghep-lai"),
    pytest.param(Role.RESPONDER, False, False, id="responder-thiet-bi-moi"),
    pytest.param(Role.INITIATOR, True, False, id="initiator-ghep-lai"),
    pytest.param(Role.INITIATOR, False, False, id="initiator-thiet-bi-moi"),
]


@pytest.mark.parametrize("peer_first", [False, True], ids=["tu-xac-nhan-truoc", "may-kia-truoc"])
@pytest.mark.parametrize(("role", "known", "keep"), M5_CASES)
def test_m5_keep_known_name_only_when_responder_repairs(role, known, keep, peer_first):
    actions = _confirm_both(role, known=known, peer_first=peer_first)
    saves = [a for a in actions if isinstance(a, SavePeer)]
    assert len(saves) == 1
    save = saves[0]
    assert type(save.keep_known_name) is bool
    assert save.keep_known_name == keep
    # SavePeer vẫn mang tên trong hello của máy kia; `app` quyết định giữ tên đã lưu hay không.
    peer, name = (CERT_B, HELLO_B.name) if role == Role.INITIATOR else (CERT_A, HELLO_A.name)
    assert (save.peer, save.name) == (peer, name)
    assert actions[-2] is save
    assert_closed(actions, Outcome.PAIRED)


def test_m5_responder_repair_with_other_name_in_hello_still_keeps_known_name():
    """Máy lạ gửi chứng chỉ công khai của V (đã tin cậy) nhưng tự xưng tên khác: vẫn là ghép đôi
    lại ở vai trả lời, nên SavePeer báo giữ tên đã lưu."""
    s = make_responder(
        peer_hello=make_hello(CERT_A.device_id, "Laptop-Bob"), known_fingerprint=CERT_A.fingerprint
    )
    s.handle(Start(), T0)
    s.handle(MessageReceived(NONCE_A), SHOWN_AT)
    s.handle(UserDecision(True), SHOWN_AT + 1)
    actions = s.handle(MessageReceived(PairConfirm()), SHOWN_AT + 2)
    assert actions[:-1] == [SavePeer(CERT_A, "Laptop-Bob", keep_known_name=True)]
    assert_closed(actions, Outcome.PAIRED)


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


def _bad_reveal(s, now):
    return s.handle(MessageReceived(PairReveal(N_OTHER.hex())), now + 1)


ABORT_REASON_CASES = [
    pytest.param(init_confirming, _reject, "user", id="user-decision-false"),
    pytest.param(resp_await_nonce, _cancel_user, "user", id="cancel-user"),
    pytest.param(init_await_commit, _cancel_busy, "busy", id="initiator-yields-busy"),
    pytest.param(init_await_commit, _wait_out, "timeout", id="no-commit-10s"),
    pytest.param(init_await_reveal, _wait_out, "timeout", id="no-reveal-10s"),
    pytest.param(resp_await_nonce, _wait_out, "timeout", id="no-nonce-5s"),
    pytest.param(resp_confirming, _wait_out, "timeout", id="confirm-60s"),
    pytest.param(init_local_confirmed, _wait_out, "timeout", id="confirm-grace-70s"),
    pytest.param(init_confirming, _bad, "invalid", id="bad-message"),
    pytest.param(init_await_reveal, _bad_reveal, "invalid", id="reveal-not-matching-commitment"),
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
    assert to_dict(request) == {"type": "pair_request", "cert": cert_a.pem}
    # net đọc pair_request, gate ACCEPT, app tạo phiên trả lời với chứng chỉ phân tích từ request.cert.
    b = ResponderSession(
        local=cert_b,
        peer=cert_a,
        peer_hello=hello_a,
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
        # ≤ 2 s mỗi bước: B nhận pair_nonce sau đúng hai bước (commit tới A, nonce tới B), tức
        # ≤ 4 s < NONCE_TIMEOUT; các hạn khác (10 s, 60 s) còn rộng hơn.
        now += data.draw(st.floats(min_value=0.0, max_value=2.0, allow_nan=False))
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
    # M5: chỉ bên trả lời ghép đôi lại mới giữ tên đã lưu; bên khởi xướng luôn cập nhật tên.
    assert rec_a == [ShowCode(expected_code), SavePeer(cert_b, name_b, keep_known_name=False), rec_a[-1]]
    assert rec_b == [ShowCode(expected_code), SavePeer(cert_a, name_a, keep_known_name=repair), rec_b[-1]]
    assert_closed(rec_a, Outcome.PAIRED)
    assert_closed(rec_b, Outcome.PAIRED)
    assert_done(a, Outcome.PAIRED)
    assert_done(b, Outcome.PAIRED)
    assert a.code == b.code == expected_code


# --- Thuộc tính (b): chuỗi sự kiện ngẫu nhiên giữ mọi bảo đảm mục 2.4 ---------------------------

ALLOWED_SENDS = {
    Role.INITIATOR: (PairRequest, PairNonce, PairConfirm, PairAbort),
    Role.RESPONDER: (PairCommit, PairReveal, PairConfirm, PairAbort),
}
SENT_REASONS = {"user", "busy", "timeout", "invalid"}

FUZZ_MESSAGES = [
    HELLO_A,
    HELLO_B,
    REQUEST_A,
    OTHER_REQUEST,
    COMMIT_B,
    COMMIT_B,
    COMMIT_B,
    OTHER_COMMIT,  # cam kết hợp lệ cho N_OTHER: đi cặp với PairReveal(N_OTHER)
    PairCommit(make_commitment(CERT_A.fingerprint, N_B)),  # cam kết bằng fp khởi xướng
    PairCommit(make_commitment(CERT_C.fingerprint, N_B)),  # cam kết bằng fp kẻ giả
    NONCE_A,
    NONCE_A,
    NONCE_A,
    PairNonce(N_OTHER.hex()),
    PairNonce(N_B.hex()),
    REVEAL_B,
    REVEAL_B,
    REVEAL_B,
    PairReveal(N_OTHER.hex()),
    PairReveal(N_A.hex()),
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
    st.sampled_from([4.99, 5.0, 9.99, 10.0, 59.9, 60.0, 70.0]),  # biên NONCE/MESSAGE/CONFIRM
)
first_events = st.sampled_from(
    [Start(), Start(), Start(), Start(), Start(), Cancel("user"), Cancel("busy"), ConnectionLost()]
)
VARIANTS = ["normal", "normal", "normal", "known_same", "known_conflict", "cn_mismatch", "self", "peer_none"]


def expected_keep_known_name(role: Role, variant: str) -> bool:
    """M5: `SavePeer.keep_known_name` chỉ True khi máy này là bên trả lời và đã tin máy kia
    (cùng fingerprint). Các biến thể khác hoặc không tới được SavePeer, hoặc là thiết bị mới."""
    return role == Role.RESPONDER and variant == "known_same"


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


def check_event_sequence(
    session, role: Role, expected_peer, expected_name, events, *, expected_keep: bool = False
) -> None:
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
                assert type(act.keep_known_name) is bool
                assert act.keep_known_name == expected_keep

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
    keep = expected_keep_known_name(role, variant)
    check_event_sequence(session, role, peer, name, [(first, first_delta), *steps], expected_keep=keep)


# Biến thể "đường đúng có nhiễu": mỗi bước hoặc là bước kế tiếp của đường đúng, hoặc là sự kiện
# ngẫu nhiên. Chuỗi thuần ngẫu nhiên hiếm khi đi tới PAIRED, nên cần biến thể này để các bảo đảm
# về SavePeer/Close(PAIRED) thật sự được kiểm.
HAPPY = "happy"
small_deltas = st.one_of(st.just(0.0), st.floats(min_value=0.0, max_value=2.0, allow_nan=False))


def happy_path(role: Role, peer_first: bool) -> list:
    if role == Role.INITIATOR:
        first = [MessageReceived(COMMIT_B), MessageReceived(REVEAL_B)]
    else:
        first = [MessageReceived(NONCE_A)]
    confirms = [UserDecision(True), MessageReceived(PairConfirm())]
    if peer_first:
        confirms.reverse()
    return [*first, *confirms]


@pytest.mark.parametrize("role", [Role.INITIATOR, Role.RESPONDER])
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    variant=st.sampled_from(["normal", "normal", "normal", "known_same", "known_conflict"]),
    peer_first=st.booleans(),
    steps=st.lists(
        # Mỗi bước: khoảng 1/8 là nhiễu, còn lại đi tiếp đường đúng.
        st.tuples(st.sampled_from([HAPPY] * 7 + ["noise"]), fuzz_events, small_deltas),
        min_size=4,
        max_size=12,
    ),
)
def test_property_noisy_happy_path_keeps_guarantees(role, variant, peer_first, steps):
    session, peer, name = build_fuzz_session(role, variant)
    path = iter(happy_path(role, peer_first))
    events = [(Start(), 0.0)]
    for kind, noise, delta in steps:
        events.append((next(path, Tick()) if kind == HAPPY else noise, delta))
    keep = expected_keep_known_name(role, variant)
    check_event_sequence(session, role, peer, name, events, expected_keep=keep)


# --- Hồi quy tấn công: bí mật của từng bên chỉ lộ đúng lúc (bên trả lời cam kết trước) ---------
#
# Lý do của thứ tự mới: ở luồng cũ, bên khởi xướng cam kết trước và bên trả lời gửi n_B ngay sau
# pair_request; kẻ đứng giữa có thể mở phiên, lấy n_B rồi bỏ đi mà B chưa từng hiện mã. Nay:
#   (a) n_B không rời B trước khi B nhận một pair_nonce hợp lệ ở AWAIT_NONCE;
#   (b) B luôn ShowCode trước khi gửi pair_reveal (người dùng thấy mã → có dấu vết);
#   (c) n_A không rời A trước khi A nhận pair_commit; pair_request không mang nonce/commitment;
#   (d) A chỉ ShowCode khi pair_reveal khớp cam kết đã nhận với fp của B.


def check_secrecy(session, role: Role, events) -> None:
    """Kiểm (a)–(d) trên một chuỗi sự kiện bất kỳ cho phiên mặc định (A = CERT_A/N_A, B = CERT_B/N_B)."""
    own_secret = (N_A if role == Role.INITIATOR else N_B).hex()
    may_leak = False  # bí mật của mình đã được phép rời máy chưa
    received_commitment: str | None = None
    now = 0.0
    for event, delta in events:
        now += delta
        before_state = session.state
        before_deadline = session.deadline
        out = session.handle(event, now)
        active = before_state not in (State.IDLE, State.DONE)
        timed_out = active and now >= before_deadline
        msg = event.message if isinstance(event, MessageReceived) else None

        gate = not timed_out and (
            (role == Role.INITIATOR and before_state == State.AWAIT_COMMIT and isinstance(msg, PairCommit))
            or (role == Role.RESPONDER and before_state == State.AWAIT_NONCE and isinstance(msg, PairNonce))
        )
        if gate and role == Role.INITIATOR:
            received_commitment = msg.commitment
        may_leak = may_leak or gate

        if not may_leak:
            assert own_secret not in sent_text(out), (
                f"{role}: nonce của mình lộ ra ở trạng thái {before_state} với sự kiện {event!r}: {out!r}"
            )

        for i, act in enumerate(out):
            if isinstance(act, SendMessage):
                m = act.message
                if isinstance(m, PairRequest):
                    # (c) pair_request chỉ có cert.
                    assert to_dict(m) == {"type": "pair_request", "cert": CERT_A.pem}
                if isinstance(m, PairNonce):
                    assert role == Role.INITIATOR
                    assert m.nonce == N_A.hex()
                    assert gate, "pair_nonce chỉ được gửi ngay khi nhận pair_commit ở AWAIT_COMMIT"
                if isinstance(m, PairReveal):
                    # (b) bên trả lời hiện mã trước khi tiết lộ n_B, trong cùng danh sách hành động.
                    assert role == Role.RESPONDER
                    assert m.nonce == N_B.hex()
                    assert gate, "pair_reveal chỉ được gửi ngay khi nhận pair_nonce ở AWAIT_NONCE"
                    assert any(isinstance(a, ShowCode) for a in out[:i]), f"reveal trước ShowCode: {out!r}"
                if isinstance(m, PairCommit):
                    assert role == Role.RESPONDER
                    assert m.commitment == make_commitment(CERT_B.fingerprint, N_B)
            elif isinstance(act, ShowCode):
                if role == Role.INITIATOR:
                    # (d) chỉ hiện mã khi reveal khớp cam kết đã nhận, tính với fp của B.
                    assert before_state == State.AWAIT_REVEAL
                    assert isinstance(msg, PairReveal)
                    assert received_commitment is not None
                    n_b = bytes.fromhex(msg.nonce)
                    assert check_commitment(CERT_B.fingerprint, n_b, received_commitment)
                    assert act.code == pairing_code(CERT_A.fingerprint, CERT_B.fingerprint, N_A, n_b)
                else:
                    assert gate
                    n_a = bytes.fromhex(msg.nonce)
                    assert act.code == pairing_code(CERT_A.fingerprint, CERT_B.fingerprint, n_a, N_B)

        # Ngược lại của (d): reveal không khớp ở AWAIT_REVEAL → luôn invalid, không mã.
        if (
            role == Role.INITIATOR
            and not timed_out
            and before_state == State.AWAIT_REVEAL
            and isinstance(msg, PairReveal)
        ):
            n_b = bytes.fromhex(msg.nonce)
            if check_commitment(CERT_B.fingerprint, n_b, received_commitment):
                assert out == [ShowCode(pairing_code(CERT_A.fingerprint, CERT_B.fingerprint, N_A, n_b))]
            else:
                assert out[:-1] == [ABORT_INVALID]
                assert_closed(out, Outcome.PROTOCOL_ERROR)
        # Đường đúng của B: đúng hai hành động, đúng thứ tự.
        if role == Role.RESPONDER and gate:
            n_a = bytes.fromhex(msg.nonce)
            code = pairing_code(CERT_A.fingerprint, CERT_B.fingerprint, n_a, N_B)
            assert out == [ShowCode(code), SendMessage(PairReveal(N_B.hex()))]
    hypothesis.event(f"may_leak={may_leak}, outcome={session.outcome}")


# Sự kiện tấn công: nghiêng về các message của luồng mới để thường xuyên chạm cổng.
attack_messages = st.sampled_from(FUZZ_MESSAGES[2:]).map(MessageReceived)
attack_events = st.one_of(attack_messages, attack_messages, attack_messages, other_events)


@pytest.mark.parametrize("role", [Role.INITIATOR, Role.RESPONDER])
@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps=st.lists(st.tuples(attack_events, deltas), max_size=12))
def test_property_nonces_never_leak_before_their_gate(role, steps):
    # (a) + (b) cho bên trả lời, (c) + (d) cho bên khởi xướng, với chuỗi sự kiện ngẫu nhiên.
    session = make_initiator() if role == Role.INITIATOR else make_responder()
    check_secrecy(session, role, [(Start(), 0.0), *steps])


@pytest.mark.parametrize("role", [Role.INITIATOR, Role.RESPONDER])
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    steps=st.lists(
        st.tuples(st.sampled_from([HAPPY] * 5 + ["noise"]), attack_events, small_deltas),
        min_size=1,
        max_size=8,
    )
)
def test_property_nonces_never_leak_on_noisy_happy_path(role, steps):
    # Biến thể đi gần đường đúng để thật sự chạm và vượt cổng (pair_commit / pair_nonce).
    session = make_initiator() if role == Role.INITIATOR else make_responder()
    path = iter(happy_path(role, peer_first=False))
    events = [(Start(), 0.0)]
    for kind, noise, delta in steps:
        events.append((next(path, Tick()) if kind == HAPPY else noise, delta))
    check_secrecy(session, role, events)


def test_responder_n_b_never_sent_before_nonce_deterministic():
    # (a) đơn giản, không phụ thuộc hypothesis: mọi sự kiện có thể tới trước pair_nonce.
    for event in ALL_NON_START_EVENTS:
        s = make_responder()
        out = s.handle(Start(), T0) + s.handle(event, T0 + 1)
        if isinstance(event, MessageReceived) and isinstance(event.message, PairNonce):
            continue
        assert N_B.hex() not in sent_text(out), f"n_B lộ với sự kiện {event!r}"
        assert not any(isinstance(a, ShowCode) for a in out)


def test_initiator_n_a_never_sent_before_commit_deterministic():
    # (c) đơn giản: mọi sự kiện có thể tới trước pair_commit.
    for event in ALL_NON_START_EVENTS:
        s = make_initiator()
        out = s.handle(Start(), T0) + s.handle(event, T0 + 1)
        if isinstance(event, MessageReceived) and isinstance(event.message, PairCommit):
            continue
        assert N_A.hex() not in sent_text(out), f"n_A lộ với sự kiện {event!r}"


@settings(max_examples=300, deadline=None)
@given(
    n_a=nonces,
    n_b=nonces,
    n_b2=nonces,
    commit_fp=st.sampled_from(["responder", "initiator", "third"]),
    d1=st.floats(0, 9.99),
    d2=st.floats(0, 9.99),
)
def test_property_responder_changing_n_b_after_seeing_n_a_is_protocol_error(
    n_a, n_b, n_b2, commit_fp, d1, d2
):
    # (d) Bên trả lời giả: cam kết n_B, thấy n_A, rồi tiết lộ n_B2 để lái mã. Hoặc cam kết bằng
    # fp không phải của B (kẻ thay chứng chỉ) rồi tiết lộ đúng nonce đã cam kết.
    fp = {
        "responder": CERT_B.fingerprint,
        "initiator": CERT_A.fingerprint,
        "third": CERT_C.fingerprint,
    }[commit_fp]
    if commit_fp == "responder":
        assume(n_b2 != n_b)
        revealed = n_b2
    else:
        revealed = n_b
    s = make_initiator(nonce=n_a)
    assert s.handle(Start(), T0) == [SendMessage(PairRequest(cert=CERT_A.pem))]
    assert s.handle(MessageReceived(PairCommit(make_commitment(fp, n_b))), T0 + d1) == [
        SendMessage(PairNonce(n_a.hex()))
    ]
    actions = s.handle(MessageReceived(PairReveal(revealed.hex())), T0 + d1 + d2)
    assert actions[:-1] == [ABORT_INVALID]
    assert_closed(actions, Outcome.PROTOCOL_ERROR)
    assert not any(isinstance(a, ShowCode) for a in actions)
    assert s.code is None
    assert_done(s, Outcome.PROTOCOL_ERROR)


@settings(max_examples=300, deadline=None)
@given(n_a=nonces, n_b=nonces, d1=st.floats(0, 9.99), d2=st.floats(0, 9.99))
def test_property_honest_reveal_always_shows_code(n_a, n_b, d1, d2):
    s = make_initiator(nonce=n_a)
    s.handle(Start(), T0)
    s.handle(MessageReceived(PairCommit(make_commitment(CERT_B.fingerprint, n_b))), T0 + d1)
    actions = s.handle(MessageReceived(PairReveal(n_b.hex())), T0 + d1 + d2)
    assert actions == [ShowCode(pairing_code(CERT_A.fingerprint, CERT_B.fingerprint, n_a, n_b))]


# (e) Kẻ đứng giữa mở N phiên liên tiếp tới B, mỗi phiên làm một kiểu: gửi pair_nonce, gửi
# reveal/commit/request sai lúc, hủy, mất kết nối, rác, chờ hết giờ… Nó chỉ được biết n_B của
# những phiên mà B đã ShowCode (người dùng ở B thấy lời hỏi nên có dấu vết).
ATTACK_MOVES = [
    "nonce",
    "nonce_n_b",
    "reveal_early",
    "commit",
    "request",
    "confirm",
    "abort",
    "lost",
    "bad",
    "hello",
    "cancel_busy",
    "timeout",
    "user_yes",
    "user_no",
]


def _attack_event(move: str, guess: bytes, n_b_seen: str | None):
    return {
        "nonce": MessageReceived(PairNonce(guess.hex())),
        # Gửi lại n_B (nếu đã thấy) làm n_A: thử dắt B tự tính mã với n_A = n_B.
        "nonce_n_b": MessageReceived(PairNonce(n_b_seen or guess.hex())),
        "reveal_early": MessageReceived(PairReveal(guess.hex())),
        "commit": MessageReceived(PairCommit(make_commitment(CERT_A.fingerprint, guess))),
        "request": MessageReceived(PairRequest(cert=CERT_C.pem)),
        "confirm": MessageReceived(PairConfirm()),
        "abort": MessageReceived(PairAbort("user")),
        "lost": ConnectionLost(),
        "bad": BadMessage("rác"),
        "hello": MessageReceived(HELLO_A),
        "cancel_busy": Cancel(AbortReason.BUSY),
        "timeout": Tick(),
        "user_yes": UserDecision(True),
        "user_no": UserDecision(False),
    }[move]


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    sessions=st.lists(
        st.tuples(
            nonces,
            nonces,
            st.lists(st.tuples(st.sampled_from(ATTACK_MOVES), small_deltas), min_size=1, max_size=4),
        ),
        min_size=1,
        max_size=8,
    )
)
def test_property_mitm_learns_n_b_only_from_sessions_that_showed_code(sessions):
    now = 0.0
    learned: set[bytes] = set()
    shown_sessions: set[bytes] = set()
    prompts = 0
    for n_b, guess, moves in sessions:
        s = make_responder(nonce=n_b)
        actions = s.handle(Start(), now)
        n_b_seen: str | None = None
        for move, delta in moves:
            if s.state == State.DONE:
                break
            now += delta
            if move == "timeout":
                now = max(now, s.deadline)
            out = s.handle(_attack_event(move, guess, n_b_seen), now)
            actions += out
            if n_b.hex() in sent_text(out):
                n_b_seen = n_b.hex()
        if s.state != State.DONE:
            actions += s.handle(ConnectionLost(), now)
        showed = any(isinstance(a, ShowCode) for a in actions)
        prompts += showed
        if showed:
            shown_sessions.add(n_b)
        if n_b.hex() in sent_text(actions):
            learned.add(n_b)
            assert showed, f"kẻ tấn công lấy được n_B mà B không hiện mã: {actions!r}"
        # Cam kết là thứ duy nhất về n_B mà kẻ tấn công thấy được khi B chưa hiện mã.
        commits = [
            a.message for a in actions if isinstance(a, SendMessage) and isinstance(a.message, PairCommit)
        ]
        assert commits == [PairCommit(make_commitment(CERT_B.fingerprint, n_b))]
    assert learned <= shown_sessions
    assert len(learned) <= prompts
    hypothesis.event(f"learned={len(learned)}/{len(sessions)}")
