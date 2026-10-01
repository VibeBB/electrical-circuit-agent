"""Hash-bound human review packets and approval decisions for library parts."""

from __future__ import annotations

import copy
import hashlib
import html
import json
import math
import os
import re
import tempfile
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, cast

from PIL import Image
from pydantic import BaseModel, ConfigDict

from . import datasheet, kicad_cli
from .datasheet import DatasheetExtraction, PageExtraction
from .landpattern import Density, LandPatternResult, compute_land_pattern, lead_rects
from .libitems import FootprintDef, PadDef, SymbolDef, parse_footprint, parse_symbol
from .libverify import LibraryVerification, VerifiedModel, VerifyFinding, verify_library_part
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


class ReviewCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pointer: str
    old: Any
    new: Any
    reason: str
    page: int


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packet_id: str
    decision: Literal["approve", "reject"] | None = None
    reviewer: str | None = None
    answers: dict[str, str]
    corrections: list[ReviewCorrection]
    event_path: Path | None = None
    event_sha256: str | None = None
    event_mtime_ns: int = 0
    event_name: str = ""
    valid: bool
    reasons: list[str]


class ReviewStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_library_review_status"]
    packet_id: str
    state: Literal["approved", "rejected", "pending", "invalid"]
    reasons: list[str]
    decisions: list[ReviewDecision]


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
) -> str:
    value = {
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
    }
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    return values


def blind_questions(spec: PartSpec, packet_id: str) -> list[BlindQuestion]:
    if not PACKET_ID_RE.fullmatch(packet_id):
        raise ValueError("packet_id must be 16 lowercase hexadecimal characters")
    pin_reading = spec.package.pin1_reading
    pin_table_page = spec.pin_table.page
    questions = [
        BlindQuestion(
            question_id="drawing_id",
            prompt="What drawing identifier is printed on this package drawing?",
            page=pin_reading.page,
            bbox=pin_reading.bbox,
            expected=spec.package.drawing_id,
        ),
        BlindQuestion(
            question_id="drawing_view",
            prompt="Is this package drawing a top or bottom view?",
            page=pin_reading.page,
            bbox=pin_reading.bbox,
            expected=spec.package.drawing_view,
        ),
        BlindQuestion(
            question_id="pin1_corner",
            prompt="Which corner is marked as pin 1 in the top view?",
            page=pin_reading.page,
            bbox=pin_reading.bbox,
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
                spec.package.exposed_pad.length.reading.page
                if spec.package.exposed_pad is not None
                else pin_table_page
            ),
            bbox=(
                spec.package.exposed_pad.length.reading.bbox
                if spec.package.exposed_pad is not None
                else None
            ),
            expected="yes" if spec.package.exposed_pad is not None else "no",
        ),
    ]
    pins = list(spec.pins)

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
        questions.append(
            BlindQuestion(
                question_id=f"pin.{number}",
                prompt=f"What signal name is printed for pin {number}?",
                page=pin.reading.page,
                bbox=pin.reading.bbox,
                expected=pin.name,
            )
        )
    return questions


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
        parsed.valid and parsed.decision == "reject" and parsed.corrections == decision.corrections
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
        corrections=corrections,
        event_path=event_path,
        event_sha256=event_sha256,
        event_mtime_ns=event_path.stat().st_mtime_ns,
        event_name=event_path.name,
        valid=not reasons,
        reasons=reasons,
    )


