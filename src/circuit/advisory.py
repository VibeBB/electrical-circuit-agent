"""Advisory Konnect observations kept separate from deterministic gates."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

VISION_REVIEW_TOOL = "vision_review"

VisualChecklist = Literal[
    "board_top",
    "board_bottom",
    "board_side",
    "board_isometric",
    "board_layers",
    "schematic",
    "footprint",
]

VisualFindingCategory = Literal[
    "silkscreen_overlap",
    "silkscreen_legibility",
    "reference_designator",
    "component_overhang",
    "polarity_mark",
    "pin1_mark",
    "connector_clearance",
    "connector_orientation",
    "mounting_hole_collision",
    "courtyard_overlap",
    "unrouted_pad",
    "height_collision",
    "tilted_component",
    "fab_completeness",
    "pad_legibility",
    "label_readability",
    "wire_label_balance",
    "sheet_utilization",
    "ambiguous_notation",
    "missing_dimension",
    "missing_manufacturing_info",
    "design_intent",
    "datasheet_mismatch",
    "other",
]


class VisualFinding(BaseModel):
    """One advisory observation from a visual review of an image."""

    model_config = ConfigDict(extra="forbid")

    category: VisualFindingCategory
    severity: Literal["error", "warning", "info"]
    note: str = Field(min_length=1)
    bbox: list[float] | None = Field(
        default=None,
        description="Optional normalized [x, y, w, h] region in the image",
    )


class VisualReviewDetail(BaseModel):
    """`detail` payload of a `vision_review` AdvisoryResult record."""

    model_config = ConfigDict(extra="forbid")

    image_path: str
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1)
    checklist: VisualChecklist
    impression: str = Field(
        min_length=1,
        description="Subjective impression from reading the drawing; required",
    )
    findings: list[VisualFinding] = Field(default_factory=lambda: list[VisualFinding]())


def parse_visual_review(result: AdvisoryResult) -> VisualReviewDetail | None:
    """Return the typed detail when `result` is a vision_review record."""
    if result.tool != VISION_REVIEW_TOOL or result.detail is None:
        return None
    try:
        return VisualReviewDetail.model_validate(result.detail)
    except ValidationError:
        return None


class AdvisoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    stage: Literal["intake", "schematic", "layout", "review", "manufacturing"]
    status: Literal["ok", "error", "not_applicable"]
    summary: str
    artifacts: list[str] = Field(default_factory=list)
    detail: dict[str, object] | None = None


def merge_detail(result: AdvisoryResult, extra: dict[str, object]) -> AdvisoryResult:
    result.detail = {**(result.detail or {}), **extra}
    return result


def review_record_path(image_path: Path, out_dir: Path | None = None) -> Path:
    """`review-visual-<slug>.advisory.json` next to the image (or out_dir)."""
    slug = re.sub(r"[^a-z0-9]+", "-", image_path.stem.lower()).strip("-") or "image"
    directory = out_dir if out_dir is not None else image_path.parent
    return directory / f"review-visual-{slug}.advisory.json"


def build_review_record(
    image_path: Path,
    *,
    model: str,
    checklist: VisualChecklist,
    impression: str,
    findings: list[dict[str, Any]],
    summary: str = "",
) -> AdvisoryResult:
    """Build the typed `vision_review` advisory record for one image.

    The image sha256 is computed here so the record always binds the
    judgment to the exact bytes reviewed; the detail is validated with
    the same schema `parse_visual_review` enforces, so a bad payload
    raises instead of producing a malformed record.
    """
    sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()
    detail = VisualReviewDetail(
        image_path=str(image_path),
        image_sha256=sha256,
        model=model,
        checklist=checklist,
        impression=impression,
        findings=[VisualFinding.model_validate(f) for f in findings],
    )
    return AdvisoryResult(
        tool=VISION_REVIEW_TOOL,
        stage="review",
        status="ok",
        summary=summary or f"visual review of {image_path.name}",
        artifacts=[str(image_path)],
        detail=detail.model_dump(mode="json"),
    )


def write_review_record(
    image_path: Path,
    *,
    model: str,
    checklist: VisualChecklist,
    impression: str,
    findings: list[dict[str, Any]],
    summary: str = "",
    out_dir: Path | None = None,
) -> Path:
    """Validate and write `review-visual-<slug>.advisory.json`; returns it."""
    record = build_review_record(
        image_path,
        model=model,
        checklist=checklist,
        impression=impression,
        findings=findings,
        summary=summary,
    )
    path = review_record_path(image_path, out_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
