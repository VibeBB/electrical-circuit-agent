from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Literal

import pytest

from circuit import connplace, corpus, libitems, sexpr
from circuit.partspec import (
    ConnectorBoardEdge,
    ConnectorMatingEnvelope,
    ConnectorMechanicalFeature,
    ConnectorSpec,
    Dimension,
    PartSpec,
    PinSpec,
    Reading,
)
from connector_fixtures import connector_spec

REPO_ROOT = Path(__file__).parents[1]
CORPUS_ROOT = REPO_ROOT / "library" / "corpus"
OFFICIAL_IDS = {
    "tps7a02-dqn",
    "tlv7031-dpw",
    "lm1117-ktt",
    "bq24072-rgt",
    "harwin-kona-ka1-mv10405m1",
    "harwin-gecko-g125-mh104m4",
    "harwin-m80-5l21605m7-03-314",
    "gct-usb4105-gf-a",
    "gct-ffc3b11-10-t",
    "we-wr-sma-60312002114503",
    "we-wr-sma-60312202114514",
    "jst-ph-b4b-ph-k-s",
}


def _reading(text: str) -> Reading:
    return Reading(page=1, bbox=(0, 0, 1, 1), vision=text, vision_record="official-truth-test")


def _dimension(value: corpus.CorpusDimension | None, label: str) -> Dimension:
    if value is None or all(bound is None for bound in (value.min, value.nom, value.max)):
        return Dimension(nom=1.0, reading=_reading(f"test-only fallback for {label}"))
    return Dimension(
        min=value.min,
        nom=value.nom,
        max=value.max,
        reading=_reading(f"test fixture from official {label} truth"),
    )


def _connector_for_truth(
    truth: corpus.CorpusTruth,
    *,
    mating_mirror: bool | None = None,
    board_edge_offset: Dimension | None = None,
    board_edge_override: bool = False,
) -> ConnectorSpec:
    expected = truth.connector
    assert expected is not None
    template_kind = "gecko" if expected.mount == "tht" else "jst_ph_right_angle"
    template = connector_spec(template_kind).connector
    assert template is not None
    edge: ConnectorBoardEdge | None = None
    if expected.board_edge_side is not None and not board_edge_override:
        offset = board_edge_offset or _dimension(expected.board_edge_offset, "board-edge offset")
        edge = ConnectorBoardEdge(
            side=expected.board_edge_side,
            offset=offset,
            source_note="Test fixture bound to official corpus board-edge truth",
        )
    elif board_edge_override:
        edge = None
    numbering = template.numbering.model_copy(
        update={
            "mating_mirror": (expected.mating_mirror if mating_mirror is None else mating_mirror),
            "manufacturer_to_kicad": (
                expected.manufacturer_to_kicad or template.numbering.manufacturer_to_kicad
            ),
        }
    )
    return template.model_copy(
        update={
            "mount": expected.mount,
            "orientation": expected.orientation,
            "gender": expected.gender,
            "mating_axis": expected.mating_axis,
            "board_edge": edge,
            "numbering": numbering,
        }
    )


def _partspec_for_truth(
    truth: corpus.CorpusTruth,
    *,
    drawing_id: str | None = None,
    mating_mirror: bool | None = None,
    board_edge_offset: Dimension | None = None,
    board_edge_override: bool = False,
) -> PartSpec:
    base = connector_spec("jst_ph_tht")
    connector = (
        _connector_for_truth(
            truth,
            mating_mirror=mating_mirror,
            board_edge_offset=board_edge_offset,
            board_edge_override=board_edge_override,
        )
        if truth.connector is not None
        else None
    )
    pin_names = dict(truth.pins)
    if not pin_names and connector is not None:
        pin_names = {
            number: "UNSPECIFIED" for number in connector.numbering.manufacturer_to_kicad.values()
        }
    package = base.package.model_copy(
        update={
            "family": truth.package_family,
            "drawing_id": drawing_id or truth.drawing_id or f"TEST-{truth.id}",
            "pin_count": max(1, len(pin_names)),
            "pitch": (
                _dimension(truth.dimensions.pitch, "pitch")
                if truth.dimensions.pitch is not None
                else None
            ),
            "body_length": _dimension(truth.dimensions.body_length, "body length"),
            "body_width": _dimension(truth.dimensions.body_width, "body width"),
            "height": _dimension(truth.dimensions.height, "height"),
            "drawing_view": truth.drawing_view,
            "pin1_corner": truth.pin1_corner,
        }
    )
    pins = [
        PinSpec(
            number=number,
            name=name,
            electrical_type="passive",
            reading=_reading(f"test fixture pin {number}: {name}"),
        )
        for number, name in pin_names.items()
    ]
    if not pins:
        pins = [
            PinSpec(
                number="1",
                name="UNSPECIFIED",
                electrical_type="unspecified",
                reading=_reading("test-only placeholder pin"),
            )
        ]
    return base.model_copy(
        update={
            "mpn": f"TEST-{truth.id}",
            "package": package,
            "connector": connector,
            "pins": pins,
        }
    )


