"""Bản dịch English của giao diện (spec D6 mục 2.7, test 5.6, quyết định 12.10 và 12.13).

Viết từ spec, không từ code:

- **`.ts` khớp nguồn.** Tập khóa `(ngữ cảnh, nguồn, disambiguation)` trích từ `src/teleport/ui/**/*.py`
  bằng bộ trích `ast` **riêng của test** (không import `packaging/i18n/update.py`, để lỗi của công cụ
  không tự xác nhận chính nó) phải bằng tập khóa trong `teleport_en.ts`; cờ numerus cũng phải khớp.
  Bộ trích nhận `QCoreApplication.translate(...)` (cả `QGuiApplication`/`QApplication`) và bí danh
  `t = QCoreApplication.translate` theo phạm vi lồng nhau (thân lớp không lộ tên cho phương thức).
  Một tham chiếu `….translate` mà không phải lời gọi hay phép gán bí danh (ví dụ truyền làm callback)
  là chỗ không trích tĩnh được: báo lỗi, không bỏ qua.
- **Không dở dang:** mọi `<translation>` không có thuộc tính `type` (`unfinished`, `vanished`,
  `obsolete`), không rỗng; numerus có đúng 2 `<numerusform>` không rỗng (tiếng Anh: số ít, số nhiều).
- **Placeholder:** tập `{tên}` của bản dịch (mọi dạng numerus) bằng tập của nguồn; `%n` có trong cả hai
  dạng numerus; nguồn có `%n` khi và chỉ khi lời gọi có `n` (mục 2.4: chuỗi đếm đổi sang `%n`).
- **Nguồn sạch:** không chuỗi được dịch nào còn `{n}`/`{count}`; không còn lời gọi `.tr(` (12.10).
- **`.qm` khớp `.ts` theo nội dung (12.13):** nạp `QTranslator`, với từng câu
  `translate(ctx, src, disamb, n)` trả đúng bản dịch (numerus: n=1 → dạng 1, n=2 → dạng 2).
- **Cửa sổ và khay English:** sau `i18n.set_language("en")` (trước khi dựng, như lúc khởi động; hay
  đổi lúc đang chạy) không widget nào của `MainWindow` (bắt đầu, đang khởi động, đang chạy, lỗi khởi
  động, cuộc trò chuyện, Tìm máy mới, mọi mục Cài đặt) và `Tray` còn chữ có dấu tiếng Việt, kể cả
  widget đang ẩn, tooltip, tên/mô tả trợ năng, placeholder, mục combo, dữ liệu của danh sách. Ngoại lệ
  duy nhất: tên tự gọi “Tiếng Việt” (mục 2.7 không dịch tên tự gọi). Dữ liệu của test (tên máy, đường
  dẫn) chỉ dùng chữ ASCII nên không cần ngoại lệ.
- **Không tràn chữ ở 720×460 (English):** cửa sổ giữ đúng 720×460 (không bị nội dung đẩy rộng ra),
  không vùng cuộn nào đang hiện có thanh cuộn ngang, và mọi nút/nhãn một dòng (`QLabel` đúng lớp, không
  xuống dòng) đang hiện có `sizeHint().width() ≤ width()` (chữ không bị cắt). Đo bằng bố cục Qt (không
  đo pixel ảnh) nên vững trên `offscreen`.
- **Cảnh** (`SCENES`) và **hộp thoại** (`DIALOGS`) dựng ở `tests/ui_scenes.py` (spec E1 mục 2.5), dùng
  chung với `tests/ui/test_a11y.py`. Hộp thoại (spec E1 mục 2.2): không chữ Việt như cửa sổ; ở cỡ của
  chính nó (sau `adjustSize`) rộng ≤ `WIDTH` và không nút/nhãn một dòng nào bị cắt (`_overflow` trừ
  điều kiện cỡ cửa sổ).
- **Quét tĩnh chữ Việt** (spec E1 mục 2.2): mọi chuỗi literal có chữ Việt trong `ui/**/*.py` chỉ nằm
  trong tham số của lời gọi dịch (trực tiếp hay bí danh, cùng bộ trích ở trên), docstring (và chuỗi đứng
  riêng làm câu lệnh), lời gọi log (`log.…`, `logger.…`, `logging.…`), exception sau `raise`, hay đúng
  “Tiếng Việt”. Chú thích không có trong `ast`. Miễn thêm, có lý do ở `_LOG_HELPERS`: hàm phụ chỉ ghi
  log, hàm dựng exception ở mọi chỗ, thông điệp `assert`. Miễn theo từng (file, chuỗi nguyên văn), không
  theo cả file: `VIETNAMESE_EXEMPT` (vĩnh viễn, có lý do) và `VIETNAMESE_KNOWN_GAPS` (mã E1c-n); mỗi mục
  phải vẫn còn trong nguồn (`test_vietnamese_exemptions_still_exist`).
- **`KNOWN_GAPS`** (spec E1 mục 2.5): chỉ cảnh mới, lỗi ở file của PR song song, `xfail(strict=True)` kèm
  mã việc sau: `FITS_KNOWN_GAPS` (tràn chữ) và `VIETNAMESE_KNOWN_GAPS` (quét tĩnh). Mục
  `FITS_KNOWN_GAPS` chỉ lộ ra khi chữ thân đo bằng font thay thế (spec E1 mục 2.6): nền `offscreen` và
  chữ thân không có font thật nào (`QFontInfo` trả family rỗng; tiến trình chưa nạp font điểm nhấn). Fixture
  autouse `_fits_known_gap_xfail` gắn `xfail(strict=True)` lúc chạy, chỉ trong trạng thái đó
  (`body_font_is_fallback`); ngoài nó (Windows thật, hay offscreen sau khi một test đã gọi
  `fonts.install()`) hai cảnh chạy như test thường và phải xanh. Đối chứng ở cuối file.
- `available_languages()` có `"en"`; `i18n.loadable("en")`; locale hệ điều hành `en_GB`/`en_US` → `en`;
  đổi vi → en → vi lúc chạy: chữ trên cửa sổ đổi rồi trở lại đúng như cũ.
- `ui.app._assets_ok()` bắt `.qm` hỏng: gói `teleport.ui` (bản chép trong `tmp_path`, giả qua
  `importlib.resources.files`) có `i18n/teleport_en.qm` là rác → False; bản chép nguyên vẹn → True
  (đối chứng: đỏ là do `.qm`, không do font hay icon).

Giả định: `.ts`/`.qm` nằm ở `<gói teleport.ui>/i18n/teleport_en.{ts,qm}` (mục 2.4); đường dẫn lấy từ
`importlib.util.find_spec("teleport.ui")`, tức đúng cây mã đang được test. Widget tự vẽ chữ trong
`paintEvent` mà không đặt `accessibleName` thì test không đọc được chữ đó (giới hạn đã biết).
"""

from __future__ import annotations

import ast
import importlib.util
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from ui_scenes import DIALOGS, SCENES

