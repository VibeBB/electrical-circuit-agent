import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Literal, cast
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from circuit import libreview
from circuit.datasheet import DatasheetExtraction, PageExtraction
from circuit.landpattern import Density, LandPatternResult
from circuit.libitems import FootprintDef, PadDef, SymbolDef, SymPin
from circuit.libverify import (
    LibraryVerification,
    VerificationInputs,
    VerifiedFootprint,
    VerifiedModel,
    VerifiedSymbol,
    VerifyFinding,
)
from circuit.lineage import (
    EvidenceRef,
    FootprintBase,
    FootprintLineage,
    PadChange,
    lineage_path_for,
)
from circuit.partspec import (
    CellRef,
    DatasheetRef,
    Dimension,
    ExposedPad,
    LandPad,
    LandPattern,
    OrderableVariant,
    PackageSpec,
    PartSpec,
    PartSpecReport,
    PinSpec,
    PinTable,
    Reading,
)
from pinout_fixtures import geometry_for_names, pinout_drawing


def _reading(
    vision: str, bbox: tuple[float, float, float, float] | None = (10, 10, 30, 20)
) -> Reading:
    return Reading(page=1, bbox=bbox, vision=vision, vision_record="vision.json")


def _spec(pdf_sha256: str = hashlib.sha256(b"pdf").hexdigest()) -> PartSpec:
    def dimension(value: float, label: str) -> Dimension:
        return Dimension(nom=value, label=label, reading=_reading(str(value)))

    package = PackageSpec(
        family="gullwing_dual",
        drawing_id="SOIC-4",
        pin_count=6,
        pitch=dimension(1.0, "pitch"),
        body_length=dimension(4.0, "length"),
        body_width=dimension(2.0, "width"),
        height=dimension(1.0, "height"),
        lead_span=dimension(5.0, "span"),
        lead_length=dimension(0.5, "lead length"),
        lead_width=dimension(0.3, "lead width"),
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=_reading("Pin 1 at top-left"),
    )
    reading = _reading("0.5 pitch")
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="TEST-4",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="part.pdf",
            sha256=pdf_sha256,
            revision="A",
            extraction_path="extraction.json",
        ),
        package=package,
        pinout=pinout_drawing({str(number): f"SIG{number}" for number in range(1, 7)}),
        land_pattern=LandPattern(
            source="datasheet",
            dimensions={"pitch": Dimension(nom=1.0, reading=reading)},
            pads=[
                LandPad(
                    number=str(number),
                    x=-2.0 if number in {1, 2} else 2.0,
                    y=-0.5 if number in {1, 4} else 0.5,
                    width=0.8,
                    height=0.4,
                    shape="rect",
                )
                for number in range(1, 7)
            ],
        ),
        pins=[
            PinSpec(
                number=str(number),
                name=f"SIG{number}",
                electrical_type="passive",
                reading=_reading(f"{number} SIG{number}"),
            )
            for number in range(1, 7)
        ],
        orderable=[
            OrderableVariant(
                mpn="TEST-4",
                package_designator="SOIC-4",
                pin_count=6,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading("TEST-4 SOIC-4"),
            )
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
    )


def _message(
    packet: str, *, decision: str = "approve", answers: dict[str, str] | None = None
) -> str:
    lines = [
        f"CIRCUIT-LIBRARY-REVIEW {packet}",
        f"decision: {decision}",
        "reviewer: Test Reviewer",
    ]
    if answers is not None:
        lines.extend(f"answer: {question} = {value}" for question, value in answers.items())
    return "\n".join(lines)


