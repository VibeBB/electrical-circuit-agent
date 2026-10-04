"""Agent-authored part specifications and deterministic datasheet checks."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Sequence
from contextlib import ExitStack
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from . import advisory, confidential, datasheet, humanrequest, visionread
from . import pinout as pinout_oracle
from .datasheet import (
    DatasheetError,
    DatasheetExtraction,
    PageExtraction,
    PdfWord,
)
from .pinout import PinoutGeometry

_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)")
_SYMBOL_PREFIX = re.compile(r"^([□⌀ØR])\s*")
_TOKEN_SPLIT = re.compile(r"[\s,]+")
_PIN1_CORNER = re.compile(r"\b(?P<corner>(?:top|bottom)[\s_-]+(?:left|right))\b", re.IGNORECASE)
_DRAWING_PHRASES = (
    "PACKAGE OUTLINE",
    "PACKAGE DRAWING",
    "MECHANICAL DATA",
    "LAND PATTERN",
    "BOARD LAYOUT",
    "SOLDER MASK",
    "STENCIL",
)
PinCorner = Literal["top_left", "top_right", "bottom_left", "bottom_right"]
DimensionKind = Literal["limit", "bilateral", "basic", "reference", "typical"]
CellKey = Literal["min", "nom", "max"]
_MIRRORED_CORNERS: dict[PinCorner, PinCorner] = {
    "top_left": "top_right",
    "top_right": "top_left",
    "bottom_left": "bottom_right",
    "bottom_right": "bottom_left",
}


class CellRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: int = Field(ge=0)
    row: int = Field(ge=0)
    col: int = Field(ge=0)


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    cells: dict[CellKey, CellRef] | None = None
    mechanical: str | None = None
    vision: str | None = Field(default=None, min_length=1)
    vision_record: str | None = Field(default=None, min_length=1)
    vision_read: str | None = None
    alternative_evidence: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")

    @model_validator(mode="after")
    def validate_source(self) -> Reading:
        if self.alternative_evidence is None and (
            self.page is None or self.vision is None or self.vision_record is None
        ):
            raise ValueError(
                "readings require page, vision, and vision_record unless alternative "
                "evidence is cited"
            )
        return self


class Dimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    kind: DimensionKind = "limit"
    min: float | None = None
    nom: float | None = None
    max: float | None = None
    reference: bool = False
    reading: Reading

    @model_validator(mode="after")
    def validate_values(self) -> Dimension:
        if self.min is None and self.nom is None and self.max is None:
            raise ValueError("at least one of min, nom, or max is required")
        if self.reading.bbox is None and self.reading.alternative_evidence is None:
            raise ValueError("dimension reading requires a bounding box")
        values = [value for value in (self.min, self.nom, self.max) if value is not None]
        if values != sorted(values):
            raise ValueError("dimension values must satisfy min <= nom <= max")
        return self


def _simple_polygon(vertices: list[tuple[float, float]]) -> bool:
    if len(vertices) < 3 or len(set(vertices)) != len(vertices):
        return False
    if any(not math.isfinite(value) for point in vertices for value in point):
        return False
    area = sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(vertices, vertices[1:] + vertices[:1], strict=True)
    )
    if math.isclose(area, 0.0, abs_tol=1e-9):
        return False

    def cross(
        first: tuple[float, float],
        second: tuple[float, float],
        third: tuple[float, float],
    ) -> float:
        return (second[0] - first[0]) * (third[1] - first[1]) - (second[1] - first[1]) * (
            third[0] - first[0]
        )

    def on_segment(
        first: tuple[float, float],
        second: tuple[float, float],
        point: tuple[float, float],
    ) -> bool:
        return (
            min(first[0], second[0]) - 1e-9 <= point[0] <= max(first[0], second[0]) + 1e-9
            and min(first[1], second[1]) - 1e-9 <= point[1] <= max(first[1], second[1]) + 1e-9
            and math.isclose(cross(first, second, point), 0.0, abs_tol=1e-9)
        )

    def intersects(
        a: tuple[float, float],
        b: tuple[float, float],
        c: tuple[float, float],
        d: tuple[float, float],
    ) -> bool:
        ab_c, ab_d = cross(a, b, c), cross(a, b, d)
        cd_a, cd_b = cross(c, d, a), cross(c, d, b)
        if ab_c * ab_d < 0 and cd_a * cd_b < 0:
            return True
        return (
            (math.isclose(ab_c, 0.0, abs_tol=1e-9) and on_segment(a, b, c))
            or (math.isclose(ab_d, 0.0, abs_tol=1e-9) and on_segment(a, b, d))
            or (math.isclose(cd_a, 0.0, abs_tol=1e-9) and on_segment(c, d, a))
            or (math.isclose(cd_b, 0.0, abs_tol=1e-9) and on_segment(c, d, b))
        )

    edges = list(zip(vertices, vertices[1:] + vertices[:1], strict=True))
    for first_index, first_edge in enumerate(edges):
        for second_index in range(first_index + 1, len(edges)):
            if second_index in {first_index, first_index + 1} or (
                first_index == 0 and second_index == len(edges) - 1
            ):
                continue
            if intersects(*first_edge, *edges[second_index]):
                return False
    return True


class ExposedPad(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    length: Dimension
    width: Dimension
    center_x: Dimension | None = None
    center_y: Dimension | None = None
    rotation_deg: float = 0.0
    polygon: list[tuple[float, float]] | None = None

    @model_validator(mode="after")
    def validate_geometry(self) -> ExposedPad:
        if not math.isfinite(self.rotation_deg):
            raise ValueError("rotation_deg must be finite")
        if self.polygon is not None and not _simple_polygon(self.polygon):
            raise ValueError("polygon must be simple with at least three vertices")
        return self


class TabSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    width: Dimension
    length: Dimension
    offset: Dimension


class BallGrid(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[str] = Field(min_length=1)
    columns: int = Field(gt=0)
    pitch_x: Dimension
    pitch_y: Dimension
    ball_diameter: Dimension

    @field_validator("rows")
    @classmethod
    def validate_rows(cls, rows: list[str]) -> list[str]:
        if any(re.fullmatch(r"(?:[A-H]|[J-N]|P|R|[T-W]|Y)", row) is None for row in rows):
            raise ValueError("BGA rows must use allowed JEDEC letters")
        if len(rows) != len(set(rows)) or rows != sorted(rows):
            raise ValueError("BGA rows must be unique and sorted")
        return rows


class PackageSpec(BaseModel):
    """Package dimensions use the KiCad top view: pin 1 at top-left, +x right, +y down.

    `body_length` is the Y extent along the pin-1 row of dual packages and
    along the left/right sides of quad packages; `body_width` is the X extent.
    `pin1_corner` is always expressed in top view; `drawing_view` identifies
    the view shown in the cited pin-1 evidence.
    """

    model_config = ConfigDict(extra="forbid")

    family: Literal[
        "no_lead_quad",
        "no_lead_dual",
        "gullwing_quad",
        "gullwing_dual",
        "chip",
        "sot223",
        "tabbed_dpak",
        "sod",
        "bga",
        "through_hole_inline",
        "custom",
    ]
    drawing_id: str
    drawing_revision: str | None = None
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
    exposed_pads: list[ExposedPad] = Field(default_factory=list[ExposedPad])
    tab: TabSpec | None = None
    missing_pins: list[str] = Field(default_factory=list)
    ball_grid: BallGrid | None = None
    pins_per_side: tuple[int, int, int, int] | None = None
    drawing_view: Literal["top", "bottom"]
    pin1_corner: PinCorner
    pin1_reading: Reading

    @model_validator(mode="after")
    def validate_pins_per_side_family(self) -> PackageSpec:
        if self.pin1_reading.bbox is None and self.pin1_reading.alternative_evidence is None:
            raise ValueError("pin-1 reading requires a bounding box")
        if self.pins_per_side is not None and self.family not in (
            "no_lead_quad",
            "gullwing_quad",
        ):
            raise ValueError("pins_per_side is only valid for quad package families")
        if self.pins_per_side is not None and any(count <= 0 for count in self.pins_per_side):
            raise ValueError("pins_per_side counts must be positive")
        tab_family = self.family in {"sot223", "tabbed_dpak"}
        if tab_family != (self.tab is not None):
            raise ValueError("sot223 and tabbed_dpak packages require exactly one tab")
        if (self.family == "bga") != (self.ball_grid is not None):
            raise ValueError("bga packages require exactly one ball_grid")
        if self.family == "sod" and self.pin_count + len(self.missing_pins) != 2:
            raise ValueError("sod packages must have two terminal positions")
        exposed_numbers = [
            exposed.number
            for exposed in (
                ([self.exposed_pad] if self.exposed_pad is not None else []) + self.exposed_pads
            )
        ]
        auxiliary_numbers = exposed_numbers + ([self.tab.number] if self.tab is not None else [])
        if len(exposed_numbers) != len(set(exposed_numbers)):
            raise ValueError("exposed pad numbers must be unique")
        if len(auxiliary_numbers) != len(set(auxiliary_numbers)):
            raise ValueError("tab and exposed pad numbers must be unique")
        if len(self.missing_pins) != len(set(self.missing_pins)):
            raise ValueError("missing_pins entries must be unique")
        if set(self.missing_pins) & set(auxiliary_numbers):
            raise ValueError("missing pins cannot be tab or exposed pad numbers")
        if self.ball_grid is not None:
            site_count = len(self.ball_grid.rows) * self.ball_grid.columns
            if len(self.missing_pins) >= site_count:
                raise ValueError("BGA must have at least one populated ball site")
            if self.pin_count != site_count - len(self.missing_pins):
                raise ValueError("BGA pin_count must match populated ball sites")
            grid_numbers = {
                f"{row}{column}"
                for row in self.ball_grid.rows
                for column in range(1, self.ball_grid.columns + 1)
            }
            if not set(self.missing_pins).issubset(grid_numbers):
                raise ValueError("BGA missing_pins must identify grid sites")
        elif any(not number.isdigit() for number in self.missing_pins):
            raise ValueError("non-BGA missing_pins entries must be numeric")
        return self

    @property
    def all_exposed_pads(self) -> list[ExposedPad]:
        return ([self.exposed_pad] if self.exposed_pad is not None else []) + self.exposed_pads

    @property
    def auxiliary_pad_numbers(self) -> set[str]:
        numbers = {pad.number for pad in self.all_exposed_pads}
        if self.tab is not None:
            numbers.add(self.tab.number)
        return numbers


def expected_signal_pin_numbers(package: PackageSpec) -> set[str]:
    if package.ball_grid is not None:
        return {
            f"{row}{column}"
            for row in package.ball_grid.rows
            for column in range(1, package.ball_grid.columns + 1)
        } - set(package.missing_pins)
    return {
        str(number) for number in range(1, package.pin_count + len(package.missing_pins) + 1)
    } - set(package.missing_pins)


class LandPad(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    x: float
    y: float
    width: float
    height: float
    shape: Literal["rect", "roundrect", "oval", "circle", "polygon"]
    rotation: float = 0.0
    polygon: list[tuple[float, float]] | None = None
    kind: Literal["signal", "exposed", "tab"] = "signal"

    @model_validator(mode="after")
    def validate_geometry(self) -> LandPad:
        if not math.isfinite(self.rotation):
            raise ValueError("pad rotation must be finite")
        if (self.shape == "polygon") != (self.polygon is not None):
            raise ValueError("polygon vertices are required only for polygon pads")
        if self.polygon is not None and not _simple_polygon(self.polygon):
            raise ValueError("polygon must be simple with at least three vertices")
        return self


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
    """A pin's optional view identifies the datasheet drawing view for its reading."""

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
    view: Literal["top", "bottom"] | None = None
    bank: str | None = None
    reading: Reading


class OrderableVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mpn: str
    package_designator: str
    pin_count: int = Field(gt=0)
    row: CellRef
    reading: Reading


class PinoutDrawing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float]
    view: Literal["top", "bottom"]
    view_reading: Reading
    labels_vision: dict[str, str]
    vision_record: str = Field(min_length=1)
    labels_vision_read: str | None = None

    @model_validator(mode="after")
    def validate_view_reading(self) -> PinoutDrawing:
        if self.view_reading.bbox is None and self.view_reading.alternative_evidence is None:
            raise ValueError("pinout view reading requires a bounding box")
        if self.view_reading.alternative_evidence is None and self.view_reading.page != self.page:
            raise ValueError("pinout view reading page must match the pinout page")
        return self


class PinTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    table: int = Field(ge=0)
    number_col: int = Field(ge=0)
    name_col: int = Field(ge=0)
    header_rows: int = Field(default=1, ge=0)
    column_designator: str | None = None
    vision_read: str | None = None


class DatasheetRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(min_length=1)
    extraction_path: str
    url: str | None = None
    confidential: bool = False
    origin: Literal["web", "user_provided"] = "web"


class SubstitutionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    substitute_mpn: str = Field(min_length=1)
    scope: list[humanrequest.SubstituteScope] = Field(min_length=1)

    @field_validator("scope")
    @classmethod
    def require_unique_scope(
        cls,
        value: list[humanrequest.SubstituteScope],
    ) -> list[humanrequest.SubstituteScope]:
        if len(value) != len(set(value)):
            raise ValueError("substitution scope entries must be unique")
        return value


class PartSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    _source_file_path: Path | None = PrivateAttr(default=None)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PartSpec):
            return NotImplemented
        return self.model_dump(mode="python") == other.model_dump(mode="python")

    artifact_kind: Literal["circuit_part_spec"]
    mpn: str
    manufacturer: str
    datasheet: DatasheetRef
    substitution: SubstitutionRef | None = None
    package: PackageSpec
    land_pattern: LandPattern | None = None
    pinout: PinoutDrawing | None = None
    pins: list[PinSpec] = Field(min_length=1)
    pin_table: PinTable
    orderable: list[OrderableVariant] = Field(min_length=1)
    authoring: str | None = None
    orderable_vision_read: str | None = None

    @model_validator(mode="after")
    def validate_missing_pins(self) -> PartSpec:
        present_numbers = {pin.number for pin in self.pins}
        overlap = present_numbers & set(self.package.missing_pins)
        if overlap:
            raise ValueError(f"missing pins cannot be present in PartSpec pins: {sorted(overlap)}")
        return self

    @property
    def source_file_path(self) -> Path | None:
        return self._source_file_path

    def bind_source_file(self, path: Path) -> None:
        self._source_file_path = path.resolve()

class ParsedDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: float | None = None
    nom: float | None = None
    max: float | None = None
    reference: bool = False
    kind: DimensionKind
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
    pinout: PinoutGeometry | None = None
    substitute_permit: humanrequest.SubstitutePermit | None = None


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

    kind: DimensionKind = "limit"
    suffix = re.search(r"\s+(BSC|TYP)\s*$", remaining, re.IGNORECASE)
    if suffix is not None:
        kind = "basic" if suffix.group(1).upper() == "BSC" else "typical"
        remaining = remaining[: suffix.start()].strip()

    value_min: float | None = None
    value_nom: float | None = None
    value_max: float | None = None
    reference = False
    literal = rf"{_NUMBER.pattern}"
    if match := re.fullmatch(rf"\(\s*({literal})\s*\)", remaining):
        value_nom = float(match.group(1))
        reference = True
        kind = "reference"
    elif match := re.fullmatch(rf"({literal})\s*±\s*({literal})", remaining):
        value_nom = float(match.group(1))
        tolerance = float(match.group(2))
        value_min = round(value_nom - tolerance, 6)
        value_max = round(value_nom + tolerance, 6)
        kind = "bilateral"
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
        kind=kind,
        count=count,
        symbols=symbols,
        numbers=numbers,
    )


