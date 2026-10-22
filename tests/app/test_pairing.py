"""Test `teleport.app.pairing` theo đặc tả PR-3 mục 2.4, 3.1–3.3, 3.8 và 4.5 (P1–P27).

Phần lớn là integration: 2–3 `Node` thật trên 127.0.0.1 (fixture `node_factory`, hằng thời gian đã
rút ngắn bằng `fast`), cộng client/server thô để thử dữ liệu độc. Mọi test kết thúc bằng `no_leaks`
(không còn task `teleport.*`, không socket nào bị bỏ rơi) và kiểm `node.lock` đã nhả (trong
`node_factory`). Ngoài ra có thêm vài ca theo đặc tả mà bảng 4.5 không đánh số (P-map, P-busy, …).
"""

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import secrets
import time

import pytest

from teleport.app.pairing import PairingFinished, PairingOutcome, PairingPrompt, PairingService
from teleport.core.framing import HEADER, encode_message
from teleport.core.messages import PROTOCOL_VERSION, Hello
from teleport.core.pairing import Outcome, Role
from teleport.core.pairing_code import format_code, pairing_code
from teleport.net.stream import read_message
from teleport.trust.identity import load_or_create_identity, peer_cert_from_pem
from teleport.trust.store import TrustStore, TrustStoreError

pytestmark = [
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
integration = pytest.mark.integration

LOCAL = "127.0.0.1"
INVALID = {"type": "pair_abort", "reason": "invalid"}
GARBAGE_PEM = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"


def ref_commitment(fp_responder: str, nonce: bytes) -> str:
    """Cam kết của bên trả lời, tính độc lập theo docs/protocol.md §5."""
    return hashlib.sha256(b"teleport-commit-v1" + bytes.fromhex(fp_responder) + nonce).hexdigest()


async def pair(a, b, timeout: float = 15.0) -> PairingFinished:
    return await asyncio.wait_for(a.pairing.pair_with([LOCAL], b.port), timeout)


def outcomes(rec) -> list[str]:
    return [str(f.outcome) for f in rec.finished]


async def wait_until(predicate, timeout: float = 2.0, what: str = "điều kiện") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"hết {timeout} s mà {what} vẫn chưa đúng")
        await asyncio.sleep(0.01)


def gates_idle(*nodes) -> bool:
    return all(n.pairing.gate.active is None for n in nodes)


# --- Unit: kiểu dữ liệu, tham số ----------------------------------------------------------------


def test_outcome_values_cover_core_outcome_plus_app_only():
    """`PairingOutcome` = mọi giá trị của `core.pairing.Outcome` + 7 kết quả chỉ `app` biết."""
    core_values = {o.value for o in Outcome}
    app_only = {
        "unreachable",
        "rejected",
        "identity_mismatch",
        "version_mismatch",
        "busy",
        "self",
        "save_failed",
    }
    assert {o.value for o in PairingOutcome} == core_values | app_only
    for value in core_values:
        assert PairingOutcome(value).value == value


def test_prompt_and_finished_are_frozen_dataclasses():
    import dataclasses

    assert dataclasses.is_dataclass(PairingPrompt) and dataclasses.is_dataclass(PairingFinished)
    names = {f.name for f in dataclasses.fields(PairingPrompt)}
    assert names == {"session_id", "role", "address", "peer_name", "peer_device_id", "code", "expires_at"}
    names = {f.name for f in dataclasses.fields(PairingFinished)}
    assert names == {
        "session_id",
        "role",
        "outcome",
        "address",
        "port",
        "peer_name",
        "peer_device_id",
        "peer_reason",
        "device",
        "known_name",
    }


@pytest.fixture
def service_parts(tmp_path):
    identity = load_or_create_identity(tmp_path / "svc")
    hello = Hello(PROTOCOL_VERSION, identity.device_id, "Svc", "0.0.0", "windows")
    store = TrustStore.load(tmp_path / "svc")
    finished: list = []
    return identity, hello, store, finished


def make_service(parts, **kwargs) -> PairingService:
    identity, hello, store, finished = parts
    return PairingService(
        identity=identity,
        hello=kwargs.pop("hello", hello),
        store=store,
        on_prompt=lambda p: None,
        on_finished=finished.append,
        on_trust_changed=lambda: None,
        **kwargs,
    )


async def test_service_rejects_hello_of_other_identity(service_parts, tmp_path):
    other = load_or_create_identity(tmp_path / "khac")
    bad = Hello(PROTOCOL_VERSION, other.device_id, "Svc", "0.0.0", "windows")
    with pytest.raises(ValueError):
        make_service(service_parts, hello=bad)


async def test_service_accepting_maps_to_gate(service_parts):
    svc = make_service(service_parts)
    try:
        assert svc.accepting is False and svc.gate.accepting is False
        svc.accepting = True
        assert svc.gate.accepting is True
        svc2 = make_service(service_parts, accepting=True)
        assert svc2.accepting is True
        await svc2.close()
        assert svc2.accepting is False
    finally:
        await svc.close()


@pytest.mark.parametrize(
    ("addresses", "port"),
    [
        ("127.0.0.1", 47474),
        (b"127.0.0.1", 47474),
        ([], 47474),
        (["localhost"], 47474),
        (["0.0.0.0"], 47474),
        (["224.0.0.251"], 47474),
        (["255.255.255.255"], 47474),
        (["01.2.3.4"], 47474),
        (["127.0.0.1", "0.0.0.0"], 47474),
        ([f"10.0.0.{i}" for i in range(1, 18)], 47474),
        ([None], 47474),
        (["127.0.0.1"], 0),
        (["127.0.0.1"], 65536),
        (["127.0.0.1"], True),
        (["127.0.0.1"], "47474"),
        (["127.0.0.1"], 47474.0),
    ],
)
async def test_pair_with_validates_arguments_before_io(service_parts, monkeypatch, addresses, port):
    """3.1 bước 1: sai → ValueError trước mọi I/O và trước khi giữ slot; không gọi on_finished."""
    from teleport.app import pairing as app_pairing

    async def no_io(*args, **kwargs):
        raise AssertionError("không được mở kết nối khi tham số sai")

    if hasattr(app_pairing, "open_pairing"):
        monkeypatch.setattr(app_pairing, "open_pairing", no_io)
    svc = make_service(service_parts)
    try:
        with pytest.raises(ValueError):
            await svc.pair_with(addresses, port)
        assert service_parts[3] == []
        assert svc.gate.active is None
    finally:
        await svc.close()


async def test_pair_with_after_close_returns_cancelled_without_connecting(service_parts, monkeypatch):
    """2.4 `close()`: sau đó `pair_with` trả ngay CANCELLED, session_id None, vẫn gọi on_finished."""
    from teleport.app import pairing as app_pairing

    async def no_io(*args, **kwargs):
        raise AssertionError("không được mở kết nối sau close()")

    if hasattr(app_pairing, "open_pairing"):
        monkeypatch.setattr(app_pairing, "open_pairing", no_io)
    svc = make_service(service_parts, accepting=True)
    await svc.close()
    await svc.close()  # gọi nhiều lần không lỗi
    result = await asyncio.wait_for(svc.pair_with([LOCAL], 47474), 2)
    assert result.outcome == PairingOutcome.CANCELLED
    assert result.session_id is None
    assert result.role == Role.INITIATOR
    assert result.address == LOCAL and result.port == 47474
    assert service_parts[3] == [result]
    assert svc.accepting is False


def test_decide_rejects_non_bool(service_parts):
    """P24 (phần unit): `accepted` không phải bool → ValueError; id lạ → False."""

    async def run():
        svc = make_service(service_parts)
        try:
            assert svc.decide(12345, True) is False
            with pytest.raises(ValueError):
                svc.decide(12345, 1)
            with pytest.raises(ValueError):
                svc.decide(12345, None)
        finally:
            await svc.close()

    asyncio.run(run())


# --- P1, P2: đường thành công ---------------------------------------------------------------------


