"""Trích chuỗi giao diện, cập nhật bản dịch `teleport_en.ts` và dịch ra `teleport_en.qm`.

Công cụ cho người phát triển, không vào wheel hay bản build. Chạy từ gốc repo:

    python packaging/i18n/update.py            # trích, gộp vào .ts, rồi lrelease ra .qm
    python packaging/i18n/update.py --check    # không ghi gì; thoát 1 nếu .ts lỗi thời hoặc còn chỗ chưa dịch

`--src DIR` (mặc định `src/teleport/ui`) là gốc mã nguồn cần trích (mọi `*.py` trong cây), `--ts FILE`
(mặc định `<src>/i18n/teleport_en.ts`) là file bản dịch; `.qm` nằm cạnh `.ts`, cùng tên. Test chạy
công cụ trên cây mã giả bằng hai tham số này.

**Trích bằng `ast`, không dùng `pyside6-lupdate`:** code giao diện gọi `QCoreApplication.translate`
qua bí danh (`t = QCoreApplication.translate`) và ghép ngầm literal nhiều dòng, lupdate bỏ sót. Được
trích: lời gọi `QCoreApplication.translate(...)` (cả `QGuiApplication`/`QApplication`), `translate(...)`
và tên được gán `= QCoreApplication.translate` ở phạm vi đang đứng hay phạm vi bao ngoài (hàm bao,
closure, lambda; như luật tên của Python, thân lớp không lộ tên cho phương thức). Ngữ cảnh, chuỗi
nguồn và disambiguation phải là literal (ghép ngầm hay bằng `+` đều được); disambiguation `None` là
không có. Có `n` (đối số thứ 4 hoặc `n=`) thì là chuỗi số đếm (`numerus`, viết `%n`). Đối số không
phải literal, hay lời gọi `.tr(...)` (ngữ cảnh phụ thuộc lớp lúc chạy, không trích tĩnh được) → báo
`file:dòng` và thoát 1, không ghi gì.

**Gộp:** khóa của một câu là `(ngữ cảnh, nguồn, disambiguation)`. Bản dịch cũ cùng khóa được giữ
(câu đang `unfinished` vẫn `unfinished`); câu mới là `type="unfinished"` với bản dịch rỗng; câu không
còn trong mã thì bỏ. File ghi theo TS 2.1 (`language="en" sourcelanguage="vi"`), UTF-8 không BOM,
xuống dòng `\\n`, ngữ cảnh rồi câu sắp theo code point, `<location>` chỉ có tên file (đường dẫn tương
đối với thư mục của `.ts`, dấu `/`), không số dòng: chạy lại trên cùng mã ra đúng từng byte.

**Dịch ra `.qm`:** gọi `lrelease` của PySide6 bằng chính trình thông dịch đang chạy
(`sys.executable -c "from PySide6.scripts.pyside_tool import lrelease; lrelease()"`), nên không phụ
thuộc PATH. Script này không import `teleport` hay PySide6.
"""

from __future__ import annotations

import argparse
import ast
import io
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SRC = REPO_ROOT / "src" / "teleport" / "ui"
TS_NAME = "teleport_en.ts"
LANGUAGE = "en"
SOURCE_LANGUAGE = "vi"
NUMERUS_FORMS = 2  # tiếng Anh: số ít, số nhiều

_TRANSLATE_OWNERS = ("QCoreApplication", "QGuiApplication", "QApplication")
_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_LRELEASE = "from PySide6.scripts.pyside_tool import lrelease; lrelease()"


class ExtractError(Exception):
    """Một chỗ trong mã không trích được (`file:dòng: lý do`)."""


@dataclass
class Message:
    """Một câu cần dịch. `comment` là disambiguation (`""` = không có)."""

    context: str
    source: str
    comment: str = ""
    numerus: bool = False
    locations: set[str] = field(default_factory=set)
    # Bản dịch: chuỗi, hoặc danh sách NUMERUS_FORMS dạng khi `numerus`.
    translation: str | list[str] = ""
    finished: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.context, self.source, self.comment)

    def is_done(self) -> bool:
        """Đã dịch xong: không `unfinished` và không dạng nào rỗng."""
        if not self.finished:
            return False
        if isinstance(self.translation, list):
            return all(self.translation)
        return bool(self.translation)


# ---- Trích -----------------------------------------------------------------------------------


