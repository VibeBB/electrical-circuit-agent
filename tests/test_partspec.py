import hashlib
import json
import math
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from circuit import humanrequest
from circuit import partspec as partspec_module
from circuit.advisory import build_review_record
from circuit.datasheet import DatasheetExtraction, LaneResult, PageExtraction, PdfWord
from circuit.partspec import (
    BallGrid,
    CellRef,
    DatasheetRef,
    Dimension,
    ExposedPad,
    LandPattern,
    OrderableVariant,
    PackageSpec,
    PartSpec,
    PinoutDrawing,
    PinSpec,
    PinTable,
    Reading,
    SpecFinding,
    SubstitutionRef,
    TabSpec,
    _vision_transcription_matches,  # pyright: ignore[reportPrivateUsage]
    check_part_spec,
    load_part_spec,
    parse_dimension_text,
    part_spec_sha256,
)
from circuit.pinout import normalized
from pinout_fixtures import QUAD16_NAMES, pinout_drawing, quad16_fixture
from vision_fixtures import attach_vision_reads

_IMPRESSION = (
    "The dimensional marks remain legible across the package drawing. "
    "The pin table and outline agree on the package identity and orientation. "
    "This review records only directly visible datasheet evidence, not inferred "
    "manufacturing recommendations or assumptions about a footprint."
)
_REDERIVED_BY_PDF: dict[Path, tuple[DatasheetExtraction, Path]] = {}


@pytest.mark.parametrize(
    ("expected", "actual", "matches"),
    [
        ("1", "16", False),
        ("0.5", "10.5", False),
        ("1.68", "□1.68±0.07", True),
        ("", "1", False),
    ],
)
def test_vision_transcription_matches_token_boundaries(
    expected: str,
    actual: str,
    matches: bool,
) -> None:
    assert _vision_transcription_matches(expected, actual) is matches


@pytest.fixture(autouse=True)
def _mock_rederivation(monkeypatch: pytest.MonkeyPatch) -> None:
    def rederive_pages(
        pdf_path: Path, _pages: list[int], _dpi: int
    ) -> tuple[DatasheetExtraction, Path]:
        return _REDERIVED_BY_PDF[pdf_path.resolve()]

    monkeypatch.setattr(partspec_module, "rederive_pages", rederive_pages)


def _word_records(texts: list[str]) -> list[PdfWord]:
    return [
        PdfWord(text=text, x0=index * 20, top=10, x1=index * 20 + 10, bottom=20)
        for index, text in enumerate(texts)
    ]


def _fixture(
    tmp_path: Path,
    *,
    extra_words: Sequence[PdfWord] = (),
) -> tuple[PartSpec, DatasheetExtraction, Path, Path]:
    pdf_path = tmp_path / "parts.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fixture")
    png_path = tmp_path / "page-001.png"
    scale = 300 / 72
    image = Image.new(
        "L",
        (math.ceil(200 * scale), math.ceil(200 * scale)),
        color=255,
    )
    draw = ImageDraw.Draw(image)
    texts = ["3.1", "2.9", "0.8", "1", "SW", "EXAMPLE-1", "X", "1"]
    words = [
        *_word_records(texts),
        PdfWord(text="D", x0=0, top=49, x1=5, bottom=58),
        PdfWord(text="MIN mm", x0=8, top=41, x1=14, bottom=47),
        PdfWord(text="MAX mm", x0=23, top=41, x1=29, bottom=47),
        PdfWord(text="2.9", x0=8, top=49, x1=14, bottom=58),
        PdfWord(text="3.1", x0=23, top=49, x1=29, bottom=58),
        *extra_words,
    ]
    for word in words:
        draw.rectangle(
            (
                math.floor(word.x0 * scale),
                math.floor(word.top * scale),
                math.ceil(word.x1 * scale),
                math.ceil(word.bottom * scale),
            ),
            fill=0,
        )
    image.save(png_path)
    vision_path = tmp_path / "vision.advisory.json"
    vision_record = build_review_record(
        png_path,
        model="placeholder-vision-model",
        checklist="datasheet",
        impression=_IMPRESSION,
        findings=[],
    )
    vision_path.write_text(vision_record.model_dump_json(indent=2), encoding="utf-8")
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
    tables = [
        {
            "bbox": [55, 0, 95, 40],
            "rows": [["Pin No.", "Function"], ["1", "SW"]],
            "cells": [
                [[55, 30, 75, 40], [75, 30, 95, 40]],
                [[55, 0, 75, 30], [75, 0, 95, 30]],
            ],
        },
        {
            "bbox": [95, 0, 155, 40],
            "rows": [["MPN", "Package", "Pins"], ["EXAMPLE-1", "X", "1"]],
            "cells": [
                [[95, 30, 115, 40], [115, 30, 135, 40], [135, 30, 155, 40]],
                [[95, 0, 115, 30], [115, 0, 135, 30], [135, 0, 155, 30]],
            ],
        },
        {
            "bbox": [0, 40, 30, 60],
            "rows": [["Symbol", "MIN mm", "NOM mm", "MAX mm"], ["D", "2.9", None, "3.1"]],
            "cells": [
                [[0, 40, 7.5, 48], [7.5, 40, 15, 48], [15, 40, 22.5, 48], [22.5, 40, 30, 48]],
                [[0, 48, 7.5, 60], [7.5, 48, 15, 60], [15, 48, 22.5, 60], [22.5, 48, 30, 60]],
            ],
        },
    ]
    tables_path.write_text(
        json.dumps({"tables": tables}),
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
                table_count=3,
                vector_objects=12,
                drawing_page=False,
                order_similarity=0.5,
            )
        ],
        tools={"pdfplumber": "0.11.10", "pdftotext": "pdftotext test", "tesseract": "unavailable"},
    )
    extraction_path = tmp_path / "extraction.json"
    extraction_path.write_text(extraction.model_dump_json(indent=2), encoding="utf-8")
    derived_dir = tmp_path / "derived"
    derived_dir.mkdir()
    derived_png = derived_dir / png_path.name
    image.save(derived_png)
    derived_lane_records: list[LaneResult] = []
    for lane_name in ("poppler", "pdfplumber"):
        source_lane = tmp_path / f"page-001.{lane_name}.json"
        (derived_dir / source_lane.name).write_bytes(source_lane.read_bytes())
        derived_lane_records.append(
            LaneResult(
                lane=lane_name,
                status="ok",
                words_path=source_lane.name,
                word_count=len(words),
            )
        )
    derived_tables_path = derived_dir / tables_path.name
    derived_tables_path.write_bytes(tables_path.read_bytes())
    derived_extraction = extraction.model_copy(deep=True)
    derived_extraction.pages[0].png_sha256 = hashlib.sha256(derived_png.read_bytes()).hexdigest()
    derived_extraction.pages[0].lanes = derived_lane_records
    _REDERIVED_BY_PDF[pdf_path.resolve()] = (derived_extraction, derived_dir)

    def dimension(
        vision: str, *, minimum: float | None, nominal: float | None, maximum: float | None
    ) -> Dimension:
        bbox = (39, 9, 51, 21) if vision == "0.8" else (0, 9, 31, 21)
        return Dimension(
            min=minimum,
            nom=nominal,
            max=maximum,
            reading=Reading(
                page=1,
                bbox=bbox,
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
            drawing_id="X",
            pin_count=1,
            body_length=dimension("3.1 2.9", minimum=2.9, nominal=None, maximum=3.1),
            body_width=dimension("3.1 2.9", minimum=2.9, nominal=None, maximum=3.1),
            height=dimension("0.8", minimum=None, nominal=0.8, maximum=None),
            drawing_view="top",
            pin1_corner="top_left",
            pin1_reading=Reading(
                page=1,
                bbox=(0, 9, 11, 21),
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
                package_designator="X",
                pin_count=1,
                row=CellRef(table=1, row=1, col=0),
                reading=Reading(
                    page=1,
                    vision="EXAMPLE-1 X package variant",
                    vision_record=vision_path.name,
                ),
            )
        ],
        pin_table=PinTable(
            page=1,
            table=0,
            number_col=0,
            name_col=1,
        ),
    )
    spec.package.body_length.label = "D"
    spec.package.body_length.reading.bbox = (7, 47, 31, 61)
    spec.package.body_length.reading.cells = {
        "min": CellRef(table=2, row=1, col=1),
        "max": CellRef(table=2, row=1, col=3),
    }
    spec_path = tmp_path / "part.spec.json"
    spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    attach_vision_reads(spec, spec_path, extraction_path)
    spec.bind_source_file(spec_path)
    return spec, extraction, spec_path, extraction_path


