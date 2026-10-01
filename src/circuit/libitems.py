"""Strict readers for KiCad footprint and symbol library items."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from . import sexpr

PadType = Literal["smd", "thru_hole", "np_thru_hole", "connect"]
GraphicKind = Literal["line", "rect", "circle", "arc", "poly"]
Point = tuple[float, float]
Vector3 = tuple[float, float, float]


class LibItemError(ValueError):
    """Raised when a KiCad library item is malformed or unsupported."""


class PadDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    type: PadType
    shape: str
    x: float
    y: float
    rotation: float
    width: float
    height: float
    drill: float | None
    layers: list[str]


class GraphicDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer: str
    kind: GraphicKind
    points: list[Point]
    width: float


class ModelRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    offset: Vector3
    scale: Vector3
    rotate: Vector3


class FootprintDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    attributes: list[str]
    pads: list[PadDef]
    graphics: list[GraphicDef]
    models: list[ModelRef]
    properties: dict[str, str]


class SymPin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    name: str
    electrical_type: str
    x: float
    y: float
    length: float
    orientation: float
    unit: int


class SymbolDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    pins: list[SymPin]
    properties: dict[str, str]


def _lists(node: list[sexpr.SExpr], name: str) -> list[list[sexpr.SExpr]]:
    return [child for child in node[1:] if isinstance(child, list) and child and child[0] == name]


def _first(node: list[sexpr.SExpr], name: str) -> list[sexpr.SExpr] | None:
    return next(iter(_lists(node, name)), None)


def _atom(node: list[sexpr.SExpr], index: int, *, label: str) -> str:
    if index >= len(node):
        raise LibItemError(f"missing {label}")
    value = node[index]
    if not isinstance(value, str):
        raise LibItemError(f"missing {label}")
    return value


def _number(value: sexpr.SExpr, *, label: str) -> float:
    if not isinstance(value, str):
        raise LibItemError(f"missing numeric {label}")
    try:
        return float(value)
    except ValueError as exc:
        raise LibItemError(f"invalid numeric {label}: {value}") from exc


def _point(node: list[sexpr.SExpr] | None, *, label: str) -> Point:
    if node is None or len(node) < 3:
        raise LibItemError(f"missing {label} point")
    return (_number(node[1], label=f"{label} x"), _number(node[2], label=f"{label} y"))


def _vector3(node: list[sexpr.SExpr] | None, default: Vector3, *, label: str) -> Vector3:
    if node is None:
        return default
    values = node[1:]
    if values and isinstance(values[0], list):
        values = values[0][1:]
    if len(values) < 3:
        raise LibItemError(f"missing {label} vector coordinates")
    return (
        _number(values[0], label=f"{label} x"),
        _number(values[1], label=f"{label} y"),
        _number(values[2], label=f"{label} z"),
    )


def _read_root(path: Path) -> list[sexpr.SExpr]:
    try:
        return sexpr.parse_text(path.read_text(encoding="utf-8"))
    except (OSError, sexpr.SExprError) as exc:
        raise LibItemError(f"could not parse KiCad item {path}: {exc}") from exc


def _parse_pad(node: list[sexpr.SExpr]) -> PadDef:
    if len(node) < 4:
        raise LibItemError("pad declaration is incomplete")
    number = _atom(node, 1, label="pad number")
    pad_type = _atom(node, 2, label="pad type")
    if pad_type not in ("smd", "thru_hole", "np_thru_hole", "connect"):
        raise LibItemError(f"unsupported pad type: {pad_type}")
    shape = _atom(node, 3, label="pad shape")
    at = _first(node, "at")
    if at is None or len(at) < 3:
        raise LibItemError(f"pad {number} is missing its position")
    x = _number(at[1], label=f"pad {number} x")
    y = _number(at[2], label=f"pad {number} y")
    rotation = _number(at[3], label=f"pad {number} rotation") if len(at) >= 4 else 0.0
    size = _first(node, "size")
    if size is None or len(size) < 3:
        raise LibItemError(f"pad {number} is missing its size")
    width = _number(size[1], label=f"pad {number} width")
    height = _number(size[2], label=f"pad {number} height")
    drill_node = _first(node, "drill")
    drill = None
    if drill_node is not None:
        for value in drill_node[1:]:
            if not isinstance(value, str):
                continue
            try:
                drill = float(value)
                break
            except ValueError:
                continue
        if drill is None:
            raise LibItemError(f"pad {number} has an invalid drill size")
    layers_node = _first(node, "layers")
    if layers_node is None:
        raise LibItemError(f"pad {number} is missing its layer list")
    layers = [value for value in layers_node[1:] if isinstance(value, str)]
    return PadDef(
        number=number,
        type=pad_type,
        shape=shape,
        x=x,
        y=y,
        rotation=rotation,
        width=width,
        height=height,
        drill=drill,
        layers=layers,
    )


def _graphic_points(node: list[sexpr.SExpr], kind: GraphicKind) -> list[Point]:
    if kind == "poly":
        pts = _first(node, "pts")
        if pts is None:
            raise LibItemError("polygon is missing points")
        return [_point(child, label="polygon") for child in _lists(pts, "xy")]
    keys = {
        "line": ("start", "end"),
        "rect": ("start", "end"),
        "circle": ("center", "end"),
        "arc": ("start", "mid", "end"),
    }[kind]
    points = [_point(_first(node, key), label=key) for key in keys if _first(node, key) is not None]
    if len(points) < 2:
        raise LibItemError(f"{kind} graphic must contain at least two points")
    return points


def _parse_graphic(node: list[sexpr.SExpr], kind: GraphicKind) -> GraphicDef:
    layer_node = _first(node, "layer")
    if layer_node is None or len(layer_node) < 2:
        raise LibItemError(f"{kind} graphic is missing its layer")
    stroke = _first(node, "stroke")
    width_node = _first(stroke, "width") if stroke is not None else None
    if width_node is None:
        width_node = _first(node, "width")
    width = _number(width_node[1], label=f"{kind} width") if width_node is not None else 0.0
    return GraphicDef(
        layer=_atom(layer_node, 1, label=f"{kind} layer"),
        kind=kind,
        points=_graphic_points(node, kind),
        width=width,
    )


def _properties(node: list[sexpr.SExpr]) -> dict[str, str]:
    properties: dict[str, str] = {}
    for child in node[1:]:
        if not isinstance(child, list) or not child:
            continue
        if child[0] == "property" and len(child) >= 3:
            key = _atom(child, 1, label="property name")
            properties[key] = _atom(child, 2, label=f"{key} property value")
        elif child[0] == "fp_text" and len(child) >= 3:
            kind = _atom(child, 1, label="footprint text kind").casefold()
            key = {"reference": "Reference", "value": "Value"}.get(kind, kind.title())
            properties.setdefault(key, _atom(child, 2, label=f"{key} text"))
    return properties


def _parse_model(node: list[sexpr.SExpr]) -> ModelRef:
    path = _atom(node, 1, label="3D model path")
    return ModelRef(
        path=path,
        offset=_vector3(_first(node, "offset"), (0.0, 0.0, 0.0), label="offset"),
        scale=_vector3(_first(node, "scale"), (1.0, 1.0, 1.0), label="scale"),
        rotate=_vector3(_first(node, "rotate"), (0.0, 0.0, 0.0), label="rotate"),
    )


def parse_footprint(path: Path) -> FootprintDef:
    """Parse a KiCad 6-10 footprint file."""

    root = _read_root(path)
    if not root or root[0] not in ("footprint", "module"):
        raise LibItemError(f"not a KiCad footprint: {path}")
    name = _atom(root, 1, label="footprint name")
    attributes = [
        value for node in _lists(root, "attr") for value in node[1:] if isinstance(value, str)
    ]
    pads = [_parse_pad(node) for node in _lists(root, "pad")]
    graphics: list[GraphicDef] = []
    for name_kind, kind in (
        ("fp_line", "line"),
        ("fp_rect", "rect"),
        ("fp_circle", "circle"),
        ("fp_arc", "arc"),
        ("fp_poly", "poly"),
    ):
        graphics.extend(
            _parse_graphic(node, cast(GraphicKind, kind)) for node in _lists(root, name_kind)
        )
    models = [_parse_model(node) for node in _lists(root, "model")]
    return FootprintDef(
        name=name,
        attributes=attributes,
        pads=pads,
        graphics=graphics,
        models=models,
        properties=_properties(root),
    )


def _symbol_node(root: list[sexpr.SExpr], name: str) -> list[sexpr.SExpr] | None:
    return next(
        (
            child
            for child in root[1:]
            if isinstance(child, list)
            and len(child) >= 2
            and child[0] == "symbol"
            and child[1] == name
        ),
        None,
    )


def _symbol_properties(node: list[sexpr.SExpr]) -> dict[str, str]:
    return {
        _atom(prop, 1, label="symbol property name"): _atom(prop, 2, label="symbol property value")
        for prop in _lists(node, "property")
        if len(prop) >= 3
    }


def _unit_from_name(name: str) -> int:
    match = re.search(r"_(?:\d+)_(\d+)$", name)
    return int(match.group(1)) if match is not None else 1


def _parse_symbol_pin(node: list[sexpr.SExpr], unit: int) -> SymPin:
    if len(node) < 2:
        raise LibItemError("symbol pin is missing its electrical type")
    electrical_type = _atom(node, 1, label="pin electrical type")
    at = _first(node, "at")
    if at is None or len(at) < 4:
        raise LibItemError("symbol pin is missing its position or orientation")
    length_node = _first(node, "length")
    if length_node is None or len(length_node) < 2:
        raise LibItemError("symbol pin is missing its length")
    name_node = _first(node, "name")
    number_node = _first(node, "number")
    pin_unit = _first(node, "unit")
    return SymPin(
        number=_atom(number_node or [], 1, label="pin number"),
        name=_atom(name_node or [], 1, label="pin name"),
        electrical_type=electrical_type,
        x=_number(at[1], label="pin x"),
        y=_number(at[2], label="pin y"),
        length=_number(length_node[1], label="pin length"),
        orientation=_number(at[3], label="pin orientation"),
        unit=int(_atom(pin_unit, 1, label="pin unit")) if pin_unit is not None else unit,
    )


def _symbol_pins(node: list[sexpr.SExpr]) -> list[SymPin]:
    pins: list[SymPin] = []

    def visit(scope: list[sexpr.SExpr], unit: int) -> None:
        for child in scope[1:]:
            if not isinstance(child, list) or not child:
                continue
            if child[0] == "pin":
                pins.append(_parse_symbol_pin(child, unit))
            elif child[0] == "symbol":
                unit_name = _atom(child, 1, label="symbol unit name")
                visit(child, _unit_from_name(unit_name))

    visit(node, 1)
    return pins


def _parse_symbol(root: list[sexpr.SExpr], name: str, stack: set[str]) -> SymbolDef:
    if name in stack:
        raise LibItemError(f"cyclic symbol inheritance: {name}")
    node = _symbol_node(root, name)
    if node is None:
        raise LibItemError(f"symbol not found: {name}")
    stack.add(name)
    properties: dict[str, str] = {}
    parent = next(
        (
            child[1]
            for child in node[1:]
            if isinstance(child, list)
            and len(child) == 2
            and child[0] == "extends"
            and isinstance(child[1], str)
        ),
        None,
    )
    pins: list[SymPin] = []
    if isinstance(parent, str):
        inherited = _parse_symbol(root, parent, stack)
        properties.update(inherited.properties)
        pins.extend(inherited.pins)
    properties.update(_symbol_properties(node))
    pins.extend(_symbol_pins(node))
    return SymbolDef(name=name, pins=pins, properties=properties)


def parse_symbol(lib_path: Path, name: str) -> SymbolDef:
    """Parse a symbol from a library, resolving any ``extends`` parent."""

    root = _read_root(lib_path)
    if not root or root[0] != "kicad_symbol_lib":
        raise LibItemError(f"not a KiCad symbol library: {lib_path}")
    return _parse_symbol(root, name, set())