# Chữ cái có dấu của tiếng Việt (như `tests/ui/test_i18n_theme.py`).
VIETNAMESE = re.compile(
    r"[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]", re.IGNORECASE
)
PLACEHOLDER = re.compile(r"\{(\w+)\}")  # như `texts.fill`
ALLOWED_VIETNAMESE = {"Tiếng Việt"}  # tên tự gọi của ngôn ngữ nguồn: không dịch (mục 2.7)
# Từ tiếng Anh có dấu trùng chữ cái tiếng Việt (“café”): bỏ trước khi tìm chữ Việt.
ENGLISH_ACCENTED = re.compile(r"\bcaf[ée]s?\b", re.IGNORECASE)
WIDTH, HEIGHT = 720, 460
NUMERUS_FORMS = 2

Key = tuple[str, str, str]  # (ngữ cảnh, nguồn, disambiguation; "" = không có)


# =================================================================================================
# Đường dẫn
# =================================================================================================


def _ui_dir() -> Path:
    spec = importlib.util.find_spec("teleport.ui")
    assert spec is not None and spec.origin is not None
    return Path(spec.origin).resolve().parent


def _ts_path() -> Path:
    return _ui_dir() / "i18n" / "teleport_en.ts"


def _qm_path() -> Path:
    return _ui_dir() / "i18n" / "teleport_en.qm"


# =================================================================================================
# Bộ trích `ast` riêng của test
# =================================================================================================

_OWNERS = {"QCoreApplication", "QGuiApplication", "QApplication"}
_ARG_NAMES = (("context",), ("key", "sourceText"), ("disambiguation",), ("n",))
_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _is_translate_attr(node: ast.AST) -> bool:
    if not isinstance(node, ast.Attribute) or node.attr != "translate":
        return False
    owner = node.value
    if isinstance(owner, ast.Name):
        return owner.id in _OWNERS
    return isinstance(owner, ast.Attribute) and owner.attr in _OWNERS


def _literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _scope_nodes(scope: ast.AST):
    """Nút thuộc riêng phạm vi `scope` (gặp hàm/lớp/lambda lồng thì trả nút đó, không đi vào trong)."""
    todo = list(ast.iter_child_nodes(scope))
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, _SCOPE_TYPES):
            todo.extend(ast.iter_child_nodes(node))


@dataclass
class Extracted:
    messages: dict[Key, bool] = field(default_factory=dict)  # khóa → numerus
    locations: dict[Key, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    tr_calls: list[str] = field(default_factory=list)


Stack = list[tuple[ast.AST, set[str]]]


class _Visitor:
    def __init__(self, where: str, out: Extracted) -> None:
        self.where = where
        self.out = out
        self.handled: set[int] = set()  # id các nút `….translate` đã hiểu (gọi trực tiếp hoặc gán bí danh)

    @staticmethod
    def _visible(name: str, stack: Stack) -> bool:
        for i, (node, aliases) in enumerate(reversed(stack)):
            if i > 0 and isinstance(node, ast.ClassDef):
                continue  # thân lớp không lộ tên cho phạm vi lồng trong nó
            if name in aliases:
                return True
        return False

    def _aliases(self, scope: ast.AST, stack: Stack) -> set[str]:
        aliases: set[str] = set()
        for node in _scope_nodes(scope):
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            if value is None or not all(isinstance(t, ast.Name) for t in targets):
                continue
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if _is_translate_attr(value):
                self.handled.add(id(value))
                aliases |= names
            elif isinstance(value, ast.Name) and (
                value.id in aliases or self._visible(value.id, [*stack, (scope, aliases)])
            ):
                aliases |= names
        return aliases

    def visit_scope(self, scope: ast.AST, stack: Stack) -> None:
        stack = [*stack, (scope, self._aliases(scope, stack))]
        for node in _scope_nodes(scope):
            if isinstance(node, _SCOPE_TYPES):
                self.visit_scope(node, stack)
            elif isinstance(node, ast.Call):
                self._call(node, stack)

    def _call(self, call: ast.Call, stack: Stack) -> None:
        where = f"{self.where}:{call.lineno}"
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "tr":
            self.out.tr_calls.append(where)
            return
        if _is_translate_attr(func):
            self.handled.add(id(func))
        elif not (isinstance(func, ast.Name) and self._visible(func.id, stack)):
            return
        if len(call.args) > 4 or any(isinstance(a, ast.Starred) for a in call.args):
            self.out.errors.append(f"{where}: đối số không trích được")
            return
        args: list[ast.expr | None] = [None] * 4
        for i, arg in enumerate(call.args):
            args[i] = arg
        for kw in call.keywords:
            index = next((i for i, names in enumerate(_ARG_NAMES) if kw.arg in names), None)
            if index is None:
                self.out.errors.append(f"{where}: đối số lạ {kw.arg}")
                return
            args[index] = kw.value
        context, source = _literal(args[0]), _literal(args[1])
        if context is None or source is None:
            self.out.errors.append(f"{where}: ngữ cảnh/nguồn không phải literal")
            return
        comment = ""
        comment_node = args[2]
        if comment_node is not None and not (
            isinstance(comment_node, ast.Constant) and comment_node.value is None
        ):
            literal = _literal(comment_node)
            if literal is None:
                self.out.errors.append(f"{where}: disambiguation không phải literal")
                return
            comment = literal
        key = (context, source, comment)
        numerus = args[3] is not None
        old = self.out.messages.get(key)
        if old is not None and old != numerus:
            self.out.errors.append(f"{where}: {source!r} vừa có vừa không có n")
        self.out.messages[key] = numerus
        self.out.locations.setdefault(key, []).append(where)


def extract_ui_sources() -> Extracted:
    root = _ui_dir()
    out = Extracted()
    for path in sorted(root.rglob("*.py")):
        where = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_bytes(), filename=str(path))
        visitor = _Visitor(where, out)
        visitor.visit_scope(tree, [])
        for node in ast.walk(tree):
            if _is_translate_attr(node) and id(node) not in visitor.handled:
                out.errors.append(f"{where}:{node.lineno}: dùng translate theo cách không trích tĩnh được")
    return out


@pytest.fixture(scope="module")
def extracted() -> Extracted:
    return extract_ui_sources()


# =================================================================================================
# Đọc .ts
# =================================================================================================


@dataclass
class TsMessage:
    key: Key
    numerus: bool
    kind: str | None  # thuộc tính `type` của <translation> (None = đã dịch xong)
    text: str  # bản dịch (không numerus)
    forms: list[str]  # các <numerusform>
    has_translation: bool


def read_ts() -> tuple[ET.Element, list[TsMessage]]:
    path = _ts_path()
    if not path.is_file():
        pytest.fail(f"chưa có {path} (chờ code: sinh bằng packaging/i18n/update.py)")
    root = ET.parse(path).getroot()
    out: list[TsMessage] = []
    for context in root.iter("context"):
        name = context.findtext("name") or ""
        for message in context.iter("message"):
            translation = message.find("translation")
            forms = (
                [f.text or "" for f in translation.findall("numerusform")] if translation is not None else []
            )
            out.append(
                TsMessage(
                    key=(name, message.findtext("source") or "", message.findtext("comment") or ""),
                    numerus=message.get("numerus") == "yes",
                    kind=translation.get("type") if translation is not None else None,
                    text=(translation.text or "") if translation is not None else "",
                    forms=forms,
                    has_translation=translation is not None,
                )
            )
    return root, out


