"""PCB assembly handoff for production-engineering-agent (``*.circuit-assembly.json``).

Each placed footprint carries its KiCad mount attribute (``smd`` or
``through_hole``), board side and DNP flag so prodeng can bind every
populated part to an SMT or THT operation. Coordinates use the same board
frame as ``*.board-geometry.json``. ``board_only`` footprints and mounting
holes are not assembled and are omitted; a populated footprint without a
mount attribute is ``unknown`` and fails the export.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import sexpr
from .board_geometry import (
    SourceFile,
    _is_mount_hole,  # pyright: ignore[reportPrivateUsage]
    _sha,  # pyright: ignore[reportPrivateUsage]
)
from .connplace import (
    _children,  # pyright: ignore[reportPrivateUsage]
    _outline,  # pyright: ignore[reportPrivateUsage]
    _parse_board_footprint,  # pyright: ignore[reportPrivateUsage]
)

ARTIFACT_KIND = "circuit_assembly"
SCHEMA_VERSION = 1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Placement(_Strict):
    ref: str = Field(min_length=1)
    lib_id: str
    value: str | None
    side: Literal["top", "bottom"]
    mount: Literal["smd", "tht"] | None
    dnp: bool
    x_mm: float
    y_mm: float
    rotation_deg: float


class Assembly(_Strict):
    artifact_kind: Literal["circuit_assembly"] = ARTIFACT_KIND
    schema_version: Literal[1] = SCHEMA_VERSION
    frame: Literal["board_center_y_up_mm"] = "board_center_y_up_mm"
    pcb: SourceFile
    placements: list[Placement]
    unknown: list[str]
    verdict: Literal["pass", "fail"]


def _attributes(node: list[sexpr.SExpr]) -> set[str]:
    tokens: set[str] = set()
    for attr in _children(node, "attr"):
        tokens.update(item for item in attr[1:] if isinstance(item, str))
    for dnp in _children(node, "dnp"):
        if len(dnp) < 2 or dnp[1] == "yes":
            tokens.add("dnp")
    return tokens


def assembly(pcb_path: Path) -> Assembly:
    root = sexpr.parse_text(pcb_path.read_text(encoding="utf-8"))
    _, polygon = _outline(root)
    unknown: list[str] = []
    if len(polygon) < 3:
        unknown.append("outline")
    xs = [p[0] for p in polygon] or [0.0]
    ys = [p[1] for p in polygon] or [0.0]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    placements: list[Placement] = []
    for node in _children(root, "footprint"):
        footprint = _parse_board_footprint(node)
        tokens = _attributes(node)
        if "board_only" in tokens or _is_mount_hole(footprint):
            continue
        mount: Literal["smd", "tht"] | None = (
            "smd" if "smd" in tokens else "tht" if "through_hole" in tokens else None
        )
        dnp = "dnp" in tokens
        if mount is None and not dnp:
            unknown.append(f"mount:{footprint.ref}")
        placements.append(
            Placement(
                ref=footprint.ref,
                lib_id=footprint.lib_id,
                value=footprint.properties.get("Value"),
                side="bottom" if footprint.side == "B.Cu" else "top",
                mount=mount,
                dnp=dnp,
                x_mm=round(footprint.x - cx, 4),
                y_mm=round(cy - footprint.y, 4),
                rotation_deg=footprint.rotation,
            )
        )
    placements.sort(key=lambda item: item.ref)
    refs = [item.ref for item in placements]
    unknown.extend(f"duplicate:{ref}" for ref in sorted({r for r in refs if refs.count(r) > 1}))
    return Assembly(
        pcb=SourceFile(path=str(pcb_path), sha256=_sha(pcb_path)),
        placements=placements,
        unknown=unknown,
        verdict="fail" if unknown else "pass",
    )


def write_assembly(result: Assembly, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")


def assembly_result(result: Assembly, out: Path) -> dict[str, Any]:
    populated = [item for item in result.placements if not item.dnp]
    counts = {
        f"{mount}_{side}": sum(1 for p in populated if p.mount == mount and p.side == side)
        for mount in ("smd", "tht")
        for side in ("top", "bottom")
    }
    return {
        "verdict": result.verdict,
        "artifact_kind": ARTIFACT_KIND,
        "output": str(out),
        "unknown": result.unknown,
        "pcb_sha256": result.pcb.sha256,
        "placements": len(result.placements),
        "dnp": len(result.placements) - len(populated),
        "counts": counts,
    }


__all__ = ["Assembly", "Placement", "assembly", "assembly_result", "write_assembly"]
