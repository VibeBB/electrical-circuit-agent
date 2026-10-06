from __future__ import annotations

import json
from pathlib import Path

import pytest

from circuit import board_geometry as bg
from circuit import cli, occt, sexpr


def _footprint(
    ref: str,
    lib_id: str,
    *,
    x: float,
    y: float,
    side: str = "F.Cu",
    model: Path | None = None,
    height_property: str | None = None,
    hole: float | None = None,
) -> list[sexpr.SExpr]:
    items: list[sexpr.SExpr] = [
        "footprint",
        sexpr.quoted(lib_id),
        ["layer", sexpr.quoted(side)],
        ["at", str(x), str(y), "0"],
        ["property", sexpr.quoted("Reference"), sexpr.quoted(ref)],
        [
            "fp_rect",
            ["start", "-0.5", "-0.5"],
            ["end", "0.5", "0.5"],
            ["layer", sexpr.quoted("B.CrtYd" if side == "B.Cu" else "F.CrtYd")],
        ],
    ]
    if height_property is not None:
        items.append(["property", sexpr.quoted("circuit_height_mm"), sexpr.quoted(height_property)])
    if model is not None:
        items.append(["model", sexpr.quoted(str(model))])
    if hole is not None:
        items.append(
            [
                "pad",
                sexpr.quoted(""),
                "np_thru_hole",
                "circle",
                ["at", "0", "0"],
                ["size", str(hole), str(hole)],
                ["drill", str(hole)],
            ]
        )
    return items


def _step(path: Path, height: float) -> Path:
    occt.write_step(occt.box(0.0, 0.0, 0.0, 1.0, 1.0, height), path, product_name=path.stem)
    return path


def _board(
    path: Path,
    model: Path,
    *,
    thickness: float | None = 1.6,
    bottom_property_only: bool = False,
) -> Path:
    root: list[sexpr.SExpr] = [
        "kicad_pcb",
        ["general", ["thickness", str(thickness)]] if thickness is not None else ["general"],
        [
            "gr_poly",
            ["pts", ["xy", "0", "0"], ["xy", "20", "0"], ["xy", "20", "10"], ["xy", "0", "10"]],
            ["layer", sexpr.quoted("Edge.Cuts")],
        ],
        _footprint("J1", "Connector_JST:Synthetic", x=19.5, y=5.0, model=model),
        _footprint("R1", "Resistor_SMD:R_0603", x=5.0, y=2.0, model=model),
        _footprint("H1", "MountingHole:MountingHole_3.2mm", x=2.0, y=8.0, hole=3.2),
    ]
    if bottom_property_only:
        root.append(
            _footprint("R2", "Resistor_SMD:R_0603", x=10.0, y=5.0, side="B.Cu", height_property="2")
        )
    path.write_text(sexpr.serialize(root) + "\n", encoding="utf-8")
    return path


def test_geometry_maps_outline_parts_and_holes_into_the_board_frame(tmp_path: Path) -> None:
    model = _step(tmp_path / "part.step", 7.0)
    pcb = _board(tmp_path / "demo.kicad_pcb", model)

    geometry = bg.board_geometry(pcb)

    assert geometry.verdict == "pass", geometry.unknown
    assert (geometry.width_mm, geometry.depth_mm, geometry.thickness_mm) == (20.0, 10.0, 1.6)
    assert bg._signed_area(geometry.outline_mm) > 0  # pyright: ignore[reportPrivateUsage]
    parts = {c.ref: c for c in geometry.components}
    assert set(parts) == {"J1", "R1"}
    assert (parts["R1"].x_mm, parts["R1"].y_mm) == (-5.0, 3.0)
    assert parts["J1"].connector and parts["J1"].edge == "right"
    assert not parts["R1"].connector and parts["R1"].edge is None
    assert parts["R1"].height_mm == pytest.approx(7.0)
    assert parts["R1"].height_sources == ["step_model"]
    assert geometry.max_height_top_mm == pytest.approx(7.0)
    assert geometry.max_height_bottom_mm == 0.0
    assert [(h.ref, h.x_mm, h.y_mm, h.diameter_mm) for h in geometry.mount_holes] == [
        ("H1", -8.0, -3.0, 3.2)
    ]


