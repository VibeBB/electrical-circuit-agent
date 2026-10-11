"""Bare-board fabrication spec checked against the authored KiCad PCB.

The brief ``fabrication`` section is the fab-note source. Copper layer count
and board thickness are read from the ``.kicad_pcb``; a mismatch fails and an
unreadable or silent board is ``unknown``. Material, finish, IPC-6012 class,
soldermask and silkscreen are carried verbatim into ``*.fabrication.json``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import sexpr
from .brief import DesignBrief, FabricationSpec, brief_sha256
from .connplace import (
    _board_thickness,  # pyright: ignore[reportPrivateUsage]
    _first,  # pyright: ignore[reportPrivateUsage]
)

ARTIFACT_KIND = "circuit_fabrication"
Status = Literal["pass", "fail", "unknown"]
_COPPER = re.compile(r"^(F|B|In[0-9]+)\.Cu$")


class FabricationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    aspect: Literal["copper_layers", "thickness"]
    required: float
    measured: float | None
    status: Status
    detail: str


class FabricationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["circuit_fabrication"] = ARTIFACT_KIND
    design: str
    brief_sha256: str
    pcb_sha256: str | None
    spec: FabricationSpec
    checks: list[FabricationCheck]
    verdict: Status


def _copper_layers(root: list[sexpr.SExpr]) -> int | None:
    layers = _first(root, "layers")
    if layers is None:
        return None
    names = [
        str(item[1])
        for item in layers[1:]
        if isinstance(item, list) and len(item) > 1 and isinstance(item[1], str)
    ]
    count = sum(1 for name in names if _COPPER.match(name))
    return count or None


def check_fabrication(design: DesignBrief, brief_path: Path, pcb_path: Path) -> FabricationReport:
    """Compare the authored board with the brief fabrication requirements."""
    spec = design.fabrication
    if spec is None:
        raise ValueError("brief has no fabrication section")
    layers: int | None = None
    thickness: float | None = None
    pcb_sha: str | None = None
    reason = "board does not state it"
    try:
        raw = pcb_path.read_bytes()
        pcb_sha = hashlib.sha256(raw).hexdigest()
        root = sexpr.parse_text(raw.decode("utf-8"))
        if not root or root[0] != "kicad_pcb":
            raise ValueError("not a KiCad PCB")
        layers = _copper_layers(root)
        thickness = _board_thickness(root)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        reason = f"board unreadable: {exc}"
    checks = [
        FabricationCheck(
            aspect="copper_layers",
            required=spec.copper_layers,
            measured=layers,
            status="unknown"
            if layers is None
            else "pass"
            if layers == spec.copper_layers
            else "fail",
            detail=reason if layers is None else f"{layers} copper layers on the board",
        ),
        FabricationCheck(
            aspect="thickness",
            required=spec.thickness_mm,
            measured=thickness,
            status="unknown"
            if thickness is None
            else "pass"
            if abs(thickness - spec.thickness_mm) <= spec.thickness_tolerance_mm
            else "fail",
            detail=reason
            if thickness is None
            else f"{thickness:g} mm vs {spec.thickness_mm:g} ± {spec.thickness_tolerance_mm:g} mm",
        ),
    ]
    statuses = {check.status for check in checks}
    return FabricationReport(
        design=design.name,
        brief_sha256=brief_sha256(brief_path),
        pcb_sha256=pcb_sha,
        spec=spec,
        checks=checks,
        verdict="fail" if "fail" in statuses else "unknown" if "unknown" in statuses else "pass",
    )
