"""Test message truyền file trong `teleport.core.messages` (kế hoạch GĐ2 P2-1, mục 6a, 6e, 6f, 9, 10).

Nguồn: docs/protocol.md §3.2 (bảng message), §3.3 (luật trường), §4.1 (`data_open`), bảng loại
kết nối §2.1. Các quyết định đã duyệt:
- `reject.reason` thêm `file_too_large` (kèm `file_index`, `max_size`) và `not_receiving`; lý do lạ
  đúng dạng vẫn nhận (6a, A3).
- Tên file trong `offer`: tầng message chỉ từ chối tên > 255 byte UTF-8, có ký tự `Cc` hoặc `Cs`
  (surrogate lẻ); mọi thứ khác (`../../x`, `CON`, `a.txt:ads`…) **phải được nhận**, vì làm sạch là
  việc của `core/filenames` và bộ làm sạch biến đổi chứ không từ chối (6e, mục 9).
- `mtime` là mili giây Unix, số nguyên 0…2⁵³−1; `block_size` ≠ 1 048 576 → message sai (6f).
- `size` ≤ 2⁴², 1–1000 file; `offer` 1000 file tên dài nhất và `resume` file 4 TiB vừa 1 MiB.
- `data_open` có chứng chỉ client → kết nối DATA; không có → `WrongConnectionKind`.

Message được dựng chủ yếu từ dict trên dây (`parse_message`) để không phụ thuộc kiểu chứa `files`.
"""

import base64
import json
import unicodedata
import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from teleport.core.framing import MAX_MESSAGE_SIZE, decode_body, encode_message
from teleport.core.messages import (
    Accept,
    Cancel,
    Complete,
    ConnectionKind,
    DataOpen,
    FileEntry,
    MessageError,
    Offer,
    Reject,
    Resume,
    UnknownMessageType,
    Verified,
    WrongConnectionKind,
    allowed_types,
    check_message_kind,
    opening_kind,
    parse_message,
    to_dict,
)

