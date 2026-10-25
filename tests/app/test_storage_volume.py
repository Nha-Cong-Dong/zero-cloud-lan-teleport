"""Test `volume_info` và `_parse_mountinfo` của `teleport.app.storage` (P2-5).

Viết theo đặc tả (spec P2-5 mục 3.4, AC-26…AC-28, H4), không theo code. Nguồn: protocol §3.3
(`reject.file_too_large`: FAT32 không chứa file ≥ 4 GiB), ke_hoach §8 dòng FAT32, architecture §4
“Thư mục nhận” (tập FAT; `exfat`, `fuseblk` và hệ file lạ → không giới hạn).

Luật kiểm ở đây:
- `fs_type` = tên hệ file chữ thường, bỏ khoảng trắng hai đầu; rỗng → `None`.
- `max_file_size` = 2³² − 1 khi `fs_type` thuộc tập FAT, còn lại `None`.
- `free_bytes` = `shutil.disk_usage(path).free`; lỗi `OSError` bay ra.
- `_fs_type_of` không bao giờ raise. `_parse_mountinfo` là hàm thuần, không bao giờ raise: mount point
  dài nhất thắng (so theo ranh giới thành phần), trùng mount point thì dòng sau thắng, escape bát phân
  được giải mã, dòng hỏng bị bỏ qua.
"""

import collections
import importlib
import os
import shutil
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

WINDOWS = sys.platform == "win32"
FAT_LIMIT = 2**32 - 1


@pytest.fixture(scope="module")
def storage():
    return importlib.import_module("teleport.app.storage")


# ---------------------------------------------------------------------------------------------
# AC-26 Hệ file thật
# ---------------------------------------------------------------------------------------------


def test_volume_info_on_tmp_path(storage, tmp_path):
    info = storage.volume_info(tmp_path)
    assert isinstance(info, storage.VolumeInfo)
    assert isinstance(info.fs_type, str)
    assert info.fs_type
    assert info.fs_type == info.fs_type.lower() == info.fs_type.strip()
    assert info.max_file_size is None
    assert type(info.free_bytes) is int
    assert info.free_bytes >= 0


def test_volume_info_accepts_str_path(storage, tmp_path):
    assert storage.volume_info(str(tmp_path)).fs_type == storage.volume_info(tmp_path).fs_type


@pytest.mark.skipif(not WINDOWS, reason="chỉ Windows")
def test_windows_tmp_is_ntfs_or_refs(storage, tmp_path):
    assert storage.volume_info(tmp_path).fs_type in {"ntfs", "refs"}


@pytest.mark.skipif(sys.platform != "darwin", reason="chỉ macOS")
def test_macos_tmp_is_apfs(storage, tmp_path):
    assert storage.volume_info(tmp_path).fs_type == "apfs"


Usage = collections.namedtuple("Usage", "total used free")


def test_free_bytes_come_from_disk_usage(storage, tmp_path, monkeypatch):
    seen = []

    def fake_usage(path):
        seen.append(os.fspath(path))
        return Usage(10**12, 1, 123_456_789)

    monkeypatch.setattr(shutil, "disk_usage", fake_usage)
    assert storage.volume_info(tmp_path).free_bytes == 123_456_789
    assert seen


def test_disk_usage_error_propagates(storage, tmp_path, monkeypatch):
    def broken(path):
        raise FileNotFoundError(2, "o dia vua bi rut")

    monkeypatch.setattr(shutil, "disk_usage", broken)
    with pytest.raises(OSError):
        storage.volume_info(tmp_path)


def test_fs_type_of_receives_realpath(storage, tmp_path, monkeypatch):
    seen = []

    def fake(path):
        seen.append(path)
        return "ext4"

    monkeypatch.setattr(storage, "_fs_type_of", fake)
    storage.volume_info(tmp_path)
    assert seen == [os.path.realpath(tmp_path)]


def test_receive_dir_volume_info_uses_root(storage, tmp_path, monkeypatch):
    seen = []

    def fake(path):
        seen.append(path)
        return "FAT32"

    monkeypatch.setattr(storage, "_fs_type_of", fake)
    with storage.ReceiveDir.open(tmp_path / "recv") as rd:
        info = rd.volume_info()
    assert isinstance(info, storage.VolumeInfo)
    assert info.fs_type == "fat32"
    assert info.max_file_size == FAT_LIMIT
    assert seen[0] == os.path.realpath(tmp_path / "recv")


def test_fs_type_of_never_raises_on_odd_paths(storage, tmp_path):
    for path in [str(tmp_path / "khong-ton-tai" / "x"), "", "\x00", "relative/path", "C:\\khong\\co"]:
        out = storage._fs_type_of(path)
        assert out is None or isinstance(out, str)


