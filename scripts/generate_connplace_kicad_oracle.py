from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

_OUTPUT_DIR = Path("tests/fixtures/kicad_connplace_oracle")
_PLACEMENTS = (
    ("J1", 20, 20, 0, "F.Cu", "F.CrtYd"),
    ("J2", 40, 20, 90, "F.Cu", "F.CrtYd"),
    ("J3", 60, 20, 180, "F.Cu", "F.CrtYd"),
    ("J4", 80, 20, 270, "F.Cu", "F.CrtYd"),
    ("J5", 100, 20, 0, "B.Cu", "B.CrtYd"),
    ("J6", 120, 20, 90, "B.Cu", "B.CrtYd"),
)
_MARKER_START = (1.25, -0.75)
_MARKER_END = (3.5, 1.0)
_AXIS_END = (4.0, 0.0)
_COURTYARD_START = (-2.0, -1.0)
_COURTYARD_END = (3.0, 2.5)


def _number(value: float) -> str:
    return f"{value:g}"


def _uuid(ref: str, item: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"connplace-oracle:{ref}:{item}"))


def _graphic_items(courtyard_layer: str, ref: str) -> str:
    x0, y0 = _COURTYARD_START
    x1, y1 = _COURTYARD_END
    marker_start = " ".join(_number(value) for value in _MARKER_START)
    marker_end = " ".join(_number(value) for value in _MARKER_END)
    axis_end = " ".join(_number(value) for value in _AXIS_END)
    return "\n".join(
        (
            f"(fp_line (start {marker_start}) (end {marker_end}) "
            f'(stroke (width 0.05) (type default)) (layer "Dwgs.User") '
            f'(uuid "{_uuid(ref, "marker")}"))',
            f"(fp_line (start 0 0) (end {axis_end}) "
            f'(stroke (width 0.05) (type default)) (layer "Dwgs.User") '
            f'(uuid "{_uuid(ref, "axis")}"))',
            f"(fp_rect (start {_number(x0)} {_number(y0)}) "
            f"(end {_number(x1)} {_number(y1)}) "
            f"(stroke (width 0.05) (type default)) (fill none) "
            f'(layer "{courtyard_layer}") (uuid "{_uuid(ref, "courtyard")}"))',
        )
    )


def _library_footprint() -> str:
    return "\n".join(
        (
            '(footprint "Asymmetric" (version 20241229) (generator "pcbnew")',
            '  (layer "F.Cu")',
            '  (property "Reference" "REF**" (at 0 -3 0) (layer "F.SilkS") (hide yes))',
            '  (property "Value" "Asymmetric" (at 0 3 0) (layer "F.Fab") (hide yes))',
            f"  {_graphic_items('F.CrtYd', 'LIB')}",
            ")",
            "",
        )
    )


def _board_footprint(
    ref: str,
    x: float,
    y: float,
    rotation: float,
    side: str,
    courtyard_layer: str,
) -> str:
    return "\n".join(
        (
            '  (footprint "ConnplaceOracle:Asymmetric"',
            f'    (layer "{side}")',
            f'    (uuid "{_uuid(ref, "footprint")}")',
            f"    (at {_number(x)} {_number(y)} {_number(rotation)})",
            f'    (property "Reference" "{ref}" '
            f'(at {_number(x)} {_number(y - 3)} 0) (layer "F.SilkS") '
            f'(uuid "{_uuid(ref, "reference")}"))',
            f'    (property "Value" "Asymmetric" '
            f'(at {_number(x)} {_number(y + 3)} 0) (layer "F.Fab") '
            f'(uuid "{_uuid(ref, "value")}"))',
            f'    (property "Datasheet" "" (at 0 0 0) (layer "F.Fab") '
            f'(hide yes) (uuid "{_uuid(ref, "datasheet")}"))',
            f'    (property "Description" "" (at 0 0 0) (layer "F.Fab") '
            f'(hide yes) (uuid "{_uuid(ref, "description")}"))',
            f"    {_graphic_items(courtyard_layer, ref)}",
            "  )",
        )
    )