TID = uuid.UUID(bytes=bytes(range(16)), version=4).hex
TID_B = uuid.UUID(bytes=bytes(range(16, 32)), version=4).hex
HEX64 = "0123456789abcdef" * 4
BLOCK = 1_048_576
MAX_SIZE = 2**42
MAX_INT = 2**53 - 1
FAT32_MAX = 4_294_967_295
FOUR_TIB_BITFIELD = base64.b64encode(b"\xff" * (2**22 // 8)).decode()

assert len(TID) == 32 and TID.lower() == TID

TRANSFER_TYPES = frozenset({"offer", "accept", "reject", "resume", "complete", "verified", "cancel"})
NEW_TYPES = TRANSFER_TYPES | {"data_open"}


def file_entry(name="a.txt", size=10, mtime=1_700_000_000_000) -> dict:
    return {"name": name, "size": size, "mtime": mtime}


def valid_dicts() -> dict[str, dict]:
    """Một message hợp lệ (dict trên dây) cho mỗi biến thể."""
    return {
        "data_open": {"type": "data_open", "transfer_id": TID, "file_index": 0, "resume_seq": 1},
        "offer": {"type": "offer", "transfer_id": TID, "block_size": BLOCK, "files": [file_entry()]},
        "offer_many": {
            "type": "offer",
            "transfer_id": TID_B,
            "block_size": BLOCK,
            "files": [
                file_entry("a.txt", 0, 0),
                file_entry("b.bin", MAX_SIZE, MAX_INT),
                file_entry("ảnh.png", 1, 5),
            ],
        },
        "accept": {"type": "accept", "transfer_id": TID},
        "reject_user": {"type": "reject", "transfer_id": TID, "reason": "user"},
        "reject_busy": {"type": "reject", "transfer_id": TID, "reason": "busy"},
        "reject_no_space": {
            "type": "reject",
            "transfer_id": TID,
            "reason": "no_space",
            "needed": 10,
            "available": 3,
        },
        "reject_file_too_large": {
            "type": "reject",
            "transfer_id": TID,
            "reason": "file_too_large",
            "file_index": 2,
            "max_size": FAT32_MAX,
        },
        "reject_not_receiving": {"type": "reject", "transfer_id": TID, "reason": "not_receiving"},
        "resume": {"type": "resume", "transfer_id": TID, "file_index": 0, "seq": 1, "bitfield": "gA=="},
        "resume_empty_file": {
            "type": "resume",
            "transfer_id": TID,
            "file_index": 3,
            "seq": 9,
            "bitfield": "",
        },
        "complete": {"type": "complete", "transfer_id": TID, "file_index": 0, "merkle_root": HEX64},
        "verified": {"type": "verified", "transfer_id": TID, "file_index": 0},
        "cancel": {"type": "cancel", "transfer_id": TID, "reason": "user"},
    }


EXPECTED_CLASS = {
    "data_open": DataOpen,
    "offer": Offer,
    "accept": Accept,
    "reject": Reject,
    "resume": Resume,
    "complete": Complete,
    "verified": Verified,
    "cancel": Cancel,
}


def wire(name: str, **overrides) -> dict:
    return {**valid_dicts()[name], **overrides}


def offer_with(files: list) -> dict:
    return {"type": "offer", "transfer_id": TID, "block_size": BLOCK, "files": files}


def assert_rejected(obj) -> None:
    with pytest.raises(MessageError) as exc:
        parse_message(obj)
    # Type đã biết: lỗi là message hỏng, không phải type lạ.
    assert not isinstance(exc.value, UnknownMessageType)


def as_wire(msg) -> dict:
    """Dạng thật sự đi trên dây của `to_dict(msg)` (tuple hay list đều thành mảng JSON)."""
    return json.loads(json.dumps(to_dict(msg), ensure_ascii=False))


def ref_name_ok(name: object) -> bool:
    """Luật tên file ở tầng message (kế hoạch 6e + mục 9), viết độc lập làm oracle."""
    if not isinstance(name, str):
        return False
    if any(unicodedata.category(c) in ("Cc", "Cs") for c in name):
        return False
    return len(name.encode("utf-8")) <= 255


def ref_int_ok(value: object, lo: int, hi: int) -> bool:
    return type(value) is int and lo <= value <= hi


# --- Round-trip trên dây -----------------------------------------------------------------------


@pytest.mark.parametrize("name", list(valid_dicts()))
def test_wire_dict_parses_to_expected_class_and_serializes_back_exactly(name):
    w = valid_dicts()[name]
    msg = parse_message(w)
    assert type(msg) is EXPECTED_CLASS[w["type"]]
    assert w["type"] == msg.TYPE
    # to_dict không thêm trường (kể cả `null` cho trường tùy chọn) và giữ đúng thứ tự file.
    assert as_wire(msg) == w
    assert parse_message(to_dict(msg)) == msg


@pytest.mark.parametrize("name", list(valid_dicts()))
def test_round_trip_through_framing(name):
    msg = parse_message(valid_dicts()[name])
    assert parse_message(decode_body(encode_message(to_dict(msg))[4:])) == msg


@pytest.mark.parametrize("type_name", sorted(NEW_TYPES))
def test_new_types_are_known(type_name):
    # Trước P2-1 các type này là UnknownMessageType; nay thiếu trường phải là message hỏng.
    assert_rejected({"type": type_name})


@pytest.mark.parametrize("name", list(valid_dicts()))
def test_unknown_fields_are_ignored(name):
    w = valid_dicts()[name]
    assert parse_message({**w, "extra": 1, "z": {"a": [1]}}) == parse_message(w)
    assert "extra" not in to_dict(parse_message({**w, "extra": 1}))


def test_unknown_fields_inside_file_entry_are_ignored():
    # protocol §3.3: phần tử `files` có đủ name/size/mtime, trường lạ bị bỏ qua.
    noisy = offer_with([{**file_entry(), "mode": 0o644, "x": None}])
    assert parse_message(noisy) == parse_message(offer_with([file_entry()]))


def test_offer_files_keep_order_and_values():
    msg = parse_message(valid_dicts()["offer_many"])
    assert len(msg.files) == 3
    assert [(f.name, f.size, f.mtime) for f in msg.files] == [
        ("a.txt", 0, 0),
        ("b.bin", MAX_SIZE, MAX_INT),
        ("ảnh.png", 1, 5),
    ]
    assert msg.transfer_id == TID_B
    assert msg.block_size == BLOCK


def test_offer_files_are_file_entries_and_offer_can_be_built_by_hand():
    msg = parse_message(valid_dicts()["offer_many"])
    assert all(type(f) is FileEntry for f in msg.files)
    by_hand = Offer(
        transfer_id=TID_B,
        block_size=BLOCK,
        files=(
            FileEntry(name="a.txt", size=0, mtime=0),
            FileEntry(name="b.bin", size=MAX_SIZE, mtime=MAX_INT),
            FileEntry(name="ảnh.png", size=1, mtime=5),
        ),
    )
    assert by_hand == msg
    assert as_wire(by_hand) == valid_dicts()["offer_many"]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: FileEntry(name="x" * 256, size=0, mtime=0),
        lambda: FileEntry(name="a\x00", size=0, mtime=0),
        lambda: FileEntry(name="\ud800", size=0, mtime=0),
        lambda: FileEntry(name="a", size=-1, mtime=0),
        lambda: FileEntry(name="a", size=MAX_SIZE + 1, mtime=0),
        lambda: FileEntry(name="a", size=0, mtime=-1),
        lambda: FileEntry(name="a", size=0, mtime=True),
        lambda: Offer(transfer_id=TID, block_size=BLOCK, files=()),
        lambda: Offer(transfer_id=TID, block_size=BLOCK + 1, files=(FileEntry(name="a", size=0, mtime=0),)),
    ],
)
def test_constructing_file_entry_or_offer_with_bad_value_raises(factory):
    with pytest.raises(MessageError):
        factory()