def _is_translate_attr(node: ast.expr) -> bool:
    """`QCoreApplication.translate` (hay `QtCore.QCoreApplication.translate`…)."""
    if not isinstance(node, ast.Attribute) or node.attr != "translate":
        return False
    owner = node.value
    if isinstance(owner, ast.Name):
        return owner.id in _TRANSLATE_OWNERS
    return isinstance(owner, ast.Attribute) and owner.attr in _TRANSLATE_OWNERS


def _walk_scope(root: ast.AST) -> Iterator[ast.AST]:
    """Mọi nút thuộc phạm vi `root`; gặp phạm vi lồng thì trả nút đó nhưng không đi vào trong."""
    todo = list(ast.iter_child_nodes(root))
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, _SCOPES):
            todo.extend(ast.iter_child_nodes(node))


@dataclass
class _Scope:
    node: ast.AST
    aliases: set[str]  # tên được gán `= QCoreApplication.translate`
    bound: set[str]  # tên khác được gán trong phạm vi (che tên cùng tên ở ngoài)


def _scope_of(node: ast.AST, outer: list[_Scope]) -> _Scope:
    aliases: set[str] = set()
    bound: set[str] = set()
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg is not None:
                bound.add(arg.arg)
    for child in _walk_scope(node):
        if isinstance(child, ast.Assign) and all(isinstance(t, ast.Name) for t in child.targets):
            value = child.value
            is_alias = _is_translate_attr(value) or (
                isinstance(value, ast.Name) and _lookup(value.id, outer, aliases, bound)
            )
            if is_alias:
                aliases.update(t.id for t in child.targets if isinstance(t, ast.Name))
                continue
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store | ast.Del):
            bound.add(child.id)
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound.add(child.name)
        elif isinstance(child, ast.alias):
            bound.add((child.asname or child.name).split(".")[0])
    return _Scope(node, aliases, bound - aliases)


def _lookup(name: str, outer: list[_Scope], aliases: set[str], bound: set[str]) -> bool:
    """`name` ở phạm vi hiện tại (aliases/bound) có là `translate` không."""
    if name in aliases:
        return True
    if name in bound:
        return False
    for scope in reversed(outer):
        if isinstance(scope.node, ast.ClassDef):
            continue  # thân lớp không lộ tên cho phạm vi lồng trong nó
        if name in scope.aliases:
            return True
        if name in scope.bound:
            return False
    return name == "translate"


