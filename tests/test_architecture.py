"""Giữ đúng quy tắc phân tầng trong docs/architecture.md.

core  ←  net  ←  clipboard  ←  ui / cli
Tầng dưới không bao giờ import tầng trên.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "teleport"

FORBIDDEN = {
    "core": ("teleport.net", "teleport.clipboard", "teleport.ui", "teleport.cli"),
    "net": ("teleport.clipboard", "teleport.ui", "teleport.cli"),
    "clipboard": ("teleport.ui", "teleport.cli"),
}


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


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_lower_layers_do_not_import_upper_layers(layer):
    violations = []
    for path in (PACKAGE / layer).rglob("*.py"):
        for name in imported_modules(path):
            if name.startswith(FORBIDDEN[layer]):
                violations.append(f"{path.relative_to(PACKAGE)} import {name}")
    assert not violations, "\n".join(violations)
