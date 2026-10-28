"""Giữ đúng quy tắc phân tầng trong docs/architecture.md.

    core  ←  trust  ←  net  ←  app  ←  ui / cli

- Mỗi tầng chỉ được import các tầng **bên dưới** nó (và tiện ích dùng chung `teleport.paths`,
  `teleport.lock`, `teleport.atomic`).
- `core` là sans-IO: không import thư viện mạng/bất đồng bộ, không import tầng nào khác.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "teleport"

# Thứ tự từ dưới lên. `ui` và `cli` ngang hàng ở trên cùng.
LAYERS = ["core", "trust", "net", "app", "ui", "cli"]
TOP = {"ui", "cli"}

# Tiện ích dùng chung: tầng nào cũng import được (trừ core), và chúng không import tầng nào.
SHARED = ("paths", "lock", "atomic")

SANS_IO_FORBIDDEN = (
    "asyncio",
    "socket",
    "ssl",
    "selectors",
    "threading",
    "zeroconf",
    "ifaddr",
    "PySide6",
)


def layer_of(module: str) -> str | None:
    parts = module.split(".")
    if parts[0] != "teleport" or len(parts) < 2:
        return None
    return parts[1] if parts[1] in LAYERS else None


def allowed_layers(layer: str) -> set[str]:
    below = LAYERS[: LAYERS.index(layer)]
    return {*below, layer} - (TOP - {layer})


def source_files(layer: str) -> list[Path]:
    if layer == "cli":
        return [PACKAGE / "cli.py", PACKAGE / "__main__.py"]
    return sorted((PACKAGE / layer).rglob("*.py"))


def imported_modules(path: Path) -> set[str]:
    """Tên đầy đủ của mọi module được import, kể cả import tương đối (`from ..ui import x`)."""
    package = ["teleport", *path.relative_to(PACKAGE).parts[:-1]]
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            if node.module:
                names.add(".".join([*base, node.module]))
            else:
                names.update(".".join([*base, alias.name]) for alias in node.names)
    return names


def test_every_top_level_module_belongs_to_a_layer():
    known = {*LAYERS, "__init__", "__main__", *SHARED}
    found = {p.stem for p in PACKAGE.iterdir() if p.suffix == ".py" or (p / "__init__.py").exists()}
    assert found <= known, f"module/gói chưa được xếp tầng: {sorted(found - known)}"


@pytest.mark.parametrize("layer", LAYERS)
def test_layer_imports_only_lower_layers(layer):
    allowed = allowed_layers(layer)
    violations = []
    for path in source_files(layer):
        for name in imported_modules(path):
            target = layer_of(name)
            if target is not None and target not in allowed:
                violations.append(f"{path.relative_to(PACKAGE)} import {name}")
    assert not violations, "\n".join(violations)


def test_core_is_sans_io():
    violations = []
    for path in source_files("core"):
        for name in imported_modules(path):
            if name.split(".")[0] in SANS_IO_FORBIDDEN or name in {f"teleport.{m}" for m in SHARED}:
                violations.append(f"{path.relative_to(PACKAGE)} import {name}")
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize("module", SHARED)
def test_shared_utility_imports_no_layer(module):
    names = imported_modules(PACKAGE / f"{module}.py")
    assert not [n for n in names if layer_of(n)], names


# P4-3 (spec-p4-3 mục 6.1): chỉ `ui` được dùng Qt. CLI và mọi tầng dưới phải chạy được khi chưa cài
# extra `gui`, nên không file nào ngoài `teleport/ui/` được import `PySide6` (kể cả `__init__.py`).
NO_QT = ("core", "trust", "net", "app", "cli")


def _non_ui_files(owner: str) -> list[Path]:
    if owner in SHARED:
        return [PACKAGE / f"{owner}.py"]
    if owner == "package":
        return [PACKAGE / "__init__.py", PACKAGE / "__main__.py"]
    return source_files(owner)


@pytest.mark.parametrize("owner", [*NO_QT, *SHARED, "package"])
def test_only_ui_imports_pyside6(owner):
    files = _non_ui_files(owner)
    assert files, owner
    violations = [
        f"{path.relative_to(PACKAGE)} import {name}"
        for path in files
        for name in imported_modules(path)
        if name.split(".")[0] == "PySide6"
    ]
    assert not violations, "\n".join(violations)


def test_pyside6_import_check_sees_relative_and_from_imports(tmp_path, monkeypatch):
    """Bộ dò bắt được cả `import PySide6.QtCore` lẫn `from PySide6 import QtWidgets` (không thì test trên
    xanh rỗng)."""
    fake_pkg = tmp_path / "teleport"
    (fake_pkg / "app").mkdir(parents=True)
    probe = fake_pkg / "app" / "probe.py"
    probe.write_text("import PySide6.QtCore\nfrom PySide6 import QtWidgets\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "PACKAGE", fake_pkg)
    names = imported_modules(probe)
    assert {"PySide6.QtCore", "PySide6"} <= names
    assert {n.split(".")[0] for n in names} == {"PySide6"}


def test_ui_does_not_import_cli():
    """spec-p4-3 mục 6.1: `ui` và `cli` ngang hàng; `ui` không import `cli` (câu chữ của `ui` nằm ở
    `ui/texts.py`). `test_layer_imports_only_lower_layers[ui]` cũng kiểm, ở đây nói rõ và không xanh rỗng
    khi `ui` chưa có file."""
    files = source_files("ui")
    if not files:
        pytest.skip("chưa có src/teleport/ui (P4-3 đang làm)")
    violations = [
        f"{path.relative_to(PACKAGE)} import {name}"
        for path in files
        for name in imported_modules(path)
        if name == "teleport.cli" or name.startswith("teleport.cli.") or name == "teleport.__main__"
    ]
    assert not violations, "\n".join(violations)
