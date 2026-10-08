"""Design report construction from deterministic gate results."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .advisory import AdvisoryResult
from .brief import DesignBrief, brief_sha256
from .kicad_cli import DiffReport, JobsetResult, Report
from .netlist import ConnectivityReport
from .sch_lint import SchLintReport


class VisionPoint(BaseModel):
    """One rendered raster a vision reviewer still has to look at.

    `checklist` is a best-effort slug derived from the file name; the
    reviewer re-checks the image kind before applying it. `record_with`
    names the record mechanism the review must land in.
    """

    model_config = ConfigDict(extra="forbid")

    image_path: str
    checklist: str
    record_with: str


class DesignReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brief_name: str
    brief_sha256: str
    kicad_version: str
    connectivity: ConnectivityReport | None
    sch_lint: SchLintReport | None = None
    erc: Report | None
    drc: Report | None
    exports: dict[str, list[str]]
    verdict: Literal["pass", "fail"]
    reasons: list[str]
    advisory: list[AdvisoryResult] = Field(default_factory=lambda: list[AdvisoryResult]())
    renders: list[str] = Field(default_factory=list)
    vision_points: list[VisionPoint] = Field(default_factory=lambda: list[VisionPoint]())
    jobset: JobsetResult | None = None
    jobset_consistent: bool | None = None
    diffs: dict[str, DiffReport] = Field(default_factory=dict)


VISION_RECORD_WITH = "circuit_record_vision_review"

_LAYER_TOKENS = ("_cu", "mask", "paste", "fab", "edge", "dwgs", "cmts", "eco", "layer")


def _vision_checklist(image_path: str) -> str:
    """Best-effort `VisualChecklist` slug for a collected render, by file name."""
    stem = Path(image_path).stem.lower()
    if "diff" in stem:
        return "diff"
    if "stackup" in stem:
        return "stackup"
    if "datasheet" in stem:
        return "datasheet"
    if "footprint" in stem or "fp_" in stem:
        return "footprint"
    if "symbol" in stem or "library" in stem:
        return "symbol"
    if "model" in stem or "3d" in stem:
        return "model3d"
    if "intake" in stem:
        return "intake_image"
    if any(token in stem for token in _LAYER_TOKENS):
        return "board_layers"
    if "iso" in stem:
        return "board_isometric"
    if "side" in stem or "front" in stem or "back" in stem:
        return "board_side"
    if "bottom" in stem:
        return "board_bottom"
    if "top" in stem or "board" in stem or "pcb" in stem:
        return "board_top"
    return "schematic"


def vision_points(renders: list[str]) -> list[VisionPoint]:
    """Vision-review packet for the renders the report already lists."""
    return [
        VisionPoint(
            image_path=image,
            checklist=_vision_checklist(image),
            record_with=VISION_RECORD_WITH,
        )
        for image in renders
    ]


def build_design_report(
    brief: DesignBrief,
    *,
    brief_path: Path,
    kicad_version: str,
    connectivity: ConnectivityReport | None,
    erc: Report | None,
    drc: Report | None,
    exports: dict[str, list[str]],
    sch_lint: SchLintReport | None = None,
    advisory: list[AdvisoryResult] | None = None,
    renders: list[str] | None = None,
    jobset: JobsetResult | None = None,
    jobset_consistent: bool | None = None,
    diffs: dict[str, DiffReport] | None = None,
) -> DesignReport:
    reasons: list[str] = []
    gates: list[tuple[str, object | None, bool]] = [
        ("connectivity", connectivity, connectivity is not None and connectivity.verdict == "pass"),
        ("sch_lint", sch_lint, sch_lint is not None and sch_lint.verdict == "pass"),
        ("erc", erc, erc is not None and erc.verdict == "pass"),
        ("drc", drc, drc is not None and drc.verdict == "pass"),
    ]
    for name, value, passed in gates:
        if value is None:
            reasons.append(f"gate not executed: {name}")
        elif not passed:
            reasons.append(f"gate failed: {name}")
    if jobset is not None and jobset_consistent is not True:
        reasons.append("gate failed: jobset consistency")
    return DesignReport(
        brief_name=brief.name,
        brief_sha256=brief_sha256(brief_path),
        kicad_version=kicad_version,
        connectivity=connectivity,
        sch_lint=sch_lint,
        erc=erc,
        drc=drc,
        exports=exports,
        verdict="pass" if not reasons else "fail",
        reasons=reasons,
        advisory=advisory or [],
        renders=renders or [],
        vision_points=vision_points(renders or []),
        jobset=jobset,
        jobset_consistent=jobset_consistent,
        diffs=diffs or {},
    )


def write_report(report: DesignReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
