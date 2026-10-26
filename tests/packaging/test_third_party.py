"""Test đơn vị cho cổng kiểm giấy phép của bản build PyInstaller (`packaging/pyinstaller/third_party.py`).

Cổng này phải đóng khi không chắc (ke_hoach mục 1, Giấy phép; docstring của module): file không rõ
thuộc gói nào, gói ngoài `APPROVED`, giấy phép khác chuỗi đã duyệt, thành phần lồng bên trong có giấy
phép ngoài tập cho phép đều làm build dừng với `LicenseAuditError`.

Module không phải gói nên nạp bằng `importlib.util.spec_from_file_location`. Test không cần PyInstaller
và không build gì: cây thư mục giả dựng trong `tmp_path` và được truyền vào tham số `base_prefix`
(`_python_dirs`, `check`) thay cho `sys.base_prefix`, nên kết quả không phụ thuộc chỗ cài trình thông
dịch thật. Bản phân phối giả (dist-info có METADATA, RECORD, SBOM) đặt trong `tmp_path` rồi thêm vào
`sys.path` để `importlib.metadata` tìm thấy.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import shutil
import struct
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from hypothesis import given
from hypothesis import strategies as st

_MODULE_PATH = Path(__file__).resolve().parents[2] / "packaging" / "pyinstaller" / "third_party.py"


def _load_third_party() -> ModuleType:
    name = "teleport_packaging_third_party_under_test"
    spec = importlib.util.spec_from_file_location(name, _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclass cần tìm thấy module của nó trong sys.modules
    spec.loader.exec_module(module)
    return module


tp = _load_third_party()
LicenseAuditError = tp.LicenseAuditError


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(path))


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _accepted(expression: str) -> bool:
    """spdx_compatible, coi `LicenseAuditError` (biểu thức hỏng) là không chấp nhận."""
    try:
        return bool(tp.spdx_compatible(expression))
    except LicenseAuditError:
        return False


# ---------------------------------------------------------------- fixture


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """Thư mục cài Python giả (bố cục Windows), truyền vào tham số `base_prefix` thay cho prefix thật."""
    root = tmp_path / "Python3X"
    (root / "Lib").mkdir(parents=True)
    (root / "DLLs").mkdir()
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "packaging").mkdir()
    return root


DistMaker = Callable[..., Path]


@pytest.fixture
def make_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DistMaker:
    """Tạo bản phân phối giả mà `importlib.metadata` tìm được; trả thư mục gốc chứa dist-info."""
    root = tmp_path / "fake-site"
    root.mkdir()
    monkeypatch.syspath_prepend(str(root))

    def make(
        name: str,
        license_expression: str | None = "MIT",
        *,
        version: str = "1.0",
        sbom: dict | None = None,
        files: tuple[str, ...] = (),
    ) -> Path:
        dist_info = root / f"{name.replace('-', '_')}-{version}.dist-info"
        dist_info.mkdir()
        meta = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
        if license_expression is not None:
            meta.append(f"License-Expression: {license_expression}")
        (dist_info / "METADATA").write_text("\n".join(meta) + "\n", encoding="utf-8")
        record = [f"{dist_info.name}/METADATA,,", f"{dist_info.name}/RECORD,,"]
        if sbom is not None:
            (dist_info / "sboms").mkdir()
            (dist_info / "sboms" / "bom.cdx.json").write_text(json.dumps(sbom), encoding="utf-8")
            record.append(f"{dist_info.name}/sboms/bom.cdx.json,,")
        for rel in files:
            _touch(root / rel)
            record.append(f"{rel},,")
        (dist_info / "RECORD").write_text("\n".join(record) + "\n", encoding="utf-8")
        return root

    return make


def _classify(path: Path, base: Path) -> bool:
    """`_is_python_file` với thư mục Python của bản cài giả `base`."""
    return tp._is_python_file(_norm(path), tp._python_dirs(str(base)))


# ------------------------------------------------- 1. phân loại file của Python


@pytest.mark.parametrize(
    "rel",
    [
        "Lib/json/__init__.py",
        "Lib/struct.py",
        "DLLs/x.pyd",
        "DLLs/_ssl.pyd",
        "DLLs/libssl-3.dll",
    ],
)
def test_stdlib_and_dlls_files_count_as_python(base: Path, rel: str) -> None:
    assert _classify(_touch(base / rel), base) is True


@pytest.mark.parametrize(
    "rel",
    [
        "Lib/site-packages/evil/__init__.py",
        "Lib/site-packages/evil.pyd",
        "Lib/dist-packages/evil.py",
        "Lib/Site-Packages/evil/__init__.py",
        "Lib/DIST-PACKAGES/evil.py",
        "lib/python3.12/site-packages/evil.py",
        "site-packages/evil.dll",
    ],
)
def test_site_packages_file_without_record_is_not_python(base: Path, rel: str) -> None:
    assert _classify(_touch(base / rel), base) is False


@pytest.mark.parametrize(
    "name",
    ["python3.dll", "python312.dll", "python314.dll", "vcruntime140.dll", "vcruntime140_1.dll"],
)
def test_known_runtime_dll_at_base_prefix_root_counts_as_python(base: Path, name: str) -> None:
    assert _classify(_touch(base / name), base) is True


@pytest.mark.skipif(os.path.normcase("A") == "A", reason="hệ tệp phân biệt hoa thường: normcase giữ nguyên")
@pytest.mark.parametrize("name", ["PYTHON314.DLL", "Python3.dll", "VCRUNTIME140.DLL"])
def test_known_runtime_dll_at_root_matches_after_normcase(base: Path, name: str) -> None:
    assert _classify(base / name, base) is True


@pytest.mark.parametrize(
    "name",
    [
        "evil.dll",
        "evil.pyd",
        "evil.py",
        "xpython312.dll",
        "evil-vcruntime140.dll",
        "python312.dll.evil",
        "python314.exe",
    ],
)
def test_unknown_file_at_base_prefix_root_is_not_python(base: Path, name: str) -> None:
    assert _classify(_touch(base / name), base) is False


@pytest.mark.parametrize(
    "rel",
    ["Lib2/evil.py", "LibEvil/evil.py", "DLLs-evil/evil.pyd", "Scripts/evil.exe", "Tools/evil.py"],
)
def test_sibling_dir_of_lib_or_dlls_is_not_python(base: Path, rel: str) -> None:
    assert _classify(_touch(base / rel), base) is False


def test_file_outside_base_prefix_is_not_python(base: Path, tmp_path: Path) -> None:
    assert _classify(_touch(tmp_path / "elsewhere" / "python314.dll"), base) is False


def test_classification_follows_injected_base_prefix_not_real_one(base: Path) -> None:
    real_json = Path(inspect.getfile(json))  # json/__init__.py của trình thông dịch thật
    assert _classify(_touch(base / "Lib" / "json" / "__init__.py"), base) is True
    assert _classify(real_json, base) is False


# ----------------------------------------------- 2. check() dừng khi gặp file lạ


def _expect_unknown(
    repo: Path,
    base: Path,
    toc: list[tuple[str, str, str]],
    bad_dest: str,
    good_srcs: tuple[str, ...] = (),
) -> None:
    with pytest.raises(LicenseAuditError) as excinfo:
        tp.check(toc, repo_dir=repo, app_version="0.0.0-test", base_prefix=str(base))
    message = str(excinfo.value)
    assert bad_dest in message
    for good in good_srcs:
        assert good not in message


def test_check_rejects_unknown_dll_at_base_prefix_root(base: Path, repo: Path) -> None:
    good = _touch(base / "Lib" / "json" / "__init__.py")
    evil = _touch(base / "evil_root.dll")
    toc = [("json/__init__.py", str(good), "DATA"), ("evil_root.dll", str(evil), "BINARY")]
    _expect_unknown(repo, base, toc, "evil_root.dll", (str(good),))


def test_check_rejects_parent_dir_path_resolving_to_base_root(base: Path, repo: Path) -> None:
    _touch(base / "evil_dotdot.dll")
    src = os.path.join(str(base), "Lib", "..", "evil_dotdot.dll")
    _expect_unknown(repo, base, [("evil_dotdot.dll", src, "BINARY")], "evil_dotdot.dll")


@pytest.mark.parametrize(
    "rel", ["Lib/site-packages/evilpkg/core.pyd", "Lib/Site-Packages/evilpkg/__init__.py"]
)
def test_check_rejects_site_packages_file_without_record(base: Path, repo: Path, rel: str) -> None:
    good = _touch(base / "DLLs" / "_socket.pyd")
    evil = _touch(base / rel)
    toc = [("_socket.pyd", str(good), "EXTENSION"), ("evilpkg/thing", str(evil), "BINARY")]
    _expect_unknown(repo, base, toc, "evilpkg/thing", (str(good),))


def test_check_rejects_file_outside_every_known_root(base: Path, repo: Path, tmp_path: Path) -> None:
    evil = _touch(tmp_path / "downloads" / "evil_outside.dll")
    _expect_unknown(repo, base, [("evil_outside.dll", str(evil), "BINARY")], "evil_outside.dll")


def test_check_rejects_unmappable_localpyc(
    base: Path, repo: Path, tmp_path: Path, make_dist: DistMaker
) -> None:
    make_dist("pyinstaller", "GPL-2.0-or-later", version="6.0")  # chỉ cần metadata, không cần PyInstaller
    pyc = _touch(tmp_path / "work" / "localpycs" / "khong_ton_tai_xyz.pyc")
    _expect_unknown(repo, base, [("khong_ton_tai_xyz", str(pyc), "PYMODULE")], "khong_ton_tai_xyz")


# ------------------------------- 2b. drop_ucrt: Universal CRT là của Windows, không đóng vào


_UCRT_SRC = r"C:\hostedtoolcache\windows\Java_Temurin-Hotspot_jdk\17.0.20-101\x64\bin"


@pytest.mark.parametrize(
    "dest",
    [
        "api-ms-win-crt-runtime-l1-1-0.dll",
        "API-MS-WIN-core-file-l1-1-0.dll",
        "api-ms-win-core-kernel32-legacy-l1-1-1.dll",
        "ucrtbase.dll",
        "UCRTBASE.DLL",
        "sub/dir/api-ms-win-crt-stdio-l1-1-0.dll",
        "sub\\ucrtbase.dll",
    ],
)
def test_drop_ucrt_removes_universal_crt(dest: str) -> None:
    entry = (dest, f"{_UCRT_SRC}\\{dest}", "BINARY")
    kept, dropped = tp.drop_ucrt([entry])
    assert kept == []
    assert dropped == [dest]


@pytest.mark.parametrize(
    "dest",
    [
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "python312.dll",
        "python3.dll",
        "_ssl.pyd",
        "libcrypto-3.dll",
        "libssl-3.dll",
        "ucrtbase.dll.txt",
        "ucrtbase.pyd",
        "xucrtbase.dll",
        "ucrtbased.dll",
        "api-ms-win-crt-runtime-l1-1-0.pyd",
        "api-ms-win-security-base-l1-1-0.dll",
        "api-ms-win-eventing-provider-l1-1-0.dll",
        "my-api-ms-win-crt-runtime-l1-1-0.dll",
        "ucrtbase.dll/evil.dll",
    ],
)
def test_drop_ucrt_keeps_other_files(dest: str) -> None:
    entry = (dest, f"C:\\Python312\\{dest}", "BINARY")
    kept, dropped = tp.drop_ucrt([entry])
    assert kept == [entry]
    assert dropped == []


def test_drop_ucrt_keeps_order_of_remaining_entries() -> None:
    toc = [
        ("python312.dll", r"C:\Python312\python312.dll", "BINARY"),
        ("ucrtbase.dll", rf"{_UCRT_SRC}\ucrtbase.dll", "BINARY"),
        ("_ssl.pyd", r"C:\Python312\DLLs\_ssl.pyd", "EXTENSION"),
        ("api-ms-win-crt-heap-l1-1-0.dll", rf"{_UCRT_SRC}\api-ms-win-crt-heap-l1-1-0.dll", "BINARY"),
        ("vcruntime140.dll", r"C:\Python312\vcruntime140.dll", "BINARY"),
    ]
    kept, dropped = tp.drop_ucrt(toc)
    assert kept == [toc[0], toc[2], toc[4]]
    assert dropped == ["ucrtbase.dll", "api-ms-win-crt-heap-l1-1-0.dll"]


@pytest.mark.parametrize("name", ["ucrtbase.dll", "api-ms-win-crt-runtime-l1-1-0.dll"])
def test_check_still_rejects_ucrt_that_was_not_dropped(
    base: Path, repo: Path, tmp_path: Path, name: str
) -> None:
    """Bộ lọc nằm ở spec, không ở cổng kiểm: UCRT lọt vào `check` vẫn là file lạ, build dừng."""
    good = _touch(base / "DLLs" / "_socket.pyd")
    ucrt = _touch(tmp_path / "jdk" / "bin" / name)
    toc = [("_socket.pyd", str(good), "EXTENSION"), (name, str(ucrt), "BINARY")]
    _expect_unknown(repo, base, toc, name, (str(good),))


@pytest.mark.parametrize("name", ["ucrtbase.dll", "api-ms-win-crt-runtime-l1-1-0.dll"])
def test_ucrt_at_base_prefix_root_is_not_python(base: Path, name: str) -> None:
    assert _classify(_touch(base / name), base) is False


# ------------------------------------------ 2. audit() dừng khi giấy phép chưa duyệt


FAKE = "teleport-test-fakepkg"


def _component(license_text: str, name: str = FAKE, version: str = "1.0") -> object:
    return tp.Component(tp._normalize(name), name, version, license_text)


def test_audit_accepts_approved_package_with_exact_license(
    make_dist: DistMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_dist(FAKE, "MIT")
    monkeypatch.setitem(tp.APPROVED, FAKE, "MIT")
    assert tp.audit({FAKE: _component("MIT")}, {}) == {}


def test_audit_rejects_package_not_in_approved(make_dist: DistMaker) -> None:
    make_dist(FAKE, "MIT")
    assert FAKE not in tp.APPROVED
    with pytest.raises(LicenseAuditError, match=FAKE):
        tp.audit({FAKE: _component("MIT")}, {})


@pytest.mark.parametrize("declared", ["GPL-2.0-only", "MIT AND SSPL-1.0", "MIT OR GPL-2.0-only", ""])
def test_audit_rejects_license_different_from_approved_string(
    make_dist: DistMaker, monkeypatch: pytest.MonkeyPatch, declared: str
) -> None:
    make_dist(FAKE, "MIT")
    monkeypatch.setitem(tp.APPROVED, FAKE, "MIT")
    with pytest.raises(LicenseAuditError, match=FAKE):
        tp.audit({FAKE: _component(declared)}, {})


def test_audit_keeps_direct_lgpl_zeroconf_approved() -> None:
    # LGPL bị bỏ khỏi tập cho thành phần lồng bên trong, nhưng zeroconf đóng trực tiếp vẫn được duyệt.
    import importlib.metadata as md

    comp = tp.Component("zeroconf", "zeroconf", md.version("zeroconf"), tp.APPROVED["zeroconf"])
    assert tp.APPROVED["zeroconf"] == "LGPL-2.1-or-later"
    tp.audit({"zeroconf": comp}, {})


def _sbom(*components: dict) -> dict:
    return {"bomFormat": "CycloneDX", "specVersion": "1.6", "components": list(components)}


def _sbom_component(name: str, version: str, *licenses: dict) -> dict:
    return {"name": name, "version": version, "licenses": list(licenses)}


def _approved_fake_with_sbom(
    make_dist: DistMaker, monkeypatch: pytest.MonkeyPatch, sbom: dict
) -> dict[str, object]:
    make_dist(FAKE, "MIT", sbom=sbom)
    monkeypatch.setitem(tp.APPROVED, FAKE, "MIT")
    return {FAKE: _component("MIT")}


def test_audit_accepts_sbom_component_with_compatible_license(
    make_dist: DistMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    sbom = _sbom(_sbom_component("crate-a", "1.2.3", {"license": {"id": "MIT"}}))
    sboms = tp.audit(_approved_fake_with_sbom(make_dist, monkeypatch, sbom), {})
    assert [entry[:3] for entry in sboms[FAKE]] == [("crate-a", "1.2.3", "MIT")]


@pytest.mark.parametrize(
    "license_item",
    [
        {"license": {"id": "LGPL-2.1-or-later"}},
        {"expression": "LGPL-2.1-or-later"},
        {"license": {"id": "GPL-2.0-only"}},
        {"expression": "MIT AND GPL-2.0-only"},
        {"license": {"name": "Giấy phép riêng"}},
    ],
)
def test_audit_rejects_sbom_component_with_disallowed_license(
    make_dist: DistMaker, monkeypatch: pytest.MonkeyPatch, license_item: dict
) -> None:
    sbom = _sbom(_sbom_component("crate-bad", "0.1.0", license_item))
    with pytest.raises(LicenseAuditError, match="crate-bad"):
        tp.audit(_approved_fake_with_sbom(make_dist, monkeypatch, sbom), {})


def test_audit_rejects_sbom_component_without_license(
    make_dist: DistMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    sbom = _sbom(_sbom_component("mystery-lib", "2.0.0"))
    with pytest.raises(LicenseAuditError, match="mystery-lib"):
        tp.audit(_approved_fake_with_sbom(make_dist, monkeypatch, sbom), {})


def test_audit_accepts_openssl_3_without_license_in_sbom(
    make_dist: DistMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    sbom = _sbom(_sbom_component("OpenSSL", "3.5.0"))
    sboms = tp.audit(_approved_fake_with_sbom(make_dist, monkeypatch, sbom), {})
    assert sboms[FAKE][0][2] == "Apache-2.0"


def test_audit_rejects_openssl_1_without_license_in_sbom(
    make_dist: DistMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    sbom = _sbom(_sbom_component("OpenSSL", "1.1.1w"))
    with pytest.raises(LicenseAuditError, match="OpenSSL"):
        tp.audit(_approved_fake_with_sbom(make_dist, monkeypatch, sbom), {})


def test_audit_accepts_every_python_embedded_component() -> None:
    python_files = {entry[0]: [f"{entry[0]}x.dll"] for entry in tp.PYTHON_EMBEDDED}
    assert tp.audit({}, python_files) == {}


def test_audit_rejects_python_embedded_component_with_disallowed_license(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evil = ("_evilmod", "Evil lib", "GPL-2.0-only", "không có", ())
    monkeypatch.setattr(tp, "PYTHON_EMBEDDED", (*tp.PYTHON_EMBEDDED, evil))
    assert tp.audit({}, {}) == {}  # chưa đóng vào thì không xét
    with pytest.raises(LicenseAuditError, match="Evil lib"):
        tp.audit({}, {"_evilmod": ["_evilmod.pyd"]})


# --------------------------------------------------------- 3. spdx_compatible


@pytest.mark.parametrize(
    "expression",
    [
        "MIT",
        "Apache-2.0 OR BSD-3-Clause",
        "(MIT OR Apache-2.0) AND Unicode-3.0",
        "Apache-2.0 WITH LLVM-exception",
        "Apache-2.0 OR GPL-2.0-only",
        "GPL-2.0-only OR Apache-2.0",
        "((MIT))",
        "Apache-2.0 OR MIT AND GPL-2.0-only",
        "GPL-2.0-only AND MIT OR Apache-2.0",
        "Apache-2.0 WITH LLVM-exception OR GPL-2.0-only",
    ],
)
def test_spdx_accepts_compatible_expression(expression: str) -> None:
    assert tp.spdx_compatible(expression) is True


@pytest.mark.parametrize(
    "expression",
    [
        "GPL-2.0-only",
        "LGPL-2.1-or-later",
        "LGPL-2.1-only",
        "LGPL-3.0-or-later",
        "GPL-2.0-only OR LGPL-2.1-or-later",
        "MIT AND LGPL-2.1-or-later",
        "Teleport-Unknown-1.0",
        "MIT AND GPL-2.0-only",
        "(MIT OR Apache-2.0) AND GPL-2.0-only",
        "MIT WITH LLVM-exception",
        "mit",
    ],
)
def test_spdx_rejects_incompatible_expression(expression: str) -> None:
    assert _accepted(expression) is False


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "   ",
        "(MIT OR Apache-2.0",
        "MIT OR Apache-2.0)",
        "()",
        "MIT or Apache-2.0",
        "MIT and Apache-2.0",
        "Apache-2.0 with LLVM-exception",
        "MIT OR",
        "OR MIT",
        "AND",
        "MIT Apache-2.0",
        "MIT WITH",
    ],
)
def test_spdx_rejects_malformed_expression(expression: str) -> None:
    # Biểu thức hỏng: chấp nhận cả hai cách đóng cổng (ném LicenseAuditError hoặc trả False).
    assert _accepted(expression) is False


_GOOD_IDS = ("MIT", "Apache-2.0", "BSD-3-Clause", "Apache-2.0 WITH LLVM-exception")
_BAD_IDS = ("GPL-2.0-only", "SSPL-1.0", "LicenseRef-chua-duyet", "LGPL-2.1-or-later")

_Tree = str | tuple[str, list, bool]

_trees = st.recursive(
    st.sampled_from(_GOOD_IDS + _BAD_IDS),
    lambda children: st.tuples(
        st.sampled_from(("AND", "OR")), st.lists(children, min_size=2, max_size=3), st.booleans()
    ),
    max_leaves=12,
)


def _render(tree: _Tree, parent_op: str | None = None) -> str:
    if isinstance(tree, str):
        return tree
    op, children, extra_parens = tree
    text = f" {op} ".join(_render(child, op) for child in children)
    # AND gắn chặt hơn OR: OR nằm trong AND bắt buộc có ngoặc; còn lại thêm ngoặc tùy ý.
    if extra_parens or (parent_op == "AND" and op == "OR"):
        return f"({text})"
    return text


def _expected(tree: _Tree) -> bool:
    if isinstance(tree, str):
        return tree in _GOOD_IDS
    op, children, _ = tree
    values = [_expected(child) for child in children]
    return all(values) if op == "AND" else any(values)


@given(_trees)
def test_spdx_matches_reference_evaluation_with_and_binding_tighter(tree: _Tree) -> None:
    assert tp.spdx_compatible(_render(tree)) is _expected(tree)


# ------------------------------------------------------------ 4. _localpyc_source


def test_localpyc_struct_maps_to_stdlib_source_and_counts_as_python(
    tmp_path: Path, make_dist: DistMaker
) -> None:
    # Dùng thư viện chuẩn của trình thông dịch đang chạy: struct.py thật là thứ PYZ dịch sẵn.
    make_dist("pyinstaller", "GPL-2.0-or-later", version="6.0")
    pyc = _touch(tmp_path / "work" / "localpycs" / "struct.pyc")
    source = tp._localpyc_source("struct", _norm(pyc))
    assert source is not None
    expected = inspect.getsourcefile(struct)
    assert expected is not None
    assert Path(source).samefile(expected)
    assert tp._is_python_file(_norm(source), tp._python_dirs()) is True


def test_localpyc_loader_module_maps_to_pyinstaller_loader(tmp_path: Path, make_dist: DistMaker) -> None:
    root = make_dist(
        "pyinstaller",
        "GPL-2.0-or-later",
        version="6.0",
        files=("PyInstaller/loader/pyimod01_archive.py",),
    )
    pyc = _touch(tmp_path / "work" / "localpycs" / "pyimod01_archive.pyc")
    source = tp._localpyc_source("pyimod01_archive", _norm(pyc))
    assert source is not None
    assert Path(source).samefile(root / "PyInstaller" / "loader" / "pyimod01_archive.py")


@pytest.mark.parametrize(
    ("name", "rel"),
    [
        ("khong_ton_tai_xyz", "work/localpycs/khong_ton_tai_xyz.pyc"),  # không có nguồn
        ("struct", "work/other/struct.pyc"),  # không nằm trong localpycs/
        ("struct", "work/localpycs/struct.py"),  # không phải .pyc
    ],
)
def test_localpyc_source_returns_none_when_unmappable(
    tmp_path: Path, make_dist: DistMaker, name: str, rel: str
) -> None:
    make_dist("pyinstaller", "GPL-2.0-or-later", version="6.0")
    assert tp._localpyc_source(name, _norm(_touch(tmp_path / rel))) is None


# ---------------------------------------------------------- 5. remove_stale_dist


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """`dist/` có bản build cũ `teleport/` và vài thứ bên cạnh không được đụng tới."""
    root = tmp_path / "dist"
    _touch(root / tp.DIST_NAME / "teleport.exe")
    _touch(root / tp.DIST_NAME / "_internal" / "python314.dll")
    _touch(root / "ghi-chu.txt")
    _touch(root / "teleport-old" / "teleport.exe")
    return root


def test_remove_stale_dist_deletes_only_teleport_dir(dist: Path) -> None:
    assert tp.DIST_NAME == "teleport"
    assert tp.remove_stale_dist(dist, "teleport") is True
    assert not (dist / "teleport").exists()
    assert (dist / "ghi-chu.txt").is_file()
    assert (dist / "teleport-old" / "teleport.exe").is_file()


def test_remove_stale_dist_returns_false_when_nothing_to_delete(tmp_path: Path) -> None:
    (tmp_path / "dist").mkdir()
    assert tp.remove_stale_dist(tmp_path / "dist", "teleport") is False


@pytest.mark.parametrize(
    "name", ["../x", "other", "", ".", "..", "./teleport", "teleport/", "teleport/../teleport", "TELEPORT"]
)
def test_remove_stale_dist_refuses_other_names(dist: Path, name: str) -> None:
    with pytest.raises(ValueError):
        tp.remove_stale_dist(dist, name)
    assert (dist / "teleport" / "teleport.exe").is_file()
    assert (dist / "ghi-chu.txt").is_file()


def test_remove_stale_dist_refuses_dotdot_name_pointing_outside(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    victim = _touch(tmp_path / "x" / "quan-trong.txt")
    with pytest.raises(ValueError):
        tp.remove_stale_dist(dist, "../x")
    assert victim.is_file()


def test_remove_stale_dist_refuses_file_target(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    target = _touch(dist / "teleport")
    with pytest.raises(ValueError):
        tp.remove_stale_dist(dist, "teleport")
    assert target.is_file()


def test_remove_stale_dist_refuses_symlink_target(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    victim = _touch(tmp_path / "victim" / "quan-trong.txt")
    try:
        os.symlink(victim.parent, dist / "teleport", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # Windows không có Developer Mode/quyền tạo symlink
        pytest.skip(f"không tạo được symlink: {exc}")
    with pytest.raises(ValueError):
        tp.remove_stale_dist(dist, "teleport")
    assert victim.is_file()
    assert os.path.lexists(dist / "teleport")


@pytest.mark.skipif(sys.platform != "win32", reason="junction chỉ có trên Windows")
def test_remove_stale_dist_refuses_junction_target(tmp_path: Path) -> None:
    import _winapi

    dist = tmp_path / "dist"
    dist.mkdir()
    victim = _touch(tmp_path / "victim" / "quan-trong.txt")
    try:
        _winapi.CreateJunction(str(victim.parent), str(dist / "teleport"))
    except OSError as exc:
        pytest.skip(f"không tạo được junction: {exc}")
    try:
        with pytest.raises(ValueError):
            tp.remove_stale_dist(dist, "teleport")
        assert victim.is_file()
    finally:
        if os.path.lexists(dist / "teleport"):
            os.rmdir(dist / "teleport")  # gỡ junction, không đụng thư mục đích


def test_remove_stale_dist_works_with_relative_distpath(dist: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(dist.parent)
    assert tp.remove_stale_dist(Path("dist"), "teleport") is True
    assert not (dist / "teleport").exists()
    assert (dist / "ghi-chu.txt").is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="junction chỉ có trên Windows")
def test_remove_stale_dist_refuses_junction_to_sibling_inside_dist(dist: Path) -> None:
    # Junction trỏ tới thư mục ngay trong dist/: kiểm "nằm thẳng trong distpath" không bắt được,
    # chỉ còn kiểm link/junction chặn.
    import _winapi

    shutil.rmtree(dist / "teleport")
    sibling = dist / "teleport-old"
    try:
        _winapi.CreateJunction(str(sibling), str(dist / "teleport"))
    except OSError as exc:
        pytest.skip(f"không tạo được junction: {exc}")
    try:
        with pytest.raises(ValueError):
            tp.remove_stale_dist(dist, "teleport")
        assert (sibling / "teleport.exe").is_file()
    finally:
        if os.path.lexists(dist / "teleport"):
            os.rmdir(dist / "teleport")
