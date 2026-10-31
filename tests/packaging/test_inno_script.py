"""Test tĩnh cho script bộ cài Inno Setup `packaging/inno/teleport.iss` (PR E2, spec việc #6).

Chạy trên mọi hệ điều hành, không cần ISCC: chỉ đọc file `.iss` (và vài file nó trỏ tới) rồi kiểm các
quyết định của spec `spec-e2-bo-cai.md` (D1–D13, mục 0, mục 4). Phần `[Code]` (Pascal Script) được bỏ
chú thích (giữ nguyên chuỗi), chia theo thủ tục/hàm, thay hằng `const` bằng giá trị rồi kiểm theo đồ
thị gọi: rule tường lửa chỉ tạo trong nhánh `IsAdminInstallMode`, `name=all` chỉ chạy lúc gỡ, xóa dữ
liệu chỉ trỏ `{userappdata}\\Teleport`… Hành vi thật (cài, gỡ, rule) ở `test_installer.py`.

Chỗ nào spec để `dong-goi` tự chọn (GUID của AppId, tên file setup, cách viết hàm) thì test đọc từ
`.iss` hoặc chỉ kiểm tính chất, không ghim chuỗi.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INNO = REPO / "packaging" / "inno"
ISS = INNO / "teleport.iss"
VIETNAMESE_ISL = INNO / "languages" / "Vietnamese.isl"
INNO_LICENSE = INNO / "licenses" / "InnoSetup-6.7.3-license.txt"
WORKFLOW = REPO / ".github" / "workflows" / "build-gui.yml"

if not ISS.is_file():
    pytest.skip(f"chưa có {ISS.relative_to(REPO)}", allow_module_level=True)

RULE_NAMES = ("Teleport (TCP-In)", "Teleport (UDP-In)")
APP_EXE = r"{app}\teleport.exe"
INSTALL_EVENTS = (
    "initializesetup",
    "initializewizard",
    "preparetoinstall",
    "curstepchanged",
    "curpagechanged",
    "nextbuttonclick",
    "shouldskippage",
    "deinitializesetup",
    "updatereadymemo",
    "registerpreviousdata",
)
UNINSTALL_EVENTS = (
    "initializeuninstall",
    "initializeuninstallprogressform",
    "curuninstallstepchanged",
    "deinitializeuninstall",
    "uninstallneedrestart",
)


# ---- đọc .iss -------------------------------------------------------------------------------


def strip_pascal_comments(code: str) -> str:
    """Bỏ chú thích `//`, `{ }`, `(* *)` của Pascal Script, giữ nguyên chuỗi '...'."""
    out: list[str] = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if c == "'":
            j = i + 1
            while j < n:
                if code[j] == "'":
                    if j + 1 < n and code[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(code[i : j + 1])
            i = j + 1
        elif code.startswith("//", i):
            j = code.find("\n", i)
            i = n if j < 0 else j
        elif c == "{":
            j = code.find("}", i)
            out.append(" ")
            i = n if j < 0 else j + 1
        elif code.startswith("(*", i):
            j = code.find("*)", i)
            out.append(" ")
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


ENTRY_PARAM = re.compile(r'(\w+)\s*:\s*("(?:[^"]|"")*"|[^;]*)')


@dataclass
class Iss:
    raw: str
    setup: dict[str, str] = field(default_factory=dict)
    sections: dict[str, list[str]] = field(default_factory=dict)
    code: str = ""  # [Code] đã bỏ chú thích, đã thay hằng
    routines: dict[str, str] = field(default_factory=dict)  # tên thường → thân (đã bỏ chú thích)

    def entries(self, section: str) -> list[dict[str, str]]:
        result = []
        for line in self.sections.get(section.lower(), []):
            params = {
                k.lower(): v.strip().strip('"').replace('""', '"') for k, v in ENTRY_PARAM.findall(line)
            }
            result.append(params)
        return result

    def lines(self, section: str) -> list[str]:
        return self.sections.get(section.lower(), [])

    def calls(self, name: str) -> set[str]:
        body = re.sub(r"'(?:[^']|'')*'", "''", self.routines[name]).lower()  # tên trong chuỗi không tính
        return {other for other in self.routines if other != name and re.search(rf"\b{other}\b", body)}

    def reachable(self, roots: tuple[str, ...]) -> set[str]:
        seen: set[str] = set()
        stack = [r for r in roots if r in self.routines]
        while stack:
            r = stack.pop()
            if r in seen:
                continue
            seen.add(r)
            stack.extend(self.calls(r) - seen)
        return seen

    def callers(self, name: str) -> set[str]:
        return {r for r in self.routines if r != name and name in self.calls(r)}


def _split_routines(code: str) -> dict[str, str]:
    heads = list(re.finditer(r"(?im)^\s*(?:procedure|function)\s+(\w+)", code))
    result: dict[str, str] = {}
    for k, m in enumerate(heads):
        end = heads[k + 1].start() if k + 1 < len(heads) else len(code)
        name = m.group(1).lower()
        result[name] = result.get(name, "") + code[m.start() : end]
    return result


def _substitute_consts(code: str) -> str:
    """Thay tên hằng chuỗi (`Ten = '...';` trong khối const) bằng giá trị để kiểm được nội dung lệnh."""
    consts = dict(re.findall(r"(?im)^\s*(\w+)\s*=\s*('(?:[^']|'')*')\s*;", code))
    for _ in range(3):  # hằng ghép từ hằng khác
        for name, value in consts.items():
            code = re.sub(rf"(?<!['\w]){name}(?![\w'])", lambda _m, v=value: v, code)
    return code


_TRIVIAL_FUNCTION = re.compile(
    r"(?is)function\s+(\w+)\s*:\s*string\s*;\s*begin\s*result\s*:=\s*(.*?);\s*end\s*;"
)


def _inline_trivial_functions(code: str) -> str:
    """Thay lời gọi hàm không tham số chỉ có `Result := <biểu thức>;` bằng `(<biểu thức>)`.

    Nhờ vậy `'… delete rule … ' + ProgramArg` được kiểm như thể `program="{app}\\Teleport.exe"` viết
    ngay trong lệnh.
    """
    for m in list(_TRIVIAL_FUNCTION.finditer(code)):
        name, expr = m.group(1), m.group(2)
        end = code.find(m.group(0)) + len(m.group(0))
        tail = re.sub(rf"(?<![\w.]){name}(?![\w(])", lambda _m, e=expr: f"({e})", code[end:], flags=re.I)
        code = code[:end] + tail
    return code


def load_iss() -> Iss:
    raw = ISS.read_text(encoding="utf-8-sig")
    iss = Iss(raw=raw)
    current = ""
    code_lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"\[[A-Za-z]+\]", stripped):
            current = stripped[1:-1].lower()
            iss.sections.setdefault(current, [])
            continue
        if current == "code":
            code_lines.append(line)
            continue
        if not stripped or stripped.startswith(";") or stripped.startswith("#"):
            continue
        iss.sections.setdefault(current, []).append(stripped)
        if current == "setup" and "=" in stripped:
            key, value = stripped.split("=", 1)
            iss.setup[key.strip().lower()] = value.strip()
    code = _inline_trivial_functions(_substitute_consts(strip_pascal_comments("\n".join(code_lines))))
    iss.code = code
    iss.routines = _split_routines(code)
    return iss


@pytest.fixture(scope="module")
def iss() -> Iss:
    return load_iss()


def norm(text: str) -> str:
    """Chữ thường, bỏ khoảng trắng thừa, bỏ dấu nối chuỗi Pascal để so lệnh netsh ghép nhiều mảnh."""
    text = text.lower().replace("'#39'", "'")
    text = re.sub(r"'\s*\+\s*'", "", text)
    return re.sub(r"\s+", " ", text)


def setup_value(iss: Iss, key: str) -> str:
    assert key.lower() in iss.setup, f"[Setup] thiếu {key}"
    return iss.setup[key.lower()]


def text_without_comments(iss: Iss) -> str:
    """Toàn bộ script trừ chú thích (`;` ngoài [Code], chú thích Pascal trong [Code])."""
    parts = [line for lines in iss.sections.values() for line in lines]
    return "\n".join([*parts, iss.code])


# ---- [Setup] (D1, D3, D4, mục 0, mục 4 điểm 7, 13) -------------------------------------------


def test_default_install_is_per_machine_with_commandline_override_only(iss: Iss) -> None:
    assert setup_value(iss, "PrivilegesRequired").lower() == "admin"
    assert setup_value(iss, "PrivilegesRequiredOverridesAllowed").lower() == "commandline"


def test_default_dir_is_autopf_teleport(iss: Iss) -> None:
    assert setup_value(iss, "DefaultDirName").lower() == r"{autopf}\teleport"


def test_requires_windows_10(iss: Iss) -> None:
    value = setup_value(iss, "MinVersion")
    assert re.fullmatch(r"10(\.0(\.\d+)?)?", value), value


def test_x64_only(iss: Iss) -> None:
    assert setup_value(iss, "ArchitecturesAllowed").lower() == "x64compatible"
    assert setup_value(iss, "ArchitecturesInstallIn64BitMode").lower() == "x64compatible"


def test_app_id_is_fixed_guid_with_never_change_comment(iss: Iss) -> None:
    value = setup_value(iss, "AppId").strip('"')
    assert re.fullmatch(r"\{\{[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}\}", value), value
    lines = iss.raw.splitlines()
    idx = next(i for i, line in enumerate(lines) if re.match(r"\s*AppId\s*=", line, re.I))
    near = "\n".join(lines[max(0, idx - 4) : idx + 1]).lower()
    assert "không bao giờ đổi" in near, "AppId phải có comment “không bao giờ đổi” (D3)"


def test_compression_is_lzma2_max_solid(iss: Iss) -> None:
    assert setup_value(iss, "Compression").lower() == "lzma2/max"
    assert setup_value(iss, "SolidCompression").lower() == "yes"


def test_version_and_source_come_from_command_line_defines(iss: Iss) -> None:
    assert "{#appversion}" in setup_value(iss, "AppVersion").lower()
    raw = iss.raw
    assert "AppVersionNumeric" in raw and "SourceDir" in raw
    numeric_users = [k for k, v in iss.setup.items() if "{#appversionnumeric}" in v.lower()]
    assert "versioninfoversion" in numeric_users, "VersionInfoVersion phải dùng AppVersionNumeric (mục 4.12)"


def test_close_applications_force_and_no_restart(iss: Iss) -> None:
    assert setup_value(iss, "CloseApplications").lower() == "force"
    assert setup_value(iss, "RestartApplications").lower() == "no"


def test_license_page_shows_repo_license(iss: Iss) -> None:
    value = setup_value(iss, "LicenseFile").strip('"')
    assert Path(value.replace("\\", "/")).name == "LICENSE"
    candidate = (ISS.parent / value.replace("\\", "/")).resolve()
    if "{#" not in value:
        assert candidate == (REPO / "LICENSE").resolve(), candidate


def test_used_user_areas_warning_disabled_with_comment(iss: Iss) -> None:
    assert setup_value(iss, "UsedUserAreasWarning").lower() == "no"
    lines = iss.raw.splitlines()
    idx = next(i for i, line in enumerate(lines) if re.match(r"\s*UsedUserAreasWarning\s*=", line, re.I))
    assert any(line.strip().startswith(";") for line in lines[max(0, idx - 4) : idx]) or ";" in lines[idx], (
        "UsedUserAreasWarning=no phải kèm comment giải thích (mục 4.7)"
    )


def test_never_schedules_reboot_deletes(iss: Iss) -> None:
    text = text_without_comments(iss).lower()
    for flag in ("restartreplace", "uninsrestartdelete"):
        assert flag not in text, f"không hẹn xóa/thay file sau khi khởi động lại ({flag})"


# ---- [Files], [InstallDelete], [UninstallDelete] (D4, D11) ---------------------------------


def test_files_install_build_but_exclude_doc_truoc(iss: Iss) -> None:
    files = iss.entries("Files")
    build = [e for e in files if "{#sourcedir}" in e.get("source", "").lower()]
    assert build, "[Files] phải lấy bản build từ {#SourceDir}"
    for e in build:
        assert "doc-truoc.txt" in e.get("excludes", "").lower(), e
    assert any("recursesubdirs" in e.get("flags", "").lower() for e in build)
    assert any(e.get("destdir", "").lower() == "{app}" for e in build)


def test_inno_license_is_installed_into_app_licenses(iss: Iss) -> None:
    assert INNO_LICENSE.is_file()
    entries = [
        e for e in iss.entries("Files") if "innosetup-6.7.3-license.txt" in e.get("source", "").lower()
    ]
    assert entries, "[Files] phải cài giấy phép Inno Setup (D11)"
    assert all(e.get("destdir", "").lower().rstrip("\\") == r"{app}\licenses" for e in entries), entries


def test_install_delete_clears_internal(iss: Iss) -> None:
    entries = iss.entries("InstallDelete")
    assert any(
        e.get("type", "").lower() == "filesandordirs"
        and e.get("name", "").lower().rstrip("\\") == r"{app}\_internal"
        for e in entries
    ), entries


def test_install_delete_internal_only_when_existing_teleport_install(iss: Iss) -> None:
    """`/DIR` trỏ nhầm vào thư mục có `_internal` của chương trình khác: không được xóa nó.

    `Check:` của mục `{app}\\_internal` phải dựa vào `FileExists` của `{app}\\Teleport.exe` (hàm tự viết
    hay gọi thẳng, không ghim tên hàm).
    """
    entries = [
        e
        for e in iss.entries("InstallDelete")
        if e.get("name", "").lower().rstrip("\\") == r"{app}\_internal"
    ]
    assert entries
    for e in entries:
        check = e.get("check", "").strip()
        assert check, "[InstallDelete] {app}\\_internal phải có Check: chỉ xóa khi là bản cài Teleport sẵn có"
        fn = check.split("(")[0].strip().lower()
        assert not fn.startswith("not "), f"Check bị phủ định: {check}"
        scope = norm(check)
        if fn in iss.routines:
            scope += " " + norm(" ".join(iss.routines[r] for r in iss.reachable((fn,))))
        assert "fileexists" in scope, f"Check phải kiểm file tồn tại: {check}"
        assert APP_EXE in scope or ("{app}" in scope and "'teleport.exe'" in scope), (
            f"Check phải kiểm {{app}}\\Teleport.exe: {check}"
        )


def _call_args(text: str, start: int) -> str:
    """Đối số trong ngoặc ngay sau vị trí `start` (rỗng nếu lời gọi không có ngoặc)."""
    i = start
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    if i >= len(text) or text[i] != "(":
        return ""
    depth = 0
    for j in range(i, len(text)):
        depth += {"(": 1, ")": -1}.get(text[j], 0)
        if depth == 0:
            return text[i : j + 1]
    return text[i:]


def _except_blocks(body: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"(?is)\bexcept\b(.*?)\bend\s*;", body)]


def _result_values(text: str) -> set[str]:
    return {v.strip() for v in re.findall(r"(?i)\bresult\s*:=\s*([^;]+);", text)}


def test_user_data_not_deleted_while_any_teleport_runs(iss: Iss) -> None:
    """Trước DelTree `{userappdata}\\Teleport`: kiểm còn Teleport.exe nào chạy (không chỉ của `{app}`).

    Một bản cài khác hay bản zip đang chạy với cùng thư mục cấu hình thì không xóa. Lời kiểm (qua WMI)
    phải đứng trước DelTree, không truyền `{app}` làm bộ lọc, và WMI lỗi thì coi như đang chạy.
    """
    wmi = _wmi_routines(iss)
    assert wmi
    deleters = [r for r, body in iss.routines.items() if r"{userappdata}\teleport" in norm(body)]
    deleters = [r for r in deleters if re.search(r"\bdeltree\s*\(", iss.routines[r], re.I)]
    assert deleters, "gỡ phải có bước hỏi xóa {userappdata}\\Teleport"
    for deleter in deleters:
        guards: list[tuple[str, str]] = []  # (hàm kiểm, đối số)
        for owner in (deleter, *iss.callers(deleter)):
            body = re.sub(r"'(?:[^']|'')*'", "''", iss.routines[owner]).lower()
            target = re.search(r"\bdeltree\s*\(" if owner == deleter else rf"\b{deleter}\b", body)
            assert target is not None
            for g in iss.calls(owner):
                if not (iss.reachable((g,)) & wmi):
                    continue
                raw = norm(iss.routines[owner])
                m = re.search(rf"\b{g}\b", body)
                if m is not None and m.start() < target.start():
                    call = re.search(rf"\b{g}\b", raw)
                    guards.append((g, _call_args(raw, call.end()) if call else ""))
        assert guards, f"{deleter}: phải kiểm Teleport.exe đang chạy (WMI) trước khi xóa dữ liệu"
        for g, args in guards:
            assert "{app}" not in args, f"{deleter}: kiểm tiến trình không được lọc theo {{app}}: {g}{args}"
        # WMI lỗi thì không xóa: hoặc khối except trả kết quả khác mặc định (“có thể đang chạy”), hoặc hàm
        # có cờ `var X: Boolean` chỉ thành True khi kiểm xong (trong try) và nơi gọi thoát khi cờ False.
        before_delete = norm(_deleter_scope_before_deltree(iss, deleter))
        for g, args in guards:
            for name in iss.reachable((g,)) & wmi:
                assert _fails_closed(iss.routines[name], args, before_delete), (
                    f"{name}: WMI lỗi thì phải không xóa (except trả “có thể đang chạy”, hoặc cờ kiểm "
                    f"được = False và {deleter} thoát khi cờ False)"
                )


def _deleter_scope_before_deltree(iss: Iss, deleter: str) -> str:
    body = iss.routines[deleter]
    m = re.search(r"(?i)\bdeltree\s*\(", body)
    return body[: m.start()] if m else body


def _fails_closed(body: str, args: str, caller_before_delete: str) -> bool:
    """WMI lỗi thì không xóa: except trả giá trị khác mặc định, hoặc cờ `var X: Boolean` kiểu “Ok”."""
    m_try = re.search(r"(?i)\btry\b", body)
    m_exc = re.search(r"(?i)\bexcept\b", body)
    excepts = _except_blocks(body)
    if m_try is None or m_exc is None or not excepts:
        return False
    before_try = body[: m_try.start()]
    failed = set().union(*(_result_values(x) for x in excepts))
    if failed and not failed <= _result_values(before_try):
        return True
    header = re.search(r"\(([^)]*)\)", body)
    params = re.findall(r"(?i)\bvar\s+(\w+)\s*:\s*boolean", header.group(1) if header else "")
    in_try = body[m_try.end() : m_exc.start()]
    passed = re.findall(r"\w+", args.lower())
    for k, flag in enumerate(params):
        set_false = re.search(rf"(?i)\b{flag}\s*:=\s*false\b", before_try)
        set_true = re.search(rf"(?i)\b{flag}\s*:=\s*true\b", in_try)
        true_in_except = any(re.search(rf"(?i)\b{flag}\s*:=\s*true\b", x) for x in excepts)
        if not (set_false and set_true) or true_in_except or k >= len(passed):
            continue
        var = passed[k]
        if re.search(rf"\bnot\s+{var}\b[^;]*?\bthen\b|\b{var}\s*=\s*false\b", caller_before_delete):
            return True
    return False


def test_uninstall_delete_only_touches_app_dir(iss: Iss) -> None:
    entries = iss.entries("UninstallDelete")
    assert any(
        e.get("type", "").lower() == "dirifempty" and e.get("name", "").lower().rstrip("\\") == "{app}"
        for e in entries
    ), entries
    for e in entries:
        assert e.get("name", "").lower().startswith("{app}"), (
            f"[UninstallDelete] chỉ được trỏ vào {{app}}: {e}"
        )


# ---- [Icons], [Tasks], [Run], [Registry] (D5, D7, mục 4 điểm 10) ----------------------------


def test_start_menu_shortcut_always_and_desktop_is_unchecked_task(iss: Iss) -> None:
    icons = iss.entries("Icons")
    menu = [e for e in icons if e.get("name", "").lower().startswith(("{autoprograms}", "{group}"))]
    assert menu and all("tasks" not in e for e in menu), "lối tắt Start Menu luôn có, không phụ thuộc task"
    assert any(e.get("filename", "").lower() == APP_EXE for e in menu)
    desktop = [e for e in icons if "desktop}" in e.get("name", "").lower()]
    assert desktop, "phải có lối tắt Desktop tùy chọn"
    assert all("tasks" in e for e in desktop), "lối tắt Desktop phải đi theo task"
    task_names = {e["tasks"].lower() for e in desktop}
    tasks = {e.get("name", "").lower(): e for e in iss.entries("Tasks")}
    for name in task_names:
        assert name in tasks, f"thiếu task {name}"
        assert "unchecked" in tasks[name].get("flags", "").lower(), "task Desktop mặc định không chọn (D5)"


def test_no_autostart_task_and_no_run_value_written(iss: Iss) -> None:
    for task in iss.entries("Tasks"):
        blob = (task.get("name", "") + " " + task.get("description", "")).lower()
        for word in ("autostart", "startup", "khởi động", "khoidong", "login", "đăng nhập"):
            assert word not in blob, f"không có task tự khởi động (D7): {task}"
    for entry in iss.entries("Registry"):
        assert "currentversion\\run" not in entry.get("subkey", "").lower(), entry
    code = iss.code.lower()
    for setter in ("regwritestringvalue", "regwriteexpandstringvalue"):
        for m in re.finditer(setter + r"\s*\(([^;]*)", code):
            assert "currentversion\\run" not in m.group(1), "bộ cài không được ghi Run (D7)"


def test_run_launches_app_postinstall_as_original_user(iss: Iss) -> None:
    runs = [e for e in iss.entries("Run") if e.get("filename", "").lower() == APP_EXE]
    assert runs, "[Run] phải có mục mở Teleport.exe sau khi cài"
    for e in runs:
        flags = set(e.get("flags", "").lower().split())
        assert {"postinstall", "nowait", "skipifsilent", "runasoriginaluser"} <= flags, flags


def test_runascurrentuser_never_used(iss: Iss) -> None:
    assert "runascurrentuser" not in text_without_comments(iss).lower()


# ---- tường lửa (D6, mục 4 điểm 2, 3) ---------------------------------------------------------


def firewall_run_entries(iss: Iss, section: str) -> list[dict[str, str]]:
    return [e for e in iss.entries(section) if "advfirewall" in e.get("parameters", "").lower()]


def routines_with(iss: Iss, needle: str) -> set[str]:
    return {name for name, body in iss.routines.items() if needle in norm(body)}


def test_firewall_rules_exist_for_both_protocols(iss: Iss) -> None:
    text = norm(text_without_comments(iss))
    for name in RULE_NAMES:
        assert name.lower() in text, f"thiếu rule {name}"
    adds = [
        norm(e["parameters"]) for e in firewall_run_entries(iss, "Run") if "add rule" in norm(e["parameters"])
    ]
    adds += [norm(iss.routines[r]) for r in routines_with(iss, "add rule")]
    assert adds, "không thấy lệnh netsh add rule"
    joined = " ".join(adds)
    assert "protocol=tcp" in joined or ("protocol=" in joined and "'tcp'" in text), "thiếu rule TCP"
    assert "protocol=udp" in joined or ("protocol=" in joined and "'udp'" in text), "thiếu rule UDP"


def test_firewall_add_rule_is_private_inbound_allow_localsubnet_for_app_exe(iss: Iss) -> None:
    adds = [
        norm(e["parameters"]) for e in firewall_run_entries(iss, "Run") if "add rule" in norm(e["parameters"])
    ]
    adds += [norm(iss.routines[r]) for r in routines_with(iss, "add rule")]
    assert adds
    for text in adds:
        for token in (
            "dir=in",
            "action=allow",
            "profile=private",
            "remoteip=localsubnet",
            "edge=no",
            "enable=yes",
            "protocol=",
            "program=",
        ):
            assert token in text, f"lệnh add rule thiếu {token}"
        assert APP_EXE in text, "rule phải theo chương trình {app}\\Teleport.exe"
        for bad in ("profile=public", "profile=any", "profile=domain", "localport=", "dir=out"):
            assert bad not in text, f"lệnh add rule không được có {bad}"


def test_firewall_rules_only_created_in_admin_install_mode(iss: Iss) -> None:
    for e in firewall_run_entries(iss, "Run"):
        assert "isadmininstallmode" in e.get("check", "").lower(), e
    for name in routines_with(iss, "add rule"):
        if "isadmininstallmode" in iss.routines[name].lower():
            continue
        callers = iss.callers(name)
        assert callers, f"{name} tạo rule nhưng không kiểm IsAdminInstallMode"
        for caller in callers:
            assert "isadmininstallmode" in iss.routines[caller].lower(), (
                f"{caller} gọi {name} (tạo rule) ngoài nhánh IsAdminInstallMode"
            )


def test_firewall_add_failure_does_not_abort_install(iss: Iss) -> None:
    for name in routines_with(iss, "add rule"):
        body = iss.routines[name].lower()
        assert "raiseexception" not in body and not re.search(r"\babort\b", body), name


def _delete_statements(text: str) -> list[str]:
    """Từng lệnh `delete rule` (tới hết câu lệnh Pascal hoặc hết dòng tham số)."""
    out = []
    for m in re.finditer(r"delete rule", text):
        end = text.find(";", m.end())
        out.append(text[m.start() : len(text) if end < 0 else end])
    return out


def test_every_netsh_delete_is_scoped_to_app_exe(iss: Iss) -> None:
    sources = [norm(e["parameters"]) for s in ("Run", "UninstallRun") for e in firewall_run_entries(iss, s)]
    sources.append(norm(iss.code))
    found = 0
    for text in sources:
        for stmt in _delete_statements(text):
            found += 1
            assert "program=" in stmt, f"lệnh netsh delete thiếu program= (mục 4.3): {stmt[:200]}"
    assert found, "không thấy lệnh netsh delete rule nào"


def _uninstall_text(iss: Iss) -> str:
    parts = [norm(e["parameters"]) for e in firewall_run_entries(iss, "UninstallRun")]
    parts += [norm(iss.routines[r]) for r in iss.reachable(UNINSTALL_EVENTS)]
    return " ".join(parts)


def _install_routines(iss: Iss) -> set[str]:
    roots = INSTALL_EVENTS + tuple(
        e.get("check", "").split("(")[0].strip().lower()
        for s in ("Files", "Run", "Icons", "Registry", "InstallDelete")
        for e in iss.entries(s)
        if e.get("check")
    )
    return iss.reachable(roots) - iss.reachable(UNINSTALL_EVENTS)


def test_uninstall_deletes_both_rules_and_generated_inbound_rules(iss: Iss) -> None:
    text = _uninstall_text(iss)
    stmts = _delete_statements(text)
    assert stmts, "gỡ (admin) phải xóa rule"
    blob = " ".join(stmts) + " " + text
    for name in RULE_NAMES:
        assert f"name={name.lower()}" in blob.replace('"', "") or f'"{name.lower()}"' in blob, name
    assert any("name=all" in s and "dir=in" in s for s in stmts), (
        "gỡ (admin) phải xóa thêm name=all dir=in program=… (mục 4.3)"
    )
    assert "isadmininstallmode" in text or firewall_run_entries(iss, "UninstallRun"), (
        "xóa rule lúc gỡ chỉ trong chế độ admin"
    )


def test_install_never_deletes_name_all_rules(iss: Iss) -> None:
    for e in firewall_run_entries(iss, "Run"):
        assert "name=all" not in norm(e["parameters"]), e
    for name in _install_routines(iss):
        assert "name=all" not in norm(iss.routines[name]), f"{name} (lúc cài) xóa name=all"


def test_install_deletes_same_name_rule_before_adding(iss: Iss) -> None:
    run = firewall_run_entries(iss, "Run")
    if run:
        params = [norm(e["parameters"]) for e in run]
        first_add = next(i for i, p in enumerate(params) if "add rule" in p)
        assert any("delete rule" in p for p in params[:first_add]), "phải xóa rule cùng tên trước khi tạo"
        return
    for name in routines_with(iss, "add rule"):
        body = norm(iss.routines[name])
        callers_text = " ".join(norm(iss.routines[c]) for c in iss.callers(name))
        deletes_first = "delete rule" in body and body.index("delete rule") < body.index("add rule")
        assert (
            deletes_first
            or "delete rule" in callers_text
            or any("delete rule" in norm(iss.routines[c]) for c in iss.calls(name))
        ), f"{name}: phải xóa rule cùng tên trước khi tạo (D6)"


# ---- Run\Teleport và StartupApproved (D7, mục 4 điểm 1, 5) -----------------------------------


def test_uninstall_removes_run_value_teleport_only_when_it_points_to_app(iss: Iss) -> None:
    text = norm(" ".join(iss.routines[r] for r in iss.reachable(UNINSTALL_EVENTS)))
    assert r"software\microsoft\windows\currentversion\run" in text
    assert "regdeletevalue" in text
    assert re.search(r"regdeletevalue\s*\([^;]*'teleport'\s*\)", text), "tên giá trị Run phải là Teleport"
    assert "regquerystringvalue" in text, "phải đọc dữ liệu Run trước khi xóa"
    assert APP_EXE in text, "so dữ liệu với {app}\\Teleport.exe"
    assert "lowercase" in text or "uppercase" in text or "comparetext" in text, (
        "so không phân biệt hoa thường"
    )
    assert "'\"'" in text or "#34" in text, "phải bỏ ngoặc kép trước khi so"
    assert "hkcu" in text or "hkey_current_user" in text


def test_startup_approved_deleted_only_after_run_value_deleted(iss: Iss) -> None:
    reachable = iss.reachable(UNINSTALL_EVENTS)
    owners = [r for r in reachable if r"explorer\startupapproved\run" in norm(iss.routines[r])]
    assert owners, "gỡ phải xử lý StartupApproved\\Run\\Teleport (mục 4.1)"
    for name in owners:
        text = norm(iss.routines[name])
        approved = text.find(r"explorer\startupapproved\run")
        guard = re.search(r"if\s+regdeletevalue\s*\([^;]*?currentversion\\run'[^;]*?\)\s*then", text)
        assert guard is not None and guard.end() < approved, (
            f"{name}: chỉ xóa StartupApproved khi vừa xóa được Run\\Teleport"
        )
    assert "notifyiconsettings" not in norm(iss.code)


# ---- dữ liệu người dùng khi gỡ (D9, mục 4 điểm 1, 14) ---------------------------------------


def test_uninstall_data_removal_only_targets_userappdata_teleport(iss: Iss) -> None:
    deleters = [
        r
        for r, body in iss.routines.items()
        if re.search(r"\b(deltree|deletefile|removedir)\s*\(", body, re.I)
    ]
    assert deleters, "gỡ phải có bước hỏi xóa {userappdata}\\Teleport"
    for name in deleters:
        body = norm(iss.routines[name])
        constants = set(re.findall(r"\{[a-z]+\}", body))
        assert constants <= {"{userappdata}", "{app}"}, f"{name} trỏ ra ngoài {{userappdata}}: {constants}"
    assert any(r"{userappdata}\teleport" in norm(iss.routines[n]) for n in deleters)
    text = norm(text_without_comments(iss))
    for bad in (
        "downloads",
        "{userdocs}",
        "{commondocs}",
        "teleport_home",
        r"\.teleport",
        "{localappdata}\\teleport",
    ):
        assert bad not in text, f"gỡ không được đụng {bad}"


def test_data_removal_question_defaults_to_no_and_skipped_when_silent(iss: Iss) -> None:
    deleters = [r for r, body in iss.routines.items() if re.search(r"\bdeltree\s*\(", body, re.I)]
    assert deleters
    for name in deleters:
        scope = norm(" ".join([iss.routines[name], *(iss.routines[c] for c in iss.callers(name))]))
        assert "mb_yesno" in scope and "mb_defbutton2" in scope, f"{name}: hỏi Có/Không, mặc định Không"
        if "suppressiblemsgbox" in scope:
            assert "idno" in scope
        assert "uninstallsilent" in scope, f"{name}: chạy silent thì luôn giữ dữ liệu, không hỏi"
        assert iss.reachable(UNINSTALL_EVENTS) & ({name} | iss.callers(name)), f"{name} không chạy lúc gỡ"


def test_uninstall_question_warns_about_repairing(iss: Iss) -> None:
    assert "ghép đôi lại" in iss.raw


# ---- app đang chạy (mục 0, mục 4 điểm 4) -----------------------------------------------------


def _wmi_routines(iss: Iss) -> set[str]:
    return routines_with(iss, "win32_process")


def test_running_app_detection_uses_wmi_executable_path_prefix(iss: Iss) -> None:
    wmi = _wmi_routines(iss)
    assert wmi, "phải tìm tiến trình qua WMI Win32_Process"
    body = norm(" ".join(iss.routines[r] for r in wmi))
    for token in ("executablepath", "sessionid", "teleport.exe", "lowercase", "addbackslash"):
        assert token in body, f"thiếu {token}"
    assert re.search(r"\btry\b", body) and re.search(r"\bexcept\b", body), (
        "WMI lỗi thì không chặn (try/except)"
    )


def test_running_app_checked_before_install_and_uninstall(iss: Iss) -> None:
    wmi = _wmi_routines(iss)
    install_side = iss.reachable(("preparetoinstall",))
    assert "preparetoinstall" in iss.routines and install_side & wmi, "cài: chặn ở PrepareToInstall"
    uninstall_side = iss.reachable(("initializeuninstall",))
    assert "initializeuninstall" in iss.routines and uninstall_side & wmi, "gỡ: kiểm ở InitializeUninstall"


def test_running_app_prompt_is_retry_cancel_and_silent_does_not_ask(iss: Iss) -> None:
    for root, silent in (("preparetoinstall", "wizardsilent"), ("initializeuninstall", "uninstallsilent")):
        text = norm(" ".join(iss.routines[r] for r in iss.reachable((root,))))
        assert "mb_retrycancel" in text and "idcancel" in text, root
        assert "suppressiblemsgbox" in text, root
        assert silent in text, f"{root}: chạy silent thì hủy, không hỏi"


def test_running_app_messages(iss: Iss) -> None:
    assert "Teleport đang chạy" in iss.raw
    assert "Thử lại" in iss.raw
    assert "tài khoản khác" in iss.raw


# ---- ngôn ngữ, thông điệp (D2, D10, mục 4 điểm 6) --------------------------------------------


def test_vietnamese_language_file_pinned_with_source_header(iss: Iss) -> None:
    langs = iss.entries("Languages")
    vi = [e for e in langs if e.get("name", "").lower() == "vi"]
    assert vi and "vietnamese.isl" in vi[0].get("messagesfile", "").lower()
    head = VIETNAMESE_ISL.read_text(encoding="utf-8-sig")[:2000]
    assert "is-6_7_3" in head and re.search(r"\b[0-9a-f]{40}\b", head), "header phải ghi nguồn tag + commit"


def test_custom_messages_are_prefixed_vi_and_en_block_is_todo(iss: Iss) -> None:
    keys = [line.split("=", 1)[0].strip() for line in iss.lines("CustomMessages") if "=" in line]
    assert keys
    for key in keys:
        assert key.startswith("vi."), f"[CustomMessages] phải có tiền tố vi.: {key}"
    assert "TODO E1" in iss.raw


def test_currentuser_finish_page_mentions_private_network_firewall_prompt(iss: Iss) -> None:
    assert "Windows sẽ hỏi quyền tường lửa khi Teleport mở lần đầu" in iss.raw
    assert "Riêng tư" in iss.raw


# ---- repo và workflow (D13) ----------------------------------------------------------------


def test_installer_output_dir_is_gitignored() -> None:
    lines = [line.strip() for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert "installer/" in lines or "/installer/" in lines


def test_workflow_builds_and_tests_installer() -> None:
    if not WORKFLOW.is_file():
        pytest.skip("chưa có build-gui.yml")
    text = WORKFLOW.read_text(encoding="utf-8")
    if "iscc" not in text.lower():
        pytest.skip("workflow chưa có bước bộ cài")
    assert "6.7.3" in text
    assert re.search(r"\b[0-9a-fA-F]{64}\b", text), "phải ghim SHA-256 của bộ cài Inno Setup"
    assert re.search(r"80[_ ]?000[_ ]?000", text), "chặn bộ cài > 80 000 000 B"
    assert "TELEPORT_INSTALLER_EXE" in text
    assert "tests/packaging/test_installer.py" in text and "-m frozen" in text
    assert "contents: read" in text and "workflow_dispatch" in text
    assert "retention-days: 14" in text
