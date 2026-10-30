"""Test `teleport.core.history` (P4-2, việc 5: T1–T9; spec `specs/spec-p4-2.md` mục 2, 3.1, 4, 7, 11).

Viết từ đặc tả, không từ code. Định dạng `history.json` (spec mục 4) được dựng lại độc lập ở đây bằng
`json` (`entry_dict`, `file_bytes`), không qua `encode_history`, để `decode_history` được kiểm theo tài
liệu chứ không theo bộ mã hóa của chính nó.

Khóa của một mục là (`direction`, `device_id`, `transfer_id`) (spec mục 2). Thứ tự là vị trí trong
mảng: mới nhất trước.
"""

from __future__ import annotations

import datetime
import json
import math

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.core import history as H
from teleport.core.messages import FileEntry
from teleport.core.transfer import Direction, TransferInfo, TransferKey, TransferStatus

UTC = datetime.UTC
DEV_A = "a" * 32
DEV_B = "b" * 32
TIDS = [f"{k:032x}" for k in range(1, 400)]
STAMP = "2026-10-28T08:15:00Z"
STAMP_DT = datetime.datetime(2026, 10, 28, 8, 15, 0, tzinfo=UTC)

SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# --- Hàm phụ trợ ---------------------------------------------------------------------------------


def make_entry(**overrides) -> H.HistoryEntry:
    """Một `HistoryEntry` hợp lệ (chiều nhận); `overrides` thay từng trường."""
    fields = {
        "direction": Direction.RECEIVE,
        "device_id": DEV_A,
        "device_name": "PC-Nha",
        "transfer_id": TIDS[0],
        "file_count": 2,
        "total_bytes": 300,
        "done_bytes": 300,
        "names": ("a.mp4", "b.txt"),
        "result": "done",
        "finished_at": STAMP_DT,
        "saved_path": "C:\\Users\\x\\Teleport\\a.mp4",
    }
    fields.update(overrides)
    return H.HistoryEntry(**fields)


def entry_dict(**overrides) -> dict:
    """Một mục JSON hợp lệ theo spec mục 4 (dựng tay, không qua `encode_history`)."""
    d = {
        "direction": "receive",
        "device_id": DEV_A,
        "device_name": "PC-Nha",
        "transfer_id": TIDS[0],
        "file_count": 12,
        "total_bytes": 104857600,
        "done_bytes": 104857600,
        "names": ["a.mp4", "b.mp4"],
        "result": "done",
        "finished_at": STAMP,
        "saved_path": "C:\\Users\\x\\Teleport\\a.mp4",
    }
    d.update(overrides)
    return d


def file_bytes(entries: list, *, version: object = 1, bom: bool = False) -> bytes:
    data = json.dumps({"version": version, "entries": entries}, ensure_ascii=False).encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


def key_of(e: H.HistoryEntry) -> tuple[str, str, str]:
    return (str(e.direction), e.device_id, e.transfer_id)


def info(n_files: int, *, direction: Direction = Direction.RECEIVE, done: int | None = None) -> TransferInfo:
    files = tuple(FileEntry(f"tep-{k:02d}.bin", 10 + k, 0) for k in range(n_files))
    total = sum(f.size for f in files)
    return TransferInfo(
        key=TransferKey(DEV_B, direction, TIDS[7]),
        status=TransferStatus.TRANSFERRING,
        files=files,
        names=tuple(f.name for f in files),
        file_index=0,
        done_bytes=total if done is None else done,
        total_bytes=total,
    )


# --- Hằng (spec mục 3.1) -------------------------------------------------------------------------


def test_constants_match_spec():
    assert H.MAX_HISTORY == 100
    assert H.MAX_HISTORY_NAMES == 10
    assert H.MAX_HISTORY_FILE == 4 * 1024 * 1024
    assert H.HISTORY_VERSION == 1
    assert issubclass(H.HistoryError, ValueError)


# --- T1: cắt còn 100 -----------------------------------------------------------------------------


