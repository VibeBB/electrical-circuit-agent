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
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from . import authoring, kicad_cli, occt, visionread
from . import pinout as pinout_oracle
from .datasheet import load_extraction
from .klc import KlcReport, run_klc
from .landpattern import (
    Density,
    LandPatternResult,
    Rect,
    compute_land_pattern,
    lead_rects,
    standard_pin_placements,
)
from .libitems import (
    FootprintDef,
    GraphicDef,
    LibItemError,
    ModelRef,
    PadDef,
    SymbolDef,
    parse_footprint,
    parse_symbol,
)
from .libsource import (
    LibrarySourceError,
    ProvenanceEntry,
    assert_safe_destination,
    load_library_provenance,
)
from .libtestboard import TestBoard, build_test_board
from .lineage import (
    FootprintLineage,
    compare_recorded_changes,
    lineage_path_for,
    pad_changes,
)
from .model3d import GENERATOR_VERSION, GeneratedModel
from .modeloracle import ModelExportReport, verify_model_export
from .packageid import check_package_identity
from .partspec import (
    Dimension,
    LandPad,
    PartSpec,
    PartSpecReport,
    check_part_spec,
    part_spec_sha256,
)
from .pinout import PinoutGeometry
from .pinsource import (
    PinSource,
    PinSourceComparison,
    PinSourceInput,
    compare_pin_sources,
    parse_pin_source,
    source_from_part_spec,
)
from .ruleprofile import EffectiveRules, load_rules

if TYPE_CHECKING:
    from .occt import Bounds, Shape, ShapeFacts, SlabRegion


class VerifyFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["error", "warning", "info"]
    subject: str
    message: str
    model_sha256: str | None = None


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


class VerifiedModelInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    solid_count: int
    total_volume_mm3: float
    bbox_mm: tuple[float, float, float, float, float, float]
    units: str
    valid: bool
    closed_shells: tuple[bool, ...]
    pin1_marker: str | None
    pin1_color_marker: str | None


class VerifiedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    resolved: bool
    sha256: str | None
    inspection: VerifiedModelInspection | None = None
    export_oracle: ModelExportReport | None = None


class ModelInspectionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_model_inspection"] = "circuit_model_inspection"
    verdict: Literal["pass", "fail"]
    path: Path
    sha256: str | None
    facts: VerifiedModelInspection | None
    findings: list[VerifyFinding]


class ModelCrossCheckGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path
    sha256: str
    body_extents_mm: tuple[float, float, float]
    terminal_centers_xy_mm: list[tuple[float, float]]
    pin1_quadrant: str | None


class ModelCrossCheckFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal["model_source_disagreement"] = "model_source_disagreement"
    severity: Literal["error"] = "error"
    message: str


class ModelCrossCheckReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_model_cross_check"] = "circuit_model_cross_check"
    passed: bool
    generated: ModelCrossCheckGeometry | None
    imported: ModelCrossCheckGeometry | None
    imported_provenance: ProvenanceEntry | None
    findings: list[ModelCrossCheckFinding]


def _empty_pin_source_inputs() -> list[PinSourceInput]:
    return []


def _empty_pin_source_hashes() -> list[str | None]:
    return []


class VerificationInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_spec_path: Path
    symbol_lib: Path
    symbol_name: str
    footprint_path: Path
    density: Density
    tolerance_mm: float
    model_required: bool
    klc: bool = False
    test_board: bool = True
    pin_source_path: Path | None = None
    pin_source_sha256: str | None = None
    pin_sources: list[PinSourceInput] = Field(default_factory=_empty_pin_source_inputs)
    pin_source_sha256s: list[str | None] = Field(default_factory=_empty_pin_source_hashes)


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
    test_board: TestBoard | None = None
    pin_source_comparison: PinSourceComparison | None = None


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
    *,
    model_sha256: str | None = None,
) -> None:
    findings.append(
        VerifyFinding(
            code=code,
            severity=severity,
            subject=subject,
            message=message,
            model_sha256=model_sha256,
        )
    )


def _correction_pointer_key(pointer: str, spec: PartSpec) -> str | None:
    tokens = pointer.split("/")
    if len(tokens) < 3 or tokens[0] != "" or tokens[1] != "pins":
        return pointer
    try:
        index = int(tokens[2])
    except ValueError:
        return pointer
    if not 0 <= index < len(spec.pins):
        return None
    number = spec.pins[index].number.replace("~", "~0").replace("/", "~1")
    return "/" + "/".join(("pins", number, *tokens[3:]))


def _human_correction_exists(
    library_dir: Path | None,
    spec: PartSpec,
    pointer: str,
    current_value: object,
) -> bool:
    if library_dir is None:
        return False
    corpus = library_dir / "reviews" / "corrections.jsonl"
    try:
        lines = corpus.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(record, dict):
            return False
        correction = cast(dict[str, object], record)
        normalized_pointer = _correction_pointer_key(str(correction.get("pointer", "")), spec)
        if (
            normalized_pointer == pointer
            and correction.get("mpn") == spec.mpn
            and correction.get("pdf_sha256") == spec.datasheet.sha256
            and isinstance(correction.get("event_sha256"), str)
            and _authoring_value_equal(pointer, correction.get("new"), current_value)
        ):
            return True
    return False


def _authoring_value_equal(pointer: str, left: object, right: object) -> bool:
    if pointer.endswith("/name") or "/labels_vision/" in pointer:
        return (
            isinstance(left, str)
            and isinstance(right, str)
            and pinout_oracle.names_equal(left, right)
        )
    return left == right


def _check_authoring_consensus(
    spec: PartSpec,
    *,
    spec_path: Path,
    library_dir: Path | None,
    findings: list[VerifyFinding],
) -> None:
    if spec.authoring is None:
        _finding(
            findings,
            "authoring_missing",
            "error",
            "authoring",
            "PartSpec has no blind authoring run reference",
        )
        return
    reference = Path(spec.authoring)
    if reference.is_absolute():
        _finding(
            findings,
            "authoring_invalid",
            "error",
            "authoring",
            "authoring run path must be relative to the PartSpec directory",
        )
        return
    spec_root = spec_path.resolve().parent
    run_dir = (spec_root / reference).resolve()
    if not run_dir.is_relative_to(spec_root):
        _finding(
            findings,
            "authoring_invalid",
            "error",
            "authoring",
            "authoring run path escapes the PartSpec directory",
        )
        return
    try:
        comparison = authoring.compare_runs(run_dir)
        sealed_a = PartSpec.model_validate_json(
            (run_dir / "sealed" / "a.json").read_text(encoding="utf-8")
        )
        _sealed_b = PartSpec.model_validate_json(
            (run_dir / "sealed" / "b.json").read_text(encoding="utf-8")
        )
    except (authoring.AuthoringError, OSError, ValueError) as exc:
        _finding(findings, "authoring_invalid", "error", "authoring", str(exc))
        return
    for issue in comparison.issues:
        _finding(findings, issue.code, issue.severity, "authoring", issue.message)
    current = authoring.normalize(spec)
    values_a = authoring.normalize(sealed_a)
    for pointer in comparison.agreed:
        agreed_value = values_a.get(pointer)
        current_value = current.get(pointer)
        same_value = pointer in current and _authoring_value_equal(
            pointer, current_value, agreed_value
        )
        if same_value or _human_correction_exists(library_dir, spec, pointer, current_value):
            continue
        _finding(
            findings,
            "authoring_consensus_violated",
            "error",
            pointer,
            "current PartSpec differs from independently agreed authoring value",
        )
    for disagreement in comparison.disagreements:
        _finding(
            findings,
            "authoring_disagreement",
            "warning",
            disagreement.pointer,
            "independent lane values differ: "
            f"A={json.dumps(disagreement.a, ensure_ascii=False, sort_keys=True)}; "
            f"B={json.dumps(disagreement.b, ensure_ascii=False, sort_keys=True)}",
        )
    if comparison.model_diversity != "distinct":
        _finding(
            findings,
            "authoring_models_not_diverse",
            "warning",
            "authoring",
            f"independent authors used {comparison.model_diversity} models",
        )


