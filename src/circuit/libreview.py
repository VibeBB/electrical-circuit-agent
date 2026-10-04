"""Hash-bound human review packets and approval decisions for library parts."""

from __future__ import annotations

import copy
import hashlib
import html
import json
import math
import os
import random
import re
import secrets
import tempfile
from collections import Counter
from collections.abc import Iterable, Sequence
from contextlib import suppress
from pathlib import Path
from statistics import median
from typing import Any, Literal, cast

import pdfplumber
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from . import (
    advisory,
    authoring,
    confidential,
    datasheet,
    humanrequest,
    kicad_cli,
    mutation,
    pinsource,
    revwatch,
    visionread,
)
from . import pinout as pinout_oracle
from .datasheet import DatasheetExtraction, PageExtraction
from .landpattern import Density, LandPatternResult, compute_land_pattern
from .libitems import FootprintDef, PadDef, SymbolDef, parse_footprint, parse_symbol
from .libverify import LibraryVerification, VerifiedModel, VerifyFinding, verify_library_part
from .lineage import FootprintLineage, lineage_path_for
from .partspec import (
    Dimension,
    PartSpec,
    PartSpecReport,
    Reading,
    SpecFinding,
    check_part_spec,
    load_part_spec,
    part_spec_sha256,
)
from .pinout import PinoutGeometry
from .ruleprofile import EffectiveRules, load_rules

