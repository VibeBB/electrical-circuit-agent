import math
from pathlib import Path

import pytest

from circuit import kicad_cli
from circuit.libitems import FootprintDef, GraphicDef, PadDef
from circuit.libtestboard import (
    _check_pinmap,  # pyright: ignore[reportPrivateUsage]
    _check_position_file,  # pyright: ignore[reportPrivateUsage]
    _parse_ipcd356,  # pyright: ignore[reportPrivateUsage]
    _paste_findings,  # pyright: ignore[reportPrivateUsage]
    build_test_board,
)
from circuit.partspec import (
    CellRef,
    DatasheetRef,
    Dimension,
    ExposedPad,
    OrderableVariant,
    PackageSpec,
    PartSpec,
    PinSpec,
    PinTable,
    Reading,
)
from circuit.ruleprofile import PasteRule, load_rules


def _reading(text: str) -> Reading:
    return Reading(
        page=1,
        bbox=(1.0, 1.0, 2.0, 2.0),
        vision=text,
        vision_record="fixture",
    )


def _dimension(value: float) -> Dimension:
    return Dimension(nom=value, reading=_reading(str(value)))


def _spec(*, exposed_pad: bool = False) -> PartSpec:
    package = PackageSpec(
        family="custom",
        drawing_id="FixturePackage",
        pin_count=3 if exposed_pad else 2,
        body_length=_dimension(4.0),
        body_width=_dimension(3.0),
        height=_dimension(1.0),
        exposed_pad=(
            ExposedPad(number="3", length=_dimension(2.0), width=_dimension(2.0))
            if exposed_pad
            else None
        ),
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=_reading("pin 1 at top left"),
    )
    pins = [
        PinSpec(number="1", name="VIN", electrical_type="input", reading=_reading("1 VIN")),
        PinSpec(number="2", name="GND", electrical_type="power_in", reading=_reading("2 GND")),
    ]
    if exposed_pad:
        pins.append(
            PinSpec(
                number="3",
                name="EP",
                electrical_type="passive",
                reading=_reading("3 EP"),
            )
        )
    return PartSpec(
        artifact_kind="circuit_part_spec",
        mpn="FIXTURE",
        manufacturer="Example",
        datasheet=DatasheetRef(
            path="fixture.pdf",
            sha256="a" * 64,
            revision="A",
            extraction_path="fixture-extraction.json",
        ),
        package=package,
        pins=pins,
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
        orderable=[
            OrderableVariant(
                mpn="FIXTURE",
                package_designator="FixturePackage",
                pin_count=package.pin_count,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading("FIXTURE FixturePackage"),
            )
        ],
    )


def _pinmap_netlist(pin1_function: str) -> str:
    return f"""(export
  (components (comp (ref "U1") (value "FIXTURE") (footprint "test:FixtureFootprint")))
  (nets
    (net (name "VIN") (node (ref "U1") (pin "1") (pinfunction "{pin1_function}")))
    (net (name "GND") (node (ref "U1") (pin "2") (pinfunction "GND_2")))))
"""


def _symbol_library() -> str:
    return """(kicad_symbol_lib
  (version 20241209)
  (generator "kicad_symbol_editor")
  (symbol "Fixture"
    (property "Reference" "U" (at 0 0 0) (effects (font (size 1 1))))
    (property "Value" "Fixture" (at 0 0 0) (effects (font (size 1 1))))
    (symbol "Fixture_0_1"
      (pin input line (at 5 2.54 180) (length 2.54)
        (name "VIN" (effects (font (size 1 1))))
        (number "1" (effects (font (size 1 1)))))
      (pin power_in line (at 5 -2.54 180) (length 2.54)
        (name "GND" (effects (font (size 1 1))))
        (number "2" (effects (font (size 1 1))))))))
"""