def test_flat_messages_can_be_built_by_hand():
    assert parse_message(valid_dicts()["data_open"]) == DataOpen(transfer_id=TID, file_index=0, resume_seq=1)
    assert parse_message(valid_dicts()["accept"]) == Accept(transfer_id=TID)
    assert parse_message(valid_dicts()["verified"]) == Verified(transfer_id=TID, file_index=0)
    assert parse_message(valid_dicts()["cancel"]) == Cancel(transfer_id=TID, reason="user")
    assert parse_message(valid_dicts()["complete"]) == Complete(
        transfer_id=TID, file_index=0, merkle_root=HEX64
    )
    assert parse_message(valid_dicts()["resume"]) == Resume(
        transfer_id=TID, file_index=0, seq=1, bitfield="gA=="
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: DataOpen(transfer_id=TID.upper(), file_index=0, resume_seq=1),
        lambda: DataOpen(transfer_id=TID, file_index=-1, resume_seq=1),
        lambda: DataOpen(transfer_id=TID, file_index=0, resume_seq=0),
        lambda: Accept(transfer_id="x"),
        lambda: Verified(transfer_id=TID, file_index=True),
        lambda: Cancel(transfer_id=TID, reason="USER"),
        lambda: Complete(transfer_id=TID, file_index=0, merkle_root=HEX64.upper()),
        lambda: Resume(transfer_id=TID, file_index=0, seq=2**53, bitfield=""),
    ],
)
def test_constructing_by_hand_with_bad_value_raises(factory):
    # Như mọi message (messages.py): kiểm ở __post_init__, nên tạo tay sai cũng bị chặn.
    with pytest.raises(MessageError):
        factory()


# --- Thiếu trường ------------------------------------------------------------------------------


def _missing_field_cases():
    for name, w in valid_dicts().items():
        for field in w:
            if field != "type":
                yield pytest.param(name, field, id=f"{name}-{field}")


@pytest.mark.parametrize(("name", "field"), list(_missing_field_cases()))
def test_missing_field_rejected(name, field):
    w = valid_dicts()[name]
    del w[field]
    assert_rejected(w)


@pytest.mark.parametrize("field", ["name", "size", "mtime"])
def test_file_entry_missing_field_rejected(field):
    entry = file_entry()
    del entry[field]
    assert_rejected(offer_with([entry]))


# --- transfer_id -------------------------------------------------------------------------------

BAD_TRANSFER_IDS = [
    TID.upper(),
    TID[:31],
    TID + "0",
    TID[:31] + "\n",
    TID + "\n",
    " " + TID[1:],
    str(uuid.UUID(TID)),  # có gạch nối
    "０" * 32,
    "g" * 32,
    "",
    None,
    1,
    [TID],
    TID.encode(),
]


@pytest.mark.parametrize("value", BAD_TRANSFER_IDS, ids=repr)
@pytest.mark.parametrize(
    "name", ["data_open", "offer", "accept", "reject_user", "resume", "complete", "verified", "cancel"]
)
def test_bad_transfer_id_rejected(name, value):
    assert_rejected(wire(name, transfer_id=value))


# --- Số nguyên: file_index, seq, resume_seq, size, mtime, block_size ----------------------------

NOT_INTS = [True, False, 1.0, 0.0, 1.5, "1", None, [1], {"v": 1}]

FILE_INDEX_FIELDS = [
    ("data_open", "file_index"),
    ("resume", "file_index"),
    ("complete", "file_index"),
    ("verified", "file_index"),
    ("reject_file_too_large", "file_index"),
]


@pytest.mark.parametrize("value", [0, 1, 999])
@pytest.mark.parametrize(("name", "field"), FILE_INDEX_FIELDS)
def test_valid_file_index_accepted(name, field, value):
    msg = parse_message(wire(name, **{field: value}))
    assert getattr(msg, field) == value
    assert type(getattr(msg, field)) is int


@pytest.mark.parametrize("value", [-1, -(2**53), 1000, 2**32, *NOT_INTS], ids=repr)
@pytest.mark.parametrize(("name", "field"), FILE_INDEX_FIELDS)
def test_bad_file_index_rejected(name, field, value):
    # protocol §3.3: `file_index` là số nguyên 0 … 999 (offer có tối đa 1000 file).
    assert_rejected(wire(name, **{field: value}))


SEQ_FIELDS = [("resume", "seq"), ("data_open", "resume_seq")]


@pytest.mark.parametrize("value", [1, 2, MAX_INT])
@pytest.mark.parametrize(("name", "field"), SEQ_FIELDS)
def test_valid_seq_accepted(name, field, value):
    assert getattr(parse_message(wire(name, **{field: value})), field) == value


@pytest.mark.parametrize("value", [0, -1, 2**53, 2**53 + 1, 10**100, *NOT_INTS], ids=repr)
@pytest.mark.parametrize(("name", "field"), SEQ_FIELDS)
def test_bad_seq_rejected(name, field, value):
    # protocol §3.3: `seq`, `resume_seq` là số nguyên JSON 1 … 2⁵³ − 1.
    assert_rejected(wire(name, **{field: value}))


@pytest.mark.parametrize("literal", ["1e3", "1.0", "true", "null", '"1"', "9007199254740992", "-0.0"])
def test_seq_from_raw_json_non_integer_rejected(literal):
    body = f'{{"type":"resume","transfer_id":"{TID}","file_index":0,"seq":{literal},"bitfield":""}}'
    assert_rejected(decode_body(body.encode()))


@pytest.mark.parametrize("value", [0, 1, BLOCK, MAX_SIZE])
def test_valid_size_accepted(value):
    assert parse_message(offer_with([file_entry(size=value)])).files[0].size == value