@pytest.fixture(scope="module")
def ts() -> list[TsMessage]:
    return read_ts()[1]


def _has_vietnamese(text: str) -> bool:
    return VIETNAMESE.search(ENGLISH_ACCENTED.sub("", text)) is not None


def _translations(m: TsMessage) -> list[str]:
    return m.forms if m.numerus else [m.text]


# =================================================================================================
# Bộ trích của test: tự kiểm
# =================================================================================================


def test_test_extractor_finds_known_strings(extracted):
    """Bộ trích của test thấy lời gọi trực tiếp, qua bí danh `t`, ở `Theme` (12.7) và `Tray` (12.10)."""
    keys = set(extracted.messages)
    assert ("Theme", "Bạc hà", "") in keys
    assert ("Texts", "Hãy dừng nó rồi bấm Thử lại.", "") in keys  # qua `t = QCoreApplication.translate`
    assert ("Tray", "Menu của Teleport", "") in keys
    assert len(keys) > 200, len(keys)


def test_test_extractor_alias_scoping():
    """Bí danh ở hàm bao ngoài, closure, lambda được nhận; bí danh trong thân lớp không lộ cho phương thức."""
    code = (
        "from PySide6.QtCore import QCoreApplication\n"
        "def outer():\n"
        "    t = QCoreApplication.translate\n"
        "    def inner():\n"
        "        return t('A', 'trong closure')\n"
        "    f = lambda: t('A', 'trong lambda', None, 3)\n"
        "    return inner, f\n"
        "class K:\n"
        "    t = QCoreApplication.translate\n"
        "    def m(self):\n"
        "        return t('A', 'không phải dịch')\n"
    )
    out = Extracted()
    _Visitor("x.py", out).visit_scope(ast.parse(code), [])
    assert out.messages == {("A", "trong closure", ""): False, ("A", "trong lambda", ""): True}


# =================================================================================================
# Nguồn sạch
# =================================================================================================


def test_ui_sources_have_no_tr_calls(extracted):
    """12.10: `.tr(` không trích tĩnh được; đợt 3 đổi hết sang `QCoreApplication.translate`."""
    assert extracted.tr_calls == []


def test_ui_translate_calls_are_all_extractable(extracted):
    """Mọi lời gọi dịch có ngữ cảnh/nguồn/disambiguation literal; không dùng `translate` kiểu lạ."""
    assert extracted.errors == []


def test_no_count_placeholder_left_in_translated_sources(extracted):
    """Mục 2.4: chuỗi đếm `{n}`/`{count}` đổi sang numerus `%n` (Q6)."""
    bad = [
        (key, extracted.locations[key])
        for key in extracted.messages
        if {"n", "count"} & set(PLACEHOLDER.findall(key[1]))
    ]
    assert bad == []


def test_percent_n_only_in_numerus_calls(extracted):
    """Nguồn có `%n` thì lời gọi phải truyền `n` (nếu không Qt để nguyên “%n”), và ngược lại."""
    bad = [
        (key, numerus, extracted.locations[key])
        for key, numerus in extracted.messages.items()
        if ("%n" in key[1]) != numerus
    ]
    assert bad == []


# ---- Quét tĩnh chữ Việt ngoài `translate` (spec E1 mục 2.2) ---------------------------------------

_LOG_OBJECTS = {"log", "logger", "_log", "logging"}
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
# Miễn thêm ngoài danh sách của spec (rủi ro mục 6: miễn mới phải có lý do):
# - hàm phụ chỉ đưa chuỗi vào `log.warning` (`settings._warn`, `TransfersController._log_error`): như lời
#   gọi log;
# - dựng exception thật (builtin hay `_NON_BUILTIN_EXCEPTIONS`, không theo đuôi tên) ở bất cứ đâu (không
#   chỉ sau `raise`: `RuntimeError(...)` truyền cho `on_done`): giao diện
#   không hiện `str(e)`, `ui/errorlog.py` chỉ ghi tên loại lỗi;
# - thông điệp của `assert`: chỉ hiện khi lỗi lập trình.
_LOG_HELPERS = {"_warn", "_log_error"}


# Lớp exception không phải builtin mà `ui` dựng (tên cụ thể, không theo đuôi tên: một hàm giao diện kiểu
# `setError("…")` không được miễn): `asyncio.CancelledError` / `concurrent.futures.CancelledError`.
_NON_BUILTIN_EXCEPTIONS = {"CancelledError"}


def _is_exception_name(name: str) -> bool:
    """Tên là exception builtin (lớp con `BaseException` resolve được từ `builtins`) hay trong
    `_NON_BUILTIN_EXCEPTIONS`."""
    import builtins

    obj = getattr(builtins, name, None)
    return (isinstance(obj, type) and issubclass(obj, BaseException)) or name in _NON_BUILTIN_EXCEPTIONS


