"""Kiểm giấy phép và sinh `THIRD-PARTY-NOTICES.txt`, `SOURCE.txt` cho bản build PyInstaller.

`teleport-cli.spec` gọi `check` ngay sau Analysis/PYZ (trước EXE/COLLECT) và `write_outputs` sau
COLLECT, chạy trong chính môi trường Python dùng để build.
Danh sách thành phần lấy từ các file PyInstaller thật sự đóng vào (TOC của `Analysis` và module khởi
động `PYZ.dependencies`), rồi tra
ngược file đó thuộc bản phân phối nào qua `RECORD` của gói đã cài (`importlib.metadata`), nên gồm
cả dependency gián tiếp. Văn bản giấy phép lấy từ dist-info của từng gói. Python lấy `LICENSE.txt`
của trình thông dịch dùng để build, cộng văn bản chép sẵn trong `licenses/` cho phần mềm tích hợp
trong CPython và thư viện C của bản Windows mà `LICENSE.txt` không có (`CPYTHON_LICENSE_TEXTS`,
`PYTHON_EMBEDDED`, theo đúng phiên bản Python). Gói có SBOM CycloneDX trong `.dist-info/sboms/`
(PEP 770, ví dụ `cryptography` liên kết tĩnh OpenSSL và các crate Rust) thì liệt kê và kiểm cả thành
phần trong SBOM.

Luật (ke_hoach mục 1, Giấy phép): mọi thứ đóng vào phải tương thích GPLv3. Gói nằm ngoài
`APPROVED`, gói có giấy phép trong metadata khác chuỗi đã duyệt, file không biết thuộc gói nào,
hay thành phần SBOM có giấy phép ngoài `SPDX_COMPATIBLE` đều làm build dừng với `LicenseAuditError`:
không rõ thì báo, không đoán. Thêm hay nâng dependency mà đổi giấy phép thì phải duyệt lại rồi sửa
`APPROVED` ở đây.
"""

from __future__ import annotations

import contextlib
import fnmatch
import importlib.metadata as md
import json
import os
import platform
import re
import shutil
import site
import ssl
import subprocess
import sys
import sysconfig
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# Tên bản phân phối (đã chuẩn hóa) -> chuỗi giấy phép đúng như metadata của gói (License-Expression,
# không có thì License, không có nữa thì các classifier License). Đã đối chiếu tương thích GPLv3.
APPROVED: dict[str, str] = {
    "zero-cloud-lan-teleport": "GPL-3.0-or-later",
    "cryptography": "Apache-2.0 OR BSD-3-Clause",
    "cffi": "MIT-0",
    "pycparser": "BSD-3-Clause",
    "zeroconf": "LGPL-2.1-or-later",
    "ifaddr": "MIT",
    # Chỉ bootloader (nhúng trong teleport.exe), module loader và runtime hook được đóng vào:
    # bootloader theo GPL-2.0-or-later kèm Bootloader Exception, runtime hook theo Apache-2.0.
    "pyinstaller": (
        "GPLv2-or-later with a special exception which allows to use PyInstaller to build "
        "and distribute non-free programs (including commercial ones)"
    ),
    # Chỉ runtime hook được đóng vào, và runtime hook theo Apache-2.0 (file LICENSE của gói).
    "pyinstaller-hooks-contrib": (
        "License :: OSI Approved :: Apache Software License; "
        "License :: OSI Approved :: GNU General Public License v2 (GPLv2)"
    ),
}

# Giấy phép được chấp nhận cho thành phần LỒNG BÊN TRONG một gói hay một runtime đã duyệt (thành phần
# trong SBOM của gói, thư viện C và mã tích hợp trong CPython ở PYTHON_EMBEDDED). Nguồn quyết định là
# ke_hoach mục 1 (Giấy phép): gói đóng vào trực tiếp phải nằm trong APPROVED; thành phần lồng bên trong
# được dùng thêm các giấy phép dễ dãi tương thích GPLv3 liệt kê ở đây. Ngoài tập này thì build dừng;
# muốn thêm định danh thì sửa ke_hoach mục 1 trước rồi mới sửa tập này.
# Cố ý KHÔNG có LGPL: theo ke_hoach mục 1, LGPL chỉ dùng được cho gói đóng vào dạng file riêng mà người
# dùng thay được (đi qua APPROVED, ví dụ zeroconf), không cho thành phần lồng bên trong/liên kết tĩnh,
# vì người dùng không thay được phần đó.
# Biểu thức có OR thì chỉ cần một nhánh nằm trong tập, có AND thì mọi vế phải nằm trong tập.
SPDX_COMPATIBLE: frozenset[str] = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "Apache-2.0 WITH LLVM-exception",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "bzip2-1.0.6",
        "ISC",
        "MIT",
        "MIT-0",
        "PSF-2.0",
        "Unicode-3.0",
        "Unicode-DFS-2016",
        "Zlib",
        # Không có định danh SPDX chắc chắn nên đặt tên LicenseRef riêng (văn bản ở license.rst/COPYING).
        # Mỗi tên ứng với đúng một mục trong dòng Giấy phép của ke_hoach mục 1:
        "LicenseRef-public-domain",  # mục "public domain" của §1; ở đây: liblzma (XZ Utils 5.2.5)
        # mục "giấy phép chỉ đòi giữ thông báo của dtoa.c (David M. Gay, trong lõi CPython)" của §1
        "LicenseRef-dtoa-notice",
        # mục "DLL runtime MSVC (vcruntime140*.dll) ... ngoại lệ System Library của GPLv3" của §1
        "LicenseRef-MSVC-runtime-system-library",
    }
)