def test_add_entry_keeps_newest_100_and_drops_the_oldest():
    """T1. Đột biến “bỏ cắt 100” làm test này đỏ."""
    entries: tuple[H.HistoryEntry, ...] = ()
    made = [make_entry(transfer_id=TIDS[k]) for k in range(101)]
    for e in made:
        entries = H.add_entry(entries, e)
    assert isinstance(entries, tuple)
    assert len(entries) == 100
    assert entries[0] == made[100], "mục mới nhất phải ở đầu"
    assert made[0] not in entries, "mục cũ nhất phải bị bỏ"
    assert list(entries) == list(reversed(made[1:]))


def test_add_entry_does_not_mutate_input():
    first = [make_entry(transfer_id=TIDS[1])]
    H.add_entry(first, make_entry(transfer_id=TIDS[2]))
    assert first == [make_entry(transfer_id=TIDS[1])]


# --- T2: khử trùng theo khóa ---------------------------------------------------------------------


def test_add_entry_same_key_replaces_and_moves_to_front():
    """T2. Đột biến “bỏ khử trùng ở add_entry” làm test này đỏ."""
    a = make_entry(transfer_id=TIDS[1], result="cancelled_user")
    b = make_entry(transfer_id=TIDS[2])
    a2 = make_entry(transfer_id=TIDS[1], result="done", done_bytes=300)
    entries = H.add_entry(H.add_entry((), a), b)
    assert entries == (b, a)
    entries = H.add_entry(entries, a2)
    assert entries == (a2, b), "cùng khóa: chỉ còn một mục, mục mới ở đầu"


def test_add_entry_same_transfer_id_other_direction_is_a_different_entry():
    recv = make_entry(direction=Direction.RECEIVE, transfer_id=TIDS[3])
    send = make_entry(direction=Direction.SEND, transfer_id=TIDS[3], saved_path=None)
    entries = H.add_entry(H.add_entry((), recv), send)
    assert entries == (send, recv)


def test_add_entry_same_transfer_id_other_device_is_a_different_entry():
    one = make_entry(device_id=DEV_A, transfer_id=TIDS[4])
    two = make_entry(device_id=DEV_B, transfer_id=TIDS[4])
    assert H.add_entry(H.add_entry((), one), two) == (two, one)


# --- T3: entry_from_info -------------------------------------------------------------------------


def test_entry_from_info_keeps_first_10_names_and_full_file_count():
    """T3: 25 file → `names` 10 tên đầu theo thứ tự offer, `file_count == 25`."""
    inf = info(25)
    e = H.entry_from_info(
        inf, result="done", device_name="PC-Nha", wall_now=1_800_000_000.0, saved_path="/x/a"
    )
    assert e.names == tuple(f"tep-{k:02d}.bin" for k in range(10))
    assert e.file_count == 25
    assert e.total_bytes == inf.total_bytes
    assert e.done_bytes == inf.done_bytes
    assert e.direction == Direction.RECEIVE
    assert e.device_id == DEV_B
    assert e.transfer_id == TIDS[7]
    assert e.result == "done"
    assert e.device_name == "PC-Nha"
    assert e.saved_path == "/x/a"


@pytest.mark.parametrize("wall_now", [1_800_000_000.999, 1_800_000_000.0, 1_761_639_300.5])
def test_entry_from_info_floors_finished_at_to_the_second_in_utc(wall_now):
    e = H.entry_from_info(info(1), result="done", device_name=None, wall_now=wall_now, saved_path=None)
    expected = datetime.datetime.fromtimestamp(math.floor(wall_now), UTC)
    assert e.finished_at == expected
    assert e.finished_at.utcoffset() == datetime.timedelta(0)
    assert e.finished_at.microsecond == 0


def test_entry_from_info_send_with_saved_path_is_rejected():
    inf = info(2, direction=Direction.SEND)
    with pytest.raises(H.HistoryError):
        H.entry_from_info(inf, result="done", device_name="A", wall_now=1_800_000_000.0, saved_path="/x/a")
    # Cùng đầu vào, `saved_path=None` thì hợp lệ: lỗi trên đúng là do `saved_path` ở chiều gửi.
    e = H.entry_from_info(inf, result="done", device_name="A", wall_now=1_800_000_000.0, saved_path=None)
    assert e.direction == Direction.SEND and e.saved_path is None