def _is_exception_constructor(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    return _is_exception_name(name)


def _is_log_helper_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    if name in _LOG_HELPERS:
        return True
    # functools.partial(self._log_error, "…")
    return (
        name == "partial"
        and bool(node.args)
        and isinstance(node.args[0], ast.Attribute | ast.Name)
        and getattr(node.args[0], "attr", getattr(node.args[0], "id", "")) in _LOG_HELPERS
    )


class _TranslateCalls(_Visitor):
    """Bộ trích của test, ghi thêm nút lời gọi dịch nhận ra được (trực tiếp hay qua bí danh)."""

    def __init__(self, where: str, out: Extracted) -> None:
        super().__init__(where, out)
        self.calls: list[ast.Call] = []

    def _call(self, call: ast.Call, stack: Stack) -> None:
        func = call.func
        if _is_translate_attr(func) or (isinstance(func, ast.Name) and self._visible(func.id, stack)):
            self.calls.append(call)
        super()._call(call, stack)


def _is_log_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    owner = node.func.value
    return node.func.attr in _LOG_METHODS and isinstance(owner, ast.Name) and owner.id in _LOG_OBJECTS


def vietnamese_literal_nodes(tree: ast.AST) -> list[tuple[int, str]]:
    """(dòng, chuỗi) của mọi literal có chữ Việt **không** nằm ở chỗ được phép: tham số của lời gọi dịch;
    docstring (và chuỗi đứng riêng làm câu lệnh); lời gọi log; exception sau `raise`; đúng “Tiếng Việt”.
    Chú thích không có trong `ast` nên tự được bỏ qua. Chưa trừ các mục miễn theo chuỗi (dưới)."""
    visitor = _TranslateCalls("", Extracted())
    visitor.visit_scope(tree, [])
    allowed: set[int] = set()

    def allow(root: ast.AST) -> None:
        allowed.update(id(n) for n in ast.walk(root))

    for call in visitor.calls:
        for arg in [*call.args, *(kw.value for kw in call.keywords)]:
            allow(arg)
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            allow(node.value)  # docstring, chuỗi đứng riêng
        elif isinstance(node, ast.Raise) and node.exc is not None:
            allow(node.exc)
        elif _is_log_call(node) or _is_log_helper_call(node) or _is_exception_constructor(node):
            allow(node)
        elif isinstance(node, ast.Assert) and node.msg is not None:
            allow(node.msg)
    bad = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in allowed:
            continue
        if node.value in ALLOWED_VIETNAMESE or not _has_vietnamese(node.value):
            continue
        bad.append((node.lineno, node.value))
    return bad


def vietnamese_literals(where: str, tree: ast.AST) -> list[str]:
    """`vietnamese_literal_nodes` dạng `file:dòng: 'chuỗi'` để báo lỗi."""
    return [f"{where}:{line}: {text[:60]!r}" for line, text in vietnamese_literal_nodes(tree)]


# Miễn vĩnh viễn, theo (file trong `ui/`, chuỗi nguyên văn), không miễn cả file: chuỗi không bao giờ hiện
# trên giao diện.
_LOG_FILE_TEXT = "nội dung file log: câu nối chuỗi lỗi (như traceback của Python), không hiện trên giao diện"
VIETNAMESE_EXEMPT: dict[tuple[str, str], str] = {
    ("errorlog.py", "Lỗi trên là nguyên nhân trực tiếp của lỗi sau:"): _LOG_FILE_TEXT,
    ("errorlog.py", "Trong lúc xử lý lỗi trên thì lỗi sau xảy ra:"): _LOG_FILE_TEXT,
}

# KNOWN_GAPS của quét tĩnh (spec E1 mục 2.5): lỗi thật ở file thuộc PR song song, E1 không được sửa. Mỗi mục
# là (file, chuỗi nguyên văn) → mã việc sau. `test_vietnamese_exemptions_still_exist` bắt buộc mỗi chuỗi
# miễn vẫn còn bị quét bắt: sửa xong ở nguồn thì test đó đỏ, đòi xóa mục miễn (strict, không giấu lỗi).
_E1C_3 = (
    "E1c-3: help argparse của `teleport-gui --help` (console) chưa dịch; file chủ `ui/app.py` (PR song song)"
)
VIETNAMESE_KNOWN_GAPS: dict[tuple[str, str], str] = {
    ("app.py", "Teleport: gửi file trong mạng LAN."): _E1C_3,
    ("app.py", "thư mục cấu hình (mặc định: TELEPORT_HOME hoặc thư mục của hệ điều hành)"): _E1C_3,
    ("app.py", "chỉ hiện ở khay hệ thống (khi khởi động cùng máy)"): _E1C_3,
    ("app.py", "dựng giao diện rồi thoát (kiểm tra bản build)"): _E1C_3,
}


def _ui_vietnamese_literals() -> list[tuple[str, int, str]]:
    """(file trong `ui/`, dòng, chuỗi) quét được ở mọi `ui/**/*.py`, chưa trừ miễn."""
    root = _ui_dir()
    out: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_bytes(), filename=str(path))
        where = path.relative_to(root).as_posix()
        out += [(where, line, text) for line, text in vietnamese_literal_nodes(tree)]
    return out


def test_vietnamese_literal_scan_allows_only_listed_places():
    """Đối chứng cho test dưới: chuỗi Việt trong `setText("…")`, f-string, phép gán bị bắt; trong lời gọi
    dịch (trực tiếp, bí danh), docstring, log, `raise`, “Tiếng Việt” thì không."""
    code = (
        '"""Tài liệu của module."""\n'
        "import logging\n"
        "from PySide6.QtCore import QCoreApplication\n"
        "log = logging.getLogger(__name__)\n"
        "def f(label, n):\n"
        '    """Hàm có docstring tiếng Việt."""\n'
        "    t = QCoreApplication.translate\n"
        '    label.setText(QCoreApplication.translate("A", "Đã gửi"))\n'
        '    label.setText(t("A", "Đã nhận {n}").format(n=n))\n'
        '    log.warning("không mở được %s", n)\n'
        '    combo = ["Tiếng Việt", "English"]\n'
        "    if n < 0:\n"
        '        raise ValueError(f"số âm: {n}")\n'
        '    label.setText("Chưa dịch")\n'
        '    label.setToolTip(f"Còn {n} giây")\n'
        '    title = "Cài đặt"\n'
        '    host.call(f, _warn("lưu cài đặt"))\n'
        '    host.call(f, functools.partial(self._log_error, "hủy lượt"))\n'
        '    on_done(RuntimeError("chưa có node"))\n'
        '    assert n, "phải có số"\n'
        '    label.setText(str(n) + " giây")\n'
        "    return combo, title\n"
    )
    bad = vietnamese_literals("x.py", ast.parse(code))
    assert sorted({int(b.split(":", 2)[1]) for b in bad}) == [14, 15, 16, 21], bad


def test_exception_exemption_is_only_for_real_exception_classes():
    """Miễn “dựng exception” chỉ cho lớp exception thật: `RuntimeError(...)`, `asyncio.CancelledError(...)`;
    không cho hàm giao diện có tên tận cùng Error/Exception như `label.setError("…")`."""

    def call(src: str) -> ast.AST:
        return ast.parse(src, mode="eval").body

    assert _is_exception_constructor(call('RuntimeError("lỗi")'))
    assert _is_exception_constructor(call('asyncio.CancelledError("hủy")'))
    assert not _is_exception_constructor(call('self.name_field.setError("Hãy nhập tên")'))
    assert not _is_exception_constructor(call('showException("Lỗi")'))
    assert not _is_exception_constructor(call('MyDialogError("Lỗi")'))


def test_ui_sources_have_no_untranslated_vietnamese_literals():
    """Spec E1 mục 2.2: chuỗi literal có chữ Việt trong `ui/**/*.py` chỉ nằm ở chỗ được phép."""
    exempt = VIETNAMESE_EXEMPT.keys() | VIETNAMESE_KNOWN_GAPS.keys()
    bad = [
        f"{where}:{line}: {text[:60]!r}"
        for where, line, text in _ui_vietnamese_literals()
        if (where, text) not in exempt
    ]
    assert bad == []


def test_vietnamese_exemptions_still_exist():
    """Mỗi mục `VIETNAMESE_EXEMPT`/`VIETNAMESE_KNOWN_GAPS` vẫn đúng là một chuỗi quét bắt được ở đúng file
    đó. Đỏ ở đây = chuỗi đã sửa hay dời (vd. E1c-3 đã dịch): xóa mục miễn, đừng giữ chỗ miễn thừa."""
    found = {(where, text) for where, _line, text in _ui_vietnamese_literals()}
    stale = sorted((VIETNAMESE_EXEMPT.keys() | VIETNAMESE_KNOWN_GAPS.keys()) - found)
    assert stale == []
    assert not VIETNAMESE_EXEMPT.keys() & VIETNAMESE_KNOWN_GAPS.keys()
    assert all(reason.startswith("E1c-") for reason in VIETNAMESE_KNOWN_GAPS.values())


