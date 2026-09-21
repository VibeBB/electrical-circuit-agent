import json
import subprocess
import sys
from pathlib import Path
from typing import Literal

from circuit.report import DesignReport

SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "circuit"
    / "hooks"
    / "scripts"
    / "report_design_status.py"
)


def _write_report(path: Path, verdict: Literal["pass", "fail"]) -> None:
    report = DesignReport(
        brief_name="fixture",
        brief_sha256="0" * 64,
        kicad_version="11",
        connectivity=None,
        erc=None,
        drc=None,
        exports={},
        verdict=verdict,
        reasons=[] if verdict == "pass" else ["gate failed: drc"],
    )
    path.parent.mkdir(parents=True)
    path.write_text(report.model_dump_json(), encoding="utf-8")


def _run_hook(working_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"working_dir": str(working_dir)}),
        text=True,
        capture_output=True,
        check=False,
    )


def test_report_design_status_pass_and_fail(tmp_path: Path) -> None:
    _write_report(tmp_path / "pass" / "circuit-reports" / "design-report.json", "pass")
    _write_report(tmp_path / "fail" / "circuit-reports" / "design-report.json", "fail")

    result = _run_hook(tmp_path)

    assert result.returncode == 0
    context = json.loads(result.stdout)["additionalContext"]
    assert "verdict=pass" in context
    assert "verdict=fail" in context
    assert "state each failing gate explicitly" in context


def test_report_design_status_none(tmp_path: Path) -> None:
    result = _run_hook(tmp_path)

    assert result.returncode == 0
    assert "No design reports found" in json.loads(result.stdout)["additionalContext"]


def test_report_design_status_malformed(tmp_path: Path) -> None:
    report = tmp_path / "circuit-reports" / "design-report.json"
    report.parent.mkdir()
    report.write_text("{not-json", encoding="utf-8")

    result = _run_hook(tmp_path)

    assert result.returncode == 1
    assert "report_design_status:" in result.stderr
