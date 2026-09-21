import json
from pathlib import Path

from scripts.render_konnect_tools import render

ROOT = Path(__file__).parents[1]
MATRIX_PATH = ROOT / "plugins/circuit/skills/circuit-konnect/references/konnect-tools.json"
FIXTURE_PATH = ROOT / "tests/data/konnect_tools_v0.12.1.json"
DOC_PATH = ROOT / "docs/konnect-tools.md"
ROLES = {
    "authoring",
    "authoring_conditional",
    "advisory",
    "export_comparison",
    "library",
    "lifecycle",
    "excluded",
}
STAGES = {"intake", "schematic", "layout", "review", "manufacturing", "any", "none"}
IPC_MODES = {"required", "optional", "file", "unknown"}


def test_matrix_covers_every_tool_once() -> None:
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    names = [item["tool"] for item in matrix]

    assert len(names) == len(set(names))
    assert set(names) == set(fixture)
    assert names == sorted(names)


def test_matrix_enums_and_policy_notes() -> None:
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))

    for item in matrix:
        assert set(item) == {"tool", "category", "role", "stage", "ipc", "note"}
        assert item["role"] in ROLES
        assert item["stage"] in STAGES
        assert item["ipc"] in IPC_MODES
        if item["role"] in {"excluded", "authoring_conditional"}:
            assert item["note"].strip()


def test_rendered_coverage_document_is_current() -> None:
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    assert DOC_PATH.read_text(encoding="utf-8") == render(matrix)
