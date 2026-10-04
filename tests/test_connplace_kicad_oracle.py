from __future__ import annotations

import hashlib
import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from circuit import connplace as connplace_module
from circuit import sexpr
from connector_fixtures import connector_spec, dimension

_ORACLE_DIR = Path(__file__).parent / "fixtures" / "kicad_connplace_oracle"
_BOARD_PATH = _ORACLE_DIR / "rotation_oracle.kicad_pcb"
_FOOTPRINT_PATH = _ORACLE_DIR / "Asymmetric.kicad_mod"
_EXPECTED_BOARD_SHA256 = "4c06afc4242c523635801e23d4642c5be95b9645c639fcc4d059b9f5d6885e37"
_EXPECTED_FOOTPRINT_SHA256 = "c61b869b6e069c78d156263f7cfcaad4d34eba14a199a205d4aad74e5d7e627c"
_EXPECTED_DXF_LINES = (
    ((21.25, -19.25), (23.5, -21.0)),
    ((40.0, -20.0), (40.0, -16.0)),
    ((39.25, -18.75), (41.0, -16.5)),
    ((20.0, -20.0), (24.0, -20.0)),
    ((58.75, -20.75), (56.5, -19.0)),
    ((60.0, -20.0), (56.0, -20.0)),
    ((80.75, -21.25), (79.0, -23.5)),
    ((80.0, -20.0), (80.0, -24.0)),
    ((101.25, -19.25), (103.5, -21.0)),
    ((100.0, -20.0), (104.0, -20.0)),
    ((119.25, -18.75), (121.0, -16.5)),
    ((120.0, -20.0), (120.0, -16.0)),
)


def _kicad_cli_available() -> bool:
    return shutil.which("kicad-cli") is not None or (
        shutil.which("docker") is not None and bool(os.environ.get("CIRCUIT_TOOLS_IMAGE"))
    )


def _kicad_cli_command(tmp_path: Path) -> list[str]:
    if shutil.which("kicad-cli") is not None:
        return ["kicad-cli"]
    image = os.environ["CIRCUIT_TOOLS_IMAGE"]
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "circuit",
        "--volume",
        f"{tmp_path}:{tmp_path}",
        image,
        "kicad-cli",
    ]


def _canonical_line(
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float]]:
    points = sorted(
        (
            (round(start[0], 3), round(start[1], 3)),
            (round(end[0], 3), round(end[1], 3)),
        )
    )
    return (points[0], points[1])


def _dxf_lines(
    path: Path,
) -> list[tuple[str, tuple[float, float], tuple[float, float]]]:
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) % 2 == 0
    pairs = list(zip(raw_lines[::2], raw_lines[1::2], strict=True))
    output: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    entity: str | None = None
    fields: dict[str, str] = {}

    def append_line() -> None:
        if entity != "LINE":
            return
        output.append(
            (
                fields["8"].lower(),
                (float(fields["10"]), float(fields["20"])),
                (float(fields["11"]), float(fields["21"])),
            )
        )

    for code, value in pairs:
        code = code.strip()
        value = value.strip()
        if code == "0":
            append_line()
            entity = value
            fields = {}
        elif entity == "LINE":
            fields[code] = value
    append_line()
    return output


def _footprint_nodes(root: list[sexpr.SExpr]) -> list[list[sexpr.SExpr]]:
    return [node for node in root[1:] if isinstance(node, list) and node and node[0] == "footprint"]


def _graphic_geometry(
    node: list[sexpr.SExpr],
) -> tuple[tuple[str, tuple[float, float], tuple[float, float]], ...]:
    geometry: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    for item in node[1:]:
        if not isinstance(item, list) or not item or item[0] not in {"fp_line", "fp_rect"}:
            continue
        start = next(
            child for child in item[1:] if isinstance(child, list) and child and child[0] == "start"
        )
        end = next(
            child for child in item[1:] if isinstance(child, list) and child and child[0] == "end"
        )
        geometry.append(
            (
                str(item[0]),
                (float(str(start[1])), float(str(start[2]))),
                (float(str(end[1])), float(str(end[2]))),
            )
        )
    return tuple(sorted(geometry))


def test_kicad_saved_bottom_children_match_library_local_geometry() -> None:
    assert hashlib.sha256(_BOARD_PATH.read_bytes()).hexdigest() == _EXPECTED_BOARD_SHA256
    assert hashlib.sha256(_FOOTPRINT_PATH.read_bytes()).hexdigest() == _EXPECTED_FOOTPRINT_SHA256
    metadata = (_ORACLE_DIR / "oracle.json").read_text(encoding="utf-8")
    assert (
        '"board_path": "tests/fixtures/kicad_connplace_oracle/rotation_oracle.kicad_pcb"'
        in metadata
    )
    assert f'"board_sha256": "{_EXPECTED_BOARD_SHA256}"' in metadata
    assert '"board_saved_with": "kicad-cli pcb upgrade --force"' in metadata
    assert (
        '"footprint_path": "tests/fixtures/kicad_connplace_oracle/Asymmetric.kicad_mod"' in metadata
    )
    assert f'"footprint_sha256": "{_EXPECTED_FOOTPRINT_SHA256}"' in metadata
    assert (
        '"bottom_storage_note": "The B.Cu placements are direct placements, not GUI flips.'
        in metadata
    )

    board_root = sexpr.parse_text(_BOARD_PATH.read_text(encoding="utf-8"))
    library_root = sexpr.parse_text(_FOOTPRINT_PATH.read_text(encoding="utf-8"))
    assert isinstance(board_root, list)
    assert isinstance(library_root, list)
    footprints = _footprint_nodes(board_root)
    library_geometry = _graphic_geometry(library_root)
    for node in footprints:
        footprint = connplace_module._parse_board_footprint(  # pyright: ignore[reportPrivateUsage]
            node
        )
        if footprint.side == "B.Cu":
            assert footprint.scale == (1.0, 1.0)
            assert _graphic_geometry(node) == library_geometry