@integration
async def test_p1_pairing_success_saves_both_sides(node_factory, fast):
    B = await node_factory("B", accept_pairing=True, answer=True)
    A = await node_factory("A", answer=True)
    t0 = time.monotonic()
    result = await pair(A, B)
    await B.rec.wait_finished(1)
    elapsed = time.monotonic() - t0

    assert result.outcome == PairingOutcome.PAIRED
    assert outcomes(B.rec) == ["paired"]
    assert elapsed < 3.0

    # Mỗi bên đúng 1 lời hỏi, mã trùng nhau, 6 chữ số.
    assert len(A.rec.prompts) == 1 and len(B.rec.prompts) == 1
    pa, pb = A.rec.prompts[0], B.rec.prompts[0]
    assert re.fullmatch(r"[0-9]{6}", pa.code)
    assert pa.code == pb.code
    assert pa.role == Role.INITIATOR and pb.role == Role.RESPONDER
    assert (pa.peer_name, pa.peer_device_id, pa.address) == ("B", B.id, LOCAL)
    assert (pb.peer_name, pb.peer_device_id, pb.address) == ("A", A.id, LOCAL)
    # expires_at = deadline ngay sau ShowCode = lúc hiện mã + CONFIRM_TIMEOUT (giờ của loop).
    for rec in (A.rec, B.rec):
        left = rec.prompts[0].expires_at - rec.prompt_loop_times[0]
        assert 0.8 * fast.CONFIRM_TIMEOUT <= left <= fast.CONFIRM_TIMEOUT + 0.05, left
    assert A.rec.decide_results == [True] and B.rec.decide_results == [True]

    # on_finished đúng một lần mỗi bên; bên khởi xướng nhận đúng object pair_with trả về.
    assert A.rec.finished == [result]
    rb = B.rec.finished[0]
    assert result.role == Role.INITIATOR
    assert (result.address, result.port) == (LOCAL, B.port)
    assert (result.peer_name, result.peer_device_id) == ("B", B.id)
    assert result.peer_reason is None and result.known_name is None
    assert result.device is not None and result.device.device_id == B.id
    assert isinstance(result.session_id, int)
    assert result.session_id == pa.session_id
    assert rb.role == Role.RESPONDER
    assert rb.port is None
    assert (rb.address, rb.peer_name, rb.peer_device_id) == (LOCAL, "A", A.id)
    assert rb.device is not None and rb.device.device_id == A.id
    assert rb.session_id == pb.session_id

    # Kho trong RAM và trên đĩa.
    da, db = A.store.get(B.id), B.store.get(A.id)
    assert da is not None and da.fingerprint == B.identity.fingerprint and da.name == "B"
    assert db is not None and db.fingerprint == A.identity.fingerprint and db.name == "A"
    assert TrustStore.load(A.dir).get(B.id).fingerprint == B.identity.fingerprint
    assert TrustStore.load(B.dir).get(A.id).fingerprint == A.identity.fingerprint

    # Gate: rảnh hai bên, và đã finish trước on_finished (thứ tự mục 2.4).
    assert gates_idle(A, B)
    assert A.rec.gate_active_at_finish == [None] and B.rec.gate_active_at_finish == [None]
    assert B.pairing.gate.failures(LOCAL) == 0


@integration
async def test_p2_control_connections_work_after_pairing_and_after_restart(node_factory, control_client):
    B = await node_factory("B", accept_pairing=True, answer=True)
    A = await node_factory("A", answer=True)
    assert (await pair(A, B)).outcome == PairingOutcome.PAIRED
    await B.rec.wait_finished(1)

    # Không khởi động lại: listener đã đổi context theo kho mới (on_trust_changed).
    ab = await control_client(A, B)
    ba = await control_client(B, A)
    await wait_until(lambda: A.id in B.connected_devices() and B.id in A.connected_devices(), 2)
    await wait_until(lambda: ab.pongs() >= 1 and ba.pongs() >= 1, 3, "chưa trao ping/pong")

    await A.close()
    await B.close()
    await asyncio.wait_for(asyncio.gather(ab.task, ba.task, return_exceptions=True), 5)

    # Khởi động lại trên cùng thư mục: hai máy tự nhận ra nhau, không có lời hỏi nào.
    B2 = await node_factory("B", accept_pairing=True)
    A2 = await node_factory("A")
    ab2 = await control_client(A2, B2)
    ba2 = await control_client(B2, A2)
    await wait_until(lambda: A2.id in B2.connected_devices() and B2.id in A2.connected_devices(), 2)
    await wait_until(lambda: ab2.pongs() >= 1 and ba2.pongs() >= 1, 3, "chưa trao ping/pong")
    assert A2.rec.prompts == [] and B2.rec.prompts == []
    assert A2.rec.finished == [] and B2.rec.finished == []


# --- P3–P8: từ chối, hết giờ, bận, thời gian chờ ----------------------------------------------------


@integration
async def test_p3_responder_rejects(node_factory):
    B = await node_factory("B", accept_pairing=True, answer=False)
    A = await node_factory("A", answer=True)
    result = await pair(A, B)
    await B.rec.wait_finished(1)
    assert result.outcome == PairingOutcome.PEER_REJECTED
    assert result.peer_reason == "user"
    assert outcomes(B.rec) == ["user_rejected"]
    assert A.store.devices() == [] and B.store.devices() == []
    assert not (A.dir / "trusted.json").exists() or TrustStore.load(A.dir).devices() == []
    assert B.pairing.gate.failures(LOCAL) == 1
    assert gates_idle(A, B)


@integration
async def test_p4_initiator_rejects(node_factory):
    B = await node_factory("B", accept_pairing=True, answer=True)
    A = await node_factory("A", answer=False)
    result = await pair(A, B)
    await B.rec.wait_finished(1)
    assert result.outcome == PairingOutcome.USER_REJECTED
    assert outcomes(B.rec) == ["peer_rejected"]
    assert B.rec.finished[0].peer_reason == "user"
    assert A.store.devices() == [] and B.store.devices() == []
    assert gates_idle(A, B)


@integration
async def test_p5_nobody_answers_times_out(node_factory, fast):
    B = await node_factory("B", accept_pairing=True)
    A = await node_factory("A")
    result = await pair(A, B)
    await B.rec.wait_finished(1)
    both = {str(result.outcome), *outcomes(B.rec)}
    assert both <= {"timeout", "peer_timeout"}
    assert "timeout" in both
    assert A.store.devices() == [] and B.store.devices() == []
    limit = fast.CONFIRM_TIMEOUT + fast.CONFIRM_GRACE + 2
    for rec in (A.rec, B.rec):
        waited = rec.finished_times[0] - rec.prompt_times[0]
        assert 0.8 * fast.CONFIRM_TIMEOUT <= waited <= limit, waited
    assert gates_idle(A, B)


@integration
async def test_p6_initiator_accepts_responder_silent(node_factory):
    B = await node_factory("B", accept_pairing=True)
    A = await node_factory("A", answer=True)
    result = await pair(A, B)
    await B.rec.wait_finished(1)
    assert outcomes(B.rec) == ["timeout"]
    assert result.outcome == PairingOutcome.PEER_TIMEOUT
    assert A.store.get(B.id) is None
    assert B.store.get(A.id) is None


@integration
async def test_p7_responder_not_accepting_is_busy(node_factory):
    B = await node_factory("B", accept_pairing=False, answer=True)
    A = await node_factory("A", answer=True)
    result = await pair(A, B)
    assert result.outcome == PairingOutcome.PEER_BUSY
    assert result.peer_reason == "busy"
    await asyncio.sleep(0.3)
    assert B.rec.prompts == [] and B.rec.finished == []
    assert A.rec.prompts == []
    assert B.pairing.gate.failures(LOCAL) == 0
    assert gates_idle(A, B)


@integration
async def test_p8_backoff_after_failure_then_admitted_again(node_factory, fast):
    B = await node_factory("B", accept_pairing=True, answer=False)
    A = await node_factory("A", answer=True)
    assert (await pair(A, B)).outcome == PairingOutcome.PEER_REJECTED
    await B.rec.wait_finished(1)
    finished_at = B.rec.finished_times[0]

    retry = await pair(A, B)
    retry_done = time.monotonic()
    assert retry_done - finished_at < fast.BACKOFF_BASE, "máy test quá chậm để kiểm thời gian chờ"
    assert retry.outcome == PairingOutcome.PEER_BUSY
    assert len(B.rec.prompts) == 1 and len(B.rec.finished) == 1  # BUSY: không prompt, không on_finished

    await asyncio.sleep(max(0.0, finished_at + fast.BACKOFF_BASE + 0.15 - time.monotonic()))
    B.rec.answer = True
    again = await pair(A, B)
    assert len(B.rec.prompts) == 2
    assert again.outcome == PairingOutcome.PAIRED


