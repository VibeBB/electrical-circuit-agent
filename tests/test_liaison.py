"""Sister Liaison Protocol v2: inbox states, response writing, refusals."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.types import TextContent

from circuit import cli, liaison, records

IMPRESSION = (
    "The liaison request reads as a coherent change order: the requested edits "
    "are bound to hashed inputs, and the acceptance list is concrete enough to "
    "verify. What worries me is whether downstream sisters can rely on the "
    "recorded decision refs when a request is revised mid-flight. A maintainer "
    "could still act on the inbox states alone, although the malformed list "
    "needs the same care as the requests. Next I would keep every response "
    "tied to fresh input hashes. Overall the flow communicates intent well."
)


def _decision(evidence_path: str) -> dict[str, Any]:
    return {
        "id": "liaison-answer-order",
        "stage": "design",
        "question": "Should the response be written before or after the gates?",
        "principles": [
            "A done answer must bind the post-change artifact hashes",
        ],
        "options": [
            {"name": "before", "pros": ["earlier"], "cons": ["stale hashes"]},
            {"name": "after", "pros": ["fresh binding"], "cons": ["slower"]},
        ],
        "chosen": "after",
        "rationale": (
            "The response carries the current input and artifact sha256 values, so "
            "writing it before the gates would bind stale bytes and the producer "
            "would see a mismatch on re-check; answering after the gates keeps the "
            "record consistent with what was actually verified."
        ),
        "evidence": [{"path": evidence_path}],
        "assumptions": ["gate JSON is current"],
        "unknowns": ["producer re-reads timing"],
        "risks": ["request may be revised again"],
        "revisit_when": "the producer sends a follow-up request",
    }


def _request(request_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 2,
        "system": "ux-creator",
        "id": request_id,
        "target_agent": "circuit",
        "stage": "design",
        "risk": "low",
        "purpose": "Update the board connector placement for the new enclosure.",
        "rationale": "",
        "requested_changes": ["Move J2 to the east edge."],
        "inputs": [],
        "expected_deliverables": ["board.kicad_pcb"],
        "acceptance": ["DRC passes with zero violations."],
        "depends_on": [],
        "created_at": "2026-10-05T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def _write_request(root: Path, request_id: str, **overrides: Any) -> Path:
    directory = root / "liaison"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{request_id}.ux-request.json"
    path.write_text(json.dumps(_request(request_id, **overrides)), encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENHANDS_PROJECT_DIR", str(tmp_path))
    return tmp_path


def _input_file(root: Path, name: str = "input.txt") -> dict[str, str]:
    path = root / name
    path.write_text("payload\n", encoding="utf-8")
    return {"path": name, "sha256": hashlib.sha256(b"payload\n").hexdigest()}


def test_inbox_new_request(workspace: Path) -> None:
    _write_request(workspace, "move-j2")
    inbox = liaison.ux_inbox()
    assert len(inbox.requests) == 1
    entry = inbox.requests[0]
    assert entry.id == "move-j2"
    assert entry.state == "new"
    assert entry.path == "liaison/move-j2.ux-request.json"
    assert inbox.malformed == []


def test_inbox_ignored_other_targets(workspace: Path) -> None:
    _write_request(workspace, "for-wire", target_agent="wire")
    inbox = liaison.ux_inbox()
    assert inbox.requests == []
    assert inbox.other_targets == 1


def test_inbox_answered(workspace: Path) -> None:
    _write_request(workspace, "move-j2")
    liaison.ux_respond(
        {
            "request": "move-j2",
            "status": "accepted",
            "reason": "",
        },
        workspace,
    )
    inbox = liaison.ux_inbox()
    assert inbox.requests[0].state == "answered"
    assert inbox.requests[0].response_status == "accepted"


def test_inbox_stale_via_changed_input(workspace: Path) -> None:
    item = _input_file(workspace)
    _write_request(workspace, "move-j2", inputs=[item])
    (workspace / "input.txt").write_text("changed\n", encoding="utf-8")
    inbox = liaison.ux_inbox()
    assert inbox.requests[0].state == "stale"
    assert inbox.requests[0].stale_inputs == ["input.txt"]


def test_inbox_stale_via_response_input_hashes(workspace: Path) -> None:
    item = _input_file(workspace)
    _write_request(workspace, "move-j2", inputs=[item])
    liaison.ux_respond({"request": "move-j2", "status": "accepted"}, workspace)
    (workspace / "input.txt").write_text("changed\n", encoding="utf-8")
    inbox = liaison.ux_inbox()
    assert inbox.requests[0].state == "stale"


def test_inbox_blocked_by_unanswered_dependency(workspace: Path) -> None:
    _write_request(workspace, "move-j2", depends_on=["mech-envelope"])
    inbox = liaison.ux_inbox()
    assert inbox.requests[0].state == "blocked"
    assert inbox.requests[0].unanswered_dependencies == ["mech-envelope"]


def test_inbox_blocked_unblocks_on_other_responder(workspace: Path) -> None:
    _write_request(workspace, "move-j2", depends_on=["mech-envelope"])
    response = {
        "schema_version": 2,
        "system": "ux-creator",
        "request": "mech-envelope",
        "responder": "mech",
        "status": "done",
        "reason": "Envelope delivered with margin data attached.",
        "responded_at": "2026-10-05T00:00:00+00:00",
    }
    (workspace / "liaison" / "mech-envelope.ux-response.json").write_text(
        json.dumps(response), encoding="utf-8"
    )
    inbox = liaison.ux_inbox()
    assert inbox.requests[0].state == "new"


def test_inbox_malformed_files_reported(workspace: Path) -> None:
    directory = workspace / "liaison"
    directory.mkdir()
    (directory / "bad-json.ux-request.json").write_text("{oops", encoding="utf-8")
    (directory / "mismatch.ux-request.json").write_text(
        json.dumps(_request("other-id")), encoding="utf-8"
    )
    (directory / "extra.ux-request.json").write_text(
        json.dumps(_request("extra") | {"surprise": 1}), encoding="utf-8"
    )
    (directory / "old.ux-request.json").write_text(
        json.dumps({"version": 1, "id": "old"}), encoding="utf-8"
    )
    inbox = liaison.ux_inbox()
    for malformed_entry in inbox.malformed:
        assert not Path(malformed_entry.path).is_absolute()
        assert malformed_entry.path.startswith("liaison/")
    assert {Path(m.path).stem for m in inbox.malformed} == {
        "bad-json.ux-request",
        "mismatch.ux-request",
        "extra.ux-request",
        "old.ux-request",
    }
    assert inbox.requests == []


def _make_refs(workspace: Path) -> tuple[str, str]:
    (workspace / "out").mkdir(exist_ok=True)
    (workspace / "out" / "board.kicad_pcb").write_text("(kicad_pcb)\n", encoding="utf-8")
    decision = records.record_decision(_decision("out/board.kicad_pcb"), workspace)
    impression = records.record_impression(
        {"stage": "layout", "artifacts": ["out"], "impression": IMPRESSION}, workspace
    )
    return (
        cast(str, decision["record"]["event_id"]),
        cast(str, impression["record"]["event_id"]),
    )


def test_respond_done_happy_path(workspace: Path) -> None:
    item = _input_file(workspace)
    _write_request(workspace, "move-j2", inputs=[item])
    decision_id, impression_id = _make_refs(workspace)
    result = liaison.ux_respond(
        {
            "request": "move-j2",
            "status": "done",
            "reason": "Connector moved to the east edge; DRC clean.",
            "artifacts": ["out/board.kicad_pcb"],
            "gate_verdicts": [{"gate": "drc", "verdict": "pass"}],
            "decision_refs": [decision_id],
            "impression_refs": [impression_id],
        },
        workspace,
    )
    assert Path(result.path).is_file()
    written = json.loads(Path(result.path).read_text(encoding="utf-8"))
    assert written["responder"] == "circuit"
    assert written["status"] == "done"
    assert written["input_hashes"]["input.txt"] == item["sha256"]
    assert liaison.ux_inbox().requests[0].state == "answered"


def test_respond_refuses_done_with_bad_verdict(workspace: Path) -> None:
    _write_request(workspace, "move-j2")
    _make_refs(workspace)
    decision_id, impression_id = _last_ids(workspace)
    with pytest.raises(ValueError, match="fail or unknown"):
        liaison.ux_respond(
            {
                "request": "move-j2",
                "status": "done",
                "reason": "DRC still reports clearance violations.",
                "gate_verdicts": [{"gate": "drc", "verdict": "fail"}],
                "decision_refs": [decision_id],
                "impression_refs": [impression_id],
            },
            workspace,
        )


def _last_ids(workspace: Path) -> tuple[str, str]:
    directory = workspace / "observations" / "circuit"
    decisions = (directory / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    impressions = (directory / "impressions.jsonl").read_text(encoding="utf-8").splitlines()
    return json.loads(decisions[-1])["event_id"], json.loads(impressions[-1])["event_id"]


def _done_payload(workspace: Path) -> dict[str, Any]:
    decision_id, impression_id = _make_refs(workspace)
    return {
        "request": "move-j2",
        "status": "done",
        "reason": "Connector moved to the east edge; DRC clean.",
        "artifacts": ["out/board.kicad_pcb"],
        "gate_verdicts": [{"gate": "drc", "verdict": "pass"}],
        "decision_refs": [decision_id],
        "impression_refs": [impression_id],
    }


def test_respond_refuses_done_without_gate_verdicts(workspace: Path) -> None:
    _write_request(workspace, "move-j2")
    payload = _done_payload(workspace)
    payload["gate_verdicts"] = []
    with pytest.raises(ValueError, match="gate_verdict"):
        liaison.ux_respond(payload, workspace)


def test_respond_refuses_done_without_artifacts(workspace: Path) -> None:
    _write_request(workspace, "move-j2")
    payload = _done_payload(workspace)
    payload["artifacts"] = []
    with pytest.raises(ValueError, match="artifact"):
        liaison.ux_respond(payload, workspace)


def test_respond_refuses_done_without_decision_refs(workspace: Path) -> None:
    _write_request(workspace, "move-j2")
    payload = _done_payload(workspace)
    payload["decision_refs"] = []
    with pytest.raises(ValueError, match="decision_ref"):
        liaison.ux_respond(payload, workspace)


def test_respond_refuses_done_without_impression_refs(workspace: Path) -> None:
    _write_request(workspace, "move-j2")
    payload = _done_payload(workspace)
    payload["impression_refs"] = []
    with pytest.raises(ValueError, match="impression_ref"):
        liaison.ux_respond(payload, workspace)


def test_respond_refuses_nonexistent_artifact(workspace: Path) -> None:
    _write_request(workspace, "move-j2")
    with pytest.raises(ValueError, match="does not exist"):
        liaison.ux_respond(
            {
                "request": "move-j2",
                "status": "in_progress",
                "artifacts": ["missing/board.kicad_pcb"],
            },
            workspace,
        )


def test_respond_refuses_done_with_missing_input(workspace: Path) -> None:
    item = _input_file(workspace)
    _write_request(workspace, "move-j2", inputs=[item])
    (workspace / "input.txt").unlink()
    payload = _done_payload(workspace)
    with pytest.raises(ValueError, match="missing"):
        liaison.ux_respond(payload, workspace)


def test_respond_missing_input_omitted_for_other_statuses(workspace: Path) -> None:
    item = _input_file(workspace)
    _write_request(workspace, "move-j2", inputs=[item])
    (workspace / "input.txt").unlink()
    result = liaison.ux_respond(
        {
            "request": "move-j2",
            "status": "rejected",
            "reason": "Cannot evaluate the change: the input file is missing.",
        },
        workspace,
    )
    assert result.response.input_hashes == {}
    written = json.loads(Path(result.path).read_text(encoding="utf-8"))
    assert written["input_hashes"] == {}


def test_respond_rejected_needs_reason(workspace: Path) -> None:
    _write_request(workspace, "move-j2")
    with pytest.raises(ValueError, match="reason"):
        liaison.ux_respond(
            {"request": "move-j2", "status": "rejected", "reason": "short"},
            workspace,
        )
    ok = liaison.ux_respond(
        {"request": "move-j2", "status": "accepted", "reason": ""},
        workspace,
    )
    assert ok.response.status == "accepted"


def test_respond_unknown_request(workspace: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        liaison.ux_respond({"request": "ghost", "status": "accepted"}, workspace)


def test_mcp_ux_tools(workspace: Path) -> None:
    from circuit import mcp_server

    _write_request(workspace, "move-j2")
    result = asyncio.run(mcp_server.call_tool("circuit_ux_inbox", {}))
    assert result.isError is False
    payload = json.loads(cast(TextContent, result.content[0]).text)
    assert payload["requests"][0]["id"] == "move-j2"
    result = asyncio.run(
        mcp_server.call_tool("circuit_ux_respond", {"request": "move-j2", "status": "accepted"})
    )
    assert result.isError is False
    written = json.loads(
        (workspace / "liaison" / "move-j2.ux-response.json").read_text(encoding="utf-8")
    )
    assert written["status"] == "accepted"


def test_cli_ux_inbox_and_respond(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_request(workspace, "move-j2")
    assert cli.main(["ux", "inbox"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requests"][0]["state"] == "new"
    response_file = workspace / "response.json"
    response_file.write_text(
        json.dumps({"request": "move-j2", "status": "accepted"}), encoding="utf-8"
    )
    assert cli.main(["ux", "respond", "--json", str(response_file)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "pass"
    assert (workspace / "liaison" / "move-j2.ux-response.json").is_file()