# =================================================================================================
# .ts
# =================================================================================================


def test_ts_header_is_ts21_english_from_vietnamese():
    root, _ = read_ts()
    assert root.tag == "TS"
    assert (root.get("version"), root.get("language"), root.get("sourcelanguage")) == ("2.1", "en", "vi")


def test_ts_keys_match_sources(extracted, ts):
    in_ts = [m.key for m in ts]
    assert len(in_ts) == len(set(in_ts)), "khóa trùng trong .ts"
    missing = sorted(set(extracted.messages) - set(in_ts))
    extra = sorted(set(in_ts) - set(extracted.messages))
    assert (missing, extra) == ([], []), "(thiếu trong .ts, thừa trong .ts): chạy lại update.py"


def test_ts_numerus_flag_matches_sources(extracted, ts):
    bad = [m.key for m in ts if m.key in extracted.messages and extracted.messages[m.key] != m.numerus]
    assert bad == []


def test_ts_has_no_unfinished_empty_vanished_or_obsolete(ts):
    bad = []
    for m in ts:
        if not m.has_translation:
            bad.append((m.key, "không có <translation>"))
        elif m.kind is not None:
            bad.append((m.key, f'type="{m.kind}"'))
        elif m.numerus:
            if len(m.forms) != NUMERUS_FORMS or not all(f.strip() for f in m.forms):
                bad.append((m.key, f"numerus {m.forms!r}"))
        elif not m.text.strip():
            bad.append((m.key, "rỗng"))
    assert bad == []


def test_ts_placeholders_preserved(ts):
    bad = []
    for m in ts:
        wanted = set(PLACEHOLDER.findall(m.key[1]))
        bad += [(m.key, text) for text in _translations(m) if set(PLACEHOLDER.findall(text)) != wanted]
    assert bad == []


def test_ts_numerus_forms_keep_percent_n(ts):
    numerus = [m for m in ts if m.numerus]
    assert numerus, "đợt 3 phải có chuỗi đếm %n (mục 2.4)"
    bad = [(m.key, m.forms) for m in numerus if "%n" not in m.key[1] or not all("%n" in f for f in m.forms)]
    assert bad == []


def test_ts_translations_have_no_vietnamese_letters(ts):
    bad = [(m.key, t) for m in ts for t in _translations(m) if _has_vietnamese(t)]
    assert bad == []


# =================================================================================================
# .qm
# =================================================================================================


def test_qm_matches_ts(qapp, ts):
    """12.13: so theo nội dung, không so byte."""
    from PySide6.QtCore import QTranslator

    path = _qm_path()
    assert path.is_file(), f"chưa có {path} (chờ code)"
    translator = QTranslator()
    assert translator.load(str(path))
    bad = []
    for m in ts:
        ctx, src, comment = m.key
        disamb = comment or None
        if m.numerus:
            got = [translator.translate(ctx, src, disamb, 1), translator.translate(ctx, src, disamb, 2)]
            if got != m.forms:
                bad.append((m.key, m.forms, got))
        else:
            one = translator.translate(ctx, src, disamb)
            if one != m.text:
                bad.append((m.key, m.text, one))
    assert bad == []
    assert translator.translate("KhongCo", "câu không có trong bản dịch") == ""


# =================================================================================================
# i18n: ngôn ngữ có sẵn, locale, nạp được
# =================================================================================================


@pytest.fixture
def back_to_source(qapp):
    """Test đổi ngôn ngữ: trả về chuỗi nguồn sau test (trạng thái cấp module của `i18n`)."""
    from teleport.ui import i18n

    yield i18n
    i18n.set_language(i18n.SOURCE_LANGUAGE)
    qapp.processEvents()


def test_available_languages_include_en(back_to_source):
    assert "en" in back_to_source.available_languages()


def test_en_qm_is_loadable(back_to_source):
    assert back_to_source.loadable("en")


@pytest.mark.parametrize("locale", ["en_GB", "en_US", "en"])
def test_english_system_locale_installs_en(back_to_source, monkeypatch, qapp, locale):
    """Q7: một `teleport_en` cho mọi `en_*`."""
    from PySide6.QtCore import QCoreApplication

    i18n = back_to_source
    monkeypatch.setattr(i18n, "system_language", lambda: locale)
    i18n.install(qapp, None)
    assert i18n.current_language() == "en"
    english = QCoreApplication.translate("Tray", "Menu của Teleport")
    assert english != "Menu của Teleport" and not VIETNAMESE.search(english)


def test_numerus_strings_render_count_in_english(qapp, back_to_source, ts):
    """Lúc chạy: `QCoreApplication.translate(..., n)` chọn đúng dạng số ít/số nhiều và thay `%n`."""
    from PySide6.QtCore import QCoreApplication

    _english(back_to_source, qapp)
    bad = []
    for m in (m for m in ts if m.numerus):
        ctx, src, comment = m.key
        for n, form in ((1, m.forms[0]), (2, m.forms[1]), (7, m.forms[1])):
            got = QCoreApplication.translate(ctx, src, comment or None, n)
            if got != form.replace("%n", str(n)):
                bad.append((m.key, n, got))
    assert bad == []


# =================================================================================================
# Cửa sổ và khay bằng English
# =================================================================================================


def _strings(root) -> list[tuple[str, str]]:
    """(chỗ, chữ) của mọi widget trong `root`, kể cả đang ẩn: chữ, tooltip, trợ năng, placeholder, combo,
    dữ liệu của danh sách, hành động (menu)."""
    from PySide6.QtCore import QModelIndex
    from PySide6.QtWidgets import (
        QAbstractButton,
        QAbstractItemView,
        QComboBox,
        QGroupBox,
        QLabel,
        QLineEdit,
        QPlainTextEdit,
        QTabWidget,
        QTextEdit,
        QWidget,
    )

    out: list[tuple[str, str]] = []

    def add(where: str, text: object) -> None:
        if isinstance(text, str) and text:
            out.append((where, text))

    roles = (
        Qt.ItemDataRole.DisplayRole,
        Qt.ItemDataRole.ToolTipRole,
        Qt.ItemDataRole.StatusTipRole,
        Qt.ItemDataRole.AccessibleTextRole,
        Qt.ItemDataRole.AccessibleDescriptionRole,
    )
    attrs = ("windowTitle", "toolTip", "statusTip", "whatsThis", "accessibleName", "accessibleDescription")
    for w in [root, *root.findChildren(QWidget)]:
        where = f"{type(w).__name__}({w.objectName()})"
        for attr in attrs:
            add(f"{where}.{attr}", getattr(w, attr)())
        if isinstance(w, QLabel | QAbstractButton):
            add(f"{where}.text", w.text())
        if isinstance(w, QLineEdit):
            add(f"{where}.text", w.text())
            add(f"{where}.placeholder", w.placeholderText())
        if isinstance(w, QTextEdit | QPlainTextEdit):
            add(f"{where}.text", w.toPlainText())
            add(f"{where}.placeholder", w.placeholderText())
        if isinstance(w, QComboBox):
            for i in range(w.count()):
                add(f"{where}.item{i}", w.itemText(i))
                add(f"{where}.item{i}.tooltip", w.itemData(i, Qt.ItemDataRole.ToolTipRole))
        if isinstance(w, QGroupBox):
            add(f"{where}.title", w.title())
        if isinstance(w, QTabWidget):
            for i in range(w.count()):
                add(f"{where}.tab{i}", w.tabText(i))
                add(f"{where}.tab{i}.tooltip", w.tabToolTip(i))
        if isinstance(w, QAbstractItemView) and w.model() is not None:
            model, top = w.model(), QModelIndex()
            try:
                columns = model.columnCount(top)
            except TypeError:  # QAbstractListModel: columnCount là private trong PySide6, luôn 1 cột
                columns = 1
            for row in range(model.rowCount(top)):
                for col in range(columns):
                    index = model.index(row, col)
                    for role in roles:
                        add(f"{where}[{row},{col}].{role.name}", model.data(index, role))
        for action in w.actions():
            add(f"{where}.action.text", action.text())
            add(f"{where}.action.toolTip", action.toolTip())
    return out