@integration
async def test_p9_one_session_at_a_time(node_factory):
    B = await node_factory("B", accept_pairing=True)
    A = await node_factory("A")
    C = await node_factory("C", answer=True)
    task = asyncio.create_task(A.pairing.pair_with([LOCAL], B.port), name="test.pair")
    await A.rec.wait_prompts()
    await B.rec.wait_prompts()

    rc = await pair(C, B)
    assert rc.outcome == PairingOutcome.PEER_BUSY
    assert C.rec.prompts == []
    assert len(B.rec.prompts) == 1

    assert A.pairing.decide(A.rec.prompts[0].session_id, True) is True
    assert B.pairing.decide(B.rec.prompts[0].session_id, True) is True
    ra = await asyncio.wait_for(task, 10)
    await B.rec.wait_finished(1)
    assert ra.outcome == PairingOutcome.PAIRED
    assert outcomes(B.rec) == ["paired"]
    assert B.store.get(C.id) is None


@integration
async def test_p10_closed_port_is_unreachable(node_factory, app_helpers):
    A = await node_factory("A")
    port = app_helpers.free_port()
    result = await asyncio.wait_for(A.pairing.pair_with([LOCAL], port), 10)
    assert result.outcome == PairingOutcome.UNREACHABLE
    assert A.rec.finished == [result]
    assert (result.address, result.port) == (LOCAL, port)
    assert result.peer_name is None and result.peer_device_id is None
    assert result.role == Role.INITIATOR
    assert A.rec.prompts == []
    assert A.pairing.gate.active is None


@integration
async def test_p11_pairing_with_self(node_factory):
    A = await node_factory("A", accept_pairing=True, answer=True)
    result = await pair(A, A)
    assert result.outcome == PairingOutcome.SELF
    await asyncio.sleep(0.3)
    assert A.rec.prompts == []
    assert A.rec.finished == [result]  # không có phiên trả lời nào được tạo
    assert A.store.devices() == []
    assert A.pairing.gate.active is None
    assert A.pairing.gate.failures(LOCAL) == 0


# --- P12, P13: danh tính --------------------------------------------------------------------------


@integration
async def test_p12a_initiator_knows_other_identity_for_device_id(node_factory, app_helpers):
    B = await node_factory("B", accept_pairing=True, answer=True)
    A = await node_factory("A", answer=True)
    fake = peer_cert_from_pem(app_helpers.fake_cert_with_cn(B.id))
    assert fake.fingerprint != B.identity.fingerprint
    A.store.add(fake, "B-cũ")

    result = await pair(A, B)
    assert result.outcome == PairingOutcome.IDENTITY_CONFLICT
    assert result.known_name == "B-cũ"
    assert result.peer_device_id == B.id
    await asyncio.sleep(0.3)
    # A không gửi pair_request: B không có phiên nào.
    assert B.rec.prompts == [] and B.rec.finished == []
    assert B.pairing.gate.failures(LOCAL) == 0
    assert A.rec.prompts == []
    assert A.store.get(B.id).fingerprint == fake.fingerprint
    assert gates_idle(A, B)


@integration
async def test_p12b_responder_knows_other_identity_for_device_id(node_factory, app_helpers):
    B = await node_factory("B", accept_pairing=True, answer=True)
    A = await node_factory("A", answer=True)
    fake = peer_cert_from_pem(app_helpers.fake_cert_with_cn(A.id))
    B.store.add(fake, "A-cũ")

    result = await pair(A, B)
    await B.rec.wait_finished(1)
    assert outcomes(B.rec) == ["identity_conflict"]
    assert B.rec.finished[0].known_name == "A-cũ"
    assert result.outcome == PairingOutcome.PEER_INVALID
    assert A.rec.prompts == [] and B.rec.prompts == []
    assert A.store.get(B.id) is None
    assert B.store.get(A.id).fingerprint == fake.fingerprint
    assert gates_idle(A, B)


@integration
async def test_p13_repair_same_identity_and_name_update(node_factory):
    B = await node_factory("B", accept_pairing=True, answer=True)
    A = await node_factory("A", answer=True)
    assert (await pair(A, B)).outcome == PairingOutcome.PAIRED
    await B.rec.wait_finished(1)
    assert (await pair(A, B)).outcome == PairingOutcome.PAIRED
    await B.rec.wait_finished(2)
    assert outcomes(B.rec) == ["paired", "paired"]

    await B.close()
    B2 = await node_factory("B", accept_pairing=True, answer=True, name="B2")
    assert B2.hello.name == "B2"
    result = await pair(A, B2)
    assert result.outcome == PairingOutcome.PAIRED
    assert result.peer_name == "B2"
    assert A.store.get(B2.id).name == "B2"
    assert TrustStore.load(A.dir).get(B2.id).name == "B2"
    assert len(A.store.devices()) == 1


# --- P14, P15: đóng node khi đang hỏi -----------------------------------------------------------------


@integration
async def test_p14_initiator_node_close_while_asking(node_factory):
    B = await node_factory("B", accept_pairing=True)
    A = await node_factory("A")
    task = asyncio.create_task(A.pairing.pair_with([LOCAL], B.port), name="test.pair")
    await A.rec.wait_prompts()
    await B.rec.wait_prompts()

    t0 = time.monotonic()
    await asyncio.wait_for(A.close(), 10)
    result = await asyncio.wait_for(task, 5)
    assert result.outcome == PairingOutcome.CANCELLED
    assert A.rec.finished == [result]
    await B.rec.wait_finished(1, timeout=3)
    assert outcomes(B.rec) == ["peer_rejected"]
    assert B.rec.finished_times[0] - t0 <= 1.0
    assert B.store.devices() == []


@integration
async def test_p15_responder_node_close_while_asking(node_factory):
    B = await node_factory("B", accept_pairing=True)
    A = await node_factory("A")
    task = asyncio.create_task(A.pairing.pair_with([LOCAL], B.port), name="test.pair")
    await A.rec.wait_prompts()
    await B.rec.wait_prompts()

    t0 = time.monotonic()
    await asyncio.wait_for(B.close(), 10)
    assert outcomes(B.rec) == ["cancelled"]
    result = await asyncio.wait_for(task, 5)
    assert result.outcome == PairingOutcome.PEER_REJECTED
    assert time.monotonic() - t0 <= 2.0
    assert A.rec.finished == [result]
    assert A.pairing.gate.active is None


# --- P16, P17: client thô bỏ dở ---------------------------------------------------------------------


@integration
async def test_p16_raw_client_closes_after_commit_gets_no_n_b_and_no_prompt(
    node_factory, raw_pairing_client, tmp_path
):
    """Client thô lấy `pair_commit` rồi bỏ đi: B không hiện lời hỏi, không bao giờ gửi n_B."""
    B = await node_factory("B", accept_pairing=True, answer=True)
    raw_id = load_or_create_identity(tmp_path / "raw")
    c = await raw_pairing_client(B.port)
    await c.begin(raw_id)
    commitment = await c.read_commit()
    # Chưa gửi pair_nonce thì B không gửi gì thêm (đặc biệt không có pair_reveal).
    with pytest.raises(TimeoutError):
        await c.read(timeout=0.3)
    t0 = time.monotonic()
    c.abort()
    await B.rec.wait_finished(1, timeout=2)
    assert outcomes(B.rec) == ["connection_lost"]
    await wait_until(lambda: B.pairing.gate.active is None, 1.0)
    assert time.monotonic() - t0 <= 1.0 + 0.5
    assert B.pairing.gate.failures(LOCAL) == 1
    assert B.rec.prompts == []
    assert re.fullmatch(r"[0-9a-f]{64}", commitment)


@integration
async def test_raw_client_reads_reveal_then_aborts_was_prompted_and_backs_off(
    node_factory, raw_pairing_client, tmp_path
):
    """Kẻ lấy n_B rồi bỏ đi ngay: B đã hiện lời hỏi cho đúng phiên đó (có dấu vết), IP bị tính
    một lần thất bại (chịu backoff), không lưu gì."""
    B = await node_factory("B", accept_pairing=True)  # không trả lời lời hỏi
    raw_id = load_or_create_identity(tmp_path / "raw")
    c = await raw_pairing_client(B.port)
    await c.begin(raw_id)
    code = await c.run_to_code(B.identity.fingerprint)
    c.abort()
    await B.rec.wait_finished(1, timeout=3)
    assert outcomes(B.rec) == ["connection_lost"]
    assert len(B.rec.prompts) == 1, "B phải hiện lời hỏi trước khi tiết lộ n_B"
    prompt = B.rec.prompts[0]
    assert prompt.session_id == B.rec.finished[0].session_id
    assert prompt.code == code
    assert B.rec.prompt_times[0] <= B.rec.finished_times[0]
    await wait_until(lambda: B.pairing.gate.active is None, 1.0)
    assert B.pairing.gate.failures(LOCAL) == 1
    assert B.store.devices() == []


