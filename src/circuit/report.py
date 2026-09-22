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
    jobset: JobsetResult | None = None
    jobset_consistent: bool | None = None
    diffs: dict[str, DiffReport] = Field(default_factory=dict)


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
        jobset=jobset,
        jobset_consistent=jobset_consistent,
        diffs=diffs or {},
    )


def write_report(report: DesignReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