def _check_vision_comparisons(
    spec: PartSpec,
    *,
    spec_path: Path,
    symbol_lib: Path,
    footprint_path: Path,
    spec_hash: str,
    findings: list[VerifyFinding],
) -> None:
    artifacts: tuple[
        tuple[
            Literal["compare_footprint", "compare_symbol"],
            Literal["footprint", "symbol"],
            Path,
        ],
        ...,
    ] = (
        ("compare_footprint", "footprint", footprint_path),
        ("compare_symbol", "symbol", symbol_lib),
    )
    for kind, artifact_kind, path in artifacts:
        artifact_hash = (_sha256(path) if path.is_file() else "") or ""
        evidence, stale = visionread.find_comparison_evidence(
            spec_path.resolve().parent,
            kind=kind,
            spec_sha256=spec_hash,
            artifact_sha256=artifact_hash,
            artifact_kind=artifact_kind,
        )
        if not evidence:
            code = "vision_compare_stale" if stale else "vision_compare_missing"
            message = (
                "no current comparison record matches the PartSpec and library artifact hashes"
                if stale
                else "a current hash-bound comparison record is required"
            )
            _finding(findings, code, "error", artifact_kind, message)
            continue
        for item in evidence:
            if not item.impression_valid:
                _finding(
                    findings,
                    "vision_impression_missing",
                    "error",
                    artifact_kind,
                    f"comparison read {item.item.read_id} has a missing or invalid impression",
                )
            if item.batch.pdf_sha256 != spec.datasheet.sha256:
                _finding(
                    findings,
                    "vision_compare_stale",
                    "error",
                    artifact_kind,
                    f"comparison read {item.item.read_id} is bound to a different datasheet PDF",
                )
            if not item.answers.control_passed:
                _finding(
                    findings,
                    "vision_control_failed",
                    "error",
                    artifact_kind,
                    f"comparison control failed for read {item.item.read_id}",
                )
            if item.answers.status.get(item.item.read_id) != "ok" or item.normalized is None:
                _finding(
                    findings,
                    "vision_compare_stale",
                    "error",
                    artifact_kind,
                    f"comparison read {item.item.read_id} is missing a parseable answer",
                )
                continue
            comparison = item.normalized
            pin1_matches = comparison.get("pin1_matches")
            arrangement_matches = comparison.get("arrangement_matches")
            numbering_direction_matches = comparison.get("numbering_direction_matches")
            differences = comparison.get("differences")
            if (
                not isinstance(pin1_matches, bool)
                or not isinstance(arrangement_matches, bool)
                or not isinstance(numbering_direction_matches, bool)
                or not isinstance(differences, list)
            ):
                _finding(
                    findings,
                    "vision_compare_stale",
                    "error",
                    artifact_kind,
                    f"comparison read {item.item.read_id} has an invalid normalized answer",
                )
                continue
            if (
                not pin1_matches
                or not arrangement_matches
                or not numbering_direction_matches
                or differences
            ):
                _finding(
                    findings,
                    "vision_compare_mismatch",
                    "warning",
                    artifact_kind,
                    "visual comparison reported differences: " + "; ".join(differences),
                )


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

    drawing_positions = pinout_oracle.to_top_view(
        {label.number: (label.x, label.y) for label in geometry.labels},
        geometry.view,
    )
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


