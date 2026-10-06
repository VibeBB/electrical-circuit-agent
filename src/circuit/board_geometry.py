"""Board geometry handoff for mechanical-agent (``*.board-geometry.json``).

Coordinates use the board frame: millimetres, origin at the centre of the
Edge.Cuts bounding box, +x to the right and +y toward the top of the KiCad
view (KiCad's y axis points down, so it is negated). ``front`` is the -y
edge, matching mechanical-agent's enclosure faces. Heights are measured from
the copper face the part sits on; ``bottom`` parts hang below the board.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import occt, sexpr
from .connplace import (
    Point,
    _board_thickness,  # pyright: ignore[reportPrivateUsage]
    _BoardFootprint,  # pyright: ignore[reportPrivateUsage]
    _children,  # pyright: ignore[reportPrivateUsage]
    _first,  # pyright: ignore[reportPrivateUsage]
    _height_evidence,  # pyright: ignore[reportPrivateUsage]
    _is_connector,  # pyright: ignore[reportPrivateUsage]
    _number,  # pyright: ignore[reportPrivateUsage]
    _outline,  # pyright: ignore[reportPrivateUsage]
    _parse_board_footprint,  # pyright: ignore[reportPrivateUsage]
    _transform,  # pyright: ignore[reportPrivateUsage]
)
from .partspec import PartSpec, load_part_spec

ARTIFACT_KIND = "circuit_board_geometry"
SCHEMA_VERSION = 1
EDGE_TOLERANCE_MM = 0.5
_IDF_DATE = "2000/01/01.00:00:00"

Face = Literal["front", "back", "left", "right"]
HeightSource = Literal["property", "part_spec", "step_model"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceFile(_Strict):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StepModel(SourceFile):
    valid: bool


class MountHole(_Strict):
    ref: str
    x_mm: float
    y_mm: float
    diameter_mm: float = Field(gt=0)


class Component(_Strict):
    ref: str
    lib_id: str
    side: Literal["top", "bottom"]
    x_mm: float
    y_mm: float
    rotation_deg: float
    bbox_mm: tuple[float, float, float, float] | None
    height_mm: float | None
    height_sources: list[HeightSource]
    connector: bool
    edge: Face | None


class BoardGeometry(_Strict):
    artifact_kind: Literal["circuit_board_geometry"] = ARTIFACT_KIND
    schema_version: Literal[1] = SCHEMA_VERSION
    frame: Literal["board_center_y_up_mm"] = "board_center_y_up_mm"
    pcb: SourceFile
    part_specs: dict[str, str] = Field(default_factory=dict)
    step: StepModel | None = None
    width_mm: float | None
    depth_mm: float | None
    thickness_mm: float | None
    outline_mm: list[tuple[float, float]]
    mount_holes: list[MountHole]
    components: list[Component]
    max_height_top_mm: float | None
    max_height_bottom_mm: float | None
    unknown: list[str]
    verdict: Literal["pass", "fail"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signed_area(points: list[Point]) -> float:
    return sum(
        a[0] * b[1] - b[0] * a[1] for a, b in zip(points, [*points[1:], points[0]], strict=True)
    )


def _mount_holes(node: list[sexpr.SExpr], footprint: _BoardFootprint) -> list[tuple[Point, float]]:
    holes: list[tuple[Point, float]] = []
    for pad in _children(node, "pad"):
        if len(pad) < 3 or pad[2] not in {"np_thru_hole", "thru_hole"}:
            continue
        at, drill = _first(pad, "at"), _first(pad, "drill")
        if at is None or drill is None:
            continue
        sizes = [_number(v, "drill size") for v in drill[1:] if isinstance(v, str) and v != "oval"]
        if not sizes:
            continue
        local = (_number(at[1], "pad x"), _number(at[2], "pad y"))
        holes.append((_transform(local, footprint), min(sizes)))
    return holes


def _is_mount_hole(footprint: _BoardFootprint) -> bool:
    return footprint.lib_id.casefold().startswith("mountinghole:")


def _edge(
    bbox: tuple[float, float, float, float] | None, half_w: float, half_d: float
) -> Face | None:
    if bbox is None:
        return None
    distances: dict[Face, float] = {
        "left": bbox[0] + half_w,
        "front": bbox[1] + half_d,
        "right": half_w - bbox[2],
        "back": half_d - bbox[3],
    }
    face = min(distances, key=lambda key: distances[key])
    return face if distances[face] <= EDGE_TOLERANCE_MM else None


def board_geometry(
    pcb_path: Path,
    part_specs: dict[str, Path] | None = None,
    step_path: Path | None = None,
) -> BoardGeometry:
    part_specs = part_specs or {}
    root = sexpr.parse_text(pcb_path.read_text(encoding="utf-8"))
    _, polygon = _outline(root)
    thickness = _board_thickness(root)
    specs: dict[str, PartSpec] = {ref: load_part_spec(path) for ref, path in part_specs.items()}
    unknown: list[str] = []
    if len(polygon) < 3:
        unknown.append("outline")
    if thickness is None:
        unknown.append("thickness")
    xs = [p[0] for p in polygon] or [0.0]
    ys = [p[1] for p in polygon] or [0.0]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    half_w, half_d = (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2

    def board(point: Point) -> tuple[float, float]:
        return round(point[0] - cx, 4), round(cy - point[1], 4)

    outline = [board(point) for point in polygon]
    if len(outline) >= 3 and _signed_area(outline) < 0:
        outline.reverse()
    holes: list[MountHole] = []
    components: list[Component] = []
    for node in _children(root, "footprint"):
        footprint = _parse_board_footprint(node)
        if _is_mount_hole(footprint):
            for point, diameter in _mount_holes(node, footprint):
                x, y = board(point)
                holes.append(MountHole(ref=footprint.ref, x_mm=x, y_mm=y, diameter_mm=diameter))
            continue
        corners = [board(_transform(point, footprint)) for point in footprint.courtyard]
        bbox = (
            (
                min(p[0] for p in corners),
                min(p[1] for p in corners),
                max(p[0] for p in corners),
                max(p[1] for p in corners),
            )
            if corners
            else None
        )
        height, sources = _height_evidence(footprint, specs.get(footprint.ref), pcb_path)
        if height is None:
            unknown.append(f"height:{footprint.ref}")
        connector = _is_connector(footprint, part_specs)
        x, y = board((footprint.x, footprint.y))
        components.append(
            Component(
                ref=footprint.ref,
                lib_id=footprint.lib_id,
                side="bottom" if footprint.side == "B.Cu" else "top",
                x_mm=x,
                y_mm=y,
                rotation_deg=footprint.rotation,
                bbox_mm=bbox,
                height_mm=None if height is None else round(height, 4),
                height_sources=sources,
                connector=connector,
                edge=_edge(bbox, half_w, half_d) if connector else None,
            )
        )
    components.sort(key=lambda item: item.ref)
    holes.sort(key=lambda item: (item.ref, item.x_mm, item.y_mm))

    def side_max(side: str) -> float | None:
        heights = [c.height_mm for c in components if c.side == side]
        if any(h is None for h in heights):
            return None
        return max((h for h in heights if h is not None), default=0.0)

    step: StepModel | None = None
    if step_path is not None:
        try:
            valid = occt.inspect(occt.read_step(step_path)).valid
        except Exception:
            valid = False
        step = StepModel(path=str(step_path), sha256=_sha(step_path), valid=valid)
        if not valid:
            unknown.append("step")
    return BoardGeometry(
        pcb=SourceFile(path=str(pcb_path), sha256=_sha(pcb_path)),
        part_specs={ref: _sha(path) for ref, path in sorted(part_specs.items())},
        step=step,
        width_mm=round(2 * half_w, 4) if polygon else None,
        depth_mm=round(2 * half_d, 4) if polygon else None,
        thickness_mm=thickness,
        outline_mm=outline,
        mount_holes=holes,
        components=components,
        max_height_top_mm=side_max("top"),
        max_height_bottom_mm=side_max("bottom"),
        unknown=unknown,
        verdict="fail" if unknown else "pass",
    )


def idf_files(geometry: BoardGeometry, design: str) -> dict[str, str]:
    """IDF 3.0 board (``.emn``) and library (``.emp``) text for a passing geometry.

    Each part is exported as its axis-aligned courtyard box in the board
    frame (placement rotation 0), a conservative envelope for clearance.
    """
    if geometry.verdict != "pass" or geometry.thickness_mm is None:
        raise ValueError("IDF export needs a passing board geometry")
    header = f'"VibeBB circuit" {_IDF_DATE} 1'
    emn = [
        ".HEADER",
        f"BOARD_FILE 3.0 {header}",
        f'"{design}" MM',
        ".END_HEADER",
        ".BOARD_OUTLINE UNOWNED",
        f"{geometry.thickness_mm:.4f}",
    ]
    loop = [*geometry.outline_mm, geometry.outline_mm[0]]
    emn += [f"0 {x:.4f} {y:.4f} 0" for x, y in loop]
    emn += [".END_BOARD_OUTLINE", ".DRILLED_HOLES"]
    emn += [
        f"{h.diameter_mm:.4f} {h.x_mm:.4f} {h.y_mm:.4f} NPTH {h.ref} MTG UNOWNED"
        for h in geometry.mount_holes
    ]
    emn += [".END_DRILLED_HOLES", ".PLACEMENT"]
    emp = [".HEADER", f"LIBRARY_FILE 3.0 {header}", ".END_HEADER"]
    for c in geometry.components:
        if c.bbox_mm is None or c.height_mm is None:
            continue
        x0, y0, x1, y1 = c.bbox_mm
        half_x, half_y = (x1 - x0) / 2, (y1 - y0) / 2
        name = f"{c.ref}_ENVELOPE"
        emn += [
            f'"{name}" "{c.lib_id}" "{c.ref}"',
            f"{(x0 + x1) / 2:.4f} {(y0 + y1) / 2:.4f} 0 0 {c.side.upper()} PLACED",
        ]
        emp += [f'.ELECTRICAL\n"{name}" "{c.lib_id}" MM {c.height_mm:.4f}']
        emp += [
            f"0 {x:.4f} {y:.4f} 0"
            for x, y in (
                (-half_x, -half_y),
                (half_x, -half_y),
                (half_x, half_y),
                (-half_x, half_y),
                (-half_x, -half_y),
            )
        ]
        emp.append(".END_ELECTRICAL")
    emn.append(".END_PLACEMENT")
    return {"emn": "\n".join(emn) + "\n", "emp": "\n".join(emp) + "\n"}


def write_board_geometry(
    geometry: BoardGeometry, out: Path, *, design: str, idf: bool = False
) -> dict[str, str]:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(geometry.model_dump_json(indent=2) + "\n", encoding="utf-8")
    written = {"geometry": str(out)}
    if idf and geometry.verdict == "pass":
        stem = out.name.removesuffix(".json").removesuffix(".board-geometry")
        for suffix, text in idf_files(geometry, design).items():
            path = out.with_name(f"{stem}.{suffix}")
            path.write_text(text, encoding="utf-8")
            written[suffix] = str(path)
    return written


def board_geometry_result(geometry: BoardGeometry, written: dict[str, str]) -> dict[str, Any]:
    return {
        "verdict": geometry.verdict,
        "artifact_kind": ARTIFACT_KIND,
        "outputs": written,
        "unknown": geometry.unknown,
        "pcb_sha256": geometry.pcb.sha256,
        "components": len(geometry.components),
        "mount_holes": len(geometry.mount_holes),
        "max_height_top_mm": geometry.max_height_top_mm,
        "max_height_bottom_mm": geometry.max_height_bottom_mm,
    }


__all__ = [
    "BoardGeometry",
    "board_geometry",
    "board_geometry_result",
    "idf_files",
    "write_board_geometry",
]
