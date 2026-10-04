from dataclasses import replace
from pathlib import Path

import pytest

from circuit import connplace as connplace_module
from circuit import occt, sexpr
from circuit.connplace import check_connector_placement
from connector_fixtures import connector_spec


def _footprint(
    ref: str,
    *,
    x: float,
    y: float,
    rotation: float = 0.0,
    side: str = "F.Cu",
    board_edge: bool = False,
    height_property: str | None = None,
    model_path: Path | None = None,
) -> list[sexpr.SExpr]:
    items: list[sexpr.SExpr] = [
        "footprint",
        sexpr.quoted("Connector:Synthetic"),
        ["layer", sexpr.quoted(side)],
        ["at", str(x), str(y), str(rotation)],
        [
            "property",
            sexpr.quoted("Reference"),
            sexpr.quoted(ref),
            ["at", "0", "0", "0"],
            ["layer", sexpr.quoted("F.SilkS")],
        ],
        [
            "fp_rect",
            ["start", "-0.5", "-0.5"],
            ["end", "0.5", "0.5"],
            ["layer", sexpr.quoted("B.CrtYd" if side == "B.Cu" else "F.CrtYd")],
        ],
    ]
    if board_edge:
        items.append(
            [
                "fp_line",
                ["start", "0", "-1"],
                ["end", "0", "1"],
                ["layer", sexpr.quoted("Dwgs.User")],
            ]
        )
    if height_property is not None:
        items.append(
            [
                "property",
                sexpr.quoted("circuit_height_mm"),
                sexpr.quoted(height_property),
                ["at", "0", "0", "0"],
                ["layer", sexpr.quoted("F.Fab")],
            ]
        )
    if model_path is not None:
        items.append(["model", sexpr.quoted(str(model_path))])
    return items


def _board(
    path: Path,
    *,
    position: tuple[float, float] = (10.0, 5.0),
    rotation: float = 0.0,
    side: str = "F.Cu",
    neighbor: bool = False,
    thickness: float | None = 1.6,
    neighbor_height_property: str | None = None,
    neighbor_model_path: Path | None = None,
) -> None:
    root: list[sexpr.SExpr] = [
        "kicad_pcb",
        (["general", ["thickness", str(thickness)]] if thickness is not None else ["general"]),
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
        _footprint(
            "J1",
            x=position[0],
            y=position[1],
            rotation=rotation,
            side=side,
            board_edge=True,
        ),
    ]
    if neighbor:
        root.append(
            _footprint(
                "R1",
                x=12.0,
                y=5.0,
                height_property=neighbor_height_property,
                model_path=neighbor_model_path,
            )
        )
    path.write_text(sexpr.serialize(root) + "\n", encoding="utf-8")


def _spec_path(
    tmp_path: Path,
    *,
    kind: str = "jst_ph_right_angle",
    unknown_envelope: bool = False,
    z_min: float | None = None,
) -> Path:
    spec = connector_spec(kind)
    connector = spec.connector
    assert connector is not None
    if unknown_envelope:
        spec = spec.model_copy(
            update={"connector": connector.model_copy(update={"mating_envelope": None})}
        )
    elif z_min is not None:
        envelope = connector.mating_envelope
        assert envelope is not None
        spec = spec.model_copy(
            update={
                "connector": connector.model_copy(
                    update={"mating_envelope": envelope.model_copy(update={"z_min": z_min})}
                )
            }
        )
    path = tmp_path / "J1.part.spec.json"
    path.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def test_right_angle_connector_at_edge_passes(tmp_path: Path) -> None:
    board = tmp_path / "edge.kicad_pcb"
    _board(board)

    report = check_connector_placement(board, {"J1": _spec_path(tmp_path)})

    assert report.verdict == "pass"
    assert report.findings == []
    assert report.input_hashes["pcb"]


@pytest.mark.parametrize("z_min", [0.0, -0.5e-6])
def test_inland_connector_above_board_fails_edge_without_interference(
    tmp_path: Path,
    z_min: float,
) -> None:
    board = tmp_path / "inland.kicad_pcb"
    _board(board, position=(7.0, 5.0))

    report = check_connector_placement(
        board,
        {"J1": _spec_path(tmp_path, z_min=z_min)},
    )

    codes = {finding.code for finding in report.findings}
    assert "connector_not_at_board_edge" in codes
    assert "connector_mating_board_interference" not in codes


def test_inland_connector_below_board_fails_edge_and_interference(tmp_path: Path) -> None:
    board = tmp_path / "inland-below-board.kicad_pcb"
    _board(board, position=(7.0, 5.0))

    report = check_connector_placement(
        board,
        {"J1": _spec_path(tmp_path, z_min=-0.1)},
    )

    codes = {finding.code for finding in report.findings}
    assert "connector_not_at_board_edge" in codes
    assert "connector_mating_board_interference" in codes


def test_unknown_board_thickness_fails_closed(tmp_path: Path) -> None:
    board = tmp_path / "unknown-thickness.kicad_pcb"
    _board(board, position=(7.0, 5.0), thickness=None)

    report = check_connector_placement(board, {"J1": _spec_path(tmp_path)})

    assert "connector_mating_board_interference" in {finding.code for finding in report.findings}