def _project_relative_file(path_text: str, project_root: Path) -> Path | None:
    relative = Path(path_text)
    if relative.is_absolute():
        return None
    try:
        resolved = (project_root / relative).resolve(strict=True)
        resolved.relative_to(project_root.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _validate_lineage(
    footprint_path: Path,
    footprint: FootprintDef | None,
    library_dir: Path | None,
    findings: list[VerifyFinding],
) -> tuple[FootprintLineage | None, FootprintDef | None, bool]:
    sidecar = lineage_path_for(footprint_path)
    if not sidecar.exists() and not sidecar.is_symlink():
        return None, None, False
    try:
        lineage = FootprintLineage.model_validate_json(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        _finding(
            findings,
            "lineage_invalid",
            "error",
            str(sidecar),
            f"footprint lineage cannot be read: {error}",
        )
        return None, None, False

    valid = True
    if lineage.footprint_sha256 != _sha256(footprint_path):
        _finding(
            findings,
            "lineage_footprint_hash",
            "error",
            str(sidecar),
            "lineage footprint hash does not match the current footprint",
        )
        valid = False

    project_root = (
        library_dir.parent.resolve()
        if library_dir is not None
        else footprint_path.parent.parent.resolve()
    )
    base_path = _project_relative_file(lineage.base.path, project_root)
    base_footprint: FootprintDef | None = None
    if base_path is None or _sha256(base_path) != lineage.base.sha256:
        _finding(
            findings,
            "lineage_base",
            "error",
            lineage.base.path,
            "lineage base file is missing or its hash does not match",
        )
        valid = False
    else:
        try:
            base_footprint = parse_footprint(base_path)
        except (LibItemError, OSError) as error:
            _finding(
                findings,
                "lineage_base",
                "error",
                lineage.base.path,
                f"lineage base footprint cannot be parsed: {error}",
            )
            valid = False

    for evidence in lineage.evidence:
        evidence_path = _project_relative_file(evidence.path, project_root)
        if evidence_path is None or _sha256(evidence_path) != evidence.sha256:
            _finding(
                findings,
                "lineage_evidence",
                "error",
                evidence.path,
                "lineage evidence file is missing or its hash does not match",
            )
            valid = False

    if base_footprint is not None and footprint is not None:
        actual_changes = pad_changes(base_footprint, footprint)
        unrecorded, stale = compare_recorded_changes(actual_changes, lineage.changes)
        if unrecorded:
            _finding(
                findings,
                "lineage_unrecorded_change",
                "error",
                str(sidecar),
                "unrecorded pad changes: "
                + ", ".join(f"{change.pad}.{change.field}" for change in unrecorded),
            )
            valid = False
        if stale:
            _finding(
                findings,
                "lineage_stale_change",
                "error",
                str(sidecar),
                "stale pad changes: "
                + ", ".join(f"{change.pad}.{change.field}" for change in stale),
            )
            valid = False
    elif footprint is None:
        _finding(
            findings,
            "lineage_unrecorded_change",
            "error",
            str(sidecar),
            "current footprint could not be parsed for lineage comparison",
        )
        valid = False

    return lineage, base_footprint, valid


def _reference_deviation_fields(
    reference: LandPatternResult,
    footprint: FootprintDef,
    tolerance_mm: float,
) -> list[tuple[str, str]]:
    expected = _reference_boxes(reference.pads)
    actual = _pad_boxes(footprint.pads)
    deviations: list[tuple[str, str]] = []
    for number in sorted(expected.keys() & actual.keys()):
        ex0, ey0, ex1, ey1 = expected[number]
        ax0, ay0, ax1, ay1 = actual[number]
        deltas = {
            "x": abs((ex0 + ex1 - ax0 - ax1) / 2),
            "y": abs((ey0 + ey1 - ay0 - ay1) / 2),
            "width": abs((ex1 - ex0) - (ax1 - ax0)),
            "height": abs((ey1 - ey0) - (ay1 - ay0)),
        }
        deviations.extend(
            (number, field) for field, delta in deltas.items() if delta > tolerance_mm
        )
    return deviations


def _tuning_delta_report(
    reference: LandPatternResult,
    footprint: FootprintDef,
    base: FootprintDef,
) -> str:
    reference_boxes = _reference_boxes(reference.pads)
    current_boxes = _pad_boxes(footprint.pads)
    base_boxes = _pad_boxes(base.pads)
    report: list[dict[str, object]] = []
    for number in sorted(current_boxes):
        current = current_boxes[number]

        def delta(
            boxes: dict[str, tuple[float, float, float, float]],
            *,
            current_box: tuple[float, float, float, float] = current,
            pin_number: str = number,
        ) -> dict[str, float] | None:
            other = boxes.get(pin_number)
            if other is None:
                return None
            return {
                "x": (current_box[0] + current_box[2] - other[0] - other[2]) / 2,
                "y": (current_box[1] + current_box[3] - other[1] - other[3]) / 2,
                "width": (current_box[2] - current_box[0]) - (other[2] - other[0]),
                "height": (current_box[3] - current_box[1]) - (other[3] - other[1]),
            }

        report.append(
            {
                "pad": number,
                "reference_delta_mm": delta(reference_boxes),
                "base_delta_mm": delta(base_boxes),
            }
        )
    return json.dumps(report, sort_keys=True, separators=(",", ":"))


def _replace_with_intentional_tuning(
    reference: LandPatternResult,
    footprint: FootprintDef,
    base: FootprintDef,
    lineage: FootprintLineage,
    tolerance_mm: float,
    findings: list[VerifyFinding],
) -> None:
    deviation_fields = _reference_deviation_fields(reference, footprint, tolerance_mm)
    if not deviation_fields:
        return
    recorded = {(change.pad, change.field) for change in lineage.changes}
    if not all(deviation in recorded for deviation in deviation_fields):
        return
    geometry_finding = next(
        (finding for finding in findings if finding.code == "pad_geometry"),
        None,
    )
    if geometry_finding is None:
        return
    findings.remove(geometry_finding)
    _finding(
        findings,
        "intentional_tuning",
        "info",
        "footprint",
        "recorded pad deviations; deltas against reference and base in mm: "
        + _tuning_delta_report(reference, footprint, base),
    )


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


def _check_pad_geometry(
    spec: PartSpec,
    footprint: FootprintDef,
    reference: LandPatternResult,
    tolerance_mm: float,
    fabrication_tolerance_mm: float,
    lineage_valid: bool,
    findings: list[VerifyFinding],
) -> dict[str, tuple[float, float, float, float]]:
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
    if not lineage_valid:
        for number in expected_boxes.keys() & actual_boxes.keys():
            ex0, ey0, ex1, ey1 = expected_boxes[number]
            ax0, ay0, ax1, ay1 = actual_boxes[number]
            center_delta = math.hypot(
                (ex0 + ex1 - ax0 - ax1) / 2,
                (ey0 + ey1 - ay0 - ay1) / 2,
            )
            if center_delta > fabrication_tolerance_mm:
                _finding(
                    findings,
                    "pad_position",
                    "error",
                    f"pad.{number}",
                    f"pad center deviates {center_delta:.4f} mm from the PartSpec-derived "
                    f"reference, beyond fabrication tolerance {fabrication_tolerance_mm:.4f} mm",
                )

        exposed = spec.package.exposed_pad
        if (
            exposed is not None
            and exposed.number in expected_boxes
            and exposed.number in actual_boxes
        ):
            expected = expected_boxes[exposed.number]
            actual = actual_boxes[exposed.number]
            width_delta = abs((expected[2] - expected[0]) - (actual[2] - actual[0]))
            height_delta = abs((expected[3] - expected[1]) - (actual[3] - actual[1]))
            if max(width_delta, height_delta) > fabrication_tolerance_mm:
                _finding(
                    findings,
                    "ep_size",
                    "error",
                    f"pad.{exposed.number}",
                    f"EP size differs from the PartSpec-derived reference by "
                    f"{max(width_delta, height_delta):.4f} mm, beyond fabrication "
                    f"tolerance {fabrication_tolerance_mm:.4f} mm",
                )

        pitch = _dimension_value(spec.package.pitch) if spec.package.pitch is not None else None
        if pitch is not None and spec.package.family in (
            "no_lead_dual",
            "no_lead_quad",
            "gullwing_dual",
            "gullwing_quad",
            "chip",
        ):
            centers = {
                number: ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
                for number, box in actual_boxes.items()
            }
            placements = {number: side for number, side, _ in standard_pin_placements(spec)}
            rows: dict[str, list[float]] = {}
            for number, side in placements.items():
                center = centers.get(number)
                if center is None:
                    continue
                tangent = center[0] if side in {"top", "bottom"} else center[1]
                rows.setdefault(side, []).append(tangent)
            for side, positions in rows.items():
                ordered = sorted(positions)
                if any(abs((right - left) - pitch) > 0.005 for left, right in pairwise(ordered)):
                    _finding(
                        findings,
                        "footprint_pitch",
                        "error",
                        f"footprint.{side}",
                        f"adjacent pad pitch differs from PartSpec nominal {pitch:.4f} mm "
                        "by more than 0.005 mm",
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


def nominal_body_box(spec: PartSpec) -> tuple[float, float, float, float] | None:
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
    body = nominal_body_box(spec)
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
    spec: PartSpec,
    footprint: FootprintDef,
    rules: EffectiveRules,
    findings: list[VerifyFinding],
) -> None:
    pads = [pad for pad in footprint.pads if pad.number and _is_copper(pad)]
    exposed_number = spec.package.exposed_pad.number if spec.package.exposed_pad else None
    for index, first in enumerate(pads):
        for second in pads[index + 1 :]:
            if first.number == second.number:
                continue
            distance = _polygon_distance(_pad_polygon(first), _pad_polygon(second))
            is_ep_clearance = first.number == exposed_number or second.number == exposed_number
            minimum = (
                rules.min_ep_to_pad_clearance_mm if is_ep_clearance else rules.min_pad_clearance_mm
            )
            if is_ep_clearance and distance < minimum:
                _finding(
                    findings,
                    "ep_pad_clearance",
                    "error",
                    f"pads.{first.number},{second.number}",
                    f"EP-to-pad clearance is {distance:.4f} mm, below {minimum:.4f} mm",
                )
            elif not is_ep_clearance and distance < min(0.10, minimum):
                _finding(
                    findings,
                    "pad_clearance",
                    "error",
                    f"pads.{first.number},{second.number}",
                    f"copper clearance is {distance:.4f} mm, below {min(0.10, minimum):.4f} mm",
                )
            elif not is_ep_clearance and distance < minimum:
                _finding(
                    findings,
                    "pad_clearance",
                    "warning",
                    f"pads.{first.number},{second.number}",
                    f"copper clearance is {distance:.4f} mm, below {minimum:.4f} mm",
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


def functional_findings(
    spec: PartSpec,
    footprint: FootprintDef,
    rules: EffectiveRules,
) -> list[VerifyFinding]:
    """Return non-configurable pad, orientation, lead, and clearance findings."""

    findings: list[VerifyFinding] = []
    if spec.package.family not in (
        "no_lead_quad",
        "no_lead_dual",
        "gullwing_quad",
        "gullwing_dual",
        "chip",
    ):
        _check_pad_clearance(spec, footprint, rules, findings)
        return findings
    reference = compute_land_pattern(spec, rules=rules)
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

    _check_pin1_location(spec, footprint, findings)
    pin_numbers = {str(number) for number in range(1, spec.package.pin_count + 1)}
    expected_positions: dict[str, tuple[float, float]] = {}
    expected_groups: dict[str, list[tuple[float, float]]] = {}
    for pad in reference.pads:
        if pad.number in pin_numbers:
            expected_groups.setdefault(pad.number, []).append((pad.x, pad.y))
    for number, positions in expected_groups.items():
        expected_positions[number] = (
            sum(point[0] for point in positions) / len(positions),
            sum(point[1] for point in positions) / len(positions),
        )
    actual_groups: dict[str, list[tuple[float, float]]] = {}
    for pad in footprint.pads:
        if pad.number in pin_numbers and pad.type != "np_thru_hole":
            actual_groups.setdefault(pad.number, []).append((pad.x, pad.y))
    actual_positions = {
        number: (
            sum(point[0] for point in positions) / len(positions),
            sum(point[1] for point in positions) / len(positions),
        )
        for number, positions in actual_groups.items()
    }
    for issue in pinout_oracle.compare_orientation(expected_positions, actual_positions):
        _finding(
            findings,
            f"footprint_{issue.code}",
            issue.severity,
            "footprint",
            issue.message,
        )
    _check_pad_clearance(spec, footprint, rules, findings)
    _check_lead_geometry(spec, footprint, findings)
    return findings


def _check_klc(
    kind: Literal["footprint", "symbol"],
    path: Path,
    findings: list[VerifyFinding],
) -> None:
    report: KlcReport = run_klc(kind, path)
    for violation in report.violations:
        code = (
            violation.rule
            if violation.rule in {"klc_unavailable", "klc_failed"}
            else f"klc_{violation.rule}"
        )
        _finding(findings, code, violation.severity, str(path), violation.message)


def _model_pad_bbox(pad: PadDef) -> tuple[float, float, float, float]:
    angle = math.radians(pad.rotation)
    width = abs(pad.width * math.cos(angle)) + abs(pad.height * math.sin(angle))
    height = abs(pad.width * math.sin(angle)) + abs(pad.height * math.cos(angle))
    return (
        pad.x - width / 2,
        pad.y - height / 2,
        pad.x + width / 2,
        pad.y + height / 2,
    )


def _model_bbox(facts: ShapeFacts) -> tuple[float, float, float, float, float, float] | None:
    solids = facts.solids
    if not solids:
        return None
    return (
        min(solid.bbox.x_min for solid in solids),
        min(solid.bbox.y_min for solid in solids),
        min(solid.bbox.z_min for solid in solids),
        max(solid.bbox.x_max for solid in solids),
        max(solid.bbox.y_max for solid in solids),
        max(solid.bbox.z_max for solid in solids),
    )


def _model_dimension_bounds(
    dimension: Dimension,
    tolerance_mm: float,
) -> tuple[float | None, float | None]:
    nominal = _dimension_value(dimension)
    lower = (
        float(dimension.min)
        if dimension.min is not None
        else nominal - tolerance_mm
        if nominal is not None
        else None
    )
    upper = (
        float(dimension.max)
        if dimension.max is not None
        else nominal + tolerance_mm
        if nominal is not None
        else None
    )
    return lower, upper


def _model_pad_contains(
    pad_bbox: tuple[float, float, float, float],
    region_bbox: tuple[float, float, float, float],
    tolerance: float = 0.0,
) -> bool:
    return (
        pad_bbox[0] <= region_bbox[0] + tolerance
        and pad_bbox[1] <= region_bbox[1] + tolerance
        and pad_bbox[2] >= region_bbox[2] - tolerance
        and pad_bbox[3] >= region_bbox[3] - tolerance
    )


def _model_rectangles_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return (
        min(left[2], right[2]) - max(left[0], right[0]) > 1e-9
        and min(left[3], right[3]) - max(left[1], right[1]) > 1e-9
    )


def _generated_terminal_bindings(
    spec: PartSpec,
    model_path: Path,
    model_sha256: str,
    findings: list[VerifyFinding],
) -> dict[str, tuple[float, float]] | None:
    manifest_path = Path(f"{model_path}.gen.json")
    if not manifest_path.is_file():
        return None
    try:
        loaded: object = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return None
        manifest = cast(dict[str, object], loaded)
        if manifest.get("generator_version") != GENERATOR_VERSION:
            return None
        spec_sha256 = hashlib.sha256(
            json.dumps(
                spec.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            manifest.get("step_sha256") != model_sha256
            or manifest.get("spec_sha256") != spec_sha256
        ):
            raise ValueError("model or PartSpec hash does not match its generation manifest")
        parameters_value = manifest.get("parameters")
        parameters = (
            cast(dict[str, object], parameters_value)
            if isinstance(parameters_value, dict)
            else None
        )
        entries = parameters.get("terminal_map") if parameters is not None else None
        if not isinstance(entries, list):
            raise ValueError("terminal_map is missing from the generation manifest")
        bindings: dict[str, tuple[float, float]] = {}
        for entry_value in cast(list[object], entries):
            if not isinstance(entry_value, dict):
                raise ValueError("terminal_map contains an invalid entry")
            entry = cast(dict[str, object], entry_value)
            number = entry.get("number")
            center = entry.get("terminal_center_mm")
            if not isinstance(number, str) or number in bindings:
                raise ValueError("terminal_map contains an invalid or duplicate terminal")
            if not isinstance(center, list):
                raise ValueError("terminal_map contains an invalid terminal center")
            center_values = cast(list[object], center)
            if len(center_values) != 2:
                raise ValueError("terminal_map contains an invalid terminal center")
            center_x, center_y = center_values
            if (
                not isinstance(center_x, (int, float))
                or isinstance(center_x, bool)
                or not isinstance(center_y, (int, float))
                or isinstance(center_y, bool)
            ):
                raise ValueError("terminal_map contains an invalid or duplicate terminal")
            bindings[number] = (float(center_x), float(center_y))
        if len(bindings) != spec.package.pin_count + int(spec.package.exposed_pad is not None):
            raise ValueError("terminal_map does not cover the PartSpec terminal set")
        return bindings
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        _finding(
            findings,
            "model_manifest_invalid",
            "error",
            f"model.{model_path.name}",
            f"generation manifest cannot bind model terminals: {exc}",
            model_sha256=model_sha256,
        )
        return None


def _check_model_terminals(
    spec: PartSpec,
    footprint: FootprintDef,
    regions: list[SlabRegion],
    body_bbox: tuple[float, float, float, float, float, float],
    model_sha256: str,
    findings: list[VerifyFinding],
    terminal_bindings: dict[str, tuple[float, float]] | None = None,
) -> None:
    pads = [
        (pad, _model_pad_bbox(pad))
        for pad in footprint.pads
        if pad.type != "np_thru_hole" and any(layer.endswith(".Cu") for layer in pad.layers)
    ]
    if not pads:
        return
    largest_pad_area = max((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) for _, bbox in pads)
    assigned: dict[int, list[tuple[float, float]]] = {index: [] for index in range(len(pads))}
    for region in regions:
        region_bbox = region.bbox_xy
        center = (
            (region_bbox[0] + region_bbox[2]) / 2,
            (region_bbox[1] + region_bbox[3]) / 2,
        )
        overlaps = [
            index
            for index, (_, pad_bbox) in enumerate(pads)
            if _model_rectangles_overlap(region_bbox, pad_bbox)
        ]
        if len(overlaps) > 1 or region.area > largest_pad_area * 1.1:
            _finding(
                findings,
                "model_terminals_unseparable",
                "error",
                "model.terminals",
                "a terminal region overlaps multiple pads or exceeds the largest pad area",
                model_sha256=model_sha256,
            )
        if terminal_bindings is not None:
            matched_numbers = [
                number
                for number, position in terminal_bindings.items()
                if math.dist(center, position) <= 0.01
            ]
            if len(matched_numbers) != 1:
                _finding(
                    findings,
                    "model_terminal_mismatch",
                    "error",
                    "model.terminals",
                    f"terminal region centered at {center} does not match one PartSpec terminal",
                    model_sha256=model_sha256,
                )
                continue
            number = matched_numbers[0]
            targets = [index for index, (pad, _) in enumerate(pads) if pad.number == number]
            if len(targets) != 1:
                _finding(
                    findings,
                    "model_terminal_mismatch",
                    "error",
                    f"model.pad.{number}",
                    "PartSpec terminal does not map to exactly one copper pad with the same number",
                    model_sha256=model_sha256,
                )
                continue
            pad_index = targets[0]
            assigned[pad_index].append(center)
            pad_bbox = pads[pad_index][1]
            if not _model_pad_contains(pad_bbox, region_bbox, tolerance=0.025):
                _finding(
                    findings,
                    "model_terminal_outside_pad",
                    "error",
                    f"model.pad.{number}",
                    "terminal region bbox is not contained by its numbered "
                    "pad bbox within 0.025 mm",
                    model_sha256=model_sha256,
                )
                _finding(
                    findings,
                    "model_terminal_mismatch",
                    "error",
                    f"model.pad.{number}",
                    "PartSpec terminal geometry does not match the same-numbered footprint pad",
                    model_sha256=model_sha256,
                )
            continue
        containing = [
            index
            for index, (_, pad_bbox) in enumerate(pads)
            if _model_pad_contains(pad_bbox, (*center, *center))
        ]
        if not containing:
            _finding(
                findings,
                "model_terminal_unmatched",
                "error",
                "model.terminals",
                f"terminal region centered at {center} lies inside no copper pad",
                model_sha256=model_sha256,
            )
            continue
        if len(containing) > 1:
            _finding(
                findings,
                "model_terminals_unseparable",
                "error",
                "model.terminals",
                f"terminal region center at {center} lies inside multiple copper pads",
                model_sha256=model_sha256,
            )
            continue
        pad_index = containing[0]
        assigned[pad_index].append(center)
        if not _model_pad_contains(pads[pad_index][1], region_bbox, tolerance=0.025):
            _finding(
                findings,
                "model_terminal_outside_pad",
                "error",
                f"model.pad.{pads[pad_index][0].number}",
                "terminal region bbox is not contained by its pad bbox within 0.025 mm",
                model_sha256=model_sha256,
            )
    for index, (pad, _) in enumerate(pads):
        if not assigned[index]:
            _finding(
                findings,
                "model_pad_unmatched",
                "error",
                f"model.pad.{pad.number}",
                "copper pad contains no terminal region center",
                model_sha256=model_sha256,
            )
        elif len(assigned[index]) > 1:
            _finding(
                findings,
                "model_terminals_unseparable",
                "error",
                f"model.pad.{pad.number}",
                "multiple terminal region centers map to one copper pad",
                model_sha256=model_sha256,
            )

    pitch_dimension = spec.package.pitch
    pitch = None
    if pitch_dimension is not None:
        pitch = pitch_dimension.nom
        if pitch is None and pitch_dimension.min is not None and pitch_dimension.max is not None:
            pitch = (pitch_dimension.min + pitch_dimension.max) / 2
    body_width = body_bbox[3] - body_bbox[0]
    body_length = body_bbox[4] - body_bbox[1]
    if pitch is None or pitch <= 0:
        return
    if terminal_bindings is not None:
        footprint_rows: dict[tuple[str, float], set[float]] = {}
        for number in terminal_bindings:
            numbered_pads = [pad for pad, _ in pads if pad.number == number]
            if len(numbered_pads) != 1:
                continue
            pad = numbered_pads[0]
            x_distance = abs(pad.x) - body_width / 2
            y_distance = abs(pad.y) - body_length / 2
            side, row_position, position = (
                ("y", pad.y, pad.x) if y_distance >= x_distance else ("x", pad.x, pad.y)
            )
            footprint_rows.setdefault((side, round(row_position, 4)), set()).add(round(position, 4))
        if any(
            abs((right - left) - pitch) > 0.005
            for positions in footprint_rows.values()
            for left, right in pairwise(sorted(positions))
        ):
            _finding(
                findings,
                "model_pitch",
                "error",
                "model.terminals",
                f"numbered footprint terminal spacing does not match nominal pitch {pitch} mm",
                model_sha256=model_sha256,
            )
    rows: dict[tuple[str, float], set[float]] = {}
    for index in range(len(pads)):
        if len(assigned[index]) != 1:
            continue
        x, y = assigned[index][0]
        x_distance = abs(x) - body_width / 2
        y_distance = abs(y) - body_length / 2
        side = "y" if y_distance >= x_distance else "x"
        key, position = ((side, round(y, 4)), x) if side == "y" else ((side, round(x, 4)), y)
        rows.setdefault(key, set()).add(round(position, 4))
    for positions in rows.values():
        ordered = sorted(positions)
        if any(abs((right - left) - pitch) > 0.01 for left, right in pairwise(ordered)):
            _finding(
                findings,
                "model_pitch",
                "error",
                "model.terminals",
                f"terminal center spacing does not match nominal pitch {pitch} mm",
                model_sha256=model_sha256,
            )
            break


def _single_solid_body_bbox(
    shape: Shape,
    overall_bbox: tuple[float, float, float, float, float, float],
) -> Bounds | None:
    from . import occt

    top = overall_bbox[5]
    top_regions = occt.slab_regions(shape, top - 0.02, top - 0.01)
    if not top_regions:
        return None
    top_region = max(top_regions, key=lambda region: region.area)
    body_xy = top_region.bbox_xy
    body_area = top_region.area
    body_width = body_xy[2] - body_xy[0]
    body_length = body_xy[3] - body_xy[1]
    if body_area <= 0 or body_width <= 0 or body_length <= 0:
        return None

    bottom: float | None = None
    cursor = overall_bbox[2]
    while cursor < top - 0.01:
        upper = min(cursor + 0.01, top - 0.01)
        if upper - cursor < 1e-6:
            break
        regions = occt.slab_regions(shape, cursor, upper)
        for region in regions:
            x_min, y_min, x_max, y_max = region.bbox_xy
            if (
                region.area >= body_area * 0.5
                and x_max - x_min >= body_width * 0.7
                and y_max - y_min >= body_length * 0.7
            ):
                bottom = cursor
                break
        if bottom is not None:
            break
        cursor = upper
    if bottom is None:
        return None
    return occt.Bounds(
        body_xy[0],
        body_xy[1],
        bottom,
        body_xy[2],
        body_xy[3],
        top,
    )


def _imported_model_provenance(path: Path) -> ProvenanceEntry | None:
    resolved = path.resolve(strict=True)
    digest = _sha256(resolved)
    if digest is None:
        return None
    for root in resolved.parents:
        if not (root / "provenance.json").is_file():
            continue
        relative = resolved.relative_to(root).as_posix()
        provenance = load_library_provenance(root)
        return next(
            (
                entry
                for entry in provenance.entries
                if entry.artifact == "model3d" and entry.path == relative and entry.sha256 == digest
            ),
            None,
        )
    return None


def _cross_check_model_geometry(path: Path) -> ModelCrossCheckGeometry:
    from . import occt

    resolved = path.resolve(strict=True)
    digest = _sha256(resolved)
    if digest is None:
        raise ValueError("model could not be hashed")
    shape = occt.read_step(resolved)
    facts = occt.inspect(shape)
    overall_bbox = _model_bbox(facts)
    if (
        not facts.valid
        or facts.units != "mm"
        or not facts.solids
        or overall_bbox is None
        or any(not solid.closed_shell or solid.volume <= 0 for solid in facts.solids)
    ):
        raise ValueError("model must contain valid closed solids in millimetres")
    body_bbox = max(facts.solids, key=lambda solid: solid.volume).bbox
    if facts.solid_count == 1:
        inferred = _single_solid_body_bbox(shape, overall_bbox)
        if inferred is None:
            raise ValueError("model body bounds cannot be separated from its terminals")
        body_bbox = inferred
    regions = occt.slab_regions(shape, 0.0, 0.02)
    terminal_centers = sorted(
        {
            (
                round((region.bbox_xy[0] + region.bbox_xy[2]) / 2, 5),
                round((region.bbox_xy[1] + region.bbox_xy[3]) / 2, 5),
            )
            for region in regions
        }
    )
    marker = occt.pin1_marker(shape, body_bbox)
    pin1_quadrant = marker.quadrant if marker is not None else None
    if pin1_quadrant is None:
        pin1_quadrant = occt.face_color_marker(resolved, body_bbox)
    return ModelCrossCheckGeometry(
        path=resolved,
        sha256=digest,
        body_extents_mm=(
            body_bbox.x_max - body_bbox.x_min,
            body_bbox.y_max - body_bbox.y_min,
            body_bbox.z_max - body_bbox.z_min,
        ),
        terminal_centers_xy_mm=terminal_centers,
        pin1_quadrant=pin1_quadrant,
    )


def _terminal_centers_match(
    left: list[tuple[float, float]],
    right: list[tuple[float, float]],
    tolerance_mm: float,
) -> bool:
    if len(left) != len(right):
        return False
    neighbors = [
        [
            right_index
            for right_index, (right_x, right_y) in enumerate(right)
            if math.hypot(left_x - right_x, left_y - right_y) <= tolerance_mm
        ]
        for left_x, left_y in left
    ]
    matched: dict[int, int] = {}

    def augment(left_index: int, visited: set[int]) -> bool:
        for right_index in neighbors[left_index]:
            if right_index in visited:
                continue
            visited.add(right_index)
            previous = matched.get(right_index)
            if previous is None or augment(previous, visited):
                matched[right_index] = left_index
                return True
        return False

    for left_index in sorted(range(len(left)), key=lambda index: len(neighbors[index])):
        if not neighbors[left_index] or not augment(left_index, set()):
            return False
    return True


def cross_check_models(
    generated: GeneratedModel | Path,
    imported: Path,
    spec: PartSpec,
) -> ModelCrossCheckReport:
    generated_path = generated.step_path if isinstance(generated, GeneratedModel) else generated
    findings: list[ModelCrossCheckFinding] = []
    generated_geometry: ModelCrossCheckGeometry | None = None
    imported_geometry: ModelCrossCheckGeometry | None = None
    imported_provenance: ProvenanceEntry | None = None
    try:
        generated_geometry = _cross_check_model_geometry(generated_path)
    except Exception as exc:
        findings.append(
            ModelCrossCheckFinding(
                message=f"generated STEP model could not be inspected: {exc}",
            )
        )
    try:
        imported_provenance = _imported_model_provenance(imported)
        if imported_provenance is None:
            raise ValueError("no matching hash-bound libsource provenance entry")
        imported_geometry = _cross_check_model_geometry(imported)
    except Exception as exc:
        findings.append(
            ModelCrossCheckFinding(
                message=f"imported STEP model could not be verified: {exc}",
            )
        )
    if generated_geometry is not None and imported_geometry is not None:
        if any(
            abs(left - right) > 0.05
            for left, right in zip(
                generated_geometry.body_extents_mm,
                imported_geometry.body_extents_mm,
                strict=True,
            )
        ):
            findings.append(
                ModelCrossCheckFinding(
                    message="generated and imported body extents differ by more than 0.05 mm",
                )
            )
        if not _terminal_centers_match(
            generated_geometry.terminal_centers_xy_mm,
            imported_geometry.terminal_centers_xy_mm,
            0.05,
        ):
            findings.append(
                ModelCrossCheckFinding(
                    message=(
                        "generated and imported terminal center sets differ by more than 0.05 mm"
                    ),
                )
            )
        if (
            spec.package.family != "chip"
            and (
                generated_geometry.pin1_quadrant is None or imported_geometry.pin1_quadrant is None
            )
        ) or generated_geometry.pin1_quadrant != imported_geometry.pin1_quadrant:
            findings.append(
                ModelCrossCheckFinding(
                    message="generated and imported pin-1 quadrants do not agree",
                )
            )
    return ModelCrossCheckReport(
        passed=not findings,
        generated=generated_geometry,
        imported=imported_geometry,
        imported_provenance=imported_provenance,
        findings=findings,
    )


def _verify_model_geometry(
    spec: PartSpec,
    footprint: FootprintDef,
    footprint_path: Path,
    model: ModelRef,
    resolved: Path | None,
    model_sha256: str | None,
    tolerance_mm: float,
    findings: list[VerifyFinding],
) -> VerifiedModelInspection | None:
    path = model.path
    if Path(path).suffix.casefold() not in {".step", ".stp"}:
        _finding(
            findings,
            "model_format",
            "error",
            f"model.{path}",
            "3D model must use STEP .step or .stp format",
            model_sha256=model_sha256,
        )
    if (
        model.offset != (0.0, 0.0, 0.0)
        or model.rotate != (0.0, 0.0, 0.0)
        or model.scale != (1.0, 1.0, 1.0)
    ):
        _finding(
            findings,
            "model_transform_not_identity",
            "error",
            f"model.{path}",
            "3D model offset and rotation must be zero and scale must be one",
            model_sha256=model_sha256,
        )
    if resolved is None or model_sha256 is None:
        return None
    try:
        from . import occt

        shape = occt.read_step(resolved)
        facts = occt.inspect(shape)
    except Exception as exc:
        _finding(
            findings,
            "model_inspection_unavailable",
            "error",
            f"model.{path}",
            f"STEP inspection failed: {exc}",
            model_sha256=model_sha256,
        )
        return None

    total_volume = sum(solid.volume for solid in facts.solids)
    overall_bbox = _model_bbox(facts)
    if (
        not facts.valid
        or facts.solid_count == 0
        or facts.units != "mm"
        or any(not solid.closed_shell or solid.volume <= 0 for solid in facts.solids)
    ):
        _finding(
            findings,
            "model_invalid",
            "error",
            f"model.{path}",
            "model must contain valid positive-volume closed solids in millimetres",
            model_sha256=model_sha256,
        )
    try:
        with tempfile.TemporaryDirectory(prefix="circuit-model-roundtrip-") as directory:
            roundtrip_path = Path(directory) / "roundtrip.step"
            occt.write_step(shape, roundtrip_path, product_name=Path(path).stem)
            roundtrip_shape = occt.read_step(roundtrip_path)
            roundtrip = occt.inspect(roundtrip_shape)
            roundtrip_bbox = _model_bbox(roundtrip)
            original_volume = total_volume
            roundtrip_volume = sum(solid.volume for solid in roundtrip.solids)
            volume_changed = (
                abs(roundtrip_volume - original_volume) / max(abs(original_volume), 1e-12) > 1e-6
            )
            bbox_changed = (
                overall_bbox is None
                or roundtrip_bbox is None
                or any(
                    abs(left - right) > 1e-6
                    for left, right in zip(overall_bbox, roundtrip_bbox, strict=True)
                )
            )
            if facts.solid_count != roundtrip.solid_count or volume_changed or bbox_changed:
                raise ValueError("solid count, total volume, or bounding box changed")
    except Exception as exc:
        _finding(
            findings,
            "model_roundtrip",
            "error",
            f"model.{path}",
            f"STEP write/read roundtrip changed or failed: {exc}",
            model_sha256=model_sha256,
        )

    if not facts.solids or overall_bbox is None:
        return None
    body_solid = max(facts.solids, key=lambda solid: solid.volume)
    body_bbox = body_solid.bbox
    body_inferred = True
    if facts.solid_count == 1:
        try:
            inferred_body_bbox = _single_solid_body_bbox(shape, overall_bbox)
        except Exception as exc:
            _finding(
                findings,
                "model_inspection_unavailable",
                "error",
                f"model.{path}",
                f"single-solid body inspection failed: {exc}",
                model_sha256=model_sha256,
            )
            return None
        if inferred_body_bbox is None:
            body_inferred = False
        else:
            body_bbox = inferred_body_bbox
    body_values = (
        body_bbox.x_min,
        body_bbox.y_min,
        body_bbox.z_min,
        body_bbox.x_max,
        body_bbox.y_max,
        body_bbox.z_max,
    )
    body_actual = (
        body_bbox.x_max - body_bbox.x_min,
        body_bbox.y_max - body_bbox.y_min,
    )
    dimensions = (
        (
            (spec.package.body_length, body_actual[0]),
            (spec.package.body_width, body_actual[1]),
        )
        if spec.package.family == "chip"
        else (
            (spec.package.body_width, body_actual[0]),
            (spec.package.body_length, body_actual[1]),
        )
    )
    dimension_mismatch = any(
        (lower is not None and actual < lower - 1e-6)
        or (upper is not None and actual > upper + 1e-6)
        for dimension, actual in dimensions
        for lower, upper in (_model_dimension_bounds(dimension, tolerance_mm),)
    )
    if dimension_mismatch or not body_inferred:
        _finding(
            findings,
            "model_body_dimension",
            "error",
            f"model.{path}",
            "model body X/Y limits fail or the body could not be isolated",
            model_sha256=model_sha256,
        )
    if abs(overall_bbox[2]) > 0.01:
        _finding(
            findings,
            "model_body_dimension",
            "error",
            f"model.{path}",
            "model overall bottom Z must be 0 ±0.01 mm",
            model_sha256=model_sha256,
        )
    height_lower, height_upper = _model_dimension_bounds(spec.package.height, tolerance_mm)
    if (height_lower is not None and overall_bbox[5] < height_lower - 1e-6) or (
        height_upper is not None and overall_bbox[5] > height_upper + 1e-6
    ):
        _finding(
            findings,
            "model_height",
            "error",
            f"model.{path}",
            "model overall z_max is outside the PartSpec seated-height limits",
            model_sha256=model_sha256,
        )

    body_bounds = occt.Bounds(*body_values)
    geometric_marker = occt.pin1_marker(shape, body_bounds)
    try:
        color_marker = occt.face_color_marker(resolved, body_bounds)
    except Exception:
        color_marker = None
    marker = geometric_marker.quadrant if geometric_marker is not None else None
    if spec.package.family != "chip":
        mismatched_markers = [
            value
            for value in (marker, color_marker)
            if value is not None and value != spec.package.pin1_corner
        ]
        if mismatched_markers:
            _finding(
                findings,
                "model_pin1_mismatch",
                "error",
                f"model.{path}",
                f"model pin-1 marker disagrees with {spec.package.pin1_corner}",
                model_sha256=model_sha256,
            )
        elif marker is None and color_marker is None:
            _finding(
                findings,
                "model_pin1_unverifiable",
                "error",
                f"model.{path}",
                "polarized package has no detectable geometric or color pin-1 marker",
                model_sha256=model_sha256,
            )

    regions = occt.slab_regions(shape, 0.0, 0.02)
    terminal_bindings = _generated_terminal_bindings(
        spec,
        resolved,
        model_sha256,
        findings,
    )
    _check_model_terminals(
        spec,
        footprint,
        regions,
        (
            body_bbox.x_min,
            body_bbox.y_min,
            body_bbox.z_min,
            body_bbox.x_max,
            body_bbox.y_max,
            body_bbox.z_max,
        ),
        model_sha256,
        findings,
        terminal_bindings,
    )
    courtyard = _graphic_box(
        [graphic for graphic in footprint.graphics if graphic.layer == "F.CrtYd"]
    )
    model_xy = (overall_bbox[0], overall_bbox[1], overall_bbox[3], overall_bbox[4])
    if courtyard is None or not _contains(courtyard, model_xy, 1e-6):
        _finding(
            findings,
            "model_courtyard",
            "error",
            f"model.{path}",
            "F.CrtYd does not enclose the union of model body and terminal bounds",
            model_sha256=model_sha256,
        )
    fab = _graphic_box([graphic for graphic in footprint.graphics if graphic.layer == "F.Fab"])
    body_xy = (body_bbox.x_min, body_bbox.y_min, body_bbox.x_max, body_bbox.y_max)
    if fab is None or any(
        abs(actual - expected) > 0.1 for actual, expected in zip(fab, body_xy, strict=True)
    ):
        _finding(
            findings,
            "model_fab_outline",
            "warning",
            f"model.{path}",
            "F.Fab outline differs from the inspected model body by more than 0.1 mm",
            model_sha256=model_sha256,
        )
    return VerifiedModelInspection(
        solid_count=facts.solid_count,
        total_volume_mm3=total_volume,
        bbox_mm=overall_bbox,
        units=facts.units,
        valid=facts.valid,
        closed_shells=tuple(solid.closed_shell for solid in facts.solids),
        pin1_marker=marker,
        pin1_color_marker=color_marker,
    )


def inspect_model_file(
    spec: PartSpec,
    footprint_path: Path,
    model_path: Path,
    *,
    tolerance_mm: float = 0.02,
) -> ModelInspectionReport:
    """Inspect one STEP file against its PartSpec and footprint geometry."""

    if not math.isfinite(tolerance_mm) or tolerance_mm < 0:
        raise ValueError("tolerance_mm must be finite and non-negative")
    resolved: Path | None = None
    try:
        candidate = model_path.resolve(strict=True)
    except OSError:
        candidate = None
    if candidate is not None and candidate.is_file():
        resolved = candidate
    model_sha256 = _sha256(resolved) if resolved is not None else None
    findings: list[VerifyFinding] = []
    facts: VerifiedModelInspection | None = None
    try:
        footprint = parse_footprint(footprint_path)
    except (LibItemError, OSError) as exc:
        footprint = None
        _finding(
            findings,
            "model_footprint_unavailable",
            "error",
            str(footprint_path),
            f"footprint inspection failed: {exc}",
            model_sha256=model_sha256,
        )
    if resolved is None:
        _finding(
            findings,
            "model_missing",
            "error",
            str(model_path),
            "STEP model file is missing or unreadable",
            model_sha256=model_sha256,
        )
    elif footprint is not None:
        model = ModelRef(
            path=str(model_path),
            offset=(0.0, 0.0, 0.0),
            scale=(1.0, 1.0, 1.0),
            rotate=(0.0, 0.0, 0.0),
        )
        facts = _verify_model_geometry(
            spec,
            footprint,
            footprint_path,
            model,
            resolved,
            model_sha256,
            tolerance_mm,
            findings,
        )
    return ModelInspectionReport(
        verdict="fail" if any(item.severity == "error" for item in findings) else "pass",
        path=model_path,
        sha256=model_sha256,
        facts=facts,
        findings=findings,
    )


def _check_models(
    spec: PartSpec,
    footprint: FootprintDef | None,
    footprint_path: Path,
    library_dir: Path | None,
    rules: EffectiveRules,
    model_required: bool,
    run_export_oracle: bool,
    tolerance_mm: float,
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
        model_sha256 = _sha256(resolved) if resolved is not None else None
        inspection = _verify_model_geometry(
            spec,
            footprint,
            footprint_path,
            model,
            resolved,
            model_sha256,
            tolerance_mm,
            findings,
        )
        export_report: ModelExportReport | None = None
        if run_export_oracle:
            with tempfile.TemporaryDirectory(prefix="circuit-model-export-") as temporary_name:
                export_report = verify_model_export(
                    spec,
                    footprint_path,
                    model_reference=str(resolved) if resolved is not None else expanded,
                    model_path=resolved,
                    rules=rules,
                    out_dir=Path(temporary_name),
                )
            for item in export_report.findings:
                _finding(
                    findings,
                    item.code,
                    item.severity,
                    f"model.{path}",
                    item.message,
                    model_sha256=model_sha256,
                )
        result.append(
            VerifiedModel(
                path=path,
                resolved=resolved is not None,
                sha256=model_sha256,
                inspection=inspection,
                export_oracle=export_report,
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


def _has_alternative_evidence(value: object) -> bool:
    if isinstance(value, dict):
        record = cast(dict[str, object], value)
        if record.get("alternative_evidence") is not None:
            return True
        return any(_has_alternative_evidence(item) for item in record.values())
    if isinstance(value, list):
        return any(_has_alternative_evidence(item) for item in cast(list[object], value))
    return False


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
    rules: EffectiveRules | None = None,
    tolerance_mm: float = 0.02,
    model_required: bool = True,
    run_export_oracle: bool = True,
    klc: bool = False,
    test_board: bool = True,
    pin_source_path: Path | None = None,
    pin_sources: Sequence[PinSourceInput | Path] | None = None,
    output_path: Path | None = None,
) -> LibraryVerification:
    """Verify a library part against its current PartSpec and generated land pattern."""

    if not math.isfinite(tolerance_mm) or tolerance_mm < 0:
        raise ValueError("tolerance_mm must be finite and non-negative")
    rules = rules or load_rules("builtin:ipc7351b", Path("."))
    findings: list[VerifyFinding] = []
    spec_hash = part_spec_sha256(spec_path) if spec_path.is_file() else ""
    pin_source_comparison: PinSourceComparison | None = None
    descriptor_inputs: list[PinSourceInput] = []
    if pin_sources is not None:
        for source in pin_sources:
            source_input = (
                source if isinstance(source, PinSourceInput) else PinSourceInput(path=source)
            )
            duplicate_index = next(
                (
                    index
                    for index, existing in enumerate(descriptor_inputs)
                    if existing.path.resolve() == source_input.path.resolve()
                ),
                None,
            )
            if duplicate_index is None:
                descriptor_inputs.append(source_input)
            else:
                descriptor_inputs[duplicate_index] = source_input
    source_inputs: list[PinSourceInput] = []
    if pin_source_path is not None:
        source_inputs.append(PinSourceInput(path=pin_source_path))
    for source_input in descriptor_inputs:
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(source_inputs)
                if existing.path.resolve() == source_input.path.resolve()
            ),
            None,
        )
        if duplicate_index is None:
            source_inputs.append(source_input)
        else:
            source_inputs[duplicate_index] = source_input
    source_hashes: list[str | None] = [_sha256(item.path) for item in source_inputs]
    parsed_sources: list[PinSource] = []
    for source_input in source_inputs:
        try:
            parsed_sources.append(
                parse_pin_source(
                    source_input.path,
                    kind=source_input.kind,
                    pinout_name=source_input.pinout_name,
                    derived_from=source_input.derived_from,
                )
            )
        except (OSError, ValueError) as exc:
            _finding(
                findings,
                "pin_source_invalid",
                "error",
                str(source_input.path),
                f"Pin source could not be parsed: {exc}",
            )
    if not parsed_sources:
        _finding(
            findings,
            "pin_source_single",
            "warning",
            "pin_sources",
            "only the PartSpec cell-bound pin source is available",
        )
    else:
        class_a = source_from_part_spec(
            spec,
            spec_sha256=spec_hash,
            spec_path=spec_path,
        )
        pin_source_comparison = compare_pin_sources(class_a, parsed_sources)
        if len(pin_source_comparison.independent_lineages) < 2:
            _finding(
                findings,
                "pin_source_single",
                "warning",
                "pin_sources",
                "fewer than two independent pin-source lineages are available",
            )
        for item in pin_source_comparison.findings:
            subject = (
                "pin_sources.identity"
                if item.code == "pin_source_identity_mismatch"
                else f"pin {item.number}"
            )
            _finding(findings, item.code, "error", subject, item.message)
    check: PartSpecReport | None = None
    check_matches_spec = False
    try:
        requires_fresh_human_evidence = spec.substitution is not None or _has_alternative_evidence(
            spec.model_dump(mode="python")
        )
        if spec_check_path is not None and not requires_fresh_human_evidence:
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
        check_matches_spec = check.part_spec_sha256 == spec_hash
        check_ok = check.verdict == "pass" and check_matches_spec
        check_detail = "" if check_ok else "fresh PartSpec check failed or is stale"
    except Exception as exc:
        check_ok = False
        check_detail = str(exc)
    if check is not None and check_matches_spec:
        for item in check.findings:
            subject = item.field
            if item.page is not None:
                subject = f"{subject} (page {item.page})"
            _finding(findings, item.code, item.severity, subject, item.message)
    if not check_ok:
        _finding(
            findings,
            "part_spec_unchecked",
            "error",
            "part_spec",
            check_detail or "PartSpec check is missing, failed, or stale",
        )
    if spec.substitution is not None and check is not None and check.substitute_permit is not None:
        required_scopes = {"land_pattern"}
        if model_required:
            required_scopes.add("model3d")
        alternative_land_pattern = any(
            item.code == "alternative_evidence_used" and item.field.startswith("land_pattern.")
            for item in check.findings
        )
        for scope in sorted(required_scopes - set(check.substitute_permit.granted_scope)):
            if scope == "land_pattern" and alternative_land_pattern:
                continue
            _finding(
                findings,
                "substitute_scope_exceeded",
                "error",
                f"substitution.{scope}",
                f"{scope} is outside the substitute permission and lacks granted "
                "alternative evidence",
            )

    _check_authoring_consensus(
        spec,
        spec_path=spec_path,
        library_dir=library_dir,
        findings=findings,
    )

    try:
        symbol = parse_symbol(symbol_lib, symbol_name)
    except (LibItemError, OSError):
        symbol = None
    try:
        footprint = parse_footprint(footprint_path)
    except (LibItemError, OSError):
        footprint = None
    lineage, lineage_base, lineage_valid = _validate_lineage(
        footprint_path,
        footprint,
        library_dir,
        findings,
    )
    footprint_name = footprint.name if footprint is not None else footprint_path.stem
    report_base = library_dir if library_dir is not None else footprint_path.parent
    target = output_path or report_base / "verification" / f"{footprint_name}.verification.json"
    test_board_report: TestBoard | None = None
    _check_symbol(spec, symbol, symbol_lib, footprint_name, library_dir, findings)
    _check_vision_comparisons(
        spec,
        spec_path=spec_path,
        symbol_lib=symbol_lib,
        footprint_path=footprint_path,
        spec_hash=spec_hash,
        findings=findings,
    )
    _check_pinout_geometry(spec, check, symbol, None, findings)
    if klc:
        _check_klc("symbol", symbol_lib, findings)
        _check_klc("footprint", footprint_path, findings)
    if test_board:
        test_board_dir = target.parent / f"{target.stem}.test-board"
        try:
            assert_safe_destination(test_board_dir)
            test_board_report = build_test_board(
                spec,
                symbol_lib,
                symbol_name,
                footprint_path,
                rules,
                test_board_dir,
            )
        except (LibrarySourceError, OSError) as exc:
            _finding(
                findings,
                "testboard_output_unavailable",
                "error",
                str(test_board_dir),
                str(exc),
            )
        if test_board_report is not None:
            for item in test_board_report.findings:
                _finding(findings, item.code, item.severity, item.subject, item.message)

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
        findings.extend(functional_findings(spec, footprint, rules))
        _check_pad_geometry(
            spec,
            footprint,
            reference,
            tolerance_mm,
            rules.fabrication_tolerance,
            lineage_valid,
            findings,
        )
        if lineage_valid and lineage is not None and lineage_base is not None:
            _replace_with_intentional_tuning(
                reference,
                footprint,
                lineage_base,
                lineage,
                tolerance_mm,
                findings,
            )
        _check_pad_types(spec, footprint, findings)
        _check_courtyard_and_fab(spec, footprint, findings)
        _check_silk_clearance(footprint, findings)
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
        spec,
        footprint,
        footprint_path,
        library_dir,
        rules,
        model_required,
        run_export_oracle,
        tolerance_mm,
        findings,
    )
    identity_model: occt.Shape | None = None
    model_failures: list[str] = []
    if footprint is not None:
        for model_ref in footprint.models:
            model_value = model_ref.path
            expansion_root = library_dir.parent if library_dir is not None else None
            model_value = model_value.replace("${KIPRJMOD}", str(expansion_root or ""))
            model_value = model_value.replace("$KIPRJMOD", str(expansion_root or ""))
            expanded_model = os.path.expandvars(model_value)
            if "$" in expanded_model or "${" in expanded_model:
                model_failures.append(f"{model_ref.path}: unresolved model path variables")
                continue
            model_candidate = Path(expanded_model)
            if not model_candidate.is_absolute():
                model_candidate = footprint_path.parent / model_candidate
            try:
                resolved_model = model_candidate.resolve(strict=True)
                identity_model = occt.read_step(resolved_model)
                break
            except (OSError, RuntimeError, ValueError) as exc:
                model_failures.append(f"{model_ref.path}: {exc}")
        if footprint.models and identity_model is None:
            _finding(
                findings,
                "package_identity_verification_unavailable",
                "error",
                "model",
                "no referenced 3D model could be read for package identity verification: "
                + "; ".join(model_failures),
            )

    datasheet_path = Path(spec.datasheet.path)
    if not datasheet_path.is_absolute():
        datasheet_path = spec_path.resolve().parent / datasheet_path
    try:
        resolved_datasheet = datasheet_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        resolved_datasheet = None
        datasheet_error = f"datasheet path could not be resolved: {exc}"
    else:
        datasheet_error = (
            None
            if resolved_datasheet.is_file()
            else f"datasheet path does not refer to a file: {resolved_datasheet}"
        )
    if datasheet_error is not None:
        _finding(
            findings,
            "package_identity_verification_unavailable",
            "error",
            "datasheet",
            datasheet_error,
        )
    elif footprint is not None and resolved_datasheet is not None:
        try:
            findings.extend(
                check_package_identity(
                    spec,
                    footprint,
                    identity_model,
                    pdf_path=resolved_datasheet,
                )
            )
        except (OSError, RuntimeError, ValueError) as exc:
            _finding(
                findings,
                "package_identity_verification_unavailable",
                "error",
                "datasheet",
                f"package identity verification failed closed: {exc}",
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

    source_root = (library_dir or footprint_path.parent.parent).resolve()
    pin_source_sha256 = _sha256(pin_source_path) if pin_source_path is not None else None
    relative_pin_source_path = (
        Path(os.path.relpath(pin_source_path.resolve(), source_root))
        if pin_source_path is not None
        else None
    )
    relative_pin_sources = [
        source_input.model_copy(
            update={"path": Path(os.path.relpath(source_input.path.resolve(), source_root))}
        )
        for source_input in descriptor_inputs
    ]
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
            klc=klc,
            test_board=test_board,
            pin_source_path=relative_pin_source_path,
            pin_source_sha256=pin_source_sha256,
            pin_sources=relative_pin_sources,
            pin_source_sha256s=source_hashes,
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
        test_board=test_board_report,
        pin_source_comparison=pin_source_comparison,
    )
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