@pytest.mark.skipif(not _kicad_cli_available(), reason="requires KiCad CLI or CIRCUIT_TOOLS_IMAGE")
def test_transform_and_swept_envelope_match_kicad_dxf(tmp_path: Path) -> None:
    assert hashlib.sha256(_BOARD_PATH.read_bytes()).hexdigest() == _EXPECTED_BOARD_SHA256
    board_path = tmp_path / _BOARD_PATH.name
    dxf_path = tmp_path / "rotation_oracle.dxf"
    shutil.copyfile(_BOARD_PATH, board_path)
    subprocess.run(
        [
            *_kicad_cli_command(tmp_path),
            "pcb",
            "export",
            "dxf",
            "--mode-single",
            "--output",
            str(dxf_path),
            "--output-units",
            "mm",
            "--layers",
            "Dwgs.User,F.CrtYd,B.CrtYd",
            str(board_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = _dxf_lines(dxf_path)
    dwgs_lines = [
        _canonical_line(start, end) for layer, start, end in lines if layer == "user.drawings"
    ]
    expected_lines = [_canonical_line(start, end) for start, end in _EXPECTED_DXF_LINES]
    assert sorted(dwgs_lines) == sorted(expected_lines)

    board_root = sexpr.parse_text(board_path.read_text(encoding="utf-8"))
    assert isinstance(board_root, list)
    footprints = _footprint_nodes(board_root)
    parsed = [
        (
            node,
            connplace_module._parse_board_footprint(  # pyright: ignore[reportPrivateUsage]
                node
            ),
        )
        for node in footprints
    ]
    expected_from_transform: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for node, footprint in parsed:
        for item in node[1:]:
            if not isinstance(item, list) or not item or item[0] != "fp_line":
                continue
            layer = next(
                (
                    child[1]
                    for child in item[1:]
                    if isinstance(child, list) and child and child[0] == "layer"
                ),
                None,
            )
            if layer != "Dwgs.User":
                continue
            start_node = next(
                child
                for child in item[1:]
                if isinstance(child, list) and child and child[0] == "start"
            )
            end_node = next(
                child
                for child in item[1:]
                if isinstance(child, list) and child and child[0] == "end"
            )
            start = connplace_module._transform(  # pyright: ignore[reportPrivateUsage]
                (float(str(start_node[1])), float(str(start_node[2]))),
                footprint,
            )
            end = connplace_module._transform(  # pyright: ignore[reportPrivateUsage]
                (float(str(end_node[1])), float(str(end_node[2]))),
                footprint,
            )
            expected_from_transform.append(((start[0], -start[1]), (end[0], -end[1])))
    assert sorted(_canonical_line(*line) for line in expected_from_transform) == sorted(
        expected_lines
    )

    base_spec = connector_spec("jst_ph_right_angle")
    connector = base_spec.connector
    assert connector is not None
    envelope = connector.mating_envelope
    assert envelope is not None
    envelope = envelope.model_copy(
        update={
            "box": (-2.0, -1.0, 3.0, 2.5),
            "travel": dimension(3.5),
            "access_margin_mm": 0.0,
        }
    )
    spec = base_spec.model_copy(
        update={
            "connector": connector.model_copy(
                update={"mating_axis": "+x", "mating_envelope": envelope}
            )
        }
    )
    for _, footprint in parsed:
        courtyard_layer = "b.courtyard" if footprint.side == "B.Cu" else "f.courtyard"
        courtyard_points = [
            (x, -y)
            for layer, start, end in lines
            if layer == courtyard_layer
            for x, y in (start, end)
            if abs(x - footprint.x) < 4.0 and abs(-y - footprint.y) < 4.0
        ]
        unique_courtyard_points = sorted(set(courtyard_points))
        assert len(unique_courtyard_points) == 4
        origin = (footprint.x, -footprint.y)
        axis_line = next(
            (start, end)
            for layer, start, end in lines
            if layer == "user.drawings"
            and math.isclose(math.dist(start, end), 4.0, abs_tol=1e-3)
            and (math.dist(start, origin) < 1e-3 or math.dist(end, origin) < 1e-3)
        )
        far_endpoint = axis_line[1] if math.dist(axis_line[0], origin) < 1e-3 else axis_line[0]
        axis_vector = (far_endpoint[0] - origin[0], -(far_endpoint[1] - origin[1]))
        swept_points = [
            *unique_courtyard_points,
            *(
                (
                    x + axis_vector[0] * 3.5 / 4.0,
                    y + axis_vector[1] * 3.5 / 4.0,
                )
                for x, y in unique_courtyard_points
            ),
        ]
        expected_box = (
            min(point[0] for point in swept_points),
            min(point[1] for point in swept_points),
            max(point[0] for point in swept_points),
            max(point[1] for point in swept_points),
        )
        actual_box = connplace_module._envelope_box(  # pyright: ignore[reportPrivateUsage]
            envelope,
            footprint,
            spec,
        )
        assert actual_box == pytest.approx(expected_box, abs=1e-3)
