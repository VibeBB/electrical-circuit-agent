import base64
import hashlib
import json
import os
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
    events = tmp_path / "observations" / "circuit" / "vision-tool-events.jsonl"
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
    assert not (tmp_path / "observations" / "circuit" / "vision-tool-events.jsonl").exists()


def test_record_vision_tool_event_skips_errors(tmp_path: Path) -> None:
    payload = {
        "working_dir": str(tmp_path),
        "tool_name": "inspect_image_with_vision",
        "tool_input": {"image_index": 0},
        "tool_response": {"error": "vision profile missing"},
    }

    result = _run_vision_hook(payload)

    assert result.returncode == 0
    assert not (tmp_path / "observations" / "circuit" / "vision-tool-events.jsonl").exists()


ATTACH_SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "circuit"
    / "hooks"
    / "scripts"
    / "intake_attachments.py"
)

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c626001000000ffff03000006000557bfabd40000000049"
    "454e44ae426082"
)


def _write_event(events: Path, name: str, source: str, urls: list[str]) -> None:
    event = {
        "id": name,
        "source": source,
        "llm_message": {
            "role": "user",
            "content": (
                [{"type": "image", "image_urls": urls}]
                if urls
                else [{"type": "text", "text": "hi"}]
            ),
        },
    }
    (events / name).write_text(json.dumps(event), encoding="utf-8")