def _save_spec(spec: PartSpec, path: Path) -> None:
    path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")


def _human_request(
    *,
    kind: str,
    mpn: str,
    details: dict[str, Any],
) -> humanrequest.HumanRequest:
    return humanrequest.build_request(
        kind=kind,
        subject={"manufacturer": "Example", "mpn": mpn},
        reason="A human decision is needed to bind this source evidence.",
        evidence=[
            {
                "kind": "note",
                "ref": "verification",
                "summary": "The request is tied to the current PartSpec evidence.",
            }
        ],
        known=["The PartSpec and request identities are hash-bound."],
        unknown=["The underlying source claim requires independent human review."],
        agent_assessment=_IMPRESSION,
        recommendation="Grant the reviewed scope.",
        recommendation_rationale="Only the cited evidence and requested fields are in scope.",
        alternatives=[
            {
                "option": "Grant the reviewed scope.",
                "risks": ["An incorrect source claim could be accepted."],
            },
            {
                "option": "Deny the request.",
                "risks": ["The part remains blocked from the library."],
            },
        ],
        recommended=0,
        details=details,
    )


def _record_human_response(
    request: humanrequest.HumanRequest,
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_fields: list[str],
) -> None:
    request_path, _ = humanrequest.write_request(request, project_root)
    events_root = project_root / "events"
    events_root.mkdir(exist_ok=True)
    monkeypatch.setenv("CIRCUIT_AGENT_EVENTS_DIR", str(events_root))
    content = "\n".join(
        [
            f"CIRCUIT-HUMAN-RESPONSE {request.request_id}",
            "decision: grant",
            "reviewer: Test Reviewer",
            *response_fields,
        ]
    )
    raw = json.dumps({"source": "user", "message": {"content": content}}).encode("utf-8")
    event_path = events_root / f"event-{request.request_id}.json"
    event_path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    response_dir = request_path.parent / "responses"
    response_dir.mkdir(exist_ok=True)
    (response_dir / f"{request.request_id}.{digest[:12]}.json").write_text(
        json.dumps(
            {
                "artifact_kind": "circuit_human_response_pointer",
                "request_id": request.request_id,
                "request_sha256": request.request_sha256,
                "event_path": str(event_path),
                "event_sha256": digest,
                "recorded_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def _vision_answer_record_path(spec_dir: Path, reference: str) -> tuple[Path, str]:
    batch_ref, read_id = reference.split("#", maxsplit=1)
    return spec_dir / Path(batch_ref).parent / "answers.json", read_id


def _replace_vision_answer(
    spec_dir: Path,
    reference: str,
    *,
    answer: str | None = None,
    normalized: object | None = None,
    impression: str | None = None,
    clear_impression: bool = False,
) -> None:
    path, read_id = _vision_answer_record_path(spec_dir, reference)
    record = json.loads(path.read_text(encoding="utf-8"))
    if answer is not None:
        record["answers"][read_id] = answer
    if normalized is not None:
        record["normalized"][read_id] = normalized
        record["status"][read_id] = "ok"
    if clear_impression:
        record["impressions"].pop(read_id, None)
    elif impression is not None:
        record["impressions"][read_id] = impression
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def _pinout_fixture(
    tmp_path: Path,
    *,
    page_view: Literal["top", "bottom"] = "top",
    declared_view: Literal["top", "bottom"] | None = None,
    vision_mismatch: bool = False,
    mirror_page_x: bool = False,
) -> tuple[PartSpec, DatasheetExtraction, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    words, _, names = quad16_fixture(QUAD16_NAMES)
    extra_words = [
        PdfWord(
            text=f"{page_view.title()} View",
            x0=13,
            top=13,
            x1=27,
            bottom=17,
        )
    ]
    for word in words:
        x0, x1 = word.x0 + 100, word.x1 + 100
        if page_view == "bottom" or mirror_page_x:
            x0, x1 = 200 - x1, 200 - x0
        extra_words.append(
            PdfWord(
                text=word.text,
                x0=x0,
                top=word.top + 100,
                x1=x1,
                bottom=word.bottom + 100,
            )
        )
    spec, extraction, spec_path, extraction_path = _fixture(
        tmp_path,
        extra_words=extra_words,
    )
    spec.package = spec.package.model_copy(update={"family": "gullwing_quad", "pin_count": 16})
    spec.pins = [
        PinSpec(
            number=number,
            name=name,
            electrical_type="passive",
            reading=Reading(
                page=1,
                vision=f"{number} {name}",
                vision_record="vision.advisory.json",
            ),
        )
        for number, name in names.items()
    ]
    tables_path = tmp_path / "page-001.tables.json"
    tables = json.loads(tables_path.read_text(encoding="utf-8"))
    rows: list[list[str]] = [["Pin No.", "Function"]]
    rows.extend([number, name] for number, name in names.items())
    table_cells = [[[55, 0, 75, 2], [75, 0, 95, 2]]]
    for index in range(1, len(rows)):
        top = 2 + (index - 1) * 2
        table_cells.append([[55, top, 75, top + 2], [75, top, 95, top + 2]])
    tables["tables"][0]["rows"] = rows
    tables["tables"][0]["cells"] = table_cells
    tables["tables"][0]["bbox"] = [55, 0, 95, 34]
    tables_path.write_text(json.dumps(tables), encoding="utf-8")
    spec.orderable[0].pin_count = 16
    spec.pinout = pinout_drawing(
        names,
        page=1,
        view=declared_view or page_view,
        bbox=(70, 70, 130, 130),
        vision_record="vision.advisory.json",
    )
    if vision_mismatch:
        spec.pinout.labels_vision["1"] = "WRONG"
    _save_spec(spec, spec_path)
    attach_vision_reads(spec, spec_path, extraction_path)
    return spec, extraction, spec_path, extraction_path


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


def test_alternative_evidence_requires_a_current_grant_and_file_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    evidence_path = tmp_path / "measurement.csv"
    evidence_path.write_text("length_mm,3.0\n", encoding="utf-8")
    evidence_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    request = _human_request(
        kind="alternative_evidence",
        mpn=spec.mpn,
        details={
            "kind": "alternative_evidence",
            "evidence_kind": "measurement",
            "files": [{"path": evidence_path.name, "sha256": evidence_hash}],
            "covers": ["/package/body_length"],
            "unknown_fields": ["drawing revision"],
            "measurement_method": "Three caliper measurements averaged.",
        },
    )
    _record_human_response(
        request,
        tmp_path,
        monkeypatch,
        ["covers: /package/body_length"],
    )
    spec.package.body_length.reading = Reading(alternative_evidence=request.request_id)
    _save_spec(spec, spec_path)

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert any(
        finding.code == "alternative_evidence_used" and finding.field == "package.body_length"
        for finding in report.findings
    )
    assert not any(
        finding.code == "alternative_evidence_missing" and finding.field == "package.body_length"
        for finding in report.findings
    )

    evidence_path.write_text("length_mm,4.0\n", encoding="utf-8")
    stale = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert any(
        finding.code == "alternative_evidence_stale" and finding.field == "package.body_length"
        for finding in stale.findings
    )


def test_alternative_evidence_cannot_exceed_granted_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    evidence_path = tmp_path / "measurement.csv"
    evidence_path.write_text("length_mm,3.0\n", encoding="utf-8")
    evidence_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    request = _human_request(
        kind="alternative_evidence",
        mpn=spec.mpn,
        details={
            "kind": "alternative_evidence",
            "evidence_kind": "measurement",
            "files": [{"path": evidence_path.name, "sha256": evidence_hash}],
            "covers": ["/package/body_width"],
            "unknown_fields": ["body length"],
            "measurement_method": "Three caliper measurements averaged.",
        },
    )
    _record_human_response(
        request,
        tmp_path,
        monkeypatch,
        ["covers: /package/body_width"],
    )
    spec.package.body_length.reading = Reading(alternative_evidence=request.request_id)
    _save_spec(spec, spec_path)

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert any(
        finding.code == "alternative_evidence_scope_exceeded"
        and finding.field == "package.body_length"
        for finding in report.findings
    )


def test_invalid_alternative_pin_does_not_fall_back_to_datasheet_table(
    tmp_path: Path,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.pins[0].name = "ALTERNATIVE NAME"
    spec.pins[0].reading = Reading(alternative_evidence="a" * 16)
    _save_spec(spec, spec_path)

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )

    codes = {finding.code for finding in report.findings}
    assert "alternative_evidence_missing" in codes
    assert "pin_table_bijection" not in codes


def test_photo_alternative_evidence_requires_a_vision_impression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    photo_path = tmp_path / "package-photo.jpg"
    photo_path.write_bytes(b"synthetic photo evidence")
    photo_hash = hashlib.sha256(photo_path.read_bytes()).hexdigest()
    request = _human_request(
        kind="alternative_evidence",
        mpn=spec.mpn,
        details={
            "kind": "alternative_evidence",
            "evidence_kind": "photo",
            "files": [{"path": photo_path.name, "sha256": photo_hash}],
            "covers": ["/package/body_length"],
            "unknown_fields": ["drawing revision"],
        },
    )
    _record_human_response(
        request,
        tmp_path,
        monkeypatch,
        ["covers: /package/body_length"],
    )
    spec.package.body_length.reading = Reading(alternative_evidence=request.request_id)
    _save_spec(spec, spec_path)

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert any(
        finding.code == "alternative_evidence_missing"
        and finding.field == "package.body_length"
        and "vision read" in finding.message
        for finding in report.findings
    )


def test_substitute_permission_binds_grant_mpn_and_datasheet_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    substitute_mpn = "EXAMPLE-2"
    granted_scope: list[humanrequest.SubstituteScope] = [
        "package_dimensions",
        "pin_table",
        "orderable",
    ]
    request = _human_request(
        kind="substitute_permission",
        mpn=spec.mpn,
        details={
            "kind": "substitute_permission",
            "target_mpn": spec.mpn,
            "substitute_mpn": substitute_mpn,
            "substitute_datasheet_sha256": spec.datasheet.sha256,
            "requested_scope": granted_scope,
            "unverifiable_fields": ["electrical limits"],
            "similarity_evidence": ["same package and pinout"],
        },
    )
    _record_human_response(
        request,
        tmp_path,
        monkeypatch,
        [
            f"scope: {','.join(granted_scope)}",
            f"target: {spec.mpn}",
            f"substitute: {substitute_mpn}",
            f"substitute_sha256: {spec.datasheet.sha256}",
        ],
    )
    spec.substitution = SubstitutionRef(
        request_id=request.request_id,
        substitute_mpn=substitute_mpn,
        scope=granted_scope,
    )
    spec.orderable[0].mpn = substitute_mpn
    _save_spec(spec, spec_path)

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {finding.code for finding in report.findings}
    assert report.substitute_permit is not None
    assert "substitute_permission_stale" not in codes
    assert "package_variant_unbound" not in codes
    assert "orderable_mpn_mismatch" in codes

    spec.substitution.scope = ["model3d"]
    _save_spec(spec, spec_path)
    excess_scope = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert any(
        finding.code == "substitute_scope_exceeded" and finding.field == "substitution.scope"
        for finding in excess_scope.findings
    )

    spec.substitution.request_id = "0" * 16
    _save_spec(spec, spec_path)
    missing = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "substitute_permission_missing" in {finding.code for finding in missing.findings}


def test_substitute_permission_rejects_a_different_substitute_pdf_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    substitute_mpn = "EXAMPLE-2"
    granted_scope: list[humanrequest.SubstituteScope] = [
        "package_dimensions",
        "pin_table",
        "orderable",
    ]
    request = _human_request(
        kind="substitute_permission",
        mpn=spec.mpn,
        details={
            "kind": "substitute_permission",
            "target_mpn": spec.mpn,
            "substitute_mpn": substitute_mpn,
            "substitute_datasheet_sha256": "b" * 64,
            "requested_scope": granted_scope,
            "unverifiable_fields": ["electrical limits"],
            "similarity_evidence": ["same package and pinout"],
        },
    )
    _record_human_response(
        request,
        tmp_path,
        monkeypatch,
        [
            f"scope: {','.join(granted_scope)}",
            f"target: {spec.mpn}",
            f"substitute: {substitute_mpn}",
            f"substitute_sha256: {'b' * 64}",
        ],
    )
    spec.substitution = SubstitutionRef(
        request_id=request.request_id,
        substitute_mpn=substitute_mpn,
        scope=granted_scope,
    )
    spec.orderable[0].mpn = substitute_mpn
    _save_spec(spec, spec_path)

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "substitute_permission_stale" in {finding.code for finding in report.findings}
    assert report.substitute_permit is None


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


def test_dimension_count_prefix_is_ignored_by_lane_comparison(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.pitch = Dimension(
        nom=0.8,
        reading=Reading(
            page=1,
            bbox=(39, 9, 51, 21),
            vision="1X 0.8",
            vision_record="vision.advisory.json",
        ),
    )
    _save_spec(spec, spec_path)
    _, derived_dir = _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()]
    for lane in ("poppler", "pdfplumber"):
        words_path = derived_dir / f"page-001.{lane}.json"
        words = json.loads(words_path.read_text(encoding="utf-8"))
        next(word for word in words if word["text"] == "0.8")["text"] = "1X 0.8"
        words_path.write_text(json.dumps(words), encoding="utf-8")

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )

    assert not any(
        finding.code in {"mechanical_mismatch", "mechanical_single_lane"}
        and finding.field == "package.pitch"
        for finding in report.findings
    )


def test_confidential_datasheet_paths_must_be_inside_project_store(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec = spec.model_copy(
        update={"datasheet": spec.datasheet.model_copy(update={"confidential": True})}
    )
    _save_spec(spec, spec_path)

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )

    outside_store = [
        finding.field
        for finding in report.findings
        if finding.code == "confidential_artifact_outside_store"
    ]
    assert outside_store == ["datasheet.path", "datasheet.extraction_path"]
    assert (tmp_path / ".confidential" / ".gitignore").is_file()


def test_centered_count_prefix_is_removed_before_stacked_dimension_values() -> None:
    words = [
        PdfWord(text="0.30", x0=10, top=0, x1=20, bottom=5),
        PdfWord(text="16X", x0=0, top=2, x1=8, bottom=7),
        PdfWord(text="0.18", x0=10, top=8, x1=20, bottom=13),
    ]

    assert partspec_module._numbers_in_bbox(  # pyright: ignore[reportPrivateUsage]
        words, (0, 0, 20, 13), 16
    ) == {
        "0.3": 1,
        "0.18": 1,
    }


@pytest.mark.parametrize(
    ("notation", "kind"),
    [
        ("1.0 BSC", "basic"),
        ("1.0 TYP", "typical"),
        ("(1.0)", "reference"),
        ("1.0±0.1", "bilateral"),
    ],
)
def test_parse_dimension_kind_notation(notation: str, kind: str) -> None:
    assert parse_dimension_text(notation).kind == kind


def test_strict_models_and_dimension_bounds() -> None:
    with pytest.raises(ValidationError):
        Reading(page=0, vision="x", vision_record="record.json")
    with pytest.raises(ValidationError):
        Reading.model_validate(
            {
                "page": 1,
                "vision": "x",
                "vision_record": "record.json",
                "user_confirmed": "R1",
            }
        )
    with pytest.raises(ValidationError):
        Reading.model_validate(
            {
                "page": 1,
                "vision": "x",
                "vision_record": "record.json",
                "cells": {"invalid": {"table": 0, "row": 0, "col": 0}},
            }
        )
    with pytest.raises(ValidationError, match="at least one"):
        Dimension(reading=Reading(page=1, vision="1", vision_record="r.json"))
    with pytest.raises(ValidationError, match="min <= nom"):
        Dimension(
            min=2,
            nom=1,
            reading=Reading(
                page=1,
                bbox=(0, 0, 10, 10),
                vision="1",
                vision_record="r.json",
            ),
        )
    with pytest.raises(ValidationError, match="at least one dimension"):
        LandPattern(source="datasheet", pads=[], dimensions={})
    with pytest.raises(ValidationError):
        PartSpec.model_validate({"artifact_kind": "circuit_part_spec", "unknown": True})


def test_special_package_dimensions_are_checked_as_datasheet_readings(
    tmp_path: Path,
) -> None:
    spec, _extraction, _spec_path, _extraction_path = _fixture(tmp_path)

    def dimension(value: float) -> Dimension:
        return Dimension(
            nom=value,
            reading=Reading(
                page=1,
                bbox=(0.0, 0.0, 1.0, 1.0),
                vision=str(value),
                vision_record="fixture.json",
            ),
        )

    exposed = ExposedPad(
        number="2",
        length=dimension(0.8),
        width=dimension(0.7),
        center_x=dimension(-0.2),
        center_y=dimension(0.1),
    )
    tab = TabSpec(
        number="3",
        width=dimension(2.0),
        length=dimension(1.5),
        offset=dimension(3.0),
    )
    package = spec.package.model_copy(
        update={
            "exposed_pad": exposed,
            "exposed_pads": [
                exposed.model_copy(update={"number": "4"}),
            ],
            "tab": tab,
            "ball_grid": BallGrid(
                rows=["A"],
                columns=2,
                pitch_x=dimension(0.8),
                pitch_y=dimension(0.9),
                ball_diameter=dimension(0.4),
            ),
        }
    )
    fields = {
        field
        for field, _dimension in partspec_module._dimensions(  # pyright: ignore[reportPrivateUsage]
            spec.model_copy(update={"package": package})
        )
    }

    assert {
        "package.exposed_pad.center_x",
        "package.exposed_pad.center_y",
        "package.exposed_pads[0].center_x",
        "package.tab.width",
        "package.tab.length",
        "package.tab.offset",
        "package.ball_grid.pitch_x",
        "package.ball_grid.pitch_y",
        "package.ball_grid.ball_diameter",
    } <= fields


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


def test_partspec_gate_loads_vision_sidecars_in_a_fresh_process(tmp_path: Path) -> None:
    _spec, _extraction, spec_path, extraction_path = _fixture(tmp_path)
    derived, derived_dir = _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()]
    derived_path = tmp_path / "rederived-extraction.json"
    derived_path.write_text(derived.model_dump_json(), encoding="utf-8")
    code = (
        "import json,sys; from pathlib import Path; "
        "from circuit.datasheet import DatasheetExtraction; "
        "from circuit.partspec import check_part_spec,load_part_spec; "
        "import circuit.partspec as partspec; "
        "p=json.load(sys.stdin); "
        "derived=DatasheetExtraction.model_validate_json("
        "Path(p['derived']).read_text(encoding='utf-8')); "
        "partspec.rederive_pages=lambda *_args,**_kwargs:(derived,Path(p['derived_dir'])); "
        "spec_path=Path(p['spec']); extraction_path=Path(p['extraction']); "
        "extraction=DatasheetExtraction.model_validate_json("
        "extraction_path.read_text(encoding='utf-8')); "
        "report=check_part_spec(load_part_spec(spec_path),extraction,"
        "spec_path=spec_path,extraction_path=extraction_path); "
        "assert report.verdict == 'pass', report.model_dump_json()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(
            {
                "derived": str(derived_path),
                "derived_dir": str(derived_dir),
                "spec": str(spec_path),
                "extraction": str(extraction_path),
            }
        ),
        text=True,
        capture_output=True,
        cwd=Path(__file__).parents[1],
        check=False,
    )

    assert result.returncode == 0, result.stderr


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


def test_dimension_notation_kind_and_bound_table_cells_are_checked(
    tmp_path: Path,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.body_length.kind = "basic"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "kind_mismatch" in {finding.code for finding in report.findings}

    derived, derived_dir = _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()]
    tables_path = derived_dir / "page-001.tables.json"
    tables = json.loads(tables_path.read_text(encoding="utf-8"))
    tables["tables"][2]["rows"][1][1] = "2.8"
    tables_path.write_text(json.dumps(tables), encoding="utf-8")
    _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()] = (derived, derived_dir)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "cell_value_mismatch" in {finding.code for finding in report.findings}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("header", "cell_header_mismatch"),
        ("label", "cell_label_mismatch"),
        ("units", "cell_unit_mismatch"),
    ],
)
def test_dimension_cell_headers_labels_and_units_are_bound(
    tmp_path: Path,
    change: str,
    expected: str,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    derived, derived_dir = _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()]
    tables_path = derived_dir / "page-001.tables.json"
    tables = json.loads(tables_path.read_text(encoding="utf-8"))
    if change == "header":
        tables["tables"][2]["rows"][0][1] = "TYP mm"
    elif change == "label":
        tables["tables"][2]["rows"][1][0] = "E1"
    else:
        tables["tables"][2]["rows"][0][1] = "MIN inch"
    tables_path.write_text(json.dumps(tables), encoding="utf-8")

    if change in ("header", "units"):
        for lane in ("poppler", "pdfplumber"):
            words_path = derived_dir / f"page-001.{lane}.json"
            words = json.loads(words_path.read_text(encoding="utf-8"))
            next(word for word in words if word["text"] == "MIN mm")["text"] = (
                "TYP mm" if change == "header" else "MIN inch"
            )
            words_path.write_text(json.dumps(words), encoding="utf-8")
    elif change == "label":
        for lane in ("poppler", "pdfplumber"):
            words_path = derived_dir / f"page-001.{lane}.json"
            words = json.loads(words_path.read_text(encoding="utf-8"))
            next(word for word in words if word["text"] == "D")["text"] = "E1"
            words_path.write_text(json.dumps(words), encoding="utf-8")

    _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()] = (derived, derived_dir)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert expected in {finding.code for finding in report.findings}


