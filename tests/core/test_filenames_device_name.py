"""Test `teleport.core.filenames.is_device_name` (spec-f5 mục 9 D3b).

Hàm công khai mới: tên có phải tên thiết bị của Windows không, cùng luật bước 9 của
`sanitize_filename` (so phần trước dấu chấm đầu tiên, đã bỏ dấu cách hai đầu, không phân biệt hoa
thường; ¹²³ là chữ số ở COM/LPT; CONIN$, CONOUT$), **không** tính tên nội bộ `.teleport`. `ui.ipc`
dùng nó để từ chối đường dẫn có thành phần là tên thiết bị.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

DEVICE_NAMES = [
    "CON",
    "con",
    "Con",
    "PRN",
    "AUX",
    "NUL",
    "nul",
    "CONIN$",
    "conout$",
    "COM1",
    "com9",
    "LPT1",
    "lpt9",
    "COM¹",
    "LPT²",
    "COM³",
    "CON.txt",
    "nul.tar.gz",
    "con .txt",
    " con",
    "CON ",
    "aux.",
    "Prn.Doc",
]

NOT_DEVICE_NAMES = [
    "",
    ".teleport",
    ".TELEPORT",
    "CONSOLE",
    "CONX.txt",
    "COM",
    "LPT",
    "COM10",
    "LPT12.txt",
    "a.con",
    "x.nul",
    "_CON",
    "CON_",
    "file.txt",
    "NULL",
    "AUXILIARY",
    "Tài liệu.docx",
]


def fn():
    from teleport.core import filenames

    return filenames


@pytest.mark.parametrize("name", DEVICE_NAMES)
def test_recognizes_windows_device_names(name):
    assert fn().is_device_name(name) is True


@pytest.mark.parametrize("name", NOT_DEVICE_NAMES)
def test_ordinary_and_internal_names_are_not_device_names(name):
    assert fn().is_device_name(name) is False


def test_internal_temp_dir_name_is_not_a_device_name_but_still_reserved_for_received_files():
    f = fn()
    assert f.is_device_name(f.TEMP_DIR_NAME) is False
    assert f.sanitize_filename(f.TEMP_DIR_NAME) != f.TEMP_DIR_NAME, "bước 9 vẫn đổi tên `.teleport`"


@pytest.mark.parametrize("name", DEVICE_NAMES)
def test_device_names_are_renamed_by_sanitize_filename(name):
    """Cùng luật với bước 9: tên nào `is_device_name` nhận thì bên nhận không bao giờ giữ nguyên."""
    f = fn()
    out = f.sanitize_filename(name)
    assert not f.is_device_name(out), (name, out)


_STEM = st.sampled_from(["CON", "PRN", "AUX", "NUL", "COM1", "LPT3", "CONIN$", "CONOUT$", "COM²"])
_CASE = st.lists(st.booleans(), min_size=8, max_size=8)
_EXT = st.sampled_from(["", ".txt", ".tar.gz", ".", ".mp4"])
_PAD = st.sampled_from(["", " ", "  "])


@given(_STEM, _CASE, _EXT, _PAD, _PAD)
def test_any_case_padding_and_extension_of_device_stem_is_device_name(stem, case, ext, left, right):
    mixed = "".join(c.lower() if flip else c for c, flip in zip(stem, case, strict=False))
    assert fn().is_device_name(left + mixed + right + ext)


@given(st.text(max_size=20))
def test_never_raises_and_agrees_with_sanitize_step_9(name):
    """Tên đã qua `sanitize_filename` không bao giờ là tên thiết bị (bước 9 thêm `_`)."""
    f = fn()
    assert isinstance(f.is_device_name(name), bool)
    assert not f.is_device_name(f.sanitize_filename(name))
