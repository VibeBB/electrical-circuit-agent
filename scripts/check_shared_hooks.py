from __future__ import annotations

import ast
import hashlib
import re
import sys
from pathlib import Path

_EXPECTED_ENSURE_LLM_PROFILES_NORMALIZED_AST_SHA256 = (
    "e8eb58bf540e432be683e737a913a97e84b7f2c2e20daddf27cb6fec42316c79"
)
_EXPECTED_PROVENANCE_NORMALIZED_AST_SHA256 = (
    "129bc2a98d85c300026ef50dabe90940c4b3c0e7054fb02d52d1d1dee3b18672"
)
_EXPECTED_SAFETY_RAIL_NORMALIZED_AST_SHA256 = (
    "1a9f3f72fec383f046db2ca8805a7190c33daa86daf42f06c3cd27e3f8be245b"
)
EXPECTED: dict[str, str] = {
    "ensure_llm_profiles.py": _EXPECTED_ENSURE_LLM_PROFILES_NORMALIZED_AST_SHA256,
    "_provenance.py": _EXPECTED_PROVENANCE_NORMALIZED_AST_SHA256,
    "safety_rail.py": _EXPECTED_SAFETY_RAIL_NORMALIZED_AST_SHA256,
}
REQUIRED = frozenset({"ensure_llm_profiles.py", "safety_rail.py"})
_DOCSTRING_NODE_TYPES = (
    ast.Module,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)


def _strip_docstrings(node: ast.AST) -> None:
    if (
        isinstance(node, _DOCSTRING_NODE_TYPES)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        node.body = node.body[1:]
    for child in ast.iter_child_nodes(node):
        _strip_docstrings(child)


def _digest(path: Path, plugin_name: str) -> str:
    source = path.read_text(encoding="utf-8")
    source = re.sub(rf"\b{re.escape(plugin_name)}\b", "PLUGIN", source)
    if plugin_name == "ux":
        source = re.sub(r"\bux_creator\b", "PLUGIN", source)
    tree = ast.parse(source, filename=str(path))
    _strip_docstrings(tree)
    normalized = ast.dump(tree, include_attributes=False).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    hook_paths = (root / "plugins").glob("*/hooks/scripts")
    hook_dirs = sorted(path for path in hook_paths if path.is_dir())
    if len(hook_dirs) != 1:
        print("expected one plugins/<name>/hooks/scripts directory", file=sys.stderr)
        return 1

    hooks_dir = hook_dirs[0]
    plugin_name = hooks_dir.parent.parent.name
    failures: list[str] = []
    for filename, expected in EXPECTED.items():
        path = hooks_dir / filename
        if not path.is_file():
            if filename in REQUIRED:
                failures.append(f"missing required shared hook: {path}")
            continue
        try:
            actual = _digest(path, plugin_name)
        except (OSError, UnicodeError, SyntaxError) as exc:
            failures.append(f"could not normalize {path}: {exc}")
            continue
        if actual != expected:
            failures.append(
                f"{filename}: expected {expected}, got {actual}; compare with "
                "the wire canonical and update all copies and EXPECTED together"
            )

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("shared hooks match canonical AST hashes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
