"""Test `teleport.ui.ipc`: lệnh `show` / `send` giữa hai tiến trình app trên cùng máy (spec-f5 mục 2.2,
2.3, mục 8 và mục 9 D2, D3, D5, D11, D14).

Viết từ spec, không từ code. Module là hàm thuần (không Qt, không I/O), nên mọi luật được thử với cả
`windows=True` và `windows=False` trên mọi hệ điều hành. Thư mục `tests/ui` gắn marker `gui` cho mọi
test (`tests/ui/conftest.py`), kể cả file này; chỉ các ca D14 và ca hằng của `ui.app` cần PySide6.

Phủ:
- `check_path`: luật chung 1–4, luật Windows 5–10 cùng D3 a–e và D11, luật POSIX 11; độ dài
  `PATH_MAX_BYTES` ±1 tính theo byte UTF-8; không bao giờ ném; kết quả bất biến khi kiểm lại.
- `prepare_send(args, *, cwd, windows)`: nối với `cwd` (D2), bỏ trùng giữ lần đầu, `TOO_MANY` sau khi
  bỏ trùng, `BAD_PATH` kèm đường dẫn sai đầu tiên, rỗng → `BAD_PATH`.
- `encode_send` ↔ `IpcReader`: khứ hồi, đúng byte của định dạng, `TOO_LARGE` khi vượt `MAX_BYTES`.
- `IpcReader`: mọi `DropReason` mà bộ đọc tự gán (`timeout` do `SingleInstance` gán), đưa từng byte,
  ngắt giữa chừng (`close`), một quyết định duy nhất, tách dòng trên byte `\\n` (D3f: U+2028 không tách).
- D14: khóa so trùng của `ipc` và `send.selection_error` cho cùng kết quả.

Chỗ spec chưa ghim mã lý do (ví dụ `send\\n\\n`: không có dòng đường dẫn; dòng đầu quá
`HEADER_MAX_BYTES`), test chỉ đòi `Dropped` với một trong các mã hợp lý.
"""

from __future__ import annotations

import importlib
import ntpath
import os
import posixpath
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

HOST_WINDOWS = os.name == "nt"
BOTH = pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def ipc():
    return importlib.import_module("teleport.ui.ipc")


def _ipc():
    return importlib.import_module("teleport.ui.ipc")


# ---- Hàm phụ trợ ------------------------------------------------------------------------------------


def send_bytes(*lines: str) -> bytes:
    """Lệnh `send` đúng định dạng mục 2.2, dựng tay (không qua `encode_send`)."""
    return b"send\n" + b"".join(line.encode("utf-8") + b"\n" for line in lines) + b"\n"


