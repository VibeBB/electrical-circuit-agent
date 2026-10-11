"""Test-point access gate against the hash-pinned prodeng factory test spec.

Every net the factory test touches (FTM interface nets and command
``measures_nets``) must exist in the brief and carry at least one test point:
a part whose footprint comes from KiCad's ``TestPoint`` library. A missing or
unreadable spec is ``unknown``; a changed or malformed spec fails.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .brief import DesignBrief, brief_sha256

ARTIFACT_KIND = "circuit_dft"
TEST_POINT_LIBRARY = "TestPoint"
Status = Literal["pass", "fail", "unknown"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FtmEntrySpec(_Strict):
    method: str = Field(min_length=1)
    detail: str
    conditions: list[str]


class FtmLockoutSpec(_Strict):
    method: str = Field(min_length=1)
    detail: str


class FtmInterfaceSpec(_Strict):
    transport: Literal["uart", "usb_cdc", "swd", "jtag", "i2c", "spi", "can", "ble", "other"]
    settings: str
    nets: list[str]


class FtmCommandSpec(_Strict):
    id: str = Field(pattern=r"^TC-[0-9]{2,4}$")
    name: str = Field(min_length=1)
    request: str = Field(min_length=1)
    response_pattern: str = Field(min_length=1)
    timeout_ms: int = Field(gt=0)
    measures_nets: list[str]
    covers: list[str]
    destructive: bool


class FtmProvisioningSpec(_Strict):
    item: str = Field(min_length=1)
    source: str
    write_once: bool


class ProdengFtmSpec(_Strict):
    """Strict mirror of production-engineering ``factory-test-spec.json`` (declared FTM)."""

    declared: Literal[True]
    entry: FtmEntrySpec
    field_lockout: FtmLockoutSpec
    interface: FtmInterfaceSpec
    commands: list[FtmCommandSpec]
    provisioning: list[FtmProvisioningSpec]
    exit: str
    max_duration_s: float = Field(gt=0)
    command_timeout_budget_s: float = Field(ge=0)


class NoFtmSpec(_Strict):
    declared: Literal[False]
    reason: str


class DftCheck(_Strict):
    net: str
    test_points: list[str]
    status: Status
    detail: str


class DftReport(_Strict):
    artifact_kind: Literal["circuit_dft"] = ARTIFACT_KIND
    design: str
    brief_sha256: str
    ftm_spec_sha256: str | None
    checks: list[DftCheck]
    verdict: Status
    detail: str


def _parse(raw: bytes) -> ProdengFtmSpec | NoFtmSpec:
    data: Any = json.loads(raw.decode("utf-8"))
    if isinstance(data, dict) and data.get("declared") is False:  # pyright: ignore[reportUnknownMemberType]
        return NoFtmSpec.model_validate(data)
    return ProdengFtmSpec.model_validate(data)


def check_dft(design: DesignBrief, brief_path: Path) -> DftReport:
    """Check each factory-test net has a test point, against the pinned spec."""
    if design.dft is None:
        raise ValueError("brief has no dft section")
    pinned = design.dft.ftm_spec_sha256

    def report(checks: list[DftCheck], verdict: Status, detail: str) -> DftReport:
        return DftReport(
            design=design.name,
            brief_sha256=brief_sha256(brief_path),
            ftm_spec_sha256=pinned,
            checks=checks,
            verdict=verdict,
            detail=detail,
        )

    spec_path = brief_path.parent / design.dft.ftm_spec_path
    try:
        raw = spec_path.read_bytes()
    except OSError as exc:
        return report([], "unknown", f"factory test spec unreadable: {exc}")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != pinned:
        return report([], "fail", f"factory test spec changed since pinned (now {actual})")
    try:
        spec = _parse(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        return report([], "fail", f"factory test spec is malformed: {exc}")
    from ._envelope import check_envelope
    from .workspace import workspace_root

    provenance, provenance_detail = check_envelope(spec_path, "prodeng", workspace_root(), pinned)
    if provenance != "pass":
        return report([], provenance, f"factory test spec provenance: {provenance_detail}")
    if isinstance(spec, NoFtmSpec):
        return report([], "pass", "prodeng declares no factory test mode")
    nets = set(spec.interface.nets)
    nets.update(net for command in spec.commands for net in command.measures_nets)
    if not nets:
        return report([], "pass", "factory test touches no nets")
    test_point_refs = {
        part.reference
        for part in design.parts
        if part.footprint.split(":", 1)[0] == TEST_POINT_LIBRARY
    }
    brief_nets = {net.name: net for net in design.nets}
    checks: list[DftCheck] = []
    for name in sorted(nets):
        net = brief_nets.get(name)
        if net is None:
            checks.append(
                DftCheck(net=name, test_points=[], status="fail", detail="net not in brief")
            )
            continue
        points = sorted({token.split(".", 1)[0] for token in net.pins} & test_point_refs)
        checks.append(
            DftCheck(
                net=name,
                test_points=points,
                status="pass" if points else "fail",
                detail="test point present" if points else "no TestPoint footprint on net",
            )
        )
    verdict: Status = "pass" if all(check.status == "pass" for check in checks) else "fail"
    return report(checks, verdict, f"{len(checks)} factory-test nets checked")
