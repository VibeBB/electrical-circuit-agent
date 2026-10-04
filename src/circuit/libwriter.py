"""Deterministic KiCad symbol and footprint writers."""

from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import sexpr
from .landpattern import LandPatternResult
from .libverify import nominal_body_box
from .partspec import Dimension, LandPad, PartSpec, PinSpec

WRITER_VERSION = "1"
_SUPPORTED_FAMILIES = {
    "no_lead_quad",
    "no_lead_dual",
    "gullwing_quad",
    "gullwing_dual",
    "chip",
    "sot223",
    "tabbed_dpak",
    "sod",
    "bga",
    "connector",
}
_SYMBOL_PIN_TYPES = {
    "input": "input",
    "output": "output",
    "bidirectional": "bidirectional",
    "tri_state": "tri_state",
    "passive": "passive",
    "free": "free",
    "unspecified": "unspecified",
    "power_in": "power_in",
    "power_out": "power_out",
    "open_collector": "open_collector",
    "open_emitter": "open_emitter",
    "no_connect": "no_connect",
}
_GROUND_NAMES = {"GND", "VSS", "PGND", "AGND", "DGND", "EP"}
_NUMBER_TOKEN = re.compile(r"\d+")


class LibWriterError(ValueError):
    """Raised when deterministic library output cannot be produced safely."""


class WriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str
    writer_version: str
    part_spec_sha256: str
    land_pattern_sha256: str | None
    name: str


def _format_number(value: float) -> str:
    if not math.isfinite(value):
        raise LibWriterError("invalid_geometry")
    rendered = f"{round(value, 4):.4f}".rstrip("0").rstrip(".")
    return "0" if rendered in ("", "-0") else rendered


def _checked_spec_sha256(spec: PartSpec, supplied: str) -> str:
    source_path = spec.source_file_path
    if source_path is None or re.fullmatch(r"[0-9a-f]{64}", supplied) is None:
        raise LibWriterError("part_spec_unchecked")
    try:
        payload = source_path.read_bytes()
        current_sha256 = hashlib.sha256(payload).hexdigest()
        current_spec = PartSpec.model_validate_json(payload)
    except (OSError, ValueError):
        raise LibWriterError("part_spec_unchecked") from None
    if current_sha256 != supplied or current_spec.model_dump(mode="json") != spec.model_dump(
        mode="json"
    ):
        raise LibWriterError("part_spec_unchecked")
    return current_sha256


def _quoted_property(
    name: str,
    value: str,
    *,
    y: float = 0.0,
    layer: str = "F.Fab",
    hidden: bool = True,
) -> sexpr.SExpr:
    property_node: list[sexpr.SExpr] = [
        "property",
        sexpr.quoted(name),
        sexpr.quoted(value),
        ["at", "0", _format_number(y), "0"],
        ["layer", sexpr.quoted(layer)],
    ]
    if hidden:
        property_node.append(["hide", "yes"])
    property_node.append(["effects", ["font", ["size", "1", "1"]]])
    return property_node


def _symbol_property(
    name: str,
    value: str,
    *,
    y: float = 0.0,
    hidden: bool = True,
) -> sexpr.SExpr:
    node: list[sexpr.SExpr] = [
        "property",
        sexpr.quoted(name),
        sexpr.quoted(value),
        ["at", "0", _format_number(y), "0"],
    ]
    effects: list[sexpr.SExpr] = ["effects", ["font", ["size", "1", "1"]]]
    if hidden:
        effects.append(["hide", "yes"])
    node.append(effects)
    return node


def _pad_number_key(number: str) -> tuple[object, ...]:
    if number.isdigit():
        return (0, int(number), number)
    parts: list[tuple[int, int | str]] = []
    for item in re.split(r"(\d+)", number.casefold()):
        if not item:
            continue
        parts.append((0, int(item)) if item.isdigit() else (1, item))
    return (1, tuple(parts), number)


