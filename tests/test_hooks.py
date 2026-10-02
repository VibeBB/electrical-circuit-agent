import base64
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import pytest

from circuit.advisory import AdvisoryResult
from circuit.humanrequest import HumanRequest, build_request, load_responses, write_request
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
AUTHOR_LANE_GUARD_SCRIPT = (
    Path(__file__).parents[1] / "plugins" / "circuit" / "hooks" / "scripts" / "guard_author_lane.py"
)
LIBRARY_REVIEW_SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "circuit"
    / "hooks"
    / "scripts"
    / "record_library_review.py"
)
HUMAN_RESPONSE_SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "circuit"
    / "hooks"
    / "scripts"
    / "record_human_response.py"
)
PART_AUTHOR_PROFILES_SCRIPT = (
    Path(__file__).parents[1]
    / "plugins"
    / "circuit"
    / "hooks"
    / "scripts"
    / "ensure_part_author_profiles.py"
)


def _run_protect_hook(payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROTECT_SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )


def _run_author_lane_guard(payload: dict[str, Any], lane: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CIRCUIT_AUTHORING_LANE"] = lane
    return subprocess.run(
        [sys.executable, str(AUTHOR_LANE_GUARD_SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
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
        "cp event.json .openhands/agent-canvas/session/events/event-1.json",
        "tee .openhands/agent-canvas/session/events/event-2.json",
        "rm .openhands/agent-canvas/session/events/event-3.json",
    ):
        payload = {"tool_name": "terminal", "tool_input": {"command": command}}
        assert _run_protect_hook(payload).returncode == 2, command
    for payload in (
        {
            "tool_name": "file_editor",
            "tool_input": {
                "command": "create",
                "path": ".openhands/agent-canvas/dev_conversations/session/events/event-1.json",
            },
        },
        {
            "tool_name": "terminal",
            "tool_input": {
                "command": (
                    "echo x > .openhands/agent-canvas/dev_conversations/session/events/event-1.json"
                )
            },
        },
    ):
        assert _run_protect_hook(payload).returncode == 2


def test_protect_allows_reading_agent_canvas_events() -> None:
    payload = {
        "tool_name": "terminal",
        "tool_input": {
            "command": "cat .openhands/agent-canvas/dev_conversations/session/events/event-1.json"
        },
    }
    assert _run_protect_hook(payload).returncode == 0


def test_protect_blocks_git_and_web_access_to_confidential_artifacts(tmp_path: Path) -> None:
    project = tmp_path / "project"
    private_file = project / ".confidential" / "private.pdf"
    private_file.parent.mkdir(parents=True)
    private_file.write_bytes(_PDF)
    listed_file = project / "intake" / "attachments" / "listed.pdf"
    listed_file.parent.mkdir(parents=True)
    listed_file.write_bytes(_PDF)
    manifest = listed_file.parent / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "confidential": True,
                "attachment_path": str(listed_file),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    git_init = subprocess.run(
        ["git", "init", "-q"],
        cwd=project,
        capture_output=True,
        check=False,
    )
    assert git_init.returncode == 0
    git_add = subprocess.run(
        ["git", "add", "-f", "--", ".confidential/private.pdf"],
        cwd=project,
        capture_output=True,
        check=False,
    )
    assert git_add.returncode == 0

    for command in (
        "git add .confidential/private.pdf",
        f"git -C {project} add .confidential/private.pdf",
        "git add intake/attachments/listed.pdf",
        "git commit -m 'publish artifacts'",
        "git push origin feature",
    ):
        payload = {
            "working_dir": str(project),
            "tool_name": "terminal",
            "tool_input": {"command": command},
        }
        result = _run_protect_hook(payload)
        assert result.returncode == 2, command
        assert "confidential artifacts" in result.stderr
        assert "private.pdf" not in result.stderr
        assert "listed.pdf" not in result.stderr

    for tool_name in ("web_search", "fetch_url"):
        payload = {
            "working_dir": str(project),
            "tool_name": tool_name,
            "tool_input": {"url": str(private_file)},
        }
        result = _run_protect_hook(payload)
        assert result.returncode == 2, tool_name
        assert "confidential artifacts" in result.stderr

    listed_payload = {
        "working_dir": str(project),
        "tool_name": "terminal",
        "tool_input": {"command": f"git add {listed_file}"},
    }
    assert _run_protect_hook(listed_payload).returncode == 2


_VISION_CONTROL_READS: tuple[dict[str, Any], ...] = (
    {
        "tool_name": "terminal",
        "tool_input": {"command": "cat /project/.vision-control/x.json"},
    },
    {
        "tool_name": "terminal",
        "tool_input": {"command": "rg . /project/.vision-control"},
    },
    {
        "tool_name": "file_editor",
        "tool_input": {"command": "view", "path": "/project/.vision-control/x.json"},
    },
    {
        "tool_name": "circuit_vision_answer",
        "tool_input": {"batch_path": "/project/.vision-control/x.json"},
    },
)

_CORPUS_TRUTH_READS: tuple[dict[str, Any], ...] = (
    {
        "tool_name": "terminal",
        "tool_input": {"command": "cat /project/library/corpus/truth/x.json"},
    },
    {
        "tool_name": "terminal",
        "tool_input": {"command": "rg . /project/library/corpus/truth"},
    },
    {
        "tool_name": "file_editor",
        "tool_input": {"command": "view", "path": "/project/library/corpus/truth/x.json"},
    },
    {
        "tool_name": "file_editor",
        "tool_input": {"command": "create", "path": "/project/library/corpus/corpus.json"},
    },
    {
        "tool_name": "circuit_corpus_score",
        "tool_input": {"entry_id": "fixture"},
    },
)


def test_main_agent_guard_denies_vision_control_reads() -> None:
    for payload in _VISION_CONTROL_READS:
        result = _run_protect_hook(payload)
        assert result.returncode == 2, payload
        assert "vision control state is inaccessible" in result.stderr


def test_main_agent_guard_denies_corpus_truth_and_scoring() -> None:
    for payload in _CORPUS_TRUTH_READS:
        result = _run_protect_hook(payload)
        assert result.returncode == 2, payload
        assert "golden corpus" in result.stderr


@pytest.mark.parametrize("lane", ["a", "b"])
def test_author_lane_guard_denies_vision_control_reads(lane: str) -> None:
    for payload in _VISION_CONTROL_READS:
        result = _run_author_lane_guard(payload, lane)
        assert result.returncode == 2, (lane, payload)
        assert "blind authoring lane context is isolated" in result.stderr


@pytest.mark.parametrize("lane", ["a", "b"])
def test_author_lane_guard_denies_corpus_truth_and_scoring(lane: str) -> None:
    for payload in _CORPUS_TRUTH_READS:
        result = _run_author_lane_guard(payload, lane)
        assert result.returncode == 2, (lane, payload)
        assert "blind authoring lane context is isolated" in result.stderr


def test_part_author_profiles_use_first_distinct_model(tmp_path: Path) -> None:
    profile_dir = tmp_path / ".openhands" / "profiles"
    profile_dir.mkdir(parents=True)
    (tmp_path / ".openhands" / "settings.json").write_text(
        json.dumps({"active_profile": "active-model"}), encoding="utf-8"
    )
    for name, model in (
        ("active-model", "model-active"),
        ("aaa-model", "model-a"),
        ("zzz-model", "model-z"),
        ("vibebb-candidate", "model-ignored"),
    ):
        (profile_dir / f"{name}.json").write_text(
            json.dumps({"model": model}),
            encoding="utf-8",
        )
    (profile_dir / "000-unreadable.json").write_text("{", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(PART_AUTHOR_PROFILES_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["templates"] == {
        "vibebb-part-author-a": "active-model",
        "vibebb-part-author-b": "aaa-model",
    }
    author_a = json.loads((profile_dir / "vibebb-part-author-a.json").read_text(encoding="utf-8"))
    author_b = json.loads((profile_dir / "vibebb-part-author-b.json").read_text(encoding="utf-8"))
    assert author_a["model"] == "model-active"
    assert author_b["model"] == "model-a"


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
_PDF = b"%PDF-1.4\n%%EOF\n"


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


def _write_review_event(events: Path, name: str, source: str, text: str) -> Path:
    event_path = events / name
    event_path.write_text(
        json.dumps(
            {
                "id": name,
                "source": source,
                "llm_message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            }
        ),
        encoding="utf-8",
    )
    return event_path


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


def test_intake_attachments_stores_confidential_pdf_and_request_metadata(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events"
    events.mkdir()
    request_id = "a" * 16
    event_path = events / "event-1.json"
    event_path.write_text(
        json.dumps(
            {
                "source": "user",
                "llm_message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"CIRCUIT-HUMAN-RESPONSE {request_id}\n"
                                "decision: provided\nconfidential: yes"
                            ),
                        },
                        {
                            "type": "file",
                            "url": "data:application/pdf;base64," + base64.b64encode(_PDF).decode(),
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / "project"

    result = _run_attach_hook({"working_dir": str(project)}, events)

    assert result.returncode == 0
    private_dir = project / ".confidential" / "intake" / "attachments"
    pdf_path = private_dir / f"{hashlib.sha256(_PDF).hexdigest()[:12]}.pdf"
    assert pdf_path.read_bytes() == _PDF
    assert (project / ".confidential" / ".gitignore").read_text(encoding="utf-8") == (
        "*\n!.gitignore\n"
    )
    records = [
        json.loads(line)
        for line in (project / "intake" / "attachments" / "manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(records) == 1
    assert records[0]["attachment_path"] == str(pdf_path)
    assert records[0]["origin"] == "user_provided"
    assert records[0]["event_sha256"] == hashlib.sha256(event_path.read_bytes()).hexdigest()
    assert records[0]["request_id"] == request_id
    assert records[0]["confidential"] is True


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


def test_intake_attachments_does_not_persist_undecodable_url_data(tmp_path: Path) -> None:
    events = tmp_path / "events"
    events.mkdir()
    url = "data:image/png;base64,private-datasheet-token"
    _write_event(events, "event-1.json", "user", [url])
    workdir = tmp_path / "work"
    workdir.mkdir()

    result = _run_attach_hook({"working_dir": str(workdir)}, events)

    assert result.returncode == 0
    manifest = workdir / "intake" / "attachments" / "manifest.jsonl"
    text = manifest.read_text(encoding="utf-8")
    record = json.loads(text.splitlines()[0])
    assert record["materialized"] is False
    assert record["reason"] == "undecodable"
    assert "url_prefix" not in record
    assert "private-datasheet-token" not in text


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


def test_record_library_review_pointer_is_user_only_and_idempotent(tmp_path: Path) -> None:
    events = tmp_path / "events"
    events.mkdir()
    packet_id = "0123456789abcdef"
    event_text = (
        f"CIRCUIT-LIBRARY-REVIEW {packet_id}\ndecision: approve\nreviewer: Human Reviewer\n"
    )
    event_path = _write_review_event(events, "event-1.json", "user", event_text)
    _write_review_event(events, "event-2.json", "agent", event_text)
    payload = {"working_dir": str(tmp_path / "project")}
    env = dict(os.environ)
    env["CIRCUIT_AGENT_EVENTS_DIR"] = str(events)

    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(LIBRARY_REVIEW_SCRIPT)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        assert result.returncode == 0

    pointers = list((tmp_path / "project" / "library" / "reviews" / "decisions").glob("*.json"))
    assert len(pointers) == 1
    pointer = json.loads(pointers[0].read_text(encoding="utf-8"))
    event_sha = hashlib.sha256(event_path.read_bytes()).hexdigest()
    assert pointer == {
        "artifact_kind": "circuit_library_review_pointer",
        "packet_id": packet_id,
        "event_path": str(event_path.resolve()),
        "event_sha256": event_sha,
        "recorded_at": pointer["recorded_at"],
    }


def test_record_library_review_pointer_without_events_exits_zero(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["CIRCUIT_AGENT_EVENTS_DIR"] = str(tmp_path / "unreadable")
    result = subprocess.run(
        [sys.executable, str(LIBRARY_REVIEW_SCRIPT)],
        input=json.dumps({"working_dir": str(tmp_path)}),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0
    assert not (tmp_path / "library").exists()


def test_record_library_review_pointer_unreadable_events_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = tmp_path / "events"
    events.mkdir()
    module_spec = importlib.util.spec_from_file_location(
        "record_library_review_under_test", LIBRARY_REVIEW_SCRIPT
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    def unreadable_glob(_path: Path, _pattern: str) -> Any:
        raise PermissionError("event directory is unreadable")

    monkeypatch.setenv("CIRCUIT_AGENT_EVENTS_DIR", str(events))
    monkeypatch.setattr(Path, "glob", unreadable_glob)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"working_dir": str(tmp_path / "project")})),
    )

    assert module.main() == 0


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


def test_record_image_observation_parses_vision_read_tool_result(tmp_path: Path) -> None:
    image = tmp_path / "vision" / "read.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(_PNG)
    payload = {
        "working_dir": str(tmp_path),
        "tool_name": "circuit_vision_read",
        "tool_input": {"requests": [{"kind": "table"}]},
        "tool_response": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"items": [{"image_path": str(image)}]}),
                }
            ]
        },
        "session_id": "vision-session",
    }

    assert _run_observe_hook(payload).returncode == 0
    records = _observations(tmp_path)
    assert len(records) == 1
    assert records[0]["tool_name"] == "circuit_vision_read"
    assert records[0]["image_path"] == str(image)
    assert records[0]["image_sha256"] == hashlib.sha256(_PNG).hexdigest()


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
        "git add -A",
        "git add --all",
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


def test_record_vision_tool_event_records_actor(tmp_path: Path) -> None:
    """Payload identity keys land on the record so each event is attributable."""
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
        "agent_name": "circuit-review",
        "tool_call_id": "call-11",
    }

    assert _run_vision_hook(payload).returncode == 0

    events = tmp_path / "observations" / "circuit" / "vision-tool-events.jsonl"
    record = json.loads(events.read_text(encoding="utf-8").splitlines()[0])
    assert record["actor"] == {"agent_name": "circuit-review", "tool_call_id": "call-11"}
    assert record["tool_call_id"] == "call-11"


def test_record_image_observation_records_actor(tmp_path: Path) -> None:
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
            ]
        },
        "session_id": "s1",
        "subagent_type": "circuit-layout",
        "action_id": "act-5",
    }

    assert _run_observe_hook(payload).returncode == 0

    records = _observations(tmp_path)
    assert records[0]["actor"] == {"action_id": "act-5", "subagent_type": "circuit-layout"}
    assert records[0]["tool_call_id"] == "act-5"


def test_record_hooks_share_provenance_contract(tmp_path: Path) -> None:
    """Both observation hooks attribute the same actor and emit 64-hex ids."""
    image = tmp_path / "renders" / "board.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(_PNG)
    base = {
        "working_dir": str(tmp_path),
        "session_id": "s1",
        "agent_name": "circuit-layout",
        "tool_call_id": "call-7",
    }
    vision_payload = {
        **base,
        "tool_name": "inspect_image_with_vision",
        "tool_input": {"image_index": 0, "question": "q"},
        "tool_response": {
            "answer": "a",
            "profile_name": "vision",
            "model": "m",
        },
    }
    observe_payload = {
        **base,
        "tool_name": "file_editor",
        "tool_input": {"command": "view", "path": str(image)},
        "tool_response": {"output": "ok"},
    }

    assert _run_vision_hook(vision_payload).returncode == 0
    assert _run_observe_hook(observe_payload).returncode == 0

    vision = json.loads(
        (tmp_path / "observations" / "circuit" / "vision-tool-events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    observe = _observations(tmp_path)[0]
    expected_actor = {"agent_name": "circuit-layout", "tool_call_id": "call-7"}
    assert vision["actor"] == observe["actor"] == expected_actor
    assert len(vision["event_id"]) == len(observe["event_id"]) == 64
    int(vision["event_id"], 16)
    int(observe["event_id"], 16)


def test_provenance_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location(
        "_provenance",
        Path(__file__).parents[1] / "plugins" / "circuit" / "hooks" / "scripts" / "_provenance.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    missing = tmp_path / "missing.jsonl"
    assert module.next_sequence(missing) == 1
    missing.write_text("a\nb\n", encoding="utf-8")
    assert module.next_sequence(missing) == 3

    payload: dict[str, Any] = {"working_dir": str(tmp_path)}
    rel = Path("observations/x.jsonl")
    env = "CIRCUIT_TEST_EVENTS"
    monkeypatch.delenv(env, raising=False)
    assert module.events_path(payload, env, rel) == tmp_path / rel
    monkeypatch.setenv(env, "sub/log.jsonl")
    assert module.events_path(payload, env, rel) == tmp_path / "sub" / "log.jsonl"
    absolute = tmp_path / "abs" / "log.jsonl"
    monkeypatch.setenv(env, str(absolute))
    assert module.events_path(payload, env, rel) == absolute


def _human_request_for_hook() -> HumanRequest:
    return build_request(
        kind="library_review",
        subject={"manufacturer": "Example", "mpn": "TEST-1"},
        reason="Review the current library artifacts against the source evidence.",
        evidence=[
            {
                "kind": "note",
                "ref": "verification",
                "summary": "A fresh deterministic verification report.",
            }
        ],
        known=["The request is bound to current artifact hashes."],
        unknown=[],
        agent_assessment=(
            "This request presents source evidence and deterministic findings for review. "
            "Compare each package and pin claim with the cited material before deciding. "
            "Hash agreement does not prove that the underlying library content is correct. "
            "Every unresolved field remains explicit, and approval requires independent "
            "human review of the evidence."
        ),
        recommendation="Approve only after review.",
        recommendation_rationale="Approval remains an independent human decision.",
        alternatives=[
            {
                "option": "Approve only after review.",
                "risks": ["A source discrepancy could be overlooked."],
            },
            {
                "option": "Request corrections.",
                "risks": ["Release is delayed pending correction."],
            },
        ],
        recommended=0,
        details={"kind": "library_review", "packet_id": "a" * 16},
    )


def _datasheet_request_for_hook() -> HumanRequest:
    return build_request(
        kind="datasheet_acquisition",
        subject={"manufacturer": "Example", "mpn": "TEST-1", "revision": "A"},
        reason="The matching datasheet could not be found.",
        evidence=[
            {
                "kind": "note",
                "ref": "attempts",
                "summary": "The manufacturer source was checked.",
            }
        ],
        known=["The requested part is TEST-1."],
        unknown=["Package evidence remains unavailable."],
        agent_assessment=(
            "The library cannot proceed without a datasheet matching the requested part and "
            "revision. Package, pin, orderable, and mechanical claims remain unsupported until "
            "the source PDF is checked. Keep the request open until an official matching "
            "document is received and its evidence is reviewed."
        ),
        recommendation="Provide the requested datasheet.",
        recommendation_rationale="Package and pin claims require source evidence.",
        alternatives=[
            {
                "option": "Provide the requested datasheet.",
                "risks": ["The library remains blocked until the PDF is checked."],
            },
            {
                "option": "Report the datasheet unavailable.",
                "risks": ["Alternative evidence or substitute permission is required."],
            },
        ],
        recommended=0,
        details={
            "kind": "datasheet_acquisition",
            "failure_reason": "not_found",
            "requested_revision": "A",
            "required_sections": ["package_drawing", "pinout", "pin_table"],
            "attempted_sources": ["manufacturer"],
            "optional_cad_requested": False,
        },
    )


def test_record_human_response_pointer_is_user_only_and_hash_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    request = _human_request_for_hook()
    write_request(request, project)
    events = tmp_path / "events"
    events.mkdir()
    monkeypatch.setenv("CIRCUIT_AGENT_EVENTS_DIR", str(events))
    response_text = (
        f"CIRCUIT-HUMAN-RESPONSE {request.request_id}\n"
        "decision: approve\nreviewer: Human Reviewer\n"
    )
    event_path = _write_review_event(events, "event-1.json", "user", response_text)
    _write_review_event(events, "event-2.json", "agent", response_text)
    env = dict(os.environ)
    env["CIRCUIT_AGENT_EVENTS_DIR"] = str(events)
    env["OPENHANDS_PROJECT_DIR"] = str(project)
    payload = json.dumps({"working_dir": str(project)})

    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(HUMAN_RESPONSE_SCRIPT)],
            input=payload,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        assert result.returncode == 0

    pointers = list((project / "library" / "requests" / "responses").glob("*.json"))
    assert len(pointers) == 1
    pointer = json.loads(pointers[0].read_text(encoding="utf-8"))
    assert pointer["request_sha256"] == request.request_sha256
    assert pointer["event_sha256"] == hashlib.sha256(event_path.read_bytes()).hexdigest()
    responses = load_responses(project, request)
    assert len(responses) == 1
    assert responses[0].valid is True
    assert responses[0].decision == "approve"


@pytest.mark.parametrize(("include_pdf", "valid"), [(True, True), (False, False)])
def test_provided_datasheet_response_requires_pdf_in_same_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    include_pdf: bool,
    valid: bool,
) -> None:
    project = tmp_path / "project"
    request = _datasheet_request_for_hook()
    request_json_path, request_markdown_path = write_request(
        request,
        project,
        confidential=True,
    )
    private_request_dir = project / ".confidential" / "library" / "requests"
    assert request_json_path.parent == private_request_dir
    assert request_markdown_path.parent == private_request_dir
    assert (project / ".confidential" / ".gitignore").is_file()
    events = tmp_path / "events"
    events.mkdir()
    monkeypatch.setenv("CIRCUIT_AGENT_EVENTS_DIR", str(events))
    content: list[dict[str, str]] = [
        {
            "type": "text",
            "text": (
                f"CIRCUIT-HUMAN-RESPONSE {request.request_id}\n"
                "decision: provided\nreviewer: Human Reviewer\nconfidential: yes"
            ),
        }
    ]
    if include_pdf:
        content.append(
            {
                "type": "file",
                "url": "data:application/pdf;base64," + base64.b64encode(_PDF).decode(),
            }
        )
    event_path = events / "event-1.json"
    event_path.write_text(
        json.dumps(
            {
                "source": "user",
                "llm_message": {"role": "user", "content": content},
            }
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["CIRCUIT_AGENT_EVENTS_DIR"] = str(events)
    env["OPENHANDS_PROJECT_DIR"] = str(project)
    result = subprocess.run(
        [sys.executable, str(HUMAN_RESPONSE_SCRIPT)],
        input=json.dumps({"working_dir": str(project)}),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0
    responses = load_responses(project, request)
    assert len(responses) == 1
    assert responses[0].valid is valid
    if not valid:
        assert "provided response requires a PDF attachment in the same message" in (
            responses[0].reasons
        )


def test_unavailable_datasheet_response_requires_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    request = _datasheet_request_for_hook()
    write_request(request, project)
    events = tmp_path / "events"
    events.mkdir()
    monkeypatch.setenv("CIRCUIT_AGENT_EVENTS_DIR", str(events))
    event_path = events / "event-1.json"
    event_path.write_text(
        json.dumps(
            {
                "source": "user",
                "llm_message": {
                    "role": "user",
                    "content": (
                        f"CIRCUIT-HUMAN-RESPONSE {request.request_id}\n"
                        "decision: unavailable\nreviewer: Human Reviewer"
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["CIRCUIT_AGENT_EVENTS_DIR"] = str(events)
    env["OPENHANDS_PROJECT_DIR"] = str(project)
    result = subprocess.run(
        [sys.executable, str(HUMAN_RESPONSE_SCRIPT)],
        input=json.dumps({"working_dir": str(project)}),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0
    responses = load_responses(project, request)
    assert len(responses) == 1
    assert responses[0].valid is False
    assert "unavailable response requires reason" in responses[0].reasons


def test_human_response_event_hash_tampering_is_not_trusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    request = _human_request_for_hook()
    write_request(request, project)
    events = tmp_path / "events"
    events.mkdir()
    monkeypatch.setenv("CIRCUIT_AGENT_EVENTS_DIR", str(events))
    event_path = _write_review_event(
        events,
        "event-1.json",
        "user",
        f"CIRCUIT-HUMAN-RESPONSE {request.request_id}\n"
        "decision: approve\nreviewer: Human Reviewer\n",
    )
    env = dict(os.environ)
    env["CIRCUIT_AGENT_EVENTS_DIR"] = str(events)
    env["OPENHANDS_PROJECT_DIR"] = str(project)
    result = subprocess.run(
        [sys.executable, str(HUMAN_RESPONSE_SCRIPT)],
        input=json.dumps({"working_dir": str(project)}),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0
    event_path.write_text("tampered event", encoding="utf-8")
    responses = load_responses(project, request)
    assert len(responses) == 1
    assert responses[0].valid is False
    assert "response event hash mismatch" in responses[0].reasons


def test_human_response_is_invalid_after_request_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    request = _human_request_for_hook()
    request_path, _ = write_request(request, project)
    events = tmp_path / "events"
    events.mkdir()
    monkeypatch.setenv("CIRCUIT_AGENT_EVENTS_DIR", str(events))
    _write_review_event(
        events,
        "event-1.json",
        "user",
        f"CIRCUIT-HUMAN-RESPONSE {request.request_id}\n"
        "decision: approve\nreviewer: Human Reviewer\n",
    )
    env = dict(os.environ)
    env["CIRCUIT_AGENT_EVENTS_DIR"] = str(events)
    env["OPENHANDS_PROJECT_DIR"] = str(project)
    result = subprocess.run(
        [sys.executable, str(HUMAN_RESPONSE_SCRIPT)],
        input=json.dumps({"working_dir": str(project)}),
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0
    stored = json.loads(request_path.read_text(encoding="utf-8"))
    stored["reason"] = "changed after response"
    request_path.write_text(json.dumps(stored), encoding="utf-8")
    responses = load_responses(project, request)
    assert len(responses) == 1
    assert responses[0].valid is False
    assert "stored request hash is missing or changed" in responses[0].reasons
