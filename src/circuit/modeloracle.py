"""KiCad STEP-export checks for footprint 3D models."""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import kicad_cli, occt, sexpr
from .libitems import parse_footprint
from .libtestboard import write_model_export_board
from .model3d import ExpectedTerminal, expected_terminals, footprint_to_board_xy
from .partspec import PartSpec
from .ruleprofile import EffectiveRules


class ModelExportFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "model_export_missing",
        "model_export_mismatch",
        "model_export_unavailable",
        "model_export_pin1",
    ]
    severity: Literal["error"] = "error"
    message: str
    rotation_deg: float | None = None


class ModelExportRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rotation_deg: float
    board_top_z_mm: float
    expected_volume_mm3: float | None
    exported_volume_mm3: float | None
    expected_terminal_count: int
    exported_terminal_count: int
    terminal_centers_xy: list[tuple[float, float]]
    expected_pin1: str | None
    exported_pin1: str | None
    passed: bool


class ModelExportReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_model_export_oracle"] = "circuit_model_export_oracle"
    verdict: Literal["pass", "fail"]
    model_sha256: str | None
    runs: list[ModelExportRun]
    findings: list[ModelExportFinding]


def _first(node: list[sexpr.SExpr], name: str) -> list[sexpr.SExpr] | None:
    return next(
        (child for child in node[1:] if isinstance(child, list) and child and child[0] == name),
        None,
    )


def _board_thickness_mm(board_path: Path) -> float:
    root = sexpr.parse_text(board_path.read_text(encoding="utf-8"))
    if not root or root[0] != "kicad_pcb":
        raise ValueError("generated board is not a KiCad PCB")
    general = _first(root, "general")
    thickness = _first(general, "thickness") if general is not None else None
    if thickness is None or len(thickness) != 2 or not isinstance(thickness[1], str):
        raise ValueError("generated board has no thickness")
    try:
        result = float(thickness[1])
    except ValueError as error:
        raise ValueError("generated board thickness is not numeric") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError("generated board thickness is invalid")
    return result


def _relative_volume_match(expected: float, actual: float) -> bool:
    return expected > 0 and abs(actual - expected) / expected <= 1e-4


def _model_load_failure(message: str) -> bool:
    normalized = re.sub(r"\s+", " ", message.casefold())
    return "model" in normalized and any(
        marker in normalized
        for marker in (
            "failed",
            "failure",
            "not found",
            "cannot load",
            "could not load",
            "unable to load",
            "unable to read",
        )
    )


def _expected_terminal_regions(
    terminals: list[ExpectedTerminal],
    rotation_deg: float,
    placement_xy_mm: tuple[float, float],
) -> list[tuple[float, float, float, float]]:
    swaps_axes = round(rotation_deg / 90.0) % 2 == 1
    regions: list[tuple[float, float, float, float]] = []
    for terminal in terminals:
        center = footprint_to_board_xy(
            terminal.center_xy[0],
            terminal.center_xy[1],
            rotation_deg=rotation_deg,
            origin_x=placement_xy_mm[0],
            origin_y=placement_xy_mm[1],
        )
        width, height = terminal.size_xy
        if swaps_axes:
            width, height = height, width
        regions.append((center[0], center[1], width, height))
    return regions


def _terminal_regions(
    shape: occt.Shape,
    board_top_z_mm: float,
) -> list[tuple[float, float, float, float]]:
    return [
        (
            (region.bbox_xy[0] + region.bbox_xy[2]) / 2,
            (region.bbox_xy[1] + region.bbox_xy[3]) / 2,
            region.bbox_xy[2] - region.bbox_xy[0],
            region.bbox_xy[3] - region.bbox_xy[1],
        )
        for region in occt.slab_regions(shape, board_top_z_mm - 0.005, board_top_z_mm + 0.015)
    ]


def _match_terminal_regions(
    expected: list[tuple[float, float, float, float]],
    actual: list[tuple[float, float, float, float]],
) -> bool:
    remaining = list(expected)
    for x, y, width, height in actual:
        match = next(
            (
                index
                for index, (expected_x, expected_y, expected_width, expected_height) in enumerate(
                    remaining
                )
                if abs(expected_x - x) <= 0.02
                and abs(expected_y - y) <= 0.02
                and abs(expected_width - width) <= 0.02
                and abs(expected_height - height) <= 0.02
            ),
            None,
        )
        if match is None:
            return False
        remaining.pop(match)
    return not remaining


