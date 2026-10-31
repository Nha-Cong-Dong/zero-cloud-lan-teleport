"""C1c (= P4-0b): `TransferEngine.recheck_space`, `CheckSpace.check_id`, `has_accepted_receives`.

Viết từ đặc tả, không từ code: `specs/spec-dot-c.md` mục 4.2 (API), 4.4 (bất biến), 4.6 (tiêu chí xong).
"Đổi…" thư mục nhận được phép khi chưa có lượt nhận nào đã chấp nhận; engine kiểm lại chỗ trống và trần
FAT trên thư mục mới cho các `offer` đang kiểm (`CHECKING`) hay đang hỏi (`ASKING`), bỏ kết quả kiểm cũ
còn đang bay về (theo `check_id`), không hỏi người dùng lần hai. Dây không đổi: `offer` đang hỏi bị
từ chối bằng `reject{no_space}` / `reject{file_too_large}` như lần kiểm đầu (protocol §3.3).

Mọi tên mới (`recheck_space`, `has_accepted_receives`, `CheckSpace.check_id`, tham số `check_id=` của
`space_checked`) chỉ được truy cập lúc chạy, để module thu thập được trước khi code có.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.core.blocks import DIGEST_SIZE
from teleport.core.messages import Accept, Reject

HYPO = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# ---- Phụ trợ ----------------------------------------------------------------------------------


def checks(tx, acts, transfer_id: str | None = None) -> list[Any]:
    """Các `CheckSpace` trong `acts` (lọc theo lượt nếu có)."""
    found = tx.of(acts, tx.tr.CheckSpace)
    return [a for a in found if transfer_id is None or a.transfer_id == transfer_id]


def last_check_id(tx, r) -> int:
    """`check_id` của `CheckSpace` gần nhất engine phát cho lượt của `r`."""
    found = checks(tx, r.all_actions(), r.T)
    assert found, "lượt chưa có CheckSpace nào"
    return found[-1].check_id


def space(r, free: int | None, max_size: int | None = None, *, check_id: int | None = None, T=None):
    """`space_checked` qua bộ lái (now tăng, kiểm G5, ghi nhật ký); `check_id` None thì không truyền."""
    r.clock.now += 0.01
    kw = {} if check_id is None else {"check_id": check_id}
    acts = r.e.space_checked(r.peer, T or r.T, free, max_size, r.clock.now, **kw)
    assert isinstance(acts, list)
    r.log.append(("space_checked", acts))
    return acts


def recheck(tx, r) -> list[Any]:
    acts = r.call("recheck_space")
    tx.check_order(acts)
    return acts


def logged(r) -> list[Any]:
    """Mọi hành động trong nhật ký của `r`, mỗi đối tượng một lần.

    `Recv.to_asking` nối danh sách bằng `+=` vào chính danh sách đã ghi của `control_up`, nên
    `all_actions()` có thể lặp lại một hành động; lọc theo `id` để đếm đúng.
    """
    seen: dict[int, Any] = {}
    for a in r.all_actions():
        seen.setdefault(id(a), a)
    return list(seen.values())


def ask_count(tx, r) -> int:
    return len([a for a in tx.of(logged(r), tx.tr.AskUser) if a.transfer_id == r.T])


def create_count(tx, r) -> int:
    return len([a for a in tx.of(logged(r), tx.tr.CreateTemp) if a.transfer_id == r.T])


def no_effect(tx, acts) -> None:
    """Không hỏi, không gửi gì lên dây, không tạo thư mục tạm, không kết thúc lượt."""
    assert not tx.of(acts, (tx.tr.AskUser, tx.tr.CreateTemp, tx.tr.SendMessage)), acts
    assert not tx.ended(acts), acts


# ---- CheckSpace.check_id ----------------------------------------------------------------------


def test_check_space_has_check_id_defaulting_to_zero(tx):
    cs = tx.tr.CheckSpace(tx.PEER, tx.T1, 10)
    assert cs.check_id == 0
    assert tx.tr.CheckSpace(tx.PEER, tx.T1, 10, 7).check_id == 7  # trường cuối, theo vị trí được


def test_check_ids_strictly_increase_across_transfers_and_rechecks(tx):
    ra = tx.Recv(tx.entries(1))
    rc = tx.Recv(tx.entries(2), T=tx.T2, peer=tx.OTHER, like=ra, cid=2)
    ra.up()
    rc.up()
    seen: list[Any] = []
    seen += ra.send_offer()
    seen += rc.send_offer()
    seen += recheck(tx, ra)
    space(ra, tx.FREE, check_id=last_check_id(tx, ra))
    seen += recheck(tx, ra)
    ids = [a.check_id for a in checks(tx, seen)]
    assert len(ids) == 6, ids  # 2 lần đầu + 2 × 2 lần kiểm lại
    assert ids == sorted(ids) and len(set(ids)) == len(ids), f"check_id phải tăng dần: {ids}"


# ---- recheck_space theo pha -------------------------------------------------------------------


def test_recheck_asking_ok_no_second_askuser(tx):
    r = tx.Recv(tx.entries(2, 1))
    acts = r.to_asking()
    first = tx.one(checks(tx, acts), object)
    assert tx.one(acts, tx.tr.AskUser).transfer_id == r.T

    acts = recheck(tx, r)
    cs = tx.one(checks(tx, acts), object)
    assert (cs.device_id, cs.transfer_id, cs.total_size) == (tx.PEER, r.T, first.total_size)
    assert cs.check_id > first.check_id
    no_effect(tx, acts)
    assert r.e.info(r.key) is not None

    acts = space(r, tx.FREE, check_id=cs.check_id)
    no_effect(tx, acts)
    assert ask_count(tx, r) == 1, "kiểm lại xong không được hỏi người dùng lần hai"
    assert r.e.info(r.key) is not None
    # Hộp hỏi cũ vẫn trả lời được như thường.
    acts = r.call("decide", tx.PEER, r.T, True)
    assert tx.one(acts, tx.tr.CreateTemp).transfer_id == r.T


def test_recheck_asking_exactly_enough_space_counts_own_offer_once(tx):
    """Lần kiểm lại không được trừ chính `offer` đang hỏi (đã nằm trong `promised_bytes`) khỏi chỗ trống:
    đủ đúng bằng tổng kích thước thì vẫn đủ, như lần kiểm đầu."""
    r = tx.Recv(tx.entries(3))
    total = tx.size_of(3)
    r.up()
    r.send_offer()
    assert tx.one(space(r, total), tx.tr.AskUser)
    acts = recheck(tx, r)
    acts = space(r, total, check_id=tx.one(checks(tx, acts), object).check_id)
    no_effect(tx, acts)
    assert r.e.info(r.key) is not None


def test_recheck_asking_no_space_rejects(tx):
    r = tx.Recv(tx.entries(2, 1))
    total = tx.size_of(2) + tx.size_of(1)
    r.to_asking()
    cid = tx.one(checks(tx, recheck(tx, r)), object).check_id
    acts = space(r, total - 1, check_id=cid)
    rej = Reject(transfer_id=r.T, reason="no_space", needed=total, available=total - 1)
    assert tx.sent(acts) == [rej]
    assert not tx.of(acts, (tx.tr.AskUser, tx.tr.CreateTemp))
    end = tx.one(tx.ended(acts), object)
    assert (end.key, end.outcome, end.reason, end.by_peer, end.reject) == (
        r.key,
        tx.tr.Outcome.REJECTED,
        "no_space",
        False,
        rej,
    )
    assert r.e.info(r.key) is None
    # Người dùng bấm Nhận trên hộp đã đóng: không có gì xảy ra.
    assert r.call("decide", tx.PEER, r.T, True) == []
    # Như lần kiểm đầu, `reject` được nhớ: `offer` gửi lại nhận lại đúng `reject` đó.
    assert tx.sent(r.send_offer()) == [rej]


def test_recheck_fat_cap_rejects(tx):
    r = tx.Recv(tx.entries(1, 3, 4))
    r.to_asking()  # thư mục cũ: không có trần
    mx = tx.size_of(2)
    cid = tx.one(checks(tx, recheck(tx, r)), object).check_id
    acts = space(r, tx.FREE, mx, check_id=cid)
    rej = Reject(transfer_id=r.T, reason="file_too_large", file_index=1, max_size=mx)
    assert tx.sent(acts) == [rej]
    end = tx.one(tx.ended(acts), object)
    assert (end.outcome, end.reason, end.reject) == (tx.tr.Outcome.REJECTED, "file_too_large", rej)
    assert not tx.of(acts, (tx.tr.AskUser, tx.tr.CreateTemp))
    assert r.e.info(r.key) is None


def test_recheck_checking_issues_new_check_and_asks_once(tx):
    r = tx.Recv(tx.entries(2))
    r.up()
    first = tx.one(checks(tx, r.send_offer()), object)
    acts = recheck(tx, r)
    cs = tx.one(checks(tx, acts), object)
    assert (cs.transfer_id, cs.total_size) == (r.T, first.total_size)
    assert cs.check_id > first.check_id
    no_effect(tx, acts)
    acts = space(r, tx.FREE, check_id=cs.check_id)
    assert tx.one(acts, tx.tr.AskUser).transfer_id == r.T
    assert ask_count(tx, r) == 1


def test_recheck_running_and_empty_engine_do_nothing(tx):
    e = tx.tr.TransferEngine(tx.ME, receiving=True)
    assert e.recheck_space(1.0) == []
    r = tx.Recv(tx.entries(2))
    r.accept()
    acts = recheck(tx, r)
    assert checks(tx, acts) == []
    assert tx.sent(acts) == [] and not tx.ended(acts)


def test_recheck_ignores_send_transfers(tx):
    s = tx.Send(tx.entries(2))
    s.start(accept=False)
    acts = s.call("recheck_space")
    assert checks(tx, acts) == [] and tx.sent(acts) == []


# ---- check_id: kết quả cũ bị bỏ ---------------------------------------------------------------


@pytest.mark.parametrize("phase", ["checking", "asking"])
def test_stale_space_result_ignored(tx, phase):
    r = tx.Recv(tx.entries(2))
    r.up()
    old = tx.one(checks(tx, r.send_offer()), object).check_id
    if phase == "asking":
        assert tx.one(space(r, tx.FREE, check_id=old), tx.tr.AskUser)
    new = tx.one(checks(tx, recheck(tx, r)), object).check_id
    assert new != old

    before = tx.snapshot(r.e)
    for free, mx in ((0, None), (tx.FREE, 1), (tx.FREE, None)):  # thiếu chỗ, vượt trần, đủ
        assert space(r, free, mx, check_id=old) == [], "kết quả có check_id cũ phải bị bỏ"
        assert tx.snapshot(r.e) == before

    acts = space(r, tx.FREE, check_id=new)
    if phase == "checking":
        assert tx.one(acts, tx.tr.AskUser).transfer_id == r.T
    else:
        no_effect(tx, acts)
    assert ask_count(tx, r) == 1
    # Kết quả cũ tới sau kết quả mới cũng bị bỏ.
    before = tx.snapshot(r.e)
    assert space(r, 0, check_id=old) == []
    assert tx.snapshot(r.e) == before
    assert r.e.info(r.key) is not None


def test_space_result_with_unknown_future_check_id_ignored(tx):
    r = tx.Recv(tx.entries(1))
    r.up()
    cid = tx.one(checks(tx, r.send_offer()), object).check_id
    assert space(r, 0, check_id=cid + 1000) == []
    assert tx.one(space(r, tx.FREE, check_id=cid), tx.tr.AskUser)


def test_space_result_without_check_id_matches_latest(tx):
    """`check_id=None` coi như khớp (test cũ, lời gọi không truyền `check_id`)."""
    r = tx.Recv(tx.entries(1))
    r.up()
    r.send_offer()
    recheck(tx, r)
    assert tx.one(space(r, tx.FREE), tx.tr.AskUser).transfer_id == r.T
    acts = recheck(tx, r)
    acts = space(r, 0)
    assert tx.sent(acts)[0].reason == "no_space"


# ---- Nhận / Từ chối trong lúc đang kiểm lại ----------------------------------------------------


def test_accept_during_recheck_applied_after_ok(tx):
    r = tx.Recv(tx.entries(2))
    r.to_asking()
    cid = tx.one(checks(tx, recheck(tx, r)), object).check_id
    acts = r.call("decide", tx.PEER, r.T, True)
    no_effect(tx, acts)
    assert r.e.has_accepted_receives() is False, "Nhận lúc đang kiểm lại chỉ được ghi nhận"
    # Bấm Nhận lần nữa cũng chỉ ghi nhận.
    no_effect(tx, r.call("decide", tx.PEER, r.T, True))

    acts = space(r, tx.FREE, check_id=cid)
    assert tx.one(acts, tx.tr.CreateTemp).transfer_id == r.T
    assert not tx.of(acts, tx.tr.AskUser)
    assert not tx.ended(acts)
    assert r.e.has_accepted_receives() is True
    acts = r.created()
    assert tx.sent(acts, Accept) == [Accept(transfer_id=r.T)]
    assert create_count(tx, r) == 1
    assert ask_count(tx, r) == 1


def test_accept_during_recheck_dropped_on_reject(tx):
    r = tx.Recv(tx.entries(2))
    r.to_asking()
    cid = tx.one(checks(tx, recheck(tx, r)), object).check_id
    no_effect(tx, r.call("decide", tx.PEER, r.T, True))
    acts = space(r, 1, check_id=cid)
    assert [m.reason for m in tx.sent(acts)] == ["no_space"]
    assert not tx.of(acts, tx.tr.CreateTemp)
    assert tx.one(tx.ended(acts), object).reason == "no_space"
    assert tx.sent(logged(r), Accept) == []
    assert create_count(tx, r) == 0
    assert r.e.has_accepted_receives() is False
    assert r.e.info(r.key) is None


def test_decline_during_recheck_rejects_immediately(tx):
    r = tx.Recv(tx.entries(2))
    r.to_asking()
    cid = tx.one(checks(tx, recheck(tx, r)), object).check_id
    acts = r.call("decide", tx.PEER, r.T, False)
    assert tx.sent(acts) == [Reject(transfer_id=r.T, reason="user")]
    assert tx.one(tx.ended(acts), object).reason == "user"
    assert r.e.info(r.key) is None
    assert space(r, tx.FREE, check_id=cid) == []


def test_accept_then_decline_during_recheck_rejects(tx):
    r = tx.Recv(tx.entries(2))
    r.to_asking()
    cid = tx.one(checks(tx, recheck(tx, r)), object).check_id
    r.call("decide", tx.PEER, r.T, True)
    acts = r.call("decide", tx.PEER, r.T, False)
    assert tx.sent(acts) == [Reject(transfer_id=r.T, reason="user")]
    assert space(r, tx.FREE, check_id=cid) == []
    assert create_count(tx, r) == 0


def test_second_recheck_while_rechecking_uses_only_latest_result(tx):
    r = tx.Recv(tx.entries(2))
    r.to_asking()
    c1 = tx.one(checks(tx, recheck(tx, r)), object).check_id
    r.call("decide", tx.PEER, r.T, True)
    c2 = tx.one(checks(tx, recheck(tx, r)), object).check_id
    assert c2 > c1
    assert space(r, tx.FREE, check_id=c1) == []  # đủ nhưng cũ: chưa chấp nhận
    assert create_count(tx, r) == 0
    acts = space(r, tx.FREE, check_id=c2)
    assert tx.one(acts, tx.tr.CreateTemp).transfer_id == r.T
    assert ask_count(tx, r) == 1


# ---- Hàng offer -------------------------------------------------------------------------------


def test_queued_checked_later_on_new_dir(tx):
    r1 = tx.Recv(tx.entries(1))
    r1.to_asking()
    r2 = tx.Recv(tx.entries(2), T=tx.T2, like=r1)
    r2.send_offer()
    assert r1.e.info(r2.key).status == tx.tr.TransferStatus.QUEUED

    acts = recheck(tx, r1)
    assert [c.transfer_id for c in checks(tx, acts)] == [r1.T], "lượt QUEUED không được kiểm lúc này"
    assert r1.e.info(r2.key).status == tx.tr.TransferStatus.QUEUED
    space(r1, tx.FREE, check_id=last_check_id(tx, r1))

    acts = r1.call("decide", tx.PEER, r1.T, False)  # đầu hàng xong → lượt kế được kiểm trên thư mục mới
    cs = tx.one(checks(tx, acts), object)
    assert (cs.transfer_id, cs.total_size) == (tx.T2, tx.size_of(2))
    earlier = [c.check_id for c in checks(tx, r1.all_actions()) if c is not cs]
    assert cs.check_id > max(earlier)
    acts = space(r2, tx.FREE, check_id=cs.check_id)
    assert tx.one(acts, tx.tr.AskUser).transfer_id == tx.T2


def test_recheck_with_checking_head_and_queued_only_checks_head(tx):
    r1 = tx.Recv(tx.entries(1))
    r1.up()
    r1.send_offer()
    r2 = tx.Recv(tx.entries(2), T=tx.T2, like=r1)
    r2.send_offer()
    acts = recheck(tx, r1)
    assert [c.transfer_id for c in checks(tx, acts)] == [r1.T]


def test_recheck_two_devices_asking_each_rechecked_independently(tx):
    ra = tx.Recv(tx.entries(1))
    rc = tx.Recv(tx.entries(1), T=tx.T2, peer=tx.OTHER, like=ra, cid=2)
    ra.to_asking()
    rc.to_asking()
    acts = recheck(tx, ra)
    by_t = {c.transfer_id: c.check_id for c in checks(tx, acts)}
    assert set(by_t) == {ra.T, rc.T}
    assert by_t[ra.T] != by_t[rc.T]
    acts = space(rc, 0, check_id=by_t[rc.T])
    assert [m.reason for m in tx.sent(acts)] == ["no_space"]
    # Kết quả của máy kia không đổi gì ở lượt `ra`.
    assert ra.e.info(ra.key) is not None
    no_effect(tx, space(ra, tx.FREE, check_id=by_t[ra.T]))
    assert ask_count(tx, ra) == 1


@pytest.mark.parametrize("pressed", [True, False], ids=["da-bam-nhan", "chua-bam"])
@pytest.mark.parametrize(("ratio", "admitted"), [((3, 2), 1), ((2, 1), 2), ((9, 10), 0)])
def test_recheck_two_devices_share_new_space(tx, pressed, ratio, admitted):
    """Hai máy, mỗi máy một offer cỡ S cùng đang hỏi; thư mục mới có `ratio`·S chỗ trống. Kết quả
    kiểm lại của hai lượt về lần lượt: số lượt còn sống đúng bằng `admitted`, lượt kia `no_space`, và
    phần đã hứa không vượt chỗ trống mới (không lượt nào được tính chỗ của lượt kia hai lần)."""
    S = tx.size_of(4)
    free = S * ratio[0] // ratio[1]
    ra = tx.Recv(tx.entries(4))
    rc = tx.Recv(tx.entries(4), T=tx.T2, peer=tx.OTHER, like=ra, cid=2)
    ra.to_asking()
    rc.to_asking()
    acts = recheck(tx, ra)
    ids = {c.transfer_id: c.check_id for c in checks(tx, acts)}
    assert set(ids) == {ra.T, rc.T}
    if pressed:
        no_effect(tx, ra.call("decide", tx.PEER, ra.T, True))
        no_effect(tx, rc.call("decide", tx.OTHER, rc.T, True))

    outcome: dict[str, str] = {}
    for r in (ra, rc):
        a = space(r, free, check_id=ids[r.T])
        tx.check_order(a)
        rejects = tx.sent(a, Reject)
        if rejects:
            assert [m.reason for m in rejects] == ["no_space"], rejects
            assert not tx.of(a, tx.tr.CreateTemp)
            outcome[r.T] = "rejected"
        else:
            no_wire = [m for m in tx.sent(a) if not isinstance(m, Accept)]
            assert no_wire == [], a
            if pressed:
                assert tx.one(a, tx.tr.CreateTemp).transfer_id == r.T
            else:
                assert not tx.of(a, tx.tr.CreateTemp)
            outcome[r.T] = "admitted"

    alive = [r for r in (ra, rc) if ra.e.info(r.key) is not None]
    assert len(alive) == admitted, outcome
    assert sum(1 for v in outcome.values() if v == "admitted") == admitted
    assert ra.e.promised_bytes() == admitted * S
    assert ra.e.promised_bytes() <= free, "tổng đã hứa vượt chỗ trống của thư mục mới"
    assert ra.e.has_accepted_receives() is (pressed and admitted > 0)
    assert ask_count(tx, ra) == 1 and ask_count(tx, rc) == 1


# ---- has_accepted_receives ----------------------------------------------------------------------


def test_has_accepted_receives_phases(tx):
    r = tx.Recv(tx.entries(2))
    e = r.e
    assert e.has_accepted_receives() is False  # engine rỗng
    r.up()
    r.send_offer()
    assert e.has_accepted_receives() is False  # CHECKING
    space(r, tx.FREE)
    assert e.has_accepted_receives() is False  # ASKING
    r2 = tx.Recv(tx.entries(1), T=tx.T2, like=r)
    r2.send_offer()
    assert e.has_accepted_receives() is False  # ASKING + QUEUED
    r.call("decide", tx.PEER, r.T, True)
    assert e.has_accepted_receives() is True  # đã chấp nhận, chưa `temp_created`
    r.created()
    assert e.has_accepted_receives() is True  # đang chạy
    r.finish_all()
    # r xong; r2 lên đầu hàng (CHECKING): không còn lượt nào đã chấp nhận.
    assert e.info(r.key) is None
    assert e.has_accepted_receives() is False


def test_has_accepted_receives_true_while_reconnecting(tx):
    r = tx.Recv(tx.entries(2))
    r.accept()
    r.lost()
    assert r.e.info(r.key) is not None
    assert r.e.has_accepted_receives() is True
    assert checks(tx, recheck(tx, r)) == []


def test_has_accepted_receives_false_after_cancel_and_for_send(tx):
    r = tx.Recv(tx.entries(2))
    r.accept()
    r.call("cancel", r.key)
    assert r.e.has_accepted_receives() is False
    s = tx.Send(tx.entries(2), T=tx.T2, like=r)
    s.start()
    assert r.e.has_accepted_receives() is False, "lượt gửi không phải lượt nhận"


def test_has_accepted_receives_true_for_restored_receive(tx):
    tr = tx.tr
    e = tr.TransferEngine(tx.ME, receiving=True)
    r = tx.Recv(tx.entries(3), engine=e, clock=tx.Clock(1000.0))
    on = (0,)
    leaves = bytearray(DIGEST_SIZE * r.n(0))
    leaves[0:DIGEST_SIZE] = r.dg(0, 0)
    item = tr.RestoredReceive(
        device_id=r.peer,
        offer=r.offer,
        file_index=0,
        bits=tx.bits(r.n(0), on),
        leaves=leaves,
        part_blocks=1,
        age=5.0,
    )
    e.restore(receives=(item,), tombstones=(), done=(), now=1000.0)
    assert e.has_accepted_receives() is True  # khôi phục, chưa nối lại
    assert checks(tx, recheck(tx, r)) == []


# ---- Mô phỏng hypothesis -----------------------------------------------------------------------

OPS = st.lists(
    st.one_of(
        st.just(("recheck",)),
        st.tuples(
            st.just("result"),
            st.sampled_from(["latest", "stale", "none"]),
            st.sampled_from(["ok", "no_space", "too_large"]),
        ),
        st.tuples(st.just("decide"), st.booleans()),
    ),
    min_size=1,
    max_size=25,
)


@HYPO
@given(ops=OPS, start_asking=st.booleans())
def test_recheck_interleavings_keep_invariants(tx, ops, start_asking):
    """Xen kẽ `recheck_space` / `space_checked` (id mới nhất, id cũ, không id) / `decide` trên một lượt.

    Mô hình theo spec 4.2: CHECKING → kết quả đủ thì hỏi (một lần duy nhất); ASKING + kiểm lại → kết quả
    mới nhất quyết định; Nhận lúc đang kiểm lại chỉ ghi nhận rồi áp khi kết quả đủ; Từ chối thì từ chối
    ngay. Bất biến: không bao giờ hai `AskUser`; không chấp nhận khi kết quả mới nhất thiếu chỗ/vượt trần;
    chấp nhận đúng một lần; kết quả có id cũ không đổi gì.
    """
    r = tx.Recv(tx.entries(2, 1))
    total = tx.size_of(2) + tx.size_of(1)
    r.up()
    ids = [tx.one(checks(tx, r.send_offer()), object).check_id]
    phase = "checking"  # checking | asking | accepted | ended
    rechecking = False
    pending = False
    if start_asking:
        assert tx.one(space(r, tx.FREE, check_id=ids[-1]), tx.tr.AskUser)
        phase = "asking"

    for op in ops:
        before = tx.snapshot(r.e)
        if op[0] == "recheck":
            acts = recheck(tx, r)
            mine = checks(tx, acts, r.T)
            if phase in ("checking", "asking"):
                cs = tx.one(mine, object)
                assert cs.check_id > max(ids)
                ids.append(cs.check_id)
                if phase == "asking":
                    rechecking = True
            else:
                assert mine == []
            no_effect(tx, acts)
            continue

        if op[0] == "decide":
            accept = op[1]
            acts = r.call("decide", tx.PEER, r.T, accept)
            if phase != "asking":
                assert not tx.of(acts, (tx.tr.CreateTemp, tx.tr.AskUser)) and tx.sent(acts) == []
                continue
            if not accept:
                assert tx.sent(acts) == [Reject(transfer_id=r.T, reason="user")]
                phase = "ended"
            elif rechecking:
                no_effect(tx, acts)
                pending = True
            else:
                assert tx.one(acts, tx.tr.CreateTemp).transfer_id == r.T
                phase = "accepted"
            continue

        _, which, verdict = op
        if which == "stale":
            if len(ids) < 2:
                continue
            cid: int | None = ids[-2]
        elif which == "latest":
            cid = ids[-1]
        else:
            cid = None
        free, mx = {"ok": (tx.FREE, None), "no_space": (total - 1, None), "too_large": (tx.FREE, 1)}[verdict]
        acts = space(r, free, mx, check_id=cid)
        live = phase == "checking" or (phase == "asking" and rechecking)
        if which == "stale" or not live:
            assert acts == [], f"kết quả bị bỏ phải trả []: {op}, pha {phase}"
            assert tx.snapshot(r.e) == before
            continue
        if verdict != "ok":
            reason = "no_space" if verdict == "no_space" else "file_too_large"
            assert [m.reason for m in tx.sent(acts)] == [reason]
            assert not tx.of(acts, (tx.tr.CreateTemp, tx.tr.AskUser))
            phase = "ended"
        elif phase == "checking":
            assert tx.one(acts, tx.tr.AskUser).transfer_id == r.T
            phase = "asking"
        else:
            rechecking = False
            if pending:
                assert tx.one(acts, tx.tr.CreateTemp).transfer_id == r.T
                assert not tx.of(acts, tx.tr.AskUser)
                phase = "accepted"
            else:
                no_effect(tx, acts)

    # Bất biến trên cả nhật ký.
    assert ask_count(tx, r) <= 1, "không bao giờ hai AskUser cho một lượt"
    assert create_count(tx, r) == (1 if phase == "accepted" else 0)
    assert r.e.has_accepted_receives() is (phase == "accepted")
    assert (r.e.info(r.key) is None) is (phase == "ended")