def _run_attach_hook(
    payload: dict[str, Any], events_dir: Path | None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if events_dir is not None:
        env["CIRCUIT_AGENT_EVENTS_DIR"] = str(events_dir)
    else:
        env.pop("CIRCUIT_AGENT_EVENTS_DIR", None)
    return subprocess.run(
        [sys.executable, str(ATTACH_SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_intake_attachments_materializes_user_images(tmp_path: Path) -> None:
    events = tmp_path / "events"
    events.mkdir()
    encoded = "data:image/png;base64," + base64.b64encode(_PNG).decode()
    _write_event(events, "event-1.json", "user", [encoded])
    _write_event(events, "event-2.json", "agent", [encoded])
    _write_event(events, "event-3.json", "user", [])
    workdir = tmp_path / "work"
    workdir.mkdir()

    payload = {"working_dir": str(workdir)}
    result = _run_attach_hook(payload, events)

    assert result.returncode == 0
    attachments = workdir / "intake" / "attachments"
    images = list(attachments.glob("*.png"))
    assert len(images) == 1
    assert images[0].read_bytes() == _PNG
    records = [
        json.loads(line)
        for line in (attachments / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["sha256"] == hashlib.sha256(_PNG).hexdigest()
    assert records[0]["materialized"] is True
    # second run is a no-op
    assert _run_attach_hook(payload, events).returncode == 0
    assert len(list(attachments.glob("*.png"))) == 1
    assert len((attachments / "manifest.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_intake_attachments_records_non_data_urls(tmp_path: Path) -> None:
    events = tmp_path / "events"
    events.mkdir()
    _write_event(events, "event-1.json", "user", ["https://example.com/board.png"])
    workdir = tmp_path / "work"
    workdir.mkdir()

    result = _run_attach_hook({"working_dir": str(workdir)}, events)

    assert result.returncode == 0
    manifest = workdir / "intake" / "attachments" / "manifest.jsonl"
    record = json.loads(manifest.read_text(encoding="utf-8").splitlines()[0])
    assert record["materialized"] is False
    assert record["reason"] == "non-data-url"


def test_intake_attachments_fails_open_without_events_dir(tmp_path: Path) -> None:
    result = _run_attach_hook({"working_dir": str(tmp_path)}, None)
    assert result.returncode == 0
    assert not (tmp_path / "intake").exists()


def test_intake_attachments_uses_session_default_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    events = home / ".openhands" / "agent-canvas" / "dev_conversations" / "session-9" / "events"
    events.mkdir(parents=True)
    encoded = "data:image/png;base64," + base64.b64encode(_PNG).decode()
    _write_event(events, "event-1.json", "user", [encoded])
    workdir = tmp_path / "work"
    workdir.mkdir()

    env = dict(os.environ)
    env.pop("CIRCUIT_AGENT_EVENTS_DIR", None)
    env["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, str(ATTACH_SCRIPT)],
        input=json.dumps({"working_dir": str(workdir), "session_id": "session-9"}),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0
    assert list((workdir / "intake" / "attachments").glob("*.png"))


OBSERVE_SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "circuit"
    / "hooks"
    / "scripts"
    / "record_image_observation.py"
)


def _run_observe_hook(payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(OBSERVE_SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def _observations(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "observations" / "circuit" / "image-observations.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_record_image_observation_logs_render_paths(tmp_path: Path) -> None:
    image = tmp_path / "circuit-reports" / "render-top.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(_PNG)
    payload = {
        "working_dir": str(tmp_path),
        "tool_name": "circuit_render",
        "tool_input": {"board_path": "b.kicad_pcb"},
        "tool_response": {
            "content": [
                {"type": "text", "text": json.dumps({"output_path": str(image)})},
                {"type": "image", "data": "...", "mimeType": "image/png"},
            ]
        },
        "session_id": "s1",
    }

    assert _run_observe_hook(payload).returncode == 0
    records = _observations(tmp_path)
    assert len(records) == 1
    assert records[0]["tool_name"] == "circuit_render"
    assert records[0]["image_path"] == str(image)
    assert records[0]["image_sha256"] == hashlib.sha256(_PNG).hexdigest()
    assert records[0]["session_id"] == "s1"


def test_record_image_observation_logs_file_editor_view(tmp_path: Path) -> None:
    image = tmp_path / "renders" / "board.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(_PNG)
    payload = {
        "working_dir": str(tmp_path),
        "tool_name": "file_editor",
        "tool_input": {"command": "view", "path": str(image)},
        "tool_response": {"output": "ok"},
    }

    assert _run_observe_hook(payload).returncode == 0
    records = _observations(tmp_path)
    assert len(records) == 1
    assert records[0]["image_sha256"] == hashlib.sha256(_PNG).hexdigest()


def test_record_image_observation_skips_non_image_and_errors(tmp_path: Path) -> None:
    for payload in (
        {
            "working_dir": str(tmp_path),
            "tool_name": "file_editor",
            "tool_input": {"command": "create", "path": str(tmp_path / "a.png")},
            "tool_response": {"output": "ok"},
        },
        {
            "working_dir": str(tmp_path),
            "tool_name": "circuit_render",
            "tool_input": {},
            "tool_response": {"error": "kicad-cli missing"},
        },
        {
            "working_dir": str(tmp_path),
            "tool_name": "circuit_render",
            "tool_input": {},
            "tool_response": {
                "content": [{"type": "text", "text": '{"output_path": "/no/such.png"}'}]
            },
        },
    ):
        assert _run_observe_hook(payload).returncode == 0
    assert _observations(tmp_path) == []


SAFETY_RAIL_SCRIPT = (
    Path(__file__).parents[1] / "plugins" / "circuit" / "hooks" / "scripts" / "safety_rail.py"
)


def _run_safety_rail(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SAFETY_RAIL_SCRIPT)],
        input=json.dumps({"tool_name": "terminal", "tool_input": {"command": command}}),
        text=True,
        capture_output=True,
        check=False,
    )


def test_safety_rail_denies_denylist() -> None:
    for command in (
        "rm -rf /",
        "rm -fr ~",
        "dd if=x of=/dev/sda",
        "mkfs.ext4 /dev/sda1",
        "shutdown now",
        "git push origin main",
        "git push --force origin feat",
        "git reset --hard",
        "git clean -fd",
        "git checkout -- src/circuit/brief.py",
        "git stash drop",
        "git add .",
        "git commit --amend",
        "git commit --no-verify",
    ):
        assert _run_safety_rail(command).returncode == 2, command


def test_safety_rail_allows_normal_commands() -> None:
    for command in (
        "rm -rf out/artifacts",
        "git push --force-with-lease origin feat",
        "git push origin feat",
        "git add src/circuit/brief.py docs",
        "git commit -m message",
        "python -m circuit doctor",
        "echo hi > out.txt",
        "find . -name '*.kicad_sch'",
    ):
        assert _run_safety_rail(command).returncode == 0, command
