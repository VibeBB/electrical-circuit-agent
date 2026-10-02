"""Hash-bound requests for human decisions and evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .advisory import impression_is_prose

HumanRequestKind = Literal[
    "datasheet_acquisition",
    "substitute_permission",
    "alternative_evidence",
    "library_review",
]
EvidenceKind = Literal[
    "page_image",
    "document",
    "hash",
    "check_result",
    "measurement",
    "photo",
    "note",
]
SubstituteScope = Literal[
    "package_dimensions",
    "land_pattern",
    "pinout",
    "pin_table",
    "model3d",
    "orderable",
]
RequiredSection = Literal[
    "package_drawing",
    "pinout",
    "pin_table",
    "land_pattern",
    "orderable_table",
]
_NonEmpty = Annotated[StrictStr, Field(min_length=1)]
_SHA256 = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
_REQUEST_ID = re.compile(r"^[0-9a-f]{16}$")
_REQUEST_SHA = re.compile(r"^[0-9a-f]{64}$")
_EVENTS_DIR_ENV = "CIRCUIT_AGENT_EVENTS_DIR"
_DEFAULT_EVENTS_ROOT = Path(".openhands/agent-canvas/dev_conversations")
_RESPONSE_LINE = re.compile(r"^CIRCUIT-HUMAN-RESPONSE ([0-9a-f]{16})$")


class HumanRequestError(ValueError):
    """Raised when a stored request or response binding is invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _clean_strings(value: Any) -> Any:
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("string fields must not be empty")
        return cleaned
    if isinstance(value, list):
        return [_clean_strings(item) for item in cast(list[Any], value)]
    if isinstance(value, dict):
        return {key: _clean_strings(item) for key, item in cast(dict[str, Any], value).items()}
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator("*", mode="before")
    @classmethod
    def strip_strings(cls, value: Any) -> Any:
        return _clean_strings(value)


class RequestSubject(_StrictModel):
    manufacturer: _NonEmpty
    mpn: _NonEmpty
    revision: _NonEmpty | None = None


class RequestEvidence(_StrictModel):
    kind: EvidenceKind
    ref: _NonEmpty
    sha256: _SHA256 | None = None
    summary: _NonEmpty


class RequestAlternative(_StrictModel):
    option: _NonEmpty
    risks: list[_NonEmpty] = Field(min_length=1)


class DatasheetAcquisitionDetails(_StrictModel):
    kind: Literal["datasheet_acquisition"]
    failure_reason: Literal[
        "access_blocked",
        "not_found",
        "nda",
        "custom_part",
        "mpn_mismatch",
        "revision_mismatch",
        "sections_missing",
    ]
    requested_revision: _NonEmpty | None = None
    required_sections: list[RequiredSection] = Field(min_length=1)
    attempted_sources: list[_NonEmpty]
    current_source_sha256: _SHA256 | None = None
    optional_cad_requested: bool


class SubstitutePermissionDetails(_StrictModel):
    kind: Literal["substitute_permission"]
    target_mpn: _NonEmpty
    substitute_mpn: _NonEmpty
    target_source_sha256: _SHA256 | None = None
    substitute_datasheet_sha256: _SHA256
    requested_scope: list[SubstituteScope] = Field(min_length=1)
    unverifiable_fields: list[_NonEmpty]
    similarity_evidence: list[_NonEmpty] = Field(min_length=1)


class AlternativeEvidenceFile(_StrictModel):
    path: _NonEmpty
    sha256: _SHA256


class AlternativeEvidenceDetails(_StrictModel):
    kind: Literal["alternative_evidence"]
    evidence_kind: Literal[
        "measurement",
        "photo",
        "old_revision",
        "manufacturer_correspondence",
        "other_document",
    ]
    files: list[AlternativeEvidenceFile] = Field(min_length=1)
    covers: list[_NonEmpty] = Field(min_length=1)
    unknown_fields: list[_NonEmpty] = Field(default_factory=list)
    measurement_method: _NonEmpty | None = None