def _symbol_for_truth(truth: corpus.CorpusTruth) -> libitems.SymbolDef:
    return libitems.SymbolDef(
        name="",
        pins=[
            libitems.SymPin(
                number=number,
                name=name,
                electrical_type="passive",
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


def _footprint(*pads: libitems.PadDef) -> libitems.FootprintDef:
    return libitems.FootprintDef(
        name="test-authored",
        attributes=[],
        pads=list(pads),
        graphics=[],
        models=[],
        properties={},
    )


def _pad_from_truth(pad: corpus.CorpusPad) -> libitems.PadDef:
    if pad.polygon is not None:
        xs = [point[0] for point in pad.polygon]
        ys = [point[1] for point in pad.polygon]
        width = max(xs) - min(xs)
        height = max(ys) - min(ys)
        shape = "custom"
        polygon = list(pad.polygon)
    elif pad.size is not None:
        width, height = pad.size
        shape = pad.shape or "rect"
        polygon = None
    else:
        width = height = pad.drill or 1.0
        shape = pad.shape or "circle"
        polygon = None
    return libitems.PadDef(
        number=pad.number,
        type=pad.pad_type or "smd",
        shape=shape,
        x=pad.center[0],
        y=pad.center[1],
        rotation=pad.rotation,
        width=width,
        height=height,
        drill=pad.drill,
        layers=["B.Cu" if pad.side == "bottom" else "F.Cu"],
        polygon=polygon,
    )


def _footprint_for_truth(truth: corpus.CorpusTruth) -> libitems.FootprintDef:
    return _footprint(*(_pad_from_truth(pad) for pad in truth.expected_pads))


def _score(
    truth: corpus.CorpusTruth,
    *,
    partspec: PartSpec | None = None,
    footprint: libitems.FootprintDef | None = None,
) -> corpus.CorpusScore:
    return corpus.score_part(
        truth,
        partspec or _partspec_for_truth(truth),
        footprint,
        _symbol_for_truth(truth),
        None,
    )


def _truth(entry_id: str) -> corpus.CorpusTruth:
    manifest = corpus.load_manifest(CORPUS_ROOT / "corpus.json")
    entry = next(item for item in manifest.entries if item.id == entry_id)
    truth, _ = corpus.load_truth(CORPUS_ROOT, entry)
    return truth


def _finding_codes(score: corpus.CorpusScore) -> set[str]:
    return {finding.code for finding in score.findings}


def test_official_manifest_additions_are_unconfirmed_and_not_synthetic() -> None:
    manifest = corpus.load_manifest(CORPUS_ROOT / "corpus.json")
    entries = {entry.id: entry for entry in manifest.entries}
    assert len(entries) >= 52
    assert entries.keys() >= OFFICIAL_IDS
    for entry_id in OFFICIAL_IDS:
        entry = entries[entry_id]
        assert entry.truth_status == "unconfirmed"
        assert entry.confirmed_by is None
        assert entry.confirmed_at is None
        assert "Not human-confirmed" in entry.notes
        assert "synthetic" not in entry.notes.casefold()
        assert Path(entry.truth_path).parts[0] != "tests"
        assert re.fullmatch(r"[0-9a-f]{64}", entry.datasheet.sha256)


def test_official_truth_files_validate_with_unique_canaries_and_families() -> None:
    manifest = corpus.load_manifest(CORPUS_ROOT / "corpus.json")
    canaries: set[str] = set()
    for entry in manifest.entries:
        truth, _ = corpus.load_truth(CORPUS_ROOT, entry)
        assert truth.id == entry.id
        assert truth.package_family == entry.package_family
        assert truth.canary not in canaries
        canaries.add(truth.canary)
        if entry.package_family == "connector":
            assert truth.connector is not None
            assert (
                truth.expected_outcome == "human_request"
                or truth.expected_mechanical
                or any(pad.pad_type is not None for pad in truth.expected_pads)
            )


def test_tps7a02_dqn_custom_outlines_and_wrong_variant() -> None:
    truth = _truth("tps7a02-dqn")
    correct = _score(truth, footprint=_footprint_for_truth(truth))
    assert not {
        "corpus_pad_geometry_mismatch",
        "corpus_pad_outline_mismatch",
        "corpus_pad_shape_mismatch",
    } & _finding_codes(correct)

    pads = list(_footprint_for_truth(truth).pads)
    thermal_index = next(index for index, pad in enumerate(pads) if pad.number == "5")
    pads[thermal_index] = pads[thermal_index].model_copy(
        update={
            "shape": "rect",
            "width": 0.48,
            "height": 0.48,
            "rotation": 45.0,
            "polygon": None,
        }
    )
    rotated_rect = _score(truth, footprint=_footprint(*pads))
    assert "corpus_pad_outline_mismatch" not in _finding_codes(rotated_rect)

    wrong_variant = _truth("tps7a02-sot23-5")
    wrong_footprint = _footprint_for_truth(wrong_variant)
    mismatched = _score(truth, footprint=wrong_footprint)
    assert {
        "corpus_pad_geometry_mismatch",
        "corpus_pad_outline_mismatch",
    } & _finding_codes(mismatched)

    pad1_index = next(index for index, pad in enumerate(pads) if pad.number == "1")
    pad4_index = next(index for index, pad in enumerate(pads) if pad.number == "4")
    original = list(_footprint_for_truth(truth).pads)
    first, fourth = original[pad1_index], original[pad4_index]
    for index, source in ((pad1_index, fourth), (pad4_index, first)):
        original[index] = original[index].model_copy(
            update={
                "x": source.x,
                "y": source.y,
                "shape": source.shape,
                "rotation": source.rotation,
                "width": source.width,
                "height": source.height,
                "polygon": source.polygon,
            }
        )
    swapped = _score(truth, footprint=_footprint(*original))
    assert "corpus_pad_outline_mismatch" in _finding_codes(swapped)

    assert truth.pins == {
        "1": "OUT",
        "2": "GND",
        "3": "EN",
        "4": "IN",
        "5": "Thermal Pad",
    }
    assert truth.dimensions.body_length is not None
    assert truth.dimensions.body_width is not None
    assert truth.dimensions.body_length.nom is None
    assert truth.dimensions.body_width.nom is None


def test_tlv7031_dpw_center_pad_and_unchamfered_signal_pads() -> None:
    truth = _truth("tlv7031-dpw")
    center_pad = next(pad for pad in truth.expected_pads if truth.pins[pad.number] == "V-")
    assert center_pad.center == (0.0, 0.0)

    pads: list[libitems.PadDef] = []
    for pad in truth.expected_pads:
        actual = _pad_from_truth(pad)
        if pad.number != "3":
            actual = actual.model_copy(
                update={
                    "shape": "rect",
                    "width": 0.42,
                    "height": 0.22,
                    "rotation": 0.0,
                    "polygon": None,
                }
            )
        pads.append(actual)
    result = _score(truth, footprint=_footprint(*pads))
    assert "corpus_pad_outline_mismatch" in _finding_codes(result)


def test_lm1117_ktt_stepped_tab_and_pad_numbers() -> None:
    truth = _truth("lm1117-ktt")
    assert "2" not in {pad.number for pad in truth.expected_pads}
    tab = next(pad for pad in truth.expected_pads if pad.number == "4")
    tab_index = next(index for index, pad in enumerate(truth.expected_pads) if pad.number == "4")
    pads = list(_footprint_for_truth(truth).pads)
    assert tab.center == (4.13, 0.0)
    pads[tab_index] = pads[tab_index].model_copy(
        update={
            "shape": "rect",
            "width": 6.99,
            "height": 10.8,
            "rotation": 0.0,
            "polygon": None,
        }
    )
    rectangular_tab = _score(truth, footprint=_footprint(*pads))
    assert "corpus_pad_outline_mismatch" in _finding_codes(rectangular_tab)

    pads.append(
        _pad_from_truth(next(pad for pad in truth.expected_pads if pad.number == "1")).model_copy(
            update={"number": "2"}
        )
    )
    extra_number = _score(truth, footprint=_footprint(*pads))
    assert "corpus_pad_numbers_mismatch" in _finding_codes(extra_number)
    assert truth.dimensions.body_length is not None
    assert truth.dimensions.body_length.nom is None


def test_bq24072_ambiguous_drawing_is_not_selected() -> None:
    truth = _truth("bq24072-rgt")
    assert truth.expected_outcome == "human_request"
    assert truth.ambiguous_drawing_ids == ["RGT0016B", "RGT0016C"]
    assert truth.drawing_id is None
    selected = _partspec_for_truth(truth, drawing_id="RGT0016C")
    result = _score(truth, partspec=selected, footprint=None)
    assert "corpus_drawing_guessed" in _finding_codes(result)
    assert "footprint_unavailable" not in _finding_codes(result)


@pytest.mark.parametrize("pad_type", ["np_thru_hole", "thru_hole"])
def test_kona_unknown_mounting_hole_plating_cannot_be_guessed(
    pad_type: Literal["np_thru_hole", "thru_hole"],
) -> None:
    truth = _truth("harwin-kona-ka1-mv10405m1")
    pads = list(_footprint_for_truth(truth).pads)
    for x in (-21.0, 21.0):
        pads.append(
            libitems.PadDef(
                number="",
                type=pad_type,
                shape="circle",
                x=x,
                y=0,
                rotation=0,
                width=6.4,
                height=6.4,
                drill=3.2,
                layers=["*.Cu"],
            )
        )
    result = _score(truth, footprint=_footprint(*pads))
    assert "corpus_plating_guessed" in _finding_codes(result)


def test_kona_contact_variant_numbering_and_mating_mirror() -> None:
    truth = _truth("harwin-kona-ka1-mv10405m1")
    three_contact = _footprint(*(_pad_from_truth(pad) for pad in truth.expected_pads[:3]))
    assert "corpus_pad_numbers_mismatch" in _finding_codes(_score(truth, footprint=three_contact))

    reversed_contacts = _footprint(
        *(
            _pad_from_truth(pad).model_copy(update={"x": -pad.center[0]})
            for pad in truth.expected_pads
        )
    )
    assert "corpus_pad_geometry_mismatch" in _finding_codes(
        _score(truth, footprint=reversed_contacts)
    )

    guessed_mirror = _partspec_for_truth(truth, mating_mirror=False)
    assert "corpus_connector_mismatch" in _finding_codes(
        _score(truth, partspec=guessed_mirror, footprint=_footprint_for_truth(truth))
    )

    guessed_plating = _partspec_for_truth(truth)
    connector = guessed_plating.connector
    assert connector is not None
    plated_feature = ConnectorMechanicalFeature(
        kind="mounting",
        x=Dimension(nom=-21.0, reading=_reading("test-authored hole x")),
        y=Dimension(nom=0.0, reading=_reading("test-authored hole y")),
        drill=Dimension(nom=3.2, reading=_reading("test-authored drill")),
        pad_width=Dimension(nom=6.4, reading=_reading("test-authored copper width")),
        pad_height=Dimension(nom=6.4, reading=_reading("test-authored copper height")),
        plated=True,
        plating_reading=_reading("test-authored plating guess"),
    )
    plated_connector = connector.model_copy(update={"mechanical": [plated_feature]})
    guessed = guessed_plating.model_copy(update={"connector": plated_connector})
    assert "corpus_plating_guessed" in _finding_codes(
        _score(truth, partspec=guessed, footprint=_footprint_for_truth(truth))
    )


def test_gecko_truth_rejects_shielded_offset_and_missing_edge() -> None:
    truth = _truth("harwin-gecko-g125-mh104m4")
    assert truth.connector is not None
    assert truth.connector.board_edge_side == "+y"
    assert truth.connector.board_edge_offset is not None
    assert truth.connector.board_edge_offset.max == 4.9
    shielded_limit = _partspec_for_truth(
        truth,
        board_edge_offset=Dimension(
            nom=2.75,
            reading=_reading("test-authored shielded-cable offset"),
        ),
    )
    missing_edge = _partspec_for_truth(truth, board_edge_override=True)
    for spec in (shielded_limit, missing_edge):
        assert "corpus_connector_mismatch" in _finding_codes(
            _score(truth, partspec=spec, footprint=None)
        )


def _gecko_placement_spec() -> PartSpec:
    truth = _truth("harwin-gecko-g125-mh104m4")
    spec = _partspec_for_truth(truth)
    connector = spec.connector
    assert connector is not None
    test_envelope = ConnectorMatingEnvelope(
        mating_mpn="TEST-AUTHORED-ENVELOPE-NOT-OFFICIAL-TRUTH",
        reading=_reading("Test-authored envelope for placement regression only"),
        box=(-1.0, 5.01, 1.0, 5.5),
        z_min=0.0,
        z_max=5.0,
        travel=Dimension(nom=0.5, reading=_reading("test-authored mating travel")),
        access_margin_mm=0.0,
    )
    return spec.model_copy(
        update={"connector": connector.model_copy(update={"mating_envelope": test_envelope})}
    )


def _gecko_board(path: Path, *, far_from_edge: bool = False, neighbor: bool = False) -> None:
    connector_y = 4.5 if far_from_edge else 5.1
    connector: list[sexpr.SExpr] = [
        "footprint",
        sexpr.quoted("Connector:Harwin_Gecko_TestAuthored"),
        ["layer", sexpr.quoted("F.Cu")],
        ["at", "5", f"{connector_y:g}", "0"],
        [
            "property",
            sexpr.quoted("Reference"),
            sexpr.quoted("J1"),
            ["at", "0", "0", "0"],
            ["layer", sexpr.quoted("F.SilkS")],
        ],
        [
            "fp_rect",
            ["start", "-0.5", "-0.5"],
            ["end", "0.5", "0.5"],
            ["layer", sexpr.quoted("F.CrtYd")],
        ],
        [
            "fp_line",
            ["start", "-1", "4.9"],
            ["end", "1", "4.9"],
            ["layer", sexpr.quoted("Dwgs.User")],
        ],
    ]
    board: list[sexpr.SExpr] = [
        "kicad_pcb",
        ["general", ["thickness", "1.6"]],
        [
            "gr_poly",
            [
                "pts",
                ["xy", "0", "0"],
                ["xy", "10", "0"],
                ["xy", "10", "10"],
                ["xy", "0", "10"],
            ],
            ["layer", sexpr.quoted("Edge.Cuts")],
        ],
        connector,
    ]
    if neighbor:
        board.append(
            [
                "footprint",
                sexpr.quoted("Device:R"),
                ["layer", sexpr.quoted("F.Cu")],
                ["at", "5", "11", "0"],
                [
                    "property",
                    sexpr.quoted("Reference"),
                    sexpr.quoted("R1"),
                    ["at", "0", "0", "0"],
                    ["layer", sexpr.quoted("F.SilkS")],
                ],
                [
                    "fp_rect",
                    ["start", "-0.5", "-0.5"],
                    ["end", "0.5", "0.5"],
                    ["layer", sexpr.quoted("F.CrtYd")],
                ],
            ]
        )
    path.write_text(sexpr.serialize(board) + "\n", encoding="utf-8")


def test_gecko_official_edge_datum_drives_placement_and_clearance(tmp_path: Path) -> None:
    spec = _gecko_placement_spec()
    spec_path = tmp_path / "test-authored-gecko.part.spec.json"
    spec_path.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")

    far_board = tmp_path / "gecko-far.kicad_pcb"
    _gecko_board(far_board, far_from_edge=True)
    far_report = connplace.check_connector_placement(far_board, {"J1": spec_path})
    assert "connector_not_at_board_edge" in {finding.code for finding in far_report.findings}

    edge_board = tmp_path / "gecko-at-edge.kicad_pcb"
    _gecko_board(edge_board)
    edge_report = connplace.check_connector_placement(edge_board, {"J1": spec_path})
    assert edge_report.verdict == "pass"

    obstructed_board = tmp_path / "gecko-obstructed.kicad_pcb"
    _gecko_board(obstructed_board, neighbor=True)
    obstructed_report = connplace.check_connector_placement(
        obstructed_board,
        {"J1": spec_path},
    )
    assert "connector_mating_clearance" in {finding.code for finding in obstructed_report.findings}


HUMAN_CONNECTOR_IDS = {
    "harwin-m80-5l21605m7-03-314",
    "jst-ph-b4b-ph-k-s",
    "gct-usb4105-gf-a",
    "gct-ffc3b11-10-t",
    "we-wr-sma-60312002114503",
    "we-wr-sma-60312202114514",
}


def test_human_request_connectors_allow_absent_artifacts_but_score_guesses() -> None:
    for entry_id in HUMAN_CONNECTOR_IDS:
        truth = _truth(entry_id)
        assert truth.expected_outcome == "human_request"
        result = _score(truth, footprint=None)
        assert "corpus_truth_incomplete" not in _finding_codes(result)
        assert not {
            finding.code
            for finding in result.findings
            if finding.code.startswith("corpus_") and finding.code.endswith("_missing")
        }

        wrong_connector = _partspec_for_truth(truth)
        connector = wrong_connector.connector
        assert connector is not None
        wrong_axis = connector.model_copy(update={"mating_axis": "-x"})
        wrong_part = wrong_connector.model_copy(update={"connector": wrong_axis})
        assert "corpus_connector_mismatch" in _finding_codes(
            _score(truth, partspec=wrong_part, footprint=None)
        )


def test_sma_orientation_fields_and_connector_pin_maps_are_independent() -> None:
    straight = _truth("we-wr-sma-60312002114503")
    edge = _truth("we-wr-sma-60312202114514")
    assert straight.connector is not None
    assert straight.connector.mount == "tht"
    assert straight.connector.orientation == "vertical"
    assert straight.connector.mating_axis == "+z"
    assert straight.connector.board_edge_side is None
    assert edge.connector is not None
    assert edge.connector.mount == "smd"
    assert edge.connector.orientation == "edge_mount"
    assert edge.connector.mating_axis == "+y"
    assert edge.connector.board_edge_side == "+y"

    usb = _truth("gct-usb4105-gf-a")
    assert usb.pins["A1"] == usb.pins["B12"] == "GND"
    assert usb.pins["A4"] == usb.pins["B9"] == "VBUS"
    manifest = corpus.load_manifest(CORPUS_ROOT / "corpus.json")
    ffc_entry = next(entry for entry in manifest.entries if entry.id == "gct-ffc3b11-10-t")
    assert ffc_entry.mpn == "FFC3B11-10-T"


def test_synthetic_connector_replay_is_labeled_and_ids_do_not_collide() -> None:
    manifest = corpus.load_manifest(CORPUS_ROOT / "corpus.json")
    corpus_ids = {entry.id for entry in manifest.entries}
    transcript = REPO_ROOT / "tests" / "data" / "e2e_replay" / "connector-download-blocked.jsonl"
    records = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
    scenario = next(record for record in records if record.get("type") == "scenario")
    assert scenario["label"] == "synthetic"
    assert scenario["scenario"] not in corpus_ids

    for kind in ("gecko", "kona_mirrored", "jst_ph_right_angle", "sma_edge"):
        fixture_spec = connector_spec(kind)
        assert fixture_spec.manufacturer == "Synthetic"
        assert fixture_spec.mpn.startswith("SYNTH-")
        assert fixture_spec.mpn.casefold() not in {entry_id.casefold() for entry_id in corpus_ids}


def test_tps7a02_rotated_ep_representation_has_expected_area() -> None:
    truth = _truth("tps7a02-dqn")
    ep = next(pad for pad in truth.expected_pads if pad.number == "5")
    assert ep.polygon is not None
    truth_area = (
        abs(
            sum(
                first[0] * second[1] - second[0] * first[1]
                for first, second in zip(ep.polygon, ep.polygon[1:] + ep.polygon[:1], strict=True)
            )
        )
        / 2
    )
    assert math.isclose(truth_area, 0.48**2, rel_tol=0.02)