@pytest.mark.parametrize("value", [-1, MAX_SIZE + 1, 2**53, 10**30, *NOT_INTS], ids=repr)
def test_bad_size_rejected(value):
    assert_rejected(offer_with([file_entry(size=value)]))


@pytest.mark.parametrize("value", [0, 1, 1_700_000_000_000, MAX_INT])
def test_valid_mtime_accepted(value):
    assert parse_message(offer_with([file_entry(mtime=value)])).files[0].mtime == value


@pytest.mark.parametrize("value", [-1, -1_700_000_000_000, 2**53, 2**64, *NOT_INTS], ids=repr)
def test_bad_mtime_rejected(value):
    # 6f: `mtime` mili giây Unix nguyên 0…2⁵³−1 (bên gửi ép âm về 0; bên nhận vẫn kiểm).
    assert_rejected(offer_with([file_entry(mtime=value)]))


def test_block_size_exactly_one_mib_accepted():
    assert parse_message(valid_dicts()["offer"]).block_size == 1_048_576


@pytest.mark.parametrize(
    "value",
    [0, 1, 1_048_575, 1_048_577, 2 * BLOCK, BLOCK // 2, -BLOCK, 1_048_576.0, True, "1048576", None],
    ids=repr,
)
def test_block_size_other_than_one_mib_rejected(value):
    assert_rejected(wire("offer", block_size=value))


# --- files: số lượng, kiểu ---------------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 999, 1000])
def test_file_count_within_limit_accepted(count):
    msg = parse_message(offer_with([file_entry(f"f{i}") for i in range(count)]))
    assert len(msg.files) == count


@pytest.mark.parametrize("count", [0, 1001, 5000])
def test_file_count_outside_1_to_1000_rejected(count):
    assert_rejected(offer_with([file_entry(f"f{i}") for i in range(count)]))


@pytest.mark.parametrize("files", [None, "a.txt", 1, {"name": "a"}, {"0": file_entry()}, True], ids=repr)
def test_files_not_a_list_rejected(files):
    assert_rejected(wire("offer", files=files))


@pytest.mark.parametrize("entry", [None, "a.txt", 1, [], ["a.txt", 1, 2], True], ids=repr)
def test_file_entry_not_an_object_rejected(entry):
    assert_rejected(offer_with([file_entry(), entry]))


def test_duplicate_names_in_offer_accepted():
    # protocol §4.4: trùng tên trong cùng `offer` được bên nhận đánh số ` (1)`, không từ chối.
    msg = parse_message(offer_with([file_entry("a.txt"), file_entry("a.txt"), file_entry("A.TXT")]))
    assert [f.name for f in msg.files] == ["a.txt", "a.txt", "A.TXT"]


# --- name --------------------------------------------------------------------------------------

ACCEPTED_NAMES = [
    "a.txt",
    "../../x",
    "..",
    "/etc/passwd",
    "C:\\Windows\\x",
    "\\\\?\\C:\\x",
    "CON",
    "NUL.txt",
    "con .txt",
    "COM¹",
    "a.txt:ads",
    "x.",
    "x.part.state",
    ".teleport",
    " khoảng trắng đầu",
    "cuối ",
    "a\u202eb.txt",  # RTL override: loại Cf, bộ làm sạch xử lý
    "a\u2028b",  # loại Zl, không phải Cc
    "a\u200bb",
    "e\u0301.txt",  # NFD giữ nguyên ở tầng message
    "x" * 255,
    "ệ" * 85,  # 3 byte × 85 = 255 byte
    "é" * 127 + "a",  # 2 × 127 + 1 = 255 byte
    "🚀" * 63 + "abc",  # 4 × 63 + 3 = 255 byte
    '"' * 255,
    "\\" * 255,
]


@pytest.mark.parametrize("name", ACCEPTED_NAMES, ids=repr)
def test_dangerous_but_well_formed_names_are_accepted_unchanged(name):
    msg = parse_message(offer_with([file_entry(name)]))
    assert msg.files[0].name == name
    assert len(msg.files[0].name) == len(name)  # không chuẩn hóa, không làm sạch
    assert parse_message(to_dict(msg)) == msg


def test_empty_name_is_accepted_at_message_level():
    """Kế hoạch 6e, protocol §3.3: tầng message chỉ từ chối tên > 255 byte hoặc có Cc/Cs; tên
    rỗng do bộ làm sạch đổi thành `file` (P2-2), không phải lý do bỏ cả `offer`."""
    assert parse_message(offer_with([file_entry("")])).files[0].name == ""


REJECTED_NAMES = [
    "x" * 256,
    "🚀" * 64,  # 64 code point nhưng 256 byte: đếm theo byte UTF-8, không theo ký tự
    "ệ" * 86,
    "é" * 128,
    "a\x00b",
    "a\nb",
    "a\rb",
    "\t",
    "a\x1bb",
    "a\x1fb",
    "a\x7fb",
    "a\x85b",  # Cc C1
    "a\x9fb",
    "\ud800",  # surrogate lẻ: không mã hóa được UTF-8
    "a\udfffb",
    "\ud83d",
    None,
    1,
    ["a.txt"],
    {"name": "a"},
    b"a.txt",
]


@pytest.mark.parametrize("name", REJECTED_NAMES, ids=repr)
def test_bad_name_rejected(name):
    assert_rejected(offer_with([file_entry(name)]))


@pytest.mark.parametrize("escape", ["\\ud800", "\\udfff", "a\\ud83db", "\\u0000", "\\u001b", "\\u007f"])
def test_bad_name_from_raw_json_raises_only_message_error(escape):
    # Surrogate lẻ tới từ JSON escape: kiểm độ dài UTF-8 không được ném UnicodeEncodeError.
    body = (
        f'{{"type":"offer","transfer_id":"{TID}","block_size":{BLOCK},'
        f'"files":[{{"name":"{escape}","size":1,"mtime":0}}]}}'
    )
    assert_rejected(decode_body(body.encode()))


def test_one_bad_file_rejects_whole_offer():
    files = [file_entry(f"f{i}") for i in range(10)]
    files[7] = file_entry("x" * 256)
    assert_rejected(offer_with(files))


# --- reject ------------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["user", "busy", "not_receiving", "new_reason", "a", "a" * 32, "_"])
def test_reject_reason_without_extra_fields_accepted(reason):
    msg = parse_message(wire("reject_user", reason=reason))
    assert msg.reason == reason
    assert type(msg.reason) is str


def test_reject_not_receiving_is_valid():
    msg = parse_message(valid_dicts()["reject_not_receiving"])
    assert isinstance(msg, Reject)
    assert msg.reason == "not_receiving"
    assert as_wire(msg) == valid_dicts()["reject_not_receiving"]


@pytest.mark.parametrize(
    "reason",
    ["", "USER", "User", "a" * 33, "user\n", " user", "no-space", "user1", "ü", None, 1, ["user"]],
    ids=repr,
)
def test_reject_bad_reason_rejected(reason):
    assert_rejected(wire("reject_user", reason=reason))


def test_reject_no_space_values():
    msg = parse_message(valid_dicts()["reject_no_space"])
    assert (msg.needed, msg.available) == (10, 3)


@pytest.mark.parametrize("field", ["needed", "available"])
@pytest.mark.parametrize("value", [-1, 2**53, *NOT_INTS], ids=repr)
def test_reject_no_space_bad_amount_rejected(field, value):
    assert_rejected(wire("reject_no_space", **{field: value}))


@pytest.mark.parametrize(("needed", "available"), [(0, 0), (MAX_INT, 0), (1, MAX_INT)])
def test_reject_no_space_amount_range_accepted(needed, available):
    msg = parse_message(wire("reject_no_space", needed=needed, available=available))
    assert (msg.needed, msg.available) == (needed, available)


def test_reject_file_too_large_values():
    msg = parse_message(valid_dicts()["reject_file_too_large"])
    assert msg.reason == "file_too_large"
    assert msg.file_index == 2
    assert msg.max_size == FAT32_MAX


@pytest.mark.parametrize("field", ["file_index", "max_size"])
def test_reject_file_too_large_requires_index_and_max_size(field):
    w = valid_dicts()["reject_file_too_large"]
    del w[field]
    assert_rejected(w)


@pytest.mark.parametrize("value", [-1, 2**53, *NOT_INTS], ids=repr)
def test_reject_file_too_large_bad_max_size_rejected(value):
    assert_rejected(wire("reject_file_too_large", max_size=value))


@pytest.mark.parametrize("value", [0, 1, FAT32_MAX, MAX_SIZE, MAX_INT])
def test_reject_file_too_large_max_size_range_accepted(value):
    assert parse_message(wire("reject_file_too_large", max_size=value)).max_size == value


@pytest.mark.parametrize(
    ("field", "value"),
    [("needed", 5), ("available", 0), ("max_size", FAT32_MAX), ("file_index", 0)],
)
@pytest.mark.parametrize("reason", ["user", "busy", "not_receiving", "new_reason"])
def test_reject_attached_field_for_other_reason_accepted_when_valid(reason, field, value):
    # protocol §3.3: trường kèm theo được vắng mặt với lý do khác, có mặt thì phải đúng luật.
    msg = parse_message(wire("reject_user", reason=reason, **{field: value}))
    assert msg.reason == reason
    assert parse_message(to_dict(msg)) == msg


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("needed", -1),
        ("needed", "5"),
        ("available", 1.0),
        ("max_size", True),
        ("file_index", 1000),
        ("file_index", -1),
    ],
)
@pytest.mark.parametrize("reason", ["user", "busy", "not_receiving", "new_reason"])
def test_reject_attached_field_for_other_reason_still_validated(reason, field, value):
    assert_rejected(wire("reject_user", reason=reason, **{field: value}))