def _invalid_decision(packet_id: str, name: str, reason: str) -> ReviewDecision:
    return ReviewDecision(
        packet_id=packet_id,
        answers={},
        corrections=[],
        event_name=name,
        valid=False,
        reasons=[reason],
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


def review_status(library_dir: Path, spec: PartSpec, packet_id: str) -> ReviewStatus:
    questions = blind_questions(spec, packet_id)
    decisions = load_decisions(library_dir, packet_id)
    expected = {question.question_id: question.expected for question in questions}
    valid_approvals: list[ReviewDecision] = []
    valid_rejections: list[ReviewDecision] = []
    status_reasons: list[str] = []
    for decision in decisions:
        if not decision.valid:
            status_reasons.extend(decision.reasons)
            continue
        if decision.decision == "reject":
            valid_rejections.append(decision)
        elif decision.decision == "approve":
            if decision.corrections:
                status_reasons.append("approval_must_not_include_corrections")
                continue
            if set(decision.answers) != set(expected):
                status_reasons.append("human_review_blind_mismatch")
                continue
            if any(
                _normalise_answer(question_id, decision.answers[question_id])
                != _normalise_answer(question_id, expected[question_id])
                for question_id in expected
            ):
                status_reasons.append("human_review_blind_mismatch")
                continue
            valid_approvals.append(decision)

    def order(item: ReviewDecision) -> tuple[int, str]:
        return item.event_mtime_ns, item.event_name

    latest_rejection = max(valid_rejections, key=order, default=None)
    eligible_approvals = [
        item
        for item in valid_approvals
        if latest_rejection is None or order(item) > order(latest_rejection)
    ]
    if eligible_approvals and not any(not item.valid for item in decisions):
        return ReviewStatus(
            artifact_kind="circuit_library_review_status",
            packet_id=packet_id,
            state="approved",
            reasons=[],
            decisions=decisions,
        )
    if latest_rejection is not None and not any(not item.valid for item in decisions):
        return ReviewStatus(
            artifact_kind="circuit_library_review_status",
            packet_id=packet_id,
            state="rejected",
            reasons=["human_review_rejected"],
            decisions=decisions,
        )
    if status_reasons:
        if any(reason == "human_review_blind_mismatch" for reason in status_reasons):
            status_reasons = ["human_review_blind_mismatch"]
        return ReviewStatus(
            artifact_kind="circuit_library_review_status",
            packet_id=packet_id,
            state="invalid",
            reasons=list(dict.fromkeys(status_reasons)),
            decisions=decisions,
        )
    return ReviewStatus(
        artifact_kind="circuit_library_review_status",
        packet_id=packet_id,
        state="pending",
        reasons=["human_review_missing"],
        decisions=decisions,
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


def apply_corrections(spec_path: Path, decision: ReviewDecision) -> CorrectionResult:
    if not _validated_reject(decision):
        return CorrectionResult(
            artifact_kind="circuit_library_review_correction",
            applied=False,
            packet_id=decision.packet_id,
            applied_pointers=[],
            reasons=["correction_requires_valid_reject"],
        )
    if not decision.corrections:
        return CorrectionResult(
            artifact_kind="circuit_library_review_correction",
            applied=False,
            packet_id=decision.packet_id,
            applied_pointers=[],
            reasons=["no_corrections"],
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
    library_dir = spec_path.resolve().parent / "library"
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
    for correction in decision.corrections:
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
    for correction in decision.corrections:
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
        applied_pointers=[item.pointer for item in decision.corrections],
        reasons=[],
    )


def correction_regressions(library_dir: Path, spec: PartSpec) -> list[ReviewFinding]:
    corpus_path = library_dir / "reviews" / "corrections.jsonl"
    if not corpus_path.exists():
        return []
    try:
        records: list[Any] = [
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
    return findings


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
) -> _CropRecord:
    with Image.open(source_path) as opened:
        image: Image.Image = opened.convert("RGB")
    pixels_per_point = page.dpi / 72.0
    expanded = (
        max(0.0, bbox[0] - 12.0),
        max(0.0, bbox[1] - 12.0),
        min(page.width_pt, bbox[2] + 12.0),
        min(page.height_pt, bbox[3] + 12.0),
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
    return results


def _dimension_records(spec: PartSpec, crops: dict[str, _CropRecord]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for field, reading, dimension in _readings(spec):
        if dimension is None:
            continue
        crop = crops.get(field)
        records.append(
            {
                "field": field,
                "label": dimension.label or field,
                "kind": dimension.kind,
                "min": dimension.min,
                "nom": dimension.nom,
                "max": dimension.max,
                "page": reading.page,
                "crop_path": crop.path if crop is not None else None,
                "crop_sha256": crop.sha256 if crop is not None else None,
            }
        )
    return records


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
) -> list[dict[str, str | int | None]]:
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
    output: list[dict[str, str | int | None]] = []
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
        expanded_numbers = numbers if numbers else [number.strip()]
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
                }
            )
    return output


def _augment_pin_rows(
    rows: list[dict[str, str | int | None]],
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
    return rows


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


def _overlay_svg(
    spec: PartSpec,
    footprint: FootprintDef,
    reference: LandPatternResult,
    output_path: Path,
    *,
    scale_px_per_mm: float,
) -> None:
    all_x: list[float] = []
    all_y: list[float] = []

    def include(rect: tuple[float, float, float, float]) -> None:
        all_x.extend((rect[0], rect[2]))
        all_y.extend((rect[1], rect[3]))

    body_length = _format_dim(spec.package.body_length)
    body_width = _format_dim(spec.package.body_width)
    include((-body_width / 2, -body_length / 2, body_width / 2, body_length / 2))
    for pad in footprint.pads:
        points = _pad_corners(pad)
        include(
            (
                min(point[0] for point in points),
                min(point[1] for point in points),
                max(point[0] for point in points),
                max(point[1] for point in points),
            )
        )
    for pad in reference.pads:
        include(
            (
                pad.x - pad.width / 2,
                pad.y - pad.height / 2,
                pad.x + pad.width / 2,
                pad.y + pad.height / 2,
            )
        )
    leads = lead_rects(spec)
    for rectangles in leads.values():
        for rect in rectangles:
            include((rect.x0, rect.y0, rect.x1, rect.y1))
    minimum_x = min(all_x) - 1.0
    minimum_y = min(all_y) - 1.0
    maximum_x = max(all_x) + 1.0
    maximum_y = max(all_y) + 1.0
    width_mm, height_mm = maximum_x - minimum_x, maximum_y - minimum_y
    esc = html.escape
    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width_mm * scale_px_per_mm:.2f}px" '
            f'height="{height_mm * scale_px_per_mm:.2f}px" '
            f'viewBox="{minimum_x:.5f} {minimum_y:.5f} {width_mm:.5f} {height_mm:.5f}">'
        ),
        '<defs><pattern id="hatch" width="0.12" height="0.12" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="0.12" '
        'stroke="#aa5500" stroke-width="0.035"/></pattern></defs>',
        '<rect width="100%" height="100%" fill="white"/>',
        (
            f'<rect x="{-body_width / 2:.5f}" y="{-body_length / 2:.5f}" '
            f'width="{body_width:.5f}" height="{body_length:.5f}" fill="none" '
            'stroke="#333" stroke-width="0.04"/>'
        ),
    ]
    for pad in reference.pads:
        parts.append(
            f'<rect x="{pad.x - pad.width / 2:.5f}" y="{pad.y - pad.height / 2:.5f}" '
            f'width="{pad.width:.5f}" height="{pad.height:.5f}" fill="none" '
            'stroke="#1769aa" stroke-width="0.04" stroke-dasharray="0.12 0.08"/>'
        )
    for pad in footprint.pads:
        corners = _pad_corners(pad)
        points = " ".join(f"{x:.5f},{y:.5f}" for x, y in corners)
        parts.append(
            f'<polygon points="{points}" fill="#d4e8f4" fill-opacity="0.75" '
            'stroke="#114477" stroke-width="0.035"/>'
        )
        parts.append(
            f'<text x="{pad.x:.5f}" y="{pad.y:.5f}" font-size="0.18" '
            f'text-anchor="middle" dominant-baseline="central">{esc(pad.number)}</text>'
        )
    for rectangles in leads.values():
        for rect in rectangles:
            parts.append(
                f'<rect x="{rect.x0:.5f}" y="{rect.y0:.5f}" '
                f'width="{rect.x1 - rect.x0:.5f}" height="{rect.y1 - rect.y0:.5f}" '
                'fill="url(#hatch)" stroke="#aa5500" stroke-width="0.025"/>'
            )
    pin_one = next((pad for pad in reference.pads if pad.number == "1"), None)
    if pin_one is not None:
        marker_x, marker_y = pin_one.x, pin_one.y
        parts.append(
            f'<circle cx="{marker_x:.5f}" cy="{marker_y:.5f}" r="0.12" '
            'fill="none" stroke="#cc2222" stroke-width="0.04"/>'
        )
    bar_x, bar_y = minimum_x + 0.2, maximum_y - 0.25
    parts.extend(
        [
            f'<line x1="{bar_x:.5f}" y1="{bar_y:.5f}" x2="{bar_x + 1:.5f}" '
            f'y2="{bar_y:.5f}" stroke="#111" stroke-width="0.05"/>',
            f'<text x="{bar_x:.5f}" y="{bar_y - 0.08:.5f}" font-size="0.16">1 mm</text>',
            (
                f'<text x="{minimum_x + 0.2:.5f}" y="{minimum_y + 0.28:.5f}" '
                'font-size="0.18">Top-view placement · drawing view '
                f"{esc(spec.package.drawing_view)}</text>"
            ),
            "</svg>",
        ]
    )
    output_path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def _relative_path(base: Path, path: Path) -> str:
    return path.resolve().relative_to(base.resolve()).as_posix()