def _blocking_pairing_client(port: int, identity, out: dict) -> None:
    """Client ghép đôi thô chạy trong **luồng riêng** (socket chặn), đóng vai A tới hết `pair_reveal`.

    Ghi `out["reveal_read_at"]` = `time.monotonic()` ngay khi đọc xong `pair_reveal`. Vì không chung
    event loop với node, nó đọc được reveal ngay khi reveal lên dây, kể cả khi loop của node đang bận.
    """
    import json
    import socket as socket_mod

    from teleport.net import tls

    def read_frame(s) -> dict:
        def exact(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = s.recv(n - len(buf))
                if not chunk:
                    raise ConnectionError("đóng giữa chừng")
                buf += chunk
            return buf

        (length,) = HEADER.unpack(exact(HEADER.size))
        assert length <= 16 * 1024
        return json.loads(exact(length))

    n_a = secrets.token_bytes(32)
    raw = socket_mod.create_connection((LOCAL, port), timeout=5)
    s = tls.client_context(None).wrap_socket(raw)
    try:
        s.settimeout(5)
        hello = read_frame(s)
        assert hello["type"] == "hello"
        s.sendall(
            encode_message(
                {
                    "type": "hello",
                    "protocol": PROTOCOL_VERSION,
                    "device_id": identity.device_id,
                    "name": "Raw",
                    "app_version": "0.0.0",
                    "os": "windows",
                }
            )
        )
        s.sendall(encode_message({"type": "pair_request", "cert": identity.cert_pem()}))
        commit = read_frame(s)
        assert commit["type"] == "pair_commit", commit
        out["sent_nonce_at"] = time.monotonic()
        s.sendall(encode_message({"type": "pair_nonce", "nonce": n_a.hex()}))
        reveal = read_frame(s)
        out["reveal_read_at"] = time.monotonic()
        assert reveal["type"] == "pair_reveal", reveal
        n_b = bytes.fromhex(reveal["nonce"])
        assert ref_commitment(out["responder_fp"], n_b) == commit["commitment"]
        out["code"] = pairing_code(identity.fingerprint, out["responder_fp"], n_a, n_b)
    finally:
        with contextlib.suppress(OSError):
            s.close()


PROMPT_HOLD = 0.2  # lời hỏi ở B "chiếm" chừng này giây; reveal gửi trước lời hỏi sẽ bị đọc trong lúc đó


@integration
async def test_responder_prompt_happens_before_pair_reveal_reaches_the_wire(node_factory, tmp_path):
    """B phải gọi `on_prompt` (hiện mã) **xong** rồi mới gửi `pair_reveal`.

    `on_prompt` của B giữ PROMPT_HOLD giây rồi ghi mốc kết thúc. Client ở luồng riêng ghi mốc lúc
    đọc được `pair_reveal`. Thứ tự đúng: mốc kết thúc lời hỏi < mốc đọc reveal. Nếu app gửi reveal
    trước (đảo ShowCode/SendMessage), client đọc được reveal trong lúc lời hỏi đang giữ → fail.
    """
    prompt_done: list[float] = []

    def hold_prompt(prompt) -> None:
        time.sleep(PROMPT_HOLD)  # cố ý chặn loop của B: chỉ client ở luồng khác còn chạy
        prompt_done.append(time.monotonic())

    B = await node_factory("B", accept_pairing=True, answer=hold_prompt)
    raw_id = load_or_create_identity(tmp_path / "raw")
    out: dict = {"responder_fp": B.identity.fingerprint}
    await asyncio.wait_for(asyncio.to_thread(_blocking_pairing_client, B.port, raw_id, out), 10)

    await B.rec.wait_finished(1, timeout=3)  # client đã đóng → connection_lost
    assert len(B.rec.prompts) == 1
    assert B.rec.prompts[0].code == out["code"]
    assert len(prompt_done) == 1
    assert B.rec.prompt_times[0] >= out["sent_nonce_at"]
    assert prompt_done[0] <= out["reveal_read_at"], (
        f"pair_reveal tới client lúc {out['reveal_read_at']:.3f}, trước khi lời hỏi xong lúc "
        f"{prompt_done[0]:.3f}: n_B lộ trước khi B hiện mã"
    )
    await wait_until(lambda: B.pairing.gate.active is None, 1.0)


@integration
async def test_p17_save_only_after_both_confirm(node_factory, raw_pairing_client, tmp_path, fast, caplog):
    """P17: client thô (vai A) nhận mã nhưng không gửi pair_confirm; B đồng ý → B không lưu."""
    caplog.set_level(logging.DEBUG)
    B = await node_factory("B", accept_pairing=True, answer=True)
    raw_id = load_or_create_identity(tmp_path / "raw")
    c = await raw_pairing_client(B.port)
    await c.begin(raw_id, "Raw")
    code = await c.run_to_code(B.identity.fingerprint)

    await B.rec.wait_prompts()
    assert B.rec.prompts[0].code == code  # mã của B đúng công thức protocol §5
    assert await c.read_type("pair_confirm") == {"type": "pair_confirm"}

    await B.rec.wait_finished(1, timeout=fast.CONFIRM_TIMEOUT + fast.CONFIRM_GRACE + fast.SLACK)
    assert outcomes(B.rec)[0] in ("timeout", "connection_lost")
    assert B.store.get(raw_id.device_id) is None
    assert TrustStore.load(B.dir).get(raw_id.device_id) is None
    rest = await c.read_until_closed(3)
    assert rest in ([], [{"type": "pair_abort", "reason": "timeout"}])

    # N9: log không chứa mã, nonce hai bên, cam kết.
    text = caplog.text
    assert not re.search(rf"(?<!\d){code}(?!\d)", text)
    assert format_code(code) not in text
    assert c.nonce.hex() not in text
    assert c.responder_nonce.hex() not in text
    assert c.commitment not in text


# --- P18: hai bên cùng chủ động ------------------------------------------------------------------------


P18_ROUNDS = 10


@integration
@pytest.mark.parametrize("accept_pairing", [True, False], ids=["listen", "pair"])
async def test_p18_yield_rule_always_gives_one_paired_result_per_side(node_factory, accept_pairing):
    """Sửa F1 (review PR-3): luật nhường phải chạy **mỗi lần**, không chỉ khi may mắn.

    Hai node cùng `pair_with` nhau, lặp `P18_ROUNDS` lần. `accept_pairing=False` mô phỏng hai
    `teleport pair` chạy cùng lúc (luật nhường xét trước `accepting`, protocol §5.3). Mỗi lần:
    - cả hai `pair_with` trả `PAIRED`: bên nhỏ hơn với vai `INITIATOR`, bên lớn hơn với vai
      `RESPONDER` (kết quả của phiên trả lời đã thay phiên khởi xướng của nó), không bao giờ
      `PEER_BUSY` hay `CANCELLED`;
    - `on_finished` của mỗi bên được gọi **đúng một lần**, và chính là kết quả `pair_with` trả;
    - `trusted.json` của mỗi bên có máy kia; mỗi bên đúng một lời hỏi, cùng mã.
    """
    tally: list[str] = []
    for i in range(P18_ROUNDS):
        A = await node_factory(f"A{i}", accept_pairing=accept_pairing, answer=True)
        B = await node_factory(f"B{i}", accept_pairing=accept_pairing, answer=True)
        lo, hi = sorted([A, B], key=lambda n: n.id)
        r_lo, r_hi = await asyncio.wait_for(
            asyncio.gather(lo.pairing.pair_with([LOCAL], hi.port), hi.pairing.pair_with([LOCAL], lo.port)),
            20,
        )
        await asyncio.sleep(0.1)  # để lộ lời gọi on_finished thừa tới muộn (nếu có)
        tally.append(f"{r_lo.outcome}/{r_hi.outcome}/{r_hi.role}")
        where = f"lần {i + 1}/{P18_ROUNDS}, các lần đến giờ: {tally}"

        assert r_lo.outcome == PairingOutcome.PAIRED, where
        assert r_lo.role == Role.INITIATOR, where
        assert r_lo.peer_device_id == hi.id, where
        assert r_hi.outcome == PairingOutcome.PAIRED, where
        assert r_hi.role == Role.RESPONDER, where
        assert r_hi.peer_device_id == lo.id, where

        assert lo.rec.finished == [r_lo], (where, outcomes(lo.rec))
        assert hi.rec.finished == [r_hi], (where, outcomes(hi.rec))

        assert TrustStore.load(lo.dir).get(hi.id) is not None, where
        assert TrustStore.load(hi.dir).get(lo.id) is not None, where
        assert len(lo.rec.prompts) == 1 and len(hi.rec.prompts) == 1, where
        assert lo.rec.prompts[0].code == hi.rec.prompts[0].code, where
        assert gates_idle(A, B), where

        await A.close()
        await B.close()


# --- P19–P23: dữ liệu độc ------------------------------------------------------------------------------


# Message sai gửi tới B khi B đang chờ `pair_nonce` (AWAIT_NONCE).
BAD_AFTER_COMMIT = {
    "rac": lambda c, ident: c.send_bytes(HEADER.pack(9) + b"khong-json"[:9]),
    "hello_lan_hai": lambda c, ident: c.send_dict(
        {
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "device_id": ident.device_id,
            "name": "Raw",
            "app_version": "0.0.0",
            "os": "windows",
        }
    ),
    # pair_reveal / pair_commit chỉ bên trả lời được gửi.
    "reveal_sai_vai": lambda c, ident: c.send_dict({"type": "pair_reveal", "nonce": secrets.token_hex(32)}),
    "commit_sai_vai": lambda c, ident: c.send_dict(
        {"type": "pair_commit", "commitment": secrets.token_hex(32)}
    ),
    "request_lan_hai": lambda c, ident: c.send_dict({"type": "pair_request", "cert": ident.cert_pem()}),
    "confirm_truoc_nonce": lambda c, ident: c.send_dict({"type": "pair_confirm"}),
    "type_la": lambda c, ident: c.send_dict({"type": "chao_tu_tuong_lai"}),
    "challenge_cu": lambda c, ident: c.send_dict({"type": "pair_challenge", "nonce": secrets.token_hex(32)}),
    "nonce_sai_kieu": lambda c, ident: c.send_dict({"type": "pair_nonce", "nonce": 5}),
    "nonce_hoa": lambda c, ident: c.send_dict({"type": "pair_nonce", "nonce": "AB" * 32}),
    "nonce_ngan": lambda c, ident: c.send_dict({"type": "pair_nonce", "nonce": "ab" * 31}),
    "header_qua_lon": lambda c, ident: c.send_bytes(HEADER.pack(16 * 1024 + 1)),
}


@integration
@pytest.mark.parametrize("case", list(BAD_AFTER_COMMIT))
async def test_p19_responder_bad_message_after_commit(node_factory, raw_pairing_client, tmp_path, case):
    B = await node_factory("B", accept_pairing=True, answer=True)
    raw_id = load_or_create_identity(tmp_path / "raw")
    c = await raw_pairing_client(B.port)
    await c.begin(raw_id)
    await c.read_commit()
    BAD_AFTER_COMMIT[case](c, raw_id)
    await c.drain()

    msgs = await c.read_until_closed(5)
    # Không bao giờ có pair_reveal: n_B không lộ khi chưa có pair_nonce hợp lệ.
    assert all(m.get("type") != "pair_reveal" for m in msgs), msgs
    if case != "header_qua_lon":
        assert msgs == [INVALID], msgs
    await B.rec.wait_finished(1, timeout=3)
    assert outcomes(B.rec) == ["protocol_error"]
    assert B.pairing.gate.failures(LOCAL) == 1
    assert B.rec.prompts == []
    assert B.store.devices() == []
    assert B.pairing.gate.active is None


def _serve_pairing_script(identity, bad_bytes: bytes, received: list):
    """Kịch bản FakeServer: hello thật, đọc hello + pair_request của client, gửi `bad_bytes`, ghi phản hồi."""

    async def script(reader, writer):
        try:
            writer.write(
                encode_message(
                    {
                        "type": "hello",
                        "protocol": PROTOCOL_VERSION,
                        "device_id": identity.device_id,
                        "name": "Srv",
                        "app_version": "0.0.0",
                        "os": "linux",
                    }
                )
            )
            await writer.drain()
            await read_message(reader, 16384)  # hello của client
            await read_message(reader, 16384)  # pair_request
            writer.write(bad_bytes)
            await writer.drain()
            while (msg := await read_message(reader, 16384)) is not None:
                received.append(msg)
        except Exception:
            pass

    return script


# Message sai gửi tới A khi A đang chờ `pair_commit` (AWAIT_COMMIT).
BAD_FOR_INITIATOR = {
    "commit_hong": encode_message({"type": "pair_commit", "commitment": "zz"}),
    "commit_hoa": encode_message({"type": "pair_commit", "commitment": "AB" * 32}),
    "confirm_som": encode_message({"type": "pair_confirm"}),
    "rac": HEADER.pack(4) + b"\xff\x00ab",
    "reveal_truoc_commit": encode_message({"type": "pair_reveal", "nonce": "ab" * 32}),
    "nonce_sai_vai": encode_message({"type": "pair_nonce", "nonce": "ab" * 32}),
    "challenge_cu": encode_message({"type": "pair_challenge", "nonce": "ab" * 32}),
}


@integration
@pytest.mark.parametrize("case", list(BAD_FOR_INITIATOR))
async def test_p20_initiator_bad_message_from_server(node_factory, fake_server, tmp_path, case):
    A = await node_factory("A", answer=True)
    srv = load_or_create_identity(tmp_path / "srv")
    received: list = []
    port = await fake_server(srv, _serve_pairing_script(srv, BAD_FOR_INITIATOR[case], received))
    result = await asyncio.wait_for(A.pairing.pair_with([LOCAL], port), 10)
    assert result.outcome == PairingOutcome.PROTOCOL_ERROR
    await wait_until(lambda: INVALID in received, 3, "server chưa nhận pair_abort{invalid}")
    # A chưa nhận pair_commit hợp lệ thì không được gửi n_A.
    assert all(m.get("type") != "pair_nonce" for m in received), received
    assert A.rec.prompts == []
    assert A.rec.finished == [result]
    assert A.pairing.gate.active is None


def _fake_responder_script(identity, mode: str, log: list, initiator_fp: str | None = None):
    """Bên trả lời giả (FakeServer) theo luồng mới; `log` ghi mọi message nó nhận sau hello của mình.

    mode:
    - "honest": cam kết n_B bằng fp của mình, tiết lộ đúng n_B (đối chứng).
    - "doi_n_b": cam kết n_B, thấy n_A rồi tiết lộ n_B khác (lái mã).
    - "cam_ket_fp_khoi_xuong": cam kết bằng fp của A thay vì fp của mình, tiết lộ đúng nonce.
    - "reveal_truoc_commit": gửi pair_reveal ngay sau pair_request, không có pair_commit.
    """
    n_b = secrets.token_bytes(32)
    n_b_other = secrets.token_bytes(32)
    state: dict = {"n_b": n_b, "n_b_other": n_b_other}

    async def script(reader, writer):
        try:
            writer.write(
                encode_message(
                    {
                        "type": "hello",
                        "protocol": PROTOCOL_VERSION,
                        "device_id": identity.device_id,
                        "name": "Srv",
                        "app_version": "0.0.0",
                        "os": "linux",
                    }
                )
            )
            await writer.drain()
            hello = await read_message(reader, 16384)
            log.append(hello)
            request = await read_message(reader, 16384)
            log.append(request)
            if mode == "reveal_truoc_commit":
                writer.write(encode_message({"type": "pair_reveal", "nonce": n_b.hex()}))
            else:
                fp = identity.fingerprint
                if mode == "cam_ket_fp_khoi_xuong":
                    fp = initiator_fp
                commitment = ref_commitment(fp, n_b)
                state["commitment"] = commitment
                writer.write(encode_message({"type": "pair_commit", "commitment": commitment}))
                await writer.drain()
                nonce_msg = await read_message(reader, 16384)
                log.append(nonce_msg)
                state["n_a"] = nonce_msg["nonce"]
                revealed = n_b_other if mode == "doi_n_b" else n_b
                writer.write(encode_message({"type": "pair_reveal", "nonce": revealed.hex()}))
            await writer.drain()
            while (msg := await read_message(reader, 16384)) is not None:
                log.append(msg)
        except Exception:
            pass

    script.state = state  # type: ignore[attr-defined]
    return script


@integration
@pytest.mark.parametrize("mode", ["doi_n_b", "cam_ket_fp_khoi_xuong", "reveal_truoc_commit"])
async def test_initiator_rejects_fake_responder_without_prompt(
    node_factory, fake_server, tmp_path, caplog, mode
):
    """Bên trả lời giả lái mã (đổi n_B sau khi thấy n_A, cam kết sai fp, reveal trước commit):
    `pair_with` → protocol_error, A không hiện lời hỏi, bên giả nhận `pair_abort{invalid}`.
    `pair_request` A gửi chỉ có `cert`; log không chứa nonce hay cam kết."""
    caplog.set_level(logging.DEBUG)
    A = await node_factory("A", answer=True)
    srv = load_or_create_identity(tmp_path / "srv")
    log: list = []
    script = _fake_responder_script(srv, mode, log, initiator_fp=A.identity.fingerprint)
    port = await fake_server(srv, script)
    result = await asyncio.wait_for(A.pairing.pair_with([LOCAL], port), 10)
    assert result.outcome == PairingOutcome.PROTOCOL_ERROR
    await wait_until(lambda: INVALID in log, 3, "bên giả chưa nhận pair_abort{invalid}")
    assert A.rec.prompts == []
    assert A.rec.finished == [result]
    assert A.store.devices() == []
    assert A.pairing.gate.active is None

    # Message đầu tiên sau hello: pair_request chỉ có cert (không nonce, không cam kết).
    assert log[0]["type"] == "hello"
    assert log[1] == {"type": "pair_request", "cert": A.identity.cert_pem()}
    if mode == "reveal_truoc_commit":
        # Chưa có pair_commit thì A không gửi n_A.
        assert all(m.get("type") != "pair_nonce" for m in log), log
    else:
        assert log[2]["type"] == "pair_nonce"

    text = caplog.text
    secrets_seen = [script.state["n_b"].hex(), script.state["n_b_other"].hex()]
    if "commitment" in script.state:
        secrets_seen.append(script.state["commitment"])
    if "n_a" in script.state:
        secrets_seen.append(script.state["n_a"])
    for value in secrets_seen:
        assert value not in text


@integration
async def test_initiator_with_honest_fake_responder_prompts_with_spec_code(
    node_factory, fake_server, tmp_path
):
    """Đối chứng cho bên giả ở trên: cùng kịch bản nhưng trung thực → A hiện mã đúng công thức."""
    A = await node_factory("A", answer=False)
    srv = load_or_create_identity(tmp_path / "srv")
    log: list = []
    script = _fake_responder_script(srv, "honest", log)
    port = await fake_server(srv, script)
    result = await asyncio.wait_for(A.pairing.pair_with([LOCAL], port), 10)
    assert result.outcome == PairingOutcome.USER_REJECTED
    assert len(A.rec.prompts) == 1
    n_a = bytes.fromhex(script.state["n_a"])
    expected = pairing_code(A.identity.fingerprint, srv.fingerprint, n_a, script.state["n_b"])
    assert A.rec.prompts[0].code == expected
    assert log[1] == {"type": "pair_request", "cert": A.identity.cert_pem()}
    await wait_until(lambda: {"type": "pair_abort", "reason": "user"} in log, 3, "chưa nhận pair_abort{user}")


@integration
@pytest.mark.parametrize("cert_kind", ["ca", "pem_rac"])
async def test_p21_bad_cert_in_pair_request(node_factory, raw_pairing_client, app_helpers, cert_kind):
    B = await node_factory("B", accept_pairing=True, answer=True)
    device_id = app_helpers.new_device_id()
    pem = app_helpers.fake_cert_with_cn(device_id, ca=True) if cert_kind == "ca" else GARBAGE_PEM
    c = await raw_pairing_client(B.port)
    c.send_dict(app_helpers.hello_dict(device_id))
    c.send_dict({"type": "pair_request", "cert": pem})
    await c.drain()
    msgs = await c.read_until_closed(5)
    assert msgs == [INVALID], msgs
    await B.rec.wait_finished(1)
    assert outcomes(B.rec) == ["protocol_error"]
    assert B.rec.prompts == []
    assert B.store.devices() == []


@integration
async def test_p22_cert_cn_differs_from_hello(node_factory, raw_pairing_client, app_helpers, tmp_path):
    B = await node_factory("B", accept_pairing=True, answer=True)
    real = load_or_create_identity(tmp_path / "x")
    c = await raw_pairing_client(B.port)
    c.send_dict(app_helpers.hello_dict(app_helpers.new_device_id()))
    c.send_dict({"type": "pair_request", "cert": real.cert_pem()})
    await c.drain()
    msgs = await c.read_until_closed(5)
    assert msgs == [INVALID], msgs
    await B.rec.wait_finished(1)
    assert outcomes(B.rec) == ["protocol_error"]
    assert B.rec.prompts == []


@integration
async def test_p23_silent_after_request_times_out(node_factory, raw_pairing_client, tmp_path, fast):
    B = await node_factory("B", accept_pairing=True, answer=True)
    raw_id = load_or_create_identity(tmp_path / "raw")
    c = await raw_pairing_client(B.port)
    await c.begin(raw_id)
    await c.read_commit()
    t0 = time.monotonic()
    msg = await c.read(timeout=fast.MESSAGE_TIMEOUT + fast.SLACK)
    waited = time.monotonic() - t0
    assert msg == {"type": "pair_abort", "reason": "timeout"}
    assert 0.8 * fast.MESSAGE_TIMEOUT <= waited <= fast.MESSAGE_TIMEOUT + fast.SLACK
    await B.rec.wait_finished(1)
    assert outcomes(B.rec) == ["timeout"]
    assert B.rec.prompts == []
    await wait_until(lambda: B.pairing.gate.active is None, 1)


GATE_FREE_AFTER_FAILURE = 1.5  # sửa F4: gửi pair_abort + đóng tổng 1 s, cộng 0,5 s cho máy chậm


@integration
@pytest.mark.parametrize("case", ["confirm_truoc_nonce", "rac"])
async def test_failed_session_frees_gate_quickly_when_peer_never_closes(
    node_factory, raw_pairing_client, tmp_path, monkeypatch, case
):
    """Sửa F4 (tan-cong PR-3): phiên hỏng mà máy kia không đọc, không đóng socket.

    Trước đây node đóng gọn chờ tới `CLOSE_TIMEOUT` (5 s) nên gate bị giữ thêm 5 s. Kết quả
    ≠ PAIRED thì gửi `pair_abort` rồi đóng với giới hạn tổng 1 s, quá thì abort: gate phải
    trống trong ≤ 1,5 s kể từ lúc gửi message sai. Dùng hằng thời gian thật của kết nối
    (`CLOSE_TIMEOUT = 5.0`), vì `fast` rút nó còn 0,5 s và che mất lỗi.
    """
    from teleport.app import pairing as app_pairing
    from teleport.net import connection

    monkeypatch.setattr(connection, "CLOSE_TIMEOUT", 5.0)
    monkeypatch.setattr(app_pairing, "BUSY_REPLY_TIMEOUT", 1.0)
    monkeypatch.setattr(app_pairing, "ABORT_ON_CANCEL_TIMEOUT", 1.0)
    B = await node_factory("B", accept_pairing=True, answer=True)
    raw_id = load_or_create_identity(tmp_path / "raw")
    c = await raw_pairing_client(B.port)
    await c.begin(raw_id)
    await c.read_commit()
    c.writer.transport.pause_reading()  # từ đây không đọc gì nữa (kể cả close_notify), không đóng
    BAD_AFTER_COMMIT[case](c, raw_id)
    await c.drain()
    t0 = time.monotonic()
    await wait_until(
        lambda: B.pairing.gate.active is None, GATE_FREE_AFTER_FAILURE + 1.0, "gate chưa được nhả"
    )
    freed_after = time.monotonic() - t0
    assert freed_after <= GATE_FREE_AFTER_FAILURE, f"gate bị giữ {freed_after:.2f} s sau phiên hỏng"
    await B.rec.wait_finished(1, timeout=1)
    assert outcomes(B.rec) == ["protocol_error"]
    assert B.store.devices() == []


# --- P24–P27: decide, callback, lỗi lưu, log ----------------------------------------------------------


@integration
async def test_p24_decide_rules(node_factory):
    B = await node_factory("B", accept_pairing=True)
    A = await node_factory("A")
    task = asyncio.create_task(A.pairing.pair_with([LOCAL], B.port), name="test.pair")
    await A.rec.wait_prompts()
    await B.rec.wait_prompts()
    sid = B.rec.prompts[0].session_id

    assert B.pairing.decide(sid + 1000, True) is False
    assert B.pairing.decide(A.rec.prompts[0].session_id + 1000, False) is False
    with pytest.raises(ValueError):
        B.pairing.decide(sid, 1)
    assert B.pairing.decide(sid, True) is True
    assert B.pairing.decide(sid, True) is False  # đã quyết rồi
    assert B.pairing.decide(sid, False) is False  # không đổi ý được
    assert A.pairing.decide(A.rec.prompts[0].session_id, True) is True

    result = await asyncio.wait_for(task, 10)
    await B.rec.wait_finished(1)
    assert result.outcome == PairingOutcome.PAIRED
    assert outcomes(B.rec) == ["paired"]
    assert B.pairing.decide(sid, True) is False  # phiên đã xong
    assert A.pairing.decide(A.rec.prompts[0].session_id, False) is False


@integration
async def test_p25_raising_callbacks_do_not_break_session(node_factory, caplog):
    caplog.set_level(logging.DEBUG)
    B = await node_factory(
        "B", accept_pairing=True, answer=True, raise_in_prompt=True, raise_in_finished=True
    )
    A = await node_factory("A", answer=True, raise_in_prompt=True, raise_in_finished=True)
    result = await pair(A, B)
    await B.rec.wait_finished(1)
    assert result.outcome == PairingOutcome.PAIRED
    assert outcomes(B.rec) == ["paired"]
    assert len(A.rec.finished) == 1 and len(B.rec.finished) == 1
    assert A.store.get(B.id) is not None and B.store.get(A.id) is not None
    logged = [
        r
        for r in caplog.records
        if r.name.startswith("teleport.app") and r.levelno >= logging.ERROR and r.exc_info is not None
    ]
    assert len(logged) >= 4, [r.getMessage() for r in logged]
    assert gates_idle(A, B)


@integration
async def test_p26_store_add_failure_gives_save_failed(node_factory, monkeypatch):
    B = await node_factory("B", accept_pairing=True, answer=True)
    A = await node_factory("A", answer=True)

    def failing_add(*args, **kwargs):
        raise TrustStoreError("giả lập không ghi được trusted.json")

    monkeypatch.setattr(B.store, "add", failing_add)
    result = await pair(A, B)
    await B.rec.wait_finished(1)
    assert outcomes(B.rec) == ["save_failed"]
    assert B.rec.finished[0].device is None
    assert result.outcome == PairingOutcome.PAIRED
    assert B.pairing.gate.failures(LOCAL) == 0  # gate finish với PAIRED
    assert B.store.get(A.id) is None
    assert gates_idle(A, B)


@integration
async def test_p27_logs_never_contain_code_or_pem(node_factory, caplog):
    caplog.set_level(logging.DEBUG)
    B = await node_factory("B", accept_pairing=True, answer=True)
    A = await node_factory("A", answer=True)
    assert (await pair(A, B)).outcome == PairingOutcome.PAIRED
    await B.rec.wait_finished(1)
    await asyncio.sleep(0.1)
    text = caplog.text
    code = A.rec.prompts[0].code
    assert not re.search(rf"(?<!\d){code}(?!\d)", text)
    assert format_code(code) not in text
    assert "BEGIN CERTIFICATE" not in text
    for ident in (A.identity, B.identity):
        body_line = ident.cert_pem().splitlines()[1]
        assert body_line not in text
    # Có log INFO kết thúc phiên (3.3), chỉ ghi device_id[:8].
    infos = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and r.name.startswith("teleport.app")
    ]
    assert any(B.id[:8] in m for m in infos), infos
    assert not any(B.id in m for m in infos), "log chỉ được ghi device_id[:8]"