def test_swept_mating_envelope_reports_obstructing_courtyard(tmp_path: Path) -> None:
    board = tmp_path / "obstructed.kicad_pcb"
    _board(board, neighbor=True)

    report = check_connector_placement(board, {"J1": _spec_path(tmp_path)})

    obstruction = next(
        finding for finding in report.findings if finding.code == "connector_mating_clearance"
    )
    assert obstruction.obstructing_ref == "R1"


def test_height_property_alone_does_not_suppress_clearance_finding(tmp_path: Path) -> None:
    board = tmp_path / "unverified-neighbor-height.kicad_pcb"
    _board(board, neighbor=True, neighbor_height_property="1.0")
    spec_path = _spec_path(tmp_path, kind="header_2x5", z_min=2.0)

    report = check_connector_placement(board, {"J1": spec_path})

    obstruction = next(
        finding for finding in report.findings if finding.code == "connector_mating_clearance"
    )
    assert obstruction.obstructing_ref == "R1"


def test_neighbor_height_uses_maximum_available_source(tmp_path: Path) -> None:
    model_path = tmp_path / "neighbor.step"
    occt.write_step(
        occt.box(0.0, 0.0, 0.0, 1.0, 1.0, 7.0),
        model_path,
        product_name="neighbor",
    )
    board = tmp_path / "neighbor-height.kicad_pcb"
    _board(
        board,
        neighbor=True,
        neighbor_height_property="8.0",
        neighbor_model_path=model_path,
    )
    root = sexpr.parse_text(board.read_text(encoding="utf-8"))
    assert isinstance(root, list)
    footprint_nodes = [
        node
        for node in connplace_module._children(  # pyright: ignore[reportPrivateUsage]
            root, "footprint"
        )
    ]
    neighbor = connplace_module._parse_board_footprint(  # pyright: ignore[reportPrivateUsage]
        footprint_nodes[1]
    )
    spec = connector_spec("header_2x5")
    height_function = connplace_module._neighbor_height  # pyright: ignore[reportPrivateUsage]
    assert height_function(neighbor, spec, board) == pytest.approx(8.0)

    lower_property = replace(
        neighbor,
        properties={**neighbor.properties, "circuit_height_mm": "6.0"},
    )
    assert height_function(lower_property, spec, board) == pytest.approx(7.0)

    package = spec.package
    assert package.height is not None
    taller_height = package.height.model_copy(update={"nom": 10.0})
    taller_spec = spec.model_copy(
        update={"package": package.model_copy(update={"height": taller_height})}
    )
    assert height_function(neighbor, taller_spec, board) == pytest.approx(10.0)


@pytest.mark.parametrize(
    ("rotation", "position"),
    [
        (0.0, (10.0, 5.0)),
        (90.0, (5.0, 10.0)),
        (180.0, (0.0, 5.0)),
        (270.0, (5.0, 0.0)),
    ],
)
def test_connector_board_edge_rotations_pass(
    tmp_path: Path,
    rotation: float,
    position: tuple[float, float],
) -> None:
    board = tmp_path / f"rotation-{rotation}.kicad_pcb"
    _board(board, position=position, rotation=rotation)

    report = check_connector_placement(board, {"J1": _spec_path(tmp_path)})

    assert report.verdict == "pass"


def test_bottom_side_connector_transform_passes_at_opposite_edge(tmp_path: Path) -> None:
    board = tmp_path / "bottom.kicad_pcb"
    _board(board, position=(0.0, 5.0), side="B.Cu")

    report = check_connector_placement(board, {"J1": _spec_path(tmp_path)})

    assert report.verdict == "pass"


def test_unknown_mating_envelope_requests_human_review(tmp_path: Path) -> None:
    board = tmp_path / "unknown.kicad_pcb"
    _board(board)

    report = check_connector_placement(
        board,
        {"J1": _spec_path(tmp_path, unknown_envelope=True)},
    )

    assert report.artifact_kind == "circuit_connector_placement_report"
    assert "connector_mating_envelope_unknown" in {finding.code for finding in report.findings}
    assert report.human_requests


def test_missing_connector_part_spec_fails_closed(tmp_path: Path) -> None:
    board = tmp_path / "missing-spec.kicad_pcb"
    _board(board)

    report = check_connector_placement(board, {})

    assert "connector_placement_part_spec_missing" in {finding.code for finding in report.findings}
    assert report.human_requests


def test_edge_cuts_lines_and_arcs_are_parsed() -> None:
    root = sexpr.parse_text(
        "(kicad_pcb "
        '(gr_line (start 0 0) (end 1 0) (layer "Edge.Cuts")) '
        '(gr_arc (start 1 0) (mid 1.5 0.5) (end 2 0) (layer "Edge.Cuts"))'
        ")"
    )
    assert isinstance(root, list)

    segments, polygon = connplace_module._outline(  # pyright: ignore[reportPrivateUsage]
        root
    )

    assert len(segments) > 4
    assert polygon == []


def test_collinear_disjoint_segments_do_not_intersect() -> None:
    intersect = connplace_module._segments_intersect  # pyright: ignore[reportPrivateUsage]

    assert not intersect(((0.0, 0.0), (1.0, 0.0)), ((2.0, 0.0), (3.0, 0.0)))
    assert intersect(((0.0, 0.0), (1.0, 0.0)), ((1.0, 0.0), (2.0, 0.0)))
