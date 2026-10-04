import hashlib
from pathlib import Path

import pytest

from circuit import humanrequest, occt
from circuit import libverify as libverify_module
from circuit.landpattern import LandPatternResult, compute_land_pattern
from circuit.libitems import parse_footprint
from circuit.libwriter import write_footprint
from circuit.model3d import generate_model
from circuit.partspec import PartSpec, load_part_spec, part_spec_sha256
from connector_fixtures import connector_spec

_CONNECTOR_CASES = (
    "header_2x5",
    "jst_ph_tht",
    "jst_ph_right_angle",
    "usb_c",
    "fpc_10",
    "sma_edge",
    "gecko",
    "kona_mirrored",
    "datamate",
)


def _write_spec(tmp_path: Path, kind: str) -> tuple[Path, PartSpec]:
    path = tmp_path / f"{kind}.part.spec.json"
    path.write_text(connector_spec(kind).model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path, load_part_spec(path)


def _with_resolved_plating(pattern: LandPatternResult) -> LandPatternResult:
    return pattern.model_copy(
        update={
            "pads": [
                pad.model_copy(update={"pad_type": "np_thru_hole"})
                if pad.pad_type == "unknown"
                else pad
                for pad in pattern.pads
            ]
        }
    )


@pytest.mark.parametrize("kind", _CONNECTOR_CASES)
def test_synthetic_connector_families_use_documented_contact_geometry(
    tmp_path: Path,
    kind: str,
) -> None:
    _path, spec = _write_spec(tmp_path, kind)

    pattern = compute_land_pattern(spec)

    assert pattern.family == "connector"
    assert pattern.source == "datasheet"
    assert len(pattern.pads) >= spec.package.pin_count
    assert spec.connector is not None
    expected_mount = (
        "tht"
        if kind in {"header_2x5", "jst_ph_tht", "gecko", "kona_mirrored"}
        else "mixed"
        if kind == "usb_c"
        else "smd"
    )
    assert spec.connector.mount == expected_mount
    if kind == "gecko":
        assert any(pad.pad_type == "unknown" for pad in pattern.pads)
    if kind == "jst_ph_tht":
        assert any(pad.pad_type == "np_thru_hole" for pad in pattern.pads)
    if kind == "usb_c":
        by_number = {pad.number: pad for pad in pattern.pads}
        assert by_number["1"].y != by_number["2"].y
        assert {"SH1", "SH2"}.issubset(by_number)
    if kind == "fpc_10":
        assert {pad.kind for pad in pattern.pads} >= {"signal", "retention"}
    if kind == "kona_mirrored":
        by_number = {pad.number: pad for pad in pattern.pads}
        assert by_number["1"].x > by_number["4"].x
    if kind == "sma_edge":
        assert len([pad for pad in pattern.pads if pad.kind == "shield"]) == 4
        assert spec.connector.copper_keepout is not None


def test_connector_footprint_writer_records_board_edge_and_pad_geometry(
    tmp_path: Path,
) -> None:
    spec_path, spec = _write_spec(tmp_path, "jst_ph_right_angle")
    output = tmp_path / "SYNTH-JST_PH_RIGHT_ANGLE.kicad_mod"

    write_footprint(
        spec,
        compute_land_pattern(spec),
        output,
        spec_sha256=part_spec_sha256(spec_path),
    )
    footprint = parse_footprint(output)

    assert footprint.properties["circuit_board_edge"] == "+x 0"
    assert any(graphic.layer == "Dwgs.User" for graphic in footprint.graphics)
    assert all(pad.type == "smd" for pad in footprint.pads if pad.number)


def test_connector_writer_preserves_npth_locator_and_shell_tabs(tmp_path: Path) -> None:
    for kind, expected_number in (("jst_ph_tht", ""), ("usb_c", "SH1")):
        spec_path, spec = _write_spec(tmp_path, kind)
        output = tmp_path / f"{kind}.kicad_mod"
        write_footprint(
            spec,
            compute_land_pattern(spec),
            output,
            spec_sha256=part_spec_sha256(spec_path),
        )

        footprint = parse_footprint(output)

        if kind == "jst_ph_tht":
            assert any(
                pad.type == "np_thru_hole" and pad.number == expected_number
                for pad in footprint.pads
            )
        else:
            assert any(
                pad.number == expected_number and pad.type == "thru_hole" for pad in footprint.pads
            )


def test_unknown_connector_plating_fails_closed_for_model_generation(tmp_path: Path) -> None:
    spec_path, spec = _write_spec(tmp_path, "gecko")
    pattern = compute_land_pattern(spec)
    footprint_path = tmp_path / "gecko.kicad_mod"
    with pytest.raises(ValueError, match="connector_plating_unresolved"):
        write_footprint(
            spec,
            pattern,
            footprint_path,
            spec_sha256=part_spec_sha256(spec_path),
        )
    write_footprint(
        spec,
        _with_resolved_plating(pattern),
        footprint_path,
        spec_sha256=part_spec_sha256(spec_path),
    )

    with pytest.raises(ValueError, match="connector_plating_unresolved"):
        generate_model(spec, footprint_path, tmp_path / "models")


def test_unknown_connector_plating_is_reported_with_human_request(tmp_path: Path) -> None:
    spec_path, spec = _write_spec(tmp_path, "gecko")
    pattern = compute_land_pattern(spec)
    footprint_path = tmp_path / "gecko.kicad_mod"
    write_footprint(
        spec,
        _with_resolved_plating(pattern),
        footprint_path,
        spec_sha256=part_spec_sha256(spec_path),
    )
    findings: list[libverify_module.VerifyFinding] = []
    requests: list[humanrequest.HumanRequest] = []

    libverify_module._check_connector(  # pyright: ignore[reportPrivateUsage]
        spec,
        parse_footprint(footprint_path),
        pattern,
        findings,
        requests,
    )

    assert "connector_plating_unresolved" in {finding.code for finding in findings}
    assert requests


def test_tht_connector_model_extends_pins_below_board(tmp_path: Path) -> None:
    spec_path, spec = _write_spec(tmp_path, "header_2x5")
    footprint_path = tmp_path / "header.kicad_mod"
    write_footprint(
        spec,
        compute_land_pattern(spec),
        footprint_path,
        spec_sha256=part_spec_sha256(spec_path),
    )

    generated = generate_model(spec, footprint_path, tmp_path / "models")
    facts = occt.inspect(occt.read_step(generated.step_path))
    minimum_z = min(solid.bbox.z_min for solid in facts.solids)

    assert minimum_z == pytest.approx(-3.0, abs=0.01)
    assert generated.marker == "mating_face:+z"
    assert hashlib.sha256(footprint_path.read_bytes()).hexdigest() == generated.footprint_sha256


def test_right_angle_model_marks_horizontal_mating_face(tmp_path: Path) -> None:
    spec_path, spec = _write_spec(tmp_path, "jst_ph_right_angle")
    footprint_path = tmp_path / "right-angle.kicad_mod"
    write_footprint(
        spec,
        compute_land_pattern(spec),
        footprint_path,
        spec_sha256=part_spec_sha256(spec_path),
    )

    generated = generate_model(spec, footprint_path, tmp_path / "models")

    assert generated.marker == "mating_face:+x"