# --- Các ca thêm theo đặc tả (không đánh số trong bảng 4.5) -------------------------------------------


async def _close_after_tls(reader, writer):
    writer.close()


def _hello_script(identity, **overrides):
    async def script(reader, writer):
        hello = {
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "device_id": identity.device_id,
            "name": "Srv",
            "app_version": "0.0.0",
            "os": "linux",
        }
        hello.update(overrides)
        writer.write(encode_message(hello))
        await writer.drain()
        try:
            await asyncio.sleep(10)
        finally:
            writer.close()

    return script


@integration
@pytest.mark.parametrize("case", ["rejected", "version_mismatch", "identity_mismatch"])
async def test_connect_errors_map_to_outcomes(node_factory, fake_server, tmp_path, app_helpers, case):
    """3.1 bước 5: Rejected / VersionMismatch / IdentityMismatch → kết quả tương ứng."""
    A = await node_factory("A")
    srv = load_or_create_identity(tmp_path / "srv")
    if case == "rejected":
        script = _close_after_tls
    elif case == "version_mismatch":
        script = _hello_script(srv, protocol=PROTOCOL_VERSION + 1)
    else:
        script = _hello_script(srv, device_id=app_helpers.new_device_id())
    port = await fake_server(srv, script)
    result = await asyncio.wait_for(A.pairing.pair_with([LOCAL], port), 10)
    assert result.outcome == PairingOutcome(case)
    assert A.rec.finished == [result]
    assert A.rec.prompts == []
    assert A.pairing.gate.active is None


