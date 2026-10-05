"""Connector mating-envelope and board-edge placement checks."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import humanrequest, occt, sexpr
from .partspec import ConnectorMatingEnvelope, Dimension, PartSpec, load_part_spec

Point = tuple[float, float]
Segment = tuple[Point, Point]


def _empty_human_requests() -> list[humanrequest.HumanRequest]:
    return []


class ConnectorPlacementFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["error", "warning"]
    ref: str
    message: str
    obstructing_ref: str | None = None


class ConnectorPlacementReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_connector_placement_report"] = (
        "circuit_connector_placement_report"
    )
    verdict: Literal["pass", "fail"]
    input_hashes: dict[str, str]
    findings: list[ConnectorPlacementFinding]
    human_requests: list[humanrequest.HumanRequest] = Field(default_factory=_empty_human_requests)


@dataclass(frozen=True)
class _BoardFootprint:
    ref: str
    lib_id: str
    x: float
    y: float
    rotation: float
    side: Literal["F.Cu", "B.Cu"]
    scale: Point
    courtyard: tuple[Point, ...]
    edge_lines: tuple[Segment, ...]
    properties: dict[str, str]
    models: tuple[str, ...]


def _children(node: list[sexpr.SExpr], name: str) -> list[list[sexpr.SExpr]]:
    return [item for item in node[1:] if isinstance(item, list) and item and item[0] == name]


def _first(node: list[sexpr.SExpr], name: str) -> list[sexpr.SExpr] | None:
    return next(iter(_children(node, name)), None)


def _atom(node: list[sexpr.SExpr], index: int, label: str) -> str:
    if index >= len(node) or not isinstance(node[index], str):
        raise ValueError(f"missing {label}")
    return str(node[index])


def _number(value: sexpr.SExpr, label: str) -> float:
    if not isinstance(value, str):
        raise ValueError(f"missing {label}")
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"invalid {label}") from exc
    if not math.isfinite(number):
        raise ValueError(f"invalid {label}")
    return number


def _point(node: list[sexpr.SExpr] | None, label: str) -> Point:
    if node is None or len(node) < 3:
        raise ValueError(f"missing {label}")
    return (_number(node[1], f"{label}.x"), _number(node[2], f"{label}.y"))


def _dimension_value(dimension: Dimension) -> float:
    if dimension.nom is not None:
        return dimension.nom
    if dimension.min is not None and dimension.max is not None:
        return (dimension.min + dimension.max) / 2
    if dimension.min is not None:
        return dimension.min
    if dimension.max is not None:
        return dimension.max
    raise ValueError("connector placement dimension has no usable value")


def _maximum_dimension_value(dimension: Dimension) -> float:
    if dimension.max is not None:
        return dimension.max
    if dimension.nom is not None:
        return dimension.nom
    if dimension.min is not None:
        return dimension.min
    raise ValueError("connector placement dimension has no usable value")


def _layer(node: list[sexpr.SExpr]) -> str:
    value = _first(node, "layer")
    return _atom(value, 1, "layer") if value is not None else ""


def _points_node(node: list[sexpr.SExpr]) -> list[Point]:
    pts = _first(node, "pts")
    if pts is None:
        return []
    return [_point(point, "polygon point") for point in _children(pts, "xy")]


def _arc_segments(node: list[sexpr.SExpr]) -> list[Segment]:
    start = _point(_first(node, "start"), "arc start")
    middle = _point(_first(node, "mid"), "arc middle")
    end = _point(_first(node, "end"), "arc end")
    ax, ay = start
    bx, by = middle
    cx, cy = end
    determinant = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(determinant) < 1e-9:
        return [(start, end)]
    aa, bb, cc = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    center = (
        (aa * (by - cy) + bb * (cy - ay) + cc * (ay - by)) / determinant,
        (aa * (cx - bx) + bb * (ax - cx) + cc * (bx - ax)) / determinant,
    )
    radius = math.dist(center, start)
    angles = [
        math.atan2(point[1] - center[1], point[0] - center[0]) for point in (start, middle, end)
    ]
    ccw_span = (angles[2] - angles[0]) % (2 * math.pi)
    middle_span = (angles[1] - angles[0]) % (2 * math.pi)
    sweep = ccw_span if middle_span <= ccw_span else ccw_span - 2 * math.pi
    count = max(4, math.ceil(abs(sweep) / (math.pi / 24)))
    curve = [
        (
            center[0] + radius * math.cos(angles[0] + sweep * index / count),
            center[1] + radius * math.sin(angles[0] + sweep * index / count),
        )
        for index in range(count + 1)
    ]
    return list(pairwise(curve))


def _outline(root: list[sexpr.SExpr]) -> tuple[list[Segment], list[Point]]:
    segments: list[Segment] = []
    polygons: list[list[Point]] = []
    for node in root[1:]:
        if not isinstance(node, list) or not node:
            continue
        kind = node[0]
        if kind not in {"gr_line", "gr_arc", "gr_poly"} or _layer(node) != "Edge.Cuts":
            continue
        if kind == "gr_line":
            segments.append(
                (
                    _point(_first(node, "start"), "Edge.Cuts start"),
                    _point(_first(node, "end"), "Edge.Cuts end"),
                )
            )
        elif kind == "gr_arc":
            segments.extend(_arc_segments(node))
        else:
            points = _points_node(node)
            if len(points) >= 3:
                polygons.append(points)
                segments.extend(zip(points, [*points[1:], points[0]], strict=True))
    if polygons:
        return segments, max(
            polygons,
            key=lambda points: abs(
                sum(
                    first[0] * second[1] - second[0] * first[1]
                    for first, second in zip(
                        points,
                        [*points[1:], points[0]],
                        strict=True,
                    )
                )
            ),
        )
    if not segments:
        return [], []
    all_segments = list(segments)
    first = segments[0]
    chain = [first[0], first[1]]
    remaining = segments[1:]
    while remaining:
        endpoint = chain[-1]
        nearest_index = min(
            range(len(remaining)),
            key=lambda index: min(
                math.dist(endpoint, remaining[index][0]),
                math.dist(endpoint, remaining[index][1]),
            ),
        )
        first, second = remaining.pop(nearest_index)
        if math.dist(endpoint, first) <= math.dist(endpoint, second):
            chain.append(second)
        else:
            chain.append(first)
    if len(chain) >= 4 and math.dist(chain[0], chain[-1]) <= 0.1:
        return all_segments, chain
    return all_segments, []


def _board_thickness(root: list[sexpr.SExpr]) -> float | None:
    general = _first(root, "general")
    thickness = _first(general, "thickness") if general is not None else None
    if thickness is not None and len(thickness) > 1:
        return _number(thickness[1], "board thickness")
    setup = _first(root, "setup")
    stackup = _first(setup, "stackup") if setup is not None else None
    if stackup is None:
        return None
    values: list[float] = []
    for layer in _children(stackup, "layer"):
        layer_thickness = _first(layer, "thickness")
        if layer_thickness is not None and len(layer_thickness) > 1:
            values.append(_number(layer_thickness[1], "stackup layer thickness"))
    return sum(values) if values else None


def _parse_board_footprint(node: list[sexpr.SExpr]) -> _BoardFootprint:
    transform = _first(node, "transform")
    if transform is None:
        position = _first(node, "at")
        if position is None:
            raise ValueError("board footprint has no placement")
        x = _number(position[1], "footprint x")
        y = _number(position[2], "footprint y")
        rotation = _number(position[3], "footprint rotation") if len(position) > 3 else 0.0
        scale = (1.0, 1.0)
    else:
        translation = _first(transform, "translate")
        angle = _first(transform, "rotate")
        scale_node = _first(transform, "scale")
        if translation is None:
            raise ValueError("board footprint transform has no translation")
        x, y = _point(translation, "footprint translation")
        rotation = _number(angle[1], "footprint rotation") if angle is not None else 0.0
        scale = (
            (
                _number(scale_node[1], "footprint x scale"),
                _number(scale_node[2], "footprint y scale"),
            )
            if scale_node is not None
            else (1.0, 1.0)
        )
    layer = _layer(node)
    side: Literal["F.Cu", "B.Cu"] = "B.Cu" if layer.startswith("B.") else "F.Cu"
    ref = ""
    for prop in _children(node, "property"):
        if len(prop) > 2 and prop[1] == "Reference" and isinstance(prop[2], str):
            ref = prop[2]
            break
    if not ref:
        for item in _children(node, "fp_text"):
            if len(item) > 2 and item[1] == "reference" and isinstance(item[2], str):
                ref = item[2]
                break
    if not ref:
        raise ValueError("board footprint has no reference")
    courtyard_points: list[Point] = []
    edge_lines: list[Segment] = []
    properties: dict[str, str] = {}
    models: list[str] = []
    for item in node[1:]:
        if not isinstance(item, list) or not item:
            continue
        if item[0] == "property" and len(item) > 2:
            if isinstance(item[1], str) and isinstance(item[2], str):
                properties[item[1]] = item[2]
            continue
        if item[0] == "model" and len(item) > 1 and isinstance(item[1], str):
            models.append(item[1])
            continue
        item_layer = _layer(item)
        if item_layer == ("B.CrtYd" if side == "B.Cu" else "F.CrtYd"):
            start_node, end_node = _first(item, "start"), _first(item, "end")
            if start_node is not None and end_node is not None:
                start, end = (
                    _point(start_node, "courtyard start"),
                    _point(end_node, "courtyard end"),
                )
                courtyard_points.extend(
                    [(start[0], start[1]), (end[0], end[1]), (start[0], end[1]), (end[0], start[1])]
                )
            courtyard_points.extend(_points_node(item))
            if item[0] == "fp_line":
                courtyard_points.extend(
                    [
                        _point(_first(item, "start"), "courtyard line start"),
                        _point(_first(item, "end"), "courtyard line end"),
                    ]
                )
            elif item[0] == "fp_arc":
                courtyard_points.extend(
                    [point for segment in _arc_segments(item) for point in segment]
                )
        elif item_layer == "Dwgs.User" and item[0] == "fp_line":
            edge_lines.append(
                (
                    _point(_first(item, "start"), "board-edge start"),
                    _point(_first(item, "end"), "board-edge end"),
                )
            )
    return _BoardFootprint(
        ref=ref,
        lib_id=_atom(node, 1, "footprint library ID"),
        x=x,
        y=y,
        rotation=rotation,
        side=side,
        scale=scale,
        courtyard=tuple(courtyard_points),
        edge_lines=tuple(edge_lines),
        properties=properties,
        models=tuple(models),
    )


def _transform(point: Point, footprint: _BoardFootprint) -> Point:
    local_x = point[0] * footprint.scale[0]
    local_y = point[1] * footprint.scale[1]
    angle = math.radians(footprint.rotation)
    return (
        footprint.x + local_x * math.cos(angle) + local_y * math.sin(angle),
        footprint.y - local_x * math.sin(angle) + local_y * math.cos(angle),
    )


def _transform_vector(vector: Point, footprint: _BoardFootprint) -> Point:
    local_x = vector[0] * footprint.scale[0]
    local_y = vector[1] * footprint.scale[1]
    angle = math.radians(footprint.rotation)
    return (
        local_x * math.cos(angle) + local_y * math.sin(angle),
        -local_x * math.sin(angle) + local_y * math.cos(angle),
    )


def _mirror_library_local(point: Point, footprint: _BoardFootprint) -> Point:
    if footprint.side == "B.Cu":
        return point[0], -point[1]
    return point


def _transform_library_point(point: Point, footprint: _BoardFootprint) -> Point:
    return _transform(_mirror_library_local(point, footprint), footprint)


def _transform_library_vector(vector: Point, footprint: _BoardFootprint) -> Point:
    return _transform_vector(_mirror_library_local(vector, footprint), footprint)


def _mirror_library_axis(axis: str, footprint: _BoardFootprint) -> str:
    if footprint.side == "B.Cu":
        if axis == "+y":
            return "-y"
        if axis == "-y":
            return "+y"
    return axis


def _bbox(points: tuple[Point, ...] | list[Point]) -> tuple[float, float, float, float] | None:
    if not points:
        return None
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _intersects(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return (
        first[0] < second[2] - 1e-6
        and second[0] < first[2] - 1e-6
        and first[1] < second[3] - 1e-6
        and second[1] < first[3] - 1e-6
    )


def _inside(point: Point, polygon: list[Point]) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        if (current[1] > point[1]) != (previous[1] > point[1]):
            crossing = (previous[0] - current[0]) * (point[1] - current[1]) / (
                previous[1] - current[1]
            ) + current[0]
            if point[0] < crossing:
                inside = not inside
        previous = current
    return inside


def _point_segment_distance(point: Point, segment: Segment) -> float:
    start, end = segment
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        return math.dist(point, start)
    factor = max(
        0.0,
        min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_squared),
    )
    return math.dist(point, (start[0] + factor * dx, start[1] + factor * dy))


def _segments_intersect(first: Segment, second: Segment) -> bool:
    def orientation(a: Point, b: Point, c: Point) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(a: Point, b: Point, point: Point) -> bool:
        return (
            min(a[0], b[0]) - 1e-9 <= point[0] <= max(a[0], b[0]) + 1e-9
            and min(a[1], b[1]) - 1e-9 <= point[1] <= max(a[1], b[1]) + 1e-9
        )

    a, b = first
    c, d = second
    first_c, first_d = orientation(a, b, c), orientation(a, b, d)
    second_a, second_b = orientation(c, d, a), orientation(c, d, b)
    if first_c * first_d < -1e-9 and second_a * second_b < -1e-9:
        return True
    return (
        (abs(first_c) <= 1e-9 and on_segment(a, b, c))
        or (abs(first_d) <= 1e-9 and on_segment(a, b, d))
        or (abs(second_a) <= 1e-9 and on_segment(c, d, a))
        or (abs(second_b) <= 1e-9 and on_segment(c, d, b))
    )


def _outline_intersects_box(
    box: tuple[float, float, float, float],
    polygon: list[Point],
) -> bool:
    x0, y0, x1, y1 = box
    corners = [(x0, y0), (x0, y1), (x1, y0), (x1, y1)]
    if any(_inside(point, polygon) for point in corners):
        return True
    if any(x0 < x < x1 and y0 < y < y1 for x, y in polygon):
        return True
    box_edges: list[Segment] = [
        ((x0, y0), (x1, y0)),
        ((x1, y0), (x1, y1)),
        ((x1, y1), (x0, y1)),
        ((x0, y1), (x0, y0)),
    ]
    outline_edges = list(zip(polygon, [*polygon[1:], polygon[0]], strict=True))
    return any(
        _segments_intersect(box_edge, outline_edge)
        for box_edge in box_edges
        for outline_edge in outline_edges
    )


def _neighbor_height(
    footprint: _BoardFootprint,
    spec: PartSpec | None,
    board_path: Path,
) -> float | None:
    verified_heights: list[float] = []
    property_height: float | None = None
    property_value = footprint.properties.get("circuit_height_mm")
    if property_value is not None:
        try:
            height = float(property_value)
        except ValueError:
            height = math.nan
        if math.isfinite(height) and height > 0:
            property_height = height
    if spec is not None:
        try:
            height = _maximum_dimension_value(spec.package.height)
        except ValueError:
            height = math.nan
        if math.isfinite(height) and height > 0:
            verified_heights.append(height)

    for model in footprint.models:
        model_path = model.replace("${KIPRJMOD}", str(board_path.parent))
        resolved = Path(model_path)
        if not resolved.is_absolute():
            resolved = board_path.parent / resolved
        if not resolved.is_file():
            continue
        try:
            facts = occt.inspect(occt.read_step(resolved))
        except Exception:
            continue
        bounds = [solid.bbox for solid in facts.solids]
        if facts.valid and bounds:
            height = max(bbox.z_max for bbox in bounds) - min(bbox.z_min for bbox in bounds)
            if math.isfinite(height) and height > 0:
                verified_heights.append(height)
    if not verified_heights:
        return None
    if property_height is not None:
        verified_heights.append(property_height)
    return max(verified_heights)


def _envelope_box(
    envelope: ConnectorMatingEnvelope,
    footprint: _BoardFootprint,
    spec: PartSpec,
) -> tuple[float, float, float, float]:
    x_min, y_min, x_max, y_max = envelope.box
    local_corners = [
        (x_min, y_min),
        (x_min, y_max),
        (x_max, y_min),
        (x_max, y_max),
    ]
    transformed = [_transform_library_point(point, footprint) for point in local_corners]
    travel = _maximum_dimension_value(envelope.travel) + (envelope.access_margin_mm or 0.0)
    connector = spec.connector
    if connector is None or connector.mating_axis == "+z":
        return _bbox(transformed) or (0.0, 0.0, 0.0, 0.0)
    vectors = {
        "+x": (1.0, 0.0),
        "-x": (-1.0, 0.0),
        "+y": (0.0, 1.0),
        "-y": (0.0, -1.0),
    }
    axis = _transform_library_vector(vectors[connector.mating_axis], footprint)
    swept = [*transformed, *((x + axis[0] * travel, y + axis[1] * travel) for x, y in transformed)]
    return _bbox(swept) or (0.0, 0.0, 0.0, 0.0)


def _human_request(
    *,
    ref: str,
    board_path: Path,
    board_sha: str,
    spec: PartSpec | None,
    code: str,
    reason: str,
) -> humanrequest.HumanRequest:
    return humanrequest.build_request(
        kind="library_review",
        subject={
            "manufacturer": spec.manufacturer if spec is not None else "unknown",
            "mpn": spec.mpn if spec is not None else ref,
            "revision": spec.datasheet.revision if spec is not None else None,
        },
        reason=reason,
        evidence=[
            {
                "kind": "document" if spec is not None else "hash",
                "ref": spec.datasheet.path if spec is not None else str(board_path),
                "sha256": spec.datasheet.sha256 if spec is not None else board_sha,
                "summary": "Connector placement evidence",
            }
        ],
        known=["The board footprint is identified in the placement input."],
        unknown=[reason],
        agent_assessment=(
            "The board identifies a connector footprint, but its mating envelope, insertion "
            "travel, access clearance, or matching connector PartSpec is incomplete. Without "
            "authoritative interface geometry, the transformed mating path cannot be checked "
            "against the board outline or neighboring component courtyards. The placement "
            "must remain unapproved until the manufacturer evidence is reviewed, because "
            "either interference or unreachable travel would silently block assembly."
        ),
        recommendation="Provide the exact connector datasheet and mating envelope",
        recommendation_rationale=(
            "Mating clearance must not be guessed from an undocumented envelope."
        ),
        alternatives=[
            {
                "option": "Provide the exact connector datasheet and mating envelope",
                "risks": ["Placement remains unverified until the envelope is checked."],
            },
            {
                "option": "Provide a manufacturer-approved mating-part drawing",
                "risks": ["The drawing must identify the exact connector variant."],
            },
        ],
        recommended=0,
        details={"kind": "library_review", "finding_codes": [code]},
    )


def check_connector_placement(
    pcb_path: Path,
    part_specs: dict[str, Path],
) -> ConnectorPlacementReport:
    board_sha = hashlib.sha256(pcb_path.read_bytes()).hexdigest()
    parsed = sexpr.parse_text(pcb_path.read_text(encoding="utf-8"))
    segments, polygon = _outline(parsed)
    board_footprints = [_parse_board_footprint(node) for node in _children(parsed, "footprint")]
    specs: dict[str, PartSpec] = {}
    hashes = {"pcb": board_sha}
    findings: list[ConnectorPlacementFinding] = []
    requests: list[humanrequest.HumanRequest] = []
    for ref, path in part_specs.items():
        spec = load_part_spec(path)
        specs[ref] = spec
        hashes[f"part_spec:{ref}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    likely_connectors = [
        footprint
        for footprint in board_footprints
        if footprint.ref in part_specs
        or footprint.ref.upper().startswith(("J", "CN", "USB"))
        or any(
            token in footprint.lib_id.casefold()
            for token in ("connector", "header", "fpc", "jst", "usb", "sma", "coax")
        )
    ]
    for footprint in likely_connectors:
        spec = specs.get(footprint.ref)
        if spec is None or spec.connector is None:
            code = "connector_placement_part_spec_missing"
            findings.append(
                ConnectorPlacementFinding(
                    code=code,
                    severity="error",
                    ref=footprint.ref,
                    message="connector-family footprint has no connector PartSpec",
                )
            )
            requests.append(
                _human_request(
                    ref=footprint.ref,
                    board_path=pcb_path,
                    board_sha=board_sha,
                    spec=spec,
                    code=code,
                    reason=(
                        "A connector footprint cannot be safely placed without a "
                        "connector PartSpec."
                    ),
                )
            )
            continue
        connector = spec.connector
        envelope = connector.mating_envelope
        if envelope is None:
            code = "connector_mating_envelope_unknown"
            findings.append(
                ConnectorPlacementFinding(
                    code=code,
                    severity="error",
                    ref=footprint.ref,
                    message="mating envelope, travel, and access clearance are unknown",
                )
            )
            requests.append(
                _human_request(
                    ref=footprint.ref,
                    board_path=pcb_path,
                    board_sha=board_sha,
                    spec=spec,
                    code=code,
                    reason="The connector mating envelope is not evidenced.",
                )
            )
        if connector.orientation in {"right_angle", "edge_mount"}:
            board_edge = connector.board_edge
            local_edges = [
                (_transform(start, footprint), _transform(end, footprint))
                for start, end in footprint.edge_lines
            ]
            offset_matches = False
            if board_edge is not None:
                expected_side = _mirror_library_axis(board_edge.side, footprint)
                expected_offset = _dimension_value(board_edge.offset)
                if expected_side != board_edge.side:
                    expected_offset = -expected_offset
                offset_matches = any(
                    (
                        abs(start[0] - expected_offset) <= 0.1
                        and abs(end[0] - expected_offset) <= 0.1
                    )
                    if expected_side in {"+x", "-x"}
                    else (
                        abs(start[1] - expected_offset) <= 0.1
                        and abs(end[1] - expected_offset) <= 0.1
                    )
                    for start, end in footprint.edge_lines
                )
            at_edge = any(
                max(
                    _point_segment_distance(first, edge),
                    _point_segment_distance(second, edge),
                )
                <= 0.1
                for first, second in local_edges
                for edge in segments
            )
            if not offset_matches or not at_edge:
                findings.append(
                    ConnectorPlacementFinding(
                        code="connector_not_at_board_edge",
                        severity="error",
                        ref=footprint.ref,
                        message=(
                            "transformed connector board-edge line does not match "
                            "Edge.Cuts within 0.1 mm"
                        ),
                    )
                )
        if envelope is None:
            continue
        envelope_box = _envelope_box(envelope, footprint, spec)
        axis = connector.mating_axis
        thickness = _board_thickness(parsed)
        overlaps_board_z = thickness is None or envelope.z_min < -1e-6
        if (
            axis != "+z"
            and polygon
            and overlaps_board_z
            and _outline_intersects_box(envelope_box, polygon)
        ):
            findings.append(
                ConnectorPlacementFinding(
                    code="connector_mating_board_interference",
                    severity="error",
                    ref=footprint.ref,
                    obstructing_ref="Edge.Cuts",
                    message="mating envelope or swept access region intersects the board outline",
                )
            )
        for neighbor in board_footprints:
            if neighbor.ref == footprint.ref or neighbor.side != footprint.side:
                continue
            neighbor_box = _bbox([_transform(point, neighbor) for point in neighbor.courtyard])
            if neighbor_box is None or not _intersects(envelope_box, neighbor_box):
                continue
            neighbor_spec = specs.get(neighbor.ref)
            if axis == "+z":
                height = _neighbor_height(neighbor, neighbor_spec, pcb_path)
                if height is not None and envelope.z_min > height:
                    continue
            findings.append(
                ConnectorPlacementFinding(
                    code="connector_mating_clearance",
                    severity="error",
                    ref=footprint.ref,
                    obstructing_ref=neighbor.ref,
                    message=(
                        "mating swept envelope overlaps a same-side courtyard; "
                        "neighboring height is unknown or interferes"
                    ),
                )
            )
    return ConnectorPlacementReport(
        verdict="fail" if any(item.severity == "error" for item in findings) else "pass",
        input_hashes=hashes,
        findings=findings,
        human_requests=requests,
    )
