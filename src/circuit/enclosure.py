"""Part height and edge-connector gate against the hash-pinned mech board envelope.

mechanical-agent exports ``*.mech-board-envelope.json``: the board size and
mount holes its standoffs expect, the part height allowed above and below the
board, and each side-face opening as a face-local span plus a z span from the
board top surface. This gate reads circuit's own ``*.board-geometry.json`` and
checks the board matches, every part fits under its height limit and every
edge connector meets an opening, using the same span rules as mech's
``board_geometry`` gate. A missing envelope or an incomplete geometry is
``unknown``; a changed or malformed envelope fails.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .board_geometry import BoardGeometry, Component, Face
from .brief import DesignBrief, brief_sha256

ARTIFACT_KIND = "circuit_enclosure"
DIMENSION_TOL_MM = 0.05
HOLE_TOL_MM = 0.05
COURTYARD_EXCESS_MM = 0.25
_EPS = 1e-9
Status = Literal["pass", "fail", "unknown"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EnvelopeHole(_Strict):
    id: str
    x_mm: float
    y_mm: float
    diameter_mm: float = Field(gt=0)


class EnvelopeBoard(_Strict):
    width_mm: float = Field(gt=0)
    depth_mm: float = Field(gt=0)
    thickness_mm: float = Field(gt=0)
    mount_holes: list[EnvelopeHole]


class EnvelopeOpening(_Strict):
    id: str
    face: Face
    along_mm: tuple[float, float]
    z_mm: tuple[float, float]


class MechBoardEnvelope(_Strict):
    schema_version: Literal[1]
    system: Literal["mech"]
    artifact_kind: Literal["mech_board_envelope"]
    frame: Literal["board_center_y_up_mm"]
    design: str
    brief_sha256: str
    board: EnvelopeBoard
    max_height_top_mm: float = Field(ge=0)
    max_height_bottom_mm: float = Field(ge=0)
    openings: list[EnvelopeOpening]


class EnclosureCheck(_Strict):
    subject: str
    status: Status
    measured: float | None = None
    limit: float | None = None
    detail: str


class EnclosureReport(_Strict):
    artifact_kind: Literal["circuit_enclosure"] = ARTIFACT_KIND
    design: str
    brief_sha256: str
    envelope_sha256: str
    geometry_sha256: str | None
    checks: list[EnclosureCheck]
    verdict: Status
    detail: str


def _along_span(part: Component) -> tuple[float, float]:
    assert part.bbox_mm is not None
    x0, y0, x1, y1 = part.bbox_mm
    low, high = (x0, x1) if part.edge in ("front", "back") else (y0, y1)
    return low + COURTYARD_EXCESS_MM, high - COURTYARD_EXCESS_MM


def _board_checks(envelope: MechBoardEnvelope, geometry: BoardGeometry) -> list[EnclosureCheck]:
    checks: list[EnclosureCheck] = []
    for subject, limit, measured in (
        ("width", envelope.board.width_mm, geometry.width_mm),
        ("depth", envelope.board.depth_mm, geometry.depth_mm),
        ("thickness", envelope.board.thickness_mm, geometry.thickness_mm),
    ):
        assert measured is not None
        checks.append(
            EnclosureCheck(
                subject=subject,
                status="pass" if abs(limit - measured) <= DIMENSION_TOL_MM + _EPS else "fail",
                measured=measured,
                limit=limit,
                detail="board vs mech cavity board",
            )
        )
    unmatched = [
        hole.ref
        for hole in geometry.mount_holes
        if not any(
            abs(hole.x_mm - mh.x_mm) <= HOLE_TOL_MM + _EPS
            and abs(hole.y_mm - mh.y_mm) <= HOLE_TOL_MM + _EPS
            and abs(hole.diameter_mm - mh.diameter_mm) <= HOLE_TOL_MM + _EPS
            for mh in envelope.board.mount_holes
        )
    ]
    holes_ok = not unmatched and len(geometry.mount_holes) == len(envelope.board.mount_holes)
    checks.append(
        EnclosureCheck(
            subject="mount_holes",
            status="pass" if holes_ok else "fail",
            measured=len(geometry.mount_holes),
            limit=len(envelope.board.mount_holes),
            detail=f"holes off the mech standoffs: {unmatched}" if unmatched else "hole sets match",
        )
    )
    return checks


def _part_checks(envelope: MechBoardEnvelope, geometry: BoardGeometry) -> list[EnclosureCheck]:
    thickness = envelope.board.thickness_mm
    checks: list[EnclosureCheck] = []
    for part in sorted(geometry.components, key=lambda item: item.ref):
        limit = envelope.max_height_top_mm if part.side == "top" else envelope.max_height_bottom_mm
        if part.height_mm is None:
            checks.append(
                EnclosureCheck(
                    subject=f"height:{part.ref}",
                    status="unknown",
                    limit=limit,
                    detail="part height unknown",
                )
            )
            continue
        checks.append(
            EnclosureCheck(
                subject=f"height:{part.ref}",
                status="pass" if part.height_mm <= limit + _EPS else "fail",
                measured=part.height_mm,
                limit=limit,
                detail=f"{part.side}-side part height vs mech cavity",
            )
        )
        if not part.connector or part.edge is None or part.bbox_mm is None:
            continue
        low, high = _along_span(part)
        z0, z1 = (
            (0.0, part.height_mm)
            if part.side == "top"
            else (-thickness - part.height_mm, -thickness)
        )
        served = [
            opening.id
            for opening in envelope.openings
            if opening.face == part.edge
            and opening.along_mm[0] <= low + _EPS
            and opening.along_mm[1] >= high - _EPS
            and min(opening.z_mm[1], z1) - max(opening.z_mm[0], z0) > 0
        ]
        checks.append(
            EnclosureCheck(
                subject=f"connector:{part.ref}",
                status="pass" if served else "fail",
                detail=(
                    f"served by {served[0]}"
                    if served
                    else f"no {part.edge} opening spans {low:g}..{high:g} mm"
                    f" and z {z0:g}..{z1:g} mm"
                ),
            )
        )
    return checks


def check_enclosure(design: DesignBrief, brief_path: Path, geometry_path: Path) -> EnclosureReport:
    """Check board, part heights and edge connectors against the pinned mech envelope."""
    if design.enclosure is None:
        raise ValueError("brief has no enclosure section")
    pinned = design.enclosure.envelope_sha256
    geometry_sha: str | None = None

    def report(checks: list[EnclosureCheck], verdict: Status, detail: str) -> EnclosureReport:
        return EnclosureReport(
            design=design.name,
            brief_sha256=brief_sha256(brief_path),
            envelope_sha256=pinned,
            geometry_sha256=geometry_sha,
            checks=checks,
            verdict=verdict,
            detail=detail,
        )

    envelope_path = brief_path.parent / design.enclosure.envelope_path
    try:
        raw = envelope_path.read_bytes()
    except OSError as exc:
        return report([], "unknown", f"mech board envelope unreadable: {exc}")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != pinned:
        return report([], "fail", f"mech board envelope changed since pinned (now {actual})")
    try:
        envelope = MechBoardEnvelope.model_validate(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        return report([], "fail", f"mech board envelope is malformed: {exc}")
    try:
        geometry_raw = geometry_path.read_bytes()
        geometry_sha = hashlib.sha256(geometry_raw).hexdigest()
        geometry = BoardGeometry.model_validate(json.loads(geometry_raw.decode("utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        return report([], "unknown", f"circuit board geometry unusable: {exc}")
    if geometry.verdict != "pass":
        return report([], "unknown", f"circuit board geometry incomplete: {geometry.unknown}")
    checks = _board_checks(envelope, geometry) + _part_checks(envelope, geometry)
    statuses = {check.status for check in checks}
    verdict: Status = (
        "fail" if "fail" in statuses else "unknown" if "unknown" in statuses else "pass"
    )
    return report(checks, verdict, f"{len(checks)} enclosure checks")