def test_fresh_rederivation_overrides_stored_words_and_detects_staleness(
    tmp_path: Path,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    wrong_words = [PdfWord(text="999", x0=0, top=10, x1=10, bottom=20)]
    for lane_name in ("poppler", "pdfplumber"):
        (tmp_path / f"page-001.{lane_name}.json").write_text(
            json.dumps([word.model_dump(mode="json") for word in wrong_words]),
            encoding="utf-8",
        )
    (tmp_path / "page-001.tables.json").write_text("not-json", encoding="utf-8")
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert not any(
        finding.code == "mechanical_mismatch" and finding.field == "package.body_length"
        for finding in report.findings
    )

    derived, derived_dir = _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()]
    derived.pages[0].png_sha256 = "0" * 64
    _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()] = (derived, derived_dir)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "extraction_stale" in {finding.code for finding in report.findings}


def test_rederivation_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)

    def fail_rederive(
        _pdf_path: Path, _pages: Sequence[int], _dpi: int
    ) -> tuple[DatasheetExtraction, Path]:
        raise RuntimeError("renderer failed")

    monkeypatch.setattr(
        partspec_module,
        "rederive_pages",
        fail_rederive,
    )
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "rederivation_failed" in {finding.code for finding in report.findings}


def test_single_lane_and_invisible_mechanical_evidence_fail(
    tmp_path: Path,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    derived, derived_dir = _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()]
    derived.pages[0].lanes[1].status = "error"
    _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()] = (derived, derived_dir)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "mechanical_single_lane" in {finding.code for finding in report.findings}

    derived.pages[0].lanes[1].status = "ok"
    white_page = Image.new(
        "L",
        (math.ceil(200 * 300 / 72), math.ceil(200 * 300 / 72)),
        color=255,
    )
    ImageDraw.Draw(white_page).rectangle(
        (0, math.floor(10 * 300 / 72), math.ceil(10 * 300 / 72), math.ceil(20 * 300 / 72)),
        fill=0,
    )
    white_page.save(tmp_path / "page-001.png")
    white_page.save(derived_dir / "page-001.png")
    image_hash = hashlib.sha256((tmp_path / "page-001.png").read_bytes()).hexdigest()
    extraction.pages[0].png_sha256 = image_hash
    derived.pages[0].png_sha256 = image_hash
    record_ref = spec.package.pin1_reading.vision_record
    assert record_ref is not None
    record_path = tmp_path / record_ref
    record = build_review_record(
        tmp_path / "page-001.png",
        model="placeholder-vision-model",
        checklist="datasheet",
        impression=_IMPRESSION,
        findings=[],
    )
    record_path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
    _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()] = (derived, derived_dir)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "invisible_text" in {finding.code for finding in report.findings}


