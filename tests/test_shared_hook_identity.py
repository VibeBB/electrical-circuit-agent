"""Owner literals in canonical record hooks must survive repository normalization."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("filename", ["_records.py", "require_records.py"])
def test_canonical_hook_digest_preserves_all_owner_literals(tmp_path: Path, filename: str) -> None:
    checker = Path(__file__).resolve().parents[1] / "scripts/check_shared_hooks.py"
    spec = importlib.util.spec_from_file_location("shared_hook_checker", checker)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / filename
    path.write_text('OWNERS = {"ux", "bard", "wire"}\n', encoding="utf-8")
    original = module._digest(path, "ux")
    for plugin in (
        "ux",
        "bard",
        "circuit",
        "dashboard",
        "doc",
        "firmware",
        "fpga",
        "mech",
        "prodeng",
        "sim",
        "wire",
    ):
        assert module._digest(path, plugin) == original
    path.write_text('OWNERS = {\n    "ux", "bard", "wire",\n}\n', encoding="utf-8")
    assert module._digest(path, "bard") == original
    path.write_text('OWNERS = {"ux", "bard", "other"}\n', encoding="utf-8")
    assert module._digest(path, "ux") != original
