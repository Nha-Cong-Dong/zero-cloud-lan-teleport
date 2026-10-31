"""Công cụ dịch `packaging/i18n/update.py` (spec-d6 mục 2.4, 5.4; mục 12 ý 10–12 thắng).

Viết từ spec, không từ code. Spec chỉ định giao diện dòng lệnh (`update.py`, `update.py --check`), nên test
chạy công cụ thật bằng tiến trình con trên một **cây mã giả** trong `tmp_path`: chép thư mục
`packaging/i18n/` vào `<tmp>/repo/packaging/i18n/`, dựng `<tmp>/repo/src/teleport/ui/**/*.py`, chạy với
thư mục làm việc là `<tmp>/repo` (công cụ tìm gốc repo theo vị trí của nó hay theo thư mục làm việc đều
ra cây giả). Không đụng `src/teleport/ui/i18n/` thật (đợt 1–2 không commit `teleport_en.*`, mục 2.4).

Module không phải gói nên nạp bằng `importlib.util.spec_from_file_location` (như `test_third_party.py`;
`import packaging…` trùng gói PyPI), chỉ để kiểm nó nạp được mà không chạy gì và không import `teleport`
hay PySide6 (mục 12 ý 11).

Hành vi kiểm:
- Trích bằng `ast`: `QCoreApplication.translate(...)`, `translate(...)`, bí danh gán bằng
  `QCoreApplication.translate` theo phạm vi lồng nhau (hàm bao ngoài, closure, lambda); literal ghép ngầm;
  disambiguation (None bỏ qua); `n` ở vị trí 4 hoặc `n=` → numerus. Không phải literal, hay `.tr(` → lỗi
  `file:dòng`, thoát 1.
- Gộp với `.ts` cũ theo khóa `(context, source, comment)`: giữ bản dịch, message mới `type="unfinished"`,
  message không còn thì bỏ. TS 2.1 `language="en" sourcelanguage="vi"`, sắp theo ngữ cảnh rồi nguồn (code
  point), `<location>` chỉ có `filename` (tương đối, `/`), không số dòng; UTF-8 không BOM, `\\n`; chạy lại ra
  đúng từng byte. Rồi lrelease → `teleport_en.qm` (gọi qua `sys.executable`, không cần lrelease trên PATH).
- `--check`: không ghi; thoát 1 nếu `.ts` khác kết quả trích hoặc còn unfinished/rỗng.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_TOOL_DIR = _REPO / "packaging" / "i18n"
_TOOL = _TOOL_DIR / "update.py"
_TS_REL = Path("src/teleport/ui/i18n/teleport_en.ts")
_QM_REL = Path("src/teleport/ui/i18n/teleport_en.qm")


# =================================================================================================
# Hàm phụ trợ
# =================================================================================================


@dataclass
class Msg:
    context: str
    source: str
    comment: str | None
    numerus: bool
    type: str | None
    translation: str | None
    forms: list[str] = field(default_factory=list)
    locations: list[dict[str, str]] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.context, self.source, self.comment or None)


def parse_ts(path: Path) -> list[Msg]:
    root = ET.parse(path).getroot()
    assert root.tag == "TS"
    out: list[Msg] = []
    for ctx in root.findall("context"):
        name = ctx.findtext("name")
        assert name is not None
        for m in ctx.findall("message"):
            tr = m.find("translation")
            assert tr is not None, f"message thiếu <translation>: {m.findtext('source')!r}"
            out.append(
                Msg(
                    context=name,
                    source=m.findtext("source") or "",
                    comment=m.findtext("comment"),
                    numerus=m.get("numerus") == "yes",
                    type=tr.get("type"),
                    translation=tr.text,
                    forms=[f.text or "" for f in tr.findall("numerusform")],
                    locations=[dict(loc.attrib) for loc in m.findall("location")],
                )
            )
    return out


def by_key(msgs: list[Msg]) -> dict[tuple[str, str, str | None], Msg]:
    result = {m.key: m for m in msgs}
    assert len(result) == len(msgs), "khóa (context, source, comment) trùng trong .ts"
    return result


def fill_translations(path: Path, mapping: dict[str, str] | None = None) -> None:
    """Dịch mọi message của `.ts` (bỏ `type`): `mapping[source]` nếu có, không thì `EN:<source>`;
    numerus điền hai dạng có `%n`."""
    mapping = mapping or {}
    tree = ET.parse(path)
    for m in tree.getroot().iter("message"):
        source = m.findtext("source") or ""
        tr = m.find("translation")
        assert tr is not None
        tr.attrib.pop("type", None)
        for child in list(tr):
            tr.remove(child)
        if m.get("numerus") == "yes":
            tr.text = None
            for form in (f"%n {source} (một)", f"%n {source} (nhiều)"):
                ET.SubElement(tr, "numerusform").text = form
        else:
            tr.text = mapping.get(source, f"EN:{source}")
    tree.write(path, encoding="utf-8", xml_declaration=True)


class Repo:
    """Cây repo giả: `packaging/i18n/` chép từ repo thật, `src/teleport/ui/` do test viết."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.ui = root / "src" / "teleport" / "ui"
        (self.ui / "i18n").mkdir(parents=True)
        (root / "src" / "teleport" / "__init__.py").write_text("", encoding="utf-8")
        (self.ui / "__init__.py").write_text("", encoding="utf-8")
        shutil.copytree(_TOOL_DIR, root / "packaging" / "i18n", ignore=shutil.ignore_patterns("__pycache__"))

    @property
    def ts(self) -> Path:
        return self.root / _TS_REL

    @property
    def qm(self) -> Path:
        return self.root / _QM_REL

    def write(self, rel: str, code: str) -> Path:
        path = self.ui / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")
        return path

    def run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        run_env = dict(os.environ if env is None else env)
        run_env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(self.root / "packaging" / "i18n" / "update.py"), *args],
            cwd=self.root,
            env=run_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )

    def ok(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = self.run(*args)
        assert result.returncode == 0, (
            f"update.py {args} thoát {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
        return result


def output_of(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout or "") + (result.stderr or "")


@pytest.fixture
def repo(tmp_path) -> Repo:
    assert _TOOL.is_file(), f"chưa có {_TOOL} (spec-d6 mục 2.4)"
    return Repo(tmp_path / "repo")


HEADER = "from PySide6.QtCore import QCoreApplication\n\n"


def src(body: str) -> str:
    """Mã nguồn giả: dòng import `QCoreApplication` + `body` đã bỏ thụt lề."""
    return HEADER + textwrap.dedent(body).lstrip("\n")


# =================================================================================================
# Nạp module, ranh giới import
# =================================================================================================


def test_module_loads_without_running(tmp_path, monkeypatch):
    """Nạp bằng `spec_from_file_location` không chạy công cụ (có `if __name__ == "__main__"`), không ghi
    gì."""
    assert _TOOL.is_file(), f"chưa có {_TOOL}"
    monkeypatch.chdir(tmp_path)
    name = "teleport_packaging_i18n_update_under_test"
    spec = importlib.util.spec_from_file_location(name, _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    assert list(tmp_path.iterdir()) == []


def test_tool_imports_neither_teleport_nor_pyside():
    """Mục 12 ý 11: script không import `teleport`/PySide6 (PySide6 chỉ chạy trong tiến trình con lrelease)
    và không tìm lrelease bằng `shutil.which`."""
    assert _TOOL.is_file(), f"chưa có {_TOOL}"
    for path in sorted(_TOOL_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                top = n.split(".")[0]
                assert top not in {"teleport", "PySide6", "shiboken6"}, (
                    f"{path.name}:{node.lineno} import {n}"
                )
            if isinstance(node, ast.Attribute) and node.attr == "which":
                assert not (isinstance(node.value, ast.Name) and node.value.id == "shutil"), (
                    f"{path.name}:{node.lineno} dùng shutil.which"
                )


# =================================================================================================
# Trích
# =================================================================================================


def test_extracts_alias_and_implicit_concat(repo):
    repo.write(
        "a.py",
        src(
            """
        translate = QCoreApplication.translate


        def direct():
            return QCoreApplication.translate("Ctx", "Trực tiếp")


        def bare():
            return translate("Ctx", "Tên trần")


        def alias():
            t = QCoreApplication.translate
            return t("Ctx", "Một " "hai"), t(
                "Ctx",
                "Dòng một, "
                "dòng hai",
            )


        def outer():
            tr = QCoreApplication.translate

            def inner():
                def deeper():
                    return tr("Ctx", "Closure hai tầng")

                return tr("Ctx", "Trong closure"), deeper

            callback = lambda: tr("Ctx", "Trong lambda")  # noqa: E731
            return inner, callback


        def other():
            t = "{}{}".format
            return t("Ctx", "Không trích: t ở đây không phải bí danh")


        class Page:
            def retranslate(self):
                t = QCoreApplication.translate
                self.title = t("Trang", "Tiêu đề trong phương thức")
        """
        ),
    )
    repo.write(
        "sub/b.py",
        src(
            """
        def f():
            return QCoreApplication.translate("Khac", "Trong thư mục con")
        """
        ),
    )
    repo.ok()
    keys = {(m.context, m.source) for m in parse_ts(repo.ts)}
    assert keys == {
        ("Ctx", "Trực tiếp"),
        ("Ctx", "Tên trần"),
        ("Ctx", "Một hai"),
        ("Ctx", "Dòng một, dòng hai"),
        ("Ctx", "Closure hai tầng"),
        ("Ctx", "Trong closure"),
        ("Ctx", "Trong lambda"),
        ("Trang", "Tiêu đề trong phương thức"),
        ("Khac", "Trong thư mục con"),
    }
    assert all(m.type == "unfinished" for m in parse_ts(repo.ts))


def test_extracts_disambiguation_and_numerus(repo):
    repo.write(
        "a.py",
        src(
            """
        def f(count):
            t = QCoreApplication.translate
            return (
                t("Ctx", "Mở", "menu Tệp"),
                t("Ctx", "Mở", "nút " "thanh công cụ"),
                t("Ctx", "Không chú thích", None),
                QCoreApplication.translate("Ctx", "%n máy", None, count),
                t("Ctx", "%n lượt", n=count),
                t("Ctx", "%n tệp", "đếm tệp", count),
            )
        """
        ),
    )
    repo.ok()
    msgs = by_key(parse_ts(repo.ts))
    assert ("Ctx", "Mở", "menu Tệp") in msgs
    assert ("Ctx", "Mở", "nút thanh công cụ") in msgs
    plain = msgs[("Ctx", "Không chú thích", None)]
    assert not plain.numerus
    for source, comment in (("%n máy", None), ("%n lượt", None), ("%n tệp", "đếm tệp")):
        assert msgs[("Ctx", source, comment)].numerus, source
    assert not msgs[("Ctx", "Mở", "menu Tệp")].numerus


BAD_CASES = {
    "nguon-la-bien": 'QCoreApplication.translate("Ctx", ten)',
    "ngu-canh-la-bien": 'QCoreApplication.translate(ctx, "Nguồn")',
    "f-string": 'QCoreApplication.translate("Ctx", f"Xin chào {ten}")',
    "chu-thich-la-bien": 'QCoreApplication.translate("Ctx", "Nguồn", ctx)',
    "bi-danh": 't("Ctx", ten)',
    "self-tr": 'self.tr("Xin chào")',
    "widget-tr": 'widget.tr("Xin chào")',
}


@pytest.mark.parametrize("call", list(BAD_CASES.values()), ids=list(BAD_CASES))
def test_non_literal_argument_is_error_with_location(repo, call):
    """Dòng lỗi nằm ở dòng 8 của `ui/sub/bad.py`: thông báo phải có `bad.py:8`, thoát 1."""
    repo.write("good.py", HEADER + 'X = QCoreApplication.translate("Ctx", "Tốt")\n')
    code = (
        HEADER  # dòng 1–2
        + "\n"  # 3
        + "def f(self, widget, ten, ctx):\n"  # 4
        + "    t = QCoreApplication.translate\n"  # 5
        + '    ok = t("Ctx", "Hợp lệ")\n'  # 6
        + "    del ok\n"  # 7
        + f"    return {call}\n"  # 8
    )
    repo.write("sub/bad.py", code)
    result = repo.run()
    assert result.returncode == 1, output_of(result)
    assert re.search(r"bad\.py:8\b", output_of(result)), output_of(result)


def test_check_also_reports_tr_call(repo):
    repo.write("w.py", "class W:\n    def f(self):\n        return self.tr('Nút')\n")
    result = repo.run("--check")
    assert result.returncode == 1
    assert re.search(r"w\.py:3\b", output_of(result)), output_of(result)


# =================================================================================================
# Gộp với .ts cũ
# =================================================================================================


def test_merge_keeps_translations_marks_new_unfinished(repo):
    repo.write(
        "a.py",
        src(
            """
        def f():
            t = QCoreApplication.translate
            return t("Ctx", "Ba"), t("Ctx", "Mở", "menu"), t("Ctx", "Mở", "nút")
        """
        ),
    )
    repo.ok()
    fill_translations(repo.ts, {"Ba": "Three", "Mở": "Open"})
    repo.ok()
    # Đổi chú thích thì là message khác (khóa có comment); thêm một chuỗi mới.
    repo.write(
        "a.py",
        src(
            """
        def f():
            t = QCoreApplication.translate
            return t("Ctx", "Ba"), t("Ctx", "Mở", "menu"), t("Ctx", "Mở", "thanh"), t("Ctx", "Bốn")
        """
        ),
    )
    repo.ok()
    msgs = by_key(parse_ts(repo.ts))
    assert msgs[("Ctx", "Ba", None)].translation == "Three"
    assert msgs[("Ctx", "Ba", None)].type is None
    assert msgs[("Ctx", "Mở", "menu")].translation == "Open"
    assert msgs[("Ctx", "Mở", "menu")].type is None
    for key in (("Ctx", "Bốn", None), ("Ctx", "Mở", "thanh")):
        assert msgs[key].type == "unfinished", key


def test_merge_drops_vanished_messages(repo):
    repo.write("a.py", HEADER + 'A = QCoreApplication.translate("Ctx", "Còn")\n')
    repo.write("b.py", HEADER + 'B = QCoreApplication.translate("Cu", "Sẽ mất")\n')
    repo.ok()
    fill_translations(repo.ts)
    repo.ok()
    (repo.ui / "b.py").unlink()
    repo.ok()
    msgs = parse_ts(repo.ts)
    assert [m.key for m in msgs] == [("Ctx", "Còn", None)]
    assert msgs[0].translation == "EN:Còn"
    root = ET.parse(repo.ts).getroot()
    assert [c.findtext("name") for c in root.findall("context")] == ["Ctx"], "ngữ cảnh rỗng phải bị bỏ"
    assert "vanished" not in repo.ts.read_text(encoding="utf-8"), (
        "không giữ message cũ dạng vanished/obsolete"
    )


# =================================================================================================
# Định dạng, tất định
# =================================================================================================


def test_output_deterministic_bytes(repo):
    repo.write(
        "z.py",
        src(
            """
        def f():
            t = QCoreApplication.translate
            return t("Zeta", "b"), t("Zeta", "B"), t("Zeta", "a"), t("Zeta", "Ă"), t("beta", "x")
        """
        ),
    )
    repo.write(
        "sub/a.py",
        HEADER
        + 'A = (QCoreApplication.translate("Alpha", "Một"), QCoreApplication.translate("Đích", "Hai"))\n',
    )
    repo.ok()
    first = repo.ts.read_bytes()
    repo.ok()
    assert repo.ts.read_bytes() == first, "chạy lại phải ra đúng từng byte"

    assert not first.startswith(b"\xef\xbb\xbf"), "không BOM"
    assert b"\r" not in first, "chỉ `\\n`"
    first.decode("utf-8")
    root = ET.fromstring(first)
    assert (root.get("version"), root.get("language"), root.get("sourcelanguage")) == ("2.1", "en", "vi")

    contexts = [c.findtext("name") for c in root.findall("context")]
    assert contexts == sorted(contexts) == ["Alpha", "Zeta", "beta", "Đích"]
    zeta = next(c for c in root.findall("context") if c.findtext("name") == "Zeta")
    sources = [m.findtext("source") for m in zeta.findall("message")]
    assert sources == sorted(sources) == ["B", "a", "b", "Ă"]

    locations = [m.locations for m in parse_ts(repo.ts)]
    assert all(locs for locs in locations), "mỗi message phải có <location>"
    for locs in locations:
        for loc in locs:
            assert set(loc) == {"filename"}, f"<location> chỉ có filename, không số dòng: {loc}"
            name = loc["filename"]
            assert "\\" not in name and not os.path.isabs(name) and ":" not in name, name
    names = {loc["filename"] for locs in locations for loc in locs}
    assert any(n.endswith("sub/a.py") for n in names), names
    assert any(n.endswith("z.py") for n in names), names


def test_output_independent_of_line_numbers(repo):
    """Không số dòng trong `<location>`: thêm dòng trống phía trên không đổi `.ts` (diff gọn)."""
    repo.write("a.py", HEADER + 'A = QCoreApplication.translate("Ctx", "Một")\n')
    repo.ok()
    before = repo.ts.read_bytes()
    repo.write("a.py", HEADER + "\n\n\n# chú thích\n" + 'A = QCoreApplication.translate("Ctx", "Một")\n')
    repo.ok()
    assert repo.ts.read_bytes() == before


# =================================================================================================
# --check
# =================================================================================================


def test_check_fails_when_stale_or_unfinished(repo):
    repo.write("a.py", HEADER + 'A = QCoreApplication.translate("Ctx", "Một")\n')
    result = repo.run("--check")
    assert result.returncode == 1, "chưa có .ts thì --check phải đỏ"
    assert not repo.ts.exists() and not repo.qm.exists(), "--check không được ghi"

    repo.ok()
    unfinished = repo.ts.read_bytes()
    assert repo.run("--check").returncode == 1, "còn unfinished thì --check phải đỏ"
    assert repo.ts.read_bytes() == unfinished

    fill_translations(repo.ts)
    repo.ok()
    done = repo.ts.read_bytes()
    qm_before = repo.qm.read_bytes()
    assert repo.run("--check").returncode == 0, output_of(repo.run("--check"))

    # Nguồn đổi (thêm chuỗi) mà .ts chưa cập nhật → đỏ, không ghi.
    repo.write(
        "a.py",
        HEADER
        + 'A = QCoreApplication.translate("Ctx", "Một")\nB = QCoreApplication.translate("Ctx", "Hai")\n',
    )
    assert repo.run("--check").returncode == 1
    assert repo.ts.read_bytes() == done
    assert repo.qm.read_bytes() == qm_before

    # Bản dịch rỗng (không đánh dấu unfinished) cũng đỏ.
    repo.write("a.py", HEADER + 'A = QCoreApplication.translate("Ctx", "Một")\n')
    tree = ET.parse(repo.ts)
    for tr in tree.getroot().iter("translation"):
        tr.text = ""
    tree.write(repo.ts, encoding="utf-8", xml_declaration=True)
    repo.ok()
    assert repo.run("--check").returncode == 1, "bản dịch rỗng thì --check phải đỏ"


def test_check_fails_when_numerus_form_empty(repo):
    repo.write("a.py", HEADER + 'A = QCoreApplication.translate("Ctx", "%n máy", None, 2)\n')
    repo.ok()
    fill_translations(repo.ts)
    repo.ok()
    assert repo.run("--check").returncode == 0
    tree = ET.parse(repo.ts)
    for form in tree.getroot().iter("numerusform"):
        form.text = ""
        break
    tree.write(repo.ts, encoding="utf-8", xml_declaration=True)
    repo.ok()
    assert repo.run("--check").returncode == 1


# =================================================================================================
# lrelease
# =================================================================================================


def _translator(qm: Path):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QTranslator

    tr = QTranslator()
    assert tr.load(str(qm)), f"không nạp được {qm}"
    return tr


TRICKY = 'Gửi & nhận <tệp> "x"\nDòng 2'


def test_release_builds_loadable_qm(repo):
    repo.write(
        "a.py",
        src(
            f"""
        def f(count):
            t = QCoreApplication.translate
            return (
                t("Ctx", "Ba"),
                t("Ctx", "Mở", "menu"),
                t("Ctx", {TRICKY!r}),
                t("Ctx", "%n máy", None, count),
            )
        """
        ),
    )
    repo.ok()
    assert repo.qm.is_file(), "chạy không --check phải sinh teleport_en.qm"
    fill_translations(repo.ts, {"Ba": "Three", "Mở": "Open", TRICKY: 'Send & receive <file> "x"\nLine 2'})
    repo.ok()
    tr = _translator(repo.qm)
    assert tr.translate("Ctx", "Ba") == "Three"
    assert tr.translate("Ctx", "Mở", "menu") == "Open"
    assert tr.translate("Ctx", TRICKY) == 'Send & receive <file> "x"\nLine 2', "thoát ký tự XML sai khóa"
    assert tr.translate("Ctx", "%n máy", None, 1) == "%n %n máy (một)"
    assert tr.translate("Ctx", "%n máy", None, 5) == "%n %n máy (nhiều)"


def test_release_works_without_lrelease_on_path(repo):
    """Mục 12 ý 11: lrelease gọi qua `sys.executable -c "from PySide6.scripts.pyside_tool import lrelease…"`,
    nên PATH không có `pyside6-lrelease` vẫn sinh được `.qm`."""
    repo.write("a.py", HEADER + 'A = QCoreApplication.translate("Ctx", "Ba")\n')
    env = {k: v for k, v in os.environ.items() if k.upper() != "PATH"}
    env["PATH"] = (
        os.environ.get("SYSTEMROOT", "") + os.sep + "System32" if os.name == "nt" else "/usr/bin:/bin"
    )
    result = repo.run(env=env)
    assert result.returncode == 0, output_of(result)
    assert repo.qm.is_file()
    _translator(repo.qm)