def test_dimension_reading_bbox_covers_referenced_cells_not_entire_table(
    tmp_path: Path,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert not any(
        finding.code == "cell_value_mismatch" and finding.field == "package.body_length"
        for finding in report.findings
    )

    spec.package.body_length.reading.bbox = (7, 47, 20, 61)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert any(
        finding.code == "cell_value_mismatch"
        and finding.field == "package.body_length"
        and "referenced table cell" in finding.message
        for finding in report.findings
    )


def test_dimension_contradictions_and_glyph_loss_remain_errors(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.body_width.reading.vision = "3.2 2.9"
    spec.package.height.reading.vision = "□0.8±0.1"
    spec.package.height.kind = "bilateral"
    spec.package.height.min = 0.7
    spec.package.height.nom = 0.8
    spec.package.height.max = 0.9
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    coded = {(finding.code, finding.field, finding.severity) for finding in report.findings}
    assert ("mechanical_mismatch", "package.body_width", "error") in coded
    assert ("mechanical_mismatch", "package.height", "error") in coded
    assert ("glyph_loss_ambiguous", "package.height", "error") in coded
    assert ("glyph_loss", "package.height", "warning") in coded
    assert (
        sum(
            finding.code == "glyph_loss" and finding.field == "package.height"
            for finding in report.findings
        )
        == 1
    )


def test_stacked_dimension_limits_require_maximum_above_minimum(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.body_length.reading.bbox = (0, 5, 11, 35)
    derived, derived_dir = _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()]
    scale = 300 / 72

    def set_order(maximum_top: int, minimum_top: int) -> None:
        image = Image.new(
            "L",
            (math.ceil(200 * scale), math.ceil(200 * scale)),
            color=255,
        )
        draw = ImageDraw.Draw(image)
        positions = {"3.1": maximum_top, "2.9": minimum_top}
        for lane in ("poppler", "pdfplumber"):
            words_path = derived_dir / f"page-001.{lane}.json"
            words = json.loads(words_path.read_text(encoding="utf-8"))
            for word in words:
                if word["text"] in positions and word["top"] < 30:
                    top = positions[word["text"]]
                    word.update({"x0": 0, "x1": 10, "top": top, "bottom": top + 10})
                    draw.rectangle(
                        (
                            0,
                            math.floor(top * scale),
                            math.ceil(10 * scale),
                            math.ceil((top + 10) * scale),
                        ),
                        fill=0,
                    )
            words_path.write_text(json.dumps(words), encoding="utf-8")
        image.save(tmp_path / "page-001.png")
        image.save(derived_dir / "page-001.png")
        image_hash = hashlib.sha256((tmp_path / "page-001.png").read_bytes()).hexdigest()
        extraction.pages[0].png_sha256 = image_hash
        derived.pages[0].png_sha256 = image_hash
        record = build_review_record(
            tmp_path / "page-001.png",
            model="placeholder-vision-model",
            checklist="datasheet",
            impression=_IMPRESSION,
            findings=[],
        )
        (tmp_path / "vision.advisory.json").write_text(
            record.model_dump_json(indent=2),
            encoding="utf-8",
        )

    set_order(10, 22)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "stacked_limit_order" not in {finding.code for finding in report.findings}

    set_order(22, 10)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "stacked_limit_order" in {finding.code for finding in report.findings}


def test_page_checks_pin_evidence_and_table_matching(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.pin1_reading.page = 2
    spec.pins[0].reading.page = 2
    spec.pins[0].reading.vision = "1"
    spec.pin_table.number_col = 1
    spec.pin_table.name_col = 0
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
    assert "pin_reading_page_mismatch" in codes
    assert "pin_table_bijection" in codes

    spec.pin_table.table = 9
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "pin_table_missing" in {finding.code for finding in report.findings}


def test_pin_table_range_expansion_and_lane_disagreement(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.pin_count = 3
    spec.pins = [
        spec.pins[0].model_copy(
            update={
                "number": str(number),
                "reading": spec.pins[0].reading.model_copy(update={"vision": f"{number} SW"}),
            }
        )
        for number in range(1, 4)
    ]
    spec.orderable[0].pin_count = 3
    derived, derived_dir = _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()]
    table_path = derived_dir / "page-001.tables.json"
    table_file = json.loads(table_path.read_text(encoding="utf-8"))
    table_file["tables"][0]["rows"][1][0] = "1-3"
    table_file["tables"][1]["rows"][1][2] = "3"
    table_path.write_text(json.dumps(table_file), encoding="utf-8")
    for lane in ("poppler", "pdfplumber"):
        words_path = derived_dir / f"page-001.{lane}.json"
        words = json.loads(words_path.read_text(encoding="utf-8"))
        for word in words:
            if word["x0"] == 60:
                word["text"] = "1-3"
            elif word["x0"] == 140:
                word["text"] = "3"
        words_path.write_text(json.dumps(words), encoding="utf-8")
    _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()] = (derived, derived_dir)
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {finding.code for finding in report.findings}
    assert "pin_table_bijection" not in codes
    assert "pin_numbering_incomplete" not in codes

    words_path = derived_dir / "page-001.poppler.json"
    words = json.loads(words_path.read_text(encoding="utf-8"))
    next(word for word in words if word["x0"] == 80)["text"] = "OTHER"
    words_path.write_text(json.dumps(words), encoding="utf-8")
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "table_lane_disagreement" in {finding.code for finding in report.findings}

    words = json.loads(words_path.read_text(encoding="utf-8"))
    next(word for word in words if word["x0"] == 80)["text"] = "SW"
    next(word for word in words if word["x0"] == 60)["text"] = "2"
    words_path.write_text(json.dumps(words), encoding="utf-8")
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "table_lane_disagreement" in {finding.code for finding in report.findings}


def test_pin_table_number_column_disambiguation_is_package_bound(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    derived, derived_dir = _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()]
    tables_path = derived_dir / "page-001.tables.json"
    tables = json.loads(tables_path.read_text(encoding="utf-8"))
    tables["tables"][0]["rows"] = [["X PIN", "NAME", "Y PIN"], ["1", "SW", "9"]]
    tables_path.write_text(json.dumps(tables), encoding="utf-8")
    _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()] = (derived, derived_dir)

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "pin_table_column_ambiguous" in {finding.code for finding in report.findings}

    spec.pin_table.column_designator = "X"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "pin_table_column_ambiguous" not in {finding.code for finding in report.findings}
    assert "pin_table_column_mismatch" not in {finding.code for finding in report.findings}

    spec.pin_table.number_col = 2
    spec.pin_table.column_designator = "Y"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "pin_table_column_mismatch" in {finding.code for finding in report.findings}


def test_drawing_identifier_must_appear_in_both_lanes(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    derived, derived_dir = _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()]
    for lane in ("poppler", "pdfplumber"):
        words_path = derived_dir / f"page-001.{lane}.json"
        words = json.loads(words_path.read_text(encoding="utf-8"))
        words = [word for word in words if word["text"] != "X"]
        words_path.write_text(json.dumps(words), encoding="utf-8")
    _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()] = (derived, derived_dir)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "drawing_id_missing" in {finding.code for finding in report.findings}


