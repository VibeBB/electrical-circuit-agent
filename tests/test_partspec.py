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
    OrderableVariant,
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
    words = _word_records(["3.1", "2.9", "0.8", "1,2,3", "SW", "EXAMPLE-1", "X"])
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
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(
        json.dumps(
            {
                "brief_sha256": "0" * 64,
                "requirements": [
                    {
                        "id": "R1",
                        "text": "The exposed thermal pad is pin 17.",
                        "source": "user",
                        "speaker": "user",
                    }
                ],
                "assumptions": [],
                "open_questions": [],
                "part_sources": {},
                "net_sources": {},
            }
        ),
        encoding="utf-8",
    )

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
        intake_path=intake_path.name,
        package=PackageSpec(
            family="custom",
            code="X",
            pin_count=1,
            body_length=dimension("3.1 2.9", minimum=2.9, nominal=None, maximum=3.1),
            body_width=dimension("3.1 2.9", minimum=2.9, nominal=None, maximum=3.1),
            height=dimension("0.8", minimum=None, nominal=0.8, maximum=None),
            drawing_view="top",
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
        orderable=[
            OrderableVariant(
                mpn="EXAMPLE-1",
                package_code="X",
                reading=Reading(
                    page=1,
                    vision="EXAMPLE-1 X package variant",
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
    assert (
        Reading(page=1, vision="x", vision_record="record.json", user_confirmed="R4").user_confirmed
        == "R4"
    )
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
    assert report.checked_readings == 6
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
    spec.package.body_length.reading.user_confirmed = "R1"
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
    spec.pins[0].reading.user_confirmed = "R1"
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
    spec.package.pins_per_side = (3, 3, 3, 3)
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


def test_user_confirmation_requires_a_user_requirement(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.body_length.reading.user_confirmed = "R9"
    spec.package.body_length.reading.vision = "3.2 2.9"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    findings = {(item.code, item.severity) for item in report.findings}
    assert ("mechanical_mismatch", "error") in findings
    assert ("user_confirmation_unverified", "error") in findings
    assert not any(item.code == "mechanical_unconfirmed_user_confirmed" for item in report.findings)

    intake_path = tmp_path / "intake.json"
    intake_value = json.loads(intake_path.read_text(encoding="utf-8"))
    intake_value["requirements"][0]["source"] = "agent"
    intake_path.write_text(json.dumps(intake_value), encoding="utf-8")
    spec.package.body_length.reading.user_confirmed = "R1"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    findings = {(item.code, item.severity) for item in report.findings}
    assert ("mechanical_mismatch", "error") in findings
    assert ("user_confirmation_unverified", "error") in findings


@pytest.mark.parametrize(
    ("vision", "expected"),
    [
        ("Pin 1 is at the top-left corner.", None),
        ("Pin 1 is at the top left corner.", None),
        ("Pin 1 is at the TOP_LEFT corner.", None),
        ("Pin 1 is marked.", "pin1_unparseable"),
        ("Pin 1 is at the top left and bottom right.", "pin1_unparseable"),
        ("Pin 1 is at the top right corner.", "pin1_mismatch"),
    ],
)
def test_pin1_corner_reading_matches_declared_orientation(
    tmp_path: Path, vision: str, expected: str | None
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.pin1_reading.vision = vision
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {item.code for item in report.findings}
    if expected is None:
        assert "pin1_unparseable" not in codes
        assert "pin1_mismatch" not in codes
    else:
        assert expected in codes


@pytest.mark.parametrize(
    "artifact_state", ["missing_png", "changed_png", "missing_words", "invalid_words"]
)
def test_cited_page_artifacts_are_rehashed_and_parsed(tmp_path: Path, artifact_state: str) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    if artifact_state == "missing_png":
        (tmp_path / "page-001.png").unlink()
    elif artifact_state == "changed_png":
        (tmp_path / "page-001.png").write_bytes(b"changed image")
    elif artifact_state == "missing_words":
        (tmp_path / "page-001.poppler.json").unlink()
    else:
        (tmp_path / "page-001.poppler.json").write_text('{"not":"words"}', encoding="utf-8")
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {item.code for item in report.findings}
    expected = "evidence_sha_mismatch" if artifact_state == "changed_png" else "evidence_missing"
    assert expected in codes


def test_observation_log_is_required_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CIRCUIT_IMAGE_OBSERVATIONS", raising=False)
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "vision_observation_log_missing" in {item.code for item in report.findings}

    observation_path = tmp_path / "observations.jsonl"
    observation_path.write_text(
        json.dumps({"image_sha256": extraction.pages[0].png_sha256}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CIRCUIT_IMAGE_OBSERVATIONS", str(observation_path))
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "vision_not_observed" not in {item.code for item in report.findings}

    observation_path.write_text("{}\n", encoding="utf-8")
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "vision_not_observed" in {item.code for item in report.findings}


def test_observation_log_is_found_above_spec_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CIRCUIT_IMAGE_OBSERVATIONS", raising=False)
    spec_dir = tmp_path / "project" / "parts"
    spec_dir.mkdir(parents=True)
    spec, extraction, spec_path, extraction_path = _fixture(spec_dir)
    log_path = tmp_path / "project" / "observations" / "circuit" / "image-observations.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        json.dumps({"image_sha256": extraction.pages[0].png_sha256}) + "\n",
        encoding="utf-8",
    )
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {item.code for item in report.findings}
    assert "vision_not_observed" not in codes
    assert "vision_observation_log_missing" not in codes


def test_drawing_dimensions_warn_when_bbox_is_missing(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    extraction.pages[0].drawing_page = True
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert any(
        item.code == "bbox_missing" and item.field == "package.body_length"
        for item in report.findings
    )


def test_quad_side_counts_and_axis_specific_pitch_bounds(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.family = "no_lead_quad"
    spec.package.pin_count = 16
    spec.package.pins_per_side = (4, 4, 4, 4)
    spec.package.pitch = Dimension(
        nom=1.5,
        reading=Reading(page=1, vision="1.5", vision_record="vision.advisory.json"),
    )
    spec.package.body_length = Dimension(
        nom=8,
        reading=Reading(page=1, vision="8", vision_record="vision.advisory.json"),
    )
    spec.package.body_width = Dimension(
        nom=4,
        reading=Reading(page=1, vision="4", vision_record="vision.advisory.json"),
    )
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    pitch_findings = [item for item in report.findings if item.code == "pitch_exceeds_body"]
    assert [item.message.split()[0] for item in pitch_findings] == ["bottom", "top"]

    spec.package.pins_per_side = (4, 4, 4, 3)
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "pin_count_family" in {item.code for item in report.findings}


def test_pins_per_side_is_limited_to_quad_families(tmp_path: Path) -> None:
    spec, _, _, _ = _fixture(tmp_path)
    value = spec.package.model_dump(mode="python")
    value["pins_per_side"] = (1, 1, 1, 1)
    with pytest.raises(ValidationError, match="only valid for quad"):
        PackageSpec.model_validate(value)


@pytest.mark.parametrize(
    ("drawing_view", "vision", "expected"),
    [
        ("top", "Pin 1 at top left.", None),
        ("bottom", "Pin 1 at top right.", None),
        ("bottom", "Pin 1 at top left.", "pin1_mismatch"),
        ("bottom", "Pin 1 at top left and bottom right.", "pin1_unparseable"),
    ],
)
def test_pin1_corner_respects_drawing_view(
    tmp_path: Path, drawing_view: str, vision: str, expected: str | None
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package = spec.package.model_copy(update={"drawing_view": drawing_view})
    spec.package.pin1_reading.vision = vision
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {item.code for item in report.findings}
    if expected is None:
        assert "pin1_unparseable" not in codes
        assert "pin1_mismatch" not in codes
    else:
        assert expected in codes


def test_orderable_variant_binding_and_vision_proof(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.orderable[0].package_code = "Y"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "package_variant_unbound" in {item.code for item in report.findings}

    spec.orderable[0].package_code = "X"
    spec.orderable[0].reading.vision_record = "missing-vision.json"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert any(
        item.code == "vision_record_missing" and item.field == "orderable[0]"
        for item in report.findings
    )

    spec.mpn = "example-1"
    spec.orderable[0].reading.vision_record = "vision.advisory.json"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "package_variant_unbound" not in {item.code for item in report.findings}


def test_orderable_mechanical_mismatch_uses_verified_confirmation(
    tmp_path: Path,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    words = _word_records(["3.1", "2.9", "0.8", "1,2,3", "SW"])
    for lane_name in ("poppler", "pdfplumber"):
        (tmp_path / f"page-001.{lane_name}.json").write_text(
            json.dumps([word.model_dump(mode="json") for word in words]),
            encoding="utf-8",
        )
    spec.orderable[0].reading.user_confirmed = "R1"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert any(
        item.code == "mechanical_unconfirmed_user_confirmed"
        and item.field == "orderable[0]"
        and item.severity == "warning"
        for item in report.findings
    )

    spec.orderable[0].reading.user_confirmed = "R9"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert any(
        item.code == "mechanical_mismatch"
        and item.field == "orderable[0]"
        and item.severity == "error"
        for item in report.findings
    )
    assert any(
        item.code == "user_confirmation_unverified" and item.field == "orderable[0]"
        for item in report.findings
    )


def test_pin_view_is_recorded_without_extra_validation(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.pins[0].view = "bottom"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert not any(item.field == "pins[0].view" for item in report.findings)


def test_drawing_view_and_orderable_variants_are_required(tmp_path: Path) -> None:
    spec, _, _, _ = _fixture(tmp_path)
    spec_value = spec.model_dump(mode="python")
    del spec_value["package"]["drawing_view"]
    with pytest.raises(ValidationError):
        PartSpec.model_validate(spec_value)

    spec_value = spec.model_dump(mode="python")
    del spec_value["orderable"]
    with pytest.raises(ValidationError):
        PartSpec.model_validate(spec_value)
