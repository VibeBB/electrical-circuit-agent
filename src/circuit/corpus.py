"""Hash-checked golden corpus inputs and read-only scoring."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import libitems, libreview, occt, partspec
from .libitems import FootprintDef, SymbolDef
from .partspec import Dimension, PartSpec

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CANARY_PREFIX = "CIRCUIT-CORPUS-CANARY-"
_CANARY_RE = re.compile(
    r"CIRCUIT-CORPUS-CANARY-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_DIMENSION_FIELDS = (
    "body_length",
    "body_width",
    "height",
    "pitch",
    "lead_span",
    "lead_length",
    "lead_width",
    "exposed_pad_length",
    "exposed_pad_width",
)


class CorpusError(ValueError):
    """Raised when a corpus manifest or truth file is invalid."""


class CorpusDatasheet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(min_length=1)


class CorpusEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    manufacturer: str = Field(min_length=1)
    mpn: str = Field(min_length=1)
    package_family: str = Field(min_length=1)
    datasheet: CorpusDatasheet
    truth_path: str = Field(min_length=1)
    truth_status: Literal["human_confirmed", "unconfirmed"]
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    notes: str


class CorpusManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: Literal["circuit_golden_corpus"] = Field(alias="schema")
    version: Literal[1]
    entries: list[CorpusEntry]

    @model_validator(mode="after")
    def unique_entries(self) -> CorpusManifest:
        ids = [entry.id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("corpus entry ids must be unique")
        return self


class CorpusDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: float | None = None
    nom: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def ordered(self) -> CorpusDimension:
        values = [value for value in (self.min, self.nom, self.max) if value is not None]
        if values and values != sorted(values):
            raise ValueError("corpus dimensions must satisfy min <= nom <= max")
        return self


class CorpusDimensions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body_length: CorpusDimension | None = None
    body_width: CorpusDimension | None = None
    height: CorpusDimension | None = None
    pitch: CorpusDimension | None = None
    lead_span: CorpusDimension | None = None
    lead_length: CorpusDimension | None = None
    lead_width: CorpusDimension | None = None
    exposed_pad_length: CorpusDimension | None = None
    exposed_pad_width: CorpusDimension | None = None


class CorpusPad(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    center: tuple[float, float]
    size: tuple[float, float]
    drill: float | None = Field(default=None, gt=0)
    shape: Literal["rect", "roundrect", "oval", "circle", "polygon"] | None = None


class CorpusTruth(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: Literal["circuit_corpus_truth"] = Field(alias="schema")
    version: Literal[1]
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    package_family: str
    pins: dict[str, str]
    pin1_corner: Literal["top_left", "top_right", "bottom_left", "bottom_right"]
    drawing_view: Literal["top", "bottom"]
    dimensions: CorpusDimensions
    expected_pads: list[CorpusPad]
    canary: str
    notes: str

    @model_validator(mode="after")
    def validate_canary(self) -> CorpusTruth:
        match = _CANARY_RE.fullmatch(self.canary)
        if match is None:
            raise ValueError("canary must be CIRCUIT-CORPUS-CANARY-<uuid4>")
        value = self.canary.removeprefix(_CANARY_PREFIX)
        if str(UUID(value, version=4)) != value:
            raise ValueError("canary must contain a canonical UUID4")
        if len(self.pins) != len(set(self.pins)):
            raise ValueError("truth pin numbers must be unique")
        return self


class CorpusApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: Literal["circuit_corpus_truth_approval"] = Field(alias="schema")
    version: Literal[1]
    entry_id: str
    packet_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    truth_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1)
    confirmed_at: str = Field(min_length=1)


class CorpusFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["error", "warning"]
    field: str
    message: str
    expected: Any | None = None
    actual: Any | None = None


class CorpusScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_corpus_score"] = "circuit_corpus_score"
    entry_id: str
    verdict: Literal["pass", "fail", "not_available"]
    truth_status: Literal["human_confirmed", "unconfirmed"]
    datasheet_status: Literal["verified", "not_available", "hash_mismatch"]
    truth_sha256: str
    manifest_sha256: str
    artifact_hashes: dict[str, str]
    findings: list[CorpusFinding]


def _ensure_scoring_allowed() -> None:
    if os.environ.get("CIRCUIT_AUTHORING_LANE") in {"a", "b"}:
        raise CorpusError("author lanes cannot read or score golden corpus truth")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(manifest_path: Path) -> CorpusManifest:
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        return CorpusManifest.model_validate(document)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise CorpusError(f"corpus manifest is unavailable or invalid: {exc}") from exc


def _truth_path(corpus_root: Path, entry: CorpusEntry) -> Path:
    root = (corpus_root / "truth").resolve()
    path = (corpus_root / entry.truth_path).resolve()
    if not path.is_relative_to(root):
        raise CorpusError("truth_path must resolve inside the corpus truth directory")
    return path


def load_truth(corpus_root: Path, entry: CorpusEntry) -> tuple[CorpusTruth, str]:
    _ensure_scoring_allowed()
    path = _truth_path(corpus_root, entry)
    try:
        raw = path.read_bytes()
        truth = CorpusTruth.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        raise CorpusError(f"corpus truth is unavailable or invalid: {exc}") from exc
    if truth.id != entry.id:
        raise CorpusError("truth id does not match its manifest entry")
    return truth, hashlib.sha256(raw).hexdigest()


def approval_packet_id(entry_id: str, truth_sha256: str) -> str:
    if _SHA256_RE.fullmatch(truth_sha256) is None:
        raise CorpusError("truth_sha256 must be a lowercase SHA-256 digest")
    material = f"circuit-corpus-truth-v1:{entry_id}:{truth_sha256}".encode()
    return hashlib.sha256(material).hexdigest()[:16]


def _approval_is_valid(
    corpus_root: Path,
    entry: CorpusEntry,
    truth_sha256: str,
) -> tuple[bool, str]:
    if entry.truth_status != "human_confirmed":
        return False, "corpus_truth_unconfirmed"
    if not entry.confirmed_by or not entry.confirmed_at:
        return False, "corpus_confirmation_fields_missing"
    try:
        datetime.fromisoformat(entry.confirmed_at.replace("Z", "+00:00"))
    except ValueError:
        return False, "corpus_confirmation_time_invalid"
    packet_id = approval_packet_id(entry.id, truth_sha256)
    approval_path = corpus_root / "approvals" / f"{entry.id}.json"
    try:
        approval = CorpusApproval.model_validate_json(approval_path.read_bytes())
        decisions = libreview.load_decisions(corpus_root.parent, packet_id)
    except (OSError, ValueError) as exc:
        return False, f"corpus_approval_unavailable:{exc}"
    matches = [
        decision
        for decision in decisions
        if decision.valid
        and decision.integrity_valid
        and decision.decision == "approve"
        and decision.reviewer == entry.confirmed_by
        and decision.answers == {"truth_sha256": truth_sha256}
        and decision.event_sha256 is not None
    ]
    if len(matches) != 1:
        return False, "corpus_approval_event_invalid"
    decision = matches[0]
    if (
        approval.entry_id != entry.id
        or approval.packet_id != packet_id
        or approval.truth_sha256 != truth_sha256
        or approval.event_sha256 != decision.event_sha256
        or approval.reviewer != decision.reviewer
        or approval.confirmed_at != entry.confirmed_at
    ):
        return False, "corpus_approval_binding_mismatch"
    return True, "human_confirmed"


def _cached_pdf_status(entry: CorpusEntry) -> str:
    configured = (
        Path(os.environ["CIRCUIT_CORPUS_CACHE"]).expanduser()
        if os.environ.get("CIRCUIT_CORPUS_CACHE")
        else None
    )
    if configured is None or not configured.is_dir():
        return "not_available"
    try:
        pdfs = sorted(configured.glob("*.pdf"))
    except OSError:
        return "not_available"
    for path in pdfs:
        try:
            if sha256(path) == entry.datasheet.sha256:
                return "verified"
        except OSError:
            continue
    return "hash_mismatch" if pdfs else "not_available"


def _dimension_values(dimension: Dimension | None) -> dict[str, float | None] | None:
    if dimension is None:
        return None
    return {"min": dimension.min, "nom": dimension.nom, "max": dimension.max}


def _truth_dimension_values(dimension: CorpusDimension | None) -> dict[str, float | None] | None:
    if dimension is None:
        return None
    return {"min": dimension.min, "nom": dimension.nom, "max": dimension.max}


def _missing_truth_requirements(truth: CorpusTruth) -> list[str]:
    required = ["body_length", "body_width", "height", "pitch"]
    if truth.package_family.startswith("gullwing"):
        required.extend(("lead_span", "lead_length", "lead_width"))
    missing = [
        field
        for field in required
        if (dimension := getattr(truth.dimensions, field)) is None
        or all(value is None for value in (dimension.min, dimension.nom, dimension.max))
    ]
    if not truth.expected_pads:
        missing.append("expected_pads")
    if not truth.pins:
        missing.append("pins")
    return missing


def _part_dimensions(spec: PartSpec) -> dict[str, Dimension | None]:
    package = spec.package
    exposed_pad = package.exposed_pad
    return {
        "body_length": package.body_length,
        "body_width": package.body_width,
        "height": package.height,
        "pitch": package.pitch,
        "lead_span": package.lead_span,
        "lead_length": package.lead_length,
        "lead_width": package.lead_width,
        "exposed_pad_length": exposed_pad.length if exposed_pad is not None else None,
        "exposed_pad_width": exposed_pad.width if exposed_pad is not None else None,
    }


def _canonical_symbol_pins(symbol: SymbolDef) -> dict[str, set[str]]:
    pins: dict[str, set[str]] = {}
    for pin in symbol.pins:
        pins.setdefault(pin.number, set()).add(pin.name)
    return pins


def _check_footprint_pads(
    truth: CorpusTruth,
    footprint: FootprintDef,
    add_finding: Any,
    *,
    tolerance_mm: float,
) -> None:
    expected = truth.expected_pads
    if not expected:
        add_finding(
            "corpus_expected_pads_unavailable",
            "warning",
            "expected_pads",
            "truth does not contain a verified footprint pad set",
        )
        return
    actual_pads = [pad for pad in footprint.pads if pad.number]
    expected_numbers = Counter(pad.number for pad in expected)
    actual_numbers = Counter(pad.number for pad in actual_pads)
    if expected_numbers != actual_numbers:
        add_finding(
            "corpus_pad_numbers_mismatch",
            "error",
            "footprint.pads",
            "numbered footprint pads differ from corpus truth",
            dict(expected_numbers),
            dict(actual_numbers),
        )
        return
    actual_by_number = {pad.number: pad for pad in actual_pads}
    for pad in expected:
        actual = actual_by_number[pad.number]
        expected_geometry = (*pad.center, *pad.size)
        actual_geometry = (actual.x, actual.y, actual.width, actual.height)
        if any(
            abs(expected_value - actual_value) > tolerance_mm
            for expected_value, actual_value in zip(expected_geometry, actual_geometry, strict=True)
        ):
            add_finding(
                "corpus_pad_geometry_mismatch",
                "error",
                f"footprint.pad.{pad.number}",
                "pad center or size differs from corpus truth",
                expected_geometry,
                actual_geometry,
            )
        if pad.drill is not None and (actual.drill is None or abs(pad.drill - actual.drill) > 0.01):
            add_finding(
                "corpus_pad_drill_mismatch",
                "error",
                f"footprint.pad.{pad.number}.drill",
                "pad drill differs from corpus truth by more than 0.01 mm",
                pad.drill,
                actual.drill,
            )
        actual_shape = "polygon" if actual.shape == "custom" else actual.shape
        if pad.shape is not None and actual_shape != pad.shape:
            add_finding(
                "corpus_pad_shape_mismatch",
                "error",
                f"footprint.pad.{pad.number}.shape",
                "pad shape differs from corpus truth",
                pad.shape,
                actual_shape,
            )


def _model_dimensions(model: occt.Shape) -> tuple[dict[str, float], str | None]:
    facts = occt.inspect(model)
    if not facts.solids:
        return {}, "model contains no solids"
    bounds = [solid.bbox for solid in facts.solids]
    overall = {
        "x": max(bound.x_max for bound in bounds) - min(bound.x_min for bound in bounds),
        "y": max(bound.y_max for bound in bounds) - min(bound.y_min for bound in bounds),
        "z": max(bound.z_max for bound in bounds) - min(bound.z_min for bound in bounds),
    }
    return overall, None


def _dimension_bounds(
    dimension: CorpusDimension | None,
) -> tuple[float | None, float | None] | None:
    if dimension is None:
        return None
    lower = dimension.min if dimension.min is not None else dimension.nom
    upper = dimension.max if dimension.max is not None else dimension.nom
    if lower is None and upper is None:
        return None
    return lower, upper


def score_part(
    truth: CorpusTruth,
    partspec: PartSpec,
    footprint: FootprintDef,
    symbol: SymbolDef,
    model: occt.Shape,
) -> CorpusScore:
    """Compare parsed artifact values with truth without reading or writing files."""
    findings: list[CorpusFinding] = []

    def add_finding(
        code: str,
        severity: Literal["error", "warning"],
        field: str,
        message: str,
        expected: Any | None = None,
        actual: Any | None = None,
    ) -> None:
        findings.append(
            CorpusFinding(
                code=code,
                severity=severity,
                field=field,
                message=message,
                expected=expected,
                actual=actual,
            )
        )

    missing_truth = _missing_truth_requirements(truth)
    if missing_truth:
        add_finding(
            "corpus_truth_incomplete",
            "warning",
            "truth",
            "required truth fields are unavailable",
            missing_truth,
        )

    artifact_values = (
        partspec.model_dump_json(),
        footprint.model_dump_json(),
        symbol.model_dump_json(),
    )
    if any(_CANARY_RE.search(value) or _CANARY_PREFIX in value for value in artifact_values):
        add_finding(
            "corpus_canary_leak",
            "error",
            "artifacts",
            "an authored artifact contains a corpus canary",
        )

    actual_pins = {pin.number: pin.name for pin in partspec.pins}
    if actual_pins != truth.pins:
        add_finding(
            "corpus_pin_map_mismatch",
            "error",
            "partspec.pins",
            "PartSpec pin number/name map differs from corpus truth",
            truth.pins,
            actual_pins,
        )
    if partspec.package.pin1_corner != truth.pin1_corner:
        add_finding(
            "corpus_pin1_corner_mismatch",
            "error",
            "partspec.package.pin1_corner",
            "PartSpec pin-1 corner differs from corpus truth",
            truth.pin1_corner,
            partspec.package.pin1_corner,
        )
    if partspec.package.drawing_view != truth.drawing_view:
        add_finding(
            "corpus_drawing_view_mismatch",
            "error",
            "partspec.package.drawing_view",
            "PartSpec drawing view differs from corpus truth",
            truth.drawing_view,
            partspec.package.drawing_view,
        )
    if partspec.package.family != truth.package_family:
        add_finding(
            "corpus_package_family_mismatch",
            "error",
            "partspec.package.family",
            "PartSpec package family differs from corpus truth",
            truth.package_family,
            partspec.package.family,
        )
    for field_name in _DIMENSION_FIELDS:
        expected = getattr(truth.dimensions, field_name)
        expected_values = _truth_dimension_values(expected)
        if expected_values is None or all(value is None for value in expected_values.values()):
            continue
        actual_values = _dimension_values(_part_dimensions(partspec).get(field_name))
        if actual_values != expected_values:
            add_finding(
                "corpus_dimension_mismatch",
                "error",
                f"partspec.package.{field_name}",
                f"{field_name} limits differ from corpus truth",
                expected_values,
                actual_values,
            )

    _check_footprint_pads(
        truth,
        footprint,
        add_finding,
        tolerance_mm=0.02,
    )
    actual_symbol_pins = _canonical_symbol_pins(symbol)
    expected_symbol_pins = {number: {name} for number, name in truth.pins.items()}
    if actual_symbol_pins != expected_symbol_pins:
        add_finding(
            "corpus_symbol_pin_map_mismatch",
            "error",
            "symbol.pins",
            "symbol pin number/name map differs from corpus truth",
            {key: sorted(value) for key, value in expected_symbol_pins.items()},
            {key: sorted(value) for key, value in actual_symbol_pins.items()},
        )

    try:
        actual_model_dimensions, model_error = _model_dimensions(model)
        if model_error is not None:
            add_finding("model_geometry_mismatch", "error", "model", model_error)
        else:
            height = truth.dimensions.height
            model_expected_dimensions: dict[str, CorpusDimension | None] = {}
            if truth.package_family in {"no_lead_quad", "no_lead_dual"}:
                model_expected_dimensions = {
                    "x": truth.dimensions.body_width,
                    "y": truth.dimensions.body_length,
                }
            elif truth.package_family == "gullwing_dual":
                model_expected_dimensions = {
                    "x": truth.dimensions.lead_span,
                    "y": truth.dimensions.body_length,
                }
            elif truth.package_family == "gullwing_quad":
                model_expected_dimensions = {
                    "x": truth.dimensions.lead_span,
                    "y": truth.dimensions.lead_span,
                }
            model_expected_dimensions["z"] = height
            for axis, expected_dimension in model_expected_dimensions.items():
                bounds = _dimension_bounds(expected_dimension)
                if bounds is None:
                    continue
                lower, upper = bounds
                actual = actual_model_dimensions[axis]
                below_minimum = lower is not None and actual < lower - 0.02
                above_maximum = upper is not None and actual > upper + 0.02
                if below_minimum or above_maximum:
                    add_finding(
                        "model_geometry_mismatch",
                        "error",
                        f"model.{axis}",
                        f"overall STEP {axis.upper()} extent is outside corpus truth bounds",
                        {"min": lower, "max": upper},
                        actual,
                    )
    except Exception as exc:
        add_finding("model_geometry_mismatch", "error", "model", str(exc))

    errors = any(finding.severity == "error" for finding in findings)
    return CorpusScore(
        entry_id=truth.id,
        verdict="fail" if errors else "not_available",
        truth_status="unconfirmed",
        datasheet_status="not_available",
        truth_sha256=hashlib.sha256(truth.model_dump_json().encode("utf-8")).hexdigest(),
        manifest_sha256="",
        artifact_hashes={},
        findings=findings,
    )


def score_entry(
    corpus_root: Path,
    entry_id: str,
    partspec_path: Path,
    footprint_path: Path,
    symbol_path: Path,
    symbol_name: str,
    model_path: Path,
) -> CorpusScore:
    _ensure_scoring_allowed()
    manifest_path = corpus_root / "corpus.json"
    manifest = load_manifest(manifest_path)
    manifest_digest = sha256(manifest_path)
    entry = next((item for item in manifest.entries if item.id == entry_id), None)
    if entry is None:
        raise CorpusError(f"unknown corpus entry id: {entry_id}")
    truth, truth_digest = load_truth(corpus_root, entry)
    findings: list[CorpusFinding] = []
    artifact_hashes: dict[str, str] = {}
    artifact_values: list[str] = []
    artifact_paths = {
        "partspec": partspec_path,
        "footprint": footprint_path,
        "symbol": symbol_path,
        "model": model_path,
    }
    for name, path in artifact_paths.items():
        try:
            raw = path.read_bytes()
            artifact_hashes[name] = hashlib.sha256(raw).hexdigest()
            artifact_values.append(raw.decode("utf-8", errors="ignore"))
        except OSError as exc:
            findings.append(
                CorpusFinding(
                    code=f"{name}_unavailable",
                    severity="error",
                    field=name,
                    message=f"{name} artifact is unavailable: {exc}",
                )
            )
    canary_leaked = any(
        _CANARY_RE.search(value) or _CANARY_PREFIX in value for value in artifact_values
    )
    if canary_leaked:
        findings.append(
            CorpusFinding(
                code="corpus_canary_leak",
                severity="error",
                field="artifacts",
                message="an authored artifact contains a corpus canary",
            )
        )
    if sha256(manifest_path) != manifest_digest:
        findings.append(
            CorpusFinding(
                code="corpus_manifest_changed_during_scoring",
                severity="error",
                field="manifest",
                message="corpus manifest changed during scoring",
            )
        )
    if sha256(_truth_path(corpus_root, entry)) != truth_digest:
        findings.append(
            CorpusFinding(
                code="corpus_truth_changed_during_scoring",
                severity="error",
                field="truth",
                message="corpus truth changed during scoring",
            )
        )
    try:
        spec = partspec.load_part_spec(partspec_path)
    except Exception as exc:
        findings.append(
            CorpusFinding(
                code="partspec_unavailable",
                severity="error",
                field="partspec",
                message=f"PartSpec could not be freshly loaded: {exc}",
            )
        )
        score = CorpusScore(
            entry_id=entry_id,
            verdict="fail",
            truth_status="unconfirmed",
            datasheet_status="not_available",
            truth_sha256=truth_digest,
            manifest_sha256=manifest_digest,
            artifact_hashes=artifact_hashes,
            findings=findings,
        )
        return score
    try:
        footprint = libitems.parse_footprint(footprint_path)
    except (OSError, ValueError) as exc:
        findings.append(
            CorpusFinding(
                code="footprint_unavailable",
                severity="error",
                field="footprint",
                message=str(exc),
            )
        )
        footprint = FootprintDef(
            name="",
            attributes=[],
            pads=[],
            graphics=[],
            models=[],
            properties={},
        )
    try:
        symbol = libitems.parse_symbol(symbol_path, symbol_name)
    except (OSError, ValueError) as exc:
        findings.append(
            CorpusFinding(
                code="symbol_unavailable",
                severity="error",
                field="symbol",
                message=str(exc),
            )
        )
        symbol = SymbolDef(name="", pins=[], properties={})
    try:
        model = occt.read_step(model_path)
    except Exception as exc:
        findings.append(
            CorpusFinding(
                code="model_unavailable",
                severity="error",
                field="model",
                message=str(exc),
            )
        )
        model = occt.box(0, 0, 0, 0.001, 0.001, 0.001)

    score = score_part(truth, spec, footprint, symbol, model)
    combined_findings = [*score.findings, *findings]
    if any(item.code == "corpus_canary_leak" for item in score.findings):
        combined_findings = [
            item for item in combined_findings if item.code != "corpus_canary_leak"
        ]
        combined_findings.append(
            CorpusFinding(
                code="corpus_canary_leak",
                severity="error",
                field="artifacts",
                message="an authored artifact contains a corpus canary",
            )
        )

    approval_valid, approval_reason = _approval_is_valid(
        corpus_root,
        entry,
        truth_digest,
    )
    truth_status: Literal["human_confirmed", "unconfirmed"] = (
        "human_confirmed" if approval_valid else "unconfirmed"
    )
    if not approval_valid:
        combined_findings.append(
            CorpusFinding(
                code=approval_reason.split(":", 1)[0],
                severity="warning",
                field="truth_status",
                message=approval_reason,
            )
        )
    missing_truth = _missing_truth_requirements(truth)

    datasheet_status = cast(
        Literal["verified", "not_available", "hash_mismatch"],
        _cached_pdf_status(entry),
    )
    if datasheet_status == "not_available":
        combined_findings.append(
            CorpusFinding(
                code="datasheet_not_available",
                severity="warning",
                field="datasheet",
                message="the corpus datasheet PDF is not available in CIRCUIT_CORPUS_CACHE",
            )
        )
    elif datasheet_status == "hash_mismatch":
        combined_findings.append(
            CorpusFinding(
                code="datasheet_hash_mismatch",
                severity="error",
                field="datasheet",
                message="no cached PDF matches the corpus datasheet SHA-256",
                expected=entry.datasheet.sha256,
            )
        )

    errors = any(item.severity == "error" for item in combined_findings)
    verdict: Literal["pass", "fail", "not_available"] = (
        "fail"
        if errors
        else "pass"
        if truth_status == "human_confirmed"
        and datasheet_status == "verified"
        and not missing_truth
        else "not_available"
    )
    return score.model_copy(
        update={
            "entry_id": entry_id,
            "verdict": verdict,
            "truth_status": truth_status,
            "datasheet_status": datasheet_status,
            "truth_sha256": truth_digest,
            "manifest_sha256": manifest_digest,
            "artifact_hashes": artifact_hashes,
            "findings": combined_findings,
        }
    )
