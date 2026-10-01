"""Verification of authored KiCad symbols, footprints, models, and provenance."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import kicad_cli
from . import pinout as pinout_oracle
from .datasheet import load_extraction
from .landpattern import Density, LandPatternResult, Rect, lead_rects
from .libitems import (
    FootprintDef,
    GraphicDef,
    LibItemError,
    PadDef,
    SymbolDef,
    parse_footprint,
    parse_symbol,
)
from .libsource import (
    LibrarySourceError,
    assert_safe_destination,
    load_library_provenance,
)
from .partspec import (
    Dimension,
    LandPad,
    PartSpec,
    PartSpecReport,
    check_part_spec,
    part_spec_sha256,
)
from .pinout import PinoutGeometry


class VerifyFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["error", "warning", "info"]
    subject: str
    message: str


class VerifiedSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lib_path: Path
    name: str
    sha256: str | None


class VerifiedFootprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    name: str
    sha256: str | None


class VerifiedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    resolved: bool
    sha256: str | None


class VerificationInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_spec_path: Path
    symbol_lib: Path
    symbol_name: str
    footprint_path: Path
    density: Density
    tolerance_mm: float
    model_required: bool


class LibraryVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_library_verification"]
    verdict: Literal["pass", "fail"]
    part_spec_sha256: str
    inputs: VerificationInputs
    symbol: VerifiedSymbol
    footprint: VerifiedFootprint
    models: list[VerifiedModel]
    findings: list[VerifyFinding]


_SYMBOL_TYPE_ALIASES = {
    "open collector": "open_collector",
    "open emitter": "open_emitter",
    "no connect": "no_connect",
}


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _finding(
    findings: list[VerifyFinding],
    code: str,
    severity: Literal["error", "warning", "info"],
    subject: str,
    message: str,
) -> None:
    findings.append(VerifyFinding(code=code, severity=severity, subject=subject, message=message))


def _normalized_name(value: str) -> str:
    return re.sub(r"~\{([^}]*)\}", r"\1", value).casefold()


def _symbol_pin_groups(
    symbol: SymbolDef,
) -> tuple[Counter[str], dict[str, set[str]], dict[str, set[str]]]:
    numbers = Counter(pin.number for pin in symbol.pins)
    names: dict[str, set[str]] = {}
    types: dict[str, set[str]] = {}
    for pin in symbol.pins:
        names.setdefault(pin.number, set()).add(_normalized_name(pin.name))
        pin_type = _SYMBOL_TYPE_ALIASES.get(
            pin.electrical_type.casefold(), pin.electrical_type.casefold()
        )
        types.setdefault(pin.number, set()).add(pin_type)
    return numbers, names, types


def _spec_pin_groups(
    spec: PartSpec,
) -> tuple[Counter[str], dict[str, set[str]], dict[str, set[str]]]:
    numbers = Counter(pin.number for pin in spec.pins)
    names: dict[str, set[str]] = {}
    types: dict[str, set[str]] = {}
    for pin in spec.pins:
        names.setdefault(pin.number, set()).add(_normalized_name(pin.name))
        types.setdefault(pin.number, set()).add(pin.electrical_type)
    return numbers, names, types


def _check_symbol(
    spec: PartSpec,
    symbol: SymbolDef | None,
    symbol_lib: Path,
    footprint_name: str,
    library_dir: Path | None,
    findings: list[VerifyFinding],
) -> None:
    if symbol is None:
        _finding(findings, "symbol_pin_set", "error", "symbol", "symbol could not be parsed")
        return
    expected_numbers, expected_names, expected_types = _spec_pin_groups(spec)
    actual_numbers, actual_names, actual_types = _symbol_pin_groups(symbol)
    if expected_numbers != actual_numbers:
        _finding(
            findings,
            "symbol_pin_set",
            "error",
            "symbol",
            f"symbol pin numbers {sorted(actual_numbers.elements())} do not match "
            f"PartSpec {sorted(expected_numbers.elements())}",
        )
    name_mismatches = [
        number
        for number in expected_names.keys() | actual_names.keys()
        if expected_names.get(number, set()) != actual_names.get(number, set())
    ]
    if name_mismatches:
        _finding(
            findings,
            "symbol_pin_name",
            "error",
            "symbol",
            f"pin names differ for numbers {sorted(name_mismatches)}",
        )
    type_mismatches = [
        number
        for number in expected_types.keys() | actual_types.keys()
        if expected_types.get(number, set()) != actual_types.get(number, set())
    ]
    if type_mismatches:
        _finding(
            findings,
            "symbol_pin_type",
            "error",
            "symbol",
            f"electrical types differ for numbers {sorted(type_mismatches)}",
        )
    off_grid = [
        pin.number
        for pin in symbol.pins
        if not math.isclose(pin.x / 1.27, round(pin.x / 1.27), rel_tol=0, abs_tol=1e-6)
        or not math.isclose(pin.y / 1.27, round(pin.y / 1.27), rel_tol=0, abs_tol=1e-6)
    ]
    if off_grid:
        _finding(
            findings,
            "symbol_pin_grid",
            "warning",
            "symbol",
            f"pins are not on the 1.27 mm grid: {sorted(set(off_grid))}",
        )
    for property_name in ("Reference", "Value", "Footprint", "Datasheet"):
        if not symbol.properties.get(property_name, "").strip():
            _finding(
                findings,
                "symbol_property",
                "warning",
                f"symbol.{property_name}",
                f"symbol property {property_name} is missing or empty",
            )
    if library_dir is not None:
        expected_footprint = f"{symbol_lib.stem}:{footprint_name}"
        if symbol.properties.get("Footprint", "").strip() != expected_footprint:
            _finding(
                findings,
                "symbol_property",
                "warning",
                "symbol.Footprint",
                f"Footprint property should be {expected_footprint}",
            )


def _check_pinout_geometry(
    spec: PartSpec,
    check: PartSpecReport | None,
    symbol: SymbolDef | None,
    footprint: FootprintDef | None,
    findings: list[VerifyFinding],
) -> None:
    required = spec.package.family in {
        "no_lead_quad",
        "no_lead_dual",
        "gullwing_quad",
        "gullwing_dual",
    }
    geometry: PinoutGeometry | None = check.pinout if check is not None else None
    if required and geometry is None:
        _finding(
            findings,
            "pinout_unverified",
            "error",
            "pinout",
            "required pinout geometry is missing from the fresh PartSpec check",
        )
    if geometry is None:
        return

    drawing_positions = {label.number: (label.x, label.y) for label in geometry.labels}
    pin_numbers = {str(number) for number in range(1, spec.package.pin_count + 1)}
    if footprint is not None:
        pad_positions: dict[str, list[tuple[float, float]]] = {}
        for pad in footprint.pads:
            if pad.number in pin_numbers and pad.type != "np_thru_hole":
                pad_positions.setdefault(pad.number, []).append((pad.x, pad.y))
        pad_centers = {
            number: (
                sum(point[0] for point in points) / len(points),
                sum(point[1] for point in points) / len(points),
            )
            for number, points in pad_positions.items()
        }
        for issue in pinout_oracle.compare_orientation(drawing_positions, pad_centers):
            _finding(
                findings,
                f"footprint_{issue.code}",
                "error",
                "footprint",
                issue.message,
            )

    if symbol is None:
        return
    symbol_names: dict[str, list[str]] = {}
    for pin in symbol.pins:
        if pin.number in pin_numbers:
            symbol_names.setdefault(pin.number, []).append(pin.name)
    drawing_names: dict[str, str] = {}
    for label in geometry.labels:
        if label.name is not None:
            drawing_names[label.number] = label.name
    actual_names: dict[str, str] = {
        number: sorted(names)[0] if names else "" for number, names in symbol_names.items()
    }
    mismatches: list[str] = []
    for number in sorted(drawing_names, key=lambda value: int(value)):
        expected = drawing_names[number]
        actual = symbol_names.get(number, [])
        if not actual or any(not pinout_oracle.names_equal(expected, name) for name in actual):
            mismatches.append(number)
            _finding(
                findings,
                "symbol_pinout_name_mismatch",
                "error",
                "symbol",
                (
                    f"symbol pin {number} names {sorted(actual) or ['missing']} "
                    f"differ from pinout name {expected}"
                ),
            )
    if mismatches:
        hypotheses = pinout_oracle.diagnose_permutation(
            drawing_positions,
            drawing_names,
            actual_names,
        )
        _finding(
            findings,
            "symbol_permutation_diagnosis",
            "info",
            "symbol",
            f"symbol pinout permutation hypotheses: {', '.join(hypotheses) or 'none'}",
        )


def _pad_polygon(pad: PadDef) -> list[tuple[float, float]]:
    angle = math.radians(pad.rotation)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    corners = [
        (-pad.width / 2, -pad.height / 2),
        (pad.width / 2, -pad.height / 2),
        (pad.width / 2, pad.height / 2),
        (-pad.width / 2, pad.height / 2),
    ]
    return [(pad.x + x * cosine - y * sine, pad.y + x * sine + y * cosine) for x, y in corners]


def _polygon_box(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _pad_boxes(pads: list[PadDef]) -> dict[str, tuple[float, float, float, float]]:
    groups: dict[str, list[tuple[float, float, float, float]]] = {}
    for pad in pads:
        if pad.number:
            groups.setdefault(pad.number, []).append(_polygon_box(_pad_polygon(pad)))
    return {
        number: (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
        for number, boxes in groups.items()
    }


def _reference_boxes(
    pads: list[LandPad],
) -> dict[str, tuple[float, float, float, float]]:
    groups: dict[str, list[tuple[float, float, float, float]]] = {}
    for pad in pads:
        groups.setdefault(pad.number, []).append(
            (
                pad.x - pad.width / 2,
                pad.y - pad.height / 2,
                pad.x + pad.width / 2,
                pad.y + pad.height / 2,
            )
        )
    return {
        number: (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
        for number, boxes in groups.items()
    }


def _check_pad_set_and_geometry(
    footprint: FootprintDef,
    reference: LandPatternResult,
    tolerance_mm: float,
    findings: list[VerifyFinding],
) -> dict[str, tuple[float, float, float, float]]:
    expected_numbers = Counter(pad.number for pad in reference.pads)
    actual_numbers = Counter(pad.number for pad in footprint.pads if pad.number)
    if expected_numbers != actual_numbers:
        _finding(
            findings,
            "pad_set",
            "error",
            "footprint",
            f"pad numbers {sorted(actual_numbers.elements())} do not match "
            f"reference {sorted(expected_numbers.elements())}",
        )
    expected_boxes = _reference_boxes(reference.pads)
    actual_boxes = _pad_boxes(footprint.pads)
    center_delta = 0.0
    size_delta = 0.0
    for number in expected_boxes.keys() & actual_boxes.keys():
        ex0, ey0, ex1, ey1 = expected_boxes[number]
        ax0, ay0, ax1, ay1 = actual_boxes[number]
        center_delta = max(
            center_delta,
            abs((ex0 + ex1 - ax0 - ax1) / 2),
            abs((ey0 + ey1 - ay0 - ay1) / 2),
        )
        size_delta = max(
            size_delta,
            abs((ex1 - ex0) - (ax1 - ax0)),
            abs((ey1 - ey0) - (ay1 - ay0)),
        )
    if center_delta > tolerance_mm or size_delta > tolerance_mm:
        _finding(
            findings,
            "pad_geometry",
            "error",
            "footprint",
            f"pad center delta {center_delta:.4f} mm and size delta "
            f"{size_delta:.4f} mm exceed {tolerance_mm:.4f} mm",
        )
    return actual_boxes


def _check_pad_types(
    spec: PartSpec,
    footprint: FootprintDef,
    findings: list[VerifyFinding],
) -> None:
    if spec.package.family in (
        "no_lead_dual",
        "no_lead_quad",
        "gullwing_dual",
        "gullwing_quad",
        "chip",
    ):
        bad = [pad.number for pad in footprint.pads if pad.number and pad.type != "smd"]
        if bad:
            _finding(
                findings,
                "pad_type",
                "error",
                "footprint",
                f"SMD package has non-SMD pads: {sorted(set(bad))}",
            )
    elif spec.package.family == "through_hole_inline":
        bad = [pad.number for pad in footprint.pads if pad.number and pad.type != "thru_hole"]
        if bad:
            _finding(
                findings,
                "pad_type",
                "error",
                "footprint",
                f"through-hole package has non-through-hole pads: {sorted(set(bad))}",
            )


def _graphic_box(graphics: list[GraphicDef]) -> tuple[float, float, float, float] | None:
    if not any(graphic.points for graphic in graphics):
        return None
    return (
        min(point[0] - graphic.width / 2 for graphic in graphics for point in graphic.points),
        min(point[1] - graphic.width / 2 for graphic in graphics for point in graphic.points),
        max(point[0] + graphic.width / 2 for graphic in graphics for point in graphic.points),
        max(point[1] + graphic.width / 2 for graphic in graphics for point in graphic.points),
    )


def _dimension_value(dimension: Dimension, *, upper: bool = False) -> float | None:
    if upper:
        value = dimension.max if dimension.max is not None else dimension.nom
    else:
        value = dimension.nom
    if value is not None:
        return float(value)
    minimum = dimension.min
    maximum = dimension.max
    if minimum is not None and maximum is not None:
        return (float(minimum) + float(maximum)) / 2
    fallback = maximum if upper else minimum
    if fallback is None:
        fallback = minimum if upper else maximum
    return float(fallback) if fallback is not None else None


def _body_box(spec: PartSpec) -> tuple[float, float, float, float] | None:
    body_width = _dimension_value(spec.package.body_width)
    body_length = _dimension_value(spec.package.body_length)
    if body_width is None or body_length is None:
        return None
    return (-body_width / 2, -body_length / 2, body_width / 2, body_length / 2)


def _contains(
    outer: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    tolerance: float = 0.0,
) -> bool:
    return (
        outer[0] <= inner[0] + tolerance
        and outer[1] <= inner[1] + tolerance
        and outer[2] >= inner[2] - tolerance
        and outer[3] >= inner[3] - tolerance
    )


def _check_courtyard_and_fab(
    spec: PartSpec,
    footprint: FootprintDef,
    findings: list[VerifyFinding],
) -> None:
    courtyard_graphics = [
        graphic for graphic in footprint.graphics if graphic.layer in ("F.CrtYd", "B.CrtYd")
    ]
    if not courtyard_graphics:
        _finding(
            findings,
            "courtyard_missing",
            "error",
            "footprint",
            "footprint has no F.CrtYd or B.CrtYd graphics",
        )
    courtyard_box = _graphic_box(courtyard_graphics)
    body = _body_box(spec)
    pad_boxes = list(_pad_boxes(footprint.pads).values())
    if courtyard_box is not None:
        enclosed = all(_contains(courtyard_box, box, 1e-6) for box in pad_boxes)
        if body is not None:
            enclosed = enclosed and _contains(courtyard_box, body, 1e-6)
        if not enclosed:
            _finding(
                findings,
                "courtyard_enclosure",
                "error",
                "footprint",
                "courtyard does not enclose all pads and the package body",
            )
    else:
        _finding(
            findings,
            "courtyard_enclosure",
            "error",
            "footprint",
            "courtyard has no usable geometry",
        )
    fab_graphics = [graphic for graphic in footprint.graphics if graphic.layer == "F.Fab"]
    fab_box = _graphic_box(fab_graphics)
    if (
        body is None
        or fab_box is None
        or any(
            abs(actual - expected) > 0.05 for actual, expected in zip(fab_box, body, strict=True)
        )
    ):
        _finding(
            findings,
            "fab_outline",
            "warning",
            "footprint",
            "F.Fab outline is missing or differs from the nominal body by more than 0.05 mm",
        )


def _cross(
    first: tuple[float, float],
    second: tuple[float, float],
    third: tuple[float, float],
) -> float:
    return (second[0] - first[0]) * (third[1] - first[1]) - (second[1] - first[1]) * (
        third[0] - first[0]
    )


def _on_segment(
    start: tuple[float, float],
    end: tuple[float, float],
    point: tuple[float, float],
) -> bool:
    return (
        min(start[0], end[0]) - 1e-9 <= point[0] <= max(start[0], end[0]) + 1e-9
        and min(start[1], end[1]) - 1e-9 <= point[1] <= max(start[1], end[1]) + 1e-9
        and abs(_cross(start, end, point)) <= 1e-9
    )


def _segments_intersect(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    ab_c = _cross(a, b, c)
    ab_d = _cross(a, b, d)
    cd_a = _cross(c, d, a)
    cd_b = _cross(c, d, b)
    if ab_c * ab_d < 0 and cd_a * cd_b < 0:
        return True
    return (
        (abs(ab_c) <= 1e-9 and _on_segment(a, b, c))
        or (abs(ab_d) <= 1e-9 and _on_segment(a, b, d))
        or (abs(cd_a) <= 1e-9 and _on_segment(c, d, a))
        or (abs(cd_b) <= 1e-9 and _on_segment(c, d, b))
    )


def _point_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.dist(point, start)
    fraction = max(
        0.0,
        min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq),
    )
    closest = (start[0] + fraction * dx, start[1] + fraction * dy)
    return math.dist(point, closest)


def _polygon_edges(
    polygon: list[tuple[float, float]],
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return list(zip(polygon, polygon[1:] + polygon[:1], strict=True))


def _point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    inside = False
    x, y = point
    for (x0, y0), (x1, y1) in _polygon_edges(polygon):
        if _on_segment((x0, y0), (x1, y1), point):
            return True
        if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


def _polygon_distance(
    first: list[tuple[float, float]],
    second: list[tuple[float, float]],
) -> float:
    first_edges = _polygon_edges(first)
    second_edges = _polygon_edges(second)
    if any(_segments_intersect(a, b, c, d) for a, b in first_edges for c, d in second_edges):
        return 0.0
    if _point_in_polygon(first[0], second) or _point_in_polygon(second[0], first):
        return 0.0
    distances = [
        _point_segment_distance(point, start, end) for point in first for start, end in second_edges
    ]
    distances.extend(
        _point_segment_distance(point, start, end) for point in second for start, end in first_edges
    )
    return min(distances, default=math.inf)


def _graphic_segments(
    graphic: GraphicDef,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    points = graphic.points
    if len(points) < 2:
        return []
    if graphic.kind == "line":
        return [(points[0], points[1])]
    if graphic.kind == "rect":
        x0, y0 = points[0]
        x1, y1 = points[-1]
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return _polygon_edges(corners)
    if graphic.kind == "circle":
        center, edge = points[0], points[-1]
        radius = math.dist(center, edge)
        circle = [
            (
                center[0] + math.cos(index * math.tau / 32) * radius,
                center[1] + math.sin(index * math.tau / 32) * radius,
            )
            for index in range(32)
        ]
        return _polygon_edges(circle)
    if graphic.kind == "poly":
        return _polygon_edges(points)
    return list(pairwise(points))


def _check_silk_clearance(
    footprint: FootprintDef,
    findings: list[VerifyFinding],
) -> None:
    silk = [graphic for graphic in footprint.graphics if graphic.layer == "F.SilkS"]
    copper_pads = [pad for pad in footprint.pads if _is_copper(pad)]
    for graphic in silk:
        segments = _graphic_segments(graphic)
        for pad in copper_pads:
            polygon = _pad_polygon(pad)
            if any(
                _segment_polygon_distance(segment, polygon) <= 0.05 + graphic.width / 2
                for segment in segments
            ):
                _finding(
                    findings,
                    "silk_over_pad",
                    "error",
                    f"pad.{pad.number}",
                    "F.SilkS graphic is within 0.05 mm of a copper pad",
                )
                return


def _segment_polygon_distance(
    segment: tuple[tuple[float, float], tuple[float, float]],
    polygon: list[tuple[float, float]],
) -> float:
    start, end = segment
    if _point_in_polygon(start, polygon) or _point_in_polygon(end, polygon):
        return 0.0
    edges = _polygon_edges(polygon)
    if any(_segments_intersect(start, end, edge_start, edge_end) for edge_start, edge_end in edges):
        return 0.0
    return min(
        [_point_segment_distance(start, edge_start, edge_end) for edge_start, edge_end in edges]
        + [_point_segment_distance(point, start, end) for point in polygon],
        default=math.inf,
    )


def _is_copper(pad: PadDef) -> bool:
    return any(layer == "*.Cu" or layer.endswith(".Cu") for layer in pad.layers)


def _check_pad_clearance(
    footprint: FootprintDef,
    findings: list[VerifyFinding],
) -> None:
    pads = [pad for pad in footprint.pads if pad.number and _is_copper(pad)]
    for index, first in enumerate(pads):
        for second in pads[index + 1 :]:
            if first.number == second.number:
                continue
            distance = _polygon_distance(_pad_polygon(first), _pad_polygon(second))
            if distance < 0.10:
                _finding(
                    findings,
                    "pad_clearance",
                    "error",
                    f"pads.{first.number},{second.number}",
                    f"copper clearance is {distance:.4f} mm, below 0.10 mm",
                )
            elif distance < 0.15:
                _finding(
                    findings,
                    "pad_clearance",
                    "warning",
                    f"pads.{first.number},{second.number}",
                    f"copper clearance is {distance:.4f} mm, below 0.15 mm",
                )


def _axis_for_lead(spec: PartSpec, rect: Rect) -> Literal["x", "y"]:
    family = spec.package.family
    if family in ("chip", "gullwing_dual", "no_lead_dual"):
        return "x"
    body_width = _dimension_value(spec.package.body_width)
    body_length = _dimension_value(spec.package.body_length)
    center_x = (rect.x0 + rect.x1) / 2
    center_y = (rect.y0 + rect.y1) / 2
    if family == "no_lead_quad" and body_width is not None and body_length is not None:
        x_edge_distance = abs(body_width / 2 - abs(center_x))
        y_edge_distance = abs(body_length / 2 - abs(center_y))
        return "x" if x_edge_distance <= y_edge_distance else "y"
    if body_width is not None and abs(center_x) > body_width / 2:
        return "x"
    return "y"


def _pad_projection(pad: PadDef, axis: Literal["x", "y"]) -> tuple[float, float]:
    polygon = _pad_polygon(pad)
    coordinates = [point[0 if axis == "x" else 1] for point in polygon]
    return min(coordinates), max(coordinates)


def _check_lead_geometry(
    spec: PartSpec,
    footprint: FootprintDef,
    findings: list[VerifyFinding],
) -> None:
    try:
        leads = lead_rects(spec)
    except (ValueError, TypeError) as exc:
        _finding(findings, "lead_outside_pad", "error", "leads", f"could not compute leads: {exc}")
        return
    by_number: dict[str, list[PadDef]] = {}
    for pad in footprint.pads:
        if pad.number and pad.type != "np_thru_hole":
            by_number.setdefault(pad.number, []).append(pad)
    for number, rects in leads.items():
        pads = by_number.get(number, [])
        for index, lead in enumerate(rects):
            subject = f"lead.{number}[{index}]"
            if not pads:
                _finding(
                    findings,
                    "lead_outside_pad",
                    "error",
                    subject,
                    f"no footprint pad shares lead number {number}",
                )
                continue
            center = ((lead.x0 + lead.x1) / 2, (lead.y0 + lead.y1) / 2)
            if not any(_point_in_polygon(center, _pad_polygon(pad)) for pad in pads):
                _finding(
                    findings,
                    "lead_outside_pad",
                    "error",
                    subject,
                    "lead center is outside every same-number copper pad",
                )
            axis = _axis_for_lead(spec, lead)
            lead_span = (lead.x0, lead.x1) if axis == "x" else (lead.y0, lead.y1)
            same_number_span = (
                min(_pad_projection(pad, axis)[0] for pad in pads),
                max(_pad_projection(pad, axis)[1] for pad in pads),
            )
            if (
                lead_span[0] < same_number_span[0] - 0.005
                or lead_span[1] > same_number_span[1] + 0.005
            ):
                _finding(
                    findings,
                    "lead_outside_pad",
                    "error",
                    subject,
                    "lead toe-to-heel extent is not contained in its copper pad",
                )
            across_axis: Literal["x", "y"] = "y" if axis == "x" else "x"
            lead_width = lead.y1 - lead.y0 if across_axis == "y" else lead.x1 - lead.x0
            nearest_pad = min(
                pads,
                key=lambda pad: math.dist(
                    (pad.x, pad.y),
                    center,
                ),
            )
            pad_width = _pad_projection(nearest_pad, across_axis)
            if lead_width > pad_width[1] - pad_width[0] + 0.1:
                _finding(
                    findings,
                    "lead_width_exceeds_pad",
                    "warning",
                    subject,
                    f"lead width {lead_width:.4f} mm exceeds pad width "
                    f"{pad_width[1] - pad_width[0]:.4f} mm by more than 0.1 mm",
                )


def _check_exposed_pad_size(
    spec: PartSpec,
    footprint: FootprintDef,
    findings: list[VerifyFinding],
) -> None:
    exposed = spec.package.exposed_pad
    if exposed is None:
        return
    boxes = _pad_boxes([pad for pad in footprint.pads if pad.number == exposed.number])
    box = boxes.get(exposed.number)
    if box is None:
        return
    actual_width, actual_height = box[2] - box[0], box[3] - box[1]
    nominal_width = _dimension_value(exposed.width)
    nominal_height = _dimension_value(exposed.length)
    max_width = _dimension_value(exposed.width, upper=True)
    max_height = _dimension_value(exposed.length, upper=True)
    if None in (nominal_width, nominal_height, max_width, max_height):
        return
    assert nominal_width is not None and nominal_height is not None
    assert max_width is not None and max_height is not None
    direct_ok = (
        actual_width >= nominal_width * 0.5
        and actual_height >= nominal_height * 0.5
        and actual_width <= max_width + 0.3
        and actual_height <= max_height + 0.3
    )
    swapped_ok = (
        actual_width >= nominal_height * 0.5
        and actual_height >= nominal_width * 0.5
        and actual_width <= max_height + 0.3
        and actual_height <= max_width + 0.3
    )
    if not direct_ok and not swapped_ok:
        _finding(
            findings,
            "exposed_pad_size",
            "warning",
            f"pad.{exposed.number}",
            "exposed copper pad is below half the package EP nominal or exceeds "
            "its maximum by more than 0.3 mm",
        )


def _check_pin1_location(
    spec: PartSpec,
    footprint: FootprintDef,
    findings: list[VerifyFinding],
) -> None:
    pad = next(
        (
            candidate
            for candidate in footprint.pads
            if candidate.number == "1" and candidate.type != "np_thru_hole"
        ),
        None,
    )
    if pad is None:
        _finding(findings, "pin1_location", "error", "footprint", "footprint has no pad 1")
        return
    if spec.package.family == "chip":
        valid = pad.x < 0
    elif spec.package.family in (
        "no_lead_dual",
        "no_lead_quad",
        "gullwing_dual",
        "gullwing_quad",
    ):
        valid = pad.x < 0 and pad.y < 0
    else:
        return
    if not valid:
        _finding(
            findings,
            "pin1_location",
            "error",
            "footprint.pad.1",
            "pad 1 is not at the expected top-left / negative-x location",
        )


def _check_models(
    footprint: FootprintDef | None,
    footprint_path: Path,
    library_dir: Path | None,
    model_required: bool,
    findings: list[VerifyFinding],
) -> list[VerifiedModel]:
    if footprint is None:
        return []
    if model_required and not footprint.models:
        _finding(findings, "model_missing", "error", "footprint", "footprint has no 3D model")
    result: list[VerifiedModel] = []
    for model in footprint.models:
        path = model.path
        expansion_root = library_dir.parent if library_dir is not None else None
        value = path.replace("${KIPRJMOD}", str(expansion_root or ""))
        value = value.replace("$KIPRJMOD", str(expansion_root or ""))
        expanded = os.path.expandvars(value)
        resolved: Path | None = None
        if "$" not in expanded and "${" not in expanded:
            candidate = Path(expanded)
            if not candidate.is_absolute():
                candidate = footprint_path.parent / candidate
            try:
                actual = candidate.resolve(strict=True)
            except OSError:
                actual = None
            if actual is not None and actual.is_file():
                resolved = actual
        if resolved is None:
            _finding(
                findings,
                "model_unresolved",
                "error",
                f"model.{path}",
                f"3D model reference cannot be resolved: {path}",
            )
        result.append(
            VerifiedModel(
                path=path,
                resolved=resolved is not None,
                sha256=_sha256(resolved) if resolved is not None else None,
            )
        )
    return result


def _verify_cli(
    symbol_lib: Path,
    symbol_name: str,
    footprint_path: Path,
    footprint_name: str,
    findings: list[VerifyFinding],
) -> None:
    if not symbol_lib.is_file() or not footprint_path.is_file():
        _finding(
            findings,
            "kicad_parse",
            "error",
            "library",
            "symbol library or footprint file is missing",
        )
        return
    with tempfile.TemporaryDirectory(prefix="circuit-library-verify-") as temporary_name:
        work = Path(temporary_name)
        symbol_copy = work / symbol_lib.name
        shutil.copyfile(symbol_lib, symbol_copy)
        symbol_output = work / "sym-upgrade"
        footprint_source = work / f"{footprint_name}.pretty"
        footprint_source.mkdir()
        shutil.copyfile(footprint_path, footprint_source / footprint_path.name)
        footprint_output = work / "fp-upgrade"
        calls = [
            (
                ["sym", "upgrade", str(symbol_copy), "--output", str(symbol_output)],
                "symbol",
            ),
            (
                ["fp", "upgrade", str(footprint_source), "--output", str(footprint_output)],
                "footprint",
            ),
        ]
        for arguments, subject in calls:
            try:
                result = kicad_cli.run(arguments)
            except kicad_cli.KicadCliError as exc:
                message = str(exc)
                unavailable = any(
                    marker in message.casefold()
                    for marker in ("errno 2", "no such file", "not found")
                )
                _finding(
                    findings,
                    "kicad_cli_unavailable" if unavailable else "kicad_parse",
                    "error",
                    subject,
                    message,
                )
                return
            if result.returncode:
                details = result.stderr.strip() or result.stdout.strip()
                unavailable = any(
                    marker in details.casefold()
                    for marker in ("no such file", "not found", "cannot find")
                )
                _finding(
                    findings,
                    "kicad_cli_unavailable" if unavailable else "kicad_parse",
                    "error",
                    subject,
                    details or f"kicad-cli could not upgrade the {subject}",
                )
                return


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _check_provenance_item(
    library_dir: Path,
    artifact_path: Path,
    *,
    artifact: Literal["symbol", "footprint", "model3d"],
    name: str,
    digest: str | None,
    findings: list[VerifyFinding],
) -> None:
    if not _inside(artifact_path, library_dir):
        return
    relative = artifact_path.resolve().relative_to(library_dir.resolve()).as_posix()
    try:
        provenance = load_library_provenance(library_dir)
    except LibrarySourceError as exc:
        _finding(findings, "provenance_missing", "error", relative, str(exc))
        return
    entry = next(
        (
            candidate
            for candidate in provenance.entries
            if candidate.path == relative
            and candidate.artifact == artifact
            and candidate.name == name
        ),
        None,
    )
    if entry is None:
        _finding(
            findings,
            "provenance_missing",
            "error",
            relative,
            "library artifact has no matching provenance entry",
        )
        return
    if digest is None or entry.sha256 != digest:
        _finding(
            findings,
            "provenance_sha_mismatch",
            "error",
            relative,
            "provenance SHA-256 differs from the current artifact",
        )
    if entry.source.origin == "third_party" or entry.source.license.redistribution != "allowed":
        _finding(
            findings,
            "redistribution_review",
            "warning",
            relative,
            "third-party source or restricted redistribution requires review",
        )


def _write_report(report: LibraryVerification, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    path.write_text(content + "\n", encoding="utf-8")


def verify_library_part(
    spec: PartSpec,
    *,
    spec_path: Path,
    spec_check_path: Path | None = None,
    symbol_lib: Path,
    symbol_name: str,
    footprint_path: Path,
    library_dir: Path | None,
    reference: LandPatternResult,
    tolerance_mm: float = 0.02,
    model_required: bool = True,
    output_path: Path | None = None,
) -> LibraryVerification:
    """Verify a library part against its current PartSpec and generated land pattern."""

    if not math.isfinite(tolerance_mm) or tolerance_mm < 0:
        raise ValueError("tolerance_mm must be finite and non-negative")
    findings: list[VerifyFinding] = []
    spec_hash = part_spec_sha256(spec_path) if spec_path.is_file() else ""
    check: PartSpecReport | None = None
    try:
        if spec_check_path is not None:
            check = PartSpecReport.model_validate_json(spec_check_path.read_text(encoding="utf-8"))
        else:
            extraction_path = Path(spec.datasheet.extraction_path)
            if not extraction_path.is_absolute():
                extraction_path = spec_path.resolve().parent / extraction_path
            extraction = load_extraction(extraction_path)
            check = check_part_spec(
                spec,
                extraction,
                spec_path=spec_path,
                extraction_path=extraction_path,
            )
        check_ok = check.verdict == "pass" and check.part_spec_sha256 == spec_hash
        check_detail = "" if check_ok else "fresh PartSpec check failed or is stale"
    except Exception as exc:
        check_ok = False
        check_detail = str(exc)
    if not check_ok:
        _finding(
            findings,
            "part_spec_unchecked",
            "error",
            "part_spec",
            check_detail or "PartSpec check is missing, failed, or stale",
        )

    try:
        symbol = parse_symbol(symbol_lib, symbol_name)
    except (LibItemError, OSError):
        symbol = None
    try:
        footprint = parse_footprint(footprint_path)
    except (LibItemError, OSError):
        footprint = None
    footprint_name = footprint.name if footprint is not None else footprint_path.stem
    _check_symbol(spec, symbol, symbol_lib, footprint_name, library_dir, findings)
    _check_pinout_geometry(spec, check, symbol, footprint, findings)

    if footprint is None:
        _finding(findings, "pad_set", "error", "footprint", "footprint could not be parsed")
        _finding(
            findings,
            "courtyard_missing",
            "error",
            "footprint",
            "footprint graphics cannot be checked",
        )
    else:
        _check_pad_set_and_geometry(
            footprint,
            reference,
            tolerance_mm,
            findings,
        )
        _check_pad_types(spec, footprint, findings)
        _check_pin1_location(spec, footprint, findings)
        _check_courtyard_and_fab(spec, footprint, findings)
        _check_silk_clearance(footprint, findings)
        _check_pad_clearance(footprint, findings)
        _check_lead_geometry(spec, footprint, findings)
        _check_exposed_pad_size(spec, footprint, findings)
        if (
            spec.package.family
            in (
                "no_lead_dual",
                "no_lead_quad",
                "gullwing_dual",
                "gullwing_quad",
                "chip",
            )
            and "smd" not in footprint.attributes
        ):
            _finding(
                findings,
                "fp_attribute",
                "warning",
                "footprint",
                "SMD footprint is missing the smd attribute",
            )

    symbol_numbers: set[str] = {pin.number for pin in symbol.pins} if symbol is not None else set()
    pad_numbers: set[str] = (
        {pad.number for pad in footprint.pads if pad.number and pad.type != "np_thru_hole"}
        if footprint is not None
        else set()
    )
    if not symbol_numbers.issubset(pad_numbers) or not pad_numbers.issubset(symbol_numbers):
        _finding(
            findings,
            "pin_pad_mapping",
            "error",
            "library",
            "symbol pins and numbered copper pads do not map in both directions",
        )

    _verify_cli(symbol_lib, symbol_name, footprint_path, footprint_name, findings)
    verified_models = _check_models(
        footprint,
        footprint_path,
        library_dir,
        model_required,
        findings,
    )

    if library_dir is not None:
        _check_provenance_item(
            library_dir,
            symbol_lib,
            artifact="symbol",
            name=symbol_name,
            digest=_sha256(symbol_lib),
            findings=findings,
        )
        _check_provenance_item(
            library_dir,
            footprint_path,
            artifact="footprint",
            name=footprint_name,
            digest=_sha256(footprint_path),
            findings=findings,
        )
        if footprint is not None:
            for model, verified in zip(footprint.models, verified_models, strict=True):
                expanded = os.path.expandvars(model.path)
                if "${KIPRJMOD}" in expanded or "$KIPRJMOD" in expanded:
                    expanded = expanded.replace("${KIPRJMOD}", str(library_dir.parent)).replace(
                        "$KIPRJMOD", str(library_dir.parent)
                    )
                candidate = Path(expanded)
                if not candidate.is_absolute():
                    candidate = footprint_path.parent / candidate
                try:
                    resolved_model = candidate.resolve(strict=True)
                except OSError:
                    continue
                _check_provenance_item(
                    library_dir,
                    resolved_model,
                    artifact="model3d",
                    name=resolved_model.name,
                    digest=verified.sha256,
                    findings=findings,
                )

    report = LibraryVerification(
        artifact_kind="circuit_library_verification",
        verdict="fail" if any(finding.severity == "error" for finding in findings) else "pass",
        part_spec_sha256=spec_hash,
        inputs=VerificationInputs(
            part_spec_path=Path(
                os.path.relpath(
                    spec_path.resolve(),
                    (library_dir or footprint_path.parent.parent).resolve(),
                )
            ),
            symbol_lib=Path(
                os.path.relpath(
                    symbol_lib.resolve(),
                    (library_dir or footprint_path.parent.parent).resolve(),
                )
            ),
            symbol_name=symbol_name,
            footprint_path=Path(
                os.path.relpath(
                    footprint_path.resolve(),
                    (library_dir or footprint_path.parent.parent).resolve(),
                )
            ),
            density=reference.density,
            tolerance_mm=tolerance_mm,
            model_required=model_required,
        ),
        symbol=VerifiedSymbol(
            lib_path=symbol_lib,
            name=symbol_name,
            sha256=_sha256(symbol_lib),
        ),
        footprint=VerifiedFootprint(
            path=footprint_path,
            name=footprint_name,
            sha256=_sha256(footprint_path),
        ),
        models=verified_models,
        findings=findings,
    )
    target = output_path
    if target is None:
        base = library_dir if library_dir is not None else footprint_path.parent
        target = base / "verification" / f"{footprint_name}.verification.json"
    try:
        assert_safe_destination(target.parent)
        _write_report(report, target)
    except (LibrarySourceError, OSError) as exc:
        _finding(
            report.findings,
            "verification_output_unavailable",
            "error",
            str(target),
            str(exc),
        )
        report.verdict = "fail"
    return report