def test_entry_from_info_keeps_unknown_result_and_partial_bytes():
    inf = info(3, done=5)
    e = H.entry_from_info(inf, result="ket_qua_moi", device_name=None, wall_now=0.0, saved_path=None)
    assert e.result == "ket_qua_moi"
    assert e.done_bytes == 5
    assert e.device_name is None


# --- HistoryEntry.__post_init__ ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("device_id", "A" * 32),
        ("device_id", "a" * 31),
        ("transfer_id", "xyz"),
        ("device_name", ""),
        ("device_name", " PC"),
        ("file_count", 0),
        ("file_count", -1),
        ("total_bytes", -1),
        ("done_bytes", -1),
        ("done_bytes", 301),
        ("names", tuple(f"n{k}" for k in range(11))),
        ("names", ("ok", "x\x00y")),
        ("result", ""),
        ("result", "x" * 65),
        ("result", "kết_quả"),
        ("saved_path", "C:\\a\x07b"),
        ("saved_path", "C:\\a\ud800b"),
        ("saved_path", "x" * 32768),
    ],
    ids=lambda v: ascii(v)[:24],  # id an toàn cho biến môi trường (không surrogate, không quá dài)
)
def test_history_entry_rejects_invalid_field(field, value):
    with pytest.raises(H.HistoryError):
        make_entry(**{field: value})


def test_history_entry_boundaries_are_valid():
    make_entry(names=tuple(f"n{k}" for k in range(10)))
    make_entry(result="x" * 64)
    make_entry(saved_path="x" * 32767)
    make_entry(total_bytes=0, done_bytes=0)
    make_entry(device_name=None, saved_path=None)


# --- T4: mã hóa rồi giải mã giữ nguyên -----------------------------------------------------------


def test_encode_decode_roundtrip_keeps_unicode_none_and_order():
    entries = [
        make_entry(
            transfer_id=TIDS[10],
            names=("phim-tiếng-việt.mp4", "日本語.txt", "🎉.png"),
            file_count=3,
            device_name="Máy-Nhà 💻",
            saved_path="C:\\Users\\Đức\\Teleport\\phim-tiếng-việt.mp4",
        ),
        make_entry(transfer_id=TIDS[11], direction=Direction.SEND, saved_path=None, device_name=None),
        make_entry(transfer_id=TIDS[12], result="ket_qua_la", done_bytes=7),
    ]
    data = H.encode_history(entries)
    assert isinstance(data, bytes)
    assert H.decode_history(data) == entries
    # ensure_ascii=False: tên Unicode nằm nguyên UTF-8 trong file, không phải \uXXXX.
    assert "phim-tiếng-việt.mp4".encode() in data


def test_encoded_file_has_the_documented_shape():
    """Spec mục 4: object `version`/`entries`, giờ dạng `YYYY-MM-DDTHH:MM:SSZ`, chiều là chuỗi."""
    e = make_entry(transfer_id=TIDS[13])
    obj = json.loads(H.encode_history([e]).decode("utf-8"))
    assert obj["version"] == 1
    [d] = obj["entries"]
    assert d == {
        "direction": "receive",
        "device_id": DEV_A,
        "device_name": "PC-Nha",
        "transfer_id": TIDS[13],
        "file_count": 2,
        "total_bytes": 300,
        "done_bytes": 300,
        "names": ["a.mp4", "b.txt"],
        "result": "done",
        "finished_at": STAMP,
        "saved_path": "C:\\Users\\x\\Teleport\\a.mp4",
    }


def test_encode_empty_history():
    assert json.loads(H.encode_history([])) == {"version": 1, "entries": []}
    assert H.decode_history(H.encode_history([])) == []


def test_decode_handwritten_file():
    data = file_bytes([entry_dict(), entry_dict(transfer_id=TIDS[1], direction="send", saved_path=None)])
    [r, s] = H.decode_history(data)
    assert r.direction == Direction.RECEIVE and s.direction == Direction.SEND
    assert r.finished_at == STAMP_DT
    assert r.names == ("a.mp4", "b.mp4")
    assert r.file_count == 12 and r.total_bytes == r.done_bytes == 104857600
    assert s.saved_path is None


