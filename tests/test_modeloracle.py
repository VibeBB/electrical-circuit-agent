from __future__ import annotations

from pathlib import Path

import pytest

from circuit import kicad_cli, occt
from circuit.libitems import parse_footprint
from circuit.model3d import expected_terminals, generate_model
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
            family="chip",
            drawing_id="Chip-2",
            pin_count=2,
            body_length=Dimension(nom=3, reading=_reading("3")),
            body_width=Dimension(nom=2, reading=_reading("2")),
            height=Dimension(nom=1, reading=_reading("1")),
            lead_length=Dimension(nom=0.6, reading=_reading("0.6")),
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


def _no_lead_spec() -> PartSpec:
    spec = _spec()
    package = spec.package.model_copy(
        update={
            "family": "no_lead_dual",
            "pitch": Dimension(nom=0.5, reading=_reading("0.5")),
            "body_length": Dimension(nom=3, reading=_reading("3")),
            "body_width": Dimension(nom=2, reading=_reading("2")),
            "lead_length": Dimension(nom=0.3, reading=_reading("0.3")),
            "lead_width": Dimension(nom=0.25, reading=_reading("0.25")),
            "standoff": Dimension(nom=0.1, reading=_reading("0.1")),
        }
    )
    return spec.model_copy(update={"package": package})


def _model_shape() -> occt.Shape:
    return occt.compound(
        [
            occt.box(-1.5, -1, 0.2, 3, 2, 0.8),
            occt.box(-2.1, -1, 0, 0.6, 2, 1),
            occt.box(1.5, -1, 0, 0.6, 2, 1),
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
  (pad "1" smd rect (at -1.8 0) (size 0.6 2)
    (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd rect (at 1.8 0) (size 0.6 2)
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
    assert all(
        run.passed
        and run.expected_terminal_count == len(expected_terminals(_spec()))
        and run.expected_pin1 is None
        for run in report.runs
    )


def test_export_oracle_checks_part_spec_pin1_corner_at_both_rotations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    footprint_path, _source_model_path = _inputs(tmp_path)
    spec = _no_lead_spec()
    generated = generate_model(spec, footprint_path, tmp_path / "generated")
    rotations = iter((0.0, 90.0))

    def run(args: list[str], **_kwargs: object) -> kicad_cli.CompletedRun:
        output = Path(args[args.index("--output") + 1])
        rotation = next(rotations)
        exported = occt.transform(
            occt.read_step(generated.step_path),
            translation=(0.0, 0.0, 1.595),
            rotation_z_deg=rotation,
        )
        occt.write_step(exported, output, product_name="KiCad export")
        return kicad_cli.CompletedRun(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kicad_cli, "run", run)
    report = verify_model_export(
        spec,
        footprint_path,
        model_reference=str(generated.step_path),
        model_path=generated.step_path,
        rules=load_rules("builtin:ipc7351b", tmp_path),
        out_dir=tmp_path / "oracle",
    )

    assert report.verdict == "pass"
    assert [run.expected_pin1 for run in report.runs] == ["top_left", "top_right"]
    assert [run.exported_pin1 for run in report.runs] == ["top_left", "top_right"]


def test_export_oracle_reports_missing_transformed_pin1_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    footprint_path, _source_model_path = _inputs(tmp_path)
    spec = _no_lead_spec()
    shape = occt.compound(
        (
            occt.box(-1, -1.5, 0.1, 2, 3, 0.9),
            *(
                occt.box(
                    terminal.center_xy[0] - terminal.size_xy[0] / 2,
                    terminal.center_xy[1] - terminal.size_xy[1] / 2,
                    0,
                    terminal.size_xy[0],
                    terminal.size_xy[1],
                    0.2,
                )
                for terminal in expected_terminals(spec)
            ),
        )
    )
    model_path = tmp_path / "without-pin1.step"
    occt.write_step(shape, model_path, product_name="Model without marker")
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
        spec,
        footprint_path,
        model_reference=str(model_path),
        model_path=model_path,
        rules=load_rules("builtin:ipc7351b", tmp_path),
        out_dir=tmp_path / "oracle",
    )

    assert report.verdict == "fail"
    assert "model_export_pin1" in {finding.code for finding in report.findings}
    assert all(run.expected_pin1 is not None and run.exported_pin1 is None for run in report.runs)


def test_export_oracle_uses_model_terminals_not_copper_pad_centers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    footprint_path, model_path = _inputs(tmp_path)
    footprint_path.write_text(
        footprint_path.read_text(encoding="utf-8")
        .replace("(at -1.8 0)", "(at -1.9 0)")
        .replace("(at 1.8 0)", "(at 1.9 0)"),
        encoding="utf-8",
    )
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
    assert all(run.passed for run in report.runs)


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
