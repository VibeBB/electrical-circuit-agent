import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from circuit.titleblock import PAPER_SIZES

pytestmark = pytest.mark.docker

REPO = Path(__file__).parents[2].resolve()


def _run_authoring(
    image: str, workdir: Path, brief: Path, intake: Path | None
) -> subprocess.CompletedProcess[str]:
    workdir.mkdir()
    workdir.chmod(0o777)
    command = [
        "docker",
        "run",
        "--rm",
        "--user",
        "circuit",
        "-e",
        f"PYTHONPATH={REPO / 'src'}",
        "-v",
        f"{REPO}:{REPO}",
        "-v",
        f"{workdir}:{workdir}",
        "-w",
        str(REPO),
        image,
        "python3",
        "scripts/e2e_authoring.py",
        "--brief",
        str(brief),
        "--workdir",
        str(workdir),
    ]
    if intake is not None:
        command.extend(["--intake", str(intake)])
    return subprocess.run(command, capture_output=True, text=True, check=False)


def test_e2e_authoring_in_tools_image(tmp_path: Path) -> None:
    image = os.environ.get("CIRCUIT_TOOLS_IMAGE")
    if not image:
        pytest.skip("CIRCUIT_TOOLS_IMAGE is not set")
    workdir = tmp_path / "authoring"
    result = _run_authoring(
        image,
        workdir,
        REPO / "tests/data/brief_led_loop.json",
        REPO / "tests/data/brief_led_loop.intake.json",
    )
    assert result.returncode == 0, result.stderr + result.stdout
    design_report = workdir / "circuit-reports" / "design-report.json"
    value = json.loads(design_report.read_text(encoding="utf-8"))
    assert value["connectivity"]["verdict"] == "pass"
    assert value["erc"]["verdict"] == "pass"
    assert value["drc"]["verdict"] == "pass"
    assert value["drc"]["warnings"] > 0
    assert value["verdict"] == "pass"
    assert len(value["renders"]) == 2
    assert all(Path(path).is_file() and Path(path).stat().st_size > 0 for path in value["renders"])
    assert value["jobset"]["exit_code"] == 0
    assert len(value["jobset"]["outputs"]) >= 10
    assert value["jobset_consistent"] is True
    assert value["diffs"]["schematic"]["identical"] is True
    assert value["diffs"]["pcb"]["identical"] is True
    result = json.loads((workdir / "e2e-authoring.json").read_text(encoding="utf-8"))
    assert result["verdict"] == "pass"
    assert result["intake"]["verdict"] == "ready"
    assert result["libraries"]["verdict"] == "pass"
    provenance = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    assert provenance["schema_version"] == 1
    assert provenance["license"] == "BSD-3-Clause"
    assert provenance["generator"].startswith("circuit-agent/")
    assert provenance["brief_sha256"]
    assert provenance["intake_sha256"]
    assert "python" in provenance["tool_versions"]
    # Assert a floor rather than an exact count: the advisory op surface
    # grows with Konnect releases (0.13.0's IPC-dependent op does not run
    # in this image), so exact counts red unrelated dependency PRs.
    counts = result["advisory_counts"]
    assert counts["ok"] >= 50
    assert counts["error"] + counts["not_applicable"] <= 5
    advisory = {item["tool"]: item for item in value["advisory"]}
    assert advisory["refine_placement_force_directed"]["status"] == "ok"
    assert advisory["score_placement"]["detail"]["outline_missing"] is False
    assert list((workdir / "exports" / "gerbers").glob("*"))
    assert list((workdir / "konnect-exports").rglob("*"))

    # Signal-chain layout: symbols on-sheet at distinct positions, real wires
    # drawn, and none of the readability findings the gate now fails on.
    schematic = workdir / "led_loop.kicad_sch"
    text = schematic.read_text(encoding="utf-8")
    assert "(wire" in text, "schematic carries no drawn wires"
    lint_report = json.loads(
        (workdir / "circuit-reports" / "led_loop.sch_lint.json").read_text(encoding="utf-8")
    )
    assert lint_report["verdict"] == "pass"
    gated = {
        "property_on_symbol",
        "sheet_underutilized",
        "item_out_of_bounds",
        "label_only_connectivity",
    }
    assert not [f for f in lint_report["findings"] if f["type"] in gated]
    symbol_positions = re.findall(r'\(symbol\s+\(lib_id "[^"]+"\)\s+\(at ([\d.]+) ([\d.]+)', text)
    assert len(symbol_positions) == 3
    assert len(set(symbol_positions)) == 3
    paper_match = re.search(r'\(paper "(\w+)"', text)
    assert paper_match is not None
    width, height = PAPER_SIZES[paper_match.group(1)]
    for x_text, y_text in symbol_positions:
        assert 0 < float(x_text) < width
        assert 0 < float(y_text) < height


def test_e2e_authoring_rejects_corrupt_net(tmp_path: Path) -> None:
    """A net naming a pin that does not exist must fail the verdict."""
    image = os.environ.get("CIRCUIT_TOOLS_IMAGE")
    if not image:
        pytest.skip("CIRCUIT_TOOLS_IMAGE is not set")
    corrupt = json.loads((REPO / "tests/data/brief_led_loop.json").read_text(encoding="utf-8"))
    corrupt["nets"][1]["pins"][1] = "D1.99"
    brief_path = tmp_path / "brief_corrupt.json"
    brief_path.write_text(json.dumps(corrupt), encoding="utf-8")
    result = _run_authoring(image, tmp_path / "corrupt", brief_path, None)
    assert result.returncode == 1
    value = json.loads((tmp_path / "corrupt" / "e2e-authoring.json").read_text(encoding="utf-8"))
    assert value["verdict"] == "fail"