def test_idf_export_and_pcb_hash_pin(tmp_path: Path) -> None:
    model = _step(tmp_path / "part.step", 7.0)
    pcb = _board(tmp_path / "demo.kicad_pcb", model)
    geometry = bg.board_geometry(pcb)

    written = bg.write_board_geometry(
        geometry, tmp_path / "demo.board-geometry.json", design="demo", idf=True
    )

    emn = Path(written["emn"]).read_text(encoding="utf-8")
    emp = Path(written["emp"]).read_text(encoding="utf-8")
    assert "1.6000" in emn and "3.2000 -8.0000 -3.0000 NPTH H1 MTG UNOWNED" in emn
    assert '"R1_ENVELOPE" "Resistor_SMD:R_0603" "R1"' in emn
    assert '"R1_ENVELOPE" "Resistor_SMD:R_0603" MM 7.0000' in emp
    payload = json.loads(Path(written["geometry"]).read_text(encoding="utf-8"))
    assert payload["artifact_kind"] == "circuit_board_geometry"
    assert len(payload["pcb"]["sha256"]) == 64
    assert bg.write_board_geometry(geometry, tmp_path / "again.json", design="demo", idf=True)
    assert (tmp_path / "again.emn").read_text(encoding="utf-8") == emn


def test_property_only_height_is_unknown_and_blocks_idf(tmp_path: Path) -> None:
    model = _step(tmp_path / "part.step", 7.0)
    pcb = _board(tmp_path / "demo.kicad_pcb", model, bottom_property_only=True)

    geometry = bg.board_geometry(pcb)

    assert geometry.verdict == "fail"
    assert geometry.unknown == ["height:R2"]
    assert geometry.max_height_bottom_mm is None
    assert geometry.max_height_top_mm == pytest.approx(7.0)
    written = bg.write_board_geometry(geometry, tmp_path / "x.json", design="demo", idf=True)
    assert set(written) == {"geometry"}
    with pytest.raises(ValueError, match="passing board geometry"):
        bg.idf_files(geometry, "demo")


@pytest.mark.parametrize(
    ("thickness", "step_bytes", "expected"),
    [
        (None, None, ["thickness"]),
        (1.6, b"not a step file", ["step"]),
    ],
)
def test_missing_inputs_fail_closed(
    tmp_path: Path, thickness: float | None, step_bytes: bytes | None, expected: list[str]
) -> None:
    model = _step(tmp_path / "part.step", 7.0)
    pcb = _board(tmp_path / "demo.kicad_pcb", model, thickness=thickness)
    step = None
    if step_bytes is not None:
        step = tmp_path / "board.step"
        step.write_bytes(step_bytes)

    geometry = bg.board_geometry(pcb, step_path=step)

    assert geometry.verdict == "fail"
    assert geometry.unknown == expected


def test_missing_outline_is_unknown(tmp_path: Path) -> None:
    pcb = tmp_path / "bare.kicad_pcb"
    pcb.write_text(
        sexpr.serialize(["kicad_pcb", ["general", ["thickness", "1.6"]]]) + "\n", encoding="utf-8"
    )

    geometry = bg.board_geometry(pcb)

    assert geometry.unknown == ["outline"]
    assert geometry.width_mm is None and geometry.outline_mm == []


@pytest.mark.parametrize(
    ("right", "face"),
    [(9.49, None), (9.5, "right"), (9.51, "right")],
)
def test_connector_edge_tolerance_boundary(right: float, face: str | None) -> None:
    assert bg._edge((right - 1.0, -0.5, right, 0.5), 10.0, 5.0) == face  # pyright: ignore[reportPrivateUsage]
    assert bg._edge(None, 10.0, 5.0) is None  # pyright: ignore[reportPrivateUsage]


def test_cli_emits_verdict_and_rejects_bad_part_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    model = _step(tmp_path / "part.step", 7.0)
    pcb = _board(tmp_path / "demo.kicad_pcb", model)
    out = tmp_path / "demo.board-geometry.json"

    assert cli.main(["board-geometry", "--pcb", str(pcb), "--out", str(out), "--idf"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "pass"
    assert (tmp_path / "demo.emn").is_file()
    assert (
        cli.main(["board-geometry", "--pcb", str(pcb), "--out", str(out), "--part-spec", "J1"]) == 1
    )
    assert "REF=PATH" in json.loads(capsys.readouterr().out)["detail"]
