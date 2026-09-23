import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from circuit.advisory import AdvisoryResult
from circuit.kicad_cli import JobsetResult
from circuit.report import DesignReport
from circuit.sch_lint import SchLintReport

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

PROTECT_SCRIPT = (
    Path(__file__).parents[1] / "plugins" / "circuit" / "hooks" / "scripts" / "protect_libraries.py"
)


def _run_protect_hook(payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROTECT_SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def test_protect_denies_design_file_writes() -> None:
    for payload in (
        {
            "tool_name": "file_editor",
            "tool_input": {"command": "create", "file_path": "led.kicad_sch"},
        },
        {
            "tool_name": "apply_patch",
            "tool_input": {"patch": "+++ b/led.kicad_pcb\n"},
        },
    ):
        result = _run_protect_hook(payload)
        assert result.returncode == 2
        assert "Konnect" in result.stderr


def test_protect_allows_design_file_view_and_terminal() -> None:
    for payload in (
        {
            "tool_name": "file_editor",
            "tool_input": {"command": "view", "path": "led.kicad_sch"},
        },
        {
            "tool_name": "terminal",
            "tool_input": {"command": "kicad-cli sch erc led.kicad_sch"},
        },
    ):
        assert _run_protect_hook(payload).returncode == 0


def test_protect_denies_library_writes() -> None:
    for command in (
        "cp x libraries/cern-kicad-libs/foo",
        "echo x > libraries/cern-kicad-libs/foo.kicad_sym",
        "tee libraries/cern-kicad-libs/foo.kicad_sym",
        "rm libraries/cern-kicad-libs/foo.kicad_sym",
        "sed -i s/a/b/ libraries/cern-kicad-libs/foo.kicad_sym",
        "mkdir -p libraries/cern-kicad-libs/new",
    ):
        payload = {"tool_name": "terminal", "tool_input": {"command": command}}
        assert _run_protect_hook(payload).returncode == 2, command


def test_protect_denies_terminal_design_writes() -> None:
    for command in (
        "echo x > out.kicad_sch",
        "echo x >> out.kicad_pcb",
        "cat a | tee out.kicad_sch",
        "cp template.txt out.kicad_pcb",
        "mv draft.kicad_sch final.kicad_sch",
        "dd of=out.kicad_sch",
        "sed -i s/a/b/ out.kicad_sch",
        "install -m644 src out.kicad_pcb",
        "touch out.kicad_sch",
        "rm out.kicad_pcb",
        "python3 gen.py && cp x out.kicad_sch",
        "cmd 2> err.kicad_sch",
    ):
        payload = {"tool_name": "terminal", "tool_input": {"command": command}}
        assert _run_protect_hook(payload).returncode == 2, command


def test_protect_allows_terminal_reads_and_content_mentions() -> None:
    for command in (
        "cat out.kicad_sch",
        "kicad-cli sch erc out.kicad_sch",
        "kicad-cli pcb drc out.kicad_pcb",
        "find libraries/cern-kicad-libs -name '*.kicad_sym'",
        "grep -r Battery libraries/cern-kicad-libs",
        "cp out.kicad_sch backups/out.bak",
        "tar czf libs.tgz libraries/cern-kicad-libs",
        "echo 'see libraries/cern-kicad-libs' > notes.md",
        "echo '.kicad_sch is a format' > README.md",
        "cmd >&2",
        "cmd 2>&1 | grep out.kicad_sch",
    ):
        payload = {"tool_name": "terminal", "tool_input": {"command": command}}
        assert _run_protect_hook(payload).returncode == 0, command


def test_protect_scans_paths_not_file_bodies() -> None:
    for payload in (
        {
            "tool_name": "file_editor",
            "tool_input": {
                "command": "create",
                "path": "AGENTS.md",
                "file_text": "use libraries/cern-kicad-libs and .kicad_sch files",
            },
        },
        {
            "tool_name": "file_editor",
            "tool_input": {
                "command": "str_replace",
                "path": "docs/notes.md",
                "old_str": "a",
                "new_str": "(kicad_sch (version 1))",
            },
        },
    ):
        assert _run_protect_hook(payload).returncode == 0


def test_protect_denies_file_editor_writes_under_libraries() -> None:
    for action in ("create", "str_replace", "insert"):
        payload = {
            "tool_name": "file_editor",
            "tool_input": {
                "command": action,
                "path": "libraries/cern-kicad-libs/Device.kicad_sym",
            },
        }
        assert _run_protect_hook(payload).returncode == 2
    payload = {
        "tool_name": "file_editor",
        "tool_input": {
            "command": "view",
            "path": "libraries/cern-kicad-libs/Device.kicad_sym",
        },
    }
    assert _run_protect_hook(payload).returncode == 0


def _write_report(path: Path, verdict: Literal["pass", "fail"], **kwargs: Any) -> None:
    fields: dict[str, Any] = {
        "brief_name": "fixture",
        "brief_sha256": "0" * 64,
        "kicad_version": "11",
        "connectivity": None,
        "erc": None,
        "drc": None,
        "exports": {},
        "verdict": verdict,
        "reasons": [] if verdict == "pass" else ["gate failed: drc"],
    }
    fields.update(kwargs)
    report = DesignReport(**fields)
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


def test_report_design_status_warns_on_empty_sections(tmp_path: Path) -> None:
    report_path = tmp_path / "circuit-reports" / "design-report.json"
    _write_report(report_path, "pass")

    result = _run_hook(tmp_path)

    assert result.returncode == 0
    context = json.loads(result.stdout)["additionalContext"]
    assert "sections not recorded" in context
    assert "exports" in context
    assert "renders" in context
    assert "jobset" in context


def test_report_design_status_no_section_warning_when_complete(tmp_path: Path) -> None:
    report_path = tmp_path / "circuit-reports" / "design-report.json"
    jobset = JobsetResult(
        jobset=Path("default.kicad_jobset"),
        project=Path("board.kicad_pro"),
        output_dir=Path("jobset"),
        exit_code=0,
    )
    sch_lint_report = SchLintReport(
        source=Path("board.kicad_sch"),
        verdict="pass",
        errors=0,
        warnings=0,
        symbols_checked=1,
    )
    advisory = AdvisoryResult(tool="run_erc", stage="schematic", status="ok", summary="clean")
    _write_report(
        report_path,
        "pass",
        sch_lint=sch_lint_report,
        exports={"gerbers": ["exports/gerbers/board-F_Cu.gtl"]},
        renders=["circuit-reports/render-top.png"],
        jobset=jobset,
        advisory=[advisory],
    )

    result = _run_hook(tmp_path)

    assert result.returncode == 0
    context = json.loads(result.stdout)["additionalContext"]
    assert "sections not recorded" not in context


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
