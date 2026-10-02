from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from circuit import occt
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

pytestmark = pytest.mark.docker

_DRIVER = """
import json
import sys
from pathlib import Path

from circuit.klc import run_klc
from circuit.libtestboard import build_test_board
from circuit.partspec import PartSpec
from circuit.ruleprofile import load_rules
from circuit.modeloracle import verify_model_export

spec_path, symbol_path, footprint_path, out_dir = map(Path, sys.argv[1:])
spec = PartSpec.model_validate_json(spec_path.read_text(encoding="utf-8"))
rules = load_rules("builtin:ipc7351b", Path("."))
reports = {
    "klc_footprint": run_klc("footprint", footprint_path).model_dump(mode="json"),
    "klc_symbol": run_klc("symbol", symbol_path).model_dump(mode="json"),
    "test_board": build_test_board(
        spec,
        symbol_path,
        "Fixture",
        footprint_path,
        rules,
        out_dir,
    ).model_dump(mode="json"),
}
reports["model_export"] = verify_model_export(
    spec,
    footprint_path,
    model_reference=str(out_dir.parent / "Fixture.step"),
    model_path=out_dir.parent / "Fixture.step",
    rules=rules,
    out_dir=out_dir / "model-export",
).model_dump(mode="json")
reports["model_export_missing"] = verify_model_export(
    spec,
    footprint_path,
    model_reference=str(out_dir.parent / "missing.step"),
    model_path=None,
    rules=rules,
    out_dir=out_dir / "model-export-missing",
).model_dump(mode="json")
print(json.dumps(reports, ensure_ascii=False))
"""


def _reading(text: str) -> Reading:
    return Reading(
        page=1,
        bbox=(1.0, 1.0, 2.0, 2.0),
        vision=text,
        vision_record="fixture",
    )


def _spec() -> PartSpec:
    package = PackageSpec(
        family="custom",
        drawing_id="FixturePackage",
        pin_count=2,
        body_length=Dimension(nom=4.0, reading=_reading("4")),
        body_width=Dimension(nom=3.0, reading=_reading("3")),
        height=Dimension(nom=1.0, reading=_reading("1")),
        drawing_view="top",
        pin1_corner="top_left",
        pin1_reading=_reading("pin 1 at top left"),
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
        pins=[
            PinSpec(number="1", name="VIN", electrical_type="input", reading=_reading("1 VIN")),
            PinSpec(number="2", name="GND", electrical_type="power_in", reading=_reading("2 GND")),
        ],
        pin_table=PinTable(page=1, table=0, number_col=0, name_col=1),
        orderable=[
            OrderableVariant(
                mpn="FIXTURE",
                package_designator="FixturePackage",
                pin_count=2,
                row=CellRef(table=0, row=1, col=0),
                reading=_reading("FIXTURE FixturePackage"),
            )
        ],
    )


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


def _footprint(model_path: str) -> str:
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


def test_klc_and_test_board_in_tools_image(tmp_path: Path) -> None:
    image = os.environ.get("CIRCUIT_TOOLS_IMAGE")
    if not image:
        pytest.skip("CIRCUIT_TOOLS_IMAGE is not set")

    repo = Path(__file__).parents[2].resolve()
    workdir = tmp_path / "library-smoke"
    workdir.mkdir()
    workdir.chmod(0o777)
    spec_path = workdir / "part.spec.json"
    spec_path.write_text(_spec().model_dump_json(), encoding="utf-8")
    symbol_path = workdir / "Fixture.kicad_sym"
    symbol_path.write_text(_symbol_library(), encoding="utf-8")
    footprint_path = workdir / "FixtureFootprint.kicad_mod"
    out_dir = workdir / "output"
    model_path = workdir / "Fixture.step"
    shape = occt.compound(
        [
            occt.box(-1.5, -1.0, 0.2, 3.0, 2.0, 0.8),
            occt.box(-1.3, -0.4, 0.0, 0.6, 0.8, 0.2),
            occt.box(0.7, -0.4, 0.0, 0.6, 0.8, 0.2),
        ]
    )
    occt.write_step(shape, model_path, product_name="Fixture")
    footprint_path.write_text(_footprint(str(model_path)), encoding="utf-8")
    container_name = f"circuit-library-smoke-{uuid.uuid4().hex}"
    command = [
        "docker",
        "run",
        "--name",
        container_name,
        "--user",
        "circuit",
        "-e",
        f"PYTHONPATH={repo / 'src'}",
        "-v",
        f"{repo}:{repo}",
        "-v",
        f"{workdir}:{workdir}",
        "-w",
        str(repo),
        image,
        "python3",
        "-c",
        _DRIVER,
        str(spec_path),
        str(symbol_path),
        str(footprint_path),
        str(out_dir),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr + result.stdout
    reports = json.loads(result.stdout)
    for key in ("klc_footprint", "klc_symbol"):
        violations = reports[key]["violations"]
        assert reports[key]["commit"] == "90b0af91eaffcd91552027c3bfd166896f78c7de"
        assert all(item["rule"] not in {"klc_unavailable", "klc_failed"} for item in violations)

    test_board: dict[str, Any] = reports["test_board"]
    assert test_board["verdict"] == "pass", json.dumps(test_board, indent=2)
    assert all(check["passed"] for check in test_board["checks"])
    for key in (
        "netlist_path",
        "ipcd356_path",
        "position_path",
        "drc_report_path",
        "erc_report_path",
    ):
        path = test_board[key]
        if path is not None:
            assert Path(path).is_file()
    model_export: dict[str, Any] = reports["model_export"]
    assert model_export["verdict"] == "pass", json.dumps(model_export, indent=2)
    assert [run["rotation_deg"] for run in model_export["runs"]] == [0.0, 90.0]
    assert all(run["passed"] for run in model_export["runs"])
    model_export_missing: dict[str, Any] = reports["model_export_missing"]
    assert model_export_missing["verdict"] == "fail"
    assert any(item["code"] == "model_export_missing" for item in model_export_missing["findings"])