@integration
async def test_pair_with_while_busy_returns_busy_without_connecting(node_factory, fake_server, tmp_path):
    """3.1 bước 3: đang có phiên → BUSY, session_id None, không kết nối, vẫn gọi on_finished."""
    B = await node_factory("B", accept_pairing=True)
    A = await node_factory("A")
    task = asyncio.create_task(A.pairing.pair_with([LOCAL], B.port), name="test.pair")
    await A.rec.wait_prompts()

    connections = []

    async def count(reader, writer):
        connections.append(1)
        writer.close()

    srv = load_or_create_identity(tmp_path / "srv")
    port = await fake_server(srv, count)
    busy = await asyncio.wait_for(A.pairing.pair_with([LOCAL], port), 5)
    assert busy.outcome == PairingOutcome.BUSY
    assert busy.session_id is None
    assert busy.role == Role.INITIATOR
    assert (busy.address, busy.port) == (LOCAL, port)
    await asyncio.sleep(0.2)
    assert connections == []
    assert busy in A.rec.finished

    A.pairing.decide(A.rec.prompts[0].session_id, False)
    first = await asyncio.wait_for(task, 5)
    assert first.outcome == PairingOutcome.USER_REJECTED
    assert len(A.rec.finished) == 2


@integration
async def test_incoming_after_service_close_is_busy(node_factory):
    """2.4 `close()`: sau đó `handle_incoming` trả lời BUSY."""
    A = await node_factory("A", accept_pairing=True, answer=True)
    B = await node_factory("B", answer=True)
    await A.pairing.close()
    result = await pair(B, A)
    assert result.outcome == PairingOutcome.PEER_BUSY
    await asyncio.sleep(0.2)
    assert A.rec.prompts == [] and A.rec.finished == []