def test_mechanical_lane_unavailable_and_unparseable_vision(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.body_length.reading.vision = "not a dimension"
    extraction.pages[0].lanes = [
        lane.model_copy(update={"status": "error"}) for lane in extraction.pages[0].lanes
    ]
    derived, derived_dir = _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()]
    derived.pages[0].lanes = [
        lane.model_copy(update={"status": "error"}) for lane in derived.pages[0].lanes
    ]
    _REDERIVED_BY_PDF[(tmp_path / "parts.pdf").resolve()] = (derived, derived_dir)
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {finding.code for finding in report.findings}
    assert "vision_unparseable" in codes
    assert "mechanical_mismatch" in codes


def test_consistency_errors_cover_pin_family_pitch_pad_and_height(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.family = "no_lead_quad"
    spec.package.pin_count = 10
    spec.package.pins_per_side = (3, 3, 3, 3)
    spec.package.pitch = Dimension(
        nom=4,
        reading=Reading(
            page=1,
            bbox=(0, 9, 11, 21),
            vision="4",
            vision_record="vision.advisory.json",
        ),
    )
    spec.package.exposed_pad = ExposedPad(
        number="17",
        length=Dimension(
            nom=4,
            reading=Reading(
                page=1,
                bbox=(0, 9, 11, 21),
                vision="4",
                vision_record="vision.advisory.json",
            ),
        ),
        width=Dimension(
            nom=4,
            reading=Reading(
                page=1,
                bbox=(0, 9, 11, 21),
                vision="4",
                vision_record="vision.advisory.json",
            ),
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


def test_dimensions_and_pin1_readings_require_bounding_boxes() -> None:
    with pytest.raises(ValidationError, match="requires a bounding box"):
        Dimension(
            nom=1.0,
            reading=Reading(
                page=1,
                vision="1.0",
                vision_record="vision.json",
            ),
        )
    with pytest.raises(ValidationError, match="requires a bounding box"):
        PackageSpec(
            family="custom",
            drawing_id="X",
            pin_count=1,
            body_length=Dimension(
                nom=1,
                reading=Reading(
                    page=1,
                    bbox=(0, 0, 1, 1),
                    vision="1",
                    vision_record="vision.json",
                ),
            ),
            body_width=Dimension(
                nom=1,
                reading=Reading(
                    page=1,
                    bbox=(0, 0, 1, 1),
                    vision="1",
                    vision_record="vision.json",
                ),
            ),
            height=Dimension(
                nom=1,
                reading=Reading(
                    page=1,
                    bbox=(0, 0, 1, 1),
                    vision="1",
                    vision_record="vision.json",
                ),
            ),
            drawing_view="top",
            pin1_corner="top_left",
            pin1_reading=Reading(
                page=1,
                vision="top left",
                vision_record="vision.json",
            ),
        )


def test_quad_side_counts_and_axis_specific_pitch_bounds(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.family = "no_lead_quad"
    spec.package.pin_count = 16
    spec.package.pins_per_side = (4, 4, 4, 4)
    spec.package.pitch = Dimension(
        nom=1.5,
        reading=Reading(
            page=1,
            bbox=(0, 9, 11, 21),
            vision="1.5",
            vision_record="vision.advisory.json",
        ),
    )
    spec.package.body_length = Dimension(
        nom=8,
        reading=Reading(
            page=1,
            bbox=(0, 9, 11, 21),
            vision="8",
            vision_record="vision.advisory.json",
        ),
    )
    spec.package.body_width = Dimension(
        nom=4,
        reading=Reading(
            page=1,
            bbox=(0, 9, 11, 21),
            vision="4",
            vision_record="vision.advisory.json",
        ),
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
    spec.orderable[0].package_designator = "Y"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {item.code for item in report.findings}
    assert "orderable_designator_mismatch" in codes
    assert "package_variant_unbound" not in codes

    spec.orderable[0].mpn = "OTHER-MPN"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "package_variant_unbound" in {item.code for item in report.findings}
    spec.orderable[0].mpn = "EXAMPLE-1"

    spec.orderable[0].package_designator = "X"
    spec.orderable[0].pin_count = 2
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "orderable_pin_count_mismatch" in {item.code for item in report.findings}

    spec.orderable[0].pin_count = 1
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


@pytest.mark.parametrize(
    ("vision_rows", "expected_column_shift", "expected_missing", "expected_extra"),
    [
        (
            [["Pin No.", "Package", "Function"], ["1", "QFN", "SW"]],
            True,
            [("1", "SW")],
            [("1", "QFN")],
        ),
        ([["Pin No.", "Function"]], False, [("1", "SW")], []),
        (
            [["Pin No.", "Function"], ["1", "SW"], ["1", "SW"]],
            False,
            [],
            [("1", "SW")],
        ),
    ],
)
def test_pin_table_vision_reports_column_shift_missing_and_duplicate_rows(
    tmp_path: Path,
    vision_rows: list[list[str]],
    expected_column_shift: bool,
    expected_missing: list[tuple[str, str]],
    expected_extra: list[tuple[str, str]],
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    assert spec.pin_table.vision_read is not None
    _replace_vision_answer(
        spec_path.parent,
        spec.pin_table.vision_read,
        answer=json.dumps(vision_rows),
        normalized=vision_rows,
    )
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )

    finding = next(
        item
        for item in report.findings
        if item.code == "vision_table_mismatch" and item.field == "pin_table"
    )
    assert f"column_shift={expected_column_shift}" in finding.message
    assert f"missing={expected_missing}" in finding.message
    assert f"extra={expected_extra}" in finding.message


def test_orderable_vision_requires_one_row_per_mpn_and_package_code(
    tmp_path: Path,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    assert spec.orderable_vision_read is not None
    for rows in (
        [],
        [["EXAMPLE-1", "X"], ["EXAMPLE-1", "X"]],
        [["EXAMPLE-1", "Y"]],
        [["EXAMPLE-10", "X"]],
    ):
        _replace_vision_answer(
            spec_path.parent,
            spec.orderable_vision_read,
            answer=json.dumps(rows),
            normalized=rows,
        )
        report = check_part_spec(
            spec,
            extraction,
            spec_path=spec_path,
            extraction_path=extraction_path,
        )
        assert any(
            item.code == "vision_table_mismatch" and item.field == "orderable"
            for item in report.findings
        ), f"rows={rows!r}, findings={report.findings!r}"


def test_missing_table_vision_refs_fail_closed(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.pin_table.vision_read = None
    spec.orderable_vision_read = None
    _save_spec(spec, spec_path)

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )

    assert {
        (item.code, item.field) for item in report.findings if item.code == "vision_read_missing"
    } >= {("vision_read_missing", "pin_table"), ("vision_read_missing", "orderable")}


@pytest.mark.parametrize(
    ("impression", "clear_impression"),
    [
        ("", False),
        ("This is too short. It remains too short.", False),
        (
            "The image is clear, the printed values are legible, the table has consistent "
            "spacing, no characters appear clipped, and the surrounding marks provide enough "
            "context to read each label without uncertainty or needing to infer an obscured value, "
            "while the close crop still preserves the row boundaries, column alignment, header "
            "position, punctuation, spacing, and the distinction between each symbol and number.",
            False,
        ),
        (None, True),
    ],
)
def test_partspec_rejects_missing_or_invalid_stored_vision_impressions(
    tmp_path: Path,
    impression: str | None,
    clear_impression: bool,
) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    reading = spec.package.body_length.reading
    assert reading.vision_read is not None
    _replace_vision_answer(
        spec_path.parent,
        reading.vision_read,
        impression=impression,
        clear_impression=clear_impression,
    )

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )

    assert any(
        item.code == "vision_impression_missing" and item.field == "package.body_length"
        for item in report.findings
    )


def test_pinout_vision_must_match_freshly_derived_geometry(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _pinout_fixture(tmp_path)
    assert spec.pinout is not None
    assert spec.pinout.labels_vision_read is not None
    mismatched_labels = dict(spec.pinout.labels_vision)
    mismatched_labels["1"] = "MISMATCH"
    _replace_vision_answer(
        spec_path.parent,
        spec.pinout.labels_vision_read,
        answer=json.dumps(mismatched_labels),
        normalized=mismatched_labels,
    )

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )

    assert "vision_pinout_mismatch" in {item.code for item in report.findings}
    assert any(
        item.code == "vision_read_mismatch" and item.field == "pinout.labels_vision"
        for item in report.findings
    )


def test_pinout_vision_labels_match_geometry_on_happy_path(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _pinout_fixture(tmp_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )

    assert not any(
        item.code in {"vision_pinout_mismatch", "vision_read_mismatch"}
        and item.field in {"pinout", "pinout.labels_vision"}
        for item in report.findings
    )


def test_orderable_combined_package_and_pin_cell_matches_both_lanes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, extraction, _spec_path, extraction_path = _fixture(tmp_path)
    tables_path = tmp_path / "page-001.tables.json"
    table_data = json.loads(tables_path.read_text(encoding="utf-8"))
    table_data["tables"][1]["rows"][1] = ["EXAMPLE-1", "Active", "Production", "X | 1"]
    table_data["tables"][1]["cells"][1] = [
        [95, 0, 115, 30],
        [115, 0, 125, 30],
        [125, 0, 135, 30],
        [135, 0, 155, 30],
    ]
    tables_path.write_text(json.dumps(table_data), encoding="utf-8")

    def cell_text(
        _extraction: DatasheetExtraction,
        _extraction_dir: Path,
        _page: PageExtraction,
        _table: dict[str, object],
        _rows: list[list[str | None]],
        _row_index: int,
        col_index: int,
        **_kwargs: object,
    ) -> tuple[str | None, str | None]:
        values = ["EXAMPLE-1", "Active", "Production", "X | 1"]
        return values[col_index], values[col_index]

    monkeypatch.setattr(partspec_module, "_cell_lane_texts", cell_text)
    findings: list[SpecFinding] = []
    partspec_module._orderable_row_checks(  # pyright: ignore[reportPrivateUsage]
        spec,
        spec.orderable[0],
        0,
        extraction,
        extraction_path.parent,
        extraction.pages[0],
        findings,
    )
    assert not any(
        finding.code
        in {
            "orderable_designator_mismatch",
            "orderable_pin_count_mismatch",
        }
        for finding in findings
    )


def test_unprinted_exposed_pad_number_uses_bound_pin_name(tmp_path: Path) -> None:
    spec, extraction, _spec_path, _extraction_path = _fixture(tmp_path)
    spec.package.exposed_pad = ExposedPad(
        number="17",
        length=spec.package.body_length,
        width=spec.package.body_width,
    )
    pin = PinSpec(
        number="17",
        name="Exposed Thermal Pad",
        electrical_type="passive",
        reading=Reading(
            page=1,
            vision="Exposed Thermal Pad",
            vision_record="vision.advisory.json",
        ),
    )
    findings: list[SpecFinding] = []
    partspec_module._pin_checks(  # pyright: ignore[reportPrivateUsage]
        spec, tmp_path, pin, 0, extraction.pages[0], findings
    )
    assert not any(item.code == "vision_unparseable" for item in findings)


def test_orderable_mechanical_mismatch_cannot_be_downgraded(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.orderable[0].mpn = "NOT-EXAMPLE"
    _save_spec(spec, spec_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert any(
        item.code == "orderable_mpn_mismatch"
        and item.field == "orderable[0]"
        and item.severity == "error"
        for item in report.findings
    )
    assert "mechanical_unconfirmed_user_confirmed" not in {item.code for item in report.findings}


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


def test_pinout_view_reading_requires_a_bbox_on_the_pinout_page() -> None:
    with pytest.raises(ValidationError, match="requires a bounding box"):
        PinoutDrawing(
            page=3,
            bbox=(0, 0, 20, 20),
            view="top",
            view_reading=Reading(
                page=3,
                vision="Top View",
                vision_record="vision.json",
            ),
            labels_vision={"1": "SW"},
            vision_record="vision.json",
        )
    with pytest.raises(ValidationError, match="page must match"):
        PinoutDrawing(
            page=3,
            bbox=(0, 0, 20, 20),
            view="top",
            view_reading=Reading(
                page=2,
                bbox=(0, 0, 20, 20),
                vision="Top View",
                vision_record="vision.json",
            ),
            labels_vision={"1": "SW"},
            vision_record="vision.json",
        )
    pinout_value = pinout_drawing(QUAD16_NAMES).model_dump()
    pinout_value["extra"] = "forbidden"
    with pytest.raises(ValidationError):
        PinoutDrawing.model_validate(pinout_value)


def test_required_pinout_is_freshly_derived_with_names_and_ccw_winding(
    tmp_path: Path,
) -> None:
    spec, extraction, spec_path, extraction_path = _pinout_fixture(tmp_path)
    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )

    assert report.pinout is not None
    assert report.pinout.winding == "ccw"
    assert {label.number: label.name for label in report.pinout.labels} == QUAD16_NAMES
    assert not {
        finding.code
        for finding in report.findings
        if finding.field == "pinout"
        and finding.code
        in {
            "pinout_view_unverified",
            "pinout_number_missing",
            "pinout_number_duplicate",
            "pinout_name_unresolved",
            "pinout_name_ambiguous",
            "pinout_name_mismatch",
            "pinout_vision_mismatch",
            "pinout_pin1_corner_mismatch",
        }
    }


def test_pinout_name_shift_reports_the_expected_permutation(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _pinout_fixture(tmp_path)
    for pin in spec.pins:
        next_number = int(pin.number) % len(QUAD16_NAMES) + 1
        pin.name = QUAD16_NAMES[str(next_number)]
    _save_spec(spec, spec_path)

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {item.code for item in report.findings}
    diagnosis = next(
        item.message for item in report.findings if item.code == "pinout_permutation_diagnosis"
    )

    assert "pinout_name_mismatch" in codes
    assert "shift+1" in diagnosis


def test_pinout_mirrored_names_report_mirror_x(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _pinout_fixture(tmp_path)
    _, positions, _ = quad16_fixture()
    normalized_positions = normalized(positions)
    for pin in spec.pins:
        x, y = normalized_positions[pin.number]
        mirrored = min(
            normalized_positions,
            key=lambda number: math.dist(
                (-x, y),
                normalized_positions[number],
            ),
        )
        pin.name = QUAD16_NAMES[mirrored]
    _save_spec(spec, spec_path)

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    codes = {item.code for item in report.findings}
    diagnosis = next(
        item.message for item in report.findings if item.code == "pinout_permutation_diagnosis"
    )

    assert "pinout_name_mismatch" in codes
    assert "mirror_x" in diagnosis


def test_bottom_view_normalizes_to_the_same_top_view_geometry(tmp_path: Path) -> None:
    top_spec, top_extraction, top_spec_path, top_extraction_path = _pinout_fixture(tmp_path / "top")
    bottom_spec, bottom_extraction, bottom_spec_path, bottom_extraction_path = _pinout_fixture(
        tmp_path / "bottom", page_view="bottom"
    )

    top_report = check_part_spec(
        top_spec,
        top_extraction,
        spec_path=top_spec_path,
        extraction_path=top_extraction_path,
    )
    bottom_report = check_part_spec(
        bottom_spec,
        bottom_extraction,
        spec_path=bottom_spec_path,
        extraction_path=bottom_extraction_path,
    )

    assert top_report.pinout is not None
    assert bottom_report.pinout is not None
    assert top_report.pinout.winding == bottom_report.pinout.winding == "ccw"
    assert [(label.number, label.name, label.x, label.y) for label in top_report.pinout.labels] == [
        (label.number, label.name, label.x, label.y) for label in bottom_report.pinout.labels
    ]


def test_wrong_declared_pinout_view_and_vision_labels_are_reported(
    tmp_path: Path,
) -> None:
    wrong_view_spec, wrong_view_extraction, wrong_spec_path, wrong_extraction_path = (
        _pinout_fixture(
            tmp_path / "wrong-view",
            page_view="top",
            declared_view="bottom",
        )
    )
    wrong_view_report = check_part_spec(
        wrong_view_spec,
        wrong_view_extraction,
        spec_path=wrong_spec_path,
        extraction_path=wrong_extraction_path,
    )
    assert "pinout_view_unverified" in {item.code for item in wrong_view_report.findings}
    assert "pinout_pin1_corner_mismatch" in {item.code for item in wrong_view_report.findings}

    vision_spec, vision_extraction, vision_spec_path, vision_extraction_path = _pinout_fixture(
        tmp_path / "vision", vision_mismatch=True
    )
    vision_report = check_part_spec(
        vision_spec,
        vision_extraction,
        spec_path=vision_spec_path,
        extraction_path=vision_extraction_path,
    )
    assert "pinout_vision_mismatch" in {item.code for item in vision_report.findings}


def test_top_view_clockwise_pinout_warns(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _pinout_fixture(
        tmp_path,
        mirror_page_x=True,
    )
    spec.package.pin1_corner = "top_right"
    _save_spec(spec, spec_path)

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )

    assert report.pinout is not None
    assert report.pinout.winding == "cw"
    finding = next(item for item in report.findings if item.code == "pinout_winding_nonstandard")
    assert finding.severity == "warning"


def test_required_pinout_missing_but_chip_pinout_is_optional(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _fixture(tmp_path)
    spec.package.family = "gullwing_dual"
    required = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "pinout_missing" in {item.code for item in required.findings}

    spec.package.family = "chip"
    optional = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )
    assert "pinout_missing" not in {item.code for item in optional.findings}


def test_pinout_page_missing_from_fresh_derivation_is_unverified(tmp_path: Path) -> None:
    spec, extraction, spec_path, extraction_path = _pinout_fixture(tmp_path)
    pdf_path = (spec_path.parent / spec.datasheet.path).resolve()
    derived, derived_dir = _REDERIVED_BY_PDF[pdf_path]
    _REDERIVED_BY_PDF[pdf_path] = (derived.model_copy(update={"pages": []}), derived_dir)

    report = check_part_spec(
        spec,
        extraction,
        spec_path=spec_path,
        extraction_path=extraction_path,
    )

    assert report.pinout is None
    assert "pinout_unverified" in {item.code for item in report.findings}