def _rotated_pin1_corner(spec: PartSpec, rotation_deg: float) -> str | None:
    if spec.package.family == "chip":
        return None
    signs = {
        "top_left": (-1, -1),
        "top_right": (1, -1),
        "bottom_right": (1, 1),
        "bottom_left": (-1, 1),
    }
    x, y = signs[spec.package.pin1_corner]
    angle = math.radians(rotation_deg)
    rotated_x = math.cos(angle) * x - math.sin(angle) * y
    rotated_y = math.sin(angle) * x + math.cos(angle) * y
    horizontal = "left" if rotated_x < 0 else "right"
    vertical = "top" if rotated_y < 0 else "bottom"
    return f"{vertical}_{horizontal}"


def _exported_pin1(shape: occt.Shape) -> str | None:
    facts = occt.inspect(shape)
    if not facts.solids:
        return None
    body = max(facts.solids, key=lambda solid: solid.volume).bbox
    marker = occt.pin1_marker(shape, body)
    return marker.quadrant if marker is not None else None


def verify_model_export(
    spec: PartSpec,
    footprint_path: Path,
    *,
    model_reference: str,
    model_path: Path | None,
    rules: EffectiveRules,
    out_dir: Path,
    placement_xy_mm: tuple[float, float] = (0.0, 0.0),
) -> ModelExportReport:
    """Compare KiCad's board STEP export with the referenced model placement."""
    findings: list[ModelExportFinding] = []
    runs: list[ModelExportRun] = []
    model_sha256: str | None = None
    expected_volume: float | None = None
    terminals: list[ExpectedTerminal] = []
    try:
        terminals = expected_terminals(spec)
    except ValueError as error:
        findings.append(
            ModelExportFinding(
                code="model_export_unavailable",
                message=f"PartSpec terminals could not be derived: {error}",
            )
        )
    if model_path is not None and model_path.is_file():
        model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
        try:
            source_facts = occt.inspect(occt.read_step(model_path))
            if source_facts.valid and source_facts.units == "mm" and source_facts.solid_count > 0:
                expected_volume = sum(solid.volume for solid in source_facts.solids)
        except (OSError, ValueError) as error:
            findings.append(
                ModelExportFinding(
                    code="model_export_mismatch",
                    message=f"referenced STEP model could not be inspected: {error}",
                )
            )

    try:
        parse_footprint(footprint_path)
    except (OSError, ValueError) as error:
        findings.append(
            ModelExportFinding(
                code="model_export_unavailable",
                message=f"could not prepare KiCad export board: {error}",
            )
        )
        return ModelExportReport(
            verdict="fail",
            model_sha256=model_sha256,
            runs=runs,
            findings=findings,
        )

    for rotation_deg in (0.0, 90.0):
        run_dir = out_dir / f"rotation-{int(rotation_deg)}"
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            board_path = write_model_export_board(
                run_dir,
                spec=spec,
                footprint_path=footprint_path,
                rules=rules,
                rotation_deg=rotation_deg,
                placement_xy_mm=placement_xy_mm,
                model_reference_override=model_reference,
            )
            board_top_z_mm = _board_thickness_mm(board_path) - 0.005
        except (OSError, ValueError) as error:
            findings.append(
                ModelExportFinding(
                    code="model_export_unavailable",
                    message=f"could not prepare KiCad export board: {error}",
                    rotation_deg=rotation_deg,
                )
            )
            continue

        step_path = run_dir / "model-export.step"
        try:
            command = kicad_cli.run(
                [
                    "pcb",
                    "export",
                    "step",
                    "--no-board-body",
                    "--force",
                    "--output",
                    str(step_path),
                    str(board_path),
                ]
            )
        except kicad_cli.KicadCliError as error:
            findings.append(
                ModelExportFinding(
                    code="model_export_unavailable",
                    message=str(error),
                    rotation_deg=rotation_deg,
                )
            )
            continue

        details = "\n".join(part for part in (command.stderr, command.stdout) if part).strip()
        if _model_load_failure(details):
            findings.append(
                ModelExportFinding(
                    code="model_export_missing",
                    message=details,
                    rotation_deg=rotation_deg,
                )
            )
            continue
        if command.returncode:
            findings.append(
                ModelExportFinding(
                    code="model_export_unavailable",
                    message=details or "kicad-cli STEP export failed",
                    rotation_deg=rotation_deg,
                )
            )
            continue
        if not step_path.is_file():
            findings.append(
                ModelExportFinding(
                    code="model_export_missing",
                    message=details or "kicad-cli STEP export contains no component solids",
                    rotation_deg=rotation_deg,
                )
            )
            continue
        try:
            exported_shape = occt.read_step(step_path)
            exported_facts = occt.inspect(exported_shape)
        except (OSError, ValueError) as error:
            findings.append(
                ModelExportFinding(
                    code="model_export_missing",
                    message=f"export contains no loadable component solids: {error}",
                    rotation_deg=rotation_deg,
                )
            )
            continue
        if exported_facts.solid_count == 0:
            findings.append(
                ModelExportFinding(
                    code="model_export_missing",
                    message="kicad-cli STEP export contains no component solids",
                    rotation_deg=rotation_deg,
                )
            )
            continue

        exported_volume = sum(solid.volume for solid in exported_facts.solids)
        expected_regions = _expected_terminal_regions(terminals, rotation_deg, placement_xy_mm)
        try:
            actual_regions = _terminal_regions(exported_shape, board_top_z_mm)
        except ValueError as error:
            findings.append(
                ModelExportFinding(
                    code="model_export_mismatch",
                    message=f"could not inspect exported terminal regions: {error}",
                    rotation_deg=rotation_deg,
                )
            )
            continue
        actual_centers = [(region[0], region[1]) for region in actual_regions]
        volume_matches = (
            expected_volume is not None
            and exported_facts.valid
            and exported_facts.units == "mm"
            and _relative_volume_match(expected_volume, exported_volume)
        )
        terminals_match = bool(expected_regions) and _match_terminal_regions(
            expected_regions,
            actual_regions,
        )
        expected_pin1 = _rotated_pin1_corner(spec, rotation_deg)
        exported_pin1 = _exported_pin1(exported_shape)
        pin1_matches = expected_pin1 is None or exported_pin1 == expected_pin1
        passed = volume_matches and terminals_match and pin1_matches
        runs.append(
            ModelExportRun(
                rotation_deg=rotation_deg,
                board_top_z_mm=board_top_z_mm,
                expected_volume_mm3=expected_volume,
                exported_volume_mm3=exported_volume,
                expected_terminal_count=len(expected_regions),
                exported_terminal_count=len(actual_centers),
                terminal_centers_xy=actual_centers,
                expected_pin1=expected_pin1,
                exported_pin1=exported_pin1,
                passed=passed,
            )
        )
        if not passed:
            details_parts: list[str] = []
            if not volume_matches:
                details_parts.append(
                    f"exported volume {exported_volume:.9g} mm^3 does not match "
                    f"model volume {expected_volume!r} mm^3"
                )
            if not terminals_match:
                details_parts.append(
                    f"exported terminal regions ({len(actual_centers)}) do not match "
                    f"PartSpec terminal regions ({len(expected_regions)}) within 0.02 mm"
                )
            findings.append(
                ModelExportFinding(
                    code="model_export_mismatch",
                    message="; ".join(details_parts),
                    rotation_deg=rotation_deg,
                )
            )
        if not pin1_matches:
            findings.append(
                ModelExportFinding(
                    code="model_export_pin1",
                    message=(
                        f"exported pin-1 marker {exported_pin1!r} does not match "
                        f"PartSpec corner {expected_pin1!r}"
                    ),
                    rotation_deg=rotation_deg,
                )
            )

    zero_run = next((run for run in runs if run.rotation_deg == 0.0), None)
    ninety_run = next((run for run in runs if run.rotation_deg == 90.0), None)
    if zero_run is not None and ninety_run is not None and zero_run.passed and ninety_run.passed:
        zero_shape = occt.read_step(out_dir / "rotation-0" / "model-export.step")
        ninety_shape = occt.read_step(out_dir / "rotation-90" / "model-export.step")
        rotated_zero_regions = [
            (
                *footprint_to_board_xy(
                    x - placement_xy_mm[0],
                    y - placement_xy_mm[1],
                    rotation_deg=90.0,
                    origin_x=placement_xy_mm[0],
                    origin_y=placement_xy_mm[1],
                ),
                height,
                width,
            )
            for x, y, width, height in _terminal_regions(
                zero_shape,
                zero_run.board_top_z_mm,
            )
        ]
        if not _match_terminal_regions(
            rotated_zero_regions,
            _terminal_regions(ninety_shape, ninety_run.board_top_z_mm),
        ):
            findings.append(
                ModelExportFinding(
                    code="model_export_mismatch",
                    message=(
                        "rotation-90 export does not match the rotated rotation-0 terminal regions"
                    ),
                    rotation_deg=90.0,
                )
            )

    if model_path is None or not model_path.is_file():
        if not any(finding.code == "model_export_missing" for finding in findings):
            findings.append(
                ModelExportFinding(
                    code="model_export_missing",
                    message="referenced STEP model cannot be resolved to a local file",
                )
            )
    elif not runs and not any(
        finding.code in {"model_export_missing", "model_export_unavailable"} for finding in findings
    ):
        findings.append(
            ModelExportFinding(
                code="model_export_unavailable",
                message="kicad-cli produced no model export results",
            )
        )

    return ModelExportReport(
        verdict="fail" if findings else "pass",
        model_sha256=model_sha256,
        runs=runs,
        findings=findings,
    )
