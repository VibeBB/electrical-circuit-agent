import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from circuit.advisory import build_review_record
from circuit.datasheet import DatasheetExtraction, LaneResult, PageExtraction, PdfWord
from circuit.partspec import (
    DatasheetRef,
    Dimension,
    ExposedPad,
    LandPattern,
    PackageSpec,
    PartSpec,
    PinSpec,
    Reading,
    check_part_spec,
    load_part_spec,
    parse_dimension_text,
    part_spec_sha256,
)

_IMPRESSION = (
    "The dimensional marks remain legible across the package drawing. "
    "The pin table and outline agree on the package identity and orientation. "
    "This review records only directly visible datasheet evidence, not inferred "
    "manufacturing recommendations or assumptions about a footprint."
)


def _word_records(texts: list[str]) -> list[PdfWord]:
    return [
        PdfWord(text=text, x0=index * 20, top=10, x1=index * 20 + 10, bottom=20)
        for index, text in enumerate(texts)
    ]


def _fixture(tmp_path: Path) -> tuple[PartSpec, DatasheetExtraction, Path, Path]:
    pdf_path = tmp_path / "parts.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fixture")
    png_path = tmp_path / "page-001.png"
    png_path.write_bytes(b"png fixture")
    vision_path = tmp_path / "vision.advisory.json"
    vision_record = build_review_record(
        png_path,
        model="placeholder-vision-model",
        checklist="datasheet",
        impression=_IMPRESSION,
        findings=[],
    )
    vision_path.write_text(vision_record.model_dump_json(indent=2), encoding="utf-8")
    words = _word_records(["3.1", "2.9", "0.8", "1,2,3", "SW"])
    lane_records: list[LaneResult] = []
    for lane_name in ("poppler", "pdfplumber"):
        lane_path = tmp_path / f"page-001.{lane_name}.json"
        lane_path.write_text(
            json.dumps([word.model_dump(mode="json") for word in words]),
            encoding="utf-8",
        )
        lane_records.append(
            LaneResult(
                lane=lane_name,
                status="ok",
                words_path=lane_path.name,
                word_count=len(words),
            )
        )
    tables_path = tmp_path / "page-001.tables.json"
    tables_path.write_text(
        json.dumps({"tables": [{"bbox": [0, 0, 100, 100], "rows": [["1,2,3", "SW"]]}]}),
        encoding="utf-8",
    )
    extraction = DatasheetExtraction(
        artifact_kind="circuit_datasheet_extraction",
        pdf_path=str(pdf_path),
        pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        page_count=1,
        pages=[
            PageExtraction(
                page=1,
                width_pt=200,
                height_pt=200,
                png_path=png_path.name,
                png_sha256=hashlib.sha256(png_path.read_bytes()).hexdigest(),
                dpi=300,
                text_layer=True,
                lanes=lane_records,
                tables_path=tables_path.name,
                table_count=1,
                vector_objects=12,
                drawing_page=False,
                order_similarity=0.5,
            )
        ],
        tools={"pdfplumber": "0.11.10", "pdftotext": "pdftotext test", "tesseract": "unavailable"},
    )
    extraction_path = tmp_path / "extraction.json"
    extraction_path.write_text(extraction.model_dump_json(indent=2), encoding="utf-8")

    def dimension(
        vision: str, *, minimum: float | None, nominal: float | None, maximum: float | None
    ) -> Dimension:
        return Dimension(
            min=minimum,
            nom=nominal,
            max=maximum,
            reading=Reading(
                page=1,
                vision=vision,
                vision_record=vision_path.name,
            ),
        )

    spec = PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="EXAMPLE-1",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path=pdf_path.name,
            sha256=extraction.pdf_sha256,
            revision="A",
            extraction_path=extraction_path.name,
        ),
        package=PackageSpec(
            family="custom",
            code="X",
            pin_count=1,
            body_length=dimension("3.1 2.9", minimum=2.9, nominal=None, maximum=3.1),
            body_width=dimension("3.1 2.9", minimum=2.9, nominal=None, maximum=3.1),
            height=dimension("0.8", minimum=None, nominal=0.8, maximum=None),
            pin1_corner="top_left",
            pin1_reading=Reading(
                page=1,
                vision="pin 1 is at the top left",
                vision_record=vision_path.name,
            ),
        ),
        pins=[
            PinSpec(
                number="1",
                name="SW",
                electrical_type="output",
                reading=Reading(
                    page=1,
                    vision="1 SW",
                    vision_record=vision_path.name,
                ),
            )
        ],
    )
    spec_path = tmp_path / "part.spec.json"
    spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    return spec, extraction, spec_path, extraction_path


