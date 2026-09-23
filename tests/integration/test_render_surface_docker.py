"""Render the extended kicad-cli surface inside the pinned circuit-tools image."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.docker

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "smoke-board"

_DRIVER = """
import json
import sys
from pathlib import Path
from circuit import kicad_cli, stackup

work = Path(sys.argv[1])
board = work / "board.kicad_pcb"
sch = work / "board.kicad_sch"

out = {}

for side in ["top", "bottom", "left", "right", "front", "back"]:
    out[f"side_{side}"] = str(kicad_cli.render(board, work / f"render-{side}.png", side=side))

out["isometric"] = str(
    kicad_cli.render(
        board,
        work / "render-isometric.png",
        side="top",
        rotate="-60,0,-45",
        zoom=1.4,
        perspective=True,
        floor=True,
        quality="high",
    )
)
out["schematic"] = [str(p) for p in kicad_cli.render_schematic(sch, work / "sch-png", dpi=150)]
out["layers"] = [
    str(p)
    for p in kicad_cli.render_layers(
        board, work / "layer-png", layers="F.Cu,B.Cu,Edge.Cuts", dpi=150
    )
]
out["fp_svg"] = [
    str(p) for p in kicad_cli.export("fp_svg", work / "fp-lib.pretty", work / "fp-svg")
]

imported = kicad_cli.import_file(
    "sch", work / "test.asc", work / "imported.kicad_sch", format="ltspice"
)
out["import"] = {
    "output": str(imported.output),
    "report": imported.report,
    "report_path": str(imported.report_path),
}

stackup_data = kicad_cli.export_stackup(board, work / "board-stackup.json")
out["stackup_svg"] = str(
    stackup.write_stackup_diagram(stackup_data, work / "board-stackup.svg")
)

(work / "render-surface.json").write_text(json.dumps(out), encoding="utf-8")
"""


def test_extended_render_surface_in_tools_image(tmp_path: Path) -> None:
    image = os.environ.get("CIRCUIT_TOOLS_IMAGE")
    if not image:
        pytest.skip("CIRCUIT_TOOLS_IMAGE is not set")
    repo = Path(__file__).parents[2].resolve()
    workdir = tmp_path / "render-surface"
    workdir.mkdir()
    workdir.chmod(0o777)
    shutil.copy(_FIXTURE / "board.kicad_pcb", workdir / "board.kicad_pcb")
    shutil.copy(_FIXTURE / "board.kicad_sch", workdir / "board.kicad_sch")
    shutil.copy(_FIXTURE / "board.kicad_pro", workdir / "board.kicad_pro")
    lib = workdir / "fp-lib.pretty"
    lib.mkdir()
    lib.chmod(0o777)
    (lib / "test_pad.kicad_mod").write_text(
        '(footprint "test_pad" (version 20241229) (generator "test") (layer "F.Cu")\n'
        '  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")))\n',
        encoding="utf-8",
    )
    (workdir / "test.asc").write_text(
        "Version 4\nSHEET 1 880 680\nWIRE 208 144 208 176\n"
        "SYMBOL res 224 176 R0\nSYMATTR InstName R1\nSYMATTR Value 1k\nFLAG 208 144 0\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
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
            str(workdir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    produced = json.loads((workdir / "render-surface.json").read_text(encoding="utf-8"))
    expected_counts = {
        "schematic": 1,
        "layers": 3,
        "fp_svg": 1,
    }
    for key, count in expected_counts.items():
        assert len(produced[key]) == count, key
    side_paths = [
        Path(produced[f"side_{side}"])
        for side in ("top", "bottom", "left", "right", "front", "back")
    ]
    all_paths = (
        side_paths
        + [Path(produced["isometric"])]
        + [Path(p) for value in expected_counts for p in produced[value]]
    )
    assert all(path.is_file() and path.stat().st_size > 0 for path in all_paths)
    imported = produced["import"]
    assert Path(imported["output"]).is_file()
    assert Path(imported["report_path"]).is_file()
    assert imported["report"]["source_format"] == "LTspice"
    stackup_svg = Path(produced["stackup_svg"])
    assert stackup_svg.read_text(encoding="utf-8").startswith("<svg")
