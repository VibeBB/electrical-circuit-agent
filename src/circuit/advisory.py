"""Advisory Konnect observations kept separate from deterministic gates."""

from __future__ import annotations

from typing import Literal

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