def _outward(value: float, *, lower: bool) -> float:
    scaled = Decimal(str(value)) * 100
    rounding = ROUND_FLOOR if lower else ROUND_CEILING
    return float(scaled.to_integral_value(rounding=rounding) / 100)


def _land_hash(land: LandPatternResult, spec: PartSpec) -> str:
    legacy_primary_ep = (
        spec.package.exposed_pad.number
        if spec.package.family
        in {"no_lead_quad", "no_lead_dual", "gullwing_quad", "gullwing_dual", "chip"}
        and spec.package.exposed_pad is not None
        else None
    )
    pads: list[dict[str, object]] = []
    for pad in land.pads:
        pad_data: dict[str, object] = {
            "number": pad.number,
            "x": pad.x,
            "y": pad.y,
            "width": pad.width,
            "height": pad.height,
            "shape": pad.shape,
        }
        if pad.rotation != 0:
            pad_data["rotation"] = pad.rotation
        if pad.polygon is not None:
            pad_data["polygon"] = pad.polygon
        if pad.kind != "signal" and pad.number != legacy_primary_ep:
            pad_data["kind"] = pad.kind
        if spec.package.family == "connector" or pad.pad_type != "smd" or pad.drill is not None:
            pad_data["pad_type"] = pad.pad_type
            if pad.drill is not None:
                pad_data["drill"] = pad.drill
        pads.append(pad_data)
    payload: dict[str, object] = {
        "family": land.family,
        "density": land.density,
        "source": land.source,
        "params": land.params,
        "pads": pads,
        "courtyard": land.courtyard,
        "konnect_pads": land.konnect_pads,
        "rule_chain": land.rule_chain,
        "rule_chain_sha256": land.rule_chain_sha256,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _pad_node(
    pad: LandPad,
    *,
    exposed_pad_number: str | None,
    ep_paste_margin_mm: float | None,
) -> sexpr.SExpr:
    number = pad.number
    x = pad.x
    y = pad.y
    width = pad.width
    height = pad.height
    shape = pad.shape
    is_polygon = shape == "polygon"
    if shape not in {"rect", "roundrect", "oval", "circle", "polygon"}:
        raise LibWriterError("unsupported_pad_shape")
    if is_polygon and pad.polygon is None:
        raise LibWriterError("polygon_vertices_missing")
    kicad_shape = "custom" if is_polygon else shape
    if pad.pad_type == "unknown":
        raise LibWriterError("connector_plating_unresolved")
    at: list[sexpr.SExpr] = ["at", _format_number(float(x)), _format_number(float(y))]
    if pad.rotation != 0 or is_polygon:
        at.append(_format_number(pad.rotation))
    node: list[sexpr.SExpr] = [
        "pad",
        sexpr.quoted(str(number)),
        pad.pad_type,
        str(kicad_shape),
        at,
        ["size", _format_number(float(width)), _format_number(float(height))],
    ]
    if pad.pad_type in {"thru_hole", "np_thru_hole"}:
        if pad.drill is None:
            raise LibWriterError("connector_drill_missing")
        node.append(["drill", _format_number(pad.drill)])
    if shape == "roundrect":
        node.append(["roundrect_rratio", "0.25"])
    if ep_paste_margin_mm is not None and (
        pad.kind == "exposed" or str(number) == exposed_pad_number
    ):
        node.append(["solder_paste_margin", _format_number(ep_paste_margin_mm)])
    layers: list[str] = (
        ["*.Cu", "*.Mask"]
        if pad.pad_type == "thru_hole"
        else ["*.Mask"]
        if pad.pad_type == "np_thru_hole"
        else ["F.Cu", "F.Paste", "F.Mask"]
    )
    node.append(["layers", *[sexpr.quoted(layer) for layer in layers]])
    if is_polygon:
        polygon = pad.polygon
        if polygon is None:
            raise LibWriterError("polygon_vertices_missing")
        node.append(["options", ["clearance", "outline"], ["anchor", "rect"]])
        node.append(
            [
                "primitives",
                [
                    "gr_poly",
                    [
                        "pts",
                        *[["xy", _format_number(px), _format_number(py)] for px, py in polygon],
                    ],
                    ["width", "0.05"],
                    ["fill", "yes"],
                ],
            ]
        )
    return node


def _pin1_silk_circle(spec: PartSpec, land: LandPatternResult) -> sexpr.SExpr:
    if spec.connector is not None:
        points: list[tuple[float, float]] = []
        for pad in land.pads:
            angle = math.radians(pad.rotation)
            cosine, sine = math.cos(angle), math.sin(angle)
            source = (
                pad.polygon
                if pad.polygon is not None
                else [
                    (-pad.width / 2, -pad.height / 2),
                    (-pad.width / 2, pad.height / 2),
                    (pad.width / 2, -pad.height / 2),
                    (pad.width / 2, pad.height / 2),
                ]
            )
            points.extend(
                (
                    pad.x + x * cosine - y * sine,
                    pad.y + x * sine + y * cosine,
                )
                for x, y in source
            )
        if points:
            center_x = min(x for x, _ in points) - 0.25
            center_y = min(y for _, y in points) - 0.25
            radius = 0.05
            return [
                "fp_circle",
                ["center", _format_number(center_x), _format_number(center_y)],
                ["end", _format_number(center_x + radius), _format_number(center_y)],
                ["stroke", ["width", "0.15"], ["type", "solid"]],
                ["fill", "solid"],
                ["layer", sexpr.quoted("F.SilkS")],
            ]
    first_pin = next(
        (
            pad
            for pad in land.pads
            if pad.number == "1" or (spec.package.family == "bga" and pad.kind == "signal")
        ),
        None,
    )
    if first_pin is None:
        raise LibWriterError("pin1_pad_missing")
    corner = spec.package.pin1_corner or "top_left"
    sx = -1 if "left" in corner else 1
    sy = -1 if "top" in corner else 1
    if first_pin.x != 0:
        sx = -1 if first_pin.x < 0 else 1
    if first_pin.y != 0:
        sy = -1 if first_pin.y < 0 else 1
    center_x = first_pin.x + sx * (first_pin.width / 2 + 0.25)
    center_y = first_pin.y + sy * (first_pin.height / 2 + 0.25)
    radius = 0.05
    return [
        "fp_circle",
        ["center", _format_number(center_x), _format_number(center_y)],
        ["end", _format_number(center_x + radius), _format_number(center_y)],
        ["stroke", ["width", "0.15"], ["type", "solid"]],
        ["fill", "solid"],
        ["layer", sexpr.quoted("F.SilkS")],
    ]


def _fab_chamfer(
    spec: PartSpec,
    body: tuple[float, float, float, float],
) -> sexpr.SExpr:
    x0, y0, x1, y1 = body
    corner = spec.package.pin1_corner or "top_left"
    chamfer = min(x1 - x0, y1 - y0) * 0.1
    if corner == "top_left":
        start, end = (x0, y0 + chamfer), (x0 + chamfer, y0)
    elif corner == "top_right":
        start, end = (x1 - chamfer, y0), (x1, y0 + chamfer)
    elif corner == "bottom_left":
        start, end = (x0, y1 - chamfer), (x0 + chamfer, y1)
    else:
        start, end = (x1 - chamfer, y1), (x1, y1 - chamfer)
    return [
        "fp_line",
        ["start", _format_number(start[0]), _format_number(start[1])],
        ["end", _format_number(end[0]), _format_number(end[1])],
        ["stroke", ["width", "0.05"], ["type", "solid"]],
        ["layer", sexpr.quoted("F.Fab")],
    ]


def _dimension_value(dimension: Dimension) -> float:
    nominal = dimension.nom
    if nominal is not None:
        return float(nominal)
    minimum = dimension.min
    maximum = dimension.max
    if minimum is not None and maximum is not None:
        return (float(minimum) + float(maximum)) / 2
    value = minimum if minimum is not None else maximum
    if value is None:
        raise LibWriterError("connector_dimension_missing")
    return float(value)


def _connector_board_edge_node(
    spec: PartSpec, courtyard: tuple[float, float, float, float]
) -> sexpr.SExpr | None:
    connector = spec.connector
    if connector is None or connector.board_edge is None:
        return None
    edge = connector.board_edge
    offset = _dimension_value(edge.offset)
    x = offset if edge.side in {"+x", "-x"} else 0.0
    y = offset if edge.side in {"+y", "-y"} else 0.0
    if edge.side == "+x" or edge.side == "-x":
        start, end = (x, courtyard[1]), (x, courtyard[3])
    elif edge.side == "+y":
        start, end = (courtyard[0], y), (courtyard[2], y)
    else:
        start, end = (courtyard[0], y), (courtyard[2], y)
    return [
        "fp_line",
        ["start", _format_number(start[0]), _format_number(start[1])],
        ["end", _format_number(end[0]), _format_number(end[1])],
        ["stroke", ["width", "0.05"], ["type", "solid"]],
        ["layer", sexpr.quoted("Dwgs.User")],
    ]


def _connector_keepout_node(spec: PartSpec) -> sexpr.SExpr | None:
    connector = spec.connector
    if connector is None or connector.copper_keepout is None:
        return None
    keepout = connector.copper_keepout
    points = [
        (_dimension_value(keepout.x0), _dimension_value(keepout.y0)),
        (_dimension_value(keepout.x1), _dimension_value(keepout.y0)),
        (_dimension_value(keepout.x1), _dimension_value(keepout.y1)),
        (_dimension_value(keepout.x0), _dimension_value(keepout.y1)),
    ]
    return [
        "zone",
        ["net", "0"],
        ["net_name", sexpr.quoted("")],
        ["layer", sexpr.quoted("F.Cu")],
        ["hatch", "edge", "0.5"],
        [
            "keepout",
            ["tracks", "not_allowed"],
            ["vias", "not_allowed"],
            ["pads", "not_allowed"],
            ["copperpour", "not_allowed"],
            ["footprints", "not_allowed"],
        ],
        [
            "polygon",
            [
                "pts",
                *[["xy", _format_number(x), _format_number(y)] for x, y in points],
            ],
        ],
    ]


def write_footprint(
    spec: PartSpec,
    land: LandPatternResult,
    out: Path,
    *,
    spec_sha256: str,
    name: str | None = None,
    model_path: str | None = None,
    ep_paste_margin_mm: float | None = None,
) -> WriteResult:
    checked_sha256 = _checked_spec_sha256(spec, spec_sha256)
    if spec.package.family not in _SUPPORTED_FAMILIES:
        raise LibWriterError("unsupported_family")
    if land.family != spec.package.family:
        raise LibWriterError("land_pattern_family_mismatch")
    if ep_paste_margin_mm is not None and not math.isfinite(ep_paste_margin_mm):
        raise LibWriterError("invalid_paste_margin")
    footprint_name = name if name is not None else spec.package.drawing_id
    if not footprint_name:
        raise LibWriterError("footprint_name_missing")
    body = nominal_body_box(spec)
    if body is None:
        raise LibWriterError("nominal_body_missing")
    x0, y0, x1, y1 = body
    courtyard = (
        _outward(land.courtyard[0], lower=True),
        _outward(land.courtyard[1], lower=True),
        _outward(land.courtyard[2], lower=False),
        _outward(land.courtyard[3], lower=False),
    )
    land_sha256 = _land_hash(land, spec)
    properties: list[sexpr.SExpr] = [
        _quoted_property(
            "Reference",
            "REF**",
            y=min(courtyard[1], y0) - 0.8,
            layer="F.SilkS",
            hidden=False,
        ),
        _quoted_property(
            "Value",
            footprint_name,
            y=max(courtyard[3], y1) + 0.8,
            hidden=False,
        ),
        _quoted_property("Datasheet", spec.datasheet.url or spec.datasheet.path),
        _quoted_property("circuit_part_spec_sha256", checked_sha256),
        _quoted_property("circuit_land_pattern_sha256", land_sha256),
        _quoted_property("circuit_writer_version", WRITER_VERSION),
    ]
    if spec.connector is not None and spec.connector.board_edge is not None:
        edge = spec.connector.board_edge
        properties.append(
            _quoted_property(
                "circuit_board_edge",
                f"{edge.side} {_format_number(_dimension_value(edge.offset))}",
            )
        )
    attr: sexpr.SExpr | None = (
        ["attr", "smd"]
        if spec.connector is None or spec.connector.mount == "smd"
        else ["attr", "through_hole"]
        if spec.connector.mount == "tht"
        else None
    )
    root: list[sexpr.SExpr] = [
        "footprint",
        sexpr.quoted(footprint_name),
        ["layer", sexpr.quoted("F.Cu")],
        *([] if attr is None else [attr]),
        *properties,
        [
            "fp_rect",
            ["start", _format_number(x0), _format_number(y0)],
            ["end", _format_number(x1), _format_number(y1)],
            ["stroke", ["width", "0.05"], ["type", "solid"]],
            ["fill", "none"],
            ["layer", sexpr.quoted("F.Fab")],
        ],
        _fab_chamfer(spec, body),
        [
            "fp_rect",
            ["start", _format_number(courtyard[0]), _format_number(courtyard[1])],
            ["end", _format_number(courtyard[2]), _format_number(courtyard[3])],
            ["stroke", ["width", "0.05"], ["type", "solid"]],
            ["fill", "none"],
            ["layer", sexpr.quoted("F.CrtYd")],
        ],
        _pin1_silk_circle(spec, land),
    ]
    board_edge = _connector_board_edge_node(spec, courtyard)
    if board_edge is not None:
        root.append(board_edge)
    keepout = _connector_keepout_node(spec)
    if keepout is not None:
        root.append(keepout)
    ep_numbers = {pad.number for pad in spec.package.all_exposed_pads}
    root.extend(
        _pad_node(
            pad,
            exposed_pad_number=pad.number if pad.number in ep_numbers else None,
            ep_paste_margin_mm=ep_paste_margin_mm,
        )
        for pad in sorted(land.pads, key=lambda item: _pad_number_key(item.number))
    )
    if model_path is not None:
        root.append(
            [
                "model",
                sexpr.quoted(model_path),
                ["offset", ["xyz", "0", "0", "0"]],
                ["scale", ["xyz", "1", "1", "1"]],
                ["rotate", ["xyz", "0", "0", "0"]],
            ]
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(sexpr.serialize(root) + "\n", encoding="utf-8")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    return WriteResult(
        path=str(out),
        sha256=digest,
        writer_version=WRITER_VERSION,
        part_spec_sha256=checked_sha256,
        land_pattern_sha256=land_sha256,
        name=footprint_name,
    )


def _pin_group(
    spec: PartSpec, pin: PinSpec
) -> Literal["ground", "power", "left", "right", "remaining"]:
    normalized_name = re.sub(r"[^A-Z0-9]", "", pin.name.upper())
    if normalized_name in _GROUND_NAMES or pin.number in spec.package.auxiliary_pad_numbers:
        return "ground"
    if pin.electrical_type in ("power_in", "power_out"):
        return "power"
    if pin.electrical_type == "input":
        return "left"
    if pin.electrical_type in ("output", "bidirectional", "tri_state"):
        return "right"
    return "remaining"


def _side_pin_position(index: int, count: int) -> float:
    return (index - (count - 1) / 2) * 2.54


def _snap_body_size(value: float) -> float:
    return math.ceil(value / 2.54) * 2.54


def _symbol_pin_groups(spec: PartSpec, pins: list[PinSpec]) -> dict[str, list[PinSpec]]:
    grouped: dict[str, list[PinSpec]] = {
        "ground": [],
        "power": [],
        "left": [],
        "right": [],
        "remaining": [],
    }
    for pin in pins:
        grouped[_pin_group(spec, pin)].append(pin)
    remaining = sorted(grouped["remaining"], key=lambda pin: _pad_number_key(pin.number))
    left_count = (len(remaining) + 1) // 2
    grouped["left"].extend(remaining[:left_count])
    grouped["right"].extend(remaining[left_count:])
    for pin_group in grouped.values():
        pin_group.sort(key=lambda pin: _pad_number_key(pin.number))
    return grouped


def _symbol_body_size(grouped: dict[str, list[PinSpec]]) -> tuple[float, float]:
    left_right_count = max(len(grouped["left"]), len(grouped["right"]))
    top_bottom_count = max(len(grouped["power"]), len(grouped["ground"]))
    max_name_length = max(
        (len(pin.name) for pin_group in grouped.values() for pin in pin_group),
        default=1,
    )
    body_width = _snap_body_size(
        max(5.08, max_name_length * 0.635 + 5.08, max(0, top_bottom_count - 1) * 2.54 + 5.08)
    )
    body_height = _snap_body_size(max(5.08, max(0, left_right_count - 1) * 2.54 + 5.08))
    return body_width, body_height


def _symbol_pin_node(
    pin: PinSpec,
    *,
    x: float,
    y: float,
    orientation: int,
) -> sexpr.SExpr:
    try:
        electrical_type = _SYMBOL_PIN_TYPES[pin.electrical_type]
    except KeyError:
        raise LibWriterError("unsupported_symbol_pin_type") from None
    return [
        "pin",
        electrical_type,
        "line",
        ["at", _format_number(x), _format_number(y), str(orientation)],
        ["length", "2.54"],
        ["name", sexpr.quoted(pin.name), ["effects", ["font", ["size", "1", "1"]]]],
        ["number", sexpr.quoted(pin.number), ["effects", ["font", ["size", "1", "1"]]]],
    ]


def _symbol_unit_node(
    spec: PartSpec,
    symbol_name: str,
    pins: list[PinSpec],
    unit_number: int,
) -> sexpr.SExpr:
    grouped = _symbol_pin_groups(spec, pins)
    body_width, body_height = _symbol_body_size(grouped)
    half_width = body_width / 2
    half_height = body_height / 2
    unit_pins: list[sexpr.SExpr] = [
        [
            "rectangle",
            ["start", _format_number(-half_width), _format_number(-half_height)],
            ["end", _format_number(half_width), _format_number(half_height)],
            ["stroke", ["width", "0.254"], ["type", "default"]],
            ["fill", ["type", "background"]],
        ]
    ]
    for side in ("left", "right"):
        pins = grouped[side]
        for index, pin in enumerate(pins):
            y = _side_pin_position(index, len(pins))
            x = -(half_width + 2.54) if side == "left" else half_width + 2.54
            orientation = 0 if side == "left" else 180
            unit_pins.append(_symbol_pin_node(pin, x=x, y=y, orientation=orientation))
    for side in ("power", "ground"):
        pins = grouped[side]
        for index, pin in enumerate(pins):
            x = _side_pin_position(index, len(pins))
            y = half_height + 2.54 if side == "power" else -(half_height + 2.54)
            orientation = 270 if side == "power" else 90
            unit_pins.append(_symbol_pin_node(pin, x=x, y=y, orientation=orientation))
    return [
        "symbol",
        sexpr.quoted(f"{symbol_name}_0_{unit_number}"),
        *unit_pins,
    ]


def _symbol_node(
    spec: PartSpec,
    symbol_name: str,
    footprint_id: str,
    spec_sha256: str,
    unit_groups: list[list[PinSpec]] | None = None,
) -> sexpr.SExpr:
    datasheet = spec.datasheet.url or spec.datasheet.path
    groups = [spec.pins] if unit_groups is None else unit_groups
    half_height = max(_symbol_body_size(_symbol_pin_groups(spec, pins))[1] / 2 for pins in groups)
    root: list[sexpr.SExpr] = [
        "symbol",
        sexpr.quoted(symbol_name),
        _symbol_property("Reference", "U", y=-half_height - 1.27, hidden=False),
        _symbol_property("Value", spec.mpn, y=half_height + 1.27, hidden=False),
        _symbol_property("Footprint", footprint_id),
        _symbol_property("Datasheet", datasheet),
        _symbol_property("circuit_part_spec_sha256", spec_sha256),
        _symbol_property("circuit_land_pattern_sha256", ""),
        _symbol_property("circuit_writer_version", WRITER_VERSION),
    ]
    root.extend(
        _symbol_unit_node(spec, symbol_name, pins, unit_number)
        for unit_number, pins in enumerate(groups, start=1)
    )
    return root


def write_symbol(
    spec: PartSpec,
    library: Path,
    *,
    spec_sha256: str,
    symbol_name: str | None = None,
    footprint_id: str,
    units: Literal["single", "bank"] = "single",
) -> WriteResult:
    checked_sha256 = _checked_spec_sha256(spec, spec_sha256)
    name = spec.mpn if symbol_name is None else symbol_name
    if not name:
        raise LibWriterError("symbol_name_missing")
    if not footprint_id:
        raise LibWriterError("footprint_id_missing")
    headers: list[sexpr.SExpr]
    symbols: list[sexpr.SExpr]
    if library.is_file():
        try:
            root = sexpr.parse_text(library.read_text(encoding="utf-8"))
        except (OSError, sexpr.SExprError):
            raise LibWriterError("symbol_library_invalid") from None
        if not root or root[0] != "kicad_symbol_lib":
            raise LibWriterError("symbol_library_invalid")
        headers = [child for child in root[1:] if not _is_symbol_node(child)]
        symbols = [child for child in root[1:] if _is_symbol_node(child) and child[1] != name]
    else:
        version_header: sexpr.SExpr = ["version", "20241209"]
        generator_header: sexpr.SExpr = [
            "generator",
            sexpr.quoted("kicad_symbol_editor"),
        ]
        headers = [version_header, generator_header]
        symbols = []
    if units == "single":
        symbol = _symbol_node(spec, name, footprint_id, checked_sha256)
    elif units == "bank":
        grouped: dict[str, list[PinSpec]] = {}
        for pin in spec.pins:
            grouped.setdefault(pin.bank or "COMMON", []).append(pin)
        bank_names = sorted(
            (bank for bank in grouped if bank != "COMMON"),
            key=_pad_number_key,
        )
        ordered_names = bank_names + (["COMMON"] if "COMMON" in grouped else [])
        symbol = _symbol_node(
            spec,
            name,
            footprint_id,
            checked_sha256,
            [grouped[bank] for bank in ordered_names],
        )
    else:
        raise LibWriterError("unsupported_symbol_units")
    symbols.append(symbol)
    symbols.sort(key=lambda item: str(item[1]))
    root: list[sexpr.SExpr] = ["kicad_symbol_lib", *headers, *symbols]
    library.parent.mkdir(parents=True, exist_ok=True)
    library.write_text(sexpr.serialize(root) + "\n", encoding="utf-8")
    digest = hashlib.sha256(library.read_bytes()).hexdigest()
    return WriteResult(
        path=str(library),
        sha256=digest,
        writer_version=WRITER_VERSION,
        part_spec_sha256=checked_sha256,
        land_pattern_sha256=None,
        name=name,
    )


def _is_symbol_node(node: sexpr.SExpr) -> bool:
    return (
        isinstance(node, list)
        and len(node) >= 2
        and node[0] == "symbol"
        and isinstance(node[1], str)
    )
