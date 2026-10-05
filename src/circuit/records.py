"""Typed writers for the VibeBB Record Protocol (VRP) v1.

Every sibling plugin keeps the same three append-only logs under
``observations/<plugin>/`` in the workspace:

* ``decisions.jsonl`` — why a design choice was made, from first
  principles, with at least two options, evidence bound by sha256,
  assumptions, unknowns, residual risks and a revisit trigger;
* ``impressions.jsonl`` — a long-form impression at the end of every
  stage, bound to the artifacts it read (file or directory tree sha256);
* ``vision-reviews.jsonl`` — what the model thought after looking at an
  image, bound to the image bytes or to the vision tool event.

The plugin's Stop hook (``hooks/scripts/require_records.py``) re-validates
these lines with a stdlib mirror of the same rules and refuses to finish
a session that still owes records. Records are advisory evidence and
never change a deterministic gate verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .workspace import workspace_path, workspace_root

PLUGIN: Final = "circuit"
SCHEMA_VERSION: Final = 1
RECORDS_DIR: Final = Path("observations") / PLUGIN
IMPRESSION_MIN_CHARS: Final = 400
IMPRESSION_MIN_SENTENCES: Final = 3
RATIONALE_MIN_CHARS: Final = 200
PRINCIPLE_MIN_CHARS: Final = 12
QUESTION_MIN_CHARS: Final = 10
LOG_FILES: Final[dict[str, str]] = {
    "decision": "decisions.jsonl",
    "stage_impression": "impressions.jsonl",
    "vision_review": "vision-reviews.jsonl",
}
SKIP_PARTS: Final = frozenset({".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"})
_SLUG: Final = r"^[a-z0-9][a-z0-9._-]{0,63}$"
_SHA256: Final = r"^[0-9a-f]{64}$"
_CJK_END: Final = re.compile(r"[。\uff01\uff1f]")
_LATIN_END: Final = re.compile(r"[.!?](?=\s|$)")


def sentence_count(text: str) -> int:
    return len(_CJK_END.findall(text)) + len(_LATIN_END.findall(text))


def _sentences(text: str) -> set[str]:
    parts = re.split(r"(?<=[。\uff01\uff1f])|(?<=[.!?])(?=\s|$)", text)
    return {part.strip() for part in parts if part.strip()}


def impression_is_prose(value: str) -> str:
    """Reject a terse status line where a long-form impression is required."""
    text = value.strip()
    if len(text) < IMPRESSION_MIN_CHARS:
        raise ValueError(
            f"impression has {len(text)} characters; write at least {IMPRESSION_MIN_CHARS}"
            " (what you noticed, what works, what worries you, how a maker or user would"
            " read it, what to do next)"
        )
    if sentence_count(text) < IMPRESSION_MIN_SENTENCES:
        raise ValueError(f"impression must contain at least {IMPRESSION_MIN_SENTENCES} sentences")
    if len(_sentences(text)) < IMPRESSION_MIN_SENTENCES:
        raise ValueError("impression repeats the same sentence")
    return text


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_sha256(path: Path) -> str:
    """sha256 of a file, or of a directory's sorted (relative path, sha256) list."""
    if path.is_file():
        return sha256_file(path)
    digest = hashlib.sha256()
    for child in sorted(p for p in path.rglob("*") if p.is_file() and not p.is_symlink()):
        relative = child.relative_to(path)
        if any(part in SKIP_PARTS for part in relative.parts):
            continue
        digest.update(f"{relative.as_posix()}\0{sha256_file(child)}\n".encode())
    return digest.hexdigest()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactRef(_Strict):
    path: str = Field(min_length=1, description="Workspace-relative path")
    sha256: str = Field(pattern=_SHA256)


class EvidenceRef(_Strict):
    path: str | None = Field(default=None, description="Workspace-relative artifact path")
    sha256: str | None = Field(default=None, pattern=_SHA256)
    reference: str | None = Field(
        default=None, description="Standard, datasheet or law cited when there is no file"
    )

    @model_validator(mode="after")
    def _one_kind(self) -> EvidenceRef:
        if self.path is None and not (self.reference and self.reference.strip()):
            raise ValueError("evidence needs a path (bound by sha256) or a reference")
        if self.path is not None and self.sha256 is None:
            raise ValueError("file evidence must carry its sha256")
        return self


