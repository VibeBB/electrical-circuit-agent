"""Deterministic readability lint for .kicad_sch files."""

from __future__ import annotations

import argparse
import itertools
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import sexpr


class SchLintError(ValueError):
    """Raised when a schematic cannot be linted."""


class SchLintFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    severity: Literal["error", "warning"]
    description: str
    items: list[str] = Field(default_factory=list)


class SchLintReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["sch_lint"] = "sch_lint"
    source: Path
    verdict: Literal["pass", "fail"]
    errors: int
    warnings: int
    symbols_checked: int
    findings: list[SchLintFinding] = Field(default_factory=lambda: list[SchLintFinding]())


_PAPER_SIZES: dict[str, tuple[float, float]] = {
    "A0": (1189.0, 841.0),
    "A1": (841.0, 594.0),
    "A2": (594.0, 420.0),
    "A3": (420.0, 297.0),
    "A4": (297.0, 210.0),
    "A5": (210.0, 148.0),
}
_LABEL_DISTANCE_MM = 30.0
_PROPERTY_ON_BODY_MM = 2.5
_LABELED_PROPERTIES = {"Reference", "Value"}
_TITLE_BLOCK_FIELDS = ("title", "date", "rev")
_MIN_SHEET_USAGE = 0.30
# Tolerance for an endpoint/anchor sitting on a wire segment.
_ON_WIRE_EPS_MM = 0.1
# A label this close to a symbol body is assumed to sit on a pin stub.
_LABEL_NEAR_SYMBOL_MM = 20.0
_WIRE_ITEMS = {"wire", "bus"}
_LABEL_ITEMS = {"label", "global_label", "hierarchical_label"}
_POSITIONED_ITEMS = {
    "label",
    "global_label",
    "hierarchical_label",
    "text",
    "text_box",
    "image",
    "junction",
    "no_connect",
    "sheet",
}


def _find_children(node: list[sexpr.SExpr], name: str) -> list[list[sexpr.SExpr]]:
    return [child for child in node[1:] if isinstance(child, list) and child and child[0] == name]


def _first_child(node: list[sexpr.SExpr], name: str) -> list[sexpr.SExpr] | None:
    for child in node[1:]:
        if isinstance(child, list) and child and child[0] == name:
            return child
    return None


def _position(node: list[sexpr.SExpr]) -> tuple[float, float] | None:
    for child in node[1:]:
        if (
            isinstance(child, list)
            and child
            and child[0] == "at"
            and len(child) >= 3
            and isinstance(child[1], str)
            and isinstance(child[2], str)
        ):
            try:
                return float(child[1]), float(child[2])
            except ValueError:
                return None
    return None


def _has_flag(node: sexpr.SExpr, flag: str) -> bool:
    if isinstance(node, str):
        return node == flag
    return any(_has_flag(child, flag) for child in node[1:])


def _paper_size(root: list[sexpr.SExpr]) -> tuple[float, float]:
    paper = _first_child(root, "paper")
    if paper is None or len(paper) < 2 or not isinstance(paper[1], str):
        return _PAPER_SIZES["A4"]
    width, height = _PAPER_SIZES.get(paper[1].upper(), _PAPER_SIZES["A4"])
    if any(child == "portrait" for child in paper[2:]):
        return height, width
    return width, height


def _wire_points(node: list[sexpr.SExpr]) -> list[tuple[float, float]]:
    pts = _first_child(node, "pts")
    if pts is None:
        return []
    points: list[tuple[float, float]] = []
    for xy in _find_children(pts, "xy"):
        if len(xy) >= 3 and isinstance(xy[1], str) and isinstance(xy[2], str):
            try:
                points.append((float(xy[1]), float(xy[2])))
            except ValueError:
                continue
    return points


