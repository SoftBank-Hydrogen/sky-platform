"""Keep the pure Sky layers independent of runtime integrations."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
ALLOWED_INTERNAL = {
    "domain": set(),
    "engine": {"domain"},
    "ports": {"domain"},
}
INTERNAL = ALLOWED_INTERNAL.keys() | {"application", "adapters", "interfaces", "assets"}


def test_pure_layer_imports_point_inward():
    for layer, allowed in ALLOWED_INTERNAL.items():
        for source in (SRC / layer).rglob("*.py"):
            tree = ast.parse(source.read_text())
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imports.append(node.module)
            forbidden = {
                name.split(".", 1)[0]
                for name in imports
                if name.split(".", 1)[0] in INTERNAL - allowed - {layer}
            }
            assert not forbidden, f"{source.relative_to(SRC)} imports {sorted(forbidden)}"
