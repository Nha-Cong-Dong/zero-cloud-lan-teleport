"""Test `teleport.core.merkle` theo docs/protocol.md §4.4 (luật Merkle root) và kế hoạch GĐ2 P2-1.

Luật: lá là SHA-256 của từng khối theo thứ tự; nút cha = SHA-256(con trái ‖ con phải); tầng có
số nút lẻ thì nút cuối được đẩy thẳng lên tầng trên; root là nút còn lại cuối cùng. File rỗng
(0 khối): root = SHA-256(""). `merkle_root` nhận các lá nối liền thành một `bytes`/`bytearray`
32·N byte (bên gửi giữ hash trong `bytearray`, kế hoạch mục 2).

Giá trị kỳ vọng luôn tính **độc lập trong test** bằng `hashlib`, không gọi lại `merkle_root`.
"""

import hashlib
import re
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from teleport.core.merkle import merkle_root

PROTOCOL_MD = Path(__file__).resolve().parents[2] / "docs" / "protocol.md"


def h(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def leaf(i: int) -> bytes:
    """Lá thứ i: hash của một "khối" giả, dễ tái tạo."""
    return h(b"block-%d" % i)


def leaves(n: int) -> bytes:
    return b"".join(leaf(i) for i in range(n))


def ref_root_iterative(data: bytes) -> bytes:
    """Cài đặt tham chiếu theo đúng câu chữ §4.4: gộp từng tầng, nút lẻ cuối đẩy lên."""
    assert len(data) % 32 == 0
    level = [data[i : i + 32] for i in range(0, len(data), 32)]
    if not level:
        return h(b"")
    while len(level) > 1:
        nxt = [h(level[i] + level[i + 1]) for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0]


def ref_root_recursive(nodes: list[bytes]) -> bytes:
    """Tham chiếu thứ hai, viết khác hẳn: tách tại lũy thừa 2 lớn nhất nhỏ hơn n.

    Với luật "nút lẻ đẩy thẳng lên", cây trái luôn là cây đầy đủ 2^k lá, nên hai cách cho
    cùng kết quả; hai oracle khớp nhau là bằng chứng bản tham chiếu không sai cùng kiểu với code.
    """
    if len(nodes) == 1:
        return nodes[0]
    k = 1
    while k * 2 < len(nodes):
        k *= 2
    return h(ref_root_recursive(nodes[:k]) + ref_root_recursive(nodes[k:]))


# --- Vector tính tay theo luật (0, 1, 2, 3, 5 lá) ---------------------------------------------


def test_empty_file_root_is_sha256_of_empty_string():
    assert merkle_root(b"") == hashlib.sha256(b"").digest()
    assert merkle_root(b"").hex() == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_empty_bytearray_root_is_sha256_of_empty_string():
    assert merkle_root(bytearray()) == hashlib.sha256(b"").digest()


def test_one_leaf_root_is_the_leaf_itself():
    # Tầng duy nhất có một nút: đó là root, không băm thêm.
    assert merkle_root(leaf(0)) == leaf(0)


def test_two_leaves_root_is_hash_of_concatenation():
    assert merkle_root(leaves(2)) == h(leaf(0) + leaf(1))


def test_two_leaves_order_matters():
    assert merkle_root(leaf(1) + leaf(0)) == h(leaf(1) + leaf(0))
    assert merkle_root(leaf(1) + leaf(0)) != merkle_root(leaves(2))


def test_three_leaves_last_leaf_promoted_unchanged():
    expected = h(h(leaf(0) + leaf(1)) + leaf(2))
    assert merkle_root(leaves(3)) == expected
    # Không nhân đôi nút lẻ kiểu Bitcoin.
    assert merkle_root(leaves(3)) != h(h(leaf(0) + leaf(1)) + h(leaf(2) + leaf(2)))
    # Không băm lại nút lẻ khi đẩy lên.
    assert merkle_root(leaves(3)) != h(h(leaf(0) + leaf(1)) + h(leaf(2)))


def test_four_leaves_balanced_tree():
    expected = h(h(leaf(0) + leaf(1)) + h(leaf(2) + leaf(3)))
    assert merkle_root(leaves(4)) == expected


def test_five_leaves_odd_node_promoted_twice():
    h01 = h(leaf(0) + leaf(1))
    h23 = h(leaf(2) + leaf(3))
    # Tầng 1: [h01, h23, lá 4]; tầng 2: [h(h01‖h23), lá 4]; root.
    expected = h(h(h01 + h23) + leaf(4))
    assert merkle_root(leaves(5)) == expected


def test_accepts_bytearray_and_does_not_mutate_it():
    data = bytearray(leaves(5))
    snapshot = bytes(data)
    assert merkle_root(data) == merkle_root(snapshot)
    assert data == snapshot


def test_root_is_32_bytes():
    for n in (0, 1, 2, 3, 7, 64):
        root = merkle_root(leaves(n))
        assert isinstance(root, bytes)
        assert len(root) == 32


def test_changing_any_leaf_changes_root():
    base = leaves(5)
    root = merkle_root(base)
    for i in range(5):
        changed = bytearray(base)
        changed[32 * i] ^= 1
        assert merkle_root(changed) != root


@pytest.mark.parametrize("length", [1, 31, 33, 63, 65])
def test_leaves_not_multiple_of_32_rejected(length):
    # Lựa chọn của test: đầu vào không phải 32·N byte là lỗi lập trình, phải báo (ValueError).
    with pytest.raises(ValueError):
        merkle_root(bytes(length))


def test_ten_gib_file_root_matches_reference():
    # File 10 GiB = 10240 khối (ví dụ trong protocol §4.2: 320 KiB hash).
    data = b"".join(h(i.to_bytes(4, "big")) for i in range(10240))
    assert len(data) == 320 * 1024
    assert merkle_root(data) == ref_root_iterative(data)


# --- Hai tham chiếu khớp nhau, và khớp code (hypothesis) ---------------------------------------


@settings(max_examples=200, deadline=None)
@given(st.integers(min_value=1, max_value=300))
def test_reference_implementations_agree(n):
    data = leaves(n)
    assert ref_root_iterative(data) == ref_root_recursive([data[i : i + 32] for i in range(0, len(data), 32)])


@settings(max_examples=300, deadline=None)
@given(st.lists(st.binary(min_size=32, max_size=32), max_size=200))
def test_property_root_matches_reference(leaf_list):
    data = b"".join(leaf_list)
    assert merkle_root(data) == ref_root_iterative(data)
    assert merkle_root(bytearray(data)) == ref_root_iterative(data)


# --- Test vector trong docs/protocol.md §4.4 ---------------------------------------------------


def _merkle_section() -> str:
    text = PROTOCOL_MD.read_text(encoding="utf-8")
    start = text.index("### 4.4.")
    end = text.index("\n## ", start)
    return text[start:end]


def _doc_vector_rows() -> dict[int, str]:
    """Bảng `| Số lá | Root (hex) |` ngay sau câu "Test vector Merkle" trong §4.4."""
    section = _merkle_section()
    m = re.search(r"Test vector Merkle", section)
    assert m, "protocol.md §4.4 chưa có test vector Merkle"
    rows = re.findall(r"^\|\s*(\d+)\s*\|\s*`([0-9a-f]{64})`\s*\|\s*$", section[m.start() :], flags=re.M)
    assert rows, "không đọc được bảng vector Merkle (cột 1: số lá, cột 2: root 64 hex)"
    return {int(n): root for n, root in rows}


def doc_leaf(i: int) -> bytes:
    """Quy ước lá của vector trong tài liệu: SHA-256 của khối 1 byte có giá trị i."""
    return h(bytes([i]))


def test_protocol_md_states_leaf_convention():
    # Câu quy ước phải còn đúng như test dùng; đổi quy ước thì sửa cả `doc_leaf`.
    section = _merkle_section()
    assert "SHA-256(bytes([i]))" in section


def test_protocol_md_has_merkle_vectors_for_0_1_2_3_5_leaves():
    assert set(_doc_vector_rows()) >= {0, 1, 2, 3, 5}


def test_protocol_md_merkle_vectors_match_independent_computation():
    """Mỗi root trong tài liệu được tính lại bằng hashlib theo câu chữ của luật (ba cách
    độc lập: tham chiếu lặp, tham chiếu đệ quy, công thức viết tay), rồi mới so với code."""
    rows = _doc_vector_rows()
    lv = [doc_leaf(i) for i in range(5)]
    by_hand = {
        0: hashlib.sha256(b"").digest(),
        1: lv[0],
        2: h(lv[0] + lv[1]),
        3: h(h(lv[0] + lv[1]) + lv[2]),
        5: h(h(h(lv[0] + lv[1]) + h(lv[2] + lv[3])) + lv[4]),
    }
    for n, root_hex in rows.items():
        data = b"".join(doc_leaf(i) for i in range(n))
        assert ref_root_iterative(data).hex() == root_hex, f"vector {n} lá trong tài liệu sai"
        if n:
            assert ref_root_recursive([doc_leaf(i) for i in range(n)]).hex() == root_hex
        if n in by_hand:
            assert by_hand[n].hex() == root_hex, f"vector {n} lá lệch công thức viết tay"
        assert merkle_root(data).hex() == root_hex, f"code lệch vector {n} lá"
