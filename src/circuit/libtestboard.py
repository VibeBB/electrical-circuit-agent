"""KiCad-backed test-board oracles for library items."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import shutil
import tempfile
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from . import kicad_cli, sexpr
from .gerber import ExportParseError, GerberFeature, parse_excellon, parse_gerber
from .landpattern import compute_land_pattern
from .libitems import (
    FootprintDef,
    GraphicDef,
    PadDef,
    SymbolDef,
    SymPin,
    parse_footprint,
    parse_symbol,
)
from .model3d import Model3dError, expected_terminals
from .netlist import parse_netlist
from .partspec import LandPad, PartSpec
from .ruleprofile import EffectiveRules

_BOARD_REFERENCE = "U1"
_LIBRARY_ALIAS = "test"
_UUID_NAMESPACE = uuid.UUID("f1a7f2a0-19e8-5dd0-a5c4-03c1f2cf32f0")
_IPCD356_COORDINATE = re.compile(r"A\d{2}X([+-]\d+)Y([+-]\d+)X\d+Y\d+R\d{3}")
_IPCD356_NET_ALIAS = re.compile(r"^P\s+NNAME(M\d{4})\s+(.+?)\s*$")


class TestBoardCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    __test__: ClassVar[bool] = False

    name: str
    passed: bool
    details: str = ""


class TestBoardFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    __test__: ClassVar[bool] = False

    code: str
    severity: Literal["error", "warning"]
    subject: str
    message: str


class TestBoard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    __test__: ClassVar[bool] = False

    artifact_kind: Literal["circuit_test_board"]
    verdict: Literal["pass", "fail"]
    clearance_mm: float
    project_sha256: str
    checks: list[TestBoardCheck] = Field(default_factory=lambda: list[TestBoardCheck]())
    findings: list[TestBoardFinding] = Field(default_factory=lambda: list[TestBoardFinding]())
    erc_findings: list[TestBoardFinding] = Field(default_factory=lambda: list[TestBoardFinding]())
    netlist_path: Path | None = None
    ipcd356_path: Path | None = None
    position_path: Path | None = None
    drc_report_path: Path | None = None
    erc_report_path: Path | None = None
    manufacturing_export_paths: list[Path] = Field(default_factory=lambda: list[Path]())


def _uuid(value: str) -> str:
    return str(uuid.uuid5(_UUID_NAMESPACE, value))


def _format_number(value: float) -> str:
    rendered = f"{value:.6f}".rstrip("0").rstrip(".")
    return rendered if rendered else "0"


def _lists(node: list[sexpr.SExpr], name: str) -> list[list[sexpr.SExpr]]:
    return [child for child in node[1:] if isinstance(child, list) and child and child[0] == name]


def _first(node: list[sexpr.SExpr], name: str) -> list[sexpr.SExpr] | None:
    return next(iter(_lists(node, name)), None)


def _net_token(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").upper()


def _pin_function_token(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_")


def _symbol_cache(symbol_lib: Path, symbol_name: str) -> list[sexpr.SExpr]:
    root = sexpr.parse_text(symbol_lib.read_text(encoding="utf-8"))
    if not root or root[0] != "kicad_symbol_lib":
        raise ValueError(f"not a KiCad symbol library: {symbol_lib}")
    symbols = {
        child[1]: child
        for child in root[1:]
        if isinstance(child, list)
        and len(child) > 1
        and child[0] == "symbol"
        and isinstance(child[1], str)
    }
    if symbol_name not in symbols:
        raise ValueError(f"symbol not found: {symbol_name}")
    pending = [symbol_name]
    selected: list[sexpr.SExpr] = []
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        node = symbols.get(current)
        if node is None:
            raise ValueError(f"symbol inheritance parent is missing: {current}")
        seen.add(current)
        cloned = copy.deepcopy(node)
        cloned[1] = sexpr.quoted(f"{_LIBRARY_ALIAS}:{current}")
        extends = _first(cloned, "extends")
        if extends is not None and len(extends) == 2 and isinstance(extends[1], str):
            parent = extends[1]
            pending.append(parent)
            extends[1] = sexpr.quoted(f"{_LIBRARY_ALIAS}:{parent}")
        selected.append(cloned)
    return selected


def _symbol_instance(
    *,
    unit: int,
    pins: list[SymPin],
    spec: PartSpec,
    footprint_name: str,
    symbol_name: str,
    root_uuid: str,
    project_name: str,
    instance_index: int,
    wires: list[sexpr.SExpr],
) -> list[sexpr.SExpr]:
    origin_x = 100.0 + (unit - 1) * 40.0
    origin_y = 100.0
    reference = _BOARD_REFERENCE
    value = spec.mpn
    symbol_instance_uuid = _uuid(f"{spec.mpn}:symbol:{instance_index}")
    properties: list[sexpr.SExpr] = []
    for name, text, y_offset in (
        ("Reference", reference, -3.0),
        ("Value", value, 3.0),
        ("Footprint", f"{_LIBRARY_ALIAS}:{footprint_name}", 0.0),
        ("Datasheet", "", 0.0),
    ):
        properties.append(
            [
                "property",
                sexpr.quoted(name),
                sexpr.quoted(text),
                ["at", _format_number(origin_x), _format_number(origin_y + y_offset), "0"],
                ["effects", ["font", ["size", "1.27", "1.27"]]],
            ]
        )

    instance_pins: list[sexpr.SExpr] = []
    spec_names = {pin.number: pin.name for pin in spec.pins}
    for index, pin in enumerate(pins):
        number = pin.number
        pin_uuid = _uuid(f"{spec.mpn}:pin:{unit}:{index}:{number}")
        instance_pins.append(["pin", sexpr.quoted(number), ["uuid", sexpr.quoted(pin_uuid)]])
        if number not in spec_names:
            continue
        endpoint_x = origin_x + pin.x
        endpoint_y = origin_y - pin.y
        angle = math.radians(-pin.orientation)
        outward_x = endpoint_x + 2.54 * math.cos(angle)
        outward_y = endpoint_y + 2.54 * math.sin(angle)
        wires.append(
            [
                "wire",
                [
                    "pts",
                    ["xy", _format_number(endpoint_x), _format_number(endpoint_y)],
                    ["xy", _format_number(outward_x), _format_number(outward_y)],
                ],
                ["stroke", ["width", "0"], ["type", "default"]],
                ["uuid", sexpr.quoted(_uuid(f"{spec.mpn}:wire:{unit}:{index}:{number}"))],
            ]
        )
        wires.append(
            [
                "label",
                sexpr.quoted(spec_names[number]),
                ["at", _format_number(outward_x), _format_number(outward_y), "0"],
                [
                    "effects",
                    ["font", ["size", "1.27", "1.27"]],
                    ["justify", "left", "bottom"],
                ],
                ["uuid", sexpr.quoted(_uuid(f"{spec.mpn}:label:{unit}:{index}:{number}"))],
            ]
        )
    return [
        "symbol",
        ["lib_id", sexpr.quoted(f"{_LIBRARY_ALIAS}:{symbol_name}")],
        ["at", _format_number(origin_x), _format_number(origin_y), "0"],
        ["unit", str(unit)],
        ["exclude_from_sim", "no"],
        ["in_bom", "yes"],
        ["on_board", "yes"],
        ["dnp", "no"],
        ["uuid", sexpr.quoted(symbol_instance_uuid)],
        *properties,
        *instance_pins,
        [
            "instances",
            [
                "project",
                sexpr.quoted(project_name),
                [
                    "path",
                    sexpr.quoted(f"/{root_uuid}"),
                    ["reference", sexpr.quoted(reference)],
                    ["unit", str(unit)],
                ],
            ],
        ],
    ]


def _write_schematic(
    project_dir: Path,
    *,
    spec: PartSpec,
    symbol_lib: Path,
    symbol_name: str,
    footprint_name: str,
    symbol: SymbolDef,
) -> Path:
    project_name = "test-board"
    root_uuid = _uuid(f"{spec.mpn}:schematic-root")
    cache_symbols = _symbol_cache(symbol_lib, symbol_name)
    symbol_units = sorted({pin.unit for pin in symbol.pins if pin.unit > 0}) or [1]
    wires: list[sexpr.SExpr] = []
    instances: list[sexpr.SExpr] = []
    for index, unit in enumerate(symbol_units):
        unit_pins = [
            pin for pin in symbol.pins if pin.unit == unit or (unit == 1 and pin.unit == 0)
        ]
        instances.append(
            _symbol_instance(
                unit=unit,
                pins=unit_pins,
                spec=spec,
                footprint_name=footprint_name,
                symbol_name=symbol_name,
                root_uuid=root_uuid,
                project_name=project_name,
                instance_index=index,
                wires=wires,
            )
        )
    root: list[sexpr.SExpr] = [
        "kicad_sch",
        ["version", "20231120"],
        ["generator", "eeschema"],
        ["uuid", sexpr.quoted(root_uuid)],
        ["paper", sexpr.quoted("A4")],
        ["lib_symbols", *cache_symbols],
        *wires,
        *instances,
        ["sheet_instances", ["path", sexpr.quoted("/"), ["page", sexpr.quoted("1")]]],
    ]
    output = project_dir / f"{project_name}.kicad_sch"
    output.write_text(sexpr.serialize(root) + "\n", encoding="utf-8")
    return output


def _set_net(node: list[sexpr.SExpr], number: int, name: str) -> None:
    net = _first(node, "net")
    value: list[sexpr.SExpr] = ["net", str(number), sexpr.quoted(name)]
    if net is None:
        node.append(value)
    else:
        net[:] = value


def _opposite_layer(layer: str) -> str:
    if layer.startswith("F."):
        return f"B.{layer[2:]}"
    if layer.startswith("B."):
        return f"F.{layer[2:]}"
    return layer


def _flip_footprint_children(root: list[sexpr.SExpr]) -> None:
    board_at = _first(root, "at")

    def mirror(node: sexpr.SExpr) -> None:
        if not isinstance(node, list) or not node:
            return
        kind = node[0]
        if kind == "at" and len(node) >= 3 and isinstance(node[2], str):
            node[2] = _format_number(-float(node[2]))
            if len(node) >= 4 and isinstance(node[3], str):
                node[3] = _format_number(-float(node[3]))
        elif kind in {"xy", "start", "end", "center", "mid", "control"} and len(node) >= 3:
            if isinstance(node[2], str):
                node[2] = _format_number(-float(node[2]))
        elif kind == "layer" and len(node) >= 2 and isinstance(node[1], str):
            node[1] = sexpr.quoted(_opposite_layer(node[1].strip('"')))
        elif kind == "layers":
            for index in range(1, len(node)):
                if isinstance(node[index], str):
                    layer = cast(str, node[index]).strip('"')
                    node[index] = sexpr.quoted(_opposite_layer(layer))
        for child in node[1:]:
            mirror(child)

    for child in root[1:]:
        if child is board_at:
            continue
        mirror(child)
    layer = _first(root, "layer")
    if layer is not None and len(layer) >= 2:
        layer[1] = sexpr.quoted("B.Cu")


def _board_footprint(
    footprint_path: Path,
    *,
    footprint_name: str,
    net_ids: dict[str, int],
    number_to_name: dict[str, str],
    rotation_deg: float = 0.0,
    placement_xy_mm: tuple[float, float] = (0.0, 0.0),
    model_reference_override: str | None = None,
    side: Literal["F.Cu", "B.Cu"] = "F.Cu",
) -> list[sexpr.SExpr]:
    root = sexpr.parse_text(footprint_path.read_text(encoding="utf-8"))
    if not root or root[0] not in {"footprint", "module"}:
        raise ValueError(f"not a KiCad footprint: {footprint_path}")
    root[0] = "footprint"
    root[1] = sexpr.quoted(f"{_LIBRARY_ALIAS}:{footprint_name}")
    if model_reference_override is not None:
        model_nodes = [
            child for child in root[1:] if isinstance(child, list) and child and child[0] == "model"
        ]
        if model_nodes:
            model_nodes[0][1] = sexpr.quoted(model_reference_override)
            for model_node in model_nodes[1:]:
                root.remove(model_node)
        else:
            root.append(["model", sexpr.quoted(model_reference_override)])
    placement_found = False
    for child in root[1:]:
        if not isinstance(child, list) or not child:
            continue
        if child[0] == "at":
            child[:] = [
                "at",
                _format_number(placement_xy_mm[0]),
                _format_number(placement_xy_mm[1]),
                _format_number(rotation_deg),
            ]
            placement_found = True
        elif child[0] == "property" and len(child) >= 3:
            if child[1] == "Reference":
                child[2] = sexpr.quoted(_BOARD_REFERENCE)
            elif child[1] == "Value":
                child[2] = sexpr.quoted(footprint_name)
        elif child[0] == "pad" and len(child) >= 2 and isinstance(child[1], str):
            pad_name = child[1]
            pin_name = number_to_name.get(pad_name)
            if pin_name is not None and pin_name in net_ids:
                _set_net(child, net_ids[pin_name], pin_name)
            else:
                _set_net(child, 0, "")
    if not placement_found:
        root.insert(
            2,
            [
                "at",
                _format_number(placement_xy_mm[0]),
                _format_number(placement_xy_mm[1]),
                _format_number(rotation_deg),
            ],
        )
    if _first(root, "uuid") is None:
        root.append(["uuid", sexpr.quoted(_uuid(f"{footprint_name}:board-footprint"))])
    if _first(root, "path") is None:
        root.append(["path", sexpr.quoted(f"/{_uuid(f'{footprint_name}:board-path')}")])
    if side == "B.Cu":
        _flip_footprint_children(root)
    for child in root[1:]:
        if not isinstance(child, list) or not child or child[0] != "pad":
            continue
        pad_at = _first(child, "at")
        if pad_at is None or len(pad_at) < 3:
            continue
        pad_rotation = float(pad_at[3]) if len(pad_at) >= 4 and isinstance(pad_at[3], str) else 0.0
        placed_rotation = _format_number(pad_rotation + rotation_deg)
        if len(pad_at) >= 4:
            pad_at[3] = placed_rotation
        else:
            pad_at.append(placed_rotation)
    return root


def _graphic_bounds(graphics: list[GraphicDef]) -> tuple[float, float, float, float] | None:
    points = [point for graphic in graphics for point in graphic.points]
    if not points:
        return None
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _write_board(
    project_dir: Path,
    *,
    spec: PartSpec,
    footprint_path: Path,
    footprint: FootprintDef,
    rules: EffectiveRules,
    rotation_deg: float = 0.0,
    placement_xy_mm: tuple[float, float] = (0.0, 0.0),
    board_thickness_mm: float = 1.6,
    model_reference_override: str | None = None,
    side: Literal["F.Cu", "B.Cu"] = "F.Cu",
) -> tuple[Path, dict[str, str]]:
    copper_pads = [
        pad for pad in footprint.pads if any(layer.endswith(".Cu") for layer in pad.layers)
    ]
    number_to_name = {pin.number: pin.name for pin in spec.pins}
    if any(not pin.name.strip() for pin in spec.pins):
        raise ValueError("spec pins require non-empty names for test-board labels")
    if len(number_to_name) != len(spec.pins):
        raise ValueError("spec pin numbers must be unique for test-board net assignment")
    net_names = sorted({pin.name for pin in spec.pins})
    net_ids = {name: index + 1 for index, name in enumerate(net_names)}
    footprint_root = _board_footprint(
        footprint_path,
        footprint_name=footprint.name,
        net_ids=net_ids,
        number_to_name=number_to_name,
        rotation_deg=rotation_deg,
        placement_xy_mm=placement_xy_mm,
        model_reference_override=model_reference_override,
        side=side,
    )
    courtyard = [
        graphic for graphic in footprint.graphics if graphic.layer in {"F.CrtYd", "B.CrtYd"}
    ]
    bounds = _graphic_bounds(courtyard)
    if bounds is None:
        bounds = _graphic_bounds(
            [
                GraphicDef(
                    layer="F.CrtYd",
                    kind="rect",
                    points=[
                        (
                            pad.x - pad.width / 2,
                            pad.y - pad.height / 2,
                        ),
                        (
                            pad.x + pad.width / 2,
                            pad.y + pad.height / 2,
                        ),
                    ],
                    width=0,
                )
                for pad in copper_pads
            ]
        )
        if bounds is None:
            raise ValueError("footprint has no courtyard or copper pads for the board outline")
    left, top, right, bottom = bounds
    edge = [
        "gr_rect",
        ["start", _format_number(left - 1.0), _format_number(top - 1.0)],
        ["end", _format_number(right + 1.0), _format_number(bottom + 1.0)],
        ["stroke", ["width", "0.05"], ["type", "default"]],
        ["fill", "none"],
        ["layer", sexpr.quoted("Edge.Cuts")],
        ["uuid", sexpr.quoted(_uuid(f"{footprint.name}:edge-cuts"))],
    ]
    layers: list[sexpr.SExpr] = [
        ["0", sexpr.quoted("F.Cu"), "signal"],
        ["31", sexpr.quoted("B.Cu"), "signal"],
        ["32", sexpr.quoted("B.Adhes"), "user", sexpr.quoted("b.adhesive")],
        ["33", sexpr.quoted("F.Adhes"), "user", sexpr.quoted("f.adhesive")],
        ["34", sexpr.quoted("B.Paste"), "user"],
        ["35", sexpr.quoted("F.Paste"), "user"],
        ["36", sexpr.quoted("B.SilkS"), "user", sexpr.quoted("b.silkscreen")],
        ["37", sexpr.quoted("F.SilkS"), "user", sexpr.quoted("f.silkscreen")],
        ["38", sexpr.quoted("B.Mask"), "user"],
        ["39", sexpr.quoted("F.Mask"), "user"],
        ["44", sexpr.quoted("Edge.Cuts"), "user"],
        ["46", sexpr.quoted("B.CrtYd"), "user", sexpr.quoted("b.courtyard")],
        ["47", sexpr.quoted("F.CrtYd"), "user", sexpr.quoted("f.courtyard")],
        ["48", sexpr.quoted("B.Fab"), "user"],
        ["49", sexpr.quoted("F.Fab"), "user"],
    ]
    root = cast(
        list[sexpr.SExpr],
        [
            "kicad_pcb",
            ["version", "20241229"],
            ["generator", "pcbnew"],
            ["general", ["thickness", _format_number(board_thickness_mm)]],
            ["paper", sexpr.quoted("A4")],
            ["layers", *layers],
            ["setup", ["pad_to_mask_clearance", "0"]],
            ["net", "0", sexpr.quoted("")],
            *[["net", str(net_id), sexpr.quoted(name)] for name, net_id in net_ids.items()],
            footprint_root,
            edge,
        ],
    )
    board_path = project_dir / "test-board.kicad_pcb"
    board_path.write_text(sexpr.serialize(root) + "\n", encoding="utf-8")
    project = {
        "meta": {"filename": "test-board", "version": 1},
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": rules.min_pad_clearance_mm,
                    "track_width": 0.25,
                    "via_dia": 0.6,
                    "via_drill": 0.3,
                    "diff_pair_width": 0.25,
                    "diff_pair_gap": 0.25,
                    "microvia_dia": 0.3,
                    "microvia_drill": 0.1,
                }
            ]
        },
    }
    project_path = project_dir / "test-board.kicad_pro"
    project_path.write_text(json.dumps(project, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return board_path, number_to_name


def write_model_export_board(
    project_dir: Path,
    *,
    spec: PartSpec,
    footprint_path: Path,
    rules: EffectiveRules,
    rotation_deg: float,
    placement_xy_mm: tuple[float, float] = (0.0, 0.0),
    model_reference_override: str,
    board_thickness_mm: float = 1.6,
    side: Literal["F.Cu", "B.Cu"] = "F.Cu",
) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    footprint = parse_footprint(footprint_path)
    board_path, _ = _write_board(
        project_dir,
        spec=spec,
        footprint_path=footprint_path,
        footprint=footprint,
        rules=rules,
        rotation_deg=rotation_deg,
        placement_xy_mm=placement_xy_mm,
        board_thickness_mm=board_thickness_mm,
        model_reference_override=model_reference_override,
        side=side,
    )
    return board_path


def _write_local_tables(project_dir: Path, symbol_lib: Path, footprint_path: Path) -> None:
    shutil.copyfile(symbol_lib, project_dir / "test.kicad_sym")
    footprint_library = project_dir / "test.pretty"
    footprint_library.mkdir()
    shutil.copyfile(footprint_path, footprint_library / footprint_path.name)
    (project_dir / "sym-lib-table").write_text(
        '(sym_lib_table (lib (name "test") (type "KiCad") '
        '(uri "${KIPRJMOD}/test.kicad_sym") (options "") (descr "test board")))\n',
        encoding="utf-8",
    )
    (project_dir / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "test") (type "KiCad") '
        '(uri "${KIPRJMOD}/test.pretty") (options "") (descr "test board")))\n',
        encoding="utf-8",
    )


def _shape_area(width: float, height: float, shape: str, ratio: float | None = None) -> float:
    width = max(0.0, width)
    height = max(0.0, height)
    if shape == "circle":
        return math.pi * (min(width, height) / 2) ** 2
    if shape == "oval":
        short = min(width, height)
        long = max(width, height)
        return short * (long - short) + math.pi * (short / 2) ** 2
    if shape == "roundrect":
        radius = max(0.0, min(width, height) * (ratio or 0.0))
        return max(0.0, width * height - (4.0 - math.pi) * radius * radius)
    return width * height


def _pad_area(pad: PadDef, *, margin: float = 0.0) -> float:
    return _shape_area(
        pad.width + 2 * margin,
        pad.height + 2 * margin,
        pad.shape,
        pad.roundrect_ratio,
    )


def _graphic_area(graphic: GraphicDef) -> float:
    if graphic.kind == "rect":
        first, second = graphic.points[:2]
        return abs(second[0] - first[0]) * abs(second[1] - first[1])
    if graphic.kind == "circle":
        center, edge = graphic.points[:2]
        return math.pi * math.dist(center, edge) ** 2
    if graphic.kind == "poly":
        points = graphic.points
        return abs(
            sum(
                points[index][0] * points[(index + 1) % len(points)][1]
                - points[(index + 1) % len(points)][0] * points[index][1]
                for index in range(len(points))
            )
            / 2
        )
    return 0.0


def _paste_findings(
    spec: PartSpec,
    footprint: FootprintDef,
    rules: EffectiveRules,
) -> list[TestBoardFinding]:
    findings: list[TestBoardFinding] = []
    copper_by_number: dict[tuple[str, str], list[PadDef]] = defaultdict(list)
    paste_by_number: dict[tuple[str, str], list[PadDef]] = defaultdict(list)
    for pad in footprint.pads:
        if not pad.number:
            continue
        for side in ("F", "B"):
            if f"{side}.Cu" in pad.layers or "*.Cu" in pad.layers:
                copper_by_number[(pad.number, side)].append(pad)
            if f"{side}.Paste" in pad.layers or "*.Paste" in pad.layers:
                paste_by_number[(pad.number, side)].append(pad)
    exposed_number = spec.package.exposed_pad.number if spec.package.exposed_pad else None
    for (number, side), pads in copper_by_number.items():
        if number == exposed_number:
            continue
        smd_pads = [pad for pad in pads if pad.type == "smd"]
        if not smd_pads:
            continue
        copper_area = sum(_pad_area(pad) for pad in smd_pads)
        paste_pads = paste_by_number.get((number, side), [])
        paste_area = sum(_pad_area(pad, margin=pad.paste_margin or 0.0) for pad in paste_pads)
        coverage = paste_area / copper_area if copper_area else 0.0
        if not rules.paste.coverage_min <= coverage <= rules.paste.coverage_max:
            findings.append(
                TestBoardFinding(
                    code="paste_coverage",
                    severity="error",
                    subject=f"pad {number}",
                    message=(
                        f"paste coverage {coverage:.4f} is outside "
                        f"{rules.paste.coverage_min:.4f}..{rules.paste.coverage_max:.4f}"
                    ),
                )
            )

    exposed = spec.package.exposed_pad
    if exposed is not None:
        exposed_key = next(
            (
                (exposed.number, side)
                for side in ("F", "B")
                if (exposed.number, side) in copper_by_number
            ),
            (exposed.number, "F"),
        )
        pads = copper_by_number.get(exposed_key, [])
        copper_area = sum(_pad_area(pad) for pad in pads)
        paste_area = sum(
            _pad_area(pad, margin=pad.paste_margin or 0.0)
            for pad in paste_by_number.get(exposed_key, [])
        )
        paste_layer = f"{exposed_key[1]}.Paste"
        paste_graphics = [graphic for graphic in footprint.graphics if graphic.layer == paste_layer]
        if pads:
            boxes = [
                (
                    pad.x - pad.width / 2,
                    pad.y - pad.height / 2,
                    pad.x + pad.width / 2,
                    pad.y + pad.height / 2,
                )
                for pad in pads
            ]
            ep_box = (
                min(box[0] for box in boxes),
                min(box[1] for box in boxes),
                max(box[2] for box in boxes),
                max(box[3] for box in boxes),
            )
            for graphic in paste_graphics:
                graphic_bounds = _graphic_bounds([graphic])
                if graphic_bounds is not None and (
                    ep_box[0] <= (graphic_bounds[0] + graphic_bounds[2]) / 2 <= ep_box[2]
                    and ep_box[1] <= (graphic_bounds[1] + graphic_bounds[3]) / 2 <= ep_box[3]
                ):
                    paste_area += _graphic_area(graphic)
        coverage = paste_area / copper_area if copper_area else 0.0
        if not pads or not rules.paste.ep_coverage_min <= coverage <= rules.paste.ep_coverage_max:
            findings.append(
                TestBoardFinding(
                    code="ep_paste_coverage",
                    severity="error",
                    subject=f"exposed pad {exposed.number}",
                    message=(
                        f"EP paste coverage {coverage:.4f} is outside "
                        f"{rules.paste.ep_coverage_min:.4f}..{rules.paste.ep_coverage_max:.4f}"
                    ),
                )
            )

    mask_pads = [
        pad
        for pad in footprint.pads
        if any(layer.endswith(".Cu") for layer in pad.layers)
        and any(layer.endswith(".Mask") for layer in pad.layers)
    ]
    for index, left_pad in enumerate(mask_pads):
        left_layers = {layer for layer in left_pad.layers if layer.endswith(".Mask")}
        for right_pad in mask_pads[index + 1 :]:
            common_layers = left_layers.intersection(
                layer for layer in right_pad.layers if layer.endswith(".Mask")
            )
            if not common_layers:
                continue
            for layer in common_layers:
                left_margin = left_pad.mask_margin or 0.0
                right_margin = right_pad.mask_margin or 0.0
                left_angle = math.radians(left_pad.rotation)
                right_angle = math.radians(right_pad.rotation)
                left_width = abs(left_pad.width * math.cos(left_angle)) + abs(
                    left_pad.height * math.sin(left_angle)
                )
                left_height = abs(left_pad.width * math.sin(left_angle)) + abs(
                    left_pad.height * math.cos(left_angle)
                )
                right_width = abs(right_pad.width * math.cos(right_angle)) + abs(
                    right_pad.height * math.sin(right_angle)
                )
                right_height = abs(right_pad.width * math.sin(right_angle)) + abs(
                    right_pad.height * math.cos(right_angle)
                )
                dx = max(
                    0.0,
                    abs(left_pad.x - right_pad.x)
                    - (left_width + 2 * left_margin + right_width + 2 * right_margin) / 2,
                )
                dy = max(
                    0.0,
                    abs(left_pad.y - right_pad.y)
                    - (left_height + 2 * left_margin + right_height + 2 * right_margin) / 2,
                )
                web = math.hypot(dx, dy)
                if web < rules.min_mask_web_mm:
                    findings.append(
                        TestBoardFinding(
                            code="mask_web",
                            severity="warning",
                            subject=f"pads {left_pad.number}/{right_pad.number}",
                            message=(
                                f"{layer} opening web {web:.4f} mm is below "
                                f"{rules.min_mask_web_mm:.4f} mm"
                            ),
                        )
                    )
    return findings


def _add_result(
    checks: list[TestBoardCheck],
    findings: list[TestBoardFinding],
    *,
    name: str,
    passed: bool,
    details: str = "",
    code: str | None = None,
    severity: Literal["error", "warning"] = "error",
    subject: str = "test_board",
) -> None:
    checks.append(TestBoardCheck(name=name, passed=passed, details=details))
    if not passed and code is not None:
        findings.append(
            TestBoardFinding(code=code, severity=severity, subject=subject, message=details)
        )


@dataclass(frozen=True)
class _ExpectedExportFeature:
    x: float
    y: float
    width: float
    height: float
    area: float
    pad: LandPad


def _kicad_rotate(point: tuple[float, float], angle_deg: float) -> tuple[float, float]:
    angle = math.radians(angle_deg)
    return (
        point[0] * math.cos(angle) + point[1] * math.sin(angle),
        -point[0] * math.sin(angle) + point[1] * math.cos(angle),
    )


def _land_pad_area(pad: LandPad) -> float:
    if pad.shape == "circle":
        return math.pi * pad.width * pad.width / 4
    if pad.shape == "oval":
        short, long = min(pad.width, pad.height), max(pad.width, pad.height)
        return short * (long - short) + math.pi * short * short / 4
    if pad.shape == "roundrect":
        radius = min(pad.width, pad.height) * 0.25
        return pad.width * pad.height - (4 - math.pi) * radius * radius
    if pad.shape == "polygon" and pad.polygon is not None:
        return abs(
            sum(
                first[0] * second[1] - second[0] * first[1]
                for first, second in zip(
                    pad.polygon,
                    (*pad.polygon[1:], pad.polygon[0]),
                    strict=True,
                )
            )
            / 2
        )
    return pad.width * pad.height


def _expected_export_feature(
    pad: LandPad,
    *,
    side: Literal["F.Cu", "B.Cu"],
    rotation_deg: float,
) -> _ExpectedExportFeature:
    mirror_sign = -1.0 if side == "B.Cu" else 1.0
    pad_angle = pad.rotation * mirror_sign
    if pad.shape == "polygon" and pad.polygon is not None:
        corners = list(pad.polygon)
    else:
        corners = [
            (-pad.width / 2, -pad.height / 2),
            (pad.width / 2, -pad.height / 2),
            (pad.width / 2, pad.height / 2),
            (-pad.width / 2, pad.height / 2),
        ]
    transformed: list[tuple[float, float]] = []
    for x, y in corners:
        local = _kicad_rotate((x, y * mirror_sign), pad_angle)
        board_point = _kicad_rotate(local, rotation_deg)
        transformed.append((board_point[0], -board_point[1]))
    min_x = min(point[0] for point in transformed)
    min_y = min(point[1] for point in transformed)
    max_x = max(point[0] for point in transformed)
    max_y = max(point[1] for point in transformed)
    center = _kicad_rotate((pad.x, pad.y * mirror_sign), rotation_deg)
    center = (center[0], -center[1])
    if pad.shape == "polygon":
        center = ((min_x + max_x) / 2, (min_y + max_y) / 2)
        area = abs(
            sum(
                first[0] * second[1] - second[0] * first[1]
                for first, second in zip(
                    transformed,
                    (*transformed[1:], transformed[0]),
                    strict=True,
                )
            )
            / 2
        )
    else:
        area = _land_pad_area(pad)
    if pad.pad_type == "thru_hole" and pad.drill is not None:
        area -= math.pi * pad.drill**2 / 4
    return _ExpectedExportFeature(
        x=center[0],
        y=center[1],
        width=max_x - min_x,
        height=max_y - min_y,
        area=area,
        pad=pad,
    )


def _compare_export_features(
    expected: list[_ExpectedExportFeature],
    actual: list[GerberFeature],
    *,
    label: str,
) -> str | None:
    unmatched = list(actual)
    errors: list[str] = []
    for wanted in expected:
        if not unmatched:
            errors.append(f"{label} is missing pad {wanted.pad.number}")
            continue
        nearest = min(
            unmatched,
            key=lambda item: math.hypot(item.x - wanted.x, item.y - wanted.y),
        )
        center_delta = math.hypot(nearest.x - wanted.x, nearest.y - wanted.y)
        if center_delta > 0.01:
            errors.append(f"{label} pad {wanted.pad.number} center delta {center_delta:.4f} mm")
            continue
        unmatched.remove(nearest)
        width_delta = abs(nearest.width - wanted.width)
        height_delta = abs(nearest.height - wanted.height)
        area_delta = abs(nearest.area - wanted.area) / wanted.area if wanted.area > 0 else math.inf
        if width_delta > 0.01 or height_delta > 0.01 or area_delta > 0.02:
            errors.append(
                f"{label} pad {wanted.pad.number} extents {nearest.width:.4f}x"
                f"{nearest.height:.4f} mm, area delta {area_delta:.2%}"
            )
    if unmatched:
        errors.append(f"{label} contains {len(unmatched)} unexpected feature(s)")
    return "; ".join(errors) if errors else None


def _gerber_layer(path: Path) -> str | None:
    normalized = path.name.casefold().replace(".", "_").replace("-", "_")
    for layer in ("F.Cu", "B.Cu", "F.Mask", "B.Mask", "F.Paste", "B.Paste"):
        if layer.casefold().replace(".", "_") in normalized:
            return layer
    return None


def _layer_pads(
    pads: list[LandPad],
    *,
    component_side: Literal["F.Cu", "B.Cu"],
    layer: str,
) -> list[LandPad]:
    if layer.endswith(".Cu"):
        return [
            pad
            for pad in pads
            if pad.pad_type == "thru_hole" or (pad.pad_type == "smd" and layer == component_side)
        ]
    if layer.endswith(".Mask"):
        return [
            pad
            for pad in pads
            if pad.pad_type in {"thru_hole", "np_thru_hole"}
            or (pad.pad_type == "smd" and layer.removesuffix(".Mask") + ".Cu" == component_side)
        ]
    return [
        pad
        for pad in pads
        if pad.pad_type == "smd" and layer.removesuffix(".Paste") + ".Cu" == component_side
    ]


def _layer_features(
    pads: list[LandPad],
    *,
    component_side: Literal["F.Cu", "B.Cu"],
    layer: str,
    rotation_deg: float,
) -> list[_ExpectedExportFeature]:
    return [
        _expected_export_feature(pad, side=component_side, rotation_deg=rotation_deg)
        for pad in _layer_pads(pads, component_side=component_side, layer=layer)
    ]


def _paste_mismatch(
    pads: list[LandPad],
    actual: list[GerberFeature],
    *,
    component_side: Literal["F.Cu", "B.Cu"],
    layer: Literal["F.Paste", "B.Paste"],
    rotation_deg: float,
    rules: EffectiveRules,
    exposed_numbers: set[str],
) -> str | None:
    active_layer = f"{component_side.removesuffix('.Cu')}.Paste"
    if layer != active_layer:
        return (
            f"{layer} contains {len(actual)} unexpected opening(s) for {component_side} placement"
            if actual
            else None
        )
    expected = _layer_features(
        pads,
        component_side=component_side,
        layer=layer,
        rotation_deg=rotation_deg,
    )
    remaining = list(actual)
    errors: list[str] = []
    for item in expected:
        if item.pad.number in exposed_numbers:
            candidates = [
                feature
                for feature in remaining
                if abs(feature.x - item.x) <= item.width / 2 + 0.01
                and abs(feature.y - item.y) <= item.height / 2 + 0.01
            ]
            area = sum(feature.area for feature in candidates)
            ratio = area / item.area if item.area > 0 else 0.0
            if (
                not candidates
                or not rules.paste.ep_coverage_min <= ratio <= rules.paste.ep_coverage_max
            ):
                errors.append(
                    f"EP paste coverage for pad {item.pad.number} is {ratio:.4f}, outside "
                    f"{rules.paste.ep_coverage_min:.4f}..{rules.paste.ep_coverage_max:.4f}"
                )
            for candidate in candidates:
                remaining.remove(candidate)
            continue
        if not remaining:
            errors.append(f"paste opening is missing pad {item.pad.number}")
            continue
        feature = min(
            remaining,
            key=lambda item2: math.hypot(item2.x - item.x, item2.y - item.y),
        )
        delta = math.hypot(feature.x - item.x, feature.y - item.y)
        ratio = feature.area / item.area if item.area > 0 else 0.0
        if delta > 0.01 or not rules.paste.coverage_min <= ratio <= rules.paste.coverage_max:
            errors.append(
                f"paste opening for pad {item.pad.number} center delta {delta:.4f} mm, "
                f"coverage {ratio:.4f}"
            )
        else:
            remaining.remove(feature)
    if remaining:
        errors.append(f"paste layer contains {len(remaining)} unexpected opening(s)")
    return "; ".join(errors) if errors else None


def _drill_file(path: Path) -> Literal["PTH", "NPTH"] | None:
    name = path.name.casefold()
    if "npth" in name:
        return "NPTH"
    if "pth" in name:
        return "PTH"
    return None


def _drill_expected(
    pads: list[LandPad],
    *,
    side: Literal["F.Cu", "B.Cu"],
    rotation_deg: float,
    pad_type: Literal["thru_hole", "np_thru_hole"],
) -> list[tuple[float, float, float, str]]:
    values: list[tuple[float, float, float, str]] = []
    for pad in pads:
        if pad.pad_type != pad_type or pad.drill is None:
            continue
        center = _kicad_rotate(
            (pad.x, pad.y * (-1.0 if side == "B.Cu" else 1.0)),
            rotation_deg,
        )
        values.append((round(center[0], 12), round(center[1], 12), pad.drill, pad.number))
    return values


def _compare_drills(
    expected: list[tuple[float, float, float, str]],
    actual: list[tuple[float, float, float]],
    *,
    label: str,
) -> str | None:
    unmatched = list(actual)
    errors: list[str] = []
    for x, y, diameter, number in expected:
        if not unmatched:
            errors.append(f"{label} drill is missing pad {number}")
            continue
        nearest = min(unmatched, key=lambda hit: math.hypot(hit[0] - x, hit[1] - y))
        distance = math.hypot(nearest[0] - x, nearest[1] - y)
        if distance > 0.01 or abs(nearest[2] - diameter) > 0.01:
            errors.append(
                f"{label} drill pad {number} position delta {distance:.4f} mm, "
                f"diameter {nearest[2]:.4f} mm expected {diameter:.4f} mm"
            )
        else:
            unmatched.remove(nearest)
    if unmatched:
        errors.append(f"{label} contains {len(unmatched)} unexpected drill hit(s)")
    return "; ".join(errors) if errors else None


def _parse_ipcd356(path: Path) -> list[tuple[str, str, str, float, float]]:
    records: list[tuple[str, str, str, float, float]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    net_aliases = {
        match.group(1): match.group(2)
        for line in lines
        if (match := _IPCD356_NET_ALIAS.fullmatch(line)) is not None
    }
    for line in lines:
        if not line.startswith("327"):
            continue
        parts = line.split()
        if len(parts) < 4:
            raise ValueError("malformed IPC-D-356 record")
        match = _IPCD356_COORDINATE.fullmatch(parts[3])
        if match is None:
            raise ValueError("IPC-D-356 record has malformed pad coordinates")
        records.append(
            (
                net_aliases.get(parts[0][3:], parts[0][3:]),
                parts[2].lstrip("-"),
                parts[1],
                int(match.group(1)) * 0.00254,
                int(match.group(2)) * 0.00254,
            )
        )
    return records


def _compare_pad_readback(
    footprint: FootprintDef,
    path: Path,
    number_to_name: dict[str, str],
) -> tuple[bool, str]:
    records = [item for item in _parse_ipcd356(path) if item[2] == _BOARD_REFERENCE]
    copper_pads = [
        pad
        for pad in footprint.pads
        if pad.number
        and pad.type != "np_thru_hole"
        and any(layer.endswith(".Cu") for layer in pad.layers)
    ]
    errors: list[str] = []
    if len(records) != len(copper_pads):
        errors.append(f"KiCad exported {len(records)} records for {len(copper_pads)} copper pads")
    remaining = list(records)
    for pad in copper_pads:
        net_name = number_to_name.get(pad.number)
        expected_token = _net_token(net_name) if net_name is not None else ""
        match_index = next(
            (
                index
                for index, record in enumerate(remaining)
                if record[1] == pad.number
                and abs(record[3] - pad.x) <= 0.01
                and abs(record[4] + pad.y) <= 0.01
            ),
            None,
        )
        if match_index is None:
            errors.append(f"pad {pad.number} at ({pad.x:.4f}, {pad.y:.4f}) was not read back")
            continue
        record = remaining.pop(match_index)
        if _net_token(record[0]) != expected_token:
            errors.append(f"pad {pad.number} net {record[0]!r} does not match {net_name!r}")
    return not errors, "; ".join(errors)


def _check_terminal_readback(
    spec: PartSpec,
    footprint: FootprintDef,
    path: Path,
) -> tuple[bool, str]:
    records = [item for item in _parse_ipcd356(path) if item[2] == _BOARD_REFERENCE]
    try:
        terminals = expected_terminals(spec)
    except Model3dError as error:
        if str(error) == "unsupported_family":
            return True, ""
        return False, str(error)
    errors: list[str] = []
    for terminal in terminals:
        contained = False
        for record in records:
            if record[1] != terminal.number:
                continue
            pad_center = (record[3], -record[4])
            pads = [
                pad
                for pad in footprint.pads
                if pad.number == terminal.number
                and math.hypot(pad.x - pad_center[0], pad.y - pad_center[1]) <= 0.01
            ]
            for pad in pads:
                angle = math.radians(pad.rotation)
                dx = terminal.center_xy[0] - pad_center[0]
                dy = terminal.center_xy[1] - pad_center[1]
                local_x = math.cos(angle) * dx + math.sin(angle) * dy
                local_y = -math.sin(angle) * dx + math.cos(angle) * dy
                if abs(local_x) <= pad.width / 2 + 1e-6 and abs(local_y) <= pad.height / 2 + 1e-6:
                    contained = True
                    break
            if contained:
                break
        if not contained:
            errors.append(
                f"PartSpec terminal {terminal.number} at "
                f"({terminal.center_xy[0]:.4f}, {terminal.center_xy[1]:.4f}) mm "
                "lies outside every corresponding IPC-D-356 pad"
            )
    return not errors, "; ".join(errors)


def _check_pinmap(
    spec: PartSpec,
    netlist_path: Path,
    expected_footprint: str,
) -> tuple[bool, str]:
    parsed = parse_netlist(netlist_path)
    errors: list[str] = []
    component = parsed.components.get(_BOARD_REFERENCE)
    if component is None:
        errors.append(f"netlist has no component {_BOARD_REFERENCE}")
    elif component.footprint != expected_footprint:
        errors.append(
            f"footprint field {component.footprint!r} does not resolve to {expected_footprint!r}"
        )
    occurrences: Counter[str] = Counter(
        pin for pins in parsed.nets.values() for ref, pin in pins if ref == _BOARD_REFERENCE
    )
    for pin in spec.pins:
        count = occurrences[pin.number]
        if count != 1:
            errors.append(f"pin {pin.number} appears {count} times in the netlist")
        actual_name = parsed.pin_functions.get(f"{_BOARD_REFERENCE}.{pin.number}")
        suffix = f"_{pin.number}"
        if actual_name is not None and actual_name.endswith(suffix):
            actual_name = actual_name[: -len(suffix)]
        if _pin_function_token(actual_name or "") != _pin_function_token(pin.name):
            errors.append(f"pin {pin.number} function {actual_name!r} does not match {pin.name!r}")
    return not errors, "; ".join(errors)


def _check_position_file(
    path: Path,
    *,
    footprint_name: str,
    attributes: list[str],
    pads: list[PadDef],
) -> tuple[bool, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    header = next((line[1:].split() for line in lines if line.startswith("# Ref")), None)
    if header is None or header[:3] != ["Ref", "Val", "Package"]:
        return False, "position export has no column header"
    row = next((line.split() for line in lines if line.startswith(_BOARD_REFERENCE)), None)
    if row is None:
        return False, f"{_BOARD_REFERENCE} is excluded from the position export"
    if len(row) < 3 or row[0] != _BOARD_REFERENCE or row[2] != footprint_name:
        return False, "position export footprint does not match the test-board footprint"
    copper = [pad for pad in pads if any(layer.endswith(".Cu") for layer in pad.layers)]
    has_through_hole = any(pad.type in {"thru_hole", "np_thru_hole"} for pad in copper)
    has_smd = any(pad.type == "smd" for pad in copper)
    expected_attribute = "through_hole" if has_through_hole else "smd" if has_smd else None
    if expected_attribute is None:
        return False, "footprint has no SMD or through-hole copper pads"
    normalized_attributes = {value.casefold() for value in attributes}
    if "exclude_from_pos_files" in normalized_attributes:
        return False, "footprint is marked exclude_from_pos_files"
    if expected_attribute not in normalized_attributes or {"smd", "through_hole"}.issubset(
        normalized_attributes
    ):
        return (
            False,
            f"footprint attribute does not match its {expected_attribute} pad types",
        )
    return True, f"position export contains {_BOARD_REFERENCE} as {expected_attribute}"


def _project_digest(project_dir: Path) -> str:
    entries: list[str] = []
    for path in sorted(item for item in project_dir.rglob("*") if item.is_file()):
        entries.append(
            f"{path.relative_to(project_dir).as_posix()}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        )
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def _unavailable_result(clearance: float, message: str) -> TestBoard:
    finding = TestBoardFinding(
        code="testboard_unavailable",
        severity="error",
        subject="kicad-cli",
        message=message,
    )
    return TestBoard(
        artifact_kind="circuit_test_board",
        verdict="fail",
        clearance_mm=clearance,
        project_sha256=hashlib.sha256(b"").hexdigest(),
        checks=[TestBoardCheck(name="kicad_cli_available", passed=False, details=message)],
        findings=[finding],
        erc_findings=[],
    )


def _manufacturing_export(
    project_root: Path,
    output_root: Path,
    *,
    spec: PartSpec,
    footprint_path: Path,
    footprint: FootprintDef,
    rules: EffectiveRules,
) -> tuple[list[TestBoardCheck], list[TestBoardFinding], list[Path]]:
    checks: list[TestBoardCheck] = []
    findings: list[TestBoardFinding] = []
    artifacts: list[Path] = []
    try:
        reference = compute_land_pattern(spec, rules=rules)
    except (ValueError, RuntimeError) as error:
        _add_result(
            checks,
            findings,
            name="manufacturing_export",
            passed=False,
            details=f"PartSpec land pattern cannot be derived: {error}",
            code="export_oracle_unparsed",
        )
        return checks, findings, artifacts
    if any(pad.pad_type == "unknown" for pad in reference.pads):
        _add_result(
            checks,
            findings,
            name="manufacturing_export",
            passed=False,
            details="PartSpec-derived manufacturing pad type is unknown",
            code="export_oracle_unparsed",
        )
        return checks, findings, artifacts

    setups: tuple[tuple[Literal["F.Cu", "B.Cu"], float], ...] = (
        ("F.Cu", 0.0),
        ("F.Cu", 90.0),
        ("F.Cu", 180.0),
        ("F.Cu", 270.0),
        ("B.Cu", 0.0),
        ("B.Cu", 90.0),
    )
    exposed_numbers = {pad.number for pad in spec.package.all_exposed_pads}
    output_root.mkdir(parents=True, exist_ok=True)
    for component_side, rotation_deg in setups:
        tag = f"{component_side.replace('.', '-')}-{int(rotation_deg)}"
        board_dir = project_root / f"manufacturing-{tag}"
        board_dir.mkdir(parents=True, exist_ok=True)
        board_path, _ = _write_board(
            board_dir,
            spec=spec,
            footprint_path=footprint_path,
            footprint=footprint,
            rules=rules,
            rotation_deg=rotation_deg,
            side=component_side,
        )
        gerber_dir = output_root / tag / "gerbers"
        drill_dir = output_root / tag / "drills"
        gerber_dir.mkdir(parents=True, exist_ok=True)
        drill_dir.mkdir(parents=True, exist_ok=True)
        gerber_run = kicad_cli.run(
            [
                "pcb",
                "export",
                "gerbers",
                "--layers",
                "F.Cu,B.Cu,F.Mask,B.Mask,F.Paste,B.Paste",
                "--output",
                str(gerber_dir) + "/",
                str(board_path),
            ]
        )
        if gerber_run.returncode:
            _add_result(
                checks,
                findings,
                name=f"manufacturing_gerber_{tag}",
                passed=False,
                details=gerber_run.stderr.strip() or "KiCad Gerber export failed",
                code="export_oracle_unparsed",
            )
            continue
        drill_run = kicad_cli.run(
            [
                "pcb",
                "export",
                "drill",
                "--excellon-units",
                "mm",
                "--drill-origin",
                "absolute",
                "--excellon-separate-th",
                "--output",
                str(drill_dir) + "/",
                str(board_path),
            ]
        )
        if drill_run.returncode:
            _add_result(
                checks,
                findings,
                name=f"manufacturing_drill_{tag}",
                passed=False,
                details=drill_run.stderr.strip() or "KiCad Excellon export failed",
                code="export_oracle_unparsed",
            )
            continue

        gerbers = {
            layer: path
            for path in gerber_dir.iterdir()
            if path.is_file()
            if (layer := _gerber_layer(path)) is not None
        }
        parsed: dict[str, list[GerberFeature]] = {
            layer: [] for layer in ("F.Cu", "B.Cu", "F.Mask", "B.Mask", "F.Paste", "B.Paste")
        }
        try:
            for layer, path in gerbers.items():
                parsed[layer] = list(parse_gerber(path).features)
                artifacts.append(path)
        except (ExportParseError, OSError, ValueError) as error:
            _add_result(
                checks,
                findings,
                name=f"manufacturing_gerber_{tag}",
                passed=False,
                details=str(error),
                code="export_oracle_unparsed",
            )
            continue

        export_errors: list[tuple[str, str]] = []
        for layer in ("F.Cu", "B.Cu"):
            expected = _layer_features(
                reference.pads,
                component_side=component_side,
                layer=layer,
                rotation_deg=rotation_deg,
            )
            if expected and layer not in gerbers:
                export_errors.append(
                    ("export_gerber_layer_mismatch", f"{layer} Gerber is missing expected pads")
                )
            mismatch = _compare_export_features(expected, parsed[layer], label=f"{layer} copper")
            if mismatch:
                code = "export_gerber_pad_mismatch" if expected else "export_gerber_layer_mismatch"
                export_errors.append((code, mismatch))

        for layer in ("F.Mask", "B.Mask"):
            expected = _layer_features(
                reference.pads,
                component_side=component_side,
                layer=layer,
                rotation_deg=rotation_deg,
            )
            if expected and layer not in gerbers:
                export_errors.append(
                    ("export_mask_mismatch", f"{layer} Gerber is missing expected openings")
                )
            mismatch = _compare_export_features(expected, parsed[layer], label=f"{layer} mask")
            if mismatch:
                export_errors.append(("export_mask_mismatch", mismatch))

        for layer in ("F.Paste", "B.Paste"):
            expected = _layer_features(
                reference.pads,
                component_side=component_side,
                layer=layer,
                rotation_deg=rotation_deg,
            )
            if expected and layer not in gerbers:
                export_errors.append(
                    ("export_paste_mismatch", f"{layer} Gerber is missing expected openings")
                )
            mismatch = _paste_mismatch(
                reference.pads,
                parsed[layer],
                component_side=component_side,
                layer=layer,
                rotation_deg=rotation_deg,
                rules=rules,
                exposed_numbers=exposed_numbers,
            )
            if mismatch:
                export_errors.append(("export_paste_mismatch", mismatch))

        drill_files = {
            file_type: path
            for path in drill_dir.iterdir()
            if path.is_file()
            if (file_type := _drill_file(path)) is not None
        }
        if any(
            path.suffix.casefold() == ".drl" and _drill_file(path) is None
            for path in drill_dir.iterdir()
        ):
            export_errors.append(("export_oracle_unparsed", "unrecognized Excellon file name"))
        for file_type, pad_type in (("PTH", "thru_hole"), ("NPTH", "np_thru_hole")):
            expected_drills = _drill_expected(
                reference.pads,
                side=component_side,
                rotation_deg=rotation_deg,
                pad_type=cast(Literal["thru_hole", "np_thru_hole"], pad_type),
            )
            drill_path = drill_files.get(file_type)
            if drill_path is None:
                if expected_drills:
                    export_errors.append(
                        ("export_drill_missing", f"{file_type} Excellon file is missing")
                    )
                continue
            try:
                parsed_drills = parse_excellon(drill_path)
                artifacts.append(drill_path)
            except (ExportParseError, OSError, ValueError) as error:
                export_errors.append(("export_oracle_unparsed", str(error)))
                continue
            mismatch = _compare_drills(
                expected_drills,
                [(hit.x, hit.y, hit.diameter) for hit in parsed_drills.hits],
                label=file_type,
            )
            if mismatch:
                export_errors.append(("export_drill_mismatch", mismatch))

        for code, message in export_errors:
            _add_result(
                checks,
                findings,
                name=f"manufacturing_{tag}_{code}",
                passed=False,
                details=message,
                code=code,
            )
        if not export_errors:
            checks.append(
                TestBoardCheck(
                    name=f"manufacturing_export_{tag}",
                    passed=True,
                    details="Gerber and Excellon geometry matches the PartSpec",
                )
            )
    return checks, findings, artifacts


def _error_code(error: Exception, fallback: str) -> str:
    message = str(error).casefold()
    if isinstance(error, kicad_cli.KicadCliError) and any(
        marker in message for marker in ("errno 2", "no such file", "not found", "not recognized")
    ):
        return "testboard_unavailable"
    return fallback


def build_test_board(
    spec: PartSpec,
    symbol_lib: Path,
    symbol_name: str,
    footprint_path: Path,
    rules: EffectiveRules,
    out_dir: Path,
) -> TestBoard:
    """Build a temporary project, run KiCad's oracles, and retain their outputs."""

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        version = kicad_cli.version()
    except kicad_cli.KicadCliError as error:
        message = str(error)
        unavailable = any(
            marker in message.casefold()
            for marker in ("errno 2", "no such file", "not found", "not recognized")
        )
        if unavailable:
            return _unavailable_result(rules.min_pad_clearance_mm, message)
        checks: list[TestBoardCheck] = []
        findings: list[TestBoardFinding] = []
        _add_result(
            checks,
            findings,
            name="kicad_cli_available",
            passed=False,
            details=message,
            code="testboard_kicad_cli",
        )
        return TestBoard(
            artifact_kind="circuit_test_board",
            verdict="fail",
            clearance_mm=rules.min_pad_clearance_mm,
            project_sha256=hashlib.sha256(b"").hexdigest(),
            checks=checks,
            findings=findings,
        )

    checks = [TestBoardCheck(name="kicad_cli_available", passed=True, details=version)]
    findings: list[TestBoardFinding] = []
    erc_findings: list[TestBoardFinding] = []
    try:
        symbol = parse_symbol(symbol_lib, symbol_name)
        footprint = parse_footprint(footprint_path)
        with tempfile.TemporaryDirectory(prefix="circuit-test-board-") as temporary:
            project_dir = Path(temporary)
            project_dir.mkdir(exist_ok=True)
            _write_local_tables(project_dir, symbol_lib, footprint_path)
            schematic_path = _write_schematic(
                project_dir,
                spec=spec,
                symbol_lib=symbol_lib,
                symbol_name=symbol_name,
                footprint_name=footprint.name,
                symbol=symbol,
            )
            board_path, number_to_name = _write_board(
                project_dir,
                spec=spec,
                footprint_path=footprint_path,
                footprint=footprint,
                rules=rules,
            )
            manufacturing_checks, manufacturing_findings, manufacturing_paths = (
                _manufacturing_export(
                    project_dir,
                    out_dir / "manufacturing",
                    spec=spec,
                    footprint_path=footprint_path,
                    footprint=footprint,
                    rules=rules,
                )
            )
            checks.extend(manufacturing_checks)
            findings.extend(manufacturing_findings)
            project_sha256 = _project_digest(project_dir)
            testboard_files: dict[str, Path | None] = {
                "netlist_path": None,
                "ipcd356_path": None,
                "position_path": None,
                "drc_report_path": None,
                "erc_report_path": None,
            }

            netlist_path = out_dir / "test-board.net"
            try:
                kicad_cli.export_netlist(schematic_path, netlist_path)
                passed, details = _check_pinmap(
                    spec,
                    netlist_path,
                    f"{_LIBRARY_ALIAS}:{footprint.name}",
                )
                _add_result(
                    checks,
                    findings,
                    name="testboard_pinmap",
                    passed=passed,
                    details=details,
                    code="testboard_pinmap",
                )
                testboard_files["netlist_path"] = netlist_path
            except (kicad_cli.KicadCliError, OSError, ValueError) as error:
                _add_result(
                    checks,
                    findings,
                    name="testboard_pinmap",
                    passed=False,
                    details=str(error),
                    code=_error_code(error, "testboard_pinmap"),
                )

            ipcd356_path = out_dir / "test-board.ipcd356"
            try:
                kicad_cli.export("ipcd356", board_path, out_dir)
                passed, details = _compare_pad_readback(
                    footprint,
                    ipcd356_path,
                    number_to_name,
                )
                _add_result(
                    checks,
                    findings,
                    name="testboard_pad_readback",
                    passed=passed,
                    details=details,
                    code="testboard_pad_readback",
                )
                terminal_passed, terminal_details = _check_terminal_readback(
                    spec,
                    footprint,
                    ipcd356_path,
                )
                _add_result(
                    checks,
                    findings,
                    name="testboard_terminal_containment",
                    passed=terminal_passed,
                    details=terminal_details,
                    code="testboard_terminal_outside_pad",
                )
                testboard_files["ipcd356_path"] = ipcd356_path
            except (kicad_cli.KicadCliError, OSError, ValueError) as error:
                _add_result(
                    checks,
                    findings,
                    name="testboard_pad_readback",
                    passed=False,
                    details=str(error),
                    code=_error_code(error, "testboard_pad_readback"),
                )

            position_dir = out_dir / "position"
            try:
                outputs = kicad_cli.export("pos", board_path, position_dir)
                position_path = next(
                    (path for path in outputs if path.name.endswith("-pos.csv")),
                    position_dir / f"{board_path.stem}-pos.csv",
                )
                passed, details = _check_position_file(
                    position_path,
                    footprint_name=footprint.name,
                    attributes=footprint.attributes,
                    pads=footprint.pads,
                )
                _add_result(
                    checks,
                    findings,
                    name="assembly_attribute",
                    passed=passed,
                    details=details,
                    code="assembly_attribute",
                )
                testboard_files["position_path"] = position_path
            except (kicad_cli.KicadCliError, OSError, ValueError) as error:
                _add_result(
                    checks,
                    findings,
                    name="assembly_attribute",
                    passed=False,
                    details=str(error),
                    code=_error_code(error, "assembly_attribute"),
                )

            assembly_findings = _paste_findings(spec, footprint, rules)
            for finding in assembly_findings:
                findings.append(finding)
                checks.append(
                    TestBoardCheck(
                        name=finding.code,
                        passed=finding.severity != "error",
                        details=finding.message,
                    )
                )
            if not any(
                item.code in {"paste_coverage", "ep_paste_coverage"} for item in assembly_findings
            ):
                checks.append(TestBoardCheck(name="paste_coverage", passed=True))
                if spec.package.exposed_pad is not None:
                    checks.append(TestBoardCheck(name="ep_paste_coverage", passed=True))

            drc_report_path = out_dir / "test-board.drc.json"
            try:
                report = kicad_cli.drc(board_path, drc_report_path)
                errors = [
                    item
                    for item in report.violations
                    if item.severity.casefold() == "error"
                    and item.type.casefold() != "unconnected_items"
                ]
                for violation in report.violations:
                    if violation.type.casefold() == "unconnected_items":
                        continue
                    if violation.severity.casefold() == "error":
                        code = f"testboard_drc_{violation.type}"
                        findings.append(
                            TestBoardFinding(
                                code=code,
                                severity="error",
                                subject=violation.type,
                                message=violation.description,
                            )
                        )
                    elif violation.severity.casefold() == "warning":
                        findings.append(
                            TestBoardFinding(
                                code=f"testboard_drc_{violation.type}",
                                severity="warning",
                                subject=violation.type,
                                message=violation.description,
                            )
                        )
                _add_result(
                    checks,
                    findings,
                    name="testboard_drc",
                    passed=not errors,
                    details="; ".join(item.description for item in errors),
                    code="testboard_drc",
                )
                testboard_files["drc_report_path"] = drc_report_path
            except (kicad_cli.KicadCliError, OSError, ValueError) as error:
                _add_result(
                    checks,
                    findings,
                    name="testboard_drc",
                    passed=False,
                    details=str(error),
                    code=_error_code(error, "testboard_drc"),
                )

            erc_report_path = out_dir / "test-board.erc.json"
            try:
                report = kicad_cli.erc(schematic_path, erc_report_path)
                for violation in report.violations:
                    issue = TestBoardFinding(
                        code=f"testboard_erc_{violation.type}",
                        severity="warning",
                        subject=violation.type,
                        message=violation.description,
                    )
                    erc_findings.append(issue)
                    findings.append(issue)
                checks.append(
                    TestBoardCheck(
                        name="testboard_erc",
                        passed=True,
                        details=f"{len(report.violations)} warning(s)",
                    )
                )
                testboard_files["erc_report_path"] = erc_report_path
            except (kicad_cli.KicadCliError, OSError, ValueError) as error:
                _add_result(
                    checks,
                    findings,
                    name="testboard_erc",
                    passed=False,
                    details=str(error),
                    code=_error_code(error, "testboard_erc"),
                )

            if not any(item.code == "mask_web" for item in assembly_findings):
                checks.append(TestBoardCheck(name="mask_web", passed=True))

            return TestBoard(
                artifact_kind="circuit_test_board",
                verdict="fail" if any(item.severity == "error" for item in findings) else "pass",
                clearance_mm=rules.min_pad_clearance_mm,
                project_sha256=project_sha256,
                checks=checks,
                findings=findings,
                erc_findings=erc_findings,
                manufacturing_export_paths=manufacturing_paths,
                **testboard_files,
            )
    except (OSError, ValueError, sexpr.SExprError) as error:
        _add_result(
            checks,
            findings,
            name="testboard_setup",
            passed=False,
            details=str(error),
            code="testboard_setup",
        )
        return TestBoard(
            artifact_kind="circuit_test_board",
            verdict="fail",
            clearance_mm=rules.min_pad_clearance_mm,
            project_sha256=hashlib.sha256(b"").hexdigest(),
            checks=checks,
            findings=findings,
            erc_findings=erc_findings,
        )
