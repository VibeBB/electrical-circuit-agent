"""Deterministic drop-in check of the second-source parts declared in the brief.

An alternate passes only when it keeps the primary's footprint and symbol and
declares at least the primary's voltage/current ratings. A different symbol
leaves pin equivalence unproven, and a missing rating cannot be compared;
both are ``unknown``, which fails the gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .brief import Alternate, DesignBrief, Part, brief_sha256

ARTIFACT_KIND = "circuit_alternates"
Status = Literal["pass", "fail", "unknown"]
Aspect = Literal["footprint", "symbol", "voltage", "current"]


class AlternateCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: str
    mpn: str
    aspect: Aspect
    primary: str | float | None
    alternate: str | float | None
    status: Status
    detail: str


class AlternateEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: str
    mpn: str
    manufacturer: str
    source: str
    status: Status


class AlternatesReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["circuit_alternates"] = ARTIFACT_KIND
    design: str
    brief_sha256: str
    alternates: list[AlternateEntry]
    checks: list[AlternateCheck]
    verdict: Status


def _worst(statuses: set[Status]) -> Status:
    return "fail" if "fail" in statuses else "unknown" if "unknown" in statuses else "pass"


def _same(part: Part, alt: Alternate, aspect: Literal["footprint", "symbol"]) -> AlternateCheck:
    primary = part.footprint if aspect == "footprint" else part.lib_id
    alternate = alt.footprint if aspect == "footprint" else alt.lib_id
    if primary == alternate:
        status: Status = "pass"
        detail = f"same {aspect}"
    elif aspect == "footprint":
        status = "fail"
        detail = "footprint differs; not a drop-in"
    else:
        status = "unknown"
        detail = "symbol differs; pin equivalence is unproven"
    return AlternateCheck(
        reference=part.reference,
        mpn=alt.mpn,
        aspect=aspect,
        primary=primary,
        alternate=alternate,
        status=status,
        detail=detail,
    )


def _rating(
    part: Part, alt: Alternate, aspect: Literal["voltage", "current"]
) -> AlternateCheck | None:
    unit = "V" if aspect == "voltage" else "A"
    primary = part.rated_voltage_v if aspect == "voltage" else part.rated_current_a
    alternate = alt.rated_voltage_v if aspect == "voltage" else alt.rated_current_a
    if primary is None:
        return None
    if alternate is None:
        status: Status = "unknown"
        detail = f"{alt.mpn} has no rated_{aspect}_{unit.lower()}"
    elif alternate >= primary:
        status = "pass"
        detail = f"{alternate:g} {unit} >= {primary:g} {unit}"
    else:
        status = "fail"
        detail = f"{alternate:g} {unit} < {primary:g} {unit}"
    return AlternateCheck(
        reference=part.reference,
        mpn=alt.mpn,
        aspect=aspect,
        primary=primary,
        alternate=alternate,
        status=status,
        detail=detail,
    )


def check_alternates(design: DesignBrief, brief_path: Path) -> AlternatesReport:
    """Check every declared alternate against its primary part."""
    if not any(part.alternates for part in design.parts):
        raise ValueError("brief declares no alternates")
    checks: list[AlternateCheck] = []
    entries: list[AlternateEntry] = []
    for part in design.parts:
        for alt in part.alternates:
            own = [_same(part, alt, "footprint"), _same(part, alt, "symbol")]
            own += [
                check
                for check in (_rating(part, alt, "voltage"), _rating(part, alt, "current"))
                if check is not None
            ]
            checks.extend(own)
            entries.append(
                AlternateEntry(
                    reference=part.reference,
                    mpn=alt.mpn,
                    manufacturer=alt.manufacturer,
                    source=alt.source,
                    status=_worst({check.status for check in own}),
                )
            )
    return AlternatesReport(
        design=design.name,
        brief_sha256=brief_sha256(brief_path),
        alternates=sorted(entries, key=lambda e: (e.reference, e.mpn)),
        checks=sorted(checks, key=lambda c: (c.reference, c.mpn, c.aspect)),
        verdict=_worst({check.status for check in checks}),
    )