def _literal(node: ast.expr) -> str | None:
    """Chuỗi literal (ghép ngầm đã là một hằng; ghép bằng `+` cũng nhận); khác → None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


_ARG_NAMES: tuple[tuple[str, ...], ...] = (
    ("context",),
    ("key", "sourceText"),
    ("disambiguation",),
    ("n",),
)


def _call_args(call: ast.Call, where: str) -> list[ast.expr | None]:
    """Bốn đối số (context, source, disambiguation, n) theo vị trí hay tên; thiếu → None."""
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        raise ExtractError(f"{where}: không trích được lời gọi có *args/**kwargs")
    if len(call.args) > len(_ARG_NAMES):
        raise ExtractError(f"{where}: thừa đối số")
    result: list[ast.expr | None] = [None] * len(_ARG_NAMES)
    for i, arg in enumerate(call.args):
        result[i] = arg
    for keyword in call.keywords:
        for i, names in enumerate(_ARG_NAMES):
            if keyword.arg in names:
                if result[i] is not None:
                    raise ExtractError(f"{where}: đối số {keyword.arg} bị truyền hai lần")
                result[i] = keyword.value
                break
        else:
            raise ExtractError(f"{where}: đối số lạ {keyword.arg}")
    return result


def _message_from_call(call: ast.Call, where: str, location: str) -> Message:
    context_node, source_node, comment_node, n_node = _call_args(call, where)
    if context_node is None or source_node is None:
        raise ExtractError(f"{where}: thiếu ngữ cảnh hoặc chuỗi nguồn")
    context = _literal(context_node)
    if context is None:
        raise ExtractError(f"{where}: ngữ cảnh phải là chuỗi literal")
    source = _literal(source_node)
    if source is None:
        raise ExtractError(f"{where}: chuỗi nguồn phải là chuỗi literal")
    comment = ""
    if comment_node is not None and not (
        isinstance(comment_node, ast.Constant) and comment_node.value is None
    ):
        literal = _literal(comment_node)
        if literal is None:
            raise ExtractError(f"{where}: disambiguation phải là chuỗi literal hoặc None")
        comment = literal
    return Message(context, source, comment, numerus=n_node is not None, locations={location})


def extract_file(path: Path, location: str) -> list[Message]:
    """Các câu cần dịch trong một file `.py`; `location` là tên ghi vào `<location>`.

    Chỗ không trích được → ExtractError (gộp mọi chỗ của file, mỗi chỗ một dòng `file:dòng: …`).
    """
    tree = ast.parse(path.read_bytes(), filename=str(path))
    messages: list[Message] = []
    errors: list[tuple[int, str]] = []

    def visit(scope_node: ast.AST, outer: list[_Scope]) -> None:
        scope = _scope_of(scope_node, outer)
        stack = [*outer, scope]
        for node in _walk_scope(scope_node):
            if isinstance(node, _SCOPES):
                visit(node, stack)
                continue
            if not isinstance(node, ast.Call):
                continue
            where = f"{location}:{node.lineno}"
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "tr":
                errors.append((node.lineno, f"{where}: dùng QCoreApplication.translate thay cho .tr()"))
                continue
            is_translate = _is_translate_attr(func) or (
                isinstance(func, ast.Name) and _lookup(func.id, outer, scope.aliases, scope.bound)
            )
            if not is_translate:
                continue
            try:
                messages.append(_message_from_call(node, where, location))
            except ExtractError as e:
                errors.append((node.lineno, str(e)))

    visit(tree, [])
    if errors:
        raise ExtractError("\n".join(text for _, text in sorted(errors)))
    return messages


def extract(src_root: Path, ts_path: Path) -> dict[tuple[str, str, str], Message]:
    """Mọi câu trong `src_root/**/*.py`, theo khóa; `<location>` tương đối với thư mục của `ts_path`.

    Chỗ không trích được (mọi file) → ExtractError; cùng khóa mà chỗ có `n`, chỗ không → cũng vậy.
    """
    messages: dict[tuple[str, str, str], Message] = {}
    errors: list[str] = []
    files = sorted(src_root.rglob("*.py"), key=lambda p: p.relative_to(src_root).as_posix())
    for path in files:
        location = _relative(path, ts_path.parent, src_root)
        try:
            found = extract_file(path, location)
        except ExtractError as e:
            errors.append(str(e))
            continue
        except SyntaxError as e:
            errors.append(f"{location}:{e.lineno}: lỗi cú pháp")
            continue
        for message in found:
            old = messages.get(message.key)
            if old is None:
                messages[message.key] = message
            elif old.numerus != message.numerus:
                errors.append(f"{location}: câu {message.source!r} vừa có vừa không có n")
            else:
                old.locations |= message.locations
    if errors:
        raise ExtractError("\n".join(errors))
    return messages


def _relative(path: Path, base: Path, fallback: Path) -> str:
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:  # Windows: khác ổ đĩa
        return path.relative_to(fallback).as_posix()


# ---- Đọc / gộp / ghi .ts ----------------------------------------------------------------------


def read_ts(ts_path: Path) -> dict[tuple[str, str, str], Message]:
    """Bản dịch đang có trong `ts_path` (chưa có file → rỗng). Câu `vanished`/`obsolete` bị bỏ."""
    try:
        root = ET.parse(ts_path).getroot()
    except FileNotFoundError:
        return {}
    result: dict[tuple[str, str, str], Message] = {}
    for context in root.iter("context"):
        name = context.findtext("name") or ""
        for element in context.iter("message"):
            translation = element.find("translation")
            kind = translation.get("type") if translation is not None else "unfinished"
            if kind in ("vanished", "obsolete"):
                continue
            numerus = element.get("numerus") == "yes"
            text: str | list[str]
            if numerus:
                forms = translation.findall("numerusform") if translation is not None else []
                text = [form.text or "" for form in forms]
            else:
                text = (translation.text or "") if translation is not None else ""
            message = Message(
                name,
                element.findtext("source") or "",
                element.findtext("comment") or "",
                numerus=numerus,
                translation=text,
                finished=kind is None,
            )
            result[message.key] = message
    return result


def merge(
    extracted: dict[tuple[str, str, str], Message], old: dict[tuple[str, str, str], Message]
) -> list[Message]:
    """Câu đã trích kèm bản dịch cũ cùng khóa (cùng dạng số đếm), sắp theo ngữ cảnh, nguồn, comment."""
    result: list[Message] = []
    for key in sorted(extracted):
        message = extracted[key]
        previous = old.get(key)
        if previous is not None and previous.numerus == message.numerus:
            translation = previous.translation
            finished = previous.finished
            if isinstance(translation, list) and len(translation) != NUMERUS_FORMS:
                finished = finished and len(translation) > NUMERUS_FORMS
                translation = (translation + [""] * NUMERUS_FORMS)[:NUMERUS_FORMS]
            message.translation = translation
            message.finished = finished
        else:
            message.translation = [""] * NUMERUS_FORMS if message.numerus else ""
            message.finished = False
        result.append(message)
    return result


def _escape(text: str, attribute: bool = False) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#xd;")
    if attribute:
        text = text.replace('"', "&quot;").replace("\n", "&#xa;").replace("\t", "&#x9;")
    return text


def render_ts(messages: list[Message]) -> bytes:
    """Nội dung `.ts` (TS 2.1) của `messages` đã sắp; UTF-8 không BOM, xuống dòng `\\n`."""
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<!DOCTYPE TS>",
        f'<TS version="2.1" language="{LANGUAGE}" sourcelanguage="{SOURCE_LANGUAGE}">',
    ]
    context: str | None = None
    for message in messages:
        if message.context != context:
            if context is not None:
                lines.append("</context>")
            context = message.context
            lines += ["<context>", f"    <name>{_escape(context)}</name>"]
        numerus = ' numerus="yes"' if message.numerus else ""
        lines.append(f"    <message{numerus}>")
        lines += [
            f'        <location filename="{_escape(location, attribute=True)}"/>'
            for location in sorted(message.locations)
        ]
        lines.append(f"        <source>{_escape(message.source)}</source>")
        if message.comment:
            lines.append(f"        <comment>{_escape(message.comment)}</comment>")
        kind = "" if message.finished else ' type="unfinished"'
        if isinstance(message.translation, list):
            lines.append(f"        <translation{kind}>")
            lines += [
                f"            <numerusform>{_escape(form)}</numerusform>" for form in message.translation
            ]
            lines.append("        </translation>")
        else:
            lines.append(f"        <translation{kind}>{_escape(message.translation)}</translation>")
        lines.append("    </message>")
    if context is not None:
        lines.append("</context>")
    lines.append("</TS>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def build(src_root: Path, ts_path: Path) -> tuple[bytes, list[Message]]:
    """Trích `src_root`, gộp với `ts_path`; trả nội dung `.ts` mới và danh sách câu. Lỗi → ExtractError."""
    messages = merge(extract(src_root, ts_path), read_ts(ts_path))
    return render_ts(messages), messages


def release(ts_path: Path, qm_path: Path) -> None:
    """Dịch `ts_path` ra `qm_path` bằng lrelease của PySide6; thất bại → CalledProcessError."""
    subprocess.run(
        [sys.executable, "-c", _LRELEASE, str(ts_path), "-qm", str(qm_path)],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)


# ---- Dòng lệnh --------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Chạy công cụ; trả mã thoát (0 = ổn, 1 = lỗi trích, `.ts` lỗi thời/chưa dịch xong, lrelease lỗi)."""
    parser = argparse.ArgumentParser(description="Cập nhật bản dịch teleport_en.ts / .qm.")
    parser.add_argument("--check", action="store_true", help="không ghi; thoát 1 nếu .ts lỗi thời")
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC, help="gốc mã nguồn cần trích")
    parser.add_argument("--ts", type=Path, default=None, help="file .ts (mặc định <src>/i18n/teleport_en.ts)")
    args = parser.parse_args(argv)
    # Bảng mã mặc định của Windows (cp1252) không ghi được tiếng Việt khi stdout là pipe.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    src_root: Path = args.src
    ts_path: Path = args.ts if args.ts is not None else src_root / "i18n" / TS_NAME

    try:
        content, messages = build(src_root, ts_path)
    except ExtractError as e:
        print(e, file=sys.stderr)
        return 1

    if args.check:
        ok = True
        try:
            current = ts_path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != content:
            print(f"{ts_path}: lỗi thời, chạy lại packaging/i18n/update.py", file=sys.stderr)
            ok = False
        pending = [m for m in messages if not m.is_done()]
        for message in pending:
            print(f"chưa dịch xong: [{message.context}] {message.source!r}", file=sys.stderr)
        return 0 if ok and not pending else 1

    _write(ts_path, content)
    try:
        release(ts_path, ts_path.with_suffix(".qm"))
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"lrelease thất bại: {e}", file=sys.stderr)
        return 1
    unfinished = sum(not m.is_done() for m in messages)
    print(f"{ts_path}: {len(messages)} câu, {unfinished} câu chưa dịch xong")
    return 0


if __name__ == "__main__":
    sys.exit(main())