# --- T5: cả file hỏng ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"khong phai json", id="khong-phai-json"),
        pytest.param(b"", id="rong"),
        pytest.param(b'{"version": 1, "entries": [', id="cat-cut"),
        pytest.param(b'{"version": 1, "entries": [], "x": "\xff\xfe"}', id="khong-phai-utf8"),
        pytest.param(json.dumps([]).encode(), id="mang-goc"),
        pytest.param(b"null", id="null"),
        pytest.param(b'"chuoi"', id="chuoi"),
        pytest.param(file_bytes([], version=2), id="version-2"),
        pytest.param(file_bytes([], version="1"), id="version-chuoi"),
        pytest.param(file_bytes([], version=True), id="version-true"),
        pytest.param(file_bytes([], version=1.0), id="version-float"),
        pytest.param(json.dumps({"entries": []}).encode(), id="thieu-version"),
        pytest.param(json.dumps({"version": 1, "entries": {}}).encode(), id="entries-object"),
        pytest.param(json.dumps({"version": 1, "entries": None}).encode(), id="entries-null"),
        pytest.param(json.dumps({"version": 1, "entries": "[]"}).encode(), id="entries-chuoi"),
        pytest.param(json.dumps({"version": 1}).encode(), id="thieu-entries"),
    ],
)
def test_decode_whole_file_corrupt_raises_history_error(data):
    with pytest.raises(H.HistoryError):
        H.decode_history(data)


def test_decode_accepts_bom():
    assert H.decode_history(file_bytes([entry_dict()], bom=True)) == H.decode_history(
        file_bytes([entry_dict()])
    )
    assert len(H.decode_history(file_bytes([entry_dict()], bom=True))) == 1


def test_decode_accepts_bytearray_and_memoryview():
    data = file_bytes([entry_dict()])
    assert H.decode_history(bytearray(data)) == H.decode_history(memoryview(data)) == H.decode_history(data)


def test_decode_rejects_file_one_byte_over_the_cap_before_parsing():
    data = file_bytes([entry_dict()])
    over = data + b" " * (H.MAX_HISTORY_FILE + 1 - len(data))
    assert len(over) == H.MAX_HISTORY_FILE + 1
    with pytest.raises(H.HistoryError):
        H.decode_history(over)
    # Không giải mã: byte rác vượt trần cũng chỉ là HistoryError.
    with pytest.raises(H.HistoryError):
        H.decode_history(b"\xff" * (H.MAX_HISTORY_FILE + 1))


def test_decode_accepts_file_exactly_at_the_cap():
    data = file_bytes([entry_dict()])
    exact = data + b" " * (H.MAX_HISTORY_FILE - len(data))
    assert len(exact) == H.MAX_HISTORY_FILE
    assert len(H.decode_history(exact)) == 1


# --- T6: mục hỏng bị bỏ riêng --------------------------------------------------------------------


def _without(field: str) -> dict:
    d = entry_dict(transfer_id=TIDS[50])
    del d[field]
    return d


