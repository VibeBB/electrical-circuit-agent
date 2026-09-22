import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from circuit.report import DesignReport

SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "circuit"
    / "hooks"
    / "scripts"
    / "report_design_status.py"
)

VISION_SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "circuit"
    / "hooks"
    / "scripts"
    / "record_vision_tool_event.py"
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


def _run_vision_hook(payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VISION_SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def test_record_vision_tool_event_appends(tmp_path: Path) -> None:
    payload = {
        "working_dir": str(tmp_path),
        "session_id": "session-1",
        "tool_name": "inspect_image_with_vision",
        "tool_input": {"image_index": 0, "question": "Check for unrouted pads"},
        "tool_response": {
            "answer": "No unrouted pads are visible.",
            "profile_name": "vision",
            "model": "vision-model-1",
        },
    }

    result = _run_vision_hook(payload)

    assert result.returncode == 0
    events = tmp_path / ".openhands" / "circuit" / "vision-tool-events.jsonl"
    record = json.loads(events.read_text(encoding="utf-8").splitlines()[0])
    assert record["tool_name"] == "inspect_image_with_vision"
    assert record["image_index"] == 0
    assert record["profile_name"] == "vision"
    assert record["model"] == "vision-model-1"
    assert record["response_sha256"].startswith("sha256:")
    assert record["session_id"] == "session-1"


def test_record_vision_tool_event_ignores_other_tools(tmp_path: Path) -> None:
    payload = {
        "working_dir": str(tmp_path),
        "tool_name": "circuit_render",
        "tool_input": {},
        "tool_response": {"answer": "ok", "profile_name": "vision", "model": "m"},
    }

    result = _run_vision_hook(payload)

    assert result.returncode == 0
    assert not (tmp_path / ".openhands" / "circuit" / "vision-tool-events.jsonl").exists()


def test_record_vision_tool_event_skips_errors(tmp_path: Path) -> None:
    payload = {
        "working_dir": str(tmp_path),
        "tool_name": "inspect_image_with_vision",
        "tool_input": {"image_index": 0},
        "tool_response": {"error": "vision profile missing"},
    }

    result = _run_vision_hook(payload)

    assert result.returncode == 0
    assert not (tmp_path / ".openhands" / "circuit" / "vision-tool-events.jsonl").exists()
