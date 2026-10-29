import base64
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from teleport.core.framing import (
    HEADER,
    MAX_JSON_DEPTH,
    MAX_MESSAGE_SIZE,
    MAX_PAIRING_MESSAGE_SIZE,
    FramingError,
    decode_body,
    decode_header,
    encode_message,
)
from teleport.core.messages import MAX_SAFE_INTEGER


def split(frame: bytes) -> tuple[int, bytes]:
    return decode_header(frame[: HEADER.size]), frame[HEADER.size :]


def test_roundtrip_keeps_unicode():
    msg = {"type": "msg", "text": "Xin chào, Việt Nam 🚀"}
    length, body = split(encode_message(msg))
    assert length == len(body)
    assert decode_body(body) == msg


def test_encoding_is_compact_utf8():
    _, body = split(encode_message({"a": "đ"}))
    assert body == '{"a":"đ"}'.encode()


def test_header_at_limit_is_accepted():
    assert decode_header(HEADER.pack(MAX_MESSAGE_SIZE)) == MAX_MESSAGE_SIZE


def test_oversized_header_is_rejected():
    with pytest.raises(FramingError, match="quá lớn"):
        decode_header(HEADER.pack(MAX_MESSAGE_SIZE + 1))


def test_custom_limit_for_pairing_connections():
    assert decode_header(HEADER.pack(MAX_PAIRING_MESSAGE_SIZE), MAX_PAIRING_MESSAGE_SIZE) == 16 * 1024
    with pytest.raises(FramingError):
        decode_header(HEADER.pack(MAX_PAIRING_MESSAGE_SIZE + 1), MAX_PAIRING_MESSAGE_SIZE)


def test_oversized_message_cannot_be_encoded():
    with pytest.raises(FramingError, match="quá lớn"):
        encode_message({"data": "x" * MAX_MESSAGE_SIZE})


@pytest.mark.parametrize("body", [b"not json", b"[1, 2, 3]", b'"chuoi"', b"\xff\xfe"])
def test_invalid_body_raises(body):
    with pytest.raises(FramingError):
        decode_body(body)


# --- Phụ lục điểm 15: số nguyên quá dài, JSON lồng quá sâu → FramingError --------------------


@pytest.mark.parametrize(
    "body",
    [
        b'{"a":' + b"1" * 5000 + b"}",
        b'{"a":-' + b"9" * 4301 + b"}",
        b'{"a":[' + b"7" * 100_000 + b"]}",
    ],
    ids=["5000-chu-so", "4301-chu-so-am", "100k-chu-so-trong-list"],
)
def test_huge_integer_raises_framing_error(body):
    with pytest.raises(FramingError):
        decode_body(body)


@pytest.mark.parametrize(
    "body",
    [
        b"[" * 100_000,
        b"[" * 100_000 + b"]" * 100_000,
        b'{"a":' * 100_000 + b"1" + b"}" * 100_000,
        b'{"a":' + b"[" * 50_000 + b"]" * 50_000 + b"}",
    ],
    ids=["mo-ngoac-100k", "list-long-100k", "object-long-100k", "trong-object"],
)
def test_deeply_nested_json_raises_framing_error(body):
    with pytest.raises(FramingError):
        decode_body(body)


def test_moderately_nested_json_still_decodes():
    body = b'{"a":' + b"[" * 50 + b"]" * 50 + b"}"
    value = decode_body(body)["a"]
    depth = 0
    while value != []:
        assert isinstance(value, list) and len(value) == 1
        value = value[0]
        depth += 1
    assert depth == 49


def nested(depth: int) -> bytes:
    """Object ngoài cùng (cấp 1) chứa list lồng nhau, tổng cộng đúng `depth` cấp."""
    inner = depth - 1
    return b'{"a":' + b"[" * inner + b"]" * inner + b"}"


def test_depth_exactly_at_limit_is_accepted():
    assert isinstance(decode_body(nested(MAX_JSON_DEPTH)), dict)


