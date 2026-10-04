"""Deterministic nominal package model generation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from . import occt
from .landpattern import Side, bga_pin_positions, standard_pin_placements
from .partspec import Dimension, PartSpec

GENERATOR_VERSION = "2"
_SUPPORTED = {
    "chip",
    "gullwing_dual",
    "gullwing_quad",
    "no_lead_dual",
    "no_lead_quad",
    "sot223",
    "tabbed_dpak",
    "sod",
    "bga",
}


class Model3dError(ValueError):
    """Raised when a nominal model cannot be generated."""


class GeneratedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_path: Path
    manifest_path: Path
    step_sha256: str
    spec_sha256: str
    footprint_sha256: str
    generator_version: str
    marker: str | None
    marker_note: str | None


@dataclass(frozen=True)
class ExpectedTerminal:
    number: str
    center_xy: tuple[float, float]
    size_xy: tuple[float, float]


def _nominal(
    dimension: Dimension | None,
    field: str,
    derived_nominals: dict[str, str],
    *,
    allow_zero: bool = False,
) -> float:
    if dimension is None:
        raise Model3dError(f"{field} is unavailable")
    value = dimension.nom
    derived = value is None
    if value is None:
        if dimension.min is None or dimension.max is None:
            raise Model3dError(f"{field} requires a nominal value or both min and max")
        value = (dimension.min + dimension.max) / 2
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        requirement = "non-negative" if allow_zero else "positive"
        raise Model3dError(f"{field} must have a {requirement} nominal value")
    if derived:
        derived_nominals[field] = "midpoint"
    return float(value)


def _coordinate(
    dimension: Dimension | None,
    field: str,
    derived_nominals: dict[str, str],
) -> float:
    if dimension is None:
        return 0.0
    value = dimension.nom
    derived = value is None
    if value is None:
        if dimension.min is None or dimension.max is None:
            raise Model3dError(f"{field} requires a nominal value or both min and max")
        value = (dimension.min + dimension.max) / 2
    if not math.isfinite(value):
        raise Model3dError(f"{field} must be finite")
    if derived:
        derived_nominals[field] = "midpoint"
    return float(value)


def footprint_to_board_xy(
    x: float,
    y: float,
    *,
    rotation_deg: float,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> tuple[float, float]:
    angle = math.radians(rotation_deg)
    return (
        origin_x + math.cos(angle) * x - math.sin(angle) * y,
        origin_y + math.sin(angle) * x + math.cos(angle) * y,
    )


def _numbered_pin_positions(spec: PartSpec) -> list[tuple[str, Side, float]]:
    if spec.package.family == "bga":
        return []
    positions = standard_pin_placements(spec)
    corner = spec.package.pin1_corner
    family = spec.package.family
    if family in {"sot223", "tabbed_dpak", "sod"}:
        return positions
    if family == "chip":
        anchor_side = "left" if corner.endswith("left") else "right"
        anchor_position = 0.0
    elif family.endswith("dual"):
        anchors = {
            "top_left": ("left", "min"),
            "bottom_left": ("left", "max"),
            "top_right": ("right", "min"),
            "bottom_right": ("right", "max"),
        }
        anchor_side, anchor_end = anchors[corner]
        row_positions = [position for _, side, position in positions if side == anchor_side]
        anchor_position = min(row_positions) if anchor_end == "min" else max(row_positions)
    else:
        anchors = {
            "top_left": ("left", "min"),
            "bottom_left": ("bottom", "min"),
            "bottom_right": ("right", "max"),
            "top_right": ("top", "max"),
        }
        anchor_side, anchor_end = anchors[corner]
        row_positions = [position for _, side, position in positions if side == anchor_side]
        anchor_position = min(row_positions) if anchor_end == "min" else max(row_positions)
    start = next(
        (
            index
            for index, (_, side, position) in enumerate(positions)
            if side == anchor_side and math.isclose(position, anchor_position, abs_tol=1e-9)
        ),
        None,
    )
    if start is None:
        raise Model3dError(f"pin1_corner {corner} does not align with a package terminal row")
    ordered = positions[start:] + positions[:start]
    return [(str(index + 1), side, position) for index, (_, side, position) in enumerate(ordered)]


def expected_terminals(spec: PartSpec) -> list[ExpectedTerminal]:
    family = spec.package.family
    if family not in _SUPPORTED:
        raise Model3dError("unsupported_family")
    derived_nominals: dict[str, str] = {}
    body_length = _nominal(spec.package.body_length, "body_length", derived_nominals)
    body_width = _nominal(spec.package.body_width, "body_width", derived_nominals)
    lead_length = (
        _nominal(spec.package.lead_length, "lead_length", derived_nominals)
        if family != "bga"
        else 0.0
    )
    lead_width = (
        _nominal(spec.package.lead_width, "lead_width", derived_nominals)
        if family not in {"chip", "bga"}
        else body_width
    )
    lead_span = (
        _nominal(spec.package.lead_span, "lead_span", derived_nominals)
        if family.startswith("gullwing_") or family in {"sot223", "tabbed_dpak", "sod"}
        else None
    )
    terminals: list[ExpectedTerminal] = []
    if family == "bga":
        if spec.package.ball_grid is None:
            raise Model3dError("package.ball_grid is required")
        ball_diameter = _nominal(
            spec.package.ball_grid.ball_diameter,
            "ball_grid.ball_diameter",
            derived_nominals,
        )
        terminals.extend(
            ExpectedTerminal(number, (x, y), (ball_diameter, ball_diameter))
            for number, x, y in bga_pin_positions(spec)
        )
    for number, side, position in _numbered_pin_positions(spec):
        sign = -1.0 if side in {"left", "top"} else 1.0
        if family == "chip":
            center = (sign * (body_length / 2 + lead_length / 2), 0.0)
            size = (lead_length, body_width)
        elif family == "sod":
            if lead_span is None:
                raise Model3dError("package.lead_span is required for SOD terminals")
            center = (sign * (lead_span / 2 - lead_length / 2), 0.0)
            size = (lead_length, lead_width)
        elif family in {"sot223", "tabbed_dpak"}:
            if lead_span is None:
                raise Model3dError("package.lead_span is required for package terminals")
            center = (position, lead_span / 2 - lead_length / 2)
            size = (lead_width, lead_length)
        else:
            if side in {"left", "right"}:
                radial_extent = lead_span if lead_span is not None else body_width
                center = (sign * (radial_extent / 2 - lead_length / 2), position)
                size = (lead_length, lead_width)
            else:
                radial_extent = lead_span if lead_span is not None else body_length
                center = (position, sign * (radial_extent / 2 - lead_length / 2))
                size = (lead_width, lead_length)
        terminals.append(ExpectedTerminal(number, center, size))
    for index, exposed in enumerate(spec.package.all_exposed_pads):
        ep_width = _nominal(exposed.width, f"exposed_pads[{index}].width", derived_nominals)
        ep_length = _nominal(exposed.length, f"exposed_pads[{index}].length", derived_nominals)
        center_x = _coordinate(
            exposed.center_x,
            f"exposed_pads[{index}].center_x",
            derived_nominals,
        )
        center_y = _coordinate(
            exposed.center_y,
            f"exposed_pads[{index}].center_y",
            derived_nominals,
        )
        terminals.append(
            ExpectedTerminal(exposed.number, (center_x, center_y), (ep_width, ep_length))
        )
    if spec.package.tab is not None:
        tab = spec.package.tab
        terminals.append(
            ExpectedTerminal(
                tab.number,
                (
                    0.0,
                    -_nominal(tab.offset, "tab.offset", derived_nominals),
                ),
                (
                    _nominal(tab.width, "tab.width", derived_nominals),
                    _nominal(tab.length, "tab.length", derived_nominals),
                ),
            )
        )
    return terminals


def _validate_pitch(
    spec: PartSpec,
    positions: list[tuple[str, Side, float]],
    *,
    body_width: float,
    body_length: float,
    pitch: float | None,
    lead_width: float | None,
) -> None:
    if spec.package.family in {"chip", "sod"}:
        if len(positions) != spec.package.pin_count:
            raise Model3dError("package terminal count does not match pin_count")
        return
    if spec.package.family == "bga":
        return
    if pitch is None:
        raise Model3dError("package.pitch is required for this family")
    if lead_width is None:
        raise Model3dError("package.lead_width is required for this family")
    if lead_width >= pitch:
        raise Model3dError("package.lead_width must be smaller than package.pitch")
    if len(positions) != spec.package.pin_count:
        raise Model3dError("package terminal count does not match pin_count")
    rows: dict[str, list[tuple[str, float]]] = {}
    for number, side, position in positions:
        rows.setdefault(side, []).append((number, position))
    for side, row in rows.items():
        ordered = sorted(row, key=lambda item: item[1])
        for (left_number, left), (right_number, right) in pairwise(ordered):
            try:
                skipped_slots = abs(int(right_number) - int(left_number))
            except ValueError:
                skipped_slots = 1
            if skipped_slots < 1 or not math.isclose(
                right - left,
                pitch * skipped_slots,
                abs_tol=1e-9,
            ):
                raise Model3dError("package terminal rows do not match package.pitch")
        positions_only = [position for _, position in ordered]
        if len(positions_only) != len(set(positions_only)):
            raise Model3dError("package terminal rows do not match package.pitch")
        side_extent = body_length if side in {"left", "right"} else body_width
        if positions_only[-1] - positions_only[0] + lead_width > side_extent + 1e-6:
            raise Model3dError("package.pitch and lead_width exceed the package body extent")


def _radial_box(
    side: str,
    position: float,
    *,
    outer_edge: float,
    radial_length: float,
    tangent_width: float,
    z: float,
    thickness: float,
) -> tuple[occt.Shape, tuple[float, float]]:
    sign = -1.0 if side in {"left", "top"} else 1.0
    inner_edge = outer_edge - sign * radial_length
    radial_start = min(outer_edge, inner_edge)
    if side in {"left", "right"}:
        shape = occt.box(
            radial_start,
            position - tangent_width / 2,
            z,
            radial_length,
            tangent_width,
            thickness,
        )
        center = ((outer_edge + inner_edge) / 2, position)
    else:
        shape = occt.box(
            position - tangent_width / 2,
            radial_start,
            z,
            tangent_width,
            radial_length,
            thickness,
        )
        center = (position, (outer_edge + inner_edge) / 2)
    return shape, center


def _gullwing_terminal(
    side: str,
    terminal: ExpectedTerminal,
    *,
    body_bottom: float,
    body_width: float,
    body_length: float,
    lead_span: float,
) -> tuple[occt.Shape, tuple[float, float]]:
    sign = -1.0 if side in {"left", "top"} else 1.0
    lead_length = terminal.size_xy[0] if side in {"left", "right"} else terminal.size_xy[1]
    lead_width = terminal.size_xy[1] if side in {"left", "right"} else terminal.size_xy[0]
    position = terminal.center_xy[1] if side in {"left", "right"} else terminal.center_xy[0]
    outer_edge = (
        terminal.center_xy[0] + sign * lead_length / 2
        if side in {"left", "right"}
        else terminal.center_xy[1] + sign * lead_length / 2
    )
    foot_height = min(0.15, body_bottom)
    foot, center = _radial_box(
        side,
        position,
        outer_edge=outer_edge,
        radial_length=lead_length,
        tangent_width=lead_width,
        z=0.0,
        thickness=foot_height,
    )
    if body_bottom <= foot_height:
        return foot, center
    body_extent = body_width if side in {"left", "right"} else body_length
    inner_edge = sign * (lead_span / 2 - lead_length)
    body_edge = sign * body_extent / 2
    shoulder_length = abs(inner_edge - body_edge)
    if shoulder_length <= 1e-9:
        return foot, center
    overlap = 1e-4
    shoulder_edge = inner_edge + sign * overlap
    shoulder_z = foot_height - overlap
    if side in {"left", "right"}:
        shoulder = occt.box(
            min(shoulder_edge, body_edge),
            position - lead_width / 2,
            shoulder_z,
            abs(shoulder_edge - body_edge),
            lead_width,
            body_bottom - shoulder_z,
        )
    else:
        shoulder = occt.box(
            position - lead_width / 2,
            min(shoulder_edge, body_edge),
            shoulder_z,
            lead_width,
            abs(shoulder_edge - body_edge),
            body_bottom - shoulder_z,
        )
    return occt.fuse((foot, shoulder)), center


def _safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._+-]+", "_", value).strip("._")
    return name or "model"


def generate_model(spec: PartSpec, footprint: Path, out: Path) -> GeneratedModel:
    family = spec.package.family
    if family not in _SUPPORTED:
        raise Model3dError("unsupported_family")
    try:
        derived_nominals: dict[str, str] = {}
        body_length = _nominal(spec.package.body_length, "body_length", derived_nominals)
        body_width = _nominal(spec.package.body_width, "body_width", derived_nominals)
        height = _nominal(spec.package.height, "height", derived_nominals)
        standoff = (
            _nominal(
                spec.package.standoff,
                "standoff",
                derived_nominals,
                allow_zero=True,
            )
            if spec.package.standoff is not None
            else 0.0
        )
        pitch = (
            _nominal(spec.package.pitch, "pitch", derived_nominals)
            if family not in {"chip", "bga"} and spec.package.pitch is not None
            else None
        )
        ball_diameter = (
            _nominal(
                spec.package.ball_grid.ball_diameter,
                "ball_grid.ball_diameter",
                derived_nominals,
            )
            if family == "bga" and spec.package.ball_grid is not None
            else None
        )
        lead_span = (
            _nominal(spec.package.lead_span, "lead_span", derived_nominals)
            if (family.startswith("gullwing_") or family in {"sot223", "tabbed_dpak", "sod"})
            and spec.package.lead_span is not None
            else None
        )
        lead_length = (
            _nominal(spec.package.lead_length, "lead_length", derived_nominals)
            if spec.package.lead_length is not None
            else (0.0 if family == "bga" else None)
        )
        lead_width = (
            _nominal(spec.package.lead_width, "lead_width", derived_nominals)
            if family not in {"chip", "bga"} and spec.package.lead_width is not None
            else None
        )
        if lead_length is None:
            raise Model3dError("package.lead_length is required for package terminals")
        if family not in {"chip", "bga"} and lead_width is None:
            raise Model3dError("package.lead_width is required for this family")
        if (
            family.startswith("gullwing_") or family in {"sot223", "tabbed_dpak", "sod"}
        ) and lead_span is None:
            raise Model3dError("package.lead_span is required for gullwing terminals")
        if family == "gullwing_dual" and lead_span is not None and lead_span < body_width:
            raise Model3dError("package.lead_span must enclose the gullwing body width")
        if (
            family == "gullwing_quad"
            and lead_span is not None
            and lead_span < max(body_width, body_length)
        ):
            raise Model3dError("package.lead_span must enclose the gullwing body extents")
        numbered_positions = _numbered_pin_positions(spec)
        expected_by_number = {item.number: item for item in expected_terminals(spec)}
        _validate_pitch(
            spec,
            numbered_positions,
            body_width=body_width,
            body_length=body_length,
            pitch=pitch,
            lead_width=lead_width,
        )
        body_bottom = max(standoff, ball_diameter or 0.03)
        body_top = height
        if body_top <= body_bottom:
            raise Model3dError("height must exceed the body bottom")
        body_x = body_length if family == "chip" else body_width
        body_y = body_width if family == "chip" else body_length
        body = occt.box(
            -body_x / 2,
            -body_y / 2,
            body_bottom,
            body_x,
            body_y,
            body_top - body_bottom,
        )
        terminals: list[occt.Shape] = []
        terminal_map: list[dict[str, object]] = []
        bga_positions = bga_pin_positions(spec) if family == "bga" else []
        if family == "bga":
            if ball_diameter is None:
                raise Model3dError("package.ball_grid.ball_diameter is required")
            for number, x, y in bga_positions:
                solid_index = len(terminals) + 1
                terminals.append(occt.cylinder(x, y, 0.0, ball_diameter / 2, ball_diameter))
                terminal_map.append(
                    {
                        "number": number,
                        "terminal_center_mm": [x, y],
                        "terminal_size_mm": [ball_diameter, ball_diameter],
                        "solid_index": solid_index,
                    }
                )
        for number, side, _position in numbered_positions:
            expected = expected_by_number[number]
            solid_index = len(terminals) + 1
            if family == "chip":
                center = expected.center_xy
                size_x, size_y = expected.size_xy
                terminals.append(
                    occt.box(
                        center[0] - size_x / 2,
                        center[1] - size_y / 2,
                        0.0,
                        size_x,
                        size_y,
                        height,
                    )
                )
            elif family.startswith("gullwing_") or family in {
                "sot223",
                "tabbed_dpak",
                "sod",
            }:
                if lead_span is None or lead_width is None:
                    raise Model3dError("gullwing lead dimensions are unavailable")
                terminal, center = _gullwing_terminal(
                    side,
                    expected,
                    body_bottom=body_bottom,
                    body_width=body_width,
                    body_length=body_length,
                    lead_span=lead_span,
                )
                center = expected.center_xy
                terminals.append(terminal)
            else:
                center = expected.center_xy
                size_x, size_y = expected.size_xy
                terminal = occt.box(
                    center[0] - size_x / 2,
                    center[1] - size_y / 2,
                    0.0,
                    size_x,
                    size_y,
                    min(0.2, height),
                )
                terminals.append(terminal)
            terminal_map.append(
                {
                    "number": number,
                    "terminal_center_mm": [center[0], center[1]],
                    "terminal_size_mm": list(expected.size_xy),
                    "solid_index": solid_index,
                }
            )
        terminal_numbers = [number for number, _, _ in numbered_positions] + [
            number for number, _, _ in bga_positions
        ]
        tab_number = spec.package.tab.number if spec.package.tab is not None else None
        for auxiliary in expected_by_number.values():
            if auxiliary.number in terminal_numbers:
                continue
            if auxiliary.number == tab_number:
                pass
            elif (
                abs(auxiliary.center_xy[0]) + auxiliary.size_xy[0] / 2 > body_width / 2
                or abs(auxiliary.center_xy[1]) + auxiliary.size_xy[1] / 2 > body_length / 2
            ):
                raise Model3dError("exposed_pad dimensions exceed the package body extents")
            solid_index = len(terminals) + 1
            terminals.append(
                occt.box(
                    auxiliary.center_xy[0] - auxiliary.size_xy[0] / 2,
                    auxiliary.center_xy[1] - auxiliary.size_xy[1] / 2,
                    0.0,
                    auxiliary.size_xy[0],
                    auxiliary.size_xy[1],
                    min(0.2, height),
                )
            )
            terminal_numbers.append(auxiliary.number)
            terminal_map.append(
                {
                    "number": auxiliary.number,
                    "terminal_center_mm": list(auxiliary.center_xy),
                    "terminal_size_mm": list(auxiliary.size_xy),
                    "solid_index": solid_index,
                }
            )
        marker_corner: str | None = None
        marker_note: str | None = None
        if family != "chip":
            marker_corner = spec.package.pin1_corner
            radius = max(0.15, min(0.5, 0.1 * min(body_width, body_length)))
            x = -body_width * 0.32 if marker_corner.endswith("left") else body_width * 0.32
            y = -body_length * 0.32 if marker_corner.startswith("top") else body_length * 0.32
            body = occt.cylinder_cut(body, x=x, y=y, top_z=body_top, radius=radius, depth=0.05)
        else:
            marker_note = "PartSpec has no explicit chip-polarity field; no marker was added."
        model = occt.compound((body, *terminals))
        target_dir = out / f"{_safe_name(spec.mpn)}.3dshapes"
        target_dir.mkdir(parents=True, exist_ok=True)
        step_path = target_dir / f"{_safe_name(spec.package.drawing_id)}.step"
        manifest_path = Path(f"{step_path}.gen.json")
        occt.write_step(model, step_path, product_name=_safe_name(spec.mpn))
        step_sha = hashlib.sha256(step_path.read_bytes()).hexdigest()
        spec_sha = hashlib.sha256(
            json.dumps(
                spec.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        footprint_sha = hashlib.sha256(footprint.read_bytes()).hexdigest()
        manifest = {
            "artifact_kind": "circuit_generated_model",
            "generator_version": GENERATOR_VERSION,
            "spec_sha256": spec_sha,
            "footprint_sha256": footprint_sha,
            "step_sha256": step_sha,
            "derived_nominals": derived_nominals,
            "parameters": {
                "body_length_mm": body_length,
                "body_width_mm": body_width,
                "height_mm": height,
                "standoff_mm": standoff,
                "body_bottom_mm": body_bottom,
                "family": family,
                "pitch_mm": pitch,
                "lead_span_mm": lead_span,
                "lead_length_mm": lead_length,
                "lead_width_mm": lead_width,
                "pins_per_side": spec.package.pins_per_side,
                "terminal_numbers": terminal_numbers,
                "terminal_map": terminal_map,
                "marker": marker_corner,
                "marker_note": marker_note,
            },
        }
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        return GeneratedModel(
            step_path=step_path,
            manifest_path=manifest_path,
            step_sha256=step_sha,
            spec_sha256=spec_sha,
            footprint_sha256=footprint_sha,
            generator_version=GENERATOR_VERSION,
            marker=marker_corner,
            marker_note=marker_note,
        )
    except Model3dError:
        raise
    except Exception as exc:
        raise Model3dError(str(exc)) from exc