# ---------------------------------------------------------------------------------------------
# AC-27 Giả lập hệ file (vá `_fs_type_of`)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("fat32", "fat32"),
        ("FAT32", "fat32"),
        ("vfat", "vfat"),
        ("msdos", "msdos"),
        ("fat", "fat"),
        (" FAT16 ", "fat16"),
        ("fat12", "fat12"),
        ("umsdos", "umsdos"),
        ("FAT", "fat"),
    ],
)
def test_fat_family_has_4gib_limit(storage, tmp_path, monkeypatch, raw, expected):
    monkeypatch.setattr(storage, "_fs_type_of", lambda path: raw)
    info = storage.volume_info(tmp_path)
    assert info.fs_type == expected
    assert info.max_file_size == FAT_LIMIT


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("exfat", "exfat"),
        ("exFAT", "exfat"),
        ("ntfs", "ntfs"),
        ("NTFS", "ntfs"),
        ("fuseblk", "fuseblk"),
        ("apfs", "apfs"),
        ("ext4", "ext4"),
        ("fat64", "fat64"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_other_filesystems_have_no_known_limit(storage, tmp_path, monkeypatch, raw, expected):
    monkeypatch.setattr(storage, "_fs_type_of", lambda path: raw)
    info = storage.volume_info(tmp_path)
    assert info.fs_type == expected
    assert info.max_file_size is None


# ---------------------------------------------------------------------------------------------
# AC-28 `_parse_mountinfo` (chạy trên mọi OS)
# ---------------------------------------------------------------------------------------------

MOUNTINFO = "\n".join(
    [
        "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw,errors=remount-ro",
        "40 22 8:17 / /mnt/usb rw,nosuid shared:2 master:3 - vfat /dev/sdb1 rw,fmask=0022",
        "41 22 8:33 / /mnt/my\\040usb rw - exfat /dev/sdc1 rw",
        "42 22 8:49 / /mnt/win rw - fuseblk /dev/sdd1 rw,user_id=0",
        "43 40 0:50 / /mnt/usb/inner rw - tmpfs tmpfs rw",
        "",
    ]
)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/mnt/usb/x/y.bin", "vfat"),  # mount point dài nhất thắng `/`
        ("/mnt/usb/inner/z", "tmpfs"),  # lồng sâu hơn nữa
        ("/mnt/usb", "vfat"),  # đường dẫn bằng đúng mount point
        ("/mnt/usb2/x", "ext4"),  # `/mnt/usb` không khớp `/mnt/usb2`
        ("/mnt/my usb/x", "exfat"),  # \040 là dấu cách
        ("/mnt/win/a", "fuseblk"),
        ("/home/u/Downloads", "ext4"),
        ("/", "ext4"),
    ],
)
def test_parse_mountinfo_longest_mount_point_wins(storage, path, expected):
    assert storage._parse_mountinfo(MOUNTINFO, path) == expected


def test_parse_mountinfo_later_duplicate_wins(storage):
    text = "\n".join(
        [
            "22 1 8:1 / / rw - ext4 /dev/sda1 rw",
            "40 22 8:17 / /mnt/usb rw - ext4 /dev/sdb1 rw",
            "50 22 8:18 / /mnt/usb rw - vfat /dev/sdc1 rw",  # mount sau che mount trước
        ]
    )
    assert storage._parse_mountinfo(text, "/mnt/usb/a") == "vfat"


def test_parse_mountinfo_optional_fields_before_separator(storage):
    text = "40 22 8:17 / /data rw shared:1 master:2 propagate_from:3 - xfs /dev/sdb1 rw"
    assert storage._parse_mountinfo(text, "/data/x") == "xfs"


def test_parse_mountinfo_separator_is_first_dash_after_sixth_field(storage):
    # Trường root (thứ 4) và options (thứ 6) bằng "-" không phải dấu phân cách.
    text = "\n".join(
        [
            "36 35 8:1 - /mnt/d rw - xfs /dev/x rw",
            "37 35 8:2 / /mnt/e - - btrfs /dev/y rw",
        ]
    )
    assert storage._parse_mountinfo(text, "/mnt/d/f") == "xfs"
    assert storage._parse_mountinfo(text, "/mnt/e/f") == "btrfs"


@pytest.mark.parametrize(
    ("mountpoint", "path"),
    [
        ("/mnt/a\\011b", "/mnt/a\tb/x"),
        ("/mnt/a\\012b", "/mnt/a\nb/x"),
        ("/mnt/a\\134b", "/mnt/a\\b/x"),
        ("/mnt/a\\040b\\040c", "/mnt/a b c/x"),
    ],
)
def test_parse_mountinfo_decodes_octal_escapes(storage, mountpoint, path):
    text = f"22 1 8:1 / / rw - ext4 /dev/sda1 rw\n40 22 8:17 / {mountpoint} rw - vfat /dev/sdb1 rw"
    assert storage._parse_mountinfo(text, path) == "vfat"


