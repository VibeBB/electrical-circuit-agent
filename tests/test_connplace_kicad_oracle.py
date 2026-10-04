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
_FLIP_ORACLE_DIR = _ORACLE_DIR / "gui_flipped"
_EXPECTED_BOARD_SHA256 = "4c06afc4242c523635801e23d4642c5be95b9645c639fcc4d059b9f5d6885e37"
_EXPECTED_FOOTPRINT_SHA256 = "c61b869b6e069c78d156263f7cfcaad4d34eba14a199a205d4aad74e5d7e627c"
_EXPECTED_VIDEO_FIXTURE_SHA256 = "84fe6fd348c096afbe701ef99da5f28ac5a54adbeb02764225f5ef573e67d54e"
_EXPECTED_ROYALBLUE_FIXTURE_SHA256 = (
    "3815ac7a70420713a5e9755f5fe210503683ed4b31a44f1b9502c7b094c1ec17"
)
_EXPECTED_SOIC_LIBRARY_SHA256 = "9783bc19f518a72442ddbfe6289e767a42c3d23358b65c3447b6a36bdd0e7dcf"
_EXPECTED_TAG_LIBRARY_SHA256 = "73227c312fafa8c53e6569852a9b971afedca0dcfa559f94353c4175d37d30b3"
_EXPECTED_VIDEO_NODE_SHA256 = "e1a8ec3646e64ed0f2b9c13da0c7f8a5a1255013450d004703f1daee209bc38e"
_EXPECTED_ROYALBLUE_NODE_SHA256 = "1e9a5886a03c3acb2d0dc0bd0b866fcbdc96529cd4a8eb782375ce60ad8415c2"
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
    tmp_path.chmod(0o777)
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


def _children(node: list[sexpr.SExpr], tag: str) -> list[list[sexpr.SExpr]]:
    return [child for child in node[1:] if isinstance(child, list) and child and child[0] == tag]


def _footprint_reference(node: list[sexpr.SExpr]) -> str | None:
    for prop in _children(node, "property"):
        if len(prop) > 2 and str(prop[1]) == "Reference":
            return str(prop[2])
    return None


def _numbered_pad_data(
    node: list[sexpr.SExpr],
) -> dict[str, tuple[float, float, float, float, float]]:
    pads: dict[str, tuple[float, float, float, float, float]] = {}
    for pad in _children(node, "pad"):
        if len(pad) < 2:
            continue
        number = str(pad[1])
        if not number.isdigit():
            continue
        at = _children(pad, "at")
        size = _children(pad, "size")
        assert at and size
        angle = float(str(at[0][3])) if len(at[0]) > 3 else 0.0
        pads[number] = (
            float(str(at[0][1])),
            float(str(at[0][2])),
            angle,
            float(str(size[0][1])),
            float(str(size[0][2])),
        )
    return pads


def _graphic_segments(
    node: list[sexpr.SExpr],
    layer_name: str,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for item in node[1:]:
        if not isinstance(item, list) or not item or item[0] not in {"fp_line", "fp_rect"}:
            continue
        layers = _children(item, "layer")
        if not layers or str(layers[0][1]) != layer_name:
            continue
        start_nodes = _children(item, "start")
        end_nodes = _children(item, "end")
        assert start_nodes and end_nodes
        start = (float(str(start_nodes[0][1])), float(str(start_nodes[0][2])))
        end = (float(str(end_nodes[0][1])), float(str(end_nodes[0][2])))
        if item[0] == "fp_line":
            segments.append((start, end))
        else:
            corners = (
                (start[0], start[1]),
                (start[0], end[1]),
                (end[0], start[1]),
                (end[0], end[1]),
            )
            segments.extend(
                [
                    (corners[0], corners[1]),
                    (corners[1], corners[3]),
                    (corners[3], corners[2]),
                    (corners[2], corners[0]),
                ]
            )
    return segments


def _canonical_dxf_line(
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float]]:
    points = sorted(
        (
            (round(start[0], 4), round(start[1], 4)),
            (round(end[0], 4), round(end[1], 4)),
        )
    )
    return points[0], points[1]


def _dxf_pad_center(
    lines: list[tuple[str, tuple[float, float], tuple[float, float]]],
    target: tuple[float, float],
    half_extent: tuple[float, float],
) -> tuple[float, float]:
    points = [
        point
        for layer, start, end in lines
        if layer == "b.cu"
        for point in (start, end)
        if abs(point[0] - target[0]) <= half_extent[0]
        and abs(point[1] - target[1]) <= half_extent[1]
    ]
    assert points
    return (
        (min(point[0] for point in points) + max(point[0] for point in points)) / 2,
        (min(point[1] for point in points) + max(point[1] for point in points)) / 2,
    )


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
        '"bottom_storage_note": "The rotation_oracle fixture uses direct B.Cu '
        "placements, not GUI flips." in metadata
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
        if footprint.side == "B.Cu":
            continue
        courtyard_layer = "f.courtyard"
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


