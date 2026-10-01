"""Deterministic nominal package model generation."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from . import occt
from .libitems import FootprintDef, PadDef, parse_footprint
from .partspec import PartSpec

GENERATOR_VERSION = "1"
_SUPPORTED = {
    "chip",
    "gullwing_dual",
    "gullwing_quad",
    "no_lead_dual",
    "no_lead_quad",
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


def _nominal(dimension: object, field: str) -> float:
    value = getattr(dimension, "nom", None)
    if value is None or not math.isfinite(value) or value <= 0:
        raise Model3dError(f"{field} must have a positive nominal value")
    return float(value)


def _pad_extents(pad: PadDef) -> tuple[float, float]:
    angle = math.radians(pad.rotation)
    extent_x = abs(pad.width * math.cos(angle)) + abs(pad.height * math.sin(angle))
    extent_y = abs(pad.width * math.sin(angle)) + abs(pad.height * math.cos(angle))
    return max(extent_x, 0.01), max(extent_y, 0.01)


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


def _pad_box(
    pad: PadDef,
    z: float,
    thickness: float,
    *,
    width_limit: float | None = None,
    height_limit: float | None = None,
) -> occt.Shape:
    extent_x, extent_y = _pad_extents(pad)
    if width_limit is not None:
        extent_x = min(extent_x, width_limit)
    if height_limit is not None:
        extent_y = min(extent_y, height_limit)
    return occt.box(
        pad.x - extent_x / 2,
        pad.y - extent_y / 2,
        z,
        extent_x,
        extent_y,
        thickness,
    )


def _copper_pads(footprint: FootprintDef) -> dict[str, list[PadDef]]:
    pads: dict[str, list[PadDef]] = {}
    for pad in footprint.pads:
        if (
            pad.number
            and pad.type != "np_thru_hole"
            and any(layer.endswith(".Cu") for layer in pad.layers)
        ):
            pads.setdefault(pad.number, []).append(pad)
    return pads


def _pad_side(
    pad: PadDef,
    *,
    body_width: float,
    body_length: float,
) -> str:
    x_distance = abs(pad.x) - body_width / 2
    y_distance = abs(pad.y) - body_length / 2
    if max(x_distance, y_distance) <= 0:
        raise Model3dError(f"terminal pad {pad.number} is not outside the package body")
    return "y" if y_distance >= x_distance else "x"


def _validate_pitch(
    pads: dict[str, list[PadDef]],
    *,
    family: str,
    body_width: float,
    body_length: float,
    pitch: float | None,
    excluded_number: str | None,
) -> None:
    if pitch is None or family == "chip":
        return
    rows: dict[tuple[str, float], set[float]] = defaultdict(set)
    for copies in pads.values():
        for pad in copies:
            if pad.number == excluded_number:
                continue
            side = _pad_side(pad, body_width=body_width, body_length=body_length)
            if side == "y":
                rows[(side, round(pad.y, 4))].add(round(pad.x, 4))
            else:
                rows[(side, round(pad.x, 4))].add(round(pad.y, 4))
    for positions in rows.values():
        ordered = sorted(positions)
        if any(abs((right - left) - pitch) > 0.01 for left, right in pairwise(ordered)):
            raise Model3dError("footprint terminal spacing does not match nominal pitch")


def _validate_lead_span(
    pads: dict[str, list[PadDef]],
    *,
    body_width: float,
    body_length: float,
    lead_span: float | None,
) -> None:
    if lead_span is None:
        return
    extents: list[float] = []
    for copies in pads.values():
        for pad in copies:
            extent_x, extent_y = _pad_extents(pad)
            if abs(pad.x) > body_width / 2:
                extents.append(2 * (abs(pad.x) + extent_x / 2))
            if abs(pad.y) > body_length / 2:
                extents.append(2 * (abs(pad.y) + extent_y / 2))
    if extents and max(extents) > lead_span + 0.05:
        raise Model3dError("footprint terminal span exceeds nominal lead_span")


def _validate_pins_per_side(
    pads: dict[str, list[PadDef]],
    *,
    body_width: float,
    body_length: float,
    expected: tuple[int, int, int, int] | None,
) -> None:
    if expected is None:
        return
    counts = {"top": 0, "right": 0, "bottom": 0, "left": 0}
    for copies in pads.values():
        for pad in copies:
            if abs(pad.y) - body_length / 2 >= abs(pad.x) - body_width / 2:
                counts["top" if pad.y < 0 else "bottom"] += 1
            else:
                counts["left" if pad.x < 0 else "right"] += 1
    actual = (counts["top"], counts["right"], counts["bottom"], counts["left"])
    if actual != expected:
        raise Model3dError("footprint side counts do not match pins_per_side")


def _lead_terminal(
    pad: PadDef,
    *,
    side: str,
    lead_width: float,
    lead_length: float,
    standoff: float,
    body_width: float,
    body_length: float,
) -> occt.Shape:
    extent_x, extent_y = _pad_extents(pad)
    if side == "y":
        tangent = min(extent_x, lead_width)
        radial = min(extent_y, lead_length)
        sign = -1.0 if pad.y < 0 else 1.0
        body_edge = sign * body_length / 2
        foot_inner = pad.y + sign * radial / 2
        shoulder = occt.box(
            pad.x - tangent / 2,
            min(body_edge, foot_inner),
            0.15,
            tangent,
            max(abs(foot_inner - body_edge), 0.01),
            max(standoff - 0.15, 0.01),
        )
        foot = occt.box(
            pad.x - tangent / 2,
            pad.y - radial / 2,
            0.0,
            tangent,
            radial,
            0.15,
        )
    else:
        tangent = min(extent_y, lead_width)
        radial = min(extent_x, lead_length)
        sign = -1.0 if pad.x < 0 else 1.0
        body_edge = sign * body_width / 2
        foot_inner = pad.x + sign * radial / 2
        shoulder = occt.box(
            min(body_edge, foot_inner),
            pad.y - tangent / 2,
            0.15,
            max(abs(foot_inner - body_edge), 0.01),
            tangent,
            max(standoff - 0.15, 0.01),
        )
        foot = occt.box(
            pad.x - radial / 2,
            pad.y - tangent / 2,
            0.0,
            radial,
            tangent,
            0.15,
        )
    return occt.fuse((foot, shoulder)) if standoff > 0.15 else foot


def generate_model(spec: PartSpec, footprint: Path, out: Path) -> GeneratedModel:
    family = spec.package.family
    if family not in _SUPPORTED:
        raise Model3dError("unsupported_family")
    try:
        parsed = parse_footprint(footprint)
        body_length = _nominal(spec.package.body_length, "body_length")
        body_width = _nominal(spec.package.body_width, "body_width")
        height = _nominal(spec.package.height, "height")
        standoff = (
            _nominal(spec.package.standoff, "standoff")
            if spec.package.standoff is not None
            else 0.0
        )
        pitch = _nominal(spec.package.pitch, "pitch") if spec.package.pitch is not None else None
        lead_span = (
            _nominal(spec.package.lead_span, "lead_span")
            if spec.package.lead_span is not None
            else None
        )
        lead_length = (
            _nominal(spec.package.lead_length, "lead_length")
            if spec.package.lead_length is not None
            else None
        )
        lead_width = (
            _nominal(spec.package.lead_width, "lead_width")
            if spec.package.lead_width is not None
            else None
        )
        if family.startswith(("gullwing_", "no_lead_")) and (
            lead_length is None or lead_width is None
        ):
            raise Model3dError("lead_length and lead_width are required for this family")
        body_bottom = max(standoff, 0.2)
        body_top = body_bottom + height
        body = occt.box(
            -body_width / 2,
            -body_length / 2,
            body_bottom,
            body_width,
            body_length,
            height,
        )
        copper = _copper_pads(parsed)
        exposed = spec.package.exposed_pad
        _validate_pitch(
            copper,
            family=family,
            body_width=body_width,
            body_length=body_length,
            pitch=pitch,
            excluded_number=exposed.number if exposed is not None else None,
        )
        _validate_lead_span(
            copper,
            body_width=body_width,
            body_length=body_length,
            lead_span=lead_span,
        )
        _validate_pins_per_side(
            {
                number: pads
                for number, pads in copper.items()
                if exposed is None or number != exposed.number
            },
            body_width=body_width,
            body_length=body_length,
            expected=spec.package.pins_per_side,
        )
        terminal_numbers = sorted(
            (set(copper) - ({exposed.number} if exposed is not None else set())),
            key=lambda value: (not value.isdigit(), int(value) if value.isdigit() else value),
        )
        terminals: list[occt.Shape] = []
        terminal_map: list[dict[str, object]] = []
        for number in terminal_numbers:
            for pad in copper[number]:
                solid_index = len(terminals) + 1
                if family == "chip":
                    terminals.append(_pad_box(pad, 0.0, 0.2))
                elif family.startswith("gullwing_"):
                    if lead_length is None or lead_width is None:
                        raise Model3dError("lead dimensions are unavailable")
                    terminals.append(
                        _lead_terminal(
                            pad,
                            side=_pad_side(
                                pad,
                                body_width=body_width,
                                body_length=body_length,
                            ),
                            lead_width=lead_width,
                            lead_length=lead_length,
                            standoff=body_bottom,
                            body_width=body_width,
                            body_length=body_length,
                        )
                    )
                else:
                    if lead_length is None or lead_width is None:
                        raise Model3dError("lead dimensions are unavailable")
                    side = _pad_side(
                        pad,
                        body_width=body_width,
                        body_length=body_length,
                    )
                    terminal_width = lead_width if side == "y" else lead_length
                    terminal_height = lead_width if side == "x" else lead_length
                    terminals.append(
                        _pad_box(
                            pad,
                            0.0,
                            0.2,
                            width_limit=terminal_width,
                            height_limit=terminal_height,
                        )
                    )
                terminal_map.append(
                    {
                        "number": number,
                        "pad_center_mm": [pad.x, pad.y],
                        "solid_index": solid_index,
                    }
                )
        if exposed is not None:
            ep_width = _nominal(exposed.width, "exposed_pad.width")
            ep_length = _nominal(exposed.length, "exposed_pad.length")
            ep_pad = copper.get(exposed.number, [])
            ep_x = ep_pad[0].x if ep_pad else 0.0
            ep_y = ep_pad[0].y if ep_pad else 0.0
            solid_index = len(terminals) + 1
            terminals.append(
                occt.box(
                    ep_x - ep_width / 2,
                    ep_y - ep_length / 2,
                    0.0,
                    ep_width,
                    ep_length,
                    0.2,
                )
            )
            terminal_map.append(
                {
                    "number": exposed.number,
                    "pad_center_mm": [ep_x, ep_y],
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
        target_dir = out / f"{footprint.parent.stem.removesuffix('.pretty')}.3dshapes"
        target_dir.mkdir(parents=True, exist_ok=True)
        step_path = target_dir / f"{footprint.stem}.step"
        manifest_path = Path(f"{step_path}.gen.json")
        occt.write_step(model, step_path, product_name=footprint.stem)
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