def _footprint() -> str:
    return """(footprint "FixtureFootprint" (layer "F.Cu")
  (attr smd)
  (property "Reference" "REF**" (at 0 -2 0) (layer "F.SilkS")
    (effects (font (size 1 1))))
  (property "Value" "FixtureFootprint" (at 0 2 0) (layer "F.Fab")
    (effects (font (size 1 1))))
  (fp_rect (start -2 -1) (end 2 1)
    (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))
  (pad "1" smd rect (at -1 0) (size 0.8 1)
    (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd rect (at 1 0) (size 0.8 1)
    (layers "F.Cu" "F.Paste" "F.Mask")))
"""


def test_ipcd356_parser_uses_signed_coordinates_and_ten_micrometre_units(
    tmp_path: Path,
) -> None:
    output = tmp_path / "board.ipcd356"
    output.write_text(
        "327VIN U1 -1 A01X+003937Y-001969X0354Y0394R000\n",
        encoding="utf-8",
    )

    record = _parse_ipcd356(output)[0]

    assert record[:3] == ("VIN", "1", "U1")
    assert record[3] == pytest.approx(10.0, abs=0.00254)
    assert record[4] == pytest.approx(-5.0, abs=0.00254)


def test_ipcd356_parser_resolves_extended_net_names(tmp_path: Path) -> None:
    output = tmp_path / "board.ipcd356"
    output.write_text(
        "P  NNAMEM0000 Exposed?Thermal?Pad\n327M0000 U1 -17 A01X+000000Y+000000X0661Y0661R000\n",
        encoding="utf-8",
    )

    record = _parse_ipcd356(output)[0]

    assert record[:3] == ("Exposed?Thermal?Pad", "17", "U1")


def test_pinmap_matches_kicad_pinfunction_suffix_and_detects_renames(tmp_path: Path) -> None:
    output = tmp_path / "pinmap.net"
    output.write_text(_pinmap_netlist("VIN_1"), encoding="utf-8")

    assert _check_pinmap(_spec(), output, "test:FixtureFootprint") == (True, "")

    spec = _spec()
    spec.pins[0] = spec.pins[0].model_copy(update={"name": "Exposed Thermal Pad"})
    output.write_text(_pinmap_netlist("Exposed_Thermal_Pad_1"), encoding="utf-8")
    assert _check_pinmap(spec, output, "test:FixtureFootprint") == (True, "")

    output.write_text(_pinmap_netlist("VCC_1"), encoding="utf-8")
    passed, details = _check_pinmap(_spec(), output, "test:FixtureFootprint")
    assert not passed
    assert "pin 1 function 'VCC' does not match 'VIN'" in details


def test_position_export_checks_footprint_inclusion_and_attribute(
    tmp_path: Path,
) -> None:
    path = tmp_path / "board-pos.csv"
    path.write_text(
        "# Ref Val Package PosX PosY Rot Side\nU1 FIXTURE FixtureFootprint 0 0 0 top\n",
        encoding="utf-8",
    )
    pad = PadDef(
        number="1",
        type="smd",
        shape="rect",
        x=0,
        y=0,
        rotation=0,
        width=1,
        height=1,
        drill=None,
        layers=["F.Cu", "F.Paste", "F.Mask"],
    )

    assert _check_position_file(
        path,
        footprint_name="FixtureFootprint",
        attributes=["smd"],
        pads=[pad],
    ) == (True, "position export contains U1 as smd")
    assert not _check_position_file(
        path,
        footprint_name="FixtureFootprint",
        attributes=["smd", "through_hole"],
        pads=[pad],
    )[0]
    passed, detail = _check_position_file(
        path,
        footprint_name="FixtureFootprint",
        attributes=["through_hole"],
        pads=[pad],
    )
    assert not passed
    assert "does not match its smd pad types" in detail
    path.write_text("# Ref Val Package PosX PosY Rot Side\n", encoding="utf-8")
    assert not _check_position_file(
        path,
        footprint_name="FixtureFootprint",
        attributes=["smd"],
        pads=[pad],
    )[0]