@pytest.mark.parametrize("depth", [MAX_JSON_DEPTH + 1, 1_000])
def test_depth_over_limit_is_rejected_on_every_platform(depth):
    with pytest.raises(FramingError, match="lồng"):
        decode_body(nested(depth))


def test_depth_limit_counts_objects_too():
    def objects(depth: int) -> bytes:
        return b'{"a":' * depth + b"1" + b"}" * depth

    assert isinstance(decode_body(objects(MAX_JSON_DEPTH)), dict)
    with pytest.raises(FramingError):
        decode_body(objects(MAX_JSON_DEPTH + 1))


def test_brackets_inside_strings_do_not_count_as_nesting():
    body = json.dumps({"text": "[{" * 500, "list": [1, 2, 3]}).encode()
    assert decode_body(body)["text"] == "[{" * 500


def test_wide_but_shallow_json_is_accepted():
    body = json.dumps({"hashes": [[i] for i in range(10_000)]}).encode()
    assert len(decode_body(body)["hashes"]) == 10_000


# --- Fuzz decode_body (phụ lục điểm 18) --------------------------------------------------------

json_like = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=20),
    lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(max_size=8), c, max_size=4),
    max_leaves=20,
)


@pytest.mark.slow
@settings(max_examples=500, deadline=None)
@given(
    st.binary(max_size=400)
    | json_like.map(lambda v: json.dumps(v).encode())
    | st.text(alphabet='{}[]":,0123456789-+.eE nultrfasNI\\u', max_size=200).map(str.encode)
)
def test_fuzz_decode_body_returns_dict_or_framing_error(body):
    try:
        msg = decode_body(body)
    except FramingError:
        return
    assert isinstance(msg, dict)


@pytest.mark.slow
@settings(max_examples=200, deadline=None)
@given(st.dictionaries(st.text(max_size=10), json_like, max_size=5))
def test_fuzz_encode_decode_roundtrip_of_objects(obj):
    frame = encode_message(obj)
    length, body = split(frame)
    assert length == len(body)
    # NaN != NaN nên so sánh qua dạng JSON chuẩn.
    assert json.dumps(decode_body(body), sort_keys=True) == json.dumps(obj, sort_keys=True)


# --- PR-1 D1: trần khung điều khiển 1 MiB ----------------------------------------------------
#
# Con số lấy từ đặc tả PR-1 / docs/protocol.md mục 2.3, không từ hằng số của code.

ONE_MIB = 1024 * 1024


def body_of_size(size: int) -> dict:
    """Object có thân JSON compact (như `encode_message`) dài đúng `size` byte."""
    obj = {"d": "x" * (size - len(b'{"d":""}'))}
    assert len(json.dumps(obj, separators=(",", ":")).encode()) == size
    return obj


def test_control_limit_is_one_mib_and_pairing_limit_stays_16_kib():
    assert MAX_MESSAGE_SIZE == ONE_MIB
    assert MAX_PAIRING_MESSAGE_SIZE == 16 * 1024


def test_encode_accepts_body_of_exactly_one_mib():
    frame = encode_message(body_of_size(ONE_MIB))
    assert len(frame) == HEADER.size + ONE_MIB
    assert decode_header(frame[: HEADER.size]) == ONE_MIB


def test_encode_rejects_body_one_byte_over_one_mib():
    with pytest.raises(FramingError, match="quá lớn"):
        encode_message(body_of_size(ONE_MIB + 1))


@pytest.mark.parametrize("length", [ONE_MIB + 1, 16 * ONE_MIB, 0xFFFFFFFF], ids=["1MiB+1", "16MiB", "max"])
def test_header_over_one_mib_is_rejected_by_default(length):
    with pytest.raises(FramingError, match="quá lớn"):
        decode_header(HEADER.pack(length))


def test_header_of_exactly_one_mib_is_accepted_by_default():
    assert decode_header(HEADER.pack(ONE_MIB)) == ONE_MIB