@pytest.mark.parametrize("field", ["needed", "available", "max_size", "file_index"])
@pytest.mark.parametrize("base", ["reject_user", "reject_no_space", "reject_file_too_large"])
def test_reject_optional_field_null_rejected(base, field):
    # protocol §3.3: trường tùy chọn mang `null` là sai, như mọi trường sai kiểu.
    assert_rejected(wire(base, **{field: None}))


# --- cancel ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason", ["user", "source_changed", "unknown_transfer", "error", "expired", "new_reason", "a" * 32]
)
def test_cancel_reason_accepted(reason):
    msg = parse_message(wire("cancel", reason=reason))
    assert msg.reason == reason
    assert as_wire(msg) == wire("cancel", reason=reason)


def test_cancel_reason_enum_round_trip():
    # protocol §3.3: lý do chuẩn của `cancel`, kể cả `expired` (lượt thất bại vì mất kết nối quá lâu).
    from teleport.core.messages import Cancel, CancelReason

    expected = {"user", "source_changed", "unknown_transfer", "error", "expired"}
    assert {r.value for r in CancelReason} == expected
    for reason in CancelReason:
        msg = Cancel(transfer_id="0123456789abcdef" * 2, reason=reason)
        assert type(msg.reason) is str
        assert parse_message(as_wire(msg)) == msg