# Thành phần SBOM mà metadata không ghi giấy phép: tên -> (bản chính tối thiểu, SPDX, nguồn dữ kiện).
SBOM_KNOWN_LICENSES: dict[str, tuple[int, str, str]] = {
    # OpenSSL đổi sang Apache-2.0 từ bản 3.0.0.
    "openssl": (3, "Apache-2.0", "https://openssl-library.org/source/license/"),
}

# Văn bản giấy phép của phần mềm bên thứ ba nằm trong CPython mà LICENSE.txt của bản Windows không
# có đủ. Chép nguyên văn vào packaging/pyinstaller/licenses/, theo ĐÚNG phiên bản Python dùng để build
# (thư viện C của bản Windows lấy theo PCbuild/get_externals.bat của cùng tag). Python build khác mọi
# khóa dưới đây thì build dừng: lấy văn bản của tag mới (cùng nguồn) rồi thêm khóa.
# Khóa: phiên bản Python -> các (file trong licenses/, tiêu đề, nguồn đã chép).
_CPYTHON_RST = "https://github.com/python/cpython/blob/{tag}/Doc/license.rst"
_SOURCE_DEPS = "https://github.com/python/cpython-source-deps/blob/{ref}"
CPYTHON_LICENSE_TEXTS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "3.12.10": (
        (
            "cpython-3.12.10-incorporated.rst",
            "CPython 3.12.10 - Licenses and Acknowledgements for Incorporated Software",
            _CPYTHON_RST.format(tag="v3.12.10") + " (từ tiêu đề mục này tới hết file)",
        ),
        (
            "xz-5.2.5-COPYING.txt",
            "XZ Utils 5.2.5 (liblzma trong _lzma.pyd) - COPYING",
            _SOURCE_DEPS.format(ref="xz-5.2.5/COPYING") + " (bản xz ghi trong PCbuild/get_externals.bat)",
        ),
    ),
    "3.14.5": (
        (
            "cpython-3.14.5-incorporated.rst",
            "CPython 3.14.5 - Licenses and Acknowledgements for Incorporated Software",
            _CPYTHON_RST.format(tag="v3.14.5") + " (từ tiêu đề mục này tới hết file)",
        ),
        (
            "xz-5.2.5-COPYING.txt",
            "XZ Utils 5.2.5 (liblzma trong _lzma.pyd) - COPYING",
            _SOURCE_DEPS.format(ref="xz-5.2.5/COPYING") + " (bản xz ghi trong PCbuild/get_externals.bat)",
        ),
        (
            "zlib-ng-2.2.4-LICENSE.txt",
            "zlib-ng 2.2.4 (zlib trong python314.dll) - LICENSE.md",
            _SOURCE_DEPS.format(ref="zlib-ng-2.2.4/LICENSE.md")
            + " (bản zlib-ng ghi trong get_externals.bat)",
        ),
        (
            "mpdecimal-4.0.0-COPYRIGHT.txt",
            "mpdecimal 4.0.0 (libmpdec trong _decimal.pyd) - COPYRIGHT.txt",
            _SOURCE_DEPS.format(ref="mpdecimal-4.0.0/COPYRIGHT.txt")
            + " (bản mpdecimal ghi trong get_externals.bat)",
        ),
    ),
}
LICENSES_DIR = Path(__file__).resolve().parent / "licenses"

