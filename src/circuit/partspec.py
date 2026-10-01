"""Agent-authored part specifications and deterministic datasheet checks."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import advisory, intake
from .datasheet import DatasheetExtraction, PageExtraction, PdfWord, page_tables, page_words

_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)")
_SYMBOL_PREFIX = re.compile(r"^([□⌀ØR])\s*")
_TOKEN_SPLIT = re.compile(r"[\s,]+")
_PIN1_CORNER = re.compile(r"\b(?P<corner>(?:top|bottom)[\s_-]+(?:left|right))\b", re.IGNORECASE)


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float] | None = None
    mechanical: str | None = None
    vision: str = Field(min_length=1)
    vision_record: str = Field(min_length=1)
    user_confirmed: str | None = Field(default=None, pattern=r"^R[0-9]+$")


class Dimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: float | None = None
    nom: float | None = None
    max: float | None = None
    reference: bool = False
    reading: Reading

    @model_validator(mode="after")
    def validate_values(self) -> Dimension:
        if self.min is None and self.nom is None and self.max is None:
            raise ValueError("at least one of min, nom, or max is required")
        values = [value for value in (self.min, self.nom, self.max) if value is not None]
        if values != sorted(values):
            raise ValueError("dimension values must satisfy min <= nom <= max")
        return self


class ExposedPad(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    length: Dimension
    width: Dimension


class PackageSpec(BaseModel):
    """Package dimensions use the KiCad top view: pin 1 at top-left, +x right, +y down.

    `body_length` is the Y extent along the pin-1 row of dual packages and
    along the left/right sides of quad packages; `body_width` is the X extent.
    """

    model_config = ConfigDict(extra="forbid")

    family: Literal[
        "no_lead_quad",
        "no_lead_dual",
        "gullwing_quad",
        "gullwing_dual",
        "chip",
        "through_hole_inline",
        "custom",
    ]
    code: str
    pin_count: int = Field(gt=0)
    pitch: Dimension | None = None
    body_length: Dimension
    body_width: Dimension
    height: Dimension
    standoff: Dimension | None = None
    lead_span: Dimension | None = None
    lead_length: Dimension | None = None
    lead_width: Dimension | None = None
    exposed_pad: ExposedPad | None = None
    pins_per_side: tuple[int, int, int, int] | None = None
    pin1_corner: Literal["top_left", "top_right", "bottom_left", "bottom_right"]
    pin1_reading: Reading

    @model_validator(mode="after")
    def validate_pins_per_side_family(self) -> PackageSpec:
        if self.pins_per_side is not None and self.family not in (
            "no_lead_quad",
            "gullwing_quad",
        ):
            raise ValueError("pins_per_side is only valid for quad package families")
        return self


class LandPad(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    x: float
    y: float
    width: float
    height: float
    shape: Literal["rect", "roundrect", "oval", "circle"]


class LandPattern(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["datasheet", "ipc7351b"]
    dimensions: dict[str, Dimension] = Field(default_factory=dict)
    pads: list[LandPad]

    @model_validator(mode="after")
    def validate_dimensions(self) -> LandPattern:
        if self.source == "datasheet" and not self.dimensions:
            raise ValueError("datasheet land patterns require at least one dimension")
        return self


class PinSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    name: str
    electrical_type: Literal[
        "input",
        "output",
        "bidirectional",
        "tri_state",
        "passive",
        "free",
        "unspecified",
        "power_in",
        "power_out",
        "open_collector",
        "open_emitter",
        "no_connect",
    ]
    reading: Reading


class DatasheetRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(min_length=1)
    extraction_path: str


class PartSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_part_spec"]
    mpn: str
    manufacturer: str
    datasheet: DatasheetRef
    intake_path: str | None = None
    package: PackageSpec
    land_pattern: LandPattern | None = None
    pins: list[PinSpec] = Field(min_length=1)


class ParsedDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: float | None = None
    nom: float | None = None
    max: float | None = None
    reference: bool = False
    count: int | None = None
    symbols: list[str]
    numbers: list[str]


class SpecFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["error", "warning", "info"]
    field: str
    message: str
    page: int | None = None


class PartSpecReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_kind: Literal["circuit_part_spec_check"]
    verdict: Literal["pass", "fail"]
    part_spec_sha256: str
    extraction_sha256: str
    pdf_sha256: str
    checked_readings: int
    findings: list[SpecFinding]


def _decimal_string(value: str) -> str:
    number = Decimal(value)
    if number == 0:
        return "0"
    normalized = format(number.normalize(), "f")
    return "0" + normalized if normalized.startswith(".") else normalized


def _normalise_text(text: str) -> str:
    minus_map = {"\u2212": "-", "\u2013": "-", "\u2014": "-"}
    return re.sub(r"\s+", " ", text.translate(str.maketrans(minus_map))).strip()


def parse_dimension_text(text: str) -> ParsedDimension:
    """Parse common datasheet dimension notation into numeric bounds."""
    normalized = _normalise_text(text)
    if not normalized:
        raise ValueError("dimension text is empty")
    numbers = [_decimal_string(match.group()) for match in _NUMBER.finditer(normalized)]
    if not numbers:
        raise ValueError(f"dimension text contains no number: {text!r}")
    remaining = normalized
    count: int | None = None
    count_match = re.match(r"^(\d+)\s*[xX](?:\s+|(?=[(□⌀ØR\d]))", remaining)
    if count_match is not None:
        count = int(count_match.group(1))
        remaining = remaining[count_match.end() :].strip()
    symbols: list[str] = []
    while symbol_match := _SYMBOL_PREFIX.match(remaining):
        symbols.append(symbol_match.group(1))
        remaining = remaining[symbol_match.end() :].strip()

    value_min: float | None = None
    value_nom: float | None = None
    value_max: float | None = None
    reference = False
    literal = rf"{_NUMBER.pattern}"
    if match := re.fullmatch(rf"\(\s*({literal})\s*\)", remaining):
        value_nom = float(match.group(1))
        reference = True
    elif match := re.fullmatch(rf"({literal})\s*±\s*({literal})", remaining):
        value_nom = float(match.group(1))
        tolerance = float(match.group(2))
        value_min = round(value_nom - tolerance, 6)
        value_max = round(value_nom + tolerance, 6)
    elif match := re.fullmatch(rf"({literal})\s+(MAX|MIN)", remaining, re.IGNORECASE):
        bound = float(match.group(1))
        if match.group(2).upper() == "MAX":
            value_max = bound
        else:
            value_min = bound
    elif match := re.fullmatch(rf"({literal})\s*-\s*({literal})", remaining):
        first, second = float(match.group(1)), float(match.group(2))
        value_min, value_max = min(first, second), max(first, second)
    elif re.fullmatch(rf"{literal}(?:\s+{literal}){{0,2}}", remaining):
        values = [float(item) for item in _NUMBER.findall(remaining)]
        if len(values) == 1:
            value_nom = values[0]
        elif len(values) == 2:
            value_min, value_max = min(values), max(values)
        elif len(values) == 3:
            value_min, value_nom, value_max = sorted(values)
    else:
        raise ValueError(f"could not parse dimension text: {text!r}")
    return ParsedDimension(
        min=value_min,
        nom=value_nom,
        max=value_max,
        reference=reference,
        count=count,
        symbols=symbols,
        numbers=numbers,
    )


def load_part_spec(path: Path) -> PartSpec:
    try:
        return PartSpec.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"could not load part spec {path}: {exc}") from exc


def part_spec_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dimensions(spec: PartSpec) -> list[tuple[str, Dimension]]:
    found: list[tuple[str, Dimension]] = []
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
            found.append((f"package.{name}", dimension))
    if package.exposed_pad is not None:
        found.extend(
            [
                ("package.exposed_pad.length", package.exposed_pad.length),
                ("package.exposed_pad.width", package.exposed_pad.width),
            ]
        )
    if spec.land_pattern is not None:
        found.extend(
            (f"land_pattern.dimensions.{name}", dimension)
            for name, dimension in spec.land_pattern.dimensions.items()
        )
    return found


def _all_readings(spec: PartSpec) -> list[tuple[str, Reading, Dimension | None]]:
    readings: list[tuple[str, Reading, Dimension | None]] = [
        (field, dimension.reading, dimension) for field, dimension in _dimensions(spec)
    ]
    readings.append(("package.pin1_reading", spec.package.pin1_reading, None))
    readings.extend((f"pins[{index}]", pin.reading, None) for index, pin in enumerate(spec.pins))
    return readings


def _resolved(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def _user_requirements(spec: PartSpec, spec_dir: Path) -> set[str]:
    if spec.intake_path is None or not any(
        reading.user_confirmed is not None for _, reading, _ in _all_readings(spec)
    ):
        return set()
    try:
        intake_record = intake.load_intake(_resolved(spec_dir, spec.intake_path))
    except (OSError, ValueError):
        return set()
    return {
        requirement.id for requirement in intake_record.requirements if requirement.source == "user"
    }


def _read_page_words(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(value, list):
        return False
    try:
        for item in cast(list[object], value):
            PdfWord.model_validate(item)
    except ValueError:
        return False
    return True


def _page_artifact_findings(page: PageExtraction, extraction_dir: Path) -> list[SpecFinding]:
    findings: list[SpecFinding] = []
    png_path = _resolved(extraction_dir, page.png_path)
    try:
        png_sha256 = hashlib.sha256(png_path.read_bytes()).hexdigest()
    except OSError:
        findings.append(
            SpecFinding(
                code="evidence_missing",
                severity="error",
                field=f"pages[{page.page}].png_path",
                message=f"page image is missing: {png_path}",
                page=page.page,
            )
        )
    else:
        if png_sha256 != page.png_sha256:
            findings.append(
                SpecFinding(
                    code="evidence_sha_mismatch",
                    severity="error",
                    field=f"pages[{page.page}].png_path",
                    message="page image SHA-256 differs from the extraction manifest",
                    page=page.page,
                )
            )
    for lane in page.lanes:
        if lane.status != "ok":
            continue
        lane_path = (
            _resolved(extraction_dir, lane.words_path) if lane.words_path is not None else None
        )
        if lane_path is None or not _read_page_words(lane_path):
            findings.append(
                SpecFinding(
                    code="evidence_missing",
                    severity="error",
                    field=f"pages[{page.page}].lanes.{lane.lane}.words_path",
                    message="successful lane word artifact is missing or invalid",
                    page=page.page,
                )
            )
    return findings


def _observation_log(spec_dir: Path) -> Path | None:
    configured = os.environ.get("CIRCUIT_IMAGE_OBSERVATIONS")
    if configured:
        path = Path(configured)
        return path if path.is_file() else None
    for directory in (spec_dir, *spec_dir.parents):
        path = directory / "observations" / "circuit" / "image-observations.jsonl"
        if path.is_file():
            return path
    return None


def _observed_image_hashes(path: Path) -> set[str]:
    hashes: set[str] = set()
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    digest = cast(dict[str, object], value).get("image_sha256")
                    if isinstance(digest, str):
                        hashes.add(digest)
    except (OSError, UnicodeDecodeError):
        return set()
    return hashes


def _pin1_corner_findings(
    package: PackageSpec,
    reading: Reading,
    field: str,
    findings: list[SpecFinding],
) -> None:
    matches = list(_PIN1_CORNER.finditer(reading.vision))
    if len(matches) != 1:
        findings.append(
            SpecFinding(
                code="pin1_unparseable",
                severity="error",
                field=field,
                message="pin-1 vision reading must contain exactly one supported corner phrase",
                page=reading.page,
            )
        )
        return
    corner = re.sub(r"[\s-]+", "_", matches[0].group("corner").lower())
    if corner != package.pin1_corner:
        findings.append(
            SpecFinding(
                code="pin1_mismatch",
                severity="error",
                field=field,
                message=f"pin-1 vision corner {corner} differs from {package.pin1_corner}",
                page=reading.page,
            )
        )


def _read_vision(
    reading: Reading,
    spec_dir: Path,
    page: PageExtraction | None,
    field: str,
    findings: list[SpecFinding],
) -> None:
    record_path = _resolved(spec_dir, reading.vision_record)
    try:
        record = advisory.AdvisoryResult.model_validate(
            json.loads(record_path.read_text(encoding="utf-8"))
        )
        detail = advisory.parse_visual_review(record)
    except (OSError, json.JSONDecodeError, ValueError):
        detail = None
    if detail is None:
        findings.append(
            SpecFinding(
                code="vision_record_missing",
                severity="error",
                field=field,
                message=f"vision record is missing or invalid: {record_path}",
                page=reading.page,
            )
        )
        return
    if detail.checklist != "datasheet" or page is None or detail.image_sha256 != page.png_sha256:
        findings.append(
            SpecFinding(
                code="vision_record_mismatch",
                severity="error",
                field=field,
                message="vision record must review this page image with the datasheet checklist",
                page=reading.page,
            )
        )


def _inside_bbox(word_x: float, word_y: float, bbox: tuple[float, float, float, float]) -> bool:
    x0, top, x1, bottom = bbox
    return x0 - 2 <= word_x <= x1 + 2 and top - 2 <= word_y <= bottom + 2


def _mechanical_words(
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page_number: int,
    reading: Reading,
) -> list[str]:
    words = page_words(extraction, extraction_dir, page_number)
    if reading.bbox is not None:
        words = [
            word
            for word in words
            if _inside_bbox(
                (word.x0 + word.x1) / 2,
                (word.top + word.bottom) / 2,
                reading.bbox,
            )
        ]
    return [word.text for word in words]


def _tokens(text: str) -> list[str]:
    return [token for token in _TOKEN_SPLIT.split(text.strip()) if token]


def _numeric_tokens(text: str) -> list[str]:
    return [_decimal_string(match.group()) for match in _NUMBER.finditer(text)]


def _number_close(actual: float | None, expected: float | None) -> bool:
    if actual is None or expected is None:
        return actual is expected
    return math.isclose(actual, expected, rel_tol=0, abs_tol=1e-9)


def _dimension_checks(
    spec_dimension: Dimension,
    reading: Reading,
    field: str,
    page: PageExtraction | None,
    spec_dir: Path,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    confirmation_verified: bool,
    findings: list[SpecFinding],
) -> None:
    _read_vision(reading, spec_dir, page, field, findings)
    if page is not None and page.drawing_page and reading.bbox is None:
        findings.append(
            SpecFinding(
                code="bbox_missing",
                severity="warning",
                field=field,
                message="dimension reading on a drawing page has no bounded region",
                page=reading.page,
            )
        )
    try:
        parsed = parse_dimension_text(reading.vision)
    except ValueError as exc:
        findings.append(
            SpecFinding(
                code="vision_unparseable",
                severity="error",
                field=field,
                message=str(exc),
                page=reading.page,
            )
        )
        return
    for key in ("min", "nom", "max", "reference"):
        if not _number_close(getattr(spec_dimension, key), getattr(parsed, key)):
            findings.append(
                SpecFinding(
                    code="value_mismatch",
                    severity="error",
                    field=field,
                    message=f"spec {key} does not match parsed vision value",
                    page=reading.page,
                )
            )
    words = _mechanical_words(extraction, extraction_dir, reading.page, reading)
    mechanical_numbers = [number for word in words for number in _numeric_tokens(word)]
    vision_numbers = parsed.numbers.copy()
    if parsed.count is not None and vision_numbers:
        vision_numbers.pop(0)
    if any(number not in mechanical_numbers for number in vision_numbers):
        findings.append(
            SpecFinding(
                code=(
                    "mechanical_unconfirmed_user_confirmed"
                    if confirmation_verified
                    else "mechanical_mismatch"
                ),
                severity="warning" if confirmation_verified else "error",
                field=field,
                message="mechanical lane does not support every vision number",
                page=reading.page,
            )
        )
    if reading.mechanical is not None and Counter(_numeric_tokens(reading.mechanical)) != Counter(
        parsed.numbers
    ):
        findings.append(
            SpecFinding(
                code="lane_disagreement",
                severity="error",
                field=field,
                message="mechanical and vision readings contain different numbers",
                page=reading.page,
            )
        )
    if parsed.symbols and not any(symbol in word for symbol in parsed.symbols for word in words):
        findings.append(
            SpecFinding(
                code="glyph_loss",
                severity="info",
                field=field,
                message="vision symbols are not present in mechanical words",
                page=reading.page,
            )
        )


def _pin_checks(
    spec: PartSpec,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    spec_dir: Path,
    pin: PinSpec,
    index: int,
    page: PageExtraction | None,
    confirmation_verified: bool,
    findings: list[SpecFinding],
) -> None:
    field = f"pins[{index}]"
    reading = pin.reading
    _read_vision(reading, spec_dir, page, field, findings)
    vision_tokens = set(_tokens(reading.vision))
    if pin.number not in vision_tokens or pin.name not in vision_tokens:
        findings.append(
            SpecFinding(
                code="vision_unparseable",
                severity="error",
                field=field,
                message="pin vision reading must contain its number and name as tokens",
                page=reading.page,
            )
        )
    words = _mechanical_words(extraction, extraction_dir, reading.page, reading)
    mechanical_tokens = {token for word in words for token in _tokens(word)}
    if pin.number not in mechanical_tokens or pin.name not in mechanical_tokens:
        findings.append(
            SpecFinding(
                code=(
                    "mechanical_unconfirmed_user_confirmed"
                    if confirmation_verified
                    else "mechanical_mismatch"
                ),
                severity="warning" if confirmation_verified else "error",
                field=field,
                message="mechanical lane does not support the pin number and name",
                page=reading.page,
            )
        )
    rows = page_tables(extraction, extraction_dir, reading.page)
    if rows and not any(
        any(pin.number in _tokens(cell or "") for cell in row)
        and any((cell or "").strip() == pin.name for cell in row)
        for table in rows
        for row in table
    ):
        findings.append(
            SpecFinding(
                code="pin_table_unmatched",
                severity="warning",
                field=field,
                message="no pdfplumber pin-table row matches the pin number and name",
                page=reading.page,
            )
        )


def _dimension_upper(dimension: Dimension) -> float | None:
    return dimension.max if dimension.max is not None else dimension.nom


def _consistency_checks(spec: PartSpec, findings: list[SpecFinding]) -> None:
    package = spec.package
    number_counts = Counter(pin.number for pin in spec.pins)
    for number, count in number_counts.items():
        if count > 1:
            findings.append(
                SpecFinding(
                    code="duplicate_pin",
                    severity="error",
                    field="pins",
                    message=f"pin number {number} appears {count} times",
                )
            )
    exposed_number = package.exposed_pad.number if package.exposed_pad else None
    signal_pin_count = sum(pin.number != exposed_number for pin in spec.pins)
    if signal_pin_count != package.pin_count:
        findings.append(
            SpecFinding(
                code="pin_count_mismatch",
                severity="error",
                field="package.pin_count",
                message=f"expected {package.pin_count} non-exposed pins, found {signal_pin_count}",
            )
        )
    if package.exposed_pad is not None and package.exposed_pad.number not in number_counts:
        findings.append(
            SpecFinding(
                code="exposed_pad_unmapped",
                severity="error",
                field="package.exposed_pad.number",
                message="exposed pad number is not present in the pin list",
            )
        )
    quad_family = package.family in ("no_lead_quad", "gullwing_quad")
    dual_family = package.family in ("no_lead_dual", "gullwing_dual")
    side_counts: tuple[int, int, int, int] | None = None
    if quad_family:
        if package.pins_per_side is None:
            if package.pin_count % 4 == 0:
                pins_per_side = package.pin_count // 4
                side_counts = (pins_per_side, pins_per_side, pins_per_side, pins_per_side)
        else:
            side_counts = package.pins_per_side
        if side_counts is None or sum(side_counts) != package.pin_count:
            findings.append(
                SpecFinding(
                    code="pin_count_family",
                    severity="error",
                    field="package.pin_count",
                    message=(
                        "quad side pin counts must sum to pin_count; equal splits require "
                        "divisibility by four"
                    ),
                )
            )
    elif dual_family and package.pin_count % 2:
        findings.append(
            SpecFinding(
                code="pin_count_family",
                severity="error",
                field="package.pin_count",
                message="dual package pin count must be divisible by two",
            )
        )
    if (quad_family or dual_family) and package.pitch is not None:
        pitch = package.pitch.nom
        if pitch is None and package.pitch.min is not None and package.pitch.max is not None:
            pitch = (package.pitch.min + package.pitch.max) / 2
        if pitch is not None and quad_family and side_counts is not None:
            side_dimensions = (
                ("left", side_counts[0], package.body_length),
                ("bottom", side_counts[1], package.body_width),
                ("right", side_counts[2], package.body_length),
                ("top", side_counts[3], package.body_width),
            )
            for side, pins_per_side, dimension in side_dimensions:
                body_side = _dimension_upper(dimension)
                if (
                    pins_per_side > 1
                    and body_side is not None
                    and pitch * (pins_per_side - 1) >= body_side
                ):
                    findings.append(
                        SpecFinding(
                            code="pitch_exceeds_body",
                            severity="error",
                            field="package.pitch",
                            message=f"{side} pitch span must be smaller than its body extent",
                        )
                    )
        elif pitch is not None and dual_family:
            body_length = _dimension_upper(package.body_length)
            pins_per_side = package.pin_count // 2
            if (
                pins_per_side > 1
                and body_length is not None
                and pitch * (pins_per_side - 1) >= body_length
            ):
                findings.append(
                    SpecFinding(
                        code="pitch_exceeds_body",
                        severity="error",
                        field="package.pitch",
                        message="dual-row pitch span must be smaller than body_length",
                    )
                )
    if package.exposed_pad is not None:
        body_length = _dimension_upper(package.body_length)
        body_width = _dimension_upper(package.body_width)
        pad_length = _dimension_upper(package.exposed_pad.length)
        pad_width = _dimension_upper(package.exposed_pad.width)
        if (
            body_length is not None
            and body_width is not None
            and pad_length is not None
            and pad_width is not None
            and (pad_length >= body_length or pad_width >= body_width)
        ):
            findings.append(
                SpecFinding(
                    code="exposed_pad_exceeds_body",
                    severity="error",
                    field="package.exposed_pad",
                    message="exposed pad dimensions must be smaller than the package body",
                )
            )
    height_upper = (
        _dimension_upper(package.height)
        if _dimension_upper(package.height) is not None
        else package.height.min
    )
    if height_upper is not None and height_upper <= 0:
        findings.append(
            SpecFinding(
                code="height_nonpositive",
                severity="error",
                field="package.height",
                message="package height upper bound must be positive",
            )
        )


def check_part_spec(
    spec: PartSpec,
    extraction: DatasheetExtraction,
    *,
    spec_path: Path,
    extraction_path: Path,
) -> PartSpecReport:
    """Cross-check every authored reading against extraction and provenance."""
    findings: list[SpecFinding] = []
    spec_dir = spec_path.resolve().parent
    extraction_dir = extraction_path.resolve().parent
    pdf_path = _resolved(spec_dir, spec.datasheet.path)
    actual_spec_sha = part_spec_sha256(spec_path)
    actual_extraction_sha = hashlib.sha256(extraction_path.read_bytes()).hexdigest()
    if spec.datasheet.sha256 != extraction.pdf_sha256:
        findings.append(
            SpecFinding(
                code="datasheet_sha_mismatch",
                severity="error",
                field="datasheet.sha256",
                message="PartSpec datasheet SHA-256 differs from the extraction",
            )
        )
    if not pdf_path.is_file():
        findings.append(
            SpecFinding(
                code="datasheet_missing",
                severity="error",
                field="datasheet.path",
                message=f"datasheet file is missing: {pdf_path}",
            )
        )
    elif hashlib.sha256(pdf_path.read_bytes()).hexdigest() != spec.datasheet.sha256:
        findings.append(
            SpecFinding(
                code="datasheet_sha_mismatch",
                severity="error",
                field="datasheet.sha256",
                message="PartSpec datasheet SHA-256 differs from the PDF file",
            )
        )

    pages = {page.page: page for page in extraction.pages}
    readings = _all_readings(spec)
    user_requirements = _user_requirements(spec, spec_dir)
    cited_pages = {reading.page for _, reading, _ in readings}
    for page_number in cited_pages:
        page = pages.get(page_number)
        if page is not None:
            findings.extend(_page_artifact_findings(page, extraction_dir))

    observation_log = _observation_log(spec_dir)
    if observation_log is None:
        findings.append(
            SpecFinding(
                code="vision_observation_log_missing",
                severity="warning",
                field="observations",
                message="no image observation log was found for the cited datasheet pages",
            )
        )
    else:
        observed_hashes = _observed_image_hashes(observation_log)
        for page_number in cited_pages:
            page = pages.get(page_number)
            if page is not None and page.png_sha256 not in observed_hashes:
                findings.append(
                    SpecFinding(
                        code="vision_not_observed",
                        severity="error",
                        field=f"pages[{page_number}].png_path",
                        message="page image SHA-256 is not recorded in the observation log",
                        page=page_number,
                    )
                )

    for field, reading, dimension in readings:
        page = pages.get(reading.page)
        confirmation_verified = (
            reading.user_confirmed is not None and reading.user_confirmed in user_requirements
        )
        if reading.user_confirmed is not None and not confirmation_verified:
            findings.append(
                SpecFinding(
                    code="user_confirmation_unverified",
                    severity="error",
                    field=field,
                    message=(f"{reading.user_confirmed} is not a user-sourced intake requirement"),
                    page=reading.page,
                )
            )
        if page is None:
            findings.append(
                SpecFinding(
                    code="page_not_extracted",
                    severity="error",
                    field=field,
                    message=f"page {reading.page} is not present in the extraction",
                    page=reading.page,
                )
            )
        if dimension is not None:
            _dimension_checks(
                dimension,
                reading,
                field,
                page,
                spec_dir,
                extraction,
                extraction_dir,
                confirmation_verified,
                findings,
            )
        elif field == "package.pin1_reading":
            _read_vision(reading, spec_dir, page, field, findings)
            _pin1_corner_findings(spec.package, reading, field, findings)
    for index, pin in enumerate(spec.pins):
        reading = pin.reading
        confirmation_verified = (
            reading.user_confirmed is not None and reading.user_confirmed in user_requirements
        )
        _pin_checks(
            spec,
            extraction,
            extraction_dir,
            spec_dir,
            pin,
            index,
            pages.get(reading.page),
            confirmation_verified,
            findings,
        )

    for page_number in cited_pages:
        page = pages.get(page_number)
        if page is None:
            continue
        if page.order_similarity is not None and page.order_similarity < 0.6:
            findings.append(
                SpecFinding(
                    code="reading_order_divergence",
                    severity="info",
                    field="page",
                    message="Poppler and pdfplumber word order differs substantially",
                    page=page_number,
                )
            )
        if not any(lane.status == "ok" for lane in page.lanes):
            findings.append(
                SpecFinding(
                    code="mechanical_lane_unavailable",
                    severity="error",
                    field="page",
                    message="all extraction lanes for this cited page are non-ok",
                    page=page_number,
                )
            )
    _consistency_checks(spec, findings)
    return PartSpecReport(
        artifact_kind="circuit_part_spec_check",
        verdict="fail" if any(finding.severity == "error" for finding in findings) else "pass",
        part_spec_sha256=actual_spec_sha,
        extraction_sha256=actual_extraction_sha,
        pdf_sha256=extraction.pdf_sha256,
        checked_readings=len(readings),
        findings=findings,
    )
