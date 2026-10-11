"""Deterministic voltage/current derating gate over the authored brief.

Applied stress is the highest ``voltage_v``/``current_a`` among the nets a part
touches (conservative for series parts). A rule needs the matching rated value
on the part and a non-zero applied value; otherwise the check is ``unknown``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .brief import DeratingRule, DesignBrief, brief_sha256

Status = Literal["pass", "fail", "unknown"]


class DeratingCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: str
    quantity: Literal["voltage", "current"]
    applied: float | None
    rated: float | None
    factor: float
    limit: float | None
    source: str
    status: Status
    detail: str


class DeratingReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    design: str
    brief_sha256: str
    checks: list[DeratingCheck]
    verdict: Status


def _check(
    reference: str,
    quantity: Literal["voltage", "current"],
    applied: float,
    rated: float | None,
    rule: DeratingRule,
    factor: float,
) -> DeratingCheck:
    unit = "V" if quantity == "voltage" else "A"
    limit = None if rated is None else rated * factor
    if rated is None:
        status: Status = "unknown"
        detail = f"{reference} has no rated_{quantity}_{unit.lower()}"
    elif applied <= 0:
        status = "unknown"
        detail = f"no net on {reference} states a {quantity}"
    elif limit is not None and applied <= limit:
        status = "pass"
        detail = f"{applied:g} {unit} <= {factor:g} x {rated:g} {unit}"
    else:
        status = "fail"
        detail = f"{applied:g} {unit} > {factor:g} x {rated:g} {unit}"
    return DeratingCheck(
        reference=reference,
        quantity=quantity,
        applied=applied if applied > 0 else None,
        rated=rated,
        factor=factor,
        limit=limit,
        source=rule.source,
        status=status,
        detail=detail,
    )


def check_derating(design: DesignBrief, brief_path: Path) -> DeratingReport:
    if design.derating is None:
        raise ValueError("brief has no derating section")
    parts = {part.reference: part for part in design.parts}
    stress: dict[str, tuple[float, float]] = {}
    for net in design.nets:
        for token in net.pins:
            reference = token.split(".", 1)[0]
            voltage, current = stress.get(reference, (0.0, 0.0))
            stress[reference] = (max(voltage, net.voltage_v), max(current, net.current_a))
    checks: list[DeratingCheck] = []
    for rule in design.derating.rules:
        for reference in rule.references:
            part = parts[reference]
            voltage, current = stress.get(reference, (0.0, 0.0))
            if rule.voltage_factor is not None:
                checks.append(
                    _check(
                        reference,
                        "voltage",
                        voltage,
                        part.rated_voltage_v,
                        rule,
                        rule.voltage_factor,
                    )
                )
            if rule.current_factor is not None:
                checks.append(
                    _check(
                        reference,
                        "current",
                        current,
                        part.rated_current_a,
                        rule,
                        rule.current_factor,
                    )
                )
    statuses = {check.status for check in checks}
    verdict: Status = (
        "fail" if "fail" in statuses else "unknown" if "unknown" in statuses else "pass"
    )
    return DeratingReport(
        design=design.name,
        brief_sha256=brief_sha256(brief_path),
        checks=sorted(checks, key=lambda c: (c.reference, c.quantity)),
        verdict=verdict,
    )