@pytest.mark.parametrize("reason", ["", "ERROR", "a" * 33, "error\n", "source-changed", None, 0], ids=repr)
def test_cancel_bad_reason_rejected(reason):
    assert_rejected(wire("cancel", reason=reason))


# --- complete.merkle_root ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [HEX64.upper(), HEX64[:63], HEX64 + "0", HEX64[:63] + "\n", "０" * 64, "", None, 0, bytes(32), [HEX64]],
    ids=repr,
)
def test_bad_merkle_root_rejected(value):
    assert_rejected(wire("complete", merkle_root=value))


# --- resume.bitfield ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["", "gA==", "AAAA", "/w==", "+/8=", FOUR_TIB_BITFIELD],
    ids=["empty", "1", "3", "slash", "plus", "4TiB"],
)
def test_well_formed_bitfield_accepted(value):
    assert parse_message(wire("resume", bitfield=value)).bitfield == value


@pytest.mark.parametrize("value", [None, 0, [], {"b": 1}, b"gA==", True], ids=repr)
def test_non_string_bitfield_rejected(value):
    assert_rejected(wire("resume", bitfield=value))


@pytest.mark.parametrize(
    "value",
    [
        "!!!!",
        "gA=",
        "gA",  # thiếu `=` đệm
        "g",
        "gA ==",
        "gA==\n",
        " gA==",
        "-_8=",  # bảng chữ base64url
        "gA==gA==",
        "gB==",  # bit đệm khác 0: không phải dạng bộ mã hóa chuẩn sinh ra
        "AB==",
        "AAB=",
    ],
    ids=repr,
)
def test_non_canonical_base64_bitfield_rejected(value):
    """protocol §3.3: `bitfield` là base64 chuẩn RFC 4648 §4 có `=` đệm, đúng dạng bộ mã hóa chuẩn
    sinh ra (không khoảng trắng, không xuống dòng, bit đệm bằng 0). Độ dài so với số khối thì
    tầng message không biết (kiểm ở `Bitfield.from_base64`)."""
    assert_rejected(wire("resume", bitfield=value))