def _point_segment_distance(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> tuple[float, float]:
    """Return ``(distance, t)`` of point ``p`` to segment ``(x1,y1)-(x2,y2)``.

    ``t`` is the normalized projection of ``p`` onto the segment direction,
    unclamped — ``0 < t < 1`` means the projection is strictly interior.
    A degenerate (zero-length) segment returns ``(inf, 0)``.
    """
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return float("inf"), 0.0
    t = ((px - x1) * dx + (py - y1) * dy) / length_sq
    cx, cy = x1 + min(max(t, 0.0), 1.0) * dx, y1 + min(max(t, 0.0), 1.0) * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5, t


def lint_schematic(path: Path) -> SchLintReport:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SchLintError(f"could not read schematic {path}: {exc}") from exc
    try:
        root = sexpr.parse_text(text)
    except sexpr.SExprError as exc:
        raise SchLintError(f"could not parse schematic {path}: {exc}") from exc
    if not root or root[0] != "kicad_sch":
        raise SchLintError(f"not a kicad_sch file: {path}")

    findings: list[SchLintFinding] = []
    page_width, page_height = _paper_size(root)
    symbols = 0
    non_power_symbols = 0
    wires = 0
    labels = 0
    item_positions: list[tuple[float, float]] = []
    segments: list[tuple[float, float, float, float]] = []
    wire_endpoints: list[tuple[float, float]] = []
    junction_points: set[tuple[float, float]] = set()
    net_labels: list[tuple[str, float, float]] = []
    symbol_centers: list[tuple[float, float]] = []
    has_text_notes = False
    for node in root[1:]:
        if not isinstance(node, list) or not node or not isinstance(node[0], str):
            continue
        if node[0] in _WIRE_ITEMS:
            wires += 1
            points = _wire_points(node)
            for a, b in itertools.pairwise(points):
                segments.append((a[0], a[1], b[0], b[1]))
            if points:
                wire_endpoints.extend((points[0], points[-1]))
        if node[0] in _LABEL_ITEMS:
            labels += 1
            if node[0] != "hierarchical_label":
                label_at = _position(node)
                if label_at is not None:
                    name = node[1] if len(node) > 1 and isinstance(node[1], str) else "?"
                    net_labels.append((name, label_at[0], label_at[1]))
        if node[0] == "junction":
            junction_at = _position(node)
            if junction_at is not None:
                junction_points.add(junction_at)
        if node[0] in ("text", "text_box"):
            has_text_notes = True
        if node[0] != "symbol" and node[0] not in _POSITIONED_ITEMS:
            continue
        position = _position(node)
        if position is None:
            continue
        px, py = position
        item_positions.append(position)
        if not (0.0 <= px <= page_width and 0.0 <= py <= page_height):
            findings.append(
                SchLintFinding(
                    type="item_out_of_bounds",
                    severity="error",
                    description=(
                        f"{node[0]} at ({px:.2f},{py:.2f}) is outside the "
                        f"{page_width:.0f}x{page_height:.0f}mm sheet"
                    ),
                )
            )
        if node[0] != "symbol":
            continue
        symbols += 1
        symbol_centers.append(position)
        lib_id = _first_child(node, "lib_id")
        if not (
            lib_id is not None
            and len(lib_id) > 1
            and isinstance(lib_id[1], str)
            and lib_id[1].startswith("power:")
        ):
            non_power_symbols += 1
        uuid = next(
            (
                child[1]
                for child in _find_children(node, "uuid")
                if len(child) == 2 and isinstance(child[1], str)
            ),
            "?",
        )
        for prop in _find_children(node, "property"):
            if len(prop) < 3 or not isinstance(prop[1], str) or prop[1] not in _LABELED_PROPERTIES:
                continue
            name = prop[1]
            prop_position = _position(prop)
            if prop_position is None:
                findings.append(
                    SchLintFinding(
                        type="property_missing_position",
                        severity="error",
                        description=f"{name} property has no usable position",
                        items=[f"symbol uuid={uuid}"],
                    )
                )
                continue
            lx, ly = prop_position
            distance = max(abs(lx - px), abs(ly - py))
            if distance > _LABEL_DISTANCE_MM:
                findings.append(
                    SchLintFinding(
                        type="property_far_from_symbol",
                        severity="error",
                        description=(
                            f"{name} property at ({lx:.2f},{ly:.2f}) is "
                            f"{distance:.1f}mm from its symbol at ({px:.2f},{py:.2f})"
                        ),
                        items=[f"symbol uuid={uuid} {name}={prop[2]!r}"],
                    )
                )
            body_distance = ((lx - px) ** 2 + (ly - py) ** 2) ** 0.5
            if body_distance < _PROPERTY_ON_BODY_MM:
                findings.append(
                    SchLintFinding(
                        type="property_on_symbol",
                        severity="warning",
                        description=(
                            f"{name} property at ({lx:.2f},{ly:.2f}) sits on the "
                            f"symbol body at ({px:.2f},{py:.2f}) and may be unreadable"
                        ),
                        items=[f"symbol uuid={uuid} {name}={prop[2]!r}"],
                    )
                )
            if _has_flag(prop, "hide"):
                findings.append(
                    SchLintFinding(
                        type="property_hidden",
                        severity="warning",
                        description=f"{name} property {prop[2]!r} is hidden",
                        items=[f"symbol uuid={uuid}"],
                    )
                )

    title_block = _first_child(root, "title_block")
    missing_fields: list[str] = []
    for field_name in _TITLE_BLOCK_FIELDS:
        field = _first_child(title_block, field_name) if title_block is not None else None
        if field is None or len(field) < 2 or not isinstance(field[1], str) or not field[1].strip():
            missing_fields.append(field_name)
    if missing_fields:
        findings.append(
            SchLintFinding(
                type="title_block_incomplete",
                severity="warning",
                description="title block fields are empty or missing: " + ", ".join(missing_fields),
            )
        )
    has_comments = title_block is not None and any(
        len(comment) >= 3 and isinstance(comment[2], str) and comment[2].strip()
        for comment in _find_children(title_block, "comment")
    )
    if non_power_symbols >= 1 and not has_text_notes and not has_comments:
        findings.append(
            SchLintFinding(
                type="notes_absent",
                severity="warning",
                description=(
                    "schematic carries no text notes or title-block comments; "
                    "annotate functional blocks and non-obvious topology "
                    "(single-point grounds, decoupling intent, matching groups)"
                ),
            )
        )

    junctioned = {(round(jx, 2), round(jy, 2)) for jx, jy in junction_points}
    reported_taps: set[tuple[float, float]] = set()
    for ex, ey in wire_endpoints:
        if (round(ex, 2), round(ey, 2)) in junctioned:
            continue
        for x1, y1, x2, y2 in segments:
            distance, t = _point_segment_distance(ex, ey, x1, y1, x2, y2)
            if distance > _ON_WIRE_EPS_MM or not (1e-9 < t < 1.0 - 1e-9):
                continue
            key = (round(ex, 2), round(ey, 2))
            if key in reported_taps:
                break
            reported_taps.add(key)
            findings.append(
                SchLintFinding(
                    type="junction_missing",
                    severity="warning",
                    description=(
                        f"wire endpoint at ({ex:.2f},{ey:.2f}) taps the middle of "
                        "another wire but has no junction dot; add_junction there "
                        "or move the endpoint off the run"
                    ),
                )
            )
            break

    off_wire: list[str] = []
    for name, lx, ly in net_labels:
        on_wire = any(
            _point_segment_distance(lx, ly, x1, y1, x2, y2)[0] <= _ON_WIRE_EPS_MM
            for x1, y1, x2, y2 in segments
        )
        if on_wire:
            continue
        near_symbol = any(
            (lx - sx) ** 2 + (ly - sy) ** 2 <= _LABEL_NEAR_SYMBOL_MM**2 for sx, sy in symbol_centers
        )
        if not near_symbol:
            off_wire.append(f'"{name}" at ({lx:.2f},{ly:.2f})')
    if off_wire:
        findings.append(
            SchLintFinding(
                type="label_off_wire",
                severity="warning",
                description=(
                    f"{len(off_wire)} net label(s) sit on no wire and near no "
                    "symbol; a floating label reads as connectivity but connects "
                    "nothing"
                ),
                items=off_wire,
            )
        )
    if symbols >= 2 and item_positions:
        xs = [position[0] for position in item_positions]
        ys = [position[1] for position in item_positions]
        usage = ((max(xs) - min(xs)) * (max(ys) - min(ys))) / (page_width * page_height)
        if usage < _MIN_SHEET_USAGE:
            findings.append(
                SchLintFinding(
                    type="sheet_underutilized",
                    severity="warning",
                    description=(
                        f"placed items cover {usage * 100:.0f}% of the sheet; "
                        "spread placement for readability"
                    ),
                )
            )

    if non_power_symbols >= 2 and wires == 0 and labels > 0:
        findings.append(
            SchLintFinding(
                type="label_only_connectivity",
                severity="warning",
                description=(
                    f"{labels} net labels carry all connectivity with no wires; "
                    "wire the main signal chain and reserve labels for power "
                    "rails and crossing nets"
                ),
            )
        )

    errors = sum(1 for f in findings if f.severity == "error")
    warnings = sum(1 for f in findings if f.severity == "warning")
    return SchLintReport(
        source=path,
        verdict="fail" if errors else "pass",
        errors=errors,
        warnings=warnings,
        symbols_checked=symbols,
        findings=findings,
    )


def lint_file(path: Path, output: Path | None = None) -> SchLintReport:
    try:
        report = lint_schematic(path)
    except SchLintError as exc:
        report = SchLintReport(
            source=path,
            verdict="fail",
            errors=1,
            warnings=0,
            symbols_checked=0,
            findings=[
                SchLintFinding(
                    type="parse_error",
                    severity="error",
                    description=str(exc),
                )
            ],
        )
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("schematic", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    report = lint_file(args.schematic, args.output)
    print(report.model_dump_json(indent=2))
    return 0 if report.verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
