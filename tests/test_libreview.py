import hashlib
import json
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
        )

    first = packet_for(values)
    assert first == packet_for({**values, "model_sha256s": ["e" * 64, "f" * 64]})
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
    assert '<td class="mismatch">passive</td>' in rendered
    assert "<th>Mismatch</th>" in rendered
    assert "Same-scale placement overlay" in rendered
    assert "package.pitch</td><td>—</td>" in rendered


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
    spec = spec.model_copy(update={"package": spec.package.model_copy(update={"pitch": pitch})})
    pdf_path = tmp_path / "part.pdf"
    pdf_path.write_bytes(b"pdf")
    spec_path = tmp_path / "part-spec.json"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    model_path = model_dir / "Test.step"
    model_path.write_bytes(b"ISO-10303-21; fixture")
    monkeypatch.setenv("KICAD10_3DMODEL_DIR", str(model_dir))
    data_dir = Path(__file__).parent / "data" / "library"
    symbol_lib = data_dir / "symbols.kicad_sym"
    footprint_path = data_dir / "modern.kicad_mod"
    extraction_holder: dict[str, DatasheetExtraction] = {}

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
        _spec: PartSpec,
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
        output_path: Path,
    ) -> LibraryVerification:
        assert json.loads(spec_check_path.read_text(encoding="utf-8"))["verdict"] == fresh_verdict
        assert reference.source == "datasheet"
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
            findings=[],
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
        for question in libreview.blind_questions(spec, packet.packet_id)
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
    assert "Land-pattern drawing-view crop" in review_html
    assert "Same-scale placement overlay" in review_html
    assert "render_unavailable" in review["unknowns"]
    assert "3D visual review not included yet" in review["unknowns"]
    assert "model:0" in review["artifact_hashes"]
    assert review["overlay"] is not None
    assert review["land_pattern_crop"] is not None
    assert review["land_pattern_crop"]["path"] == "crops/land_pattern.drawing_view.png"
    crop_fields = {item["field"]: item for item in review["crops"]}
    assert crop_fields["package.drawing_view"]["path"] == "crops/package.drawing_view.png"
    assert crop_fields["package.drawing_view"]["crop_bbox"] == [0, 0, 126, 126]
    assert crop_fields["land_pattern.drawing_view"]["crop_bbox"] == [0, 0, 66, 56]
    assert 'src="crops/package.drawing_view.png"' in blind_html
    assert 'src="crops/pin_table.png"' in blind_html
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
    assert overlay_svg.count("<polygon ") == len(libreview.parse_footprint(footprint_path).pads)
    assert overlay_svg.count('fill="url(#hatch)"') == sum(
        len(rectangles) for rectangles in libreview.lead_rects(spec).values()
    )
    assert overlay_svg.count('stroke-dasharray="0.12 0.08"') == len(
        libreview.compute_land_pattern(spec, "nominal").pads
    )
    assert "1 mm" in overlay_svg
    assert "<circle " in overlay_svg
    assert "Top-view placement" in overlay_svg
    assert "drawing view top" in overlay_svg
    assert review["overlay"]["scale_known"] is True
    crop = review["land_pattern_crop"]
    crop_path = packet.packet_dir / crop["path"]
    assert hashlib.sha256(crop_path.read_bytes()).hexdigest() == crop["sha256"]
    with Image.open(crop_path) as image:
        assert min(image.size) >= 400
    land_page = next(item for item in review["evidence_pages"] if item["page"] == crop["page"])
    crop_scale = land_page["dpi"] / 25.4 * crop_fields["land_pattern.drawing_view"]["scale"]
    assert review["overlay"]["scale_px_per_mm"] == pytest.approx(crop_scale)
    svg_root = ET.fromstring(overlay_svg)
    view_box = [float(item) for item in svg_root.attrib["viewBox"].split()]
    overlay_width_px = float(svg_root.attrib["width"].removesuffix("px"))
    assert overlay_width_px / view_box[2] == pytest.approx(crop_scale)
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
    assert no_scale_review["overlay"]["scale_known"] is False
    assert any("overlay not to scale" in item for item in no_scale_review["unknowns"])
    assert "Placement overlay — not to scale" in no_scale_html
    assert (
        extraction_holder["extraction"].pages[0].png_sha256 == review["evidence_pages"][0]["sha256"]
    )


def test_json_pointer_rejects_invalid_escapes_and_noncanonical_indices() -> None:
    private_api: Any = libreview
    with pytest.raises(ValueError, match="invalid escape"):
        private_api._pointer_tokens("/items/~2")
    with pytest.raises(IndexError):
        private_api._get_pointer({"items": ["value"]}, "/items/01")