def test_bitfield_larger_than_any_valid_file_rejected():
    # size ≤ 2⁴² ⇒ bitfield ≤ 512 KiB; dài hơn thì không file nào khớp được (protocol §3.3).
    too_long = base64.b64encode(b"\xff" * (2**22 // 8 + 1)).decode()
    assert_rejected(wire("resume", bitfield=too_long))


# --- Kích thước: vừa trần 1 MiB ----------------------------------------------------------------

LONGEST_NAMES = {
    "ascii": "x" * 255,
    "quote": '"' * 255,  # mỗi ký tự thành `\"` trong JSON
    "backslash": "\\" * 255,
    "2-byte": "é" * 127 + "a",
    "3-byte": "ệ" * 85,
    "4-byte": "🚀" * 63 + "abc",
}


@pytest.mark.parametrize("kind", list(LONGEST_NAMES))
def test_offer_1000_files_longest_names_fits_one_mib(kind):
    name = LONGEST_NAMES[kind]
    assert len(name.encode("utf-8")) == 255
    w = offer_with([file_entry(name, MAX_SIZE, MAX_INT) for _ in range(1000)])
    msg = parse_message(w)
    frame = encode_message(to_dict(msg))  # ném FramingError nếu > 1 MiB
    assert len(frame) - 4 <= MAX_MESSAGE_SIZE
    assert parse_message(decode_body(frame[4:])) == msg


def test_offer_1000_files_worst_case_fits_even_with_ascii_only_json():
    # Phòng xa: dù bộ mã hóa có escape mọi ký tự ngoài ASCII (\uXXXX), vẫn vừa trần.
    w = offer_with([file_entry("é" * 127 + "a", MAX_SIZE, MAX_INT) for _ in range(1000)])
    assert len(json.dumps(w, ensure_ascii=True, separators=(",", ":")).encode()) <= MAX_MESSAGE_SIZE


def test_resume_of_four_tib_file_fits_one_mib():
    assert len(base64.b64decode(FOUR_TIB_BITFIELD)) == 512 * 1024
    msg = parse_message(wire("resume", file_index=999, seq=MAX_INT, bitfield=FOUR_TIB_BITFIELD))
    frame = encode_message(to_dict(msg))
    assert len(frame) - 4 <= MAX_MESSAGE_SIZE
    assert parse_message(decode_body(frame[4:])) == msg


# --- Bảng loại kết nối -------------------------------------------------------------------------

SAMPLE_TRANSFER_MESSAGES = [
    pytest.param(name, id=name) for name, w in valid_dicts().items() if w["type"] in TRANSFER_TYPES
]


def test_control_allows_exactly_ping_pong_clipboard_and_transfer_messages():
    # GĐ5 P5-0 thêm `clipboard` vào kết nối điều khiển (spec-p5-0 mục 2.2).
    assert allowed_types(ConnectionKind.CONTROL) == frozenset({"ping", "pong", "clipboard"}) | TRANSFER_TYPES


def test_pairing_allows_no_transfer_message():
    assert not (allowed_types(ConnectionKind.PAIRING) & NEW_TYPES)


def test_data_allows_no_json_message_after_opening():
    # Sau `data_open` chỉ có khung khối nhị phân (protocol §2.1, §4.2).
    assert allowed_types(ConnectionKind.DATA) == frozenset()


@pytest.mark.parametrize("name", SAMPLE_TRANSFER_MESSAGES)
def test_transfer_message_valid_on_control_only(name):
    msg = parse_message(valid_dicts()[name])
    assert check_message_kind(ConnectionKind.CONTROL, msg) is None
    for kind in (ConnectionKind.PAIRING, ConnectionKind.DATA):
        with pytest.raises(WrongConnectionKind):
            check_message_kind(kind, msg)


@pytest.mark.parametrize("name", SAMPLE_TRANSFER_MESSAGES)
@pytest.mark.parametrize("has_cert", [False, True])
def test_transfer_message_cannot_open_a_connection(name, has_cert):
    with pytest.raises(WrongConnectionKind):
        opening_kind(parse_message(valid_dicts()[name]), has_client_cert=has_cert)


def test_data_open_with_client_cert_opens_data():
    msg = parse_message(valid_dicts()["data_open"])
    assert opening_kind(msg, has_client_cert=True) is ConnectionKind.DATA


def test_data_open_without_client_cert_is_wrong_kind():
    msg = parse_message(valid_dicts()["data_open"])
    with pytest.raises(WrongConnectionKind):
        opening_kind(msg, has_client_cert=False)


@pytest.mark.parametrize("kind", list(ConnectionKind))
def test_data_open_is_never_valid_after_opening(kind):
    with pytest.raises(WrongConnectionKind):
        check_message_kind(kind, parse_message(valid_dicts()["data_open"]))


# --- Hypothesis: round-trip --------------------------------------------------------------------

transfer_ids = st.uuids(version=4).map(lambda u: u.hex)
file_indexes = st.integers(min_value=0, max_value=999)
seqs = st.integers(min_value=1, max_value=MAX_INT)
reasons = st.from_regex(r"[a-z_]{1,32}", fullmatch=True) | st.sampled_from(
    ["user", "busy", "not_receiving", "source_changed", "unknown_transfer", "error"]
)
valid_names = st.text(alphabet=st.characters(blacklist_categories=("Cc", "Cs")), max_size=80).filter(
    lambda s: len(s.encode("utf-8")) <= 255
) | st.sampled_from(ACCEPTED_NAMES)
file_entries = st.fixed_dictionaries(
    {
        "name": valid_names,
        "size": st.integers(min_value=0, max_value=MAX_SIZE),
        "mtime": st.integers(min_value=0, max_value=MAX_INT),
    }
)
bitfields = st.binary(max_size=200).map(lambda b: base64.b64encode(b).decode())

valid_wires = st.one_of(
    st.fixed_dictionaries(
        {
            "type": st.just("data_open"),
            "transfer_id": transfer_ids,
            "file_index": file_indexes,
            "resume_seq": seqs,
        }
    ),
    st.fixed_dictionaries(
        {
            "type": st.just("offer"),
            "transfer_id": transfer_ids,
            "block_size": st.just(BLOCK),
            "files": st.lists(file_entries, min_size=1, max_size=20),
        }
    ),
    st.fixed_dictionaries({"type": st.just("accept"), "transfer_id": transfer_ids}),
    st.fixed_dictionaries(
        {
            "type": st.just("reject"),
            "transfer_id": transfer_ids,
            "reason": reasons.filter(lambda r: r not in ("no_space", "file_too_large")),
        }
    ),
    st.fixed_dictionaries(
        {
            "type": st.just("reject"),
            "transfer_id": transfer_ids,
            "reason": st.just("no_space"),
            "needed": st.integers(0, MAX_INT),
            "available": st.integers(0, MAX_INT),
        }
    ),
    st.fixed_dictionaries(
        {
            "type": st.just("reject"),
            "transfer_id": transfer_ids,
            "reason": st.just("file_too_large"),
            "file_index": file_indexes,
            "max_size": st.integers(0, MAX_INT),
        }
    ),
    st.fixed_dictionaries(
        {
            "type": st.just("resume"),
            "transfer_id": transfer_ids,
            "file_index": file_indexes,
            "seq": seqs,
            "bitfield": bitfields,
        }
    ),
    st.fixed_dictionaries(
        {
            "type": st.just("complete"),
            "transfer_id": transfer_ids,
            "file_index": file_indexes,
            "merkle_root": st.binary(min_size=32, max_size=32).map(bytes.hex),
        }
    ),
    st.fixed_dictionaries(
        {"type": st.just("verified"), "transfer_id": transfer_ids, "file_index": file_indexes}
    ),
    st.fixed_dictionaries({"type": st.just("cancel"), "transfer_id": transfer_ids, "reason": reasons}),
)


@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(valid_wires)
def test_property_valid_wire_round_trips(w):
    msg = parse_message(w)
    assert type(msg) is EXPECTED_CLASS[w["type"]]
    assert as_wire(msg) == w
    assert parse_message(to_dict(msg)) == msg
    frame = encode_message(to_dict(msg))
    assert parse_message(decode_body(frame[4:])) == msg


# --- Hypothesis: fuzz chỉ ném MessageError -----------------------------------------------------

TRICKY = [
    "\x00",
    "\x1b",
    "\x7f",
    "\x85",
    "\u202e",
    "\u2028",
    "\ud800",
    "\udfff",
    "０",
    "é",
    "🚀",
    "/",
    "\\",
    ":",
    ".",
    " ",
    "\n",
    "=",
]
tricky_text = st.lists(
    st.sampled_from(TRICKY) | st.characters(codec="utf-8") | st.sampled_from("0123456789abcdef_"),
    max_size=300,
).map("".join)
json_scalars = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=True, allow_infinity=True)
    | st.text(max_size=40)
)
json_values = st.recursive(
    json_scalars,
    lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(max_size=8), c, max_size=4),
    max_leaves=20,
)
field_values = (
    st.just(TID)
    | st.just(HEX64)
    | st.sampled_from(["user", "no_space", "file_too_large", "not_receiving", "gA==", "gB==", "", "!!!!"])
    | st.integers(-3, 3)
    | st.sampled_from(
        [BLOCK, BLOCK + 1, MAX_SIZE, MAX_SIZE + 1, MAX_INT, MAX_INT + 1, FAT32_MAX, 999, 1000, 1.0, -0.0, 1e3]
    )
    | tricky_text
    | json_values
)
entry_values = st.fixed_dictionaries(
    {},
    optional={
        "name": tricky_text | valid_names | json_scalars,
        "size": st.integers(-2, 3) | st.sampled_from([MAX_SIZE, MAX_SIZE + 1, 1.0, True]) | json_scalars,
        "mtime": st.integers(-2, 3) | st.sampled_from([MAX_INT, MAX_INT + 1, -1, 0.5, False]) | json_scalars,
        "x": json_values,
    },
)
near_valid = st.builds(
    lambda name, overrides: {**valid_dicts()[name], **overrides},
    st.sampled_from(list(valid_dicts())),
    st.dictionaries(
        st.sampled_from(
            [
                "transfer_id",
                "file_index",
                "resume_seq",
                "block_size",
                "files",
                "reason",
                "needed",
                "available",
                "max_size",
                "seq",
                "bitfield",
                "merkle_root",
                "type",
                "x",
            ]
        ),
        field_values | st.lists(entry_values, max_size=4) | st.sampled_from(sorted(NEW_TYPES)),
        max_size=3,
    ),
)
offers_with_fuzzed_files = st.lists(entry_values | json_values, min_size=0, max_size=6).map(offer_with)


