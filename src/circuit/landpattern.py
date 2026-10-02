"""IPC-7351B land-pattern calculations for supported surface-mount packages."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from .partspec import Dimension, LandPad, PartSpec

if TYPE_CHECKING:
    from .ruleprofile import EffectiveRules

Density = Literal["most", "nominal", "least"]
Family = Literal[
    "no_lead_quad",
    "no_lead_dual",
    "gullwing_quad",
    "gullwing_dual",
    "chip",
]
Side = Literal["left", "bottom", "right", "top"]


class LandPatternError(ValueError):
    """Raised when a package cannot produce a safe supported land pattern."""


class Rect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x0: float
    y0: float
    x1: float
    y1: float


class LandPatternResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family: Family
    density: Density
    source: Literal["datasheet", "ipc7351b"]
    params: dict[str, float]
    pads: list[LandPad]
    courtyard: tuple[float, float, float, float]
    konnect_pads: list[dict[str, object]]
    rule_chain: list[str] = Field(default_factory=list)
    rule_chain_sha256: str | None = None


@dataclass(frozen=True)
class _Placement:
    number: str
    side: Side
    position: float


@dataclass(frozen=True)
class _Goals:
    toe: float
    heel: float
    side: float
    courtyard: float


def _round_hundredth(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _outward_hundredth(value: float, rounding: str) -> float:
    mode = ROUND_FLOOR if rounding == "floor" else ROUND_CEILING
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=mode))


def _bounds(dimension: Dimension, *, field: str) -> tuple[float, float]:
    minimum = dimension.min
    nominal = dimension.nom
    maximum = dimension.max
    if minimum is None and nominal is None and maximum is None:
        raise LandPatternError(f"{field} has no usable value")
    if minimum is None:
        minimum = nominal if nominal is not None else maximum
    if maximum is None:
        maximum = nominal if nominal is not None else minimum
    if minimum is None or maximum is None:
        raise LandPatternError(f"{field} has no usable value")
    if minimum <= 0 or maximum <= 0:
        raise LandPatternError(f"{field} must be positive")
    return float(minimum), float(maximum)


def _nominal(dimension: Dimension, *, field: str) -> float:
    minimum, maximum = _bounds(dimension, field=field)
    return float(dimension.nom) if dimension.nom is not None else (minimum + maximum) / 2


def _family(spec: PartSpec) -> Family:
    family = spec.package.family
    if family not in (
        "no_lead_quad",
        "no_lead_dual",
        "gullwing_quad",
        "gullwing_dual",
        "chip",
    ):
        raise LandPatternError(f"unsupported package family: {family}")
    return family


def _required_dimension(spec: PartSpec, name: str) -> Dimension:
    dimensions: dict[str, Dimension | None] = {
        "pitch": spec.package.pitch,
        "lead_span": spec.package.lead_span,
        "lead_length": spec.package.lead_length,
        "lead_width": spec.package.lead_width,
    }
    value = dimensions.get(name)
    if value is None:
        raise LandPatternError(f"package.{name} is required")
    return value


def _goals(spec: PartSpec, density: Density) -> _Goals:
    family = _family(spec)
    if family.startswith("gullwing"):
        pitch = _required_dimension(spec, "pitch")
        pitch_nominal = _nominal(pitch, field="package.pitch")
        key = "small" if pitch_nominal <= 0.625 else "large"
        table: dict[str, dict[Density, tuple[float, float, float, float]]] = {
            "small": {
                "most": (0.55, 0.45, 0.01, 0.50),
                "nominal": (0.35, 0.35, -0.02, 0.25),
                "least": (0.15, 0.25, -0.04, 0.10),
            },
            "large": {
                "most": (0.55, 0.45, 0.05, 0.50),
                "nominal": (0.35, 0.35, 0.03, 0.25),
                "least": (0.15, 0.25, 0.01, 0.10),
            },
        }
        toe, heel, side, courtyard = table[key][density]
    elif family.startswith("no_lead"):
        values = {
            "most": (0.40, 0.00, -0.04, 0.50),
            "nominal": (0.30, 0.00, -0.04, 0.25),
            "least": (0.20, 0.00, -0.04, 0.10),
        }
        toe, heel, side, courtyard = values[density]
    else:
        body_length = _nominal(spec.package.body_length, field="package.body_length")
        if body_length >= 1.608:
            values = {
                "most": (0.55, 0.00, 0.05, 0.50),
                "nominal": (0.35, 0.00, 0.00, 0.25),
                "least": (0.15, 0.00, -0.05, 0.10),
            }
        else:
            values = {
                "most": (0.30, 0.00, 0.05, 0.20),
                "nominal": (0.20, 0.00, 0.00, 0.15),
                "least": (0.10, 0.00, -0.05, 0.10),
            }
        toe, heel, side, courtyard = values[density]
    return _Goals(toe=toe, heel=heel, side=side, courtyard=courtyard)


def _effective_goals(spec: PartSpec, density: Density, rules: EffectiveRules | None) -> _Goals:
    goals = _goals(spec, density)
    if rules is None:
        return goals
    override = rules.goal_overrides.get(_family(spec))
    if override is None:
        return goals
    return _Goals(
        toe=goals.toe if override.toe is None else override.toe,
        heel=goals.heel if override.heel is None else override.heel,
        side=goals.side if override.side is None else override.side,
        courtyard=(goals.courtyard if override.courtyard is None else override.courtyard),
    )


def _pitch(spec: PartSpec) -> float:
    dimension = _required_dimension(spec, "pitch")
    pitch = _nominal(dimension, field="package.pitch")
    if pitch <= 0:
        raise LandPatternError("package.pitch must be positive")
    return pitch


def _quad_counts(spec: PartSpec) -> tuple[int, int, int, int]:
    package = spec.package
    counts = package.pins_per_side
    if counts is None:
        if package.pin_count % 4:
            raise LandPatternError("quad pin_count must divide evenly across four sides")
        each = package.pin_count // 4
        counts = (each, each, each, each)
    if any(count <= 0 for count in counts) or sum(counts) != package.pin_count:
        raise LandPatternError("pins_per_side must contain positive counts summing to pin_count")
    return counts


def _axis_positions(count: int, pitch: float, *, reverse: bool = False) -> list[float]:
    positions = [(index - (count - 1) / 2) * pitch for index in range(count)]
    return list(reversed(positions)) if reverse else positions


def _placements(spec: PartSpec) -> list[_Placement]:
    package = spec.package
    family = _family(spec)
    if family == "chip":
        if package.pin_count != 2:
            raise LandPatternError("chip packages must have exactly two pins")
        return [
            _Placement("1", "left", 0.0),
            _Placement("2", "right", 0.0),
        ]
    pitch = _pitch(spec)
    placements: list[_Placement] = []
    if family.endswith("dual"):
        if package.pin_count % 2:
            raise LandPatternError("dual package pin_count must be even")
        half = package.pin_count // 2
        for index, position in enumerate(_axis_positions(half, pitch)):
            placements.append(_Placement(str(index + 1), "left", position))
        for index, position in enumerate(_axis_positions(half, pitch, reverse=True)):
            placements.append(_Placement(str(half + index + 1), "right", position))
        return placements
    counts = _quad_counts(spec)
    number = 1
    rows: tuple[tuple[Side, int, bool], ...] = (
        ("left", counts[0], False),
        ("bottom", counts[1], False),
        ("right", counts[2], True),
        ("top", counts[3], True),
    )
    for side, count, reverse in rows:
        for position in _axis_positions(count, pitch, reverse=reverse):
            placements.append(_Placement(str(number), side, position))
            number += 1
    return placements


def standard_pin_placements(spec: PartSpec) -> list[tuple[str, Side, float]]:
    """Return top-left-anchored counter-clockwise pin placements."""

    return [
        (placement.number, placement.side, placement.position) for placement in _placements(spec)
    ]


def _metrics(
    length: tuple[float, float],
    terminal_length: tuple[float, float],
    width: tuple[float, float],
    goals: _Goals,
    fabrication_tolerance: float,
    placement_tolerance: float,
) -> dict[str, float]:
    l_min, l_max = length
    t_min, t_max = terminal_length
    w_min, w_max = width
    cl = l_max - l_min
    ct = t_max - t_min
    cw = w_max - w_min
    s_min = l_min - 2 * t_max
    s_max = l_max - 2 * t_min
    s_tol = s_max - s_min
    s_tol_rms = math.sqrt(cl**2 + 2 * ct**2)
    s_max_rms = s_max - (s_tol - s_tol_rms) / 2
    z_max = (
        l_min + 2 * goals.toe + math.sqrt(cl**2 + fabrication_tolerance**2 + placement_tolerance**2)
    )
    g_min = (
        s_max_rms
        - 2 * goals.heel
        - math.sqrt(s_tol_rms**2 + fabrication_tolerance**2 + placement_tolerance**2)
    )
    x_max = (
        w_min
        + 2 * goals.side
        + math.sqrt(cw**2 + fabrication_tolerance**2 + placement_tolerance**2)
    )
    pad_length = (z_max - g_min) / 2
    offset = (z_max + g_min) / 4
    if pad_length <= 0 or x_max <= 0:
        raise LandPatternError("computed IPC pad geometry is not positive")
    return {
        "Lmin": l_min,
        "Lmax": l_max,
        "Tmin": t_min,
        "Tmax": t_max,
        "Wmin": w_min,
        "Wmax": w_max,
        "Cl": cl,
        "Ct": ct,
        "Cw": cw,
        "Smin": s_min,
        "Smax": s_max,
        "Stol": s_tol,
        "StolRMS": s_tol_rms,
        "SmaxRMS": s_max_rms,
        "Zmax": z_max,
        "Gmin": g_min,
        "Xmax": x_max,
        "pad_length": pad_length,
        "offset": offset,
        "F": fabrication_tolerance,
        "P": placement_tolerance,
        "toe": goals.toe,
        "heel": goals.heel,
        "side": goals.side,
        "courtyard_excess": goals.courtyard,
    }


def _round_pad(
    number: str,
    x: float,
    y: float,
    width: float,
    height: float,
) -> LandPad:
    return LandPad(
        number=number,
        x=_round_hundredth(x),
        y=_round_hundredth(y),
        width=_round_hundredth(width),
        height=_round_hundredth(height),
        shape="roundrect",
    )


def _body_box(spec: PartSpec) -> tuple[float, float, float, float]:
    body_width = _nominal(spec.package.body_width, field="package.body_width")
    body_length = _nominal(spec.package.body_length, field="package.body_length")
    return (-body_width / 2, -body_length / 2, body_width / 2, body_length / 2)


def _courtyard(
    spec: PartSpec, pads: list[LandPad], excess: float
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = _body_box(spec)
    for pad in pads:
        x0 = min(x0, pad.x - pad.width / 2)
        y0 = min(y0, pad.y - pad.height / 2)
        x1 = max(x1, pad.x + pad.width / 2)
        y1 = max(y1, pad.y + pad.height / 2)
    return (
        _outward_hundredth(x0 - excess, "floor"),
        _outward_hundredth(y0 - excess, "floor"),
        _outward_hundredth(x1 + excess, "ceil"),
        _outward_hundredth(y1 + excess, "ceil"),
    )


def _konnect_pads(pads: list[LandPad], vertical: set[str]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for pad in pads:
        is_vertical = pad.number in vertical
        konnect_pad: dict[str, object] = {
            "number": pad.number,
            "type": "smd",
            "shape": pad.shape,
            "x": pad.x,
            "y": pad.y,
            "width": pad.height if is_vertical else pad.width,
            "height": pad.width if is_vertical else pad.height,
            "rotation": 90.0 if is_vertical else 0.0,
            "layers": ["F.Cu", "F.Paste", "F.Mask"],
        }
        if pad.shape == "roundrect":
            konnect_pad["roundrect_rratio"] = 0.25
        result.append(konnect_pad)
    return result


def _datasheet_pattern(
    spec: PartSpec,
    density: Density,
    fabrication_tolerance: float,
    placement_tolerance: float,
    rules: EffectiveRules | None,
) -> LandPatternResult:
    if spec.land_pattern is None:
        raise LandPatternError("datasheet land-pattern is absent")
    goals = _effective_goals(spec, density, rules)
    pads = list(spec.land_pattern.pads)
    return LandPatternResult(
        family=_family(spec),
        density=density,
        source="datasheet",
        params={
            "F": fabrication_tolerance,
            "P": placement_tolerance,
            "toe": goals.toe,
            "heel": goals.heel,
            "side": goals.side,
            "courtyard_excess": goals.courtyard,
        },
        pads=pads,
        courtyard=_courtyard(spec, pads, goals.courtyard),
        konnect_pads=_konnect_pads(pads, set()),
        rule_chain=[] if rules is None else rules.chain,
        rule_chain_sha256=None if rules is None else rules.chain_sha256,
    )


def compute_land_pattern(
    spec: PartSpec,
    density: Density | None = None,
    *,
    rules: EffectiveRules | None = None,
    fabrication_tolerance: float | None = None,
    placement_tolerance: float | None = None,
) -> LandPatternResult:
    """Compute supported IPC-7351B pads in KiCad top-view coordinates."""

    density = (
        density if density is not None else (rules.density if rules is not None else "nominal")
    )
    fabrication_tolerance = (
        fabrication_tolerance
        if fabrication_tolerance is not None
        else (rules.fabrication_tolerance if rules is not None else 0.05)
    )
    placement_tolerance = (
        placement_tolerance
        if placement_tolerance is not None
        else (rules.placement_tolerance if rules is not None else 0.025)
    )
    if density not in ("most", "nominal", "least"):
        raise LandPatternError(f"unsupported density level: {density}")
    if (
        not math.isfinite(fabrication_tolerance)
        or not math.isfinite(placement_tolerance)
        or fabrication_tolerance < 0
        or placement_tolerance < 0
    ):
        raise LandPatternError("tolerances must be finite and non-negative")
    family = _family(spec)
    if spec.land_pattern is not None and spec.land_pattern.source == "datasheet":
        return _datasheet_pattern(
            spec,
            density,
            fabrication_tolerance,
            placement_tolerance,
            rules,
        )

    goals = _effective_goals(spec, density, rules)
    placements = _placements(spec)
    terminal_length = _bounds(_required_dimension(spec, "lead_length"), field="package.lead_length")
    if family == "chip":
        width = _bounds(spec.package.body_width, field="package.body_width")
        length = _bounds(spec.package.body_length, field="package.body_length")
        metrics = _metrics(
            length,
            terminal_length,
            width,
            goals,
            fabrication_tolerance,
            placement_tolerance,
        )
        params = dict(metrics)
        pads: list[LandPad] = []
        for placement in placements:
            x = -metrics["offset"] if placement.side == "left" else metrics["offset"]
            pads.append(
                _round_pad(
                    placement.number,
                    x,
                    0.0,
                    metrics["pad_length"],
                    metrics["Xmax"],
                )
            )
        vertical: set[str] = set()
    else:
        lead_width = _bounds(_required_dimension(spec, "lead_width"), field="package.lead_width")
        if family.startswith("gullwing"):
            shared_length = _bounds(
                _required_dimension(spec, "lead_span"), field="package.lead_span"
            )
            row_dimensions = {
                "left": shared_length,
                "right": shared_length,
                "bottom": shared_length,
                "top": shared_length,
            }
        else:
            row_dimensions = {
                "left": _bounds(spec.package.body_width, field="package.body_width"),
                "right": _bounds(spec.package.body_width, field="package.body_width"),
                "bottom": _bounds(spec.package.body_length, field="package.body_length"),
                "top": _bounds(spec.package.body_length, field="package.body_length"),
            }
        row_metrics: dict[Side, dict[str, float]] = {}
        row_sides: tuple[Side, ...] = ("left", "right", "bottom", "top")
        for side in row_sides:
            row_metrics[side] = _metrics(
                row_dimensions[side],
                terminal_length,
                lead_width,
                goals,
                fabrication_tolerance,
                placement_tolerance,
            )
        params = {
            "F": fabrication_tolerance,
            "P": placement_tolerance,
            "toe": goals.toe,
            "heel": goals.heel,
            "side": goals.side,
            "courtyard_excess": goals.courtyard,
        }
        if family.endswith("dual"):
            params.update(row_metrics["left"])
        else:
            params.update(
                {
                    f"{side}.{key}": value
                    for side, values in row_metrics.items()
                    for key, value in values.items()
                }
            )
        pads = []
        vertical = set()
        for placement in placements:
            metrics = row_metrics[placement.side]
            if placement.side in ("left", "right"):
                sign = -1 if placement.side == "left" else 1
                x, y = sign * metrics["offset"], placement.position
                width, height = metrics["pad_length"], metrics["Xmax"]
            else:
                sign = -1 if placement.side == "top" else 1
                x, y = placement.position, sign * metrics["offset"]
                width, height = metrics["Xmax"], metrics["pad_length"]
                vertical.add(placement.number)
            pads.append(_round_pad(placement.number, x, y, width, height))

    if spec.package.exposed_pad is not None:
        exposed = spec.package.exposed_pad
        ep_width = _nominal(exposed.width, field="package.exposed_pad.width")
        ep_height = _nominal(exposed.length, field="package.exposed_pad.length")
        pads.append(_round_pad(exposed.number, 0.0, 0.0, ep_width, ep_height))
    return LandPatternResult(
        family=family,
        density=density,
        source="ipc7351b",
        params=params,
        pads=pads,
        courtyard=_courtyard(spec, pads, goals.courtyard),
        konnect_pads=_konnect_pads(pads, vertical),
        rule_chain=[] if rules is None else rules.chain,
        rule_chain_sha256=None if rules is None else rules.chain_sha256,
    )


def _max_value(dimension: Dimension, *, field: str) -> float:
    return _bounds(dimension, field=field)[1]


def _max_material_leads(spec: PartSpec) -> tuple[float, float, float]:
    family = _family(spec)
    terminal_length = _max_value(
        _required_dimension(spec, "lead_length"), field="package.lead_length"
    )
    if family == "chip":
        terminal_width = _max_value(spec.package.body_width, field="package.body_width")
        length = _max_value(spec.package.body_length, field="package.body_length")
    else:
        terminal_width = _max_value(
            _required_dimension(spec, "lead_width"), field="package.lead_width"
        )
        if family.startswith("gullwing"):
            length = _max_value(_required_dimension(spec, "lead_span"), field="package.lead_span")
        else:
            length = 0.0
    return length, terminal_length, terminal_width


def lead_rects(spec: PartSpec) -> dict[str, list[Rect]]:
    """Return maximum-material package lead rectangles in top-view coordinates."""

    family = _family(spec)
    placements = _placements(spec)
    span, terminal_length, terminal_width = _max_material_leads(spec)
    body_width = _max_value(spec.package.body_width, field="package.body_width")
    body_length = _max_value(spec.package.body_length, field="package.body_length")
    rectangles: dict[str, list[Rect]] = {}
    for placement in placements:
        if family == "chip":
            center_x = (-1 if placement.side == "left" else 1) * (
                body_length / 2 + terminal_length / 2
            )
            x0, x1 = center_x - terminal_length / 2, center_x + terminal_length / 2
            y0, y1 = -terminal_width / 2, terminal_width / 2
        elif placement.side in ("left", "right"):
            sign = -1 if placement.side == "left" else 1
            if family.startswith("gullwing"):
                center_x = sign * (span - terminal_length) / 2
            else:
                center_x = sign * (body_width / 2 - terminal_length / 2)
            x0, x1 = center_x - terminal_length / 2, center_x + terminal_length / 2
            y0, y1 = (
                placement.position - terminal_width / 2,
                placement.position + terminal_width / 2,
            )
        else:
            sign = -1 if placement.side == "top" else 1
            if family.startswith("gullwing"):
                center_y = sign * (span - terminal_length) / 2
            else:
                center_y = sign * (body_length / 2 - terminal_length / 2)
            x0, x1 = (
                placement.position - terminal_width / 2,
                placement.position + terminal_width / 2,
            )
            y0, y1 = center_y - terminal_length / 2, center_y + terminal_length / 2
        rectangles.setdefault(placement.number, []).append(Rect(x0=x0, y0=y0, x1=x1, y1=y1))
    return rectangles