def _page_path(extraction_dir: Path, page: PageExtraction) -> Path:
    return extraction_dir / page.png_path


def _question_crop_field(question_id: str, spec: PartSpec) -> str:
    if question_id in {"drawing_id", "drawing_view", "pin1_corner"}:
        return "package.pin1_reading"
    if question_id == "pin_count":
        return "pin_table"
    if question_id == "exposed_pad" and spec.package.exposed_pad is not None:
        return "package.exposed_pad.length"
    if question_id.startswith("pin."):
        number = question_id.removeprefix("pin.")
        return f"pins.{number}.reading"
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
        crop_field = _question_crop_field(question.question_id, spec)
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
        f"</head><body><h1>Blind review</h1><p>Packet {html.escape(packet_id)}</p>"
        + "".join(sections)
        + "</body></html>\n"
    )


def _review_html(review: dict[str, Any]) -> str:
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
    pin_rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(row.get(key) or ''))}</td>"
            for key in (
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
            )
        )
        + "</tr>"
        for row in review["pin_comparisons"]
    )
    dimension_rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(row.get(key) if row.get(key) is not None else ''))}</td>"
            for key in (
                "field",
                "label",
                "kind",
                "min",
                "nom",
                "max",
                "page",
                "crop_path",
                "crop_sha256",
            )
        )
        + "</tr>"
        for row in review["dimensions"]
    )
    render_items = (
        "".join(
            f'<li><a href="{html.escape(item["path"])}">{html.escape(item["kind"])}</a> '
            f"SHA-256 {html.escape(item['sha256'])}</li>"
            for item in review["renders"]
        )
        or "<li>No render artifacts available.</li>"
    )
    hashes_html = "".join(
        f"<li><code>{html.escape(str(name))}</code>: <code>{html.escape(str(value))}</code></li>"
        for name, value in sorted(review["artifact_hashes"].items())
    )
    overlay = review.get("overlay")
    land_crop = review.get("land_pattern_crop")
    overlay_html = (
        f'<div class="pair"><figure><img src="{html.escape(land_crop["path"])}">'
        f"<figcaption>Land-pattern page crop</figcaption></figure><figure>"
        f'<img src="{html.escape(overlay["path"])}"><figcaption>Same-scale placement overlay'
        "</figcaption></figure></div>"
        if overlay is not None and land_crop is not None
        else "<p>Land-pattern overlay or cited page crop is unavailable.</p>"
    )
    message = html.escape(review["message_template"])
    unknowns = "".join(f"<li>{html.escape(item)}</li>" for item in review["unknowns"])
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Library review</title>'
        "<style>body{font:15px sans-serif;max-width:1200px;margin:2rem auto}"
        "table{border-collapse:collapse;"
        "width:100%;font-size:13px}td,th{border:1px solid #bbb;padding:.3rem;text-align:left}"
        "img{max-width:100%;height:auto}.pair{display:flex;align-items:flex-start;gap:1rem}"
        ".pair figure{margin:0}.pair img{display:block;max-width:none}</style></head><body>"
        f"<h1>Review packet {html.escape(review['packet_id'])}</h1>"
        "<h2>Contradictions and deterministic findings</h2><ul>"
        + findings_html
        + "</ul><h2>Pin comparisons</h2><table><thead><tr>"
        "<th>Pin</th><th>PDFPlumber number</th><th>Poppler number</th>"
        "<th>PDFPlumber name</th><th>Poppler name</th><th>PartSpec name</th>"
        "<th>PartSpec type</th>"
        "<th>Symbol number</th><th>Symbol name</th><th>Symbol type</th><th>Footprint pad</th>"
        "</tr></thead><tbody>" + pin_rows + "</tbody></table><h2>Dimensions</h2><table><thead><tr>"
        "<th>Field</th><th>Label</th><th>Kind</th><th>Min</th><th>Nom</th><th>Max</th>"
        "<th>Page</th><th>Crop</th><th>Crop SHA-256</th></tr></thead><tbody>"
        + dimension_rows
        + "</tbody></table><h2>KiCad renders</h2><ul>"
        + render_items
        + "</ul><h2>Placement overlay</h2>"
        + overlay_html
        + "<h2>Artifact hashes</h2><ul>"
        + hashes_html
        + "</ul><h2>Unknowns</h2><ul>"
        + unknowns
        + "</ul><h2>Decision message template</h2><pre>"
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
    out_dir: Path,
) -> ReviewPacket:
    """Build fresh deterministic and human-review evidence for a library part."""
    spec = load_part_spec(spec_path)
    spec_dir = spec_path.resolve().parent
    pdf_path = _relative_or_absolute(spec_dir, spec.datasheet.path)
    extraction_path = _relative_or_absolute(spec_dir, spec.datasheet.extraction_path)
    try:
        stored_extraction = datasheet.load_extraction(extraction_path)
        dpi = stored_extraction.pages[0].dpi if stored_extraction.pages else 300
    except (OSError, ValueError, datasheet.DatasheetError):
        dpi = 300
    cited_pages = sorted(
        {reading.page for _, reading, _ in _readings(spec)} | {spec.pin_table.page}
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
    }
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
    )
    packet_dir = out_dir / _safe_field(spec.mpn) / current_id
    packet_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir = packet_dir / "evidence"
    crop_dir = packet_dir / "crops"
    findings: list[ReviewFinding] = []
    unknowns: list[str] = []
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
        reference = compute_land_pattern(spec, density)
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

    symbol_def: SymbolDef | None = None
    with suppress(OSError, ValueError):
        symbol_def = parse_symbol(symbol_lib, symbol_name)
    pin_rows = _pin_table_rows(spec, extraction, evidence_dir) if extraction is not None else []
    pin_rows = _augment_pin_rows(pin_rows, symbol_def, footprint_def)

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
    unknowns.append("3D visual review not included yet")

    overlay_record: dict[str, Any] | None = None
    land_crop_field = next(
        (
            field
            for name in (spec.land_pattern.dimensions if spec.land_pattern is not None else {})
            if (field := f"land_pattern.dimensions.{name}") in crops
        ),
        None,
    )
    land_pattern_crop = crops.get(land_crop_field) if land_crop_field is not None else None
    if reference is not None and footprint_def is not None:
        try:
            source_page = (
                page_by_number.get(land_pattern_crop.page)
                if land_pattern_crop is not None
                else None
            )
            pixel_scale = (
                source_page.dpi / 25.4 * land_pattern_crop.scale
                if source_page is not None and land_pattern_crop is not None
                else 30.0
            )
            overlay_path = packet_dir / "overlay.svg"
            _overlay_svg(
                spec,
                footprint_def,
                reference,
                overlay_path,
                scale_px_per_mm=pixel_scale,
            )
            overlay_record = {
                "path": _relative_path(packet_dir, overlay_path),
                "sha256": _sha256(overlay_path),
                "scale_px_per_mm": pixel_scale,
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

    questions = blind_questions(spec, current_id)
    dimensions = _dimension_records(spec, crops)
    extracted_pages: list[dict[str, Any]] = (
        [
            {
                "page": page.page,
                "path": _relative_path(packet_dir, _page_path(evidence_dir, page)),
                "sha256": page.png_sha256,
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
    artifact_hashes.update({f"render:{item['kind']}": item["sha256"] for item in renders})
    if overlay_record is not None:
        artifact_hashes["overlay"] = str(overlay_record["sha256"])

    severity_order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda item: (severity_order[item.severity], item.code, item.field))
    approvable = (
        part_check.verdict == "pass"
        and verification is not None
        and verification.verdict == "pass"
        and not any(item.code == "correction_regressed" for item in findings)
    )
    message_template = _message_template(current_id, questions)
    review_document: dict[str, Any] = {
        "artifact_kind": "circuit_library_review_packet",
        "packet_id": current_id,
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
            }
            for item in questions
        ],
        "pin_comparisons": pin_rows,
        "dimensions": dimensions,
        "evidence_pages": extracted_pages,
        "crops": [item.model_dump(mode="json") for item in crops.values()],
        "renders": renders,
        "models": [item.model_dump(mode="json") for item in model_records],
        "overlay": overlay_record,
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
    )