class DecisionOption(_Strict):
    name: str = Field(min_length=1)
    pros: list[str] = Field(min_length=1)
    cons: list[str] = Field(min_length=1)


class _DecisionBody(_Strict):
    id: str = Field(pattern=_SLUG, description="Stable decision id, e.g. 'circuit-vcc-decoupling'")
    stage: str = Field(pattern=_SLUG, description="Stage the decision belongs to")
    question: str = Field(min_length=QUESTION_MIN_CHARS)
    principles: list[str] = Field(
        min_length=1,
        description="First principles, physical laws or standards the choice rests on",
    )
    options: list[DecisionOption] = Field(min_length=2)
    chosen: str = Field(min_length=1)
    rationale: str = Field(min_length=RATIONALE_MIN_CHARS)
    assumptions: list[str] = Field(default_factory=list[str])
    unknowns: list[str] = Field(default_factory=list[str])
    risks: list[str] = Field(min_length=1)
    revisit_when: str = Field(min_length=1)
    decided_by: Literal["agent", "user"] = "agent"

    @field_validator("principles")
    @classmethod
    def _principles_are_cited(cls, value: list[str]) -> list[str]:
        if any(len(item.strip()) < PRINCIPLE_MIN_CHARS for item in value):
            raise ValueError(f"each principle needs at least {PRINCIPLE_MIN_CHARS} characters")
        return value

    @model_validator(mode="after")
    def _chosen_is_an_option(self) -> _DecisionBody:
        names = [option.name for option in self.options]
        if len(set(names)) != len(names):
            raise ValueError("option names must be unique")
        if self.chosen not in names:
            raise ValueError("chosen must name one of the options")
        return self


class EvidenceInput(_Strict):
    path: str | None = None
    reference: str | None = None


class DecisionInput(_DecisionBody):
    """What the agent supplies; evidence paths are hashed by the writer."""

    evidence: list[EvidenceInput] = Field(min_length=1)


class StageImpressionInput(_Strict):
    stage: str = Field(pattern=_SLUG)
    artifacts: list[str] = Field(
        min_length=1, description="Workspace-relative files or directories the stage produced"
    )
    impression: str

    _check_impression = field_validator("impression")(impression_is_prose)


Severity = Literal["info", "warning", "error"]


class VisionFinding(_Strict):
    category: str = Field(min_length=1)
    severity: Severity
    note: str = Field(min_length=1)


class VisionReviewInput(_Strict):
    image_path: str | None = Field(default=None, description="Workspace-relative image")
    source_event_id: str | None = Field(
        default=None, description="event_id of the vision tool event or image observation"
    )
    model: str = Field(min_length=1)
    checklist: str = Field(pattern=_SLUG)
    findings: list[VisionFinding] = Field(default_factory=list[VisionFinding])
    impression: str

    _check_impression = field_validator("impression")(impression_is_prose)

    @model_validator(mode="after")
    def _bound(self) -> VisionReviewInput:
        if self.image_path is None and self.source_event_id is None:
            raise ValueError("a vision review needs image_path or source_event_id")
        return self


class _Envelope(_Strict):
    schema_version: Literal[1] = SCHEMA_VERSION
    plugin: Literal["circuit"] = PLUGIN
    sequence: int = Field(ge=1)
    event_id: str = Field(pattern=_SHA256)
    recorded_at: str


class DecisionRecord(_Envelope, _DecisionBody):
    kind: Literal["decision"] = "decision"
    evidence: list[EvidenceRef] = Field(min_length=1)


class StageImpression(_Envelope):
    kind: Literal["stage_impression"] = "stage_impression"
    stage: str = Field(pattern=_SLUG)
    artifacts: list[ArtifactRef] = Field(min_length=1)
    impression: str

    _check_impression = field_validator("impression")(impression_is_prose)