# Thành phần bên thứ ba nằm trong file của CPython được đóng vào: tiền tố tên file (chữ thường) ->
# (thành phần, giấy phép dạng biểu thức SPDX kiểm theo SPDX_COMPATIBLE, nơi có văn bản trong
# THIRD-PARTY-NOTICES.txt, các regex phải khớp trong văn bản giấy phép của Python để chắc văn bản đó
# thật sự có mặt). "license.rst" là file cpython-<phiên bản>-incorporated.rst ở trên.
PYTHON_EMBEDDED: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    (
        "libssl-",
        "OpenSSL",
        "Apache-2.0",
        "LICENSE.txt của Python (Apache License 2.0); mục OpenSSL của license.rst",
        (r"Apache License", r"(?m)^OpenSSL$"),
    ),
    (
        "libcrypto-",
        "OpenSSL",
        "Apache-2.0",
        "LICENSE.txt của Python (Apache License 2.0); mục OpenSSL của license.rst",
        (r"Apache License", r"(?m)^OpenSSL$"),
    ),
    (
        "libffi-",
        "libffi",
        "MIT",
        "LICENSE.txt của Python; mục libffi của license.rst",
        (r"libffi - Copyright", r"(?m)^libffi$"),
    ),
    ("pyexpat", "expat", "MIT", "mục expat của license.rst", (r"(?m)^expat$",)),
    (
        "_decimal",
        "libmpdec (mpdecimal)",
        "BSD-2-Clause",
        "mục libmpdec của license.rst; COPYRIGHT.txt của mpdecimal nếu bản Python dùng mpdecimal ngoài",
        (r"(?m)^libmpdec$",),
    ),
    (
        "_lzma",
        "liblzma (XZ Utils)",
        "LicenseRef-public-domain",
        "COPYING của XZ Utils",
        (r"liblzma is in the public domain",),
    ),
    ("_bz2", "bzip2/libbzip2", "bzip2-1.0.6", "LICENSE.txt của Python", (r"bzip2/libbzip2",)),
    (
        "_zstd",
        "Zstandard và phần bindings lấy từ pyzstd",
        "BSD-3-Clause",
        "LICENSE.txt của Python (Zstandard); mục Zstandard bindings của license.rst",
        (r"For Zstandard software", r"(?m)^Zstandard bindings$"),
    ),
    (
        "python3",
        "Mã tích hợp trong lõi CPython: zlib (bản 3.14: zlib-ng), Mersenne Twister, SipHash24, "
        "strtod/dtoa, cfuhash, mimalloc (3.13+), GUS (3.14), phần asyncio lấy từ uvloop",
        "Zlib AND BSD-2-Clause AND BSD-3-Clause AND MIT AND LicenseRef-dtoa-notice",
        "các mục tương ứng của license.rst; LICENSE của zlib-ng nếu bản Python dùng zlib-ng",
        (r"(?m)^zlib$", r"(?m)^Mersenne Twister$", r"(?m)^SipHash24$", r"(?m)^strtod and dtoa$"),
    ),
    (
        "vcruntime140",
        "Microsoft Visual C++ Runtime",
        "LicenseRef-MSVC-runtime-system-library",
        "LICENSE.txt của Python (Additional Conditions for this Windows binary build)",
        (r"Microsoft Distributable Code",),
    ),
)

# Ghi chú bắt buộc cho DLL runtime của trình biên dịch đi kèm Python.
VCRUNTIME_NOTE = (
    "vcruntime140.dll và vcruntime140_1.dll là thư viện runtime của trình biên dịch Microsoft Visual C++\n"
    "đã dùng để build CPython. Chúng đi kèm như một phần của runtime Python, được phân phối theo ngoại\n"
    'lệ System Library của GPLv3 (mục 1 của GPLv3) và theo điều kiện "Additional Conditions for this\n'
    'Windows binary build" trong LICENSE.txt của Python.'
)

_LICENSE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.IGNORECASE)
_SEPARATOR = "=" * 78
_BASE_LIBRARY = "base_library.zip"


class LicenseAuditError(Exception):
    """Bản build chứa thứ chưa duyệt được giấy phép; build phải dừng."""


@dataclass
class Component:
    key: str
    name: str
    version: str
    license: str
    files: list[str] = field(default_factory=list)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _norm_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(path))