@pytest.mark.skipif(not _kicad_cli_available(), reason="requires KiCad CLI or CIRCUIT_TOOLS_IMAGE")
def test_real_gui_flipped_demo_transforms_match_kicad_dxf(tmp_path: Path) -> None:
    samples = (
        (
            "video_U3.kicad_pcb",
            "SOIC-20W_7.5x12.8mm_P1.27mm.kicad_mod",
            "U3",
            0.0,
            _EXPECTED_VIDEO_FIXTURE_SHA256,
            _EXPECTED_SOIC_LIBRARY_SHA256,
            _EXPECTED_VIDEO_NODE_SHA256,
            (1.1, 0.5),
        ),
        (
            "royalblue_J8.kicad_pcb",
            "Tag-Connect_TC2030-IDC-NL_2x03_P1.27mm_Vertical.kicad_mod",
            "J8",
            90.0,
            _EXPECTED_ROYALBLUE_FIXTURE_SHA256,
            _EXPECTED_TAG_LIBRARY_SHA256,
            _EXPECTED_ROYALBLUE_NODE_SHA256,
            (0.5, 0.5),
        ),
    )
    metadata = (_ORACLE_DIR / "oracle.json").read_text(encoding="utf-8")
    for provenance in (
        "https://gitlab.com/kicad/code/kicad/-/archive/master/kicad-master.tar.gz?path=demos",
        "a1166fc53abbbf33024e265ebc19cfcb7496a267",
        "9326b9efd0de1a3b286e3adfed68086ed3b278c4",
        "f620675158ddd5c61e5ce9e15f4c92b230ae1df1cbcec6c502b8ce266e4007ea",
        "535dbe212f43a8bab12b23277e176a94ac9694177258af2981c0e7b08e4b7252",
    ):
        assert provenance in metadata

    for (
        board_filename,
        library_filename,
        reference,
        expected_rotation,
        expected_board_hash,
        expected_library_hash,
        expected_node_hash,
        pad_half_extent,
    ) in samples:
        board_source = _FLIP_ORACLE_DIR / board_filename
        library_path = _FLIP_ORACLE_DIR / library_filename
        assert hashlib.sha256(board_source.read_bytes()).hexdigest() == expected_board_hash
        assert hashlib.sha256(library_path.read_bytes()).hexdigest() == expected_library_hash
        board_root = sexpr.parse_text(board_source.read_text(encoding="utf-8"))
        library_root = sexpr.parse_text(library_path.read_text(encoding="utf-8"))
        assert isinstance(board_root, list)
        assert isinstance(library_root, list)
        board_node = next(
            node for node in _footprint_nodes(board_root) if _footprint_reference(node) == reference
        )
        assert hashlib.sha256(sexpr.serialize(board_node).encode("utf-8")).hexdigest() == (
            expected_node_hash
        )
        board_footprint = connplace_module._parse_board_footprint(  # pyright: ignore[reportPrivateUsage]
            board_node
        )
        assert board_footprint.side == "B.Cu"
        assert board_footprint.rotation == expected_rotation
        assert board_footprint.scale == (1.0, 1.0)
        board_pads = _numbered_pad_data(board_node)
        library_pads = _numbered_pad_data(library_root)
        assert board_pads.keys() == library_pads.keys()
        for number, board_pad in board_pads.items():
            library_pad = library_pads[number]
            assert board_pad[0] == pytest.approx(library_pad[0], abs=1e-6)
            assert board_pad[1] == pytest.approx(-library_pad[1], abs=1e-6)
            assert board_pad[2] == pytest.approx(expected_rotation, abs=1e-6)
            assert library_pad[2] == pytest.approx(0.0, abs=1e-6)

        board_path = tmp_path / board_filename
        dxf_path = tmp_path / f"{reference}-gui-flip.dxf"
        shutil.copyfile(board_source, board_path)
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
                "B.CrtYd,B.Cu",
                str(board_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        dxf_lines = _dxf_lines(dxf_path)
        expected_courtyard: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for start, end in _graphic_segments(board_node, "B.CrtYd"):
            transformed_start = connplace_module._transform(  # pyright: ignore[reportPrivateUsage]
                start, board_footprint
            )
            transformed_end = connplace_module._transform(  # pyright: ignore[reportPrivateUsage]
                end, board_footprint
            )
            expected_courtyard.append(
                _canonical_dxf_line(
                    (transformed_start[0], -transformed_start[1]),
                    (transformed_end[0], -transformed_end[1]),
                )
            )
        actual_courtyard = [
            _canonical_dxf_line(start, end)
            for layer, start, end in dxf_lines
            if layer == "b.courtyard"
        ]
        assert sorted(actual_courtyard) == sorted(expected_courtyard)

        library_pad1 = library_pads["1"]
        board_pad1 = board_pads["1"]
        transformed_library_pad = connplace_module._transform_library_point(  # pyright: ignore[reportPrivateUsage]
            (library_pad1[0], library_pad1[1]), board_footprint
        )
        transformed_board_pad = connplace_module._transform(  # pyright: ignore[reportPrivateUsage]
            (board_pad1[0], board_pad1[1]), board_footprint
        )
        expected_pad_center = (
            transformed_library_pad[0],
            -transformed_library_pad[1],
        )
        stored_pad_center = (transformed_board_pad[0], -transformed_board_pad[1])
        assert expected_pad_center == pytest.approx(stored_pad_center, abs=1e-6)
        measured_pad_center = _dxf_pad_center(
            dxf_lines,
            expected_pad_center,
            pad_half_extent,
        )
        assert measured_pad_center == pytest.approx(expected_pad_center, abs=1e-3)

        if reference == "U3":
            pos_path = tmp_path / "video_U3-pos.csv"
            subprocess.run(
                [
                    *_kicad_cli_command(tmp_path),
                    "pcb",
                    "export",
                    "pos",
                    "--format",
                    "csv",
                    "--units",
                    "mm",
                    "--side",
                    "back",
                    "--output",
                    str(pos_path),
                    str(board_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            assert (
                '"U3","74LS245","SOIC-20W_7.5x12.8mm_P1.27mm",168.783000,'
                "-106.553000,0.000000,bottom"
            ) in pos_path.read_text(encoding="utf-8")