BAD_ENTRIES = [
    pytest.param(_without("result"), id="thieu-result"),
    pytest.param(_without("device_id"), id="thieu-device_id"),
    pytest.param(_without("finished_at"), id="thieu-finished_at"),
    pytest.param(_without("names"), id="thieu-names"),
    pytest.param(_without("saved_path"), id="thieu-saved_path"),
    pytest.param(_without("device_name"), id="thieu-device_name"),
    pytest.param(entry_dict(transfer_id=TIDS[50], file_count="12"), id="file_count-chuoi"),
    pytest.param(entry_dict(transfer_id=TIDS[50], file_count=1.5), id="file_count-float"),
    pytest.param(entry_dict(transfer_id=TIDS[50], file_count=0), id="file_count-0"),
    pytest.param(entry_dict(transfer_id=TIDS[50], names="a.mp4"), id="names-chuoi"),
    pytest.param(entry_dict(transfer_id=TIDS[50], names=["a", 1]), id="names-phan-tu-so"),
    pytest.param(entry_dict(transfer_id=TIDS[50], names=["a\x00b"]), id="names-ky-tu-cc"),
    pytest.param(entry_dict(transfer_id=TIDS[50], names=[f"n{k}" for k in range(11)]), id="names-11"),
    pytest.param(entry_dict(transfer_id=TIDS[50], device_id="A" * 32), id="device_id-hoa"),
    pytest.param(entry_dict(transfer_id=TIDS[50], device_id="a" * 33), id="device_id-dai"),
    pytest.param(entry_dict(transfer_id=TIDS[50], device_id=None), id="device_id-null"),
    pytest.param(entry_dict(transfer_id="g" * 32), id="transfer_id-sai"),
    pytest.param(entry_dict(transfer_id=TIDS[50], direction="SEND"), id="direction-hoa"),
    pytest.param(entry_dict(transfer_id=TIDS[50], direction="both"), id="direction-la"),
    pytest.param(entry_dict(transfer_id=TIDS[50], finished_at="2026-10-28 08:15:00"), id="gio-sai-dang"),
    pytest.param(
        entry_dict(transfer_id=TIDS[50], finished_at="2026-02-30T00:00:00Z"), id="gio-khong-ton-tai"
    ),
    pytest.param(entry_dict(transfer_id=TIDS[50], finished_at=1761639300), id="gio-so"),
    pytest.param(entry_dict(transfer_id=TIDS[50], total_bytes=-1, done_bytes=0), id="total-am"),
    pytest.param(entry_dict(transfer_id=TIDS[50], done_bytes=-1), id="done-am"),
    pytest.param(entry_dict(transfer_id=TIDS[50], done_bytes=104857601), id="done-lon-hon-total"),
    pytest.param(entry_dict(transfer_id=TIDS[50], device_name=""), id="device_name-rong"),
    pytest.param(entry_dict(transfer_id=TIDS[50], device_name=5), id="device_name-so"),
    pytest.param(entry_dict(transfer_id=TIDS[50], result=""), id="result-rong"),
    pytest.param(entry_dict(transfer_id=TIDS[50], result=None), id="result-null"),
    pytest.param(entry_dict(transfer_id=TIDS[50], result="x" * 65), id="result-dai"),
    pytest.param(entry_dict(transfer_id=TIDS[50], saved_path=7), id="saved_path-so"),
    pytest.param(entry_dict(transfer_id=TIDS[50], saved_path="C:\\a\x01b"), id="saved_path-cc"),
    pytest.param(entry_dict(transfer_id=TIDS[50], saved_path="C:\\a\ud800b"), id="saved_path-surrogate"),
    pytest.param(entry_dict(transfer_id=TIDS[50], saved_path="x" * 32768), id="saved_path-dai"),
    pytest.param("khong-phai-object", id="muc-la-chuoi"),
    pytest.param(None, id="muc-null"),
    pytest.param([1, 2], id="muc-la-mang"),
]


@pytest.mark.parametrize("bad", BAD_ENTRIES)
def test_decode_drops_only_the_corrupt_entry(bad):
    """T6: mục hỏng đứng giữa hai mục tốt → chỉ nó bị bỏ, thứ tự giữ nguyên."""
    good1 = entry_dict(transfer_id=TIDS[1])
    good2 = entry_dict(transfer_id=TIDS[2])
    # json.dumps không ghi được surrogate lẻ thành UTF-8: dùng \\ud800 (ensure_ascii=True) cho ca đó.
    obj = {"version": 1, "entries": [good1, bad, good2]}
    data = json.dumps(obj, ensure_ascii=True).encode("ascii")
    got = H.decode_history(data)
    assert [e.transfer_id for e in got] == [TIDS[1], TIDS[2]]


def test_decode_ignores_unknown_fields():
    d = entry_dict(truong_moi="gi-do", khac={"a": [1, 2]})
    [e] = H.decode_history(file_bytes([d]))
    assert e.transfer_id == TIDS[0]
    assert not hasattr(e, "truong_moi")