EVENTS_DIR_ENV = "CIRCUIT_AGENT_EVENTS_DIR"
DEFAULT_EVENTS_ROOT = Path(".openhands/agent-canvas/dev_conversations")
PACKET_ID_RE = re.compile(r"^[0-9a-f]{16}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVIEW_LINE_RE = re.compile(r"^CIRCUIT-LIBRARY-REVIEW ([0-9a-f]{16})$")


class BlindQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    prompt: str
    page: int
    bbox: tuple[float, float, float, float] | None
    expected: str
    evidence_field: str | None = None


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["error", "warning", "info"]
    field: str
    message: str
    page: int | None = None


class ReviewPacket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_library_review_packet"]
    packet_id: str
    packet_dir: Path
    approvable: bool
    inputs: dict[str, Any]
    findings: list[ReviewFinding]
    unknowns: list[str]
    agent_request: humanrequest.HumanRequest


class ReviewCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pointer: str
    old: Any
    new: Any
    reason: str
    page: int


class ReviewTrialPlant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: str
    operator: str
    pointer: str
    field: str
    column: Literal["min", "nom", "max"]
    wrong_value: float
    right_value: float
    evidence_crop_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewTrialState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["circuit_review_trial_state"] = "circuit_review_trial_state"
    packet_id: str
    seed: int
    questions: list[BlindQuestion]
    plants: list[ReviewTrialPlant]


class ReviewRegressionCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["circuit_library_regression_case"] = "circuit_library_regression_case"
    case_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    mpn: str = Field(min_length=1)
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pointer: str = Field(min_length=2)
    field: str = Field(min_length=1)
    wrong_value: Any
    right_value: Any
    evidence_crop_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    packet_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packet_id: str
    decision: Literal["approve", "reject"] | None = None
    reviewer: str | None = None
    answers: dict[str, str]
    findings: dict[str, str] = Field(default_factory=dict)
    corrections: list[ReviewCorrection]
    event_path: Path | None = None
    event_sha256: str | None = None
    event_mtime_ns: int = 0
    event_name: str = ""
    valid: bool
    reasons: list[str]
    integrity_valid: bool = True


class ReviewStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_library_review_status"]
    packet_id: str
    state: Literal["approved", "rejected", "pending", "invalid"]
    reasons: list[str]
    decisions: list[ReviewDecision]
    findings: list[ReviewFinding] = Field(default_factory=lambda: cast(list[ReviewFinding], []))


class CorrectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_library_review_correction"]
    applied: bool
    packet_id: str
    applied_pointers: list[str]
    reasons: list[str]


class _CropRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    page: int
    path: str
    sha256: str
    source_png_sha256: str
    bbox: tuple[float, float, float, float]
    crop_bbox: tuple[float, float, float, float]
    scale: float


type _PdfPoint = tuple[float, float]
type _PdfBBox = tuple[float, float, float, float]
type _PdfEdge = tuple[_PdfPoint, _PdfPoint]


class _OverlayGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scale_pt_per_mm: float | None = None
    pad_size_estimate_pt_per_mm: float | None = None
    pitch_estimate_pt_per_mm: float | None = None
    scale_px_per_mm: float | None = None
    anchor_kind: Literal["exposed_pad", "candidate_centroid"] | None = None
    anchor_page_pt: tuple[float, float] | None = None
    anchor_crop_px: tuple[float, float] | None = None
    anchor_footprint_mm: tuple[float, float] | None = None
    candidate_pad_count: int = 0

    @property
    def scale_known(self) -> bool:
        return (
            self.scale_pt_per_mm is not None
            and self.scale_px_per_mm is not None
            and self.anchor_page_pt is not None
            and self.anchor_crop_px is not None
            and self.anchor_footprint_mm is not None
        )


def _overlay_unknown_codes(geometry: _OverlayGeometry) -> list[str]:
    return [] if geometry.scale_known else ["overlay_scale_unknown"]


def packet_id(
    *,
    pdf_sha256: str,
    part_spec_sha256: str,
    symbol_lib_sha256: str,
    symbol_name: str,
    footprint_sha256: str,
    model_sha256s: Iterable[str],
    density: Density,
    tolerance_mm: float,
    model_required: bool,
    authoring_sha256s: Iterable[str] = (),
    lineage_sha256: str | None = None,
    rule_chain_sha256: str | None = None,
    pin_source_sha256: str | None = None,
    pin_source_kind: str | None = None,
    pin_source_inputs: Iterable[dict[str, Any]] = (),
    request_sha256: str | None = None,
) -> str:
    value: dict[str, Any] = {
        "format": 1,
        "pdf_sha256": pdf_sha256,
        "part_spec_sha256": part_spec_sha256,
        "symbol_lib_sha256": symbol_lib_sha256,
        "symbol_name": symbol_name,
        "footprint_sha256": footprint_sha256,
        "model_sha256s": sorted(model_sha256s),
        "density": density,
        "tolerance_mm": tolerance_mm,
        "model_required": model_required,
        "lineage_sha256": lineage_sha256,
        "rule_chain_sha256": rule_chain_sha256,
    }
    authoring_hashes = sorted(authoring_sha256s)
    if authoring_hashes:
        value["authoring_sha256s"] = authoring_hashes
    if pin_source_sha256 is not None:
        value["pin_source_sha256"] = pin_source_sha256
        value["pin_source_kind"] = pin_source_kind
    source_inputs = list(pin_source_inputs)
    if source_inputs:
        value["pin_source_inputs"] = sorted(
            source_inputs,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if request_sha256 is not None:
        value["request_sha256"] = request_sha256
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _matching_agent_request(
    review_root: Path,
    input_hashes: dict[str, Any],
    base_packet_id: str,
) -> humanrequest.HumanRequest | None:
    for review_path in sorted(review_root.glob("*/review.json")):
        try:
            document = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(document, dict):
            continue
        record = cast(dict[str, Any], document)
        stored_inputs = record.get("inputs")
        if not isinstance(stored_inputs, dict) or any(
            cast(dict[str, Any], stored_inputs).get(name) != value
            for name, value in input_hashes.items()
        ):
            continue
        request_value = record.get("agent_request")
        if not isinstance(request_value, dict):
            continue
        try:
            request = humanrequest.HumanRequest.model_validate_json(
                json.dumps(request_value, ensure_ascii=False)
            )
        except ValueError:
            continue
        if (
            request.kind == "library_review"
            and isinstance(request.details, humanrequest.LibraryReviewDetails)
            and request.details.packet_id == base_packet_id
        ):
            return request
    return None


def _matching_project_request(
    project_dir: Path,
    spec: PartSpec,
    input_hashes: dict[str, Any],
    base_packet_id: str,
) -> humanrequest.HumanRequest | None:
    request_dir = humanrequest.request_directory(
        project_dir,
        confidential=spec.datasheet.confidential,
    )
    expected_hashes = {
        "datasheet": input_hashes.get("pdf_sha256"),
        "part_spec": input_hashes.get("part_spec_sha256"),
        "symbol": input_hashes.get("symbol_lib_sha256"),
        "footprint": input_hashes.get("footprint_sha256"),
    }
    expected_hashes = {
        key: value if isinstance(value, str) and SHA256_RE.fullmatch(value) else None
        for key, value in expected_hashes.items()
    }
    matches: list[humanrequest.HumanRequest] = []
    for request_path in sorted(request_dir.glob("*.json")):
        try:
            request = humanrequest.load_request(request_path)
        except humanrequest.HumanRequestError:
            continue
        if (
            request.kind != "library_review"
            or not isinstance(request.details, humanrequest.LibraryReviewDetails)
            or request.details.packet_id != base_packet_id
            or request.subject.manufacturer != spec.manufacturer
            or request.subject.mpn != spec.mpn
        ):
            continue
        stored_hashes = {item.ref: item.sha256 for item in request.evidence}
        if all(stored_hashes.get(key) == value for key, value in expected_hashes.items()):
            matches.append(request)
    return max(matches, key=lambda item: (item.created_at, item.request_id), default=None)


def _build_agent_request(
    spec: PartSpec,
    *,
    base_packet_id: str,
    input_hashes: dict[str, Any],
) -> humanrequest.HumanRequest:
    evidence = [
        humanrequest.RequestEvidence(
            kind="hash",
            ref=label,
            sha256=value if isinstance(value, str) and SHA256_RE.fullmatch(value) else None,
            summary=(
                f"Current {label.replace('_', ' ')} input is bound to this review packet."
                if isinstance(value, str) and SHA256_RE.fullmatch(value)
                else f"No current hash is available for {label.replace('_', ' ')}."
            ),
        )
        for label, value in (
            ("datasheet", input_hashes.get("pdf_sha256")),
            ("part_spec", input_hashes.get("part_spec_sha256")),
            ("symbol", input_hashes.get("symbol_lib_sha256")),
            ("footprint", input_hashes.get("footprint_sha256")),
        )
    ]
    assessment = (
        "The packet presents the current PartSpec, source datasheet, symbol, footprint, model, "
        "and deterministic verification results for human review. These artifacts may still "
        "contain authored mistakes even when hashes and seals agree. Compare each "
        "evidence-backed pin and package claim with the source; this assessment is advisory "
        "and grants no authority by itself."
    )
    return humanrequest.build_request(
        kind="library_review",
        subject={
            "manufacturer": spec.manufacturer,
            "mpn": spec.mpn,
            "revision": spec.datasheet.revision or None,
        },
        reason="Review the evidence-bound library packet before accepting this part.",
        evidence=evidence,
        known=[
            "The packet identity binds the current datasheet, PartSpec, symbol, and footprint.",
            "Hash or seal agreement does not prove that the authored content matches the source.",
        ],
        unknown=[],
        agent_assessment=assessment,
        recommendation="Approve only after the evidence and deterministic findings are reviewed.",
        recommendation_rationale=(
            "Approval is a separate human decision. Reject or request corrections if any pin, "
            "package, land-pattern, or model claim is unsupported or contradictory."
        ),
        alternatives=[
            {
                "option": (
                    "Approve only after the evidence and deterministic findings are reviewed."
                ),
                "risks": ["An overlooked source mismatch could propagate into downstream designs."],
            },
            {
                "option": "Request corrections or additional evidence.",
                "risks": ["Library release is delayed until the discrepancy is resolved."],
            },
        ],
        recommended=0,
        details={
            "kind": "library_review",
            "packet_id": base_packet_id,
            "finding_codes": [],
        },
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _optional_sha256(path: Path) -> str | None:
    try:
        return _sha256(path) if path.is_file() else None
    except OSError:
        return None


def _load_lineage(footprint_path: Path) -> FootprintLineage | None:
    path = lineage_path_for(footprint_path)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return FootprintLineage.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _review_rules(
    lineage: FootprintLineage | None,
    rules_dir: Path,
) -> tuple[EffectiveRules, bool]:
    expected_hash = lineage.base.rule_chain_sha256 if lineage is not None else None
    if expected_hash is None:
        return load_rules("builtin:ipc7351b", rules_dir), True

    profile_ids = ["builtin:ipc7351b", "builtin:kicad-generator"]
    with suppress(OSError):
        profile_ids.extend(path.stem for path in rules_dir.glob("*.json"))
    for profile_id in sorted(set(profile_ids)):
        try:
            rules = load_rules(profile_id, rules_dir)
        except (OSError, ValueError):
            continue
        if rules.chain_sha256 == expected_hash:
            return rules, True
    return load_rules("builtin:ipc7351b", rules_dir), False


def _relative_or_absolute(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def _model_path(
    path: str,
    *,
    footprint_path: Path,
    library_dir: Path | None,
    project_dir: Path,
) -> Path | None:
    resolved = path
    for variable in re.findall(r"\$\{([^}]+)\}", path):
        replacement = str(project_dir) if variable == "KIPRJMOD" else os.environ.get(variable)
        if replacement is None:
            return None
        resolved = resolved.replace("${" + variable + "}", replacement)
    resolved = os.path.expandvars(resolved)
    if "$" in resolved:
        return None
    model = Path(resolved).expanduser()
    if model.is_absolute():
        return model
    base = library_dir if library_dir is not None else footprint_path.parent
    return (base / model).resolve()


def _model_hashes(
    footprint: FootprintDef,
    *,
    footprint_path: Path,
    library_dir: Path | None,
    project_dir: Path,
) -> tuple[list[str], list[VerifiedModel]]:
    hashes: list[str] = []
    records: list[VerifiedModel] = []
    for model in footprint.models:
        path = _model_path(
            model.path,
            footprint_path=footprint_path,
            library_dir=library_dir,
            project_dir=project_dir,
        )
        try:
            digest = _sha256(path) if path is not None and path.is_file() else None
        except OSError:
            digest = None
        if digest is not None:
            hashes.append(digest)
        records.append(
            VerifiedModel(
                path=model.path,
                resolved=digest is not None,
                sha256=digest,
            )
        )
    return sorted(hashes), records


def current_packet_id(
    spec_path: Path,
    *,
    symbol_lib: Path,
    symbol_name: str,
    footprint_path: Path,
    library_dir: Path | None,
    density: Density,
    tolerance_mm: float,
    model_required: bool,
    pin_source_path: Path | None = None,
    pin_sources: Sequence[pinsource.PinSourceInput] | None = None,
) -> str:
    spec = load_part_spec(spec_path)
    spec_dir = spec_path.resolve().parent
    pdf_path = _relative_or_absolute(spec_dir, spec.datasheet.path)
    footprint = parse_footprint(footprint_path)
    model_hashes, _ = _model_hashes(
        footprint,
        footprint_path=footprint_path,
        library_dir=library_dir,
        project_dir=(library_dir.parent if library_dir is not None else spec_dir),
    )
    authoring_hashes: list[str] = []
    if spec.authoring is not None:
        run_dir = (spec_dir / spec.authoring).resolve()
        if not run_dir.is_relative_to(spec_dir):
            raise ValueError("authoring run path escapes the PartSpec directory")
        comparison = authoring.compare_runs(run_dir)
        authoring_hashes = sorted(comparison.sealed.values())
    lineage = _load_lineage(footprint_path)
    rules, _ = _review_rules(
        lineage,
        (library_dir / "rules") if library_dir is not None else (spec_dir / "library" / "rules"),
    )
    pin_source_inputs = _combined_pin_source_inputs(pin_source_path, pin_sources)
    pin_source_records = _pin_source_input_records(pin_source_inputs)
    input_hashes: dict[str, Any] = {
        "pdf_sha256": _sha256(pdf_path),
        "part_spec_sha256": part_spec_sha256(spec_path),
        "symbol_lib_sha256": _sha256(symbol_lib),
        "symbol_name": symbol_name,
        "footprint_sha256": _sha256(footprint_path),
        "model_sha256s": sorted(model_hashes),
        "density": density,
        "tolerance_mm": tolerance_mm,
        "model_required": model_required,
        "lineage_sha256": _optional_sha256(lineage_path_for(footprint_path)),
        "rule_chain_sha256": rules.chain_sha256,
        "authoring_sha256s": authoring_hashes,
        "pin_source_path": str(pin_source_path.resolve()) if pin_source_path is not None else None,
        "pin_source_sha256": (
            _optional_sha256(pin_source_path) if pin_source_path is not None else None
        ),
        "pin_source_kind": (
            pin_source_path.suffix.casefold() if pin_source_path is not None else None
        ),
    }
    if pin_sources:
        input_hashes["pin_sources"] = pin_source_records
    base_packet_id = packet_id(
        pdf_sha256=_sha256(pdf_path),
        part_spec_sha256=part_spec_sha256(spec_path),
        symbol_lib_sha256=_sha256(symbol_lib),
        symbol_name=symbol_name,
        footprint_sha256=_sha256(footprint_path),
        model_sha256s=model_hashes,
        density=density,
        tolerance_mm=tolerance_mm,
        model_required=model_required,
        authoring_sha256s=authoring_hashes,
        lineage_sha256=_optional_sha256(lineage_path_for(footprint_path)),
        rule_chain_sha256=rules.chain_sha256,
        pin_source_sha256=(
            _optional_sha256(pin_source_path) if pin_source_path is not None else None
        ),
        pin_source_kind=(
            pin_source_path.suffix.casefold() if pin_source_path is not None else None
        ),
        pin_source_inputs=pin_source_records if pin_sources else (),
    )
    project_root = confidential.project_root_for(spec_path)
    review_library_dir = library_dir if library_dir is not None else spec_dir / "library"
    if spec.datasheet.confidential:
        private_root = confidential.ensure_confidential_store(project_root)
        review_library_dir = private_root / "library"
    request = _matching_agent_request(
        review_library_dir / "reviews" / _safe_field(spec.mpn),
        input_hashes,
        base_packet_id,
    )
    if request is None:
        request = _matching_project_request(
            project_root,
            spec,
            input_hashes,
            base_packet_id,
        )
    if request is None:
        return base_packet_id
    return packet_id(
        pdf_sha256=_sha256(pdf_path),
        part_spec_sha256=part_spec_sha256(spec_path),
        symbol_lib_sha256=_sha256(symbol_lib),
        symbol_name=symbol_name,
        footprint_sha256=_sha256(footprint_path),
        model_sha256s=model_hashes,
        density=density,
        tolerance_mm=tolerance_mm,
        model_required=model_required,
        authoring_sha256s=authoring_hashes,
        lineage_sha256=_optional_sha256(lineage_path_for(footprint_path)),
        rule_chain_sha256=rules.chain_sha256,
        pin_source_sha256=input_hashes["pin_source_sha256"],
        pin_source_kind=input_hashes["pin_source_kind"],
        pin_source_inputs=pin_source_records if pin_sources else (),
        request_sha256=request.request_sha256,
    )


def _readings(spec: PartSpec) -> list[tuple[str, Reading, Dimension | None]]:
    values: list[tuple[str, Reading, Dimension | None]] = []
    package = spec.package
    for name in (
        "pitch",
        "body_length",
        "body_width",
        "height",
        "standoff",
        "lead_span",
        "lead_length",
        "lead_width",
    ):
        dimension = getattr(package, name)
        if dimension is not None:
            values.append((f"package.{name}", dimension.reading, dimension))
    if package.exposed_pad is not None:
        for name in ("length", "width"):
            dimension = getattr(package.exposed_pad, name)
            values.append((f"package.exposed_pad.{name}", dimension.reading, dimension))
    if spec.land_pattern is not None:
        values.extend(
            (f"land_pattern.dimensions.{name}", dimension.reading, dimension)
            for name, dimension in spec.land_pattern.dimensions.items()
        )
    values.append(("package.pin1_reading", package.pin1_reading, None))
    values.extend((f"pins.{pin.number}.reading", pin.reading, None) for pin in spec.pins)
    values.extend(
        (f"orderable.{index}.reading", variant.reading, None)
        for index, variant in enumerate(spec.orderable)
    )
    if spec.pinout is not None:
        values.append(("pinout.view_reading", spec.pinout.view_reading, None))
    return values


def _alternative_evidence_review_data(
    spec: PartSpec,
    project_root: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    requests: dict[str, dict[str, Any]] = {}
    unknown_fields: set[str] = set()
    for field, reading, _ in _readings(spec):
        request_id = reading.alternative_evidence
        if request_id is None:
            continue
        if request_id in requests:
            requests[request_id]["fields"].append(field)
            continue
        request_path = humanrequest.find_request_path(project_root, request_id)
        if not request_path.is_file():
            continue
        try:
            request = humanrequest.load_request(request_path)
        except humanrequest.HumanRequestError:
            continue
        if not isinstance(request.details, humanrequest.AlternativeEvidenceDetails):
            continue
        details = request.details
        requests[request_id] = {
            "request_id": request_id,
            "evidence_kind": details.evidence_kind,
            "fields": [],
            "files": [item.model_dump(mode="json") for item in details.files],
            "covers": details.covers,
            "unknown_fields": details.unknown_fields,
            "measurement_method": details.measurement_method,
        }
        unknown_fields.update(details.unknown_fields)
        requests[request_id]["fields"].append(field)
    return list(requests.values()), sorted(unknown_fields)


def _authoring_question_region(
    spec: PartSpec,
    pointer: str,
) -> tuple[int, tuple[float, float, float, float] | None, str]:
    tokens = [
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer.removeprefix("/").split("/")
    ]
    reading: Reading | None = None
    field = "pin_table"
    page = spec.pin_table.page
    if tokens[:1] == ["package"]:
        package_field = tokens[1] if len(tokens) > 1 else ""
        dimension = getattr(spec.package, package_field, None)
        if isinstance(dimension, Dimension):
            reading = dimension.reading
            field = f"package.{package_field}"
        elif (
            package_field == "exposed_pad"
            and len(tokens) > 2
            and spec.package.exposed_pad is not None
        ):
            dimension = getattr(spec.package.exposed_pad, tokens[2], None)
            if isinstance(dimension, Dimension):
                reading = dimension.reading
                field = f"package.exposed_pad.{tokens[2]}"
        elif package_field in {
            "drawing_id",
            "drawing_revision",
            "drawing_view",
            "pin1_corner",
            "pin_count",
        }:
            reading = spec.package.pin1_reading
            field = "package.drawing_view"
    elif tokens[:1] == ["land_pattern"] and len(tokens) > 2:
        if tokens[1] == "dimensions":
            dimension = spec.land_pattern.dimensions.get(tokens[2]) if spec.land_pattern else None
            if dimension is not None:
                reading = dimension.reading
                field = f"land_pattern.dimensions.{tokens[2]}"
    elif tokens[:1] == ["pins"] and len(tokens) > 1:
        try:
            index = int(tokens[1])
        except ValueError:
            index = -1
        if 0 <= index < len(spec.pins):
            pin = spec.pins[index]
            reading = pin.reading
            field = f"pins.{pin.number}.reading"
    elif tokens[:1] == ["orderable"] and len(tokens) > 1:
        try:
            index = int(tokens[1])
        except ValueError:
            index = -1
        if 0 <= index < len(spec.orderable):
            variant = spec.orderable[index]
            reading = variant.reading
            field = f"orderable.{index}.row"
    elif tokens[:1] == ["pinout"] and spec.pinout is not None:
        reading = spec.pinout.view_reading
        field = "pinout"
    if reading is not None:
        if reading.alternative_evidence is not None or reading.page is None:
            return page, None, field
        return reading.page, reading.bbox, field
    return page, None, field


def blind_questions(
    spec: PartSpec,
    packet_id: str,
    authoring_comparison: authoring.AuthoringComparison | None = None,
    comparison_evidence: Sequence[visionread.VisionComparisonEvidence] = (),
) -> list[BlindQuestion]:
    if not PACKET_ID_RE.fullmatch(packet_id):
        raise ValueError("packet_id must be 16 lowercase hexadecimal characters")
    pin_reading = spec.package.pin1_reading
    pin_table_page = spec.pin_table.page
    pin_page = pin_reading.page or pin_table_page
    pin_bbox = pin_reading.bbox if pin_reading.alternative_evidence is None else None
    questions = [
        BlindQuestion(
            question_id="drawing_id",
            prompt="What drawing identifier is printed on this package drawing?",
            page=pin_page,
            bbox=pin_bbox,
            expected=spec.package.drawing_id,
        ),
        BlindQuestion(
            question_id="drawing_view",
            prompt="Is this package drawing a top or bottom view?",
            page=pin_page,
            bbox=pin_bbox,
            expected=spec.package.drawing_view,
        ),
        BlindQuestion(
            question_id="pin1_corner",
            prompt="Which corner is marked as pin 1 in the top view?",
            page=pin_page,
            bbox=pin_bbox,
            expected=spec.package.pin1_corner,
        ),
        BlindQuestion(
            question_id="pin_count",
            prompt="How many numbered pins are shown in the package pin table?",
            page=pin_table_page,
            bbox=None,
            expected=str(spec.package.pin_count),
        ),
        BlindQuestion(
            question_id="exposed_pad",
            prompt="Does the package drawing or pin table show an exposed pad?",
            page=(
                spec.package.exposed_pad.length.reading.page or pin_table_page
                if spec.package.exposed_pad is not None
                else pin_table_page
            ),
            bbox=(
                spec.package.exposed_pad.length.reading.bbox
                if spec.package.exposed_pad is not None
                and spec.package.exposed_pad.length.reading.alternative_evidence is None
                else None
            ),
            expected="yes" if spec.package.exposed_pad is not None else "no",
        ),
    ]
    if spec.pinout is not None and spec.pinout.view_reading.alternative_evidence is None:
        questions.append(
            BlindQuestion(
                question_id="pinout.view",
                prompt="Is the pinout drawing a top view or a bottom view?",
                page=spec.pinout.page,
                bbox=spec.pinout.bbox,
                expected=spec.pinout.view,
            )
        )
    exposed_number = (
        spec.package.exposed_pad.number if spec.package.exposed_pad is not None else None
    )
    pins = [
        pin
        for pin in spec.pins
        if pin.number != exposed_number
        and pin.reading.alternative_evidence is None
        and pin.reading.vision is not None
        and pin.number in _pin_number_tokens(pin.reading.vision)
    ]

    def number_key(value: str) -> tuple[int, int | str]:
        return (0, int(value)) if value.isdigit() else (1, value.casefold())

    sorted_pins = sorted(pins, key=lambda pin: number_key(pin.number))
    selected_numbers: list[str] = []
    if any(pin.number == "1" for pin in pins):
        selected_numbers.append("1")
    if sorted_pins:
        highest = sorted_pins[-1].number
        if highest not in selected_numbers:
            selected_numbers.append(highest)
    remaining = [pin.number for pin in pins if pin.number not in selected_numbers]
    remaining.sort(key=lambda number: hashlib.sha256(f"{packet_id}{number}".encode()).digest())
    selected_numbers.extend(remaining[:2])
    for number in selected_numbers:
        pin = next(pin for pin in pins if pin.number == number)
        if pin.reading.page is None:
            continue
        questions.append(
            BlindQuestion(
                question_id=f"pin.{number}",
                prompt=f"What signal name is printed for pin {number}?",
                page=pin.reading.page,
                bbox=pin.reading.bbox,
                expected=pin.name,
            )
        )
    if authoring_comparison is not None:
        for index, disagreement in enumerate(authoring_comparison.disagreements):
            page, bbox, evidence_field = _authoring_question_region(spec, disagreement.pointer)
            expected = disagreement.a if disagreement.a is not None else disagreement.b
            expected_text = (
                expected
                if isinstance(expected, str)
                else json.dumps(expected, ensure_ascii=False, sort_keys=True)
            )
            questions.append(
                BlindQuestion(
                    question_id=f"authoring.{index}",
                    prompt=(
                        f"Independent authors disagree about {disagreement.pointer}. "
                        "What value is supported by the datasheet?"
                    ),
                    page=page,
                    bbox=bbox,
                    expected=expected_text,
                    evidence_field=evidence_field,
                )
            )
    for item in comparison_evidence:
        comparison = item.normalized
        if comparison is None:
            continue
        differences = comparison.get("differences")
        if item.item.kind == "compare_model":
            matches = (
                comparison.get("pin1_marker_matches"),
                comparison.get("outline_matches"),
                comparison.get("lead_arrangement_matches"),
            )
            artifact_kind = "model"
        else:
            matches = (
                comparison.get("pin1_matches"),
                comparison.get("arrangement_matches"),
                comparison.get("numbering_direction_matches"),
            )
            artifact_kind = "footprint" if item.item.kind == "compare_footprint" else "symbol"
        if (
            all(value is True for value in matches)
            and isinstance(differences, list)
            and not differences
        ):
            continue
        difference_text = ""
        if isinstance(differences, list):
            difference_text = "; ".join(differences)
        if not difference_text:
            difference_text = "one or more visual match checks returned false"
        questions.append(
            BlindQuestion(
                question_id=f"vision.{item.item.kind}.{item.item.read_id}",
                prompt=(
                    "Does the library match the datasheet here? yes/no "
                    f"Vision differences: {difference_text}. "
                    f"AI impression: {item.impression or 'unavailable'}"
                ),
                page=item.item.page,
                bbox=item.item.bbox,
                expected="yes",
                evidence_field=f"comparison.{artifact_kind}.{item.item.read_id}",
            )
        )
    return questions


def _comparison_evidence(
    spec: PartSpec,
    *,
    spec_path: Path,
    symbol_lib: Path,
    footprint_path: Path,
    models: Sequence[VerifiedModel],
    findings: list[ReviewFinding],
) -> list[visionread.VisionComparisonEvidence]:
    spec_hash = _sha256(spec_path) if spec_path.is_file() else ""
    if not spec_hash:
        return []
    records: list[visionread.VisionComparisonEvidence] = []
    comparisons: tuple[
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
    for kind, artifact_kind, path in comparisons:
        artifact_hash = _sha256(path) if path.is_file() else ""
        if not artifact_hash:
            continue
        current, _stale = visionread.find_comparison_evidence(
            spec_path.resolve().parent,
            kind=kind,
            spec_sha256=spec_hash,
            artifact_sha256=artifact_hash,
            artifact_kind=artifact_kind,
        )
        records.extend(current)
    footprint_hash = _sha256(footprint_path) if footprint_path.is_file() else ""
    for model_hash in sorted({item.sha256 for item in models if item.sha256 is not None}):
        current, stale = visionread.find_comparison_evidence(
            spec_path.resolve().parent,
            kind="compare_model",
            spec_sha256=spec_hash,
            artifact_sha256=model_hash,
            artifact_kind="model3d",
            additional_bindings={
                "footprint_sha256": footprint_hash,
                "model_sha256": model_hash,
            },
        )
        if not current:
            findings.append(
                ReviewFinding(
                    code="model_vision_comparison_missing",
                    severity="error",
                    field=f"model.{model_hash}",
                    message=(
                        "no current hash-bound model comparison vision record was found"
                        + ("; an older or stale comparison exists" if stale else "")
                    ),
                )
            )
            continue
        for evidence in current:
            if not evidence.answers.control_passed:
                findings.append(
                    ReviewFinding(
                        code="model_vision_control_failed",
                        severity="error",
                        field=f"model.{model_hash}",
                        message="model comparison control was not detected",
                        page=evidence.item.page,
                    )
                )
            if evidence.normalized is None or not evidence.impression_valid:
                findings.append(
                    ReviewFinding(
                        code="model_vision_record_invalid",
                        severity="error",
                        field=f"model.{model_hash}",
                        message="model comparison answer or impression is missing or invalid",
                        page=evidence.item.page,
                    )
                )
            elif any(
                evidence.normalized.get(key) is False
                for key in (
                    "pin1_marker_matches",
                    "outline_matches",
                    "lead_arrangement_matches",
                )
            ) or bool(evidence.normalized.get("differences")):
                findings.append(
                    ReviewFinding(
                        code="model_vision_mismatch",
                        severity="warning",
                        field=f"model.{model_hash}",
                        message=(
                            "model comparison reported differences requiring human review: "
                            + "; ".join(cast(list[str], evidence.normalized["differences"]))
                        ),
                        page=evidence.item.page,
                    )
                )
            records.append(evidence)
    return records


def _comparison_image_path(
    evidence: visionread.VisionComparisonEvidence,
    item: visionread.VisionReadItem,
) -> Path:
    batch_dir = evidence.batch_path.resolve().parent
    path = (batch_dir / item.image_path).resolve()
    if not path.is_relative_to(batch_dir):
        raise ValueError("comparison image path escapes its batch directory")
    return path


def _vision_review_image(path: Path, kind: str) -> dict[str, str]:
    resolved = path.resolve(strict=True)
    return {
        "kind": kind,
        "path": str(resolved),
        "sha256": _sha256(resolved),
        "review_record_path": str(advisory.review_record_path(resolved).resolve()),
    }


def _comparison_review_images(
    evidence: Sequence[visionread.VisionComparisonEvidence],
) -> list[dict[str, str]]:
    images: dict[str, dict[str, str]] = {}
    for record in evidence:
        artifact_kind = record.item.kind.removeprefix("compare_")
        for item in record.batch.items:
            path = _comparison_image_path(record, item)
            image = _vision_review_image(path, artifact_kind)
            if image["sha256"] != item.image_sha256:
                continue
            images[image["path"]] = image
    return [images[path] for path in sorted(images)]


def _copy_comparison_crop(
    evidence: visionread.VisionComparisonEvidence,
    item: visionread.VisionReadItem,
    target_dir: Path,
) -> _CropRecord:
    source = _comparison_image_path(evidence, item)
    if not source.is_file() or _sha256(source) != item.image_sha256:
        raise ValueError("comparison image is missing or stale")
    artifact_kind = item.kind.removeprefix("compare_")
    target = target_dir / f"comparison-{artifact_kind}-{item.read_id}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return _CropRecord(
        field=f"comparison.{artifact_kind}.{item.read_id}",
        page=item.page,
        path=target.as_posix(),
        sha256=_sha256(target),
        source_png_sha256=item.image_sha256,
        bbox=item.bbox,
        crop_bbox=item.crop_bbox,
        scale=1.0,
    )


def _fresh_authoring_comparison(
    spec: PartSpec,
    spec_dir: Path,
) -> authoring.AuthoringComparison | None:
    if spec.authoring is None:
        return None
    reference = Path(spec.authoring)
    if reference.is_absolute():
        raise ValueError("authoring run path must be relative to the PartSpec directory")
    run_dir = (spec_dir / reference).resolve()
    if not run_dir.is_relative_to(spec_dir.resolve()):
        raise ValueError("authoring run path escapes the PartSpec directory")
    return authoring.compare_runs(run_dir)


def _vision_read_records(spec: PartSpec, spec_dir: Path) -> list[dict[str, Any]]:
    references = [
        (field, reading.vision_read)
        for field, reading, _ in _readings(spec)
        if reading.vision_read is not None
    ]
    references.extend(
        (field, reference)
        for field, reference in (
            ("pin_table", spec.pin_table.vision_read),
            ("orderable", spec.orderable_vision_read),
            (
                "pinout.labels_vision",
                spec.pinout.labels_vision_read if spec.pinout is not None else None,
            ),
        )
        if reference is not None
    )
    result: list[dict[str, Any]] = []
    for field, reference in sorted(set(references)):
        try:
            _, item, answers = visionread.load_vision_read(spec_dir, reference)
            normalized = answers.normalized.get(item.read_id)
            result.append(
                {
                    "field": field,
                    "read_id": item.read_id,
                    "answer": answers.answers.get(item.read_id, ""),
                    "normalized_answer": (
                        normalized
                        if isinstance(normalized, str)
                        else json.dumps(normalized, ensure_ascii=False, sort_keys=True)
                        if normalized is not None
                        else ""
                    ),
                    "impression": answers.impressions.get(item.read_id, ""),
                }
            )
        except (OSError, ValueError):
            result.append(
                {
                    "field": field,
                    "read_id": reference.rsplit("#", 1)[-1],
                    "answer": "",
                    "normalized_answer": "",
                    "impression": "",
                }
            )
    return result


def _message_texts(value: Any, key: str = "") -> list[str]:
    if isinstance(value, str):
        return [value] if key in {"content", "message", "text"} else []
    if isinstance(value, list):
        return [text for child in cast(list[Any], value) for text in _message_texts(child)]
    if isinstance(value, dict):
        record = cast(dict[str, Any], value)
        return [
            text
            for child_key, child in record.items()
            for text in _message_texts(child, str(child_key))
        ]
    return []


def _trusted_event_path(event_path: Path) -> bool:
    if event_path.name.startswith("event-") is False or event_path.suffix != ".json":
        return False
    override = os.environ.get(EVENTS_DIR_ENV)
    try:
        resolved = event_path.resolve(strict=True)
        if override:
            return resolved.is_relative_to(Path(override).expanduser().resolve())
        root = (Path.home() / DEFAULT_EVENTS_ROOT).resolve()
        relative = resolved.relative_to(root)
        return len(relative.parts) == 3 and relative.parts[1] == "events"
    except (OSError, ValueError):
        return False


def _parse_correction(line: str) -> ReviewCorrection:
    payload = line.removeprefix("correction:").strip()
    parts = payload.split(" | ", 4)
    if len(parts) != 5:
        raise ValueError("correction must have five pipe-delimited fields")
    pointer, old_raw, new_raw, reason, page_raw = parts
    if not pointer.startswith("/") or not reason.strip():
        raise ValueError("correction requires a JSON pointer and non-empty reason")
    old = json.loads(old_raw)
    new = json.loads(new_raw)
    page_match = re.fullmatch(r"page=(\d+)", page_raw.strip())
    if page_match is None or int(page_match.group(1)) < 1:
        raise ValueError("correction page must be page=<positive integer>")
    return ReviewCorrection(
        pointer=pointer,
        old=old,
        new=new,
        reason=reason.strip(),
        page=int(page_match.group(1)),
    )


def _validated_reject(decision: ReviewDecision) -> bool:
    if (
        not decision.valid
        or decision.decision != "reject"
        or decision.event_path is None
        or decision.event_sha256 is None
        or not _trusted_event_path(decision.event_path)
    ):
        return False
    try:
        raw = decision.event_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != decision.event_sha256:
            return False
        event: Any = json.loads(raw)
        if not isinstance(event, dict) or cast(dict[str, Any], event).get("source") != "user":
            return False
        parsed = _parse_decision(
            "\n".join(_message_texts(cast(dict[str, Any], event))),
            decision.packet_id,
            decision.event_path,
            decision.event_sha256,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    return (
        parsed.valid
        and parsed.decision == "reject"
        and parsed.corrections == decision.corrections
        and parsed.findings == decision.findings
    )


def _parse_decision(
    text: str, packet_id: str, event_path: Path, event_sha256: str
) -> ReviewDecision:
    reasons: list[str] = []
    lines = text.splitlines()
    first = lines[0].strip() if lines else ""
    match = REVIEW_LINE_RE.fullmatch(first)
    if match is None:
        reasons.append("review_keyword_missing_or_malformed")
    elif match.group(1) != packet_id:
        reasons.append("packet_id_mismatch")
    decision: Literal["approve", "reject"] | None = None
    reviewer: str | None = None
    answers: dict[str, str] = {}
    findings: dict[str, str] = {}
    corrections: list[ReviewCorrection] = []
    for line in lines[1:]:
        value = line.strip()
        if not value:
            continue
        if value.startswith("decision:"):
            parsed = value.removeprefix("decision:").strip()
            if parsed not in {"approve", "reject"} or decision is not None:
                reasons.append("decision_field_invalid_or_duplicated")
            else:
                decision = cast(Literal["approve", "reject"], parsed)
        elif value.startswith("reviewer:"):
            parsed = value.removeprefix("reviewer:").strip()
            if not parsed or reviewer is not None:
                reasons.append("reviewer_field_invalid_or_duplicated")
            else:
                reviewer = parsed
        elif value.startswith("answer:"):
            match_answer = re.fullmatch(r"answer:\s*([^\s=]+)\s*=\s*(.*?)\s*", value)
            if match_answer is None:
                reasons.append("answer_field_malformed")
            else:
                question_id, answer = match_answer.groups()
                if question_id in answers:
                    reasons.append(f"duplicate_answer:{question_id}")
                else:
                    answers[question_id] = answer
        elif value.startswith("finding:"):
            match_finding = re.fullmatch(r"finding:\s*([^\s|]+)\s*\|\s*(.*?)\s*", value)
            if match_finding is None:
                reasons.append("finding_field_malformed")
            else:
                question_id, finding = match_finding.groups()
                if not finding:
                    reasons.append("finding_field_empty")
                elif question_id in findings:
                    reasons.append(f"duplicate_finding:{question_id}")
                else:
                    findings[question_id] = finding
        elif value.startswith("correction:"):
            try:
                correction = _parse_correction(value)
                if any(item.pointer == correction.pointer for item in corrections):
                    reasons.append(f"duplicate_correction:{correction.pointer}")
                else:
                    corrections.append(correction)
            except (ValueError, json.JSONDecodeError) as exc:
                reasons.append(f"correction_malformed:{exc}")
        else:
            reasons.append("unknown_review_field")
    if decision is None:
        reasons.append("decision_field_missing")
    if reviewer is None:
        reasons.append("reviewer_field_missing")
    return ReviewDecision(
        packet_id=packet_id,
        decision=decision,
        reviewer=reviewer,
        answers=answers,
        findings=findings,
        corrections=corrections,
        event_path=event_path,
        event_sha256=event_sha256,
        event_mtime_ns=event_path.stat().st_mtime_ns,
        event_name=event_path.name,
        valid=not reasons,
        reasons=reasons,
        integrity_valid=True,
    )


def _invalid_decision(packet_id: str, name: str, reason: str) -> ReviewDecision:
    return ReviewDecision(
        packet_id=packet_id,
        answers={},
        corrections=[],
        event_name=name,
        valid=False,
        reasons=[reason],
        integrity_valid=False,
    )


def load_decisions(library_dir: Path, packet_id: str) -> list[ReviewDecision]:
    """Load current-packet pointers and revalidate the referenced user events."""
    if not PACKET_ID_RE.fullmatch(packet_id):
        raise ValueError("packet_id must be 16 lowercase hexadecimal characters")
    directory = library_dir / "reviews" / "decisions"
    if not directory.exists():
        return []
    try:
        pointer_paths = sorted(directory.glob(f"{packet_id}.*.json"))
    except OSError:
        return [_invalid_decision(packet_id, directory.name, "decision_directory_unreadable")]
    decisions: list[ReviewDecision] = []
    for pointer_path in pointer_paths:
        try:
            if pointer_path.is_symlink():
                raise ValueError("pointer file must not be a symlink")
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            if not isinstance(pointer, dict):
                raise ValueError("pointer must be a JSON object")
            pointer = cast(dict[str, Any], pointer)
            if pointer.get("artifact_kind") != "circuit_library_review_pointer":
                raise ValueError("pointer artifact_kind is invalid")
            pointed_packet = pointer.get("packet_id")
            if pointed_packet != packet_id:
                raise ValueError("pointer packet_id does not match current packet")
            event_path_value = pointer.get("event_path")
            event_sha256 = pointer.get("event_sha256")
            if not isinstance(event_path_value, str) or not Path(event_path_value).is_absolute():
                raise ValueError("event_path must be absolute")
            if not isinstance(event_sha256, str) or SHA256_RE.fullmatch(event_sha256) is None:
                raise ValueError("event_sha256 must be a SHA-256 digest")
            if pointer_path.name != f"{packet_id}.{event_sha256[:12]}.json":
                raise ValueError("pointer filename does not match the event hash")
            event_path = Path(event_path_value)
            if not _trusted_event_path(event_path):
                raise ValueError("event_path is outside the agent-canvas event store")
            raw = event_path.read_bytes()
            actual_sha256 = hashlib.sha256(raw).hexdigest()
            if actual_sha256 != event_sha256:
                raise ValueError("event_sha256 does not match the current event bytes")
            event: Any = json.loads(raw)
            if not isinstance(event, dict) or cast(dict[str, Any], event).get("source") != "user":
                raise ValueError("referenced event is not a user message")
            text = "\n".join(_message_texts(cast(dict[str, Any], event)))
            decision = _parse_decision(text, packet_id, event_path, event_sha256)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            decision = _invalid_decision(packet_id, pointer_path.name, f"decision_invalid:{exc}")
        decisions.append(decision)
    decisions.sort(key=lambda item: (item.event_mtime_ns, item.event_name))
    return decisions


def _normalise_answer(question_id: str, value: str) -> str:
    normalized = " ".join(value.casefold().split())
    if question_id == "pin1_corner":
        return re.sub(r"[\s_-]+", "_", normalized)
    return normalized


def _packet_vision_precheck(
    library_dir: Path,
    packet: dict[str, object],
    packet_id: str,
) -> list[str]:
    if (
        packet.get("artifact_kind") != "circuit_library_review_packet"
        or packet.get("packet_id") != packet_id
        or not isinstance(packet.get("blind_questions"), list)
    ):
        return ["packet_vision_precheck_missing"]
    images = packet.get("vision_review_images")
    if not isinstance(images, list):
        return ["packet_vision_precheck_missing"]
    project_root = library_dir.resolve().parent
    for item in cast(list[object], images):
        if not isinstance(item, dict):
            return ["packet_vision_precheck_missing"]
        item = cast(dict[str, object], item)
        path_value = item.get("path")
        digest = item.get("sha256")
        record_value = item.get("review_record_path")
        kind = item.get("kind")
        if (
            not isinstance(path_value, str)
            or not isinstance(digest, str)
            or not isinstance(record_value, str)
            or kind not in {"footprint", "symbol"}
        ):
            return ["packet_vision_precheck_missing"]
        checklist: Literal["footprint", "symbol"] = cast(Literal["footprint", "symbol"], kind)
        try:
            image_path = Path(path_value).resolve(strict=True)
            record_path = Path(record_value).resolve(strict=True)
            if (
                not image_path.is_relative_to(project_root)
                or not record_path.is_relative_to(project_root)
                or _sha256(image_path) != digest
            ):
                return ["packet_vision_precheck_missing"]
            record = advisory.AdvisoryResult.model_validate(
                json.loads(record_path.read_text(encoding="utf-8"))
            )
            detail = advisory.parse_visual_review(record)
        except (OSError, json.JSONDecodeError, ValueError):
            return ["packet_vision_precheck_missing"]
        if (
            record.stage != "review"
            or record.status != "ok"
            or detail is None
            or detail.checklist != checklist
            or detail.image_sha256 != digest
            or Path(detail.image_path).resolve() != image_path
        ):
            return ["packet_vision_precheck_missing"]
    return []


def review_status(
    library_dir: Path,
    spec: PartSpec,
    packet_id: str,
    *,
    spec_path: Path | None = None,
    authoring_comparison: authoring.AuthoringComparison | None = None,
    review_scope: Literal["full", "relaxed"] = "full",
) -> ReviewStatus:
    if review_scope == "relaxed":
        from . import libmetrics

        try:
            libmetrics.require_relaxation_supported(library_dir.resolve().parent)
        except libmetrics.LibraryMetricsError:
            return ReviewStatus(
                artifact_kind="circuit_library_review_status",
                packet_id=packet_id,
                state="invalid",
                reasons=["review_relaxation_not_supported_by_metrics"],
                decisions=[],
            )
    if authoring_comparison is None and spec_path is not None:
        authoring_comparison = _fresh_authoring_comparison(spec, spec_path.resolve().parent)
    questions = blind_questions(spec, packet_id, authoring_comparison)
    packet_path = library_dir / "reviews" / _safe_field(spec.mpn) / packet_id / "review.json"
    packet_document: dict[str, object] = {}
    persisted_questions: dict[str, dict[str, object]] = {}
    try:
        loaded_packet = json.loads(packet_path.read_text(encoding="utf-8"))
        if isinstance(loaded_packet, dict):
            candidate_packet = cast(dict[str, object], loaded_packet)
            raw_questions = candidate_packet.get("blind_questions", [])
            vision_questions: list[BlindQuestion] = []
            if isinstance(raw_questions, list):
                for raw_item in cast(list[object], raw_questions):
                    if not isinstance(raw_item, dict):
                        continue
                    item = cast(dict[str, object], raw_item)
                    question_id = item.get("question_id")
                    if isinstance(question_id, str):
                        persisted_questions[question_id] = item
                    if isinstance(question_id, str) and question_id.startswith("vision.compare_"):
                        vision_questions.append(BlindQuestion.model_validate(item))
                packet_document = candidate_packet
                questions.extend(vision_questions)
    except (OSError, json.JSONDecodeError, ValueError):
        packet_document = {}
    trial_state, trial_state_error = _load_bound_review_trial_state(library_dir, packet_id)
    planted_question_pattern = re.compile(r"^package\.[A-Za-z0-9_]+\.(?:min|nom|max)$")
    if (
        trial_state is None
        and trial_state_error is None
        and any(
            planted_question_pattern.fullmatch(question_id) for question_id in persisted_questions
        )
    ):
        trial_state_error = "review_trial_state_missing"
    if trial_state is not None:
        for question in trial_state.questions:
            persisted = persisted_questions.get(question.question_id)
            expected_persisted = question.model_dump(mode="json", exclude={"expected"})
            if persisted != expected_persisted:
                trial_state_error = "review_trial_packet_mismatch"
                break
            questions.append(question)
    decisions = load_decisions(library_dir, packet_id)
    expected = {question.question_id: question.expected for question in questions}
    valid_approvals: list[ReviewDecision] = []
    valid_rejections: list[ReviewDecision] = []
    grammar_reasons: list[str] = []
    status_reasons: list[str] = []
    integrity_reasons: list[str] = []
    trial_findings: list[ReviewFinding] = []
    trial_question_ids: set[str] = set()
    if trial_state is not None:
        trial_question_ids = {question.question_id for question in trial_state.questions}
    if trial_state_error is not None:
        integrity_reasons.append(trial_state_error)
    revision_blocker = revwatch.approval_blocker(library_dir, spec)
    if revision_blocker is not None:
        integrity_reasons.append(revision_blocker)
    for decision in decisions:
        if not decision.integrity_valid:
            integrity_reasons.extend(decision.reasons)
            continue
        if not decision.valid:
            grammar_reasons.extend(decision.reasons)
            continue
        if decision.decision == "reject":
            valid_rejections.append(decision)
        elif decision.decision == "approve":
            comparison_no = any(
                question_id.startswith("vision.compare_")
                and _normalise_answer(question_id, answer) == "no"
                for question_id, answer in decision.answers.items()
            )
            if comparison_no:
                valid_rejections.append(decision.model_copy(update={"decision": "reject"}))
                continue
            missed_plants = (
                [
                    plant
                    for plant in trial_state.plants
                    if not _decision_catches_plant(decision, plant)
                ]
                if trial_state is not None
                else []
            )
            for plant in missed_plants:
                trial_findings.append(
                    ReviewFinding(
                        code="review_catch_trial_missed",
                        severity="error",
                        field=plant.field,
                        message="approval did not flag the planted review item",
                    )
                )
            trial_corrections_only = trial_state is not None and all(
                any(
                    correction.pointer == plant.pointer
                    and _same_json(correction.old, plant.wrong_value)
                    and _same_json(correction.new, plant.right_value)
                    for plant in trial_state.plants
                )
                for correction in decision.corrections
            )
            if decision.corrections and not trial_corrections_only:
                status_reasons.append("approval_must_not_include_corrections")
                continue
            if set(decision.answers) != set(expected):
                if trial_state_error is None:
                    status_reasons.append("human_review_blind_mismatch")
                continue
            mismatched_answers = False
            for question_id in expected:
                answer = _normalise_answer(question_id, decision.answers[question_id])
                if question_id in trial_question_ids:
                    flagged = question_id in decision.findings or (
                        trial_state is not None
                        and any(
                            plant.question_id == question_id
                            and _decision_catches_plant(decision, plant)
                            for plant in trial_state.plants
                        )
                    )
                    if answer not in {"yes", "no"} or (answer != "no" and not flagged):
                        mismatched_answers = True
                        break
                elif answer != _normalise_answer(question_id, expected[question_id]):
                    mismatched_answers = True
                    break
            if mismatched_answers:
                status_reasons.append("human_review_blind_mismatch")
                continue
            if missed_plants:
                continue
            valid_approvals.append(decision)

    if trial_findings and not valid_approvals:
        status_reasons.append("review_catch_trial_missed")
    if valid_approvals:
        status_reasons.extend(_packet_vision_precheck(library_dir, packet_document, packet_id))

    def order(item: ReviewDecision) -> tuple[int, str]:
        return item.event_mtime_ns, item.event_name

    latest_rejection = max(valid_rejections, key=order, default=None)
    eligible_approvals = [
        item
        for item in valid_approvals
        if latest_rejection is None or order(item) > order(latest_rejection)
    ]
    blockers = integrity_reasons + status_reasons
    if eligible_approvals and not blockers:
        return ReviewStatus(
            artifact_kind="circuit_library_review_status",
            packet_id=packet_id,
            state="approved",
            reasons=list(dict.fromkeys(grammar_reasons)),
            decisions=decisions,
            findings=trial_findings,
        )
    if latest_rejection is not None and not blockers:
        return ReviewStatus(
            artifact_kind="circuit_library_review_status",
            packet_id=packet_id,
            state="rejected",
            reasons=list(dict.fromkeys([*grammar_reasons, "human_review_rejected"])),
            decisions=decisions,
            findings=trial_findings,
        )
    if blockers or grammar_reasons:
        status_reasons.extend(integrity_reasons)
        status_reasons.extend(grammar_reasons)
        if any(reason == "human_review_blind_mismatch" for reason in status_reasons):
            status_reasons = ["human_review_blind_mismatch"]
        return ReviewStatus(
            artifact_kind="circuit_library_review_status",
            packet_id=packet_id,
            state="invalid",
            reasons=list(dict.fromkeys(status_reasons)),
            decisions=decisions,
            findings=trial_findings,
        )
    return ReviewStatus(
        artifact_kind="circuit_library_review_status",
        packet_id=packet_id,
        state="pending",
        reasons=["human_review_missing"],
        decisions=decisions,
        findings=trial_findings,
    )


def _pointer_tokens(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise ValueError("JSON pointer must start with '/'")
    tokens = pointer[1:].split("/")
    if any(re.search(r"~(?![01])", token) for token in tokens):
        raise ValueError("JSON pointer contains an invalid escape")
    return [token.replace("~1", "/").replace("~0", "~") for token in tokens]


def _list_index(token: str, pointer: str) -> int:
    if re.fullmatch(r"(?:0|[1-9][0-9]*)", token, flags=re.ASCII) is None:
        raise IndexError(pointer)
    return int(token)


def _get_pointer(document: Any, pointer: str) -> Any:
    current: Any = document
    for token in _pointer_tokens(pointer):
        if isinstance(current, list):
            current = cast(list[Any], current)[_list_index(token, pointer)]
        elif isinstance(current, dict):
            current = cast(dict[str, Any], current)[token]
        else:
            raise KeyError(pointer)
    return current


def _set_pointer(document: Any, pointer: str, value: Any) -> None:
    tokens = _pointer_tokens(pointer)
    if not tokens:
        raise ValueError("cannot replace the PartSpec root")
    parent: Any = document
    for token in tokens[:-1]:
        if isinstance(parent, list):
            parent = cast(list[Any], parent)[_list_index(token, pointer)]
        else:
            parent = cast(dict[str, Any], parent)[token]
    final = tokens[-1]
    if isinstance(parent, list):
        cast(list[Any], parent)[_list_index(final, pointer)] = value
    elif isinstance(parent, dict):
        parent_record = cast(dict[str, Any], parent)
        if final not in parent_record:
            raise KeyError(pointer)
        parent_record[final] = value
    else:
        raise KeyError(pointer)


def _same_json(left: Any, right: Any) -> bool:
    try:
        return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
            right, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return False


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _review_trial_path(library_dir: Path, packet_id: str) -> Path:
    project_root = confidential.project_root_for(library_dir)
    return project_root / ".confidential" / "review-trials" / f"{packet_id}.json"


def _load_review_trial_state(
    library_dir: Path,
    packet_id: str,
) -> tuple[ReviewTrialState | None, str | None]:
    path = _review_trial_path(library_dir, packet_id)
    if not path.exists() and not path.is_symlink():
        return None, None
    if path.is_symlink() or not path.is_file():
        return None, "review_trial_state_invalid"
    try:
        state = ReviewTrialState.model_validate_json(path.read_bytes())
    except (OSError, ValueError):
        return None, "review_trial_state_invalid"
    if state.packet_id != packet_id or not state.plants:
        return None, "review_trial_state_invalid"
    return state, None


def _load_bound_review_trial_state(
    library_dir: Path,
    packet_id: str,
    *,
    packet_path: Path | None = None,
) -> tuple[ReviewTrialState | None, str | None]:
    state, error = _load_review_trial_state(library_dir, packet_id)
    if error is not None:
        return None, error
    packet_paths = (
        [packet_path]
        if packet_path is not None
        else list((library_dir / "reviews").glob(f"*/{packet_id}/review.json"))
    )
    if not packet_paths:
        return (None, None) if state is None else (None, "review_trial_state_hash_mismatch")
    if len(packet_paths) != 1:
        return None, "review_trial_state_hash_mismatch"
    if packet_paths[0].is_symlink() or not packet_paths[0].is_file():
        return None, "review_trial_state_hash_mismatch"
    try:
        packet_value = json.loads(packet_paths[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "review_trial_state_hash_mismatch"
    if not isinstance(packet_value, dict):
        return None, "review_trial_state_hash_mismatch"
    declared_hash = cast(dict[str, Any], packet_value).get("review_trial_state_sha256")
    if state is None:
        return (None, "review_trial_state_missing") if declared_hash is not None else (None, None)
    sidecar_path = _review_trial_path(library_dir, packet_id)
    if (
        not isinstance(declared_hash, str)
        or SHA256_RE.fullmatch(declared_hash) is None
        or not sidecar_path.is_file()
        or _sha256(sidecar_path) != declared_hash
    ):
        return None, "review_trial_state_hash_mismatch"
    return state, None


def _write_review_trial_state(library_dir: Path, state: ReviewTrialState) -> None:
    confidential.ensure_confidential_store(confidential.project_root_for(library_dir))
    path = _review_trial_path(library_dir, state.packet_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(state.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    if path.exists():
        existing, error = _load_review_trial_state(library_dir, state.packet_id)
        if error is not None or existing != state:
            raise ValueError("review trial state already exists with different contents")
        return
    _atomic_write(path, serialized)


def _seeded_review_trial(
    spec: PartSpec,
    packet_id: str,
    dimensions: list[dict[str, Any]],
    crops: dict[str, _CropRecord],
    *,
    seed: int | None = None,
) -> ReviewTrialState | None:
    trial_seed = secrets.randbits(64) if seed is None else seed
    rng = random.Random(trial_seed)
    operators = [
        item
        for item in mutation.MUTATION_OPERATORS
        if item.name == "partspec_min_nom_max_column_shift"
    ]
    if not operators:
        return None
    operator = rng.choice(operators)
    artifacts = mutation.MutationArtifacts(
        spec=spec,
        symbol=cast(Any, None),
        footprint=cast(Any, None),
        model=cast(Any, None),
        model_path=Path(),
    )
    try:
        mutated, record = operator.apply(artifacts, trial_seed)
    except mutation.MutationError:
        return None
    field_name = record.params.get("field")
    if not isinstance(field_name, str):
        return None
    field = f"package.{field_name}"
    crop = crops.get(field)
    dimension = getattr(spec.package, field_name, None)
    mutated_dimension = getattr(mutated.spec.package, field_name, None)
    if (
        crop is None
        or dimension is None
        or mutated_dimension is None
        or dimension.reading.page is None
    ):
        return None
    changed_columns: list[Literal["min", "nom", "max"]] = [
        column
        for column in ("min", "nom", "max")
        if getattr(dimension, column) != getattr(mutated_dimension, column)
        and getattr(mutated_dimension, column) is not None
        and getattr(dimension, column) is not None
    ]
    if not changed_columns:
        return None
    column = rng.choice(changed_columns)
    right_value = float(getattr(dimension, column))
    wrong_value = float(getattr(mutated_dimension, column))
    question_id = f"{field}.{column}"
    question = BlindQuestion(
        question_id=question_id,
        prompt=(
            f"Does the cited datasheet support the displayed {column} value "
            f"{wrong_value:g} mm for {field}?"
        ),
        page=dimension.reading.page,
        bbox=dimension.reading.bbox,
        expected="no",
        evidence_field=field,
    )
    row = next((item for item in dimensions if item.get("field") == field), None)
    if row is None:
        return None
    row[column] = wrong_value
    plant = ReviewTrialPlant(
        question_id=question_id,
        operator=operator.name,
        pointer=f"/{field.replace('.', '/')}/{column}",
        field=field,
        column=column,
        wrong_value=wrong_value,
        right_value=right_value,
        evidence_crop_sha256=crop.sha256,
    )
    return ReviewTrialState(
        packet_id=packet_id,
        seed=trial_seed,
        questions=[question],
        plants=[plant],
    )


def _apply_review_trial_state(
    spec: PartSpec,
    state: ReviewTrialState,
    dimensions: list[dict[str, Any]],
    crops: dict[str, _CropRecord],
) -> None:
    questions = {question.question_id: question for question in state.questions}
    for plant in state.plants:
        field_name = plant.field.removeprefix("package.")
        dimension = getattr(spec.package, field_name, None)
        row = next((item for item in dimensions if item.get("field") == plant.field), None)
        crop = crops.get(plant.field)
        question = questions.get(plant.question_id)
        if (
            not field_name
            or "." in field_name
            or dimension is None
            or row is None
            or crop is None
            or crop.sha256 != plant.evidence_crop_sha256
            or question is None
            or question.evidence_field != plant.field
            or plant.question_id != f"{plant.field}.{plant.column}"
            or getattr(dimension, plant.column) != plant.right_value
            or plant.wrong_value == plant.right_value
        ):
            raise ValueError("stored review trial does not match current packet inputs")
        row[plant.column] = plant.wrong_value


def _review_trial_for_packet(
    spec: PartSpec,
    packet_id: str,
    dimensions: list[dict[str, Any]],
    crops: dict[str, _CropRecord],
    library_dir: Path,
    packet_path: Path,
) -> tuple[ReviewTrialState | None, str | None]:
    if packet_path.exists() or packet_path.is_symlink():
        state, error = _load_bound_review_trial_state(
            library_dir,
            packet_id,
            packet_path=packet_path,
        )
    else:
        state, error = _load_review_trial_state(library_dir, packet_id)
    if error is not None:
        return None, error
    if state is not None:
        try:
            _apply_review_trial_state(spec, state, dimensions, crops)
        except ValueError:
            return None, "review_trial_state_invalid"
    else:
        state = _seeded_review_trial(spec, packet_id, dimensions, crops)
    if state is not None:
        _write_review_trial_state(library_dir, state)
    return state, None


def _decision_catches_plant(decision: ReviewDecision, plant: ReviewTrialPlant) -> bool:
    answer = decision.answers.get(plant.question_id, "").casefold().strip()
    if answer == "no" or bool(decision.findings.get(plant.question_id, "").strip()):
        return True
    return any(
        correction.pointer == plant.pointer
        and _same_json(correction.old, plant.wrong_value)
        and _same_json(correction.new, plant.right_value)
        for correction in decision.corrections
    )


def review_trial_outcomes(library_dir: Path) -> list[tuple[str, bool]]:
    """Return one latest caught/missed result per reviewer and private trial."""
    trial_dir = _review_trial_path(library_dir, "placeholder").parent
    if not trial_dir.is_dir():
        return []
    outcomes: list[tuple[str, bool]] = []
    for path in sorted(trial_dir.glob("*.json")):
        packet_id = path.stem
        if PACKET_ID_RE.fullmatch(packet_id) is None:
            continue
        state, error = _load_bound_review_trial_state(library_dir, packet_id)
        if error is not None or state is None:
            continue
        latest: dict[str, ReviewDecision] = {}
        for decision in load_decisions(library_dir, packet_id):
            if (
                not decision.integrity_valid
                or decision.reviewer is None
                or decision.decision is None
            ):
                continue
            current = latest.get(decision.reviewer)
            if current is None or (decision.event_mtime_ns, decision.event_name) > (
                current.event_mtime_ns,
                current.event_name,
            ):
                latest[decision.reviewer] = decision
        for reviewer, decision in sorted(latest.items()):
            caught = all(_decision_catches_plant(decision, plant) for plant in state.plants)
            outcomes.append((reviewer, caught))
    return outcomes


def _correction_field(spec: PartSpec, pointer: str) -> str | None:
    try:
        tokens = _pointer_tokens(pointer)
    except ValueError:
        return None
    if len(tokens) >= 2 and tokens[0] == "package":
        if tokens[1] == "exposed_pad" and len(tokens) >= 3:
            return f"package.exposed_pad.{tokens[2]}"
        if tokens[1] in {"drawing_id", "drawing_revision", "drawing_view", "pin1_corner"}:
            return "package.drawing_view"
        return f"package.{tokens[1]}"
    if len(tokens) >= 2 and tokens[0] == "pins":
        try:
            index = _list_index(tokens[1], pointer)
            return f"pins.{spec.pins[index].number}.reading"
        except (IndexError, ValueError):
            return None
    if len(tokens) >= 2 and tokens[0] == "orderable":
        try:
            index = _list_index(tokens[1], pointer)
        except (IndexError, ValueError):
            return None
        return f"orderable.{index}.row"
    if tokens[:1] == ["pinout"]:
        return "pinout"
    return None


def _correction_evidence_crop_sha256(
    library_dir: Path,
    packet_id: str,
    spec: PartSpec,
    correction: ReviewCorrection,
) -> str | None:
    packets = list((library_dir / "reviews").glob(f"*/{packet_id}/review.json"))
    if len(packets) != 1:
        return None
    packet_path = packets[0]
    try:
        raw_packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw_packet, dict):
        return None
    crops = cast(dict[str, Any], raw_packet).get("crops")
    if not isinstance(crops, list):
        return None
    field = _correction_field(spec, correction.pointer)
    records: list[dict[str, Any]] = [
        cast(dict[str, Any], item) for item in cast(list[object], crops) if isinstance(item, dict)
    ]
    exact = [
        item
        for item in records
        if field is not None and item.get("field") == field and item.get("page") == correction.page
    ]
    candidates = exact or [item for item in records if item.get("page") == correction.page]
    for item in candidates:
        digest = item.get("sha256")
        relative = item.get("path")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            continue
        if not isinstance(relative, str) or not relative:
            continue
        try:
            unresolved_crop_path = packet_path.parent / relative
            if unresolved_crop_path.is_symlink():
                continue
            crop_path = unresolved_crop_path.resolve(strict=True)
            if (
                not crop_path.is_relative_to(packet_path.parent.resolve())
                or _sha256(crop_path) != digest
            ):
                continue
        except OSError:
            continue
        return digest
    return None


def _regression_case_id(spec_sha256: str, pointer: str, event_sha256: str) -> str:
    return hashlib.sha256(f"{spec_sha256}:{pointer}:{event_sha256}".encode()).hexdigest()[:24]


def _write_regression_case(library_dir: Path, case: ReviewRegressionCase) -> None:
    path = library_dir / "regressions" / f"{case.case_id}.json"
    serialized = json.dumps(case.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    if path.exists():
        existing = ReviewRegressionCase.model_validate_json(path.read_bytes())
        if existing != case:
            raise ValueError("regression case ID collides with different contents")
        return
    _atomic_write(path, serialized)


def export_correction_regression_fixtures(library_dir: Path) -> list[Path]:
    """Export correction regressions as deterministic mutation fixture records."""
    output_dir = library_dir / "mutation-fixtures"
    output: list[Path] = []
    regression_dir = library_dir / "regressions"
    if not regression_dir.is_dir():
        return output
    for source in sorted(regression_dir.glob("*.json")):
        case = ReviewRegressionCase.model_validate_json(source.read_bytes())
        path = output_dir / f"{case.case_id}.json"
        document = {
            "artifact_kind": "circuit_mutation_fixture",
            "fixture_id": case.case_id,
            "source_spec_sha256": case.spec_sha256,
            "mpn": case.mpn,
            "pdf_sha256": case.pdf_sha256,
            "pointer": case.pointer,
            "wrong_value": case.wrong_value,
            "right_value": case.right_value,
            "evidence_crop_sha256": case.evidence_crop_sha256,
        }
        _atomic_write(path, json.dumps(document, sort_keys=True, indent=2) + "\n")
        output.append(path)
    return output


def apply_corrections(spec_path: Path, decision: ReviewDecision) -> CorrectionResult:
    if not _validated_reject(decision):
        return CorrectionResult(
            artifact_kind="circuit_library_review_correction",
            applied=False,
            packet_id=decision.packet_id,
            applied_pointers=[],
            reasons=["correction_requires_valid_reject"],
        )
    library_dir = spec_path.resolve().parent / "library"
    trial_state, trial_state_error = _load_bound_review_trial_state(library_dir, decision.packet_id)
    if trial_state_error is not None:
        return CorrectionResult(
            artifact_kind="circuit_library_review_correction",
            applied=False,
            packet_id=decision.packet_id,
            applied_pointers=[],
            reasons=[trial_state_error],
        )
    corrections = [
        correction
        for correction in decision.corrections
        if trial_state is None
        or not any(
            correction.pointer == plant.pointer
            and _same_json(correction.old, plant.wrong_value)
            and _same_json(correction.new, plant.right_value)
            for plant in trial_state.plants
        )
    ]
    if not corrections:
        return CorrectionResult(
            artifact_kind="circuit_library_review_correction",
            applied=False,
            packet_id=decision.packet_id,
            applied_pointers=[],
            reasons=["no_part_spec_corrections"],
        )
    try:
        document = json.loads(spec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CorrectionResult(
            artifact_kind="circuit_library_review_correction",
            applied=False,
            packet_id=decision.packet_id,
            applied_pointers=[],
            reasons=[f"spec_unreadable:{exc}"],
        )
    corpus_path = library_dir / "reviews" / "corrections.jsonl"
    records: list[dict[str, Any]] = []
    if corpus_path.exists():
        try:
            parsed_records: list[Any] = [
                json.loads(line)
                for line in corpus_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError):
            return CorrectionResult(
                artifact_kind="circuit_library_review_correction",
                applied=False,
                packet_id=decision.packet_id,
                applied_pointers=[],
                reasons=["correction_corpus_unreadable"],
            )

        def valid_record(value: Any) -> bool:
            if not isinstance(value, dict):
                return False
            record = cast(dict[str, Any], value)
            return isinstance(record.get("event_sha256"), str) and isinstance(
                record.get("pointer"), str
            )

        if any(not valid_record(record) for record in parsed_records):
            return CorrectionResult(
                artifact_kind="circuit_library_review_correction",
                applied=False,
                packet_id=decision.packet_id,
                applied_pointers=[],
                reasons=["correction_corpus_unreadable"],
            )
        records = [cast(dict[str, Any], record) for record in parsed_records]
    existing = {(str(record["event_sha256"]), str(record["pointer"])) for record in records}
    updated = copy.deepcopy(document)
    changed = False
    for correction in corrections:
        try:
            current = _get_pointer(updated, correction.pointer)
        except (KeyError, IndexError, ValueError, TypeError):
            current = object()
        idempotency_key = (decision.event_sha256, correction.pointer)
        if _same_json(current, correction.new) and idempotency_key in existing:
            continue
        if not _same_json(current, correction.old):
            return CorrectionResult(
                artifact_kind="circuit_library_review_correction",
                applied=False,
                packet_id=decision.packet_id,
                applied_pointers=[],
                reasons=[f"correction_conflict:{correction.pointer}"],
            )
        try:
            _set_pointer(updated, correction.pointer, correction.new)
            changed = True
        except (KeyError, IndexError, ValueError, TypeError):
            return CorrectionResult(
                artifact_kind="circuit_library_review_correction",
                applied=False,
                packet_id=decision.packet_id,
                applied_pointers=[],
                reasons=[f"correction_conflict:{correction.pointer}"],
            )
    try:
        PartSpec.model_validate(updated)
    except ValueError as exc:
        return CorrectionResult(
            artifact_kind="circuit_library_review_correction",
            applied=False,
            packet_id=decision.packet_id,
            applied_pointers=[],
            reasons=[f"correction_invalid_spec:{exc}"],
        )
    spec = PartSpec.model_validate(updated)
    event_sha256 = decision.event_sha256 or ""
    additions: list[dict[str, Any]] = []
    pending_corrections = [
        correction
        for correction in corrections
        if (event_sha256, correction.pointer) not in existing
    ]
    regression_cases: list[ReviewRegressionCase] = []
    if pending_corrections:
        try:
            original_spec = PartSpec.model_validate(document)
            original_sha256 = part_spec_sha256(spec_path)
        except (OSError, ValueError):
            return CorrectionResult(
                artifact_kind="circuit_library_review_correction",
                applied=False,
                packet_id=decision.packet_id,
                applied_pointers=[],
                reasons=["spec_unreadable_for_regression"],
            )
        for correction in pending_corrections:
            crop_sha256 = _correction_evidence_crop_sha256(
                library_dir,
                decision.packet_id,
                original_spec,
                correction,
            )
            if crop_sha256 is None:
                return CorrectionResult(
                    artifact_kind="circuit_library_review_correction",
                    applied=False,
                    packet_id=decision.packet_id,
                    applied_pointers=[],
                    reasons=[f"correction_evidence_crop_missing:{correction.pointer}"],
                )
            case_id = _regression_case_id(
                original_sha256,
                correction.pointer,
                event_sha256,
            )
            regression_cases.append(
                ReviewRegressionCase(
                    case_id=case_id,
                    mpn=original_spec.mpn,
                    pdf_sha256=original_spec.datasheet.sha256,
                    spec_sha256=original_sha256,
                    pointer=correction.pointer,
                    field=_correction_field(original_spec, correction.pointer)
                    or correction.pointer,
                    wrong_value=correction.old,
                    right_value=correction.new,
                    evidence_crop_sha256=crop_sha256,
                    packet_id=decision.packet_id,
                    event_sha256=event_sha256,
                )
            )
    for correction in corrections:
        key = (event_sha256, correction.pointer)
        if key in existing:
            continue
        additions.append(
            {
                "packet_id": decision.packet_id,
                "mpn": spec.mpn,
                "pdf_sha256": spec.datasheet.sha256,
                "pointer": correction.pointer,
                "old": correction.old,
                "new": correction.new,
                "reason": correction.reason,
                "page": correction.page,
                "event_sha256": event_sha256,
            }
        )
        existing.add(key)
    for case in regression_cases:
        _write_regression_case(library_dir, case)
    if changed:
        _atomic_write(
            spec_path,
            json.dumps(updated, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
    if additions:
        records.extend(additions)
        _atomic_write(
            corpus_path,
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for record in records
            ),
        )
    return CorrectionResult(
        artifact_kind="circuit_library_review_correction",
        applied=True,
        packet_id=decision.packet_id,
        applied_pointers=[item.pointer for item in corrections],
        reasons=[],
    )


def correction_regressions(library_dir: Path, spec: PartSpec) -> list[ReviewFinding]:
    corpus_path = library_dir / "reviews" / "corrections.jsonl"
    records: list[Any] = []
    try:
        if corpus_path.exists():
            records = [
                json.loads(line)
                for line in corpus_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
    except (OSError, json.JSONDecodeError) as exc:
        return [
            ReviewFinding(
                code="correction_regressed",
                severity="error",
                field="corrections",
                message=f"correction corpus is unreadable: {exc}",
            )
        ]
    findings: list[ReviewFinding] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        record = cast(dict[str, Any], record)
        if (
            str(record.get("mpn", "")).casefold() != spec.mpn.casefold()
            or record.get("pdf_sha256") != spec.datasheet.sha256
            or not isinstance(record.get("pointer"), str)
        ):
            continue
        try:
            current = _get_pointer(spec.model_dump(mode="json"), str(record["pointer"]))
        except (KeyError, IndexError, ValueError, TypeError):
            current = object()
        if not _same_json(current, record.get("new")):
            findings.append(
                ReviewFinding(
                    code="correction_regressed",
                    severity="error",
                    field=str(record["pointer"]),
                    message="current PartSpec value differs from the accepted correction",
                )
            )
    regression_dir = library_dir / "regressions"
    if regression_dir.is_dir():
        for path in sorted(regression_dir.glob("*.json")):
            try:
                case = ReviewRegressionCase.model_validate_json(path.read_bytes())
            except (OSError, ValueError):
                findings.append(
                    ReviewFinding(
                        code="correction_regressed",
                        severity="error",
                        field="regressions",
                        message=f"regression case is unreadable: {path.name}",
                    )
                )
                continue
            if (
                case.mpn.casefold() != spec.mpn.casefold()
                or case.pdf_sha256 != spec.datasheet.sha256
            ):
                continue
            try:
                current = _get_pointer(spec.model_dump(mode="json"), case.pointer)
            except (KeyError, IndexError, ValueError, TypeError):
                current = object()
            if not _same_json(current, case.right_value):
                findings.append(
                    ReviewFinding(
                        code="correction_regressed",
                        severity="error",
                        field=case.field,
                        message="current PartSpec value differs from the accepted regression case",
                    )
                )
    deduplicated: list[ReviewFinding] = []
    finding_keys: set[tuple[str, str]] = set()
    for finding in findings:
        key = (finding.code, finding.field)
        if key not in finding_keys:
            deduplicated.append(finding)
            finding_keys.add(key)
    return deduplicated


def _safe_field(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "field"


def _table_record(
    extraction: DatasheetExtraction, extraction_dir: Path, page_number: int, table_index: int
) -> dict[str, Any] | None:
    page = next((item for item in extraction.pages if item.page == page_number), None)
    if page is None or page.tables_path is None:
        return None
    try:
        payload: Any = json.loads((extraction_dir / page.tables_path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        payload_record = cast(dict[str, Any], payload)
        tables = payload_record.get("tables")
        if not isinstance(tables, list):
            return None
        table_records = cast(list[Any], tables)
        if not 0 <= table_index < len(table_records):
            return None
        record = table_records[table_index]
        return cast(dict[str, Any], record) if isinstance(record, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _numeric_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list):
        return None
    coordinates = cast(list[Any], value)
    if len(coordinates) == 4 and all(
        isinstance(coordinate, (int, float)) for coordinate in coordinates
    ):
        return cast(
            tuple[float, float, float, float],
            tuple(float(item) for item in coordinates),
        )
    return None


def _table_cell_bbox(
    table: dict[str, Any], row_index: int, column_index: int
) -> tuple[float, float, float, float] | None:
    cells = table.get("cells")
    if not isinstance(cells, list):
        return None
    cell_rows = cast(list[Any], cells)
    if not 0 <= row_index < len(cell_rows):
        return None
    row = cell_rows[row_index]
    if not isinstance(row, list):
        return None
    row_cells = cast(list[Any], row)
    if not 0 <= column_index < len(row_cells):
        return None
    return _numeric_bbox(row_cells[column_index])


def _union_bboxes(
    boxes: Iterable[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    items = list(boxes)
    if not items:
        return None
    return (
        min(item[0] for item in items),
        min(item[1] for item in items),
        max(item[2] for item in items),
        max(item[3] for item in items),
    )


def _crop_image(
    source_path: Path,
    target_path: Path,
    *,
    bbox: tuple[float, float, float, float],
    page: PageExtraction,
    field: str,
    margin_pt: float = 12.0,
) -> _CropRecord:
    with Image.open(source_path) as opened:
        image: Image.Image = opened.convert("RGB")
    pixels_per_point = page.dpi / 72.0
    expanded = (
        max(0.0, bbox[0] - margin_pt),
        max(0.0, bbox[1] - margin_pt),
        min(page.width_pt, bbox[2] + margin_pt),
        min(page.height_pt, bbox[3] + margin_pt),
    )
    left = max(0, math.floor(expanded[0] * pixels_per_point))
    top = max(0, math.floor(expanded[1] * pixels_per_point))
    right = min(image.width, math.ceil(expanded[2] * pixels_per_point))
    bottom = min(image.height, math.ceil(expanded[3] * pixels_per_point))
    if right <= left or bottom <= top:
        raise ValueError(f"empty evidence crop for {field}")
    crop: Image.Image = image.crop((left, top, right, bottom))
    upscale = max(1.0, 400.0 / min(crop.width, crop.height))
    if upscale > 1:
        size: tuple[int, int] = (
            math.ceil(crop.width * upscale),
            math.ceil(crop.height * upscale),
        )
        resize = cast(Any, crop).resize
        crop = cast(Image.Image, resize(size, cast(Any, Image.Resampling.LANCZOS)))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    crop.save(target_path, format="PNG")
    return _CropRecord(
        field=field,
        page=page.page,
        path=target_path.as_posix(),
        sha256=_sha256(target_path),
        source_png_sha256=page.png_sha256,
        bbox=bbox,
        crop_bbox=expanded,
        scale=upscale,
    )


def _cited_bboxes(
    spec: PartSpec,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
) -> list[tuple[str, int, tuple[float, float, float, float]]]:
    results: list[tuple[str, int, tuple[float, float, float, float]]] = []
    for field, reading, _ in _readings(spec):
        if reading.page is None or reading.alternative_evidence is not None:
            continue
        bbox = reading.bbox
        if bbox is None and reading.cells:
            cell_boxes: list[tuple[float, float, float, float]] = []
            for cell_ref in reading.cells.values():
                table = _table_record(extraction, extraction_dir, reading.page, cell_ref.table)
                if table is not None:
                    cell_bbox = _table_cell_bbox(table, cell_ref.row, cell_ref.col)
                    if cell_bbox is not None:
                        cell_boxes.append(cell_bbox)
            bbox = _union_bboxes(cell_boxes)
        if bbox is not None:
            results.append((field, reading.page, bbox))
    for index, variant in enumerate(spec.orderable):
        if variant.reading.page is None or variant.reading.alternative_evidence is not None:
            continue
        table = _table_record(
            extraction,
            extraction_dir,
            variant.reading.page,
            variant.row.table,
        )
        cells = table.get("cells") if table is not None else None
        row_boxes: list[tuple[float, float, float, float]] = []
        cell_rows = cast(list[Any], cells) if isinstance(cells, list) else []
        if 0 <= variant.row.row < len(cell_rows):
            row = cell_rows[variant.row.row]
            if isinstance(row, list):
                row_boxes = [
                    box for cell in cast(list[Any], row) if (box := _numeric_bbox(cell)) is not None
                ]
        row_bbox = _union_bboxes(row_boxes)
        if row_bbox is not None:
            results.append((f"orderable.{index}.row", variant.reading.page, row_bbox))
    table = _table_record(extraction, extraction_dir, spec.pin_table.page, spec.pin_table.table)
    table_bbox = _numeric_bbox(table.get("bbox")) if table is not None else None
    if table_bbox is not None:
        results.append(("pin_table", spec.pin_table.page, table_bbox))
    if spec.pinout is not None and spec.pinout.view_reading.alternative_evidence is None:
        results.append(("pinout", spec.pinout.page, spec.pinout.bbox))
    return results


def _drawing_id_bbox(
    spec: PartSpec,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page: int,
) -> tuple[float, float, float, float] | None:
    expected = re.sub(r"[\W_]+", "", spec.package.drawing_id.casefold())
    if not expected:
        return None
    try:
        words = datasheet.page_words(extraction, extraction_dir, page, lanes=("poppler",))
    except (OSError, ValueError, datasheet.DatasheetError):
        return None
    words.sort(key=lambda word: ((word.top + word.bottom) / 2, word.x0))
    page_text: list[str] = []
    character_boxes: list[tuple[float, float, float, float]] = []
    for word in words:
        normalized = re.sub(r"[\W_]+", "", word.text.casefold())
        if normalized:
            page_text.append(normalized)
            character_boxes.extend([(word.x0, word.top, word.x1, word.bottom)] * len(normalized))
    match_start = "".join(page_text).find(expected)
    if match_start < 0:
        return None
    return _union_bboxes(character_boxes[match_start : match_start + len(expected)])


def _drawing_view_crops(
    spec: PartSpec,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    packet_dir: Path,
    crops: dict[str, _CropRecord],
) -> dict[str, _CropRecord]:
    page_by_number = {page.page: page for page in extraction.pages}
    requests: list[tuple[str, int, list[tuple[float, float, float, float]]]] = []
    package_page = spec.package.pin1_reading.page
    if package_page is None or spec.package.pin1_reading.alternative_evidence is not None:
        return {}
    package_boxes = [
        crop.bbox
        for crop in crops.values()
        if crop.field.startswith("package.") and crop.page == package_page
    ]
    drawing_id_bbox = _drawing_id_bbox(spec, extraction, extraction_dir, package_page)
    if drawing_id_bbox is not None:
        package_boxes.append(drawing_id_bbox)
    requests.append(("package.drawing_view", package_page, package_boxes))

    land_pattern_pages = Counter(
        crop.page for crop in crops.values() if crop.field.startswith("land_pattern.dimensions.")
    )
    if land_pattern_pages:
        land_page = min(
            land_pattern_pages,
            key=lambda page: (-land_pattern_pages[page], page),
        )
        land_boxes = [
            crop.bbox
            for crop in crops.values()
            if crop.field.startswith("land_pattern.dimensions.") and crop.page == land_page
        ]
        requests.append(("land_pattern.drawing_view", land_page, land_boxes))

    records: dict[str, _CropRecord] = {}
    for field, page_number, boxes in requests:
        bbox = _union_bboxes(boxes)
        page = page_by_number.get(page_number)
        if bbox is None or page is None:
            continue
        target = packet_dir / "crops" / f"{field}.png"
        record = _crop_image(
            _page_path(extraction_dir, page),
            target,
            bbox=bbox,
            page=page,
            field=field,
            margin_pt=36.0,
        )
        record.path = _relative_path(packet_dir, target)
        records[field] = record
    return records


def _dimension_records(
    spec: PartSpec,
    crops: dict[str, _CropRecord],
    vision_by_field: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for field, reading, dimension in _readings(spec):
        if dimension is None:
            continue
        crop = crops.get(field)
        vision = vision_by_field.get(field, {})
        records.append(
            {
                "field": field,
                "label": dimension.label or "—",
                "kind": dimension.kind,
                "min": dimension.min,
                "nom": dimension.nom,
                "max": dimension.max,
                "page": reading.page,
                "crop_path": crop.path if crop is not None else None,
                "crop_sha256": crop.sha256 if crop is not None else None,
                "vision_answer": vision.get("normalized_answer", ""),
                "vision_impression": vision.get("impression", ""),
            }
        )
    return records


def _pinout_comparison_rows(
    spec: PartSpec,
    geometry: PinoutGeometry | None,
    symbol: SymbolDef | None,
    vision: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if geometry is None:
        return []
    spec_names = {pin.number: pin.name for pin in spec.pins}
    symbol_names: dict[str, list[str]] = {}
    if symbol is not None:
        for pin in symbol.pins:
            symbol_names.setdefault(pin.number, []).append(pin.name)
    rows: list[dict[str, Any]] = []
    for label in geometry.labels:
        vision_name = (
            spec.pinout.labels_vision.get(label.number) if spec.pinout is not None else None
        )
        partspec_name = spec_names.get(label.number)
        drawing_name = label.name
        symbol_pin_names = sorted(set(symbol_names.get(label.number, [])))
        symbol_name = ", ".join(symbol_pin_names) if symbol_pin_names else None
        mismatch_fields: list[str] = []
        if partspec_name is None or not symbol_pin_names:
            mismatch_fields.append("number")
        if (
            vision_name is None
            or drawing_name is None
            or not pinout_oracle.names_equal(vision_name, drawing_name)
        ):
            mismatch_fields.append("vision_name")
        if (
            partspec_name is None
            or drawing_name is None
            or not pinout_oracle.names_equal(partspec_name, drawing_name)
        ):
            mismatch_fields.append("part_spec_name")
        if (
            not symbol_pin_names
            or drawing_name is None
            or any(not pinout_oracle.names_equal(name, drawing_name) for name in symbol_pin_names)
        ):
            mismatch_fields.append("symbol_name")
        rows.append(
            {
                "number": label.number,
                "drawing_name": drawing_name,
                "vision_name": vision_name,
                "vision_answer": vision_name or "—",
                "vision_impression": (vision or {}).get("impression", ""),
                "part_spec_name": partspec_name,
                "symbol_name": symbol_name,
                "mismatch_fields": mismatch_fields,
                "mismatch": bool(mismatch_fields),
            }
        )
    return rows


def _cell_text(
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page: PageExtraction,
    bbox: tuple[float, float, float, float] | None,
) -> str:
    if bbox is None:
        return ""
    try:
        words = datasheet.page_words(extraction, extraction_dir, page.page, lanes=("poppler",))
    except (OSError, ValueError, datasheet.DatasheetError):
        return ""
    selected = [
        word
        for word in words
        if bbox[0] <= (word.x0 + word.x1) / 2 <= bbox[2]
        and bbox[1] <= (word.top + word.bottom) / 2 <= bbox[3]
    ]
    selected.sort(key=lambda word: ((word.top + word.bottom) / 2, word.x0))
    return " ".join(word.text for word in selected)


def _pin_table_rows(
    spec: PartSpec,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
) -> list[dict[str, Any]]:
    page = next((item for item in extraction.pages if item.page == spec.pin_table.page), None)
    if page is None:
        return []
    table = _table_record(extraction, extraction_dir, page.page, spec.pin_table.table)
    if table is None:
        return []
    raw_rows = table.get("rows")
    if not isinstance(raw_rows, list):
        return []
    rows = cast(list[Any], raw_rows)
    spec_pins = {pin.number: pin for pin in spec.pins}
    output: list[dict[str, Any]] = []
    for row_index in range(spec.pin_table.header_rows, len(rows)):
        row = rows[row_index]
        if not isinstance(row, list):
            continue
        cells = cast(list[Any], row)
        number = (
            str(cells[spec.pin_table.number_col])
            if spec.pin_table.number_col < len(cells)
            and cells[spec.pin_table.number_col] is not None
            else ""
        )
        name = (
            str(cells[spec.pin_table.name_col])
            if spec.pin_table.name_col < len(cells) and cells[spec.pin_table.name_col] is not None
            else ""
        )
        number_bbox = _table_cell_bbox(table, row_index, spec.pin_table.number_col)
        name_bbox = _table_cell_bbox(table, row_index, spec.pin_table.name_col)
        number_poppler = _cell_text(extraction, extraction_dir, page, number_bbox)
        name_poppler = _cell_text(extraction, extraction_dir, page, name_bbox)
        numbers = re.findall(r"(?<!\w)\d+(?!\w)", number)
        exposed_label = name or name_poppler
        exposed_row = (
            spec.package.exposed_pad is not None
            and not numbers
            and re.search(
                r"\b(exposed|thermal)\b|power.?pad",
                exposed_label,
                re.IGNORECASE,
            )
            is not None
        )
        expanded_numbers = (
            [spec.package.exposed_pad.number]
            if exposed_row and spec.package.exposed_pad is not None
            else numbers
            if numbers
            else [number.strip()]
        )
        for pin_number in expanded_numbers:
            pin = spec_pins.get(pin_number)
            output.append(
                {
                    "pin_number": pin_number or None,
                    "pdfplumber_number": number,
                    "poppler_number": number_poppler,
                    "pdfplumber_name": name,
                    "poppler_name": name_poppler,
                    "part_spec_name": pin.name if pin is not None else None,
                    "part_spec_type": pin.electrical_type if pin is not None else None,
                    "symbol_number": None,
                    "symbol_name": None,
                    "symbol_type": None,
                    "footprint_pad_present": None,
                    "exposed_pad_row": exposed_row,
                }
            )
    return output


def _normalized_pin_name(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _pin_number_tokens(value: Any) -> set[str]:
    text = str(value or "").strip()
    numbers = re.findall(r"(?<!\w)\d+(?!\w)", text)
    return set(numbers) if numbers else ({text.casefold()} if text else set())


def _augment_pin_rows(
    rows: list[dict[str, Any]],
    symbol: SymbolDef | None,
    footprint: FootprintDef | None,
) -> list[dict[str, Any]]:
    symbol_by_number = {pin.number: pin for pin in symbol.pins} if symbol is not None else {}
    pad_numbers: set[str] = (
        {pad.number for pad in footprint.pads} if footprint is not None else set()
    )
    for row in rows:
        number = row["pin_number"]
        symbol_pin = symbol_by_number.get(str(number)) if number is not None else None
        row["symbol_number"] = symbol_pin.number if symbol_pin is not None else None
        row["symbol_name"] = symbol_pin.name if symbol_pin is not None else None
        row["symbol_type"] = symbol_pin.electrical_type if symbol_pin is not None else None
        row["footprint_pad_present"] = str(number) in pad_numbers if number is not None else False
        mismatches: set[str] = set()
        if number is not None and not row.get("exposed_pad_row"):
            for field in ("pdfplumber_number", "poppler_number"):
                value = row.get(field)
                if str(number) not in _pin_number_tokens(value):
                    mismatches.add(field)
            if any(field in mismatches for field in ("pdfplumber_number", "poppler_number")):
                mismatches.add("pin_number")
        part_name = row.get("part_spec_name")
        if part_name:
            expected_name = _normalized_pin_name(str(part_name))
            for field in ("pdfplumber_name", "poppler_name", "symbol_name"):
                actual_name = row.get(field)
                if not actual_name or _normalized_pin_name(str(actual_name)) != expected_name:
                    mismatches.update((field, "part_spec_name"))
        part_type = row.get("part_spec_type")
        symbol_type = row.get("symbol_type")
        if part_type and (
            not symbol_type or str(part_type).casefold() != str(symbol_type).casefold()
        ):
            mismatches.update(("part_spec_type", "symbol_type"))
        if number is not None and row["symbol_number"] is None:
            mismatches.add("symbol_number")
        if number is not None and not row["footprint_pad_present"]:
            mismatches.add("footprint_pad_present")
        row["mismatch_fields"] = sorted(mismatches)
        row["mismatch"] = bool(mismatches)
    return rows


def _attach_pin_vision(
    rows: list[dict[str, Any]],
    vision_by_field: dict[str, dict[str, Any]],
) -> None:
    for row in rows:
        number = row.get("pin_number")
        vision = vision_by_field.get(f"pins.{number}.reading", {}) if number else {}
        row["vision_answer"] = vision.get("normalized_answer", "")
        row["vision_impression"] = vision.get("impression", "")


def _format_dim(dimension: Dimension) -> float:
    if dimension.nom is not None:
        return dimension.nom
    if dimension.min is not None and dimension.max is not None:
        return (dimension.min + dimension.max) / 2
    if dimension.min is not None:
        return dimension.min
    if dimension.max is not None:
        return dimension.max
    return 0.0


def _pad_corners(pad: PadDef) -> list[tuple[float, float]]:
    radians = math.radians(pad.rotation)
    cosine, sine = math.cos(radians), math.sin(radians)
    return [
        (
            pad.x + x * cosine - y * sine,
            pad.y + x * sine + y * cosine,
        )
        for x, y in (
            (-pad.width / 2, -pad.height / 2),
            (pad.width / 2, -pad.height / 2),
            (pad.width / 2, pad.height / 2),
            (-pad.width / 2, pad.height / 2),
        )
    ]


def _pdf_bbox(item: Any) -> _PdfBBox | None:
    if not isinstance(item, dict):
        return None
    data = cast(dict[str, Any], item)
    values = [data.get(key) for key in ("x0", "top", "x1", "bottom")]
    if not all(isinstance(value, (int, float)) for value in values):
        return None
    x0, top, x1, bottom = (float(cast(float, value)) for value in values)
    return min(x0, x1), min(top, bottom), max(x0, x1), max(top, bottom)


def _bbox_in_crop(
    bbox: _PdfBBox,
    crop_bbox: _PdfBBox,
) -> bool:
    center_x = (bbox[0] + bbox[2]) / 2
    center_y = (bbox[1] + bbox[3]) / 2
    return crop_bbox[0] <= center_x <= crop_bbox[2] and crop_bbox[1] <= center_y <= crop_bbox[3]


def _curve_is_closed(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    data = cast(dict[str, Any], item)
    path = data.get("path")
    if isinstance(path, list):
        commands = cast(list[tuple[Any, ...]], path)
        if any(command and command[0] == "h" for command in commands):
            return True
    points = data.get("pts")
    if not isinstance(points, list):
        return False
    typed_points = cast(list[_PdfPoint], points)
    if len(typed_points) < 3:
        return False
    return math.dist(typed_points[0], typed_points[-1]) <= 0.05


def _pdf_vector_shapes(
    page: Any,
    crop_bbox: _PdfBBox,
) -> list[_PdfBBox]:
    shapes: dict[_PdfBBox, _PdfBBox] = {}

    def add_shape(bbox: _PdfBBox | None) -> None:
        if bbox is None or not _bbox_in_crop(bbox, crop_bbox):
            return
        key: _PdfBBox = (
            round(bbox[0], 2),
            round(bbox[1], 2),
            round(bbox[2], 2),
            round(bbox[3], 2),
        )
        shapes[key] = bbox

    for item in getattr(page, "rects", []):
        add_shape(_pdf_bbox(item))
    for item in getattr(page, "curves", []):
        bbox = _pdf_bbox(item)
        if _curve_is_closed(item):
            add_shape(bbox)

    edges: dict[_PdfEdge, _PdfBBox] = {}
    for collection_name in ("lines", "curves"):
        for item in getattr(page, collection_name, []):
            bbox = _pdf_bbox(item)
            if bbox is None or not _bbox_in_crop(bbox, crop_bbox):
                continue
            if not isinstance(item, dict):
                continue
            points = cast(dict[str, Any], item).get("pts")
            if not isinstance(points, list):
                continue
            typed_points = cast(list[_PdfPoint], points)
            if len(typed_points) < 2:
                continue
            start, end = typed_points[0], typed_points[-1]
            start_node = (round(float(start[0]), 2), round(float(start[1]), 2))
            end_node = (round(float(end[0]), 2), round(float(end[1]), 2))
            if start_node == end_node:
                continue
            edge: _PdfEdge = (
                (start_node, end_node) if start_node <= end_node else (end_node, start_node)
            )
            edges.setdefault(edge, bbox)

    parents: dict[_PdfPoint, _PdfPoint] = {}

    def find(node: _PdfPoint) -> _PdfPoint:
        parents.setdefault(node, node)
        if parents[node] != node:
            parents[node] = find(parents[node])
        return parents[node]

    for start, end in edges:
        start_root, end_root = find(start), find(end)
        if start_root != end_root:
            parents[end_root] = start_root

    components: dict[_PdfPoint, list[tuple[_PdfEdge, _PdfBBox]]] = {}
    for edge, bbox in edges.items():
        components.setdefault(find(edge[0]), []).append((edge, bbox))

    for component in components.values():
        degrees: Counter[_PdfPoint] = Counter()
        vertices: set[_PdfPoint] = set()
        for edge, _ in component:
            vertices.update(edge)
            degrees.update(edge)
        if len(vertices) < 4 or any(degrees[vertex] != 2 for vertex in vertices):
            continue
        add_shape(
            (
                min(bbox[0] for _, bbox in component),
                min(bbox[1] for _, bbox in component),
                max(bbox[2] for _, bbox in component),
                max(bbox[3] for _, bbox in component),
            )
        )
    return list(shapes.values())


def _nearest_row_spacing(
    shapes: list[_PdfBBox],
) -> float | None:
    if len(shapes) < 2:
        return None
    short_sides = [min(bbox[2] - bbox[0], bbox[3] - bbox[1]) for bbox in shapes]
    alignment_tolerance = max(0.25, median(short_sides) * 0.15)
    rows: dict[Literal["x", "y"], list[list[tuple[float, float]]]] = {"x": [], "y": []}
    for bbox in shapes:
        width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
        center_x, center_y = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        row_axis: Literal["x", "y"] = "y" if width >= height else "x"
        cross_coordinate, along_coordinate = (
            (center_x, center_y) if row_axis == "y" else (center_y, center_x)
        )
        row = next(
            (
                candidate
                for candidate in rows[row_axis]
                if abs(median(point[0] for point in candidate) - cross_coordinate)
                <= alignment_tolerance
            ),
            None,
        )
        if row is None:
            rows[row_axis].append([(cross_coordinate, along_coordinate)])
        else:
            row.append((cross_coordinate, along_coordinate))

    row_spacings: list[float] = []
    for row_groups in rows.values():
        for row in row_groups:
            along_coordinates = [point[1] for point in row]
            if len(along_coordinates) < 2:
                continue
            nearest = [
                min(
                    abs(value - other)
                    for other_index, other in enumerate(along_coordinates)
                    if other_index != index
                )
                for index, value in enumerate(along_coordinates)
            ]
            row_spacings.append(median(nearest))
    return median(row_spacings) if row_spacings else None


def _view_x(x: float, spec: PartSpec) -> float:
    return -x if spec.package.drawing_view == "bottom" else x


def _derive_overlay_geometry(
    page: Any,
    crop: _CropRecord,
    spec: PartSpec,
    footprint: FootprintDef,
    reference: LandPatternResult,
    *,
    dpi: int,
) -> _OverlayGeometry:
    exposed_number = (
        spec.package.exposed_pad.number if spec.package.exposed_pad is not None else None
    )
    reference_pads = [pad for pad in reference.pads if pad.number != exposed_number]
    pad_lengths = [
        max(pad.width, pad.height) for pad in reference_pads if min(pad.width, pad.height) > 0
    ]
    pad_widths = [
        min(pad.width, pad.height) for pad in reference_pads if min(pad.width, pad.height) > 0
    ]
    if spec.package.pitch is None:
        return _OverlayGeometry()
    pitch_mm = _format_dim(spec.package.pitch)
    if not pad_lengths or not pad_widths or pitch_mm <= 0:
        return _OverlayGeometry()

    pad_length_mm = median(pad_lengths)
    pad_width_mm = median(pad_widths)
    target_ratio = pad_length_mm / pad_width_mm
    page_shapes = _pdf_vector_shapes(page, crop.crop_bbox)
    ratio_matches: list[tuple[float, float, float, float]] = []
    for bbox in page_shapes:
        width_pt, height_pt = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if width_pt <= 0 or height_pt <= 0:
            continue
        ratio = max(width_pt, height_pt) / min(width_pt, height_pt)
        if abs(ratio - target_ratio) / target_ratio <= 0.15:
            ratio_matches.append(bbox)
    if not ratio_matches:
        return _OverlayGeometry()

    candidate_clusters: dict[frozenset[_PdfBBox], list[_PdfBBox]] = {}
    for seed in ratio_matches:
        seed_long = max(seed[2] - seed[0], seed[3] - seed[1])
        seed_short = min(seed[2] - seed[0], seed[3] - seed[1])
        cluster = [
            bbox
            for bbox in ratio_matches
            if abs(max(bbox[2] - bbox[0], bbox[3] - bbox[1]) - seed_long) / seed_long <= 0.15
            and abs(min(bbox[2] - bbox[0], bbox[3] - bbox[1]) - seed_short) / seed_short <= 0.15
        ]
        candidate_clusters.setdefault(frozenset(cluster), cluster)

    cluster_metrics: list[tuple[list[_PdfBBox], float, float | None, float]] = []
    for cluster in candidate_clusters.values():
        median_long_pt = median(max(bbox[2] - bbox[0], bbox[3] - bbox[1]) for bbox in cluster)
        size_estimate = median_long_pt / pad_length_mm
        nearest_spacing = _nearest_row_spacing(cluster)
        pitch_estimate = nearest_spacing / pitch_mm if nearest_spacing is not None else None
        agreement = (
            abs(size_estimate - pitch_estimate) / ((size_estimate + pitch_estimate) / 2)
            if pitch_estimate is not None and size_estimate + pitch_estimate > 0
            else math.inf
        )
        cluster_metrics.append((cluster, size_estimate, pitch_estimate, agreement))

    valid_clusters = [
        item
        for item in cluster_metrics
        if len(item[0]) >= 4 and item[2] is not None and item[3] <= 0.03
    ]
    candidate_shapes, size_estimate, pitch_estimate, scale_disagreement = max(
        valid_clusters or cluster_metrics,
        key=lambda item: (len(item[0]), -item[3]),
    )
    centers = [((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2) for bbox in candidate_shapes]
    scale_agrees = (
        len(candidate_shapes) >= 4 and pitch_estimate is not None and scale_disagreement <= 0.03
    )
    scale_pt_per_mm = size_estimate if scale_agrees else None

    candidate_centroid = (
        (sum(x for x, _ in centers) / len(centers), sum(y for _, y in centers) / len(centers))
        if centers
        else None
    )
    anchor_kind: Literal["exposed_pad", "candidate_centroid"] | None = None
    anchor_page_pt: tuple[float, float] | None = None
    anchor_footprint_mm: tuple[float, float] | None = None
    if spec.package.exposed_pad is not None:
        exposed_pad = spec.package.exposed_pad
        expected_ratio = max(_format_dim(exposed_pad.length), _format_dim(exposed_pad.width)) / min(
            _format_dim(exposed_pad.length), _format_dim(exposed_pad.width)
        )
        expected_long_pt = (
            max(_format_dim(exposed_pad.length), _format_dim(exposed_pad.width)) * size_estimate
        )
        exposed_shapes = [
            bbox
            for bbox in page_shapes
            if abs(
                (max(bbox[2] - bbox[0], bbox[3] - bbox[1]))
                / (min(bbox[2] - bbox[0], bbox[3] - bbox[1]))
                - expected_ratio
            )
            / expected_ratio
            <= 0.15
            and abs(max(bbox[2] - bbox[0], bbox[3] - bbox[1]) - expected_long_pt) / expected_long_pt
            <= 0.20
        ]
        if exposed_shapes and candidate_centroid is not None:
            ep_bbox = min(
                exposed_shapes,
                key=lambda bbox: math.dist(
                    ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2),
                    candidate_centroid,
                ),
            )
            anchor_page_pt = ((ep_bbox[0] + ep_bbox[2]) / 2, (ep_bbox[1] + ep_bbox[3]) / 2)
            anchor_kind = "exposed_pad"
        footprint_ep = next(
            (pad for pad in footprint.pads if pad.number == exposed_pad.number), None
        )
        reference_ep = next(
            (pad for pad in reference.pads if pad.number == exposed_pad.number), None
        )
        if footprint_ep is not None:
            anchor_footprint_mm = (_view_x(footprint_ep.x, spec), footprint_ep.y)
        elif reference_ep is not None:
            anchor_footprint_mm = (_view_x(reference_ep.x, spec), reference_ep.y)
    else:
        if candidate_centroid is not None:
            anchor_page_pt = candidate_centroid
            anchor_kind = "candidate_centroid"
        footprint_pads = [pad for pad in footprint.pads if pad.type != "np_thru_hole"]
        if not footprint_pads:
            footprint_pads = reference_pads
        if footprint_pads:
            anchor_footprint_mm = (
                median([_view_x(pad.x, spec) for pad in footprint_pads]),
                median([pad.y for pad in footprint_pads]),
            )

    scale_px_per_mm = (
        scale_pt_per_mm * dpi / 72.0 * crop.scale
        if scale_pt_per_mm is not None and dpi > 0
        else None
    )
    anchor_crop_px = (
        (
            (anchor_page_pt[0] - crop.crop_bbox[0]) * dpi / 72.0 * crop.scale,
            (anchor_page_pt[1] - crop.crop_bbox[1]) * dpi / 72.0 * crop.scale,
        )
        if anchor_page_pt is not None and dpi > 0
        else None
    )
    return _OverlayGeometry(
        scale_pt_per_mm=scale_pt_per_mm,
        pad_size_estimate_pt_per_mm=size_estimate,
        pitch_estimate_pt_per_mm=pitch_estimate,
        scale_px_per_mm=scale_px_per_mm,
        anchor_kind=anchor_kind,
        anchor_page_pt=anchor_page_pt,
        anchor_crop_px=anchor_crop_px,
        anchor_footprint_mm=anchor_footprint_mm,
        candidate_pad_count=len(candidate_shapes),
    )


def _overlay_svg(
    spec: PartSpec,
    footprint: FootprintDef,
    crop_image_path: Path,
    crop_image_href: str,
    output_path: Path,
    *,
    geometry: _OverlayGeometry,
    include_background: bool = True,
) -> None:
    with Image.open(crop_image_path) as image:
        image_width, image_height = image.size
    esc = html.escape
    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{image_width}px" '
            f'height="{image_height}px" viewBox="0 0 {image_width} {image_height}">'
        )
    ]
    if include_background:
        parts.append(
            f'<image href="{esc(crop_image_href)}" x="0" y="0" width="{image_width}" '
            f'height="{image_height}" preserveAspectRatio="none"/>'
        )
    if geometry.scale_known:
        anchor_x, anchor_y = geometry.anchor_footprint_mm or (0.0, 0.0)
        crop_anchor_x, crop_anchor_y = geometry.anchor_crop_px or (0.0, 0.0)
        pixel_scale = geometry.scale_px_per_mm or 0.0
        parts.append('<g id="candidate-pads">')
        for pad in footprint.pads:
            if pad.type == "np_thru_hole":
                continue
            pad_corners = _pad_corners(pad)
            points = [
                (
                    crop_anchor_x + (_view_x(x, spec) - anchor_x) * pixel_scale,
                    crop_anchor_y + (y - anchor_y) * pixel_scale,
                )
                for x, y in pad_corners
            ]
            point_text = " ".join(f"{x:.3f},{y:.3f}" for x, y in points)
            pin_one = pad.number == "1"
            parts.append(
                f'<polygon data-pad-number="{esc(pad.number)}" points="{point_text}" '
                f'fill="#ff3333" fill-opacity="{0.72 if pin_one else 0.42}" '
                f'stroke="{("#ffbf00" if pin_one else "#d00000")}" '
                f'stroke-width="{4 if pin_one else 2}"/>'
            )
            center_x = crop_anchor_x + (_view_x(pad.x, spec) - anchor_x) * pixel_scale
            center_y = crop_anchor_y + (pad.y - anchor_y) * pixel_scale
            font_size = max(8.0, min(24.0, min(pad.width, pad.height) * pixel_scale * 0.55))
            parts.append(
                f'<text x="{center_x:.3f}" y="{center_y:.3f}" font-size="{font_size:.3f}" '
                f'fill="#540000" text-anchor="middle" dominant-baseline="central">'
                f"{esc(pad.number)}</text>"
            )
        parts.append("</g>")
    parts.append("</svg>")
    output_path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def _blink_html(source_href: str, cad_href: str) -> str:
    source = html.escape(source_href, quote=True)
    cad = html.escape(cad_href, quote=True)
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        "body{margin:0;background:#222;color:#fff;font:14px sans-serif}"
        ".frame{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}"
        ".cad{animation:blink-cad 2s steps(1,end) infinite}"
        "@keyframes blink-cad{0%,49.99%{opacity:0}50%,100%{opacity:1}}"
        ".caption{position:fixed;left:0;right:0;bottom:0;padding:8px;"
        "background:#000b;text-align:center;z-index:2}"
        "</style></head><body>"
        f'<img class="frame source" src="{source}" alt="Datasheet evidence crop">'
        f'<img class="frame cad" src="{cad}" alt="CAD geometry at matched scale and registration">'
        '<div class="caption">Alternating datasheet crop and registered CAD geometry</div>'
        "</body></html>\n"
    )


def _relative_path(base: Path, path: Path) -> str:
    return path.resolve().relative_to(base.resolve()).as_posix()


def _page_path(extraction_dir: Path, page: PageExtraction) -> Path:
    return extraction_dir / page.png_path


def _question_crop_field(
    question_id: str,
    spec: PartSpec,
    evidence_field: str | None = None,
) -> str:
    if evidence_field is not None:
        return evidence_field
    if question_id in {"drawing_id", "drawing_view", "pin1_corner"}:
        return "package.drawing_view"
    if question_id == "pin_count":
        return "pin_table"
    if question_id == "exposed_pad" and spec.package.exposed_pad is not None:
        return "package.exposed_pad.length"
    if question_id == "pinout.view":
        return "pinout"
    if question_id.startswith("pin."):
        return "pin_table"
    return "pin_table"


def _message_template(packet_id: str, questions: list[BlindQuestion]) -> str:
    lines = [
        f"CIRCUIT-LIBRARY-REVIEW {packet_id}",
        "decision: approve",
        "reviewer: <your name>",
    ]
    lines.extend(f"answer: {question.question_id} = " for question in questions)
    return "\n".join(lines)


def _blind_html(
    packet_id: str,
    questions: list[BlindQuestion],
    crops: dict[str, _CropRecord],
    spec: PartSpec,
    packet_dir: Path,
) -> str:
    sections: list[str] = []
    pages: dict[int, str] = {}
    for question in questions:
        crop_field = _question_crop_field(
            question.question_id,
            spec,
            question.evidence_field,
        )
        crop = crops.get(crop_field)
        evidence = ""
        if crop is not None:
            crop_path = html.escape(_relative_path(packet_dir, packet_dir / crop.path))
            evidence = f'<p><img alt="Source crop" src="{crop_path}"></p>'
        full_page = next(
            (
                page
                for page in pages.values()
                if page.startswith(f"evidence/page-{question.page:03d}")
            ),
            None,
        )
        if full_page is None:
            full_page = f"evidence/page-{question.page:03d}.png"
            pages[question.page] = full_page
        sections.append(
            f"<section><h2>{html.escape(question.prompt)}</h2>"
            f"<p>Question ID: <code>{html.escape(question.question_id)}</code></p>"
            f'{evidence}<p><a href="{html.escape(full_page)}">'
            f"Open full page {question.page}</a></p>"
            '<p>Answer: <span class="answer-line"></span></p></section>'
        )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        "<title>Blind library review</title>"
        "<style>body{font:16px sans-serif;max-width:1000px;margin:2rem auto}"
        "img{max-width:100%;height:auto}section{border-bottom:1px solid #bbb;padding:1rem 0}"
        ".answer-line{display:inline-block;min-width:20rem;border-bottom:1px solid #333}</style>"
        "</head><body>"
        + (
            '<p class="confidential-banner"><strong>CONFIDENTIAL — local only</strong></p>'
            if spec.datasheet.confidential
            else ""
        )
        + f"<h1>Blind review</h1><p>Packet {html.escape(packet_id)}</p>"
        + "<p>To flag a specific item, add "
        "<code>finding: &lt;question_id&gt; | &lt;reason&gt;</code> to the decision message.</p>"
        + "".join(sections)
        + "</body></html>\n"
    )


def _review_html(review: dict[str, Any]) -> str:
    def escape(value: Any) -> str:
        return html.escape(str(value))

    def record_items(value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        return [
            cast(dict[str, object], item)
            for item in cast(list[object], value)
            if isinstance(item, dict)
        ]

    def string_items(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in cast(list[object], value)]

    findings = review["findings"]
    findings_html = (
        "".join(
            f"<li><strong>{html.escape(item['severity'])}: "
            f"{html.escape(item['code'])}</strong> — "
            f"{html.escape(item['field'])}: {html.escape(item['message'])}</li>"
            for item in findings
        )
        or "<li>None.</li>"
    )
    vision_read_rows = "".join(
        "<tr>"
        f"<td>{escape(item.get('field', '—'))}</td>"
        f"<td>{escape(item.get('read_id', '—'))}</td>"
        f"<td>{escape(item.get('normalized_answer') or '—')}</td>"
        f"<td>{escape(item.get('impression') or '—')}</td>"
        "</tr>"
        for item in review.get("vision_reads", [])
    )
    authoring_comparison = review.get("authoring_comparison")
    author_impressions: dict[str, str] = {}
    if isinstance(authoring_comparison, dict):
        comparison_data = cast(dict[str, object], authoring_comparison)
        raw_impressions = comparison_data.get("impressions")
        if isinstance(raw_impressions, dict):
            impression_values = cast(dict[object, object], raw_impressions)
            author_impressions = {
                lane: impression
                for lane, impression in impression_values.items()
                if isinstance(lane, str) and isinstance(impression, str)
            }
    author_impressions_html = (
        "<h2>Author impressions</h2><table><thead><tr><th>Lane</th><th>Impression</th>"
        "</tr></thead><tbody>"
        + "".join(
            f"<tr><td>{escape(lane.upper())}</td><td>{escape(impression)}</td></tr>"
            for lane, impression in sorted(author_impressions.items())
        )
        + "</tbody></table>"
        if author_impressions
        else ""
    )
    evidence_pages = {
        int(item["page"]): str(item["path"]) for item in review.get("evidence_pages", [])
    }

    def full_page_link(page: Any) -> str:
        try:
            page_number = int(page)
        except (TypeError, ValueError):
            return "—"
        path = evidence_pages.get(page_number)
        return f'<a href="{escape(path)}">Page {page_number}</a>' if path is not None else "—"

    pin_cell_keys = (
        "pin_number",
        "pdfplumber_number",
        "poppler_number",
        "pdfplumber_name",
        "poppler_name",
        "part_spec_name",
        "part_spec_type",
        "symbol_number",
        "symbol_name",
        "symbol_type",
        "footprint_pad_present",
        "vision_answer",
        "vision_impression",
    )

    def pin_cell(row: dict[str, Any], key: str) -> str:
        value = row.get(key)
        if key == "footprint_pad_present":
            text = "—" if value is None else ("yes" if value else "no")
        else:
            text = "—" if value is None or value == "" else str(value)
        class_name = ' class="mismatch"' if key in row.get("mismatch_fields", []) else ""
        return f"<td{class_name}>{escape(text)}</td>"

    pin_rows = "".join(
        "<tr>"
        + "".join(pin_cell(row, key) for key in pin_cell_keys)
        + ('<td class="mismatch">Mismatch</td>' if row.get("mismatch") else "<td>—</td>")
        + "</tr>"
        for row in review["pin_comparisons"]
    )
    pin_sources = review.get("pin_sources")
    pin_source_panel = ""
    if isinstance(pin_sources, dict):
        source_data = cast(dict[str, object], pin_sources)
        class_a_value = source_data.get("class_a")
        class_b_value = source_data.get("class_b")
        class_a = cast(dict[str, object], class_a_value) if isinstance(class_a_value, dict) else {}
        class_b = cast(dict[str, object], class_b_value) if isinstance(class_b_value, dict) else {}
        class_b_input_value = source_data.get("class_b_input")
        class_b_input = (
            cast(dict[str, object], class_b_input_value)
            if isinstance(class_b_input_value, dict)
            else {}
        )
        source_values = record_items(source_data.get("sources"))
        if not source_values and class_b:
            source_values = [class_b]
        if not source_values and class_b_input:
            source_values = [class_b_input]
        source_input_values = record_items(source_data.get("source_inputs"))
        class_a_pins = record_items(class_a.get("pins"))
        class_a_by_number = {
            str(item["number"]): str(item["name"])
            for item in class_a_pins
            if "number" in item and "name" in item
        }
        source_by_number = [
            {
                str(item["number"]): (
                    f"{item['name']} (bank {item['bank']})"
                    if isinstance(item.get("bank"), str) and item["bank"]
                    else str(item["name"])
                )
                for item in record_items(source.get("pins"))
                if "number" in item and "name" in item
            }
            for source in source_values
        ]
        mismatch_numbers = {
            str(item["number"])
            for item in record_items(
                cast(dict[str, object], source_data.get("comparison")).get("findings")
                if isinstance(source_data.get("comparison"), dict)
                else None
            )
            if "number" in item
        }

        def source_description(data: dict[str, object]) -> str:
            parts = [
                str(data.get("description") or data.get("kind") or "pin source"),
                str(data.get("path") or "path unavailable"),
                f"SHA-256 {data.get('sha256') or 'unavailable'}",
            ]
            identity = data.get("identity")
            if isinstance(identity, list) and identity:
                identity_values = cast(list[object], identity)
                parts.append(f"identity {', '.join(str(item) for item in identity_values)}")
            lineage = data.get("lineage")
            if isinstance(lineage, str) and lineage:
                parts.append(f"lineage {lineage}")
            derived_from = data.get("derived_from")
            if isinstance(derived_from, list) and derived_from:
                derived_values = cast(list[object], derived_from)
                parts.append(f"derived from {', '.join(str(item) for item in derived_values)}")
            return " — ".join(parts)

        class_a_header = source_description(class_a)
        parsed_by_path = {
            str(source.get("path")): source
            for source in source_values
            if source.get("path") is not None
        }
        source_descriptions: list[str] = []
        for source_input in source_input_values:
            parsed_source = parsed_by_path.get(str(source_input.get("path")))
            source_descriptions.append(
                source_description({**source_input, **parsed_source})
                if parsed_source is not None
                else source_description(source_input)
            )
        source_headers = (
            [source_description(source) for source in source_values]
            if not source_input_values
            else source_descriptions
        )
        source_number_set = set(class_a_by_number)
        for source in source_by_number:
            source_number_set.update(source)
        source_numbers = sorted(
            source_number_set,
            key=lambda item: (0, int(item), item) if item.isdigit() else (1, item.casefold(), item),
        )

        def pin_source_cell(value: str, mismatch: bool) -> str:
            class_name = ' class="mismatch"' if mismatch else ""
            return f"<td{class_name}>{escape(value)}</td>"

        pin_source_rows = "".join(
            "<tr>"
            + pin_source_cell(number, number in mismatch_numbers)
            + pin_source_cell(class_a_by_number.get(number, "—"), number in mismatch_numbers)
            + "".join(
                pin_source_cell(source.get(number, "—"), number in mismatch_numbers)
                for source in source_by_number
            )
            + "</tr>"
            for number in source_numbers
        )
        single_banner = (
            '<p class="single-pin-source"><strong>Single pin source:</strong> '
            "fewer than two independent lineages are available.</p>"
            if source_data.get("single_source") is True
            else ""
        )
        source_error = source_data.get("error")
        source_error_html = (
            f"<p><strong>Pin source unavailable:</strong> {escape(source_error)}</p>"
            if source_error
            else ""
        )
        source_headers_html = "".join(
            f"<p><strong>{'Class B' if len(source_headers) == 1 else f'Source {index}'}:</strong> "
            f"{escape(header)}</p>"
            for index, header in enumerate(source_headers, start=1)
        )
        pin_table_headers = "".join(
            f"<th>{'Class B' if len(source_by_number) == 1 else f'Source {index}'} name</th>"
            for index in range(1, len(source_by_number) + 1)
        )
        colspan = 2 + len(source_by_number)
        pin_source_panel = (
            "<h2>Independent pin sources</h2>"
            + single_banner
            + source_error_html
            + f"<p><strong>Class A:</strong> {escape(class_a_header)}</p>"
            + source_headers_html
            + "<table><thead><tr><th>Pin</th><th>Class A name</th>"
            + pin_table_headers
            + "</tr></thead><tbody>"
            + (pin_source_rows or f'<tr><td colspan="{colspan}">No pin mappings.</td></tr>')
            + "</tbody></table>"
        )
    pinout_cell_keys = (
        "number",
        "drawing_name",
        "vision_name",
        "part_spec_name",
        "symbol_name",
        "vision_answer",
        "vision_impression",
    )

    def pinout_cell(row: dict[str, Any], key: str) -> str:
        value = row.get(key)
        text = "—" if value is None or value == "" else str(value)
        class_name = ' class="mismatch"' if key in row.get("mismatch_fields", []) else ""
        return f"<td{class_name}>{escape(text)}</td>"

    pinout_rows_html = "".join(
        "<tr>"
        + "".join(pinout_cell(row, key) for key in pinout_cell_keys)
        + ('<td class="mismatch">Mismatch</td>' if row.get("mismatch") else "<td>—</td>")
        + "</tr>"
        for row in review.get("pinout_comparisons", [])
    )
    raw_rule_chain = review.get("rule_chain")
    rule_chain_data = (
        cast(dict[str, object], raw_rule_chain) if isinstance(raw_rule_chain, dict) else {}
    )
    profile_chain = record_items(rule_chain_data.get("profiles"))
    rule_chain_rows = "".join(
        "<tr>"
        f"<td>{escape(item.get('profile_id', '—'))}</td>"
        f"<td>{escape(item.get('layer', '—'))}</td>"
        f"<td>{escape(item.get('sha256', '—'))}</td>"
        "</tr>"
        for item in profile_chain
    )
    tuning = review.get("footprint_tuning")
    tuning_html = "<p>No footprint lineage sidecar.</p>"
    if isinstance(tuning, dict):
        tuning_data = cast(dict[str, Any], tuning)
        base = tuning_data.get("base")
        base_html = ""
        if isinstance(base, dict):
            base_data = cast(dict[str, Any], base)
            base_link = base_data.get("link")
            base_path = escape(base_data.get("path", "—"))
            base_sha256 = escape(base_data.get("sha256", "—"))
            base_html = (
                f'<p>Base: <a href="{escape(base_link)}">{base_path}</a> '
                f"(SHA-256 {base_sha256})</p>"
                if isinstance(base_link, str)
                else f"<p>Base: {base_path} (SHA-256 {base_sha256})</p>"
            )
        change_rows = "".join(
            "<tr>"
            f"<td>{escape(item.get('pad', '—'))}</td>"
            f"<td>{escape(item.get('field', '—'))}</td>"
            f"<td>{escape(json.dumps(item.get('before'), ensure_ascii=False))}</td>"
            f"<td>{escape(json.dumps(item.get('after'), ensure_ascii=False))}</td>"
            "</tr>"
            for item in record_items(tuning_data.get("changes"))
        )
        evidence_rows = "".join(
            "<li>"
            + (
                f'<a href="{escape(item["link"])}">{escape(item.get("path", "Evidence"))}</a>'
                if isinstance(item.get("link"), str)
                else escape(item.get("path", "Evidence"))
            )
            + f" — SHA-256 {escape(item.get('sha256', '—'))}"
            + (f" — {escape(item['note'])}" if item.get("note") else "")
            + "</li>"
            for item in record_items(tuning_data.get("evidence"))
        )
        deviation_rows = "".join(
            "<tr>"
            f"<td>{escape(item.get('pad', '—'))}</td>"
            f"<td>{escape(json.dumps(item.get('reference_delta_mm'), sort_keys=True))}</td>"
            f"<td>{escape(json.dumps(item.get('base_delta_mm'), sort_keys=True))}</td>"
            "</tr>"
            for item in record_items(tuning_data.get("intentional_deviations"))
        )
        sidecar_link = tuning_data.get("lineage_link")
        sidecar_path = escape(tuning_data.get("lineage_path", "—"))
        sidecar_html = (
            f'<a href="{escape(sidecar_link)}">{sidecar_path}</a>'
            if isinstance(sidecar_link, str)
            else sidecar_path
        )
        tuning_html = (
            f"<p>Lineage: {sidecar_html} — SHA-256 "
            f"{escape(tuning_data.get('lineage_sha256', '—'))}</p>"
            f"<p>Layer: {escape(tuning_data.get('layer', '—'))}; "
            f"reason: {escape(tuning_data.get('reason', '—'))}</p>"
            + base_html
            + "<h3>Base-to-current pad changes</h3><table><thead><tr>"
            "<th>Pad</th><th>Field</th><th>Base value</th><th>Current value</th>"
            "</tr></thead><tbody>"
            + (change_rows or '<tr><td colspan="4">None.</td></tr>')
            + "</tbody></table><h3>Lineage evidence</h3><ul>"
            + (evidence_rows or "<li>None.</li>")
            + "</ul><h3>Intentional deviations from standard</h3>"
            "<table><thead><tr><th>Pad</th><th>Delta vs IPC/manufacturer reference (mm)</th>"
            "<th>Delta from base (mm)</th></tr></thead><tbody>"
            + (deviation_rows or '<tr><td colspan="3">None.</td></tr>')
            + "</tbody></table>"
        )

    def dimension_crop(row: dict[str, Any]) -> str:
        path = row.get("crop_path")
        if not path:
            return "—"
        escaped_path = escape(path)
        alt = escape(row.get("field", "Dimension evidence"))
        return (
            f'<a href="{escaped_path}"><img src="{escaped_path}" alt="{alt} evidence crop" '
            'style="max-height:120px;width:auto"></a>'
        )

    dimension_rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{escape(row.get(key) if row.get(key) is not None else '—')}</td>"
            for key in ("field", "label", "kind", "min", "nom", "max")
        )
        + f"<td>{full_page_link(row.get('page'))}</td>"
        + f"<td>{dimension_crop(row)}</td>"
        + f"<td>{escape(row.get('vision_answer') or '—')}</td>"
        + f"<td>{escape(row.get('vision_impression') or '—')}</td>"
        + f"<td>{escape(row.get('crop_sha256') or '—')}</td>"
        + "</tr>"
        for row in review["dimensions"]
    )
    render_items = (
        "".join(
            f'<figure><a href="{escape(item["path"])}">'
            f'<img src="{escape(item["path"])}" alt="{escape(item["kind"])} render" '
            f'style="width:{480 if item["kind"] == "footprint_svg" else 360}px;'
            'height:auto;max-width:100%"></a>'
            f"<figcaption>{escape(item['kind'])} · SHA-256 "
            f"{escape(item['sha256'])}</figcaption></figure>"
            for item in review["renders"]
        )
        or "<p>No render artifacts available.</p>"
    )
    hashes_html = "".join(
        f"<li><code>{html.escape(str(name))}</code>: <code>{html.escape(str(value))}</code></li>"
        for name, value in sorted(review["artifact_hashes"].items())
    )
    overlay = review.get("overlay")
    overlay_caption = (
        "Data-derived placement overlay"
        if overlay is not None and overlay.get("scale_known")
        else "Placement overlay — not to scale"
    )
    if overlay is not None:
        overlay_html = (
            f'<figure><img src="{escape(overlay["path"])}" '
            'alt="Land-pattern drawing with footprint placement overlay" '
            'style="max-width:100%;height:auto">'
            f"<figcaption>{overlay_caption}</figcaption></figure>"
        )
    else:
        overlay_html = "<p>Land-pattern overlay or cited page crop is unavailable.</p>"
    blink_value = review.get("overlay_blink")
    blink = cast(dict[str, object], blink_value) if isinstance(blink_value, dict) else None
    blink_path = blink.get("path") if blink is not None else None
    blink_html = (
        f'<p><a href="{escape(blink_path)}">Open matched-scale blink comparison</a></p>'
        if isinstance(blink_path, str)
        else "<p>Matched-scale blink comparison is unavailable.</p>"
    )
    vision_review_images_html = (
        "".join(
            f'<figure><a href="{escape(item.get("display_path", item["path"]))}">'
            f'<img src="{escape(item.get("display_path", item["path"]))}" '
            f'alt="{escape(item.get("kind", "Vision review image"))}" '
            'style="max-height:320px;width:auto"></a>'
            f"<figcaption>{escape(item.get('kind', 'image'))} · SHA-256 "
            f"{escape(item.get('sha256', ''))}<br>Review record: "
            f"<code>{escape(item.get('review_record_path', ''))}</code></figcaption></figure>"
            for item in review.get("vision_review_images", [])
        )
        or "<p>No overlay or comparison images are listed.</p>"
    )
    crop_records = review.get("crops", [])

    def crop_figure(item: dict[str, Any], caption: str) -> str:
        path = escape(item["path"])
        page = item.get("page")
        return (
            f'<figure><a href="{path}"><img src="{path}" alt="{escape(caption)}" '
            'style="max-height:400px;width:auto"></a>'
            f"<figcaption>{escape(caption)} · {full_page_link(page)}</figcaption></figure>"
        )

    pin1_crops = [
        item
        for item in crop_records
        if item.get("field") in {"package.pin1_reading", "package.drawing_view"}
    ]
    pin1_html = (
        "".join(crop_figure(item, str(item.get("field", "Pin-1 evidence"))) for item in pin1_crops)
        or "<p>No pin-1 evidence crop is available.</p>"
    )
    orderable_crops = [
        item
        for item in crop_records
        if str(item.get("field", "")).startswith("orderable.")
        and str(item.get("field", "")).endswith(".row")
    ]
    orderable_html = (
        "".join(
            crop_figure(item, str(item.get("field", "Orderable row"))) for item in orderable_crops
        )
        or "<p>No orderable-row evidence crop is available.</p>"
    )
    pinout_crop = next(
        (item for item in crop_records if item.get("field") == "pinout"),
        None,
    )
    pinout_crop_html = (
        crop_figure(pinout_crop, "Pinout drawing")
        if pinout_crop is not None
        else "<p>No pinout evidence crop is available.</p>"
    )
    pinout_finding_codes = {
        "footprint_chirality_mismatch",
        "footprint_rotation_mismatch",
        "footprint_order_mismatch",
    }
    pinout_findings = [
        item
        for item in findings
        if item["code"].startswith("pinout_") or item["code"] in pinout_finding_codes
    ]
    pinout_findings_html = (
        "".join(
            f"<li><strong>{escape(item['code'])}</strong>: {escape(item['message'])}</li>"
            for item in pinout_findings
        )
        or "<li>None.</li>"
    )
    message = html.escape(review["message_template"])
    unknowns = "".join(f"<li>{html.escape(item)}</li>" for item in review["unknowns"])
    request_value = review.get("agent_request")
    request_data = cast(dict[str, Any], request_value) if isinstance(request_value, dict) else {}
    alternatives_value = request_data.get("alternatives")
    alternatives_html = "".join(
        "<li><strong>"
        + escape(item.get("option", ""))
        + (" (recommended)" if index == request_data.get("recommended") else "")
        + "</strong><ul>"
        + "".join(f"<li>{escape(risk)}</li>" for risk in string_items(item.get("risks")))
        + "</ul></li>"
        for index, item in enumerate(record_items(alternatives_value))
    )
    agent_request_html = (
        "<h2>Agent assessment</h2><p>"
        + escape(request_data.get("agent_assessment", ""))
        + "</p><h2>Recommendation</h2><p><strong>"
        + escape(request_data.get("recommendation", ""))
        + "</strong></p><p>"
        + escape(request_data.get("recommendation_rationale", ""))
        + "</p><h2>Alternatives and risks</h2><ul>"
        + (alternatives_html or "<li>None.</li>")
        + "</ul>"
        if request_data
        else ""
    )
    confidential_banner = (
        '<p class="confidential-banner"><strong>CONFIDENTIAL — local only</strong></p>'
        if review.get("confidential") is True
        else ""
    )
    substitution_value = review.get("substitution")
    substitution_data = (
        cast(dict[str, Any], substitution_value) if isinstance(substitution_value, dict) else None
    )
    substitution_html = (
        "<section><h2>Substitution authorization</h2><p>Target: <strong>"
        + escape(substitution_data.get("target_mpn", "—"))
        + "</strong>; substitute: <strong>"
        + escape(substitution_data.get("substitute_mpn", "—"))
        + "</strong></p><p>Granted scope: "
        + escape(", ".join(string_items(substitution_data.get("granted_scope"))) or "None")
        + "</p><p>Unverifiable fields: "
        + escape(", ".join(string_items(substitution_data.get("unverifiable_fields"))) or "None")
        + "</p></section>"
        if substitution_data is not None
        else ""
    )
    alternative_fields = string_items(review.get("alternative_evidence_fields"))
    alternative_unknowns = string_items(review.get("alternative_evidence_unknown_fields"))
    alternative_review_html = (
        "<section><h2>Alternative evidence and unknowns</h2><p>Human-review-backed fields "
        "(not deterministic support):</p><ul>"
        + "".join(f"<li>{escape(item)}</li>" for item in alternative_fields)
        + ("" if alternative_fields else "<li>None.</li>")
        + "</ul><p>Unknown fields:</p><ul>"
        + "".join(f"<li>{escape(item)}</li>" for item in alternative_unknowns)
        + ("" if alternative_unknowns else "<li>None.</li>")
        + "</ul></section>"
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Library review</title>'
        "<style>body{font:15px sans-serif;max-width:1200px;margin:2rem auto}"
        "table{border-collapse:collapse;"
        "width:100%;font-size:13px}td,th{border:1px solid #bbb;padding:.3rem;text-align:left}"
        "img{max-width:100%;height:auto}"
        ".mismatch{background:#f8d7da;color:#842029}"
        ".single-pin-source{padding:.6rem;background:#fff3cd;color:#664d03}"
        ".confidential-banner{padding:.6rem;background:#f8d7da;color:#842029}</style>"
        "</head><body>"
        + confidential_banner
        + substitution_html
        + alternative_review_html
        + f"<h1>Review packet {html.escape(review['packet_id'])}</h1>"
        "<h2>Contradictions and deterministic findings</h2><ul>"
        + findings_html
        + "</ul>"
        + author_impressions_html
        + "<h2>Tool-managed vision reads</h2><table><thead><tr>"
        "<th>Field</th><th>Read ID</th><th>Vision answer</th><th>AI impression</th>"
        "</tr></thead><tbody>"
        + (vision_read_rows or '<tr><td colspan="4">None.</td></tr>')
        + "</tbody></table><h2>Pin comparisons</h2><table><thead><tr>"
        "<th>Pin</th><th>PDFPlumber number</th><th>Poppler number</th>"
        "<th>PDFPlumber name</th><th>Poppler name</th><th>PartSpec name</th>"
        "<th>PartSpec type</th>"
        "<th>Symbol number</th><th>Symbol name</th><th>Symbol type</th><th>Footprint pad</th>"
        "<th>Vision answer</th><th>Vision impression</th>"
        "<th>Mismatch</th>"
        "</tr></thead><tbody>"
        + pin_rows
        + "</tbody></table>"
        + pin_source_panel
        + "<h2>Pinout name-at-position</h2>"
        + pinout_crop_html
        + "<table><thead><tr><th>Number</th><th>Drawing name</th><th>Vision name</th>"
        "<th>PartSpec name</th><th>Symbol name</th><th>Vision answer</th>"
        "<th>Vision impression</th><th>Mismatch</th>"
        "</tr></thead><tbody>"
        + pinout_rows_html
        + "</tbody></table><h3>Relevant pinout findings</h3><ul>"
        + pinout_findings_html
        + "</ul><h2>Dimensions</h2><table><thead><tr>"
        "<th>Field</th><th>Label</th><th>Kind</th><th>Min</th><th>Nom</th><th>Max</th>"
        "<th>Page</th><th>Evidence crop</th><th>Vision answer</th>"
        "<th>Vision impression</th><th>Crop SHA-256</th></tr></thead><tbody>"
        + dimension_rows
        + "</tbody></table><h2>Pin-1 evidence</h2>"
        + pin1_html
        + "<h2>Orderable-row evidence</h2>"
        + orderable_html
        + "<h2>KiCad renders</h2>"
        + render_items
        + "<h2>Placement overlay</h2>"
        + overlay_html
        + "<h2>Matched-scale blink view</h2>"
        + blink_html
        + "<h2>Footprint tuning</h2>"
        + tuning_html
        + "<h2>Rule chain</h2><table><thead><tr><th>Profile</th><th>Layer</th>"
        "<th>SHA-256</th></tr></thead><tbody>"
        + (rule_chain_rows or '<tr><td colspan="3">None.</td></tr>')
        + "</tbody></table><p>Chain SHA-256: "
        + escape(rule_chain_data.get("chain_sha256", "—"))
        + "</p>"
        + "<h2>Images requiring vision review before approval</h2>"
        + vision_review_images_html
        + "<h2>Artifact hashes</h2><ul>"
        + hashes_html
        + "</ul><h2>Unknowns</h2><ul>"
        + unknowns
        + "</ul>"
        + agent_request_html
        + "<h2>Decision message template</h2><pre>"
        + message
        + "</pre></body></html>\n"
    )


def _finding_from_verify(item: VerifyFinding) -> ReviewFinding:
    return ReviewFinding(
        code=item.code,
        severity=item.severity,
        field=item.subject,
        message=item.message,
    )


def _finding_from_spec(item: SpecFinding) -> ReviewFinding:
    return ReviewFinding(
        code=item.code,
        severity=item.severity,
        field=item.field,
        message=item.message,
        page=item.page,
    )


def _pin_source_input_records(
    inputs: Sequence[pinsource.PinSourceInput],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in inputs:
        suffix_kind = item.path.suffix.lstrip(".").casefold()
        kind = item.kind or (
            "ibis"
            if suffix_kind in {"ibs", "ibis"}
            else "bsdl"
            if suffix_kind == "bsdl"
            else "microchip_atdf"
            if suffix_kind == "atdf"
            else "amd_package_file"
            if suffix_kind in {"csv", "txt", "pins", "pkg"}
            else suffix_kind
        )
        records.append(
            {
                "path": str(item.path.resolve()),
                "sha256": _optional_sha256(item.path),
                "kind": kind,
                "pinout_name": item.pinout_name,
                "derived_from": list(item.derived_from),
            }
        )
    return records


def _combined_pin_source_inputs(
    pin_source_path: Path | None,
    pin_sources: Sequence[pinsource.PinSourceInput] | None,
) -> list[pinsource.PinSourceInput]:
    result = [pinsource.PinSourceInput(path=pin_source_path)] if pin_source_path is not None else []
    for source in pin_sources or []:
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(result)
                if existing.path.resolve() == source.path.resolve()
            ),
            None,
        )
        if duplicate_index is None:
            result.append(source)
        else:
            result[duplicate_index] = source
    return result


def _pin_source_review_document(
    spec: PartSpec,
    *,
    spec_path: Path,
    pin_source_path: Path | None,
    pin_source_sha256: str | None,
    pin_sources: Sequence[pinsource.PinSourceInput] | None = None,
) -> dict[str, Any]:
    class_a = pinsource.source_from_part_spec(
        spec,
        spec_sha256=part_spec_sha256(spec_path),
        spec_path=spec_path,
    )
    source_inputs = _combined_pin_source_inputs(pin_source_path, pin_sources)
    source_input_records = _pin_source_input_records(source_inputs)
    if pin_source_path is not None and source_input_records and pin_source_sha256 is not None:
        source_input_records[0]["sha256"] = pin_source_sha256
    parsed_sources: list[pinsource.PinSource] = []
    comparison: pinsource.PinSourceComparison | None = None
    errors: list[str] = []
    for source_input in source_inputs:
        try:
            parsed_sources.append(
                pinsource.parse_pin_source(
                    source_input.path,
                    kind=source_input.kind,
                    pinout_name=source_input.pinout_name,
                    derived_from=source_input.derived_from,
                )
            )
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    if parsed_sources:
        comparison = pinsource.compare_pin_sources(class_a, parsed_sources)
    class_b = parsed_sources[0] if parsed_sources else None
    class_b_input = source_input_records[0] if source_input_records else None
    return {
        "single_source": comparison is None or len(comparison.independent_lineages) < 2,
        "class_a": class_a.model_dump(mode="json"),
        "class_b": class_b.model_dump(mode="json") if class_b is not None else None,
        "class_b_input": class_b_input,
        "sources": [item.model_dump(mode="json") for item in parsed_sources],
        "source_inputs": source_input_records,
        "comparison": comparison.model_dump(mode="json") if comparison is not None else None,
        "error": "; ".join(errors) if errors else None,
    }


def build_review_packet(
    spec_path: Path,
    *,
    symbol_lib: Path,
    symbol_name: str,
    footprint_path: Path,
    library_dir: Path,
    density: Density,
    tolerance_mm: float = 0.02,
    model_required: bool = True,
    pin_source_path: Path | None = None,
    pin_sources: Sequence[pinsource.PinSourceInput] | None = None,
    out_dir: Path,
) -> ReviewPacket:
    """Build fresh deterministic and human-review evidence for a library part."""
    spec = load_part_spec(spec_path)
    spec_dir = spec_path.resolve().parent
    project_root = confidential.project_root_for(spec_path)
    if spec.datasheet.confidential:
        out_dir = confidential.ensure_confidential_store(project_root) / "library" / "reviews"
    pin_source_sha256 = _optional_sha256(pin_source_path) if pin_source_path is not None else None
    pin_source_kind = pin_source_path.suffix.casefold() if pin_source_path is not None else None
    pin_source_inputs = _combined_pin_source_inputs(pin_source_path, pin_sources)
    pin_source_records = _pin_source_input_records(pin_source_inputs)
    pin_source_document = _pin_source_review_document(
        spec,
        spec_path=spec_path,
        pin_source_path=pin_source_path,
        pin_source_sha256=pin_source_sha256,
        pin_sources=pin_sources,
    )
    authoring_comparison: authoring.AuthoringComparison | None = None
    authoring_error: str | None = None
    try:
        authoring_comparison = _fresh_authoring_comparison(spec, spec_dir)
    except (OSError, ValueError) as exc:
        authoring_error = str(exc)
    pdf_path = _relative_or_absolute(spec_dir, spec.datasheet.path)
    extraction_path = _relative_or_absolute(spec_dir, spec.datasheet.extraction_path)
    try:
        stored_extraction = datasheet.load_extraction(extraction_path)
        dpi = stored_extraction.pages[0].dpi if stored_extraction.pages else 300
    except (OSError, ValueError, datasheet.DatasheetError):
        dpi = 300
    cited_pages = sorted(
        {
            reading.page
            for _, reading, _ in _readings(spec)
            if reading.page is not None and reading.alternative_evidence is None
        }
        | {spec.pin_table.page}
    )

    model_hashes: list[str] = []
    model_records: list[VerifiedModel] = []
    footprint_def: FootprintDef | None = None
    symbol_def: SymbolDef | None = None
    try:
        footprint_def = parse_footprint(footprint_path)
        model_hashes, model_records = _model_hashes(
            footprint_def,
            footprint_path=footprint_path,
            library_dir=library_dir,
            project_dir=library_dir.parent,
        )
    except (OSError, ValueError):
        pass

    def safe_hash(path: Path) -> str:
        try:
            return _sha256(path) if path.is_file() else ""
        except OSError:
            return ""

    pdf_sha256 = safe_hash(pdf_path)
    part_spec_hash = safe_hash(spec_path)
    symbol_lib_sha256 = safe_hash(symbol_lib)
    footprint_sha256 = safe_hash(footprint_path)
    lineage_path = lineage_path_for(footprint_path)
    lineage_sha256 = _optional_sha256(lineage_path)
    lineage = _load_lineage(footprint_path)
    rules, rule_chain_resolved = _review_rules(lineage, library_dir / "rules")
    input_hashes: dict[str, Any] = {
        "pdf_sha256": pdf_sha256,
        "part_spec_sha256": part_spec_hash,
        "symbol_lib_sha256": symbol_lib_sha256,
        "symbol_name": symbol_name,
        "footprint_sha256": footprint_sha256,
        "model_sha256s": sorted(model_hashes),
        "density": density,
        "tolerance_mm": tolerance_mm,
        "model_required": model_required,
        "lineage_sha256": lineage_sha256,
        "rule_chain_sha256": rules.chain_sha256,
        "authoring_sha256s": (
            sorted(authoring_comparison.sealed.values()) if authoring_comparison is not None else []
        ),
        "pin_source_path": (
            str(pin_source_path.resolve()) if pin_source_path is not None else None
        ),
        "pin_source_sha256": pin_source_sha256,
        "pin_source_kind": pin_source_kind,
    }
    if pin_sources:
        input_hashes["pin_sources"] = pin_source_records
    base_packet_id = packet_id(
        pdf_sha256=pdf_sha256,
        part_spec_sha256=part_spec_hash,
        symbol_lib_sha256=symbol_lib_sha256,
        symbol_name=symbol_name,
        footprint_sha256=footprint_sha256,
        model_sha256s=model_hashes,
        density=density,
        tolerance_mm=tolerance_mm,
        model_required=model_required,
        authoring_sha256s=(
            authoring_comparison.sealed.values() if authoring_comparison is not None else ()
        ),
        lineage_sha256=lineage_sha256,
        rule_chain_sha256=rules.chain_sha256,
        pin_source_sha256=pin_source_sha256,
        pin_source_kind=pin_source_kind,
        pin_source_inputs=pin_source_records if pin_sources else (),
    )
    agent_request = _matching_agent_request(
        out_dir / _safe_field(spec.mpn),
        input_hashes,
        base_packet_id,
    )
    if agent_request is None:
        agent_request = _matching_project_request(
            project_root,
            spec,
            input_hashes,
            base_packet_id,
        )
    if agent_request is None:
        agent_request = _build_agent_request(
            spec,
            base_packet_id=base_packet_id,
            input_hashes=input_hashes,
        )
    humanrequest.write_request(
        agent_request,
        project_root,
        confidential=spec.datasheet.confidential,
    )
    current_id = packet_id(
        pdf_sha256=pdf_sha256,
        part_spec_sha256=part_spec_hash,
        symbol_lib_sha256=symbol_lib_sha256,
        symbol_name=symbol_name,
        footprint_sha256=footprint_sha256,
        model_sha256s=model_hashes,
        density=density,
        tolerance_mm=tolerance_mm,
        model_required=model_required,
        authoring_sha256s=(
            authoring_comparison.sealed.values() if authoring_comparison is not None else ()
        ),
        lineage_sha256=lineage_sha256,
        rule_chain_sha256=rules.chain_sha256,
        pin_source_sha256=pin_source_sha256,
        pin_source_kind=pin_source_kind,
        pin_source_inputs=pin_source_records if pin_sources else (),
        request_sha256=agent_request.request_sha256,
    )
    packet_dir = out_dir / _safe_field(spec.mpn) / current_id
    packet_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = packet_dir / "evidence"
    crop_dir = packet_dir / "crops"
    findings: list[ReviewFinding] = []
    unknowns: list[str] = []
    if authoring_error is not None:
        findings.append(
            ReviewFinding(
                code="authoring_invalid",
                severity="error",
                field="authoring",
                message=authoring_error,
            )
        )
    if lineage is not None and not rule_chain_resolved:
        findings.append(
            ReviewFinding(
                code="lineage_rule_chain_unresolved",
                severity="error",
                field="lineage.base.rule_chain_sha256",
                message="no current rule profile chain matches the lineage base hash",
            )
        )
    extraction: DatasheetExtraction | None = None
    fresh_extraction_path = evidence_dir / "extraction.json"
    try:
        extraction = datasheet.extract_datasheet(
            pdf_path,
            evidence_dir,
            pages=cited_pages,
            dpi=dpi,
        )
        part_check = check_part_spec(
            spec,
            extraction,
            spec_path=spec_path,
            extraction_path=fresh_extraction_path,
        )
        findings.extend(_finding_from_spec(item) for item in part_check.findings)
    except Exception as exc:
        part_check = PartSpecReport(
            artifact_kind="circuit_part_spec_check",
            verdict="fail",
            part_spec_sha256=part_spec_hash,
            extraction_sha256="",
            pdf_sha256=pdf_sha256,
            checked_readings=0,
            findings=[
                SpecFinding(
                    code="fresh_part_spec_check_failed",
                    severity="error",
                    field="part_spec",
                    message=f"fresh PartSpec extraction/check failed: {exc}",
                )
            ],
        )
        findings.extend(_finding_from_spec(item) for item in part_check.findings)

    spec_check_path = packet_dir / "part-spec-check.json"
    try:
        spec_check_path.write_text(part_check.model_dump_json(indent=2), encoding="utf-8")
    except OSError as exc:
        findings.append(
            ReviewFinding(
                code="part_spec_report_write_failed",
                severity="error",
                field="part_spec_check",
                message=str(exc),
            )
        )

    reference: LandPatternResult | None = None
    verification: LibraryVerification | None = None
    try:
        reference = compute_land_pattern(spec, density, rules=rules)
        verification = verify_library_part(
            spec,
            spec_path=spec_path,
            spec_check_path=spec_check_path,
            symbol_lib=symbol_lib,
            symbol_name=symbol_name,
            footprint_path=footprint_path,
            library_dir=library_dir,
            reference=reference,
            tolerance_mm=tolerance_mm,
            model_required=model_required,
            rules=rules,
            pin_source_path=pin_source_path,
            pin_sources=pin_sources,
            output_path=packet_dir / "verification.json",
        )
        findings.extend(_finding_from_verify(item) for item in verification.findings)
        model_records = verification.models
    except Exception as exc:
        findings.append(
            ReviewFinding(
                code="fresh_library_verification_failed",
                severity="error",
                field="verification",
                message=f"fresh library verification failed: {exc}",
            )
        )
    findings.extend(correction_regressions(library_dir, spec))

    crops: dict[str, _CropRecord] = {}
    page_by_number = {page.page: page for page in extraction.pages} if extraction else {}
    if extraction is not None:
        for field, page_number, bbox in _cited_bboxes(spec, extraction, evidence_dir):
            page = page_by_number.get(page_number)
            if page is None:
                continue
            try:
                source = _page_path(evidence_dir, page)
                target = crop_dir / f"{_safe_field(field)}.png"
                record = _crop_image(
                    source,
                    target,
                    bbox=bbox,
                    page=page,
                    field=field,
                )
                record.path = _relative_path(packet_dir, target)
                crops[field] = record
            except (OSError, ValueError) as exc:
                findings.append(
                    ReviewFinding(
                        code="evidence_crop_failed",
                        severity="warning",
                        field=field,
                        message=str(exc),
                        page=page_number,
                    )
                )
    else:
        unknowns.append("evidence_extraction_unavailable")
    if extraction is not None:
        try:
            crops.update(
                _drawing_view_crops(
                    spec,
                    extraction,
                    evidence_dir,
                    packet_dir,
                    crops,
                )
            )
        except (OSError, ValueError, datasheet.DatasheetError) as exc:
            findings.append(
                ReviewFinding(
                    code="evidence_crop_failed",
                    severity="warning",
                    field="drawing_view",
                    message=str(exc),
                )
            )

    symbol_def: SymbolDef | None = None
    with suppress(OSError, ValueError):
        symbol_def = parse_symbol(symbol_lib, symbol_name)
    vision_reads = _vision_read_records(spec, spec_dir)
    vision_by_field = {str(item["field"]): item for item in vision_reads}
    comparison_evidence = _comparison_evidence(
        spec,
        spec_path=spec_path,
        symbol_lib=symbol_lib,
        footprint_path=footprint_path,
        models=model_records,
        findings=findings,
    )
    for evidence in comparison_evidence:
        for image_item in evidence.batch.items:
            try:
                record = _copy_comparison_crop(evidence, image_item, crop_dir)
                record.path = _relative_path(packet_dir, Path(record.path))
                crops[record.field] = record
            except (OSError, ValueError) as exc:
                findings.append(
                    ReviewFinding(
                        code="comparison_image_unavailable",
                        severity="warning",
                        field=image_item.field,
                        message=str(exc),
                        page=image_item.page,
                    )
                )
    pin_rows = _pin_table_rows(spec, extraction, evidence_dir) if extraction is not None else []
    pin_rows = _augment_pin_rows(pin_rows, symbol_def, footprint_def)
    _attach_pin_vision(pin_rows, vision_by_field)
    pinout_rows = _pinout_comparison_rows(
        spec,
        part_check.pinout,
        symbol_def,
        vision_by_field.get("pinout.labels_vision"),
    )

    renders: list[dict[str, str]] = []
    render_dir = packet_dir / "renders"
    render_dir.mkdir(parents=True, exist_ok=True)
    render_sources = [
        ("footprint_svg", "fp_svg", footprint_path.parent, None),
        ("symbol_svg", "sym_svg", symbol_lib, symbol_name),
    ]
    for label, export_kind, source, selected_symbol in render_sources:
        try:
            typed_export_kind = cast(kicad_cli.ExportKind, export_kind)
            if selected_symbol is None:
                exported = kicad_cli.export(typed_export_kind, source, render_dir / label)
            else:
                exported = kicad_cli.export(
                    typed_export_kind,
                    source,
                    render_dir / label,
                    symbol_name=selected_symbol,
                )
            selected = next(
                (
                    path
                    for path in exported
                    if path.suffix.lower() == ".svg"
                    and (
                        path.stem == footprint_path.stem
                        if label == "footprint_svg"
                        else path.stem == symbol_name
                    )
                ),
                next((path for path in exported if path.suffix.lower() == ".svg"), None),
            )
            if selected is not None:
                renders.append(
                    {
                        "kind": label,
                        "path": _relative_path(packet_dir, selected),
                        "sha256": _sha256(selected),
                    }
                )
        except (kicad_cli.KicadCliError, OSError, ValueError) as exc:
            detail = str(exc).casefold()
            if (
                "unavailable" in detail
                or "not found" in detail
                or "no such file or directory" in detail
                or "errno 2" in detail
            ):
                unknowns.append("render_unavailable")
            else:
                unknowns.append(f"render_failed:{label}")
    if (
        not any(item["kind"] == "symbol_svg" for item in renders)
        and "render_unavailable" not in unknowns
    ):
        unknowns.append("symbol_render_missing")
    if (
        not any(item["kind"] == "footprint_svg" for item in renders)
        and "render_unavailable" not in unknowns
    ):
        unknowns.append("footprint_render_missing")
    overlay_record: dict[str, Any] | None = None
    overlay_blink_record: dict[str, Any] | None = None
    land_crop_field = "land_pattern.drawing_view" if "land_pattern.drawing_view" in crops else None
    land_pattern_crop = crops.get(land_crop_field) if land_crop_field is not None else None
    if reference is not None and footprint_def is not None and land_pattern_crop is not None:
        try:
            source_page = page_by_number.get(land_pattern_crop.page)
            dpi = source_page.dpi if source_page is not None else 0
            geometry = _OverlayGeometry()
            try:
                with pdfplumber.open(pdf_path) as pdf:
                    if 1 <= land_pattern_crop.page <= len(pdf.pages):
                        geometry = _derive_overlay_geometry(
                            pdf.pages[land_pattern_crop.page - 1],
                            land_pattern_crop,
                            spec,
                            footprint_def,
                            reference,
                            dpi=dpi,
                        )
            except Exception:
                geometry = _OverlayGeometry()
            overlay_path = packet_dir / "overlay.svg"
            crop_image_path = packet_dir / land_pattern_crop.path
            _overlay_svg(
                spec,
                footprint_def,
                crop_image_path,
                _relative_path(packet_dir, crop_image_path),
                overlay_path,
                geometry=geometry,
            )
            unknowns.extend(
                code for code in _overlay_unknown_codes(geometry) if code not in unknowns
            )
            overlay_record = {
                "path": _relative_path(packet_dir, overlay_path),
                "sha256": _sha256(overlay_path),
                "scale_known": geometry.scale_known,
                "scale_pt_per_mm": geometry.scale_pt_per_mm,
                "pad_size_estimate_pt_per_mm": geometry.pad_size_estimate_pt_per_mm,
                "pitch_estimate_pt_per_mm": geometry.pitch_estimate_pt_per_mm,
                "scale_px_per_mm": geometry.scale_px_per_mm,
                "candidate_pad_count": geometry.candidate_pad_count,
                "page_dpi": dpi,
                "crop_upscale": land_pattern_crop.scale,
                "anchor": {
                    "kind": geometry.anchor_kind,
                    "page_pt": geometry.anchor_page_pt,
                    "crop_px": geometry.anchor_crop_px,
                    "footprint_mm": geometry.anchor_footprint_mm,
                },
            }
            if geometry.scale_known:
                cad_path = packet_dir / "overlay-cad.svg"
                _overlay_svg(
                    spec,
                    footprint_def,
                    crop_image_path,
                    _relative_path(packet_dir, crop_image_path),
                    cad_path,
                    geometry=geometry,
                    include_background=False,
                )
                blink_path = packet_dir / "overlay-blink.html"
                _atomic_write(
                    blink_path,
                    _blink_html(
                        _relative_path(packet_dir, crop_image_path),
                        _relative_path(packet_dir, cad_path),
                    ),
                )
                overlay_blink_record = {
                    "path": _relative_path(packet_dir, blink_path),
                    "sha256": _sha256(blink_path),
                    "cad_path": _relative_path(packet_dir, cad_path),
                    "cad_sha256": _sha256(cad_path),
                }
        except (OSError, ValueError) as exc:
            findings.append(
                ReviewFinding(
                    code="overlay_failed",
                    severity="warning",
                    field="overlay",
                    message=str(exc),
                )
            )
            if "overlay_scale_unknown" not in unknowns:
                unknowns.append("overlay_scale_unknown")
    elif "overlay_scale_unknown" not in unknowns:
        unknowns.append("overlay_scale_unknown")

    questions = blind_questions(
        spec,
        current_id,
        authoring_comparison,
        comparison_evidence,
    )
    dimensions = _dimension_records(spec, crops, vision_by_field)
    trial_state, trial_state_error = _review_trial_for_packet(
        spec,
        current_id,
        dimensions,
        crops,
        library_dir,
        packet_dir / "review.json",
    )
    if trial_state_error is not None:
        findings.append(
            ReviewFinding(
                code=trial_state_error,
                severity="error",
                field="review_trial",
                message="stored review trial state is invalid or does not match this packet",
            )
        )
    if trial_state is not None:
        questions.extend(trial_state.questions)
    extracted_pages: list[dict[str, Any]] = (
        [
            {
                "page": page.page,
                "path": _relative_path(packet_dir, _page_path(evidence_dir, page)),
                "sha256": page.png_sha256,
                "dpi": page.dpi,
            }
            for page in extraction.pages
        ]
        if extraction is not None
        else []
    )
    artifact_hashes: dict[str, str] = {
        key: value
        for key, value in input_hashes.items()
        if key.endswith("sha256") and isinstance(value, str) and value
    }
    artifact_hashes.update({f"crop:{field}": crop.sha256 for field, crop in crops.items()})
    artifact_hashes.update(
        {f"page:{item['page']}": str(item["sha256"]) for item in extracted_pages}
    )
    artifact_hashes.update(
        {f"model:{index}": digest for index, digest in enumerate(sorted(model_hashes))}
    )
    if authoring_comparison is not None:
        artifact_hashes.update(
            {f"authoring:{lane}": digest for lane, digest in authoring_comparison.sealed.items()}
        )
    artifact_hashes.update({f"render:{item['kind']}": item["sha256"] for item in renders})
    if overlay_record is not None:
        artifact_hashes["overlay"] = str(overlay_record["sha256"])
    if overlay_blink_record is not None:
        artifact_hashes["overlay_blink"] = str(overlay_blink_record["sha256"])
        artifact_hashes["overlay_cad"] = str(overlay_blink_record["cad_sha256"])
    vision_review_images: list[dict[str, str]] = []
    if overlay_record is not None:
        overlay_image = _vision_review_image(
            packet_dir / str(overlay_record["path"]),
            "footprint",
        )
        overlay_image["display_path"] = str(overlay_record["path"])
        vision_review_images.append(overlay_image)
    comparison_images = _comparison_review_images(comparison_evidence)
    for image in comparison_images:
        for evidence in comparison_evidence:
            artifact_kind = evidence.item.kind.removeprefix("compare_")
            for image_item in evidence.batch.items:
                if _comparison_image_path(evidence, image_item).as_posix() == image["path"]:
                    record = crops.get(f"comparison.{artifact_kind}.{image_item.read_id}")
                    if record is not None:
                        image["display_path"] = record.path
                    break
        image.setdefault("display_path", image["path"])
    vision_review_images.extend(comparison_images)
    vision_review_images.sort(key=lambda item: item["path"])
    artifact_hashes.update(
        {
            f"vision_review_image:{index}": item["sha256"]
            for index, item in enumerate(vision_review_images)
        }
    )

    def project_asset_link(path: Path) -> str | None:
        project_root = library_dir.parent.resolve()
        try:
            resolved = path.resolve()
            resolved.relative_to(project_root)
        except (OSError, ValueError):
            return None
        return Path(os.path.relpath(resolved, packet_dir.resolve())).as_posix()

    def lineage_asset_link(value: str) -> str | None:
        relative = Path(value)
        if relative.is_absolute():
            return None
        return project_asset_link(library_dir.parent / relative)

    deviations: list[dict[str, Any]] = []
    if verification is not None:
        marker = "deltas against reference and base in mm: "
        for item in verification.findings:
            if item.code != "intentional_tuning" or marker not in item.message:
                continue
            try:
                parsed = json.loads(item.message.split(marker, 1)[1])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list):
                deviations.extend(
                    cast(dict[str, Any], row)
                    for row in cast(list[object], parsed)
                    if isinstance(row, dict)
                )

    footprint_tuning: dict[str, Any] | None = None
    if lineage_sha256 is not None:
        footprint_tuning = {
            "lineage_path": str(lineage_path),
            "lineage_link": project_asset_link(lineage_path),
            "lineage_sha256": lineage_sha256,
            "intentional_deviations": deviations,
        }
        if lineage is not None:
            evidence = [
                {
                    **item.model_dump(mode="json"),
                    "link": lineage_asset_link(item.path),
                }
                for item in lineage.evidence
            ]
            footprint_tuning.update(
                {
                    "layer": lineage.layer,
                    "product": lineage.product,
                    "reason": lineage.reason,
                    "base": {
                        **lineage.base.model_dump(mode="json"),
                        "link": lineage_asset_link(lineage.base.path),
                    },
                    "changes": [item.model_dump(mode="json") for item in lineage.changes],
                    "evidence": evidence,
                }
            )

    severity_order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda item: (severity_order[item.severity], item.code, item.field))
    alternative_requests, alternative_unknown_fields = _alternative_evidence_review_data(
        spec,
        project_root,
    )
    alternative_evidence_fields = sorted(
        {field for field, reading, _ in _readings(spec) if reading.alternative_evidence is not None}
    )
    for unknown_field in reversed(alternative_unknown_fields):
        unknowns.insert(0, f"alternative_evidence_unknown:{unknown_field}")
    substitution_display = (
        {
            "target_mpn": (
                part_check.substitute_permit.target_mpn
                if part_check.substitute_permit is not None
                else spec.mpn
            ),
            "substitute_mpn": (
                part_check.substitute_permit.substitute_mpn
                if part_check.substitute_permit is not None
                else spec.substitution.substitute_mpn
            ),
            "granted_scope": (
                part_check.substitute_permit.granted_scope
                if part_check.substitute_permit is not None
                else []
            ),
            "unverifiable_fields": (
                part_check.substitute_permit.unverifiable_fields
                if part_check.substitute_permit is not None
                else []
            ),
        }
        if spec.substitution is not None
        else None
    )
    approvable = (
        part_check.verdict == "pass"
        and verification is not None
        and verification.verdict == "pass"
        and authoring_comparison is not None
        and not any(issue.severity == "error" for issue in authoring_comparison.issues)
        and not any(item.code == "correction_regressed" for item in findings)
        and not any(
            item.code
            in {
                "model_vision_comparison_missing",
                "model_vision_control_failed",
                "model_vision_record_invalid",
            }
            for item in findings
        )
    )
    message_template = _message_template(current_id, questions)
    review_document: dict[str, Any] = {
        "artifact_kind": "circuit_library_review_packet",
        "packet_id": current_id,
        "confidential": spec.datasheet.confidential,
        "substitution": substitution_display,
        "alternative_evidence": alternative_requests,
        "alternative_evidence_fields": alternative_evidence_fields,
        "alternative_evidence_unknown_fields": alternative_unknown_fields,
        "inputs": {
            **input_hashes,
            "spec_path": str(spec_path.resolve()),
            "symbol_lib": str(symbol_lib.resolve()),
            "footprint_path": str(footprint_path.resolve()),
            "library_dir": str(library_dir.resolve()),
        },
        "fresh_part_spec_check": part_check.model_dump(mode="json"),
        "fresh_library_verification": (
            verification.model_dump(mode="json") if verification is not None else None
        ),
        "findings": [item.model_dump(mode="json") for item in findings],
        "blind_questions": [
            {
                "question_id": item.question_id,
                "prompt": item.prompt,
                "page": item.page,
                "bbox": item.bbox,
                "evidence_field": item.evidence_field,
            }
            for item in questions
        ],
        "review_trial_state_sha256": (
            _sha256(_review_trial_path(library_dir, current_id))
            if trial_state is not None
            else None
        ),
        "vision_review_images": vision_review_images,
        "pin_comparisons": pin_rows,
        "pin_sources": pin_source_document,
        "pinout_comparisons": pinout_rows,
        "vision_reads": vision_reads,
        "authoring_comparison": (
            authoring_comparison.model_dump(mode="json")
            if authoring_comparison is not None
            else None
        ),
        "dimensions": dimensions,
        "evidence_pages": extracted_pages,
        "crops": [item.model_dump(mode="json") for item in crops.values()],
        "renders": renders,
        "models": [item.model_dump(mode="json") for item in model_records],
        "footprint_tuning": footprint_tuning,
        "rule_chain": {
            "chain": rules.chain,
            "chain_sha256": rules.chain_sha256,
            "profiles": [item.model_dump(mode="json") for item in rules.profile_chain],
        },
        "overlay": overlay_record,
        "overlay_blink": overlay_blink_record,
        "land_pattern_crop": (
            {
                "path": land_pattern_crop.path,
                "sha256": land_pattern_crop.sha256,
                "page": land_pattern_crop.page,
            }
            if land_pattern_crop is not None
            else None
        ),
        "artifact_hashes": artifact_hashes,
        "approvable": approvable,
        "unknowns": sorted(set(unknowns)),
        "message_template": message_template,
        "agent_request": agent_request.model_dump(mode="json"),
    }
    (packet_dir / "review.json").write_text(
        json.dumps(review_document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (packet_dir / "01-blind.html").write_text(
        _blind_html(current_id, questions, crops, spec, packet_dir),
        encoding="utf-8",
    )
    (packet_dir / "02-review.html").write_text(
        _review_html(review_document),
        encoding="utf-8",
    )
    return ReviewPacket(
        artifact_kind="circuit_library_review_packet",
        packet_id=current_id,
        packet_dir=packet_dir,
        approvable=approvable,
        inputs=review_document["inputs"],
        findings=findings,
        unknowns=review_document["unknowns"],
        agent_request=agent_request,
    )