def test_header_accepts_bytearray():
    assert decode_header(bytearray(HEADER.pack(5))) == 5


def test_decode_body_accepts_bytearray():
    body = '{"type":"ping","t":1,"text":"Xin chào 🚀"}'.encode()
    assert (
        decode_body(bytearray(body)) == decode_body(body) == {"type": "ping", "t": 1, "text": "Xin chào 🚀"}
    )


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1, 2, 3]",
        b'"chuoi"',
        b"\xff\xfe",
        b'{"a":"\xc3"}',
        b'{"a":' + b"1" * 5000 + b"}",
        nested(MAX_JSON_DEPTH + 1),
        b'{"a":' * (MAX_JSON_DEPTH + 1) + b"1" + b"}" * (MAX_JSON_DEPTH + 1),
        b"[" * 100_000,
    ],
    ids=["khong-json", "mang", "chuoi", "ff-fe", "utf8-dang-do", "so-dai", "list-65", "object-65", "mo-100k"],
)
def test_decode_body_keeps_every_check_for_bytearray(body):
    """D1: bên đọc đưa thẳng `bytearray` vào decode; mọi kiểm tra của thân bytes vẫn phải giữ."""
    with pytest.raises(FramingError):
        decode_body(bytearray(body))


@settings(max_examples=300, deadline=None)
@given(
    st.binary(max_size=300)
    | json_like.map(lambda v: json.dumps(v).encode())
    | st.text(alphabet='{}[]":,0123456789-+.eE nultrfasNI\\u', max_size=200).map(str.encode)
)
def test_fuzz_bytearray_and_bytes_decode_the_same(body):
    def outcome(b):
        try:
            return ("ok", json.dumps(decode_body(b), sort_keys=True))
        except FramingError:
            return ("error", None)

    assert outcome(bytearray(body)) == outcome(bytes(body))


# --- Thân phải là UTF-8 đúng nghĩa (protocol mục 3.1) ----------------------------------------
#
# `json.loads(bytes)` tự dò mã hóa (UTF-16/32 theo BOM hoặc byte 0, bỏ qua BOM UTF-8) và
# `surrogatepass` sẽ nhận surrogate mã hóa thành byte; cả hai đều không phải UTF-8 hợp lệ.
# Surrogate dạng thoát JSON (`"\ud800"`) là JSON hợp lệ nên vẫn qua tầng khung (tầng message
# kiểm riêng): không thuộc nhóm này.

PING = '{"type":"ping","t":1}'
NOT_UTF8_BODIES = {
    "utf-16": PING.encode("utf-16"),
    "utf-16-le": PING.encode("utf-16-le"),
    "utf-16-be": PING.encode("utf-16-be"),
    "utf-32": PING.encode("utf-32"),
    "bom-utf-8": b"\xef\xbb\xbf" + PING.encode(),
    "surrogate-byte": b'{"a":"\xed\xa0\x80"}',
    "surrogate-byte-cuoi": b'{"type":"ping","t":1,"x":"\xed\xbf\xbf"}',
}


@pytest.mark.parametrize("container", [bytes, bytearray], ids=["bytes", "bytearray"])
@pytest.mark.parametrize("body", list(NOT_UTF8_BODIES.values()), ids=list(NOT_UTF8_BODIES))
def test_body_not_strict_utf8_is_rejected(body, container):
    with pytest.raises(FramingError):
        decode_body(container(body))


def test_plain_utf8_body_with_non_ascii_text_is_accepted():
    """Đối chứng cho nhóm trên: UTF-8 không BOM, có chữ có dấu và emoji (4 byte) vẫn nhận."""
    body = '{"type":"ping","t":1,"x":"Tiếng Việt 🚀"}'.encode()
    assert decode_body(body)["x"] == "Tiếng Việt 🚀"
    assert decode_body(bytearray(body))["x"] == "Tiếng Việt 🚀"