def _check_only_message_error(obj) -> None:
    try:
        msg = parse_message(obj)
    except MessageError:
        return
    # Đã qua kiểm tra thì phải tuần tự hóa, mã hóa khung và round-trip được.
    d = to_dict(msg)
    assert parse_message(d) == msg
    encode_message(d)


@settings(max_examples=600, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(near_valid | offers_with_fuzzed_files | json_values | st.builds(lambda t: {"type": t}, json_values))
def test_fuzz_parse_message_only_raises_message_error(obj):
    _check_only_message_error(obj)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(near_valid.map(lambda d: json.dumps(d, ensure_ascii=True)))
def test_fuzz_raw_json_with_escaped_surrogates_only_raises_message_error(body):
    # ensure_ascii=True: surrogate lẻ đi qua dưới dạng `\ud800`, đúng như máy kia gửi được.
    _check_only_message_error(decode_body(body.encode("ascii")))


@settings(max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(entry_values, min_size=1, max_size=4))
def test_property_offer_accepted_iff_every_file_follows_rules(entries):
    expected_ok = all(
        {"name", "size", "mtime"} <= e.keys()
        and ref_name_ok(e["name"])
        and ref_int_ok(e["size"], 0, MAX_SIZE)
        and ref_int_ok(e["mtime"], 0, MAX_INT)
        for e in entries
    )
    try:
        msg = parse_message(offer_with(entries))
    except MessageError:
        assert not expected_ok, f"offer hợp lệ bị từ chối: {entries!r}"
        return
    assert expected_ok, f"offer sai luật lại được nhận: {entries!r}"
    assert [f.name for f in msg.files] == [e["name"] for e in entries]


@settings(max_examples=300, deadline=None)
@given(st.binary(max_size=40))
def test_property_canonical_base64_bitfield_accepted(raw):
    s = base64.b64encode(raw).decode()
    assert parse_message(wire("resume", bitfield=s)).bitfield == s
