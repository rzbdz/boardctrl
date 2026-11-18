#!/usr/bin/env python3
"""Check the layers of this repository against `architecture.json`.

    python3 scripts/check-architecture.py

Every Python file belongs to a layer by its top-level directory (`core/`, `providers/<name>/`, `frontends/`,
`platforms/`, `hooks/`, `tests/`). A file's imports must stay inside what its layer may import: the kernel knows no
provider and no frontend, a provider reaches other providers only through `ctx.service()`, and a frontend only
renders what the kernel offers. What is not a layer's business is also listed as forbidden (FastAPI in the kernel).

`tests/` may import anything: it stands in for every layer at once.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYERS = {"core", "providers", "frontends", "platforms", "hooks"}


def layer_of(path: Path) -> str | None:
    """The layer a file belongs to: `core`, `providers/<name>`, `frontends`, or None for data and tests."""
    rel = path.relative_to(ROOT)
    if not rel.parts:
        return None
    top = rel.parts[0]
    if top == "providers" and len(rel.parts) > 1:
        return f"providers/{rel.parts[1]}"
    if top in ("core", "frontends", "platforms", "hooks"):
        return top
    return None


def imports_of(path: Path) -> set[str]:
    """The top-level modules a file imports (`core`, `providers.gpio`, `fastapi`, ...)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as e:
        print(f"{path}: cannot read ({e})", file=sys.stderr)
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:                       # relative: the file's own package
                continue
            if node.module:
                out.add(node.module.split(".")[0])
    return out


def allowed(layer: str, rule: dict) -> set[str]:
    """The top-level names a layer may import, from `may_import` (`providers/<name>` is itself and `core`)."""
    out: set[str] = set()
    for entry in rule.get("may_import", []):
        if entry == "providers/<name>":
            out.add("providers")
        elif "<" in entry:
            continue
        else:
            out.add(entry.split("/")[0])
    return out


def main() -> int:
    rules = json.loads((ROOT / "architecture.json").read_text(encoding="utf-8"))
    by_from = {r["from"]: r for r in rules["rules"]}
    forbidden = rules.get("forbidden", [])
    problems: list[str] = []

    for path in sorted(ROOT.rglob("*.py")):
        if any(part in (".git", "__pycache__", "node_modules", "serial-mux", "tests", "dist") for part in path.parts):
            continue
        layer = layer_of(path)
        if layer is None:
            continue
        rule = by_from.get(layer) or by_from.get("providers/<name>") if layer.startswith("providers/") else by_from.get(layer)
        if rule is None:
            continue
        permit = allowed(layer, rule)
        for name in sorted(imports_of(path)):
            if name in LAYERS or name in ("providers", "frontends", "core", "platforms", "hooks"):
                pass                  # ours: checked below
            else:
                continue              # the standard library and third-party packages are nobody's layer
            if name in ("__future__",):
                continue
            rel = path.relative_to(ROOT)
            if name in permit:
                # A provider reaches another provider only through the loader, never its files.
                if name == "providers" and layer.startswith("providers/"):
                    for node_top in _provider_imports(path):
                        other = node_top.split(".")[1] if "." in node_top else None
                        if other and other != layer.split("/")[1]:
                            problems.append(f"{rel}: provider {layer} imports providers.{other}'s files "
                                            f"(use ctx.service() instead)")
                continue
            problems.append(f"{rel}: layer {layer} imports '{name}' "
                            f"(may import: {', '.join(sorted(permit))}) {rule.get('why', '')}")

    for path in sorted(ROOT.rglob("*.py")):
        if any(part in (".git", "__pycache__", "node_modules", "serial-mux", "tests", "dist") for part in path.parts):
            continue
        layer = layer_of(path)
        if layer is None:
            continue
        names = _module_names(path)
        for ban in forbidden:
            source = ban["from"]
            if not (layer == source or layer.startswith(source + "/")):
                continue
            for bad in ban["imports"]:
                if any(n == bad or n.startswith(bad + ".") for n in names):
                    problems.append(f"{path.relative_to(ROOT)}: layer {layer} imports '{bad}' — {ban['why']}")

    for p in problems:
        print(p)
    print(f"architecture: {len(problems)} problem(s)" if problems else "architecture: ok")
    return 1 if problems else 0


def _provider_imports(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("providers."):
            out.append(node.module)
        elif isinstance(node, ast.Import):
            out += [a.name for a in node.names if a.name.startswith("providers.")]
    return out


def _module_names(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.append(node.module)
    return out


if __name__ == "__main__":
    sys.exit(main())
