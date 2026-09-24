import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.docker


def test_e2e_authoring_in_tools_image(tmp_path: Path) -> None:
    image = os.environ.get("CIRCUIT_TOOLS_IMAGE")
    if not image:
        pytest.skip("CIRCUIT_TOOLS_IMAGE is not set")
    repo = Path(__file__).parents[2].resolve()
    workdir = tmp_path / "authoring"
    workdir.mkdir()
    workdir.chmod(0o777)
    command = [
        "docker",
        "run",
        "--rm",
        "--user",
        "circuit",
        "-e",
        f"PYTHONPATH={repo / 'src'}",
        "-v",
        f"{repo}:{repo}",
        "-v",
        f"{workdir}:{workdir}",
        "-w",
        str(repo),
        image,
        "python3",
        "scripts/e2e_authoring.py",
        "--brief",
        str(repo / "tests/data/brief_led_loop.json"),
        "--intake",
        str(repo / "tests/data/brief_led_loop.intake.json"),
        "--workdir",
        str(workdir),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
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
    assert result["advisory_counts"]["ok"] == 53
    assert result["advisory_counts"]["error"] == 0
    assert result["advisory_counts"]["not_applicable"] == 0
    advisory = {item["tool"]: item for item in value["advisory"]}
    assert advisory["refine_placement_force_directed"]["status"] == "ok"
    assert advisory["score_placement"]["detail"]["outline_missing"] is False
    assert list((workdir / "exports" / "gerbers").glob("*"))
    assert list((workdir / "konnect-exports").rglob("*"))
