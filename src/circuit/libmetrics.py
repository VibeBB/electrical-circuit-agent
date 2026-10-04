"""Hash-bound library review escape-rate metrics."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from . import corpus, libreview, mutation
from .partspec import PartSpec, load_part_spec
from .pinsource import PinSourceInput

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_CORPUS = Path(__file__).resolve().parents[2] / "library" / "corpus" / "corpus.json"
_CRITICAL_DIMENSIONS = {
    "body_length",
    "body_width",
    "height",
    "pitch",
    "standoff",
    "lead_span",
    "lead_length",
    "lead_width",
}


class LibraryMetricsError(ValueError):
    """Raised when stored metrics cannot authorize a relaxed review scope."""


class CorrectionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packet_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    mpn: str = Field(min_length=1)
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pointer: str = Field(min_length=2)
    old: Any
    new: Any
    reason: str = Field(min_length=1)
    page: int = Field(ge=1)
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LibraryMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: Literal["circuit_library_metrics"] = "circuit_library_metrics"
    accepted_parts: int = Field(ge=0)
    escapes: int = Field(ge=0)
    upper95: float = Field(ge=0, le=1)
    corpus_manifest_sha256: str | None
    mutation_report_sha256: str | None
    family_detection_rates: dict[mutation.CheckFamily, float]
    operator_outcomes: list[mutation.MutationOutcome]
    critical_single_oracle: list[str]
    release_relaxation_supported: bool
    findings: list[str]


def clopper_pearson_upper95(n: int, k: int) -> float:
    """Return the exact one-sided 95% Clopper-Pearson upper confidence bound."""
    if n < 0 or k < 0 or k > n:
        raise ValueError("Clopper-Pearson counts require 0 <= k <= n")
    if n == 0:
        return 1.0
    if k == n:
        return 1.0
    alpha = 0.05

    def binomial_cdf(probability: float) -> float:
        if probability <= 0:
            return 1.0
        if probability >= 1:
            return 0.0 if k < n else 1.0
        log_p = math.log(probability)
        log_q = math.log1p(-probability)
        log_terms = [
            math.lgamma(n + 1)
            - math.lgamma(index + 1)
            - math.lgamma(n - index + 1)
            + index * log_p
            + (n - index) * log_q
            for index in range(k + 1)
        ]
        maximum = max(log_terms)
        return min(1.0, math.exp(maximum) * sum(math.exp(value - maximum) for value in log_terms))

    lower, upper = 0.0, 1.0
    for _ in range(80):
        midpoint = (lower + upper) / 2
        if binomial_cdf(midpoint) > alpha:
            lower = midpoint
        else:
            upper = midpoint
    return (lower + upper) / 2


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_path(project: Path) -> Path:
    project_manifest = project / "library" / "corpus" / "corpus.json"
    return project_manifest if project_manifest.is_file() else _DEFAULT_CORPUS


def _mutation_report_path(project: Path) -> Path:
    return project / "library" / "mutation-report.json"


def _critical_field(pointer: str) -> str | None:
    if not pointer.startswith("/"):
        return None
    raw_tokens = pointer[1:].split("/")
    if any(re.search(r"~(?![01])", token) for token in raw_tokens):
        return None
    tokens = [token.replace("~1", "/").replace("~0", "~") for token in raw_tokens]
    lowered = [token.casefold() for token in tokens]
    if not lowered:
        return None
    if "drawing_view" in lowered or "view" in lowered:
        return "view"
    if lowered[0] == "pins":
        return "pin_map"
    if lowered[0] == "pinout" and any("label" in token or "pin" in token for token in lowered[1:]):
        return "pin_map"
    if (
        "model_orientation" in lowered
        or "rotation" in lowered
        or ("model" in lowered and "orientation" in lowered)
    ):
        return "model_orientation"
    if "pin1_corner" in lowered or "pin1" in lowered:
        return "pin1"
    if "exposed_pad" in lowered or "land_pattern" in lowered:
        return "pad_geometry"
    if lowered[0] == "package" and any(token in _CRITICAL_DIMENSIONS for token in lowered[1:]):
        return "body_pitch"
    return None


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _review_inputs(document: dict[str, Any]) -> dict[str, Any] | None:
    value = document.get("inputs")
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _current_review_status(
    library_dir: Path,
    packet_path: Path,
    document: dict[str, Any],
) -> tuple[PartSpec, tuple[str, str]] | None:
    packet_id = packet_path.parent.name
    if re.fullmatch(r"[0-9a-f]{16}", packet_id) is None:
        return None
    inputs = _review_inputs(document)
    if inputs is None:
        return None
    path_values = {
        name: inputs.get(name)
        for name in ("spec_path", "symbol_lib", "footprint_path", "library_dir")
    }
    if any(not isinstance(value, str) or not value for value in path_values.values()):
        return None
    spec_path = Path(cast(str, path_values["spec_path"]))
    symbol_lib = Path(cast(str, path_values["symbol_lib"]))
    footprint_path = Path(cast(str, path_values["footprint_path"]))
    recorded_library = Path(cast(str, path_values["library_dir"]))
    if (
        not spec_path.is_absolute()
        or not symbol_lib.is_absolute()
        or not footprint_path.is_absolute()
        or recorded_library.resolve() != library_dir.resolve()
    ):
        return None
    try:
        spec = load_part_spec(spec_path)
        density_value = inputs.get("density", "nominal")
        if density_value not in {"most", "nominal", "least"}:
            return None
        pin_source_value = inputs.get("pin_source_path")
        pin_source_path = (
            Path(pin_source_value)
            if isinstance(pin_source_value, str) and pin_source_value
            else None
        )
        pin_sources_value = inputs.get("pin_sources")
        pin_sources = (
            [
                PinSourceInput.model_validate(cast(dict[str, object], value))
                for value in cast(list[object], pin_sources_value)
                if isinstance(value, dict)
            ]
            if isinstance(pin_sources_value, list)
            else None
        )
        current_id = libreview.current_packet_id(
            spec_path,
            symbol_lib=symbol_lib,
            symbol_name=str(inputs["symbol_name"]),
            footprint_path=footprint_path,
            library_dir=library_dir,
            density=density_value,
            tolerance_mm=float(inputs.get("tolerance_mm", 0.02)),
            model_required=bool(inputs.get("model_required", True)),
            pin_source_path=pin_source_path,
            pin_sources=pin_sources,
        )
        if current_id != packet_id:
            return None
        status = libreview.review_status(
            library_dir,
            spec,
            packet_id,
            spec_path=spec_path,
        )
    except (KeyError, OSError, TypeError, ValueError):
        return None
    if status.state != "approved" or document.get("approvable") is not True:
        return None
    return spec, (spec.mpn, spec.datasheet.sha256)


def _accepted_parts(library_dir: Path) -> set[tuple[str, str]]:
    reviews = library_dir / "reviews"
    accepted: set[tuple[str, str]] = set()
    if not reviews.is_dir():
        return accepted
    for packet_path in sorted(reviews.glob("*/????????????????/review.json")):
        document = _load_json(packet_path)
        if (
            document is None
            or document.get("artifact_kind") != "circuit_library_review_packet"
            or document.get("packet_id") != packet_path.parent.name
        ):
            continue
        result = _current_review_status(library_dir, packet_path, document)
        if result is None:
            continue
        _, identity = result
        accepted.add(identity)
    return accepted


def _correction_escapes(
    library_dir: Path,
    accepted: set[tuple[str, str]],
) -> tuple[set[tuple[str, str]], list[str]]:
    journal = library_dir / "reviews" / "corrections.jsonl"
    if not journal.is_file():
        return set(), []
    escapes: set[tuple[str, str]] = set()
    findings: list[str] = []
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return set(), ["correction_records_unreadable"]
    reviews_dir = library_dir / "reviews"
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            record = CorrectionRecord.model_validate(raw)
            correction = libreview.ReviewCorrection.model_validate(
                {key: raw[key] for key in ("pointer", "old", "new", "reason", "page")}
            )
        except (json.JSONDecodeError, ValueError):
            findings.append(f"correction_record_invalid:{line_number}")
            continue
        critical_field = _critical_field(record.pointer)
        if critical_field is None or (record.mpn, record.pdf_sha256) not in accepted:
            continue
        decisions = libreview.load_decisions(library_dir, record.packet_id)
        matching_decision = next(
            (
                decision
                for decision in decisions
                if decision.valid
                and decision.integrity_valid
                and decision.decision == "reject"
                and decision.event_sha256 == record.event_sha256
                and correction in decision.corrections
            ),
            None,
        )
        if matching_decision is None:
            findings.append(f"correction_event_invalid:{record.packet_id}:{line_number}")
            continue
        packets = list(reviews_dir.glob(f"*/{record.packet_id}/review.json"))
        if len(packets) != 1:
            findings.append(f"correction_packet_unavailable:{record.packet_id}")
            continue
        packet_document = _load_json(packets[0])
        if packet_document is None or packet_document.get("packet_id") != record.packet_id:
            findings.append(f"correction_packet_invalid:{record.packet_id}")
            continue
        inputs = _review_inputs(packet_document)
        part_check = packet_document.get("fresh_part_spec_check")
        verification = packet_document.get("fresh_library_verification")
        if (
            inputs is None
            or inputs.get("pdf_sha256") != record.pdf_sha256
            or packet_document.get("approvable") is not True
            or not isinstance(part_check, dict)
            or not isinstance(verification, dict)
        ):
            continue
        part_check_data = cast(dict[str, Any], part_check)
        verification_data = cast(dict[str, Any], verification)
        if part_check_data.get("verdict") != "pass" or verification_data.get("verdict") != "pass":
            continue
        escapes.add((record.mpn, record.pdf_sha256))
    return escapes, findings


def compute_metrics(project: Path) -> LibraryMetrics:
    """Compute metrics for a project root containing the PR-B review journal."""
    project = project.expanduser().resolve()
    library_dir = project / "library"
    findings: list[str] = []
    manifest_path = _manifest_path(project)
    manifest_hash: str | None = None
    try:
        manifest_hash = _hash(manifest_path)
        corpus.load_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        findings.append(f"corpus_manifest_unavailable:{exc}")

    report_path = _mutation_report_path(project)
    report_hash: str | None = None
    export_oracle_run: bool | None = None
    family_rates: dict[mutation.CheckFamily, float] = {}
    operator_outcomes: list[mutation.MutationOutcome] = []
    critical_single_oracle: list[str] = []
    try:
        raw_report = report_path.read_bytes()
        report_hash = hashlib.sha256(raw_report).hexdigest()
        report = mutation.MutationReport.model_validate_json(raw_report)
        export_oracle_run = report.export_oracle_run
        family_rates = report.family_detection_rates
        operator_outcomes = report.outcomes
        critical_single_oracle = [
            outcome.mutation.operator
            for outcome in report.outcomes
            if outcome.status == "single_oracle" and outcome.mutation.critical
        ]
    except (OSError, ValueError) as exc:
        findings.append(f"mutation_report_unavailable:{exc}")
    if export_oracle_run is False:
        findings.append("mutation_export_oracle_not_run")

    accepted = _accepted_parts(library_dir)
    correction_escapes, correction_findings = _correction_escapes(
        library_dir,
        accepted,
    )
    findings.extend(correction_findings)
    n = len(accepted)
    k = len(correction_escapes.intersection(accepted))
    upper95 = clopper_pearson_upper95(n, k)
    if n < 299:
        findings.append("accepted_part_sample_below_299")
    if upper95 >= 0.01:
        findings.append("escape_rate_upper95_not_below_0_01")
    if critical_single_oracle:
        findings.append("critical_single_oracle_mutation")
    supported = (
        n >= 299
        and upper95 < 0.01
        and not critical_single_oracle
        and manifest_hash is not None
        and report_hash is not None
        and export_oracle_run is True
        and not findings
    )
    return LibraryMetrics(
        accepted_parts=n,
        escapes=k,
        upper95=upper95,
        corpus_manifest_sha256=manifest_hash,
        mutation_report_sha256=report_hash,
        family_detection_rates=family_rates,
        operator_outcomes=operator_outcomes,
        critical_single_oracle=critical_single_oracle,
        release_relaxation_supported=supported,
        findings=list(dict.fromkeys(findings)),
    )


def require_relaxation_supported(
    project: Path,
    *,
    metrics_path: Path | None = None,
) -> LibraryMetrics:
    """Validate that a stored metrics snapshot still binds current source reports."""
    project = project.expanduser().resolve()
    library_dir = project / "library"
    path = metrics_path or library_dir / "library-metrics.json"
    try:
        metrics = LibraryMetrics.model_validate_json(path.read_bytes())
        current_manifest_hash = _hash(_manifest_path(project))
        current_report_hash = _hash(_mutation_report_path(project))
        computed = compute_metrics(project)
    except (OSError, ValueError) as exc:
        raise LibraryMetricsError("review_relaxation_not_supported_by_metrics") from exc
    if (
        metrics != computed
        or metrics.corpus_manifest_sha256 != current_manifest_hash
        or metrics.mutation_report_sha256 != current_report_hash
        or not metrics.release_relaxation_supported
        or metrics.accepted_parts < 299
        or metrics.upper95 >= 0.01
        or metrics.critical_single_oracle
    ):
        raise LibraryMetricsError("review_relaxation_not_supported_by_metrics")
    return metrics