def _write_event(
    tmp_path: Path,
    library_dir: Path,
    packet: str,
    content: str,
    *,
    source: str = "user",
    suffix: str = "1",
    pointer_packet: str | None = None,
) -> Path:
    packet_dir = library_dir / "reviews" / _spec().mpn / packet
    packet_dir.mkdir(parents=True, exist_ok=True)
    packet_document = packet_dir / "review.json"
    if not packet_document.exists():
        packet_document.write_text(
            json.dumps(
                {
                    "artifact_kind": "circuit_library_review_packet",
                    "packet_id": packet,
                    "blind_questions": [],
                    "vision_review_images": [],
                }
            ),
            encoding="utf-8",
        )
    events = tmp_path / "events"
    events.mkdir(exist_ok=True)
    event_path = events / f"event-{suffix}.json"
    raw = json.dumps({"source": source, "message": {"content": content}}).encode()
    event_path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    decisions = library_dir / "reviews" / "decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    pointer_id = packet if pointer_packet is None else pointer_packet
    pointer = decisions / f"{pointer_id}.{digest[:12]}.json"
    pointer.write_text(
        json.dumps(
            {
                "artifact_kind": "circuit_library_review_pointer",
                "packet_id": pointer_id,
                "event_path": str(event_path),
                "event_sha256": digest,
                "recorded_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return event_path


def _expected_answers(spec: PartSpec, packet: str) -> dict[str, str]:
    return {item.question_id: item.expected for item in libreview.blind_questions(spec, packet)}


def test_packet_id_is_stable_and_binds_every_artifact_input() -> None:
    values: dict[str, Any] = {
        "pdf_sha256": "a" * 64,
        "part_spec_sha256": "b" * 64,
        "symbol_lib_sha256": "c" * 64,
        "symbol_name": "Part",
        "footprint_sha256": "d" * 64,
        "model_sha256s": ["f" * 64, "e" * 64],
        "density": "nominal",
        "tolerance_mm": 0.02,
        "model_required": True,
        "authoring_sha256s": ["f" * 64, "e" * 64],
        "lineage_sha256": None,
        "rule_chain_sha256": "0" * 64,
    }

    def packet_for(fields: dict[str, Any]) -> str:
        return libreview.packet_id(
            pdf_sha256=cast(str, fields["pdf_sha256"]),
            part_spec_sha256=cast(str, fields["part_spec_sha256"]),
            symbol_lib_sha256=cast(str, fields["symbol_lib_sha256"]),
            symbol_name=cast(str, fields["symbol_name"]),
            footprint_sha256=cast(str, fields["footprint_sha256"]),
            model_sha256s=cast(list[str], fields["model_sha256s"]),
            density=cast(Density, fields["density"]),
            tolerance_mm=cast(float, fields["tolerance_mm"]),
            model_required=cast(bool, fields["model_required"]),
            authoring_sha256s=cast(list[str], fields["authoring_sha256s"]),
            lineage_sha256=cast(str | None, fields["lineage_sha256"]),
            rule_chain_sha256=cast(str | None, fields["rule_chain_sha256"]),
        )

    first = packet_for(values)
    assert first == packet_for({**values, "model_sha256s": ["e" * 64, "f" * 64]})
    assert first == packet_for({**values, "authoring_sha256s": ["e" * 64, "f" * 64]})
    assert len(first) == 16
    for field, changed in (
        ("pdf_sha256", "0" * 64),
        ("part_spec_sha256", "0" * 64),
        ("symbol_lib_sha256", "0" * 64),
        ("symbol_name", "Other"),
        ("footprint_sha256", "0" * 64),
        ("model_sha256s", ["0" * 64]),
        ("density", "least"),
        ("tolerance_mm", 0.03),
        ("model_required", False),
        ("authoring_sha256s", ["0" * 64]),
        ("lineage_sha256", "1" * 64),
        ("rule_chain_sha256", "2" * 64),
    ):
        assert packet_for({**values, field: changed}) != first


def test_blind_questions_select_pins_deterministically_and_omit_expected(tmp_path: Path) -> None:
    spec = _spec()
    packet = "1" * 16
    questions = libreview.blind_questions(spec, packet)
    assert questions == libreview.blind_questions(spec, packet)
    assert [item.question_id for item in questions[:5]] == [
        "drawing_id",
        "drawing_view",
        "pin1_corner",
        "pin_count",
        "exposed_pad",
    ]
    selected = sorted(
        ("2", "3", "4", "5"),
        key=lambda number: hashlib.sha256(f"{packet}{number}".encode()).digest(),
    )[:2]
    assert {item.question_id for item in questions[5:]} == {
        "pinout.view",
        "pin.1",
        "pin.6",
        *(f"pin.{number}" for number in selected),
    }
    private_api: Any = libreview
    blind = tmp_path / "01-blind.html"
    blind.write_text(
        private_api._blind_html(packet, questions, {}, spec, tmp_path),
        encoding="utf-8",
    )
    assert "SIG1" not in blind.read_text(encoding="utf-8")


def test_blind_questions_do_not_sample_the_exposed_pad_pin() -> None:
    spec = _spec()
    exposed_pad = ExposedPad(
        number="6",
        length=Dimension(nom=0.8, reading=_reading("0.8")),
        width=Dimension(nom=0.8, reading=_reading("0.8")),
    )
    spec = spec.model_copy(
        update={"package": spec.package.model_copy(update={"exposed_pad": exposed_pad})}
    )

    questions = libreview.blind_questions(spec, "a" * 16)
    pin_questions = {item.question_id for item in questions if item.question_id.startswith("pin.")}

    assert "pin.6" not in pin_questions
    assert "pin.5" in pin_questions
    private_api: Any = libreview
    assert private_api._question_crop_field("pin.2", spec) == "pin_table"
    assert private_api._question_crop_field("pinout.view", spec) == "pinout"


def test_blind_questions_skip_pins_without_a_numbered_row_and_include_disagreements() -> None:
    spec = _spec()
    spec.pins.append(
        PinSpec(
            number="17",
            name="Exposed Thermal Pad",
            electrical_type="passive",
            reading=_reading("Exposed Thermal Pad"),
        )
    )
    comparison = libreview.authoring.AuthoringComparison(
        artifact_kind="circuit_part_authoring_comparison",
        run_dir="authoring/run",
        sealed={"a": "a" * 64, "b": "b" * 64},
        models={"a": "model-a", "b": "model-b"},
        profiles={"a": "profile-a", "b": "profile-b"},
        impressions={"a": "A impression", "b": "B impression"},
        rasterizers={"a": ["pdftoppm"], "b": ["pdfium"]},
        agreed=[],
        disagreements=[
            libreview.authoring.AuthoringDisagreement(
                pointer="/package/pitch/nom",
                a=0.5,
                b=0.6,
            )
        ],
        model_diversity="distinct",
        issues=[],
    )

    questions = libreview.blind_questions(spec, "a" * 16, comparison)
    question = next(item for item in questions if item.question_id == "authoring.0")
    pin_questions = {item.question_id for item in questions if item.question_id.startswith("pin.")}

    assert "pin.17" not in pin_questions
    assert question.evidence_field == "package.pitch"
    assert spec.package.pitch is not None
    assert question.bbox == spec.package.pitch.reading.bbox


def test_vision_comparison_mismatch_adds_mandatory_rejection_question(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vision = libreview.visionread
    item = vision.VisionReadItem(
        read_id="compare01",
        field="library.footprint",
        kind="compare_footprint",
        page=2,
        bbox=(10, 20, 30, 40),
        crop_bbox=(8, 18, 32, 42),
        dpi=300,
        rasterizer="pdftoppm",
        image_path="images/compare01.png",
        image_sha256="a" * 64,
        prompt=vision.prompt_for_kind("compare_footprint"),
        prompt_sha256="b" * 64,
        bindings={
            "part_spec_sha256": "c" * 64,
            "artifact_sha256": "d" * 64,
            "artifact_kind": "footprint",
        },
    )
    batch = vision.VisionBatch(
        artifact_kind="circuit_vision_read_batch",
        batch_id="batch01",
        created_at="2026-01-01T00:00:00Z",
        lane="main",
        profile="profile",
        model="model",
        pdf_path="part.pdf",
        pdf_sha256="e" * 64,
        items=[item],
        control_salt="salt",
        control_answer_sha256="f" * 64,
        control_read_sha256="1" * 64,
    )
    answers = vision.VisionAnswerRecord(
        artifact_kind="circuit_vision_read_answers",
        batch_id=batch.batch_id,
        answered_at="2026-01-01T00:00:00Z",
        answers={item.read_id: "{}"},
        impressions={item.read_id: "A clear and useful view. Some details remain uncertain."},
        normalized={
            item.read_id: {
                "pin1_matches": True,
                "arrangement_matches": False,
                "numbering_direction_matches": True,
                "differences": ["pad order differs"],
            }
        },
        status={item.read_id: "ok"},
        control_passed=True,
    )
    evidence = vision.VisionComparisonEvidence(
        batch_path=tmp_path / "batch.json",
        batch=batch,
        item=item,
        answers=answers,
        normalized={
            "pin1_matches": True,
            "arrangement_matches": False,
            "numbering_direction_matches": True,
            "differences": ["pad order differs"],
        },
        impression=answers.impressions[item.read_id],
        impression_valid=True,
    )
    questions = libreview.blind_questions(
        _spec(),
        "b" * 16,
        comparison_evidence=[evidence],
    )
    question = next(
        item for item in questions if item.question_id == "vision.compare_footprint.compare01"
    )
    assert question.expected == "yes"
    assert "pad order differs" in question.prompt
    assert question.evidence_field == "comparison.footprint.compare01"

    monkeypatch.setenv(libreview.EVENTS_DIR_ENV, str(tmp_path / "events"))
    library = tmp_path / "library"
    packet = "b" * 16
    packet_json = library / "reviews" / _spec().mpn / packet / "review.json"
    packet_json.parent.mkdir(parents=True, exist_ok=True)
    packet_json.write_text(
        json.dumps(
            {
                "artifact_kind": "circuit_library_review_packet",
                "packet_id": packet,
                "blind_questions": [question.model_dump(mode="json")],
                "vision_review_images": [],
            }
        ),
        encoding="utf-8",
    )
    answers_by_question = _expected_answers(_spec(), packet)
    answers_by_question[question.question_id] = "no"
    _write_event(
        tmp_path,
        library,
        packet,
        _message(packet, answers=answers_by_question),
    )
    status = libreview.review_status(library, _spec(), packet)
    assert status.state == "rejected"


def test_unlabelled_exposed_pad_row_maps_to_the_part_symbol_and_footprint_pin(
    tmp_path: Path,
) -> None:
    spec = _spec()
    exposed_pad = ExposedPad(
        number="6",
        length=Dimension(nom=0.8, reading=_reading("0.8")),
        width=Dimension(nom=0.8, reading=_reading("0.8")),
    )
    spec = spec.model_copy(
        update={"package": spec.package.model_copy(update={"exposed_pad": exposed_pad})}
    )
    pdf_path = tmp_path / "part.pdf"
    pdf_path.write_bytes(b"pdf")
    extraction_dir = tmp_path / "evidence"
    extraction = _make_extraction(pdf_path, extraction_dir)
    table_path = extraction_dir / "page-001.tables.json"
    table_document = json.loads(table_path.read_text(encoding="utf-8"))
    table = table_document["tables"][0]
    table["rows"].append(["", "Exposed Thermal Pad"])
    table["cells"].append([[0, 50, 10, 60], [10, 50, 40, 60]])
    table_path.write_text(json.dumps(table_document), encoding="utf-8")

    private_api: Any = libreview
    rows = private_api._pin_table_rows(spec, extraction, extraction_dir)
    symbol = SymbolDef(
        name="Example",
        pins=[
            SymPin(
                number="6",
                name="EP",
                electrical_type="passive",
                x=0,
                y=0,
                length=1,
                orientation=0,
                unit=1,
            )
        ],
        properties={},
    )
    footprint = FootprintDef(
        name="Example",
        attributes=["smd"],
        pads=[
            PadDef(
                number="6",
                type="smd",
                shape="rect",
                x=0,
                y=0,
                rotation=0,
                width=1,
                height=1,
                drill=None,
                layers=["F.Cu"],
            )
        ],
        graphics=[],
        models=[],
        properties={},
    )

    row = next(item for item in rows if item["exposed_pad_row"])
    augmented = private_api._augment_pin_rows([row], symbol, footprint)[0]

    assert augmented["pin_number"] == "6"
    assert augmented["part_spec_name"] == "SIG6"
    assert augmented["symbol_number"] == "6"
    assert augmented["symbol_name"] == "EP"
    assert augmented["footprint_pad_present"] is True
    assert augmented["mismatch"] is True
    assert "pdfplumber_name" in augmented["mismatch_fields"]


def test_review_html_lists_warnings_and_information_after_errors() -> None:
    review: dict[str, Any] = {
        "artifact_kind": "circuit_library_review_packet",
        "packet_id": "a" * 16,
        "findings": [
            {
                "severity": "error",
                "code": "deterministic_error",
                "field": "package",
                "message": "blocking finding",
            },
            {
                "severity": "warning",
                "code": "review_warning",
                "field": "drawing",
                "message": "review this",
            },
            {
                "severity": "info",
                "code": "review_info",
                "field": "model",
                "message": "informational",
            },
        ],
        "pin_comparisons": [],
        "dimensions": [],
        "renders": [],
        "artifact_hashes": {},
        "overlay": None,
        "land_pattern_crop": None,
        "message_template": "CIRCUIT-LIBRARY-REVIEW",
        "unknowns": [],
    }

    private_api: Any = libreview
    rendered = private_api._review_html(review)

    assert rendered.index("error: deterministic_error") < rendered.index("warning: review_warning")
    assert rendered.index("warning: review_warning") < rendered.index("info: review_info")


def test_review_html_renders_inline_evidence_renders_and_mismatch_cells() -> None:
    review: dict[str, Any] = {
        "packet_id": "a" * 16,
        "findings": [],
        "evidence_pages": [{"page": 1, "path": "evidence/page-001.png"}],
        "pin_comparisons": [
            {
                "pin_number": "1",
                "part_spec_name": "SIG1",
                "part_spec_type": "passive",
                "symbol_type": "input",
                "footprint_pad_present": True,
                "mismatch_fields": ["part_spec_type", "symbol_type"],
                "mismatch": True,
            }
        ],
        "dimensions": [
            {
                "field": "package.pitch",
                "label": "—",
                "kind": "limit",
                "min": None,
                "nom": 0.5,
                "max": None,
                "page": 1,
                "crop_path": "crops/package.pitch.png",
                "crop_sha256": "b" * 64,
            }
        ],
        "crops": [
            {"field": "package.pin1_reading", "page": 1, "path": "crops/pin1.png"},
            {"field": "orderable.0.row", "page": 1, "path": "crops/orderable.png"},
        ],
        "renders": [
            {"kind": "footprint_svg", "path": "renders/footprint.svg", "sha256": "c" * 64},
            {"kind": "symbol_svg", "path": "renders/symbol.svg", "sha256": "d" * 64},
        ],
        "artifact_hashes": {},
        "overlay": {"path": "overlay.svg", "scale_known": True},
        "land_pattern_crop": {"path": "crops/land-pattern.png", "page": 1},
        "vision_reads": [
            {
                "field": "package.pitch",
                "read_id": "pitch-read",
                "normalized_answer": "<b>0.5</b>",
                "impression": "<img src=x onerror=alert(1)>",
            }
        ],
        "authoring_comparison": {
            "impressions": {
                "a": "<script>alert('A')</script>",
                "b": "Clear & legible.",
            }
        },
        "message_template": "",
        "unknowns": [],
    }
    private_api: Any = libreview

    rendered = private_api._review_html(review)

    assert '<img src="crops/package.pitch.png"' in rendered
    assert 'style="max-height:120px' in rendered
    assert 'href="evidence/page-001.png">Page 1</a>' in rendered
    assert '<img src="crops/pin1.png"' in rendered
    assert '<img src="crops/orderable.png"' in rendered
    assert '<img src="renders/footprint.svg"' in rendered
    assert '<img src="renders/symbol.svg"' in rendered
    assert "<h2>Author impressions</h2>" in rendered
    assert "&lt;script&gt;alert(&#x27;A&#x27;)&lt;/script&gt;" in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered
    assert "<script>alert(" not in rendered
    assert '<td class="mismatch">passive</td>' in rendered
    assert "<th>Mismatch</th>" in rendered
    assert "Data-derived placement overlay" in rendered
    assert (
        '<img src="overlay.svg" alt="Land-pattern drawing with footprint placement overlay" '
        'style="max-width:100%;height:auto">'
    ) in rendered
    assert 'style="width:480px;height:auto;max-width:100%"' in rendered
    assert 'style="width:360px;height:auto;max-width:100%"' in rendered
    assert ".pair img" not in rendered
    assert "package.pitch</td><td>—</td>" in rendered


def test_overlay_scale_uses_pdf_vector_size_and_pitch(tmp_path: Path) -> None:
    private_api: Any = libreview
    spec = _spec()
    fixture_pads = [
        LandPad(number="1", x=-1.5, y=-2.0, width=0.8, height=0.4, shape="rect"),
        LandPad(number="2", x=-1.5, y=0.0, width=0.8, height=0.4, shape="rect"),
        LandPad(number="3", x=-1.5, y=1.0, width=0.8, height=0.4, shape="rect"),
        LandPad(number="4", x=1.5, y=-2.0, width=0.8, height=0.4, shape="rect"),
        LandPad(number="5", x=1.5, y=0.0, width=0.8, height=0.4, shape="rect"),
        LandPad(number="6", x=1.5, y=1.0, width=0.8, height=0.4, shape="rect"),
        LandPad(number="7", x=-1.5, y=3.0, width=0.4, height=0.8, shape="rect"),
        LandPad(number="8", x=1.5, y=3.0, width=0.4, height=0.8, shape="rect"),
    ]
    assert spec.land_pattern is not None
    spec = spec.model_copy(
        update={"land_pattern": spec.land_pattern.model_copy(update={"pads": fixture_pads})}
    )
    reference = private_api.compute_land_pattern(spec, "nominal")
    footprint = FootprintDef(
        name="Synthetic",
        attributes=["smd"],
        pads=[
            PadDef(
                number=pad.number,
                type="smd",
                shape="rect",
                x=pad.x,
                y=pad.y,
                rotation=0,
                width=pad.width,
                height=pad.height,
                drill=None,
                layers=["F.Cu"],
            )
            for pad in fixture_pads
        ],
        graphics=[],
        models=[],
        properties={},
    )

    def vector_objects(center_scale: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rects: list[dict[str, Any]] = []
        curves: list[dict[str, Any]] = []
        for index, pad in enumerate(fixture_pads):
            center_x, center_y = 100 + pad.x * center_scale, 100 + pad.y * center_scale
            half_width, half_height = pad.width * 10, pad.height * 10
            x0, x1 = center_x - half_width, center_x + half_width
            top, bottom = center_y - half_height, center_y + half_height
            bbox = {"x0": x0, "top": top, "x1": x1, "bottom": bottom}
            if index < 2:
                rects.append(bbox)
            else:
                curves.append(
                    {
                        **bbox,
                        "pts": [(x0, top), (x1, top), (x1, bottom), (x0, bottom), (x0, top)],
                        "path": [
                            ("m", (x0, top)),
                            ("l", (x1, top)),
                            ("l", (x1, bottom)),
                            ("l", (x0, bottom)),
                            ("h",),
                        ],
                    }
                )
        for index in range(4):
            center_x, center_y = 160.0, 20.0 + index * 12.0
            half_width = fixture_pads[0].width * 2.5
            half_height = fixture_pads[0].height * 2.5
            rects.append(
                {
                    "x0": center_x - half_width,
                    "top": center_y - half_height,
                    "x1": center_x + half_width,
                    "bottom": center_y + half_height,
                }
            )
        return rects, curves

    rects, curves = vector_objects(20.0)
    synthetic_page = type("SyntheticPage", (), {"rects": rects, "curves": curves, "lines": []})()
    crop = private_api._CropRecord(
        field="land_pattern.drawing_view",
        page=1,
        path="crops/land_pattern.drawing_view.png",
        sha256="a" * 64,
        source_png_sha256="b" * 64,
        bbox=(0, 0, 200, 200),
        crop_bbox=(0, 0, 200, 200),
        scale=1.0,
    )
    geometry = private_api._derive_overlay_geometry(
        synthetic_page,
        crop,
        spec,
        footprint,
        reference,
        dpi=72,
    )
    assert geometry.scale_known
    assert geometry.scale_pt_per_mm == pytest.approx(20.0, rel=0.01)
    assert geometry.pad_size_estimate_pt_per_mm == pytest.approx(20.0)
    assert geometry.pitch_estimate_pt_per_mm == pytest.approx(20.0)
    assert geometry.scale_px_per_mm == pytest.approx(20.0)
    assert geometry.candidate_pad_count == 8
    crop_path = tmp_path / "land-pattern.png"
    Image.new("RGB", (200, 200), "white").save(crop_path)
    overlay_path = tmp_path / "overlay.svg"
    private_api._overlay_svg(
        spec,
        footprint,
        crop_path,
        "crops/land_pattern.drawing_view.png",
        overlay_path,
        geometry=geometry,
    )
    svg_root = ET.parse(overlay_path).getroot()
    svg_namespace = "{http://www.w3.org/2000/svg}"
    image = svg_root.find(f"{svg_namespace}image")
    assert image is not None
    assert image.attrib["href"] == "crops/land_pattern.drawing_view.png"
    assert image.attrib["width"] == "200"
    polygons = {
        item.attrib["data-pad-number"]: item
        for item in svg_root.findall(f".//{svg_namespace}polygon")
    }
    assert polygons["1"].attrib["fill-opacity"] == "0.72"
    assert polygons["1"].attrib["stroke"] == "#ffbf00"
    for pad in fixture_pads:
        actual = {
            tuple(float(value) for value in point.split(","))
            for point in polygons[pad.number].attrib["points"].split()
        }
        center_x, center_y = 100 + pad.x * 20, 100 + pad.y * 20
        expected = {
            (center_x - pad.width * 10, center_y - pad.height * 10),
            (center_x + pad.width * 10, center_y - pad.height * 10),
            (center_x + pad.width * 10, center_y + pad.height * 10),
            (center_x - pad.width * 10, center_y + pad.height * 10),
        }
        assert all(
            min(math.dist(actual_point, expected_point) for expected_point in expected) <= 1.0
            for actual_point in actual
        )

    mismatched_rects, mismatched_curves = vector_objects(24.0)
    mismatched_page = type(
        "MismatchedPage",
        (),
        {"rects": mismatched_rects, "curves": mismatched_curves, "lines": []},
    )()
    mismatched_geometry = private_api._derive_overlay_geometry(
        mismatched_page,
        crop,
        spec,
        footprint,
        reference,
        dpi=72,
    )
    assert mismatched_geometry.scale_pt_per_mm is None
    assert mismatched_geometry.pad_size_estimate_pt_per_mm == pytest.approx(20.0)
    assert mismatched_geometry.pitch_estimate_pt_per_mm == pytest.approx(24.0)
    assert private_api._overlay_unknown_codes(mismatched_geometry) == ["overlay_scale_unknown"]


def test_review_status_approves_normalized_answers_and_rejects_corrections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(libreview.EVENTS_DIR_ENV, str(tmp_path / "events"))
    spec = _spec()
    packet = "2" * 16
    library = tmp_path / "library"
    answers = _expected_answers(spec, packet)
    answers["pin1_corner"] = "TOP - LEFT"
    answers["drawing_id"] = "  soic-4 "
    _write_event(tmp_path, library, packet, _message(packet, answers=answers))

    status = libreview.review_status(library, spec, packet)

    assert status.state == "approved"
    assert status.reasons == []


@pytest.mark.parametrize(
    ("source", "content", "reason"),
    [
        (
            "assistant",
            "CIRCUIT-LIBRARY-REVIEW 3333333333333333\ndecision: approve\nreviewer: X",
            "not a user message",
        ),
        (
            "user",
            "CIRCUIT-LIBRARY-REVIEW 3333333333333333\ndecision: maybe\nreviewer: X",
            "decision_field_invalid",
        ),
    ],
)
def test_review_status_fails_closed_for_non_user_and_malformed_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    content: str,
    reason: str,
) -> None:
    monkeypatch.setenv(libreview.EVENTS_DIR_ENV, str(tmp_path / "events"))
    library = tmp_path / "library"
    packet = "3" * 16
    _write_event(
        tmp_path,
        library,
        packet,
        content,
        source=source,
    )

    status = libreview.review_status(library, _spec(), packet)

    assert status.state == "invalid"
    assert any(reason in item for item in status.reasons)


def test_review_status_recovers_from_user_grammar_typo_followed_by_valid_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(libreview.EVENTS_DIR_ENV, str(tmp_path / "events"))
    spec = _spec()
    library = tmp_path / "library"
    packet = "8" * 16
    _write_event(
        tmp_path,
        library,
        packet,
        f"CIRCUIT-LIBRARY-REVIEW {packet}\ndecision: aproove\nreviewer: Ada",
        suffix="typo",
    )
    _write_event(
        tmp_path,
        library,
        packet,
        _message(packet, answers=_expected_answers(spec, packet)),
        suffix="valid",
    )

    status = libreview.review_status(library, spec, packet)

    assert status.state == "approved"
    assert "decision_field_invalid_or_duplicated" in status.reasons
    assert all(item.integrity_valid for item in status.decisions)


def test_review_status_keeps_tampered_event_as_blocker_after_valid_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(libreview.EVENTS_DIR_ENV, str(tmp_path / "events"))
    spec = _spec()
    library = tmp_path / "library"
    packet = "9" * 16
    tampered = _write_event(
        tmp_path,
        library,
        packet,
        _message(packet, decision="reject"),
        suffix="tampered",
    )
    tampered.write_text("changed after event hashing", encoding="utf-8")
    _write_event(
        tmp_path,
        library,
        packet,
        _message(packet, answers=_expected_answers(spec, packet)),
        suffix="valid",
    )

    status = libreview.review_status(library, spec, packet)

    assert status.state == "invalid"
    assert any("event_sha256" in reason for reason in status.reasons)
    assert any(not item.integrity_valid for item in status.decisions)


def test_review_status_requires_hash_bound_vision_reviews_for_packet_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(libreview.EVENTS_DIR_ENV, str(tmp_path / "events"))
    spec = _spec()
    library = tmp_path / "library"
    packet = "a" * 16
    image_path = tmp_path / "library" / "reviews" / "packet-overlay.svg"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_text("<svg></svg>", encoding="utf-8")
    review_json = library / "reviews" / spec.mpn / packet / "review.json"
    review_json.parent.mkdir(parents=True, exist_ok=True)
    review_json.write_text(
        json.dumps(
            {
                "artifact_kind": "circuit_library_review_packet",
                "packet_id": packet,
                "blind_questions": [],
                "vision_review_images": [
                    {
                        "path": str(image_path),
                        "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                        "kind": "footprint",
                        "review_record_path": str(
                            libreview.advisory.review_record_path(image_path)
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _write_event(
        tmp_path,
        library,
        packet,
        _message(packet, answers=_expected_answers(spec, packet)),
    )

    missing = libreview.review_status(library, spec, packet)
    assert missing.state == "invalid"
    assert "packet_vision_precheck_missing" in missing.reasons

    record_path = libreview.advisory.write_review_record(
        image_path,
        model="review-model",
        checklist="footprint",
        impression=(
            "The overlay is readable and its pad arrangement is clear, with the "
            "datasheet drawing visible beneath the proposed footprint. The red pad "
            "outlines can be compared against the rounded land pattern, and the "
            "highlighted pin-one pad is easy to locate. Some fine labels are small, "
            "and I cannot independently confirm dimensions from this image alone."
        ),
        findings=[],
    )
    assert record_path == libreview.advisory.review_record_path(image_path)
    record_document = json.loads(record_path.read_text(encoding="utf-8"))
    record_document["detail"]["impression"] = "Too short."
    record_path.write_text(json.dumps(record_document), encoding="utf-8")
    invalid = libreview.review_status(library, spec, packet)
    assert invalid.state == "invalid"
    assert "packet_vision_precheck_missing" in invalid.reasons

    record_path.unlink()
    libreview.advisory.write_review_record(
        image_path,
        model="review-model",
        checklist="footprint",
        impression=(
            "The overlay is readable and its pad arrangement is clear, with the "
            "datasheet drawing visible beneath the proposed footprint. The red pad "
            "outlines can be compared against the rounded land pattern, and the "
            "highlighted pin-one pad is easy to locate. Some fine labels are small, "
            "and I cannot independently confirm dimensions from this image alone."
        ),
        findings=[],
    )
    approved = libreview.review_status(library, spec, packet)
    assert approved.state == "approved"


def test_review_status_detects_tampering_wrong_answers_and_corrections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(libreview.EVENTS_DIR_ENV, str(tmp_path / "events"))
    library = tmp_path / "library"
    packet = "4" * 16
    spec = _spec()
    wrong = _expected_answers(spec, packet)
    wrong["pin.1"] = "WRONG"
    _write_event(tmp_path, library, packet, _message(packet, answers=wrong), suffix="1")
    status = libreview.review_status(library, spec, packet)
    assert status.state == "invalid"
    assert "human_review_blind_mismatch" in status.reasons

    decisions = library / "reviews" / "decisions"
    for path in decisions.glob("*.json"):
        path.unlink()
    missing = _expected_answers(spec, packet)
    missing.pop("pin.1")
    _write_event(tmp_path, library, packet, _message(packet, answers=missing), suffix="missing")
    status = libreview.review_status(library, spec, packet)
    assert status.state == "invalid"
    assert "human_review_blind_mismatch" in status.reasons

    for path in decisions.glob("*.json"):
        path.unlink()
    event = _write_event(
        tmp_path,
        library,
        packet,
        _message(packet, answers=_expected_answers(spec, packet)),
        suffix="2",
    )
    event.write_text("tampered", encoding="utf-8")
    status = libreview.review_status(library, spec, packet)
    assert status.state == "invalid"
    assert any("event_sha256" in reason for reason in status.reasons)

    for path in decisions.glob("*.json"):
        path.unlink()
    missing_event = _write_event(
        tmp_path,
        library,
        packet,
        _message(packet, decision="reject"),
        suffix="missing-event",
    )
    missing_event.unlink()
    status = libreview.review_status(library, spec, packet)
    assert status.state == "invalid"
    assert any("decision_invalid" in reason for reason in status.reasons)

    for path in decisions.glob("*.json"):
        path.unlink()
    _write_event(
        tmp_path, library, packet, _message(packet, decision="reject"), suffix="unreadable-pointer"
    )
    next(decisions.glob("*.json")).write_text("{", encoding="utf-8")
    status = libreview.review_status(library, spec, packet)
    assert status.state == "invalid"
    assert any("decision_invalid" in reason for reason in status.reasons)

    for path in decisions.glob("*.json"):
        path.unlink()
    content = (
        _message(packet, answers=_expected_answers(spec, packet))
        + '\ncorrection: /manufacturer | "Example" | "Other" | fix | page=1'
    )
    _write_event(tmp_path, library, packet, content, suffix="3")
    status = libreview.review_status(library, spec, packet)
    assert status.state == "invalid"
    assert "approval_must_not_include_corrections" in status.reasons


def test_later_reject_overrides_approval_and_stale_packet_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(libreview.EVENTS_DIR_ENV, str(tmp_path / "events"))
    spec = _spec()
    library = tmp_path / "library"
    packet = "5" * 16
    approval = _write_event(
        tmp_path,
        library,
        packet,
        _message(packet, answers=_expected_answers(spec, packet)),
        suffix="1",
    )
    rejection = _write_event(
        tmp_path,
        library,
        packet,
        _message(packet, decision="reject"),
        suffix="2",
    )
    os.utime(approval, ns=(1_000_000_000, 1_000_000_000))
    os.utime(rejection, ns=(2_000_000_000, 2_000_000_000))
    decisions = libreview.load_decisions(library, packet)
    assert [item.event_name for item in decisions] == [approval.name, rejection.name]
    assert libreview.review_status(library, spec, packet).state == "rejected"

    assert libreview.review_status(library, spec, "6" * 16).state == "pending"


def _reject_with_correction(
    tmp_path: Path,
    library: Path,
    packet: str,
    *,
    old: Any = "Example",
    new: Any = "Corrected",
    pointer: str = "/manufacturer",
    suffix: str = "1",
) -> libreview.ReviewDecision:
    content = (
        f"CIRCUIT-LIBRARY-REVIEW {packet}\n"
        "decision: reject\n"
        "reviewer: Corrector\n"
        f"correction: {pointer} | {json.dumps(old)} | {json.dumps(new)} | corrected source | page=1"
    )
    _write_event(tmp_path, library, packet, content, suffix=suffix)
    return next(
        item
        for item in libreview.load_decisions(library, packet)
        if item.corrections and item.corrections[0].new == new
    )


def test_apply_corrections_is_validated_idempotent_and_regression_tracked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(libreview.EVENTS_DIR_ENV, str(tmp_path / "events"))
    spec = _spec()
    spec_path = tmp_path / "part-spec.json"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    library = tmp_path / "library"
    library.mkdir()
    packet = "7" * 16
    decision = _reject_with_correction(tmp_path, library, packet)

    first = libreview.apply_corrections(spec_path, decision)
    second = libreview.apply_corrections(spec_path, decision)

    assert first.applied and second.applied
    assert libreview.load_part_spec(spec_path).manufacturer == "Corrected"
    corpus = (library / "reviews" / "corrections.jsonl").read_text(encoding="utf-8")
    assert corpus.count('"event_sha256"') == 1
    assert libreview.correction_regressions(library, libreview.load_part_spec(spec_path)) == []

    reverted = libreview.load_part_spec(spec_path).model_dump(mode="json")
    reverted["manufacturer"] = "Example"
    spec_path.write_text(json.dumps(reverted), encoding="utf-8")
    findings = libreview.correction_regressions(library, libreview.load_part_spec(spec_path))
    assert [item.code for item in findings] == ["correction_regressed"]


def test_apply_corrections_rejects_conflicts_invalid_specs_and_tampered_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(libreview.EVENTS_DIR_ENV, str(tmp_path / "events"))
    spec = _spec()
    spec_path = tmp_path / "part-spec.json"
    document = spec.model_dump(mode="json")
    document["manufacturer"] = "Already different"
    spec_path.write_text(json.dumps(document), encoding="utf-8")
    library = tmp_path / "library"
    packet = "8" * 16
    decision = _reject_with_correction(tmp_path, library, packet)
    result = libreview.apply_corrections(spec_path, decision)
    assert not result.applied
    assert result.reasons == ["correction_conflict:/manufacturer"]
    assert libreview.load_part_spec(spec_path).manufacturer == "Already different"

    document["manufacturer"] = "Example"
    spec_path.write_text(json.dumps(document), encoding="utf-8")
    invalid_decision = _reject_with_correction(
        tmp_path,
        library,
        packet,
        old=6,
        new=0,
        pointer="/package/pin_count",
        suffix="2",
    )
    result = libreview.apply_corrections(spec_path, invalid_decision)
    assert not result.applied
    assert result.reasons[0].startswith("correction_invalid_spec:")
    assert libreview.load_part_spec(spec_path).manufacturer == "Example"

    event_path = invalid_decision.event_path
    assert event_path is not None
    event_path.write_text("tampered", encoding="utf-8")
    result = libreview.apply_corrections(spec_path, invalid_decision)
    assert not result.applied
    assert result.reasons == ["correction_requires_valid_reject"]


def test_apply_corrections_fails_closed_for_malformed_corpus_and_json_type_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(libreview.EVENTS_DIR_ENV, str(tmp_path / "events"))
    spec = _spec()
    spec_path = tmp_path / "part-spec.json"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    library = tmp_path / "library"
    packet = "9" * 16

    type_mismatch = _reject_with_correction(
        tmp_path,
        library,
        packet,
        old=True,
        new=5,
        pointer="/package/pin_count",
        suffix="type-mismatch",
    )
    result = libreview.apply_corrections(spec_path, type_mismatch)
    assert not result.applied
    assert result.reasons == ["correction_conflict:/package/pin_count"]

    malformed = _reject_with_correction(
        tmp_path,
        library,
        packet,
        suffix="malformed-corpus",
    )
    corpus_path = library / "reviews" / "corrections.jsonl"
    corpus_path.write_text('{"event_sha256":"incomplete"}\n', encoding="utf-8")
    result = libreview.apply_corrections(spec_path, malformed)
    assert not result.applied
    assert result.reasons == ["correction_corpus_unreadable"]
    assert libreview.load_part_spec(spec_path).manufacturer == "Example"


def _make_extraction(pdf_path: Path, evidence_dir: Path) -> DatasheetExtraction:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (200, 200), "white")
    for x in range(20, 100):
        image.putpixel((x, 40), (0, 0, 0))
    png = evidence_dir / "page-001.png"
    image.save(png)
    tables_path = evidence_dir / "page-001.tables.json"
    cells = [
        [[0, 0, 10, 10], [10, 0, 40, 10]],
        [[0, 10, 10, 20], [10, 10, 40, 20]],
        [[0, 20, 10, 30], [10, 20, 40, 30]],
        [[0, 30, 10, 40], [10, 30, 40, 40]],
        [[0, 40, 10, 50], [10, 40, 40, 50]],
    ]
    tables_path.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "bbox": [0, 0, 40, 50],
                        "rows": [
                            ["Pin", "Name"],
                            ["1", "SIG1"],
                            ["2", "SIG2"],
                            ["3", "SIG3"],
                            ["4", "SIG4"],
                        ],
                        "cells": cells,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(png.read_bytes()).hexdigest()
    return DatasheetExtraction(
        artifact_kind="circuit_datasheet_extraction",
        pdf_path=str(pdf_path),
        pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        page_count=1,
        pages=[
            PageExtraction(
                page=1,
                width_pt=200,
                height_pt=200,
                png_path=png.name,
                png_sha256=digest,
                dpi=72,
                text_layer=True,
                lanes=[],
                tables_path=tables_path.name,
                table_count=1,
                vector_objects=0,
                drawing_page=True,
                order_similarity=None,
            )
        ],
        tools={},
    )


@pytest.mark.parametrize(
    ("fresh_verdict", "regressed"),
    [("pass", False), ("fail", False), ("pass", True)],
)
def test_build_packet_binds_fresh_checks_crops_hashes_and_blind_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh_verdict: Literal["pass", "fail"],
    regressed: bool,
) -> None:
    spec = _spec()
    pitch = Dimension(
        nom=1.0,
        label="pitch",
        reading=_reading("1.0", bbox=(70, 70, 90, 90)),
    )
    spec = spec.model_copy(
        update={
            "package": spec.package.model_copy(update={"pitch": pitch}),
            "authoring": "authoring/run-1",
        }
    )
    pdf_path = tmp_path / "part.pdf"
    pdf_path.write_bytes(b"pdf")
    spec_path = tmp_path / "part-spec.json"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    footprint_path = library_dir / "Modern.kicad_mod"
    footprint_source = Path(__file__).parent / "data" / "library" / "modern.kicad_mod"
    footprint_path.write_bytes(footprint_source.read_bytes())
    base_path = tmp_path / "base.kicad_mod"
    base_path.write_bytes(footprint_path.read_bytes())
    evidence_path = tmp_path / "prototype.txt"
    evidence_path.write_text("prototype evidence", encoding="utf-8")
    lineage_rules = libreview.load_rules("builtin:kicad-generator", library_dir / "rules")
    lineage = FootprintLineage(
        artifact_kind="circuit_footprint_lineage",
        footprint_sha256=hashlib.sha256(footprint_path.read_bytes()).hexdigest(),
        layer="organization",
        base=FootprintBase(
            kind="generated",
            path="base.kicad_mod",
            sha256=hashlib.sha256(base_path.read_bytes()).hexdigest(),
            rule_chain_sha256=lineage_rules.chain_sha256,
        ),
        changes=[PadChange(pad="1", field="width", before=0.3, after=0.32)],
        reason="<script>production tweak</script>",
        evidence=[
            EvidenceRef(
                kind="prototype",
                path="prototype.txt",
                sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                note="<script>reviewed</script>",
            )
        ],
    )
    lineage_path_for(footprint_path).write_text(lineage.model_dump_json(), encoding="utf-8")
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    model_path = model_dir / "Test.step"
    model_path.write_bytes(b"ISO-10303-21; fixture")
    monkeypatch.setenv("KICAD10_3DMODEL_DIR", str(model_dir))
    data_dir = Path(__file__).parent / "data" / "library"
    symbol_lib = data_dir / "symbols.kicad_sym"
    extraction_holder: dict[str, DatasheetExtraction] = {}
    comparison = libreview.authoring.AuthoringComparison(
        artifact_kind="circuit_part_authoring_comparison",
        run_dir="authoring/run-1",
        sealed={"a": "a" * 64, "b": "b" * 64},
        models={"a": "model-a", "b": "model-b"},
        profiles={"a": "profile-a", "b": "profile-b"},
        impressions={
            "a": "<script>lane A</script>",
            "b": "Legible & clear.",
        },
        rasterizers={"a": ["pdftoppm"], "b": ["pdfium"]},
        agreed=[],
        disagreements=[
            libreview.authoring.AuthoringDisagreement(
                pointer="/package/pitch/nom",
                a=0.5,
                b=0.6,
            )
        ],
        model_diversity="distinct",
        issues=[],
    )

    def extract(
        path: Path,
        out_dir: Path,
        *,
        pages: list[int],
        dpi: int,
    ) -> DatasheetExtraction:
        assert pages == [1]
        assert dpi == 300
        extraction = _make_extraction(path, out_dir)
        extraction_holder["extraction"] = extraction
        return extraction

    def check(
        checked_spec: PartSpec,
        _extraction: DatasheetExtraction,
        *,
        spec_path: Path,
        extraction_path: Path,
    ) -> PartSpecReport:
        return PartSpecReport(
            artifact_kind="circuit_part_spec_check",
            verdict=fresh_verdict,
            part_spec_sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest(),
            extraction_sha256="a" * 64,
            pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
            checked_readings=1,
            findings=[],
            pinout=geometry_for_names(
                checked_spec.pinout.labels_vision,
                pin_count=checked_spec.package.pin_count,
                topology="dual",
                page=checked_spec.pinout.page,
            )
            if checked_spec.pinout is not None
            else None,
        )

    def verify(
        _spec: PartSpec,
        *,
        spec_path: Path,
        spec_check_path: Path,
        symbol_lib: Path,
        symbol_name: str,
        footprint_path: Path,
        library_dir: Path,
        reference: LandPatternResult,
        tolerance_mm: float,
        model_required: bool,
        rules: Any,
        output_path: Path,
    ) -> LibraryVerification:
        assert json.loads(spec_check_path.read_text(encoding="utf-8"))["verdict"] == fresh_verdict
        assert reference.source == "datasheet"
        assert reference.rule_chain == rules.chain == ["builtin:kicad-generator"]
        del library_dir, model_required
        return LibraryVerification(
            artifact_kind="circuit_library_verification",
            verdict=fresh_verdict,
            part_spec_sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest(),
            inputs=VerificationInputs(
                part_spec_path=spec_path,
                symbol_lib=symbol_lib,
                symbol_name=symbol_name,
                footprint_path=footprint_path,
                density="nominal",
                tolerance_mm=tolerance_mm,
                model_required=True,
            ),
            symbol=VerifiedSymbol(
                lib_path=symbol_lib,
                name=symbol_name,
                sha256=hashlib.sha256(symbol_lib.read_bytes()).hexdigest(),
            ),
            footprint=VerifiedFootprint(
                path=footprint_path,
                name="Modern",
                sha256=hashlib.sha256(footprint_path.read_bytes()).hexdigest(),
            ),
            models=[
                VerifiedModel(
                    path="${KICAD10_3DMODEL_DIR}/Test.step",
                    resolved=True,
                    sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(),
                )
            ],
            findings=[
                VerifyFinding(
                    code="intentional_tuning",
                    severity="info",
                    subject="footprint",
                    message=(
                        "recorded pad deviations; deltas against reference and base in mm: "
                        '[{"pad":"1","reference_delta_mm":{"width":0.02},'
                        '"base_delta_mm":{"width":0.02}}]'
                    ),
                )
            ],
        )

    def missing_cli(*_args: Any, **_kwargs: Any) -> list[Path]:
        raise libreview.kicad_cli.KicadCliError("kicad-cli unavailable")

    monkeypatch.setattr(libreview.datasheet, "extract_datasheet", extract)

    def missing_extraction(_path: Path) -> DatasheetExtraction:
        raise FileNotFoundError

    monkeypatch.setattr(libreview.datasheet, "load_extraction", missing_extraction)
    monkeypatch.setattr(libreview, "check_part_spec", check)
    monkeypatch.setattr(libreview, "verify_library_part", verify)
    monkeypatch.setattr(libreview.kicad_cli, "export", missing_cli)

    def fresh_comparison(
        _spec: PartSpec, _spec_dir: Path
    ) -> libreview.authoring.AuthoringComparison:
        return comparison

    monkeypatch.setattr(libreview, "_fresh_authoring_comparison", fresh_comparison)

    def regressions(_library_dir: Path, _spec: PartSpec) -> list[libreview.ReviewFinding]:
        return (
            [
                libreview.ReviewFinding(
                    code="correction_regressed",
                    severity="error",
                    field="/manufacturer",
                    message="current value differs from the accepted correction",
                )
            ]
            if regressed
            else []
        )

    monkeypatch.setattr(libreview, "correction_regressions", regressions)
    packet = libreview.build_review_packet(
        spec_path,
        symbol_lib=symbol_lib,
        symbol_name="Derived",
        footprint_path=footprint_path,
        library_dir=library_dir,
        density="nominal",
        out_dir=library_dir / "reviews",
    )

    review_path = packet.packet_dir / "review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    blind_html = (packet.packet_dir / "01-blind.html").read_text(encoding="utf-8")
    assert packet.approvable is (fresh_verdict == "pass" and not regressed)
    assert packet.packet_id == review["packet_id"]
    assert (
        review["inputs"]["lineage_sha256"]
        == hashlib.sha256(lineage_path_for(footprint_path).read_bytes()).hexdigest()
    )
    assert review["inputs"]["rule_chain_sha256"] == lineage_rules.chain_sha256
    assert review["rule_chain"]["profiles"][0]["profile_id"] == "builtin:kicad-generator"
    assert review["footprint_tuning"]["reason"] == "<script>production tweak</script>"
    assert review["footprint_tuning"]["evidence"][0]["sha256"] == lineage.evidence[0].sha256
    assert review["footprint_tuning"]["intentional_deviations"][0]["pad"] == "1"
    assert review["inputs"]["authoring_sha256s"] == sorted(comparison.sealed.values())
    assert review["artifact_hashes"]["authoring:a"] == comparison.sealed["a"]
    if regressed:
        assert any(item["code"] == "correction_regressed" for item in review["findings"])
    assert review["pin_comparisons"]
    assert {
        "pin_number",
        "pdfplumber_number",
        "poppler_number",
        "pdfplumber_name",
        "poppler_name",
        "symbol_number",
        "symbol_name",
        "symbol_type",
        "footprint_pad_present",
    } <= set(review["pin_comparisons"][0])
    assert review["dimensions"]
    assert {
        "field",
        "label",
        "kind",
        "min",
        "nom",
        "max",
        "page",
        "crop_path",
        "crop_sha256",
    } <= set(review["dimensions"][0])
    assert review["message_template"].startswith(f"CIRCUIT-LIBRARY-REVIEW {packet.packet_id}\n")
    assert all(
        f"answer: {question.question_id} = " in review["message_template"]
        for question in libreview.blind_questions(spec, packet.packet_id, comparison)
    )
    assert "SIG1" not in blind_html
    assert "<script" not in blind_html.lower()
    assert "base64," not in blind_html.lower()
    assert "http://" not in blind_html.lower()
    assert "expected" not in json.dumps(review["blind_questions"])
    review_html = (packet.packet_dir / "02-review.html").read_text(encoding="utf-8")
    assert "<script" not in review_html.lower()
    assert "base64," not in review_html.lower()
    assert "http://" not in review_html.lower()
    assert "&lt;script&gt;lane A&lt;/script&gt;" in review_html
    assert "&lt;script&gt;production tweak&lt;/script&gt;" in review_html
    assert "&lt;script&gt;reviewed&lt;/script&gt;" in review_html
    assert "Intentional deviations from standard" in review_html
    assert "Legible &amp; clear." in review_html
    assert "Land-pattern drawing-view crop" not in review_html
    assert "Pinout name-at-position" in review_html
    assert review["pinout_comparisons"]
    assert 'src="crops/pinout.png"' in review_html
    assert "Placement overlay — not to scale" in review_html
    assert "render_unavailable" in review["unknowns"]
    assert "overlay_scale_unknown" in review["unknowns"]
    assert "3D visual review not included yet" in review["unknowns"]
    assert "model:0" in review["artifact_hashes"]
    assert review["overlay"] is not None
    assert len(review["vision_review_images"]) == 1
    vision_image = review["vision_review_images"][0]
    assert vision_image["kind"] == "footprint"
    assert (
        Path(vision_image["path"]).read_bytes()
        == (packet.packet_dir / review["overlay"]["path"]).read_bytes()
    )
    assert vision_image["sha256"] == review["overlay"]["sha256"]
    assert vision_image["review_record_path"].endswith("review-visual-overlay.advisory.json")
    assert "Images requiring vision review before approval" in review_html
    assert review["land_pattern_crop"] is not None
    assert review["land_pattern_crop"]["path"] == "crops/land_pattern.drawing_view.png"
    crop_fields = {item["field"]: item for item in review["crops"]}
    assert crop_fields["package.drawing_view"]["path"] == "crops/package.drawing_view.png"
    assert crop_fields["package.drawing_view"]["crop_bbox"] == [0, 0, 126, 126]
    assert crop_fields["land_pattern.drawing_view"]["crop_bbox"] == [0, 0, 66, 56]
    assert 'src="crops/package.drawing_view.png"' in blind_html
    assert 'src="crops/pin_table.png"' in blind_html
    assert 'src="crops/pinout.png"' in blind_html
    assert any(item["field"] == "pinout" for item in review["crops"])
    assert any(item["field"] == "orderable.0.row" for item in review["crops"])
    page_hashes = {item["page"]: item["sha256"] for item in review["evidence_pages"]}
    for page_record in review["evidence_pages"]:
        page_path = packet.packet_dir / page_record["path"]
        assert hashlib.sha256(page_path.read_bytes()).hexdigest() == page_record["sha256"]
    for crop_record in review["crops"]:
        crop_path = packet.packet_dir / crop_record["path"]
        assert hashlib.sha256(crop_path.read_bytes()).hexdigest() == crop_record["sha256"]
        assert crop_record["source_png_sha256"] == page_hashes[crop_record["page"]]
    overlay_svg = (packet.packet_dir / review["overlay"]["path"]).read_text(encoding="utf-8")
    assert overlay_svg.count("<polygon ") == 0
    assert 'href="crops/land_pattern.drawing_view.png"' in overlay_svg
    assert review["overlay"]["scale_known"] is False
    crop = review["land_pattern_crop"]
    crop_path = packet.packet_dir / crop["path"]
    assert hashlib.sha256(crop_path.read_bytes()).hexdigest() == crop["sha256"]
    with Image.open(crop_path) as image:
        assert min(image.size) >= 400
    svg_root = ET.fromstring(overlay_svg)
    svg_namespace = "{http://www.w3.org/2000/svg}"
    composite_image = svg_root.find(f"{svg_namespace}image")
    assert composite_image is not None
    assert composite_image.attrib["href"] == "crops/land_pattern.drawing_view.png"
    assert svg_root.findall(f".//{svg_namespace}polygon") == []
    assert review["overlay"]["scale_known"] is False
    assert review["overlay"]["scale_pt_per_mm"] is None
    private_api: Any = libreview

    def no_drawing_view_crops(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(private_api, "_drawing_view_crops", no_drawing_view_crops)
    no_scale_packet = libreview.build_review_packet(
        spec_path,
        symbol_lib=symbol_lib,
        symbol_name="Derived",
        footprint_path=footprint_path,
        library_dir=library_dir,
        density="nominal",
        out_dir=tmp_path / "reviews-no-scale",
    )
    no_scale_review = json.loads(
        (no_scale_packet.packet_dir / "review.json").read_text(encoding="utf-8")
    )
    no_scale_html = (no_scale_packet.packet_dir / "02-review.html").read_text(encoding="utf-8")
    assert no_scale_review["overlay"] is None
    assert "overlay_scale_unknown" in no_scale_review["unknowns"]
    assert "Land-pattern overlay or cited page crop is unavailable." in no_scale_html
    assert (
        extraction_holder["extraction"].pages[0].png_sha256 == review["evidence_pages"][0]["sha256"]
    )


def test_json_pointer_rejects_invalid_escapes_and_noncanonical_indices() -> None:
    private_api: Any = libreview
    with pytest.raises(ValueError, match="invalid escape"):
        private_api._pointer_tokens("/items/~2")
    with pytest.raises(IndexError):
        private_api._get_pointer({"items": ["value"]}, "/items/01")