def test_decode_ignores_unknown_top_level_fields():
    obj = {"version": 1, "entries": [entry_dict()], "ghi_chu": "x"}
    assert len(H.decode_history(json.dumps(obj).encode())) == 1


def test_decode_keeps_unknown_result():
    [e] = H.decode_history(file_bytes([entry_dict(result="ket_qua_tuong_lai")]))
    assert e.result == "ket_qua_tuong_lai"


def test_decode_accepts_null_device_name_and_saved_path():
    [e] = H.decode_history(file_bytes([entry_dict(device_name=None, saved_path=None)]))
    assert e.device_name is None and e.saved_path is None


# --- T7: 150 mục có trùng khóa -------------------------------------------------------------------


def test_decode_150_entries_with_duplicates_keeps_first_and_cuts_to_100():
    dicts = [entry_dict(transfer_id=TIDS[k], result="done") for k in range(150)]
    # Mục 5 và 9 trùng khóa với mục 0 và 1 (khác `result`): giữ mục đứng trước.
    dicts[5] = entry_dict(transfer_id=TIDS[0], result="cancelled_user")
    dicts[9] = entry_dict(transfer_id=TIDS[1], result="expired")
    got = H.decode_history(file_bytes(dicts))
    assert len(got) == 100
    expected_ids = [TIDS[k] for k in range(150) if k not in (5, 9)][:100]
    assert [e.transfer_id for e in got] == expected_ids
    assert got[0].result == "done" and got[1].result == "done"
    assert len({key_of(e) for e in got}) == 100


def test_decode_same_transfer_id_other_direction_is_not_a_duplicate():
    dicts = [entry_dict(), entry_dict(direction="send", saved_path=None)]
    assert len(H.decode_history(file_bytes(dicts))) == 2


# --- T8: mã hóa vượt trần ------------------------------------------------------------------------


def test_encode_over_cap_drops_oldest_until_it_fits():
    long_path = "C:\\" + "ữ" * 32000  # ~96 KB UTF-8 mỗi mục; 100 mục ≈ 9,6 MB > 4 MiB
    long_names = tuple(("ạ" * 80 + f"{k}")[:84] for k in range(10))
    entries = [
        make_entry(transfer_id=TIDS[k], saved_path=long_path, names=long_names, file_count=10)
        for k in range(100)
    ]
    data = H.encode_history(entries)
    assert len(data) <= H.MAX_HISTORY_FILE
    got = H.decode_history(data)
    assert 0 < len(got) < 100
    assert got == entries[: len(got)], "phải bỏ mục cuối (cũ nhất), giữ mục mới"


def test_encode_under_cap_keeps_everything():
    entries = [make_entry(transfer_id=TIDS[k]) for k in range(100)]
    assert H.decode_history(H.encode_history(entries)) == entries


# --- T9: dữ liệu tùy ý chỉ ném HistoryError ------------------------------------------------------


@SETTINGS
@given(st.binary(max_size=4096))
def test_decode_arbitrary_bytes_only_raises_history_error(data):
    try:
        out = H.decode_history(data)
    except H.HistoryError:
        return
    assert isinstance(out, list)


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=40),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=12), children, max_size=4)
    ),
    max_leaves=12,
)


@SETTINGS
@given(
    field=st.sampled_from(sorted(entry_dict())),
    value=json_values,
)
def test_decode_entry_with_random_field_value_never_raises(field, value):
    """Thay một trường của mục tốt bằng giá trị JSON tùy ý: hoặc mục đó bị bỏ, hoặc còn; không ném."""
    bad = entry_dict(transfer_id=TIDS[60])
    bad[field] = value
    data = json.dumps({"version": 1, "entries": [entry_dict(), bad]}).encode()
    got = H.decode_history(data)
    assert got[0].transfer_id == TIDS[0]
    assert len(got) in (1, 2)


@pytest.mark.slow
@SETTINGS
@given(st.lists(json_values, max_size=6))
def test_decode_entries_of_random_json_values_never_raises(values):
    data = json.dumps({"version": 1, "entries": values}).encode()
    got = H.decode_history(data)
    assert isinstance(got, list)
    assert all(isinstance(e, H.HistoryEntry) for e in got)