def test_paste_coverage_counts_ep_windows_and_warns_for_narrow_mask_web(
    tmp_path: Path,
) -> None:
    spec = _spec(exposed_pad=True)
    rules = load_rules("builtin:ipc7351b", tmp_path / "rules").model_copy(
        update={
            "min_mask_web_mm": 0.2,
            "paste": PasteRule(
                coverage_min=0.5,
                coverage_max=1.0,
                ep_coverage_min=0.3,
                ep_coverage_max=0.4,
            ),
        }
    )
    pads = [
        PadDef(
            number="1",
            type="smd",
            shape="rect",
            x=-2,
            y=0,
            rotation=0,
            width=1,
            height=1,
            drill=None,
            layers=["F.Cu", "F.Paste", "F.Mask"],
            mask_margin=0.1,
        ),
        PadDef(
            number="2",
            type="smd",
            shape="rect",
            x=-0.9,
            y=0,
            rotation=0,
            width=1,
            height=1,
            drill=None,
            layers=["F.Cu", "F.Mask"],
            mask_margin=0.1,
        ),
        PadDef(
            number="3",
            type="smd",
            shape="rect",
            x=1,
            y=3,
            rotation=0,
            width=2,
            height=2,
            drill=None,
            layers=["F.Cu", "F.Mask"],
        ),
    ]
    graphics = [
        GraphicDef(
            layer="F.Paste",
            kind="rect",
            points=[(0.3, 2.3), (1.1, 3.1)],
            width=0,
        ),
        GraphicDef(
            layer="F.Paste",
            kind="rect",
            points=[(0.9, 2.9), (1.7, 3.7)],
            width=0,
        ),
    ]
    footprint = FootprintDef(
        name="FixtureFootprint",
        attributes=["smd"],
        pads=pads,
        graphics=graphics,
        models=[],
        properties={},
    )

    findings = _paste_findings(spec, footprint, rules)

    assert any(item.code == "paste_coverage" and item.subject == "pad 2" for item in findings)
    assert not any(item.code == "ep_paste_coverage" for item in findings)
    assert any(item.code == "mask_web" and item.severity == "warning" for item in findings)
    assert math.isclose(sum(0.8 * 0.8 for _ in graphics) / 4.0, 0.32)