def declared_license(dist: md.Distribution) -> str:
    """Giấy phép theo metadata: License-Expression, rồi License, rồi các classifier License."""
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip()
    text = meta.get("License")
    if text and text.strip() and text.strip().upper() != "UNKNOWN":
        return " ".join(text.split())
    classifiers = [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    return "; ".join(classifiers)


def spdx_compatible(expression: str) -> bool:
    """Biểu thức SPDX có tương thích GPLv3 không, theo `SPDX_COMPATIBLE` (OR: một nhánh; AND: mọi vế)."""
    tokens = re.findall(r"\(|\)|[^\s()]+", expression)
    merged: list[str] = []
    i = 0
    while i < len(tokens):  # gộp "X WITH Y" thành một định danh
        if i + 2 < len(tokens) and tokens[i + 1] == "WITH":
            merged.append(f"{tokens[i]} WITH {tokens[i + 2]}")
            i += 3
        else:
            merged.append(tokens[i])
            i += 1
    pos = 0

    def parse_or() -> bool:
        nonlocal pos
        result = parse_and()
        while pos < len(merged) and merged[pos] == "OR":
            pos += 1
            right = parse_and()
            result = result or right
        return result

    def parse_and() -> bool:
        nonlocal pos
        result = parse_atom()
        while pos < len(merged) and merged[pos] == "AND":
            pos += 1
            right = parse_atom()
            result = result and right
        return result

    def parse_atom() -> bool:
        nonlocal pos
        if pos >= len(merged):
            raise LicenseAuditError(f"biểu thức SPDX không hợp lệ: {expression!r}")
        token = merged[pos]
        pos += 1
        if token == "(":
            result = parse_or()
            if pos >= len(merged) or merged[pos] != ")":
                raise LicenseAuditError(f"biểu thức SPDX không hợp lệ: {expression!r}")
            pos += 1
            return result
        return token in SPDX_COMPATIBLE

    result = parse_or()
    if pos != len(merged):
        raise LicenseAuditError(f"biểu thức SPDX không hợp lệ: {expression!r}")
    return result


def _owner_index() -> dict[str, md.Distribution]:
    """Đường dẫn file (đã chuẩn hóa) -> bản phân phối có file đó trong RECORD."""
    index: dict[str, md.Distribution] = {}
    for dist in md.distributions():
        for entry in dist.files or []:
            index.setdefault(_norm_path(Path(str(dist.locate_file(entry)))), dist)
    return index


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def _license_files(dist: md.Distribution) -> list[Path]:
    found = []
    for entry in dist.files or []:
        parts = Path(str(entry)).parts
        in_dist_info = any(p.endswith(".dist-info") for p in parts[:-1])
        if in_dist_info and ("licenses" in parts or _LICENSE_FILE.match(parts[-1])):
            found.append(Path(str(dist.locate_file(entry))))
    return sorted(found)


def _sbom_components(dist: md.Distribution) -> list[tuple[str, str, str, str]]:
    """(tên, phiên bản, giấy phép SPDX, nguồn dữ kiện) cho từng thành phần trong SBOM CycloneDX."""
    components = []
    for entry in dist.files or []:
        parts = Path(str(entry)).parts
        if "sboms" not in parts or not parts[-1].endswith(".json"):
            continue
        data = json.loads(Path(str(dist.locate_file(entry))).read_text(encoding="utf-8"))
        for comp in data.get("components", []):
            name, version = comp.get("name", "?"), comp.get("version", "?")
            licenses = []
            for item in comp.get("licenses", []):
                lic = item.get("expression") or item.get("license", {}).get("id")
                if not lic:
                    raise LicenseAuditError(f"SBOM của {dist.name}: {name} có giấy phép không phải SPDX")
                licenses.append(lic)
            source = f"SBOM {parts[-1]}"
            if not licenses:
                known = SBOM_KNOWN_LICENSES.get(name.lower())
                major = int(version.split(".")[0]) if version.split(".")[0].isdigit() else -1
                if known is None or major < known[0]:
                    raise LicenseAuditError(f"SBOM của {dist.name}: {name} {version} không ghi giấy phép")
                licenses = [known[1]]
                source = f"SBOM {parts[-1]} không ghi giấy phép; theo {known[2]}"
            expression = " AND ".join(f"({lic})" if " " in lic else lic for lic in licenses)
            components.append((name, version, expression, source))
    return components


def _python_license_file(base_prefix: str | None = None) -> Path:
    """LICENSE.txt của bản Python dùng để build (`base_prefix` mặc định là `sys.base_prefix`)."""
    base = Path(sys.base_prefix if base_prefix is None else base_prefix)
    candidates = [
        base / "LICENSE.txt",
        base / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "LICENSE.txt",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise LicenseAuditError(f"không tìm thấy LICENSE.txt của Python trong {base}")


def _git(repo_dir: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo_dir), *args], check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return out.stdout.strip()


def _python_dirs(base_prefix: str | None = None) -> tuple[str, list[str], list[str]]:
    """(base_prefix, thư mục thư viện chuẩn, thư mục site-packages) của bản Python dùng để build.

    `base_prefix` mặc định là `sys.base_prefix`, đọc lúc gọi; test truyền thư mục giả vào đây.
    """
    base = sys.base_prefix if base_prefix is None else base_prefix
    base_vars = {"base": base, "platbase": base, "installed_base": base, "installed_platbase": base}
    base_paths = sysconfig.get_paths(vars=base_vars)
    stdlib = [
        os.path.join(base, "DLLs"),
        os.path.join(base, "Lib"),
        base_paths["stdlib"],
        base_paths["platstdlib"],
    ]
    site_dirs = [base_paths["purelib"], base_paths["platlib"]]
    current = sysconfig.get_paths()
    site_dirs += [current["purelib"], current["platlib"]]
    with contextlib.suppress(AttributeError):  # site.getsitepackages không có trong vài môi trường cũ
        site_dirs += site.getsitepackages() + site.getsitepackages([base])
    # Trên Windows site.getsitepackages() trả cả chính prefix (ví dụ C:\Python314) bên cạnh
    # Lib\site-packages; chỉ giữ thư mục site-packages thật, nếu không mọi file Python đều bị loại.
    site_dirs = [p for p in site_dirs if Path(p).name.lower() in {"site-packages", "dist-packages"}]
    return (
        _norm_path(base),
        sorted({_norm_path(p) for p in stdlib}),
        sorted({_norm_path(p) for p in site_dirs}),
    )


# File được nhận là của Python khi nằm thẳng trong base_prefix (mẫu tên, so chữ thường). Mọi file khác
# ở gốc base_prefix là file lạ. python.exe/pythonw.exe không có ở đây vì bản onedir không đóng chúng.
PYTHON_ROOT_FILES: tuple[str, ...] = ("python3*.dll", "vcruntime140*.dll")

# Universal CRT (mẫu tên file đích, so chữ thường): thành phần của Windows 10 trở lên nên không đóng vào.
# PyInstaller cố ý gom các DLL này khi tìm thấy chúng ngoài System32, ví dụ trong thư mục bin của một
# JDK nằm trên PATH, nên bản sao lấy từ công cụ không liên quan lọt vào bản build. Spec lọc chúng khỏi
# `Analysis.binaries` bằng `drop_ucrt` TRƯỚC `check`; `check` không nhận chúng là file đã biết, nên bản
# sao nào lọt qua bộ lọc vẫn làm build dừng như mọi file lạ. Chỉ đúng ba họ này, không mở rộng.
UCRT_FILES: tuple[str, ...] = ("api-ms-win-core-*.dll", "api-ms-win-crt-*.dll", "ucrtbase.dll")


def drop_ucrt(
    toc: Iterable[tuple[str, str, str]],
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Bỏ khỏi TOC các file Universal CRT (`UCRT_FILES`, theo tên file đích, không phân biệt hoa thường).

    Trả (TOC còn lại, giữ nguyên thứ tự; tên đích đã bỏ).
    """
    kept: list[tuple[str, str, str]] = []
    dropped: list[str] = []
    for entry in toc:
        name = re.split(r"[\\/]", entry[0])[-1].lower()
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in UCRT_FILES):
            dropped.append(entry[0])
        else:
            kept.append(entry)
    return kept, dropped


def _is_python_file(path: str, python_dirs: tuple[str, list[str], list[str]]) -> bool:
    """File thuộc chính bản Python (PSF-2.0 và thành phần ở PYTHON_EMBEDDED) hay không.

    `path` đã chuẩn hóa bằng `_norm_path`; `python_dirs` là kết quả của `_python_dirs`. Chỉ nhận: file
    nằm thẳng trong `base_prefix` có tên khớp `PYTHON_ROOT_FILES`, file trong `DLLs/` và trong thư
    viện chuẩn `Lib/`. Mọi thứ trong site-packages (CI không dùng venv nên site-packages nằm dưới
    `base_prefix`) mà không có RECORD nhận là file lạ, build phải dừng.
    """
    base, stdlib, site_dirs = python_dirs
    parts = {part.lower() for part in Path(path).parts}
    if parts & {"site-packages", "dist-packages"}:
        return False
    if any(_is_under(path, root) for root in site_dirs):
        return False
    if os.path.dirname(path) == base:
        name = os.path.basename(path).lower()
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in PYTHON_ROOT_FILES)
    return any(_is_under(path, root) for root in stdlib)


def _localpyc_source(name: str, path: str, base_prefix: str | None = None) -> str | None:
    """Nguồn của module khởi động mà PYZ dịch sẵn vào `<workpath>/localpycs/` (PYZ.dependencies).

    Đó là module loader của PyInstaller (`PyInstaller/loader/pyimod0x_*.py`) và `struct` của thư viện
    chuẩn. Trả đường dẫn file nguồn để phân loại như mọi file khác; không nhận ra thì None (file lạ).
    """
    if Path(path).parent.name != "localpycs" or not path.endswith(".pyc"):
        return None
    base = sys.base_prefix if base_prefix is None else base_prefix
    loader = md.distribution("pyinstaller").locate_file(f"PyInstaller/loader/{name}.py")
    base_vars = {"installed_base": base, "base": base}
    candidates = [Path(str(loader)), Path(sysconfig.get_paths(vars=base_vars)["stdlib"]) / f"{name}.py"]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def collect_components(
    toc: Iterable[tuple[str, str, str]], *, repo_dir: Path, base_prefix: str | None = None
) -> tuple[dict[str, Component], dict[str, list[str]]]:
    """Phân loại mọi file trong TOC theo thành phần.

    Trả (thành phần, file của CPython có chứa thành phần bên thứ ba theo `PYTHON_EMBEDDED`).
    """
    owners = _owner_index()
    # Mã của dự án: src/ (gói teleport) và packaging/ (launcher). Không lấy cả gốc repo vì venv
    # build có thể nằm trong repo (.venv/), file của venv phải tra theo RECORD.
    own_roots = [_norm_path(repo_dir / "src"), _norm_path(repo_dir / "packaging")]
    python_dirs = _python_dirs(base_prefix)
    components: dict[str, Component] = {}
    python_files: dict[str, list[str]] = {}  # tiền tố trong PYTHON_EMBEDDED -> tên file đã đóng vào
    unknown: list[str] = []

    def add(dist: md.Distribution, dest: str) -> None:
        key = _normalize(dist.name)
        comp = components.get(key)
        if comp is None:
            comp = Component(key, dist.name, dist.version, declared_license(dist))
            components[key] = comp
        comp.files.append(dest)

    for dest, src, _typecode in toc:
        if not src:
            continue
        path = _norm_path(src)
        source = _localpyc_source(dest, path, base_prefix)
        if source is not None:
            path = _norm_path(source)
        # Tra RECORD trước: site-packages có thể nằm trong thư mục cài Python (không dùng venv).
        if path in owners:
            add(owners[path], dest)
        elif any(_is_under(path, root) for root in own_roots):
            add(md.distribution("zero-cloud-lan-teleport"), dest)
        elif _is_python_file(path, python_dirs) or (
            dest == _BASE_LIBRARY and os.path.basename(path) == _BASE_LIBRARY
        ):
            # base_library.zip do PyInstaller dựng trong workpath từ thư viện chuẩn của Python.
            components.setdefault(
                "python",
                Component("python", "Python (CPython)", sys.version.split()[0], "PSF-2.0"),
            ).files.append(dest)
            base = os.path.basename(path)
            for prefix, *_rest in PYTHON_EMBEDDED:
                # Chỉ file mã máy (.pyd/.dll); module .py trùng tiền tố (ví dụ _zstdfile.py) không tính.
                if base.lower().startswith(prefix) and base.lower().endswith((".pyd", ".dll")):
                    python_files.setdefault(prefix, []).append(base)
        else:
            unknown.append(f"{dest} <- {src}")
    if unknown:
        raise LicenseAuditError(
            "không xác định được file sau thuộc gói nào:\n  " + "\n  ".join(sorted(unknown))
        )
    # Bootloader nhúng trong teleport.exe không nằm trong TOC.
    if "pyinstaller" not in components:
        add(md.distribution("pyinstaller"), "teleport.exe (bootloader)")
    return components, {prefix: sorted(set(files)) for prefix, files in python_files.items()}


def audit(
    components: dict[str, Component], python_files: dict[str, list[str]]
) -> dict[str, list[tuple[str, str, str, str]]]:
    """Dừng build nếu có gì chưa duyệt; trả thành phần SBOM của từng gói (đã kiểm)."""
    errors = []
    sboms: dict[str, list[tuple[str, str, str, str]]] = {}
    for key, comp in sorted(components.items()):
        if key == "python":
            continue
        expected = APPROVED.get(key)
        if expected is None:
            errors.append(f"{comp.name} {comp.version} ({comp.license}): chưa có trong APPROVED")
        elif comp.license != expected:
            errors.append(
                f"{comp.name} {comp.version}: giấy phép {comp.license!r} khác bản đã duyệt {expected!r}"
            )
        entries = _sbom_components(md.distribution(comp.name))
        for name, version, expression, _source in entries:
            if not spdx_compatible(expression):
                errors.append(f"{comp.name} (SBOM): {name} {version} giấy phép {expression!r} chưa duyệt")
        if entries:
            sboms[key] = entries
    for prefix, name, lic, _where, _markers in PYTHON_EMBEDDED:
        if prefix in python_files and not spdx_compatible(lic):
            errors.append(f"Python: {name} giấy phép {lic!r} chưa duyệt")
    if errors:
        raise LicenseAuditError("giấy phép chưa duyệt:\n  " + "\n  ".join(errors))
    return sboms


_SOURCE_URL_LABELS = frozenset(
    {"source", "source code", "sourcecode", "repository", "github", "homepage", "home"}
)


def _project_urls(dist: md.Distribution) -> list[str]:
    """Nơi lấy mã nguồn: trang file của đúng phiên bản trên PyPI (có sdist) + kho mã trong metadata."""
    urls = [f"https://pypi.org/project/{dist.name}/{dist.version}/#files"]
    for entry in dist.metadata.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in _SOURCE_URL_LABELS and url.strip():
            urls.append(url.strip())
    home = dist.metadata.get("Home-page")
    if home and home.strip():
        urls.append(home.strip())
    return list(dict.fromkeys(urls))


def _cpython_license_texts() -> list[tuple[str, str, str]]:
    """(tiêu đề, nguồn, nội dung) của văn bản giấy phép CPython bổ sung, đúng phiên bản đang build."""
    version = platform.python_version()
    entries = CPYTHON_LICENSE_TEXTS.get(version)
    if entries is None:
        raise LicenseAuditError(
            f"chưa có văn bản giấy phép phần mềm tích hợp của CPython {version} trong "
            "packaging/pyinstaller/licenses/ (xem CPYTHON_LICENSE_TEXTS)"
        )
    return [
        (title, source, (LICENSES_DIR / name).read_text(encoding="utf-8")) for name, title, source in entries
    ]


def _python_library_versions() -> dict[str, str]:
    """Phiên bản thư viện C trong bản Python đang build (cùng các file sẽ được đóng vào)."""
    versions = {"libssl-": ssl.OPENSSL_VERSION, "libcrypto-": ssl.OPENSSL_VERSION}
    with contextlib.suppress(ImportError):
        import pyexpat

        versions["pyexpat"] = pyexpat.EXPAT_VERSION
    with contextlib.suppress(ImportError):
        import decimal

        versions["_decimal"] = f"libmpdec {decimal.__libmpdec_version__}"
    with contextlib.suppress(ImportError):
        import zlib

        zlib_ng = getattr(zlib, "ZLIBNG_VERSION", None)
        versions["python3"] = f"zlib-ng {zlib_ng}" if zlib_ng else f"zlib {zlib.ZLIB_RUNTIME_VERSION}"
    with contextlib.suppress(ImportError):
        import _zstd

        versions["_zstd"] = f"zstd {_zstd.zstd_version}"
    return versions


def build_notices(
    components: dict[str, Component],
    python_files: dict[str, list[str]],
    sboms: dict[str, list[tuple[str, str, str, str]]],
    *,
    app_version: str,
    base_prefix: str | None = None,
) -> str:
    """Nội dung THIRD-PARTY-NOTICES.txt. Thiếu văn bản giấy phép nào thì `LicenseAuditError`."""
    python_license = ""
    extra_texts: list[tuple[str, str, str]] = []
    if "python" in components:
        python_license = _python_license_file(base_prefix).read_text(encoding="utf-8", errors="replace")
        extra_texts = _cpython_license_texts()
    all_python_text = "\n".join([python_license, *(text for _title, _source, text in extra_texts)])
    embedded = [entry for entry in PYTHON_EMBEDDED if entry[0] in python_files]
    for prefix, name, _lic, _where, markers in embedded:
        missing = [m for m in markers if not re.search(m, all_python_text)]
        if missing:
            raise LicenseAuditError(
                f"văn bản giấy phép của Python thiếu phần cho {name} "
                f"({', '.join(python_files[prefix])}): {missing}"
            )
    versions = _python_library_versions()
    lines = [
        f"Teleport {app_version} (bản CLI cho Windows) - thông báo giấy phép của thành phần bên thứ ba",
        "",
        "Teleport phát hành theo GPL-3.0-or-later (file LICENSE cùng thư mục). Bản build này đóng gói",
        "kèm các thành phần dưới đây; mỗi thành phần giữ giấy phép riêng, văn bản đầy đủ ở phần sau.",
        "Mã nguồn đúng bản build: xem SOURCE.txt.",
        "",
        "Danh sách thành phần (tên, phiên bản, giấy phép theo metadata):",
    ]
    others = [c for k, c in sorted(components.items()) if k != "zero-cloud-lan-teleport"]
    for comp in others:
        lines.append(f"  - {comp.name} {comp.version}: {comp.license}")
    if embedded:
        lines.append("  - Thành phần bên thứ ba nằm trong các file của Python được đóng vào:")
        for prefix, name, lic, where, _markers in embedded:
            version = f" [{versions[prefix]}]" if prefix in versions else ""
            lines.append(f"      {name}{version} ({', '.join(python_files[prefix])}): {lic}.")
            lines.append(f"        Văn bản: {where}.")
    for key, entries in sorted(sboms.items()):
        lines.append(
            f"  - Thành phần liên kết tĩnh trong {components[key].name} theo SBOM của gói"
            " (SBOM liệt kê cả crate chỉ dùng lúc biên dịch):"
        )
        lines.extend(f"      {name} {version}: {expression}" for name, version, expression, _ in entries)
    if "vcruntime140" in python_files:
        lines += ["", VCRUNTIME_NOTE]
    for comp in others:
        lines += ["", _SEPARATOR, f"{comp.name} {comp.version}", f"Giấy phép: {comp.license}"]
        if comp.key == "python":
            lines.append(
                f"Văn bản: LICENSE.txt của Python {comp.version}, rồi phần mềm tích hợp trong CPython"
                " (license.rst đúng phiên bản) và thư viện C của bản Windows mà LICENSE.txt chưa có"
            )
            lines += [_SEPARATOR, f"[Python] LICENSE.txt (từ thư mục cài Python {comp.version})", ""]
            lines.append(python_license.rstrip())
            for title, source, text in extra_texts:
                lines += [_SEPARATOR, f"[Python] {title}", f"Nguồn: {source}", "", text.rstrip()]
            continue
        dist = md.distribution(comp.name)
        lines.append("Mã nguồn: " + " ; ".join(_project_urls(dist)))
        for name, version, expression, source in sboms.get(comp.key, []):
            if "theo http" in source:
                lines.append(f"Ghi chú: {name} {version} liên kết tĩnh, giấy phép {expression} ({source}).")
        license_files = _license_files(dist)
        if not license_files:
            raise LicenseAuditError(f"{comp.name}: không có file giấy phép trong dist-info")
        for lic in license_files:
            lines += [_SEPARATOR, f"[{comp.name}] {lic.name}", "", lic.read_text(encoding="utf-8").rstrip()]
    return "\n".join(lines) + "\n"


def build_source(components: dict[str, Component], *, repo_dir: Path, app_version: str) -> str:
    """Nội dung SOURCE.txt (URL repo, SHA commit, cách thay thư viện LGPL, nơi lấy mã nguồn)."""
    pyproject = tomllib.loads((repo_dir / "pyproject.toml").read_text(encoding="utf-8"))
    repo_url = pyproject["project"]["urls"]["Repository"].rstrip("/")
    sha = os.environ.get("GITHUB_SHA") or _git(repo_dir, "rev-parse", "HEAD")
    dirty = bool(_git(repo_dir, "status", "--porcelain"))
    lines = [
        f"Teleport {app_version} (bản CLI cho Windows) - mã nguồn",
        "",
        "Teleport là phần mềm tự do theo GPL-3.0-or-later (file LICENSE).",
        f"Kho mã nguồn: {repo_url}",
        f"Commit dùng để build: {sha}",
        f"Mã nguồn đúng bản build: {repo_url}/tree/{sha}",
    ]
    if dirty:
        lines.append(
            "CẢNH BÁO: bản build này dựng từ cây làm việc có thay đổi chưa commit, "
            "nên mã nguồn không trùng hoàn toàn commit trên. Đừng phát hành bản build này."
        )
    zeroconf = components.get("zeroconf")
    if zeroconf is not None:
        lines += [
            "",
            f"Thư viện LGPL: zeroconf {zeroconf.version} ({zeroconf.license}) nằm ở _internal\\zeroconf\\",
            "dưới dạng file riêng (.py và .pyd), không gộp vào teleport.exe. Bạn có thể thay cả thư mục",
            "đó bằng bản zeroconf đã sửa đổi có cùng API để chạy với Teleport. Các module .pyd (Cython) phụ",
            "thuộc lẫn nhau nên thay cả bộ: hoặc .pyd build cho đúng phiên bản Python ghi ở cuối file này,",
            "hoặc xóa mọi .pyd và đặt file .py tương ứng (zeroconf chạy được ở dạng Python thuần).",
            "Mã nguồn zeroconf: " + " ; ".join(_project_urls(md.distribution("zeroconf"))),
        ]
    lines += [
        "",
        "Mã nguồn các thành phần khác: xem dòng 'Mã nguồn:' của từng thành phần",
        "trong THIRD-PARTY-NOTICES.txt.",
        "",
        f"Công cụ build: Python {sys.version.split()[0]}, PyInstaller {md.version('pyinstaller')}, "
        f"pyinstaller-hooks-contrib {md.version('pyinstaller-hooks-contrib')}.",
    ]
    return "\n".join(lines) + "\n"


@dataclass
class AuditResult:
    """Kết quả kiểm giấy phép (trước EXE/COLLECT): thành phần và nội dung các file sẽ ghi sau COLLECT."""

    components: dict[str, Component]
    python_files: dict[str, list[str]]
    sboms: dict[str, list[tuple[str, str, str, str]]]
    notices: str  # nội dung THIRD-PARTY-NOTICES.txt
    source: str  # nội dung SOURCE.txt


def check(
    toc: Iterable[tuple[str, str, str]],
    *,
    repo_dir: Path,
    app_version: str,
    base_prefix: str | None = None,
) -> AuditResult:
    """Kiểm giấy phép mọi file trong TOC và dựng sẵn nội dung file giấy phép. Gọi trước EXE/COLLECT.

    Mọi phép kiểm (gói lạ, giấy phép chưa duyệt, SBOM, file Python lạ, LICENSE.txt của Python, văn
    bản CPython đúng phiên bản, đoạn chữ bắt buộc, file giấy phép trong dist-info, SHA commit) đều
    chạy ở đây; lỗi thì `LicenseAuditError` (hoặc lỗi của git) trước khi có teleport.exe.
    `base_prefix` mặc định là `sys.base_prefix`.
    """
    components, python_files = collect_components(toc, repo_dir=repo_dir, base_prefix=base_prefix)
    sboms = audit(components, python_files)
    notices = build_notices(components, python_files, sboms, app_version=app_version, base_prefix=base_prefix)
    source = build_source(components, repo_dir=repo_dir, app_version=app_version)
    report = ["Thành phần đóng vào bản build (giấy phép theo metadata):"]
    for _key, comp in sorted(components.items()):
        report.append(f"  {comp.name} {comp.version}: {comp.license} ({len(comp.files)} file)")
    for prefix, name, lic, _where, _markers in PYTHON_EMBEDDED:
        if prefix in python_files:
            report.append(f"  {name} ({', '.join(python_files[prefix])}, trong Python): {lic}")
    for key, entries in sorted(sboms.items()):
        report.append(f"  SBOM {components[key].name}: {len(entries)} thành phần, đều tương thích GPLv3")
    log("\n".join(report))
    return AuditResult(components, python_files, sboms, notices, source)


def write_outputs(result: AuditResult, *, dist_dir: Path, repo_dir: Path) -> None:
    """Chỉ ghi file: LICENSE, THIRD-PARTY-NOTICES.txt, SOURCE.txt vào `dist_dir`. Gọi sau COLLECT."""
    (dist_dir / "LICENSE").write_bytes((repo_dir / "LICENSE").read_bytes())
    (dist_dir / "THIRD-PARTY-NOTICES.txt").write_text(result.notices, encoding="utf-8")
    (dist_dir / "SOURCE.txt").write_text(result.source, encoding="utf-8")


# Tên thư mục onedir trong distpath (COLLECT name trong teleport-cli.spec).
DIST_NAME = "teleport"


def remove_stale_dist(distpath: Path, name: str) -> bool:
    """Xóa bản build cũ `distpath/DIST_NAME` khi `check` hỏng; trả True nếu đã xóa.

    `--clean` của PyInstaller không dọn distpath (chỉ COLLECT mới thay thư mục), nên nếu không xóa thì
    dist/ vẫn còn teleport.exe của lần build trước dù lần này kiểm giấy phép hỏng. Chỉ xóa đúng thư
    mục tên `DIST_NAME` nằm thẳng trong `distpath`: tên khác, link/junction, hay không phải thư mục
    thì `ValueError` và không xóa gì.
    """
    if name != DIST_NAME:
        raise ValueError(f"chỉ xóa thư mục {DIST_NAME!r} trong distpath, không xóa {name!r}")
    root = Path(os.path.abspath(distpath))
    target = root / name
    if not os.path.lexists(target):
        return False
    if target.is_symlink() or os.path.isjunction(target) or not target.is_dir():
        raise ValueError(f"không xóa {target}: không phải thư mục thường")
    if target.resolve().parent != root.resolve():
        raise ValueError(f"không xóa {target}: không nằm thẳng trong {root}")
    shutil.rmtree(target)
    return True


def log(text: str) -> None:
    """In ra log build; console Windows có thể không phải UTF-8 nên thay ký tự không in được."""
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, "backslashreplace").decode(encoding), flush=True)