class VisionReview(_Envelope):
    kind: Literal["vision_review"] = "vision_review"
    image_path: str | None = None
    image_sha256: str | None = Field(default=None, pattern=_SHA256)
    source_event_id: str | None = None
    model: str = Field(min_length=1)
    checklist: str = Field(pattern=_SLUG)
    findings: list[VisionFinding]
    impression: str

    _check_impression = field_validator("impression")(impression_is_prose)


def records_dir(root: Path | None = None) -> Path:
    return (root or workspace_root()) / RECORDS_DIR


def _relative(value: str, root: Path) -> tuple[str, Path]:
    path = workspace_path(value, root)
    if not path.exists():
        raise ValueError(f"artifact does not exist: {value}")
    relative = path.relative_to(root.resolve()).as_posix() if path != root.resolve() else "."
    return relative, path


def _append(kind: str, body: dict[str, Any], root: Path) -> dict[str, Any]:
    log = records_dir(root) / LOG_FILES[kind]
    log.parent.mkdir(parents=True, exist_ok=True)
    existing = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    sequence = len([line for line in existing if line.strip()]) + 1
    identity = {"kind": kind, "sequence": sequence, **body}
    event_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "plugin": PLUGIN,
        "sequence": sequence,
        "event_id": event_id,
        "recorded_at": datetime.now(UTC).isoformat(),
        **body,
    }
    models: dict[str, type[BaseModel]] = {
        "decision": DecisionRecord,
        "stage_impression": StageImpression,
        "vision_review": VisionReview,
    }
    validated = models[kind].model_validate(record).model_dump(mode="json", exclude_none=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(validated, ensure_ascii=False, separators=(",", ":")) + "\n")
    return {"verdict": "pass", "kind": kind, "path": str(log), "record": validated}


def record_decision(payload: Mapping[str, Any], root: Path | None = None) -> dict[str, Any]:
    base = (root or workspace_root()).resolve()
    decision = DecisionInput.model_validate(dict(payload))
    evidence: list[dict[str, Any]] = []
    for item in decision.evidence:
        if item.path is not None:
            relative, path = _relative(item.path, base)
            evidence.append({"path": relative, "sha256": tree_sha256(path)})
        else:
            evidence.append({"reference": item.reference})
    body = decision.model_dump(mode="json", exclude={"evidence"})
    body["evidence"] = evidence
    return _append("decision", body, base)


def record_impression(payload: Mapping[str, Any], root: Path | None = None) -> dict[str, Any]:
    base = (root or workspace_root()).resolve()
    impression = StageImpressionInput.model_validate(dict(payload))
    artifacts: list[dict[str, str]] = []
    for value in impression.artifacts:
        relative, path = _relative(value, base)
        artifacts.append({"path": relative, "sha256": tree_sha256(path)})
    body = {"stage": impression.stage, "artifacts": artifacts, "impression": impression.impression}
    return _append("stage_impression", body, base)


def record_vision_review(payload: Mapping[str, Any], root: Path | None = None) -> dict[str, Any]:
    base = (root or workspace_root()).resolve()
    review = VisionReviewInput.model_validate(dict(payload))
    body = review.model_dump(mode="json")
    if review.image_path is not None:
        relative, path = _relative(review.image_path, base)
        if not path.is_file():
            raise ValueError(f"image is not a file: {review.image_path}")
        body["image_path"] = relative
        body["image_sha256"] = sha256_file(path)
    else:
        body["image_sha256"] = None
    return _append("vision_review", body, base)


RECORDERS = {
    "decision": record_decision,
    "impression": record_impression,
    "vision-review": record_vision_review,
}


def records_summary(root: Path | None = None) -> dict[str, Any]:
    """Counts per log plus the last Stop-hook verdict; informational only."""
    directory = records_dir(root)
    counts: dict[str, int] = {}
    for kind, filename in LOG_FILES.items():
        path = directory / filename
        lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
        counts[kind] = len([line for line in lines if line.strip()])
    status_path = directory / "records-status.json"
    status: Any = None
    if status_path.is_file():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            status = {"verdict": "fail", "problems": ["records-status.json is malformed"]}
    return {"verdict": "pass", "records_dir": str(directory), "counts": counts, "last_stop": status}
