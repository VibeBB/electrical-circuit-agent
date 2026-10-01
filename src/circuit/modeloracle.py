"""KiCad STEP-export checks for footprint 3D models."""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import kicad_cli, occt, sexpr
from .libitems import FootprintDef, parse_footprint
from .libtestboard import write_model_export_board
from .model3d import footprint_to_board_xy
from .partspec import PartSpec
from .ruleprofile import EffectiveRules


class ModelExportFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal["model_export_missing", "model_export_mismatch", "model_export_unavailable"]
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


def _expected_terminal_centers(
    footprint: FootprintDef,
    rotation_deg: float,
) -> list[tuple[float, float]]:
    return [
        footprint_to_board_xy(pad.x, pad.y, rotation_deg=rotation_deg)
        for pad in footprint.pads
        if pad.type != "np_thru_hole" and any(layer.endswith(".Cu") for layer in pad.layers)
    ]


def _terminal_centers(shape: occt.Shape, board_top_z_mm: float) -> list[tuple[float, float]]:
    return [
        ((region.bbox_xy[0] + region.bbox_xy[2]) / 2, (region.bbox_xy[1] + region.bbox_xy[3]) / 2)
        for region in occt.slab_regions(shape, board_top_z_mm - 0.005, board_top_z_mm + 0.015)
    ]


def _match_terminal_centers(
    expected: list[tuple[float, float]],
    actual: list[tuple[float, float]],
) -> bool:
    remaining = list(expected)
    for x, y in actual:
        match = next(
            (
                index
                for index, (expected_x, expected_y) in enumerate(remaining)
                if abs(expected_x - x) <= 0.02 and abs(expected_y - y) <= 0.02
            ),
            None,
        )
        if match is None:
            return False
        remaining.pop(match)
    return not remaining


def verify_model_export(
    spec: PartSpec,
    footprint_path: Path,
    *,
    model_reference: str,
    model_path: Path | None,
    rules: EffectiveRules,
    out_dir: Path,
) -> ModelExportReport:
    """Compare KiCad's board STEP export with the referenced model and pads."""
    findings: list[ModelExportFinding] = []
    runs: list[ModelExportRun] = []
    model_sha256: str | None = None
    expected_volume: float | None = None
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
        footprint = parse_footprint(footprint_path)
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
        expected_centers = _expected_terminal_centers(footprint, rotation_deg)
        try:
            actual_centers = _terminal_centers(exported_shape, board_top_z_mm)
        except ValueError as error:
            findings.append(
                ModelExportFinding(
                    code="model_export_mismatch",
                    message=f"could not inspect exported terminal regions: {error}",
                    rotation_deg=rotation_deg,
                )
            )
            continue
        volume_matches = (
            expected_volume is not None
            and exported_facts.valid
            and exported_facts.units == "mm"
            and _relative_volume_match(expected_volume, exported_volume)
        )
        terminals_match = bool(expected_centers) and _match_terminal_centers(
            expected_centers,
            actual_centers,
        )
        passed = volume_matches and terminals_match
        runs.append(
            ModelExportRun(
                rotation_deg=rotation_deg,
                board_top_z_mm=board_top_z_mm,
                expected_volume_mm3=expected_volume,
                exported_volume_mm3=exported_volume,
                expected_terminal_count=len(expected_centers),
                exported_terminal_count=len(actual_centers),
                terminal_centers_xy=actual_centers,
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
                    f"board pads ({len(expected_centers)}) within 0.02 mm"
                )
            findings.append(
                ModelExportFinding(
                    code="model_export_mismatch",
                    message="; ".join(details_parts),
                    rotation_deg=rotation_deg,
                )
            )

    zero_run = next((run for run in runs if run.rotation_deg == 0.0), None)
    ninety_run = next((run for run in runs if run.rotation_deg == 90.0), None)
    if zero_run is not None and ninety_run is not None and zero_run.passed and ninety_run.passed:
        rotated_zero_centers = [(-y, x) for x, y in zero_run.terminal_centers_xy]
        if not _match_terminal_centers(rotated_zero_centers, ninety_run.terminal_centers_xy):
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
