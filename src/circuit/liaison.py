"""Sister Liaison Protocol (SLP) v2 — circuit's local strict mirror.

UX-creator writes ``liaison/<id>.ux-request.json``; this plugin answers
with ``liaison/<id>.ux-response.json`` beside it. This module is a local
mirror of the v2 contract — it imports no UX-creator code. Requests and
responses are strict (extra fields are forbidden); malformed files are
reported, never raised, by ``ux_inbox``.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .records import records_dir, sha256_file, tree_sha256
from .workspace import workspace_path, workspace_root

SLUG = r"^[a-z0-9][a-z0-9._-]{0,63}$"
SHA256_RE = r"^[0-9a-f]{64}$"
REQUEST_SUFFIX = ".ux-request.json"
RESPONSE_SUFFIX = ".ux-response.json"
RESPONDER: str = "circuit"

TARGET_AGENTS = (
    "bard",
    "circuit",
    "dashboard",
    "doc",
    "firmware",
    "fpga",
    "mech",
    "prodeng",
    "sim",
    "wire",
)

Stages = Literal[
    "requirements",
    "design",
    "manufacturing_handoff",
    "build",
    "evaluation",
    "revision",
]
Statuses = Literal[
    "accepted",
    "in_progress",
    "done",
    "rejected",
    "deferred",
    "needs_info",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputRef(_Strict):
    path: str = Field(min_length=1, description="Workspace-relative input path")
    sha256: str = Field(pattern=SHA256_RE)

    @field_validator("path")
    @classmethod
    def _relative_only(cls, value: str) -> str:
        if Path(value).is_absolute() or ".." in Path(value).parts:
            raise ValueError("input path must be workspace-relative without '..'")
        return value


class ArtifactOut(_Strict):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_RE)


class GateVerdict(_Strict):
    gate: str = Field(min_length=1)
    verdict: Literal["pass", "fail", "unknown"]


class UxRequestV2(_Strict):
    schema_version: Literal[2]
    system: Literal["ux-creator"]
    id: str = Field(pattern=SLUG)
    target_agent: Literal[
        "bard",
        "circuit",
        "dashboard",
        "doc",
        "firmware",
        "fpga",
        "mech",
        "prodeng",
        "sim",
        "wire",
    ]
    stage: Stages
    risk: Literal["low", "high"]
    purpose: str = Field(min_length=20)
    rationale: str = ""
    requested_changes: list[str] = Field(min_length=1)
    inputs: list[InputRef] = Field(default_factory=list[InputRef])
    expected_deliverables: list[str] = Field(min_length=1)
    acceptance: list[str] = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list[str])
    created_at: AwareDatetime

    @field_validator("requested_changes", "expected_deliverables", "acceptance")
    @classmethod
    def _non_empty_items(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("list items must be non-empty")
        return value

    @model_validator(mode="after")
    def _high_risk_needs_rationale(self) -> UxRequestV2:
        if self.risk == "high" and not re.search(r"\b[a-z][a-z0-9_]*\b", self.rationale.strip()):
            raise ValueError("high-risk requests must cite a UX job id in rationale")
        return self


class UxResponseV2(_Strict):
    schema_version: Literal[2]
    system: Literal["ux-creator"]
    request: str = Field(pattern=SLUG)
    responder: str = Field(pattern=SLUG)
    status: Statuses
    reason: str = ""
    input_hashes: dict[str, str] = Field(default_factory=dict[str, str])
    artifacts: list[ArtifactOut] = Field(default_factory=list[ArtifactOut])
    gate_verdicts: list[GateVerdict] = Field(default_factory=list[GateVerdict])
    decision_refs: list[str] = Field(default_factory=list[str])
    impression_refs: list[str] = Field(default_factory=list[str])
    questions_for_user: list[str] = Field(default_factory=list[str])
    responded_at: AwareDatetime

    @model_validator(mode="after")
    def _reason_required_unless_acknowledged(self) -> UxResponseV2:
        if self.status not in ("accepted", "in_progress") and len(self.reason.strip()) < 20:
            raise ValueError("reason needs at least 20 characters for this status")
        return self


class MalformedEntry(_Strict):
    path: str
    error: str


class InboxEntry(_Strict):
    id: str
    path: str
    stage: str
    risk: str
    purpose: str
    requested_changes: list[str]
    expected_deliverables: list[str]
    acceptance: list[str]
    depends_on: list[str]
    state: Literal["new", "answered", "stale", "blocked"]
    response_status: str | None = None
    stale_inputs: list[str] = Field(default_factory=list[str])
    unanswered_dependencies: list[str] = Field(default_factory=list[str])


class UxInboxResult(_Strict):
    verdict: Literal["pass"] = "pass"
    requests: list[InboxEntry] = Field(default_factory=list[InboxEntry])
    malformed: list[MalformedEntry] = Field(default_factory=list[MalformedEntry])
    other_targets: int = 0


def _liaison_dir(root: Path) -> Path:
    return root / "liaison"


def _load_request(path: Path) -> UxRequestV2:
    raw = json.loads(path.read_text(encoding="utf-8"))
    request = UxRequestV2.model_validate(raw)
    if request.id != path.name.removesuffix(REQUEST_SUFFIX):
        raise ValueError(f"request id {request.id!r} does not match file stem")
    return request


def _load_response(path: Path, responder: str | None = None) -> UxResponseV2:
    raw = json.loads(path.read_text(encoding="utf-8"))
    response = UxResponseV2.model_validate(raw)
    if responder is not None and response.responder != responder:
        raise ValueError(f"response is from {response.responder!r}, not {responder!r}")
    return response


def _current_input_hashes(request: UxRequestV2, root: Path) -> dict[str, str]:
    """sha256 of each request input; a missing input hashes as changed."""
    hashes: dict[str, str] = {}
    for item in request.inputs:
        try:
            resolved = workspace_path(item.path, root)
        except ValueError:
            hashes[item.path] = ""
            continue
        if resolved.is_file():
            hashes[item.path] = sha256_file(resolved)
        elif resolved.is_dir():
            hashes[item.path] = tree_sha256(resolved)
        else:
            hashes[item.path] = ""
    return hashes


def ux_inbox(root: Path | None = None) -> UxInboxResult:
    """Scan ``<root>/liaison/`` for requests targeting circuit."""
    base = (root or workspace_root()).resolve()
    directory = _liaison_dir(base)
    result = UxInboxResult()
    if not directory.is_dir():
        return result

    requests: dict[str, UxRequestV2] = {}
    request_paths: dict[str, Path] = {}
    malformed: list[MalformedEntry] = []
    responses: dict[str, UxResponseV2] = {}
    other_targets = 0

    for path in sorted(directory.glob(f"*{REQUEST_SUFFIX}")):
        try:
            request = _load_request(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            malformed.append(MalformedEntry(path=str(path), error=str(exc)))
            continue
        if request.target_agent != RESPONDER:
            other_targets += 1
            continue
        requests[request.id] = request
        request_paths[request.id] = path

    for path in sorted(directory.glob(f"*{RESPONSE_SUFFIX}")):
        stem = path.name.removesuffix(RESPONSE_SUFFIX)
        try:
            response = _load_response(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            malformed.append(MalformedEntry(path=str(path), error=str(exc)))
            continue
        if response.request == stem:
            responses.setdefault(stem, response)
        else:
            malformed.append(
                MalformedEntry(
                    path=str(path),
                    error=f"response.request {response.request!r} does not match file stem",
                )
            )

    entries: list[InboxEntry] = []
    for request_id, request in sorted(requests.items()):
        path = request_paths[request_id]
        current = _current_input_hashes(request, base)
        stale_inputs = [
            item.path for item in request.inputs if current.get(item.path) != item.sha256
        ]
        own_response = responses.get(request_id)
        response_status = own_response.status if own_response else None
        if own_response is not None and own_response.responder != RESPONDER:
            own_response = None
        if own_response is not None and own_response.input_hashes:
            stale_inputs = sorted(
                set(stale_inputs)
                | {
                    path_key
                    for path_key, digest in own_response.input_hashes.items()
                    if current.get(path_key) != digest
                }
            )
        unanswered = [dep for dep in request.depends_on if dep not in responses]
        if stale_inputs:
            state = "stale"
        elif own_response is not None:
            state = "answered"
        elif unanswered:
            state = "blocked"
        else:
            state = "new"
        entries.append(
            InboxEntry(
                id=request_id,
                path=str(path),
                stage=request.stage,
                risk=request.risk,
                purpose=request.purpose,
                requested_changes=request.requested_changes,
                expected_deliverables=request.expected_deliverables,
                acceptance=request.acceptance,
                depends_on=request.depends_on,
                state=state,
                response_status=response_status,
                stale_inputs=stale_inputs,
                unanswered_dependencies=unanswered,
            )
        )
    return UxInboxResult(
        requests=entries,
        malformed=malformed,
        other_targets=other_targets,
    )


def _record_event_ids(root: Path) -> tuple[set[str], set[str]]:
    """Known event_ids in the decisions and impressions/vision-review logs."""
    directory = records_dir(root)
    decisions: set[str] = set()
    impressions: set[str] = set()
    for line in (
        (directory / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        if (directory / "decisions.jsonl").is_file()
        else []
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        event_id = cast(dict[str, Any], record).get("event_id")
        if isinstance(event_id, str):
            decisions.add(event_id)
    for name in ("impressions.jsonl", "vision-reviews.jsonl"):
        path = directory / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            event_id = cast(dict[str, Any], record).get("event_id")
            if isinstance(event_id, str):
                impressions.add(event_id)
    return decisions, impressions


class UxRespondResult(_Strict):
    verdict: Literal["pass"] = "pass"
    path: str
    sha256: str = Field(pattern=SHA256_RE)
    response: UxResponseV2


def _list_field(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return cast(list[Any], value)


def ux_respond(payload: dict[str, Any], root: Path | None = None) -> UxRespondResult:
    """Validate and write ``liaison/<id>.ux-response.json`` for a request."""
    base = (root or workspace_root()).resolve()
    request_id = str(payload.get("request") or "")
    if not re.match(SLUG, request_id):
        raise ValueError(f"request must be a request id slug, got {request_id!r}")
    request_path = _liaison_dir(base) / f"{request_id}{REQUEST_SUFFIX}"
    if not request_path.is_file():
        raise ValueError(f"ux request not found: liaison/{request_id}{REQUEST_SUFFIX}")
    request = _load_request(request_path)
    if request.target_agent != RESPONDER:
        raise ValueError(f"request {request_id!r} targets {request.target_agent!r}")

    status = str(payload.get("status") or "")
    if status not in get_args(Statuses):
        raise ValueError(f"unknown ux response status {status!r}")
    reason = str(payload.get("reason") or "")
    if status not in ("accepted", "in_progress") and len(reason.strip()) < 20:
        raise ValueError("reason needs at least 20 characters for this status")

    input_hashes = _current_input_hashes(request, base)
    missing = [item.path for item in request.inputs if not (base / item.path).exists()]
    if missing:
        raise ValueError(f"request inputs are missing from the workspace: {missing}")

    if status == "done" and (
        stale := [
            item.path for item in request.inputs if input_hashes.get(item.path) != item.sha256
        ]
    ):
        raise ValueError(f"request inputs changed since the request was written: {stale}")

    artifacts: list[ArtifactOut] = []
    artifact_values: list[Any] = _list_field(payload, "artifacts")
    for value in artifact_values:
        artifact_path = workspace_path(str(value), base)
        if not artifact_path.exists():
            raise ValueError(f"artifact does not exist: {value}")
        artifacts.append(
            ArtifactOut(
                path=artifact_path.relative_to(base).as_posix(),
                sha256=tree_sha256(artifact_path),
            )
        )

    gate_verdicts = [
        GateVerdict.model_validate(item) for item in _list_field(payload, "gate_verdicts")
    ]
    if status == "done":
        if any(v.verdict != "pass" for v in gate_verdicts):
            raise ValueError(
                "cannot answer 'done' while a gate verdict is fail or unknown; "
                "change status to needs_info or rejected with a reason"
            )
        if not gate_verdicts and not artifacts:
            raise ValueError("a 'done' response needs gate verdicts or artifacts as evidence")

    decision_refs = [str(v) for v in _list_field(payload, "decision_refs")]
    impression_refs = [str(v) for v in _list_field(payload, "impression_refs")]
    known_decisions, known_impressions = _record_event_ids(base)
    bad_decisions = [ref for ref in decision_refs if ref not in known_decisions]
    if bad_decisions:
        raise ValueError(f"decision_refs not found in decisions.jsonl: {bad_decisions}")
    bad_impressions = [ref for ref in impression_refs if ref not in known_impressions]
    if bad_impressions:
        raise ValueError(
            f"impression_refs not found in impressions.jsonl or vision-reviews.jsonl: "
            f"{bad_impressions}"
        )
    if status == "done" and not decision_refs and not impression_refs:
        raise ValueError(
            "a 'done' response needs at least one decision_ref or impression_ref "
            "linking the reasoning recorded for this work"
        )

    response = UxResponseV2(
        schema_version=2,
        system="ux-creator",
        request=request_id,
        responder=RESPONDER,
        status=cast(Statuses, status),
        reason=reason,
        input_hashes=input_hashes,
        artifacts=artifacts,
        gate_verdicts=gate_verdicts,
        decision_refs=decision_refs,
        impression_refs=impression_refs,
        questions_for_user=[str(q) for q in _list_field(payload, "questions_for_user")],
        responded_at=datetime.now(UTC),
    )
    out_path = _liaison_dir(base) / f"{request_id}{RESPONSE_SUFFIX}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(response.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return UxRespondResult(
        path=str(out_path),
        sha256=sha256_file(out_path),
        response=response,
    )