class LibraryReviewDetails(_StrictModel):
    kind: Literal["library_review"]
    packet_id: _NonEmpty | None = None
    finding_codes: list[_NonEmpty] = Field(default_factory=list)


RequestDetails = Annotated[
    DatasheetAcquisitionDetails
    | SubstitutePermissionDetails
    | AlternativeEvidenceDetails
    | LibraryReviewDetails,
    Field(discriminator="kind"),
]


class _HumanRequestFields(_StrictModel):
    artifact_kind: Literal["circuit_human_request"] = "circuit_human_request"
    kind: HumanRequestKind
    created_at: datetime
    subject: RequestSubject
    reason: _NonEmpty
    evidence: list[RequestEvidence] = Field(min_length=1)
    known: list[_NonEmpty] = Field(min_length=1)
    unknown: list[_NonEmpty] = Field(default_factory=list)
    agent_assessment: _NonEmpty
    recommendation: _NonEmpty
    recommendation_rationale: _NonEmpty
    alternatives: list[RequestAlternative] = Field(min_length=2)
    recommended: int = Field(ge=0)
    details: RequestDetails

    @field_validator("created_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")
        return value.astimezone(UTC)

    @field_validator("agent_assessment")
    @classmethod
    def require_assessment_prose(cls, value: str) -> str:
        return impression_is_prose(value)

    @model_validator(mode="after")
    def validate_request_content(self) -> _HumanRequestFields:
        if self.kind != self.details.kind:
            raise ValueError("details.kind must match HumanRequest.kind")
        if self.kind != "library_review" and not self.unknown:
            raise ValueError("unknown must contain at least one item for non-review requests")
        if self.recommended >= len(self.alternatives):
            raise ValueError("recommended must identify an existing alternative")
        if self.alternatives[self.recommended].option != self.recommendation:
            raise ValueError("recommended alternative must match recommendation")
        return self


class HumanRequest(_HumanRequestFields):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{16}$")]
    request_sha256: _SHA256

    @model_validator(mode="after")
    def validate_request_hash(self) -> HumanRequest:
        payload = self.model_dump(
            mode="json",
            exclude={"request_id", "request_sha256"},
        )
        digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
        if self.request_sha256 != digest or self.request_id != digest[:16]:
            raise ValueError("human_request_tampered: canonical request hash mismatch")
        return self


class HumanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    request_sha256: str
    decision: str | None = None
    reviewer: str = ""
    fields: dict[str, str] = Field(default_factory=dict)
    event_path: Path | None = None
    event_sha256: str | None = None
    valid: bool
    reasons: list[str]


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def build_request(**fields: Any) -> HumanRequest:
    if "request_id" in fields or "request_sha256" in fields:
        raise ValueError("request_id and request_sha256 are computed by build_request")
    values = dict(fields)
    values.setdefault("created_at", datetime.now(UTC))
    content = _HumanRequestFields.model_validate(values)
    payload = content.model_dump(mode="json")
    digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return HumanRequest.model_validate_json(
        json.dumps(
            {
                **payload,
                "request_id": digest[:16],
                "request_sha256": digest,
            },
            ensure_ascii=False,
        )
    )


