"""Deterministic readability lint for .kicad_sch files."""

from __future__ import annotations

import argparse
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
_LABELED_PROPERTIES = {"Reference", "Value"}
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
    for node in root[1:]:
        if not isinstance(node, list) or not node or not isinstance(node[0], str):
            continue
        if node[0] != "symbol" and node[0] not in _POSITIONED_ITEMS:
            continue
        position = _position(node)
        if position is None:
            continue
        px, py = position
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
            if _has_flag(prop, "hide"):
                findings.append(
                    SchLintFinding(
                        type="property_hidden",
                        severity="warning",
                        description=f"{name} property {prop[2]!r} is hidden",
                        items=[f"symbol uuid={uuid}"],
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