def _vietnamese(strings: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [
        (where, text) for where, text in strings if _has_vietnamese(text) and text not in ALLOWED_VIETNAMESE
    ]


def _english(i18n, qapp) -> None:
    i18n.set_language("en")
    for _ in range(3):
        qapp.processEvents()
    assert i18n.current_language() == "en", "không nạp được teleport_en.qm (chờ code)"


def _show(window, qapp, width: int = WIDTH, height: int = HEIGHT) -> None:
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.resize(width, height)
    window.show()
    for _ in range(3):  # hàng đổi bố cục theo bề ngang cần thêm lượt layout (như test_snapshots)
        qapp.processEvents()


# Cảnh mới của spec E1 mục 2.2 → widget (objectName) phải đang hiện sau khi hiện cửa sổ: cảnh dựng đúng
# trạng thái nó mang tên (không thì test English của cảnh đó xanh mà không kiểm gì).
NEW_SCENE_MARKERS = {
    "welcome_name_error": "firstRunNameError",
    "welcome_dir_error": "firstRunDirError",
    "welcome_no_autostart": "firstRunAutostartState",
    "wait_pairing": "waitPairingCopy",
    "wait_pairing_hidden": "waitPairingHidden",
    "settings_devices_empty": "trustedEmpty",
    "discover_notice": "discoverInfo",
    "chat_offer": "offerPanel",
    "chat_clipboard_note": "clipboardNote",
}


@pytest.mark.parametrize("scene", list(NEW_SCENE_MARKERS))
def test_new_scene_shows_its_state(qtbot, qapp, back_to_source, scene):
    from PySide6.QtWidgets import QWidget

    window = SCENES[scene](qtbot)
    _show(window, qapp)
    marked = window.findChildren(QWidget, NEW_SCENE_MARKERS[scene])
    assert any(w.isVisible() for w in marked), f"{scene}: không thấy {NEW_SCENE_MARKERS[scene]} đang hiện"
    if scene == "wait_pairing":
        assert not any(w.isVisible() for w in window.findChildren(QWidget, "waitPairingHidden"))


@pytest.mark.parametrize("scene", list(SCENES))
def test_scene_shows_vietnamese_before_english(qtbot, qapp, back_to_source, scene):
    """Đối chứng cho test dưới: ở chuỗi nguồn, bộ đọc chữ của test thấy chữ Việt ở mọi cảnh (không rỗng)."""
    window = SCENES[scene](qtbot)
    _show(window, qapp)
    assert _vietnamese(_strings(window))


@pytest.mark.parametrize("when", ["before_build", "live_switch"])
@pytest.mark.parametrize("scene", list(SCENES))
def test_english_window_shows_no_vietnamese(qtbot, qapp, back_to_source, scene, when):
    """Mục 2.7: mọi chuỗi qua `translate`, kể cả tooltip và trợ năng; đổi lúc chạy cũng dịch hết."""
    i18n = back_to_source
    if when == "before_build":
        _english(i18n, qapp)
    window = SCENES[scene](qtbot)
    _show(window, qapp)
    if when == "live_switch":
        _english(i18n, qapp)
    assert _vietnamese(_strings(window)) == []


@pytest.mark.parametrize("when", ["before_build", "live_switch"])
def test_english_tray_shows_no_vietnamese(qapp, back_to_source, when):
    from teleport.ui.tray import Tray

    i18n = back_to_source
    if when == "before_build":
        _english(i18n, qapp)
    tray = Tray()
    try:
        if when == "live_switch":
            _english(i18n, qapp)
        strings = _strings(tray.menu)
        strings += [(f"action{i}.text", a.text()) for i, a in enumerate(tray.actions())]
        strings += [(f"action{i}.toolTip", a.toolTip()) for i, a in enumerate(tray.actions())]
        strings.append(("icon.toolTip", tray._icon.toolTip()))
        assert all(a.text() for a in tray.actions())
        assert _vietnamese(strings) == []
    finally:
        tray.dispose()


def test_language_combo_keeps_vietnamese_endonym_in_english(qtbot, qapp, back_to_source):
    """Ngoại lệ duy nhất của test trên: ô ngôn ngữ vẫn có “Tiếng Việt” (không dịch) khi đang English."""
    from PySide6.QtWidgets import QComboBox

    _english(back_to_source, qapp)
    window = SCENES["settings_appearance"](qtbot)
    items = [c.itemText(i) for c in window.findChildren(QComboBox) for i in range(c.count())]
    assert "Tiếng Việt" in items and "English" in items


# ---- Tràn chữ ở 720×460 ----------------------------------------------------------------------


def _overflow(window) -> list[str]:
    from PySide6.QtWidgets import QAbstractButton, QAbstractScrollArea, QLabel

    problems: list[str] = []
    if (window.width(), window.height()) != (WIDTH, HEIGHT):
        problems.append(f"cửa sổ {window.width()}x{window.height()} (nội dung đẩy cửa sổ quá cỡ)")
    for area in window.findChildren(QAbstractScrollArea):
        bar = area.horizontalScrollBar()
        if area.isVisible() and bar.maximum() > 0:
            problems.append(f"{type(area).__name__}({area.objectName()}) cuộn ngang {bar.maximum()} px")
    for button in window.findChildren(QAbstractButton):
        if button.isVisible() and button.text() and button.sizeHint().width() > button.width():
            problems.append(
                f"nút {button.text()!r} cần {button.sizeHint().width()} px, có {button.width()} px"
            )
    for label in window.findChildren(QLabel):
        if type(label) is not QLabel or not label.isVisible() or not label.text() or label.wordWrap():
            continue  # nhãn tự cắt “…” (lớp con) hay xuống dòng: không cắt chữ
        if label.sizeHint().width() > label.width() + 1:
            problems.append(f"nhãn {label.text()!r} cần {label.sizeHint().width()} px, có {label.width()} px")
    return problems


def test_overflow_check_detects_clipped_button_and_label(qtbot, qapp):
    """Đối chứng cho test dưới: nút và nhãn bị ép hẹp hơn chữ thì `_overflow` báo."""
    from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

    window = QWidget()
    qtbot.addWidget(window)
    layout = QVBoxLayout(window)
    button = QPushButton("Choose receive folder and many more words")
    button.setFixedWidth(40)
    label = QLabel("A rather long single line label that cannot fit")
    label.setFixedWidth(40)
    layout.addWidget(button)
    layout.addWidget(label)
    _show(window, qapp)
    problems = _overflow(window)
    assert any(p.startswith("nút") for p in problems), problems
    assert any(p.startswith("nhãn") for p in problems), problems


# KNOWN_GAPS của `test_english_fits_720x460` (spec E1 mục 2.5): chỉ cảnh mới của E1, lỗi ở file thuộc PR
# song song (E1 không sửa). Cảnh → (objectName, kiểm nào, file chủ, mã việc sau). `xfail(strict=True)`: sửa
# xong thì test XPASS → đỏ, đòi xóa mục.
#
# Hai mục dưới chỉ lộ ra khi chữ thân đo bằng font thay thế (spec E1 mục 2.6): nền `offscreen` không có
# font hệ thống, và trước khi tiến trình nạp font điểm nhấn (`fonts.install`) thì chữ thân không có font
# thật nào, đo rộng gần gấp đôi. Sau khi một test bất kỳ đã nạp font điểm nhấn, chữ thân dùng font đó (cả
# sau `fonts._reset()`, Qt giữ kết quả chọn font) và hai cảnh vừa khung; trên `QT_QPA_PLATFORM=windows`
# (font thật) hai cảnh cũng vừa. Nên xfail gắn lúc chạy (`_fits_known_gap_xfail`) theo đúng trạng thái làm
# lỗi lộ ra (`body_font_is_fallback`), không gắn lúc thu thập; ngoài trạng thái đó hai cảnh là test thường
# và phải xanh. Vẫn strict trong trạng thái đó: hết tràn thì XPASS → đỏ, đòi xóa mục.
FITS_KNOWN_GAPS: dict[str, str] = {
    "chat_offer": (
        "E1c-1: chỉ khi chữ thân dùng font thay thế (offscreen, chưa nạp font điểm nhấn): ô `offerAlways` "
        "(OfferPanel) bị cắt chữ English (“Always accept from this device”); kiểm: nút/ô một dòng "
        "sizeHint ≤ width; file chủ `ui/chat_bubbles.py` (G4/F5); Windows thật xanh"
    ),
    "chat_clipboard_note": (
        "E1c-2: chỉ khi chữ thân dùng font thay thế (offscreen, chưa nạp font điểm nhấn): nhãn "
        "`clipboardNoteTitle` không xuống dòng nên luồng chat cuộn ngang ở English; kiểm: vùng cuộn không "
        "cuộn ngang; file chủ `ui/chat_bubbles.py` (G4/F5); Windows thật xanh"
    ),
}


def body_font_is_fallback() -> bool:
    """Trạng thái làm E1c-1/E1c-2 lộ ra, đọc từ trạng thái thật của Qt lúc chạy: nền `offscreen` và chữ
    thân (`QGuiApplication.font()`) không có font thật nào (`QFontInfo` trả family rỗng: offscreen trên
    Windows không thấy font hệ thống, và tiến trình chưa nạp font điểm nhấn). Offscreen trên Linux/macOS có
    font hệ thống thật nên không rơi vào đây: ở đó hai cảnh là test thường. Đo bằng `QFontInfo` (font Qt
    thật sự dùng để dàn chữ), không bằng cờ `fonts._installed`, vì `fonts._reset()` xóa cờ mà Qt vẫn giữ
    font đã chọn."""
    from PySide6.QtGui import QFontInfo, QGuiApplication

    if QGuiApplication.platformName() != "offscreen":
        return False
    return QFontInfo(QGuiApplication.font()).family() == ""


@pytest.fixture(autouse=True)
def _fits_known_gap_xfail(request):
    """Gắn `xfail(strict=True)` cho cảnh của `FITS_KNOWN_GAPS` trong `test_english_fits_720x460` khi
    `body_font_is_fallback()`; ngoài trạng thái đó để test chạy thật (phải PASS)."""
    if request.node.originalname != "test_english_fits_720x460":
        return
    scene = request.node.callspec.params.get("scene")
    if scene not in FITS_KNOWN_GAPS:
        return
    request.getfixturevalue("qapp")
    if body_font_is_fallback():
        request.node.add_marker(
            pytest.mark.xfail(raises=AssertionError, strict=True, reason=FITS_KNOWN_GAPS[scene])
        )


def _fits_params() -> list:
    return [pytest.param(name, id=name) for name in SCENES]


def test_fits_known_gaps_name_existing_new_scenes():
    """Mục `FITS_KNOWN_GAPS` chỉ cho cảnh có thật và là cảnh mới của E1 (spec E1: không miễn cảnh cũ)."""
    assert set(FITS_KNOWN_GAPS) <= set(SCENES)
    assert set(FITS_KNOWN_GAPS) <= set(NEW_SCENE_MARKERS)
    assert all(reason.startswith("E1c-") for reason in FITS_KNOWN_GAPS.values())


@pytest.mark.parametrize("scene", _fits_params())
def test_english_fits_720x460(qtbot, qapp, back_to_source, scene):
    """Rủi ro mục 8: English dài hơn; ở cỡ nhỏ nhất không cuộn ngang, không cắt chữ nút/nhãn."""
    _english(back_to_source, qapp)
    window = SCENES[scene](qtbot)
    _show(window, qapp)
    assert _overflow(window) == []


# ---- Hộp thoại (spec E1 mục 2.2) -------------------------------------------------------------


def _show_dialog(dialog, qapp) -> None:
    """Hộp ở cỡ của chính nó: hiện (không lên màn hình), `adjustSize`, cho bố cục chạy xong."""
    dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    dialog.show()
    for _ in range(3):
        qapp.processEvents()
    dialog.adjustSize()
    for _ in range(3):
        qapp.processEvents()


def _dialog_overflow(dialog) -> list[str]:
    """`_overflow` trừ điều kiện cỡ cửa sổ 720×460 (hộp có cỡ riêng), cộng bề ngang hộp ≤ `WIDTH`."""
    problems = [p for p in _overflow(dialog) if not p.startswith("cửa sổ ")]
    if dialog.width() > WIDTH:
        problems.append(f"hộp rộng {dialog.width()} px > {WIDTH} px")
    return problems


@pytest.mark.parametrize("dialog", list(DIALOGS))
def test_dialog_shows_vietnamese_before_english(qtbot, qapp, back_to_source, dialog):
    """Đối chứng: ở chuỗi nguồn, bộ đọc chữ thấy chữ Việt ở mọi hộp."""
    box = DIALOGS[dialog](qtbot)
    _show_dialog(box, qapp)
    assert _vietnamese(_strings(box))


@pytest.mark.parametrize("when", ["before_build", "live_switch"])
@pytest.mark.parametrize("dialog", list(DIALOGS))
def test_english_dialog_shows_no_vietnamese(qtbot, qapp, back_to_source, dialog, when):
    """Spec E1 mục 2.2: hộp thoại English không còn chữ Việt, dựng sau khi đổi hay đổi lúc đang mở."""
    i18n = back_to_source
    if when == "before_build":
        _english(i18n, qapp)
    box = DIALOGS[dialog](qtbot)
    _show_dialog(box, qapp)
    if when == "live_switch":
        _english(i18n, qapp)
    assert _vietnamese(_strings(box)) == []


def test_dialog_overflow_check_ignores_window_size_but_not_clipping(qtbot, qapp):
    """Đối chứng cho test dưới: hộp nhỏ hơn 720×460 không bị báo; nút bị ép hẹp vẫn bị báo."""
    from PySide6.QtWidgets import QDialog, QPushButton, QVBoxLayout

    box = QDialog()
    qtbot.addWidget(box)
    layout = QVBoxLayout(box)
    layout.addWidget(QPushButton("OK"))
    _show_dialog(box, qapp)
    assert _dialog_overflow(box) == []
    clipped = QPushButton("Choose receive folder and many more words")
    clipped.setFixedWidth(40)
    layout.addWidget(clipped)
    for _ in range(3):
        qapp.processEvents()
    assert any(p.startswith("nút") for p in _dialog_overflow(box))


@pytest.mark.parametrize("dialog", list(DIALOGS))
def test_english_dialog_fits_its_own_size(qtbot, qapp, back_to_source, dialog):
    """Spec E1 mục 2.2: ở cỡ của chính nó (sau `adjustSize`) hộp rộng ≤ 720 px, không cắt chữ nút/nhãn."""
    _english(back_to_source, qapp)
    box = DIALOGS[dialog](qtbot)
    _show_dialog(box, qapp)
    assert _dialog_overflow(box) == []


# ---- Đổi lúc chạy ----------------------------------------------------------------------------


def test_switch_vi_en_vi_at_runtime_changes_and_restores_texts(qtbot, qapp, back_to_source):
    i18n = back_to_source
    window = SCENES["settings_general"](qtbot)
    _show(window, qapp, 980, 660)
    vietnamese = _strings(window)
    assert _vietnamese(vietnamese), "trước khi đổi phải đang hiện tiếng Việt"
    _english(i18n, qapp)
    english = _strings(window)
    assert english != vietnamese
    assert _vietnamese(english) == []
    i18n.set_language("vi")
    for _ in range(3):
        qapp.processEvents()
    assert i18n.current_language() == "vi"
    assert _not_elided(_strings(window)) == _not_elided(vietnamese)


def _not_elided(strings: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Bỏ chữ của nhãn tự cắt “…” (lớp tên có `Elided`): chữ đó theo bề ngang lúc vẽ, không theo bản dịch."""
    return [(w, t) for w, t in strings if not ("Elided" in w.split("(", 1)[0] and w.endswith(".text"))]


# =================================================================================================
# `_assets_ok` của `--selftest`
# =================================================================================================


@pytest.fixture
def ui_package_copy(monkeypatch, tmp_path):
    """Bản chép gói `teleport.ui` (tài sản + bản dịch) trong `tmp_path`, trả qua `resources.files`."""
    from importlib import resources

    root = tmp_path / "ui"
    shutil.copytree(_ui_dir(), root, ignore=shutil.ignore_patterns("__pycache__"))
    real_files = resources.files

    def files(package):
        if package == "teleport.ui":
            return root
        return real_files(package)

    monkeypatch.setattr(resources, "files", files)
    return root


def test_assets_ok_with_intact_translation(qapp, ui_package_copy, back_to_source):
    """Đối chứng cho test dưới: cùng bản chép, `.qm` nguyên vẹn → True."""
    from teleport.ui import app as ui_app

    assert (ui_package_copy / "i18n" / "teleport_en.qm").is_file(), "chưa có teleport_en.qm (chờ code)"
    assert "en" in back_to_source.available_languages()
    assert ui_app._assets_ok() is True


@pytest.mark.parametrize(
    "garbage",
    [b"", b"day khong phai file qm " * 8, b"\x3c\xb8\x64\x18" + b"\xff" * 64],
    ids=["empty", "text", "bad_body"],
)
def test_assets_ok_rejects_broken_qm(qapp, ui_package_copy, back_to_source, garbage):
    """2.6: `_assets_ok` nạp mọi mã ≠ vi trong `available_languages()`; `.qm` hỏng → False."""
    from teleport.ui import app as ui_app

    qm = ui_package_copy / "i18n" / "teleport_en.qm"
    qm.parent.mkdir(exist_ok=True)
    qm.write_bytes(garbage)
    i18n = back_to_source
    assert "en" in i18n.available_languages()
    assert i18n.loadable("en") is False
    assert ui_app._assets_ok() is False


# Đặt cuối file: test này nạp font điểm nhấn (đổi font chữ thân của cả tiến trình), để theo thứ tự file
# các test 720×460 ở trên vẫn đo trong trạng thái chưa nạp.
def test_fits_known_gap_condition_follows_real_font_state(qapp):
    """Đối chứng của điều kiện xfail: (1) nền không phải `offscreen` → luôn False; (2) trên `offscreen`,
    khi điều kiện là True thì chữ mẫu của E1c-1 đo bằng font chữ thân rộng hơn hẳn khi đo bằng font điểm
    nhấn (đúng là font thay thế); sau `fonts.install()` điều kiện là False, và vẫn False sau
    `fonts._reset()` (Qt giữ font đã chọn, hai cảnh vừa khung, nên phải chạy như test thường)."""
    from PySide6.QtGui import QFont, QFontMetrics, QGuiApplication

    from teleport.ui import fonts, theme

    if QGuiApplication.platformName() != "offscreen":
        assert body_font_is_fallback() is False
        return
    sample = "Always accept from this device"
    if body_font_is_fallback():
        body = QFontMetrics(QGuiApplication.font()).horizontalAdvance(sample)
        accent = QFont(QGuiApplication.font())
        was_installed = fonts._installed
        fonts.install()
        accent.setFamilies([theme.ACCENT_FAMILY])
        assert body > QFontMetrics(accent).horizontalAdvance(sample), (
            "điều kiện True mà chữ thân không rộng hơn"
        )
        if not was_installed:
            fonts._reset()
    fonts.install()
    assert body_font_is_fallback() is False
    fonts._reset()
    assert body_font_is_fallback() is False
