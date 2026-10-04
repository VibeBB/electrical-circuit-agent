from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from circuit import corpus, libitems, libreview, occt
from circuit.partspec import (
    CellRef,
    DatasheetRef,
    Dimension,
    OrderableVariant,
    PackageSpec,
    PartSpec,
    PinSpec,
    PinTable,
    Reading,
)
from connector_fixtures import connector_spec

REPO_ROOT = Path(__file__).parents[1]
CORPUS_ROOT = REPO_ROOT / "library" / "corpus"
_DATASHEET_SHA256_SNAPSHOT = "584ef469c312b3ec7cbf67ba94b3f8801caf303d04212e1c41e55b123af60013"


def _reading(text: str) -> Reading:
    return Reading(page=1, bbox=(0, 0, 1, 1), vision=text, vision_record="test")


def _partspec(pin_name: str = "VCC") -> PartSpec:
    package = PackageSpec(
        family="custom",
        drawing_id="test",
        pin_count=1,
        body_length=Dimension(nom=1, reading=_reading("1 mm")),
        body_width=Dimension(nom=1, reading=_reading("1 mm")),
        height=Dimension(nom=1, reading=_reading("1 mm")),
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=_reading("pin 1 at top left"),
    )
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="TEST",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="test.pdf",
            sha256="a" * 64,
            revision="A",
            extraction_path="test-extraction.json",
        ),
        package=package,
        pins=[
            PinSpec(
                number="1",
                name=pin_name,
                electrical_type="power_in",
                reading=_reading(f"1 {pin_name}"),
            )
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
        orderable=[
            OrderableVariant(
                mpn="TEST",
                package_designator="test",
                pin_count=1,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading("TEST test"),
            )
        ],
    )


def _truth(**overrides: object) -> corpus.CorpusTruth:
    values: dict[str, object] = {
        "schema": "circuit_corpus_truth",
        "version": 1,
        "id": "synthetic-entry",
        "package_family": "custom",
        "pins": {"1": "VCC"},
        "pin1_corner": "top_left",
        "drawing_view": "top",
        "dimensions": corpus.CorpusDimensions(
            body_length=corpus.CorpusDimension(nom=1),
            body_width=corpus.CorpusDimension(nom=1),
            height=corpus.CorpusDimension(nom=1),
            pitch=corpus.CorpusDimension(nom=0.5),
        ),
        "expected_pads": [
            corpus.CorpusPad(
                number="1",
                center=(0, 0),
                size=(1, 1),
                shape="rect",
            )
        ],
        "canary": "CIRCUIT-CORPUS-CANARY-00000000-0000-4000-8000-000000000001",
        "notes": "Synthetic test truth.",
    }
    values.update(overrides)
    return corpus.CorpusTruth.model_validate(values)


def _footprint(*pads: libitems.PadDef) -> libitems.FootprintDef:
    return libitems.FootprintDef(
        name="",
        attributes=[],
        pads=list(pads),
        graphics=[],
        models=[],
        properties={},
    )


def _numbered_pad(
    *,
    shape: str = "rect",
    rotation: float = 0,
    width: float = 1,
    height: float = 1,
    pad_type: str = "smd",
    drill: float | None = None,
    polygon: list[tuple[float, float]] | None = None,
    layers: list[str] | None = None,
    number: str = "1",
    x: float = 0,
    y: float = 0,
) -> libitems.PadDef:
    return libitems.PadDef(
        number=number,
        type=pad_type,  # type: ignore[arg-type]
        shape=shape,
        x=x,
        y=y,
        rotation=rotation,
        width=width,
        height=height,
        drill=drill,
        layers=layers or ["*.Cu"],
        polygon=polygon,
    )


def _symbol(truth: corpus.CorpusTruth) -> libitems.SymbolDef:
    return libitems.SymbolDef(
        name="",
        pins=[
            libitems.SymPin(
                number=number,
                name=name,
                electrical_type="input",
                x=0,
                y=0,
                length=1,
                orientation=0,
                unit=1,
            )
            for number, name in truth.pins.items()
        ],
        properties={},
    )


def _score(
    truth: corpus.CorpusTruth,
    *,
    partspec: PartSpec | None = None,
    footprint: libitems.FootprintDef | None = None,
) -> corpus.CorpusScore:
    return corpus.score_part(
        truth,
        partspec or _partspec(),
        footprint if footprint is not None else _footprint(_numbered_pad()),
        _symbol(truth),
        None,
    )