def long_path(n_bytes: int, idx: int = 0, *, windows: bool) -> str:
    """Đường dẫn hợp lệ dài đúng `n_bytes` byte UTF-8 (ASCII), thành phần ≤ 200 ký tự, riêng theo `idx`."""
    sep = "\\" if windows else "/"
    head = "C:\\" if windows else "/"
    body_len = n_bytes - len(head)
    assert body_len >= 8
    body = (f"d{idx:05d}" + sep + ("a" * 199 + sep) * (body_len // 200 + 2))[:body_len]
    if body.endswith(sep):
        body = body[:-1] + "b"
    path = head + body
    assert len(path.encode("utf-8")) == n_bytes
    return path


def host_path(*parts: str) -> str:
    """Đường dẫn tuyệt đối theo hệ điều hành đang chạy test."""
    if HOST_WINDOWS:
        return "C:\\" + "\\".join(parts)
    return "/" + "/".join(parts)


def decide(reader, data: bytes, *, step: int | None = None):
    """Đưa `data` (cả cục, hay từng `step` byte) rồi `close()` nếu chưa có quyết định."""
    result = None
    if step is None:
        result = reader.feed(data)
    else:
        for i in range(0, len(data), step):
            result = reader.feed(data[i : i + step])
            if result is not None:
                break
    if result is None:
        result = reader.close()
    return result


def read_all(data: bytes, *, windows: bool, step: int | None = None):
    return decide(_ipc().IpcReader(windows=windows), data, step=step)


def dropped(*reasons: str):
    """Kiểm một kết quả là `Dropped` với mã nằm trong `reasons`."""
    m = _ipc()

    def check(result) -> None:
        assert isinstance(result, m.Dropped), f"phải bỏ lệnh, thấy {result!r}"
        assert str(result.reason) in reasons, f"mã lý do {result.reason!r}, mong đợi một trong {reasons}"

    return check


# =================================================================================================
# Hằng và API (mục 2.2, D5)
# =================================================================================================


def test_constants_match_spec(ipc):
    from teleport.core.messages import MAX_OFFER_FILES

    assert ipc.SHOW_COMMAND == b"show\n"
    assert ipc.SEND_HEADER == b"send\n"
    assert ipc.HEADER_MAX_BYTES == 16
    assert ipc.MAX_BYTES == 131_072
    assert ipc.SEND_MAX_FILES == MAX_OFFER_FILES
    assert ipc.PATH_MAX_BYTES == 4096


def test_drop_reasons_are_exactly_the_logged_codes(ipc):
    """D5: `DropReason` là đúng chín mã ghi log ở mục 2.4."""
    expected = {"header", "size", "utf8", "path", "count", "dup", "trailing", "timeout", "eof"}
    assert {str(r) for r in ipc.DropReason} == expected
    assert {r.value for r in ipc.DropReason} == expected


def test_send_args_error_kinds_and_is_value_error(ipc):
    assert {k.value for k in ipc.SendArgsErrorKind} == {"bad_path", "too_many", "too_large"}
    assert issubclass(ipc.SendArgsError, ValueError)


def test_command_values_are_frozen_dataclasses(ipc):
    assert ipc.ShowCommand() == ipc.ShowCommand()
    cmd = ipc.SendCommand(("C:\\a.txt",))
    assert cmd == ipc.SendCommand(("C:\\a.txt",))
    assert ipc.Dropped(ipc.DropReason.DUP) == ipc.Dropped(ipc.DropReason.DUP)
    assert ipc.Dropped(ipc.DropReason.DUP) != ipc.Dropped(ipc.DropReason.EOF)
    with pytest.raises((AttributeError, TypeError)):
        cmd.paths = ()  # type: ignore[misc]


def test_ipc_module_does_not_import_qt():
    """Việc 1: `ui/ipc.py` là hàm thuần, không import PySide6."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO / "src"), env.get("PYTHONPATH", "")) if p)
    code = "import sys, teleport.ui.ipc; print(any(m.split('.')[0] == 'PySide6' for m in sys.modules))"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=env, cwd=str(REPO)
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_app_reexports_show_command_and_ipc_limits(ipc):
    """Mục 2.2: `app.py` import lại `SHOW_COMMAND`; `IPC_MAX_BYTES` thành trần cả lệnh; hạn đọc và số
    kết nối giữ nguyên."""
    from teleport.ui import app

    assert app.SHOW_COMMAND == ipc.SHOW_COMMAND == b"show\n"
    assert app.IPC_MAX_BYTES == ipc.MAX_BYTES
    assert app.IPC_READ_TIMEOUT_MS == 1000
    assert app.IPC_MAX_CONNECTIONS == 4


# =================================================================================================
# check_path: luật chung (mục 2.3, 1–4)
# =================================================================================================


@BOTH
@pytest.mark.parametrize(
    "tail",
    ["", "\x00", "\t", "\r", "\n", "\x01", "\x1b", "\x1f", "\x7f", "\x80", "\x85", "\x9f"],
    ids=["empty", "nul", "tab", "cr", "lf", "soh", "esc", "us", "del", "c1-80", "nel", "c1-9f"],
)
def test_rejects_empty_and_control_characters(ipc, windows, tail):
    if tail == "":
        assert ipc.check_path("", windows=windows) is None
        return
    base = "C:\\Users\\An\\a" if windows else "/home/an/a"
    assert ipc.check_path(base + ".txt", windows=windows) is not None, "đầu vào gốc phải hợp lệ"
    assert ipc.check_path(base + tail + ".txt", windows=windows) is None


@BOTH
def test_rejects_lone_surrogate(ipc, windows):
    base = "C:\\Users\\An\\" if windows else "/home/an/"
    assert ipc.check_path(base + "a.txt", windows=windows) is not None
    assert ipc.check_path(base + "a\ud800.txt", windows=windows) is None
    assert ipc.check_path(base + "a\udfff.txt", windows=windows) is None


@BOTH
def test_path_max_bytes_boundary(ipc, windows):
    at_limit = long_path(4096, windows=windows)
    over = long_path(4097, windows=windows)
    assert ipc.check_path(at_limit, windows=windows) == at_limit
    assert ipc.check_path(over, windows=windows) is None


@BOTH
def test_length_limit_counts_utf8_bytes_not_characters(ipc, windows):
    sep = "\\" if windows else "/"
    head = "C:\\" if windows else "/"
    comp = "ả" * 50  # 150 byte
    path = head + sep.join([comp] * 28)  # 1400 + 27 + đầu: < 4096 ký tự, > 4096 byte
    assert len(path) < 4096 < len(path.encode("utf-8"))
    assert ipc.check_path(path, windows=windows) is None
    short = head + sep.join([comp] * 20)
    assert len(short.encode("utf-8")) <= 4096
    assert ipc.check_path(short, windows=windows) == short


@BOTH
@pytest.mark.parametrize("component", [".", ".."])
@pytest.mark.parametrize("where", ["middle", "end", "start"])
def test_rejects_dot_components_before_normalizing(ipc, windows, component, where):
    """Luật 3: `C:\\a\\..\\b` bị từ chối dù `normpath` sẽ thu thành `C:\\b`."""
    sep = "\\" if windows else "/"
    head = "C:\\" if windows else "/"
    parts = {"middle": ["a", component, "b.txt"], "end": ["a", component], "start": [component, "b.txt"]}
    path = head + sep.join(parts[where])
    assert ipc.check_path(path, windows=windows) is None


@BOTH
def test_rejects_trailing_separator_and_root(ipc, windows):
    if windows:
        cases = ["C:\\", "C:/", "C:", "C:\\dir\\", "C:/dir/", "\\\\srv\\share", "\\\\srv\\share\\"]
    else:
        cases = ["/", "/dir/", "/home/an/"]
    for case in cases:
        assert ipc.check_path(case, windows=windows) is None, case


@BOTH
@pytest.mark.parametrize(
    "name",
    [
        "a\u2028b.txt",
        "a\u2029b.txt",
        "a\u202egpj.exe",
        "a\u200bb.txt",
        "&lt;b&amp;x.txt",
        "Tài liệu ảnh 😀.jpg",
    ],
    ids=["u2028", "u2029", "rlo", "zwsp", "html", "unicode"],
)
def test_accepts_non_cc_unicode_names(ipc, windows, name):
    """Chỉ ký tự loại Cc bị cấm (luật 2); ký tự vô hình, RLO, U+2028 và thực thể HTML đi qua để hộp hiện
    bằng chữ thuần (tiêu chí 10). Thẻ `<b>` thì Windows cấm (D3c), nên ở đây dùng thực thể `&lt;`."""
    path = ("C:\\Users\\An\\" if windows else "/home/an/") + name
    assert ipc.check_path(path, windows=windows) == path


def test_windows_none_means_current_os(ipc):
    for text in ("C:\\Users\\An\\a.txt", "/home/an/a.txt", "C:/x/y.txt", "/a/CON", "/a/b:c"):
        assert ipc.check_path(text) == ipc.check_path(text, windows=HOST_WINDOWS), text


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(st.text(max_size=60), st.booleans())
def test_check_path_never_raises_and_result_is_stable(text, windows):
    m = _ipc()
    out = m.check_path(text, windows=windows)
    if out is None:
        return
    assert isinstance(out, str)
    assert m.check_path(out, windows=windows) == out, "kết quả đã chuẩn hóa phải tự hợp lệ"
    assert len(out.encode("utf-8")) <= m.PATH_MAX_BYTES
    assert not any(0 <= ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in out)
    if windows:
        assert "/" not in out
        assert (
            len(out) > 3 and out[0].isascii() and out[0].isalpha() and out[1:3] == ":\\"
        ) or out.startswith("\\\\")
    else:
        assert out.startswith("/") and not out.startswith("//")


@settings(max_examples=200)
@given(
    st.lists(st.sampled_from(["a", "b", ".", "..", "", "x.txt", "C:", ":", "CON", " ", "..."]), max_size=6),
    st.sampled_from(["C:\\", "C:/", "\\\\srv\\sh\\", "//srv/sh/", "/", "\\", ""]),
    st.booleans(),
)
def test_check_path_fuzz_with_path_fragments(parts, head, windows):
    m = _ipc()
    sep = "\\" if windows else "/"
    text = head + sep.join(parts)
    out = m.check_path(text, windows=windows)
    if out is not None:
        assert m.check_path(out, windows=windows) == out
        comps = out.split("\\" if windows else "/")
        assert "." not in comps and ".." not in comps


# =================================================================================================
# check_path: Windows (5–10, D3, D11)
# =================================================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("C:\\Users\\An\\a.txt", "C:\\Users\\An\\a.txt"),
        ("c:\\x.txt", "c:\\x.txt"),
        ("Z:\\a", "Z:\\a"),
        ("C:/Users/An/a.txt", "C:\\Users\\An\\a.txt"),
        ("C:\\Users/An\\a.txt", "C:\\Users\\An\\a.txt"),
        ("C:\\Users\\An\\.bashrc", "C:\\Users\\An\\.bashrc"),
        ("C:\\My Files\\a b.c.txt", "C:\\My Files\\a b.c.txt"),
        ("C:\\CONSOLE\\COM10.txt", "C:\\CONSOLE\\COM10.txt"),
        ("\\\\server\\share\\file.txt", "\\\\server\\share\\file.txt"),
        ("\\\\server\\share\\dir\\file.txt", "\\\\server\\share\\dir\\file.txt"),
        ("//server/share/file.txt", "\\\\server\\share\\file.txt"),
    ],
)
def test_windows_accepts_drive_and_unc_paths(ipc, text, expected):
    assert ipc.check_path(text, windows=True) == expected


@pytest.mark.parametrize(
    "text",
    [
        "a.txt",
        "dir\\a.txt",
        "\\abc",
        "\\abc\\x.txt",
        "/abc",
        "C:abc",
        "C:abc\\x.txt",
        "1:\\x.txt",
        "Đ:\\x.txt",
        "CC:\\x.txt",
        ":\\x.txt",
        "~\\x.txt",
    ],
)
def test_windows_rejects_relative_rooted_without_drive_and_bad_drive(ipc, text):
    """Luật 6 và 8: chỉ `X:\\…` (X là chữ ASCII) hay UNC."""
    assert ipc.check_path(text, windows=True) is None


@pytest.mark.parametrize(
    "text",
    [
        "\\\\?\\C:\\x.txt",
        "//?/C:/x.txt",
        "\\\\?\\UNC\\srv\\share\\x.txt",
        "\\\\.\\C:\\x.txt",
        "\\\\.\\PhysicalDrive0",
        "//./pipe/x",
        "\\\\.\\pipe\\teleport-x",
        "\\??\\C:\\x.txt",
        "/??/C:/x.txt",
    ],
)
def test_windows_rejects_device_and_namespace_prefixes(ipc, text):
    """Luật 7 và D11: `\\\\?\\`, `\\\\.\\`, `\\??\\` (kể cả `\\\\?\\C:\\…`)."""
    assert ipc.check_path(text, windows=True) is None


@pytest.mark.parametrize(
    "text",
    ["\\\\server", "\\\\server\\", "\\\\\\share\\x.txt", "\\\\server\\\\x.txt", "\\\\", "\\\\\\\\"],
)
def test_windows_rejects_unc_without_server_or_share(ipc, text):
    assert ipc.check_path(text, windows=True) is None


@pytest.mark.parametrize(
    "text",
    [
        "C:\\a.txt:stream",
        "C:\\a.txt::$DATA",
        "C:\\dir:x\\a.txt",
        "C:\\a:b",
        "\\\\srv\\share\\a.txt:stream",
        "\\\\srv\\share:x\\a.txt",
        "\\\\srv:1\\share\\a.txt",
        "C::\\a.txt",
    ],
)
def test_windows_rejects_colon_outside_drive_position(ipc, text):
    """Luật 9, D3e: `:` chỉ ở vị trí ổ đĩa (chặn alternate data stream), áp cả UNC."""
    assert ipc.check_path(text, windows=True) is None


@pytest.mark.parametrize(
    "text",
    [
        "C:\\dir.\\a.txt",
        "C:\\dir \\a.txt",
        "C:\\a.txt.",
        "C:\\a.txt ",
        "C:\\...\\a.txt",
        "C:\\ \\a.txt",
        "C:\\. .\\a.txt",
        "C:\\a\\....",
        "\\\\srv\\share\\dir.\\a.txt",
    ],
)
def test_windows_rejects_components_ending_with_dot_or_space(ipc, text):
    """D3a: Windows tự cắt dấu chấm/cách ở cuối, nên hai đường dẫn khác nhau trỏ cùng một file."""
    assert ipc.check_path(text, windows=True) is None


@pytest.mark.parametrize(
    "component",
    ["CON", "con", "NUL.txt", "nul.tar.gz", "con .txt", "COM1", "LPT9.log", "COM\u00b9", "CONIN$", "AUX"],
)
@pytest.mark.parametrize("where", ["last", "middle", "unc"])
def test_windows_rejects_device_name_components(ipc, component, where):
    """D3b: thành phần là tên thiết bị (`core.filenames.is_device_name`) mở thiết bị, không mở file."""
    path = {
        "last": f"C:\\Users\\An\\{component}",
        "middle": f"C:\\{component}\\a.txt",
        "unc": f"\\\\srv\\share\\{component}",
    }[where]
    assert ipc.check_path(path, windows=True) is None


def test_windows_device_rule_matches_core_filenames(ipc):
    """D3b dùng lại luật của `core.filenames`: tên nào `is_device_name` nhận thì bị từ chối, và ngược lại."""
    from teleport.core.filenames import is_device_name

    for name in ("CON", "con.txt", "PRN", "COM\u00b2", "CONOUT$", "CONSOLE", "COM10", "a.con", ".teleport"):
        ok = ipc.check_path(f"C:\\Users\\{name}", windows=True) is not None
        assert ok is (not is_device_name(name)), name


@pytest.mark.parametrize("ch", list('<>"|?*'))
def test_windows_rejects_forbidden_characters(ipc, ch):
    """D3c."""
    assert ipc.check_path("C:\\Users\\An\\ab.txt", windows=True) is not None
    assert ipc.check_path(f"C:\\Users\\An\\a{ch}b.txt", windows=True) is None


@pytest.mark.parametrize(
    "text",
    ["C:\\a\\\\b.txt", "C://a.txt", "C:/a//b.txt", "\\\\srv\\share\\\\a.txt", "\\\\srv\\share\\a\\\\b"],
)
def test_windows_rejects_empty_components_except_unc_prefix(ipc, text):
    """D3d."""
    assert ipc.check_path(text, windows=True) is None


# =================================================================================================
# check_path: POSIX (11)
# =================================================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/home/an/a.txt", "/home/an/a.txt"),
        ("/home//an/a.txt", "/home/an/a.txt"),
        ("///etc/x", "/etc/x"),
        ("/a/CON", "/a/CON"),
        ("/a/b:c", "/a/b:c"),
        ("/a/b.", "/a/b."),
        ("/a/b ", "/a/b "),
        ('/a/b<>|?*"', '/a/b<>|?*"'),
        ("/a\\b.txt", "/a\\b.txt"),
        ("/a/C:\\x", "/a/C:\\x"),
    ],
)
def test_posix_accepts_absolute_paths_and_windows_only_rules_do_not_apply(ipc, text, expected):
    assert ipc.check_path(text, windows=False) == expected


@pytest.mark.parametrize(
    "text",
    ["a.txt", "./a.txt", "~/a.txt", "C:\\x.txt", "C:/x.txt", "\\\\srv\\share\\x", "//srv/share/x", "//x"],
)
def test_posix_rejects_relative_and_double_slash(ipc, text):
    assert ipc.check_path(text, windows=False) is None


# =================================================================================================
# prepare_send (mục 2.3, D2)
# =================================================================================================


def P(*texts: str) -> tuple[Path, ...]:
    return tuple(Path(t) for t in texts)


def test_prepare_send_joins_relative_paths_with_cwd_windows(ipc):
    out = ipc.prepare_send(
        ["a.txt", "sub/b.txt", "sub\\c.txt", "D:\\x\\d.txt", "\\\\srv\\sh\\e.txt"],
        cwd="C:\\Users\\An",
        windows=True,
    )
    assert out == P(
        "C:\\Users\\An\\a.txt",
        "C:\\Users\\An\\sub\\b.txt",
        "C:\\Users\\An\\sub\\c.txt",
        "D:\\x\\d.txt",
        "\\\\srv\\sh\\e.txt",
    )


def test_prepare_send_joins_relative_paths_with_cwd_posix(ipc):
    out = ipc.prepare_send(["a.txt", "sub/b.txt", "/etc/c"], cwd="/home/an", windows=False)
    assert out == P("/home/an/a.txt", "/home/an/sub/b.txt", "/etc/c")


def test_prepare_send_returns_paths(ipc):
    out = ipc.prepare_send([host_path("x", "a.txt")], cwd=host_path("x"))
    assert isinstance(out, tuple) and all(isinstance(p, Path) for p in out)


@pytest.mark.parametrize("bad", ["\\abc.txt", "C:abc.txt", "C:..\\x"])
def test_prepare_send_still_rejects_rooted_without_drive_and_drive_relative(ipc, bad):
    """D2: chỉ đường dẫn không ổ, không gốc mới được nối với `cwd`."""
    with pytest.raises(ipc.SendArgsError) as e:
        ipc.prepare_send([bad], cwd="C:\\Users\\An", windows=True)
    assert e.value.kind is ipc.SendArgsErrorKind.BAD_PATH
    assert e.value.path == bad


@BOTH
def test_prepare_send_rejects_relative_with_dot_dot(ipc, windows):
    cwd = "C:\\Users\\An" if windows else "/home/an"
    with pytest.raises(ipc.SendArgsError) as e:
        ipc.prepare_send(["../x.txt"], cwd=cwd, windows=windows)
    assert e.value.kind is ipc.SendArgsErrorKind.BAD_PATH
    assert e.value.path is not None and e.value.path.endswith("x.txt")


@BOTH
def test_prepare_send_empty_is_bad_path(ipc, windows):
    with pytest.raises(ipc.SendArgsError) as e:
        ipc.prepare_send([], cwd="C:\\x" if windows else "/x", windows=windows)
    assert e.value.kind is ipc.SendArgsErrorKind.BAD_PATH


def test_prepare_send_reports_first_bad_path(ipc):
    with pytest.raises(ipc.SendArgsError) as e:
        ipc.prepare_send(
            ["C:\\ok.txt", "C:bad1.txt", "C:\\a\\CON", "\\bad3"], cwd="C:\\Users\\An", windows=True
        )
    assert e.value.kind is ipc.SendArgsErrorKind.BAD_PATH
    assert e.value.path == "C:bad1.txt"


def test_prepare_send_reports_first_bad_path_posix(ipc):
    with pytest.raises(ipc.SendArgsError) as e:
        ipc.prepare_send(["/ok", "/a/../b", "/c/./d"], cwd="/home", windows=False)
    assert e.value.path == "/a/../b"


def test_prepare_send_drops_duplicates_silently_keeping_first_windows(ipc):
    out = ipc.prepare_send(
        ["C:\\A\\x.txt", "C:\\b.txt", "c:/a/X.TXT", "C:\\b.txt", "x.txt"],
        cwd="C:\\a",
        windows=True,
    )
    assert out == P("C:\\A\\x.txt", "C:\\b.txt")


def test_prepare_send_duplicates_are_case_sensitive_on_posix(ipc):
    out = ipc.prepare_send(["/a/X", "/a/x", "/a//x", "x"], cwd="/a", windows=False)
    assert out == P("/a/X", "/a/x")


@BOTH
def test_prepare_send_count_limit_after_dedupe(ipc, windows):
    n = ipc.SEND_MAX_FILES
    cwd = "C:\\d" if windows else "/d"
    names = [f"f{i:05d}.txt" for i in range(n)]
    assert len(ipc.prepare_send(names, cwd=cwd, windows=windows)) == n
    assert len(ipc.prepare_send([*names, names[0]], cwd=cwd, windows=windows)) == n, "trùng bỏ trước khi đếm"
    with pytest.raises(ipc.SendArgsError) as e:
        ipc.prepare_send([*names, "thua.txt"], cwd=cwd, windows=windows)
    assert e.value.kind is ipc.SendArgsErrorKind.TOO_MANY


def test_send_max_files_is_read_at_use(ipc, monkeypatch):
    monkeypatch.setattr(ipc, "SEND_MAX_FILES", 2)
    with pytest.raises(ipc.SendArgsError) as e:
        ipc.prepare_send(["a", "b", "c"], cwd="/d", windows=False)
    assert e.value.kind is ipc.SendArgsErrorKind.TOO_MANY
    reader = ipc.IpcReader(windows=False)
    dropped("count")(decide(reader, send_bytes("/a", "/b", "/c")))


# =================================================================================================
# encode_send ↔ IpcReader
# =================================================================================================


def test_encode_send_bytes_follow_format(ipc):
    a, b = Path(host_path("x", "a.txt")), Path(host_path("Tài liệu", "ảnh.jpg"))
    assert ipc.encode_send([a, b]) == send_bytes(str(a), str(b))


def test_encode_then_read_round_trip_keeps_order(ipc):
    paths = [Path(host_path("x", f"{name}.txt")) for name in ("b", "a", "c", "đ")]
    result = read_all(ipc.encode_send(paths), windows=HOST_WINDOWS)
    assert result == ipc.SendCommand(tuple(str(p) for p in paths))
    assert all(isinstance(p, str) for p in result.paths), "D5: `SendCommand.paths` là chuỗi"


def test_encode_send_exactly_max_bytes_round_trips_and_one_more_is_too_large(ipc):
    paths = [Path(long_path(4000, i, windows=HOST_WINDOWS)) for i in range(32)]
    last = Path(long_path(3033, 99, windows=HOST_WINDOWS))
    data = ipc.encode_send([*paths, last])
    assert len(data) == ipc.MAX_BYTES
    assert isinstance(read_all(data, windows=HOST_WINDOWS), ipc.SendCommand)
    with pytest.raises(ipc.SendArgsError) as e:
        ipc.encode_send([*paths, Path(long_path(3034, 99, windows=HOST_WINDOWS))])
    assert e.value.kind is ipc.SendArgsErrorKind.TOO_LARGE


def test_show_command_round_trip(ipc):
    assert read_all(ipc.SHOW_COMMAND, windows=True) == ipc.ShowCommand()
    assert read_all(ipc.SHOW_COMMAND, windows=False, step=1) == ipc.ShowCommand()


@BOTH
def test_reader_normalizes_paths(ipc, windows):
    if windows:
        data, want = (
            send_bytes("C:/Users/An/a.txt", "//srv/sh/b.txt"),
            ("C:\\Users\\An\\a.txt", "\\\\srv\\sh\\b.txt"),
        )
    else:
        data, want = send_bytes("/home//an/a.txt"), ("/home/an/a.txt",)
    assert read_all(data, windows=windows) == ipc.SendCommand(want)


@BOTH
def test_reader_splits_only_on_lf_bytes_not_unicode_line_breaks(ipc, windows):
    """D3f: U+2028, U+2029 không phải dấu xuống dòng của lệnh (cấm `splitlines()`)."""
    base = "C:\\Users\\An\\" if windows else "/home/an/"
    names = [base + "a\u2028b.txt", base + "c\u2029d.txt"]
    assert read_all(send_bytes(*names), windows=windows) == ipc.SendCommand(tuple(names))


# =================================================================================================
# IpcReader: mọi lý do bỏ lệnh
# =================================================================================================


@pytest.mark.parametrize(
    "data",
    [
        b"hello\n",
        b"SHOW\n",
        b"show\r\n",
        b"SEND\nC:\\a.txt\n\n",
        b"send \nC:\\a.txt\n\n",
        b"send\r\nC:\\a.txt\n\n",
        b"\xef\xbb\xbfsend\nC:\\a.txt\n\n",
        b" send\nC:\\a.txt\n\n",
        b"\n",
        b"show\nsend\nC:\\a.txt\n\n",
        b"show\nx",
    ],
    ids=[
        "hello",
        "upper-show",
        "crlf-show",
        "upper-send",
        "space",
        "crlf-send",
        "bom",
        "lead",
        "lf",
        "show-send",
        "show-x",
    ],
)
def test_reader_drops_wrong_first_line(data):
    m = _ipc()
    for windows in (True, False):
        result = read_all(data, windows=windows)
        assert isinstance(result, m.Dropped), (data, result)
    dropped("header", "trailing")(read_all(data, windows=True))


@pytest.mark.parametrize(
    "data", [b"x" * 17, b"send" + b" " * 13, b"show" * 5], ids=["junk", "send-pad", "showx5"]
)
def test_reader_drops_header_over_16_bytes_without_newline(data):
    m = _ipc()
    reader = m.IpcReader(windows=True)
    first = reader.feed(data[:16])  # bỏ sớm khi đã chắc không phải `show`/`send` cũng được
    assert first is None or isinstance(first, m.Dropped)
    dropped("header", "size")(reader.feed(data[16:]) if first is None else first)
    assert reader.decided


def test_reader_drops_over_max_bytes(ipc):
    lines = [long_path(4000, i, windows=True) for i in range(33)]
    data = send_bytes(*lines)
    assert len(data) > ipc.MAX_BYTES
    dropped("size")(read_all(data, windows=True))


def test_reader_drops_one_byte_over_max_bytes_even_if_well_formed(ipc):
    lines = [long_path(4000, i, windows=False) for i in range(32)]
    ok = send_bytes(*lines, long_path(3033, 99, windows=False))
    over = send_bytes(*lines, long_path(3034, 99, windows=False))
    assert len(ok) == ipc.MAX_BYTES and len(over) == ipc.MAX_BYTES + 1
    assert isinstance(read_all(ok, windows=False), ipc.SendCommand)
    dropped("size")(read_all(over, windows=False))
    dropped("size")(read_all(over, windows=False, step=4096))


@pytest.mark.parametrize(
    "bad",
    [b"\xff", b"\xc0\xaf", b"\xed\xa0\x80", b"\xe1\xba", b"\x80", b"\xf8\x88\x80\x80\x80"],
    ids=["ff", "overlong", "surrogate", "truncated", "continuation", "5-byte"],
)
def test_reader_drops_invalid_utf8(bad):
    dropped("utf8")(read_all(b"send\nC:\\a" + bad + b".txt\n\n", windows=True))
    dropped("utf8")(read_all(b"send\n/a" + bad + b".txt\n\n", windows=False))


@pytest.mark.parametrize(
    "line",
    [
        "relative.txt",
        "C:\\a\\..\\b.txt",
        "C:\\a\x00b.txt",
        "C:\\a\tb.txt",
        "C:\\a.txt\r",
        "C:\\a\x1bb.txt",
        "C:\\a\x85b.txt",
        "\\\\?\\C:\\x.txt",
        "\\\\.\\PhysicalDrive0",
        "//./pipe/x",
        "\\??\\C:\\x.txt",
        "C:\\a.txt:ads",
        "C:\\",
        "C:\\dir\\",
        "C:\\CON",
        "C:abc",
        "\\abc",
        "\ufeffC:\\a.txt",
    ],
)
def test_reader_drops_bad_path_line_windows(line):
    dropped("path")(read_all(send_bytes("C:\\ok.txt", line), windows=True))


def test_reader_drops_path_over_path_max_bytes(ipc):
    dropped("path", "size")(read_all(send_bytes(long_path(4097, windows=True)), windows=True))
    assert isinstance(read_all(send_bytes(long_path(4096, windows=True)), windows=True), ipc.SendCommand)


@pytest.mark.parametrize("line", ["rel.txt", "/a/../b", "//srv/x", "/", "/a/\x00b"])
def test_reader_drops_bad_path_line_posix(line):
    dropped("path")(read_all(send_bytes("/ok.txt", line), windows=False))


def test_reader_count_limit(ipc):
    n = ipc.SEND_MAX_FILES
    lines = [f"C:\\d\\f{i:05d}.txt" for i in range(n + 1)]
    ok = read_all(send_bytes(*lines[:n]), windows=True)
    assert isinstance(ok, ipc.SendCommand) and len(ok.paths) == n
    dropped("count")(read_all(send_bytes(*lines), windows=True))


@pytest.mark.parametrize(
    ("windows", "a", "b"),
    [
        (True, "C:\\A\\x.txt", "c:\\a\\X.TXT"),
        (True, "C:\\a\\x.txt", "C:/a/x.txt"),
        (True, "\\\\srv\\sh\\f", "//SRV/SH/F"),
        (False, "/a/x", "/a/x"),
        (False, "/a/x", "/a//x"),
    ],
)
def test_reader_drops_duplicate_paths(windows, a, b):
    dropped("dup")(read_all(send_bytes(a, "C:\\z.txt" if windows else "/z", b), windows=windows))


def test_reader_case_differs_is_not_duplicate_on_posix(ipc):
    assert read_all(send_bytes("/a/X", "/a/x"), windows=False) == ipc.SendCommand(("/a/X", "/a/x"))


@pytest.mark.parametrize(
    "tail", [b"x", b"\n", b"show\n", b"C:\\b.txt\n\n"], ids=["byte", "lf", "show", "second-command"]
)
def test_reader_drops_bytes_after_terminator_in_same_chunk(tail):
    dropped("trailing")(read_all(send_bytes("C:\\a.txt") + tail, windows=True))


def test_reader_drops_blank_line_between_paths():
    dropped("trailing")(read_all(b"send\nC:\\a.txt\n\nC:\\b.txt\n\n", windows=True))


@pytest.mark.parametrize(
    "data", [b"send\n\n", b"send\n\n\n", b"send\n\nC:\\a.txt\n\n"], ids=["empty", "3lf", "lead"]
)
def test_reader_drops_send_without_paths(data):
    m = _ipc()
    for step in (None, 1):
        result = read_all(data, windows=True, step=step)
        assert isinstance(result, m.Dropped), (data, step, result)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"s",
        b"sho",
        b"show",
        b"send",
        b"send\n",
        b"send\nC:\\a.txt",
        b"send\nC:\\a.txt\n",
        b"send\n" + b"a" * 16384,
    ],
    ids=["nothing", "s", "sho", "show", "send", "send-lf", "no-lf", "no-blank", "16k"],
)
def test_reader_close_before_complete_is_eof(data):
    m = _ipc()
    reader = m.IpcReader(windows=True)
    first = reader.feed(data) if data else None
    if first is not None:  # 16 KiB một dòng: được phép bỏ sớm (dòng quá PATH_MAX_BYTES)
        dropped("path", "size")(first)
        return
    assert not reader.decided
    assert reader.close() == m.Dropped(m.DropReason.EOF)
    assert reader.decided


# =================================================================================================
# IpcReader: một quyết định duy nhất, đưa từng byte
# =================================================================================================


def test_reader_byte_by_byte_send(ipc):
    data = send_bytes("C:\\Users\\An\\ảnh.jpg", "C:\\b.txt")
    reader = ipc.IpcReader(windows=True)
    results = [reader.feed(data[i : i + 1]) for i in range(len(data))]
    assert all(r is None for r in results[:-1]), "chưa đủ lệnh thì chưa quyết"
    assert results[-1] == ipc.SendCommand(("C:\\Users\\An\\ảnh.jpg", "C:\\b.txt"))
    assert reader.decided


def test_reader_decision_is_final_and_later_bytes_are_ignored(ipc):
    reader = ipc.IpcReader(windows=True)
    first = reader.feed(send_bytes("C:\\a.txt"))
    assert first == ipc.SendCommand(("C:\\a.txt",))
    assert reader.decided
    assert reader.feed(b"x" * 200_000) == first, "sau khi quyết, byte thừa không đổi kết quả"
    assert reader.feed(b"") == first
    assert reader.close() == first
    assert reader.close() == first


def test_reader_drop_is_final(ipc):
    reader = ipc.IpcReader(windows=True)
    first = reader.feed(b"hello\n")
    assert isinstance(first, ipc.Dropped)
    assert reader.feed(b"show\n") == first
    assert reader.feed(send_bytes("C:\\a.txt")) == first
    assert reader.close() == first


def test_reader_show_decided_at_first_newline(ipc):
    reader = ipc.IpcReader(windows=False)
    for ch in b"show":
        assert reader.feed(bytes([ch])) is None
        assert not reader.decided
    assert reader.feed(b"\n") == ipc.ShowCommand()
    assert reader.feed(b"junk") == ipc.ShowCommand()
    assert reader.close() == ipc.ShowCommand()


def test_reader_does_not_keep_bytes_after_decision(ipc):
    """Sau khi quyết, `feed` không giữ thêm byte (RAM không tăng mãi khi bên kia gửi tiếp)."""
    import tracemalloc

    reader = ipc.IpcReader(windows=True)
    reader.feed(b"hello\n")
    chunk = b"y" * 1_000_000
    tracemalloc.start()
    try:
        for _ in range(20):
            reader.feed(chunk)
        current, _peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert current < 2_000_000, f"bộ đọc đã quyết vẫn giữ {current} byte"


def _feed_bytewise_seconds(m, data: bytes, *, windows: bool) -> tuple[float, object]:
    """Thời gian ngắn nhất trong 3 lần đưa `data` từng byte một (giảm nhiễu của máy chạy test)."""
    import time

    best, result = float("inf"), None
    for _ in range(3):
        reader = m.IpcReader(windows=windows)
        t0 = time.perf_counter()
        for i in range(len(data)):
            result = reader.feed(data[i : i + 1])
        best = min(best, time.perf_counter() - t0)
    return best, result


@pytest.mark.slow
@pytest.mark.parametrize("shape", ["long-lines", "many-lines"])
def test_reader_is_linear_when_fed_one_byte_at_a_time(ipc, shape):
    """D20: `IpcReader` giữ offset quét, không tìm lại từ đầu bộ đệm mỗi `feed`: ~128 KiB đưa từng byte
    chạy dưới ~0,5 s (bản quét lại từ đầu mất cỡ giây)."""
    if shape == "long-lines":
        lines = [long_path(4000, i, windows=False) for i in range(32)] + [long_path(3033, 99, windows=False)]
    else:
        lines = [f"/d/{i:05d}/" + "a" * 115 for i in range(ipc.SEND_MAX_FILES)]
    data = send_bytes(*lines)
    assert len(data) > 120_000
    seconds, result = _feed_bytewise_seconds(ipc, data, windows=False)
    assert isinstance(result, ipc.SendCommand)
    assert seconds < 0.5, f"đưa {len(data)} byte từng byte mất {seconds:.2f} s"


# =================================================================================================
# Hypothesis: khứ hồi và dữ liệu bất kỳ
# =================================================================================================

_COMP = st.text(alphabet="abcxyzABCĐđăễảệ0123456789-_ \u2028", min_size=0, max_size=10).map(
    lambda s: "f" + s + "g"
)


def _join(comps: list[str], windows: bool) -> str:
    return ("C:\\" + "\\".join(comps)) if windows else ("/" + "/".join(comps))


def _key(path: str, windows: bool) -> str:
    return ntpath.normcase(path) if windows else posixpath.normcase(path)


@st.composite
def path_lists(draw, windows: bool):
    paths = draw(
        st.lists(
            st.lists(_COMP, min_size=1, max_size=4).map(lambda c: _join(c, windows)), min_size=1, max_size=8
        )
    )
    seen: set[str] = set()
    out = []
    for p in paths:
        if _key(p, windows) not in seen:
            seen.add(_key(p, windows))
            out.append(p)
    return out


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(st.data(), st.booleans())
def test_well_formed_command_is_accepted_whatever_the_chunking(data, windows):
    m = _ipc()
    paths = data.draw(path_lists(windows))
    raw = send_bytes(*paths)
    cuts = sorted(data.draw(st.lists(st.integers(0, len(raw)), max_size=6)))
    reader = m.IpcReader(windows=windows)
    result = None
    prev = 0
    for cut in [*cuts, len(raw)]:
        r = reader.feed(raw[prev:cut])
        prev = cut
        if r is not None:
            result = r
            assert cut == len(raw), "không được quyết trước khi có dòng rỗng kết thúc"
    assert result == m.SendCommand(tuple(paths))
    assert reader.close() == result


@settings(max_examples=100)
@given(st.data())
def test_encode_send_round_trips_host_paths(data):
    m = _ipc()
    paths = [Path(p) for p in data.draw(path_lists(HOST_WINDOWS))]
    result = read_all(m.encode_send(paths), windows=HOST_WINDOWS)
    assert result == m.SendCommand(tuple(str(p) for p in paths))


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.sampled_from([b"", b"send\n", b"show\n", b"send\nC:\\a.txt\n", b"send\n/a\n"]),
    st.binary(max_size=200),
    st.booleans(),
    st.integers(1, 7),
)
def test_reader_never_raises_and_always_decides_on_close(prefix, body, windows, step):
    m = _ipc()
    reader = m.IpcReader(windows=windows)
    data = prefix + body
    result = None
    for i in range(0, len(data), step):
        r = reader.feed(data[i : i + step])
        assert r is None or isinstance(r, m.ShowCommand | m.SendCommand | m.Dropped)
        if result is not None:
            assert r == result, "quyết định không đổi"
        if r is not None:
            result = r
    final = reader.close()
    assert isinstance(final, m.ShowCommand | m.SendCommand | m.Dropped)
    assert reader.decided
    if result is not None:
        assert final == result
    if isinstance(final, m.SendCommand):
        assert 1 <= len(final.paths) <= m.SEND_MAX_FILES
        for p in final.paths:
            assert m.check_path(p, windows=windows) == p
        assert len({_key(p, windows) for p in final.paths}) == len(final.paths)
    if isinstance(final, m.ShowCommand):
        assert data.startswith(b"show\n")


# =================================================================================================
# D14: khóa so trùng của ipc và send.selection_error
# =================================================================================================


def _selection_dup(paths: list[str]) -> bool:
    from teleport.ui import send

    return send.selection_error([Path(p) for p in paths]) is not None


if HOST_WINDOWS:
    D14_PAIRS = [
        ("C:\\a\\x.txt", "c:/A/X.TXT"),
        ("C:\\a\\x.txt", "C:\\a\\y.txt"),
        ("\\\\srv\\sh\\f.txt", "//SRV/sh/F.TXT"),
        ("C:\\Đ\\ảnh.jpg", "c:\\đ\\ảnh.jpg"),
        ("C:\\a\\x.txt", "D:\\a\\x.txt"),
    ]
else:
    D14_PAIRS = [
        ("/a/x", "/a//x"),
        ("/a/X", "/a/x"),
        ("/a/x", "/a/y"),
        ("///a/x", "/a/x"),
    ]


@pytest.mark.parametrize(("a", "b"), D14_PAIRS)
def test_ipc_and_selection_error_agree_on_duplicates(ipc, a, b):
    sel_dup = _selection_dup([a, b])
    prepared = ipc.prepare_send([a, b], cwd=host_path("cwd"))
    assert (len(prepared) == 1) is sel_dup, (a, b)
    result = read_all(send_bytes(a, b), windows=HOST_WINDOWS)
    assert (result == ipc.Dropped(ipc.DropReason.DUP)) is sel_dup, (a, b, result)


_CASEY = st.text(alphabet="aAbBđĐéÉ", min_size=1, max_size=3)


@settings(max_examples=100)
@given(st.lists(_CASEY, min_size=1, max_size=3), st.lists(_CASEY, min_size=1, max_size=3), st.booleans())
def test_ipc_and_selection_error_agree_on_generated_pairs(c1, c2, alt_sep):
    m = _ipc()
    a = _join(c1, HOST_WINDOWS)
    b = _join(c2, HOST_WINDOWS)
    if alt_sep:
        b = b.replace("\\", "/") if HOST_WINDOWS else b[0] + b[1:].replace("/", "//")
    sel_dup = _selection_dup([a, b])
    prepared = m.prepare_send([a, b], cwd=host_path("cwd"))
    assert (len(prepared) == 1) is sel_dup, (a, b)
