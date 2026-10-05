from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

ROOT = Path(__file__).parents[1]
TRANSCRIPT_ROOT = ROOT / "tests" / "data" / "e2e_replay"
SCRIPT = ROOT / "scripts" / "run_library_e2e_replay.py"


def _replay_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("library_e2e_replay_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _steps(name: str) -> list[dict[str, object]]:
    path = TRANSCRIPT_ROOT / name
    records = [
        cast(dict[str, object], json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records[0]["type"] == "scenario"
    assert records[0]["label"] == "synthetic"
    return records[1:]


def test_synthetic_scenarios_cover_required_dispatch_flows() -> None:
    happy = _steps("tps62130-happy.jsonl")
    happy_sequence = [cast(str, step["tool"]) for step in happy if step.get("type") == "tool"]
    happy_tools = set(happy_sequence)
    assert {
        "circuit_human_request_create",
        "circuit_datasheet_check_received",
        "circuit_datasheet_extract",
        "circuit_vision_read",
        "circuit_vision_answer",
        "circuit_part_author_commit",
        "circuit_part_author_compare",
        "circuit_part_spec_check",
        "circuit_footprint_write",
        "circuit_symbol_write",
        "circuit_library_verify",
        "circuit_library_review_packet",
        "circuit_library_review_status",
    }.issubset(happy_tools)
    required_order = [
        "circuit_human_request_create",
        "circuit_datasheet_check_received",
        "circuit_datasheet_extract",
        "circuit_vision_read",
        "circuit_vision_answer",
        "circuit_part_author_commit",
        "circuit_part_author_compare",
        "circuit_part_spec_check",
        "circuit_footprint_write",
        "circuit_symbol_write",
        "circuit_library_verify",
        "circuit_library_review_packet",
        "circuit_library_review_status",
    ]
    positions = [happy_sequence.index(tool) for tool in required_order]
    assert positions == sorted(positions)

    nda = _steps("nda-blocked.jsonl")
    assert {cast(str, step["tool"]) for step in nda if step.get("type") == "tool"} == {
        "circuit_human_request_create",
        "circuit_human_request_status",
    }
    assert any(
        step.get("type") == "human_event" and step.get("decision") == "unavailable" for step in nda
    )
    substitute = _steps("substitute-permission.jsonl")
    assert {cast(str, step["tool"]) for step in substitute if step.get("type") == "tool"} == {
        "circuit_human_request_create",
        "circuit_human_request_status",
    }
    decisions = [step.get("decision") for step in substitute if step.get("type") == "human_event"]
    assert decisions == ["deny", "grant"]


def test_connector_download_blocked_replay_fails_closed() -> None:
    replay = cast(Any, _replay_module())
    transcript_path = TRANSCRIPT_ROOT / "connector-download-blocked.jsonl"
    steps = _steps(transcript_path.name)
    fetch = next(step for step in steps if step.get("type") == "fetch_response")
    request_step = next(step for step in steps if step.get("id") == "request")
    request = cast(dict[str, object], cast(dict[str, object], request_step["arguments"])["request"])
    required_fields = {
        "reason",
        "evidence",
        "known",
        "unknown",
        "agent_assessment",
        "recommendation",
        "recommendation_rationale",
        "alternatives",
    }
    assert required_fields <= request.keys()
    assert all(
        isinstance(alternative, dict) and "risks" in cast(dict[str, object], alternative)
        for alternative in cast(list[object], request["alternatives"])
    )
    assert fetch["content_type"] == "text/html"
    assert cast(str, fetch["body"]).startswith("<!DOCTYPE html>")
    tool_names = {cast(str, step["tool"]) for step in steps if step.get("type") == "tool"}
    assert tool_names == {
        "circuit_human_request_create",
        "circuit_datasheet_check_received",
    }

    result = replay.run_scenario(transcript_path, ROOT)
    results = cast(dict[str, object], result["results"])
    assert result["label"] == "synthetic"
    assert cast(dict[str, object], results["download"])["content_type"] == "text/html"
    intake = cast(list[dict[str, object]], results["intake"])
    assert intake[0]["code"] == "datasheet_extraction_failed"
    assert intake[0]["severity"] == "error"
    assert cast(dict[str, object], results["request"])["request_id"]


def test_live_mode_requires_explicit_environment_opt_in() -> None:
    environment = os.environ.copy()
    environment.pop("CIRCUIT_E2E_LIVE", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--live", "--transcripts", str(TRANSCRIPT_ROOT)],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert result.returncode == 2
    assert "--live requires CIRCUIT_E2E_LIVE=1" in result.stderr


def test_transcript_references_resolve_and_escape_paths_fail_closed(tmp_path: Path) -> None:
    replay = cast(Any, _replay_module())
    project = tmp_path / "project"
    project.mkdir()
    replay._ACTIVE_PROJECT.set(project)
    results = {"request": {"request_id": "1234567890abcdef"}}
    assert replay._reference({"$ref": "request#/request_id"}, results) == "1234567890abcdef"
    assert replay._project_file(project, "candidate/file.json") == (
        project / "candidate" / "file.json"
    )
    try:
        replay._project_file(project, "../outside.json")
    except replay.ReplayError:
        pass
    else:
        raise AssertionError("transcript path traversal must fail closed")


def test_private_vision_control_answer_is_redacted() -> None:
    replay = cast(Any, _replay_module())
    redacted = replay._redact_private(
        {"nested": ["control answer is ABC123"]},
        ["ABC123"],
    )
    assert redacted == {"nested": ["control answer is [private-control-redacted]"]}