@integration
async def test_hard_cancel_of_pair_with(node_factory):
    """3.1 bước 8: task bị hủy → pair_abort{user}, on_finished(CANCELLED), rồi CancelledError."""
    B = await node_factory("B", accept_pairing=True)
    A = await node_factory("A")
    task = asyncio.create_task(A.pairing.pair_with([LOCAL], B.port), name="test.pair")
    await A.rec.wait_prompts()
    await B.rec.wait_prompts()
    t0 = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert outcomes(A.rec) == ["cancelled"]
    assert A.pairing.gate.active is None
    await B.rec.wait_finished(1, timeout=3)
    assert outcomes(B.rec) == ["peer_rejected"]
    assert B.rec.finished_times[0] - t0 <= 1.5


@integration
async def test_cancel_all_while_connecting(node_factory, fake_server, tmp_path, fast):
    """2.4 `cancel_all`: hủy cả phiên khởi xướng đang open_pairing → CANCELLED, nhanh hơn hết giờ."""
    A = await node_factory("A")

    async def silent(reader, writer):
        await asyncio.sleep(30)

    port = await fake_server(None, silent, use_tls=False)  # TCP nhận nhưng không bao giờ bắt tay TLS
    task = asyncio.create_task(A.pairing.pair_with([LOCAL], port), name="test.pair")
    await asyncio.sleep(0.2)
    assert not task.done()
    t0 = time.monotonic()
    await asyncio.wait_for(A.pairing.cancel_all(), 5)
    result = await asyncio.wait_for(task, 5)
    assert result.outcome == PairingOutcome.CANCELLED
    assert time.monotonic() - t0 < fast.OPENING_TIMEOUT * 0.8
    assert A.rec.finished == [result]
    assert A.pairing.gate.active is None


