"""Giữ đúng quy tắc phân tầng trong docs/architecture.md.

    core  ←  trust  ←  net  ←  app  ←  ui / cli

- Mỗi tầng chỉ được import các tầng **bên dưới** nó (và tiện ích dùng chung `teleport.paths`,
  `teleport.lock`).
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
SHARED = ("paths", "lock")

SANS_IO_FORBIDDEN = ("asyncio", "socket", "ssl", "selectors", "threading", "zeroconf", "PySide6")


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