@pytest.mark.parametrize(
    ("drc_violation", "expected_verdict", "expected_drc_code"),
    [
        (
            kicad_cli.Violation(
                type="unconnected_items",
                severity="error",
                description="expected unconnected pads",
            ),
            "pass",
            None,
        ),
        (
            kicad_cli.Violation(
                type="clearance",
                severity="error",
                description="pad clearance violation",
            ),
            "fail",
            "testboard_drc_clearance",
        ),
    ],
)
def test_build_test_board_runs_pinmap_readback_assembly_drc_and_erc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drc_violation: kicad_cli.Violation,
    expected_verdict: str,
    expected_drc_code: str | None,
) -> None:
    spec = _spec()
    symbol_lib = tmp_path / "Fixture.kicad_sym"
    symbol_lib.write_text(_symbol_library(), encoding="utf-8")
    footprint_path = tmp_path / "FixtureFootprint.kicad_mod"
    footprint_path.write_text(_footprint(), encoding="utf-8")
    rules = load_rules("builtin:ipc7351b", tmp_path / "rules")
    observed: dict[str, str] = {}

    monkeypatch.setattr(kicad_cli, "version", lambda: "KiCad 11 fixture")

    def export_netlist(source: Path, output: Path) -> Path:
        schematic = source.read_text(encoding="utf-8")
        observed["schematic"] = schematic
        assert (source.parent / "sym-lib-table").is_file()
        assert (source.parent / "fp-lib-table").is_file()
        output.write_text(
            """(export
  (components (comp (ref "U1") (value "FIXTURE") (footprint "test:FixtureFootprint")))
  (nets
    (net (name "VIN") (node (ref "U1") (pin "1") (pinfunction "VIN")))
    (net (name "GND") (node (ref "U1") (pin "2") (pinfunction "GND")))))
""",
            encoding="utf-8",
        )
        return output

    monkeypatch.setattr(kicad_cli, "export_netlist", export_netlist)

    def export(kind: str, source: Path, out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        board = source.read_text(encoding="utf-8")
        observed["board"] = board
        if kind == "ipcd356":
            output = out_dir / "test-board.ipcd356"
            output.write_text(
                "327VIN U1 -1 A01X-000394Y+000000X0354Y0394R000\n"
                "327GND U1 -2 A01X+000394Y+000000X0354Y0394R000\n",
                encoding="utf-8",
            )
        else:
            output = out_dir / "test-board-pos.csv"
            output.write_text(
                "# Ref Val Package PosX PosY Rot Side\nU1 FIXTURE FixtureFootprint 0 0 0 top\n",
                encoding="utf-8",
            )
        return [output]

    monkeypatch.setattr(kicad_cli, "export", export)

    def drc(source: Path, output: Path) -> kicad_cli.Report:
        observed["clearance"] = source.read_text(encoding="utf-8")
        observed["project"] = source.with_suffix(".kicad_pro").read_text(encoding="utf-8")
        errors = int(drc_violation.severity == "error")
        return kicad_cli.Report(
            kind="drc",
            source=source,
            report_path=output,
            kicad_version="KiCad 11 fixture",
            errors=errors,
            warnings=0,
            exclusions=0,
            unconnected=int(drc_violation.type == "unconnected_items"),
            violations=[drc_violation],
            verdict="fail" if errors else "pass",
        )

    monkeypatch.setattr(kicad_cli, "drc", drc)

    def erc(source: Path, output: Path) -> kicad_cli.Report:
        return kicad_cli.Report(
            kind="erc",
            source=source,
            report_path=output,
            kicad_version="KiCad 11 fixture",
            errors=1,
            warnings=1,
            exclusions=0,
            unconnected=0,
            violations=[
                kicad_cli.Violation(
                    type="pin_error",
                    severity="error",
                    description="ERC errors are review warnings",
                )
            ],
            verdict="fail",
        )

    monkeypatch.setattr(kicad_cli, "erc", erc)

    result = build_test_board(
        spec,
        symbol_lib,
        "Fixture",
        footprint_path,
        rules,
        tmp_path / "output",
    )

    assert result.verdict == expected_verdict
    assert any(item.code == expected_drc_code for item in result.findings) is (
        expected_drc_code is not None
    )
    assert all(item.severity == "warning" for item in result.erc_findings)
    if expected_drc_code is None:
        assert all(check.passed for check in result.checks)
    assert result.ipcd356_path is not None and result.ipcd356_path.is_file()
    assert result.position_path is not None and result.position_path.is_file()
    assert '(label "VIN"' in observed["schematic"]
    assert '(label "GND"' in observed["schematic"]
    assert '"clearance": 0.15' in observed["project"]
    assert '(net 1 "GND")' in observed["board"]
    assert '(net 2 "VIN")' in observed["board"]
    assert "(net_settings " not in observed["board"]
    assert "gr_rect" in observed["board"]


def test_missing_kicad_cli_is_reported_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kicad_cli,
        "version",
        lambda: (_ for _ in ()).throw(kicad_cli.KicadCliError("[Errno 2] No such file")),
    )
    result = build_test_board(
        _spec(),
        tmp_path / "missing.kicad_sym",
        "Fixture",
        tmp_path / "missing.kicad_mod",
        load_rules("builtin:ipc7351b", tmp_path / "rules"),
        tmp_path / "output",
    )

    assert result.verdict == "fail"
    assert result.findings[0].code == "testboard_unavailable"