@integration
async def test_all_background_tasks_are_named(node_factory):
    """3.8 "Mọi task có tên (`teleport.pairing*`, `teleport.node*`)"; kết nối là `teleport.connection*`."""
    B = await node_factory("B", accept_pairing=True)
    A = await node_factory("A")
    task = asyncio.create_task(A.pairing.pair_with([LOCAL], B.port), name="test.pair")
    await A.rec.wait_prompts()
    await B.rec.wait_prompts()
    await asyncio.sleep(0.05)
    me = asyncio.current_task()
    asyncio_dir = os.path.dirname(asyncio.__file__)

    def internal(t: asyncio.Task) -> bool:
        # Task của chính asyncio (ví dụ IocpProactor.accept trên Windows), không phải của Teleport.
        code = getattr(t.get_coro(), "cr_code", None)
        return code is not None and os.path.dirname(code.co_filename) == asyncio_dir

    tasks = [
        t
        for t in asyncio.all_tasks()
        if t is not me and not t.get_name().startswith("test.") and not internal(t)
    ]
    assert tasks, "phải có task của phiên đang chạy"
    unnamed = [f"{t.get_name()}: {t.get_coro()!r}" for t in tasks if not t.get_name().startswith("teleport.")]
    assert unnamed == [], unnamed
    A.pairing.decide(A.rec.prompts[0].session_id, False)
    await asyncio.wait_for(task, 5)


@integration
async def test_pair_confirm_flood_is_rejected_after_second_frame(node_factory, raw_pairing_client, tmp_path):
    """3.3/3.8: message kế tiếp chỉ đọc khi message trước xử lý xong; `pair_confirm` thứ hai → invalid."""
    B = await node_factory("B", accept_pairing=True)
    raw_id = load_or_create_identity(tmp_path / "raw")
    c = await raw_pairing_client(B.port)
    await c.begin(raw_id)
    await c.run_to_code(B.identity.fingerprint)
    c.send_bytes(encode_message({"type": "pair_confirm"}) * 500)
    await c.drain()
    msgs = await c.read_until_closed(5)
    assert msgs == [INVALID], msgs
    await B.rec.wait_finished(1)
    assert outcomes(B.rec) == ["protocol_error"]
    assert B.store.devices() == []
    assert B.pairing.gate.active is None


# =================================================================================================
# PR-4 (đặc tả PR-4 mục 4.7 PA1; mục 4.5 NL13 = backlog b)
# =================================================================================================

PA1_ROUNDS = 5
TEST_NET = "192.0.2.1"  # TEST-NET-1: không có máy nào; kết nối treo hoặc lỗi ngay tùy mạng


@integration
async def test_pa1_yield_rule_with_several_addresses(node_factory, fast):
    """Backlog d: bên lớn hơn thử `[192.0.2.1, 127.0.0.1]`, yêu cầu của bên nhỏ tới từ 127.0.0.1
    (địa chỉ phụ của phiên khởi xướng) → vẫn coi là hai bên cùng chủ động và bên lớn nhường.

    Kỳ vọng như P18: mỗi bên đúng một lời hỏi, cùng mã, hai kho có nhau, mỗi bên đúng một kết quả
    PAIRED. Không phải đợi hết OPENING_TIMEOUT của địa chỉ treo.
    """
    tally: list[str] = []
    for i in range(PA1_ROUNDS):
        A = await node_factory(f"A{i}", accept_pairing=True, answer=True)
        B = await node_factory(f"B{i}", accept_pairing=True, answer=True)
        lo, hi = sorted([A, B], key=lambda n: n.id)
        t0 = time.monotonic()
        r_lo, r_hi = await asyncio.wait_for(
            asyncio.gather(
                lo.pairing.pair_with([LOCAL], hi.port),
                hi.pairing.pair_with([TEST_NET, LOCAL], lo.port),
            ),
            20,
        )
        took = time.monotonic() - t0
        await asyncio.sleep(0.1)
        tally.append(f"{r_lo.outcome}/{r_hi.outcome}/{r_hi.role}")
        where = f"lần {i + 1}/{PA1_ROUNDS}, các lần đến giờ: {tally}"
        assert r_lo.outcome == PairingOutcome.PAIRED, where
        assert r_hi.outcome == PairingOutcome.PAIRED, where
        assert r_lo.role == Role.INITIATOR, where
        assert r_hi.role == Role.RESPONDER, where
        assert lo.rec.finished == [r_lo], (where, outcomes(lo.rec))
        assert hi.rec.finished == [r_hi], (where, outcomes(hi.rec))
        assert len(lo.rec.prompts) == 1 and len(hi.rec.prompts) == 1, where
        assert lo.rec.prompts[0].code == hi.rec.prompts[0].code, where
        assert TrustStore.load(lo.dir).get(hi.id) is not None, where
        assert TrustStore.load(hi.dir).get(lo.id) is not None, where
        assert gates_idle(A, B), where
        assert took < fast.OPENING_TIMEOUT, f"{where}: mất {took:.2f} s (đợi hết thời gian của {TEST_NET}?)"
        await A.close()
        await B.close()


NL13_TRIES = 5


@integration
async def test_nl13_cancel_pair_with_while_waiting_for_replacement(node_factory):
    """Backlog b: task `pair_with` của bên lớn hơn đã nhường (gate có slot RESPONDER) bị hủy.

    → task raise CancelledError; phiên thay thế kết thúc CANCELLED (đúng một on_finished); bên nhỏ
    hơn nhận PEER_REJECTED với `peer_reason == "user"`. Cuộc đua đã biết (bên lớn nhận `busy` trước
    khi kịp nhường) thì thử lại, tối đa NL13_TRIES lần.
    """
    races: list[str] = []
    for i in range(NL13_TRIES):
        A = await node_factory(f"A{i}", accept_pairing=True)
        B = await node_factory(f"B{i}", accept_pairing=True)
        lo, hi = sorted([A, B], key=lambda n: n.id)
        t_lo = asyncio.create_task(lo.pairing.pair_with([LOCAL], hi.port), name="test.pair-lo")
        t_hi = asyncio.create_task(hi.pairing.pair_with([LOCAL], lo.port), name="test.pair-hi")
        await wait_until(
            lambda hi=hi, t_hi=t_hi: (
                t_hi.done()
                or (hi.pairing.gate.active is not None and hi.pairing.gate.active.role == Role.RESPONDER)
            ),
            5,
            "bên lớn chưa nhường",
        )
        if t_hi.done():
            races.append(str(t_hi.result().outcome))
            t_lo.cancel()
            await asyncio.gather(t_lo, return_exceptions=True)
            await A.close()
            await B.close()
            continue
        await hi.rec.wait_prompts()
        await lo.rec.wait_prompts()
        assert not t_hi.done(), "pair_with của bên đã nhường phải chờ phiên thay thế"
        t_hi.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(t_hi, 5)
        await hi.rec.wait_finished(1, timeout=3)
        await asyncio.sleep(0.2)
        assert outcomes(hi.rec) == ["cancelled"], outcomes(hi.rec)
        assert hi.rec.finished[0].role == Role.RESPONDER
        r_lo = await asyncio.wait_for(t_lo, 5)
        assert r_lo.outcome == PairingOutcome.PEER_REJECTED
        assert r_lo.peer_reason == "user"
        assert lo.rec.finished == [r_lo]
        assert TrustStore.load(hi.dir).get(lo.id) is None
        assert TrustStore.load(lo.dir).get(hi.id) is None
        assert gates_idle(A, B)
        return
    pytest.skip(f"cuộc đua đã biết xảy ra cả {NL13_TRIES} lần: {races}")