def test_parse_mountinfo_bad_escape_is_kept_literally(storage):
    text = "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n40 22 8:17 / /mnt/x\\9 rw - vfat /dev/sdb1 rw"
    assert storage._parse_mountinfo(text, "/mnt/x\\9/y") == "vfat"


def test_parse_mountinfo_skips_broken_lines(storage):
    text = "\n".join(
        [
            "22 1 8:1 / / rw - ext4 /dev/sda1 rw",
            "40 22 8:17 / /mnt/usb rw vfat /dev/sdb1 rw",  # thiếu "-"
            "41 22",  # ít trường
            "42 22 8:18 / /mnt/usb rw -",  # thiếu fstype sau "-"
            "\\ \\\\ - - -",
        ]
    )
    assert storage._parse_mountinfo(text, "/mnt/usb/a") == "ext4"


def test_parse_mountinfo_without_match_returns_none(storage):
    text = "40 22 8:17 / /mnt/usb rw - vfat /dev/sdb1 rw"
    assert storage._parse_mountinfo(text, "/home/u") is None


@pytest.mark.parametrize("text", ["", "\n\n", "rác không phải mountinfo"])
def test_parse_mountinfo_empty_or_garbage_returns_none(storage, text):
    assert storage._parse_mountinfo(text, "/mnt/usb") is None


def test_parse_mountinfo_handles_surrogate_escaped_bytes(storage):
    # Văn bản đọc bằng errors="surrogateescape": byte không phải UTF-8 thành surrogate lẻ.
    raw = b"22 1 8:1 / / rw - ext4 /dev/sda1 rw\n40 22 8:17 / /mnt/\xff\xfe rw - vfat /dev/sdb1 rw\n"
    text = raw.decode("utf-8", "surrogateescape")
    path = b"/mnt/\xff\xfe/x".decode("utf-8", "surrogateescape")
    assert storage._parse_mountinfo(text, path) == "vfat"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="chỉ Linux")
def test_linux_fs_type_of_root_is_known(storage):
    assert storage._fs_type_of("/") is not None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="chỉ Linux")
def test_linux_reads_mountinfo_from_seam(storage, tmp_path, monkeypatch):
    fake = tmp_path / "mountinfo"
    fake.write_text("22 1 8:1 / / rw - ext4 /dev/sda1 rw\n40 22 8:17 / /mnt/usb rw - vfat /dev/sdb1 rw\n")
    monkeypatch.setattr(storage, "_MOUNTINFO_PATH", str(fake))
    assert storage._fs_type_of("/mnt/usb/a") == "vfat"
    monkeypatch.setattr(storage, "_MOUNTINFO_PATH", str(tmp_path / "khong-co"))
    assert storage._fs_type_of("/mnt/usb/a") is None


def test_mountinfo_path_default(storage):
    assert storage._MOUNTINFO_PATH == "/proc/self/mountinfo"


# ---------------------------------------------------------------------------------------------
# H4 `_parse_mountinfo` không bao giờ raise
# ---------------------------------------------------------------------------------------------

PIECES = st.sampled_from(
    [
        "22",
        "1",
        "8:1",
        "/",
        "/mnt/usb",
        "/mnt/my\\040usb",
        "/mnt/x\\9",
        "\\",
        "\\777",
        "\\0400",
        "\\12",
        "-",
        "ext4",
        "vfat",
        "fuseblk",
        "rw",
        "shared:1",
        "master:2",
        "/dev/sda1",
        "",
        "\t",
    ]
)
ANY_TEXT = st.text(st.characters(exclude_categories=()), max_size=30)
LINE = st.lists(st.one_of(PIECES, ANY_TEXT), max_size=14).map(" ".join)
TEXT = st.one_of(
    st.lists(st.one_of(LINE, ANY_TEXT), max_size=20).map("\n".join),
    st.just("1 2 3:4 / /a rw - " + "x" * 100_000 + " src rw"),
    st.just("1 2 3:4 / /" + "\\" * 50_000 + " rw - ext4 src rw"),
)
PATHS = st.one_of(st.sampled_from(["/", "/mnt/usb/x", "/mnt/my usb/x", "", "/a/b"]), ANY_TEXT)


@settings(max_examples=500, deadline=None)
@given(TEXT, PATHS)
def test_property_parse_mountinfo_never_raises(storage, text, path):
    out = storage._parse_mountinfo(text, path)
    assert out is None or (isinstance(out, str) and out in text)