def _save_spec(spec: PartSpec, path: Path) -> None:
    path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")


_DIMENSION_EXAMPLES: list[
    tuple[
        str,
        tuple[float | None, float | None, float | None, bool, int | None, list[str]],
    ]
] = [
    ("3.1 2.9", (2.9, None, 3.1, False, None, [])),
    ("□1.68±0.07", (1.61, 1.68, 1.75, False, None, ["□"])),
    ("16X 0.30 0.18", (0.18, None, 0.3, False, 16, [])),
    ("(0.45)", (None, 0.45, None, True, None, [])),
    ("4X (0.45)", (None, 0.45, None, True, 4, [])),
    ("1.0 0.8", (0.8, None, 1.0, False, None, [])),
    ("0.05 0.00", (0.0, None, 0.05, False, None, [])),
    ("1.5", (None, 1.5, None, False, None, [])),
    ("0.07 MAX", (None, None, 0.07, False, None, [])),
    ("2.90\u20133.10", (2.9, None, 3.1, False, None, [])),
    ("R0.05", (None, 0.05, None, False, None, ["R"])),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    _DIMENSION_EXAMPLES,
)
def test_parse_dimension_text_examples(
    text: str,
    expected: tuple[float | None, float | None, float | None, bool, int | None, list[str]],
) -> None:
    parsed = parse_dimension_text(text)
    assert (
        parsed.min,
        parsed.nom,
        parsed.max,
        parsed.reference,
        parsed.count,
        parsed.symbols,
    ) == expected


def test_parse_dimension_text_normalizes_numbers_and_rejects_unparseable() -> None:
    parsed = parse_dimension_text("1.0 0.8")
    assert parsed.numbers == ["1", "0.8"]
    assert parse_dimension_text(".5").numbers == ["0.5"]
    with pytest.raises(ValueError, match="could not parse"):
        parse_dimension_text("about 0.5 mm")


def test_strict_models_and_dimension_bounds() -> None:
    with pytest.raises(ValidationError):
        Reading(page=0, vision="x", vision_record="record.json")
    with pytest.raises(ValidationError):
        Reading(page=1, vision="x", vision_record="record.json", user_confirmed="Qx")
    with pytest.raises(ValidationError, match="at least one"):
        Dimension(reading=Reading(page=1, vision="1", vision_record="r.json"))
    with pytest.raises(ValidationError, match="min <= nom"):
        Dimension(
            min=2,
            nom=1,
            reading=Reading(page=1, vision="1", vision_record="r.json"),
        )
    with pytest.raises(ValidationError, match="at least one dimension"):
        LandPattern(source="datasheet", pads=[], dimensions={})
    with pytest.raises(ValidationError):
        PartSpec.model_validate({"artifact_kind": "circuit_part_spec", "unknown": True})


