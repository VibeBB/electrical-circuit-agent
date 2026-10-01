from __future__ import annotations

from pathlib import Path

import pytest

from circuit import kicad_cli, occt
from circuit.libitems import parse_footprint
from circuit.modeloracle import verify_model_export
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
from circuit.ruleprofile import load_rules


def _reading(text: str) -> Reading:
    return Reading(page=1, bbox=(0, 0, 1, 1), vision=text, vision_record="fixture")


def _spec() -> PartSpec:
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
        package=PackageSpec(
            family="custom",
            drawing_id="Fixture",
            pin_count=2,
            body_length=Dimension(nom=3, reading=_reading("3")),
            body_width=Dimension(nom=2, reading=_reading("2")),
            height=Dimension(nom=1, reading=_reading("1")),
            drawing_view="top",
            pin1_corner="top_left",
            pin1_reading=_reading("pin 1 at top left"),
        ),
        pins=[
            PinSpec(number="1", name="VIN", electrical_type="input", reading=_reading("1 VIN")),
            PinSpec(number="2", name="GND", electrical_type="power_in", reading=_reading("2 GND")),
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
        orderable=[
            OrderableVariant(
                mpn="FIXTURE",
                package_designator="Fixture",
                pin_count=2,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading("FIXTURE Fixture"),
            )
        ],
    )


def _model_shape() -> occt.Shape:
    return occt.compound(
        [
            occt.box(-1.5, -1, 0.2, 3, 2, 0.8),
            occt.box(-1.3, -0.4, 0, 0.6, 0.8, 0.2),
            occt.box(0.7, -0.4, 0, 0.6, 0.8, 0.2),
        ]
    )


def _footprint(model_path: Path) -> str:
    return f"""(footprint "FixtureFootprint" (layer "F.Cu")
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
    (layers "F.Cu" "F.Paste" "F.Mask"))
  (model "{model_path}"
    (offset (xyz 0 0 0))
    (scale (xyz 1 1 1))
    (rotate (xyz 0 0 0))))
"""


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    model_path = tmp_path / "Fixture.step"
    occt.write_step(_model_shape(), model_path, product_name="Fixture")
    footprint_path = tmp_path / "Fixture.kicad_mod"
    footprint_path.write_text(_footprint(model_path), encoding="utf-8")
    assert parse_footprint(footprint_path).models
    return footprint_path, model_path


def test_export_oracle_checks_volume_terminals_and_both_rotations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    footprint_path, model_path = _inputs(tmp_path)
    rotations = iter((0.0, 90.0))

    def run(args: list[str], **_kwargs: object) -> kicad_cli.CompletedRun:
        output = Path(args[args.index("--output") + 1])
        rotation = next(rotations)
        exported = occt.transform(
            occt.read_step(model_path),
            translation=(0.0, 0.0, 1.595),
            rotation_z_deg=rotation,
        )
        occt.write_step(exported, output, product_name="KiCad export")
        return kicad_cli.CompletedRun(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kicad_cli, "run", run)
    report = verify_model_export(
        _spec(),
        footprint_path,
        model_reference=str(model_path),
        model_path=model_path,
        rules=load_rules("builtin:ipc7351b", tmp_path),
        out_dir=tmp_path / "oracle",
    )

    assert report.verdict == "pass"
    assert [run.rotation_deg for run in report.runs] == [0.0, 90.0]
    assert all(run.passed and run.expected_terminal_count == 2 for run in report.runs)


def test_export_oracle_reports_unavailable_kicad_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    footprint_path, model_path = _inputs(tmp_path)

    def unavailable(*_args: object, **_kwargs: object) -> kicad_cli.CompletedRun:
        raise kicad_cli.KicadCliError("kicad-cli failed: executable not found")

    monkeypatch.setattr(kicad_cli, "run", unavailable)
    report = verify_model_export(
        _spec(),
        footprint_path,
        model_reference=str(model_path),
        model_path=model_path,
        rules=load_rules("builtin:ipc7351b", tmp_path),
        out_dir=tmp_path / "oracle",
    )

    assert report.verdict == "fail"
    assert {finding.code for finding in report.findings} == {"model_export_unavailable"}


def test_export_oracle_reports_missing_component_solids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    footprint_path, model_path = _inputs(tmp_path)

    def empty_export(args: list[str], **_kwargs: object) -> kicad_cli.CompletedRun:
        return kicad_cli.CompletedRun(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kicad_cli, "run", empty_export)
    report = verify_model_export(
        _spec(),
        footprint_path,
        model_reference=str(model_path),
        model_path=model_path,
        rules=load_rules("builtin:ipc7351b", tmp_path),
        out_dir=tmp_path / "oracle",
    )

    assert report.verdict == "fail"
    assert all(finding.code == "model_export_missing" for finding in report.findings)


def test_export_oracle_rejects_a_volume_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    footprint_path, model_path = _inputs(tmp_path)
    rotations = iter((0.0, 90.0))

    def run(args: list[str], **_kwargs: object) -> kicad_cli.CompletedRun:
        output = Path(args[args.index("--output") + 1])
        rotation = next(rotations)
        exported = occt.transform(
            occt.read_step(model_path),
            translation=(0.0, 0.0, 1.595),
            rotation_z_deg=rotation,
            scale=1.01,
        )
        occt.write_step(exported, output, product_name="Mismatched export")
        return kicad_cli.CompletedRun(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kicad_cli, "run", run)
    report = verify_model_export(
        _spec(),
        footprint_path,
        model_reference=str(model_path),
        model_path=model_path,
        rules=load_rules("builtin:ipc7351b", tmp_path),
        out_dir=tmp_path / "oracle",
    )

    assert report.verdict == "fail"
    assert all(finding.code == "model_export_mismatch" for finding in report.findings)