# --- Kiểm độ sâu JSON: chỉ đúng/sai, không đo thời gian --------------------------------------


def ref_depth(value: object) -> int:
    """Bộ đếm tham chiếu: số cấp dict/list lồng nhau, container ngoài cùng là cấp 1, lá là 0."""
    if isinstance(value, dict):
        return 1 + max((ref_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((ref_depth(v) for v in value), default=0)
    return 0


def chain(kinds: str, leaf: object = 1) -> object:
    """Lồng `leaf` theo `kinds` từ ngoài vào trong: "d" là dict {"k": …}, "l" là list [ … ]."""
    value = leaf
    for kind in reversed(kinds):
        value = {"k": value} if kind == "d" else [value]
    return value


def compact(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


@pytest.mark.parametrize(
    "inner",
    ["l", "d", "ld", "dl", "ldd", "dll"],
    ids=["list", "dict", "list-dict", "dict-list", "list-dict-dict", "dict-list-list"],
)
def test_depth_limit_is_exact_for_mixed_nesting(inner):
    """Object ngoài cùng là cấp 1; lồng tới đúng MAX_JSON_DEPTH nhận, thêm một cấp bị từ chối."""
    pattern = (inner * MAX_JSON_DEPTH)[: MAX_JSON_DEPTH - 1]
    at_limit = {"v": chain(pattern)}
    assert ref_depth(at_limit) == MAX_JSON_DEPTH
    assert decode_body(compact(at_limit)) == at_limit
    over = {"v": chain(pattern + inner[0])}
    assert ref_depth(over) == MAX_JSON_DEPTH + 1
    with pytest.raises(FramingError, match="lồng"):
        decode_body(compact(over))


@pytest.mark.parametrize("leaf", [1, {}, [], "x"], ids=["so", "dict-rong", "list-rong", "chuoi"])
def test_empty_container_as_innermost_level_counts_as_a_level(leaf):
    """`{}` / `[]` ở trong cùng là một cấp, số hay chuỗi thì không."""
    extra = 1 if isinstance(leaf, dict | list) else 0
    value = {"v": chain("l" * (MAX_JSON_DEPTH - 1 - extra), leaf)}
    assert ref_depth(value) == MAX_JSON_DEPTH
    assert decode_body(compact(value)) == value
    deeper = {"v": chain("l" * (MAX_JSON_DEPTH - extra), leaf)}
    with pytest.raises(FramingError):
        decode_body(compact(deeper))


def test_wide_shallow_json_of_about_one_mib_is_accepted():
    """Rộng mà nông: ~350 nghìn `{}` / `[]` trong một mảng, thân gần đúng 1 MiB, độ sâu 3."""
    head, tail = b'{"a":[', b"]}"
    count = (MAX_MESSAGE_SIZE - len(head) - len(tail) + 1) // 3
    items = [b"{}" if i % 2 else b"[]" for i in range(count)]
    body = head + b",".join(items) + tail
    assert count >= 349_000
    assert len(body) <= MAX_MESSAGE_SIZE
    msg = decode_body(body)
    assert len(msg["a"]) == count
    assert ref_depth(msg) == 3


def test_many_siblings_at_limit_then_one_too_deep_at_the_end_is_rejected():
    """Mọi nhánh đều phải được kiểm: 1000 nhánh đúng trần, nhánh cuối sâu hơn một cấp."""
    ok_branch = chain("l" * (MAX_JSON_DEPTH - 2))
    siblings = [ok_branch] * 1000
    assert decode_body(compact({"a": siblings})) == {"a": siblings}
    with pytest.raises(FramingError):
        decode_body(compact({"a": [*siblings, chain("l" * (MAX_JSON_DEPTH - 1))]}))


@pytest.mark.parametrize(
    "value",
    [
        {"t": "[" * 500_000},
        {"t": "{" * 250_000 + "[" * 250_000},
        {"[[[[{{{{": "]]}}" * 1000, "x": ['"[[[[', "\\{{{{", '\\"[[[{{{']},
        {"t": '\\"' + "[" * 10_000 + '\\"'},
    ],
    ids=["mo-ngoac-vuong-500k", "tron-500k", "trong-khoa-va-chuoi-thoat", "dau-nhay-thoat"],
)
def test_brackets_inside_strings_and_keys_are_not_nesting(value):
    """`[` / `{` trong chuỗi (kể cả trong khóa, sau dấu nháy thoát) không làm tăng độ sâu."""
    body = compact(value)
    assert len(body) <= MAX_MESSAGE_SIZE
    assert decode_body(body) == value


# Thân viết tay: các dạng `json.dumps` không bao giờ sinh (`\u0022`, `\u005c`, `\u005b`, `\/`,
# khoảng trắng giữa token) — đúng chỗ cách đếm ngoặc trên bytes dễ sai nếu sau này bị sửa.
_HAND_WRITTEN = [
    b'{"a":"\\u0022[[[","b":"\\\\"}',
    b'{ "a" : [ "\\/]" ,\t"\\\\\\"[" ] }',
    b'{"\\u005b[":"\\u005c","b":["\\u0022",{"c":"\\\\\\\\"}]}',
    b'{\r\n"a":\n[\n"\\"]]]",\n{ }\n]\n}',
]


@pytest.mark.parametrize("body", _HAND_WRITTEN)
@pytest.mark.parametrize("kind", [bytes, bytearray])
def test_hand_written_escapes_and_whitespace_do_not_change_depth(body, kind):
    assert decode_body(kind(body)) == json.loads(body)


@pytest.mark.parametrize("kind", [bytes, bytearray])
def test_hand_written_escapes_inside_nesting_at_and_over_limit(kind):
    """Lồng đúng trần thì nhận, thêm một cấp thì từ chối, dù mỗi cấp có chuỗi thoát khó."""
    filler = b'"\\u0022[","\\\\","\\/{", '

    def nested(levels: int) -> bytes:
        # object ngoài cùng là cấp 1, mỗi `[` thêm một cấp.
        return b'{"a": ' + (b"[ " + filler) * (levels - 1) + b"0" + b" ]" * (levels - 1) + b"}"

    assert isinstance(decode_body(kind(nested(MAX_JSON_DEPTH))), dict)
    with pytest.raises(FramingError):
        decode_body(kind(nested(MAX_JSON_DEPTH + 1)))


def test_duplicate_key_branch_still_counts_toward_depth():
    """Đếm theo văn bản: nhánh quá sâu bị khóa trùng ghi đè vẫn làm khung bị từ chối."""
    deep = b"[" * (MAX_JSON_DEPTH + 5) + b"]" * (MAX_JSON_DEPTH + 5)
    with pytest.raises(FramingError):
        decode_body(b'{"type":"ping","t":1,"a":' + deep + b',"a":1}')


# Cây ngẫu nhiên, cộng thêm một "xương sống" lồng sâu quanh trần để luôn chạm được cả hai phía.
_leaves = st.none() | st.booleans() | st.integers(-1000, 1000) | st.text(alphabet='[]{}"\\ab', max_size=6)
_trees = st.recursive(
    _leaves,
    lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(alphabet="[{ab", max_size=3), c, max_size=3),
    max_leaves=12,
)


@st.composite
def _deep_documents(draw) -> dict:
    spine = draw(st.text(alphabet="ld", min_size=MAX_JSON_DEPTH - 8, max_size=MAX_JSON_DEPTH + 4))
    leaf = draw(_trees)
    value = chain(spine, leaf)
    # Gắn thêm cây ngẫu nhiên làm anh em ở cấp ngoài, để nhánh sâu nhất không phải lúc nào cũng đứng đầu.
    siblings = draw(st.lists(_trees, max_size=3))
    return {"v": [*siblings, value]} if draw(st.booleans()) else {"v": value, "s": siblings}


@pytest.mark.slow
@settings(max_examples=400, deadline=None)
@given(_deep_documents() | st.dictionaries(st.text(max_size=4), _trees, max_size=4))
def test_fuzz_depth_check_agrees_with_reference_counter(doc):
    body = compact(doc)
    if ref_depth(doc) <= MAX_JSON_DEPTH:
        assert decode_body(body) == doc
        assert decode_body(bytearray(body)) == doc
    else:
        with pytest.raises(FramingError):
            decode_body(body)
        with pytest.raises(FramingError):
            decode_body(bytearray(body))


# --- PR-1 D2: các message lớn nhất của giao thức vừa trần 1 MiB --------------------------------
#
# Hằng số chép từ đặc tả đã duyệt (không đọc từ file .md, để sửa văn không làm gãy test):
CLIPBOARD_MESSAGE_MAX = 768 * 1024  # protocol §3.2: mỗi message `clipboard` sau mã hóa ≤ 768 KiB
FILE_NAME_MAX_BYTES = 255  # protocol §3.3: `name` ≤ 255 byte UTF-8, không có ký tự loại Cc
FILE_SIZE_MAX = 2**42  # protocol §3.3: `size` ≤ 2⁴² (4 TiB)
OFFER_FILES_MAX = 1000  # protocol §3.2: `offer` có 1–1000 file
BLOCK_SIZE = ONE_MIB  # protocol §4.2: khối 1 MiB


def test_largest_clipboard_message_fits_control_limit():
    overhead = len(compact({"type": "clipboard", "kind": "image/png", "sha256": "f" * 64, "data": ""}))
    msg = {
        "type": "clipboard",
        "kind": "image/png",
        "sha256": "f" * 64,
        "data": "A" * (CLIPBOARD_MESSAGE_MAX - overhead),
    }
    frame = encode_message(msg)
    assert len(frame) - HEADER.size == CLIPBOARD_MESSAGE_MAX <= MAX_MESSAGE_SIZE


def test_worst_case_offer_of_1000_files_fits_control_limit():
    """protocol §3.3: “`offer` 1000 file luôn vừa trần 1 MiB” với tên ≤ 255 byte, không có `Cc`.

    Tên xấu nhất cho JSON là toàn ký tự phải thoát (`"` và `\\`: 1 byte thành 2); ký tự không phải
    ASCII không bị thoát (`ensure_ascii=False`). Kiểm cả hai, với `size`/`mtime` lớn nhất.
    """
    names = (
        '"' * FILE_NAME_MAX_BYTES,
        "\\" * FILE_NAME_MAX_BYTES,
        "🚀" * (FILE_NAME_MAX_BYTES // 4),
        "đ" * (FILE_NAME_MAX_BYTES // 2),
    )
    for name in names:
        assert len(name.encode()) <= FILE_NAME_MAX_BYTES
        offer = {
            "type": "offer",
            "transfer_id": "f" * 32,
            "block_size": BLOCK_SIZE,
            "files": [{"name": name, "size": FILE_SIZE_MAX, "mtime": MAX_SAFE_INTEGER}] * OFFER_FILES_MAX,
        }
        assert len(encode_message(offer)) - HEADER.size <= MAX_MESSAGE_SIZE


def test_resume_of_largest_file_fits_control_limit():
    """protocol §3.3: `resume` của file lớn nhất (bitfield ⌈khối / 8⌉ byte, base64) vừa trần."""
    blocks = -(-FILE_SIZE_MAX // BLOCK_SIZE)
    bitfield = base64.b64encode(b"\xff" * -(-blocks // 8)).decode()
    resume = {
        "type": "resume",
        "transfer_id": "f" * 32,
        "file_index": OFFER_FILES_MAX - 1,
        "seq": MAX_SAFE_INTEGER,
        "bitfield": bitfield,
    }
    assert len(encode_message(resume)) - HEADER.size <= MAX_MESSAGE_SIZE