def _corpus_connector(spec: PartSpec) -> corpus.CorpusConnector:
    connector = spec.connector
    assert connector is not None
    board_edge = connector.board_edge
    offset = board_edge.offset if board_edge is not None else None
    return corpus.CorpusConnector(
        mount=connector.mount,
        orientation=connector.orientation,
        gender=connector.gender,
        mating_axis=connector.mating_axis,
        board_edge_side=board_edge.side if board_edge is not None else None,
        board_edge_offset=(
            corpus.CorpusDimension(min=offset.min, nom=offset.nom, max=offset.max)
            if offset is not None
            else None
        ),
        mating_mirror=connector.numbering.mating_mirror,
        manufacturer_to_kicad=dict(connector.numbering.manufacturer_to_kicad),
    )


def test_manifest_and_seeded_truth_files_validate() -> None:
    manifest = corpus.load_manifest(CORPUS_ROOT / "corpus.json")
    assert len(manifest.entries) >= 40
    assert all(entry.truth_status == "unconfirmed" for entry in manifest.entries)
    canaries: set[str] = set()
    datasheet_hashes = [(entry.id, entry.datasheet.sha256) for entry in manifest.entries]
    for entry in manifest.entries:
        assert len(entry.datasheet.sha256) == 64
        assert all(character in "0123456789abcdef" for character in entry.datasheet.sha256)
        truth, digest = corpus.load_truth(CORPUS_ROOT, entry)
        assert truth.id == entry.id
        assert truth.package_family == entry.package_family
        assert digest == corpus.sha256(CORPUS_ROOT / entry.truth_path)
        canary_uuid = truth.canary.removeprefix("CIRCUIT-CORPUS-CANARY-")
        assert str(UUID(canary_uuid, version=4)) == canary_uuid
        assert truth.canary not in canaries
        canaries.add(truth.canary)
        if entry.package_family == "connector":
            assert truth.connector is not None
            assert truth.expected_mechanical or any(
                pad.pad_type is not None for pad in truth.expected_pads
            )
    snapshot = hashlib.sha256(
        json.dumps(datasheet_hashes, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    assert snapshot == _DATASHEET_SHA256_SNAPSHOT


def test_tht_corpus_pads_compare_drill_and_shape() -> None:
    manifest = corpus.load_manifest(CORPUS_ROOT / "corpus.json")
    entry = next(item for item in manifest.entries if item.id == "lm317-to220")
    truth, _ = corpus.load_truth(CORPUS_ROOT, entry)

    def footprint_for_truth(*, mutate: bool = False) -> libitems.FootprintDef:
        pads: list[libitems.PadDef] = []
        for pad in truth.expected_pads:
            drill = pad.drill
            shape = pad.shape or "rect"
            assert pad.size is not None
            if mutate and pad.number == "1" and drill is not None:
                drill += 0.02
            if mutate and pad.number == "2":
                shape = "rect" if shape != "rect" else "circle"
            pads.append(
                libitems.PadDef(
                    number=pad.number,
                    type="thru_hole",
                    shape=shape,
                    x=pad.center[0],
                    y=pad.center[1],
                    rotation=0,
                    width=pad.size[0],
                    height=pad.size[1],
                    drill=drill,
                    layers=["*.Cu"],
                )
            )
        return libitems.FootprintDef(
            name="",
            attributes=[],
            pads=pads,
            graphics=[],
            models=[],
            properties={},
        )

    symbol = libitems.SymbolDef(name="", pins=[], properties={})
    model = occt.box(0, 0, 0, 0.001, 0.001, 0.001)
    correct = corpus.score_part(truth, _partspec(), footprint_for_truth(), symbol, model)
    assert not {
        finding.code
        for finding in correct.findings
        if finding.code in {"corpus_pad_drill_mismatch", "corpus_pad_shape_mismatch"}
    }

    incorrect = corpus.score_part(
        truth,
        _partspec(),
        footprint_for_truth(mutate=True),
        symbol,
        model,
    )
    mismatch_codes = {finding.code for finding in incorrect.findings}
    assert "corpus_pad_drill_mismatch" in mismatch_codes
    assert "corpus_pad_shape_mismatch" in mismatch_codes


def test_polygon_pad_matches_rotated_rectangle_and_custom_polygon() -> None:
    corners = [(-0.5, -0.5), (-0.5, 0.5), (0.5, 0.5), (0.5, -0.5)]
    truth = _truth(
        expected_pads=[
            corpus.CorpusPad(
                number="1",
                center=(0, 0),
                rotation=45,
                polygon=corners,
            )
        ]
    )
    rotated_rect = _numbered_pad(rotation=45)
    custom_polygon = _numbered_pad(shape="custom", rotation=45, polygon=corners)

    for pad in (rotated_rect, custom_polygon):
        result = _score(truth, footprint=_footprint(pad))
        assert "corpus_pad_outline_mismatch" not in {finding.code for finding in result.findings}


def test_pad_rotation_is_compared_modulo_180_for_rectangles() -> None:
    truth = _truth(
        expected_pads=[
            corpus.CorpusPad(number="1", center=(0, 0), size=(1, 1), shape="rect", rotation=10)
        ]
    )
    equivalent = _score(truth, footprint=_footprint(_numbered_pad(rotation=190)))
    mismatched = _score(truth, footprint=_footprint(_numbered_pad(rotation=11)))

    assert "corpus_pad_rotation_mismatch" not in {finding.code for finding in equivalent.findings}
    assert "corpus_pad_rotation_mismatch" in {finding.code for finding in mismatched.findings}


def test_duplicate_pad_numbers_match_by_center_and_side() -> None:
    truth = _truth(
        expected_pads=[
            corpus.CorpusPad(number="2", center=(0, 0), size=(1, 1), shape="rect"),
            corpus.CorpusPad(number="2", center=(2, 0), size=(1, 1), shape="rect"),
        ]
    )
    swapped = _score(
        truth,
        footprint=_footprint(
            _numbered_pad(number="2", x=2),
            _numbered_pad(number="2", x=0),
        ),
    )
    moved = _score(
        truth,
        footprint=_footprint(
            _numbered_pad(number="2", x=0),
            _numbered_pad(number="2", x=2.5),
        ),
    )
    bottom_truth = _truth(
        expected_pads=[
            corpus.CorpusPad(number="2", center=(0, 0), size=(1, 1), side="top"),
            corpus.CorpusPad(number="2", center=(2, 0), size=(1, 1), side="bottom"),
        ]
    )
    correct_side = _score(
        bottom_truth,
        footprint=_footprint(
            _numbered_pad(number="2", x=2, layers=["B.Cu"]),
            _numbered_pad(number="2", x=0, layers=["F.Cu"]),
        ),
    )
    wrong_side = _score(
        _truth(
            expected_pads=[
                corpus.CorpusPad(
                    number="1",
                    center=(0, 0),
                    size=(1, 1),
                    side="bottom",
                )
            ]
        ),
        footprint=_footprint(_numbered_pad(layers=["F.Cu"])),
    )

    assert "corpus_pad_geometry_mismatch" not in {finding.code for finding in swapped.findings}
    assert "corpus_pad_geometry_mismatch" in {finding.code for finding in moved.findings}
    assert "corpus_pad_side_mismatch" not in {finding.code for finding in correct_side.findings}
    assert "corpus_pad_side_mismatch" in {finding.code for finding in wrong_side.findings}


def test_pad_outline_type_and_size_omission_findings() -> None:
    polygon_truth = _truth(
        expected_pads=[
            corpus.CorpusPad(
                number="1",
                center=(0, 0),
                polygon=[(-0.5, -0.5), (-0.5, 0.5), (0.5, 0.5), (0.5, -0.5)],
            )
        ]
    )
    outline_result = _score(
        polygon_truth,
        footprint=_footprint(_numbered_pad(shape="oval")),
    )
    assert "corpus_pad_outline_mismatch" in {finding.code for finding in outline_result.findings}

    type_truth = _truth(
        expected_pads=[
            corpus.CorpusPad(
                number="1",
                center=(0, 0),
                size=(1, 1),
                pad_type="smd",
            )
        ]
    )
    type_result = _score(
        type_truth,
        footprint=_footprint(_numbered_pad(pad_type="thru_hole")),
    )
    assert "corpus_pad_type_mismatch" in {finding.code for finding in type_result.findings}

    drill_only = corpus.CorpusPad(
        number="1",
        center=(0, 0),
        size=None,
        drill=0.8,
        shape="circle",
        pad_type="thru_hole",
        rotation=0,
    )
    assert drill_only.size is None
    with pytest.raises(ValidationError, match="require a drill"):
        corpus.CorpusPad(number="1", center=(0, 0), size=None)
    drill_truth = _truth(expected_pads=[drill_only])
    drill_result = _score(
        drill_truth,
        footprint=_footprint(
            _numbered_pad(
                shape="circle",
                pad_type="smd",
                drill=0.82,
                width=0.4,
                height=0.4,
            )
        ),
    )
    drill_codes = {finding.code for finding in drill_result.findings}
    assert "corpus_pad_drill_mismatch" in drill_codes
    assert "corpus_pad_type_mismatch" in drill_codes
    assert "corpus_pad_geometry_mismatch" not in drill_codes


def _mechanical_pad(
    *,
    x: float = 1,
    y: float = 2,
    drill: float | None = 0.8,
    width: float = 2,
    height: float = 2,
    pad_type: str = "thru_hole",
) -> libitems.PadDef:
    return _numbered_pad(
        number="",
        x=x,
        y=y,
        drill=drill,
        width=width,
        height=height,
        pad_type=pad_type,
        shape="circle",
    )


def test_mechanical_truth_matches_unnumbered_pads_and_reports_mismatches() -> None:
    hole = corpus.CorpusMechanicalHole(
        kind="mounting",
        center=(1, 2),
        drill=0.8,
        size=(2, 2),
        plated=True,
    )
    truth = _truth(expected_mechanical=[hole])
    numbered = _numbered_pad()

    missing = _score(truth, footprint=_footprint(numbered))
    assert "corpus_mechanical_missing" in {finding.code for finding in missing.findings}

    correct = _score(truth, footprint=_footprint(numbered, _mechanical_pad()))
    assert not {
        finding.code
        for finding in correct.findings
        if finding.code.startswith("corpus_mechanical_")
    }

    drill_mismatch = _score(
        truth,
        footprint=_footprint(numbered, _mechanical_pad(drill=0.82)),
    )
    assert "corpus_mechanical_drill_mismatch" in {
        finding.code for finding in drill_mismatch.findings
    }

    plating_mismatch = _score(
        truth,
        footprint=_footprint(numbered, _mechanical_pad(pad_type="np_thru_hole")),
    )
    assert "corpus_mechanical_plating_mismatch" in {
        finding.code for finding in plating_mismatch.findings
    }

    size_mismatch = _score(
        truth,
        footprint=_footprint(numbered, _mechanical_pad(width=2.1)),
    )
    assert "corpus_mechanical_size_mismatch" in {finding.code for finding in size_mismatch.findings}

    unexpected = _score(
        truth,
        footprint=_footprint(numbered, _mechanical_pad(), _mechanical_pad(x=4)),
    )
    assert "corpus_mechanical_unexpected" in {finding.code for finding in unexpected.findings}

    no_mechanical_truth = _score(
        _truth(),
        footprint=_footprint(numbered, _mechanical_pad()),
    )
    assert "corpus_mechanical_unexpected" not in {
        finding.code for finding in no_mechanical_truth.findings
    }


def test_unplated_smd_mechanical_pad_requires_size_without_guessing_plating() -> None:
    hole = corpus.CorpusMechanicalHole(
        kind="retention",
        center=(1, 2),
        drill=None,
        size=(2, 1),
        plated=None,
    )
    truth = _truth(expected_mechanical=[hole])
    correct = _score(
        truth,
        footprint=_footprint(
            _numbered_pad(),
            _mechanical_pad(drill=None, width=2, height=1, pad_type="smd"),
        ),
    )
    incorrect = _score(
        truth,
        footprint=_footprint(
            _numbered_pad(),
            _mechanical_pad(drill=0.8, width=2, height=1, pad_type="smd"),
        ),
    )
    assert not {
        "corpus_mechanical_drill_mismatch",
        "corpus_mechanical_size_mismatch",
        "corpus_mechanical_plating_mismatch",
        "corpus_plating_guessed",
    } & {finding.code for finding in correct.findings}
    assert "corpus_mechanical_drill_mismatch" in {finding.code for finding in incorrect.findings}
    assert "corpus_plating_guessed" not in {finding.code for finding in incorrect.findings}
    with pytest.raises(ValidationError, match="copper size and unknown plating"):
        corpus.CorpusMechanicalHole(
            kind="retention",
            center=(1, 2),
            drill=None,
            size=None,
            plated=None,
        )
    with pytest.raises(ValidationError, match="copper size and unknown plating"):
        corpus.CorpusMechanicalHole(
            kind="retention",
            center=(1, 2),
            drill=None,
            size=(2, 1),
            plated=False,
        )


def test_unknown_plating_rejects_footprint_and_partspec_guesses() -> None:
    hole = corpus.CorpusMechanicalHole(
        kind="locating",
        center=(-3, 0),
        drill=1,
        size=(2, 2),
        plated=None,
    )
    truth = _truth(expected_mechanical=[hole])
    footprint = _footprint(_numbered_pad(), _mechanical_pad(x=-3, y=0, drill=1, width=2))

    footprint_guess = _score(truth, footprint=footprint)
    assert "corpus_plating_guessed" in {finding.code for finding in footprint_guess.findings}

    connector = connector_spec("jst_ph_tht")
    partspec_guess = _score(truth, partspec=connector, footprint=footprint)
    assert "corpus_plating_guessed" in {finding.code for finding in partspec_guess.findings}

    human_request = _truth(
        expected_outcome="human_request",
        expected_pads=[],
        dimensions=corpus.CorpusDimensions(),
        pins={},
        expected_mechanical=[hole],
    )
    human_request_score = _score(human_request, partspec=connector, footprint=footprint)
    assert "corpus_plating_guessed" in {finding.code for finding in human_request_score.findings}


def test_connector_truth_validation_and_comparison() -> None:
    with pytest.raises(ValidationError, match="connector truth must be present"):
        _truth(package_family="connector")
    with pytest.raises(ValidationError, match="connector truth must be present"):
        _truth(
            connector=corpus.CorpusConnector(
                mount="tht",
                orientation="vertical",
                gender="none",
                mating_axis="+z",
            )
        )

    spec = connector_spec("jst_ph_right_angle")
    connector_truth = _corpus_connector(spec)
    truth = _truth(
        package_family="connector",
        connector=connector_truth,
        dimensions=corpus.CorpusDimensions(),
        expected_pads=[],
    )
    assert set(corpus._missing_truth_requirements(truth)) == {  # pyright: ignore[reportPrivateUsage]
        "body_length",
        "body_width",
        "height",
        "expected_pads",
    }
    matching = _score(truth, partspec=spec, footprint=_footprint())
    assert "corpus_connector_mismatch" not in {finding.code for finding in matching.findings}
    with_unexpected_mechanical = _score(
        truth,
        partspec=spec,
        footprint=_footprint(_numbered_pad(), _mechanical_pad()),
    )
    assert "corpus_mechanical_unexpected" in {
        finding.code for finding in with_unexpected_mechanical.findings
    }

    missing_connector = _score(
        truth,
        partspec=spec.model_copy(update={"connector": None}),
        footprint=_footprint(),
    )
    assert "corpus_connector_missing" in {finding.code for finding in missing_connector.findings}

    actual_connector = spec.connector
    assert actual_connector is not None
    actual_board_edge = actual_connector.board_edge
    assert actual_board_edge is not None
    changed_edge = actual_connector.model_copy(
        update={
            "board_edge": actual_board_edge.model_copy(
                update={"offset": Dimension(nom=1, reading=_reading("1 mm"))}
            )
        }
    )
    offset_mismatch = _score(
        truth,
        partspec=spec.model_copy(update={"connector": changed_edge}),
        footprint=_footprint(),
    )
    assert any(
        finding.code == "corpus_connector_mismatch"
        and finding.field == "partspec.connector.board_edge_offset"
        for finding in offset_mismatch.findings
    )

    skipped_map_truth = _truth(
        package_family="connector",
        connector=connector_truth.model_copy(update={"manufacturer_to_kicad": None}),
        dimensions=corpus.CorpusDimensions(),
        expected_pads=[],
    )
    skipped_map = _score(skipped_map_truth, partspec=spec, footprint=_footprint())
    assert not any(
        finding.code == "corpus_connector_mismatch"
        and finding.field.endswith("manufacturer_to_kicad")
        for finding in skipped_map.findings
    )


def test_connector_model_checks_only_z_height() -> None:
    spec = connector_spec("header_2x5")
    connector = _corpus_connector(spec)
    truth = _truth(
        package_family="connector",
        connector=connector,
        dimensions=corpus.CorpusDimensions(
            body_length=corpus.CorpusDimension(nom=1),
            body_width=corpus.CorpusDimension(nom=2),
            height=corpus.CorpusDimension(nom=3),
        ),
        expected_pads=[],
    )
    symbol = _symbol(truth)
    footprint = _footprint()

    matching_height = corpus.score_part(
        truth,
        spec,
        footprint,
        symbol,
        occt.box(0, 0, 0, 5, 6, 3),
    )
    assert "model_geometry_mismatch" not in {finding.code for finding in matching_height.findings}

    wrong_height = corpus.score_part(
        truth,
        spec,
        footprint,
        symbol,
        occt.box(0, 0, 0, 5, 6, 3.1),
    )
    assert any(
        finding.code == "model_geometry_mismatch" and finding.field == "model.z"
        for finding in wrong_height.findings
    )


def test_ambiguous_drawing_ids_and_human_request_requirements() -> None:
    truth = _truth(
        expected_outcome="human_request",
        expected_pads=[],
        dimensions=corpus.CorpusDimensions(),
        drawing_id=None,
        ambiguous_drawing_ids=["TEST"],
    )
    result = _score(truth)
    assert "corpus_drawing_guessed" in {finding.code for finding in result.findings}
    assert "corpus_expected_pads_unavailable" not in {finding.code for finding in result.findings}
    assert corpus._missing_truth_requirements(truth) == []  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(ValidationError, match="ambiguous drawing ids"):
        _truth(ambiguous_drawing_ids=["TEST"])
    with pytest.raises(ValidationError, match="ambiguous drawing ids"):
        _truth(
            expected_outcome="human_request",
            drawing_id="test",
            ambiguous_drawing_ids=["TEST"],
        )

    no_pin_truth = _truth(
        expected_outcome="human_request",
        expected_pads=[],
        dimensions=corpus.CorpusDimensions(),
        pins={},
    )
    assert corpus._missing_truth_requirements(no_pin_truth) == []  # pyright: ignore[reportPrivateUsage]
    no_pin_score = _score(no_pin_truth)
    assert not {
        "corpus_expected_pads_unavailable",
        "corpus_pin_map_mismatch",
        "corpus_symbol_pin_map_mismatch",
    } & {finding.code for finding in no_pin_score.findings}

    with pytest.raises(ValidationError, match="empty pin truth is allowed only"):
        _truth(pins={})


def test_human_request_allows_missing_footprint_and_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truth_root = tmp_path / "corpus"
    (truth_root / "truth").mkdir(parents=True)
    truth = _truth(
        expected_outcome="human_request",
        expected_pads=[],
        dimensions=corpus.CorpusDimensions(),
    )
    (truth_root / "truth" / "synthetic-entry.json").write_text(
        truth.model_dump_json(by_alias=True),
        encoding="utf-8",
    )
    entry = corpus.CorpusEntry(
        id=truth.id,
        manufacturer="Example",
        mpn="TEST",
        package_family="custom",
        datasheet=corpus.CorpusDatasheet(
            url="https://example.invalid/test.pdf",
            sha256="a" * 64,
            revision="A",
        ),
        truth_path="truth/synthetic-entry.json",
        truth_status="unconfirmed",
        notes="Synthetic test entry.",
    )
    manifest = corpus.CorpusManifest(schema="circuit_golden_corpus", version=1, entries=[entry])
    (truth_root / "corpus.json").write_text(
        manifest.model_dump_json(by_alias=True),
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    partspec_path = candidate / "part.spec.json"
    partspec_path.write_text(_partspec().model_dump_json(), encoding="utf-8")
    symbol_path = candidate / "symbols.kicad_sym"
    symbol_path.write_text("unused by the parser stub", encoding="utf-8")
    monkeypatch.setenv("CIRCUIT_CORPUS_CACHE", "")

    def parse_symbol(_path: Path, _name: str) -> libitems.SymbolDef:
        return _symbol(truth)

    monkeypatch.setattr(corpus.libitems, "parse_symbol", parse_symbol)
    result = corpus.score_entry(
        truth_root,
        truth.id,
        partspec_path,
        candidate / "missing.kicad_mod",
        symbol_path,
        "TEST",
        candidate / "missing.step",
    )

    codes = {finding.code for finding in result.findings}
    assert result.verdict == "not_available"
    assert (
        not {
            "footprint_unavailable",
            "model_unavailable",
            "model_geometry_mismatch",
            "corpus_truth_incomplete",
        }
        & codes
    )


def test_drawing_id_comparison_is_case_insensitive() -> None:
    truth = _truth(drawing_id="TEST")
    assert "corpus_drawing_id_mismatch" not in {finding.code for finding in _score(truth).findings}
    mismatched = _score(_truth(drawing_id="OTHER"))
    assert "corpus_drawing_id_mismatch" in {finding.code for finding in mismatched.findings}


def test_load_truth_rejects_manifest_family_mismatch(tmp_path: Path) -> None:
    truth_root = tmp_path / "corpus"
    (truth_root / "truth").mkdir(parents=True)
    truth = _truth()
    (truth_root / "truth" / "synthetic-entry.json").write_text(
        truth.model_dump_json(by_alias=True),
        encoding="utf-8",
    )
    entry = corpus.CorpusEntry(
        id=truth.id,
        manufacturer="Example",
        mpn="TEST",
        package_family="chip",
        datasheet=corpus.CorpusDatasheet(
            url="https://example.invalid/test.pdf",
            sha256="a" * 64,
            revision="A",
        ),
        truth_path="truth/synthetic-entry.json",
        truth_status="unconfirmed",
        notes="Synthetic test entry.",
    )
    with pytest.raises(corpus.CorpusError, match="package family"):
        corpus.load_truth(truth_root, entry)


def test_mcp1700_truth_stays_incomplete_without_datasheet_geometry() -> None:
    manifest = corpus.load_manifest(CORPUS_ROOT / "corpus.json")
    entry = next(item for item in manifest.entries if item.id == "mcp1700-sot23")
    truth, _ = corpus.load_truth(CORPUS_ROOT, entry)
    missing = corpus._missing_truth_requirements(truth)  # pyright: ignore[reportPrivateUsage]
    required = {
        "body_length",
        "body_width",
        "height",
        "pitch",
        "lead_span",
        "lead_length",
        "lead_width",
    }
    assert required <= set(missing)
    assert "expected_pads" in missing


def test_datasheet_cache_requires_exact_sha256(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = corpus.load_manifest(CORPUS_ROOT / "corpus.json").entries[0]
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("CIRCUIT_CORPUS_CACHE", str(cache))
    assert corpus._cached_pdf_status(entry) == "not_available"  # pyright: ignore[reportPrivateUsage]
    pdf = cache / "cached.pdf"
    pdf.write_bytes(b"not the configured datasheet")
    assert corpus._cached_pdf_status(entry) == "hash_mismatch"  # pyright: ignore[reportPrivateUsage]
    pdf.write_bytes(b"verified")
    entry.datasheet.sha256 = hashlib.sha256(b"verified").hexdigest()
    assert corpus._cached_pdf_status(entry) == "verified"  # pyright: ignore[reportPrivateUsage]


def test_human_confirmation_requires_hash_bound_pr_b_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truth_digest = "a" * 64
    event_digest = "b" * 64
    entry = corpus.CorpusEntry(
        id="fixture",
        manufacturer="Example",
        mpn="FIXTURE",
        package_family="custom",
        datasheet=corpus.CorpusDatasheet(
            url="https://example.invalid/fixture.pdf",
            sha256="c" * 64,
            revision="A",
        ),
        truth_path="truth/fixture.json",
        truth_status="human_confirmed",
        confirmed_by="Test Reviewer",
        confirmed_at="2026-01-01T00:00:00Z",
        notes="fixture",
    )
    packet_id = corpus.approval_packet_id(entry.id, truth_digest)
    approval_dir = tmp_path / "corpus" / "approvals"
    approval_dir.mkdir(parents=True)
    approval = corpus.CorpusApproval(
        schema="circuit_corpus_truth_approval",
        version=1,
        entry_id=entry.id,
        packet_id=packet_id,
        truth_sha256=truth_digest,
        event_sha256=event_digest,
        reviewer="Test Reviewer",
        confirmed_at="2026-01-01T00:00:00Z",
    )
    (approval_dir / "fixture.json").write_text(
        approval.model_dump_json(by_alias=True),
        encoding="utf-8",
    )
    seen_packet_ids: list[str] = []

    def load_decisions(_library_root: Path, seen_id: str) -> list[libreview.ReviewDecision]:
        seen_packet_ids.append(seen_id)
        return [
            libreview.ReviewDecision(
                packet_id=seen_id,
                decision="approve",
                reviewer="Test Reviewer",
                answers={"truth_sha256": truth_digest},
                corrections=[],
                event_sha256=event_digest,
                valid=True,
                reasons=[],
                integrity_valid=True,
            )
        ]

    monkeypatch.setattr(corpus.libreview, "load_decisions", load_decisions)
    assert corpus._approval_is_valid(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "corpus",
        entry,
        truth_digest,
    ) == (True, "human_confirmed")
    assert seen_packet_ids == [packet_id]
    assert corpus._approval_is_valid(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "corpus",
        entry,
        "d" * 64,
    ) == (False, "corpus_approval_event_invalid")


def test_unconfirmed_truth_never_scores_as_pass() -> None:
    entry = corpus.load_manifest(CORPUS_ROOT / "corpus.json").entries[0]
    truth, _ = corpus.load_truth(CORPUS_ROOT, entry)
    footprint = libitems.FootprintDef(
        name="",
        attributes=[],
        pads=[],
        graphics=[],
        models=[],
        properties={},
    )
    symbol = libitems.SymbolDef(name="", pins=[], properties={})
    model = occt.box(0, 0, 0, 0.001, 0.001, 0.001)
    arguments = (truth, _partspec(), footprint, symbol, model)
    result = corpus.score_part(*arguments)
    repeated = corpus.score_part(*arguments)
    assert result.truth_status == "unconfirmed"
    assert result.verdict != "pass"
    assert result.truth_sha256
    assert result.manifest_sha256 == ""
    assert repeated == result


@pytest.mark.parametrize("artifact_name", ["partspec", "footprint", "symbol"])
def test_authored_canary_is_an_error(artifact_name: str) -> None:
    entry = corpus.load_manifest(CORPUS_ROOT / "corpus.json").entries[0]
    truth, _ = corpus.load_truth(CORPUS_ROOT, entry)
    result = corpus.score_part(
        truth,
        _partspec(truth.canary if artifact_name == "partspec" else "VCC"),
        libitems.FootprintDef(
            name=truth.canary if artifact_name == "footprint" else "",
            attributes=[],
            pads=[],
            graphics=[],
            models=[],
            properties={},
        ),
        libitems.SymbolDef(
            name=truth.canary if artifact_name == "symbol" else "",
            pins=[],
            properties={},
        ),
        occt.box(0, 0, 0, 0.001, 0.001, 0.001),
    )
    finding = next(item for item in result.findings if item.code == "corpus_canary_leak")
    assert finding.severity == "error"


def test_model_file_canary_is_an_error(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus"
    shutil.copytree(CORPUS_ROOT, corpus_root)
    entry = corpus.load_manifest(corpus_root / "corpus.json").entries[0]
    truth, _ = corpus.load_truth(corpus_root, entry)
    partspec_path = tmp_path / "part.spec.json"
    partspec_path.write_text(_partspec().model_dump_json(), encoding="utf-8")
    footprint_path = tmp_path / "footprint.kicad_mod"
    symbol_path = tmp_path / "symbols.kicad_sym"
    model_path = tmp_path / "model.step"
    model_path.write_text(truth.canary, encoding="utf-8")
    result = corpus.score_entry(
        corpus_root,
        entry.id,
        partspec_path,
        footprint_path,
        symbol_path,
        "TEST",
        model_path,
    )
    assert "corpus_canary_leak" in {finding.code for finding in result.findings}


def test_author_lane_cannot_load_or_score_corpus_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = corpus.load_manifest(CORPUS_ROOT / "corpus.json")
    monkeypatch.setenv("CIRCUIT_AUTHORING_LANE", "b")
    with pytest.raises(corpus.CorpusError, match="author lanes cannot read"):
        corpus.load_truth(CORPUS_ROOT, manifest.entries[0])
