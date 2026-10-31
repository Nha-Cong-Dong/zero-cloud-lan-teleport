"""C1 (spec-dot-c mục 1.2.2, 1.5; mục 11 M3): `PairingService.set_hello` và `before_save`.

`before_save(device_id)` được gọi ngay trước `store.add` trong cùng khối đồng bộ; ném lỗi thì kết quả
`SAVE_FAILED` và `trusted.json` không đổi; không bao giờ được gọi khi kết quả khác `PAIRED`.

Node tự truyền `before_save` của nó (A5: đặt lại cài đặt theo thiết bị). Để chen vào, test thay
`teleport.app.node.PairingService` (tên toàn cục của module, như các seam khác ở architecture §6) bằng
một hàm dựng bọc `before_save` theo tên node (`hello.name`).
"""

import asyncio

import pytest

from teleport.app import node as node_mod
from teleport.app.pairing import PairingOutcome, PairingService
from teleport.core.messages import PROTOCOL_VERSION, Hello
from teleport.trust.identity import load_or_create_identity
from teleport.trust.store import TrustStore

pytestmark = [
    pytest.mark.usefixtures("no_leaks"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
integration = pytest.mark.integration

LOCAL = "127.0.0.1"


async def pair(a, b, timeout: float = 15.0):
    return await asyncio.wait_for(a.pairing.pair_with([LOCAL], b.port), timeout)


def outcomes(rec) -> list[str]:
    return [str(f.outcome) for f in rec.finished]


@pytest.fixture
def wrap_before_save(monkeypatch):
    """`wrap_before_save({"B": fn})`: node tên "B" nhận `before_save` = `fn(original, device_id)`.

    `original` là `before_save` mà Node tự truyền (có thể None). Trả danh sách lời gọi `(tên, device_id)`.
    """
    calls: list[tuple[str, str]] = []
    real = node_mod.PairingService

    def install(wrappers: dict):
        def factory(*args, **kwargs):
            name = kwargs["hello"].name
            original = kwargs.get("before_save")

            def before_save(device_id: str) -> None:
                calls.append((name, device_id))
                wrapper = wrappers.get(name)
                if wrapper is not None:
                    wrapper(original, device_id)
                elif original is not None:
                    original(device_id)

            kwargs["before_save"] = before_save
            return real(*args, **kwargs)

        monkeypatch.setattr(node_mod, "PairingService", factory)
        return calls

    return install


# --- Unit: set_hello --------------------------------------------------------------------------------


@pytest.fixture
def parts(tmp_path):
    identity = load_or_create_identity(tmp_path / "svc")
    hello = Hello(PROTOCOL_VERSION, identity.device_id, "Svc", "0.0.0", "windows")
    store = TrustStore.load(tmp_path / "svc")
    return identity, hello, store


def make_service(parts, **kwargs) -> PairingService:
    identity, hello, store = parts
    return PairingService(
        identity=identity,
        hello=hello,
        store=store,
        on_prompt=lambda p: None,
        on_finished=lambda r: None,
        on_trust_changed=lambda: None,
        **kwargs,
    )


async def test_service_accepts_before_save_keyword(parts):
    svc = make_service(parts, before_save=lambda device_id: None)
    await svc.close()
    svc = make_service(parts, before_save=None)
    await svc.close()


async def test_service_set_hello_checks_like_constructor(parts, tmp_path):
    identity, _, _ = parts
    svc = make_service(parts)
    try:
        other = load_or_create_identity(tmp_path / "khac")
        with pytest.raises(ValueError):
            svc.set_hello(Hello(PROTOCOL_VERSION, other.device_id, "Gia", "0.0.0", "windows"))
        with pytest.raises(ValueError):
            svc.set_hello({"name": "khong-phai-hello"})  # type: ignore[arg-type]
        assert (
            svc.set_hello(Hello(PROTOCOL_VERSION, identity.device_id, "Ten-Moi", "0.0.0", "windows")) is None
        )
    finally:
        await svc.close()


# --- Integration: before_save --------------------------------------------------------------------


@integration
@pytest.mark.parametrize("failing", ["B", "A"], ids=["ben-tra-loi", "ben-khoi-xuong"])
async def test_before_save_raises_gives_save_failed_and_trusted_unchanged(
    node_factory, wrap_before_save, failing
):
    """Callback ném lỗi → bên đó `SAVE_FAILED`, không ghi `trusted.json` (byte trên đĩa không đổi);
    bên kia vẫn lưu bình thường."""

    def boom(original, device_id):
        raise OSError(28, "giả lập không ghi được settings.json")

    calls = wrap_before_save({failing: boom})
    B = await node_factory("B", accept_pairing=True, answer=True)
    A = await node_factory("A", answer=True)
    victim, other = (B, A) if failing == "B" else (A, B)
    trusted = victim.dir / "trusted.json"
    before = trusted.read_bytes() if trusted.exists() else None

    result = await pair(A, B)
    await B.rec.wait_finished(1)
    await A.rec.wait_finished(1)

    assert outcomes(victim.rec) == ["save_failed"]
    assert victim.rec.finished[0].device is None
    assert outcomes(other.rec) == ["paired"]
    if failing == "A":
        assert result.outcome == PairingOutcome.SAVE_FAILED
    assert victim.store.get(other.id) is None
    assert (trusted.read_bytes() if trusted.exists() else None) == before
    assert TrustStore.load(victim.dir).get(other.id) is None
    assert other.store.get(victim.id) is not None
    assert (victim.label, other.id) in calls


@integration
async def test_before_save_called_once_per_side_with_peer_id_when_paired(node_factory, wrap_before_save):
    calls = wrap_before_save({})
    B = await node_factory("B", accept_pairing=True, answer=True)
    A = await node_factory("A", answer=True)
    result = await pair(A, B)
    await B.rec.wait_finished(1)
    assert result.outcome == PairingOutcome.PAIRED
    assert sorted(calls) == sorted([("A", B.id), ("B", A.id)])


@integration
@pytest.mark.parametrize("who_rejects", ["A", "B", "nobody-answers-busy"])
async def test_before_save_not_called_when_outcome_is_not_paired(node_factory, wrap_before_save, who_rejects):
    """M3: kết quả khác `PAIRED` (bên trả lời từ chối, bên khởi xướng từ chối, máy kia không nhận
    ghép đôi) → `before_save` không bao giờ được gọi, ở cả hai bên."""
    calls = wrap_before_save({})
    if who_rejects == "nobody-answers-busy":
        B = await node_factory("B", accept_pairing=False, answer=True)
        A = await node_factory("A", answer=True)
    else:
        B = await node_factory("B", accept_pairing=True, answer=who_rejects != "B")
        A = await node_factory("A", answer=who_rejects != "A")
    result = await pair(A, B)
    assert result.outcome != PairingOutcome.PAIRED
    await asyncio.sleep(0.2)
    assert calls == []
    assert A.store.devices() == [] and B.store.devices() == []