def test_load_part_spec_hash_and_happy_path(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    loaded = load_part_spec(spec_path)
    assert loaded == spec
    assert part_spec_sha256(spec_path) == hashlib.sha256(spec_path.read_bytes()).hexdigest()
    report = check_part_spec(
        loaded,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert report.verdict == "pass"
    assert report.checked_readings == 5
    assert report.extraction_sha256 == hashlib.sha256(extraction_path.read_bytes()).hexdigest()
    assert any(finding.code == "reading_order_divergence" for finding in report.findings)


def test_datasheet_sha_mismatch_and_missing_file(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.datasheet.sha256 = "f" * 64
    spec.datasheet.path = "missing.pdf"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {finding.code for finding in report.findings}
    assert "datasheet_sha_mismatch" in codes
    assert "datasheet_missing" in codes
    assert report.verdict == "fail"


def test_vision_record_missing_mismatch_and_value_mismatch(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.body_length.reading.vision_record = "missing.json"
    spec.package.body_width.max = 3.2
    record_path = tmp_path / "vision.advisory.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["detail"]["checklist"] = "footprint"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {finding.code for finding in report.findings}
    assert "vision_record_missing" in codes
    assert "vision_record_mismatch" in codes
    assert "value_mismatch" in codes


def test_dimension_mechanical_mismatch_confirmation_disagreement_and_glyph_loss(
    tmp_path: Path,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.body_length.reading.user_confirmed = "Q4"
    spec.package.body_width.reading.mechanical = "3.2 2.9"
    spec.package.height.reading.vision = "□0.8±0.1"
    spec.package.height.min = 0.7
    spec.package.height.nom = 0.8
    spec.package.height.max = 0.9
    words = _word_records(["2.9", "0.8", "1,2,3", "SW"])
    for lane_name in ("poppler", "pdfplumber"):
        (tmp_path / f"page-001.{lane_name}.json").write_text(
            json.dumps([word.model_dump(mode="json") for word in words]),
            encoding="utf-8",
        )
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    coded = {(finding.code, finding.field, finding.severity) for finding in report.findings}
    assert ("mechanical_unconfirmed_user_confirmed", "package.body_length", "warning") in coded
    assert ("lane_disagreement", "package.body_width", "error") in coded
    assert ("mechanical_mismatch", "package.height", "error") in coded
    assert ("glyph_loss", "package.height", "info") in coded


def test_page_checks_pin_evidence_and_table_matching(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.pin1_reading.page = 2
    spec.pins[0].reading.vision = "1"
    spec.pins[0].reading.user_confirmed = "Q12"
    for lane_name in ("poppler", "pdfplumber"):
        path = tmp_path / f"page-001.{lane_name}.json"
        words = _word_records(["3.1", "2.9", "0.8", "1,2,3"])
        path.write_text(
            json.dumps([word.model_dump(mode="json") for word in words]), encoding="utf-8"
        )
    tables_path = tmp_path / "page-001.tables.json"
    tables_path.write_text(
        json.dumps({"tables": [{"bbox": [0, 0, 1, 1], "rows": [["1,2,3", "EN"]]}]}),
        encoding="utf-8",
    )
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {finding.code for finding in report.findings}
    assert "page_not_extracted" in codes
    assert "vision_unparseable" in codes
    assert "mechanical_unconfirmed_user_confirmed" in codes
    assert "pin_table_unmatched" in codes


def test_mechanical_lane_unavailable_and_unparseable_vision(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.body_length.reading.vision = "not a dimension"
    extraction.pages[0].lanes = [
        lane.model_copy(update={"status": "error"}) for lane in extraction.pages[0].lanes
    ]
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {finding.code for finding in report.findings}
    assert "vision_unparseable" in codes
    assert "mechanical_lane_unavailable" in codes


def test_consistency_errors_cover_pin_family_pitch_pad_and_height(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.family = "no_lead_quad"
    spec.package.pin_count = 10
    spec.package.pitch = Dimension(
        nom=4,
        reading=Reading(page=1, vision="4", vision_record="vision.advisory.json"),
    )
    spec.package.exposed_pad = ExposedPad(
        number="17",
        length=Dimension(
            nom=4,
            reading=Reading(page=1, vision="4", vision_record="vision.advisory.json"),
        ),
        width=Dimension(
            nom=4,
            reading=Reading(page=1, vision="4", vision_record="vision.advisory.json"),
        ),
    )
    spec.package.height.nom = 0
    spec.pins.append(
        PinSpec(
            number="1",
            name="SW",
            electrical_type="output",
            reading=Reading(page=1, vision="1 SW", vision_record="vision.advisory.json"),
        )
    )
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {finding.code for finding in report.findings}
    assert {
        "duplicate_pin",
        "pin_count_mismatch",
        "exposed_pad_unmapped",
        "pin_count_family",
        "pitch_exceeds_body",
        "exposed_pad_exceeds_body",
        "height_nonpositive",
    } <= codes


def test_pin_count_mismatch_and_load_failure(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.pin_count = 2
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "pin_count_mismatch" in {finding.code for finding in report.findings}
    spec_path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="could not load"):
        load_part_spec(spec_path)