def _request_document(request: HumanRequest) -> str:
    return (
        json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


def _markdown(request: HumanRequest) -> str:
    lines = [
        f"# Human request {request.request_id}",
        "",
        "## Reason",
        "",
        request.reason,
        "",
        "## Evidence",
        "",
    ]
    for item in request.evidence:
        if item.kind == "page_image" and not Path(item.ref).is_absolute():
            lines.extend(
                [
                    f"![{item.summary}]({item.ref})",
                    "",
                    f"SHA-256: `{item.sha256 or 'not supplied'}`",
                    "",
                ]
            )
        else:
            digest = f" — SHA-256 `{item.sha256}`" if item.sha256 else ""
            lines.extend([f"- **{item.kind}** `{item.ref}` — {item.summary}{digest}", ""])
    lines.extend(["## Known", ""])
    lines.extend(f"- {item}" for item in request.known)
    lines.extend(["", "## Unknown", ""])
    lines.extend(f"- {item}" for item in request.unknown)
    if not request.unknown:
        lines.append("- None recorded.")
    lines.extend(
        [
            "",
            "## Agent assessment",
            "",
            request.agent_assessment,
            "",
            "## Recommendation + rationale",
            "",
            f"**Recommendation:** {request.recommendation}",
            "",
            request.recommendation_rationale,
            "",
            "## Alternatives and risks",
            "",
        ]
    )
    for index, alternative in enumerate(request.alternatives):
        marker = " **(recommended)**" if index == request.recommended else ""
        lines.append(f"- **{alternative.option}**{marker}")
        lines.extend(f"  - Risk: {risk}" for risk in alternative.risks)
    lines.extend(
        [
            "",
            "## How to respond",
            "",
            f"Start the first line with `CIRCUIT-HUMAN-RESPONSE {request.request_id}`.",
            "Add `decision: grant|deny|provided` and `reviewer: <your name>`.",
            "Include the request-specific response fields listed by the circuit agent.",
            "",
        ]
    )
    return "\n".join(lines)


def write_request(request: HumanRequest, project: Path) -> tuple[Path, Path]:
    directory = project / "library" / "requests"
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{request.request_id}.json"
    markdown_path = directory / f"{request.request_id}.md"
    for path, content in (
        (json_path, _request_document(request)),
        (markdown_path, _markdown(request)),
    ):
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
        except FileExistsError:
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise HumanRequestError(
                    "human_request_write_conflict",
                    f"cannot read existing request artifact {path}",
                ) from exc
            if existing != content:
                raise HumanRequestError(
                    "human_request_write_conflict",
                    f"existing request artifact differs: {path}",
                ) from None
    return json_path, markdown_path


def load_request(path: Path) -> HumanRequest:
    try:
        return HumanRequest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise HumanRequestError(
            "human_request_tampered",
            f"stored HumanRequest failed validation: {path}",
        ) from exc


def _message_text(value: Any, key: str = "") -> list[str]:
    if isinstance(value, str):
        return [value] if key in {"content", "message", "text"} else []
    if isinstance(value, list):
        return [text for child in cast(list[Any], value) for text in _message_text(child)]
    if isinstance(value, dict):
        record = cast(dict[str, Any], value)
        return [
            text
            for child_key, child in record.items()
            for text in _message_text(child, str(child_key))
        ]
    return []


def _events_root() -> Path:
    override = os.environ.get(_EVENTS_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / _DEFAULT_EVENTS_ROOT).resolve()


def _response_from_event(
    request: HumanRequest,
    event_path: Path | None,
    event_sha256: str | None,
    text: str | None,
    pointer_request_sha256: str | None,
    error: str | None,
) -> HumanResponse:
    reasons: list[str] = []
    if error is not None:
        reasons.append(error)
    fields: dict[str, str] = {}
    decision: str | None = None
    reviewer = ""
    if text is not None:
        lines = text.splitlines()
        first_line = lines[0].strip() if lines else ""
        match = _RESPONSE_LINE.fullmatch(first_line)
        if match is None or match.group(1) != request.request_id:
            reasons.append("response request id does not match")
        for line in lines[1:]:
            if not line.strip():
                continue
            key, separator, value = line.partition(":")
            if not separator or not key.strip() or not value.strip():
                reasons.append(f"malformed response line: {line}")
                continue
            normalized_key = key.strip().casefold()
            if normalized_key in fields:
                reasons.append(f"duplicate response field: {normalized_key}")
                continue
            fields[normalized_key] = value.strip()
        decision = fields.pop("decision", None)
        reviewer = fields.pop("reviewer", "")
        allowed = {
            "datasheet_acquisition": {"provided", "unavailable"},
            "substitute_permission": {"grant", "deny"},
            "alternative_evidence": {"grant", "deny"},
            "library_review": {"approve", "reject", "grant", "deny"},
        }[request.kind]
        if decision not in allowed:
            reasons.append(f"decision is invalid for {request.kind}")
        if not reviewer:
            reasons.append("reviewer is required")
        if (
            request.kind == "datasheet_acquisition"
            and decision == "unavailable"
            and not fields.get("reason")
        ):
            reasons.append("unavailable response requires reason")
        if (
            request.kind == "datasheet_acquisition"
            and decision == "provided"
            and fields.get("confidential", "").casefold() not in {"yes", "no"}
        ):
            reasons.append("provided response requires confidential: yes|no")
        if request.kind == "substitute_permission" and decision == "grant":
            for required in ("scope", "target", "substitute", "substitute_sha256"):
                if not fields.get(required):
                    reasons.append(f"substitute grant requires {required}")
        if (
            request.kind == "alternative_evidence"
            and decision == "grant"
            and not fields.get("covers")
        ):
            reasons.append("alternative-evidence grant requires covers")
    if pointer_request_sha256 != request.request_sha256:
        reasons.append("request hash changed after the response was recorded")
    return HumanResponse(
        request_id=request.request_id,
        request_sha256=request.request_sha256,
        decision=decision,
        reviewer=reviewer,
        fields=fields,
        event_path=event_path,
        event_sha256=event_sha256,
        valid=not reasons,
        reasons=reasons,
    )


def load_responses(project: Path, request: HumanRequest) -> list[HumanResponse]:
    directory = project / "library" / "requests" / "responses"
    if not directory.is_dir():
        return []
    request_path = project / "library" / "requests" / f"{request.request_id}.json"
    try:
        current = load_request(request_path)
        current_hash = current.request_sha256
    except HumanRequestError:
        current_hash = None
    responses: list[HumanResponse] = []
    for pointer_path in sorted(directory.glob(f"{request.request_id}.*.json")):
        event_path: Path | None = None
        event_sha256: str | None = None
        pointer_request_sha256: str | None = None
        text: str | None = None
        error: str | None = None
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            if not isinstance(pointer, dict):
                raise ValueError("response pointer is not an object")
            pointer_record = cast(dict[str, Any], pointer)
            if pointer_record.get("artifact_kind") != "circuit_human_response_pointer":
                raise ValueError("response pointer artifact kind is invalid")
            if pointer_record.get("request_id") != request.request_id:
                raise ValueError("response pointer request id is invalid")
            event_path = Path(str(pointer_record["event_path"])).resolve()
            event_sha256 = str(pointer_record["event_sha256"])
            pointer_request_sha256 = str(pointer_record["request_sha256"])
            events_root = _events_root()
            override = os.environ.get(_EVENTS_DIR_ENV)
            if override:
                if event_path.parent != events_root:
                    raise ValueError("response event is outside the trusted events directory")
            else:
                relative_event = event_path.relative_to(events_root)
                if len(relative_event.parts) < 3 or relative_event.parts[-2] != "events":
                    raise ValueError("response event is outside the trusted events directory")
            raw = event_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != event_sha256:
                raise ValueError("response event hash mismatch")
            if pointer_path.stem != f"{request.request_id}.{event_sha256[:12]}":
                raise ValueError("response pointer filename hash mismatch")
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError("response event is not user-authored")
            event_record = cast(dict[str, Any], event)
            if event_record.get("source") != "user":
                raise ValueError("response event is not user-authored")
            if current_hash is None or current_hash != request.request_sha256:
                raise ValueError("stored request hash is missing or changed")
            for candidate in _message_text(event_record):
                if candidate.splitlines() and candidate.splitlines()[0].strip().startswith(
                    "CIRCUIT-HUMAN-RESPONSE "
                ):
                    text = candidate
                    break
            if text is None:
                raise ValueError("response event has no HumanRequest response")
        except (KeyError, OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            error = str(exc)
        responses.append(
            _response_from_event(
                request,
                event_path,
                event_sha256,
                text,
                pointer_request_sha256,
                error,
            )
        )
    return responses