def _board() -> str:
    layers = "\n".join(
        (
            '    (0 "F.Cu" signal)',
            '    (31 "B.Cu" signal)',
            '    (32 "B.Adhes" user "b.adhesive")',
            '    (33 "F.Adhes" user "f.adhesive")',
            '    (34 "B.Paste" user)',
            '    (35 "F.Paste" user)',
            '    (36 "B.SilkS" user "b.silkscreen")',
            '    (37 "F.SilkS" user "f.silkscreen")',
            '    (38 "B.Mask" user)',
            '    (39 "F.Mask" user)',
            '    (40 "Dwgs.User" user "user.drawings")',
            '    (41 "Cmts.User" user "user.comments")',
            '    (42 "Eco1.User" user "user.1")',
            '    (43 "Eco2.User" user "user.2")',
            '    (44 "Edge.Cuts" user)',
            '    (45 "Margin" user)',
            '    (46 "B.CrtYd" user "b.courtyard")',
            '    (47 "F.CrtYd" user "f.courtyard")',
            '    (48 "B.Fab" user)',
            '    (49 "F.Fab" user)',
            '    (50 "User.1" user)',
            '    (51 "User.2" user)',
            '    (52 "User.3" user)',
            '    (53 "User.4" user)',
            '    (54 "User.5" user)',
            '    (55 "User.6" user)',
            '    (56 "User.7" user)',
            '    (57 "User.8" user)',
            '    (58 "User.9" user)',
        )
    )
    footprints = "\n".join(
        _board_footprint(ref, x, y, rotation, side, courtyard_layer)
        for ref, x, y, rotation, side, courtyard_layer in _PLACEMENTS
    )
    return "\n".join(
        (
            '(kicad_pcb (version 20241229) (generator "pcbnew")',
            "  (general (thickness 1.6))",
            '  (paper "A4")',
            "  (layers",
            layers,
            "  )",
            "  (setup (pad_to_mask_clearance 0))",
            footprints,
            ")",
            "",
        )
    )


def regenerate(output_dir: Path = _OUTPUT_DIR) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    footprint_path = output_dir / "Asymmetric.kicad_mod"
    board_path = output_dir / "rotation_oracle.kicad_pcb"
    dxf_path = output_dir / "rotation_oracle.dxf"
    with tempfile.TemporaryDirectory(prefix="connplace-kicad-oracle-") as temp_dir:
        work_dir = Path(temp_dir)
        work_footprint = work_dir / footprint_path.name
        work_board = work_dir / board_path.name
        work_dxf = work_dir / dxf_path.name
        work_footprint.write_text(_library_footprint(), encoding="utf-8")
        work_board.write_text(_board(), encoding="utf-8")
        subprocess.run(
            ["kicad-cli", "pcb", "upgrade", "--force", str(work_board)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "kicad-cli",
                "pcb",
                "export",
                "dxf",
                "--mode-single",
                "--output",
                str(work_dxf),
                "--output-units",
                "mm",
                "--layers",
                "Dwgs.User,F.CrtYd,B.CrtYd",
                str(work_board),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        shutil.copyfile(work_footprint, footprint_path)
        shutil.copyfile(work_board, board_path)
        shutil.copyfile(work_dxf, dxf_path)
    board_sha256 = hashlib.sha256(board_path.read_bytes()).hexdigest()
    footprint_sha256 = hashlib.sha256(footprint_path.read_bytes()).hexdigest()
    dxf_sha256 = hashlib.sha256(dxf_path.read_bytes()).hexdigest()
    metadata = {
        "artifact_kind": "circuit_connplace_kicad_oracle",
        "board_path": "tests/fixtures/kicad_connplace_oracle/rotation_oracle.kicad_pcb",
        "board_sha256": board_sha256,
        "board_saved_with": "kicad-cli pcb upgrade --force",
        "bottom_storage_note": (
            "The B.Cu placements are direct placements, not GUI flips. In this KiCad-saved "
            "fixture, their stored local children match the on-disk library footprint."
        ),
        "dxf_path": "tests/fixtures/kicad_connplace_oracle/rotation_oracle.dxf",
        "dxf_sha256": dxf_sha256,
        "footprint_path": "tests/fixtures/kicad_connplace_oracle/Asymmetric.kicad_mod",
        "footprint_sha256": footprint_sha256,
        "kicad_cli_version": subprocess.run(
            ["kicad-cli", "version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
    }
    (output_dir / "oracle.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return board_path, footprint_path, dxf_path


if __name__ == "__main__":
    board_path, _, dxf_path = regenerate()
    print(f"KiCad-saved board: {board_path}")
    print(f"KiCad DXF oracle: {dxf_path}")