def load_part_spec(path: Path) -> PartSpec:
    try:
        spec = PartSpec.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"could not load part spec {path}: {exc}") from exc
    spec.bind_source_file(path)
    return spec


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
        exposed_dimensions = [("package.exposed_pad", package.exposed_pad)]
    else:
        exposed_dimensions = []
    exposed_dimensions.extend(
        (f"package.exposed_pads[{index}]", exposed)
        for index, exposed in enumerate(package.exposed_pads)
    )
    for field, exposed in exposed_dimensions:
        for name, dimension in (
            ("length", exposed.length),
            ("width", exposed.width),
            ("center_x", exposed.center_x),
            ("center_y", exposed.center_y),
        ):
            if dimension is not None:
                found.append((f"{field}.{name}", dimension))
    if package.tab is not None:
        found.extend(
            (
                (f"package.tab.{name}", dimension)
                for name, dimension in (
                    ("width", package.tab.width),
                    ("length", package.tab.length),
                    ("offset", package.tab.offset),
                )
            )
        )
    if package.ball_grid is not None:
        found.extend(
            (
                (f"package.ball_grid.{name}", dimension)
                for name, dimension in (
                    ("pitch_x", package.ball_grid.pitch_x),
                    ("pitch_y", package.ball_grid.pitch_y),
                    ("ball_diameter", package.ball_grid.ball_diameter),
                )
            )
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
    readings.extend(
        (f"orderable[{index}]", variant.reading, None)
        for index, variant in enumerate(spec.orderable)
    )
    if spec.pinout is not None:
        readings.append(("pinout.view_reading", spec.pinout.view_reading, None))
    return readings


def _resolved(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


_RE_DERIVATION_STACK: ContextVar[ExitStack | None] = ContextVar(
    "partspec_rederivation_stack",
    default=None,
)
_RE_DERIVATION_BASE: ContextVar[Path | None] = ContextVar(
    "partspec_rederivation_base",
    default=None,
)


def rederive_pages(
    pdf_path: Path, pages: Sequence[int], dpi: int
) -> tuple[DatasheetExtraction, Path]:
    stack = _RE_DERIVATION_STACK.get()
    if stack is None:
        raise DatasheetError("rederive_pages must run within a PartSpec check")
    base_dir = _RE_DERIVATION_BASE.get()
    if base_dir is not None:
        base_dir.mkdir(parents=True, exist_ok=True)
    output_dir = Path(
        stack.enter_context(tempfile.TemporaryDirectory(prefix="circuit-partspec-", dir=base_dir))
    )
    return (
        datasheet.extract_datasheet(pdf_path, output_dir, pages=pages, dpi=dpi),
        output_dir,
    )


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
    if reading.vision is None:
        findings.append(
            SpecFinding(
                code="evidence_missing",
                severity="error",
                field=field,
                message="pin-1 reading has no vision transcription",
                page=reading.page,
            )
        )
        return
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
    expected = package.pin1_corner
    if package.drawing_view == "bottom":
        expected = _MIRRORED_CORNERS[expected]
    if corner != expected:
        findings.append(
            SpecFinding(
                code="pin1_mismatch",
                severity="error",
                field=field,
                message=(
                    f"pin-1 vision corner {corner} differs from {expected} "
                    f"in {package.drawing_view} view"
                ),
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
    if reading.page is None or reading.vision_record is None:
        findings.append(
            SpecFinding(
                code="evidence_missing",
                severity="error",
                field=field,
                message="reading has no datasheet page or visual review record",
                page=reading.page,
            )
        )
        return
    _vision_record_check(
        reading.vision_record,
        reading.page,
        spec_dir,
        page,
        field,
        findings,
    )


def _vision_record_check(
    vision_record: str,
    page_number: int,
    spec_dir: Path,
    page: PageExtraction | None,
    field: str,
    findings: list[SpecFinding],
) -> None:
    record_path = _resolved(spec_dir, vision_record)
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
                page=page_number,
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
                page=page_number,
            )
        )


def _inside_bbox(word_x: float, word_y: float, bbox: tuple[float, float, float, float]) -> bool:
    x0, top, x1, bottom = bbox
    return x0 <= word_x <= x1 and top <= word_y <= bottom


def _lane_words(
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page_number: int,
    lane_name: Literal["poppler", "pdfplumber"],
) -> list[PdfWord]:
    page = next((item for item in extraction.pages if item.page == page_number), None)
    lane = (
        None
        if page is None
        else next((item for item in page.lanes if item.lane == lane_name), None)
    )
    if lane is None or lane.status != "ok" or lane.words_path is None:
        return []
    try:
        value: object = json.loads(
            _resolved(extraction_dir, lane.words_path).read_text(encoding="utf-8")
        )
        if not isinstance(value, list):
            return []
        return [PdfWord.model_validate(item) for item in cast(list[object], value)]
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def _visible_words(
    words: list[PdfWord],
    page: PageExtraction,
    extraction_dir: Path,
) -> tuple[list[PdfWord], list[PdfWord]]:
    image_path = _resolved(extraction_dir, page.png_path)
    return datasheet.words_by_ink(
        image_path,
        words,
        dpi=page.dpi,
        width_pt=page.width_pt,
        height_pt=page.height_pt,
    )


def _words_in_bbox(
    words: list[PdfWord], bbox: tuple[float, float, float, float] | None
) -> list[PdfWord]:
    if bbox is None:
        return []
    return [
        word
        for word in words
        if _inside_bbox((word.x0 + word.x1) / 2, (word.top + word.bottom) / 2, bbox)
    ]


def _numbers_in_bbox(
    words: list[PdfWord],
    bbox: tuple[float, float, float, float] | None,
    count: int | None,
) -> Counter[str]:
    selected = _words_in_bbox(words, bbox)
    selected.sort(key=lambda word: (word.x0, word.top))
    numbers = [number for word in selected for number in _numeric_tokens(word.text)]
    count_token = _decimal_string(str(count)) if count is not None else None
    if count_token is not None and numbers and numbers[0] == count_token:
        numbers.pop(0)
    return Counter(numbers)


def _bbox_findings(
    reading: Reading,
    page: PageExtraction | None,
    field: str,
    findings: list[SpecFinding],
) -> None:
    if reading.bbox is None:
        findings.append(
            SpecFinding(
                code="bbox_missing",
                severity="error",
                field=field,
                message="reading requires a bounded evidence region",
                page=reading.page,
            )
        )
        return
    if page is None:
        return
    x0, top, x1, bottom = reading.bbox
    if (
        x0 < 0
        or top < 0
        or x1 > page.width_pt
        or bottom > page.height_pt
        or x1 <= x0
        or bottom <= top
    ):
        findings.append(
            SpecFinding(
                code="bbox_outside_page",
                severity="error",
                field=field,
                message="reading bounding box must be a non-empty region inside the page",
                page=reading.page,
            )
        )
        return
    if (x1 - x0) * (bottom - top) > page.width_pt * page.height_pt * 0.02:
        findings.append(
            SpecFinding(
                code="bbox_too_large",
                severity="error",
                field=field,
                message="reading bounding box occupies more than two percent of the page",
                page=reading.page,
            )
        )


def _visible_pinout_tokens(
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page: PageExtraction,
    bbox: tuple[float, float, float, float],
) -> list[PdfWord]:
    lanes: dict[str, list[PdfWord]] = {}
    for lane_name in ("poppler", "pdfplumber"):
        words = _lane_words(extraction, extraction_dir, page.page, lane_name)
        visible, _ = _visible_words(words, page, extraction_dir)
        lanes[lane_name] = _words_in_bbox(visible, bbox)

    poppler = lanes["poppler"]
    pdfplumber = lanes["pdfplumber"]
    matched_pdfplumber: set[int] = set()
    tokens: list[PdfWord] = []
    for poppler_word in poppler:
        text = poppler_word.text.strip()
        if not text:
            continue
        matches = [
            (index, word)
            for index, word in enumerate(pdfplumber)
            if index not in matched_pdfplumber
            and word.text.strip() == text
            and math.dist(
                (
                    (poppler_word.x0 + poppler_word.x1) / 2,
                    (poppler_word.top + poppler_word.bottom) / 2,
                ),
                ((word.x0 + word.x1) / 2, (word.top + word.bottom) / 2),
            )
            <= 1.5
        ]
        if not matches:
            continue
        index, _ = min(
            matches,
            key=lambda item: math.dist(
                (
                    (poppler_word.x0 + poppler_word.x1) / 2,
                    (poppler_word.top + poppler_word.bottom) / 2,
                ),
                ((item[1].x0 + item[1].x1) / 2, (item[1].top + item[1].bottom) / 2),
            ),
        )
        matched_pdfplumber.add(index)
        tokens.append(
            PdfWord(
                text=text,
                x0=poppler_word.x0,
                top=poppler_word.top,
                x1=poppler_word.x1,
                bottom=poppler_word.bottom,
            )
        )
    return tokens


def _pinout_view_check(
    pinout: PinoutDrawing,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page: PageExtraction | None,
    findings: list[SpecFinding],
) -> None:
    expected = pinout.view
    phrase = re.compile(r"\b(top|bottom)\s+view\b")
    problems: list[str] = []
    mechanical = _normalise_text(pinout.view_reading.mechanical or "").casefold()
    mechanical_match = phrase.search(mechanical)
    label_disagrees = mechanical_match is not None and mechanical_match.group(1) != expected
    if mechanical_match is None or mechanical_match.group(1) != expected:
        problems.append("mechanical reading does not confirm the declared view")
    if page is None:
        problems.append("the pinout view page was not re-derived")
    else:
        for lane_name in ("poppler", "pdfplumber"):
            words = _lane_words(extraction, extraction_dir, pinout.page, lane_name)
            visible, _ = _visible_words(words, page, extraction_dir)
            bounded = _words_in_bbox(visible, pinout.view_reading.bbox)
            text = _normalise_text(" ".join(word.text for word in bounded)).casefold()
            lane_match = phrase.search(text)
            if lane_match is not None and lane_match.group(1) != expected:
                label_disagrees = True
            if lane_match is None or lane_match.group(1) != expected:
                problems.append(f"{lane_name} lane does not show the declared view")
    if label_disagrees:
        findings.append(
            SpecFinding(
                code="view_label_mismatch",
                severity="error",
                field="pinout.view_reading",
                message="the datasheet view label differs from the declared pinout view",
                page=pinout.page,
            )
        )
    if problems:
        findings.append(
            SpecFinding(
                code="pinout_view_unverified",
                severity="error",
                field="pinout",
                message="; ".join(problems),
                page=pinout.page,
            )
        )


def _pinout_fresh_checks(
    spec: PartSpec,
    spec_dir: Path,
    stored_page: PageExtraction | None,
    derived_page: PageExtraction | None,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    findings: list[SpecFinding],
) -> PinoutGeometry | None:
    drawing = spec.pinout
    if drawing is None:
        return None
    field = "pinout.view_reading"
    _read_vision(drawing.view_reading, spec_dir, stored_page, field, findings)
    _bbox_findings(drawing.view_reading, derived_page, field, findings)
    _vision_record_check(
        drawing.vision_record,
        drawing.page,
        spec_dir,
        stored_page,
        "pinout",
        findings,
    )
    _pinout_view_check(drawing, extraction, extraction_dir, derived_page, findings)
    if derived_page is None:
        findings.append(
            SpecFinding(
                code="pinout_unverified",
                severity="error",
                field="pinout",
                message="pinout page was not available from fresh PDF derivation",
                page=drawing.page,
            )
        )
        return None

    tokens = _visible_pinout_tokens(extraction, extraction_dir, derived_page, drawing.bbox)
    geometry, issues = pinout_oracle.derive_pinout(
        tokens,
        view=drawing.view,
        pin_count=spec.package.pin_count,
        page=drawing.page,
    )
    for issue in issues:
        findings.append(
            SpecFinding(
                code=issue.code,
                severity=issue.severity,
                field="pinout",
                message=issue.message,
                page=drawing.page,
            )
        )
    if geometry is None:
        return None

    labels = {label.number: label for label in geometry.labels}
    partspec_pins: dict[str, PinSpec] = {}
    for pin in spec.pins:
        partspec_pins.setdefault(pin.number, pin)
    drawing_names: dict[str, str] = {}
    actual_names: dict[str, str] = {}
    mismatched_names = False
    for number in range(1, spec.package.pin_count + 1):
        key = str(number)
        label = labels.get(key)
        mechanical_name = label.name if label is not None else None
        if mechanical_name is not None:
            drawing_names[key] = mechanical_name
        pin = partspec_pins.get(key)
        if pin is None:
            mismatched_names = True
            findings.append(
                SpecFinding(
                    code="pinout_name_mismatch",
                    severity="error",
                    field="pinout",
                    message=f"PartSpec has no non-exposed pin {key}",
                    page=drawing.page,
                )
            )
        else:
            actual_names[key] = pin.name
            if mechanical_name is None or not pinout_oracle.names_equal(mechanical_name, pin.name):
                mismatched_names = True
                findings.append(
                    SpecFinding(
                        code="pinout_name_mismatch",
                        severity="error",
                        field="pinout",
                        message=(
                            f"mechanical pinout name for pin {key} "
                            f"({mechanical_name or 'unresolved'}) differs from PartSpec "
                            f"({pin.name})"
                        ),
                        page=drawing.page,
                    )
                )
    expected_vision_keys = {str(number) for number in range(1, spec.package.pin_count + 1)}
    vision_keys = set(drawing.labels_vision)
    vision_disagreements = [
        number
        for number in sorted(expected_vision_keys & vision_keys, key=lambda value: int(value))
        if number not in labels
        or labels[number].name is None
        or not pinout_oracle.names_equal(drawing.labels_vision[number], labels[number].name or "")
    ]
    missing_vision = sorted(expected_vision_keys - vision_keys, key=lambda value: int(value))
    extra_vision = sorted(vision_keys - expected_vision_keys)
    if missing_vision or extra_vision or vision_disagreements:
        findings.append(
            SpecFinding(
                code="pinout_vision_mismatch",
                severity="error",
                field="pinout",
                message=(
                    f"vision labels differ from the mechanical pinout "
                    f"(missing={missing_vision}, extra={extra_vision}, "
                    f"disagree={vision_disagreements})"
                ),
                page=drawing.page,
            )
        )
    if mismatched_names:
        hypotheses = pinout_oracle.diagnose_permutation(
            {label.number: (label.x, label.y) for label in geometry.labels},
            drawing_names,
            actual_names,
        )
        findings.append(
            SpecFinding(
                code="pinout_permutation_diagnosis",
                severity="info",
                field="pinout",
                message=f"pinout name permutation hypotheses: {', '.join(hypotheses) or 'none'}",
                page=drawing.page,
            )
        )
    pin1 = labels.get("1")
    if pin1 is not None:
        corner = f"{'top' if pin1.y < 0 else 'bottom'}_{'left' if pin1.x < 0 else 'right'}"
        if corner != spec.package.pin1_corner:
            findings.append(
                SpecFinding(
                    code="pinout_pin1_corner_mismatch",
                    severity="error",
                    field="pinout",
                    message=(
                        f"pinout pin 1 is in {corner}, not PackageSpec {spec.package.pin1_corner}"
                    ),
                    page=drawing.page,
                )
            )
    if geometry.winding == "cw":
        findings.append(
            SpecFinding(
                code="pinout_winding_nonstandard",
                severity="warning",
                field="pinout",
                message="top-view pinout labels have clockwise winding",
                page=drawing.page,
            )
        )
    return geometry


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
    stored_page: PageExtraction | None,
    mechanical_page: PageExtraction | None,
    spec_dir: Path,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    findings: list[SpecFinding],
) -> None:
    if reading.page is None or reading.vision is None:
        findings.append(
            SpecFinding(
                code="evidence_missing",
                severity="error",
                field=field,
                message="dimension reading has no datasheet page or transcription",
                page=reading.page,
            )
        )
        return
    _read_vision(reading, spec_dir, stored_page, field, findings)
    _bbox_findings(reading, mechanical_page, field, findings)
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
    if spec_dimension.kind != parsed.kind:
        findings.append(
            SpecFinding(
                code="kind_mismatch",
                severity="error",
                field=field,
                message=f"spec kind {spec_dimension.kind} differs from parsed {parsed.kind}",
                page=reading.page,
            )
        )

    if mechanical_page is None:
        return
    lane_words = {
        lane: _lane_words(extraction, extraction_dir, reading.page, lane)
        for lane in ("poppler", "pdfplumber")
    }
    visible_by_lane: dict[str, list[PdfWord]] = {}
    invisible_by_lane: dict[str, list[PdfWord]] = {}
    for lane, words in lane_words.items():
        visible, invisible = _visible_words(words, mechanical_page, extraction_dir)
        visible_by_lane[lane] = _words_in_bbox(visible, reading.bbox)
        invisible_by_lane[lane] = _words_in_bbox(invisible, reading.bbox)

    expected_numbers = parsed.numbers.copy()
    if parsed.count is not None and expected_numbers:
        expected_numbers.pop(0)
    expected = Counter(expected_numbers)
    raw_matches = {
        lane: _numbers_in_bbox(words, reading.bbox, parsed.count) == expected
        for lane, words in lane_words.items()
    }
    visible_matches = {
        lane: _numbers_in_bbox(visible_by_lane[lane], reading.bbox, parsed.count) == expected
        for lane in ("poppler", "pdfplumber")
    }
    visible_counters = {
        lane: _numbers_in_bbox(visible_by_lane[lane], reading.bbox, parsed.count)
        for lane in ("poppler", "pdfplumber")
    }
    lane_only_support = any(
        (visible_counters["poppler"][number] > 0) != (visible_counters["pdfplumber"][number] > 0)
        for number in expected
    )
    if not mechanical_page.text_layer or lane_only_support or sum(visible_matches.values()) == 1:
        findings.append(
            SpecFinding(
                code="mechanical_single_lane",
                severity="error",
                field=field,
                message="mechanical support must agree in Poppler and pdfplumber lanes",
                page=reading.page,
            )
        )
    elif not all(visible_matches.values()):
        if any(
            raw_matches[lane] and not visible_matches[lane] for lane in ("poppler", "pdfplumber")
        ):
            findings.append(
                SpecFinding(
                    code="invisible_text",
                    severity="error",
                    field=field,
                    message="mechanical numeric support is present only in invisible text",
                    page=reading.page,
                )
            )
        else:
            findings.append(
                SpecFinding(
                    code="mechanical_mismatch",
                    severity="error",
                    field=field,
                    message=(
                        "visible numeric tokens in each lane must exactly match the vision reading"
                    ),
                    page=reading.page,
                )
            )

    stacked_order_wrong = False
    for lane in ("poppler", "pdfplumber"):
        numeric_words = [
            (word, _numeric_tokens(word.text))
            for word in visible_by_lane[lane]
            if _numeric_tokens(word.text)
        ]
        numeric_words.sort(key=lambda item: (item[0].x0, item[0].top))
        if parsed.count is not None:
            count_token = _decimal_string(str(parsed.count))
            for index, (word, numbers) in enumerate(numeric_words):
                if numbers and numbers[0] == count_token:
                    remaining_numbers = numbers[1:]
                    if remaining_numbers:
                        numeric_words[index] = (word, remaining_numbers)
                    else:
                        numeric_words.pop(index)
                    break
        if len(numeric_words) != 2:
            continue
        first, second = numeric_words[0][0], numeric_words[1][0]
        narrower_width = min(first.x1 - first.x0, second.x1 - second.x0)
        overlap = min(first.x1, second.x1) - max(first.x0, second.x0)
        vertical_gap = max(0.0, max(first.top, second.top) - min(first.bottom, second.bottom))
        taller_height = max(first.bottom - first.top, second.bottom - second.top)
        if (
            narrower_width <= 0
            or overlap / narrower_width < 0.5
            or vertical_gap > 1.5 * taller_height
        ):
            continue
        ordered_words = sorted(numeric_words, key=lambda item: item[0].top)
        upper_numbers = ordered_words[0][1]
        lower_numbers = ordered_words[1][1]
        expected_max = spec_dimension.max if spec_dimension.max is not None else spec_dimension.nom
        expected_min = spec_dimension.min if spec_dimension.min is not None else spec_dimension.nom
        stacked_order_wrong |= (
            len(upper_numbers) != 1
            or len(lower_numbers) != 1
            or not _number_close(float(upper_numbers[0]), expected_max)
            or not _number_close(float(lower_numbers[0]), expected_min)
        )
    if stacked_order_wrong:
        findings.append(
            SpecFinding(
                code="stacked_limit_order",
                severity="error",
                field=field,
                message="stacked drawing limits must place maximum above minimum",
                page=reading.page,
            )
        )

    glyphs = parsed.symbols
    tolerance_symbol_missing = "±" in reading.vision and not any(
        "±" in word.text for words in lane_words.values() for word in words
    )
    tolerance_pair_visible = False
    if tolerance_symbol_missing:
        base = parsed.numbers[0] if parsed.numbers else None
        tolerance = parsed.numbers[1] if len(parsed.numbers) > 1 else None
        if parsed.count is not None and base is not None:
            base = parsed.numbers[1] if len(parsed.numbers) > 1 else None
            tolerance = parsed.numbers[2] if len(parsed.numbers) > 2 else None
        baseline_pair = (
            base is not None
            and tolerance is not None
            and all(
                any(
                    _numeric_tokens(left.text) == [base]
                    and _numeric_tokens(right.text) == [tolerance]
                    and left.x0 < right.x0
                    and abs((left.top + left.bottom) / 2 - (right.top + right.bottom) / 2)
                    <= 0.5 * max(left.bottom - left.top, right.bottom - right.top)
                    for left in visible_by_lane[lane]
                    for right in visible_by_lane[lane]
                )
                for lane in ("poppler", "pdfplumber")
            )
        )
        tolerance_pair_visible = baseline_pair
        findings.append(
            SpecFinding(
                code="glyph_loss" if baseline_pair else "glyph_loss_ambiguous",
                severity="warning" if baseline_pair else "error",
                field=field,
                message=(
                    "vision tolerance symbol is absent from mechanical words"
                    if baseline_pair
                    else "lost tolerance symbol has no unambiguous base/tolerance word pair"
                ),
                page=reading.page,
            )
        )
    if (
        glyphs
        and not all(
            all(any(symbol in word.text for word in visible_by_lane[lane]) for symbol in glyphs)
            for lane in ("poppler", "pdfplumber")
        )
        and not (tolerance_symbol_missing and tolerance_pair_visible)
    ):
        findings.append(
            SpecFinding(
                code="glyph_loss",
                severity="warning",
                field=field,
                message="vision symbols are not present in mechanical words",
                page=reading.page,
            )
        )


def _pin_checks(
    spec: PartSpec,
    spec_dir: Path,
    pin: PinSpec,
    index: int,
    stored_page: PageExtraction | None,
    findings: list[SpecFinding],
) -> None:
    field = f"pins[{index}]"
    reading = pin.reading
    if reading.page is None or reading.vision is None:
        findings.append(
            SpecFinding(
                code="evidence_missing",
                severity="error",
                field=field,
                message="pin reading has no datasheet page or transcription",
                page=reading.page,
            )
        )
        return
    _read_vision(reading, spec_dir, stored_page, field, findings)
    if reading.page != spec.pin_table.page:
        findings.append(
            SpecFinding(
                code="pin_reading_page_mismatch",
                severity="error",
                field=field,
                message="pin reading page must match the bound pin table page",
                page=reading.page,
            )
        )
    vision_tokens = set(_tokens(reading.vision))
    normalized_vision = _normalise_text(reading.vision).casefold()
    normalized_name = _normalise_text(pin.name).casefold()
    name_present = (
        re.search(rf"(?<!\w){re.escape(normalized_name)}(?!\w)", normalized_vision) is not None
    )
    is_exposed_pad_pin = pin.number in spec.package.auxiliary_pad_numbers
    if (pin.number not in vision_tokens and not is_exposed_pad_pin) or not name_present:
        findings.append(
            SpecFinding(
                code="vision_unparseable",
                severity="error",
                field=field,
                message="pin vision reading must contain its number and name as tokens",
                page=reading.page,
            )
        )


def _union_bbox(
    bboxes: Sequence[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    if not bboxes:
        return None
    return (
        min(bbox[0] for bbox in bboxes),
        min(bbox[1] for bbox in bboxes),
        max(bbox[2] for bbox in bboxes),
        max(bbox[3] for bbox in bboxes),
    )


def _reading_cells_bbox(
    reading: Reading,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
) -> tuple[float, float, float, float] | None:
    if reading.cells is None or reading.page is None:
        return None
    boxes: list[tuple[float, float, float, float]] = []
    for reference in reading.cells.values():
        table = _table_record(extraction, extraction_dir, reading.page, reference.table)
        if table is None:
            continue
        bbox = _table_cell_bbox(table, reference.row, reference.col)
        if bbox is not None:
            boxes.append(bbox)
    return _union_bbox(boxes)


def _vision_read_binding(
    spec_dir: Path,
    ref: str | None,
    *,
    field: str,
    expected_kind: visionread.VisionKind,
    page: int,
    bbox: tuple[float, float, float, float] | None,
    reading: Reading | None,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    observation_log: Path | None,
    observed_hashes: set[str],
    findings: list[SpecFinding],
) -> tuple[visionread.VisionBatch, visionread.VisionReadItem, object] | None:
    if ref is None:
        findings.append(
            SpecFinding(
                code="vision_read_missing",
                severity="error",
                field=field,
                message="tool-managed vision read reference is required",
                page=page,
            )
        )
        return None
    try:
        batch, item, answers = visionread.load_vision_read(spec_dir, ref)
    except (OSError, ValueError) as exc:
        findings.append(
            SpecFinding(
                code="vision_read_invalid",
                severity="error",
                field=field,
                message=f"vision read could not be loaded: {exc}",
                page=page,
            )
        )
        return None
    if item.control:
        findings.append(
            SpecFinding(
                code="vision_read_invalid",
                severity="error",
                field=field,
                message="control reads cannot be cited as datasheet evidence",
                page=page,
            )
        )
        return None
    if not answers.control_passed:
        findings.append(
            SpecFinding(
                code="vision_read_invalid",
                severity="error",
                field=field,
                message="vision batch control answer did not match its challenge",
                page=page,
            )
        )
        return None
    if batch.pdf_sha256 != extraction.pdf_sha256:
        findings.append(
            SpecFinding(
                code="vision_read_invalid",
                severity="error",
                field=field,
                message="vision batch PDF SHA-256 differs from PartSpec datasheet SHA-256",
                page=page,
            )
        )
        return None
    if answers.status.get(item.read_id) != "ok":
        findings.append(
            SpecFinding(
                code="vision_read_invalid",
                severity="error",
                field=field,
                message="vision answer is unparseable or missing",
                page=page,
            )
        )
        return None
    impression = answers.impressions.get(item.read_id)
    try:
        if not isinstance(impression, str):
            raise ValueError("impression is missing")
        advisory.impression_is_prose(impression)
    except ValueError as exc:
        findings.append(
            SpecFinding(
                code="vision_impression_missing",
                severity="error",
                field=field,
                message=f"vision-read impression is missing or invalid: {exc}",
                page=page,
            )
        )
    if (
        item.prompt != visionread.prompt_for_kind(item.kind)
        or hashlib.sha256(item.prompt.encode("utf-8")).hexdigest() != item.prompt_sha256
    ):
        findings.append(
            SpecFinding(
                code="vision_read_invalid",
                severity="error",
                field=field,
                message="vision prompt differs from the fixed prompt for its kind",
                page=page,
            )
        )
        return None
    if item.kind != expected_kind:
        findings.append(
            SpecFinding(
                code="vision_read_kind_mismatch",
                severity="error",
                field=field,
                message=f"expected {expected_kind!r} vision read, found {item.kind!r}",
                page=page,
            )
        )
        return None
    target_bbox = bbox
    if target_bbox is None and reading is not None:
        target_bbox = _reading_cells_bbox(reading, extraction, extraction_dir)
    if target_bbox is None:
        findings.append(
            SpecFinding(
                code="vision_read_invalid",
                severity="error",
                field=field,
                message="reading has neither a bounding box nor cited table-cell bounds",
                page=page,
            )
        )
        return None
    crop = item.crop_bbox
    if item.page != page or not (
        crop[0] <= target_bbox[0] + 1
        and crop[1] <= target_bbox[1] + 1
        and crop[2] >= target_bbox[2] - 1
        and crop[3] >= target_bbox[3] - 1
    ):
        findings.append(
            SpecFinding(
                code="vision_read_region_mismatch",
                severity="error",
                field=field,
                message="vision crop does not contain the cited reading region within 1 pt",
                page=page,
            )
        )
        return None
    if observation_log is not None and item.image_sha256 not in observed_hashes:
        findings.append(
            SpecFinding(
                code="vision_read_not_observed",
                severity="error",
                field=field,
                message="vision crop SHA-256 is absent from the image observation log",
                page=page,
            )
        )
    normalized = answers.normalized.get(item.read_id)
    if normalized is None:
        findings.append(
            SpecFinding(
                code="vision_read_invalid",
                severity="error",
                field=field,
                message="vision answer normalization is missing",
                page=page,
            )
        )
        return None
    return batch, item, normalized


def _vision_transcription_matches(expected: str, actual: str) -> bool:
    expected_text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", expected))
    actual_text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", actual))
    return bool(expected_text) and (
        re.search(
            rf"(?<![0-9A-Za-z.]){re.escape(expected_text)}(?![0-9A-Za-z])",
            actual_text,
        )
        is not None
    )


def _vision_name_map_matches(expected: dict[str, str], actual: object) -> bool:
    if not isinstance(actual, dict):
        return False
    labels = cast(dict[str, object], actual)
    if set(expected) != set(labels):
        return False
    for number, expected_name in expected.items():
        actual_name = labels.get(number)
        if not isinstance(actual_name, str) or not pinout_oracle.names_equal(
            expected_name, actual_name
        ):
            return False
    return True


def _vision_table_rows(value: object) -> list[list[str]] | None:
    if not isinstance(value, list):
        return None
    rows: list[list[str]] = []
    for raw_row in cast(list[object], value):
        if not isinstance(raw_row, list):
            return None
        row: list[str] = []
        for cell in cast(list[object], raw_row):
            if not isinstance(cell, str):
                return None
            row.append(cell)
        rows.append(row)
    return rows


def _table_value_matches(value: str, cell: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(value)}(?!\w)", cell) is not None


def _table_pairs_equal(
    first: list[tuple[str, str]], second: list[tuple[str, str]]
) -> tuple[bool, list[tuple[str, str]], list[tuple[str, str]]]:
    unmatched = list(first)
    extra: list[tuple[str, str]] = []
    for number, name in second:
        match_index = next(
            (
                index
                for index, candidate in enumerate(unmatched)
                if candidate[0] == number and pinout_oracle.names_equal(candidate[1], name)
            ),
            None,
        )
        if match_index is None:
            extra.append((number, name))
        else:
            unmatched.pop(match_index)
    return not unmatched and not extra, unmatched, extra


def _table_pairs(
    rows: list[list[str]],
    number_col: int,
    name_col: int,
    start_row: int,
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for row in rows[start_row:]:
        number_text = row[number_col] if number_col < len(row) else ""
        name = row[name_col].strip() if name_col < len(row) else ""
        if not name:
            continue
        pairs.extend((number, name) for number in _pin_tokens(number_text))
    return pairs


def _pin_table_vision_checks(
    spec: PartSpec,
    spec_dir: Path,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    observation_log: Path | None,
    observed_hashes: set[str],
    findings: list[SpecFinding],
) -> None:
    ordinary_pins = [pin for pin in spec.pins if pin.reading.alternative_evidence is None]
    if not ordinary_pins:
        return
    table_ref = spec.pin_table
    if table_ref.vision_read is None:
        _vision_read_binding(
            spec_dir,
            None,
            field="pin_table",
            expected_kind="table",
            page=table_ref.page,
            bbox=None,
            reading=None,
            extraction=extraction,
            extraction_dir=extraction_dir,
            observation_log=observation_log,
            observed_hashes=observed_hashes,
            findings=findings,
        )
        return
    table = _table_record(extraction, extraction_dir, table_ref.page, table_ref.table)
    rows = _table_rows(table) if table is not None else None
    citations: dict[int, list[CellRef]] = {}
    boxes: list[tuple[float, float, float, float]] = []
    valid_citations = True
    for pin in ordinary_pins:
        references = list(pin.reading.cells.values()) if pin.reading.cells is not None else []
        if not references:
            valid_citations = False
            continue
        row_indices = {
            reference.row for reference in references if reference.table == table_ref.table
        }
        columns = {reference.col for reference in references if reference.table == table_ref.table}
        if (
            len(row_indices) != 1
            or table_ref.number_col not in columns
            or table_ref.name_col not in columns
            or any(reference.table != table_ref.table for reference in references)
        ):
            valid_citations = False
            continue
        row_index = next(iter(row_indices))
        citations.setdefault(row_index, []).extend(references)
        if table is not None:
            for reference in references:
                bbox = _table_cell_bbox(table, reference.row, reference.col)
                if bbox is not None:
                    boxes.append(bbox)
                else:
                    valid_citations = False
    target_bbox = _union_bbox(boxes)
    if not valid_citations or target_bbox is None or rows is None:
        findings.append(
            SpecFinding(
                code="vision_read_invalid",
                severity="error",
                field="pin_table",
                message="pin readings must cite number and name cells in the bound pin table",
                page=table_ref.page,
            )
        )
        return
    binding = _vision_read_binding(
        spec_dir,
        table_ref.vision_read,
        field="pin_table",
        expected_kind="table",
        page=table_ref.page,
        bbox=target_bbox,
        reading=None,
        extraction=extraction,
        extraction_dir=extraction_dir,
        observation_log=observation_log,
        observed_hashes=observed_hashes,
        findings=findings,
    )
    if binding is None:
        return
    _, _, normalized = binding
    vision_rows = _vision_table_rows(normalized)
    if vision_rows is None:
        findings.append(
            SpecFinding(
                code="vision_read_invalid",
                severity="error",
                field="pin_table",
                message="pin-table vision answer is not a row list",
                page=table_ref.page,
            )
        )
        return
    mechanical_pairs: list[tuple[str, str]] = []
    for row_index in sorted(citations):
        if not 0 <= row_index < len(rows):
            findings.append(
                SpecFinding(
                    code="vision_read_invalid",
                    severity="error",
                    field="pin_table",
                    message=f"pin reading cites missing table row {row_index}",
                    page=table_ref.page,
                )
            )
            return
        number_text = rows[row_index][table_ref.number_col] or ""
        name = rows[row_index][table_ref.name_col] or ""
        mechanical_pairs.extend((number, name) for number in _pin_tokens(number_text))
    ordinary_numbers = {pin.number for pin in ordinary_pins}
    vision_pairs = [
        pair
        for pair in _table_pairs(
            vision_rows,
            table_ref.number_col,
            table_ref.name_col,
            table_ref.header_rows,
        )
        if pair[0] in ordinary_numbers
    ]
    equal, missing, extra = _table_pairs_equal(mechanical_pairs, vision_pairs)
    if equal:
        return
    max_columns = max((len(row) for row in vision_rows), default=0)
    column_shift = any(
        _table_pairs_equal(
            mechanical_pairs,
            [
                pair
                for pair in _table_pairs(
                    vision_rows,
                    table_ref.number_col,
                    column,
                    table_ref.header_rows,
                )
                if pair[0] in ordinary_numbers
            ],
        )[0]
        for column in range(max_columns)
        if column not in (table_ref.number_col, table_ref.name_col)
    )
    findings.append(
        SpecFinding(
            code="vision_table_mismatch",
            severity="error",
            field="pin_table",
            message=(
                f"pin-table vision rows differ from cited mechanical cells; missing={missing}, "
                f"extra={extra}, column_shift={column_shift}"
            ),
            page=table_ref.page,
        )
    )


def _orderable_vision_checks(
    spec: PartSpec,
    spec_dir: Path,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    observation_log: Path | None,
    observed_hashes: set[str],
    findings: list[SpecFinding],
) -> None:
    variants = [
        variant for variant in spec.orderable if variant.reading.alternative_evidence is None
    ]
    if not variants:
        return
    if spec.orderable_vision_read is None:
        page = variants[0].reading.page
        if page is None:
            findings.append(
                SpecFinding(
                    code="orderable_mpn_mismatch",
                    severity="error",
                    field="orderable",
                    message="orderable table reading has no source page",
                )
            )
            return
        _vision_read_binding(
            spec_dir,
            None,
            field="orderable",
            expected_kind="table",
            page=page,
            bbox=None,
            reading=None,
            extraction=extraction,
            extraction_dir=extraction_dir,
            observation_log=observation_log,
            observed_hashes=observed_hashes,
            findings=findings,
        )
        return
    boxes: list[tuple[float, float, float, float]] = []
    pages = {variant.reading.page for variant in variants}
    valid = len(pages) == 1
    page_number = next((page for page in pages if page is not None), spec.pin_table.page)
    for variant in variants:
        if variant.reading.page is None:
            valid = False
            continue
        if variant.reading.page != page_number:
            valid = False
            continue
        table = _table_record(extraction, extraction_dir, page_number, variant.row.table)
        rows = _table_rows(table) if table is not None else None
        if rows is None or not 0 <= variant.row.row < len(rows):
            valid = False
            continue
        for column in range(len(rows[variant.row.row])):
            bbox = _table_cell_bbox(table, variant.row.row, column) if table is not None else None
            if bbox is None:
                valid = False
            else:
                boxes.append(bbox)
    target_bbox = _union_bbox(boxes)
    if not valid or target_bbox is None:
        findings.append(
            SpecFinding(
                code="vision_read_invalid",
                severity="error",
                field="orderable",
                message="orderable variants do not cite one readable table region",
                page=page_number,
            )
        )
        return
    binding = _vision_read_binding(
        spec_dir,
        spec.orderable_vision_read,
        field="orderable",
        expected_kind="table",
        page=page_number,
        bbox=target_bbox,
        reading=None,
        extraction=extraction,
        extraction_dir=extraction_dir,
        observation_log=observation_log,
        observed_hashes=observed_hashes,
        findings=findings,
    )
    if binding is None:
        return
    _, _, normalized = binding
    vision_rows = _vision_table_rows(normalized)
    if vision_rows is None:
        findings.append(
            SpecFinding(
                code="vision_read_invalid",
                severity="error",
                field="orderable",
                message="orderable vision answer is not a row list",
                page=page_number,
            )
        )
        return
    normalized_rows = [
        [re.sub(r"\s+", " ", unicodedata.normalize("NFKC", cell)).casefold() for cell in row]
        for row in vision_rows
    ]
    mismatches: list[str] = []
    for variant in spec.orderable:
        mpn = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", variant.mpn)).casefold()
        package_code = re.sub(
            r"\s+",
            " ",
            unicodedata.normalize("NFKC", variant.package_designator),
        ).casefold()
        matching_rows = [
            row_index
            for row_index, row in enumerate(normalized_rows)
            if any(_table_value_matches(mpn, cell) for cell in row)
        ]
        if len(matching_rows) != 1:
            mismatches.append(f"{variant.mpn}: rows={matching_rows}")
            continue
        row = normalized_rows[matching_rows[0]]
        if not any(_table_value_matches(package_code, cell) for cell in row):
            mismatches.append(f"{variant.mpn}: package code {variant.package_designator!r} missing")
    if mismatches:
        findings.append(
            SpecFinding(
                code="vision_table_mismatch",
                severity="error",
                field="orderable",
                message="orderable vision table does not bind each MPN to one package row: "
                + "; ".join(mismatches),
                page=page_number,
            )
        )


def _table_record(
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page_number: int,
    table_index: int,
) -> dict[str, object] | None:
    page = next((item for item in extraction.pages if item.page == page_number), None)
    if page is None or page.tables_path is None:
        return None
    try:
        value: object = json.loads(
            _resolved(extraction_dir, page.tables_path).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    tables = cast(dict[str, object], value).get("tables")
    if not isinstance(tables, list):
        return None
    table_values = cast(list[object], tables)
    if not 0 <= table_index < len(table_values):
        return None
    table = table_values[table_index]
    if not isinstance(table, dict):
        return None
    table_record = cast(dict[str, object], table)
    rows = table_record.get("rows")
    cells = table_record.get("cells")
    bbox = table_record.get("bbox")
    if not isinstance(rows, list) or _numeric_bbox(bbox) is None:
        return None
    if not isinstance(cells, list):
        return None
    return table_record


def _numeric_bbox(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list):
        return None
    coordinates: list[float] = []
    for item in cast(list[object], value):
        if not isinstance(item, (int, float)):
            return None
        coordinates.append(float(item))
    if len(coordinates) != 4:
        return None
    return coordinates[0], coordinates[1], coordinates[2], coordinates[3]


def _table_rows(table: dict[str, object]) -> list[list[str | None]] | None:
    value = table.get("rows")
    if not isinstance(value, list):
        return None
    rows: list[list[str | None]] = []
    for row in cast(list[object], value):
        if not isinstance(row, list):
            return None
        cells: list[str | None] = []
        for cell in cast(list[object], row):
            cells.append(cell if isinstance(cell, str) else None)
        rows.append(cells)
    return rows


def _table_cell_bbox(
    table: dict[str, object], row_index: int, col_index: int
) -> tuple[float, float, float, float] | None:
    value = table.get("cells")
    if not isinstance(value, list):
        return None
    table_cells = cast(list[object], value)
    if not 0 <= row_index < len(table_cells):
        return None
    row = table_cells[row_index]
    if not isinstance(row, list):
        return None
    row_cells = cast(list[object], row)
    if not 0 <= col_index < len(row_cells):
        return None
    return _numeric_bbox(row_cells[col_index])


def _table_cell_text(rows: list[list[str | None]], row: int, col: int) -> str | None:
    if not 0 <= row < len(rows) or not 0 <= col < len(rows[row]):
        return None
    return rows[row][col]


def _poppler_cell_text(
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page: PageExtraction,
    cell_bbox: tuple[float, float, float, float] | None,
) -> str | None:
    if cell_bbox is None:
        return None
    words = _lane_words(extraction, extraction_dir, page.page, "poppler")
    visible, _ = _visible_words(words, page, extraction_dir)
    selected = _words_in_bbox(visible, cell_bbox)
    selected.sort(key=lambda word: ((word.top + word.bottom) / 2, word.x0))
    return " ".join(word.text for word in selected)


def _cell_lane_texts(
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page: PageExtraction,
    table: dict[str, object],
    rows: list[list[str | None]],
    row_index: int,
    col_index: int,
    *,
    field: str,
    findings: list[SpecFinding],
    missing_code: str = "table_lane_disagreement",
) -> tuple[str | None, str | None]:
    plumber_text = _table_cell_text(rows, row_index, col_index)
    cell_bbox = _table_cell_bbox(table, row_index, col_index)
    poppler_text = _poppler_cell_text(extraction, extraction_dir, page, cell_bbox)
    if plumber_text is None or poppler_text is None:
        findings.append(
            SpecFinding(
                code=missing_code,
                severity="error",
                field=field,
                message="table cell text or its visible Poppler cell region is unavailable",
                page=page.page,
            )
        )
        return plumber_text, poppler_text
    if _normalise_text(plumber_text).casefold() != _normalise_text(poppler_text).casefold():
        findings.append(
            SpecFinding(
                code="table_lane_disagreement",
                severity="error",
                field=field,
                message="pdfplumber cell text differs from visible Poppler words in the cell",
                page=page.page,
            )
        )
    return plumber_text, poppler_text


def _cell_numeric_value(text: str | None) -> float | None:
    if text is None:
        return None
    numbers = _numeric_tokens(text)
    if len(numbers) != 1:
        return None
    return float(numbers[0])


def _header_rows(rows: list[list[str | None]]) -> int:
    header_words = {
        "DIMENSION",
        "DIMENSIONS",
        "SYMBOL",
        "MIN",
        "NOM",
        "MAX",
        "REF",
        "REFERENCE",
        "PARAMETER",
        "MILLIMETERS",
        "MILLIMETRES",
        "INCHES",
        "INCH",
        "NOTE",
        "NOTES",
    }
    for row_index, row in enumerate(rows):
        first = next((cell.strip() for cell in row if cell and cell.strip()), "")
        if first and first.upper().rstrip(":") not in header_words:
            return row_index
    return len(rows)


def _dimension_cell_checks(
    dimension: Dimension,
    reading: Reading,
    field: str,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page: PageExtraction,
    findings: list[SpecFinding],
) -> None:
    if reading.cells is None or reading.page is None:
        return
    label = dimension.label
    for key, cell_ref in reading.cells.items():
        table = _table_record(extraction, extraction_dir, reading.page, cell_ref.table)
        if table is None:
            findings.append(
                SpecFinding(
                    code="cell_value_mismatch",
                    severity="error",
                    field=field,
                    message=f"re-derived table {cell_ref.table} is unavailable",
                    page=reading.page,
                )
            )
            continue
        rows = _table_rows(table)
        if rows is None:
            findings.append(
                SpecFinding(
                    code="cell_value_mismatch",
                    severity="error",
                    field=field,
                    message="re-derived table rows are unreadable",
                    page=reading.page,
                )
            )
            continue
        cell_bbox = _table_cell_bbox(table, cell_ref.row, cell_ref.col)
        if (
            cell_bbox is None
            or reading.bbox is None
            or not (
                reading.bbox[0] <= cell_bbox[0]
                and reading.bbox[1] <= cell_bbox[1]
                and reading.bbox[2] >= cell_bbox[2]
                and reading.bbox[3] >= cell_bbox[3]
            )
        ):
            findings.append(
                SpecFinding(
                    code="cell_value_mismatch",
                    severity="error",
                    field=field,
                    message="reading bounding box does not cover the referenced table cell",
                    page=reading.page,
                )
            )
        plumber_text, poppler_text = _cell_lane_texts(
            extraction,
            extraction_dir,
            page,
            table,
            rows,
            cell_ref.row,
            cell_ref.col,
            field=field,
            findings=findings,
            missing_code="cell_value_mismatch",
        )
        expected = getattr(dimension, key)
        if (
            expected is None
            or _cell_numeric_value(plumber_text) is None
            or not _number_close(_cell_numeric_value(plumber_text), expected)
            or _cell_numeric_value(poppler_text) is None
            or not _number_close(_cell_numeric_value(poppler_text), expected)
        ):
            findings.append(
                SpecFinding(
                    code="cell_value_mismatch",
                    severity="error",
                    field=field,
                    message=f"table {key} cell does not equal the PartSpec value",
                    page=reading.page,
                )
            )

        headers = _header_rows(rows)
        header_text = " ".join(
            cell or ""
            for row in rows[:headers]
            for index, cell in enumerate(row)
            if index == cell_ref.col
        )
        if key.upper() not in header_text.upper():
            findings.append(
                SpecFinding(
                    code="cell_header_mismatch",
                    severity="error",
                    field=field,
                    message=f"column header does not contain {key.upper()}",
                    page=reading.page,
                )
            )
        if label is None or not 0 <= cell_ref.row < len(rows):
            row_label = None
        else:
            row_label = next(
                (cell.strip() for cell in rows[cell_ref.row] if cell is not None and cell.strip()),
                None,
            )
        if label is None or row_label != label:
            findings.append(
                SpecFinding(
                    code="cell_label_mismatch",
                    severity="error",
                    field=field,
                    message="table row label does not equal Dimension.label",
                    page=reading.page,
                )
            )
        whole_header = " ".join(cell or "" for row in rows[:headers] for cell in row)
        if re.search(
            r"\b(?:inch(?:es)?|in\.?|mil(?:s)?)", whole_header, re.IGNORECASE
        ) and not re.search(r"\b(?:mm|millimet)\w*\b", header_text, re.IGNORECASE):
            findings.append(
                SpecFinding(
                    code="cell_unit_mismatch",
                    severity="error",
                    field=field,
                    message="bound table column must identify millimeter units",
                    page=reading.page,
                )
            )


def _pin_tokens(text: str | None) -> list[str]:
    if text is None:
        return []
    normalized = text.replace("\u2013", "-").replace("\u2014", "-")
    result: list[str] = []
    for token in _TOKEN_SPLIT.split(normalized.strip()):
        if not token:
            continue
        if match := re.fullmatch(r"(\d+)-(\d+)", token):
            start, end = int(match.group(1)), int(match.group(2))
            if end < start or end - start > 500:
                continue
            result.extend(str(number) for number in range(start, end + 1))
        elif re.fullmatch(r"(?:\d+|[A-Za-z]{1,2}\d+)", token):
            result.append(token)
    return result


def _pin_table_checks(
    spec: PartSpec,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page: PageExtraction | None,
    findings: list[SpecFinding],
) -> None:
    if not any(pin.reading.alternative_evidence is None for pin in spec.pins):
        return
    field = "pin_table"
    table_ref = spec.pin_table
    table = _table_record(extraction, extraction_dir, table_ref.page, table_ref.table)
    if page is None or table is None:
        findings.append(
            SpecFinding(
                code="pin_table_missing",
                severity="error",
                field=field,
                message="bound re-derived pin table is missing or unreadable",
                page=table_ref.page,
            )
        )
        return
    rows = _table_rows(table)
    if rows is None or not rows or table_ref.header_rows >= len(rows):
        findings.append(
            SpecFinding(
                code="pin_table_missing",
                severity="error",
                field=field,
                message="bound re-derived pin table has no readable body rows",
                page=table_ref.page,
            )
        )
        return

    max_columns = max((len(row) for row in rows), default=0)
    if max(table_ref.number_col, table_ref.name_col) >= max_columns:
        findings.append(
            SpecFinding(
                code="pin_table_missing",
                severity="error",
                field=field,
                message="pin table number/name column is outside the table",
                page=table_ref.page,
            )
        )
        return

    body_rows = rows[table_ref.header_rows :]
    number_like_columns: list[int] = []
    for col in range(max_columns):
        nonempty: list[str] = []
        for row in body_rows:
            if col >= len(row):
                continue
            value = row[col]
            if value is not None and value.strip():
                nonempty.append(value)
        if nonempty and sum(bool(_pin_tokens(value)) for value in nonempty) / len(nonempty) >= 0.8:
            number_like_columns.append(col)
    if len(number_like_columns) > 1:
        if table_ref.column_designator is None:
            findings.append(
                SpecFinding(
                    code="pin_table_column_ambiguous",
                    severity="error",
                    field=field,
                    message=f"multiple number-like columns exist: {number_like_columns}",
                    page=table_ref.page,
                )
            )
        else:
            header_parts: list[str] = []
            for index in range(min(table_ref.header_rows, len(rows))):
                if table_ref.number_col >= len(rows[index]):
                    continue
                value = rows[index][table_ref.number_col]
                if value is not None:
                    header_parts.append(value)
            header = " ".join(header_parts)
            if table_ref.column_designator.casefold() not in header.casefold():
                findings.append(
                    SpecFinding(
                        code="pin_table_column_mismatch",
                        severity="error",
                        field=field,
                        message=(
                            "column designator is absent from the selected number-column header"
                        ),
                        page=table_ref.page,
                    )
                )
            if any(
                variant.package_designator.casefold() != table_ref.column_designator.casefold()
                for variant in spec.orderable
            ):
                findings.append(
                    SpecFinding(
                        code="pin_table_column_mismatch",
                        severity="error",
                        field=field,
                        message="column designator differs from an orderable package designator",
                        page=table_ref.page,
                    )
                )

    table_pairs: Counter[tuple[str, str]] = Counter()
    exposed_rows: list[str] = []
    for row_index in range(table_ref.header_rows, len(rows)):
        number_text, number_poppler = _cell_lane_texts(
            extraction,
            extraction_dir,
            page,
            table,
            rows,
            row_index,
            table_ref.number_col,
            field=field,
            findings=findings,
            missing_code="pin_table_missing",
        )
        name_text, name_poppler = _cell_lane_texts(
            extraction,
            extraction_dir,
            page,
            table,
            rows,
            row_index,
            table_ref.name_col,
            field=field,
            findings=findings,
            missing_code="pin_table_missing",
        )
        number_values = _pin_tokens(number_text)
        number_poppler_values = _pin_tokens(number_poppler)
        name_value = _normalise_text(name_text or "")
        name_poppler_value = _normalise_text(name_poppler or "")
        if number_values != number_poppler_values or name_value != name_poppler_value:
            findings.append(
                SpecFinding(
                    code="table_lane_disagreement",
                    severity="error",
                    field=field,
                    message="pin number or name differs between table lanes",
                    page=table_ref.page,
                )
            )
        if not number_values and re.search(
            r"thermal pad|exposed pad|powerpad|\bEP\b", name_value, re.IGNORECASE
        ):
            exposed_rows.append(name_value)
            exposed_index = len(exposed_rows) - 1
            if exposed_index < len(spec.package.all_exposed_pads):
                exposed_pad = spec.package.all_exposed_pads[exposed_index]
                table_pairs[(exposed_pad.number, name_value)] += 1
            continue
        if not number_values or not name_value:
            continue
        table_pairs.update((number, name_value) for number in number_values)

    if len(exposed_rows) != len(spec.package.all_exposed_pads):
        findings.append(
            SpecFinding(
                code="exposed_pad_table_mismatch",
                severity="error",
                field=field,
                message="exposed-pad table row presence differs from PackageSpec",
                page=table_ref.page,
            )
        )
    alternative_numbers = {
        pin.number for pin in spec.pins if pin.reading.alternative_evidence is not None
    }
    table_pairs = Counter(
        {pair: count for pair, count in table_pairs.items() if pair[0] not in alternative_numbers}
    )
    ordinary_pins = [pin for pin in spec.pins if pin.reading.alternative_evidence is None]
    expected_pairs = Counter((pin.number, pin.name) for pin in ordinary_pins)
    if table_pairs != expected_pairs:
        missing = sorted((expected_pairs - table_pairs).elements())
        extra = sorted((table_pairs - expected_pairs).elements())
        findings.append(
            SpecFinding(
                code="pin_table_bijection",
                severity="error",
                field=field,
                message=f"pin table differs from PartSpec; missing={missing}, extra={extra}",
                page=table_ref.page,
            )
        )
    signal_numbers = {
        number for number, _ in table_pairs if number not in spec.package.auxiliary_pad_numbers
    }
    spec_signal_numbers = [
        pin.number for pin in ordinary_pins if pin.number not in spec.package.auxiliary_pad_numbers
    ]
    expected_signal_numbers = (
        expected_signal_pin_numbers(spec.package)
        if not alternative_numbers
        else set(spec_signal_numbers)
    )
    if (
        spec.package.ball_grid is not None
        or all(number.isdigit() for number in spec_signal_numbers)
    ) and (signal_numbers != expected_signal_numbers):
        findings.append(
            SpecFinding(
                code="pin_numbering_incomplete",
                severity="error",
                field=field,
                message="numeric pin numbers do not cover 1 through package.pin_count",
                page=table_ref.page,
            )
        )


def _orderable_row_checks(
    spec: PartSpec,
    variant: OrderableVariant,
    index: int,
    extraction: DatasheetExtraction,
    extraction_dir: Path,
    page: PageExtraction | None,
    findings: list[SpecFinding],
) -> None:
    field = f"orderable[{index}]"
    page_number = variant.reading.page
    if page_number is None:
        findings.append(
            SpecFinding(
                code="orderable_mpn_mismatch",
                severity="error",
                field=field,
                message="orderable row has no source page",
            )
        )
        return
    if page is None:
        findings.append(
            SpecFinding(
                code="orderable_mpn_mismatch",
                severity="error",
                field=field,
                message="orderable table page is unavailable",
                page=page_number,
            )
        )
        return
    table = _table_record(extraction, extraction_dir, page_number, variant.row.table)
    if table is None or (rows := _table_rows(table)) is None or variant.row.row >= len(rows):
        findings.append(
            SpecFinding(
                code="orderable_mpn_mismatch",
                severity="error",
                field=field,
                message="bound re-derived orderable row is missing or unreadable",
                page=page_number,
            )
        )
        return
    plumber_cells = [cell or "" for cell in rows[variant.row.row]]
    poppler_cells: list[str] = []
    for col_index in range(len(plumber_cells)):
        plumber_text, poppler_text = _cell_lane_texts(
            extraction,
            extraction_dir,
            page,
            table,
            rows,
            variant.row.row,
            col_index,
            field=field,
            findings=findings,
            missing_code="orderable_mpn_mismatch",
        )
        if plumber_text is not None:
            plumber_cells[col_index] = plumber_text.strip()
        poppler_cells.append((poppler_text or "").strip())
    if variant.mpn not in plumber_cells or variant.mpn not in poppler_cells:
        findings.append(
            SpecFinding(
                code="orderable_mpn_mismatch",
                severity="error",
                field=field,
                message="exact orderable MPN is absent from the bound row",
                page=page_number,
            )
        )
    package_pin_cell = re.compile(
        rf"\s*{re.escape(variant.package_designator)}\s*\|\s*{variant.pin_count}\s*"
    )
    if (
        variant.package_designator not in plumber_cells
        and not any(package_pin_cell.fullmatch(cell) for cell in plumber_cells)
    ) or (
        variant.package_designator not in poppler_cells
        and not any(package_pin_cell.fullmatch(cell) for cell in poppler_cells)
    ):
        findings.append(
            SpecFinding(
                code="orderable_designator_mismatch",
                severity="error",
                field=field,
                message="exact package designator is absent from the bound row",
                page=page_number,
            )
        )
    expected_row_pin_counts = set(
        range(
            spec.package.pin_count,
            spec.package.pin_count + len(spec.package.auxiliary_pad_numbers) + 1,
        )
    )
    if (
        (
            str(variant.pin_count) not in plumber_cells
            and not any(
                re.fullmatch(
                    rf"\s*{re.escape(variant.package_designator)}\s*\|\s*"
                    rf"{variant.pin_count}\s*",
                    cell,
                )
                for cell in plumber_cells
            )
        )
        or (
            str(variant.pin_count) not in poppler_cells
            and not any(
                re.fullmatch(
                    rf"\s*{re.escape(variant.package_designator)}\s*\|\s*"
                    rf"{variant.pin_count}\s*",
                    cell,
                )
                for cell in poppler_cells
            )
        )
        or variant.pin_count not in expected_row_pin_counts
    ):
        findings.append(
            SpecFinding(
                code="orderable_pin_count_mismatch",
                severity="error",
                field=field,
                message="pin count is absent from the bound row or differs from PackageSpec",
                page=page_number,
            )
        )


def _latest_valid_response(
    responses: list[humanrequest.HumanResponse],
) -> humanrequest.HumanResponse | None:
    valid = [response for response in responses if response.valid]
    return max(
        valid,
        key=lambda response: (
            response.recorded_at.isoformat() if response.recorded_at is not None else "",
            response.event_sha256 or "",
        ),
        default=None,
    )


def _has_stale_response(responses: list[humanrequest.HumanResponse]) -> bool:
    stale_reasons = (
        "event hash mismatch",
        "filename hash mismatch",
        "request hash changed",
        "stored request hash is missing or changed",
        "pointer timestamp is invalid",
    )
    return any(
        any(marker in reason for marker in stale_reasons)
        for response in responses
        for reason in response.reasons
    )


def _substitute_permission(
    spec: PartSpec,
    spec_path: Path,
    findings: list[SpecFinding],
) -> humanrequest.SubstitutePermit | None:
    substitution = spec.substitution
    if substitution is None:
        return None
    project_root = confidential.project_root_for(spec_path)
    request_path = humanrequest.find_request_path(project_root, substitution.request_id)
    if not request_path.is_file():
        findings.append(
            SpecFinding(
                code="substitute_permission_missing",
                severity="error",
                field="substitution",
                message="the referenced substitute permission request is missing",
            )
        )
        return None
    try:
        request = humanrequest.load_request(request_path)
    except humanrequest.HumanRequestError as exc:
        findings.append(
            SpecFinding(
                code="substitute_permission_stale",
                severity="error",
                field="substitution",
                message=f"the substitute permission request is invalid: {exc}",
            )
        )
        return None
    details = request.details
    if request.kind != "substitute_permission" or not isinstance(
        details, humanrequest.SubstitutePermissionDetails
    ):
        findings.append(
            SpecFinding(
                code="substitute_permission_missing",
                severity="error",
                field="substitution",
                message="the referenced HumanRequest is not a substitute permission request",
            )
        )
        return None
    if (
        request.subject.mpn.casefold() != spec.mpn.casefold()
        or details.target_mpn.casefold() != spec.mpn.casefold()
        or details.substitute_mpn.casefold() != substitution.substitute_mpn.casefold()
    ):
        findings.append(
            SpecFinding(
                code="substitute_permission_stale",
                severity="error",
                field="substitution",
                message="substitute permission target or substitute MPN no longer matches",
            )
        )
        return None
    if details.substitute_datasheet_sha256 != spec.datasheet.sha256:
        findings.append(
            SpecFinding(
                code="substitute_permission_stale",
                severity="error",
                field="datasheet.sha256",
                message="substitute datasheet SHA-256 differs from the permission request",
            )
        )
        return None
    responses = humanrequest.load_responses(project_root, request)
    response = _latest_valid_response(responses)
    if response is None or response.decision != "grant":
        code = (
            "substitute_permission_stale"
            if _has_stale_response(responses)
            else "substitute_permission_missing"
        )
        findings.append(
            SpecFinding(
                code=code,
                severity="error",
                field="substitution",
                message="no current trusted substitute permission grant is available",
            )
        )
        return None
    event_path = response.event_path
    event_sha256 = response.event_sha256
    granted_at = response.recorded_at
    if granted_at is None and event_path is not None:
        try:
            granted_at = datetime.fromtimestamp(event_path.stat().st_mtime, UTC)
        except OSError:
            granted_at = None
    if event_path is None or event_sha256 is None or granted_at is None:
        findings.append(
            SpecFinding(
                code="substitute_permission_stale",
                severity="error",
                field="substitution",
                message="the substitute permission response is missing its trusted event binding",
            )
        )
        return None
    granted_scope = cast(
        list[humanrequest.SubstituteScope],
        [item.strip() for item in response.fields.get("scope", "").split(",") if item.strip()],
    )
    try:
        return humanrequest.SubstitutePermit(
            request_id=request.request_id,
            request_sha256=request.request_sha256,
            target_mpn=details.target_mpn,
            substitute_mpn=details.substitute_mpn,
            target_source_sha256=details.target_source_sha256,
            substitute_datasheet_sha256=details.substitute_datasheet_sha256,
            granted_scope=granted_scope,
            unverifiable_fields=details.unverifiable_fields,
            reviewer=response.reviewer,
            event_path=event_path,
            event_sha256=event_sha256,
            granted_at=granted_at,
        )
    except ValueError as exc:
        findings.append(
            SpecFinding(
                code="substitute_permission_stale",
                severity="error",
                field="substitution",
                message=f"the substitute permission grant cannot be derived: {exc}",
            )
        )
        return None


def _scope_for_field(field: str) -> humanrequest.SubstituteScope | None:
    if field.startswith("package."):
        return "package_dimensions"
    if field.startswith("land_pattern."):
        return "land_pattern"
    if field.startswith("pinout."):
        return "pinout"
    if field.startswith("pins[") or field == "pin_table":
        return "pin_table"
    if field.startswith("orderable["):
        return "orderable"
    return None


def _pointer_for_field(field: str) -> str:
    if field == "package.pin1_reading":
        return "/package/pin1_corner"
    if field.startswith("pinout."):
        return "/pinout"
    if field.startswith("pins["):
        index = field.removeprefix("pins[").split("]", 1)[0]
        return f"/pins/{index}"
    if field.startswith("orderable["):
        index = field.removeprefix("orderable[").split("]", 1)[0]
        return f"/orderable/{index}"
    tokens = field.removesuffix(".reading").split(".")
    escaped = [token.replace("~", "~0").replace("/", "~1") for token in tokens]
    return "/" + "/".join(escaped)


def _pointer_is_covered(pointer: str, covers: set[str]) -> bool:
    return any(pointer == item or pointer.startswith(item.rstrip("/") + "/") for item in covers)


def _photo_alternative_has_vision(
    reading: Reading,
    field: str,
    spec_dir: Path,
    file_hashes: set[str],
    findings: list[SpecFinding],
) -> bool:
    if reading.vision is None or reading.vision_record is None or reading.vision_read is None:
        findings.append(
            SpecFinding(
                code="alternative_evidence_missing",
                severity="error",
                field=field,
                message="photo evidence requires a hash-bound vision read and impression",
                page=reading.page,
            )
        )
        return False
    record_path = _resolved(spec_dir, reading.vision_record)
    try:
        record = advisory.AdvisoryResult.model_validate(
            json.loads(record_path.read_text(encoding="utf-8"))
        )
        detail = advisory.parse_visual_review(record)
        batch, item, answers = visionread.load_vision_read(spec_dir, reading.vision_read)
    except (OSError, json.JSONDecodeError, ValueError):
        detail = None
        item = None
        answers = None
        batch = None
    if (
        detail is None
        or item is None
        or answers is None
        or batch is None
        or detail.image_sha256 not in file_hashes
        or item.image_sha256 not in file_hashes
        or item.control
        or not answers.control_passed
        or answers.status.get(item.read_id) != "ok"
        or not advisory.impression_is_prose(detail.impression)
        or not advisory.impression_is_prose(answers.impressions.get(item.read_id, ""))
    ):
        findings.append(
            SpecFinding(
                code="alternative_evidence_missing",
                severity="error",
                field=field,
                message="photo evidence lacks a valid impression bound to the cited image",
                page=reading.page,
            )
        )
        return False
    normalized = answers.normalized.get(item.read_id)
    if not isinstance(normalized, str) or not _vision_transcription_matches(
        reading.vision,
        normalized,
    ):
        findings.append(
            SpecFinding(
                code="alternative_evidence_scope_exceeded",
                severity="error",
                field=field,
                message="photo vision answer does not support the authored reading",
                page=reading.page,
            )
        )
        return False
    return True


def _alternative_evidence_check(
    reading: Reading,
    field: str,
    spec_dir: Path,
    project_root: Path,
    pointer: str,
    findings: list[SpecFinding],
    *,
    confidential_data: bool,
) -> bool:
    request_id = reading.alternative_evidence
    if request_id is None:
        return False
    request_path = humanrequest.find_request_path(project_root, request_id)
    if not request_path.is_file():
        findings.append(
            SpecFinding(
                code="alternative_evidence_missing",
                severity="error",
                field=field,
                message="the cited alternative evidence request is missing",
                page=reading.page,
            )
        )
        return False
    try:
        request = humanrequest.load_request(request_path)
    except humanrequest.HumanRequestError as exc:
        findings.append(
            SpecFinding(
                code="alternative_evidence_stale",
                severity="error",
                field=field,
                message=f"the alternative evidence request is invalid: {exc}",
                page=reading.page,
            )
        )
        return False
    details = request.details
    if request.kind != "alternative_evidence" or not isinstance(
        details, humanrequest.AlternativeEvidenceDetails
    ):
        findings.append(
            SpecFinding(
                code="alternative_evidence_missing",
                severity="error",
                field=field,
                message="the cited request is not an alternative evidence request",
                page=reading.page,
            )
        )
        return False
    responses = humanrequest.load_responses(project_root, request)
    response = _latest_valid_response(responses)
    if response is None or response.decision != "grant":
        code = (
            "alternative_evidence_stale"
            if _has_stale_response(responses)
            else "alternative_evidence_missing"
        )
        findings.append(
            SpecFinding(
                code=code,
                severity="error",
                field=field,
                message="no current trusted alternative evidence grant is available",
                page=reading.page,
            )
        )
        return False
    granted_covers = {
        item.strip() for item in response.fields.get("covers", "").split(",") if item.strip()
    }
    if not _pointer_is_covered(pointer, set(details.covers)) or not _pointer_is_covered(
        pointer,
        granted_covers,
    ):
        findings.append(
            SpecFinding(
                code="alternative_evidence_scope_exceeded",
                severity="error",
                field=field,
                message=f"alternative evidence does not grant coverage for {pointer}",
                page=reading.page,
            )
        )
        return False
    file_hashes: set[str] = set()
    for item in details.files:
        path = Path(item.path)
        if not path.is_absolute():
            path = project_root / path
        try:
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            actual_hash = ""
        if actual_hash != item.sha256:
            findings.append(
                SpecFinding(
                    code="alternative_evidence_stale",
                    severity="error",
                    field=field,
                    message=f"alternative evidence file is missing or changed: {item.path}",
                    page=reading.page,
                )
            )
            return False
        file_hashes.add(actual_hash)
        if confidential_data:
            private_root = confidential.ensure_confidential_store(project_root)
            try:
                path.resolve().relative_to(private_root.resolve())
            except ValueError:
                findings.append(
                    SpecFinding(
                        code="confidential_artifact_outside_store",
                        severity="error",
                        field=field,
                        message="confidential alternative evidence is outside .confidential/",
                        page=reading.page,
                    )
                )
                return False
    if details.evidence_kind == "photo" and not _photo_alternative_has_vision(
        reading,
        field,
        spec_dir,
        file_hashes,
        findings,
    ):
        return False
    findings.append(
        SpecFinding(
            code="alternative_evidence_used",
            severity="warning",
            field=field,
            message=f"alternative evidence supports {pointer}; it is not deterministic",
            page=reading.page,
        )
    )
    return True


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
    auxiliary_numbers = package.auxiliary_pad_numbers
    signal_pin_count = sum(pin.number not in auxiliary_numbers for pin in spec.pins)
    if signal_pin_count != package.pin_count:
        findings.append(
            SpecFinding(
                code="pin_count_mismatch",
                severity="error",
                field="package.pin_count",
                message=f"expected {package.pin_count} signal pins, found {signal_pin_count}",
            )
        )
    for index, exposed_pad in enumerate(package.all_exposed_pads):
        if exposed_pad.number in number_counts:
            continue
        findings.append(
            SpecFinding(
                code="exposed_pad_unmapped",
                severity="error",
                field=f"package.exposed_pads[{index}].number",
                message="exposed pad number is not present in the pin list",
            )
        )
    if package.tab is not None and package.tab.number not in number_counts:
        findings.append(
            SpecFinding(
                code="tab_unmapped",
                severity="error",
                field="package.tab.number",
                message="tab number is not present in the pin list",
            )
        )
    if package.family in {
        "no_lead_quad",
        "no_lead_dual",
        "gullwing_quad",
        "gullwing_dual",
        "chip",
        "sot223",
        "tabbed_dpak",
        "sod",
        "bga",
    }:
        expected_numbers = expected_signal_pin_numbers(package)
        actual_numbers = {pin.number for pin in spec.pins if pin.number not in auxiliary_numbers}
        if actual_numbers != expected_numbers:
            findings.append(
                SpecFinding(
                    code="pin_numbering_incomplete",
                    severity="error",
                    field="pins",
                    message=(
                        "signal pin numbers differ from populated package positions; "
                        f"missing={sorted(expected_numbers - actual_numbers)}, "
                        f"extra={sorted(actual_numbers - expected_numbers)}"
                    ),
                )
            )
    expected_mpn = spec.substitution.substitute_mpn if spec.substitution is not None else spec.mpn
    matching_mpn = [
        variant for variant in spec.orderable if variant.mpn.casefold() == expected_mpn.casefold()
    ]
    if len(matching_mpn) != 1:
        findings.append(
            SpecFinding(
                code="package_variant_unbound",
                severity="error",
                field="orderable",
                message="the PartSpec MPN must match exactly one orderable variant",
            )
        )
    quad_family = package.family in ("no_lead_quad", "gullwing_quad")
    dual_family = package.family in ("no_lead_dual", "gullwing_dual")
    physical_pin_count = package.pin_count + len(package.missing_pins)
    side_counts: tuple[int, int, int, int] | None = None
    if quad_family:
        if package.pins_per_side is None:
            if physical_pin_count % 4 == 0:
                pins_per_side = physical_pin_count // 4
                side_counts = (pins_per_side, pins_per_side, pins_per_side, pins_per_side)
        else:
            side_counts = package.pins_per_side
        if side_counts is None or sum(side_counts) != physical_pin_count:
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
    elif dual_family and physical_pin_count % 2:
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
            pins_per_side = physical_pin_count // 2
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
    if package.family in {"sot223", "tabbed_dpak"} and package.pitch is not None:
        pitch = package.pitch.nom
        if pitch is None and package.pitch.min is not None and package.pitch.max is not None:
            pitch = (package.pitch.min + package.pitch.max) / 2
        body_width = _dimension_upper(package.body_width)
        lead_slots = package.pin_count + len(package.missing_pins)
        if (
            pitch is not None
            and body_width is not None
            and lead_slots > 1
            and pitch * (lead_slots - 1) >= body_width
        ):
            findings.append(
                SpecFinding(
                    code="pitch_exceeds_body",
                    severity="error",
                    field="package.pitch",
                    message="lead pitch span must be smaller than the body width",
                )
            )
    for index, exposed_pad in enumerate(package.all_exposed_pads):
        body_length = _dimension_upper(package.body_length)
        body_width = _dimension_upper(package.body_width)
        pad_length = _dimension_upper(exposed_pad.length)
        pad_width = _dimension_upper(exposed_pad.width)
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
                    field=f"package.exposed_pads[{index}]",
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
    if (
        spec.package.family
        in {
            "no_lead_quad",
            "no_lead_dual",
            "gullwing_quad",
            "gullwing_dual",
        }
        and spec.pinout is None
    ):
        findings.append(
            SpecFinding(
                code="pinout_missing",
                severity="error",
                field="pinout",
                message=f"{spec.package.family} packages require a pinout drawing",
            )
        )
    spec_dir = spec_path.resolve().parent
    extraction_dir = extraction_path.resolve().parent
    project_root = confidential.project_root_for(spec_path)
    pdf_path = _resolved(spec_dir, spec.datasheet.path)
    stored_extraction_path = _resolved(spec_dir, spec.datasheet.extraction_path)
    actual_spec_sha = part_spec_sha256(spec_path)
    if spec.datasheet.confidential:
        project_root = confidential.project_root_for(spec_path)
        private_root = confidential.ensure_confidential_store(project_root)
        for field, artifact_path in (
            ("datasheet.path", pdf_path),
            ("datasheet.extraction_path", stored_extraction_path),
        ):
            try:
                artifact_path.resolve().relative_to(private_root.resolve())
            except ValueError:
                findings.append(
                    SpecFinding(
                        code="confidential_artifact_outside_store",
                        severity="error",
                        field=field,
                        message=(
                            "confidential datasheet artifacts must be stored under "
                            + str(private_root)
                        ),
                    )
                )
    try:
        actual_extraction_sha = hashlib.sha256(extraction_path.read_bytes()).hexdigest()
    except OSError as exc:
        actual_extraction_sha = ""
        findings.append(
            SpecFinding(
                code="evidence_missing",
                severity="error",
                field="datasheet.extraction_path",
                message=f"stored extraction is unavailable: {exc}",
            )
        )
    if spec.datasheet.sha256 != extraction.pdf_sha256:
        findings.append(
            SpecFinding(
                code="datasheet_sha_mismatch",
                severity="error",
                field="datasheet.sha256",
                message="PartSpec datasheet SHA-256 differs from the extraction",
            )
        )
    pdf_sha256: str | None = None
    if not pdf_path.is_file():
        findings.append(
            SpecFinding(
                code="datasheet_missing",
                severity="error",
                field="datasheet.path",
                message=f"datasheet file is missing: {pdf_path}",
            )
        )
    else:
        try:
            pdf_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        except OSError as exc:
            findings.append(
                SpecFinding(
                    code="datasheet_missing",
                    severity="error",
                    field="datasheet.path",
                    message=f"datasheet file cannot be read: {exc}",
                )
            )
        if pdf_sha256 is not None and pdf_sha256 != spec.datasheet.sha256:
            findings.append(
                SpecFinding(
                    code="datasheet_sha_mismatch",
                    severity="error",
                    field="datasheet.sha256",
                    message="PartSpec datasheet SHA-256 differs from the PDF file",
                )
            )

    stored_pages = {page.page: page for page in extraction.pages}
    readings = _all_readings(spec)
    orderable_by_field = {
        f"orderable[{index}]": (index, variant) for index, variant in enumerate(spec.orderable)
    }
    pin_by_field = {f"pins[{index}]": (index, pin) for index, pin in enumerate(spec.pins)}
    substitute_permit = _substitute_permission(spec, spec_path, findings)
    alternative_fields: set[str] = set()
    alternative_reading_fields = {
        field for field, reading, _ in readings if reading.alternative_evidence is not None
    }
    for field, reading, _ in readings:
        if reading.alternative_evidence is None:
            continue
        if _alternative_evidence_check(
            reading,
            field,
            spec_dir,
            project_root,
            _pointer_for_field(field),
            findings,
            confidential_data=spec.datasheet.confidential,
        ):
            alternative_fields.add(field)
    if spec.substitution is not None and substitute_permit is not None:
        granted_scope = set(substitute_permit.granted_scope)
        if not set(spec.substitution.scope).issubset(granted_scope):
            findings.append(
                SpecFinding(
                    code="substitute_scope_exceeded",
                    severity="error",
                    field="substitution.scope",
                    message="PartSpec substitution scope exceeds the granted permission",
                )
            )
        for field, reading, _ in readings:
            scope = _scope_for_field(field)
            if scope is not None and scope not in granted_scope and field not in alternative_fields:
                findings.append(
                    SpecFinding(
                        code="substitute_scope_exceeded",
                        severity="error",
                        field=field,
                        message=(
                            f"{scope} is outside the substitute permission and lacks "
                            "granted alternative evidence"
                        ),
                        page=reading.page,
                    )
                )
    cited_pages = {
        reading.page
        for _, reading, _ in readings
        if reading.page is not None and reading.alternative_evidence is None
    }
    cited_pages.add(spec.pin_table.page)
    if spec.pinout is not None and "pinout.view_reading" not in alternative_reading_fields:
        cited_pages.add(spec.pinout.page)
    for page_number in cited_pages:
        page = stored_pages.get(page_number)
        if page is None:
            findings.append(
                SpecFinding(
                    code="page_not_extracted",
                    severity="error",
                    field=f"pages[{page_number}]",
                    message="cited page is not present in the stored extraction",
                    page=page_number,
                )
            )
        else:
            findings.extend(_page_artifact_findings(page, extraction_dir))

    observation_log = _observation_log(spec_dir)
    observed_hashes: set[str] = set()
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
            page = stored_pages.get(page_number)
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

    available_stored_pages = [
        stored_pages[page_number]
        for page_number in sorted(cited_pages)
        if page_number in stored_pages
    ]
    derived_extraction: DatasheetExtraction | None = None
    pinout_geometry: PinoutGeometry | None = None
    derived_dir = Path()
    with ExitStack() as cleanup_stack:
        confidential_token: Token[Path | None] | None = None
        if spec.datasheet.confidential:
            private_root = confidential.ensure_confidential_store(
                confidential.project_root_for(spec_path)
            )
            confidential_token = _RE_DERIVATION_BASE.set(private_root / "partspec-checks")
        token: Token[ExitStack | None] = _RE_DERIVATION_STACK.set(cleanup_stack)
        try:
            if not pdf_path.is_file() or not available_stored_pages:
                raise DatasheetError("no readable PDF and stored page DPI are available")
            dpis = {page.dpi for page in available_stored_pages}
            if len(dpis) != 1:
                raise DatasheetError("cited stored pages do not share a single DPI")
            derived_extraction, derived_dir = rederive_pages(
                pdf_path,
                sorted(cited_pages & stored_pages.keys()),
                next(iter(dpis)),
            )
        except Exception as exc:
            findings.append(
                SpecFinding(
                    code="rederivation_failed",
                    severity="error",
                    field="datasheet",
                    message=f"could not re-derive cited PDF pages: {exc}",
                )
            )
        finally:
            _RE_DERIVATION_STACK.reset(token)
            if confidential_token is not None:
                _RE_DERIVATION_BASE.reset(confidential_token)

        if derived_extraction is not None:
            if pdf_sha256 is not None and derived_extraction.pdf_sha256 != pdf_sha256:
                findings.append(
                    SpecFinding(
                        code="datasheet_sha_mismatch",
                        severity="error",
                        field="datasheet.sha256",
                        message="re-derived PDF SHA-256 differs from the current PDF",
                    )
                )
            derived_pages = {page.page: page for page in derived_extraction.pages}
            for page_number in cited_pages:
                stored_page = stored_pages.get(page_number)
                derived_page = derived_pages.get(page_number)
                if stored_page is not None and (
                    derived_page is None or stored_page.png_sha256 != derived_page.png_sha256
                ):
                    findings.append(
                        SpecFinding(
                            code="extraction_stale",
                            severity="error",
                            field=f"pages[{page_number}].png_sha256",
                            message="stored page image hash differs from fresh PDF derivation",
                            page=page_number,
                        )
                    )
                if derived_page is None:
                    findings.append(
                        SpecFinding(
                            code="page_not_extracted",
                            severity="error",
                            field=f"pages[{page_number}]",
                            message="cited page is absent from the re-derived extraction",
                            page=page_number,
                        )
                    )

            for field, reading, dimension in readings:
                if reading.alternative_evidence is not None:
                    continue
                if reading.page is None:
                    continue
                stored_page = stored_pages.get(reading.page)
                derived_page = derived_pages.get(reading.page)
                if dimension is not None:
                    _dimension_checks(
                        dimension,
                        reading,
                        field,
                        stored_page,
                        derived_page,
                        spec_dir,
                        derived_extraction,
                        derived_dir,
                        findings,
                    )
                    if derived_page is not None:
                        _dimension_cell_checks(
                            dimension,
                            reading,
                            field,
                            derived_extraction,
                            derived_dir,
                            derived_page,
                            findings,
                        )
                elif field == "package.pin1_reading":
                    _read_vision(reading, spec_dir, stored_page, field, findings)
                    _bbox_findings(reading, derived_page, field, findings)
                    _pin1_corner_findings(spec.package, reading, field, findings)
                elif field in orderable_by_field:
                    index, variant = orderable_by_field[field]
                    _read_vision(reading, spec_dir, stored_page, field, findings)
                    _orderable_row_checks(
                        spec,
                        variant,
                        index,
                        derived_extraction,
                        derived_dir,
                        derived_page,
                        findings,
                    )
                elif field in pin_by_field:
                    index, pin = pin_by_field[field]
                    _pin_checks(spec, spec_dir, pin, index, stored_page, findings)

            pin_reading_fields = set(pin_by_field)
            if not pin_reading_fields.issubset(alternative_fields):
                _pin_table_checks(
                    spec,
                    derived_extraction,
                    derived_dir,
                    derived_pages.get(spec.pin_table.page),
                    findings,
                )
            if spec.pinout is not None and "pinout.view_reading" not in alternative_reading_fields:
                pinout_geometry = _pinout_fresh_checks(
                    spec,
                    spec_dir,
                    stored_pages.get(spec.pinout.page),
                    derived_pages.get(spec.pinout.page),
                    derived_extraction,
                    derived_dir,
                    findings,
                )

            drawing_pages = {
                dimension.reading.page
                for field, dimension in _dimensions(spec)
                if field.startswith("package.") or field.startswith("land_pattern.")
                if dimension.reading.page is not None
                and dimension.reading.alternative_evidence is None
            }
            if (
                spec.package.pin1_reading.page is not None
                and spec.package.pin1_reading.alternative_evidence is None
            ):
                drawing_pages.add(spec.package.pin1_reading.page)
            for page_number in sorted(drawing_pages):
                page = derived_pages.get(page_number)
                if page is None:
                    continue
                missing_lanes: list[str] = []
                missing_revisions: list[str] = []
                for lane in ("poppler", "pdfplumber"):
                    words = _lane_words(derived_extraction, derived_dir, page_number, lane)
                    visible, _ = _visible_words(words, page, derived_dir)
                    tokens = {word.text.strip(".,;:()[]{}").casefold() for word in visible}
                    if spec.package.drawing_id.casefold() not in tokens:
                        missing_lanes.append(lane)
                    if (
                        spec.package.drawing_revision is not None
                        and spec.package.drawing_revision.casefold() not in tokens
                    ):
                        missing_revisions.append(lane)
                if missing_lanes:
                    findings.append(
                        SpecFinding(
                            code="drawing_id_missing",
                            severity="error",
                            field=f"package.drawing_id.pages[{page_number}]",
                            message=(
                                f"visible drawing_id is missing from lanes: "
                                f"{', '.join(missing_lanes)}"
                            ),
                            page=page_number,
                        )
                    )
                if missing_revisions:
                    findings.append(
                        SpecFinding(
                            code="drawing_revision_missing",
                            severity="error",
                            field=f"package.drawing_revision.pages[{page_number}]",
                            message=(
                                f"visible drawing revision is missing from lanes: "
                                f"{', '.join(missing_revisions)}"
                            ),
                            page=page_number,
                        )
                    )
            for page_number in sorted(cited_pages):
                page = derived_pages.get(page_number)
                if (
                    page is not None
                    and page.order_similarity is not None
                    and page.order_similarity < 0.6
                ):
                    findings.append(
                        SpecFinding(
                            code="reading_order_divergence",
                            severity="info",
                            field="page",
                            message="Poppler and pdfplumber word order differs substantially",
                            page=page_number,
                        )
                    )
        else:
            for field, reading, dimension in readings:
                if reading.alternative_evidence is not None:
                    continue
                if reading.page is None:
                    continue
                stored_page = stored_pages.get(reading.page)
                if dimension is not None:
                    _read_vision(reading, spec_dir, stored_page, field, findings)
                elif field == "package.pin1_reading":
                    _read_vision(reading, spec_dir, stored_page, field, findings)
                    _pin1_corner_findings(spec.package, reading, field, findings)
                elif field in orderable_by_field:
                    _read_vision(reading, spec_dir, stored_page, field, findings)
                elif field in pin_by_field:
                    index, pin = pin_by_field[field]
                    _pin_checks(spec, spec_dir, pin, index, stored_page, findings)
                elif field == "pinout.view_reading":
                    _read_vision(reading, spec_dir, stored_page, field, findings)
            if spec.pinout is not None and "pinout.view_reading" not in alternative_reading_fields:
                _vision_record_check(
                    spec.pinout.vision_record,
                    spec.pinout.page,
                    spec_dir,
                    stored_pages.get(spec.pinout.page),
                    "pinout",
                    findings,
                )
                findings.append(
                    SpecFinding(
                        code="pinout_unverified",
                        severity="error",
                        field="pinout",
                        message="pinout geometry was not freshly re-derived from the datasheet",
                        page=spec.pinout.page,
                    )
                )

        vision_extraction = derived_extraction or extraction
        vision_extraction_dir = derived_dir if derived_extraction is not None else extraction_dir
        pinout_labels_binding: (
            tuple[visionread.VisionBatch, visionread.VisionReadItem, object] | None
        ) = None
        for field, reading, _ in readings:
            if reading.alternative_evidence is not None or reading.page is None:
                continue
            if reading.vision is None:
                continue
            expected_kind: visionread.VisionKind = (
                "pin1_corner"
                if field == "package.pin1_reading"
                else "view"
                if field == "pinout.view_reading"
                else "transcribe"
            )
            binding = _vision_read_binding(
                spec_dir,
                reading.vision_read,
                field=field,
                expected_kind=expected_kind,
                page=reading.page,
                bbox=reading.bbox,
                reading=reading,
                extraction=vision_extraction,
                extraction_dir=vision_extraction_dir,
                observation_log=observation_log,
                observed_hashes=observed_hashes,
                findings=findings,
            )
            if binding is None:
                continue
            _, _, normalized = binding
            value_matches = False
            if expected_kind == "transcribe":
                value_matches = isinstance(normalized, str) and _vision_transcription_matches(
                    reading.vision, normalized
                )
            elif expected_kind == "pin1_corner":
                corner = _PIN1_CORNER.search(reading.vision)
                expected_corner = (
                    re.sub(r"[\s-]+", "_", corner.group("corner").casefold())
                    if corner is not None
                    else None
                )
                value_matches = normalized == expected_corner
            elif expected_kind == "view":
                expected_view = spec.pinout.view if spec.pinout is not None else None
                value_matches = isinstance(normalized, str) and normalized == expected_view
            if not value_matches:
                findings.append(
                    SpecFinding(
                        code="vision_read_mismatch",
                        severity="error",
                        field=field,
                        message="tool-owned vision answer differs from the PartSpec reading",
                        page=reading.page,
                    )
                )

        if not set(pin_by_field).issubset(alternative_fields):
            _pin_table_vision_checks(
                spec,
                spec_dir,
                vision_extraction,
                vision_extraction_dir,
                observation_log,
                observed_hashes,
                findings,
            )
        _orderable_vision_checks(
            spec,
            spec_dir,
            vision_extraction,
            vision_extraction_dir,
            observation_log,
            observed_hashes,
            findings,
        )
        if spec.pinout is not None and "pinout.view_reading" not in alternative_reading_fields:
            pinout_labels_binding = _vision_read_binding(
                spec_dir,
                spec.pinout.labels_vision_read,
                field="pinout.labels_vision",
                expected_kind="pin_labels",
                page=spec.pinout.page,
                bbox=spec.pinout.bbox,
                reading=None,
                extraction=vision_extraction,
                extraction_dir=vision_extraction_dir,
                observation_log=observation_log,
                observed_hashes=observed_hashes,
                findings=findings,
            )
            if pinout_labels_binding is not None:
                _, _, normalized = pinout_labels_binding
                if not _vision_name_map_matches(spec.pinout.labels_vision, normalized):
                    findings.append(
                        SpecFinding(
                            code="vision_read_mismatch",
                            severity="error",
                            field="pinout.labels_vision",
                            message="tool-owned pin labels differ from labels_vision",
                            page=spec.pinout.page,
                        )
                    )
                if pinout_geometry is not None:
                    derived_names = {
                        label.number: label.name
                        for label in pinout_geometry.labels
                        if label.name is not None
                    }
                    if not _vision_name_map_matches(derived_names, normalized):
                        findings.append(
                            SpecFinding(
                                code="vision_pinout_mismatch",
                                severity="error",
                                field="pinout",
                                message=(
                                    "tool-owned pin labels differ from freshly derived "
                                    "pinout geometry"
                                ),
                                page=spec.pinout.page,
                            )
                        )

        _consistency_checks(spec, findings)
        return PartSpecReport(
            artifact_kind="circuit_part_spec_check",
            verdict="fail" if any(finding.severity == "error" for finding in findings) else "pass",
            part_spec_sha256=actual_spec_sha,
            extraction_sha256=actual_extraction_sha,
            pdf_sha256=spec.datasheet.sha256,
            checked_readings=len(readings),
            findings=findings,
            pinout=pinout_geometry,
            substitute_permit=substitute_permit,
        )
